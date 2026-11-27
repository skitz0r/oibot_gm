"""The follow-ups' fix round (review of F1–F6, 2026-11-27): a lock never lands under an unlock's late Discord edits, an
archived sheet thread still gives up its 🔒 line, shutdown waits for the edits still running, an empty roster's lock
line, the updates thread's normal "no card yet" is quiet, threads follow a move, a lineup's one thread names the raid
of a line about one raid and a cascading cancel posts one line, and the lock's warning when the officers' channel would
be the signup channel says what to change."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from conftest import join
from test_bench_last import base_eight, schedule_event
from test_event_threads import OPS, SIGNUP, ThreadBot, card_in, post_sheet
from test_follow_on import open_pair
from test_raid_night import run
from test_unlock import locked_event

from oibot_gm import discord_raid
from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError


def _tagged_open_layout(monkeypatch):
    orig = discord_raid.confirm_open_layout

    def tagged(*a, **k):
        v = orig(*a, **k)
        v._open_again = True
        return v

    monkeypatch.setattr(discord_raid, "confirm_open_layout", tagged)


# ---------------------------------------------------------------- 1. a relock during the unlock's Discord edits

def test_a_lock_waits_for_the_unlocks_discord_edits_and_is_never_overwritten(reg, rs, monkeypatch):
    """The reviewer's probe: the unlock answered, its Confirm-card redraws were still going when a Lock was pressed.
    The person's lock now waits for them; nothing redraws a Confirm card as "open again" once the event is locked, and the
    "unlocked" line comes before the lock's."""
    bot, ev, _ms = locked_event(reg, rs)
    _tagged_open_layout(monkeypatch)
    gate = asyncio.Event()
    seen = []

    async def edit_live_card(reg_, cid, mid, view):
        if getattr(view, "_open_again", False):
            await asyncio.wait_for(gate.wait(), 5)  # Discord slow on the unlock's Confirm card redraws
        seen.append(("open again" if getattr(view, "_open_again", False) else "lock card", rs.events[ev.key].state))
        return True

    bot.edit_live_card = edit_live_card

    async def go():
        await bot.unlock_run(reg, rs, ev, "Jon", answer_within=0.01)
        assert rs.events[ev.key].state == "open" and bot.unlock_finishing(reg, ev) is not None
        lock = asyncio.ensure_future(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False, by_person=True))
        await asyncio.sleep(0.05)
        assert not lock.done() and rs.events[ev.key].state == "open", "the lock waits for the unlock's edits"
        gate.set()
        await lock
        await asyncio.gather(*bot.__dict__.get("_finishing", set()))

    run(asyncio.wait_for(go(), 20))
    assert rs.events[ev.key].state == "locked"
    assert not [s for s in seen if s == ("open again", "locked")], seen
    assert any("unlocked by" in u for u in bot.updates), "the unlock finished, line and all, before the lock went on"


def test_the_clocks_lock_is_refused_while_an_unlock_finishes_and_late_edits_skip_a_locked_event(reg, rs, monkeypatch):
    bot, ev, _ms = locked_event(reg, rs)
    _tagged_open_layout(monkeypatch)
    gate = asyncio.Event()
    seen = []

    async def edit_live_card(reg_, cid, mid, view):
        await asyncio.wait_for(gate.wait(), 5)
        seen.append("open again" if getattr(view, "_open_again", False) else "card")
        return True

    bot.edit_live_card = edit_live_card

    async def go():
        await bot.unlock_run(reg, rs, ev, "Jon", answer_within=0.01)
        with pytest.raises(RegistryError) as e:  # the clock: no wait, it tries again next minute
            await bot.lock_run(reg, rs, rs.events[ev.key], fill=False)
        assert str(e.value) == rc.LOCK_UNLOCKING
        rs.events[ev.key].state = "locked"  # (b) a lock got in anyway: every later step re-checks and skips
        gate.set()
        await asyncio.gather(*bot.__dict__.get("_finishing", set()))

    updates_before = len(bot.updates)
    run(asyncio.wait_for(go(), 20))
    assert seen.count("open again") <= 1, "at most the redraw already under way; none after the re-check"
    assert not any("unlocked by" in u for u in bot.updates[updates_before:]), "no 'unlocked' line after a relock"
    w = rc.LOCK_UNLOCKING
    assert w.endswith(".") and w.count(".") == 1 and len(w.split()) <= 20


# ---------------------------------------------------------------- 2. the 🔒 line in an archived sheet thread

def test_unlock_unarchives_the_sheet_thread_to_delete_the_lock_line(reg, rs, caplog):
    from test_lock_as_it_stands import ten_with_board

    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    run(bot.lock_run(reg, rs, ev, by="Jon", fill=False))
    ev = rs.events[ev.key]
    tid, mid = ev.lock_line
    thread = bot.rooms[tid]
    thread.archived = True  # a day idle
    orig_delete = thread.msgs[mid].delete

    async def delete():
        if thread.archived:
            raise RuntimeError("50083: Thread is archived")
        await orig_delete()

    thread.msgs[mid].delete = delete

    async def edit(**kw):
        thread.archived = kw.get("archived", thread.archived)

    thread.edit = edit
    run(bot.unlock_run(reg, rs, ev, "Jon"))
    assert mid in thread.deleted and not thread.archived


def test_a_delete_that_still_fails_is_a_warning_naming_the_event(reg, rs, caplog):
    bot = ThreadBot(reg, rs)
    with caplog.at_level(logging.WARNING, logger="oibot_gm.discord_raid"):
        assert run(bot._delete_message(OPS, 999_999, key="bd-test")) is False
    assert any("bd-test" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- 3. shutdown waits for the edits still running

def test_shutdown_waits_for_finish_later_and_names_what_it_abandons(reg, rs, caplog):
    bot, ev, _ms = locked_event(reg, rs)

    async def go():
        quick = bot.finish_later(reg, ev, "the unlock", asyncio.sleep(0.01))
        stuck = bot.finish_later(reg, ev, "the unlock", asyncio.sleep(30))
        left = await bot.finish_pending(timeout=0.2)
        stuck.cancel()
        return quick.done(), left

    with caplog.at_level(logging.WARNING, logger="oibot_gm.discord_raid"):
        quick_done, left = run(go())
    assert quick_done and left == [f"the unlock of {reg.key}/{ev.key}"]
    assert any(ev.key in r.getMessage() and "abandoned" in r.getMessage() for r in caplog.records)


def test_the_bot_waits_for_them_before_it_closes():
    import inspect

    from oibot_gm.discord_bot import OibotGM

    src = inspect.getsource(OibotGM.close)
    assert src.index("finish_pending") < src.index("STORE.flush"), "the edits first, then the push"


# ---------------------------------------------------------------- 6. an empty roster's lock line

def test_an_empty_rosters_lock_line_says_nobody_is_rostered(reg, rs):
    bot = ThreadBot(reg, rs)
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg))
    ev = post_sheet(bot, reg, rs, rs.events[ev.key])
    run(bot.lock_run(reg, rs, ev, by="Jon", fill=False, by_person=True))
    ev = rs.events[ev.key]
    lines = bot.rooms[ev.sheet_thread_id].lines()
    assert any("is locked with nobody rostered yet. An officer decides who takes a seat" in x for x in lines), lines
    assert not any("0 rostered" in x or "confirm in your DMs" in x for x in lines)


# ---------------------------------------------------------------- 7. no card yet is the normal state, not a warning

def test_no_card_yet_is_not_a_warning(reg, rs, caplog):
    bot = ThreadBot(reg, rs)
    ev = schedule_event(reg, rs)
    with caplog.at_level(logging.INFO, logger="oibot_gm.discord_raid"):
        run(bot.post_run_update(reg, ev, "✏️ a move"))
    no_card = [r for r in caplog.records if "no updates thread" in r.getMessage()]
    assert no_card and all(r.levelno < logging.WARNING for r in no_card)


# ---------------------------------------------------------------- 8. a move renames the event's threads

@pytest.mark.parametrize("how", ["reschedule", "start now"])
def test_a_move_renames_the_sheet_and_updates_threads(reg, rs, how):
    from test_lock_as_it_stands import ten_with_board

    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    run(bot.lock_run(reg, rs, ev, by="Jon", fill=False))
    ev = rs.events[ev.key]
    card_in(bot, rs, ev, OPS)
    run(bot.post_run_update(reg, ev, "a line"))
    ev = rs.events[ev.key]
    names = {}
    for tid in (ev.sheet_thread_id, ev.updates_thread_id):
        th = bot.rooms[tid]

        async def edit(*, name=None, _th=th, **kw):
            names[_th.id] = name
            _th.name = name

        th.edit = edit
    if how == "reschedule":
        run(bot.reschedule_event(reg, rs, ev, start=ev.start + timedelta(hours=1), by="Jon"))
    else:
        run(bot.start_now(reg, rs, ev, "Jon"))
    title = discord_raid.run_title(reg, rs.events[ev.key])
    assert names == {ev.sheet_thread_id: title, ev.updates_thread_id: f"{title} · updates"}


# ---------------------------------------------------------------- 9. one updates thread per lineup

def test_a_lineups_lines_name_their_raid_and_a_cascading_cancel_posts_one_line(reg, rs):
    reg.seed_test_members(24, "t")
    bot = ThreadBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    card_in(bot, rs, lead, OPS, key="health")
    run(bot.post_run_update(reg, rs.events[fev.key], "🧩 asked Ash"))
    thread = bot.rooms[rs.events[lead.key].updates_thread_id]
    assert thread.lines()[-1] == f"{reg.raid_def(fev.instance)['name']}: 🧩 asked Ash"
    run(bot.set_autofill_paused(reg, rs, rs.events[lead.key], True, by="Jon"))
    assert thread.lines()[-1].startswith("⏸️"), "an event-wide line names no raid"
    n = len(thread.lines())
    run(bot.cancel_run(reg, rs, rs.events[lead.key], "Jon", "server down", notify=False))
    new = thread.lines()[n:]
    assert len([x for x in new if x.startswith("🛑")]) == 1, new
    assert reg.raid_def(lead.instance)["name"] in new[0] and reg.raid_def(fev.instance)["name"] in new[0]
    assert rs.events[fev.key].state == "cancelled"


# ---------------------------------------------------------------- 11. the lock's warning names what to change

def test_the_lock_warning_says_the_officers_channel_cannot_be_the_signup_channel(reg, rs):
    bot = ThreadBot(reg, rs)
    reg.config.ops_channel_id = SIGNUP  # roster and ops both the signup channel
    ev = schedule_event(reg, rs)
    assert bot.officer_channel(reg, ev) is None
    why = bot.no_officer_channel_why(reg, ev)
    assert "can't be the signup channel" in why and "ops channel" in why and "Config page" in why
    reg.config.ops_channel_id = reg.config.roster_channel_id = None
    assert bot.no_officer_channel_why(reg, ev).startswith("No roster or ops channel is set.")
