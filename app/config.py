from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTORY = ROOT / "data" / "directory.json"


@dataclass(frozen=True)
class Settings:
    totp_master_key: str
    tool_shared_secret: str
    directory_path: Path = DEFAULT_DIRECTORY
    max_attempts: int = 3
    lockout_seconds: int = 15 * 60
    token_ttl_seconds: int = 5 * 60
    totp_valid_window: int = 1  # accept the previous/next 30s step for speech latency
    okta_org_url: str | None = None  # e.g. https://integrator-1234567.okta.com
    okta_api_token: str | None = None  # admin API token; server-side only

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        missing = [k for k in ("TOTP_MASTER_KEY", "TOOL_SHARED_SECRET") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")
        return cls(
            totp_master_key=os.environ["TOTP_MASTER_KEY"],
            tool_shared_secret=os.environ["TOOL_SHARED_SECRET"],
            okta_org_url=os.environ.get("OKTA_ORG_URL") or None,
            okta_api_token=os.environ.get("OKTA_API_TOKEN") or None,
        )
