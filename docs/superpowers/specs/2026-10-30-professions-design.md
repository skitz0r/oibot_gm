# Professions: a tracker and a finder — design (2026-10-30)

Owner's brief: members register which professions their characters have and at what skill, plus the notable recipes
they know; anyone can ask who can make a thing; a #professions channel is listened to so changes and questions there
need no tag. Mockups, approved 2026-10-30: https://claude.ai/artifact/BFSSTCp7FbGrVUUCi72Zen (six boards: Me, the
in-place editor, Members, the Professions page, Discord, phone). Design §5.60 folds this in when built.

## Decisions

- **Depth: both, recipes optional.** Profession and skill always; notable recipes when the crafter types or pastes them.
  Nobody types a 200-recipe book: the record holds the ones worth telling the guild about.
- **Names only.** The finder answers with crafters' names. Nobody is pinged, nothing is tracked after the answer, no
  request queue, no mats, no tips.
- **No recipe database now.** The catalog is what members register. The import boundary is built so an addon export
  or a wago.tools seed (the client's data tables as CSV; foreverchanges.pro's own source) can drop in later. Nothing
  reads any website. Wowhead stays off limits.
- **Read-only data is public** (owner decision 2026-10-29): who plays what and who can make what is answered to any
  member in any channel the bot answers in.
- **Rights follow the people rules.** A member edits their own characters' professions, an officer anyone's, the owner
  everything. No officer confirm applies to a profession.

## Data

### Game data: `profiles/forever/professions.yaml`

Twelve professions in three groups, the skill cap, an icon name each; an optional `recipes:` seed list, empty now.

```yaml
skill_cap: 300
trade_slots: 2            # crafting + gathering together
professions:
  alchemy:        {name: Alchemy,        group: crafting,  icon: trade_alchemy}
  blacksmithing:  {name: Blacksmithing,  group: crafting,  icon: trade_blacksmithing}
  enchanting:     {name: Enchanting,     group: crafting,  icon: trade_engraving}
  engineering:    {name: Engineering,    group: crafting,  icon: trade_engineering}
  leatherworking: {name: Leatherworking, group: crafting,  icon: trade_leatherworking}
  tailoring:      {name: Tailoring,      group: crafting,  icon: trade_tailoring}
  herbalism:      {name: Herbalism,      group: gathering, icon: trade_herbalism}
  mining:         {name: Mining,         group: gathering, icon: trade_mining}
  skinning:       {name: Skinning,       group: gathering, icon: inv_misc_pelt_wolf_01}
  cooking:        {name: Cooking,        group: secondary, icon: inv_misc_food_15}
  first_aid:      {name: First Aid,      group: secondary, icon: spell_holy_sealofsacrifice}
  fishing:        {name: Fishing,        group: secondary, icon: trade_fishing}
recipes: []               # later: [{id: <game recipe id>, name, profession, skill}]
```

`GameProfile.professions` loads it (ids, names, groups, cap, icons; `recipes` keyed by id and by normalised name).
Icons come through the same render-CDN proxy as class icons (`classes.yaml: icons`); the generated badge is the
fallback. No profession name is written in code. A gathering profession has no recipes.

### Member data: `RegisteredCharacter.professions`

```python
class Recipe(BaseModel):
    name: str                   # as the member typed it, ≤ RECIPE_NAME_MAX (60), member text
    id: Optional[int] = None    # the game's recipe id when an import or seed knew it

class Profession(BaseModel):
    id: str                     # a profile profession id
    skill: int                  # 1..skill_cap
    recipes: list[Recipe] = []  # notable recipes; empty for gathering
```

`professions: list[Profession]` on the character, stored in the member's existing file, versioned by `characters_rev`,
saved through the character verb. It goes with the character on retire, delete and set-main. Validation: a profession
id the profile has; skill in range; at most `trade_slots` of crafting + gathering together, secondary free; one entry
per profession; recipe names non-empty, ≤ 60 characters, unique per profession after normalisation; no recipes on a
gathering profession.

### The catalog, per guild: `professions.catalog(reg)`

Derived from the records, never stored on its own: one entry per normalised recipe name (lower-case, punctuation and
double spaces folded, a trailing "s" kept — "Gauntlet" and "Gauntlets" are two names until an officer merges them),
with the display name most crafters use, the profession, and the crafters (character, member, skill). A name matching
a seeded recipe takes its id. Officers' `rename` and `merge` rewrite the names on the records they touch, through the
verb, one audit line.

## Words (manual §1a)

*profession* · *skill* (a number, 1–300; 300 reads green) · *notable recipes* (the ones worth telling the guild about,
never a full book) · *crafter* (a character that knows a recipe or has the profession) · *who can make* (the finder).
Skill is typed on the site (validated 1–300) and a select of 25-point steps in Discord (Wizard rule). Icons stand
alone: a profession icon with the skill beside it, no label (Iconography rule). Empty states in words: "No notable
recipes yet — add the ones worth telling the guild about in Professions…"; "no professions on record — <name> adds
them on the Me page or with /me professions"; "Nobody has registered Cooking yet. A cook adds it on the Me page, with
/me professions, or by saying so in #professions."

## The module: `src/oibot_gm/professions.py`

Everything about professions lives here; other modules call it.

- `set_professions(bot, reg, member, character_label, entries, *, by, by_id, preview=False, rev=None)` — THE verb:
  validates (`check_professions`), writes through `discord_people.save_characters` (registry lock, validated on a copy,
  `CharactersChanged` on a stale rev), never touches runs. Audit line: `"<name>: <char>'s Leatherworking 275 → 300, +
  Devilsaur Gauntlets"`. Returns the confirm sentences on preview.
- `profession_consequence(reg, m, c, before, after, *, by_id)` — the Confirm-rule generator, rendered and linted by
  `tests/test_confirm_readability.py`. "Your Leatherworking is recorded at 300 with 4 notable recipes." / "Rhoz's
  Enchanting is removed from their record." / "Your Skinning is recorded at 285." A refusal is one sentence: "A
  character has two trade professions — drop one first." / "Skill is a number from 1 to 300." / "A recipe name is up
  to 60 characters." / "<X> isn't a profession in this game version."
- `catalog(reg)`, `rename_recipe(...)`, `merge_recipe(...)` — officers' catalog maintenance through the verb.
- `who_can_make(reg, text) -> list[Hit]` — exact normalised name first, then every catalog recipe whose words all
  appear in the text; each hit = recipe + crafters (character, member, profession, skill), highest skill first.
- `crafters_of(reg, profession_id, min_skill=0)` — who has a profession, highest skill first.
- Both finders skip retired characters, test puppets and members with `left_at`.
- `help_section(ask)` — the help snapshot's professions section (members' view): each profession's crafters with
  skill, then the catalog with makers; gate words in `HELP_WORDS`.
- `LISTEN_WORDS` — what the Listening description and glossary gain.

### Import boundary: `src/oibot_gm/importers/professions.py`

`detect(text) -> str | None` and `parse(text) -> list[dict]` with rows `{character, profession, skill, recipes:
[{id?, name}]}`, the shape the leveling guide's paste and the ledger import use. Recognises nothing yet; the Me page
shows no paste box until a format exists. The first real format is a parser and a paste box, nothing else.

## Plain text (configops)

A `profession` op in the own-record group (`OP_GROUP["profession"] = "self"`, `bind_self` scopes it): fields `target`
= character label (optional when the member has one character with that profession or one character), `profession`,
`skill`, `recipe_add`, `recipe_remove`, `remove: true`. "Rhoz is a 300 leatherworker", "I just learned Devilsaur
Gauntlets on Rhoz", "drop enchanting on Jonnie" become ops, previewed and applied on the press. Recipe names are
member text in the data block, validated for length, never interpreted. The describe line: `Rhoz · Leatherworking:
275 → 300` / `Rhoz · notable recipes: + Devilsaur Gauntlets`. Parity matrix: the op with its four surfaces.

## The help answer

`help.SECTIONS["professions"]` (members' view, cap 3 000) gated on profession names, "craft", "make", "recipe",
"enchant", "who can", and any catalog recipe name in the question; builder = `professions.help_section`. Questions in
any channel, DM or the site's Tell the bot box are answered from it; the bot says plainly when nobody has registered
a recipe and how a crafter adds one.

## Listening

No new channel kind. The owner sets #professions to Listening Replies with plain text Own record only (as #wow-chat
today). `LISTEN_BOT_WORDS` gains "it knows each character's professions, their skill and the notable recipes they
make"; `LISTEN_GLOSSARY` gains the profession names, "recipe", "craft", "make", "who can make". A change becomes an
own-record preview with Apply; a question an answer; unsure gets the 👀.

## Surfaces

### Site
- **Me › Characters**: one professions line under each character row (icon + skill per profession, recipe chips, or
  the empty state); the row ⋮ gains Professions…, which opens that character's professions in place (mockup Edit):
  profession select grouped crafting / gathering / secondary with taken trades greyed, skill input 1–300, recipe chips
  with an Add input, a trash per profession, Cancel / Save <char>'s professions; Save previews the sentences through
  `POST /api/me/characters` `preview` then writes. Refusals replace the preview.
- **Members**: the same line under each character for every member (read-only); the character ⋮ gains Professions…
  for officers, the same editor; the search box also matches recipe names. No new column, no new count chip.
- **Professions page** (`/app/professions`, members; rail entry after Loot log): a search box over the catalog, hits as
  recipe → crafters (highest skill first), officers' Rename… / Merge… per hit (each previews one sentence); a tab per
  profession with count, each listing crafters with a skill bar and their recipe chips; empty states in words.
- Routes: `GET /api/professions` (catalog + crafters per profession), `GET /api/professions/find?q=`,
  `POST /api/professions/rename|merge` (officers, preview → write), `POST /api/members/set` gains `professions`
  (the one-member route), `/api/me/characters` carries `professions` rows. `/api/meta.professions` = the profile list.
- Phone: the finder first, hits as cards, profession tabs scroll sideways, 44 px targets; Me's card gets a full-width
  Professions… button.

### Discord
- `/me professions`: a wizard on the person's own characters — character select → hub (its professions, each
  profession's recipes, "Two trades are taken. Cooking, First Aid and Fishing are open.") with Add profession… ·
  Change skill… · Add recipe… · Remove…, each ending on `Wizard.decide` with the confirm sentence. Profession and
  skill are selects, a recipe name a prose modal (validated, not parsed).
- `/roster professions member:<picker>`: the officers' form of the same wizard.
- `/professions who recipe:<autocomplete over the guild's catalog>` and `/professions list profession:<Choice>`:
  ephemeral, no model; an unknown recipe answers "Nobody has registered that recipe yet — crafters add theirs with
  /me professions". `/professions` is its own top-level group (two subcommands); the registering wizards sit under
  `/me` and `/roster`, which have room.
- Registration and the application wizards are unchanged.

### MCP
`set_professions(member, character, …)` and `who_can_make(text)` over the site's routes; `plain_change` already covers
the op. `get_member` rows carry professions.

## Tests
- `tests/test_professions.py`: validation (two-trade rule, range, names, gathering has no recipes, unknown
  profession), the verb's write and rev refusal, catalog normalisation / rename / merge, `who_can_make` exact and
  word matches, retired and departed left out, the import boundary's empty `detect`.
- Confirm sentences in `test_confirm_readability.py`; labels and empty states in `test_copy.py`; the op in
  `test_parity_matrix.py`; the help gate and public view in `test_help.py`; the wizards in the harness; frontend source
  tests for the page, the editor and the rail entry; shots for Me, Members and Professions, desktop and phone.

## Docs, same commits
Manual: a Professions section, §1a words, the commands, MCP rows, §8c Listening note. Design §5.60 = this spec.
CLAUDE.md: `professions.py`, `importers/professions.py`, the help section, the op, the page.

## Build order (each phase gated and committed by an Opus agent; push and restart by the lead)
1. Profile file + `GameProfile.professions`; `Profession` / `Recipe` on the character; `professions.py` verb,
   validation, consequence, catalog, finders; import boundary; tests.
2. Plain-text op + parity; help section; Listening words; MCP tools; manual words.
3. Site: Me and Members lines + editor; the Professions page; routes; bundle; shots.
4. Discord: `/me professions`, `/roster professions`, `/professions who|list`; harness runs; manual commands.

## Out of scope
A request queue, pings or DMs about crafting; materials or tips; a full recipe database; reading any website;
skill-level history; recipes on gathering professions.
