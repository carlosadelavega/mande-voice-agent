"""Create or update the agent tests defined in evals/cases.py.

Usage:
  python scripts/sync_tests.py            # all cases
  python scripts/sync_tests.py N1 G1      # only these
  python scripts/sync_tests.py --dry-run  # print rendered definitions

Test IDs are stored in evals/test_ids.json so later syncs update in place.
The tests are also attached to the agent so they show up in its dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from elevenlabs import ElevenLabs  # noqa: E402

from evals.cases import CASES  # noqa: E402

IDS_FILE = ROOT / "evals" / "test_ids.json"
AGENT_IDS = ROOT / "agent" / "ids.json"
PLACEHOLDER = re.compile(r"\$\{TOOL_ID:([a-z_]+)\}")


def render(node, tool_ids: dict):
    """Replace ${TOOL_ID:name} in strings and dict keys."""
    if isinstance(node, dict):
        return {render(k, tool_ids): render(v, tool_ids) for k, v in node.items()}
    if isinstance(node, list):
        return [render(v, tool_ids) for v in node]
    if isinstance(node, str):
        return PLACEHOLDER.sub(lambda m: tool_ids[m.group(1)], node)
    return node


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases", nargs="*", help="case IDs (default: all)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    agent_ids = json.loads(AGENT_IDS.read_text())
    selected = args.cases or list(CASES)
    unknown = [c for c in selected if c not in CASES]
    if unknown:
        sys.exit(f"Unknown cases: {unknown}")

    rendered = {cid: render(CASES[cid], agent_ids["tools"]) for cid in selected}
    if args.dry_run:
        print(json.dumps(rendered, indent=2, ensure_ascii=False))
        return

    load_dotenv(ROOT / ".env")
    client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))
    test_ids = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}

    for cid, body in rendered.items():
        if cid in test_ids:
            client.conversational_ai.tests.update(test_ids[cid], request=body)
            print(f"updated {cid}")
        else:
            test_ids[cid] = client.conversational_ai.tests.create(request=body).id
            print(f"created {cid}")
        IDS_FILE.write_text(json.dumps(test_ids, indent=2) + "\n")

    client.conversational_ai.agents.update(
        agent_ids["agent_id"],
        platform_settings={"testing": {"attached_tests": [{"test_id": t} for t in test_ids.values()]}},
    )
    print(f"attached {len(test_ids)} tests to the agent")


if __name__ == "__main__":
    main()
