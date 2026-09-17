# oibot_GM — how the bot works (member and officer manual)

This is the bot's own description of itself. It is injected, with the live command list and the guild's
current settings, whenever someone asks the bot a question (`/ask`, or @mention it outside the officer
channels). Keep it truthful: if a behaviour changes, change it here in the same commit.

## 1. What the bot is

oibot_GM is the guild's raid administrator. It keeps the character registry, opens signup sheets on a
schedule, checks the roster's health, builds groups, fills gaps by DM, and — once loot tables exist —
runs a loot council whose recommendations an officer confirms or overrides. Code does the computing
(solver, scoring, schedules); Claude only explains, judges against the written policy, and turns
officers' plain-text requests into typed changes that are shown as a diff and confirmed before they apply.
Humans always decide: every roster, award and config change has an approve step, and overrides are
stored with their reason as precedents.

Your Discord account is your identity. Character names can be added later (before launch a "planned"
main has no name yet). Roles are derived from the spec you register (Protection → tank, Holy → healer,
Fire → ranged…); an offspec in another role counts as flexibility. There is no standing availability to
fill in: you answer each run's sheet when it is posted, and you post absences for days you're away.

## 2. Channels

- **#register (registration channel)** — read-only; the pinned card has three buttons: *Register / plan my
  main*, *Add an alt*, *My status*. Anyone can press them.
- **Signup channel** — one sheet per run with **Join / Bench / No thanks** buttons (Bench = call me if you need me).
- **Absences channel** — a pinned card with *I'll be away*; each absence is announced there (no reason shown).
- **Roster channel (officers)** — health cards, roster proposals, fill progress (🧩 lines).
- **Analytics channel (officers)** — the character bank, and per roster: pool readiness, optimised
  groups, desired comp. They re-post at the bottom after every change, with a change-log line above.
  Officers change comp ideals here in plain text by @mentioning the bot.
- **Ops channel (officers)** — every action the bot takes, one line each; errors also DM the owner.
- **Applications channel** — review cards for `/apply` with Accept / Decline buttons.

## 2a. The website

**https://gm.earlyandoften.gg** — log in with Discord. Everything below can also be done there:
your characters (press **Edit characters**: add a row, first + last name, spec/offspec, main/alt, then one **Save changes**; the crown marks your main, the trash icon deletes), absences,
your sheets and confirmations, and DM opt-out on **Me**; officers get, in the left rail, **Rosters** (per raid: sheets open for signup with every answer, pins, a draft roster and *Lock now*; locked runs with confirmations and freed seats; upcoming slots; history), **Raids** (slots, cadence, comp and seat weights per raid; the owner presses **Edit rules**), **Members** (every member's main and alts in one table, with an owner/officer/member badge taken from Discord roles, a Confirm button for named characters, and their upcoming absences; **Edit members** lets an officer change any member's characters exactly as on Me, with one save), **Config** (read-only) and **Ops** (the bot's log). Only
members of the Discord server can log in; officer pages follow the same rules as the officer commands.
Signing up for a raid is still done on the sheet in Discord.

## 3. Members: registering and keeping your record current

- **Register / plan a main**: press *Register / plan my main* in #register (class → spec → optional
  offspec → first and last name; Forever characters have two-word names; leave them blank until the
  character exists) or use `/register` / `/me plan main`.
  Pressing the button again replaces a planned main (roster placement and rank carry over). A named
  character waits for an officer to confirm it.
- **Alts**: *Add an alt* button, `/me char add`, `/me plan alt`.
- **Change spec/offspec**: `/me char spec`. **Switch main**: `/me char main` (or the crown on the website).
  **Delete**: the bin icon on the website removes a character and its roster placements (`/me char retire`
  keeps history instead).
- **Name a planned character at launch**: `/me char name`.
- **Absences**: `/me absent list`, `/me absent clear` (adding is above).
- **Away for a while**: press *I'll be away* on the card in the absences channel (from, to, optional
  reason for officers), or `/me absent add`, or the Me page. Sheets on those days get *No thanks* for you,
  and if you were already seated the seat is handed back and the bench is asked.
- **See everything the bot has on you**: `/me view` or the *My status* button.
- All `/me` commands also work in a DM with the bot.

## 4. Raids, runs and the signup cycle

A **raid** is an instance and its rules: size, lockout cadence, expected duration, desired tank / healer /
dps counts, and — what drives everything — its **slots** and **cadence**. At launch: **Barrow Deeps**
(10-player, 3-day lockout guess), **Hyjal Summit** (20-player, weekly), **Onyxia's Lair** (40-player,
5-day like Classic). Each raid has a **first opens at** (seeded 9 Dec 2026, 15:00 PST for all three); no
sheet opens before it. The owner sets all of this with `/gm config raid`, on the Raids page, or in plain
text (`raid_set`).

A **slot** is a recurring run time in the guild's timezone (US Pacific), e.g. `Tue 19:30`. A raid can have
several. One **run** = one slot occurrence = one sheet. Members never keep standing availability; they
answer each sheet.

Timeline for each run (per-raid settings; defaults in brackets, Barrow Deeps opens 48 h before):

1. **Sheet opens** — `signup_lead_hours` [120 h] before the slot the bot posts the sheet in the signup
   channel: **Join** (I'm coming), **Bench** (call me if you need me), **No thanks**. With more than one
   character you pick which one. Absences pre-fill *No thanks*. Officers can open a sheet early with
   `/raid open <raid>` (next slot, or a one-off `YYYY-MM-DD HH:MM`) or the Rosters page.
2. **Nudge** — halfway to lock the officer channel gets a health card (headcount, tank/healer tiles with
   who could cover via offspec/alt, buff coverage, non-responders, double-booked members) and
   non-responders are nudged once by DM.
3. **Officers shape the roster** (Rosters page, before lock): see every answer, build the **draft** the
   solver would seat, **pin** someone to the roster or **keep them on the bench**, set anyone's answer,
   swap characters. More Join answers than a raid needs → the solver builds a second (third…) roster for
   the same slot while a full run with its tank/healer minimums is possible; the rest are bench.
4. **Lock** — `lock_hours_before` [24 h] before the slot (or *Lock now* / `/raid lock`): the draft becomes
   the roster(s); the sheet shows groups and a confirmation tally; the officer channel gets the roster
   cards. Every seated member gets a DM: **Confirm** / **Can't make it**. The Me page shows the same ask.
5. **Confirmation deadline** — `confirm_hours_before` [6 h] before the slot, anyone who hasn't answered
   counts as out and their seat is freed.
6. **Fill** — whenever a seat frees (decline, callout, absence, missed confirmation), if `autofill` is on,
   the bot DMs the next best people: joiners the solver benched and *Bench* answers first → mains not on
   the sheet → offspec switches → alts of the needed role. It asks the shortfall + 1 at a time, never more
   than 3 outstanding, and never asks someone who is In on another raid within 4 hours, absent, or opted
   out of DMs. A yes goes straight into the freed seat. Officers can run or preview a batch with
   `/raid fill` or *Ask the bench* on the Rosters page.
7. **Raid** — `/raid loot` opens the loot council thread (when loot tables exist for the game version).
8. **Close** — 6 h after start the sheet closes.

Seat selection at lock is deterministic: role bounds from the raid's comp, buff synergy, the comp policy's
standing instructions, officer pins, and the raid's **weights** — rank (core > raider > trial), main over
alt, sat out last window, earlier signup. Officers tune the weights per raid on the Raids page.

If you can't make a run you joined: press *No thanks* on the sheet, or `/raid out [note]`. After lock this
frees your seat immediately and the bench is asked.

## 5. How groups are built

The group optimiser (a constraint solver, no AI) maximises party-buff synergy: totems, auras and shouts
are party-wide in Classic, so it puts the Enhancement shaman with the melee, hunters together, casters
with the Balance druid, and tanks with healers. Groups are seeded by archetype (tank/heal, melee, ranged,
casters — officers can change the layout, e.g. "tank, healers, melee, casters"). Totems are one per
element per shaman, so a group gets the single best totem per element for its members. The solver may
switch someone to their offspec to meet a tank/healer minimum and says so in the advisories. Raid-wide
cast buffs (Fortitude, Spirit, Mark of the Wild, Arcane Intellect, one Greater Blessing per paladin) are
counted on the whole raid.

Every buff assumption carries a status — confirmed, reported, or assumed from Vanilla — printed on the
cards, because WoW: Forever has not published totem scoping yet.

## 6. Analytics (officers)

- **Character bank** — every member → main (class, spec/offspec, role, rank, rosters) and alts.
- **Pool readiness** per raid — every planned/active main vs the roster size: headcount, role tiles,
  buff coverage, who isn't placed on the roster yet.
- **Optimised groups** per raid — the solver on the pool at full size with open slots shown, aura
  badges per group, totem picks, raid-wide buffs.
- **Desired comp** per raid — derived targets per role/class/spec with a one-line reason each,
  have vs want, and how many could fill a slot by switching to their offspec. Officers override targets in
  plain text: "reduce healer to 3-5", "cap hunters at 3 because Trueshot", "clear the paladin target",
  "groups tank, healers, melee, casters".
- `/roster overview` shows the readiness card on demand; `/roster members` lists a roster.

## 7. Officers: day-to-day commands

- Registry: `/roster list|confirm|rank|set-main|add|remove|members|absences|absent`.
- Applications: `/roster applicants`, `/roster applicant`, or the buttons on the review card.
- Runs: `/raid open|sheet|health|fill|lock|set|cancel|list|loot|end` (officers) and `/raid out` (anyone).
- Policy: `/gm policy show|edit|reload`, `/gm rule loot|comp` — prose is compiled by Claude into rules and
  an officer confirms the reading before it goes live.
- Plain-text config: `/gm change <text>` or @mention the bot in the ops or analytics channel — the bot
  shows a "current → new" diff with Apply/Cancel. It never guesses: ambiguous requests come back as a
  question. Whoever presses Apply needs the rights for the change.
- Status: `/gm status` (uptime, data repo, registry counts, LLM spend vs budget, companions, recent ops),
  `/gm config show`.

## 8. Owner: setup

`/gm config owner` (first claim needs the Discord server owner or Manage Server), then
`/gm config ops-channel`, `registration-channel`, `analytics-channel`, `roster-channel`,
`signup-channel`, `absences-channel`, `applications-channel`, `officer-role`, `timezone`, and per raid
`/gm config raid raid:<id> slots:'Tue 19:30, Thu 20:00' signup_lead_hours: lock_hours_before: confirm_hours_before:`
(also weights, first_open, lockout, duration, comp, notes). Nothing opens until a raid has slots.

**Times.** The guild's timezone is US Pacific (`America/Los_Angeles`, the default). Every schedule, cutoff,
lockout window, page clock and "asked/starts at" stamp the bot shows is in that timezone; only Discord's own
`<t:…>` timestamps render in each viewer's local time.

## 9. Loot (when loot tables exist)

The council scores each drop deterministically — tier for the spec × slot weight × attendance × rank ×
wishlist, discounted by recent loot and a same-slot repeat — and Claude judges the top candidates against
the written loot policy, explains, and flags close calls. Officers confirm, or override with a reason; the
reason becomes a precedent the bot cites next time. In-game awards from the companion app are matched
to the proposal automatically. WoW: Forever loot tables are not published yet, so this part is waiting
for data discovered in game.

## 10. Data, privacy, cost

Guild state lives in a private git repository (one commit per change, so every edit has an author and a
time). Absence reasons and officer notes are officer-only. The bot DMs only for sheets, nudges and fill
asks; members can ask an officer to opt them out. Claude is used sparingly — never for registry, signup
or solver work — and spend is capped by a budget the owner sets; `/gm status` shows it.

## 10a. Asking the bot questions

`/ask`, a DM, or an @mention outside the officer channels gets a free-form answer from this manual and
your own record. Who may do that is set by the owner (`ask_audience`: officers, confirmed members,
registered members, or everyone; default registered). Anyone outside that audience gets the **static
guide** instead — a menu with *About the guild*, *Raid schedule*, *How to register*, *How signups work*,
*How to apply*, *Who to contact* — built from the guild's settings, no AI involved.

## 11. Things the bot cannot do (yet)

Add or tier loot items from Discord; record wishlists; list or retire precedents; show loot history;
change the buff matrix, scoring weights or raid definitions (those are files in the code repository);
split a pool into two rosters automatically. If you ask for one of these, say so plainly.
