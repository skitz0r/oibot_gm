"""A recipe's identity is the game's recipe id (armory link, phase 1 — spec §4.2, §4.3): check_professions resolves a
typed name against the profile's list, refuses what is not on it, round-trips an already-stored unmatched recipe, and
the one-time migration matches the typed recipes on record. A profile without a list behaves exactly as before."""
import dataclasses
import subprocess
from pathlib import Path

import pytest

from oibot_gm import professions as pr
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import Profession, Recipe, Registry, RegistryError

ROOT = Path(__file__).resolve().parents[1]
UID, LABEL = 9100, "Thornwick"  # a real member (the fixture's seeded members are puppets, which the finders never name)


def _real(reg):
    reg.add_character(UID, "Kestrel", LABEL, "Hunter", "Survival", None, True)


def _char(reg, uid=UID, label=LABEL):
    return next(c for c in reg.members[uid].active() if c.label == label)


def _commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, check=True, capture_output=True, text=True)
    return int(out.stdout.strip())


def _store_typed(reg, uid, label, profs: list[Profession]):
    """Old data: recipes typed before the list, written straight to the record (as the records on disk hold them)."""
    m = reg.members[uid]
    _char(reg, uid, label).professions = profs
    reg.save(m, "seed typed recipes")


def test_a_typed_name_takes_the_lists_id_and_spelling(reg):
    out = reg.check_professions([{"id": "leatherworking", "skill": 300, "recipes": [
        {"name": "devilsaur  GAUNTLETS"}, {"name": "Devilsaur Gauntlets"}, {"id": 12355}]}])
    assert [(r.id, r.name) for r in out[0].recipes] == [(10789, "Devilsaur Gauntlets"), (12355, "Hide of the Wild")], \
        "the list's id and spelling; the same recipe twice is one (dedupe by id); an id alone takes the list's name"
    # R6: a name that is two recipes of one profession (the original and its twin) resolves to the lowest id
    twins = sorted(r.id for r in reg.profile.recipes_named("lesser arcane elixir") if r.profession == "alchemy")
    assert len(twins) == 2
    out = reg.check_professions([{"id": "alchemy", "skill": 300, "recipes": [{"name": "Lesser Arcane Elixir"}]}])
    assert [r.id for r in out[0].recipes] == [twins[0]]
    # R7: with a list, a gathering profession has the list's recipes (Mining smelts)
    out = reg.check_professions([{"id": "mining", "skill": 300, "recipes": [{"name": "Smelt Iron"}]}])
    assert [(r.id, r.name) for r in out[0].recipes] == [(1847, "Smelt Iron")]


def test_a_recipe_of_another_profession_is_refused(reg):
    with pytest.raises(RegistryError, match=r"^Devilsaur Gauntlets isn't a Tailoring recipe\.$"):
        reg.check_professions([{"id": "tailoring", "skill": 300, "recipes": [{"id": 10789, "name": "Devilsaur Gauntlets"}]}])
    with pytest.raises(RegistryError, match=r"^Devilsaur Gauntlets isn't a Tailoring recipe\.$"):
        reg.check_professions([{"id": "tailoring", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}])


def test_an_unlisted_name_is_refused_on_entry(reg):
    with pytest.raises(RegistryError, match=r"^Devilsaur Gauntlet isn't a recipe in the game's list\.$"):
        reg.check_professions([{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet"}]}])
    with pytest.raises(RegistryError, match=r"isn't a recipe in the game's list\.$"):
        reg.check_professions([{"id": "leatherworking", "skill": 300, "recipes": [{"id": 999999999, "name": "Made Up"}]}])
    _real(reg)
    with pytest.raises(RegistryError, match="isn't a recipe in the game's list"):
        reg.save_character_table(UID, [{"label": LABEL, "professions": [
            {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet"}]}]}], "t", officer=True)
    assert _char(reg).professions == [], "nothing was written"


def test_an_unmatched_stored_recipe_survives_a_table_save(reg):
    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlet")])])
    entries = reg.professions_entries(_char(reg))
    assert entries == [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet", "id": None, "unmatched": True}]}]
    entries[0]["skill"] = 295
    entries[0]["recipes"].append({"name": "Hide of the Wild"})
    reg.save_character_table(UID, [{"label": LABEL, "professions": entries}], "t", officer=True)
    p = _char(reg).professions[0]
    assert p.skill == 295
    assert [(r.name, r.id) for r in p.recipes] == [("Devilsaur Gauntlet", None), ("Hide of the Wild", 12355)], \
        "the unmatched recipe is kept as it was, no new field on the record"
    assert "unmatched" not in p.recipes[0].model_dump()
    assert pr.unmatched(reg) == [{"uid": UID, "member": "Kestrel", "label": LABEL, "profession": "leatherworking", "name": "Devilsaur Gauntlet"}]


def test_migration_matches_and_is_idempotent(reg):
    _real(reg)
    reg.add_character(UID, "Kestrel", "Hollowfen", "Hunter", "Survival", None, False)
    _store_typed(reg, UID, LABEL, [
        Profession(id="leatherworking", skill=300, recipes=[
            Recipe(name="devilsaur  gauntlets"), Recipe(name="Devilsaur Gauntlets"), Recipe(name="Devilsaur Gauntlet"),
            Recipe(name="Mooncloth Bag")]),  # Mooncloth Bag is a Tailoring recipe: a cross-profession hit moves nothing
        Profession(id="alchemy", skill=300, recipes=[Recipe(name="Lesser Arcane Elixir")])])
    _store_typed(reg, UID, "Hollowfen", [Profession(id="tailoring", skill=300, recipes=[Recipe(name="Mooncloth Bag")])])
    before = _commits(reg)
    heard = []
    reg.listeners.append(lambda r, kind, lines: heard.append(lines))
    assert reg.migrate_recipes() == {"matched": 4, "unmatched": 2}
    assert heard == [], "no change-log line per member: the ops line says it once"
    assert _commits(reg) == before + 1, "one commit for the guild"
    lw, al = _char(reg).professions
    assert [(r.name, r.id) for r in lw.recipes] == [("Devilsaur Gauntlets", 10789), ("Devilsaur Gauntlet", None), ("Mooncloth Bag", None)], \
        "matched once (two spellings of one recipe are one), the rest kept as they were"
    twins = sorted(r.id for r in reg.profile.recipes_named("lesser arcane elixir") if r.profession == "alchemy")
    assert [r.id for r in al.recipes] == [twins[0]], "R6: the lowest id of the name within the profession"
    assert [(r.name, r.id) for r in _char(reg, UID, "Hollowfen").professions[0].recipes] == [("Mooncloth Bag", 10431)]
    assert pr.migrated_line({"matched": 4, "unmatched": 2}) == "Recipes matched to the game's list: 4 matched, 2 not in the list."
    assert reg.migrate_recipes() == {"matched": 0, "unmatched": 2}
    assert _commits(reg) == before + 1, "a second run writes nothing"
    assert [(r["label"], r["name"]) for r in pr.unmatched(reg)] == [(LABEL, "Devilsaur Gauntlet"), (LABEL, "Mooncloth Bag")]


def test_unmatched_lists_visible_characters_only(reg):
    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlet")])])
    puppet = Registry.TEST_BASE
    plabel = reg.members[puppet].active()[0].label
    _store_typed(reg, puppet, plabel, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlet")])])
    assert [r["uid"] for r in pr.unmatched(reg)] == [UID], "a puppet is never named"
    reg.member_left(UID)
    assert pr.unmatched(reg) == []


def test_trusted_skips_the_hand_checks(reg):
    entries = [{"id": "leatherworking", "skill": 375, "recipes": [{"id": 999999999, "name": "A Recipe From a Newer Build"}]},
               {"id": "tailoring", "skill": 375}, {"id": "mining", "skill": 375}]
    with pytest.raises(RegistryError):
        reg.check_professions(entries)
    out = reg.check_professions(entries, trusted=True)
    assert [(p.id, p.skill) for p in out] == [("leatherworking", 375), ("tailoring", 375), ("mining", 375)]
    assert [(r.id, r.name) for r in out[0].recipes] == [(999999999, "A Recipe From a Newer Build")]
    with pytest.raises(RegistryError, match="isn't a profession in this game version"):
        reg.check_professions([{"id": "jewelcrafting", "skill": 10}], trusted=True)


def test_tbc_check_is_unchanged(reg):
    tbc = GameProfile.load(ROOT / "profiles" / "tbc")
    assert not tbc.has_recipe_list
    reg_tbc = Registry(reg.store, reg.key, tbc)
    with pytest.raises(RegistryError, match="Leatherworking isn't a profession in this game version."):
        reg_tbc.check_professions([{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet"}]}])
    assert reg_tbc.migrate_recipes() == {"matched": 0, "unmatched": 0}
    assert pr.unmatched(reg_tbc) == []
    # a version with professions but no recipe list: typed recipes as before (any name, the makes rule, the seed)
    bare = dataclasses.replace(reg.profile, recipes={})
    assert not bare.has_recipe_list
    reg.profile = bare
    out = reg.check_professions([{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet"}, {"name": "devilsaur gauntlet"}]}])
    assert [(r.name, r.id) for r in out[0].recipes] == [("Devilsaur Gauntlet", None)]
    with pytest.raises(RegistryError, match="A gathering profession has no recipes."):
        reg.check_professions([{"id": "mining", "skill": 300, "recipes": [{"name": "Smelt Iron"}]}])
    with pytest.raises(RegistryError, match="A character has two trade professions"):
        reg.check_professions([{"id": "alchemy", "skill": 1}, {"id": "tailoring", "skill": 1}, {"id": "mining", "skill": 1}], trusted=False)


def test_the_start_migrates_each_guild_and_says_so_once(reg):
    """The ready hook: Registry.migrate_recipes per guild in a thread; one ops line when it matched, none on a later start."""
    import asyncio
    from types import SimpleNamespace

    from oibot_gm.discord_bot import OibotGM

    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlets"), Recipe(name="Devilsaur Gauntlet")])])
    said: list[str] = []

    async def emit(cfg, level, text):
        said.append(text)

    bot = SimpleNamespace(registries=SimpleNamespace(ordered=lambda: [reg]), ops=SimpleNamespace(emit=emit))
    asyncio.run(OibotGM._recipes_migrate(bot))
    asyncio.run(OibotGM._recipes_migrate(bot))
    assert said == ["Recipes matched to the game's list: 1 matched, 1 not in the list."]


def test_a_flagged_new_name_is_refused_on_a_members_own_save(reg):
    """`unmatched: true` keeps only what the character already holds: a new name carrying the flag is an ordinary name."""
    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlet")])])
    entries = reg.professions_entries(_char(reg))
    entries[0]["recipes"].append({"name": "Gauntlets of Make Believe", "id": None, "unmatched": True})
    with pytest.raises(RegistryError, match=r"^Gauntlets of Make Believe isn't a recipe in the game's list\.$"):
        reg.save_character_table(UID, [{"label": LABEL, "professions": entries}], "Kestrel", by_id=UID)
    with pytest.raises(RegistryError, match=r"isn't a recipe in the game's list\.$"):  # no character: nothing is kept
        reg.check_professions([{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet", "unmatched": True}]}])
    # the stored one still survives the member's own save; the same flag under another profession keeps nothing
    entries = reg.professions_entries(_char(reg))
    reg.save_character_table(UID, [{"label": LABEL, "professions": entries}], "Kestrel", by_id=UID)
    assert [(r.name, r.id) for r in _char(reg).professions[0].recipes] == [("Devilsaur Gauntlet", None)]
    with pytest.raises(RegistryError, match="isn't a recipe in the game's list"):
        reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "tailoring", "skill": 300, "recipes": [
            {"name": "Devilsaur Gauntlet", "id": None, "unmatched": True}]}]}], "Kestrel", by_id=UID)


def test_a_flagged_name_the_list_has_takes_its_id(reg):
    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="devilsaur  gauntlets")])])
    entries = reg.professions_entries(_char(reg))
    assert entries[0]["recipes"][0]["unmatched"] is True
    reg.save_character_table(UID, [{"label": LABEL, "professions": entries}], "Kestrel", by_id=UID)
    assert [(r.name, r.id) for r in _char(reg).professions[0].recipes] == [("Devilsaur Gauntlets", 10789)]


def test_a_stored_unmatched_recipe_resent_without_the_flag_is_kept(reg):
    """MCP set_professions and older clients send the record back without `unmatched: true`: the stored set is the
    authority, the flag a convenience. A name that is not stored on the character is still refused."""
    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlet")])])
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300, "recipes": [
        {"name": "Devilsaur Gauntlet", "id": None}, {"name": "Hide of the Wild"}]}]}], "Kestrel", by_id=UID)
    assert [(r.name, r.id) for r in _char(reg).professions[0].recipes] == [("Devilsaur Gauntlet", None), ("Hide of the Wild", 12355)]
    with pytest.raises(RegistryError, match=r"^Gauntlets of Make Believe isn't a recipe in the game's list\.$"):
        reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300, "recipes": [
            {"name": "Devilsaur Gauntlet"}, {"name": "Gauntlets of Make Believe"}]}]}], "Kestrel", by_id=UID)


def test_a_failed_migration_write_leaves_memory_as_it_was(reg, monkeypatch):
    _real(reg)
    _store_typed(reg, UID, LABEL, [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Devilsaur Gauntlets")])])
    before = reg.members[UID].model_dump()

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(reg.store, "write_text", broken)
    with pytest.raises(OSError):
        reg.migrate_recipes()
    assert reg.members[UID].model_dump() == before, "the live record changes only after the write"
    monkeypatch.undo()
    assert reg.migrate_recipes() == {"matched": 1, "unmatched": 0}
    assert [r.id for r in _char(reg).professions[0].recipes] == [10789]
