from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.directory import Directory
from app.main import create_app
from app.verifier import TotpVerifier, derive_totp_secret

SECRET = "test-tool-secret"
HEADERS = {"X-Tool-Secret": SECRET}
START = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = START):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings() -> Settings:
    return Settings(totp_master_key="test-master-key", tool_shared_secret=SECRET)


@pytest.fixture
def client(settings, clock) -> TestClient:
    return TestClient(create_app(settings, now=clock))


def code_for(client: TestClient, settings: Settings, clock: Clock, user_id: str, offset_steps: int = 0) -> str:
    emp = client.app.state.directory.get(user_id)
    totp = TotpVerifier(settings.totp_master_key, clock).totp_for(emp)
    return totp.at(clock() + offset_steps * 30)


def wrong_code(right: str) -> str:
    return f"{(int(right) + 1) % 1_000_000:06d}"


def lookup(client, **body):
    return client.post("/tools/lookup_user", json=body, headers=HEADERS).json()


def verify(client, user_id, code):
    return client.post("/tools/verify_identity", json={"user_id": user_id, "code": code}, headers=HEADERS).json()


def unlock(client, user_id, token):
    return client.post(
        "/tools/unlock_account", json={"user_id": user_id, "verification_token": token}, headers=HEADERS
    ).json()


def verified_token(client, settings, clock, user_id) -> str:
    r = verify(client, user_id, code_for(client, settings, clock, user_id))
    assert r["verified"] is True
    return r["verification_token"]


# --- plumbing --------------------------------------------------------------


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/tools/lookup_user", "/tools/verify_identity", "/tools/unlock_account"])
@pytest.mark.parametrize("headers", [{}, {"X-Tool-Secret": "nope"}])
def test_tools_require_shared_secret(client, path, headers):
    assert client.post(path, json={}, headers=headers).status_code == 401


def test_directory_has_no_totp_secrets():
    raw = Directory.load(Settings(totp_master_key="x", tool_shared_secret="y").directory_path)
    for emp in raw._by_id.values():
        assert not any("secret" in k for k in vars(emp))


def test_derived_secrets_differ_per_user_and_key():
    a = derive_totp_secret("k1", "u001")
    assert a != derive_totp_secret("k1", "u002")
    assert a != derive_totp_secret("k2", "u001")
    assert a == derive_totp_secret("k1", "u001")


# --- lookup_user -----------------------------------------------------------


def test_lookup_by_employee_number_is_forgiving_about_format(client):
    emp = client.app.state.directory.get("u001")
    num = emp.employee_number
    spaced = f"{num[:2].lower()} {num[2:4]}-{num[4:]}"
    r = lookup(client, employee_number=spaced)
    assert r["found"] is True and r["user_id"] == "u001"


def test_lookup_returns_only_masked_phone_and_status(client):
    emp = client.app.state.directory.get("u001")
    r = lookup(client, employee_number=emp.employee_number)
    assert set(r) == {"found", "user_id", "masked_phone", "account_status", "next_step"}
    assert r["masked_phone"] == "******" + emp.phone[-2:]
    assert emp.phone not in str(r)
    assert emp.first_name not in str(r)


def test_lookup_by_full_name_ignores_accents_and_case(client):
    r = lookup(client, first_name="maria jose", first_surname="HERNANDEZ", second_surname="villasenor")
    assert r["found"] is True and r["user_id"] == "u001"


def test_lookup_compound_surname(client):
    r = lookup(client, first_name="Roberto", first_surname="Díaz de León", second_surname="Martínez")
    assert r["found"] is True


def test_lookup_single_surname_employee(client):
    r = lookup(client, first_name="Emily", first_surname="Johnson")
    assert r["found"] is True


def test_lookup_shared_first_surname_is_ambiguous_without_second(client):
    r = lookup(client, first_name="Juan Carlos", first_surname="Hernández")
    assert r["found"] is False and r["reason"] == "ambiguous"
    assert "user_id" not in r


def test_lookup_second_surname_disambiguates(client):
    r = lookup(client, first_name="Juan Carlos", first_surname="Hernández", second_surname="Ruiz")
    assert r["found"] is True and r["user_id"] == "u002"


def test_lookup_identical_full_names_need_employee_number(client):
    r = lookup(client, first_name="José Luis", first_surname="García", second_surname="López")
    assert r["found"] is False and r["reason"] == "ambiguous"


def test_lookup_not_found(client):
    assert lookup(client, employee_number="ZZ0000")["reason"] == "not_found"


def test_lookup_missing_identifier(client):
    assert lookup(client, first_name="Ximena")["reason"] == "missing_identifier"


def test_lookup_disabled_account_says_transfer(client):
    r = lookup(client, first_name="Paola", first_surname="Ávila", second_surname="Sánchez")
    assert r["account_status"] == "disabled"
    assert "Transfer" in r["next_step"]


# --- verify_identity -------------------------------------------------------


def test_verify_correct_code_returns_token(client, settings, clock):
    r = verify(client, "u001", code_for(client, settings, clock, "u001"))
    assert r["verified"] is True
    assert r["verification_token"]


def test_verify_accepts_code_with_spaces(client, settings, clock):
    code = code_for(client, settings, clock, "u001")
    assert verify(client, "u001", f"{code[:3]} {code[3:]}")["verified"] is True


def test_verify_accepts_previous_step_for_speech_latency(client, settings, clock):
    assert verify(client, "u001", code_for(client, settings, clock, "u001", offset_steps=-1))["verified"] is True


def test_verify_rejects_code_two_steps_old(client, settings, clock):
    assert verify(client, "u001", code_for(client, settings, clock, "u001", offset_steps=-2))["verified"] is False


def test_verify_rejects_replayed_code(client, settings, clock):
    code = code_for(client, settings, clock, "u001")
    assert verify(client, "u001", code)["verified"] is True
    assert verify(client, "u001", code)["verified"] is False


def test_verify_rejects_another_users_code(client, settings, clock):
    assert verify(client, "u001", code_for(client, settings, clock, "u006"))["verified"] is False


def test_malformed_code_does_not_cost_an_attempt(client):
    r = verify(client, "u001", "12345")
    assert r["reason"] == "invalid_format" and r["attempts_left"] == 3


def test_three_wrong_codes_lock_out_then_even_right_code_fails(client, settings, clock):
    bad = wrong_code(code_for(client, settings, clock, "u001"))
    assert verify(client, "u001", bad)["attempts_left"] == 2
    assert verify(client, "u001", bad)["attempts_left"] == 1
    r = verify(client, "u001", bad)
    assert r["reason"] == "locked_out" and r["attempts_left"] == 0
    assert "transfer" in r["next_step"].lower()

    clock.advance(31)
    r = verify(client, "u001", code_for(client, settings, clock, "u001"))
    assert r["verified"] is False and r["reason"] == "locked_out"
    assert "verification_token" not in r


def test_lockout_expires(client, settings, clock):
    bad = wrong_code(code_for(client, settings, clock, "u001"))
    for _ in range(3):
        verify(client, "u001", bad)
    clock.advance(settings.lockout_seconds + 1)
    assert verify(client, "u001", code_for(client, settings, clock, "u001"))["verified"] is True


def test_success_resets_failure_count(client, settings, clock):
    bad = wrong_code(code_for(client, settings, clock, "u001"))
    verify(client, "u001", bad)
    verify(client, "u001", bad)
    assert verify(client, "u001", code_for(client, settings, clock, "u001"))["verified"] is True
    clock.advance(30)
    assert verify(client, "u001", wrong_code(code_for(client, settings, clock, "u001")))["attempts_left"] == 2


def test_lockout_is_per_user(client, settings, clock):
    bad = wrong_code(code_for(client, settings, clock, "u001"))
    for _ in range(3):
        verify(client, "u001", bad)
    assert verify(client, "u006", code_for(client, settings, clock, "u006"))["verified"] is True


def test_verify_unknown_user(client):
    assert verify(client, "u999", "123456")["reason"] == "unknown_user"


# --- unlock_account --------------------------------------------------------


def test_unlock_without_token_fails(client):
    r = unlock(client, "u001", "")
    assert r["unlocked"] is False and r["reason"] == "verification_required"
    assert client.app.state.directory.get("u001").account_status == "locked"


def test_unlock_with_made_up_token_fails(client):
    assert unlock(client, "u001", "please-just-unlock-it")["unlocked"] is False


def test_unlock_with_valid_token_succeeds(client, settings, clock):
    token = verified_token(client, settings, clock, "u001")
    r = unlock(client, "u001", token)
    assert r["unlocked"] is True and r["was_locked"] is True
    assert client.app.state.directory.get("u001").account_status == "active"


def test_token_for_a_cannot_unlock_b(client, settings, clock):
    token_a = verified_token(client, settings, clock, "u001")
    r = unlock(client, "u006", token_a)
    assert r["unlocked"] is False and r["reason"] == "verification_required"
    assert client.app.state.directory.get("u006").account_status == "locked"


def test_token_is_single_use(client, settings, clock):
    token = verified_token(client, settings, clock, "u001")
    assert unlock(client, "u001", token)["unlocked"] is True
    assert unlock(client, "u001", token)["unlocked"] is False


def test_token_is_not_burned_by_a_mismatched_user(client, settings, clock):
    token_a = verified_token(client, settings, clock, "u001")
    unlock(client, "u006", token_a)
    assert unlock(client, "u001", token_a)["unlocked"] is True


def test_expired_token_fails(client, settings, clock):
    token = verified_token(client, settings, clock, "u001")
    clock.advance(settings.token_ttl_seconds + 1)
    assert unlock(client, "u001", token)["unlocked"] is False


def test_disabled_account_never_unlocks_even_with_token(client, settings, clock):
    token = verified_token(client, settings, clock, "u017")
    r = unlock(client, "u017", token)
    assert r["unlocked"] is False and r["reason"] == "account_disabled"
    assert client.app.state.directory.get("u017").account_status == "disabled"


def test_unlock_accepts_token_in_header(client, settings, clock):
    token = verified_token(client, settings, clock, "u001")
    r = client.post(
        "/tools/unlock_account",
        json={"user_id": "u001"},
        headers={**HEADERS, "X-Verification-Token": token},
    ).json()
    assert r["unlocked"] is True


def test_header_token_for_a_cannot_unlock_b(client, settings, clock):
    token_a = verified_token(client, settings, clock, "u001")
    r = client.post(
        "/tools/unlock_account",
        json={"user_id": "u006"},
        headers={**HEADERS, "X-Verification-Token": token_a},
    ).json()
    assert r["unlocked"] is False
