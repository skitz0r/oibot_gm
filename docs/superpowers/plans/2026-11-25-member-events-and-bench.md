# Members' event view, and Bench as the last to fill — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A member opens Events in the rail and sees each live event the way the Discord sheet shows it (signups by
class, the locked roster) with their own answer buttons, and nothing an officer-only surface shows; and the lock seats
people who answered **Bench** only where no joiner can take the seat.

**Architecture:** Part A adds a separate member-safe read model, `member_event_view.py`. It is built from the same
readers `raid_views.sheet_message` reads, not stripped from `event_view.event_json`. It is served by two member routes in
a new `web/api_member_events.py` and drawn by a new `pages/MemberEvents.tsx` on the existing `ListPage`. The page reuses
three components: the Me page's answer row, extracted once into `MyAnswer.tsx`; the officers' signup tabs, extracted into
`SignupTabs.tsx`; and a new read-only `MemberRoster.tsx`. Part B makes "last" a cost tier inside the one CP-SAT model:
every Bench seat costs more than anything a seat can earn, and a refund applies only to a Bench seat that fills a tank or
healer minimum the joiners cannot meet. A first-to-join event lines Bench answers up after every joiner, in answer order.
The board keeps Bench names it or Auto-fill places, and shows them in a second bank section.

**Tech Stack:** Python 3.12, OR-tools CP-SAT, FastAPI, React 19 + Mantine 9 (Vite), pytest (+ xdist).

**Spec:** `docs/superpowers/specs/2026-11-25-member-events-and-bench-design.md` (approved 2026-11-25). Read `CLAUDE.md`'s
Principles first, then only the layout areas you touch: `docs/layout/events.md` (event_view, event_board, raid_views,
raidcycle), `docs/layout/web.md` (api_events, api_me), `docs/layout/frontend.md`, `docs/layout/discord.md` (only for
the sheet footer). Implementers follow `.superpowers/common/implementer-rules.md`, last block "Effort 1 follow-up".

**Shipping point:** Part A (Tasks 1–3) is deployable on its own. Task 3 ends with the member page live, the manual
updated and screenshots taken. Nothing in Part A depends on Part B.

## What this plan found in the code (it differs from the design's §1)

- **The lock can already seat a Bench answer.** `rc.players_for` (`raidcycle.py:3934-3964`) hands every non-"out"
  signup to the solver, and a Bench answer arrives as `Player.status = "bench"`. `solver._seat_value` (`roster/solver.py:401-408`)
  gives a joiner `+signed_bonus` (30) and a Bench answer `-bench_penalty` (15) (`profiles/*/comp_rules.yaml`).
  `_add_seat_and_group_constraints` (`solver.py:302-316`) fills exactly `min(target × rosters, seatable)` seats, and
  `seatable` counts Bench answers. So today an empty seat already goes to the bench. A 45-point gap is also small enough
  that bonuses, synergy or a class minimum can put a Bench answer in front of a joiner. `explain.annotate` even prints
  "In from the bench" (`roster/explain.py:81-84`), and `event_view._part_view` notes "the lock may seat someone from the Bench".
  **What Part B actually changes:** Bench becomes strictly last. No joiner is displaced except for a tank or healer
  minimum. A first-to-join event puts Bench in answer order. The board shows and keeps Bench names, and the words say it.
- `rc.how_many_rosters` (`raidcycle.py:4123-4136`) already counts only joiners (`status == "signed"`). Bench never raises the
  number of rosters, and this plan keeps that and pins it with a test.
- Before the lock, three places drop a Bench name from the board. `rc.prune_board` (`raidcycle.py:4631-4664`) does,
  `rc.apply_move` refuses one (`raidcycle.py:4725-4731`), and `rc.autofill` filters the solve's Bench seats out
  (`raidcycle.py:4946-4950`). `RaidMixin._write_board` keeps only `status == "in"` names (`discord_raid.py:1188`), even on
  a LOCKED board. A Bench answer the lock seated would be dropped by an officer's next drag. Task 5 fixes all four
  through one predicate.
- The site's Signups tab labels and empty sentences are typed in `SignupsSection.tsx` (`TABS`). Task 2 moves them to
  `PAGE_WORDS["signup_tabs"]`, so both pages read the server's words.

## Decisions this plan makes (the owner may disagree)

- **D1. The members' Coming up** reads `event_view.coming_nights` (the officers' Coming up, the same nights the public
  schedule names), not `web/app.py`'s `schedule_sections`. A coming row is plain text, not a link: there is no member
  page for a night without a sheet. `ListItem.to` becomes optional for that.
- **D2. A key the member cannot see** gets 404 with one new sentence, `PAGE_WORDS["event_over_member"]` = "That event has
  finished or was cancelled, so it is not on the Events list." This covers finished, cancelled, unknown, dropped, and
  another tester's rehearsal (a non-tester learns nothing about a rehearsal). The Me page's `me_event_over` ends "so My
  sheets has no row for it", which is false on this page, so it stays on the Me page only.
- **D3. The rail.** `NAV` gains Events (after Me) for a viewer who is not an officer. An officer keeps only the Officers
  section's Events, so the rail never shows two.
- **D4. The lock box's bench count goes in the head sentence**, not in the "who gets the DM" sentence. Every lock box has
  the head (an event without confirmations has no DM sentence), and the DMs-off variant is already 18 words. The
  DM sentence's count includes the Bench seats, because those people get the DM too.
- **D5. The Discord sheet's Bench row is unchanged.** It lists every Bench answer, rostered or not, as the member page's
  Bench tab does, so the page and the sheet never disagree. The roster says who plays.
- **D6. Bench seats the lock will take (the box's count)** come from a pure prediction, `rc.bench_seats`, made without a
  solve, because boxes never solve. Tests pin that it equals the lock's count in the plan's cases.
- **D7. Screenshots as a member:** `scripts/serve_scratch.py --member` writes a throw-away session cookie for a seeded
  member who is not the owner, and `scripts/shots.py --member-cookie FILE` shoots the member pages with it. Both are for
  the scratch server only.

## Global Constraints

- **Copy rule:** every word says what it does for the reader, in `docs/manual.md` §1a's words. Say "event", never "run".
  Say "rostered", never "seated". An empty list says what is missing and how it fills ("Nobody has joined yet.").
  `tests/test_copy.py` (retired words and the manual lint) and `tests/test_quiet_edges.py` stay green.
- **Words from the server:** a .tsx file never types a word the server has (`PAGE_WORDS`, `rc.BANK_WORDS`, `RUN_WORDS`,
  the sheet's words). The officers' page's existing typed section titles ("Details", "Signups") are the precedent for
  the new section titles "Your answer" and "Roster".
- **Confirm rule:** the changed lock box sentences have at most three sentences, each one idea, a capital letter and a
  period, and twenty words or fewer, with no parentheses, semicolons or dash-glued fragments.
  `tests/test_confirm_readability.py` renders the new cases.
- **Clock rule:** times a person reads come from `Registry.local12` (on the site, `t12`). The site never shows ISO or
  24-hour times.
- **Iconography rule:** a member line is the spec icon alone, then the name. Role counts are the role icon and a number.
  No "3 tank" labels, and no word beside a class, spec, role or aura icon.
- **DM rule:** only a Bench answer the lock rosters gets something new, the lock's Confirm ask (an act). Nobody else is
  told anything new.
- **Code computes, Claude explains:** the solver decides seats. Words describe what code decided.
- **Member-safe:** the member routes' JSON never carries an officer-only fact (Task 1's no-leak tests). The officers'
  routes are untouched.
- **No real guild data:** use test fixtures only. Screenshots come only from `scripts/serve_scratch.py` with a fresh
  `--data` folder, never the live bot.
- **The solver stays repeatable:** a deterministic model and the same coefficients for the same input. A pool with no
  seatable Bench answer builds exactly today's model (no new variable, constraint or term).
  `tests/test_solver_repeatable.py` and `tests/test_solver_kept_answer.py` stay green.
- **The after-lock fill engine does not change:** `rc.role_plan`, `fill_batch` and `expire_confirmations` keep their
  order, with the bench asked first.
- **Frontend:** after any change under `frontend/`, run `cd frontend && npx tsc --noEmit && npm run build`, then
  `uv run python scripts/check_bundle.py`, and commit the bundle.
- **Docs:** `docs/manual.md` and the `docs/layout/*.md` entries change in the same commit as the behaviour they describe.

## Review Focus

1. **A member who is also on another guild's server, or an officer viewing the member routes.** Both must see only
   their own guild, and the officer gets no officer extras from the member routes. Task 1:
   `test_an_officer_gets_the_member_shape_from_the_member_routes`.
2. **A later raid's key, or a dropped raid's key, opened from a Discord link.** A later raid's key answers as its event,
   with `key` set to the first raid's. A dropped raid's key is 404. Task 1: `test_a_later_raids_key_answers_as_its_event`.
3. **Bench answers when the event's seats are all held by hand placements.** Nobody hand-placed is moved, and no Bench
   answer is rostered past the seats. Task 4: `test_hand_placed_joiners_fill_every_seat_and_the_bench_stays_off`.
4. **A Bench answer who leaves the bench (Bench → No thanks) after Auto-fill placed them.** They leave the board in the
   next save, and the lock never seats them. Task 5: `test_a_placed_bench_name_whose_answer_becomes_no_thanks_leaves_the_board`.
5. **An officer's drag on a LOCKED board that holds a Bench answer the lock rostered.** The seat is kept. Task 5:
   `test_a_locked_board_edit_keeps_a_rostered_bench_answer`.

---

## Part A — the members' event view

### Task 1: The member read model, its routes, and the no-leak tests

**Files:**
- Create: `src/oibot_gm/member_event_view.py`
- Create: `src/oibot_gm/web/api_member_events.py`
- Modify: `src/oibot_gm/web/api.py:639-670` (the `/api/me` row builder moves out), `src/oibot_gm/web/api.py:2921-2951`
  (install the new routes)
- Modify: `src/oibot_gm/web/api_me.py` (gains `me_sheet_row`)
- Modify: `src/oibot_gm/constants.py` (`PAGE_WORDS` keys below)
- Modify: `docs/layout/events.md` (an entry for member_event_view.py after event_view.py), `docs/layout/web.md` (an
  entry for api_member_events.py, and api_me.py's `me_sheet_row`)
- Test: `tests/test_member_events.py` (new)

**Interfaces:**
- Consumes:
  - `event_view.coming_nights(reg, rs, now, find=None, sid=None)` (`event_view.py:33`), `event_view._state(parts)`
    (`:55`), `event_view._lineup(reg, rs, first, parts)` (`:76`), `event_view._raids_words` (`:85`), `event_view._fact`
    (`:90`), `event_view._sheet_url` (`:94`), `event_view.tz_words` (`:134`)
  - `rc._first_part(e)`, `rc.first_part(rs, ev)`, `rc.event_parts(rs, first)`, `rc.rehearsal_run(reg, ev)`
    (`raidcycle.py:2568`); the tester is `(reg.config.team(ev.team) or {}).get("test_by")` (`raidcycle.py:2576`)
  - `raid_views.split_away(reg, ev, outs)` (`raid_views.py:91`), `raid_views.OUT_MARK` (`:87`),
    `raid_views.run_times(reg, ev, team)` (`:78`)
  - `rc.confirmations(reg, ev)`, `rc.run_confirms(reg, ev)`, `rc.priority_words(reg, first)`,
    `rc.lineup_words(reg, lineup)`, `rc.answer_word(ev, status, roster)` (`raidcycle.py:5588`), `rc.RUN_STATE_WORDS`,
    `inbox.asks_of(reg, rs, uid)`
  - The sheet's locked rows, exactly as `raid_views.sheet_message` reads them (`raid_views.py:180-212`): `ev.all_rosters`,
    each roster's `groups` and `selected`, the marks only when `state == "locked"` and `rc.run_confirms`, and
    "Not rostered" = `rosters[0].benched` ∩ the joiners
- Produces:
  - `member_event_view.visible(reg, e, viewer: int) -> bool`: a first raid (`rc._first_part`), not `dropped`, and either
    not a rehearsal or a rehearsal whose `test_by == viewer`.
  - `member_event_view.member_event_list(reg, rs, now, viewer: int) -> dict` returns
    `{open: [MemberRow], coming: [{title, subtitle}], tz_words, empty}`.
    - `MemberRow = {key, title, subtitle, state, chip, answer}`.
    - `answer` = `rc.answer_word` on the first raid, for the viewer's signup and seat.
    - `empty` = `PAGE_WORDS["member_events_empty"]`.
    - Live events only, soonest first. There is no History.
  - `member_event_view.member_event_json(reg, rs, ev, viewer: int) -> dict` raises `member_event_view.NotListed` (a
    `LookupError`) for anything `visible` refuses, or a finished or cancelled event. It returns
    `{key (first raid's), title, subtitle, state, chip, test_chip, details: [Fact], signups: {counts: {in, sub, out, away}, rows: {in, sub, out, away: [MemberPerson]}}, rosters: [MemberPart], part_keys: [str]}`.
    - `MemberPerson = {display_name, character, cls, spec, callout: bool}`. `callout` = `OUT_MARK` applies (a called-out
      No thanks). The rows come in the order of `ev.by_status`.
    - `away` / `out` = `split_away` of the first raid's "out" rows.
    - `MemberPart = {key, raid_name, label, rosters: [{n, groups: [[MemberSeat]]}], not_rostered: [display_name], marks: bool}`.
      There is one per LOCKED or done raid of the event. Before the lock, `rosters == []`.
    - `MemberSeat = {display_name, character, cls, spec, role, answer}`. `answer` is the confirmation answer ("yes" / "no"
      / "expired" / null) only when `marks`, else absent.
    - `details`, in this order, only these labels: Lineup, Priority, Locks, Confirm by (only when `rc.run_confirms`),
      Signup sheet (the link, or `PAGE_WORDS["sheet_not_posted"]`).
  - `api_me.me_sheet_row(reg, rs, ev, m, uid: int) -> dict` is the `/api/me` `sheets[]` row (`web/api.py:645-656`
    plus `sheet_row_extras`), moved out unchanged. `/api/me` calls it, and its output stays byte-identical.
  - Routes (`web/api_member_events.py`, `install_member_events_api(app, bot, *, who)`), for any member of the selected
    guild (`who(request)`, no `officer=True`). Each runs in `asyncio.to_thread`.
    - `GET /api/me/events` returns `member_event_list`.
    - `GET /api/me/event/{key}` returns `member_event_json`, plus three things the route adds:
      - `answers: [me_sheet_row]`, one per live raid in `part_keys`, the first raid first;
      - `asks`, which is `inbox.asks_of(...)` filtered to `run in part_keys`;
      - `characters: [{label, is_main}]`, the viewer's active characters, the main first.
    - `NotListed` → 404 `{"error": PAGE_WORDS["event_over_member"]}`.
  - New `PAGE_WORDS` keys:
    - `"event_over_member"`: "That event has finished or was cancelled, so it is not on the Events list."
    - `"member_events_empty"`: "No event is open for signup. An event shows here once its sheet opens in Discord."
    - `"called_out"`: "Called out after answering"
    - `"answer_needs_character"`: "Register a character on the Me page to answer."

- [ ] **Step 1: Write the failing tests** in `tests/test_member_events.py`.
  - Fixture `member_client(reg, rs, monkeypatch)` is a `TestClient(web.create_app(NightBot(reg, rs)))` with
    `OIBOT_WEB_SECRET` set. It mirrors `tests/test_follow_on.py:785-793` and uses `as_user` from
    `tests/test_loot_web.py:94`.
  - `leak_fixture(reg, rs)` builds one locked event and one open event. Between them they hold every officer-only fact
    the tests check: a member with no answer, an away member whose absence reason is "dentist-XYZ", a callout whose note
    is "note-XYZ", an "in" pin and an "out" pin, a pending fill ask to another member, `placed_by` / `auto_placed` /
    `layout` on the open event, a distinctive `ev.log` line "log-XYZ", and a short role (so Role switches has a row).
  - Helpers: `keys_of(obj) -> set[str]` (every dict key, recursively) and `dump(j) = json.dumps(j)`.

  ```python
  def test_member_routes_leave_out_who_has_not_answered(member_client, reg, rs): ...
      # the unanswered member's display_name not in dump(GET /api/me/event/{key}); "none", "no_answer", "people" not in keys_of
  def test_member_routes_leave_out_absence_reasons(...): ...     # "dentist-XYZ" not in dump; the name IS in signups.rows.away
  def test_member_routes_leave_out_callout_notes(...): ...       # "note-XYZ" not in dump; "note" not in keys_of; callout is True on the row
  def test_member_routes_leave_out_pins(...): ...                # "pin", "pins", "kept_off" not in keys_of; "Pinned", "Kept off", "kept off" not in dump
  def test_member_routes_leave_out_fill_asks(...): ...           # "fill_asks" not in keys_of; for a viewer who is not the ask's target, `asks == []`
  def test_member_routes_leave_out_the_officers_board(...): ...  # "layout", "placed_by", "auto_placed", "mine", "undo_box", "autofill" not in keys_of; open event: rosters == []
  def test_member_routes_leave_out_the_activity_log(...): ...    # "activity" not in keys_of; "log-XYZ" not in dump
  def test_member_routes_leave_out_role_switches(...): ...       # "switches" not in keys_of
  def test_member_routes_leave_out_priority_scores(...): ...     # "bonus", "bench_whatif", "advisories", "synergy", "objective", "weights" not in keys_of; "Closest to a seat" not in dump
  def test_member_details_are_the_public_facts_only(...): ...    # [f["label"] for f in details] == ["Lineup", "Priority", "Locks", "Confirm by", "Signup sheet"] (locked, confirms on)
  def test_the_member_list_has_live_events_only(...): ...        # open rows soonest first; no "history" key; a done event absent; coming rows have title + subtitle only
  def test_a_rehearsal_is_hidden_from_everyone_but_its_tester(...): ...   # a non-tester: absent from the list, 404 with event_over_member; the tester: listed and 200
  def test_a_dropped_raid_is_hidden(...): ...                    # the dropped raid's key is 404; not in part_keys or rosters of its event
  def test_a_later_raids_key_answers_as_its_event(...): ...      # GET a later raid's key → j["key"] == the first raid's key
  def test_a_finished_event_is_404_in_the_one_sentence(...): ... # 404, body == {"error": PAGE_WORDS["event_over_member"]}
  def test_a_non_member_of_the_guild_gets_403(...): ...          # uses tests/test_web_multiguild.py's `bot` / client_for / as_user(c, DEMO_ONLY) with X-Oibot-Guild GUILD2 → 403, not_member True, for both routes
  def test_an_officer_gets_the_member_shape_from_the_member_routes(web_bot, ...): ...  # bearer (owner) → same keys_of as a member, no "boxes"/"actions"
  def test_the_locked_roster_matches_the_sheet(...): ...         # MemberPart groups == [[p.character or p.signup_name ...] for each roster group] and marks follow rc.confirmations; not_rostered == the sheet's left_off
  def test_answers_and_asks_are_the_viewers_own(...): ...        # answers[0] == the /api/me sheets row for that key; asks only the viewer's, only this event's raids
  def test_api_me_rows_are_unchanged_by_the_move(...): ...       # GET /api/me sheets rows == the same rows built through api_me.me_sheet_row
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_member_events.py`
  Expected: FAIL, because 404 for `/api/me/events` (the route doesn't exist) and `member_event_view` can't be imported.

- [ ] **Step 3: Implement `member_event_view.py`** (`visible`, `member_event_list`, `member_event_json`, `NotListed`).
  Keep it pure: no write, no solve. Build every row from the readers above, never from `event_json`. `subtitle` is
  `event_view._raids_words` of the lineup. Locks = `reg.local12(first.locked_at or run_times(...)[1])`. Confirm by =
  `reg.local12(run_times(...)[2])`.

- [ ] **Step 4: Move the `/api/me` row into `api_me.me_sheet_row`**, and add `web/api_member_events.py` with the two
  routes. Install it from `web/api.py` next to `install_events_api`. Add the four `PAGE_WORDS` keys.

- [ ] **Step 5: Run the tests to verify they pass**

  Run: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_member_events.py tests/test_events_api.py tests/test_follow_on.py tests/test_member_inbox_web.py tests/test_copy.py`
  Expected: PASS.

- [ ] **Step 6: Update `docs/layout/events.md` and `docs/layout/web.md`**, run the whole fast suite once (the
  implementer rules' command), and commit.

  ```bash
  git add src/oibot_gm/member_event_view.py src/oibot_gm/web/api_member_events.py src/oibot_gm/web/api.py src/oibot_gm/web/api_me.py src/oibot_gm/constants.py docs/layout/events.md docs/layout/web.md tests/test_member_events.py
  git commit -F <msg>   # "Members' event view, task 1: the member read model and its routes, with the no-leak tests."
  ```

### Task 2: One answer component, shared signup tabs, a read-only roster

**Files:**
- Create: `frontend/src/components/MyAnswer.tsx` (the Me page's `SheetRow`, `pages/Me.tsx:150-227`, moved)
- Create: `frontend/src/components/events/SignupTabs.tsx` (the tabs body of `SignupsSection.tsx`)
- Create: `frontend/src/components/events/MemberRoster.tsx`
- Modify: `frontend/src/pages/Me.tsx` (uses `MyAnswer`), `frontend/src/components/events/SignupsSection.tsx` (wraps
  `SignupTabs`), `frontend/src/api.ts` (types below)
- Modify: `src/oibot_gm/constants.py` (`PAGE_WORDS["signup_tabs"]`)
- Modify: `docs/layout/frontend.md`
- Test: `tests/test_frontend_source.py`, `tests/test_events_api.py` (the meta test at `:538`)

**Interfaces:**
- Consumes: Task 1's JSON shapes (`MemberPerson`, `MemberPart`, `MemberSeat`); `SeatLine` / `markOf` / `RoleCounts`
  (`components/board/shared.tsx:45-91`); `GameIcon`; `ViewSection`; `useConfirm` (`ConfirmOpts`, `ConfirmResult`).
- Produces:
  - `PAGE_WORDS["signup_tabs"]`:
    ```python
    {"in":   {"label": "Joined",        "empty": "Nobody has joined yet."},
     "sub":  {"label": "Bench",         "empty": "Nobody is on the bench."},
     "out":  {"label": "No thanks",     "empty": "Nobody said No thanks."},
     "none": {"label": "No answer yet", "empty": "Everyone has answered."},
     "away": {"label": "Away",          "empty": "Nobody is away that day."}}
    ```
  - `api.ts` types:
    - `MemberPerson { display_name: string; character: string; cls: string; spec: string; callout?: boolean }`
    - `MemberSeat { display_name; character; cls; spec; role; answer?: string | null }`
    - `MemberPart { key: string; raid_name: string; label: string; rosters: { n: number; groups: MemberSeat[][] }[]; not_rostered: string[]; marks: boolean }`
  - `MyAnswer({ s, characters, meta, ask, onDone, flash, link }: { s: MySheet; characters: { label: string; is_main: boolean }[]; meta: Meta; ask: (o: ConfirmOpts) => Promise<ConfirmResult>; onDone: () => void; flash?: boolean; link?: boolean })`
    keeps `SheetRow`'s behaviour exactly: the same routes, busy rules, queue note, Can't make it, Leaving after and I'm
    staying. `link` makes the raid line a `Link` to `/events/<key>`. Task 3 turns it on.
  - `SignupTabs({ tabs, counts, rows, meta, right }: { tabs: ("in" | "sub" | "out" | "none" | "away")[]; counts: Record<string, number>; rows: Record<string, MemberPerson[] | EventPerson[]>; meta: Meta; right?: (p: EventPerson | MemberPerson) => React.ReactNode })`
    draws one tab per key, with its label and empty sentence from `meta.page_words.signup_tabs`, each tab grouped by
    class (the current `byClass`). A row with `callout` shows ⚑ (`raid_views.OUT_MARK`'s mark) with
    `meta.page_words.called_out` as its tooltip.
  - `SignupsSection` keeps its props and output: tabs `["in","sub","out","none"]`, `right` = the row ⋮, and `SomeoneElse`.
  - `MemberRoster({ parts, meta }: { parts: MemberPart[]; meta: Meta })` shows, for each raid, its label, then each roster
    as `Roster N` with `RoleCounts`, then groups as columns of `SeatLine` (the mark only when `part.marks`), then a "Not
    rostered" line of names. The heading word comes from `meta.run_words.bank.locked`. There is no menu, drag, pin or
    button.

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/test_frontend_source.py
  def test_one_answer_component_serves_me_and_the_member_event_page():
      # MyAnswer.tsx exists and holds "/api/me/run/"; Me.tsx imports MyAnswer, has no "function SheetRow", and no "/api/me/run/"
  def test_signup_tabs_are_shared_and_worded_by_the_server():
      # SignupsSection.tsx imports SignupTabs; neither file holds '"No answer yet"' / '"Nobody has joined yet."';
      # SignupTabs.tsx reads "page_words.signup_tabs"
  def test_the_member_roster_has_no_officer_controls():
      # MemberRoster.tsx: no "MemberMenu", "draggable", "PinMark", "onBoard", "api.post"
  # replace the literal-label assertion at :1197-1198 with: SignupsSection still renders <MemberMenu and <SomeoneElse
  # tests/test_events_api.py
  def test_meta_carries_the_signup_tab_words(officer_client): ...   # /api/meta page_words.signup_tabs == PAGE_WORDS["signup_tabs"]
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_frontend_source.py -k "answer_component or signup_tabs or member_roster" tests/test_events_api.py -k signup_tab`
  Expected: FAIL (the files don't exist, and `signup_tabs` is missing).

- [ ] **Step 3: Implement.** Move `SheetRow` into `MyAnswer.tsx` and give it a `characters` prop instead of `me`. Extract
  `SignupTabs`. Write `MemberRoster`. Add the types and `PAGE_WORDS["signup_tabs"]`. The Me page and the officers'
  Events page must look exactly as before.

- [ ] **Step 4: Build and check**

  Run: `cd frontend && npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py`, then
  `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_frontend_source.py tests/test_events_api.py tests/test_copy.py`
  Expected: tsc is clean, the bundle check passes, and the tests PASS.

- [ ] **Step 5: Update `docs/layout/frontend.md`** (MyAnswer, SignupTabs, MemberRoster), run the whole fast suite once, and
  commit, including `src/oibot_gm/web/static/app`.

### Task 3: The member Events page, the rail, links from Discord and the Me page, the manual, screenshots

**Files:**
- Create: `frontend/src/pages/MemberEvents.tsx`
- Modify: `frontend/src/App.tsx` (`NAV`, `EventRoute`, the `/events` route), `frontend/src/components/ListPage.tsx`
  (`to` optional), `frontend/src/pages/Me.tsx` (`MyAnswer link`), `frontend/src/api.ts`
- Modify: `src/oibot_gm/raid_views.py:249-258` (`event_url`'s docstring only: a member now gets the members' event view)
- Modify: `scripts/serve_scratch.py` (`--member`), `scripts/shots.py` (`--member-cookie`)
- Modify: `docs/manual.md` §2a (the members' pages paragraph at about line 315) and the sheet-title link wherever the
  manual says it opens the Me page (grep "Me page" near "title"), `docs/layout/frontend.md`, `docs/layout/events.md`
  (event_url)
- Test: `tests/test_frontend_source.py`

**Interfaces:**
- Consumes: Task 1's routes and JSON; Task 2's `MyAnswer`, `SignupTabs`, `MemberRoster`, `EventHead`
  (`components/events/shared.tsx`), `EventDetails`, `ListPage`, `useLive`, `usePoll`.
- Produces:
  - `api.ts`:
    - `MemberEventRow { key; title; subtitle; state; chip; answer: string }`
    - `MemberEventList { open: MemberEventRow[]; coming: { title: string; subtitle: string }[]; tz_words: string; empty: string }`
    - `MemberEventView { key; title; subtitle; state; chip; test_chip: string | null; details: Fact[]; signups: { counts: Record<"in" | "sub" | "out" | "away", number>; rows: Record<"in" | "sub" | "out" | "away", MemberPerson[]> }; rosters: MemberPart[]; part_keys: string[]; answers: MySheet[]; asks: MyAsk[]; characters: { label: string; is_main: boolean }[] }`
  - `MemberEventsPage({ meta })`:
    - The list is `ListPage` with title Events, `intro` = `tz_words`, and two groups. Open rows have `chip` = `answer`.
      Coming up rows have no `to` and are drawn as plain text.
    - `/events/:key` draws the event:
      - `EventHead` (title, chip, test tag);
      - "Your answer": `AskCard` for each of `asks`, then `MyAnswer` for each of `answers`, or
        `page_words.answer_needs_character` when `characters` is empty;
      - `EventDetails`;
      - "Signups": `SignupTabs` with tabs `["in","sub","out","away"]` and no `right`;
      - "Roster": `MemberRoster`, only when `rosters.length`.
    - A 404 prints the server's `error` above the list. A later raid's key moves the address to `j.key` (replace).
    - The page reloads on `useLive(load, ["run:", "member"])` and on `usePoll`.
  - `ListItem.to?: string`. Without it, `ListPage` renders the item as a non-interactive row.
  - `App.tsx`:
    - `NAV` gets `{ to: "/events", label: "Events", icon: IconCalendarEvent }` after Me when `!meta.viewer.officer`.
    - `/events` and `/events/:key` render `MemberEventsPage` for a viewer who is not an officer. Officers see the same
      pages as today.
    - `EventRoute` never navigates to `/me`.
  - `serve_scratch.py --member` sets a throw-away `OIBOT_WEB_SECRET` (`secrets.token_hex(16)`). It writes
    `<data>/member_cookie.txt`, a session for the lowest-id seeded main who is not `owner_discord_id`, signed as
    `web/app.py` signs sessions (`URLSafeSerializer(secret, salt="session")`, `{"uid", "name", "iat"}`).
  - `shots.py --member-cookie FILE` shoots `/app/events` and `/app/events/<the first open key from /api/me/events>` as
    that session, with no bearer. Files are named `<view>-member-events*.png`.

- [ ] **Step 1: Write the failing tests** (`tests/test_frontend_source.py`)

  ```python
  def test_a_member_has_events_in_the_rail_and_an_officer_has_it_once():
      # App.tsx: NAV gains "/events" only for !meta.viewer.officer; OFFICER keeps "/events"
  def test_an_event_link_opens_the_member_event_view():
      # EventRoute renders <MemberEventsPage for a non-officer; no 'pathname: "/me"' in EventRoute
      # (replaces the assertions at :592-611 and the EventRoute exemption at :1346-1350)
  def test_the_member_event_page_reads_only_member_routes():
      # MemberEvents.tsx holds "/api/me/events" and "/api/me/event/"; never "/api/event/" , "/api/event-list", "/api/run/", "MemberMenu", "EventBoard", "RoleSwitches", "ActivitySection"
  def test_the_member_event_page_uses_the_shared_pieces():
      # MemberEvents.tsx imports MyAnswer, SignupTabs, MemberRoster and AskCard; its SignupTabs tabs list has "away" and not "none"
  def test_me_rows_link_to_their_event():
      # Me.tsx passes `link` to MyAnswer
  def test_a_list_item_without_an_address_is_not_a_link():
      # ListPage.tsx: `to?:` in ListItem and a branch that renders no <a> without it
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_frontend_source.py`
  Expected: FAIL on the six new tests, and the old EventRoute test now contradicts the plan (replace it in the same step).

- [ ] **Step 3: Implement** the page, the route and rail changes, the `ListItem` change, `Me.tsx`'s `link`, the
  `event_url` docstring, and the two script options.

- [ ] **Step 4: Build and check**

  Run: `cd frontend && npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py`, then
  `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_frontend_source.py tests/test_copy.py tests/test_member_events.py tests/test_web_multiguild.py`
  Expected: PASS.

- [ ] **Step 5: Manual.** In §2a, members get **Events**: a list of live events (*Open*) and the next nights (*Coming
  up*). One event shows the member's own answer (the same buttons as the Me page), the public details, the signups by
  class (*Joined*, *Bench*, *No thanks*, *Away*), and after the lock each roster's groups. Every Discord event link
  opens it, and the Me page's rows link to it. An officer's view is unchanged. `tests/test_copy.py` stays green
  (the manual lint).

- [ ] **Step 6: Screenshots on the scratch server only**

  Run: `uv run python scripts/serve_scratch.py --seed --member --data <new temp folder>` in the background, then
  `uv run python scripts/shots.py http://127.0.0.1:8799 --guild demo --member-cookie <data>/member_cookie.txt`.
  Expected: `out/shots/desktop-member-events*.png` and `phone-member-events*.png` exist. Read them: no officer button,
  no "No answer yet" tab, one member per line, no labels beside icons. Stop the scratch server.

- [ ] **Step 7: Update `docs/layout/frontend.md` and `docs/layout/events.md`**, run the whole fast suite once, and commit
  (with the bundle). **Part A is shippable here.**

---

## Part B — Bench fills last

### Task 4: The solve takes Bench last

**Files:**
- Modify: `src/oibot_gm/roster/solver.py`. Add the constant, `_bench_terms` called right after `_extra_role_terms` in
  the objective step, and the module docstring's step list.
- Modify: `src/oibot_gm/raidcycle.py`. `first_come_bench_out` goes next to `first_come_out` (`:4230`); `_solve_run`
  (`:4345-4425`) calls it on a first-to-join event; `_roster_count` is extracted from `_solve_run`'s `n_rosters` line.
- Modify: `docs/layout/events.md` (the raidcycle / roster entries)
- Test: `tests/test_bench_last.py` (new, rc level), `tests/test_solver_bench.py` (new, solver level)

**Interfaces:**
- Consumes: `_Model` (`solver.py:144`), `_role_expr` (`:240`), `_slot_expr` (`:273`), `_extra_role_cost`
  (`:431`), `_per_roster` (`:441`), the objective step calling `_extra_role_terms` (`:130`), `SolveOptions.force_out` / `force_in`, `Player.status` ("signed" | "bench"),
  `rc.players_for` (unchanged: Bench answers already arrive as "bench"), `rc.first_come_out` (`raidcycle.py:4230`),
  `rc.merged_role_min`, `rc.EXTRA_ROLES`, `rc.how_many_rosters`, `rc.lock_forces`.
- Produces:
  - `solver.BENCH_SEAT_FACTOR = 4`, so `BENCH_SEAT_COST = BENCH_SEAT_FACTOR × _extra_role_cost(s, roster_bonus)`. This
    is more than a class or spec minimum's reward (2×) plus a past-minimum tank or healer's cost (1×) plus any seat
    value or synergy.
  - `solver._bench_terms(s: _Model, roster_bonus) -> None`. Bench = players with `status == "bench"` who are not in
    `force_out`. With no Bench answer it returns before creating anything, so the model is unchanged. Otherwise:
    - It adds a term of `-BENCH_SEAT_COST × x[name]` for each Bench answer.
    - For `role in ("tank", "healer")` with a minimum `n > 0`, it takes the pool-level
      `short_role = max(0, n × rosters − capable_joiners(role))`. Capable means a seatable joiner whose `role` or offspec
      role is `role`.
    - When `short_role > 0`, per roster `r` it adds `refund[role, r]` in `0..n` with:
      - `refund ≤ Σ bench role seats in r` (`_role_expr(..., only=bench)`);
      - `refund + Σ joiner role seats in r ≤ n`;
      - `Σ_r refund[role, r] ≤ short_role`;
      - and a term of `+BENCH_SEAT_COST × refund`.
  - `rc.first_come_bench_out(players: list[Player], seats: int, role_min: dict[str, int], fixed: set[str], late: set[str], kept_off: set[str]) -> set[str]`
    returns the Bench answers to force out on a first-to-join event. The steps:
    1. `free = seats −` the joiners not in `late ∪ kept_off`.
    2. `short[r] = role_min[r] −` those joiners playing `r` (for `r` in `EXTRA_ROLES`).
    3. Walk the Bench answers (not kept off) in `pos` order. A `fixed` one is kept. One playing a short role is kept
       (`short[r] −= 1`). Otherwise one is kept while `free > 0`. Every keep does `free −= 1`.
    4. Everything not kept is returned.
  - `rc._roster_count(reg, ev, players, layout_groups_used) -> int` is the same expression `_solve_run` uses today. Task
    6's `bench_seats` reads it.

- [ ] **Step 1: Write the failing tests**

  The rosters are built with `reg.seed_test_members` / `add_character` and `conftest.join(..., status="sub")` on
  `barrow_deeps`, size 10. A schedule's event uses `conftest.weekly`, and a one-off uses `rc.open_run`. "Seated" means
  `{p.signup_name for r in rosters for p in r.selected}` from `rc.propose(reg, rs, ev, save=False)`.

  ```python
  # tests/test_bench_last.py
  def test_empty_seats_go_to_the_bench_by_the_events_order(reg, rs):
      # 10 seats, 8 joined (2 tanks + 2 healers + 4 dps), 3 benched dps of one class and spec, ranks core / raider / trial
      # on a schedule's event → all 8 joiners + the core and raider benched; the trial benched is not seated
  def test_a_full_roster_of_joiners_seats_no_bench(reg, rs):
      # 10 seats, 12 joined (meets tank/healer minimums), 2 benched (one a core-rank healer) → no "bench" status among selected
  def test_a_benched_healer_fills_a_healer_minimum_the_joiners_cannot(reg, rs):
      # healer minimum 2 (Registry.set_minimums), 12 joined damage dealers, 0 healers, 1 benched healer →
      # the benched healer is seated; joiners seated == 9 (one more left out than the 10 without the bench answer)
  def test_a_benched_damage_dealer_never_displaces_a_joiner(reg, rs):
      # same as above with the benched player a damage dealer → not seated; 10 joiners seated
  def test_a_strong_bench_answer_never_displaces_a_weak_joiner(reg, rs):
      # 11 joined (minimums met; one a trial-rank alt with no regular spot), 1 benched core-rank main who is a regular and
      # sat out last time, 10 seats, a schedule's event → the benched player is not seated (today's 45-point gap loses to their bonus)
  def test_a_first_to_join_event_seats_the_bench_in_answer_order_after_every_joiner(reg, rs):
      # one-off (first to join), 10 seats, 7 joined, 5 benched with updated_at t1 < … < t5 → all 7 + the 3 earliest benched
  def test_the_bench_never_adds_a_roster(reg, rs):
      # one-off (rosters 1), 10 joined, 10 benched with tanks and healers for two → len(rosters) == 1, no bench seated
  def test_a_kept_off_bench_answer_is_never_seated(reg, rs):
      # 10 seats, 8 joined, 2 benched, one pinned out (ev.pins[uid] = "out") → only the other benched is seated (9 rostered)
  def test_a_hand_placed_bench_answer_keeps_its_group(reg, rs):
      # 10 joined, 1 benched placed in group 2 via ev.layout set in memory, not saved (prune_board keeps Bench names only
      # from Task 5) → propose(save=False) seats them in group 2 (forced in); Task 5 adds the saved variant
  def test_hand_placed_joiners_fill_every_seat_and_the_bench_stays_off(reg, rs):
      # 10 joiners all placed by hand, 2 benched → the roster is exactly the board; no bench seated
  def test_the_same_inputs_give_the_same_roster_with_bench_answers(reg, rs):
      # case 1's event solved twice (solver.forget_answers() between) → identical groups and selections
  def test_preview_lock_and_lock_agree_with_bench_answers(reg, rs):
      # as tests/test_solver_repeatable.py:34 with case 1's signups: lock_preview's rosters == lock's rosters
  def test_a_later_raid_fills_from_the_bench_too(reg, rs):
      # a two-raid event (open_run lineup=[{raid: barrow_deeps, rosters: 1}]), 8 joined + 2 benched → both raids seat the
      # same 8 + 2; locking part 0 then part 1 (lock_parts order) gives each raid 10
  # tests/test_solver_bench.py  (solver level, profile fixture, solver.solve_rosters directly)
  def test_no_seatable_bench_answer_adds_nothing_to_the_model():
      # players all "signed" (and again with every "bench" player in force_out): the model's variable and constraint counts
      # and objective are equal with and without _bench_terms (call the step builders on two _Model copies)
  def test_bench_seats_cost_more_than_a_class_minimum_earns():
      # a Paladin minimum of 1, 10 signed non-paladins + 1 benched paladin, 10 seats → the paladin is not seated
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_bench_last.py tests/test_solver_bench.py`
  Expected: FAIL on `test_bench_seats_cost_more_than_a_class_minimum_earns` and
  `test_a_strong_bench_answer_never_displaces_a_weak_joiner` (today's 45-point gap loses to a class minimum's reward and
  to a strong bonus), and on `test_no_seatable_bench_answer_adds_nothing_to_the_model` (no `_bench_terms` yet). Cases that
  already hold today stay as pins of the rule.

- [ ] **Step 3: Implement** `BENCH_SEAT_FACTOR`, `_bench_terms` (called after `_extra_role_terms`, inside the objective
  step, so the model's step order stays as the docstring lists), `first_come_bench_out`, `_roster_count`, and the call
  in `_solve_run`. In `_solve_run` the call is `force_out += sorted(bench_out - kept)`, the same way `late` is added.

- [ ] **Step 4: Run the tests to verify they pass, including the slow solver files**

  Run: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_bench_last.py tests/test_solver_bench.py tests/test_event_board.py tests/test_raid_night.py tests/test_follow_on.py tests/test_fill.py`, then
  `uv run pytest -q -m slow tests/test_solver*.py`
  Expected: PASS. A test elsewhere that expected a Bench answer in front of a joiner is the old behaviour. Change its
  expectation, and name each such test in the report.

- [ ] **Step 5: Update `docs/layout/events.md`**, run the whole fast suite once, and commit.

### Task 5: The board keeps Bench names

**Files:**
- Modify: `src/oibot_gm/raidcycle.py`. Add `board_names` (new). Change `prune_board` (`:4631`), `apply_move` (`:4725`),
  `autofill` (`:4946-4950`) and `board_complete` (`:4805`).
- Modify: `src/oibot_gm/discord_raid.py:1188` (`_write_board`), `src/oibot_gm/configops.py:1822` (`run_move`'s refusal)
- Modify: `docs/layout/events.md`
- Test: `tests/test_bench_last.py`, `tests/test_parity_ops_board.py:64`

**Interfaces:**
- Consumes: Task 4's solve. `rc.lock_forces` (placed Bench names are kept on).
- Produces:
  - `rc.board_names(ev: RaidEvent) -> set[str]` is the names a board may hold. While open, that is the signups with
    status "in" or "sub". Once locked, it is the "in" signups plus every seated name.
  - `prune_board` keeps a placed name whose status is "in" or "sub". A placed name whose answer becomes "out" still
    leaves, as do one pinned out and one whose character or spec changed.
  - `apply_move` refuses a name outside `board_names` with `rc.NOT_ON_SHEET = "{name} hasn't answered Join or Bench on this event"`.
    `configops`' `run_move` refusal uses the same constant (`f"{name} ..."` filled in).
  - `autofill` keeps the solve's names that are in `board_names(ev)` (Bench names it seats included).
  - `board_complete` is also False while an unplaced Bench answer who is not kept off exists and the board's placed
    count is below `run_size × run_rosters`.
  - `_write_board` filters by `rc.board_names(ev)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_bench_last.py`)

  ```python
  def test_a_bench_name_can_be_placed_and_survives_a_save(reg, rs): ...       # apply_move of a "sub" name → layout holds it after rs.save (prune_board keeps it)
  def test_a_saved_hand_placed_bench_answer_keeps_its_group_at_the_lock(reg, rs): ...  # case 8 with the layout saved → the lock seats them in group 2
  def test_a_placed_bench_name_whose_answer_becomes_no_thanks_leaves_the_board(reg, rs): ...  # set the signup "out", save → off the layout, "off the board: <name>" in ev.log
  def test_a_name_with_no_join_or_bench_is_refused_on_the_board(reg, rs): ...  # apply_move of an "out" name → ValueError(NOT_ON_SHEET filled)
  def test_auto_fill_places_the_bench_answers_the_lock_would_seat(reg, rs): ...  # case 1's signups: autofill's layout names == the lock's seated names
  def test_preview_lock_lock_and_auto_fill_land_on_one_roster_with_bench(reg, rs): ...  # sets equal across the three
  def test_a_board_with_an_open_seat_and_a_bench_answer_is_not_complete(reg, rs): ...  # 8 joined all placed, 2 benched → board_complete False; all 10 placed → True
  def test_a_locked_board_edit_keeps_a_rostered_bench_answer(reg, rs): ...  # NightBot(reg, rs) from tests/test_raid_night.py: lock case 1, then RaidMixin._write_board with the roster's groups moved → the benched seat is still seated
  # tests/test_parity_ops_board.py:64 asserts the new sentence ("hasn't answered Join or Bench on this event")
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_bench_last.py -k "board or auto_fill or placed or refused" tests/test_parity_ops_board.py`
  Expected: FAIL.

- [ ] **Step 3: Implement** `board_names`, the four `raidcycle` changes, `_write_board` and the `configops` refusal.

- [ ] **Step 4: Run the tests to verify they pass**

  Run: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_bench_last.py tests/test_event_board.py tests/test_parity_ops_board.py tests/test_raid_night.py tests/test_events_api.py`
  Expected: PASS.

- [ ] **Step 5: Update `docs/layout/events.md`** (prune_board, apply_move, autofill, board_complete, board_names), run the
  whole fast suite once, and commit.

### Task 6: The lock's words: the box, the sheet, the receipt, the Confirm ask, the manual

**Files:**
- Modify: `src/oibot_gm/raidcycle.py`. Add `bench_seats` (new) and change `_answer_state`'s Bench sentence (`:6161-6162`).
- Modify: `src/oibot_gm/consequences.py:182-242` (`_board_state`, `_lock_head`, `_lock`)
- Modify: `src/oibot_gm/raid_views.py:185` (the open sheet's footer)
- Modify: `docs/manual.md` §1a **Bench** (`:96-97`), §2 the sheet's Bench line (`:219`), §4 the lock (where leftovers and
  the bench are described, about `:1283-1290`), `docs/layout/events.md`, `docs/layout/confirms.md` (lock_box)
- Test: `tests/test_bench_last.py`, `tests/test_confirm_readability.py` (the lock box cases), `tests/test_raid_night.py`
  (the Confirm DM)

**Interfaces:**
- Consumes: Task 4's `_roster_count`, `rc.lock_forces`, `rc._capable`, `rc.merged_role_min`.
- Produces:
  - `rc.bench_seats(reg, rs, ev) -> int` predicts, without a solve, how many Bench answers the lock rosters.
    - `J` = joiners not kept off; `B` = Bench answers not kept off; `seats = run_size × _roster_count`.
    - `empty = max(0, seats − |J|)`.
    - `short_fill = Σ_{tank,healer} min(max(0, min_r × n − capable(J, r)), capable(B, r))`.
    - The result is `min(|B|, max(empty, short_fill))`. On a first-to-join event, the same count.
  - `consequences._board_state` counts, as "waiting", the unplaced joiners not kept off, plus the unplaced Bench answers
    not kept off while placed < seats.
  - The lock box head, with `k = rc.bench_seats(...)`. `{n}` = "one" when `k == 1`, else the digit.
    - Empty board: `f"The lock builds the roster for {whom} from those who joined and {n} from the bench."`, falling back
      to `"… for all {len(lineup)} raids from those who joined and {n} from the bench."` past 20 words.
    - Part placed: `f"Everyone on the board keeps their place, and the lock fills the rest, {n} from the bench."`
    - A full board, or `k == 0`: today's sentences, unchanged.
  - `_lock`'s DM-sentence count is `n = min(len(joined) + k, seats)`.
  - `raid_views` footer: `"Join = I'm coming · Bench = call me if you need me"` becomes `rc.SHEET_ANSWERS = "Join = I'm coming · Bench = call me if a seat would stay empty"`.
  - The Bench receipt: `f"You're on the **Bench** as {as_} for {run} — the lock rosters you only if a seat would stay empty, and you're among the first asked if one opens."`

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/test_bench_last.py
  def test_the_bench_count_the_box_predicts_is_what_the_lock_rosters(reg, rs):
      # cases 1, 2, 3, 5 and 7: rc.bench_seats(...) == count of status "bench" in propose()'s selected
  def test_the_lock_box_says_how_many_come_from_the_bench(reg, rs):
      # case 1, empty board → box sentences[0] ends "from those who joined and 2 from the bench."; the DM sentence says "The 10 rostered get"
      # one placed joiner → sentences[0] == "Everyone on the board keeps their place, and the lock fills the rest, 2 from the bench."
      # case 2 → today's head (no "bench")
  def test_the_bench_receipt_says_when_the_lock_rosters_them(reg, rs): ...  # rc.answer_receipt for a "sub" signup contains "only if a seat would stay empty"
  # tests/test_raid_night.py
  def test_a_bench_answer_the_lock_rosters_gets_the_confirm_ask(reg, rs): ...  # NightBot lock on case 1 (confirms on): the 2 benched members get the Confirm ask; no other member is DMed beyond today's
  # tests/test_confirm_readability.py: add case 1 (empty board), case 1 with one placed name and case 3 to its lock_box cases
  ```
  Grep `tests/` for the two old sentences (the footer and "not coming unless needed") and update every match.

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_bench_last.py -k "box or receipt or predicts" tests/test_confirm_readability.py tests/test_raid_night.py -k "bench or lock"`
  Expected: FAIL.

- [ ] **Step 3: Implement** `bench_seats`, `_board_state`, the head variants, the count, the footer constant and the
  receipt.

- [ ] **Step 4: Manual.**
  - §1a **Bench**: "the sheet answer 'call me if a seat would stay empty': the lock rosters you only when a seat would
    otherwise stay empty, or when a tank or healer minimum is short and you play that role. A seat is never taken from
    someone who joined for any other reason. When a place frees up later, you are among the first asked."
  - §2: the sheet's footer words.
  - §4 (the lock): the order is everyone who joined, then the bench by the event's order (by priority, or by when they
    answered on a first-to-join event). A benched person who is rostered gets the Confirm ask like anyone. The number of
    rosters comes from those who joined. The lock box says how many come from the bench.

- [ ] **Step 5: Run the tests to verify they pass**

  Run: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_bench_last.py tests/test_confirm_readability.py tests/test_raid_night.py tests/test_copy.py tests/test_quiet_edges.py tests/test_events_api.py`
  Expected: PASS.

- [ ] **Step 6: Update `docs/layout/events.md` and `docs/layout/confirms.md`**, run the whole fast suite once, and commit.

### Task 7: The board shows the bench

**Files:**
- Modify: `src/oibot_gm/raidcycle.py:37` (`BANK_WORDS`), `src/oibot_gm/constants.py` (`PAGE_WORDS`),
  `src/oibot_gm/event_view.py:175-208` (`_part_view`'s `placed_words`), `src/oibot_gm/web/api.py:931-960`
  (`board_json`'s `bench`)
- Modify: `frontend/src/components/board/BoardView.tsx` (the event variant's bank: a second section),
  `frontend/src/components/events/EventBoard.tsx` (if it passes the bank through), `frontend/src/api.ts` (`Board.bench`)
- Modify: `docs/manual.md` §1a **Not placed / Not rostered** (`:105`) and §4 *The Events page*'s board text
  (`:1108-1136`), `docs/layout/frontend.md`, `docs/layout/events.md`
- Test: `tests/test_events_api.py`, `tests/test_event_board.py`, `tests/test_frontend_source.py`

**Interfaces:**
- Consumes: Task 5's `board_names` and placed Bench names; the existing `POST /api/run/{key}/move` (`rc.apply_move`
  accepts a Bench name since Task 5).
- Produces:
  - `rc.BANK_WORDS["bench"] = "Bench"` (it travels to the page through `/api/meta`, as `bank.open` / `bank.locked` do).
  - New `PAGE_WORDS` keys:
    - `"placed_of_bench"`: "{n} of {m} joined and {b} from the bench". `_part_view` uses it instead of `placed_of` when
      `b` (placed Bench names) > 0.
    - `"bench_empty"`: "Nobody is on the bench. A member who answers Bench shows here."
    - `"bench_note"`: "The lock rosters someone here only when a seat would stay empty or a tank or healer minimum is short."
    - `"board_footer"` (changed): "The lock keeps these groups and fills any open seat from Not placed, then from the
      bench. Auto-fill groups places them now the way the lock would."
  - `board_json` (open events only) adds `"bench": [seat_json(...)]`, the Bench answers not in the layout, in answer
    order. It is `[]` once locked. `api.ts`: `Board.bench?: Seat[]`.
  - The event variant of `BoardView`, before the lock, shows the bank as two sections: `bank.open` (today's) and then
    `bank.bench`, with `bench_note` and `bench_empty`. A Bench line can be dragged into a group (the same `/move`), and a
    group name dragged onto either section un-places it.

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/test_events_api.py
  def test_the_board_lists_bench_answers_apart_from_not_placed(officer_client, reg, rs): ...
      # open event, 8 joined + 2 benched, none placed → parts[0].sheet.board.bench == the 2 benched; board.bank holds the 8 joiners only
  def test_placed_words_count_the_bench_apart(officer_client, reg, rs): ...
      # one benched placed by hand → placed_words == "0 of 8 joined and 1 from the bench"
  def test_meta_carries_the_bench_bank_word(officer_client): ...   # meta run_words.bank.bench == "Bench"
  # tests/test_frontend_source.py
  def test_the_event_board_draws_a_bench_section_from_the_servers_words(): ...
      # BoardView.tsx reads "bank.bench", "page_words.bench_note", "page_words.bench_empty", and "board.bench"; no literal '"Bench"'
  ```

- [ ] **Step 2: Run the tests to verify they fail**

  Run: `uv run pytest -q tests/test_events_api.py -k "bench" tests/test_frontend_source.py -k bench`
  Expected: FAIL.

- [ ] **Step 3: Implement** the words, `board_json`'s `bench`, `placed_words`, and the `BoardView` section.

- [ ] **Step 4: Build and check**

  Run: `cd frontend && npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py`, then
  `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests/test_events_api.py tests/test_event_board.py tests/test_frontend_source.py tests/test_copy.py`
  Expected: PASS.

- [ ] **Step 5: Manual.** In §1a, *Not placed / Not rostered* gains *Bench* (before the lock, the Bench answers not in a
  group, which an officer may drag in). In §4, the board's text names the two sections and the new placed count.

- [ ] **Step 6: Screenshot on the scratch server** (`serve_scratch.py --seed --data <new temp folder>`, after giving the
  seeded open event two Bench answers), then shots of `/app/events/<key>`. Read the board: two bank sections, no label
  beside an icon. Stop the server.

- [ ] **Step 7: Update `docs/layout/frontend.md` and `docs/layout/events.md`**, run the whole fast suite once, and commit
  with the bundle.
