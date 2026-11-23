# Events, New event, Schedules and Raids pages (phase 4) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The site's raid pages become what the owner approved on the mockups: an Events page (list on the left, one
event in sections, the board always drawn as the lock would place it, role switches as one-line moves), a New event page
whose lineup builder is also Edit lineup and the schedule editor, a Schedules page, and a Raids page with Rules ·
Priority · Readiness — every press that asks first showing the §7b confirmation box with the server's sentences.

**Architecture:** The server gains read models that compose everything a page prints (`event_view.py`,
`schedule_view.py`, `raid_page.py`), one board-preview cache (`event_board.py`) that runs the lock's own solve in a
thread per event fingerprint, and a `Box` shape (`consequences.box`) that every confirm carries: title, ≤ 3 sentences,
the verb, red or not, the "label: old → new" lines and a fingerprint the action route re-checks (`seen` → 409 with the
fresh box). Writes stay calls to the existing verbs (`lock_run`, `cancel_run`, `reschedule_event`, `edit_lineup`,
`board_move`, `save_board`, `set_answer`, `send_plan`, `do_member_action`, `apply_ops`, `regular_add/remove`); three
verbs gain one parameter each (`set_answer(spec=)`, `send_plan(seats=)`, `do_member_action(spec=)`). The frontend gets
one page file per page plus focused components; old pages keep working until the task that replaces them deletes them;
the rail changes last.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, OR-Tools CP-SAT (through `raidcycle._solve_run`), React 19 + Mantine 9 +
@mantine/dates (Vite), react-router-dom 7 (`BrowserRouter`, basename `/app`), pytest, Playwright (`scripts/shots.py`).

**Spec:** `docs/superpowers/specs/2026-11-22-raids-events-schedules-design.md` — §9 item 4, bound by D9, D11–D18, C3, C9,
C10, C13, C14, §7 as superseded by §7a, §7a in full, §7b (site part), §8. The approved mockups (one HTML per board):
`/private/tmp/claude-501/-Users-jonathanlin-dev-oibot-gm/0fc69e01-b901-4101-9fd7-14893493fd6a/scratchpad/mock/project/`
`Main.dc.html` (Events), `NewEvent.dc.html`, `Schedules.dc.html`, `Raids.dc.html` (+ `Readiness.dc.html`, its third view),
`Confirms.dc.html` (the twenty boxes). Starts from the state phases 2 and 3 leave:
`docs/superpowers/plans/2026-11-23-raids-without-times.md` and `docs/superpowers/plans/2026-11-23-events-with-identity.md`.
Read `CLAUDE.md` first (the frontend paragraph and the Copy, Confirm, Clock, Iconography and DM rules).

## Global Constraints

- **Plan against phases 2 and 3 as their plans state them** (the tree may be half-built while this is written). Every task
  names what it consumes from them. Names used here from those plans: `SCHEDULE_TIMING`, `EVENT_TIMING`,
  `Registry.minimums` / `set_minimums`, `Registry.all_schedules` / `schedule_entry` / `schedule_runs_open` /
  `schedule_removal_effects(sid, rs)`, `config_rev("schedule:<id>")`, `rc.occurrences`, `rc.occurrence_stamp`,
  `rc.event_of_occurrence`, `rc.event_parts` / `first_part` / `later_parts`, `rc.lineup_refusal`, `rc.reschedule_refusal`,
  `rc.parts_short`, `RaidMixin.reschedule_event(..., preview=)`, `RaidMixin.edit_lineup(..., preview=)`,
  `consequences.reschedule_consequence`, `lineup_consequence`, `schedule_keeps_sentence`, `before_first_open_sentence`,
  routes `POST /api/run/{key}/reschedule`, `POST /api/run/{key}/lineup`, `POST /api/raid/{rid}/open` (+ `/preview`) taking
  `lineup` and `occurrence`, `ev_json` fields `part`, `event`, `occurrence`, `lineup`, the structured kinds `schedule` /
  `schedule_remove` by id with revs `config:schedule:<id>`, `/api/raids` rows with `minimums` / `minimum`, `/api/readiness`
  rows with `minimums` / `minimum`. Where a phase leaves one of these out, the task that consumes it adds it (said there).
- **`LINEUP_MAX = 4`**, 1–4 rosters per step, one raid per step (D10), no raid twice; in Edit lineup the first step's raid
  is fixed (`LINEUP_FIRST`).
- **Page words** (copy exactly; the mockups as approved — these are the only "event" words this phase adds, the rest of the
  rename is phase 6):
  - Events: page title `Events`; button `New event`; list groups `Open` · `Coming up` · `History`; header actions
    `Reschedule`, `Edit lineup`, `Lock now`, `Open the sheet now`; quiet action `Cancel event`; sections `Details`,
    `Signups`, `Role switches`, `Board`, `Activity · {n} lines`, history's `Board, as it was locked`; Details labels
    `Lineup`, `Comes from`, `Priority`, `Confirmation`, `Sheet opened`, `Sheet opens`, `Opening by hand`, `Locks`,
    `Signup sheet` (value `Open in Discord`), history `Rostered`, `Ended`; values `One-off, no schedule`, `First to join`,
    `Not asked`, `Asked at the lock`, `From {local12}`; Signups labels `Joined`, `Bench`, `No thanks`, `No answer yet`,
    button `Answer for someone`; Role switches label `{Raid} is short`, buttons `Show {n} possible switches` /
    `Hide the {n} switches`, row `{Name}: {from} → {to}`, `no answer yet`, buttons `Switch` / `Answer for them`; Board raid
    switch `{time} · {Raid}`, facts `Rosters` (`1 of 20`), `Placed` (`18 of 18 joined`), `Short`, headings `Roster {n}`,
    `Group {n}`, the empty seat `open seat`, footer `Placed as the lock would place them now. Drag a name to move it.`,
    button `Undo my moves`; chips = `RUN_STATE_WORDS` values, plus `no sheet yet`.
  - New event: title `New event`, link `Back to Events`; sections `Start from` (label `Copy a lineup`, last option
    `An empty lineup`), `Lineup` (palette label `Raids`, a raid's line `{size}-player · about {h} h`, hint
    `Drag a raid onto the track. Drag a step to reorder it.`, step head `Step {n}`, `Starts`, `Rosters`, stepper
    aria-labels `one roster fewer` / `one roster more`, node `{Raid}` + `{n} seats`, `{Raid} · roster {k}`, empty slot
    `Drop a raid here for step {n}`), `When` (labels `Day`, `Starts`, `Locks`; options `2 hours before`,
    `24 hours before`), `What this makes` (labels `One signup sheet`, `Seats`, `Priority`, `Confirmation`); buttons `Back`,
    `Open the sheet`. Keyboard / phone controls this plan adds: `Add` per palette raid, `Move earlier` / `Move later` /
    `Remove` per step (aria-labels name the raid: `Move Barrow Deeps earlier`).
  - Schedules: title `Schedules`; button `New schedule`; groups `Active` · `Paused`; chips `active` / `paused`; actions
    `Edit`, `Pause` / `Resume`, `Remove`; sections `Details` (`Repeats`, `First event` | `Next event if resumed`,
    `Priority`, `Confirmation`, `Open seats after the lock`, `Answers` = `One for the whole evening`), `Lineup`,
    `Sheet timing` (`Sheet opens`, `Reminder`, `Locks`, `Confirm by`, `Open events keeping their own timing`),
    `Next events` (columns `Starts`, `Sheet opens`, `Locks`), `Regulars` (`Regulars` = `{n} for {seats} seats`,
    `Expected without regulars` = `Every main`, `Regulars are short`, `Next four weeks`; `Add a member`, placeholder
    `search members`, button `Add`).
  - Raids: title `Raids`; list line `{n} players`; view switch `Rules` · `Priority` · `Readiness`; button `Edit`; Rules
    sections `Details` (`Size`, `Groups`, `Lockout`, `Length`, `First opens`, `Played in`) and `Group make-up` (columns
    `In each roster`, `Minimum`); Priority section `Priority` (`Used when` = `More people join than there are seats`,
    `One-off events` = `First to join, whatever the priority`; bars `Regular`, `Rank`, `Main character`,
    `Sat out last time`, `Signed up early`); Readiness sections `Who could play` (`Standing`, `Mains` =
    `{n} for {size} seats`, `Alts`, `Could switch role`) and `Against the make-up` (columns `In each roster`, `Have`,
    `Minimum`).
  - These words live ONCE in Python and reach the site through `/api/meta.page_words` (Task 2) or the read models'
    JSON; a `.tsx` file never holds a value word (`First to join`, `Not asked` …) — section headings and field labels
    may be literals in the page files.
- **§7b boxes.** A box = `{title, sentences (≤ 3), verb | null, danger, diff: ["label: old → new"], fp}` built only by
  `consequences.box` / `refusal_box`; the site prints it and composes none of it. Two buttons: `Back` then the verb;
  `color="red"` only when `danger`. Never `Yes`, `No`, `OK`, `Cancel` or `Confirm` as a label on these four pages
  (`Cancel event` is the verb). A refusal = `verb: null`, one sentence, only `Back`. A box whose facts changed while open
  shows the fresh box, the line `CONFIRM_WORDS["changed"]` above it, and waits again.
- **Clock rule:** a date is picked from `DatePickerInput` (calendar), a time from a `Select` of 15-minute steps whose
  labels come from `clock12`; no `TextInput` takes a time, no typed timestamp anywhere. Every time a page prints comes
  from the server's `local12` words.
- **Iconography rule:** a role count is role icon + number; a spec icon alone on a member line; table rows of the make-up
  are words (`Tanks`) with no icon beside them. The empty seat is the role icon + `open seat` in a dashed outline (the
  words name the seat, not the icon).
- **Phone (below `sm`, 48em):** every list page is list-then-detail: `/events` shows the list; picking navigates to
  `/events/<key>`, whose detail starts with a `Back to Events` link and hides the list. The same for `/schedules/<id>` and
  `/raids/<id>`. Every control ≥ 44 px (the existing `rosters.module.css` rule, moved to `components/board/board.module.css`
  in Task 10 and reused as `pages/listpage.module.css`'s `.touch`). No horizontal page scroll at 400 px.
- **Every edit mode calls `useDirty(key, on, what)`**; leaving through the rail or the list asks `Leave without saving?`.
- **Out of scope:** Discord wizards and confirm buttons (phase 5); the rename of existing strings, the manual's glossary
  and the copy lint's word list (phase 6); saved lineups, a calendar, per-raid answers (§8). The Members page's Test
  bench keeps `SheetCard` (it is not an event page).
- **Commands.** Frontend: `cd frontend && npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py`.
  Fast tests per task as listed. The full check (`scripts/check_commands.py`, ~20 min) runs once, in Task 12. Never pipe a
  check through `tail` before `&&`. `docs/manual.md` changes in the same commit as the behaviour it describes. Commits on
  the work branch; merging, pushing and restarting the bot are the owner's.
- **Screenshots** never touch the live bot: `scripts/serve_scratch.py` (Task 6) serves this worktree's app on a scratch
  copy of the data with the test suite's `FakeBot`; `scripts/shots.py http://127.0.0.1:8799 --guild <key>` shoots it. The
  implementer LOOKS at every shot named in a task and describes desktop and phone in the task report.

## Review Focus

1. **An event with zero signups:** the board shows every roster's groups empty with an open seat per minimum, no solve
   runs, `Placed` reads `0 of 0 joined`, Lock's box says nobody is asked to confirm, Role switches lists only
   `Answer for them` rows. (Task 3 `test_an_event_with_nobody_joined_draws_open_seats_and_never_solves`, Task 4
   `test_switches_with_nobody_joined_are_answers_for_them`.)
2. **A 4-raid lineup on a phone:** the builder stacks steps vertically, every step reachable with Add / Move earlier /
   Move later / Remove and the stepper, no drag needed and no sideways scroll; the board's raid switch wraps. (Task 7
   `test_the_builder_works_without_a_drag` + its phone shot; `forever` has three raids, so the 4-step case is a source
   assertion and a tbc-profile API test — see "Impossible or contradictory".)
3. **A board recompute while someone drags:** a drop is sent as one move against the board as it is NOW on the server; a
   preview that changed under the drag never loses the move or applies it to a stale picture; the page does not re-render
   mid-drag. (Task 3 `test_a_move_lands_on_the_board_as_it_is_now_when_the_preview_changed`, Task 9's source test on
   `live.dragging`.)
4. **Two officers editing the same schedule:** the second Save carries the first one's stale `config:schedule:<id>` rev,
   gets 409, keeps its draft and says the schedule changed; nothing is overwritten. (Task 5
   `test_a_stale_schedule_save_is_refused`, Task 11 source test.)
5. **A confirm box whose facts change while open:** a Lock pressed after someone joined meanwhile is refused with 409 and
   the fresh box (new count), which the page shows and waits on. (Task 1 `test_a_stale_box_is_refused_with_the_fresh_one`,
   Task 6 `ConfirmBox` source test.)
6. **Leaving the builder with unsaved edits:** a rail link, a list pick or `Back` asks `Leave without saving?` with the
   count of edits; `Discard edits` clears the draft and goes; `Keep editing` stays. (Task 6 `test_leaving_asks_first`,
   Task 7 / Task 11 register their drafts.)

---

## File structure

Server (new): `src/oibot_gm/event_view.py` (the Events read model: list, one event, coming up, history),
`src/oibot_gm/event_board.py` (preview cache + fingerprint), `src/oibot_gm/schedule_view.py` (Schedules read model),
`src/oibot_gm/raid_page.py` (Raids read model rows), `src/oibot_gm/web/api_events.py`, `src/oibot_gm/web/api_schedules.py`,
`scripts/serve_scratch.py`. Server (changed): `consequences.py`, `inbox.py`, `constants.py`, `raidcycle.py`,
`discord_raid.py`, `configops.py`, `mcp_server.py`, `raid_views.py` (site link), `help.py` (PAGES), `web/api.py`,
`web/app.py`, `scripts/shots.py`.

Frontend (new): `components/ConfirmBox.tsx`, `components/LeaveGuard.tsx`, `components/ListPage.tsx`,
`components/Facts.tsx`, `components/WhenPicker.tsx`, `components/lineup/LineupBuilder.tsx`,
`components/lineup/LineupPicture.tsx`, `components/events/{EventDetails,SignupsSection,RoleSwitches,EventBoard,ActivitySection,RescheduleBox,ComingUpView,HistoryView}.tsx`,
`components/schedules/{ScheduleDetails,RegularsSection}.tsx`, `components/raids/{RulesView,PriorityView,ReadinessView}.tsx`,
`pages/Events.tsx`, `pages/LineupPage.tsx`, `pages/Schedules.tsx`, `pages/listpage.module.css`. Rewritten: `pages/Raids.tsx`,
`App.tsx`, `hooks/useDirty.ts`, `components/board/BoardView.tsx`, `api.ts`. Deleted: `pages/Readiness.tsx` (Task 6),
`components/CompTargets.tsx` (Task 6; `SlotIcon` / `RoleIcon` move to `components/Icons.tsx`), `pages/Rosters.tsx`
(Task 10), `components/ScheduleCard.tsx`, `components/RaidHeader.tsx`, and `RaidCard.tsx`'s `WhenItRuns`, `RunWeek`,
`SeatWeights`, `Section`, `Panel`, `Pair` (Task 11; `RunStrip` stays for `SheetCard`).

---

### Task 1: The confirmation box and its sentences, on the server

**Files:**
- Modify: `src/oibot_gm/consequences.py`, `src/oibot_gm/inbox.py` (`action_consequence`), `src/oibot_gm/constants.py`,
  `src/oibot_gm/raidcycle.py` (word helpers only), `src/oibot_gm/web/api.py` (lock, cancel, open routes: `seen`), the
  phase-3 reschedule / lineup routes (`seen`)
- Test: `tests/test_confirm_boxes.py` (new), `tests/test_confirm_readability.py` (cases)
- Modify: `docs/manual.md` (§4: the confirm says who is told; the reschedule refusal's sentence)

**Interfaces:**
- Consumes: phase 3's `rc.event_parts`, `first_part`, `reschedule_refusal`, `reschedule_consequence`, `lineup_consequence`,
  `Registry.schedule_removal_effects(sid, rs)`, `schedule_runs_open(sid, rs)`, `rc.occurrences`; phase 2's `minimums`.
- Produces (all in `consequences.py` unless named):
  - `box(title: str, sentences: list[str], verb: str | None, *, danger: bool = False, diff: list[str] = ()) -> dict` →
    `{title, sentences, verb, danger, diff, fp}`; `fp` = sha1 of `title`, `diff`, `sentences` joined by `\n`, first 12 hex.
    `refusal_box(title: str, sentence: str) -> dict` = `box(title, [sentence], None)`.
  - `REFUSAL_TITLES: dict[str, str]` — `"past": "That time has passed"`, `"clash": "That time is taken"`,
    `"closed": "This event is over"`, `"locked": "The lineup is fixed"`, `"other": "This can't go ahead"`.
  - `constants.CONFIRM_WORDS = {"back": "Back", "keep": "Keep editing", "discard": "Discard edits", "leave_title":
    "Leave without saving?", "changed": "This changed while the box was open. Read it again before you press."}`.
  - `leave_unsaved_sentence(n: int, name: str) -> str` — `"Your 2 edits to Hyjal Summit are not saved yet."`,
    `"Your one edit to Hyjal Summit is not saved yet."`; `LEAVE_TEMPLATE = {"one": "Your one edit to {name} is not saved
    yet.", "many": "Your {n} edits to {name} are not saved yet."}` (the site fills it; Task 2 puts it in meta).
  - `rc.lineup_words(reg, lineup: list[dict]) -> str` — `"Hyjal Summit, then Barrow Deeps × 2"`;
    `rc.lineup_roster_words(reg, lineup) -> str` — `"Hyjal Summit and both Barrow Deeps rosters"` (one roster: the raid's
    name; two: `both {Raid} rosters`; 3–4: `all {n} {Raid} rosters`; joined `, ` and a final ` and `);
    `rc.event_lineup(reg, ev) -> list[dict]` — part 0's dict `lineup`, else `[{raid: instance, rosters}] + follow`;
    `rc.priority_words(reg, ev_or_schedule_def) -> str` (`"First to join"` for a one-off; else the two heaviest weights:
    `"{A} first, then {b}"` from `constants.PRIORITY_SHORT = {"regular": "Regulars", "rank": "rank", "main": "main
    characters", "sat_out": "who sat out last time", "signup_order": "who signed up first"}`);
    `rc.confirmation_words(confirm: bool) -> str` (`"Asked at the lock"` / `"Not asked"`).
  - The twenty boxes (each generator returns the box; the sentence lists below are exact for the mockup's case):

| # | Box (mockup) | Generator | Title · verb |
|---|---|---|---|
| 1 | Lock, one-off | `lock_box(reg, ev)` over `_lock` rewritten: 1st sentence `The board becomes the roster for {lineup_roster_words}.`; one-off tail `Nobody is asked to confirm.` + `You can still move people after the lock.` | `Lock {event_title}?` · `Lock` |
| 2 | Lock, scheduled | same; `The 20 rostered get the Confirm or Can't make it DM.` + `They have until {local12} to answer.` (DMs-off variants kept) | same |
| 3 | Reschedule | `reschedule_box(reg, rs, ev, start, timing)` over phase 3's `reschedule_consequence`, reworded: `The event moves to {local12}.` · `All {n} answers stand, and each person gets a notice.` (0: `Nobody has answered yet.`) · third = phase 3's first applicable tail, else `The sheet in Discord shows the new time.` | `Move {event_title}?` · `Move` |
| 4 | Edit lineup, Save | `lineup_box(reg, rs, ev, lineup)` over phase 3's `lineup_consequence`, reworded: `{Raid} goes from two rosters to one.` / `{Raid} joins the lineup after {Raid}.` / `{Raid} leaves the lineup.` (the first change) · `All {n} answers stand.` · `Moves made on the removed roster are dropped.` (only when places drop); `diff` from `lineup_diff_lines(reg, rs, ev, lineup) -> list[str]` (`"Barrow Deeps: 2 rosters → 1 roster"`) | `Save this lineup?` · `Save lineup` |
| 5 | Cancel event | `cancel_box(reg, ev)` over `_cancel` rewritten for a whole event: `{Both raids of this event are cancelled. | This event is cancelled. | All {n} raids of this event are cancelled.}` · open: `The {n} who joined see it on the sheet, and nobody gets a DM.` / locked: `The {r} rostered see it on their cards, and nobody gets a DM.` · `A cancelled event cannot be reopened.` | `Cancel {event_title}?` · `Cancel event`, danger |
| 6 | Switch, before the lock | `switch_box(reg, ev, m, character, spec, raids, *, locked=False)`: `{Name} plays {Spec} for {raids}, not {Old}.` (another character: `{Name} plays {Char}, {Spec} for {raids}.`) · `They get a notice.` · `You can switch them back until the lock.` | `Switch {Name} to {Spec}?` · `Switch` |
| 7 | Switch, after the lock | same, `locked=True`: `{Name} is asked to play {Spec} for {raids}.` · `They get a DM with Yes and No.` (DMs off: `It waits on their Me page.`) · `Their seat changes only when they say yes.` | `Ask {Name} to play {Spec}?` · `Ask` |
| 8 | Answer for someone / for them | `answer_box(reg, ev, m, key, character, spec, *, by_id)` over `inbox.action_consequence(..., spec=)`; the pre-lock `join` branch reworded: `{Name} is marked as joined, playing {Spec}.` · `They get a notice.` · `They can change the answer themselves.` | `Answer for someone` · `Record answer` |
| 9 | Undo my moves | `undo_box(n: int)`: `Your {n} moves on this board are dropped.` (1: `Your one move …is dropped.`) · `Everyone goes back to where the lock would place them.` | `Undo your moves?` · `Undo moves` |
| 10 | Drag after the lock (swap) | `_board("swap")` after the lock reworded: `{name} and {other} swap seats.` · `Both see the change on the roster card.` · `You can swap them back.` (move / roster / substitute / release unchanged) | `Swap {name} and {other}?` · `Swap` (titles stay in `BoardView`, Task 9) |
| 11 | Open the sheet now | `open_now_box(reg, start, opens, lock_at)`: `The sheet opens now, {three days} early.` (`count_words`: one–ten spelled, then digits; days when ≥ 24 h, else hours) · `It posts in the signup channel.` · `It still locks {local12(lock_at)}.`; + `before_first_open_sentence` replaces the second sentence when it applies (C9) | `Open the sheet for {day}?` (`Tue 15 Dec`) · `Open the sheet` |
| 12 | Refusal: Reschedule into the past | `move_past_sentence(reg, new, old) -> str`: `{local12(new)} is in the past, so the event stays at {local12(old)}.` — replaces phase 3's `MOVE_PAST` constant in `reschedule_refusal`; route answers `refusal_box(REFUSAL_TITLES[kind], sentence)` | `That time has passed` · — |
| 13 | New schedule, Save | `schedule_create_box(reg, entry: dict)`: `An event opens {every Tuesday at 6:30 PM}, starting {local12(first)}.` · `Its first sheet opens {local12}.` · `You can pause or edit the schedule at any time.` | `Create {name}?` · `Create schedule` |
| 14 | Schedule Edit, Save | `schedule_save_box(reg, rs, sid, fields, diff)`: `The change starts with the {local12(day)} event.` (the first occurrence without a sheet) · `The {day} event is already open and keeps its timing.` / `The {n} open events keep their timing.` (none: omitted) — replaces phase 3's `schedule_keeps_sentence` in the `schedule_set` hook | `Save {name}?` · `Save` |
| 15 | Pause | `schedule_pause_box(reg, rs, sid)`: `No new sheet opens from this schedule.` · `The event already open stays open.` (n ≠ 1 variants; none: omitted) · `Regulars keep their spots.` | `Pause {name}?` · `Pause` |
| 16 | Resume | `schedule_resume_box(reg, rs, sid)`: `Sheets open from this schedule again.` · `The next one opens now, for {local12}.` / `The next one opens {local12(opens)}, for {local12(start)}.` | `Resume {name}?` · `Resume` |
| 17 | Remove | `schedule_remove_box(reg, rs, sid)` (counts from `schedule_removal_effects`): `The schedule and its {n} regular spots are removed.` (0: `The schedule is removed.`) · `The open event keeps its sheet and its answers.` (n open ≠ 1 variants; none: omitted) · `A removed schedule cannot be brought back.` | `Remove {name}?` · `Remove schedule`, danger |
| 18 | Removing a regular | `regular_remove` rewritten (key = schedule id): `{Name} is no longer a regular of {schedule name}.` · `They keep their answer on the open event.` (no open event: omitted) · `You can add them again.` | `Take {Name} off the regulars?` · `Remove` |
| 19 | Raid Edit, Save | `raid_save_box(reg, rs, rid, changed: set[str], diff)`: minimums only `The new minimum applies at the next lock.` (several: `minimums apply`), weights only `The new priority applies at the next lock.`, mixed `The changes apply at the next lock.`, lockout / first-open / length only `Lockout checks use the new values from now on.` · `That includes the event open now.` / `That includes the {n} events open now.` (none: omitted) | `Save {Raid}?` · `Save` |
| 20 | Leaving with unsaved edits | `leave_unsaved_sentence(n, name)` | `Leave without saving?` · `Discard edits`, danger; `Keep editing` in place of Back |

  - `event_title(reg, ev) -> str` = `reg.local12(first_part.starts_at)`; `{Raid}` and `{Name}` as people read them.
  - `CONSEQUENCE_HOOKS` (configops): the `schedule_set` hook returns box 13 (new id) / 15 or 16 (only `active` changes)
    / 14 otherwise; `schedule_remove` → 17; `raid_set` / `comp` → 19. `describe_ops` carries the box as
    `Described.box` (JSON `box`) beside its `consequences` lines (which become the box's sentences).
  - **`seen`:** `POST /api/run/{key}/lock`, `/cancel`, `/reschedule`, `/lineup`, `/switch` (Task 4), `/api/raid/{rid}/open`,
    `POST /api/run/{key}/layout` (undo) take `seen` (a box `fp`). When present and the freshly built box's `fp` differs, the
    route writes nothing and answers 409 `{"error": CONFIRM_WORDS["changed"], "box": fresh}`. Without `seen`, unchanged
    (MCP, plain text). A route's `preview: true` answers `{"box": …}`; a refusal answers 400
    `{"error": sentence, "box": refusal_box(...)}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_confirm_boxes.py`; use the suite's `reg`, `rs`, `bot`, `join`,
  `lock_with_board`, `officer_client`, `start_in`, and phase 3's `open_run(..., lineup=)`; `EVENING` =
  `[{"raid": "barrow_deeps", "rosters": 2}]` after `hyjal_summit_forever`)

```python
def test_lock_box_names_the_lineup_and_says_one_off_rules(reg, rs):
    ev = rc.open_run(reg, rs, "hyjal_summit_forever", start_in(reg, 3), lineup=EVENING)
    b = cq.lock_box(reg, ev)
    assert b["title"] == f"Lock {reg.local12(ev.starts_at)}?" and b["verb"] == "Lock" and not b["danger"]
    assert b["sentences"] == ["The board becomes the roster for Hyjal Summit and both Barrow Deeps rosters.",
                              "Nobody is asked to confirm.", "You can still move people after the lock."]


def test_cancel_box_is_red_and_says_the_event(reg, rs):
    ev = rc.open_run(reg, rs, "hyjal_summit_forever", start_in(reg, 3), lineup=EVENING); join(reg, rs, ev, people(reg)[:18])
    b = cq.cancel_box(reg, ev)
    assert b["verb"] == "Cancel event" and b["danger"] and b["sentences"] == [
        "Both raids of this event are cancelled.", "The 18 who joined see it on the sheet, and nobody gets a DM.",
        "A cancelled event cannot be reopened."]


def test_undo_and_leave_words():
    assert cq.undo_box(4)["sentences"] == ["Your 4 moves on this board are dropped.", "Everyone goes back to where the lock would place them."]
    assert cq.leave_unsaved_sentence(2, "Hyjal Summit") == "Your 2 edits to Hyjal Summit are not saved yet."


def test_the_past_refusal_is_one_sentence_with_a_title(reg, rs):
    """reschedule_refusal for a start yesterday returns move_past_sentence(...); the route's 400 body carries
    box == {title: 'That time has passed', sentences: [that sentence], verb: None, ...}."""


def test_a_stale_box_is_refused_with_the_fresh_one(officer_client, reg, rs):
    """GET the event's lock box (fp A); join one more member; POST /api/run/{key}/lock {seen: A} → 409, nothing locked,
    body.box.fp != A and body.box.sentences name the new count; POST again with the new fp → 200 and locked."""


def test_schedule_boxes_read_as_the_mockups(reg, rs):
    """Create (13), Save with a lock change and one open event (14), Pause (15), Resume when the next sheet is due now
    (16), Remove with 6 regulars (17), a regular's removal (18): each box's title, verb, danger and sentences equal the
    table's text for that case."""


def test_raid_save_box_counts_the_open_events(reg, rs):
    """A healer minimum 5 → 4 with one open event: sentences == ['The new minimum applies at the next lock.',
    'That includes the event open now.'] and diff == ['Healers, minimum: 5 → 4']."""


def test_describe_ops_carries_the_box(reg):
    """configops.describe_ops for a schedule pause → Described.box['title'] == 'Pause <name>?' and its sentences equal
    Described.consequences."""
```

  `tests/test_confirm_readability.py`: render every new generator across its cases (each variant named in the table,
  DMs on and off, 0 / 1 / many) through `dump.add` so the lint reads them.
- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_confirm_boxes.py -q`.
- [ ] **Step 3: Implement** the interfaces. The rewritten generators (`_lock`, `_cancel`, `_board` swap, `regular_remove`,
  `action_consequence` join, phase 3's two) are shared with Discord; the Discord screens print the new words too (D18).
- [ ] **Step 4: Run** `uv run pytest tests/test_confirm_boxes.py tests/test_confirm_readability.py tests/test_quiet_edges.py tests/test_rosters_page.py tests/test_raid_night.py tests/test_member_inbox.py tests/test_structured_ops.py -q` → pass
  (update a pinned old sentence in those files to the new one, never the other way).
- [ ] **Step 5: Manual change, commit** — "Confirmations: one box shape with the approved sentences; a stale box is refused
  with the fresh one."

### Task 2: The Events read model and the page words

**Files:**
- Create: `src/oibot_gm/event_view.py`, `src/oibot_gm/web/api_events.py` (installed from `api.py`)
- Modify: `src/oibot_gm/constants.py` (`PAGE_WORDS`), `src/oibot_gm/web/api.py` (`/api/meta.page_words`,
  `/api/meta.confirm_words`, `/api/meta.leave`), `frontend/src/api.ts` (types only)
- Test: `tests/test_events_api.py` (new)

**Interfaces:**
- Consumes: Task 1's boxes and word helpers; phase 3's `rc.occurrences`, `occurrence_stamp`, `event_of_occurrence`,
  `event_parts`, `first_part`, `open_row_refusal(reg, rs, instance, start, sid, opens, now)`, `ev_json`'s `part` / `event`.
- Produces:
  - `constants.PAGE_WORDS` — the value words of the Global Constraints (`one_off`, `first_to_join`, `not_asked`,
    `asked_at_lock`, `no_sheet_yet`, `from` = `"From {when}"`, `open_in_discord`, `no_answer_yet`, `answers_one`,
    `bot_asks` = `"The bot asks people"`, `officer_asks` = `"An officer asks"`, `every_main`, `used_when`, `one_off_rule`,
    `priority` (the five bar labels), `rosters_of` = `"{n} of {size}"`, `placed_of` = `"{n} of {m} joined"`,
    `board_footer`, `show_switches`, `hide_switches`, `raid_short` = `"{raid} is short"`). `/api/meta.page_words` = it.
  - Routes (officers; `GET /api/events` stays the live stream):
    - `GET /api/event-list?history_offset=0` → `{"open": [Row], "coming": [ComingRow], "history": [Row],
      "history_more": bool}`. `Row = {key, title, subtitle, state, chip}`: `key` = part 0's key, `title` = local12 start,
      `subtitle` = the schedule's name for a scheduled event, else `rc.lineup_words` with `, then` joining raids (the
      mockup's `Hyjal Summit, then Barrow Deeps`); history subtitle `"{lineup} · {state words}"`. `open` = live events
      (open AND locked), soonest first; `history` = 20 newest finished / cancelled per page. `ComingRow = {schedule,
      occurrence, title, subtitle}` = each active schedule's next three occurrences (`rc.occurrences`) with no event yet.
      Test runs never listed.
    - `GET /api/event/{key}` (any part's key resolves to part 0) → `EventView` = `{key, title, state, chip, kind
      ("one_off" | "scheduled"), schedule {id, name} | null, details: [Fact], signups {joined, bench, no_thanks,
      no_answer, rows {in, sub, out, none: [{uid, display_name, character, cls, spec}]}}, parts: [PartView],
      activity: [{when, line}] (every part's `log_rows`, newest first), boxes {lock, cancel}, actions {reschedule,
      edit_lineup, lock, cancel, open_now: false}, lineup: [{raid, rosters, starts}], start_iso, timing {EVENT_TIMING
      values}, people (members with no answer, for Answer for someone), sheet_url}`. `Fact = {label, value, href?}`.
      Open: Details = Lineup, Comes from (href `/schedules/<id>`), Priority, Confirmation, Sheet opened, Locks, Signup sheet
      (`href` = the Discord jump link). Locked: + `Confirmed` (marks tally words, `meta.marks`), `Open seats`
      (`rc.fill_state_text`). History: Lineup, Comes from, Rostered (count), Ended (local12 `closed_at`).
      `PartView = {key, raid, raid_name, label ("6:30 PM · Hyjal Summit"), rosters_words, placed_words, short {role: n},
      sheet (ev_json full of that part — BoardView's input), board_state ("ready" | "working" | "failed" | "locked"),
      board_error | null, mine: [names], undo_box | null}` — Task 3 fills the board fields; here `board_state` is
      `"locked"` after the lock and the layout board before it.
    - `GET /api/event/coming/{sid}/{occurrence}` → `{title, chip, schedule {id, name}, details: [Fact] (Lineup, Comes
      from, Priority, Confirmation, Sheet opens, Opening by hand = `From {local12(start − OPEN_HORIZON_DAYS)}`, Locks),
      can_open, why (open_row_refusal's sentence | null), open {start_iso, schedule, occurrence}, box (Task 1 #11) |
      null}`; 404 when the occurrence no longer exists (schedule edited).
  - `event_view.py` holds the pure builders: `event_list(reg, rs, now, offset) -> dict`, `event_json(reg, rs, ev,
    viewer: int) -> dict`, `coming_json(reg, rs, sid, occurrence, now) -> dict`. Routes run them in `asyncio.to_thread`.
  - SSE: no new topic; the pages listen to `run:`, `member`, `config`.

- [ ] **Step 1: Write the failing tests** (`tests/test_events_api.py`)

```python
def test_the_list_has_three_groups_and_no_test_runs(officer_client, reg, rs, bot):
    """One open one-off (Hyjal → BD ×2), a locked run, a weekly schedule with no sheet yet, a finished run, a test run:
    open == [one-off, locked] soonest first; coming has the schedule's next three with schedule + occurrence; history
    has the finished one with subtitle ending ' · finished'; the test run appears nowhere."""


def test_an_open_event_reads_by_its_sections(officer_client, reg, rs):
    ev = rc.open_run(reg, rs, "hyjal_summit_forever", start_in(reg, 3), lineup=EVENING); join(reg, rs, ev, people(reg)[:18])
    j = officer_client.get(f"/api/event/{rc.event_parts(rs, ev)[1].key}").json()
    assert j["key"] == ev.key and [f["label"] for f in j["details"]] == ["Lineup", "Comes from", "Priority", "Confirmation", "Sheet opened", "Locks", "Signup sheet"]
    assert {f["label"]: f["value"] for f in j["details"]}["Comes from"] == "One-off, no schedule"
    assert j["signups"]["joined"] == 18 and [p["label"] for p in j["parts"]][0].endswith("· Hyjal Summit")
    assert j["boxes"]["lock"]["verb"] == "Lock" and j["actions"]["edit_lineup"] is True


def test_a_coming_up_event_says_when_it_opens_by_hand(officer_client, reg):
    """A weekly schedule: GET /api/event/coming/<sid>/<occurrence> → details include 'Sheet opens' and 'Opening by hand'
    = 'From <local12(start − 21 days)>'; can_open False with the refusal sentence when it is too far ahead."""


def test_meta_carries_the_page_words(officer_client):
    m = officer_client.get("/api/meta").json()
    assert m["page_words"]["one_off"] == "One-off, no schedule" and m["confirm_words"]["discard"] == "Discard edits"
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_events_api.py -q`.
- [ ] **Step 3: Implement** the interfaces; add the TypeScript types `EventRow`, `ComingRow`, `EventList`, `EventView`,
  `PartView`, `Fact`, `Box`, `ComingView`, `PageWords` to `api.ts` (no page uses them yet).
- [ ] **Step 4: Run** `uv run pytest tests/test_events_api.py tests/test_rosters_page.py tests/test_parity_routes.py -q` and
  `cd frontend && npx tsc --noEmit` → pass.
- [ ] **Step 5: Commit** — "Events: one read model for the list, an event, a coming-up event and history."

### Task 3: The board as the lock would place it

**Files:**
- Create: `src/oibot_gm/event_board.py`
- Modify: `src/oibot_gm/raidcycle.py` (`RaidEvent.placed_by`, `event_board_preview`, `apply_move_shown`,
  `open_seat_roles`, `_solve_run(earlier=)`), `src/oibot_gm/discord_raid.py` (`board_move` / `_write_board` open branch),
  `src/oibot_gm/event_view.py` (PartView board fields), `src/oibot_gm/web/api.py` (`board_json` open seats)
- Test: `tests/test_event_board.py` (new), `tests/test_event_board_solver.py` (new, `pytestmark = pytest.mark.slow`)
- Modify: `docs/manual.md` (§4: the board before the lock shows the lock's placement; Undo my moves)

**Interfaces:**
- Consumes: phase 3's `event_parts`, `LEAD_SEAT_BONUS` on earlier parts; phase 2's minimums in `_solve_run`; Task 1's
  `undo_box`; Task 2's `PartView`.
- Produces:
  - `RaidEvent.placed_by: dict[str, int] = {}` — signup name → the officer's Discord id who placed it by hand. Written by
    `board_move` before the lock for the mover (and a swapped partner); pruned by `_write_board` to names still in the
    layout; cleared at the lock.
  - `rc._solve_run(..., earlier: set[int] | None = None)` — when given, the uids that get `LEAD_SEAT_BONUS` instead of
    reading earlier parts' `all_rosters` (the preview has no locked earlier part).
  - `rc.event_board_preview(reg, rs, first) -> dict[str, list[RosterResult] | str]` — PURE, on deep copies: parts in
    order, each `_solve_run(copy, time_limit=PREVIEW_TIME_LIMIT_S, whatif=False, earlier=<uids rostered on the previews of
    the parts before>)`; a part's solver failure → its value is the `solver_error_text` sentence (later parts still
    solve, with `earlier` from the parts that worked). A part with no joiner → `board_rosters(reg, ev, [], size)` (no
    solve). Every part's benched = joiners not placed.
  - `rc.open_seat_roles(reg, ev, rosters) -> list[list[str]]` — per roster, each minimum's shortfall as roles (tank
    first, then healer, then the class / spec slots by their role), at most the roster's empty seats.
  - `event_board.fingerprint(reg, rs, first) -> str` — sha1 over each live part's `rc.solve_inputs(ev)`,
    `rc.board_rev(reg, ev)`, `ev.state`, the run dict (`reg.config.team(ev.team)`), `reg.config_rev(f"raids:{instance}")`.
  - `event_board.BoardCache` on the bot (`bot.event_boards`): `{(guild key, event key): (fp, previews, at)}`;
    `async get(bot, reg, rs, first) -> tuple[str, dict | None]` → `("ready", previews)` when the cached fp matches;
    otherwise starts ONE background compute per event (an `asyncio.Lock` per key, `asyncio.to_thread(event_board_preview,
    …)` on a deep copy of the parts) and returns `("working", stale previews or None)`; when the compute ends with the fp
    still current it stores and `publish("run:<first key>")`; a compute whose fp went stale meanwhile is dropped and
    started again once. `shown_layout(reg, ev) -> list[list[str]] | None` = the cached preview's flat groups for that part.
  - `rc.apply_move_shown(reg, ev, shown: list[list[str]], name, to) -> list[list[str]]` — the move applied to the SHOWN
    board (same swap / full-group rules as `apply_move`), returning the new officers' layout = the old `ev.layout` with
    the mover (and a swapped partner) pinned at their new group, every other name left where `ev.layout` had it (or
    unplaced). A drop on `Not placed` before the lock = remove from the layout AND `ev.pins[uid] = "out"`.
  - `RaidMixin.board_move` before the lock reads `self.event_boards.shown_layout(reg, ev)` and uses `apply_move_shown`
    (falls back to `apply_move` when nothing is cached); records `placed_by`. After the lock unchanged.
  - PartView (Task 2) gains: `sheet.board` = `board_json` of the preview (`board_state` `"ready"`), the layout board while
    `"working"`, the layout board + `board_error` when the preview failed; `board.rosters[i].open` = `open_seat_roles`;
    `mine` = names in `placed_by` with the viewer's id; `undo_box` = `undo_box(len(mine))` when `mine`, else null;
    `layout` = the officers' layout (Undo posts `layout` minus `mine` to `POST /api/run/{key}/layout` with `rev`).

- [ ] **Step 1: Write the failing tests** (`tests/test_event_board.py`; monkeypatch `rc._solve_run` with a deterministic
  stub that places joiners in signup order where the test is not about the solve)

```python
def test_an_event_with_nobody_joined_draws_open_seats_and_never_solves(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: calls.append(1))
    ev = rc.open_run(reg, rs, "hyjal_summit_forever", start_in(reg, 3), lineup=EVENING)
    pv = rc.event_board_preview(reg, rs, ev)
    assert calls == [] and all(sum(len(g) for r in v for g in r.groups) == 0 for v in pv.values())
    assert rc.open_seat_roles(reg, ev, pv[ev.key])[0][:2] == ["tank", "tank"]


def test_a_later_part_prefers_the_earlier_previews_rostered(reg, rs):  # slow file: the real solver
    """20 joiners on Hyjal → BD ×2: the BD preview's 20 are the Hyjal preview's 20 whenever the minimums allow."""


def test_a_move_lands_on_the_board_as_it_is_now_when_the_preview_changed(bot, reg, rs):
    """Cache a preview where Ava is in group 2; another officer's move changes the preview so Ava is in group 3; a drop of
    Ava onto group 1 position 0 (sent against the old picture) pins Ava to group 1 in ev.layout, swaps with whoever the
    CURRENT shown group 1 holds at 0, and nobody else enters the layout."""


def test_undo_drops_only_my_moves(bot, reg, rs, officer_client):
    """Officer A moves two names, officer B one: A's event JSON has mine == A's two and undo_box '…2 moves…'; posting
    layout minus mine with rev leaves B's one placement and placed_by == {B's name: B}."""


def test_the_cache_computes_once_per_fingerprint(bot, reg, rs):
    """Two concurrent get() calls start one compute; a join changes the fingerprint and the next get() is 'working'."""


def test_a_failed_part_keeps_the_others(reg, rs, monkeypatch):
    """_solve_run raises for part 1: part 0 has rosters, part 1's value is the sentence, part 2 still solves."""
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_event_board.py -q`.
- [ ] **Step 3: Implement** the interfaces. The lock itself is unchanged: it already solves with the layout as pins, so a
  lock right after a preview places people as the preview showed (the preview and the lock share `_solve_run`).
- [ ] **Step 4: Run** `uv run pytest tests/test_event_board.py tests/test_events_api.py tests/test_rosters_page.py tests/test_raid_night.py -q`
  and `uv run pytest -q -m slow tests/test_event_board_solver.py` → pass.
- [ ] **Step 5: Manual change, commit** — "Board: before the lock it shows where the lock would place everyone; drags pin,
  Undo my moves drops yours."

### Task 4: Role switches for the whole event

**Files:**
- Modify: `src/oibot_gm/raidcycle.py` (`event_switches`), `src/oibot_gm/discord_raid.py` (`set_answer(spec=)`,
  `send_plan(seats=)`), `src/oibot_gm/inbox.py` (`do_member_action(spec=)`), `src/oibot_gm/web/api_events.py`
  (`/switch`), `src/oibot_gm/web/api_me.py` (`/api/admin/action` `spec`), `src/oibot_gm/event_view.py` (`switches`),
  `src/oibot_gm/configops.py` (`PARITY_PENDING`, the answer op's `spec` field), `src/oibot_gm/mcp_server.py`
  (`set_answer(spec=)`, `fill_seats(seats=)`), `tests/test_parity_matrix.py`
- Test: `tests/test_role_switches.py` (new)
- Modify: `docs/manual.md` (§4: role switches; an officer picks the spec someone plays)

**Interfaces:**
- Consumes: `rc.role_plan`, `plan_rev`, `send_plan`, phase 3's `event_parts`, Task 1's `switch_box` / `answer_box`.
- Produces:
  - `RaidMixin.set_answer(reg, rs, ev, m, status, character, by, by_id=None, *, spec: str | None = None)` — `spec` must be
    the character's spec, offspec or `rc.spec_for_role` of one of its flex roles, else `RegistryError(f"{label} doesn't
    play {spec}.")`; the signup's `spec` and `role` follow. Written on part 0 (answers live there).
  - `RaidMixin.send_plan(..., seats: list[int] | None = None)` — send only those seat indexes (with `picks`).
  - `do_member_action(..., spec=None)` passes it to `set_answer`; `action_consequence(..., spec=None)`.
  - `rc.event_switches(reg, rs, first) -> dict` = `{"short": [{"raid": name, "roles": {role: n}}], "rows": [Switch],
    "revs": {part key: plan rev}}`; `Switch = {"uid", "display_name", "role", "from": words, "to": words, "answered": bool,
    "raids": [names], "part": part key, "seat": int, "option": int, "character", "spec", "box": switch_box | answer_box}`.
    Rows come from each live part's `role_plan` (prelock before the lock, fill after it) — every option's ask, merged by
    `(uid, character, spec)` across parts (`raids` = every part it helps, `part`/`seat`/`option` = the first). `from` =
    `"Shadow"` or `"Nyx, Shadow"` when the character changes, `PAGE_WORDS["no_answer_yet"]` without an answer; `to` the
    same shape. Ordered by role (meta order), then plan rank. `short` lists only parts with a short role.
  - `POST /api/run/{key}/switch {uid, character, spec, part, seat, option, rev, seen?, preview?}` (officers live): before
    the lock → `set_answer(status "in", character, spec=spec)` (an unanswered person: the same, the box is
    `answer_box`); after the lock → `send_plan(rev=rev, picks={seat: option}, seats=[seat])` on `part`. `PlanChanged` →
    409 with the fresh `switches`. Preview → `{box}`.
  - `EventView.switches` (Task 2 JSON) = `event_switches`; empty `short` → the section is not drawn.
  - `PARITY_PENDING["run_switch_spec"] = {"discord": "phase 5: the spec select in the SetWizard; until then /gm change
    reaches it as plain text"}`; MCP `set_answer(..., spec: str | None = None)` and `fill_seats(..., seats: list[int] |
    None = None)`; the matrix row `run_switch` → web `POST /api/run/{key}/switch`, mcp `set_answer` + `fill_seats`.

- [ ] **Step 1: Write the failing tests** (`tests/test_role_switches.py`)

```python
def test_switches_list_one_line_per_person_across_raids(reg, rs):
    """Hyjal → BD ×2, joiners short of healers on both, Ilse (Balance, offspec Restoration) joined: one row for Ilse
    with raids == ['Hyjal Summit', 'Barrow Deeps'], from 'Balance', to 'Restoration', role 'healer'."""


def test_switches_with_nobody_joined_are_answers_for_them(reg, rs):
    """No joiner: every row has answered False and from == 'no answer yet', and its box verb is 'Record answer'."""


def test_switch_before_the_lock_changes_the_answer_and_nothing_is_sent(bot, officer_client, reg, rs):
    """POST /switch for Nyx Shadow → Holy: part 0's signup spec Holy, role healer; bot.dms == []; Nyx has a notice."""


def test_switch_after_the_lock_sends_only_that_ask(bot, officer_client, reg, rs):
    """Locked with two short seats: POST /switch for seat 1 option 0 → exactly one FillAsk sent, for that person."""


def test_a_spec_the_character_cannot_play_is_refused(bot, reg, rs):
    with pytest.raises(RegistryError, match="doesn't play Holy"):
        asyncio.run(bot.set_answer(reg, rs, ev, warrior, "in", None, "o", by_id=OFFICER_ID, spec="Holy"))
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_role_switches.py tests/test_parity_matrix.py tests/test_mcp.py tests/test_member_inbox.py tests/test_raid_night.py tests/test_confirm_readability.py -q` → pass.
- [ ] **Step 5: Manual change, commit** — "Role switches: one list per event; Switch changes the answer before the lock and
  sends the ask after it."

### Task 5: The Schedules and Raids read models; regulars by schedule id

**Files:**
- Create: `src/oibot_gm/schedule_view.py`, `src/oibot_gm/raid_page.py`, `src/oibot_gm/web/api_schedules.py`
- Modify: `src/oibot_gm/web/api.py` (`/api/raids` rows gain `page`; `/api/admin/raid/regulars` by `schedule` id),
  `src/oibot_gm/configops.py` (the hooks of Task 1 wired), `frontend/src/api.ts` (types)
- Test: `tests/test_schedules_api.py` (new)

**Interfaces:**
- Consumes: phase 3's `all_schedules`, `schedule_entry`, `schedule_def(instance, sid)`, `schedule_runs_open`, `regulars(sid)`,
  `series_outlook` keyed by id, `rc.next_runs_of`, `occurrences`, `event_of_occurrence`; phase 2's `minimums`,
  `SCHEDULE_TIMING`; Task 1's boxes, `priority_words`, `confirmation_words`, `lineup_words`.
- Produces:
  - `GET /api/schedules` → `{"active": [SRow], "paused": [SRow]}`, `SRow = {id, name, repeats}` (`repeats` =
    `"Every Tuesday at 6:30 PM"`, `"Day 1 of each lockout at 8:00 PM"` — `schedule_view.repeats_words(reg, entry)`).
  - `GET /api/schedules/{id}` → `{id, name, active, chip, details: [Fact], lineup: [{raid, name, rosters, starts,
    time_words, seats}], timing: [Fact] (Sheet opens / Reminder / Locks / Confirm by as `"5 days before"` words through
    `constants.hours_words`, then Open events keeping their own timing = n), next: [{starts, opens, locks, key | null}]
    (empty while paused; a paused schedule's Details shows `Next event if resumed`), regulars {count, words ("0 for 20
    seats"), expected ("Every main" when none, else absent), short {role: n} | null (null when none are regulars),
    outlook ("Short every week" | "Ready every week" | "Short {n} of the next four weeks"), rows [{uid, display_name,
    character, cls, spec, box (Task 1 #18), rev}]}, people [{uid, display_name, characters}], boxes {pause | resume,
    remove}, stored (the entry in machine form for the editor), rev (`config_rev("schedule:<id>")`)}`.
  - `POST /api/admin/raid/regulars {schedule, member | uid, character?, on, rev?}` — `schedule` = the id (phase 3 may
    already take it; if `raid` is still required there, it becomes optional here). Removing with `seen` checks the box.
  - `/api/raids` rows gain `page = raid_page.raid_row(reg, rs, rid)` = `{details: [Fact] (Size "20 players", Groups
    "4 of 5", Lockout "7 days", Length "About 3 hours", First opens local12 | "Open now", Played in = schedule names with
    `href`), make_up: [{slot, label ("Tanks"), min, note}], priority: [{key, label, value}], used_when, one_off_rule,
    open_events (n)}`.
  - Schedule Save / Pause / Resume / Remove and Raid Save go through `POST /api/ops/change` (structured kinds `schedule`,
    `schedule_remove`, `raid`, `comp`) with `review: true` forced for these kinds (`Described.review` true whenever the
    batch touches a schedule or a raid), so the Save always previews its box.

- [ ] **Step 1: Write the failing tests** (`tests/test_schedules_api.py`)

```python
def test_schedules_are_grouped_and_read_in_words(officer_client, reg):
    """weekly 'tue' Tue 18:30 (Hyjal → BD ×2) active, 'ony' lockout day 1 20:00 paused: active == [{id: 'tue', name, repeats:
    'Every Tuesday at 6:30 PM'}], paused == [{id: 'ony', …, repeats: 'Day 1 of each lockout at 8:00 PM'}]."""


def test_a_schedule_reads_by_its_sections(officer_client, reg, rs):
    """GET /api/schedules/tue: Details labels == ['Repeats', 'First event', 'Priority', 'Confirmation',
    'Open seats after the lock', 'Answers']; lineup has two steps with seats 20 and 10 ×2; next has 3 rows; regulars
    words == '0 for 20 seats', expected == 'Every main', short is None."""


def test_a_stale_schedule_save_is_refused(officer_client, reg):
    """Two saves from the same rev of config:schedule:tue: the first applies, the second answers 409 and the schedule
    holds the first one's value."""


def test_a_raid_row_names_where_it_is_played(officer_client, reg):
    """/api/raids row for barrow_deeps: page.details 'Played in' lists 'tue' by name with href '/schedules/tue';
    no row of /api/raids or page carries any SCHEDULE_TIMING key or a slot time."""


def test_schedule_and_raid_saves_always_preview(officer_client, reg):
    """POST /api/ops/change {changes: [a schedule lock change]} without `confirmed` → needs_review with described.box."""
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement**; the types `ScheduleRow`, `ScheduleView`, `RaidPage` in `api.ts`.
- [ ] **Step 4: Run** `uv run pytest tests/test_schedules_api.py tests/test_structured_ops.py tests/test_parity_ops_setup.py tests/test_roster_p1_regulars.py -q` → pass.
- [ ] **Step 5: Commit** — "Schedules and raids: read models in words; schedule and raid saves always show their box."

### Task 6: Shared site pieces and the Raids page

**Files:**
- Create: `frontend/src/components/ConfirmBox.tsx`, `components/LeaveGuard.tsx`, `components/ListPage.tsx`,
  `components/Facts.tsx`, `components/raids/RulesView.tsx`, `components/raids/PriorityView.tsx`,
  `components/raids/ReadinessView.tsx`, `pages/listpage.module.css`, `scripts/serve_scratch.py`
- Modify: `frontend/src/pages/Raids.tsx` (rewritten), `App.tsx` (routes `/raids/:id`, `/readiness` redirect, LeaveGuard),
  `hooks/useDirty.ts`, `components/ReviewModal.tsx` (`useOpsSave` options), `components/Icons.tsx` (`SlotIcon`,
  `RoleIcon` moved in), `scripts/shots.py`
- Delete: `frontend/src/pages/Readiness.tsx`, `frontend/src/components/CompTargets.tsx`
- Test: `tests/test_frontend_source.py`, `tests/test_copy.py` (runs), the bundle
- Modify: `docs/manual.md` (the Raids page: Rules · Priority · Readiness; Readiness is a view of a raid)

**Interfaces:**
- Consumes: Task 1's `Box`, `CONFIRM_WORDS`; Task 2's `/api/meta.page_words` / `confirm_words` / `leave`; Task 5's
  `/api/raids` `page`; `/api/readiness?raid=` (phase 2 shape).
- Produces:
  - `useConfirmBox(): [ask, dialog]`, `ask(o: { box: Box; fields?: ReactNode; act?: (fp: string) => Promise<Box | null>;
    refresh?: () => Promise<Box> }) => Promise<boolean>` — draws title, `diff` lines, `fields`, sentences; buttons `Back`
    then `box.verb` (`color="red"` iff `danger`); a `verb: null` box shows only `Back`. `act(fp)` runs the write and
    resolves `null` when done, or a fresh `Box` (a 409's body) → the box swaps, shows `meta.confirm_words.changed` above
    it and waits again. `refresh` is called when a `useLive` topic the page passes fires while open; a fresh box with a
    different `fp` swaps the same way.
  - `useDirty(key: string, on: boolean, what?: { n: number; name: string })`; `dirtyWhat(): { n: number; name: string } |
    null`; `clearDirty()`.
  - `LeaveGuard` (rendered once in `App`): `guardNav(to: string)` — when `anyDirty()`, opens the box `Leave without
    saving?` with `leave_unsaved_sentence` filled from `meta.leave` (`Keep editing`, `Discard edits` red), else navigates;
    the rail's `NavLink`s and `ListPage` picks call it; `window.onbeforeunload` set while dirty.
  - `ListPage({ title, action?, groups: { label: string; items: { id: string; title: string; subtitle?: string; to:
    string; chip?: string }[] }[], selected: string | null, back: string, children, empty })` — desktop: list column (≤ 290
    px) + detail; below `sm`: the list alone when `selected` is null, else the detail with a `back` link. List items carry
    `data-list-item` (shots.py taps the first on a phone).
  - `Facts({ items: Fact[] })` (label / value grid, `href` → `Link`) and `ViewSection({ title, action?, children })`.
  - `useOpsSave(): [save(changes, { revs, title, verb, always }), review]` — `verb` replaces `Apply N changes`, `Back`
    replaces `Back to edits`, `always` shows the box even when the server would apply; the box prints `described.box`
    when present. Other pages unchanged (they pass neither).
  - Raids page: routes `/raids` (desktop: the first raid; phone: the list), `/raids/:id?view=rules|priority|readiness`
    (`SegmentedControl` `Rules` · `Priority` · `Readiness`; the view in the URL). Rules: `Details` = `page.details`,
    `Group make-up` = a table (`In each roster` / `Minimum`); Priority: the five bars (`Progress` value = weight / max,
    the number beside it), `Used when`, `One-off events`; Readiness: `Who could play` (`Standing` = `<LevelChip>`, `Mains`,
    `Alts`, `Could switch role`), `Against the make-up` (`Have` red when below `Minimum`). `Edit` (Rules and Priority)
    turns that view into its edit mode (Lockout days select, Length select in half hours, First opens = `WhenPicker`
    (Task 7 — until then the existing `DateTimePicker`), a Minimum `NumberInput`-free select 0–size per row and `Add a
    role or class` select, weights selects 0–6); one `Save` → `useOpsSave` with `{always: true, verb: "Save", title:
    "Save {raid}?"}` and `revs: {"config:raids:<id>": rev}`; `useDirty("raid:<id>", editing, {n, name})`. Size is shown,
    never edited (profile data). No raid view shows a time other than `First opens` (anti-goal).
  - `/readiness` → `<Navigate to="/raids?view=readiness" replace />` (`?raid=x` → `/raids/x?view=readiness`).
  - `scripts/serve_scratch.py [--data DIR] [--guild KEY] [--port 8799] [--seed]` — copies `../oibot_gm-data` to the
    scratch dir when `--data` is not given (remotes removed), opens it with `GitStore(push=False)`, builds
    `tests/test_mcp.FakeBot`, sets `OIBOT_MCP_TOKEN` to `test_mcp.TOKEN`, serves `web.create_app(bot)` with uvicorn;
    `--seed` opens a one-off Hyjal Summit → Barrow Deeps ×2 three days out with 18 of the guild's mains joined (the
    suite's `join` path) and a weekly schedule if the guild has none. Never writes the real data repo.
  - `scripts/shots.py`: `PAGES` gains `/app/raids/<first raid>?view=priority` and `?view=readiness`; drops
    `/app/readiness`; a `DETAIL = {"/app/raids", "/app/events", "/app/schedules"}` set: on the phone context, after the
    list shot, tap the first `[data-list-item]` and shoot `<view>-<page>-detail.png`.

- [ ] **Step 1: Write the failing source tests** in `tests/test_frontend_source.py`:

```python
def test_confirm_box_prints_the_servers_box():
    box = _code_only((SRC / "components" / "ConfirmBox.tsx").read_text())
    assert "box.sentences" in box and "box.diff" in box and "box.verb" in box and 'box.danger ? "red"' in box
    assert "confirm_words.changed" in box and not re.search(r">\s*(Yes|No|OK|Cancel|Confirm)\s*<", box)


def test_leaving_asks_first():
    app = _code_only((SRC / "App.tsx").read_text()); guard = _code_only((SRC / "components" / "LeaveGuard.tsx").read_text())
    assert "<LeaveGuard" in app and "guardNav(" in app and "confirm_words.discard" in guard and "beforeunload" in guard


def test_raids_page_is_a_list_with_three_views():
    page = _code_only((SRC / "pages" / "Raids.tsx").read_text())
    assert "<ListPage" in page and "<SegmentedControl" in page and all(v in page for v in ('"rules"', '"priority"', '"readiness"'))
    assert not (SRC / "pages" / "Readiness.tsx").exists() and not (SRC / "components" / "CompTargets.tsx").exists()
    for f in ("Raids.tsx", "raids/RulesView.tsx", "raids/PriorityView.tsx", "raids/ReadinessView.tsx"):
        code = _code_only(((SRC / "pages") if f == "Raids.tsx" else (SRC / "components")).joinpath(f).read_text())
        assert not re.search(r"slot_labels|signup_lead_hours|lock_hours_before|run_times|WhenItRuns", code), f
        assert "useConfirm(" not in code, f"{f}: the §7b box only"
```

  Update `test_the_run_vocabulary_has_no_copy_in_the_frontend`'s Readiness assertion to `<LevelChip` in
  `components/raids/ReadinessView.tsx`.
- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/test_frontend_source.py -q`.
- [ ] **Step 3: Implement,** then `cd frontend && npx tsc --noEmit && npm run build`.
- [ ] **Step 4: Verify** — `uv run pytest tests/test_frontend_source.py tests/test_copy.py -q`,
  `uv run python scripts/check_bundle.py` → pass. Start `uv run python scripts/serve_scratch.py --seed` in the background,
  run `uv run python scripts/shots.py http://127.0.0.1:8799 --guild <key>`, LOOK at `desktop-raids*.png` and
  `phone-raids*.png` (list, detail, each view) and describe them against `Raids.dc.html` / `Readiness.dc.html`.
- [ ] **Step 5: Manual change, commit with the bundle** — "Raids: a list with Rules, Priority and Readiness; Readiness
  moves into it; the §7b box and the leave guard."

### Task 7: The lineup builder and the New event page

**Files:**
- Create: `frontend/src/components/lineup/LineupBuilder.tsx`, `components/lineup/LineupPicture.tsx`,
  `components/WhenPicker.tsx`, `pages/LineupPage.tsx`
- Modify: `frontend/src/App.tsx` (route `/events/new`), `pages/Rosters.tsx` (`StartAdhoc` deleted; a `New event` link
  to `/events/new` in its place), `src/oibot_gm/web/api.py` (the open route and preview: `timing`; preview words),
  `scripts/shots.py` (`/app/events/new`)
- Test: `tests/test_frontend_source.py`, `tests/test_events_api.py` (open with a lineup)
- Modify: `docs/manual.md` (§4: New event and the lineup builder)

**Interfaces:**
- Consumes: phase 3's `POST /api/raid/{rid}/open/preview` / `open` with `lineup` (later entries) and `occurrence`,
  `rc.open_preview`, `before_first_open_sentence`, `Registry.lineup_offsets`; Task 1's `lineup_words`, `priority_words`,
  `confirmation_words`; Task 5's `/api/schedules` (Start from), `/api/raids` (palette: name, size, duration_hours).
- Produces:
  - `type LineupEntry = { raid: string; rosters: number; starts: number }` (minutes after the first step; step 1 = 0).
  - `LineupBuilder({ raids: { id: string; name: string; size: number; hours: number }[]; value: LineupEntry[]; onChange;
    startClock: string | null; fixedFirst?: boolean; max: number })` — the palette (each raid: name, `{size}-player · about
    {h} h`, an `Add` button; greyed when used), the track: steps as columns on desktop / stacked on phone; a step's head =
    `Step {n}`, `Starts` (a `Select` of 15-minute steps from the previous step's start to +12 h, labels `clock12` of
    `startClock` + offset, or `{n} hours after the start` when `startClock` is null), `Rosters` stepper (`−` / `+`,
    aria-labels from the page words, 1–4); N nodes per step (`{Raid}` + `{size} seats`, or `{Raid} · roster {k}`); SVG
    connectors fanned from each node of the step before to each node of the step (vertical on phone); the empty slot
    `Drop a raid here for step {n}` while fewer than `max` steps; `Move earlier` / `Move later` / `Remove` per step (not on
    step 1 when `fixedFirst`). Desktop drag: native HTML5 drag from a palette raid onto a slot or step (the slot takes it),
    a step onto another step (reorder; step 1 stays put when `fixedFirst`). A new step's `starts` = the previous step's +
    the previous raid's length (C12). Pure: it never fetches.
  - `LineupPicture({ lineup: { name, rosters, time_words, seats }[] })` — the builder's picture, read-only (Schedules).
  - `WhenPicker({ day, time, onDay, onTime, minDate, label })` — `DatePickerInput` (format `ddd D MMM`, never `DD`) + a
    time `Select` in 15-minute steps with `clock12` labels; the value pair is sent as guild wall time `'YYYY-MM-DD HH:MM'`.
  - `LineupPage({ meta, mode })`, `mode` = `"new-event"` here (Task 10 adds `"edit-lineup"`, Task 11 `"new-schedule"` /
    `"edit-schedule"`). New event: `Start from` (`Copy a lineup` = each active schedule's name → its lineup, then
    `An empty lineup`; default the first schedule, or empty), `Lineup`, `When` (`Day`, `Starts`, `Locks` = `2 hours
    before` / `24 hours before` → `timing.lock_hours_before`), `What this makes` (the preview's facts: `One signup sheet`,
    `Seats` (`"20, then 10 + 10"`), `Priority`, `Confirmation`, + its `before_open` / `exists` / scheduled line); `Back`
    (→ `/events`, through `guardNav`) and `Open the sheet` (no box: this section is the confirmation) → `POST
    /api/raid/{first raid}/open {start, lineup: later entries, timing, occurrence}` → navigate to `/events/<key>`.
    Every change re-asks `/open/preview` (latest answer wins). `useDirty("lineup:new", touched, {n: changes, name: "New
    event"})`.
  - Server: `/api/raid/{rid}/open` and `/open/preview` take `timing: {lock_hours_before}` (passed to `ensure_run`'s
    cutoffs for a one-off); the preview answers `facts: [Fact]` (the four above) built with Task 1's helpers.
  - A picked time that IS a schedule's occurrence of the same first raid: the preview says so (phase 3's `which_run`), the
    facts show that schedule's lineup and `Comes from`, and Open opens the schedule's event — the builder's own lineup
    is not used (the builder shows the schedule's).

- [ ] **Step 1: Write the failing tests**

```python
def test_the_builder_works_without_a_drag():
    b = _code_only((SRC / "components" / "lineup" / "LineupBuilder.tsx").read_text())
    for needle in ("draggable", "onDrop", '"Add"', "Move earlier", "Move later", '"Remove"', "one roster fewer", "one roster more", "fixedFirst"):
        assert needle in b, needle
    assert "max" in b and 'useMediaQuery("(max-width: 48em)")' in b, "steps stack below sm"


def test_new_event_picks_a_time_never_types_one():
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text()); when = _code_only((SRC / "components" / "WhenPicker.tsx").read_text())
    assert "<WhenPicker" in page and "<DatePickerInput" in when and "clock12" in when and "TextInput" not in when + page
    assert 'useDirty("lineup:new"' in page and "guardNav(" in page and "StartAdhoc" not in (SRC / "pages" / "Rosters.tsx").read_text()
```

  `tests/test_events_api.py::test_open_with_a_lineup_and_a_lock_choice` — preview facts' labels == `["One signup sheet",
  "Seats", "Priority", "Confirmation"]`, Seats value `"20, then 10 + 10"`; open with `timing.lock_hours_before = 24` → the
  run dict's `cutoff_hard_hours == 24`. `test_a_four_step_lineup_opens_on_a_four_raid_profile` (phase 3's widened profile
  fixture from `tests/test_lineups.py`).
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** build.
- [ ] **Step 4: Verify** — `uv run pytest tests/test_frontend_source.py tests/test_copy.py tests/test_events_api.py -q`,
  `check_bundle.py`; shots of `/app/events/new` desktop and phone with a three-step lineup (Hyjal, Barrow Deeps ×2,
  Onyxia's Lair) built through `Add` on the phone; LOOK and describe against `NewEvent.dc.html`.
- [ ] **Step 5: Manual change, commit with the bundle** — "New event: build a lineup by drag or by buttons, pick when,
  open the sheet."

### Task 8: The Events page — list, details, signups, activity and the event's actions

**Files:**
- Create: `frontend/src/pages/Events.tsx`, `components/events/EventDetails.tsx`, `SignupsSection.tsx`,
  `ActivitySection.tsx`, `RescheduleBox.tsx`, `ComingUpView.tsx`, `HistoryView.tsx`
- Modify: `frontend/src/App.tsx` (routes `/events`, `/events/:key`, `/events/coming/:sid/:occurrence`),
  `components/board/MemberMenu.tsx` (`SomeoneElse` takes a character and spec, prints `answer_box`), `scripts/shots.py`
- Test: `tests/test_frontend_source.py`
- Modify: `docs/manual.md` (the Events page)

**Interfaces:**
- Consumes: Task 2's routes and `EventView`; Task 1's boxes and `seen`; Task 6's `ListPage`, `Facts`, `ViewSection`,
  `useConfirmBox`, `WhenPicker`; phase 3's `/reschedule` (`start`, `timing`, `preview`), `/api/run/{key}/lock`, `/cancel`.
- Produces:
  - `EventsPage({ meta })` — `ListPage` with `Open` / `Coming up` / `History` (+ `Show older` paging history) and
    `New event` (→ `/events/new`); desktop `/events` selects the first open event, else the first coming-up one; the
    detail by route. `useLive(load, ["run:", "member", "config"])`, `usePoll(load)`.
  - Open / locked event view, sections in this order: header (title, chip, actions `Reschedule`, `Edit lineup` (open
    only; → `/events/<key>/lineup`, Task 10 — hidden until then), `Lock now` (open)), `Details` (`EventDetails`),
    `Signups` (`SignupsSection`: the four counts; each count a button that unfolds its names with the row ⋮ `MemberMenu`;
    `Answer for someone` → `SomeoneElse`), `Role switches` + `Board` (Task 9 slots), `Activity · {n} lines` (folded
    list of `when` · `line`) with the quiet `Cancel event` at its right.
  - `Lock now` → `ask({box: boxes.lock, act: fp => POST /lock {seen: fp}})`; `Cancel event` → `boxes.cancel`, POST
    `/cancel {seen, notify: false}`. `Reschedule` → `RescheduleBox` = the box with `fields` = `WhenPicker` (`New day`,
    `Starts`) + `Locks` select (open events only; `TIMING_LOCKED` otherwise hides it), each change re-asks `POST
    /reschedule {preview: true}` and swaps the box (a refusal shows the refusal box, only `Back`); `Move` posts with
    `seen`.
  - `ComingUpView` — header `title`, chip `no sheet yet`, `Open the sheet now` (disabled with `why` under it when
    `!can_open`; else `ask({box})` → `POST /api/raid/{first raid}/open {start, schedule, occurrence, seen}` → navigate to
    the new event), `Details`. No Reschedule (see "Impossible or contradictory").
  - `HistoryView` — `Details`, `Board, as it was locked` (read-only `BoardPreview` per part), `Activity`.
  - shots.py `PAGES` gains `/app/events`.

- [ ] **Step 1: Write the failing tests**

```python
def test_events_page_reads_by_its_sections():
    page = _code_only((SRC / "pages" / "Events.tsx").read_text())
    assert "<ListPage" in page and "/api/event-list" in page and all(g in page for g in ('"Open"', '"Coming up"', '"History"'))
    order = [page.index(s) for s in ("<EventDetails", "<SignupsSection", "<ActivitySection")]
    assert order == sorted(order)
    for f in (SRC / "components" / "events").glob("*.tsx"):
        code = _code_only(f.read_text())
        assert "useConfirm(" not in code and not re.search(r"message:\s*[`\"']", code), f.name
        assert not re.search(r"[\"'`](First to join|Not asked|Asked at the lock|One-off, no schedule)[\"'`]", code), f.name


def test_reschedule_picks_never_types():
    box = _code_only((SRC / "components" / "events" / "RescheduleBox.tsx").read_text())
    assert "<WhenPicker" in box and "preview: true" in box and "seen" in box and "datetime-local" not in box
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** build.
- [ ] **Step 4: Verify** — `uv run pytest tests/test_frontend_source.py tests/test_copy.py -q`, `check_bundle.py`; shots of
  `/app/events` (desktop: the seeded event; phone: list then detail), a coming-up event and a history event; LOOK and
  describe against `Main.dc.html`. Rosters still works (`/app/rosters`).
- [ ] **Step 5: Manual change, commit with the bundle** — "Events: a list and one event in sections; lock, cancel,
  reschedule and open now ask with the server's box."

### Task 9: The Events page — the board and role switches

**Files:**
- Create: `frontend/src/components/events/EventBoard.tsx`, `components/events/RoleSwitches.tsx`
- Modify: `frontend/src/components/board/BoardView.tsx` (`variant`, open seats, `useConfirmBox`), `pages/Events.tsx`
- Test: `tests/test_frontend_source.py`

**Interfaces:**
- Consumes: Task 3's PartView board fields and `layout` / `mine` / `undo_box`; Task 4's `switches` and `/switch`;
  Task 6's `useConfirmBox`.
- Produces:
  - `EventBoard({ meta, view: EventView, onReload })` — the raid switch (`SegmentedControl`, `parts[i].label`, wraps on a
    phone; hidden for a one-raid event), facts `Rosters` / `Placed` / `Short` (role icon + number), `BoardView` of the
    picked part with `variant="event"`, the footer sentence (`page_words.board_footer`, open only) and `Undo my moves`
    (shown when `undo_box`) → `ask({box: undo_box, act: fp => POST /api/run/{part}/layout {groups: layout minus mine,
    rev, seen: fp}})`. While `board_state === "working"` the board draws the layout board dimmed with a `Loader`; `failed`
    prints `board_error` above the layout board.
  - `BoardView({ ..., variant?: "event" })` — `variant="event"` hides `Clear`, `Place the rest` and the split control;
    draws each roster's `open` roles as dashed `open seat` slots (role icon + the words) after the placed names; before the
    lock a drag and `Move to…` move at once (no box); after the lock every move asks through `useConfirmBox` with a box
    built from the move's title and the server sentence (`fillNames`). Without `variant` (the Test bench) it behaves as
    today. The `live.dragging` guard stays: no reload re-renders the board mid-drag.
  - `RoleSwitches({ meta, view, onReload })` — drawn only when `switches.short` is non-empty: one fact per short raid
    (`{raid} is short` → role icons + numbers), the toggle `Show {n} possible switches` / `Hide the {n} switches`
    (collapsed by default; open state per viewer in `localStorage`), and one row per switch: role icon, `{Name}: {from} →
    {to}` (the arrow `aria-hidden`, a visually hidden `to`), a chip per raid, and `Switch` / `Answer for them` →
    `ask({box: row.box, act: fp => POST /api/run/{row.part}/switch {…row, rev: switches.revs[row.part], seen: fp}})`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_event_board_replaces_suggest_and_preview_lock():
    board = _code_only((SRC / "components" / "events" / "EventBoard.tsx").read_text())
    view = _code_only((SRC / "components" / "board" / "BoardView.tsx").read_text())
    assert 'variant="event"' in board and "undo_box" in board and "board_footer" in board and "Suggest" not in board and "lock/preview" not in board
    assert 'variant === "event"' in view and "open seat" in view and "live.dragging" in view and "useConfirmBox" in view


def test_role_switches_are_one_line_moves():
    sw = _code_only((SRC / "components" / "events" / "RoleSwitches.tsx").read_text())
    assert "switches.short" in sw and "show_switches" in sw and "hide_switches" in sw and "/switch" in sw and "row.box" in sw
    assert 'aria-hidden' in sw
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** build.
- [ ] **Step 4: Verify** — `uv run pytest tests/test_frontend_source.py tests/test_copy.py -q`, `check_bundle.py`; on the
  scratch server drag two names, switch raids, open the switches, Undo; shots of the event's Board and Role switches
  (desktop and phone); LOOK and describe against `Main.dc.html`; also open the Members page's Test bench and confirm its
  board is unchanged.
- [ ] **Step 5: Commit with the bundle** — "Events: the board always drawn as the lock would place it; role switches as
  one-line moves."

### Task 10: Edit lineup, and Events replaces Rosters

**Files:**
- Modify: `frontend/src/pages/LineupPage.tsx` (`mode="edit-lineup"`), `App.tsx` (route `/events/:key/lineup`; `/rosters`
  → `RostersRedirect`), `components/board/BoardView.tsx` / `SheetCard.tsx` (css import), `components/TestBench.tsx`
  (`navigate("/events/<key>")`), `src/oibot_gm/raid_views.py` (the site link), `src/oibot_gm/help.py` (`PAGES`),
  `src/oibot_gm/web/app.py` (old-URL redirects), `scripts/shots.py`
- Move: `frontend/src/pages/rosters.module.css` → `frontend/src/components/board/board.module.css`
- Delete: `frontend/src/pages/Rosters.tsx`
- Test: `tests/test_frontend_source.py` (Rosters tests rewritten for Events), `tests/test_rosters_page.py` (the deep link),
  `tests/test_events_api.py`
- Modify: `docs/manual.md` (Edit lineup; the Rosters page is now Events)

**Interfaces:**
- Consumes: phase 3's `POST /api/run/{key}/lineup {lineup, preview}`, `lineup_refusal`; Task 1's `lineup_box` and `seen`;
  Task 7's `LineupBuilder`; Task 2's `EventView.lineup` / `start_iso`.
- Produces:
  - `LineupPage mode="edit-lineup"` at `/events/:key/lineup`: sections `Lineup` (`LineupBuilder` with `fixedFirst`,
    seeded from `view.lineup`, `startClock` = the event's start) and `What this makes`; buttons `Back` and `Save` → `ask(
    {box: (POST /lineup {preview: true}).box, act: fp => POST /lineup {lineup: later entries, seen: fp}})` → back to the
    event. `useDirty("lineup:<key>", changed, {n, name: event title})`. A locked event's route shows the refusal box
    (`LINEUP_LOCKED`) and only `Back`.
  - `RostersRedirect`: `/rosters#run-<key>` → `/events/<key>`; `/rosters` → `/events`. `raid_views` site link →
    `{web}/app/events/{first part key}?g={key}`; `help.PAGES`: `"events": ("/app/events", "Events", True)`,
    `"schedules": ("/app/schedules", "Schedules", True)`, `"rosters"` and `"readiness"` removed (a help answer about either
    links Events / Raids); `web/app.py`'s `("/rosters", "/app/rosters")` → `/app/events`.
  - `tests/test_frontend_source.py`: the Rosters-page tests (`test_rosters_page_reads_by_its_shape`,
    `test_the_rosters_page_is_a_card_per_night_in_time_order`, `test_answer_for_someone_offers_only_members_without_an_answer`,
    `test_the_rosters_confirms_print_only_the_servers_sentence`, `test_rosters_controls_are_44px_on_a_phone`,
    `test_the_run_vocabulary_has_no_copy_in_the_frontend`'s file list) are rewritten against `pages/Events.tsx` and
    `components/events/*` with the same intent (server sentences only, the Clock rule, 44 px, answer-for-someone offers
    members without an answer); none is deleted without its replacement.

- [ ] **Step 1: Write the failing tests**

```python
def test_rosters_is_gone_and_redirects_to_events():
    app = _code_only((SRC / "App.tsx").read_text())
    assert not (SRC / "pages" / "Rosters.tsx").exists() and "RostersRedirect" in app and '"/events/:key/lineup"' in app


def test_edit_lineup_keeps_the_first_raid():
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert 'mode === "edit-lineup"' in page and "fixedFirst" in page and "/lineup" in page and "preview: true" in page
```

  `tests/test_rosters_page.py::test_a_card_links_the_event_page`: `raid_views` site link for a later part ends with
  `/app/events/<part 0 key>?g=<key>`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** build.
- [ ] **Step 4: Verify** — `uv run pytest tests/test_frontend_source.py tests/test_copy.py tests/test_rosters_page.py tests/test_events_api.py tests/test_help.py -q`,
  `check_bundle.py`; shots: `/app/rosters` lands on Events, Edit lineup desktop and phone; LOOK and describe.
- [ ] **Step 5: Manual change, commit with the bundle** — "Events replaces Rosters; Edit lineup on the builder."

### Task 11: The Schedules page and the schedule editor

**Files:**
- Create: `frontend/src/pages/Schedules.tsx`, `components/schedules/ScheduleDetails.tsx`,
  `components/schedules/RegularsSection.tsx`
- Modify: `frontend/src/pages/LineupPage.tsx` (`"new-schedule"`, `"edit-schedule"`), `App.tsx` (routes `/schedules`,
  `/schedules/:id`, `/schedules/new`, `/schedules/:id/edit`), `components/RaidCard.tsx` (unused exports removed),
  `scripts/shots.py`
- Delete: `frontend/src/components/ScheduleCard.tsx`, `components/RaidHeader.tsx` (when unused)
- Test: `tests/test_frontend_source.py`
- Modify: `docs/manual.md` (the Schedules page; officers edit schedules on the site)

**Interfaces:**
- Consumes: Task 5's `/api/schedules`, `/api/schedules/{id}`, regulars route; Task 1's boxes 13–18; Task 6's `useOpsSave`
  options, `ListPage`, `Facts`, `useConfirmBox`; Task 7's `LineupBuilder`, `LineupPicture`; phase 3's structured kinds
  `schedule` / `schedule_remove` by id, `revs` `config:schedule:<id>`; `SlotEditor`.
- Produces:
  - `SchedulesPage({ meta })` — `ListPage` (`Active` / `Paused`, `New schedule` → `/schedules/new`); detail: header (name,
    chip, `Edit` → `/schedules/<id>/edit`, `Pause` / `Resume` → `ask({box: boxes.pause|resume, act})` posting the `schedule`
    change `{active}` through `useOpsSave`'s token path, `Remove` → `boxes.remove` (red) then `schedule_remove`),
    `Details`, `Lineup` (`LineupPicture`), `Sheet timing`, `Next events` (a table; a row with `key` links to
    `/events/<key>`), `Regulars` (`RegularsSection`: the facts; `Add a member` = a searchable `Select` over `people` →
    `Add` adds at once, no box; each name has its `Remove` → `row.box` → POST regulars `{on: false, rev, seen}`).
    The empty Regulars shows no shortfall chip (spec §7).
  - `LineupPage mode="new-schedule" | "edit-schedule"`: sections `Start from` (new only: `Copy a lineup`), `Details`
    (`Name`), `Lineup` (`LineupBuilder`, `startClock` = the repeat's time), `Repeats` (in place of `When`: `Weekly` with
    `SlotEditor` days + times, or `Day N of each lockout` with a day select and a time select — never typed), `Sheet timing`
    (the four hours as preset selects + switches `Reminder`, `The bot asks people for open seats`, `DM the regulars when
    the sheet opens`, `Ask the rostered to confirm`), `Priority` (the five weights as selects 0–6, `Use the raid's`
    clears them); one `Save` → `useOpsSave([{kind: "schedule", target: id, fields}], {always: true, verb: "Create
    schedule" | "Save", title, revs: {"config:schedule:<id>": rev}})`; a 409 keeps the draft, shows the server's sentence and
    a `Reload` button that drops the draft after `Leave without saving?`. `useDirty("schedule:<id>", changed, {n, name})`.
  - shots.py `PAGES` gains `/app/schedules`, `/app/schedules/new`.

- [ ] **Step 1: Write the failing tests**

```python
def test_schedules_page_reads_by_its_sections():
    page = _code_only((SRC / "pages" / "Schedules.tsx").read_text())
    assert "<ListPage" in page and '"Active"' in page and '"Paused"' in page and "<LineupPicture" in page and "<RegularsSection" in page
    assert not (SRC / "components" / "ScheduleCard.tsx").exists()


def test_the_schedule_editor_is_the_builder_with_repeats():
    page = _code_only((SRC / "pages" / "LineupPage.tsx").read_text())
    assert '"edit-schedule"' in page and "<SlotEditor" in page and "always: true" in page and "config:schedule:" in page
    assert 'useDirty(`schedule:' in page and "status === 409" in page
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** build.
- [ ] **Step 4: Verify** — `uv run pytest tests/test_frontend_source.py tests/test_copy.py tests/test_schedules_api.py -q`,
  `check_bundle.py`; on the scratch server open the same schedule's editor in two tabs and save both (the second shows
  the 409 sentence, keeps its draft); shots of `/app/schedules` (active and paused, desktop and phone) and the editor;
  LOOK and describe against `Schedules.dc.html`.
- [ ] **Step 5: Manual change, commit with the bundle** — "Schedules: their own page; the editor is the lineup builder
  with Repeats; one Save."

### Task 12: The rail, the docs, the full check and the final look

**Files:**
- Modify: `frontend/src/App.tsx` (`OFFICER` rail), `docs/design.md` (§5.67 "Phase 4 built"; §5.54 and §5.56 get a
  pointer), `CLAUDE.md` (the frontend paragraph and the server entries this plan added), `docs/manual.md` (the officers'
  page list), `scripts/shots.py` (final `PAGES`)

**Interfaces:**
- Produces: the officers' rail = `Events` (`/events`, `IconCalendarEvent`), `Schedules` (`/schedules`, `IconRepeat`),
  `Raids` (`/raids`, `IconMountain`), then Members, Applications, Loot, Auras, Ops, Policy, Agents, Config (the mockup's
  order; `Readiness` and `Rosters` gone). `officerPage` (Tell the bot) covers the new paths.

- [ ] **Step 1: Change the rail,** build, `uv run pytest tests/test_frontend_source.py tests/test_copy.py -q`,
  `check_bundle.py`.
- [ ] **Step 2: Write §5.67's phase 4 part** (the read models, the board cache and fingerprint, `placed_by` and Undo, the
  switches, the Box and `seen`, the routes, the pages, the calls below), the pointers, `CLAUDE.md`'s frontend paragraph
  (pages Events · New event · Schedules · Raids, the components named here, `ConfirmBox` replacing `useConfirm` on these
  pages, `LeaveGuard`, `ListPage`) and server entries (`event_view.py`, `event_board.py`, `schedule_view.py`,
  `raid_page.py`, `api_events.py`, `api_schedules.py`, `serve_scratch.py`), and the manual's page list. Only what is built.
- [ ] **Step 3: Run the full check** in the background — `uv run python scripts/check_commands.py > <scratch>/check.log 2>&1; echo "exit=$?" >> <scratch>/check.log`
  (about 20 minutes; no edits while it runs). Then `uv run pytest -q -m slow tests/test_event_board_solver.py tests/test_lineup_solver.py`.
  Expected: `exit=0` and the slow tests pass.
- [ ] **Step 4: Final look.** `serve_scratch.py --seed`, `shots.py` for every page; put each of the four pages' desktop and
  phone shots beside its mockup and list every difference (layout, label, order, missing or extra control) in the task
  report, each marked "intended (a call below)" or "to fix"; fix the "to fix" ones in this task and shoot again.
- [ ] **Step 5: Commit with the bundle** — "Rail: Events, Schedules, Raids; design §5.67 phase 4 and the agent guide."

---

## Calls made in this plan

- **`GET /api/events` is the live stream, so the list is `/api/event-list` and one event `/api/event/{key}`.** If wrong: a
  rename of two routes.
- **The `Open` group holds open AND locked events** (the mockup has only an open one); the chip tells them apart. If wrong:
  a fourth group.
- **New and rewritten confirm sentences use the mockups' words, "event" included,** and the Discord screens sharing those
  generators print them a phase early; untouched generators keep "run" until phase 6. If wrong: one word swap per sentence.
- **Box titles are built on the server with the box** (not composed by the page). If wrong: nothing breaks; titles move.
- **A stale box is caught by a fingerprint of its words (`seen`)**, not a data rev: any change a person would read makes it
  wait again; a change they wouldn't read does not. If wrong: a fact that changes nothing in the sentences slips through.
- **"Undo my moves" is per officer** (`RaidEvent.placed_by`), as the box says "Your 4 moves"; a whole-board clear stays for
  MCP and plain text. If wrong: Undo becomes Clear and the box's words change.
- **A drag before the lock pins only what was moved** (the rest follow the lock's solve); a drop on `Not placed` pins them
  out. If wrong: officers who expect a drag to freeze the whole board see others move after a join.
- **The preview runs in the background and the page reloads on `run:<key>`** when it is ready; the first view after a
  change shows the officers' layout dimmed with a loader. If wrong: a blocking 2–5 s request instead.
- **`Move to…` before the lock moves at once, like a drag** (§7b lists only a drag; the menu asked before). If wrong: one
  condition in `BoardView.place`.
- **The split strategy control leaves the event board**; the strategy still comes from the schedule / raid, and MCP / plain
  text keep `set_strategy`. If wrong: a select in the Board section.
- **Signups counts unfold to names with the row menu** (the mockup shows counts only); without it an answer could not be
  changed from the page. If wrong: the menu moves elsewhere.
- **A locked event adds `Confirmed` and `Open seats` to Details and keeps Fill seats / Remind / Pause / End in the header's
  `⋮`**, using the existing sentences; the mockups show no locked event. If wrong: a section of its own.
- **Switch before the lock writes the spec on the answer** (`set_answer(spec=)`) for the whole evening (D5); its box names
  the raids it helps. If wrong: per-raid answers, which §8 excludes.
- **Switch after the lock sends only that one ask** (`send_plan(seats=[i])`), not the plan's whole batch. If wrong: one
  more parameter dropped.
- **Coming-up events have no Reschedule** (no event exists to move). If wrong: a new "move an occurrence" verb.
- **"Opening by hand: From …" = the start minus `OPEN_HORIZON_DAYS`** (today's open-early limit). If wrong: one field.
- **New event at a time that is a schedule's occurrence opens that schedule's event with its lineup** (today's
  `which_run`), and says so in What this makes. If wrong: officers expecting a one-off get the scheduled event.
- **New event's `Locks` offers the mockup's two values only**; other timing waits for Reschedule. If wrong: a longer select.
- **Builder starts are 15-minute steps** from the previous step to +12 h, shown as clock times (or "N hours after the start"
  for a schedule with several slot times). If wrong: a finer select.
- **Edit on a raid edits the view shown** (Rules: details and minimums; Priority: weights); Readiness has no Edit. Size is
  shown, never edited. If wrong: one edit mode for all.
- **Schedule and raid Saves always show their box** (`Described.review` forced), as §7b asks; other pages' Saves keep
  applying routine edits at once. If wrong: one flag.
- **`ReviewModal` gains options instead of being replaced**; the Config, Auras and other pages keep `Apply N changes` until
  phase 6. If wrong: those pages break §7b for one more phase.
- **Phone = list-then-detail with URLs**, not a top picker. If wrong: `ListPage` changes, pages don't.
- **Screenshots come from `serve_scratch.py`** (this worktree's app, a scratch data copy, the suite's FakeBot), never the
  live bot. If wrong: shots show fake Discord bits (no channel names).
- **The empty seat shows the role icon with the words `open seat`** (the mockup); the words name the seat, not the icon.
- **Schedule removal's second sentence says the open event keeps its sheet and answers** instead of the mockup's "stays as a
  one-off" (see below).

## Impossible or contradictory

- **A coming-up event cannot be rescheduled:** the mockup gives it `Reschedule`, but no event file exists before its sheet
  opens and phase 3's reschedule works on events. Left out; a skip-or-move-an-occurrence verb would be new scope.
- **"The event already open stays as a one-off" (Remove box) is not what the code does:** the open event keeps its run dict,
  so its confirmations and weights stay; only its regulars go. The sentence is written to what happens.
- **Four raids cannot be shown on `forever`:** it has three raids and a raid cannot repeat (D10), so the 4-step case is
  proved on a widened test profile and a source assertion, and the phone shot uses three steps.
- **D16 says officers edit a raid's size, the mockup shows Size beside Edit,** but size is profile data with no editor
  (phase 2 noted it). Shown, not edited.
- **§7a puts Edit lineup on the event header while §6 / phase 3 refuse it after the lock:** the button is hidden on a locked
  event and its route shows the refusal box.
- **The mockup's Reschedule box shows `Locks` for every event, but phase 3 refuses timing after the lock (`TIMING_LOCKED`):**
  the select is shown only on an open event.
- **§7b's "a drag before the lock does not ask" vs the existing Move-to-asks rule** — resolved above as a call.
- **Copy rule vs mockup:** none of the approved words hits `tests/test_copy.py`'s list (checked: no "pool", "seated",
  "series", "targets", "bounds"); `Cancel event` and `Priority` are new words, not banned ones, so the lint passes without
  an `ALLOW` entry.
