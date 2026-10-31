import pytest
from oibot_gm import configops, professions
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import Registry, RegistryError
from conftest import OWNER

# the same constants as tests/test_professions.py (a seeded test puppet; the table verb doesn't hide puppets)
UID, LABEL = Registry.TEST_BASE, "Ashvane"


def _op(**kw):
    return ConfigOp(op="character", **kw)


def test_profession_field_upserts_and_keeps_recipes(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 275, "recipes": [{"name": "Hide of the Wild"}]}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="leatherworking 300"))
    assert rows[0]["professions"][0] == {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Hide of the Wild", "id": None}]}
    assert line == f"{LABEL} · Leatherworking: 275 → 300"
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="Skinning"))
    assert rows[0]["professions"][1]["id"] == "skinning" and rows[0]["professions"][1]["skill"] is None and line == f"{LABEL} · Skinning: added"


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
    with pytest.raises(professions.NeedsProfession, match="Which profession makes Savory Deviate Delight — Leatherworking or Cooking?") as e:
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Savory Deviate Delight"))
    assert (e.value.character, e.value.recipe, e.value.candidates) == (LABEL, "Savory Deviate Delight", ["leatherworking", "cooking"])
    rows, _ = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Savory Deviate Delight", text="cooking"))
    assert rows[0]["professions"][1]["recipes"][0]["name"] == "Savory Deviate Delight"


def test_profession_remove_and_a_bad_profession(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "enchanting", "skill": 100}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession_remove", value="enchanting"))
    assert rows[0]["professions"] == [] and line == f"{LABEL} · Enchanting: removed"
    with pytest.raises(RegistryError, match="Jewelcrafting isn't a profession in this game version."):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="jewelcrafting 50"))


def test_a_fisher_is_not_asked_which_profession_makes_a_recipe(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "fishing", "skill": 150}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Devilsaur Gauntlets"))
    assert rows[0]["professions"][0]["recipes"] == [{"name": "Devilsaur Gauntlets", "id": None}] and line == f"{LABEL} · Leatherworking recipes: + Devilsaur Gauntlets"


def test_a_hint_naming_a_profession_the_character_lacks_is_refused(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(RegistryError, match=f"^{LABEL} has no Tailoring on record\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Mooncloth Bag", text="tailoring"))


def test_recipe_remove_finds_the_profession_that_holds_it(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [
        {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]},
        {"id": "cooking", "skill": 300, "recipes": [{"name": "Savory Deviate Delight"}]}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="savory deviate delight"))
    assert rows[0]["professions"][1]["recipes"] == [] and rows[0]["professions"][0]["recipes"] == [{"name": "Devilsaur Gauntlets", "id": None}]
    assert line == f"{LABEL} · Cooking recipes: − Savory Deviate Delight"


def test_an_unknown_character_field_lists_the_profession_fields(reg):
    m = reg.members[UID]
    with pytest.raises(RegistryError, match="flex \\| profession \\| profession_remove \\| recipe_add \\| recipe_remove\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="haircut", value="x"))


def test_recipe_remove_of_a_recipe_nobody_holds_is_refused_not_asked(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "cooking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(RegistryError, match=f"^{LABEL} has no Mooncloth Bag on record\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="Mooncloth Bag"))


def test_a_hint_naming_a_profession_that_makes_nothing_is_refused(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "fishing", "skill": 150}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(RegistryError, match="^Fishing has no recipes\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Big Fish", text="fishing"))


def _real(reg, uid, name, cls="Hunter", spec="Survival", main=True):
    reg.add_character(uid, name, name, cls, spec, None, main)
    return name


def test_a_recipe_with_no_character_named_goes_to_the_one_crafter(reg):
    """"I can make Witherbark Boots" without a character: the player matters more than the character (design §5.60)."""
    a = _real(reg, 9100, "Thornwick")
    b = _real(reg, 9100, "Thornalt", cls="Priest", spec="Holy", main=False)
    reg.save_character_table(9100, [{"label": b, "professions": [{"id": "leatherworking", "skill": 300}]}], "t", officer=True)
    m = reg.members[9100]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, field="recipe_add", value="Witherbark Boots"))
    assert rows[0]["label"] == b and line == f"{b} · Leatherworking recipes: + Witherbark Boots", "the alt is the only crafter"
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, field="profession", value="skinning 150"))
    assert rows[0]["label"] == a and line == f"{a} · Skinning: 150", "a profession with no character goes to the main"


def test_a_recipe_with_two_crafters_and_no_main_among_them_asks_which(reg):
    _real(reg, 9101, "Ravenmoor")
    _real(reg, 9101, "Ravenalt", cls="Priest", spec="Holy", main=False)
    _real(reg, 9101, "Raventwo", cls="Mage", spec="Frost", main=False)
    reg.save_character_table(9101, [{"label": "Ravenalt", "professions": [{"id": "tailoring", "skill": 300}]},
                                    {"label": "Raventwo", "professions": [{"id": "enchanting", "skill": 300}]}], "t", officer=True)
    m = reg.members[9101]
    with pytest.raises(RegistryError, match=r"^Which character — Ravenalt or Raventwo\?$"):
        configops._character_rows(reg, m, _op(member=m.display_name, field="recipe_add", value="Mooncloth Bag"))
    rows, _ = configops._character_rows(reg, m, _op(member=m.display_name, field="recipe_add", value="Mooncloth Bag", character="Ravenalt"))
    assert rows[0]["label"] == "Ravenalt"


# ---- a recipe on a character with no profession that makes things: ask, then record both (design §5.60 follow-up)

MAKERS = ["alchemy", "blacksmithing", "enchanting", "engineering", "leatherworking", "tailoring", "cooking", "first_aid"]


def test_a_recipe_with_a_guess_on_a_bare_character_adds_the_profession_too(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "skinning", "skill": 200}]}], "t", officer=True)
    m = reg.members[UID]
    op = _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Cloak", text="leatherworking")
    rows, line = configops._character_rows(reg, m, op)
    assert rows == [{"label": LABEL, "professions": [{"id": "skinning", "skill": 200, "recipes": []},
                                                     {"id": "leatherworking", "skill": None, "recipes": [{"name": "Green Dragonscale Cloak", "id": None}]}]}]
    assert line == f"{LABEL} · Leatherworking: added with Green Dragonscale Cloak"
    said = configops.describe(reg, op, configops.PLAIN_WEB, by_id=UID)
    assert said.split("\n") == [f"{LABEL} has no Leatherworking on record yet.",
                                f"Apply records Leatherworking on {LABEL} and adds Green Dragonscale Cloak to the recipes {LABEL} makes.",
                                f'Say the skill when you know it, like "{LABEL} is 300 Leatherworking".']
    reg.save_character_table(UID, rows, "t", officer=True)
    c = next(c for c in reg.members[UID].active() if c.label == LABEL)
    assert reg.professions_summary(c) == "Skinning 200 · Leatherworking"


def test_a_recipe_without_a_guess_on_a_bare_character_asks_which_profession(reg):
    m = reg.members[UID]
    with pytest.raises(professions.NeedsProfession, match=r"^Which profession makes Green Dragonscale Cloak\?$") as e:
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Cloak"))
    assert (e.value.character, e.value.recipe, e.value.candidates) == (LABEL, "Green Dragonscale Cloak", MAKERS)
    with pytest.raises(professions.NeedsProfession):
        configops.describe(reg, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Cloak"), strict=True)
    assert configops.describe(reg, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Cloak")) == \
        "Which profession makes Green Dragonscale Cloak?", "the web and MCP print the question alone"


def test_the_preview_says_the_confirm_sentences_for_profession_fields(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Hide of the Wild"}]}]}], "t", officer=True)
    m = reg.members[UID]
    add = _op(member=m.display_name, character=LABEL, field="recipe_add", value="Devilsaur Gauntlets")
    assert configops.describe(reg, add, by_id=UID) == "Your Leatherworking gains Devilsaur Gauntlets."
    assert configops.describe(reg, add, by_id=OWNER) == f"{LABEL}'s Leatherworking gains Devilsaur Gauntlets."
    rem = _op(member=m.display_name, character=LABEL, field="recipe_remove", value="hide of the wild")
    assert configops.describe(reg, rem, by_id=OWNER) == f"{LABEL}'s Leatherworking no longer lists Hide of the Wild."
    assert configops.describe(reg, rem, by_id=UID) == "Your Leatherworking no longer lists Hide of the Wild."
    prof = _op(member=m.display_name, character=LABEL, field="profession", value="leatherworking 275")
    assert configops.describe(reg, prof, by_id=UID) == "Your Leatherworking is recorded at 275 with 1 notable recipe."
    drop = _op(member=m.display_name, character=LABEL, field="profession_remove", value="leatherworking")
    assert configops.describe(reg, drop, by_id=OWNER) == f"{m.display_name}'s Leatherworking is removed from their record."


def test_only_professions_the_character_can_hold_are_offered(reg):
    """Two gathering trades taken: only Cooking or First Aid can still hold a recipe — the question offers those, and a
    Leatherworking guess becomes that question instead of a promise Apply would break."""
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "herbalism", "skill": 200}, {"id": "mining", "skill": 200}]}], "t", officer=True)
    m = reg.members[UID]
    for text in (None, "leatherworking"):
        with pytest.raises(professions.NeedsProfession, match=r"^Which profession makes Green Dragonscale Cloak\?$") as e:
            configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Cloak", text=text))
        assert e.value.candidates == ["cooking", "first_aid"]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Spiced Wolf Meat", text="cooking"))
    assert line == f"{LABEL} · Cooking: added with Spiced Wolf Meat"
