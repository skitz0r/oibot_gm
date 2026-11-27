"""F3 (2026-11-27): an empty board is the officer's choice. "If I want to lock with a not-full roster that is my
choice." A PERSON's lock as it stands (the site's box, Discord's Lock as it stands, plain text, MCP) with people who
answered and nobody on a raid's board is no longer refused: the box warns in one sentence within its three
("Nobody is on the Hyjal Summit board, so its roster locks empty.") and Lock stays pressable; the lock writes that
raid's roster empty (every joiner Not rostered, with the existing notice; Automated Fill Requests paused as for any lock
as it stands with open slots). A lineup with one raid empty and another placed locks whole. The CLOCK's lock (Auto-fill
on Auto Lock off, an empty board) still fails once and the sheet stays open (tests/test_lock_as_it_stands.py).

A deliberately empty locked raid is told from a crashed lock by what the lock writes in the same save as its flip — its
roster list, one roster even when nobody is on it (rc.lock_unfinished): the scheduler's crash recovery and recover_lock
leave it alone, also read back from disk. The board, the locked sheet, the lock cards, the members' page, Unlock (back
to an empty board), End and Cancel all work on it."""
from __future__ import annotations

from datetime import timedelta

from conftest import join
from test_bench_last import base_eight, schedule_event
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import H, NightBot, run, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp


def empty_board(reg, rs, autofill: bool = True):
    """A schedule's event: eight joined, nobody placed (Automated Fill Requests on unless `autofill` is False)."""
    ev = schedule_event(reg, rs)
    ms = base_eight(reg)
    join(reg, rs, ev, ms)
    rc.reschedule(reg, rs, ev, timing={"autofill": autofill}, by="o")
    return rs.events[ev.key], ms


def lock_empty(bot, reg, rs, ev):
    return run(bot.lock_run(reg, rs, ev, by="Jon", fill=False, by_person=True))


def not_rostered(reg, ev, m) -> list[dict]:
    return [n for n in reg.members[m.discord_id].notices if n.get("kind") == "not_rostered" and n.get("run") == ev.key]


# ---------------------------------------------------------------- the box warns, Lock stays pressable

def test_the_box_warns_in_one_sentence_and_lock_stays_pressable(reg, rs):
    ev, _ms = empty_board(reg, rs)
    assert rc.lock_empty_refusal(reg, rs, ev) == rc.LOCK_EMPTY_BOARD, "the clock's lock still refuses it"
    b = cq.lock_box(reg, ev, rs)
    slots = rc.lock_open_slots(reg, rs, ev)[0]
    assert b["verb"] == "Lock" and b["choice"] == {"label": b["choice"]["label"], "value": False}
    assert b["sentences"] == [f"The roster has {slots} open slots.", rc.LOCK_EMPTY_WARN_ONE, cq.LOCK_PAUSE]


def test_a_lineup_with_an_empty_later_board_warns_naming_the_raid(reg, rs):
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    b = cq.lock_box(reg, lead, rs)
    raid = reg.raid_def(fev.instance)["name"]
    assert b["verb"] == "Lock" and b["sentences"][0] == f"Nobody is on the {raid} board, so its roster locks empty."
    assert len(b["sentences"]) <= 3


# ---------------------------------------------------------------- the lock goes through

def test_a_persons_lock_with_an_empty_board_locks_it_empty(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = empty_board(reg, rs)
    line = lock_empty(bot, reg, rs, ev)
    ev = rs.events[ev.key]
    assert ev.state == "locked" and not ev.seated() and len(ev.all_rosters) == 1 and not ev.lock_error, line
    assert ev.lock_fill is False and ev.autofill_paused and ev.autofill_paused_by == "Jon", "the asks pause: slots left open"
    assert all(len(not_rostered(reg, ev, m)) == 1 for m in ms), "every joiner is Not rostered, with the notice"
    assert not any("failed" in u for u in bot.updates)


def test_the_lock_card_goes_up_for_an_empty_roster_and_recovery_posts_no_second(reg, rs):
    from test_event_threads import ThreadBot, post_sheet

    bot = ThreadBot(reg, rs)
    ev, _ms = empty_board(reg, rs)
    ev = post_sheet(bot, reg, rs, ev)
    lock_empty(bot, reg, rs, ev)
    ev = rs.events[ev.key]
    card = ev.cards.get("lock:0")
    assert ev.state == "locked" and card, "the lock card went up"
    assert run(bot.recover_lock(reg, rs, ev)) == 0 and rs.events[ev.key].cards["lock:0"] == card, "nothing re-posted"


def test_a_lineup_with_one_raid_empty_and_another_placed_locks_whole(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    lock_empty(bot, reg, rs, lead)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == "locked" and fev.state == "locked", "the whole event locks"
    assert {p.signup_name for p in lead.seated()} == {m.display_name for m in ms[:5]} and not fev.seated()
    assert not lead.lock_error and not fev.lock_error


def test_every_surface_locks_an_empty_board(reg, rs, web_bot):
    ev, _ms = empty_board(reg, rs)
    url = f"/api/run/{ev.key}/lock"
    pv = web_bot.c.post(f"{url}/preview", json={"fill": False}, headers=H())
    assert pv.status_code == 200 and not any(g for r in pv.json()["board"]["rosters"] for g in r["groups"]), pv.text
    b = web_bot.c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert b["verb"] == "Lock"
    r = web_bot.c.post(url, json={"seen": b["fp"]}, headers=H())
    assert r.status_code == 200 and rs.events[ev.key].state == "locked", r.text
    other, _ms = empty_board(reg, rs)  # MCP: the route without a box
    assert web_bot.c.post(f"/api/run/{other.key}/lock", json={}, headers=H()).status_code == 200
    assert rs.events[other.key].state == "locked"


def test_plain_text_locks_an_empty_board_after_its_review(reg, rs):
    bot = NightBot(reg, rs)
    ev, _ms = empty_board(reg, rs)
    op = ConfigOp(op="run_lock", target=ev.key)
    d = configops.describe(reg, op)
    assert rc.LOCK_EMPTY_WARN_ONE in d and "lock " in d, d
    run(configops.apply_async(reg, op, "Officer", False, bot=bot, via="chat"))
    assert rs.events[ev.key].state == "locked" and not rs.events[ev.key].seated()


def test_discord_lock_as_it_stands_locks_an_empty_board():
    """raid_buttons.lock_decide no longer refuses an empty board before lock_run (the box warns instead)."""
    import inspect

    from oibot_gm import raid_buttons

    assert "lock_empty_refusal" not in inspect.getsource(raid_buttons.lock_decide)


# ---------------------------------------------------------------- a deliberate empty lock is never a crashed one

def test_the_scheduler_and_recovery_leave_an_empty_lock_alone_also_read_back_from_disk(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev, _ms = empty_board(reg, rs)
    lock_empty(bot, reg, rs, ev)
    assert not rc.lock_unfinished(rs.events[ev.key]), "the lock wrote its roster: one roster, nobody on it"
    rs2 = rc.RaidStore(rs.store, rs.key)  # a restart: the files as written
    back = rs2.events[ev.key]
    assert back.state == "locked" and len(back.all_rosters) == 1 and not back.seated() and not rc.lock_unfinished(back)
    bot2 = NightBot(reg, rs2)
    at = back.start - timedelta(hours=2)
    monkeypatch.setattr(reg, "now_local", lambda: at)
    run(bot2.scheduler_tick_guild(reg))
    assert rs2.events[ev.key].state == "locked", "never reopened as a crashed lock"
    assert not any("locked without a roster" in str(x) for x in bot2.ops.lines)
    assert run(bot2.recover_lock(reg, rs2, rs2.events[ev.key])) == 0
    crashed = rs2.events[ev.key].model_copy(deep=True)
    crashed.rosters = []
    assert rc.lock_unfinished(crashed), "a locked raid with no roster written is the crash the recovery reopens"


# ---------------------------------------------------------------- every page and verb on an empty locked raid

def test_the_board_sheet_cards_and_members_page_draw_an_empty_locked_raid(reg, rs, web_bot):
    from oibot_gm import member_event_view
    from oibot_gm.raid_views import sheet_message

    bot = NightBot(reg, rs)
    ev, ms = empty_board(reg, rs)
    lock_empty(bot, reg, rs, ev)
    ev = rs.events[ev.key]
    n = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    assert rc.board_layout(reg, ev) == [[] for _ in range(n)]
    embed, _view = sheet_message(reg, ev, rc.run_team(reg, ev), bot.ico)
    assert embed is not None
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["board_state"] == "locked" and part["rostered_words"].startswith("0 of")
    assert web_bot.c.get(f"/api/run/{ev.key}", headers=H()).status_code == 200
    mine = member_event_view.member_event_json(reg, rs, ev, ms[0].discord_id)
    assert mine["state"] == "locked" and len(mine["rosters"]) == 1, "the members' page draws the empty roster"


def test_unlock_end_and_cancel_work_on_an_empty_locked_raid(reg, rs):
    bot = NightBot(reg, rs)
    ev, _ms = empty_board(reg, rs)
    lock_empty(bot, reg, rs, ev)
    run(bot.unlock_run(reg, rs, rs.events[ev.key], "Jon"))
    ev = rs.events[ev.key]
    assert ev.state == "open" and not any(g for g in rc.board_layout(reg, ev)), "back to an empty board"
    lock_empty(bot, reg, rs, ev)
    ev = rs.events[ev.key]
    assert ev.state == "locked"
    run(bot.cancel_run(reg, rs, ev, "Jon", None, notify=False))
    assert rs.events[ev.key].state == "cancelled"
    other, _ms = empty_board(reg, rs)
    lock_empty(bot, reg, rs, other)
    assert rc.end_run(reg, rs, rs.events[other.key], "Jon") and rs.events[other.key].state == "done"
