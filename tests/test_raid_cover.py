"""The solver seats raid-wide cast buffs: a pool whose party-buff classes could fill every seat still keeps a priest
(Fortitude, Divine Spirit), a mage (Intellect) and a druid (Mark), and each buff's value is the raid's, not a group's."""
from pathlib import Path

import pytest

from oibot_gm.models import Player
from oibot_gm.profiles import GameProfile
from oibot_gm.roster.solver import solve

pytestmark = pytest.mark.slow
FOREVER = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "forever")


def pool(rows):
    return [Player(signup_name=f"p{i:02d}", pos=i, status="signed", cls=c, spec=s, role=FOREVER.spec(c, s).role, character=f"p{i:02d}")
            for i, (c, s) in enumerate(rows)]


def test_raid_buff_providers_keep_a_seat():
    rows = ([("Warrior", "Protection")] * 2 + [("Warrior", "Fury")] * 4 + [("Rogue", "Combat")] * 3 + [("Hunter", "Marksmanship")] * 2
            + [("Druid", "Feral")] + [("Druid", "Balance")] * 2 + [("Druid", "Restoration")] + [("Shaman", "Enhancement")] + [("Shaman", "Restoration")] * 2
            + [("Paladin", "Retribution")] + [("Paladin", "Holy")] * 2 + [("Priest", "Holy")] * 2 + [("Priest", "Shadow")] + [("Mage", "Frost")] * 3
            + [("Warlock", "Destruction")] * 2 + [("Mage", "Fire")])
    raid = next(rid for rid, r in FOREVER.raids.items() if r.get("size") == 20)
    r = solve(FOREVER, pool(rows), raid)
    seated = {p.cls for p in r.selected}
    assert len(r.selected) == 20
    assert {"Priest", "Mage", "Druid", "Paladin"} <= seated  # Fortitude/Spirit, Intellect, Mark, a Blessing


def test_crit_auras_are_one_family():
    fam = {b.id: b.family_id for b in FOREVER.buffs}
    assert fam["leader_of_the_pack"] == fam["moonkin_aura"]
    assert not {"sanctity_aura", "tranquil_air"} & set(fam)
