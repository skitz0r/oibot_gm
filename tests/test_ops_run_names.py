"""Phase 3 final review: a run's key is random letters since §5.67 phase 3, so an officer-channel or audit line names the
run by its raid and 12-hour time — never by its key (the Copy rule: the ops channel is read by people). Drives the lock
that fails, a cancel with its later raids, a move, a lineup edit, and the scheduler's open and lock."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import OWNER, join, open_test_run
from test_lineups import BD, H, bot, reg, start_in  # noqa: F401 — the wide profile's fixtures

from oibot_gm import raidcycle as rc
from oibot_gm import raid_views as rv


def run(coro):
    return asyncio.run(coro)


def lines(bot) -> list[str]:
    return [text for _level, text in bot.ops.lines]


def assert_no_key(bot, *events) -> None:
    """Every ops / audit line written names none of these runs by key, and at least one line was written."""
    got = lines(bot)
    assert got, "no ops line at all: the test proves nothing"
    for line in got:
        for ev in events:
            assert ev.key not in line, line


def two_raid_run(reg, rs):
    return rc.open_run(reg, rs, H, start_in(reg, 3), by="t", schedule=rc.ADHOC_SCHEDULE,
                       lineup=[{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])


def test_a_failed_lock_names_the_run(bot, reg, rs, monkeypatch):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:10])

    def boom(*a, **k):
        raise RuntimeError("no roster fits")

    monkeypatch.setattr(rc, "propose", boom)
    run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert any("lock failed" in x and rv.run_title(reg, ev) in x for x in lines(bot)), lines(bot)
    assert_no_key(bot, ev)


def test_a_cancel_names_the_run_and_its_later_raids(bot, reg, rs):
    first = two_raid_run(reg, rs)
    parts = rc.event_parts(rs, first)
    run(bot.cancel_run(reg, rs, first, "Jon", "server down", by_id=OWNER))
    line = next(x for x in lines(bot) if "cancelled" in x)
    assert rv.run_title(reg, first) in line and all(rv.run_title(reg, p) in line for p in parts[1:]), line
    assert_no_key(bot, *parts)


def test_a_move_and_a_lineup_edit_name_the_run(bot, reg, rs):
    first = two_raid_run(reg, rs)
    parts = rc.event_parts(rs, first)
    run(bot.reschedule_event(reg, rs, first, start=first.start + timedelta(days=1), by="Jon", by_id=OWNER))
    assert any(x.endswith(f"moved Hyjal Summit from {reg.local12(first.start - timedelta(days=1))} to {reg.local12(first.start)}")
               for x in lines(bot)), lines(bot)
    run(bot.edit_lineup(reg, rs, first, [{"raid": H, "rosters": 1}, {"raid": "extra_b", "rosters": 1}], by="Jon", by_id=OWNER))
    line = next(x for x in lines(bot) if "lineup" in x)
    assert "removing " in line and rv.run_title(reg, parts[1]) in line, line
    assert_no_key(bot, *rc.event_parts(rs, first), *parts)


def test_the_schedulers_open_and_lock_lines_name_the_run(bot, reg, rs, monkeypatch):
    reg.config.signup_channel_id = bot.signup.id
    ev = open_test_run(reg, rs, soft=90, hard=80)  # its lock is due now
    join(reg, rs, ev, reg.test_members()[:10])
    run(bot.scheduler_tick_guild(reg))
    assert ev.state == "locked", ev.log
    other = open_test_run(reg, rs, days=4, soft=110, hard=100)

    def boom(*a, **k):
        raise RuntimeError("no roster fits")

    monkeypatch.setattr(rc, "propose", boom)
    run(bot.scheduler_tick_guild(reg))
    assert any("lock failed" in x for x in lines(bot)), lines(bot)
    assert_no_key(bot, ev, other)
