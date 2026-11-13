"""Deterministic checks over saved test transcripts, independent of the LLM judge.

Usage:
  python scripts/check_transcripts.py baseline after-fix

Currently checks for silent hang-ups: the agent calls end_call without saying
anything to the caller in that turn (no agent message since the caller last
spoke, and no system__message_to_speak on the end_call).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"


def silent_hangup(transcript: list[dict]) -> bool:
    spoke_since_user = False
    for m in transcript:
        if m.get("role") == "user":
            spoke_since_user = False
            continue
        if m.get("message"):
            spoke_since_user = True
        for call in m.get("tool_calls") or []:
            if call.get("tool_name") != "end_call":
                continue
            params = json.loads(call.get("params_as_json") or "{}")
            if not spoke_since_user and not params.get("system__message_to_speak"):
                return True
    return False


def main() -> None:
    labels = sys.argv[1:] or ["baseline"]
    test_ids = json.loads((ROOT / "evals" / "test_ids.json").read_text())
    case_of = {v: k for k, v in test_ids.items()}
    for label in labels:
        record = json.loads((RESULTS / f"{label}.json").read_text())
        counts = defaultdict(lambda: [0, 0])  # case -> [silent, runs]
        for run in record["raw_runs"]:
            case = case_of.get(run["test_id"], run["test_id"])
            counts[case][1] += 1
            if silent_hangup(run.get("agent_responses") or []):
                counts[case][0] += 1
        total = sum(c[0] for c in counts.values())
        runs = sum(c[1] for c in counts.values())
        print(f"{label}: silent hang-ups in {total}/{runs} runs")
        for case, (silent, n) in counts.items():
            if silent:
                print(f"  {case}: {silent}/{n}")


if __name__ == "__main__":
    main()
