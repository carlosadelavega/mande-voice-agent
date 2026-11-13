"""Second-factor verification.

One interface, swappable backends. The minimum version uses TOTP via pyotp;
an Okta Verify push backend can implement the same interface later without
the agent's prompt or tool contract changing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Callable, Protocol

import pyotp

from .directory import Employee


def derive_totp_secret(master_key: str, user_id: str) -> str:
    """Per-user base32 TOTP secret derived from one master key in .env.

    Keeps secrets out of the committed directory file while staying
    deterministic, so the code-printing script and the server agree.
    """
    digest = hmac.new(master_key.encode(), f"totp:{user_id}".encode(), hashlib.sha256).digest()
    return base64.b32encode(digest[:20]).decode().rstrip("=")


def normalize_code(code: str | None) -> str:
    return re.sub(r"\D", "", code or "")


class Verifier(Protocol):
    def verify(self, employee: Employee, code: str) -> bool: ...


class TotpVerifier:
    STEP = 30

    def __init__(self, master_key: str, now: Callable[[], float], valid_window: int = 1):
        self._master_key = master_key
        self._now = now
        self._valid_window = valid_window
        # Last accepted time-step per user, so a code can't be replayed.
        self._last_counter: dict[str, int] = {}

    def totp_for(self, employee: Employee) -> pyotp.TOTP:
        return pyotp.TOTP(derive_totp_secret(self._master_key, employee.user_id), interval=self.STEP)

    def verify(self, employee: Employee, code: str) -> bool:
        totp = self.totp_for(employee)
        now = self._now()
        current = int(now // self.STEP)
        for offset in range(-self._valid_window, self._valid_window + 1):
            counter = current + offset
            if hmac.compare_digest(totp.at(counter * self.STEP), code):
                if counter <= self._last_counter.get(employee.user_id, -1):
                    return False  # replay of an already-used code
                self._last_counter[employee.user_id] = counter
                return True
        return False
