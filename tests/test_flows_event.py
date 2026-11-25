"""Phase 5, Task 3: /raid reschedule and /raid edit (flows_event.py) driven end to end. Reschedule picks a day and a
time from selects (15-minute steps), refuses a wall time the clocks skip, and moves through the server's box 3; Edit
event is a hub of the event's own settings in the schedule editor's words, saved through the same box with `edit`. The
client is test_raid_night's NightBot: the real RaidMixin.reschedule_event, nothing on Discord."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
from conftest import join, lock_with_board
from discord import app_commands
from test_flows_raid import officer  # noqa: F401 — the fixture
from test_raid_night import NightBot
from test_reschedule import OFFICER_ID, moved_notices, open_one_off, real_people

from oibot_gm import consequences as cq
from oibot_gm import flows_event
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS
from oibot_gm.event_view import EDIT_AFTER_LOCK
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

F = PAGE_WORDS["schedule_fields"]


def run(c):
    return asyncio.run(c)


def officer_user():
    return SimpleNamespace(id=OFFICER_ID, display_name="Officer", mention="@Officer")


def harness(reg, rs):
    bot = NightBot(reg, rs)
    return Harness(reg, rs, user=officer_user(), client=bot), bot


def wall(reg, dt) -> str:
    return dt.astimezone(reg.tz).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- /raid reschedule

def test_reschedule_picks_day_and_time_and_moves_with_the_box(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    people = real_people(reg, 3)
    join(reg, rs, first, people)
    h, bot = harness(reg, rs)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    assert cq.reschedule_pick_box(reg, rs, first)["title"] in h.text and rc.MOVE_WHEN in h.text
    assert h.buttons() == ["Pick the day and time", "Back"] and first.key not in h.text, "no run key shown"
    run(h.press("Pick the day and time"))
    minutes = [o.value for o in h.modal._items["minute"].options]
    assert minutes == ["0", "15", "30", "45"], "15-minute steps"
    day = (first.start + timedelta(days=1)).astimezone(reg.tz).date().isoformat()
    run(h.submit(day=day, hour="20", minute="15"))
    assert h.buttons() == ["Move", "Pick another time", "Back"] and h.text.startswith("**Move ")
    assert h.screen["view"].children[0].style is discord.ButtonStyle.success
    run(h.press("Move"))
    assert wall(reg, first.start) == f"{day} 20:15" and "✅" in h.text
    assert all(moved_notices(reg, p.discord_id) for p in people) and not bot.dms, "a notice, never a DM"


def test_pick_another_time_reopens_the_picker(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    run(h.press("Pick the day and time"))
    day = (first.start + timedelta(days=1)).astimezone(reg.tz).date().isoformat()
    run(h.submit(day=day, hour="20", minute="15"))
    run(h.press("Pick another time"))
    assert h.modal is not None
    run(h.submit(day=day, hour="21", minute="45"))
    run(h.press("Move"))
    assert wall(reg, first.start) == f"{day} 21:45"


def test_later_than_this_asks_for_the_week(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    run(h.press("Pick the day and time"))
    run(h.submit(day="later", hour="20", minute="0"))
    assert h.selects() == ["Pick the week"]
    week = h.screen["view"].children[0].options[1].value
    run(h.pick("Pick the week", week))
    assert h.modal is not None and h.modal._items["day"].options[0].value == week


def test_a_time_the_clocks_skip_is_refused(reg, rs, officer, monkeypatch):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    before = first.start
    h, _bot = harness(reg, rs)
    monkeypatch.setattr(reg, "now_local", lambda: datetime(2026, 3, 1, 12, 0, tzinfo=reg.tz))  # the picker's days only
    run(FLOWS["raid_reschedule"](h.command(), reg))
    run(h.press("Pick the day and time"))
    gap = next(d for d in range(1, 15) if datetime(2026, 3, d, 3, 0, tzinfo=reg.tz).utcoffset() != datetime(2026, 3, d, 1, 0, tzinfo=reg.tz).utcoffset())
    run(h.submit(day=f"2026-03-{gap:02d}", hour="2", minute="30"))
    assert rc.MOVE_GAP in h.text and h.buttons() == ["Back"], "never silently moved to 3:30"
    assert first.start == before
    run(h.press("Back"))
    assert h.buttons() == ["Pick the day and time", "Back"]


def test_another_officer_moved_it_first(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    run(h.press("Pick the day and time"))
    day = (first.start + timedelta(days=1)).astimezone(reg.tz).date().isoformat()
    run(h.submit(day=day, hour="20", minute="15"))
    other = rc.move_start(reg, f"{day} 21:00")
    rc.reschedule(reg, rs, first, start=other, by="Other")
    rs.save(first, "moved by another officer")
    run(h.press("Move"))
    assert h.text.startswith(CONFIRM_WORDS["changed"]) and wall(reg, first.start) == f"{day} 21:00", "nothing written by this press"
    assert h.buttons() == ["Move", "Pick another time", "Back"]
    run(h.press("Move"))
    assert wall(reg, first.start) == f"{day} 20:15", "the second press acts"


def test_one_event_goes_straight_in_and_none_says_how_to_open_one(reg, rs, officer):  # noqa: F811
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    assert flows_event.NO_EVENT in h.text
    a = open_one_off(reg, rs, days=3)
    b = open_one_off(reg, rs, days=5)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    sel = h.screen["view"].children[0]
    assert [o.value for o in sel.options] == [a.key, b.key], "soonest first, keys as values only"
    assert all(o.label != o.value for o in sel.options)
    run(h.pick(sel.placeholder, b.key))
    assert cq.reschedule_pick_box(reg, rs, b)["title"] in h.text


def test_pick_event_lists_first_raids_only(reg, rs, officer):  # noqa: F811
    for r in ("barrow_deeps", "onyxias_lair"):
        reg.set_raid_overrides(r, {"first_open": "2026-01-01T00:00"}, "t")
    first = open_one_off(reg, rs, days=4, lineup=[{"raid": "onyxias_lair", "rosters": 1}])
    assert len(rc.event_parts(rs, first)) == 2
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_reschedule"](h.command(), reg))
    assert cq.reschedule_pick_box(reg, rs, first)["title"] in h.text, "one event: straight in, its later raid not offered"


# ---------------------------------------------------------------- /raid edit

def pick_setting(h, field, value):
    run(h.pick("Change a setting", field))
    run(h.pick(h.selects()[0], value))


def test_edit_event_changes_lock_and_seating_through_the_edit_box(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=4)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    assert F["nudge"] in h.text and "Nudge" not in h.text
    assert h.buttons() == ["Review changes", "Back"] and h.screen["view"].children[1].disabled, "nothing to review yet"
    run(h.pick("Change a setting", "lock_hours_before"))
    assert h.selects() == [F["lock_hours_before"]]
    run(h.pick(F["lock_hours_before"], "24"))
    run(h.pick("Change a setting", "first_come"))
    run(h.pick(F["first_come"], "priority"))
    assert "→" in h.text and not h.screen["view"].children[1].disabled
    run(h.press("Review changes"))
    box = cq.reschedule_box(reg, rs, first, None, {"lock_hours_before": 24, "first_come": False}, edit=True)
    assert box["diff"] and all(d in h.text for d in box["diff"]) and h.buttons() == ["Save", "Back"]
    run(h.press("Save"))
    t = rc.run_timing(reg, first)
    assert t["lock_hours_before"] == 24 and t["first_come"] is False and "✅" in h.text


def test_a_setting_put_back_is_no_change(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=4)
    now = rc.run_timing(reg, first)["lock_hours_before"]
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    pick_setting(h, "lock_hours_before", "24")
    pick_setting(h, "lock_hours_before", str(int(now)))
    assert h.screen["view"].children[1].disabled


def test_edit_after_the_lock_offers_only_the_asks_and_the_wait(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=4)
    people = real_people(reg, 3)
    join(reg, rs, first, people)
    lock_with_board(reg, rs, first, [p.main.label for p in people])
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    sel = next(s for s in h.screen["view"].children if getattr(s, "placeholder", "") == "Change a setting")
    assert {o.value for o in sel.options} == set(EDIT_AFTER_LOCK) and PAGE_WORDS["edit_locked"] in h.text


def test_a_started_event_says_it_cant_change(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=4)
    first.starts_at = (reg.now_local() - timedelta(minutes=5)).isoformat()
    rs.save(first, "started")
    h, _bot = harness(reg, rs)
    run(flows_event.EditEventWizard(reg, rs, OFFICER_ID, first.key, title="Edit event").start(h.command()))
    assert PAGE_WORDS["edit_over"] in h.text and h.screen["view"] is None


def test_the_event_locks_while_edit_is_open(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=4)
    people = real_people(reg, 3)
    join(reg, rs, first, people)
    was = rc.run_confirms(reg, first)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    pick_setting(h, "confirm", "off" if was else "on")
    run(h.press("Review changes"))
    assert h.buttons() == ["Save", "Back"]
    lock_with_board(reg, rs, first, [p.main.label for p in people])  # the scheduler locks meanwhile
    run(h.press("Save"))
    assert h.buttons() == ["Back"] and rc.CONFIRM_LOCKED in h.text and rc.run_confirms(reg, first) is was, "nothing written"


def test_the_weights_are_one_modal_of_five_selects(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=4)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    run(h.pick("Change a setting", "weights"))
    form = h.modal
    labels = [c.text for c in form.children]
    assert labels == list(PAGE_WORDS["priority"].values()) and len(form._items) == 5
    w = dict(rc.run_timing(reg, first)["weights"])
    run(h.submit(**{**{k: str(v) for k, v in w.items()}, "rank": str((w["rank"] + 1) % 7)}))
    assert not h.screen["view"].children[1].disabled
    run(h.press("Review changes"))
    run(h.press("Save"))
    assert rc.run_timing(reg, first)["weights"]["rank"] == (w["rank"] + 1) % 7


def test_the_split_is_one_select_per_raid(reg, rs, officer):  # noqa: F811
    for r in ("barrow_deeps", "onyxias_lair"):
        reg.set_raid_overrides(r, {"first_open": "2026-01-01T00:00"}, "t")
    first = open_one_off(reg, rs, days=4, lineup=[{"raid": "onyxias_lair", "rosters": 1}])
    later = rc.event_parts(rs, first)[1]
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    run(h.pick("Change a setting", "split_policy"))
    assert len(h.selects()) == 2 and h.buttons() == ["Back"]
    other = next(p for p in ("balanced", "first", "rotation") if p != rc.run_strategy(reg, later))
    run(h.pick(h.selects()[1], other))
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Save"))
    assert rc.run_strategy(reg, later) == other


# ---------------------------------------------------------------- the commands

def test_the_commands_take_no_typed_value(reg):
    from oibot_gm.discord_raid import register_raid_commands
    from oibot_gm.discord_registry import Guilds
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    class Bot:
        raids = type("R", (), {"store": lambda self, r: None})()

    register_raid_commands(tree, Guilds.of(reg.store, reg), Ops(client), Bot())
    raid = next(c for c in tree.get_commands() if c.name == "raid")
    cmds = {c.name: c for c in raid.commands}
    assert cmds["reschedule"].parameters == [] and cmds["edit"].parameters == []
    assert cmds["reschedule"].description.startswith("Officer: ") and cmds["edit"].description.startswith("Officer: ")
