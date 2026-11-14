"""Prices for people (auction prices, spec §3, §4.3–§4.5, C6, C9, C10): item names, the price search, a recipe's cost to
make and every sentence a person reads about a price. Pure: it reads the guild's price table (discord_auctions) and never
asks Blizzard — the routes, Discord, the help answer and the pages are thin callers of it.

Names (§4.3): what a recipe makes and its reagents, then the profile's loot items, then the names the hourly check looked
up for listed ids nothing local names (`out/auctions/items.json`, one block per game version, written whole). An unnamed
id is not searchable until it is named.

Every read is gated on discord_auctions.state: fresh or stale shows prices; off, waiting and old (a day, C10) show none.
Cost to make is per craft (C9): what the recipe makes shows its own lowest price "each", never combined with the cost."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import discord_auctions
from .auctions import Price, money
from .profiles import RecipeDef

log = logging.getLogger(__name__)

NONE_LISTED = "none listed"
OWNER_OFF_WORDS = "Pick the guild's auction house on the Config page to see prices."


# ------------------------------------------------------------------ names


_LOCAL: dict[str, tuple[tuple, dict[int, str]]] = {}  # version → (the profile's tables it was built from, names)
_FETCHED: dict[str, dict[int, str]] | None = None  # version → {id: name} looked up from Blizzard (the file); None = unread
_GONE: dict[str, set[int]] = {}  # version → ids Blizzard has no item for (this process only)
_MERGED: dict[str, tuple[tuple, dict[int, str]]] = {}  # version → ((the local names, _GEN), local + fetched)
_GEN = 0  # moves on every change to the looked-up names
_FILE_LOCK = threading.Lock()


def reset_names() -> None:
    """Forget the names kept in memory (a restart; tests)."""
    global _FETCHED, _GEN
    with _FILE_LOCK:
        _GEN += 1
        _LOCAL.clear()
        _MERGED.clear()
        _GONE.clear()
        _FETCHED = None


def _items_path() -> Path:
    return discord_auctions.OUT / "items.json"


def _fetched() -> dict[str, dict[int, str]]:
    global _FETCHED
    if _FETCHED is None:
        got: dict[str, dict[int, str]] = {}
        try:
            d = json.loads(_items_path().read_text(encoding="utf-8"))
            got = {str(v): {int(k): str(n) for k, n in block.items() if str(n)} for v, block in d.items()}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a damaged file of looked-up names is looked up again
            log.warning("item names file unreadable; names are looked up again")
        _FETCHED = got
    return _FETCHED


def _local(profile) -> dict[int, str]:
    """The names the profile carries: the loot items (known_items, then the item data), then the recipe list on top."""
    tables = (profile.recipes, profile.items, profile.known_items)
    held = _LOCAL.get(profile.name)
    if held is not None and all(a is b for a, b in zip(held[0], tables)):
        return held[1]
    out: dict[int, str] = {int(k): str(v) for k, v in (profile.known_items or {}).items()}
    out.update({int(k): str(it.name) for k, it in (profile.items or {}).items()})
    for r in (profile.recipes or {}).values():
        for x in r.reagents:
            out[int(x["id"])] = str(x["name"])
        if r.makes:
            out[int(r.makes["id"])] = str(r.makes["name"])
    _LOCAL[profile.name] = (tables, out)
    return out


def names(reg) -> dict[int, str]:
    """Every item the bot can name for the guild's game version: the recipe list (what a recipe makes, its reagents), the
    profile's loot items, then the names looked up for listed ids. Read it, never change it."""
    version = reg.profile.name
    local = _local(reg.profile)
    gen = _GEN
    held = _MERGED.get(version)
    if held is not None and held[0][0] is local and held[0][1] == gen:
        return held[1]
    out = dict(_fetched().get(version) or {})
    out.update(local)
    _MERGED[version] = ((local, gen), out)
    return out


def unnamed(reg, listed: dict[int, Price], limit: int) -> list[int]:
    """Up to `limit` listed ids that nothing names and Blizzard was not found lacking, most listed first."""
    known = names(reg)
    gone = _GONE.get(reg.profile.name, set())
    ids = [i for i in listed if i not in known and i not in gone]
    ids.sort(key=lambda i: (-listed[i].qty, i))
    return ids[:limit]


def remember(reg, found: dict[int, str], gone: set[int]) -> None:
    """Keep the names looked up for the guild's version and write the file whole (a unique temp name, then a rename).
    BLOCKS on the write: call it through asyncio.to_thread."""
    global _FETCHED, _GEN
    version = reg.profile.name
    with _FILE_LOCK:
        _GONE.setdefault(version, set()).update(gone)
        if not found:
            return
        current = _fetched()
        block = dict(current.get(version) or {})
        block.update({int(k): str(v) for k, v in found.items() if v})
        _FETCHED = {**current, version: block}  # copy on write: readers on the loop never see a half-built dict
        _GEN += 1
        text = json.dumps({v: {str(k): n for k, n in sorted(b.items())} for v, b in sorted(_FETCHED.items())},
                          separators=(",", ":"), ensure_ascii=False)
        path = _items_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".items-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError:
            log.exception("item names not saved")


# ------------------------------------------------------------------ prices


def _shown(reg) -> bool:
    return discord_auctions.state(reg) in ("fresh", "stale")


@dataclass(frozen=True)
class ItemPrice:
    id: int
    name: str
    low: int | None  # the lowest price listed, copper; None = none listed
    qty: int  # how many are listed


def _item(reg, item_id: int, name: str) -> ItemPrice:
    p = discord_auctions.price_of(reg, item_id)
    return ItemPrice(id=int(item_id), name=name, low=p.low if p else None, qty=p.qty if p else 0)


def search(reg, text: str, limit: int = 25) -> tuple[list[ItemPrice], int]:
    """Named items whose name holds every typed word (case-folded): listed ones first, then by name → (the first
    `limit`, how many match in all). Nothing typed, or no prices to show → ([], 0)."""
    words = str(text or "").casefold().split()
    if not words or not _shown(reg):
        return [], 0
    t = discord_auctions.table(reg)
    hits = [(i, n) for i, n in names(reg).items() if all(w in n.casefold() for w in words)]
    hits.sort(key=lambda h: (h[0] not in t.prices, h[1].casefold(), h[0]))
    return [_item(reg, i, n) for i, n in hits[:max(0, int(limit))]], len(hits)


@dataclass(frozen=True)
class CostLine:
    id: int
    name: str
    quantity: int
    unit: int | None  # the lowest price listed, copper; None = none listed
    total: int | None  # unit × quantity


@dataclass(frozen=True)
class Cost:
    lines: list[CostLine]
    total: int  # the reagents with a price, summed
    missing: int  # how many reagents have none listed
    made: ItemPrice | None  # what one craft makes, priced each; None = it makes nothing (an enchant)
    makes_min: int
    makes_max: int


def recipe_cost(reg, recipe: RecipeDef) -> Cost | None:
    """A craft's cost to make at the lowest prices listed, reagent by reagent (a reagent named twice is one line), and
    what it makes priced each. None when there are no prices to show."""
    if not _shown(reg):
        return None
    qty: dict[int, int] = {}
    label: dict[int, str] = {}
    for x in recipe.reagents:
        i = int(x["id"])
        qty[i] = qty.get(i, 0) + int(x.get("quantity") or 1)
        label.setdefault(i, str(x["name"]))
    lines = []
    for i, q in qty.items():
        p = discord_auctions.price_of(reg, i)
        lines.append(CostLine(id=i, name=label[i], quantity=q, unit=p.low if p else None, total=p.low * q if p else None))
    m = recipe.makes
    made = _item(reg, int(m["id"]), str(m["name"])) if m else None
    lo = int(m.get("quantity") or 1) if m else 0
    hi = int(m.get("quantity_max") or lo) if m else 0
    return Cost(lines=lines, total=sum(c.total for c in lines if c.total is not None),
                missing=sum(1 for c in lines if c.unit is None), made=made, makes_min=lo, makes_max=hi)


# ------------------------------------------------------------------ the words


def _listed(p: ItemPrice) -> str:
    return f"lowest price {money(p.low)}" if p.low is not None else NONE_LISTED


def price_words(p: ItemPrice) -> str:
    """One item's price line: 'Arcanite Bar: lowest price 48g 99s 98c, 30 listed.' / 'Arcanite Bar: none listed.'"""
    if p.low is None:
        return f"{p.name}: {NONE_LISTED}."
    return f"{p.name}: {_listed(p)}, {p.qty} listed."


def cost_sentence(cost: Cost) -> str:
    """The cost to make in one line: the sum, 'at least' the sum and how many reagents have none listed, or not known."""
    if cost.lines and cost.missing >= len(cost.lines):
        return "Cost to make: not known — none of the reagents are listed."
    if cost.missing:
        many = "1 reagent has" if cost.missing == 1 else f"{cost.missing} reagents have"
        return f"Cost to make: at least {money(cost.total)} — {many} none listed."
    return f"Cost to make: {money(cost.total)}."


def made_words(cost: Cost) -> str:
    """What one craft makes and its own lowest price each: 'Makes 2 to 4 · lowest price 3g 20s each, 14 listed'; '' when
    it makes nothing."""
    p = cost.made
    if p is None:
        return ""
    count = str(cost.makes_min) if cost.makes_max <= cost.makes_min else f"{cost.makes_min} to {cost.makes_max}"
    if p.low is None:
        return f"Makes {count} · {NONE_LISTED}"
    return f"Makes {count} · {_listed(p)} each, {p.qty} listed"


def status_sentence(reg, owner: bool) -> str:
    """How recent the prices are, or why there are none: when off, the owner is told how to turn them on and everyone
    else reads nothing."""
    if discord_auctions.state(reg) == "off":
        return OWNER_OFF_WORDS if owner else ""
    return discord_auctions.as_of_words(reg)


__all__ = ["Cost", "CostLine", "ItemPrice", "NONE_LISTED", "OWNER_OFF_WORDS", "cost_sentence", "made_words", "names",
           "price_words", "recipe_cost", "remember", "reset_names", "search", "status_sentence", "unnamed"]
