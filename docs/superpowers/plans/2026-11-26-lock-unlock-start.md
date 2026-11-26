# Unlock, a lock that asks before it fills, Start now — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** An officer can unlock a locked event and lock it again; a lock fills empty slots only when asked; the clock's lock and its fill are settings; a locked event can be started now.

**Architecture:** Two stamped event settings gate the scheduler's lock. `RaidMixin.lock_run` gains a `fill` flag: False builds the roster from the board alone. A new `rc.unlock` turns rosters back into board placements and reopens the event; the next lock reconciles confirmation asks instead of re-sending them. `rc.start_now` moves the start through the reschedule path without touching the confirm deadline.

**Tech Stack:** Python 3.12 / uv, discord.py, FastAPI, React + Mantine (committed bundle).

**Spec:** `docs/superpowers/specs/2026-11-26-lock-unlock-start-design.md` (binding; read it first). Facts about today's code with file:line: the controller passes a recon file path in each dispatch.

## Global Constraints

- CLAUDE.md Principles bind every string: Copy, Confirm (≤ 3 sentences, ≤ 20 words each, linted by `tests/test_confirm_readability.py` — every new box generator is registered there with its cases), Clock, Wizard, DM, Iconography. Every word a page prints comes from the server (PAGE_WORDS / the box); "event", never "run", in person-read text.
- **No new DM kinds.** Unlock, Start now and a setting change DM nobody. Outcomes are notices and edited cards.
- **Defaults keep today's behaviour:** a missing `auto_lock` / `auto_lock_fill` reads True; `RaidMixin.lock_run(..., fill=True)` is the default so engine-level tests keep meaning "solve everyone". Surfaces a PERSON presses (site, Discord, plain text, MCP) pass `fill` explicitly and default it to False.
- Parity: every new verb (lock's fill choice, unlock, start now, the two settings) exists on the site, Discord, plain text (`configops`) and MCP, and the parity matrix test is updated — nothing new goes to `PARITY_PENDING`.
- `docs/manual.md` is updated in the same commit as each behaviour; `docs/layout/events.md` / `confirms.md` / `frontend.md` entries updated when a name others rely on is added; `docs/design.md` §5.67 gains a "Lock, unlock, start now built" paragraph in the last task that touches it.
- No real guild data, member names or Discord ids. Never touch the live bot or the main checkout; never run `scripts/check_commands.py` (the controller does). Never a bare `git stash`. Stage only your own hunks.
- Tests: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests` (a failure under `-n` is re-run alone before it counts). Frontend: `cd frontend && npx tsc --noEmit && npm run build`, `uv run python scripts/check_bundle.py`, bundle committed with the source.
- Multi-guild: process state keyed by (guild key, event key); persistent `custom_id`s carry the guild key.

## Review Focus

1. A seat freed or an ask expired because of Unlock, a late lock or Start now (the spec's anti-goal): confirmation expiry while open, a confirm deadline already past at the lock, `confirm_by` shifted into the past by Start now.
2. The scheduler locking an unlocked event, or an Auto lock Off event, a minute later; a failed as-it-stands clock lock retried every minute.
3. A second lock after an unlock re-DMing people who already confirmed, or writing a second Not rostered notice.
4. A multi-raid event: unlock, the fill choice and Start now applied to the first raid only.
5. An event stored before this change (no settings, no stored lock-line id, `placed_by` wiped): unlock and lock must still work.

---

### Task 1: The two settings, and the clock obeys them

**Files:** `registry.py` (schedule fields), `raidcycle.py` (stamp at open, readers, `EVENT_TIMING` / edit-event fields, Details words), `raid_scheduler.py`, `constants.py` (PAGE_WORDS labels, `field_help`), `event_view.py`, `schedule_view.py`, `consequences.py` (the Save boxes' diff labels), `configops.py`, `mcp_server.py`, `flows_schedule.py` / `flows_event.py` (the Discord editors), `frontend/` (the schedule editor and Edit event: the two switches, the second shown under the first and disabled when it is Off; Details rows), `docs/manual.md`, tests.

**Produces:**
- Schedule fields `auto_lock: bool = True`, `auto_lock_fill: bool = True`; stamped on the event at open like sheet timing; `rc.auto_lock(reg, ev) -> bool`, `rc.auto_lock_fill(reg, ev) -> bool` (missing → True). A one-off's open form offers both.
- Editable per event before the lock through the existing edit-event verb on all four surfaces; refused after the lock in one sentence like the other timing fields.
- Labels **Auto Lock** On/Off and **Auto-fill on Auto Lock** On/Off, with (?) hints, in Details on Events and Schedules and in the editors (same word tables #13 used).
- Scheduler: with `auto_lock` False it neither closes pre-lock asks nor locks at the lock time; those asks run to their own deadline. With `auto_lock` True it calls `lock_run(..., fill=rc.auto_lock_fill(reg, ev))` — Task 2 adds the parameter; until then pass nothing and leave a `# Task 2` marker test xfail-free by testing only the off path here.
- Details "Locks" says, in a server sentence, that an officer locks it when Auto lock is Off.
- Manual: both settings; what an event still open at its start does today (find it in the scheduler and state it; do not change it).

- [ ] Failing tests: defaults True on old stored schedules/events; stamped at open; scheduler does not lock an Off event past its lock time and does not close its pre-lock asks; edit before lock on each surface; refused after; Details words; Save box diff lines pass the confirm lint.
- [ ] Implement; frontend build; parity matrix; manual. Fast suite green; commit.

### Task 2: The lock's fill choice in the engine

**Files:** `raidcycle.py`, `discord_raid.py` (`lock_run`, `lock_parts`, `lock_preview`), `raid_scheduler.py`, `docs/manual.md`, tests (`tests/test_lock_as_it_stands.py` new).

**Produces:**
- `RaidMixin.lock_run(reg, rs, ev, by="scheduler", fill: bool = True)`; `lock_parts` passes the same `fill` to later raids; `lock_preview(..., fill: bool = True)`.
- `fill=False` — **as it stands**: the roster is exactly the board (every placed name in its group, including Bench answers Auto-fill put there; a pinned-in or never-bench name NOT on the board is not rostered). Build it by the solve with every unplaced name held out, or from the board directly — whichever keeps the usual advisories and `lock_seats`; the result must equal the board name for name, group for group (test). `rc.lock_open_slots(reg, rs, ev) -> list[int]` (open slots per roster on the board as it stands) and `rc.lock_left_out(reg, ev) -> list[str]` (kept-on names not on the board) for Task 3's box.
- Refusal `rc.LOCK_EMPTY_BOARD` (one sentence naming the way out) for `fill=False` when people joined and nobody is placed; an event nobody joined still locks as today. From the scheduler this is a failed lock: `lock_error` set, the existing 15-minute back-off applies, one officers' line.
- **A late lock:** when the confirm deadline `run_times` gives is not in the future at the lock, `confirm_by = min(start, now + 1h)` (share the rule Reschedule uses — one helper, not a copy). Applies to every lock.
- `fill=False` with open slots left and Automated Fill Requests On: the lock pauses them for the event through the existing pause (`autofill_paused`, by = the locker), one log line. `rc.leftover_notices` at such a lock uses a second sentence that promises no DM: an officer decides who takes an open slot.
- The lock stores the signup channel's "is locked" message id on the event (`ev.lock_line = [channel_id, message_id]`), for Task 4.
- The scheduler passes `fill=rc.auto_lock_fill(reg, ev)`.

- [ ] Failing tests: as-it-stands equals the board (single roster, two rosters, lineup's later raid, Bench name on board kept, pinned-in off board left out); default `fill=True` byte-identical rosters to before on three existing fixtures; empty-board refusal vs nobody-joined; late-lock deadline never in the past and ≤ start; asks paused and notice wording; clock lock with fill Off; failed clock lock not retried within 15 min.
- [ ] Implement; manual (the lock section). Fast suite green; commit.

### Task 3: The lock box and every surface's choice

**Files:** `consequences.py` (`lock_box`, `_LockFacts`, `_lock_head`), `event_view.py`, `web/api.py` (`/api/run/{key}/lock`, `/lock/preview`), `raid_buttons.py` (`lock_decide`), `configops.py` (`run_lock`), `mcp_server.py` (`lock_run`), `frontend/` (one lock box component used by Events and SheetCard — SheetCard's own LockModal goes), `constants.py`, `docs/manual.md`, `docs/layout/confirms.md`, tests.

**Produces:**
- `consequences.lock_box(reg, ev, rs, fill: bool = False)`: board complete → today's box, no choice. Otherwise it leads with the open slots per roster ("Roster 1 has 3 open slots."; one roster: "The roster has 3 open slots."), then for `fill=False` what locking as it stands does (who is rostered, that Automated Fill Requests pause when On, a kept-on name left out when there is one), for `fill=True` today's rule sentence. Still ≤ 3 sentences of ≤ 20 words: choose which facts lead; register all cases in `tests/test_confirm_readability.py`. The box carries `choice: {label, value}` (label from PAGE_WORDS: "Auto-fill empty roster slots") only when a choice exists. The box fingerprint includes `fill`.
- Routes: `POST /api/run/{key}/lock` body `fill` (default False), `preview` returns the box for that `fill`; `/lock/preview` takes `fill`. A stale `seen` → 409 with the fresh box, as today.
- Site: the box draws the checkbox (unticked) between the diff and the sentences, re-previews through `replace(box)` on change (RescheduleBox is the model); Preview lock shows the roster for the current choice.
- Discord `lock_decide`: open slots → buttons **Lock as it stands** · **Lock and auto-fill** · Preview lock · Back (each verb's screen shows its own box); none → **Lock**. Labels from RUN_WORDS.
- Plain text: `run_lock` gains `fill` (default False); "lock and fill …" sets it; the review line says which. MCP `lock_run(autofill: bool = False)`.

- [ ] Failing tests: box sentences per case and lint; fingerprint differs by fill; route default as-it-stands; 409 on stale; Discord buttons by case; parser cases; MCP flag; frontend source test for the choice.
- [ ] Implement; frontend build; manual; parity. Fast suite green; commit.

### Task 4: Unlock in the engine

**Files:** `raidcycle.py`, `discord_raid.py`, `registry.py` (remove a notice by kind + event; an answer word for a withdrawn ask if one is missing), `raid_scheduler.py` (expiry guards), `docs/manual.md`, `docs/layout/events.md`, tests (`tests/test_unlock.py` new).

**Produces:**
- `rc.unlock_refusal(reg, rs, ev, at=None) -> str | None`: not locked; started; a loot council open on it. One sentence each.
- `rc.unlock(reg, rs, ev, by) -> Unlocked` for the FIRST raid, applied to every raid of the event: `layout` = the locked rosters' groups; every rostered name marked placed by hand (`placed_by`), `auto_placed` empty; `rosters` cleared; `state="open"`, `locked_at=None`, `confirm_by=None`, `lock_error=None`; `auto_lock=False` on the event; pending fill asks closed; `autofill_paused` cleared (it means nothing while open). A person whose seat was freed after the lock by silence or by an officer's move returns to Join; one who said Can't make it or called out stays out — decide from the stored source / callout and state in the report exactly which stored values map to which.
- Confirmation asks: untouched by unlock. `expire_confirmation_asks` and every other silence path never act on an open event (test). A Confirm / Can't pressed while open is recorded; a Can't sets the answer out and takes the name off the board.
- `RaidMixin.unlock_run(reg, rs, ev, by, by_id=None) -> str`: under the same locks as `lock_run`; withdraws fill-ask cards (as `cancel_run` does); deletes the officer channel's lock cards and the stored lock line (missing id or message gone → nothing fails); `refresh_sheet`, `refresh_cards`; removes this event's `not_rostered` notices; one thread line, one audit line.
- **The next lock reconciles:** a rostered person with a yes answer for this event is not asked; one with an open ask keeps it (card re-rendered); a new one is asked; a person with an ask or a yes who is no longer rostered has the ask closed and its card edited, and gets the Not rostered notice — at most one such notice per person per event, ever.
- An event locked before this change (no `lock_line`, `placed_by` empty) unlocks the same way.

- [ ] Failing tests: board after unlock equals the roster, all hand-placed; a relock with `fill=False` reproduces the roster exactly; lineup unlocks whole; the scheduler does not relock (auto_lock False); no expiry while open; reconcile cases (confirmed kept, open kept, new asked, dropped closed + one notice); freed-by-silence returns, Can't stays out; notices removed; refusals; old stored event.
- [ ] Implement; manual; layout doc. Fast suite green; commit.

### Task 5: Unlock on every surface

**Files:** `consequences.py` (`unlock_box`), `event_view.py`, `web/api.py` or `web/api_events.py` (`POST /api/run/{key}/unlock`), `frontend/` (Events › More and the sheet card), `raid_buttons.py` / `raid_commands.py` / `raid_views.py` (the lock card's button and `/raid unlock`, beside End), `configops.py` (`run_unlock`, always reviewed), `mcp_server.py` (`unlock_run`), `constants.py`, `docs/manual.md`, tests.

**Produces:** `consequences.unlock_box(reg, ev, rs)` — what changes (open signup, rosters stay on the board), who is told (nobody gets a DM; N confirmation asks stay open, said only when N > 0; fill asks withdrawn when any), what follows (Auto lock is off, an officer locks it again); `seen` / 409 as other boxes; the button shown only when `unlock_refusal` is None, the refusal as the disabled note otherwise; registered in the confirm lint and the parity matrix.

- [ ] Failing tests per surface; lint; frontend source test.
- [ ] Implement; frontend build; manual. Fast suite green; commit.

### Task 6: Start now, and the loot End gap

**Files:** `raidcycle.py`, `discord_raid.py`, `consequences.py` (`start_now_box`), `web/api.py`, `web/api_loot.py`, `discord_loot.py` (`end_run`), `raid_buttons.py` (`end_decide`'s refusal screen, the lock card), `raid_views.py`, `configops.py` (`run_start`), `mcp_server.py` (`start_now`, `end_loot`), `frontend/` (More, SheetCard beside a disabled End, Loot › Tonight), `docs/manual.md`, `docs/design.md` §5.67, tests.

**Produces:**
- `rc.start_now_refusal(reg, rs, ev, at=None)`: not locked; already started. `rc.start_now(reg, rs, ev, by)`: the first raid's start becomes this minute, later raids move by the same delta, through the reschedule path's writes EXCEPT `confirm_by`, which is not changed (test: no ask expires at the next tick, no seat is freed). The `moved` notice as Reschedule sends it; no DM; cards and sheet edited in place.
- `consequences.start_now_box`: what changes; who is told; that a started event cannot be moved back.
- Offered in Events › More, on the sheet card and the Discord lock card whenever `start_now_refusal` is None, and next to the "hasn't started" End refusal on the site (the disabled End's note gains the action) and in Discord's `end_decide` refusal screen (a **Start now** button that opens the box).
- `LootMixin.end_run`, `POST /api/loot/{run}/end` and MCP `end_loot` apply `rc.end_refusal` like every other End (keep the existing exception for an open council exactly as `end_decide` has it); Loot › Tonight shows the refusal with Start now.
- Plain text `run_start` (always reviewed); MCP `start_now`.

- [ ] Failing tests: start moved, parts shifted, `confirm_by` untouched, End allowed afterwards, Reschedule refused afterwards; refusals; loot End refused before the start on route and MCP, allowed with a council; surfaces; lint; parity.
- [ ] Implement; frontend build; manual; design.md paragraph covering Tasks 1–6. Fast suite green; commit.
