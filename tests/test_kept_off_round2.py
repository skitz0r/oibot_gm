"""Round 2 after the kept-off solver fix: a name kept off the roster (an "out" pin, a drag onto Not placed, the comp
policy's always_bench) is never asked by the planner, never counted as rostered by the lock box, never ranked "closest
to a seat"; a lock with everyone who joined kept off is refused (never an empty roster); "another one" with nothing
left says so; a conflict's names are escaped on the Discord lines."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import join, lock_with_board
from test_raid_night import NightBot

from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError
from oibot_gm.roster import solver

BD = "barrow_deeps"


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def run_of(reg, rs, days: float = 3):
    start = (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)
    return rc.open_run(reg, rs, BD, start, by="t", schedule=rc.ADHOC_SCHEDULE)


def asked_ids(plan) -> set[int]:
    return {a.discord_id for st in plan.seats for opt in st.options for a in opt}


# ---------------------------------------------------------------- 3. the planner never asks a kept-off name

def test_after_the_lock_automatic_asks_never_ask_a_name_kept_off(reg, rs):
    ev = run_of(reg, rs)
    ms = reg.test_members()[:8]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:5]])  # 5 of 10 rostered, 3 joiners left on the bench
    off, other = ms[5], ms[6]
    ev.pins[str(off.discord_id)] = "out"  # dragged onto Not placed before the lock
    rs.save(ev, "kept off")
    rc.run_team(reg, ev)["autofill"] = True
    plan = rc.role_plan(reg, rs, ev)
    who = asked_ids(plan)
    assert off.discord_id not in who and other.discord_id in who, "the other benched joiners are asked, the kept-off one never"
    sent, _ = rc.send_batch(plan)
    assert off.discord_id not in {a.discord_id for a in sent}
    assert rc.fill_state_text(reg, ev) == "5 seats are open. Automated Fill Requests will ask for them."


def test_before_the_lock_a_kept_off_joiner_is_neither_counted_nor_suggested(reg, rs):
    ev = run_of(reg, rs)
    tanks = [m for m in reg.test_members() if reg.profile.spec(m.main.cls, m.main.spec).role == "tank"]
    join(reg, rs, ev, tanks[:1])
    with_one = rc.role_plan(reg, rs, ev)
    ev.pins[str(tanks[0].discord_id)] = "out"
    rs.save(ev, "kept off")
    without = rc.role_plan(reg, rs, ev)
    assert without.have["tank"] == with_one.have["tank"] - 1, "kept off: not coming as far as the lock is concerned"
    assert tanks[0].discord_id not in asked_ids(without)


def test_the_comp_policys_always_bench_is_never_asked(reg, rs, monkeypatch):
    ev = run_of(reg, rs)
    ms = reg.test_members()[:8]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:5]])
    from oibot_gm import policy

    real = policy.PolicyStore.comp_constraints
    monkeypatch.setattr(policy.PolicyStore, "comp_constraints",
                        lambda self: {**real(self), "always_bench": [ms[6].main.label]})
    assert ms[6].discord_id in rc.kept_off_ids(reg, rs, ev)
    assert ms[6].discord_id not in asked_ids(rc.role_plan(reg, rs, ev))


# ---------------------------------------------------------------- 5. everyone who joined kept off

def test_a_lock_with_everyone_who_joined_kept_off_is_refused(reg, rs):
    ev = run_of(reg, rs)
    ms = reg.test_members()[:4]
    join(reg, rs, ev, ms)
    for m in ms:
        ev.pins[str(m.discord_id)] = "out"
    rs.save(ev, "everyone kept off")
    bot = NightBot(reg, rs)
    line = asyncio.run(bot.lock_run(reg, rs, ev, by="scheduler"))
    assert ev.state == "open" and not ev.rosters and ev.lock_error == rc.SOLVER_WORDS["all_off"], line
    with pytest.raises(RegistryError, match="kept off the roster"):
        asyncio.run(bot.lock_preview(reg, rs, ev, by="Jon"))
    pv = rc.event_board_preview(reg, rs, ev)
    assert pv[ev.key] == rc.SOLVER_WORDS["all_off"], "the board says it too"


def test_a_run_nobody_joined_still_locks_as_before(reg, rs):
    """Today's behaviour, kept: nobody joined (no answer to keep off) → the lock locks an empty roster; the lock box
    already says nobody is asked to confirm."""
    ev = run_of(reg, rs)
    asyncio.run(NightBot(reg, rs).lock_run(reg, rs, ev, by="scheduler"))
    assert ev.state == "locked" and sum(len(r.selected) for r in ev.all_rosters) == 0


# ---------------------------------------------------------------- 6. "another one", closest to a seat, escaping

def test_another_split_with_nothing_left_says_so(reg, rs, monkeypatch):
    ev = run_of(reg, rs)
    join(reg, rs, ev, reg.test_members()[:6])
    real = rc._solve_run

    def solve(reg_, rs_, ev_, *a, **k):
        if k.get("avoid") or k.get("avoid_groups"):
            raise RuntimeError("no roster found: INFEASIBLE")
        return real(reg_, rs_, ev_, *a, **k)

    monkeypatch.setattr(rc, "_solve_run", solve)
    with pytest.raises(RuntimeError) as e:
        rc.split_preview(reg, rs, ev, "balanced", avoid=[[[s.display_name for s in ev.by_status("in")]]], time_limit=1.0)
    assert rc.solver_error_text(e.value) == rc.SOLVER_WORDS["no_other"]
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no roster found: INFEASIBLE")))
    with pytest.raises(RuntimeError) as e:
        rc.split_preview(reg, rs, ev, "balanced", avoid=[[["x"]]], time_limit=1.0)
    assert rc.solver_error_text(e.value) == rc.SOLVER_WORDS["infeasible"], "the board itself has no roster: that sentence"


def test_closest_to_a_seat_never_ranks_a_name_kept_off(reg, profile):
    from oibot_gm.models import Player, RosterResult
    from oibot_gm.roster import explain

    ps = [Player(signup_name=f"p{i}", pos=i, status="signed", cls="Mage", spec="Fire", role="ranged") for i in range(12)]
    r = RosterResult(selected=ps[:10], benched=ps[10:], groups=[], group_reports=[], objective=0, synergy_value=0,
                     role_counts={}, advisories=[])
    opts = solver.SolveOptions(raid_size=10, force_out=("p10",), kept_off=("p10",), time_limit_s=1.0)
    out = explain.annotate(profile, ps, BD, r, whatif=True, opts=opts)
    assert "p10" not in out.bench_whatif and "p11" in out.bench_whatif


def test_a_conflicts_names_are_escaped_on_the_discord_lines(reg, rs, monkeypatch):
    ev = run_of(reg, rs)
    join(reg, rs, ev, reg.test_members()[:4])

    def propose(*a, **k):
        raise solver.Conflict(solver.GROUP_FULL, names=("Ma*rk", "@everyone"), group=1, have=6, seats=5)

    monkeypatch.setattr(rc, "propose", propose)
    bot = NightBot(reg, rs)
    asyncio.run(bot.lock_run(reg, rs, ev, by="J_o_n"))
    assert "Ma*rk" in ev.lock_error, "stored as it is (the site escapes on its own)"
    line = bot.updates[-1]
    assert "Ma\\*rk" in line and "J\\_o\\_n" in line and "@everyone" not in line.replace("@​everyone", ""), line
