"""The guild's price table and the hourly check that keeps it current (auction prices, spec §4.2, C3, C7, C10).

`refresh` reads the selected auction house (auctions.Client.table: its listings and its stackables, conditional on the
last Last-Modified) at most once an hour; `auctions_tick` is the scheduler's hook (one background task per guild when
due — the minute loop never waits for Blizzard); `house_choices` is the owner's picker (the guild's ruleset from its
armory page, then each house of the guild's faction, C1). Module functions taking the client, like discord_armory.py.

The table is derived, foreign data: `out/auctions/<guild key>/prices.json`, written whole (temp + rename), loaded at
first use after a restart, never in the data repo (C7). `Table.at` is the last check that worked (an update or a
not-modified answer), so an unchanged feed stays fresh. Stale after six hours (C3), not shown at all after a day (C10)
— a table loaded after a restart too. A table of a house that is not the one selected now is no table. Two guilds on
one auction house share one fetch within the hour (memory). A fetch whose market changed while it ran is discarded.

After each check that leaves a table, the same background task names up to NAME_PER_FETCH listed ids nothing local names
(Blizzard's item endpoint, most listed first; prices.remember keeps them per game version in out/auctions/items.json).
A failure there stops the naming quietly until the next check.

The DM rule holds: nothing here DMs anyone, writes a notice or posts an ops line on success. A failure keeps the last
table and warns the guild's ops feed at most once an hour."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from . import armory, auctions
from .armory import ArmoryError
from .auctions import House, Price
from .registry import RegistryError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "auctions"  # read at call time (tests point it elsewhere)

FETCH_EVERY = timedelta(hours=1)
STALE_AFTER = timedelta(hours=6)  # C3: shown, marked stale
HIDE_AFTER = timedelta(hours=24)  # C10: not shown at all
WARN_EVERY = timedelta(hours=1)  # one ops warning per guild per hour
NAME_PER_FETCH = 200  # listed ids named from Blizzard after each check, most listed first

# the words (spec §3): what a reader sees when there is no price to show, and why
OFF_WORDS = "Prices are off until the owner picks the guild's auction house on the Config page."
WAITING_WORDS = "Prices haven't been read from the auction house yet. The bot reads it every hour."
OLD_WORDS = "Prices are out of date: the last ones read are more than a day old. The bot tries again every hour."
STALE_TAIL = " — more than six hours old"
LINK_FIRST = "Link the server to its guild in the game first, then pick its auction house."

State = Literal["off", "waiting", "fresh", "stale", "old"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client(reg) -> auctions.Client:
    """The one seam to the auction client (tests replace it)."""
    return auctions.client_for(reg)


@dataclass(frozen=True)
class Table:
    at: datetime  # the last check that worked (UTC)
    house_id: int
    ruleset_id: int
    prices: dict[int, Price]
    modified: str | None  # the listings' Last-Modified, asked on the next check


@dataclass(frozen=True)
class Choice:
    """One auction house the owner may pick: how many are listed in all and the reference items' lowest prices."""
    house: House
    ruleset_id: int
    listings: int
    samples: list[tuple[int, int | None]]  # (item id, lowest price in copper | None = none listed)


PRICE_LISTENERS: list[Callable[[str], None]] = []  # fn(guild key) after a guild's table is updated


# ------------------------------------------------------------------ in-memory state

_STATE: dict[str, dict] = {}
_SHARED: dict[tuple, Table] = {}  # (namespace, ruleset, house) → the last table read, whichever guild read it
_LOCKS: dict[tuple, tuple[object, asyncio.Lock]] = {}
_UNLOADED = object()


def reset_state() -> None:
    """Forget everything kept in memory (a restart; tests)."""
    _STATE.clear()
    _SHARED.clear()
    _LOCKS.clear()


def _st(reg) -> dict:
    return _STATE.setdefault(reg.key, {"table": _UNLOADED, "warned_at": None, "tried": None, "task": None})


def _house_key(reg, ruleset_id: int, house_id: int) -> tuple:
    """Which auction house, process-wide: the version's namespace in the guild's region, the ruleset, the house."""
    a = reg.profile.auctions or {}
    region = str((reg.profile.armory or {}).get("region", ""))
    return str(a.get("dynamic_namespace", "")).format(region=region), int(ruleset_id), int(house_id)


def _lock(key: tuple) -> asyncio.Lock:
    """One lock per auction house, for the running loop: two guilds on one house never fetch it twice at once."""
    loop = asyncio.get_running_loop()
    held = _LOCKS.get(key)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _LOCKS[key] = held
    return held[1]


def _market(reg):
    """The selected market, or None: none picked, or a version with no auction houses."""
    return reg.config.market if reg.profile.auctions else None


def _same(m, t: Table | None) -> bool:
    return m is not None and t is not None and (t.house_id, t.ruleset_id) == (int(m.house_id), int(m.ruleset_id))


# ------------------------------------------------------------------ the file (out/, never the data repo)


def _path(reg) -> Path:
    return OUT / reg.key / "prices.json"


def _decode(d: dict) -> Table:
    at = datetime.fromisoformat(str(d["at"]))
    if at.tzinfo is None:
        raise ValueError("naive time")
    prices = {int(k): Price(low=int(v[0]), qty=int(v[1])) for k, v in d["prices"].items()}
    return Table(at=at, house_id=int(d["house_id"]), ruleset_id=int(d["ruleset_id"]), prices=prices,
                 modified=None if d.get("modified") is None else str(d["modified"]))


def _encode(t: Table) -> str:
    return json.dumps({"at": t.at.isoformat(timespec="seconds"), "house_id": t.house_id, "ruleset_id": t.ruleset_id,
                       "modified": t.modified, "prices": {str(k): [p.low, p.qty] for k, p in sorted(t.prices.items())}},
                      separators=(",", ":"))


def _load(reg) -> Table | None:
    st = _st(reg)
    if st["table"] is _UNLOADED:
        t = None
        try:
            t = _decode(json.loads(_path(reg).read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a damaged derived file is read again from the auction house
            log.warning("price table for %s unreadable; it is read again", reg.key)
        st["table"] = t
    return st["table"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


async def _save(reg, t: Table) -> None:
    _st(reg)["table"] = t
    try:
        await asyncio.to_thread(_write, _path(reg), _encode(t))
    except OSError:
        log.exception("price table for %s not saved", reg.key)


def _heard(reg) -> None:
    for fn in list(PRICE_LISTENERS):
        try:
            fn(reg.key)
        except Exception:  # noqa: BLE001 — a listener never stops the check
            log.exception("price listener failed")


# ------------------------------------------------------------------ reads


def table(reg) -> Table | None:
    """The stored table of the auction house selected NOW (any age), else None (none selected, none read yet, or the file
    holds another house's)."""
    m = _market(reg)
    if m is None:
        return None
    t = _load(reg)
    return t if _same(m, t) else None


def state(reg) -> State:
    """off = no auction houses on this version or none picked; waiting = none read yet; fresh; stale (six hours); old
    (a day: not shown)."""
    if _market(reg) is None:
        return "off"
    t = table(reg)
    if t is None:
        return "waiting"
    age = _now() - t.at
    return "fresh" if age < STALE_AFTER else "stale" if age < HIDE_AFTER else "old"


def price_of(reg, item_id: int) -> Price | None:
    """An item's lowest price and how many are listed, from a table recent enough to show; None = none listed or no
    prices to show (check `state`)."""
    if state(reg) not in ("fresh", "stale"):
        return None
    return table(reg).prices.get(int(item_id))


def as_of_words(reg) -> str:
    """The one line saying how recent the prices are, or why there are none."""
    s = state(reg)
    if s == "off":
        return OFF_WORDS
    if s == "waiting":
        return WAITING_WORDS
    if s == "old":
        return OLD_WORDS
    words = f"Prices as of {reg.local12(table(reg).at)}"
    return words + STALE_TAIL if s == "stale" else words


# ------------------------------------------------------------------ the check


async def _emit_warn(bot, reg, text: str) -> None:
    st = _st(reg)
    if st["warned_at"] is not None and _now() - st["warned_at"] < WARN_EVERY:
        log.info("[%s] %s (warned this hour already)", reg.key, text)
        return
    st["warned_at"] = _now()
    ops = getattr(bot, "ops", None)
    if ops is None:
        log.warning("[%s] %s", reg.key, text)
        return
    try:
        await ops.emit(reg.config, "warn", text)
    except Exception:  # noqa: BLE001 — an ops line never stops the check
        log.exception("ops emit failed")


async def refresh(bot, reg, *, force: bool = False) -> str:
    """Read the selected auction house when due (an hour since the guild's last check, or `force`) →
    "updated" | "unchanged" (not due, the feed unchanged, or the market changed while it ran) | "off" | "failed".
    Another guild's read of the same house within the hour is taken instead of a request."""
    m = _market(reg)
    if m is None or not auctions.available(reg):
        return "off"
    key = _house_key(reg, m.ruleset_id, m.house_id)
    st = _st(reg)
    async with _lock(key):
        st["tried"] = (key, _now())
        own = table(reg)
        if not force and own is not None and _now() - own.at < FETCH_EVERY:
            return "unchanged"
        shared = _SHARED.get(key)
        if not force and shared is not None and _now() - shared.at < FETCH_EVERY:
            if own == shared:
                return "unchanged"
            await _save(reg, shared)
            _heard(reg)
            return "updated"
        base = own if own is not None else shared
        if base is not None and shared is not None and shared.at > base.at:
            base = shared
        since = base.modified if base is not None else None
        try:
            prices, modified = await asyncio.to_thread(_client(reg).table, int(m.ruleset_id), int(m.house_id), since)
        except ArmoryError as e:
            log.warning("price fetch for %s failed: %s", reg.key, e)
            await _emit_warn(bot, reg, f"Reading prices from the {m.name} stopped. {e.words}")
            return "failed"
        now = _now()
        if prices is None and base is None:  # a 304 with nothing to keep: read it whole next time
            return "unchanged"
        t = Table(at=now, house_id=int(m.house_id), ruleset_id=int(m.ruleset_id),
                  prices=base.prices if prices is None else prices, modified=modified or (base.modified if base else None))
        _SHARED[key] = t
        if not _same(_market(reg), t):  # the owner changed or cleared the market while it ran
            return "off" if _market(reg) is None else "unchanged"
        await _save(reg, t)
        if own is not None and t.prices == own.prices:  # the guild's own prices, only fresher: nothing new to hear
            return "unchanged"
        _heard(reg)
        return "updated"


def _due(reg, m) -> bool:
    st = _st(reg)
    now = _now()
    key = _house_key(reg, m.ruleset_id, m.house_id)
    tried = st["tried"]
    if tried is not None and tried[0] == key and now - tried[1] < FETCH_EVERY:
        return False  # tried this hour (worked or not): a failure waits an hour; a house picked since is read now
    t = table(reg)
    return t is None or now - t.at >= FETCH_EVERY


async def auctions_tick(bot, reg):
    """raid_scheduler.scheduler_tick_guild's hook, every minute: when due, ONE background task for the guild reads its
    auction house. Never waits for Blizzard itself. Off without auction houses, credentials or a picked house.
    → the task started, or None."""
    m = _market(reg)
    if m is None or not auctions.available(reg):
        return None
    st = _st(reg)
    if st["task"] is not None and not st["task"].done():
        return None
    if not _due(reg, m):
        return None

    async def go():
        try:
            if await refresh(bot, reg) in ("updated", "unchanged"):
                await name_items(bot, reg)
        except Exception:  # noqa: BLE001 — the hourly check never takes the scheduler down
            log.exception("auctions tick failed for %s", reg.key)

    st["task"] = asyncio.get_running_loop().create_task(go())
    return st["task"]


# ------------------------------------------------------------------ item names (spec §4.3)


def _name_batch(cli, ids: list[int]) -> tuple[dict[int, str], set[int]]:
    """Look up each id's name in turn (BLOCKS) → (named, ids Blizzard has no item for). The first failure stops it with
    what was named so far."""
    found: dict[int, str] = {}
    gone: set[int] = set()
    for i in ids:
        try:
            name = cli.item_name(i)
        except Exception as e:  # noqa: BLE001 — naming is best effort: it stops quietly and goes on next hour
            log.info("item naming stopped after %d: %s", len(found), getattr(e, "detail", None) or type(e).__name__)
            break
        if name:
            found[i] = name
        else:
            gone.add(i)
    return found, gone


async def name_items(bot, reg) -> int:
    """After a check, name up to NAME_PER_FETCH listed ids that nothing names, most listed first (the hourly check's own
    background task, never a request path) → how many were named. A failure stops it quietly: no ops line. One naming
    per game version at a time; the names are kept per version (prices.remember)."""
    from . import prices  # prices reads this module's table

    t = table(reg)
    if t is None:
        return 0
    async with _lock(("names", reg.profile.name)):
        ids = prices.unnamed(reg, t.prices, NAME_PER_FETCH)
        if not ids:
            return 0
        found, gone = await asyncio.to_thread(_name_batch, _client(reg), ids)
        await asyncio.to_thread(prices.remember, reg, found, gone)
        return len(found)


# ------------------------------------------------------------------ the owner's picker


async def house_choices(bot, reg) -> list[Choice]:
    """The auction houses the owner may pick (C1): the guild's ruleset read from its armory page, then each house named
    for the guild's faction that has listings now, with how many are listed and the reference items' lowest prices.
    Blocking requests in threads (the caller shows a working screen). Raises RegistryError without auction houses or a
    linked guild, armory's errors when Blizzard fails. A house read here is shared with the first hourly check."""
    if not reg.profile.auctions:
        raise RegistryError(auctions.NO_AUCTIONS_WORDS)
    g = reg.config.armory
    if g is None:
        raise RegistryError(LINK_FIRST)
    facts = await asyncio.to_thread(armory.client_for(reg).guild, g.slug)
    ruleset = int(facts.ruleset_id)
    cli = _client(reg)
    houses = auctions.faction_houses(reg, await asyncio.to_thread(cli.houses, ruleset), g.faction)
    refs = [int(i) for i in (reg.profile.auctions.get("reference_items") or [])]
    out: list[Choice] = []
    for h in houses:
        prices, modified = await asyncio.to_thread(cli.table, ruleset, h.id)
        if not prices:
            continue
        _SHARED[_house_key(reg, ruleset, h.id)] = Table(at=_now(), house_id=int(h.id), ruleset_id=ruleset, prices=prices, modified=modified)
        out.append(Choice(house=h, ruleset_id=ruleset, listings=sum(p.qty for p in prices.values()),
                          samples=[(i, prices[i].low if i in prices else None) for i in refs]))
    return out


__all__ = ["Choice", "FETCH_EVERY", "HIDE_AFTER", "NAME_PER_FETCH", "OFF_WORDS", "OLD_WORDS", "PRICE_LISTENERS", "STALE_AFTER", "Table",
           "WAITING_WORDS", "as_of_words", "auctions_tick", "house_choices", "name_items", "price_of", "refresh", "reset_state", "state", "table"]
