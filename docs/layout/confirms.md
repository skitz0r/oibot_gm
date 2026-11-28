# Layout — every confirm sentence and box (the Confirm rule's generators), the member inbox

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  consequences.py      consequence(verb, reg, ev) for lock / cancel / clear / release, the board's move / roster / swap / substitute ({name} / {other} /
                       Channels (2026-11-27): channel_words (THE '#alt-signups' / "the guild's #raid-signups"), channel_move_sentence, channel_change_consequence (what moves · Nobody gets a DM. · You can change it back until the event ends.); reschedule_consequence answers a channels-only edit with it; mixed with timing it folds the move into the first sentence when that stays ≤ 20 words, else the box's diff line carries it — the timing box's three sentences (locks within a minute, seating, undo) always stand; event_edit_lines prints 'Signup sheet: #a → #b' (EDIT_LINE_ORDER ends with the three).
                       {to} tokens the board fills — shared.fillNames), remind and end (Reopen's window from rc.REOPEN_GRACE_HOURS; LootMixin.end_consequence
                       puts the council's part in front), regular_remove(reg, m, key): the ONE confirm sentence from code (design §5.33, §5.56 phase 3);
                       ev_json.consequences carries it to the site (the board's verbs on the Events page and the Test bench card), wizard.decide shows it in Discord
                       — under the Confirm rule (design §5.57): CONFIRM_DM, reopen_sentence, ended_line, series_words; rc.run_on / rc.refill_sentence /
                       rc.no_confirm_sentence / rc.open_rules are the shared words; tests/test_confirm_readability.py renders and lints every generator.
                       People (design §5.58): people_consequence(reg, rs, m, verb, by_id=) → retire / delete / main / spec / flex / rank / confirm / dm /
                       back / save _consequence ("your" on your own record, "<name>'s" then "they" for an officer; a refusal = RegistryError, one sentence);
                       RANK_CHOICES (never alt); CharWizard, /roster confirm and the Members routes' `preview` print exactly these;
                       absence_consequence = Add the absence's words (the absence previews' `sentences`, Me and Members)
                       The server's guild (armory link §5.1): armory_guild_consequence(reg, facts) = what the lookup found (name, faction, kind,
                       members, created — armory_created_words, never ISO) then what changes; armory_guild_clear_consequence (every character keeps its link)
                       The auction house (§5.65): market_consequence(reg, house) (a first pick · the same house: "nothing changes" — set_market then writes
                       nothing · another: "instead of the one picked before", never the old name), market_clear_consequence; PRICES_OFF = the guild
                       link's undo sentence when a relink / unlink clears the market (T2-a: still three sentences)
                       A character's link (§5.2): armory_link_consequence(reg, m, c, found, by_id) (what the armory or the roster shows, the name and the
                       professions it takes — from level 10 when found on the roster), armory_unlink_consequence(reg, m, c, by_id) (kept by hand from now
                       on), profession_sync_consequence(on, label, current=) (on: the hand edits are replaced and can't be brought back; already so → one sentence)
                       A move (§5.4): move_consequence(reg, m_from, c, m_to) (≤ 3: it moves with its loot history, attendance [and regular
                       spots it keeps] — "with everything on its record" when long names would pass twenty words; a regular spot it gives up / whose main changes, only when they apply (Registry.move_effects); both see a notice,
                       left out when both special cases take the room; no "move it back" line)
                       Runs with their own identity (§5.67 phase 3): reschedule_consequence(reg, rs, ev, start, timing) (what moves or the run's own
                       timing — built from the keys that change —, who answered sees a `moved` notice, then the first of: locks now · a conflict ·
                       away answers · that night's schedule run is this one · undo); lineup_consequence(reg, rs, ev, lineup) (the losses of a removed
                       raid ALWAYS counted — an assert —, the new start times degrading to the clock or "see their new start times", C9, the
                       schedule note when it has room) over lineup_words (also edit_lineup's done / thread lines); later_places_sentence(reg, held) =
                       the ONE "Your X and Y places go too." (the Me page's Can't, /raid out); before_first_open_sentence(…, only=) (C9);
                       schedule_keeps_sentence(n) (C10, the Discord raid wizard's); consequence("cancel", …, rs=) names every live raid of the
                       run (first_closed: no "whole", no sheet); raid_reset_consequence(reg, raid, unsaved=) = the raid wizard's Reset
                       THE confirmation box (§5.67 phase 4, spec §7b): box(title, sentences, verb, *, danger, diff, rev) → {title, sentences, verb,
                       danger, diff, fp} (fp = sha1 of title + diff + sentences + rev, 12 hex; verb None = a refusal, Back alone; red only when it can't
                       be undone); refusal_box / refusal_kind (the kind a built refusal carries — rc.Refusal past | clash — or the constant it equals,
                       never words inside it) / refusal_box_for → REFUSAL_TITLES; the twenty boxes: lock_box(reg, ev, rs, fill=False) (1–2; issue 14: lock_choice = any roster of any open raid has an
                       open slot (rc.lock_open_slots; the owner's ruling, fix round 1) → the box carries `choice` {label: PAGE_WORDS["lock_fill"], value: fill} and its fp covers `fill`;
                       lock_fill(reg, ev, rs, fill) = the `fill` a person's lock passes (their choice, else True on a complete board — every
                       surface reads it); fill=False = _lock_as_it_stands: S1 _slots_sentence (rc.lock_open_slots per open raid; several
                       raids: the raid named only when one has them, else the total) + _with_left (", and X is not rostered"), S2
                       _rostered_told (only the board rostered + who is told: DMs off, a test event, no confirmation, several raids), S3
                       LOCK_PAUSE while Automated Fill Requests are on and not paused, else the deadline; an empty board is the officer's
                       choice (F3): one raid → S2 is rc.LOCK_EMPTY_WARN_ONE, a lineup → S1 is rc.lock_empty_warning (in place of the slots), Lock
                       pressable; fill=True = _lock_ticked: where the lock places nobody
                       (every raid full, or nobody answered) slots + who is told + LOCK_ASKS (asks on) or the deadline, else the slots
                       sentence (only when every raid has someone on every roster it expects) + _lock; no open slot = _lock, no choice. _lock_told /
                       _lock_deadline (rc.late_confirm_by, as the lock) are shared by both. After an unlock (issue 14 Task 4 fix round 1)
                       who is told reads _confirm_plan = rc.confirm_plan, the lock fan-out's own reader: _newly_told counts only the newly
                       rostered ("nobody new is asked to confirm"; a lineup's "for the first raid"), _lock_told's rule / count, and
                       _closes ("<name> is left off the roster, so their Confirm ask closes.") takes S3 in place of the pause / deadline;
                       a first lock (no earlier asks) reads as before. A lineup's first-lock S2 names the first raid's board (only it
                       confirms); _with_left's count says "kept on the roster … left off". The head states a RULE true whatever the solve decides, never a predicted
                       number (Task 6 fix): _LockFacts per open raid from rc.lock_forces + rc.lock_seat_count (full = every seat of every
                       roster the lock builds placed, or nobody left to place, no auto-placed Bench name, and every kept-on name on the
                       board; a kept-on Bench answer off the board makes the head name the bench) → key: "The board becomes the
                       roster…" (+ ", so its Bench answer is rostered ahead of people who joined" when a hand-placed one leaves a joiner off) ·
                       "A Bench answer an officer placed or pinned is rostered ahead of people who joined." (joined ≥ seats) · _short_first
                       (rc.bench_short_roles: "If those who joined cannot meet the healer minimum, a healer on the bench is rostered
                       ahead of them.") · AUTO_AGAIN (a Bench place decided again: Auto-fill, a whole-board write, or a placed joiner who answered Bench) · "… from those who joined, then from the bench." — raids that differ get the rule true of
                       all (losses first); the DM sentence counts only when the board is full (the board's number), else "Everyone rostered
                       gets …" / "… ask, as a DM or on the site." with DMs off; the fp covers _lock_rev (event_rev + each open raid's
                       board_rev, auto_placed and answers); back_sentence / _bank_word / undo_box(bench=) = where names taken off the
                       groups go (Not placed, or Bench for a Bench answer); lock_refusal = everyone kept off), reschedule_box (3; `edit=True` = Edit event's box, the owner's tweaks B:
                       "Change <event>?" + Save + event_edit_lines = 'label: old → new' in PAGE_WORDS schedule_fields / priority / split words,
                       event_value_words; TIMING_NAMES carries confirmation, seating, priority, split) + reschedule_pick_box, lineup_box (4) + lineup_diff_lines,
                       cancel_box (5, red; built for rc.cancel_target, so a part-ended event's box names the raids still to play), switch_box (6–7: locked=, swap=, own=,
                       replaces= the raids whose own switch a whole-event switch replaces — its last sentence), raid_switch_box(reg, rs, part, m,
                       character, spec, *, own, rev) = a switch for ONE raid (issue 15: what they play there and stay for the others,
                       RAID_SWITCH_TOLD, the undo; the answer's own = the way back; refusals rc.raid_switch_refusal / RAID_SWITCH_SAME;
                       `status` = a Join / Bench the press records too: "Nyx joins as Shadow and plays Holy for Barrow Deeps."), answer_box (8, inbox.action_lines) = a thin call of
                       member_action_box(reg, ev, m, key, character, spec, *, by_id, rev, rs=None — with `rs` a Join with a spec says which raid's
                       own switch it replaces: action_lines(replaces=)) = THE box of whatever an officer records for
                       someone (/raid set's SetWizard, POST /api/admin/action's preview / seen, the row ⋮ menu): action_lines under
                       MEMBER_ACTION_WORDS' title naming them + verb (answers keep "Answer for someone" / "Record answer"), never red;
                       raises RegistryError for what the write refuses (no_longer_applies, a spec not played); after the lock an answer for
                       someone is the quiet path (inbox.action_lines: QUIET_TOLD "Nobody gets a DM.", Join → "waits in Not rostered");
                       a seated Play another character of another role says the move first (inbox.action_lines), undo_box / undo_box_for (9),
                       the board's consequence("swap") after the lock (10), open_now_box (11) + open_box_for(reg, rid, p, here=) = THE box 11 from
                       rc.open_preview (/raid open and both open routes; rev = the occurrence; the later raids keep their own `starts`, as
                       coming_json's lineup; OPEN_UNREAD when the lead or lock can't be read; event_view.coming_json passes the same rev;
                       here = /raid open asked in a server channel → no_channel_sentence's "posts in this channel" — the site never passes it), move_past_sentence / PAST_TAIL (12),
                       schedule_create_box / schedule_save_box / schedule_pause_box / schedule_resume_box / schedule_remove_box (13–17; 14 says
                       "keeps its timing" only for a time setting, "keeps its lineup" for lineup / rosters), regular_remove_box (18), raid_save_box
                       (19, open_events_of_raid), leave_box / leave_unsaved_sentence / LEAVE_TEMPLATE (20, /api/meta.leave); + raid_reset_box,
                       the board after the lock (2026-11-27, the quiet path): _board / _release say what changes, QUIET_TOLD, QUIET_UNDO ("Undo
                       puts the roster back as it was."), _off_word = the list someone taken off waits in; UNDO_LOCKED + undo_box_for's locked
                       branch (the officer's one-step Undo); CLEAR_LOCKED / STRATEGY_LOCKED / PROPOSE_LOCKED name Unlock; switch_box(locked,
                       swap) = Switch at once for someone rostered, Ask for someone not; raid_switch_box on a locked raid tells nobody; every
                       switch's way back SWITCH_BACK_OWN / SWITCH_BACK_THEM ("until the event starts");
                       strategy_box (More › Split strategy, fp = rc.board_rev + the split the raid has now) and strategy_refusal = STRATEGY_LOCKED
                       after the lock on EVERY surface (the route, MCP, plain text's run_strategy in the diff and at apply); the final fix wave's
                       remind_box (POST /api/run/{key}/remind), unlock_box(reg, ev, rs) (issue 14: S1 open signup with the rosters on the board —
                       "The whole event…" with several raids; S2 _unlock_told: "Nobody gets a DM", then the fill asks it withdraws, else the
                       confirmation asks that stay open — unlock_facts reads both BEFORE the unlock, the fill side as distinct people; S3 UNLOCK_AGAIN; verb UNLOCK_VERB; rc.unlock_refusal
                       → the refusal box; POST /api/run/{key}/unlock, unlock_decide, run_unlock's review), start_now_box(reg, ev, rs) (issue 14
                       Task 6: S1 START_NOW_HEAD — the event starts this minute (later raids move up) and ends by itself at
                       start_now_close (the latest run_close_at moved by the same amount; left out of the fp, it moves each minute); S2 _start_now_told: who
                       answered gets a notice, then the confirmation asks that keep their deadline, else "nobody gets a DM" — start_now_facts
                       reads them BEFORE; S3 START_NOW_FIXED; verb START_NOW_VERB; rc.start_now_refusal → the refusal box; POST
                       /api/run/{key}/start, start_decide, run_start's review), end_box (LootMixin.end_box builds it from end_sentences, its fp covering the councils; POST /api/loot/{run}/end) and
                       board_move_box (over rc.board_move_kind: a locked board's Move to / swap / take off, POST /api/run/{key}/move);
                       event_title, count_words.
                       A route takes `preview` → {box} and `seen` (the fp pressed) → 409 {error: CONFIRM_WORDS changed, box: fresh}, nothing written;
                       a refusal → 400 {error, box}. lineup_consequence counts a lowered roster count's dropped board places (rc.roster_trim), losses first
  inbox.py             the member inbox (design §5.40): asks_of (every open ask, wherever it waits: dm | web), notices_of / inbox_lines
                       (Registry.notify = the outcome half of the DM rule), DM_ON/OFF_SENTENCE; officers on behalf: action_consequence(spec=, by_id=) / action_lines
                       (the same sentences as a list: consequences.member_action_box) / no_longer_applies + do_member_action(spec=) over rc.member_actions (the SetWizard, the row ⋮ menu, POST /api/admin/action); pin_write / pin_member =
                       THE pin write (every surface; re-renders the sheet's pin footer). Never a DM from here
```
