"""F5 (2026-11-27): a box that answers "it has already happened". The owner pressed Unlock, the write took seconds, and
the presses after it came back "This event is still open for signup, so there is nothing to unlock." under the orange
"This changed while the box was open" line. A refusal that says only that the verb's own outcome is already true —
Unlock on an open event, Lock on a locked one, Cancel on a cancelled one, Start now on a started one — carries
`done: true` (on the answer and on its box), and the page closes the box and reloads instead of showing it. Any other
refusal carries no `done`. The page side is tests/test_frontend_source.py's (useConfirmBox)."""
from __future__ import annotations

from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture
from test_unlock import locked_event

from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc


def refused(r, done: bool):
    assert r.status_code in (400, 409), r.text
    j = r.json()
    assert bool(j.get("done")) is done and bool(j["box"].get("done")) is done, j
    assert j["box"]["verb"] is None, "a refusal: Back alone"
    return j


def test_unlock_on_an_open_event_is_done_and_on_a_cancelled_one_is_not(reg, rs, web_bot):
    _bot, ev, _ms = locked_event(reg, rs)
    c, url = web_bot.c, f"/api/run/{ev.key}/unlock"
    assert c.post(url, json={"seen": cq.unlock_box(reg, ev, rs)["fp"]}, headers=H()).status_code == 200
    assert refused(c.post(url, json={"preview": True}, headers=H()), True)["error"] == rc.UNLOCK_OPEN
    assert refused(c.post(url, json={"seen": "a box from before"}, headers=H()), True)["error"] == rc.UNLOCK_OPEN
    assert c.post(f"/api/run/{ev.key}/cancel", json={"notify": False}, headers=H()).status_code == 200
    assert refused(c.post(url, json={"preview": True}, headers=H()), False)["error"] == rc.UNLOCK_CANCELLED


def test_lock_on_a_locked_event_is_done_and_on_a_cancelled_one_is_not(reg, rs, web_bot):
    _bot, ev, _ms = locked_event(reg, rs)
    c, url = web_bot.c, f"/api/run/{ev.key}/lock"
    refused(c.post(url, json={"preview": True}, headers=H()), True)
    refused(c.post(url, json={"seen": "a box from before"}, headers=H()), True)
    assert c.post(f"/api/run/{ev.key}/cancel", json={"notify": False}, headers=H()).status_code == 200
    refused(c.post(url, json={"preview": True}, headers=H()), False)


def test_cancel_on_a_cancelled_event_is_done(reg, rs, web_bot):
    _bot, ev, _ms = locked_event(reg, rs)
    c, url = web_bot.c, f"/api/run/{ev.key}/cancel"
    assert c.post(url, json={"seen": cq.cancel_box(reg, ev, rs)["fp"], "notify": False}, headers=H()).status_code == 200
    refused(c.post(url, json={"preview": True}, headers=H()), True)
    refused(c.post(url, json={"seen": "a box from before", "notify": False}, headers=H()), True)


def test_start_now_on_a_started_event_is_done_and_on_an_open_one_is_not(reg, rs, web_bot):
    _bot, ev, _ms = locked_event(reg, rs)
    c, url = web_bot.c, f"/api/run/{ev.key}/start"
    assert c.post(url, json={"seen": cq.start_now_box(reg, ev, rs)["fp"]}, headers=H()).status_code == 200
    assert refused(c.post(url, json={"preview": True}, headers=H()), True)["error"] == rc.START_NOW_STARTED
    refused(c.post(url, json={"seen": "a box from before"}, headers=H()), True)
    _bot, other, _ms = locked_event(reg, rs)
    assert c.post(f"/api/run/{other.key}/unlock", json={"seen": cq.unlock_box(reg, other, rs)["fp"]}, headers=H()).status_code == 200
    assert refused(c.post(f"/api/run/{other.key}/start", json={"preview": True}, headers=H()), False)["error"] == rc.START_NOW_OPEN


# ---------------------------------------------------------------- the unlock answers before its Discord edits finish

def test_the_unlock_route_answers_while_its_discord_edits_go_on(reg, rs, web_bot):
    """The owner's unlock took 11 s: the state flipped at once, then the cards were edited and deleted. The route answers
    once the state and the audit line are written; the edits run on (RaidMixin.finish_later) and then finish: the sheet
    redrawn and the "unlocked" line posted."""
    import asyncio
    import threading
    import time

    _bot, ev, _ms = locked_event(reg, rs)
    bot = web_bot.bot
    slow, redrawn = threading.Event(), []

    async def refresh_sheet(reg_, ev_):
        await asyncio.to_thread(slow.wait, 10)  # Discord taking its time
        redrawn.append(ev_.key)

    bot.refresh_sheet = refresh_sheet
    with web_bot.c as c:  # one event loop for the whole block: the background edits keep running after the answer
        t = time.monotonic()
        r = c.post(f"/api/run/{ev.key}/unlock", json={"seen": cq.unlock_box(reg, ev, rs)["fp"]}, headers=H())
        assert r.status_code == 200 and "back to open signup" in r.json()["message"], r.text
        assert time.monotonic() - t < 5, "answered without waiting for Discord"
        assert rs.events[ev.key].state == "open" and not any(k.startswith("lock:") for k in rs.events[ev.key].cards)
        assert any("unlocked" in str(x) for x in bot.ops.lines), "the audit line is written before the answer"
        assert not redrawn and not any("unlocked by" in u for u in bot.updates), "the edits are still running"
        slow.set()
        deadline = time.monotonic() + 10
        while bot.__dict__.get("_finishing") and time.monotonic() < deadline:
            time.sleep(0.05)
    assert redrawn == [ev.key], "the sheet was redrawn behind the answer"
    assert any("unlocked by" in u for u in bot.updates), "and the thread's line went out"


def test_a_failing_discord_edit_after_the_unlock_is_a_warning_naming_the_event(reg, rs, caplog):
    import asyncio
    import logging

    bot, ev, _ms = locked_event(reg, rs)

    async def refresh_sheet(reg_, ev_):
        raise RuntimeError("Discord said no")

    bot.refresh_sheet = refresh_sheet

    async def go():
        line = await bot.unlock_run(reg, rs, ev, "Jon", answer_within=0.01)
        await asyncio.gather(*bot.__dict__.get("_finishing", set()))
        return line

    with caplog.at_level(logging.WARNING, logger="oibot_gm.discord_raid"):
        assert asyncio.run(go()).startswith("Unlocked")
    assert rs.events[ev.key].state == "open"
    assert any(ev.key in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records), caplog.text
