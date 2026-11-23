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
    T = reg.timing_suggestion(RID)  # every stored schedule carries its sheet timing (phase 2)
    assert stored(reg, RID) == [{"id": "tue", "lineup": [{"raid": RID, "rosters": 2}, {"raid": OTHER, "rosters": 2}], "kind": "weekly", "slots": ["Tue 19:30"], **T}]
    assert not {"schedules", "slots"} & set(reg.config.raids.get(RID) or {})
    row = next(s for s in reg.schedules(RID) if s["id"] == "tue")
    assert row["rosters"] == 2 and row["follow"] == {"raid": OTHER, "rosters": 2}


def test_run_times_are_the_default_schedule(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    assert stored(reg, RID) == [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 19:30"], **reg.timing_suggestion(RID)}]
    assert reg.raid_def(RID)["slots"] == ["Tue 19:30"] and [s["id"] for s in reg.raid_def(RID)["schedules"]] == ["default"]  # its timing
    assert [s["id"] for s in reg.schedules(RID)] == ["default"]


def test_default_keeps_its_own_settings_without_run_times(reg):
    reg.set_schedule(RID, "default", {"slots": "Tue 19:30", "rosters": 2}, "t")
    reg.set_raid_override(RID, "slots", "", "t")
    assert stored(reg, RID) == [{"id": "default", "lineup": [{"raid": RID, "rosters": 2}], **reg.timing_suggestion(RID)}]
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


def _old_keys(reg):
    """A raid override as an older guild.yaml holds it: run times and a schedule under the raid."""
    reg.config.raids[RID] = {"notes": "old", "slots": ["Tue 19:30"], "schedules": [{"id": "alt", "kind": "weekly", "slots": ["Sat 20:00"]}, {"id": "keep", "kind": "pickup"}]}


def test_a_run_times_write_lifts_the_old_keys_and_keeps_what_they_held(reg):
    _old_keys(reg)
    reg.set_raid_override(RID, "slots", "Wed 20:00", "t")
    assert reg.config.raids[RID] == {"notes": "old"}
    assert reg.raid_def(RID)["slots"] == ["Wed 20:00"] and [s["id"] for s in reg.schedules(RID)] == ["default", "alt", "keep"]
    reg.set_raid_override(RID, "slots", "", "t")  # the profile has no run times: none left, and the old ones don't come back
    assert reg.raid_def(RID)["slots"] == [] and [s["id"] for s in reg.schedules(RID)] == ["alt", "keep"]


def test_a_schedule_write_lifts_the_old_keys_and_a_removed_one_stays_gone(reg):
    _old_keys(reg)
    reg.set_schedule(RID, "keep", {"name": "Pickups"}, "t")
    assert not {"schedules", "slots"} & set(reg.config.raids[RID])
    assert reg.raid_def(RID)["slots"] == ["Tue 19:30"] and [s["id"] for s in reg.schedules(RID)] == ["default", "alt", "keep"]
    reg.remove_schedule(RID, "alt", "t")
    reg.remove_schedule(RID, "keep", "t")
    assert [s["id"] for s in reg.schedules(RID)] == ["default"] and [s["id"] for s in reg.raid_def(RID)["schedules"]] == ["default"]
    assert reg.config.raids[RID] == {"notes": "old"}


def test_a_schedule_refused_before_the_lift_leaves_the_old_keys_where_they_were(reg):
    """Refused in schedule_value, before any raid write starts: the lift never runs."""
    _old_keys(reg)
    before = (copy.deepcopy(reg.config.raids), copy.deepcopy(reg.config.schedules))
    with pytest.raises(RegistryError):
        reg.set_schedule(RID, "alt", {"rosters": 9}, "t")
    assert (reg.config.raids, reg.config.schedules) == before


def test_a_follow_on_given_as_a_map_keeps_its_start_and_text_never_has_one():
    assert R.parse_follow({"raid": OTHER, "rosters": 2, "starts": 90}) == {"raid": OTHER, "rosters": 2, "starts": 90}
    assert R.parse_follow(f"{OTHER} x2") == {"raid": OTHER, "rosters": 2}
    for bad in (0, -5, "soon", True):
        with pytest.raises(RegistryError):
            R.parse_follow({"raid": OTHER, "starts": bad})


# ---- Task 3: an older guild.yaml is lifted when it is read, and saved once at start
import subprocess  # noqa: E402

import yaml  # noqa: E402

from conftest import GUILD, OFFICER_ROLE, OWNER  # noqa: E402
from oibot_gm.registry import Registry  # noqa: E402
from oibot_gm.store import GitStore  # noqa: E402

OLD = {
    "raids": {
        "hyjal_summit_forever": {"first_open": "2026-12-15T15:00:00-08:00",
                                 "schedules": [{"id": "tuesday", "kind": "weekly", "slots": ["Tue 18:30"], "follow": {"raid": "barrow_deeps", "rosters": 2}}]},
        "barrow_deeps": {"confirm_hours_before": 12, "slots": ["Tue 04:00"],
                         "schedules": [{"id": "default", "rosters": 2}]},
        "onyxias_lair": {"slots": ["Wed 20:00"]},
    },
}


def base_config() -> dict:
    """The minimal GuildConfig kwargs conftest's data_root writes."""
    return {"key": GUILD, "name": "Demo Guild", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": OWNER,
            "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE]}


def _write_old(data_root, cfg: dict) -> None:
    """conftest's guild.yaml with `cfg` on top, committed: a file as an older bot left it."""
    (data_root / GUILD / "guild.yaml").write_text(yaml.safe_dump({**base_config(), **copy.deepcopy(cfg)}, sort_keys=False))
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "old file"], cwd=data_root, check=True)


@pytest.fixture
def old_store(data_root):
    _write_old(data_root, OLD)
    return GitStore(data_root, push=False)


@pytest.fixture
def open_registry(profile):
    return lambda store: Registry(store, GUILD, profile)


def commit_count(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, check=True, capture_output=True, text=True).stdout)


def test_lifting_moves_every_raid_and_is_idempotent():
    cfg = R.GuildConfig(**{**base_config(), **copy.deepcopy(OLD)})
    lines = R.lift_schedules(cfg)
    assert len(lines) == 3
    assert cfg.schedules == [
        {"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}, {"raid": "barrow_deeps", "rosters": 2}], "kind": "weekly", "slots": ["Tue 18:30"]},
        {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 2}], "slots": ["Tue 04:00"]},
        {"id": "default", "lineup": [{"raid": "onyxias_lair", "rosters": 1}], "slots": ["Wed 20:00"]},
    ]
    assert cfg.raids == {"hyjal_summit_forever": {"first_open": "2026-12-15T15:00:00-08:00"}, "barrow_deeps": {"confirm_hours_before": 12}}
    assert R.lift_schedules(cfg) == [] and len(cfg.schedules) == 3


def test_an_old_file_reads_at_once(old_store, open_registry):
    reg = open_registry(old_store)
    assert [s["id"] for s in reg.schedules("hyjal_summit_forever")] == ["tuesday"]
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 04:00"]
    assert reg.schedules_lifted
    assert "Hyjal Summit: 1 schedule" in reg.schedules_lifted


def test_the_lift_is_committed_once(old_store, open_registry):
    reg = open_registry(old_store)
    n0 = commit_count(old_store)
    assert len(reg.migrate_schedules()) == 3 and commit_count(old_store) == n0 + 1
    assert reg.migrate_schedules() == [] and commit_count(old_store) == n0 + 1
    assert open_registry(old_store).schedules_lifted == []


def test_a_preview_copy_never_commits_the_lift(old_store, open_registry):
    reg = open_registry(old_store)
    n0 = commit_count(old_store)
    reg.shadow().migrate_schedules()
    assert commit_count(old_store) == n0
    assert reg.schedules_lifted  # the live registry still owes its save


def test_a_raid_rev_is_the_same_before_and_after_the_lift_is_saved(old_store, open_registry):
    reg = open_registry(old_store)
    revs = {rid: reg.config_rev(f"raids:{rid}") for rid in OLD["raids"]}
    reg.migrate_schedules()
    again = open_registry(old_store)
    assert {rid: again.config_rev(f"raids:{rid}") for rid in OLD["raids"]} == revs


def test_a_raid_that_cannot_be_lifted_stays_as_it_was(data_root, open_registry):
    bad = copy.deepcopy(OLD)
    bad["raids"]["barrow_deeps"]["schedules"].append({"id": "Not A Key!", "kind": "weekly"})
    _write_old(data_root, bad)
    reg = open_registry(GitStore(data_root, push=False))
    assert reg.config.raids["barrow_deeps"] == bad["raids"]["barrow_deeps"]
    assert [s["id"] for s in reg.schedules("hyjal_summit_forever")] == ["tuesday"]  # the other raids moved
    assert not [e for e in reg.config.schedules if e["lineup"][0]["raid"] == "barrow_deeps"]
    with pytest.raises(RegistryError) as refused:
        R.norm_schedule({"id": "Not A Key!", "kind": "weekly"})
    assert not [x for x in reg.schedules_lifted if "Barrow Deeps" in x]  # a refusal is never told as a move
    assert len(reg.schedules_refused) == 1 and "Barrow Deeps" in reg.schedules_refused[0]
    assert str(refused.value) in reg.schedules_refused[0]
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 04:00"]  # it still reads as before
    assert reg.raid_def("barrow_deeps")["schedules"] == bad["raids"]["barrow_deeps"]["schedules"]


def test_a_stored_entry_that_cannot_be_read_is_skipped_and_named(data_root, open_registry):
    good = {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "slots": ["Tue 19:30"]}
    broken = {"id": "late", "lineup": [{"raid": "barrow_deeps", "rosters": 9}], "kind": "weekly", "slots": ["Wed 20:00"]}
    _write_old(data_root, {"schedules": [good, broken]})
    reg = open_registry(GitStore(data_root, push=False))
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 19:30"]
    assert [s["id"] for s in reg.schedules("barrow_deeps")] == ["default"]
    assert len(reg.schedules_unread) == 1 and "late" in reg.schedules_unread[0] and "Barrow Deeps" in reg.schedules_unread[0]
    reg.set_raid_override("barrow_deeps", "slots", "Thu 20:00", "t")  # a write of that raid leaves it where it was
    assert broken in reg.config.schedules
    assert reg.raid_def("barrow_deeps")["slots"] == ["Thu 20:00"]


def test_the_start_lifts_each_guild_and_says_so_once(data_root, open_registry):
    import asyncio
    from types import SimpleNamespace

    from oibot_gm.discord_bot import OibotGM

    broken = {"id": "late", "lineup": [{"raid": "barrow_deeps", "rosters": 9}]}
    _write_old(data_root, {**OLD, "schedules": [broken]})
    reg = open_registry(GitStore(data_root, push=False))
    said: list[tuple[str, str]] = []

    async def emit(cfg, level, text):
        said.append((level, text))

    bot = SimpleNamespace(registries=SimpleNamespace(ordered=lambda: [reg]), ops=SimpleNamespace(emit=emit))
    asyncio.run(OibotGM._schedules_migrate(bot))
    asyncio.run(OibotGM._schedules_migrate(bot))
    assert said[0] == ("info", "Schedules are now stored at guild level: Hyjal Summit: 1 schedule · Barrow Deeps: 1 schedule · Onyxia's Lair: 1 schedule.")
    assert said[1] == ("info", TIMING_MOVED)  # phase 2: the raid-level timing went onto its schedule in the same save
    assert said[2][0] == "warn" and "late" in said[2][1]
    assert len(said) == 3  # the warning is told once a start, not on every call


TIMING_MOVED = "Sheet timing now belongs to each schedule. Moved: Barrow Deeps · default: confirm-by 12 hours before."


def _start(reg) -> list[tuple[str, str]]:
    """The start hook once, as tests/test_recipe_identity.py drives _recipes_migrate: the (level, text) lines it sends."""
    import asyncio
    from types import SimpleNamespace

    from oibot_gm.discord_bot import OibotGM

    said: list[tuple[str, str]] = []

    async def emit(cfg, level, text):
        said.append((level, text))

    asyncio.run(OibotGM._schedules_migrate(SimpleNamespace(registries=SimpleNamespace(ordered=lambda: [reg]), ops=SimpleNamespace(emit=emit))))
    return said


BAD_ROW = {"id": "Not A Key!", "kind": "weekly"}


def _refusal() -> str:
    with pytest.raises(RegistryError) as refused:
        R.norm_schedule(BAD_ROW)
    return str(refused.value)


def test_a_file_whose_only_old_raid_is_refused_says_no_move_and_commits_nothing(data_root, open_registry):
    _write_old(data_root, {"raids": {"barrow_deeps": {"slots": ["Tue 04:00"], "schedules": [BAD_ROW]}}})
    store = GitStore(data_root, push=False)
    reg = open_registry(store)
    n0 = commit_count(store)
    said = _start(reg)
    assert commit_count(store) == n0
    assert not [t for _, t in said if t.startswith("Schedules are now stored at guild level")]
    assert len(said) == 1 and said[0][0] == "warn" and "Barrow Deeps" in said[0][1] and _refusal() in said[0][1]
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 04:00"]


def test_a_moved_raid_and_a_refused_one_are_told_apart(data_root, open_registry):
    _write_old(data_root, {"raids": {"onyxias_lair": {"slots": ["Wed 20:00"]}, "barrow_deeps": {"slots": ["Tue 04:00"], "schedules": [BAD_ROW]}}})
    said = _start(open_registry(GitStore(data_root, push=False)))
    assert ("info", "Schedules are now stored at guild level: Onyxia's Lair: 1 schedule.") in said
    warns = [t for lv, t in said if lv == "warn"]
    assert len(warns) == 1 and "Barrow Deeps" in warns[0] and _refusal() in warns[0] and "Onyxia" not in warns[0]


def test_a_stored_entry_that_is_not_a_mapping_is_kept_skipped_and_named(data_root, open_registry):
    good = {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "slots": ["Tue 19:30"]}
    _write_old(data_root, {"schedules": ["just some words", good]})
    store = GitStore(data_root, push=False)
    reg = open_registry(store)
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 19:30"]
    assert len(reg.schedules_unread) == 1
    reg.set_raid_override("barrow_deeps", "slots", "Thu 20:00", "t")
    on_disk = yaml.safe_load((data_root / GUILD / "guild.yaml").read_text())["schedules"]
    assert "just some words" in on_disk and reg.raid_def("barrow_deeps")["slots"] == ["Thu 20:00"]
    warns = [t for lv, t in _start(reg) if lv == "warn"]
    assert len(warns) == 1 and "can't be read" in warns[0]


def test_a_write_refused_after_the_lift_puts_the_old_keys_back(reg):
    """The lift runs (old keys → rows), both fields parse, and the whole-raid check refuses the pair: the override and
    the guild-level list are exactly as they were, old keys included."""
    _old_keys(reg)
    before = (copy.deepcopy(reg.config.raids), copy.deepcopy(reg.config.schedules))
    with pytest.raises(RegistryError, match="min 5 above max 1"):
        reg.set_raid_overrides(RID, {"tank_min": 5, "tank_max": 1}, "t")
    assert (reg.config.raids, reg.config.schedules) == before


# ---- the final review's fixes
@pytest.mark.parametrize("field", ["rosters", "starts"])
def test_a_unicode_digit_in_a_lineup_is_refused_in_words(field):
    lineup = [{"raid": "a", "rosters": 1}, {"raid": "b", "rosters": 1}]
    lineup[1][field] = "²"
    with pytest.raises(RegistryError):
        R.parse_lineup(lineup)


def test_a_unicode_digit_as_a_follow_on_start_is_refused_in_words():
    with pytest.raises(RegistryError):
        R.parse_follow({"raid": OTHER, "starts": "²"})


def test_a_stored_entry_with_a_unicode_digit_is_skipped_at_load(data_root, open_registry):
    good = {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "slots": ["Tue 19:30"]}
    odd = {"id": "late", "lineup": [{"raid": "barrow_deeps", "rosters": "²"}], "kind": "weekly", "slots": ["Wed 20:00"]}
    _write_old(data_root, {"schedules": [good, odd]})
    reg = open_registry(GitStore(data_root, push=False))
    assert [s["id"] for s in reg.schedules("barrow_deeps")] == ["default"]
    assert len(reg.schedules_unread) == 1 and "late" in reg.schedules_unread[0]


def test_editing_a_raid_keeps_its_block_before_a_later_raid(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    reg.set_schedule(OTHER, "wed", {"kind": "weekly", "slots": "Wed 20:00"}, "t")
    reg.set_schedule(RID, "tue", {"name": "Tuesday"}, "t")
    reg.set_schedule(RID, "sat", {"kind": "weekly", "slots": "Sat 20:00"}, "t")
    assert [(e["lineup"][0]["raid"], e["id"]) for e in reg.config.schedules] == [(RID, "tue"), (RID, "sat"), (OTHER, "wed")]


NOTHING_TO_MOVE = {"raids": {"barrow_deeps": {"notes": "kept", "schedules": None}, "onyxias_lair": {"slots": []}}}


def test_a_raid_with_nothing_to_move_is_no_line():
    cfg = R.GuildConfig(**{**base_config(), **copy.deepcopy(NOTHING_TO_MOVE)})
    raids = {"barrow_deeps": {"name": "Barrow Deeps"}, "onyxias_lair": {"name": "Onyxia's Lair"}}  # the game gives neither run times
    assert R.lift_schedules(cfg, raids) == []
    assert cfg.raids == {"barrow_deeps": {"notes": "kept"}} and cfg.schedules == []


def test_a_raid_with_nothing_to_move_is_still_saved_once_and_never_told(data_root, open_registry):
    _write_old(data_root, NOTHING_TO_MOVE)
    store = GitStore(data_root, push=False)
    reg = open_registry(store)
    n0 = commit_count(store)
    said = _start(reg)
    assert commit_count(store) == n0 + 1 and said == []
    doc = yaml.safe_load((data_root / GUILD / "guild.yaml").read_text())
    assert doc["raids"] == {"barrow_deeps": {"notes": "kept"}}
    assert _start(open_registry(store)) == [] and commit_count(store) == n0 + 1


def test_a_refused_raid_says_how_to_get_out_in_two_plain_sentences(data_root, open_registry):
    _write_old(data_root, {"raids": {"barrow_deeps": {"slots": ["Tue 04:00"], "schedules": [BAD_ROW]}}})
    said = _start(open_registry(GitStore(data_root, push=False)))
    (level, text), = said
    assert level == "warn" and _refusal() in text and "(" + _refusal() not in text
    assert text.count(". ") == 1 and text.endswith(".")
    assert "by hand" in text and "reset" in text


def _last_subject(root) -> str:
    return subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root, check=True, capture_output=True, text=True).stdout


def test_the_lift_is_saved_before_anything_else_at_start(old_store, open_registry):
    """setup_hook saves the lift first, under its own message, before the web, the scheduler or ready can save; the
    ready hook then tells it."""
    import asyncio
    import inspect
    from types import SimpleNamespace

    from oibot_gm.discord_bot import OibotGM

    src = inspect.getsource(OibotGM.setup_hook)
    first = src.index("_schedules_save")
    assert all(first < src.index(x) for x in ("tree.sync", "scheduler()", "web_serve"))
    reg = open_registry(old_store)
    said: list[tuple[str, str]] = []

    async def emit(cfg, level, text):
        said.append((level, text))

    bot = SimpleNamespace(registries=SimpleNamespace(ordered=lambda: [reg]), ops=SimpleNamespace(emit=emit))
    asyncio.run(OibotGM._schedules_save(bot))
    assert "schedules moved to the guild level" in _last_subject(old_store.root) and said == []
    reg.save_config("something else")  # ready's officer roles, say: nothing of the lift is left for it to carry
    asyncio.run(OibotGM._schedules_migrate(bot))
    assert said == [("info", "Schedules are now stored at guild level: Hyjal Summit: 1 schedule · Barrow Deeps: 1 schedule · Onyxia's Lair: 1 schedule."),
                    ("info", TIMING_MOVED)]


# ---- going back to older code: scripts/lower_schedules.py
OLD_FOLLOW = {"raids": {"barrow_deeps": {"notes": "x", "slots": ["Tue 19:30"],
                                         "schedules": [{"id": "default", "rosters": 2, "lock_hours_before": 20},
                                                       {"id": "alt", "kind": "weekly", "slots": ["Sat 20:00"], "rosters": 3,
                                                        "follow": {"raid": "hyjal_summit_forever", "rosters": 2}}]}}}


@pytest.mark.parametrize("old", [OLD, OLD_FOLLOW], ids=["plan", "follow-on"])
def test_lift_then_lower_gives_back_the_old_shape(old):
    cfg = R.GuildConfig(**{**base_config(), **copy.deepcopy(old)})
    lifted = R.lift_schedules(cfg)
    lowered = R.lower_schedules(cfg)
    assert cfg.raids == old["raids"] and cfg.schedules == []
    assert [x.split(":")[0] for x in lowered] == [x.split(":")[0] for x in lifted]


def test_lowering_an_unreadable_entry_refuses_and_changes_nothing():
    good = {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 1}], "slots": ["Tue 19:30"]}
    broken = {"id": "late", "lineup": [{"raid": "barrow_deeps", "rosters": 9}]}
    cfg = R.GuildConfig(**{**base_config(), "raids": {"barrow_deeps": {"notes": "x"}}, "schedules": [good, broken]})
    before = (copy.deepcopy(cfg.raids), copy.deepcopy(cfg.schedules))
    with pytest.raises(RegistryError, match="late"):
        R.lower_schedules(cfg)
    assert (cfg.raids, cfg.schedules) == before


def _lower_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "lower_schedules.py"
    spec = importlib.util.spec_from_file_location("lower_schedules", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _untimed(raids: dict) -> dict:
    """The raids with every sheet timing value taken out, of the raid and of each of its schedules (a `default` row
    left with nothing but its id is gone, as it was before it carried timing)."""
    out = {}
    for rid, o in raids.items():
        o = {k: v for k, v in o.items() if k not in R.SCHEDULE_TIMING}
        rows = [{k: v for k, v in r.items() if k not in R.SCHEDULE_TIMING} for r in o.pop("schedules", None) or []]
        rows = [r for r in rows if r != {"id": "default"}]
        if rows:
            o["schedules"] = rows
        out[rid] = o
    return out


def test_the_script_lowers_each_guild_once_and_this_code_lifts_it_again(old_store, open_registry, data_root):
    reg = open_registry(old_store)
    reg.migrate_schedules()
    lifted = copy.deepcopy(reg.config.schedules)
    script = _lower_script()
    n0 = commit_count(old_store)
    dry = script.lower(data_root, dry_run=True)
    assert len(dry) == 1 and GUILD in dry[0] and commit_count(old_store) == n0
    assert yaml.safe_load((data_root / GUILD / "guild.yaml").read_text())["schedules"] == lifted
    said = script.lower(data_root)
    assert len(said) == 1 and GUILD in said[0] and commit_count(old_store) == n0 + 1
    doc = yaml.safe_load((data_root / GUILD / "guild.yaml").read_text())
    assert "schedules" not in doc and _untimed(doc["raids"]) == _untimed(OLD["raids"])  # phase 2: timing rides on each schedule
    assert not any(k in o for o in doc["raids"].values() for k in R.SCHEDULE_TIMING)
    assert all(set(R.SCHEDULE_TIMING) <= set(row) for o in doc["raids"].values() for row in o.get("schedules") or [])
    assert doc["raids"]["barrow_deeps"]["schedules"][0]["confirm_hours_before"] == 12
    assert "under each raid" in _last_subject(data_root)
    again = open_registry(GitStore(data_root, push=False))
    assert again.config.schedules == lifted and again.schedules_lifted
    assert len(script.lower(data_root)) == 1 and commit_count(old_store) == n0 + 1  # nothing left to lower: no commit


def test_the_script_refuses_a_guild_with_an_unreadable_entry(data_root):
    broken = {"id": "late", "lineup": [{"raid": "barrow_deeps", "rosters": 9}]}
    _write_old(data_root, {"schedules": [broken]})
    text = (data_root / GUILD / "guild.yaml").read_text()
    store = GitStore(data_root, push=False)
    n0 = commit_count(store)
    said = _lower_script().lower(data_root)
    assert len(said) == 1 and "late" in said[0] and "nothing" in said[0].lower()
    assert (data_root / GUILD / "guild.yaml").read_text() == text and commit_count(store) == n0
