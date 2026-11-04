# Follow-on runs: a second raid the same night, from one signup — design

Date: 2026-11-04. Status: approved in chat (owner), spec for review. Design home when built: `docs/design.md` §5.62.

## 1. What the owner asked for

One message to the bot must set up a whole night and a weekly series:

> set up an ad hoc hyjal raid for 6:30 pst on Dec 10th with 10 man barrow deeps splits afterwards. also set up a
> scheduled hyjal raid for 6:30 pst every tuesday beginning december 15

Intended behaviour: **one signup** covers the 20-man Hyjal Summit and the two 10-man Barrow Deeps groups that follow
it; the weekly Hyjal posts itself from December 15 like any schedule. The weekly series carries no splits unless the
owner says so.

What exists today: a schedule's `rosters` splits ONE raid into N rosters of its own size from one sheet (§5.22,
§5.27); two different raids on one night are two schedules with two sheets, and the same-night conflict check
(`rc.conflicts`, ±4 h) treats anyone In on one as unavailable to fill the other. Nothing links two raids.

Decisions taken in chat (2026-11-04):

- **D1 One Join for the night.** Join on the one sheet means both halves. Leaving after the first raid is a callout on
  the follow-on alone. No new answer on the sheet.
- **D2 Surfaces.** Plain text and the site set a follow-on in this cut. The Discord `/raid open` and schedule wizards
  show one and don't set one yet (parity matrix, with the site path). MCP gets it through the same routes.
- **D3 Shape.** A linked second run, not legs inside one run: the follow-on stays an ordinary run for loot, cards,
  callouts, the board and the site; the link is one field each way.

## 2. The word

**Follow-on** — a second raid the same night, from the same signup, that starts when the first ends. The first raid is
the **lead**. Every label, card line, confirm sentence and manual line uses these two words (manual §1a). Never
"legs", "chained", "child", "linked run" or "phase" where a person reads.

## 3. The model

### 3.1 Config: `follow` on a schedule and on an ad hoc open

A schedule entry (`raids.<id>.schedules[]`, §5.27) gains an optional `follow`:

```yaml
follow: {raid: barrow_deeps, rosters: 2}
```

`SCHEDULE_FIELDS` gains `follow`; `schedules_of` fills `follow: None` when absent (the default is not stored).
Validation (`Registry.set_schedule`, `_check_schedules`): `raid` is a raid of the profile other than the schedule's own,
`rosters` 1–`MAX_ROSTERS_PER_SLOT`. A follow-on never has a follow-on of its own (one level; refused in words:
"A follow-on can't have one of its own").

An ad hoc open takes the same value once, for that run only (the ad hoc "schedule" is virtual; the value rides on the
open call, §4.1).

### 3.2 The run dict (`reg.config.rosters[]`, `ensure_run`)

The lead's run dict records `follow: {raid, rosters}` (copied from the schedule or the open call) so a later schedule
edit never changes a night in flight. The follow-on's run dict is created by the same `ensure_run` call, with:

- `key` = `run_key(follow.raid, follow_start, FOLLOW_SCHEDULE)` where `FOLLOW_SCHEDULE = "after"` is a reserved schedule
  id (like `ADHOC_SCHEDULE`), so a Barrow Deeps follow-on never collides with a Barrow Deeps run of its own at the same
  minute; `follow_start` = the lead's start + the LEAD raid's `duration_hours` (Hyjal 3 h → 9:30 PM for a 6:30 PM lead).
- `name` = "Barrow Deeps · after Hyjal Summit <local12 start>", `size` the follow raid's, `rosters` = `follow.rosters`,
  `kind` = the lead's kind, `series` = None (a follow-on has no regulars of its own), `confirm: False` always (§5.3),
  `follows` = the lead's run key, cadence hours copied from the lead's dict (its lock IS the lead's lock, §5).
- The lead's dict gets `followed_by` = the follow-on's key.

### 3.3 The run file (`RaidEvent`)

Two new fields: `follows: Optional[str]` (the lead's key, on the follow-on) and `followed_by: Optional[str]` (on the
lead). Helpers in `raidcycle`: `lead_of(rs, ev)`, `follow_of(rs, ev)` (the other run of the pair or None),
`is_follow(ev)`, `night_of(rs, ev)` = `(lead, follow)`.

**Signups are the lead's.** The follow-on's `signups` are *materialised* from the lead's, never answered directly:

- `RaidStore.save(lead)` re-materialises the follow-on (`rc.sync_follow(reg, rs, lead)`): every lead signup copied
  (same `Signup` rows: status, character, spec, pins are NOT copied — a pin is a decision on one run's roster), then
  each uid in the follow-on's `leaving` list becomes `out` with source `callout` ("leaving after Hyjal Summit"). The
  follow-on is saved when anything changed. The copy is persisted so every offline reader (the site, help, MCP, the
  scheduler on restart) sees the same answers without knowing about the link.
- `leaving: list[int]` on the follow-on's `RaidEvent` = the uids who called out of the follow-on alone (§6).
- `set_signup` on a follow-on raises `RegistryError("Answer on the <lead raid> sheet — it covers the whole night")`;
  the only writes to a follow-on's signups are `sync_follow` and the leave-early callout. The sheet buttons never
  reach it (it has no sheet); the site's answer routes, plain text's `run_answer` and MCP's `set_answer` get the
  refusal.
- `conflicts(rs, ev)` skips the other run of the pair.

The follow-on has its own `layout`, `pins`, `split_strategy`, `rosters`, `fill_asks`, `callouts`, `cards`,
`updates_thread_id`, `lock_*` and loot sessions — an ordinary run after the lock.

## 4. Opening

### 4.1 One open, two runs

`open_run(reg, rs, instance, start, …, follow=None)` and `RaidMixin.open_run_and_post(…, follow=None)`: `follow`
= `{raid, rosters}` for an ad hoc open; for a scheduled open it comes from `schedule_def(...)["schedule"]["follow"]`.
Opening the lead opens the follow-on in the same call (both run dicts in one config write, both run files, the lead's
sheet posted, NO sheet for the follow-on). `rc.refuse_past_start` and `RunKeyClash` checks run for both keys before
either is written. The scheduler's open step (`raid_scheduler`, `rc.upcoming(lead=True)`) changes nothing: it opens the
lead, and the lead opens its follow-on. `open_preview` (§5.39) adds the follow-on's line: "then Barrow Deeps ×2 at
9:30 PM".

### 4.2 The sheet (the lead's)

`raid_views.sheet_message`: title "Hyjal Summit, then Barrow Deeps ×2"; the description line adds "then Barrow Deeps
×2 at <t:unix:t>" after the start stamp. Seats advertised and the health check stay the lead's (size × its rosters).
`health_data` / the health card add ONE line when the night's joiners can't cover the follow-on's tank or healer
minimums × rosters: "Barrow Deeps follow-on: short 🛡 1" (icon + number, Iconography rule). `/raid health` the same.
The sheet's Join / Bench / No thanks / Can't make it answer the lead (and so the night).

### 4.3 The public schedule, the Raids page, the Me page

`/schedule` and `schedule_json` add "then Barrow Deeps ×2" to a schedule with a follow-on; `upcoming_rows` carries
`follow: {raid, name, rosters, size}`. The Rosters page lists the follow-on as its own run card directly under the lead
("after Hyjal Summit" in its header, no Join counts of its own: "answers on the Hyjal Summit sheet"). The Me page row
for the night reads "Hyjal Summit, then Barrow Deeps ×2 · <when>" with the member's answer once; after lock it shows
their seat on each (`rostered_runs` returns both runs; `inbox` groups them as one row).

## 5. The lock

### 5.1 One lock, two solves

`RaidMixin.lock_run(lead)` locks both, in this order, under the lead's `solving` + `board_lock` and then the
follow-on's: (1) the lead solves as today; (2) `rc.sync_follow` runs so the follow-on's signups match the final answers;
(3) the follow-on solves with `rc.propose(follow)` where `_solve_run` adds a **lead-rostered bonus**: every player
rostered on the lead gets `roster_bonus` weight `LEAD_SEAT_BONUS` (large enough to beat every seat weight combined, so
the two 10s are the 20 split in two whenever roles allow) and the follow-on's own board layout and pins stay hard, as
today. Roster count = `how_many_rosters(want=follow.rosters)` — bodies and role minimums still cap it. (4) Both flip to
`locked`; the lead's lock cards post, then the follow-on's ("Barrow Deeps · after Hyjal Summit · Roster 1 / 2"), in the
same roster channel, the follow-on's updates thread under its own first card.

A lock of the follow-on on its own (`/raid lock` on its key, the site, MCP) is refused while the lead is open: "Lock
Hyjal Summit — its lock builds the follow-on too". After the lead is locked, the follow-on re-locks on its own like
any locked run (an officer's re-propose after a yes).

### 5.2 Scheduled lock

The scheduler's lock step reads the lead only; the follow-on's `cutoff_hard_hours` equals the lead's, and
`raid_scheduler` skips a follow-on whose lead is live (`is_follow(ev) and lead_of(...)`), so it never locks first.
Recovery (`recover_lock`) treats each run on its own once locked.

### 5.3 Confirmations

A follow-on never confirms (`confirm: False` on its dict → `run_confirms` False): no Confirm / Can't DMs, no deadline.
On a recurring lead, the lead's Confirm covers the night; a **Can't make it** on the lead (DM card, Me page, `/raid out`
without a key) writes a callout on the lead AND a leave on the follow-on (`leaving` + `sync_follow`), so both seats
free. Nobody is told twice: the one outcome card edit is the lead's.

## 6. Leaving early, fill, the board

**Leaving after the lead** = a callout on the follow-on alone: the Me page's row offers "Leaving after Hyjal Summit"
(`POST /api/me/run/{follow key}/out`), `/raid out` with the follow-on's key, plain text `run_answer out` on the
follow-on (the one `run_answer` the follow-on accepts: value `out`), or an officer's removal on its board. It appends
the uid to `leaving`, writes the callout, frees the 10-man seat, and leaves the lead untouched. Its consequence
sentence: "You stay on the Hyjal Summit roster. Your Barrow Deeps place is freed. An officer may fill it." (Confirm
rule.) Undo: "I'm staying" on the follow-on's Me row (`rc.stay_for_follow`, its own verb — never `set_signup`) removes
the uid from `leaving` and re-materialises their lead answer; the seat is not restored (fill may have taken it) — the
fill engine offers it when short.

**Fill** on a locked follow-on works as today (`fill_candidates`: benched joiners first, then the rest of its order);
the pool is the night's joiners because the signups are the lead's. Nobody In on the lead is "busy" for the follow-on
(the conflict exemption) and the double-booked field never names the pair.

**The board** of the follow-on on the Rosters page is its own: before lock it starts empty (the lead's groups are not
copied: a 20-man's groups say nothing about two 10s); Auto-fill and Propose splits run on it with the lead bonus
(§5.1) so the preview shows the 20 split; after lock it is the roster. Pins on the follow-on are the follow-on's.

## 7. Cancel, end, reopen, cleanup

- **Cancel the lead** cancels the follow-on in the same verb (`cancel_run` → both, one audit line naming both;
  `cancel_effects` counts both runs' rostered). Consequence: "Hyjal Summit and its Barrow Deeps follow-on are cancelled.
  Rostered members are not told automatically. This can't be undone." (three sentences, Confirm rule). Cancelling the
  follow-on alone is allowed: "The Barrow Deeps follow-on is cancelled. Hyjal Summit stands." The lead's `followed_by`
  is cleared and its sheet title loses ", then …".
- **End**: each ends on its own (`end_run`); the loot council per roster of the follow-on uses the existing split keys
  (`<follow key>-r1`, `-r2`). `loot_may_close` and `loot_boss_down` work per run.
- **Reopen** (an END by mistake, within the hour; never a cancelled run — D19) reopens one run only: each run ends on
  its own, so each reopens on its own. A cancel is final for both.
- **Cleanup** of ephemeral run dicts removes the pair together (the follow-on's dict goes with the lead's).

## 8. Surfaces

### 8.1 Plain text (the owner's message)

`run_open` gains the follow-on in `text`: `"<raid id> x<rosters>"` (`barrow_deeps x2`; `x1` or bare id = one roster),
parsed in code by `configops.parse_follow(text)` (unknown raid / bad count → `RegistryError` in words). The schema text
tells the parser: `run_open: … text=<optional follow-on "<raid id> x<N>": a second raid the same night from the same
signup, starting when the first ends>`. `schedule_set` gets the setting `follow` with the same value grammar (`clear`
removes it). The owner's sentence parses as:

1. `run_open target=hyjal value="2026-12-10 18:30" field=adhoc text="barrow_deeps x2"`
2. `schedule_set target=hyjal field=tue.slots value="Tue 18:30"` (+ `tue.name`, `tue.first_open=2026-12-15 18:30`;
   one write, coalesced as today)

`describe()`: the open line adds "then Barrow Deeps ×2 at 9:30 PM"; the schedule diff shows `follow: none → Barrow
Deeps ×2`. `op_consequences`: a follow-on on a schedule with open runs says it reaches the next run only (as every
schedule edit does). `ops_from_fields(kind="schedule")` accepts `follow` (the site's Then row); `ops_from_fields` for
the site's ad hoc Open passes `text`.

### 8.2 The site

- Raids page, `ScheduleCard` editor: a **Then** row — a raid select (every other raid of the profile, "No follow-on")
  and a rosters select (1–4), saved in the one diff-only Save through `/api/ops/change` as `schedule_set … follow`.
  The card's read view shows "then Barrow Deeps ×2" under the schedule's slots.
- Rosters page, ad hoc **Open**: a Then field (same two selects) → `POST /api/raid/{rid}/open` body `follow`
  (`{raid, rosters}`), preview shows the follow-on line.
- Rosters page: the follow-on's run card under the lead (§4.3); its board and Propose splits as today.
- Me page: the night's row (§4.3) with "Leaving after Hyjal Summit" (§6).
- `/api/meta.run_labels` / `RUN_WORDS`: "Leaving after {lead}" (filled client-side like the board's name tokens).
- `api.ts` shapes: `Schedule.follow`, `Run.follows` / `followed_by` / `follow` (name, rosters), `Upcoming.follow`.

### 8.3 Discord

- The sheet and cards as §4.2 / §5.1. `/raid open`'s wizard and the schedule screen of `RaidConfigWizard` SHOW an
  existing follow-on on their confirm ("then Barrow Deeps ×2") and don't set one: `configops.PARITY_PENDING` entries
  `run_open.follow` and `schedule_set.follow` with the site path (D2).
- `/raid cancel` on a lead: the decide screen prints the pair's sentence (§7).
- `/raid out` with the follow-on's key = leaving early (§6); `/me view` lists the night as one line with both seats.

### 8.4 MCP

`open_run(…, follow=)` and `schedule_set(…, follow=)` pass the field through to the same routes; `get_run` carries
`follows` / `followed_by`. No new tool.

## 9. Help and the manual

`docs/manual.md`: §1a gets **Follow-on** and **Lead** (§2); the Rosters section gets "A second raid the same night"
(one signup, the lock builds both, leaving early, cancel); the officers' plain-text examples get the owner's sentence.
`help.guild_state`'s run lines say "then Barrow Deeps ×2" and the follow-on's own line "after Hyjal Summit".
`docs/design.md` §5.62 = this document as built; CLAUDE.md's raidcycle / discord_raid lines name the follow-on helpers.

## 10. Testing

`tests/test_follow_on.py` on a scratch copy of the demo fixtures (`GitStore(scratch, push=False)`):

- open a lead with `follow` → two run dicts and two run files, keys, start = lead + duration, `follows`/`followed_by`;
  no sheet for the follow-on; opening twice is idempotent; a past start refuses both.
- answers on the lead materialise on the follow-on (Join, Bench, No thanks, a character change); `set_signup` on the
  follow-on refuses; `conflicts` exempts the pair.
- lock: 24 joiners, Hyjal 20 + Barrow Deeps ×2 → the two 10s are exactly the lead's 20 whenever the role minimums allow
  (assert set equality), bench joiners fill when they don't; the follow-on never confirms; a lead Can't frees both.
- leaving early: `leaving` + callout frees one seat, the lead untouched; "I'm staying" clears it.
- cancel the lead → both cancelled, one audit line; cancel the follow-on alone → the lead stands; reopen rules.
- the scheduler never locks a follow-on before its lead.
- `tests/test_plain.py` (or the existing parser test module): the owner's sentence against a canned model reply →
  the two ops, `describe` lines, `parse_follow` cases (bad raid, x0, x5, same raid as the lead).
- `tests/test_confirm_readability.py`: every new sentence (leave early, cancel pair, cancel follow-on alone, the
  follow-on-can't-have-one refusal) through the lint.
- `tests/test_parity_matrix.py`: the two PARITY_PENDING entries; `tests/test_frontend_source.py` for the Then rows;
  `tests/test_copy.py` for the new words; web route tests for `/open` with `follow`, `/api/me/run/{key}/out` on a
  follow-on, `schedule_set follow` through `/api/ops/change`.

## 11. Out of scope (named so nobody builds them by accident)

A follow-on of a follow-on; a per-member "first raid only / splits only" answer (D1 — the leave-early callout covers
it); a follow-on of a different size that is not a split (e.g. a 40 after a 20 — the model allows it, nothing special
is built for it); Discord wizards that SET a follow-on (D2); copying the lead's groups onto the follow-on's board.
