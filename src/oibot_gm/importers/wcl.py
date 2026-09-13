"""Warcraft Logs attendance, from the JSON we dump out of
`guildData.guild.attendance` (see docs/research.md §6c)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Attendance:
    reports: list[dict]  # {code, date, zone, present: set[str]}

    @property
    def total(self) -> int:
        return len(self.reports)

    def count(self, names: list[str]) -> int:
        """Reports in which any of `names` was present (a main+alts family counts once)."""
        return sum(1 for r in self.reports if r["present"] & set(names))


def attendance(path: Path) -> Attendance:
    raw = json.loads(path.read_text())
    reports = [
        {"code": r["code"], "date": r["date"], "zone": r["zone"], "present": {p["name"] for p in r["players"] if p.get("presence", 1) == 1}}
        for r in raw
    ]
    return Attendance(reports)
