"""ElevenLabs post-call webhook: check it, then reduce the call to one event.

A post-call webhook carries the whole conversation: every turn, tool calls and
results, cost, and ElevenLabs' own analysis. A SIEM needs much less. This
module turns it into one compact helpdesk:call event:

  - what happened, from the tool results the server returned (who was looked
    up, which factor, failed attempts, lockout, unlock), not from the
    agent's words;
  - the analysis ElevenLabs ran after the call (summary, and two yes/no
    security questions), marked as LLM output;
  - no transcript. Digits and employee numbers are removed from the free text
    that is kept, because callers read codes aloud.

The full transcript stays in ElevenLabs, under conversation_id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict

SIGNATURE_TOLERANCE_SECS = 30 * 60  # same window as the ElevenLabs SDK
MAX_FUTURE_SKEW_SECS = 5 * 60

FAILED_VERIFY_REASONS = {"wrong_code", "push_rejected", "push_expired"}


class SignatureError(Exception):
    pass


def verify_signature(raw_body: bytes, header: str | None, secret: str, now: float | None = None) -> None:
    """Check the ElevenLabs-Signature header: "t=<unix secs>,v0=<hex hmac>",
    where the HMAC-SHA256 is over "<t>.<raw body>" with the webhook secret."""
    if not header:
        raise SignatureError("missing signature header")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v0")
    if not ts or not sig or not ts.isdigit():
        raise SignatureError("malformed signature header")
    now = time.time() if now is None else now
    age = now - int(ts)
    if age > SIGNATURE_TOLERANCE_SECS or age < -MAX_FUTURE_SKEW_SECS:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("signature mismatch")


class SeenEvents:
    """Remembers recent deliveries so a retried webhook is processed once."""

    def __init__(self, size: int = 1000) -> None:
        self._seen: OrderedDict[tuple, None] = OrderedDict()
        self._size = size

    def first_time(self, key: tuple) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


_EMPLOYEE_NUMBER = re.compile(r"\b[A-Za-z]{2}[\s-]?\d{4}\b")
_DIGIT_RUN = re.compile(r"\d(?:[\s.,-]?\d){2,}")


def redact(text: str | None) -> str | None:
    """Remove employee numbers and any run of 3+ digits (codes, phone numbers)."""
    if not text:
        return text
    text = _EMPLOYEE_NUMBER.sub("[employee_number]", text)
    return _DIGIT_RUN.sub("[digits]", text)


def _tool_results(transcript: list[dict]) -> list[tuple[str, dict, bool]]:
    out = []
    for turn in transcript or []:
        for r in turn.get("tool_results") or []:
            try:
                value = json.loads(r.get("result_value") or "{}")
            except (TypeError, ValueError):
                value = {}
            out.append((r.get("tool_name") or "", value if isinstance(value, dict) else {}, bool(r.get("is_error"))))
    return out


def _outcome(s: dict) -> str:
    if s["unlocked"]:
        return "unlocked"
    if s["locked_out"]:
        return "locked_out"
    if s["unlock_refused"]:
        return "unlock_refused"
    if s["account_status"] == "disabled":
        return "disabled_account"
    if s["account_status"] == "active":
        return "not_locked"
    if s["factor_unavailable"]:
        return "factor_unavailable"
    if s["user_id"] and not s["verified"]:
        return "verification_not_completed"
    if s["lookups"]:
        return "not_identified"
    return "no_lookup"


def summarize_call(payload: dict) -> dict:
    data = payload.get("data") or {}
    meta = data.get("metadata") or {}
    analysis = data.get("analysis") or {}

    s = {"user_id": None, "account_status": None, "lookups": 0, "lookups_not_found": 0, "lookups_ambiguous": 0,
         "factor": None, "failed_attempts": 0, "verified": False, "locked_out": False, "factor_unavailable": False,
         "unlocked": False, "unlock_refused": False, "tool_errors": 0, "ended_by_agent": False}
    for name, value, is_error in _tool_results(data.get("transcript") or []):
        s["tool_errors"] += is_error
        if name == "lookup_user":
            s["lookups"] += 1
            if value.get("found"):
                s["user_id"] = value.get("user_id")
                s["account_status"] = value.get("account_status")
            elif value.get("reason") == "not_found":
                s["lookups_not_found"] += 1
            elif value.get("reason") == "ambiguous":
                s["lookups_ambiguous"] += 1
        elif name == "verify_identity":
            if value.get("status") in ("push_sent", "waiting") or str(value.get("reason", "")).startswith("push_"):
                s["factor"] = "okta_push"
            elif s["factor"] is None and value:
                s["factor"] = "totp"
            if value.get("verified"):
                s["verified"] = True
            reason = value.get("reason")
            if reason in FAILED_VERIFY_REASONS:
                s["failed_attempts"] += 1
            elif reason == "locked_out":
                s["locked_out"] = True
            elif reason == "factor_unavailable":
                s["factor_unavailable"] = True
        elif name == "unlock_account":
            if value.get("unlocked"):
                s["unlocked"] = True
            elif value.get("reason") == "verification_required":
                s["unlock_refused"] = True
        elif name == "end_call":
            s["ended_by_agent"] = True
    if s["locked_out"]:
        s["failed_attempts"] = max(s["failed_attempts"], 3)

    questions = {k: v.get("value") if isinstance(v, dict) else v
                 for k, v in (analysis.get("data_collection_results") or {}).items()}
    bypass = questions.get("verification_bypass_attempt") is True
    personal = questions.get("personal_data_request") is True

    if s["locked_out"] or s["unlock_refused"] or bypass:
        severity = "high"
    elif s["failed_attempts"] or personal or s["lookups_not_found"] >= 2 or s["factor_unavailable"]:
        severity = "medium"
    else:
        severity = "low"

    return {
        "event_type": "call_summary",
        "conversation_id": data.get("conversation_id"),
        "agent_id": data.get("agent_id"),
        "start_time_unix": meta.get("start_time_unix_secs"),
        "duration_secs": meta.get("call_duration_secs"),
        "language": meta.get("main_language"),
        "termination_reason": meta.get("termination_reason"),
        "cost_credits": meta.get("cost"),
        "outcome": _outcome(s),
        "severity": severity,
        **s,
        "llm_analysis": {
            "note": "Written by an LLM after the call; context, not proof.",
            "call_successful": analysis.get("call_successful"),
            "summary": redact(analysis.get("transcript_summary")),
            "verification_bypass_attempt": questions.get("verification_bypass_attempt"),
            "personal_data_request": questions.get("personal_data_request"),
        },
    }
