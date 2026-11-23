"""Phase 2, Task 2: sheet timing is written on every schedule, and a raid refuses it (C7)."""
import copy
from datetime import timedelta

import pytest

from oibot_gm import raidcycle as rc
from oibot_gm import registry as R
from oibot_gm.registry import RegistryError

from test_guild_schedules import OTHER, RID
from conftest import weekly

TIMING = R.SCHEDULE_TIMING


def entry(reg, raid, sid):
    return next(e for e in reg.config.schedules if e["lineup"][0]["raid"] == raid and e["id"] == sid)


def test_a_new_schedule_is_seeded_from_the_profile(reg):
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t", raid=RID)
    e = entry(reg, RID, "tue")
    assert {k: e[k] for k in TIMING} == reg.timing_suggestion(RID)
    assert e["signup_lead_hours"] == 48 and e["lock_hours_before"] == 24  # profiles/forever/raids.yaml barrow_deeps


def test_a_pickup_schedule_is_seeded_from_the_first_come_values(reg):
    reg.set_schedule("pug", {"kind": "pickup"}, "t", raid=RID)
    e = entry(reg, RID, "pug")
    assert {k: e[k] for k in TIMING} == {k: R.ADHOC_DEFAULTS[k] for k in TIMING}


def test_run_times_alone_seed_their_schedule(reg):
    sid = weekly(reg, RID, "Tue 19:30")
    assert set(TIMING) <= set(entry(reg, RID, sid))


def test_a_stored_value_is_kept_and_inherit_means_the_suggestion(reg):
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30", "confirm_hours_before": 12}, "t", raid=RID)
    assert entry(reg, RID, "tue")["confirm_hours_before"] == 12
    reg.set_schedule("tue", {"confirm_hours_before": "inherit"}, "t")
    assert entry(reg, RID, "tue")["confirm_hours_before"] == reg.timing_suggestion(RID)["confirm_hours_before"]


@pytest.mark.parametrize("field", R.SCHEDULE_TIMING)
def test_the_raid_refuses_timing(reg, field):
    before = (copy.deepcopy(reg.config.raids), copy.deepcopy(reg.config.schedules))
    with pytest.raises(RegistryError, match="belongs to each schedule"):
        reg.set_raid_override(RID, field, "true" if field in R.RAID_BOOL_FIELDS else 30, "t")
    assert (reg.config.raids, reg.config.schedules) == before


def test_moving_the_lock_past_the_reminder_moves_the_reminder(reg):
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30", "signup_lead_hours": 48, "lock_hours_before": 24, "nudge_hours_before": 30}, "t", raid=RID)
    reg.set_schedule("tue", {"lock_hours_before": 36}, "t")
    e = entry(reg, RID, "tue")
    assert e["lock_hours_before"] == 36 and e["nudge_hours_before"] == R.default_nudge_hours(48, 36)


def test_a_reminder_inside_the_new_window_stays(reg):
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30", "signup_lead_hours": 48, "lock_hours_before": 12, "nudge_hours_before": 30}, "t", raid=RID)
    reg.set_schedule("tue", {"lock_hours_before": 24}, "t")
    assert entry(reg, RID, "tue")["nudge_hours_before"] == 30


def test_the_follow_on_editor_still_writes_lineup_entry_two(reg):
    reg.set_schedule("tuesday", {"kind": "weekly", "slots": "Tue 18:30", "follow": f"{RID} x2"}, "t", raid=OTHER)
    e = entry(reg, OTHER, "tuesday")
    assert e["lineup"][1] == {"raid": RID, "rosters": 2} and set(TIMING) <= set(e)


def test_raid_def_is_effective_raid(reg):
    assert reg.raid_def(RID) == R.effective_raid(reg.profile.raids[RID], reg.raid_override(RID))


def test_a_one_off_still_uses_the_first_come_values(reg):
    sd = reg.schedule_def(RID, R.ADHOC_SCHEDULE)
    assert {k: sd[k] for k in TIMING} == {k: R.ADHOC_DEFAULTS[k] for k in TIMING}


def test_a_schedule_edit_never_reaches_an_open_run(reg, rs):
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t", raid=RID)
    start = (reg.now_local() + timedelta(days=5)).replace(hour=19, minute=30, second=0, microsecond=0)
    t = rc.ensure_run(reg, RID, start, schedule="tue")
    keep = {k: t[k] for k in ("cutoff_hard_hours", "confirm_hours", "fill_ask_hours")}
    reg.set_schedule("tue", {"lock_hours_before": 36, "confirm_hours_before": 12}, "t")
    after = reg.config.roster(t["key"])
    assert {k: after[k] for k in keep} == keep


# ---- Task 3: an older file's raid-level timing moves onto its schedules at start
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from oibot_gm.store import GitStore  # noqa: E402
from test_guild_schedules import BAD_ROW, _last_subject, _start, _write_old, base_config, commit_count, open_registry  # noqa: E402,F401

OLD_TIMING = {
    "raids": {
        "hyjal_summit_forever": {"nudge_hours_before": 72},
        "barrow_deeps": {"nudge_hours_before": 36, "lockout_days": 7},
    },
    "schedules": [{"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}], "kind": "weekly", "slots": ["Tue 18:30"]}],
}


@pytest.fixture
def old_store_with(data_root):
    """phase 1's old_store, parametrised by the file: conftest's guild.yaml with `cfg` on top, committed."""
    def make(cfg: dict) -> GitStore:
        _write_old(data_root, cfg)
        return GitStore(data_root, push=False)
    return make


def test_lifting_timing_writes_it_out_and_drops_what_has_no_schedule(profile):
    cfg = R.GuildConfig(**{**base_config(), **copy.deepcopy(OLD_TIMING)})
    moved, dropped, owed = R.lift_timing(cfg, profile.raids)
    e = cfg.schedules[0]
    assert e["nudge_hours_before"] == 72 and e["signup_lead_hours"] == 120 and e["confirm"] is True
    assert cfg.raids == {"barrow_deeps": {"lockout_days": 7}}
    assert owed and moved == ["Hyjal Summit · Weekly: reminder 3 days before"]  # by its name, never its id
    assert dropped == ["Barrow Deeps: reminder 36 hours before"]
    assert R.lift_timing(cfg, profile.raids) == ([], [], False)


def test_an_old_file_reads_its_timing_at_once(old_store_with, open_registry):
    reg = open_registry(old_store_with(OLD_TIMING))
    assert reg.schedule_def("hyjal_summit_forever", "tuesday")["nudge_hours_before"] == 72
    assert reg.timing_dropped and not any(k in (reg.config.raids.get("barrow_deeps") or {}) for k in R.SCHEDULE_TIMING)


def test_the_timing_lift_is_committed_once(old_store_with, open_registry):
    store = old_store_with(OLD_TIMING)
    reg = open_registry(store)
    n0 = commit_count(store)
    reg.migrate_schedules()
    assert commit_count(store) == n0 + 1 and reg.timing_saved
    reg.migrate_schedules()
    assert commit_count(store) == n0 + 1 and open_registry(store).timing_dropped == []


def test_the_start_says_where_the_timing_went_once(old_store_with, open_registry):
    store = old_store_with(OLD_TIMING)
    reg = open_registry(store)
    said = _start(reg) + _start(reg)
    assert said == [("info", "Sheet timing now belongs to each schedule. Moved: Hyjal Summit · Weekly: reminder 3 days before. "
                             "Dropped, with no schedule to take it: Barrow Deeps: reminder 36 hours before.")]
    assert "sheet timing moved onto each schedule (by " in _last_subject(store.root)


RICH = {  # an older file: phase-1 schedules, raid-level timing of every sort, a pickup and a schedule with its own opening
    "raids": {
        "hyjal_summit_forever": {"nudge_hours_before": 72, "confirm": False, "fill_ask_hours": 2},
        "barrow_deeps": {"lock_hours_before": 12, "autofill": False, "open_dm": True, "notes": "kept"},
    },
    "schedules": [
        {"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}, {"raid": "barrow_deeps", "rosters": 2}], "kind": "weekly", "slots": ["Tue 18:30"]},
        {"id": "early", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}], "kind": "weekly", "slots": ["Sat 18:30"], "signup_lead_hours": 48},
        {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "slots": ["Tue 04:00"]},
        {"id": "pug", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "kind": "pickup"},
    ],
}


def test_every_schedule_reads_the_same_before_and_after(old_store_with, open_registry, profile):
    """The old merge, computed by hand from the old file, equals the new read for every key of SCHEDULE_TIMING."""
    old = R.GuildConfig(**{**base_config(), **copy.deepcopy(RICH)})
    before = {}
    for e in old.schedules:
        rid, row = R.from_stored_schedule(e)
        rd = R.effective_raid(profile.raids[rid], R._raid_override_of(old, rid))  # the old raid_def: the raid's timing in the merge
        sd = R.schedule_def_of(rd, row["id"])
        before[(rid, row["id"])] = {k: sd[k] for k in TIMING}
    reg = open_registry(old_store_with(RICH))
    for (rid, sid), want in before.items():
        sd = reg.schedule_def(rid, reg.config.schedule_aliases[f"{rid}:{sid}"])  # phase 3: `default` has an id of its own
        assert {k: sd[k] for k in TIMING} == want, (rid, sid)
    assert before[("hyjal_summit_forever", "tuesday")]["nudge_hours_before"] == 72
    assert before[("barrow_deeps", "default")]["lock_hours_before"] == 12
    assert not any(k in o for o in reg.config.raids.values() for k in TIMING)
    assert reg.config.raids["barrow_deeps"] == {"notes": "kept"}
    assert reg.timing_moved == [
        "Hyjal Summit · Weekly: reminder 3 days before, a fill ask waits 2 hours, confirmations off",
        "Hyjal Summit · Weekly: a fill ask waits 2 hours, confirmations off",  # `early`: its reminder is halfway to its own opening
        "Barrow Deeps · Raid nights: locks 12 hours before, automatic fill off, opening DM on",
    ]  # the pickup read the first-come values, never its raid's
    assert reg.timing_dropped == []


def _live_run(key: str, start: datetime) -> dict:
    """A run dict as ensure_run wrote it for a plain `default` run before Task 2: no fill_ask_hours (read live)."""
    return {"key": key, "name": f"Hyjal Summit {key}", "size": 20, "schedule": start.strftime("%a %H:%M"), "instance": "hyjal_summit_forever",
            "cutoff_soft_hours": 72.0, "cutoff_hard_hours": 24.0, "confirm_hours": 6.0, "open_days_before": 5.0, "reminders": "dm",
            "open_dm": False, "autofill": True, "ephemeral": True, "schedule_id": "default", "kind": "recurring",
            "series": "hyjal_summit_forever:default", "confirm": True}


def test_a_live_run_keeps_its_fill_ask_wait(old_store_with, open_registry):
    """A run dict opened from `default` without fill_ask_hours, its raid at fill_ask_hours 2 in the old file:
    after the load, the run dict has fill_ask_hours 2 and rc.ask_deadline(reg, ev, at) is at + 2 hours."""
    start = datetime(2026, 12, 15, 18, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    store = old_store_with({"raids": {"hyjal_summit_forever": {"fill_ask_hours": 2, "slots": ["Tue 18:30"]}}, "rosters": [_live_run("hsf-1215-1830", start)]})
    reg = open_registry(store)
    assert reg.config.roster("hsf-1215-1830")["fill_ask_hours"] == 2
    ev = rc.RaidEvent(key="hsf-1215-1830-x", team="hsf-1215-1830", instance="hyjal_summit_forever", starts_at=start.isoformat())
    at = start - timedelta(days=1)
    assert rc.ask_deadline(reg, ev, at) == at + timedelta(hours=2)
    reg.migrate_schedules()
    assert rc.ask_deadline(open_registry(store), ev, at) == at + timedelta(hours=2)


def test_a_one_off_and_its_follow_on_are_untouched(old_store_with, open_registry):
    """Two run dicts copied from big-guys-for's live pair (adhoc lead + `after` follow-on, both with fill_ask_hours):
    after the load and migrate_schedules, config.rosters is byte-for-byte what the file held."""
    after = {"key": "bd-1210-2130-after", "name": "Barrow Deeps · after Hyjal Summit Thu 10 Dec 6:30 PM", "size": 10, "schedule": "Thu 21:30",
             "instance": "barrow_deeps", "cutoff_soft_hours": 6.0, "cutoff_hard_hours": 5.0, "confirm_hours": 4.0, "open_days_before": 7.0,
             "reminders": "none", "open_dm": False, "autofill": True, "ephemeral": True, "schedule_id": "after", "kind": "adhoc", "series": None,
             "confirm": False, "rosters": 2, "fill_ask_hours": 1.0, "split_policy": "balanced", "follows": "hsf-1210-1830-adhoc"}
    lead = {"key": "hsf-1210-1830-adhoc", "name": "Hyjal Summit · Ad hoc Thu 10 Dec 6:30 PM", "size": 20, "schedule": "Thu 18:30",
            "instance": "hyjal_summit_forever", "cutoff_soft_hours": 3.0, "cutoff_hard_hours": 2.0, "confirm_hours": 1.0, "open_days_before": 7.0,
            "reminders": "none", "open_dm": False, "autofill": True, "ephemeral": True, "schedule_id": "adhoc", "kind": "adhoc", "series": None,
            "confirm": False, "rosters": 1, "fill_ask_hours": 1.0, "split_policy": "balanced", "follow": {"raid": "barrow_deeps", "rosters": 2},
            "followed_by": "bd-1210-2130-after"}
    held = [after, lead]
    store = old_store_with({**OLD_TIMING, "rosters": copy.deepcopy(held)})
    reg = open_registry(store)
    assert reg.config.rosters == held
    reg.migrate_schedules()
    assert reg.config.rosters == held and open_registry(store).config.rosters == held


def test_a_refused_raid_keeps_its_timing(old_store_with, open_registry):
    """A raid whose old `schedules` key can't be lifted (phase 1's refused case) and that holds lock_hours_before 12:
    the key stays in its override, and the raid is in neither moved nor dropped. Phase 3: no run opens from it."""
    reg = open_registry(old_store_with({"raids": {"barrow_deeps": {"lock_hours_before": 12, "slots": ["Tue 04:00"], "schedules": [BAD_ROW]}}}))
    assert reg.schedules_refused
    assert reg.config.raids["barrow_deeps"]["lock_hours_before"] == 12
    assert reg.schedules("barrow_deeps") == []
    assert not [x for x in reg.timing_moved + reg.timing_dropped if "Barrow Deeps" in x]


def test_a_preview_copy_never_commits_the_timing_lift(old_store_with, open_registry):
    """reg.shadow().migrate_schedules() writes no commit."""
    store = old_store_with(OLD_TIMING)
    reg = open_registry(store)
    n0 = commit_count(store)
    reg.shadow().migrate_schedules()
    assert commit_count(store) == n0


def test_hours_words_lives_in_constants_and_flows_config_re_exports_it():
    from oibot_gm import constants, flows_config
    assert flows_config.hours_words is constants.hours_words
    assert [constants.hours_words(h) for h in (72, 36, 168, 1, 1.5)] == ["3 days", "36 hours", "a week", "1 hour", "1.5 hours"]


@pytest.mark.parametrize("raid,words", [
    ({"lock_hours_before": "soon"}, "Sheet timing was left as it was in guild.yaml"),
    ({"comp": {"healer": {"min": "lots"}}}, "The old role limits were left as they were in guild.yaml"),
    ({"lock_hours_before": "soon", "comp": {"healer": {"min": "lots"}}}, "Sheet timing and the old role limits were left as they were in guild.yaml"),
])
def test_a_lift_that_fails_at_load_is_one_warning_at_start(old_store_with, open_registry, raid, words):
    """A timing or make-up lift that can't read a value keeps the file as it was (load_config) and the start tells the
    ops channel once, as a warning: what was left, then how to fix it — one line for the guild."""
    reg = open_registry(old_store_with({"raids": {"barrow_deeps": raid}}))
    assert reg.config.raids["barrow_deeps"] == raid  # left as it was
    said = _start(reg) + _start(reg)
    warns = [t for level, t in said if level == "warn"]
    assert len(warns) == 1 and warns[0].startswith(words)
    assert warns[0].endswith("Find it in the bot's log, correct it by hand and restart the bot to move it.")
    assert warns[0].count(". ") == 1  # two sentences


def test_the_timing_lift_skips_a_run_entry_that_is_not_a_map(profile):
    """lift_timing checks a run entry is a map before reading it: a stray value is left alone, never a crash."""
    cfg = R.GuildConfig(**{**base_config(), **copy.deepcopy(OLD_TIMING)})
    cfg.rosters = ["stray", {"key": "bd-x", "instance": "barrow_deeps"}]
    R.lift_timing(cfg, profile.raids)
    assert cfg.rosters[0] == "stray" and cfg.rosters[1]["fill_ask_hours"] > 0
