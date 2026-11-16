import pytest
from oibot_gm import configops, professions
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import Registry, RegistryError
from conftest import OWNER

# the same constants as tests/test_professions.py (a seeded test puppet; the table verb doesn't hide puppets)
UID, LABEL = Registry.TEST_BASE, "Ashvane"


def _op(**kw):
    return ConfigOp(op="character", **kw)


# ---- professions are looked up in chat, never written from a message (the owner, 2026-11-16)

def test_no_profession_field_is_taken_from_a_message(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 275, "recipes": [{"name": "Hide of the Wild"}]}]}], "t", officer=True)
    m = reg.members[UID]
    before = reg.professions_entries(next(c for c in m.active() if c.label == LABEL))
    for field, value in (("profession", "leatherworking 300"), ("profession_remove", "leatherworking"),
                         ("recipe_add", "Devilsaur Gauntlets"), ("recipe_remove", "Hide of the Wild")):
        for label in (LABEL, ""):
            op = _op(member=m.display_name, character=label, field=field, value=value)
            with pytest.raises(RegistryError) as e:
                configops._character_rows(reg, m, op)
            assert str(e.value) == configops.PROFESSIONS_NOT_HERE, (field, label)
            with pytest.raises(RegistryError):
                configops.describe(reg, op, strict=True)
            assert configops.PROFESSIONS_NOT_HERE in configops.describe(reg, op), "the lenient form prints the sentence"
    assert reg.professions_entries(next(c for c in reg.members[UID].active() if c.label == LABEL)) == before


def test_the_parser_is_told_professions_are_not_set_here():
    text = configops.SCHEMA_TEXT
    assert "recipe_add" not in text and "profession_remove" not in text and "field=profession" not in text
    assert "a character's professions, skill or recipes — NEVER an op" in text
    assert "profession" not in configops.CHARACTER_FIELDS


# ---- the officers' fix for typed recipes the game's list does not have (armory link §4.3)

def _typed(reg, uid, label, pid, names):
    from oibot_gm.registry import Profession, Recipe
    m = reg.members[uid]
    c = next(c for c in m.active() if c.label == label)
    c.professions = [Profession(id=pid, skill=300, recipes=[Recipe(name=n) for n in names])]
    reg.save(m, "typed recipes")


def test_fix_unmatched_pick_and_remove(reg):
    reg.add_character(9100, "Kestrel", "Thornwick", "Hunter", "Survival", None, True)
    _typed(reg, 9100, "Thornwick", "leatherworking", ["Devilsaur Gauntlet", "Bold Gloves", "Hide of the Wild"])
    reg.migrate_recipes()
    assert [r["name"] for r in professions.unmatched(reg)] == ["Bold Gloves", "Devilsaur Gauntlet"]
    said = professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Devilsaur Gauntlet", 10789, "Officer", by_id=OWNER, preview=True)
    assert said == ["Thornwick's Leatherworking lists Devilsaur Gauntlets in place of Devilsaur Gauntlet."]
    assert len(professions.unmatched(reg)) == 2, "a preview writes nothing"
    assert professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "devilsaur gauntlet", 10789, "Officer", by_id=OWNER) == said
    assert [r["name"] for r in professions.unmatched(reg)] == ["Bold Gloves"]
    c = reg.members[9100].main
    assert [(r.id, r.name) for r in c.professions[0].recipes] == [(10789, "Devilsaur Gauntlets"), (None, "Bold Gloves"), (12355, "Hide of the Wild")], "in place"
    said = professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Bold Gloves", None, "Officer", by_id=OWNER)
    assert said == ["Bold Gloves comes off Thornwick's Leatherworking."]
    assert professions.unmatched(reg) == [] and [r.id for r in reg.members[9100].main.professions[0].recipes] == [10789, 12355]
    with pytest.raises(RegistryError, match=r"^Thornwick's Leatherworking no longer lists Bold Gloves\.$"):
        professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Bold Gloves", None, "Officer", by_id=OWNER)
    # a recipe of another profession is refused; a pick the record already lists drops the typed one
    _typed(reg, 9100, "Thornwick", "leatherworking", ["Devilsaur Gauntlet", "Devilsaur Gauntlets"])
    reg.members[9100].main.professions[0].recipes[1].id = 10789
    with pytest.raises(RegistryError, match=r"^Mooncloth Bag isn't a Leatherworking recipe\.$"):
        professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Devilsaur Gauntlet", 10431, "Officer", by_id=OWNER)
    said = professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Devilsaur Gauntlet", 10789, "Officer", by_id=OWNER)
    assert said == ["Thornwick's Leatherworking already lists Devilsaur Gauntlets, so Devilsaur Gauntlet comes off."]
    assert [(r.id, r.name) for r in reg.members[9100].main.professions[0].recipes] == [(10789, "Devilsaur Gauntlets")]


def test_fix_unmatched_never_overwrites_a_change_between_read_and_write(reg, monkeypatch):
    from oibot_gm.registry import CharactersChanged

    reg.add_character(9100, "Kestrel", "Thornwick", "Hunter", "Survival", None, True)
    _typed(reg, 9100, "Thornwick", "leatherworking", ["Bold Gloves"])
    real = reg.professions_entries

    def member_saves_meanwhile(c):
        out = real(c)
        reg.save_character_table(9100, [{"label": "Thornwick", "professions": [{"id": "leatherworking", "skill": 300, "recipes": [
            {"name": "Bold Gloves"}, {"name": "Hide of the Wild"}]}]}], "Kestrel", by_id=9100)
        return out
    monkeypatch.setattr(reg, "professions_entries", member_saves_meanwhile)
    with pytest.raises(CharactersChanged):
        professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Bold Gloves", None, "Officer", by_id=OWNER)
    assert [r.name for r in reg.members[9100].main.professions[0].recipes] == ["Bold Gloves", "Hide of the Wild"], "the member's save stands"


def test_fix_unmatched_preview_runs_the_writes_refusals(reg, monkeypatch):
    reg.add_character(9100, "Kestrel", "Thornwick", "Hunter", "Survival", None, True)
    _typed(reg, 9100, "Thornwick", "leatherworking", ["Bold Gloves"])
    seen = []

    def refuse(*a, **k):
        seen.append(k.get("rev"))
        raise RegistryError("Another officer's rehearsal character is theirs to change.")
    monkeypatch.setattr(reg, "check_character_table", refuse)
    with pytest.raises(RegistryError, match="rehearsal"):
        professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Bold Gloves", None, "Officer", by_id=OWNER, preview=True)
    assert seen and seen[0] is not None, "the preview checks the table at the rev it read"


def test_fix_unmatched_needs_a_list(reg):
    import dataclasses
    reg.add_character(9100, "Kestrel", "Thornwick", "Hunter", "Survival", None, True)
    _typed(reg, 9100, "Thornwick", "leatherworking", ["Bold Gloves"])
    reg.profile = dataclasses.replace(reg.profile, recipes={})
    with pytest.raises(RegistryError, match=r"^This game version has no recipe list\.$"):
        professions.fix_unmatched(reg, 9100, "Thornwick", "leatherworking", "Bold Gloves", None, "Officer", by_id=OWNER)

