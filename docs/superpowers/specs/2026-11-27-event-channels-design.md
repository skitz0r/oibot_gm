# A schedule's and an event's own Discord channels — design

Status: approved by the owner in conversation, 2026-11-27 (the three places, the mock-up, and "yes to all 3": a change
moves the sheet and cards, the loot log is included, the ops feed stays the guild's).
Recon (every call site and hazard): `.superpowers/sdd/2026-11-27-event-channels/recon.md`.

Marks: **[owner]** = the owner's words or answer. **[call]** = the designer's call, open to correction.

## 1. Why
**[owner]** "I'd like to add optional event and schedule overrides for the discord channels that their sheets and things
post in." A guild has one signup channel, one officers' channel and one loot channel today; a second team or an alt
run cannot have its own.

## 2. Goals, non-goals, anti-goals
- Goals: a schedule and an event can each name their own channel for three places; an event with none uses its
  schedule's, then the guild's; changing one on a live event moves what is posted.
- Non-goals: the ops feed (stays the guild's **[owner]**); registration, absences, pool readiness, news, applications;
  a channel per raid of a multi-raid event (one set for the whole event); moving anything when the GUILD's channel
  setting changes (unchanged from today).
- Anti-goals: officer cards landing in a public channel; a sheet posted twice or left behind in the old channel; a
  thread of lock or cancel lines orphaned in the old channel; a guild with no signup channel of its own being unable to
  open an event that names one; any DM caused by a move.

## 3. The three places
| Field (the page's label) | What is posted there | Guild setting it falls back to |
|---|---|---|
| Signup sheet | the sheet, and the thread on it with the lock and cancel lines | Signup channel |
| Officers' cards | the roster health card, the lock cards, the event's updates thread, a loot council's thread | Officers' (roster) channel, then ops as today |
| Loot log | one public line per confirmed award | Loot channel, then the event's updates thread as today |

The Loot log field is offered on every schedule and event **[call]** (whether a loot council will run is not known when
the form is filled); its hint says it applies when a loot council runs.

## 4. Rules
- **One resolver per place.** `sheet channel of (reg, ev)`, `officers' channel of (reg, ev)` (today's
  `officer_channel`, which already takes `ev`), `loot log target of (reg, run key)` (already has the run key). Each
  reads: the event's own → the guild's. A schedule's value is stamped on the event when it opens, exactly as `auto_lock`
  is (ensure_run from schedule_def; later raids of a lineup copy the first raid's). Every read the recon lists under A
  that has an event in scope goes through the resolver; those without an event (appeals, guild-wide prompts) stay the
  guild's.
- **Empty means the guild's.** A stored event or schedule from before this change has no fields and behaves as today.
- **Opening without a guild signup channel.** The scheduler, Open now, the previews and the "no signup channel is set"
  warning ask the resolver, so a schedule that names its own sheet channel opens even when the guild has none.
- **Officers' cards never in the event's sheet channel.** Checked when saved (a refusal in one sentence) as well as at
  run time. The pickers are not filtered by who can see a channel **[call]**: as on the Setup page, the officer picks.
- **Permissions.** Saving a channel runs the same check the guild setting runs (`_channel_or_refuse`: reachable, a text
  channel, this server, the bot's permissions for that kind, including threads for the sheet channel). A channel that is
  later deleted or closed to the bot falls back to the guild's with one line to the ops feed, never a crash.
- **Editing.** On a schedule: any time; it applies to events opened afterwards and does not move live events **[call]**
  (Edit event moves one). On an event: until it ends or is cancelled — open, locked and started alike **[call]**; these
  fields are not timing and must not inherit Reschedule's "not after the start / not after the lock" gates.
- **A change on a live event moves things [owner].** Sheet channel changed: the sheet is posted in the new channel with
  every answer intact, the old message is deleted, the stored sheet thread and lock line are reset, and on a locked
  event the lock line is written again in the new sheet's thread. Officers' channel changed: the health / lock cards of
  every raid of the event are deleted and reposted in the new channel, and a new updates thread is started there (the
  old thread is left, archived, with one last line saying where updates continue). Loot log changed: later award lines
  go to the new channel; lines already posted stay. The write happens first; Discord edits run after it (the Unlock
  pattern), and a failed delete is logged, not fatal.
- **Who is told.** Nobody gets a DM. DMs already sent keep their old channel link; the manual says so.
- **Plain mode / Listening.** A sheet channel named by a live event is treated like the guild's signup channel for the
  one exemption `repost_sheet` gives today. Other per-channel modes stay keyed on the guild's settings.

## 5. Where it is set
- **Site:** New schedule, Edit schedule, New event, Edit event get a collapsed **Channels** section under the timing
  settings: three selects (Signup sheet · Officers' cards · Loot log). Each lists the server's text channels as the Setup
  page's pickers do; its empty option reads "#raid-signups (the guild's)" naming the channel it falls back to (on an
  event from a schedule: "(the schedule's)"). Details on the Events and Schedules pages show the three channels only
  when one differs from the guild's, with where it comes from. Labels, hints and the box sentences come from the server.
  The word "override" is never printed (Copy rule).
- **Discord:** the schedule and event wizards get a Channels step with native channel selects (a widget — Wizard rule),
  skippable.
- **Plain text and MCP:** the same three fields on the existing ops (`schedule_set`, the event's edit / reschedule op,
  `open_run`); no new verb. The parity test covers all four surfaces.
- **Confirm sentences:** Edit event's box, when a channel changes on a live event, says what moves ("The signup sheet
  moves to #alt-signups with every answer."), "Nobody gets a DM.", and that it can be changed back.

## 6. Constraints
CLAUDE.md Principles; the manual and `docs/layout` updated in the same commits; each hand-kept list of settings the
recon names (`event_rev`, `TIMING_UNKNOWN`, `_SHEET_TIMING`, `EDIT_LINE_ORDER`, …) gains the fields or the builder says
why not; persistent buttons keep working after a move (they are keyed by event, and the one message-id check on lock
cards reads the new ids); no real guild data in tests.

## 7. Left to the builder
Field names; whether the move reuses `repost_sheet` (fixing its stale thread / lock line and edit-instead-of-delete) or
a new function; test layout.
