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


# ---------------------------------------------------------------- the loot audit lines (phase 3 leftovers)

def council_on(reg, rs):
    """A locked real run with its council open, on test_loot_verbs' LootBot (it records its ops lines)."""
    from test_loot_verbs import LootBot, real_run

    ev, ms = real_run(reg, rs)
    lb = LootBot(reg, rs)
    s, _created = run(lb.open_loot(reg, rs, ev, by="Officer", by_id=1))
    lb.ops.lines.clear()
    return lb, ev, ms, s


def loot_lines(lb) -> list[str]:
    return [text for _key, _level, text in lb.ops.lines]


def test_the_companions_recorded_lines_name_the_run(reg, rs):
    from test_loot_verbs import char, scribe

    lb, ev, ms, s = council_on(reg, rs)
    run(lb.tick(reg, s, add=[{"name": "Shard of Doom", "boss": "Gatekeeper", "copies": 1}], by="Officer", by_id=1))
    (p,) = run(lb.distribute(reg, s, by="Officer"))["proposals"]
    other = next(char(m) for m in ms if char(m) != p.to)
    out = run(lb.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "m1"}, scribe()))
    assert out["result"] == "recorded_reason_pending"
    run(lb.handle_feed_event({"type": "drop", "boss": "Gatekeeper", "items": [{"id": 123456, "name": "Mystery Blade"}], "idem": "d1"}, scribe()))
    out = run(lb.handle_feed_event({"type": "loot", "recipient": char(ms[3]), "item_id": 123456, "item_name": "Mystery Blade", "idem": "l1"}, scribe()))
    assert out["result"] == "manual"
    got = [x for x in loot_lines(lb) if "recorded" in x and "in game" in x]
    assert len(got) == 2 and all(f"loot {rc.run_words(reg, ev)}: recorded" in x for x in got), got
    assert not any(ev.key in x for x in loot_lines(lb)), loot_lines(lb)


def test_a_silent_companion_names_the_run(reg, rs):
    import types

    from oibot_gm.raid_scheduler import RaidSchedulerMixin

    lb, ev, _ms, s = council_on(reg, rs)
    lb.feed = types.SimpleNamespace(companions={1: types.SimpleNamespace(character="Scribe", silent_for=600, warned=False)})
    lb.active_raid = lambda c: s
    run(RaidSchedulerMixin.companion_check(lb))
    (line,) = [x for x in loot_lines(lb) if "silent" in x]
    assert f"during {rc.run_words(reg, ev)} —" in line and ev.key not in line, line


def test_a_split_runs_council_names_its_roster(reg, rs):
    lb, ev, _ms, s = council_on(reg, rs)
    lb.events.pop(s.key)
    s.id = f"{ev.key}-r2"  # the second roster's council of a split run
    lb.events[s.key] = s
    run(lb.set_scores_only(reg, s, True, by="Officer", by_id=1))
    (line,) = [x for x in loot_lines(lb) if "scores only" in x]
    assert f"loot {rc.run_words(reg, ev)} roster 2: scores only on" in line and ev.key not in line, line


def test_a_gone_run_is_not_echoed_by_key(reg, rs, monkeypatch):
    """The site's 404 for a run key that is not live never repeats the key (random letters, the Copy rule)."""
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, FakeBot

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    c = TestClient(web.create_app(FakeBot(reg, rs)))
    r = c.get("/api/run/zz-qq7x2k", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404 and "zz-qq7x2k" not in r.text and "no live run by that name" in r.text, r.text
