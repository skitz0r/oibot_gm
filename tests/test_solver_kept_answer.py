"""One answer per model (the final fix wave of §5.67 phase 4). The deterministic budget makes a solve repeatable only
while it ends inside the wall-clock ceiling; on a loaded machine the ceiling stopped the Events board's picture and
the lock at different points and the later raid's rosters came out swapped. The process now keeps each model's answer:
a solve of a model answered before takes it without searching, so the picture, Preview lock and the lock agree on the
same input whatever the load. Small models (fast CP-SAT)."""
from pathlib import Path

import pytest
from ortools.sat.python import cp_model

from oibot_gm.models import Player
from oibot_gm.profiles import GameProfile
from oibot_gm.roster import solver
from oibot_gm.roster.solver import SolveOptions, solve_rosters

FOREVER = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "forever")


def pool(n: int) -> list[Player]:
    out = []
    for i in range(n):
        role, cls, spec = (("tank", "Warrior", "Protection") if i < 2 else ("healer", "Priest", "Holy") if i < 5
                           else ("ranged", "Mage", "Fire"))
        out.append(Player(signup_name=f"p{i}", pos=i, status="signed", cls=cls, spec=spec, role=role))
    return out


def shape(rosters):
    return [[sorted(g) for g in r.groups] for r in rosters]


def solve(**kw):
    return solve_rosters(FOREVER, pool(24), "barrow_deeps", SolveOptions(raid_size=10, time_limit_s=1.0, **kw), rosters=2)


def test_a_model_answered_before_takes_that_answer_without_searching(monkeypatch):
    first = solve()

    def no_search(self, model, *a, **k):
        raise AssertionError("searched again")

    monkeypatch.setattr(cp_model.CpSolver, "Solve", no_search)
    assert shape(solve()) == shape(first), "the same model, the same rosters, numbered the same"


def test_a_different_model_searches(monkeypatch):
    solve()
    searched = []
    real = cp_model.CpSolver.Solve

    def spy(self, model, *a, **k):
        searched.append(1)
        return real(self, model, *a, **k)

    monkeypatch.setattr(cp_model.CpSolver, "Solve", spy)
    got = solve(force_out=("p9",))
    assert searched and "p9" not in {p.signup_name for r in got for p in r.selected}


def test_a_stopped_handle_never_takes_a_kept_answer():
    solve()
    stop = solver.Stop()
    stop.stop()
    with pytest.raises(solver.Stopped):
        solve(stop=stop)


def test_nothing_is_kept_for_a_failed_solve():
    ps = pool(30)
    with pytest.raises(RuntimeError):
        solve_rosters(FOREVER, ps, "hyjal_summit_forever", SolveOptions(raid_size=20, time_limit_s=1e-6), rosters=1)
    assert not solver._answers


def test_a_solve_the_wall_clock_cut_is_never_kept(monkeypatch):
    """The re-review: a solve the hang guard cut short of its work budget depends on the load, so it is used once, logged
    and never kept — a later quiet solve searches again and gives the repeatable answer."""
    specs = [(c, s) for c, cd in FOREVER.classes.items() for s in list(cd)[:3]]
    ps = [Player(signup_name=f"p{i}", pos=i, status="signed", cls=c, spec=s, role=FOREVER.spec(c, s).role)
          for i, (c, s) in enumerate(specs[i % len(specs)] for i in range(40))]  # every class: a model not proved at once

    def solve():
        return solve_rosters(FOREVER, ps, "hyjal_summit_forever", SolveOptions(raid_size=20, time_limit_s=1.0), rosters=2)

    monkeypatch.setattr(solver, "DTIME_PER_SECOND", 1e6)  # a work budget no search reaches here …
    monkeypatch.setattr(solver, "WALL_CEILING", 3.0)  # … so the wall clock cuts it (3 s at a 1 s budget)
    hits = solver.ceiling_hits
    assert solve()[0].solver_status == "FEASIBLE"
    assert solver.ceiling_hits == hits + 1 and not solver._answers, "cut: logged, not kept"
    searched = []
    real = cp_model.CpSolver.Solve

    def spy(self, model, *a, **k):
        searched.append(1)
        return real(self, model, *a, **k)

    monkeypatch.setattr(cp_model.CpSolver, "Solve", spy)
    solve()
    assert searched, "the same model searches again"


def test_the_ceiling_is_a_hang_guard_far_past_the_budget():
    assert solver.WALL_CEILING >= 10, "120 s at the lock's 12 s budget: it changes no answer that ends on its work budget"
