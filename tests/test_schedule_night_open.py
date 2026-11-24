"""Two events on one night (the final fix wave of §5.67 phase 4). open_schedule only looked OPEN_HORIZON_DAYS ahead, so
a schedule's night further out opened as a one-off with no schedule stamp, and when the schedule's own sheet came due
the scheduler opened a SECOND run at the same minute and posted a second sheet. Now: a schedule's night is that
schedule's whatever the distance; opening it too early is refused with the Coming up row's sentence; asking for it as a
one-off (or from a pickup template) is refused on every surface; and a one-off already on such a night (made before
this fix) keeps it — the scheduler opens nothing there and warns ops once."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import weekly
from test_event_identity import _scheduler
from test_flows_raid import channel, harness, officer  # noqa: F401 — the wizard fixtures
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import configops
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.raid_views import run_title
from oibot_gm.registry import RegistryError
from oibot_gm.wizard import FLOWS

RID = "barrow_deeps"
OPENED = {"first_open": "2026-01-01T00:00"}


def run(coro):
    return asyncio.run(coro)


def nights(reg):
    """A weekly schedule whose night 22 days out (past the open horizon) and 8 days out (inside it) are both its own."""
    reg.set_raid_overrides(RID, OPENED, "t")
    far = (reg.now_local() + timedelta(days=22)).replace(hour=19, minute=30, second=0, microsecond=0)
    sid = weekly(reg, RID, far.strftime("%a %H:%M"))
    return sid, far, far - timedelta(days=14)


def test_a_schedule_night_past_the_horizon_is_still_that_schedules(reg):
    sid, far, _near = nights(reg)
    assert far - reg.now_local() > timedelta(days=rc.OPEN_HORIZON_DAYS)
    assert rc.open_schedule(reg, RID, far, None) == sid
    assert rc.which_run(reg, RID, far)["kind"] == "scheduled"
    assert rc.open_schedule(reg, RID, far + timedelta(hours=1), None) == rc.ADHOC_SCHEDULE, "any other time is a one-off"


def test_the_site_refuses_a_night_too_far_ahead_and_an_ad_hoc_on_a_schedule_night(reg, rs, web_bot):
    sid, far, near = nights(reg)
    c = web_bot.c
    p = c.post(f"/api/raid/{RID}/open/preview", json={"start": far.isoformat()}, headers=H()).json()
    assert p["schedule"] == sid and "too far ahead to open now" in (p["refusal"] or "")
    r = c.post(f"/api/raid/{RID}/open", json={"start": far.isoformat()}, headers=H())
    assert r.status_code == 400 and "too far ahead to open now" in r.json()["error"] and not rs.events
    r = c.post(f"/api/raid/{RID}/open", json={"start": near.isoformat(), "schedule": "adhoc"}, headers=H())
    assert r.status_code == 400 and r.json()["error"] == rc.SCHEDULE_NIGHT.format(when=reg.local12(near), name="Raid nights")
    assert not rs.events, "nothing written"
    ok = c.post(f"/api/raid/{RID}/open", json={"start": (near + timedelta(hours=1)).isoformat(), "schedule": "adhoc"}, headers=H())
    assert ok.status_code == 200, ok.text
    assert rs.events[ok.json()["key"]].schedule in (None, rc.ADHOC_SCHEDULE), "a time that is not a schedule's night still opens"
    ok = c.post(f"/api/raid/{RID}/open", json={"start": near.isoformat()}, headers=H())
    assert ok.status_code == 200 and rs.events[ok.json()["key"]].occurrence == rc.occurrence_stamp(reg, near), "its own night opens"


def test_plain_text_shows_both_refusals_before_apply_and_apply_writes_nothing(reg, rs):
    _sid, far, near = nights(reg)
    far_op = ConfigOp(op="run_open", target=RID, value=far.strftime("%Y-%m-%d %H:%M"))
    adhoc_op = ConfigOp(op="run_open", target=RID, field="adhoc", value=near.strftime("%Y-%m-%d %H:%M"))
    assert "This run is too far ahead to open now." in configops.describe(reg, far_op)
    assert rc.SCHEDULE_NIGHT.format(when=reg.local12(near), name="Raid nights") in configops.describe(reg, adhoc_op)
    from test_raid_night import NightBot

    bot = NightBot(reg, rs)
    for op in (far_op, adhoc_op):
        with pytest.raises(RegistryError):
            run(configops.apply_async(reg, op, "Jon", True, bot=bot))
    assert not rs.events


def test_discord_refuses_a_pickup_template_on_a_schedule_night_and_a_far_night(reg, rs, officer, channel):  # noqa: F811
    _sid, far, near = nights(reg)
    reg.set_schedule("pug", {"kind": "pickup", "name": "Pickup"}, "t", raid=RID)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", RID)
        await h.press("Ad hoc run…")
        await h.press("Pickup — pick a time")
        await h.submit(h.modal, day=near.date().isoformat(), hour=str(near.hour), minute=str(near.minute))
        assert "is a night of Raid nights, so a one-off can't start then." in h.text
        assert "Open" not in h.buttons()
        await h.press("Change")
        await h.press("Ad hoc run…")
        await h.press("One-off — pick a day and time")
        assert far.date().isoformat() in [o.value for o in h.modal._items["day"].options]
        await h.submit(h.modal, day=far.date().isoformat(), hour=str(far.hour), minute=str(far.minute))
        assert "That time is the Raid nights run" in h.text, "a one-off at a schedule's night is that schedule's run"
        assert "too far ahead to open now" in h.text and "Open" not in h.buttons()
    run(go())
    assert not [c for c in bot.calls if c[0] == "open_run_and_post"] and not rs.events


def test_a_one_off_already_on_the_night_keeps_it_and_the_scheduler_opens_no_second(reg, rs, monkeypatch):
    """The reviewer's dupprobe: a one-off made on a schedule's night beyond the horizon (before this fix: written here
    straight through open_run), then the schedule's sheet comes due. One run, one sheet, one ops warning naming both."""
    sid, far, _near = nights(reg)
    one_off = rc.open_run(reg, rs, RID, far, by="t", schedule=rc.ADHOC_SCHEDULE)
    assert one_off.occurrence is None, "the premise: an unstamped one-off on the night"
    bot, opened = _scheduler(reg, rs)
    warns: list[str] = []

    async def emit(cfg, level, text, *a, **k):
        if level == "warn":
            warns.append(text)
    monkeypatch.setattr(bot.ops, "emit", emit)
    lead = float(reg.schedule_def(RID, sid)["signup_lead_hours"])
    due = far - timedelta(hours=lead) + timedelta(minutes=5)
    monkeypatch.setattr(reg, "now_local", lambda: due)
    run(bot.scheduler_tick_guild(reg))
    run(bot.scheduler_tick_guild(reg))
    assert opened == [] and len([e for e in rs.live() if e.start == far]) == 1, "no second run at that minute"
    assert not bot.signup.sent, "no second sheet"
    night = [w for w in warns if "did not open its night" in w]
    assert len(night) == 1, warns
    assert "Raid nights" in night[0] and one_off.key not in night[0] and "already starts then" in night[0]
    # an open by hand at that minute is refused in the same words, never a second run beside it
    assert rc.open_refusal(reg, rs, RID, far, None) == rc.NIGHT_TAKEN.format(title=run_title(reg, one_off), name="Raid nights")
