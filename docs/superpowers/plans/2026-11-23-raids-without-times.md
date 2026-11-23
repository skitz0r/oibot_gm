# Raids without times: timing on schedules, one minimum, officers edit (phase 2) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sheet timing is each schedule's own and leaves the raid (C7); a raid's make-up is one minimum per role or
class (D16); officers edit raids and schedules on every surface (D8, C17).

**Architecture:** Same pattern as phase 1: the stored shape moves at load (pure lift functions on `GuildConfig`, saved
once at start by `Registry.migrate_schedules`), the read shape stays where it can. Every stored schedule carries its
nine timing values written out in full; `raid_def` keeps merging what a raid override holds, and after the lift no
override holds timing (only a raid whose phase-1 lift was refused can, and it keeps reading as before). Minimums
replace `comp {min,max}` and `comp_targets` with one map, `raids.<id>.minimums`; the solver turns every minimum into
an objective preference (never a constraint) and charges for a tank or healer above its minimum.

**Tech stack:** Python 3.12, Pydantic, OR-Tools CP-SAT, FastAPI, discord.py, React + Mantine (Vite), pytest.

**Spec:** `docs/superpowers/specs/2026-11-22-raids-events-schedules-design.md` — §9 item 2, bound by D2, D8, D16, C1,
C6, C7, C10, C17, C19, §4, §5 ("Raid-level timing keys", Migration steps 1–2), §7a "Behaviour that D16 changes".
Phase 1's plan (`docs/superpowers/plans/2026-11-22-guild-level-schedules.md`) and `docs/design.md` §5.67 say what is
built. Read `CLAUDE.md` first.

## Global constraints

- `SCHEDULE_TIMING = ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before",
  "fill_ask_hours", "nudge", "autofill", "open_dm", "confirm")` — the nine values a schedule owns (C7). `split_policy`
  and `weights` are not timing: they stay on the raid as the default and a schedule may set its own (C6).
- A one-off (`ADHOC_SCHEDULE`) keeps `ADHOC_DEFAULTS` exactly. A run already opened keeps its run dict (C10): no
  value a live run reads changes, and nothing in `config.rosters` is rewritten except the one stamp in Task 3.
- No minimum is ever a solver constraint. A role has no upper limit anywhere (storage, solver, readiness, copy).
- Schedule identity, regulars keys, run keys and `LINEUP_MAX = 2` are unchanged (phase 3). The follow-on editor still
  writes lineup entry 2 (C19). No page is redesigned (phase 4); no Discord confirm button is renamed (phase 5); "run"
  stays the word (phase 6).
- Copy: the word is **minimum** ("Minimum", "at least 3 healers"). "limits", "aims", "(owner)" / "(officers)" beside
  them, "at most", "over cap" leave every page, card, wizard, help answer, MCP docstring and the manual. Copy rule,
  Confirm rule, Clock rule and Wizard rule apply to every new string; `tests/test_copy.py` and
  `tests/test_confirm_readability.py` must pass.
- Refusal sentences (exact, used by every surface):
  - `TIMING_ON_SCHEDULE = "Sheet timing belongs to each schedule now. Change it on the schedule."`
  - `NO_UPPER_LIMIT = "A minimum has no upper limit. Give only the minimum."`
  - `MINIMUM_ON_RAID = "Minimums belong to the raid. Set them on the raid, not on one run."`
- `docs/manual.md` changes in the same commit as the behaviour it describes. Real guild data is never committed; the
  live data repo is read-only for this work.
- Frontend changes rebuild and commit the bundle (`cd frontend && npm run build`, then
  `uv run python scripts/check_bundle.py`).
- Commits are made on the work branch. Merging, pushing and restarting the bot happen only on the owner's word.

## Review focus

1. A `guild.yaml` from before this phase: schedule timing reads right the moment the registry loads, before any save.
   (Task 3 `test_an_old_file_reads_its_timing_at_once`.)
2. A run opened from the `default` schedule before the migration has no `fill_ask_hours` in its run dict and read the
   raid's live: its fill-ask deadline is the same after the migration. (Task 3 `test_a_live_run_keeps_its_fill_ask_wait`.)
3. An officer moves a schedule's lock past its stored reminder without touching the reminder: the edit saves and the
   reminder moves to halfway, instead of a refusal. (Task 2 `test_moving_the_lock_past_the_reminder_moves_the_reminder`.)
4. One roster, a tank minimum and a healer minimum, and one druid who could be either: the lock still finds a roster.
   (Task 4 `test_one_hybrid_for_two_minimums_on_one_roster_still_solves`.)
5. A raid whose phase-1 lift was refused (`schedules_refused`): its raid-level timing stays and still reads; nothing
   of it is dropped. (Task 3 `test_a_refused_raid_keeps_its_timing`.)

---

## Where things are

- `src/oibot_gm/registry.py`: `RAID_HOURS_FIELDS` / `RAID_BOOL_FIELDS` / `RAID_DEFAULTS` / `ADHOC_DEFAULTS` :762–779,
  `RAID_IDENTITY` :780, `SCHEDULE_CADENCE` :864, `_raid_rows_of` / `_set_raid_rows_of` / `_raid_override_of` :1063–1095,
  `lift_schedules` :1130, `lower_schedules` :1164, `schedule_value` :1186, `schedule_def_of` :1316 (nudge halfway rule
  :1344), `load_config` :2249, `migrate_schedules` :2270, `raid_def` :4488, `raid_shell` :4557, `role_bounds` :4592,
  `set_schedule` :4619, `set_comp_targets` :4795, `_check_raid` :4861, `_apply_raid_override` :4912 (timing :4964–4977,
  `tank_min…` :5007), `PLAIN_GROUPS` :346.
- `src/oibot_gm/raidcycle.py`: `run_settings` :294, `run_team` :309, `ensure_run` stamping :605–644, `ask_deadline`
  :1417, `gaps` :1392, `first_come_out` :2507, `merged_role_min` :2542, `_solve_run` :2552.
- `src/oibot_gm/roster/solver.py`: `SolveOptions` :26, `scaled_role_bounds` :47, `_setup` :136, `_add_role_bounds` :248,
  `_short_role_terms` :331. Tests: `tests/test_short_role_solver.py`, `tests/test_follow_on_solver.py`.
- `src/oibot_gm/comp.py`: solver callers :127, :191; `ideal_comp` :283; `readiness` :383 (`over` :401).
- `src/oibot_gm/configops.py`: `OWNER_OPS` :446, `OP_GROUP` :466, schema text :120–140, current config text :362,
  `OVERRIDE_WORDS` :907, `_comp_entry` :1313, `config_summary` raid lines :3445, `authorize_ops` :3967.
- `src/oibot_gm/flows_config.py`: `_owner_guard` :55, `RaidConfigWizard` :404 (sections `cadence` :922, `comp` :968,
  `behaviour` :1091), `CompTargetsWizard` :1229, flows `roster_comp` :1256 / `raid_config` :1267.
- `src/oibot_gm/help.py`: `gate_of` :119, readiness lines :1069, raid config lines :1329.
- `src/oibot_gm/web/api.py`: `/api/raids` :2053, `/api/readiness` :1165, `schedule_json` :845.
  `src/oibot_gm/web/api_ops.py`: `need_owner` in the four raid / schedule routes :354–390.
- `src/oibot_gm/mcp_server.py`: `set_comp_target` :379, `raid_set` / `schedule_set` docstrings :640–680, list rows :291.
- `src/oibot_gm/discord_bot.py`: `_schedules_save` :516, `_schedules_migrate` :528. `scripts/lower_schedules.py`.
- Frontend: `pages/Raids.tsx` (`RuleCard`, `HOURS`, `SWITCHES`, the limits block), `components/CompTargets.tsx`,
  `pages/Readiness.tsx`, `components/ScheduleCard.tsx` (`inherited`, `SOURCE_WORDS`, `ADHOC_DEFAULT`), `api.ts`.
- Live data shape (read 2026-11-23): big-guys-for raids `barrow_deeps: {nudge_hours_before: 36}` (no schedule) and
  `hyjal_summit_forever: {nudge_hours_before: 72}` (schedule `tuesday`); oi `barrow_deeps: {lockout_days: 7,
  confirm_hours_before: 12, nudge_hours_before: 36}` (schedule `default`). No guild has `comp` or `comp_targets`.

---

### Task 1: Tests run clean in a checkout with a .env

**Files:**
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: an autouse fixture `_no_host_env(monkeypatch)` that removes `OIBOT_WEB_URL` and every variable whose name
  starts with `OIBOT_BUDGET_USD_` (the per-workload caps), `raising=False`. A test that wants one sets it itself.

- [ ] **Step 1: Reproduce.** Run
  `OIBOT_WEB_URL=https://gm.example.org OIBOT_BUDGET_USD_HELP=0.5 uv run pytest -q -p no:cacheprovider "tests/test_budget_and_find.py::test_spend_survives_a_restart_and_help_has_a_share" "tests/test_parity_d1.py::test_small_print_says_only_what_the_reader_acts_on_and_a_page_is_a_link" "tests/test_price_watch_discord.py::test_a_list_past_discords_select_shows_the_newest_and_says_so" "tests/test_professions_flows.py::test_professions_who_stays_under_discords_limit_and_says_how_many_more"`
  → 4 failed (checked while writing this plan).
- [ ] **Step 2: Add the fixture** next to `_no_reference_price_reads`.
- [ ] **Step 3: Run the same command** → 4 passed.
- [ ] **Step 4: Commit** — `git add tests/conftest.py` · "Tests: a host's .env no longer leaks the site address or a
  budget cap into the suite."

### Task 2: Sheet timing is written on every schedule, and the raid refuses it

**Files:**
- Modify: `src/oibot_gm/registry.py`
- Test: `tests/test_schedule_timing.py` (new; imports `reg`, `RID`, `OTHER` the way `tests/test_guild_schedules.py` does)
- Modify (tests that set raid-level timing): `tests/test_registry_raids.py`, `tests/test_schedules.py`,
  `tests/test_roster_p1_classes.py`, `tests/test_phase0.py`, `tests/test_guild_schedules.py` — each raid-level timing
  write becomes the same value on the schedule the test reads, or an assertion of `TIMING_ON_SCHEDULE`.
- Modify: `docs/manual.md` (the schedules section: a schedule owns its sheet timing; a new one starts from the game's
  suggestion for its raid; the raid's rules no longer hold timing)

**Interfaces:**
- Produces:
  - `SCHEDULE_TIMING` (Global constraints), `TIMING_ON_SCHEDULE`.
  - `effective_raid(base: dict, over: dict) -> dict` — the merge `raid_def` does today (profile → guild override →
    `RAID_DEFAULTS`, comp per role, weights per key, derived nudge, booleans parsed), lifted out unchanged so the lift
    in Task 3 can run on a bare `GuildConfig`. `Registry.raid_def` becomes `effective_raid(profile raid, raid_override)`.
  - `write_out_timing(row: dict, rd: dict) -> dict` — a copy of the row (row shape) with every `SCHEDULE_TIMING` key
    set to what `schedule_def_of({**rd, "schedules": [row]}, row["id"])` resolves for it today (own → raid → defaults
    for a recurring row, own → `ADHOC_DEFAULTS` for a pickup row, the halfway reminder where the row moves the opening
    or the lock without its own). Pure.
  - `Registry.timing_suggestion(raid: str) -> dict` — the nine values the profile suggests for a new schedule of
    `raid`: `{k: effective_raid(profile raid, {})[k] for k in SCHEDULE_TIMING}`.

**Rules the implementer must keep:**
- `_set_raid_rows` (the Registry method; the pure `_set_raid_rows_of` the phase-1 lift uses is unchanged) passes every
  row through `write_out_timing` against `effective_raid(profile raid, the raid's current override)` before storing —
  after Task 3's lift no override holds timing, so that is the profile's suggestion; before it, a row keeps reading
  what it read. So a new
  schedule, a new `default` row from `slots`, and the whole-list write from the wizard are all seeded from the profile
  (pickup rows from `ADHOC_DEFAULTS`), and a stored value is never replaced.
- `schedule_value` for a timing field with `None` / an inherit word still returns `None`; the row then loses the key
  and `_set_raid_rows` seeds it again from the suggestion. So "inherit" now means "the game's suggestion".
- `set_schedule`: when the fields change `signup_lead_hours` or `lock_hours_before` but not `nudge_hours_before`, and
  the stored reminder would fall outside `[lock, lead]`, the reminder becomes `default_nudge_hours(lead, lock)`.
- `_apply_raid_override` raises `RegistryError(TIMING_ON_SCHEDULE)` for any `SCHEDULE_TIMING` field, before anything
  changes. `_check_raid` keeps only `_check_schedules` and the comp rule (its raid-level timing ordering checks go).
- `split_policy`, `weight_*`, `lockout_days`, `duration_hours`, `first_open`, `notes`, `auto`, `slots`, `schedules`
  are unchanged.
- A follow-on schedule (`follow`) still stores as lineup entry 2 with its lead's timing on the entry (C19).

- [ ] **Step 1: Write the failing tests**

```python
TIMING = R.SCHEDULE_TIMING


def entry(reg, raid, sid):
    return next(e for e in reg.config.schedules if e["lineup"][0]["raid"] == raid and e["id"] == sid)


def test_a_new_schedule_is_seeded_from_the_profile(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30"}, "t")
    e = entry(reg, RID, "tue")
    assert {k: e[k] for k in TIMING} == reg.timing_suggestion(RID)
    assert e["signup_lead_hours"] == 48 and e["lock_hours_before"] == 24  # profiles/forever/raids.yaml barrow_deeps


def test_a_pickup_schedule_is_seeded_from_the_first_come_values(reg):
    reg.set_schedule(RID, "pug", {"kind": "pickup"}, "t")
    e = entry(reg, RID, "pug")
    assert {k: e[k] for k in TIMING} == {k: R.ADHOC_DEFAULTS[k] for k in TIMING}


def test_run_times_alone_seed_the_default_schedule(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    assert set(TIMING) <= set(entry(reg, RID, "default"))


def test_a_stored_value_is_kept_and_inherit_means_the_suggestion(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "confirm_hours_before": 12}, "t")
    assert entry(reg, RID, "tue")["confirm_hours_before"] == 12
    reg.set_schedule(RID, "tue", {"confirm_hours_before": "inherit"}, "t")
    assert entry(reg, RID, "tue")["confirm_hours_before"] == reg.timing_suggestion(RID)["confirm_hours_before"]


@pytest.mark.parametrize("field", R.SCHEDULE_TIMING)
def test_the_raid_refuses_timing(reg, field):
    before = (copy.deepcopy(reg.config.raids), copy.deepcopy(reg.config.schedules))
    with pytest.raises(RegistryError, match="belongs to each schedule"):
        reg.set_raid_override(RID, field, "true" if field in R.RAID_BOOL_FIELDS else 30, "t")
    assert (reg.config.raids, reg.config.schedules) == before


def test_moving_the_lock_past_the_reminder_moves_the_reminder(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "signup_lead_hours": 48, "lock_hours_before": 24, "nudge_hours_before": 30}, "t")
    reg.set_schedule(RID, "tue", {"lock_hours_before": 36}, "t")
    e = entry(reg, RID, "tue")
    assert e["lock_hours_before"] == 36 and e["nudge_hours_before"] == R.default_nudge_hours(48, 36)


def test_a_reminder_inside_the_new_window_stays(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "signup_lead_hours": 48, "lock_hours_before": 12, "nudge_hours_before": 30}, "t")
    reg.set_schedule(RID, "tue", {"lock_hours_before": 24}, "t")
    assert entry(reg, RID, "tue")["nudge_hours_before"] == 30


def test_the_follow_on_editor_still_writes_lineup_entry_two(reg):
    reg.set_schedule(OTHER, "tuesday", {"kind": "weekly", "slots": "Tue 18:30", "follow": f"{RID} x2"}, "t")
    e = entry(reg, OTHER, "tuesday")
    assert e["lineup"][1] == {"raid": RID, "rosters": 2} and set(TIMING) <= set(e)


def test_raid_def_is_effective_raid(reg):
    assert reg.raid_def(RID) == R.effective_raid(reg.profile.raids[RID], reg.raid_override(RID))


def test_a_one_off_still_uses_the_first_come_values(reg):
    sd = reg.schedule_def(RID, R.ADHOC_SCHEDULE)
    assert {k: sd[k] for k in TIMING} == {k: R.ADHOC_DEFAULTS[k] for k in TIMING}


def test_a_schedule_edit_never_reaches_an_open_run(reg, rs):
    """ensure_run a run of schedule `tue` (its run dict stamped), then set_schedule(lock_hours_before=36,
    confirm_hours_before=12): the run dict's cutoff_hard_hours, confirm_hours and fill_ask_hours are what they were."""
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_schedule_timing.py -q`.
- [ ] **Step 3: Implement** the interfaces and rules above.
- [ ] **Step 4: Update the listed tests,** then run
  `uv run pytest tests/test_schedule_timing.py tests/test_guild_schedules.py tests/test_schedules.py tests/test_registry_raids.py tests/test_roster_p1_classes.py tests/test_phase0.py tests/test_follow_on.py tests/test_raid_night.py -q` → all pass.
- [ ] **Step 5: Write the manual change,** then commit — "Schedules: each one holds its sheet timing in full; a raid
  refuses timing."

### Task 3: Lift an older file's raid-level timing onto its schedules

**Files:**
- Modify: `src/oibot_gm/registry.py` (`load_config`, `migrate_schedules`), `src/oibot_gm/discord_bot.py`
  (`_schedules_save`, `_schedules_migrate`)
- Test: `tests/test_schedule_timing.py` (reuse `old_store`, `open_registry`, `commit_count`, `base_config` from
  `tests/test_guild_schedules.py`)

**Interfaces:**
- Consumes: Task 2's `effective_raid`, `write_out_timing`, `SCHEDULE_TIMING`.
- Produces:
  - `lift_timing(cfg: GuildConfig, raids: dict, skip: set[str] = frozenset()) -> tuple[list[str], list[str], bool]` →
    `(moved, dropped, owed)`. For every readable stored entry: `write_out_timing(row, effective_raid(profile raid,
    _raid_override_of(cfg, raid)))` stored back in place. Then every `SCHEDULE_TIMING` key is popped from every raid
    override (an override left empty is dropped). Then every `cfg.rosters` entry without `fill_ask_hours` and without
    `test` gets the value the old merge gives its `instance`. `moved` = one line per schedule that took a value from
    its raid ("Hyjal Summit · tuesday: reminder 72 hours before"); `dropped` = one line per raid whose override held
    timing and that leads no schedule ("Barrow Deeps: reminder 36 hours before"); `owed` = anything changed. Raids in
    `skip` (the phase-1 lift's refused raids) are left exactly as they are. Idempotent: a second call returns
    `([], [], False)`.
  - `load_config` calls it after `lift_schedules` with `skip` = the refused raids' ids, keeps
    `self.timing_moved` / `self.timing_dropped`, and ORs `owed` into `_schedules_owed`.
  - `migrate_schedules` saves once when the schedule lift OR the timing lift owes a save, message
    `"sheet timing moved onto each schedule (by …)"` when only timing moved; it still returns the schedule lift's lines.
    It copies the timing lines into `self.timing_saved: list[str]` (the ops line's words) before clearing them.
  - The start hook posts, per guild with `timing_saved` non-empty, ONE ops line:
    `"Sheet timing now belongs to each schedule. " + ("Moved: " + " · ".join(moved) + ". " if moved else "") + ("Dropped, with no schedule to take it: " + " · ".join(dropped) + "." if dropped else "")`
    — a line is `"<raid name> · <schedule name, else id>: <value words>"` / `"<raid name>: <value words>"`, values
    joined by ", ", in the order of `SCHEDULE_TIMING`; hours read as `flows_config.hours_words` writes them ("3 days",
    "36 hours" — move `hours_words` to `constants.py` and re-export it from `flows_config`, since `registry` can't import
    a Discord module); words per key: "sheet opens N before", "reminder N before", "locks N before", "confirm-by N
    before", "a fill ask waits N", and "reminder off" / "automatic fill on" / "opening DM on" / "confirmations off".

- [ ] **Step 1: Write the failing tests**

```python
OLD_TIMING = {
    "raids": {
        "hyjal_summit_forever": {"nudge_hours_before": 72},
        "barrow_deeps": {"nudge_hours_before": 36, "lockout_days": 7},
    },
    "schedules": [{"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}], "kind": "weekly", "slots": ["Tue 18:30"]}],
}


def test_lifting_timing_writes_it_out_and_drops_what_has_no_schedule(profile):
    cfg = R.GuildConfig(**{**base_config(), **copy.deepcopy(OLD_TIMING)})
    moved, dropped, owed = R.lift_timing(cfg, profile.raids)
    e = cfg.schedules[0]
    assert e["nudge_hours_before"] == 72 and e["signup_lead_hours"] == 120 and e["confirm"] is True
    assert cfg.raids == {"barrow_deeps": {"lockout_days": 7}}
    assert owed and moved == ["Hyjal Summit · tuesday: reminder 3 days before"]
    assert dropped == ["Barrow Deeps: reminder 36 hours before"]
    assert R.lift_timing(cfg, profile.raids) == ([], [], False)


def test_an_old_file_reads_its_timing_at_once(old_store_with, open_registry):   # old_store_with(OLD_TIMING): phase 1's old_store fixture, parametrised by the file
    reg = open_registry(old_store_with(OLD_TIMING))
    assert reg.schedule_def("hyjal_summit_forever", "tuesday")["nudge_hours_before"] == 72
    assert reg.timing_dropped and not any(k in (reg.config.raids.get("barrow_deeps") or {}) for k in R.SCHEDULE_TIMING)


def test_the_timing_lift_is_committed_once(old_store_with, open_registry):
    store = old_store_with(OLD_TIMING)
    reg = open_registry(store)
    n0 = commit_count(store)
    reg.migrate_schedules()
    assert commit_count(store) == n0 + 1 and reg.timing_saved
    reg.migrate_schedules()
    assert commit_count(store) == n0 + 1 and open_registry(store).timing_dropped == []


def test_every_schedule_reads_the_same_before_and_after(old_store_with, open_registry):
    """The old merge, computed by hand from the old file, equals the new read for every key of SCHEDULE_TIMING."""


def test_a_live_run_keeps_its_fill_ask_wait(old_store_with, open_registry):
    """A run dict opened from `default` without fill_ask_hours, its raid at fill_ask_hours 2 in the old file:
    after the load, the run dict has fill_ask_hours 2 and rc.ask_deadline(reg, ev, at) is at + 2 hours."""


def test_a_one_off_and_its_follow_on_are_untouched(old_store_with, open_registry):
    """Two run dicts copied from big-guys-for's live pair (adhoc lead + `after` follow-on, both with fill_ask_hours):
    after the load and migrate_schedules, config.rosters is byte-for-byte what the file held."""


def test_a_refused_raid_keeps_its_timing(old_store_with, open_registry):
    """A raid whose old `schedules` key can't be lifted (phase 1's refused case) and that holds lock_hours_before 12:
    the key stays in its override, the schedule still reads 12, and the raid is in neither moved nor dropped."""


def test_a_preview_copy_never_commits_the_timing_lift(old_store_with, open_registry):
    """reg.shadow().migrate_schedules() writes no commit."""
```

  The four tests given as docstrings are written to exactly what the docstring states. Build `old_store_with` from
  phase 1's `old_store` / `_write_old`; no new way to open a registry.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** `lift_timing`, the `load_config` / `migrate_schedules` changes and the ops line.
- [ ] **Step 4: Run** `uv run pytest tests/test_schedule_timing.py tests/test_guild_schedules.py tests/test_schedules.py -q` → pass.
- [ ] **Step 5: Commit** — "Schedules: an older file's raid-level sheet timing moves onto its schedules at start."

### Task 4: The lock fills minimums first and never stops on one

**Files:**
- Modify: `src/oibot_gm/roster/solver.py`
- Modify: `tests/test_short_role_solver.py` (`OPTS` → `minimums={"tank": 2, "healer": 2}`, no `role_max`; same assertions)
- Test: `tests/test_minimums_solver.py` (new, `pytestmark = pytest.mark.slow`, the `pool()` helper of
  `test_short_role_solver.py`)

**Interfaces:**
- Produces:
  - `SolveOptions.minimums: dict[str, int] | None` — slot → per-roster minimum; a slot is a role (`tank`, `healer`,
    `melee`, `ranged`), a class (`Paladin`) or `Class:Spec`. `role_min` and `role_max` are removed.
  - `scaled_role_minimums(rules: dict, raid_size: int) -> dict[str, int]` (replaces `scaled_role_bounds`; ceil, as
    today's mins). `_setup` uses it for tank / healer when `opts.minimums` is None, then lays `opts.minimums` over it.
    `_Model.role_bounds` becomes `_Model.minimums: dict[str, int]`.
  - `_slot_expr(s, slot, r=None) -> list` — a role: `_role_expr`; a class: the seats of players of that class; a
    `Class:Spec`: the seats of players signed with that spec.
  - `_minimum_terms(s, roster_bonus)` replaces `_add_role_bounds` and `_short_role_terms`: per slot and per roster
    (the whole pool when one roster) an int var `filled ∈ [0, n]`, `filled ≤ sum(_slot_expr)`, objective
    `+ MINIMUM_REWARD × filled`.
  - `_extra_role_terms(s, roster_bonus)`: per roster, for `tank` and `healer`, `extra ≥ sum(_role_expr) − n`,
    `extra ≥ 0`, objective `− EXTRA_ROLE_COST × extra`.
  - With `top` as `_short_role_terms` computes it today: `EXTRA_ROLE_COST = (top + top // 2 + SHORT_ROLE_MARGIN) ×
    SCALE` and `MINIMUM_REWARD = 2 × EXTRA_ROLE_COST`. No minimum adds a constraint; the seat count and group caps
    stay as they are.

- [ ] **Step 1: Write the failing tests**

```python
def test_one_hybrid_for_two_minimums_on_one_roster_still_solves():
    ps = pool([("Warrior", "Protection", "tank", None, 1), ("Druid", "Guardian", "tank", "Restoration", 1), ("Priest", "Holy", "healer", None, 1), ("Mage", "Fire", "ranged", None, 9)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}))[0]
    assert {"p00", "p01", "p02"} <= {p.signup_name for p in r.selected}


def test_seats_past_the_minimums_go_to_damage_dealers():
    ps = pool([("Warrior", "Protection", "tank", None, 4), ("Priest", "Holy", "healer", None, 4), ("Mage", "Fire", "ranged", None, 6)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}))[0]
    assert r.role_counts == {"tank": 2, "healer": 2, "ranged": 6}


def test_an_extra_healer_sits_only_when_nobody_else_joined():
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Priest", "Holy", "healer", None, 4), ("Mage", "Fire", "ranged", None, 4)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}))[0]
    assert len(r.selected) == 10 and r.role_counts["healer"] == 4


def test_a_class_minimum_is_met_when_the_joiners_allow():
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Paladin", "Holy", "healer", None, 2), ("Priest", "Holy", "healer", None, 2),
               ("Paladin", "Retribution", "melee", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2, "Paladin": 3}))[0]
    assert sum(1 for p in r.selected if p.cls == "Paladin") >= 3


def test_a_class_minimum_the_joiners_cannot_meet_never_stops_the_lock():
    ps = pool([("Warrior", "Protection", "tank", None, 2), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2, "Paladin": 4}))[0]
    assert len(r.selected) == 10


def test_officers_placing_an_extra_tank_is_kept():
    ps = pool([("Warrior", "Protection", "tank", None, 3), ("Priest", "Holy", "healer", None, 2), ("Mage", "Fire", "ranged", None, 8)])
    r = solve_rosters(FOREVER, ps, "barrow_deeps", SolveOptions(raid_size=10, minimums={"tank": 2, "healer": 2}, force_in=("p00", "p01", "p02")))[0]
    assert {"p00", "p01", "p02"} <= {p.signup_name for p in r.selected}
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_minimums_solver.py -q`.
- [ ] **Step 3: Implement** the interfaces above. Callers outside the solver still pass `role_min` until Task 5; keep
  them compiling by changing `comp.py`'s two calls and `raidcycle._solve_run` to `minimums=` with the same numbers
  in this task (no other behaviour change there).
- [ ] **Step 4: Run** `uv run pytest tests/test_minimums_solver.py tests/test_short_role_solver.py tests/test_follow_on_solver.py tests/test_roster_p0_bugs.py -q` → pass.
- [ ] **Step 5: Commit** — "Solver: every minimum is a preference filled first; a tank or healer past its minimum
  sits only when nobody else can."

### Task 5: One minimum per role or class, stored, read and migrated

**Files:**
- Modify: `src/oibot_gm/registry.py`, `src/oibot_gm/raidcycle.py`, `src/oibot_gm/comp.py`, `src/oibot_gm/roster/explain.py`
  (only if it reads `role_bounds` max), `profiles/forever/raids.yaml`, `profiles/forever/comp_rules.yaml`,
  `profiles/tbc/comp_rules.yaml`, `src/oibot_gm/profiles.py` (if the loader names `comp`), `scripts/lower_schedules.py`
- Test: `tests/test_minimums.py` (new); update `tests/test_phase3_setup.py`, `tests/test_roster_p0_bugs.py`,
  `tests/test_registry_raids.py`, `tests/test_structured_ops.py`, `tests/test_phase0.py` where they write or read
  `comp` max, `comp_targets` or `set_comp_targets`
- Modify: `docs/manual.md` (§1a: **Minimum** replaces **Limits** and **Aims**; the make-up paragraphs of §4 / §6:
  one number per role or class, what the lock does with it, short shown on the run, never a stop)

**Interfaces:**
- Consumes: Task 4's `SolveOptions.minimums`, `scaled_role_minimums`.
- Produces:
  - Stored: `raids.<id>.minimums: {slot: {"min": int, "note"?: str}}` (the note ≤ `COMP_NOTE_MAX`, prose). The keys
    `comp` and `comp_targets` are no longer written. Profile `raids.yaml` carries `minimums: {tank: n, healer: n}`
    (forever's numbers: each raid's old `comp` min; its `dps` entry and every max go); `comp_rules.yaml` `roles` keep
    only `min`.
  - `Registry.minimums(instance: str | None, size: int | None = None) -> dict[str, int]` — guild entries over the
    profile's over the comp rules' tank / healer minimums; scaled by `round(n × size / raid size)` when `size` differs;
    zero entries left out.
  - `Registry.role_bounds(instance, size)` keeps its name and returns `{"tank": {"min": n}, "healer": {"min": n},
    "dps": {"min": 0}}` from `minimums` — no `"max"` key anywhere.
  - `Registry.set_minimums(raid: str, values: dict[str, dict | None], by: str, rev: str | None = None) -> list[str]`
    replaces `set_comp_targets` (same `@_config_verb(_raid_section)`, one save). A run key → `MINIMUM_ON_RAID`; a
    `max` key → `NO_UPPER_LIMIT`; `min` above the raid's size → refused in one sentence; `None` clears the slot.
    Lines: `"<Raid> · Healers: at least 3"`, `"<Raid> · Paladins: back to the game's minimum"`.
  - `lift_minimums(cfg: GuildConfig) -> list[str]` — per raid: `comp[tank|healer].min` → `minimums`; a class or
    spec entry of `comp_targets` → `minimums` with its note; a `melee` / `ranged` target → `minimums`; a `tank` /
    `healer` target, every max and `comp.dps` are dropped and named; the run dicts' `comp_targets` are dropped and
    named. Called by `load_config` after `lift_timing`; its lines join `timing_saved`'s ops line as
    `"Make-up is one minimum per role or class now: …"`. Idempotent.
  - `lower_minimums(cfg: GuildConfig, raids: dict) -> list[str]` — the way back: role entries → `comp: {role: {min}}`
    (both tank and healer written, the missing one from the profile), the rest → `comp_targets: {slot: {min, note?}}`,
    `minimums` removed.
  - `raidcycle.first_come_out(players, seats, role_min, fixed) -> set[str]` — `role_max` removed; a tank or healer
    joiner whose role already meets its minimum waits until every other joiner has a seat, then takes what is left in
    signup order.
  - `raidcycle.gaps(...)` adds `"classes": {slot: short}` for class / spec / melee / ranged minimums the joiners (or
    the locked rosters) don't meet; `needs()` and the fill engine are unchanged (they ask for roles only);
    `raid_views.gaps_text` and `rc.short_why` name a class short ("short 1 Paladin").
  - `raidcycle._solve_run` passes `minimums = {**reg.minimums(raid, size), **merged policy role_min}`.
  - `comp.ideal_comp` / `comp.readiness`: role lines from `minimums` (why: "the raid's minimum" / "the game's
    minimum"); no dps line; `CompLine.max` and readiness `comp[].max` and `over` are removed (and their readers in
    `help.py` and `discord_pool.py`); the officer source reads the stored `minimums`.
  - `RAID_IDENTITY`: `comp`, `comp_targets` → `minimums`. `raid_shell` carries `minimums`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_minimum_is_one_number_per_slot(reg):
    reg.set_minimums(RID, {"healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}}, "t")
    assert reg.config.raids[RID]["minimums"] == {"healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}}
    assert reg.minimums(RID)["healer"] == 3 and reg.role_bounds(RID, 10)["healer"] == {"min": 3}


def test_a_maximum_is_refused_and_nothing_changes(reg):
    before = copy.deepcopy(reg.config.raids)
    with pytest.raises(RegistryError, match="no upper limit"):
        reg.set_minimums(RID, {"healer": {"min": 2, "max": 3}}, "t")
    assert reg.config.raids == before


def test_a_run_key_is_refused(reg, open_run_key):     # any live run key the suite's fixtures open
    with pytest.raises(RegistryError, match="belong to the raid"):
        reg.set_minimums(open_run_key, {"healer": {"min": 2}}, "t")


def test_minimums_scale_with_the_roster_size(reg):
    reg.set_minimums("hyjal_summit_forever", {"healer": {"min": 6}}, "t")
    assert reg.minimums("hyjal_summit_forever", 10)["healer"] == 3


def test_lifting_comp_and_aims(profile):
    cfg = R.GuildConfig(**{**base_config(), "raids": {RID: {"comp": {"tank": {"min": 2, "max": 3}, "healer": {"min": 3, "max": 4}, "dps": {"min": 5, "max": 6}},
                                                            "comp_targets": {"Paladin": {"min": 2, "max": 3, "note": "blessings"}, "healer": {"min": 4}, "melee": {"min": 3}}}}})
    lines = R.lift_minimums(cfg)
    assert cfg.raids[RID] == {"minimums": {"tank": {"min": 2}, "healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}, "melee": {"min": 3}}}
    assert lines and R.lift_minimums(cfg) == []


def test_lowering_and_lifting_again_round_trips_the_minimums(profile):
    """lower_minimums then lift_minimums gives back the same `minimums` map."""


def test_first_come_takes_a_late_damage_dealer_before_an_extra_healer():
    """Seats 5, healer minimum 1: joiners in order healer, healer, mage, mage, mage, mage → the second healer is out."""


def test_a_class_short_is_named_and_never_asked_for(reg, rs):
    """A run of a raid with a Paladin minimum of 2 and no paladin joined: gaps()["classes"] == {"Paladin": 2},
    gaps_text names it, and needs()["roles"] holds no Paladin."""
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** storage, readers, the lift and the profile YAML. Point every caller of
  `set_comp_targets` / `comp_targets` (configops' `comp` op and `_comp_entry`, `flows_config.CompTargetsBlock`'s
  preview, `/api/readiness`'s `targets`, `help.py`'s config lines) at `set_minimums` / `minimums` in this task with no
  change of wording, so the suite stays green; Task 6 changes their words and shapes.
- [ ] **Step 4: Extend `scripts/lower_schedules.py`:** it lowers minimums first (`lower_minimums`), then schedules; a
  new `--minimums-only` flag lowers only the minimums (the way back to the phase-1 code, which reads timing on
  schedules as their own values and needs nothing else). Update its docstring with both paths. Add
  `test_the_script_lowers_minimums_only_when_asked` to `tests/test_guild_schedules.py`'s script tests.
- [ ] **Step 5: Run** `uv run pytest tests/test_minimums.py tests/test_phase3_setup.py tests/test_roster_p0_bugs.py tests/test_registry_raids.py tests/test_structured_ops.py tests/test_phase0.py tests/test_guild_schedules.py tests/test_rosters_page.py -q` → pass.
- [ ] **Step 6: Write the manual change,** then commit — "Make-up: one minimum per role or class; no upper limit; an
  older file's limits and aims become minimums at start."

### Task 6: Officers edit raids and schedules: plain text, Discord, MCP, routes

**Files:**
- Modify: `src/oibot_gm/configops.py`, `src/oibot_gm/registry.py` (`PLAIN_GROUPS`), `src/oibot_gm/flows_config.py`,
  `src/oibot_gm/discord_registry.py` (the `/gm config raid` description), `src/oibot_gm/help.py`,
  `src/oibot_gm/web/api_ops.py`, `src/oibot_gm/web/api.py`, `src/oibot_gm/mcp_server.py`
- Test: `tests/test_officer_raids.py` (new); update `tests/test_parity_ops_owner.py`, `tests/test_parity_ops_setup.py`,
  `tests/test_parity_matrix.py`, `tests/test_parity_routes.py`, `tests/test_flows_config.py`, `tests/test_mcp.py`,
  `tests/test_plain_text.py`, `tests/test_plain_permissions.py`, `tests/test_configops.py`, `tests/test_agents.py`,
  `tests/test_news.py`, `tests/test_parity_d2.py` wherever they expect "Owner only" or the old comp fields
- Modify: `docs/manual.md` (who may: officers edit raids and schedules; §8's owner list loses them; `/gm config raid`
  in the officers' command list; the plain-text permissions table's two group names)

**Interfaces:**
- Consumes: Task 2's `TIMING_ON_SCHEDULE`, Task 5's `set_minimums`, `NO_UPPER_LIMIT`, `MINIMUM_ON_RAID`.
- Produces:
  - `OWNER_OPS` loses `raid_set`, `raid_reset`, `schedule_set`, `schedule_remove`. Auras stay the owner's.
  - `OP_GROUP`: those four → `"comp"`. `PLAIN_GROUPS["comp"] = ("Raids, schedules and policy", ["officers"],
    "Barrow Deeps runs Tue 7:30 pm · at least 3 healers in Barrow Deeps · add a loot rule")`;
    `PLAIN_GROUPS["raids"] = ("Auras", ["owner"], "fortitude is party-wide")`. Group ids do not change, so stored
    plain policies keep working. `GROUP_WHERE` follows.
  - The `comp` structured kind and the `comp_target` / `comp_target_clear` ops write through `set_minimums`;
    `_comp_entry` returns `{"min", "note"}`; the schema text, `describe` words and `config_summary` say "at least N"
    and never "at most". `raid_set` with a timing field → `TIMING_ON_SCHEDULE`; with `tank_min … dps_max` → one
    sentence pointing to the minimum ("Set a role's minimum as a minimum: at least N tanks."). Raid lines in
    `current_config_text`, `config_summary` and `help.py` drop raid-level timing and list each schedule's own.
  - `/api/admin/raid`, `/api/admin/raid/reset`, `/api/admin/raid/schedule`, `/api/admin/raid/schedule/remove`: no
    `need_owner`; the tier is `authorize_ops`' (a live officer). `/api/raids` rows drop the nine timing keys, `comp`
    and `comp_targets` and gain `minimums` (stored, with notes) and `minimum` (the effective `Registry.minimums`);
    `/api/readiness` rows replace `targets` / `bounds` with the same two.
  - Discord: `RaidConfigWizard` and the flow `raid_config` take an officer guard (live, `discord_registry.is_officer`);
    the hub loses the `cadence` section; `behaviour` keeps only the split ("How one night's groups are made"); the
    `comp` section opens `CompTargetsBlock`, which edits minimums (a number select and an optional reason, no
    at-most); the schedule screens show the schedule's own nine values and offer "the game's suggestion" in place of
    "the raid's". `CompTargetsWizard` (`/roster comp`) is the same block, titled "Minimums". `help.gate_of` returns
    `"officer"` for `/gm config raid`; its description reads "Officers: a raid's rules — schedules, make-up, seat
    weights, lockout, notes".
  - MCP: `set_comp_target` → `set_minimum(raid: str, slot: str, min: int | None = None, note: str | None = None,
    clear: bool = False, preview: bool = False, guild: Guild = None)`; `raid_set`'s docstring drops timing and limits;
    `schedule_set` / `schedule_remove` / `raid_set` docstrings say "officers"; list rows drop timing keys.
    `tests/test_parity_matrix.py`'s `comp_target` row names `set_minimum`.

- [ ] **Step 1: Write the failing tests** (`tests/test_officer_raids.py`)

```python
@pytest.mark.parametrize("change", [
    {"kind": "raid", "target": RID, "fields": {"notes": "officers wrote this"}},
    {"kind": "schedule", "target": f"{RID}:tue", "fields": {"kind": "weekly", "slots": "Tue 19:30"}},
    {"kind": "comp", "target": RID, "fields": {"healer": {"min": 3}}},
])
def test_an_officer_saves_raid_and_schedule_changes_on_the_site(officer_client, change):
    r = officer_client.post("/api/ops/change", json={"changes": [change], "confirmed": True})
    assert r.status_code == 200 and not r.json().get("refused")


def test_the_admin_raid_routes_take_an_officer(officer_client):
    assert officer_client.post("/api/admin/raid/schedule", json={"instance": RID, "id": "tue", "fields": {"kind": "weekly", "slots": "Tue 19:30"}, "confirmed": True}).status_code == 200


def test_a_member_is_still_refused(member_client):
    r = member_client.post("/api/ops/change", json={"changes": [{"kind": "raid", "target": RID, "fields": {"notes": "x"}}], "confirmed": True})
    assert "Only officers" in " ".join(r.json().get("refused") or [])


def test_plain_text_lets_officers_change_a_raid_by_default(reg):
    assert reg.may_plain("comp", OFFICER_ID, officer=True) is None   # the call's real signature; OFFICER_ID from the suite


def test_raid_set_refuses_timing_and_limits_in_one_sentence(reg):
    with pytest.raises(RegistryError, match="Change it on the schedule"):
        configops.apply(reg, ConfigOp(op="raid_set", target=RID, field="lock_hours_before", value="12"), "t", is_owner=False)


async def test_the_raid_wizard_opens_for_an_officer(harness_officer):
    """tests/wizard_harness drives flow raid_config as an officer: the hub shows, no 'Owner only.', no section named
    for sheet timing, and the make-up screen offers no at-most select; every screen lints."""


def test_gate_of_names_officers_for_the_raid_wizard():
    assert help.gate_of("/gm config raid") == "officer"


def test_set_minimum_is_the_mcp_tool():
    assert "set_minimum" in mcp_tool_names() and "set_comp_target" not in mcp_tool_names()
```

  Use the suite's existing officer / member clients, harness and helper names (`tests/test_parity_ops_owner.py` and
  `tests/test_flows_config.py` show them); keep the assertions.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces above. Every new or changed string passes the Copy rule; any sentence a
  confirm screen prints passes the Confirm rule.
- [ ] **Step 4: Run** `uv run pytest tests/test_officer_raids.py tests/test_parity_matrix.py tests/test_parity_ops_owner.py tests/test_parity_ops_setup.py tests/test_parity_routes.py tests/test_parity_d2.py tests/test_flows_config.py tests/test_mcp.py tests/test_plain_text.py tests/test_plain_permissions.py tests/test_configops.py tests/test_structured_ops.py tests/test_copy.py tests/test_confirm_readability.py tests/test_quiet_edges.py -q` → pass.
- [ ] **Step 5: Write the manual change,** then commit — "Officers edit raids and schedules everywhere; the raid
  wizard loses sheet timing and edits minimums."

### Task 7: The site: no timing on the raid, one Minimum editor, officers edit

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/pages/Raids.tsx`, `frontend/src/components/RaidCard.tsx` (only what
  the raid rule's week used), `frontend/src/components/ScheduleCard.tsx`, `frontend/src/components/CompTargets.tsx`,
  `frontend/src/pages/Readiness.tsx`; the committed bundle under `src/oibot_gm/web/static/app`
- Test: `tests/test_frontend_source.py` (add), `tests/test_copy.py` (runs over the new strings)
- Modify: `docs/manual.md` (the Raids and Readiness descriptions in the officers' paragraph and §6)

**Interfaces:**
- Consumes: Task 6's `/api/raids` and `/api/readiness` shapes (`minimums`, `minimum`), the `comp` change kind.
- Produces:
  - Raids page: `RuleCard`'s draft and editor lose `HOURS`, `SWITCHES` and the "A run's week" section fed by the raid;
    "Edit rules" and the schedule editors show for every viewer of the page (the route is officers-only); the
    `owner` prop and "The owner edits the rules; officers set the aims." go.
  - `CompTargets.tsx` becomes the one make-up editor: a row per slot = slot icon + its minimum (Iconography rule:
    icon and number, the reason in the tooltip); the editor rows are slot · at least · why; one "Save the minimums"
    button through `useOpsSave` with `revs: {"config:raids:<id>": rev}`; no limits row, no at-most input, no
    "(owner)" / "(officers)". Empty: "No minimums set. The lock uses the game's minimum for tanks and healers. Add one
    for a role or class below."
  - `ScheduleCard.tsx`: every timing input shows the schedule's own value (no "empty follows the raid's" placeholder,
    `SOURCE_WORDS` / `inherited` / `ADHOC_DEFAULT` go); clearing a field shows "the game's suggestion" as its hint.
  - `Readiness.tsx`: "Against the minimums"; the table's want column is the minimum alone (no range, no amber over);
    the "Limits on the Raids page" link goes.

- [ ] **Step 1: Write the failing tests** in `tests/test_frontend_source.py`: no `owner` prop on `RuleCard`;
  `Raids.tsx` contains none of `signup_lead_hours`, `nudge_hours_before`, `lock_hours_before`,
  `confirm_hours_before`, `fill_ask_hours`; `CompTargets.tsx` and `Readiness.tsx` contain no `"limits"`, `"aims"`,
  `"at most"`, `max:`; `ScheduleCard.tsx` contains no `"the raid's"`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** then `cd frontend && npm run build`.
- [ ] **Step 4: Run** `uv run pytest tests/test_frontend_source.py tests/test_copy.py -q` and
  `uv run python scripts/check_bundle.py` → pass. Take the screenshot audit of Raids and Readiness
  (`uv run python scripts/shots.py`, desktop + phone) and look at both.
- [ ] **Step 5: Write the manual change,** then commit with the bundle — "Raids and Readiness: no sheet timing on a
  raid, one minimum per role or class, officers edit."

### Task 8: Design, agent guide, full check and a rehearsal on a copy of the real data

**Files:**
- Modify: `docs/design.md` (§5.67 gains "Phase 2 built": timing on schedules, the lift and its ops line, minimums and
  the solver's two terms, the permission move, the way back; pointers from §5.27 and §5.44), `CLAUDE.md` (the Copy
  rule's glossary words: "a minimum, never limits or aims"; the `registry.py` entry: `SCHEDULE_TIMING`,
  `effective_raid`, `write_out_timing`, `lift_timing`, `minimums` / `set_minimums` / `lift_minimums` /
  `lower_minimums`; the `solver.py` line; `configops` / `flows_config` / `mcp_server` lines that name aims or the
  owner tier for raids)

- [ ] **Step 1: Write §5.67's phase 2 part and update `CLAUDE.md`.** Only what is built.
- [ ] **Step 2: Run the full check** in the background — `uv run python scripts/check_commands.py > <scratch>/check.log 2>&1; echo "exit=$?" >> <scratch>/check.log`
  (about 20 minutes; never piped through `tail` before `&&`; no edits while it runs). Then `uv run pytest -q -m slow
  tests/test_minimums_solver.py tests/test_short_role_solver.py tests/test_follow_on_solver.py`. Expected: `exit=0`
  and the slow tests pass.
- [ ] **Step 3: Rehearse on a copy of the real data.** Copy `/Users/jonathanlin/dev/oibot_gm-data` twice into a scratch
  directory (A and B), remove each copy's git remote, and open every guild with `GitStore(<copy>, push=False)`. Check
  out the phase-1 code (`git worktree add <scratch>/old 53b9153`) and run a script with it against A; run the same
  script with this branch against B after `migrate_schedules()`. The script prints, per guild, JSON of:
  `reg.schedules(rid)` and `{k: reg.schedule_def(rid, sid)[k] for k in SCHEDULE_TIMING}` for every raid and schedule;
  `rc.upcoming_rows(reg, rs, rid)`; and for every live run (big-guys-for: `hsf-1210-1830-adhoc-2026-12-10` and
  `bd-1210-2130-after-2026-12-10`) `rc.run_team`, `raid_views.run_times`, `rc.ask_deadline(reg, ev, <fixed time>)`,
  `rc.needs`, `rc.health`, `rc.run_split_reason` and `raid_views.sheet_message(reg, ev, team, ico=<stub>)[0].to_dict()`.
  Diff A against B: they must be equal. Report each guild's ops line text (expected: big-guys-for moves Hyjal
  Summit · tuesday's 72-hour reminder and drops Barrow Deeps' 36-hour reminder; oi moves Barrow Deeps · Raid nights'
  12-hour confirm-by and 36-hour reminder). Also run `rc.propose(reg, rs, ev, save=False)` on both live runs on A and
  B and report any difference as a D16 change, not a failure. Then run `scripts/lower_schedules.py --minimums-only` on
  B and confirm the phase-1 code loads it and prints the same JSON as A. Remove the scratch worktree. Nothing is
  written to the real data repo.
- [ ] **Step 4: Commit** — "Design §5.67 phase 2 and the agent guide: raids without times, minimums, officers edit."

## Deploying (the owner's call, not a task)

The first start writes one commit per guild (big-guys-for and oi have raid-level timing; every guild's schedules get
their timing written out) and posts one ops line per guild. Note the data repo's head before the restart.

Going back to the phase-1 code: stop the bot, run `uv run python scripts/lower_schedules.py --minimums-only`
(`--dry-run` first), start the older code. The older code is **main at 53b9153** (phase 1) — never the branch commit
4ef2e35, which cannot propose a run on its own (its solver lost a function its callers still used). Timing written on schedules reads there as each schedule's own values,
unchanged. What cannot come back: a raid-level timing value that had no schedule (named in the ops line), a role's
old maximum, a tank or healer aim. Going back further than phase 1: run the script without the flag.

## Calls made in this plan

- **The raid's merge is unchanged; the migration empties it.** `raid_def` still merges whatever a raid override
  holds; only a raid refused by the phase-1 lift can still hold timing, and it keeps reading it. If wrong: a hand-edit
  that puts timing back on a raid is read silently instead of refused at load.
- **Timing is the nine values of C7; `split_policy` stays on the raid as a default a schedule may override** (like
  weights, C6). If wrong: a later phase moves one more key with the same lift.
- **"Inherit" on a schedule's timing now means the game's suggestion for its first raid.** If wrong: an officer who
  clears a field expecting the old raid value gets the profile's.
- **A lock moved past the stored reminder moves the reminder to halfway** instead of refusing. If wrong: the reminder
  moves when an officer meant to keep it; the confirm diff shows it, so it is visible before Save.
- **Only `fill_ask_hours` is stamped on live run dicts.** Every other value a live run reads is already stamped at
  open or is `split_policy`, which still reads the raid. If wrong: a run opened by code older than the stamping reads
  a profile value for another key; the rehearsal compares every live read to catch this.
- **Storage key `minimums: {slot: {min, note?}}`**; the officer's reason is kept. D16's "one number" is the minimum;
  the reason is a tooltip. If wrong: one field to drop later.
- **A tank / healer aim, every maximum, `comp.dps` and run-level aims are dropped by the lift**, named in the ops line.
  No live guild holds any (checked 2026-11-23). If wrong: an aim set by a guild on other data is lost after start.
- **Minimums are solver preferences at two tiers** (`MINIMUM_REWARD = 2 × EXTRA_ROLE_COST`), never constraints, for one
  roster as well as several — phase 1's hard per-roster floor goes. If wrong: a lock with enough joiners could, in a
  rare tie, leave a minimum unmet; the reward dominates every seat value and synergy, so it should not.
- **A healer past the minimum with a damage offspec is switched rather than seated as an extra healer**, as today's
  role maximum did. If wrong: someone who joined to heal is rostered as damage; the role switch is shown on the card.
- **First come (one-offs) acts on tank and healer minimums only**; a class minimum on a one-off is a solver preference
  among the joiners in time. If wrong: a late paladin on a one-off doesn't jump the queue.
- **A class short is shown, never asked for:** the fill engine still asks for roles only. If wrong: a class minimum
  stays short after the lock until an officer asks someone.
- **Plain text: the four raid and schedule ops join the `comp` group** (officers by default), relabelled "Raids,
  schedules and policy"; `raids` becomes "Auras" (owner). Group ids are unchanged, so stored policies still load. If
  wrong: a guild that opened `raids` to a Discord role loses plain-text raid edits for that role until it opens `comp`.
  Both live guilds already open `raids` to officers.
- **Op and kind names (`comp_target`, `comp`) stay**; only the MCP tool is renamed (`set_minimum`), since its name is
  what a person types to Claude. If wrong: one rename later.
- **The way back is the existing script with `--minimums-only`** rather than a new script.

No requirement of the spec was found impossible. Two notes. D16 lists a raid's size among what officers edit, but no
surface edits a raid's size today (it is profile data; `_apply_raid_override` has no `size` field), so there is
nothing to move and this plan adds no size editor. And a tension: §5 Migration step 2 drops a raid-level value "with no
schedule", but a raid that is the second entry of another raid's lineup also never applied its own timing (a follow-on
reads its lead's), so its value is dropped and named too.
