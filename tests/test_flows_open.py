"""Phase 5, Task 5: /raid open (flows_raid.OpenWizard) prints the server's box 11 for a schedule's night
(consequences.open_box_for, the same function the site's open routes call) and builds a one-off's lineup on
flows_event.LineupBlock, with "What this makes" in event_view.new_event_view's words as its confirmation. Runs on
test_lineups' widened TEST profile (Extra A and Extra B beside the game's raids)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from test_flows_raid import channel, harness, officer, with_slots  # noqa: F401 — the fixtures
from test_lineups import reg  # noqa: F401 — the widened profile
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import consequences as cq
from oibot_gm import event_view
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.wizard import FLOWS

RID, RAID_B = "barrow_deeps", "extra_a"
NO_CHANNEL_HERE = "No signup channel is set, so the sheet posts in this channel."
ONE_OFF_HEADING = "What this makes"  # the New event page's section title


def run(c):
    return asyncio.run(c)


def sched(reg) -> str:
    return next(s["id"] for s in reg.schedules(RID) if s["kind"] != "pickup")


def test_a_schedules_night_ahead_prints_box_11(reg, rs, officer, channel):  # noqa: F811
    with_slots(reg)
    h, bot = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press(next(b for b in h.buttons() if b.startswith("Next run"))))
    p = rc.open_preview(reg, rs, RID, None, sched(reg))  # the night the button names
    box = cq.open_box_for(reg, RID, p, here=True)  # asked in a server channel, no signup channel: it posts here
    assert box["verb"] == "Open the sheet"
    assert all(s in h.text for s in box["sentences"]) and h.buttons()[0] == box["verb"]
    assert not bot.calls, "nothing before the press"
    run(h.press(box["verb"]))
    assert bot.calls[0][:3] == ("open_run_and_post", RID, rc.open_when(reg, RID, None, sched(reg))[0])
    assert "✅ Opened" in h.text


def test_a_night_that_changed_under_the_box_is_shown_again(reg, rs, officer, channel):  # noqa: F811
    with_slots(reg)
    h, bot = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press(next(b for b in h.buttons() if b.startswith("Next run"))))
    reg.set_schedule(sched(reg), {"lock_hours_before": 6}, "t")  # its lock moved while the box was open
    run(h.press("Open the sheet"))
    assert CONFIRM_WORDS["changed"] in h.text and not bot.calls


def test_a_night_already_open_is_refused_by_its_name(reg, rs, officer, channel):  # noqa: F811
    with_slots(reg)
    first = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    ev = rc.open_run(reg, rs, RID, first, by="t")
    h, bot = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press(f"Next run — {reg.local12(first)}"))
    assert str(cq.already_open_sentence(reg, ev)) in h.text and h.buttons() == ["Back"]
    assert ev.key not in h.text and not bot.calls


def test_a_one_off_with_a_second_raid_opens_one_sheet(reg, rs, officer, channel):  # noqa: F811
    h, bot = harness(reg, rs)
    day = (reg.now_local() + timedelta(days=3)).date().isoformat()
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press("Ad hoc run"))  # no pickup template: straight to the day · hour · minute form
    assert [o.value for o in h.modal._items["minute"].options] == ["0", "15", "30", "45"]
    run(h.submit(h.modal, day=day, hour="20", minute="0"))
    assert h.selects() == ["Pick a step", "Add a raid"]
    run(h.pick("Add a raid", RAID_B))
    run(h.press("Review changes"))
    assert h.selects() == ["Locks", "Rosters"] and h.buttons() == ["Open the sheet", "Change", "Back"]
    assert not bot.calls
    assert [o.label for o in next(s for s in h._items() if getattr(s, "placeholder", None) == "Locks").options] == [
        x["words"] for x in event_view.lock_choices()]
    run(h.pick("Locks", "24"))
    assert f"**{ONE_OFF_HEADING}**\n" in h.text and NO_CHANNEL_HERE in h.text
    assert "One signup sheet: " in h.text and "Extra A" in h.text and "Priority: " in h.text
    assert h.buttons()[0] == "Open the sheet"
    run(h.press("Open the sheet"))
    call = bot.calls[0]
    assert call[:2] == ("open_run_and_post", RID) and call[4] == rc.ADHOC_SCHEDULE
    kw = call[5]
    assert [e["raid"] for e in kw["lineup"]] == [RAID_B] and kw["lock_hours"] == 24 and kw["first_rosters"] == 1
    assert bot.calls[1] == ("post_sheet", rs.events[bot.calls[1][1]].key, channel.id)  # no signup channel: posted where asked
    assert "✅ Opened" in h.text and "posted in this channel" in h.text


def test_a_one_off_on_a_schedules_night_is_that_night_and_its_box(reg, rs, officer, channel):  # noqa: F811
    with_slots(reg, slots=("Wed 20:00",))
    nxt = next(t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21) if t - reg.now_local() > timedelta(days=1))
    h, bot = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press("Ad hoc run"))
    local = nxt.astimezone(reg.tz)
    run(h.submit(h.modal, day=local.date().isoformat(), hour=str(local.hour), minute=str(local.minute)))
    p = rc.open_preview(reg, rs, RID, nxt.isoformat(), sched(reg))
    box = cq.open_box_for(reg, RID, p, here=True)
    assert all(s in h.text for s in box["sentences"]) and h.buttons()[0] == "Open the sheet"
    assert event_view.SCHEDULED_NIGHT.format(name=p["schedule_name"]) in h.text  # it says the time became that schedule's event
    run(h.press("Open the sheet"))
    assert bot.calls[0][:3] == ("open_run_and_post", RID, nxt) and bot.calls[0][4:] == (sched(reg),)


def test_a_one_off_refused_shows_its_box_and_back(reg, rs, officer, channel, monkeypatch):  # noqa: F811
    h, bot = harness(reg, rs)
    day = (reg.now_local() + timedelta(days=3)).date().isoformat()
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press("Ad hoc run"))
    run(h.submit(h.modal, day=day, hour="20", minute="0"))
    run(h.press("Review changes"))
    start = rc.open_when(reg, RID, f"{day} 20:00", rc.ADHOC_SCHEDULE)[0]
    ev = rc.open_run(reg, rs, RID, start, by="t", schedule=rc.ADHOC_SCHEDULE)  # someone opened that minute meanwhile
    run(h.press("Open the sheet"))
    assert str(cq.already_open_sentence(reg, ev)) in h.text and h.buttons() == ["Back"]
    assert not bot.calls


def test_the_open_box_is_one_function_for_both_surfaces(reg, rs, web_bot):  # noqa: F811
    with_slots(reg)
    sid = sched(reg)
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[1][1]
    p = web_bot.c.post(f"/api/raid/{RID}/open/preview", json={"start": t.isoformat(), "schedule": sid}, headers=H()).json()
    assert p["box"] == cq.open_box_for(reg, RID, rc.open_preview(reg, rs, RID, t.isoformat(), sid))
    assert p["box"]["fp"] != cq.open_now_box(reg, t, t - timedelta(hours=1), t)["fp"]


def test_no_horizon_words_left(reg, rs, officer, channel):  # noqa: F811
    h, bot = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    assert "next 21 days" not in h.text and "in the next" not in h.text
    assert "no schedule yet" in h.text and "Add a schedule" in h.buttons()
    assert not bot.calls


def test_no_slot_reason_names_the_first_opening_never_a_horizon(reg, rs):  # noqa: F811
    from oibot_gm.flows_raid import OpenWizard

    with_slots(reg)
    reg.set_raid_overrides(RID, {"first_open": (reg.now_local() + timedelta(days=400)).replace(microsecond=0).isoformat()}, "t")
    wiz = OpenWizard(reg, rs, 1)
    wiz.draft["raid"] = RID
    words = wiz.no_slot_reason()
    assert "in the next" not in words and reg.local12(reg.first_open(RID)) in words


def test_a_night_asked_in_a_dm_says_the_sheet_waits_and_posts_nothing(reg, rs, officer):  # noqa: F811
    """No `channel` fixture: the command came with no server channel, so box 11 keeps the site's sentence."""
    with_slots(reg)
    h, bot = harness(reg, rs)
    run(FLOWS["raid_open"](h.command(), reg))
    run(h.pick("Pick the raid", RID))
    run(h.press(next(b for b in h.buttons() if b.startswith("Next run"))))
    assert "No signup channel is set, so the sheet waits until one is." in h.text and NO_CHANNEL_HERE not in h.text
    run(h.press("Open the sheet"))
    assert [c[0] for c in bot.calls] == ["open_run_and_post"] and "✅ Opened" in h.text


def test_here_changes_only_the_discord_box(reg, rs):  # noqa: F811
    """The site's box (no `here`) stays the one the routes print; `here` swaps only the where-it-posts sentence."""
    with_slots(reg)
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[1][1]
    p = rc.open_preview(reg, rs, RID, t.isoformat(), sched(reg))
    site, here = cq.open_box_for(reg, RID, p), cq.open_box_for(reg, RID, p, here=True)
    assert site["sentences"][1] == "No signup channel is set, so the sheet waits until one is."
    assert here["sentences"][1] == NO_CHANNEL_HERE and here["sentences"][::2] == site["sentences"][::2]
    assert cq.open_box_for(reg, RID, {**p, "channel": True}, here=True) == cq.open_box_for(reg, RID, {**p, "channel": True})


def test_open_wizard_is_a_lineup_block_and_the_block_stands_alone():
    import ast
    import inspect

    from oibot_gm import flows_event, flows_lineup
    from oibot_gm.flows_raid import OpenWizard

    assert flows_lineup.LineupBlock in OpenWizard.__mro__ and flows_event.LineupBlock is flows_lineup.LineupBlock
    assert not hasattr(OpenWizard, "make")
    tree = ast.parse(inspect.getsource(flows_lineup))
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not names & {"flows_raid", "flows_event", "oibot_gm.flows_raid", "oibot_gm.flows_event"}
