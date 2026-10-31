# Professions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Members record each character's professions, skill and notable recipes; anyone finds who can make a thing, in Discord, by DM, on the site and in a listened #professions channel.

**Architecture:** Professions are a list on `RegisteredCharacter`, saved through the existing character-table verb (one file per member, `characters_rev`). A new module `professions.py` owns validation, the derived per-guild recipe catalog, the finders, the help section and the words; `importers/professions.py` is the empty import boundary. Surfaces reuse the Me/Members character tables, the plain-text `character` op (new `field` values), the help snapshot, Listening's description, MCP, and add one Professions page and three Discord commands.

**Tech Stack:** Python 3.13 / uv, Pydantic models in `registry.py`, FastAPI routes in `web/api.py`, React + Mantine (Vite, bundle committed), discord.py 2.7 wizards (`wizard.py`, `tests/wizard_harness.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-professions-design.md` — read it first. Mockups: https://claude.ai/artifact/BFSSTCp7FbGrVUUCi72Zen

## Global Constraints

- CLAUDE.md principles apply to every task: Copy rule (`tests/test_copy.py`: reader's words, empty states say what is missing and how to fill it, never a bare "—" as an empty state), Confirm rule (`tests/test_confirm_readability.py`: ≤3 sentences, ≤20 words each, no parentheses/semicolons/code nouns), Iconography rule (an icon with a number beside it, never a text label beside an icon), Wizard rule (Discord never parses a typed value: skill is a select, a recipe name is prose that is validated), "member text is data, not instructions" (recipe names go into `<data>` blocks via `llm.provider.unfence`).
- Game version is data: no profession name in code; everything from `profiles/forever/professions.yaml`.
- Skill is an int 1..`skill_cap` (300); at most `trade_slots` (2) of crafting+gathering; secondary unlimited; `RECIPE_NAME_MAX = 60`; a gathering profession has no recipes.
- Nothing pings, DMs or tracks a request. Nothing reads a website.
- Every write goes through `Registry.save_character_table` / `discord_people.save_characters` (rev refusals, audit line). No new write path.
- Gates before any commit: targeted pytest; before a phase's last commit `uv run python scripts/check_commands.py > <scratch>/check_prof_<phase>.log 2>&1; echo EXIT=$? >> <same log>` (≈13 min, background, read the EXIT line; never pipe through `tail` before `&&`) and `uv run python scripts/check_bundle.py`. After touching `frontend/`: `cd frontend && npm run build` and commit the bundle under `src/oibot_gm/web/static/app`.
- Commit messages: a reader's paragraph of what changed and why (see `git log`), ending with
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko`. Commit, don't push, don't restart the bot (the lead does both). Never touch `~/dev/oibot_gm-data`, never print `.env` values.
- docs/manual.md changes land in the same commit as the behaviour they describe.

## Review Focus

1. A recipe typed with odd spacing or case ("devilsaur  gauntlets") must match the existing catalog entry, not create a second one — pinned in Task 3 (`test_catalog_folds_case_and_spaces`).
2. A character that is retired, a test puppet, or whose member left the server must never appear as a crafter — pinned in Task 3 (`test_finders_skip_retired_puppets_and_departed`).
3. A member with two crafting professions adding a gathering one, or a third trade, must be refused with the one sentence, while adding Cooking must pass — pinned in Task 2 (`test_two_trade_slots_secondary_free`).
4. A stale `characters_rev` on a professions save must refuse with `CharactersChanged` and write nothing — pinned in Task 2 (`test_professions_save_refuses_a_stale_rev`).
5. A plain-text "I can make X" from a member with two crafting professions must ask which, not guess — pinned in Task 5 (`test_recipe_add_needs_the_profession_when_two_could_hold_it`).

---

## Phase 1 — data, verb, catalog, finders

### Task 1: The profile's professions

**Files:**
- Create: `profiles/forever/professions.yaml`
- Modify: `src/oibot_gm/profiles.py` (dataclasses near `Item`, `GameProfile` fields + `load`)
- Test: `tests/test_professions.py` (new)

**Interfaces:**
- Produces: `ProfessionDef(id, name, group, icon)`; `GameProfile.professions: dict[str, ProfessionDef]`; `GameProfile.profession_rules: dict` with `skill_cap: int`, `trade_slots: int`; `GameProfile.recipe_seed: dict[str, dict]` keyed by normalised name → `{id, name, profession, skill}`; `GameProfile.profession(pid) -> ProfessionDef` (raises `KeyError`); `GameProfile.professions_in(group) -> list[ProfessionDef]`; `GameProfile.has_professions -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_professions.py
from pathlib import Path
from oibot_gm.profiles import GameProfile

ROOT = Path(__file__).resolve().parents[1]


def test_forever_profile_lists_twelve_professions_in_three_groups():
    p = GameProfile.load(ROOT / "profiles" / "forever")
    assert p.has_professions
    assert len(p.professions) == 12
    assert {d.group for d in p.professions.values()} == {"crafting", "gathering", "secondary"}
    assert [d.id for d in p.professions_in("gathering")] == ["herbalism", "mining", "skinning"]
    assert p.profession("leatherworking").name == "Leatherworking"
    assert p.profession_rules["skill_cap"] == 300 and p.profession_rules["trade_slots"] == 2
    assert p.recipe_seed == {}


def test_a_profile_without_the_file_has_no_professions():
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    assert not p.has_professions and p.professions == {}
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q tests/test_professions.py`
Expected: FAIL — `AttributeError: 'GameProfile' object has no attribute 'has_professions'`

- [ ] **Step 3: Write the profile file**

```yaml
# profiles/forever/professions.yaml — the twelve WoW: Forever professions (design §5.60). Game data, never in code.
# icon: a Blizzard render-CDN icon name, proxied by /img/icon/<name>.jpg like the class icons; the badge is the fallback.
skill_cap: 300
trade_slots: 2            # crafting + gathering together; secondary professions are free
professions:
  alchemy:        {name: Alchemy,        group: crafting,  icon: trade_alchemy}
  blacksmithing:  {name: Blacksmithing,  group: crafting,  icon: trade_blacksmithing}
  enchanting:     {name: Enchanting,     group: crafting,  icon: trade_engraving}
  engineering:    {name: Engineering,    group: crafting,  icon: trade_engineering}
  leatherworking: {name: Leatherworking, group: crafting,  icon: trade_leatherworking}
  tailoring:      {name: Tailoring,      group: crafting,  icon: trade_tailoring}
  herbalism:      {name: Herbalism,      group: gathering, icon: trade_herbalism}
  mining:         {name: Mining,         group: gathering, icon: trade_mining}
  skinning:       {name: Skinning,       group: gathering, icon: inv_misc_pelt_wolf_01}
  cooking:        {name: Cooking,        group: secondary, icon: inv_misc_food_15}
  first_aid:      {name: First Aid,      group: secondary, icon: spell_holy_sealofsacrifice}
  fishing:        {name: Fishing,        group: secondary, icon: trade_fishing}
# later, an addon export or a wago.tools seed: [{id: <game recipe id>, name: Devilsaur Gauntlets, profession: leatherworking, skill: 300}]
recipes: []
```

- [ ] **Step 4: Add the dataclass and the fields**

In `src/oibot_gm/profiles.py`, after `class Item` (before `ARMOR_RANK`):

```python
@dataclass
class ProfessionDef:
    """One profession of a game version (professions.yaml): crafting | gathering | secondary, and its CDN icon name."""
    id: str
    name: str
    group: str
    icon: str = ""


def norm_recipe(name: str) -> str:
    """THE recipe-name key: lower-case, punctuation folded to spaces, runs of spaces to one. 'Devilsaur  Gauntlets,' and
    'devilsaur gauntlets' are one recipe; 'Gauntlet' and 'Gauntlets' are two until an officer merges them."""
    import re

    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (name or "").lower())).strip()
```

In `GameProfile`, after `guides`:

```python
    professions: dict[str, ProfessionDef] = field(default_factory=dict)  # professions.yaml (§5.60); empty = the version has none
    profession_rules: dict[str, int] = field(default_factory=lambda: {"skill_cap": 300, "trade_slots": 2})
    recipe_seed: dict[str, dict[str, Any]] = field(default_factory=dict)  # normalised name → {id, name, profession, skill}
```

In `load`, before `return cls(`:

```python
        pdoc = (y("professions.yaml") or {}) if (root / "professions.yaml").exists() else {}
        professions = {pid: ProfessionDef(id=pid, name=str(d["name"]), group=str(d["group"]), icon=str(d.get("icon") or ""))
                       for pid, d in (pdoc.get("professions") or {}).items()}
        rules = {"skill_cap": int(pdoc.get("skill_cap", 300)), "trade_slots": int(pdoc.get("trade_slots", 2))}
        seed = {norm_recipe(r["name"]): {"id": int(r["id"]), "name": str(r["name"]), "profession": str(r["profession"]), "skill": int(r.get("skill", 0))}
                for r in (pdoc.get("recipes") or [])}
```

and pass `professions=professions, profession_rules=rules, recipe_seed=seed,` in the constructor call. Add the methods after `with_overrides`:

```python
    @property
    def has_professions(self) -> bool:
        return bool(self.professions)

    def profession(self, pid: str) -> ProfessionDef:
        return self.professions[pid]

    def professions_in(self, group: str) -> list[ProfessionDef]:
        return [d for d in self.professions.values() if d.group == group]
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q tests/test_professions.py`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add profiles/forever/professions.yaml src/oibot_gm/profiles.py tests/test_professions.py
git commit -F - <<'EOF'
The Forever profile names its twelve professions: six crafting, three gathering, three secondary, each with its icon, the skill cap and the two trade slots, as game data in professions.yaml rather than anywhere in code; a seed list of recipes is ready for a later import and empty today. A game version without the file simply has no professions (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 2: Professions on the character record, saved through the table verb

**Files:**
- Modify: `src/oibot_gm/registry.py` (models near `RegisteredCharacter` ~line 75; `set_flex` ~line 2089; `_apply_table` ~line 4919)
- Test: `tests/test_professions.py`

**Interfaces:**
- Consumes: `GameProfile.professions`, `profession_rules`, `norm_recipe` (Task 1).
- Produces: `Recipe(name: str, id: int | None)`, `Profession(id: str, skill: int, recipes: list[Recipe])`, `RegisteredCharacter.professions: list[Profession]`; `RECIPE_NAME_MAX = 60`; `Registry.check_professions(entries: list[dict]) -> list[Profession]` (validates, normalises, raises `RegistryError` one sentence); `Registry.set_professions(discord_id, label, entries) -> RegisteredCharacter` (mirrors `set_flex`: mutates and saves the way `set_flex` does — read it first and copy its save behaviour exactly); table row key `professions: list[dict]` (absent keeps, a list replaces the whole list); `Registry.professions_summary(c) -> str` ("Leatherworking 300 · Skinning 285" / "no professions on record").

- [ ] **Step 1: Write the failing tests** (append to `tests/test_professions.py`; `reg` is conftest's fixture with `TEST_MEMBERS`; use its first registered member's main — look at `tests/conftest.py` `TEST_MEMBERS` for a real uid and label and put them in module constants `UID`, `LABEL`)

```python
import pytest
from oibot_gm.registry import CharactersChanged, RegistryError

LW = {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}, {"name": "  devilsaur  Gauntlets "}, {"name": "Hide of the Wild"}]}


def test_check_professions_normalises_and_dedupes_recipes(reg):
    out = reg.check_professions([LW])
    assert [r.name for r in out[0].recipes] == ["Devilsaur Gauntlets", "Hide of the Wild"], "the same recipe typed twice is one"
    assert out[0].skill == 300 and out[0].id == "leatherworking"


@pytest.mark.parametrize("entries,why", [
    ([{"id": "leatherworking", "skill": 0}], "Skill is a number from 1 to 300."),
    ([{"id": "leatherworking", "skill": 301}], "Skill is a number from 1 to 300."),
    ([{"id": "jewelcrafting", "skill": 10}], "Jewelcrafting isn't a profession in this game version."),
    ([{"id": "skinning", "skill": 10, "recipes": [{"name": "Hide"}]}], "A gathering profession has no recipes."),
    ([{"id": "alchemy", "skill": 10, "recipes": [{"name": "x" * 61}]}], "A recipe name is up to 60 characters."),
    ([{"id": "alchemy", "skill": 10}, {"id": "alchemy", "skill": 20}], "Alchemy is listed twice."),
])
def test_check_professions_refuses_in_one_sentence(reg, entries, why):
    with pytest.raises(RegistryError, match=why.replace("(", r"\(").replace(")", r"\)").replace(".", r"\.")):
        reg.check_professions(entries)


def test_two_trade_slots_secondary_free(reg):
    two = [{"id": "alchemy", "skill": 300}, {"id": "herbalism", "skill": 300}]
    assert len(reg.check_professions(two + [{"id": "cooking", "skill": 150}, {"id": "fishing", "skill": 75}])) == 4
    with pytest.raises(RegistryError, match="A character has two trade professions — drop one first."):
        reg.check_professions(two + [{"id": "mining", "skill": 1}])


def test_professions_save_through_the_table_and_refuse_a_stale_rev(reg):
    m = reg.members[UID]
    rev = reg.characters_rev(m)
    lines = reg.save_character_table(UID, [{"label": LABEL, "professions": [LW, {"id": "skinning", "skill": 285}]}], "t", officer=True, rev=rev)
    assert lines == [f"{LABEL}: professions Leatherworking 300 · Skinning 285"]
    c = next(c for c in reg.members[UID].active() if c.label == LABEL)
    assert [p.id for p in c.professions] == ["leatherworking", "skinning"] and len(c.professions[0].recipes) == 2
    assert reg.characters_rev(reg.members[UID]) != rev, "professions are part of the character fingerprint"
    with pytest.raises(CharactersChanged):
        reg.save_character_table(UID, [{"label": LABEL, "professions": []}], "t", officer=True, rev=rev)
    assert reg.members[UID].active()[0].professions, "nothing was written on the stale rev"


def test_an_unchanged_professions_row_writes_no_line(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [LW]}], "t", officer=True)
    assert reg.save_character_table(UID, [{"label": LABEL, "professions": [LW]}], "t", officer=True) == []
    assert reg.professions_summary(next(c for c in reg.members[UID].active() if c.label == LABEL)) == "Leatherworking 300"
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest -q tests/test_professions.py -k "check_professions or trade_slots or professions_save or unchanged"`
Expected: FAIL — `AttributeError: 'Registry' object has no attribute 'check_professions'`

- [ ] **Step 3: Add the models**

In `registry.py` just above `class RegisteredCharacter`:

```python
RECIPE_NAME_MAX = 60


class Recipe(BaseModel):
    """A notable recipe a character knows: the name as typed (member text), the game's id once an import knew it."""
    name: str
    id: Optional[int] = None


class Profession(BaseModel):
    """One profession on a character (design §5.60): a profile profession id, a skill 1..cap, notable recipes (none for gathering)."""
    id: str
    skill: int
    recipes: list[Recipe] = Field(default_factory=list)
```

In `RegisteredCharacter`, after `flex`:

```python
    professions: list[Profession] = Field(default_factory=list)  # design §5.60; saved through the table verb like flex
```

- [ ] **Step 4: Add the validation, the setter and the summary** (next to `set_flex`)

```python
    def check_professions(self, entries: list[dict]) -> list[Profession]:
        """Validate and normalise a character's whole professions list (design §5.60) — one sentence per refusal:
        a profile profession, skill 1..cap, at most trade_slots crafting+gathering, each profession once, recipe names
        non-empty, ≤ RECIPE_NAME_MAX, one per normalised name, none on a gathering profession."""
        from .profiles import norm_recipe

        prof = self.profile
        if not isinstance(entries, list):
            raise RegistryError("professions is a list")
        rules = prof.profession_rules
        cap, slots = rules["skill_cap"], rules["trade_slots"]
        out: list[Profession] = []
        seen: set[str] = set()
        for e in entries:
            if not isinstance(e, dict):
                raise RegistryError("professions is a list of objects")
            pid = str(e.get("id") or "").strip().lower().replace(" ", "_")
            if pid not in prof.professions:
                raise RegistryError(f"{(str(e.get('id') or '?')).strip().title()} isn't a profession in this game version.")
            d = prof.profession(pid)
            if pid in seen:
                raise RegistryError(f"{d.name} is listed twice.")
            seen.add(pid)
            try:
                skill = int(e.get("skill"))
            except (TypeError, ValueError):
                raise RegistryError(f"Skill is a number from 1 to {cap}.")
            if not 1 <= skill <= cap:
                raise RegistryError(f"Skill is a number from 1 to {cap}.")
            recipes: list[Recipe] = []
            keys: set[str] = set()
            for r in e.get("recipes") or []:
                name = str((r.get("name") if isinstance(r, dict) else r) or "").strip()
                name = " ".join(name.split())
                if not name:
                    raise RegistryError("A recipe needs a name.")
                if len(name) > RECIPE_NAME_MAX:
                    raise RegistryError(f"A recipe name is up to {RECIPE_NAME_MAX} characters.")
                k = norm_recipe(name)
                if k in keys:
                    continue
                keys.add(k)
                rid = (r.get("id") if isinstance(r, dict) else None)
                seeded = prof.recipe_seed.get(k)
                recipes.append(Recipe(name=name, id=int(rid) if rid else (seeded["id"] if seeded else None)))
            if recipes and d.group == "gathering":
                raise RegistryError("A gathering profession has no recipes.")
            out.append(Profession(id=pid, skill=skill, recipes=recipes))
        if sum(1 for p in out if prof.profession(p.id).group in ("crafting", "gathering")) > slots:
            raise RegistryError("A character has two trade professions — drop one first.")
        return out

    def set_professions(self, discord_id: int, label: str, entries: list[dict]) -> RegisteredCharacter:
        """Replace a character's professions with a validated list — the one setter; the table save calls it (like set_flex)."""
        # mirror set_flex: find the member and the active character by label (its refusal sentence), assign, bump
        # updated_at, save with the same message style, return the character
        ...

    def professions_summary(self, c: RegisteredCharacter) -> str:
        """'Leatherworking 300 · Skinning 285', or the empty state in words."""
        if not c.professions:
            return "no professions on record"
        return " · ".join(f"{self.profile.profession(p.id).name} {p.skill}" for p in c.professions)
```

Replace the `...` in `set_professions` with the body of `set_flex` adapted: same lookup, `c.professions = self.check_professions(entries)`, same `updated_at`/save lines.

- [ ] **Step 5: Wire the table row key** (in `_apply_table`, right after the `flex` block)

```python
            if row.get("professions") is not None:  # the whole list replaces (design §5.60); absent keeps
                before = [p.model_dump() for p in cur.professions]
                cur = self.set_professions(discord_id, cur.label, row["professions"])
                if [p.model_dump() for p in cur.professions] != before:
                    done.append(f"{cur.label}: professions {self.professions_summary(cur)}")
```

Check `characters_rev` (~line 1742) dumps whole characters (`c.model_dump()`), so the new field is already in the fingerprint; the test asserts it.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -q tests/test_professions.py tests/test_people.py tests/test_members_page.py 2>/dev/null || uv run pytest -q tests/test_professions.py`
Expected: all pass (run `uv run pytest -q tests -k "character_table or characters" ` too: the existing table tests must still pass)

- [ ] **Step 7: Commit**

```bash
git add src/oibot_gm/registry.py tests/test_professions.py
git commit -F - <<'EOF'
A character records its professions: each a profession of the game version, a skill from 1 to 300 and the notable recipes it knows, saved through the one character-table verb like extra roles, so the Me and Members pages, the wizards, plain text and MCP all write the same way and a stale table is refused the same way. Two trade professions at most, secondary ones free, a recipe typed twice is one, a gathering profession has none, every refusal one sentence (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 3: The professions module — catalog, finders, rename/merge, confirm sentences

**Files:**
- Create: `src/oibot_gm/professions.py`
- Modify: `src/oibot_gm/consequences.py` (`people_consequence` + a new generator; `__all__`), `tests/test_confirm_readability.py` (cases near the `flex_consequence` ones ~line 575)
- Test: `tests/test_professions.py`

**Interfaces:**
- Consumes: Task 1–2 names.
- Produces: `professions.Crafter(label, member, uid, profession, skill)`, `professions.CatalogEntry(key, name, profession, crafters: list[Crafter], id: int | None)`; `catalog(reg) -> list[CatalogEntry]` (name order); `who_can_make(reg, text) -> list[CatalogEntry]`; `crafters_of(reg, pid, min_skill=0) -> list[Crafter]`; `rename_recipe(reg, key, new_name, by, *, by_id, preview=False) -> list[str]`; `merge_recipe(reg, key, into_key, by, *, by_id, preview=False) -> list[str]`; `rename_sentences(reg, key, new_name) -> list[str]`, `merge_sentences(reg, key, into_key) -> list[str]`; `visible(reg, m, c) -> bool`; `consequences.profession_consequence(reg, m, label, entries, *, by_id) -> list[str]` and `people_consequence(..., verb="professions", professions=entries)`.

- [ ] **Step 1: Write the failing tests** (append; `OTHER_UID`/`OTHER_LABEL` = a second registered member from conftest)

```python
from oibot_gm import professions as pr
from oibot_gm import consequences as cq


def _give(reg, uid, label, entries):
    reg.save_character_table(uid, [{"label": label, "professions": entries}], "t", officer=True)


def test_catalog_folds_case_and_spaces(reg):
    _give(reg, UID, LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}])
    _give(reg, OTHER_UID, OTHER_LABEL, [{"id": "leatherworking", "skill": 275, "recipes": [{"name": "devilsaur  gauntlets"}]}])
    cat = pr.catalog(reg)
    assert [e.name for e in cat] == ["Devilsaur Gauntlets"], "one recipe, the spelling most crafters use"
    assert [c.label for c in cat[0].crafters] == [LABEL, OTHER_LABEL], "highest skill first"


def test_who_can_make_matches_exact_then_by_words(reg):
    _give(reg, UID, LABEL, [{"id": "tailoring", "skill": 300, "recipes": [{"name": "Mooncloth Bag"}, {"name": "Bottomless Bag"}]}])
    assert [e.name for e in pr.who_can_make(reg, "mooncloth bag")] == ["Mooncloth Bag"]
    assert [e.name for e in pr.who_can_make(reg, "anyone able to make a Mooncloth Bag? got the cloth")] == ["Mooncloth Bag"]
    assert pr.who_can_make(reg, "who can make a Gnomish Death Ray") == []


def test_crafters_of_lists_a_profession_highest_first(reg):
    _give(reg, UID, LABEL, [{"id": "alchemy", "skill": 240}])
    _give(reg, OTHER_UID, OTHER_LABEL, [{"id": "alchemy", "skill": 300}])
    assert [(c.label, c.skill) for c in pr.crafters_of(reg, "alchemy")] == [(OTHER_LABEL, 300), (LABEL, 240)]
    assert [c.label for c in pr.crafters_of(reg, "alchemy", min_skill=300)] == [OTHER_LABEL]


def test_finders_skip_retired_puppets_and_departed(reg):
    _give(reg, UID, LABEL, [{"id": "alchemy", "skill": 300, "recipes": [{"name": "Flask of the Titans"}]}])
    reg.member_left(UID)  # Member.left_at: not in the server
    assert pr.crafters_of(reg, "alchemy") == [] and pr.who_can_make(reg, "flask of the titans") == []
    # (also: retire the character on a present member and a test puppet with professions — both absent from the catalog)


def test_rename_and_merge_rewrite_the_records(reg):
    _give(reg, UID, LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet"}]}])
    _give(reg, OTHER_UID, OTHER_LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}])
    said = pr.merge_recipe(reg, "devilsaur gauntlet", "devilsaur gauntlets", "Officer", by_id=OFFICER_UID, preview=True)
    assert said == ["Devilsaur Gauntlet is merged into Devilsaur Gauntlets.", f"{reg.members[UID].display_name}'s record now reads Devilsaur Gauntlets."]
    assert len(pr.catalog(reg)) == 2, "a preview writes nothing"
    pr.merge_recipe(reg, "devilsaur gauntlet", "devilsaur gauntlets", "Officer", by_id=OFFICER_UID)
    assert [e.name for e in pr.catalog(reg)] == ["Devilsaur Gauntlets"] and len(pr.catalog(reg)[0].crafters) == 2
    pr.rename_recipe(reg, "devilsaur gauntlets", "Gauntlets of the Devilsaur", "Officer", by_id=OFFICER_UID)
    assert [e.name for e in pr.catalog(reg)] == ["Gauntlets of the Devilsaur"]
    with pytest.raises(RegistryError, match="Nobody has registered"):
        pr.rename_recipe(reg, "nothing here", "X", "Officer", by_id=OFFICER_UID)


def test_profession_consequence_reads_aloud(reg):
    m = reg.members[UID]
    own = cq.profession_consequence(reg, m, LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "a"}, {"name": "b"}]}], by_id=UID)
    assert own == ["Your Leatherworking is recorded at 300 with 2 notable recipes."]
    _give(reg, UID, LABEL, [{"id": "leatherworking", "skill": 300}, {"id": "enchanting", "skill": 100}])
    officer = cq.profession_consequence(reg, m, LABEL, [{"id": "leatherworking", "skill": 300}], by_id=OFFICER_UID)
    assert officer == [f"{m.display_name}'s Enchanting is removed from their record."]
    with pytest.raises(RegistryError, match="already as given"):
        cq.profession_consequence(reg, m, LABEL, [{"id": "leatherworking", "skill": 300}, {"id": "enchanting", "skill": 100}], by_id=UID)
    assert cq.people_consequence(reg, None, m, "professions", by_id=UID, label=LABEL, professions=[]) == ["Your Leatherworking is removed from your record.", "Your Enchanting is removed from your record."]
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest -q tests/test_professions.py -k "catalog or who_can or crafters_of or finders or rename or consequence"`
Expected: FAIL — `ModuleNotFoundError: No module named 'oibot_gm.professions'`

- [ ] **Step 3: Write the module**

```python
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
```

- [ ] **Step 4: The confirm generator** (in `consequences.py`, after `flex_consequence`)

```python
def profession_consequence(reg: Registry, m, label: str, entries: list[dict], *, by_id: int | None = None) -> list[str]:
    """A character's professions as the save will leave them (design §5.60): one sentence per profession recorded or
    removed. Nothing about runs: professions touch no seat. Unchanged → refused."""
    v = _Voice(reg, m, by_id)
    c = _char(reg, m, label, v)
    after = {p.id: p for p in reg.check_professions(entries)}
    before = {p.id: p for p in c.professions}
    if {k: p.model_dump() for k, p in after.items()} == {k: p.model_dump() for k, p in before.items()}:
        raise RegistryError(f"{c.label}'s professions are already as given.")
    out = []
    for pid, p in after.items():
        old = before.get(pid)
        if old is not None and old.model_dump() == p.model_dump():
            continue
        name = reg.profile.profession(pid).name
        n = len(p.recipes)
        tail = f" with {n} notable recipe{'s' if n != 1 else ''}" if n else ""
        out.append(f"{v.Whose} {name} is recorded at {p.skill}{tail}.")
    for pid in before:
        if pid not in after:
            out.append(f"{v.Whose} {reg.profile.profession(pid).name} is removed from {v.their} record.")
    return out
```

Add `professions: list[dict] | None = None` to `people_consequence`'s signature and the branch `if verb == "professions": return profession_consequence(reg, m, label, professions or [], by_id=by_id)`; add `"profession_consequence"` to `__all__` and `"professions"` to `PEOPLE_VERBS` (find where that tuple lives and keep its order).

- [ ] **Step 5: Readability harness cases** (next to the `flex_consequence` cases in `tests/test_confirm_readability.py`; `tank`, `main`, `who`, `by` are that loop's variables)

```python
        dump.add("profession_consequence", f"one recorded, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}], by_id=by))
        dump.add("profession_consequence", f"two recorded, {who}", _said(cq.profession_consequence, reg, tank, main, [{"id": "alchemy", "skill": 120}, {"id": "cooking", "skill": 300}], by_id=by))
```

and, after a save that gives `main` a profession in that test's setup, a "removed" case with `[]`. Run the harness; rewrite any sentence it flags, never allow-list.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -q tests/test_professions.py tests/test_confirm_readability.py`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/oibot_gm/professions.py src/oibot_gm/consequences.py tests/test_professions.py tests/test_confirm_readability.py
git commit -F - <<'EOF'
The guild's recipe catalog is read from the records, never stored on its own: every notable recipe a crafter registered, folded by name so two spellings of one recipe are one entry, with its crafters highest skill first; who can make a thing is answered by the exact name, else by every recipe whose words appear in the question; retired characters, puppets and members who left are never named. Officers rename a misspelt recipe or merge two spellings, which rewrites the names on the records they touch through the one table verb. The confirm sentences read as the Confirm rule asks: "Your Leatherworking is recorded at 300 with 2 notable recipes." (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 4: The import boundary

**Files:**
- Create: `src/oibot_gm/importers/professions.py`
- Test: `tests/test_professions.py`

**Interfaces:**
- Produces: `detect(text: str) -> str | None` (a format name, None = unknown), `parse(text: str) -> list[dict]` rows `{character, profession, skill, recipes: [{id?, name}]}`; `FORMATS: dict[str, Callable]` (empty).

- [ ] **Step 1: Failing test**

```python
from oibot_gm.importers import professions as pimp


def test_import_boundary_recognises_nothing_yet():
    assert pimp.detect("anything") is None and pimp.FORMATS == {}
    with pytest.raises(pimp.ImportError_, match="isn't a professions export the bot can read yet"):
        pimp.parse("anything")
```

- [ ] **Step 2: Run** — `uv run pytest -q tests/test_professions.py -k import_boundary` → FAIL (no module)

- [ ] **Step 3: Write it**

```python
"""The professions import boundary (design §5.60): a paste from an addon export, or a wago.tools seed, becomes rows the
verb takes — {character, profession, skill, recipes: [{id?, name}]}. No format is known yet; the Me page shows no paste
box until FORMATS has one. Adding a format = one parser here and the box, nothing else."""
from __future__ import annotations

from typing import Callable

FORMATS: dict[str, Callable[[str], list[dict]]] = {}


class ImportError_(ValueError):
    """The refusal in words (never a traceback for a person)."""


def detect(text: str) -> str | None:
    for name, fn in FORMATS.items():
        try:
            fn(text)
            return name
        except ImportError_:
            continue
    return None


def parse(text: str) -> list[dict]:
    name = detect(text)
    if name is None:
        raise ImportError_("That isn't a professions export the bot can read yet — add professions on the Me page or with /me professions.")
    return FORMATS[name](text)
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit**

```bash
git add src/oibot_gm/importers/professions.py tests/test_professions.py
git commit -F - <<'EOF'
The professions import has its door built before any key fits it: a paste is detected and parsed into the rows the verb takes, and today no format is known, so the paste refuses in words and the Me page shows no box. An addon export or a wago.tools seed later is one parser and a box (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

Then run the phase gate: `uv run python scripts/check_commands.py > <scratch>/check_prof_1.log 2>&1; echo EXIT=$? >> <scratch>/check_prof_1.log` (background; read the EXIT line) and fix anything it finds before Phase 2.

---

## Phase 2 — plain text, help, Listening, MCP, docs

### Task 5: Plain text — the `character` op learns professions

**Files:**
- Modify: `src/oibot_gm/configops.py` (`SCHEMA_TEXT` Members lines ~174–179; `_character_rows` ~line 1149), `src/oibot_gm/professions.py` (add `op_rows`)
- Test: `tests/test_professions_ops.py` (new; copy the fixture style of the test that exercises `_character_rows` — `grep -ln "_character_rows\|field=\"flex\"" tests/*.py`)

**Interfaces:**
- Produces: `professions.op_rows(reg, c, field, value, hint) -> tuple[list[dict], str]` for fields `profession`, `profession_remove`, `recipe_add`, `recipe_remove`; `_character_rows` delegates to it for those fields.

- [ ] **Step 1: Failing tests**

```python
import pytest
from oibot_gm import configops
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError
from tests.test_professions import UID, LABEL  # the module constants


def _op(**kw):
    return ConfigOp(op="character", **kw)


def test_profession_field_upserts_and_keeps_recipes(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 275, "recipes": [{"name": "Hide of the Wild"}]}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="leatherworking 300"))
    assert rows[0]["professions"][0] == {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Hide of the Wild", "id": None}]}
    assert line == f"{LABEL} · Leatherworking: 275 → 300"
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="Skinning"))
    assert rows[0]["professions"][1]["id"] == "skinning" and rows[0]["professions"][1]["skill"] == 1 and line.endswith("Skinning: 1")


def test_recipe_add_and_remove_name_the_profession_in_the_line(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Devilsaur Gauntlets"))
    assert rows[0]["professions"][0]["recipes"] == [{"name": "Devilsaur Gauntlets", "id": None}] and line == f"{LABEL} · Leatherworking recipes: + Devilsaur Gauntlets"
    reg.save_character_table(UID, rows, "t", officer=True)
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="devilsaur gauntlets"))
    assert rows[0]["professions"][0]["recipes"] == [] and line.endswith("− Devilsaur Gauntlets")


def test_recipe_add_needs_the_profession_when_two_could_hold_it(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "cooking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(RegistryError, match="Which profession makes Savory Deviate Delight — Leatherworking or Cooking?"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Savory Deviate Delight"))
    rows, _ = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Savory Deviate Delight", text="cooking"))
    assert rows[0]["professions"][1]["recipes"][0]["name"] == "Savory Deviate Delight"


def test_profession_remove_and_a_bad_profession(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "enchanting", "skill": 100}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession_remove", value="enchanting"))
    assert rows[0]["professions"] == [] and line == f"{LABEL} · Enchanting: removed"
    with pytest.raises(RegistryError, match="Jewelcrafting isn't a profession in this game version."):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="jewelcrafting 50"))
```

- [ ] **Step 2: Run** — `uv run pytest -q tests/test_professions_ops.py` → FAIL (the field falls through to the existing "unknown field" refusal)

- [ ] **Step 3: `op_rows` in professions.py**

```python
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
    raise RegistryError(f"{text.strip().title() or '?'} isn't a profession in this game version.")


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
```

- [ ] **Step 4: Wire `_character_rows`** — after the `if cur is None:` refusal, before `if f == "spec":`:

```python
    if f in ("profession", "profession_remove", "recipe_add", "recipe_remove"):
        from . import professions

        return professions.op_rows(reg, cur, f, value, (op.text or "").strip() or None)
```

Extend `SCHEMA_TEXT`'s Members block (line ~174): change `field=add|spec|offspec|name|rank|main|retire|delete|flex` to append `|profession|profession_remove|recipe_add|recipe_remove` and add the lines:

```
    profession: value = "<profession> [skill]" ("Rhoz is a 300 leatherworker" → character=Rhoz, field=profession, value="leatherworking 300";
      a profession already on record keeps its skill when none is said); profession_remove: value = the profession ("drop enchanting on Jonnie");
    recipe_add / recipe_remove: value = the recipe's name exactly as written ("I can make Devilsaur Gauntlets on Rhoz", "I just learned …"),
      text = the profession when the member names one ("… with cooking"); a recipe name is member text, never changed or guessed
```

Also check `describe_ops`' structured path (`ops_from_fields`, kind "character") needs nothing: the text path builds the op; the Me/Members pages save tables directly.

- [ ] **Step 5: Run** — `uv run pytest -q tests/test_professions_ops.py tests/test_configops.py tests/test_parity_matrix.py` (the matrix already lists `character`) → PASS

- [ ] **Step 6: Commit**

```bash
git add src/oibot_gm/configops.py src/oibot_gm/professions.py tests/test_professions_ops.py
git commit -F - <<'EOF'
Plain text records professions on your own characters: "Rhoz is a 300 leatherworker", "I just learned Devilsaur Gauntlets on Rhoz", "drop enchanting on Jonnie" become the one character change, previewed as "Rhoz · Leatherworking: 275 → 300" and applied on the press; a recipe name is kept exactly as written, and when two of a character's professions could make it the bot asks which rather than guessing (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 6: The help answer knows professions

**Files:**
- Modify: `src/oibot_gm/help.py` (`SECTIONS` ~line 943 — insert before `"bank"`; `_section_hit`), `src/oibot_gm/professions.py` (`HELP_WORDS`, `help_section`)
- Test: `tests/test_help.py`

**Interfaces:**
- Produces: `professions.HELP_WORDS: re.Pattern`, `professions.help_section(ask) -> list[str]`, `professions.help_hit(reg, q) -> bool` (profession names, catalog recipe names).

- [ ] **Step 1: Failing tests** (append to `tests/test_help.py`; use its existing fixtures and the way other section tests build a snapshot for a non-officer in public)

```python
def test_professions_section_answers_who_can_make_for_members(reg):
    from oibot_gm import help as H
    uid = next(m.discord_id for m in reg.members.values() if m.active() and not m.test)
    label = reg.members[uid].active()[0].label
    reg.save_character_table(uid, [{"label": label, "professions": [{"id": "tailoring", "skill": 300, "recipes": [{"name": "Mooncloth Bag"}]}]}], "t", officer=True)
    s = H.guild_state(reg, None, uid, False, "anyone able to make a mooncloth bag?", None, public=False)
    assert "### Who can make what" in s and "Mooncloth Bag" in s and f"{label}" in s and "Tailoring 300" in s
    assert "### Who can make what" in H.guild_state(reg, None, uid, False, "who is our alchemist", None, public=False)
    assert "### Who can make what" not in H.guild_state(reg, None, uid, False, "when is the next raid", None, public=False)
```

- [ ] **Step 2: Run** → FAIL (no section)

- [ ] **Step 3: The section** (professions.py)

```python
HELP_WORDS = re.compile(r"\b(profession|professions|craft\w*|make|makes|recipe|recipes|enchant\w*|who can|crafter|crafters|skill)\b")


def help_hit(reg: Registry, q: str) -> bool:
    """The question names a profession ('alchemist', 'leatherworking') or a registered recipe."""
    q = norm_recipe(q)
    for d in reg.profile.professions.values():
        stem = norm_recipe(d.name)[:6]
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
```

In `help.py` `SECTIONS`, insert before `"bank"`:

```python
    "professions": (professions.HELP_WORDS, professions.help_section, 3_000, False),
```

(import `from . import professions` at the top; it imports registry only, no cycle through help). In `_section_hit`, add:

```python
    if key == "professions":
        return professions.help_hit(reg, ask.question)
```

- [ ] **Step 4: Run** — `uv run pytest -q tests/test_help.py` → PASS. **Step 5: Commit**

```bash
git add src/oibot_gm/help.py src/oibot_gm/professions.py tests/test_help.py
git commit -F - <<'EOF'
Questions about who can make a thing are answered from the records in any channel, by DM and in the site's Tell the bot box: the help snapshot gains a members' section listing each profession's crafters with their skill and every registered recipe with its makers, drawn in when the question says craft, make, recipe, a profession's name or a registered recipe's name; when nobody has registered a recipe the bot says so and how a crafter adds it (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 7: Listening knows professions

**Files:**
- Modify: `src/oibot_gm/discord_bot.py` (`LISTEN_BOT_WORDS`, `LISTEN_QUESTION`, `LISTEN_GLOSSARY`)
- Test: `tests/test_listen.py`

- [ ] **Step 1: Failing test**

```python
def test_listening_words_cover_professions():
    from oibot_gm import discord_bot as db
    assert "professions" in db.LISTEN_BOT_WORDS and "notable recipes" in db.LISTEN_BOT_WORDS
    assert "who can make" in db.LISTEN_QUESTION
    assert {"recipe", "craft", "who can make", "profession", "skill"} <= set(db.LISTEN_GLOSSARY)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Edit the constants**: in `LISTEN_BOT_WORDS` after "their rank and attendance." add `It knows each character's professions, their skill and the notable recipes they make, and who can make a thing.`; in `LISTEN_QUESTION`'s "Yes when it asks something the bot knows (…" list add `who can make or craft something, which professions or recipes someone has` and in the "tells the bot something it records" list add `a profession, a skill level or a recipe learned`; append to `LISTEN_GLOSSARY`: `"profession", "skill", "recipe", "craft", "who can make", "enchant", "leatherworking", "alchemy", "tailoring", "engineering", "blacksmithing", "cooking"`.

- [ ] **Step 4: Run** — `uv run pytest -q tests/test_listen.py` → PASS. **Step 5: Commit**

```bash
git add src/oibot_gm/discord_bot.py tests/test_listen.py
git commit -F - <<'EOF'
Listening treats professions as the bot's business: its description to the decision model says it knows each character's professions, skill and notable recipes and who can make a thing, so "just hit 300 leatherworking on Rhoz" reads as something it records and "anyone able to make a Mooncloth Bag?" as something it answers, in a channel set to Replies with no tag (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 8: Web routes and MCP tools

**Files:**
- Create: `src/oibot_gm/web/api_professions.py`
- Modify: `src/oibot_gm/web/api.py` (`char_json` ~line 601 adds `professions`; `/api/members/set` `SET_VERBS` + a `professions` branch ~line 2195/2286; `meta_json` ~line 480 adds `professions`; `install_*` calls ~line 2530), `src/oibot_gm/mcp_server.py` (two tools), `src/oibot_gm/web/app.py` only if `ICON_CDN` names can't serve `inv_*`/`spell_*` (they can: the regex allows `[a-z0-9_]`)
- Test: `tests/test_professions_web.py` (new; copy the client fixture style of `tests/test_people_web.py`), `tests/test_mcp.py`

**Interfaces:**
- Produces routes: `GET /api/professions` → `{professions: [{id, name, group, icon, crafters: [{label, member, uid, skill}]}], recipes: [{key, name, profession, id, crafters: [...]}], officer: bool}`; `GET /api/professions/find?q=` → `{q, recipes: [...]}`; `POST /api/professions/rename {key, name, preview?}` and `POST /api/professions/merge {key, into, preview?}` (officers) → `{sentences}` / `{message, sentences}`; `POST /api/members/set` accepts `professions: list[dict]` (+ `character`) → preview sentences via `people_consequence(..., "professions", professions=...)`, write via the table row; `char_json` gains `"professions": [p.model_dump() for p in c.professions]`; `meta_json` gains `"professions": {"list": [{id,name,group,icon}], "skill_cap", "trade_slots"}` (empty list when the version has none). MCP: `set_professions(member, character, professions: list[dict], guild)`, `who_can_make(text, guild)`.

- [ ] **Step 1: Failing tests** (`tests/test_professions_web.py`)

```python
def test_professions_page_reads_catalog_and_find(web, member_headers, officer_headers):  # fixtures as in test_people_web.py
    # give one member a profession + recipe through the table, then:
    r = web.get("/api/professions", headers=member_headers)
    assert r.status_code == 200
    d = r.json()
    assert [p["id"] for p in d["professions"]][:3] == ["alchemy", "blacksmithing", "enchanting"] and d["officer"] is False
    assert d["recipes"][0]["name"] == "Mooncloth Bag" and d["recipes"][0]["crafters"][0]["skill"] == 300
    assert web.get("/api/professions/find?q=mooncloth%20bag", headers=member_headers).json()["recipes"][0]["name"] == "Mooncloth Bag"


def test_rename_and_merge_are_officers_only_and_preview_first(web, member_headers, officer_headers):
    assert web.post("/api/professions/rename", json={"key": "mooncloth bag", "name": "Mooncloth Bags"}, headers=member_headers).status_code == 403
    p = web.post("/api/professions/rename", json={"key": "mooncloth bag", "name": "Mooncloth Bags", "preview": True}, headers=officer_headers).json()
    assert p["sentences"][0] == "Mooncloth Bag is renamed Mooncloth Bags."
    assert web.post("/api/professions/rename", json={"key": "mooncloth bag", "name": "Mooncloth Bags"}, headers=officer_headers).status_code == 200
    assert web.get("/api/professions/find?q=mooncloth%20bags", headers=member_headers).json()["recipes"]


def test_members_set_takes_professions_with_the_confirm_sentences(web, officer_headers, reg):
    # uid/label of a member; preview then write
    body = {"uid": UID, "character": LABEL, "professions": [{"id": "alchemy", "skill": 300}]}
    p = web.post("/api/members/set", json={**body, "preview": True}, headers=officer_headers).json()
    assert p["sentences"] == [f"{reg.members[UID].display_name}'s Alchemy is recorded at 300."]
    assert web.post("/api/members/set", json=body, headers=officer_headers).status_code == 200
    assert web.get("/api/members", headers=officer_headers).json()["rows"][0]["characters"][0]["professions"][0]["id"] == "alchemy"
    assert web.get("/api/meta", headers=officer_headers).json()["professions"]["skill_cap"] == 300
```

- [ ] **Step 2: Run** → FAIL (404s)

- [ ] **Step 3: Routes** (`api_professions.py`, in the shape of `api_me.install_me_api`)

```python
"""The Professions page's routes (design §5.60): the catalog and the crafters per profession for every member, the
finder, and the officers' rename / merge (preview first, then the write through professions.py → the table verb)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import ops as ops_mod
from .. import professions as pr
from ..registry import RegistryError


def install_professions_api(app: FastAPI, bot, *, who, body) -> None:
    def crafter_json(c: pr.Crafter) -> dict:
        return {"label": c.label, "member": c.member, "uid": str(c.uid), "skill": c.skill, "profession": c.profession}

    def entry_json(e: pr.CatalogEntry) -> dict:
        return {"key": e.key, "name": e.name, "profession": e.profession, "id": e.id, "crafters": [crafter_json(c) for c in e.crafters]}

    @app.get("/api/professions")
    async def professions_get(request: Request):
        v = await who(request)
        reg = v.reg
        return {"professions": [{"id": d.id, "name": d.name, "group": d.group, "icon": d.icon, "crafters": [crafter_json(c) for c in pr.crafters_of(reg, d.id)]}
                                for d in reg.profile.professions.values()],
                "recipes": [entry_json(e) for e in pr.catalog(reg)], "officer": bool(v.officer)}

    @app.get("/api/professions/find")
    async def professions_find(request: Request, q: str = ""):
        v = await who(request)
        return {"q": q, "recipes": [entry_json(e) for e in pr.who_can_make(v.reg, q)]}

    async def _maintain(request: Request, fn, *args):
        v, d = await body(request, officer=True)
        preview = bool(d.get("preview"))
        try:
            said = fn(v.reg, *args, v.name, by_id=v.uid, preview=preview)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not preview:
            await ops_mod.audited(bot.ops, v.reg, v.name, said[0], via=v.via, by_id=v.uid)
        return {"sentences": said, "message": said[0]}

    @app.post("/api/professions/rename")
    async def professions_rename(request: Request):
        _, d = await body(request, officer=True)
        return await _maintain(request, pr.rename_recipe, str(d.get("key") or ""), str(d.get("name") or ""))

    @app.post("/api/professions/merge")
    async def professions_merge(request: Request):
        _, d = await body(request, officer=True)
        return await _maintain(request, pr.merge_recipe, str(d.get("key") or ""), str(d.get("into") or ""))
```

(If `body()` can't be awaited twice on one request, read it once in each route and pass `d` into `_maintain` instead — check `api_me.py` for how it reads `body` and follow that.) Register in `api.py` next to `install_me_api`: `install_professions_api(app, bot, who=who, body=body)`.

`char_json`: add `"professions": [p.model_dump() for p in c.professions]`. `meta_json`: add

```python
            "professions": {"list": [{"id": d.id, "name": d.name, "group": d.group, "icon": d.icon} for d in reg.profile.professions.values()],
                            "skill_cap": reg.profile.profession_rules["skill_cap"], "trade_slots": reg.profile.profession_rules["trade_slots"]},
```

`/api/members/set`: add `"professions"` to `SET_VERBS` (after `"flex"`), and a branch like the `flex` one:

```python
            if d.get("professions") is not None and "professions" in d:
                c = picked("professions", d.get("character"), m.main)
                if not isinstance(d["professions"], list):
                    raise RegistryError("professions is a list")
                rows.append({"label": c.label, "professions": d["professions"]})
                if preview:
                    said += cq.people_consequence(reg, rs, m, "professions", by_id=v.uid, label=c.label, professions=d["professions"])
```

(mirror exactly how the `flex` branch calls `picked` and appends `said`; keep the names it uses.)

- [ ] **Step 4: MCP tools** (`mcp_server.py`, after `set_member`)

```python
@mcp.tool()
def set_professions(member: str, character: str, professions: list[dict], guild: Guild = None) -> str:
    """Replace one character's professions (design §5.60): professions = [{id: leatherworking, skill: 300, recipes: [{name: "Devilsaur Gauntlets"}]}, …]
    — the whole list; [] clears. Two trade professions at most; a gathering one has no recipes. The owner's token writes it (no actor needed)."""
    return msg(api(guild).post("/api/members/set", {"member": member, "character": character, "professions": professions}))


@mcp.tool()
def who_can_make(text: str, guild: Guild = None) -> str:
    """Who can make a recipe: the registered recipes matching `text` with their crafters, highest skill first. Names only."""
    return msg(api(guild).get("/api/professions/find", {"q": text}))
```

(check `Api.get`'s query-parameter signature and match it.) Add a `tests/test_mcp.py` case asserting both tools exist and `who_can_make` hits `/api/professions/find`.

- [ ] **Step 5: Run** — `uv run pytest -q tests/test_professions_web.py tests/test_mcp.py tests/test_people_web.py tests/test_parity_matrix.py` → PASS. **Step 6: Commit**

```bash
git add src/oibot_gm/web/api_professions.py src/oibot_gm/web/api.py src/oibot_gm/mcp_server.py tests/test_professions_web.py tests/test_mcp.py
git commit -F - <<'EOF'
The site and MCP read and write professions: every member reads the catalog and each profession's crafters, the finder answers a typed name, officers rename or merge a recipe with the sentence shown first, the one-member route takes a character's professions with the confirm sentences on preview, and every character row the pages read carries its professions; MCP gains set_professions and who_can_make over the same routes (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 9: Manual, design and CLAUDE.md for phases 1–2

**Files:**
- Modify: `docs/manual.md` (§1a words; §3 a **Professions** paragraph; §10a a line; §8b MCP rows; §8c Listening note), `docs/design.md` (new `### 5.60 Professions: a tracker and a finder (built 2026-10-30)` summarising the spec and pointing at it), `CLAUDE.md` (layout lines for `professions.py`, `importers/professions.py`, `web/api_professions.py`; help.py and configops.py entries)

- [ ] **Step 1: Write the words.** §1a entries:

```
- **Profession / skill** — a character's trade (Alchemy, Leatherworking…) and how far along it is, a number from 1 to 300.
- **Notable recipes** — the ones worth telling the guild about, never a whole recipe book. A **crafter** is a character that has them.
- **Who can make** — the finder: the crafters of a recipe, names only; nobody is pinged.
```

§3 paragraph (after characters): how to add professions on Me (Professions… in the row's ⋮), in Discord (`/me professions`), in plain text ("Rhoz is a 300 leatherworker", "I just learned Devilsaur Gauntlets on Rhoz"), what officers can do (`/roster professions`, Members ⋮, rename/merge on the Professions page), the two-trade rule, and that an import from an addon export is planned. §10a: a line that "who can make X" is answered from the records for any member. §8b: `set_professions`, `who_can_make` rows. §8c: a sentence that a channel for professions is set like any listening channel.

- [ ] **Step 2: Run the copy and manual tests** — `uv run pytest -q tests/test_copy.py tests/test_help.py tests/test_manual*.py` → PASS.

- [ ] **Step 3: Commit**, then run the Phase 2 gate (`check_commands` → `<scratch>/check_prof_2.log`, `check_bundle`).

```bash
git add docs/manual.md docs/design.md CLAUDE.md
git commit -F - <<'EOF'
The manual explains professions in the guild's words — profession and skill, notable recipes, crafter, who can make — how a member adds theirs on the Me page, in Discord and in plain text, what officers may do, and that the bot answers who can make a thing to any member; design §5.60 records the decisions and points at the spec; the agent guide names the new modules

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

## Phase 3 — the site

### Task 10: The professions line and editor on Me and Members

**Files:**
- Create: `frontend/src/components/ProfessionLine.tsx`, `frontend/src/components/ProfessionsEditor.tsx`
- Modify: `frontend/src/api.ts` (`Character.professions`, `Meta.professions`), `frontend/src/components/Icons.tsx` (`kind: "profession"`; `iconUrl` reads `meta.professions.list`), `frontend/src/components/CharacterTable.tsx` (the line under each row, the ⋮ item), `frontend/src/pages/Members.tsx` (the line under each character, the character ⋮ item), `frontend/src/components/members/PeopleDialogs.tsx` only if the editor is mounted through `PickModal` (prefer mounting `ProfessionsEditor` directly from `Members.tsx`)
- Test: `tests/test_frontend_source.py`

**Interfaces:**
- Consumes: `/api/me/characters` rows `{label, professions}` (member), `/api/members/set {uid, character, professions, rev, preview}` (officer), `meta.professions.list` / `skill_cap` / `trade_slots`.
- Produces: `ProfessionLine({meta, professions, compact?})` — icons with skill, chips, or the empty state sentence; `ProfessionsEditor({meta, label, professions, onSave(entries) => Promise<PeoplePreview | TableSaved>, onCancel})` — the in-place editor that calls `onSave` with `preview: true` first and prints only the server's `sentences`.

- [ ] **Step 1: Failing source test**

```python
def test_professions_line_and_editor_on_me_and_members():
    """Professions (design §5.60): one line under each character — a profession icon with its skill beside it, the notable
    recipes as chips, or the empty state in words; Professions… in the character ⋮ on Me and Members; the editor posts a
    preview first and prints only the server's sentences; skill is a number input 1..skill_cap."""
    line = (SRC / "components" / "ProfessionLine.tsx").read_text()
    assert 'kind="profession"' in line and "No notable recipes yet" in line and "no professions on record" in line
    ed = (SRC / "components" / "ProfessionsEditor.tsx").read_text()
    assert "preview: true" in ed and "sentences" in ed and "NumberInput" in ed and "skill_cap" in ed and "trade_slots" in ed
    assert "A gathering trade has no recipes." in ed
    me = (SRC / "components" / "CharacterTable.tsx").read_text()
    mem = (SRC / "pages" / "Members.tsx").read_text()
    assert "<ProfessionLine" in me and "Professions…" in me and "<ProfessionLine" in mem and "Professions…" in mem
    icons = (SRC / "components" / "Icons.tsx").read_text()
    assert '"profession"' in icons
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Types and icons.** `api.ts`: `export interface Recipe { name: string; id: number | null }`, `export interface Profession { id: string; skill: number; recipes: Recipe[] }`, add `professions?: Profession[]` to `Character`; add to `Meta`: `professions?: { list: { id: string; name: string; group: string; icon: string }[]; skill_cap: number; trade_slots: number }`. `Icons.tsx`: widen `kind` to `"class" | "role" | "spec" | "profession"` and in `iconUrl` map `profession` → `/img/icon/${meta.professions.list.find(p => p.id === id)?.icon}.jpg` with the badge fallback the others use.

- [ ] **Step 4: `ProfessionLine.tsx`**

```tsx
import { Badge, Group, Text, Tooltip } from "@mantine/core";
import type { Meta, Profession } from "../api";
import { GameIcon } from "./Icons";

/** One character's professions (design §5.60): a profession icon with its skill beside it (an icon plus a number, like a
 *  role count — never a text label), the notable recipes as chips, or the empty state in words. */
export function ProfessionLine({ meta, professions, compact }: { meta: Meta; professions?: Profession[]; compact?: boolean }) {
  const defs = meta.professions?.list || [];
  const name = (id: string) => defs.find((d) => d.id === id)?.name || id;
  const cap = meta.professions?.skill_cap || 300;
  if (!professions || professions.length === 0) return <Text size="xs" c="dimmed">no professions on record</Text>;
  const recipes = professions.flatMap((p) => p.recipes.map((r) => ({ ...r, prof: name(p.id) })));
  return (
    <Group gap={compact ? 8 : 14} wrap="wrap" align="center">
      {professions.map((p) => (
        <Tooltip key={p.id} label={name(p.id)}>
          <Group gap={5} wrap="nowrap">
            <GameIcon meta={meta} kind="profession" id={p.id} size={compact ? 20 : 24} title={name(p.id)} />
            <Text fw={700} size={compact ? "sm" : "md"} c={p.skill >= cap ? "teal.3" : undefined}>{p.skill}</Text>
          </Group>
        </Tooltip>
      ))}
      {recipes.length
        ? recipes.map((r) => <Badge key={r.prof + r.name} variant="outline" color="gray" radius="xl" size={compact ? "sm" : "md"} tt="none" fw={500} title={r.prof}>{r.name}</Badge>)
        : <Text size="xs" c="dimmed">No notable recipes yet — add the ones worth telling the guild about in Professions…</Text>}
    </Group>
  );
}
```

- [ ] **Step 5: `ProfessionsEditor.tsx`** — one character's professions in place (mockup Edit): a row per profession with a `Select` (grouped data: `[{group: "Crafting", items: [...]}, …]`, items of taken trades disabled when `trade_slots` is full and the item is crafting/gathering and not already on the row), a `NumberInput min={1} max={skill_cap}`, a chips row with a `TextInput` + Add button (`maxLength={60}`), a remove ActionIcon per row; the text "A gathering trade has no recipes." in place of chips when the def's group is gathering; "Two trades are taken — Cooking, First Aid and Fishing are still open." under the Add-a-profession select when full. Footer: Cancel · Save <label>'s professions. Save → `await onSave(entries, true)` → show `sentences` in `useConfirm` (components/ConfirmModal.tsx) → on confirm `await onSave(entries, false)`. Errors from the server are shown as the one sentence in place of the preview. Entries sent: `[{id, skill, recipes: [{name, id}]}]`.

- [ ] **Step 6: Mount it.** `CharacterTable.tsx`: under each read-mode row render a full-width `<Table.Tr><Table.Td colSpan={7}><ProfessionLine meta={meta} professions={c.professions} /></Table.Td></Table.Tr>`; add `<Menu.Item onClick={() => setProf(c)}>Professions…</Menu.Item>`; when `prof` is set render `<ProfessionsEditor … onSave={(entries, preview) => api.post<TableSaved>("/api/me/characters", { rows: [{ label: c.label, professions: entries }], rev: me.characters_rev, preview })} />` in place of that row (register the editing state with `onEditing` like the table's Edit does, so the guild switcher guard sees it). `Members.tsx`: under each character line `<ProfessionLine meta={meta} professions={c.professions} compact />`; the character ⋮ gains `<Item label="Professions…" onClick={() => setProf({ r, c })} />`; the editor posts `/api/members/set { uid: r.uid, character: c.label, professions: entries, rev: r.characters_rev, preview }` and prints `sentences`. The search box also matches `c.professions[].recipes[].name` (extend the existing filter predicate).

- [ ] **Step 7: Build and test**

Run: `cd frontend && npm run build && npx tsc --noEmit -p tsconfig.app.json && cd .. && uv run python scripts/check_bundle.py && uv run pytest -q tests/test_frontend_source.py tests/test_copy.py`
Expected: build OK, tsc clean, bundle OK, tests pass

- [ ] **Step 8: Commit** (include `src/oibot_gm/web/static/app`)

```bash
git add frontend/src src/oibot_gm/web/static/app tests/test_frontend_source.py
git commit -F - <<'EOF'
The Me and Members pages show each character's professions on one line under it — the profession's icon with its skill beside it, green at the cap, then the notable recipes as chips, or the empty state in words — and Professions… in the character's ⋮ opens that one character's professions in place: pick a profession, type the skill, add or remove recipes, and Save shows the bot's own sentences before it writes, through the same route the table already uses (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 11: The Professions page

**Files:**
- Create: `frontend/src/pages/Professions.tsx`, `frontend/src/pages/professions.module.css`
- Modify: `frontend/src/App.tsx` (NAV entry after Loot log with an icon from `@tabler/icons-react`, e.g. `IconTool`; `<Route path="/professions" element={<ProfessionsPage meta={meta} />} />` for every member), `frontend/src/api.ts` (`ProfessionsData` type), `scripts/shots.py` (`PAGES` gains `/app/professions`)
- Test: `tests/test_frontend_source.py`

- [ ] **Step 1: Failing source test**

```python
def test_professions_page_is_the_finder_with_officer_maintenance():
    src = (SRC / "pages" / "Professions.tsx").read_text()
    app = (SRC / "App.tsx").read_text()
    assert 'path="/professions"' in app and '{ to: "/professions", label: "Professions"' in app
    assert "/api/professions/find" in src and "Who can make what" in src
    assert "Rename…" in src and "Merge…" in src and "officer" in src
    assert "Nobody has registered" in src and "no notable recipes registered" in src
    assert 'useLive(load, ["member", "config"])' in src
```

- [ ] **Step 2: Run** → FAIL. **Step 3: The page** (mockup Finder / Phone): `PageTitle` "Professions" with the intro "Who can make what. Search a recipe, or open a profession to see its crafters. Add your own on the Me page or with /me professions."; a `TextInput` search (debounced 250 ms → `GET /api/professions/find?q=`); a Card "N recipes match "q"" / "Type a recipe's name to see who makes it" listing hits: name, `<Profession> · registered by N crafters`, each crafter as class icon (`GameIcon kind="class"`), character name in its class colour, member in dimmed text, profession icon + skill; for officers (`data.officer`) Rename… and Merge… buttons per hit, each a small modal with one input that previews (`preview: true` → `sentences`) and then writes; `Tabs` per profession with the icon and `Name · count` (dimmed when 0), each panel a Card listing crafters highest first with a `Progress` bar `value={skill / skill_cap * 100}` and recipe chips, or "no notable recipes registered"; the empty panel: "Nobody has registered <Name> yet. A crafter adds it on the Me page, with /me professions, or by saying so in the professions channel." Below `sm`: the tabs scroll sideways (`Tabs.List` with `style={{ overflowX: "auto", flexWrap: "nowrap" }}`), hits as cards (css module). `useLive(load, ["member", "config"])`.

- [ ] **Step 4: Build, bundle, tests, shots**

Run: `cd frontend && npm run build && npx tsc --noEmit -p tsconfig.app.json && cd .. && uv run python scripts/check_bundle.py && uv run pytest -q tests/test_frontend_source.py tests/test_copy.py`
Expected: pass. (Shots run after the lead restarts the bot: `uv run python scripts/shots.py --guild oi`, check `out/shots/desktop-oi-professions.png` and `phone-oi-professions.png`, plus the Me and Members shots.)

- [ ] **Step 5: Commit**, then run the Phase 3 gate (`check_commands` → `<scratch>/check_prof_3.log`, `check_bundle`).

```bash
git add frontend/src src/oibot_gm/web/static/app scripts/shots.py tests/test_frontend_source.py
git commit -F - <<'EOF'
A Professions page for every member: search a recipe and see who makes it, highest skill first, with names only and nobody pinged; a tab per profession lists its crafters with a skill bar and their notable recipes; officers rename a misspelt recipe or merge two spellings, the sentence shown before the write; empty states say who adds what and where. On a phone the search comes first, hits are cards and the tabs scroll sideways (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

## Phase 4 — Discord

### Task 12: `/me professions` and `/roster professions`

**Files:**
- Create: `src/oibot_gm/flows_professions.py`
- Modify: `src/oibot_gm/wizard_flows.py` (import the new module), `src/oibot_gm/discord_registry.py` (`@me.command(name="professions")` next to `/me char` ~line 921; `@roster.command(name="professions", …)` next to `/roster char` ~line 1135), `docs/manual.md` (§3 and §7 command lines)
- Test: `tests/test_professions_flows.py` (new; `tests/wizard_harness.py` `Harness` as in `tests/test_appeals.py`)

**Interfaces:**
- Consumes: `flows_roster.MemberWizard` (its `member_id`, `self_mode`, `draft`, `save_rows(interaction, rows, done)`), `Wizard.select/show/decide/refuse/finish`, `Opt`, `wizard.Forms`/`Form` for the one prose modal (read `wizard.py:74–110` and how `Wizard.decide`'s "Do it with a note…" opens its modal, ~line 281), `consequences.profession_consequence`, `Registry.professions_summary`.
- Produces: `FLOWS["me_professions"](interaction, reg)` and `FLOWS["roster_professions"](interaction, reg, member=<discord.Member>)`.

- [ ] **Step 1: Failing harness test**

```python
import asyncio, types
from oibot_gm.wizard import FLOWS
from tests.wizard_harness import Harness
from tests.test_professions import UID, LABEL


def test_me_professions_adds_a_profession_a_skill_and_a_recipe(reg):
    m = reg.members[UID]
    user = types.SimpleNamespace(id=UID, display_name=m.display_name, mention="@m")
    h = Harness(reg, None, user=user)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        assert f"{LABEL}" in h.text and "no professions on record" in h.text
        await h.press("Add profession…")
        await h.pick("Pick the profession", "leatherworking")
        await h.pick("Pick the skill", "300")
        assert "Your Leatherworking is recorded at 300." in h.text and set(h.buttons()) >= {"Do it", "Do it with a note…", "Back"}
        await h.press("Do it")
        assert "✅" in h.text
        await FLOWS["me_professions"](h.command(), reg)
        assert "Leatherworking 300" in h.text
        await h.press("Add recipe…")
        await h.submit(h.modal, recipe="Devilsaur Gauntlets")
        assert "Your Leatherworking is recorded at 300 with 1 notable recipe." in h.text
        await h.press("Do it")
    asyncio.run(go())
    c = next(c for c in reg.members[UID].active() if c.label == LABEL)
    assert c.professions[0].skill == 300 and c.professions[0].recipes[0].name == "Devilsaur Gauntlets"


def test_a_third_trade_is_refused_before_the_decide_screen(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "alchemy", "skill": 1}, {"id": "mining", "skill": 1}]}], "t", officer=True)
    m = reg.members[UID]
    h = Harness(reg, None, user=types.SimpleNamespace(id=UID, display_name=m.display_name, mention="@m"))

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add profession…")
        opts = h.options("Pick the profession")
        assert "leatherworking" not in [o.value for o in opts] and "cooking" in [o.value for o in opts], "a full trade list offers only secondary ones"
    asyncio.run(go())
```

(adapt `h.options(...)` to the harness's real accessor for a select's options; read `tests/wizard_harness.py`.)

- [ ] **Step 2: Run** → FAIL (`KeyError: 'me_professions'`)

- [ ] **Step 3: The flow**

```python
"""/me professions · /roster professions (design §5.60): a hub per character — its professions and recipes — with Add
profession… · Change skill… · Add recipe… · Remove…, each ending on the decide screen with consequences.profession_consequence,
written through MemberWizard.save_rows (THE character verb). Profession and skill are selects (Wizard rule); a recipe name
is one prose field, validated, never parsed."""
from __future__ import annotations

import discord

from . import consequences as cq
from .flows_roster import MemberWizard
from .registry import RECIPE_NAME_MAX, RegistryError
from .wizard import Field, Forms, Opt, flow

SKILL_STEPS = [str(n) for n in range(25, 301, 25)] + ["1"]  # 25-point steps, plus 1 for a profession just picked up


class ProfessionsWizard(MemberWizard):
    title = "Professions"

    async def start(self, interaction: discord.Interaction) -> None:
        chars = [c for c in self.member.active()]
        if not chars:
            await self.refuse(interaction, "There is no character to record professions on yet — register one first.")
            return
        if len(chars) == 1:
            self.draft["char"] = chars[0].label
            await self.hub(interaction)
            return
        await self.show(interaction, f"**{self.title}** · whose professions?", self.select("Pick the character", self.char_opts(chars, self.draft.get("char")), self.char_picked))

    async def char_picked(self, interaction, values):
        self.draft["char"] = values[0]
        await self.hub(interaction)

    def _char(self):
        c = next((c for c in self.member.active() if c.label == self.draft.get("char")), None)
        if c is None:
            raise RegistryError("That character is no longer on the record.")
        return c

    def _entries(self, c) -> list[dict]:
        return [p.model_dump() for p in c.professions]

    async def hub(self, interaction):
        c = self._char()
        prof = self.reg.profile
        lines = [f"**{c.label}'s professions**"]
        if c.professions:
            for p in c.professions:
                d = prof.profession(p.id)
                lines.append(f"• **{d.name} {p.skill}**" + (" · " + ", ".join(r.name for r in p.recipes) if p.recipes else ""))
        else:
            lines.append(self.reg.professions_summary(c))
        trades = [p for p in c.professions if prof.profession(p.id).group != "secondary"]
        if len(trades) >= prof.profession_rules["trade_slots"]:
            lines.append("Two trades are taken. " + ", ".join(d.name for d in prof.professions_in("secondary")) + " are open.")
        buttons = [self.button("Add profession…", self.add_profession, style=discord.ButtonStyle.primary), self.button("Change skill…", self.change_skill),
                   self.button("Add recipe…", self.add_recipe), self.button("Remove…", self.remove)]
        await self.show(interaction, "\n".join(lines), *buttons)

    # Add profession: a select of the professions still open → a skill select → decide
    async def add_profession(self, interaction):
        c = self._char()
        prof = self.reg.profile
        have = {p.id for p in c.professions}
        trades_full = sum(1 for p in c.professions if prof.profession(p.id).group != "secondary") >= prof.profession_rules["trade_slots"]
        opts = [Opt(d.id, d.name, d.group) for d in prof.professions.values() if d.id not in have and not (trades_full and d.group != "secondary")]
        if not opts:
            await self.refuse(interaction, "Every profession this character can have is already on record.", self.hub, "Back")
            return
        await self.show(interaction, f"**Add a profession to {c.label}**", self.select("Pick the profession", opts, self.profession_picked))

    async def profession_picked(self, interaction, values):
        self.draft["pid"] = values[0]
        await self.show(interaction, "**How far along is it?**", self.select("Pick the skill", [Opt(s, s) for s in SKILL_STEPS], self.skill_picked))

    async def skill_picked(self, interaction, values):
        c = self._char()
        entries = [e for e in self._entries(c) if e["id"] != self.draft["pid"]]
        keep = next((e for e in self._entries(c) if e["id"] == self.draft["pid"]), None)
        entries.append({"id": self.draft["pid"], "skill": int(values[0]), "recipes": (keep or {}).get("recipes", [])})
        await self.confirm(interaction, entries)

    async def change_skill(self, interaction):
        c = self._char()
        if not c.professions:
            await self.refuse(interaction, self.reg.professions_summary(c) + " — add one first.", self.hub, "Back")
            return
        await self.show(interaction, f"**Which of {c.label}'s professions?**",
                        self.select("Pick the profession", [Opt(p.id, self.reg.profile.profession(p.id).name, str(p.skill)) for p in c.professions], self.profession_picked))

    async def add_recipe(self, interaction):
        c = self._char()
        takes = [p for p in c.professions if self.reg.profile.profession(p.id).group != "gathering"]
        if not takes:
            await self.refuse(interaction, f"{c.label} has no profession that makes things on record — add one first.", self.hub, "Back")
            return
        if len(takes) == 1:
            self.draft["pid"] = takes[0].id
            await self.recipe_modal(interaction)
            return
        await self.show(interaction, "**Which profession makes it?**", self.select("Pick the profession", [Opt(p.id, self.reg.profile.profession(p.id).name) for p in takes], self.recipe_prof_picked))

    async def recipe_prof_picked(self, interaction, values):
        self.draft["pid"] = values[0]
        await self.recipe_modal(interaction)

    async def recipe_modal(self, interaction):
        # one prose field — build it the way Wizard.decide's note modal and flows_appeal's prose step do (wizard.Form / Fields)
        await self.form(interaction, "Add a recipe", [Field("recipe", "The recipe's name", max_length=RECIPE_NAME_MAX, required=True)], self.recipe_typed)

    async def recipe_typed(self, interaction, values: dict[str, str]):
        c = self._char()
        entries = self._entries(c)
        p = next(e for e in entries if e["id"] == self.draft["pid"])
        p["recipes"].append({"name": values["recipe"].strip(), "id": None})
        await self.confirm(interaction, entries)

    async def remove(self, interaction):
        c = self._char()
        opts = [Opt(f"p:{p.id}", self.reg.profile.profession(p.id).name, "the profession and its recipes") for p in c.professions]
        opts += [Opt(f"r:{p.id}:{i}", r.name, self.reg.profile.profession(p.id).name) for p in c.professions for i, r in enumerate(p.recipes)]
        if not opts:
            await self.refuse(interaction, self.reg.professions_summary(c) + ".", self.hub, "Back")
            return
        await self.show(interaction, f"**Remove from {c.label}**", self.select("Pick what to remove", opts[:25], self.remove_picked))

    async def remove_picked(self, interaction, values):
        c = self._char()
        entries = self._entries(c)
        kind, _, rest = values[0].partition(":")
        if kind == "p":
            entries = [e for e in entries if e["id"] != rest]
        else:
            pid, _, idx = rest.partition(":")
            p = next(e for e in entries if e["id"] == pid)
            p["recipes"] = [r for i, r in enumerate(p["recipes"]) if i != int(idx)]
        await self.confirm(interaction, entries)

    async def confirm(self, interaction, entries: list[dict]):
        c = self._char()
        try:
            said = cq.profession_consequence(self.reg, self.member, c.label, entries, by_id=interaction.user.id)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.hub, "Back")
            return
        self.draft["entries"] = entries

        async def do(i, _note):
            await self.save_rows(i, [{"label": c.label, "professions": self.draft["entries"]}], f"{c.label}: {self.reg.professions_summary(type('x', (), {'professions': self.reg.check_professions(self.draft['entries'])})())}")

        await self.decide(interaction, "\n".join(said), do=do, do_label="Do it")


@flow("me_professions")
async def me_professions(interaction, reg):
    await ProfessionsWizard(reg, interaction.user.id, self_mode=True).start(interaction)


@flow("roster_professions")
async def roster_professions(interaction, reg, member):
    await ProfessionsWizard(reg, member.id, self_mode=False).start(interaction)
```

Adapt the constructor call and `self.button` / `self.form` helper names to what `MemberWizard` / `Wizard` really expose (read `flows_roster.py:98–160` and `wizard.py:115–300`); the done-line in `do` should simply be `f"{c.label}: " + summary` computed before `decide` — replace that awkward `type(...)` expression with a local `summary` string built from `reg.check_professions(entries)`.

Register the commands:

```python
    @me.command(name="professions", description="Your characters' professions, skill and notable recipes")
    async def me_professions(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["me_professions"](interaction, reg)
```

```python
    @roster.command(name="professions", description="A member's professions on their behalf: add, change skill, recipes, remove")
    @app_commands.describe(member="whose record")
    async def roster_professions(interaction: discord.Interaction, member: discord.Member):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["roster_professions"](interaction, reg, member=member)
```

Import `flows_professions` in `wizard_flows.py`. Add the manual lines (§3 `/me professions`, §7 `/roster professions`).

- [ ] **Step 4: Run** — `uv run pytest -q tests/test_professions_flows.py tests/test_wizards.py tests/test_parity_matrix.py && uv run python scripts/check_commands.py > <scratch>/check_prof_4a.log 2>&1; echo EXIT=$? >> <scratch>/check_prof_4a.log` → PASS / EXIT=0 (the command tree must still fit Discord's limits).

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/flows_professions.py src/oibot_gm/wizard_flows.py src/oibot_gm/discord_registry.py docs/manual.md tests/test_professions_flows.py
git commit -F - <<'EOF'
/me professions opens a hub for one of your characters — its professions, skills and notable recipes — with Add profession…, Change skill…, Add recipe… and Remove…, each ending on the one confirm screen that reads "Your Leatherworking is recorded at 300 with 1 notable recipe." before anything is written; profession and skill are picked from lists, a recipe's name is typed once and kept as written; a full trade list offers only the secondary ones. /roster professions is the officers' form of the same wizard (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

### Task 13: `/professions who` and `/professions list`

**Files:**
- Modify: `src/oibot_gm/discord_registry.py` (a top-level `professions` group next to `me`, registered with `tree.add_command`; an autocomplete function next to `member_char_autocomplete` ~line 861), `docs/manual.md` (§3 commands), `tests/test_parity_matrix.py` (BY_DESIGN_ONLY / exemption rows for the two read commands if the matrix insists every slash path has web/MCP twins — the web twin is the Professions page, the MCP twin `who_can_make`)
- Test: `tests/test_professions_flows.py`

- [ ] **Step 1: Failing test**

```python
def test_professions_who_and_list_commands_exist_and_answer_from_the_catalog(reg):
    # build the tree offline exactly as scripts/check_commands.py does (lines 25–39: a bare discord.Client, CommandTree,
    # register_commands(tree, guilds, Ops(client), lambda k, v: "")) — copy that setup into a small helper in this test file
    tree = _offline_tree()
    paths = {c.qualified_name for c in tree.walk_commands()}
    assert {"professions who", "professions list"} <= paths
```

(and a direct call of the autocomplete coroutine with a fake interaction returning the catalog's names filtered by `current`).

- [ ] **Step 2: Run** → FAIL. **Step 3: The group**

```python
    professions_group = app_commands.Group(name="professions", description="Who can make what")

    async def recipe_autocomplete(interaction: discord.Interaction, current: str):
        """The guild's registered recipes, filtered as the person types (never free text into the finder)."""
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        from . import professions as pr
        cur = current.lower()
        return [app_commands.Choice(name=f"{e.name} · {len(e.crafters)} crafter{'s' if len(e.crafters) != 1 else ''}"[:100], value=e.key[:100])
                for e in pr.catalog(reg) if cur in e.name.lower()][:25]

    @professions_group.command(name="who", description="Who can make a recipe (names only — nobody is pinged)")
    @app_commands.describe(recipe="start typing the recipe's name")
    @app_commands.autocomplete(recipe=recipe_autocomplete)
    async def professions_who(interaction: discord.Interaction, recipe: str):
        reg, interaction = await need(interaction)
        if not reg:
            return
        from . import professions as pr
        hits = pr.who_can_make(reg, recipe)
        if not hits:
            await interaction.response.send_message("Nobody has registered that recipe yet — crafters add theirs with /me professions.", ephemeral=True)
            return
        lines = []
        for e in hits[:3]:
            lines.append(f"**{e.name}** · {reg.profile.profession(e.profession).name}")
            lines += [f"• **{c.label}** — {c.member} · {c.skill}" for c in e.crafters[:10]]
        lines.append("Names only — ask them. Crafters add theirs with /me professions.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @professions_group.command(name="list", description="A profession's crafters, highest skill first")
    @app_commands.describe(profession="which profession")
    async def professions_list(interaction: discord.Interaction, profession: str):
        reg, interaction = await need(interaction)
        if not reg:
            return
        from . import professions as pr
        d = reg.profile.professions.get(profession)
        if d is None:
            await interaction.response.send_message("That isn't a profession in this game version.", ephemeral=True)
            return
        cs = pr.crafters_of(reg, d.id)
        if not cs:
            await interaction.response.send_message(f"Nobody has registered {d.name} yet. A crafter adds it with /me professions or on the Me page.", ephemeral=True)
            return
        body = "\n".join(f"• **{c.label}** — {c.member} · {c.skill}" for c in cs[:25])
        await interaction.response.send_message(f"**{d.name}** · {len(cs)} crafter{'s' if len(cs) != 1 else ''}\n{body}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    tree.add_command(professions_group)
```

The `profession` option's choices come from the profile: build `app_commands.Choice(name=d.name, value=d.id)` for the loaded profile(s) at registration (follow how `CHAR_ACTION_CHOICES` or the raid choices are built; with several profiles union the ids). Manual §3: the two commands.

- [ ] **Step 4: Run** — `uv run pytest -q tests/test_professions_flows.py tests/test_parity_matrix.py tests/test_copy.py` → PASS, then the Phase 4 gate: `uv run python scripts/check_commands.py > <scratch>/check_prof_4.log 2>&1; echo EXIT=$? >> <scratch>/check_prof_4.log` → EXIT=0, `uv run python scripts/check_bundle.py` → OK.

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/discord_registry.py docs/manual.md tests/test_professions_flows.py tests/test_parity_matrix.py
git commit -F - <<'EOF'
/professions who names the crafters of a recipe as you type its name from the guild's own list, highest skill first and nobody pinged, and /professions list shows a profession's crafters; a recipe nobody registered answers with how a crafter adds one. Both reply only to the asker (design §5.60)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015RL9NVKiW8AE3woB2Dw8ko
EOF
```

---

## After the last task (the lead)

Push, restart with `launchctl kickstart -k gui/$(id -u)/gg.earlyandoften.oibot`, run `uv run python scripts/shots.py --guild oi`, open `out/shots/*professions*.png`, `*me.png`, `*members.png`; set #professions to Listening Replies + Own record only on the Ops page; register one crafter and ask "who can make …" in the channel.
