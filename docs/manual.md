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
Fire → ranged…); an offspec in another role counts as flexibility.

## 2. Channels

- **#register (registration channel)** — read-only; the pinned card has three buttons: *Register / plan my
  main*, *Add an alt*, *My status*. Anyone can press them.
- **Signup channel** — the weekly sheets with In / Tentative / Sub only / Out buttons.
- **Roster channel (officers)** — health cards, roster proposals, fill progress (🧩 lines).
- **Analytics channel (officers)** — the character bank, and per roster: pool readiness, optimised
  groups, desired comp. They re-post at the bottom after every change, with a change-log line above.
  Officers change comp ideals here in plain text by @mentioning the bot.
- **Ops channel (officers)** — every action the bot takes, one line each; errors also DM the owner.
- **Applications channel** — review cards for `/apply` with Accept / Decline buttons.

## 2a. The website

**https://gm.earlyandoften.gg** — log in with Discord. Everything below can also be done there with forms:
your characters (add, spec/offspec, name at launch, make main, retire), standing availability, absences,
flex roles and DM opt-out on **Me**; officers get **Admin** (place members on rosters, ranks, confirmations,
roster settings, comp targets, group layout), **Bank**, **Rosters**, **Raids**, **Config** and **Ops**. Only
members of the Discord server can log in; officer pages follow the same rules as the officer commands.
Signing up for a raid is still done on the sheet in Discord.

## 3. Members: registering and keeping your record current

- **Register / plan a main**: press *Register / plan my main* in #register (class → spec → optional
  offspec → name; leave the name blank until the character exists) or use `/register` / `/me plan main`.
  Pressing the button again replaces a planned main (roster placement and rank carry over). A named
  character waits for an officer to confirm it.
- **Alts**: *Add an alt* button, `/me char add`, `/me plan alt`.
- **Change spec/offspec**: `/me char spec`. **Switch main**: `/me char main`. **Retire**: `/me char retire`.
- **Name a planned character at launch**: `/me char name`.
- **Extra roles you'd play**: `/me plan roles` (your main role always follows your spec; this adds flex).
  If *My status* shows a flex role you no longer want (e.g. "healer" left over from an earlier plan), run
  `/me plan roles` again with only the roles you mean — it overwrites the stored preference.
- **Standing availability per roster** (in / out / sub-only): `/me availability`. It pre-fills sheets so you
  only act on exceptions.
- **Absences**: `/me absent add start [end] [reason]`, `/me absent list`, `/me absent clear`. Absent days
  pre-fill you as Out and the bot won't ask you to fill on those days.
- **Raid times you can make**: on the website (Me → *Raid times I can make*) rate each candidate slot the
  officers listed as yes / maybe / no. Officers use the heat-map to decide when the 10-mans run.
- **See everything the bot has on you**: `/me view` or the *My status* button.
- All `/me` commands also work in a DM with the bot.

## 4. Rosters and the weekly cycle

A **roster** is a named run with a size, a schedule ("Tue 19:30" server time), an instance, and cutoffs. A character
can be on the 20-man and a 10-man as long as the times don't overlap; each raid has its own lockout (weekly for now)
and a member is only ever in one raid per time slot. 10-mans are expected to be rebuilt each lockout from
availability, signups and who needs what.
Officers place a member's character on a roster with `/roster add` (one character per member per
roster). A character can be on several rosters; people not on a roster can still sign as *Sub only*.

Timeline for each raid (times from the roster's settings; defaults in brackets):

1. **Open** — `open_days_before` [6 days] before the raid the sheet is posted, pre-filled from availability
   and absences (roster members default to In). With `open_dm` on, everyone on the roster is DMed the
   sheet with the same buttons. Officers can open early with `/raid open`.
2. **Health check** — at the soft cutoff [48 h] the officer channel gets a health card (headcount, tank/
   healer tiles with who could cover via offspec/flex/alt, buff coverage, non-responders, double-booked
   members) and non-responders are nudged by DM.
3. **Fill** — from the health check until lock, if `autofill` is on [yes], the bot DMs the next best
   people to close gaps: subs already on the sheet → roster members who haven't answered → members of
   other rosters who are free that night → In players who could play their offspec → In players with an
   alt of the needed role. It asks the shortfall + 1 at a time, never more than 3 outstanding, and never
   asks someone who is In on another raid within 4 hours, absent, or opted out of DMs. Each DM has
   *Yes, count me in* / *Can't this time*. A callout after the health check triggers a replacement ask.
   Officers can run or preview a batch with `/raid fill`.
4. **Lock** — at the hard cutoff [24 h] signups lock and the solver proposes a roster and groups,
   posted to the roster channel with advisories. Officers adjust in plain text in the sheet channel
   ("swap X and Y", "bench Z", "keep A with B") and `/raid accept`.
5. **Raid** — `/raid loot` opens the loot council thread (when loot tables exist for the game version).
6. **Close** — 6 h after start the sheet closes.

If you can't make a raid you've signed for: press *Out* on the sheet, or `/raid out [note]`. After lock this
is a **callout** and is recorded as late.

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
- **Pool readiness** per roster — every planned/active main vs the roster size: headcount, role tiles,
  buff coverage, who isn't placed on the roster yet.
- **Optimised groups** per roster — the solver on the pool at full size with open slots shown, aura
  badges per group, totem picks, raid-wide buffs.
- **Desired comp** per roster — derived targets per role/class/spec with a one-line reason each,
  have vs want, and how many could fill a slot by switching to their offspec. Officers override targets in
  plain text: "reduce healer to 3-5", "cap hunters at 3 because Trueshot", "clear the paladin target",
  "groups tank, healers, melee, casters".
- `/roster overview` shows the readiness card on demand; `/roster members` lists a roster.

## 7. Officers: day-to-day commands

- Registry: `/roster list|confirm|rank|set-main|add|remove|members|absences|availability|absent`.
- Applications: `/roster applicants`, `/roster applicant`, or the buttons on the review card.
- Raids: `/raid open|sheet|health|fill|lock|accept|set|cancel|list|loot|end`.
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
`signup-channel`, `applications-channel`, `officer-role`, `timezone`, and one `/gm config roster` per
team (key, size, schedule, instance, soft/hard cutoffs, open days, open_dm, autofill).

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
