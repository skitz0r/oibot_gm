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


def test_officers_placing_an_extra_tank_is_kept():
    ps = pool([("Warrior", "Protection", "tank", None, 3), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}, force_in=("p00", "p01", "p02")))[0]
    assert {"p00", "p01", "p02"} <= {p.signup_name for p in r.selected}
