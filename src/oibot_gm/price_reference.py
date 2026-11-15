"""Reference prices from ANOTHER economy (Bargains, 2026-11-15): one public CSV of market values per item — for
`forever`, TradeSkillMaster's Classic region file (integrations.yaml `auctions.reference`; its terms allow automated
reads of the public pricing files). Read at most once a day into out/auctions/reference/ (never the data repo), kept
whole; a failed read keeps the last copy. BLOCKING: callers use asyncio.to_thread. Nothing here knows a guild, a member
or an item name — a URL in, numbers out; the request carries nothing about this bot's guilds."""
from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BYTES = 20_000_000  # a file larger than this is not the file
MIN_ROWS = 100  # fewer rows than this is not the file either
COLUMNS = ("itemId", "marketValue", "soldPerDay")


class ReferenceFailed(Exception):
    """The file couldn't be read or isn't the expected table (the last copy stays)."""


@dataclass(frozen=True)
class Reference:
    read_at: datetime  # when this copy was written (UTC)
    values: dict[int, tuple[int, float]]  # item id → (market value in copper, sold per day)


def fetch_text(url: str) -> str:
    """The file's text (the network; tests replace it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; price-reader)", "Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — the URL is the profile's, https
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ReferenceFailed("too large")
    return body.decode("utf-8")


def parse(text: str) -> dict[int, tuple[int, float]]:
    """{item id: (market value in copper, sold per day)} from the CSV; a row that doesn't read is skipped, a table
    without the columns or with too few rows is refused."""
    rows = csv.DictReader(io.StringIO(text))
    if not rows.fieldnames or any(c not in rows.fieldnames for c in COLUMNS):
        raise ReferenceFailed("unexpected columns")
    out: dict[int, tuple[int, float]] = {}
    for row in rows:
        try:
            item, value, sold = int(row["itemId"]), int(float(row["marketValue"])), float(row["soldPerDay"])
        except (TypeError, ValueError):
            continue
        if item > 0 and value > 0 and sold >= 0:
            out[item] = (value, sold)
    if len(out) < MIN_ROWS:
        raise ReferenceFailed(f"only {len(out)} rows")
    return out


def refresh(url: str, dest: Path) -> int:
    """Read the file and keep it whole at `dest` (a temp name, then a rename) → how many items it prices. Raises
    ReferenceFailed (or the network's error) with the last copy untouched."""
    text = fetch_text(url)
    n = len(parse(text))
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.stem}-", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return n


_LOADED: dict[Path, tuple[int, Reference]] = {}  # path → (mtime in ns, what it held)


def load(dest: Path) -> Reference | None:
    """The kept copy, read once per write; None when there is none or it can't be read."""
    try:
        stat = dest.stat()
    except OSError:
        return None
    held = _LOADED.get(dest)
    if held is not None and held[0] == stat.st_mtime_ns:
        return held[1]
    try:
        ref = Reference(read_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc), values=parse(dest.read_text(encoding="utf-8")))
    except (OSError, ReferenceFailed, UnicodeDecodeError):
        log.warning("reference prices unreadable at %s", dest)
        return None
    _LOADED[dest] = (stat.st_mtime_ns, ref)
    return ref


__all__ = ["Reference", "ReferenceFailed", "fetch_text", "load", "parse", "refresh"]
