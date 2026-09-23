"""The wizard core: dates and times without typing, Discord's limits checked offline, and the absence flow driven end to
end through fake interactions (tests/wizard_harness.py)."""
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
import pytest

from oibot_gm import wizard as wz
from oibot_gm.wizard_flows import FLOWS
from wizard_harness import Harness

LA = ZoneInfo("America/Los_Angeles")


# ---- time, resolved like a person means it

def test_resolve_local_normal_gap_and_overlap():
    dt, note = wz.resolve_local(datetime(2026, 9, 24, 19, 30), LA)
    assert note == "" and int(dt.timestamp()) == 1790303400
    dt, note = wz.resolve_local(datetime(2026, 3, 8, 2, 30), LA)  # the clocks go forward: 2:30 AM never happens
    assert note == "gap" and (dt.hour, dt.minute) == (3, 30) and dt.utcoffset() == timedelta(hours=-7)
    dt, note = wz.resolve_local(datetime(2026, 11, 1, 1, 30), LA)  # the clocks go back: 1:30 AM happens twice
    assert note == "ambiguous" and int(dt.timestamp()) == 1793521800  # the first pass (PDT)
    assert "doesn't exist" in wz.when_note("gap", dt) and "twice" in wz.when_note("ambiguous", dt)


def test_hours_minutes_and_durations_have_no_military_time_and_fit_discord():
    hours = wz.hour_opts(19)
    assert len(hours) == 24 and hours[0].label == "12 AM" and hours[12].label == "12 PM" and hours[19].label == "7 PM"
    assert next(o for o in hours if o.default).value == "19"
    assert not any(":" in o.label for o in hours)
    mins = wz.minute_opts(5, 30)
    assert len(mins) == 12 and [o.label for o in mins[:4]] == [":00", ":15", ":30", ":45"] and next(o for o in mins if o.default).value == "30"
    durs = wz.duration_opts()
    assert int(durs[-1].value) <= 120 and durs[0].value == "1" and len(durs) <= 25


def test_every_duration_is_a_span_the_registry_accepts(reg):
    m = reg.test_members()[0]
    start = date(2031, 1, 1)
    for o in wz.duration_opts():
        end = wz.span_end(start, int(o.value))
        assert end >= start and (end - start).days <= 120
        reg.add_absence(m.discord_id, start.isoformat(), end.isoformat(), None, "t")  # never refused
        reg.clear_absence(m.discord_id, start.isoformat(), "t")


def test_day_options_say_today_and_offer_later(reg):
    today = reg.now_local().date()
    opts = wz.day_opts(reg, today, later=True)
    assert len(opts) == 25 and opts[0].label.startswith("Today · ") and opts[1].label.startswith("Tomorrow · ")
    assert opts[-1].value == "later" and all(o.value == (today + timedelta(days=i)).isoformat() for i, o in enumerate(opts[:-1]))


def test_combine_when_is_aware_in_the_guild_zone(reg):
    dt, note = wz.combine_when(reg, {"day": "2026-12-10", "hour": "19", "minute": "30"})
    assert dt.tzinfo is not None and (dt.hour, dt.minute) == (19, 30) and note == ""
    assert "7:30 PM" in wz.both_clocks(reg, dt) and f"<t:{int(dt.timestamp())}:f>" in wz.both_clocks(reg, dt)


# ---- Discord's limits, checked offline

def test_lint_catches_what_the_api_rejects():
    async def go():
        m = discord.ui.Modal(title="x")
        m.add_item(discord.ui.Button(label="no"))
        assert any("Button inside a modal" in e for e in wz.lint(m))
        v = discord.ui.View()
        v.add_item(discord.ui.Label(text="no", component=discord.ui.Select(options=[discord.SelectOption(label="a")])))
        assert any("Label inside a view" in e for e in wz.lint(v))
        big = discord.ui.View()
        big.add_item(discord.ui.Select(options=[discord.SelectOption(label=str(i), value=str(i)) for i in range(25)]))
        assert wz.lint(big) == []
    asyncio.run(go())


def test_the_absence_form_fits_discord(reg):
    from oibot_gm.flows_absence import AbsenceWizard

    async def go():
        wiz = AbsenceWizard(reg, None, 1, 1, "Tester", officer=False)
        form = wiz.form()
        assert wz.lint(form) == [] and len(form.children) == 3
    asyncio.run(go())


# ---- the absence flow, end to end

def test_absence_wizard_end_to_end(reg, rs):
    h = Harness(reg, rs)
    m = reg.test_members()[0]
    h.user.id, h.user.display_name = m.discord_id, m.display_name
    start = reg.now_local().date() + timedelta(days=3)

    async def go():
        await FLOWS["absence"](h.command(), reg)
        assert h.modal is not None and h.modal.title == "I'll be away"
        await h.submit(start=start.isoformat(), days="3", reason="holiday")
        assert "away" in h.text and "3 days" in h.text and h.buttons() == ["Confirm", "Change", "Cancel"]
        assert reg.day_label(start.isoformat()) in h.text and start.isoformat() not in h.text  # a day a person reads, never ISO
        await h.press("Confirm")
        assert "✅" in h.text and h.screen["view"] is None
    asyncio.run(go())
    a = next(a for a in reg.members[m.discord_id].absences if a.start == start.isoformat())
    assert a.end == (start + timedelta(days=2)).isoformat() and a.reason == "holiday"
    assert h.announced and h.announced[0][1] == start.isoformat()


def test_absence_change_keeps_the_draft_and_cancel_writes_nothing(reg, rs):
    h = Harness(reg, rs)
    m = reg.test_members()[1]
    h.user.id, h.user.display_name = m.discord_id, m.display_name
    start = reg.now_local().date() + timedelta(days=5)
    before = list(reg.members[m.discord_id].absences)

    async def go():
        await FLOWS["absence"](h.command(), reg)
        await h.submit(start=start.isoformat(), days="7", reason="trip")
        await h.press("Change")  # reopens the SAME form with the picks restored
        chosen = {k: [o.value for o in it.options if o.default] for k, it in h.modal._items.items() if isinstance(it, discord.ui.Select)}
        assert chosen == {"start": [start.isoformat()], "days": ["7"]}
        assert h.modal._items["reason"].default == "trip"
        await h.submit(h.modal, start=start.isoformat(), days="7", reason="trip")
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    asyncio.run(go())
    assert reg.members[m.discord_id].absences == before


def test_absence_later_than_the_window_narrows_by_week(reg, rs):
    h = Harness(reg, rs)
    m = reg.test_members()[2]
    h.user.id, h.user.display_name = m.discord_id, m.display_name
    got = {}

    async def go():
        await FLOWS["absence"](h.command(), reg)
        await h.submit(start="later", days="2")
        assert "Which week" in h.text
        week = h._items()[0].options[3].value  # a few weeks past the window
        got["week"] = week
        await h.pick("Pick the week", week)
        assert h.modal is not None  # back to the form, now starting that week
        assert h.modal._items["start"].options[0].value == week
        await h.submit(h.modal, start=week, days="2")
        await h.press("Confirm")
    asyncio.run(go())
    assert any(a.start == got["week"] for a in reg.members[m.discord_id].absences)


def test_officer_records_an_absence_for_someone_else(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    h = Harness(reg, rs)
    target = reg.test_members()[3]
    start = reg.now_local().date() + timedelta(days=1)

    async def go():
        await FLOWS["absence"](h.command(), reg, member=type("U", (), {"id": target.discord_id, "display_name": target.display_name})())
        assert h.modal.title == f"Away · {target.display_name}"
        await h.submit(start=start.isoformat(), days="1")
        await h.press("Confirm")
        assert target.display_name in h.text
    asyncio.run(go())
    a = next(a for a in reg.members[target.discord_id].absences if a.start == start.isoformat())
    assert a.by == "Tester"


def test_officer_loses_the_role_midway_and_nothing_saves(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    state = {"officer": True}
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: state["officer"])
    h = Harness(reg, rs)
    target = reg.test_members()[4]
    before = list(reg.members[target.discord_id].absences)

    async def go():
        await FLOWS["absence"](h.command(), reg, member=type("U", (), {"id": target.discord_id, "display_name": target.display_name})())
        await h.submit(start=(reg.now_local().date() + timedelta(days=2)).isoformat(), days="1")
        state["officer"] = False
        view = h.screen["view"]
        itx = h._on_screen()
        assert not await view.interaction_check(itx)  # the guard runs on every press, not only at the start
    asyncio.run(go())
    assert reg.members[target.discord_id].absences == before


def test_someone_else_cannot_press_your_wizard(reg, rs):
    h = Harness(reg, rs)

    async def go():
        await FLOWS["absence"](h.command(), reg)
        await h.submit(start=reg.now_local().date().isoformat(), days="1")
        view = h.screen["view"]
        intruder = h._on_screen()
        intruder.user = type("U", (), {"id": 1, "display_name": "Other"})()
        assert not await view.interaction_check(intruder)
    asyncio.run(go())
