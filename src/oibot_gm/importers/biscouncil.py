"""BisCouncil "Loot Summary" export (CSV with a summary table followed by a
`#DETAILED_LOOT_START` section). Only the detailed rows become ledger entries;
the summary is kept for cross-checking. `parse_text` reads the file's text (the
web's paste / upload and MCP's import_ledger reach it through
importers/ledger_csv.py); `parse` reads a path."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from ..models import LootAward

DETAIL_MARK = "#DETAILED_LOOT_START"


def split_text(text: str) -> tuple[list[str], list[str]]:
    """(summary lines, detail lines) of a BisCouncil export: the detail table follows the marker line."""
    lines = text.lstrip("﻿").splitlines()
    marks = [i for i, ln in enumerate(lines) if ln.strip() == DETAIL_MARK]
    if marks:
        return lines[:marks[0]], lines[marks[0] + 1:]
    return lines, []


def row_award(row: dict) -> LootAward:
    """One detail row as an award (KeyError / ValueError on a row that lacks a field or carries a bad value)."""
    return LootAward(
        raider=row["raider_name"],
        item_id=int(row["item_id"]),
        tier=row["item_tier"],
        total_weight=float(row["total_weight"]),
        offspec=(row.get("offspec_flag") or "0") == "1",
        received=date.fromisoformat(row["date_received"]),
        instance=row["instance_name"],
        boss=row["boss_name"],
    )


def parse_text(text: str) -> tuple[list[dict], list[LootAward]]:
    summary_lines, detail_lines = split_text(text)
    summary = list(csv.DictReader(summary_lines))
    awards = [row_award(row) for row in csv.DictReader(detail_lines)]
    return summary, awards


def parse(path: Path) -> tuple[list[dict], list[LootAward]]:
    return parse_text(path.read_text())
