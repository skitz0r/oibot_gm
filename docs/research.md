# Research Notes: Guild Master Bot

_Compiled 2026-09-13. This is reference material; the design proposal is in [design.md](design.md)._

---

## 1. WoW: Forever: what we know (announced 2026-09-12, BlizzCon)

- A permanent "Classic+" game: the original game continued on an alternate timeline, starting before Molten Core. Blizzard says it is "not a mode, a season, or a new version of Classic."
- Level cap 60. New zones, 9 new dungeons, a new race (Skyborne), and new race/class combos (e.g. **Dwarf Shaman, Forsaken Paladin**, so both factions have both shamans and paladins).
- Characters pick a realm ruleset (Normal / PvP / RP, Hardcore later) and can only group within it.

**Key dates**

| Date | Event |
|---|---|
| Sep 17 → ~Oct 22 | Beta |
| **Nov 4, 2026** | Launch |
| **Dec 9, 2026** | First raids open: Barrow Deeps (**10**-player), Hyjal Summit (**20**), Onyxia's Lair (**40**, from reports of the roadmap slide) |
| Spring 2027 | New 10-player and 20-player raids |
| Summer 2027 | New raid, plus a revamped "iconic" raid |

**Not announced yet (these directly affect us):**
- Loot method: master loot, group loot, or personal loot, and whether there are trade windows. The only hint is that raid transmog unlocks for whoever the item binds to, which "leaves organized loot decisions in the hands of raid groups." This suggests loot council stays viable, but that is **speculation**.
- Addon API rules (whether they follow Classic or the Midnight lockdown). This decides whether Gargul and RCLootCouncil will work.
- Boss lists and loot tables. No item data exists anywhere yet.
- Raid lockout rules, including whether one character can run several sizes each week.

Sources: [Blizzard announcement](https://news.blizzard.com/en-us/article/24302093/carve-a-new-path-with-world-of-warcraft-forever) · [What's Next recap](https://news.blizzard.com/en-us/article/24303862/world-of-warcraft-forever-whats-next-panel-recap) · [Deep Dive recap](https://news.blizzard.com/en-us/article/24303313/world-of-warcraft-forever-deep-dive-panel-recap) · [Icy Veins roadmap](https://www.icy-veins.com/wow/news/warcraft-forever-roadmap-unveils-raid-unlocks-and-major-updates/) · [Forum: Forever addons](https://us.forums.blizzard.com/en/wow/t/forever-addons/2347307)

### 1a. Forever buffs and group mechanics (checked 2026-09-15)

What the profile's `buffs.yaml` can lean on, by confidence:

| Claim | Status | Source |
|---|---|---|
| Blessing of Kings, Divine Spirit and Improved Mark of the Wild are **baseline** (no talent tax) | confirmed | [Blizzard deep-dive recap](https://news.blizzard.com/en-us/article/24303313/world-of-warcraft-forever-deep-dive-panel-recap) |
| Trueshot Aura: "+30 Ranged Attack Power to party members within 45 yards", MM row 4 — **party-scoped, ranged-only** | confirmed (beta capture, may change) | [classicwowforever.com hunter](https://classicwowforever.com/class-changes/hunter/) |
| Blessings last 1 h; one blessing per paladin still applies | reported | [zockify paladin](https://www.zockify.com/forever/paladin/), [classicwow.gg paladin](https://classicwow.gg/forever/guides/paladin) |
| Totems: "static radius", new **Totemic Projection** moves all totems | reported | [Icy Veins shaman](https://www.icy-veins.com/wow-forever/shaman-class-overview), [zockify shaman](https://www.zockify.com/forever/shaman/) |
| Totem scope (party vs raid), Windfury/Grace of Air/Strength of Earth/Mana Spring/Mana Tide values | **not published** → profile assumes Vanilla (party-wide) | — |
| Bloodlust/Heroism, Totem of Wrath, Wrath of Air, Unleashed Rage, Ferocious Inspiration (TBC-era) | **no evidence they exist** → dropped from the Forever profile | — |
| Vampiric Embrace: 30 s debuff with a cooldown (party heal, not a mana battery) | reported (beta capture) | [classicwowforever.com priest](https://classicwowforever.com/class-changes/priest/) |
| Shadow Weaving becomes a personal buff, not a raid debuff | reported | same |
| New spells: Lava Burst, Riptide, Wild Growth, Penance, Prayer of Mending, Berserk, Lone Wolf, Maelstrom Weapon | reported (beta capture) | classicwowforever.com class pages |
| Battle Shout, Blood Pact, Leader of the Pack, Moonkin Aura, Sanctity Aura scope | not addressed → assumed Vanilla party-wide | — |

The cards print each entry's `status` (confirmed / reported / assumed) so officers can see which numbers are a guess. Revisit once Blizzard publishes class data or the beta datamine settles (beta runs Sep 17 → ~Oct 22).

---

## 2. letmelcthatforyou (prior art)

[github.com/rashad-malik/letmelcthatforyou](https://github.com/rashad-malik/letmelcthatforyou): MIT, 1 author, 4★, last commit 2026-08-10 (v2.6.0-beta).

**What it is:**
- A Python desktop app (NiceGUI + PySide6). No Discord and no multi-user support.
- An officer logs into TMB, picks a zone, and gets "Suggestion 1/2/3 + a 1–2 sentence rationale" per item, exported as CSV. Prios are then copied into TMB by hand.

**LLM approach:**
- One plain chat completion per item, using `any-llm` (23 providers; default `claude-sonnet-5`) with max 500 output tokens.
- No tool use or structured output; results are parsed with regex.
- Metrics are computed in code, but the weighing is left entirely to the model.

**Data it uses:**
- **TMB**, with no official API. It captures the officer's TMB session cookie through a Qt browser login, then fetches export URLs:
  - `/{guild_id}/{slug}/export/characters-with-items/html`: JSON with characters, wishlists, received items and notes.
  - `/export/attendance/html`: CSV `raid_date, raid_name, character_name, credit, remark`.
  - `/export/item-notes/html`: CSV `id, name, instance_name, tier, prio_note`.
- **Warcraft Logs v2** GraphQL, for parses and for gear taken from combatant info.
- **Blizzard profile API**, for equipped gear.
- **Item DB**: `nexus-devs/wow-classic-items`. It does not use Wowhead.

**Loot factors it uses:**
- wishlist position, MS/OS, attendance %, recent MS wins, days since the last item in that slot
- WCL parse, ilvl upgrade, tier count, notes, professions
- items already awarded this session

**Weaknesses:**
- A custom policy is silently truncated to 800 characters.
- Attendance is divided by every raid date in the guild, so it breaks when a guild has several raid teams.
- The model does all the weighing, so results are not reproducible.

**Verdict: borrow, don't fork.** Worth copying (MIT; keep the notice):
- `services/tmb_manager.py` (TMB endpoints and how it detects an expired session)
- `data/tokens.json`, a hand-curated map from tier tokens and quest turn-ins to rewards, and recipe → profession
- `data/zones.json` (WCL zone IDs)
- the WCL GraphQL queries
- its candidate-filtering logic

---

## 3. ThatsMyBIS (TMB)

- **No public API.** `routes/api.php` only has internal autocomplete routes.
- **Source is no longer public.** `github.com/thatsmybis/thatsmybis` returns 404, but forks survive (e.g. [Karumenta/thatsmybis](https://github.com/Karumenta/thatsmybis)). It is Laravel with a **Commons Clause** license: self-hosting is allowed for your own guild, but it is not OSS.
- **Exports** (Guild → Exports):
  - `loot/{csv|html}/{all|prio|received|wishlist}`
  - `attendance/csv`
  - `item-notes/csv`
  - `raid-groups/csv`
  - `characters-with-items/json`
  - a Gargul export
- **Public loot table, no login needed:** `https://thatsmybis.com/loot/table/classic/csv` gives `instance_name, source_name, item_name, item_quality, item_id, url`. This is an item → boss → raid mapping.
- **Imports:** RCLootCouncil CSV and Gargul `/gl export` go into Assign Loot. **There is no way for us to write prios into TMB programmatically.**
- **Discord integration:** used for login and roles only. No webhooks.
- Supports Classic, SoD, TBC, WotLK, Cata and MoP. **No Forever support yet**, and for a solo-maintainer project it may arrive late.

## 4. Wowhead (deep dive, 2026-09-13)

**Can we get an API key? No.** There is no API, data-licensing or partner program, past or present. Users have asked on the Wowhead forums for 15+ years (threads from 2010 through Feb 2026) with no program ever created. Fanbyte/ZAM has no developer contact; the legal page lists only `accounts@zam.com`. The only staff-invited channel is the feedback link on [wowhead.com/tooltips](https://www.wowhead.com/tooltips), scoped to *tooltip feature requests* from fansites. Worth one polite email; expect no data grant.

**What's sanctioned:**
- The browser tooltip script (`wow.zamimg.com/widgets/power.js`). Useless inside Discord.
- **Wowhead's official Discord bot** (app 626904737731706881): `/tooltip <term>` posts a tooltip *screenshot* of the top search result; `/setup` picks Classic/Retail per channel. No lookup by ID, no loot tables, and no way for our bot to invoke it (Discord bots can't trigger each other's slash commands).
- Plain links. Discord unfurls `https://www.wowhead.com/classic/item=19019` into an OpenGraph card (icon + summary) with no server-side fetching by us.

**What's grey or forbidden:**
- `&xml` item feed: still *documented* on the Tooltips page, but staff called it "a legacy data format which Wowhead hasn't kept updated" (2019 thread on removing it). Contains name/quality/icon/tooltip only, no "dropped by".
- `nether.wowhead.com/.../tooltip/item/ID`: the JSON that `tooltips.js` fetches internally. Undocumented; server-side use falls under the ToS clause against "any engine, software, tool, agent … other than generally available third-party web browsers". Tolerated at low volume in practice, prohibited on paper.
- Bulk scraping, BiS lists, loot tables: prohibited. `robots.txt` blocks ClaudeBot and anthropic-ai.

**Precedents:** every major tool (Warcraft Logs, ThatsMyBIS, wowaudit, Raidbots, WoWAnalyzer, softres.it) uses Wowhead only as a client-side display layer and keeps its own item data. Raidbots builds from game files; TMB's original DB came from a private-server dump; Gargul reads the in-game API. Community bots that scraped Wowhead are all archived.

**Licensed alternatives for item data + loot sources:**

| Source | License | Item → boss | Notes |
|---|---|---|---|
| Blizzard Game Data API (`static-classic1x-{region}`) | Free, non-commercial, attribute Blizzard, refresh ≤30 days | **No** | Names, stats, icons, quality, item-search. No journal/encounter endpoints for Classic. |
| **AtlasLootClassic** | GPL-2.0 | Yes (Lua per boss) | Best curated tables. Copyleft: keep as a separately imported dataset. |
| **classicdb/database** (CMaNGOS) | GPL-3.0 | Yes (`creature_loot_template`) | Emulator-derived, powers classicdb.ch. |
| nexus-devs/wow-classic-items | MIT | Yes | Unmaintained since 2023 and built by scraping Wowhead, so the MIT tag doesn't launder provenance. Avoid. |
| Questie | none (all rights reserved) | Yes | Not reusable. |
| wago.tools | none stated | No (loot isn't client data) | Grey. |
| TMB public loot table CSV | unstated | Yes | Convenient; provenance is TMB's own DB. |

**Conclusion:** Blizzard API for item facts + AtlasLootClassic or CMaNGOS tables for loot sources (imported as data, kept separate from our code), officer override uploads for Forever, and Wowhead **only as outbound links**. No licensed BiS source exists; we curate our own prio notes. The official Wowhead Discord bot can sit in the server as a convenience for members; we don't depend on it.

## 5. Other data sources

| Source | Use | Notes |
|---|---|---|
| Blizzard Game Data API | Item names, stats, icons | Namespaces: `static-classic1x-{region}` (Era), `static-classic-{region}` (progression), `classicann-{region}` (Anniversary). No loot-source endpoints. Forever's namespace is TBD. |
| TMB public loot table CSV | item → boss → raid | Best mapping, and its item IDs match TMB wishlists |
| AtlasLootClassic Lua | item → boss → raid | Hand-curated; upkeep is uneven |
| wago.tools DB2 | Client data | No loot tables (those live on the server) |
| Warcraft Logs v2 | Parses, actual attendance, gear | OAuth client credentials. 3,600 points/hour on the free tier. Build attendance from report rosters (the `guild.attendance` field is flaky). |
| Raid-Helper API | Signups, events, attendance | `raid-helper.xyz/api/v4/`. Per-server API key from `/apikey` (admins only). Reading an event needs no auth. Webhooks need Premium (~€2–3/month). |
| softres.it | Soft reserves | Undocumented endpoints (`/api/raid/{id}/csv`, `/gargul`), which could break |

## 6. In-game addons (the path from bot to game)

- **Gargul** v8.0.0 (released 2026-09-12). `/gl tmb` imports TMB wishlists, prios and notes; `/gl export` sends CSV back to TMB. It also imports softres.it.
- **RCLootCouncil:** its CSV is TMB's original import format.
- The normal flow today is **TMB → Gargul → game → Gargul export → TMB**.
- Our bot can plug in either (a) by proposing prios that an officer enters into TMB, or (b) later, by generating a Gargul-compatible import string directly. That format is undocumented.
- **Both depend on the Forever addon API rules.**

## 6a. Loot-tool internals (deep dive, 2026-09-13)

From cloned sources: TMB fork (`Karumenta/thatsmybis`), Gargul, RCLootCouncil Classic, Core Loot Manager.

### ThatsMyBIS schema, the parts we should mirror
- **`character_items`** is the single pivot for everything: `item_id, character_id, raid_group_id, type ('wishlist'|'prio'|'received'|'recipe'), order, is_received, received_at, list_number (1–10), note, officer_note, is_offspec, import_id (unique, ≤20 chars)`.
  - Wishlist rank = `order` per character per `list_number` (up to 100 items × 10 lists).
  - Prio rank = `order` per `(item_id, raid_group_id)`; ties allowed; **hand-edited only, TMB has no prio-generation logic.**
  - On award, TMB inserts a `received` row and marks/deletes the first unreceived wishlist row matching the item **or its `parent_item_id` token**. Guild flags: `is_wishlist_autopurged`, `is_prio_autopurged`, `is_prio_private`, `tier_mode`.
- **`guild_items`**: per-guild `note, priority (free text), officer_note, tier (1–6)`. This is the "spec-level prio note" concept.
- **`items`** has `parent_item_id` (token → reward), `item_sources` (boss/object) and `instances`, all partitioned by `expansion_id` (1 Classic, 2 TBC, 3 Wrath, 4 SoD, 5 Cata, 6 MoP). Forever items would most likely reuse Classic IDs under `expansion_id=1`.
- **Attendance**: `raid_characters.credit` in ticks `0, .25, .5, .75, 1`, `is_exempt`, `remark_id` (1 Late, 2 Unprepared, 3 Late & unprepared, 4 No-call-no-show, 5 Gave notice, 6 Benched). `attendance % = SUM(credit)/COUNT(*)` over non-ignored, non-cancelled raids in a decay window, excluding exempt rows.
- **Faction head remaps**: Alliance/Horde variants of the same head (18423↔18422, 19003↔19002, …) are normalised on import. Gargul does the same.

### Formats
**Read (stable):**
- RCLC CSV, header-locked: `player,date,time,id,item,itemID,itemString,response,votes,class,instance,boss,difficultyID,mapID,groupSize,gear1,gear2,responseID,isAwardReason,subType,equipLoc,note,owner`
- Gargul `/gl export` TMB CSV: `dateTime,character,itemID,offspec,id` (stable since 2021)
- TMB `loot/csv/all`: `type, raid_group_name, member_name, character_name, character_class, character_is_alt, character_inactive_at, character_note, sort_order, item_name, item_id, is_offspec, note, received_at, import_id, item_note, item_prio_note, officer_note, item_tier, item_tier_label, created_at, updated_at, instance_name, source_name`
- TMB `attendance/csv`: `raid_date, raid_name, character_name, class, credit, is_alt, remark, character_inactive_at, is_exempt, member_name, character_note, raid_note, instances, raid_groups`
- softres.it Gargul JSON: base64(zlib(`{metadata, softreserves:[{name,class,note,plusOnes,items:[{id}]}], hardreserves}`))

**Write:**
- TMB Assign Loot CSV (case-sensitive headers): `character, date, itemID, itemName, note, offspec, id`. `id` gives idempotent re-import.
- **Gargul TMB payload** (`/gl tmb`): `base64(zlib(JSON))` with
  `{"wishlists": {"<itemId>": ["<lowercase name>[(OS)]|<order>|<1=prio,2=wishlist>|<raid_group_id or 0>", ...]}, "groups": {...}, "loot": "<itemId> > A > B\n...", "notes": {...}, "tiers": {...}}`.
  The 4-part pipe string is the contract and has been stable. Gargul also accepts a plain-text fallback: `itemId,a[1],b[2]` (ties `a,b|c`).
- Gargul has no prio-decision hook, but exposes `_G.Gargul` and events `TMB_IMPORTED`, `ROLLOFF_STARTED`, `ITEM_AWARDED`, plus `AwardedLoot:addWinner(...)`.

**Likely to break:** TMB `characters-with-items/json` (unversioned ORM dump), Gargul JSON award export (internal DB shape), softres.it reverse export, WCL zone IDs per season.

### RCLootCouncil Classic
Maintained (v1.5.1, 2026-09-01). Response model: per-item-type button sets (default Need / Greed / Minor Upgrade), award reasons (Disenchant/Banking/Free), council votes stored only as a count. No import for external prio lists.

### Core Loot Manager
Event-sourced DKP/EPGP ledger; exports TMB CSV `date,name,itemID,id`.

### Warcraft Logs (verified fields)
- Hosts: `www.` retail, `classic.` progression (MoP), `fresh.` Anniversary, `vanilla.` Era/HC, `sod.`. Forever's host is TBD.
- Attendance: `reportData.reports(guildID, zoneID, startTime, endTime) { data { code startTime zone{id} rankedCharacters{name classID} masterData{actors(type:"Player"){name subType}} fights{encounterID kill friendlyPlayers} } }`
- Parses: `characterData.character(name, serverSlug, serverRegion).zoneRankings(zoneID, metric, ...)` → `{bestPerformanceAverage, medianPerformanceAverage, rankings:[{encounter, rankPercent, medianPercent, spec, totalKills}]}`
- Gear: `report(code).events(dataType: CombatantInfo)` → `gear:[{id, itemLevel, permanentEnchant, gems}]`
- Budget: 3,600 points/hour; check `rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }`.

**Design consequences:** (1) model wishlist/prio/received as one typed ledger row like `character_items`, keep `import_id` for idempotent imports, and carry `list_number`; (2) reuse TMB's credit ticks and remark codes so attendance round-trips; (3) handle `parent_item_id` tokens and faction head remaps in the catalog; (4) the Gargul TMB payload is stable enough that **generating it directly is feasible in phase 1, not phase 2**.

## 6b. Wider prior art (deep sweep, 2026-09-13)

Note: non-English GitHub searches (CN/RU/DE/KR) turned up nothing reusable; CN Classic tooling lives on QQ/NGA/WebDKP, not GitHub.

### Published loot-scoring formulas (these converge)
| Source | Formula / factors | Take |
|---|---|---|
| **RCLootCouncil "Merit"** (CurseForge, GPL-2) | `Score = [((Audit+40)·0.4·UpgradeBoost·Attend%) / LootDivisor · Roll] · AltPenalty`. UpgradeBoost 0–12% by ilvl delta. **LootDivisor** grows steeply with items received in the last 14 days (0–1 items → 1.0, 2 → 1.2, 3 → ~3.7), with a small "greed allowance". Alt penalty, blackout dates. | The only published LC formula. Steal the loot-recency divisor and attendance-window semantics. |
| **cr0ok/ultra** (PHP, Aug 2026) | Six weighted factors, each zero-able: **U** upgrades received in this zone (tiered S/A/B, 6 mo), **L** time at #1 on wishlist, **T** time since last big upgrade, **R** raids attended, **A** 2-month attendance ratio, **P** best WCL parse. Pulls TMB JSON, WCL, Blizzard, Raid-Helper. | Closest thing to a de-facto TMB-era LC rubric. Steal the factor set. |
| **Krizerion/LPS** (Angular) | `LPS = ((ΔI·0.2)+(S·5)) / (1+L) · A · F`: ilvl delta, sim upgrade %, items in last 7 days, activity tier, effort. Reads wowaudit. | The "upgrade value ÷ (1 + recent loot)" shape. |
| EPGP | `PR = EP/GP` with periodic decay | Classical baseline; not planned. |
| BisCouncil Loot Summary (addon) | "Fairness 0–100" for loot evenness within role; formula unpublished | Keep the idea of a role-normalised fairness metric for analytics. |

Common shape: **`upgrade_value × attendance × performance ÷ f(recent_loot)`**, plus a wishlist-tenure term and an alt/rank multiplier.

### Composition solvers
- **christopherime/raidforge** (Go, MIT, the "WoW Raid Forge" from §7): boss-by-boss optimiser, heuristic + exact ILP; maximises Σ throughput + buff/debuff/utility − penalties, subject to size, tank/heal minima, boss-required utilities. Versioned buff matrix in `/data/`. Retail-oriented; the *objective structure and data layout* transfer.
- **sontommo/TBC-Raid-Comp-Optimiser** (Lua): encodes TBC 5-group meta rules (mana group, BM-hunter stacking, enh shaman in tank group, aura decoupling). Good seed for Classic-era soft constraints.
- **harvard-hbs/team-formation** (Py, MIT): CP-SAT team split with weighted soft constraints (size, cluster, diversify, numeric range). Reusable skeleton.
- **d-krupke/cpsat-primer**: the CP-SAT modelling guide to read.
- **No open Classic-era buff-group CP-SAT model exists.**

### Officer workflow / human-in-the-loop patterns
- **Alliance Auth SRP + AFAT** (EVE, GPL-3): the most mature officer HITL: request → evidence → reviewer decision → audit trail, Discord-notified; role sync from game auth. Model our approve/override/audit loop on it.
- **MangelSpec/wowaudit-mcp-server** (MIT): MCP server exposing roster/attendance/loot/wishlists with **write-gating flags**. Good model for role-gated tools.
- **lantisnt/DKPBot** (Apache-2): SavedVariables parsers for CLM / RCLootCouncil / CEPGP / Monolith / Community DKP. Reusable.
- **antony-ramos/GuildOps** (Go): loot counter, absences, strikes, notes per player. Copy the strikes/notes/absence data model.
- **MagdyAboYoussef/World-of-warcraft-discord-raid-manager** (Py): signup bot with a live buff-coverage board. Copy the UX.
- **dungnotnull/mmorpg-guild-management-agent-skill** (MIT): a prompt-only Claude Code skill with an LC rubric, escalation ladder and "precedent retrieval" prose. Skim, not software.
- **letta-ai/letta-discord-bot-example**: per-user attachable memory blocks. Reference for per-member memory, not policy.
- **humanlayer**: `require_approval` decorator over Slack/Discord, now deprecated. Copy the API shape only.
- **openclaw**: self-hosted agent gateway with Discord + tool approval. Overkill; mine its approval UX.
- Other MMO bots (GW2, FFXIV, Destiny elevatorbot, Albion, RoK) are signup/DKP variants with nothing on fairness or LLMs.

### What's genuinely unsolved (i.e. what we'd be building)
1. A persistent, human-editable **policy document + precedent memory** (override captured → applied and cited next time). Nobody has it.
2. An open **Classic-era buff-group CP-SAT model**.
3. **Role-scoped LLM tool access** below the "who may talk to the bot" level.
4. A published, validated **fairness metric**.

## 6c. Warcraft Logs, verified against the shadow guild (2026-09-13)

Queried with our client (`oibot-discord`, creds in `.env`) against `https://fresh.warcraftlogs.com/api/v2/client`. Sample: `fresh.warcraftlogs.com/reports/kdwgJZyDWjHaP8t1`, "25 Big Guys", US-Nightslayer, guild id **813187**, full BT + Hyjal clear on 2026-09-09.

**IDs.** TBC Anniversary runs on the `fresh.` host in its own partition: the report's zone is **1060 "BT / Hyjal"** with encounter IDs **50601–50609 (BT) and 50618–50622 (Hyjal)**. `worldData { zones }` lists the *original* TBC zone 1011 with encounters 601+, not 1060, so zone IDs for Anniversary must come from reports, not the zone list. Expect the same `50xxx` offset for Sunwell.

**Queries confirmed working and their shapes:**
- `report { title startTime zone guild { id name server { slug region } } masterData { actors(type:"Player") { id name subType } } fights(killType: Encounters) { id name encounterID kill size friendlyPlayers } }`. Note `actors` returned 659 entries with `subType: "Unknown"` for most; filter to those present in `fights.friendlyPlayers`.
- `report { table(dataType: Summary, fightIDs: [9]) }` → JSON with `composition: [{name, type, specs: [{spec, role}]}]` (25 rows, spec + role per player), plus `itemLevel`, `playerDetails`, `damageDone`, `healingDone`, `deathEvents`. **This is the spec source for rosters.**
- `guildData { guild(id: 813187) { attendance(zoneID: 1060, limit: 3) { total data { code startTime players { name type presence } } } } }` → works; 6 reports in zone; `presence` 1 = present. Earlier reports of flakiness didn't reproduce.
- `report { events(dataType: Buffs, fightIDs: [n], startTime, endTime, limit: 10000) { data nextPageTimestamp } }` → one page per short fight.
- Cost: the whole exploration (zones, summary, attendance, buff events for two fights, abilities) was **19 points** of 3,600/hour. Budget is not a concern.

**Party layout inference.** WCL doesn't expose group numbers, but party-scoped buffs land on exactly the five members of a party. Taking `applybuff`/`refreshbuff` target sets per ability and keeping sets of size 4–6 that span more than one class recovered, for the Naj'entus pull:

| Group | Members (spec) | Signal |
|---|---|---|
| Melee 1 | Olnick (Arms), Innater (Combat), Paldebaran (Ret), Growlbabe (Feral), Boviche (Enh) | Battle Shout |
| Melee 2 | Thillyw (Fury), Onaholeslave (Feral), Peezydst (Surv), Whistletipz (BM), Shamthrax (Enh) | Ferocious Inspiration |
| Casters | Gubini, Helvrax, Midgetboy (Warlocks), Moniix (Ele), Sugargoo (Moonkin) | Moonkin Aura |
| Mana group (inferred) | Thebeanz (Shadow), Cbottoms, Salieri (Arcane), Draingg (Aff), one Resto shaman | Blood Pact set on Rage Winterchill |
| Tank/healers (remainder) | Rhozy (Prot), Codenamezeus (Holy pal), Diza, Piddy (Holy priests), other Resto shaman | by elimination |

This is textbook T6 layout (one enh shaman per melee group, BM hunter + FI in melee 2, moonkin with the locks, shadow priest with the mana users), so it's a strong validation target for the comp solver.

Pitfalls the inference must handle, each of which maps to a `buffs.yaml` field:
- **Class-wide buffs** (Greater Blessings hit all five shamans) look like a party. Exclude single-class sets, and tag blessings `scope: class`.
- **Procs** (Windfury Attack, Lightning Speed, Dragonspine Flurry) hit whoever is meleeing across groups. Tag `kind: proc`, ignore.
- **Buffs present at pull** appear in `CombatantInfo.auras`, not as `applybuff` events, which is why totem buffs (Strength of Earth, Mana Spring) were absent. Combine both sources.
- **Groups change between bosses** (the Hyjal sets differ from the BT sets), so infer per fight, not per report.
- Player-targeted buffs (Power Word: Shield, Essence of the Martyr) are noise; a curated allow-list of party-scoped abilities per game profile is the robust approach, with the data-driven method as a fallback and a way to *discover* that list.

## 7. Existing Discord tools

- **Raid-Helper** is the de facto signup bot. It has a REST API, a Comp Tool, and Classic addon integrations (OG-RaidHelper, RaidPlanningHelper).
- **Raidify** is a free, Classic-focused bot with signups, rosters and a comp planner. No public API.
- **WoW Raid Forge** is the closest prior art for composition. It imports a Raid-Helper event, auto-builds buff groups, and pushes them back. TBC only, closed source.
- **No open-source Classic group optimizer exists.**
- **LLM guild bots:** essentially none beyond letmelcthatforyou.
- **Loot systems in use:** loot council (TMB/RCLC), EPGP, DKP (Core Loot Manager), soft reserve (softres.it), GDKP and MS>OS (Gargul).
- **Discord frameworks:** discord.py (mature; app commands, persistent Views, modals, Components V2) or discord.js.
- **Discord command permissions:** set `default_member_permissions` on each command, then admins grant roles per command under Server Settings → Integrations. Bots cannot change these overrides themselves, so we also check roles inside the bot.
