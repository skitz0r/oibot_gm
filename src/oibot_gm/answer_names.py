"""What a message names (answer tools, design §5): before any model request, code matches the message's words against
every name the guild has — recipes, items, members, characters, raids, schedules, professions, classes and specs,
commands and manual parts — and hands at most RESOLVE_LIMIT typed candidates to the first request. The same function backs the `find` tool.

Never a second matcher: recipes come from professions.search / phrase_hits, items from prices.names (the names alone, so
an item resolves on a day or a game version without prices), people from help.mentioned_members and help's whole-word
test, manual parts from help.manual_match, and raids, schedules, professions, commands and items go through
professions.match_tiers. This module ranks and merges what those finders return.

A word is rare when few names hold it (word_table): "ironfeather" is in four names, "leather" in about two hundred. A
stop word, a word under MIN_WORD letters or a common word never makes a candidate alone. The table ranks; it never
matches on its own: a word start ("ironf…") counts only as a search box's lone word, from START_MIN letters, when the
table says it starts few names; only the WORD_SEARCHES rarest words get a search of their own; manual parts only for a
how-to (HOW_TO) or a text of one word nothing else names — so ordinary chat resolves to nothing.

Every `name` and `detail` this module returns is already escaped for a prompt's data block (_safe: a fence can neither
open nor close, no line breaks): callers put them in a prompt as they are. Nothing here writes, sends or calls a model."""
from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable

from . import help as help_mod
from . import prices
from . import professions
from .llm.provider import unfence
from .profiles import norm_recipe
from .registry import Registry, schedule_name

KINDS = ("recipe", "item", "member", "character", "raid", "schedule", "profession", "class", "command", "manual")
STOP_WORDS = frozenset({
    "who", "what", "which", "when", "where", "why", "how", "can", "could", "does", "did", "has", "have", "had", "make",
    "makes", "made", "craft", "crafts", "the", "and", "for", "with", "any", "anyone", "someone", "our", "you", "your",
    "are", "was", "bot", "oibot", "hey", "please", "piece", "pieces", "get", "got", "need", "know", "tell",
    # more of the same: question and filler words no name is found by
    "would", "should", "will", "there", "here", "this", "that", "these", "those", "they", "them", "their", "him", "her",
    "his", "she", "its", "not", "all", "some", "one", "out", "about", "from", "into", "much", "many", "now", "yet", "just",
    "like", "want", "see", "show", "give", "find", "look", "anybody", "somebody", "everyone", "able", "doing",
    "thanks", "thank", "yes", "yeah", "okay", "hello", "hiya", "guys", "folks", "plz", "pls", "also", "still", "then",
    "than", "very", "really", "maybe", "else", "other", "more", "most", "less", "good", "best", "new", "old", "next", "last",
    "play", "plays", "playing",
    # chat filler (the fix round): "hey bot can you help me with something" names nothing
    "help", "something", "anything", "thing", "things", "stuff", "question",
})
RARE_SHARE = 0.005  # a word in at most this share of all names is rare (2,871 names in the forever profile → 14)
RARE_MIN = 3        # … and never fewer than this many
RESOLVE_LIMIT = 10
MANUAL_MATCHES = 2
MIN_WORD = 3
WORD_SEARCHES = 8   # per-word searches: only the rarest telling words, so a long message costs no more than a short one
START_MIN = 4       # a word start ("ironf…") counts only from this many letters, only when it starts few names, and only
                    # when it is the text's one telling word (a search box's shape — in a sentence people type whole words)
# a how-to's shape: only then are manual parts searched (or when the text is one telling word nothing else names)
HOW_TO = re.compile(r"\b(?:how|why|explain|mean|means)\b|\b(?:what|where) (?:is|are|do|does|s)\b|\b(?:can|do|should) i\b|\bis there\b")
DETAIL_MAX = 120
FENCE_TAGS = ("data", "member-text")

_DATA_KINDS = frozenset(KINDS) - {"command", "manual"}
NOTHING = "Nothing in the guild's names matched."


@dataclass(frozen=True)
class NameMatch:
    kind: str
    id: str
    name: str
    why: str
    detail: str = ""


@dataclass(frozen=True)
class WordTable:
    counts: dict[str, int]  # word → how many distinct names hold it
    total: int              # how many distinct names in all

    @property
    def cut(self) -> float:
        """A word in at most this many names is rare."""
        return max(RARE_MIN, RARE_SHARE * self.total)

    def rare(self, word: str) -> bool:
        return self.counts.get(word, 0) <= self.cut

    @cached_property
    def _sorted(self) -> list[str]:
        return sorted(self.counts)

    def starts(self, word: str) -> int:
        """How many names hold a word starting with `word` (the table's own words read in order — no matching here; a
        name holding two such words counts twice, so this is never under the truth)."""
        keys, n = self._sorted, 0
        for k in keys[bisect_left(keys, word):]:
            if not k.startswith(word):
                break
            n += self.counts[k]
        return n


# ---------------------------------------------------------------- the word-count table

@dataclass(frozen=True)
class _ProfilePart:
    recipes: dict     # the profile's recipe dict, held so a new dict at a reused id never reads a stale entry
    item_names: dict  # prices.names(reg) when built (a cached object: a newly named item makes a new dict, so a new part)
    names: frozenset  # normalised names
    counts: dict      # word → names holding it
    items: tuple      # ((item id, words tuple, words frozenset, name), …) for match_tiers, one id per name (the first)
    classes: tuple    # ((class id, words tuple, words frozenset, name), …): every class, and every spec as 'Class:Spec'


_PROFILE: dict[str, _ProfilePart] = {}


def _words_of(name: str) -> tuple[str, ...]:
    return tuple(norm_recipe(name).split())


def _count(names: Iterable[str]) -> Counter:
    c: Counter = Counter()
    for n in names:
        c.update(set(n.split()))
    return c


def _spec_name(cls: str, spec: str) -> str:
    """'Holy Priest', 'Beast Mastery Hunter' — a spec as people say it."""
    return f"{re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', spec)} {cls}"


def _profile_part(reg: Registry) -> _ProfilePart:
    """Recipe names, every item name the bot can give (prices.names: what recipes make, their reagents, loot items) and
    the classes and specs, built once per (profile, prices.names dict) — the profile's tables never change while the
    bot runs, and prices.names hands out a new dict only when the hourly naming learns new ids."""
    item_names = prices.names(reg)
    recipes = reg.profile.recipes
    held = _PROFILE.get(reg.profile.name)
    if held is not None and held.recipes is recipes and held.item_names is item_names:
        return held
    classes = []
    for cls, specs in reg.profile.classes.items():
        classes.append((cls, cls))
        classes += [(f"{cls}:{spec}", _spec_name(cls, spec)) for spec in specs]
    class_rows = tuple((cid, w, frozenset(w), n) for cid, n in classes if (w := _words_of(n)))
    names = ({norm_recipe(r.name) for r in recipes.values()} | {norm_recipe(n) for n in item_names.values()}
             | {" ".join(w) for _, w, _, _ in class_rows})
    names.discard("")
    seen, items = set(), []
    for iid, n in item_names.items():
        w = _words_of(n)
        if w and w not in seen:  # two ids with one name: the first stands for both
            seen.add(w)
            items.append((iid, w, frozenset(w), n))
    part = _ProfilePart(recipes, item_names, frozenset(names), dict(_count(names)), tuple(items), class_rows)
    _PROFILE[reg.profile.name] = part
    return part


def _people(reg: Registry):
    """(member, its characters) for everyone a candidate may name: never a test puppet, never someone who left."""
    for m in reg.members.values():
        if m.test or getattr(m, "left_at", None):
            continue
        yield m, [c for c in m.active() if c.name]


people = _people  # the public name (answer_tools' finders read the same population); `_people` keeps working


def _guild_names(reg: Registry) -> set[str]:
    out = set()
    for m, chars in _people(reg):
        out.add(norm_recipe(m.display_name))
        out.update(norm_recipe(c.label) for c in chars)
    for rid in reg.profile.raids:
        out.add(norm_recipe(reg.raid_def(rid).get("name") or rid))
    out.update(norm_recipe(schedule_name(s)) for s in reg.all_schedules())
    out.update(norm_recipe(d.name) for d in reg.profile.professions.values())
    out.discard("")
    return out


def word_table(reg: Registry) -> WordTable:
    """How many distinct names hold each word. The profile's part is cached (_profile_part); the guild's part — members,
    characters, raids, schedules, professions — is counted on every call, so a new character counts at once."""
    part = _profile_part(reg)
    extra = _guild_names(reg) - part.names
    counts = dict(part.counts)
    for w, n in _count(extra).items():
        counts[w] = counts.get(w, 0) + n
    return WordTable(counts, len(part.names) + len(extra))


# ---------------------------------------------------------------- ranking

def _safe(text) -> str:
    """Text a prompt will hold as data: no fence can open or close, no line breaks, and no '·' (match_lines' field
    separator: a name holding ' · recipe · ' never forges a field)."""
    out = unfence(text, *FENCE_TAGS).replace("\n", " ").replace("\r", " ").replace(" · ", ", ").replace("·", ",")
    return out.strip()


def _join(*parts: str) -> str:
    """Fields of a detail, each escaped, joined by the one separator code writes."""
    return " · ".join(_safe(p) for p in parts if p)


def _cut(text: str) -> str:
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1].rstrip() + "…"


def _telling(word: str) -> bool:
    return len(word) >= MIN_WORD and word not in STOP_WORDS


WHY = {0: "the whole name", 1: "every word of the name"}


def _why_word(tier: int, word: str) -> str:
    return f'the rare word "{word}"' if tier == 2 else f'a word starting "{word}"'


@dataclass
class _Hit:
    tier: int
    rarity: int  # a tier-2 / tier-3 hit: how many names hold its word (rarer first); else 0
    owned: bool  # a recipe somebody makes, a member or a character
    match: NameMatch


def _key(h: _Hit) -> tuple:
    return (h.tier, h.match.kind not in _DATA_KINDS, h.rarity, not h.owned, h.match.name.lower(), h.match.kind, h.match.id)


def _whole_tier(q: str, words: frozenset, name_words: frozenset, table: WordTable) -> tuple[int, int, str] | None:
    """(tier, rarity, why) of a name the whole text matched: 0 exact; 1 every word present of a name with two telling
    words; a name with ONE telling word inside a longer text is the evidence of one rare word — tier 2 at that word's
    rarity, and only when it is rare (a common word never makes a candidate alone); None otherwise."""
    if " ".join(sorted(name_words)) == " ".join(sorted(q.split())) and q:
        return 0, 0, WHY[0]
    if name_words <= words:
        telling = [w for w in name_words if _telling(w)]
        if len(telling) >= 2:
            return 1, 0, WHY[1]
        if len(telling) == 1 and table.rare(telling[0]):
            return 2, table.counts.get(telling[0], 0), _why_word(2, telling[0])
    return None


def _word_tier(word: str, name_words: Iterable[str], starts: frozenset) -> int | None:
    """2 when the rare word is a whole word of the name, 3 when it only starts one (only a word in `starts`)."""
    name_words = tuple(name_words)
    if word in name_words:
        return 2
    if word in starts and any(k.startswith(word) for k in name_words):
        return 3
    return None


def _named(q: str, words: frozenset, rare: list[str], starts: frozenset, rows, table: WordTable) -> dict:
    """Things (rows of (thing, words tuple, words set)) through match_tiers: the whole text's tiers 0–1, then each rare
    word → {thing: (tier, rarity, why)}, the best tier kept."""
    out: dict = {}

    def keep(x, tier, rarity, why):
        if x not in out or (tier, rarity) < out[x][:2]:
            out[x] = (tier, rarity, why)

    by_thing = {x: ks for x, _, ks in rows}
    for x in professions.match_tiers(q, rows, longest=True):
        t = _whole_tier(q, words, by_thing[x], table)
        if t is not None:
            keep(x, *t)
    for w in rare:
        for x in professions.match_tiers(w, rows):
            t = _word_tier(w, by_thing[x], starts)
            if t is not None:
                keep(x, t, table.counts.get(w, 0), _why_word(t, w))
    return out


# ---------------------------------------------------------------- each kind

def _recipes(reg: Registry, text: str, q: str, words: frozenset, rare: list[str], starts: frozenset,
             table: WordTable) -> list[_Hit]:
    found: dict[str, tuple] = {}

    def keep(e, tier, rarity, why):
        k = str(e.id) if e.id is not None else e.key
        if k not in found or (tier, rarity) < found[k][1:3]:
            found[k] = (e, tier, rarity, why)

    for e in professions.search(reg, text, None):
        t = _whole_tier(q, words, frozenset(e.key.split()), table)
        if t is not None:
            keep(e, *t)
    for e in professions.phrase_hits(reg, text, RESOLVE_LIMIT * 5, tails=True):
        keep(e, 2, 0, "two words of the name or of what it makes")
    for w in rare:
        for e in professions.search(reg, w, None):
            t = _word_tier(w, e.key.split(), starts)
            if t is not None:
                keep(e, t, table.counts.get(w, 0), _why_word(t, w))
    out = []
    for k, (e, tier, rarity, why) in found.items():
        where = professions.where_words(reg, e)
        if e.crafters:
            c = e.crafters[0]
            who = f"made by {c.label} ({professions.skill_words(c.skill)})" + (f" and {len(e.crafters) - 1} more" if len(e.crafters) > 1 else "")
        else:
            who = "nobody in the guild makes it yet"
        made = _made(reg, e)
        detail = _join(*where.split(" · "), f"makes {made}" if made and norm_recipe(made) != e.key else "", who)
        out.append(_Hit(tier, rarity, bool(e.crafters), NameMatch("recipe", k, _safe(e.name), _safe(why), _cut(detail))))
    return out


def _made(reg: Registry, e) -> str | None:
    """The name of what a listed recipe makes, else None."""
    d = reg.profile.recipes.get(e.id) if e.id is not None else None
    return (d.makes or {}).get("name") if d is not None else None


def _covered(reg: Registry, recipe_hits: list[_Hit]) -> set[int]:
    """Item ids a recipe candidate MAKES: the recipe's detail says what it makes, so the item's name is never lost (a
    reagent stays a candidate of its own — R1)."""
    ids: set[int] = set()
    for h in recipe_hits:
        d = reg.profile.recipes.get(int(h.match.id)) if h.match.id.isdigit() else None
        if d is not None and d.makes:
            ids.add(int(d.makes["id"]))
    return ids


def _items(reg: Registry, q, words, rare, starts, table, covered: set[int]) -> list[_Hit]:
    rows, name_of = [], {}
    for iid, w, ks, n in _profile_part(reg).items:
        if iid not in covered:
            rows.append((iid, w, ks))
            name_of[iid] = n  # the part's own name: an id the current names lack never raises
    return [_Hit(t, r, False, NameMatch("item", str(iid), _safe(name_of[iid]), _safe(why)))
            for iid, (t, r, why) in _named(q, words, rare, starts, rows, table).items()]


def _person_tier(q: str, full: str, first: str | None, table: WordTable) -> tuple[int, int, str] | None:
    """A person named by whole word (help's test): the full name → tier 1; a first name alone → tier 2. A name that is
    only a stop word or too short never counts; a common word does (a person is named on purpose — design §5's
    Review Focus: a character called like a recipe word gives both kinds)."""
    if _telling(norm_recipe(full)) or len(norm_recipe(full).split()) > 1:
        if help_mod.whole_words(q, full):
            return 1, 0, WHY[1]
    if first and _telling(norm_recipe(first)) and help_mod.whole_words(q, first):
        w = norm_recipe(first)
        return 2, table.counts.get(w, 0), f'the name "{first}"'
    return None


def _members(reg: Registry, text: str, table: WordTable, want: set[str]) -> list[_Hit]:
    q = (text or "").lower()
    out = []
    for m in help_mod.mentioned_members(reg, text):
        if m.test or getattr(m, "left_at", None):
            continue
        chars = [c for c in m.active() if c.name]
        if "member" in want:
            t = _person_tier(q, m.display_name, None, table)
            if t is not None:
                out.append(_Hit(t[0], t[1], True, NameMatch("member", str(m.discord_id), _safe(m.display_name), _safe(t[2]),
                                                        _cut(_member_detail(reg, m, chars)))))
        if "character" in want:
            for c in chars:
                t = _person_tier(q, c.label, c.name if c.surname else None, table)
                if t is not None:
                    out.append(_Hit(t[0], t[1], True, NameMatch("character", f"{m.discord_id}:{c.label}", _safe(c.label), _safe(t[2]),
                                                        _cut(_join(f"{m.display_name}'s", f"{c.cls} {c.spec}")))))
    return out


def _member_detail(reg: Registry, m, chars) -> str:
    """The member's characters in words: help's who line when the tree has it (the same line the who section prints),
    else their first named character; a planned main never hides an active alt."""
    who_line = getattr(help_mod, "who_line", None)
    if who_line is not None:
        line = who_line(reg, m)
        if line is None:
            return "no character yet"
        head = f"- {help_mod.safe_text(m.display_name)}: "
        return _safe(line[len(head):] if line.startswith(head) else line)
    if not chars:
        return "no character yet"
    c = next((c for c in chars if c.is_main), chars[0])
    return _join(f"{'main' if c.is_main else 'alt'} {c.label}", f"{c.cls} {c.spec}")


def _raids(reg: Registry, q, words, rare, starts, table) -> list[_Hit]:
    rows = []
    for rid in reg.profile.raids:
        r = reg.raid_def(rid)
        if w := _words_of(r.get("name") or rid):
            rows.append((rid, w, frozenset(w)))
    out = []
    for rid, (t, rr, why) in _named(q, words, rare, starts, rows, table).items():
        r = reg.raid_def(rid)
        size = r.get("size")
        out.append(_Hit(t, rr, False, NameMatch("raid", rid, _safe(r.get("name") or rid), _safe(why), f"{size} players" if size else "")))
    return out


def _schedules(reg: Registry, q, words, rare, starts, table) -> list[_Hit]:
    rows, by_id = [], {}
    for s in reg.all_schedules():
        by_id[s["id"]] = s
        if w := _words_of(schedule_name(s)):
            rows.append((s["id"], w, frozenset(w)))
    out = []
    for sid, (t, rr, why) in _named(q, words, rare, starts, rows, table).items():
        s = by_id[sid]
        raid = reg.raid_def(s["raid"]).get("name") or s["raid"]
        out.append(_Hit(t, rr, False, NameMatch("schedule", sid, _safe(schedule_name(s)), _safe(why), _cut(_safe(f"a schedule of {raid}")))))
    return out


def _professions(reg: Registry, q, words, rare, starts, table) -> list[_Hit]:
    defs = reg.profile.professions
    rows = [(pid, w, frozenset(w)) for pid, d in defs.items() if (w := _words_of(d.name))]
    out = []
    for pid, (t, rr, why) in _named(q, words, rare, starts, rows, table).items():
        n = len(professions.crafters_of(reg, pid))
        detail = f"{n} in the guild have it" if n else "nobody in the guild has it yet"
        out.append(_Hit(t, rr, False, NameMatch("profession", pid, _safe(defs[pid].name), _safe(why), detail)))
    return out


def _classes(reg: Registry, q, words, rare, starts, table) -> list[_Hit]:
    """Classes and specs (id 'Druid' / 'Druid:Restoration'); detail = how many members play it as their main."""
    part = _profile_part(reg)
    rows = [(cid, w, ks) for cid, w, ks, _ in part.classes]
    name_of = {cid: n for cid, _, _, n in part.classes}
    mains = Counter()
    for m, _chars in _people(reg):
        main = m.main
        if main is not None and main.name:
            mains[main.cls] += 1
            mains[f"{main.cls}:{main.spec}"] += 1
    out = []
    for cid, (t, rr, why) in _named(q, words, rare, starts, rows, table).items():
        n = mains[cid]
        detail = ("no member plays it as a main yet" if not n else
                  "1 member plays it as a main" if n == 1 else f"{n} members play it as a main")
        out.append(_Hit(t, rr, False, NameMatch("class", cid, _safe(name_of[cid]), _safe(why), detail)))
    return out


def _commands(tree, q, words, rare, starts, table) -> list[_Hit]:
    rows, desc = [], {}
    for row in help_mod.command_rows(tree, "member"):
        desc[row["path"]] = row["description"]
        if w := _words_of(row["path"]):
            rows.append((row["path"], w, frozenset(w)))
    return [_Hit(t, rr, False, NameMatch("command", path, _safe(path), _safe(why), _cut(_safe(desc[path]))))
            for path, (t, rr, why) in _named(q, words, rare, starts, rows, table).items()]


def _manual(telling: list[str]) -> list[_Hit]:
    if not telling:
        return []
    parts = {p["id"]: p for p in help_mod.manual_parts()}
    out = []
    for pid in help_mod.manual_match(" ".join(telling), MANUAL_MATCHES):
        p = parts.get(pid)
        if p:
            out.append(_Hit(2, 0, False, NameMatch("manual", pid, _cut(_safe(p["gist"])), "the manual's words", _cut(_safe(p["title"])))))
    return out


# ---------------------------------------------------------------- the resolver

def resolve(reg: Registry, text: str, *, kinds: Iterable[str] | None = None, limit: int = RESOLVE_LIMIT,
            tree=None) -> tuple[list[NameMatch], int]:
    """The names a text holds → (the first `limit` candidates, how many in all). Tier 0 the exact name; 1 every word of
    a name present; 2 a rare word of a name (rarer first); 3 a word start. Within a tier a recipe somebody makes and
    people come first, then data before commands and manual parts, then by name. `kinds` keeps those kinds (unknown
    ones ignored); `tree` = the live command tree (None: no commands). Text with no telling word resolves to ([], 0)."""
    want = set(KINDS) if kinds is None else {k for k in kinds if k in KINDS}
    limit = max(0, min(limit, RESOLVE_LIMIT))
    q = norm_recipe(text)
    words = frozenset(q.split())
    telling = [w for w in dict.fromkeys(q.split()) if _telling(w)]
    if not telling or not want:
        return [], 0
    table = word_table(reg)
    rare = sorted((w for w in telling if table.rare(w)), key=lambda w: (table.counts.get(w, 0), w))[:WORD_SEARCHES]
    # a word start counts only in a search box's shape: the text's one telling word, long enough, starting few names
    # ("ironf" starts 7; "run" starts 52, and "mark" in "mark myself away" is a sentence's word, not a search)
    starts = frozenset(w for w in rare if len(telling) == 1 and len(w) >= START_MIN and table.starts(w) <= table.cut)
    hits: list[_Hit] = []
    recipe_hits = _recipes(reg, text, q, words, rare, starts, table) if "recipe" in want else []
    hits += recipe_hits
    if "item" in want:
        hits += _items(reg, q, words, rare, starts, table, _covered(reg, recipe_hits))
    if want & {"member", "character"}:
        hits += _members(reg, text, table, want)
    if "raid" in want:
        hits += _raids(reg, q, words, rare, starts, table)
    if "schedule" in want:
        hits += _schedules(reg, q, words, rare, starts, table)
    if "profession" in want:
        hits += _professions(reg, q, words, rare, starts, table)
    if "class" in want:
        hits += _classes(reg, q, words, rare, starts, table)
    if "command" in want and tree is not None:
        hits += _commands(tree, q, words, rare, starts, table)
    if "manual" in want and (HOW_TO.search(q) or (len(telling) == 1 and not hits)):
        # the words no name above took: "ironfeather" finds recipes, not the manual's example of it
        named = {w for h in hits for w in norm_recipe(h.match.name).split()}
        hits += _manual([w for w in telling if w not in named])
    hits.sort(key=_key)
    return [h.match for h in hits[:limit]], len(hits)


def match_lines(matches: list[NameMatch], total: int) -> list[str]:
    """The candidates as the model reads them: '<kind> · <name> · <detail> (<why>)' each, then '<n> of <total>'; or
    the one NOTHING line."""
    if not matches:
        return [NOTHING]
    return [" · ".join(x for x in (m.kind, m.name, m.detail) if x) + f" ({m.why})" for m in matches] + [f"{len(matches)} of {total}"]
