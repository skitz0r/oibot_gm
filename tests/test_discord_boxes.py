"""Phase 5, Task 2: the run cards' Lock, Cancel, Remind and End print the server's boxes (§7b on Discord) — the same
box the site shows (consequences.lock_box / cancel_box / remind_box, LootMixin.end_box), its verb as the button, Back,
a box that changed while open shown again under CONFIRM_WORDS["changed"], a refusal with Back alone."""
from __future__ import annotations

import asyncio

import discord
from conftest import join, open_test_run
from test_flows_raid import channel, officer  # noqa: F401 — the fixtures
from test_raid_night_flows import NightFlowBot, locked_ten, officer_user

from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.raid_buttons import RunButton, cancel_decide
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness


def run(c):
    return asyncio.run(c)


def bot_for(reg, rs):
    return NightFlowBot(rs, reg)


def reals(reg, n, start=0):
    return reg.test_members()[start:start + n]


def joined_run(reg, rs, n):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reals(reg, n))
    return ev


def test_lock_screen_is_the_lock_box(reg, rs, officer, channel):  # noqa: F811
    ev = joined_run(reg, rs, 6)
    h = Harness(reg, rs, client=bot_for(reg, rs))
    run(FLOWS["raid_lock"](h.command(), reg))
    box = cq.lock_box(reg, ev, rs)  # nobody on the board: the lock as it stands warns its roster locks empty (F3), both offered
    assert box["title"] in h.text and rc.LOCK_EMPTY_WARN_ONE in box["sentences"] and rc.LOCK_EMPTY_WARN_ONE in h.text
    assert h.buttons() == ["Lock as it stands", "Lock and auto-fill", "Preview lock", "Back"]
    run(h.press("Lock and auto-fill"))
    box = cq.lock_box(reg, ev, rs, fill=True)
    assert box["title"] in h.text and all(s in h.text for s in box["sentences"])
    assert h.buttons() == ["Lock and auto-fill", "Preview lock", "Back"]
    assert h.screen["view"].children[0].style is discord.ButtonStyle.success


def test_a_lock_box_that_changed_is_shown_again(reg, rs, officer, channel):  # noqa: F811
    ev = joined_run(reg, rs, 6)
    bot = bot_for(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_lock"](h.command(), reg))
    run(h.press("Lock and auto-fill"))
    join(reg, rs, ev, reals(reg, 1, start=6))  # someone joins while the box is open
    run(h.press("Lock and auto-fill"))
    assert not [c for c in bot.calls if c[0] == "lock_run"] and h.text.startswith(CONFIRM_WORDS["changed"])
    run(h.press("Lock and auto-fill"))
    assert ("lock_run", ev.key, "Tester") in bot.calls


def test_the_preview_screens_lock_goes_back_through_the_box(reg, rs, officer, channel):  # noqa: F811
    ev = joined_run(reg, rs, 6)
    bot = bot_for(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_lock"](h.command(), reg))
    run(h.press("Lock and auto-fill"))
    run(h.press("Preview lock"))
    assert h.buttons() == ["Lock and auto-fill", "Preview lock", "Back"] and cq.lock_box(reg, ev, rs)["title"] in h.text
    assert h.screen["embed"] is not None, "the roster the lock would build, under the box"
    join(reg, rs, ev, reals(reg, 1, start=6))  # the box changed during the preview
    run(h.press("Lock and auto-fill"))
    assert not [c for c in bot.calls if c[0] == "lock_run"] and h.text.startswith(CONFIRM_WORDS["changed"])
    assert h.buttons() == ["Lock and auto-fill", "Preview lock", "Back"]
    run(h.press("Lock and auto-fill"))
    assert ("lock_run", ev.key, "Tester") in bot.calls


def test_cancel_is_red_and_its_note_carries_the_reason_and_the_channel(reg, rs, officer, channel):  # noqa: F811
    ev = joined_run(reg, rs, 3)
    bot = bot_for(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_cancel"](h.command(), reg))
    box = cq.cancel_box(reg, ev, rs)
    assert box["title"] in h.text and all(s in h.text for s in box["sentences"])
    assert h.buttons() == ["Cancel event", "Cancel event with a note", "Back"]
    assert h.screen["view"].children[0].style is discord.ButtonStyle.danger
    run(h.press("Cancel event with a note"))
    run(h.submit(reason="Server maintenance.", notify="quiet"))
    assert ("cancel_run", ev.key, "Server maintenance.", False) in [c[:4] for c in bot.calls]


def test_a_plain_cancel_has_no_reason_and_tells_the_channel(reg, rs, officer, channel):  # noqa: F811
    ev = joined_run(reg, rs, 3)
    bot = bot_for(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_cancel"](h.command(), reg))
    run(h.press("Cancel event"))
    assert ("cancel_run", ev.key, None, True) in [c[:4] for c in bot.calls]


def test_a_closed_run_shows_the_refusal_box_with_back_only(reg, rs):
    ev = joined_run(reg, rs, 3)
    bot = bot_for(reg, rs)
    h = Harness(reg, rs, client=bot, user=officer_user())
    run(RunButton(reg.key, ev.key, "cancel").callback(h.card_press()))
    assert h.buttons()[0] == "Cancel event"
    rs.events[ev.key].state = "cancelled"  # another officer cancelled it while this box was open
    run(h.press("Cancel event"))
    refusal = cq.cancel_box(reg, ev, rs)
    assert refusal["verb"] is None and h.buttons() == ["Back"] and refusal["sentences"][0] in h.text
    assert not [c for c in bot.calls if c[0] == "cancel_run"]
    h2 = Harness(reg, rs, client=bot, user=officer_user())  # the entry point itself, on a closed run
    run(cancel_decide(h2.command(), bot, reg, rs, ev))
    assert h2.buttons() == ["Back"] and refusal["sentences"][0] in h2.text


def test_remind_and_end_print_their_boxes(reg, rs, monkeypatch):
    ev, seated = locked_ten(reg, rs)
    for m in seated:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    bot = bot_for(reg, rs)
    h = Harness(reg, rs, client=bot, user=officer_user())

    async def go():
        await RunButton(reg.key, ev.key, "remind").callback(h.card_press())
        box = cq.remind_box(reg, ev, rs)
        assert h.buttons() == ["Send reminder", "Back"] and box["title"] in h.text
        assert all(s in h.text for s in box["sentences"]) and not [c for c in bot.calls if c[0] == "remind"]
        await h.press("Send reminder")
        assert ("remind", ev.key, "Tester") in bot.calls
        await RunButton(reg.key, ev.key, "end").callback(h.card_press())
        why = rc.end_refusal(ev)
        # before the start End is the refusal box, with Start now beside Back (issue 14, Task 6)
        assert why == rc.END_NOT_STARTED and h.buttons() == ["Start now", "Back"] and why in h.text
        monkeypatch.setattr(rc, "end_refusal", lambda ev_, at=None: None)
        await RunButton(reg.key, ev.key, "end").callback(h.card_press())
        assert h.buttons() == ["End the event", "Back"]
        assert all(s in h.text for s in bot.end_sentences(reg, rs, ev)) and bot.end_box(reg, rs, ev)["title"] in h.text
        await h.press("End the event")
        assert ev.state == "done" and ("end_run", ev.key, "Tester") in bot.calls
    run(go())


def test_a_remind_refusal_is_the_box_not_a_message(reg, rs):
    ev = joined_run(reg, rs, 3)  # open, not locked: nothing to remind
    h = Harness(reg, rs, client=bot_for(reg, rs), user=officer_user())
    run(RunButton(reg.key, ev.key, "remind").callback(h.card_press()))
    box = cq.remind_box(reg, ev, rs)
    assert box["verb"] is None and h.buttons() == ["Back"] and box["sentences"][0] in h.text
