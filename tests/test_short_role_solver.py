"""The short-role preference at the solver (slow: CP-SAT): when tanks or healers are short across a run's rosters,
everyone who joined for the role is rostered before a seat of it is left empty — as a strong preference, so a hybrid who
could fill either short role, or a board that already fills every seat, never makes the run unsolvable."""
from pathlib import Path

import pytest

from oibot_gm.models import Player
from oibot_gm.profiles import GameProfile
from oibot_gm.roster.solver import SolveOptions, solve_rosters

pytestmark = pytest.mark.slow
FOREVER = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "forever")
OPTS = dict(raid_size=10, role_min={"tank": 2, "healer": 2}, role_max={"tank": 2, "healer": 3})


def pool(shape):
    """[(class, spec, role, offspec, how many)] → signed players p00, p01, …"""
    out = []
    for cls, spec, role, off, n in shape:
        for _ in range(n):
            i = len(out)
            out.append(Player(signup_name=f"p{i:02d}", pos=i, status="signed", cls=cls, spec=spec, role=role, offspec=off))
    return out


@pytest.mark.parametrize("hybrid", [("Druid", "Guardian", "tank", "Restoration"), ("Druid", "Restoration", "healer", "Guardian")])
def test_a_hybrid_for_two_short_roles_fills_one_and_the_run_solves(hybrid):
    """Two 10s, tanks and healers both short (two tanks, two priests and one druid who can tank or heal): a roster is
    found, every tank and healer who joined is seated, and the druid takes one of the two roles."""
    ps = pool([("Warrior", "Protection", "tank", None, 2), (*hybrid, 1), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 17)])
    rosters = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(**OPTS), rosters=2)
    seated = {p.signup_name: p for r in rosters for p in r.selected}
    assert {"p00", "p01", "p03", "p04"} <= set(seated), "every tank and healer who joined is rostered"
    assert "p02" in seated and seated["p02"].role in ("tank", "healer"), "the druid fills one of the short roles"


def test_a_board_that_fills_every_seat_still_solves_with_a_role_short():
    """Officers placed 20 people (two tanks among them) and a third tank joined: the short-role preference can't seat
    her, and the run still solves with the officers' 20."""
    ps = pool([("Warrior", "Protection", "tank", None, 3), ("Priest", "Holy", "healer", None, 4), ("Mage", "Fire", "ranged", None, 16)])
    board = ["p00", "p01"] + [p.signup_name for p in ps[3:7]] + [p.signup_name for p in ps[7:21]]
    rosters = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(**OPTS, force_in=tuple(board)), rosters=2)
    seated = {p.signup_name for r in rosters for p in r.selected}
    assert seated == set(board) and "p02" not in seated
