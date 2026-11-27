"""An event of two raids whose FIRST raid has ended while the later raid is still locked (a part-ended event): Cancel
takes the raids still to play from every surface, whichever raid's key is pressed — the site's route (either key), the
Events page's box and actions, the Discord card buttons and /raid cancel, plain text's run_cancel and MCP's cancel_run.
After it the event leaves Open for History. The unchanged cases stay: one raid done refuses, a live event cancels whole,
a later raid pressed while the first is live cancels the whole event. Unlock still refuses, in a truthful sentence."""
from __future__ import annotations

import pytest

from conftest import open_test_run
from test_flows_raid import channel, officer  # noqa: F401 — the fixtures
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import H, NightBot, run, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import configops, event_view
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError

LATER = ["The Barrow Deeps raid of this event is cancelled.", "Nobody gets a DM, but every ask card says Cancelled.",
         "A cancelled event cannot be reopened."]


def part_ended(reg, rs):
    """Hyjal Summit then Barrow Deeps, both locked; then Hyjal Summit ends (done). → (bot, first, later)."""
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == fev.state == "locked"
    lead.prev_state, lead.state, lead.closed_at = lead.state, "done", rc.now()
    rs.save(lead, "harness: the first raid ended")
    return bot, lead, fev


def audits(bot):
    return [t for _lvl, t in bot.ops.lines if "cancelled" in t]


# ---------------------------------------------------------------- the verb and the box

def test_the_box_from_the_ended_first_raid_names_the_raid_still_to_play(reg, rs):
    _bot, first, later = part_ended(reg, rs)
    b = cq.cancel_box(reg, first, rs)
    assert b["verb"] == "Cancel event" and b["danger"] and b["sentences"] == LATER, b
    assert b == cq.cancel_box(reg, later, rs), "either raid's key prints the same box"
    assert cq.consequence("cancel", reg, first, rs=rs) == LATER
    assert rc.cancel_target(rs, first) is later and rc.cancel_target(rs, later) is later
    assert not rc.cancel_tells_channel(rs, first), "the first raid has ended: no sheet, no channel line"


@pytest.mark.parametrize("pressed", ["first", "later"])
def test_cancel_on_either_raid_cancels_the_raid_still_to_play(reg, rs, pressed):
    bot, first, later = part_ended(reg, rs)
    line = run(bot.cancel_run(reg, rs, first if pressed == "first" else later, "Jon", None))
    assert (rs.events[first.key].state, rs.events[later.key].state) == ("done", "cancelled")
    assert line.startswith(f"Cancelled {rc.run_words(reg, later)}.") and "Nobody was DMed" in line, line
    a = audits(bot)
    assert len(a) == 1 and rc.run_words(reg, later) in a[0] and rc.run_words(reg, first) not in a[0], a
    assert not bot.signup.sent, "the first raid has ended: nothing posts in the signup channel"


def test_nothing_live_is_refused_as_today(reg, rs):
    bot, first, later = part_ended(reg, rs)
    run(bot.cancel_run(reg, rs, later, "Jon", None))
    for ev in (first, later):
        with pytest.raises(RegistryError, match="nothing was changed"):
            run(bot.cancel_run(reg, rs, rs.events[ev.key], "Jon", None))
        assert cq.cancel_box(reg, rs.events[ev.key], rs)["verb"] is None
    one = open_test_run(reg, rs, days=5)
    rc.end_run(reg, rs, one, "t")
    with pytest.raises(RegistryError, match="already done"):
        run(bot.cancel_run(reg, rs, rs.events[one.key], "Jon", None))
    assert cq.cancel_box(reg, rs.events[one.key], rs)["sentences"] == ["This event is already finished."]


def test_a_live_event_still_cancels_whole_from_either_raid(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    assert rc.cancel_target(rs, fev) is lead, "the first raid is live: it stands for the event"
    run(bot.cancel_run(reg, rs, fev, "Jon", None))
    assert rs.events[lead.key].state == rs.events[fev.key].state == "cancelled"
    a = audits(bot)
    assert len(a) == 1 and rc.run_words(reg, lead) in a[0] and rc.run_words(reg, fev) in a[0], a


# ---------------------------------------------------------------- the Events page

def test_the_event_view_offers_cancel_and_nothing_built_on_the_ended_raid(reg, rs):
    _bot, first, later = part_ended(reg, rs)
    v = event_view.event_json(reg, rs, first, 0)
    assert v["state"] == "locked" and v["actions"]["cancel"] and v["boxes"]["cancel"] == cq.cancel_box(reg, later, rs)
    assert not v["actions"]["reschedule"] and not v["actions"]["edit"] and v["boxes"]["reschedule"] is None and v["edit"] is None
    assert v["people"] == [] and v["signups"]["rows"]["none"] == [], "the answers closed with the first raid"


def test_after_the_cancel_the_event_leaves_open_for_history(reg, rs):
    bot, first, later = part_ended(reg, rs)
    lst = event_view.event_list(reg, rs, reg.now_local())
    assert [r["key"] for r in lst["open"]] == [first.key] and not lst["history"]
    run(bot.cancel_run(reg, rs, first, "Jon", None))
    lst = event_view.event_list(reg, rs, reg.now_local())
    assert not lst["open"] and [(r["key"], r["state"]) for r in lst["history"]] == [(first.key, "done")]


# ---------------------------------------------------------------- the site and MCP

@pytest.mark.parametrize("pressed", ["first", "later"])
def test_the_route_cancels_from_either_key_with_the_box_seen(reg, rs, web_bot, pressed):
    bot, first, later = part_ended(reg, rs)
    url = f"/api/run/{(first if pressed == 'first' else later).key}/cancel"
    r = web_bot.c.post(url, json={"preview": True}, headers=H())
    assert r.status_code == 200 and r.json()["box"]["sentences"] == LATER, r.text
    r = web_bot.c.post(url, json={"seen": r.json()["box"]["fp"], "notify": False}, headers=H())
    assert r.status_code == 200 and rs.events[later.key].state == "cancelled" and rs.events[first.key].state == "done", r.text
    r = web_bot.c.post(url, json={"preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["verb"] is None


def test_mcp_cancel_reaches_the_rest_through_the_first_raids_key(reg, rs, web_bot, monkeypatch):
    from oibot_gm import mcp_server

    _bot, first, later = part_ended(reg, rs)

    class Api:
        def post(self, path, body=None):
            return web_bot.c.post(path, json=body or {}, headers=H()).json()

    monkeypatch.setattr(mcp_server, "api", lambda guild=None: Api())
    said = mcp_server.cancel_run(first.key)
    assert said.startswith("Cancelled") and rs.events[later.key].state == "cancelled", said


# ---------------------------------------------------------------- Discord

def _night(reg, rs):
    from test_raid_night_flows import night, officer_user

    return night(reg, rs, user=officer_user())


@pytest.mark.parametrize("pressed", ["first", "later"])
def test_a_card_cancel_on_either_raid_shows_the_box_then_cancels_the_rest(reg, rs, pressed):
    from oibot_gm.raid_buttons import RunButton

    _b, first, later = part_ended(reg, rs)
    h, bot = _night(reg, rs)
    run(RunButton(reg.key, (first if pressed == "first" else later).key, "cancel").callback(h.card_press()))
    assert "Cancel event" in h.buttons() and all(s in h.text for s in LATER), h.text
    run(h.press("Cancel event"))
    assert [c for c in bot.calls if c[0] == "cancel_run"] == [("cancel_run", later.key, None, True)]


def test_raid_cancel_picks_the_raid_still_to_play(reg, rs, officer, channel):  # noqa: F811
    from oibot_gm.wizard import FLOWS

    _b, first, later = part_ended(reg, rs)
    h, bot = _night(reg, rs)
    run(FLOWS["raid_cancel"](h.command(), reg))
    assert all(s in h.text for s in LATER), h.text
    run(h.press("Cancel event"))
    assert [c[1] for c in bot.calls if c[0] == "cancel_run"] == [later.key]


# ---------------------------------------------------------------- plain text

@pytest.mark.parametrize("pressed", ["first", "later"])
def test_plain_text_cancel_names_the_rest_and_cancels_it(reg, rs, pressed):
    bot, first, later = part_ended(reg, rs)
    op = ConfigOp(op="run_cancel", target=(first if pressed == "first" else later).key)
    d = configops.describe(reg, op, strict=True)
    assert " ".join(LATER) in d and "signup channel" not in d, d
    run(configops.apply_async(reg, op, "Officer", False, bot=bot, via="chat"))
    assert rs.events[later.key].state == "cancelled" and rs.events[first.key].state == "done"


# ---------------------------------------------------------------- Unlock still refuses, truthfully

def test_unlock_refuses_in_a_sentence_that_points_at_cancel(reg, rs):
    _bot, first, later = part_ended(reg, rs)
    for ev in (first, later):
        assert rc.unlock_refusal(reg, rs, ev) == rc.UNLOCK_PART_ENDED
        assert cq.unlock_box(reg, ev, rs)["sentences"] == [rc.UNLOCK_PART_ENDED]
    with pytest.raises(RegistryError) as e:
        configops.describe(reg, ConfigOp(op="run_unlock", target=first.key), strict=True)
    assert str(e.value) == rc.UNLOCK_PART_ENDED
    s = rc.UNLOCK_PART_ENDED
    assert s[0].isupper() and s.endswith(".") and s.count(".") == 1 and len(s.split()) <= 20 and not any(c in s for c in "();:—")
