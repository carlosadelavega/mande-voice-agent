"""Write a before/after pass-rate table from two result files.

Usage:
  python scripts/compare_results.py baseline after-fix
Writes evals/results/<before>-vs-<after>.md and prints it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.check_transcripts import silent_hangup  # noqa: E402

RESULTS = ROOT / "evals" / "results"


def load(label: str) -> dict:
    return json.loads((RESULTS / f"{label}.json").read_text())


def silent_count(record: dict) -> tuple[int, int]:
    runs = record["raw_runs"]
    return sum(silent_hangup(r.get("agent_responses") or []) for r in runs), len(runs)


def main() -> None:
    before_label, after_label = sys.argv[1], sys.argv[2]
    before, after = load(before_label), load(after_label)
    b = {s["case"]: s for s in before["summary"]}
    a = {s["case"]: s for s in after["summary"]}

    lines = [
        f"# {before_label} vs {after_label}",
        "",
        f"Same 15 tests, same scenarios, same models, repeat_count {before['repeat_count']} "
        f"and {after['repeat_count']}.",
        "",
        f"| ID | Test | {before_label} | {after_label} |",
        "|----|------|----------|----------|",
    ]
    for case in b:
        mark = lambda s: f"{s['passed']}/{s['runs']}"  # noqa: E731
        row_a = mark(a[case]) if case in a else "not run"
        lines.append(f"| {case} | {b[case]['name'][len(case) + 1:]} | {mark(b[case])} | {row_a} |")
    lines.append(f"| | **Total** | **{before['passed']}/{before['runs']}** | **{after['passed']}/{after['runs']}** |")
    sb, nb = silent_count(before)
    sa, na = silent_count(after)
    lines += [
        "",
        "Checked from transcripts, not by the LLM judge (`scripts/check_transcripts.py`):",
        "",
        f"| Check | {before_label} | {after_label} |",
        "|-------|----------|----------|",
        f"| Silent hang-ups (end_call without a word to the caller) | {sb}/{nb} runs | {sa}/{na} runs |",
    ]
    out = RESULTS / f"{before_label}-vs-{after_label}.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
