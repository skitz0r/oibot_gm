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


SPANS: dict[str, tuple[int, str]] = {"7": (7, "7 days"), "30": (30, "30 days"), "90": (90, "90 days"), "365": (365, "1 year")}
"""The chart's spans: the key a page sends → (days, the span in words)."""
UNNAMED = "An item the bot can't name yet"
EMPTY_LIST_SITE = "You aren't watching anything yet. Search for an item above and press Watch."
EMPTY_LIST_DISCORD = "You aren't watching anything yet. Use /price with an item and press Watch."
EMPTY_LIST_HIDDEN = "You aren't watching anything yet."  # prices hidden: nothing can be watched, so no search is pointed at
NO_HISTORY_YET = "The bot hasn't kept any prices for this auction house yet."
NOT_A_SPAN = "That isn't one of the spans."
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


def _point_json(reg, p: price_history.Point) -> dict:
    # a folded day is one UTC day (price_history's fold): read as that day alone, never shifted into the guild's zone
    when = fmt12(p.at.astimezone(timezone.utc), "%a %d %b") if p.qty is None else reg.local12(p.at)
    return {"t": int(p.at.timestamp()), "when": when, "low": p.low, "high": p.high,
            "words": money(p.low) if p.low is not None else NONE_LISTED}


def chart(reg, item_id: int, span: str, *, now: datetime | None = None) -> dict:
    """The chart's data for one item over one of SPANS (BLOCKING: reads the history file): its points (epoch seconds,
    the time in the Clock rule's words, lowest / highest in copper, the lowest in words; low None = nothing listed from
    then: a gap), the span's lowest and highest, and `since` / `until` = the span's start and the server's now in epoch
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
    except price_history.HistoryError:
        log.warning("price history unreadable for %s", key, exc_info=True)
        return out
    out["points"] = [_point_json(reg, p) for p in pts]
    out.update(low=s.low, high=s.high, low_words=money(s.low) if s.low is not None else NONE_LISTED,
               high_words=money(s.high) if s.high is not None else NONE_LISTED)
    return out


__all__ = ["EMPTY_LIST_DISCORD", "EMPTY_LIST_HIDDEN", "EMPTY_LIST_SITE", "NOT_A_SPAN", "NO_HISTORY_YET", "SPANS", "UNNAMED", "chart", "history_sentences",
           "is_watching", "unwatch", "watch", "watch_list", "Cost", "CostLine", "HELP_ITEMS", "HELP_RECIPES", "HELP_WORDS", "ItemPrice", "NONE_LISTED", "NO_MATCH", "OWNER_LINK_WORDS", "OWNER_OFF_WORDS", "PRICE_LINES", "answer_lines", "choice_description",
           "choices_words", "cost_line", "help_hit", "help_section", "named_items", "named_recipes","item_price", "listings_words", "price_answer", "sample_names", "sample_rows", "more_words", "cost_sentence", "made_words", "names",
           "price_words", "recipe_cost", "remember", "reset_names", "search", "status_sentence", "unnamed"]
