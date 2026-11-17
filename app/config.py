from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTORY = ROOT / "data" / "directory.json"
# Written by scripts/push_agent.py when it creates the post-call webhook.
WEBHOOK_SECRET_FILE = ROOT / ".secrets" / "elevenlabs_webhook_secret"


def _read_secret(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


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
    okta_client_id: str | None = None  # Okta API Services app (scoped OAuth)
    okta_private_key_path: Path = ROOT / ".secrets" / "okta_service_app.pem"
    splunk_hec_url: str | None = None  # e.g. https://localhost:8088
    splunk_hec_token: str | None = None  # write-only HEC token, limited to one index
    splunk_ca_path: Path = ROOT / ".secrets" / "splunk" / "ca.pem"
    events_spool_path: Path = ROOT / "logs" / "events.jsonl"  # used when HEC is unset or down
    elevenlabs_webhook_secret: str | None = None  # HMAC secret of the post-call webhook
    elevenlabs_webhook_secret_file: Path | None = None  # read on each delivery, so a new webhook needs no restart

    def webhook_secret(self) -> str | None:
        if self.elevenlabs_webhook_secret:
            return self.elevenlabs_webhook_secret
        return _read_secret(self.elevenlabs_webhook_secret_file) if self.elevenlabs_webhook_secret_file else None

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
            okta_client_id=os.environ.get("OKTA_CLIENT_ID") or None,
            okta_private_key_path=Path(os.environ.get("OKTA_PRIVATE_KEY_PATH") or ROOT / ".secrets" / "okta_service_app.pem"),
            splunk_hec_url=os.environ.get("SPLUNK_HEC_URL") or None,
            splunk_hec_token=os.environ.get("SPLUNK_HEC_TOKEN") or None,
            splunk_ca_path=Path(os.environ.get("SPLUNK_CA_PATH") or ROOT / ".secrets" / "splunk" / "ca.pem"),
            elevenlabs_webhook_secret=os.environ.get("ELEVENLABS_WEBHOOK_SECRET") or None,
            elevenlabs_webhook_secret_file=WEBHOOK_SECRET_FILE,
        )
