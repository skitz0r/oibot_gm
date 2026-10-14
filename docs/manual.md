# oibot_GM — how the bot works (member and officer manual)

This is the bot's own description of itself. It is injected, with the live command list and the guild's
current settings, whenever someone asks the bot a question (`/ask`, a DM, or an @mention). Keep it truthful: if a behaviour changes, change it here in the same commit.

## 1. What the bot is

oibot_GM is the guild's raid administrator. It keeps the character registry, opens signup sheets on a
schedule, checks the roster's health, builds groups, fills freed seats by DM, and — once loot tables exist —
runs a loot council whose recommendations an officer confirms or overrides. Code does the computing
(solver, scoring, schedules); Claude only explains, judges against the written policy, and turns
plain-text requests (members' about their own record, officers' about anything the site or a slash command can do,
except handing the guild to a new owner and the loot session — §8) into typed changes that are shown as a diff and
confirmed before they apply.
Humans always decide: every roster, award and config change has an approve step, and overrides are
stored with their reason as precedents.

Your Discord account is your identity. Character names can be added later (before launch a "planned"
main has no name yet). Roles are derived from the spec you register (Protection → tank, Holy → healer,
Fire → ranged…); an offspec in another role counts as flexibility. There is no standing availability to
fill in anywhere: you answer each run's sheet when it is posted, and you post absences for days you're away.

## 2. Channels

- **#register (registration channel)** — read-only; the pinned card has four buttons: *Apply to join*
  (newcomers start here), *Register / plan my main*, *Add an alt*, *My status*. Anyone can press them. The card
  says when applications are closed and shows the recruitment note; the bot edits it in place when those change.
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
- **Applications channel** — one review card per application (§3a) with Accept / Decline… buttons (they keep
  working after a bot restart).
- **News channel** — a Wowhead webhook posts game news there. The bot reads only the post itself (title and
  summary, never the linked article) and keeps the items about our game version, our raids, or the extra news
  keywords on the Config page. A daily review compares them with what the bot believes about the game; a
  contradiction becomes a **news proposal** card in the ops channel (§7a).

## 2a. The website

**https://gm.earlyandoften.gg** — the homepage (for visitors who aren't logged in) shows the guild's name and
Discord icon, a one-line intro, the owner's *about* paragraph, **Join our Discord** when the owner has set an
invite (no invite, no button) and **Log in with Discord** for members. Anyone can join the Discord server;
new raiders apply there with `/apply` (there is no application form on the website). Someone who logs in
with a Discord account that isn't in the server yet gets a short page saying so, with the Join button and a
log-out link, instead of an error; after joining they log in again. A logged-in member goes straight to **Me**.

**When the bot looks after more than one Discord server**, the homepage shows the servers side by side, each with
its own icon, name, about text, **Join our Discord** (its own invite) and **Log in with Discord**. Once you're logged
in, a **server selector** at the top of the left rail lists the servers you can use (you're in that Discord server, or
you own its guild) with your role in each; pick one and every page, save and live update is for that server only. Your
choice is remembered in this browser. Links the bot posts in Discord (Open the board, Members, Raid rules) open the
right server. If you leave the server you had selected, the site opens on another one you're in and tells you so; if
you're in none of them you get the not-a-member page with every server's Join button. A link to a server you aren't
in opens one you are in, with a note saying so. If Discord doesn't answer whether you're in a server (it happens for
a few seconds now and then), the site says so with a **Try again** button rather than switching you to the other
server. Owners and officers are per
server: owning or running one guild gives you nothing in the other. With one server there is no selector and the site
looks exactly as before.

Log in with Discord. Everything below can also be done there:
your characters (press **Edit characters**: add a row, first + last name, spec/offspec, main/alt, then one **Save changes**; the crown marks your main, the trash icon deletes), absences,
your sheets and confirmations, and the DM switch on **Me**; officers get, in the left rail, **Rosters** (per raid: sheets open for signup with every answer, pins, the board and *Lock now*; locked runs with confirmations, freed seats and **Fill seats**; upcoming slots; history), **Raids** (slots, cadence, nudge/lock/confirm/fill-ask hours, autofill, open_dm, comp and seat weights per raid; the owner presses **Edit rules**), **Auras** (what the guild knows about each buff; the owner edits), **Members** (every member's main and alts in one table, with an owner/officer/member badge taken from Discord roles, a Confirm button for named characters, and their upcoming absences; **Edit members** lets an officer change any member's characters exactly as on Me, with one save; the **Test bench** card builds puppet members of any mix and a comp sandbox, see §8a), **Config** (owner edits; officers read), **Ops** (the bot's recent actions, the plain-text permissions, and the loot
**precedents** and **ledger** — read-only tables, newest first) and **Agents** (the news review and apply jobs on the host: running or not, what the current run is doing step by step, past runs, the news kept, the proposals with Approve / Dismiss). Only
members of the Discord server can log in; officer pages follow the same rules as the officer commands.
Me, Rosters, Members, Ops and Agents update live the moment something changes (a press on a sheet, a drag by another
officer, a change in Discord); a 45-second refresh is only the fallback when the live connection drops. There is no
standing signup: each run is answered on its sheet in Discord, or by telling the bot ("bench me for tonight").

## 3. Members: registering and keeping your record current

- **New here? Apply first.** Anyone can join the Discord server with the public invite; joining the guild is an
  application, made in Discord (§3a). Someone with nothing registered who presses *Register / plan my main* or
  *Add an alt*, or runs `/register`, `/me plan main|alt` or `/me char add`, is sent to the application with one line:
  *"New here? Apply first — the officers take it from there."* Plain text ("add my alt …", "plan my main as …") says
  the same, with the **Apply to join** button on the reply while applications are open. Officers, and anyone holding one of the guild's **member roles** (set by the owner, §8), register
  directly; anyone already registered is never affected and keeps adding alts as below.
- **Register / plan a main**: press *Register / plan my main* in #register (class → spec → optional
  offspec → first and last name; Forever characters have two-word names; leave them blank until the
  character exists) or use `/register` / `/me plan main`. Each opens the same picker (class → spec → offspec →
  names). Nothing is typed but the name, and a refused name reopens the form with your picks kept.
  Pressing the button again replaces a planned main (roster placement and rank carry over). A named
  character waits for an officer to confirm it.
- **Alts**: *Add an alt* button, `/me char add` (first and last name, like the button), `/me plan alt`.
- **Change spec/offspec**: `/me char spec`. **Switch main**: `/me char main` (or the crown on the website).
  **Delete**: the bin icon on the website, or telling the bot ("delete my alt Jonny"), removes a character outright
  (a seat it already holds on a locked run is not freed by the delete — press *Can't make it* for that run); if it was your main, your next character becomes main (`/me char retire` keeps history
  instead).
- **Name a planned character at launch**: `/me char name`: pick the planned character (skipped when there is
  only one), then type first and last name.
- `/me char spec|main|retire` take no arguments: pick the character from your own list. Spec choices are always
  that character's class, and retire asks you to confirm. `/apply` is the application (§3a).
- **Extra roles**: your role follows your spec and your offspec counts as flex; `/me plan roles` adds roles your main
  would play beyond that, and telling the bot sets them on any of your characters ("I can also tank on Xanthe", "no
  extra roles on Jonny"). The site has no flex toggles yet; officers can also set a character's flex list over MCP
  (`save_characters`).
- **Absences**: `/me absent list` shows yours with a **Clear** button on each; `/me absent clear` does the same.
- **Away for a while**: press *I'll be away* on the card in the absences channel, or `/me absent add`, or the Me
  page. Nothing is typed: pick the **first day** (Today, Tomorrow, then the days after — *Later than this…* picks a
  week further out) and **how long** (1 day up to 4 months), with an optional reason only you and the officers see. A confirm
  screen shows the days in words and which live sheets it touches before anything is saved. Sheets on those days
  get *No thanks* for you, and if you were already rostered the seat is released (you get a DM saying so) and the
  bench is asked; the reply names the sheets it touched (on the Me page and over MCP too). Officers record one for
  someone else with `/roster absent` and a member picker, same screens.
- **See everything the bot has on you**: `/me view` or the *My status* button.
- All `/me` commands also work in a DM with the bot.
- **Or just tell the bot.** DM it or @mention it: "I'll be away Nov 1–3", "back early, clear my absence",
  "make Xanthe my main", "change Xanthe to Holy", "add my alt Jonny, Mage Frost", "add Jonny as my main",
  "plan my main as a Holy Priest" (a planned character, no name yet; saying it again replaces the plan and keeps
  its rank and roster placement), "delete my alt Jonny", "I can also tank on Xanthe", "bench me for tonight",
  "I can't make tonight" (a callout like `/raid out`; "… — work trip" adds a note only the officers see), "I confirm for
  tonight" (also answers a fill ask the bot DMed you), "turn my DMs off". It shows what it will change and waits for
  your **Apply** — only you (or an officer) can press it. Plain text only ever changes *your own* record: a
  request about someone else is refused with who can do it, and the rest of what you asked still applies.
  Ranks are officers' (see §8c for who may do what, and where).

## 3a. Applying to join

Press **Apply to join** on the registration card, or run `/apply` (also in a DM with the bot). It is one private
message that moves forward screen by screen; nothing is sent until you press **Submit**, and closing it changes
nothing.

1. **Class, main spec and an optional offspec** — the same picker as registering. The guild's recruitment note (e.g.
   "Looking for healers") is shown here.
2. **Your character's first and last name** (2–12 letters each) and an optional logs or armory link. If the guild
   has no questions of its own, this form also has an optional *Anything else?* box.
3. **The raid schedules you can make** — pick every one that fits (or none) from the guild's active schedules, e.g.
   "Barrow Deeps · Tuesdays and Thursdays 7:30 PM". Skipped when the guild has no schedules yet.
4. **The officers' questions** — the guild's own list (the owner edits it, §8), five per form. With more than five,
   a *Next questions (2 of 3)* button leads to the next form. Required questions must be answered; each answer
   holds up to 1000 characters. A refused answer says what is missing and **Fix this** reopens that form with
   everything you typed kept.
5. **Review** — every answer on one screen (long ones shortened for the screen only). *Change a step* goes back to
   any step and returns here; **Submit** sends it; **Cancel** drops it.

When applications are closed, `/apply` and the button say so instead of opening. You can have one open application
at a time; *My status* on the card tells you it is waiting.

**Officers** get a review card in the applications channel: the full character name, class and spec icons, the logs
link, the schedules the applicant can make in words, and every answer as its own line. A very long application is
shortened on the card with "…", and its last line says where the full answers are: ask the bot ("show Kessa's
application", officers) or MCP `list_applications` — the site has no applications page.
**Accept** registers the character as the applicant's main, rank trial, already confirmed; **Decline…** takes an
optional note. Either way the applicant gets a DM, the card updates to show the decision, and the ops channel logs
it. The same decision is on `/roster applicant`, in plain text ("accept Nyx", "decline nyxie: we're full on
healers" — officers by default, and the note goes to the applicant) and over MCP (`decide_application`), all through
the same code. A newcomer who tells the bot to register a character is refused with *"New here? Apply first"* and
gets the **Apply to join** button on that reply. A declined applicant is never
removed from the Discord server; they can stay and apply again later.

## 4. Raids, runs and the signup cycle

A **raid** is an instance and its rules: size, lockout cadence, expected duration, desired tank / healer /
dps counts, and — what drives everything — its **slots** and **cadence**. At launch: **Barrow Deeps**
(10-player, 3-day lockout guess), **Hyjal Summit** (20-player, weekly), **Onyxia's Lair** (40-player,
5-day like Classic). Each raid has a **first opens at** (seeded 9 Dec 2026, 3:00 PM PST for all three); no
sheet opens before it. The owner sets all of this with `/gm config raid`, on the Raids page, or in plain
text (`raid_set`). Every run is built from its own sheet; a recurring schedule also has **regulars** (below) — the
people expected each week — but nobody's answer is ever filled in for them.

A **slot** is a recurring run time in the guild's timezone (US Pacific), e.g. Tue 7:30 PM (on the site and in Discord you pick the weekday and time; nothing is typed). A raid can have
several. One **run** = one slot occurrence = one sheet. A run is **open**, then **locked**, then **done**
(or **cancelled**). Members never keep standing availability; they answer each sheet.

**Schedules** say *when* a raid runs; the raid itself keeps what it is (size, comp, lockout, weights) and its
cadence as the defaults. A raid's own run times are its first schedule (*Raid nights*); it can have more, each
with a name, and each can set its own cadence (when its sheet opens, nudges, locks, confirms, how long fill asks
wait, autofill, DM on open, split policy) — anything it doesn't set follows the raid's. Three kinds:
- **Weekly nights** — run times like the raid's own ("Saturdays 8:00 PM"). A Tuesday main night and a Saturday alt
  run of the same raid, each with its own lock time.
- **Days of each lockout** — "Day 1 of each 3-day lockout, 8:00 PM": day 1 is the day the lockout resets, counted
  from the raid's first opening every `lockout_days`, so a 3- or 5-day reset is followed exactly (and daylight saving
  never moves the wall-clock time; a time the clocks skip that night moves past the gap).
- **Pickup template** — never opens by itself. `/raid open` → *Ad hoc run…* offers it (*Pickup — pick a time*), the
  Rosters page has it in *Start ad hoc run*'s template picker beside the date and time; the run gets the template's
  settings.
Each schedule also says how many **rosters** a run expects (1–4). With 2, the sheet advertises 2 × the raid's size
(20 seats for a 10-player raid), the health check and the fill engine measure against two rosters, and the lock aims
for two — as far as the tanks and healers who joined allow (it never builds a roster its minimums can't fill). A
schedule can be **paused** (nothing opens from it). Runs from a schedule other than the raid's own run times carry
its id in their key (`bd-1212-2000-alt`), so two schedules starting at the same minute are two runs.

**Recurring and ad hoc.** Every run is one or the other, and nobody sets it by hand — it follows the schedule's kind:
- **Recurring** — weekly nights and days of each lockout (the raid's own run times included). Opens by itself on its
  cadence, has **regulars**, and its lock weighs being a regular (below). Confirmations on by default.
- **Ad hoc** — a pickup template, or a **one-off** run at a time an officer picks (*Ad hoc run…* in `/raid open`,
  *Start ad hoc run* on the Rosters page, or plain text / MCP with a time that is none of the raid's upcoming runs).
  It takes the raid's size, comp, lockout and length but none of its recurring timing — its own lighter defaults: no
  reminder, no DM on open, **no confirmations** (Join is the commitment; *Can't make it* frees your seat), locks
  **2 h** before, fill asks wait **1 h**, and seats go **first come, first rostered** (the joiners in the order they
  first pressed Join take the seats — pressing Join again, switching character or saying yes to an officer's ask keeps
  your place; Bench or No thanks and then Join again puts you at the back — except that a seat still needed for a tank or healer minimum is kept for the first later joiner who plays
  that role). An officer's *Bench* pin (or a comp-policy *always bench*) takes that joiner out of the line, so the
  next joiner gets the seat; a comp-policy *never bench* name keeps a seat even if they joined late. Nobody is expected on an ad hoc sheet, so there's no "No answer" list and nobody is reminded. A one-off's
  key ends in `-adhoc` (`bd-1212-2100-adhoc`). The Raids page shows each schedule's class, and for a pickup template
  the editor hides the fields that mean nothing there (signup lead, reminder, DM on open); every value it inherits says
  where from (*raid* or *ad hoc default*). The owner can change any of them per schedule, including **confirmations**
  (on/off) and the **seat order** weights — on the Raids page, in plain text, over MCP, or in `/gm config raid`
  (*Behaviour*: the raid's **Ask the rostered to confirm** switch; per schedule, *Change a switch* → default / on / off).

**Regulars** (recurring schedules only). Officers keep, per recurring schedule, a list of **regulars**: one member and
the one character they raid on there. Regulars are the people the bot *expects* on every sheet of that schedule:
- **Nothing is answered for them.** A regular starts every sheet unanswered like everyone else and presses Join, Bench
  or No thanks. Anyone with a recorded absence covering the day — regular or not — is marked away (No thanks, the
  *Away* row) automatically: when the sheet opens, and on a sheet already open when the absence is recorded; clearing
  the absence puts them back to unanswered.
- **Officers see who hasn't answered**: the sheet card on the Rosters page lists *Regulars who haven't answered* (one
  per line, spec icon + character; the member's name on hover) with how the other regulars answered; the officer
  health card's **No answer** names them; MCP `get_run` and an officer's `/ask` carry the same list. The list is
  officer-only: a member's `/ask` gets the count of who hasn't answered, never the names (nor the names of who could
  cover a short role but hasn't signed).
- **The reminder** (the nudge DM) goes to the regulars who haven't answered. While a schedule has no regulars yet,
  every main counts as expected (exactly as before regulars existed).
- **At lock** being a regular on your regular character is a seat weight (`regular`, 4 by default — more than one rank
  step): between equals the regular sits, but a much better fit for the comp can still win a seat. A regular who agreed
  to an officer's ask to bring an alt (or play an offspec) keeps that weight, and the main weight, on what they were
  asked to bring. A non-regular's Join
  stays a Join; they are seated after regulars and are the first asked when a seat frees.
- **Fill** asks the bench first, then the regulars who haven't answered, then other members (the Fill modal labels
  each ask *from the bench* / *a regular, no answer* / *not a regular*; on an ad hoc run or a schedule with no regulars
  yet, where everyone counts, it says *no answer* — nobody is called a regular there). The DM says only what the bot
  knows: "you haven't answered the sheet" — never that you're free that night. See *Suggestions* below for short tanks and
  healers.
- **Regulars coverage** (no run needed): per recurring schedule, the regulars' role counts against the tank and healer
  minimums (role icon and *count/minimum*, red when short), coloured **green** (the regulars' own characters cover the
  minimums with a spare in each), **amber** (exactly the minimum with no spare — one absence leaves it short, e.g.
  tanks 2/2 — or covered only with offspecs, a declared flex role or alts; the card says which, and who could cover) or
  **red** (they can't field the minimum — the card names members who play the missing role, best rank first, to
  recruit). Beside it a **4-week forecast**: for each upcoming
  run in the next four weeks (counted from the raid's first opening until then) the regulars minus recorded absences, e.g. "Tue 15 Dec: short
  1 tank, 1 healer (Thorn, Ona away)" — the hole shows a week ahead instead of at reminder time. The same numbers show
  on the Rosters page's *Regulars* cards (the colour under the role counts, a *Next 4 weeks* list: role icon and a
  number per short role, who is away on hover), in an officer's `/roster overview` (*Regulars*), on each raid's analytics card (one
  line per schedule: a 🟢/🟠/🔴 dot, role icons with counts, ⚠ when short, regulars / seats, then a line per short run)
  and in an officer's `/ask` about readiness.
- Officers edit regulars on the Rosters page (*Regulars* tab: one card per recurring schedule — role icons with counts
  against the minimums, the regulars one per line, *Add* a member and character, × to remove), with `/roster add`,
  `/roster remove` and `/roster members` (pick the schedule, then the member, then the character), in plain text ("make
  Ana a Tuesday Barrow Deeps regular with Anabelle") or MCP `set_regular`. One character per member per schedule; the
  same character on two schedules of one raid is allowed with a warning (one lockout). A test puppet is never a
  regular — a test run treats every puppet as one. Removing a schedule never strips its regulars silently: the Rosters
  page lists them to clear.
- The **Members** page shows each character's **Rank** (it weighs on seating) and **Regular on** (the schedules it is a
  regular of); officers change regulars on the Rosters page and ranks with `/roster rank`.
- **Members** see *My regular raids* on the Me page and *Regular on* in `/me view`; **Step down** (either place)
  takes you off a schedule's regulars. Becoming a regular is an officer's call — ask one.

**Suggestions: covering a short tank or healer seat.** When a run's tank or healer minimum is short, the bot works out
who could cover it — and **sends nothing until an officer presses Ask**. It never changes anyone's spec on its own.
- **Who counts.** Before lock: who joined, plus (on a recurring schedule with regulars) the regulars who haven't
  answered and aren't away — they are expected anyway, so the suggestions go beyond them — plus anyone already asked and
  not yet answered. After lock: who is rostered.
- **Cheapest first**, each with a reason written by the bot (e.g. "short 1 healer · Ana's declared flex Holy · DPS
  stays 6 of 5 minimum"): someone on **Bench** who plays the role or a joiner's **declared flex role** (*/me plan
  roles*) · a joiner's **offspec** or a **regular who hasn't answered** · an **alt** of the role (never one already
  rostered on this raid in the same lockout) · a **member who isn't a regular**. When every seat is already taken,
  switches by people coming go first — a new healer would only push a DPS to the bench at lock. A switch that would
  drop the role someone leaves under its minimum is offered only tied to someone covering it, and both must say yes:
  the first yes changes nothing until the other answers; the second yes makes both changes; a no (or silence, or the
  lock arriving first) from either closes the other with a "never mind" DM. Nobody is suggested for two seats (the
  *Suggest…* screen refuses picking one person for two), and the bot never takes the one person a later seat can't do
  without. Test puppets are never suggested or asked on a real run.
- **Two alternates** per short seat, so you can ask someone else without typing a name, and a **Still short if all say
  yes** line ("nothing", or what to recruit).
- **Where.** The officer **health card** gets a *Suggested* block (up to three seats; one member per line, the spec
  icon they'd play and the character) with **Ask** (sends what the card shows — only those seats: when more are short
  the card says *+N more* and Ask leaves them alone) and **Suggest…** (every seat with its alternates in a select, then
  *Send*). The block, the regulars who haven't answered and the buttons are for
  officers: a member who runs `/raid health` sees the counts, what is short and the missing buffs only. On a recurring schedule with
  regulars the health card is posted **as soon as the sheet opens** when the regulars (minus anyone away) can't cover a
  minimum — an early warning — and posted again, rebuilt, at reminder time. On the site, **Suggest** beside the sheet's
  *short* badge opens the same list (pick the suggestion or an alternate per seat, then *Send*). `/raid fill` on an open
  run, plain text ("suggest fixes for Tuesday's run", then "send them") and MCP `fill_seats` give the same list.
- **A stale list is refused.** If the suggestions changed since you looked (someone joined, someone answered), *Ask*
  sends nothing and the list is redrawn as it is now — check it and press again.
- **What the member gets** (before lock): a DM — "Barrow Deeps <time> is short a healer. Could you come as Holy?"
  with **Yes, as Holy** / **No**. **Yes** changes their sign-up (Join, as that spec or character) and rosters nobody:
  the lock still builds the roster and now sees the agreed role. **No** frees the seat in the suggestions and the next
  alternate shows on the card. At most one ask per member before lock; unanswered asks close when the roster locks
  (pressing them afterwards says "That question closed when the roster locked"). After lock the fill engine may ask
  the same person again for a seat freed later — someone who said yes before lock and wasn't rostered is the first
  asked.
- **Someone asked can't come after all.** If a member you asked records an absence for that day, answers No thanks or
  calls out, their ask closes at once (the other half of a swap is told it's off), the seat shows short again and the
  card suggests someone else; a late yes on the old DM is not taken (they're told to clear the absence first).
- **The health card follows the sheet**: every answer, and an absence recorded or cleared, redraws it. Once the run
  locks it says *Locked* and points at the lock card — its *Suggested* block and **Ask** go (pressing an old Ask says
  the run has locked).
- **After lock** the fill engine (step 6 below) uses the same list. On a **recurring** schedule it asks people for the
  same role on its own, but a **role swap** (a flex role, an offspec or an alt, with or without someone covering)
  waits for an officer: the lock card shows it under *Suggested* with **Ask** — while it waits, a seat freed later
  (a missed confirmation) still gets its same-role asks automatically. An officer's Ask after lock counts the asks
  still out (nobody is asked twice for one seat) and keeps the cap of 3 questions out at a time: what doesn't fit
  stays on the card ("1 more waits"). On an **ad hoc** run the fill engine sends swaps itself, as before.

Timeline for each run (per-raid settings; defaults in brackets, Barrow Deeps opens 48 h before):

1. **Sheet opens** — `signup_lead_hours` [120 h] before the slot the bot posts the sheet in the signup
   channel: **Join** (I'm coming), **Bench** (call me if you need me), **No thanks** (⚑ after a name = called
   out after answering). With more than one character you pick which one. Anyone with a registered absence that day
   is listed under **Away** instead (names only, the first 15 then "+N"; reasons stay with officers): they aren't
   nudged or asked to fill, and pressing Join or Bench anyway takes them out of Away. On a recurring schedule with
   regulars, if the regulars minus anyone away can't cover a tank or healer minimum, the officer channel gets the
   health card with its *Suggested* block right away (an early warning; see *Suggestions* above). With `open_dm` on [off] everyone
   the sheet is for (the schedule's regulars, or every main while it has none) is also DMed when it opens. Officers can
   open a sheet early from the Rosters page: **Open next run** takes the raid's next scheduled run (with several
   recurring schedules, a picker beside it chooses which), and **Start ad hoc run** opens a one-off (or a pickup
   template, picked beside it) at the date and time you pick. `/raid open` does the same in Discord: pick the raid,
   then **Next run — <time>**, **Another upcoming run…** (each labelled with its schedule's name when there are several)
   or **Ad hoc run…** (a pickup template, or *One-off — pick a day and time*: day, hour and minutes from lists; nothing
   typed; ad hoc defaults). The confirm screen shows
   the time in guild time and in your own. A raid with no run times offers *Set this raid's run times →*. A run opened closer
   than its cadence assumes keeps a usable window: a pickup two hours out nudges, locks and confirms inside those two
   hours instead of inheriting a lock time that has already passed. A start more than five minutes in the past is
   refused however the run is opened (the site, `/raid open`, plain text, MCP), with the time named in words.
2. **Nudge** — at `nudge_hours_before` [halfway between open and lock: lock + (lead − lock) / 2, so 72 h for a
   sheet that opens 120 h out and locks at 24 h] the officer channel gets a health card (headcount, tank/healer
   tiles with who could cover via offspec/alt, buff coverage, **No answer** — the regulars who haven't answered, one per
   line — double-booked members, and the *Suggested* block with **Ask** / **Suggest…** when a minimum is short; an early
   card from the sheet's opening is replaced by this one) and those regulars (every main while the schedule has none) are nudged once by
   DM. An ad hoc run reminds nobody; its health card comes an hour before its lock. The tiles count against the seats and tank/healer minimums of the
   rosters the run expects (1, or its schedule's *rosters per run*) — the same numbers Fill works from; when the
   joiners could make more runs than that, the "Enough people for 2 runs …" line says what another run is short. `nudge` [on] switches the DM off per raid; the health card
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
   empty seats. An answer or pin that changes while the lock is building the roster (someone presses *No thanks*,
   you keep someone on the bench) makes the lock build it again from the sheet as it now is, so nobody is rostered
   from an answer they already took back; if the sheet keeps changing, the lock fails, the sheet stays open and
   you lock again in a moment. More Join answers than a raid needs → the board shows a second (third…) roster for the same
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
   make it* button); the officer channel gets the roster cards. A roster that came out under its tank/healer minimum
   (nobody else could fill it) says **Locked short** with the role icons and a number, instead of locking quietly; the
   card also lists who the solver moved to an offspec (*Playing an offspec*, one per line) and, on a recurring schedule,
   the **regulars not rostered** (who joined but sat out — they get priority next time). Pre-lock asks nobody answered
   close now. Every rostered member gets a DM: **Confirm** / **Can't make it** — showing the spec they're rostered as
   ("You're rostered as **Protection**, your offspec"). The Me page shows the same ask. With confirmations off (the ad hoc default) nobody is asked and
   no seat expires: the sheet says "you're in — press Can't make it if that changes". After lock the board stays editable: moving people
   between groups changes nothing else; dragging someone in from the bank is a substitution (they get the
   confirmation DM); dragging someone out releases the seat (they get a DM) and the bench is asked.
5. **Confirmation deadline** — `confirm_hours_before` [6 h] before the slot, anyone who hasn't answered
   counts as out; their seat is released and they are DMed that it was.
6. **Fill** — the fill engine DMs people on its own only after lock (before lock an officer sends suggestions, see
   *Suggestions* above). Whenever a seat frees (decline, callout, absence, missed
   confirmation, officer removal) and `autofill` is on [on], the bot DMs the next best people, in the suggestions' cost
   order: joiners the solver left off and *Bench* answers first → the schedule's regulars who haven't answered (every
   main while it has none; on an ad hoc run, every main) → other members → **swaps** (a rostered player's declared flex
   role, offspec or alt in the needed role; never the spec the lock already moved them to). On a **recurring** schedule
   a swap is not sent on its own: the lock card shows it under *Suggested* and an officer presses **Ask** (or Fill
   seats); the updates thread says so once. On an ad hoc run swaps go out by themselves. A swap goes out only when the role it vacates stays covered; otherwise the bot looks ahead
   for a bench or pool member of that role and sends both asks tied together — the swap and its backfill — and
   nothing changes until both say yes (the first yes waits; the second makes both changes and tells the first);
   a no (or silence) from either withdraws the other with a "never mind" DM. It asks the shortfall + 1 at a
   time, never more than 3 outstanding, and never asks someone who is rostered on another raid within 4 hours,
   absent, or has DMs off. Every fill DM says when silence starts counting as no: `fill_ask_hours` [4 h] after
   it was sent, never later than the run start; the updates thread logs the timeout and the next person is
   asked. A yes goes straight into the freed seat (a swap changes the seat in place). A roster that is full but
   short a tank or healer has no seat for a newcomer, so only swaps are asked for it; a yes that finds every seat
   taken meanwhile is a Join, not rostered, and the bot says so (move them in on the board). Officers can run a batch
   by hand: **Fill seats** on the Rosters page (locked runs) or on the lock card in the roster channel, and
   `/raid fill`, first show the batch — what is short, who is still being waited on, each seat with its alternates,
   and **exactly the asks Send DMs**, one per line: who, as which character and spec, for which short role, a role
   swap marked **SWAP** (with the role it leaves) and its tied backfill named; what waits under the 3-at-a-time cap;
   what stays short — then *Send N asks* or *Cancel*. Send sends only what was shown: if the board or the answers
   changed the plan meanwhile, nothing is sent and the batch is shown again as it is now. An officer's Send includes
   the role swaps it lists. On a very long list the reasons go first, then the last seat lines ("+N more seats"); the
   batch itself always shows in full — if even it can't fit, *Send* stays off and the site's Suggest sends it.
   `/raid fill preview:true` shows the same batch without a Send button. With `autofill` off
   nothing is asked until an officer presses Fill seats.
7. **Raid** — `/raid loot` opens the loot council thread (when loot tables exist for the game version).
8. **Close** — 3 h after its scheduled end (start + the raid's length) the run is **done** and the sheet says so.

Seat selection at lock is deterministic: role bounds from the raid's comp, buff synergy, the comp policy's
standing instructions, officer pins, and the schedule's **weights** (the raid's unless the schedule sets its own) —
rank (core > raider > trial), main over alt, sat out last window (matched by member, so a Discord rename keeps it),
earlier signup, and being one of the schedule's **regulars** on their regular character. An ad hoc run is first come
instead (see *Recurring and ad hoc* above). A comp-policy line such
as "at least 3 healers" raises that role's minimum and leaves the others (the raid's tank minimum stays). The owner tunes the weights per raid (*Selection weights* in `/gm config
raid`, the Raids page, or plain text: "weight rank 5 for Barrow Deeps"); officers read them, and the owner can open
Raids & auras to others (§8c).

If you can't make a run you joined: press *No thanks* on an open sheet, *Can't make it* on a locked one, or
`/raid out` (a box asks for an optional note; it goes to the run's updates thread for officers, not to the public sheet). After
lock this releases your seat immediately and the bench is asked; *Can't make it* is recorded as a callout like `/raid
out` (hours before, late after the lock). An officer who hears it out of band records the
same callout, note included, with No thanks and a note on the site's answer menu, in plain text ("Kestrel can't
make it tonight, her baby is sick") or MCP `set_answer` with `note`. Your own "I can't make tonight" in plain text is a
callout too, note or not (hours before, late after the lock, the line in the updates thread) — also when an officer
presses Apply on your request for you — and so is MCP
`set_answer` with status `out`; an officer who only sets someone else's answer to No thanks without a reason changes
the answer, no callout. When an officer drags someone onto a locked board, the run's thread says they were asked to
confirm only when the run asks for confirmations (an ad hoc run with confirmations off just says *in*).

**Where the buttons are (site and Discord).** A run's actions sit in one bar under its board and never move: *Cancel
run* far left, then *Fill seats* and *Lock now* on the right; what does not apply yet is greyed with the reason
(Fill before lock, Lock after it); before lock **Suggest** sits beside the sheet's *short* badge. The board's own tools sit above the groups: *Auto-fill empty seats*, *Propose
roster/splits*, and *Clear* set apart. Officer cards in Discord use the same fixed order — Open the board · Lock now ·
Fill seats · Cancel run (the *Suggested* block has its own row: **Ask** · **Suggest…**) — and cancelling asks *Keep the run* / *Cancel the run*. Tables (Me, Members, Raids, Auras)
share one edit mode: an Edit button in the header, then a footer with any reset far left and *Cancel* / *Save changes*
on the right. Config saves each field as you change it and marks it *✓ saved*. Pages update on their own; *Refresh* in
the left rail is the manual fallback. Every yes/no DM (confirmations and fill asks) reads **Confirm / Can't make it**;
a pre-lock ask reads **Yes, as <spec>** / **No**.
A run is locked once, whoever asks first — the site, a card, chat, MCP or the schedule: a second *Lock now* while the
roster is being built is told the solver is already working on it. A move made while the lock (or *Auto-fill*) is
solving waits, then is refused as *"the board changed since you looked"* instead of being lost; check the board and
move again.

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

Every buff assumption carries a status — confirmed, reported, or assumed from Vanilla — shown on the Auras page
and in the board's aura tooltips, because WoW: Forever has not published totem scoping yet.

**Auras page (officers read, the owner edits).** As the guild learns how Forever's buffs really behave, the
owner records it here and the solver, the cards and the roster board follow. Each buff has a **scope**
(party or raid), a **stacking family**, a **strength** and a status; each family says which buffs don't
stack (the strongest present one counts) and **who benefits** from it, as points per role, damage type,
mana user, or a specific spec. Examples: put Blood Pact into Fortitude's family if they turn out not to
stack; set Devotion Aura to raid if it turns out to reach the whole raid; set "only mana users" on Intellect. Plain
text works too ("fortitude and blood pact don't stack", "devotion aura is raid-wide", "hunters don't
benefit from blood pact"), as does `/gm config aura` (pick the buff, then what to change; *Who benefits* changes one beneficiary at a
time and leaves the rest of the family's map alone). Amber on the page marks a guild override; *Reset*
returns to the game defaults.

## 6. Analytics (officers)

- **One card per raid** in the analytics channel, native (no images): mains vs the raid size, a role icon with
  have/need for each role (⚠ = short), ⛔ followed by the buffs nobody in the pool brings, **Short** and **Over cap**
  lines from the desired comp (class or spec icon with have/want), and links to the Members and Raids pages.
- **Desired comp** per raid is derived from the buff matrix and comp rules. Officers override targets in plain
  text: "reduce healer to 3-5", "cap hunters at 3 because Trueshot", "clear the paladin target". The reason you give
  ("because Trueshot") is kept with the target and shown by `/ask` (readiness) and MCP `raid_readiness`, next to the
  line it explains.
- The same readiness as data: MCP `raid_readiness` (one raid or all) and `GET /api/readiness` — headcount, each role's
  have/need with who could cover it, buffs nobody brings, every desired-comp line with its reason, what is short or
  over cap, and the pool plan (officers only; a member's request gets 403). The analytics card reads the same numbers,
  so the card, `/ask`, the API and MCP never disagree.
- The **character bank** lives on the Members page and **groups** are built on the Rosters board (Auto-fill /
  Propose); the old bank, optimised-groups and comp images are gone.
- `/roster overview` (no arguments) shows the plan summary on demand: every planned main by class, the role counts
  against each raid's size with who could flex in, buff coverage (nobody / only one), the alts, and the recruiting ask
  against the biggest raid, and — for officers only — **Regulars**: each recurring schedule's regulars coverage and its
  4-week forecast (recruit candidates and who is away are planning data; a member's overview leaves the field out). `/roster members` lists a recurring schedule's regulars by role (pick the schedule).

## 7. Officers: day-to-day commands

- Registry: `/roster list|confirm|rank|set-main|add|remove|members|absences|absent`. They take at most a member
  (Discord's own picker); the character, rank or schedule is picked from a list and confirmed before anything saves.
  `/roster confirm` alone walks the unconfirmed characters.
- Regulars: `/roster add` (pick the recurring schedule, then the member — or name them in the command — then the
  character), `/roster remove`, `/roster members` (a schedule's regulars by role, one per line). Same list as the
  Rosters page's *Regulars* tab, plain text and MCP `set_regular` (§4 *Regulars*).
- Applications: `/roster applicants`, `/roster applicant`, the Accept / Decline… buttons on the review card, or plain
  text ("accept Nyx", "decline nyxie: full on healers") (§3a).
- Runs: `/raid open|sheet|health|fill|lock|set|cancel|list|loot|end` (officers) and `/raid out` (anyone).
  They need no arguments: with one live run they act on it; with several they ask which. `/raid set` takes the member
  and answer, then asks which character. `/raid cancel` asks for an optional reason in a box.
- Policy: `/gm policy show|edit|reload`, `/gm rule loot|comp` (the bot shows the end of the current document; press
  **Write the rule** and write it in a paragraph box) — prose is compiled by Claude into rules and
  an officer confirms the reading before it goes live. Plain text does the same: "add a loot rule: tanks first on
  shields", "drop the loot rule about attendance", "recompile the comp policy", or a whole new text; after **Apply**
  the bot shows its reading with **Confirm this reading** / **Discard edit** (Discard puts the previous text back).
  If the reading can't be made right then (Claude unavailable), the change is refused and the text is put back as it
  was — nothing is left half-done. The persona has no reading: it is live as written. A document has one edit
  waiting at a time, whoever started it and wherever (Discord, the site, plain text, MCP): Discard goes back to the
  text before that edit. A reading only goes live against the text it read — if anyone changed the document after
  it was compiled, Confirm (and a stale Discard) refuses and asks for a fresh compile.
- Plain-text config: `/gm change <text>`, or @mention the bot in the ops or analytics channel — the bot
  shows a "current → new" diff with Apply/Cancel. It never guesses: ambiguous requests come back as a
  question, and a plain question ("who is rostered tonight?") gets the same answer `/ask` would give. Whoever presses
  Apply needs the rights for the change (§8c); a slow change (a lock, fill DMs, a compile, a proposal) is acknowledged
  at once — the buttons go, the diff reads *Applying…* and turns into the result when it is done, and a second press
  (or Cancel) by anyone is refused, so nothing applies twice. A board change (a move, the groups, auto-fill, using a
  proposal) is refused with *"the board changed since you looked"* when someone else changed that board between the
  diff and your Apply — nothing is overwritten, even when an unrelated step of the request (a DM setting) comes first;
  the request's own earlier steps (a pin, a move) never count as a change, and once one board step is refused the request's later board steps on that run are refused too. Absences
  entered this way are announced like `/me absent add`; channel changes post (or drop) their card like the Config page
  does. Several raid or schedule settings in a row are saved as one change, so "open 100 hours before and lock 60
  before" can't fail half way.
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
- The same list, with Approve / Dismiss, is on the site's **Agents** page, and plain text does it too: "approve
  proposal 260927-a1b2", "dismiss 260927-a1b2: handled by hand" (officers by default; the note is kept on the card).
- **Several servers.** Each server's news is reviewed on its own (its own news channel and keywords, its own cards and
  Agents page — you see your server's review runs only). A **game data** change is shared by every server on that game
  version: the review proposes it once, approving it needs the owner of every server on that version (in practice one
  person) — on the card, on the Agents page and in plain text alike; anyone else's Approve is refused with the names
  of the other servers that use it (Dismiss stays with your officers). When it lands or is rolled back every one of
  those servers gets an ops line. The restart takes every server's bot down for those two minutes. The Agents page
  and `/ask` show a job's current step and last result only for your own server's runs.

## 8. Owner: setup

**Who the owner is.** Each guild has one **owner**: the person with owner-only rights there (channels, raid rules and
schedules, auras, timezone, recruitment, plain-text permissions, the test bench's setup, ownership transfer) and the
person MCP acts as. When a server is set up (`/gm config owner` in it), the owner defaults to **the Discord server's
owner**; the setup screen can pick someone else, and `/gm config owner user:` transfers it later. (Discord doesn't tell
a bot who invited it, so "whoever added the bot" can't be read; the server's owner is the reliable stand-in.)

**Superusers.** `OIBOT_SUPERUSERS` in `.env` (Discord user ids, comma-separated; restart the bot) lists the host's
superusers: owner rights in **every** guild the bot serves, whether or not they are in that server or own it. They can
set up any server the bot is in, open every guild on the site, and use every owner-only command and plain-text change.
The setup screen still makes the server's own owner the guild owner, so a superuser can configure someone else's server
without taking it over. Keep the list to the person who runs the bot.

**Setting up a server.** Inviting the bot never sets anything up. In the new server, the Discord server owner (or
someone with Manage Server) runs `/gm config owner`: a short screen shows the server's name, the guild key the bot
picked from it (it never changes; the site and MCP use it), the game version (WoW: Forever unless you pick another) and
the owner (you, or pick someone). Nothing is saved until **Confirm**; then the server is a guild of its own — empty:
its own members, raids, schedules, policy and settings — and you continue with the channels below. Every other
command used in a server that isn't set up answers "This server isn't set up for oibot_GM yet". The bot can look
after **several** servers, each its own guild with its own data; when a second one is claimed the owners of the others
get a DM. Keep **Public Bot** switched off in the Discord Developer Portal, so only you can invite the bot (any server
the bot is in could otherwise claim itself, show on the homepage and draw on the shared AI budget). The invite link:
`uv run python scripts/move_guild.py --invite-link` (`--guild <server id>` preselects the server). Commands are global (every server and DMs); the
development mode `OIBOT_COMMAND_SCOPE=guild` puts them only in the test server and the configured servers, so a new
server can't be claimed in that mode. A command used in the test server acts on the test server's own guild only — if
the test server isn't the guild's server, it answers "isn't set up". Cards the bot posts in a channel (an
application's Accept / Decline, the absences card, an agent proposal's Approve / Dismiss) answer only in the server they
were posted in. A sheet is only ever re-posted in a channel of its own server (the site, MCP and plain text refuse
another server's channel). The companions shown by `/gm status` and on the site's **Ops** page are the ones feeding
this guild. **Loot companion with two servers:** each server's companion needs that server's own token —
`OIBOT_FEED_TOKEN_<KEY>` in the bot's `.env` (the guild key in capitals, `-` as `_`; e.g. `OIBOT_FEED_TOKEN_OI`) — and
feeds that server's raid only, whatever `--guild` says. The shared `OIBOT_FEED_TOKEN` feeds no real raid once two
servers are looked after (the demo `/mock` flow only); to keep an existing companion as it is, give its server's token
the shared value. Never hand one server's raid leader another server's token.

**Moving a guild to another server** (rather than claiming the new server as a guild of its own):
`scripts/move_guild.py --to <new server id> [--key <guild>]` with the bot stopped (which stops every server it looks
after), `--dry-run` first. Members, characters, schedules, policy and history carry over; channels, card messages,
roles, the invite and per-channel plain-text settings are cleared to set again in the new server. Once the
several-servers update is live, don't roll the bot's code back to before it: buttons posted since carry their server in
a form older code doesn't read.

`/gm config owner` in a configured server transfers ownership (first claim needs the Discord server owner or Manage Server). Handing the guild to a new owner
is only ever this slash command, by design — not the site, plain text or MCP: an MCP token acts as the owner, so a
transfer there would give the guild away and silently change whose rights the token carries. Then
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

**Recruitment.** `/gm config recruitment` opens one screen with the whole draft: applications **open or closed**, the
**note** shown on the Apply screen and the registration card (one line, up to 200 characters; empty clears it), the
guild's **questions** (up to 15, each a question of at most 45 characters, an optional hint of at most 100 shown under
it, and required or optional — *Add a question…*, or pick one to *Edit…*, *Move up*, *Move down* or *Remove*), the
**member roles** (Discord's role picker; anyone holding one registers without applying, e.g. members who joined
before the bot) and the public **invite link** (https://discord.gg/…, what the website's Join button opens). A
question that breaks a rule is refused the moment you add it, with your text kept. Nothing is saved until **Save**,
which writes every change at once (a bad invite link is refused there, draft kept) and updates the registration
card. In plain text (`recruit`): "close applications", "set the recruitment note to Looking for healers", "add a
required question: Why do you want to join us?", "reword question 2 to Warcraft Logs link, make it required" (same
question, same place, so answers already given still line up), "move the logs question to the top", "remove question
2", "@Veterans can register without applying" (member roles), "set the invite link to https://discord.gg/abc123" or
"create an invite to #welcome" (the bot makes a permanent invite there and saves it; the registration card follows).
Recruitment is in the **Setup** group of §8c (owner by default).

**Recruitment** (site: the Config page's **Recruitment** section; the owner presses **Edit recruitment**,
officers read). Applications happen in Discord; this section sets what they ask and who needs one:
- **Applications open** (a switch; closed = `/apply` and the Apply button explain that instead of the form) and
  the **recruitment note**, one line on the Apply screen (up to 200 characters, e.g. "looking for healers").
- **Discord invite**, the homepage's Join button. Paste one (`https://discord.gg/…`) or pick a channel and press
  **Create invite**: the bot makes a permanent, unlimited-use invite there and fills the field (it needs the
  *Create Invite* permission in that channel and says so if it lacks it). Clear the field to hide the button.
- **Member roles**: people with these Discord roles register without applying (existing members who haven't
  registered yet); officers always can.
- **Application questions**: an ordered list, up to 15 (Discord's forms ask five per page). Each has a short
  label (Discord allows 45 characters; a counter shows how many are used), an optional hint (100) and a
  *required* switch; ↑/↓ reorder, the bin removes, **Add question** adds. An application keeps the questions
  as they were asked, so later edits don't reword old answers.

Nothing is written until **Save recruitment**; if any part is refused (a label over 45 characters, a link
that isn't a Discord invite, the same question twice), the reason shows in the section and nothing changes.
The same settings are in `/gm config recruitment` and in the MCP tools (§8b).

**News.** `/gm config news-channel` (or the Config page's *News* picker, or "set the news channel to #news" in
plain text) is where the Wowhead webhook posts; *News keywords* on the Config page (or `news_keywords` in plain
text) adds words that make a post relevant besides the game version's names and raid names. The review and apply
jobs run on the host as their own launchd agents (`scripts/agents/README.md`; `sh scripts/agents/install.sh`), and the
site's **Agents** page shows whether they are installed and running, what the current run is doing step by step,
past runs, the news kept and the proposals; **Run review now** (owner) starts the review at once — so does telling
the bot "run the news review now" (or "apply the approved proposals now"), which only the owner can ever do, whatever
the plain-text permissions say. A menu-bar item on
the Mac mini (GM ✓ / ⟳ / ⚠ / ✕) shows the same at a glance, even when the bot is down.

**Channels in plain text.** "post raid sheets in #signups", "use #away for absences" — the same as the Config page's
pickers (the bot checks it can see the channel and posts that channel's card); "stop posting absences anywhere" or
"clear the news channel" unsets one (its card is forgotten). "Set the timezone to America/New_York" shows, before
you Apply, each raid's next run on the new clock and on today's.

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
re-opens the same sheets. The exceptions are handing the guild to a new owner (`/gm config owner` only, above) and
the loot session (`/raid loot`). Who may do what, and in which channel, is the owner's plain-text policy (§8c): by
default officer actions need an officer and raids, auras and guild setup need the owner. A run is
named by its key (`bd-1209-1930`), or by raid and day ("tonight's Barrow Deeps", "the Hyjal run") when only one of
that raid's runs is live — with two live, the bot asks which. A member is named by display name, @mention or one
of their characters. The bot reads each live run's board as it stands and each raid's next unopened runs, so
"groups for tonight" and "open Saturday's alt run" start from the real thing.

- *Runs:* "open Barrow Deeps for Thursday 7:30 PM" (one of the raid's upcoming runs opens as that schedule's run;
  "open the 8 PM Barrow Deeps" picks the next one at that time; any other time is a one-off; "open the next Barrow
  Deeps") posts the sheet now; "lock tonight's Barrow Deeps" builds the roster and DMs everyone rostered; "cancel
  bd-1209-1930, server down"; "put Xanthe on the bench for bd-1209-1930" / "set Marrow to join as Marrowlite" /
  "Kestrel can't make it" (after lock: join seats and asks to confirm, no thanks frees the seat and the fill engine
  looks for cover; with a reason — "Kestrel can't make it, work" — it is a callout like `/raid out`, the reason
  kept for the officers; your own "I can't make it" is always a callout, reason or not); "Xanthe confirmed for tonight" / "Kestrel said she can't make it after all" (on a locked
  run: answers their Confirm / Can't make it ask for them, or the fill ask the bot sent them, when they told you out
  of band — exactly as if they had pressed the button, so a no frees the seat and asks the bench); "who would fill
  ask next for tonight's run" (preview, nothing sent) and "send the fill asks" (the diff lists every ask Apply sends,
  role swaps marked SWAP and the backfill named; Apply sends exactly that batch, and nothing if it changed meanwhile);
  before lock "suggest fixes for Tuesday's Barrow Deeps" (the suggestions, nothing sent) and "send them" (the diff
  shows exactly who will be asked; if the suggestions changed before you press Apply, nothing is sent); "split tonight's run
  first-roster-first" (balanced / first / rotation); "auto-fill the board for bd-…" (before lock).
- *The board:* "move Ash to group 3" / "put Ash in 2.1" (roster 2, group 1) / "bench Ash" / "swap Ash and Dusk" —
  one person, like a drag (after lock: in from the bank = asked to confirm, out to the bank = seat freed); "groups for
  bd-…: Ash, Bright, Cinder | Dusk, Ember" sets the whole board — the layout the lock will use, or after lock the roster
  itself (names added are asked to confirm, names dropped are freed). "Propose a roster for tonight" (or "a rotation
  split") shows the solver's layout — groups, synergy, who is left out, why there can't be more runs, whether leftovers
  could make another — and saves nothing; "another one" shows a different one, "use it" puts the last one on the board
  and remembers its strategy (the bot remembers what it showed you until it restarts). "Post the sheet here" re-posts
  a live sheet in the channel you ask in (not in DMs; its buttons and updates follow the new copy) — like `/raid
  sheet`, anyone registered may ask, in any channel where plain text acts on their own record. Pins stay as
  before ("pin Ash to the roster", "keep Dusk on the bench"); a seat on a run is someone's answer ("put Ash in for
  tonight": before lock they join, after lock they are rostered and asked to confirm) or a board move.
- *Members:* "add Jon's alt Jonny, Mage Frost" (value is `Class Spec [Offspec]`; "… as his main" makes it the main in
  one step), "plan Jon's main as a Holy Priest" (a planned character with no name; replaces an earlier plan, keeping
  rank and roster placement), "delete Jon's alt Jonny" (gone outright; the diff names any run it is rostered on, by raid and
  start time — the delete does not free that seat; retire keeps history), "Jonny can also tank" (extra roles; "none" clears them), "change Jonny to Fire",
  "offspec for Jonny: none", "name Jon's planned Shaman Stormcall" (planned characters only — a named
  character is never renamed: retire it and add the new one; its class never changes either), "rank Jonny
  raider", "make Jonny Jon's main", "retire Jon's alt Jonny"; "confirm Jonny", or "confirm everyone pending" (every
  unconfirmed character at once — the diff lists them; officers' requests are read against that list; a character
really called All or Pending is confirmed alone); "Mira is away
  Nov 1–3" (the diff lists the live sheets those days touch — No thanks, or a rostered seat freed — before anything is
  saved); "Marrow is back, clear her absence" (by start
  date when she has several — the bot lists them as days, e.g. "Fri 01 Mar → Sun 03 Mar, Mon 10 Jun"); "turn DMs off for Marrow" (off: never asked to fill, confirmations wait on the site).
- *Raids (owner):* "Barrow Deeps runs Tuesdays and Thursdays at 7:30 PM" (the diff shows the next runs it makes),
  "short-notice cadence for Barrow Deeps" (Standard, Short notice or Same week — the same presets as `/gm config
  raid`: when the sheet opens, the nudge, the lock and the confirmation deadline together), "lock Barrow Deeps 12 hours
  before", schedules (`schedule_set`, above), "put Barrow Deeps back to the defaults" (the diff lists the overrides it
  drops in words: run times, first open, hours). Settings that only make sense together are saved as one change.
- *Recruitment (owner):* "close applications", "open applications", "set the recruitment note to Looking for
  healers", "add a required question: Why us? — hint: a line or two is plenty", "reword question 2 …", "move the
  logs question up", "remove the question Why us?" (or by number), "@Veterans can register without applying",
  "set the invite link to https://discord.gg/abc123", "create an invite to #welcome" (op `recruit`).
- *Channels (owner):* "post raid sheets in #signups", "stop posting absences anywhere".
- *Plain-text permissions (owner only, whatever the policy says):* "only officers and @Raid Lead can change runs by
  chat", "make #raid-chat answers only", "the bot ignores DMs" — §8c.
- *Applications (officers):* "accept Nyx", "decline nyxie: we're full on healers" — the applicant is named by
  display name, @mention or character; the decision goes through the same code as the card (DM, card, ops line).
- *Policy (officers):* "add a loot rule: …", "drop the comp rule about …", "replace the loot policy with: …",
  "recompile the loot policy" — then the reading with Confirm / Discard.
- *News (officers):* "approve proposal 260927-a1b2", "dismiss 260927-a1b2: already handled". *Jobs (owner only,
  never opened to anyone else):* "run the news review now", "apply the approved proposals now".
- *Test bench:* "seed 20 test members" (or "seed all of them"), "open a quick test Barrow Deeps run" (the tempos of
  `/gm test run`: Quick starts in 20 min, nudge 16, lock 12, confirm 8 min before; Standard 40/32/25/15, the default;
  Slow 90/70/55/35 — add "and DM them" to DM every puppet when it opens, which reaches you), "test answer join 8 bench
  2 out 1" (a random mix of puppets who haven't answered; name the run when several are live), "clear the test bench".
- Several things in one sentence become several operations, applied in order ("lock tonight's run and turn
  DMs off for Marrow"). Loot items, tiers, wishlists and standing availability are not settable anywhere; the
  loot session (`/raid loot`) and handing the guild to a new owner are slash commands only, by design.

## 8c. Plain-text permissions (owner sets, officers read)

Who may change what through plain text, and where, is one policy on the **Ops** page of the site (card
*Plain-text permissions*; the owner presses **Edit permissions**, changes the table and presses one **Save**;
officers see it read-only; MCP has `plain_permissions` / `set_plain_permissions`; the owner can also say it: "only
officers can change runs by chat", "make #raid-chat answers only", "put #general back to the default"). It covers every
plain-text path: @mentions, DMs, `/gm change`, and MCP `plain_change` (which counts as a place where plain text acts;
the site has no plain-text box). When the owner's own change would stop plain text acting in the channel it is made
in, or make the bot ignore @mentions there, the diff says so before Apply.
The owner can always do everything, anywhere the bot listens — an *Answers only* channel included. In an *Ignored*
channel the bot doesn't read @mentions at all, the owner's included; there the owner uses `/gm change`.

**What** — seven capability groups; every plain-text operation belongs to exactly one:

| Group | Default | Someone could type |
|---|---|---|
| Own record — your absences, DMs, characters (add or plan, spec, offspec, name, main, retire, delete, extra roles) and your own answer or confirmation on a run; re-posting a live sheet here (it touches nobody's record, like `/raid sheet`) | registered members | "I'll be away Nov 1–3", "make Xanthe my main", "I can't make tonight", "post tonight's sheet here" |
| Other people's records — their absences, characters, DMs, ranks (yours too), confirming characters, deciding applications | officers | "rank Jonny raider", "Mira is away next week", "accept Nyx" |
| Runs — open, answer for someone, lock, cancel, fill, split, auto-fill, the board, `confirm_for` | officers | "lock tonight's Barrow Deeps", "Xanthe confirmed for tonight" |
| Comp & policy — comp targets and group layout, pins, the loot / comp / persona policy (add or remove a rule, replace, recompile), deciding news proposals | officers | "we want 3–4 healers in Barrow Deeps", "pin Jonny in tonight" |
| Raids & auras — raid schedules and settings, buff and family facts | the owner | "Barrow Deeps runs Tue and Thu 7:30 pm" |
| Guild setup — channels, officer roles, timezone, who may ask, the about text, recruitment (applications open/closed, note, questions, invite), starting the local jobs | the owner | "post raid sheets in #signups" |
| Test bench | officers | "seed 20 puppets", "clear the test bench" |

**Who** — per group, any mix of *everyone*, *registered members* (an active character), *officers*, *owner only*,
and specific Discord roles (picked from the server's roles, stored by id, so renaming a role keeps it). An empty
list means the owner only. Anyone allowed a group for other people may also do it for themselves. Two things stay
the owner's whatever this table says: starting the local jobs and changing this policy itself — an officer given
*Guild setup* still can't widen their own rights. Your own answer follows the sheet's rules: once a run is locked,
"I'm in" or "bench me" from a member is refused in the sheet's words (answer your confirmation DM, or press *Can't
make it*) — after lock a seat comes from the fill engine or an officer; "I can't make it" still works.

**Where** — each channel is one of: **Acts** (anything the person's groups allow), **Own record only** (a member's
own record; other requests are refused with where to go instead), **Answers only** (questions, no changes) or
**Ignored** (the bot doesn't answer @mentions there). DMs have their own entry, and there is a default for every
channel not listed. Defaults: the ops and analytics channels act; DMs and every other channel are *own record
only* — so a raider can tell the bot about their absence anywhere, while run and roster changes stay in the
officer channels.

**How it decides.** The bot reads a message's intent first: in a channel that acts, the sentence is parsed as a
change straight away (a question is handed to the same answer as `/ask`, with its full snapshot). Elsewhere the bot answers it like
`/ask`, and only when the message asks it to *do* something — and you could act there — does it turn it into a
change for you to Apply. Who an operation is about is decided by the bot's code, not the model: leaving the
person out ("I'll be away") means *you*; naming yourself by display name, @mention or one of your characters
means you; a name that could also be someone else (two people with that name, or your name is another member's
character) counts as someone else; a character you name must be one of yours. Refusals say who may and where:
*"Only officers can change someone else's absence."*, *"Plain text can't change runs in #general — use
#officer-ops."* Every applied plain-text change is logged in the ops channel with who asked and where.
What the bot reads your sentence against depends on who you are, decided before it reads it: an officer's request is
matched against the open applications, the news proposals waiting, the unconfirmed characters and the member-role
names too; a member's never is, so a question the bot asks a member back can't name an applicant. Names people chose
(members, characters, applicants, the boards, news titles) reach it only as quoted data, and every time it sees comes
with the same time in 12-hour words beside it, so a question it asks back reads "Tue 6 Oct 7:30 PM", never 19:30.
Someone who has left the Discord server counts as unregistered for plain text, even with a record still on file:
a DM from them gets the static guide, never a change (an Apply left on an old diff, and `/gm change`, refuse them too).
Only Discord saying they are gone counts: a Discord outage never locks a member out.

## 8a. Rehearsing with the test bench (officers)

`/gm test seed` asks how many puppet members to create (5, 10, 20 or the whole bench; fake Discord ids, a
realistic class mix and ranks, mains confirmed). `/gm test run` is one screen: the raid (when there is more than
one), a tempo (Quick, Standard or Slow) and whether to DM everyone when it opens, then **Open the test run**. It
opens a real sheet for that raid in the signup channel (with no signup channel set: in the channel you asked in —
the same for plain text's "open a quick test run"; the site's test bench needs the signup channel) with minute-level
cadence, so the unchanged scheduler goes through nudge →
lock → confirmations → expiry → fill → close within the hour. `/gm test answer` offers **Random mix** (how many
Join, Bench and No thanks) or **One member** (pick a puppet, then Join, Bench or No thanks), and stays open so you
can answer again; after lock a puppet's *No thanks* is a
callout. `/gm test clear` cancels the test runs and deletes every puppet and their placements.

**Any mix, on the Members page.** Officers get a **Test bench** card under the members table: the bench as spec icons
with a count each (− / + to change it), the role totals, and a class → spec picker with **Add** for a spec it doesn't
hold yet. **Save test members** adds or removes the difference (removals take the newest puppets of that spec; one
rostered on a locked run is refused — cancel that run or clear the bench). **Delete all test members** is `/gm test
clear`. **Try a comp** (pick the raid, a 20-player one by default) opens a *comp sandbox*: a test run two weeks out
with every puppet joined, never posted in Discord, and takes you to its board, where **Propose roster** builds the best
comp from that mix (Leftovers and "what a second run would need" included). Change the mix and try again; the old
sandbox goes with the next clear. The members table hides test members unless you switch them on. Plain text does the
same ("add 3 Warrior Protection, 2 Priest Holy to the test bench", "remove 2 Warrior Protection", "comp sandbox for
Barrow Deeps", "seed all test members", "open a slow test Barrow Deeps run and DM them", "test answer join 8 bench 2
out 1"), and so does MCP (`test_bench` compose / comp). The bench holds at most 80.

What is safe: a test run's pool is only the puppets and you (the tester) — real members are never nudged,
rostered or asked to fill it, and the test sheet refuses presses from real members. The other way round too: a real
run (ad hoc, or a schedule with no regulars yet, where every main is expected) never expects, suggests, asks or marks
away a puppet; setting a real member's
answer on a test run is refused on every path too (the site, plain text, MCP). Every DM the bot would
send a puppet (open, nudge, Confirm / Can't make it, fill asks) arrives in **your own DMs** with the puppet's
name on it — the DMs of the officer who opened *that* test run, so two officers rehearsing at once each get their
own — and you can press its buttons on their behalf. What real members do see: the sheet itself in the
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

**Several servers.** Every tool takes an optional `guild`: the server's key or its name (any case). Leave it out
and the tool uses `OIBOT_MCP_GUILD` from `.env` (or `uv run oibot-mcp --guild <key>`), else the only server; when
the bot serves several and no default is set, the tool answers with the list instead of guessing. `list_guilds`
lists them (key, name, Discord ids, owner, game version, which one is the default) and always asks the bot afresh, so
a server claimed while the session runs can be named at once. Each call acts as the owner of the server it is for;
`guild_overview` says which server answered. The news review and apply jobs lock their sessions to one server
(`OIBOT_MCP_GUILD_LOCK=1` beside `OIBOT_MCP_GUILD`): a tool call naming any other server is refused, so nothing a
news post says can make the review read or write another server.

| Tool | Does |
|---|---|
| `list_guilds` | every server the bot serves, and which one a tool uses when `guild` is left out |
| `guild_overview` | timezone, owner, officer and member roles, news keywords, channels (a channel the bot can no longer see is marked missing), the policy documents (summary and text), raids' cadence, live runs, test bench |
| `list_runs` · `get_run` | live sheets per raid with upcoming slots · one run in full (recurring or ad hoc and its schedule; signups; `not_answered` = the regulars who haven't answered, and each regular's answer; board and its `rev`, confirmations, fill asks, callouts with their notes, absences that day, log; an open sheet's health: headcount, roles with who could cover, buffs nobody brings) |
| `list_raids` · `get_raid` | effective raid settings and overrides; each schedule's class (recurring / ad hoc), regulars count, where each inherited value comes from, seat weights |
| `set_regular` | add or remove a regular of a recurring schedule (member, character, schedule; the Rosters page's route) |
| `list_members` · `get_member` | everyone's characters, absences, asks (`filter` narrows) · one record |
| `list_absences` · `list_auras` · `ops_log` | upcoming absences · the buff matrix · status, the ops feed, the registry change lines since the bot started, and the loot precedents (up to 50) and ledger (up to 100), `member` narrowing both to one person |
| `open_run` | open a sheet now (next slot, or a date and time — an upcoming run's exact time opens that run, any other time is a one-off ad hoc run; `schedule` = that schedule's next run, or a pickup template at `when`) |
| `schedule_set` · `schedule_remove` | a raid schedule's settings, one or several as `fields` in one write (creates it with its kind, slots or lockout days) · drop a schedule (schedules come back in `list_raids` / `get_raid`) |
| `set_answer` · `pin` · `confirm_for` · `fill_answer` | Join / Bench / No thanks for someone (character swap; No thanks is always a callout like `/raid out`, a `note` is its reason, for officers only) · pin in/out for the lock · answer a Confirm ask for them · answer a fill ask (seat or swap) for them |
| `propose_split` · `set_strategy` · `autofill` · `set_layout` | preview a split (nothing saved; says why there can't be more runs and who is left over) · remember the strategy · solver shapes the board · set the groups (pass `rev` from `get_run`: a board someone changed since is refused, not overwritten) |
| `lock_run` · `fill_seats` · `cancel_run` · `repost_sheet` | lock now · preview who would be asked (before lock the suggestions, after lock the seats to fill — `plan.seats` with alternates and a `why`, `plan.lines`, `plan.batch_lines` = exactly what a send DMs, `plan.rev`), then `send=true` with the preview's `rev` sends exactly that batch (`picks` {seat: option} for an alternate; a changed plan is refused; without a `rev` a send is refused before lock, and after lock whenever the batch holds a role swap) · cancel with a reason (rostered members are not told automatically: say so in the channel) · post a fresh copy of the sheet (the signup channel, or a channel you name) |
| `raid_set` · `raid_reset` | raid settings — one, or several as `fields` in one write (slots, hours, the switches nudge/autofill/open_dm/confirm, weights, comp bounds, first open…) · back to defaults |
| `aura_set` · `family_set` · `aura_reset` | what the guild learns about buffs and stacking families |
| `add_absence` · `clear_absence` | away dates for someone (sheets answered / re-opened); `preview=true` writes nothing and names the live sheets it would touch and how · someone is back early |
| `save_characters` · `set_member` · `set_dm` | character table rows (a row's `flex` list sets its extra roles) · rank / confirm / main · DMs on or off |
| `set_channel` · `set_config` | a bot channel by name · timezone, ask audience, about, officer roles |
| `recruitment` · `set_recruitment` · `create_invite` | applications open or closed, the note, the invite, member roles (by name or id) and the questions · change any of them in one write (questions = the whole ordered list; the registration card follows) · a permanent invite to a channel you name, saved as the invite link with `save=true` |
| `list_applications` · `decide_application` | open applications and the last 20 decided (character, schedules, answers, when) · accept or decline one by the applicant's name, character or id, with a note to them |
| `test_bench` | seed (`count=0` = the whole built-in roster; more than it is capped) / run / answer / clear / compose / comp, as `/gm test` |
| `policy_get` · `policy_edit` · `policy_compile` · `policy_confirm` | a policy document (loot, comp, persona) in full · replace its text (a draft; the persona is live as written) · the bot's reading of it, nothing live yet · make that reading live, or `confirm=false` to discard the edit and restore the text — what `/gm policy edit` does |
| `plain_change` | text → the config ops it means, with current → new, and a `token`; `apply=true` with that token (within ten minutes, once) applies exactly those ops — never a new reading of the text — and refuses a board someone changed since the preview; like Discord's Apply, an op that fails is listed in `refused` with why and the rest still apply (`applied` is what landed, `error` the first failure) (everything plain text does, §8 — except re-posting a sheet "here": use `repost_sheet`) |
| `plain_permissions` · `set_plain_permissions` | who may act through plain text and where (groups with who, every channel's mode, DMs, the default, the server's roles) · change it (owner: groups, per-channel modes, DMs, default) |
| `raid_readiness` | each raid's pool readiness (§6): headcount, roles with who could cover, buffs nobody brings, the desired comp with its reasons, short / over cap, the pool plan — one raid or all |
| `list_news` · `get_profile` | the news kept since a time · the effective profile (buffs, families, raids, comp rules) with guild overrides marked |
| `post_proposal` · `list_proposals` · `get_proposal` · `resolve_proposal` | the news review's proposals (§7a): post one, list by state, one in full, move its state |
| `job_status` · `get_job_run` · `run_job` | the review and apply jobs: state, last and next run, launchd, recent runs · one run's steps · start a job now (refused when it is running or not installed) |

Members can be named by display name, character name or Discord id; runs by key, roster key or the raid's
name when it has one live run. An ambiguous name comes back with the candidates instead of a guess.

**Safety.** The server acts as the **owner of the chosen server's guild** (owner-only settings included) and every
change is logged as that owner via `mcp`. Keep `OIBOT_MCP_TOKEN` private — it is as sensitive as the bot token — and never
put it in `.mcp.json` or a commit. **Local only** (on by default): the bot accepts the token only from a call made on
the machine it runs on, which is where the MCP server runs. The same token arriving from anywhere else, through the
public site's tunnel included, is refused as if it were wrong, and the bot logs a warning (never the token), because
it means the token has leaked: rotate it. `OIBOT_MCP_LOCAL_ONLY=0` in `.env` (then restart the bot) lets it work
remotely too; leave it on unless you run the MCP server on another machine. Browser logins are unaffected. Ask for a preview first (`propose_split`, `fill_seats` without send,
`plain_change` without apply) and read the returned line back: it says what actually happened. There is no tool
for handing the guild to a new owner, by design: the token *is* the owner, so a transfer through it would give the
guild away and change whose rights the token carries — that stays `/gm config owner` in Discord.

## 9. Loot (when loot tables exist)

The council scores each drop deterministically — tier for the spec × slot weight × attendance × rank ×
wishlist, discounted by recent loot and a same-slot repeat — and Claude judges the top candidates against
the written loot policy, explains, and flags close calls. Officers confirm, or override with a reason; the
reason becomes a precedent the bot cites next time. In-game awards from the companion app are matched
to the proposal automatically (when the bot looks after several servers, each companion uses its own server's token,
`OIBOT_FEED_TOKEN_<KEY>`, see §8; with the shared token its drops go nowhere and it is told so). WoW: Forever loot tables are not published yet, so this part is waiting
for data discovered in game.

## 10. Data, privacy, cost

Guild state lives in a private git repository (one commit per change, so every edit has an author and a
time). An absence reason is seen by you and the officers, nobody else. The bot DMs only for sheets, nudges, confirmations,
released seats and fill asks; switch DMs off on the **Me** page if you'd rather not get them — with DMs off
you are never asked to fill, and a confirmation ask is shown on the Me page instead and counts as pending
until you answer it there. **My absences** on the absences card lists yours with a **Clear** button each; clearing
an absence puts you back to unanswered on any open sheet it had pre-filled (a seat freed on a locked run is
not handed back automatically — ask an officer). Claude is used sparingly — never for registry, signup or solver work — and spend is
capped by a monthly budget the owner sets (`OIBOT_BUDGET_USD`, per calendar month, kept across restarts); questions and
plain-text requests may use at most half of it each, so they can't spend what loot night needs. `/gm status` shows
this month's spend. The budget is one for the whole bot: if it ever looks after several servers, they share it.

## 10a. Asking the bot questions

**Two servers?** When the bot looks after more than one Discord server and you are in more than one of them, a DM to
the bot — a question, a request, a slash command or a wizard used in DMs, `/help`, `/ask`, applying — first asks
**which server it is for** (one button per server). It asks every time; it never assumes. Pick one and the bot carries
on with what you asked, for that server; nobody picked in two minutes and it says so — ask again any time. If you are
in only one of them, it is that one, no question (a server you just joined or left counts within seconds). A server
whose owner turned DMs off is never offered; if every server has, a plain DM gets no answer. Buttons the bot sent you (Confirm / Can't make it, a seat offer, Join
on a sheet, Apply to join) already know their server and never ask. With several servers, DMs and cards start with the
server's name.

`/ask`, a DM, or an @mention outside the officer channels gets a free-form answer from this manual and
your own record — or, when you're asking the bot to *do* something about your own record, the change itself to
Apply (§3, §8c). Who may do that is set by the owner (`ask_audience`: officers, confirmed members,
registered members, or everyone; default registered). Anyone outside that audience gets the **static
guide** instead — a menu with *About the guild*, *Raid schedule*, *How to register*, *How signups work*,
*How to apply*, *Who to contact* — built from the guild's settings, no AI involved.

What `/ask` knows comes from one bounded snapshot the bot builds in code when you ask (it summarises, it never
dumps; long lists are cut short). Always in it: the guild's channels (a channel the bot can no longer see is marked
*not found*), officer roles, owner and ask audience; the *about* text, the invite link and the news keywords;
recruitment (applications open or closed, the note, the guild's questions with their hints); every raid's schedules (in words, with
their own settings and next runs — *Raid schedule* in the static guide lists each schedule's next three runs too),
cadence (slots, when sheets open, nudge, lock and confirmation times, `fill_ask_hours`, `autofill`, `open_dm`, split
policy, weights, comp bounds, comp targets and group layout, which settings are overridden), the raid's notes and the
lockout window it is in now; the guild's aura and family overrides; the test bench; and every live run as the Raids
page has it — state, nudge / lock / confirmation-expiry times, split strategy, the schedule it came from and how many
rosters it expects, who joined (character, spec and class) and how many per role, bench, no thanks, who hasn't
answered, pins, the board before lock (per roster, with who is still in the bank and the same aura line as after
lock), what the run is short and who could cover it (a member gets the counts — "+1 offspec" — not the names),
auras nobody brings, people In on another run that evening
(before and after lock), the rostered groups after lock (each seat's spec and role) with confirmed / waiting /
declined, and per roster its auras: every raid-wide buff with how many bring it, each group's missing auras and
totems, the synergy (the solver's top advisories are for officers only),
the bench after lock, leftovers and whether they could make another run, for officers the fill asks outstanding
(who, why) and recent answers — a member sees only their own open ask (what it asks, until when), never who else was asked —
for officers the bot's suggestions for short tank/healer seats (exactly as the cards word them — the answer may
rephrase them, never invent one), members absent that day, callouts, and the last log lines (more of the log when your question names the run, its
raid or "tonight"; a member's view leaves out the officers' workflow lines — suggestions and fill asks sent, approvals,
the planner's notes). Also the last few finished or cancelled runs per raid (with the cancel reason) — a past run you
name by key or in words ("last Tuesday", "the last Barrow Deeps") in full, who joined and who was rostered — and when
the next sheet opens. And your own record — characters, roles, DMs, upcoming absences with your own reasons,
confirmations and fill asks waiting on you, your answer and seat on each live run — or, if you applied, your
application's status and the note sent to you (never which officer decided).

Officers get more, only when the question is about it (a few keywords decide; "status", "overview" or "what's going
on" brings the main ones): **status** (data repo and push, LLM spend, the loot feed, uptime, the last ops lines, the
last ten registry change lines — the analytics change log's from → to — and the last data commits), **applications** (open ones oldest first with the schedules they can make, the last five
decisions; an applicant's own answers only when you name them), **absences** (the next 30 days, with reasons),
**readiness** (each raid's pool against its desired comp: roles, missing auras, where the comp is short or over cap
with the reason, and the whole desired comp — met targets too — each with its justification), **news** (the last kept
items, every proposal and its state — waiting ones first, then applied, reverted or dismissed with the commit — and the
review jobs with their last three runs), **loot** (the last precedents and ledger rows, or a named member's; "why did we
override …" brings it too), **bank** (the unconfirmed characters first, then every member's characters and rank, who is
an officer), **policy** (each doc,
compiled or not; the text when you name it), **auras** (the full effective matrix — members get this one too) and the
**test bench**. An officer's snapshot always carries one index line with counts (open applications, proposals to
decide (proposed or failed), unconfirmed characters, unanswered confirmations, fill asks, who is away this week) and says which sections
were left out, so the answer can tell you what to ask for. An officer who names a member (display name or character)
or an applicant gets their record too, absence reasons included. Members never get other people's reasons,
applications, ops, precedents, proposals or who holds which privilege.

**Where you ask decides what the answer may carry.** The officer view goes only where only officers read the reply:
a DM, `/ask` and `/gm change` (ephemeral), the ops, applications and roster channels, and any channel no non-officer
role can see (the analytics channel when you made it officer-only — the bot's own role, or the bot let in by name,
doesn't count as a member; a channel where you let one member in by name counts as public unless that member is an
officer the bot can check). An officer who @mentions the bot anywhere else —
#general, a public analytics channel — gets the member view, because everyone there reads the answer, and the reply
says so: ask in the ops channel or by DM for the officer view. The same holds when that message is a change: the
bot's clarifying questions there are written from the member view too. Anyone who asks where others read the reply
also gets their *own* absence reasons left out of it (ask by DM to see them). Text people typed (news titles, applicants'
answers, reasons) is handed to the model as quoted data, never as instructions. The answer only ever *points* at
commands — a change is a separate step you Apply (plain text, §8c).

A newcomer who writes to the bot (a DM or an @mention) while applications are open gets the **Apply to join** button
on the reply; it opens the same application as the registration card, right there. *How to apply* in the static guide
speaks to you: your application waiting, a past decision, or already a member.

## 11. Things the bot cannot do (yet)

Servers: there is no per-server spending cap — the monthly budget is shared by every server the bot looks after — and
removing a server (when the bot leaves it) or renaming a guild's key is a manual job in the data repo.

Loot: add or tier loot items, record wishlists, retire a precedent, or see your own loot history on `/me` — the
loot council only runs on the demo data until Forever's loot tables exist. The precedents and the loot ledger are
read-only: the Ops page's tables, MCP `ops_log` (a member filter included) and `/ask` (officers) show them. Splits: the **Loot-quality**
split philosophy is a greyed placeholder until wishlists exist (Balanced, Raid one first and Rotation
work). Buff facts, comp bounds, cadence and split policy *can* be changed (Auras page, Raids page,
`/gm config raid|aura`, plain text); the buff list itself and the item tables are files in the code
repository. Regulars are a list of who is expected, never a standing answer: there is no standing availability or
standing signup to set and no attendance or loot view on
`/me` yet: each run is answered on its own sheet (or by telling the bot, or an officer for you). If you ask for one
of these, say so plainly.
