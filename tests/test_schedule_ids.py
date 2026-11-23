"""Phase 3, Task 1: every schedule has a guild-unique id, there is no `default` schedule, and regulars are keyed by the
schedule id — an older file is lifted at load (lift_schedule_ids, Registry.remap_regulars) and saved once."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from oibot_gm import raidcycle as rc
from oibot_gm import registry as R
from oibot_gm.registry import ConfigChanged, RegistryError

from conftest import GUILD, weekly
from oibot_gm.registry import Registry
from test_guild_schedules import base_config, commit_count


@pytest.fixture
def open_registry(profile):
    return lambda store: Registry(store, GUILD, profile)


RID = "barrow_deeps"
NYX, ANA = 300_000_000_000_000_001, 300_000_000_000_000_002

# the live shapes this lift meets (read 2026-11-23): oi's own run time stored as Barrow Deeps's `default` entry with a
# confirm-by of its own; big-guys-for's weekly `tuesday` on Hyjal Summit and a live one-off lead + follow-on pair
OI = {"schedules": [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 04:00"], "confirm_hours_before": 12}]}
BIG_GUYS = {
    "schedules": [{"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}], "kind": "weekly", "slots": ["Tue 18:30"]}],
    "rosters": [
        {"key": "hsf-1210-1830-adhoc", "name": "Hyjal Summit · Ad hoc Thu 10 Dec 6:30 PM", "size": 25, "instance": "hyjal_summit_forever",
         "schedule_id": "adhoc", "kind": "adhoc", "series": None, "follow": {"raid": RID, "rosters": 1}, "followed_by": "bd-1210-2130-after",
         "fill_ask_hours": 1.0},
        {"key": "bd-1210-2130-after", "name": "Barrow Deeps · after Hyjal Summit", "size": 10, "instance": RID, "schedule_id": "after",
         "kind": "adhoc", "series": None, "follows": "hsf-1210-1830-adhoc", "fill_ask_hours": 1.0},
    ],
}


def _member_file(store, uid) -> dict:
    return json.loads((store.root / GUILD / "members" / f"{uid}.json").read_text())


def test_default_becomes_an_ordinary_schedule(profile):
    cfg = R.GuildConfig(**{**base_config(), "schedules": [{"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "slots": ["Tue 04:00"]}]})
    lines, owed = R.lift_schedule_ids(cfg, profile.raids)
    assert cfg.schedules[0]["id"] == "barrow-deeps" and cfg.schedules[0]["name"] == R.DEFAULT_SCHEDULE_NAME
    assert cfg.schedule_aliases == {"barrow_deeps:default": "barrow-deeps"}
    assert owed and lines == ["Barrow Deeps · Raid nights is now barrow-deeps"]
    assert R.lift_schedule_ids(cfg, profile.raids) == ([], False)


def test_two_raids_sharing_an_id_are_made_unique(profile):
    cfg = R.GuildConfig(**{**base_config(), "schedules": [
        {"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}], "kind": "weekly", "slots": ["Tue 18:30"]},
        {"id": "tuesday", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "kind": "weekly", "slots": ["Tue 21:30"]}]})
    R.lift_schedule_ids(cfg, profile.raids)
    assert [e["id"] for e in cfg.schedules] == ["tuesday", "tuesday-2"]
    assert cfg.schedule_aliases["barrow_deeps:tuesday"] == "tuesday-2"


def test_a_lifted_id_is_trimmed_and_never_takes_a_later_schedules_id(profile):
    assert R.lifted_schedule_id("hyjal_summit_forever", set()) == "hyjal-summit-for"
    assert R.lifted_schedule_id("hyjal_summit_forever", {"hyjal-summit-for"}) == "hyjal-summit-f-2"
    cfg = R.GuildConfig(**{**base_config(), "schedules": [
        {"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 04:00"]},
        {"id": "barrow-deeps", "lineup": [{"raid": "onyxias_lair", "rosters": 1}], "kind": "weekly", "slots": ["Wed 20:00"]}]})
    R.lift_schedule_ids(cfg, profile.raids)
    assert [e["id"] for e in cfg.schedules] == ["barrow-deeps-2", "barrow-deeps"]


def test_the_oi_shape_keeps_its_confirm_by_and_its_start_line_names_the_schedule(old_store_with, open_registry):
    store = old_store_with(OI)
    reg = open_registry(store)
    s = reg.schedule_entry("barrow-deeps")
    assert (s["raid"], s["name"], s["slots"], s["confirm_hours_before"]) == (RID, "Raid nights", ["Tue 04:00"], 12)
    assert reg.schedule_def(RID, "barrow-deeps")["confirm_hours_before"] == 12
    said = _start(reg)
    assert ("info", "Schedules now have ids of their own: Barrow Deeps · Raid nights is now barrow-deeps.") in said
    assert not any("default" in text for _lvl, text in said)


def test_the_timing_line_names_an_old_default_by_its_name(old_store_with, open_registry):
    """Carried from phase 2's rehearsal: the start-up timing line once read "Barrow Deeps · default", a code word."""
    store = old_store_with({"raids": {RID: {"confirm_hours_before": 12}},
                            "schedules": [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 04:00"]}]})
    said = _start(open_registry(store))
    assert ("info", "Sheet timing now belongs to each schedule. Moved: Barrow Deeps · Raid nights: confirm-by 12 hours before.") in said


def test_the_big_guys_shape_changes_no_id_and_leaves_its_one_off_pair_alone(old_store_with, open_registry):
    store = old_store_with(BIG_GUYS)
    before = copy.deepcopy(BIG_GUYS["rosters"])
    reg = open_registry(store)
    assert [s["id"] for s in reg.all_schedules()] == ["tuesday"] and reg.ids_lifted == []
    assert reg.config.schedule_aliases == {"hyjal_summit_forever:tuesday": "tuesday"}
    assert [{k: t.get(k) for k in b} for t, b in zip(reg.config.rosters, before)] == before  # the lead and its follow-on, as they were
    said = _start(reg)
    assert not any("ids of their own" in text for _lvl, text in said)
    again = open_registry(store)
    assert again.config.schedule_aliases == {"hyjal_summit_forever:tuesday": "tuesday"} and not again._schedules_owed


def _start(reg) -> list[tuple[str, str]]:
    """The start hooks as the bot runs them: setup_hook's save, then ready's lines."""
    from oibot_gm.discord_bot import OibotGM

    said: list[tuple[str, str]] = []

    async def emit(cfg, level, text):
        said.append((level, text))

    bot = SimpleNamespace(registries=SimpleNamespace(ordered=lambda: [reg]), ops=SimpleNamespace(emit=emit))
    asyncio.run(OibotGM._schedules_save(bot))
    asyncio.run(OibotGM._schedules_migrate(bot))
    return said


def test_regulars_follow_their_schedule_and_save_once(old_store_with, open_registry):
    """A member file holding rosters ['barrow_deeps:default'] beside the oi-shaped guild.yaml: after load,
    reg.regulars('barrow-deeps') lists them before any save; migrate_schedules adds ONE commit holding guild.yaml and
    the member file; a second call adds none; reopening reads ['barrow-deeps'] from disk."""
    store = old_store_with(OI, members={NYX: ("Nyx", ["barrow_deeps:default"])})
    reg = open_registry(store)
    assert [m.display_name for m, _c in reg.regulars("barrow-deeps")] == ["Nyx"]
    assert _member_file(store, NYX)["characters"][0]["rosters"] == ["barrow_deeps:default"]  # nothing saved yet
    n0 = commit_count(store)
    reg.migrate_schedules()
    assert commit_count(store) == n0 + 1
    import subprocess

    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=store.root, check=True, capture_output=True, text=True).stdout.split()
    assert sorted(files) == sorted([f"{GUILD}/guild.yaml", f"{GUILD}/members/{NYX}.json"])
    reg.migrate_schedules()
    assert commit_count(store) == n0 + 1
    assert _member_file(store, NYX)["characters"][0]["rosters"] == ["barrow-deeps"]
    again = open_registry(store)
    assert again.members[NYX].main.rosters == ["barrow-deeps"] and not again._regulars_owed and not again._schedules_owed


def test_an_applicants_schedules_follow_too(old_store_with, open_registry):
    store = old_store_with(OI, applicants={ANA: ("Ana", ["barrow_deeps:default"])})
    reg = open_registry(store)
    assert reg.applicants[ANA].schedules == ["barrow-deeps"]
    reg.migrate_schedules()
    assert json.loads((store.root / GUILD / "applicants" / f"{ANA}.json").read_text())["schedules"] == ["barrow-deeps"]


def test_a_live_run_dict_is_remapped_and_a_test_one_is_not(old_store_with, open_registry):
    """run dicts {series: 'barrow_deeps:default', schedule_id: None} → {series: 'barrow-deeps', schedule_id: 'barrow-deeps'};
    a dict with test: True is byte-for-byte unchanged."""
    live = {"key": "bd-1215-0400", "name": "Barrow Deeps Tue 15 Dec 4:00 AM", "size": 10, "instance": RID, "kind": "recurring",
            "series": "barrow_deeps:default", "schedule_id": None, "fill_ask_hours": 4.0}
    test = {"key": "bd-1215-0400-test", "name": "Barrow Deeps Tue 15 Dec 4:00 AM", "size": 10, "instance": RID, "kind": "recurring",
            "series": "barrow_deeps:default", "schedule_id": "default", "test": True}
    store = old_store_with({**OI, "rosters": [live, test]})
    reg = open_registry(store)
    got = {t["key"]: t for t in reg.config.rosters}
    assert (got["bd-1215-0400"]["series"], got["bd-1215-0400"]["schedule_id"]) == ("barrow-deeps", "barrow-deeps")
    assert got["bd-1215-0400-test"] == test


def test_an_old_series_stamp_on_a_run_file_reads_through_the_aliases(old_store_with, open_registry):
    store = old_store_with(OI, members={NYX: ("Nyx", ["barrow_deeps:default"])})
    reg = open_registry(store)
    ev = rc.RaidEvent(key="bd-1215-0400-2026-12-15", team="bd-1215-0400", instance=RID, series="barrow_deeps:default", kind="recurring",
                      starts_at="2026-12-15T04:00:00-08:00")
    assert rc.run_series(reg, ev) == "barrow-deeps"
    assert [m.display_name for m, _c in rc.run_regulars(reg, ev)] == ["Nyx"]


def test_an_orphan_regular_key_is_dropped_and_named(old_store_with, open_registry):
    """rosters ['barrow_deeps:gone'] with no such schedule: dropped, and reg.ids_saved names the member."""
    store = old_store_with(OI, members={NYX: ("Nyx", ["barrow_deeps:gone", "barrow_deeps:default"])})
    reg = open_registry(store)
    assert reg.members[NYX].main.rosters == ["barrow-deeps"]
    reg.migrate_schedules()
    assert "Nyx: dropped a regular spot on a removed Barrow Deeps schedule" in reg.ids_saved
    assert _member_file(store, NYX)["characters"][0]["rosters"] == ["barrow-deeps"]


def test_the_raid_refuses_run_times(reg):
    with pytest.raises(RegistryError, match="belong to a schedule"):
        reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    with pytest.raises(RegistryError, match="belong to a schedule"):
        reg.set_raid_override(RID, "schedules", [], "t")
    assert R.RUN_TIMES_ON_SCHEDULE == "Run times belong to a schedule now. Add or change a schedule for this raid."


def test_a_schedule_is_addressed_by_its_id_alone(reg):
    sid = weekly(reg, RID, "Tue 19:30", "tue")
    reg.set_schedule("tue", {"slots": "Wed 19:30"}, "t")
    assert reg.schedule_entry("tue")["slots"] == ["Wed 19:30"] and reg.schedule_def(RID, "tue")["schedule"]["id"] == "tue"
    with pytest.raises(RegistryError, match="already called tue"):
        reg.set_schedule("tue", {"slots": "Thu 19:30"}, "t", raid="hyjal_summit_forever")
    with pytest.raises(RegistryError, match="no schedule called nope"):
        reg.set_schedule("nope", {"active": False}, "t")
    assert sid == "tue"
    with pytest.raises(RegistryError, match="no schedule called tue"):
        reg.schedule_def("hyjal_summit_forever", "tue")  # a schedule is its first raid's


def test_the_raids_whole_list_refuses_another_raids_id(reg):
    weekly(reg, "hyjal_summit_forever", "Tue 18:30", "tue")
    with pytest.raises(RegistryError, match="already called tue"):
        reg.set_raid_overrides(RID, {"schedules": [{"id": "tue", "kind": "weekly", "slots": ["Wed 20:00"]}]}, "t")
    assert [s["raid"] for s in reg.all_schedules()] == ["hyjal_summit_forever"]


def test_default_is_no_schedule_id_any_more(reg):
    with pytest.raises(RegistryError, match="kept for older files"):
        reg.set_schedule("default", {"kind": "weekly", "slots": "Tue 19:30"}, "t", raid=RID)


def test_removing_a_schedule_takes_its_regulars_off(reg):
    """weekly(reg) → regular_add(uid, sid) → remove_schedule(sid): the member's rosters no longer hold sid; one commit."""
    sid = weekly(reg)
    m = reg.test_members()[0]
    m.test = False  # a real member: puppets are never regulars
    reg.save(m, "real")
    reg.regular_add(m.discord_id, sid, "t")
    n0 = commit_count(reg.store)
    reg.remove_schedule(sid, "t")
    assert commit_count(reg.store) == n0 + 1
    assert sid not in reg.members[m.discord_id].main.rosters and reg.all_schedules() == []


def test_a_stale_schedule_rev_refuses(reg):
    """rev = reg.config_rev('schedule:tue'); another set_schedule('tue', …); set_schedule('tue', …, rev=rev) → ConfigChanged."""
    weekly(reg, RID, "Tue 19:30", "tue")
    rev = reg.config_rev("schedule:tue")
    raid_rev = reg.config_rev(f"raids:{RID}")
    reg.set_schedule("tue", {"name": "Main night"}, "t")
    assert reg.config_rev(f"raids:{RID}") == raid_rev  # the raid's section no longer covers its schedules
    with pytest.raises(ConfigChanged):
        reg.set_schedule("tue", {"name": "Late night"}, "t", rev=rev)


def test_a_regular_is_keyed_by_the_schedule_id(reg):
    sid = weekly(reg)
    weekly(reg, RID, "Thu 19:30", "thu")
    m = reg.test_members()[0]
    m.test = False
    reg.save(m, "real")
    _m, _c, warn = reg.regular_add(m.discord_id, sid, "t")
    assert reg.members[m.discord_id].main.rosters == [sid] and warn == []
    _m, _c, warn = reg.regular_add(m.discord_id, "thu", "t")
    assert len(warn) == 1 and "same raid's lockout" in warn[0]
    assert reg.series_label(sid) == "Barrow Deeps · Raid nights"
    assert reg.series_label("barrow_deeps:gone") == "barrow_deeps:gone (removed schedule)"


def test_lowering_ids_round_trips(old_store_with, open_registry):
    """lower_schedule_ids then lift_schedule_ids + remap gives back the same schedules, ids, aliases and member keys."""
    old = {"schedules": [*OI["schedules"],
                         {"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}], "kind": "weekly", "slots": ["Tue 18:30"]},
                         {"id": "tuesday", "lineup": [{"raid": RID, "rosters": 1}], "kind": "weekly", "slots": ["Tue 21:30"]}]}
    store = old_store_with(old, members={NYX: ("Nyx", ["barrow_deeps:default", "barrow_deeps:tuesday"])})
    reg = open_registry(store)
    reg.migrate_schedules()
    lifted = (copy.deepcopy(reg.config.schedules), dict(reg.config.schedule_aliases), list(reg.members[NYX].main.rosters))
    assert [e["id"] for e in lifted[0]] == ["barrow-deeps", "tuesday", "tuesday-2"] and lifted[2] == ["barrow-deeps", "tuesday-2"]
    cfg = reg.config.model_copy(deep=True)
    members = [reg.members[NYX].model_copy(deep=True)]
    lines = R.lower_schedule_ids(cfg, members)
    assert [e["id"] for e in cfg.schedules] == ["default", "tuesday", "tuesday"] and cfg.schedule_aliases == {}
    assert members[0].main.rosters == ["barrow_deeps:default", "barrow_deeps:tuesday"] and len(lines) == 2
    R.lift_schedule_ids(cfg, reg.base_profile.raids)
    assert (cfg.schedules, cfg.schedule_aliases) == lifted[:2]
    reg.config = cfg
    reg.members[NYX] = members[0]
    reg.remap_regulars()
    assert reg.members[NYX].main.rosters == lifted[2]


def test_lowering_refuses_a_lineup_the_older_code_cannot_read(profile):
    cfg = R.GuildConfig(**{**base_config(), "schedule_aliases": {}, "schedules": [
        {"id": "night", "lineup": [{"raid": RID, "rosters": 1}], "kind": "weekly", "slots": ["Tue 19:30"]}]})
    cfg.schedules[0]["lineup"] += [{"raid": "hyjal_summit_forever", "rosters": 1}, {"raid": "onyxias_lair", "rosters": 1}]
    with pytest.raises(RegistryError, match="night"):
        R.lower_schedule_ids(cfg, [])


def test_a_new_run_of_a_schedule_carries_its_id_as_its_series(reg, rs):
    sid = weekly(reg, RID, "Tue 19:30")
    start = rc.next_runs_of(reg, RID, reg.schedule_entry(sid), n=1)[0]
    t = rc.ensure_run(reg, RID, start, schedule=sid)
    assert (t["series"], t["schedule_id"], t["kind"]) == (sid, sid, "recurring")
    assert t["key"].endswith(f"-{sid}")
