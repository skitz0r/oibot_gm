"""BisCouncil "Loot Summary" export (CSV with a summary table followed by a
`#DETAILED_LOOT_START` section). Only the detailed rows become ledger entries;
the summary is kept for cross-checking."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from ..models import LootAward


def parse(path: Path) -> tuple[list[dict], list[LootAward]]:
    summary: list[dict] = []
    awards: list[LootAward] = []
    lines = path.read_text().splitlines()
    if "#DETAILED_LOOT_START" in lines:
        split = lines.index("#DETAILED_LOOT_START")
        summary_lines, detail_lines = lines[:split], lines[split + 1 :]
    else:
        summary_lines, detail_lines = lines, []
    summary = list(csv.DictReader(summary_lines))
    for row in csv.DictReader(detail_lines):
        awards.append(
            LootAward(
                raider=row["raider_name"],
                item_id=int(row["item_id"]),
                tier=row["item_tier"],
                total_weight=float(row["total_weight"]),
                offspec=row.get("offspec_flag", "0") == "1",
                received=date.fromisoformat(row["date_received"]),
                instance=row["instance_name"],
                boss=row["boss_name"],
            )
        )
    return summary, awards
