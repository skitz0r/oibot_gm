"""Prices for people (auction prices, spec §3, §4.3–§4.5, C6, C9, C10): item names, the price search, a recipe's cost to
make and every sentence a person reads about a price. Pure: it reads the guild's price table (discord_auctions) and never
asks Blizzard — the routes, Discord, the help answer and the pages are thin callers of it.

Names (§4.3): what a recipe makes and its reagents, then the profile's loot items, then the names the hourly check looked
up for listed ids nothing local names (`out/auctions/items.json`, one block per game version, written whole). An unnamed
id is not searchable until it is named.

Every read is gated on discord_auctions.state: fresh or stale shows prices; off, waiting and old (a day, C10) show none.
Cost to make is per craft (C9): what the recipe makes shows its own lowest price "each", never combined with the cost.

The watch list and the price history (price watch §3, §4.2, C16): the member's own watch / unwatch sentences, the list's
rows, the history's sentences and the chart's data — read from Registry and price_history (the history reads BLOCK)."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import discord_auctions
from . import auctions
from . import price_history
from .auctions import NO_AUCTIONS_WORDS, Price, money
from .constants import fmt12
from .profiles import RecipeDef

log = logging.getLogger(__name__)

NONE_LISTED = "none listed"
OWNER_OFF_WORDS = "Pick the guild's auction house on the Config page to see prices."
OWNER_LINK_WORDS = "Link the server to its guild in the game on the Config page first, then pick its auction house to see prices."
NO_MATCH = "No item the bot knows has that name."


# ------------------------------------------------------------------ names


_LOCAL: dict[str, tuple[tuple, dict[int, str]]] = {}  # version → (the profile's tables it was built from, names)
_FETCHED: dict[str, dict[int, str]] | None = None  # version → {id: name} looked up from Blizzard (the file); None = unread
_GONE: dict[str, dict[int, float]] | None = None  # version → {id Blizzard has no item for: when it said so (epoch)}; None = unread
_KINDS: dict[str, dict[int, str]] | None = None  # version → {id: Blizzard's item class in words; "" = asked, none given}; None = unread
_LEVELS: dict[str, dict[int, tuple[int, int, int]]] | None = None  # version → {id: (item level, level needed, vendor price)}; None = unread
GONE_RETRY = timedelta(days=7)  # an id Blizzard couldn't name is asked again after this long (a patch may add the item)
_MERGED: dict[str, tuple[tuple, dict[int, str]]] = {}  # version → ((the local names, _GEN), local + fetched)
_GEN = 0  # moves on every change to the looked-up names
_FILE_LOCK = threading.Lock()


def reset_names() -> None:
    """Forget the names kept in memory (a restart; tests)."""
    global _FETCHED, _GEN, _GONE, _KINDS, _LEVELS
    with _FILE_LOCK:
        _GEN += 1
        _LOCAL.clear()
        _MERGED.clear()
        _GONE = None
        _FETCHED = None
        _KINDS = None
        _LEVELS = None
    _MOVERS.clear()  # the movers' answers were built from those names
    _BARGAINS.clear()
    _GAPS.clear()
    _PREP.clear()


def _items_path() -> Path:
    return discord_auctions.OUT / "items.json"


def _gone_path() -> Path:
    return discord_auctions.OUT / "items_gone.json"


def _kinds_path() -> Path:
    return discord_auctions.OUT / "items_kinds.json"


def _kinds_file() -> dict[str, dict[int, str]]:
    global _KINDS
    if _KINDS is None:
        got: dict[str, dict[int, str]] = {}
        try:
            d = json.loads(_kinds_path().read_text(encoding="utf-8"))
            got = {str(v): {int(k): str(c) for k, c in block.items()} for v, block in d.items()}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a damaged file of item classes is read from Blizzard again
            log.warning("item classes file unreadable; classes are looked up again")
        _KINDS = got
    return _KINDS


def kinds(reg) -> dict[int, str]:
    """Blizzard's item class per item id for the guild's game version ('Trade Goods', 'Armor', …; "" = the page gave
    none), read with the names (out/auctions/items_kinds.json). An id not in it was never asked. Read it, never change it."""
    return _kinds_file().get(reg.profile.name) or {}


def _levels_path() -> Path:
    return discord_auctions.OUT / "items_levels.json"


def _levels_file() -> dict[str, dict[int, tuple[int, int, int]]]:
    global _LEVELS
    if _LEVELS is None:
        got: dict[str, dict[int, tuple[int, int, int]]] = {}
        try:
            d = json.loads(_levels_path().read_text(encoding="utf-8"))
            got = {str(v): {int(k): (int(x[0]), int(x[1]), int(x[2])) for k, x in block.items()} for v, block in d.items()}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a damaged file is read from Blizzard again
            log.warning("item levels file unreadable; they are looked up again")
        _LEVELS = got
    return _LEVELS


def levels(reg) -> dict[int, tuple[int, int, int]]:
    """(item level, level needed, what a vendor pays in copper) per LISTED item the bot asked Blizzard about
    (out/auctions/items_levels.json; zeros = none given). The recipe list's items are in the profile instead
    (GameProfile.item_facts): `item_fact` reads both. Read it, never change it."""
    return _levels_file().get(reg.profile.name) or {}


def item_fact(reg, item_id: int) -> tuple[int, int, int] | None:
    """An item's (item level, level needed, vendor price): the profile's generated facts first, else what the hourly
    naming read; None = not known yet."""
    held = reg.profile.item_facts.get(item_id)
    return held[:3] if held is not None else levels(reg).get(item_id)


def unkinded(reg, listed: dict[int, Price], limit: int) -> list[int]:
    """Up to `limit` listed ids the bot names but whose class — or level and vendor price — was never asked, most
    listed first."""
    if limit <= 0:
        return []
    known, have, lv, held = names(reg), kinds(reg), levels(reg), reg.profile.item_facts
    ids = [i for i in listed if i in known and (i not in have or (i not in lv and i not in held))]
    ids.sort(key=lambda i: (-listed[i].qty, i))
    return ids[:limit]


def _gone() -> dict[str, dict[int, float]]:
    """The ids Blizzard has no item for, kept ACROSS restarts (out/auctions/items_gone.json): forgotten at a restart they
    sat first in the queue — most listed first — and were asked again every hour, so nothing new was ever named (found
    live 2026-11-14). An entry older than GONE_RETRY is dropped, so the id is asked once more."""
    global _GONE
    if _GONE is None:
        got: dict[str, dict[int, float]] = {}
        try:
            d = json.loads(_gone_path().read_text(encoding="utf-8"))
            cut = discord_auctions._now().timestamp() - GONE_RETRY.total_seconds()
            got = {str(v): {int(k): float(t) for k, t in block.items() if float(t) >= cut} for v, block in d.items()}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a damaged file only means those ids are asked once more
            log.warning("unnameable items file unreadable; those ids are asked again")
        _GONE = got
    return _GONE


def _write_whole(path: Path, text: str, what: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        log.exception("%s not saved", what)


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
    with _FILE_LOCK:
        gone = _gone().get(reg.profile.name) or {}
    ids = [i for i in listed if i not in known and i not in gone]
    ids.sort(key=lambda i: (-listed[i].qty, i))
    return ids[:limit]


def remember(reg, found: dict[int, str], gone: set[int], kinds: dict[int, str] | None = None,
             levels: dict[int, tuple[int, int, int]] | None = None) -> None:
    """Keep the names (and item classes) looked up for the guild's version and write each file whole (a unique temp
    name, then a rename). BLOCKS on the write: call it through asyncio.to_thread."""
    global _FETCHED, _GEN, _GONE, _KINDS, _LEVELS
    version = reg.profile.name
    with _FILE_LOCK:
        if levels:
            held_levels = _levels_file()
            block = dict(held_levels.get(version) or {})
            block.update({int(k): (int(x[0]), int(x[1]), int(x[2])) for k, x in levels.items()})
            _LEVELS = {**held_levels, version: block}
            _write_whole(_levels_path(), json.dumps({v: {str(k): list(x) for k, x in sorted(b.items())} for v, b in sorted(_LEVELS.items())},
                                                    separators=(",", ":")), "item levels")
        if kinds:
            held_kinds = _kinds_file()
            block = dict(held_kinds.get(version) or {})
            block.update({int(k): str(c or "") for k, c in kinds.items()})
            _KINDS = {**held_kinds, version: block}
            _write_whole(_kinds_path(), json.dumps({v: {str(k): c for k, c in sorted(b.items())} for v, b in sorted(_KINDS.items())},
                                                   separators=(",", ":"), ensure_ascii=False), "item classes")
        held = _gone()
        mine = dict(held.get(version) or {})
        before = dict(mine)
        stamp = discord_auctions._now().timestamp()
        for i in gone:
            mine.setdefault(int(i), stamp)
        for i in found:  # named after all (a retry): no longer unnameable
            mine.pop(int(i), None)
        if mine != before:
            _GONE = {**held, version: mine}
            _write_whole(_gone_path(), json.dumps({v: {str(k): t for k, t in sorted(b.items())} for v, b in sorted(_GONE.items())},
                                                  separators=(",", ":")), "unnameable items")
        if not found:
            return
        current = _fetched()
        block = dict(current.get(version) or {})
        block.update({int(k): str(v) for k, v in found.items() if v})
        _FETCHED = {**current, version: block}  # copy on write: readers on the loop never see a half-built dict
        _GEN += 1
        text = json.dumps({v: {str(k): n for k, n in sorted(b.items())} for v, b in sorted(_FETCHED.items())},
                          separators=(",", ":"), ensure_ascii=False)
        _write_whole(_items_path(), text, "item names")


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


def status_sentence(reg, owner: bool, *, stamp: bool = False) -> str:
    """How recent the prices are, or why there are none: when off, the owner is told how to turn them on — only where
    the picker exists (auction houses and the Blizzard keys: pick the house, or link the guild first) — and everyone
    else reads nothing. `stamp`: Discord's native relative time (discord_auctions.as_of_words)."""
    if discord_auctions.state(reg) == "off":
        if not owner or not auctions.available(reg):
            return ""
        return OWNER_OFF_WORDS if reg.config.armory is not None else OWNER_LINK_WORDS
    return discord_auctions.as_of_words(reg, stamp=stamp)


def more_words(n: int) -> str:
    """The last line when a search shows fewer items than match: 'And 3 more items match.'"""
    return "And 1 more item matches." if n == 1 else f"And {n} more items match."


def answer_lines(reg, shown: list[ItemPrice], total: int, owner: bool, *, stamp: bool = False) -> list[str]:
    """A price search in words (the MCP tool, Discord's /price): how recent the prices are, one line per item shown, then
    how many more match — or no match. Prices off: the status line alone (nothing for a member)."""
    status = status_sentence(reg, owner, stamp=stamp)
    if not _shown(reg):
        return [status] if status else []
    lines = [status, *(price_words(p) for p in shown)] if shown else [status, NO_MATCH]
    if total > len(shown) and shown:
        lines.append(more_words(total - len(shown)))
    return lines


PRICE_LINES = 10  # items Discord's /price lists for typed text; the rest are counted


def item_price(reg, item_id: int) -> ItemPrice | None:
    """One named item's price (a picked '#<id>'); None when the bot has no name for that id."""
    name = names(reg).get(int(item_id))
    return _item(reg, int(item_id), name) if name else None


def price_answer(reg, text: str, owner: bool, *, stamp: bool = True, limit: int = PRICE_LINES) -> list[str]:
    """Discord's /price in lines: '#<id>' (the autocomplete's value) = that item, typed text = the search's first `limit`
    and how many more. No prices to show (off, none read yet, a day old): that one sentence alone — a member reads
    why too, since they asked."""
    if not auctions.available(reg):  # no auction houses, or no keys to read them: no picker to point at
        return [NO_AUCTIONS_WORDS]
    s = discord_auctions.state(reg)
    if s == "off":
        return [status_sentence(reg, owner) or discord_auctions.OFF_WORDS]
    if s not in ("fresh", "stale"):
        return [discord_auctions.as_of_words(reg)]
    raw = str(text or "").strip()
    if raw.startswith("#") and raw[1:].isdigit():
        p = item_price(reg, int(raw[1:]))
        return [status_sentence(reg, owner, stamp=stamp), price_words(p) if p else NO_MATCH]
    shown, total = search(reg, raw, limit)
    return answer_lines(reg, shown, total, owner, stamp=stamp)


def cost_line(reg, recipe: RecipeDef | None) -> str:
    """A recipe's cost to make in one line, or "" when there is nothing to say: no recipe, no reagents (never a zero
    cost) or no prices to show."""
    if recipe is None or not recipe.reagents:
        return ""
    cost = recipe_cost(reg, recipe)
    return cost_sentence(cost) if cost is not None else ""


# ------------------------------------------------------------------ the help answer's section (C5)


HELP_WORDS = re.compile(r"\b(prices?|costs?|worth|how much|auctions?|cheapest)\b")
"""A question about prices: one of these as a word. The section also needs an item or a recipe the question names."""
HELP_ITEMS = 10  # items the question names, priced in the help snapshot
HELP_RECIPES = 3  # recipes the question names, with their cost to make
HELP_BUDGET = 1_950  # the section's cap in help.SECTIONS (2,000) less _clip's trailer room: whole blocks only, never a cut
HELP_HEAD = ("### Prices: the lowest price listed on the guild's auction house as of the line below — what the cheapest one is up "
             "for, not what an item is worth or will sell for; say 'lowest listed' and give the time · 'none listed' = nothing is "
             "up there right now, never a price of zero · cost to make = one craft's reagents at their lowest prices; 'at least' = "
             "some reagents have none listed · what a craft makes is priced each and never subtracted: never work out a profit · "
             "no price lines = say the line below and quote no price")

_HELP_IX: dict[str, tuple[dict, dict]] = {}  # version → (the names dict it was built from, first word → [(words, ids)])


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", str(text or "").casefold().replace("'", "").replace("’", ""))


def _help_index(reg) -> dict[str, list[tuple[tuple, list[int]]]]:
    named = names(reg)
    held = _HELP_IX.get(reg.profile.name)
    if held is not None and held[0] is named:
        return held[1]
    by_words: dict[tuple, list[int]] = {}
    for i, n in named.items():
        w = tuple(_tokens(n))
        if w and not (len(w) == 1 and len(w[0]) < 4):  # a short one-word name is too common a word
            by_words.setdefault(w, []).append(int(i))
    ix: dict[str, list] = {}
    for w, ids in by_words.items():
        ix.setdefault(w[0], []).append((w, sorted(ids)))
    _HELP_IX[reg.profile.name] = (named, ix)
    return ix


def _word_is(q: str, w: str, last: bool) -> bool:
    return q == w or (last and q in (w + "s", w + "es"))


def named_items(reg, question: str, limit: int = HELP_ITEMS) -> list[int]:
    """The ids of the items a sentence names by their whole name (case-folded words; the last word may be plural), the
    longest names first (a name inside a longer named one is not named again), in the sentence's order, a listed id
    before an unlisted one of the same name; at most `limit`."""
    qw = _tokens(question)
    ix = _help_index(reg)
    spans = []
    for i, q in enumerate(qw):
        keys = {q, q[:-1] if q.endswith("s") else q, q[:-2] if q.endswith("es") else q}
        for k in keys:
            for w, ids in ix.get(k, ()):
                end = i + len(w)
                if end <= len(qw) and all(_word_is(qw[i + j], w[j], j == len(w) - 1) for j in range(len(w))):
                    spans.append((i, end, ids))
    spans.sort(key=lambda s: (s[0] - s[1], s[0]))
    taken: list[tuple[int, int, list[int]]] = []
    for a, b, ids in spans:
        if all(b <= x or a >= y for x, y, _ids in taken):
            taken.append((a, b, ids))
    t = discord_auctions.table(reg)
    listed = t.prices if t else {}
    out: list[int] = []
    for _a, _b, ids in sorted(taken, key=lambda s: s[0]):
        out += [i for i in sorted(ids, key=lambda i: (i not in listed, i)) if i not in out]
    return out[:limit]


def named_recipes(reg, question: str, limit: int = HELP_RECIPES) -> list[RecipeDef]:
    """The recipes of the game's list a sentence names (professions' finders: its whole words; only when none, two
    neighbouring words of a name — looser, so never beside a whole name) that have reagents — a recipe with none has no
    cost to make; at most `limit`."""
    from . import professions

    q = question or ""
    hits = professions.search(reg, q, professions.HELP_HITS) or professions.phrase_hits(reg, q, professions.HELP_HITS)
    out: list[RecipeDef] = []
    for e in hits:
        if e.recipe is not None and e.recipe.reagents and e.recipe not in out:
            out.append(e.recipe)
    return out[:limit]


def help_hit(reg, question: str) -> bool:
    """The help answer's prices section: the version has auction houses, the owner picked one, the question asks about a
    price (HELP_WORDS) and names an item or a recipe. None read yet or a day old still hits: the section then says why."""
    if not reg.profile.auctions or not HELP_WORDS.search(str(question or "").casefold()):
        return False
    if discord_auctions.state(reg) == "off":
        return False
    return bool(named_items(reg, question) or named_recipes(reg, question))


def _safe(text) -> str:
    from .llm.provider import unfence

    return unfence(text, "member-text").replace("\n", " ").strip()


def _reagent_words(c: CostLine) -> str:
    head = f"  - {_safe(c.name)} × {c.quantity}: "
    if c.unit is None:
        return head + NONE_LISTED
    return head + f"lowest price {money(c.unit)} each, {money(c.total)} in all"


def help_section(ask) -> list[str]:
    """The help snapshot's prices section (C5; code computes, the model words it): what the numbers are, how recent they
    are, then up to HELP_ITEMS items the question names with their lowest price or none listed, then up to HELP_RECIPES
    named recipes with each reagent and the cost to make. None read yet or too old: the line saying why, and no price.
    Whole blocks while HELP_BUDGET allows, never a line cut. Item names may come from Blizzard: unfenced."""
    reg = ask.reg
    q = ask.question or ""
    out = [HELP_HEAD, discord_auctions.as_of_words(reg)]
    if not _shown(reg):
        return out
    used = len("\n".join(out))

    def fits(block: list[str]) -> bool:
        nonlocal used
        n = len("\n".join(block)) + 1
        if used + n > HELP_BUDGET:
            return False
        out.extend(block)
        used += n
        return True

    named = names(reg)
    for i in named_items(reg, q):
        if not fits([price_words(_item(reg, i, _safe(named.get(i, ""))))]):
            break
    for r in named_recipes(reg, q):
        cost = recipe_cost(reg, r)
        if cost is None:
            continue
        block = [f"{_safe(r.name)} (one craft):", *(_reagent_words(c) for c in cost.lines)]
        if made := made_words(cost):
            block.append("  " + made)
        block.append("  " + cost_sentence(cost))
        fits(block)
    return out


# ------------------------------------------------------------------ the owner's picker (the site and Discord)


def listings_words(n: int) -> str:
    """How much is up on one auction house: the total quantity listed, not a number of different items."""
    return f"{int(n)} listed in all"


def choices_words(choices: list) -> str:
    """Why the picker offers nothing, or "" when it has choices."""
    return "" if choices else discord_auctions.NO_CHOICES


def sample_rows(reg, choice) -> list[tuple[int, str, str]]:
    """A house's reference items as (id, name, lowest price in words), in the profile's order."""
    named = names(reg)
    return [(int(i), named.get(int(i)) or f"Item {int(i)}", money(low) if low is not None else NONE_LISTED) for i, low in choice.samples]


def sample_names(reg, choices: list) -> str:
    """The reference items' names in one phrase: 'Arcanite Bar, Dreamfoil and Runecloth'."""
    got = [n for _i, n, _w in sample_rows(reg, choices[0])] if choices else []
    if len(got) < 2:
        return "".join(got)
    return ", ".join(got[:-1]) + f" and {got[-1]}"


def choice_description(reg, choice) -> str:
    """One house in a select's description (at most 100 characters): how much is listed, then each reference item's
    lowest price in the order sample_names gives."""
    return " · ".join([listings_words(choice.listings), *(w for _i, _n, w in sample_rows(reg, choice))])[:100]


# ------------------------------------------------------------------ the watch list and the price history (price watch §3, §4.2)


SPANS: dict[str, tuple[int, str]] = {"1": (1, "24 hours"), "7": (7, "7 days"), "30": (30, "30 days"), "90": (90, "90 days"), "365": (365, "1 year")}
"""The chart's spans: the key a page sends → (days, the span in words)."""
UNNAMED = "An item the bot can't name yet"
EMPTY_LIST_SITE = "You aren't watching anything yet. Search for an item above and press Watch."
EMPTY_LIST_DISCORD = "You aren't watching anything yet. Use /price with an item and press Watch."
EMPTY_LIST_HIDDEN = "You aren't watching anything yet."  # prices hidden: nothing can be watched, so no search is pointed at
NO_HISTORY_YET = "The bot hasn't kept any prices for this auction house yet."
NOT_A_SPAN = "That isn't one of the spans."
NOT_READ = "not read"  # a chart point: from here the bot didn't read the auction house (a break in the line)
SHORT_HISTORY = timedelta(days=30)  # kept for less than this: the history says since when


def watch(reg, discord_id: int, item_id: int) -> str:
    """Put a named item on the member's own watch list → the sentence (also when it was already there). Refused, in this
    order: on a version with no auction houses; while prices don't show (C15: the status sentence that says why —
    discord_auctions.as_of_words); for an id the bot can't name (NO_MATCH); with Registry.watch's refusals."""
    from .registry import RegistryError

    if not reg.profile.auctions:
        raise RegistryError(NO_AUCTIONS_WORDS)
    if not _shown(reg):
        raise RegistryError(discord_auctions.as_of_words(reg))
    name = names(reg).get(int(item_id))
    if name is None:
        raise RegistryError(NO_MATCH)
    reg.watch(discord_id, int(item_id))
    return f"You're watching {name}."


def unwatch(reg, discord_id: int, item_id: int) -> str:
    """Take an item off the member's own watch list → the sentence (also when it wasn't there); never refused."""
    reg.unwatch(discord_id, int(item_id))
    name = names(reg).get(int(item_id))
    return f"You've stopped watching {name}." if name else "You've stopped watching that item."


def is_watching(reg, discord_id: int, item_id: int) -> bool:
    return int(item_id) in reg.watching(discord_id)


def watch_list(reg, discord_id: int) -> list[ItemPrice]:
    """The member's watch list in its order (newest first), each item priced while prices show, else its name alone
    (low None, qty 0, C16). An id the bot can't name reads UNNAMED."""
    named = names(reg)
    shown = _shown(reg)
    out = []
    for i in reg.watching(discord_id):
        name = named.get(int(i)) or UNNAMED
        out.append(_item(reg, i, name) if shown else ItemPrice(id=int(i), name=name, low=None, qty=0))
    return out


GUILD_WATCH_TOP = 50  # the guild's list shows this many items at most, the most watched first
GUILD_WATCH_EMPTY = "Nobody in the guild is watching anything yet."


WATCH_SHARED_WORDS = "Your items count on the guild's list."
WATCH_HIDDEN_WORDS = "Your items are hidden from the guild's list."


def share_watch(reg, discord_id: int, on: bool) -> str:
    """The member's own choice to count on the guild's list → the sentence (also when it was already so)."""
    reg.set_watch_shared(discord_id, bool(on))
    return WATCH_SHARED_WORDS if on else WATCH_HIDDEN_WORDS


def watchers_words(n: int) -> str:
    return "1 member watching" if n == 1 else f"{n} members watching"


def guild_watch(reg) -> list[tuple[ItemPrice, int]]:
    """What the guild watches (the owner, 2026-11-15) → (item, how many members watch it), the most watched first, then
    by name; at most GUILD_WATCH_TOP. A COUNT, never who: no member, name or id leaves here. Members in the server
    only (no test puppet, nobody who left); priced while prices show, else names alone, as watch_list."""
    counts: dict[int, int] = {}
    for m in reg.members.values():
        if m.test or getattr(m, "left_at", None) or m.watch_hidden:  # hidden = the member's own choice (Registry.set_watch_shared)
            continue
        for i in set(m.watching):
            counts[int(i)] = counts.get(int(i), 0) + 1
    named = names(reg)
    shown = _shown(reg)
    rows = [(named.get(i) or UNNAMED, i, n) for i, n in counts.items()]
    rows.sort(key=lambda r: (-r[2], r[0].casefold(), r[1]))
    return [(_item(reg, i, name) if shown else ItemPrice(id=i, name=name, low=None, qty=0), n) for name, i, n in rows[:GUILD_WATCH_TOP]]


def _span_sentence(s: price_history.Span, words: str) -> str:
    """'Lowest in the last 7 days: 12s 40c · highest: 19s 2c' / 'None listed in the last 7 days.'"""
    if s.low is None:
        return f"None listed in the last {words}."
    return f"Lowest in the last {words}: {money(s.low)} · highest: {money(s.high)}"


def _day_words(reg, t: datetime) -> str:
    return reg.local12(t, "%a %d %b")


def history_sentences(reg, item_id: int, *, now: datetime | None = None) -> list[str]:
    """An item's history in words (BLOCKING: reads the history file): the 7-day sentence, the 30-day sentence, and since
    when the bot has kept prices when that is less than 30 days. [] while prices don't show (C16); [NO_HISTORY_YET] when
    the house has nothing kept, or its file can't be read."""
    key = discord_auctions.history_key(reg)
    if key is None or not _shown(reg):
        return []
    now = now or discord_auctions._now()
    try:
        since = price_history.kept_since(key)
        if since is None:
            return [NO_HISTORY_YET]
        lines = [_span_sentence(price_history.span(key, item_id, SPANS[k][0], now), SPANS[k][1]) for k in ("7", "30")]
    except price_history.HistoryError:
        log.warning("price history unreadable for %s", key, exc_info=True)
        return [NO_HISTORY_YET]
    if now - since < SHORT_HISTORY:
        lines.append(f"The bot has kept prices since {_day_words(reg, since)}.")
    return lines


def listed_words(qty: int) -> str:
    """How many are listed, in words: '1,240 listed'."""
    return f"{int(qty):,} listed"


def _point_json(reg, p: price_history.Point) -> dict:
    if p.unread:  # the bot didn't read the house from here: a break, read "not read"
        return {"t": int(p.at.timestamp()), "when": reg.local12(p.at), "low": None, "high": None, "words": NOT_READ, "unread": True,
                "qty": None, "listed_words": ""}
    # a folded day is one UTC day (price_history's fold): read as that day alone, never shifted into the guild's zone
    when = fmt12(p.at.astimezone(timezone.utc), "%a %d %b") if p.qty is None else reg.local12(p.at)
    # how many were listed from this point (2026-11-15: the chart's bars under the line) — the feed has no sales, so this
    # is supply, never trades; None on a folded day (the fold keeps no quantity), with nothing listed, and unread
    qty = int(p.qty) if p.qty is not None and p.low is not None else None
    return {"t": int(p.at.timestamp()), "when": when, "low": p.low, "high": p.high,
            "words": money(p.low) if p.low is not None else NONE_LISTED, "unread": False,
            "qty": qty, "listed_words": listed_words(qty) if qty is not None else ""}


def chart(reg, item_id: int, span: str, *, now: datetime | None = None) -> dict:
    """The chart's data for one item over one of SPANS (BLOCKING: reads the history file): its points (epoch seconds,
    the time in the Clock rule's words, lowest / highest in copper, the lowest in words, `qty` / `listed_words` = how many were listed from then (None / "" on a folded day, with nothing listed, unread); low None = nothing listed from
    then: a gap; `unread` = the bot didn't read the house from then: a gap whose words are NOT_READ), the span's lowest and highest, and `since` / `until` = the span's start and the server's now in epoch
    seconds (the chart's x axis: the page reads no clock of its own). No points while prices don't show, without history, or for a file
    that can't be read. An unknown span is refused."""
    from .registry import RegistryError

    if str(span) not in SPANS:
        raise RegistryError(NOT_A_SPAN)
    span = str(span)
    now = now or discord_auctions._now()
    days = SPANS[span][0]
    out = {"span": span, "points": [], "low": None, "high": None, "low_words": NONE_LISTED, "high_words": NONE_LISTED,
           "since": int((now - timedelta(days=days)).timestamp()), "until": int(now.timestamp())}
    key = discord_auctions.history_key(reg)
    if key is None or not _shown(reg):
        return out
    try:
        pts = price_history.points(key, item_id, now - timedelta(days=days), now)
        s = price_history.span(key, item_id, days, now)
        kept = price_history.kept_since(key)
    except price_history.HistoryError:
        log.warning("price history unreadable for %s", key, exc_info=True)
        return out
    if kept is not None and int(kept.timestamp()) > out["since"]:
        # a history younger than the span: the x axis starts where the history does, so the line fills the chart
        # instead of sitting in its last sliver (the "kept since" sentence says why)
        out["since"] = min(int(kept.timestamp()), out["until"])
    out["points"] = [_point_json(reg, p) for p in pts]
    out.update(low=s.low, high=s.high, low_words=money(s.low) if s.low is not None else NONE_LISTED,
               high_words=money(s.high) if s.high is not None else NONE_LISTED)
    return out


SPARK_SPAN = "1"  # the watch list's small line: the last 24 hours


def _change_words(pct: int) -> str:
    return f"{'+' if pct > 0 else '−' if pct < 0 else ''}{abs(pct)}%"


def watch_groups(reg) -> tuple[list[dict], object]:
    """The watch list's item groups — Movers' own (All, the profile's by Blizzard's item class, Other) — and item id →
    its group id."""
    rows, _reagents, of = _grouper(reg)
    return rows, of


def spark(reg, item_id: int, *, now: datetime | None = None, window: str | None = None) -> dict | None:
    """A watched item's small line for its row in the list (BLOCKING: reads the history file): the last 24 hours
    (SPARK_SPAN; less while the history is younger) as {since, until, points [[epoch seconds, lowest in copper | None =
    nothing listed or not read from then]], change_words, up}. `change_words` = the price now against the first price of
    the stretch ('+12%' / '−8%' / '0%'; "" when nothing is listed now), `up` = True risen / False fallen / None neither —
    the line's colour. None when there is nothing to draw: prices don't show, no history, or nothing listed all day."""
    if window is None or str(window) == "24":
        c = chart(reg, item_id, SPARK_SPAN, now=now)
    else:  # the list's time choice (2026-11-15): Movers' windows — the same line over 6 hours, 12 hours or 7 days
        from .registry import RegistryError

        if str(window) not in WINDOWS:
            raise RegistryError(NOT_A_WINDOW)
        now = now or discord_auctions._now()
        start = now - WINDOWS[str(window)][0]
        c = {"since": int(start.timestamp()), "until": int(now.timestamp()), "points": []}
        key = discord_auctions.history_key(reg)
        if key is not None and _shown(reg):
            try:
                pts = price_history.points(key, item_id, start, now)
                kept = price_history.kept_since(key)
            except price_history.HistoryError:
                log.warning("price history unreadable for %s", key, exc_info=True)
                pts, kept = [], None
            if kept is not None and int(kept.timestamp()) > c["since"]:
                c["since"] = min(int(kept.timestamp()), c["until"])
            c["points"] = [{"t": int(p.at.timestamp()), "low": p.low} for p in pts]
    lows = [p["low"] for p in c["points"] if p["low"] is not None]
    if not lows:
        return None
    last = c["points"][-1]["low"]
    pct = round((last - lows[0]) * 100 / lows[0]) if last is not None and lows[0] > 0 else None
    return {"since": c["since"], "until": c["until"], "points": [[p["t"], p["low"]] for p in c["points"]],
            "change_words": _change_words(pct) if pct is not None else "", "up": None if not pct else pct > 0}


# ------------------------------------------------------------------ movers (2026-11-14)

WINDOWS: dict[str, tuple[timedelta, str]] = {"6": (timedelta(hours=6), "6 hours"), "12": (timedelta(hours=12), "12 hours"),
                                             "24": (timedelta(hours=24), "24 hours"), "168": (timedelta(days=7), "7 days")}
NOT_A_WINDOW = "That isn't one of the windows."
MOVE_MIN_PCT = 10  # a move smaller than this against the usual price is no mover
MOVE_MIN_PRICE = 100  # copper: an item under one silver — usually or now — is no mover (a few copper tripling is noise)
MOVERS_TOP = 15  # rows a list shows at first, and more with each "Show more"
MOVE_STEPS = (10, 25, 50, 100)  # the card's "moved at least" choices, in percent; the first is MOVE_MIN_PCT
MOVER_SORTS = (("pct", "By percent"), ("gold", "By gold"))
ALL_GROUP = {"id": "all", "name": "All"}
OTHER_GROUP = {"id": "other", "name": "Other"}
MOVERS_NONE = "Nothing in this group has moved that much. Pick another group or a smaller move."
# Climbing / Sliding (the owner, 2026-11-15): a price that went ONE way step after step over the window.
# window → (how many steps, the step in words)
TREND_STEPS: dict[str, tuple[int, str]] = {"6": (6, "every hour"), "12": (6, "every 2 hours"), "24": (6, "every 4 hours"),
                                           "168": (7, "every day")}
TREND_FLAT = 1  # steps that may stand still; a step the other way is never allowed


def _trend_way(t) -> int:
    """+1 = its price rose step after step and stands no lower now, −1 = it fell and stands no higher, 0 = neither.
    At most TREND_FLAT steps stood still."""
    diffs = [b - a for a, b in zip(t.values, t.values[1:])]
    if sum(1 for d in diffs if d == 0) > TREND_FLAT:
        return 0
    if all(d >= 0 for d in diffs) and t.now >= t.values[-1]:
        return 1
    if all(d <= 0 for d in diffs) and t.now <= t.values[-1]:
        return -1
    return 0


def _groups(reg) -> tuple[list[dict], dict[str, str], str | None]:
    """The profile's item groups for Movers (integrations.yaml `auctions.groups`: {id, name, classes — Blizzard's item
    classes —, reagents: true on the one a recipe's reagent of no grouped class falls into}) → (the card's choices: All,
    the profile's, Other; class → group id; the reagents' group id)."""
    rows = list((reg.profile.auctions or {}).get("groups") or [])
    by_class = {str(c): str(g["id"]) for g in rows for c in (g.get("classes") or [])}
    reagents = next((str(g["id"]) for g in rows if g.get("reagents")), None)
    return [ALL_GROUP, *({"id": str(g["id"]), "name": str(g["name"])} for g in rows), OTHER_GROUP], by_class, reagents


def _diff_words(diff: int) -> str:
    return f"{'+' if diff > 0 else '−'}{money(abs(diff))}"
USED_IN_NAMES = 3  # recipes named in the "Used in …" sentence; the rest are counted

_USES: dict[str, dict[int, list[str]]] = {}  # profile name → {item id: the names of the recipes it is a reagent of}


def _uses(reg) -> dict[int, list[str]]:
    name = reg.profile.name
    if name not in _USES:
        idx: dict[int, set[str]] = {}
        for r in reg.profile.recipes.values():
            for x in r.reagents:
                idx.setdefault(int(x["id"]), set()).add(r.name)
        _USES[name] = {i: sorted(v, key=str.casefold) for i, v in idx.items()}
    return _USES[name]


def used_in_words(reg, item_id: int) -> str:
    """'Used in A, B, C and 4 more.' — the recipes of the game's list the item is a reagent of; "" when none."""
    names = _uses(reg).get(int(item_id)) or []
    if not names:
        return ""
    shown, rest = names[:USED_IN_NAMES], len(names) - USED_IN_NAMES
    return f"Used in {', '.join(shown)}{f' and {rest} more' if rest > 0 else ''}."


def _uses_count_words(n: int) -> str:
    return "" if n <= 0 else "Used in 1 recipe" if n == 1 else f"Used in {n} recipes"


def _duration_words(span: timedelta) -> str:
    """'2 hours 40 minutes' / '1 hour' / '3 days 4 hours': the two largest parts, whole minutes."""
    minutes = max(1, int(span.total_seconds() // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{n} {u}{'' if n == 1 else 's'}" for n, u in ((days, "day"), (hours, "hour"), (mins, "minute")) if n]
    return " ".join(parts[:2])


def _pct(usual: int, now: int) -> int:
    return round((now - usual) * 100 / usual)


_MOVERS: dict[tuple, tuple[tuple, dict]] = {}  # (guild key, window) → (what it was computed from, the answer)


def _grouper(reg):
    """→ (the card's groups, the reagents' group id, item id → its group id): Blizzard's item class through the
    profile's groups, else — for a recipe's reagent — the reagents' group, else Other."""
    rows, by_class, reagents = _groups(reg)
    classes, uses = kinds(reg), _uses(reg)

    def of(item: int) -> str:
        return by_class.get(classes.get(item) or "") or ((reagents if uses.get(item) else None) or OTHER_GROUP["id"])

    return rows, reagents, of


def movers(reg, window: str, *, now: datetime | None = None) -> dict:
    """`_movers`, answered once per price table: an open page asks again on every live event, and the answer only
    changes with a new table, a newly named item or another auction house."""
    if now is not None:
        return _movers(reg, window, now=now)
    t = discord_auctions.table(reg)
    stamp = (discord_auctions.history_key(reg), t.at if t else None, _shown(reg), len(names(reg)), len(kinds(reg)))
    hit = _MOVERS.get((reg.key, str(window)))
    if hit is not None and hit[0] == stamp:
        return hit[1]
    out = _movers(reg, window)
    if out["ready"]:  # a history still too short is asked again: it fills in with the next read
        _MOVERS[(reg.key, str(window))] = (stamp, out)
    return out


def _movers(reg, window: str, *, now: datetime | None = None) -> dict:
    """The items whose lowest price NOW is at least MOVE_MIN_PCT % away from their USUAL price over one of WINDOWS
    (price_history.moves: the middle value over the window, at least MOVE_MIN_QTY listed now, listed for half the
    window), both prices at least MOVE_MIN_PRICE (`floor_words` says the two floors) — BLOCKING: one read of the history file. → {window, windows [{id, label}], ready, words, dips, rising}:
    the two lists hold EVERY such item, the biggest move in percent first, as {id, name, usual_words, now_words,
    change_words ('−40%' / '+50%'), pct, diff (copper, signed) and diff_words ('−20g' / '+2g 50s'), qty, uses (how many
    recipes it is a reagent of), uses_words, group}. The page filters and sorts them (2026-11-15): `groups` (All, the
    profile's item groups by Blizzard's item class, Other — an item whose class isn't read yet is Other unless a recipe
    uses it) with `group` the one shown first, `sorts` (by percent, by gold), `steps` (moved at least …%), `page` (rows
    shown at first and per Show more), `none_words` (the sentence for a choice that leaves a list empty). Only items the
    bot can name. A history younger than the window is ranked over what is kept (from two hours on) and `words` says over how
    long; `ready` False with one sentence in `words` when less than that is kept; an empty list has its own sentence
    (`dips_words` / `rising_words`). Prices not shown, or a file that can't be read: not ready, no words. An unknown window is
    refused."""
    from .registry import RegistryError

    if str(window) not in WINDOWS:
        raise RegistryError(NOT_A_WINDOW)
    window = str(window)
    span, label = WINDOWS[window]
    out = {"window": window, "windows": [{"id": k, "label": v[1]} for k, v in WINDOWS.items()], "ready": False, "words": "",
           "dips": [], "rising": [], "dips_words": "", "rising_words": "",
           "climbing": [], "sliding": [], "climbing_words": "", "sliding_words": "", "trend_words": "", "groups": _groups(reg)[0], "group": ALL_GROUP["id"],
           "sorts": [{"id": k, "label": v} for k, v in MOVER_SORTS], "steps": [{"id": str(s), "label": f"{s}%"} for s in MOVE_STEPS],
           "page": MOVERS_TOP, "none_words": MOVERS_NONE,
           "floor_words": f"at least {price_history.MOVE_MIN_QTY} listed, from {money(MOVE_MIN_PRICE)}"}
    key = discord_auctions.history_key(reg)
    if key is None or not _shown(reg):
        return out
    now = now or discord_auctions._now()
    try:
        found = price_history.moves(key, span, now)
        trend = price_history.trends(key, span, TREND_STEPS[window][0], now) if found is not None else None
        start = price_history.moves_since(key, span, now) if found is not None else None
        since = price_history.kept_since(key) if found is None else None
    except price_history.HistoryError:
        log.warning("price history unreadable for %s", key, exc_info=True)
        return out
    if found is None or start is None:
        need = min(span, price_history.MOVES_MIN_HISTORY)
        out["words"] = (f"The bot hasn't kept prices for {_duration_words(need)} yet. It started {reg.local12(since)}."
                        if since is not None else NO_HISTORY_YET)
        return out
    if start - (now - span) > timedelta(minutes=1):  # a history younger than the window: ranked over what is kept, and said so
        label = _duration_words(now - start)
        out["words"] = f"Over the last {label}, all the bot has kept so far."
    known, uses = names(reg), _uses(reg)
    out["groups"], _reagents, group_of = _grouper(reg)
    out["group"] = out["groups"][1]["id"] if len(out["groups"]) > 2 else ALL_GROUP["id"]
    rows = []
    for m in found:
        pct = _pct(m.usual, m.now) if m.usual > 0 else 0
        if abs(pct) < MOVE_MIN_PCT or m.item not in known or min(m.usual, m.now) < MOVE_MIN_PRICE:
            continue
        n = len(uses.get(m.item) or [])
        group = group_of(m.item)
        rows.append((pct, {"id": m.item, "name": known[m.item], "usual_words": money(m.usual), "now_words": money(m.now),
                           "change_words": _change_words(pct), "pct": pct, "diff": m.now - m.usual,
                           "diff_words": _diff_words(m.now - m.usual), "qty": m.qty, "uses": n,
                           "uses_words": _uses_count_words(n), "group": group}))
    out["ready"] = True
    out["dips"] = [r for _p, r in sorted((x for x in rows if x[0] < 0), key=lambda x: (x[0], x[1]["name"].casefold()))]
    out["rising"] = [r for _p, r in sorted((x for x in rows if x[0] > 0), key=lambda x: (-x[0], x[1]["name"].casefold()))]
    if not out["dips"]:
        out["dips_words"] = f"Nothing has dipped {MOVE_MIN_PCT}% or more in the last {label}."
    if not out["rising"]:
        out["rising_words"] = f"Nothing has risen {MOVE_MIN_PCT}% or more in the last {label}."
    # Climbing / Sliding: the same rows, measured from the price in the first step to the price now
    steps, step_words = TREND_STEPS[window]
    if trend is None:
        need = _duration_words(span / steps * price_history.TREND_MIN_STEPS)
        out["climbing_words"] = out["sliding_words"] = f"The bot needs {need} of prices to see a steady move here."
        return out
    items, kept = trend
    over = label if kept == steps else _duration_words(span / steps * kept)
    out["trend_words"] = (f"Its price went one way {step_words} over the last {over}, and one step may stand still. "
                          "The figures are the price then and the price now.")
    steady = []
    for t in items:
        way = _trend_way(t)
        first = t.values[0]
        pct = _pct(first, t.now) if first > 0 else 0
        if not way or pct * way < MOVE_MIN_PCT or t.item not in known or min(first, t.now) < MOVE_MIN_PRICE:
            continue
        n = len(uses.get(t.item) or [])
        steady.append((pct, {"id": t.item, "name": known[t.item], "usual_words": money(first), "now_words": money(t.now),
                             "change_words": _change_words(pct), "pct": pct, "diff": t.now - first,
                             "diff_words": _diff_words(t.now - first), "qty": t.qty, "uses": n,
                             "uses_words": _uses_count_words(n), "group": group_of(t.item)}))
    out["sliding"] = [r for _p, r in sorted((x for x in steady if x[0] < 0), key=lambda x: (x[0], x[1]["name"].casefold()))]
    out["climbing"] = [r for _p, r in sorted((x for x in steady if x[0] > 0), key=lambda x: (-x[0], x[1]["name"].casefold()))]
    if not out["climbing"]:
        out["climbing_words"] = f"Nothing has climbed {step_words} over the last {over}."
    if not out["sliding"]:
        out["sliding_words"] = f"Nothing has slid {step_words} over the last {over}."
    return out


# ------------------------------------------------------------------ bargains (2026-11-15)

BARGAIN_MIN_REF = 100  # copper: a material under one silver in the reference economy is not compared
BARGAIN_MIN_SOLD = 1.0  # … nor one that sells less than once a day there (its price there is one seller's)
BARGAIN_MIN_GAP = 2.0  # a bargain is at least this many times cheaper than most materials, against its reference price
BARGAIN_MIN_ITEMS = 20  # fewer materials compared than this is no "most"
BARGAIN_STALE = timedelta(days=7)  # a reference copy older than this is not compared
BARGAINS_HINT = ("Materials that cost far less here than most do, measured against their {name} price. "
                 "Things to look into, not advice: a new economy floods some materials.")
BARGAINS_NONE = "No material is at least twice as cheap as most right now."

_BARGAINS: dict[str, tuple[tuple, dict]] = {}  # guild key → (what it was computed from, the answer)
_GAPS: dict[str, dict[int, float]] = {}  # guild key → {material: how many times under the typical ratio} of the last ready answer


def _times_words(gap: float) -> str:
    return f"{gap:.0f}×" if gap >= 10 else f"{gap:.1f}×"


def bargains(reg, *, now: datetime | None = None) -> dict:
    """The materials whose lowest price here is far below where most materials sit against a REFERENCE economy's price
    (integrations.yaml `auctions.reference`: for forever, TradeSkillMaster's Classic region market value, read daily —
    price_reference) — BLOCKING on the first read of the kept file. Each compared material's ratio = its lowest price
    here ÷ its reference price; `typical` = the middle ratio (a new economy holds less coin, so everything is cheaper:
    only the gap to the typical ratio says anything); a bargain is ≥ BARGAIN_MIN_GAP times under it. Compared: the
    materials group (the profile's `reagents: true` group), named, ≥ MOVE_MIN_QTY listed here, a reference price ≥
    BARGAIN_MIN_REF selling ≥ BARGAIN_MIN_SOLD a day there. → {ready, words, hint_words, typical_words, source_words,
    rows [{id, name, ref_words, now_words, gap, gap_words ('17× cheaper than most'), qty, uses, uses_words}] — the
    widest gap first, every one —, page, none_words}. Not ready with one sentence in `words` when the file isn't read
    yet, is older than BARGAIN_STALE or too few materials compare; no words while prices are hidden or the version has
    no reference. Answered once per price table and reference copy."""
    import statistics

    from . import price_reference

    ref_cfg = (reg.profile.auctions or {}).get("reference") or {}
    name = str(ref_cfg.get("name") or "")
    out = {"ready": False, "words": "", "hint_words": BARGAINS_HINT.format(name=name) if name else "", "typical_words": "",
           "source_words": "", "rows": [], "page": MOVERS_TOP, "none_words": BARGAINS_NONE}
    dest = discord_auctions.reference_path(reg)
    t = discord_auctions.table(reg)
    if dest is None or t is None or not _shown(reg):
        return out
    ref = price_reference.load(dest)
    now = now or discord_auctions._now()
    if ref is None:
        out["words"] = f"The bot hasn't read {name} prices yet. It reads them once a day."
        return out
    if now - ref.read_at > BARGAIN_STALE:
        out["words"] = f"The bot's {name} prices are too old to compare. It last read them {reg.local12(ref.read_at)}."
        return out
    stamp = (t.at, ref.read_at, len(names(reg)), len(kinds(reg)))
    hit = _BARGAINS.get(reg.key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    known, uses = names(reg), _uses(reg)
    _rows, materials, group_of = _grouper(reg)
    compared = []
    for item, p in t.prices.items():
        there = ref.values.get(item)
        if (there is None or item not in known or p.qty < price_history.MOVE_MIN_QTY or p.low <= 0
                or there[0] < BARGAIN_MIN_REF or there[1] < BARGAIN_MIN_SOLD or materials is None or group_of(item) != materials):
            continue
        compared.append((p.low / there[0], item, p, there[0]))
    if len(compared) < BARGAIN_MIN_ITEMS:
        out["words"] = f"Too few materials are listed here to compare with {name} yet."
        return out
    typical = statistics.median(c[0] for c in compared)
    _GAPS[reg.key] = {item: typical / ratio for ratio, item, _p, _there in compared}
    rows = []
    for ratio, item, p, there in compared:
        gap = typical / ratio
        if gap < BARGAIN_MIN_GAP:
            continue
        n = len(uses.get(item) or [])
        rows.append({"id": item, "name": known[item], "ref_words": money(there), "now_words": money(p.low), "gap": round(gap, 2),
                     "gap_words": f"{_times_words(gap)} cheaper than most", "qty": p.qty, "uses": n, "uses_words": _uses_count_words(n)})
    rows.sort(key=lambda r: (-r["gap"], r["name"].casefold()))
    pct = max(1, round(typical * 100))
    out.update(ready=True, rows=rows,
               typical_words=f"Most of the {len(compared)} materials compared cost about {pct}% of their {name} price here.",
               source_words=f"{name} prices are {ref_cfg.get('source') or 'the reference file'}'s, read {reg.local12(ref.read_at)}.")
    _BARGAINS[reg.key] = (stamp, out)
    return out


# ------------------------------------------------------------------ prep (2026-11-15)

PREP_HINT = ("Materials from levelling zones that end up in level-{level} consumables: plentiful while people level, "
             "wanted once they raid. Things to look into, not advice.")
PREP_NONE = "No listed material from a levelling zone goes into a high-level consumable right now."
PREP_NAMES = 2  # consumables named in "Goes into …"; the rest are counted
BELOW_HINT = "Listed for less than a vendor pays. Only the cheapest listing is known to be that low."
BELOW_NONE = "Nothing the bot knows a vendor price for is listed below it right now."

_PREP: dict[str, tuple[tuple, dict]] = {}  # guild key → (what it was computed from, the answer)


def _goes_into_words(ends: list[str]) -> str:
    n = len(ends)
    shown, rest = ends[:PREP_NAMES], n - PREP_NAMES
    return (f"Goes into {n} consumable{'' if n == 1 else 's'}: {', '.join(shown)}"
            f"{f' and {rest} more' if rest > 0 else ''}.")


def prep(reg) -> dict:
    """The Prep view (the owner's experiment, 2026-11-15) — BLOCKING like `bargains`, which it reads for the gap:
    `rows` = the LEVELLING-ZONE materials that end up in high-level consumables: the materials group, named, ≥
    MOVE_MIN_QTY listed, an item level inside the profile's `auctions.prep.material_levels`, not one a vendor sells
    (`vendor_sold`), and — directly or through what is crafted from it — a reagent of a recipe whose product is of an
    `end_classes` class with an item level (or level needed) ≥ `end_level`. No list of "the right" consumables: the
    recipe list and Blizzard's item levels decide. Each row {id, name, now_words, qty, ends, uses_words ('Goes into 6
    consumables: A, B and 4 more.'), vendor_words ('A vendor pays 10c.' | ""), gap, gap_words (Bargains' '24× cheaper
    than most' | "")}; the widest gap first, then the most consumables. `below_rows` = every listed item priced under
    what a vendor pays ({id, name, now_words, vendor_words, profit_words '+16c each', qty}; the largest share first).
    → {ready, hint_words ("" = the version has no Prep), rows, none_words, below_rows, below_words, below_none, page}.
    Not ready (no sentence) while prices are hidden. Answered once per price table and set of item facts."""
    cfg = (reg.profile.auctions or {}).get("prep") or {}
    out = {"ready": False, "hint_words": PREP_HINT.format(level=int(cfg["end_level"])) if cfg else "", "rows": [], "none_words": PREP_NONE,
           "below_rows": [], "below_words": BELOW_HINT, "below_none": BELOW_NONE, "page": MOVERS_TOP}
    if not cfg:
        return out
    t = discord_auctions.table(reg)
    if t is None or not _shown(reg):
        return out
    ready_bargains = bargains(reg)["ready"]
    gaps = _GAPS.get(reg.key) if ready_bargains else None
    stamp = (t.at, len(names(reg)), len(kinds(reg)), len(levels(reg)), id(gaps))
    hit = _PREP.get(reg.key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    known, classes = names(reg), kinds(reg)
    _rows, materials, group_of = _grouper(reg)
    lo, hi = (int(x) for x in cfg["material_levels"])
    end_level, end_classes = int(cfg["end_level"]), {str(c) for c in cfg.get("end_classes") or []}
    vendor_sold = {int(i) for i in (cfg.get("vendor_sold") or {})}
    used_by: dict[int, set[int]] = {}
    for r in reg.profile.recipes.values():
        if r.makes:
            for x in r.reagents:
                used_by.setdefault(int(x["id"]), set()).add(int(r.makes["id"]))

    def is_end(item: int) -> bool:
        held = reg.profile.item_facts.get(item)
        cls = held[3] if held is not None else (classes.get(item) or "")
        f = item_fact(reg, item)
        return cls in end_classes and f is not None and max(f[0], f[1]) >= end_level

    memo: dict[int, frozenset] = {}

    def reach(item: int, seen: tuple = ()) -> frozenset:
        if item in memo:
            return memo[item]
        got: set[int] = set()
        for p in used_by.get(item, ()):
            if p in seen:
                continue
            if is_end(p):
                got.add(p)
            got |= reach(p, (*seen, item))
        memo[item] = frozenset(got)  # the recipe graph has no loops worth the care: a cut branch only loses a product
        return memo[item]

    rows, below = [], []
    for item, p in t.prices.items():
        if item not in known or p.low <= 0:
            continue
        f = item_fact(reg, item)
        if f is not None and f[2] > p.low:
            below.append(((f[2] - p.low) / p.low, {"id": item, "name": known[item], "now_words": money(p.low), "vendor_words": money(f[2]),
                                                   "profit_words": f"+{money(f[2] - p.low)} each", "qty": p.qty}))
        if (f is None or item not in used_by or item in vendor_sold or p.qty < price_history.MOVE_MIN_QTY or not lo <= f[0] <= hi
                or materials is None or group_of(item) != materials):
            continue
        ends = sorted((known.get(e) or UNNAMED for e in reach(item)), key=str.casefold)
        if not ends:
            continue
        gap = (gaps or {}).get(item)
        rows.append({"id": item, "name": known[item], "now_words": money(p.low), "qty": p.qty, "ends": len(ends),
                     "uses_words": _goes_into_words(ends), "vendor_words": f"A vendor pays {money(f[2])}." if f[2] else "",
                     "gap": round(gap, 2) if gap else None,
                     "gap_words": f"{_times_words(gap)} cheaper than most" if gap and gap >= BARGAIN_MIN_GAP else ""})
    rows.sort(key=lambda r: (-(r["gap"] or 0), -r["ends"], r["name"].casefold()))
    below.sort(key=lambda x: (-x[0], x[1]["name"].casefold()))
    out.update(ready=True, rows=rows, below_rows=[r for _s, r in below])
    _PREP[reg.key] = (stamp, out)
    return out


__all__ = ["MOVERS_TOP", "prep", "PREP_NONE", "BELOW_NONE", "levels", "item_fact", "bargains", "BARGAINS_NONE", "BARGAIN_MIN_GAP", "MOVE_STEPS", "MOVERS_NONE", "kinds", "unkinded", "MOVE_MIN_PCT", "NOT_A_WINDOW", "WINDOWS", "movers", "used_in_words", "EMPTY_LIST_DISCORD", "SPARK_SPAN", "spark", "EMPTY_LIST_HIDDEN", "EMPTY_LIST_SITE", "NOT_A_SPAN", "NOT_READ", "NO_HISTORY_YET", "SPANS", "UNNAMED", "chart", "history_sentences",
           "is_watching", "unwatch", "watch", "watch_list", "Cost", "CostLine", "HELP_ITEMS", "HELP_RECIPES", "HELP_WORDS", "ItemPrice", "NONE_LISTED", "NO_MATCH", "OWNER_LINK_WORDS", "OWNER_OFF_WORDS", "PRICE_LINES", "answer_lines", "choice_description",
           "choices_words", "cost_line", "help_hit", "help_section", "named_items", "named_recipes","item_price", "listings_words", "price_answer", "sample_names", "sample_rows", "more_words", "cost_sentence", "made_words", "names",
           "price_words", "watch_groups", "guild_watch", "share_watch", "WATCH_SHARED_WORDS", "WATCH_HIDDEN_WORDS", "watchers_words", "GUILD_WATCH_EMPTY", "GUILD_WATCH_TOP", "recipe_cost", "remember", "reset_names", "search", "status_sentence", "unnamed"]
