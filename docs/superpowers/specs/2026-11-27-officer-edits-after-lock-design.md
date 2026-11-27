# The lock closes the sheet to members, not to officers — design

Status: direction approved by the owner in conversation, 2026-11-27. Follows `2026-11-26-lock-unlock-start-design.md` (live).
Recon (every call site, sentence and test): `.superpowers/sdd/2026-11-27-officer-edits-after-lock/recon.md`.

Marks: **[owner]** = the owner's words or answer. **[call]** = the designer's call, open to correction.

## 1. Why
**[owner]** "When locked, members shouldn't be able to make changes any more, but officers should still be able to make
changes as they like." "Assume that post-lock there are no confirmations required, this is officers making their own tweaks."
Today a locked roster is half-closed to officers: a move works but DMs people, and Auto-fill groups, Clear, Undo, a
Bench drag and a direct spec switch are refused.

## 2. Goals, non-goals, anti-goals
- Goals: on a locked event before it ends, every board tool an officer has on an open event works; none of them asks
  anyone to confirm, DMs anyone, or writes a notice **[owner: "no" to the Confirm DM and to the Not rostered notice]**.
  Members can no longer change their answer after the lock, except to say they can't come.
- Non-goals **[call]**: Edit event's settings and the lineup stay closed after the lock (Unlock opens them); Lock /
  Preview lock on a locked event; changing what the LOCK itself sends.
- Anti-goals: a person losing a confirmation they already gave because an officer moved them; a DM caused by an officer's
  tweak; a locked roster that silently collapses from two rosters to one; an Unlock that no longer restores the board.

## 3. What stays exactly as today
- The lock (the clock's or an officer's): its roster, its Confirm asks when the event has confirmations on, its Not
  rostered notices, its cards. Those asks keep their deadline and still free a seat on silence or a Can't make it.
- Unlock **[owner: stays]**, Start now, Reschedule, End, Cancel, Remind, confirm-for, withdraw an ask, pause, and
  **Fill seats** pressed by an officer (that is an officer choosing to ask the bench; its DMs are the point).
- Automated Fill Requests for a seat a MEMBER gives up (a Can't make it, an expired ask).

## 4. Officer changes on a locked roster
One quiet path. Every officer change below goes through one engine entry (the builder's name; today's
`apply_layout_locked` with its fan-out removed for officer edits) that:
- seats / unseats / moves exactly what was asked, across every roster, keeping the roster count;
- sends no Confirm ask to a newly rostered person; they are simply rostered (no open ask, so nothing expires for them);
- sends no DM and writes no notice to a removed person. **[call]** their answer stays what it was (Join or Bench), they
  go to Not rostered and can be dragged back — today's `free_seat` sets them to out, which is wrong for an officer's tweak;
- a removed person's open Confirm ask is closed and its DM card edited to its final state (a card edit is not a DM);
  a person who had confirmed and is moved between groups or rosters keeps their confirmation;
- **[call]** triggers no automatic fill asks. The officer left that seat open on purpose or will fill it by hand;
  Fill seats remains a button;
- redraws the sheet, the lock cards and the members' event page, and writes one line to the event's log and the ops feed
  naming the officer (as today).

What uses it:
| Tool | Today when locked | After |
|---|---|---|
| Move, swap, substitute, take off | works, DMs and notices | quiet |
| Drag a Bench or Not rostered name onto the roster | Bench refused | allowed, quiet |
| Auto-fill groups | refused | fills OPEN slots only from Not rostered then Bench (the solver, placed names fixed); never moves a rostered person |
| Clear the board | refused | refused still **[call]** — clearing a published roster is Unlock's job; the sentence names Unlock |
| Undo | refused | undoes the officer's last quiet change (one step, as on an open board) |
| Split strategy / Propose | refused | refused still **[call]** — changes the roster count; the sentence names Unlock |
| Record an answer for someone (Join / Bench / No thanks), incl. a person who never answered | Join seats + Confirm DM | records the answer; Join puts them in Not rostered, the officer places them; no DM |
| Role switch, whole event or one raid | refused / an Ask to the member | applied directly, quiet **[owner: no confirmations; call on dropping the Ask]**; the Role switches list offers Switch after the lock as before it |
| Pins | accepted, no effect | hidden / refused in one sentence (unchanged menus) |

Confirm sentences (Confirm rule) for these on a locked event say what changes, then "Nobody gets a DM.", then what can
be undone; "until the lock" tails become "until the event starts" where the tool now works after the lock.

## 5. Members after the lock
- Join, Bench and No thanks are refused everywhere with the existing locked sentence — including the API path that still
  accepts No thanks today (recon D).
- Kept: Can't make it / callout (sheet, Me page, `/raid out`, DM), answering a Confirm or fill ask, absences. A member
  must always be able to say they can't come.
- A member's own character/spec change on a locked event: unchanged from today.

## 6. Every surface
Site (board, Signups ⋮, Role switches, sheet card), Discord (`/raid set`, the lock card's buttons), plain text and MCP
follow the same rules through the same engine entry; the parity matrix is kept true. The test bench follows the event page.

## 7. Constraints
CLAUDE.md Principles (Copy, Confirm, Clock, Wizard, DM, Iconography; words from the server; "event" never "run"); the
manual updated in the same commits; no migration: events locked before this change behave by the new rules from the
restart on. A split loot council's roster index must stay valid (the roster count never changes while locked).

## 8. Left to the builder
Names; whether Undo keeps one step or the open board's history; how the quiet path shares code with the member-caused
path (`after_board_change`) as long as member-caused openings keep today's fan-out; test layout (recon F lists 35 tests
to rewrite).
