# oibot_GM — how the bot works (member and officer manual)

This is the bot's own description of itself. It is injected, with the live command list and the guild's
current settings, whenever someone asks the bot a question (`/ask`, or @mention it outside the officer
channels). Keep it truthful: if a behaviour changes, change it here in the same commit.

## 1. What the bot is

oibot_GM is the guild's raid administrator. It keeps the character registry, opens signup sheets on a
schedule, checks the roster's health, builds groups, fills freed seats by DM, and — once loot tables exist —
runs a loot council whose recommendations an officer confirms or overrides. Code does the computing
(solver, scoring, schedules); Claude only explains, judges against the written policy, and turns
officers' plain-text requests into typed changes that are shown as a diff and confirmed before they apply.
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
- **Absences channel** — a pinned card with *I'll be away*; each absence is announced there (no reason shown).
- **Roster channel (officers)** — health cards, roster cards at lock, fill progress (🧩 lines). Each run's card
  has an **updates thread** underneath where every ask, answer, freed seat, confirmation and expiry is logged.
- **Analytics channel (officers)** — the character bank, and per raid: pool readiness, optimised
  groups, desired comp. They re-post at the bottom after every change, with a change-log line above.
  Officers change comp ideals here in plain text by @mentioning the bot.
- **Ops channel (officers)** — every action the bot takes, one line each; errors also DM the owner. The same
  lines go to a rotating log file on the host (`out/oibot.log`).
- **Applications channel** — review cards for `/apply` with Accept / Decline buttons.

## 2a. The website

**https://gm.earlyandoften.gg** — log in with Discord. Everything below can also be done there:
your characters (press **Edit characters**: add a row, first + last name, spec/offspec, main/alt, then one **Save changes**; the crown marks your main, the trash icon deletes), absences,
your sheets and confirmations, and the DM switch on **Me**; officers get, in the left rail, **Rosters** (per raid: sheets open for signup with every answer, pins, the board and *Lock now*; locked runs with confirmations, freed seats and **Fill seats**; upcoming slots; history), **Raids** (slots, cadence, nudge/lock/confirm/fill-ask hours, autofill, open_dm, comp and seat weights per raid; the owner presses **Edit rules**), **Auras** (what the guild knows about each buff; the owner edits), **Members** (every member's main and alts in one table, with an owner/officer/member badge taken from Discord roles, a Confirm button for named characters, and their upcoming absences; **Edit members** lets an officer change any member's characters exactly as on Me, with one save), **Config** (owner edits; officers read) and **Ops** (the bot's log). Only
members of the Discord server can log in; officer pages follow the same rules as the officer commands.
Pages refresh themselves every 45 s, so what you see is at most that old. Signing up for a raid is still done
on the sheet in Discord.

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
- **Extra roles**: your role follows your spec and your offspec counts as flex; `/me plan roles` (or the per-character
  flex toggles on the website) adds roles you'd play beyond that.
- **Absences**: `/me absent list`, `/me absent clear` (adding is below).
- **Away for a while**: press *I'll be away* on the card in the absences channel (from, to, optional
  reason for officers), or `/me absent add`, or the Me page. Sheets on those days get *No thanks* for you,
  and if you were already rostered the seat is released (you get a DM saying so) and the bench is asked.
- **See everything the bot has on you**: `/me view` or the *My status* button.
- All `/me` commands also work in a DM with the bot.

## 4. Raids, runs and the signup cycle

A **raid** is an instance and its rules: size, lockout cadence, expected duration, desired tank / healer /
dps counts, and — what drives everything — its **slots** and **cadence**. At launch: **Barrow Deeps**
(10-player, 3-day lockout guess), **Hyjal Summit** (20-player, weekly), **Onyxia's Lair** (40-player,
5-day like Classic). Each raid has a **first opens at** (seeded 9 Dec 2026, 15:00 PST for all three); no
sheet opens before it. The owner sets all of this with `/gm config raid`, on the Raids page, or in plain
text (`raid_set`). There are no standing rosters: every run is built from its own sheet.

A **slot** is a recurring run time in the guild's timezone (US Pacific), e.g. `Tue 19:30`. A raid can have
several. One **run** = one slot occurrence = one sheet. A run is **open**, then **locked**, then **done**
(or **cancelled**). Members never keep standing availability; they answer each sheet.

Timeline for each run (per-raid settings; defaults in brackets, Barrow Deeps opens 48 h before):

1. **Sheet opens** — `signup_lead_hours` [120 h] before the slot the bot posts the sheet in the signup
   channel: **Join** (I'm coming), **Bench** (call me if you need me), **No thanks** (⚑ after a name = called
   out after answering, ✈ = away that day, pre-filled from an absence). With more than one
   character you pick which one. Absences pre-fill *No thanks*. With `open_dm` on [off] every main is also
   DMed when the sheet opens. Officers can open a sheet early with `/raid open <raid>` (next slot, or a
   one-off `YYYY-MM-DD HH:MM`) or the Rosters page.
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
   plain text) that the scheduled lock uses when nobody chose one for the run. **Cancel run** withdraws every open confirmation ask (nothing lingers on anyone's Me page) and warns you that
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
`/raid out [note]` (the note goes to the run's updates thread for officers, not to the public sheet). After
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
benefit from blood pact"), as does `/gm config aura`. Amber on the page marks a guild override; *Reset*
returns to the game defaults.

## 6. Analytics (officers)

- **Character bank** — every member → main (class, spec/offspec, role, rank, runs) and alts.
- **Pool readiness** per raid — every planned/active main vs the raid size: headcount, role tiles,
  buff coverage, who isn't on an upcoming run yet.
- **Optimised groups** per raid — the solver on the pool at full size with open slots shown, aura
  badges per group, totem picks, raid-wide buffs.
- **Desired comp** per raid — derived targets per role/class/spec with a one-line reason each,
  have vs want, and how many could fill a slot by switching to their offspec. Officers override targets in
  plain text: "reduce healer to 3-5", "cap hunters at 3 because Trueshot", "clear the paladin target",
  "groups tank, healers, melee, casters".
- `/roster overview` shows the readiness card on demand; `/roster members` lists a run's roster.

## 7. Officers: day-to-day commands

- Registry: `/roster list|confirm|rank|set-main|add|remove|members|absences|absent`.
- Applications: `/roster applicants`, `/roster applicant`, or the buttons on the review card.
- Runs: `/raid open|sheet|health|fill|lock|set|cancel|list|loot|end` (officers) and `/raid out` (anyone).
- Policy: `/gm policy show|edit|reload`, `/gm rule loot|comp` — prose is compiled by Claude into rules and
  an officer confirms the reading before it goes live.
- Plain-text config: `/gm change <text>` or @mention the bot in the ops or analytics channel — the bot
  shows a "current → new" diff with Apply/Cancel. It never guesses: ambiguous requests come back as a
  question. Whoever presses Apply needs the rights for the change. Absences entered this way are announced
  like `/me absent add`; channel changes post their card like the command does.
- Status: `/gm status` (uptime, data repo, registry counts, LLM spend vs budget, companions, recent ops),
  `/gm config show`.

## 8. Owner: setup

`/gm config owner` (first claim needs the Discord server owner or Manage Server), then
`/gm config ops-channel`, `registration-channel`, `analytics-channel`, `roster-channel`,
`signup-channel`, `absences-channel`, `applications-channel`, `officer-role`, `timezone` — or all of these on
the site's **Config** page (channel pickers per function, officer roles, timezone, who may ask the bot,
the about text; changing a channel posts its card the same way the command does) — and per raid
`/gm config raid raid:<id> slots:'Tue 19:30, Thu 20:00' signup_lead_hours: nudge: nudge_hours_before: lock_hours_before: confirm_hours_before: fill_ask_hours:`
(also `autofill`, `open_dm`, weights, split_policy, first_open, lockout, duration, comp, notes). Nothing opens until a raid has slots.
The same settings are on the Raids page and in plain text (`raid_set`). Comp bounds are one thing with three
spellings: `tanks:'2-3'` on the command ↔ *tank min / max* on the Raids page ↔ `tank_min` / `tank_max` in
plain text (same for healers and dps).

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
re-opens the same sheets. Officer actions need an officer; the owner-only settings still need the owner. A run is
named by its key (`bd-1209-1930`), or by raid and day ("tonight's Barrow Deeps", "the Hyjal run") when only one of
that raid's runs is live — with two live, the bot asks which. A member is named by display name, @mention or one
of their characters.

- *Runs:* "open Barrow Deeps for Thursday 19:30" (or "open the next Barrow Deeps") posts the sheet now;
  "lock tonight's Barrow Deeps" builds the roster and DMs everyone rostered; "cancel bd-1209-1930, server
  down"; "put Xanthe on the bench for bd-1209-1930" / "set Marrow to join as Marrowlite" / "Kestrel can't
  make it" (after lock: join seats and asks to confirm, no thanks frees the seat and the fill engine looks for
  cover); "who would fill ask next for tonight's run" (preview, nothing sent) and "send the fill asks"; "split
  tonight's run first-roster-first" (balanced / first / rotation); "auto-fill the board for bd-…" (before lock);
  "groups for bd-…: Ash, Bright, Cinder | Dusk, Ember" sets the board — the layout the lock will use, or after
  lock the roster itself (names added are asked to confirm, names dropped are freed). Pins and per-run seats
  stay as before ("pin Ash to the roster", "keep Dusk on the bench").
- *Members:* "add Jon's alt Jonny, Mage Frost" (value is `Class Spec [Offspec]`), "change Jonny to Fire",
  "offspec for Jonny: none", "name Jon's planned Shaman Stormcall" (planned characters only — a named
  character is never renamed: retire it and add the new one; its class never changes either), "rank Jonny
  raider", "make Jonny Jon's main", "retire Jon's alt Jonny"; "Marrow is back, clear her absence" (by start
  date when she has several); "turn DMs off for Marrow" (off: never asked to fill, confirmations wait on the site).
- *Test bench:* "seed 20 test members", "open a test Barrow Deeps run" (the `/gm test run` defaults: starts in
  40 min, nudge 32, lock 25, confirm 15 min before), "clear the test bench".
- Several things in one sentence become several operations, applied in order ("lock tonight's run and turn
  DMs off for Marrow"). Loot items, tiers, wishlists and standing availability are still not settable anywhere.

## 8a. Rehearsing with the test bench (officers)

`/gm test seed count:20` creates puppet members (fake Discord ids, a realistic class mix and ranks, mains
confirmed). `/gm test run raid:<id> start_in:40 lock_in:25 confirm_in:15 nudge_in:32` opens a real sheet for
that raid in the signup channel with minute-level cadence, so the unchanged scheduler goes through nudge →
lock → confirmations → expiry → fill → close within the hour. `/gm test answer` makes puppets answer (a random
mix with `join: bench: out:` counts, or one puppet with a status); after lock a puppet's *No thanks* is a
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
| `open_run` | open a sheet now (next slot, or a date and time) |
| `set_answer` · `pin` · `confirm_for` | Join / Bench / No thanks for someone (character swap) · pin in/out for the lock · answer a Confirm ask for them |
| `propose_split` · `set_strategy` · `autofill` · `set_layout` | preview a split (nothing saved) · remember the strategy · solver shapes the board · set the groups |
| `lock_run` · `fill_seats` · `cancel_run` | lock now · preview the fill asks, `send=true` sends them · cancel with a reason |
| `raid_set` · `raid_reset` | one raid setting (slots, hours, weights, comp bounds, first open…) · back to defaults |
| `aura_set` · `family_set` · `aura_reset` | what the guild learns about buffs and stacking families |
| `add_absence` · `clear_absence` | away dates for someone (sheets answered / re-opened) |
| `save_characters` · `set_member` · `set_dm` | character table rows · rank / confirm / main · DMs on or off |
| `set_channel` · `set_config` | a bot channel by name · timezone, ask audience, about, officer roles |
| `test_bench` | seed / run / answer / clear, as `/gm test` |
| `plain_change` | text → the config ops it means, with current → new; applied only with `apply=true` |

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
capped by a budget the owner sets; `/gm status` shows it.

## 10a. Asking the bot questions

`/ask`, a DM, or an @mention outside the officer channels gets a free-form answer from this manual and
your own record. Who may do that is set by the owner (`ask_audience`: officers, confirmed members,
registered members, or everyone; default registered). Anyone outside that audience gets the **static
guide** instead — a menu with *About the guild*, *Raid schedule*, *How to register*, *How signups work*,
*How to apply*, *Who to contact* — built from the guild's settings, no AI involved.

What `/ask` knows is what the site shows, in one bounded snapshot taken when you ask: the guild's channels,
officer roles, owner and ask audience; every raid's cadence (slots, when sheets open, nudge, lock and
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
changing anything is `/gm change`.

## 11. Things the bot cannot do (yet)

Loot: add or tier loot items, record wishlists, list or retire precedents, or show loot history — the
loot council only runs on the demo data until Forever's loot tables exist. Splits: the **Loot-quality**
split philosophy is a greyed placeholder until wishlists exist (Balanced, Raid one first and Rotation
work). Buff facts, comp bounds, cadence and split policy *can* be changed (Auras page, Raids page,
`/gm config raid|aura`, plain text); the buff list itself and the item tables are files in the code
repository. There is no standing roster or availability to set, no attendance or loot view on `/me` yet,
and no way to sign up for a run anywhere except its sheet. If you ask for one of these, say so plainly.
