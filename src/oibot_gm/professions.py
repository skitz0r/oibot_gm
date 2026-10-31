"""Professions (design §5.60): the derived recipe catalog of a guild, the finders, the officers' rename and merge, the
help section and the words. Member data lives on RegisteredCharacter.professions (registry.py); game data in the
profile (professions.yaml). Nothing here pings, DMs or tracks a request; nothing reads a website."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .profiles import norm_recipe
from .registry import Registry, RegistryError


@dataclass
class Crafter:
    label: str      # the character
    member: str     # display name
    uid: int
    profession: str  # profile id
    skill: int


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
        e.crafters.sort(key=lambda x: (-x.skill, x.label.lower()))
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
    out = [Crafter(c.label, m.display_name, m.discord_id, p.id, p.skill) for m, c in _rows(reg) for p in c.professions if p.id == pid and p.skill >= min_skill]
    return sorted(out, key=lambda x: (-x.skill, x.label.lower()))


def _entry(reg: Registry, key: str) -> CatalogEntry:
    e = next((e for e in catalog(reg) if e.key == norm_recipe(key)), None)
    if e is None:
        raise RegistryError(f"Nobody has registered {key.strip() or 'that recipe'} yet.")
    return e


def _rewrite(reg: Registry, old_key: str, new_name: str, by: str, by_id: int | None, preview: bool) -> list[str]:
    """Every record holding old_key gets new_name: one table save per member, a row per touched character; returns one
    'record now reads' line per member."""
    touched: dict[int, tuple] = {}
    for m, c in list(_rows(reg)):
        if not any(norm_recipe(r.name) == old_key for p in c.professions for r in p.recipes):
            continue
        entries = [{"id": p.id, "skill": p.skill, "recipes": [{"name": new_name if norm_recipe(r.name) == old_key else r.name, "id": r.id} for r in p.recipes]} for p in c.professions]
        touched.setdefault(m.discord_id, (m, []))[1].append({"label": c.label, "professions": entries})
    said = []
    for m, rows in touched.values():
        said.append(f"{m.display_name}'s record now reads {new_name}.")
        if not preview:
            reg.save_character_table(m.discord_id, rows, by, officer=True, by_id=by_id)
    return said


def rename_sentences(reg: Registry, key: str, new_name: str) -> list[str]:
    e = _entry(reg, key)
    new_name = " ".join((new_name or "").split())
    if not new_name:
        raise RegistryError("A recipe needs a name.")
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


def op_rows(reg: Registry, c, field: str, value: str, hint: str | None) -> tuple[list[dict], str]:
    """The plain-text `character` op's profession fields → one table row {label, professions} and the describe line.
    profession: value 'Leatherworking 300' (skill optional: a new one starts at 1, an existing one keeps its skill);
    profession_remove: value = the profession; recipe_add / recipe_remove: value = the recipe name, `hint` (op.text) =
    the profession when the character has more than one that takes recipes."""
    cur = [p.model_dump() for p in c.professions]
    if field in ("profession", "profession_remove"):
        d, skill = _find_profession(reg, value)
        have = next((p for p in cur if p["id"] == d.id), None)
        if field == "profession_remove":
            if have is None:
                raise RegistryError(f"{c.label} has no {d.name} on record.")
            return [{"label": c.label, "professions": [p for p in cur if p["id"] != d.id]}], f"{c.label} · {d.name}: removed"
        if have is None:
            cur.append({"id": d.id, "skill": skill or 1, "recipes": []})
            return [{"label": c.label, "professions": cur}], f"{c.label} · {d.name}: {skill or 1}"
        old = have["skill"]
        have["skill"] = skill if skill is not None else old
        return [{"label": c.label, "professions": cur}], f"{c.label} · {d.name}: {old} → {have['skill']}"
    if field in ("recipe_add", "recipe_remove"):
        name = " ".join((value or "").split())
        if not name:
            raise RegistryError("A recipe needs a name.")
        takes = [p for p in cur if reg.profile.profession(p["id"]).group != "gathering"]
        if hint:
            d, _ = _find_profession(reg, hint)
            takes = [p for p in takes if p["id"] == d.id]
        if not takes:
            raise RegistryError(f"{c.label} has no profession that makes things on record — say which, like 'Leatherworking 300'.")
        if len(takes) > 1:
            names = [reg.profile.profession(p["id"]).name for p in takes]
            raise RegistryError(f"Which profession makes {name} — {' or '.join(names)}?")
        p = takes[0]
        pname = reg.profile.profession(p["id"]).name
        k = norm_recipe(name)
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


HELP_WORDS = re.compile(r"\b(profession|professions|craft\w*|make|makes|recipe|recipes|enchant\w*|who can|crafter|crafters|skill)\b")


def help_hit(reg: Registry, q: str) -> bool:
    """The question names a profession ('alchemist', 'leatherworking') or a registered recipe."""
    q = norm_recipe(q)
    for d in reg.profile.professions.values():
        stem = norm_recipe(d.name)[:6].strip()
        if stem and stem in q:
            return True
    return any(e.key in q for e in catalog(reg))


def help_section(ask) -> list[str]:
    """The help snapshot's professions section (members' view, design §5.60): each profession's crafters with skill,
    then the catalog with makers. Names only: the bot says who, never pings."""
    reg = ask.reg
    out = ["### Who can make what: professions with their crafters (highest skill first), then the notable recipes the guild has registered "
           "and who makes them · a question about a recipe nobody registered: say so and that a crafter adds it on the Me page, with "
           "/me professions or by saying 'I can make …' in the professions channel"]
    for d in reg.profile.professions.values():
        cs = crafters_of(reg, d.id)
        out.append(f"{d.name}: " + (", ".join(f"{c.label} ({c.member}, {c.skill})" for c in cs[:12]) if cs else "nobody registered yet"))
    cat = catalog(reg)
    out.append(f"recipes registered ({len(cat)}):" if cat else "recipes registered: none yet")
    for e in cat[:80]:
        out.append(f"- {e.name} [{reg.profile.profession(e.profession).name}]: " + ", ".join(f"{c.label} ({c.skill})" for c in e.crafters[:6]))
    return out
