"""Okta Verify push with number challenge, via the Okta Factors API.

Flow for one verification:
  1. find the user's active push factor (by Okta login)
  2. POST .../factors/{factorId}/verify with {"useNumberMatchingChallenge": true}
  3. tell the caller which number to tap (correctAnswer)
  4. GET the poll link until factorResult is SUCCESS, REJECTED or TIMEOUT

Authentication is a scoped OAuth token with DPoP (see okta_auth.py). The
private key stays on the server; nothing about Okta appears in the agent's config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx


class OktaError(Exception):
    """Okta could not be reached or answered something unusable. Fail closed."""


@dataclass
class PushChallenge:
    poll_url: str
    number: int | None


@dataclass
class PushStatus:
    result: str  # WAITING | SUCCESS | REJECTED | TIMEOUT | anything else Okta sends
    number: int | None


class PushClient(Protocol):
    def start_push(self, okta_login: str) -> PushChallenge: ...
    def poll(self, poll_url: str) -> PushStatus: ...


def _correct_answer(body: dict) -> int | None:
    challenge = (body.get("_embedded") or {}).get("challenge") or {}
    value = challenge.get("correctAnswer")
    return int(value) if value is not None else None


class OktaPushClient:
    def __init__(self, org_url: str, auth: httpx.Auth, transport: httpx.BaseTransport | None = None):
        self._org_url = org_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._org_url,
            auth=auth,
            headers={
                "Accept": "application/json",
                # Okta requires a User-Agent to verify push factors.
                "User-Agent": "helpdesk-unlock-demo/1.0",
            },
            timeout=10.0,
            transport=transport,
        )
        self._factor_cache: dict[str, tuple[str, str]] = {}

    def _get(self, url: str) -> dict | list:
        try:
            r = self._http.get(url)
        except httpx.HTTPError as e:
            raise OktaError(f"GET failed: {e}") from e
        if r.status_code != 200:
            raise OktaError(f"GET {url} returned {r.status_code}")
        return r.json()

    def _push_factor(self, okta_login: str) -> tuple[str, str]:
        if okta_login not in self._factor_cache:
            user = self._get(f"/api/v1/users/{quote(okta_login, safe='')}")
            user_id = user["id"]
            factors = self._get(f"/api/v1/users/{user_id}/factors")
            push = [f for f in factors if f.get("factorType") == "push" and f.get("status") == "ACTIVE"]
            if not push:
                raise OktaError(f"no active push factor for {okta_login}")
            self._factor_cache[okta_login] = (user_id, push[0]["id"])
        return self._factor_cache[okta_login]

    def start_push(self, okta_login: str) -> PushChallenge:
        user_id, factor_id = self._push_factor(okta_login)
        try:
            r = self._http.post(
                f"/api/v1/users/{user_id}/factors/{factor_id}/verify",
                json={"useNumberMatchingChallenge": True},
            )
        except httpx.HTTPError as e:
            raise OktaError(f"verify failed: {e}") from e
        if r.status_code not in (200, 201):
            raise OktaError(f"verify returned {r.status_code}: {r.text[:200]}")
        body = r.json()
        poll_url = ((body.get("_links") or {}).get("poll") or {}).get("href")
        if not poll_url:
            raise OktaError("verify response has no poll link")
        number = _correct_answer(body)
        if number is None:
            # The docs show the number on the poll response; fetch it once now.
            number = self.poll(poll_url).number
        return PushChallenge(poll_url=poll_url, number=number)

    def poll(self, poll_url: str) -> PushStatus:
        # Only ever follow links back to our own org.
        if not poll_url.startswith(self._org_url + "/"):
            raise OktaError("poll link points outside the Okta org")
        body = self._get(poll_url)
        return PushStatus(result=str(body.get("factorResult", "")), number=_correct_answer(body))
