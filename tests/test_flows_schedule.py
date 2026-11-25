"""Phase 5, Task 6: the schedule wizard (flows_schedule.ScheduleWizard, flow `schedule`) and the raid wizard's Save on
box 19, driven end to end through tests/wizard_harness.py (every screen linted against Discord's limits). A schedule is
saved through configops like the site: box 13 (create), 14 (save), 15 / 16 (pause / resume), 17 (remove)."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import discord
from conftest import weekly
from test_flows_raid import officer  # noqa: F401 — the fixture
from test_reschedule import OFFICER_ID

from oibot_gm import flows_config  # noqa: F401 — registers the flows
from oibot_gm import flows_schedule  # noqa: F401
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

RAID_B = "onyxias_lair"
OLD_WORDS = ("Nudge", "A run's week", "Fill seats automatically", "DM everyone on open")


def run(c):
    return asyncio.run(c)


def harness(reg, rs):
    user = SimpleNamespace(id=OFFICER_ID, display_name="Officer", mention="@Officer")
    return Harness(reg, rs, user=user)


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


def button(h, label):
    return next(b for b in h.screen["view"].children if isinstance(b, discord.ui.Button) and b.label == label)


def open_hub(h, reg, sid, raid="barrow_deeps"):
    run(FLOWS["schedule"](h.command(), reg, raid=raid))
    run(h.pick("Pick a schedule", sid))


def test_a_schedules_lineup_step_saves_through_box_14(reg, rs, officer):  # noqa: F811
    sid = weekly(reg, "barrow_deeps", "Tue 19:30")
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    assert sid not in h.text, "a schedule is named, never shown by its id"
    run(h.pick("Change", "lineup"))
    run(h.pick("Add a raid", RAID_B))
    run(h.press("Review changes"))  # the lineup block's: back to the hub
    assert "Lineup: Barrow Deeps → Barrow Deeps, then Onyxia's Lair" in h.text
    run(h.press("Review changes"))  # the hub's: the box
    assert h.buttons()[0] == "Save" and "Lineup:" in h.text and "Save Raid nights?" in h.text
    run(h.press("Save"))
    assert "✅" in h.text and h.screen["view"] is None
    assert [e["raid"] for e in reg.schedule_entry(sid)["lineup"]] == ["barrow_deeps", RAID_B]


def test_new_schedule_is_box_13_and_pause_is_box_15(reg, rs, officer):  # noqa: F811
    h = harness(reg, rs)
    run(FLOWS["schedule"](h.command(), reg, raid="barrow_deeps"))
    assert "No schedule plays Barrow Deeps yet. Add one with New schedule." in h.text
    run(h.press("New schedule"))
    run(h.pick("Repeats", "weekly"))
    run(h.submit(h.modal, name="Alt run"))
    assert "**Alt run**" in h.text and "Review changes" in h.buttons()
    assert "Pause" not in h.buttons() and "Remove" not in h.buttons(), "nothing to pause or remove before it exists"
    run(h.pick("Change", "repeats"))
    run(h.press("Add an event time"))
    assert [o.value for o in h.modal._items["minute"].options] == ["0", "15", "30", "45"]
    run(h.submit(h.modal, day="Thu", hour="20", minute="15"))
    assert "✕ Thu 8:15 PM" in h.buttons() and "Every Thursday at 8:15 PM" in h.text and "20:15" not in h.text
    run(h.press("Back"))
    run(h.press("Review changes"))
    assert "Create Alt run?" in h.text and h.buttons() == ["Create schedule", "Back"]
    run(h.press("Create schedule"))
    assert "✅" in h.text
    sid = next(s["id"] for s in reg.all_schedules() if s["name"] == "Alt run")
    assert reg.schedule_entry(sid)["slots"] == ["Thu 20:15"] and reg.schedule_entry(sid)["active"]

    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.press("Pause"))
    assert "Pause Alt run?" in h.text and h.buttons() == ["Pause", "Back"]
    assert button(h, "Pause").style == discord.ButtonStyle.success
    run(h.press("Pause"))
    assert not reg.schedule_entry(sid)["active"]
    h = harness(reg, rs)
    run(FLOWS["schedule"](h.command(), reg, raid="barrow_deeps"))
    assert "**Paused**" in h.text and "Alt run" in h.text
    run(h.pick("Pick a schedule", sid))
    assert PAGE_WORDS["paused_next"] in h.text and "Resume" in h.buttons()
    run(h.press("Resume"))
    assert "Resume Alt run?" in h.text
    run(h.press("Resume"))
    assert reg.schedule_entry(sid)["active"]


def test_a_lockout_schedule_picks_its_days_and_time(reg, rs, officer):  # noqa: F811
    h = harness(reg, rs)
    run(FLOWS["schedule"](h.command(), reg, raid="barrow_deeps"))
    run(h.press("New schedule"))
    run(h.pick("Repeats", "lockout"))
    run(h.submit(h.modal, name="Reset night"))
    run(h.pick("Change", "repeats"))
    run(h.press("Pick the days and time"))
    run(h.submit(h.modal, days="1", hour="21", minute="30"))
    assert "9:30 PM" in h.text and "21:30" not in h.text
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Create schedule"))
    s = next(s for s in reg.all_schedules() if s["name"] == "Reset night")
    assert s["kind"] == "lockout" and s["days"] == [1] and s["time"] == "21:30"


def test_run_times_add_and_drop_and_a_duplicate_is_refused(reg, rs, officer):  # noqa: F811
    sid = weekly(reg, "barrow_deeps", "Mon 20:00")
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.pick("Change", "repeats"))
    run(h.press("Add an event time"))
    run(h.submit(h.modal, day="Sat", hour="21", minute="15"))
    run(h.press("Add an event time"))
    run(h.submit(h.modal, day="Tue", hour="21", minute="15"))
    run(h.press("✕ Tue 9:15 PM"))
    run(h.press("Add an event time"))
    run(h.submit(h.modal, day="Sat", hour="21", minute="15"))
    assert "Sat 9:15 PM is already an event time." in h.text
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Save"))
    assert reg.schedule_entry(sid)["slots"] == ["Mon 20:00", "Sat 21:15"]


def test_a_refused_timing_is_the_boxs_refusal_and_the_draft_stays(reg, rs, store, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    n0 = commits(store)
    open_hub(h, reg, sid)
    run(h.pick("Change", "timing"))
    run(h.pick("Pick a setting", "nudge_hours_before"))
    run(h.pick("Signup Reminder goes out", "1"))  # after the lock: the verb refuses it
    run(h.press("Back"))
    assert "Signup Reminder goes out: 36 hours before → 1 hour before" in h.text
    run(h.press("Review changes"))
    assert "Signup Reminder must go out" in h.text and h.buttons() == ["Back"]
    run(h.press("Back"))
    assert "→ 1 hour before" in h.text, "the draft is kept"
    run(h.press("Back"))  # to the list: nothing written
    assert commits(store) == n0 and reg.schedule_entry(sid)["nudge_hours_before"] == 36


def test_a_preset_and_the_switches_save_together(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.pick("Change", "timing"))
    run(h.pick("Use a preset", "Standard"))
    run(h.press("Back"))
    run(h.pick("Change", "switches"))
    run(h.pick("Turn one on or off", "autofill=off"))
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Save"))
    s = reg.schedule_entry(sid)
    assert (s["signup_lead_hours"], s["nudge_hours_before"], s["lock_hours_before"], s["confirm_hours_before"]) == (120, 72, 24, 6)
    assert s["autofill"] is False


def test_priority_split_and_name_save_in_one_box(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.pick("Change", "weights"))
    assert set(h.modal._items) == set(PAGE_WORDS["priority"])
    run(h.submit(h.modal, rank="5", regular="raid", main="raid", sat_out="raid", signup_order="raid"))
    run(h.pick("Change", "split_policy"))
    run(h.pick(PAGE_WORDS["schedule_fields"]["split_policy"], "rotation"))
    run(h.press("Back"))
    run(h.pick("Change", "name"))
    run(h.submit(h.modal, name="Main night"))
    run(h.press("Review changes"))
    assert "Save Raid nights?" in h.text, "the box names the schedule as it is stored"
    run(h.press("Save"))
    s = reg.schedule_entry(sid)
    assert s["name"] == "Main night" and s["weights"] == {"rank": 5}
    assert reg.schedule_def("barrow_deeps", sid)["split_policy"] == "rotation"


def test_remove_is_box_17_and_red_only_if_the_box_says_so(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    assert button(h, "Remove").style == discord.ButtonStyle.secondary, "only a box's verb is red"
    run(h.press("Remove"))
    assert "Remove Raid nights?" in h.text and h.buttons() == ["Remove schedule", "Back"]
    assert button(h, "Remove schedule").style == discord.ButtonStyle.danger
    run(h.press("Back"))
    assert "Change" in h.selects(), "Back returns to the schedule"
    run(h.press("Remove"))
    run(h.press("Remove schedule"))
    assert "✅" in h.text and not any(s["id"] == sid for s in reg.all_schedules())


def test_another_officer_saved_first(reg, rs, store, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.pick("Change", "lineup"))
    run(h.pick("Add a raid", RAID_B))
    run(h.press("Review changes"))
    run(h.press("Review changes"))
    reg.set_schedule(sid, {"name": "Other"}, "t")
    n0 = commits(store)
    run(h.press("Save"))
    assert h.text.startswith(CONFIRM_WORDS["changed"]) and "Save Other?" in h.text
    assert commits(store) == n0 and [e["raid"] for e in reg.schedule_entry(sid)["lineup"]] == ["barrow_deeps"], "nothing written"
    assert "Lineup:" in h.text, "the draft is kept"
    run(h.press("Save"))  # the box shown again is the one that saves
    assert "✅" in h.text and h.screen["view"] is None
    s = reg.schedule_entry(sid)
    assert s["name"] == "Other" and [e["raid"] for e in s["lineup"]] == ["barrow_deeps", RAID_B]


def test_an_open_event_keeps_its_timing_says_the_box(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    now = reg.now_local()
    start = next(t for t in rc.next_runs_of(reg, "barrow_deeps", reg.schedule_entry(sid), n=3) if t - now >= timedelta(hours=24))
    rc.open_run(reg, rs, "barrow_deeps", start, by="t", schedule=sid)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.pick("Change", "timing"))
    run(h.pick("Pick a setting", "lock_hours_before"))
    run(h.pick("Locks", "12"))
    run(h.press("Back"))
    run(h.press("Review changes"))
    assert "already open and keeps its timing." in h.text


def test_the_words_are_the_pages(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    seen = [h.text]
    for f in ("nudge", "confirm", "autofill", "open_dm"):
        assert PAGE_WORDS["schedule_fields"][f] in h.text
    run(h.pick("Change", "switches"))
    seen.append(h.text + " ".join(o.label for s in h.screen["view"].children if isinstance(s, discord.ui.Select) for o in s.options))
    assert all(PAGE_WORDS["schedule_fields"][f] in seen[-1] for f in ("nudge", "confirm", "autofill", "open_dm"))
    run(h.press("Back"))
    run(h.pick("Change", "timing"))
    seen.append(h.text + " ".join(o.label for s in h.screen["view"].children if isinstance(s, discord.ui.Select) for o in s.options))
    assert "An ask waits" in seen[-1] and "Locks" in seen[-1] and "Confirm by" in seen[-1]
    for text in seen:
        assert not any(w in text for w in OLD_WORDS), text


def test_back_from_the_list_writes_nothing(reg, rs, store, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    n0 = commits(store)
    open_hub(h, reg, sid)
    run(h.pick("Change", "name"))
    run(h.submit(h.modal, name="Never saved"))
    run(h.press("Back"))
    assert CONFIRM_WORDS["leave_title"] in h.text and "Your one edit to Never saved is not saved yet." in h.text
    assert h.buttons() == [CONFIRM_WORDS["discard"], CONFIRM_WORDS["keep"]]
    assert button(h, CONFIRM_WORDS["discard"]).style == discord.ButtonStyle.danger
    run(h.press(CONFIRM_WORDS["keep"]))
    assert h.text.startswith("**Never saved**") and "Change" in h.selects(), "Keep editing: the hub with the draft"
    run(h.press("Back"))
    run(h.press(CONFIRM_WORDS["discard"]))
    assert "Pick a schedule" in h.selects()
    run(h.press("Back"))
    assert h.screen["view"] is None and commits(store) == n0 and reg.schedule_entry(sid)["name"] == "Raid nights"


def test_the_raid_wizards_save_is_box_19_and_its_schedules_section_hands_off(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    run(FLOWS["raid_config"](h.command(), reg))
    run(h.pick("Pick a raid", "barrow_deeps"))
    run(h.pick("Pick a section", "times"))
    assert "Pick a schedule" in h.selects() and "Raid nights" in h.text
    run(h.pick("Pick a schedule", sid))
    run(h.press("Back"))
    run(h.press("Back"))  # the list's Back: the raid's hub again
    assert "Pick a section" in h.selects()
    run(h.pick("Pick a section", "weights"))
    run(h.submit(h.modal, rank="5", main="2", sat_out="2", signup_order="1", regular="3"))
    run(h.press("Review changes"))
    assert "Save Barrow Deeps?" in h.text and h.buttons() == ["Save", "Back"]
    assert "The new priority applies at the next lock." in h.text
    run(h.press("Save"))
    assert "✅" in h.text
    assert reg.raid_def("barrow_deeps")["weights"]["rank"] == 5


def test_the_raid_wizard_clears_notes_and_a_changed_box_saves_at_its_press(reg, rs, officer):  # noqa: F811
    reg.set_raid_override("barrow_deeps", "notes", "old", "t")
    h = harness(reg, rs)
    run(FLOWS["raid_config"](h.command(), reg))
    run(h.pick("Pick a raid", "barrow_deeps"))
    run(h.pick("Pick a section", "notes"))
    run(h.submit(h.modal, notes=""))
    run(h.press("Review changes"))
    run(h.press("Save"))
    assert "✅" in h.text and not reg.raid_def("barrow_deeps").get("notes")
    h = harness(reg, rs)
    run(FLOWS["raid_config"](h.command(), reg))
    run(h.pick("Pick a raid", "barrow_deeps"))
    run(h.pick("Pick a section", "length"))
    run(h.submit(h.modal, lockout_days="5", duration_hours="4.5"))
    run(h.press("Review changes"))
    reg.set_raid_override("barrow_deeps", "notes", "someone else", "t")
    run(h.press("Save"))
    assert h.text.startswith(CONFIRM_WORDS["changed"])
    assert reg.raid_def("barrow_deeps").get("lockout_days") != 5, "nothing written at the first press"
    run(h.press("Save"))  # the box shown again saves
    assert "✅" in h.text
    rd = reg.raid_def("barrow_deeps")
    assert rd["lockout_days"] == 5 and rd["notes"] == "someone else"


def test_a_box_that_changes_in_the_gap_before_the_write_is_shown_again(reg, rs, store, officer, monkeypatch):  # noqa: F811
    """apply_ops `expect`: the write re-describes the box under config_lock; another officer's save between the press's
    check and the write is the changed box, nothing written, and its own press saves."""
    from oibot_gm import configops

    sid = weekly(reg)
    real, gap = configops.apply_ops, []

    async def racing(*a, **kw):
        if not gap:
            gap.append(True)
            reg.set_schedule(sid, {"name": "Other"}, "t")
        return await real(*a, **kw)
    monkeypatch.setattr(configops, "apply_ops", racing)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.pick("Change", "split_policy"))
    run(h.pick(PAGE_WORDS["schedule_fields"]["split_policy"], "rotation"))
    run(h.press("Back"))
    run(h.press("Review changes"))
    n0 = commits(store)
    run(h.press("Save"))
    assert h.text.startswith(CONFIRM_WORDS["changed"]) and "Save Other?" in h.text
    assert commits(store) == n0 + 1, "the other officer's save alone"
    assert reg.schedule_def("barrow_deeps", sid).get("split_policy") != "rotation"
    run(h.press("Save"))
    assert "✅" in h.text and reg.schedule_def("barrow_deeps", sid)["split_policy"] == "rotation"


def test_pause_and_remove_with_unsaved_edits_ask_first(reg, rs, store, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    n0 = commits(store)
    open_hub(h, reg, sid)
    run(h.pick("Change", "name"))
    run(h.submit(h.modal, name="Draft name"))
    run(h.press("Pause"))
    assert CONFIRM_WORDS["leave_title"] in h.text and commits(store) == n0
    run(h.press(CONFIRM_WORDS["keep"]))
    assert "Pause" in h.buttons() and "Draft name" in h.text
    run(h.press("Remove"))
    assert CONFIRM_WORDS["leave_title"] in h.text
    run(h.press(CONFIRM_WORDS["discard"]))
    assert "Remove Raid nights?" in h.text, "the edits dropped, then Remove's own box"
    assert commits(store) == n0 and reg.schedule_entry(sid)["name"] == "Raid nights"


def test_remove_carries_the_schedules_rev(reg, rs, store, officer):  # noqa: F811
    sid = weekly(reg)
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    run(h.press("Remove"))
    reg.set_schedule(sid, {"name": "Other"}, "t")
    n0 = commits(store)
    run(h.press("Remove schedule"))
    assert h.text.startswith(CONFIRM_WORDS["changed"]) and "Remove Other?" in h.text and commits(store) == n0
    run(h.press("Remove schedule"))
    assert "✅" in h.text and not any(s["id"] == sid for s in reg.all_schedules())


def test_pause_on_a_schedule_removed_meanwhile_returns_to_the_list(reg, rs, officer):  # noqa: F811
    sid = weekly(reg)
    keep = weekly(reg, "barrow_deeps", "Thu 20:00")
    h = harness(reg, rs)
    open_hub(h, reg, sid)
    reg.remove_schedule(sid, "t")
    run(h.press("Pause"))
    assert "Pick a schedule" in h.selects() and h.screen["view"] is not None
    assert reg.schedule_entry(keep)


def test_the_lists_text_is_paged_with_its_select(reg, rs, officer):  # noqa: F811
    from oibot_gm.registry import schedule_name

    for n in range(27):
        reg.set_schedule(f"s{n:02d}", {"slots": f"Mon {n % 24:02d}:00", "name": f"Night {n:02d}"}, "t", raid="barrow_deeps")
    rows = [s for s in reg.all_schedules() if s["raid"] == "barrow_deeps"]
    h = harness(reg, rs)
    run(FLOWS["schedule"](h.command(), reg, raid="barrow_deeps"))
    first = [o.label for s in h.screen["view"].children if isinstance(s, discord.ui.Select) for o in s.options]
    assert len(first) == 25 and all(f"• {name} ·" in h.text for name in first)
    rest = [schedule_name(s) for s in rows if schedule_name(s) not in first]
    assert rest and not any(f"• {name} ·" in h.text for name in rest)
    run(h.press("More →"))
    assert all(f"• {name} ·" in h.text for name in rest)


def test_a_new_pickup_sends_only_what_its_screen_shows():
    from oibot_gm.flows_schedule import PICKUP_HIDDEN, ScheduleWizard

    class R:  # the few Registry reads seed_new / changed make
        profile = SimpleNamespace(raids={"barrow_deeps": {}})

        def all_schedules(self):
            return []
    wiz = ScheduleWizard.__new__(ScheduleWizard)
    wiz.reg = R()
    wiz.seed_new("barrow_deeps", "pickup", "Pickup")
    assert not PICKUP_HIDDEN & set(wiz.changed())
