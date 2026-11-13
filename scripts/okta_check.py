"""Check the Okta setup for push employees, without going through the agent.

Usage:
  python scripts/okta_check.py              # find each push employee's Okta user and push factor
  python scripts/okta_check.py --push u019  # also send a real push and wait for the result

Reads OKTA_ORG_URL and OKTA_CLIENT_ID from .env and the private key file. Never prints secrets.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.directory import Directory  # noqa: E402
from app.okta import OktaError  # noqa: E402
from app.okta_auth import build_okta_client  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--push", metavar="USER_ID", help="send a real push to this employee and poll it")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not (settings.okta_org_url and settings.okta_client_id):
        sys.exit("Set OKTA_ORG_URL and OKTA_CLIENT_ID in .env first.")
    client = build_okta_client(settings)
    directory = Directory.load(settings.directory_path)
    push_people = [e for e in directory._by_id.values() if e.factor == "okta_push"]

    for emp in push_people:
        try:
            user_id, factor_id = client._push_factor(emp.okta_login)
            print(f"OK   {emp.user_id} {emp.okta_login}: Okta user {user_id}, push factor {factor_id}")
        except OktaError as e:
            print(f"FAIL {emp.user_id} {emp.okta_login}: {e}")

    if args.push:
        emp = directory.get(args.push)
        if emp is None or emp.factor != "okta_push":
            sys.exit(f"{args.push} is not a push employee")
        ch = client.start_push(emp.okta_login)
        print(f"Push sent. Tap {ch.number} in Okta Verify.")
        deadline = time.time() + 120
        while time.time() < deadline:
            status = client.poll(ch.poll_url)
            if status.result != "WAITING":
                print(f"Result: {status.result}")
                return
            time.sleep(3)
        print("Still waiting after 2 minutes.")


if __name__ == "__main__":
    main()
