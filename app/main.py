"""FastAPI service behind the agent's three webhook tools.

The agent talks; this service decides. Every rule that matters for security
is enforced here, so a caller who talks the agent into skipping a step still
can't unlock anything:

  - lookup_user never returns the full phone or other personal data
  - verify_identity allows 3 wrong codes, then locks verification out
  - unlock_account needs a short-lived, single-use token issued by a
    successful verify_identity for that same user_id
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import Settings
from .directory import Directory
from .verifier import TotpVerifier, Verifier, normalize_code


class LookupUserRequest(BaseModel):
    employee_number: str | None = None
    first_name: str | None = None
    first_surname: str | None = None
    second_surname: str | None = None


class VerifyIdentityRequest(BaseModel):
    user_id: str
    code: str


class UnlockAccountRequest(BaseModel):
    user_id: str
    # Normally sent as the X-Verification-Token header from a secret__ dynamic
    # variable so it never reaches the LLM; the body field is a fallback.
    verification_token: str | None = None


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class _Token:
    user_id: str
    expires_at: float


@dataclass
class State:
    attempts: dict[str, _Attempts] = field(default_factory=dict)
    tokens: dict[str, _Token] = field(default_factory=dict)


def create_app(
    settings: Settings,
    directory: Directory | None = None,
    verifier: Verifier | None = None,
    now: Callable[[], float] = time.time,
) -> FastAPI:
    directory = directory or Directory.load(settings.directory_path)
    verifier = verifier or TotpVerifier(settings.totp_master_key, now, settings.totp_valid_window)
    state = State()

    app = FastAPI(title="IT helpdesk unlock tools")
    app.state.directory = directory
    app.state.verifier = verifier
    app.state.state = state

    def require_tool_secret(x_tool_secret: str | None = Header(default=None)) -> None:
        if not x_tool_secret or not hmac.compare_digest(x_tool_secret, settings.tool_shared_secret):
            raise HTTPException(status_code=401, detail="invalid tool secret")

    tools = [Depends(require_tool_secret)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/tools/lookup_user", dependencies=tools)
    def lookup_user(req: LookupUserRequest) -> dict:
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
        else:
            result["next_step"] = "Ask the caller for the 6-digit code from their authenticator app."
        return result

    @app.post("/tools/verify_identity", dependencies=tools)
    def verify_identity(req: VerifyIdentityRequest) -> dict:
        emp = directory.get(req.user_id)
        if emp is None:
            return {"verified": False, "reason": "unknown_user", "next_step": "Look the user up again first."}

        t = now()
        att = state.attempts.setdefault(emp.user_id, _Attempts())
        if att.locked_until > t:
            return _locked_out_response()
        if att.locked_until and att.locked_until <= t:
            state.attempts[emp.user_id] = att = _Attempts()

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
            att.failures = 0
            token = secrets.token_urlsafe(24)
            state.tokens[token] = _Token(user_id=emp.user_id, expires_at=t + settings.token_ttl_seconds)
            return {
                "verified": True,
                "verification_token": token,
                "expires_in_seconds": settings.token_ttl_seconds,
                "next_step": "Identity verified. Call unlock_account now.",
            }

        att.failures += 1
        if att.failures >= settings.max_attempts:
            att.locked_until = t + settings.lockout_seconds
            return _locked_out_response()
        return {
            "verified": False,
            "reason": "wrong_code",
            "attempts_left": settings.max_attempts - att.failures,
            "next_step": "The code is wrong. Ask the caller to wait for a fresh code and read it again.",
        }

    @app.post("/tools/unlock_account", dependencies=tools)
    def unlock_account(
        req: UnlockAccountRequest, x_verification_token: str | None = Header(default=None)
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
    return create_app(Settings.from_env())
