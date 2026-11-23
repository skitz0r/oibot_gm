"""Phase 2, Task 2: sheet timing is written on every schedule, and a raid refuses it (C7)."""
import copy
from datetime import timedelta

import pytest

from oibot_gm import raidcycle as rc
from oibot_gm import registry as R
from oibot_gm.registry import RegistryError

from test_guild_schedules import OTHER, RID

TIMING = R.SCHEDULE_TIMING


def entry(reg, raid, sid):
    return next(e for e in reg.config.schedules if e["lineup"][0]["raid"] == raid and e["id"] == sid)


def test_a_new_schedule_is_seeded_from_the_profile(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    e = entry(reg, RID, "tue")
    assert {k: e[k] for k in TIMING} == reg.timing_suggestion(RID)
    assert e["signup_lead_hours"] == 48 and e["lock_hours_before"] == 24  # profiles/forever/raids.yaml barrow_deeps


def test_a_pickup_schedule_is_seeded_from_the_first_come_values(reg):
    reg.set_schedule(RID, "pug", {"kind": "pickup"}, "t")
    e = entry(reg, RID, "pug")
    assert {k: e[k] for k in TIMING} == {k: R.ADHOC_DEFAULTS[k] for k in TIMING}


def test_run_times_alone_seed_the_default_schedule(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    assert set(TIMING) <= set(entry(reg, RID, "default"))


def test_a_stored_value_is_kept_and_inherit_means_the_suggestion(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "confirm_hours_before": 12}, "t")
    assert entry(reg, RID, "tue")["confirm_hours_before"] == 12
    reg.set_schedule(RID, "tue", {"confirm_hours_before": "inherit"}, "t")
    assert entry(reg, RID, "tue")["confirm_hours_before"] == reg.timing_suggestion(RID)["confirm_hours_before"]


@pytest.mark.parametrize("field", R.SCHEDULE_TIMING)
def test_the_raid_refuses_timing(reg, field):
    before = (copy.deepcopy(reg.config.raids), copy.deepcopy(reg.config.schedules))
    with pytest.raises(RegistryError, match="belongs to each schedule"):
        reg.set_raid_override(RID, field, "true" if field in R.RAID_BOOL_FIELDS else 30, "t")
    assert (reg.config.raids, reg.config.schedules) == before


def test_moving_the_lock_past_the_reminder_moves_the_reminder(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "signup_lead_hours": 48, "lock_hours_before": 24, "nudge_hours_before": 30}, "t")
    reg.set_schedule(RID, "tue", {"lock_hours_before": 36}, "t")
    e = entry(reg, RID, "tue")
    assert e["lock_hours_before"] == 36 and e["nudge_hours_before"] == R.default_nudge_hours(48, 36)


def test_a_reminder_inside_the_new_window_stays(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "signup_lead_hours": 48, "lock_hours_before": 12, "nudge_hours_before": 30}, "t")
    reg.set_schedule(RID, "tue", {"lock_hours_before": 24}, "t")
    assert entry(reg, RID, "tue")["nudge_hours_before"] == 30


def test_the_follow_on_editor_still_writes_lineup_entry_two(reg):
    reg.set_schedule(OTHER, "tuesday", {"kind": "weekly", "slots": "Tue 18:30", "follow": f"{RID} x2"}, "t")
    e = entry(reg, OTHER, "tuesday")
    assert e["lineup"][1] == {"raid": RID, "rosters": 2} and set(TIMING) <= set(e)


def test_a_default_schedule_on_the_games_run_times_keeps_its_own_timing(reg, monkeypatch):
    """A raid whose run times come from the profile: a `default` row holding only timing is still its schedule."""
    monkeypatch.setitem(reg.profile.raids, RID, {**reg.profile.raids[RID], "slots": ["Tue 19:30"]})
    reg.set_schedule(RID, "default", {"confirm_hours_before": 12}, "t")
    assert entry(reg, RID, "default")["confirm_hours_before"] == 12
    assert reg.schedule_def(RID, "default")["confirm_hours_before"] == 12


def test_raid_def_is_effective_raid(reg):
    assert reg.raid_def(RID) == R.effective_raid(reg.profile.raids[RID], reg.raid_override(RID))


def test_a_one_off_still_uses_the_first_come_values(reg):
    sd = reg.schedule_def(RID, R.ADHOC_SCHEDULE)
    assert {k: sd[k] for k in TIMING} == {k: R.ADHOC_DEFAULTS[k] for k in TIMING}


def test_a_schedule_edit_never_reaches_an_open_run(reg, rs):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    start = (reg.now_local() + timedelta(days=5)).replace(hour=19, minute=30, second=0, microsecond=0)
    t = rc.ensure_run(reg, RID, start, schedule="tue")
    keep = {k: t[k] for k in ("cutoff_hard_hours", "confirm_hours", "fill_ask_hours")}
    reg.set_schedule(RID, "tue", {"lock_hours_before": 36, "confirm_hours_before": 12}, "t")
    after = reg.config.roster(t["key"])
    assert {k: after[k] for k in keep} == keep
