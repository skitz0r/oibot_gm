"""Lock-time solving (slow: CP-SAT): how many rosters the joiners support and a split preview for one and two runs."""
import pytest

from conftest import join, open_test_run

from oibot_gm import raidcycle as rc

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _quick_previews(monkeypatch):
    monkeypatch.setattr(rc, "PREVIEW_TIME_LIMIT_S", 4.0)  # split_preview reads the module value at call time


def test_how_many_rosters(reg, rs):
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    rb = reg.role_bounds("barrow_deeps", 10)
    join(reg, rs, ev, reg.test_members()[:12])
    assert rc.how_many_rosters(reg, rc.players_for(reg, ev), 10, rb) == 1
    join(reg, rs, ev, reg.test_members())
    players = rc.players_for(reg, ev)
    assert len(players) == 24 and rc.how_many_rosters(reg, players, 10, rb) == 2
    assert rc.how_many_rosters(reg, players, 20, reg.role_bounds("barrow_deeps", 20)) == 1
    # bench answers don't count, and a run needs its tank minimum
    join(reg, rs, ev, [m for m in reg.test_members() if reg.profile.spec(m.main.cls, m.main.spec).role == "tank"][2:], "sub")
    assert rc.how_many_rosters(reg, rc.players_for(reg, ev), 10, rb) == 1


def test_split_preview_one_roster(reg, rs):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:12])
    layout, rosters = rc.split_preview(reg, rs, ev, "balanced")
    assert len(rosters) == 1 and len(rosters[0].selected) == 10 and len(rosters[0].benched) == 2
    assert len(layout) == 2 and sum(len(g) for g in layout) == 10
    counts = rosters[0].role_counts
    assert counts["tank"] == 2 and 2 <= counts["healer"] <= 3
    assert ev.rosters == [], "a preview never touches the sheet"


def test_split_preview_two_rosters(reg, rs):
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members())
    layout, rosters = rc.split_preview(reg, rs, ev, "balanced")
    assert [len(r.selected) for r in rosters] == [10, 10] and len(rosters[0].benched) == 4
    assert len(layout) == 4 and all(len(g) == 5 for g in layout)
    seated = [p.signup_name for r in rosters for p in r.selected]
    assert len(set(seated)) == 20, "nobody sits in two rosters"
    for r in rosters:
        assert r.role_counts["tank"] == 2 and 2 <= r.role_counts["healer"] <= 3
