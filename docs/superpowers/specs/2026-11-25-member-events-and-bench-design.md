# Members' event view, and Bench as the last to fill — design

Status: approved by the owner in conversation, 2026-11-25 ("your readings are correct, and yes to left rail for members"). Part of the schedules / raids / events effort; follows `2026-11-22-raids-events-schedules-design.md` (phases 1–6, all live).

## 1. Why

- A member can answer on the site but cannot see who else is coming or which group they are in: the Events page is officers-only, and a Discord event link sends a member to their own row on the Me page. The only place a member sees the signups and the roster is the Discord sheet.
- A **Bench** answer ("call me if you need me") never gets a seat at the lock. It is used only afterwards: when a seat is open, benched people are the first the bot asks. The owner wants benched people used at the lock itself to fill what joiners cannot, at the lowest priority, and visible on the board.

## 2. Part A — the members' event view

**The rule: a member sees on the site exactly what the Discord signup sheet already shows everyone, plus their own answer buttons.** Nothing an officer-only surface shows today reaches it.

### Where
- **The rail:** members get an **Events** entry (officers keep theirs, which opens the officers' page as today).
- **The list** (`/events` for a member): **Open** — every live event (open for signup or locked), soonest first, each with its title, time, state and the member's own answer in the server's word; **Coming up** — each active schedule's next nights, read-only (what the public `/schedule` page shows; no Open button). No History. Test events (rehearsals) and dropped raids are never listed, except a tester's own rehearsal.
- **One event** (`/events/<key>`): the address every Discord event link already opens (`raid_views.event_url`). A member gets this view instead of the redirect to the Me page. Each live event row on the Me page links to it. A key that is finished, cancelled or gone says so in the one sentence the Me page uses today and shows the list.

### One event, top to bottom
1. **Header:** the event's title, time and state (the server's words).
2. **Your answer:** the member's current answer with Join / Bench / No thanks and the character picker; after the lock Confirm / Can't make it — the SAME component and routes the Me page row uses (`/api/me/run/{key}/answer|out`, `/api/me/fill/{key}`, `/api/me/ask/{id}`), not a copy.
3. **Details:** the public facts only — Lineup, Priority (first to join / by priority), Locks, Confirm by, the signup sheet's link. Not: Comes from, Sheet opened, Reminder, open-seat counts, the officer card / thread links, automatic asks.
4. **Signups:** the officers' tabs component (`SignupsSection`'s tabs, grouped by class, spec icon alone) with **Joined**, **Bench**, **No thanks** and — as the sheet has it — **Away**. No row ⋮ menu, no Answer for someone, and **no "No answer yet" tab** (who has not answered is officer-only today and stays so).
5. **Roster, after the lock:** each raid of the event, its rosters and groups read-only, one member per line, with the marks the locked sheet shows. Before the lock there is no board for members: the officers' layout is not shown.
6. **Not shown, ever:** the board before the lock, Role switches, Activity, absence reasons, callout notes, pins / kept off the roster, who was asked to fill, any officer button.

### Server
- A separate member-safe read model (not `event_view.event_json` with fields stripped): built from the same readers the sheet uses (`raid_views.sheet_message`'s data, `rc` readers), so the page and the sheet cannot disagree. Routes for any member of the guild (`may_access`), never MCP-only; officers may call them too.
- Live updates: the page reloads on the event's `run:` topic and `member`, as the Me page does.
- Tests pin, field by field, that no officer-only fact is in the member routes' JSON: not-answered names, absence reasons, callout notes, pins, fill asks and their targets, placed_by / drag state, the activity log, role-switch rows, priority weights' per-person scores.

### Words and rules
- Every word from the server (PAGE_WORDS, the sheet's words); the Copy, Clock and Iconography rules; "event", never "run".
- Empty states say what is missing ("Nobody has joined yet.").

## 3. Part B — Bench fills last

### At the lock
- The lock places everyone who **Joined** exactly as today. A seat that no joiner can take is then offered to people who answered **Bench**, at the lowest priority: after every joiner, whatever the weights say.
- "A seat no joiner can take" means: a seat left empty once every eligible joiner is placed, **or** a tank / healer minimum that the joiners cannot meet. (The designer's call for the second: a roster short a healer is worse than a benched healer taking a seat while a joined damage dealer waits. A benched player never displaces a joiner for any other reason.)
- Among benched people the event's own order applies: by priority on a schedule's event (the same weights), by when they answered on a first-to-join event.
- Benched people **never raise the number of rosters**: the joiners decide how many rosters the event makes, as today; Bench only fills seats in those.
- A benched person who gets a seat is **rostered** like anyone: they appear on the locked sheet's roster, get the Confirm / Can't make it ask where the event asks for confirmation, and a seat they don't confirm is freed as today. Their sheet answer still reads Bench in the Signups list; the roster is what says they play.
- Kept-off pins, hand placements, the later raids of an event and the repeatable solve all hold as today. Preview lock and Auto-fill groups use the same solve, so they show the same result.
- After the lock nothing changes: an open seat is still offered to the remaining bench first.

### On the board, before the lock
- The bank beside the groups has two sections: **Not placed** (joined, not in a group — today's bank) and **Bench** (answered Bench, not in a group). An officer can drag a benched person into a group; that is a hand placement the lock keeps, like any other.
- The facts above the board keep counting joiners ("Placed N of M joined"); a benched person placed by hand or by Auto-fill is counted in a second number only if a server sentence can say it plainly ("and 1 from the bench").

### Words
- The lock box's sentence that counts who is rostered says when some come from the bench ("… 2 of them from the bench."), within the Confirm rule's twenty words.
- The manual's §1a **Bench** entry and the sheet footer say the new meaning: on the bench you are placed only when a seat would otherwise stay empty, and you are among the first asked when one frees up later.
- A benched person rostered at the lock gets the lock's Confirm ask (an act they must take — the DM rule allows it); nobody else is told anything new.

## 4. Not in this project
- A member view of History, of the board before the lock, or of other members' attendance.
- Changing what "End the event" ends on a multi-raid event (the owner has not decided).
- Any change to the after-lock fill engine's order.

## 5. Left to the builder
Module, route and component names; how the solver expresses "last" (a cost tier, a second pass) as long as the result is repeatable and no joiner is displaced except for a tank / healer minimum; the roster's read-only component (reuse the board's group layout); test layout.
