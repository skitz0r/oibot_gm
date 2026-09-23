# oibot_GM — how the bot works (member and officer manual)

This is the bot's own description of itself. It is injected, with the live command list and the guild's
current settings, whenever someone asks the bot a question (`/ask`, a DM, or an @mention). Keep it truthful: if a behaviour changes, change it here in the same commit.

## 1. What the bot is

oibot_GM is the guild's raid administrator. It keeps the character registry, opens signup sheets on a
schedule, checks the roster's health, builds groups, fills freed seats by DM, and — once loot tables exist —
runs a loot council whose recommendations an officer confirms or overrides. Code does the computing
(solver, scoring, schedules); Claude only explains, judges against the written policy, and turns
plain-text requests (members' about their own record, officers' about anything) into typed changes that are
shown as a diff and confirmed before they apply.
Humans always decide: every roster, award and config change has an approve step, and overrides are
stored with their reason as precedents.

Your Discord account is your identity. Character names can be added later (before launch a "planned"
main has no name yet). Roles are derived from the spec you register (Protection → tank, Holy → healer,
Fire → ranged…); an offspec in another role counts as flexibility. There is no standing availability to
fill in anywhere: you answer each run's sheet when it is posted, and you post absences for days you're away.

## 2. Channels

- **#register (registration channel)** — read-only; the pinned card has three buttons: *Register / plan my
  main*, *Add an alt*, *My status*. Anyone can press them.
- **Signup channel** — one sheet per run. An **open** sheet has **Join / Bench / No thanks** buttons (Bench =
  call me if you need me). The sheet is a card: raid emblem, date and countdown, joined count and role counts,
  joiners grouped by class with real class/spec/role icons, Bench and No thanks as member rows, lock and confirm
  times; it updates in place on every answer. Join asks which character when you have more than one; Bench and
  No thanks are your answer as a person, so they don't. A **locked** sheet shows the roster(s) group by group,
  a **Not rostered** row (joiners the solver left off), the Bench and No thanks rows, and a single **Can't make
  it** button. A **cancelled** or **finished** sheet says so and has no buttons.
  The sheet lists who joined in columns, a class per column with one character per line (spec icon + name);
  after lock the columns are the groups, with ✅ / ⏳ / ❌ for each person's confirmation.
- **Absences channel** — a pinned card with *I'll be away*; each absence is announced there (no reason shown).
- **Roster channel (officers)** — health cards, roster cards at lock, fill progress (🧩 lines). Each run's card
  has an **updates thread** underneath where every ask, answer, freed seat, confirmation and expiry is logged.
- **Analytics channel (officers)** — one card per raid: how many mains the pool has for its size, role counts
  have/need, buffs nobody brings, and where the desired comp is short or over cap. The cards are edited in place
  (no re-posting), with a change-log line when the registry changes. Officers change comp ideals here in plain
  text by @mentioning the bot. The character bank is the **Members** page; building groups is the **Rosters** board.
- **Ops channel (officers)** — every action the bot takes, one line each; errors also DM the owner. The same
  lines go to a rotating log file on the host (`out/oibot.log`).
- **Applications channel** — review cards for `/apply` with Accept / Decline buttons.
- **News channel** — a Wowhead webhook posts game news there. The bot reads only the post itself (title and
  summary, never the linked article) and keeps the items about our game version, our raids, or the extra news
  keywords on the Config page. A daily review compares them with what the bot believes about the game; a
  contradiction becomes a **news proposal** card in the ops channel (§7a).

## 2a. The website

**https://gm.earlyandoften.gg** — log in with Discord. Everything below can also be done there:
your characters (press **Edit characters**: add a row, first + last name, spec/offspec, main/alt, then one **Save changes**; the crown marks your main, the trash icon deletes), absences,
your sheets and confirmations, and the DM switch on **Me**; officers get, in the left rail, **Rosters** (per raid: sheets open for signup with every answer, pins, the board and *Lock now*; locked runs with confirmations, freed seats and **Fill seats**; upcoming slots; history), **Raids** (slots, cadence, nudge/lock/confirm/fill-ask hours, autofill, open_dm, comp and seat weights per raid; the owner presses **Edit rules**), **Auras** (what the guild knows about each buff; the owner edits), **Members** (every member's main and alts in one table, with an owner/officer/member badge taken from Discord roles, a Confirm button for named characters, and their upcoming absences; **Edit members** lets an officer change any member's characters exactly as on Me, with one save), **Config** (owner edits; officers read), **Ops** (the bot's log) and **Agents** (the news review and apply jobs on the host: running or not, what the current run is doing step by step, past runs, the news kept, the proposals with Approve / Dismiss). Only
members of the Discord server can log in; officer pages follow the same rules as the officer commands.
Pages refresh themselves every 45 s, so what you see is at most that old. Signing up for a raid is still done
on the sheet in Discord.

## 3. Members: registering and keeping your record current

- **Register / plan a main**: press *Register / plan my main* in #register (class → spec → optional
  offspec → first and last name; Forever characters have two-word names; leave them blank until the
  character exists) or use `/register` / `/me plan main`. Each opens the same picker (class → spec → offspec →
  names). Nothing is typed but the name, and a refused name reopens the form with your picks kept.
  Pressing the button again replaces a planned main (roster placement and rank carry over). A named
  character waits for an officer to confirm it.
- **Alts**: *Add an alt* button, `/me char add` (first and last name, like the button), `/me plan alt`.
- **Change spec/offspec**: `/me char spec`. **Switch main**: `/me char main` (or the crown on the website).
  **Delete**: the bin icon on the website removes a character and its roster placements (`/me char retire`
  keeps history instead).
- **Name a planned character at launch**: `/me char name`: pick the planned character (skipped when there is
  only one), then type first and last name.
- `/me char spec|main|retire` take no arguments: pick the character from your own list. Spec choices are always
  that character's class, and retire asks you to confirm. `/apply` opens the same class/spec picker, then a form
  for name, logs, when you can raid and anything else.
- **Extra roles**: your role follows your spec and your offspec counts as flex; `/me plan roles` (or the per-character
  flex toggles on the website) adds roles you'd play beyond that.
- **Absences**: `/me absent list` shows yours with a **Clear** button on each; `/me absent clear` does the same.
- **Away for a while**: press *I'll be away* on the card in the absences channel, or `/me absent add`, or the Me
  page. Nothing is typed: pick the **first day** (Today, Tomorrow, then the days after — *Later than this…* picks a
  week further out) and **how long** (1 day up to 4 months), with an optional reason only officers see. A confirm
  screen shows the days in words and which live sheets it touches before anything is saved. Sheets on those days
  get *No thanks* for you, and if you were already rostered the seat is released (you get a DM saying so) and the
  bench is asked. Officers record one for someone else with `/roster absent` and a member picker, same screens.
- **See everything the bot has on you**: `/me view` or the *My status* button.
- All `/me` commands also work in a DM with the bot.
- **Or just tell the bot.** DM it or @mention it: "I'll be away Nov 1–3", "back early, clear my absence",
  "make Xanthe my main", "change Xanthe to Holy", "add my alt Jonny, Mage Frost", "bench me for tonight",
  "I can't make tonight", "I confirm for tonight", "turn my DMs off". It shows what it will change and waits for
  your **Apply** — only you (or an officer) can press it. Plain text only ever changes *your own* record: a
  request about someone else is refused with who can do it, and the rest of what you asked still applies.
  Ranks are officers' (see §8c for who may do what, and where).

## 4. Raids, runs and the signup cycle

A **raid** is an instance and its rules: size, lockout cadence, expected duration, desired tank / healer /
dps counts, and — what drives everything — its **slots** and **cadence**. At launch: **Barrow Deeps**
(10-player, 3-day lockout guess), **Hyjal Summit** (20-player, weekly), **Onyxia's Lair** (40-player,
5-day like Classic). Each raid has a **first opens at** (seeded 9 Dec 2026, 15:00 PST for all three); no
sheet opens before it. The owner sets all of this with `/gm config raid`, on the Raids page, or in plain
text (`raid_set`). There are no standing rosters: every run is built from its own sheet.

A **slot** is a recurring run time in the guild's timezone (US Pacific), e.g. Tue 7:30 PM (on the site and in Discord you pick the weekday and time; nothing is typed). A raid can have
several. One **run** = one slot occurrence = one sheet. A run is **open**, then **locked**, then **done**
(or **cancelled**). Members never keep standing availability; they answer each sheet.

**Schedules** say *when* a raid runs; the raid itself keeps what it is (size, comp, lockout, weights) and its
cadence as the defaults. A raid's own run times are its first schedule (*Regular nights*); it can have more, each
with a name, and each can set its own cadence (when its sheet opens, nudges, locks, confirms, how long fill asks
wait, autofill, DM on open, split policy) — anything it doesn't set follows the raid's. Three kinds:
- **Weekly nights** — run times like the raid's own ("Saturdays 8:00 PM"). A Tuesday main night and a Saturday alt
  run of the same raid, each with its own lock time.
- **Days of each lockout** — "Day 1 of each 3-day lockout, 8:00 PM": day 1 is the day the lockout resets, counted
  from the raid's first opening every `lockout_days`, so a 3- or 5-day reset is followed exactly (and daylight saving
  never moves the wall-clock time; a time the clocks skip that night moves past the gap).
- **Pickup template** — never opens by itself. `/raid open` offers *Open Pickup — pick a time*, the Rosters page has it
  in the schedule picker beside the date and time; the run gets the template's settings.
Each schedule also says how many **rosters** a run expects (1–4). With 2, the sheet advertises 2 × the raid's size
(20 seats for a 10-player raid), the health check and the fill engine measure against two rosters, and the lock aims
for two — as far as the tanks and healers who joined allow (it never builds a roster its minimums can't fill). A
schedule can be **paused** (nothing opens from it). Runs from a schedule other than the raid's own run times carry
its id in their key (`bd-1212-2000-alt`), so two schedules starting at the same minute are two runs.

Timeline for each run (per-raid settings; defaults in brackets, Barrow Deeps opens 48 h before):

1. **Sheet opens** — `signup_lead_hours` [120 h] before the slot the bot posts the sheet in the signup
   channel: **Join** (I'm coming), **Bench** (call me if you need me), **No thanks** (⚑ after a name = called
   out after answering). With more than one character you pick which one. Anyone with a registered absence that day
   is listed under **Away** instead (names only, the first 15 then "+N"; reasons stay with officers): they aren't
   nudged or asked to fill, and pressing Join or Bench anyway takes them out of Away. With `open_dm` on [off] every main is also
   DMed when the sheet opens. Officers can open a sheet early from the
   Rosters page: pick a date and time with the picker beside the raid's name, or press the button with nothing picked
   to take the raid's next scheduled slot (with several schedules, a picker beside it chooses which schedule, or a
   pickup template, which needs the date and time). `/raid open` does the same in Discord: pick the raid, then press *Open the next slot*, pick another upcoming
   run (each labelled with its schedule's name when there are several), press *Open <template> — pick a time* for a
   pickup template, or choose *Another day and time…* (day, hour and minutes from lists; nothing typed; the raid's own
   cadence). The confirm screen shows
   the time in guild time and in your own. A raid with no run times offers *Set this raid's run times →*. A run opened closer
   than its cadence assumes keeps a usable window: a pickup two hours out nudges, locks and confirms inside those two
   hours instead of inheriting a lock time that has already passed.
2. **Nudge** — at `nudge_hours_before` [halfway between open and lock: lock + (lead − lock) / 2, so 72 h for a
   sheet that opens 120 h out and locks at 24 h] the officer channel gets a health card (headcount, tank/healer
   tiles with who could cover via offspec/alt, buff coverage, non-responders, double-booked members) and mains
   who haven't answered are nudged once by DM. `nudge` [on] switches the DM off per raid; the health card
   still comes.
3. **Officers shape the roster** (Rosters page, before lock): the sheet shows every answer by class, Bench
   and No thanks rows, absences that day, and the **board** — a bank of everyone who joined and the groups
   of the run. Drag names from the bank into groups, between groups, or back to the bank; dropping on
   someone swaps them. Every group shows its auras as you go (coloured = present, greyed with a red edge =
   someone in the group wants it and nobody brings it, one totem per element) and the raid-wide buffs
   update too. The board is **shared**: every officer sees the same one, a change anywhere (another officer, a
   new Join in Discord, the scheduler) reaches open pages within a second, and two officers can drag at the same
   time — each drag is applied to the board as it is, so nothing is overwritten. Only whole-board actions (*Clear*,
   *Use this split/layout*) are refused if someone changed the board since you looked; the page shows the current
   board and you press again. The bank sorts by **signup** (who answered first), **spec** or **role** (tanks, healers,
   melee, ranged); the choice sticks in your browser. On a joiner's menu, **Pin to roster** guarantees a seat and
   **Keep on bench** keeps them off this run; pins are per run and the solver honours them at lock (plain text
   works too: "pin Xanthe to roster", "keep Jon on the bench", "clear the pin for Xanthe"). **Propose
   roster** shows the solver's own layout for the run (your placements and pins kept), with *another layout*
   for a different one, and *Use this layout on the board* seeds the builder; **Auto-fill empty seats** lets
   the solver seat whoever is left in the bank around what you placed; **Clear** empties the board after a
   confirmation. Whatever is on the board when the sheet locks is the roster, with the solver filling any
   empty seats. More Join answers than a raid needs → the board shows a second (third…) roster for the same
   slot while a full run with its tank/healer minimums is possible, and the button becomes **Propose
   splits**: pick a philosophy — **Balanced** (both runs equal in synergy, tanks, healers and seat quality),
   **Raid one first** (roster 1 gets the best, roster 2 the rest), **Rotation** (whoever sat out or was in
   the weaker run last window moves up) — check the one preview, press *another split like this* if it
   doesn't feel right, and *Use this split on the board*. The solver seats both runs in one go, so a shaman
   lands where it lifts the pair the most. Each raid has a **split policy** (Raids page, `/gm config raid`,
   plain text) that the scheduled lock uses when nobody chose one for the run.
   **When it can't split.** If enough people joined for more runs than the tanks and healers allow, the builder
   header says so where the split count would be — e.g. "Enough people for 2 runs — a second is short" followed by
   the tank and healer icons with a number each (2 and 2) — and the same line is on the Discord health card,
   `/raid health` and the lock card. If not even one run meets its tank/healer minimums it says "1 run is short"
   and what. The bot then makes the **one healthiest run** it can: the tank/healer minimums come first (every tank
   and healer who joined is rostered before anyone's rank or signup order counts), then buffs and weights. The
   joiners it couldn't fit are listed apart as **Leftovers (n)** — on the board's bank, in the Propose preview and
   on the lock card, one per line — with what another run is short under them. When the leftovers plus the Bench
   answers plus the roster's pool (people who haven't answered and aren't away) could make another run, one more
   line says so, and says when that needs offspecs or alts. Nothing is sent for it: open another run, or ask people,
   yourself. After lock the leftovers are simply the bench — the first people Fill asks when a seat frees up.
   **Cancel run** withdraws every open confirmation ask (nothing lingers on anyone's Me page) and warns you that
   rostered members are not told automatically — say so in the signup channel yourself.
4. **Lock** — `lock_hours_before` [24 h] before the slot (or *Lock now* / `/raid lock`): the board becomes
   the roster(s); the sheet switches to its locked form (groups, Not rostered, Bench, No thanks, one *Can't
   make it* button); the officer channel gets the roster cards. Every rostered member gets a DM: **Confirm** /
   **Can't make it**. The Me page shows the same ask. After lock the board stays editable: moving people
   between groups changes nothing else; dragging someone in from the bank is a substitution (they get the
   confirmation DM); dragging someone out releases the seat (they get a DM) and the bench is asked.
5. **Confirmation deadline** — `confirm_hours_before` [6 h] before the slot, anyone who hasn't answered
   counts as out; their seat is released and they are DMed that it was.
6. **Fill** — only after lock, never on an open sheet. Whenever a seat frees (decline, callout, absence, missed
   confirmation, officer removal) and `autofill` is on [on], the bot DMs the next best people: joiners the solver
   left off and *Bench* answers first → mains not on the sheet → **swaps** (a rostered player's offspec or alt in
   the needed role). A swap goes out only when the role it vacates stays covered; otherwise the bot looks ahead
   for a bench or pool member of that role and sends both asks tied together — the swap and its backfill — and
   a no (or silence) from either withdraws the other with a "never mind" DM. It asks the shortfall + 1 at a
   time, never more than 3 outstanding, and never asks someone who is rostered on another raid within 4 hours,
   absent, or has DMs off. Every fill DM says when silence starts counting as no: `fill_ask_hours` [4 h] after
   it was sent, never later than the run start; the updates thread logs the timeout and the next person is
   asked. A yes goes straight into the freed seat (a swap changes the seat in place). Officers can run a batch
   by hand: **Fill seats** on the Rosters page (locked runs only) or on the lock card in the roster channel
   shows a preview — exactly what is short, who is still being waited on and who would be DMed now — then
   *Send N asks* or *Cancel*; `/raid fill` does the same. With `autofill` off nothing is asked until an officer
   presses Fill seats.
7. **Raid** — `/raid loot` opens the loot council thread (when loot tables exist for the game version).
8. **Close** — 6 h after start the run is **done** and the sheet says so.

Seat selection at lock is deterministic: role bounds from the raid's comp, buff synergy, the comp policy's
standing instructions, officer pins, and the raid's **weights** — rank (core > raider > trial), main over
alt, sat out last window, earlier signup. Officers tune the weights per raid on the Raids page.

If you can't make a run you joined: press *No thanks* on an open sheet, *Can't make it* on a locked one, or
`/raid out` (a box asks for an optional note; it goes to the run's updates thread for officers, not to the public sheet). After
lock this releases your seat immediately and the bench is asked.

**Where the buttons are (site and Discord).** A run's actions sit in one bar under its board and never move: *Cancel
run* far left, then *Fill seats* and *Lock now* on the right; what does not apply yet is greyed with the reason
(Fill before lock, Lock after it). The board's own tools sit above the groups: *Auto-fill empty seats*, *Propose
roster/splits*, and *Clear* set apart. Officer cards in Discord use the same fixed order — Open the board · Lock now ·
Fill seats · Cancel run — and cancelling asks *Keep the run* / *Cancel the run*. Tables (Me, Members, Raids, Auras)
share one edit mode: an Edit button in the header, then a footer with any reset far left and *Cancel* / *Save changes*
on the right. Config saves each field as you change it and marks it *✓ saved*. Pages update on their own; *Refresh* in
the left rail is the manual fallback. Every yes/no DM (confirmations and fill asks) reads **Confirm / Can't make it**.

## 5. How groups are built

The group optimiser (a constraint solver, no AI) maximises party-buff synergy: totems, auras and shouts
are party-wide in Classic, so it puts the Enhancement shaman with the melee, hunters together, casters
with the Balance druid, and tanks with healers. Groups are seeded by archetype (tank/heal, melee, ranged,
casters — officers can change the layout, e.g. "tank, healers, melee, casters"). Totems are one per
element per shaman, so a group gets the single best totem per element for its members. The solver may
switch someone to their offspec to meet a tank/healer minimum and says so in the advisories. Raid-wide
cast buffs (Fortitude, Spirit, Mark of the Wild, Arcane Intellect, one Greater Blessing per paladin) are
counted on the whole raid. Officer pins (Pin to roster / Keep on bench) and board placements are hard
constraints; everything else is the objective.

Every buff assumption carries a status — confirmed, reported, or assumed from Vanilla — printed on the
cards, because WoW: Forever has not published totem scoping yet.

**Auras page (officers read, the owner edits).** As the guild learns how Forever's buffs really behave, the
owner records it here and the solver, the cards and the roster board follow. Each buff has a **scope**
(party or raid), a **stacking family**, a **strength** and a status; each family says which buffs don't
stack (the strongest present one counts) and **who benefits** from it, as points per role, damage type,
mana user, or a specific spec. Examples: put Blood Pact into Fortitude's family if they turn out not to
stack; set Sanctity Aura to raid if it reaches the whole raid; set "only mana users" on Intellect. Plain
text works too ("fortitude and blood pact don't stack", "sanctity aura is raid-wide", "hunters don't
benefit from blood pact"), as does `/gm config aura` (pick the buff, then what to change; *Who benefits* changes one beneficiary at a
time and leaves the rest of the family's map alone). Amber on the page marks a guild override; *Reset*
returns to the game defaults.

## 6. Analytics (officers)

- **One card per raid** in the analytics channel, native (no images): mains vs the raid size, a role icon with
  have/need for each role (⚠ = short), ⛔ followed by the buffs nobody in the pool brings, **Short** and **Over cap**
  lines from the desired comp (class or spec icon with have/want), and links to the Members and Raids pages.
- **Desired comp** per raid is derived from the buff matrix and comp rules. Officers override targets in plain
  text: "reduce healer to 3-5", "cap hunters at 3 because Trueshot", "clear the paladin target".
- The **character bank** lives on the Members page and **groups** are built on the Rosters board (Auto-fill /
  Propose); the old bank, optimised-groups and comp images are gone.
- `/roster overview` shows the readiness card on demand; `/roster members` lists a run's roster.

## 7. Officers: day-to-day commands

- Registry: `/roster list|confirm|rank|set-main|add|remove|members|absences|absent`. They take at most a member
  (Discord's own picker); the character, rank or roster is picked from a list and confirmed before anything saves.
  `/roster confirm` alone walks the unconfirmed characters.
- Applications: `/roster applicants`, `/roster applicant`, or the buttons on the review card.
- Runs: `/raid open|sheet|health|fill|lock|set|cancel|list|loot|end` (officers) and `/raid out` (anyone).
  They need no arguments: with one live run they act on it; with several they ask which. `/raid set` takes the member
  and answer, then asks which character. `/raid cancel` asks for an optional reason in a box.
- Policy: `/gm policy show|edit|reload`, `/gm rule loot|comp` (the bot shows the end of the current document; press
  **Write the rule** and write it in a paragraph box) — prose is compiled by Claude into rules and
  an officer confirms the reading before it goes live.
- Plain-text config: `/gm change <text>`, or @mention the bot in the ops or analytics channel — the bot
  shows a "current → new" diff with Apply/Cancel. It never guesses: ambiguous requests come back as a
  question. Whoever presses Apply needs the rights for the change (§8c). Absences entered this way are announced
  like `/me absent add`; channel changes post their card like the command does.
- Status: `/gm status` (uptime, data repo, registry counts, LLM spend vs budget, companions, recent ops),
  `/gm config show`.

## 7a. News proposals (officers)

Once a day (9:00 AM by the host's clock) a review reads the news the bot kept from the news channel and compares it with
what the bot believes: the buffs and which ones stack, the raids (size, lockout, opening), the comp rules, and the
guild's own overrides. When a post clearly contradicts one of those, it posts a **news proposal** card in the ops
channel: the change in words, what it touches, the sentence from the news it rests on, links to the posts, and a
confidence (low / medium / high). Most days there is no card: nothing new, or nothing that contradicts anything.

- **Approve** (officers) records who approved it and when. It changes nothing by itself: the apply job on the host
  checks every 15 minutes and carries it out. A **guild setting** (a raid's lockout, what the guild knows about a
  buff) is applied like the same change on the Raids or Auras page, without a restart. A **game data** change edits
  the bot's profile file for the game version, runs the full test suite, commits, restarts the bot and checks it
  came back — about two minutes.
- **Dismiss** (officers) keeps the card as dismissed; nothing changes, and the review won't propose it again unless
  newer news says more. An approved proposal can still be dismissed until the job picks it up.
- **Needs a developer** — the change can't be expressed as a setting or a data edit; the card has no Approve. Dismiss
  it once someone has handled it.
- The card updates in place: **Applying now**, then **Applied** (with the commit for a game data change),
  **Failed — nothing changed** (the setting was refused, or the tests failed and the edit was undone; a failed one can
  be approved again), or **Reverted — the change was rolled back** (the bot did not come back healthy after a game
  data change, so the commit was undone and the bot restarted on the old version).
- The same list, with Approve / Dismiss, is on the site's **Agents** page.

## 8. Owner: setup

`/gm config owner` (first claim needs the Discord server owner or Manage Server), then
`/gm config ops-channel`, `registration-channel`, `analytics-channel`, `roster-channel`,
`signup-channel`, `absences-channel`, `applications-channel`, `officer-role`, `timezone` (five common zones as buttons, or a region then a city; it shows what
now and the next run become before saving) — or all of these on
the site's **Config** page (channel pickers per function, officer roles, timezone, who may ask the bot,
the about text; changing a channel posts its card the same way the command does) — and per raid
`/gm config raid`: pick the raid, then a section — Schedules, Signup cadence (Standard /
Short notice / Same week, or Custom… hour by hour), Group make-up, Selection weights, Lockout & length, First lockout
opens, Behaviour (nudge, fill seats automatically, DM on open, split policy, fill-ask expiry), Notes, Reset. Changes
collect in a draft; Save shows them as sentences with the next three runs (per schedule) and writes them as one change. Nothing
opens until a raid has run times. The same settings are on the Raids page and in plain text (`raid_set`).
**Schedules** in `/gm config raid`: a raid with only its own run times opens straight on them (✕ a time to drop it,
*Add a run time* = nights + hour + minutes, *Add another schedule*); with several, pick one from the list. *Add a
schedule* asks the kind (weekly nights, days of each lockout, pickup template) and then one form (name, nights or
lockout days, hour, minutes). *Change this schedule* renames it, sets its rosters per run (1–4), its cadence (each
step "Same as the raid" or its own hours), its behaviour (each switch "the raid's", on or off), pauses/resumes it or
removes it. On the site: the Raids page's **Schedules** block under each raid lists them in words with the next three
runs; the owner adds, edits (weekday + 12-hour time pickers, lockout-day picks; cadence fields left empty follow the
raid) and removes them. In plain text: `schedule_set` (e.g. "add an alt run on Saturdays at 8 to Barrow Deeps",
"Barrow Deeps alt run locks 12 hours before", "make the alt run two rosters") and `schedule_remove`. Comp
bounds: *Group make-up* in `/gm config raid` ↔ *tank min / max* on the Raids page ↔ `tank_min` / `tank_max` in
plain text (same for healers and dps).

**News.** `/gm config news-channel` (or the Config page's *News* picker, or "set the news channel to #news" in
plain text) is where the Wowhead webhook posts; *News keywords* on the Config page (or `news_keywords` in plain
text) adds words that make a post relevant besides the game version's names and raid names. The review and apply
jobs run on the host as their own launchd agents (`scripts/agents/README.md`; `sh scripts/agents/install.sh`), and the
site's **Agents** page shows whether they are installed and running, what the current run is doing step by step,
past runs, the news kept and the proposals; **Run review now** (owner) starts the review at once. A menu-bar item on
the Mac mini (GM ✓ / ⟳ / ⚠ / ✕) shows the same at a glance, even when the bot is down.

**Officer roles.** `/gm config officer-role role:@Officers` (add; `remove:true` to take one away), the Config page's
role picker, or in plain text ("make @Council officers"). The bot remembers the *role itself* (its Discord id), not
its name: renaming the role keeps its officers, and a new role someone creates with the same name grants nothing.
Manage Server always counts as officer; the owner is set separately. Configs from before this change listed roles
by name — the bot converts them to ids the first time it sees the server (one commit in the data repo) and, until
then, the names still work so nobody is locked out; a name it can't find in the server is shown on the Config page
and in the ops channel so it can be re-added by picking the role.

Per-raid switches: `nudge` [on] — DM mains who haven't answered, once, at `nudge_hours_before`; `autofill`
[on] — after lock the bot fills released seats by DM on its own; `open_dm` [off] — DM every main when a sheet
opens; `fill_ask_hours` [4] — how long a fill DM waits before silence counts as no.

**Times.** The guild's timezone is US Pacific (`America/Los_Angeles`, the default). Every schedule, lock time,
lockout window, page clock and "asked/starts at" stamp the bot shows is in that timezone; only Discord's own
`<t:…>` timestamps render in each viewer's local time.

**Plain text does everything the site does.** `/gm change <text>` (or an @mention in the ops or analytics
channel) covers every action on the Rosters, Raids, Members and Config pages, not only settings. The bot turns
the sentence into one or more operations, shows each as a one-line "what will happen" diff, and applies on
**Apply** through exactly the code the slash command or page button would use — so a lock sends the same
confirmation DMs and cards as `/raid lock`, a cancel withdraws the same confirmations, a cleared absence
re-opens the same sheets. Who may do what, and in which channel, is the owner's plain-text policy (§8c): by
default officer actions need an officer and raids, auras and guild setup need the owner. A run is
named by its key (`bd-1209-1930`), or by raid and day ("tonight's Barrow Deeps", "the Hyjal run") when only one of
that raid's runs is live — with two live, the bot asks which. A member is named by display name, @mention or one
of their characters.

- *Runs:* "open Barrow Deeps for Thursday 19:30" (or "open the next Barrow Deeps") posts the sheet now;
  "lock tonight's Barrow Deeps" builds the roster and DMs everyone rostered; "cancel bd-1209-1930, server
  down"; "put Xanthe on the bench for bd-1209-1930" / "set Marrow to join as Marrowlite" / "Kestrel can't
  make it" (after lock: join seats and asks to confirm, no thanks frees the seat and the fill engine looks for
  cover); "Xanthe confirmed for tonight" / "Kestrel said she can't make it after all" (on a locked run: answers
  their Confirm / Can't make it ask for them, when they told you out of band — exactly as if they had pressed the
  button, so a no frees the seat and asks the bench); "who would fill ask next for tonight's run" (preview, nothing sent) and "send the fill asks"; "split
  tonight's run first-roster-first" (balanced / first / rotation); "auto-fill the board for bd-…" (before lock);
  "groups for bd-…: Ash, Bright, Cinder | Dusk, Ember" sets the board — the layout the lock will use, or after
  lock the roster itself (names added are asked to confirm, names dropped are freed). Pins and per-run seats
  stay as before ("pin Ash to the roster", "keep Dusk on the bench").
- *Members:* "add Jon's alt Jonny, Mage Frost" (value is `Class Spec [Offspec]`), "change Jonny to Fire",
  "offspec for Jonny: none", "name Jon's planned Shaman Stormcall" (planned characters only — a named
  character is never renamed: retire it and add the new one; its class never changes either), "rank Jonny
  raider", "make Jonny Jon's main", "retire Jon's alt Jonny"; "Marrow is back, clear her absence" (by start
  date when she has several); "turn DMs off for Marrow" (off: never asked to fill, confirmations wait on the site).
- *Test bench:* "seed 20 test members", "open a test Barrow Deeps run" (`/gm test run` preselects the Standard
  tempo: starts in 40 min, nudge 32, lock 25, confirm 15 min before; Quick is 20/16/12/8 and Slow 90/70/55/35), "clear the test bench".
- Several things in one sentence become several operations, applied in order ("lock tonight's run and turn
  DMs off for Marrow"). Loot items, tiers, wishlists and standing availability are still not settable anywhere.

## 8c. Plain-text permissions (owner sets, officers read)

Who may change what through plain text, and where, is one policy on the **Ops** page of the site (card
*Plain-text permissions*; the owner presses **Edit permissions**, changes the table and presses one **Save**;
officers see it read-only; MCP has `plain_permissions` / `set_plain_permissions`). It covers every plain-text
path: @mentions, DMs, `/gm change`, and the site's plain-text box (which counts as a place where plain text acts).
The owner can always do everything, anywhere the bot listens.

**What** — seven capability groups; every plain-text operation belongs to exactly one:

| Group | Default | Someone could type |
|---|---|---|
| Own record — your absences, DMs, characters (add, spec, offspec, name, main, retire) and your own answer or confirmation on a run | registered members | "I'll be away Nov 1–3", "make Xanthe my main", "I can't make tonight" |
| Other people's records — their absences, characters, DMs, ranks (yours too), confirming characters | officers | "rank Jonny raider", "Mira is away next week" |
| Runs — open, answer for someone, lock, cancel, fill, split, auto-fill, the board, seats on a run, `confirm_for` | officers | "lock tonight's Barrow Deeps", "Xanthe confirmed for tonight" |
| Comp & policy — comp targets and group layout, pins, appending a rule to the loot or comp policy | officers | "we want 3–4 healers in Barrow Deeps", "pin Jonny in tonight" |
| Raids & auras — raid schedules and settings, buff and family facts | the owner | "Barrow Deeps runs Tue and Thu 7:30 pm" |
| Guild setup — channels, officer roles, timezone, who may ask, the about text | the owner | "post raid sheets in #signups" |
| Test bench | officers | "seed 20 puppets", "clear the test bench" |

**Who** — per group, any mix of *everyone*, *registered members* (an active character), *officers*, *owner only*,
and specific Discord roles (picked from the server's roles, stored by id, so renaming a role keeps it). An empty
list means the owner only. Anyone allowed a group for other people may also do it for themselves.

**Where** — each channel is one of: **Acts** (anything the person's groups allow), **Own record only** (a member's
own record; other requests are refused with where to go instead), **Answers only** (questions, no changes) or
**Ignored** (the bot doesn't answer @mentions there). DMs have their own entry, and there is a default for every
channel not listed. Defaults: the ops and analytics channels act; DMs and every other channel are *own record
only* — so a raider can tell the bot about their absence anywhere, while run and roster changes stay in the
officer channels.

**How it decides.** The bot reads a message's intent first: in a channel that acts, the sentence is parsed as a
change straight away (a question gets a short answer from the live settings). Elsewhere the bot answers it like
`/ask`, and only when the message asks it to *do* something — and you could act there — does it turn it into a
change for you to Apply. Who an operation is about is decided by the bot's code, not the model: leaving the
person out ("I'll be away") means *you*; naming yourself by display name, @mention or one of your characters
means you; a name that could also be someone else (two people with that name, or your name is another member's
character) counts as someone else; a character you name must be one of yours. Refusals say who may and where:
*"Only officers can change someone else's absence."*, *"Plain text can't change runs in #general — use
#officer-ops."* Every applied plain-text change is logged in the ops channel with who asked and where.

## 8a. Rehearsing with the test bench (officers)

`/gm test seed` asks how many puppet members to create (5, 10, 20 or the whole bench; fake Discord ids, a
realistic class mix and ranks, mains confirmed). `/gm test run` is one screen: the raid (when there is more than
one), a tempo (Quick, Standard or Slow) and whether to DM everyone when it opens, then **Open the test run**. It
opens a real sheet for that raid in the signup channel with minute-level cadence, so the unchanged scheduler goes through nudge →
lock → confirmations → expiry → fill → close within the hour. `/gm test answer` offers **Random mix** (how many
Join, Bench and No thanks) or **One member** (pick a puppet, then Join, Bench or No thanks), and stays open so you
can answer again; after lock a puppet's *No thanks* is a
callout. `/gm test clear` cancels the test runs and deletes every puppet and their placements.

What is safe: a test run's pool is only the puppets and you (the tester) — real members are never nudged,
rostered or asked to fill it, and the test sheet refuses presses from real members. Every DM the bot would
send a puppet (open, nudge, Confirm / Can't make it, fill asks) arrives in **your own DMs** with the puppet's
name on it, and you can press its buttons on their behalf. What real members do see: the sheet itself in the
signup channel (marked as a test), the roster-channel cards, the ops lines, and the analytics cards pausing
until `/gm test clear`.

## 8b. MCP server (owner, from Claude Code)

The bot ships an MCP server, `oibot-mcp`, so a Claude Code session opened in the bot's repo can run the
guild the way the site does: read sheets and records, answer for people, lock, fill, cancel, edit raid
cadence and auras, record absences, change settings in plain words. It is a thin client: every tool is one
call to the **running** bot's web API, so what it does is exactly what the officer panel does, side effects
included (sheets re-rendered, roster cards, DMs, ops lines). If the bot is not running, every tool says so.

**Start.** `uv run oibot discord` (the bot, with `OIBOT_WEB_BIND` set) and `OIBOT_MCP_TOKEN` in `.env`
(the same value the bot reads). `.mcp.json` at the repo root registers the server for Claude Code
(`uv run oibot-mcp`); `claude mcp list` shows `oibot_gm`. `uv run oibot-mcp --list-tools` prints the tools.
The bot's address defaults to `http://127.0.0.1:8788` (`OIBOT_MCP_URL` to change it).

| Tool | Does |
|---|---|
| `guild_overview` | timezone, owner, officer roles, channels, raids' cadence, live runs, test bench |
| `list_runs` · `get_run` | live sheets per raid with upcoming slots · one run in full (signups, board, confirmations, fill asks, absences that day, log) |
| `list_raids` · `get_raid` | effective raid settings and overrides |
| `list_members` · `get_member` | everyone's characters, absences, asks (`filter` narrows) · one record |
| `list_absences` · `list_auras` · `ops_log` | upcoming absences · the buff matrix · status and the ops feed |
| `open_run` | open a sheet now (next slot, or a date and time; `schedule` = that schedule's next run, or a pickup template at `when`) |
| `schedule_set` · `schedule_remove` | one setting of a raid's schedule (creates it with its kind, slots or lockout days) · drop a schedule (schedules come back in `list_raids` / `get_raid`) |
| `set_answer` · `pin` · `confirm_for` | Join / Bench / No thanks for someone (character swap) · pin in/out for the lock · answer a Confirm ask for them |
| `propose_split` · `set_strategy` · `autofill` · `set_layout` | preview a split (nothing saved; says why there can't be more runs and who is left over) · remember the strategy · solver shapes the board · set the groups |
| `lock_run` · `fill_seats` · `cancel_run` | lock now · preview the fill asks, `send=true` sends them · cancel with a reason |
| `raid_set` · `raid_reset` | one raid setting (slots, hours, weights, comp bounds, first open…) · back to defaults |
| `aura_set` · `family_set` · `aura_reset` | what the guild learns about buffs and stacking families |
| `add_absence` · `clear_absence` | away dates for someone (sheets answered / re-opened) |
| `save_characters` · `set_member` · `set_dm` | character table rows · rank / confirm / main · DMs on or off |
| `set_channel` · `set_config` | a bot channel by name · timezone, ask audience, about, officer roles |
| `test_bench` | seed / run / answer / clear, as `/gm test` |
| `plain_change` | text → the config ops it means, with current → new; applied only with `apply=true` |
| `list_news` · `get_profile` | the news kept since a time · the effective profile (buffs, families, raids, comp rules) with guild overrides marked |
| `post_proposal` · `list_proposals` · `get_proposal` · `resolve_proposal` | the news review's proposals (§7a): post one, list by state, one in full, move its state |
| `job_status` | the review and apply jobs: state, last and next run, launchd, recent runs |

Members can be named by display name, character name or Discord id; runs by key, roster key or the raid's
name when it has one live run. An ambiguous name comes back with the candidates instead of a guess.

**Safety.** The server acts as the **guild owner** (owner-only settings included) and every change is logged
as the owner via `mcp`. Keep `OIBOT_MCP_TOKEN` private — it is as sensitive as the bot token — and never
put it in `.mcp.json` or a commit. Ask for a preview first (`propose_split`, `fill_seats` without send,
`plain_change` without apply) and read the returned line back: it says what actually happened.

## 9. Loot (when loot tables exist)

The council scores each drop deterministically — tier for the spec × slot weight × attendance × rank ×
wishlist, discounted by recent loot and a same-slot repeat — and Claude judges the top candidates against
the written loot policy, explains, and flags close calls. Officers confirm, or override with a reason; the
reason becomes a precedent the bot cites next time. In-game awards from the companion app are matched
to the proposal automatically. WoW: Forever loot tables are not published yet, so this part is waiting
for data discovered in game.

## 10. Data, privacy, cost

Guild state lives in a private git repository (one commit per change, so every edit has an author and a
time). Absence reasons and officer notes are officer-only. The bot DMs only for sheets, nudges, confirmations,
released seats and fill asks; switch DMs off on the **Me** page if you'd rather not get them — with DMs off
you are never asked to fill, and a confirmation ask is shown on the Me page instead and counts as pending
until you answer it there. **My absences** on the absences card lists yours with a **Clear** button each; clearing
an absence puts you back to unanswered on any open sheet it had pre-filled (a seat freed on a locked run is
not handed back automatically — ask an officer). Claude is used sparingly — never for registry, signup or solver work — and spend is
capped by a monthly budget the owner sets (`OIBOT_BUDGET_USD`, per calendar month, kept across restarts); questions and
plain-text requests may use at most half of it each, so they can't spend what loot night needs. `/gm status` shows
this month's spend.

## 10a. Asking the bot questions

`/ask`, a DM, or an @mention outside the officer channels gets a free-form answer from this manual and
your own record — or, when you're asking the bot to *do* something about your own record, the change itself to
Apply (§3, §8c). Who may do that is set by the owner (`ask_audience`: officers, confirmed members,
registered members, or everyone; default registered). Anyone outside that audience gets the **static
guide** instead — a menu with *About the guild*, *Raid schedule*, *How to register*, *How signups work*,
*How to apply*, *Who to contact* — built from the guild's settings, no AI involved.

What `/ask` knows is what the site shows, in one bounded snapshot taken when you ask: the guild's channels,
officer roles, owner and ask audience; every raid's schedules (in words, with their own settings and next runs —
*Raid schedule* in the static guide lists each schedule's next three runs too) and cadence (slots, when sheets open, nudge, lock and
confirmation times, `fill_ask_hours`, `autofill`, `open_dm`, split policy, weights, comp bounds, comp targets
and group layout, which settings are overridden); the guild's aura and family overrides; the test bench (puppets,
test runs); and every live run as the Raids page has it — state, nudge / lock / confirmation-expiry times, split
strategy, who joined (with character and spec), bench, no thanks, who hasn't answered, pins, the board before
lock, the rostered groups after lock with confirmed / waiting / declined, the bench after lock, what the run is
short, fill asks outstanding (who, for what, deadline) and recent answers, members absent that day, callouts, and
the last log lines. It also knows your own record — characters, roles, DMs, upcoming absences, confirmations
waiting on you, and your answer and seat on each live run. An officer who names a member (display name or
character) in the question gets that member's record too, absence reasons included; members asking about
others don't. Long lists are cut short rather than dumped, and the answer only ever *points* at commands —
a change is a separate step you Apply (plain text, §8c).

## 11. Things the bot cannot do (yet)

Loot: add or tier loot items, record wishlists, list or retire precedents, or show loot history — the
loot council only runs on the demo data until Forever's loot tables exist. Splits: the **Loot-quality**
split philosophy is a greyed placeholder until wishlists exist (Balanced, Raid one first and Rotation
work). Buff facts, comp bounds, cadence and split policy *can* be changed (Auras page, Raids page,
`/gm config raid|aura`, plain text); the buff list itself and the item tables are files in the code
repository. There is no standing roster or availability to set, no attendance or loot view on `/me` yet,
and no way to sign up for a run anywhere except its sheet. If you ask for one of these, say so plainly.
