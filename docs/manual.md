# oibot_GM — how the bot works (member and officer manual)

This is the bot's own description of itself. It is injected, with the live command list and the guild's
current settings, whenever someone asks the bot a question (`/ask`, a DM, or an @mention). Keep it truthful: if a behaviour changes, change it here in the same commit.

## 1. What the bot is

oibot_GM is the guild's raid administrator. It keeps the character registry, opens signup sheets on a
schedule, checks the roster's health, builds groups, fills freed seats by DM, and runs a loot council whose
recommendations an officer confirms or overrides (items the tables don't know are recorded by name, §9). Code does the computing
(solver, scoring, schedules); Claude only explains, judges against the written policy, and turns
plain-text requests (members' about their own record, officers' about anything the site or a slash command can do,
except handing the guild to a new owner — §8) into typed changes that are shown as a diff and
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
  times; it updates in place on every answer. With more than one character, Join, Bench and No thanks each ask which
  one (a Bench on your alt makes your alt the fill candidate): up to four characters are one button each, your main
  first; five or more are one list with your main already picked, plus a *Join as <main>* button — nothing is left
  out. The reply is your state, not the press: *"You're joined as Kestrel (Holy) for Barrow Deeps Tue 9 Dec 7:30 PM ·
  14/20 so far. The roster is decided at lock."* — on a run with two rosters it says *"Join counts for 1 of 2 rosters —
  which one is decided at lock"*, on an ad hoc run your place in the queue (*"14th of 20 seats — in if nothing
  changes"*). Answering is code only, no model. The sheet's footer counts officers' pins (*Pinned: 2 · Bench-pinned:
  1* — numbers only), current whichever way the pin was set (`/raid set`, the site, plain text or MCP). A **locked** sheet shows the roster(s) group by group,
  a **Not rostered** row (joiners the solver left off), the Bench and No thanks rows, and a single **Can't make
  it** button. A **cancelled** or **finished** sheet says so and has no buttons.
  The sheet lists who joined in columns, a class per column with one character per line (spec icon + name);
  after lock the columns are the groups, with ✅ / ⏳ / ❌ for each person's confirmation.
- **Absences channel** — a pinned card with *I'll be away*; each absence is announced there (no reason shown).
- **Roster channel (officers)** — health cards, roster cards at lock, fill progress (🧩 lines). Each run's card
  has an **updates thread** underneath where every ask, answer, freed seat, confirmation and expiry is logged. The
  **loot council's thread** lives here too (else in the ops channel), §9.
- **Analytics channel (officers)** — one card per raid: how many mains the pool has for its size, role counts
  have/need, buffs nobody brings, and where the desired comp is short or over cap. The cards are edited in place
  (no re-posting), with a change-log line when the registry changes. Officers change comp ideals here in plain
  text by @mentioning the bot. The character bank is the **Members** page; building groups is the **Rosters** board.
- **Ops channel (officers)** — every action the bot takes, one line each. The same lines go to a rotating log file on
  the host (`out/oibot.log`). **Errors** also DM the owner — once per kind of error per 30 minutes: the same error again
  inside that half hour edits the first DM (*"and 4 more like this since 7:12 PM — details in #ops"*, at most once a
  minute) instead of sending another, and the Ops page shows how many were folded in. If the DM couldn't be delivered
  (closed DMs), the next error tries again rather than waiting out the half hour. `/gm config errors-to member: [remove:] [reset:]` (or
  **Change who** on the Config page's Setup card: the owner, these people, or nobody) picks who
  gets them (the owner by default; several people, or nobody — then errors stay in the ops channel); errors only, never
  info or warnings. If the bot can't post in the ops channel three times in a row, that is itself an error alert — once
  per 30 minutes however often it flaps, never while Discord has the server in an outage, and not again when the hourly
  channel check already reported that channel.
- **Applications channel** — one review card per application (§3a) with Accept / Decline… buttons (they keep
  working after a bot restart).
- **Loot channel (the public loot log)** — one line per recorded award (item → character · boss), with an **Add to my
  wishlist** button; never a reason or a score (§9). Not set: the lines go to each run's updates thread.
- **News channel** — a Wowhead webhook posts game news there. The bot reads only the post itself (title and
  summary, never the linked article) and keeps the items about our game version, our raids, or the extra news
  keywords on the Config page. A daily review compares them with what the bot believes about the game; a
  contradiction becomes a **news proposal** card in the ops channel (§7a).

## 2a. The website

**https://gm.earlyandoften.gg** — the homepage (for visitors who aren't logged in) shows the guild's name and
Discord icon, a one-line intro, the owner's *about* paragraph, **Join our Discord** when the owner has set an
invite and **Log in with Discord** for members. Anyone can join the Discord server;
new raiders apply there with `/apply` (there is no application form on the website). With no invite set there is no
Join button and the card says *"Invitations are closed — ask an officer in game."* Someone who logs in
with a Discord account that isn't in the server yet gets a short page saying so, with the Join button (or that same
line) and a log-out link, instead of an error; after joining they log in again. A logged-in member goes straight to **Me**.

**Raid schedule** (the link on the card, `/schedule`): a public page — no login — with each raid's name and size and,
per schedule, when it runs and its **next three runs** in words (*"Fri 6 Nov 8:00 PM"*, guild time; a paused schedule
says so, a pickup template says an officer opens those runs). Nothing about members is on it. With several servers it
lists them; `/schedule?g=<key>` opens one.

**Unlisted guilds.** The owner can switch a guild to *unlisted* (Config › Recruitment and more › **Homepage**, or
"unlist the guild" in plain text): it then has no card on the homepage, no public schedule, and the not-a-member page
doesn't name it. Its members still log in and find it in their own server list as before.

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

With unsaved edits on a page (an edit mode with changes), the server selector asks you to **save or discard your
edits first** — switching reloads the page. If Discord can't confirm right now that you're still in the server, the
site keeps showing what you could see for up to six hours, but an officer's change waits until Discord answers (a
yellow line at the top says so; a change you try meanwhile answers *"Discord can't confirm your officer role right now —
reading works, changes wait."*).

Log in with Discord. Every member gets **Me** and the **Loot log** (every award the council recorded, §9).
Someone who has applied sees **Your application** at the top of Me: waiting, or the decision.
Everything below can also be done there:
your characters (press **Edit**: add a row, first + last name, spec/offspec, extra roles (*Flex*), main/alt, then one **Save changes**; the crown marks your main; each row's **⋮** menu has *Make main*, *Retire* and *Delete* — when the change reaches a sheet or a seat, the confirm says so first), absences (*Add*: **From** a day **for how long**, previewed; **I'm back** clears one),
your sheets and confirmations, **My loot** and your **Wishlist** (§9), and the DM switch on **Me**. **Me is your
inbox** (§4 *Your inbox*): every ask waiting for you — Confirm / Can't make it, a seat or swap question, a pre-lock
"could you come?" — as a card with its two buttons and its deadline, then your **Notices** (what happened that you
should know: not rostered, a seat released, an officer answered for you, a run cancelled…), and **My sheets** with
Join · Bench · No thanks (and *Can't make it…* once a run is locked) — the same answers as the sheet's buttons; officers get, in the left rail, **Rosters** (per raid: sheets open for signup with every answer, pins, the board and *Lock now*; locked runs with confirmations, freed seats and **Fill seats**; upcoming slots; history), **Raids** (slots, cadence, nudge/lock/confirm/fill-ask hours, autofill, open_dm, comp and seat weights per raid; the owner presses **Edit rules**; every officer edits the **Desired comp** table; each schedule has **Pause / Resume** beside Edit), **Readiness** (per raid: the pool against its size, roles with who could cover, buffs nobody brings, the desired comp against what the pool has, the officers' targets with **Edit targets**, and the regulars' coverage with the next four weeks — §6), **Auras** (what the guild knows about each buff; the owner edits), **Members** (every member's main and alts in one table, with an owner/officer/member badge taken from Discord roles; the member column stays in view while the table scrolls sideways; the **rank** select on each main (rank is the member's, §3); a DM switch per member; each character's **⋮** menu — *Make main*, *Confirm*, *Retire*, *Delete*; extra roles (*Flex*); upcoming absences with **I'm back**, and *+ away*; members with no character under **No characters**; the **Absences** table — everyone away, with the reason and who recorded it; **Edit members** lets an officer change any member's characters exactly as on Me, with one save — a member someone else changed meanwhile is refused alone and stays drafted; the **Test bench** card seeds or composes puppet members, opens and answers test runs, follows them in a live feed and opens a comp sandbox, see §8a), **Config** (an **Overview**: the **Setup** checklist, then the whole configuration in words — the owner also sees the host's facts — then **Settings**, **Channels** and **Recruitment and more**; owner edits; officers read; it shows who owns the guild — handing it over is `/gm config owner` in the server, §8), **Loot** (Tonight — the live council; History; Precedents with Retire;
Items — the untiered list and the tier grid; Wishlists — who wants what, and the ThatsMyBIS import; §9), **Ops** (the
bot's recent actions and the plain-text permissions; a badge counts errors folded into an earlier alert), **Applications**
(the open applications oldest first with every answer in full, the earlier verdicts, Accept / Decline with the same
sentences as the Discord card and an optional note, the last 20 decided — who decided, the note, what happened to the
member role), **Policy** (the loot policy, the standing comp instructions and the
persona: what was written beside what the bot applies, a red bar when they differ, the waiting draft with Confirm /
Discard, Edit and Add a rule — §7) and **Agents** (the news review and apply jobs on the host: running or not, what the current run is doing step by step, past runs, the news kept, the proposals with Approve / Dismiss, §7a). Everyone also gets **Help**: the
whole manual, section by section (sections for officers or the owner are marked, not hidden), and the commands you may
run; `/help` in Discord links to it. Only
members of the Discord server can log in; officer pages follow the same rules as the officer commands.
Me, Rosters, Members, Loot, the Loot log, Ops, Agents, Raids, Readiness, Auras and Config update live the moment something changes (a press on a sheet, a drag by another
officer, a change in Discord); a 45-second refresh is only the fallback when the live connection drops. A page in edit mode is never
reloaded under you: the Config page's Settings say *Someone changed the settings since you opened them* with **Reload (keeps your
edits)** — a field you didn't touch takes the new value; a field you edited that someone else changed too is listed
(*yours … · saved now …*) with **Keep mine** / **Take theirs**, and Save waits until you've picked for each.

**Tell the bot** (officers): every officer page has **Tell the bot…** in the left rail, and every live sheet on the Rosters page has
**Tell the bot about this run** (it starts with the run's name). Type a question ("who hasn't answered tonight?") and the answer comes
back from the same snapshot `/ask` uses, the officer view; type a request ("bench Jonny tonight", "Barrow Deeps alt run locks 12 hours
before") and the bot shows the change first — the same current → new lines an @mention shows in Discord, refused lines saying who may
and where — with **Apply N changes** / **Cancel**, then the result line by line. Nothing is saved before Apply; a preview is good for
ten minutes (it says how long is left) and an expired one is previewed again rather than applied unseen. The box keeps its preview while
the page updates around it. There is no box on **Me** (owner decision): members ask with `/ask` or a DM, or press the buttons. With the
AI off the box says *The bot can't read sentences right now — use the controls on this page.* There is no
standing signup: each run is answered on its sheet in Discord, or by telling the bot ("bench me for tonight").

## 3. Members: registering and keeping your record current

- **New here? Apply first.** Anyone can join the Discord server with the public invite; joining the guild is an
  application, made in Discord (§3a). Someone with nothing registered who presses *Register / plan my main* or
  *Add an alt*, or runs `/register`, `/me plan main|alt` or `/me char action:Add an alt`, is sent to the application with one line:
  *"New here? Apply first — the officers take it from there."* Plain text ("add my alt …", "plan my main as …") says
  the same, with the **Apply to join** button on the reply while applications are open. Officers, and anyone holding one of the guild's **member roles** (set by the owner, §8), register
  directly; anyone already registered is never affected and keeps adding alts as below.
- **Register / plan a main**: press *Register / plan my main* in #register (class → spec → optional
  offspec → first and last name; Forever characters have two-word names; leave them blank until the
  character exists) or use `/register` / `/me plan main`. Each opens the same picker (class → spec → offspec →
  names). Nothing is typed but the name, and a refused name reopens the form with your picks kept.
  Pressing the button again replaces a planned main (roster placement and rank carry over). A named
  character waits for an officer to confirm it.
- **Your characters: `/me char`** — one command. Alone it lists your characters and asks what to change; with
  `action:` (Spec · Main · Flex · Retire · Delete · Name a planned character · Add an alt) it goes straight there. You
  pick the character from your own list (spec choices are always that character's class), then one confirm screen
  says what changes — and what it does beyond your record, worked out by the bot: *"Holds a healer seat on Barrow
  Deeps Tue 9 Dec 7:30 PM — the seat is handed back and the bench is asked"*, *"Answered Join on … — the answer now
  reads Shadow"*, *"Regular of Barrow Deeps · Raid nights — comes off it"*. The Me page's characters card and telling
  the bot do exactly the same (one verb behind all three).
  - **Alts**: *Add an alt* button, `/me char action:Add an alt` (first and last name, like the button), `/me plan alt`.
  - **Spec/offspec**: a sheet you answered shows the new spec at once (sheets keep the spec you answered with) — a
    planned character without a name too, whose name on the sheet follows ("Priest (Holy)" becomes "Priest (Shadow)"). On a
    locked roster, a change in the same role follows your seat; a change of role (healer → damage) on a **recurring**
    run keeps the seat as the roster was built and tells the officers on the run's card — they decide; on an **ad
    hoc** run it hands the seat back (Join again to queue). Nobody is DMed.
  - **Main**: your rank moves with it (below). **Retire** keeps history; **Delete** removes a character outright
    (prefer Retire if you may come back to it). Either way a seat the character holds on a locked run is handed back
    and the bench asked, an open sheet's Join or Bench with it is cleared (a No thanks moves to your other character),
    and it comes off the regulars of any schedule; if it was your main, your next character becomes main. Your own
    change writes you no "seat released" notice (you did it); when an officer retires or deletes it for you, your Me
    page says so.
  - **Name a planned character at launch**: `/me char action:Name a planned character` — pick it (skipped when
    there is only one), type first and last name, and the confirm screen says what follows (*"Answered Join on … — the
    answer now names Stormcall"*); answers and seats it holds take the new name.
  - There is no "delete my record": a member who leaves keeps their history with the officers.
- **Rank** is the member's, not a character's: it lives on your main, every alt reads *alt*, and switching mains moves
  it to the new main. Only officers set it (`/roster rank`, `/roster char`, the Members page's rank select) — never
  your own (the owner aside), and never on an alt.
- **Extra roles**: your role follows your spec and your offspec counts as flex; `/me char action:Flex` (or the Me
  page's *Flex* column in Edit) picks roles a character could also play — only the roles its spec and offspec don't
  already give — and telling the bot does too ("I can also tank on Xanthe", "no extra roles on Jonny").
- **Absences**: `/me absent list` shows yours with a **Clear** button on each (they survive a bot restart); a Clear,
  `/me absent clear` and **I'm back** on the absences card all open the same screen: pick the absence, see which
  sheets re-open for you (a seat it handed back stays handed back), press *I'm back*.
- **Away for a while**: press *I'll be away* on the card in the absences channel, or `/me absent add`, or the Me
  page. Nothing is typed: pick the **first day** (Today, Tomorrow, then the days after — *Later than this…* picks a
  week further out; on the site, a date picker) and **how long** (1 day up to 4 months — the same list everywhere),
  with an optional reason (up to 200 characters) only you and the officers see. A confirm
  screen (on the site: the preview under the pickers) shows the days in words and which live sheets it touches before
  anything is saved. Days that overlap an absence already on file are refused with that absence named ("already away
  Tue 9 Dec → Thu 11 Dec (recorded by Kessa) — clear that one first (I'm back), or pick days outside it"). Sheets on those days
  get *No thanks* for you, and if you were already rostered the seat is released (your confirmation card in your DMs is
  edited to say so — no new DM) and the bench is asked; the reply names the sheets it touched (on the Me page and over MCP too). Officers record one for
  someone else with `/roster absent` and a member picker, same screens. Your own absence writes you no "seat released"
  notice (you did it); when an officer records it for you, your Me page and `/me view` say your seat was released
  because you're marked away.
- **See everything the bot has on you**: `/me view` (your inbox first — open asks and unread notices, with **Mark
  notices read** — then characters, rosters, absences, your attendance, your last ten awards and how many items are on
  your wishlist) or the *My status* button.
- **Attendance**: *"5 of 6 in the last 30 days"* in `/me view`, and the Me page's **Attendance** card lists each run —
  day, raid, the character's spec icon and what happened (rostered, benched, away, can't make it, no thanks, no
  answer). It counts finished raids only, over the window your loot policy uses (30 days unless the guild changed it):
  a run counts when you were one of its regulars or joined it, and a day you marked away **before the raid started** is
  shown but never counted (an absence added after a raid you missed doesn't take the miss away).
  Every character of yours counts toward the same number. Rostered and benched both count here; for **loot** only
  runs you were rostered on — and on a regular raid confirmed — count, and attendance counts in loot scores only once
  the guild has three finished raids in the window (until then it is neutral for everyone). A pickup run counts only
  for those who joined it. Before the first raid ends it reads *"not tracked yet — counts from the first run that ends"*.
  Officers see each member's count as *att 5/6* under their name on the Members page (the loot count in its tooltip),
  and once the guild has three finished raids in the window the roster builder prefers better attendance too — someone
  with no finished raid yet is treated as full attendance, never held back for being new.
- **DMs on or off**: `/me dms on|off`, the switch on the Me page, or "turn my DMs off". The bot says it in one sentence
  everywhere: *"With DMs off, Confirm and fill asks wait on your Me page and the sheet; nothing is sent anywhere else."*
- **Wishlist**: `/me wishlist` — per character, up to 10 items in the order you want them, main spec or offspec, a
  note; the Me page's Wishlist card is the place for long lists and re-ordering (§9). **My loot** on the Me page shows
  what your characters received.
- All `/me` commands also work in a DM with the bot.
- **Or just tell the bot.** DM it or @mention it: "I'll be away Nov 1–3", "back early, clear my absence",
  "make Xanthe my main", "change Xanthe to Holy", "add my alt Jonny, Mage Frost", "add Jonny as my main",
  "plan my main as a Holy Priest" (a planned character, no name yet; saying it again replaces the plan and keeps
  its rank and roster placement), "delete my alt Jonny", "I can also tank on Xanthe", "bench me for tonight",
  "I can't make tonight" (a callout like `/raid out`; "… — work trip" adds a note only the officers see), "I confirm for
  tonight" (also answers a fill ask the bot DMed you), "turn my DMs off", "put the Robe of Tests on my wishlist as #2
  for offspec", "take the wand off Jonny's wishlist". It shows what it will change and waits for
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

**Your own application comes first.** While it is open, `/register`, *Apply to join*, `/me view` and the Me page say
*"Your application for Kessa Dawnfire is waiting for an officer"* — even if applications have closed since (you are
never told "isn't taking applications" about one you already sent). Once decided, the Me page and `/me view` show the
decision, its date (and the note, if any) — never which officer decided; you also get the decision by DM, since you
were waiting on it. Officers see who decided on the Applications page.

**Officers** get a review card in the applications channel: the full character name, class and spec icons, the logs
link, the schedules the applicant can make in words, and every answer as its own line. When the same person applied
before, the card says so first: *"Applied before: declined 12 Mar by Jonny — full on healers"*. A very long
application is shortened on the card with "…", and its last line points to the site's **Applications** page (officers),
which shows every answer in full, the earlier verdicts, and Accept / Decline with the same sentences as the card.
**Accept** registers the character as the applicant's main, rank trial, already confirmed, and gives them the guild's
**member role** (the first one in the recruitment settings) when the bot has Manage Roles and the role sits below the
bot's own — otherwise the verdict says it couldn't and you add it by hand; **Decline…** turns them down. A second
decision on the same application is refused with who made the first (*"Already declined by Jonny (Tue 12 Mar 8:02
PM)."*); the note to the applicant holds up to 400 characters. Both open the same small screen first: one sentence saying exactly what happens to the applicant, then
**Accept** (or **Decline**), **… with a note…** (a box for a note that goes into their DM) and **Back**. Nothing is
written before that press. Either way the applicant gets a DM, the card updates to show the decision, and the ops
channel logs it. The same decision is on `/roster applicant`, in plain text ("accept Nyx", "decline nyxie: we're full on
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
  1 tank, 1 healer (Thorn, Ona away)" — the hole shows a week ahead instead of at reminder time. On the **Readiness**
  page a short run has **Open this run early**: after a confirm the bot opens that run's sheet now (the same as opening it
  from the Raids page), so the hole is worked on from today; a run already open says *sheet open* instead. The same numbers show
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
  lock arriving first) from either closes the other: its DM card is edited to "never mind" (never a new DM); when
  the second yes comes, the first one's card is edited to say it's on. Nobody is suggested for two seats (the
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
  minimum — an early warning — and posted again, rebuilt, at reminder time. On the site, the sheet's one **Fill**
  button opens the same list (pick the suggestion or an alternate per seat, then *Send*); its tooltip says how many
  seats are short and how many suggestions there are. `/raid fill` on an open
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
- **After lock** the fill engine (step 6 below) uses the same list. On every run — recurring or ad hoc — it asks people for the
  same role on its own, but a **role swap** (a flex role, an offspec or an alt, with or without someone covering)
  waits for an officer: the lock card shows it under *Suggested* with **Ask** — while it waits, a seat freed later
  (a missed confirmation) still gets its same-role asks automatically. An officer's Ask after lock counts the asks
  still out (nobody is asked twice for one seat) and keeps the cap of 3 questions out at a time: what doesn't fit
  stays on the card ("1 more waits"). An **ad hoc** run works the same way: no swap goes out without an officer.

Timeline for each run (per-raid settings; defaults in brackets, Barrow Deeps opens 48 h before):

1. **Sheet opens** — `signup_lead_hours` [120 h] before the slot the bot posts the sheet in the signup
   channel: **Join** (I'm coming), **Bench** (call me if you need me), **No thanks** (⚑ after a name = called
   out after answering). With more than one character you pick which one. Anyone with a registered absence that day
   is listed under **Away** instead (names only, the first 15 then "+N"; reasons stay with officers): they aren't
   nudged or asked to fill, and pressing Join or Bench anyway takes them out of Away. A Join on a day you're away
   answers that sheet only: the receipt says *"joined for this run; your absence … still stands for the other sheets"*
   with an **I'm back** button for the whole absence. On a recurring schedule with
   regulars, if the regulars minus anyone away can't cover a tank or healer minimum, the officer channel gets the
   health card with its *Suggested* block right away (an early warning; see *Suggestions* above). With `open_dm` on [off] everyone
   the sheet is for (the schedule's regulars, or every main while it has none) is also DMed when it opens. Officers can
   open a sheet early from the Rosters page: **Open next run** takes the raid's next scheduled run (with several
   recurring schedules, a picker beside it chooses which), and **Start ad hoc run** opens a one-off (or a pickup
   template, picked beside it) at the date and time you pick. `/raid open` does the same in Discord: pick the raid,
   then **Next run — <time>**, **Another upcoming run…** (each labelled with its schedule's name when there are several)
   or **Ad hoc run…** (a pickup template, or *One-off — pick a day and time*: day, hour and minutes from lists; nothing
   typed; ad hoc defaults; more than three pickup templates come as one *Pick a template* select). **Another upcoming
   run…** pages by week (*← Earlier week* / *Later week →*) when the raid has more upcoming runs than one list holds.
   A one-off picked at exactly the minute of an upcoming recurring run **is** that run: it opens as scheduled, with its
   regulars and confirmations, and the confirm screen says so. The confirm screen shows
   the time in guild time and in your own, and on a daylight-saving night says which time it used (2:30 AM on the
   spring-forward night becomes 3:30 AM; on the fall-back night the first 1:30 AM is taken). On the site the
   **Upcoming** tab has an **Open** on every row (one press, then the same confirm), and *Start ad hoc run* asks the bot
   what the picked time would open as you pick: when it is a scheduled run the button reads *Open the … run —
   scheduled, with confirmations*; its confirm shows both clocks, the run's rules, the daylight-saving note and the
   channel the sheet posts in, and opens exactly the run it showed (if the time now means another run, nothing opens
   and the preview is shown again). With **no signup channel set** the scheduler opens nothing: each run that was due
   gets one warning on the ops feed ("Barrow Deeps Tue 7:30 PM did not open: no signup channel is set") and its
   Upcoming row says *needs a signup channel* in red. A raid with no run times offers *Set this raid's run times →*. A run opened closer
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
   yourself. After lock the leftovers are simply the bench — the first people Fill asks when a seat frees up; each
   gets one notice on the Me page (*"You weren't rostered (20 seats, 23 joined). You're in line for a freed seat"*),
   never a DM until a seat is actually offered.
   **Cancel run** is one screen everywhere: first the sentence from the run as it stands — how many seats are
   released, how many confirmation and fill asks close — then an optional **reason** (officers only: it goes to the
   run's updates thread and the ops feed, never to the signup channel) and **Tell the channel?** (a fixed line in the
   signup channel — "🛑 Barrow Deeps … is cancelled. Rostered seats are released — nothing to do." — on by default, or
   no line). **Nobody is DMed.** Every open ask is closed and its card in the member's DMs is edited to *Cancelled*;
   the sheet changes to *Cancelled*; the Me page shows it. In Discord the card's **Cancel run** and `/raid cancel` show
   the sentence with *Cancel the run…* (the box with the reason and the channel choice) and *Back*; the site's dialog
   has the same three parts; plain text takes the reason and "…, no announcement" for no channel line, MCP takes
   `reason` and `notify`. A cancel waits for a lock that is being built and then cancels the locked run — the lock
   stops asking people the moment the run is cancelled, and so do fill asks, suggestions and a Remind that are going
   out: nobody after the cancel is DMed, and a card that arrived just as it happened is edited to *Cancelled* at once.
   Clearing the test bench cancels its test runs without a line in the signup channel. A cancelled run is final: it
   can't be reopened, and opening a run at the same time is refused ("That time's run was cancelled — pick another
   time"; the open screen and the site say so before you press anything). A test run opened in the same minute as one
   you just cleared starts a minute later.
4. **Lock** — `lock_hours_before` [24 h] before the slot (or *Lock now* / `/raid lock`): the board becomes
   the roster(s); the sheet switches to its locked form (groups, Not rostered, Bench, No thanks, one *Can't
   make it* button); the officer channel gets the roster cards. A roster that came out under its tank/healer minimum
   (nobody else could fill it) says **Locked short** with the role icons and a number, instead of locking quietly; the
   card also lists who the solver moved to an offspec (*Playing an offspec*, one per line) and, on a recurring schedule,
   the **regulars not rostered** (who joined but sat out — they get priority next time). Pre-lock asks nobody answered
   close now. Every rostered member gets a DM: **Confirm** / **Can't make it** — showing the spec they're rostered as
   ("You're rostered as **Protection**, your offspec"). The Me page shows the same ask; with DMs off, or when the DM
   can't be delivered, the ask waits there instead (same deadline — a failed DM never counts as an answer) and the lock
   card marks them *(site)*. **Preview lock**: the lock's confirm screen (the card's *Lock now* and `/raid lock`) has a
   *Preview lock* button that draws the roster(s) the lock would build right now — every group, one member per line
   with the spec icon, then the leftovers (the first 15) — without saving anything; *Lock now* or *Back* from there.
   The site's lock dialog has the same *Preview*. If the bot stops half way through the confirmation DMs (a restart, a
   Discord outage), the lock card says *confirmations not yet sent* and within a minute the bot asks exactly the
   rostered who weren't asked — nobody twice. If a lock fails (the solver finds no roster), the sheet's footer says
   *Lock failed <time> — the sheet stays open, retrying* and so does the site. With confirmations off (the ad hoc default) nobody is asked and
   no seat expires: the sheet says "you're in — press Can't make it if that changes". After lock the board stays editable: moving people
   between groups changes nothing else; dragging someone in from the bank is a substitution (they get the
   confirmation DM); dragging someone out releases the seat (their confirmation card says so — no new DM) and the bench
   is asked — the confirm names them and says exactly what the bot will do, in the same words as the Discord card. A
   drag started on the open board and dropped after the run locked is refused ("The run locked while you were
   dragging") and the board shows the roster — it is never applied as a change to the locked roster.
5. **Confirmation deadline** — `confirm_hours_before` [6 h] before the slot, anyone who hasn't answered
   counts as out; their seat is released and their confirmation card is edited to say so (no new DM). Only someone who
   was actually asked can time out: a seat whose confirmation never went out (an interrupted lock) is never freed for
   "no answer". **Remind the unconfirmed** (the lock card's second row, the site's run menu) sends the Confirm / Can't
   DM again to everyone still unanswered — the same ask, the same deadline — at most once an hour per run; the ones
   waiting on the site aren't DMed. The older Confirm card loses its buttons and reads *Reminded — see the newer message
   below*, so only the newest card answers. Two officers pressing it at once send one round; the second is told who reminded.
6. **Fill** — the fill engine DMs people on its own only after lock (before lock an officer sends suggestions, see
   *Suggestions* above). Whenever a seat frees (decline, callout, absence, missed
   confirmation, officer removal) and `autofill` is on [on], the bot DMs the next best people, in the suggestions' cost
   order: joiners the solver left off and *Bench* answers first → the schedule's regulars who haven't answered (every
   main while it has none; on an ad hoc run, every main) → other members → **swaps** (a rostered player's declared flex
   role, offspec or alt in the needed role; never the spec the lock already moved them to). On every run (recurring or ad hoc)
   a swap is not sent on its own: the lock card shows it under *Suggested* and an officer presses **Ask** (or Fill
   seats); the updates thread says so once. A swap goes out only when the role it vacates stays covered; otherwise the bot looks ahead
   for a bench or pool member of that role and sends both asks tied together — the swap and its backfill — and
   nothing changes until both say yes (the first yes waits; the second makes both changes and tells the first);
   a no (or silence) from either withdraws the other — its card is edited to "never mind". It asks the shortfall + 1 at a
   time, never more than 3 outstanding, and never asks someone who is rostered on another raid within 4 hours or
   absent. A fill DM that can't be delivered leaves the ask on the person's Me page (same deadline; the lock card counts
   "N wait on the site") — never a no. **Pause autofill** (the lock card's second row, the site's run menu) stops the
   engine asking anyone on its own for that run; an officer's own *Fill* / *Ask* still sends, and *Resume autofill*
   picks up where it left off. Every fill DM says when silence starts counting as no: `fill_ask_hours` [4 h] after
   it was sent, never later than the run start; the updates thread logs the timeout and the next person is
   asked. A yes goes straight into the freed seat (a swap changes the seat in place). A roster that is full but
   short a tank or healer has no seat for a newcomer, so only swaps are asked for it; a yes that finds every seat
   taken meanwhile is a Join, not rostered, and the bot says so (move them in on the board). Officers can run a batch
   by hand: **Fill seats** on the Rosters page (locked runs) or on the lock card in the roster channel, and
   `/raid fill`, first show the batch — what is short, who is still being waited on, each seat with its alternates,
   and **exactly the asks Send DMs**, one per line: who, as which character and spec, for which short role, a role
   swap marked **SWAP** (with the role it leaves) and its tied backfill named; what waits under the 3-at-a-time cap;
   what stays short — then *Send N asks* or *Cancel*. Send sends only what was shown: if the board or the answers
   changed the plan meanwhile, nothing is sent and the batch is shown again as it is now; when another officer pressed
   first the screen says so ("Already sent by Jon at 8:12 PM — the plan is redrawn"). An officer's Send includes
   the role swaps it lists. On a very long list the reasons go first, then the last seat lines ("+N more seats"); when
   even the batch can't fit one screen, the screen lists the asks of the first seats that fit and the button reads
   **Send these N (the rest on the site)** — it DMs exactly those; the site's Fill sends the rest. **Withdraw an ask…**
   on the same screen (and *Withdraw* on an ask in the site's run detail) closes an ask already out before its
   deadline: its card says "never mind", its tied half is released.
   `/raid fill preview:true` shows the same batch without a Send button. With `autofill` off
   nothing is asked until an officer presses Fill seats. On the site one **Fill** button opens the right screen before
   and after lock; when a role swap waits for you (any run, ad hoc too), a yellow banner above the roster says *Waiting on
   you: …* with the Fill button, and the fill state is in words ("asking — waiting for answers (1 on the site)").
7. **Raid** — `/raid loot` (or the lock card's **Loot**, or Loot › Tonight on the site) opens the loot council;
   `/raid end` ends the run and its council (§9) — so does **End the run now** on the lock card's second row and the
   site's run menu, both after the scheduled start and both through one confirm screen. Ended by mistake? The closed
   officer card keeps a **Reopen** button (and the run's page in History a *Reopen*) for an hour: the run goes back to
   locked with its roster and asks as they were (the loot council stays archived — open it again with /raid loot).
   Never a cancelled run, and not after its scheduled close.
8. **Close** — 3 h after its scheduled end (start + the raid's length) the run is **done** and the sheet says so.

**Reading a run.** `/raid health` works at any time: on an open sheet the health card, on a locked run a brief with
rostered against the seats, what is short (role icon + number), the confirmations tally and the fill state in words —
officers also see who is waiting and the lock card's buttons; members see the counts only. On the site a sheet shows,
under its role counts, who could cover a short role (officers) and the party buffs nobody brings (icons only, names in
the tooltips); links to the sheet, the officer card and the updates thread in Discord; and, once locked, *Playing an
offspec* and *Regulars not rostered* (one member per line, the spec icon alone). The run's **detail** lists its fill
asks (who answered for whom, which wait on the site), callouts with their notes, and the **log** — every line with its
time in words, lines only officers would read marked *officers*, the raw lines behind *machine form*. **History**
lists the newest eight past runs per raid and *Show older runs* pages the rest; a row opens the run read-only in a
side panel (a `#run-<key>` link opens it too).

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
out` (hours before, late after the lock). The same two words mean the same thing on every surface, before and after
lock: **No thanks** is an answer change (no record), **Can't make it** is a callout with an optional note for the
officers. On the Me page *My sheets* has Join · Bench · No thanks while the sheet is open and *Can't make it…* (with
the note box) once it is locked; your own "I can't make tonight" in plain text is a callout too, note or not — also
when an officer presses Apply on your request for you.

**Officers record anything a member can do, for them, from one place per surface.** In Discord: `/raid set
member:<someone>` (optionally `what:` to go straight to one thing) picks the run first — only runs where something
applies to that person, never a test run (answer for puppets with `/gm test answer` or the Test bench) — then offers **only what applies to them on that run**, in the members' own words, raid-night
first: *Confirm for them* / *Can't make it for them* (their open Confirm ask) · *Confirm their fill* / *No to their fill*
(their open seat or swap question) · *Record a callout…* (with an optional note; before lock it is No thanks + the
callout) · *Join* (as which character, "as <alt>") · *Bench* · *No thanks* · *Pin to roster* / *Keep on bench* /
*Unpin* (open sheet). The confirm screen says what happens and who is told — *"Kestrel goes to No thanks on Barrow
Deeps Tue 7:30 PM; no callout is recorded. Kestrel sees a notice on their Me page — no new DM."* — and if the member
pressed something meanwhile it says so first (*"Kestrel is now Bench as Kestrel (since 5:12 PM) — keep going?"*).
On your own record it is your own answer, and the screen says so: *"Your own record: no notice is written."* On
the site every row of a sheet (joined, Bench, No thanks, and the *not answered* list) has a **⋮ menu** with the same
list, and the sheet's header has **Someone else…** for anyone not on it. Plain text ("Kestrel can't make it tonight,
her baby is sick" is a callout; "set Kestrel to No thanks" is not) and MCP `set_answer` (`out` is a callout unless
`callout=false`) do the same. Whatever an officer records for someone reaches them as a **notice** on their Me page and
in `/me view` ("Jon set you to No thanks for Barrow Deeps Tue 7:30 PM"), and an open DM card of theirs is edited to
*answered for you by Jon* — never a new DM. Pins are officers' planning: nobody is told. When an officer drags
someone onto a locked board, the run's thread says they were asked to confirm only when the run asks for
confirmations (an ad hoc run with confirmations off just says *in*).

**Your inbox (Me page, `/me view`).** Asks — what you must act on — come first, one card each: what it is (*Confirm
your seat*, *A seat is open*, *Could you switch?*, *Could you come?*), the run, the spec icon you'd play (its tooltip
says *as Holy*), the group it puts you in
(spec icons, one member per line), *answer by Tue 6:30 PM*, and the two buttons. Pressing one is the same as pressing
the DM's button — the card shows the reply, and a second answer (from another device, or the DM) is refused with what
the first was (*"Already answered (yes, by Kestrel at 5:12 PM)."*). A tied pair you said yes to shows *waiting for
Kestrel's answer*. Under the asks, your **Notices**: outcomes the bot used to DM, or never told you, now land here
instead — *not rostered* at lock (how many seats, how many joined, and that a freed seat would reach you), a seat
released (by the board, an absence, a missed confirmation), something an officer answered for you, a run cancelled,
a schedule you were a regular of removed, your application's decision, a character confirmed, a loot award (a
correction reaches both — the one who gets the item and the one who held it). Notices
are kept 30 days (the newest 20); *Mark read* clears the bold. **DMs off** changes where asks wait, never whether you
are asked: before lock and for confirmations you are asked in the same order as everyone else, on the Me page, with
the same deadline (the lock card and the updates thread mark it *(site)*). After lock, in the last hours before the
start (when a fill ask can no longer run its whole wait), a seat offered to someone with DMs off is also offered to
the next person a DM can reach, at the same time: the first yes takes the seat and the other ask closes (*"Never mind
— someone else took the seat first"*).

**Where the buttons are (site and Discord).** A run's actions sit in one bar under its board and never move: *Cancel
run* far left, then *Fill* and *Lock now* on the right; what does not apply yet is greyed with the reason. The run's
**⋮ menu** (top right) holds *Remind the unconfirmed*, *Pause / Resume autofill* and *End the run now*. The board's own tools sit above the groups: *Auto-fill empty seats*, *Propose
roster/splits*, and *Clear* set apart. Officer cards in Discord use the same fixed order — Open the board · Lock now ·
Fill seats · Loot · Cancel run (the *Suggested* block has its own row: **Ask** · **Suggest…**), and the lock card adds
a second row: **Remind the unconfirmed** · **Pause autofill** · **End the run now**. **One confirm screen, the same
words everywhere.** Before a lock, a cancel, clearing the test bench, deciding an application or approving / dismissing
a news proposal, the bot shows one sentence computed from the run as it stands — for a lock: *"The board becomes the
roster. 18 rostered get a confirmation DM (2 with DMs off see it on the site). Confirm by Tue 6:30 PM."*; on an ad hoc
run: *"Joining was the commitment — nobody is asked to confirm."* — then **Do it** (named for the verb: *Lock now*,
*Cancel the run*, *Approve*…), **… with a note…** where a note makes sense, and **Back**. The site's dialogs print the
very same sentence (it comes from the bot, the page never writes its own), so no surface promises a DM the bot would
not send. Tables (Me, Members, Raids, Auras)
share one edit mode: an Edit button in the header, then a footer with any reset far left and *Cancel* / *Save changes*
on the right. Config's **Settings** and **Channels** are edit modes too (**Edit settings** / **Edit**, then one Save, reviewed like any other); only the loot-log switch saves as you flip it, and the news keywords when you leave the field (*✓ saved*). **How much a Save asks depends on what it
does** (the same rule on every surface): a routine, reversible edit (a note, a status, a strength, an hour) saves at once
and the toast says what happened — *"2 saved"*, or *"1 saved, 1 refused"*, never a bare "saved". A change that moves
where things post or who counts — a channel, the timezone, an officer role, a schedule removed or paused, a buff's scope
or stacking family, an aura reset, applications closed, plain text in DMs or the ops channel — or one with a
consequence (regulars coming off a schedule, say) opens a **Review** first: one block per thing it writes, each setting
*current → new*, any part the bot would refuse already marked ⛔, the consequences in a yellow box, then **Apply N
changes** or **Back to edits** (your draft stays). After Apply the same window lists ✅ / ⛔ per write — a refused one
names why, and nothing else was changed; the table stays in edit mode holding only what was refused. Each thing is one
write: a buff's fields save together or not at all. If someone else saved the same settings while you were editing, the
Save is refused (*"…changed since you looked"*) and your edits are kept — reload to see their change. If the review sat
open past ten minutes it is checked again on Apply and goes through when nothing moved; after a bot restart it says
*"The bot restarted — review again"* and shows the fresh lines. Pages update on their own; *Refresh* in
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
benefit from blood pact"; naming a family that doesn't exist yet creates it). On the page, **Edit auras** turns the buff table
into one draft: scope, stacking family (or **New family…** — you name it, the bot makes its id from the name; ids are never
typed), strength as a preset (×0.5 … ×3, or *other…* for any number), status and note, then one **Save changes**; a scope or
family change is reviewed first (above). A family's editor saves only the beneficiaries you changed, so a key someone else
set meanwhile is kept. `/gm config aura` works the same way in Discord: pick the buff, then change what you like — each pick
only updates a draft, shown as one line (*"✏️ Not saved yet: Scope party → raid · Who benefits: Healers 2 → 3 · 2
changes"*) — and **Save this buff** writes it all at once; *Another buff* or *Done* with unsaved changes asks Save /
Discard / Back. *Who benefits* changes one beneficiary at a time and leaves the rest of the family's map alone. **Reset
everything to game defaults** (on the buff list) says exactly what goes — *"Overrides on 3 buffs (…) and 1 family (…)
go…"* — before it does it. Amber on the page marks a guild override; *Reset* returns one buff or family to the game
defaults, and asks first on the page as in Discord (*"Blood Pact goes back to the game's scope, family, strength, status
and note…"*). Choosing *Stands alone* for a buff that shares a family gives it a family of its own: *Who benefits* then
edits that new family and leaves the one it shared alone.

## 6. Analytics (officers)

- **One card per raid** in the analytics channel, native (no images): mains vs the raid size, a role icon with
  have/need for each role (⚠ = short), ⛔ followed by the buffs nobody in the pool brings, **Short** and **Over cap**
  lines from the desired comp (class or spec icon with have/want), and links to the Members and Raids pages.
- **Desired comp** per raid is derived from the buff matrix and comp rules. Two tiers, the same words everywhere:
  **bounds** — *at least / at most* of each role the **solver seats** — are the owner's (*Group make-up* in `/gm config
  raid`, *tank min / max* on the Raids page, `tank_min` in plain text); **targets** — what the guild **aims for**, what
  readiness measures — are the officers': per role (tank, healer, melee, ranged), class or spec, *at least*, an optional
  *at most* and *why* (the selects also offer a value already stored past their range, such as a max equal to the run size). Set them on the **Readiness** page (**Edit targets**: slot icon · at least · at most · why, one
  **Save targets**), in the Raids page's **Desired comp** table, with `/roster comp` (pick the raid, *Set a target for…*
  a role, a class or *A particular spec…*, then at least / at most / why in one form; *Remove a target*; **Save
  targets**), in plain text ("reduce healer to 3-5", "cap hunters at 3 because Trueshot", "clear the paladin target")
  or MCP `set_comp_target`. One write per raid whichever way; a target outside the bounds is refused with *"at least 4
  healers but the raid allows at most 3 — change the bounds first (owner)"*. A target without a reason shows *no reason
  given*; the reason you give is kept with the target and shown by the Readiness page, `/ask` (readiness) and MCP
  `raid_readiness`, next to the line it explains. The group layout (`comp_groups`) stays plain text only.
- The same readiness as data: MCP `raid_readiness` (one raid or all) and `GET /api/readiness` — headcount, each role's
  have/need with who could cover it, buffs nobody brings, every desired-comp line with its reason, what is short or
  over cap, and the pool plan (officers only; a member's request gets 403). The analytics card reads the same numbers,
  so the card, `/ask`, the API and MCP never disagree.
- The **character bank** lives on the Members page and **groups** are built on the Rosters board (Auto-fill /
  Propose); the old bank, optimised-groups and comp images are gone.
- `/roster overview` (no arguments) shows the plan summary on demand, only to you (officers get **Post to the roster
  channel** under it for the officer version): every planned main by class, the role counts
  against each raid's size with who could flex in, buff coverage (nobody / only one), the alts, and the recruiting ask
  against the biggest raid, and — for officers only — **Regulars**: one field per recurring schedule with its regulars
  coverage and 4-week forecast (recruit candidates and who is away are planning data; a member's overview leaves them out). `/roster members` lists a recurring schedule's regulars by role (pick the schedule).

## 7. Officers: day-to-day commands

- Registry: `/roster list|confirm|rank|set-main|char|add|remove|members|absences|absent|back|comp` (`comp` = the desired
  comp's targets, §6). They take at most a member
  (Discord's own picker) and a fixed choice; the character, rank or schedule is picked from a list and confirmed before anything saves.
  `/roster char member: [action:]` is `/me char` on their behalf (Spec · Main · Flex · Rank · Retire · Delete), with the
  same ripple on the confirm screen; `/roster rank` and `/roster set-main` are its Rank and Main. **Rank** is the
  member's (on the main): no character to pick. `/roster confirm` alone walks the unconfirmed characters — never your
  own (self-confirm defeats the badge; the owner aside); the member sees it on their Me page, no DM. Rank, confirm
  and every character change go through the one character save, whichever surface you use (Discord, the Members page,
  plain text, MCP), so two officers at once never half-apply each other's change.
  `/roster absences` lists the guild's upcoming absences
  (days in words, with reasons) for the next 30 days; a select switches the horizon (a week … three months) — no
  number is typed; a long list ends "and N more — the Members page lists them all". `/roster back member:` is their
  *I'm back*: pick the absence, see which sheets re-open, confirm. Test puppets stay out of these lists, the absences
  card and the absences channel. Officers only, checked on every press: lose the officer role mid-screen and the next press is refused.
- Regulars: `/roster add` (pick the recurring schedule, then the member — or name them in the command — then the
  character), `/roster remove`, `/roster members` (a schedule's regulars by role, one per line). Same list as the
  Rosters page's *Regulars* tab, plain text and MCP `set_regular` (§4 *Regulars*).
- Applications: `/roster applicants` (oldest first, ten cards; the rest are on the site's **Applications** page),
  `/roster applicant member: decision: [note:]` (the same one confirm screen as the card's buttons), the Accept /
  Decline… buttons on the review card, the Applications page, or plain text ("accept Nyx", "decline nyxie: full on
  healers") (§3a).
- Someone who **left the Discord server**: their record stays. The bot marks when they left as soon as Discord tells it
  so — when it looks them up (a page, a DM, a sheet) or in its hourly check of the registered members (about twenty per
  hour) — and clears the mark if they come back. The
  Members page lists them under **Left the server** with **Remove record…** (one confirm saying what goes and what stays;
  loot already awarded stays in the ledger); in Discord `/roster remove-record member:`; MCP `remove_record`. Refused
  while they're still in the server or hold a seat on a live run. Members can't delete their own record.
- Runs: `/raid open|sheet|health|fill|lock|set|cancel|list|loot|distribute|end` (officers) and `/raid out` (anyone).
  They need no arguments: with one live run they act on it; with several they ask which. `/raid set` takes the member
  (and optionally `what`), then offers only what applies to them (§4 *Officers record anything…*). `/raid sheet`
  (anyone): an officer **moves** the live sheet here, or to `channel:` — its buttons and updates follow it and the old
  message says *Re-posted in #raids*; anyone else posts a **read-only copy** labelled *copy — the live sheet is in
  #signups* that follows the sheet but never moves it (a member's `channel:` is not followed — the copy lands in the
  channel they ran it in, and the reply says so). Channels where the bot keeps quiet (plain-text mode *ignore*)
  are refused. The site's sheet menu has **Re-post sheet…** with a channel list. `/raid cancel` asks for an optional
  reason in a box.
- Precedents: `/gm precedents` — read the council's precedents and retire one with a reason (§9).
- Policy: `/gm policy show|edit|reload`, `/gm rule loot|comp` (the bot shows the end of the current document; press
  **Write the rule** and write it in a paragraph box) — prose is compiled by Claude into rules and
  an officer confirms the reading before it goes live. Plain text does the same: "add a loot rule: tanks first on
  shields", "drop the loot rule about attendance", "recompile the comp policy", or a whole new text; after **Apply**
  the bot shows its reading with **Confirm this reading** / **Discard edit** (Discard puts the previous text back).
  If the reading can't be made right then (Claude unavailable), the change is refused and the text is put back as it
  was — nothing is left half-done. The **persona** gets the same two steps: its draft is shown back as written (no
  Claude needed) and the bot keeps speaking with the old one until someone presses Confirm. A document has one edit
  waiting at a time, whoever started it and wherever (Discord, the site, plain text, MCP): Discard goes back to the
  text before that edit. The waiting edit is kept on disk, so a bot restart loses nothing — the draft, its reading and
  the text to go back to are still there. A reading only goes live against the text it read — if anyone changed the
  document after it was compiled, Confirm (and a stale Discard) refuses, names whose draft is waiting, and asks for a
  fresh compile. **Adding a rule** (`/gm rule`, "add a loot rule: …", the site's *Add a rule*, MCP `policy_append`)
  while ANOTHER officer's draft waits is refused with their name — confirm or discard theirs first; your own draft you
  may keep adding to.
  `/gm policy show` sends the written text (up to three messages, then "continued on the site"), one **Compiled
  reading** card with the live rules numbered (fifteen, then "+N more on the site") and a footer saying when it was
  compiled and by whom, whether the text still reads that way, and whose draft waits — and, when a draft's reading is
  waiting, that reading with **Confirm this reading** / **Discard edit**. `/gm policy edit` opens the whole document in
  the box when it fits Discord's 4,000 characters; a longer document is never shown cut — the box then only adds what
  you type at the end ("the full text is on the site"). The site's **Policy** page shows each document's written text
  beside what the bot applies (compiled when), a red bar when they differ, the waiting draft with Confirm / Discard,
  **Edit** (no length limit) → Save draft → Compile → the reading beside the draft → Confirm or Discard, and **Add a
  rule**.
- Plain-text config: `/gm change <text>`, or @mention the bot in the ops or analytics channel — the bot
  shows a "current → new" diff with Apply/Cancel. It never guesses: ambiguous requests come back as a
  question, and a plain question ("who is rostered tonight?") gets the same answer `/ask` would give. Whoever presses
  Apply needs the rights for the change (§8c); a slow change (a lock, fill DMs, a compile, a proposal) is acknowledged
  at once — the buttons go, the diff reads *Applying…* and turns into the result when it is done, and a second press
  (or Cancel) by anyone is refused, so nothing applies twice. A diff nobody presses within ten minutes turns into
  *Expired — say it again.*; a press on a diff (or a policy reading) from before a bot restart answers *Expired after a
  restart — nothing was changed* instead of Discord's "interaction failed". When something breaks inside a change,
  every surface says the same thing — *Something went wrong and nothing was changed. The owner has the details.* — and
  the owner gets the details in the ops channel and a DM; a refusal ("that run is locked") always reads as written. A board change (a move, the groups, auto-fill, using a
  proposal) is refused with *"the board changed since you looked"* when someone else changed that board between the
  diff and your Apply — nothing is overwritten, even when an unrelated step of the request (a DM setting) comes first;
  the request's own earlier steps (a pin, a move) never count as a change, and once one board step is refused the request's later board steps on that run are refused too. Absences
  entered this way are announced like `/me absent add`; channel changes post (or drop) their card like the Config page
  does. Several raid or schedule settings in a row are saved as one change, so "open 100 hours before and lock 60
  before" can't fail half way.
- Status: `/gm status` (uptime, data repo, registry counts, LLM spend vs budget, companions, the news review — last run,
  its result, the next run, proposals waiting — and the apply job: idle, running, busy with another server's proposal or
  failed with why, recent ops),
  `/gm config show` (the effective configuration in words, one block per section — Guild (timezone with the time
  there now, owner, officer roles, who may ask), Channels (by name; one the bot can't see any more is marked), Raids and
  schedules (in words, paused ones say so), Recruitment (open or closed, the questions, the invite link and whether it
  is a Discord invite), Plain text, Test bench (with the analytics cards paused while puppets exist) and, for the owner,
  **Host** (who gets the error alerts, the host superusers by name — who can act as owner here — the AI spend against the
  shared cap, whether the MCP token works only on the host, a parked guild, *Settings on disk (.env) changed since the bot started — restart to apply*); officers read *Host facts: owner
  only.*; a long block ends *+N more on the site* — the Config page's **Overview** prints the same sections in full.
  Never the raw file). An officer's `/ask` about the settings, the setup, the channels or the host reads the same
  sections (the owner's with Host).
- Every refusal and error reads the same on Discord, the site, in plain text and over MCP: a refusal as written, a bug
  as *Something went wrong and nothing was changed. The owner has the details.* (the details go to the ops channel).

## 7a. News proposals (officers)

Once a day (9:00 AM by the host's clock) a review reads the news the bot kept from the news channel and compares it with
what the bot believes: the buffs and which ones stack, the raids (size, lockout, opening), the comp rules, and the
guild's own overrides. When a post clearly contradicts one of those, it posts a **news proposal** card in the ops
channel: the change in words, what it touches, the sentence from the news it rests on, links to the posts, and a
confidence (low / medium / high). Most days there is no card: nothing new, or nothing that contradicts anything.

- **Approve** first shows what approving does, then *Approve* / *Approve with a note…* / *Back* — the press itself never
  writes. A **guild setting** (a raid's lockout, what the guild knows about a buff, a comp target) reads "the apply
  job carries it out at its next run (the time), no restart:" and the change as a diff line; it is applied like the same
  change on the Raids or Auras page. A **game data** change reads "Claude edits the game data for <version>, the tests
  run, and the bot restarts for every server it serves (every name) for about two minutes", plus "⚠ <server> has a run
  right now — the job will wait" for each server that is raiding. **Only the owner approves a game-data change** (it
  restarts the whole bot, for every server), even when the bot serves one guild; an officer's press is refused at once
  and says so. The same holds for a guild setting that only the owner may change on any surface — a raid's lockout or
  run times, a buff, a stacking family, an aura reset: **the owner approves it** (an officer's press is refused, an
  officer can't post one on the site either, and the apply job checks again who approved it before it applies — a
  game-data change an officer approved before this rule is not applied: it reads **Failed** with "only the owner approves
  a game-data change … (approved by <officer>) — the owner approves it again to apply it");
  officers approve the rest (comp targets). Approving records who approved it and when; the apply job on the host (every 15 minutes) carries it out.
- **Dismiss** (officers) gets the same screen (D34): nothing changes, and the review is told not to propose it again
  unless newer news says more. For a game-data change that is a rule, not only a request: a new game-data proposal
  that cites nothing but news a dismissed one cited (in your server or another on the same game version) is refused. Once approved, the button reads **Withdraw approval** — it can still be withdrawn until
  the job picks it up. There is no "edit before approve" (D35): dismiss it and say the change in plain text.
- A second press after someone decided is told who did and when ("already approved by Jonny Tue 8:12 PM — nothing to
  do"), and the card catches up.
- **Needs a developer** — the change can't be expressed as a setting or a data edit; the card has no Approve and says
  so in its Status. Dismiss it once someone has handled it.
- **Raid time (D33).** A game-data change restarts the bot, so the apply job never restarts it while any server it
  serves is busy: a locked run from 4 hours before its start until it closes, an open run within the hour, or a live
  loot council (however late it runs — a council still open past the run's scheduled end counts until it ends). It waits, and writes why on the proposal ("waiting: Barrow Deeps raids until 11:30 PM") — the card's
  Status and the Agents page show it. If a raid begins while the edit and the tests run, the change is committed and
  the restart waits for the next quiet poll ("committed c0ffee1; the restart waits: …"). It also waits while the host's
  checkout has uncommitted changes or is on another branch (the bot runs from it), saying so on the proposal. A
  committed change waits for its restart even when its server is no longer served: it changes every server on that
  game version. If the proposal was dismissed while the edit ran, the commit stays and the bot restarts for it at the
  next quiet poll (the Agents page shows "Restart owed" to the owner, with **Apply now** to do it sooner). If the
  bot doesn't come back healthy, exactly that commit is undone (never whatever landed since); if it can't be undone
  cleanly the proposal reads **Failed** and says to revert it by hand. Guild settings never wait: they need no restart.
- The card updates in place: **Applying now**, then **Applied** (with the commit for a game data change),
  **Failed — nothing changed** (the setting was refused, or the tests failed and the edit was undone; a failed one can
  be approved again), or **Reverted — the change was rolled back** (the bot did not come back healthy after a game
  data change, so the commit was undone and the bot restarted on the old version).
- The same list is on the site's **Agents** page — cards stacked on a phone, each with its history (every decision and
  every "waiting" note) and the same consequence text in the confirm (with an optional note). The owner gets **Apply
  now** when something is approved or a restart is owed (the page follows the job for ten seconds and shows its result), the apply mode
  (applies to main, or opens a pull request), the MCP server's state (configured, local only, last write) and this
  month's Claude spend per server under the one shared cap (D45). A job that is busy with another server's proposal
  says just that. If an apply run died part-way (a proposal stuck on **Applying now** while the job isn't running), the
  owner can **Mark as interrupted**: it becomes **Failed** with "the apply run was interrupted; approve again to retry"
  — the job does the same by itself at its next poll. A proposal whose change is already committed and only waits for
  its restart is not interrupted: it isn't offered, and the page says "Committed c0ffee1; only the restart waits". A run
  that stopped after its commit (a push that hung, say) leaves the restart owed the same way. Without an ops channel the page says the cards aren't posted. Plain text does it too: "approve
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
its own members, raids, schedules, policy and settings — and you continue with the channels below. When the bot is
**added** to a server that isn't a guild yet, the host's superusers (or, with none configured, the owner of the one guild it serves — with several guilds and no superusers nobody is DMed: the process log and the Host card carry it) get
one DM: *"I was added to X. It isn't a guild yet — `/gm config owner` there claims it. Missing permissions: …"*. When
the bot is **removed** from a guild's server, that guild is **parked**: nothing opens, no DMs go out, the site and the
DM server picker no longer list it (the host still sees it on the Config page's **Host** card, in `/gm status` and in
MCP `list_guilds`); nothing is deleted, and inviting the bot back serves it again at once. A Discord outage never parks
anything. Every other
command used in a server that isn't set up answers "This server isn't set up for oibot_GM yet". The bot can look
after **several** servers, each its own guild with its own data; when another one is claimed the host's superusers
(`OIBOT_SUPERUSERS`) get a DM, and every other guild's ops channel gets one line — their owners get no DM. The host's
superusers also get the Config page's **Host** card: the bot's invite link (with exactly the permissions it uses), the
servers it is in that aren't guilds yet, and the parked guilds. Keep **Public Bot** switched off in the Discord Developer Portal, so only you can invite the bot (any server
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
servers are looked after; to keep an existing companion as it is, give its server's token
the shared value. Never hand one server's raid leader another server's token.

**Moving a guild to another server** (rather than claiming the new server as a guild of its own):
`scripts/move_guild.py --to <new server id> [--key <guild>]` with the bot stopped (which stops every server it looks
after), `--dry-run` first. Members, characters, schedules, policy and history carry over; channels, card messages,
roles, the invite and per-channel plain-text settings are cleared to set again in the new server. Once the
several-servers update is live, don't roll the bot's code back to before it: buttons posted since carry their server in
a form older code doesn't read.

`/gm config owner [user]` in a configured server transfers ownership (first claim needs the Discord server owner or
Manage Server): pick the person (Discord's member picker), then **one confirm screen** — *"X becomes the owner of …
You lose owner rights here (you keep officer rights only if your Discord roles grant them). The site, MCP, plain text
and the error alerts treat them as the owner from now on; nobody is DMed."* — and *Transfer ownership*. Refused: a bot,
the current owner, yourself, someone not in the server (or a Discord lookup that can't say right now). The owner, or the
host's superuser, may do it — a superuser can hand over a guild they host but don't own (the screen then says *"you act
as the host here"*: they had no owner rights of their own to lose, and the ops line is marked *(host)*). Only in the
server, by design: never the site (its **Config** page shows who owns the guild), plain text or MCP — an MCP token acts
as the owner, so a transfer through it would give the guild away and silently change whose rights the token carries. Then
`/gm config ops-channel`, `registration-channel`, `analytics-channel`, `roster-channel`,
`signup-channel`, `absences-channel`, `applications-channel`, `news-channel` — each takes `channel:` (Discord's picker)
or `clear:` (stop using one; neither or both is refused) and shows **one confirm screen** first: what happens — the card
posted and pinned there and the old one removed, cards forgotten (not deleted), applications falling back to the ops
channel, *"Sheets will have nowhere to post until a signup channel is set"* — and *"I can't post in #x: missing Send
Messages, Embed Links"* when the bot lacks a permission there (then nothing can be saved until it's fixed). A card is
always posted in the new channel **before** the old one is removed, so a refused post loses nothing. `/gm config
channels` is all of them on one screen: each function with its channel (and a ⚠ line for one the bot can't post in),
**Change which?** → Discord's channel picker or **— not set —**, as many as you like as a draft, **Review** (every change
and what it does), **Save** (a change refused at Save stays in the draft with why). For the news channel the bot creates
the **news webhook** itself when it has Manage Webhooks there, and hands you its URL (privately) to point your news
source at; without the permission it says what to add. Every hour the bot re-checks each channel it uses: a channel
deleted or a permission taken away is one warning in the ops channel (and on the Setup card) — not repeated until it
changes; for the ops or signup channel, without which the bot can't tell anyone anything or open a run, it is an error,
so it also reaches whoever gets the error alerts. A change someone else made to the settings after you opened a
channel screen or the hub makes Save refuse that channel (nothing posted) and keep it in your draft. `loot-channel
channel: | clear: [log:]` (the public loot log, §9 — the same one confirm screen as the other channels, with the log
switch said on it), `officer-role role: [remove:]` (applies at once; removing the last one
says *"No officer roles now: only the server owner and Manage Server count."*), `officers` (Discord's role picker with the
current roles chosen, a **Map <name>…** button for an old role name the bot never found, Review, Save; @everyone and a
bot's or integration's own role are refused), `timezone` (five common zones as buttons, or a region then a city; it shows what
now and the next run become before saving; `about` likewise shows the text first — if someone changed the guild settings
meanwhile, Save says so and writes nothing) — or all of these on
the site's **Config** page — **Setup** (a checklist: owner, each channel, officer roles, the server-wide permissions
the invite asks for, the optional ones — Manage Roles to give accepted applicants the member role, Manage Channels to
make the registration channel read-only — the error alerts and the homepage invite; each ✓ is the same test the bot runs
before it posts, named when you hover it; *Open in Discord*), **Channels** (press **Edit**, pick per function or *— not
set —*, **Save** → one review of every change and what it does → the result; while Discord is unreachable editing waits
and the page says so instead of calling channels "not found"; *Webhook URL* beside the news channel), and **Settings**: press **Edit settings**, change any of the timezone, who may ask the bot, the officer roles and the
about text, then one **Save settings**. The **timezone** is picked from a searchable list grouped by region (type a city to
find it; never an IANA name to spell); under it the bot shows each raid's next run under the new zone and on today's clock,
and every run already open or locked whose time reads differently on the new clock (*"Barrow Deeps Tue 20 Oct 8:00 PM stays
at its real moment (now 11:00 PM in America/New_York) — move open runs with the clock to keep its time"*). Then you pick:
**Keep runs at their real moment**, or **Move open runs with the clock** — an open run keeps its 8:00 PM on the new clock,
same sheet, same signups, nobody asked again. A **locked** run never moves (its roster confirmed that moment), and while one
is affected the change only goes through once you've picked. `/gm config timezone` offers the same two Saves. A timezone or officer-role change is reviewed before
it is written, with those lines; the rest saves at once. Widening **who may ask** asks first: *widening this shares the
model budget with everyone who may ask*. The **about** text counts its characters against the cap and shows the homepage
card as it will look. Under Channels the **News** card keeps the news **keywords** (saved once when you leave the field),
shows the terms that are **always matched** (the game version's names and the raids'), the news channel's state (*Last item
kept … · N recent items kept*, or *Nothing received yet — is the webhook pointed at the news channel?*) and **Re-read the
channel** — and per raid
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
removes it. A paused schedule says *Paused — would next run Sat 24 Oct 8:00 PM · nothing opens* (never "Next runs"), and
changing the rosters per run says *From the next run: the sheet advertises 2 × 10 seats. The run already open keeps its 1 ×
10.* On the site: the Raids page's **Schedules** block under each raid lists them in words with the next three runs (a
paused one: *Paused — would next run … · nothing opens*); the owner adds, edits (weekday + 12-hour time pickers,
lockout-day picks; cadence fields left empty follow the raid; the **Standard / Short notice / Same week** presets fill the
cadence; under the form *With these rules* shows the next three runs the bot computes from the draft, or the refusal the
Save would give) and removes them. **Pause / Resume** sits beside Edit with one confirm (*"Nothing new opens from Alt run;
… is already open and keeps its sheet. Regulars stay."*). The editor saves only the fields you changed, so a pause made in
Discord a moment ago is never undone by a Save on the site. **Remove** confirms with what goes: *"Removes Alt run (Saturdays
8:00 PM). 3 regulars come off it (Ana, Bo, Cy); they see a notice on the Me page, nobody is DMed. 1 run already open from it
keeps its time and sheet (…), but locks with the raid's weights instead of the schedule's own and no regulars."* — the
same lines on the wizard, the review, plain text and MCP. If someone changed the raid meanwhile, Pause, Remove and the
editor's Save say so and write nothing; the page reloads. In plain text: `schedule_set` (e.g. "add an alt run on Saturdays at 8 to Barrow Deeps",
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
- **Discord invite**, the homepage's Join button. Paste one (`https://discord.gg/…`), or pick a channel under it:
  **Save recruitment** then makes a permanent, unlimited-use invite there and uses it (nothing is made before you save).
  It needs the *Create Invite* permission in that channel; without it you read *"Saved. Invite not created: …"* — the
  rest is saved and the channel stays picked. Clear the field to hide the button.
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
you Apply, each raid's next run on the new clock and on today's; with a run locked, add "keep the runs where they are"
(or "and move the open runs with the clock"). A setup change asked in plain words reads in the
proposal exactly as the site's review does: one line per thing it writes (two settings of one buff are one line),
each setting *current → new*, a part the bot would refuse marked ⛔, and what else happens marked ⚠ — and when a
plain-text permission change would stop the bot acting in the channel you are asking in, the line says so.

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
  first-roster-first" (balanced / first / rotation); "auto-fill the board for bd-…" (before lock); "remind the
  unconfirmed for tonight" (once an hour); "pause autofill on tonight's run" / "resume autofill"; "withdraw the ask
  to Kestrel"; "reopen bd-1209-1930" (an end by mistake, within the hour — never a cancelled run).
- *The board:* "move Ash to group 3" / "put Ash in 2.1" (roster 2, group 1) / "bench Ash" / "swap Ash and Dusk" —
  one person, like a drag (after lock: in from the bank = asked to confirm, out to the bank = seat freed); "groups for
  bd-…: Ash, Bright, Cinder | Dusk, Ember" sets the whole board — the layout the lock will use, or after lock the roster
  itself (names added are asked to confirm, names dropped are freed). "Propose a roster for tonight" (or "a rotation
  split") shows the solver's layout — groups, synergy, who is left out, why there can't be more runs, whether leftovers
  could make another — and leaves the board as it is; "another one" shows a different one, "use it" puts the last one on the board
  with its strategy in one step (what it showed you — also after a restart, and a preview you looked at on the site or
  with the lock's Preview; never one only another officer saw, and never one made before someone moved people on the
  board since: then it asks you for a fresh one). "Post the sheet here" works like
  `/raid sheet` in the channel you ask in (not in DMs): an officer's ask moves the live sheet there (its buttons and
  updates follow the new message); anyone else registered may ask too, in any channel where plain text acts on their own
  record, and gets a read-only copy — the live sheet stays where it is. The preview says which it will be. Pins stay as
  before ("pin Ash to the roster", "keep Dusk on the bench"); a seat on a run is someone's answer ("put Ash in for
  tonight": before lock they join, after lock they are rostered and asked to confirm) or a board move.
- *Members:* "add Jon's alt Jonny, Mage Frost" (value is `Class Spec [Offspec]`; "… as his main" makes it the main in
  one step), "plan Jon's main as a Holy Priest" (a planned character with no name; replaces an earlier plan, keeping
  rank and roster placement), "delete Jon's alt Jonny" (gone outright; the diff names what it does beyond the record —
  a seat it holds is handed back and the bench asked, an open sheet's answer cleared; retire keeps history), "Jonny can also tank" (extra roles; "none" clears them), "change Jonny to Fire"
  (every character change's diff says what it does beyond the record, as the confirm screen does: an answer that now
  reads Fire, a seat kept for the officers, a rank moving with a new main),
  "offspec for Jonny: none", "name Jon's planned Shaman Stormcall" (planned characters only — a named
  character is never renamed: retire it and add the new one; its class never changes either), "rank Jonny
  raider" (the member's rank — name their main; an alt is refused; a planned "Priest (Holy)" several members share
  needs the member named: "rank Jon's Priest (Holy) raider"), "make Jonny Jon's main", "retire Jon's alt Jonny"; "confirm Jonny", or "confirm everyone pending" (every
  unconfirmed character at once — the diff lists them; officers' requests are read against that list; a character
really called All or Pending is confirmed alone); "Mira is away
  Nov 1–3" (the diff lists the live sheets those days touch — No thanks, or a rostered seat freed — before anything is
  saved); "Marrow is back, clear her absence" (by start
  date when she has several — the bot lists them as days, e.g. "Fri 01 Mar → Sun 03 Mar, Mon 10 Jun"); "turn DMs off for Marrow" (off: her Confirm and fill asks wait on her Me page; she is still asked, in order).
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
  2 out 1" (a random mix of puppets who haven't answered; name the run when several test runs are live — a real run is
  refused), "test answer yes 3 no 1" once it is locked (that many Confirm and Can't make it among the puppets holding an
  open ask, as `/gm test answer` and the site do; *out* is No thanks on the sheet, *no* is Can't make it), "clear the
  test bench". Opening a test run by plain text needs the signup channel, or a server channel to
  ask in (§8a).
- *Loot council (officers, §9):* "open the loot council", "the Gatekeeper dropped the Robe of Tests", "distribute",
  "give the Syphon to Eltam instead — tanks progression" (a reason is required), "confirm the rest", "hold the Shard for
  later", "Mira got the wand because …" (the reason for an in-game award), "the robe should have gone to Kestrel —
  misclick", "end tonight's run", "retire the Syphon precedent — tier changed", "tier the Robe of Tests S for holy
  priests" (add "and re-weight its past awards" to re-weight them). *Wishlists:* your own (§3), or an officer's for
  someone ("put the Robe on Kestrel's wishlist").
- *Loot log (owner):* "post the loot log in #loot", "turn the loot log off".
- Several things in one sentence become several operations, applied in order ("lock tonight's run and turn
  DMs off for Marrow"). Standing availability is not settable anywhere; handing the guild to a new owner is a slash
  command only, by design.

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

**Reading it in Discord (D37).** `/gm config plain` (owner and officers) shows the policy as words: first where you
are — "Here (#raid-chat): Answers only — listed" (or *the default for other channels*, *the ops channel acts unless
listed*, the DMs setting in a DM) — then who may change what, then every other listed channel and "The site and MCP:
Acts (not configurable)". It is read-only: the table is edited on the site. Every plain-text refusal ends with
"`/gm config plain` shows the whole policy". On the site the table shows the site/MCP row greyed (it always acts; what
it may do is the person's own rights), and saving a table that would ignore plain text in the ops channel or in DMs
asks first.

**DMs with several servers (D41).** When the bot looks after more than one server, a plain DM asks which server it is
for, listing every server you belong to; one whose policy ignores DMs is marked "(plain text off here)" and picking it
answers once "DMs are off for <server> — ask in one of its channels instead (/gm change works there)" — the bot is never silent.

**What** — eight capability groups; every plain-text operation belongs to exactly one (your own wishlist is *Own
record*; someone else's is *Other people's records*):

| Group | Default | Someone could type |
|---|---|---|
| Own record — your absences, DMs, characters (add or plan, spec, offspec, name, main, retire, delete, extra roles) and your own answer or confirmation on a run; re-posting a live sheet here (it touches nobody's record, like `/raid sheet`) | registered members | "I'll be away Nov 1–3", "make Xanthe my main", "I can't make tonight", "post tonight's sheet here" |
| Other people's records — their absences, characters, DMs, ranks (yours too), confirming characters, deciding applications | officers | "rank Jonny raider", "Mira is away next week", "accept Nyx" |
| Runs — open, answer for someone, lock, cancel, fill, split, auto-fill, the board, pins (who the lock rosters), `confirm_for` | officers | "lock tonight's Barrow Deeps", "Xanthe confirmed for tonight", "pin Jonny in tonight" |
| Comp & policy — comp targets and group layout, the loot / comp / persona policy (add or remove a rule, replace, recompile), deciding news proposals | officers | "we want 3–4 healers in Barrow Deeps", "add a loot rule" |
| Raids & auras — raid schedules and settings, buff and family facts | the owner | "Barrow Deeps runs Tue and Thu 7:30 pm" |
| Guild setup — channels, officer roles, timezone, who may ask, the about text, recruitment (applications open/closed, note, questions, invite), starting the local jobs | the owner | "post raid sheets in #signups" |
| Test bench | officers | "seed 20 puppets", "clear the test bench" |
| Loot council — open, tick, distribute, award (override, with its reason), confirm, hold or discard, a reason or a correction for an award, end, retire a precedent, item tiers | officers | "give the Syphon to Eltam instead — tanks progression", "confirm the rest", "tier the Robe S for holy priests" |

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

The test bench is on both surfaces: everything below works in Discord (`/gm test …`) and on the site (the **Test
bench** card on the Members page), and plain text and MCP reach the same steps.

**Puppets.** `/gm test seed` asks how many puppet members to create (5, 10, 20 or the whole bench; fake Discord ids, a
realistic class mix and ranks, mains confirmed). `/gm test compose` builds the bench by composition instead: pick a
class, then a spec, then how many; repeat for as many specs as you like; the screen keeps the review line ("3 to add ·
2 to remove") and nothing is written before **Save** (removals take the newest puppets of that spec; one rostered on a
locked run is refused — cancel that run or clear the bench). On the site the card shows the bench as spec icons with a
count each (− / + to change it), the role totals, **Seed** 5 / 10 / 20 / all, and a class → spec picker with **Add**;
**Save test members** writes the difference. The bench holds at most 80; the members table hides puppets unless you
switch them on.

**A test run.** `/gm test run` is one screen: the raid (when there is more than one), a tempo (Quick, Standard or Slow)
and whether to DM every puppet when it opens, then **Open the test run**. On the site, **Open a test run** has the
same three choices. It opens a real sheet for that raid with minute-level cadence, so the unchanged scheduler goes
through nudge → lock → confirmations → expiry → fill → close within the hour. The sheet goes to the signup channel; with
no signup channel set it goes to the channel you used the command in. With neither — the site, MCP, a DM, plain text
on the site — the bot refuses before anything is written and says so: set the signup channel (Config › Channels or
`/gm config signup-channel`), or use `/gm test run` in a server channel. A signup channel that is set but the bot
can't reach (deleted, or hidden from the bot) is refused the same way, and the message says that is the problem. The
site's card says all of this before you press.
A test run never shares a real run's sheet, even when it starts the same minute as one: its key ends in `-test`
(`bd-1209-1930-test`), and the schedule id `test` is kept for it. A test run belongs to the officer who opened it:
a second officer opening one in the same minute is refused — "Tess opened a test run at Tue 9 Dec 7:30 PM — join
theirs or clear it" — and nothing is opened for them.

**Where its traffic goes.** The test run's officer cards (health, lock, the updates thread) post in **the channel you
opened it from**, marked 🧪 *Test run* — not in the roster channel (a run opened from the site or MCP uses the roster
channel, as any run does, still marked). The puppets never get DMs of their own: what each would have got (open, nudge,
Confirm / Can't make it, fill asks) is collected into **one DM per batch** to the officer running *that* test run —
"12 Confirm asks sent to puppets · 3 fill asks", the puppets' names, and an **Answer as puppets…** button that opens
the answer screen on that run. Two officers rehearsing at once each get their own.

**The feed.** The site follows a rehearsal live: the Test bench card on Members and the **Test** tab on Rosters (per
raid, shown while a test run is live; test runs never appear under Open or Locked) list every card posted, every
puppet DM (with what it said), every scheduler step (opened, nudged, locked, confirmations sent, expired, filled,
cancelled, thread lines) and every answer, newest first, as they happen. The feed is kept in the bot's memory only:
nothing of it is stored, and a restart empties it.

**Answering as puppets.** `/gm test answer` (or the digest's button, or **Answer as puppets** on the site) offers a
**Random mix** — on an open sheet how many Join, Bench and No thanks; once locked how many Confirm and Can't make it
among the puppets holding an open ask — or **One member** (pick a puppet, then Join, Bench or No thanks, or Confirm /
Can't make it when they hold an ask). It stays open so you can answer again; after lock a seated puppet's *No thanks*
is a callout. Puppets answer test runs only — naming a real run is refused on every path.

**Try a comp.** `/gm test comp` (pick the raid, then **Try a comp**) or **Try a comp** on the site opens a *comp
sandbox*: a test run two weeks out with every puppet joined, never posted in Discord; Discord answers with a link to
its board and the site takes you there, where **Propose roster** builds the best comp from that mix (Leftovers and
"what a second run would need" included). Change the mix and try again; the old sandbox goes with the next clear.

**Clearing.** `/gm test clear` shows what it does — "Cancels 1 test run, deletes 12 puppets, analytics cards
resume." — then *Clear the test bench* / *Back*; the site's **Clear the test bench** asks with the same words.

Every bench step writes one line to the ops feed, saying who did it and where: `[web]` from the site, `[mcp]` from MCP,
`[chat]` from plain text, no prefix from Discord. A restart sends a puppet digest that was still waiting before the bot
goes down. Only officers' pages follow the feed live.

Plain text does the same ("add 3 Warrior Protection, 2 Priest Holy to the test bench", "remove 2 Warrior Protection",
"comp sandbox for Barrow Deeps", "seed all test members", "open a slow test Barrow Deeps run and DM them", "test answer
join 8 bench 2 out 1", "test answer yes 3 no 1"), through the very same steps as `/gm test` and the site — and so does MCP (`test_bench`, §8b; its tempo is a name — quick, standard, slow — or minutes to
the start, at least 4: nudge, lock and the confirmation deadline each get a minute before it).

What is safe: a test run's pool is only the puppets and you (the tester) — real members are never nudged,
rostered or asked to fill it, and the test sheet refuses presses from real members. The other way round too: a real
run (ad hoc, or a schedule with no regulars yet, where every main is expected) never expects, suggests, asks or marks
away a puppet; setting a real member's answer on a test run is refused on every path too (the site, plain text, MCP),
and a test run refuses `/raid set`. A rehearsal opened before test runs had their own keys (`-test`) can still sit at a
real run's key: that real run doesn't open, and the ops feed says so once, naming the run and the fix — clear the test
bench (`/gm test clear`) and it opens on the next minute. What real members do see: the test sheet in the signup channel (marked as a
test), the ops lines, and the analytics cards pausing until the bench is cleared.

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

**Setup tools take typed values and can preview.** `raid_set`, `schedule_set`, `schedule_remove`, `raid_reset`,
`aura_set`, `family_set`, `aura_reset`, `set_channel`, `set_config`, `set_recruitment` and `set_plain_permissions` send
plain values — numbers, booleans, lists, objects (`slots: ["Tue 19:30"]`, `active: false`, `value: {"all": 3}`) — and the
bot builds the change, the same way the site's Save does. With `preview=true` nothing is written and the tool returns the
review: each write with its settings current → new, anything the bot would refuse with who may and where, the
consequences, and whether the site would ask for a review first. Read that to the person before writing.

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
| `list_guilds` | every server the bot serves, and which one a tool uses when `guild` is left out; `detached` = parked guilds (the bot was removed from their server — never a target) |
| `setup_status` | the Config page's Setup checklist (each row ok / warn / bad with what was tested), the invite's state, and the Host card (invite link, servers that aren't guilds, parked guilds) |
| `left_members` · `remove_record` | members who left the Discord server · remove one's record (officers; refused while they're in the server or hold a live seat) |
| `guild_overview` | timezone, owner, officer and member roles, news keywords, channels (a channel the bot can no longer see is marked missing), the policy documents (summary and text), raids' cadence, live runs, test bench, and `summary`: the configuration in words by section (as `config_summary`) |
| `list_runs` · `get_run` | live sheets per raid with upcoming slots · one run in full (recurring or ad hoc and its schedule; signups; `not_answered` = the regulars who haven't answered, and each regular's answer; board and its `rev`, confirmations, fill asks, callouts with their notes, absences that day, log; an open sheet's health: headcount, roles with who could cover, buffs nobody brings) |
| `list_raids` · `get_raid` | effective raid settings and overrides; each schedule's class (recurring / ad hoc), regulars count, where each inherited value comes from, seat weights |
| `set_regular` | add or remove a regular of a recurring schedule (member, character, schedule; the Rosters page's route) |
| `list_members` · `get_member` | everyone's characters, absences, asks (`filter` narrows) · one record, with its `attendance` (the sentence, the count the site shows, the loot factor's count — null while unknown — and a row per run) |
| `list_absences` · `list_auras` · `ops_log` | upcoming absences · the buff matrix · status, the ops feed, the registry change lines since the bot started, and the loot precedents (up to 50) and award log (up to 100) from the Loot pages, `member` narrowing both to one person |
| `get_loot` · `open_loot` · `loot_tick` · `loot_distribute` · `loot_discard` · `end_loot` | the councils (every locked run, or one run in full: drops by boss, picks with their candidate tables, pending overrides, awards, reasons owed, the roster, `batch_rev` / `drops_rev`, the End sentence) · open the council on a locked run · tick drops by name or id (`remove` un-ticks) · judge the ticked drops · drop pending picks (`batch_rev`, or `only` some = decide later; `preview`) · end the run and its council (`preview` = the sentence) |
| `loot_confirm` · `loot_award` · `loot_reason` · `loot_correct` | **decisions — need `OIBOT_MCP_ACTOR`**: record picks (one with `expect_to`, or the rest with `batch_rev`; `preview` needs no actor) · override a pick with a reason · the reason for an in-game award (an ended council too) · fix a recorded award |
| `loot_history` · `loot_log` · `list_precedents` · `retire_precedent` | ended councils (one in full) · the award log (`scores` for officers' columns) · precedents with their refs and the awards still needing a reason · retire or restore one (actor) |
| `list_items` · `tier_item` | the untiered items and one item's tier grid · set tiers per spec (actor); with past awards and no `reweight` answer nothing is written and the reply asks |
| `draft_tiers` · `export_tiers` | Claude's suggested tiers for one item — a proposal, nothing saved (confirm it with `tier_item`) · the whole tier grid as CSV text |
| `get_wishlists` · `set_wishlist` · `import_wishlists` | who wants each item / one member's lists with their `rev` · replace a character's whole list (actor; a list changed since `rev` is refused) · a ThatsMyBIS export (preview first; writing needs the actor) |
| `open_run` | open a sheet now (next slot, or a date and time — an upcoming run's exact time opens that run, any other time is a one-off ad hoc run; `schedule` = that schedule's next run, or a pickup template at `when`) |
| `schedule_set` · `schedule_remove` | a raid schedule's settings, one or several as `fields` in one write (creates it with its kind, slots or lockout days) · drop a schedule (schedules come back in `list_raids` / `get_raid`) |
| `set_answer` · `pin` · `confirm_for` · `fill_answer` | Join / Bench / No thanks for someone (character swap; `out` is *Can't make it* — a callout like `/raid out` — unless `callout=false`, which is a plain No thanks with no record; a `note` goes with a callout only and is for officers) · pin in/out for the lock · answer a Confirm ask for them · answer a fill ask (seat or swap) for them. The member gets a notice on their Me page, never a DM |
| `member_inbox` | a member's open asks (where each waits: DM or the site, its deadline) and notices, as their Me page shows them |
| `propose_split` · `set_strategy` · `autofill` · `set_layout` | preview a split (nothing saved; says why there can't be more runs and who is left over) · remember the strategy · solver shapes the board · set the groups (pass `rev` from `get_run`: a board someone changed since is refused, not overwritten) |
| `remind_unconfirmed` · `pause_autofill` · `withdraw_ask` · `reopen_run` | the Confirm / Can't DM again to the unconfirmed (once an hour per run) · pause (`paused=true`) or resume the fill engine for one run · close an ask already sent to a member (their card says never mind) · undo an end by mistake within the hour (never a cancelled run) |
| `lock_run` · `fill_seats` · `cancel_run` · `repost_sheet` | lock now · preview who would be asked (before lock the suggestions, after lock the seats to fill — `plan.seats` with alternates and a `why`, `plan.lines`, `plan.batch_lines` = exactly what a send DMs, `plan.rev`), then `send=true` with the preview's `rev` sends exactly that batch (`picks` {seat: option} for an alternate; a changed plan is refused; without a `rev` a send is refused before lock, and after lock whenever the batch holds a role swap) · cancel with a reason (nobody is DMed: every open ask card says Cancelled and a fixed line goes to the signup channel unless `notify=false`; the reason stays with officers) · post a fresh copy of the sheet (the signup channel, or a channel you name) |
| `raid_set` · `raid_reset` | raid settings — one, or several as `fields` in one write (slots, hours, the switches nudge/autofill/open_dm/confirm, weights, comp bounds, first open…) · back to defaults |
| `aura_set` · `family_set` · `aura_reset` | what the guild learns about buffs (one field, or several as `fields` in one write; `family_new` = a new family by name) and stacking families (`value` = the whole map as an object, `value:<key>` = one beneficiary; no `family` + a `name` = a new family, its id made from the name) |
| `add_absence` · `clear_absence` | away dates for someone (sheets answered / re-opened); `preview=true` writes nothing and names the live sheets it would touch and how · someone is back early |
| `save_characters` · `set_member` · `set_dm` | character table rows (a row's `flex` list sets its extra roles) · rank / confirm / main · DMs on or off — **a rank or a confirm needs `OIBOT_MCP_ACTOR`** (D8); a main change, a respec, retire and delete don't |
| `set_channel` · `set_config` | a bot channel by name, or `clear=true` to stop using one (one of the two; `loot` = the public loot log; refused when the bot can't post there; `preview=true` says what is posted, pinned or forgotten) · timezone (with runs open or locked, preview it, then add `timezone_runs`: `keep` or `move`), ask audience, about, officer roles (the whole list of ids or role NAMES, resolved by the bot; @everyone and bots' own roles refused; `[]` for none), news keywords, `loot_log` on/off, `public` (listed on the homepage and the public schedule, or not), `error_dm_ids` (who gets the error alerts; null = the owner, `[]` = nobody) — one or several as `fields` |
| `recruitment` · `set_recruitment` · `create_invite` | applications open or closed, the note, the invite, member roles (by name or id) and the questions · change any of them in one write (questions = the whole ordered list; the registration card follows) · a permanent invite to a channel you name, created and saved as the invite link in one step (`preview=true` shows it first) |
| `list_applications` · `decide_application` | open applications and the last 20 decided (character, schedules, answers, when) · accept or decline one by the applicant's name, character or id, with a note to them |
| `test_bench` | seed (`count=0` = the whole built-in roster; more than it is capped) / run (`tempo` = quick, standard, slow or minutes; refused without a signup channel) / answer (a mix of join/bench/out, yes/no on the puppets' open asks, or one `member` + `status`) / clear / compose / comp / feed (the test feed) / state, as `/gm test` and the site's Test bench |
| `policy_get` · `policy_edit` · `policy_append` · `policy_compile` · `policy_confirm` | a policy document (loot, comp, persona) in full, with what is applied and the waiting draft · replace its text (a draft, the persona too) · add one rule to the loot or comp policy as a draft (refused while another officer's draft waits) · the bot's reading of it (the persona: its text), nothing live yet · make that reading live, or `confirm=false` to discard the edit and restore the text — what `/gm policy edit` does |
| `plain_change` | text → the config ops it means, with current → new, and a `token`; `apply=true` with that token (within ten minutes, once) applies exactly those ops — never a new reading of the text — and refuses a board someone changed since the preview; like Discord's Apply, an op that fails is listed in `refused` with why and the rest still apply (`applied` is what landed, `error` the first failure) (everything plain text does, §8 — except re-posting a sheet "here": use `repost_sheet`) |
| `plain_permissions` · `set_plain_permissions` | who may act through plain text and where (groups with who, every channel's mode, DMs, the default, the server's roles) · change it (owner: groups, per-channel modes, DMs, default) |
| `raid_readiness` | each raid's pool readiness (§6): headcount, roles with who could cover, buffs nobody brings, the desired comp with its reasons, short / over cap, the pool plan — one raid or all |
| `set_comp_target` · `list_regulars` · `config_summary` | an officers' comp target for a raid (slot = role, class or Class:Spec; `min`, `max`, `note`; `clear=true` drops it; `preview=true` writes nothing — a target outside the owner's bounds is refused) · a raid's recurring schedules with their regulars, coverage and forecast (`schedule` = one) · the effective configuration in words by section, Host included (what `/gm config show` and the Config page's Overview print) |
| `list_news` · `get_profile` | the news kept since a time · the effective profile (buffs, families, raids, comp rules) with guild overrides marked |
| `post_proposal` · `list_proposals` · `get_proposal` · `resolve_proposal` | the news review's proposals (§7a): post one, list by state, one in full (with the consequence of each verdict), approve or dismiss it — a decision a person makes (D8): it needs `OIBOT_MCP_ACTOR` (sent as `X-Oibot-Actor`) naming an officer of the server, and a game-data approval needs that person to be the owner; the token alone is refused. The apply job's steps (applying · applied · failed · reverted, and its "waiting: …" notes) need the job's own `X-Oibot-Job` header, which only the job scripts send — the owner's token alone reports none, and a call with the job header never decides |
| `job_status` · `get_job_run` · `run_job` | the review and apply jobs: state, last and next run, launchd, recent runs · one run's steps · start a job now (refused when it is running or not installed) |

Members can be named by display name, character name or Discord id; runs by key, roster key or the raid's
name when it has one live run. An ambiguous name comes back with the candidates instead of a guess.

**Decisions are a named officer's (D8).** A decision about an award — confirming, overriding, a reason, a correction,
retiring a precedent, tiers, someone's wishlist — or about a person — a member's rank, confirming a character, accepting
or declining an application — is never
"the owner's token": those tools send `X-Oibot-Actor` with
`OIBOT_MCP_ACTOR` from `.env` (the Discord id of the officer at the keyboard), the bot checks that id is an officer (or
the owner) of the chosen server right then, and acts as that person: the record and the ops line read *"Mira via MCP"*.
Without `OIBOT_MCP_ACTOR` those tools are refused with a line saying so; reads, previews, ticks, Distribute, Discard
and End work without it, as do the owner's own setup writes.

**Safety.** The server acts as the **owner of the chosen server's guild** (owner-only settings included) and every
change is logged under the owner's own display name with an `[mcp]` prefix (`[mcp] Jon: signup channel → #raids`) —
the word "mcp" is never the name on a record. Keep `OIBOT_MCP_TOKEN` private — it is as sensitive as the bot token — and never
put it in `.mcp.json` or a commit. **Local only** (on by default): the bot accepts the token only from a call made on
the machine it runs on, which is where the MCP server runs. The same token arriving from anywhere else, through the
public site's tunnel included, is refused as if it were wrong, and the bot DMs the **host** (the superusers in
`OIBOT_SUPERUSERS`, or the owner when it serves one guild; once per 30 minutes) and writes it to its log — never the
token itself, and never a guild's ops channel — because it means the token has leaked: rotate it. `OIBOT_MCP_LOCAL_ONLY=0` in `.env` (then restart the bot) lets it work
remotely too; leave it on unless you run the MCP server on another machine. Browser logins are unaffected. Ask for a preview first (`propose_split`, `fill_seats` without send,
`plain_change` without apply) and read the returned line back: it says what actually happened. There is no tool
for handing the guild to a new owner, by design: the token *is* the owner, so a transfer through it would give the
guild away and change whose rights the token carries — that stays with a person: `/gm config owner` or the Config page.

## 9. Loot council

The council has **two surfaces that act on one council**: its **thread** in Discord (the moment — pressing Confirm at
8 PM on a phone) and **Loot › Tonight** on the site (the table — every candidate's numbers, many drops at once). Both
call the same council actions (so do plain text and MCP), both update the moment the other acts, and two officers can
never record one drop twice — the second one is told *"Already recorded (Mira, 8:14 PM)"*. A press made against a
picture that changed meanwhile (someone overrode a pick, a new batch arrived) is refused with what changed, and the
card or the page redraws so you can look again.

**How a pick is made.** Code scores every candidate for a drop — the item's tier for their spec × the slot's weight,
attendance, rank, wishlist, discounted by recent loot and by a second item in the same slot this week — and Claude
judges the top candidates against the guild's written loot policy, explains, and flags close calls. Officers decide.
Attendance nobody has tracked yet counts as **neutral** (it never lowers anyone's score). Attendance is the finished
raids in the loot policy's window (30 days by default) the member was rostered on — and, on a regular raid, confirmed
— out of the ones they were expected at or joined and weren't away for, every character of theirs together; it starts
counting once the window holds three finished raids (§3 *Attendance*). The site's candidate table shows the factor; the
Discord cards show only numbers.

### In Discord: the thread and its cards

**Open the council** on a locked run with `/raid loot`, the **Loot** button on the run's lock card (the raid-night
hub; it opens that card's own run, even when another run is locked too), **Open loot council** on Loot › Tonight or
under the locked run's board on Rosters (it becomes **Loot council**, a link to Loot › Tonight, once open), "open the
loot council for tonight" in plain text, or MCP `open_loot`. It lives in one thread under the officer channel (the
roster channel, else the ops channel); with neither set the bot refuses and says so. The channel gets one line *"⚔
Loot council · <run> → #thread"*. A run split into several rosters gets one council per roster (thread *"loot · Roster
2 · …"*; one block per roster on the site), each judging only its own roster's seats. A test-bench run opens a
**rehearsal** (*"rehearsal · …"*): everything works, nothing reaches the guild's loot history, the award log or the
public loot log — rehearse with a test run (§8a). If Discord can't be reached when you open it, the council opens
anyway and its thread is made as soon as Discord answers (or on your next `/raid loot`); the site shows *thread
pending* meanwhile. Running `/raid loot` again on an open council posts a fresh card at the bottom of the thread and
re-posts every pick still waiting on fresh cards.

**The council card** is the thread's first message, pinned, and always current: the roster in its groups (one member
per line, the spec icon alone), the drops per boss (▫️ ticked · 🟡 proposed · ✅ recorded), one status line (*"3
proposals pending · 1 waiting for a reason"*, *"Judging 6 drops… 4/6"*, *"waiting for drops"*, the **scores only**
banner), the companion chip (🟢 live · 🟠 quiet · 🔴 disconnected, with when it was last heard), anything that needs an
officer (an award waiting for its reason, an in-game award that disagrees with the record — *Give a reason or
correct…* picks one) and anyone who left the roster (*Remove a candidate…*). Its buttons: **Tick drops… · Distribute ·
Scores only · End council**.

**Tick drops…** — pick the boss (or *A boss not listed…* and type its name), then what dropped from its loot table,
25 to a page, several at once; an item the table doesn't know — every item on WoW: Forever's first night — goes in
with **Not on the table…** (type its name, pick how many dropped). Then the number of copies, then **Tick**. Every drop
is its own line, even two copies of one item. Such an item is recorded **by name**: everyone rostered is a candidate,
ranked by attendance and recent loot only, and the item is remembered so it can be tiered later on Loot › Items (the
card counts the untiered ones). **Un-tick…** removes a drop not yet recorded; a recorded one is fixed with Correct
instead.

**Distribute** judges the ticked drops nobody has proposed or recorded yet and adds a batch of proposals (earlier
picks still waiting count as if given); `/raid distribute` does the same. You are told about how long it takes and the
card counts the judging. With the companion connected it starts by itself about 20 seconds after a boss's last drop (a
per-raid switch, on by default) and the card hides the button while that covers everything. Only one Distribute runs
at a time per council — a second press says *"Distributing already (Jonny, 8:12 PM)"*. If the bot restarts
mid-judgement the thread says *"Distribute was interrupted at 8:12 PM — press Distribute"*, with the button; it never
re-runs on its own. **Scores only** (a toggle, named on the card) picks by score with no model call — the picks say so
— and is also what happens by itself when Claude is unavailable or the budget is spent.

**The proposal cards** follow, at most five items to a message, grouped by boss: each item says *"#2 Syphon → Kestrel"*
with its spec icon and one line of why, and marks ⚠ an override waiting for Confirm, ≈ a close call, ⇄ a pick that
depends on the order, ✎ scores only, 🆕 untiered. Each item has **Confirm** (records the pick exactly as you were
shown it), **Override…**, **Decide later** (nothing recorded; the drop waits, shown *held*, and Distribute judges it
again) and **Details**. The last card ends with **Confirm the rest (N)** (every pick still waiting) and **Discard
batch** (asks first: the ticks stay, pending overrides go with the proposals, nothing was recorded so nothing is
undone). The cards are edited as things change; when new proposals arrive the old cards say *"superseded — the current
proposals are below"*, so the buttons you need are always on the newest messages. A press on something that changed
since you looked is refused with what changed — *"The batch changed (Jonny overrode the Syphon at 8:13 PM) — look
again"* — and the cards are redrawn; nothing is recorded that you didn't see.

**Details** opens a card only you see: the candidates (spec icon, character, tier, score), the recommendation and its
reason, alternates and warnings — and **Confirm**, **Override…**, **Disenchant** (nobody gets it; a reason, recorded at
once) and **Why not…** (pick someone: the bot compares them with the pick from the numbers — no guessing). After a
press that card shows what happened (*"✅ recorded by you · 8:14 PM"*).

**Override…** is two presses: pick who gets it instead — *In the table* (the top candidates), *Rostered tonight*,
*Disenchant*, or *Someone not on the roster…* (type their character's name, first and last if they have both) — then
write the **reason** (required) and submit. A typed name is checked against the run's signups, the guild's registered
characters and anyone recorded before as a guest, and is recorded in their spelling; if nobody has that name the bot
says so and offers **Record <name> as a guest** — a second, deliberate press, so a typo never becomes an award. An
override is shown as pending (⚠, with who set it) and is **not recorded until Confirm**; the reason becomes a precedent
the bot cites next time only when the award is confirmed. If another officer already set that drop, you are asked
whether to replace theirs (**Replace their override…**); your own you can simply change. An override re-scores the
other picks; while that runs, **Confirm the rest** waits (*"Re-scoring 2 item(s) — Confirm the rest waits"*) —
confirming one item at a time still works; a pick you held or discarded while a re-score ran stays held or gone.

**Roster changes** after the council opened count at once: someone who joins the roster is a candidate straight away
(a thread line says so); someone who leaves stays a candidate until an officer removes them (*Remove a candidate…* on
the card, or ✕ beside their name on the site) — on a split run only on their own roster's council.

**Precedents.** `/gm precedents` lists the latest active precedents (what the bot picked, who the council gave it to,
why, who decided, when); pick one and **Retire…** with a reason — it stays on record, and the bot stops citing it.
Restoring one is on the site (Loot › Precedents), in plain text ("restore the Syphon precedent — …") or over MCP.

All buttons survive a bot restart (a reason box you opened before a restart still works when you submit it). Every
award, override, reason, correction and retirement is an ops line with the officer's name.

### The companion

The companion is a small program an officer runs next to the game: it reads the game's chat log and reports drops,
kills and in-game awards to the bot (its token is set up in §8). When the master looter hands an item out in game, the
bot matches it to one copy of the item: the copy whose pick names who got it (two copies of a token can be handed out in either order), or a copy an
officer already confirmed to them (never recorded twice). To the pick shown (the override if there is one) → recorded.
To someone else → **recorded at once** (the game is the truth) and the thread line carries two buttons — **Give the
reason** (it becomes the precedent) or **Mistake — correct…** (it was a misclick: the record is fixed, no precedent);
the site lists the same under *Waiting for a reason or a correction*. A drop nobody distributed is recorded straight
away — *"[item] → Kestrel · recorded"* with **Wrong? Fix…**; a sub who isn't registered is recorded by name. A reason
given twice asks before replacing the first.

**Correct…** (and *Mistake — correct…*, *Wrong? Fix…*, or **Correct…** beside the award on the site) is the same two
presses as Override: who it should be recorded to, then what went wrong (the old record is kept, marked void). A
correction also retires the precedent of the award it fixed — the bot never cites a council decision that, per the
correction, never happened — and an award whose reason was given (even if that precedent was later retired) never goes
back on the "needs a reason" list. A reason owed and an in-game award that disagrees with the record both stay fixable
after the council ends (Loot › Tonight lists them under *Still owed from ended councils*).

**Which council it feeds.** A companion's events go to its own server's live council (each server uses its own token,
`OIBOT_FEED_TOKEN_<KEY>`, see §8); tonight's council comes first — a council kept open past its raid's end (a pick
still waiting) never takes the companion's events — and when several are live it picks the one whose roster has the
companion's character. When it can't tell, it refuses and says why, and every live council card shows **Bind
companion…**: pick it on the right council and its drops and awards go there from then on (until the bot restarts; the
card offers it again if needed). The companion keeps a refused event and sends it again every 30 seconds for 10
minutes, so it lands once you bind it; it also re-sends an event the connection cut off before the bot answered
(never applied twice). **Two officers running the companion** in one raid see the same chat lines: a drop or an award
another companion of that council already reported (same boss and items, or same recipient and item, within about 30
seconds by the game's clock) is listed in the ops channel as a duplicate and recorded once — while two copies of an
item stay two drops.

**No run live.** Drops arriving when no run is live are held for 10 minutes (a restart keeps them; the site shows them
waiting) and the officer channel (roster, else ops — never the public loot log) asks once *"Drops arriving from Jon's
log but no run is live — start an ad hoc run for tonight?"* with **Start an ad hoc run…**. Held drops go into the
council of **their own run** when it opens (one opened near the run's time; never a council days early) — an event
that arrives just after a council ended stays with that ended run and is never replayed into the next raid's council,
and one a council refuses stays held. Anything still held after 10 minutes is listed in the ops channel as **not
recorded** (naming the ended run when there is one), so you can add it by hand. An in-game award that arrives shortly
after a council ended reaches the officers as **one DM** with a link to record or correct it on the site — the
council's only DM (never for an ended rehearsal). A rehearsal takes drops only from a puppet's or the tester's
companion. The companion shows as **live**, **quiet** (no events for a couple of minutes — normal during trash and
breaks) or **disconnected**, in the thread and at the top of Loot › Tonight.

**Kills and stamps.** The companion sends every event's time as an instant (it reads the chat log's clock on the PC
that wrote it), so the thread's *"☠ Gatekeeper down (8:15 PM)"* shows in each reader's own time zone. Each kill counts
toward **Bosses 3 / 6** on the lock card, the run's card on the Rosters page and Loot › Tonight, and a killed boss's
drops are judged first. When the raid's **last boss** goes down the thread says once *"All bosses down — End the run?"*
(on a split run, each roster's thread — once every roster's last boss is down) and the lock card shows the same
line with its End button reading **End the run** — the same End as always (the confirm screen first, then the run and
its council close). A raid whose bosses the game profile doesn't list yet shows *Bosses 3* and never asks. A companion
older than the one in the repo sends kills with a clock time only; the bot refuses those (it won't guess which time zone
the raider's PC was in) — the companion prints *"companion too old"* and the ops channel says so once. Update the file
from the repo's `companion/` folder.

**Loot › Companions** (officers) lists the companions feeding your server — live / quiet / disconnected, last seen,
events. The owner also sees which feed-token settings the host has (`OIBOT_FEED_TOKEN_<KEY>` for your server, the
shared `OIBOT_FEED_TOKEN`, the listen address) as **set / not set** — never a token's value; tokens live in `.env` on
the host.

### Asking the bot in the thread

Only messages addressed to it count — an @mention, a message starting with **?**, or a reply to one of the council's
cards; everything else in the thread is the officers' own chat (a reply to a companion line too). A question (*"? why
Kestrel over Mira"*) is answered from the proposals and the guild's data. A request (*"? give the Syphon to Mira, she's
our main tank"*, *"? confirm the rest"*) comes back as the same change list plain text shows (*"Syphon (d3): bot picked
Torsha → council awards Mira — “she's our main tank” · recorded at Confirm"*) with **Apply** / **Cancel**: nothing
happens before Apply, and names are checked against the candidates and tonight's roster. If you gave no reason, or the
bot had to guess it, the line asks **why?** (with its guess) and Apply waits until you press **Give the reason…** and
write it — the reason becomes a precedent, so it has to be yours. Apply records exactly what you were looking at when
you wrote: a pick that appeared or moved since (another officer's override, a new batch) is refused with what changed,
and another officer's override is never replaced from a sentence. Requests follow the plain-text permissions (§8c) of
the channel the thread sits in and the **Loot council** group (officers by default): where plain text may act there you
get the change; where it only answers questions the bot says so and points at the card's buttons. Apply checks again
for whoever presses it. When the bot can't read sentences (Claude unavailable) it says so and the buttons keep working.

### End

`/raid end` (with several locked runs it asks which, the one with a council first), **End council** on the card or on
Loot › Tonight, "end tonight's run" in plain text, or MCP `end_loot` first shows what ending means — *"End <run>: 6
awards, 1 override, 2 drops never distributed, 1 award waiting for a reason"* — with **End** / **Not yet**. End posts
the summary in the thread (the awards, the overrides and anything left undecided — proposals never confirmed, drops
never distributed, awards still waiting for a reason — so nothing anyone decided disappears), turns the card into the
summary, archives the thread, marks the run done and redraws its sheet and lock card closed. On every surface only a
locked run ends (*"No locked run to end."*), and not while a Distribute is judging (*"Distributing already (…) — wait
for it, or retry End"*). The bot ends a council by itself only after the run's scheduled end, once it has been idle
for 30 minutes with no pick waiting for Confirm and no drop nobody judged — after one *"closing in 15 min"* notice with
**Keep open**. If picks or drops still wait, it says so once in the thread and leaves the council open for an officer;
the raid itself is over either way (no more fill asks, nobody shows as rostered on it).

### On the site: Loot › Tonight, History, Precedents, Items and tiers, Wishlists

**Tonight** is the same council as a table, live: every locked run (with **Open loot council** until it has one), its
drops per boss (the *Drops* block ticks by item name, a boss and how many copies; un-tick a false drop), every proposal
with **Confirm** / **Override…** / **Decide later** — a row opens its whole candidate table (tier, upgrade, attendance,
rank, wishlist place, recent loot, score; no model call to browse it) — **Confirm the rest** and **Discard batch**,
**Distribute**, **Scores only**, **End council**, the awards *Waiting for a reason or a correction* (**Give the
reason…** / **Mistake — correct…** / **Correct…**), what is *Still owed from ended councils*, the companion chip and
drops held with no run live. What you do there edits the Discord cards; what an officer presses in Discord shows there
at once. Opening the council from the site makes the Discord thread the same way `/raid loot` does.

**History**: every ended council, newest first, with its awards, who recorded them, its overrides and its summary.

**Precedents**: every council override with its reason, newest first — what the bot cites next time. **Retire** one (a
reason is required and stays on it; the bot stops citing it, the record keeps it) or **Restore** it — a precedent whose
reason was replaced is retired by its newer reason (the one the bot cites). *Needs a reason* lists awards recorded in
game against the bot's pick that still have none; retiring a reason never puts its award back on that list. Retiring
here, with `/gm precedents`, in plain text or over MCP is the same action.

**Items and tiers** (Loot › Items): the **untiered** items — learned from drops by name, or on the tables without tiers — with when
each was first seen, how often it dropped and how many awards it has. A row opens the **tier grid**: one tier per spec
(S+, S, A, B, or – for none), the guild's over the game profile's; specs that can't equip the item are greyed. Saving
tiers for an item that already has awards asks *"N past awards used the old weight — re-weight them?"*: **No** (the
default) keeps them as they were; **Re-weight them** changes their weight in recent-loot scores only — who got what
never changes. Either answer is logged. The grid is on the site only (a grid doesn't fit Discord); "tier the Robe S
for holy priests" works in plain text, `tier_item` over MCP.

**Draft with Claude** on the grid asks Claude to suggest tiers for that item from what is known about it (slot, armour
or weapon type, boss, note), your specs and your loot policy. The suggestion fills the grid marked **draft**
(*"drafted by Claude, unverified"*), with a line on who it's for and what Claude couldn't tell; anything it named that
isn't one of your specs, or a spec that can't wear the item, is left out and listed. **Nothing is saved**: change any
cell, then **Save** — the ordinary save, with the re-weight question — or close the draft. A draft is forgotten when the
bot restarts. When the bot can't reach Claude it says so and the grid works by hand. MCP `draft_tiers` returns the same
suggestion; only `tier_item` saves.

**Export CSV / Import CSV…** move the whole grid in and out of a spreadsheet: one row per item (item_id, item, boss),
one column per spec (`Priest:Holy`; a whole class `Priest:*` or a role `role:tank` only when some item has one). In the
file a tier sets a cell, an empty cell leaves it as it is, and `-` clears a tier the guild set. **Preview** shows what
would change per item, rows that name the same item twice (merged — the later row wins), items the council doesn't know
(skipped until they first drop) and cells that aren't a tier; **Import** writes exactly the preview you saw, in one go — refused with
*"Tiers changed since this preview"* if someone saved the grid meanwhile — and asks the same re-weight question when an item
already has awards. MCP `export_tiers` returns the file. An item or boss name that starts with `=`, `+`, `-` or `@`
is written with a leading `'` so a spreadsheet shows it as text instead of running it as a formula; the import reads it
back as the same item.

**Wishlists**: who wants each item most, and every member's lists; **Import from ThatsMyBIS** takes a TMB wishlist
export (preview first; per character in TMB's order; an item already on someone's list stays as it is; a re-import
replaces only the earlier TMB rows; nothing made on the site is ever deleted).

**Appeals**: members' questions about their own awards (*Ask the council*, §9 *Members*), open ones oldest first, then
the resolved. Each shows the award, the member's words as written, the council's record of the award (how it was
decided, the bot's pick, who recorded it, tier and weight, the candidates then and the reason on file) and a link to its
thread. **Resolve…**: pick one outcome (*The award stands* · *Corrected* · *Noted for next time* · *Withdrawn*) — the
confirm says exactly what the member will read and takes an optional note that stays with the officers. In Discord the
card's **Details** shows the same record to the officer who pressed it only, and **Resolve…** is the same choice and
confirm. The member gets a notice, never a DM; the card is edited to *Resolved* and the thread archived. To change who
got the item, use **Correct…** as usual and resolve the question as *Corrected*. Over MCP, resolving needs the
`X-Oibot-Actor` header (an officer's decision about a person); MCP never opens one.

### Members: your loot and your wishlist

Every logged-in member sees **Me › My loot** (what your characters received: item, date, main or offspec) and the
**Loot log** page (every award the council recorded: item, character, boss, date — never scores, the bot's pick,
reasons or who recorded it; officers can switch the scores on). `/me view` shows your last ten awards, and asking the
bot about your loot ("what did I get this month?") answers from your own awards only. Your **wishlist** is per
character, up to 10 items, in the order you want them, each main spec or offspec with an optional note: build it on
**Me › Wishlist** (Edit, add items by name — an item the tables don't know yet is kept by name — reorder, one
**Save**; if the list changed meanwhile, in another tab or in Discord, the save is refused with the current list and
your edits kept), with `/me wishlist` in Discord (pick the character, then an item from the list or *Search by name…*,
then its place and MS/OS, Confirm), with **Add to my wishlist** under a line of the public loot log, or by telling the
bot ("put the Robe of Tests on my wishlist as #2 for offspec"). An item you received stays on the list, greyed
(*received 9 Dec*), until you remove it. The council's scoring reads these lists.

**Ask the council** about one of your own awards (a question, a disagreement, "I was told I'm next for this"): on
**Me › My loot** press the question mark beside the award, write it in your own words (10 to 1000 characters), and
**Confirm** — the box says what happens first; in Discord, `/me loot-ask` → pick the award → write it → **Send to the
council** (nothing is sent before that; **Edit my words** goes back). One question per award, and it can't be edited
once sent. It goes to the officers as a card in their channel (the roster channel, else the ops channel) with a thread
named *loot · <you> · <item>* for them to talk it over: the card shows the award as the loot log does and your words as
written — never scores or reasons. No AI reads or answers it. When an officer closes it you get a notice on your Me
page and in `/me view` with one of: *The award stands* · *The award was corrected* · *Noted for the next drop* ·
*Closed at your request* — never a DM. My loot lists your questions and where each stands. Someone else's award, or one
on someone's behalf, can't be asked about: an officer who thinks an award was wrong corrects it.

### The public loot log

With the log on (the default) every recorded award gets one line in the **loot channel** (*"🎁 Robe of Tests →
Kestrel · Gatekeeper"*; a correction *"✎ Robe of Tests: was Kestrel, now Mira"*; a disenchant *"→ disenchanted"*),
with an **Add to my wishlist** button — and nothing else goes there (no officer prompts, no reasons, no scores). With
no loot channel set the lines go to the run's updates thread — never the signup channel — and the first one says so in
the ops channel and the council's thread. With the log off nothing is posted; the site still shows every award. A
rehearsal posts nothing. The owner sets it with `/gm config loot-channel` (a channel or `clear`, and `log` on or off —
one confirm screen saying what changes), on the
Config page (the *Loot log* card shows which of the three states it is in), in plain text ("post the loot log in
#loot", "turn the loot log off") or MCP `set_channel("loot")`.

### Plain text and MCP

In the ops channel (or `/gm change`, or an @mention where plain text acts) an officer can say "open the loot council",
"the Gatekeeper dropped the Robe of Tests and a Shard of Doom", "distribute", "give the Syphon to Eltam instead — tanks
progression" (refused without a reason), "confirm the rest", "hold the Shard for later", "Mira got the wand because
she's on her last piece" (the reason for an in-game award), "the robe should have gone to Kestrel — misclick", "end
tonight's run" and "retire the Syphon precedent — tier changed": each is shown as a diff with **Apply**, and Apply
refuses a "confirm the rest" whose batch changed, or a confirm or award whose pick moved, since the diff. These are the
**Loot council** group of the plain-text permissions (officers by default, §8c). Over MCP the owner's token reads the
council, ticks, distributes, discards and ends; **decisions about an award** — `loot_confirm`, `loot_award`,
`loot_reason`, `loot_correct`, `retire_precedent`, `tier_item`, setting someone's wishlist — are made by a named
officer: `OIBOT_MCP_ACTOR` in `.env` (that officer's Discord id) rides with them, the bot checks the id is an officer of
that server, and the record and the ops line read *"Jon via MCP"*. Without it those tools are refused and say so; only
the true previews (`loot_confirm` and `import_wishlists` with `preview`) need no actor (§8b).

Every award, override, reason, correction, retired precedent, tier change and wishlist change is an ops line with the
officer's (or member's) name — a companion's own records read *"[companion] companion:<character>"*; members never see
scores, alternates or reasons. Nothing about loot is on the public homepage.

## 10. Data, privacy, cost

Guild state lives in a private git repository (one commit per change, so every edit has an author and a
time). The ops channel's audit lines read the same whatever the surface: the person's display name, then what
changed, with a prefix for where it came from — `[web]`, `[mcp]` or `[chat]`, nothing for a Discord command — and
" (host)" after the name when the bot's host acted in a server they don't own. Who you are is decided the same way
everywhere (a command, a button, a DM, the site, plain text): the server's owner, an officer by Manage Server or an
officer role, a member of the server, or outside it; a Discord nick change never costs you anything — your seat on a
locked roster, your callouts and your pending asks are tied to your Discord account, not to the name, and the name
on your record follows your server nick the next time the bot sees you in the server (a command, a button press) —
a sheet keeps the name you answered under until you answer again. An absence reason is seen by you and
the officers, nobody else. The bot DMs only when you must act — sheets, nudges, confirmations and fill
asks (a released seat, a cancelled run, an ask that expired, closed at the lock or closed by your own No thanks /
callout / absence edits the card you already have to say so — no buttons left, nothing to do; everything else you
should know is a notice on the Me page and in `/me view`); switch DMs off on the **Me** page or with `/me dms off` if you'd rather not
get them — with DMs off, Confirm and fill asks wait on your Me page and the sheet; nothing is sent anywhere else (you
are still asked, in the same order, with the same deadline). **My absences** on the absences card lists yours with a **Clear** button each; clearing
an absence puts you back to unanswered on any open sheet it had pre-filled (a seat freed on a locked run is
not handed back automatically — ask an officer). Claude is used sparingly — never for registry, signup or solver work — and spend is
capped by a monthly budget the owner sets (`OIBOT_BUDGET_USD`, per calendar month, kept across restarts); questions and
plain-text requests may use at most half of it each, so they can't spend what loot night needs. `/gm status` shows
this month's spend. The budget is one for the whole bot: if it ever looks after several servers, they share it (D45),
and the owner's Agents page shows each server's part of the month's spend. When it is used up, the bot says "The bot's
monthly budget for Claude is used up; the host raises it. Slash commands, the sheets and the site keep working."

## 10a. Asking the bot questions

**Two servers?** When the bot looks after more than one Discord server and you are in more than one of them, a DM to
the bot — a question, a request, a slash command or a wizard used in DMs, `/help`, `/ask`, applying — first asks
**which server it is for** (one button per server). It asks every time; it never assumes. Pick one and the bot carries
on with what you asked, for that server; nobody picked in two minutes and it says so — ask again any time. If you are
in only one of them, it is that one, no question (a server you just joined or left counts within seconds). A server
whose owner turned DMs off is still listed, marked "(plain text off here)"; picking it answers once "DMs are off for
<server> — ask in one of its channels instead (/gm change works there)" — never silence (§8c). Buttons the bot sent you (Confirm / Can't make it, a seat offer, Join
on a sheet, Apply to join) already know their server and never ask. With several servers, DMs and cards start with the
server's name.

**The manual on the site.** The **Help** page shows this whole manual section by section (a section for officers or
the owner is marked, never hidden — the manual isn't secret, the data is) and the commands you may run; on a phone the
sections are a picker. `/help` ends with a link to it, and `/help topic:` ends with a link to that section — a long
section stops after five messages with "continued on the site".

`/ask`, a DM, an @mention outside the officer channels, or (officers) the site's **Tell the bot** box (§2a) gets a free-form answer from this manual and
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
review jobs with their last three runs), **loot** (the last precedents with their reasons and the last awards, or a named
member's; "why did we override …" brings it too — a member asking about loot gets only their own awards), **bank** (the unconfirmed characters first, then every member's characters and rank, who is
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

Loot: a ThatsMyBIS import has no Discord path (the Loot › Wishlists page and MCP `import_wishlists`); the item tier
grid, its CSV import and Claude's tier drafts are on the site only (MCP: `draft_tiers`, `export_tiers`); importing a
loot history from another addon (Gargul, RCLC, BisCouncil) is not built yet; a Claude draft is forgotten on a restart;
the member's "ask the council about an item" thread
doesn't exist yet; a companion bound to a council with **Bind companion…** is forgotten on a restart (the card offers it
again). Splits: the **Loot-quality** split philosophy is a greyed placeholder, not built yet (Balanced, Raid one first
and Rotation work). Buff facts, comp bounds, cadence and split policy *can* be changed (Auras page, Raids page,
`/gm config raid|aura`, plain text); the buff list itself and the item tables are files in the code
repository. Regulars are a list of who is expected, never a standing answer: there is no standing availability or
standing signup to set: each run is answered on its own sheet (or by telling the bot, or an officer for you). If you ask for one
of these, say so plainly.
