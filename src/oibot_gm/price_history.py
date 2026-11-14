"""The kept price history per auction house (price watch, spec §4.1, C2, C10).

One SQLite file per auction house under `out/auctions/history/` (derived, foreign data — never the data repo), named from
the house's full key (namespace, ruleset, house). `record` keeps a row only for an item whose lowest price or quantity
listed differs from the last kept (an item that had listings and has none now gets a row with no price), once per feed
time: a feed time not later than the last kept writes nothing. A stretch of more than GAP_AFTER between two kept feed
times is kept as unread (the bot learned nothing then: the house switched away and back, prices off, the bot down, or a
feed answering "not modified" for hours). `points` / `span` read an item's rows with the price carried in from before
the span, and break the line over an unread stretch (final review F-5: a short history never reads as a quiet one);
`fold` (once a day) turns rows older than KEEP_FULL into one lowest and one highest per
item per UTC day and drops days older than KEEP_DAILY (and unread stretches that ended before it) — but each item's last value before that year stays as the
carried start while the house is still read (controller ruling P2); a file whose newest feed time is older than a year,
or that holds no row, is deleted.

Every function BLOCKS (callers use asyncio.to_thread); one connection per call, closed before returning. A file that
can't be opened or read raises HistoryError. Times are aware UTC datetimes, kept as epoch seconds."""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auctions import Price

log = logging.getLogger(__name__)

KEEP_FULL = timedelta(days=90)  # every change
KEEP_DAILY = timedelta(days=365)  # one lowest and one highest per item per day; nothing older
GAP_AFTER = timedelta(hours=3)  # more than this between two kept feed times = time the bot never read

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (one INTEGER PRIMARY KEY CHECK (one = 1), first_at INTEGER NOT NULL, last_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS changes (item INTEGER NOT NULL, at INTEGER NOT NULL, low INTEGER, high INTEGER, qty INTEGER,
                                    PRIMARY KEY (item, at)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS last (item INTEGER PRIMARY KEY, low INTEGER, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS unread (start INTEGER PRIMARY KEY, stop INTEGER NOT NULL) WITHOUT ROWID;
"""
# `unread` came after the first files were made: the IF NOT EXISTS above adds it to an older file when it is opened.


class HistoryError(Exception):
    """A history file that can't be opened or read."""


@dataclass(frozen=True)
class Point:
    """A change (`high == low`, `qty` = how many listed) or a folded day (`at` = the day's start, `qty` None).
    `low is None` = nothing listed from `at`; `unread` = the bot didn't read the house from `at` (low, high, qty None)."""
    at: datetime
    low: int | None
    high: int | None
    qty: int | None
    unread: bool = False


@dataclass(frozen=True)
class Span:
    low: int | None
    high: int | None


def root() -> Path:
    from . import discord_auctions  # read at call time (tests point OUT elsewhere; no import cycle)

    return discord_auctions.OUT / "history"


def path(key: tuple) -> Path:
    ns, ruleset, house = key
    stem = "-".join(re.sub(r"[^A-Za-z0-9-]+", "-", str(p)).strip("-") or "x" for p in (ns, ruleset, house))
    return root() / f"{stem}.db"


def _secs(t: datetime) -> int:
    return int(t.timestamp())


def _time(s: int) -> datetime:
    return datetime.fromtimestamp(int(s), timezone.utc)


def _open(p: Path) -> sqlite3.Connection:
    con = sqlite3.connect(p, timeout=5)
    try:
        con.executescript(_SCHEMA)
    except BaseException:
        con.close()
        raise
    return con


def record(key: tuple, at: datetime, prices: dict[int, Price]) -> int:
    """Keep what changed in the house at feed time `at` → rows kept; 0 when `at` is not later than the last kept feed
    time (the same table again: a restart, a second guild on the house)."""
    p = path(key)
    t = _secs(at)
    con = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = _open(p)
        con.isolation_level = None  # the transaction is opened by hand: the check and the write are one
        con.execute("BEGIN IMMEDIATE")  # the write lock before reading last_at: another writer can't slip an older feed time in
        try:
            n = _record_rows(con, t, prices)
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        return n
    except (sqlite3.Error, OSError) as e:
        raise HistoryError(f"price history {p.name}: {e}") from e
    finally:
        if con is not None:
            con.close()


def _record_rows(con: sqlite3.Connection, t: int, prices: dict[int, Price]) -> int:
    """record's check and write, inside its open transaction → rows kept."""
    row = con.execute("SELECT first_at, last_at FROM meta").fetchone()
    if row is not None and t <= row[1]:
        return 0
    if row is not None and t - row[1] > GAP_AFTER.total_seconds():
        con.execute("INSERT OR REPLACE INTO unread (start, stop) VALUES (?, ?)", (row[1], t))
    last = {item: (low, qty) for item, low, qty in con.execute("SELECT item, low, qty FROM last")}
    rows: list[tuple] = []
    for item, pr in prices.items():
        now = (int(pr.low), int(pr.qty))
        if last.get(int(item)) != now:
            rows.append((int(item), t, now[0], now[0], now[1]))
    for item, (low, _qty) in last.items():
        if low is not None and item not in prices:
            rows.append((item, t, None, None, 0))
    con.executemany("INSERT OR REPLACE INTO changes (item, at, low, high, qty) VALUES (?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT OR REPLACE INTO last (item, low, qty) VALUES (?, ?, ?)", [(r[0], r[2], r[4]) for r in rows])
    if row is None:
        con.execute("INSERT INTO meta (one, first_at, last_at) VALUES (1, ?, ?)", (t, t))
    else:
        con.execute("UPDATE meta SET last_at = ?", (t,))
    return len(rows)


def _read(key: tuple, fn):
    """Run `fn(con)` on the house's file; None when there is no file (nothing kept)."""
    p = path(key)
    if not p.exists():
        return None
    con = None
    try:
        con = _open(p)
        return fn(con)
    except (sqlite3.Error, OSError) as e:
        raise HistoryError(f"price history {p.name}: {e}") from e
    finally:
        if con is not None:
            con.close()


def points(key: tuple, item_id: int, since: datetime, until: datetime) -> list[Point]:
    """An item's kept points from `since` to `until`, oldest first; the first is the last one kept before `since`,
    moved to `since` (carried forward). [] when the item has nothing kept by `until`.

    Every unread stretch overlapping the window, for an item that had a value going into it, adds a point at the
    stretch's start (moved to `since` when it began before; low / high / qty None, `unread`) and — when the item has no
    change of its own at the stretch's end and that end is within the window — a point there with the value it resumed
    with. An item with nothing listed going in gets no extra point."""
    item, a, b = int(item_id), _secs(since), _secs(until)

    def standing(con, t: int):
        """The item's value after the read at `t` (its last row at or before it), or None."""
        return con.execute("SELECT at, low, high, qty FROM changes WHERE item = ? AND at <= ? ORDER BY at DESC LIMIT 1",
                           (item, t)).fetchone()

    def go(con):
        rows = con.execute("SELECT at, low, high, qty FROM changes WHERE item = ? AND at >= ? AND at <= ? ORDER BY at",
                           (item, a, b)).fetchall()
        before = con.execute("SELECT low, high, qty FROM changes WHERE item = ? AND at < ? ORDER BY at DESC LIMIT 1",
                             (item, a)).fetchone()
        gaps = []  # (start, stop) the item went into with a value
        for start, stop in con.execute("SELECT start, stop FROM unread WHERE start < ? AND stop > ? ORDER BY start", (b, a)).fetchall():
            into = standing(con, start)
            if into is not None and into[1] is not None:
                gaps.append((start, stop))
        out: list[tuple[int, int, Point]] = []  # (at, order, point): at one time a value comes before a break
        if before is not None and not any(s <= a < e for s, e in gaps):  # a window opening unread starts with the break
            out.append((a, 0, Point(since, *before)))
        at_set = {r[0] for r in rows}
        for at, low, high, qty in rows:
            if not any(s < at < e for s, e in gaps):  # nothing was read inside a stretch (a folded day's start may fall there)
                out.append((at, 0, Point(_time(at), low, high, qty)))
        for s, e in gaps:
            start = max(s, a)
            out.append((start, 1, Point(_time(start), None, None, None, unread=True)))
            if e <= b and e not in at_set:
                back = standing(con, e)
                out.append((e, 0, Point(_time(e), back[1], back[2], back[3])))
        out.sort(key=lambda x: (x[0], x[1]))
        return [p for _at, _o, p in out]

    return _read(key, go) or []


def span(key: tuple, item_id: int, days: int, now: datetime) -> Span:
    """The lowest and the highest of the item's lowest prices over the last `days` days; time with nothing listed and
    time the bot didn't read count for neither; both None when nothing was listed (or read) throughout."""
    pts = points(key, item_id, now - timedelta(days=days), now)
    lows = [p.low for p in pts if p.low is not None]
    highs = [p.high for p in pts if p.high is not None]
    return Span(min(lows) if lows else None, max(highs) if highs else None)


def kept_since(key: tuple) -> datetime | None:
    """The house's first kept feed time, or None when nothing was kept."""
    row = _read(key, lambda con: con.execute("SELECT first_at FROM meta").fetchone())
    return _time(row[0]) if row else None


def _day(s: int) -> int:
    return s - s % 86400


def _fold_file(p: Path, now: datetime) -> None:
    full_cut = _day(_secs(now - KEEP_FULL))  # whole UTC days before it are folded
    daily_cut = _secs(now - KEEP_DAILY)
    con = _open(p)
    try:
        meta = con.execute("SELECT first_at, last_at FROM meta").fetchone()
        if meta is not None and meta[1] < daily_cut:  # a house nobody has read for a year (P2): the file goes whole
            gone = True
        else:
            with con:
                _fold_days(con, full_cut)
                _drop_year(con, daily_cut)
                con.execute("DELETE FROM unread WHERE stop < ?", (daily_cut,))  # a stretch wholly past the year
                if meta is not None and meta[0] < daily_cut:
                    con.execute("UPDATE meta SET first_at = ?", (daily_cut,))
            gone = con.execute("SELECT 1 FROM changes LIMIT 1").fetchone() is None
    finally:
        con.close()
    if gone:
        p.unlink(missing_ok=True)


def _fold_days(con: sqlite3.Connection, cut: int) -> None:
    """Changes before `cut` → one (lowest, highest) per item per UTC day, folded with any day already folded; each item's
    last change before `cut` stays as it is (the standing value a later span carries in) and counts in its day too."""
    items = [r[0] for r in con.execute("SELECT DISTINCT item FROM changes WHERE at < ? AND qty IS NOT NULL", (cut,))]
    for item in items:
        rows = con.execute("SELECT at, low, high, qty FROM changes WHERE item = ? AND at < ? ORDER BY at", (item, cut)).fetchall()
        keep = rows[-1] if rows[-1][3] is not None else None
        days: dict[int, list[tuple]] = {}
        for r in rows:
            if r is keep:
                continue
            days.setdefault(_day(r[0]), []).append(r)
        for day, group in days.items():
            if all(r[3] is None for r in group):
                continue  # already folded
            seen = group + ([keep] if keep is not None and _day(keep[0]) == day else [])  # the standing change counts in its day
            lows = [r[1] for r in seen if r[1] is not None]
            highs = [r[2] for r in seen if r[2] is not None]
            con.executemany("DELETE FROM changes WHERE item = ? AND at = ?", [(item, r[0]) for r in group])
            con.execute("INSERT OR REPLACE INTO changes (item, at, low, high, qty) VALUES (?, ?, ?, ?, NULL)",
                        (item, day, min(lows) if lows else None, max(highs) if highs else None))


def _drop_year(con: sqlite3.Connection, cut: int) -> None:
    """Rows older than `cut` go, but each item's last one before it (its carried start, P2)."""
    con.execute("DELETE FROM changes WHERE at < ? AND at < (SELECT MAX(c.at) FROM changes c WHERE c.item = changes.item AND c.at < ?)",
                (cut, cut))


def fold(now: datetime) -> None:
    """The daily fold over every history file under root(), read or not. A file that can't be read is logged and
    skipped."""
    r = root()
    if not r.is_dir():
        return
    for p in sorted(r.glob("*.db")):
        try:
            _fold_file(p, now)
        except (sqlite3.Error, OSError):
            log.exception("price history %s not folded", p.name)


__all__ = ["GAP_AFTER", "HistoryError", "KEEP_DAILY", "KEEP_FULL", "Point", "Span", "fold", "kept_since", "path", "points", "record", "root", "span"]
