"""Generate the synthetic employee directory (data/directory.json).

Every name, employee number and phone number here is invented. Phone numbers
use the 0000 exchange, which is not a valid Mexican mobile range.

The people are hand-picked to exercise the test cases:
  - compound first names and both surnames with accents (María José Hernández Villaseñor)
  - two people who share first name and first surname (Juan Carlos Hernández ...)
  - two people with the exact same full name (José Luis García López)
  - a compound surname (Díaz de León)
  - Nahuatl-origin names that speech-to-text tends to mangle
  - an English-speaking employee with a single surname
  - a disabled (terminated) account that must never be unlocked

TOTP secrets are NOT stored here. They are derived at runtime from
TOTP_MASTER_KEY in .env (see app/verifier.py), so this file is safe to commit.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20261112
OUT = Path(__file__).resolve().parent.parent / "data" / "directory.json"

# (first_name, first_surname, second_surname, status)
PEOPLE = [
    ("María José", "Hernández", "Villaseñor", "locked"),
    ("Juan Carlos", "Hernández", "Ruiz", "locked"),
    ("Juan Carlos", "Hernández", "Ortega", "active"),
    ("José Luis", "García", "López", "locked"),
    ("José Luis", "García", "López", "active"),
    ("Guadalupe", "Ramírez", "Núñez", "locked"),
    ("Ana Sofía", "Treviño", "Garza", "active"),
    ("Luis Fernando", "Peña", "Castañeda", "locked"),
    ("Ximena", "Ibarra", "Zúñiga", "locked"),
    ("Jesús Alberto", "Ochoa", "Muñoz", "locked"),
    ("Daniela", "Velázquez", "Robles", "active"),
    ("Rodrigo", "Saldaña", "Echeverría", "locked"),
    ("Itzel", "Moctezuma", "Quiñones", "locked"),
    ("Xóchitl", "Cuauhtémoc", "Ayala", "locked"),
    ("Emily", "Johnson", None, "locked"),
    ("Roberto", "Díaz de León", "Martínez", "locked"),
    ("Paola", "Ávila", "Sánchez", "disabled"),
    ("Miguel Ángel", "Cervantes", "Uribe", "active"),
]

# Letters that sound alike in Spanish (B/V, S/C/Z, M/N, I/Y, G/J) are kept on
# purpose, so spelling an employee number aloud is a real test.
LETTERS = "ABCDEFGHJKLMNPRSTUVXYZ"


def main() -> None:
    rng = random.Random(SEED)
    seen: set[str] = set()
    employees = []
    for i, (first, sur1, sur2, status) in enumerate(PEOPLE, start=1):
        while True:
            emp_no = "".join(rng.choices(LETTERS, k=2)) + "".join(rng.choices("0123456789", k=4))
            if emp_no not in seen:
                seen.add(emp_no)
                break
        phone = f"+52 33 0000 {rng.randint(0, 9999):04d}"
        employees.append(
            {
                "user_id": f"u{i:03d}",
                "employee_number": emp_no,
                "first_name": first,
                "first_surname": sur1,
                "second_surname": sur2,
                "phone": phone,
                "account_status": status,
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"employees": employees}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(employees)} synthetic employees to {OUT}")


if __name__ == "__main__":
    main()
