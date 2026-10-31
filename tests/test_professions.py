from pathlib import Path

import pytest

from conftest import OWNER
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import CharactersChanged, Registry, RegistryError

ROOT = Path(__file__).resolve().parents[1]
# conftest's `reg` seeds TEST_MEMBERS members from Registry.test_roster() at TEST_BASE + i (puppets: the fixture guild has
# no other members). The first two are the registered members these tests write; the officer is the guild's owner
# (conftest gives officers by role, not by a member id).
UID, LABEL = Registry.TEST_BASE, "Ashvane"
OTHER_UID, OTHER_LABEL = Registry.TEST_BASE + 1, "Brightmoor"
OFFICER_UID = OWNER


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
    ([{"id": "alchemy", "skill": 10, "recipes": [{"name": "Flask", "id": "abc"}]}], "A recipe's id is a number."),
    ([{"id": "jewel_crafting", "skill": 10}], "Jewel Crafting isn't a profession in this game version."),
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


from oibot_gm import professions as pr
from oibot_gm import consequences as cq

# The fixture's seeded members are test puppets, and the finders never name a puppet: the finder tests register two real
# members first.
REAL_UID, REAL_LABEL = 9100, "Thornwick"
REAL2_UID, REAL2_LABEL = 9101, "Ravenmoor"


def _real(reg):
    reg.add_character(REAL_UID, "Kestrel", REAL_LABEL, "Hunter", "Survival", None, True)
    reg.add_character(REAL2_UID, "Wren", REAL2_LABEL, "Hunter", "Marksmanship", None, True)


def _give(reg, uid, label, entries):
    reg.save_character_table(uid, [{"label": label, "professions": entries}], "t", officer=True)


def test_catalog_folds_case_and_spaces(reg):
    _real(reg)
    _give(reg, REAL_UID, REAL_LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}])
    _give(reg, REAL2_UID, REAL2_LABEL, [{"id": "leatherworking", "skill": 275, "recipes": [{"name": "devilsaur  gauntlets"}]}])
    cat = pr.catalog(reg)
    assert [e.name for e in cat] == ["Devilsaur Gauntlets"], "one recipe, the spelling most crafters use"
    assert [c.label for c in cat[0].crafters] == [REAL_LABEL, REAL2_LABEL], "highest skill first"


def test_who_can_make_matches_exact_then_by_words(reg):
    _real(reg)
    _give(reg, REAL_UID, REAL_LABEL, [{"id": "tailoring", "skill": 300, "recipes": [{"name": "Mooncloth Bag"}, {"name": "Bottomless Bag"}]}])
    assert [e.name for e in pr.who_can_make(reg, "mooncloth bag")] == ["Mooncloth Bag"]
    assert [e.name for e in pr.who_can_make(reg, "anyone able to make a Mooncloth Bag? got the cloth")] == ["Mooncloth Bag"]
    assert pr.who_can_make(reg, "who can make a Gnomish Death Ray") == []


def test_crafters_of_lists_a_profession_highest_first(reg):
    _real(reg)
    _give(reg, REAL_UID, REAL_LABEL, [{"id": "alchemy", "skill": 240}])
    _give(reg, REAL2_UID, REAL2_LABEL, [{"id": "alchemy", "skill": 300}])
    assert [(c.label, c.skill) for c in pr.crafters_of(reg, "alchemy")] == [(REAL2_LABEL, 300), (REAL_LABEL, 240)]
    assert [c.label for c in pr.crafters_of(reg, "alchemy", min_skill=300)] == [REAL2_LABEL]


def test_finders_skip_retired_puppets_and_departed(reg):
    _real(reg)
    flask = [{"id": "alchemy", "skill": 300, "recipes": [{"name": "Flask of the Titans"}]}]
    _give(reg, REAL_UID, REAL_LABEL, flask)
    reg.member_left(REAL_UID)  # Member.left_at: not in the server
    assert pr.crafters_of(reg, "alchemy") == [] and pr.who_can_make(reg, "flask of the titans") == []
    _give(reg, UID, LABEL, flask)  # a test puppet
    reg.add_character(REAL2_UID, "Wren", "Hollowfen", "Hunter", "Survival", None, False)
    _give(reg, REAL2_UID, "Hollowfen", flask)
    assert [c.label for c in pr.crafters_of(reg, "alchemy")] == ["Hollowfen"], "the puppet is never named"
    reg.retire(REAL2_UID, "Hollowfen")
    assert pr.crafters_of(reg, "alchemy") == [] and pr.catalog(reg) == [] and pr.who_can_make(reg, "flask of the titans") == []


def test_rename_and_merge_rewrite_the_records(reg):
    _real(reg)
    _give(reg, REAL_UID, REAL_LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlet"}]}])
    _give(reg, REAL2_UID, REAL2_LABEL, [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}])
    said = pr.merge_recipe(reg, "devilsaur gauntlet", "devilsaur gauntlets", "Officer", by_id=OFFICER_UID, preview=True)
    assert said == ["Devilsaur Gauntlet is merged into Devilsaur Gauntlets.", f"{reg.members[REAL_UID].display_name}'s record now reads Devilsaur Gauntlets."]
    assert len(pr.catalog(reg)) == 2, "a preview writes nothing"
    pr.merge_recipe(reg, "devilsaur gauntlet", "devilsaur gauntlets", "Officer", by_id=OFFICER_UID)
    assert [e.name for e in pr.catalog(reg)] == ["Devilsaur Gauntlets"] and len(pr.catalog(reg)[0].crafters) == 2
    pr.rename_recipe(reg, "devilsaur gauntlets", "Gauntlets of the Devilsaur", "Officer", by_id=OFFICER_UID)
    assert [e.name for e in pr.catalog(reg)] == ["Gauntlets of the Devilsaur"]
    with pytest.raises(RegistryError, match="Nobody has registered"):
        pr.rename_recipe(reg, "nothing here", "X", "Officer", by_id=OFFICER_UID)


def test_rename_saves_a_member_once_for_all_their_characters(reg):
    _real(reg)
    reg.add_character(REAL_UID, "Kestrel", "Hollowfen", "Hunter", "Survival", None, False)
    bag = [{"id": "tailoring", "skill": 300, "recipes": [{"name": "Mooncloth Bagg"}]}]
    reg.save_character_table(REAL_UID, [{"label": REAL_LABEL, "professions": bag}, {"label": "Hollowfen", "professions": bag}], "t", officer=True)
    said = pr.rename_recipe(reg, "mooncloth bagg", "Mooncloth Bag", "Officer", by_id=OFFICER_UID)
    assert said == ["Mooncloth Bagg is renamed Mooncloth Bag.", "Kestrel's record now reads Mooncloth Bag."], "one sentence per member"
    names = {c.label: [r.name for p in c.professions for r in p.recipes] for c in reg.members[REAL_UID].active()}
    assert names == {REAL_LABEL: ["Mooncloth Bag"], "Hollowfen": ["Mooncloth Bag"]}


def test_rename_onto_another_recipe_is_refused(reg):
    _real(reg)
    _give(reg, REAL_UID, REAL_LABEL, [{"id": "tailoring", "skill": 300, "recipes": [{"name": "Mooncloth Bag"}, {"name": "Bottomless Bag"}]}])
    with pytest.raises(RegistryError, match="Bottomless Bag is already a recipe — merge into it instead."):
        pr.rename_recipe(reg, "mooncloth bag", "bottomless  bag", "Officer", by_id=OFFICER_UID)
    assert [e.name for e in pr.catalog(reg)] == ["Bottomless Bag", "Mooncloth Bag"]


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


from oibot_gm.importers import professions as pimp


def test_import_boundary_recognises_nothing_yet():
    assert pimp.detect("anything") is None and pimp.FORMATS == {}
    with pytest.raises(pimp.ImportError_, match="isn't a professions export the bot can read yet"):
        pimp.parse("anything")
