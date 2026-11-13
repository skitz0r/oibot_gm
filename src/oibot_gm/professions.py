"""Professions (design §5.60): the derived recipe catalog of a guild, the finders, plain text's entry, the officers'
fix for recipes typed before the game's list, the help section and the words. Member data lives on
RegisteredCharacter.professions (registry.py); game data in the profile (professions.yaml, recipes.yaml). Nothing here
pings, DMs or tracks a request; nothing reads a website."""
from __future__ import annotations

import re
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


SKILL_UNSAID = "skill not said yet"


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
                e.crafters.append(Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill))
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


_INDEX: dict[int, _Index] = {}


def _index(profile) -> _Index:
    recipes = profile.recipes
    ix = _INDEX.get(id(recipes))
    if ix is None or ix.recipes is not recipes:
        by_key: dict[str, list] = {}
        for r in recipes.values():
            by_key.setdefault(r.key, []).append(r)
        ix = _Index(recipes, tuple((r, tuple(r.key.split()), frozenset(r.key.split())) for r in recipes.values()), by_key)
        _INDEX[id(recipes)] = ix
    return ix


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
    out = [Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill) for m, c in _rows(reg) for p in c.professions
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


def _find_profession(reg: Registry, text: str):
    """A profession by name or id in free text ('leatherworking', 'Leather working 300'): (ProfessionDef, skill | None)."""
    t = norm_recipe(text)
    skill = None
    m = re.search(r"\b(\d{1,3})\b", t)
    if m:
        skill = int(m.group(1))
        t = (t[:m.start()] + t[m.end():]).strip()
    key = t.replace(" ", "_")
    for d in reg.profile.professions.values():
        if key in (d.id, norm_recipe(d.name).replace(" ", "_")):
            return d, skill
    raise RegistryError(f"{t.title() or text.strip() or '?'} isn't a profession in this game version.")


PROFESSION_FIELDS = ("profession", "profession_remove", "recipe_add", "recipe_remove")


def default_character(reg: Registry, m, field: str):
    """The character a profession request means when the member named none (design §5.60: the player matters more than
    the character). A recipe field goes to the one character with a profession that makes things, else to the main when
    it has one; a profession field goes to the main. Several candidates and no main among them → the bot asks which."""
    chars = list(m.active())
    if not chars:
        raise RegistryError(f"{m.display_name} has no character on record yet.")
    if len(chars) == 1:
        return chars[0]
    main = m.main
    if field in ("recipe_add", "recipe_remove"):
        cands = [c for c in chars if any(has_recipes(reg, p.id) for p in c.professions)]
        if len(cands) == 1:
            return cands[0]
        if main is not None and (not cands or main in cands):
            return main
        names = [c.label for c in cands]
        raise RegistryError(f"Which character — {' or '.join(names)}?")
    if main is not None:
        return main
    raise RegistryError(f"Which character — {' or '.join(c.label for c in chars)}?")


class NeedsProfession(RegistryError):
    """A recipe whose profession the bot can't tell (design §5.60 follow-up): the character has no profession that makes
    things and nobody named one, or it has several. The message is the question; `candidates` are the profile ids a
    picker offers (profile order). Discord answers it with ProfessionPickView; the web and MCP print the sentence."""

    def __init__(self, character: str, recipe: str, candidates: list[str], message: str):
        super().__init__(message)
        self.character, self.recipe, self.candidates = character, recipe, list(candidates)


def has_recipes(reg: Registry, pid: str) -> bool:
    """The profession holds recipes. With the game's list: the list gives it any (R7 — Mining's smelting, Fishing's
    camp recipes); without: ProfessionDef.makes (not gathering, not a `recipes: false` secondary like Fishing)."""
    if reg.profile.has_recipe_list:
        return any(r.profession == pid for r in reg.profile.recipes.values())
    return reg.profile.profession(pid).makes


DID_YOU_MEAN = 5  # names a plain-text refusal offers when part of a name matches several recipes


def did_you_mean(names: list[str]) -> str:
    """'Did you mean Devilsaur Gauntlets or Devilsaur Leggings?' — up to DID_YOU_MEAN names, in full."""
    ns = names[:DID_YOU_MEAN]
    return f"Did you mean {ns[0] if len(ns) == 1 else ', '.join(ns[:-1]) + ' or ' + ns[-1]}?"


def _added(c, cur: list[dict], pid: str, recipe: dict, reg: Registry) -> tuple[list[dict], str]:
    """The recipe goes on with its profession, which the character doesn't have yet (shown before Apply, skill not said)."""
    cur.append({"id": pid, "skill": None, "recipes": [recipe]})
    return [{"label": c.label, "professions": cur}], f"{c.label} · {reg.profile.profession(pid).name}: added with {recipe['name']}"


def _recipe_add_listed(reg: Registry, c, cur: list[dict], name: str, hint: str | None) -> tuple[list[dict], str]:
    """recipe_add with the game's list (armory link §4.4): the name — exact, the whole words of a sentence, or part of a
    name — is resolved against the list and the character's professions. Several names → did_you_mean; one name in two
    professions the character holds (or, holding neither, could take) → NeedsProfession; one candidate is never a
    question. A profession not on record that the character could take is recorded with the recipe (shown before
    Apply). `hint` naming a profession on record narrows to it; a hint naming one not on record is a guess, used only
    when it makes the recipe. Twins in one profession → the lowest id (R6: nobody picks here)."""
    hits = list_hits(reg, name, longest=True)
    held = [p["id"] for p in cur]
    if hint:
        d, _ = _find_profession(reg, hint)
        if not has_recipes(reg, d.id):
            raise RegistryError(f"{d.name} has no recipes.")
        scoped = [h for h in hits if h.profession == d.id]
        if d.id in held and not scoped:
            raise RegistryError(f"{name} isn't a {d.name} recipe." if hits else f"{name} isn't a recipe in the game's list.")
        hits = scoped or hits
    if not hits:
        raise RegistryError(f"{name} isn't a recipe in the game's list.")
    mine = [h for h in hits if h.profession in held]
    if len({h.key for h in hits}) > 1 and len({h.key for h in mine}) == 1:
        hits = mine  # part of a name: the one recipe of that name the character's own professions make
    names = list(dict.fromkeys(h.name for h in mine + hits))  # the character's own professions' recipes named first
    if len({h.key for h in hits}) > 1:
        raise RegistryError(did_you_mean(names))
    best: dict[str, RecipeDef] = {}
    for h in sorted(hits, key=lambda x: x.id):
        best.setdefault(h.profession, h)
    profs = [pid for pid in reg.profile.professions if pid in best]
    pname = lambda pid: reg.profile.profession(pid).name  # noqa: E731
    on_record = [pid for pid in profs if pid in held]
    if len(on_record) > 1:
        raise NeedsProfession(c.label, names[0], on_record, f"Which profession makes {names[0]} — {' or '.join(pname(p) for p in on_record)}?")
    if on_record:
        pid = on_record[0]
        r = best[pid]
        p = next(p for p in cur if p["id"] == pid)
        if any(x.get("id") is not None and int(x["id"]) == r.id for x in p["recipes"]):
            raise RegistryError(f"{c.label} already has {r.name} on record.")
        p["recipes"].append({"id": r.id, "name": r.name})
        return [{"label": c.label, "professions": cur}], f"{c.label} · {pname(pid)} recipes: + {r.name}"
    fit = [pid for pid in profs if _fits(reg, cur, {"id": pid, "skill": None, "recipes": [{"id": best[pid].id}]}, c)]
    if not fit:
        raise RegistryError("A character has two trade professions — drop one first.")
    if len(fit) > 1:
        raise NeedsProfession(c.label, names[0], fit, f"Which profession makes {names[0]}?")
    r = best[fit[0]]
    return _added(c, cur, fit[0], {"id": r.id, "name": r.name}, reg)


def _recipe_remove_listed(reg: Registry, c, cur: list[dict], name: str, hint: str | None) -> tuple[list[dict], str]:
    """recipe_remove with the game's list: the typed words go through the finder's three tiers over the recipes THIS
    character holds (a listed one by its list name, an unmatched one by its typed key). One recipe → it comes off (a
    name the profession holds twice comes off whole); several → did_you_mean; one name in two professions → the
    question; none → "has no … on record". `hint` = the profession that holds it."""
    profs = cur
    if hint:
        d, _ = _find_profession(reg, hint)
        profs = [p for p in cur if p["id"] == d.id]
        if not profs:
            raise RegistryError(f"{c.label} has no {d.name} on record.")
    items = []
    for p in profs:
        for r in p["recipes"]:
            k = norm_recipe(r["name"])
            items.append(((p["id"], k, r["name"]), tuple(k.split()), frozenset(k.split())))
    hits = _tiers(norm_recipe(name), items, longest=True)
    if not hits:
        raise RegistryError(f"{c.label} has no {name} on record.")
    if len({k for _, k, _ in hits}) > 1:
        raise RegistryError(did_you_mean(list(dict.fromkeys(n for _, _, n in hits))))
    pids = [pid for pid in dict.fromkeys(pid for pid, _, _ in hits)]
    if len(pids) > 1:
        raise RegistryError(f"Which profession makes {hits[0][2]} — {' or '.join(reg.profile.profession(x).name for x in pids)}?")
    pid, k, shown = hits[0]
    p = next(p for p in cur if p["id"] == pid)
    p["recipes"] = [r for r in p["recipes"] if norm_recipe(r["name"]) != k]
    return [{"label": c.label, "professions": cur}], f"{c.label} · {reg.profile.profession(pid).name} recipes: − {shown}"


def _fits(reg: Registry, cur: list[dict], entry: dict, c=None) -> bool:
    """Whether character `c` (None: a character holding nothing) could hold `cur` with `entry` added (the hand check)."""
    try:
        reg.check_hand_professions(c, cur + [entry])
    except RegistryError:
        return False
    return True


def op_rows(reg: Registry, c, field: str, value: str, hint: str | None) -> tuple[list[dict], str]:
    """The plain-text `character` op's profession fields → one table row {label, professions} and the describe line.
    profession: value 'Leatherworking 300' (skill optional: a new one is recorded with its skill not said, an existing
    one keeps its skill); profession_remove: value = the profession; recipe_add / recipe_remove: value = the recipe name,
    `hint` (op.text) = the profession when the character has more than one that takes recipes — or, on a character with
    none that makes things, the profession to record WITH the recipe (the parser's guess or the picker's choice: shown
    and confirmed, never applied silently). With the game's list a recipe_add resolves against it (_recipe_add_listed).
    No hint and several candidates → NeedsProfession (the bot asks which); one candidate is never asked. A character
    whose professions follow the armory is refused first, in the write's own sentence (Registry.professions_refusal)."""
    why = reg.professions_refusal(c)
    if why:
        raise RegistryError(why)
    cur = reg.professions_entries(c)
    if field in ("profession", "profession_remove"):
        d, skill = _find_profession(reg, value)
        have = next((p for p in cur if p["id"] == d.id), None)
        if field == "profession_remove":
            if have is None:
                raise RegistryError(f"{c.label} has no {d.name} on record.")
            return [{"label": c.label, "professions": [p for p in cur if p["id"] != d.id]}], f"{c.label} · {d.name}: removed"
        if have is None:
            cur.append({"id": d.id, "skill": skill, "recipes": []})
            return [{"label": c.label, "professions": cur}], f"{c.label} · {d.name}: {skill if skill is not None else 'added'}"
        old = have["skill"]
        have["skill"] = skill if skill is not None else old
        return [{"label": c.label, "professions": cur}], f"{c.label} · {d.name}: {skill_words(old)} → {skill_words(have['skill'])}"
    if field in ("recipe_add", "recipe_remove"):
        name = " ".join((value or "").split())
        if not name:
            raise RegistryError("A recipe needs a name.")
        if field == "recipe_add" and reg.profile.has_recipe_list:
            return _recipe_add_listed(reg, c, cur, name, hint)
        if field == "recipe_remove" and reg.profile.has_recipe_list:
            return _recipe_remove_listed(reg, c, cur, name, hint)
        k = norm_recipe(name)
        takes = [p for p in cur if has_recipes(reg, p["id"])]
        if hint:
            d, _ = _find_profession(reg, hint)
            if not has_recipes(reg, d.id):
                raise RegistryError(f"{d.name} has no recipes.")
            if not any(p["id"] == d.id for p in cur):
                if field == "recipe_add" and not takes:  # nothing on record makes things: record the profession with the recipe
                    if d.id not in _fitting(reg, cur, name, c):  # a guess the character can't hold becomes the question, never a promise
                        return _needs_or_one(reg, c, cur, name)
                    return _added(c, cur, d.id, {"name": name, "id": None}, reg)
                raise RegistryError(f"{c.label} has no {d.name} on record.")
            takes = [p for p in takes if p["id"] == d.id]
        if field == "recipe_remove":  # a remove names the profession that holds it; it asks which only when several do
            takes = [p for p in takes if any(norm_recipe(r["name"]) == k for r in p["recipes"])]
            if not takes:
                raise RegistryError(f"{c.label} has no {name} on record.")
        if not takes:
            return _needs_or_one(reg, c, cur, name)
        if len(takes) > 1:
            names = [reg.profile.profession(p["id"]).name for p in takes]
            if field == "recipe_add":
                raise NeedsProfession(c.label, name, [p["id"] for p in takes], f"Which profession makes {name} — {' or '.join(names)}?")
            raise RegistryError(f"Which profession makes {name} — {' or '.join(names)}?")
        p = takes[0]
        pname = reg.profile.profession(p["id"]).name
        if field == "recipe_add":
            if any(norm_recipe(r["name"]) == k for r in p["recipes"]):
                raise RegistryError(f"{c.label} already has {name} on record.")
            p["recipes"].append({"name": name, "id": None})
            return [{"label": c.label, "professions": cur}], f"{c.label} · {pname} recipes: + {name}"
        hit = next((r for r in p["recipes"] if norm_recipe(r["name"]) == k), None)
        if hit is None:
            raise RegistryError(f"{c.label} has no {name} on record.")
        p["recipes"] = [r for r in p["recipes"] if r is not hit]
        return [{"label": c.label, "professions": cur}], f"{c.label} · {pname} recipes: − {hit['name']}"
    raise ValueError(field)


def _fitting(reg: Registry, cur: list[dict], recipe: str, c=None) -> list[str]:
    """The professions that make things which character `c` could still take with the recipe (profile order): each
    candidate tried on the whole table — a character whose two trades are taken can only add Cooking or First Aid."""
    return [d.id for d in reg.profile.professions.values() if has_recipes(reg, d.id) and not any(p["id"] == d.id for p in cur)
            and _fits(reg, cur, {"id": d.id, "skill": None, "recipes": [{"name": recipe, "id": None}]}, c)]


def _needs_or_one(reg: Registry, c, cur: list[dict], recipe: str) -> tuple[list[dict], str]:
    """A recipe on a character with no profession that makes things (a version without the game's list): the one
    profession that fits is recorded with it (shown before Apply); several → the question, offering only what fits;
    none → the one refusal."""
    fit = _fitting(reg, cur, recipe, c)
    if len(fit) == 1:
        return _added(c, cur, fit[0], {"name": recipe, "id": None}, reg)
    if not fit:
        raise RegistryError("A character has two trade professions — drop one first.")
    raise NeedsProfession(c.label, recipe, fit, f"Which profession makes {recipe}?")


def added_with_recipe(c, rows: list[dict]) -> str | None:
    """The profession a recipe_add recorded along with its recipe (a character that had none making things), else None."""
    have = {p.id for p in c.professions}
    return next((p["id"] for p in rows[0]["professions"] if p["id"] not in have and p.get("recipes")), None) if rows else None


def preview_sentences(reg: Registry, m, c, field: str, rows: list[dict], *, by_id: int | None = None) -> list[str]:
    """What a plain-text profession change will do, in the confirm's sentences (the Confirm rule): the Discord diff, the
    site's text box and MCP previews print these instead of a describe bullet. `by_id` = the asker ("your" on their own
    record). The profession fields reuse consequences.profession_consequence; a recipe names the character."""
    from . import consequences as cq

    why = reg.professions_refusal(c, rows[0]["professions"]) if rows else None  # the write's own check, before any sentence
    if why:
        raise RegistryError(why)
    v = cq._Voice(reg, m, by_id)
    whose = "Your" if v.own else f"{c.label}'s"
    if field in ("profession", "profession_remove"):
        return cq.profession_consequence(reg, m, c.label, rows[0]["professions"], by_id=by_id)
    before = {p.id: {norm_recipe(r.name): r.name for r in p.recipes} for p in c.professions}
    after = {p["id"]: {norm_recipe(r["name"]): r["name"] for r in p.get("recipes") or []} for p in rows[0]["professions"]}
    new_pid = added_with_recipe(c, rows) if field == "recipe_add" else None
    if new_pid:
        pname, recipe = reg.profile.profession(new_pid).name, next(iter(after[new_pid].values()))
        return [f"{c.label} has no {pname} on record yet.",
                f"Apply records {pname} on {c.label} and adds {recipe} to the recipes {c.label} makes.",
                f'Say the skill when you know it, like "{c.label} is 300 {pname}".']
    for pid, names in after.items():
        old = before.get(pid, {})
        pname = reg.profile.profession(pid).name
        if field == "recipe_add" and (gained := [n for k, n in names.items() if k not in old]):
            return [f"{whose} {pname} gains {gained[0]}."]
        if field == "recipe_remove" and (lost := [n for k, n in old.items() if k not in names]):
            return [f"{whose} {pname} no longer lists {lost[0]}."]
    raise RegistryError(f"{c.label}'s professions are already as given.")


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
        return any(len(kw) > 1 and ks <= words for _, kw, ks in _index(reg.profile).rows)
    return False


HELP_HITS = 10  # the question's recipes from the game's list in the help snapshot: never the whole list in a prompt


def help_section(ask) -> list[str]:
    """The help snapshot's professions section (members' view, design §5.60): each profession's crafters with skill,
    then the catalog with makers, then (with the game's list) up to HELP_HITS recipes the question names, each with its
    crafters or nobody_sentence. Names only: the bot says who, never pings."""
    reg = ask.reg
    listed = reg.profile.has_recipe_list
    out = ["### Who can make what: professions with their crafters (highest skill first), then the notable recipes the guild has registered "
           "and who makes them" + (", then the recipes of the game's list the question names · a recipe the game's list doesn't have: "
                                   f"say \"{NOT_A_RECIPE}\" · a listed recipe nobody makes: say so (\"{nobody_sentence('<name>')}\")" if listed
                                   else " · a question about a recipe nobody registered: say so")
           + " and that a crafter adds it on the Me page, with /me professions or by saying 'I can make …' in the professions channel"]
    def safe(text) -> str:  # member text (character, display and recipe names) can't open or close a block
        return unfence(text, "member-text").replace("\n", " ").strip()

    for d in reg.profile.professions.values():
        cs = crafters_of(reg, d.id)
        out.append(f"{d.name}: " + (", ".join(f"{safe(c.label)} ({safe(c.member)}, {skill_words(c.skill)})" for c in cs[:12]) if cs else "nobody registered yet"))
    cat = catalog(reg)
    out.append(f"recipes registered ({len(cat)}):" if cat else "recipes registered: none yet")
    for e in cat[:80]:
        out.append(f"- {safe(e.name)} [{safe(where_words(reg, e))}]: " + ", ".join(f"{safe(c.label)} ({skill_words(c.skill)})" for c in e.crafters[:6]))
    hits = search(reg, ask.question or "", HELP_HITS) if listed else []
    if hits:
        out.append("the question's recipes in the game's list:")
        for e in hits:
            out.append(f"  - {safe(e.name)} [{safe(where_words(reg, e))}]: " + (", ".join(f"{safe(c.label)} ({skill_words(c.skill)})" for c in e.crafters[:6])
                                                                             if e.crafters else nobody_sentence(safe(e.name))))
    return out
