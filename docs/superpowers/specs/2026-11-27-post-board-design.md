# Post board — design

Status: approved by the owner in conversation, 2026-11-27. Site first; Discord, plain text and MCP are GitHub issue 16.
Builds on `2026-11-27-event-channels-design.md` (live): the channel fields, resolvers, pickers and the move machinery.

Marks: **[owner]** = the owner's words or answer. **[call]** = the designer's call, open to correction.

## 1. Why
Before the lock the board exists only on the site. **[owner]** "I'd like a button on the event below the board for 'post
board'. We should also have an accompanying setting for which channel this will post to. When I press the post button,
we should get a modal with 'post to <channel>' with the channel being default value of a drop-down menu where I can
dynamically select."

## 2. Goals, non-goals, anti-goals
- Goals: one press posts the event's board to a Discord channel; the post stays true as the event changes.
- Non-goals: assignments (tank targets, healing, interrupts); Discord / plain text / MCP surfaces (issue 16); a history
  of old posts.
- Anti-goals: two board posts for one event; a stale board left in a channel; a DM or a ping caused by a post or its
  updates; people who were not placed being listed.

## 3. The button and the box
- **Post board** under the board on the event page (officers), on an open or a locked event **[call]**, not on an ended
  or cancelled one.
- The box holds its title, ONE select "Post to" and the buttons Back · Post. **No sentences [owner: "none of this text
  is needed"]** — an owner-decided exception to the Confirm rule's three sentences; a refusal still shows its one sentence.
- The select opens on the event's Board posts channel (§4) and lists the server's text channels as the other channel
  pickers do. Picking another channel there posts to it AND becomes the event's own Board posts channel **[call]** — the
  post is live, so the event must remember where it is; one place, one truth.
- When the event already has a live post, the button reads **Update board post** **[call]** and the box opens on where
  it is now; choosing another channel moves it.

## 4. The setting
A fourth place, **Board posts**, beside Signup sheet, Officers' cards and Loot log: on the guild (Setup page's channel
pickers), a schedule and an event, in the same folded Channels section, with the same inheritance (event's own → its
schedule's, stamped at open → the guild's → **[call]** the event's signup sheet channel when the guild has none), the
same save-time permission check and the same fallback when a channel is gone. It may be any text channel, including
the signup channel.

## 5. What is posted
- **Every raid of the event [owner: "all"]**, in lineup order: one message per raid (a raid's rosters and groups fit
  one embed as the locked sheet's do; if a raid cannot fit, it continues in a second message).
- The locked sheet's layout, reused not copied: a section per roster with its role counts, a column per group, one
  member per line with the spec icon they play in THAT raid (the per-raid switch reader). Title: the raid and its time
  (a native Discord timestamp). No "posted by" line **[call]**; the log says who.
- **Only the board [owner: "leave out non-pertinent information"]**: no Not placed, no Bench, no left-out names, no
  confirmation marks, no buttons. A raid with nobody placed posts its header and one line saying nobody is placed yet.
- No mentions, no pings.

## 6. Live
- **[owner]** "ideally this is also live updated if the event changes." After the first post the bot keeps it current:
  every change that redraws the board (a move, Auto-fill groups, Clear, a switch, a lock, an unlock, officer edits
  after the lock, a reschedule of the start, a lineup edit adding or removing a raid) edits the messages in place,
  debounced like the sheet's refresh. A raid added to the lineup gains a message; a removed raid's message is deleted.
- **[owner]** "this should move (or remove and re-add)": changing the Board posts channel (the box or Edit event)
  posts in the new channel first, then deletes the old messages — the event-channels move, with its failure and
  restart healing.
- A message someone deleted by hand is posted again at the next change.
- After the lock the post keeps updating **[call]**. The signup sheet also shows the locked roster (that is today's
  behaviour, in the signup channel); the two can sit in different channels, which is the point of the setting.
- When the event ends or is cancelled the post is edited one last time to its final state (as locked / cancelled) and
  no longer updated **[call]**; it is not deleted.
- **Remove board post** in the event's More menu deletes the messages and stops the updates **[call]**.

## 7. Who is told
Nobody gets a DM or a notice. One line in the event's log and the ops feed for the first post, a move and a removal
(not for each live update).

## 8. Constraints
CLAUDE.md Principles (words from the server; Clock rule; Iconography — spec icon alone); the manual and `docs/layout`
updated in the same commits; stored events without the new fields behave as today; the hand-kept settings lists gain
the fourth field; no real guild data in tests.

## 9. Left to the builder
Names; whether live updates hook the existing sheet-refresh path or the board-change path; test layout.
