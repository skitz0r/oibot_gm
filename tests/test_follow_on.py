"""Follow-on runs (design §5.62): a second raid the same night from one signup. Config, the pair of runs, the lead's
answers on the follow-on, the lock, leaving early, cancel, the words, plain text and the routes."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join

from oibot_gm import raidcycle as rc
from oibot_gm import registry as rg
from oibot_gm.registry import RegistryError

LEAD, FOLLOW = "hyjal_summit_forever", "barrow_deeps"


def unlock_hyjal(reg) -> None:
    reg.set_raid_overrides(LEAD, {"first_open": "2026-01-01T00:00"}, "t")


# ---------------------------------------------------------------- Task 1: config

def test_parse_follow_grammar():
    assert rg.parse_follow("barrow_deeps x2") == {"raid": "barrow_deeps", "rosters": 2}
    assert rg.parse_follow("Barrow_Deeps ×2") == {"raid": "barrow_deeps", "rosters": 2}
    assert rg.parse_follow("barrow_deeps") == {"raid": "barrow_deeps", "rosters": 1}
    assert rg.parse_follow({"raid": "barrow_deeps", "rosters": "3"}) == {"raid": "barrow_deeps", "rosters": 3}
    assert rg.parse_follow(None) is None and rg.parse_follow("") is None and rg.parse_follow("none") is None
    with pytest.raises(RegistryError, match="rosters"):
        rg.parse_follow("barrow_deeps x0")
    with pytest.raises(RegistryError, match="rosters"):
        rg.parse_follow("barrow_deeps x5")
    with pytest.raises(RegistryError, match="whole number"):
        rg.parse_follow("barrow_deeps xtwo")


def test_schedule_follow_is_validated_and_read_back(reg):
    unlock_hyjal(reg)
    reg.set_schedule(LEAD, "tue", {"slots": "Tue 18:30", "name": "Tuesday", "follow": "barrow_deeps x2"}, "t")
    s = next(x for x in reg.schedules(LEAD) if x["id"] == "tue")
    assert s["follow"] == {"raid": "barrow_deeps", "rosters": 2}
    assert reg.schedule_label(LEAD, s).endswith("· then Barrow Deeps ×2")
    assert reg.follow_words(s["follow"]) == "Barrow Deeps ×2" and reg.follow_words(None) == ""
    reg.set_schedule(FOLLOW, "wed", {"slots": "Wed 18:30"}, "t")  # Barrow Deeps has no run times of its own in the profile
    assert next(x for x in reg.schedules(FOLLOW) if x["id"] == "wed")["follow"] is None  # every schedule carries the key
    with pytest.raises(RegistryError, match="unknown raid"):
        reg.set_schedule(LEAD, "tue", {"follow": "nope x2"}, "t")
    with pytest.raises(RegistryError, match="itself"):
        reg.set_schedule(LEAD, "tue", {"follow": "hyjal_summit_forever x2"}, "t")
    reg.set_schedule(LEAD, "tue", {"follow": "none"}, "t")
    assert next(x for x in reg.schedules(LEAD) if x["id"] == "tue")["follow"] is None
    with pytest.raises(RegistryError, match="kept for follow-on"):
        reg.set_schedule(LEAD, "after", {"slots": "Tue 18:30"}, "t")
