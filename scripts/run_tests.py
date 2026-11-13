"""Run the agent tests with repeat_count and record pass rates.

Usage:
  python scripts/run_tests.py --label baseline --repeat 3
  python scripts/run_tests.py --label smoke --repeat 1 O1     # only some cases

Writes evals/results/<label>.json (summary plus raw runs) and
evals/results/<label>.md (pass-rate table), and prints the table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from elevenlabs import ElevenLabs  # noqa: E402

from evals.cases import CASES  # noqa: E402

RESULTS = ROOT / "evals" / "results"
FINAL = {"passed", "failed", "cancelled"}


def wait_for(client: ElevenLabs, invocation_id: str, poll: float = 10.0, timeout: float = 1800.0):
    start = time.time()
    while True:
        inv = client.conversational_ai.tests.invocations.get(invocation_id)
        statuses = [r.status for r in inv.test_runs]
        done = sum(s in FINAL for s in statuses)
        bucketing_done = inv.bucketing_status in (None, "completed", "failed")
        print(f"  {done}/{len(statuses)} runs finished, bucketing: {inv.bucketing_status}", flush=True)
        if done == len(statuses) and bucketing_done:
            return inv
        if time.time() - start > timeout:
            sys.exit(f"Timed out waiting for invocation {invocation_id}")
        time.sleep(poll)


def rationale(run) -> str:
    cr = run.condition_result
    if cr is None or cr.rationale is None:
        return ""
    r = cr.rationale
    failed = [m for m in (getattr(r, "messages", None) or []) if "failed" in m.lower()]
    return " | ".join(failed) if failed else (getattr(r, "summary", None) or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases", nargs="*", help="case IDs (default: all)")
    parser.add_argument("--label", required=True, help="name of this run, e.g. baseline or after-fix-1")
    parser.add_argument("--repeat", type=int, default=3, help="repeat_count per test (default 3)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))
    agent_id = json.loads((ROOT / "agent" / "ids.json").read_text())["agent_id"]
    test_ids = json.loads((ROOT / "evals" / "test_ids.json").read_text())
    selected = args.cases or [c for c in CASES if c in test_ids]
    missing = [c for c in selected if c not in test_ids]
    if missing:
        sys.exit(f"Not synced yet (run scripts/sync_tests.py): {missing}")
    by_test_id = {test_ids[c]: c for c in selected}

    credits_before = client.user.subscription.get().character_count
    print(f"Running {len(selected)} tests x {args.repeat} ...")
    inv = client.conversational_ai.agents.run_tests(
        agent_id,
        tests=[{"test_id": test_ids[c]} for c in selected],
        repeat_count=args.repeat,
        name=args.label,
    )
    inv = wait_for(client, inv.id)
    credits_after = client.user.subscription.get().character_count

    runs = defaultdict(list)
    for r in inv.test_runs:
        runs[by_test_id.get(r.test_id, r.test_id)].append(r)

    summary = []
    for cid in selected:
        rs = runs.get(cid, [])
        passed = sum(r.status == "passed" for r in rs)
        reasons = sorted({rationale(r) for r in rs if r.status == "failed" and rationale(r)})
        summary.append({
            "case": cid,
            "name": CASES[cid]["name"],
            "type": CASES[cid]["type"],
            "passed": passed,
            "runs": len(rs),
            "credits_used": sum(r.credits_used or 0 for r in rs),
            "failure_reasons": reasons,
        })

    buckets = []
    for g in inv.result_groups or []:
        for b in g.buckets or []:
            buckets.append({"case": by_test_id.get(g.test_id, g.test_id), "status": b.status,
                            "title": b.title, "reason": b.reason, "runs": len(b.test_run_ids or [])})

    total_pass = sum(s["passed"] for s in summary)
    total_runs = sum(s["runs"] for s in summary)
    record = {
        "label": args.label,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "invocation_id": inv.id,
        "agent_id": agent_id,
        "agent_version_id": inv.version_id,
        "repeat_count": args.repeat,
        "passed": total_pass,
        "runs": total_runs,
        "credits_used_reported": sum(s["credits_used"] for s in summary),
        # The account counter lags behind the test runs, so this undercounts.
        "account_credits_delta_at_finish": credits_after - credits_before,
        "summary": summary,
        "failure_buckets": buckets,
        "raw_runs": [r.model_dump(mode="json", exclude_none=True) for r in inv.test_runs],
    }

    lines = [
        f"# Results: {args.label}",
        "",
        f"Run {record['ran_at']}, repeat_count {args.repeat}, invocation `{inv.id}`.",
        "",
        "| ID | Test | Type | Passed |",
        "|----|------|------|--------|",
    ]
    for s in summary:
        lines.append(f"| {s['case']} | {s['name']} | {s['type']} | {s['passed']}/{s['runs']} |")
    lines += ["", f"**Total: {total_pass}/{total_runs}**", ""]
    failed = [s for s in summary if s["failure_reasons"]]
    if failed:
        lines += ["## Failure reasons", ""]
        for s in failed:
            for reason in s["failure_reasons"]:
                lines.append(f"- **{s['case']}**: {reason}")
        lines.append("")
    lines.append(f"Credits: {record['credits_used_reported']} reported by the test runs.")

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{args.label}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    (RESULTS / f"{args.label}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
