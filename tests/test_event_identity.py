"""Phase 3, Task 3: a run's key holds no time — a new run is keyed '<raid abbr>-<6 random chars>' and a schedule's run is
found by the (schedule id, occurrence) stamps it was opened for; a run that existed before the upgrade (an old key,
the old `default`, a renamed duplicate, a one-off pair) is stamped once at the start and found the same way."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import GUILD, first_slot, weekly
from test_guild_schedules import commit_count
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import raidcycle as rc
from oibot_gm.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
RID, HYJAL = "barrow_deeps", "hyjal_summit_forever"
TZ = ZoneInfo("America/Los_Angeles")  # conftest's guild timezone
OPENED = {"first_open": "2026-01-01T00:00"}  # the profile's raids open 9 Dec: every run here is after it
STAMPS = ("schedule", "occurrence", "event", "part", "starts")
LEAD = {"signup_lead_hours": 120}  # every occurrence these tests write is due: the scheduler would open it now


def due(reg, raid: str, t) -> None:
    """The premise of a scheduler test: the occurrence IS due to open this minute (else the test proves nothing)."""
    assert any(s["raid"] == raid and x == t for s, x in rc.occurrences(reg, reg.now_local(), lead=True)), "not due: the test proves nothing"


def run(coro):
    return asyncio.run(coro)


def start_in(reg, days: float = 3, hour: int | None = None):
    t = (reg.now_local() + timedelta(days=days)).replace(second=0, microsecond=0)
    return t.replace(hour=hour, minute=30) if hour is not None else t


# ---------------------------------------------------------------- new runs

def test_a_new_run_key_carries_no_time(reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg, days=3), by="t")
    assert re.fullmatch(r"bd-[23456789abcdefghjkmnpqstuvwxyz]{6}", ev.key) and ev.team == ev.key
    assert (ev.event, ev.part, ev.starts, ev.schedule, ev.occurrence) == (ev.key, 0, 0, None, None)
    t = reg.config.roster(ev.key)
    assert (t["event"], t["part"], t["starts"], t["occurrence"]) == (ev.key, 0, 0, None)


def test_a_schedule_run_is_found_by_its_occurrence(reg, rs):
    reg.set_raid_overrides(RID, OPENED, "t")
    sid = weekly(reg, RID, "Tue 19:30")
    t = first_slot(reg, RID)
    ev = rc.open_run(reg, rs, RID, t, schedule=sid)
    assert ev.occurrence == rc.occurrence_stamp(reg, t) and rc.event_of_occurrence(rs, sid, ev.occurrence) is ev
    assert ev.schedule == sid and reg.config.roster(ev.key)["occurrence"] == ev.occurrence
    assert rc.open_run(reg, rs, RID, t, schedule=sid) is ev
    assert rc.open_run(reg, rs, RID, t) is ev, "a time that IS the schedule's run opens as that run"


def test_a_one_off_at_the_same_minute_is_the_same_run(reg, rs):
    t = start_in(reg, days=2)
    assert rc.open_run(reg, rs, RID, t) is rc.open_run(reg, rs, RID, t)


def test_a_rehearsal_never_meets_a_real_run_and_belongs_to_its_tester(reg, rs):
    t = start_in(reg, days=2)
    real = rc.open_run(reg, rs, RID, t)
    cut = {"soft": 2, "hard": 1.5, "confirm": 1, "test_by": 41}
    test = rc.open_run(reg, rs, RID, t, cutoffs=cut)
    assert test is not real and test.test and rc.open_run(reg, rs, RID, t, cutoffs=cut) is test
    with pytest.raises(rc.RegistryError, match="opened a test event at"):
        rc.open_run(reg, rs, RID, t, cutoffs={**cut, "test_by": 42})
    rc.end_run(reg, rs, test, "t")
    test.state = "cancelled"
    rs.save(test, "cleared")
    again = rc.open_run(reg, rs, RID, t, cutoffs=cut)
    assert again is not test and again.state == "open", "a cleared rehearsal never blocks the minute"


def test_a_closed_occurrence_is_refused_and_a_follow_on_gets_its_own_key(reg, rs):
    reg.set_raid_overrides(HYJAL, OPENED, "t")
    reg.set_raid_overrides(RID, OPENED, "t")  # C8 (Task 4): a night its follow-on isn't open yet is skipped
    sid = weekly(reg, HYJAL, "Tue 18:30")
    reg.set_schedule(sid, {"follow": f"{RID} x2"}, "t")
    t = first_slot(reg, HYJAL)
    lead = rc.open_run(reg, rs, HYJAL, t, schedule=sid)
    fev = rc.follow_of(rs, lead)
    assert re.fullmatch(r"bd-[23456789abcdefghjkmnpqstuvwxyz]{6}", fev.key) and fev.team == fev.key and fev.follows == lead.key
    assert (fev.event, fev.part, fev.starts) == (lead.key, 1, 180) and fev.start == t + timedelta(hours=3)
    lead.state = "cancelled"
    rs.save(lead, "cancelled")
    n = len(rs.events)
    with pytest.raises(rc.RegistryError, match="cancelled event isn't opened again"):
        rc.open_run(reg, rs, HYJAL, t, schedule=sid)
    assert len(rs.events) == n


def test_nothing_builds_or_parses_a_run_key():
    src = {p: p.read_text() for p in (ROOT / "src/oibot_gm").rglob("*.py")}
    for pat in (r"\bevent_key\(", r"\brun_key\(", r"schedule_kw\(", r"RunKeyClash", r"\.date\(\)\.isoformat\(\)}\"",
                r"f\"\{team\[.key.\]\}-"):
        assert not any(re.search(pat, s) for s in src.values()), pat


# ---------------------------------------------------------------- runs from before the upgrade

def _old_run(key: str, team: str, instance: str, start: datetime, *, schedule=None, series=None, kind="recurring", follows=None,
             followed_by=None, signups=None, message_id=None) -> dict:
    """A run file as phase 2's code wrote it: no identity stamps."""
    ev = rc.RaidEvent(key=key, team=team, instance=instance, schedule=schedule, series=series, kind=kind, starts_at=start.isoformat(),
                      follows=follows, followed_by=followed_by, signups=signups or {}, message_id=message_id,
                      channel_id=1111 if message_id else None)
    ev.log, ev.log_at = ["opened (recurring); 0 marked away from absences"], [""]
    raw = json.loads(ev.model_dump_json())
    for k in STAMPS[1:]:
        raw.pop(k, None)
    return raw


def _old_dict(key: str, instance: str, start: datetime, schedule_id: str, *, series=None, kind="recurring", **extra) -> dict:
    return {"key": key, "name": f"{instance} {start:%a %H:%M}", "size": 10, "schedule": start.strftime("%a %H:%M"), "instance": instance,
            "cutoff_soft_hours": 2, "cutoff_hard_hours": 1, "confirm_hours": 0.5, "open_days_before": 5, "reminders": "none",
            "open_dm": False, "autofill": False, "ephemeral": True, "schedule_id": schedule_id, "kind": kind, "series": series,
            "confirm": True, "rosters": 1, "fill_ask_hours": 1.0, "split_policy": "balanced", **extra}


def _old_guild(data_root, old_store_with, cfg: dict, files: list[dict]):
    """The data repo as phase 2 left it: `cfg` on guild.yaml and the run files; → (registry, run store) as a start opens them."""
    d = data_root / GUILD / "raids"
    d.mkdir(exist_ok=True)
    for raw in files:
        (d / f"{raw['key']}.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False))
    store = old_store_with(cfg)
    from oibot_gm.profiles import GameProfile

    reg = Registry(store, GUILD, GameProfile.load(ROOT / "profiles" / "forever"))
    return reg, rc.RaidStore(store, GUILD), store


def _soon(hours: float = 60) -> datetime:
    return (datetime.now(TZ) + timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)


def _scheduler(reg, rs) -> tuple[NightBot, list]:
    bot = NightBot(reg, rs)
    reg.config.signup_channel_id = bot.signup.id
    opened: list = []
    real = bot.open_run_and_post

    async def spy(*a, **k):
        ev = await real(*a, **k)
        opened.append(ev.key)
        return ev

    bot.open_run_and_post = spy
    return bot, opened


@pytest.mark.parametrize("stamp_first", [True, False], ids=["stamped at the start", "read before the stamps"])
def test_an_old_default_run_is_never_opened_twice(data_root, old_store_with, stamp_first):
    t = _soon()
    key = f"bd-{t:%m%d-%H%M}-{t.date().isoformat()}"
    cfg = {"raids": {RID: OPENED}, "schedules": [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": [t.strftime("%a %H:%M")], **LEAD}],
           "rosters": [_old_dict(key[:-11], RID, t, "default", series="barrow_deeps:default")]}
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, [_old_run(key, key[:-11], RID, t, schedule="default", series="barrow_deeps:default",
                                                                          message_id=555)])
    due(reg, RID, t)
    if stamp_first:
        rc.migrate_at_start(reg, rs)
        assert (rs.events[key].schedule, rs.events[key].occurrence) == ("barrow-deeps", rc.occurrence_stamp(reg, t))
    bot, opened = _scheduler(reg, rs)
    run(bot.scheduler_tick_guild(reg))
    assert opened == [] and list(rs.events) == [key] and bot.signup.sent == [], "no second run, no second sheet"
    assert rs.events[key].message_id == 555


def test_a_renamed_duplicates_old_run_is_never_opened_twice(data_root, old_store_with):
    t = _soon()
    slot = t.strftime("%a %H:%M")
    team = f"bd-{t:%m%d-%H%M}-tuesday"
    key = f"{team}-{t.date().isoformat()}"
    cfg = {"raids": {RID: OPENED, HYJAL: OPENED},
           "schedules": [{"id": "tuesday", "lineup": [{"raid": HYJAL, "rosters": 1}], "kind": "weekly", "slots": [slot], **LEAD},
                         {"id": "tuesday", "lineup": [{"raid": RID, "rosters": 1}], "kind": "weekly", "slots": [slot], **LEAD}],
           "rosters": [_old_dict(team, RID, t, "tuesday", series="barrow_deeps:tuesday")]}
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, [_old_run(key, team, RID, t, schedule="tuesday", series="barrow_deeps:tuesday",
                                                                           message_id=556)])
    assert reg.first_raid("tuesday-2") == RID
    due(reg, RID, t)
    rc.migrate_at_start(reg, rs)
    assert rs.events[key].schedule == "tuesday-2"
    bot, opened = _scheduler(reg, rs)
    run(bot.scheduler_tick_guild(reg))
    assert [rs.events[k].instance for k in opened] == [HYJAL], "Hyjal's own tuesday opens; the Barrow Deeps one is the old run"
    assert sum(1 for e in rs.events.values() if e.instance == RID) == 1


def test_a_cancelled_old_occurrence_is_not_opened_again(data_root, old_store_with):
    t = _soon()
    key = f"bd-{t:%m%d-%H%M}-{t.date().isoformat()}"
    raw = {**_old_run(key, key[:-11], RID, t, schedule="default", series="barrow_deeps:default"), "state": "cancelled"}
    cfg = {"raids": {RID: OPENED}, "schedules": [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": [t.strftime("%a %H:%M")], **LEAD}]}
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, [raw])  # its run dict long gone (cleanup)
    before = (data_root / GUILD / "raids" / f"{key}.json").read_text()
    due(reg, RID, t)
    assert rc.migrate_at_start(reg, rs) is not None and rs.events[key].event is None, "a closed run is never stamped"
    assert (data_root / GUILD / "raids" / f"{key}.json").read_text() == before
    bot, opened = _scheduler(reg, rs)
    run(bot.scheduler_tick_guild(reg))
    assert opened == [] and list(rs.events) == [key]


def _big_guys_pair(t: datetime) -> tuple[dict, list[dict], list[dict]]:
    """big-guys-for's live shape: a one-off Hyjal lead with a Barrow Deeps follow-on in the 'after' key space."""
    lt, ft = f"hsf-{t:%m%d-%H%M}-adhoc", f"bd-{(t + timedelta(hours=3)):%m%d-%H%M}-after"
    lk, fk = f"{lt}-{t.date().isoformat()}", f"{ft}-{(t + timedelta(hours=3)).date().isoformat()}"
    signups = {str(900 + i): rc.Signup(discord_id=900 + i, display_name=f"P{i}", character=f"P{i}", cls="Priest", spec="Holy", role="healer",
                                       status="in", updated_at="2026-11-20T10:00:00+00:00", joined_at="2026-11-20T10:00:00+00:00")
               for i in range(3)}
    dicts = [_old_dict(lt, HYJAL, t, "adhoc", kind="adhoc", follow={"raid": RID, "rosters": 1}, followed_by=ft),
             _old_dict(ft, RID, t + timedelta(hours=3), "after", kind="adhoc", follows=lt)]
    files = [_old_run(lk, lt, HYJAL, t, schedule="adhoc", kind="adhoc", followed_by=fk, signups=signups, message_id=777),
             _old_run(fk, ft, RID, t + timedelta(hours=3), schedule="after", kind="adhoc", follows=lk, signups=signups)]
    cfg = {"raids": {RID: OPENED, HYJAL: OPENED},
           "schedules": [{"id": "tuesday", "lineup": [{"raid": HYJAL, "rosters": 1}], "kind": "weekly", "slots": ["Tue 18:30"]}], "rosters": dicts}
    return cfg, dicts, files


def test_stamping_twice_changes_nothing(data_root, old_store_with):
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    lk, fk = files[0]["key"], files[1]["key"]
    n0 = commit_count(store)
    assert sorted(rc.stamp_runs(reg, rs)) == sorted([lk, fk])
    store.flush()
    assert commit_count(store) == n0 + 1
    on_disk = {k: json.loads((data_root / GUILD / "raids" / f"{k}.json").read_text()) for k in (lk, fk)}
    assert {k: on_disk[lk][k] for k in STAMPS} == {"schedule": None, "occurrence": None, "event": lk, "part": 0, "starts": 0}
    assert {k: on_disk[fk][k] for k in STAMPS} == {"schedule": None, "occurrence": None, "event": lk, "part": 1, "starts": 180}
    for raw in files:  # nothing but the stamps: the keys, the sheet's message id and every signup as they were
        assert {k: v for k, v in on_disk[raw["key"]].items() if k not in STAMPS} == {k: v for k, v in raw.items() if k != "schedule"}
    assert on_disk[lk]["message_id"] == 777 and on_disk[lk]["signups"] == files[0]["signups"]
    lead_dict, follow_dict = reg.config.roster(files[0]["team"]), reg.config.roster(files[1]["team"])
    assert (lead_dict["event"], lead_dict["part"], follow_dict["event"], follow_dict["part"], follow_dict["starts"]) == (lk, 0, lk, 1, 180)
    assert lead_dict["schedule_id"] == "adhoc" and follow_dict["schedule_id"] == "after", "the dicts keep their old ids"
    assert rc.stamp_runs(reg, rs) == []
    store.flush()
    assert commit_count(store) == n0 + 1
    again = rc.RaidStore(store, GUILD)  # a restart reads the stamps back
    assert (again.events[fk].event, again.events[fk].part, again.events[lk].key) == (lk, 1, lk)
    assert rc.run_kind(reg, again.events[lk]) == "adhoc" and rc.run_series(reg, again.events[lk]) is None


def test_a_run_dict_without_stamps_heals_from_its_run_file(data_root, old_store_with):
    """The files were stamped but the config save failed (twice): the next stamp_runs gives the dicts the files' stamps
    and leaves the files as they are; a third run writes nothing."""
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    lk, fk = files[0]["key"], files[1]["key"]
    rc.stamp_runs(reg, rs)
    store.flush()
    on_disk = {k: (data_root / GUILD / "raids" / f"{k}.json").read_text() for k in (lk, fk)}
    for raw in files:  # the dicts as an unsaved config left them
        d = reg.config.roster(raw["team"])
        for k in ("occurrence", "event", "part", "starts"):
            d.pop(k, None)
    reg.save_config("t: the dicts without their stamps", notify=False)
    store.flush()
    n0 = commit_count(store)
    assert sorted(rc.stamp_runs(reg, rs)) == sorted([lk, fk])
    store.flush()
    lead_dict, follow_dict = reg.config.roster(files[0]["team"]), reg.config.roster(files[1]["team"])
    assert (lead_dict["event"], lead_dict["part"], follow_dict["event"], follow_dict["part"], follow_dict["starts"]) == (lk, 0, lk, 1, 180)
    assert {k: (data_root / GUILD / "raids" / f"{k}.json").read_text() for k in (lk, fk)} == on_disk, "the files are not rewritten"
    assert commit_count(store) == n0 + 1
    assert rc.stamp_runs(reg, rs) == []


def test_the_start_saves_the_lift_and_the_stamps_in_one_commit(data_root, old_store_with):
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    cfg["schedules"] = [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 04:00"]}, *cfg["schedules"]]
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    n0 = commit_count(store)
    rc.migrate_at_start(reg, rs)
    store.flush()
    assert commit_count(store) == n0 + 1 and all(e.event for e in rs.live()) and reg.ids_saved, "the ids lift and the stamps, one commit"
    import subprocess

    said = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=store.root, check=True, capture_output=True, text=True).stdout.strip()
    assert said == (f"{GUILD}: schedules have ids of their own, the old names kept as aliases "
                    f"(Barrow Deeps · Raid nights is now barrow-deeps); live events stamped with their schedule and occurrence: "
                    f"{files[0]['key']}, {files[1]['key']}; live events keep the priority they read today: "
                    f"{files[0]['key']}, {files[1]['key']} (by migration)"), "what the save changed, each clause once"
    assert rc.migrate_at_start(reg, rs) == []
    store.flush()
    assert commit_count(store) == n0 + 1, "a second start writes nothing"


def test_the_one_off_pair_keeps_its_keys_and_opens_nothing_new(data_root, old_store_with):
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, files)
    rc.migrate_at_start(reg, rs)
    lead = rs.events[files[0]["key"]]
    assert rc.open_run(reg, rs, HYJAL, t, schedule=rc.ADHOC_SCHEDULE) is lead, "a one-off at that minute is the old lead"
    assert rc.follow_of(rs, lead).key == files[1]["key"] and rc.lead_of(rs, rc.follow_of(rs, lead)) is lead
    assert len(rs.events) == 2


def test_an_old_key_is_never_read_for_its_time(data_root, old_store_with):
    """'bd-1215-1830-tue-2026-12-15' whose run starts on the 16th: only its stamps say which occurrence it is (a run
    file from older code has no stands_for, so it stands for nothing but its own occurrence)."""
    t15 = _soon(30)
    t16 = t15 + timedelta(days=1)
    team = f"bd-{t15:%m%d-%H%M}-tue"
    key = f"{team}-{t15.date().isoformat()}"
    cfg = {"raids": {RID: OPENED}, "schedules": [{"id": "tue", "lineup": [{"raid": RID, "rosters": 1}], "kind": "weekly",
                                                   "slots": [t15.strftime("%a %H:%M")], "signup_lead_hours": 120}],
           "rosters": [_old_dict(team, RID, t16, "tue", series="tue")]}
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, [_old_run(key, team, RID, t16, schedule="tue", series="tue", message_id=9)])
    assert rc.stamp_runs(reg, rs) == [key]
    assert rs.events[key].occurrence == rc.occurrence_stamp(reg, t16)
    assert rc.event_of_occurrence(rs, "tue", rc.occurrence_stamp(reg, t15)) is None
    due(reg, RID, t15)
    bot, opened = _scheduler(reg, rs)
    run(bot.scheduler_tick_guild(reg))
    new = [rs.events[k] for k in opened]
    assert [e.start for e in new] == [t15] and new[0].key != key and new[0].key.startswith("bd-") and len(new[0].key) == 9


def test_a_timezone_change_opens_no_duplicate(reg, rs):
    reg.set_raid_overrides(RID, OPENED, "t")
    t = start_in(reg, days=2)
    sid = weekly(reg, RID, t.strftime("%a %H:%M"))
    bot, opened = _scheduler(reg, rs)
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1
    ev = rs.events[opened[0]]
    reg.config.timezone = "America/New_York"  # the guild moves; the run stays at its moment (keep)
    reg.save_config("t: timezone", notify=False)
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1 and ev.start == t and rc.event_of_occurrence(rs, sid, ev.occurrence) is ev


def test_open_refuses_when_the_occurrence_changed(reg, rs, web_bot):
    reg.set_raid_overrides(RID, OPENED, "t")
    sid = weekly(reg, RID, "Wed 20:00")
    c = web_bot.c
    p = c.post(f"/api/raid/{RID}/open/preview", json={"schedule": sid}, headers=H()).json()
    assert p["occurrence"] and "key" not in p
    reg.set_schedule(sid, {"slots": "Thu 21:00"}, "t")
    bad = c.post(f"/api/raid/{RID}/open", json={"schedule": sid, "occurrence": p["occurrence"]}, headers=H())
    assert bad.status_code == 409 and bad.json()["preview"]["occurrence"] != p["occurrence"] and not rs.events
    p2 = bad.json()["preview"]
    ok = c.post(f"/api/raid/{RID}/open", json={"start": p2["start"], "schedule": sid, "occurrence": p2["occurrence"]}, headers=H())
    assert ok.status_code == 200, ok.text
    assert rs.events[ok.json()["key"]].occurrence == p2["occurrence"]


def test_the_way_back_refuses_a_new_key_and_passes_a_stamped_old_pair(data_root, old_store_with):
    from test_guild_schedules import _lower_script

    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    rc.migrate_at_start(reg, rs)
    store.flush()
    assert _lower_script().lower(data_root, ids_only=True, dry_run=True)[0].startswith(f"{GUILD}: ")
    assert "can't find" not in _lower_script().lower(data_root, ids_only=True, dry_run=True)[0], "the old pair keeps its keys"
    new = rc.open_run(reg, rs, RID, t + timedelta(days=2))
    store.flush()
    said = _lower_script().lower(data_root, ids_only=True, dry_run=True)
    assert said == [f"{GUILD}: refused, nothing changed. The older code can't find the run Barrow Deeps {reg.local12(new.start)}, so end or cancel it first."]


def test_the_way_back_refuses_a_cancelled_new_key_run_whose_night_is_ahead(data_root, old_store_with):
    """Phase 2's scheduler finds a run only by the key it builds: a schedule's run with a phase-3 key, cancelled before
    its night, is invisible to it — it would open a fresh sheet for that night."""
    from test_guild_schedules import _lower_script

    reg, rs, store = _old_guild(data_root, old_store_with, {"raids": {RID: OPENED}}, [])
    rc.migrate_at_start(reg, rs)
    sid = weekly(reg, RID, "Tue 19:30")
    t = first_slot(reg, RID)
    ev = rc.open_run(reg, rs, RID, t, schedule=sid)
    store.flush()
    assert _lower_script().lower(data_root, ids_only=True, dry_run=True) == [
        f"{GUILD}: refused, nothing changed. The older code can't find the run Barrow Deeps {reg.local12(t)}, so wait until it has been played and ended."]
    ev.state, ev.closed_at = "cancelled", rc.now()
    rs.save(ev, "harness: cancelled")
    store.flush()
    assert _lower_script().lower(data_root, ids_only=True, dry_run=True) == [
        f"{GUILD}: refused, nothing changed. The older code would open Barrow Deeps {reg.local12(t)} again, so wait until that night "
        "has passed, or pause or remove its schedule first."]


def test_the_way_back_refuses_a_run_that_stands_in_for_another_night(data_root, old_store_with):
    """Phase 2 never reads `stands_for`: an old-key run that stands in for another night of its schedule (its night
    was edited while it was open) would have that night opened too."""
    from test_guild_schedules import _lower_script

    t = _soon()
    key = f"bd-{t:%m%d-%H%M}-{t.date().isoformat()}"
    raw = _old_run(key, key[:-11], RID, t, schedule="default", series="barrow_deeps:default")
    cfg = {"raids": {RID: OPENED}, "schedules": [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": [t.strftime("%a %H:%M")], **LEAD}],
           "rosters": [_old_dict(key[:-11], RID, t, "default", series="barrow_deeps:default")]}
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, [raw])
    rc.migrate_at_start(reg, rs)
    store.flush()
    assert "refused" not in _lower_script().lower(data_root, ids_only=True, dry_run=True)[0], "the old run alone is safe"
    ev = rs.events[key]
    ev.stands_for = rc.occurrence_stamp(reg, t + timedelta(days=1))
    rs.save(ev, "harness: stands for the next day")
    store.flush()
    assert _lower_script().lower(data_root, ids_only=True, dry_run=True) == [
        f"{GUILD}: refused, nothing changed. The run Barrow Deeps {reg.local12(t)} stands in for another night of its schedule, "
        "which the older code would open too, so wait until that night has passed."]


def test_the_old_one_off_pair_keeps_working_as_a_two_raid_run(data_root, old_store_with, monkeypatch):
    """Task 4 on big-guys-for's live shape: the same keys and sheet message, the lead's answers on both raids, no part
    opened by the scheduler's minute, the lead's lock locks both, and a cancel (on either) cancels both."""
    from oibot_gm import raid_views

    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, files)
    rc.migrate_at_start(reg, rs)
    lk, fk = files[0]["key"], files[1]["key"]
    lead = rs.events[lk]
    assert [p.key for p in rc.event_parts(rs, lead)] == [lk, fk] and rc.later_parts(rs, lead)[0].part == 1
    assert rc.first_part(rs, rs.events[fk]) is lead and rc.complete_parts(reg, rs, lead) == []
    e, _view = raid_views.sheet_message(reg, lead, rc.run_team(reg, lead), lambda *a, **k: "")
    assert e.title.endswith("Hyjal Summit, then Barrow Deeps")
    assert f"\nthen Barrow Deeps <t:{int((t + timedelta(hours=3)).timestamp())}:t>" in e.description
    lead.signups["903"] = lead.signups["900"].model_copy(update={"discord_id": 903, "display_name": "P3", "character": "P3"})
    rs.save(lead, "harness: one more joiner")
    assert set(rs.events[fk].signups) == set(lead.signups), "the lead's answers are the follow-on's"
    bot = NightBot(reg, rs)
    monkeypatch.setattr(bot, "post_health", lambda *a, **k: asyncio.sleep(0))
    run(bot.scheduler_tick_guild(reg))
    assert sorted(rs.events) == sorted([lk, fk]) and rs.events[lk].message_id == 777

    def board(reg_, rs_, ev, save=True):
        ev.rosters = rc.board_rosters(reg_, ev, [["P0", "P1", "P2"]], rc.run_size(reg_, ev))
        return [], ev.rosters[0]

    monkeypatch.setattr(rc, "propose", board)
    run(bot.lock_run(reg, rs, lead, by="Jon"))
    assert (rs.events[lk].state, rs.events[fk].state) == ("locked", "locked")
    run(bot.cancel_run(reg, rs, rs.events[fk], "Jon", None))
    assert (rs.events[lk].state, rs.events[fk].state) == ("cancelled", "cancelled")
    assert sorted(rs.events) == sorted([lk, fk]) and rs.events[lk].message_id == 777


# ---------------------------------------------------------------- fix round 1

@pytest.mark.parametrize("gone", [RID, HYJAL], ids=["a later raid left the game", "the first raid left the game"])
def test_a_schedule_with_a_raid_the_game_lacks_opens_nothing_and_warns_hourly(reg, rs, monkeypatch, gone):
    from oibot_gm.web.app import schedule_sections

    reg.set_raid_overrides(HYJAL, OPENED, "t")
    reg.set_raid_overrides(RID, OPENED, "t")
    t = start_in(reg, days=1)
    sid = weekly(reg, HYJAL, t.strftime("%a %H:%M"), "night")
    reg.set_schedule(sid, {"follow": f"{RID} x1"}, "t")
    due(reg, HYJAL, t)  # the premise: the night is due while both raids are the game's
    # a filtered COPY in place of the session-wide profile's dict: the undo puts the very same dict back, in its order
    kept = {k: v for k, v in reg.profile.raids.items() if k != gone}
    monkeypatch.setattr(reg.profile, "raids", kept)
    monkeypatch.setattr(reg.base_profile, "raids", kept)
    bot, opened = _scheduler(reg, rs)
    clock = {"now": reg.now_local()}
    monkeypatch.setattr(reg, "now_local", lambda: clock["now"])
    run(bot.scheduler_tick_guild(reg))
    run(bot.scheduler_tick_guild(reg))
    assert opened == [] and not rs.events and bot.signup.sent == [], "no run, no public sheet, never a chain without the raid"
    assert rc.occurrences(reg, reg.now_local(), 24 * 21) == []
    if gone == RID:
        assert rc.upcoming_rows(reg, rs, HYJAL) == []
    assert all(not r["runs"] for sec in schedule_sections(reg) for r in sec["schedules"]), "the public page agrees"
    warns = [x for lvl, x in bot.ops.lines if lvl == "warn" and "opens no runs" in x]
    name = "Barrow Deeps" if gone == RID else "Hyjal Summit Forever"  # a raid the profile lacks: its id in words
    assert warns == [f"Raid nights opens no runs: {name} is no longer a raid "
                     "of this game. Change its raids on the Schedules page, or remove it there."]
    clock["now"] += timedelta(minutes=61)
    run(bot.scheduler_tick_guild(reg))
    assert len([x for lvl, x in bot.ops.lines if "opens no runs" in x]) == 2, "again after an hour, never every minute"


@pytest.mark.parametrize("gone", [RID, HYJAL], ids=["a later raid left the game", "the first raid left the game"])
def test_a_schedule_with_a_raid_the_game_lacks_can_be_removed(reg, monkeypatch, gone):
    """The warning's fix works: the schedule comes off, whichever of its raids left the game."""
    reg.set_raid_overrides(HYJAL, OPENED, "t")
    reg.set_raid_overrides(RID, OPENED, "t")
    sid = weekly(reg, HYJAL, "Tue 19:30", "night")
    reg.set_schedule(sid, {"follow": f"{RID} x1"}, "t")
    kept = {k: v for k, v in reg.profile.raids.items() if k != gone}
    monkeypatch.setattr(reg.profile, "raids", kept)
    monkeypatch.setattr(reg.base_profile, "raids", kept)
    reg.remove_schedule(sid, "t")
    assert sid not in {s["id"] for s in reg.all_schedules()}
    assert sid not in {s["id"] for s in Registry(reg.store, reg.key, reg.base_profile).all_schedules()}, "saved"


def test_a_run_dict_whose_open_was_cut_off_is_cleaned_after_an_hour(reg, rs):
    t = start_in(reg, days=2)
    cut = rc.ensure_run(reg, RID, t, by="t", rs=rs)  # the open stopped here: no run file
    fresh = rc.ensure_run(reg, RID, t + timedelta(hours=1), by="t", rs=rs)
    live = rc.open_run(reg, rs, RID, t + timedelta(hours=2), by="t")
    old = (datetime.now(TZ) - timedelta(hours=2)).isoformat()
    cut["created_at"] = reg.config.roster(live.team)["created_at"] = old
    bot = NightBot(reg, rs)
    run(bot.cleanup_ephemeral(reg, rs))
    keys = {x["key"] for x in reg.config.rosters}
    assert cut["key"] not in keys and fresh["key"] in keys and live.team in keys, "only the old dict with no run goes"
