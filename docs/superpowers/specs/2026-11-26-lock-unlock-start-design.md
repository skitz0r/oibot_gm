# Unlock, a lock that asks before it fills, and Start now — design

Status: approved by the owner in conversation, 2026-11-26 (the settings, "any event", "keep open", unlock keeps the rosters, and "A" for pausing the asks). GitHub issue #14. Part of the schedules / raids / events effort; follows `2026-11-25-member-events-and-bench-design.md` (live).

Marks: **[owner]** = the owner's words or answer. **[call]** = the designer's call, open to correction.

## 1. Why

The owner is testing locks on real events and found three things in the way.

1. A lock cannot be taken back. To try again the owner has to open a new event. **[owner]** "I need to be able to reverse this and unlock to retry."
2. A lock always places everyone who is not on the board. **[owner]** "This is not desired. Instead, warn the user … and on that modal include a check box for 'Auto-fill empty roster slots' to optionally run the solver."
3. An event "starts" only when the clock says so. **[owner]** "There should be a 'Start now' option for a locked raid. Performing an action that errors because the raid is locked but not yet started should also offer start now."

## 2. Goals, non-goals, anti-goals

- Goals: an officer can unlock any locked event before its start and lock it again; a lock an officer presses fills empty slots only when asked to; whether the clock locks an event, and whether that lock fills, are settings; a locked event can be started now.
- Non-goals: a per-night setting stored on a schedule for one night; changing the fill engine's order; changing what End ends on a multi-raid event; a "started" state (started stays "the start time has passed").
- Anti-goals: an unlock that DMs people who do not have to act; a seat lost because the owner pressed Unlock or Start now; an unlocked event locked again by the clock a minute later; a scheduled raid nobody touched locking with nobody rostered because a default changed.

## 3. Part A — Unlock

### What it is
**Unlock** puts a locked event back into open signup with its rosters still on the board. **[owner]** "I don't want unlock to undo rosters, it should just put the event back into the pre-lock mutable state."

### Rules
- Allowed for any event, test or not **[owner]**, while it is locked and before its start. Refused, in one sentence each, after the start, while a loot council is open on it, and for a done or cancelled event (those keep Reopen / nothing, as today).
- A multi-raid event unlocks whole: every raid of the lineup goes back to open together (a later raid cannot stay locked while an earlier one is open).
- **The board after an unlock is the locked roster.** Every rostered name sits in the group the lock (or a later officer move, or a fill) put it in, across every roster. Each of those names counts as placed by hand: the next lock keeps it where it is. Joiners who were not rostered are in Not placed; Bench answers not rostered are in Bench. Someone who answered Can't make it or called out stays out. Someone who lost their seat after the lock without saying so (their confirmation ran out, or an officer took them off the roster) comes back as a joiner in Not placed **[call]**; where the stored answer cannot tell silence from a refusal, the builder says so in the report and the person stays out.
- The event is fully open again: members can change their answers on the sheet, officers can drag, swap, Auto-fill groups, Clear the board, Edit event and Reschedule, exactly as before a lock.
- **Confirmations are kept. [owner: "keep open" was said of Start now; applied here as a call]** An answer already given stays. An unanswered Confirm / Can't make it ask stays open and can still be answered. While the event is unlocked no confirmation expires and no seat is freed for silence.
- **Fill asks in flight are withdrawn [call]**: each pending ask is closed and its DM card edited to its final state, as a cancel does today. They offered a seat the officer may now fill by hand.
- **Auto lock is switched off for this event** (Part B), so the clock does not lock it again. The box says so. The officer may switch it back on in Edit event.
- The sheet goes back to its open-signup form. The members' event page stops showing the roster until the next lock **[call]** (members see rosters only after a lock, as today).
- The officer channel's lock cards for the event are removed or redrawn to the open state; the "🔒 … is locked" line in the signup channel is deleted. The lock stores that line's message id from now on; for an event locked before this change the line stays and nothing fails.
- The Not rostered notices the lock wrote for this event are removed, so nobody reads "in line for a freed seat" on an open event; a later lock writes at most one such notice per person per event **[call]**.
- One line in the event's thread and one audit line say who unlocked it.

### The next lock after an unlock
- Works like any lock (Part B), with the board as the starting point.
- A person who is rostered again and already confirmed is not asked again. A person rostered again with an open ask keeps that ask. A person newly rostered gets the ask. A person who held an ask or a confirmation and is no longer rostered has the ask closed (card edited) and gets the existing Not rostered notice.
- The confirm deadline is worked out fresh from the event's settings at this lock. **A late lock [call]:** when that deadline has already passed (a lock after an unlock, or an officer's lock with Auto lock Off), the deadline is the earlier of the start and one hour from the lock, the rule Reschedule already uses. This holds for every lock, not only one after an unlock. The box's deadline sentence says the real time.

### Who is told at an unlock
Nobody gets a DM. People with a withdrawn fill ask see their card change. Everyone else sees the sheet.

### Words
The Unlock box (Confirm rule: at most three sentences, twenty words each) says what changes, who is told, what can be undone, for example: "The event goes back to open signup with its rosters on the board." "Nobody gets a DM, and 3 confirmation asks stay open." "Auto lock is turned off, so an officer locks it again." Exact sentences are the builder's, from the server.

## 4. Part B — the lock

### Two settings **[owner]**
"I think we need to make 'auto lock' an optional setting, and 'run auto roster fill on auto lock' a sub-setting for the schedule and event."

- **Auto lock** (On / Off): On — the event locks by the clock at its lock time, as today. Off — it stays open until an officer locks it.
- **Auto-fill on auto lock** (On / Off), shown under Auto lock and only meaningful when Auto lock is On: On — the clock's lock fills empty slots with the solver, as today. Off — the clock's lock rosters the board as it stands.
- Both live on a schedule (for its events) and on a one-off's open form, are stamped on the event when it opens (like sheet timing), and are editable per event in Edit event before the lock. They appear in Details on the Events and Schedules pages with the same labels and (?) hints as the other fields.
- **Defaults [call]: both On** for every existing schedule and event and for new ones, so nothing changes until an officer switches one off.
- With Auto lock Off, the Details "Locks" fact says an officer locks it, in the server's words, instead of a time. The reminder and the other timings do not change. Asks sent before the lock run to their own deadline and are closed when an officer locks **[call]**; nothing else happens at the old lock time. An event still open at its start is left as today's code leaves an open event at its start and close; the manual says what that is.

### A lock an officer presses
- The lock box no longer fills silently. When the board is complete (every joiner who can be rostered is placed) the box is as today.
- When any roster has open slots, the box says so per roster ("Roster 1 has 3 open slots.") and offers one choice, **Auto-fill empty roster slots**, **unticked** **[owner]**.
  - Unticked — **lock as it stands**: exactly the names on the board are rostered, in their groups. Nobody else is placed. Joiners left in Not placed get the existing Not rostered notice. **Exactly the board means exactly [call]:** a person pinned in, or one the comp policy never benches, who is not on the board is not rostered, and the box says so in one sentence when there is one; a Bench answer that Auto-fill put on the board is rostered, because the officer saw it there and locked.
  - Ticked — the lock runs the solver as it does today (the board's hand placements fixed, Bench last, all of #11's rules).
- The box re-previews when the choice changes, so its sentences always describe the lock that will happen. **Open slots after a lock as it stands [owner: "A"]:** a lock as it stands pauses Automated Fill Requests for that event (the existing pause), so nobody is DMed for the slots the officer left open. The box says so in one sentence when the setting is On and slots stay open; the officer resumes it from the event. The Not rostered notice written at such a lock does not promise a DM for a freed seat while the asks are paused: it says an officer decides who takes an open slot.
- A lock as it stands with nobody on the board is refused in one sentence that names the way out (place someone, or tick the choice).
- Preview lock shows the roster for the choice currently selected.

### The clock's lock
- Auto lock On, Auto-fill on auto lock On: today's lock.
- Auto lock On, Auto-fill on auto lock Off: lock as it stands. With nobody on the board it fails like any failed lock today: the sheet stays open, one line tells the officers why, and it does not retry every minute.
- Auto lock Off: no lock by the clock.

### Every surface
- Site: the Events page Lock box and the sheet card's lock box (one component), with the choice.
- Discord: a decide screen has no checkbox, so it shows two buttons, **Lock as it stands** and **Lock and auto-fill** **[call]**, only when there are open slots; one **Lock** button otherwise.
- Plain text: "lock …" is reviewed first as today and locks as it stands; "lock and fill …" asks for the fill. MCP `lock_run` takes an `autofill` flag, default false. Both are a person's lock, so they follow the unticked default **[call]**.
- Later raids of a lineup lock with the same choice as the first.

## 5. Part C — Start now

- **Start now** is offered for a locked event before its start: in the event's More menu and on the sheet card, on the Discord lock card, and next to every refusal that says the event has not started (today that is End: "The event hasn't started yet…").
- It moves the start to the current minute. Later raids of the event move by the same amount. Everything that reads the start then treats the event as started (End is allowed, Reschedule and Edit event are closed, as after any start).
- **Unanswered confirmations stay open [owner]**: Start now does not move the confirm deadline and frees no seat. Asks are answered or expire at the deadline they already had.
- People see the existing "moved" notice and the edited sheet and cards. Nobody gets a DM.
- The box says what changes, who is told and that it cannot be moved back afterwards (a started event cannot be rescheduled).
- **The loot gap [call]:** Loot › Tonight's End and the MCP's `end_loot` end a locked event before its start with no check today. They get the same refusal as End everywhere else, with Start now beside it.
- Available on the site, Discord, plain text and MCP.

## 6. Constraints
- CLAUDE.md Principles: Copy, Confirm, Clock, Wizard, DM and Iconography rules; every word from the server; "event", never "run"; the manual updated in the same commits; the parity matrix (site, Discord, plain text, MCP) kept true.
- The DM rule decides who is told: no new DM kinds. An unlock, a Start now and a setting change DM nobody.
- Stored events from before this change keep working with no migration step: a missing setting reads as On.
- Tests that lock through the engine without a board keep meaning "solve everyone": the engine's lock keeps filling unless told otherwise; the surfaces a person presses pass the choice.

## 7. Not in this project
- Fill asks sent after an event's start expire at once today; Start now does not change that.
- A reset button on the Test bench (Unlock covers retrying a lock).
- Restoring an event locked before this change to the exact hand / Auto-fill marks it had: after an unlock every rostered name is a hand placement.

## 8. Left to the builder
Names of fields, routes, ops and components; whether "lock as it stands" is built from the board directly or by solving with every unplaced name held out, as long as the result is exactly the board and the usual advisories still appear; where Unlock sits in Discord (beside End / Reopen); how the lock box carries a choice (the Reschedule box's fields-inside-a-box pattern is the nearest model); test layout.

