"""Roster redesign Phase 1 ("Classes + regulars", out/roster_redesign.md §7 as amended by out/roster_decisions.md):
recurring vs ad hoc, derived from the schedule's kind; the ad hoc defaults (no reminder, no confirmations, lock 2 h,
fill asks 1 h, first come); per-field provenance; every open path stamps the run's class and series.
The Bot is the real RaidMixin over a recording fake (test_parity_verbs.Bot); Registry and RaidStore are real."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import by_role, join, open_test_run
from test_analytics_cards import text_of
from test_parity_verbs import Bot, Chan

from oibot_gm import configops
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.raid_scheduler import RaidSchedulerMixin
from oibot_gm.registry import (ADHOC_DEFAULTS, ADHOC_SCHEDULE, ADHOC_WEIGHTS, RAID_WEIGHT_DEFAULTS, RegistryError, schedule_class,
                               series_key)

RID = "barrow_deeps"
ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def opened(reg, slots=("Tue 19:30",)):
    reg.set_raid_overrides(RID, {"slots": list(slots), "first_open": "2026-01-01T00:00"}, "t")


def soon(reg, days: float = 3, hour: int = 21):
    return (reg.now_local() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------- the class follows the kind

def test_class_is_derived_from_the_kind():
    assert [schedule_class(k) for k in ("weekly", "lockout", "pickup", None)] == ["recurring", "recurring", "adhoc", "recurring"]
    assert series_key(RID, None) == f"{RID}:default" and series_key(RID, "alt") == f"{RID}:alt"
    assert RAID_WEIGHT_DEFAULTS["regular"] > RAID_WEIGHT_DEFAULTS["rank"], "D9: being a regular is worth more than one rank step"


def test_adhoc_takes_the_raids_identity_but_not_its_recurring_timing(reg):
    opened(reg)
    reg.set_raid_overrides(RID, {"lock_hours_before": 24, "confirm_hours_before": 12, "nudge_hours_before": 36}, "t")
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")
    rd, pug, dflt = reg.raid_def(RID), reg.schedule_def(RID, "pug"), reg.schedule_def(RID, "default")
    assert pug["class"] == "adhoc" and dflt["class"] == "recurring"
    for k, v in ADHOC_DEFAULTS.items():
        assert pug[k] == v, k
    assert (pug["size"], pug["comp"], pug["lockout_days"], pug["duration_hours"]) == (rd["size"], rd["comp"], rd["lockout_days"], rd["duration_hours"])
    assert pug["weights"] == {**RAID_WEIGHT_DEFAULTS, **ADHOC_WEIGHTS}
    assert (dflt["lock_hours_before"], dflt["confirm"], dflt["nudge"]) == (24, True, True)
    # where each value comes from, for the editors
    assert pug["schedule"]["source"]["lock_hours_before"] == "adhoc" and dflt["schedule"]["source"]["lock_hours_before"] == "raid"
    reg.set_schedule(RID, "pug", {"lock_hours_before": 4}, "t")
    pug = reg.schedule_def(RID, "pug")
    assert pug["schedule"]["source"]["lock_hours_before"] == "own" and pug["lock_hours_before"] == 4
    assert pug["nudge_hours_before"] == 5, "the ad hoc officer card stays an hour before its own lock"


def test_the_one_off_schedule_is_virtual(reg):
    opened(reg)
    sd = reg.schedule_def(RID, ADHOC_SCHEDULE)
    assert sd["class"] == "adhoc" and sd["lock_hours_before"] == 2 and sd["confirm"] is False
    assert ADHOC_SCHEDULE not in [s["id"] for s in reg.schedules(RID)], "never listed: listings, wizards and the config dump don't change"
    with pytest.raises(RegistryError, match="kept for one-off runs"):
        reg.set_schedule(RID, ADHOC_SCHEDULE, {"kind": "pickup"}, "t")
    t = soon(reg)
    assert rc.run_key(RID, t, ADHOC_SCHEDULE).endswith("-adhoc") and rc.run_key(RID, t, ADHOC_SCHEDULE) != rc.run_key(RID, t)


def test_schedule_weights_merge_per_key_and_refuse_junk(reg):
    opened(reg)
    reg.set_schedule(RID, "default", {"weights": {"regular": 6, "rank": 0}}, "t")
    w = reg.schedule_def(RID, "default")["weights"]
    assert (w["regular"], w["rank"], w["main"]) == (6, 0, RAID_WEIGHT_DEFAULTS["main"])
    reg.set_schedule(RID, "default", {"weights": {"rank": "inherit"}}, "t")
    w = reg.schedule_def(RID, "default")["weights"]
    assert (w["regular"], w["rank"]) == (6, RAID_WEIGHT_DEFAULTS["rank"])
    assert reg.schedule_def(RID, "default")["schedule"]["source"]["weights"]["regular"] == "own"
    for bad in ({"luck": 3}, {"rank": -1}, {"rank": "lots"}):
        with pytest.raises(RegistryError):
            reg.set_schedule(RID, "default", {"weights": bad}, "t")


def test_confirm_is_a_raid_and_schedule_setting(reg):
    opened(reg)
    reg.set_schedule(RID, "default", {"confirm": "false"}, "t")
    assert reg.schedule_def(RID, "default")["confirm"] is False
    reg.set_raid_override(RID, "confirm", "false", "t")
    assert reg.raid_def(RID)["confirm"] is False


# ---------------------------------------------------------------- every open path stamps the class

def test_run_dict_and_event_carry_kind_series_and_test(reg, rs):
    opened(reg)
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    ev = rc.open_run(reg, rs, RID, t, by="t")
    assert (ev.kind, ev.series, ev.test) == ("recurring", f"{RID}:default", False) and ev.key == rc.event_key(RID, t)
    one = rc.open_run(reg, rs, RID, soon(reg, 4), by="t", schedule=ADHOC_SCHEDULE)
    team = reg.config.roster(one.team)
    assert (one.kind, one.series, one.schedule) == ("adhoc", None, ADHOC_SCHEDULE) and one.team.endswith("-adhoc")
    assert (team["reminders"], team["open_dm"], team["confirm"], team["cutoff_hard_hours"], team["fill_ask_hours"]) == ("none", False, False, 2, 1)
    test = open_test_run(reg, rs)
    assert test.test and test.kind == "recurring" and rc.is_test_run(reg, test)


def test_scheduler_opens_recurring_runs(reg, rs):
    class SchedBot(RaidSchedulerMixin, Bot):
        events: dict = {}

        def officer_channel(self, reg, ev=None):
            return None

    t = (reg.now_local() + timedelta(days=2)).replace(second=0, microsecond=0)
    opened(reg, slots=(t.strftime("%a %H:%M"),))
    reg.config.signup_channel_id = 77
    bot = SchedBot(reg, rs)
    bot.channels[77] = Chan(77)
    run(bot.scheduler_tick())
    ev = next(e for e in rs.live() if e.instance == RID)
    assert (ev.kind, ev.series) == ("recurring", f"{RID}:default") and not ev.key.split("-")[3].isalpha()


def test_web_open_stamps_the_class(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, bearer
    from test_schedules import OpeningBot

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    opened(reg)
    bot = OpeningBot(reg, rs)
    c = TestClient(web.create_app(bot))
    nxt = c.post(f"/api/raid/{RID}/open", json={}, headers=bearer())
    assert nxt.status_code == 200, nxt.text
    assert rs.events[nxt.json()["message"].split()[1]].kind == "recurring"
    when = soon(reg, 5, 22)
    one = c.post(f"/api/raid/{RID}/open", json={"when": when.strftime("%Y-%m-%d %H:%M")}, headers=bearer())
    assert bot.opened[-1][2] == ADHOC_SCHEDULE and "-adhoc-" in one.json()["message"]
    listed = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[1][1]
    c.post(f"/api/raid/{RID}/open", json={"when": listed.strftime("%Y-%m-%d %H:%M")}, headers=bearer())
    assert bot.opened[-1][2] is None, "a picked time that IS an upcoming run opens that run, not a one-off"


def test_plain_text_and_mcp_open_paths(reg, rs):
    opened(reg)
    listed = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    assert configops._open_time(reg, RID, listed.strftime("%Y-%m-%d %H:%M"))[1] is None  # the raid's own run: recurring
    start, sid = configops._open_time(reg, RID, soon(reg, 9).strftime("%Y-%m-%d %H:%M"))
    assert sid == ADHOC_SCHEDULE
    assert "-adhoc-" in configops.describe(reg, ConfigOp(op="run_open", target=RID, value=start.strftime("%Y-%m-%d %H:%M")))

    import httpx

    from oibot_gm import mcp_server as ms

    seen = []

    def handler(request):
        import json

        seen.append((request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": RID, "name": "Barrow Deeps"}]})
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token="t" * 64, transport=httpx.MockTransport(handler)))
    try:
        ms.open_run(RID, when="2030-01-08 19:30")
    finally:
        ms.use(None)
    assert seen[-1] == (f"/api/raid/{RID}/open", {"when": "2030-01-08 19:30", "schedule": ""}), "MCP opens through the site's route"


# ---------------------------------------------------------------- ad hoc behaviour

def test_adhoc_expects_nobody_and_reminds_nobody(reg, rs):
    opened(reg)
    ev = rc.open_run(reg, rs, RID, soon(reg), by="t", schedule=ADHOC_SCHEDULE)
    assert rc.expected(reg, ev) == [] and rc.not_answered(reg, ev) == [] and rc.expected_from(reg, ev) == "none"
    bot = Bot(reg, rs)
    run(bot.post_health(reg, rs, ev, None, nudge=True))
    assert not bot.dms and not ev.nudged
    body = text_of(views.health_layout(reg, rs, ev, rc.run_team(reg, ev), ico))
    assert "No answer" not in body and "no confirmations" in body
    embed, _ = views.sheet_message(reg, ev, rc.run_team(reg, ev), ico)
    assert embed.footer.text.startswith("First come, first rostered") and "confirm by" not in embed.description


def test_adhoc_without_confirmations_asks_nothing_and_expires_nothing(reg, rs):
    from conftest import lock_with_board

    opened(reg)
    ev = rc.open_run(reg, rs, RID, soon(reg), by="t", schedule=ADHOC_SCHEDULE)
    ten = by_role(reg)["tank"][:2] + by_role(reg)["healer"][:2] + (by_role(reg)["melee"] + by_role(reg)["ranged"])[:6]
    join(reg, rs, ev, ten)
    lock_with_board(reg, rs, ev, [m.display_name for m in ten])
    ev.confirm_by = None  # what lock_run stores when the run has no confirmations
    bot = Bot(reg, rs)
    assert not rc.run_confirms(reg, ev)
    sent = run(bot.send_confirmations(reg, rs, ev, rc.run_team(reg, ev), "t"))
    assert sent == 0 and not any(m.placement_asks for m in ten) and not bot.dms
    assert rc.expire_confirmations(reg, rs, ev) == [] and len(ev.seated()) == 10
    embed, _ = views.sheet_message(reg, ev, rc.run_team(reg, ev), ico)
    assert "confirm by" not in embed.description and "you're in" in embed.footer.text


def test_first_come_out_keeps_seats_for_the_minimums():
    from oibot_gm.models import Player

    def p(name, role, pos):
        return Player(signup_name=name, pos=pos, status="signed", cls="Warrior", spec="Fury", role=role, character=name, map_confidence="high", unmapped=False)

    players = [p(f"d{i}", "melee", i) for i in range(1, 9)] + [p("t1", "tank", 9), p("h1", "healer", 10), p("d9", "melee", 11)]
    late = rc.first_come_out(players, 5, {"tank": 1, "healer": 1}, {"tank": 2}, set())
    assert late == {"d4", "d5", "d6", "d7", "d8", "d9"}, "three first joiners, then the seats held for the tank and healer"
    assert rc.first_come_out(players, 20, {"tank": 1}, {}, set()) == set()


@pytest.mark.slow
def test_adhoc_lock_seats_first_come(reg, rs):
    """12 joiners for 10 seats on an ad hoc run: the two who joined last sit out, whatever their buffs."""
    import time

    opened(reg)
    ev = rc.open_run(reg, rs, RID, soon(reg), by="t", schedule=ADHOC_SCHEDULE)
    roles = by_role(reg)
    dps = roles["melee"] + roles["ranged"]
    order = roles["tank"][:2] + roles["healer"][:2] + dps[:8]
    for m in order:
        rc.set_signup(reg, rs, ev, m, None, "in")
        time.sleep(0.002)
    _, r = rc.propose(reg, rs, ev, save=False)
    seated = {p.signup_name for p in r.selected}
    assert seated == {m.display_name for m in order[:10]}


@pytest.mark.slow
def test_adhoc_lock_run_sends_no_confirmations(reg, rs):
    opened(reg)
    ev = rc.open_run(reg, rs, RID, soon(reg), by="t", schedule=ADHOC_SCHEDULE)
    roles = by_role(reg)
    join(reg, rs, ev, roles["tank"][:2] + roles["healer"][:2] + (roles["melee"] + roles["ranged"])[:6])
    bot = Bot(reg, rs)
    line = run(bot.lock_run(reg, rs, ev, by="t"))
    assert ev.state == "locked" and ev.confirm_by is None and "0 confirmation DM(s)" in line
    assert not any(a for m in reg.members.values() for a in m.placement_asks)
