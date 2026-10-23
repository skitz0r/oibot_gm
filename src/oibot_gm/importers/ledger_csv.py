"""Loot history imports (design §3.1 "Data on-ramp" L07, dual interfaces phase 4): a CSV someone exported from another
tool → rows Registry.import_awards appends to the guild's ledger (deduped, never editing a row). Three formats:

- **biscouncil** — the BisCouncil "Loot Summary" export (importers/biscouncil.py reads its `#DETAILED_LOOT_START`
  table); its own tier and weight per row are kept (the guild's earlier council already tiered them).
- **tmb** — a ThatsMyBIS character export: the rows with `type` = received (or a `received_at` and no type) become
  awards — character_name, item_name / item_id, received_at, is_offspec, note; wishlist and prio rows are skipped
  (counted). The same export feeds the wishlist import (Registry.import_tmb_wishlists).
- **generic** — the documented fallback any spreadsheet can produce, one award per row:

      character,item,received,boss,ms_os,note

  `character` (required), `item` (required: an item id — digits — or the item's name), `received` (required: the day,
  YYYY-MM-DD; a time after it counts only with its zone — `Z`, `UTC` or `+01:00` — see `stamp`), `boss`, `ms_os` (ms |
  os; empty = ms), `note` — the last three optional.
  Header names are matched case-insensitively (aliases: raider / name, item_id / item_name, date / received_at,
  offspec / kind).

Gargul and RCLootCouncil exports are NOT read: which addon the council runs is still open (owner decision D13), so their
formats wait for that answer — export to the generic columns meanwhile. `kind="auto"` tells the three apart by their
headers. Code only reads the file: nothing here writes, decides a weight or judges an award."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field

from . import biscouncil

KINDS = ("biscouncil", "tmb", "generic")
KIND_WORDS = {"biscouncil": "BisCouncil loot summary", "tmb": "ThatsMyBIS export", "generic": "generic CSV (character,item,received,boss,ms_os,note)"}
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
NAME_MAX = 40
ITEM_MAX = 100
NOTE_MAX = 200
GENERIC_HEADER = "character,item,received,boss,ms_os,note"
TRUE_WORDS = {"1", "true", "yes", "y", "os", "offspec", "off", "off-spec", "off spec"}
ALIASES = {
    "character": ("character", "character_name", "raider", "raider_name", "name", "player"),
    "item": ("item", "item_id", "item_name", "itemid"),
    "received": ("received", "received_at", "date", "date_received", "awarded"),
    "boss": ("boss", "boss_name", "encounter"),
    "ms_os": ("ms_os", "msos", "offspec", "kind", "spec", "is_offspec"),
    "note": ("note", "notes", "comment"),
}


class ImportRow(BaseModel):
    """One award as a file states it (before anyone decides anything): `line` = its line in the file."""

    line: int
    raider: str
    item_id: int = 0
    item_name: str | None = None
    received: date  # the day as the file writes it
    received_at: datetime | None = None  # aware: the file stated a time WITH its zone (Registry.import_awards moves it to guild time)
    boss: str = ""
    instance: str = ""
    offspec: bool = False
    note: str | None = None
    tier: str | None = None  # the file's own tier (BisCouncil); None = the bot weighs it
    total_weight: float | None = None


class Parsed(BaseModel):
    kind: str
    rows: list[ImportRow] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)  # 'line 7: no received date' — skipped rows, in words
    skipped: int = 0  # rows that are not awards (a TMB wishlist / prio row)


def clean(value, cap: int) -> str:
    """A cell as text: whitespace collapsed, capped. Member-made text is data — it is shown, never read as an order."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:cap]


def day(value) -> date | None:
    """'2026-12-09' or '2026-12-09 20:15:00' / '2026-12-09T20:15' → the day; anything else None (never guessed)."""
    return stamp(value)[0]


STAMP = re.compile(r"\s*(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})?)?\s*$", re.I)


def stamp(value) -> tuple[date | None, datetime | None]:
    """A received cell → (the day as written, the moment when the file states a time WITH its zone, else None).
    '2026-12-10T04:15:00Z' / '2026-12-10 04:15 UTC' / '…+01:00' carry a zone: the import moves that moment to the guild's
    day (a UTC export of a late-evening award is the evening before). A time without a zone (TMB's and most exports)
    says nothing reliable about the zone, so only its day is read and the dedupe allows a day either side. Anything
    that isn't a YYYY-MM-DD day → (None, None): a date is never guessed."""
    text = str(value or "")
    m = STAMP.match(text)
    if not m:
        d = re.match(r"\s*(\d{4}-\d{2}-\d{2})", text)  # a day with something unreadable after it: the day alone
        try:
            return (date.fromisoformat(d.group(1)) if d else None), None
        except ValueError:
            return None, None
    try:
        d = date.fromisoformat(m.group(1))
    except ValueError:
        return None, None
    hh, mm, ss, zone = m.group(2), m.group(3), m.group(4), m.group(5)
    if hh is None or not zone:
        return d, None
    z = zone.upper()
    if z in ("Z", "UTC", "GMT"):
        tz = timezone.utc
    else:
        sign = -1 if z[0] == "-" else 1
        digits = z[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return d, datetime(d.year, d.month, d.day, int(hh), int(mm), int(ss or 0), tzinfo=tz)
    except ValueError:
        return d, None


def _header(text: str) -> list[str]:
    for ln in text.lstrip("﻿").splitlines():
        if ln.strip():
            return [h.strip().lower() for h in next(csv.reader([ln]))]
    return []


def detect(text: str) -> str:
    """Which format a file is, from its headers: the BisCouncil marker / raider_name; TMB's character_name +
    received_at; else generic."""
    if biscouncil.DETAIL_MARK in text:
        return "biscouncil"
    head = set(_header(text))
    if {"raider_name", "date_received"} <= head:
        return "biscouncil"
    if "character_name" in head and ("received_at" in head or "type" in head):
        return "tmb"
    return "generic"


def parse(text: str, kind: str = "auto") -> Parsed:
    """The file's award rows. A row that can't be read is listed in `errors` (with its line) and skipped — the rest
    still import; a file in no format at all raises ValueError with what is missing."""
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise ValueError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB — split it.")
    k = (kind or "auto").strip().lower()
    if k == "auto":
        k = detect(text)
    if k not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)} (or auto) — Gargul and RCLootCouncil exports aren't read yet (which addon is still open); use the generic columns: {GENERIC_HEADER}")
    out = {"biscouncil": _biscouncil, "tmb": _tmb, "generic": _generic}[k](text)
    if len(out.rows) > MAX_ROWS:
        raise ValueError(f"That file has {len(out.rows)} awards — import at most {MAX_ROWS} at a time.")
    return out


def _biscouncil(text: str) -> Parsed:
    out = Parsed(kind="biscouncil")
    summary, detail = biscouncil.split_text(text)
    lines = detail if detail else summary
    first = (len(summary) + 2) if detail else 1  # the file line of the detail table's header
    reader = csv.DictReader(lines)
    if not {"raider_name", "item_id", "date_received"} <= set(reader.fieldnames or []):
        raise ValueError("Not a BisCouncil loot summary: no #DETAILED_LOOT_START table with raider_name, item_id, date_received.")
    for i, row in enumerate(reader, first + 1):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # a blank line at the end
        try:
            a = biscouncil.row_award(row)
        except (KeyError, ValueError, TypeError) as e:
            out.errors.append(f"line {i}: can't read it ({clean(e, 80)})")
            continue
        if not clean(a.raider, NAME_MAX):
            out.errors.append(f"line {i}: no character")
            continue
        out.rows.append(ImportRow(line=i, raider=clean(a.raider, NAME_MAX), item_id=a.item_id, received=a.received, boss=clean(a.boss, ITEM_MAX),
                                  instance=clean(a.instance, ITEM_MAX), offspec=a.offspec, tier=clean(a.tier, 4) or None, total_weight=a.total_weight))
    return out


def _rows(text: str) -> tuple[list[str], list[tuple[int, dict]]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = [str(f or "").strip().lower() for f in (reader.fieldnames or [])]
    rows = []
    for n, raw in enumerate(reader, 2):
        rows.append((n, {str(k or "").strip().lower(): (v if isinstance(v, str) else "") for k, v in raw.items()}))
    return fields, rows


def _tmb(text: str) -> Parsed:
    out = Parsed(kind="tmb")
    fields, rows = _rows(text)
    if "character_name" not in fields or not ({"item_name", "item_id"} & set(fields)):
        raise ValueError("Not a ThatsMyBIS export: no character_name and item_name / item_id columns.")
    for n, r in rows:
        kind = clean(r.get("type"), 20).lower()
        if kind not in ("", "received", "loot"):
            out.skipped += 1  # a wishlist or prio row: the wishlist import reads those
            continue
        if not kind and not clean(r.get("received_at"), 40):
            out.skipped += 1
            continue
        _row(out, n, r.get("character_name"), r.get("item_id"), r.get("item_name"), r.get("received_at"), "", r.get("is_offspec"), r.get("note"))
    return out


def _generic(text: str) -> Parsed:
    out = Parsed(kind="generic")
    fields, rows = _rows(text)
    col = {want: next((a for a in names if a in fields), None) for want, names in ALIASES.items()}
    missing = [w for w in ("character", "item", "received") if col[w] is None]
    if missing:
        raise ValueError(f"No {', '.join(missing)} column — the generic file's header is: {GENERIC_HEADER}")
    both = col["item"] == "item_id" and "item_name" in fields  # item_id + item_name columns: the id when given, else the name
    for n, r in rows:
        item = r.get(col["item"]) or ""
        if both:
            iid, nm = item, r.get("item_name")
        elif clean(item, 20).isdigit():
            iid, nm = item, None  # digits: an item id
        else:
            iid, nm = "", item  # anything else: the item's name
        _row(out, n, r.get(col["character"]), iid, nm, r.get(col["received"]), r.get(col["boss"]) if col["boss"] else "",
             r.get(col["ms_os"]) if col["ms_os"] else "", r.get(col["note"]) if col["note"] else "")
    return out


def _row(out: Parsed, n: int, who, item_id, item_name, received, boss, offspec, note) -> None:
    raider = clean(who, NAME_MAX)
    if not raider:
        out.errors.append(f"line {n}: no character")
        return
    iid_text = clean(item_id, 20)
    iid = int(iid_text) if iid_text.isdigit() else 0
    nm = clean(item_name, ITEM_MAX) or None
    if not iid and not nm:
        out.errors.append(f"line {n}: no item (an id or a name)")
        return
    when, at = stamp(received)
    if when is None:
        out.errors.append(f"line {n}: no received date (YYYY-MM-DD)")
        return
    out.rows.append(ImportRow(line=n, raider=raider, item_id=iid, item_name=nm, received=when, received_at=at, boss=clean(boss, ITEM_MAX),
                              offspec=clean(offspec, 20).lower() in TRUE_WORDS, note=clean(note, NOTE_MAX) or None))


__all__ = ["KINDS", "KIND_WORDS", "GENERIC_HEADER", "ImportRow", "Parsed", "detect", "parse", "stamp"]
