"""Scoped OAuth access to the Okta API, with DPoP.

Instead of a static admin API token, the server signs in as an Okta
"API Services" app:

  1. It signs a short-lived JWT with its private key (private_key_jwt) and
     exchanges it at {org}/oauth2/v1/token for an access token that only
     carries the scopes we ask for (okta.users.read, okta.users.manage).
     The app's admin role limits it further, to one group of users.
  2. The token is bound to a second key that lives only in this process
     (DPoP). Every request carries a fresh proof signed with that key, so a
     leaked access token is useless on its own.
  3. The token lasts about an hour and is reused until shortly before it
     expires.

Okta may answer with "use_dpop_nonce" and a DPoP-Nonce header; the request is
then signed again with that nonce and retried once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Callable, Generator

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .okta import OktaError

SCOPES = ("okta.users.read", "okta.users.manage")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(n: int) -> str:
    return _b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def rsa_thumbprint(private_key) -> str:
    """RFC 7638 thumbprint of the public key, used as the key id (kid)."""
    pub = private_key.public_key().public_numbers()
    members = {"e": _int_b64(pub.e), "kty": "RSA", "n": _int_b64(pub.n)}
    return _b64url(hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode()).digest())


def _htu(url: httpx.URL) -> str:
    # Okta compares htu with the decoded path ("%40" in a login must be "@").
    return f"{url.scheme}://{url.netloc.decode()}{url.path}"


class OktaOAuth(httpx.Auth):
    requires_response_body = True

    def __init__(
        self,
        org_url: str,
        client_id: str,
        private_key_pem: bytes,
        scopes: tuple[str, ...] = SCOPES,
        now: Callable[[], float] = time.time,
    ):
        self._token_url = org_url.rstrip("/") + "/oauth2/v1/token"
        self._client_id = client_id
        self._client_key = serialization.load_pem_private_key(private_key_pem, password=None)
        self._kid = rsa_thumbprint(self._client_key)
        self._scopes = " ".join(scopes)
        self._now = now
        # DPoP key: generated per process, never written anywhere.
        self._dpop_key = ec.generate_private_key(ec.SECP256R1())
        nums = self._dpop_key.public_key().public_numbers()
        self._dpop_jwk = {"kty": "EC", "crv": "P-256", "x": _b64url(nums.x.to_bytes(32, "big")),
                          "y": _b64url(nums.y.to_bytes(32, "big"))}
        self._token: str | None = None
        self._token_type = "DPoP"
        self._expires_at = 0.0
        self._nonce: str | None = None

    @classmethod
    def from_file(cls, org_url: str, client_id: str, key_path: Path, **kw) -> "OktaOAuth":
        try:
            pem = Path(key_path).read_bytes()
        except OSError as e:
            raise OktaError(f"cannot read Okta private key at {key_path}: {e}") from e
        return cls(org_url, client_id, pem, **kw)

    # --- signing ----------------------------------------------------------

    def _client_assertion(self) -> str:
        t = int(self._now())
        claims = {"iss": self._client_id, "sub": self._client_id, "aud": self._token_url,
                  "iat": t, "exp": t + 300, "jti": str(uuid.uuid4())}
        return jwt.encode(claims, self._client_key, algorithm="RS256", headers={"kid": self._kid})

    def _dpop_proof(self, method: str, url: str, access_token: str | None = None) -> str:
        claims = {"htm": method, "htu": url, "iat": int(self._now()), "jti": str(uuid.uuid4())}
        if self._nonce:
            claims["nonce"] = self._nonce
        if access_token:
            claims["ath"] = _b64url(hashlib.sha256(access_token.encode()).digest())
        return jwt.encode(claims, self._dpop_key, algorithm="ES256",
                          headers={"typ": "dpop+jwt", "jwk": self._dpop_jwk})

    def _token_request(self) -> httpx.Request:
        return httpx.Request(
            "POST", self._token_url,
            data={"grant_type": "client_credentials", "scope": self._scopes,
                  "client_assertion_type": ASSERTION_TYPE, "client_assertion": self._client_assertion()},
            headers={"Accept": "application/json", "DPoP": self._dpop_proof("POST", self._token_url)},
        )

    def _sign(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = f"{self._token_type} {self._token}"
        if self._token_type.lower() == "dpop":
            request.headers["DPoP"] = self._dpop_proof(request.method, _htu(request.url), self._token)

    def _remember_nonce(self, response: httpx.Response) -> bool:
        nonce = response.headers.get("DPoP-Nonce")
        if nonce and nonce != self._nonce:
            self._nonce = nonce
            return True
        return False

    # --- flow -------------------------------------------------------------

    def _fetch_token(self) -> Generator[httpx.Request, httpx.Response, None]:
        response = yield self._token_request()
        if response.status_code == 400 and self._remember_nonce(response):
            response = yield self._token_request()
        if response.status_code != 200:
            try:
                err = response.json().get("error", "")
            except ValueError:
                err = ""
            raise OktaError(f"token request returned {response.status_code} {err}".strip())
        body = response.json()
        self._token = body["access_token"]
        self._token_type = body.get("token_type", "Bearer")
        self._expires_at = self._now() + int(body.get("expires_in", 3600)) - 60

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self._token is None or self._now() >= self._expires_at:
            yield from self._fetch_token()
        self._sign(request)
        response = yield request
        if response.status_code == 401 and self._remember_nonce(response):
            self._sign(request)
            response = yield request
        if response.status_code == 401:
            # Token revoked or expired early: get a new one and try once more.
            self._token = None
            yield from self._fetch_token()
            self._sign(request)
            yield request


def build_okta_client(settings) -> "OktaPushClient":
    """Push client for the server, signed in with the scoped OAuth app."""
    from .okta import OktaPushClient

    auth = OktaOAuth.from_file(settings.okta_org_url, settings.okta_client_id, settings.okta_private_key_path)
    return OktaPushClient(settings.okta_org_url, auth)
