"""Professions (design §5.60): the derived recipe catalog of a guild, the finders, the officers' rename and merge, the
help section and the words. Member data lives on RegisteredCharacter.professions (registry.py); game data in the
profile (professions.yaml). Nothing here pings, DMs or tracks a request; nothing reads a website."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm.provider import unfence
from .profiles import norm_recipe
from .registry import RECIPE_NAME_MAX, Registry, RegistryError

COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")


def count_word(n: int) -> str:
    """A count in words up to twelve, digits beyond (the Confirm rule's folded sentences)."""
    return COUNT_WORDS[n] if 0 <= n < len(COUNT_WORDS) else str(n)


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
    name: str        # the spelling most crafters use (ties: the first alphabetically)
    profession: str
    crafters: list[Crafter] = field(default_factory=list)
    id: int | None = None


def visible(reg: Registry, m, c) -> bool:
    """A crafter the finder may name: an active character of a member who is in the server and not a puppet."""
    return c.status == "active" and not m.test and not getattr(m, "left_at", None)


def _rows(reg: Registry):
    for m in reg.members.values():
        for c in m.active():
            if visible(reg, m, c):
                yield m, c


def catalog(reg: Registry) -> list[CatalogEntry]:
    by_key: dict[str, CatalogEntry] = {}
    spellings: dict[str, dict[str, int]] = {}
    for m, c in _rows(reg):
        for p in c.professions:
            for r in p.recipes:
                k = norm_recipe(r.name)
                e = by_key.setdefault(k, CatalogEntry(key=k, name=r.name, profession=p.id, id=r.id))
                e.crafters.append(Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill))
                spellings.setdefault(k, {})[r.name] = spellings.get(k, {}).get(r.name, 0) + 1
                if e.id is None and r.id:
                    e.id = r.id
    for e in by_key.values():
        e.name = sorted(spellings[e.key].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        e.crafters.sort(key=_by_skill)
    return sorted(by_key.values(), key=lambda e: e.name.lower())


def who_can_make(reg: Registry, text: str) -> list[CatalogEntry]:
    """Exact normalised name first; else every recipe whose words all appear in the text (whole words)."""
    q = norm_recipe(text)
    if not q:
        return []
    cat = catalog(reg)
    exact = [e for e in cat if e.key == q]
    if exact:
        return exact
    words = set(q.split())
    return [e for e in cat if set(e.key.split()) <= words]


def crafters_of(reg: Registry, pid: str, min_skill: int = 0) -> list[Crafter]:
    out = [Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill) for m, c in _rows(reg) for p in c.professions
           if p.id == pid and (not min_skill or (p.skill is not None and p.skill >= min_skill))]
    return sorted(out, key=_by_skill)


def _entry(reg: Registry, key: str) -> CatalogEntry:
    e = next((e for e in catalog(reg) if e.key == norm_recipe(key)), None)
    if e is None:
        raise RegistryError(f"Nobody has registered {key.strip() or 'that recipe'} yet.")
    return e


def _rewrite(reg: Registry, old_key: str, new_name: str, by: str, by_id: int | None, preview: bool) -> list[str]:
    """Every record holding old_key gets new_name: one table save per member, a row per touched character. Every
    touched record is validated (Registry.check_professions) BEFORE the first write, so a refusal leaves nothing half
    written. Returns one 'record now reads' line per member for one or two members, else ONE folded sentence
    ("Six members' records now read …") — the Confirm rule's three sentences with the head."""
    new_key = norm_recipe(new_name)
    touched: dict[int, tuple] = {}
    for m, c in list(_rows(reg)):
        if not any(norm_recipe(r.name) == old_key for p in c.professions for r in p.recipes):
            continue
        entries = []
        for p in c.professions:
            # a case-only rename keeps its key: the old entry IS the target, rewritten in place, never dropped
            has_new = new_key != old_key and any(norm_recipe(r.name) == new_key for r in p.recipes)
            recipes = []
            for r in p.recipes:
                if norm_recipe(r.name) == old_key:
                    if has_new:  # a merge onto a spelling this record already holds: one entry stays
                        continue
                    recipes.append({"name": new_name, "id": r.id})
                    has_new = True
                else:
                    recipes.append({"name": r.name, "id": r.id})
            entries.append({"id": p.id, "skill": p.skill, "recipes": recipes})
        touched.setdefault(m.discord_id, (m, []))[1].append({"label": c.label, "professions": entries})
    for m, rows in touched.values():
        for row in rows:
            reg.check_professions(row["professions"])
    if len(touched) > 2:
        said = [f"{count_word(len(touched)).capitalize()} members' records now read {new_name}."]
    else:
        said = [f"{m.display_name}'s record now reads {new_name}." for m, _ in touched.values()]
    if not preview:
        for m, rows in touched.values():
            reg.save_character_table(m.discord_id, rows, by, officer=True, by_id=by_id)
    return said


def rename_sentences(reg: Registry, key: str, new_name: str) -> list[str]:
    e = _entry(reg, key)
    new_name = " ".join((new_name or "").split())
    if not new_name:
        raise RegistryError("A recipe needs a name.")
    if len(new_name) > RECIPE_NAME_MAX:
        raise RegistryError(f"A recipe name is up to {RECIPE_NAME_MAX} characters.")
    if norm_recipe(new_name) == e.key and new_name == e.name:
        raise RegistryError(f"{e.name} already reads that way.")
    other = next((x for x in catalog(reg) if x.key == norm_recipe(new_name) and x.key != e.key), None)
    if other is not None:
        raise RegistryError(f"{other.name} is already a recipe — merge into it instead.")
    return [f"{e.name} is renamed {new_name}."]


def rename_recipe(reg: Registry, key: str, new_name: str, by: str, *, by_id: int | None = None, preview: bool = False) -> list[str]:
    head = rename_sentences(reg, key, new_name)
    return head + _rewrite(reg, _entry(reg, key).key, " ".join(new_name.split()), by, by_id, preview)


def merge_sentences(reg: Registry, key: str, into_key: str) -> list[str]:
    e, into = _entry(reg, key), _entry(reg, into_key)
    if e.key == into.key:
        raise RegistryError(f"{e.name} is already one recipe.")
    if e.profession != into.profession:
        raise RegistryError(f"{e.name} and {into.name} belong to different professions.")
    return [f"{e.name} is merged into {into.name}."]


def merge_recipe(reg: Registry, key: str, into_key: str, by: str, *, by_id: int | None = None, preview: bool = False) -> list[str]:
    head = merge_sentences(reg, key, into_key)
    into = _entry(reg, into_key)
    return head + _rewrite(reg, _entry(reg, key).key, into.name, by, by_id, preview)


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


def _makes(reg: Registry, pid: str) -> bool:
    """The profession holds recipes (ProfessionDef.makes: not gathering, not a `recipes: false` secondary like Fishing)."""
    return reg.profile.profession(pid).makes


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
        cands = [c for c in chars if any(_makes(reg, p.id) for p in c.professions)]
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


def op_rows(reg: Registry, c, field: str, value: str, hint: str | None) -> tuple[list[dict], str]:
    """The plain-text `character` op's profession fields → one table row {label, professions} and the describe line.
    profession: value 'Leatherworking 300' (skill optional: a new one is recorded with its skill not said, an existing
    one keeps its skill); profession_remove: value = the profession; recipe_add / recipe_remove: value = the recipe name,
    `hint` (op.text) = the profession when the character has more than one that takes recipes — or, on a character with
    none that makes things, the profession to record WITH the recipe (the parser's guess or the picker's choice: shown
    and confirmed, never applied silently). No hint there → NeedsProfession (the bot asks which)."""
    cur = [p.model_dump() for p in c.professions]
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
        k = norm_recipe(name)
        takes = [p for p in cur if _makes(reg, p["id"])]
        if hint:
            d, _ = _find_profession(reg, hint)
            if not d.makes:
                raise RegistryError(f"{d.name} has no recipes.")
            if not any(p["id"] == d.id for p in cur):
                if field == "recipe_add" and not takes:  # nothing on record makes things: record the profession with the recipe
                    cur.append({"id": d.id, "skill": None, "recipes": [{"name": name, "id": None}]})
                    return [{"label": c.label, "professions": cur}], f"{c.label} · {d.name}: added with {name}"
                raise RegistryError(f"{c.label} has no {d.name} on record.")
            takes = [p for p in takes if p["id"] == d.id]
        if field == "recipe_remove":  # a remove names the profession that holds it; it asks which only when several do
            takes = [p for p in takes if any(norm_recipe(r["name"]) == k for r in p["recipes"])]
            if not takes:
                raise RegistryError(f"{c.label} has no {name} on record.")
        if not takes:
            makers = [d.id for d in reg.profile.professions.values() if d.makes]
            raise NeedsProfession(c.label, name, makers, f"Which profession makes {name}?")
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


def added_with_recipe(c, rows: list[dict]) -> str | None:
    """The profession a recipe_add recorded along with its recipe (a character that had none making things), else None."""
    have = {p.id for p in c.professions}
    return next((p["id"] for p in rows[0]["professions"] if p["id"] not in have and p.get("recipes")), None) if rows else None


def preview_sentences(reg: Registry, m, c, field: str, rows: list[dict], *, by_id: int | None = None) -> list[str]:
    """What a plain-text profession change will do, in the confirm's sentences (the Confirm rule): the Discord diff, the
    site's text box and MCP previews print these instead of a describe bullet. `by_id` = the asker ("your" on their own
    record). The profession fields reuse consequences.profession_consequence; a recipe names the character."""
    from . import consequences as cq

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
    return any(re.search(rf"\b{re.escape(e.key)}\b", q) for e in catalog(reg) if e.key)


def help_section(ask) -> list[str]:
    """The help snapshot's professions section (members' view, design §5.60): each profession's crafters with skill,
    then the catalog with makers. Names only: the bot says who, never pings."""
    reg = ask.reg
    out = ["### Who can make what: professions with their crafters (highest skill first), then the notable recipes the guild has registered "
           "and who makes them · a question about a recipe nobody registered: say so and that a crafter adds it on the Me page, with "
           "/me professions or by saying 'I can make …' in the professions channel"]
    def safe(text) -> str:  # member text (character, display and recipe names) can't open or close a block
        return unfence(text, "member-text").replace("\n", " ").strip()

    for d in reg.profile.professions.values():
        cs = crafters_of(reg, d.id)
        out.append(f"{d.name}: " + (", ".join(f"{safe(c.label)} ({safe(c.member)}, {skill_words(c.skill)})" for c in cs[:12]) if cs else "nobody registered yet"))
    cat = catalog(reg)
    out.append(f"recipes registered ({len(cat)}):" if cat else "recipes registered: none yet")
    for e in cat[:80]:
        out.append(f"- {safe(e.name)} [{reg.profile.profession(e.profession).name}]: " + ", ".join(f"{safe(c.label)} ({skill_words(c.skill)})" for c in e.crafters[:6]))
    return out
