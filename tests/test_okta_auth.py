from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.okta import OktaError, OktaPushClient
from app.okta_auth import OktaOAuth, rsa_thumbprint

ORG = "https://integrator-test.okta.com"
TOKEN_URL = f"{ORG}/oauth2/v1/token"
CLIENT_ID = "0oaTESTCLIENT"


@pytest.fixture(scope="module")
def client_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def decode_dpop(proof: str) -> tuple[dict, dict]:
    """Check a DPoP proof's signature with the key embedded in its header."""
    header = jwt.get_unverified_header(proof)
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    jwk = header["jwk"]
    assert "d" not in jwk  # only the public part is sent
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=="), "big"),
        int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=="), "big"),
        ec.SECP256R1(),
    ).public_key()
    return header, jwt.decode(proof, pub, algorithms=["ES256"], leeway=7200)


class FakeOkta:
    """Token endpoint plus one API endpoint, with optional DPoP nonce challenges."""

    def __init__(self, client_key, token_nonce=None, api_nonce=None, expires_in=3600):
        self.client_key = client_key
        self.token_nonce = token_nonce
        self.api_nonce = api_nonce
        self.expires_in = expires_in
        self.token_calls = 0
        self.api_calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return self.token(request)
        return self.api(request)

    def token(self, request):
        _, proof = decode_dpop(request.headers["DPoP"])
        assert proof["htm"] == "POST" and proof["htu"] == TOKEN_URL
        if self.token_nonce and proof.get("nonce") != self.token_nonce:
            return httpx.Response(400, json={"error": "use_dpop_nonce"}, headers={"DPoP-Nonce": self.token_nonce})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["grant_type"] == "client_credentials"
        assert form["scope"] == "okta.users.read okta.users.manage"
        assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        assertion = jwt.decode(form["client_assertion"], self.client_key.public_key(),
                               algorithms=["RS256"], audience=TOKEN_URL,
                               leeway=7200)  # tests move the clock ahead
        assert assertion["iss"] == assertion["sub"] == CLIENT_ID
        assert jwt.get_unverified_header(form["client_assertion"])["kid"] == rsa_thumbprint(self.client_key)
        self.token_calls += 1
        return httpx.Response(200, json={"access_token": f"at{self.token_calls}", "token_type": "DPoP",
                                         "expires_in": self.expires_in, "scope": form["scope"]})

    def api(self, request):
        self.api_calls.append(request)
        scheme, token = request.headers["Authorization"].split(" ", 1)
        assert scheme == "DPoP"
        _, proof = decode_dpop(request.headers["DPoP"])
        assert proof["htm"] == request.method
        assert proof["htu"] == f"{ORG}{request.url.path}"  # decoded path, no query
        assert proof["ath"] == base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).rstrip(b"=").decode()
        if self.api_nonce and proof.get("nonce") != self.api_nonce:
            return httpx.Response(401, headers={"DPoP-Nonce": self.api_nonce,
                                                "WWW-Authenticate": 'DPoP error="use_dpop_nonce"'})
        return httpx.Response(200, json={"id": "00u1", "token_seen": token})


def http(auth, fake) -> httpx.Client:
    return httpx.Client(base_url=ORG, auth=auth, transport=httpx.MockTransport(fake.handler))


def test_gets_scoped_dpop_token_and_signs_api_calls(client_key):
    fake = FakeOkta(client_key)
    c = http(OktaOAuth(ORG, CLIENT_ID, pem(client_key)), fake)
    r = c.get("/api/v1/users/some.one%40example.com?expand=x")
    assert r.status_code == 200 and r.json()["token_seen"] == "at1"
    proof = decode_dpop(fake.api_calls[0].headers["DPoP"])[1]
    assert proof["htu"] == f"{ORG}/api/v1/users/some.one@example.com"


def test_token_is_reused_until_close_to_expiry(client_key):
    clock = [time.time()]
    fake = FakeOkta(client_key, expires_in=3600)
    c = http(OktaOAuth(ORG, CLIENT_ID, pem(client_key), now=lambda: clock[0]), fake)
    c.get("/api/v1/users/a")
    c.get("/api/v1/users/b")
    assert fake.token_calls == 1
    clock[0] += 3600 - 30  # inside the 60 s safety margin
    assert c.get("/api/v1/users/c").json()["token_seen"] == "at2"


def test_each_proof_is_unique(client_key):
    fake = FakeOkta(client_key)
    c = http(OktaOAuth(ORG, CLIENT_ID, pem(client_key)), fake)
    c.get("/api/v1/users/a")
    c.get("/api/v1/users/a")
    jtis = {decode_dpop(r.headers["DPoP"])[1]["jti"] for r in fake.api_calls}
    assert len(jtis) == 2


def test_retries_token_request_with_server_nonce(client_key):
    fake = FakeOkta(client_key, token_nonce="n-token")
    c = http(OktaOAuth(ORG, CLIENT_ID, pem(client_key)), fake)
    assert c.get("/api/v1/users/a").status_code == 200
    assert fake.token_calls == 1


def test_retries_api_call_with_server_nonce(client_key):
    fake = FakeOkta(client_key, api_nonce="n-api")
    c = http(OktaOAuth(ORG, CLIENT_ID, pem(client_key)), fake)
    assert c.get("/api/v1/users/a").status_code == 200
    assert len(fake.api_calls) == 2


def test_token_refused_becomes_okta_error(client_key):
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client"})
    auth = OktaOAuth(ORG, CLIENT_ID, pem(client_key))
    push = OktaPushClient(ORG, auth, transport=httpx.MockTransport(handler))
    with pytest.raises(OktaError, match="invalid_client"):
        push.start_push("someone@example.com")


def test_missing_key_file_is_okta_error(tmp_path):
    with pytest.raises(OktaError):
        OktaOAuth.from_file(ORG, CLIENT_ID, tmp_path / "nope.pem")


def test_private_keys_never_leave_the_process(client_key):
    seen: list[bytes] = []
    fake = FakeOkta(client_key)

    def handler(request):
        seen.append(request.content + json.dumps(dict(request.headers)).encode())
        return fake.handler(request)
    auth = OktaOAuth(ORG, CLIENT_ID, pem(client_key))
    httpx.Client(base_url=ORG, auth=auth, transport=httpx.MockTransport(handler)).get("/api/v1/users/a")
    blob = b"".join(seen)
    assert b"PRIVATE KEY" not in blob
    d = auth._dpop_key.private_numbers().private_value.to_bytes(32, "big")
    assert base64.urlsafe_b64encode(d).rstrip(b"=") not in blob
