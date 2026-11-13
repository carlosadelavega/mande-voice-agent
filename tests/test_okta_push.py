from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.okta import OktaError, OktaPushClient, PushChallenge, PushStatus

SECRET = "test-tool-secret"
HEADERS = {"X-Tool-Secret": SECRET}
PUSH_USER = "u019"
ORG = "https://integrator-test.okta.com"


class FakePush:
    """Scripted stand-in for Okta: each poll returns the next result."""

    def __init__(self, results: list[str] | None = None, number: int = 42, fail: bool = False):
        self.results = list(results or [])
        self.number = number
        self.fail = fail
        self.started = 0

    def start_push(self, okta_login: str) -> PushChallenge:
        if self.fail:
            raise OktaError("down")
        self.started += 1
        return PushChallenge(poll_url=f"{ORG}/poll/{self.started}", number=self.number)

    def poll(self, poll_url: str) -> PushStatus:
        if self.fail:
            raise OktaError("down")
        return PushStatus(result=self.results.pop(0), number=self.number)


def make_client(push) -> TestClient:
    settings = Settings(totp_master_key="k", tool_shared_secret=SECRET)
    return TestClient(create_app(settings, push_client=push))


def verify(client, **body):
    return client.post("/tools/verify_identity", json={"user_id": PUSH_USER, **body}, headers=HEADERS).json()


def unlock(client, token):
    return client.post("/tools/unlock_account", json={"user_id": PUSH_USER},
                       headers={**HEADERS, "X-Verification-Token": token}).json()


def test_lookup_tells_agent_to_use_push():
    client = make_client(FakePush())
    emp = client.app.state.directory.get(PUSH_USER)
    r = client.post("/tools/lookup_user", json={"employee_number": emp.employee_number}, headers=HEADERS).json()
    assert "Okta Verify push" in r["next_step"]
    assert "okta" not in json.dumps({k: v for k, v in r.items() if k != "next_step"}).lower()


def test_first_call_sends_push_with_number():
    push = FakePush()
    r = verify(make_client(push))
    assert r["status"] == "push_sent" and r["number"] == 42 and r["verified"] is False
    assert "tap the number 42" in r["next_step"]
    assert push.started == 1


def test_approved_push_issues_token_that_unlocks():
    client = make_client(FakePush(["WAITING", "SUCCESS"]))
    verify(client)
    assert verify(client)["status"] == "waiting"
    r = verify(client)
    assert r["verified"] is True
    assert unlock(client, r["verification_token"])["unlocked"] is True


def test_code_is_ignored_for_push_employee():
    push = FakePush()
    r = verify(make_client(push), code="123456")
    assert r["status"] == "push_sent"


def test_rejected_push_costs_an_attempt_and_next_call_sends_new_push():
    push = FakePush(["REJECTED"])
    client = make_client(push)
    verify(client)
    r = verify(client)
    assert r["reason"] == "push_rejected" and r["attempts_left"] == 2
    assert verify(client)["status"] == "push_sent"
    assert push.started == 2


def test_three_failed_pushes_lock_out():
    client = make_client(FakePush(["REJECTED", "TIMEOUT", "REJECTED"]))
    for _ in range(2):
        verify(client)
        verify(client)
    verify(client)
    r = verify(client)
    assert r["reason"] == "locked_out"
    assert verify(client)["reason"] == "locked_out"


def test_okta_down_fails_closed_without_costing_attempts():
    client = make_client(FakePush(fail=True))
    r = verify(client)
    assert r["reason"] == "factor_unavailable" and "verification_token" not in r
    assert client.app.state.state.attempts[PUSH_USER].failures == 0


def test_push_employee_without_okta_configured_fails_closed():
    r = verify(make_client(None))
    assert r["reason"] == "factor_unavailable"


def test_totp_employees_unaffected():
    r = make_client(FakePush()).post(
        "/tools/verify_identity", json={"user_id": "u001", "code": "12345"}, headers=HEADERS).json()
    assert r["reason"] == "invalid_format"


# --- OktaPushClient against a mocked Okta API -------------------------------


def okta_transport(verify_body: dict, poll_bodies: list[dict], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v1/users/sebastian.lozano%40example.com" or path == "/api/v1/users/sebastian.lozano@example.com":
            return httpx.Response(200, json={"id": "00u1"})
        if path == "/api/v1/users/00u1/factors":
            return httpx.Response(200, json=[
                {"id": "sms1", "factorType": "sms", "status": "ACTIVE"},
                {"id": "opf1", "factorType": "push", "provider": "OKTA", "status": "ACTIVE"},
            ])
        if path == "/api/v1/users/00u1/factors/opf1/verify":
            return httpx.Response(201, json=verify_body)
        if path.startswith("/api/v1/users/00u1/factors/opf1/transactions/"):
            return httpx.Response(200, json=poll_bodies.pop(0))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


POLL = f"{ORG}/api/v1/users/00u1/factors/opf1/transactions/v2t1"


def test_client_starts_number_matching_push():
    seen: list[httpx.Request] = []
    body = {"factorResult": "WAITING", "_links": {"poll": {"href": POLL}},
            "_embedded": {"challenge": {"correctAnswer": 37}}}
    c = OktaPushClient(ORG, "tok", transport=okta_transport(body, [], seen))
    ch = c.start_push("sebastian.lozano@example.com")
    assert ch.poll_url == POLL and ch.number == 37
    verify_req = [r for r in seen if r.url.path.endswith("/verify")][0]
    assert json.loads(verify_req.content) == {"useNumberMatchingChallenge": True}
    assert verify_req.headers["Authorization"] == "SSWS tok"
    assert verify_req.headers["User-Agent"]


def test_client_gets_number_from_poll_when_verify_lacks_it():
    seen: list[httpx.Request] = []
    body = {"factorResult": "WAITING", "_links": {"poll": {"href": POLL}}}
    polls = [{"factorResult": "WAITING", "_embedded": {"challenge": {"correctAnswer": 91}}},
             {"factorResult": "SUCCESS"}]
    c = OktaPushClient(ORG, "tok", transport=okta_transport(body, polls, seen))
    assert c.start_push("sebastian.lozano@example.com").number == 91
    assert c.poll(POLL).result == "SUCCESS"


def test_client_refuses_poll_links_outside_the_org():
    c = OktaPushClient(ORG, "tok", transport=okta_transport({}, [], []))
    with pytest.raises(OktaError):
        c.poll("https://evil.example.com/steal")


def test_client_errors_become_okta_error():
    def handler(request):
        return httpx.Response(401, json={"errorCode": "E0000011"})
    c = OktaPushClient(ORG, "bad", transport=httpx.MockTransport(handler))
    with pytest.raises(OktaError):
        c.start_push("sebastian.lozano@example.com")
