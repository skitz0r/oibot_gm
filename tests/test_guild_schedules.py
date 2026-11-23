import copy

import pytest

from oibot_gm import registry as R
from oibot_gm.registry import RegistryError


def test_a_row_and_its_stored_entry_round_trip():
    row = R.norm_schedule({"id": "tue", "kind": "weekly", "slots": ["Tue 19:30"], "rosters": 2, "follow": "barrow_deeps x2", "lock_hours_before": 12})
    entry = R.to_stored_schedule("hyjal_summit_forever", row)
    assert list(entry)[:2] == ["id", "lineup"]
    assert entry["lineup"] == [{"raid": "hyjal_summit_forever", "rosters": 2}, {"raid": "barrow_deeps", "rosters": 2}]
    assert "rosters" not in entry and "follow" not in entry and entry["lock_hours_before"] == 12
    assert R.from_stored_schedule(entry) == ("hyjal_summit_forever", row)


def test_one_raid_one_roster_stores_no_extras():
    row = R.norm_schedule({"id": "alt", "kind": "weekly", "slots": ["Thu 20:00"]})
    entry = R.to_stored_schedule("barrow_deeps", row)
    assert entry["lineup"] == [{"raid": "barrow_deeps", "rosters": 1}]
    assert R.from_stored_schedule(entry) == ("barrow_deeps", row)


@pytest.mark.parametrize("bad", [
    [], "barrow_deeps",
    [{"raid": "a", "rosters": 1}, {"raid": "b", "rosters": 1}, {"raid": "c", "rosters": 1}],
    [{"rosters": 1}],
    [{"raid": "a", "rosters": 0}], [{"raid": "a", "rosters": 5}],
    [{"raid": "a", "rosters": 1, "starts": 60}],
    [{"raid": "a", "rosters": 1}, {"raid": "b", "rosters": 1, "starts": 0}],
    [{"raid": "a", "rosters": 1}, {"raid": "a", "rosters": 2}],
])
def test_a_bad_lineup_is_refused(bad):
    with pytest.raises(RegistryError):
        R.parse_lineup(bad)


def test_a_second_entry_may_carry_its_start():
    assert R.parse_lineup([{"raid": "a", "rosters": 1}, {"raid": "b", "rosters": 2, "starts": 180}])[1]["starts"] == 180


# ---- Task 2: the guild-level list is where schedules are read and written
RID = "barrow_deeps"  # as in tests/test_schedules.py
OTHER = "hyjal_summit_forever"  # a second raid of the test profile


def stored(reg, raid):
    return [e for e in reg.config.schedules if e["lineup"][0]["raid"] == raid]


def test_a_schedule_is_stored_at_guild_level(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "rosters": 2, "follow": f"{OTHER} x2"}, "t")
    assert stored(reg, RID) == [{"id": "tue", "lineup": [{"raid": RID, "rosters": 2}, {"raid": OTHER, "rosters": 2}], "kind": "weekly", "slots": ["Tue 19:30"]}]
    assert not {"schedules", "slots"} & set(reg.config.raids.get(RID) or {})
    row = next(s for s in reg.schedules(RID) if s["id"] == "tue")
    assert row["rosters"] == 2 and row["follow"] == {"raid": OTHER, "rosters": 2}


def test_run_times_are_the_default_schedule(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    assert stored(reg, RID) == [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 19:30"]}]
    assert reg.raid_def(RID)["slots"] == ["Tue 19:30"] and reg.raid_def(RID).get("schedules") in (None, [])
    assert [s["id"] for s in reg.schedules(RID)] == ["default"]


def test_default_keeps_its_own_settings_without_run_times(reg):
    reg.set_schedule(RID, "default", {"slots": "Tue 19:30", "rosters": 2}, "t")
    reg.set_raid_override(RID, "slots", "", "t")
    assert stored(reg, RID) == [{"id": "default", "lineup": [{"raid": RID, "rosters": 2}]}]
    assert reg.raid_def(RID)["slots"] == []


def test_removing_default_clears_its_run_times(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.remove_schedule(RID, "default", "t")
    assert stored(reg, RID) == [] and reg.raid_def(RID)["slots"] == []


def test_two_raids_may_share_a_schedule_id(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    reg.set_schedule(OTHER, "tue", {"kind": "weekly", "slots": "Tue 21:00"}, "t")
    assert reg.schedules(RID)[0]["slots"] == ["Tue 19:30"] and reg.schedules(OTHER)[0]["slots"] == ["Tue 21:00"]
    reg.remove_schedule(RID, "tue", "t")
    assert stored(reg, RID) == [] and len(stored(reg, OTHER)) == 1


def test_a_refused_write_restores_both_stores(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    before = (list(reg.config.schedules), dict(reg.config.raids.get(RID) or {}))
    with pytest.raises(RegistryError):
        reg.set_raid_overrides(RID, {"notes": "kept?", "schedules": [{"id": "tue", "kind": "weekly", "slots": ["Tue 19:30"], "rosters": 9}]}, "t")
    assert (list(reg.config.schedules), dict(reg.config.raids.get(RID) or {})) == before


def test_a_refused_check_after_an_accepted_field_restores_both_stores(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    before = (copy.deepcopy(reg.config.schedules), copy.deepcopy(reg.config.raids))
    with pytest.raises(RegistryError, match="used twice"):  # both fields apply; the whole-raid check refuses
        reg.set_raid_overrides(RID, {"slots": "Wed 20:00", "schedules": [{"id": "a", "slots": ["Sat 20:00"]}, {"id": "a", "kind": "pickup"}]}, "t")
    assert (reg.config.schedules, reg.config.raids) == before


def test_the_raid_rev_follows_its_own_schedules_only(reg):
    r0, o0, s0 = reg.config_rev(f"raids:{RID}"), reg.config_rev(f"raids:{OTHER}"), reg.config_rev("settings")
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    assert reg.config_rev(f"raids:{RID}") != r0
    assert reg.config_rev(f"raids:{OTHER}") == o0 and reg.config_rev("settings") == s0


def test_a_stale_raid_rev_still_refuses_a_schedule_edit(reg):
    rev = reg.config_rev(f"raids:{RID}")
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    with pytest.raises(R.ConfigChanged):
        reg.set_schedule(RID, "tue", {"slots": "Wed 19:30"}, "t", rev=rev)


def test_clearing_a_raid_clears_its_schedules(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    reg.clear_raid_override(RID, "t")
    assert stored(reg, RID) == [] and reg.schedules(RID) == []


def test_the_file_holds_schedules_at_the_top(reg):
    import yaml
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    doc = yaml.safe_load((reg.store.root / reg.key / "guild.yaml").read_text())
    assert doc["schedules"][0]["id"] == "tue" and "schedules" not in (doc.get("raids") or {}).get(RID, {})


def test_a_follow_on_start_survives_an_unrelated_edit(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "follow": f"{OTHER} x2"}, "t")
    stored(reg, RID)[0]["lineup"][1]["starts"] = 180  # nothing writes `starts` in this phase; a stored one must not be lost
    reg.set_schedule(RID, "tue", {"name": "Tuesday"}, "t")
    assert stored(reg, RID)[0]["lineup"] == [{"raid": RID, "rosters": 1}, {"raid": OTHER, "rosters": 2, "starts": 180}]
    assert stored(reg, RID)[0]["name"] == "Tuesday"


def test_a_follow_on_given_as_a_map_keeps_its_start_and_text_never_has_one():
    assert R.parse_follow({"raid": OTHER, "rosters": 2, "starts": 90}) == {"raid": OTHER, "rosters": 2, "starts": 90}
    assert R.parse_follow(f"{OTHER} x2") == {"raid": OTHER, "rosters": 2}
    for bad in (0, -5, "soon", True):
        with pytest.raises(RegistryError):
            R.parse_follow({"raid": OTHER, "starts": bad})
