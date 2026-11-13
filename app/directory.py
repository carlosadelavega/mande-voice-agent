"""Mock employee directory backed by data/directory.json, held in memory."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def normalize_name(value: str | None) -> str:
    """Case-, accent- and spacing-insensitive form of a name."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", " ", no_marks.casefold())
    return " ".join(cleaned.split())


def normalize_employee_number(value: str | None) -> str:
    """Uppercase, alphanumerics only ("ab-12 34" -> "AB1234")."""
    if not value:
        return ""
    return re.sub(r"[^0-9A-Za-z]", "", value).upper()


@dataclass
class Employee:
    user_id: str
    employee_number: str
    first_name: str
    first_surname: str
    second_surname: str | None
    phone: str
    account_status: str  # "active" | "locked" | "disabled"

    @property
    def masked_phone(self) -> str:
        digits = re.sub(r"\D", "", self.phone)
        return f"******{digits[-2:]}"


class Directory:
    def __init__(self, employees: list[Employee]):
        self._by_id = {e.user_id: e for e in employees}

    @classmethod
    def load(cls, path: Path) -> "Directory":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([Employee(**e) for e in raw["employees"]])

    def get(self, user_id: str) -> Employee | None:
        return self._by_id.get(user_id)

    def find_by_employee_number(self, employee_number: str) -> Employee | None:
        wanted = normalize_employee_number(employee_number)
        if not wanted:
            return None
        for e in self._by_id.values():
            if e.employee_number == wanted:
                return e
        return None

    def find_by_name(self, first_name: str, first_surname: str, second_surname: str | None) -> list[Employee]:
        """All employees matching the given name. second_surname is optional;
        when it is omitted, everyone sharing first name and first surname matches."""
        fn, s1, s2 = normalize_name(first_name), normalize_name(first_surname), normalize_name(second_surname)
        if not fn or not s1:
            return []
        matches = []
        for e in self._by_id.values():
            if normalize_name(e.first_name) != fn or normalize_name(e.first_surname) != s1:
                continue
            if s2 and normalize_name(e.second_surname) != s2:
                continue
            matches.append(e)
        return matches
