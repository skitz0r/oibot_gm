"""A name kept off the roster (force_out: an "out" pin, an always_bench line, a drag onto Not placed) never asks for a
seat: the seats are filled from the players the solve MAY seat. Before this, a raid that wasn't over-full became
INFEASIBLE with one name kept off — the board picture failed and so did the real lock. Small models (fast CP-SAT)."""
from pathlib import Path

import pytest

from oibot_gm import raidcycle as rc
from oibot_gm.models import Player
from oibot_gm.profiles import GameProfile
from oibot_gm.roster import solver
from oibot_gm.roster.solver import SolveOptions, solve_rosters

FOREVER = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "forever")


def pool(n_dps: int, n_heal: int = 0, n_tank: int = 0):
    out = []
    for role, cls, spec, n in (("tank", "Warrior", "Protection", n_tank), ("healer", "Priest", "Holy", n_heal), ("ranged", "Mage", "Fire", n_dps)):
        for _ in range(n):
            i = len(out)
            out.append(Player(signup_name=f"p{i}", pos=i, status="signed", cls=cls, spec=spec, role=role))
    return out


def seated(rosters):
    return {p.signup_name for r in rosters for p in r.selected}


def solve(ps, rosters=1, **kw):
    return solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, time_limit_s=2.0, **kw), rosters=rosters)


def test_an_under_filled_raid_with_one_kept_off_solves_and_leaves_them_off():
    ps = pool(3, 2)  # the reviewer's case: 5 joiners for 10 seats
    assert seated(solve(ps)) == {p.signup_name for p in ps}
    got = seated(solve(ps, force_out=("p1",)))
    assert got == {p.signup_name for p in ps} - {"p1"}, "solved (it was INFEASIBLE), and p1 stays off"


def test_an_exactly_filled_raid_with_one_kept_off_seats_the_other_nine():
    ps = pool(8, 2)
    got = seated(solve(ps, force_out=("p4",)))
    assert len(got) == 9 and "p4" not in got


def test_several_rosters_fill_from_the_players_they_may_seat():
    ps = pool(17, 3, 1)  # 21 joiners, two 10s, one kept off: 20 may be seated, so both rosters are full
    rs_ = solve(ps, rosters=2, force_out=("p0",))
    assert [len(r.selected) for r in rs_] == [10, 10] and "p0" not in seated(rs_)
    ps = pool(9, 3)  # 12 joiners, two 10s, two kept off: the 10 left are seated, neither roster has to be full
    rs_ = solve(ps, rosters=2, force_out=("p0", "p1"))
    assert len(seated(rs_)) == 10 and not {"p0", "p1"} & seated(rs_)


def test_every_joiner_kept_off_solves_to_an_empty_roster():
    ps = pool(4)
    assert seated(solve(ps, force_out=tuple(p.signup_name for p in ps))) == set()


@pytest.mark.parametrize("opts, reason, words", [
    (dict(force_in=tuple(f"p{i}" for i in range(11))), solver.KEPT_ON, "11 people are kept on the roster, and it has 10 seats."),
    (dict(force_in=("p0",), force_out=("p0",)), solver.KEPT_BOTH, "p0 is kept on the roster by a pin or the comp policy"),
    (dict(pins={f"p{i}": 0 for i in range(6)}), solver.GROUP_FULL, "Group 1 on the board holds 6 people and has room for 5."),
    (None, solver.GROUP_ROLES, "Group 1 on the board holds more healers than one group may have: 3."),
])
def test_conflicts_the_board_can_make_are_named_before_searching(opts, reason, words):
    ps = pool(8, 4) if reason == solver.GROUP_ROLES else pool(12)
    if reason == solver.GROUP_ROLES:  # four healers pinned into one group (three may share one)
        opts = dict(pins={p.signup_name: 0 for p in ps if p.role == "healer"})
    with pytest.raises(solver.Conflict) as e:
        solve(ps, **opts)
    assert e.value.reason == reason and reason in str(e.value)
    said = rc.solver_error_text(e.value)
    assert said.startswith(words) and "minimum" not in said, said
    if reason in (solver.GROUP_FULL, solver.GROUP_ROLES):
        assert all(n in said for n in e.value.names), "the people are named"
    assert rc.solver_error_text(RuntimeError(said)) == said, "a stored lock_error read again is itself"


def test_a_real_timeout_says_time_never_infeasible():
    """The solver's own string on a timeout is "no roster found: UNKNOWN" (no solution inside the limit): the time
    sentence, never "no roster fits"."""
    assert rc.solver_error_text(RuntimeError("no roster found: UNKNOWN")) == rc.SOLVER_WORDS["time"]
    ps = pool(30, 6, 4)
    with pytest.raises(RuntimeError) as e:  # a budget too small to find anything: the solver's real exception text
        solve_rosters(FOREVER, ps, "hyjal_summit_forever", SolveOptions(raid_size=20, time_limit_s=1e-6), rosters=1)
    assert str(e.value) == "no roster found: UNKNOWN" and rc.solver_error_text(e.value) == rc.SOLVER_WORDS["time"]


def test_the_failure_sentences_are_true_and_whole():
    assert rc.solver_error_text(RuntimeError("no roster found: INFEASIBLE")) == rc.SOLVER_WORDS["infeasible"]
    assert rc.solver_error_text(KeyError("p9")) == rc.SOLVER_WORDS["other"]
    assert rc.solver_error_text(RuntimeError(f"no roster found: {rc.ALL_KEPT_OFF}")) == rc.SOLVER_WORDS["all_off"]
    assert rc.solver_error_text(RuntimeError(rc.SOLVER_WORDS["changing"])) == rc.SOLVER_WORDS["changing"], "web/api.py re-reads lock_error"
    old = "the answers or pins kept changing while the roster was being built; lock again in a moment"
    assert rc.solver_error_text(RuntimeError(old)) == old, "a lock_error stored before shows as it was"
    for s in rc.SOLVER_WORDS.values():
        assert "minimum" not in s and s[0].isupper() and s.endswith(".") and 1 <= s.count(". ") + 1 <= 2
