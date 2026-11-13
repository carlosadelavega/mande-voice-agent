"""Create or update the ElevenLabs agent, its three webhook tools and the
workspace secret they send, from the configs in agent/.

Usage:
  python scripts/push_agent.py --base-url https://<your-tunnel>.trycloudflare.com
  python scripts/push_agent.py --base-url ... --dry-run   # print rendered configs only

Configs use ${NAME} placeholders, filled in here:
  TOOL_BASE_URL   public URL of the FastAPI service (changes with each quick tunnel)
  TOOL_SECRET_ID  ElevenLabs workspace secret holding TOOL_SHARED_SECRET from .env
  PROMPT          agent/prompt.md
  TOOL_IDS        IDs of the three tools after they are created

IDs of created resources are stored in agent/ids.json so later pushes update
them in place instead of creating duplicates. They are not secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from elevenlabs import ElevenLabs, ToolRequestModel

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "agent"
IDS_FILE = AGENT_DIR / "ids.json"
TOOL_NAMES = ["lookup_user", "verify_identity", "unlock_account"]
SECRET_NAME = "helpdesk_tool_secret"


def render(node, values: dict):
    """Replace ${NAME} placeholders. A string that is exactly one placeholder
    takes the value's type (so TOOL_IDS becomes a list)."""
    if isinstance(node, dict):
        return {k: render(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [render(v, values) for v in node]
    if isinstance(node, str):
        for name, value in values.items():
            token = "${" + name + "}"
            if node == token:
                return value
            if token in node:
                node = node.replace(token, str(value))
    return node


def load_ids() -> dict:
    return json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}


def save_ids(ids: dict) -> None:
    IDS_FILE.write_text(json.dumps(ids, indent=2) + "\n")


def ensure_secret(client: ElevenLabs, ids: dict) -> str:
    value = os.environ.get("TOOL_SHARED_SECRET")
    if not value:
        sys.exit("TOOL_SHARED_SECRET is missing from .env")
    existing = {s.name: s.secret_id for s in client.conversational_ai.secrets.list().secrets}
    if SECRET_NAME in existing:
        secret_id = existing[SECRET_NAME]
        client.conversational_ai.secrets.update(secret_id, name=SECRET_NAME, value=value)
        print(f"updated workspace secret {SECRET_NAME}")
    else:
        secret_id = client.conversational_ai.secrets.create(name=SECRET_NAME, value=value).secret_id
        print(f"created workspace secret {SECRET_NAME}")
    ids["secret_id"] = secret_id
    return secret_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="public HTTPS URL of the tool service")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    ids = load_ids()
    base_url = args.base_url.rstrip("/")
    if not base_url.startswith("https://"):
        sys.exit("--base-url must be https")

    tool_configs = {n: json.loads((AGENT_DIR / "tools" / f"{n}.json").read_text()) for n in TOOL_NAMES}
    agent_config = json.loads((AGENT_DIR / "agent.json").read_text())
    prompt = (AGENT_DIR / "prompt.md").read_text()

    if args.dry_run:
        values = {"TOOL_BASE_URL": base_url, "TOOL_SECRET_ID": ids.get("secret_id", "<secret_id>"),
                  "PROMPT": "<agent/prompt.md>", "TOOL_IDS": ["<tool ids>"]}
        for n, cfg in tool_configs.items():
            print(f"--- tool {n}\n{json.dumps(render(cfg, values), indent=2, ensure_ascii=False)}")
        print(f"--- agent\n{json.dumps(render(agent_config, values), indent=2, ensure_ascii=False)}")
        return

    # The SDK's default reads ELEVENLABS_API_KEY at import time, before .env is loaded.
    client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))
    secret_id = ensure_secret(client, ids)
    save_ids(ids)

    tool_ids = ids.setdefault("tools", {})
    for name, cfg in tool_configs.items():
        rendered = render(cfg, {"TOOL_BASE_URL": base_url, "TOOL_SECRET_ID": secret_id})
        request = ToolRequestModel.model_validate({"tool_config": rendered})
        if name in tool_ids:
            client.conversational_ai.tools.update(tool_ids[name], request=request)
            print(f"updated tool {name}")
        else:
            tool_ids[name] = client.conversational_ai.tools.create(request=request).id
            print(f"created tool {name}")
        save_ids(ids)

    rendered_agent = render(agent_config, {"PROMPT": prompt, "TOOL_IDS": [tool_ids[n] for n in TOOL_NAMES]})
    if "agent_id" in ids:
        client.conversational_ai.agents.update(
            ids["agent_id"], name=rendered_agent["name"], conversation_config=rendered_agent["conversation_config"]
        )
        print("updated agent")
    else:
        ids["agent_id"] = client.conversational_ai.agents.create(
            name=rendered_agent["name"], conversation_config=rendered_agent["conversation_config"]
        ).agent_id
        print("created agent")
    save_ids(ids)

    agent_id = ids["agent_id"]
    print(f"\nTalk to it:  https://elevenlabs.io/app/talk-to?agent_id={agent_id}")
    print(f"Edit it:     https://elevenlabs.io/app/agents/agents/{agent_id}")


if __name__ == "__main__":
    main()
