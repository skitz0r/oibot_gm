# Schedules and events with their own identity (phase 3) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A schedule is addressed by a guild-unique id and holds its regulars; an event has an identity that is not its
time, can be rescheduled and re-timed on its own, runs a lineup of up to four raids, and can have its lineup edited
before the lock.

**Architecture:** Same pattern as phases 1 and 2: the stored shape moves at load (pure lift functions on `GuildConfig`,
in-memory remaps of member files), saved once at start in ONE `store.batch` per guild together with the stamps on live
run files. The follow-on pair (§5.62) is generalised to a chain of N parts: one run file and one run dict per lineup
entry, `follows` = the FIRST part's key on every later part, `followed_by` = the NEXT part's key, plus `event`, `part`,
`starts` stamps. The scheduler finds a schedule's event by its stamped `(schedule, occurrence)`, never by a key; new
keys carry no time. Reschedule, re-timing and Edit lineup are new verbs on `RaidMixin` with one confirm generator each.

**Tech Stack:** Python 3.12, Pydantic, FastAPI, discord.py, FastMCP, React + Mantine (Vite), pytest.

**Spec:** `docs/superpowers/specs/2026-11-22-raids-events-schedules-design.md` — §9 item 3, bound by D3, D5, D6, D7, D10,
C2, C4, C8, C9, C10, C11, C12, C13, C16, C17, C18, §2, §4, §5 (Storage, Identity, Regulars, Per-event timing, Migration
steps 3–5), §6, and the plain-text / MCP bullets of §7. Starts from the state phase 2 leaves
(`docs/superpowers/plans/2026-11-23-raids-without-times.md`: `SCHEDULE_TIMING`, `effective_raid`, `write_out_timing`,
`lift_timing`, minimums, officers on the four raid / schedule ops, `scripts/lower_schedules.py --minimums-only`). Phase 1:
`docs/superpowers/plans/2026-11-22-guild-level-schedules.md`, `docs/design.md` §5.67. Read `CLAUDE.md` first.

## Global constraints

- **Schedule ids** are guild-unique, `^[a-z0-9][a-z0-9_-]{0,15}$` (the existing `_SCHED_ID_RE`), never containing `:`.
  `adhoc`, `test` and `after` stay reserved (old run dicts carry them). There is no `default` schedule and no
  `DEFAULT_SCHEDULE` constant after Task 1; the profile's raid `slots` are no longer read (every profile has `[]`).
- **Regulars and series** are keyed by the schedule id: `RegisteredCharacter.rosters`, `Applicant.schedules`, run dicts'
  and run files' `series` hold `<schedule id>`. An old `"<raid>:<schedule>"` string is translated only through
  `GuildConfig.schedule_aliases` (written by the lift), never split.
- **Event keys.** A new run's key is `f"{abbr(first raid of that part)}-{6 chars}"`, the 6 chars drawn with `secrets.choice`
  from `KEY_ALPHABET = "23456789abcdefghjkmnpqstuvwxyz"`, unique against `config.rosters` keys and `rs.events`; the run
  dict key and the run file key are the SAME string for new runs. Old keys are opaque ids. After Task 3, `run_key`,
  `event_key`, `schedule_kw`, `RunKeyClash`, `TEST_SCHEDULE`'s key space and `FOLLOW_SCHEDULE`'s key space do not exist,
  and no code reads a raid, a date, a time or a schedule out of a run key or derives a key from a time
  (`tests/test_event_identity.py::test_nothing_builds_or_parses_a_run_key` pins it).
- **Stamps on every run file and run dict** (new fields, defaults for old files): `schedule: str | None` (the schedule id;
  None = a one-off), `occurrence: str | None` (the start the schedule gave it, GUILD WALL TIME `"YYYY-MM-DDTHH:MM"`, naive —
  so a timezone change never makes a duplicate), `event: str | None` (the first part's key), `part: int = 0`,
  `starts: int = 0` (minutes after part 0's start). `follows` (on parts ≥ 1) = the first part's key; `followed_by` = the
  next part's key. The first part owns the sheet, answers and timing.
- **Lineups**: `LINEUP_MAX = 4`; one raid per step (D10), 1–4 rosters each, no raid twice; a later entry's default
  `starts` = the previous entry's `starts` + the previous raid's `duration_hours` × 60 (C12); entry 0 never has `starts`.
- **Who may:** officers, live (`PRIV_LIVE`), for every new verb (C17). The run ops stay in group `runs`.
- **No DM** from any verb here (C4, the DM rule). A move writes a `moved` notice to everyone with a row on the first part
  (`Registry.notify`), edits the posted sheet, the officer cards and every OPEN confirmation card in place.
- **Words.** "run" stays the word people read (phase 6). Copy, Confirm, Clock, Wizard and Iconography rules apply to every
  new string; `tests/test_copy.py`, `tests/test_confirm_readability.py`, `tests/test_quiet_edges.py` must pass.
- **Exact sentences (refusals; every surface prints these):**
  - `RUN_TIMES_ON_SCHEDULE = "Run times belong to a schedule now. Add or change a schedule for this raid."`
  - `NO_SCHEDULE = "There is no schedule called {sid}."`
  - `ID_TAKEN = "Another schedule is already called {sid}. Pick another id."`
  - `MOVE_PAST = "That start is in the past. Pick a time from now on."`
  - `MOVE_CLOSED = "This run is finished or cancelled, so it can't move."`
  - `MOVE_CLASH = "{raid} already has a run at {when}. Pick another time."`
  - `MOVE_SAME = "The run already starts then."`
  - `TIMING_LOCKED = "The sheet timing is fixed once the roster locks. Move the start instead."`
  - `TIMING_ORDER = "The reminder has to come before the lock, and confirm-by after it."`
  - `LINEUP_LOCKED = "The lineup is fixed once the roster locks."`
  - `LINEUP_FIRST = "The first raid of a run can't change. Start a new run instead."`
  - `LINEUP_MAX_WORDS = "A run holds at most 4 raids."`
  - `LATER_PART_LOCK = "Lock {first} first. Its lock builds every raid of the run."`
- `docs/manual.md` changes in the same commit as the behaviour. Real guild data is never committed; the live data repo is
  read-only. Frontend changes rebuild and commit the bundle (`cd frontend && npm run build`,
  `uv run python scripts/check_bundle.py`). Commits on the work branch; merging, pushing and restarting are the owner's.
- Test helper names used below (`officer_client`, `member_client`, `harness_officer`, `bot` — the bare RaidMixin test
  client `tests/test_raid_night.py` builds, with `bot.dms` recording DMs —, `start_in(reg, days)`, `open_one_off`,
  `freezer`, `old_store_with`, `mcp_tool_names`) stand for the suite's existing fixtures; where one does not exist, the
  task that first needs it adds it to `tests/conftest.py`, keeping the assertions as written.

## Review focus

1. **An event moved across its schedule's next occurrence** (Tuesday's run moved to the next Monday, and another moved to
   the Wednesday after the next Tuesday): the next Tuesday still opens on time, the slot it left never opens again, and no
   second sheet appears. (Task 5 `test_a_moved_run_never_blocks_or_repeats_an_occurrence`.)
2. **A moved event and lockout checks:** a run moved into the next lockout window counts in THAT window for
   `saved_this_lockout`, `last_window` and conflicts. (Task 5 `test_lockout_and_conflicts_read_the_moved_start`.)
3. **A chain of 3–4 parts at lock with a short role:** the first part locks, a later part short of a tank still locks
   with whoever can play, a part whose solve fails stays open with its `lock_error` and every part after it waits; the
   scheduler retries in order. (Task 4 `test_four_raids_lock_in_order_and_a_short_role_never_stops_one`.)
4. **An old key that looks parseable:** `bd-1215-1830-tuesday-2026-12-15` whose `starts_at` is 16 Dec: nothing reads 15 Dec
   from the key; the 15 Dec occurrence is found (or not) by stamps alone. (Task 3 `test_an_old_key_is_never_read_for_its_time`.)
5. **A restart between stamping parts:** the run dicts of a 3-part event are saved, only the first run file is; the next
   scheduler minute adds the missing parts once, with no second sheet. The start-up stamp run twice changes nothing.
   (Task 4 `test_an_open_cut_off_between_parts_is_completed_once`, Task 3 `test_stamping_twice_changes_nothing`.)

---

## Where things are (before phase 2 lands; find the name, the line will have moved)

- `src/oibot_gm/registry.py`: `NOTICE_KINDS` :38, `RegisteredCharacter.rosters` :148, `Applicant.schedules` :323,
  `GuildConfig.schedules` :467, `DEFAULT_SCHEDULE` … `ADHOC_SCHEDULE` :862–885, `series_key` / `parse_series` :911–921,
  `parse_follow` :956, `LINEUP_MAX` / `parse_lineup` :989–1020, `to_stored_schedule` / `from_stored_schedule` :1023–1048,
  `_raid_rows_of` … `lower_schedules` :1063–1185, `norm_schedule` :1269, `schedules_of` :1298, `schedule_def_of` :1316,
  `schedule_label` :2070, `timezone_runs` :2143, `migrate_schedules` :2270, `raid_def` :4488, `raid_override` :4524,
  `schedules` / `schedule_def` / `set_schedule` :4610–4668, `schedule_regulars` … `remove_schedule` :4670–4740,
  `_check_schedules` :4876, `series` … `regular_remove` :5914–5998, `series_coverage` :6001, `notify` :6143.
- `src/oibot_gm/raidcycle.py`: `RaidEvent` :156, `run_team` :309, `follow_start` … `lead_name_of` :328–383, `abbr` :385,
  `run_key` / `event_key` / `schedule_kw` :402–418, `_weekly_starts` / `_lockout_starts` / `schedule_starts` / `upcoming` /
  `next_runs_of` :440–519, `series_forecast` :525, `ensure_run` :580, `closed_run_refusal` :661, `open_run` :674,
  `move_runs_with_clock` :841, `RaidStore` :891 (`save` → `sync_follow`), `open_event` :1027, `prefill` :1058,
  `sync_follow` :1070, `run_kind` / `run_series` :1096–1119, `callout` / `leave_follow` / `stay_for_follow` :1316–1368,
  `conflicts` :1370, `saved_this_lockout` :1502, `last_window` :2334, `_solve_run` :2548 (LEAD_SEAT_BONUS),
  `free_seat` :2848, `which_run` / `open_when` / `open_preview` / `open_row_refusal` / `upcoming_rows` :3126–3283.
- `src/oibot_gm/raid_scheduler.py`: `_warn_unopened` :103, `_warn_key_clash` :116, open loop :170–186, follow-on skip :192.
- `src/oibot_gm/discord_raid.py`: `edit_ask_card` :217, `refresh_cards` :266, `refresh_sheet` :387, `cleanup_ephemeral`
  ~:790, `lock_run` :846 / `lock_follow` :940, confirmation send ~:960–1000 (`confirm_layout`, `ask["dm"]`),
  `after_lead_out` :1177, `drop_seated` :1314, `open_run_and_post` :1334, test open :1360–1372, `cancel_run` :1382.
- `src/oibot_gm/raid_views.py`: `run_times` :78, sheet title "…, then …" ~:136, lock card "· after" ~:661, `confirm_layout` :704.
- `src/oibot_gm/configops.py`: schema text :120–150, `OWNER_OPS` :446, `RUN_OPS` :455, `OP_GROUP` :466,
  `BY_DESIGN_EXEMPT` / `PARITY_PENDING` :498–516, `_open_time` ~:818, `follow_of_op` ~:828, `_schedule_ref` ~:843,
  `coalesce` ~:994, `_live_event` :1362, `_describe_action` run_open :2338, apply run_open :3165, `_schedule_target` :3632,
  `ops_from_fields` schedule kinds :3690.
- `src/oibot_gm/web/api.py`: `schedule_choice_label` :128, `schedule_json` :845, `ev_json` :943, readiness forecast
  `open_key` :1185, `/api/raid/{rid}/open/preview` :1694, `/api/raid/{rid}/open` :1716, run routes :1264–1819.
  `src/oibot_gm/web/api_ops.py` schedule routes :354–372. `src/oibot_gm/mcp_server.py` `open_run` :460, `cancel_run` :582,
  `schedule_set` :664, `schedule_remove` :686.
- Frontend: `components/ScheduleCard.tsx` (targets `${r.id}:${s.id}` :48, :55, :201, :214; follow-on editor :234–237),
  `components/board/SheetCard.tsx` (card actions; `CancelModal` :215), `pages/Rosters.tsx` (regulars by `s.key`), `api.ts`.
- Live data shape (read 2026-11-23): big-guys-for `schedules: [{id: tuesday, lineup: [hyjal_summit_forever ×1], slots: [Tue 18:30]}]`,
  run dicts `hsf-1210-1830-adhoc` (`schedule_id: adhoc`, `follow`, `followed_by: bd-1210-2130-after`) and
  `bd-1210-2130-after` (`schedule_id: after`, `follows`), run files `…-2026-12-10.json` (open, 18 signups, `message_id` set);
  oi `schedules: [{id: default, lineup: [barrow_deeps ×1], slots: [Tue 04:00]}]`, no live run; no regulars anywhere.

## Key-parsing and key-deriving sites this plan removes (every one, by task)

Series keys split on `:` (Task 1 / Task 2): `registry.parse_series`, `registry.series_label`, `registry.regular_add`
(lockout twin warning), `raidcycle.series_forecast`, `flows_roster` (:702–703 twin, :835 `key.split(":")`),
`wizard_opts` (:90), `help` (:464), `configops` (:2431 twin, `_schedule_target` :3632), `consequences.schedule_names` /
`series_words` / `regular_remove` (:262, :275), `web/api.schedule_choice_label` (:130), `/api/rosters` orphans (:1130),
`/api/readiness` forecast (:1186).
Run keys built from a time (Task 3 / Task 4): `raidcycle.run_key`, `event_key`, `schedule_kw`, `ensure_run` (key and the
follow-on's `after` key), `open_run` (lead and follow-on keys), `open_event` (key = team + date; `follows` rebuilt from
a date), `which_run`, `open_preview`, `upcoming_rows`, `open_row_refusal`; `raid_scheduler` `_warn_unopened`,
`_warn_key_clash` and the open loop; `configops` upcoming list (:294), run_open describe (:2342) and apply (:3168);
`flows_raid` (:301, :326, :348); `help` (:805); `web/api` readiness `open_key` (:1189) and the open route's `key` check
(:1734); `discord_raid` test open (:1370).
Kept, because they read a council's own suffix and never a raid, date or schedule: `web/api_loot.run_of`
(`(.+)-r\d+`), `loot_ops._store_sessions`, `discord_loot` companion `import_id.rsplit("-", 1)`.

---

### Task 1: Guild-unique schedule ids, no `default`, regulars keyed by id

**Files:**
- Modify: `src/oibot_gm/registry.py`, `src/oibot_gm/raidcycle.py` (only the `DEFAULT_SCHEDULE` reads and `series_key`
  calls), `src/oibot_gm/configops.py` / `src/oibot_gm/flows_config.py` (only to call the new verb signatures with
  `raid=`), `src/oibot_gm/discord_bot.py` (`_schedules_save`, `_schedules_migrate`), `scripts/lower_schedules.py`
- Test: `tests/test_schedule_ids.py` (new); `tests/conftest.py` gains `weekly(reg, raid="barrow_deeps", slots="Tue 19:30",
  sid=None) -> str`; every test that wrote a raid's `slots` or used `"<raid>:default"` / `series_key` (about 39 files:
  `grep -rlE "set_raid_override[s]?\([^)]*slots|\"slots\":|:default|series_key\(" tests`) calls `weekly(...)` and uses the id
- Modify: `docs/manual.md` (§1a: a schedule has its own id; a raid's own run times are an ordinary schedule; regulars
  belong to a schedule)

**Interfaces:**
- Consumes: phase 2's `lift_timing`, `lift_minimums`, `timing_saved`, `write_out_timing`.
- Produces:
  - `GuildConfig.schedule_aliases: dict[str, str] = {}` — `"<raid>:<old id>"` → new id, for every schedule the lift saw.
  - `lifted_schedule_id(raid: str, taken: set[str]) -> str` — `raid.replace("_", "-")[:16].rstrip("-")`, then `-2`, `-3` …
    (trimmed to 16) while taken. `hyjal_summit_forever` → `hyjal-summit-for`, `barrow_deeps` → `barrow-deeps`.
  - `lift_schedule_ids(cfg: GuildConfig, raids: dict) -> tuple[list[str], bool]` → `(lines, owed)`. Readable entries in
    stored order: a `default` entry becomes `lifted_schedule_id(its raid)` with `name` written out as
    `DEFAULT_SCHEDULE_NAME` unless it has its own and `kind: weekly`; an id already taken by an earlier entry becomes
    `<id>-2` …; every entry's `"<raid>:<old id>"` → new id goes into `schedule_aliases` (kept, never overwritten). Every
    `cfg.rosters` dict without `test`: `series` and `schedule_id` (None / `default` → the raid's lifted id) through the
    aliases. A line per rename: `"Barrow Deeps · Raid nights is now barrow-deeps"`. Idempotent: no `default`, no
    duplicate, aliases complete → `([], False)`. Called by `load_config` after phase 2's lifts.
  - `Registry.remap_regulars() -> list[str]` — at load, in memory: every member character's `rosters` and every
    applicant's `schedules` through `schedule_aliases`; a key with `:` and no alias is dropped and named
    (`"Nyx: dropped a regular spot on a removed Barrow Deeps schedule"`). Sets `self._regulars_owed`.
  - `Registry.migrate_schedules()` saves, when the lifts or the remap owe it, in ONE `store.batch`: `guild.yaml` and each
    changed member / applicant file. `self.ids_saved: list[str]` keeps the id lines and the dropped-regulars lines for the
    ops line `"Schedules now have ids of their own: " + " · ".join(lines) + "."`, posted by `_schedules_migrate` after
    phase 2's timing line.
  - Removed: `DEFAULT_SCHEDULE`, `series_key`, `parse_series`, the `default` branches of `norm_schedule`,
    `schedules_of`, `schedule_def_of`, `_raid_override_of` (no `slots` any more), `_lift_raid_of`'s `default` insertion
    (an old raid `slots` key lifts into a schedule named by `lifted_schedule_id`). `RAID_IDENTITY` loses `slots`.
  - `Registry.all_schedules() -> list[dict]` (effective rows, guild order, each with `lineup`) and
    `Registry.schedule_entry(sid: str) -> dict` (`NO_SCHEDULE` when missing). `Registry.schedules(instance)` = the rows
    whose lineup starts with `instance` (unchanged callers). `schedule_def(instance, sid)`: `sid` None or `ADHOC_SCHEDULE` =
    the one-off of `instance`; any other id is looked up guild-wide, and `instance` must be its first raid (else
    `NO_SCHEDULE`).
  - Verbs by id: `set_schedule(sid: str, fields: dict, by: str, *, raid: str | None = None) -> list[str]` (`raid` only
    names the first raid of a NEW schedule; given for an existing id whose first raid differs → `ID_TAKEN`),
    `remove_schedule(sid: str, by: str) -> str`, `schedule_regulars(sid)`, `schedule_runs_open(sid, rs=None)`,
    `schedule_removal_effects(sid, rs=None)`. The section is `config_rev("schedule:<id>")` (`_schedule_section`); the
    `raids:<rid>` rev no longer covers schedules.
  - `Registry.series(cls)` → `(sid, label)`; `series_label(key)` reads the alias for an old key and, for an unknown one,
    `f"{key} (removed schedule)"`; `regular_add` / `regular_remove` / `regulars` take a schedule id; the lockout twin
    warning compares first raids through `schedule_entry`, never a split.
  - `_apply_raid_override` raises `RegistryError(RUN_TIMES_ON_SCHEDULE)` for `slots` and `schedules`.
    `set_raid_overrides(instance, {"schedules": rows})` (the Discord wizard's whole-list write) stays: its rows are that
    raid's schedules; an id another raid's schedule holds → `ID_TAKEN`.
  - `lower_schedule_ids(cfg: GuildConfig, members: list[Member]) -> list[str]` — the way back to phase 2's code: each
    schedule whose alias came from `"<raid>:default"` is renamed back to `default`; every member key goes back through
    the reversed aliases; `schedule_aliases` emptied. Refused (RegistryError naming them) when two schedules of one raid
    would both lower to `default`, or a schedule's lineup has more than 2 entries (Task 4), or a live run file has a key
    the older code cannot find (Task 3 stamps it: `occurrence` set and a key not built by the old `event_key`).

- [ ] **Step 1: Write the failing tests** (`tests/test_schedule_ids.py`; build old files with phase 1's `_write_old` /
  `open_registry` / `commit_count` helpers from `tests/test_guild_schedules.py`)

```python
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


def test_regulars_follow_their_schedule_and_save_once(old_store_with, open_registry):
    """A member file holding rosters ['barrow_deeps:default'] beside the oi-shaped guild.yaml: after load,
    reg.regulars('barrow-deeps') lists them before any save; migrate_schedules adds ONE commit holding guild.yaml and
    the member file; a second call adds none; reopening reads ['barrow-deeps'] from disk."""


def test_a_live_run_dict_is_remapped_and_a_test_one_is_not(old_store_with, open_registry):
    """run dicts {series: 'barrow_deeps:default', schedule_id: None} → {series: 'barrow-deeps', schedule_id: 'barrow-deeps'};
    a dict with test: True is byte-for-byte unchanged."""


def test_an_orphan_regular_key_is_dropped_and_named(old_store_with, open_registry):
    """rosters ['barrow_deeps:gone'] with no such schedule: dropped, and reg.ids_saved names the member."""


def test_the_raid_refuses_run_times(reg):
    with pytest.raises(RegistryError, match="belong to a schedule"):
        reg.set_raid_override(RID, "slots", "Tue 19:30", "t")


def test_a_schedule_is_addressed_by_its_id_alone(reg):
    sid = weekly(reg, RID, "Tue 19:30", "tue")
    reg.set_schedule("tue", {"slots": "Wed 19:30"}, "t")
    assert reg.schedule_entry("tue")["slots"] == ["Wed 19:30"] and reg.schedule_def(RID, "tue")["schedule"]["id"] == "tue"
    with pytest.raises(RegistryError, match="already called tue"):
        reg.set_schedule("tue", {"slots": "Thu 19:30"}, "t", raid="hyjal_summit_forever")
    with pytest.raises(RegistryError, match="no schedule called nope"):
        reg.set_schedule("nope", {"active": False}, "t")


def test_removing_a_schedule_takes_its_regulars_off(reg):
    """weekly(reg) → regular_add(uid, sid) → remove_schedule(sid): the member's rosters no longer hold sid; one commit."""


def test_a_stale_schedule_rev_refuses(reg):
    """rev = reg.config_rev('schedule:tue'); another set_schedule('tue', …); set_schedule('tue', …, rev=rev) → ConfigChanged."""


def test_lowering_ids_round_trips(old_store_with, open_registry):
    """lower_schedule_ids then lift_schedule_ids + remap gives back the same schedules, ids, aliases and member keys."""
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_schedule_ids.py -q`.
- [ ] **Step 3: Implement** the interfaces above. `raidcycle`: `ensure_run` stamps `series = sid` for a recurring run,
  `run_series` falls back to the run dict's `schedule_id`, `run_kind` reads `schedule_entry`; `upcoming` ordering drops
  the `default`-first tie-break (ties by schedule id). Keep `run_key(…, schedule)` building keys for now (Task 3 removes
  it): a schedule id is always a suffix.
- [ ] **Step 4: Extend `scripts/lower_schedules.py`** with `--ids-only` (lowers ids and regulars: back to phase 2's
  code); without flags it lowers ids, then minimums, then schedules. Docstring names all three paths. Add
  `test_the_script_lowers_ids_only_when_asked` to the script tests in `tests/test_guild_schedules.py`.
- [ ] **Step 5: Update the tests** listed under Files, then run
  `uv run pytest tests/test_schedule_ids.py tests/test_guild_schedules.py tests/test_schedule_timing.py tests/test_schedules.py tests/test_follow_on.py tests/test_roster_p1_regulars.py tests/test_registry_raids.py -q` → pass.
- [ ] **Step 6: Write the manual change,** commit — "Schedules: a guild-unique id each, no default schedule, regulars
  keyed by schedule; an older file is lifted at start."

### Task 2: Every surface names a schedule by its id

**Files:**
- Modify: `src/oibot_gm/configops.py`, `src/oibot_gm/web/api_ops.py`, `src/oibot_gm/web/api.py`, `src/oibot_gm/mcp_server.py`,
  `src/oibot_gm/flows_config.py`, `src/oibot_gm/flows_roster.py`, `src/oibot_gm/wizard_opts.py`, `src/oibot_gm/consequences.py`,
  `src/oibot_gm/help.py`, `src/oibot_gm/discord_pool.py` (series rows), `frontend/src/components/ScheduleCard.tsx`,
  `frontend/src/pages/Rosters.tsx`, `frontend/src/api.ts`, the bundle
- Test: `tests/test_schedule_surfaces.py` (new); update `tests/test_parity_ops_setup.py`, `tests/test_structured_ops.py`,
  `tests/test_mcp.py`, `tests/test_flows_config.py`, `tests/test_plain_text.py`, `tests/test_rosters_page.py`,
  `tests/test_frontend_source.py`
- Modify: `docs/manual.md` (plain text and MCP: a schedule by its id; §8b tool list)

**Interfaces:**
- Consumes: Task 1's id verbs, `schedule_entry`, `all_schedules`, `series_label`.
- Produces:
  - Ops: `schedule_set` target = the schedule id, field `<setting>` (no `<id>.` prefix), plus field `raid` for a new
    schedule's first raid; `schedule_remove` target = the schedule id. A target containing `:` → `"Name a schedule by
    its id alone."`. Schema text, `describe` and `coalesce` (key `("schedule_set", sid)`) follow.
  - Structured kinds `schedule` / `schedule_remove`: `target` = the id; a new schedule's `fields` carry `lineup`
    (`[{raid, rosters[, starts]}]`) or `raid`. `revs` key `config:schedule:<id>`.
  - Routes `/api/admin/raid/schedule` `{id, fields}` and `/api/admin/raid/schedule/remove` `{id}` (no `instance`; an
    `instance` sent is ignored). `schedule_json` carries `id` (now unique) and `raids` (its lineup's raid ids).
  - MCP: `schedule_set(schedule: str, field: str | None = None, value: Any = None, fields: dict | None = None,
    raid: str | None = None, preview: bool = False, guild: Guild = None)`, `schedule_remove(schedule: str,
    preview: bool = False, guild: Guild = None)`; `list_regulars` rows keyed by id.
  - C10: `consequences.schedule_keeps_sentence(n: int) -> str | None` — `None` for 0, `"The open run keeps its old
    times."` for 1, `"The {n} open runs keep their old times."` for more; appended to the `schedule_set` consequences
    through `CONSEQUENCE_HOOKS` with `n = len(reg.schedule_runs_open(sid, rs))`. Registered in
    `tests/test_confirm_readability.py` for n = 0, 1, 3.
  - Every series parse in "Key-parsing sites" (Task 2 half) reads `schedule_entry` / `series_label` instead.
  - `ScheduleCard.tsx`: targets `s.id`; a new schedule sends `fields.lineup = [{raid: r.id, rosters}]` plus the
    follow-on entry the editor holds; `revs` `config:schedule:<id>`. `Rosters.tsx` regulars keyed by `s.key` (= the id).

- [ ] **Step 1: Write the failing tests**

```python
def test_plain_text_sets_a_schedule_by_id(reg):
    ops = configops.ops_from_fields(reg, "schedule", "tue", {"slots": "Wed 19:30"})
    assert [(o.op, o.target, o.field) for o in ops] == [("schedule_set", "tue", "slots")]


def test_an_old_raid_colon_target_is_refused_in_one_sentence(reg):
    with pytest.raises(RegistryError, match="by its id alone"):
        configops.ops_from_fields(reg, "schedule", "barrow_deeps:tue", {"active": False})


def test_the_admin_route_takes_an_id_alone(officer_client, reg):
    weekly(reg, RID, "Tue 19:30", "tue")
    r = officer_client.post("/api/admin/raid/schedule", json={"id": "tue", "fields": {"active": False}, "confirmed": True})
    assert r.status_code == 200 and reg.schedule_entry("tue")["active"] is False


def test_a_schedule_edit_says_open_runs_keep_their_times(reg, rs):
    """weekly(reg, …, 'tue'), open its next run; describe_ops of a 'tue' lock_hours_before change carries
    'The open run keeps its old times.' and the run dict's cutoff_hard_hours is unchanged after Apply."""


def test_mcp_schedule_tools_take_no_raid():
    sig = inspect.signature(ms.schedule_set)
    assert "raid" in sig.parameters and list(sig.parameters)[0] == "schedule"
    assert list(inspect.signature(ms.schedule_remove).parameters)[0] == "schedule"


def test_no_series_key_is_split_any_more():
    src = "".join(p.read_text() for p in (ROOT / "src/oibot_gm").rglob("*.py"))
    assert not re.search(r"(key|k|target)\)?\.partition\(\":\"\)", src)
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** then `cd frontend && npm run build`.
- [ ] **Step 4: Run** `uv run pytest tests/test_schedule_surfaces.py tests/test_parity_matrix.py tests/test_parity_ops_setup.py tests/test_structured_ops.py tests/test_mcp.py tests/test_flows_config.py tests/test_plain_text.py tests/test_rosters_page.py tests/test_frontend_source.py tests/test_copy.py tests/test_confirm_readability.py -q`
  and `uv run python scripts/check_bundle.py` → pass.
- [ ] **Step 5: Write the manual change,** commit with the bundle — "Schedules: every surface names one by its id."

### Task 3: Runs get an identity that is not their time

**Files:**
- Modify: `src/oibot_gm/raidcycle.py`, `src/oibot_gm/raid_scheduler.py`, `src/oibot_gm/configops.py`, `src/oibot_gm/flows_raid.py`,
  `src/oibot_gm/help.py`, `src/oibot_gm/web/api.py`, `src/oibot_gm/discord_raid.py`, `src/oibot_gm/registry.py`
  (`lower_schedule_ids`' live-run check), `src/oibot_gm/discord_bot.py` (the start batch)
- Test: `tests/test_event_identity.py` (new); update the 7 files using `rc.run_key` / `rc.event_key` and the 7 asserting
  a date-shaped key (`grep -rlE "rc\.(run_key|event_key)\(|\"[a-z]{2,4}-[0-9]{4}-[0-9]{4}" tests`)

**Interfaces:**
- Consumes: Task 1's `schedule_entry`, `schedule_def`.
- Produces:
  - `RaidEvent` gains `occurrence: str | None = None`, `event: str | None = None`, `part: int = 0`, `starts: int = 0`.
    `schedule` now holds an id or None (a one-off).
  - `KEY_ALPHABET`, `new_run_key(reg: Registry, rs: RaidStore, instance: str) -> str` (Global constraints).
  - `occurrence_stamp(reg: Registry, start: datetime) -> str` — `start.astimezone(reg.tz).strftime("%Y-%m-%dT%H:%M")`.
  - `event_of_occurrence(rs: RaidStore, sid: str, occurrence: str) -> RaidEvent | None` — the part-0, non-test run file
    with those stamps, any state (the newest `created_at` if several).
  - `live_at(rs: RaidStore, instance: str, start: datetime, *, test: bool = False) -> RaidEvent | None` — a live part-0
    run whose first raid is `instance` at that exact instant and whose `test` flag matches.
  - `occurrences(reg: Registry, now: datetime, horizon_hours: float | None = None, *, lead: bool = False) ->
    list[tuple[dict, datetime]]` — every active non-pickup schedule's starts guild-wide (`upcoming(reg, instance, …)`
    becomes its filter by first raid).
  - `ensure_run(reg, instance, start, by="scheduler", cutoffs=None, schedule=None, follow=None, *, occurrence=None) -> dict`
    always CREATES (finding is the caller's): keys from `new_run_key`, the follow-on's dict keyed the same way, both dicts
    stamped `occurrence`, `event` (the first dict's key), `part`, `starts`; `"schedule": start.strftime("%a %H:%M")` stays.
  - `open_run(reg, rs, instance, start, by="scheduler", cutoffs=None, schedule=None, follow=None) -> RaidEvent`:
    a schedule's run → `event_of_occurrence` (live: returned; closed: `closed_run_refusal`'s sentence); a one-off or test
    run → `live_at` (returned when live); else ensure_run + `open_event` per part. `open_event(reg, rs, team, starts_at)`
    keys the file with `team["key"]` and takes `follows` / `event` / `part` / `starts` / `occurrence` from the dict —
    no date, no derived key. `closed_run_refusal(rs, ev)` takes the run, not a key.
  - `which_run(reg, instance, start, sid=None) -> dict` and `open_preview` return `occurrence` (or None) instead of `key`;
    `upcoming_rows` rows carry `occurrence` and `open_key` (the live run's key or None); `open_row_refusal(reg, rs,
    instance, start, sid, opens, now=None)`.
  - Scheduler: open step loops `occurrences(reg, now, lead=True)`; `event_of_occurrence` live → skip, closed → skip, none
    → `open_run_and_post`. `_warn_unopened` / `_warn_open_refused` key their once-sets by `(guild, sid, occurrence)`;
    `_warn_key_clash` and every `RunKeyClash` path are deleted.
  - `/api/raid/{rid}/open` takes `occurrence` from the preview (replacing `key`): 409 with the fresh preview when
    `which_run(...)["occurrence"]` differs. `/api/readiness` forecast `open_key` = `event_of_occurrence(rs, sid,
    occurrence_stamp(reg, start))` when live.
  - Start-up stamping: `stamp_runs(reg: Registry, rs: RaidStore) -> list[str]` — for every LIVE run file without `event`:
    `event` = its own key, or (for a follow-on) `follows`; `part` = 1 for a follow-on else 0; `starts` = minutes between
    it and its lead; `schedule` = the run dict's `schedule_id` when it is a real schedule id, else None (`adhoc`, `after`,
    `test` → None); `occurrence` = `occurrence_stamp(start)` when `schedule` is set. Every live run dict gets the same
    stamps. Finished and cancelled runs are never touched. Idempotent. `_schedules_save` runs it inside the SAME
    `store.batch` as `migrate_schedules` (one commit per guild).
  - Deleted: `run_key`, `event_key`, `schedule_kw`, `RunKeyClash`, `TEST_SCHEDULE`, `FOLLOW_SCHEDULE`'s use as a key.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_new_run_key_carries_no_time(reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg, days=3), by="t")
    assert re.fullmatch(r"bd-[23456789abcdefghjkmnpqstuvwxyz]{6}", ev.key) and ev.team == ev.key
    assert (ev.event, ev.part, ev.starts, ev.schedule, ev.occurrence) == (ev.key, 0, 0, None, None)


def test_a_schedule_run_is_found_by_its_occurrence(reg, rs):
    sid = weekly(reg, RID, "Tue 19:30")
    t = first_slot(reg, RID)
    ev = rc.open_run(reg, rs, RID, t, schedule=sid)
    assert ev.occurrence == rc.occurrence_stamp(reg, t) and rc.event_of_occurrence(rs, sid, ev.occurrence) is ev
    assert rc.open_run(reg, rs, RID, t, schedule=sid) is ev


def test_a_one_off_at_the_same_minute_is_the_same_run(reg, rs):
    t = start_in(reg, days=2)
    assert rc.open_run(reg, rs, RID, t) is rc.open_run(reg, rs, RID, t)


def test_an_old_key_is_never_read_for_its_time(reg, rs):
    """Write a run file + dict keyed 'bd-1215-1830-tue-2026-12-15' with starts_at 16 Dec 18:30 and schedule 'tue';
    stamp_runs → occurrence '…12-16T18:30'; event_of_occurrence(rs, 'tue', '…12-15T18:30') is None and the scheduler's
    open step for the 15 Dec occurrence opens a NEW run with a new key."""


def test_stamping_twice_changes_nothing(reg, rs):
    """Copy of big-guys-for's pair shape (lead dict schedule_id 'adhoc' + follow, follow-on 'after'): stamp_runs →
    lead (event=lead key, part 0, starts 0, schedule None), follow-on (event=lead key, part 1, starts 180); a second
    stamp_runs returns [] and writes no commit; signups, message_id and every other field unchanged."""


def test_a_timezone_change_opens_no_duplicate(reg, rs):
    """A schedule run open, the guild moves zone keeping runs at their moment: the next scheduler open step opens nothing
    for that schedule's occurrence (occurrence is the wall-clock reading)."""


def test_open_refuses_when_the_occurrence_changed(officer_client, reg):
    """Preview a schedule's next run, change the schedule's slots, POST open with the old occurrence → 409 + preview."""


def test_nothing_builds_or_parses_a_run_key():
    src = {p: p.read_text() for p in (ROOT / "src/oibot_gm").rglob("*.py")}
    for pat in (r"\bevent_key\(", r"\brun_key\(", r"schedule_kw\(", r"RunKeyClash", r"\.date\(\)\.isoformat\(\)}\"",
                r"f\"\{team\[.key.\]\}-"):
        assert not any(re.search(pat, s) for s in src.values()), pat
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces; replace every Task-3 site in "Key-parsing sites". The test bench
  (`/gm test run`, `open_comp_sandbox`) finds its run with `live_at(…, test=True)`; another tester's run at that minute
  is still refused with the existing sentence.
- [ ] **Step 4: Run** `uv run pytest tests/test_event_identity.py tests/test_follow_on.py tests/test_raid_night.py tests/test_schedules.py tests/test_testbench.py tests/test_flows_raid.py tests/test_rosters_page.py tests/test_parity_routes.py tests/test_public_schedule.py -q` → pass.
- [ ] **Step 5: Commit** — "Runs: a key with no time in it, found by the schedule and occurrence they were opened for."

### Task 4: Lineups of up to four raids

**Files:**
- Modify: `src/oibot_gm/registry.py`, `src/oibot_gm/raidcycle.py`, `src/oibot_gm/discord_raid.py`, `src/oibot_gm/raid_scheduler.py`,
  `src/oibot_gm/raid_views.py`, `src/oibot_gm/consequences.py`, `src/oibot_gm/flows_raid.py`, `src/oibot_gm/flows_config.py`
  (the schedule screens keep entries past the second untouched)
- Test: `tests/test_lineups.py` (new), `tests/test_lineup_solver.py` (new, `pytestmark = pytest.mark.slow`); update
  `tests/test_follow_on.py` where a follow-on cancelled alone kept its lead
- Modify: `docs/manual.md` (§4 / §11: a run of up to four raids, one signup; each later raid's start; cancel cancels all)

**Interfaces:**
- Consumes: Task 3's stamps, `new_run_key`, `event_of_occurrence`.
- Produces:
  - `LINEUP_MAX = 4`; `parse_lineup` message for too many = `LINEUP_MAX_WORDS`. The row shape carries `then:
    list[dict]` = `lineup[2:]` (`to_stored_schedule` / `from_stored_schedule` stay exact inverses); `SCHEDULE_FIELDS`
    gains `lineup` (the whole lineup, replacing `rosters` / `follow` / `then`).
  - `Registry.lineup_offsets(lineup: list[dict]) -> list[int]` — minutes per entry: 0, then the stored `starts` or the
    previous offset + the previous raid's `duration_hours` × 60 (C12). Offsets must increase, else
    `"Each raid starts after the one before it."`.
  - `rc.open_run(…, lineup: list[dict] | None = None)` — `lineup` = the LATER entries; `follow` stays as a one-entry alias.
    `ensure_run` writes every part's dict in ONE `save_config`: part k's dict = part 0's with its raid, size, rosters,
    `name = f"{raid} · after {first raid} {local12(first start)}"`, `follows` = part 0's key, `followed_by` = part k+1's
    key, `part`, `starts`, cutoffs = part 0's + `starts / 60`, `confirm: False`, `reminders` off, `open_dm: False`,
    `series: None`. Part 0's dict holds `lineup` (all entries, with offsets) instead of `follow`.
  - `rc.event_parts(rs: RaidStore, ev: RaidEvent) -> list[RaidEvent]` (by `event`, sorted by `part`),
    `rc.first_part(rs, ev) -> RaidEvent`, `rc.later_parts(rs, ev) -> list[RaidEvent]`, `rc.same_event(a, b) -> bool`.
    `lead_of` = `first_part` for a part ≥ 1; `follow_of` = the next part; `is_follow(ev)` = `ev.part > 0 or bool(ev.follows)`.
  - `rc.complete_parts(reg, rs, first: RaidEvent) -> list[RaidEvent]` — opens a part whose dict exists and whose run
    file doesn't (an open cut off by a restart); called by `open_run` on a found run and by the scheduler for every live
    part-0 run each minute. Never posts a sheet.
  - `RaidStore.save(first)` → `sync_parts(rs, first) -> bool` (each live later part copies part 0's answers, its own
    `leaving` out, its own extra rows kept — today's `sync_follow` rule per part).
  - `conflicts` skips `same_event`. `_solve_run` on part k: every player rostered on ANY earlier part gets
    `LEAD_SEAT_BONUS`. `rc.parts_short(reg, ev) -> list[tuple[str, dict[str, int]]]` (raid name, short per role) replaces
    `follow_short` on the health card. `follow_words_of` → "Barrow Deeps ×2, then Onyxia's Lair" (the sheet title stays
    "Hyjal Summit, then …").
  - `free_seat` on part 0 frees the member's seat on every locked later part; `drop_seated`, `after_lead_out` cascade
    over `later_parts`; `leave_follow` / `stay_for_follow` act on the part named.
  - `RaidMixin.lock_run` on part k ≥ 1 while any earlier part is open → `LATER_PART_LOCK`. `RaidMixin.lock_parts(reg, rs,
    first, by) -> str | None` replaces `lock_follow`: locks later parts in order, stops at the first that stays open, one
    ops warn per part. The scheduler skips part k while any earlier part is not locked, and retries the first open one.
  - `cancel_run` on ANY part cancels every live part (one audit line naming them); the cancel consequence names the
    whole lineup.
  - C8: `schedule_starts` drops a start `t` when any lineup entry's raid has `first_open` after `t + offset`.
  - C9: `consequences.before_first_open_sentence(reg, lineup: list[dict], start: datetime) -> str | None` —
    `"{Raid} first opens {local12}, after this run starts."` for the first such entry; `open_preview` returns it as
    `before_open`; `flows_raid`'s open confirm and the site's open confirm print it; Registered in
    `test_confirm_readability.py`.

- [ ] **Step 1: Write the failing tests** (`tests/test_lineups.py`; puppets from the test bench). `profiles/forever` has
  only three raids (barrow_deeps, hyjal_summit_forever, onyxias_lair), so the file's `reg` fixture overrides conftest's
  with a profile copy holding two more raids, `extra_a` and `extra_b` (copies of `barrow_deeps` with their own names,
  `duration_hours: 2`), built with `profile.model_copy(deep=True)` — no profile file changes.

```python
LINEUP = [{"raid": "barrow_deeps", "rosters": 2}, {"raid": "extra_a", "rosters": 1}, {"raid": "extra_b", "rosters": 1}]


def test_a_four_raid_lineup_opens_four_parts_from_one_signup(reg, rs):
    first = rc.open_run(reg, rs, "hyjal_summit_forever", start_in(reg, 3), lineup=LINEUP)
    parts = rc.event_parts(rs, first)
    assert [p.part for p in parts] == [0, 1, 2, 3] and {p.event for p in parts} == {first.key}
    assert [p.starts for p in parts] == reg.lineup_offsets([{"raid": "hyjal_summit_forever", "rosters": 1}, *LINEUP])
    join(reg, rs, first, reg.test_members()[:5])
    assert all(len(p.signups) == 5 for p in parts)


def test_a_fifth_raid_is_refused(reg, rs):
    with pytest.raises(RegistryError, match="at most 4 raids"):
        rc.open_run(reg, rs, "hyjal_summit_forever", start_in(reg, 3), lineup=LINEUP + [{"raid": "onyxias_lair", "rosters": 1}])


def test_a_later_part_never_locks_first(bot, reg, rs):
    """lock_run on part 2 while part 0 is open → RegistryError(LATER_PART_LOCK with part 0's title)."""


def test_four_raids_lock_in_order_and_a_short_role_never_stops_one(bot, reg, rs):
    """Joiners with one tank: locking part 0 locks parts 1–3 (each with a roster, the tank-short ones short); force part 2's
    solve to raise: parts 0–1 locked, 2 and 3 open, part 2 has lock_error; the scheduler minute retries part 2 first."""


def test_cancel_on_any_part_cancels_the_whole_run(bot, reg, rs):
    """cancel_run on part 2 → all four cancelled, one audit line."""


def test_conflicts_never_count_another_part_of_the_same_run(reg, rs):
    assert rc.conflicts(rs, rc.event_parts(rs, first)[3]) == {}


def test_a_schedule_skips_a_night_a_later_raid_is_not_open_yet(reg):
    """A weekly schedule Hyjal → Barrow Deeps; Barrow Deeps' first_open between two Tuesdays' 9:30 PM: the first Tuesday is
    not in rc.next_runs_of, the second is."""


def test_a_one_off_before_first_open_says_so(reg, rs):
    """open_preview of a one-off whose second raid opens later: before_open == the one sentence; open still works."""


def test_an_open_cut_off_between_parts_is_completed_once(reg, rs, monkeypatch):
    """Make open_event raise for part 2: dicts for 4 parts exist, files for 0–1. complete_parts opens 2 and 3 once; a
    second call opens nothing; part 0's message_id is untouched."""


def test_the_raid_wizard_keeps_a_third_raid_it_does_not_show(harness_officer, reg):
    """A schedule with a 3-entry lineup saved through flow raid_config's schedule screen (a name change): lineup[2] is kept."""
```

  `tests/test_lineup_solver.py`: `test_a_later_raid_prefers_the_earlier_rostered` — a 20 → 10 ×2 → 10 chain, part 3's
  roster is drawn from part 0's rostered whenever the minimums allow.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces. The two live runs keep their dicts' `follow` key (read as `lineup[1]` when
  `lineup` is absent); nothing rewrites them beyond Task 3's stamps.
- [ ] **Step 4: Run** `uv run pytest tests/test_lineups.py tests/test_follow_on.py tests/test_raid_night.py tests/test_schedules.py tests/test_confirm_readability.py -q`
  and `uv run pytest -q -m slow tests/test_lineup_solver.py tests/test_follow_on_solver.py` → pass.
- [ ] **Step 5: Write the manual change,** commit — "Runs: a lineup of up to four raids from one signup, one lock, one
  cancel."

### Task 5: Reschedule and per-run timing

**Files:**
- Modify: `src/oibot_gm/raidcycle.py`, `src/oibot_gm/discord_raid.py`, `src/oibot_gm/registry.py` (`NOTICE_KINDS` + `moved`),
  `src/oibot_gm/consequences.py`
- Test: `tests/test_reschedule.py` (new); `tests/test_confirm_readability.py` (cases)
- Modify: `docs/manual.md` (§4: moving a run; answers stand; who sees a notice; what the lock does)

**Interfaces:**
- Consumes: Task 4's `event_parts`, `first_part`; `Registry.notify`; `confirm_layout`.
- Produces:
  - `EVENT_TIMING = {"nudge_hours_before": "cutoff_soft_hours", "lock_hours_before": "cutoff_hard_hours",
    "confirm_hours_before": "confirm_hours", "fill_ask_hours": "fill_ask_hours", "nudge": "reminders",
    "autofill": "autofill"}` (setting → run dict key; `nudge` stores `"dm"` / `OFF`).
  - `rc.reschedule_refusal(reg, rs, ev, start: datetime | None, timing: dict | None, now=None) -> str | None` — in order:
    `MOVE_CLOSED`; `MOVE_PAST` (more than `PAST_GRACE_MINUTES` ago); `MOVE_SAME` (same instant, no timing);
    `MOVE_CLASH` (`live_at` of the first raid at the new instant, not this run); `TIMING_LOCKED` (timing on a locked run);
    `TIMING_ORDER` (reminder < lock or confirm-by > lock, hours before the start).
  - `rc.Moved` (Pydantic): `parts: list[str]`, `old: str`, `new: str` (ISO), `timing: dict`, `told: list[int]`,
    `absence_cleared: list[int]`, `absence_written: list[int]`, `locks_now: bool`, `conflicts: dict[int, str]`.
  - `rc.reschedule(reg, rs, ev, *, start=None, timing=None, by: str, save: bool = True) -> Moved` — on part 0 (any part
    resolves to it): every part's `starts_at` += delta; each dict's `schedule` label and `name` re-rendered; `timing` on
    part 0's dict, later parts' cutoffs = part 0's + `starts / 60`; `occurrence` and `schedule` NEVER change (C11);
    `confirm_by` += delta, and when that is already past, `min(new start, now + 1 hour)`; fill asks keep their expiry;
    `recompute_absences`; one log line `"moved from {old} to {new} by {by}"`; every part saved in one `store.batch`.
    `save=False` = the preview (a deep copy, nothing written). Never touches `config.schedules`.
  - `rc.recompute_absences(reg, ev) -> tuple[list[int], list[int]]` (C18): a row with `source == "absence"` is removed
    when the member's absences no longer cover the new day; a member of `absence_pool` covered on the new day with no
    row gets `_signup(..., "out", "absence")`. Any other row stands.
  - `RaidMixin.edit_live_card(reg, channel_id, message_id, view) -> bool` (keeps the buttons; `edit_ask_card` stays for
    final states).
  - `RaidMixin.reschedule_event(reg, rs, ev, *, start=None, timing=None, by: str, by_id: int | None, via: str = "discord",
    preview: bool = False) -> list[str]` — officers live; under every part's `board_lock`; refusal → `RegistryError`
    with its sentence; preview → `consequences.reschedule_consequence`'s sentences; else `rc.reschedule`, then
    `refresh_sheet(part 0)`, `refresh_cards` per part, each open confirmation ask (`ask["dm"]`, answer None, on a part's
    team) re-rendered with `confirm_layout` through `edit_live_card`, `Registry.notify(uid, "moved", "{title} now starts
    {local12}.", run=part 0 key)` for every uid with a row on part 0 (one member save each), one `post_run_update`
    line, one `ops.audited` line. No DM.
  - `consequences.reschedule_consequence(reg, rs, ev, start: datetime | None, timing: dict | None) -> list[str]`
    (≤ 3 sentences, in order): what changes — `"{title} moves to {local12}."` or `"The sheet now locks {local12}."`
    (timing only); who is told — `"Answers stand, and the {n} who answered see a notice."` (locked: `"The roster
    stands, and …"`; n = 0: `"Answers stand."`); then the FIRST of: `"It locks within a minute, because its lock time
    has passed."` · `"{name} and {k} others are also on {other run title} that night."` (C11) · `"Away answers are
    worked out again for the new day."` · `"You can move it again until it starts."`.

- [ ] **Step 1: Write the failing tests**

```python
def test_answers_stand_and_everyone_who_answered_gets_a_notice(bot, reg, rs):
    ev = open_one_off(reg, rs, days=3); join(reg, rs, ev, reg.test_members()[:4])
    before = {k: v.status for k, v in ev.signups.items()}
    new = ev.start + timedelta(days=1)
    asyncio.run(bot.reschedule_event(reg, rs, ev, start=new, by="o", by_id=OFFICER_ID))
    assert {k: v.status for k, v in ev.signups.items()} == before and ev.start == new
    assert all(any(n["kind"] == "moved" for n in reg.members[int(k)].notices) for k in before)
    assert bot.dms == []


def test_every_part_moves_with_its_offset(bot, reg, rs):
    """A 3-part run moved 2 hours: each part's start +2 h, starts unchanged, later parts' cutoffs = part 0's + offset."""


def test_a_moved_run_never_blocks_or_repeats_an_occurrence(bot, reg, rs, freezer):
    """Weekly Tue 19:30. Open next Tuesday's run (occurrence T1), move it to the Monday after (before T2): at T2's sheet
    opening the scheduler opens T2; T1's slot never opens again. Then move the T2 run past T3: T3 still opens on time."""


def test_lockout_and_conflicts_read_the_moved_start(reg, rs):
    """A locked run moved into the next lockout window: saved_this_lockout of a run in the OLD window no longer counts its
    characters, one in the NEW window does; conflicts read the new start."""


@pytest.mark.parametrize("case, sentence", [("past", "in the past"), ("closed", "finished or cancelled"),
                                            ("clash", "already has a run at"), ("same", "already starts then")])
def test_refusals_are_one_sentence(reg, rs, case, sentence):
    """Each case built; rc.reschedule_refusal returns a sentence containing `sentence`; nothing is written."""


def test_timing_lives_on_the_run_alone(bot, reg, rs):
    """A schedule run: reschedule(timing={'lock_hours_before': 6}) → part 0's cutoff_hard_hours 6; config.schedules is
    byte-for-byte unchanged; a locked run refuses timing with TIMING_LOCKED."""


def test_after_the_lock_the_roster_stands_and_open_cards_are_edited(bot, reg, rs):
    """Locked with two open confirmations: rosters unchanged; edit_live_card called once per open ask with the new
    time in the rendered view; confirm_by moved by the same delta."""


def test_away_answers_are_worked_out_again(bot, reg, rs):
    """A absent on day 1 only (absence row out), B absent on day 2 with no answer, C absent on day 2 who answered Join:
    moved to day 2 → A's row gone, B out from absence, C still Join."""
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces; add `reschedule_consequence` cases to `tests/test_confirm_readability.py`
  (open, locked, timing only, lock-now, conflict, absences, nobody answered) and `reschedule_refusal` each case.
- [ ] **Step 4: Run** `uv run pytest tests/test_reschedule.py tests/test_confirm_readability.py tests/test_quiet_edges.py tests/test_member_inbox.py -q` → pass.
- [ ] **Step 5: Write the manual change,** commit — "Runs: reschedule and re-time one run; answers stand, no DM, a
  notice to everyone who answered."

### Task 6: Edit a run's lineup before the lock

**Files:**
- Modify: `src/oibot_gm/raidcycle.py`, `src/oibot_gm/discord_raid.py`, `src/oibot_gm/consequences.py`
- Test: `tests/test_edit_lineup.py` (new); `tests/test_confirm_readability.py` (cases)
- Modify: `docs/manual.md` (§4: changing the raids of an open run)

**Interfaces:**
- Consumes: Task 4's part creation (factor the per-part dict builder out of `ensure_run` as
  `rc._part_dict(reg, first: dict, entry: dict, k: int, offset: int) -> dict`), `event_parts`, `lineup_offsets`.
- Produces:
  - `rc.lineup_refusal(reg, rs, ev, lineup: list[dict]) -> str | None` — `MOVE_CLOSED`; `LINEUP_LOCKED`; `LINEUP_FIRST`
    (entry 0's raid differs); then `parse_lineup` / `lineup_offsets` refusals; a raid unknown to the profile.
  - `rc.LineupChange` (Pydantic): `added: list[dict]`, `removed: list[str]` (part keys), `order: list[str | None]`
    (the new order of part keys, None = new), `rosters: dict[str, int]` (part key → new count), `dropped_places:
    dict[str, int]` (removed part key → board places / pins it held).
  - `rc.plan_lineup(reg, rs, ev, lineup) -> LineupChange` (pure) and `rc.apply_lineup(reg, rs, ev, change, by) ->
    list[str]` — kept parts keep their keys; `part`, `starts`, `starts_at`, `followed_by`, cutoffs and `name` rewritten
    from the new order; a removed part's file → `state: cancelled`, `closed_at`, log `"removed from the lineup by {by}"`,
    its dict dropped, no notice and no card; part 0's dict `lineup` rewritten; an entry omitted `starts` takes the C12
    default. One `save_config` and one `store.batch` for the files.
  - `RaidMixin.edit_lineup(reg, rs, ev, lineup, *, by, by_id, via="discord", preview=False) -> list[str]` — officers
    live, under every part's `board_lock`; then `refresh_sheet(part 0)`, a `post_run_update` line, one `ops.audited` line.
  - `consequences.lineup_consequence(reg, rs, ev, lineup) -> list[str]`: `"The lineup becomes {first}, then {words}."` ·
    (only when places drop) `"{n} board places on {raid} are dropped."` · `"Everyone's answer now covers the new lineup."`.

- [ ] **Step 1: Write the failing tests**

```python
def test_add_reorder_and_remove_keep_part_keys(bot, reg, rs):
    """Open Hyjal → Barrow Deeps ×2 → extra_a (the wide profile of tests/test_lineups.py); edit to Hyjal → extra_a →
    Barrow Deeps ×1 → extra_b: the Barrow Deeps and extra_a parts keep their keys with part 2 / part 1, an extra_b part 3
    is added with the C12 start, Barrow Deeps' run dict holds rosters 1; then remove extra_b: its file cancelled, its dict gone."""


def test_removing_a_raid_drops_only_its_board_places(bot, reg, rs):
    """Part 1 with a layout and a pin: removed → part 0's signups, layout and pins unchanged; members get no notice."""


def test_the_first_raid_cannot_change_and_a_locked_run_refuses(bot, reg, rs):
    """lineup_refusal → LINEUP_FIRST for another first raid; after the lock → LINEUP_LOCKED."""


def test_the_sheet_title_follows_the_new_lineup(bot, reg, rs):
    """sheet_message(...)[0] title reads 'Hyjal Summit, then <extra_a's name>, then Barrow Deeps' after the edit."""
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement**; add `lineup_consequence` cases (add, remove with places, reorder, roster count) to
  `tests/test_confirm_readability.py`.
- [ ] **Step 4: Run** `uv run pytest tests/test_edit_lineup.py tests/test_lineups.py tests/test_confirm_readability.py -q` → pass.
- [ ] **Step 5: Write the manual change,** commit — "Runs: add, remove or reorder the raids of an open run."

### Task 7: The new verbs on every surface

**Files:**
- Modify: `src/oibot_gm/configops.py`, `src/oibot_gm/web/api.py`, `src/oibot_gm/mcp_server.py`, `tests/test_parity_matrix.py`,
  `frontend/src/components/board/SheetCard.tsx`, `frontend/src/components/ScheduleCard.tsx`, `frontend/src/api.ts`, the bundle
- Test: `tests/test_event_surfaces.py` (new); update `tests/test_mcp.py`, `tests/test_plain_text.py`,
  `tests/test_parity_routes.py`, `tests/test_frontend_source.py`
- Modify: `docs/manual.md` (plain text and MCP verbs; §8b tools; the Rosters page's Reschedule; the schedule editor's lineup)

**Interfaces:**
- Consumes: Task 5's `reschedule_event`, Task 6's `edit_lineup`, Task 4's `open_run(lineup=)`.
- Produces:
  - Ops (in `RUN_OPS`, `OP_GROUP` `"runs"`): `run_reschedule` — target run key, field `start` (value = guild wall time
    `'YYYY-MM-DD HH:MM'`, resolved by `open_when`'s DST-safe path) or an `EVENT_TIMING` setting (value hours / on-off);
    several on one run coalesce into one write. `run_lineup` — target run key, value = the later entries as text
    `"barrow_deeps x2, onyxias_lair"` (`parse_lineup_text(text) -> list[dict]`, `parse_follow` per comma part).
    `run_open`'s `text` takes the same list. Schema text, `describe` (prints the consequence sentences), apply via the bot.
  - `PARITY_PENDING["run_reschedule"] = {"discord": "phase 5: the Reschedule wizard (date and time from selects); until
    then /gm change reaches it as plain text"}` and the same for `run_lineup` ("phase 5: the lineup wizard").
  - Routes: `POST /api/run/{key}/reschedule` `{start?: aware ISO or 'YYYY-MM-DD HH:MM', timing?: {setting: value},
    preview?: bool}` → `{sentences}` or `{message}`; `POST /api/run/{key}/lineup` `{lineup: [{raid, rosters, starts?}],
    preview?: bool}`; a refusal 400 `{error}`. `POST /api/raid/{rid}/open` and its preview take `lineup` (later entries)
    beside `follow`. `ev_json` gains `part`, `event`, `occurrence`, `lineup` (raid ids with rosters and `starts`).
  - MCP: `reschedule_run(run: str, when: str | None = None, timing: dict | None = None, preview: bool = False,
    guild: Guild = None)`, `edit_lineup(run: str, lineup: list[dict], preview: bool = False, guild: Guild = None)`,
    `open_run(…, lineup: list[dict] | None = None)`. Matrix rows: `run_reschedule` → web `POST /api/run/{key}/reschedule`,
    mcp `reschedule_run`; `run_lineup` → web `POST /api/run/{key}/lineup`, mcp `edit_lineup`.
  - Site (smallest control, no redesign): `SheetCard` header actions gain **Reschedule** (open and locked runs, officers)
    → a modal with a `datetime-local` input (the Clock rule's machine input) prefilled with the start → the preview's
    `sentences` through `useConfirm` (verb button "Move") → POST. `ScheduleCard`'s follow-on block becomes up to three
    later-entry rows (raid select, rosters select 1–4, start select in 30-minute steps from the C12 default) sent as
    `fields.lineup`. No Edit lineup control on the site in this phase.

- [ ] **Step 1: Write the failing tests**

```python
def test_plain_text_reschedules(bot, reg, rs):
    """configops.apply_async of run_reschedule field start value '<date> 20:30' moves the run; the diff line carries the
    reschedule_consequence sentences."""


def test_plain_text_edits_a_lineup_and_opens_one(bot, reg, rs):
    assert rg.parse_lineup_text("barrow_deeps x2, onyxias_lair") == [{"raid": "barrow_deeps", "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}]


def test_the_routes_preview_then_write(officer_client, reg, rs):
    """POST /api/run/{key}/reschedule {preview: true} → sentences, nothing written; without preview → moved; a member → 403;
    a past start → 400 with MOVE_PAST."""


def test_mcp_tools_exist():
    assert {"reschedule_run", "edit_lineup"} <= mcp_tool_names()


def test_the_matrix_lists_both_ops():
    assert set(configops.PARITY_PENDING) == {"run_reschedule", "run_lineup"}
```

  `tests/test_frontend_source.py`: `SheetCard.tsx` posts to `/reschedule` and prints the server's `sentences`;
  `ScheduleCard.tsx` sends `lineup` and no `follow_raid`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** then `cd frontend && npm run build`.
- [ ] **Step 4: Run** `uv run pytest tests/test_event_surfaces.py tests/test_parity_matrix.py tests/test_parity_routes.py tests/test_mcp.py tests/test_plain_text.py tests/test_frontend_source.py tests/test_copy.py -q`,
  `uv run python scripts/check_bundle.py`, and the screenshot audit of Rosters and Raids (`uv run python scripts/shots.py`) → pass.
- [ ] **Step 5: Write the manual change,** commit with the bundle — "Runs: reschedule and lineup edits from plain text,
  MCP and the site; Discord's wizards are phase 5."

### Task 8: Design, agent guide, full check and a rehearsal on a copy of the real data

**Files:**
- Modify: `docs/design.md` (§5.67 "Phase 3 built": ids and aliases, keys and stamps, occurrence matching, lineups as a
  chain of parts, reschedule and timing, Edit lineup, the migration and the way back; pointers from §5.27, §5.30, §5.62),
  `CLAUDE.md` (the `registry.py`, `raidcycle.py`, `raid_scheduler.py`, `discord_raid.py`, `consequences.py`,
  `configops.py`, `mcp_server.py` entries: names this plan added or deleted)

- [ ] **Step 1: Write §5.67's phase 3 part and update `CLAUDE.md`.** Only what is built.
- [ ] **Step 2: Run the full check** — `uv run python scripts/check_commands.py > <scratch>/check.log 2>&1; echo "exit=$?" >> <scratch>/check.log`
  (about 20 minutes, in the background, no edits while it runs, never piped through `tail` before `&&`), then
  `uv run pytest -q -m slow tests/test_lineup_solver.py tests/test_follow_on_solver.py`. Expected: `exit=0`, slow tests pass.
- [ ] **Step 3: Rehearse on a copy of the real data.** Copy `/Users/jonathanlin/dev/oibot_gm-data` into a scratch directory
  twice (A, B), remove each copy's git remote, open every guild with `GitStore(<copy>, push=False)`. Check out the
  phase-2 head in a scratch worktree (`git worktree add <scratch>/old <phase-2 commit>`) and run a script with it
  against A; run the same script with this branch against B after `migrate_schedules()` + `rc.stamp_runs()` in one
  batch. Per guild the script prints JSON of: every schedule (`reg.schedules(rid)` per raid, keyed by NAME and first
  raid, ids beside them), `{k: schedule_def[k] for k in SCHEDULE_TIMING}`, regulars per schedule (by name),
  `rc.upcoming(reg, rid, <fixed now>, 24 * 21)` starts, and for every live run (big-guys-for:
  `hsf-1210-1830-adhoc-2026-12-10`, `bd-1210-2130-after-2026-12-10`) `rc.run_team`, `raid_views.run_times`,
  `rc.needs`, `rc.health`, `rc.run_split_reason`, signups, `message_id`, `channel_id`, and
  `raid_views.sheet_message(reg, ev, team, ico=<stub>)[0].to_dict()`. Diff A against B: only the expected ids differ
  (oi's `default` → `barrow-deeps`); everything a person reads is equal. Check B's commit count grew by ONE per guild
  that owed a save, a second start adds none, and the two live runs carry `event` = the lead key, `part` 0 / 1,
  `starts` 0 / 180, `schedule` None. Then run the scheduler's open step on B at a fixed `now` in the week of 15 Dec:
  it opens the Tuesday schedule's 15 Dec run once, the pair is untouched. Run `scripts/lower_schedules.py --ids-only` on
  B and confirm the phase-2 code loads it and prints the same JSON as A. Remove the scratch worktree. Report the ops
  lines each guild would post. Nothing is written to the real data repo.
- [ ] **Step 4: Commit** — "Design §5.67 phase 3 and the agent guide: schedules and runs with their own identity."

## Deploying (the owner's call, not a task)

The first start writes one commit per guild that owes one (oi: the `default` id; big-guys-for: the aliases and the
two live runs' stamps) and posts one ops line per guild. Going back to phase 2's code: stop the bot, run
`uv run python scripts/lower_schedules.py --ids-only` (`--dry-run` first), start the older code. It refuses a guild
with a lineup of three or four raids, or a live schedule run opened by this phase's code (its key can't be found by the
older scheduler, which would open a second sheet): cancel or finish those first. Run keys made by this phase keep
working in the older code as opaque keys for everything but that lookup.

## Calls made in this plan

- **Occurrence is stamped as guild wall time, naive** (`"2026-12-15T18:30"`), not an instant. If wrong: a run kept at its
  real moment across a timezone change is matched by its old wall reading — that is the intent; a second schedule edit
  at the same wall time is the only collision, and it reads as "already open".
- **A `default` schedule becomes `lifted_schedule_id(raid)` and keeps the name "Raid nights".** If wrong: one rename later;
  regulars follow through the alias.
- **Old series keys are translated through stored `schedule_aliases`, never split;** an orphan regular key (a removed
  schedule) is dropped at the migration and named. No live guild holds any (checked 2026-11-23). If wrong: an officer
  re-adds a regular by hand.
- **The profile's raid `slots` are no longer read** (every profile has `[]`). If wrong: a profile that gives run times
  needs a schedule written for it.
- **Key = `<abbr>-<6 random>` from a 30-letter alphabet without 0/1/i/l/o/r**, and the run dict key equals the run file key
  for new runs. If wrong: cosmetic.
- **A one-off at the same minute and first raid as a live one is that run** (today's idempotent open); a cancelled one-off
  no longer blocks a new one-off at its minute. A schedule's closed occurrence is never reopened by an open. If wrong:
  an officer could open a second one-off where one was cancelled — the confirm shows it as new.
- **`follows` = the first part's key on every later part, `followed_by` = the next part** (a linked chain plus `event`,
  `part`, `starts`), one file and one run dict per part. If wrong: a reader walking pairs needs `event_parts`.
- **A later raid prefers anyone rostered on ANY earlier part** (`LEAD_SEAT_BONUS` each). If wrong: a 4-part night seats
  part 3 from part 0 rather than part 2; the board shows it before the lock.
- **C12's "that raid's length" is the PREVIOUS raid's length** (a later raid starts when the one before ends, as the
  follow-on does today). If wrong: one function, `lineup_offsets`.
- **Cancel on any part cancels the whole run** (spec §6); a follow-on can no longer be cancelled alone after the lock.
  Before the lock, Edit lineup removes one. If wrong: the old "follow-on cancelled on its own" path comes back.
- **Reschedule moves every part by the same delta; offsets never change on a move.** Edit lineup changes offsets.
- **After the lock: the start moves, timing does not** (`TIMING_LOCKED`); `confirm_by` moves by the delta and, if that is
  already past, becomes the earlier of the new start and an hour from now. Fill asks keep their expiry. If wrong: an
  officer wanting a later confirm-by after the lock asks for it through Remind.
- **Per-run timing is six settings** (`EVENT_TIMING`): the sheet's opening, its opening DM and whether confirmations are
  asked are fixed once the sheet exists.
- **A move into a past lock time is allowed;** the confirm says it locks within a minute. If wrong: one more refusal.
- **Notices go to everyone with a row on part 0** (Join, Bench, No thanks, away). If wrong: away members get a notice
  they don't need; the receipt text says only the new time.
- **A removed lineup entry's file is kept as cancelled with no notice and no card**, its run dict dropped. If wrong: a
  history row the Rosters page lists as cancelled.
- **Discord gets no new wizard in this phase:** both new ops sit in `PARITY_PENDING` (phase 5), reachable from Discord
  through `/gm change` and @mentions. The raid_config wizard still shows one follow-on and keeps further entries untouched.
- **The site gets Reschedule on the run card and the longer lineup in the schedule editor; Edit lineup on the site waits
  for phase 4's builder** (the route exists for MCP and the matrix).
- **The migration is one `store.batch` per guild** for `guild.yaml`, member files and the live run files.
- **`lower_schedules.py --ids-only` refuses rather than guesses** for 3–4-raid lineups and runs opened by this phase.

## Impossible or contradictory in the spec

- **C11 says a move is refused "only" into the past or onto a clashing minute,** yet a re-timing on a locked run has no
  meaning (its lock has passed) and §6 adds finished / cancelled. This plan adds `TIMING_LOCKED`, `TIMING_ORDER` and
  `MOVE_SAME`; none refuses a move of the START that C11 allows.
- **C11 wants a created conflict said in the confirm, and the Confirm rule caps a confirm at three sentences** that already
  carry what changes and who is told. The third sentence is the first of: locks now · conflict · away answers · undo.
- **Four raids in a lineup cannot happen on `forever` today:** its profile has three raids and D10 / `parse_lineup` refuse
  a raid twice, so the owner's guilds reach at most three; tbc reaches four. The tests use a widened profile copy.
- **"Nothing may parse a key" vs loot council ids `<run>-r<N>` and companion import ids:** those read a council's own
  suffix, never a raid, date or schedule, and are kept (listed above).
- **§5 Migration step 4 "Keys unchanged" and "stamp `schedule` (id or none)":** the live pair's run files hold
  `schedule: adhoc` / `after`; stamping writes None there. Their keys, dicts' keys, sheet and answers are unchanged.
