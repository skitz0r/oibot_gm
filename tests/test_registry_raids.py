"""Raid definitions: profile defaults + guild overrides, the override validation, slot parsing."""
import pytest

from oibot_gm.registry import RAID_WEIGHT_DEFAULTS, RegistryError, default_nudge_hours, parse_slots


def test_raid_def_defaults_from_profile(reg):
    rd = reg.raid_def("barrow_deeps")
    assert rd["size"] == 10 and rd["lockout_days"] == 3 and rd["duration_hours"] == 2.5
    assert rd["signup_lead_hours"] == 48 and rd["lock_hours_before"] == 24 and rd["confirm_hours_before"] == 6
    assert rd["slots"] == [] and rd["split_policy"] == "balanced"
    assert rd["nudge"] is True and rd["autofill"] is True and rd["open_dm"] is False
    assert rd["fill_ask_hours"] == 4
    assert rd["weights"] == RAID_WEIGHT_DEFAULTS
    assert rd["nudge_hours_before"] == default_nudge_hours(48, 24) == 36
    assert rd["comp"]["tank"] == {"min": 2, "max": 2}


def test_raid_def_unknown_instance_is_all_defaults(reg):
    rd = reg.raid_def("nope")
    assert rd["slots"] == [] and rd["lockout_days"] == 7 and rd["weights"] == RAID_WEIGHT_DEFAULTS


def test_role_bounds_scale_with_size(reg):
    assert reg.role_bounds("barrow_deeps", 10) == {"tank": {"min": 2, "max": 2}, "healer": {"min": 2, "max": 3}, "dps": {"min": 5, "max": 6}}
    b20 = reg.role_bounds("barrow_deeps", 20)
    assert b20["tank"] == {"min": 4, "max": 4} and b20["healer"]["min"] == 4


def test_set_raid_override_hours_and_nudge_window(reg):
    assert reg.set_raid_override("barrow_deeps", "lock_hours_before", "12", "t") == "barrow_deeps: lock_hours_before = 12"
    assert reg.raid_def("barrow_deeps")["lock_hours_before"] == 12
    assert reg.config.raids["barrow_deeps"]["lock_hours_before"] == 12
    # the derived nudge follows: halfway between open (48) and lock (12)
    assert reg.raid_def("barrow_deeps")["nudge_hours_before"] == 30
    reg.set_raid_override("barrow_deeps", "nudge_hours_before", "20", "t")
    assert reg.raid_def("barrow_deeps")["nudge_hours_before"] == 20
    with pytest.raises(RegistryError, match="between signup opening and lock"):
        reg.set_raid_override("barrow_deeps", "nudge_hours_before", "6", "t")  # before the lock
    with pytest.raises(RegistryError, match="between signup opening and lock"):
        reg.set_raid_override("barrow_deeps", "nudge_hours_before", "60", "t")  # before signups open
    with pytest.raises(RegistryError, match="confirmation deadline"):
        reg.set_raid_override("barrow_deeps", "confirm_hours_before", "30", "t")
    with pytest.raises(RegistryError, match="number of hours"):
        reg.set_raid_override("barrow_deeps", "fill_ask_hours", "soon", "t")
    with pytest.raises(RegistryError, match="negative"):
        reg.set_raid_override("barrow_deeps", "fill_ask_hours", "-1", "t")
    with pytest.raises(RegistryError, match="open before they lock"):  # last: the refused value lingers (see test_refused_raid_override_is_not_kept)
        reg.set_raid_override("barrow_deeps", "lock_hours_before", "48", "t")


def test_refused_raid_override_is_not_kept(reg):
    reg.set_raid_override("barrow_deeps", "nudge_hours_before", "30", "t")
    with pytest.raises(RegistryError):
        reg.set_raid_override("barrow_deeps", "nudge_hours_before", "6", "t")
    assert reg.raid_def("barrow_deeps")["nudge_hours_before"] == 30, "a refused change leaves the last good value"
    with pytest.raises(RegistryError):
        reg.set_raid_override("barrow_deeps", "tank_min", "3", "t")
    assert reg.raid_def("barrow_deeps")["comp"]["tank"] == {"min": 2, "max": 2}


def test_set_raid_override_bools_weights_policy(reg):
    reg.set_raid_override("barrow_deeps", "nudge", "off", "t")
    assert reg.raid_def("barrow_deeps")["nudge"] is False
    reg.set_raid_override("barrow_deeps", "open_dm", "yes", "t")
    assert reg.raid_def("barrow_deeps")["open_dm"] is True
    reg.set_raid_override("barrow_deeps", "autofill", "false", "t")
    assert reg.raid_def("barrow_deeps")["autofill"] is False
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
    reg.set_raid_override("barrow_deeps", "healer_max", "4", "t")
    assert reg.raid_def("barrow_deeps")["comp"]["healer"] == {"min": 2, "max": 4}
    with pytest.raises(RegistryError, match="above max"):
        reg.set_raid_override("barrow_deeps", "tank_min", "3", "t")
    reg.set_raid_override("barrow_deeps", "slots", "tue 19:30, Thursday 20:00", "t")
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 19:30", "Thu 20:00"]
    assert reg.clear_raid_override("barrow_deeps", "t")
    assert "barrow_deeps" not in reg.config.raids
    assert reg.raid_def("barrow_deeps")["comp"]["healer"] == {"min": 2, "max": 3} and reg.raid_def("barrow_deeps")["slots"] == []


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
