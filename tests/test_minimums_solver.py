"""One minimum per role or class at the solver (slow: CP-SAT; design decision D16): every minimum is a preference
filled first and never a constraint, a role has no upper limit, and the seats past the minimums go to damage dealers —
an extra tank or healer sits only when nobody else joined or officers placed one."""
import pytest

from oibot_gm.roster.solver import SolveOptions, solve_rosters
from test_short_role_solver import FOREVER, pool

pytestmark = pytest.mark.slow


def test_one_hybrid_for_two_minimums_on_one_roster_still_solves():
    ps = pool([("Warrior", "Protection", "tank", None, 1), ("Druid", "Guardian", "tank", "Restoration", 1), ("Priest", "Holy", "healer", None, 1), ("Mage", "Fire", "ranged", None, 9)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}))[0]
    assert {"p00", "p01", "p02"} <= {p.signup_name for p in r.selected}


def test_two_hybrids_on_a_two_roster_run_find_rosters():
    """The gap the old hard floor left: two Guardian / Restoration druids beside two warriors and two priests counted
    each druid for both roles and asked two 10s for two tanks and two healers each from six people — no roster. Now
    the rosters are found and all six are rostered."""
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Druid", "Guardian", "tank", "Restoration", 2), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 16)])
    rosters = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}), rosters=2)
    seated = {p.signup_name for r in rosters for p in r.selected}
    assert len(seated) == 20 and {"p00", "p01", "p02", "p03", "p04", "p05"} <= seated


def test_seats_past_the_minimums_go_to_damage_dealers():
    ps = pool([("Warrior", "Protection", "tank", None, 4), ("Priest", "Holy", "healer", None, 4), ("Mage", "Fire", "ranged", None, 6)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}))[0]
    assert r.role_counts == {"tank": 2, "healer": 2, "ranged": 6}


def test_an_extra_healer_sits_only_when_nobody_else_joined():
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Priest", "Holy", "healer", None, 4), ("Mage", "Fire", "ranged", None, 4)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}))[0]
    assert len(r.selected) == 10 and r.role_counts["healer"] == 4


def test_a_class_minimum_is_met_when_the_joiners_allow():
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Paladin", "Holy", "healer", None, 2), ("Priest", "Holy", "healer", None, 2),
               ("Paladin", "Retribution", "melee", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2, "Paladin": 3}))[0]
    assert sum(1 for p in r.selected if p.cls == "Paladin") >= 3


def test_a_class_minimum_the_joiners_cannot_meet_never_stops_the_lock():
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2, "Paladin": 4}))[0]
    assert len(r.selected) == 10


def test_a_hybrid_switched_to_tank_does_not_also_count_for_their_healing_spec():
    """A Restoration druid who can tank fills one minimum, never two: switched to Guardian for the tank minimum, she
    doesn't count toward a Druid:Restoration minimum, so the other Restoration druid is rostered for it even as a
    third healer beside the two priests officers placed."""
    ps = pool([("Warrior", "Protection", "tank", None, 1), ("Druid", "Restoration", "healer", "Guardian", 1), ("Druid", "Restoration", "healer", None, 1),
               ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 9)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2, "Druid:Restoration": 1}, force_in=("p03", "p04")))[0]
    seated = {p.signup_name: p for p in r.selected}
    assert seated["p01"].role == "tank" and "p02" in seated


LEAD_BONUS = 1000  # raidcycle.LEAD_SEAT_BONUS


def _follow_on(shape, minimums):
    """Two 10s of a follow-on: the first 20 of `shape` are the lead's rostered (bonus + lead_rostered), then two mages
    who were not on the lead."""
    ps = pool([*shape, ("Mage", "Fire", "ranged", None, 2)])
    lead = tuple(p.signup_name for p in ps[:20])
    opts = SolveOptions(raid_size=10, minimums=minimums, bonus={n: LEAD_BONUS for n in lead}, lead_rostered=lead)
    rosters = solve_rosters(FOREVER, ps, "barrow_deeps", opts, rosters=2)
    return set(lead), {p.signup_name for r in rosters for p in r.selected}


@pytest.mark.parametrize("shape, minimums", [
    ([("Warrior", "Protection", "tank", None, 3), ("Priest", "Holy", "healer", None, 7), ("Mage", "Fire", "ranged", None, 10)], {"tank": 2, "healer": 3}),
    ([("Warrior", "Protection", "tank", None, 4), ("Priest", "Holy", "healer", None, 6), ("Mage", "Fire", "ranged", None, 10)], {"tank": 1, "healer": 3}),
    ([("Warrior", "Protection", "tank", None, 3), ("Priest", "Holy", "healer", None, 5), ("Mage", "Fire", "ranged", None, 12)], {"tank": 2, "healer": 3}),
], ids=["healers past the minimum", "tanks past the minimum", "the live shape"])
def test_a_follow_on_keeps_every_player_the_lead_rostered(shape, minimums):
    """A tank or healer the lead rostered is never charged as extra on its follow-on: the two 10s are the lead's 20."""
    lead, seated = _follow_on(shape, minimums)
    assert seated == lead


def test_officers_placing_an_extra_tank_is_kept():
    ps = pool([("Warrior", "Protection", "tank", None, 3), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}, force_in=("p00", "p01", "p02")))[0]
    assert {"p00", "p01", "p02"} <= {p.signup_name for p in r.selected}
