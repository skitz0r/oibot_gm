# Auction Prices and Item Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner picks the guild's auction house; the bot keeps an hourly table of each item's lowest price and quantity listed, shows a recipe's cost to make by material, answers an ad hoc price check, and links item names to Wowhead.

**Architecture:** A blocking Blizzard boundary (`auctions.py`, on `armory.Armory`'s request path) reads houses and listings. `discord_auctions.py` owns the per-guild table under `out/auctions/` and the scheduler hook. `prices.py` is pure: item names, search, a recipe's cost and every sentence. Web, Discord, MCP and the help answer are thin callers of `prices.py`.

**Tech Stack:** Python 3.12 / uv, discord.py, FastAPI, React + Mantine (Vite), pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-13-auction-prices-design.md` (approved 2026-11-13). Read it first; decisions are cited as D1–D4 / C1–C12.

## Global Constraints

- Read `CLAUDE.md` first: its Principles and Conventions bind every task (Game version is data, Code computes / Claude explains, Copy, Confirm, Wizard, DM, Iconography, Clock; several guilds per process; slow work in `asyncio.to_thread`; Discord answers within 3 s).
- Gate: every price behaviour on `profile.auctions` (integrations.yaml `auctions:`) AND a selected market; `tbc` behaves exactly as today — a test proves it. Links are gated on `wowhead_item_url` alone.
- Price = the lowest buyout per unit on the selected house (D2). A listing with no buyout, a buyout ≤ 0 or a quantity ≤ 0 is ignored (C2). Per listing: `ceil(buyout / quantity)`; stackables feed: `unit_price`.
- Ages: fetch every 1 hour; stale after 6 hours (C3); not shown at all after 24 hours (C10) — also for a table loaded after a restart.
- The words (spec §3): the auction house, lowest price, "N listed", "none listed", "Prices as of <when>", cost to make, "at least … — N reagent(s) have none listed". Never house id / ruleset / namespace / commodities where a person reads (`tests/test_copy.py` stays at 0 hits). Money: `48g 99s 98c`, zero parts dropped, `0c` for zero.
- Cost to make is per craft; the made item's price is "each"; the two are never combined (C9). No profit figure anywhere.
- No DM, no notice, no channel post, no ops line on a successful fetch. A failure: at most one ops warning per guild per hour; the last good table is kept.
- No request to Blizzard on a member's request path. The only exceptions are the owner's picker (in a thread, behind a working screen) and the scheduler's background task.
- The price table lives under `out/auctions/` only — never the data repo (C7).
- Tests never call Blizzard: an injectable `fetch`, recorded responses trimmed to a few rows under `tests/fixtures/auctions/`. No token in a log, ops line or error. Never a real member's or character's name in code, tests, fixtures or docs.
- The bot never fetches Wowhead and the site never loads its scripts.
- Every new `*_consequence` / `*_sentence` generator gets cases in `tests/test_confirm_readability.py`. `docs/manual.md` changes in the same commit as a behaviour a person can see.
- Gate for a task: its own tests, then once at the end `uv run python scripts/check_commands.py > <log> 2>&1; echo "exit=$?"` (about 17 minutes; read the real exit code, never pipe through `tail`, never chain with `&&`). After touching `frontend/`: `cd frontend && npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py`; commit the bundle. Commit on the branch; never push, never restart the bot.

## Review Focus

1. **A table too old to trust.** A restart with a 30-hour-old `prices.json`, or fetches failing for a day: every surface shows no price and says the prices are out of date. (Task 3: `test_a_table_older_than_a_day_is_hidden_after_a_restart`.)
2. **One item in both feeds, or listed many times.** The lowest unit price of all rows wins and the quantities add up. (Task 1: `test_an_item_in_both_feeds_takes_the_lowest_and_sums_the_quantity`.)
3. **Rows that are not prices.** No buyout, buyout 0, quantity 0, a missing `item`: ignored, never a zero price or a division error. (Task 1: `test_rows_without_a_usable_buyout_are_ignored`.)
4. **A recipe that doesn't fit the simple case.** It makes nothing (an enchant), makes a range, lists the same reagent twice, or has a reagent with none listed: the cost still reads right and "at least" counts the missing. (Task 4: `test_cost_of_an_enchant_a_range_and_a_missing_reagent`.)
5. **The market changes under a fetch.** The owner clears or changes the house, or relinks the guild to another faction, while a fetch is in flight: the result is discarded, never stored against the new choice. (Task 3: `test_a_fetch_for_a_house_no_longer_selected_is_discarded`.)

---

### Task 1: The profile block and the auction boundary

**Files:**
- Modify: `profiles/forever/integrations.yaml`, `src/oibot_gm/profiles.py` (GameProfile.auctions), `src/oibot_gm/armory.py` (GuildFacts.ruleset_id; `Armory.get` public)
- Create: `src/oibot_gm/auctions.py`, `tests/test_auctions.py`, `tests/fixtures/auctions/{index,house,commodities,house_empty}.json`

**Interfaces:**
- Produces:
  - `integrations.yaml: auctions: {dynamic_namespace: "dynamic-forever-{region}", factions: {Alliance: "Alliance Auction House", Horde: "Horde Auction House"}, reference_items: [three stackable item ids that the recipe list names]}`; `GameProfile.auctions: dict | None` (None for tbc).
  - `armory.GuildFacts.ruleset_id: int`; `Armory.get(path, ns, since=None, missing=...)` = the existing `_get`, public.
  - `auctions.House(id: int, name: str)`, `auctions.Price(low: int, qty: int)` (frozen dataclasses, copper).
  - `auctions.Client(arm: armory.Armory, dynamic_ns: str)`: `.houses(ruleset_id) -> list[House]`; `.table(ruleset_id, house_id, since: str | None = None) -> tuple[dict[int, Price] | None, str | None]` (None = not modified; merges the listings and the stackables feed; an answer without `auctions` = `{}`).
  - `auctions.client_for(reg) -> Client`, `auctions.available(reg) -> bool` (profile block + `armory.available`), `auctions.faction_houses(reg, houses, faction) -> list[House]` (name equals the profile's for that faction), `auctions.money(copper: int) -> str`.
  - Failures are armory's own (`ArmoryDown` / `ArmoryAccess` / `ArmoryShape` / `ArmoryMissing`).

- [ ] **Step 1: Write the failing tests** in `tests/test_auctions.py` with a fake `fetch` serving the fixtures (shapes: spec §8):
  - `test_houses_lists_the_ruleset_and_faction_houses_filter_by_name`
  - `test_table_merges_listings_and_stackables` — listing `{buyout: 4099, quantity: 1}` → 4099; `{buyout: 1000, quantity: 3}` → 334.
  - `test_an_item_in_both_feeds_takes_the_lowest_and_sums_the_quantity`
  - `test_rows_without_a_usable_buyout_are_ignored` (no buyout, buyout 0, quantity 0, no item id)
  - `test_an_empty_house_is_an_empty_table_not_an_error`; `test_a_304_on_the_listings_is_not_modified`
  - `test_an_odd_shape_raises_armory_shape` (auctions not a list; a row not an object)
  - `test_money_words`: `money(489998) == "48g 99s 98c"`, `money(4099) == "40s 99c"`, `money(10000) == "1g"`, `money(0) == "0c"`.
  - `test_tbc_has_no_auctions`; `test_guild_facts_carry_the_ruleset_id`.
- [ ] **Step 2:** `uv run pytest tests/test_auctions.py -q` → fails (no module).
- [ ] **Step 3:** Implement. `table` sends `since` on the listings request only; when it answers 304, return `(None, modified)` without reading stackables.
- [ ] **Step 4:** `uv run pytest tests/test_auctions.py tests/test_armory.py -q` → pass.
- [ ] **Step 5:** Commit.

### Task 2: The market choice on the guild

**Files:**
- Modify: `src/oibot_gm/registry.py`, `src/oibot_gm/consequences.py`, `tests/test_confirm_readability.py`
- Create: `tests/test_market.py`

**Interfaces:**
- Consumes: `armory.GuildFacts.ruleset_id`, `auctions.House`.
- Produces:
  - `registry.Market {ruleset_id: int, house_id: int, name: str, faction: str, picked_by: str, picked_at: str}`; `GuildConfig.market: Market | None` (in the `settings` rev).
  - `Registry.set_market(house: House, ruleset_id: int, by, *, by_id, rev=None) -> str` and `Registry.clear_market(by, *, by_id, rev=None) -> str`: owner only (`is_owner_id`), one commit each, refused without a linked guild or where `profile.auctions` is None; the caller audits.
  - `Registry.market_words() -> str` (the choice in words, or what is missing and how to fill it).
  - `Registry.market_survives(facts) -> bool` (C11: same ruleset id and faction). `set_armory_guild` clears the market in the same commit when it does not survive; `clear_armory_guild` always clears it.
  - `consequences.market_consequence(reg, house) -> list[str]`, `market_clear_consequence(reg) -> list[str]`; `armory_guild_consequence` / `armory_guild_clear_consequence` gain one sentence when a market will be cleared ("Prices turn off until you pick the auction house again.").

- [ ] **Step 1: Failing tests** in `tests/test_market.py` (scratch `GitStore(push=False)` on the fixtures, as `tests/test_armory_guild.py` does):
  - `test_only_the_owner_sets_and_clears_the_market`; `test_no_linked_guild_or_no_auctions_block_is_refused`
  - `test_a_stale_settings_rev_raises_config_changed`
  - `test_relinking_the_same_ruleset_and_faction_keeps_the_market`; `test_relinking_another_faction_or_ruleset_clears_it_in_the_same_commit`; `test_unlinking_the_guild_clears_it`
  - `test_one_commit_per_write`
  - readability cases for the two new generators and the two extended ones.
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_market.py tests/test_armory_guild.py tests/test_confirm_readability.py -q` → pass. **Step 5:** Commit.

### Task 3: The price table and the hourly check

**Files:**
- Create: `src/oibot_gm/discord_auctions.py`, `tests/test_auction_table.py`
- Modify: `src/oibot_gm/raid_scheduler.py` (call `auctions_tick` beside `armory_tick`, same try/except)

**Interfaces:**
- Consumes: `auctions.Client.table / houses`, `auctions.client_for`, `auctions.available`, `Registry.config.market`.
- Produces (module functions; state per guild key in memory, files under `OUT = ROOT/out/auctions`, read at call time):
  - `FETCH_EVERY = 1 h`, `STALE_AFTER = 6 h`, `HIDE_AFTER = 24 h`.
  - `Table(at: datetime, house_id: int, ruleset_id: int, prices: dict[int, Price], modified: str | None)`.
  - `table(reg) -> Table | None` — the stored table for the market selected NOW (another house's file = None).
  - `state(reg) -> Literal["off", "waiting", "fresh", "stale", "old"]` (off = no profile block or no market; waiting = no table yet; old = past 24 h).
  - `price_of(reg, item_id) -> Price | None` (None unless state is fresh or stale).
  - `as_of_words(reg) -> str` ("Prices as of Fri 13 Nov 9:17 PM", + " — more than six hours old" when stale; the off / waiting / old sentences otherwise).
  - `async refresh(bot, reg, *, force=False) -> str` → `"updated" | "unchanged" | "off" | "failed"`; `async auctions_tick(bot, reg)` (one background task per guild when due; never awaits Blizzard on the minute loop).
  - `async house_choices(bot, reg) -> list[Choice]`, `Choice(house: House, ruleset_id: int, listings: int, samples: list[tuple[int, int | None]])` (item id, lowest) — reads the guild's armory page for its ruleset, then each faction house (C1); skips an empty one.
  - `reset_state()` for tests; `PRICE_LISTENERS: list[Callable[[str], None]]` fired with the guild key after an update.
- Rules: the file is written whole (temp + rename) as `out/auctions/<guild key>/prices.json`; two guilds on one (region, ruleset, house) share one fetch within the hour; a failure keeps the table and warns ops once per guild per hour (reuse `discord_armory`'s warn wording pattern, its own throttle); a fetch whose market is no longer the selected one is discarded.

- [ ] **Step 1: Failing tests** (fake client injected; a fake clock):
  - `test_first_fetch_writes_the_table_and_a_second_within_the_hour_does_nothing`
  - `test_not_modified_keeps_the_table_and_its_time_moves_to_now` (an unchanged feed is still fresh)
  - `test_stale_after_six_hours_and_hidden_after_a_day`
  - `test_a_table_older_than_a_day_is_hidden_after_a_restart` (write the file, `reset_state()`, read)
  - `test_a_corrupt_or_missing_file_is_no_table`
  - `test_a_failed_fetch_keeps_the_table_and_warns_once_an_hour`
  - `test_a_fetch_for_a_house_no_longer_selected_is_discarded`
  - `test_two_guilds_on_one_house_share_one_fetch`
  - `test_tick_is_off_without_a_market_and_on_tbc`; `test_no_dm_no_notice_no_ops_line_on_success`
  - `test_house_choices_offer_only_the_faction_houses_with_listings`
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_auction_table.py -q` → pass. **Step 5:** Commit.

### Task 4: Item names, the search, a recipe's cost and the words

**Files:**
- Create: `src/oibot_gm/prices.py`, `tests/test_prices.py`
- Modify: `src/oibot_gm/discord_auctions.py` (the naming step after a fetch), `src/oibot_gm/auctions.py` (`Client.item_name`)

**Interfaces:**
- Consumes: `discord_auctions.table / state / price_of / as_of_words`, `auctions.money`, `GameProfile.recipes`, the profile's loot items.
- Produces:
  - `auctions.Client.item_name(item_id) -> str | None` (static namespace `/data/wow/item/{id}`; 404 → None).
  - `prices.names(reg) -> dict[int, str]`: recipe `makes` and reagents, the profile's items, then `out/auctions/items.json` (process-wide cache per game version, written whole).
  - `discord_auctions`: after each update, name up to `NAME_PER_FETCH = 200` listed ids that nothing names, in the same background task, most-listed first; a failure stops the naming quietly.
  - `prices.ItemPrice(id, name, low: int | None, qty: int)`; `prices.search(reg, text, limit=25) -> tuple[list[ItemPrice], int]` — named items whose name holds every typed word (case-folded), listed ones first, then by name; `""` → `([], 0)`.
  - `prices.CostLine(id, name, quantity, unit: int | None, total: int | None)`; `prices.Cost(lines, total: int, missing: int, made: ItemPrice | None, makes_min: int, makes_max: int)`; `prices.recipe_cost(reg, recipe: RecipeDef) -> Cost | None` (None unless `state` is fresh or stale; a repeated reagent id is one line with the quantities added).
  - Sentences: `NONE_LISTED = "none listed"`; `price_words(p: ItemPrice) -> str` ("Arcanite Bar: lowest price 48g 99s 98c, 30 listed." / "Arcanite Bar: none listed."); `cost_sentence(cost) -> str` ("Cost to make: 12g 40s." / "Cost to make: at least 12g 40s — 1 reagent has none listed." / all missing: "Cost to make: not known — none of the reagents are listed."); `made_words(cost) -> str` ("Makes 1 · lowest price 3g 20s each, 14 listed" / "Makes 2 to 4 · …"; "" when it makes nothing); `status_sentence(reg, owner: bool) -> str` (as_of_words when on; off → the owner's "Pick the guild's auction house on the Config page to see prices." and "" for others).

- [ ] **Step 1: Failing tests** with a hand-built table:
  - `test_names_come_from_recipes_items_then_the_cache`
  - `test_search_matches_every_word_listed_first_and_counts_the_total`
  - `test_search_is_empty_when_prices_are_off_or_too_old`
  - `test_cost_of_an_enchant_a_range_and_a_missing_reagent`
  - `test_a_reagent_listed_twice_is_one_line`
  - `test_the_sentences` (each string above, exactly)
  - `test_naming_stops_quietly_on_a_failure_and_caps_per_fetch`
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_prices.py tests/test_auction_table.py -q` → pass. **Step 5:** Commit.

### Task 5: The site's routes, MCP and the parity rows

**Files:**
- Create: `src/oibot_gm/web/api_prices.py`, `tests/test_prices_web.py`
- Modify: `src/oibot_gm/web/api.py` (install; `/api/meta`), `src/oibot_gm/web/api_professions.py` (`entry_json` → `cost`), `src/oibot_gm/mcp_server.py`, `tests/test_parity_matrix.py`, `tests/test_mcp.py`, `docs/manual.md`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `/api/meta`: `prices: {on: bool, state, words}` (words = `status_sentence` for this viewer), `item_url: str | None` (the profile's `wowhead_item_url`).
  - `GET /api/professions` and `/find` rows gain `cost: {lines: [{id, name, quantity, unit_words, total_words}], sentence, made_words} | null`.
  - `GET /api/prices/find?q=` (every member) → `{rows: [{id, name, low_words, qty, words}], total, words}`.
  - `GET /api/admin/market` (officers read: `{selected, words, clear_sentences, rev}`), `POST /api/admin/market` (owner): `{choices: true}` → `{choices: [{house_id, name, listings, samples: [{name, low_words}]}], ruleset_id}`; `{house_id, ruleset_id, preview}` → sentences; `{house_id, ruleset_id, rev}` → set, then `discord_auctions.refresh` as a background task; `{clear, preview?, rev?}`. 404 where `auctions.available` is false; 409 on a stale rev; one `ops.audited` line per write; from MCP a write needs the owner as actor.
  - The `prices` SSE topic published from `PRICE_LISTENERS`.
  - MCP: `price_check(text, guild)` and `market(guild)` (reads). `tests/test_parity_matrix.py`: a `PRICES` table (`check`: web + discord `/price` + mcp; `market_set` / `market_clear`: web + `flow:armory_guild`, MCP exempt with a written reason — "the owner matches sample prices against the game by eye").

- [ ] **Step 1: Failing tests:** `test_meta_carries_prices_and_item_url`; `test_recipe_rows_carry_cost_when_prices_are_on_and_null_when_off`; `test_find_returns_rows_total_and_words`; `test_market_routes_owner_only_preview_set_clear_and_409`; `test_everything_404s_or_is_null_on_tbc`; the parity test; the MCP tools' tests.
- [ ] **Step 2:** run → fail. **Step 3:** implement (routes are thin callers; no rule in a route). **Step 4:** `uv run pytest tests/test_prices_web.py tests/test_parity_matrix.py tests/test_mcp.py tests/test_professions_web.py -q` → pass. **Step 5:** Commit (manual: the routes' visible behaviour is described in Task 8's manual section — add the MCP tools to §8b here).

### Task 6: Discord — the picker, `/price`, and cost on `/professions who`

**Files:**
- Modify: `src/oibot_gm/flows_config.py` (ArmoryGuildWizard), `src/oibot_gm/discord_registry.py`, `src/oibot_gm/flows_professions.py`, `docs/manual.md`
- Create: `tests/test_prices_discord.py`

**Interfaces:**
- Consumes: `discord_auctions.house_choices / refresh`, `Registry.set_market / clear_market / market_words`, the consequences, `prices.search / price_words / recipe_cost / cost_sentence / status_sentence`.
- Produces:
  - `ArmoryGuildWizard.hub` (a linked guild, where `auctions.available`): a line with `market_words()` and a button **Auction house…** → `working` → `house_choices` in the background → a select (one option per house: label "Auction house N of M", description = its listings count and the three sample prices in money words) → `decide` (`market_consequence`) → `set_market` → hub. With a market: also **Turn prices off…** (decide, `market_clear_consequence`). No new `/gm config` subcommand (the group is at 25).
  - `/price item:` (every member, ephemeral; autocomplete = `prices.search` names, value `#<id>`; the answer = `price_words` + `as_of_words`; several matches for typed text → up to 10 lines + "and N more"; off / waiting / old → that sentence alone). Description ≤ 100 characters.
  - `/professions who`: a recipe's answer gains `cost_sentence` when prices are on.

- [ ] **Step 1: Failing tests** through `tests/wizard_harness.py`: `test_the_hub_offers_the_auction_house_only_with_a_linked_guild`; `test_pick_decide_set_and_the_rev_is_checked`; `test_turn_prices_off`; `test_price_answers_one_item_several_and_the_off_sentence`; `test_autocomplete_never_waits_on_blizzard`; `test_who_adds_the_cost_only_when_prices_are_on`; every screen lints.
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_prices_discord.py tests/test_professions_flows.py -q` → pass; `uv run python -c "from oibot_gm import wizard_flows"`. **Step 5:** Commit with the manual's Discord lines.

### Task 7: The help answer's `prices` section

**Files:**
- Modify: `src/oibot_gm/help.py` (SECTIONS), `src/oibot_gm/prices.py`, `tests/test_help.py`, `tests/test_prices.py`, `docs/manual.md`

**Interfaces:**
- Produces: `prices.HELP_WORDS` (price, prices, cost, costs, worth, "how much", auction, cheapest — as words), `prices.help_hit(reg, question) -> bool` (a price word AND prices on AND an item or recipe the question names), `prices.help_section(ask) -> list[str]`: the status line, then up to 10 `price_words` lines for named items, then for a named recipe its cost lines and `cost_sentence`; member-typed text through `unfence`; cap 2,000 characters in `SECTIONS`.

- [ ] **Step 1: Failing tests:** `test_how_much_is_an_item_quotes_the_lowest_price`; `test_cost_to_make_a_named_recipe_lists_each_reagent`; `test_no_section_when_prices_are_off_or_no_item_is_named`; `test_the_section_stays_under_its_cap`.
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_help.py tests/test_prices.py -q` → pass. **Step 5:** Commit.

### Task 8: The site — item links, cost to make, the price box, the owner's card; the record

**Files:**
- Create: `frontend/src/components/ItemName.tsx`, `frontend/src/components/MarketCard.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/pages/Professions.tsx` (+ its css), `frontend/src/pages/Config.tsx`, `frontend/src/pages/LootLog.tsx`, `frontend/src/components/loot/{Tonight,History,Items,Wishlists,MyLoot}.tsx`, `tests/test_frontend_source.py`, `profiles/forever/integrations.yaml` (`wowhead_item_url: "https://www.wowhead.com/forever/item={id}"`), `tests/` for `discord_bot.wowhead` on forever, `docs/manual.md`, `docs/design.md` (§5.65, the record as built), `CLAUDE.md` (Layout entries), the built bundle.

**Interfaces:**
- Consumes: `/api/meta.prices`, `/api/meta.item_url`, the `cost` field, `/api/prices/find`, `/api/admin/market`.
- Produces:
  - `ItemName({id, name})`: with `meta.item_url` and an id → `<a href target="_blank" rel="noopener noreferrer">`; else plain text. Every item name with an id on the listed pages renders through it (a source test asserts no listed page prints `item` / `name` of an item row outside `ItemName`).
  - Professions: under a recipe's reagents, one line per reagent (`ItemName` · quantity × unit · total, or "none listed"), then the server's `sentence` and `made_words`; the status sentence once per card; nothing when `cost` is null. A price box card ("What does it go for?") above the tabs: part of a name → rows from `/api/prices/find`, the server's words for empty and "more". Cards below `sm`.
  - `MarketCard` on Config › Settings under the server's guild card (only when `meta.armory` and the market route answers): the choice in words; owner: **Pick the auction house…** → the choices with their sample prices → the server's sentences through `useConfirm` → set; **Turn prices off…**. Registers with `useDirty` if it holds an edit state.

- [ ] **Step 1: Failing source tests** in `tests/test_frontend_source.py` (the page uses `ItemName`; no page builds a Wowhead address itself; the cost and status sentences are printed from the server's fields, never composed in the page) and `test_wowhead_links_forever_items` (Python: `wowhead(item, forever_profile)` → the address; tbc unchanged).
- [ ] **Step 2:** run → fail. **Step 3:** implement; `cd frontend && npx tsc --noEmit && npm run build`; `uv run python scripts/check_bundle.py`.
- [ ] **Step 4:** Write the manual (glossary words, Professions, Config, `/price`, the Wowhead links incl. the Discord loot item card — C12), `docs/design.md` §5.65 and the `CLAUDE.md` layout entries.
- [ ] **Step 5:** The full gate: `uv run python scripts/check_commands.py > <log> 2>&1; echo "exit=$?"` → `exit=0`. Commit.
