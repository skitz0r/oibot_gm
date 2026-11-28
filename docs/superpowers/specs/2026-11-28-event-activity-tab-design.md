# The event's Activity tab — design

Status: approved by the owner in conversation, 2026-11-28 (answers 1–6 below). Site only.

Marks: **[owner]** = the owner's words or answer. **[call]** = the designer's call, open to correction.

## 1. Why
What the bot does in people's DMs is invisible to officers. The site shows a confirmation mark per name and a tally,
and hides fill requests as text lines under More › Asks and callouts…. **[owner]** "a secondary top-tab on the event to
show this stuff might be a good idea, so we can see a history, what's pending, etc."

## 2. Goals, non-goals, anti-goals
- Goals: one place on the event that shows every DM the bot sent for it and what came back; what is still waiting is
  on top.
- Non-goals **[owner]**: officer changes to the board or settings in this tab ("just discord bot activity that is not
  otherwise visible (dms sent, answers pending, etc)"); held seats ("not in scope"); a view across events
  ("event specific"); new actions beyond withdrawing an open request; Discord / plain text / MCP surfaces.
- Anti-goals: a row claiming a DM was delivered when it was not; member-private text shown to officers that they do
  not see today (a callout's note is already officer-visible; nothing new is exposed); the tab causing any DM.

## 3. The tab
- The event page gets two top tabs: the page as it is today **[call: labelled "Event"]** and **Activity**, with the
  count of pending rows on the Activity tab when it is not zero. The tab is in the URL hash so a link can open it.
  Officers only, like the rest of the event page. Offered for open, locked and finished events (a finished event's tab
  is read-only history).
- **More › Asks and callouts… is removed [owner: "yes"]**; its content lives here. The event's Log stays where it is.

### Pending
Every DM the bot is still waiting on, soonest deadline first:
| Kind | When it exists |
|---|---|
| Confirm | the lock asked a rostered person to confirm and they have not answered |
| Open seat | a fill request after the lock ("a seat opened, can you take it?") |
| Could you come? | an ask sent before the lock **[owner: "yes"]** |
| Role switch | an ask to switch role or character for the event |
Each row: the person (their character and spec icon, one per line, no label beside the icon), the kind, the raid and
roster it is for (only when the event has several), when it was sent, and when it runs out — both through the Clock
rule's formatter with the relative time beside it. A paused Automated Fill Requests state is one line above the list
in the server's words, as the card says it today.

**Withdraw [owner: "revoking an open request would be good if simple"]**: a pending Open seat, Could you come? or Role
switch row has **Withdraw**, the existing verb (`/api/run/{key}/withdraw`) with its existing confirm box. A Confirm row
has no button **[call]** — withdrawing a confirmation is taking the person off the roster, which is the board's job.
Nothing else is pressed from this tab.

### History
Newest first, the same kinds plus the DMs that expect no stored answer:
- each ask above with how it ended: said yes, said no / can't make it, ran out, withdrawn (and by whom), closed by
  the lock / an unlock / a cancel;
- the sheet's opening DMs and the reminder DMs to people who had not answered, as ONE row per send with a count
  ("Reminder sent to 6 people") that unfolds to the names **[call]**;
- an officer's Remind unconfirmed, likewise one row per press;
- a callout a member sent from a DM card (Can't make it), because it is the answer to a bot DM.
A filter by kind and by person **[call]**. A DM that could not be delivered (DMs closed) is shown as not delivered, in
the server's words, where the bot knows it.
Not here **[owner]**: board moves, settings edits, channel moves, the lock itself, loot.

## 4. Data
- The read model is built from what the event already stores (confirmations, fill asks incl. pre-lock ones with their
  sent / answered / deadline stamps, callouts, the nudge marks, the log). **[call]** Where a kind of DM is not recorded
  today with who and when (the opening DM, a reminder, a failed delivery), the bot starts recording it from this change
  on in a small per-event list; older events show what they have and one sentence says earlier DMs were not recorded.
  The builder lists in the report exactly which kinds had to gain a record.
- One route gives the tab its rows, labels and empty-state sentences (words from the server). It reloads on the same
  live topics the event page already listens to.
- Empty states name what is missing: e.g. "The bot is not waiting on anyone for this event." / "The bot has sent no
  DMs for this event yet."

## 5. Constraints
CLAUDE.md Principles (Copy, Clock, Iconography, DM rule — the tab sends nothing; words from the server; "event", never
"run"); the manual and `docs/layout` updated in the same commits; stored events load unchanged; no real guild data in
tests; works at phone width (rows stack).

## 6. Left to the builder
Component and route names; the exact record for newly logged DMs; test layout.
