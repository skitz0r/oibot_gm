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
