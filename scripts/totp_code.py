"""Print the current TOTP code for a synthetic employee.

Usage:
  python scripts/totp_code.py AB1234          # by employee number
  python scripts/totp_code.py u001            # by user_id
  python scripts/totp_code.py u001 --uri      # also print an otpauth:// URI
                                              # to enroll in an authenticator app
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.directory import Directory  # noqa: E402
from app.verifier import TotpVerifier  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("who", help="employee number or user_id")
    parser.add_argument("--uri", action="store_true", help="print an otpauth:// provisioning URI")
    args = parser.parse_args()

    settings = Settings.from_env()
    directory = Directory.load(settings.directory_path)
    emp = directory.get(args.who) or directory.find_by_employee_number(args.who)
    if emp is None:
        sys.exit(f"No synthetic employee matches {args.who!r}")

    totp = TotpVerifier(settings.totp_master_key, time.time).totp_for(emp)
    remaining = totp.interval - int(time.time()) % totp.interval
    name = f"{emp.first_name} {emp.first_surname} {emp.second_surname or ''}".strip()
    print(f"{name} ({emp.employee_number}, {emp.user_id}, {emp.account_status})")
    print(f"code: {totp.now()}  (valid for {remaining}s more)")
    if args.uri:
        print(totp.provisioning_uri(name=emp.employee_number, issuer_name="Helpdesk demo"))


if __name__ == "__main__":
    main()
