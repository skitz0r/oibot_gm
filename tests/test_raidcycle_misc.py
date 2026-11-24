"""Small pure helpers of the raid cycle."""
import pytest

from oibot_gm import raidcycle as rc


def test_solver_error_text():
    assert rc.solver_error_text(RuntimeError("solver status INFEASIBLE")) == rc.SOLVER_WORDS["infeasible"]
    assert rc.solver_error_text(RuntimeError("no roster found")) == rc.SOLVER_WORDS["infeasible"]
    assert rc.solver_error_text(RuntimeError("status UNKNOWN")) == rc.SOLVER_WORDS["time"]
    assert rc.solver_error_text(RuntimeError("time limit hit")) == rc.SOLVER_WORDS["time"]
    assert rc.solver_error_text(ValueError("boom")) == rc.SOLVER_WORDS["other"], "never an exception's own words"


def test_parse_schedule_and_the_raid_abbreviation():
    assert rc.parse_schedule("Tue 19:30") == (1, 19, 30)
    assert rc.parse_schedule("thursday 20:00") == (3, 20, 0)
    with pytest.raises(ValueError):
        rc.parse_schedule("Tuesday")
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    t = datetime(2026, 12, 9, 19, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert rc.abbr("hyjal_summit_forever") == "hsf"
    assert t.weekday() == 2  # 2026-12-09 is a Wednesday
    assert rc.next_raid_time("Wed 19:30", "America/Los_Angeles", after=t) == t + timedelta(days=7), "the same minute is not 'next'"
    assert rc.next_raid_time("Wed 19:30", "America/Los_Angeles", after=t - timedelta(days=1)) == t
    assert rc.next_raid_time("Thu 20:00", "America/Los_Angeles", after=t) == t.replace(day=10, hour=20, minute=0)
