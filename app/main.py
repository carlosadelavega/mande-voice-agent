"""FastAPI service behind the agent's three webhook tools.

The agent talks; this service decides. Every rule that matters for security
is enforced here, so a caller who talks the agent into skipping a step still
can't unlock anything:

  - lookup_user never returns the full phone or other personal data
  - verify_identity allows 3 wrong codes, then locks verification out
  - unlock_account needs a short-lived, single-use token issued by a
    successful verify_identity for that same user_id

Every tool call is also sent to the SIEM as a helpdesk:audit event, and
ElevenLabs' post-call webhook arrives at /webhooks/elevenlabs and becomes a
helpdesk:call event (see events.py and calls.py).
"""

from __future__ import annotations

import functools
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, model_validator

from .calls import SeenEvents, SignatureError, summarize_call, verify_signature
from .config import Settings
from .directory import Directory, Employee
from .events import EventSink, build_sink
from .okta import OktaError, PushClient
from .okta_auth import build_okta_client
from .verifier import TotpVerifier, Verifier, normalize_code


log = logging.getLogger("helpdesk")


class ToolRequest(BaseModel):
    """Accepts the parameters either flat or wrapped as {"parameters": {...}}."""

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data):
        if isinstance(data, dict) and isinstance(data.get("parameters"), dict):
            return data["parameters"]
        return data


class LookupUserRequest(ToolRequest):
    employee_number: str | None = None
    first_name: str | None = None
    first_surname: str | None = None
    second_surname: str | None = None


class VerifyIdentityRequest(ToolRequest):
    user_id: str
    # Required for TOTP employees; omitted for Okta Verify push employees.
    code: str | None = None


class UnlockAccountRequest(ToolRequest):
    user_id: str
    # Normally sent as the X-Verification-Token header from a secret__ dynamic
    # variable so it never reaches the LLM; the body field is a fallback.
    verification_token: str | None = None


HIDDEN_RESULT_KEYS = {"verification_token", "next_step", "masked_phone"}


def _severity(tool: str, result: dict) -> str:
    reason = result.get("reason")
    if reason == "locked_out" or (tool == "unlock_account" and reason == "verification_required"):
        return "high"
    if reason in ("wrong_code", "push_rejected", "push_expired", "factor_unavailable", "account_disabled") \
            or result.get("account_status") == "disabled":
        return "medium"
    return "info"


def _audited(events: EventSink, now: Callable[[], float]):
    """Log each tool call with secrets redacted, and send it to the SIEM."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            req = kwargs.get("req")
            params = req.model_dump(exclude_none=True) if req is not None else {}
            if "code" in params:
                params["code"] = f"<{len(normalize_code(params['code']))} digits>"
            if "verification_token" in params:
                params["verification_token"] = "<body token>"
            if kwargs.get("x_verification_token"):
                params["header_token"] = True
            shown = {k: v for k, v in result.items() if k not in HIDDEN_RESULT_KEYS}
            log.info("%s [%s] %s -> %s", fn.__name__, kwargs.get("x_conversation_id") or "-", params, shown)
            event = {
                "event_type": "tool_call",
                "tool": fn.__name__,
                "conversation_id": kwargs.get("x_conversation_id"),
                "user_id": params.get("user_id") or result.get("user_id"),
                "severity": _severity(fn.__name__, result),
                "result": shown,
            }
            if fn.__name__ == "lookup_user":
                event["lookup_by"] = "employee_number" if params.get("employee_number") else "name"
            events.send("helpdesk:audit", event, at=now())
            return result

        return wrapper

    return decorator


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class _Token:
    user_id: str
    expires_at: float


@dataclass
class _Push:
    poll_url: str
    number: int | None


@dataclass
class State:
    attempts: dict[str, _Attempts] = field(default_factory=dict)
    tokens: dict[str, _Token] = field(default_factory=dict)
    pushes: dict[str, _Push] = field(default_factory=dict)  # pending push per user_id


def create_app(
    settings: Settings,
    directory: Directory | None = None,
    verifier: Verifier | None = None,
    now: Callable[[], float] = time.time,
    push_client: PushClient | None = None,
    events: EventSink | None = None,
) -> FastAPI:
    directory = directory or Directory.load(settings.directory_path)
    verifier = verifier or TotpVerifier(settings.totp_master_key, now, settings.totp_valid_window)
    if push_client is None and settings.okta_org_url and settings.okta_client_id:
        try:
            push_client = build_okta_client(settings)
        except OktaError as e:  # e.g. key file missing: push employees fail closed
            log.error("okta disabled: %s", e)
    state = State()
    events = events if events is not None else build_sink(settings)
    _logged = _audited(events, now)
    seen_calls = SeenEvents()

    app = FastAPI(title="IT helpdesk unlock tools")
    app.state.directory = directory
    app.state.verifier = verifier
    app.state.state = state
    app.state.events = events

    def require_tool_secret(x_tool_secret: str | None = Header(default=None)) -> None:
        if not x_tool_secret or not hmac.compare_digest(x_tool_secret, settings.tool_shared_secret):
            raise HTTPException(status_code=401, detail="invalid tool secret")

    tools = [Depends(require_tool_secret)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/tools/lookup_user", dependencies=tools)
    @_logged
    def lookup_user(req: LookupUserRequest, x_conversation_id: str | None = Header(default=None)) -> dict:
        if req.employee_number:
            emp = directory.find_by_employee_number(req.employee_number)
            matches = [emp] if emp else []
        elif req.first_name and req.first_surname:
            matches = directory.find_by_name(req.first_name, req.first_surname, req.second_surname)
        else:
            return {
                "found": False,
                "reason": "missing_identifier",
                "next_step": "Ask for the employee number, or the first name and both surnames.",
            }

        if not matches:
            return {
                "found": False,
                "reason": "not_found",
                "next_step": "Confirm the spelling with the caller, or ask for the employee number.",
            }
        if len(matches) > 1:
            return {
                "found": False,
                "reason": "ambiguous",
                "next_step": (
                    "More than one employee matches. Ask for the second surname if the caller has not "
                    "given it; otherwise ask for the employee number."
                ),
            }

        emp = matches[0]
        result = {
            "found": True,
            "user_id": emp.user_id,
            "masked_phone": emp.masked_phone,
            "account_status": emp.account_status,
        }
        if emp.account_status == "disabled":
            result["next_step"] = "This account is disabled and cannot be unlocked by phone. Transfer to a human agent."
        elif emp.account_status == "active":
            result["next_step"] = "The account is not locked. Tell the caller; no unlock is needed."
        elif emp.factor == "okta_push":
            result["next_step"] = (
                "This employee verifies with an Okta Verify push, not a code. Do not ask for a code. "
                "Call verify_identity with only the user_id, then follow its next_step."
            )
        else:
            result["next_step"] = "Ask the caller for the 6-digit code from their authenticator app."
        return result

    @app.post("/tools/verify_identity", dependencies=tools)
    @_logged
    def verify_identity(req: VerifyIdentityRequest, x_conversation_id: str | None = Header(default=None)) -> dict:
        emp = directory.get(req.user_id)
        if emp is None:
            return {"verified": False, "reason": "unknown_user", "next_step": "Look the user up again first."}

        t = now()
        att = state.attempts.setdefault(emp.user_id, _Attempts())
        if att.locked_until > t:
            return _locked_out_response()
        if att.locked_until and att.locked_until <= t:
            state.attempts[emp.user_id] = att = _Attempts()

        if emp.factor == "okta_push":
            return _verify_push(emp, att, t)

        code = normalize_code(req.code)
        if len(code) != 6:
            # Malformed input can't match, so it doesn't cost an attempt.
            return {
                "verified": False,
                "reason": "invalid_format",
                "attempts_left": settings.max_attempts - att.failures,
                "next_step": "The code must be exactly 6 digits. Ask the caller to read it again.",
            }

        if verifier.verify(emp, code):
            return _verified(emp, att, t)
        return _failed(att, t, "wrong_code",
                       "The code is wrong. Ask the caller to wait for a fresh code and read it again.")

    def _verified(emp: Employee, att: _Attempts, t: float) -> dict:
        att.failures = 0
        token = secrets.token_urlsafe(24)
        state.tokens[token] = _Token(user_id=emp.user_id, expires_at=t + settings.token_ttl_seconds)
        return {
            "verified": True,
            "verification_token": token,
            "expires_in_seconds": settings.token_ttl_seconds,
            "next_step": "Identity verified. Call unlock_account now.",
        }

    def _failed(att: _Attempts, t: float, reason: str, next_step: str) -> dict:
        att.failures += 1
        if att.failures >= settings.max_attempts:
            att.locked_until = t + settings.lockout_seconds
            return _locked_out_response()
        return {
            "verified": False,
            "reason": reason,
            "attempts_left": settings.max_attempts - att.failures,
            "next_step": next_step,
        }

    def _verify_push(emp: Employee, att: _Attempts, t: float) -> dict:
        unavailable = {
            "verified": False,
            "reason": "factor_unavailable",
            "next_step": "The verification service is not available. Do not unlock. Tell the caller and "
                         "offer a human colleague from IT.",
        }
        if push_client is None or not emp.okta_login:
            return unavailable
        try:
            pending = state.pushes.get(emp.user_id)
            if pending is None:
                challenge = push_client.start_push(emp.okta_login)
                state.pushes[emp.user_id] = _Push(challenge.poll_url, challenge.number)
                tap = f"tap the number {challenge.number}" if challenge.number is not None else "approve it"
                return {
                    "verified": False,
                    "status": "push_sent",
                    "number": challenge.number,
                    "next_step": f"Tell the caller you sent a notification to their Okta Verify app and that "
                                 f"they must {tap}. Wait until they say they did it, then call "
                                 f"verify_identity again with the same user_id.",
                }
            status = push_client.poll(pending.poll_url)
        except OktaError as e:
            log.warning("okta error for %s: %s", emp.user_id, e)
            state.pushes.pop(emp.user_id, None)
            return unavailable

        if status.result == "WAITING":
            number = pending.number if pending.number is not None else status.number
            return {
                "verified": False,
                "status": "waiting",
                "number": number,
                "next_step": f"Not approved yet. Remind the caller to tap {number} in Okta Verify, wait until "
                             f"they say they did, then call verify_identity again.",
            }
        state.pushes.pop(emp.user_id, None)
        if status.result == "SUCCESS":
            return _verified(emp, att, t)
        if status.result == "REJECTED":
            return _failed(att, t, "push_rejected",
                           "The push was rejected or the wrong number was tapped. Ask the caller if they "
                           "want a new notification; if yes, call verify_identity again.")
        return _failed(att, t, "push_expired",
                       "The push expired before it was approved. Ask the caller if they want a new "
                       "notification; if yes, call verify_identity again.")

    @app.post("/tools/unlock_account", dependencies=tools)
    @_logged
    def unlock_account(
        req: UnlockAccountRequest,
        x_verification_token: str | None = Header(default=None),
        x_conversation_id: str | None = Header(default=None),
    ) -> dict:
        refused = {
            "unlocked": False,
            "reason": "verification_required",
            "next_step": "Do not unlock. The caller must pass verify_identity for this same user first.",
        }
        presented = x_verification_token or req.verification_token or ""
        token = state.tokens.get(presented)
        if token is None or token.user_id != req.user_id or token.expires_at <= now():
            return refused
        del state.tokens[presented]  # single use

        emp = directory.get(req.user_id)
        if emp is None:
            return refused
        if emp.account_status == "disabled":
            return {
                "unlocked": False,
                "reason": "account_disabled",
                "next_step": "This account is disabled and cannot be unlocked by phone. Transfer to a human agent.",
            }
        was_locked = emp.account_status == "locked"
        emp.account_status = "active"
        return {"unlocked": True, "was_locked": was_locked, "next_step": "Tell the caller the account is unlocked."}

    @app.post("/webhooks/elevenlabs")
    async def elevenlabs_webhook(request: Request) -> dict:
        """Post-call webhook. Checked, de-duplicated, reduced to one event."""
        secret = settings.webhook_secret()
        if not secret:
            raise HTTPException(status_code=503, detail="webhook secret not configured")
        raw = await request.body()
        try:
            verify_signature(raw, request.headers.get("elevenlabs-signature"), secret, now())
        except SignatureError as e:
            log.warning("rejected webhook: %s", e)
            events.send("helpdesk:audit", {"event_type": "webhook_rejected", "reason": str(e),
                                           "severity": "medium"}, at=now())
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        if payload.get("type") != "post_call_transcription":
            return {"status": "ignored"}
        conversation_id = (payload.get("data") or {}).get("conversation_id")
        if not seen_calls.first_time((conversation_id, payload.get("type"))):
            return {"status": "duplicate"}
        summary = summarize_call(payload)
        log.info("call %s -> %s (%s)", conversation_id, summary["outcome"], summary["severity"])
        events.send("helpdesk:call", summary, at=payload.get("event_timestamp") or now())
        return {"status": "ok"}

    def _locked_out_response() -> dict:
        return {
            "verified": False,
            "reason": "locked_out",
            "attempts_left": 0,
            "next_step": (
                "Too many wrong codes. Do not try again and do not unlock. "
                "Offer to transfer the caller to a human agent, or end the call politely."
            ),
        }

    return app


def build_app() -> FastAPI:
    """Entry point for uvicorn: `uvicorn app.main:build_app --factory`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    return create_app(Settings.from_env())
