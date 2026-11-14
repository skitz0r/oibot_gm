"""The guild's price table and the hourly check that keeps it current (auction prices, spec §4.2, C3, C7, C10).

`refresh` reads the selected auction house (auctions.Client.table: its listings and its stackables, conditional on the
last Last-Modified) at most once an hour; `auctions_tick` is the scheduler's hook (one background task per guild when
due — the minute loop never waits for Blizzard); `house_choices` is the owner's picker (the guild's ruleset from its
armory page, then each house of the guild's faction, C1). Module functions taking the client, like discord_armory.py.

The table is derived, foreign data: `out/auctions/<guild key>/prices.json`, written whole (temp + rename), loaded at
first use after a restart, never in the data repo (C7). `Table.at` is the feed's time: the answer's Last-Modified (never
later than now; now when it gives none or it can't be read), so a not-modified answer leaves it and a feed that stalls
ages. `Table.checked_at` is the last check that worked (an update or a not-modified answer): the hourly schedule and the
shared fetch read it. Stale after six hours (C3), not shown at all after a day (C10), both from `at` — a table loaded
after a restart too. A table of a house that is not the one selected now is no table. Two guilds on
one auction house share one fetch within the hour (memory). A fetch whose market changed while it ran is discarded.

After each check that leaves a table, the same background task names up to NAME_PER_FETCH listed ids nothing local names
(Blizzard's item endpoint, most listed first; prices.remember keeps them per game version in out/auctions/items.json).
A failure there stops the naming quietly until the next check.

A new table from Blizzard taken as the guild's table is also kept in the house's price history (price_history.record in a
thread, keyed by `history_key` — the house, never the guild), and so is an adopted shared read (the owner's pick, another
guild's read: record writes once per feed time, under the house's lock); never a not-modified answer or a read discarded
because the market changed. A failed keep keeps the table and warns ops once an hour (HISTORY_FAILED, its
own hour). `auctions_tick` starts the history's fold once a UTC day (price_history.fold, one background task).

The DM rule holds: nothing here DMs anyone, writes a notice or posts an ops line on success. A failure keeps the last
table and warns the guild's ops feed at most once an hour."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Literal

from . import armory, auctions, price_history
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
NO_CHOICES = "No auction house of the guild's faction has anything listed right now. Try again in a while."
NOT_A_CHOICE = "That isn't one of the guild's auction houses. Look at the choices again."
HOUSES_CHANGED = "The guild's auction houses changed since you looked. Look at the choices again."
HISTORY_FAILED = "Keeping the price history stopped. The prices themselves still update."

State = Literal["off", "waiting", "fresh", "stale", "old"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client(reg) -> auctions.Client:
    """The one seam to the auction client (tests replace it)."""
    return auctions.client_for(reg)


@dataclass(frozen=True)
class Table:
    at: datetime  # the feed's time (UTC): its Last-Modified, never later than the check; a not-modified answer leaves it
    house_id: int
    ruleset_id: int
    prices: dict[int, Price]
    modified: str | None  # the listings' Last-Modified, asked on the next check
    checked_at: datetime | None = None  # the last check that worked (UTC); None = `at`

    @property
    def checked(self) -> datetime:
        return self.checked_at or self.at


def feed_time(modified: str | None, now: datetime) -> datetime:
    """The feed's time from its Last-Modified, never later than `now`; `now` when there is none or it can't be read."""
    if not modified:
        return now
    try:
        t = parsedate_to_datetime(str(modified))
    except (TypeError, ValueError, IndexError):
        return now
    if t is None or t.tzinfo is None:
        return now
    return min(t.astimezone(timezone.utc), now)


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
_FOLD: dict = {"day": None}  # the UTC date of the last daily fold of the kept history (process-wide)


def reset_state() -> None:
    """Forget everything kept in memory (a restart; tests)."""
    _STATE.clear()
    _SHARED.clear()
    _LOCKS.clear()
    _FOLD["day"] = None


def _st(reg) -> dict:
    return _STATE.setdefault(reg.key, {"table": _UNLOADED, "warned_at": {}, "tried": None, "task": None, "seen": None})


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


def history_key(reg) -> tuple | None:
    """The selected auction house's key for its kept price history (price_history), None without a market."""
    m = _market(reg)
    return None if m is None else _house_key(reg, m.ruleset_id, m.house_id)


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
    checked = datetime.fromisoformat(str(d["checked_at"])) if d.get("checked_at") else at  # a file from before: checked = at
    if checked.tzinfo is None:
        raise ValueError("naive time")
    return Table(at=at, house_id=int(d["house_id"]), ruleset_id=int(d["ruleset_id"]), prices=prices,
                 modified=None if d.get("modified") is None else str(d["modified"]), checked_at=checked)


def _encode(t: Table) -> str:
    return json.dumps({"at": t.at.isoformat(timespec="seconds"), "checked_at": t.checked.isoformat(timespec="seconds"), "house_id": t.house_id, "ruleset_id": t.ruleset_id,
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


def as_of_words(reg, *, stamp: bool = False) -> str:
    """The one line saying how recent the prices are, or why there are none. `stamp`: Discord's native relative time
    (`<t:…:R>`, read in the reader's own clock) instead of Registry.local12 words."""
    s = state(reg)
    if s == "off":
        return OFF_WORDS
    if s == "waiting":
        return WAITING_WORDS
    if s == "old":
        return OLD_WORDS
    at = table(reg).at
    words = f"Prices as of <t:{int(at.timestamp())}:R>" if stamp else f"Prices as of {reg.local12(at)}"
    return words + STALE_TAIL if s == "stale" else words


# ------------------------------------------------------------------ the check


async def _emit_warn(bot, reg, text: str, kind: str = "read") -> None:
    """One ops warning per guild per hour for each kind (a failed read, a failed history keep)."""
    warned = _st(reg)["warned_at"]
    if warned.get(kind) is not None and _now() - warned[kind] < WARN_EVERY:
        log.info("[%s] %s (warned this hour already)", reg.key, text)
        return
    warned[kind] = _now()
    ops = getattr(bot, "ops", None)
    if ops is None:
        log.warning("[%s] %s", reg.key, text)
        return
    try:
        await ops.emit(reg.config, "warn", text)
    except Exception:  # noqa: BLE001 — an ops line never stops the check
        log.exception("ops emit failed")


_RUNNING: set[asyncio.Task] = set()  # background reads started by a pick (kept so they aren't collected mid-run)


async def refresh_soon(bot, reg) -> asyncio.Task:
    """After the owner picks an auction house (the site or Discord): its first read, off the request. The picker's own
    read is usually taken as it is (no request). A failure is logged; the hourly check tries again."""
    async def go():
        try:
            await refresh(bot, reg)
        except Exception:  # noqa: BLE001 — the hourly check tries again
            log.exception("first price read for %s failed", reg.key)

    t = asyncio.get_running_loop().create_task(go())
    _RUNNING.add(t)
    t.add_done_callback(_RUNNING.discard)
    await asyncio.sleep(0)  # let the task start now (a shared read then lands before the caller answers)
    return t


async def refresh(bot, reg, *, force: bool = False) -> str:
    """Read the selected auction house when due (an hour since the guild's last check — `checked_at` —, or `force`) →
    "updated" (new prices, or a new feed time bringing a stale / hidden table back to fresh) | "unchanged" (not due, a
    not-modified answer, the same prices on a fresh table, or the market changed while it ran) | "off" | "failed".
    Another guild's read of the same house within the hour is taken instead of a request."""
    m = _market(reg)
    if m is None or not auctions.available(reg):
        return "off"
    key = _house_key(reg, m.ruleset_id, m.house_id)
    st = _st(reg)
    async with _lock(key):
        st["tried"] = (key, _now())
        before = state(reg)  # a new feed time bringing stale / old back (to fresh, or old to stale) is news for an open page, same prices or not
        own = table(reg)
        if not force and own is not None and _now() - own.checked < FETCH_EVERY:
            return "unchanged"
        shared = _SHARED.get(key)
        if not force and shared is not None and _now() - shared.checked < FETCH_EVERY:
            if own == shared:
                return "unchanged"
            await _save(reg, shared)
            await _keep(bot, reg, key, shared)  # an adopted read is kept too: one already kept writes nothing (once per feed time)
            _heard(reg)
            return "updated"
        base = own if own is not None else shared
        if base is not None and shared is not None and (shared.at, shared.checked) > (base.at, base.checked):
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
        if prices is None:  # not modified: the same prices, as of the same feed time; only the check moves
            t = replace(base, house_id=int(m.house_id), ruleset_id=int(m.ruleset_id), modified=modified or base.modified, checked_at=now)
        else:
            t = Table(at=feed_time(modified, now), house_id=int(m.house_id), ruleset_id=int(m.ruleset_id), prices=prices,
                      modified=modified or (base.modified if base else None), checked_at=now)
        _SHARED[key] = t
        if not _same(_market(reg), t):  # the owner changed or cleared the market while it ran
            return "off" if _market(reg) is None else "unchanged"
        await _save(reg, t)
        if prices is not None:  # a new table from Blizzard, taken as the guild's: its changes are kept (once per house per feed time)
            await _keep(bot, reg, key, t)
        if own is not None and t.prices == own.prices and state(reg) == before:  # the same prices, shown the same way: nothing new to hear (a 304 never changes the state)
            return "unchanged"
        _heard(reg)
        return "updated"


async def _keep(bot, reg, key: tuple, t: Table) -> None:
    """Keep the new table's changes in the house's price history (a thread). A failure keeps the table and warns once an
    hour."""
    try:
        await asyncio.to_thread(price_history.record, key, t.at, t.prices)
    except price_history.HistoryError:
        log.exception("price history for %s not kept", reg.key)
        await _emit_warn(bot, reg, HISTORY_FAILED, kind="history")


def _fold_daily() -> None:
    """Once a UTC day: ONE background task folding every kept history file (price_history.fold in a thread)."""
    now = _now()
    if _FOLD["day"] == now.date():
        return
    _FOLD["day"] = now.date()

    async def go():
        try:
            await asyncio.to_thread(price_history.fold, now)
        except Exception:  # noqa: BLE001 — the fold tries again tomorrow
            log.exception("price history fold failed")

    t = asyncio.get_running_loop().create_task(go())
    _RUNNING.add(t)
    t.add_done_callback(_RUNNING.discard)


def _due(reg, m) -> bool:
    st = _st(reg)
    now = _now()
    key = _house_key(reg, m.ruleset_id, m.house_id)
    tried = st["tried"]
    if tried is not None and tried[0] == key and now - tried[1] < FETCH_EVERY:
        return False  # tried this hour (worked or not): a failure waits an hour; a house picked since is read now
    t = table(reg)
    return t is None or now - t.checked >= FETCH_EVERY


async def auctions_tick(bot, reg):
    """raid_scheduler.scheduler_tick_guild's hook, every minute: when due, ONE background task for the guild reads its
    auction house. Never waits for Blizzard itself. Off without auction houses, credentials or a picked house.
    → the task started, or None.
    It also remembers the state it last saw for the guild: time alone turning the table stale (six hours) or hidden (a
    day), with no check that worked in between, is told to the listeners once, so an open page follows."""
    st = _st(reg)
    seen, st["seen"] = st.get("seen"), state(reg)
    if seen is not None and seen != st["seen"] and st["seen"] in ("stale", "old"):
        _heard(reg)
    if reg.profile.auctions:  # the kept history's daily fold: every house's file, picked or not (no auction houses: none)
        _fold_daily()
    m = _market(reg)
    if m is None or not auctions.available(reg):
        return None
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
    """Look up each id's name in turn (BLOCKS) → (named, ids not to ask again in this process: no item there, or an odd
    answer). The first other failure (Blizzard down, access refused, anything unexpected) stops it with what was named."""
    found: dict[int, str] = {}
    gone: set[int] = set()
    for i in ids:
        try:
            name = cli.item_name(i)
        except armory.ArmoryShape as e:  # one odd answer: skipped like a 404, never asked first again every hour
            log.info("item %d not named: %s", i, e)
            name = None
        except Exception as e:  # noqa: BLE001 — naming is best effort: it stops quietly and goes on next hour
            log.info("item naming stopped after %d: %s", len(found), e if isinstance(e, ArmoryError) else type(e).__name__)
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
        now = _now()
        _SHARED[_house_key(reg, ruleset, h.id)] = Table(at=feed_time(modified, now), house_id=int(h.id), ruleset_id=ruleset, prices=prices,
                                                        modified=modified, checked_at=now)
        out.append(Choice(house=h, ruleset_id=ruleset, listings=sum(p.qty for p in prices.values()),
                          samples=[(i, prices[i].low if i in prices else None) for i in refs]))
    return out


class HousesChanged(RegistryError):
    """The guild's ruleset is not the one the owner's choices were read for (the site answers 409)."""


async def pick_house(bot, reg, house_id: int, ruleset_id: int) -> House:
    """The auction house a picker's ids name, checked against the armory NOW (the site sends ids back, so they are never
    trusted): the guild's ruleset must still be `ruleset_id` (else HousesChanged) and `house_id` one of its faction's
    houses (else RegistryError NOT_A_CHOICE). Blocking requests in threads, like house_choices; armory's errors pass."""
    if not reg.profile.auctions:
        raise RegistryError(auctions.NO_AUCTIONS_WORDS)
    g = reg.config.armory
    if g is None:
        raise RegistryError(LINK_FIRST)
    facts = await asyncio.to_thread(armory.client_for(reg).guild, g.slug)
    if int(facts.ruleset_id) != int(ruleset_id):
        raise HousesChanged(HOUSES_CHANGED)
    houses = auctions.faction_houses(reg, await asyncio.to_thread(_client(reg).houses, int(ruleset_id)), g.faction)
    for h in houses:
        if int(h.id) == int(house_id):
            return h
    raise RegistryError(NOT_A_CHOICE)


__all__ = ["Choice", "HISTORY_FAILED", "HOUSES_CHANGED", "history_key", "NO_CHOICES", "HousesChanged", "NOT_A_CHOICE", "pick_house", "FETCH_EVERY", "HIDE_AFTER", "NAME_PER_FETCH", "OFF_WORDS", "OLD_WORDS", "PRICE_LISTENERS", "STALE_AFTER", "Table",
           "WAITING_WORDS", "as_of_words", "auctions_tick", "refresh_soon", "house_choices", "name_items", "price_of", "refresh", "reset_state", "state", "table"]
