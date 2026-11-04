# Follow-on Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One signup covers a 20-man raid and the 10-man groups that follow it the same night: the lead run's lock builds the follow-on's rosters from the lead's roster, leaving early is a callout on the follow-on alone, and the owner's plain-text sentence ("set up an ad hoc hyjal raid … with 10 man barrow deeps splits afterwards") sets it up.

**Architecture:** A follow-on is a second, ordinary `RaidEvent` linked to its lead by one field each way (`follows` / `followed_by`). It has no sheet and no answers of its own: its signups are materialised from the lead's on every save of the lead (`rc.sync_follow`), minus the members who are leaving after the lead (`leaving`). The lead's lock locks both (the follow-on's solve prefers the lead's rostered players by a large seat bonus). Config carries `follow: {raid, rosters}` on a schedule or on an ad hoc open. Plain text and the site set it; Discord shows it.

**Tech Stack:** Python 3.12 / Pydantic / CP-SAT (existing solver), FastAPI routes, React + Mantine (Vite), pytest.

**Spec:** `docs/superpowers/specs/2026-11-04-follow-on-runs-design.md` (approved 2026-11-04). The spec is the authority; this plan argues from it.

## Global Constraints

- **The words:** *follow-on* (the second raid) and *lead* (the first), everywhere a person reads (spec §2). Never "legs", "chained", "child", "linked run", "phase".
- **Copy rule / Confirm rule** (CLAUDE.md): every confirm sentence ≤ 3 sentences, each ≤ 20 words, capital, full stop, no parentheses / semicolons / colon chaining / dashes. `tests/test_confirm_readability.py` lints every generator; `tests/test_copy.py` lints user-visible strings.
- **Clock rule:** nothing a person reads is 24-hour; the follow-on's start is `reg.local12(...)` on the server, `<t:unix:t>` in Discord.
- **Iconography rule:** a role shortfall is role icon + number, never "2 tank".
- **D1:** one Join for the night; the follow-on never answers directly (`set_signup` refuses it).
- **D2:** plain text and the site SET a follow-on; the Discord `/raid open` and schedule wizards SHOW one and don't set one (documented in design §5.62 and the manual — the parity matrix is keyed by op, not by field, so no `PARITY_PENDING` row).
- **D3:** a linked run, not legs inside one run.
- A follow-on never confirms (`confirm: False` on its run dict); its kind is the lead's; it has no series.
- `FOLLOW_SCHEDULE = "after"` is a reserved schedule id (like `ADHOC_SCHEDULE`); `norm_schedule` refuses it.
- Never a DM anywhere in this feature (the DM rule).
- Secrets only in `.env`; never commit real guild data or member names; always commit + push after each task's gate; commit messages end with the session's attribution lines (see the system reminder).
- Before the final restart: `uv run python scripts/check_commands.py` and `uv run python scripts/check_bundle.py` (real exit codes — never pipe through `tail` before `&&`). After touching `frontend/`: `cd frontend && npm run build` and commit the bundle.
- The manual (`docs/manual.md`) and `docs/design.md` §5.62 are written in the task that changes the behaviour (Task 9 collects them; Task 5's words must match them).
- Test fixtures: raid ids are `hyjal_summit_forever` (20, duration 3 h, tank min 2 / healer min 5), `barrow_deeps` (10, duration 2.5 h). Hyjal's profile `first_open` is 2026-12-09: every test that opens a Hyjal run first sets `reg.set_raid_overrides("hyjal_summit_forever", {"first_open": "2026-01-01T00:00"}, "t")`. `reg.seed_test_members(24, "t")` gives enough tanks and healers for a 20 + two 10s.

## Deviations from the spec (ruled while planning)

- Spec §4.3 says the Me page shows the night as ONE row. The plan keeps one row per run: the lead's row says "then Barrow Deeps ×2", the follow-on's says "after Hyjal Summit" and carries Leaving / I'm staying. Simpler, and each row keeps its own receipt.
- Spec §8.3 names `PARITY_PENDING` rows for the Discord wizards. The parity matrix is keyed by op and surface, and `run_open` / `schedule_set` have Discord paths, so a row would fail the matrix's own rule. The D2 gap is written in design §5.62 and the manual instead.
- Spec §7's reopen line was corrected in the spec itself: a cancelled run never reopens (D19); each run ends and reopens on its own.

## Review Focus

1. **A lead cancelled while its follow-on is mid-lock.** Expected: the follow-on ends cancelled too and nothing posts for it afterwards. Pinned in Task 4 (`test_cancel_lead_cancels_follow_on`) and the lock test in Task 3 checks `state` after lock.
2. **The lead's save loop.** `RaidStore.save(lead)` syncs the follow-on, whose save must not sync anything back. Pinned in Task 2 (`test_sync_runs_once_and_never_back`).
3. **A follow-on of a weekly schedule opening twice** (the scheduler's minute tick re-opening an existing pair). Expected: idempotent. Pinned in Task 2 (`test_open_pair_is_idempotent`).
4. **A member who answered No thanks on the lead.** Expected: out on the follow-on too, and leaving early never resurrects them. Pinned in Task 2 (`test_answers_materialise`) and Task 4 (`test_leave_then_stay_keeps_the_lead_answer`).
5. **A follow-on whose raid has larger minimums than the joiners cover** (two 10s need 4 tanks; 3 joined). Expected: one roster, the lock says short, nothing crashes. Pinned in Task 3 (`test_follow_on_caps_rosters_by_roles`).

---

## File structure

| File | Responsibility in this feature |
|---|---|
| `src/oibot_gm/registry.py` | `follow` on a schedule: `SCHEDULE_FIELDS`, `parse_follow`, `schedule_value`, `schedules_of`, `_check_schedules`, `schedule_label`, `Registry.follow_words`, `FOLLOW_SCHEDULE` |
| `src/oibot_gm/raidcycle.py` | the run model (`follows`, `followed_by`, `leaving`), `follow_start`, `ensure_run(follow=)` building both run dicts, `open_run(follow=)`, `open_event(follows)`, `sync_follow`, `leave_follow`, `stay_for_follow`, `lead_of` / `follow_of` / `is_follow`, `set_signup` refusal, `conflicts` exemption, `_solve_run`'s lead bonus, `callout` on a follow-on, `cancel_effects` pair, `open_preview` / `upcoming_rows` words, `follow_short` |
| `src/oibot_gm/discord_raid.py` | `open_run_and_post(follow=)`, `lock_run` locks the pair (`lock_follow`), the follow-on-alone lock refusal, `call_out` / `set_answer(out)` on a lead frees the follow-on seat (`after_lead_out`), `cancel_run` cascade, `stay_for_follow` verb, `cleanup_ephemeral` pair |
| `src/oibot_gm/raid_scheduler.py` | a follow-on whose lead is open is skipped |
| `src/oibot_gm/raid_views.py` | the sheet's "then …" title and line, the health card's follow-on shortfall line, the lock card's "after …" |
| `src/oibot_gm/consequences.py` | `_cancel` pair sentences, `leave_follow_consequence` |
| `src/oibot_gm/configops.py` | `run_open` text = the follow-on, `schedule_set … follow`, schema text, describe, apply |
| `src/oibot_gm/mcp_server.py` | `open_run(follow=)`, `schedule_set` docstring |
| `src/oibot_gm/web/api.py` | open + preview routes take `follow`; `ev_json` / `schedule_json` / `upcoming` / `/api/me` rows carry it |
| `src/oibot_gm/web/api_me.py` | `cant_consequence` on a follow-on, `sheet_row_extras` leave / stay, `POST /api/me/run/{key}/stay` |
| `src/oibot_gm/web/app.py` | the public schedule's label already carries "then …" (via `schedule_label`) |
| `src/oibot_gm/help.py` | `run_lines` head says then / after |
| `src/oibot_gm/flows_raid.py` | the open wizard's confirm shows a schedule's follow-on |
| `frontend/src/api.ts`, `components/ScheduleCard.tsx`, `pages/Rosters.tsx`, `pages/Raids.tsx`, `components/board/SheetCard.tsx`, `pages/Me.tsx` | the Then row, the Then field on the ad hoc Open, the cards' facts, the Me row's Leaving / I'm staying |
| `tests/test_follow_on.py` | config, open, sync, lock (fast), leave, cancel, scheduler, consequences, plain text, routes |
| `tests/test_follow_on_solver.py` | the slow solver test: two 10s are the 20 split |
| `docs/manual.md`, `docs/design.md`, `CLAUDE.md` | the words, §5.62, the module lines |

---

### Task 1: `follow` on a schedule (config)

**Files:**
- Modify: `src/oibot_gm/registry.py` (near `SCHEDULE_FIELDS` ~line 765, `schedule_value` ~851, `norm_schedule` ~930, `schedules_of` ~957, `_check_schedules` ~3517, `schedule_label` ~1540)
- Test: `tests/test_follow_on.py` (create)

**Interfaces:**
- Produces: `registry.FOLLOW_SCHEDULE = "after"`; `registry.parse_follow(value) -> dict | None` (`{"raid": str, "rosters": int}`); `schedule_value("follow", v)`; every schedule from `schedules_of` / `Registry.schedules` carries `follow` (None or the dict); `Registry.follow_words(follow) -> str` ("Barrow Deeps ×2", "" for None); `Registry.schedule_label` ends "· then Barrow Deeps ×2" when set.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_follow_on.py
"""Follow-on runs (design §5.62): a second raid the same night from one signup. Config, the pair of runs, the lead's
answers on the follow-on, the lock, leaving early, cancel, the words, plain text and the routes."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join

from oibot_gm import raidcycle as rc
from oibot_gm import registry as rg
from oibot_gm.registry import RegistryError

LEAD, FOLLOW = "hyjal_summit_forever", "barrow_deeps"


def unlock_hyjal(reg) -> None:
    reg.set_raid_overrides(LEAD, {"first_open": "2026-01-01T00:00"}, "t")


# ---------------------------------------------------------------- Task 1: config

def test_parse_follow_grammar():
    assert rg.parse_follow("barrow_deeps x2") == {"raid": "barrow_deeps", "rosters": 2}
    assert rg.parse_follow("Barrow_Deeps ×2") == {"raid": "barrow_deeps", "rosters": 2}
    assert rg.parse_follow("barrow_deeps") == {"raid": "barrow_deeps", "rosters": 1}
    assert rg.parse_follow({"raid": "barrow_deeps", "rosters": "3"}) == {"raid": "barrow_deeps", "rosters": 3}
    assert rg.parse_follow(None) is None and rg.parse_follow("") is None and rg.parse_follow("none") is None
    with pytest.raises(RegistryError, match="rosters"):
        rg.parse_follow("barrow_deeps x0")
    with pytest.raises(RegistryError, match="rosters"):
        rg.parse_follow("barrow_deeps x5")
    with pytest.raises(RegistryError, match="whole number"):
        rg.parse_follow("barrow_deeps xtwo")


def test_schedule_follow_is_validated_and_read_back(reg):
    unlock_hyjal(reg)
    reg.set_schedule(LEAD, "tue", {"slots": "Tue 18:30", "name": "Tuesday", "follow": "barrow_deeps x2"}, "t")
    s = next(x for x in reg.schedules(LEAD) if x["id"] == "tue")
    assert s["follow"] == {"raid": "barrow_deeps", "rosters": 2}
    assert reg.schedule_label(LEAD, s).endswith("· then Barrow Deeps ×2")
    assert reg.follow_words(s["follow"]) == "Barrow Deeps ×2" and reg.follow_words(None) == ""
    assert next(x for x in reg.schedules(FOLLOW) if x["id"] == "default" or True)["follow"] is None  # every schedule carries the key
    with pytest.raises(RegistryError, match="unknown raid"):
        reg.set_schedule(LEAD, "tue", {"follow": "nope x2"}, "t")
    with pytest.raises(RegistryError, match="itself"):
        reg.set_schedule(LEAD, "tue", {"follow": "hyjal_summit_forever x2"}, "t")
    reg.set_schedule(LEAD, "tue", {"follow": "none"}, "t")
    assert next(x for x in reg.schedules(LEAD) if x["id"] == "tue")["follow"] is None
    with pytest.raises(RegistryError, match="kept for follow-on"):
        reg.set_schedule(LEAD, "after", {"slots": "Tue 18:30"}, "t")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "parse_follow or schedule_follow"`
Expected: FAIL — `AttributeError: module 'oibot_gm.registry' has no attribute 'parse_follow'`.

- [ ] **Step 3: Implement**

In `src/oibot_gm/registry.py`:

```python
SCHEDULE_FIELDS = ("name", "kind", "active", "slots", "days", "time", "rosters", "weights", "follow") + SCHEDULE_CADENCE
# A follow-on run (design §5.62) — a second raid the same night from the same signup — keys its run with this reserved
# schedule id ('bd-1209-2130-after'), so it never collides with a run of that raid's own schedules at the same minute.
FOLLOW_SCHEDULE = "after"
FOLLOW_WORDS = {"none", "clear", "off", "no", ""}


def parse_follow(value) -> dict | None:
    """A schedule's follow-on: {raid, rosters} from a map or the text '<raid id> x<N>' ('barrow_deeps x2'; a bare id =
    one roster); None / '' / 'none' / 'clear' = no follow-on. The raid id is checked against the profile by
    Registry._check_schedules (this is a free function, like schedule_value)."""
    if value is None:
        return None
    if isinstance(value, dict):
        rid, n = str(value.get("raid") or "").strip().lower(), value.get("rosters", 1)
        if not rid:
            return None
    else:
        text = str(value).strip().lower().replace("×", "x")
        if text in FOLLOW_WORDS:
            return None
        rid, _sep, rest = text.partition(" ")
        rest = rest.strip()
        n = rest.removeprefix("x").strip() if rest else 1
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise RegistryError('a follow-on\'s rosters is a whole number, e.g. "barrow_deeps x2"')
    if not 1 <= n <= MAX_SCHEDULE_ROSTERS:
        raise RegistryError(f"a follow-on's rosters is 1 to {MAX_SCHEDULE_ROSTERS}")
    return {"raid": rid, "rosters": n}
```

In `schedule_value`, before the final `raise`:

```python
    if field == "follow":
        return parse_follow(value)
```

In `norm_schedule`, after the `"test"` refusal:

```python
    if sid == FOLLOW_SCHEDULE:
        raise RegistryError(f"schedule id {FOLLOW_SCHEDULE!r} is kept for follow-on runs; pick another id")
```

In `schedules_of`: `base = {"active": True, "rosters": 1, "slots": [], "days": [], "time": None, "follow": None}`.

In `Registry._check_schedules`, inside the per-schedule loop (after the rosters check):

```python
            f = s.get("follow")
            if f:
                if f["raid"] not in self.profile.raids:
                    raise RegistryError(f"{name}: unknown raid {f['raid']} for the follow-on (one of {', '.join(self.profile.raids)})")
                if f["raid"] == eff.get("id"):
                    raise RegistryError(f"{name}: a follow-on is another raid, not the raid itself")
```

In `Registry` (next to `schedule_label`):

```python
    def follow_words(self, follow: dict | None) -> str:
        """'Barrow Deeps ×2' — a follow-on as a person reads it ('' when none)."""
        if not follow:
            return ""
        name = self.raid_def(follow["raid"]).get("name", follow["raid"])
        n = int(follow.get("rosters") or 1)
        return f"{name} ×{n}" if n > 1 else name
```

And `schedule_label`'s return becomes:

```python
        return (text + (f" · {n} rosters" if n > 1 else "") + (f" · then {self.follow_words(s.get('follow'))}" if s.get("follow") else "")
                + ("" if s.get("active", True) else " · paused"))
```

Check `set_schedule`'s stored-entry rule: `norm_schedule` stores `follow` as the dict (a dict is "set"); `nv is None` pops it. Nothing else changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py -q -k "parse_follow or schedule_follow" && uv run pytest tests/test_schedules.py tests/test_raids_page.py -q`
Expected: PASS (the second command: the existing schedule tests still pass; if a test asserts the exact key set of a schedule, add `follow`).

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/registry.py tests/test_follow_on.py
git commit -m "Follow-on runs: a schedule can name a second raid the same night (follow: raid × rosters), validated and read back in words (spec 2026-11-04, task 1)"
```

---

### Task 2: The pair of runs — open, sync, refusals

**Files:**
- Modify: `src/oibot_gm/raidcycle.py` (`RaidEvent` ~152, `run_key` ~335, `ensure_run` ~513, `open_run` ~578, `RaidStore.save` ~787, `open_event` ~821, `set_signup` ~998, `runs_in_span` ~1048, `conflicts` ~1089)
- Test: `tests/test_follow_on.py`

**Interfaces:**
- Consumes: `registry.FOLLOW_SCHEDULE`, `registry.parse_follow`.
- Produces: `rc.FOLLOW_SCHEDULE`, `rc.LEAVING_NOTE`, `rc.FOLLOW_ANSWER_REFUSAL`; `RaidEvent.follows / followed_by / leaving`; `rc.follow_start(reg, instance, start) -> datetime`; `rc.ensure_run(..., follow: dict | None = None)` (the lead dict gains `follow` and `followed_by`; the follow-on dict exists with `follows`); `rc.open_run(..., follow=None) -> RaidEvent` (the lead; its follow-on opened too); `rc.open_event(reg, rs, team, start)` honours `team["follows"]`; `rc.is_follow(ev)`, `rc.lead_of(rs, ev)`, `rc.follow_of(rs, ev)`, `rc.lead_dict(reg, ev)`, `rc.follow_dict(reg, ev)`; `rc.sync_follow(rs, lead) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_follow_on.py`:

```python
# ---------------------------------------------------------------- Task 2: the pair of runs

def open_pair(reg, rs, days: float = 3):
    unlock_hyjal(reg)
    start = (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": FOLLOW, "rosters": 2})
    return lead, rc.follow_of(rs, lead)


def test_open_pair_opens_two_runs_with_one_signup(reg, rs):
    lead, fev = open_pair(reg, rs)
    assert fev is not None and fev.follows == lead.key and lead.followed_by == fev.key
    assert fev.instance == FOLLOW and "-after-" in fev.key  # 'bd-MMDD-HHMM-after-YYYY-MM-DD'
    assert fev.start == lead.start + timedelta(hours=3)  # Hyjal lasts 3 h
    t, ft = reg.config.roster(lead.team), reg.config.roster(fev.team)
    assert t["follow"] == {"raid": FOLLOW, "rosters": 2} and t["followed_by"] == ft["key"] and ft["follows"] == t["key"]
    assert ft["rosters"] == 2 and ft["size"] == 10 and ft["confirm"] is False and ft["kind"] == t["kind"] and ft["series"] is None
    assert ft["cutoff_hard_hours"] == t["cutoff_hard_hours"] + 3  # the same instant as the lead's lock
    assert fev.message_id is None and fev.signups == {}  # no sheet, no answers of its own yet
    assert rc.run_rosters(reg, fev) == 2 and rc.run_confirms(reg, fev) is False


def test_open_pair_is_idempotent(reg, rs):
    lead, fev = open_pair(reg, rs)
    again = rc.open_run(reg, rs, LEAD, lead.start, by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": FOLLOW, "rosters": 2})
    assert again is lead and rc.follow_of(rs, again) is fev
    assert sum(1 for t in reg.config.rosters if t.get("follows") or t.get("followed_by")) == 2


def test_a_schedule_with_a_follow_on_opens_the_pair_on_its_own(reg, rs):
    unlock_hyjal(reg)
    reg.set_schedule(LEAD, "tue", {"slots": "Tue 18:30", "follow": "barrow_deeps x2"}, "t")
    now = reg.now_local()
    t = next(t for s, t in rc.upcoming(reg, LEAD, now, 24 * 21) if s["id"] == "tue" and t - now >= timedelta(hours=30))
    lead = rc.open_run(reg, rs, LEAD, t, by="scheduler", schedule="tue")  # no explicit follow: the schedule's
    fev = rc.follow_of(rs, lead)
    assert fev is not None and fev.instance == FOLLOW and rc.run_rosters(reg, fev) == 2


def test_answers_materialise_on_the_follow_on(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms[:3])
    join(reg, rs, lead, ms[3:4], "sub")
    join(reg, rs, lead, ms[4:5], "out")
    fev = rs.events[fev.key]
    assert {k: v.status for k, v in fev.signups.items()} == {str(m.discord_id): st for m, st in zip(ms[:5], ["in"] * 3 + ["sub", "out"])}
    assert fev.signups[str(ms[0].discord_id)].character == lead.signups[str(ms[0].discord_id)].character
    rc.set_signup(reg, rs, lead, ms[0], None, "out", source="test")
    assert rs.events[fev.key].signups[str(ms[0].discord_id)].status == "out"
    with pytest.raises(RegistryError, match="Hyjal Summit sheet"):
        rc.set_signup(reg, rs, fev, ms[1], None, "sub", source="test")


def test_sync_runs_once_and_never_back(reg, rs, monkeypatch):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    saves = []
    orig = rc.RaidStore.save

    def spy(self, ev, message):
        saves.append((ev.key, message))
        return orig(self, ev, message)

    monkeypatch.setattr(rc.RaidStore, "save", spy)
    join(reg, rs, lead, reg.test_members()[:1])
    assert [k for k, _m in saves] == [lead.key, fev.key]  # the lead, then its follow-on once — nothing back
    saves.clear()
    join(reg, rs, lead, reg.test_members()[:1])  # the same answer again: the follow-on is unchanged, no save
    assert [k for k, _m in saves] == [lead.key]


def test_conflicts_exempt_the_pair_and_absences_skip_the_follow_on(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms[:2])
    assert rc.conflicts(rs, fev) == {} and rc.conflicts(rs, lead) == {}
    day = lead.start.astimezone(reg.tz).date().isoformat()
    assert [ev.key for ev in rc.runs_in_span(reg, rs, day, day)] == [lead.key]  # an absence touches the lead; the follow-on follows it
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "pair or materialise or sync_runs or conflicts_exempt"`
Expected: FAIL — `TypeError: open_run() got an unexpected keyword argument 'follow'`.

- [ ] **Step 3: Implement**

In `src/oibot_gm/raidcycle.py`:

Imports: add `FOLLOW_SCHEDULE` to the names imported from `.registry` (next to `ADHOC_SCHEDULE`), and near the constants:

```python
LEAVING_NOTE = "leaving after the first raid"  # the follow-on's out row for someone who stays for the lead only (design §5.62)
FOLLOW_ANSWER_REFUSAL = "Answer on the {lead} sheet. It covers the whole night."
LEAD_SEAT_BONUS = 1000  # a follow-on's solve: the lead's rostered beat every seat weight combined, so the splits are the lead's roster split
```

`RaidEvent` gains, after `prev_state`:

```python
    # a follow-on (design §5.62): a second raid the same night from the same signup. `follows` = the lead run's key (on the
    # follow-on), `followed_by` = the follow-on's key (on the lead). The follow-on's signups are materialised from the
    # lead's (rc.sync_follow) and never answered directly; `leaving` = the discord ids out of the follow-on alone
    follows: Optional[str] = None
    followed_by: Optional[str] = None
    leaving: list[int] = Field(default_factory=list)
```

Helpers (after `run_close_at`):

```python
def follow_start(reg: Registry, instance: str, start: datetime) -> datetime:
    """When a follow-on starts: the lead's start plus the lead raid's length (design §5.62)."""
    return start + timedelta(hours=float(reg.raid_def(instance).get("duration_hours") or RAID_DEFAULTS["duration_hours"]))


def is_follow(ev: "RaidEvent") -> bool:
    return bool(ev.follows)


def lead_of(rs: "RaidStore", ev: "RaidEvent") -> "RaidEvent | None":
    """The lead run of a follow-on (its answers are the lead's), else None."""
    return rs.events.get(ev.follows) if ev.follows else None


def follow_of(rs: "RaidStore", ev: "RaidEvent") -> "RaidEvent | None":
    """The follow-on run of a lead, else None."""
    return rs.events.get(ev.followed_by) if ev.followed_by else None


def lead_dict(reg: Registry, ev: "RaidEvent") -> dict | None:
    """The lead's run dict for a follow-on (no store needed: the sheet, the cards and the confirms read it)."""
    return reg.config.team(ev.follows) if ev.follows else None


def follow_dict(reg: Registry, ev: "RaidEvent") -> dict | None:
    return reg.config.team(ev.followed_by) if ev.followed_by else None
```

`ensure_run` gains `follow: dict | None = None` (last parameter). Before `reg.config.rosters.append(t)`:

```python
    fw = follow if follow is not None else (sched.get("follow") or None)
    if fw and not cutoffs:  # a follow-on (design §5.62): the second run's dict, keyed in the reserved 'after' space
        if fw["raid"] not in reg.profile.raids or fw["raid"] == instance:
            raise RegistryError(f"a follow-on is another raid of the profile (one of {', '.join(reg.profile.raids)})")
        dur = float(rd.get("duration_hours") or RAID_DEFAULTS["duration_hours"])
        fstart = follow_start(reg, instance, start)
        fkey = run_key(fw["raid"], fstart, FOLLOW_SCHEDULE)
        frd = reg.raid_def(fw["raid"])
        ft = {**t, "key": fkey, "instance": fw["raid"], "size": int(frd.get("size") or DEFAULT_RAID_SIZE),
              "name": f"{frd.get('name', fw['raid'])} · after {rd.get('name', instance)} {reg.local12(start)}",
              "schedule": fstart.strftime("%a %H:%M"), "schedule_id": FOLLOW_SCHEDULE, "series": None, "confirm": False,
              "rosters": int(fw.get("rosters") or 1), "follows": key, "reminders": OFF, "open_dm": False,
              # its lock, nudge and confirm fall at the same instants as the lead's (its start is `dur` later)
              "cutoff_soft_hours": t["cutoff_soft_hours"] + dur, "cutoff_hard_hours": t["cutoff_hard_hours"] + dur, "confirm_hours": t["confirm_hours"] + dur,
              "fill_ask_hours": float(rd["fill_ask_hours"]), "split_policy": rd.get("split_policy") or "balanced"}
        ft.pop("follow", None)
        ft.pop("followed_by", None)
        if reg.config.roster(fkey) is None:
            reg.config.rosters.append(ft)
        t["follow"] = {"raid": fw["raid"], "rosters": int(fw.get("rosters") or 1)}
        t["followed_by"] = fkey
```

`open_run` gains `follow: dict | None = None` and becomes:

```python
def open_run(reg: Registry, rs: "RaidStore", instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None,
             schedule: str | None = None, follow: dict | None = None) -> "RaidEvent":
    """Open (or find the live) run for `instance` at `start`. RegistryError when that time's run is closed
    (closed_run_refusal) — checked before ensure_run, so a refused open re-creates no roster entry. `follow`
    ({raid, rosters}, or the schedule's own) opens the follow-on run with it (design §5.62): no sheet, its answers the lead's."""
    why = closed_run_refusal(rs, event_key(instance, start, schedule, test=bool(cutoffs)))
    if why:
        raise RegistryError(why)
    t = ensure_run(reg, instance, start, by, cutoffs, schedule, follow=follow)
    ev = open_event(reg, rs, t, start)
    if t.get("followed_by") and not ev.followed_by:
        ft = reg.config.roster(t["followed_by"]) or {}
        fstart = follow_start(reg, instance, start)
        why = closed_run_refusal(rs, f"{ft['key']}-{fstart.date().isoformat()}")
        if why:
            raise RegistryError(why)
        fev = open_event(reg, rs, ft, fstart)
        ev.followed_by = fev.key
        ev.log.append(f"then {reg.raid_def(fev.instance).get('name', fev.instance)} ×{int(ft.get('rosters') or 1)} at {reg.local12(fstart)} from this signup")
        rs.save(ev, "follow-on opened")  # the save syncs the follow-on's answers
    return ev
```

`open_event`: the `RaidEvent(...)` construction gains `follows=team.get("follows")`, and `prefill` is skipped for a follow-on:

```python
    if not ev.follows:  # a follow-on's answers come from its lead (sync_follow), absences included
        prefill(reg, ev, team)
    ev.log.append(f"opened ({run_kind(reg, ev)}); {len(ev.signups)} marked away from absences" if not ev.follows else "opened as a follow-on: its answers are the lead's")
```

`RaidStore.save`, after the listeners loop:

```python
        if ev.followed_by:  # a lead: its follow-on's answers follow (never the other way round)
            sync_follow(self, ev)
```

`sync_follow` (after `prefill`):

```python
def sync_follow(rs: "RaidStore", lead: RaidEvent) -> bool:
    """The follow-on's signups ARE the lead's (design §5.62): copied from the lead every time the lead is saved, the
    members leaving after the lead as out (source callout, LEAVING_NOTE). Pins, the board and the rosters are the
    follow-on's own. Saves the follow-on only when something changed; returns whether it did."""
    fev = follow_of(rs, lead)
    if fev is None or fev.state in ("done", "cancelled"):
        return False
    want: dict[str, Signup] = {}
    for uid, s in lead.signups.items():
        c = s.model_copy()
        if s.discord_id in fev.leaving and c.status != "out":
            c.status, c.source, c.note, c.joined_at = "out", "callout", LEAVING_NOTE, None
        want[uid] = c
    if {k: v.model_dump() for k, v in want.items()} == {k: v.model_dump() for k, v in fev.signups.items()}:
        return False
    fev.signups = want
    rs.save(fev, "answers follow the lead")
    return True
```

`set_signup`, first lines:

```python
    if ev.follows:  # D1: one Join for the night — the follow-on is never answered on its own
        lead = rs.events.get(ev.follows)
        raise RegistryError(FOLLOW_ANSWER_REFUSAL.format(lead=reg.raid_def(lead.instance if lead else None).get("name", "first raid")))
```

`runs_in_span`: `return [ev for ev in rs.live() if not ev.follows and start <= ev.start.astimezone(reg.tz).date().isoformat() <= end]` (an absence marks the lead; the follow-on follows it; the follow-on's SEAT is freed by Task 4's `after_lead_out`).

`conflicts`: after `if other.key == ev.key: continue` add `if other.key in (ev.follows, ev.followed_by): continue  # a run and its follow-on share their people (design §5.62)`.

`run_key` is unchanged: `FOLLOW_SCHEDULE` is just another schedule id suffix.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py -q && uv run pytest tests/test_raidcycle.py tests/test_schedules.py tests/test_raid_night.py -q -x`
Expected: PASS. (If `tests/test_raidcycle.py` does not exist, run `uv run pytest tests -q -k "raidcycle or schedule" -x`.)

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/raidcycle.py tests/test_follow_on.py
git commit -m "Follow-on runs: opening a lead opens its follow-on (one signup, the follow-on's answers follow the lead's on every save, never answered on its own; the pair is exempt from the same-night conflict check) (spec 2026-11-04, task 2)"
```

---

### Task 3: The lock builds both

**Files:**
- Modify: `src/oibot_gm/raidcycle.py` (`_solve_run` ~2252), `src/oibot_gm/discord_raid.py` (`lock_run` ~820, `open_run_and_post` ~1254), `src/oibot_gm/raid_scheduler.py` (~168)
- Test: `tests/test_follow_on.py`, `tests/test_follow_on_solver.py` (create)

**Interfaces:**
- Consumes: Task 2's helpers.
- Produces: `RaidMixin.open_run_and_post(reg, rs, instance, start, by, schedule=None, follow=None)`; `RaidMixin.lock_follow(reg, rs, lead, by) -> str | None`; `lock_run` on a follow-on whose lead is open raises `RegistryError("Lock <lead title> — its lock builds the follow-on too")`; `rc.LEAD_SEAT_BONUS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_follow_on.py`:

```python
# ---------------------------------------------------------------- Task 3: the lock builds both

from test_raid_night import NightBot, fast_propose, run  # noqa: E402


def seat_by_name(names_by_key: dict[str, list[str]]):
    """rc.propose without the solver, per run key prefix: the lead's names and the follow-on's names."""
    def propose(reg, rs, ev, save=True):
        names = names_by_key[ev.instance]
        size = rc.run_size(reg, ev)
        g = int(reg.profile.comp_rules["group_size"])
        layout = [names[i:i + g] for i in range(0, len(names), g)]
        ev.rosters = rc.board_rosters(reg, ev, layout, size)
        return [], ev.rosters[0]
    return propose


def test_the_leads_lock_locks_the_follow_on_too(reg, rs, monkeypatch):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    names = [m.display_name for m in ms]
    monkeypatch.setattr(rc, "propose", seat_by_name({LEAD: names[:20], FOLLOW: names[:20]}))
    line = run(bot.lock_run(reg, rs, lead, by="Jon"))
    fev = rs.events[fev.key]
    assert lead.state == "locked" and fev.state == "locked" and fev.key in line
    assert len(fev.all_rosters) == 2 and sorted(p.signup_name for r in fev.all_rosters for p in r.selected) == sorted(names[:20])
    assert fev.confirm_by is None and not bot.dms  # a follow-on never confirms (its lead is ad hoc here: no DMs either)
    assert "lock:0" in fev.cards or bot.officer_channel(reg, fev) is None  # cards post when there is a channel


def test_a_follow_on_cannot_lock_before_its_lead(reg, rs):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    with pytest.raises(RegistryError, match="its lock builds the follow-on too"):
        run(bot.lock_run(reg, rs, fev, by="Jon"))
    assert fev.state == "open" and lead.state == "open"


def test_the_scheduler_skips_a_follow_on_while_its_lead_is_open(reg, rs, monkeypatch):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs, days=0.5)  # inside the ad hoc lock window would be days < 2/24; keep both open
    locked = []
    monkeypatch.setattr(bot, "lock_run", lambda reg, rs, ev, by="scheduler": locked.append(ev.key) or "ok")
    monkeypatch.setattr(bot, "post_health", lambda *a, **k: None)
    run(bot.scheduler_tick_guild(reg))
    assert fev.key not in locked and not fev.health_posted
```

Create `tests/test_follow_on_solver.py`:

```python
"""The follow-on's solve (slow: CP-SAT): the two 10s are the lead's 20 split in two whenever the roles allow, bench
joiners cover the rest; the roles cap the rosters."""
import pytest
from conftest import by_role, join
from test_follow_on import FOLLOW, LEAD, open_pair

from oibot_gm import raidcycle as rc

pytestmark = pytest.mark.slow


def test_the_splits_are_the_leads_roster(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    rc.propose(reg, rs, lead)
    lead.state = "locked"
    rs.save(lead, "locked (test)")
    on_lead = {p.signup_name for r in lead.all_rosters for p in r.selected}
    assert len(on_lead) == 20
    fev = rs.events[fev.key]
    rc.propose(reg, rs, fev)
    on_follow = {p.signup_name for r in fev.all_rosters for p in r.selected}
    assert len(fev.all_rosters) == 2 and on_follow == on_lead, "two 10s = the 20, bench joiners only cover gaps"


def test_follow_on_caps_rosters_by_roles(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    tanks = by_role(reg)["tank"]
    join(reg, rs, lead, [m for m in ms if m not in tanks[3:]])  # three tanks: two 10s need four
    rc.propose(reg, rs, lead)
    lead.state = "locked"
    rs.save(lead, "locked (test)")
    fev = rs.events[fev.key]
    rc.propose(reg, rs, fev)
    assert len(fev.all_rosters) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "lock or scheduler_skips"`
Expected: FAIL — the follow-on stays `open` after the lead's lock; no refusal on the follow-on's lock.

- [ ] **Step 3: Implement**

`raidcycle._solve_run`: after `bonus = seat_bonus(reg, rs, ev, players)`:

```python
    lead = lead_of(rs, ev)
    if lead is not None and lead.all_rosters:  # a follow-on: the lead's rostered come first — the splits are the lead's roster split (design §5.62)
        on_lead = {player_uid(lead, p) for p in lead.seated()}
        bonus = {**bonus, **{p.signup_name: int(bonus.get(p.signup_name, 0)) + LEAD_SEAT_BONUS for p in players if player_uid(ev, p) in on_lead}}
```

and the first-come benching applies to the lead only: `if run_kind(reg, ev) == "adhoc" and not is_follow(ev):`.

`discord_raid.RaidMixin.open_run_and_post` gains `follow: dict | None = None` and passes it: `ev = rc.open_run(reg, rs, instance, start, by=by, schedule=schedule, follow=follow)`; the ops line adds `+ (f" then {reg.raid_def(fev.instance).get('name', fev.instance)} {fev.key}" if (fev := rc.follow_of(rs, ev)) else "")`.

`RaidMixin.lock_run`: at the top, before `team = rc.run_team(reg, ev)`:

```python
        if rc.is_follow(ev) and (lead := rc.lead_of(rs, ev)) is not None and lead.state == "open":
            raise RegistryError(f"Lock {run_title(reg, lead)} — its lock builds the follow-on too")
```

and just before the final `return f"{ev.key}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)"`:

```python
        tail = await self.lock_follow(reg, rs, ev, by)
        return f"{ev.key}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)" + (f" · {tail}" if tail else "")
```

New method on `RaidMixin` (after `lock_run`):

```python
    async def lock_follow(self, reg, rs, ev, by: str) -> str | None:
        """The lead's lock builds the follow-on too (design §5.62): once the lead is locked, the follow-on solves from
        the night's answers with the lead's rostered preferred (rc.LEAD_SEAT_BONUS), flips to locked and posts its own
        lock cards; it never confirms. None when the run has no live open follow-on. A failed follow-on solve leaves
        it open with its lock_error, like any run — the scheduler retries it, the lead stands."""
        fev = rc.follow_of(rs, ev)
        if fev is None or fev.state != "open":
            return None
        return await self.lock_run(reg, rs, fev, by=by)
```

`raid_scheduler.scheduler_tick_guild`, first lines of the `for ev in rs.live():` loop:

```python
            if rc.is_follow(ev) and (lead := rc.lead_of(rs, ev)) is not None and lead.state == "open":
                continue  # a follow-on waits for its lead's lock (design §5.62): no health card, no lock of its own
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py -q && uv run pytest tests/test_follow_on_solver.py -q -m slow && uv run pytest tests/test_raid_night.py tests/test_split.py -q -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/raidcycle.py src/oibot_gm/discord_raid.py src/oibot_gm/raid_scheduler.py tests/test_follow_on.py tests/test_follow_on_solver.py
git commit -m "Follow-on runs: the lead's lock builds the follow-on too — its splits are the lead's roster split in two, bench joiners cover gaps, it never confirms and never locks before its lead (spec 2026-11-04, task 3)"
```

---

### Task 4: Leaving early, a lead's Can't, cancel, cleanup, the confirm sentences

**Files:**
- Modify: `src/oibot_gm/raidcycle.py` (`callout` ~1078, `cancel_effects` ~2793), `src/oibot_gm/discord_raid.py` (`set_answer` ~315, `cleanup_ephemeral` ~762, `call_out` ~1097, `cancel_run` ~1301), `src/oibot_gm/consequences.py` (`_cancel` ~115), `src/oibot_gm/web/api_me.py` (`cant_consequence` ~26)
- Test: `tests/test_follow_on.py`, `tests/test_confirm_readability.py`

**Interfaces:**
- Produces: `rc.leave_follow(reg, rs, ev, m, note=None) -> Callout`; `rc.stay_for_follow(reg, rs, ev, m) -> bool`; `rc.callout` on a follow-on = `leave_follow`; `RaidMixin.after_lead_out(reg, rs, lead, m, why, note)`; `RaidMixin.stay_for_follow(reg, rs, ev, m, by, by_id=None, via="discord") -> str`; `RaidMixin.cancel_run(..., audit=True)`; `consequences.leave_follow_consequence(reg, ev, seated: bool) -> list[str]`; `rc.cancel_effects(reg, ev)["follow"]` (the follow raid's name or None) and `["lead"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_follow_on.py`:

```python
# ---------------------------------------------------------------- Task 4: leaving early, cancel, the sentences

from oibot_gm import consequences as cq  # noqa: E402


def locked_pair(reg, rs, monkeypatch, bot):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    names = [m.display_name for m in ms]
    monkeypatch.setattr(rc, "propose", seat_by_name({LEAD: names[:20], FOLLOW: names[:20]}))
    run(bot.lock_run(reg, rs, lead, by="Jon"))
    return lead, rs.events[fev.key], ms


def test_leaving_early_frees_only_the_follow_on_seat(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m = ms[0]
    line = run(bot.call_out(reg, rs, fev, m, "early night", by=m.display_name, by_id=m.discord_id))
    fev = rs.events[fev.key]
    assert m.discord_id in fev.leaving and fev.signups[str(m.discord_id)].status == "out" and fev.signups[str(m.discord_id)].note == rc.LEAVING_NOTE
    assert fev.seat_of(m) is None and lead.seat_of(m) is not None and lead.signups[str(m.discord_id)].status == "in"
    assert "seat freed" in line and fev.callouts[-1].display_name == m.display_name


def test_leave_then_stay_keeps_the_lead_answer(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m, gone = ms[0], ms[4]
    rc.set_signup(reg, rs, lead, gone, None, "out", source="test")  # No thanks on the lead: out on both, for good
    run(bot.call_out(reg, rs, fev, m, None, by=m.display_name, by_id=m.discord_id))
    assert run(bot.stay_for_follow(reg, rs, rs.events[fev.key], m, by=m.display_name, by_id=m.discord_id)).startswith("Noted")
    fev = rs.events[fev.key]
    assert m.discord_id not in fev.leaving and fev.signups[str(m.discord_id)].status == "in" and fev.seat_of(m) is None  # back on the sheet, not re-seated
    assert fev.signups[str(gone.discord_id)].status == "out"
    assert rc.stay_for_follow(reg, rs, fev, gone) is False  # nothing to undo: they left the night, not the follow-on


def test_a_cant_on_the_lead_frees_both_seats(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m = ms[1]
    run(bot.call_out(reg, rs, lead, m, None, by=m.display_name, by_id=m.discord_id))
    fev = rs.events[fev.key]
    assert lead.seat_of(m) is None and fev.seat_of(m) is None and fev.signups[str(m.discord_id)].status == "out"
    m2 = ms[2]
    run(bot.set_answer(reg, rs, lead, m2, "out", None, by=m2.display_name, by_id=m2.discord_id))
    assert rs.events[fev.key].seat_of(m2) is None


def test_cancel_lead_cancels_follow_on(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    run(bot.cancel_run(reg, rs, lead, "Jon", "storm"))
    fev = rs.events[fev.key]
    assert lead.state == "cancelled" and fev.state == "cancelled"
    audits = [l for l in bot.ops.lines if "cancelled" in l]
    assert len(audits) == 1 and fev.key in audits[0] and lead.key in audits[0]


def test_cancel_follow_on_alone_leaves_the_lead_standing(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    run(bot.cancel_run(reg, rs, fev, "Jon", None))
    assert fev.state == "cancelled" and lead.state == "locked" and lead.followed_by is None
    assert reg.config.roster(lead.team).get("followed_by") is None
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(lead.team) is not None and reg.config.roster(fev.team) is None


def test_cleanup_drops_the_pair_together(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    run(bot.cancel_run(reg, rs, lead, "Jon", None))
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(lead.team) is None and reg.config.roster(fev.team) is None


def test_the_sentences(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    assert "Barrow Deeps follow-on is cancelled with it" in " ".join(cq.consequence("cancel", reg, lead))
    assert "Hyjal Summit stands" in " ".join(cq.consequence("cancel", reg, fev))
    assert cq.leave_follow_consequence(reg, fev, True) == ["You stay on the Hyjal Summit roster.", "Your Barrow Deeps place is freed.", "An officer may fill it."]
    assert cq.leave_follow_consequence(reg, fev, False) == ["You stay on the Hyjal Summit sheet.", "You are out of the Barrow Deeps follow-on."]
    fx = rc.cancel_effects(reg, lead)
    assert fx["follow"] == "Barrow Deeps" and rc.cancel_effects(reg, fev)["lead"] == "Hyjal Summit"
```

The ops fake: `bot.ops.lines` must exist on `FakeOps` (tests/test_mcp.py) — if its recorder is named differently (read the class), use that name in the assertion.

In `tests/test_confirm_readability.py`, inside `run_cases` after the cancelled case, add a lead + follow-on pair (both open and both locked) so every verb's confirm and the new generator run through the lint:

```python
    # a lead with a follow-on (design §5.62): the cancel pair sentences and leaving early
    reg.set_raid_overrides("hyjal_summit_forever", {"first_open": "2026-01-01T00:00"}, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": RID, "rosters": 2})
    join(reg, rs, lead, ms)
    cases["lead open"], cases["follow-on open"] = lead, rs.events[lead.followed_by]
    lock_with_board(reg, rs, lead, [m.display_name for m in ms[:10]], hours_to_confirm=2)
    fev = rs.events[lead.followed_by]
    lock_with_board(reg, rs, fev, [m.display_name for m in ms[:10]], hours_to_confirm=2)
    cases["lead locked"], cases["follow-on locked"] = lead, fev
    return cases
```

and in `test_every_confirm_reads_aloud`, after the `consequence(release, no name)` line:

```python
        if ev.follows:
            for seated in (True, False):
                dump.add("leave_follow_consequence", f"{case}, {'seated' if seated else 'not seated'}", cq.leave_follow_consequence(reg, ev, seated))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "leaving or stay or cant_on_the_lead or cancel or cleanup or sentences"`
Expected: FAIL — `AttributeError: 'NightBot' object has no attribute 'stay_for_follow'`, and the callout on the follow-on raises nothing useful.

- [ ] **Step 3: Implement**

`raidcycle.callout`, first lines:

```python
    if ev.follows:  # a follow-on: leaving after the lead (design §5.62) — the lead's answer stands
        return leave_follow(reg, rs, ev, m, note)
```

New functions after `callout`:

```python
def leave_follow(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, note: str | None = None) -> Callout:
    """Leaving after the lead: out of the follow-on alone (design §5.62). Records the callout on the follow-on, adds
    them to `leaving`, re-materialises their row as out; the caller frees a held seat (RaidMixin.call_out → drop_seated)."""
    s = ev.signups.get(str(m.discord_id))
    hours = (ev.start - datetime.now(ev.start.tzinfo)).total_seconds() / 3600
    co = Callout(discord_id=m.discord_id, display_name=m.display_name, character=s.character if s else (m.main.label if m.main else "?"),
                 at=now(), hours_before=round(hours, 1), note=note, late=ev.state != "open")
    ev.callouts.append(co)
    if m.discord_id not in ev.leaving:
        ev.leaving.append(m.discord_id)
    ev.log.append(f"{m.display_name} is {LEAVING_NOTE}")
    rs.save(ev, f"leaving: {m.display_name}")
    lead = lead_of(rs, ev)
    if lead is not None:
        sync_follow(rs, lead)
    return co


def stay_for_follow(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member) -> bool:
    """Undo leaving early: their lead answer stands on the follow-on again (not re-seated — fill may have taken the
    seat; the fill engine offers it when short). False when they weren't leaving (a No thanks on the lead is the night)."""
    if m.discord_id not in ev.leaving:
        return False
    ev.leaving = [u for u in ev.leaving if u != m.discord_id]
    ev.log.append(f"{m.display_name} is staying for the follow-on")
    rs.save(ev, f"staying: {m.display_name}")
    lead = lead_of(rs, ev)
    if lead is not None:
        sync_follow(rs, lead)
    return True
```

`cancel_effects` gains, in its dict: `"follow": (reg.raid_def((follow_dict(reg, ev) or {}).get("instance")).get("name") if ev.followed_by else None), "lead": (reg.raid_def((lead_dict(reg, ev) or {}).get("instance")).get("name") if ev.follows else None)`.

`consequences._cancel`: after `head` is built, replace the return with:

```python
    fx_follow, fx_lead = fx.get("follow"), fx.get("lead")
    if fx_follow:
        return [head, f"The {fx_follow} follow-on is cancelled with it, and nobody gets a DM.", "A cancelled run can't be reopened."]
    if fx_lead:
        return [head, f"{fx_lead} stands, and nobody gets a DM.", "A cancelled run can't be reopened."]
    return [head, "Nobody gets a DM, but the sheet and every ask card say Cancelled.", "A cancelled run can't be reopened."]
```

New generator in `consequences.py` (after `_cancel`):

```python
def leave_follow_consequence(reg: Registry, ev: rc.RaidEvent, seated: bool) -> list[str]:
    """Leaving after the lead (design §5.62): the Me page's and /raid out's confirm on a follow-on. Under the Confirm rule."""
    lead = (rc.lead_dict(reg, ev) or {}).get("instance")
    lead_name = reg.raid_def(lead).get("name", "the first raid")
    mine = reg.raid_def(ev.instance).get("name", ev.instance)
    if seated:
        return [f"You stay on the {lead_name} roster.", f"Your {mine} place is freed.", "An officer may fill it."]
    return [f"You stay on the {lead_name} sheet.", f"You are out of the {mine} follow-on."]
```

`api_me.cant_consequence`: first line `if ev.follows: from ..consequences import leave_follow_consequence; return " ".join(leave_follow_consequence(reg, ev, seated))` (import at the module top).

`discord_raid.RaidMixin`:

`call_out` — `rc.callout` already branches; after `co = rc.callout(...)` the rest works for a follow-on (drop_seated on `ev` frees the follow-on seat). Add, right after the `if sheet_state(ev) == "locked" and ev.seat_of(m): await self.drop_seated(...)` block and before the `await self.refresh_sheet(reg, ev)` of the not-seated path, the lead → follow-on ripple:

```python
        await self.after_lead_out(reg, rs, ev, m, "callout" if mine else "officer", note)
```

(call it in BOTH branches: before `return line + " — seat freed"` and before the `refresh_sheet` of the open path). Same call in `set_answer` when `status == "out"` (both the open-sheet branch and the locked branch), after the lead's own handling. New method:

```python
    async def after_lead_out(self, reg, rs, lead, m, why: str, note: str | None) -> None:
        """A No thanks or Can't on a lead is the whole night (design §5.62): the follow-on's row is already out (sync),
        and a seat they hold on the locked follow-on is freed the way the lead's is (drop_seated: thread line, fill)."""
        fev = rc.follow_of(rs, lead)
        if fev is None or sheet_state(fev) != "locked" or not fev.seat_of(m):
            return
        await self.drop_seated(reg, rs, fev, rc.run_team(reg, fev), m, why, note)
```

`stay_for_follow` verb:

```python
    async def stay_for_follow(self, reg, rs, ev, m, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """I'm staying: undo leaving early on a follow-on (rc.stay_for_follow). The site's Me row and plain text call it."""
        if not rc.stay_for_follow(reg, rs, ev, m):
            raise RegistryError(f"{m.display_name} isn't marked as leaving after the first raid")
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"↩ {m.display_name} is staying for {run_label(reg, ev)}")
        await ops_mod.audited(self.ops, reg, by, f"{m.display_name} is staying for {ev.key}", via=via, by_id=by_id)
        return f"Noted: you're staying for {run_label(reg, ev).replace('**', '')}. Your seat comes back when one is free."
```

`cancel_run` gains `audit: bool = True`; the `ops_mod.audited(...)` call becomes:

```python
        fev = rc.follow_of(rs, ev)
        tail = ""
        if fev is not None and fev.state not in ("done", "cancelled"):  # the lead's cancel takes its follow-on with it (design §5.62)
            await self.cancel_run(reg, rs, fev, by, reason, notify=False, by_id=by_id, via=via, audit=False)
            tail = f" and its follow-on {fev.key}"
        if ev.follows and (lead := rc.lead_of(rs, ev)) is not None and lead.followed_by == ev.key and audit:  # cancelled on its own: the lead stands alone
            lead.followed_by = None
            lead.log.append(f"the {reg.raid_def(ev.instance).get('name', ev.instance)} follow-on was cancelled; this run stands")
            rs.save(lead, "follow-on cancelled")
            t = reg.config.team(lead.team)
            if t is not None and t.get("followed_by"):
                t.pop("followed_by", None)
                reg.save_config(f"run {lead.team}: follow-on cancelled", notify=False)
            await self.refresh_sheet(reg, lead)
        if audit:
            await ops_mod.audited(self.ops, reg, by, f"cancelled {ev.key}{tail}" + (f": {reason}" if reason else ""), via=via, level="warn", by_id=by_id)
```

`cleanup_ephemeral`: `evs = [e for e in rs.events.values() if e.team in {t["key"], t.get("followed_by"), t.get("follows")} - {None}]` — a pair goes together.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py tests/test_confirm_readability.py -q && uv run pytest tests/test_raid_night.py tests/test_parity_verbs.py tests/test_member_inbox.py -q -x`
Expected: PASS. Read `out/confirm_sentences.txt` for the new blocks (`leave_follow_consequence`, `consequence(cancel)` on the pair) as a raid leader would.

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/raidcycle.py src/oibot_gm/discord_raid.py src/oibot_gm/consequences.py src/oibot_gm/web/api_me.py tests/test_follow_on.py tests/test_confirm_readability.py
git commit -m "Follow-on runs: leaving after the lead is a callout on the follow-on alone (I'm staying undoes it), a Can't on the lead frees both seats, cancelling the lead cancels its follow-on with one audit line, the pair's run dicts go together, the confirm sentences (spec 2026-11-04, task 4)"
```

---

### Task 5: The words on every surface (sheet, cards, previews, JSON, Me rows, help)

**Files:**
- Modify: `src/oibot_gm/raidcycle.py` (`open_preview` ~2885, `upcoming_rows` ~2918, new `follow_short`), `src/oibot_gm/raid_views.py` (`sheet_message` ~123, `health_layout` ~521, `lock_layout` ~635), `src/oibot_gm/web/api.py` (`schedule_json` ~830, `ev_json` ~927, `/api/me` ~630), `src/oibot_gm/web/api_me.py` (`sheet_row_extras` ~35), `src/oibot_gm/help.py` (`run_lines` ~422), `src/oibot_gm/flows_raid.py` (`confirm` ~303), `src/oibot_gm/discord_raid.py` (`lock_run`'s signup-channel line ~905)
- Test: `tests/test_follow_on.py`, `tests/test_rosters_page.py`

**Interfaces:**
- Produces: `rc.follow_short(reg, ev) -> dict[str, int]` (role → short for the follow-on's minimums × rosters against the lead's Join answers; `{}` when none); `rc.open_preview(reg, rs, instance, value, schedule=None, follow=None)` adds `follow` ({raid, name, rosters, when} or None) and `follow_line` ("then Barrow Deeps ×2 at 9:30 PM", or ""); `upcoming_rows` rows gain `follow` (words or ""); `schedule_json` gains `follow` and `follow_words`; `ev_json` base gains `follows`, `followed_by`, `follow` ({raid, rosters, when, key} or None), `lead` ({raid, key} or None), `leaving` (display names); `/api/me` sheets gain `then` (words or None), `lead_raid` (or None), `may_leave`, `may_stay`, `leave_consequence`; `api_me.sheet_row_extras` the same.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_follow_on.py`:

```python
# ---------------------------------------------------------------- Task 5: the words

from fastapi.testclient import TestClient  # noqa: E402
from test_mcp import TOKEN, FakeBot, bearer  # noqa: E402

from oibot_gm import raid_views  # noqa: E402
from oibot_gm.web import app as web  # noqa: E402


@pytest.fixture
def c(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FakeBot(reg, rs)))


def test_sheet_and_cards_say_then_and_after(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ico = lambda *a, **k: ""  # noqa: E731
    e, _view = raid_views.sheet_message(reg, lead, rc.run_team(reg, lead), ico)
    assert e.title.endswith("Hyjal Summit, then Barrow Deeps ×2") and "then Barrow Deeps ×2 <t:" in e.description
    join(reg, rs, lead, reg.test_members()[:5])
    assert rc.follow_short(reg, lead)  # five joiners can't cover two 10s' tanks and healers
    text = "\n".join(str(getattr(ch, "content", "")) for ch in raid_views.health_layout(reg, rs, lead, rc.run_team(reg, lead), ico).walk_children())
    assert "Barrow Deeps follow-on" in text
    from conftest import lock_with_board
    lock_with_board(reg, rs, rs.events[fev.key], [m.display_name for m in reg.test_members()[:10]])
    card = "\n".join(str(getattr(ch, "content", "")) for ch in raid_views.lock_layout(reg, rs.events[fev.key], rc.run_team(reg, fev), ico, 0, rs).walk_children())
    assert "Barrow Deeps · after Hyjal Summit" in card


def test_previews_and_json_carry_the_follow_on(reg, rs, c):
    unlock_hyjal(reg)
    reg.set_schedule(LEAD, "tue", {"slots": "Tue 18:30", "follow": "barrow_deeps x2"}, "t")
    p = rc.open_preview(reg, rs, LEAD, None, "tue")
    assert p["follow"]["name"] == "Barrow Deeps" and p["follow"]["rosters"] == 2 and p["follow_line"].startswith("then Barrow Deeps ×2 at ") and "PM" in p["follow_line"]
    p2 = rc.open_preview(reg, rs, LEAD, (reg.now_local() + timedelta(days=4)).strftime("%Y-%m-%d 18:30"), None, follow={"raid": FOLLOW, "rosters": 1})
    assert p2["follow"]["rosters"] == 1 and p2["follow_line"].startswith("then Barrow Deeps at ")
    rows = [u for u in rc.upcoming_rows(reg, rs, LEAD) if u["schedule"] == "tue"]
    assert rows and all(u["follow"] == "Barrow Deeps ×2" for u in rows)
    raids = c.get("/api/raids", headers=bearer()).json()["raids"]
    s = next(x for r in raids if r["id"] == LEAD for x in r["schedules"] if x["id"] == "tue")
    assert s["follow"] == {"raid": FOLLOW, "rosters": 2} and s["follow_words"] == "Barrow Deeps ×2"
    lead, fev = open_pair(reg, rs)
    reg.seed_test_members(4, "t")
    join(reg, rs, lead, reg.test_members()[:2])
    j = c.get(f"/api/run/{lead.key}", headers=bearer()).json()
    assert j["followed_by"] == fev.key and j["follow"]["raid"] == "Barrow Deeps" and j["follow"]["rosters"] == 2 and "PM" in j["follow"]["when"] and j["lead"] is None
    jf = c.get(f"/api/run/{fev.key}", headers=bearer()).json()
    assert jf["follows"] == lead.key and jf["lead"] == {"raid": "Hyjal Summit", "key": lead.key} and jf["follow"] is None and jf["leaving"] == []


def test_me_rows_offer_leaving_and_staying(reg, rs, c, monkeypatch):
    lead, fev = open_pair(reg, rs)
    reg.seed_test_members(4, "t")
    m = reg.test_members()[0]
    join(reg, rs, lead, [m])
    from itsdangerous import URLSafeSerializer
    import time
    from test_mcp import SECRET  # the session cookie's secret in the test app
    c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(m.discord_id), "name": m.display_name, "iat": time.time()}))
    me = c.get("/api/me").json()
    rows = {s["key"]: s for s in me["sheets"]}
    assert rows[lead.key]["then"] == "Barrow Deeps ×2" and rows[lead.key]["may_answer"] and rows[lead.key]["lead_raid"] is None
    f = rows[fev.key]
    assert f["lead_raid"] == "Hyjal Summit" and f["may_answer"] is False and f["may_leave"] is True and f["may_stay"] is False
    assert f["leave_consequence"] == "You stay on the Hyjal Summit sheet. You are out of the Barrow Deeps follow-on."
```

If `test_mcp` has no `SECRET`, read how `tests/test_dual_p0.py` builds the member cookie (it imports `SECRET` from the web module or sets `OIBOT_SECRET`) and do the same.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "say_then or previews_and_json or me_rows"`
Expected: FAIL — the title lacks ", then …"; `open_preview()` has no `follow` argument; `/api/me` rows have no `then`.

- [ ] **Step 3: Implement**

`raidcycle`:

```python
def follow_short(reg: Registry, ev: RaidEvent) -> dict[str, int]:
    """What the follow-on's tank and healer minimums × its rosters are short, counted on the lead's Join answers (the
    health card's one follow-on line; {} when none or covered)."""
    ft = follow_dict(reg, ev)
    if not ft:
        return {}
    size, n = int(ft.get("size") or DEFAULT_RAID_SIZE), int(ft.get("rosters") or 1)
    rb = reg.role_bounds(ft.get("instance"), size)
    ins = ev.by_status("in")
    return {r: int(rb[r]["min"]) * n - sum(1 for s in ins if s.role == r) for r in ("tank", "healer") if int(rb[r]["min"]) * n > sum(1 for s in ins if s.role == r)}


def follow_words_of(reg: Registry, ev: RaidEvent) -> str:
    """'Barrow Deeps ×2' for a lead's follow-on ('' when none)."""
    ft = follow_dict(reg, ev)
    return reg.follow_words({"raid": ft.get("instance"), "rosters": ft.get("rosters") or 1}) if ft else ""


def lead_name_of(reg: Registry, ev: RaidEvent) -> str:
    """'Hyjal Summit' for a follow-on's lead ('' when the run is not a follow-on)."""
    lt = lead_dict(reg, ev)
    return reg.raid_def(lt.get("instance")).get("name", lt.get("instance")) if lt else ""
```

`open_preview(reg, rs, instance, value, schedule=None, follow=None)`: after `w = which_run(...)`:

```python
    fw = follow if follow is not None else (reg.schedule_def(instance, w["schedule"])["schedule"].get("follow") if w["schedule"] != ADHOC_SCHEDULE else None)
    fstart = follow_start(reg, instance, start) if fw else None
    fjson = {"raid": fw["raid"], "name": reg.raid_def(fw["raid"]).get("name", fw["raid"]), "rosters": int(fw.get("rosters") or 1), "when": reg.local12(fstart)} if fw else None
```

and the returned dict gains `"follow": fjson, "follow_line": f"then {reg.follow_words(fw)} at {reg.local12(fstart)}" if fw else ""`.

`upcoming_rows`: each row gains `"follow": reg.follow_words(s.get("follow"))`.

`raid_views.sheet_message`: after `name = rd.get("name", ...)`:

```python
    then = rc.follow_words_of(reg, ev)  # a lead: "Hyjal Summit, then Barrow Deeps ×2" (design §5.62)
    if then:
        name = f"{name}, then {then}"
    after = rc.lead_name_of(reg, ev)
```

In the `state == "open"` description, after the first line, add `+ (f"\nthen {then} <t:{int(rc.follow_start(reg, ev.instance, ev.start).timestamp())}:t>" if then else "")` right after the `<t:{unix}:R>` part (one line under the stamp). Nothing for `after` on the sheet (a follow-on has none).

`health_layout`: after the `short` block:

```python
    fshort = rc.follow_short(reg, ev)
    if fshort:
        body.append(f"**{rc.follow_words_of(reg, ev).split(' ×')[0]} follow-on** short " + "   ".join(f"{ico('role', r)} {k}" for r, k in fshort.items()))
```

`lock_layout`: the header title becomes `f"🔒 {rd.get('name', ev.instance)}" + (f" · after {after}" if (after := rc.lead_name_of(reg, ev)) else "") + (f" · Roster {i + 1}" if len(ev.all_rosters) > 1 else "")`.

`discord_raid.lock_run`'s signup-channel line: `run_label(reg, ev)` is the lead's; the follow-on's own lock (via `lock_follow`) sends its own line — add `(" · after " + rc.lead_name_of(reg, ev) if rc.is_follow(ev) else "")` after `run_label(reg, ev)` in that `sig.send`.

`web/api.py`:
- `schedule_json`: add `"follow": s.get("follow"), "follow_words": reg.follow_words(s.get("follow"))`.
- `ev_json` base: add
  ```python
  "follows": ev.follows, "followed_by": ev.followed_by,
  "follow": ({"raid": reg.raid_def(ft["instance"]).get("name", ft["instance"]), "rosters": int(ft.get("rosters") or 1), "key": ev.followed_by,
              "when": t12(reg, rc.follow_start(reg, ev.instance, ev.start).isoformat())} if (ft := rc.follow_dict(reg, ev)) else None),
  "lead": ({"raid": rc.lead_name_of(reg, ev), "key": ev.follows} if ev.follows else None),
  "leaving": [reg.members[u].display_name for u in ev.leaving if u in reg.members],
  ```
- `/api/me` sheets: the row's `"raid"` stays the raid's name; add `"then": rc.follow_words_of(reg, ev) or None, "lead_raid": rc.lead_name_of(reg, ev) or None`.

`api_me.sheet_row_extras`: for a follow-on, `may_answer` is False; add

```python
    leaving = bool(m and ev.follows and m.discord_id in ev.leaving)
    on_night = bool(m and ev.follows and s is not None and s.status == "in") or seated
    return {..., "may_answer": (ev.state == "open" and rc.on_bench_side(reg, ev, m) and not ev.follows) if m else False,
            "may_cant": (seated or (locked and s is not None and s.status == "in")) and not ev.follows,
            "may_leave": bool(ev.follows and ev.state in ("open", "locked") and on_night and not leaving),
            "may_stay": leaving and ev.state in ("open", "locked"),
            "leave_consequence": " ".join(leave_follow_consequence(reg, ev, seated)) if ev.follows else None, ...}
```

`help.run_lines`: the `head` gains `+ (f" · then {then}" if (then := rc.follow_words_of(reg, ev)) else "") + (f" · after {after} (a follow-on: its answers are the lead's)" if (after := rc.lead_name_of(reg, ev)) else "")`.

`flows_raid.OpenWizard.confirm` (the `/raid open` wizard): after the rosters line, add

```python
        fw = self.reg.schedule_def(self.rid, sid)["schedule"].get("follow") if sid and not self.is_adhoc(sid) else None
        if fw:
            lines.append(f"-# Then {self.reg.follow_words(fw)} at {self.reg.local12(rc.follow_start(self.reg, self.rid, start))}, from the same signup.")
```

(D2: the wizard shows a schedule's follow-on; it sets none.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py tests/test_rosters_page.py tests/test_help.py tests/test_copy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/raidcycle.py src/oibot_gm/raid_views.py src/oibot_gm/web/api.py src/oibot_gm/web/api_me.py src/oibot_gm/help.py src/oibot_gm/flows_raid.py src/oibot_gm/discord_raid.py tests/test_follow_on.py
git commit -m "Follow-on runs in words: the sheet reads 'Hyjal Summit, then Barrow Deeps ×2' with the second start, the health card says what the follow-on is short, its lock cards say 'after Hyjal Summit', the previews, the run and schedule JSON and the Me rows carry it, help and the open wizard say it (spec 2026-11-04, task 5)"
```

---

### Task 6: Plain text and MCP

**Files:**
- Modify: `src/oibot_gm/configops.py` (SCHEMA_TEXT ~127-146, `_schedule_fields` ~817, `_schedule_words` ~839, `_describe_action` run_open ~2309, apply run_open ~3133), `src/oibot_gm/mcp_server.py` (`open_run` ~460, `schedule_set` ~663)
- Test: `tests/test_follow_on.py`

**Interfaces:**
- Consumes: `registry.parse_follow`, `RaidMixin.open_run_and_post(follow=)`.
- Produces: `run_open` op with `text="barrow_deeps x2"`; `schedule_set` with `field="<sid>.follow"`; `configops.follow_of_op(reg, rid, text) -> dict | None` (parse + profile check + not the raid itself).

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------- Task 6: plain text

from oibot_gm import configops  # noqa: E402
from oibot_gm.configops import ConfigOp  # noqa: E402


def test_the_owners_sentence_as_ops(reg, rs):
    """set up an ad hoc hyjal raid for 6:30 pst on Dec 10th with 10 man barrow deeps splits afterwards. also set up a
    scheduled hyjal raid for 6:30 pst every tuesday beginning december 15 — as the parser's ops, described and applied."""
    unlock_hyjal(reg)
    day = (reg.now_local() + timedelta(days=6)).strftime("%Y-%m-%d")
    ops = [ConfigOp(op="run_open", target=LEAD, value=f"{day} 18:30", field="adhoc", text="barrow_deeps x2"),
           ConfigOp(op="schedule_set", target=LEAD, field="tue.slots", value="Tue 18:30"),
           ConfigOp(op="schedule_set", target=LEAD, field="tue.first_open", value="2026-12-15 18:30")]
    lines = [configops.describe(reg, o) for o in ops]
    assert ", then Barrow Deeps ×2 at " in lines[0] and "PM" in lines[0]
    assert "Hyjal Summit" in lines[1]
    assert "follow" in configops.SCHEMA_TEXT and "x<N>" in configops.SCHEMA_TEXT
    bad = ConfigOp(op="run_open", target=LEAD, value=f"{day} 18:30", field="adhoc", text="hyjal_summit_forever x2")
    assert "not the raid itself" in configops.describe(reg, bad)
    fw = ConfigOp(op="schedule_set", target=LEAD, field="tue.follow", value="barrow_deeps x2")
    assert "then Barrow Deeps ×2" in configops.describe(reg, fw)
    assert configops.apply(reg, fw, by="t")
    assert next(s for s in reg.schedules(LEAD) if s["id"] == "tue")["follow"] == {"raid": FOLLOW, "rosters": 2}
    off = ConfigOp(op="schedule_set", target=LEAD, field="tue.follow", value="none")
    assert "no follow-on" in configops.describe(reg, off)


def test_run_open_applies_with_the_follow_on(reg, rs, monkeypatch):
    unlock_hyjal(reg)
    bot = NightBot(reg, rs)
    day = (reg.now_local() + timedelta(days=6)).strftime("%Y-%m-%d")
    op = ConfigOp(op="run_open", target=LEAD, value=f"{day} 18:30", field="adhoc", text="barrow_deeps x2")
    line = run(configops.apply_async(reg, op, by="Jon", bot=bot))
    lead = next(e for e in rs.live() if e.instance == LEAD)
    assert lead.followed_by and rc.follow_of(rs, lead).instance == FOLLOW and lead.key in line
```

Check `configops.apply` / `apply_async` signatures in the module (they may take `(reg, op, by, ...)` or a list) and adjust the calls; the assertions stay.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "owners_sentence or run_open_applies"`
Expected: FAIL — the describe line has no ", then …"; `SCHEMA_TEXT` has no `follow`.

- [ ] **Step 3: Implement**

`configops.py`:

SCHEMA_TEXT, the schedule settings line: after `rosters (1-4: …)` add `| follow ("<raid id> x<N>": a second raid the same night from the same signup, starting when the first ends — "barrow_deeps x2" = two 10-man Barrow Deeps groups; "none" clears it)`. The `run_open` line: after `field=<optional schedule id: …>` add `, text=<optional follow-on "<raid id> x<N>" — a second raid the same night from the same signup, starting when the first ends ("10 man barrow deeps splits afterwards" = "barrow_deeps x2")>`. The `text` field description in `ConfigOp` gains `; run_open: the follow-on "<raid id> x<N>"`.

```python
def follow_of_op(reg: Registry, rid: str, text: str | None) -> dict | None:
    """run_open's follow-on from its `text` ('barrow_deeps x2'): parsed in code (registry.parse_follow), the raid checked."""
    from .registry import parse_follow

    fw = parse_follow(text)
    if fw is None:
        return None
    if fw["raid"] not in reg.profile.raids:
        raise RegistryError(f"unknown raid {fw['raid']} for the follow-on (one of {', '.join(reg.profile.raids)})")
    if fw["raid"] == rid:
        raise RegistryError("a follow-on is another raid, not the raid itself")
    return fw
```

`_schedule_fields`: `return {setting: _typed(value) if setting in ("weights", "follow") else value}`.

`_schedule_words`: add a branch before the final `else`:

```python
        elif f == "follow":
            fw = schedule_value(f, v)
            out.append(f"then {reg.follow_words(fw)}" if fw else "no follow-on")
```

`_describe_action`, `run_open`: after computing `start, sid`, add `fw = follow_of_op(reg, rid, op.text)` and `then = f", then {reg.follow_words(fw)} at {reg.local12(rc.follow_start(reg, rid, start))}" if fw else ""`; append `then` to both return strings (before ` now (` / after `already {have.state}`).

Apply (`run_open`, ~3133): `ev = await bot.open_run_and_post(reg, rs, rid, start, by, follow=follow_of_op(reg, rid, op.text), **rc.schedule_kw(sid))`.

`mcp_server.open_run(raid, when=None, schedule=None, follow: str | None = None, guild=None)`: docstring adds `follow` ("'barrow_deeps x2': a second raid the same night from the same signup, starting when the first ends"); the body posts `{"when": when or "", "schedule": schedule or "", "follow": follow or ""}`. `schedule_set`'s docstring lists `follow ('<raid id> x<N>' or null)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py tests/test_plain_text.py tests/test_parity_matrix.py tests/test_mcp.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/configops.py src/oibot_gm/mcp_server.py tests/test_follow_on.py
git commit -m "Follow-on runs in plain text and MCP: 'with 10 man barrow deeps splits afterwards' opens the pair (run_open's follow-on), a schedule's follow setting, both described in words (spec 2026-11-04, task 6)"
```

---

### Task 7: The site's routes

**Files:**
- Modify: `src/oibot_gm/web/api.py` (`open_args` ~1656, `raid_open_preview` ~1661, `raid_open` ~1681), `src/oibot_gm/web/api_me.py` (`install_me_api`: new route after `me_out` ~96)
- Test: `tests/test_follow_on.py`

**Interfaces:**
- Produces: `POST /api/raid/{rid}/open/preview` and `/open` accept `follow` (`{"raid", "rosters"}` or the text); `POST /api/me/run/{key}/stay` → `RaidMixin.stay_for_follow`; `POST /api/me/run/{key}/out` on a follow-on = leaving early (through `call_out`); `/api/me/run/{key}/answer` on a follow-on answers 400 with the refusal.

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------- Task 7: routes


class FollowBot(FakeBot):
    """FakeBot plus the verbs the open, out and stay routes call (the real RaidMixin's would need Discord)."""

    async def open_run_and_post(self, reg, rs, instance, start, by, schedule=None, follow=None):
        return rc.open_run(reg, rs, instance, start, by=by, schedule=schedule, follow=follow)

    async def call_out(self, reg, rs, ev, m, note, by, by_id=None):
        rc.callout(reg, rs, ev, m, rc.run_team(reg, ev), note)
        return "noted"

    async def stay_for_follow(self, reg, rs, ev, m, by, by_id=None, via="web"):
        if not rc.stay_for_follow(reg, rs, ev, m):
            raise RegistryError("not leaving")
        return "staying"


@pytest.fixture
def fc(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FollowBot(reg, rs)))


H = {"X-Requested-With": "oibot"}


def test_open_route_takes_a_follow_on(reg, rs, fc):
    unlock_hyjal(reg)
    when = (reg.now_local() + timedelta(days=5)).strftime("%Y-%m-%d 18:30")
    p = fc.post(f"/api/raid/{LEAD}/open/preview", json={"when": when, "follow": {"raid": FOLLOW, "rosters": 2}}, headers={**bearer(), **H}).json()
    assert p["follow"]["name"] == "Barrow Deeps" and p["follow_line"].startswith("then Barrow Deeps ×2")
    r = fc.post(f"/api/raid/{LEAD}/open", json={"start": p["start"], "key": p["key"], "schedule": "adhoc", "follow": "barrow_deeps x2"}, headers={**bearer(), **H})
    assert r.status_code == 200, r.text
    lead = rs.events[r.json()["key"]]
    assert rc.follow_of(rs, lead).instance == FOLLOW
    bad = fc.post(f"/api/raid/{LEAD}/open/preview", json={"when": when, "follow": "hyjal_summit_forever x2"}, headers={**bearer(), **H})
    assert bad.status_code == 400 and "not the raid itself" in bad.json()["error"]


def test_me_routes_leave_and_stay(reg, rs, fc):
    lead, fev = open_pair(reg, rs)
    reg.seed_test_members(4, "t")
    m = reg.test_members()[0]
    join(reg, rs, lead, [m])
    from itsdangerous import URLSafeSerializer
    import time
    from test_mcp import SECRET
    fc.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(m.discord_id), "name": m.display_name, "iat": time.time()}))
    r = fc.post(f"/api/me/run/{fev.key}/answer", json={"status": "sub"}, headers=H)
    assert r.status_code == 400 and "Hyjal Summit sheet" in r.json()["error"]
    assert fc.post(f"/api/me/run/{fev.key}/out", json={"note": ""}, headers=H).status_code == 200
    assert m.discord_id in rs.events[fev.key].leaving
    assert fc.post(f"/api/me/run/{fev.key}/stay", json={}, headers=H).status_code == 200
    assert m.discord_id not in rs.events[fev.key].leaving
    assert fc.post(f"/api/me/run/{fev.key}/stay", json={}, headers=H).status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_follow_on.py -q -k "open_route or me_routes"`
Expected: FAIL — the preview has no `follow`; `/stay` is 404.

- [ ] **Step 3: Implement**

`web/api.py`, next to `open_args`:

```python
    def follow_arg(reg, rid: str, d: dict) -> dict | None:
        """The open / preview body's `follow` ({raid, rosters} or 'barrow_deeps x2') → the follow-on, checked in code."""
        from ..configops import follow_of_op

        return follow_of_op(reg, rid, d.get("follow")) if d.get("follow") not in (None, "") else None
```

`raid_open_preview`: inside the `try`, `fw = follow_arg(v.reg, rid, d)` and `p = rc.open_preview(v.reg, bot.raids.store(v.reg), rid, when, sid, follow=fw)`.
`raid_open`: inside the first `try`, `fw = follow_arg(reg, rid, d)`; the open call becomes `bot.open_run_and_post(reg, rs, rid, start, v.name, follow=fw, **rc.schedule_kw(sid))`; the message adds `+ (f", then {reg.follow_words(fw)}" if fw else "")`.

`api_me.py`, after `me_out`:

```python
    @app.post("/api/me/run/{key}/stay")
    async def me_stay(request: Request, key: str):
        """I'm staying: undo leaving after the lead on a follow-on (design §5.62) — RaidMixin.stay_for_follow as myself."""
        v, _d = await body(request)
        m = me_of(v)
        rs, ev = run_of(v, key)
        try:
            line = await bot.stay_for_follow(v.reg, rs, ev, m, m.display_name, by_id=v.uid, via="web")
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return receipt(v, ev, m, line)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_follow_on.py tests/test_parity_routes.py tests/test_parity_matrix.py -q`
Expected: PASS (the parity matrix: `/api/me/run/{key}/stay` is a member route, not an op's surface).

- [ ] **Step 5: Commit**

```bash
git add src/oibot_gm/web/api.py src/oibot_gm/web/api_me.py tests/test_follow_on.py
git commit -m "Follow-on runs on the site's routes: Open and its preview take the follow-on, the Me page leaves after the lead and comes back with I'm staying, a follow-on answers 'answer on the lead's sheet' (spec 2026-11-04, task 7)"
```

---

### Task 8: The pages

**Files:**
- Modify: `frontend/src/api.ts` (`MySheet` ~122, `UpcomingRun` ~188, `RaidRuns.schedules` ~196, `Schedule` ~213), `frontend/src/components/board/types.ts` (`OpenPreview` ~45, `Sheet` ~1), `frontend/src/components/ScheduleCard.tsx` (`Draft` ~106, `toDraft`, `fieldsOf` ~160, the editor's fields ~228, `Schedules` props ~40), `frontend/src/pages/Raids.tsx` (~190: pass `others`), `frontend/src/pages/Rosters.tsx` (`StartAdhoc` ~94), `frontend/src/components/board/SheetCard.tsx` (`facts` ~84), `frontend/src/pages/Me.tsx` (`SheetRow` ~140)
- Test: `tests/test_frontend_source.py`
- Build: `cd frontend && npm run build`; commit `src/oibot_gm/web/static/app`

**Interfaces:**
- Consumes: Task 5's and Task 7's JSON.

- [ ] **Step 1: Write the failing source test**

Append to `tests/test_frontend_source.py`:

```python
def test_follow_on_surfaces_exist():
    """Design §5.62 (D2): the site sets a follow-on — the schedule editor's Then row and the ad hoc Open's Then field —
    and the Me row offers leaving early and I'm staying with the server's sentence only."""
    sched = (SRC / "components" / "ScheduleCard.tsx").read_text()
    assert "follow_raid" in sched and 'label="then, the same night"' in sched and "f.follow =" in sched
    rosters = (SRC / "pages" / "Rosters.tsx").read_text()
    assert "follow_line" in rosters and "follow:" in rosters[rosters.index("function StartAdhoc"):]
    me = (SRC / "pages" / "Me.tsx").read_text()
    assert "s.may_leave" in me and "s.may_stay" in me and "/stay`" in me and "s.leave_consequence" in me
    card = (SRC / "components" / "board" / "SheetCard.tsx").read_text()
    assert "e.follow" in card and "e.lead" in card
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_frontend_source.py -q -k follow_on`
Expected: FAIL.

- [ ] **Step 3: Implement**

`api.ts`:
- `MySheet` gains `then?: string | null; lead_raid?: string | null; may_leave?: boolean; may_stay?: boolean; leave_consequence?: string | null`.
- `UpcomingRun` gains `follow?: string`.
- `RaidRuns.schedules` entries gain `follow_words?: string`.
- `Schedule` gains `follow?: { raid: string; rosters: number } | null; follow_words?: string`.
- `components/board/types.ts`: `OpenPreview` gains `follow?: { raid: string; name: string; rosters: number; when: string } | null; follow_line?: string`; `Sheet` gains `follows?: string | null; followed_by?: string | null; follow?: { raid: string; rosters: number; key: string; when: string } | null; lead?: { raid: string; key: string } | null; leaving?: string[]`.

`ScheduleCard.tsx`:
- `Schedules` and `ScheduleEditor` take a new prop `others: { id: string; name: string }[]` (the raid's siblings). `pages/Raids.tsx` passes `others={data.raids.filter((x) => x.id !== r.id).map((x) => ({ id: x.id, name: x.name }))}` where it renders `<Schedules …>` (the card component gets `others` from the page; `data` is the page's `Raids` state).
- `Draft` gains `follow_raid: string; follow_rosters: string`; `toDraft`: `follow_raid: s?.follow?.raid || "", follow_rosters: String(s?.follow?.rosters || 1)`.
- `fieldsOf` → `all`: `f.follow = x.follow_raid ? { raid: x.follow_raid, rosters: Number(x.follow_rosters) } : null;`.
- The editor, after the `rosters per run` Select:

```tsx
          <Select label="then, the same night" description="a second raid from the same signup, starting when this one ends" w={260} allowDeselect={false}
            value={d.follow_raid} onChange={(v) => setD({ ...d, follow_raid: v || "" })}
            data={[{ value: "", label: "No follow-on" }, ...others.map((o) => ({ value: o.id, label: o.name }))]} />
          {d.follow_raid && <Select label="its rosters" description="how many groups of that raid" w={130} allowDeselect={false} value={d.follow_rosters}
            onChange={(v) => setD({ ...d, follow_rosters: v || "1" })} data={Array.from({ length: maxRosters }, (_x, i) => ({ value: String(i + 1), label: String(i + 1) }))} />}
```

The read view needs nothing: `s.label` already ends "· then Barrow Deeps ×2".

`pages/Rosters.tsx` `StartAdhoc`: state `const [follow, setFollow] = useState<string>("")` and `const [followN, setFollowN] = useState<string>("2")`; the preview effect sends `follow: follow ? { raid: follow, rosters: Number(followN) } : ""` (add `follow, followN` to its deps); `start()` sends the same `follow`; under the template Select add

```tsx
          <Select label="Then, the same night" description="a second raid from the same signup, starting when this one ends" allowDeselect={false} value={follow} onChange={(v) => setFollow(v || "")}
            data={[{ value: "", label: "No follow-on" }, ...others.map((o) => ({ value: o.id, label: o.name }))]} />
          {follow && <Select label="Its rosters" allowDeselect={false} value={followN} onChange={(v) => setFollowN(v || "1")} data={["1", "2", "3", "4"].map((n) => ({ value: n, label: n }))} w={120} />}
```

and in the preview block `{p.follow_line && <Text size="sm">{p.follow_line}, from the same signup.</Text>}`. `StartAdhoc` needs `others`: the Rosters page has every raid in its `Rosters.raids`; pass `others={raids.filter((x) => x.id !== r.id).map((x) => ({ id: x.id, name: x.name }))}` from the component that renders `<StartAdhoc r={r} …>` (it holds the page's `raids`). `close()` resets `follow` to "".

`SheetCard.tsx` `facts`: prepend `e.follow ? `then ${e.follow.raid} ×${e.follow.rosters} at ${e.follow.when}` : e.lead ? `after ${e.lead.raid}` : null` to the array; for a follow-on the `ad hoc` badge is fine. When `e.lead`, the card's signup counts line says `answers on the ${e.lead.raid} sheet` instead of the Join / Bench / No thanks buttons' hint (the follow-on has no buttons of its own: find the signup buttons block and render it only when `!e.lead`).

`pages/Me.tsx` `SheetRow`: the title `{s.raid}{s.then ? `, then ${s.then}` : ""}{s.lead_raid ? ` · after ${s.lead_raid}` : ""}{s.kind === "adhoc" ? " · ad hoc" : ""}`; after the `may_cant` group:

```tsx
      {s.may_leave && <Group gap={6}><Button size="compact-sm" variant="subtle" color="red" loading={busy === "leave"} disabled={busy !== null} onClick={leave}>Leaving after {s.lead_raid}…</Button></Group>}
      {s.may_stay && <Group gap={6}><Button size="compact-sm" variant="subtle" color="teal" loading={busy === "stay"} disabled={busy !== null} onClick={stay}>I'm staying</Button></Group>}
```

with

```tsx
  async function leave() {
    const r = await ask({ title: `Leaving after ${s.lead_raid}?`, message: s.leave_consequence || "", confirmLabel: "Leaving early", color: "red",
      note: { label: "Note for the officers (optional — never on the public sheet)", maxLength: 300 } });
    if (!r) return;
    setBusy("leave");
    try { const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/out`, { note: typeof r === "object" ? r.note : "" }); ok(x.message); }
    catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  async function stay() {
    setBusy("stay");
    try { const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/stay`, {}); ok(x.message); }
    catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
```

Then `cd frontend && npm run build` and `uv run python scripts/check_bundle.py`.

- [ ] **Step 4: Run the checks**

Run: `cd frontend && npx tsc --noEmit -p . && npm run build && cd .. && uv run pytest tests/test_frontend_source.py tests/test_copy.py -q && uv run python scripts/check_bundle.py`
Expected: PASS, bundle OK.

- [ ] **Step 5: Commit**

```bash
git add frontend/src src/oibot_gm/web/static/app tests/test_frontend_source.py
git commit -m "Follow-on runs on the site: a Then row in the schedule editor, a Then field on Start an ad hoc run, the run cards say then / after, the Me row leaves after the lead and comes back with I'm staying (spec 2026-11-04, task 8)"
```

---

### Task 9: The manual, the design and the agent guide

**Files:**
- Modify: `docs/manual.md` (§1a after *Leftovers* ~line 65; §4 after *Recurring and ad hoc* ~line 490; §7's plain-text examples; §8b's `open_run` / `schedule_set` rows ~1699), `docs/design.md` (append §5.62 after §5.61), `CLAUDE.md` (the `raidcycle.py`, `discord_raid.py`, `registry.py` lines)
- Test: `tests/test_help.py` (the manual's sections still parse), `tests/test_copy.py`

- [ ] **Step 1: Write the manual**

§1a, after *Leftovers*:

```markdown
- **Follow-on** — a second raid the same night, from the same signup, that starts when the first ends (the **lead**).
  One Join covers both; the lead's lock builds the follow-on's rosters from the lead's roster (two 10s are the 20 split
  in two, bench joiners cover gaps); nobody confirms twice. *Leaving after <lead>* on the Me page (or `/raid out` on the
  follow-on) frees your follow-on place and keeps your lead seat; *I'm staying* undoes it. Cancelling the lead cancels
  its follow-on; the follow-on can be cancelled alone.
```

§4, a new paragraph after *Recurring and ad hoc*:

```markdown
**A second raid the same night (a follow-on).** A schedule (Raids page › *then, the same night*; plain text
`<schedule>.follow = barrow_deeps x2`) or an ad hoc Open (*Then, the same night* on the Rosters page; plain text *"set
up an ad hoc Hyjal raid for 6:30 on Dec 10 with 10-man Barrow Deeps splits afterwards"*) names a follow-on: a second raid
with its own rosters per run, starting when the first ends (the lead's length). The lead's sheet is the night's sheet
(*Hyjal Summit, then Barrow Deeps ×2*); the follow-on has no sheet and no answers of its own — they follow the lead's —
and its health shows on the lead's card as one line. The lead's lock locks both: the follow-on prefers the lead's
rostered, so the 10s are the 20 split in two and bench joiners only cover gaps; it never asks anyone to confirm (the
lead's Confirm covers the night, and a *Can't make it* on the lead frees both seats). Its lock cards, board, fill asks
and loot councils are its own (*Barrow Deeps · after Hyjal Summit · Roster 1*). Leaving after the lead: *Leaving after
Hyjal Summit…* on the Me page, `/raid out` on the follow-on, or an officer's removal on its board — only the follow-on
place is freed; *I'm staying* undoes it (the seat comes back when one is free). Cancelling the lead cancels the
follow-on; cancelling the follow-on alone leaves the lead standing. The Discord `/raid open` and schedule wizards show
a follow-on but don't set one — the site and plain text do.
```

§7 (officers' plain text): add the owner's sentence to the examples with what it does ("opens the ad hoc Hyjal with a Barrow Deeps ×2 follow-on and sets the weekly Tuesday schedule").

§8b: `open_run`'s row gains "`follow` ('barrow_deeps x2')"; `schedule_set`'s gains `follow`.

- [ ] **Step 2: Write design §5.62**

Append to `docs/design.md`, after §5.61, the spec as built: copy `docs/superpowers/specs/2026-11-04-follow-on-runs-design.md` §2–§8 into a `### 5.62 Follow-on runs: a second raid the same night from one signup (built 2026-11-04)` section, rewritten in the past tense where the build differed (the reopen rule; no `PARITY_PENDING` row — the matrix is per op; the D2 gap named here and in the manual), with the helper names as they exist (`rc.sync_follow`, `rc.leave_follow`, `rc.stay_for_follow`, `RaidMixin.lock_follow`, `after_lead_out`, `rc.follow_short`, `configops.follow_of_op`, `/api/me/run/{key}/stay`).

- [ ] **Step 3: CLAUDE.md**

`raidcycle.py`'s entry gains one sentence: `Follow-on runs (§5.62): a lead's `followed_by` / a follow-on's `follows`; `ensure_run(follow=)` makes both run dicts (the follow-on in the reserved `after` key space, confirm off); `sync_follow` = the follow-on's answers are the lead's (every save of the lead), `leave_follow` / `stay_for_follow`; `_solve_run` prefers the lead's rostered (LEAD_SEAT_BONUS); `conflicts` exempts the pair`. `discord_raid.py`'s: `lock_follow (the lead's lock locks both), after_lead_out, stay_for_follow, cancel_run cascades to the follow-on`. `registry.py`'s schedules line: `follow {raid, rosters} (parse_follow, follow_words)`.

- [ ] **Step 4: Run the gate**

Run: `uv run pytest tests/test_help.py tests/test_copy.py tests/test_follow_on.py -q && uv run python scripts/check_commands.py && uv run python scripts/check_bundle.py`
Expected: PASS, EXIT 0 each.

- [ ] **Step 5: Commit**

```bash
git add docs/manual.md docs/design.md CLAUDE.md
git commit -m "Follow-on runs: the manual's words (follow-on, lead), the Raids section's paragraph, design §5.62 as built, the agent guide (spec 2026-11-04, task 9)"
```

---

## Closing steps (the lead, after the final review)

1. `uv run python scripts/check_commands.py && uv run python scripts/check_bundle.py` (real exit codes), then `launchctl kickstart -k gui/$(id -u)/gg.earlyandoften.oibot`; confirm the ready line in `out/oibot.log`.
2. `uv run python scripts/shots.py --guild oi`: the Raids page's schedule editor (Then row), the Rosters page's Start an ad hoc run (Then field), a lead's card and the Me row.
3. Nothing is opened on a live guild by the plan. The owner sends the sentence to the bot when ready.
