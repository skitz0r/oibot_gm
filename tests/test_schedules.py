"""Schedules (design.md §5.27): WHEN a raid runs as entries under the raid — weekly, day N of each lockout, pickup
templates — each with its own cadence and the rosters a run expects. Back-compat first: a raid with only `slots` is the
implicit `default` schedule and behaves exactly as before. Then the registry's refusals, the cycle (keys, cutoffs,
health against N rosters), the /gm config Schedules section, plain-text ops, the web API and the MCP tools."""
from __future__ import annotations

import asyncio
import copy
import subprocess
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import by_role, join
from fastapi.testclient import TestClient
from test_flows_config import open_raid, submit_multi
from test_mcp import TOKEN, FakeBot, bearer
from wizard_harness import Harness

from oibot_gm import configops
from oibot_gm import flows_config  # noqa: F401 — registers the flows
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError, schedules_of
from oibot_gm.web import app as web

LA = ZoneInfo("America/Los_Angeles")
RID = "barrow_deeps"  # 10-player, 3-day lockout, first opens 2026-12-09 3:00 PM PST (profiles/forever/raids.yaml)


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


def opened(reg, fo: str = "2026-01-01T00:00") -> None:
    """The raid opened in the past, so weekly runs start from now."""
    reg.set_raid_override(RID, "first_open", fo, "t")


@pytest.fixture
def owner(monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)


# ---------------------------------------------------------------- back-compat

def test_a_raid_with_only_slots_is_the_default_schedule_and_nothing_changes(reg, rs):
    opened(reg)
    reg.set_raid_override(RID, "slots", "Tue 19:30, Thu 20:00", "t")
    scheds = reg.schedules(RID)
    assert [(s["id"], s["kind"], s["slots"]) for s in scheds] == [("default", "weekly", ["Tue 19:30", "Thu 20:00"])]
    now = reg.now_local()
    old = rc.slot_starts(reg, RID, now, 24 * 21)
    new = rc.upcoming(reg, RID, now, 24 * 21)
    assert [t for _s, t in old] == [t for _s, t in new] and all(s["id"] == "default" for s, _t in new)
    t = old[0][1]
    assert rc.run_key(RID, t) == rc.run_key(RID, t, "default") == f"bd-{t.strftime('%m%d-%H%M')}"
    assert rc.event_key(RID, t, "default") == f"bd-{t.strftime('%m%d-%H%M')}-{t.date().isoformat()}"
    assert rc.schedule_kw("default") == {} and rc.schedule_kw(None) == {}
    ev = rc.open_run(reg, rs, RID, t, by="t")
    team = reg.config.roster(ev.team)
    assert ev.key == rc.event_key(RID, t) and ev.schedule is None
    assert "schedule_id" not in team and "rosters" not in team  # the run's roster dict is exactly what it was
    assert rc.run_rosters(reg, ev) == 1 and reg.config.raids[RID].get("schedules") is None
    # the scheduler's own view (each schedule within its signup lead) is the old slot_starts at the raid's lead
    lead = float(reg.raid_def(RID)["signup_lead_hours"])
    assert [t for _s, t in rc.upcoming(reg, RID, now, lead=True)] == [t for _s, t in rc.slot_starts(reg, RID, now, lead)]


def test_two_weekly_schedules_coexist_with_different_lock_hours(reg, rs):
    opened(reg)
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00, Tue 19:30", "name": "Alt run", "lock_hours_before": 12}, "t")
    main, alt = reg.schedules(RID)
    assert (main["id"], alt["id"], alt["name"]) == ("default", "alt", "Alt run")
    assert reg.schedule_def(RID, "default")["lock_hours_before"] == 24 and reg.schedule_def(RID, "alt")["lock_hours_before"] == 12
    ups = rc.upcoming(reg, RID, reg.now_local(), 24 * 14)
    assert {s["id"] for s, _t in ups} == {"default", "alt"}
    tue = next(t for s, t in ups if s["id"] == "default")
    assert (tue, "alt") in {(t, s["id"]) for s, t in ups}  # both schedules start that same minute …
    a = rc.open_run(reg, rs, RID, tue, by="t")
    b = rc.open_run(reg, rs, RID, tue, by="t", schedule="alt")
    assert a.key != b.key and b.key == rc.event_key(RID, tue, "alt") and b.key.startswith(rc.run_key(RID, tue) + "-alt-")  # … as two runs
    ta, tb = reg.config.roster(a.team), reg.config.roster(b.team)
    assert (ta["cutoff_hard_hours"], tb["cutoff_hard_hours"]) == (24, 12)
    assert b.schedule == "alt" and tb["schedule_id"] == "alt" and "Alt run" in tb["name"]


def test_a_lockout_schedule_follows_a_3_day_reset_across_dst(reg):
    """Day 1 of each 3-day lockout at 8:00 PM: every third day from the first opening, always 8:00 PM on the wall clock,
    through the March 2027 spring-forward; a time the clocks skip moves past the gap."""
    reg.set_schedule(RID, "reset", {"kind": "lockout", "days": "1", "time": "20:00", "name": "Reset night"}, "t")
    s = next(x for x in reg.schedules(RID) if x["id"] == "reset")
    assert reg.schedule_label(RID, s) == "Day 1 of each 3-day lockout, 8:00 PM"
    now = datetime(2027, 3, 1, 12, 0, tzinfo=LA)
    starts = rc.schedule_starts(reg, RID, s, now, 24 * 30)
    first = date(2026, 12, 9)
    assert starts and all(t.astimezone(LA).hour == 20 and t.astimezone(LA).minute == 0 for t in starts)
    assert all((t.astimezone(LA).date() - first).days % 3 == 0 for t in starts)
    offsets = {t.utcoffset() for t in starts}
    assert offsets == {timedelta(hours=-8), timedelta(hours=-7)}  # both sides of 2027-03-14
    # days 1 and 3 (a 3-day lockout): day 3 of the window starting Fri 12 Mar is Sun 14 Mar, the spring-forward day
    reg.set_schedule(RID, "reset", {"days": "1, 3", "time": "02:30"}, "t")
    s = next(x for x in reg.schedules(RID) if x["id"] == "reset")
    got = rc.schedule_starts(reg, RID, s, datetime(2027, 3, 12, 0, 0, tzinfo=LA), 72)
    gap = next(t for t in got if t.astimezone(LA).date() == date(2027, 3, 14))
    assert (gap.astimezone(LA).hour, gap.astimezone(LA).minute, gap.utcoffset()) == (3, 30, timedelta(hours=-7))
    # before the raid opens the first runs are its first lockouts, never earlier
    early = rc.next_runs_of(reg, RID, {**s, "days": [1], "time": "20:00"}, now=datetime(2026, 10, 1, tzinfo=LA))
    assert [t.astimezone(LA).date() for t in early] == [date(2026, 12, 9), date(2026, 12, 12), date(2026, 12, 15)]


def test_a_pickup_template_never_opens_by_itself(reg, rs):
    opened(reg)
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup", "rosters": 2, "lock_hours_before": 2, "confirm_hours_before": 1,
                                  "nudge_hours_before": 3, "signup_lead_hours": 6}, "t")
    assert not [s for s, _t in rc.upcoming(reg, RID, reg.now_local(), 24 * 30) if s["id"] == "pug"]
    assert not [s for s, _t in rc.upcoming(reg, RID, reg.now_local(), lead=True) if s["id"] == "pug"]
    start = (reg.now_local() + timedelta(hours=30)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, RID, start, by="officer", schedule="pug")  # on demand: its own settings
    team = reg.config.roster(ev.team)
    assert ev.schedule == "pug" and team["rosters"] == 2 and team["cutoff_hard_hours"] == 2 and ev.key.endswith(f"-pug-{start.date().isoformat()}")


def test_rosters_2_doubles_the_advertised_seats_and_the_health_target(reg, rs):
    from oibot_gm.raid_views import sheet_message

    opened(reg)
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.set_schedule(RID, "default", {"rosters": 2}, "t")
    t = rc.slot_starts(reg, RID, reg.now_local(), 24 * 14)[0][1]
    ev = rc.open_run(reg, rs, RID, t, by="t")
    team = rc.run_team(reg, ev)
    assert rc.run_rosters(reg, ev) == 2 and ev.key == rc.event_key(RID, t)  # the default schedule keeps its key
    roles = by_role(reg)
    join(reg, rs, ev, roles["tank"][:1] + roles["healer"][:2] + roles["melee"][:3])
    h = rc.health_data(reg, ev, team)
    bounds = reg.role_bounds(RID, 10)
    assert h["headcount"][1] == 20 and h["rosters"] == 2
    need = {r["role"]: r["need"] for r in h["roles"]}
    assert need["tank"] == 2 * bounds["tank"]["min"] and need["healer"] == 2 * bounds["healer"]["min"]
    nd = rc.needs(reg, ev, team)
    assert nd["headcount"] == 20 - 6 and nd["roles"]["tank"] == 2 * bounds["tank"]["min"] - 1
    embed, _view = sheet_message(reg, ev, team, lambda kind, key: "")
    assert "/ 20" in embed.description and "2 runs" in embed.description
    players = rc.players_for(reg, ev)
    assert rc.how_many_rosters(reg, players, 10, bounds) == 1  # bodies alone: one run
    capable = rc.how_many_rosters(reg, players, 10, bounds, want=2)
    assert capable == 1  # one tank can't be two rosters' worth: bounded by the role minimums


def test_rosters_2_aims_the_lock_at_two_when_roles_allow(reg, rs):
    opened(reg)
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.set_schedule(RID, "default", {"rosters": 2}, "t")
    reg.set_raid_overrides(RID, {"tank_min": 1, "healer_min": 1}, "t")
    t = rc.slot_starts(reg, RID, reg.now_local(), 24 * 14)[0][1]
    ev = rc.open_run(reg, rs, RID, t, by="t")
    roles = by_role(reg)
    join(reg, rs, ev, roles["tank"][:2] + roles["healer"][:2] + roles["melee"][:4])  # 8 bodies: one run by headcount
    players = rc.players_for(reg, ev)
    assert rc.how_many_rosters(reg, players, 10, reg.role_bounds(RID, 10)) == 1
    assert rc.how_many_rosters(reg, players, 10, reg.role_bounds(RID, 10), want=rc.run_rosters(reg, ev)) == 2


# ---------------------------------------------------------------- the registry refuses, and leaves config alone

@pytest.mark.parametrize("sid, fields, why", [
    ("alt", {"kind": "weekly"}, "at least one run time"),
    ("alt", {"slots": "Sat 20:00", "lock_hours_before": 200}, "open before they lock"),
    ("alt", {"slots": "Sat 20:00", "lock_hours_before": 2, "confirm_hours_before": 6}, "confirmation deadline"),
    ("alt", {"slots": "Sat 20:00", "rosters": 5}, "1 to 4 rosters"),
    ("reset", {"kind": "lockout", "days": "4", "time": "20:00"}, "past the end of a 3-day lockout"),
    ("reset", {"kind": "lockout", "days": "1"}, "day(s) and a start time"),
    ("alt", {"name": "Alt"}, "start it with its kind"),
    ("Bad Id!", {"slots": "Sat 20:00"}, "short key"),
    ("alt", {"slots": "Saturday at 8"}, "should look like"),
    ("alt", {"slots": "Sat 20:00", "colour": "red"}, "unknown schedule setting"),
    ("default", {"kind": "pickup"}, "the raid's own weekly run times"),
])
def test_refusals_leave_config_and_git_untouched(reg, store, sid, fields, why):
    reg.set_schedule(RID, "keep", {"slots": "Mon 20:00"}, "t")
    before, n0 = copy.deepcopy(reg.config.raids), commits(store)
    with pytest.raises(RegistryError, match=why.replace("(", r"\(").replace(")", r"\)")):
        reg.set_schedule(RID, sid, fields, "t")
    assert reg.config.raids == before and commits(store) == n0


def test_duplicate_ids_and_a_lockout_without_first_open_are_refused(reg, store):
    with pytest.raises(RegistryError, match="used twice"):
        reg.set_raid_overrides(RID, {"schedules": [{"id": "a", "slots": ["Sat 20:00"]}, {"id": "a", "kind": "pickup"}]}, "t")
    reg.set_schedule(RID, "reset", {"kind": "lockout", "days": "1", "time": "20:00"}, "t")
    with pytest.raises(RegistryError, match="first opening"):  # a raid with no anchor can't count lockout days
        reg._check_schedules({**reg.raid_def(RID), "first_open": None})


def test_set_and_remove_are_one_commit_each_and_inherit_goes_back_to_the_raid(reg, store):
    n0 = commits(store)
    lines = reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run", "lock_hours_before": 12}, "t")
    assert commits(store) == n0 + 1 and lines == ["Barrow Deeps · Alt run: new schedule, Saturdays 8:00 PM"]
    reg.set_schedule(RID, "alt", {"lock_hours_before": "inherit", "rosters": 2}, "t")
    s = next(x for x in reg.schedules(RID) if x["id"] == "alt")
    assert "lock_hours_before" not in s and reg.schedule_def(RID, "alt")["lock_hours_before"] == reg.raid_def(RID)["lock_hours_before"]
    assert reg.schedule_label(RID, s) == "Saturdays 8:00 PM · 2 rosters"
    assert reg.remove_schedule(RID, "alt", "t") == "Barrow Deeps: schedule Alt run removed"
    assert commits(store) == n0 + 3 and reg.schedules(RID) == [] and "schedules" not in reg.config.raids[RID]
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.remove_schedule(RID, "default", "t")  # the default schedule is the raid's slots
    assert reg.raid_def(RID)["slots"] == [] and reg.schedules(RID) == []
    with pytest.raises(RegistryError, match="has no schedule"):
        reg.remove_schedule(RID, "nope", "t")


def test_schedule_labels_are_words_never_24_hour(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30, Thu 19:30, Sat 21:00", "t")
    s = reg.schedules(RID)[0]
    assert reg.schedule_label(RID, s) == "Tuesdays and Thursdays 7:30 PM, Saturdays 9:00 PM"
    reg.set_schedule(RID, "pug", {"kind": "pickup"}, "t")
    assert reg.schedule_label(RID, reg.schedules(RID)[1]) == "Pickup template: opened by an officer"
    assert reg.time_label("00:05") == "12:05 AM"


# ---------------------------------------------------------------- /gm config raid → Schedules

def test_config_schedules_add_edit_and_save_in_one_commit(reg, store, owner):
    opened(reg)
    h = Harness(reg)
    n0 = commits(store)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "times")
        assert "Add a run time" in h.buttons() and "Add another schedule" in h.buttons()
        await h.press("Add a run time")
        await submit_multi(h, nights=["Tue"], hour="19", minute="30")
        await h.press("Add another schedule")
        await h.pick("What kind of schedule?", "weekly")
        await submit_multi(h, name="Alt run", nights=["Sat"], hour="20", minute="0")
        assert "Added **Alt run**: Saturdays 8:00 PM" in h.text and "Next runs" in h.text and "20:00" not in h.text
        await h.pick("Change this schedule", "cadence")
        await h.submit(h.modal, signup_lead_hours="inherit", nudge_hours_before="inherit", lock_hours_before="12", confirm_hours_before="inherit", fill_ask_hours="2")
        assert "locks 12 hours before" in h.text and "confirm by 6 hours before (the raid's)" in h.text
        await h.pick("Change this schedule", "rosters")
        await h.pick("Rosters per run", "2")
        assert "2 rosters" in h.text and "20 seats" in h.text
        await h.press("All schedules")
        assert "Regular nights" in h.text and "Alt run" in h.text and h.selects() == ["Pick a schedule"]
        await h.press("Add a schedule")
        await h.pick("What kind of schedule?", "lockout")
        await submit_multi(h, name="Reset night", days=["1"], hour="21", minute="0")
        assert "Day 1 of each 3-day lockout, 9:00 PM" in h.text
        await h.press("Back to the sections")
        assert commits(store) == n0  # nothing before Save
        await h.press("Save")
        assert "New schedule **Alt run**: Saturdays 8:00 PM · 2 rosters" in h.text and "New schedule **Reset night**" in h.text
        assert "Next runs with these rules:" in h.text and "Alt run: " in h.text and "19:30" not in h.text
        await h.press("Save")
        assert "✅" in h.text
    asyncio.run(go())
    assert commits(store) == n0 + 1
    by_id = {s["id"]: s for s in reg.schedules(RID)}
    assert set(by_id) == {"default", "alt-run", "reset-night"} and by_id["default"]["slots"] == ["Tue 19:30"]
    assert by_id["alt-run"]["rosters"] == 2 and reg.schedule_def(RID, "alt-run")["lock_hours_before"] == 12
    assert reg.schedule_def(RID, "alt-run")["fill_ask_hours"] == 2 and by_id["reset-night"]["days"] == [1] and by_id["reset-night"]["time"] == "21:00"


def test_config_schedules_refuse_on_the_spot_and_remove_and_pause(reg, store, owner):
    opened(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    h = Harness(reg)
    before = copy.deepcopy(reg.config.raids)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "times")
        await h.pick("Pick a schedule", "alt")
        await h.press("✕ Sat 8:00 PM")  # the last run time of a weekly schedule: the registry's rule, on the spot
        assert "❌" in h.text and "at least one run time" in h.text
        await h.press("Fix this")
        assert "Sat 8:00 PM" in h.text
        await h.pick("Change this schedule", "active")
        assert "Paused" in h.text
        await h.pick("Change this schedule", "behaviour")
        await h.pick("Nudge the unanswered", "off")
        assert "the raid's" in h.text or "the raid’s" in h.text
        await h.press("Back")
        await h.pick("Change this schedule", "remove")
        await h.press("Remove")
        assert "Removed Alt run" in h.text
        await h.press("Back to the sections")
        await h.press("Cancel")
    asyncio.run(go())
    assert reg.config.raids == before  # Cancel: nothing written


# ---------------------------------------------------------------- plain text

def test_plain_text_schedule_ops_describe_in_words_and_apply(reg, store):
    op = ConfigOp(op="schedule_set", target=RID, field="alt.slots", value="Sat 20:00")
    assert configops.describe(reg, op) == "Barrow Deeps: new schedule alt — Sat 8:00 PM"
    assert "20:00" not in configops.describe(reg, op)
    with pytest.raises(RegistryError, match="needs the owner"):
        configops.apply(reg, op, "Officer", is_owner=False)
    assert configops.apply(reg, op, "Owner", is_owner=True) == "Barrow Deeps · Weekly: new schedule, Saturdays 8:00 PM"
    lock = ConfigOp(op="schedule_set", target=RID, field="alt.lock_hours_before", value="12")
    assert configops.describe(reg, lock) == "Barrow Deeps · Weekly: lock hours before: the raid's (24 hours before) → lock hours before: 12 hours before"
    configops.apply(reg, lock, "Owner", is_owner=True)
    back = ConfigOp(op="schedule_set", target=RID, field="alt.lock_hours_before", value="inherit")
    assert configops.describe(reg, back).endswith("12 hours before → lock hours before: the raid's (24 hours before)")
    configops.apply(reg, ConfigOp(op="schedule_set", target=RID, field="alt.name", value="Alt run"), "Owner", is_owner=True)
    lk = ConfigOp(op="schedule_set", target=RID, field="reset.lockout", value="1, 3 at 20:00")
    assert configops.describe(reg, lk) == "Barrow Deeps: new schedule reset — days of each lockout, day 1 and 3, 8:00 PM"
    configops.apply(reg, lk, "Owner", is_owner=True)
    assert next(s for s in reg.schedules(RID) if s["id"] == "reset")["days"] == [1, 3]
    rm = ConfigOp(op="schedule_remove", target=RID, field="alt")
    assert configops.describe(reg, rm) == "Barrow Deeps: remove schedule Alt run (Saturdays 8:00 PM); runs already open keep their times"
    assert configops.apply(reg, rm, "Owner", is_owner=True) == "Barrow Deeps: schedule Alt run removed"
    bad = ConfigOp(op="schedule_set", target=RID, field="nothing", value="x")
    assert "<schedule id>.<setting>" in configops.describe(reg, bad)
    assert "schedule_set" in configops.SCHEMA_TEXT and "schedule_remove" in configops.ConfigOp.model_fields["op"].description
    assert len(configops.ConfigOp.model_fields) <= 13


def test_run_open_takes_a_schedule(reg, rs):
    opened(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00"}, "t")
    reg.set_schedule(RID, "pug", {"kind": "pickup"}, "t")
    line = configops.describe(reg, ConfigOp(op="run_open", target=RID, field="alt"))
    assert line.startswith("open Barrow Deeps · Weekly ") and "-alt-" in line
    assert "pickup template" in configops.describe(reg, ConfigOp(op="run_open", target=RID, field="pug"))
    assert "-pug-" in configops.describe(reg, ConfigOp(op="run_open", target=RID, field="pug", value="2030-01-08 19:30"))


# ---------------------------------------------------------------- web API + MCP

class OpeningBot(FakeBot):
    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.opened = []

    async def open_run_and_post(self, reg, rs, rid, start, by, schedule=None):
        self.opened.append((rid, start, schedule))
        return rc.open_run(reg, rs, rid, start, by=by, schedule=schedule)


@pytest.fixture
def api(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = OpeningBot(reg, rs)
    return TestClient(web.create_app(bot)), bot


def test_web_routes_list_set_remove_and_open_by_schedule(api, reg, rs):
    c, bot = api
    opened(reg)
    r = c.post("/api/admin/raid/schedule", json={"instance": RID, "id": "alt", "fields": {"slots": ["Sat 20:00"], "name": "Alt run", "rosters": 2}}, headers=bearer())
    assert r.status_code == 200 and r.json()["message"] == "Barrow Deeps · Alt run: new schedule, Saturdays 8:00 PM · 2 rosters"
    bad = c.post("/api/admin/raid/schedule", json={"instance": RID, "id": "alt", "fields": {"rosters": 9}}, headers=bearer())
    assert bad.status_code == 400 and "rosters" in bad.json()["error"]
    raid = next(x for x in c.get("/api/raids", headers=bearer()).json()["raids"] if x["id"] == RID)
    alt = next(s for s in raid["schedules"] if s["id"] == "alt")
    assert alt["label"] == "Saturdays 8:00 PM · 2 rosters" and alt["seats"] == 20 and alt["slot_labels"] == ["Sat 8:00 PM"]
    assert len(alt["next"]) == 3 and all("PM" in x and "20:00" not in x for x in alt["next"])
    assert alt["effective"]["lock_hours_before"] == 24 and alt["own"] == {}
    c.post("/api/admin/raid/schedule", json={"instance": RID, "id": "pug", "fields": {"kind": "pickup", "name": "Pickup"}}, headers=bearer())
    rosters = next(x for x in c.get("/api/rosters", headers=bearer()).json()["raids"] if x["id"] == RID)
    assert {s["id"] for s in rosters["schedules"]} == {"alt", "pug"} and all(u["schedule"] == "alt" for u in rosters["upcoming"])
    need_time = c.post(f"/api/raid/{RID}/open", json={"schedule": "pug"}, headers=bearer())
    assert need_time.status_code == 400 and "pick the date and time" in need_time.json()["error"]
    ok = c.post(f"/api/raid/{RID}/open", json={"schedule": "alt"}, headers=bearer())
    assert ok.status_code == 200 and bot.opened[-1][2] == "alt" and "-alt-" in ok.json()["message"]
    when = (reg.now_local() + timedelta(days=1)).strftime("%Y-%m-%d 21:00")
    c.post(f"/api/raid/{RID}/open", json={"schedule": "pug", "when": when}, headers=bearer())
    assert bot.opened[-1][2] == "pug"
    gone = c.post("/api/admin/raid/schedule/remove", json={"instance": RID, "id": "pug"}, headers=bearer())
    assert gone.status_code == 200 and "removed" in gone.json()["message"]


def test_mcp_schedule_tools_call_the_routes():
    from oibot_gm import mcp_server as ms

    seen = []

    def handler(request: "httpx.Request"):
        import json

        seen.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": RID, "name": "Barrow Deeps"}]})
        return httpx.Response(200, json={"message": "ok"})

    import httpx

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        assert ms.schedule_set("Barrow Deeps", "alt", "lock_hours_before", "12") == "ok"
        assert ms.schedule_remove(RID, "alt") == "ok"
        assert ms.open_run(RID, schedule="alt") == "ok"
    finally:
        ms.use(None)
    posts = [(p, b) for m, p, b in seen if m == "POST"]
    assert posts[0] == ("/api/admin/raid/schedule", {"instance": RID, "id": "alt", "fields": {"lock_hours_before": "12"}})
    assert posts[1] == ("/api/admin/raid/schedule/remove", {"instance": RID, "id": "alt"})
    assert posts[2] == (f"/api/raid/{RID}/open", {"when": "", "schedule": "alt"})


def test_help_schedule_lists_the_real_upcoming_runs_per_schedule(reg):
    from oibot_gm import discord_help

    opened(reg)
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    text = discord_help.guide_text(reg, "schedule")
    assert "Regular nights: Tuesdays 7:30 PM" in text and "Alt run: Saturdays 8:00 PM" in text
    for s in reg.schedules(RID):
        for t in rc.next_runs_of(reg, RID, s):
            assert f"<t:{int(t.timestamp())}:f>" in text
    assert "19:30" not in text and "20:00" not in text


# ---------------------------------------------------------------- /raid open: every schedule's next run, pickup templates

def test_raid_open_lists_schedules_by_name_and_opens_a_pickup_template_at_a_picked_time(reg, rs, owner):
    import test_flows_raid as tfr

    from oibot_gm import flows_raid  # noqa: F401 — registers the flows
    from oibot_gm.wizard import FLOWS

    class Bot(tfr.FakeBot):
        async def open_run_and_post(self, reg, rs, instance, start, by, schedule=None):
            self.calls.append(("open_run_and_post", instance, start, schedule))
            return rc.open_run(reg, rs, instance, start, by=by, schedule=schedule)

    opened(reg)
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")
    bot = Bot(rs)
    h = Harness(reg, rs, client=bot)
    ups = rc.upcoming(reg, RID, reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", RID)
        s0, t0 = ups[0]
        assert f"Open the next slot — {reg.local12(t0)} · {s0['name']}" in h.buttons() and "Open Pickup — pick a time" in h.buttons()
        sel = next(s for s in h._items() if getattr(s, "placeholder", None) == "Or another upcoming run")
        alt_opt = next(o for o in sel.options if o.value.endswith("|alt"))
        assert alt_opt.description.startswith("Alt run · ") and "20:00" not in alt_opt.label
        await h.pick("Or another upcoming run", alt_opt.value)
        assert "**Barrow Deeps** · Alt run" in h.text
        await h.press("Open")
    asyncio.run(go())
    opens = [c for c in bot.calls if c[0] == "open_run_and_post"]
    assert opens[-1][3] == "alt"

    h2 = Harness(reg, rs, client=bot)
    day = (reg.now_local() + timedelta(days=2)).date().isoformat()

    async def go2():
        await FLOWS["raid_open"](h2.command(), reg)
        await h2.pick("Pick the raid", RID)
        await h2.press("Open Pickup — pick a time")
        assert h2.modal.title == "Open Pickup"
        await h2.submit(h2.modal, day=day, hour="21", minute="0")
        assert "Pickup" in h2.text and "9:00 PM" in h2.text
        await h2.press("Open")
    asyncio.run(go2())
    opens = [c for c in bot.calls if c[0] == "open_run_and_post"]
    assert opens[-1][3] == "pug" and opens[-1][2].hour == 21
