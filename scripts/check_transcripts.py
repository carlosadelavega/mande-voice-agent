"""Deterministic checks over saved test transcripts, independent of the LLM judge.

Usage:
  python scripts/check_transcripts.py baseline after-fix

Checks:
  - silent hang-ups: the agent calls end_call without saying anything to the
    caller in that turn (no agent message since the caller last spoke, and no
    system__message_to_speak on the end_call).
  - gendered address: the agent uses a form that assumes the caller's gender
    ("la escucho", "señor", "don", "bienvenida"...). "Ya lo encontré" is
    counted separately, because "lo" can mean the record as well as the caller.
"""

from __future__ import annotations

import json
import re
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


GENDERED = re.compile(
    r"\b(l[oa] escucho|l[oa] espero|l[oa] atiendo|l[oa] comunico|señora?|señorita|don|doña|"
    r"bienvenid[oa]|segur[oa] que|list[oa] para)\b", re.IGNORECASE)
FOUND_LO = re.compile(r"\bya l[oa] encontré\b", re.IGNORECASE)


def gendered_forms(transcript: list[dict], pattern: re.Pattern = GENDERED) -> list[str]:
    """Gendered forms the agent used toward the caller, lowercased."""
    return [h.group(0).lower() for m in transcript if m.get("role") == "agent"
            for h in pattern.finditer(m.get("message") or "")]


def main() -> None:
    labels = sys.argv[1:] or ["baseline"]
    test_ids = json.loads((ROOT / "evals" / "test_ids.json").read_text())
    case_of = {v: k for k, v in test_ids.items()}
    for label in labels:
        record = json.loads((RESULTS / f"{label}.json").read_text())
        counts = defaultdict(lambda: [0, 0])  # case -> [silent, runs]
        gendered = defaultdict(set)  # form -> cases
        gendered_runs = found_lo_runs = 0
        for run in record["raw_runs"]:
            case = case_of.get(run["test_id"], run["test_id"])
            transcript = run.get("agent_responses") or []
            counts[case][1] += 1
            if silent_hangup(transcript):
                counts[case][0] += 1
            forms = gendered_forms(transcript)
            gendered_runs += bool(forms)
            found_lo_runs += bool(gendered_forms(transcript, FOUND_LO))
            for f in forms:
                gendered[f].add(case)
        total = sum(c[0] for c in counts.values())
        runs = sum(c[1] for c in counts.values())
        print(f"{label}: silent hang-ups in {total}/{runs} runs")
        for case, (silent, n) in counts.items():
            if silent:
                print(f"  {case}: {silent}/{n}")
        print(f"{label}: gendered address in {gendered_runs}/{runs} runs")
        for form, cases in sorted(gendered.items()):
            print(f"  {form!r}: {', '.join(sorted(cases))}")
        print(f"{label}: 'ya lo encontré' (ambiguous) in {found_lo_runs}/{runs} runs")


if __name__ == "__main__":
    main()
