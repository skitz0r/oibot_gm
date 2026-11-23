# Guild-level schedules with lineups (phase 1) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store schedules in `guild.yaml: schedules[]`, each with a `lineup`, instead of inside each raid's
override — with no change to anything a person sees.

**Architecture:** The stored shape moves; the read shape does not. `Registry.raid_def(instance)` keeps returning
`"schedules"` and `"slots"` in today's shape, rebuilt from the guild-level list, so `schedules_of`,
`schedule_def_of` and their ~60 callers are untouched. The three writers (`_apply_raid_override` for `slots` and
`schedules`, and through it `set_schedule` / `remove_schedule` / the Discord wizard) write the guild-level list.
An old file is lifted in memory when it is loaded and committed once at start.

**Tech stack:** Python 3.12, Pydantic (`GuildConfig`), the git store (`store.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-22-raids-events-schedules-design.md` — §5 (storage), §9 phase 1, C16, C19.
Read `CLAUDE.md` first. The code map this plan was written from is summarised in "Where things are" below.

## Global constraints

- No surface changes: no string, command, route, MCP tool, page or Discord card differs after this phase.
  `frontend/` is not touched, so the committed bundle is not rebuilt.
- A schedule's identity stays (first lineup raid, id). `Registry.schedules(instance)`, `schedule_def(instance, sid)`,
  `set_schedule(instance, sid, …)`, `remove_schedule(instance, sid, …)` keep their signatures and return shapes.
- Series keys stay `"<raid>:<schedule id>"`. No member file is rewritten.
- Timing still falls back schedule → raid → profile. No timing key moves in this phase.
- A lineup holds 1 or 2 entries in this phase (`LINEUP_MAX = 2`); the first entry never has `starts`.
- A refused write leaves `config.schedules` and `config.raids` exactly as they were.
- `config_rev("raids:<rid>")` still changes when one of that raid's schedules changes, and only then.
- Never commit real guild data to this repo; tests build their own config.
- Before the branch is offered for merge: `uv run python scripts/check_commands.py` (about 20 minutes; run it in the
  background writing a log with `exit=$?` appended, never piped through `tail` before `&&`).
- Commits are made on the work branch. Merging, pushing and restarting the bot happen only on the owner's word.
- Every subagent runs on model `opus`.

## Review focus

1. A `guild.yaml` from before the move: its schedules and run times are readable the moment the registry loads,
   before anything is saved. (Task 3 `test_an_old_file_reads_at_once`.)
2. A refused schedule edit after an accepted one in the same call: both stores are restored. (Task 2
   `test_a_refused_write_restores_both_stores`.)
3. Two raids that each have a schedule with the same id: neither overwrites the other. (Task 2
   `test_two_raids_may_share_a_schedule_id`.)
4. The `default` schedule with settings of its own and no run times, and its removal. (Task 2
   `test_default_keeps_its_own_settings_without_run_times`, `test_removing_default_clears_its_run_times`.)
5. A page holding an old `raids:<rid>` rev saving a schedule edit: still refused with `ConfigChanged`. (Task 2
   `test_a_stale_raid_rev_still_refuses_a_schedule_edit`.)

---

## Where things are (all in `src/oibot_gm/registry.py` unless marked)

- `GuildConfig.raids: dict[str, dict]` :463 — no top-level `schedules` today.
- `norm_schedule(row)` :1059, `schedules_of(rd)` :1088 (reads `rd["schedules"]`, `rd["slots"]`),
  `schedule_def_of(rd, sid)` :1106, `parse_follow` :951.
- `Registry._section_blob` :2047 (`own` set :2049), `config_rev` :2082.
- Registry load: the `GuildConfig(**yaml.safe_load(...))` just above `save_config` (:2040), and `reload` :2128.
- `raid_def` :4244. `set_schedule` :4332 (reads `rd["schedules"]` :4347). `remove_schedule` :4424 (:4437–4440).
- `set_raid_override` :4459 and `set_raid_overrides` :4475 — both snapshot `config.raids[instance]` to roll back.
- `_apply_raid_override`: `slots` and `schedules` branches :4640–4659. `clear_raid_override` :5083.
- `_check_raid` :4583 → `_check_schedules` :4598.
- `flows_config.py` `RaidConfigWizard.save` :1216 writes `"schedules"` through `set_raid_overrides`.
- Direct storage reads to re-point: `configops.py:4413` (raid_reset describe from `config.raids[rid]`),
  `tests/test_schedules.py:71,198,213`, `tests/test_parity_d1.py:774`, `tests/test_structured_ops.py:384-401`.
- Start hook pattern: `discord_bot.py:503` (`migrate_recipes` per guild, one ops line).

## The stored shape

```yaml
schedules:
  - id: tuesday                 # unique among schedules with the same first raid
    lineup:
      - {raid: hyjal_summit_forever, rosters: 1}
      - {raid: barrow_deeps, rosters: 2}      # optional: starts: 180  (minutes after the event starts)
    kind: weekly
    slots: ['Tue 18:30']
    # …any other key norm_schedule keeps today (name, active, days, time, weights, the cadence keys)
  - id: default                 # a raid's own run times
    lineup: [{raid: barrow_deeps, rosters: 1}]
    slots: ['Tue 04:00']
```

The old row keys `rosters` and `follow` are not stored: they are `lineup[0].rosters` and `lineup[1]`.

---

### Task 1: The lineup and the two shape converters

**Files:**
- Modify: `src/oibot_gm/registry.py` (next to `parse_follow` :951 and `norm_schedule` :1059)
- Test: `tests/test_guild_schedules.py` (new)

**Interfaces:**
- Produces:
  - `LINEUP_MAX = 2`
  - `parse_lineup(value) -> list[dict]` — each entry `{"raid": str, "rosters": int}` plus `"starts": int` when given.
    Raises `RegistryError` (one sentence) for: not a non-empty list; more than `LINEUP_MAX` entries; an entry
    without a raid; rosters outside 1..`MAX_SCHEDULE_ROSTERS`; `starts` on the first entry; `starts` not a whole
    number of minutes above 0; the same raid twice.
  - `to_stored_schedule(raid: str, row: dict) -> dict` — `row` is a `norm_schedule` result. Returns the stored entry:
    `id` first, then `lineup`, then every other key of `row` except `rosters` and `follow`.
  - `from_stored_schedule(entry: dict) -> tuple[str, dict]` — the exact inverse: `(first raid, row)`, where `row`
    has `rosters` only when it is not 1 and `follow` (`{"raid", "rosters"}`) only when the lineup has a second entry.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_guild_schedules.py -q` → errors on the
  missing names.
- [ ] **Step 3: Implement the three functions and the constant.** `starts` is validated and round-tripped; nothing
  reads it in this phase.
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Commit** — `git add tests/test_guild_schedules.py src/oibot_gm/registry.py` · message
  "Schedules: the lineup and the stored shape's two converters."

### Task 2: Read and write the guild-level list

**Files:**
- Modify: `src/oibot_gm/registry.py` — `GuildConfig`, `_section_blob`, `raid_def`, `set_schedule`,
  `remove_schedule`, `set_raid_override`, `set_raid_overrides`, `_apply_raid_override`, `clear_raid_override`
- Modify: `src/oibot_gm/configops.py:4413` (the raid_reset describe)
- Modify: `tests/test_schedules.py:71,198,213`, `tests/test_parity_d1.py:774`, `tests/test_structured_ops.py:384-401`
- Test: `tests/test_guild_schedules.py`

**Interfaces:**
- Consumes: Task 1's `to_stored_schedule`, `from_stored_schedule`.
- Produces:
  - `GuildConfig.schedules: list[dict]` (default empty).
  - `Registry.raid_rows(instance: str) -> list[dict]` — that raid's rows in today's shape, in stored order. The
    `default` row carries `slots` here.
  - `Registry._set_raid_rows(instance: str, rows: list[dict]) -> None` — replaces that raid's entries in
    `config.schedules`. Other raids' entries keep their order; the raid's block stays where its first entry was,
    or goes at the end. No save.
  - `raid_def(instance)` returns `"slots"` = the `default` row's run times and `"schedules"` = the other rows, plus
    the `default` row without `slots` when it has any other key besides `id` — exactly what the stored raid
    override held before.

**Rules the implementer must keep:**
- After any write, `config.raids[instance]` has no `schedules` or `slots` key. An override left empty is removed.
- `_apply_raid_override(field="slots")` sets the `default` row's run times; an empty list with no other key on the
  row removes the row.
- `_apply_raid_override(field="schedules")` keeps today's normalisation, then stores through `_set_raid_rows`,
  keeping the `default` row's current run times unless the incoming `default` row names its own.
- `set_schedule` and `remove_schedule` read `raid_def(instance)["schedules"]` as they do today; they need no
  change beyond what `raid_def` now supplies. Confirm by test, not by assumption.
- `set_raid_override` and `set_raid_overrides` snapshot and restore `config.schedules` together with
  `config.raids[instance]`.
- `clear_raid_override(instance)` removes that raid's schedules too.
- `_section_blob("raids:<rid>")` = the raid's override plus `{"schedules": [that raid's stored entries]}`;
  `"schedules"` joins the `own` set so the `settings` rev ignores it.

- [ ] **Step 1: Write the failing tests** (append; `reg` and `RID` as in `tests/test_schedules.py`, whose fixtures
  this file imports the same way)

```python
OTHER = "barrow_deeps"  # use any second raid of the test profile that is not RID


def stored(reg, raid):
    return [e for e in reg.config.schedules if e["lineup"][0]["raid"] == raid]


def test_a_schedule_is_stored_at_guild_level(reg):
    reg.set_schedule(RID, "tue", {"kind": "weekly", "slots": "Tue 19:30", "rosters": 2, "follow": f"{OTHER} x2"}, "t")
    assert stored(reg, RID) == [{"id": "tue", "lineup": [{"raid": RID, "rosters": 2}, {"raid": OTHER, "rosters": 2}], "kind": "weekly", "slots": ["Tue 19:30"]}]
    assert not {"schedules", "slots"} & set(reg.config.raids.get(RID) or {})
    row = next(s for s in reg.schedules(RID) if s["id"] == "tue")
    assert row["rosters"] == 2 and row["follow"] == {"raid": OTHER, "rosters": 2}


def test_run_times_are_the_default_schedule(reg):
    reg.set_raid_override(RID, "slots", "Tue 19:30", "t")
    assert stored(reg, RID) == [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": ["Tue 19:30"]}]
    assert reg.raid_def(RID)["slots"] == ["Tue 19:30"] and reg.raid_def(RID).get("schedules") in (None, [])
    assert [s["id"] for s in reg.schedules(RID)] == ["default"]


def test_default_keeps_its_own_settings_without_run_times(reg):
    reg.set_schedule(RID, "default", {"slots": "Tue 19:30", "rosters": 2}, "t")
    reg.set_raid_override(RID, "slots", "", "t")
    assert stored(reg, RID) == [{"id": "default", "lineup": [{"raid": RID, "rosters": 2}]}]
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
```

  If `clear_raid_override` or `set_schedule(rev=)` take other arguments than shown, match the real signatures and
  keep the assertions.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** the interfaces and rules above.
- [ ] **Step 4: Re-point the direct storage reads.** `configops.py:4413` describes a raid reset from what the raid
  holds: include its schedules through `raid_rows`. Update the three existing tests' storage assertions to the
  guild-level list (`test_structured_ops.py:384-401` must compare `config.schedules` between the registry and its
  shadow as well as `config.raids`).
- [ ] **Step 5: Run the schedule, follow-on, regulars and ops tests** —
  `uv run pytest tests/test_guild_schedules.py tests/test_schedules.py tests/test_follow_on.py tests/test_follow_on_solver.py tests/test_roster_p1_regulars.py tests/test_structured_ops.py tests/test_parity_d1.py tests/test_phase3_setup.py tests/test_dual_p0.py tests/test_rosters_page.py tests/test_public_schedule.py -q`
  → all pass.
- [ ] **Step 6: Commit** — message "Schedules live in guild.yaml's own list; a raid's override no longer holds them."

### Task 3: Lift an old file

**Files:**
- Modify: `src/oibot_gm/registry.py` (the registry's config load and `reload`), `src/oibot_gm/discord_bot.py`
  (beside the `migrate_recipes` start hook, :503)
- Test: `tests/test_guild_schedules.py`

**Interfaces:**
- Consumes: Task 1's `to_stored_schedule`; Task 2's storage.
- Produces:
  - `lift_schedules(cfg: GuildConfig) -> list[str]` — moves every `raids.<rid>.schedules` and `raids.<rid>.slots`
    into `cfg.schedules` (raids in their stored order; within a raid `default` first, then the stored order),
    removes both keys from the raid override, drops an override left empty, and returns one line per raid moved
    ("Hyjal Summit: 1 schedule"). Returns `[]` and changes nothing when there is nothing to move.
  - The registry calls it every time it builds its `GuildConfig` from the file, and keeps the lines in
    `Registry.schedules_lifted: list[str]`.
  - `Registry.migrate_schedules(by: str = "migration") -> list[str]` — when `schedules_lifted` is non-empty: one
    `save_config("schedules moved to the guild level (by …)", notify=False)`, clears the list, returns the lines.
    Otherwise writes nothing and returns `[]`. A shadow registry never writes.
  - At start, per guild, after `migrate_recipes`: call it in a thread; when it returns lines, one ops line
    "Schedules are now stored at guild level: <lines joined by ' · '>."

- [ ] **Step 1: Write the failing tests**

```python
OLD = {
    "raids": {
        "hyjal_summit_forever": {"first_open": "2026-12-15T15:00:00-08:00",
                                 "schedules": [{"id": "tuesday", "kind": "weekly", "slots": ["Tue 18:30"], "follow": {"raid": "barrow_deeps", "rosters": 2}}]},
        "barrow_deeps": {"confirm_hours_before": 12, "slots": ["Tue 04:00"],
                         "schedules": [{"id": "default", "rosters": 2}]},
        "onyxias_lair": {"slots": ["Wed 20:00"]},
    },
}


def test_lifting_moves_every_raid_and_is_idempotent():
    cfg = R.GuildConfig(**{**base_config(), **OLD})   # base_config(): the minimal GuildConfig kwargs the test suite's fixtures use
    lines = R.lift_schedules(cfg)
    assert len(lines) == 3
    assert cfg.schedules == [
        {"id": "tuesday", "lineup": [{"raid": "hyjal_summit_forever", "rosters": 1}, {"raid": "barrow_deeps", "rosters": 2}], "kind": "weekly", "slots": ["Tue 18:30"]},
        {"id": "default", "lineup": [{"raid": "barrow_deeps", "rosters": 2}], "slots": ["Tue 04:00"]},
        {"id": "default", "lineup": [{"raid": "onyxias_lair", "rosters": 1}], "slots": ["Wed 20:00"]},
    ]
    assert cfg.raids == {"hyjal_summit_forever": {"first_open": "2026-12-15T15:00:00-08:00"}, "barrow_deeps": {"confirm_hours_before": 12}}
    assert R.lift_schedules(cfg) == [] and len(cfg.schedules) == 3


def test_an_old_file_reads_at_once(old_store):          # old_store: a scratch GitStore whose guild.yaml holds OLD
    reg = open_registry(old_store)                       # the suite's usual way to open a Registry on a store
    assert [s["id"] for s in reg.schedules("hyjal_summit_forever")] == ["tuesday"]
    assert reg.raid_def("barrow_deeps")["slots"] == ["Tue 04:00"]
    assert reg.schedules_lifted


def test_the_lift_is_committed_once(old_store):
    reg = open_registry(old_store)
    n0 = commit_count(old_store)
    assert len(reg.migrate_schedules()) == 3 and commit_count(old_store) == n0 + 1
    assert reg.migrate_schedules() == [] and commit_count(old_store) == n0 + 1
    assert open_registry(old_store).schedules_lifted == []


def test_a_preview_copy_never_commits_the_lift(old_store):
    reg = open_registry(old_store)
    n0 = commit_count(old_store)
    reg.shadow().migrate_schedules()
    assert commit_count(old_store) == n0
```

  Build `base_config`, `old_store`, `open_registry` and `commit_count` from what `tests/conftest.py` already
  offers for a scratch store; do not add a new way to construct a registry.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement** `lift_schedules`, the load-time call, `schedules_lifted`, `migrate_schedules` and the
  start hook.
- [ ] **Step 4: Run them and see them pass,** then the Task 2 list again.
- [ ] **Step 5: Commit** — message "Schedules: an older guild file is lifted when it is read and saved once at start."

### Task 4: Write it down and prove nothing moved

**Files:**
- Modify: `docs/design.md` (new §5.67 "Raids, events and schedules", phase 1 only; a pointer from §5.27 and §5.62),
  `CLAUDE.md` (the `registry.py` entry: where schedules are stored, `lift_schedules`, `raid_rows`)
- No change: `docs/manual.md` (nothing a person does or sees changed), `frontend/`

- [ ] **Step 1: Write §5.67** — what moved, the stored shape, that reads are rebuilt in the old shape, the lift,
  what later phases change. Keep it to what is built.
- [ ] **Step 2: Update `CLAUDE.md`.**
- [ ] **Step 3: Run the full check** in the background —
  `uv run python scripts/check_commands.py > <log> 2>&1; echo "exit=$?" >> <log>` — and do not edit the tree while
  it runs. Expected: `exit=0`.
- [ ] **Step 4: Rehearse the lift on a copy of the real data.** Copy `../oibot_gm-data` to a scratch directory, open
  each guild with `GitStore(scratch, push=False)`, call `migrate_schedules()`, and compare, per guild and raid,
  `reg.schedules(rid)` and `rc.upcoming_rows(reg, rs, rid)` on the copy against the same calls made by the code on
  `main` against an untouched copy. They must be equal. Report the lines each guild's lift printed. Write nothing to
  the real data repo.
- [ ] **Step 5: Commit** — message "Design §5.67 and the agent guide: where schedules are stored."

## Deploying (the owner's call, not a task)

The first start on this code writes one commit per guild to the private data repo. Code from before this phase
cannot read the lifted file, so going back means reverting that data commit as well as the code. Note the data
repo's head before the restart.
