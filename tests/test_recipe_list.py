"""The game's recipe list as profile data (armory link, phase 1 — spec §4.1): the importer over recorded static
responses (no network), profiles/forever/recipes.yaml and the profile loader."""
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from oibot_gm.importers.blizzard import Blizzard, recipe_row, recipe_rows
from oibot_gm.profiles import GameProfile, norm_recipe

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tests" / "fixtures" / "armory" / "static"


def recorded(url: str) -> dict:
    """The injected fetch: a recorded, trimmed response per API path; a path not recorded fails the test."""
    path = urlparse(url).path.strip("/").replace("/", "_")
    f = STATIC / f"{path}.json"
    if not f.exists():
        raise AssertionError(f"not recorded: {urlparse(url).path}")
    return json.loads(f.read_text())


def client() -> Blizzard:
    return Blizzard(namespace="static-forever-us", fetch=recorded)


def test_recipe_rows_drop_the_test_profession():
    # the recorded index holds Enchanting, Leatherworking and "Test Profession [DNT]"; only Enchanting is the profile's
    rows, build = recipe_rows(client(), {333: "enchanting"})
    assert build == "1.60.1_70399"
    assert {r["profession"] for r in rows} == {"enchanting"}
    assert [r["id"] for r in rows] == [4289, 4293, 4291]  # by profession, category, name
    assert [r["category"] for r in rows] == ["Bracer Enchants", "Bracer Enchants", "Runed Enchanting Rods"]


def test_recipe_rows_drop_the_test_profession_even_when_mapped():
    rows, _ = recipe_rows(client(), {333: "enchanting", 2933: "enchanting"})
    assert len(rows) == 3


def test_recipe_rows_carry_makes_and_reagents():
    rows, _ = recipe_rows(client(), {333: "enchanting"})
    rod = next(r for r in rows if r["id"] == 4291)
    assert rod["name"] == "Runed Copper Rod"
    assert rod["makes"] == {"id": 6218, "name": "Runed Copper Rod", "quantity": 1}
    assert rod["reagents"] == [{"id": 6217, "name": "Copper Rod", "quantity": 1}, {"id": 247786, "name": "Mote of Magic", "quantity": 1}]


def test_a_recipe_without_a_crafted_item_keeps_makes_none():
    rows, _ = recipe_rows(client(), {333: "enchanting"})
    enchant = next(r for r in rows if r["id"] == 4289)
    assert enchant["name"] == "Enchant Bracer - Inferior Stamina"
    assert enchant["makes"] is None
    assert enchant["reagents"] == [{"id": 247786, "name": "Mote of Magic", "quantity": 1}]


def test_a_ranged_quantity_keeps_its_minimum_and_maximum():
    # recipe 2438 (Small Bronze Bomb) makes 1–4: crafted_quantity {minimum, maximum}, no value
    row = recipe_row(json.loads((STATIC / "data_wow_recipe_2438.json").read_text()), "engineering", "Explosives")
    assert row["makes"] == {"id": 4374, "name": "Small Bronze Bomb", "quantity": 1, "quantity_max": 4}


def test_a_fixed_quantity_has_no_maximum():
    row = recipe_row(json.loads((STATIC / "data_wow_recipe_4291.json").read_text()), "enchanting", "Runed Enchanting Rods")
    assert "quantity_max" not in row["makes"]
    flat = dict(json.loads((STATIC / "data_wow_recipe_2438.json").read_text()), crafted_quantity={"minimum": 2.0, "maximum": 2.0})
    assert recipe_row(flat, "engineering", "Explosives")["makes"]["quantity"] == 2
    assert "quantity_max" not in recipe_row(flat, "engineering", "Explosives")["makes"]


@pytest.mark.parametrize("cq", [None, {}, {"minimum": 1.0}, {"value": 1.5}, {"minimum": 1.0, "maximum": 4.5}, {"minimum": 3.0, "maximum": 2.0}, {"value": 0}])
def test_a_bad_crafted_quantity_is_refused(cq):
    d = json.loads((STATIC / "data_wow_recipe_2438.json").read_text())
    if cq is None:
        d.pop("crafted_quantity")
    else:
        d["crafted_quantity"] = cq
    with pytest.raises(ValueError, match="/data/wow/recipe/2438"):
        recipe_row(d, "engineering", "Explosives")


def test_a_fractional_reagent_quantity_is_refused():
    d = json.loads((STATIC / "data_wow_recipe_4291.json").read_text())
    d["reagents"][0]["quantity"] = 1.5
    with pytest.raises(ValueError, match="quantity"):
        recipe_row(d, "enchanting", "Runed Enchanting Rods")


def test_an_unexpected_shape_is_refused():
    def broken(url: str) -> dict:
        d = recorded(url)
        if "/skill-tier/" in url:
            d = {k: v for k, v in d.items() if k != "categories"}
        return d

    with pytest.raises(ValueError, match="skill-tier"):
        recipe_rows(Blizzard(namespace="static-forever-us", fetch=broken), {333: "enchanting"})


def test_profile_loads_the_list():
    p = GameProfile.load(ROOT / "profiles" / "forever")
    assert p.has_recipe_list
    hits = p.recipes_named(norm_recipe("Ironfeather Shoulders"))
    assert len(hits) == 1 and hits[0].profession == "leatherworking"
    assert hits[0].key == "ironfeather shoulders" and hits[0].makes and hits[0].reagents
    lw = p.recipes_of("leatherworking")
    assert lw == sorted(lw, key=lambda r: (r.category, r.name))
    assert p.profession("leatherworking").blizzard_id == 165
    assert p.armory == {"region": "us", "profile_namespace": "profile-forever-{region}", "static_namespace": "static-forever-{region}"}


def test_tbc_has_no_list():
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    assert not p.has_recipe_list
    assert p.recipes == {} and p.recipes_of("leatherworking") == [] and p.recipes_named("ironfeather shoulders") == []
    assert p.armory is None


def test_same_name_in_two_professions_returns_both(tmp_path: Path):
    root = tmp_path / "forever"
    shutil.copytree(ROOT / "profiles" / "forever", root)
    rows = [
        {"id": 1, "name": "Shared Thing", "profession": "tailoring", "category": "Bags", "makes": None, "reagents": []},
        {"id": 2, "name": "Shared Thing", "profession": "leatherworking", "category": "Bags", "makes": None, "reagents": []},
        {"id": 3, "name": "Other Thing", "profession": "tailoring", "category": "Bags", "makes": None, "reagents": []},
    ]
    (root / "recipes.yaml").write_text(yaml.safe_dump({"recipes": rows}))
    p = GameProfile.load(root)
    assert sorted(r.profession for r in p.recipes_named("shared thing")) == ["leatherworking", "tailoring"]
    assert [r.id for r in p.recipes_of("tailoring")] == [3, 1]


def test_the_committed_list_is_whole():
    p = GameProfile.load(ROOT / "profiles" / "forever")
    doc = yaml.safe_load((ROOT / "profiles" / "forever" / "recipes.yaml").read_text())
    ids = [r["id"] for r in doc["recipes"]]
    assert len(ids) == len(set(ids)) == len(p.recipes)
    for r in p.recipes.values():
        assert r.id and r.name and r.category
        assert r.profession in p.professions
    assert {d.id for d in p.professions.values() if d.blizzard_id is None} == set()
