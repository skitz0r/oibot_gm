"""Professions (design §5.60): the derived recipe catalog of a guild, the finders, plain text's entry, the officers'
fix for recipes typed before the game's list, the help section and the words. Member data lives on
RegisteredCharacter.professions (registry.py); game data in the profile (professions.yaml, recipes.yaml). Nothing here
pings, DMs or tracks a request; nothing reads a website."""
from __future__ import annotations

import re
import types
from dataclasses import dataclass, field

from .llm.provider import unfence
from .profiles import RecipeDef, norm_recipe
from .registry import Registry, RegistryError


@dataclass
class Crafter:
    label: str      # the character
    member: str     # display name
    uid: int
    profession: str  # profile id
    skill: int | None  # None = not said yet (sorts last)
    specialization: str | None = None  # its name, read from the recipes (specialization_of); None = none shown


SKILL_UNSAID = "skill not said yet"


def specialization_of(reg: Registry, p) -> str | None:
    """THE reader of a character's specialization in one profession (a registry Profession): the name of the one its
    known recipes show (ProfessionDef.specialization — the armory reports none), else None. Never stored."""
    d = reg.profile.professions.get(p.id)
    s = d.specialization(r.id for r in p.recipes) if d and d.specializations else None
    return s.name if s else None


def skill_words(skill: int | None) -> str:
    """A crafter's skill as a reader sees it: the number, or that it is not said yet."""
    return str(skill) if skill is not None else SKILL_UNSAID


def _by_skill(x) -> tuple:
    """Highest skill first, a skill not said yet last, then by character."""
    return (-(x.skill if x.skill is not None else -1), x.label.lower())


@dataclass
class CatalogEntry:
    key: str         # norm_recipe(name)
    name: str        # the list's spelling; an unmatched typed recipe: the spelling most crafters use (ties: the first alphabetically)
    profession: str
    crafters: list[Crafter] = field(default_factory=list)  # [] = a listed recipe nobody in the guild makes yet
    id: int | None = None
    recipe: RecipeDef | None = None  # the game's list entry; None = a typed recipe the list doesn't have (or no list)

    @property
    def category(self) -> str | None:
        return self.recipe.category if self.recipe else None


NOT_A_RECIPE = "That isn't a recipe in the game's list."
NOT_REGISTERED = "Nobody has registered that recipe yet."  # a version without the game's list
NO_RECIPE_LIST = "This game version has no recipe list."  # the officers' fix where there is none (the route answers 404)


def nobody_sentence(name: str) -> str:
    """A recipe of the game's list that no visible character makes — a different answer from NOT_A_RECIPE."""
    return f"Nobody in the guild makes {name} yet."


def visible(reg: Registry, m, c) -> bool:
    """A crafter the finder may name: an active character of a member who is in the server and not a puppet."""
    return c.status == "active" and not m.test and not getattr(m, "left_at", None)


def _rows(reg: Registry):
    for m in reg.members.values():
        for c in m.active():
            if visible(reg, m, c):
                yield m, c


def unmatched(reg: Registry) -> list[dict]:
    """The recipes on record the game's list doesn't hold for their profession, for the officers' fix: typed ones it did
    not match (armory link §4.3: id empty = unmatched) and ones kept by an id the list lacks or gives to another
    profession (the armory's, kept by hand since — off_list). Visible characters only, and never one whose professions
    follow the armory (the armory keeps those; every fix would be refused). Rows {uid, member, label, profession, name}.
    Empty for a version without a list."""
    if not reg.profile.has_recipe_list:
        return []
    rows = [{"uid": m.discord_id, "member": m.display_name, "label": c.label, "profession": p.id, "name": r.name}
            for m, c in _rows(reg) if not (c.armory is not None and c.profession_sync)
            for p in c.professions for r in p.recipes if off_list(reg, p.id, r.id)]
    return sorted(rows, key=lambda x: (x["member"].lower(), x["label"].lower(), x["profession"], x["name"].lower()))


def off_list(reg: Registry, pid: str, rid: int | None) -> bool:
    """Whether a recipe on record (profession `pid`, id `rid`) is one the game's list doesn't hold for that profession:
    no id (typed, unmatched), an id the list lacks, or one the list gives to another profession."""
    if rid is None:
        return True
    d = reg.profile.recipes.get(int(rid))
    return d is None or d.profession != pid


def migrated_line(res: dict) -> str:
    """The ops line of a migration that matched something (Registry.migrate_recipes' result)."""
    return f"Recipes matched to the game's list: {res['matched']} matched, {res['unmatched']} not in the list."


def catalog(reg: Registry) -> list[CatalogEntry]:
    """The recipes somebody in the guild makes, by name. A recipe of the game's list is one entry per recipe id (two
    same-name recipes stay two, told apart by category — R6) in the list's spelling; a typed recipe the list doesn't
    have (or every recipe, for a version without a list) is one entry per norm_recipe name in its most used spelling."""
    listed = reg.profile.recipes
    by_key: dict[tuple, CatalogEntry] = {}
    spellings: dict[tuple, dict[str, int]] = {}
    for m, c in _rows(reg):
        for p in c.professions:
            for r in p.recipes:
                d = listed.get(r.id) if r.id is not None else None
                if d is not None:
                    gk = ("id", d.id)
                    e = by_key.setdefault(gk, CatalogEntry(key=d.key, name=d.name, profession=d.profession, id=d.id, recipe=d))
                else:
                    k = norm_recipe(r.name)
                    gk = ("name", k)
                    e = by_key.setdefault(gk, CatalogEntry(key=k, name=r.name, profession=p.id, id=r.id))
                    spellings.setdefault(gk, {})[r.name] = spellings.get(gk, {}).get(r.name, 0) + 1
                    if e.id is None and r.id:
                        e.id = r.id
                e.crafters.append(Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill, specialization_of(reg, p)))
    for gk, e in by_key.items():
        if gk in spellings:
            e.name = sorted(spellings[gk].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        e.crafters.sort(key=_by_skill)
    return sorted(by_key.values(), key=_order)


def _order(e: CatalogEntry) -> tuple:
    return (e.name.lower(), e.profession, e.category or "", e.id or 0)


@dataclass(frozen=True)
class _Index:
    """The game's list prepared once per profile for the finder: every recipe with its key's words, and by exact key."""
    recipes: dict  # the profile's dict, held so the cache entry is never matched to a new dict at a reused id()
    rows: tuple    # ((RecipeDef, words tuple, words frozenset), …)
    by_key: dict   # key → [RecipeDef]
    pairs: dict    # (word, next word) of a name → [RecipeDef]: a sentence that names part of a recipe (phrase_hits)
    tails: dict    # the one word after a name's dash → [(the name's first word, RecipeDef)] ("the crusader enchant")


_INDEX: dict[int, _Index] = {}


def _index(profile) -> _Index:
    recipes = profile.recipes
    ix = _INDEX.get(id(recipes))
    if ix is None or ix.recipes is not recipes:
        by_key: dict[str, list] = {}
        for r in recipes.values():
            by_key.setdefault(r.key, []).append(r)
        pairs: dict[tuple, list] = {}
        for r in recipes.values():
            kw = r.key.split()
            for pair in {(a, b) for a, b in zip(kw, kw[1:]) if _telling(a) and _telling(b)}:
                pairs.setdefault(pair, []).append(r)
        tails: dict[str, list] = {}
        for r in recipes.values():
            head, dash, tail = r.name.partition(" - ")
            hw, tw = norm_recipe(head).split(), norm_recipe(tail).split()
            if dash and hw and len(tw) == 1 and _telling(tw[0]):
                tails.setdefault(tw[0], []).append((hw[0], r))
        ix = _Index(recipes, tuple((r, tuple(r.key.split()), frozenset(r.key.split())) for r in recipes.values()), by_key, pairs, tails)
        _INDEX[id(recipes)] = ix
    return ix


PAIR_SKIP = frozenset({"the", "and", "for", "with"})


def _telling(word: str) -> bool:
    """A word of a recipe's name that says something on its own (three letters or more, not a joining word)."""
    return len(word) >= 3 and word not in PAIR_SKIP


def phrase_hits(reg: Registry, text: str, limit: int = 10, *, tails: bool = False) -> list[CatalogEntry]:
    """The recipes of the game's list a SENTENCE names by part of their name: two words next to each other in the
    sentence that are next to each other in the recipe's name ("can anyone do fiery weapon?" → Enchant Weapon - Fiery
    Weapon, which nobody calls by its whole name). `tails` (the help answer): the one word after a name's dash counts
    too when the sentence also has a word starting with the name's first word ("the crusader enchant" → Enchant
    Weapon - Crusader). Recipes somebody makes first, then by name; none without a list."""
    if not reg.profile.has_recipe_list:
        return []
    wl = norm_recipe(text).split()
    ix = _index(reg.profile)
    found = {r.id: r for pair in zip(wl, wl[1:]) for r in ix.pairs.get(pair, ())}
    if tails:
        found.update({r.id: r for w in wl for first, r in ix.tails.get(w, ()) if any(x.startswith(first) for x in wl)})
    if not found:
        return []
    made = {e.id: e for e in catalog(reg) if e.recipe is not None}
    hits = [made.get(rid) or _listed_entry(r) for rid, r in found.items()]
    hits.sort(key=lambda e: (not e.crafters, *_order(e)))
    return hits[:limit]


def _tiers(q: str, items, *, longest: bool = False, kept=lambda x: False):
    """The finder's three tiers over (thing, words tuple, words set): the exact key; else every name whose words all
    appear in the text (a sentence that names it — `longest`: a name inside another named one drops out, so "a mooncloth
    bag" is the bag, not also Mooncloth — unless `kept(thing)`: a recipe somebody makes is never dropped); else every
    typed word starting a word of the name (a search box)."""
    wl = q.split()
    words = set(wl)
    exact = [x for x, kw, _ in items if " ".join(kw) == q]
    if exact:
        return exact
    named = [(x, ks) for x, _, ks in items if ks <= words]
    if named:
        return [x for x, ks in named if not (longest and not kept(x) and any(ks < other for _, other in named))]
    return [x for x, kw, _ in items if all(any(k.startswith(w) for k in kw) for w in wl)]


def _list_items(reg: Registry, q: str, profession: str | None) -> list:
    """The game's list as _tiers items: the exact key's recipes alone when the text IS a recipe's name (no walk of the
    list), else every recipe; `profession` keeps that profession's."""
    ix = _index(reg.profile)
    rows = [(r, tuple(r.key.split()), frozenset(r.key.split())) for r in ix.by_key[q]] if q in ix.by_key else ix.rows
    return [x for x in rows if profession is None or x[0].profession == profession]


def search(reg: Registry, text: str, limit: int | None = 25, *, profession: str | None = None) -> list[CatalogEntry]:
    """The finder (armory link §4.4): the three tiers of who_can_make over the game's WHOLE list plus the typed recipes
    the list doesn't have that somebody makes. A listed recipe nobody makes is an entry with no crafters. Recipes
    somebody makes first, then by name; at most `limit` (None = all: len() is the total a caller says "and N more"
    from). `profession` = that profession's recipes only. A version without a list searches the catalog only, as
    before. The list's half is prepared once per profile (_index): fast enough for Discord's autocomplete."""
    q = norm_recipe(text)
    if not q:
        return []
    cat = [e for e in catalog(reg) if profession is None or e.profession == profession]
    made = {e.id: e for e in cat if e.recipe is not None}
    loose = [e for e in cat if e.recipe is None]
    items = [(e, tuple(e.key.split()), frozenset(e.key.split())) for e in loose]
    if reg.profile.has_recipe_list:
        items += _list_items(reg, q, profession)

    def crafted(x) -> bool:
        return isinstance(x, CatalogEntry) or x.id in made

    hits = [x if isinstance(x, CatalogEntry) else made.get(x.id) or _listed_entry(x)
            for x in _tiers(q, items, longest=reg.profile.has_recipe_list, kept=crafted)]
    hits.sort(key=lambda e: (not e.crafters, *_order(e)))
    return hits if limit is None else hits[:limit]


def list_hits(reg: Registry, text: str, *, profession: str | None = None, longest: bool = False) -> list[RecipeDef]:
    """The game's list alone through the finder's three tiers (entry: the wizard's select, plain text's resolution),
    by name, then category, then id — two same-name recipes stay two (R6). Empty for a version without a list."""
    q = norm_recipe(text)
    if not q or not reg.profile.has_recipe_list:
        return []
    return sorted(_tiers(q, _list_items(reg, q, profession), longest=longest), key=lambda r: (r.name.lower(), r.category, r.id))


def _listed_entry(d: RecipeDef) -> CatalogEntry:
    return CatalogEntry(key=d.key, name=d.name, profession=d.profession, id=d.id, recipe=d)


def entry_of(reg: Registry, rid: int) -> CatalogEntry | None:
    """One recipe of the game's list by id, with its crafters (none: nobody makes it yet); None when the list lacks it."""
    d = reg.profile.recipes.get(rid)
    if d is None:
        return None
    return next((e for e in catalog(reg) if e.recipe is not None and e.id == rid), None) or _listed_entry(d)


PICKED = re.compile(r"^#(\d+)$")
"""The value of a recipe picked from the game's list in Discord's autocomplete: '#<recipe id>'."""


def who_can_make(reg: Registry, text: str, limit: int | None = 25, *, profession: str | None = None) -> list[CatalogEntry]:
    """Who makes the recipe(s) a text names: search (exact name first; else every recipe whose words all appear in the
    text — whole words: a sentence that names a recipe; else the text as PART of a name — a search box: every typed word
    starts a word of the recipe: "ironfeather", "ironf", "armor kit"). A picked '#<id>' is that one recipe. A version
    without the game's list returns every match, as before. `limit` None = every match (the total)."""
    if (m := PICKED.match((text or "").strip())) and reg.profile.has_recipe_list:
        e = entry_of(reg, int(m.group(1)))
        return [e] if e and (profession is None or e.profession == profession) else []
    return search(reg, text, limit if reg.profile.has_recipe_list else None, profession=profession)


def where_words(reg: Registry, e: CatalogEntry) -> str:
    """'Leatherworking · Leather Pauldrons' — the profession, and the game's category for a listed recipe (a profession
    no longer in the profile: its id)."""
    d = reg.profile.professions.get(e.profession)
    return " · ".join(x for x in ((d.name if d else e.profession), e.category) if x)


def more_sentence(n: int) -> str:
    """The recipes a finder's answer leaves out, after the ones it shows."""
    return f"And {n} more recipe{'s' if n != 1 else ''} match{'' if n != 1 else 'es'}."


def answer_lines(reg: Registry, hits: list[CatalogEntry], total: int | None = None) -> list[str]:
    """The finder's answer in plain words (the site's `message`, MCP): one line per recipe with its crafters highest
    skill first, nobody_sentence for a listed recipe nobody makes, NOT_A_RECIPE when the game's list has no such name;
    `total` past the hits shown → more_sentence."""
    if not hits:
        return [NOT_A_RECIPE if reg.profile.has_recipe_list else NOT_REGISTERED]
    out = [f"{e.name} ({where_words(reg, e)}): " + ", ".join(f"{c.label} ({c.member}, {skill_words(c.skill)})" for c in e.crafters)
           if e.crafters else nobody_sentence(e.name) for e in hits]
    return out + ([more_sentence(total - len(hits))] if total is not None and total > len(hits) else [])


def crafters_of(reg: Registry, pid: str, min_skill: int = 0) -> list[Crafter]:
    out = [Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill, specialization_of(reg, p)) for m, c in _rows(reg) for p in c.professions
           if p.id == pid and (not min_skill or (p.skill is not None and p.skill >= min_skill))]
    return sorted(out, key=_by_skill)


def fix_unmatched(reg: Registry, uid: int, label: str, pid: str, name: str, recipe_id: int | None, by: str, *,
                  by_id: int | None = None, preview: bool = False) -> list[str]:
    """The officers' fix for a recipe typed before the game's list that the list doesn't have (armory link §4.3): Pick
    the recipe… (`recipe_id`, a recipe of that profession on the list) takes its place, in place; None = Remove. One
    table save (THE character verb); `preview` = the sentences only, after the same refusals the write runs
    (check_character_table). Read and written under config_lock, the save carrying the record's rev as read: a change
    landing in between is refused (CharactersChanged), never overwritten. Officers only: the route checks the reader."""
    if not reg.profile.has_recipe_list:
        raise RegistryError(NO_RECIPE_LIST)
    with reg.config_lock:
        return _fix_unmatched(reg, uid, label, pid, name, recipe_id, by, by_id, preview)


def _fix_unmatched(reg: Registry, uid, label, pid, name, recipe_id, by, by_id, preview) -> list[str]:
    m = reg.members.get(int(uid))
    rev = reg.characters_rev(m) if m else None
    c = next((x for x in m.active() if x.label == label), None) if m else None
    if c is None:
        raise RegistryError(f"{label} is no longer on the record.")
    pname = reg.profile.profession(pid).name if pid in reg.profile.professions else pid
    k = norm_recipe(name)
    entries = reg.professions_entries(c)
    p = next((e for e in entries if e["id"] == pid), None)
    old = next((r for r in (p["recipes"] if p else []) if off_list(reg, pid, r.get("id")) and norm_recipe(r["name"]) == k), None)
    if old is None:
        raise RegistryError(f"{label}'s {pname} no longer lists {' '.join((name or '').split()) or 'that recipe'}.")
    if recipe_id is None:
        p["recipes"] = [r for r in p["recipes"] if r is not old]
        said = [f"{old['name']} comes off {label}'s {pname}."]
    else:
        d = reg.profile.recipes.get(int(recipe_id))
        if d is None:
            raise RegistryError(NOT_A_RECIPE)
        if d.profession != pid:
            raise RegistryError(f"{d.name} isn't a {pname} recipe.")
        if any(r.get("id") == d.id for r in p["recipes"]):
            p["recipes"] = [r for r in p["recipes"] if r is not old]
            said = [f"{label}'s {pname} already lists {d.name}, so {old['name']} comes off."]
        else:
            p["recipes"] = [{"id": d.id, "name": d.name} if r is old else r for r in p["recipes"]]
            said = [f"{label}'s {pname} lists {d.name} in place of {old['name']}."]
    rows = [{"label": c.label, "professions": entries}]
    if preview:
        reg.check_character_table(m.discord_id, rows, by, officer=True, by_id=by_id, rev=rev)
    else:
        reg.save_character_table(m.discord_id, rows, by, officer=True, by_id=by_id, rev=rev)
    return said


def has_recipes(reg: Registry, pid: str) -> bool:
    """The profession holds recipes. With the game's list: the list gives it any (R7 — Mining's smelting, Fishing's
    camp recipes); without: ProfessionDef.makes (not gathering, not a `recipes: false` secondary like Fishing)."""
    if reg.profile.has_recipe_list:
        return any(r.profession == pid for r in reg.profile.recipes.values())
    return reg.profile.profession(pid).makes


HELP_WORDS = re.compile(r"\b(professions?|craft\w*|who can make|recipes?|enchant\w*|crafters?)\b")
"""Bare make / makes / skill are left out: 'I can't make it tonight' is not a professions question."""


def help_hit(reg: Registry, q: str) -> bool:
    """The question names a profession ('alchemist', 'leatherworking': a one-word name's first six letters at a word
    start; 'first aid': the whole name) or a registered recipe (its whole words). 'the first boss', 'determining the
    comp' and 'baggage' hit nothing."""
    q = norm_recipe(q)
    for d in reg.profile.professions.values():
        name = norm_recipe(d.name)
        pat = rf"\b{re.escape(name)}\b" if " " in name else rf"\b{re.escape(name[:6])}"
        if name and re.search(pat, q):
            return True
    if any(re.search(rf"\b{re.escape(e.key)}\b", q) for e in catalog(reg) if e.key):
        return True
    if reg.profile.has_recipe_list:  # a recipe of the game's list named by its whole words; a one-word name ("Corruption") is too common a word
        words = set(q.split())
        ix = _index(reg.profile)
        wl = q.split()
        return any(len(kw) > 1 and ks <= words for _, kw, ks in ix.rows) or any(pair in ix.pairs for pair in zip(wl, wl[1:]))
    return False


HELP_HITS = 10  # the question's recipes from the game's list in the help snapshot: never the whole list in a prompt


def help_section(ask) -> list[str]:
    """The help snapshot's professions section (members' view, design §5.60, §5.64): each profession's crafters with
    skill; then, without the game's list, the catalog with makers. With the list a crafter's line says how the record
    is kept — `armory` (the whole recipe book read from the game: a recipe not shown is one they do NOT know) or
    `by hand` (typed: they may know more) —, the character's level when linked and a recipe COUNT (never a full book
    in a prompt); then up to HELP_HITS recipes the question names, each with its crafters or nobody_sentence; then the
    recipes of a crafter the question names. Names only: the bot says who, never pings."""
    reg = ask.reg
    if not reg.profile.has_recipe_list:
        return _help_section_registered(ask)

    def safe(text) -> str:  # member text (character, display and recipe names) can't open or close a block
        return unfence(text, "member-text").replace("\n", " ").strip()

    out = ["### Who can make what: professions with their crafters (highest skill first), each as character (member, skill, "
           "specialization when their recipes show one, level when known, how the record is kept, recipes on record) · `armory` = the whole recipe book is read from the "
           "game about every hour: a recipe not shown for that crafter is one they do NOT know — say so plainly, never "
           "suggest asking them or adding it · `by hand` = the crafter typed their recipes and may know more: only for them say "
           "it can be added on the Me page or with /me professions (never by saying it in chat) · "
           "then the recipes of the game's list the question names with who makes them · a recipe the game's list doesn't "
           f"have: say \"{NOT_A_RECIPE}\" · a listed recipe nobody makes: say so (\"{nobody_sentence('<name>')}\") and, when "
           "every crafter of that profession is `armory`, that this is certain"]
    books: list[tuple] = []  # (character, profession def, recipes) for the crafters the question names
    q = norm_recipe(ask.question or "")
    hits = search(reg, ask.question or "", HELP_HITS)
    seen = {(e.id, e.key) for e in hits}
    hits += [e for e in phrase_hits(reg, ask.question or "", HELP_HITS, tails=True) if (e.id, e.key) not in seen][:max(0, HELP_HITS - len(hits))]
    if hits:  # first: the section is clipped at its cap and must never cut the answer itself
        out.append("the question's recipes in the game's list:")
        for e in hits:
            out.append(f"  - {safe(e.name)} [{safe(where_words(reg, e))}]: " + (", ".join(f"{safe(c.label)} ({skill_words(c.skill)})" for c in e.crafters[:6])
                                                                             if e.crafters else nobody_sentence(safe(e.name))))
    asked = {e.profession for e in hits}  # the professions the question is about lead; the rest follow
    first, rest = [], []
    for d in reg.profile.professions.values():
        rows = sorted(((c, m, p) for m, c in _rows(reg) for p in c.professions if p.id == d.id), key=lambda r: _by_skill(types.SimpleNamespace(skill=r[2].skill, label=r[0].label)))
        parts = []
        for c, m, p in rows[:12]:
            level = getattr(c.armory, "level", None) if c.armory else None
            kept = "armory" if c.armory is not None and c.profession_sync else "by hand"
            n = len(p.recipes)
            spec = specialization_of(reg, p)
            parts.append(f"{safe(c.label)} ({safe(m.display_name)}, {skill_words(p.skill)}, " + (f"{spec}, " if spec else "") + (f"level {level}, " if level else "")
                         + f"{kept}, {n} recipe{'' if n == 1 else 's'})")
            if p.recipes and any(k and re.search(rf"\b{re.escape(k)}\b", q) for k in (norm_recipe(c.label), norm_recipe(m.display_name))):
                books.append((c, d, p.recipes))
        name = norm_recipe(d.name)
        named = bool(name and re.search(rf"\b{re.escape(name)}\b" if " " in name else rf"\b{re.escape(name[:6])}", q))
        (first if d.id in asked or named else rest).append(f"{d.name}: " + (", ".join(parts) if parts else "nobody registered yet"))
    out += first
    named = [b for b in books if re.search(rf"\b{re.escape(norm_recipe(b[1].name)[:6])}", q)] or books  # the profession named too: only that book
    for c, d, recipes in named[:3]:
        names = [safe(r.name) for r in recipes]
        out.append(f"{safe(c.label)}'s {d.name} recipes ({len(names)}): " + ", ".join(names[:HELP_BOOK])
                   + (f" and {len(names) - HELP_BOOK} more (the Professions page has them all)" if len(names) > HELP_BOOK else ""))
    out += rest
    return out


HELP_BOOK = 60  # a named crafter's recipes in the help snapshot: the first sixty, then a count


def _help_section_registered(ask) -> list[str]:
    """The section for a game version WITHOUT the game's recipe list: what members registered, as before (§5.60)."""
    reg = ask.reg
    out = ["### Who can make what: professions with their crafters (highest skill first), then the notable recipes the guild has registered "
           "and who makes them · a question about a recipe nobody registered: say so"
           " and that a crafter adds it on the Me page or with /me professions (never by saying it in chat)"]

    def safe(text) -> str:  # member text (character, display and recipe names) can't open or close a block
        return unfence(text, "member-text").replace("\n", " ").strip()

    for d in reg.profile.professions.values():
        cs = crafters_of(reg, d.id)
        out.append(f"{d.name}: " + (", ".join(f"{safe(c.label)} ({safe(c.member)}, {skill_words(c.skill)})" for c in cs[:12]) if cs else "nobody registered yet"))
    cat = catalog(reg)
    out.append(f"recipes registered ({len(cat)}):" if cat else "recipes registered: none yet")
    for e in cat[:80]:
        out.append(f"- {safe(e.name)} [{safe(where_words(reg, e))}]: " + ", ".join(f"{safe(c.label)} ({skill_words(c.skill)})" for c in e.crafters[:6]))
    return out
