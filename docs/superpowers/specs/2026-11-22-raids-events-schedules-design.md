# Raids, events and schedules — design

Date: 2026-11-22. Status: approved by the owner 2026-11-22 ("clear to proceed"), with the Discord confirm screens added to scope.
Builds on: schedules (`docs/design.md` §5.27), recurring and ad hoc runs (§5.30), the Raids and Rosters pages
(§5.54, §5.56), follow-on runs (§5.62, `docs/superpowers/specs/2026-11-04-follow-on-runs-design.md`).
Design home when built: `docs/design.md` (a new §5.67); §5.27, §5.30 and §5.62 get a pointer to it.

## 1. Why

Today a schedule belongs to one raid. An evening of Hyjal Summit followed by two Barrow Deeps rosters is stored as a
Hyjal run with a "follow-on" attached, and every page is organised by raid. That shape leaks:

- The Barrow Deeps raid card shows a run it does not own, labelled "ad hoc", and an empty Regulars tab.
- A raid card's "When it runs" block mixes three kinds of row (a live run, a run with no sheet yet, the raid's
  first-open date) under one look, and the first-open row is worded as a sheet opening, which it is not.
- A raid's settings hold both what the instance is (size, make-up) and when the guild plays it (run times, sheet
  timing), so "the Tuesday night" has no home of its own.
- A run cannot move: its identity is built from its raid, date and time.

The owner's direction (2026-11-22): raids and schedules must be distinct things. Scheduling uses any number of
raids and may recur. A raid keeps only what is true of it whenever it is played.

## 2. The three things

- **Raid** — a catalogue entry. What the instance is. Nobody joins a raid.
- **Event** — one dated occasion people join: a lineup of one or more raids in order, a start time, one signup
  sheet, one answer per person, a roster per raid in the lineup, one lock. "Event" replaces "run" as the word.
- **Schedule** — a rule that opens events on its own: a lineup, when it repeats, the sheet timing, its regulars.

A **one-off** is an event an officer started by hand. It has no schedule behind it. "Ad hoc schedule" and
"pickup template" stop being concepts.

A **lineup** is an ordered list of entries `{raid, rosters, starts}`: which raid, how many rosters of it (1–4),
and when it starts relative to the event's start (the first entry is always 0). A lineup of one raid is the
ordinary case.

## 3. Decisions (the owner's, 2026-11-22)

- **D1 Raids, events, schedules** are the three nouns, as defined in §2.
- **D2 A raid holds no times the guild chose.** Desired make-up, size, and the like stay; run times and sheet
  timing leave.
- **D3 An event's lineup may hold any number of raids,** and a schedule's too.
- **D4 A schedule may recur; a one-off is not a schedule.** The owner's own case: an ad hoc Hyjal → Barrow Deeps
  event for week 0 (already open), then the same 20-player → two 10-player evening every Tuesday.
- **D5 One answer covers the whole evening.** Nobody answers per raid.
- **D6 Timing seeds from the schedule; the event owns its copy.** Once an event exists, its start and its sheet
  timing are edited on the event alone, which is how an officer reschedules one week.
- **D7 Answers stand when an event moves.** Nobody is asked again.
- **D8 Officers create, edit, pause and remove schedules,** as well as the owner. (Schedules are owner-only today.)
- **D9 Readiness lives inside the Raids page,** as one of three views of a raid (Rules · Priority · Readiness, §7a).
  There is no separate Readiness page; its address redirects.
- **D10 One raid per lineup step.** A step may split into several rosters of that raid; two different raids never
  run side by side.
- **D11 Every page is a list on the left and one item in the main view** (events, schedules, raids), not a column
  of cards.
- **D12 Every fact has a label and a section.** No run-on sublines ("first come, first rostered · nobody is asked
  to confirm"). New things are made with "New event" / "New schedule". No button label ends in an ellipsis.
- **D13 The board is always shown** for an open event, with whoever has joined. No "Open the board" step.
- **D14 Who could cover a short role is a list of one-line switches** ("Nyx: Shadow → Holy", or a change of
  character), one list for every role and raid of the event, in its own collapsible section.
- **D15 Lineups are built by drag and drop,** as a graph of steps. Editing a schedule is one page with one Save.
- **D16 One number per role or class: its minimum.** "Limits (owner)" and "aims (officers)" are gone, and so is
  the permission split: officers edit everything on a raid, its size, lockout, length and first-open date included.
- **D17 "Priority" is the word** for who gets a seat when more join than there are seats, on every page.
- **D18 Confirmations follow one set of rules on the site AND in Discord** (§7b). The owner reviewed the twenty boxes
  of the three pages on the mockup canvas and accepted them; Discord's confirm screens are brought to the same rules.

Calls made by the designer (not yet seen by the owner unless marked; each can be overruled):

- **C1 (seen)** Lockout length and the first-open date stay on the raid. They are game facts, not guild choices.
- **C2 (seen)** Regulars belong to a schedule: a regular of the Tuesday schedule is a regular of the whole evening.
- **C3 (seen)** Pages: Raids = the catalogue, with Readiness as its second tab (D9); Schedules = new; Rosters = events only, soonest first.
- **C4 (seen)** A move sends no DM. The Discord sheet is edited to the new time and everyone who answered gets a
  notice on their Me page (the DM rule).
- **C5 (seen)** The officers' after-lock exception stays: someone can be taken off one raid of the lineup (leaves
  after the first, or joins only for a later one). It is never offered on the sheet.
- **C6** Seat weights ("Priority", D17) stay on the raid as the default, and a schedule may set its own, as today.
- **C7** There is no guild-wide timing block. A new schedule's timing (sheet opens, reminder, lock, confirm-by,
  fill-ask wait, and the switches for reminder, autofill, opening DM, confirmations) is SEEDED from the game
  profile's suggestion for its first lineup raid (`profiles/<v>/raids.yaml` keeps those numbers: game version is
  data) and is the schedule's own from then on. A one-off uses the first-come values it uses today. A guild's
  raid-level timing settings stop existing: no raid card shows or edits one.
- **C8** A schedule skips an occurrence when ANY raid in its lineup has a first-open date after that occurrence's
  start time for that raid.
- **C9** Starting a one-off before a lineup raid's first-open date is allowed. The confirm says so in one
  sentence on every surface (today only Discord's open flow says it, `flows_raid.py`). An event already open is
  never skipped or closed by C8 or C9.
- **C10** A schedule edit never reaches an event that already has a sheet. The schedule's screen says how many
  open events keep their old values.
- **C11** A moved event of a schedule still counts as that schedule's occurrence for its original week. The
  schedule does not open a second event for the slot it left, and it still opens its later occurrences on time
  even when the moved event now sits in their week: the scheduler matches by the stamped `occurrence`, never by
  where an event sits. Lockout and conflict checks read the event's ACTUAL start. A move is refused only into the
  past, or onto the same minute as another live event with the same first raid. A conflict it creates is said in
  the confirm, as opening one says it today.
- **C17** Who may: officers start, reschedule, re-time, edit the lineup of, lock and cancel an EVENT (what they do
  with runs today), and SCHEDULES too (D8): the schedule verbs, routes, wizards, plain-text ops and MCP tools move
  from the owner tier to the officer tier. So does everything on a raid (D16).
- **C18** Absences on a move: an answer a person gave stands (D7). An answer the bot wrote from an absence is worked
  out again for the new day: cleared when the absence no longer covers it, written when one now does and the
  person has no answer of their own.
- **C19** The Tuesday schedule must be able to hold Hyjal then Barrow Deeps ×2 from its first event (15 Dec; its
  sheet opens 10 Dec) whichever phases have shipped: through phases 1 and 2 the existing follow-on editor keeps
  working and writes the schedule's second lineup entry.
- **C12** A lineup holds at most 4 raids. Each later entry's default start is the previous entry's start plus that
  raid's length, editable per entry.
- **C13** Saved lineups are not built. Starting a one-off offers "the same lineup as <schedule>" for each schedule,
  or building one in place. The stored data has no pickup template to migrate (checked 2026-11-22).
- **C14** The page is renamed Events; `/app/rosters` redirects. "Roster" stays the word for who holds the seats.
- **C15** The word "run" becomes "event" everywhere a person reads, in the last phase (§9). Slash command names
  (`/raid open`, `/raid lock` …) do not change in this project.
- **C16** Storage keeps one file per raid of an event (§5). The event is the group; the first raid's file owns the
  sheet and the answers. This is the follow-on's storage generalised from two to N, not a rewrite.

## 4. What each thing holds

| | Raid | Schedule | Event |
|---|---|---|---|
| Name, size, lockout length, raid length, first-open date, notes | ● | | |
| Make-up limits (owner), aims (officers), group make-up | ● | | |
| Seat weights | default | may set its own | copied at open |
| Lineup | | ● | copied at open; editable until the lock |
| When: weekly day and time, or day N of each lockout | | ● | |
| Start time | | | ● editable |
| Sheet timing and switches | | ● (seeded from the profile, C7) | copied at open; ● editable |
| Regulars | | ● | read live from its schedule |
| Paused / active | | ● | |
| Answers, pins, board, rosters, lock, fill asks, log | | | ● |

"Day N of each lockout" needs a lockout to count from: it is the first lineup entry's raid.

## 5. Storage

- **Schedules** move from `raids.<id>.schedules[]` to a guild-level `schedules[]`:
  `{id, name, lineup[], kind weekly|lockout, slots | days+time, active, weights?, timing…}`. Ids stay short keys.
  The raid's own `slots` (the `default` schedule) becomes an ordinary schedule.
- **Raid-level timing keys** in a guild's config are read once by the migration (below) and then dropped (C7).
- **Event files** stay one per raid of the lineup (C16). The event's id is the first part's FILE key. Each gains `event` (the group id, the first entry's key),
  `part` (its index in the lineup) and `starts` (its offset). `follows` / `followed_by` generalise to a chain; the
  first part owns the sheet, the answers and the timing. A one-raid event is one file, as today.
- **Identity.** New events get a key with no date or time in it (`<raid abbreviation>-<short random>`). Existing
  keys keep working as opaque ids: nothing may parse a key for a raid, a date or a schedule again. Each event
  stamps `schedule` (id or none) and `occurrence` (the start the schedule gave it, for C11).
- **Regulars** are keyed by schedule id instead of `<raid>:<schedule>`.
- **Per-event timing** stays where it already is per event: the per-run dict in config (`rosters[]`, read through
  `config.team(ev.team)`), which is stamped at open and never shared. Reschedule and re-timing write that dict and
  the event's `starts_at`. The dict, `team` and the placement asks keyed on it are NOT folded into the event file
  in this project.

### Migration (one commit per guild, at start, idempotent)

Stored today (2026-11-22): one weekly schedule (Hyjal Summit, Tuesdays 6:30 PM, no follow-on), one raid with its
own run time and a confirm-by override (`oi`), one live lead + follow-on pair, no regulars, no pickup templates.

1. Each raid schedule → a guild schedule with a one-entry lineup, or two entries where it has `follow`. Its timing
   = the schedule's own values over the guild's raid-level ones over the profile's, written out in full.
2. A guild's raid-level timing on a raid with no schedule (big-guys-for: Barrow Deeps' 36-hour reminder) has
   nowhere to go. It is dropped and named in the migration's one ops line.
3. Regular keys `<raid>:<schedule>` → the new schedule id.
4. Live events: stamp `event` / `part` / `starts`; copy their timing in. Keys unchanged.
5. Finished and cancelled events are not rewritten.

## 6. Behaviour

- **Opening.** The scheduler opens a schedule's next event when its sheet is due, as today, creating one file per
  lineup entry in one write. An officer starts a one-off by picking a lineup and a date and time.
- **Rules by origin, unchanged:** a schedule's event has regulars, weighted seats and confirmations (unless the
  schedule turns them off); a one-off is first come, first rostered, nobody asked to confirm.
- **Rostering a lineup.** One answer list. Each raid is rostered from it at its own size and roster count. A
  later raid prefers people rostered in an earlier one (today's follow-on rule). The board shows every roster of
  every raid before the lock (shipped 2026-11-22 for one follow-on).
- **One lock** locks every raid of the event. A later raid never locks before the first.
- **Reschedule** (D6, D7, C4, C11): an officer picks a new date and time, or new sheet timing, on the event. The
  confirm says what moves, that answers stand, and who gets a notice. Refused in one sentence when the new start
  is in the past, or the event is finished or cancelled. After the lock a move is allowed; rosters stand and open
  confirmation asks keep their card, edited to the new time.
- **Lineup edit on an open event** (before the lock only): add, remove or reorder later entries, change a roster
  count. Removing an entry discards its board placements, nothing else.
- **Cancel** cancels the whole event.
- **Conflicts and lockouts** are checked per raid of the lineup, as today per run.
- **Loot councils, attendance, the public schedule page** keep working per raid of an event as they do today for
  a lead and its follow-on. Counting an evening once for attendance is out of scope (§8).

## 7. Surfaces

Every change is reachable from the site, Discord, plain text and MCP through the same verbs (the parity matrix).

- **Raids page**: two tabs. *Raids*: one card per raid with only the left column of §4 — no "When it runs", no
  schedules, no run times; it names the schedules whose lineup includes the raid. *Readiness* (D9): today's
  Readiness page as it is, per raid: who could play each role against the limits and aims, the ready / thin / short
  chip, the officers' aims editor. `/app/readiness` redirects to the tab.
- **Schedules page** (officers and the owner edit, D8): a card per schedule — lineup, when it
  repeats in words, its next three events, timing as the week strip, regulars with the forecast, Pause / Resume.
  The regulars card with none added shows no shortfall chip.
- **Events page**: one card per event, soonest first; a multi-raid event is one card with a section per raid
  (shipped as "a card per night"). The answer list is drawn once per event, not once per raid. Header actions:
  Start an event… (one-off), and per card Reschedule…, Edit lineup…, Lock, Cancel. Below the live events, a short
  "Coming up" list of scheduled events with no sheet yet (when the sheet opens, Open now), then History.
  The raid cards under the events are removed.
- **Discord**: `/gm config raid` loses run times and timing; a schedules wizard gains the lineup step; `/raid open`
  picks a schedule's next event or builds a one-off lineup; a Reschedule wizard (date and time picked from
  selects, never typed). The sheet names the lineup in its title, as the follow-on sheet does.
- **Plain text and MCP**: `schedule_set` / `schedule_remove` take a schedule id without a raid; `open_run` takes a
  lineup; a new reschedule op and tool; `raid_set` refuses the moved fields with one sentence saying where they
  live now.
- **Help and the manual**: §1a *The words* gains event, lineup, one-off; run and follow-on are retired.

## 7a. The pages as agreed in the mockups (2026-11-22)

This section supersedes the three page bullets of §7 where they differ. The mockups: one canvas, four boards
(Events, New event, Schedules, Raids), shown to the owner and revised with them.

**Shared.** A list on the left, the picked item in the main view (D11). The main view is sections with a heading;
facts are label / value pairs (D12).

**Events.**
- List groups: Open · Coming up (scheduled, no sheet yet) · History. "New event" at the top.
- An open event's sections: Details (Lineup, Comes from, Priority, Confirmation, Sheet opened, Locks, Signup
  sheet) · Signups (Joined, Bench, No thanks, No answer yet; Answer for someone) · Role switches · Board · Activity.
- Actions: Reschedule, Edit lineup, Lock now, Cancel event.
- Board (D13): a switch per raid of the lineup; that raid's rosters and groups, with everyone who joined placed as
  the lock would place them now, draggable; "Undo my moves" returns to that. An open seat carries the role it
  needs. This replaces Suggest and Preview lock.
- Role switches (D14): shown only when a role is short; collapsed to what each raid is short and a "Show N
  possible switches" button. Open: one line per person — the role it fills, "Name: from → to", the raids it helps,
  and Switch (or Answer for them, for someone with no answer). Before the lock Switch changes their answer after a
  confirm; after it, it sends the ask a role swap sends today.
- A coming-up event's Details add Sheet opens and Opening by hand (from when).

**New event / the lineup builder (D15).** Start from (copy a schedule's lineup, or empty) · Lineup · When · What
this makes. The builder: raids in a palette, dragged onto a track; each drop is a step with a start time and a
roster count; a step with N rosters draws N nodes fanned out from the step before; a step drags to reorder; an
empty slot takes the next raid. A schedule's editor is the same page with Repeats in place of the day.

**Schedules.** List groups: Active · Paused. "New schedule". Sections: Details (Repeats, First event, Priority,
Confirmation, Open seats after the lock, Answers) · Lineup (the builder's picture, read-only) · Sheet timing ·
Next events (Starts, Sheet opens, Locks) · Regulars. Actions: Edit, Pause / Resume, Remove.

**Raids.** The list is the raids. Inside a raid a three-way switch:
- Rules: Details (Size, Groups, Lockout, Length, First opens, Played in) and Group make-up — a table of role or
  class against its Minimum (D16).
- Priority: the seat weights, with when they apply.
- Readiness: who could play (Standing, Mains, Alts, Could switch role) and the same make-up rows with a Have
  column.

**Behaviour that D16 changes** (the designer's reading, put to the owner twice and covered by the approval):
- No upper limit on a role. The lock fills each minimum first, then gives the remaining seats to damage dealers,
  taking an extra tank or healer only when nobody else joined.
- A class minimum (four paladins) is acted on like a role minimum: met when the joiners allow, shown as short on
  the event when they do not. No minimum ever blocks a lock.
- Stored `comp` min / max and `comp_targets` migrate to one minimum per slot: a role's old minimum; a class or
  buff's old aim.

## 7b. Confirmations (D18)

- A press asks first when it tells people something, cannot be undone, or drops someone's work. Otherwise it
  happens at once and the page shows the result.
- The box: a title naming the action and its object, then at most three sentences under the Confirm rule (what
  changes, who is told, what can be undone). An edit's box lists each change as "label: old → new" above them.
- Two buttons: the action's own verb, and Back. Never Yes, No, OK or Cancel as a label. The one exception is
  "Cancel event", which is the action's verb. The verb button is red only when the action cannot be undone.
- Leaving with unsaved edits: "Keep editing" and "Discard edits".
- A refusal is the same box with one sentence and only Back.
- A box whose facts changed while it was open shows the new sentences and waits again.
- Asks first: Lock, Reschedule, Edit lineup's Save, Cancel event, Switch (before the lock: changes the answer;
  after it: sends the ask), Answer for someone, Undo my moves, a drag after the lock, Open the sheet now, a
  schedule's create / Save / Pause / Resume / Remove, removing a regular, a raid's Save.
- Does not ask: picking in a list, switching a view, Show possible switches, a drag before the lock, adding a
  regular, the buttons that open an editor, and New event's Open the sheet (its "What this makes" section is the
  confirmation).
- **Discord.** `wizard.Wizard.decide` and every card confirm take the same rules: the first button is the action's
  verb ("Lock", "Move", "Remove schedule"), not "Do it"; "Do it with a note…" becomes the verb plus "with a note"
  and loses its ellipsis; "Back" stays. No Discord button label ends in an ellipsis. The sentences are the same
  generators the site prints.

## 8. Not in this project

- Per-raid answers on one sheet (D5).
- Saved lineups / templates (C13).
- Renaming slash commands (C15).
- Attendance counted once per evening instead of once per raid.
- A calendar view.
- Events that span guilds, or a lineup entry that is not a raid.

Anti-goals: a raid card that shows a time; a second way to make an evening (no "follow-on" option beside the
lineup); an edit to a schedule silently changing a sheet people already answered; a parsed event key.

## 9. Phasing

Each phase ships on its own and leaves the bot working. (Re-cut 2026-11-22 after the page review: the make-up and
the confirmations became phases of their own, and event identity moved next to reschedule, which is what needs it.)

1. **Schedules are stored at guild level, with lineups.** `guild.yaml: schedules[]`, each with its lineup; an old
   file is lifted when it is loaded. Nothing a person sees changes: a schedule is still addressed by its first raid
   and its id, regulars keep their keys, timing still falls back to the raid, and the follow-on editor writes
   lineup entry 2 (C19). The engine still runs at most two raids per event.
   Plan: `docs/superpowers/plans/2026-11-22-guild-level-schedules.md`.
2. **Raids hold no guild times; make-up and permissions.** Timing is written onto each schedule and leaves the
   raid (C7); one minimum per role or class (D16); officers edit raids and schedules (D8).
3. **Schedules and events with their own identity.** Guild-unique schedule ids and regulars keyed by them (C2), no
   `default` schedule; event identity without the time, the `occurrence` stamp, reschedule, per-event timing,
   lineups of up to four raids with a start per entry, Edit lineup.
4. **Pages.** Events, New event with the builder, Schedules, Raids; the confirmations of §7b on the site.
5. **Discord.** The wizards for lineups and reschedule; the confirm screens' buttons (§7b).
6. **Words.** Run → event, follow-on → lineup, Who gets a seat → Priority, in every string, the manual and the
   copy lint.

## 10. Left to the builder

File and function names; the exact key format; how the lead/follow-on code is generalised to a chain; the wizard
screens' layout; the confirm sentences (under the Confirm rule); test layout.
