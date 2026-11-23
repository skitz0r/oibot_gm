"""Raid definitions: profile defaults + guild overrides, the override validation, slot parsing."""
import pytest

from oibot_gm.registry import RAID_WEIGHT_DEFAULTS, RegistryError, default_nudge_hours, parse_slots
from conftest import weekly


def test_raid_def_defaults_from_profile(reg):
    rd = reg.raid_def("barrow_deeps")
    assert rd["size"] == 10 and rd["lockout_days"] == 3 and rd["duration_hours"] == 2.5
    assert rd["signup_lead_hours"] == 48 and rd["lock_hours_before"] == 24 and rd["confirm_hours_before"] == 6
    assert rd["slots"] == [] and rd["split_policy"] == "balanced"
    assert rd["nudge"] is True and rd["autofill"] is True and rd["open_dm"] is False
    assert rd["fill_ask_hours"] == 4
    assert rd["weights"] == RAID_WEIGHT_DEFAULTS
    assert rd["nudge_hours_before"] == default_nudge_hours(48, 24) == 36
    assert rd["minimums"] == {"tank": {"min": 2}, "healer": {"min": 2}} and "comp" not in rd


def test_raid_def_unknown_instance_is_all_defaults(reg):
    rd = reg.raid_def("nope")
    assert rd["slots"] == [] and rd["lockout_days"] == 7 and rd["weights"] == RAID_WEIGHT_DEFAULTS


def test_role_bounds_scale_with_size(reg):
    assert reg.role_bounds("barrow_deeps", 10) == {"tank": {"min": 2}, "healer": {"min": 2}, "dps": {"min": 0}}
    b20 = reg.role_bounds("barrow_deeps", 20)
    assert b20["tank"] == {"min": 4} and b20["healer"]["min"] == 4


def _sched(reg, sid="tue"):
    return reg.schedule_def("barrow_deeps", sid)


def test_schedule_hours_and_nudge_window(reg):
    """Sheet timing is each schedule's own (phase 2): the same rules as the raid's were, on the schedule."""
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t", raid="barrow_deeps")
    reg.set_schedule("tue", {"lock_hours_before": "12"}, "t", raid="barrow_deeps")
    assert _sched(reg)["lock_hours_before"] == 12
    # the stored reminder (36, the profile's halfway) still falls between open (48) and lock (12): it stays
    assert _sched(reg)["nudge_hours_before"] == 36
    reg.set_schedule("tue", {"nudge_hours_before": "20"}, "t", raid="barrow_deeps")
    assert _sched(reg)["nudge_hours_before"] == 20
    with pytest.raises(RegistryError, match="between signup opening and lock"):
        reg.set_schedule("tue", {"nudge_hours_before": "6"}, "t", raid="barrow_deeps")  # before the lock
    with pytest.raises(RegistryError, match="between signup opening and lock"):
        reg.set_schedule("tue", {"nudge_hours_before": "60"}, "t", raid="barrow_deeps")  # before signups open
    with pytest.raises(RegistryError, match="confirmation deadline"):
        reg.set_schedule("tue", {"confirm_hours_before": "30"}, "t", raid="barrow_deeps")
    with pytest.raises(RegistryError, match="number of hours"):
        reg.set_schedule("tue", {"fill_ask_hours": "soon"}, "t", raid="barrow_deeps")
    with pytest.raises(RegistryError, match="negative"):
        reg.set_schedule("tue", {"fill_ask_hours": "-1"}, "t", raid="barrow_deeps")
    with pytest.raises(RegistryError, match="open before they lock"):
        reg.set_schedule("tue", {"lock_hours_before": "48"}, "t", raid="barrow_deeps")
    assert _sched(reg)["lock_hours_before"] == 12 and _sched(reg)["nudge_hours_before"] == 20, "a refused change leaves the last good values"


def test_refused_raid_override_is_not_kept(reg):
    with pytest.raises(RegistryError, match="belongs to each schedule"):
        reg.set_raid_override("barrow_deeps", "nudge_hours_before", "30", "t")
    assert "barrow_deeps" not in reg.config.raids
    with pytest.raises(RegistryError, match="Set a role's minimum as a minimum: at least 3 tanks"):
        reg.set_raid_override("barrow_deeps", "tank_max", "3", "t")
    assert "barrow_deeps" not in reg.config.raids and reg.minimums("barrow_deeps")["tank"] == 2


def test_set_raid_override_bools_weights_policy(reg):
    reg.set_schedule("tue", {"kind": "weekly", "slots": "Tue 19:30", "nudge": "off", "open_dm": "yes", "autofill": "false"}, "t", raid="barrow_deeps")
    assert (_sched(reg)["nudge"], _sched(reg)["open_dm"], _sched(reg)["autofill"]) == (False, True, False)
    for field in ("nudge", "open_dm", "autofill", "confirm"):
        with pytest.raises(RegistryError, match="belongs to each schedule"):
            reg.set_raid_override("barrow_deeps", field, "off", "t")
    reg.set_raid_override("barrow_deeps", "weight_rank", "5", "t")
    assert reg.raid_def("barrow_deeps")["weights"] == {**RAID_WEIGHT_DEFAULTS, "rank": 5}
    with pytest.raises(RegistryError, match="whole number"):
        reg.set_raid_override("barrow_deeps", "weight_main", "lots", "t")
    with pytest.raises(RegistryError, match="negative"):
        reg.set_raid_override("barrow_deeps", "weight_main", "-2", "t")
    with pytest.raises(RegistryError, match="unknown raid field"):
        reg.set_raid_override("barrow_deeps", "weight_luck", "1", "t")
    with pytest.raises(RegistryError, match="split_policy"):
        reg.set_raid_override("barrow_deeps", "split_policy", "random", "t")
    reg.set_raid_override("barrow_deeps", "split_policy", "rotation", "t")
    assert reg.raid_def("barrow_deeps")["split_policy"] == "rotation"
    with pytest.raises(RegistryError, match="unknown raid"):
        reg.set_raid_override("molten_core", "nudge", "true", "t")


def test_set_raid_override_comp_and_reset(reg):
    for field in ("healer_min", "healer_max", "tank_min", "dps_min"):  # a minimum is set as a minimum (D16), never on raid_set
        with pytest.raises(RegistryError, match="Set a role's minimum as a minimum: at least 3 "):
            reg.set_raid_override("barrow_deeps", field, "3", "t")
    assert "barrow_deeps" not in reg.config.raids
    reg.set_minimums("barrow_deeps", {"healer": {"min": 3}}, "t")
    assert reg.config.raids["barrow_deeps"]["minimums"] == {"healer": {"min": 3}}
    with pytest.raises(RegistryError, match="no upper limit"):
        reg.set_minimums("barrow_deeps", {"healer": {"min": 3, "max": 4}}, "t")
    with pytest.raises(RegistryError, match="more than the 10"):
        reg.set_minimums("barrow_deeps", {"tank": {"min": 11}}, "t")
    sid = weekly(reg, "barrow_deeps", "tue 19:30, Thursday 20:00")
    assert reg.schedule_entry(sid)["slots"] == ["Tue 19:30", "Thu 20:00"] and reg.raid_def("barrow_deeps")["slots"] == []
    assert reg.clear_raid_override("barrow_deeps", "t")
    assert "barrow_deeps" not in reg.config.raids
    assert reg.minimums("barrow_deeps")["healer"] == 2 and reg.schedules("barrow_deeps") == []


def test_parse_slots():
    assert parse_slots("Tue 19:30, thu 20:00; SAT 9:05") == ["Tue 19:30", "Thu 20:00", "Sat 09:05"]
    assert parse_slots(["Wednesday 18:00"]) == ["Wed 18:00"]
    assert parse_slots("") == [] and parse_slots(None) == []
    with pytest.raises(RegistryError, match="look like"):
        parse_slots("Tuesday evening")
    with pytest.raises(RegistryError, match="look like"):
        parse_slots("Tue 25:00")
    with pytest.raises(RegistryError, match="twice"):
        parse_slots("Tue 19:30, tue 19:30")
