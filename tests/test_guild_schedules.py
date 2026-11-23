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
