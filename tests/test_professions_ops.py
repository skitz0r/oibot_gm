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
    assert rows[0]["professions"][0] == {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Hide of the Wild", "id": 12355}]}
    assert line == f"{LABEL} · Leatherworking: 275 → 300"
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="profession", value="Skinning"))
    assert rows[0]["professions"][1]["id"] == "skinning" and rows[0]["professions"][1]["skill"] is None and line == f"{LABEL} · Skinning: added"


def test_recipe_add_and_remove_name_the_profession_in_the_line(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Devilsaur Gauntlets"))
    assert rows[0]["professions"][0]["recipes"] == [{"id": 10789, "name": "Devilsaur Gauntlets"}] and line == f"{LABEL} · Leatherworking recipes: + Devilsaur Gauntlets"
    reg.save_character_table(UID, rows, "t", officer=True)
    with pytest.raises(RegistryError, match=f"^{LABEL} already has Devilsaur Gauntlets on record\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="devilsaur gauntlets"))
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="devilsaur gauntlets"))
    assert rows[0]["professions"][0]["recipes"] == [] and line.endswith("− Devilsaur Gauntlets")


def test_recipe_add_needs_the_profession_when_two_could_hold_it(reg):
    """With the game's list the question comes only when two LISTED recipes of that name sit in professions the
    character holds (Radiant Gloves: Blacksmithing and Tailoring); a recipe one profession makes goes there."""
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "blacksmithing", "skill": 300}, {"id": "tailoring", "skill": 300}, {"id": "cooking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(professions.NeedsProfession, match="Which profession makes Radiant Gloves — Blacksmithing or Tailoring?") as e:
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="radiant gloves"))
    assert (e.value.character, e.value.recipe, e.value.candidates) == (LABEL, "Radiant Gloves", ["blacksmithing", "tailoring"])
    rows, _ = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Radiant Gloves", text="tailoring"))
    assert rows[0]["professions"][1]["recipes"] == [{"id": 55533, "name": "Radiant Gloves"}]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Savory Deviate Delight"))
    assert rows[0]["professions"][2]["recipes"] == [{"id": 4569, "name": "Savory Deviate Delight"}] and line == f"{LABEL} · Cooking recipes: + Savory Deviate Delight"


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
    assert rows[0]["professions"][0]["recipes"] == [{"id": 10789, "name": "Devilsaur Gauntlets"}] and line == f"{LABEL} · Leatherworking recipes: + Devilsaur Gauntlets"


def test_a_recipe_of_a_profession_not_on_record_records_it_too(reg):
    """The game's list names the recipe's profession: one the character could still take is recorded with it (shown
    before Apply); a hint naming a profession on record that doesn't make it is refused."""
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "cooking", "skill": 100}]}], "t", officer=True)
    m = reg.members[UID]
    for text in (None, "tailoring", "alchemy"):  # a guess that doesn't make it is only a guess: the list decides
        rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Mooncloth Bag", text=text))
        assert line == f"{LABEL} · Tailoring: added with Mooncloth Bag", text
        assert rows[0]["professions"][2] == {"id": "tailoring", "skill": None, "recipes": [{"id": 10431, "name": "Mooncloth Bag"}]}
    with pytest.raises(RegistryError, match=r"^Mooncloth Bag isn't a Cooking recipe\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Mooncloth Bag", text="cooking"))


def test_recipe_remove_finds_the_profession_that_holds_it(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [
        {"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]},
        {"id": "cooking", "skill": 300, "recipes": [{"name": "Savory Deviate Delight"}]}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="savory deviate delight"))
    assert rows[0]["professions"][1]["recipes"] == [] and rows[0]["professions"][0]["recipes"] == [{"name": "Devilsaur Gauntlets", "id": 10789}]
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


def test_a_name_the_list_lacks_is_refused_even_on_a_fisher(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "fishing", "skill": 150}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(RegistryError, match="^Big Fish isn't a recipe in the game's list\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Big Fish", text="fishing"))


def _real(reg, uid, name, cls="Hunter", spec="Survival", main=True):
    reg.add_character(uid, name, name, cls, spec, None, main)
    return name


def test_a_recipe_with_no_character_named_goes_to_the_one_crafter(reg):
    """"I can make Ironfeather Shoulders" without a character: the player matters more than the character (design §5.60)."""
    a = _real(reg, 9100, "Thornwick")
    b = _real(reg, 9100, "Thornalt", cls="Priest", spec="Holy", main=False)
    reg.save_character_table(9100, [{"label": b, "professions": [{"id": "leatherworking", "skill": 300}]}], "t", officer=True)
    m = reg.members[9100]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, field="recipe_add", value="Ironfeather Shoulders"))
    assert rows[0]["label"] == b and line == f"{b} · Leatherworking recipes: + Ironfeather Shoulders", "the alt is the only crafter"
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

def test_a_recipe_with_a_guess_on_a_bare_character_adds_the_profession_too(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "skinning", "skill": 200}]}], "t", officer=True)
    m = reg.members[UID]
    op = _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Breastplate", text="leatherworking")
    rows, line = configops._character_rows(reg, m, op)
    assert rows == [{"label": LABEL, "professions": [{"id": "skinning", "skill": 200, "recipes": []},
                                                     {"id": "leatherworking", "skill": None, "recipes": [{"id": 10760, "name": "Green Dragonscale Breastplate"}]}]}]
    assert line == f"{LABEL} · Leatherworking: added with Green Dragonscale Breastplate"
    said = configops.describe(reg, op, configops.PLAIN_WEB, by_id=UID)
    assert said.split("\n") == [f"{LABEL} has no Leatherworking on record yet.",
                                f"Apply records Leatherworking on {LABEL} and adds Green Dragonscale Breastplate to the recipes {LABEL} makes.",
                                f'Say the skill when you know it, like "{LABEL} is 300 Leatherworking".']
    reg.save_character_table(UID, rows, "t", officer=True)
    c = next(c for c in reg.members[UID].active() if c.label == LABEL)
    assert reg.professions_summary(c) == "Skinning 200 · Leatherworking"


def test_a_recipe_without_a_guess_on_a_bare_character_asks_which_profession(reg):
    """With the game's list the question offers only the professions whose list holds the recipe (Radiant Gloves is a
    Blacksmithing and a Tailoring recipe); a name no profession makes is refused, not asked."""
    m = reg.members[UID]
    with pytest.raises(professions.NeedsProfession, match=r"^Which profession makes Radiant Gloves\?$") as e:
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Radiant Gloves"))
    assert (e.value.character, e.value.recipe, e.value.candidates) == (LABEL, "Radiant Gloves", ["blacksmithing", "tailoring"])
    with pytest.raises(professions.NeedsProfession):
        configops.describe(reg, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Radiant Gloves"), strict=True)
    assert configops.describe(reg, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Radiant Gloves")) == \
        "Which profession makes Radiant Gloves?", "the web and MCP print the question alone"
    with pytest.raises(RegistryError, match=r"^Green Dragonscale Cloak isn't a recipe in the game's list\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Green Dragonscale Cloak"))


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
    assert configops.describe(reg, prof, by_id=UID) == "Your Leatherworking is recorded at 275 with 1 recipe."
    drop = _op(member=m.display_name, character=LABEL, field="profession_remove", value="leatherworking")
    assert configops.describe(reg, drop, by_id=OWNER) == f"{m.display_name}'s Leatherworking is removed from their record."


def test_only_professions_the_character_can_hold_are_offered(reg):
    """Two gathering trades taken: only Cooking or First Aid can still hold a recipe. Spiced Wolf Meat is a Cooking
    recipe and Cooking fits, so there is nothing to ask (one candidate is never a question); a guess that doesn't make
    it is ignored, and a recipe no profession that fits makes is refused."""
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "herbalism", "skill": 200}, {"id": "mining", "skill": 200}]}], "t", officer=True)
    m = reg.members[UID]
    for text in (None, "leatherworking", "cooking"):
        rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Spiced Wolf Meat", text=text))
        assert line == f"{LABEL} · Cooking: added with Spiced Wolf Meat", text
    with pytest.raises(RegistryError, match="^A character has two trade professions — drop one first\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Radiant Gloves"))


def test_a_single_candidate_is_never_a_question_without_a_list_either(reg, monkeypatch):
    """_needs with one profession that fits records it (shown before Apply) instead of asking (task 2 carry)."""
    import dataclasses
    monkeypatch.setattr(professions, "_fitting", lambda *a, **k: ["cooking"])
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "herbalism", "skill": 200}, {"id": "mining", "skill": 200}]}], "t", officer=True)
    reg.profile = dataclasses.replace(reg.profile, recipes={})
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Spiced Wolf Meat"))
    assert line == f"{LABEL} · Cooking: added with Spiced Wolf Meat"


def test_plain_text_recipe_resolves_against_the_list(reg):
    """'I can make ironfeather shoulders': the name (in any case, or part of it) is matched against the game's list and
    the character's professions; the row carries the list's id and spelling."""
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 265}]}], "t", officer=True)
    m = reg.members[UID]
    rid = reg.profile.recipes_named("ironfeather shoulders")[0].id
    for value in ("ironfeather shoulders", "IRONFEATHER  Shoulders", "ironf shoulders"):
        rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value=value))
        assert rows[0]["professions"][0]["recipes"] == [{"id": rid, "name": "Ironfeather Shoulders"}], value
        assert line == f"{LABEL} · Leatherworking recipes: + Ironfeather Shoulders"
    # R6: a name a profession holds twice resolves to the lowest id when nobody picks
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "tailoring", "skill": 300}]}], "t", officer=True)
    rows, _ = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Bottomless Bag"))
    assert rows[0]["professions"][0]["recipes"] == [{"id": 10441, "name": "Bottomless Bag"}]
    with pytest.raises(RegistryError, match=r"^Thunderfury isn't a recipe in the game's list\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Thunderfury"))
    # part of a name many professions' recipes share: the one the character's own profession makes
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "mining", "skill": 300}]}], "t", officer=True)
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="dark iron"))
    assert line == f"{LABEL} · Mining recipes: + Smelt Dark Iron"


def test_plain_text_ambiguous_name_lists_candidates(reg):
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(RegistryError, match=r"^Did you mean Devilsaur Gauntlets or Devilsaur Leggings\?$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="devilsaur"))
    with pytest.raises(RegistryError) as e:
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="ironfeather"))
    said = str(e.value)
    names = said.removeprefix("Did you mean ").removesuffix("?").replace(" or ", ", ").split(", ")
    assert said.startswith("Did you mean ") and 2 <= len(names) <= 5 and all(n.startswith("Ironfeather ") for n in names), said


def test_plain_text_recipe_remove_resolves_against_the_characters_recipes(reg):
    """'I can't make ironfeather any more': part of a name, matched against what THIS character holds (listed by id, an
    unmatched one by its typed key) through the same three tiers."""
    from oibot_gm.registry import Profession, Recipe

    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300, "recipes": [
        {"name": "Ironfeather Shoulders"}, {"name": "Devilsaur Gauntlets"}, {"name": "Devilsaur Leggings"}]}]}], "t", officer=True)
    m = reg.members[UID]
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="ironfeather"))
    assert line == f"{LABEL} · Leatherworking recipes: − Ironfeather Shoulders"
    assert [r["name"] for r in rows[0]["professions"][0]["recipes"]] == ["Devilsaur Gauntlets", "Devilsaur Leggings"]
    with pytest.raises(RegistryError, match=r"^Did you mean Devilsaur Gauntlets or Devilsaur Leggings\?$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="devilsaur"))
    with pytest.raises(RegistryError, match=f"^{LABEL} has no Mooncloth Bag on record\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="Mooncloth Bag"))
    # an unmatched typed recipe is found by its words too
    c = next(x for x in m.active() if x.label == LABEL)
    c.professions = [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Bold Gloves"), Recipe(name="Devilsaur Gauntlets", id=10789)])]
    reg.save(m, "typed")
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_remove", value="bold"))
    assert line == f"{LABEL} · Leatherworking recipes: − Bold Gloves" and rows[0]["professions"][0]["recipes"] == [{"name": "Devilsaur Gauntlets", "id": 10789}]


def test_plain_text_recipes_without_a_list_are_unchanged(reg):
    """A version without the game's list keeps typed recipes: the name as written, the question when two professions
    on record make things, a profession that makes nothing refused."""
    import dataclasses
    reg.profile = dataclasses.replace(reg.profile, recipes={})
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "cooking", "skill": 300}]}], "t", officer=True)
    m = reg.members[UID]
    with pytest.raises(professions.NeedsProfession, match="Which profession makes Savory Deviate Delight — Leatherworking or Cooking?"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Savory Deviate Delight"))
    rows, line = configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Witherbark Boots", text="leatherworking"))
    assert rows[0]["professions"][0]["recipes"] == [{"name": "Witherbark Boots", "id": None}] and line == f"{LABEL} · Leatherworking recipes: + Witherbark Boots"
    with pytest.raises(RegistryError, match=f"^{LABEL} has no Tailoring on record\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Mooncloth Bag", text="tailoring"))
    reg.save_character_table(UID, [{"label": LABEL, "professions": [{"id": "leatherworking", "skill": 300}, {"id": "fishing", "skill": 150}]}], "t", officer=True)
    with pytest.raises(RegistryError, match="^Fishing has no recipes\\.$"):
        configops._character_rows(reg, m, _op(member=m.display_name, character=LABEL, field="recipe_add", value="Big Fish", text="fishing"))


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

