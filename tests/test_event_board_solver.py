"""Auto-fill groups and the lock, solved for real (slow: CP-SAT). Auto-fill groups writes the lock's own solve into the
board (the owner's tweaks C): for a non-trivial event — an officer's hand place, a kept-off pin, a split later raid —
after Auto-fill with nothing changed, lock_run rosters exactly that board (who, in which roster and group, in which
role), on the first raid and on the later raid, with the solver's kept answers and with them forgotten; Auto-fill on the
later raid alone reads who the first raid's lock will roster; and the lock path's preview helpers still agree."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from conftest import OWNER, join
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


def board_shape(reg, ev):
    """The board as written (ev.layout), cut into rosters, in shape()'s form (roles from the run's players)."""
    role = {p.signup_name: p.role for p in rc.players_for(reg, ev)}
    k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    lay = [list(g) for g in ev.layout or []]
    out = []
    for i in range(0, len(lay), k):
        groups = lay[i:i + k]
        names = [x for g in groups for x in g]
        if names or not out:
            out.append(([sorted(g) for g in groups], sorted((x, role[x]) for x in names)))
    return [o for o in out if o[1]] or out


def evening(reg, rs):
    reg.seed_test_members(24, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, HYJAL, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD, "rosters": 2}])
    join(reg, rs, ev, reg.test_members())
    return ev, rc.event_parts(rs, ev)[1]


def fill(bot, reg, rs, ev):
    async def go():
        return await eb.autofill_of(bot).start(reg, rs, ev, by="Jon", by_id=OWNER, via="web")
    t0 = time.monotonic()
    said = asyncio.run(go())
    print(f"auto-fill {ev.instance}: {time.monotonic() - t0:.1f} s")
    assert said is None, said
    return rs.events[ev.key]


def _filled(reg, rs):
    """The non-trivial evening (a hand place and a kept-off pin on the first raid, a hand place on the later raid), both
    raids auto-filled. → (ev, bd, names, bot)."""
    ev, bd = evening(reg, rs)
    names = [s.display_name for s in ev.by_status("in")]
    bot = NightBot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, names[5], (1, 0), "Jon", by_id=OWNER))  # an officer's hand placement: group 2
    ev.pins[next(k for k, s in ev.signups.items() if s.display_name == names[0])] = "out"  # kept off through ⋮
    rs.save(ev, "kept off")
    bd = rs.events[bd.key]
    asyncio.run(bot.board_move(reg, rs, bd, names[7], (2, 0), "Jon", by_id=OWNER))  # roster 2, group 1 of the later raid
    ev, bd = fill(bot, reg, rs, ev), fill(bot, reg, rs, rs.events[bd.key])
    assert names[5] in ev.layout[1] and names[0] not in {x for g in ev.layout for x in g}
    assert names[7] in bd.layout[2] and len(board_shape(reg, bd)) == 2
    return ev, bd, names, bot


@pytest.mark.parametrize("forget", [False, True], ids=["kept answers", "searched again"])
def test_after_autofill_the_lock_rosters_exactly_the_board_on_every_raid(reg, rs, forget):
    """`searched again`: the solver's kept answers forgotten between Auto-fill and the lock, so the lock searches at its
    budget for itself. Either way the lock rosters the board Auto-fill wrote (every placement is now a fixed one)."""
    from oibot_gm.roster import solver

    ev, bd, names, bot = _filled(reg, rs)
    first_board, bd_board = board_shape(reg, ev), board_shape(reg, bd)
    if forget:
        solver.forget_answers()
    t0 = time.monotonic()
    line = asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    print(f"lock after auto-fill: {time.monotonic() - t0:.1f} s")
    bd = rs.events[bd.key]
    assert ev.state == "locked" and bd.state == "locked", line
    assert shape(ev.all_rosters) == first_board, "the first raid: the board was the lock"
    assert shape(bd.all_rosters) == bd_board, "the later raid: the board was the lock"


def test_autofill_on_the_later_raid_alone_is_what_its_lock_rosters(reg, rs):
    """The first raid's board left empty (the lock places it), Auto-fill pressed on the later raid only: it reads who the
    first raid's lock will roster (rc.earlier_counts), so after the lock the later raid is the board it wrote."""
    ev, bd = evening(reg, rs)
    bot = NightBot(reg, rs)
    bd = fill(bot, reg, rs, bd)
    want = board_shape(reg, bd)
    asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert ev.layout is None, "the first raid's board was never written"
    assert shape(rs.events[bd.key].all_rosters) == want


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


def test_a_raid_not_full_locks_with_one_name_kept_off(reg, rs):
    """Six joined a 10, one kept off (the ⋮ menu's pin out): the lock used to be INFEASIBLE (the seats counted the
    kept-off name); now it locks with the other five and the lock's own preview showed the same."""
    reg.seed_test_members(24, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, BD, start, by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms)
    ev.pins[str(ms[2].discord_id)] = "out"
    rs.save(ev, "kept off")
    pic = rc.event_board_preview(reg, rs, ev)[ev.key]
    assert isinstance(pic, list), pic
    line = asyncio.run(NightBot(reg, rs).lock_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked" and not ev.lock_error, line
    on = {p.signup_name for p in ev.seated()}
    assert on == {m.display_name for m in ms} - {ms[2].display_name}
    assert shape(pic) == shape(ev.all_rosters)


def _preview_later_then_lock(reg, rs, forget=False):
    from oibot_gm.roster import solver

    ev, bd = evening(reg, rs)
    bot = NightBot(reg, rs)
    shown = asyncio.run(bot.lock_preview(reg, rs, bd, by="Jon"))  # the first raid is still open
    if forget:  # the lock searches for itself at its budget: the solver's own repeatability
        solver.forget_answers()
    asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    bd = rs.events[bd.key]
    assert ev.state == "locked" and bd.state == "locked"
    return shown, bd


@pytest.mark.parametrize("forget", [False, True], ids=["kept answers", "searched again"])
def test_preview_lock_on_a_later_raid_while_the_first_is_open_is_what_its_lock_rosters(reg, rs, forget):
    """The rehearsal's F2: Preview lock pressed on the later raid alone solved without the earlier raid's placement, so
    it could show another roster than the lock (which runs after the first raid locks and reads its roster). It now
    solves with who the first raid's lock will roster (rc.earlier_counts), as the board picture does."""
    shown, bd = _preview_later_then_lock(reg, rs, forget)
    assert shape(shown) == shape(bd.all_rosters), "Preview lock was the lock"


def test_without_the_earlier_raids_the_later_preview_is_another_roster(reg, rs, monkeypatch):
    """The fixture is one where it matters: the same preview made without the earlier raids differs from the lock."""
    monkeypatch.setattr(rc, "earlier_counts", lambda *a, **k: None)
    shown, bd = _preview_later_then_lock(reg, rs)
    assert shape(shown) != shape(bd.all_rosters)


@pytest.mark.parametrize("how", ["keep off", "bench"])
def test_after_autofill_a_kept_off_or_benched_name_is_not_rostered(reg, rs, how):
    """The fix round's probe: Auto-fill places everyone it seats, then one placed name is kept off (⋮ pin out) or
    answers Bench — they leave the board in the same save (rc.prune_board), so nothing keeps them on at the lock: the
    lock leaves them off and fills their seat from Not placed (22 joined a 20: the first-come joiner next in line)."""
    from oibot_gm import inbox

    reg.seed_test_members(24, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, HYJAL, start, by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:22])
    bot = NightBot(reg, rs)
    ev = fill(bot, reg, rs, ev)
    who = next(m for m in ms if m.display_name in {x for g in ev.layout for x in g})
    if how == "keep off":
        inbox.pin_write(reg, rs, ev, who, "out", "Jon")
    else:
        asyncio.run(bot.set_answer(reg, rs, ev, who, "sub", None, "Jon", by_id=OWNER))
    assert who.display_name not in {x for g in ev.layout or [] for x in g}, "off the board in the same save"
    assert who.display_name not in rc.lock_forces(reg, rs, ev).kept_on
    if how == "keep off":
        line = asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
        on = {p.signup_name for p in ev.seated()}
        assert ev.state == "locked" and who.display_name not in on and len(on) == 20, line
        return
    # Bench: nothing on the board keeps them on any more; the lock treats them as it treats every Bench answer (a
    # candidate with the solver's bench penalty) — exactly as if they had never been placed
    shown = asyncio.run(bot.lock_preview(reg, rs, ev, by="Jon"))
    line = asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked" and shape(shown) == shape(ev.all_rosters), line


def test_a_second_unchanged_press_answers_at_once(reg, rs):
    """The first press's own placements are taken off before it solves again, so an unchanged second press builds the
    first press's model: the solver's kept answer, no search, and nothing written (autofill_same)."""
    from oibot_gm.constants import PAGE_WORDS

    ev, bd = evening(reg, rs)
    bot = NightBot(reg, rs)
    fill(bot, reg, rs, ev)
    log = len(rs.events[ev.key].log)

    async def again():
        return await eb.autofill_of(bot).start(reg, rs, rs.events[ev.key], by="Jon", by_id=OWNER, via="web")
    t0 = time.monotonic()
    said = asyncio.run(again())
    took = time.monotonic() - t0
    print(f"second unchanged press: {took:.2f} s")
    assert said == PAGE_WORDS["autofill_same"] and len(rs.events[ev.key].log) == log and took < 3
