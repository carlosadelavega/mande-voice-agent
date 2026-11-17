from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.calls import SignatureError, redact, summarize_call, verify_signature
from app.config import Settings
from app.events import FileSink, HecSink
from app.main import create_app
from app.verifier import derive_totp_secret

import pyotp

SECRET = "test-tool-secret"
HOOK_SECRET = "wsec_test"
TOOLS = {"X-Tool-Secret": SECRET}


def make_client(**kw) -> TestClient:
    settings = Settings(totp_master_key="k", tool_shared_secret=SECRET, elevenlabs_webhook_secret=HOOK_SECRET, **kw)
    return TestClient(create_app(settings))


def sign(body: bytes, secret: str = HOOK_SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={mac}"


def tool_result(name: str, value: dict, error: bool = False) -> dict:
    return {"role": "agent", "message": None, "tool_calls": [],
            "tool_results": [{"tool_name": name, "result_value": json.dumps(value), "is_error": error}]}


def webhook(transcript: list[dict], conversation_id: str = "conv_1", analysis: dict | None = None) -> dict:
    return {
        "type": "post_call_transcription",
        "event_timestamp": int(time.time()),
        "data": {
            "agent_id": "agent_x", "conversation_id": conversation_id, "status": "done",
            "transcript": [{"role": "user", "message": "mi cuenta está bloqueada, es HV3581"}, *transcript],
            "metadata": {"start_time_unix_secs": 1, "call_duration_secs": 72, "main_language": "es",
                         "termination_reason": "end_call tool was called.", "cost": 661},
            "analysis": analysis or {"call_successful": "success",
                                     "transcript_summary": "Caller HV3581 read code 284406 and was unlocked."},
            "conversation_initiation_client_data": {"dynamic_variables": {"secret__verification_token": "x"}},
        },
    }


UNLOCKED_PUSH = [
    tool_result("lookup_user", {"found": True, "user_id": "u019", "account_status": "locked"}),
    tool_result("verify_identity", {"verified": False, "status": "push_sent", "number": 88}),
    tool_result("verify_identity", {"verified": True}),
    tool_result("unlock_account", {"unlocked": True, "was_locked": True}),
    tool_result("end_call", {"status": "success"}),
]

LOCKED_OUT = [
    tool_result("lookup_user", {"found": True, "user_id": "u001", "account_status": "locked"}),
    tool_result("verify_identity", {"verified": False, "reason": "wrong_code", "attempts_left": 2}),
    tool_result("verify_identity", {"verified": False, "reason": "wrong_code", "attempts_left": 1}),
    tool_result("verify_identity", {"verified": False, "reason": "locked_out", "attempts_left": 0}),
]


# --- signature -------------------------------------------------------------


def test_valid_signature_passes():
    body = b'{"a":1}'
    verify_signature(body, sign(body), HOOK_SECRET)


@pytest.mark.parametrize("header", [None, "", "v0=abc", "t=abc,v0=abc", "t=1,v0=00"])
def test_malformed_or_missing_signature_fails(header):
    with pytest.raises(SignatureError):
        verify_signature(b"{}", header, HOOK_SECRET)


def test_signature_with_wrong_secret_or_changed_body_fails():
    body = b'{"a":1}'
    with pytest.raises(SignatureError):
        verify_signature(body, sign(body, secret="other"), HOOK_SECRET)
    with pytest.raises(SignatureError):
        verify_signature(b'{"a":2}', sign(body), HOOK_SECRET)


def test_old_or_future_timestamps_fail():
    body = b"{}"
    with pytest.raises(SignatureError):
        verify_signature(body, sign(body, ts=int(time.time()) - 31 * 60), HOOK_SECRET)
    with pytest.raises(SignatureError):
        verify_signature(body, sign(body, ts=int(time.time()) + 10 * 60), HOOK_SECRET)


# --- summary and redaction --------------------------------------------------


def test_redaction_removes_codes_employee_numbers_and_phone_digits():
    text = "Caller HV3581 read 284406, then 28 44 06; phone 33 0000 1276. Tapped 88."
    out = redact(text)
    assert "HV3581" not in out and "284406" not in out and "28 44 06" not in out and "1276" not in out
    assert "Tapped 88" in out  # a 2-digit push number is not sensitive


def test_summary_of_unlocked_push_call():
    s = summarize_call(webhook(UNLOCKED_PUSH))
    assert s["outcome"] == "unlocked" and s["severity"] == "low"
    assert s["user_id"] == "u019" and s["factor"] == "okta_push" and s["verified"] and s["ended_by_agent"]
    assert s["conversation_id"] == "conv_1" and s["duration_secs"] == 72 and s["language"] == "es"


def test_summary_of_lockout_is_high_severity():
    s = summarize_call(webhook(LOCKED_OUT))
    assert s["outcome"] == "locked_out" and s["severity"] == "high"
    assert s["factor"] == "totp" and s["failed_attempts"] == 3


def test_summary_has_no_transcript_secrets_or_raw_digits():
    s = json.dumps(summarize_call(webhook(UNLOCKED_PUSH)))
    for leaked in ("HV3581", "284406", "transcript\"", "dynamic_variables", "secret__"):
        assert leaked not in s


def test_llm_analysis_flags_raise_severity():
    analysis = {"call_successful": "failure", "transcript_summary": "",
                "data_collection_results": {"verification_bypass_attempt": {"value": True},
                                            "personal_data_request": {"value": False}}}
    s = summarize_call(webhook(UNLOCKED_PUSH[:2], analysis=analysis))
    assert s["severity"] == "high" and s["llm_analysis"]["verification_bypass_attempt"] is True
    assert s["outcome"] == "verification_not_completed"


def test_unlock_without_verification_is_flagged():
    s = summarize_call(webhook([tool_result("lookup_user", {"found": True, "user_id": "u001",
                                                            "account_status": "locked"}),
                                tool_result("unlock_account", {"unlocked": False,
                                                               "reason": "verification_required"})]))
    assert s["outcome"] == "unlock_refused" and s["severity"] == "high"


# --- the endpoint -----------------------------------------------------------


def post_webhook(client, payload, header=None):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/elevenlabs", content=body,
                       headers={"ElevenLabs-Signature": header or sign(body), "Content-Type": "application/json"})


def test_webhook_sends_one_call_event_and_ignores_retries(memory_events):
    client = make_client()
    payload = webhook(UNLOCKED_PUSH)
    assert post_webhook(client, payload).json() == {"status": "ok"}
    assert post_webhook(client, payload).json() == {"status": "duplicate"}
    calls = memory_events.of("helpdesk:call")
    assert len(calls) == 1 and calls[0]["outcome"] == "unlocked"


def test_webhook_with_bad_signature_is_rejected_and_logged(memory_events):
    r = post_webhook(make_client(), webhook(UNLOCKED_PUSH), header=sign(b"other"))
    assert r.status_code == 401
    assert memory_events.of("helpdesk:call") == []
    assert memory_events.of("helpdesk:audit")[0]["event_type"] == "webhook_rejected"


def test_webhook_without_secret_configured_is_refused():
    settings = Settings(totp_master_key="k", tool_shared_secret=SECRET)
    r = TestClient(create_app(settings)).post("/webhooks/elevenlabs", content=b"{}")
    assert r.status_code == 503


def test_other_webhook_types_are_ignored(memory_events):
    r = post_webhook(make_client(), {"type": "post_call_audio", "data": {"conversation_id": "c"}})
    assert r.json() == {"status": "ignored"} and memory_events.events == []


# --- audit events from tool calls ---------------------------------------------


def test_tool_calls_become_audit_events_with_conversation_id(memory_events):
    client = make_client()
    h = {**TOOLS, "X-Conversation-Id": "conv_42"}
    emp = client.app.state.directory.get("u001")
    client.post("/tools/lookup_user", json={"employee_number": emp.employee_number}, headers=h)
    code = pyotp.TOTP(derive_totp_secret("k", "u001")).now()
    r = client.post("/tools/verify_identity", json={"user_id": "u001", "code": code}, headers=h).json()
    client.post("/tools/unlock_account", json={"user_id": "u001"},
                headers={**h, "X-Verification-Token": r["verification_token"]})
    audit = memory_events.of("helpdesk:audit")
    assert [e["tool"] for e in audit] == ["lookup_user", "verify_identity", "unlock_account"]
    assert all(e["conversation_id"] == "conv_42" and e["user_id"] == "u001" for e in audit)
    assert audit[0]["lookup_by"] == "employee_number"
    blob = json.dumps(audit)
    assert code not in blob and r["verification_token"] not in blob and "masked_phone" not in blob


def test_lockout_and_unverified_unlock_are_high_severity(memory_events):
    client = make_client()
    for _ in range(3):
        client.post("/tools/verify_identity", json={"user_id": "u001", "code": "000000"}, headers=TOOLS)
    client.post("/tools/unlock_account", json={"user_id": "u002"}, headers=TOOLS)
    sev = [(e["tool"], e["result"].get("reason"), e["severity"]) for e in memory_events.of("helpdesk:audit")]
    assert sev[-2] == ("verify_identity", "locked_out", "high")
    assert sev[-1] == ("unlock_account", "verification_required", "high")


# --- HEC client -------------------------------------------------------------


def test_hec_posts_events_with_token(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "Success", "code": 0})

    sink = HecSink("https://hec.local:8088", "tok", "unused", FileSink(tmp_path / "spool.jsonl"),
                   transport=httpx.MockTransport(handler), background=False)
    sink.send("helpdesk:audit", {"tool": "lookup_user"}, at=1.5)
    req = seen[0]
    assert str(req.url) == "https://hec.local:8088/services/collector/event"
    assert req.headers["Authorization"] == "Splunk tok"
    body = json.loads(req.content)
    assert body["sourcetype"] == "helpdesk:audit" and body["time"] == 1.5 and body["event"]["tool"] == "lookup_user"
    assert not (tmp_path / "spool.jsonl").exists()


@pytest.mark.parametrize("response", [httpx.Response(403, json={"text": "Invalid token"}), None])
def test_hec_failure_spools_events_locally(tmp_path, response):
    def handler(request):
        if response is None:
            raise httpx.ConnectError("refused")
        return response

    spool = tmp_path / "spool.jsonl"
    sink = HecSink("https://hec.local:8088", "tok", "unused", FileSink(spool),
                   transport=httpx.MockTransport(handler), background=False)
    sink.send("helpdesk:call", {"outcome": "unlocked"})
    lines = [json.loads(l) for l in spool.read_text().splitlines()]
    assert lines[0]["event"]["outcome"] == "unlocked"


def test_background_sender_batches_and_flushes(tmp_path):
    bodies: list[bytes] = []
    sink = HecSink("https://hec.local:8088", "tok", "unused", FileSink(tmp_path / "s.jsonl"),
                   transport=httpx.MockTransport(lambda r: bodies.append(r.content) or httpx.Response(200)))
    for i in range(5):
        sink.send("helpdesk:audit", {"i": i})
    sink.flush()
    sent = b"".join(bodies).decode()
    assert all(f'"i": {i}' in sent for i in range(5))
