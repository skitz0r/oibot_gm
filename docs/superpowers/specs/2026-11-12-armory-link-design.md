# Armory link: real recipes, linked characters, a synced record — design

Date: 2026-11-12. Status: written from the owner's decisions in chat, read by a builder-check reviewer; awaiting the owner's approval.
Design home when built: `docs/design.md` §5.64. Research: the live probe of 2026-11-11/12 (§9 below).

## 1. Why

WoW: Forever launched on 2026-11-04 and Blizzard's API for it is live (undocumented). It gives, with the app
credentials the bot already holds, a guild's roster and each character's professions with every known recipe. Today
the bot's professions are typed by hand and its recipe catalog is whatever people happened to type: one member's
record said Leatherworking 265 with three recipes while the armory says 300 with 103.

The owner wants the armory to keep records current **without** creating a second set of records and **without**
making guild membership a condition of anything. A fill-in from outside the guild registers and raids like anyone
else.

## 2. Decisions (the owner's, 2026-11-11/12)

- **D1 One guild or none.** A Discord server is linked to exactly one in-game guild or to none. The test server has
  none and works as today. A guild is identified by more than its name: "Oi" resolves to a stranger's guild.
- **D2 One record.** Discord registration stays the only way a member or character record comes to exist. The armory
  never creates one. It adds facts to a character somebody registered.
- **D3 Linking is optional.** Registration is unchanged and never gated on the armory. A character MAY be linked to
  its armory character. Nothing requires the character to be in the guild.
- **D4 A linked character is overwritten.** A sync overwrites what the armory knows better; no suggestions, no merge.
- **D5 Profession sync is a per-character switch.** On by default for a linked character. Off = that character's
  professions are hand-edited again. An unlinked character is always hand-edited.
- **D6 Recipes are game data.** The definitive recipe list replaces the catalog derived from typed names.
- **D7 The in-game rank is ignored.** Discord roles stay the authority for officer / member, as today. The bot's own
  per-member rank (loot scoring) stays an officer setting.
- **D8 Automatic link on a sure match.** A registered character whose first name and class match exactly one roster
  entry is linked without asking. Everything else is linked by a person.
- **D9 Officers see the map and can fix it.** The user → character → armory map is visible up front. An officer can
  move a character to another Discord user when a claim was wrong, and link / unlink for anyone.
- **D10 A change check, not a re-download.** No event stream exists. Linked characters are checked hourly with
  "changed since"; the roster every three hours.

Calls made by the designer (the owner has seen them in chat, not objected; flagged so they can be overruled):

- C1 A sync writes professions (D5), level and "in the linked guild". It never writes spec, main / alt, rank, absences.
- C2 A class that differs from the armory is flagged to officers, never overwritten.
- C3 Leaving the guild changes nothing but the "in the guild" fact. Nobody is DMed about any sync (DM rule); a sync
  writes no notice either.
- C4 Linking adopts the armory's name and surname as the character's name (see §5.2): 14 of the 27 characters
  registered before launch carry names that are not their in-game names.
- C5 Gear, talents, stats, reputations: out of scope. Nothing here should make them harder later.
- C6 Hand entry (unlinked, or sync off) picks from the real list; free-typed recipe names are no longer stored.
- C7 Rename… / Merge… of recipes are retired.
- C8 Ownership of an armory character cannot be proven without a Battle.net login, which is out of scope. The guards
  are: one armory character links to at most one record per guild, the map is visible, officers remap.

## 3. The words (Copy rule)

**Armory** — Blizzard's public page for a character. **Linked** — the character's record follows its armory page.
**In the guild** / **not in the guild** — what the roster says. **Not claimed** — a guild character nobody has
registered. **Kept by hand** — professions with sync off. Never "sync", "slug", "namespace", "ruleset" where a
person reads; a person reads "Updated from the armory <when>" (`Registry.local12`).

## 4. Phase 1 — recipes as game data

Independent of linking; ships first.

### 4.1 The list

`profiles/forever/recipes.yaml` (Game version is data): every recipe with `id` (Blizzard's), `name`, `profession`
(the profile's profession id), `category` ("Leather Boots"), `makes` {item id, name, quantity} and `reagents`
[{item id, name, quantity}]. 2,494 recipes across 12 professions at build 1.60.1_70399; "Test Profession [DNT]" is
dropped. `profiles/forever/professions.yaml` gains each profession's Blizzard id.

A script (`scripts/import_recipes.py`, the importer in `importers/blizzard.py`) regenerates the file from
`static-forever-<region>` (profession index → profession → skill tier → recipe). It is run by the operator after a
game build, reviewed as a diff and committed; the bot never fetches the list at run time. A profile without the file
(tbc) keeps today's typed recipes untouched: every behaviour below is gated on the profile having a recipe list.

### 4.2 The record

`Recipe.id` (empty today) becomes the identity; `name` is kept as a display copy filled from the list. With a list,
`check_professions` refuses a recipe that is not on it or belongs to another profession. A recipe names its
profession, so "which profession makes X?" is asked only when two listed recipes share a name across professions.

### 4.3 Migration

One pass per guild at first start: each typed recipe is matched to the list by `norm_recipe` within its profession
(then across professions). A match gets its id and the list's spelling. What does not match is kept as it is
(`id` empty = unmatched, no new field), shown to officers on the Professions page ("3 recipes are not in the game's list") with **Pick the
recipe…** and **Remove**; unmatched recipes are not searchable by the finder's list path but still name their
crafter. One commit per guild, one ops line.

### 4.4 The finder and entry

- The finder (`/professions who`, the Professions page, MCP `who_can_make`, the help section) searches the whole
  list: exact, whole words in a sentence, then part of a name (today's three tiers). A listed recipe nobody makes
  answers "Nobody in the guild makes Ironfeather Gloves yet." — a different sentence from "not a recipe".
  The site shows category and reagents.
- Entry: Discord's Add recipe… becomes a search-then-select over the list within the profession (autocomplete where
  Discord offers it, else a typed search validated against the list — prose validated, not parsed, per the Wizard
  rule); the site's editor searches the list; plain text ("I can make ironfeather shoulders") resolves against the
  list and the character's professions.
- Retired: `rename_recipe`, `merge_recipe`, their routes, the page's buttons, their MCP / parity entries.

## 5. Phase 2 — linking and the sync

### 5.1 The server's guild

`GuildConfig.armory`: `{region, guild_id, slug, name, faction, linked_by, linked_at}` or None. Owner only
(`/gm config`, Config page, MCP with the actor). The owner types the guild's name (prose); the bot looks it up and the
confirm screen says what it found — name, faction, PvP / Normal / Roleplay, member count, created date — so a
collision is seen before the press. The stored `guild_id` is checked on every roster read: a slug that now answers
with another id stops the roster sync and raises one ops warning (never silently follows a stranger's guild).
Unlinking keeps every character link; only "in the guild" stops being known.

Available only where the profile names an armory (`integrations.yaml`: the profile and static namespaces). TBC: off.

### 5.2 A character's link

`RegisteredCharacter.armory`: `{id, slug, linked_by, linked_at, checked_at, modified, level, cls, in_guild,
missing_since}` or None, and `profession_sync: bool = True`.

- **Who links.** The member for their own character; an officer for anyone; the seed (§5.5).
- **How.** The person gives the surname (and may correct the first name); the bot finds the character — the armory
  profile, or the linked guild's roster for one below level 10 — and the confirm sentence says what will happen:
  the character it found (name, class, level), that the record takes that name, and that its professions are replaced
  from the armory. Not found = a refusal in one sentence with what to do ("reach level 10 and log out once"). No
  gate on guild membership.
- **The name.** Linking sets `name` and `surname` from the armory. No rename verb exists today (`Registry.rename`
  is the member's display name; `name_character` only names a planned character) and the label is a key in run
  files, ledger rows, award ids and appeals. Designer's call (C9): **the old name becomes an alias, history is not
  rewritten.** The character gains `former_names`; the ledger stays append-only and award ids stay as they are;
  every lookup of a character by name (`Registry.find`, the loot context's label match, the import dedupe, the
  companion's loot routing) also answers to a former name of the same character. Run files of LIVE runs (open or
  locked) are rewritten to the new label in the same step, so sheets and cards show the real name; finished runs are
  left alone. This is one new verb, `rename_character`, used by linking and nothing else writes the two fields.
- **A name already taken.** The armory name equals another record's full name in the guild: refused in one sentence
  naming the holder to an officer ("Rhoz Zug is registered to Bovice. Move it or retire it first."), "ask an
  officer" to a member. Never a partial write.
- **One claim.** An armory character links to at most one record per guild; a second claim is refused naming who
  holds it (to an officer) or telling a member to ask an officer.
- **Unlink.** Keeps the record as it last was, professions become kept by hand.
- **Profession sync off / on.** Off: hand-edited from the list. Turning it on again says the hand edits are replaced.

### 5.3 What a sync does

Per linked character, hourly: one conditional request on the profile (`If-Modified-Since` = the stored `modified`).
Not modified → only `checked_at` moves, in memory, no commit. Modified → fetch profile and professions, then ONE
commit for that character if anything differs: level, `in_guild` (the profile's guild id == the server's), and —
when `profession_sync` — the professions (skill from the armory's tier; recipes as list ids; a recipe id not in the
list is kept by id and reported once to ops as "the recipe list is behind the game"). A class that differs sets a
flag officers see; `cls` is not written. The armory's answer is the truth for a synced character: it is written
without the hand-entry checks (the two-trade rule, the skill cap, "on the list"), and it replaces the whole list, so
unmatched hand-typed recipes on that character go (the link's confirm sentence says the professions are replaced).
Hand entry — unlinked, or sync off — keeps every check.

The roster: every three hours (Blizzard's own cache), conditional. It feeds `in_guild` and level for linked
characters the profile cannot show yet (below level 10), and the **not claimed** list. Kept in memory and in
`out/`, never in the data repo (derived, foreign data).

Failures: a 404 on a linked character sets `missing_since` and keeps everything; it is shown to officers after 24 h
("The armory no longer finds Rhoz Zug") and never unlinks by itself. API down, 429, a changed shape → the run stops,
one ops warning per hour at most, the last data stays. All requests off the event loop; a per-guild lock; several
guilds share one token and one polite rate (≤ 10 requests a second).

**Sync now** on a character (the member's own, officers for anyone): the same step, unconditional, rate-limited per
character.

### 5.4 What people see

- **Me.** Each character: Linked to the armory · Updated <when> · in the guild / not in the guild; **Link to the
  armory…**, **Unlink…**, **Keep professions by hand** (the switch), **Sync now**. Synced professions are shown, not
  editable, with why.
- **Members (officers).** The map up front: per Discord user, each character's link state. A fold **In the guild,
  not claimed (N)**: name, class, level, with **Give to a member…** (only when that member has a character it could
  be — it links an existing character or, when the member has none by that name, registers it for them through the
  officers' existing add path; never a record without a Discord user). Flags: class differs, armory no longer finds
  it, registered but not in the guild (information, not an error).
- **Move to another member… (officers).** Moves one character to another Discord user, and everything follows the
  character (owner's decision 2026-11-12): the record with its wishlist, regular spots and armory link, its past
  loot awards and its raid attendance count for the new user as if they had held it all along. The ledger stays
  append-only: the move is recorded (who, from, to, when) and award / attendance lookups resolve a character's
  owner through it; appeals on its awards belong to the new user. Signups and seats on live runs move with it.
  Refused while the character holds an open ask (the officer withdraws it first), or when the new user already has
  a character of that name. Both members get a notice, not a DM. No guild holds real loot or attendance yet, so no
  migration of old data is needed; the builder may choose the simplest correct mechanism.
- **Discord.** `/me char` and `/roster char` gain Link… / Unlink… / professions by hand; `/gm config` gains the guild
  link. Wizard and Confirm rules apply.
- **MCP / plain text.** Reads everywhere; link, unlink, move and the guild link through MCP with the actor. Plain
  text: out (`BY_DESIGN_ONLY`), a surname typed into chat is not worth a parser.

### 5.5 The seed

When the guild is linked (and on demand by an officer): the bot compares registered characters with the roster and
shows officers a proposal before writing anything.

- **Sure** — first name and class match exactly one roster entry, no other registered character in the guild has
  that first name and class, and no record holds that armory character: linked on the officer's one confirm (D8).
  The confirm applies each link on its own and reports any that were refused; one refusal never undoes the others.
- **Likely** — same class and a close name (the registered name against the roster's first name and first + surname;
  also the member's display name): listed with the candidate, an officer confirms each or picks another.
- **No match** — left unlinked; the member or an officer links later.

Measured on Big Guys Forever on 2026-11-12 (27 registered, 60 on the roster): 11 sure, 9 likely, 7 no match, 48
roster characters not claimed (15 of them level-1 bank characters). So the seed is a review screen, not a silent job.

## 6. Non-goals

Battle.net login and proof of ownership; alts discovered from an account; gear, talents, stats, reputations,
achievements; raid-kill history (the endpoints are empty); in-game rank; auction data; EU / KR / TW quirks beyond a
configurable region; reading the roster to create members; DMs of any kind.

## 7. Anti-goals

- A member or character record that exists because the armory said so.
- Any feature that works only for characters in the guild.
- Two sources for one fact on one character (synced professions are never also editable).
- Following a guild by name alone.
- A sync that commits when nothing changed, or that floods the data repo's log (one commit per changed character).

## 8. Constraints

- CLAUDE.md's rules: Copy, Confirm, Wizard, DM, Clock, Iconography; game version is data; several guilds in one
  process; slow work in threads; every write through the one verb per fact with the surfaces as thin callers; the
  parity matrix; the manual and design updated in the same commits.
- The API is undocumented and may change: every response is validated; an unexpected shape stops that step and keeps
  the last data.
- Tests never call Blizzard: recorded, anonymised responses under `tests/fixtures/`.
- Secrets stay in `.env`; no token in a log or an ops line.
- Armory facts are public game data, but member-typed text rules still apply to anything shown in a prompt.

## 9. What the API gives (probed with the bot's own credentials)

- Namespaces `profile-forever-us`, `static-forever-us`, `dynamic-forever-us`. No realms: a guild is
  `/data/wow/guild/<slug>`, a character `/profile/wow/character/<name>-<surname>` (lowercase).
- Guild: id, name, faction, ruleset, member_count, created_timestamp. Roster member: name, surname, slug, id, level,
  class id, race id, rank number. `Cache-Control: max-age=10800`.
- Character profile: id, name, class, race, guild {id, slug}, level, last_login_timestamp. 404 below level 10 or
  before the first logout (21 of 60 roster members on the day).
- `/professions`: primaries and secondaries, each tier with skill_points, max_skill_points and known_recipes
  [{id, name}].
- `Last-Modified` on roster, profile, professions; `If-Modified-Since` answers 304 with no body. Profile and
  professions share one time.
- Static: `/data/wow/profession/index`, `/profession/<id>` (skill_tiers), `/profession/<id>/skill-tier/<tier>`
  (categories → recipes), `/recipe/<id>` (crafted_item, reagents, crafted_quantity).
- Not available: events or webhooks, rank names, notes, online state, join dates, any account link.

## 10. Left to the builder

File and function names beyond those given; the shape of the review screen's components; where the roster snapshot
lives under `out/`; the scheduler hook (the bot's minute loop or its own task); the exact sentences (within the
Confirm and Copy rules, through the readability harness); how "likely" is scored, as long as a likely match is never
linked without an officer's press; test structure.
