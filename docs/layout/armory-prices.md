# Layout — the armory link, professions, auction prices and the price watch

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  battlenet.py         what every Blizzard client shares (armory link §5.3): TOKEN (one client-credentials token, thread-safe, drop on a 401)
                       and LIMITER (≤ 10 requests in any second, process-wide); importers/blizzard.py and armory.py both use them
  armory.py            THE armory boundary (armory link §5.3/§9): Armory(region, profile_ns, static_ns, fetch=) guild / roster(since=) /
                       character(since=) / professions — BLOCKING (callers asyncio.to_thread), every answer validated (ArmoryShape), ArmoryMissing /
                       ArmoryDown(retry_after) / ArmoryAccess, `.words` = the sentence; NOT_MODIFIED on a 304; address / guild_slug; available(reg),
                       client_for(reg) (class / ruleset tables from integrations.yaml `armory`); the rank is never read (D7), no token in any error;
                       find_guild(reg, name) = the owner's typed guild name → GuildFacts (BLOCKING; no letters / > GUILD_NAME_MAX → ArmoryMissing)
  discord_armory.py    a character's armory link and its check (armory link §5.2/§5.3), module functions taking the client (bare test clients
                       work): find_for_link(bot, reg, name, surname) → Found{id, slug, name, surname, cls, level, source profile|roster, guild_id,
                       modified} (profile first, else the linked guild's roster; RegistryError NOT_FOUND); lookup_allowed(reg, by_id) = THE lookup limit (LOOKUPS_PER_MINUTE per person per guild, TOO_MANY_LOOKUPS; the site's 429, Discord's link box); NO_GUILD_YET / NOT_READ_YET = the read views' empty states (site + /roster armory); link_character / unlink_character /
                       set_profession_sync(…, by, *, by_id, via, preview) (own character or an officer, PRIV_LIVE; preview = the consequences'
                       sentences; the rename + link = ONE commit on the loop under the member's runs' board locks with the bot's RaidStore, sheets
                       redrawn; one ops.audited line); sync_character(…, force=, by_id=) → unchanged|updated|missing|limited (Sync now: unconditional,
                       5 min per character, SYNC_LIMITED); sync_guild (hourly; the first ArmoryDown/Access/Shape stops it, one ops warn per guild per
                       hour); roster_refresh(force=) (3 h, conditional; the stored guild_id checked — a stranger warns once; out/armory/<key>/roster.json,
                       never the data repo); roster_snapshot / unclaimed / flags (class | missing after 24 h | outside) / in_guild (DERIVED, never stored — R9: the armory id on the
                       roster snapshot of the guild linked NOW; None without a guild or before its first read) / last_checked /
                       updated_words read memory + the snapshot; armory_tick(bot, reg) = the scheduler's hook (one background task per guild when due;
                       off where armory.available is false). A 404 → the roster (below level 10, or renamed: re-pointed by id + rename_character) else
                       missing_since once, never an unlink; a recipe id the list lacks is kept and told to ops once (out/armory/<key>/recipes_reported.json).
                       In memory per guild key: last check / Last-Modified per armory id, Sync now's limit (stamped only after a check that worked), the warn throttle
                       (per kind: armory | roster-missing | professions:<id> | name:<id>); one asyncio lock per guild. A roster failure waits an hour per guild id.
                       No DM, no notice — ever, but for the move: move_character(bot, reg, from_id, label, to_id, by, *, by_id, via, preview) (§5.4;
                       officers only, PRIV_LIVE, OFFICER_ONLY["move"]; preview = consequences.move_consequence; Registry.move_character on the loop
                       under the board locks of Registry.move_runs with the bot's RaidStore, sheets redrawn, one audit line; the verb writes both notices)
  auctions.py          THE auction boundary (§5.65, in armory.py's style, over Armory.get: battlenet's token + limiter): House(id, name),
                       Price(low, qty) (copper); Client(arm, dynamic_ns).houses(ruleset_id) (the INDEX without `auctions` = ArmoryShape) /
                       table(ruleset_id, house_id, since) → ({item id: Price} | None on a 304, Last-Modified) = listings ceil(buyout/qty) + stackables
                       unit_price, lowest wins, quantities summed, unusable rows ignored (C2), an empty house {} / item_name(id); BLOCKING (to_thread);
                       available(reg) (profile `auctions:` + armory.available), client_for, faction_houses (C1: the profile's name per faction, never
                       the neutral house), money(copper) = THE "48g 99s 98c" (a zero part at either end dropped, a zero between gold and copper kept: "4g 0s 75c"; "0c"); NO_AUCTIONS_WORDS
  discord_auctions.py  the price table (§5.65, discord_armory's pattern): out/auctions/<guild key>/prices.json (derived, never the data repo — C7;
                       written whole, loaded on first use); refresh(bot, reg, force=) at most every FETCH_EVERY 10 minutes (issue #8, 2026-11-15; hourly before — nearly every check is a not-modified answer; a failed check is tried again at the next, the ops warn stays one per hour), conditional on Last-Modified (Table.at = the feed's
                       time: its Last-Modified, ≤ now, now without one — a 304 leaves it; Table.checked_at = the last check that worked, which drives
                       the hour and the shared read); state(reg) off | waiting | fresh | stale (6 h, C3) | old (24 h: not shown, C10 — after a restart too), from at;
                       price_of / table / as_of_words (+ STALE_TAIL; stamp= Discord's <t:…:R>); checked_words(reg) → ('Last checked 11:48 AM. Next check about 11:58 AM.', seconds until the page asks again) — the Prices page's status line only (GET /api/prices/watching `checked` / `again_in`); two guilds on one house share a read; a read whose
                       market changed under it is discarded; PRICE_LISTENERS fn(guild key) on new prices or a new feed time changing the state (the
                       web's `prices` topic), and from auctions_tick once when time alone turns the table stale or old; auctions_tick = the scheduler's hook (one background task per guild when due, then — once per NAME_EVERY 1 hour per game version, _naming_due — name_items: ≤ 200
                       listed ids named from Blizzard into out/auctions/items.json — an id Blizzard has no item for is kept in out/auctions/items_gone.json ACROSS restarts and asked again only after prices.GONE_RETRY 7 days (forgotten at a restart, those ids filled every batch and nothing new was named — found live 2026-11-14)); a failure keeps the table + one ops warn per guild per hour;
                       house_choices (the owner's picker: ruleset from the armory, faction houses with listings, samples) / pick_house (re-checked;
                       HousesChanged 409) / refresh_soon (the first read after a pick, off the request). No DM, notice, post or ops line on success.
                       Price watch (§5.66): history_key(reg) = the picked house's (namespace, ruleset, house) | None; every new table taken as the
                       guild's — and an adopted shared read (T1-a) — is kept by _keep (price_history.record in a thread; a failure keeps the table,
                       HISTORY_FAILED one ops warn per guild per hour, kind history); not on a 304 nor a discarded read; auctions_tick starts the fold
                       once a UTC day (_fold_daily: one background task, only where the profile has `auctions:`)
  price_history.py     (movers, 2026-11-14: moves(key, window, now, min_qty=MOVE_MIN_QTY 20) → [Move(item, usual, now, qty)] | None = each item listed now with the
                       time-weighted middle value of its lowest price over the window — unlisted / unread time counts for nothing, listed for half the
                       window; a history younger than the window is ranked over what is kept from MOVES_MIN_HISTORY 2 h on (moves_since = from when; prices.movers says 'Over the last 2 hours 40 minutes, all the bot has kept so far.'), None with less; the `changes_at` index; prices.movers ranks ≥ MOVE_MIN_PCT 10 % with both prices ≥ MOVE_MIN_PRICE 1 silver into dips / rising — EVERY mover since 2026-11-15, rows with pct / diff / diff_words / group; the card filters by `groups` (All · integrations.yaml `auctions.groups` by Blizzard's item class, `reagents: true` = where an unclassed reagent goes · Other), orders by `sorts` (percent | gold), `steps` MOVE_STEPS, shows `page` MOVERS_TOP 15 per Show more, `none_words`; floor_words; Climbing / Sliding (2026-11-15) = the answer's `climbing` / `sliding` (+ `*_words`, `trend_words`): price_history.trends(key, window, steps, now) → ([Trend(item, values, now, qty)], steps kept — the whole steps a young history covers, None under TREND_MIN_STEPS 3) = each step's time-weighted middle price; prices.TREND_STEPS {window: (steps, 'every hour')}, prices._trend_way = no step the other way, ≤ TREND_FLAT 1 still, now no worse than the last step; rows = Movers' own with usual_words the FIRST step's price, ≥ MOVE_MIN_PCT from it to now; two more tabs on the card; the class = auctions.Client.item_facts → prices.kinds (out/auctions/items_kinds.json, "" = asked, none), read with each name and, with what is left of NAME_PER_FETCH, for listed items already named — prices.unkinded)
                       the kept price history (§5.66, C2, C10), BLOCKING (callers to_thread): one SQLite file per AUCTION HOUSE (never per guild)
                       under out/auctions/history/ (never the data repo), path(key) from (namespace, ruleset, house); tables meta (first/last kept
                       feed time) · changes (item, at, low, high, qty: a change high = low; a folded day high ≥ low, qty NULL; low NULL = nothing
                       listed from then) · last (the comparison base) · unread (start, stop: more than GAP_AFTER 3 h between two kept feed
                       times — added on open to an older file); record(key, at, prices) = a row only for a changed lowest price or quantity
                       (a delisted item a NULL row), once per feed time (not later than the last kept → 0), the unread stretch before it, one
                       immediate transaction; points(key, item, since, until) (the last value before `since` carried in AT since; an unread
                       stretch an item went into with a value = a Point(unread=True) at its start, clamped to since, + the value it resumed
                       with at its end unless it changed there — F-5) / span(key, item, days, now) → Span (lowest, highest of the hourly
                       lowest; nothing listed and unread time count for neither) / kept_since; fold(now) over every file:
                       older than KEEP_FULL 90 days → one low/high per item per UTC day, older than KEEP_DAILY 365 dropped (an item's standing
                       value stays as the carried start — P2; unread stretches kept, those ended past the year dropped), a file empty or whose
                       newest feed time is past a year deleted; FOLD_BATCH items per write with FOLD_PAUSE_S between (a record's 5 s busy wait
                       always gets in — F-7; cut short = still readable, the next fold finishes); HistoryError
  price_reference.py   Bargains' reference prices (2026-11-15), BLOCKING, knows no guild: fetch_text(url) (THE network read — tests refuse it in
                       conftest), parse → {item id: (market value in copper, sold per day)} (columns itemId / marketValue / soldPerDay, ≥ 100 rows),
                       refresh(url, dest) (kept whole), load(dest) → Reference(read_at, values) cached by mtime. integrations.yaml `auctions.reference`
                       {name "Classic", source "TradeSkillMaster", url: the public Classic region items.csv}; discord_auctions.reference_path(reg) =
                       out/auctions/reference/<version>.csv, _reference_daily from auctions_tick (once a day; a failed read keeps the copy, retried hourly).
                       prices.bargains(reg) = the materials group's lowest price here ÷ the reference price against the MEDIAN ratio (typical): rows ≥
                       BARGAIN_MIN_GAP 2× under it ('17× cheaper than most'), typical_words / source_words / hint_words / none_words; GET
                       /api/prices/bargains (members, site only); the Movers card's second tab
                       Prep (2026-11-15, the third tab): prices.prep(reg) = levelling-zone materials (integrations.yaml `auctions.prep`
                       {material_levels [10, 45], end_classes [Consumable], end_level 50, vendor_sold {id: name}}) that reach a high-level
                       consumable through the recipe graph — NO hand list of consumables (the owner, 2026-11-15) — with uses_words / vendor_words /
                       Bargains' gap_words, + below_rows (listed under a vendor's price); GET /api/prices/prep. Item level / level needed / vendor
                       price: profiles/<v>/item_facts.yaml (GENERATED by scripts/import_item_facts.py for the recipe list's items;
                       GameProfile.item_facts {id: (level, required, sell, class)}) else prices.levels (out/auctions/items_levels.json, read by the
                       hourly naming: auctions.Client.item_facts → ItemFacts(name, kind, level, required, sell)); prices.item_fact reads both
  armory_seed.py       the seed (armory link §5.5, D8): propose(reg, roster, *, taken_at=) → Seed{sure, likely, none, unclaimed, rev} of
                       SeedRow{uid, member, label, cls, candidate, why same name|close name|display name, others} — PURE (no request, no write);
                       considered(m, c) = THE predicate (active, named, real member still here — the map counts over it too), unlinked ones proposed; sure = first name (case-folded) + class on exactly one
                       roster row, unheld, unique on the registered side, the surname equal when set; likely = same class + a folded name equal /
                       starts with / contains (≥ 4 letters) / difflib ≥ 0.8, else the display name; ≤ 3 candidates; a held row is never a candidate
                       nor unclaimed. check(reg, picks, *, by_id) = apply's checks before the armory (the preview); give(bot, reg, uid, armory_id,
                       by, *, label=, spec=, offspec=, by_id, via, preview) = Give to a member… (officers live; Registry.give_refusal first:
                       D2, left, link_refusal, the add path on a copy; then add + link; a link refused after the add → registered, not linked).
                       apply(bot, reg, picks [(uid, label, armory id)], by, *, by_id, via) → [{uid, label, id, ok, text}]: officers
                       (live), each pick re-checked on the current snapshot (ROW_GONE / ANOTHER_CHARACTER / link_refusal) and linked through
                       find_for_link + link_character on its own; nothing links without an officer's pick
  professions.py       professions (design §5.60, §5.64): catalog(reg) = what the guild makes, DERIVED from the records (a listed recipe per id in the
                       list's spelling, an unmatched one per norm_recipe key in the most used spelling, crafters highest skill first; never stored);
                       search(reg, text, limit, profession=) = THE finder over the game's WHOLE list (indexed once per profile) + unmatched recipes with crafters,
                       three tiers (exact, whole words, part of a name; a crafted recipe never drops out as part of a longer name), made first; limit None =
                       every match (the total); list_hits = the list alone through the same tiers (the wizard's select, plain text); who_can_make reads
                       search ('#<id>' = a picked recipe); nobody_sentence / NOT_A_RECIPE / answer_lines (+ more_sentence) = its words; crafters_of(reg, pid);
                       visible = active character, not a puppet, not left; has_recipes(reg, pid) (the list's, R7, else ProfessionDef.makes);
                       chat only LOOKS UP (the owner, 2026-11-16: professions come from the armory) — no plain-text write of a profession, skill or
                       recipe (op_rows, NeedsProfession and the picker are gone; configops.PROFESSIONS_NOT_HERE is the refusal);
                       fix_unmatched(reg, uid, label, pid, name, recipe_id|None, by, by_id=, preview=) = the officers' fix for a recipe typed before the
                       list (Pick the recipe… / Remove; rename_recipe / merge_recipe and their routes are retired, §5.64 C7), unmatched(reg) = those
                       recipes for the officers' card (off_list: no id, an id the list lacks or gives elsewhere — an armory one kept by hand; never a
                       character whose professions follow the armory; none without a list), migrated_line; HELP_WORDS / help_hit / help_section = the help section
                       (every member-typed name through unfence). With the list the section (2026-11-13) = a line per crafter (member, skill,
                       level when linked, `armory` | `by hand`, a recipe COUNT — never a recipe book), the question's recipes (search, then
                       phrase_hits = two neighbouring words of a name, or with tails= an enchant's one word after its dash + "enchant…", a plural, two neighbouring words of what a recipe MAKES ("arcanite bar transmutes" → Transmute: Arcanite) and, alone, a one-word category ("flasks") — 2026-11-19), then the
                       first HELP_BOOK recipes of a crafter the question names; `armory` = a recipe not shown is one they don't know (the header
                       says so); without a list _help_section_registered = as before.
                       Names only: nothing pings, DMs, tracks a request or reads a website. Game data: profiles/<v>/professions.yaml (GameProfile.professions,
                       profession_rules, ProfessionDef.makes); the confirm words: consequences.profession_consequence (verb "professions")
  prices.py            prices for people (§5.65), PURE (reads discord_auctions, never Blizzard): names(reg) = recipe list → loot items →
                       out/auctions/items.json (an unnamed id isn't searchable, C6); search(reg, text, limit) → (ItemPrice rows, total), listed first;
                       recipe_cost(reg, recipe) → Cost (a line per reagent, a reagent named twice summed, `missing`, what it makes priced each — C9) |
                       None; THE words: NONE_LISTED, price_words, cost_sentence ("Cost to make: at least … — N reagents have none listed."),
                       made_words, cost_line, status_sentence(reg, owner, stamp=) (off: the owner's OWNER_OFF_WORDS, a member nothing), answer_lines,
                       more_words, price_answer (/price), listings_words ("N listed in all"), choices_words, sample_rows, choice_description;
                       help_hit / help_section = the help answer's `prices` section (a price word AND a named item or recipe; ≤ 10 items, ≤ 3 recipes,
                       whole blocks). Display lines, not confirms (T4-a). Every read gated on state: fresh / stale show, off / waiting / old don't.
                       Price watch (§5.66): watch / unwatch (→ "You're watching <name>." / "You've stopped watching <name>."; watch refused while
                       prices don't show — as_of_words, C15 —, an unnamed id NO_MATCH; unwatch never refused),
                       is_watching, watch_list (priced while shown, else names alone; UNNAMED), history_sentences (BLOCKING: the 7- and 30-day
                       "Lowest in the last 7 days: … · highest: …" / "None listed in the last 7 days.", + "The bot has kept prices since <day>."
                       under 30 days; NO_HISTORY_YET; [] while hidden — C16), chart(reg, item, span) (BLOCKING: points {t, when (local12; a folded
                       day its UTC day alone), low, high, words, unread (NOT_READ "not read", low/high null)} + the span's low/high and their words + since / until = the span's start and the server's now), SPANS 7 · 30 · 90 days · 1 year,
                       EMPTY_LIST_SITE / EMPTY_LIST_DISCORD (EMPTY_LIST_HIDDEN on both while prices are hidden: no search to point at)
  web/api_armory.py    install_armory_api (called from api.py; armory link §5.2, §5.4, §5.5) = thin callers of the discord_armory / armory_seed
                       verbs: POST /api/me/armory/find|link|unlink|switch|sync (the session's own character by `label`, never a uid) and
                       /api/members/armory/<same> (officers, `uid` | `member`); link names the `armory_id` its find showed (else 400 LOOK_FIRST,
                       another → 409; LOOKUPS_PER_MINUTE 6 per person → 429); POST /api/members/armory/give → armory_seed.give (no rule in the
                       route); GET /api/armory/map (officers: counts linked / not yet over armory_seed.considered, `unclaimed`, the roster's age; `members` only for MCP);
                       GET /api/armory/seed + POST …/seed/apply (`rev` → 409 with the fresh proposal; the preview = armory_seed.check, the
                       sentence counts the picks that pass); POST /api/members/move (discord_armory.move_character; on EVERY game version, R11).
                       Every write `preview`s; armory_error_response = THE armory failure (ArmoryDown / ArmoryAccess → 503, else 400; api_setup's guild link too), a refusal 400; seed apply → `message` / `head` / `refused` (armory_seed.result_lines); from MCP a write needs the actor (D8); the
                       rest 404 where armory.available is false; `member` published after each write and on
                       every roster read (discord_armory.ROSTER_LISTENERS). char_extras = the character rows' `armory` (+ `when` = the local12 time alone), `profession_sync`,
                       `flags` (web/api.py char_json). MCP: armory_map, armory_seed, link_character, unlink_character, move_character,
                       apply_armory_seed (tests/test_parity_matrix.py ARMORY: every action's web, Discord and MCP rows)
  web/api_professions.py  install_professions_api (called from api.py; design §5.60, §5.64): GET /api/professions (every member: each profession's
                       crafters + the catalog; `officer`; officers' `unmatched`), GET /api/professions/find?q=&profession= (who_can_make; 25 rows + `total`;
                       a row's `listed` / `category` / `makes` / `reagents` / `nobody` = nobody_sentence; `message` = answer_lines, `more` = more_sentence),
                       POST /api/professions/unmatched (officers; `preview` → the sentences, then fix_unmatched's write; a changed record 409, no recipe list 404). A character's professions are written by /api/members/set `professions` (officers,
                       the one-member route; MCP set_professions) and read on every character row and /api/meta.professions (`makes` = has_recipes,
                       `recipe_list` = the version has the game's list)
  web/api_prices.py    install_prices_api (called from api.py; §5.65) = thin callers of prices / discord_auctions / Registry: GET /api/prices/find?q=
                       (every member; {rows [{id, name, low_words, qty, words}], total, words, more, message}; 404 without auction houses);
                       GET /api/admin/market (officers: selected, words, prices, clear_sentences, rev, owner) + POST (owner: {choices} → houses with
                       listings_words + samples; {house_id, ruleset_id, preview} → sentences; {…, rev} → set (pick_house re-checks; 409 on a stale rev
                       or changed houses; the same house → no write, no audit); {clear, preview?, rev?}; from MCP a write needs the owner as actor;
                       404 where auctions.available is false); cost_json = a recipe row's `cost` (api_professions.entry_json; null without prices or
                       reagents — never "0c"); prices_meta = /api/meta.prices {on, state, words, market} (+ /api/meta.item_url); PRICE_LISTENERS →
                       publish("prices"). History's council view carries item_id (api_loot) so its names link.
                       The watch list (§5.66; the session's own uid, 403 from MCP — C11 OWN_LIST, 404 where auctions.available is false — /price's gate): find rows carry
                       `watching`; GET /api/prices/watching → {rows (names alone while hidden), words, shown, max, empty, registered};
                       POST /api/prices/watch {id, on} → {message, rows} (prices.watch/unwatch in a thread; a refusal 400; the quiet write publishes
                       `member`); GET /api/prices/history?id=&span= → {id, name, spans, **prices.chart, sentences} for any item the bot
                       can name, watched or not (the owner's decision 2026-11-14, replacing C6; an unnamed id only when it is on the viewer's
                       own list, else 400 NO_MATCH; the answer's `used_in` = prices.used_in_words;
                       GET /api/prices/movers?window=3|6|12|24|168 → prices.movers (members, site only — an experiment: dips / rising against the
                       usual price over the window, one history read in a thread; an unknown window 400; 404 where auctions.available is false); an unknown span 400); the chart's spans = prices.SPANS (24 hours first — the default — then 7 / 30 / 90 days, 1 year; a history younger than the span starts the x axis where the history does); a watch list row carries `spark` = prices.spark (the last 24 hours, with change_words '+12%' and up = the line's colour; before 2026-11-14 the last seven days
                       as [t, low | null] over since..until, None with nothing to draw — the page's Sparkline; read in the routes' thread)
```
