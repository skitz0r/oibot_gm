"""Server fixes after the Events board page review: the site's answer lines in the guild's 12-hour words (never a
Discord time stamp), a locked run's open seats said truly while nothing is asked yet, and the board picture solved per
raid (a move on a later raid never re-solves the first)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import OWNER, join, lock_with_board
from test_event_board import in_order
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import event_board as eb
from oibot_gm import raidcycle as rc

HYJAL, BD = "hyjal_summit_forever", "barrow_deeps"
REAL = 474_000_000_000_000_000


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def member(reg, i: int, name: str, cls: str = "Priest", spec: str = "Shadow", off: str | None = "Holy"):
    reg.add_character(REAL + i, name, name, cls, spec, off, True)
    return reg.members[REAL + i]


# ---------------------------------------------------------------- 2. no Discord stamp on the site

def test_the_answer_route_speaks_the_guilds_time_never_a_discord_stamp(reg, rs, web_bot):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = [member(reg, i, name) for i, name in enumerate(("Anwen", "Brisa", "Corvin"))]
    for body in ({"member": str(ms[0].discord_id), "status": "in"}, {"member": str(ms[1].discord_id), "status": "sub"},
                 {"member": str(ms[2].discord_id), "status": "out", "note": "away at a wedding"},
                 {"member": str(ms[0].discord_id), "status": "in", "spec": "Holy"}):
        r = web_bot.c.post(f"/api/run/{ev.key}/set", json=body, headers=H())  # the officers' Answer for someone (the coordinator's "answer" route)
        assert r.status_code == 200, r.text
        msg = r.json()["message"]
        assert "<t:" not in msg and "**" not in msg, msg
        assert reg.local12(ev.start) in msg or "out of" in msg, msg


def test_the_switch_route_speaks_the_guilds_time(reg, rs, web_bot, monkeypatch):
    monkeypatch.setattr(eb.BoardCache, "start", lambda self, *a, **k: None)
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD, "rosters": 1}])
    nyx = member(reg, 0, "Nyx")
    rc.set_signup(reg, rs, ev, nyx, None, "in", source="test")
    sw = rc.event_switches(reg, rs, ev)
    row = next(r for r in sw["rows"] if r["uid"] == str(nyx.discord_id) and r["spec"] == "Holy")
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": sw["revs"][row["part"]]}
    r = web_bot.c.post(f"/api/run/{ev.key}/switch", json=body, headers=H())
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert "<t:" not in msg and reg.local12(ev.start) in msg, msg


def test_site_words_turns_every_stamp_into_local12(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    from oibot_gm.raid_views import run_label

    line = f"✅ Nyx Join for {run_label(reg, ev)} and <t:{int(ev.start.timestamp())}:R>"
    assert rc.site_words(reg, line) == f"Nyx Join for Barrow Deeps {reg.local12(ev.start)} and {reg.local12(ev.start)}"


# ---------------------------------------------------------------- 3. a locked run's open seats, while nothing is asked

def test_a_locked_run_with_open_seats_says_how_many_and_who_asks(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members()[:7])
    lock_with_board(reg, rs, ev, [s.display_name for s in ev.by_status("in")])
    assert ev.fill_state == "idle" and rc.open_seat_count(reg, ev) == 3
    team = rc.run_team(reg, ev)
    team["autofill"] = True
    assert rc.fill_state_text(reg, ev) == "3 seats are open. Automatic asks will ask for them."
    team["autofill"] = False
    assert rc.fill_state_text(reg, ev) == f"3 seats are open. Automatic asks are off, so an officer fills them with {rc.fill_button_words()}."
    team["autofill"], ev.autofill_paused, ev.autofill_paused_by = True, True, "Jon"
    assert rc.fill_state_text(reg, ev) == "Automatic asks paused by Jon. 3 seats are open."
    full = rc.open_run(reg, rs, BD, start_in(reg, 4), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, full, reg.test_members()[:10])
    lock_with_board(reg, rs, full, [s.display_name for s in full.by_status("in")])
    assert rc.fill_state_text(reg, full) == rc.FILL_STATE_WORDS["idle"], "no seat open: as before"


# ---------------------------------------------------------------- 4. the picture solved per raid

def _counting(monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    return calls


def _settle(cache, reg, rs, ev):
    async def go():
        await cache.get(reg, rs, ev)
        while cache.tasks:
            await asyncio.gather(*list(cache.tasks.values()))
        return await cache.get(reg, rs, ev)
    return asyncio.run(go())


def test_a_later_raid_move_solves_only_that_raid(reg, rs, monkeypatch):
    calls = _counting(monkeypatch)
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD, "rosters": 2}])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    first, bd = rc.event_parts(rs, ev)
    cache = eb.BoardCache()
    assert _settle(cache, reg, rs, ev)[0] == "ready" and [c[0] for c in calls] == [first.key, bd.key]
    bd = rs.events[bd.key]
    bd.layout = [[], [], [ms[3].display_name]]
    rs.save(bd, "a move on the later raid")
    calls.clear()
    st, pv = _settle(cache, reg, rs, ev)
    assert st == "ready" and [c[0] for c in calls] == [bd.key], "the first raid's answer comes from the memo"
    assert ms[3].display_name in pv[bd.key][1].groups[0]
    ev.pins[str(ms[5].discord_id)] = "out"  # the first raid rosters someone else: the later raid's `earlier` moves
    rs.save(ev, "kept off the first raid")
    calls.clear()
    _settle(cache, reg, rs, ev)
    assert [c[0] for c in calls] == [first.key, bd.key], "the first raid changed: the later raid prefers others, both solve"


def test_identical_inputs_solve_nothing(reg, rs, monkeypatch):
    calls = _counting(monkeypatch)
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD, "rosters": 1}])
    join(reg, rs, ev, reg.test_members())
    cache = eb.BoardCache()
    _settle(cache, reg, rs, ev)
    key = (reg.key, ev.key)
    a = cache.entries[key].previews
    calls.clear()
    parts, view = eb.snapshot(rs, ev), eb.frozen(rs)
    again = cache._preview(reg, view, parts, None, key)  # the same inputs, a fresh preview through the memo
    assert calls == [] and {k: id(v) for k, v in again.items()} == {k: id(v) for k, v in a.items()}


def test_the_memo_is_bounded_and_goes_with_its_event(reg, rs, monkeypatch):
    _counting(monkeypatch)
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    cache = eb.BoardCache()
    key = (reg.key, ev.key)
    for i in range(eb.MEMO_PER_EVENT + 3):
        ev.layout = [[ms[i].display_name]]
        rs.save(ev, "a move")
        _settle(cache, reg, rs, ev)
    assert len(cache.memo[key]) == eb.MEMO_PER_EVENT
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:10]])
    other = rc.open_run(reg, rs, BD, start_in(reg, 5), by="t", schedule=rc.ADHOC_SCHEDULE)
    asyncio.run(cache.get(reg, rs, other))
    assert key not in cache.memo and key not in cache.entries


def test_the_lock_never_reads_the_memo(reg, rs, monkeypatch):
    """The lock solves on its own: a memo full of a wrong answer changes nothing it builds."""
    calls = _counting(monkeypatch)
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members()[:6])
    cache = eb.BoardCache()
    _settle(cache, reg, rs, ev)
    key = (reg.key, ev.key)
    for k in list(cache.memo[key]):
        cache.memo[key][k] = "poisoned"
    calls.clear()
    rc.propose(reg, rs, ev, save=False)
    assert [c[0] for c in calls] == [ev.key] and ev.rosters and ev.rosters[0].selected
