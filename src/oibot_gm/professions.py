"""Professions (design §5.60): the derived recipe catalog of a guild, the finders, the officers' rename and merge, the
help section and the words. Member data lives on RegisteredCharacter.professions (registry.py); game data in the
profile (professions.yaml). Nothing here pings, DMs or tracks a request; nothing reads a website."""
from __future__ import annotations

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
    """Every record holding old_key gets new_name (one table save per member); returns the 'record now reads' lines."""
    said = []
    for m, c in list(_rows(reg)):
        hit = any(norm_recipe(r.name) == old_key for p in c.professions for r in p.recipes)
        if not hit:
            continue
        entries = [{"id": p.id, "skill": p.skill, "recipes": [{"name": new_name if norm_recipe(r.name) == old_key else r.name, "id": r.id} for r in p.recipes]} for p in c.professions]
        said.append(f"{m.display_name}'s record now reads {new_name}.")
        if not preview:
            reg.save_character_table(m.discord_id, [{"label": c.label, "professions": entries}], by, officer=True, by_id=by_id)
    return said


def rename_sentences(reg: Registry, key: str, new_name: str) -> list[str]:
    e = _entry(reg, key)
    new_name = " ".join((new_name or "").split())
    if not new_name:
        raise RegistryError("A recipe needs a name.")
    if norm_recipe(new_name) == e.key and new_name == e.name:
        raise RegistryError(f"{e.name} already reads that way.")
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
