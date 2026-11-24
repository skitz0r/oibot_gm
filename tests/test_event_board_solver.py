"""The Events board before the lock, solved for real (slow: CP-SAT). The board shows "where the lock would place them
now": for a non-trivial event — an officer's board place, a pin out, a split later raid — the picture the board cache
holds is exactly what lock_run then rosters (who, in which roster and group, in which role), on the first raid and on
the later raid of the chain; and the later raid's picture prefers the people the first raid's picture rosters."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import join
from test_raid_night import NightBot

from oibot_gm import event_board as eb
from oibot_gm import raidcycle as rc

pytestmark = pytest.mark.slow

HYJAL, BD = "hyjal_summit_forever", "barrow_deeps"


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def shape(rosters):
    return [([sorted(g) for g in r.groups], sorted((p.signup_name, p.role) for p in r.selected)) for r in rosters]


def evening(reg, rs):
    reg.seed_test_members(24, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, HYJAL, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD, "rosters": 2}])
    join(reg, rs, ev, reg.test_members())
    return ev, rc.event_parts(rs, ev)[1]


def settled(cache, reg, rs, ev):
    async def go():
        await cache.get(reg, rs, ev)
        while cache.tasks:
            await asyncio.gather(*list(cache.tasks.values()))
        return await cache.get(reg, rs, ev)
    return asyncio.run(go())


def test_the_board_is_what_the_lock_rosters_on_every_raid_of_the_event(reg, rs):
    ev, bd = evening(reg, rs)
    names = [s.display_name for s in ev.by_status("in")]
    ev.layout = [[], [names[5]]]  # an officer's hand placement: group 2 of the first raid
    ev.pins[next(k for k, s in ev.signups.items() if s.display_name == names[0])] = "out"  # an officer's pin out
    rs.save(ev, "a board place and a pin")
    bd = rs.events[bd.key]
    bd.layout = [[], [], [names[7]]]  # roster 2, group 1 of the later raid
    rs.save(bd, "a later raid's board place")
    bot = NightBot(reg, rs)
    st, previews = settled(eb.cache_of(bot), reg, rs, ev)
    assert st == "ready" and set(previews) == {ev.key, bd.key}
    first_pic, bd_pic = previews[ev.key], previews[bd.key]
    assert names[5] in first_pic[0].groups[1] and names[0] not in {p.signup_name for r in first_pic for p in r.selected}
    assert len(bd_pic) == 2 and names[7] in bd_pic[1].groups[0]
    line = asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked" and rs.events[bd.key].state == "locked", line
    assert shape(first_pic) == shape(ev.all_rosters), "the first raid: the board was the lock"
    assert shape(bd_pic) == shape(rs.events[bd.key].all_rosters), "the later raid: the board was the lock"


def test_a_later_part_prefers_the_earlier_previews_rostered(reg, rs):
    """24 joiners on Hyjal (20) → Barrow Deeps × 2 (20): the Barrow Deeps picture rosters the Hyjal picture's 20,
    bringing in someone else only for a role those 20 can't cover."""
    ev, bd = evening(reg, rs)
    pv = rc.event_board_preview(reg, rs, ev)
    on_first = {p.signup_name for r in pv[ev.key] for p in r.selected}
    on_bd = {p.signup_name for r in pv[bd.key] for p in r.selected}
    assert len(on_first) == 20 and len(on_bd) == 20
    role = {p.signup_name: p.role for p in rc.players_for(reg, bd)}
    slot = lambda r: "dps" if r in ("melee", "ranged") else r  # noqa: E731
    rb = reg.role_bounds(BD, rc.run_size(reg, bd))
    have = {r: sum(1 for x in on_first if slot(role[x]) == r) for r in rb}
    short = {r: 2 * int(rb[r].get("min") or 0) - have[r] for r in rb if 2 * int(rb[r].get("min") or 0) > have[r]}
    extra = on_bd - on_first
    assert all(slot(role[x]) in short for x in extra) and len(extra) <= sum(short.values())


def test_a_board_solve_stops_at_once_when_told(reg, rs):
    """solver.Stop on a real CP-SAT search: stop() from another thread ends the solve with Stopped (CpSolver.stop_search,
    OR-tools 9.15), long before its budget; a Stop already stopped never searches."""
    import threading
    import time

    from oibot_gm.roster import solver

    ev, _bd = evening(reg, rs)
    attached = threading.Event()

    class Seen(solver.Stop):
        def attach(self, s):
            super().attach(s)
            attached.set()

    stop = Seen()
    threading.Thread(target=lambda: (attached.wait(30), stop.stop()), daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(solver.Stopped):
        rc._solve_run(reg, rs, ev, time_limit=60.0, whatif=False, stop=stop)
    assert time.monotonic() - t0 < 30, "stopped long before the 60 s budget"
    with pytest.raises(solver.Stopped):
        rc._solve_run(reg, rs, ev, time_limit=60.0, whatif=False, stop=stop)
