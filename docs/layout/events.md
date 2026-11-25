# Layout — raids, events, schedules: the solver, the signup cycle, lock / fill, the Events / Schedules / Raids read models and their routes, the Discord raid cards and buttons

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  roster/              solver.py (CP-SAT select+group, pins/forces, per-player bonus; SolveOptions.minimums = slot → per-roster minimum, NEVER a
                       constraint: _minimum_terms rewards each seat up to a minimum MINIMUM_REWARD = 2 × EXTRA_ROLE_COST, _extra_role_terms costs a
                       tank / healer past theirs EXTRA_ROLE_COST; no role maximum; SolveOptions.lead_rostered = a follow-on never charges the lead's
                       rostered as extra; scaled_role_minimums = the game's tank / healer minimums, rounded up — design §5.67 phase 2;
                       REPEATABLE per budget: interleave_search with RANDOM_SEED 0 and a deterministic-time limit (time_limit_s × DTIME_PER_SECOND), so
                       Preview lock and Lock land on one roster; the wall clock only a HANG GUARD, WALL_CEILING 10 × the budget (2 × cut solves
                       short under load — the final fix wave) — re-check tests/test_solver_repeatable.py on every OR-tools upgrade); ONE ANSWER
                       PER MODEL: _answers (LRU ANSWERS_MAX 64, keyed by the model proto + the search parameters) — an identical model takes
                       the kept answer without searching, the lock included, so the board, Preview lock and the lock agree under load; never
                       kept for a Stopped / infeasible solve nor one the ceiling cut (logged, ceiling_hits); forget_answers() = tests only; §5.67 phase 4: seats counted from SEATABLE players (_Model.seatable =
                       the pool minus force_out — a kept-off name never makes an under-filled raid infeasible; with nobody kept off the model is
                       unchanged), _refuse_conflicts → Conflict(reason KEPT_ON | KEPT_BOTH | GROUP_FULL | GROUP_ROLES, names, group, roster, have,
                       seats) before searching (str() keeps "no roster found: <reason>"); Stop (CpSolver.stop_search on every attached solver) /
                       Stopped = THE way the Events board's pictures give way to a lock (only they pass one); SolveOptions.kept_off is informational
                       (explain's Closest to a seat skips it)), explain.py (advisories), coverage.py (slot-aware)
  raidcycle.py         signup-driven cycle (design.md §5.20): raid SCHEDULES (§5.27: Registry.schedules/schedule_def; weekly slots · day N of each
                       lockout · pickup templates; every schedule has a guild-unique id, there is no `default` one) + timing (schedule_def: slots, the
                       schedule's own SCHEDULE_TIMING, weights) → occurrences/upcoming/schedule_starts/open_run(schedule=) (ephemeral run dict per raid
                       of a run, keyed by new_run_key — the key says nothing; a run is found by its stamps, see Identity below);
                       run_rosters(ev) = the schedule's expected rosters N (health/needs/sheet measure against N × size, the lock aims for N), Join/Bench/No thanks (in/sub/out), propose() at lock (weights, pins,
                       the raid's minimums, splits into several rosters per slot), confirmations (placement asks on the run key), free_seat/seat_player/
                       expire_confirmations, fill engine (after lock only: benched joiners + Bench first → pool → offspec → alt swaps, paired with a backfill
                       when the vacated role would go short; asks expire after fill_ask_hours). `nudge`/`nudge_hours_before`/`autofill`/`open_dm`
                       are the run dict's, seeded from its schedule at open (a run's own via reschedule). No LLM. Run states open | locked | done | cancelled; the member-facing word is "rostered", never "seated".
                       THE run vocabulary (design §5.56 phase 2): RUN_STATE_WORDS, NO_CONFIRMATIONS, FIRST_COME, BANK_WORDS (Not placed / Not rostered),
                       LEFTOVERS, PIN_WORDS, NOT_ANSWERED_WORDS, ASK_HOW (how an ask reaches someone), ASK_ANSWER_WORDS, fill_state_text (sentences),
                       answer_word (the Me badge), balance_words (a split compared in words) — /api/meta carries them; the site keeps no copy
                       ONE planner (rc.role_plan; fill engine extended to before lock): ranked moves with alternates and a still-short line; nothing is
                       sent before an officer's Ask (RaidMixin.send_plan with the shown plan rev; rc.send_batch = exactly what a send DMs); after lock on a
                       run (recurring or ad hoc) same-role asks are automatic and ROLE SWAPS wait for an officer (D3, out/roster_decisions.md; D22). Series
                       coverage + 4-week forecast: Registry.series_coverage / rc.series_outlook (officer-only).
                       rosters_copy(reg, raid, sid, n) = the rosters-per-run sentence (the next run, never the open one — §5.44);
                       split_reason / split_reason_text: why the joiners make fewer runs than the headcount (what the next run is short, in code);
                       leftovers() = joiners the full roster(s) left out; another_run_hint = one line when leftovers + bench + pool could field another run.
                       People (§5.41): character_changes / character_effects / character_preview / after_character_change (a respec rewrites the
                       sheet answers; a retired / deleted / respecced seat is freed — a role flip on a locked RECURRING run keeps it); join_while_away;
                       relabel_runs(rs, uid, old, new) = a renamed character's label in every LIVE run (Registry.rename_character's batch);
                       move_on_runs(rs, uid, to, names, label, series_add=, series_drop=) = a moved character's signup, seats, callouts (+ pins, board,
                       lock seat, leave, nudge, regulars) to its new member on every LIVE run (Registry.move_character's batch); character_entries / on_run
                       Raid night (design §5.39): expire_confirmations expires only a held ask (locked runs only); unasked_seats (the lock's recovery: only
                       RaidEvent.lock_seats, the seats at the flip — never a fill-seated or later seat; legacy_lock_sent = a pre-field run is stamped); cancel_effects;
                       reopen_refusal/reopen_run (an END by mistake, REOPEN_GRACE_HOURS, never a cancelled run); remind_refusal; end_refusal;
                       open_when/which_run/open_preview (THE open-time resolver, DST-safe); spec_switches / regulars_not_rostered / fill_state_text /
                       asks_on_site / log_rows (RaidEvent.log_at stamped by RaidStore.save) for ev_json; upcoming_rows (each active schedule's next three runs whatever the horizon, can_open + open_row_refusal's why) / short_why (a run card's short clause) — §5.56; FillAsk.channel dm|web (a failed DM = web)
                       Identity (§5.67 phase 3): a run's key is new_run_key(reg, rs, raid) = '<abbr>-<6 of KEY_ALPHABET>' (no 0/1/i/l/o/r), the run
                       dict's key and the run file's key the same string; NOTHING reads a raid, a date, a time or a schedule out of a key or builds one
                       from a time (run_key, event_key, schedule_kw, RunKeyClash and TEST_SCHEDULE are gone; tests/test_event_identity.py pins it).
                       Stamps on every run file and run dict: `schedule` (schedule_stamp: the id, None for a one-off, a rehearsal or a later raid),
                       `occurrence` (occurrence_stamp: the start the schedule gave it in guild WALL time 'YYYY-MM-DDTHH:MM', so a timezone change
                       never makes a second run), `event` (the first part's key), `part`, `starts` (minutes after part 0), `stands_for`, `dropped`.
                       occurrences(reg, now, horizon, lead=) = every active non-pickup schedule's starts guild-wide (upcoming = its filter by first
                       raid; a schedule whose lineup names a raid the profile lacks — gone_raids — gives none); event_of_occurrence(rs, sid, occ, reg=)
                       = THE finder of a schedule's run in any state (stamped `occurrence` or stands_for_of; with reg also an unstamped older file by
                       its start + run_schedule_id, and a live run of the schedule moved to exactly that start); live_at(rs, raid, start, test=) = a
                       one-off's / rehearsal's (a cancelled one-off never blocks its minute); find_run = what an open at a start meets (open_run,
                       open_preview, the wizard, the plain-text describe); which_run / open_preview / upcoming_rows carry `occurrence`, never a key.
                       run_schedule_id(reg, ev) = the run dict's schedule_id, else a stamped file's `schedule` as is, else an old stamp through the
                       aliases; run_series / run_kind read it. stamp_runs(reg, rs, commit=) stamps every LIVE unstamped run file (its own JSON, only the
                       new keys) and dict; stamp_weights(reg, rs) = every LIVE run dict without its own `weights` gets what it reads today
                       (live_weights; the owner's tweaks B); migrate_at_start = stamp_runs(commit=False), stamp_weights, then
                       migrate_schedules(stamped=, weighed=) in ONE store.batch (one commit
                       per guild with one message; a second start writes nothing); stamp_before_change(reg, rs, parts) = the same stamps in memory for
                       a live run the start missed, at reschedule_event / edit_lineup, written in their batch (never found by its new start). ensure_run always CREATES (stamps `created_at`; a first part's dict with no run file after
                       DICT_WITHOUT_RUN_HOURS is dropped by cleanup_ephemeral).
                       Lineups (§5.62, §5.67 phase 3): up to LINEUP_MAX 4 raids, one run file and one run dict per raid; part 0 owns the sheet, the
                       answers and the timing and its dict holds the whole `lineup`; every later part's `follows` = part 0's key, `followed_by` = the
                       next part's; event_parts / first_part / later_parts / same_event / part_no / event_id (a `dropped` raid answers as itself);
                       later_dicts / dict_start / parts_short (the health card's later-raid lines) / follow_words_of; after_minutes = every later
                       start in REAL time (UTC, a clocks-change night); complete_parts = an open cut off between parts finished once (open_run and
                       the scheduler's minute; never a sheet, never a dict); sync_parts (sync_follow its alias) = every later part's answers are part
                       0's; leave_follow / stay_for_follow; `_solve_run` adds LEAD_SEAT_BONUS once per earlier raid a player was rostered on;
                       `conflicts` exempts same_event; free_seat on part 0 frees the seat on every locked later part; held_later(rs, ev, who) = the
                       later places a Can't frees; a cancel on any part takes every live raid (cancel_effects(…, rs) / cancel_tells_channel);
                       LATER_PART_LOCK. run_strategy(reg, ev) = THE split-strategy chain (run → roster → raid → balanced) the lock and Preview lock share.
                       Reschedule (§5.67 phase 3, C11): reschedule_refusal (MOVE_CLOSED, MOVE_STARTED, MOVE_PAST, MOVE_SAME / TIMING_SAME,
                       MOVE_CLASH via live_at, TIMING_LOCKED, TIMING_ORDER, TIMING_UNKNOWN), reschedule(reg, rs, ev, start=, timing=, by, save=) →
                       Moved = every part keeps its offset (after_minutes), `occurrence` / `schedule` NEVER change, confirm_by moves by the delta (or
                       within MOVE_CONFIRM_GRACE when past), EVENT_TIMING = the settings an event takes as its own (the owner's tweaks B, 2026-11-24:
                       the sheet timing, confirm, first_come, weights, split_policy — named_split / split_changes, a raid's run FILE split_strategy);
                       AFTER_LOCK (fill_ask_hours, autofill) still change once locked, the rest is refused: TIMING_LOCKED, CONFIRM_LOCKED,
                       PRIORITY_LOCKED, consequences.STRATEGY_LOCKED; a lock-time change tells who answered (Moved.lock_moved, "<raid> now locks …");
                       run_weights = the run dict's own `weights` (stamped at open by ensure_run / _part_dict: fallback_weights; live_weights only
                       for a dict without them), run_first_come = the first raid's `first_come`, else its kind (a later raid never); first_come_shown = THE predicate
                       every first-come WORD reads (sheet footer, status line, Me queue note, help, priority_words); rotation_weight =
                       the run's `rotation_sat_out` (the raid's sat-out weight stamped at open / migration); place_run (shared with
                       move_runs_with_clock); timing_changes; answered_ids(first) = who a `moved` notice reaches (every row but away);
                       absorbed_schedule_run; move_start (an ISO instant, or guild wall time — MOVE_GAP / MOVE_TWICE / MOVE_WHEN); event_rev = the
                       fingerprint a preview hands out (RUN_CHANGED when stale). plan_stands_for(reg, raid, before, after) = THE decision, made once at
                       a schedule write (Registry._write_schedules, before the rows are installed, same commit): a live first part whose night the
                       new run times no longer give stands for the run time paired with its own (_slot_pairs by time; _anchor_window = the raid's
                       lockout window with a first opening, else the Monday week); an edit that gives its own night again clears it; the stamps are
                       written into the dicts BEFORE the rows (the scheduler's minute takes no config_lock); run_times_changed short-circuits a timing-only edit; a cancelled
                       run holds no stands_for, a done one keeps it; sync_stands_for copies the dict's onto the file (scheduler minute, cleanup).
                       Edit lineup (§5.67 phase 3): run_lineup (what a run plays now), lineup_refusal (LINEUP_CLOSED, LINEUP_LOCKED, parse_lineup's,
                       LINEUP_FIRST, LINEUP_GONE, LINEUP_ORDER, LINEUP_SAME), plan_lineup → LineupChange (kept / new / removed, moved, told,
                       dropped_*), apply_lineup = THE write (run keys never change; a removed raid's file cancelled with `dropped`, its dict dropped,
                       no notice, no card; one store.batch), _part_dict (shared with ensure_run)
                       Phase 4 (§5.67, the Events / Schedules / Raids pages): occurrence_index(reg, rs) = event_of_occurrence for many nights in ONE pass
                       (the finder answers through it); next_runs_of; pool_unanswered(reg, ev) = the pool minus everyone with a row (No answer yet,
                       Answer for someone, health's hint); rehearsal_run; run_timing(reg, first) (the inverse of the settings Reschedule takes);
                       lineup_fixed(rs, ev) = THE lineup lock rule (lineup_refusal and Edit lineup read it); parse_run_lineup; roster_trim = a lowered
                       roster count drops the board places and pins on the roster that goes (plan_lineup's trimmed_places / trimmed_pins, apply_lineup);
                       priority_words / confirmation_words / lineup_words / lineup_roster_words / event_lineup; gone_sentence; series_forecast(if_resumed=);
                       ensure_run / open_run(lock_hours=, first_rosters=) (the New event page's one-off choices, never a test run); site_words = a verb's
                       Discord line as the site prints it (no <t:…> stamp); open_seat_count / open_seats_words (a locked run's idle open seats said truly).
                       The lock's placement without a lock: event_board_preview(reg, rs, first, parts=, stop=, counts=) = every OPEN raid through
                       _solve_run with the lock's strategy and budget, `earlier` = the earlier raids' locked or previewed rosters (PURE; a raid nobody
                       joined drawn empty) — behind earlier_counts(reg, rs, ev, stop=) (Preview lock and Auto-fill on a later raid); autofill(reg, rs,
                       ev, earlier=, stop=) → (layout, auto names) = Auto-fill groups' solve (the lock's _solve_run, whatif off, on a copy whose board
                       keeps only the HAND placements: ev.auto_placed taken off first; PURE); RaidEvent.placed_by {name: officer}, drag_pins {uid:
                       {by, was, now}} and auto_placed [names Auto-fill placed] (all cleared at the lock) via record_moves (a move from another surface
                       clears whose it was; any moved name leaves auto_placed), own_moves / own_pins / undo_rev; open_seat_roles (a locked roster's).
                       prune_board(ev) = in EVERY RaidStore.save of an open run: a placed name no longer Join, pinned out, or whose character|spec
                       changed since placed (RaidEvent.placed_as) leaves the board in that save; unplace_member(rs, uid, why) (left the server);
                       board_complete(reg, rs, p) → a later raid's Auto-fill counts that board (event_board_preview(complete_boards=)). The lock: lock_forces(reg, rs, ev, players) → LockForces = THE builder of who is kept on and
                       off (_solve_run's force_in / force_out, the lock box's count, kept_off_ids — role_plan never asks a kept-off name);
                       ALL_KEPT_OFF raised before solving when every joiner is kept off; _solve_run's later-raid branch no longer reuses the groups per
                       roster as the part number (a hand place past a later raid's first groups reaches the solve); SOLVER_WORDS + conflict_sentence →
                       solver_error_text (a timeout checked before infeasible; never an exception's words; a stored sentence reads as itself).
                       Role switches: event_switches(reg, rs, first, viewer=) → {short [{raid, roles, why}], rows, revs} composed from each live raid's
                       role_plan (before the lock a row merges across raids — switch_raids —, after it is its raid's own, the officer's own row left
                       out); switch_box_for → (box, button Switch | Answer for them | Ask); _short_why; spec_role; set_signup(spec=) refuses SPEC_SEATED
                       through seated_switch_refusal(reg, ev, m, c, spec) = THE guard (member_action_box reads it too): an explicit spec of
                       ANOTHER ROLE than the seat on the locked roster (same role allowed; another character's own spec = the reseat);
                       no spec on the seated character keeps the signup's switched spec; a row whose box refuses is left out (every surface); pick_asks / send_batch(seats=); member_state_rev
                       The final fix wave (§5.67 phase 4): earlier_counts(reg, rs, ev) = a later raid's `earlier` while a raid before it is OPEN
                       (Preview lock on a later raid solves the earlier raids through event_board_preview; BLOCKING); open_schedule matches a
                       schedule's night at ANY distance (never a one-off beside the run the schedule opens later); open_refusal(reg, rs, instance,
                       start, schedule) = THE on-demand open refusal (open_run_and_post, open_preview, plain text, /raid open): NO horizon (the
                       owner, 2026-11-24 — a night weeks ahead opens as its schedule's run; open_row_refusal = a closed night only), a one-off / pickup template on a schedule's night = SCHEDULE_NIGHT, a live one-off already on
                       it = NIGHT_TAKEN (named by registry.schedule_name, never an id); one_off_at = a live one-off at the minute among EVERY live
                       first part (live_at answers only the newest); is_night_of(reg, instance, start, sid); board_move_kind (move | roster | swap |
                       substitute | release on the board as it stands); lock_failure_kind (a stored lock_error in old words = the same failure,
                       no second run-update line); open_run holds config_lock for the whole open (a token's apply re-describes under it)
  discord_raid.py      RaidMixin on the client: send_member_dm (test puppets → the tester), set_answer / call_out (the one path for an answer; `by` = a
                       display name to read, `by_id` = the actor's Discord id decides "their own" — never a name compare; seats found by id, ev.seat_of(member)), lock_run → roster
                       cards + Confirm/Can't DMs, run_fill DMs, drop_seated, open/cancel runs, after_absence ripple; re-exports the raid_* names below.
                       Phase 2 (§5.39): send_member_card + edit_ask_card (an outcome edits the ask's DM card — never an FYI DM; seat_released),
                       record_placement (check-and-set on the loop), lock_run resumable (lock_seats + confirmations_sent_at; recover_lock in the scheduler),
                       lock_preview, cancel_run(notify) under board_lock (no DMs), reopen_run, remind (the older card goes dead), set_autofill_paused, withdraw_sent_ask;
                       sheets_restore = on_ready's first boot redraws every live sheet in place (refresh_sheet), so a look change reaches posted sheets;
                       every writer of ev.fill_asks holds plan_lock (_send_asks is the one sender)
                       lock_preview solves with rc.run_strategy and the lock's own budget (rc.SOLVE_TIME_LIMIT_S), so it shows the roster Lock builds;
                       on a later raid whose first raid is open it solves the earlier raids too (rc.earlier_counts, about 25 s; the Discord
                       working line says up to half a minute).
                       Lineups (§5.62, §5.67 phase 3): lock_run on a later raid while an earlier one is open → LATER_PART_LOCK; lock_parts = part 0's
                       lock builds every later raid in order (a short role never stops one; it stops at the first part that stays open with its
                       lock_error, the scheduler retries in order; one ops warn per part that raises); after_lead_out, drop_seated cascade from part 0
                       only; stay_for_follow; cancel_run on ANY part cancels every live raid of the run (a later part redirects to part 0 while part 0
                       is live; one audit line naming every key); open_run_and_post(…, lineup=).
                       Reschedule / Edit lineup (§5.67 phase 3; officers, decided LIVE — resolve_role at PRIV_LIVE; by_id None = code for the guild;
                       preview → the consequence sentences; `rev` = rc.event_rev, else RUN_CHANGED): reschedule_event(reg, rs, ev, start=, timing=, …)
                       (MOVE_OFFICER_ONLY; under every part's board_lock, the refusal re-checked; the sheet edited in place under its message id,
                       refresh_cards per part, every open confirmation and fill-ask card re-rendered through edit_live_card, a `moved` notice per
                       answered_ids, one run-thread line naming the raid, one ops.audited line; NO DM) · edit_lineup(reg, rs, ev, lineup, …)
                       (LINEUP_OFFICER_ONLY; plan_lineup + apply_lineup under the board locks; a removed raid's pending asks closed under its plan_lock
                       and their cards edited "… was taken out of the … run, so this ask is closed."; the `moved` notice when a kept raid's start
                       changes; NO DM). cleanup_ephemeral drops a first part's dict with no run file after rc.DICT_WITHOUT_RUN_HOURS
                       Phase 4 (§5.67): solving(reg, key, lock=) = the solver's single flight per run AND the lock first: lock=True (lock_run) stops
                       every running Auto-fill (stop_all), a preview only its event's (stop_event) — wrapped so an auto-fill fault never touches the lock; board_move(…, by_id=) (before the lock
                       rc.apply_move on the officers' layout: a drop on Not placed only un-places — no pin —, a place in a group lifts a kept-off pin;
                       recorded per officer); _write_board(…, auto=) (Auto-fill's write: ev.auto_placed; save_board empties it); undo_moves(reg,
                       rs, ev, by_id, by, rev=) = Undo my moves (only that officer's placements and the pins their drags changed, rc.undo_rev checked);
                       set_answer(…, spec=, via=) (a role switch: the spec on the answer for the whole evening, a notice, one audited line);
                       send_plan(…, seats=) (one ask: the Events page's Switch after the lock); open_run_and_post(…, lock_hours=, first_rosters=);
                       the lock's failure line names its conflict (escaped for Discord)
  event_view.py        the Events page's read model (§5.67 phase 4), PURE builders over the existing readers (nothing solves, writes or composes
                       a fact of its own; words from PAGE_WORDS; a run key only as an id): event_list(reg, rs, now, offset) → {open (open AND locked,
                       soonest first), coming (coming_nights: each active schedule's next COMING_PER_SCHEDULE 3 nights with no event, through
                       rc.next_runs_of), history (HISTORY_PAGE 20, newest first), history_more, tz_words}, a row {key, title, subtitle, state, chip}; test
                       runs (rc.rehearsal_run) and dropped parts never listed; one rc.occurrence_index per request. event_json(reg, rs, ev, viewer,
                       sheet=, autofill=) — any part's key answers as its event: details (Lineup, Comes from, Priority, Confirmation, Sheet opened, Locks,
                       Signup sheet; locked: Confirmed, Open seats; _night_facts / _discord_links = the raid-night facts read from each raid's ev_json:
                       Reminder, Confirm by, Confirmations pending, Open seats per raid, Reminded, Automatic asks paused by, Also on another event, Away
                       that day, the officer card / updates thread / loot thread links), signups (counts + rows; none = rc.pool_unanswered), parts
                       (_part_view: label, rosters_of, placed_of (names on the officers' board), short (rc.needs), sheet = ev_json (open seats only
                       when locked), board_state open | locked, autofill {working, words, failed} | None, mine, undo_box, layout, strategy — nothing
                       solves), activity, boxes {lock, cancel, reschedule}, actions (+ clear: More › Clear the board), switches (rc.event_switches), lineup,
                       timing (rc.run_timing), people, characters, strategies, test_chip, today, start_day / start_time, locks, edit (edit_options: Edit
                       event's fields NOW — every one before the lock, EDIT_AFTER_LOCK after —, values, splits per raid, hours, max_weight; Details
                       read the event's own: Priority, Confirmation, Open seats after the lock, Reminder, Confirm by); coming_json (Details: Sheet
                       opens, Locks — no horizon, so can_open unless the night closed —, why, open, box 11 (says the sheet stays up and the schedule won't open it again); ComingGone COMING_OPENED (with the key) |
                       COMING_GONE); new_event_options (raids, lineups to copy — no pickup templates —, NEW_EVENT_LOCKS (2, 24), lock_choices, the
                       guild's date), new_event_view (What this makes; SCHEDULED_NIGHT), lineup_edit_json (refused through rc.lineup_fixed) /
                       lineup_draft_json (nothing written), seats_words, tz_words, character_rows. The routes run these in a thread
  event_board.py       Auto-fill groups (§5.67, the owner's tweaks C): the Events board before the lock is the OFFICERS' layout and nothing
                       solves until the button. AutoFill (autofill_of(bot)), keyed (guild key, raid key): start(reg, rs, ev, by=, by_id=, via=) refuses
                       in one sentence (PAGE_WORDS autofill_locked / autofill_nobody / autofill_busy while a lock-side solve runs) or starts ONE task
                       (a second press gets it) → result None (written) | the sentence; solves on a PREVIEW_WORKERS 1 executor over snapshot / frozen
                       views taken on the loop (rc.earlier_counts then rc.autofill); writes under the raid's board lock only on the board it solved
                       from (inputs(): every raid's rc.solve_inputs up to it + auto_placed; else autofill_changed), when no lock or preview
                       stopped it (autofill_stopped / autofill_stopped_preview) and when it differs from the board (else autofill_same); a
                       failure = rc.solver_error_text; state(reg, ev) = the page's {working, words, failed} (an outcome shown while the board is
                       unchanged); AUTOFILL_LISTENERS → publish('run:<raid key>') at start and end; one ops.audited line per write
  schedule_view.py     the Schedules page's read model (§5.67 phase 4), PURE: schedule_list(reg) → {active, paused (each {id, name, repeats}),
                       reserved_ids (RESERVED_SCHEDULE_IDS: the editor holds no copy), orphans (a removed schedule's leftover regulars with box 18),
                       editor (editor_options: the palette with lockout_days / weights / timing_suggestion, pickup defaults, lineups, hour choices in
                       words, presets, split_policies, MAX_WEIGHT 6)}; schedule_json(reg, rs, sid, now) → {id, name, active, chip, kind, gone
                       (rc.gone_sentence + GONE_TAIL when a lineup raid left the game), details (Repeats — repeats_words —, First event / Next event if
                       resumed, Priority, Confirmation, Open seats after the lock, Answers), lineup (seats None for a gone raid), timing, next
                       (NEXT_EVENTS 3: starts, opens, locks, occurrence, the event's key) + next_empty, regulars (_regulars: count vs seats, short,
                       rc.series_forecast — if_resumed on a paused one — as forecast rows with can_open / why for Open early, rows with box 18 + the
                       record's rev; None for a pickup), people, boxes {pause | resume, remove}, stored (stored_entry, the editor's machine form), rev
                       (config_rev schedule:<id>)}; RegistryError NO_SCHEDULE for an unknown id
  raid_page.py         the Raids page's read model (§5.67 phase 4), PURE: raid_row(reg, rs, rid) (/api/raids `page`) → details (Size — shown, never
                       edited —, Groups, Lockout, Length, First opens, Played in with each schedule's link), make_up (minimum_rows: Registry.minimums
                       ALONE, `own` = the guild set it), make_up_empty, priority bars, used_when / one_off_rule, open_events, boxes.reset;
                       readiness_view(reg, rid, r, players) (/api/readiness `view`) → who_could_play, the same make-up rows with have + level, level and
                       standing_why from standing() = exactly the printed rows (headcount, tank / healer, each minimum; the buff matrix's class wants
                       stay out)
  raid_views.py        pure rendering: sheet_message (the signup sheet is an EMBED: inline fields are the only columns Discord has — a class per column open, a group per column locked, one member per line) / health_layout / lock_layout / confirm_layout / fill_layout / closed_layout (Discord layout components:
                       container/section/thumbnail/text/separators/buttons, real icons as app emojis via discord_bot.ensure_icon_emojis → ico()),
                       event_url(reg, ev) = THE address of an event on the site ({web}/app/events/<first raid's key>?g=<guild>, None off http(s)):
                       the sheet's title in every state, Open the board on the health / lock cards, Open the event on the closed card, the test bench's
                       sandbox line — no Discord module builds an event address by hand (tests/test_event_links.py);
                       run_times, run_label/run_title/clock12, gaps_text/ask_line, leftover_block, split_away (the sheet's Away row); BotProto = what the mixins expect from the client;
                       the loot council: loot_layout (session card) / batch_layout + loot_chunks (proposal cards) / item_layout (the ephemeral item card) /
                       loot_closed_layout / loot_stub_layout, loot_status_text + presence_line (the companion chip), why_not_text;
                       boss_lines = the lock card's "☠ Bosses 3 / 6" + "All bosses down — End the run?" (night_actions_row's End reads "End the run")
  raid_buttons.py      RUN_WORDS = THE label of every run verb (RunButton.LABELS is built from it; /api/meta.run_labels — design §5.56 phase 2);
                       persistent DynamicItem buttons: SignupButton (Join/Bench/No thanks/Can't make it + character picker), FillButton, PlaceButton,
                       RunButton (officer Lock/Fill/Loot/Cancel on the cards; the lock card's second row Remind/Pause/End; the closed card's
                       Reopen; lock_decide / cancel_decide / remind_decide / end_decide = THE lock, cancel, remind and end screens: the server's
                       box through Wizard.decide_box — cq.lock_box (Lock · Preview lock: the same box with the roster as decide_box's embed),
                       cq.cancel_box (Cancel event · Cancel event with a note = reason + Tell the channel? · a plain press:
                       no reason, the line posted — D3), cq.remind_box (Send reminder), bot.end_box (LootMixin's) or rc.end_refusal's
                       refusal box (End the run); `refresh` = the same builder on the run as it stands), LootButton `loot.<gk>:<session>:<drop|->:<action>[:<arg>]`,
                       LootPick `lootpick.<gk>:…`, LootStartButton `lootstart.<gk>:open`; constructors take the guild key first (`raid.<gk>:…` ids; old ids still match,
                       g=None → guild_routing.resolve_button + each button's legacy matcher); raid_views ⇄ raid_buttons import each other at the bottom
  raid_scheduler.py    RaidSchedulerMixin: the minute loop (discord_armory.armory_tick → auctions → cleanup_ephemeral → _warn_gone_raids → open on cadence
                       → sync_stands_for + complete_parts per live first part → health → lock → expire → fill → close), calls RaidMixin. Open (§5.67
                       phase 3): rc.occurrences(lead=True), skipped when rc.event_of_occurrence finds that (schedule, occurrence) in any state — never
                       by a key, so a cancelled occurrence never opens again —, else open_run_and_post(schedule=); _warn_unopened / _warn_open_refused
                       keyed by (guild, schedule id, occurrence); _warn_gone_raids = one ops warn per (guild, schedule) per hour for a schedule naming a
                       raid the game lacks ("… opens no runs: … Change its raids on the Schedules page, or remove it there."); a later raid gets no
                       health card or lock of its own while a raid before it is open; an occurrence whose minute a live ONE-OFF holds
                       (rc.one_off_at) is skipped with one ops warn naming both (_warn_night_taken) — another schedule's run there never blocks it
  raid_commands.py     register_raid_commands: /raid open|sheet|health|lock|loot|distribute|end|set|cancel|reschedule|edit|lineup|list|out|fill
                       (reschedule / edit → flows_event, no arguments)
  web/api_me.py        install_me_api (called from api.py): the inbox routes — asks/notices on GET /api/me (api.py), POST /api/me/run/{key}/answer|out,
                       /api/me/fill/{key}, /api/me/ask/{id}, /api/me/notices/read (a member's own answers through the RaidMixin verbs, by_id = me);
                       officers: GET /api/admin/inbox|actions, POST /api/admin/action (rev → 409; `spec`; §5.67 phase 4: `preview` → {box} =
                       consequences.member_action_box for every action (fp over rc.member_state_rev; phase 5 Task 7), `seen` → 409 with
                       the fresh box, a refusal → 400 {error, box}); sheet_row_extras
  web/api_events.py    install_events_api (called from api.py; §5.67 phase 4) = thin callers of event_view, officers, each in a thread (GET
                       /api/events stays the live stream): GET /api/event-list?history_offset=, GET /api/event-new, GET /api/event/{key} (any part's
                       key; the officers' boards, each open raid's Auto-fill line from event_board.AutoFill.state — nothing solves; NO_EVENT 404
                       for a gone or dropped key), POST /api/event/{key}/clear (More › Clear the board: consequences.clear_board_box, preview / seen,
                       one save_board per open raid, pins untouched), GET /api/event/{key}/lineup +
                       POST it {lineup} (What this makes, nothing written — the Save is /api/run/{key}/lineup), GET /api/event/coming/{sid}/{occurrence}
                       (404 {error, key}), POST /api/run/{key}/switch {uid, character, spec, part, seat, option, rev, preview?, seen?} → one Role
                       switches row: before the lock RaidMixin.set_answer(spec=) on the first raid, after it send_plan(seats=[seat]) on its raid; a row
                       no longer listed → 409 {error: SWITCH_GONE, switches}; answers {message (rc.site_words), switches}. The page reloads on run:,
                       member and config
  web/api_schedules.py install_schedules_api (called from api.py; §5.67 phase 4), officers, in a thread: GET /api/schedules → schedule_list, GET
                       /api/schedules/{id} → schedule_json (404 NO_SCHEDULE_HERE). Writes stay elsewhere: POST /api/ops/change (kinds schedule /
                       schedule_remove, the box first) and POST /api/admin/raid/regulars by the schedule's id
```
