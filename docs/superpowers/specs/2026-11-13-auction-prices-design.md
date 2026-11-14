# Auction prices and item links — design

Date: 2026-11-13. Status: draft for the owner's approval.
Design home when built: `docs/design.md` (a new §5.65). Research: the live probe of 2026-11-13 (§8 below).

## 1. Why

The bot now knows every recipe in the game with its reagents and who can make it (armory link, §5.64). The next
question a guild asks is "what does it cost?" — what a craft costs to make, material by material, and what one item
goes for right now and whether any is up. Blizzard publishes WoW: Forever's auction listings (undocumented) to the
app credentials the bot already holds, so the bot can answer from the guild's own market.

Separately, an item's name on the site is plain text today. Wowhead now has a Forever section, so a name can be a
link to its page.

## 2. Decisions (the owner's, 2026-11-13)

- **D1 Two uses, one table.** (a) Cost to make, by individual material. (b) An ad hoc price and availability check
  for one item.
- **D2 The price is the lowest listing.** An item's price is the lowest buyout per unit on the selected market.
  No average, no "typical" price, no depth-of-book cost.
- **D3 The owner selects the market.** The armory does not say which auction house a guild trades on; the owner
  picks it once.
- **D4 Item names link to Wowhead** on the site, as plain outbound links.

Calls made by the designer (the owner saw them in chat and said "look good"; flagged so they can be overruled):

- C1 Only the guild's own faction's houses are offered. The neutral (Blackwater) houses are not.
- C2 A listing without a buyout (bid only) is ignored.
- C3 Prices older than six hours are marked stale wherever they show.
- C4 No vendor prices in this version. A vendor-bought reagent shows as none listed, or at whatever players list it for.
- C5 The plain-text help answer can quote a price ("how much is an Arcanite Bar").
- C6 The price check searches the items the recipe list names plus any item currently listed whose name the bot can
  resolve (§4.3) — the repo has no full item list for Forever.
- C7 The price table is derived data: kept under `out/`, never in the data repo, rebuilt by the next fetch after a restart.
- C8 A guild with no market selected sees no price anywhere; the Professions page says how the owner turns prices on.
- C9 Cost to make is per craft. What the recipe makes shows its lowest price "each", beside how many a craft makes
  (a range reads "makes 2 to 4"). The two numbers are never multiplied or subtracted for the reader.
- C10 Prices older than 24 hours are not shown at all: every surface behaves as "none known yet" and says the prices
  are out of date. Between 6 and 24 hours they show, marked stale (C3). This holds for a table loaded after a restart.
- C11 Linking the server to a different in-game guild keeps the market only when the new guild's ruleset and faction
  equal the stored choice's; otherwise the choice is cleared and the confirm sentence says prices turn off until the
  owner picks again.
- C12 The owner confirmed on 2026-11-13 that `https://www.wowhead.com/forever/item=13452` resolves (Wowhead adds the
  name to the address itself; the template needs the id only). So the template is set in this build. The same key
  already feeds the Discord loot item card, so that card gains its link too (intended; the manual says so).

## 3. The words (Copy rule)

- **the auction house** — the guild's selected market. Never "house id", "ruleset", "namespace", "commodities".
- **lowest price** — the lowest buyout per unit listed. **N listed** — the total quantity up.
- **none listed** — nothing with a buyout is up. Never a bare dash where a sentence fits.
- **Prices as of <when>** — the fetch time, in `Registry.local12` words (Discord: a native `<t:…:R>` stamp).
- **cost to make** — the sum of the reagents at their lowest prices. Incomplete when a reagent has none listed:
  "Cost to make: at least 12g 40s — 1 reagent has none listed."
- Money reads as gold, silver, copper ("48g 99s 98c"; zero parts dropped; one formatter in code, one on the site
  fed by the server's words where a sentence is printed).

## 4. What gets built

### 4.1 The market (owner)

- Gated on data: `profiles/<version>/integrations.yaml` gains an `auctions:` block (the dynamic namespace pattern,
  and which house names are faction houses). A version without it has no price feature at all (tbc behaves as today).
- It also needs the server's guild to be linked (§5.64 D1): the guild's ruleset and faction come from its armory page.
  The stored link holds the faction but not the ruleset — the ruleset is read from the armory when the owner opens
  the picker (and stored with the choice).
- The picker lists the houses of that ruleset whose name is the guild's faction's and which have listings now
  (probe: up to four). Beside each: how many listings, and the lowest price of three reference items (profile data:
  `auctions.reference_items`, well-known stackables) so the owner can match it against the game.
- Surfaces: the Config page (next to the server's guild card) and Discord inside the existing `/gm config armory`
  hub — `/gm config` is at Discord's 25-subcommand limit, so no new subcommand.
- Owner only (`Registry.is_owner_id`), one commit, one audit line, a confirm sentence under the Confirm rule.
  Clearing the choice turns prices off. Unlinking the server's guild clears the choice too.
- Stored on `GuildConfig` beside the armory link: the ruleset id, the house id, the house's name, who and when.

### 4.2 The price table

- One fetch per guild per hour of the selected house: its listings and its stackables feed. Per item id the table
  keeps the lowest unit price (listings: buyout ÷ quantity, rounded up; stackables: unit price) and the total
  quantity listed, plus the fetch's time (the feed's Last-Modified when given).
- A new boundary module in the style of `armory.py`: blocking calls run in `asyncio.to_thread`, the shared token and
  rate limiter (`battlenet.py`), every answer validated, an unexpected shape keeps the last good table and warns ops
  at most once per guild per hour. A conditional request when the feed allows it; an unchanged feed does no work.
- Kept at `out/auctions/<guild key>/prices.json`, swapped in whole. Loaded at start; missing = no prices until the
  first fetch. Two guilds on the same house share one fetch.
- The hourly check hangs off the scheduler's minute loop like `armory_tick`. No DM, no notice, no ops line on success.
- Item variants: a listing's item may carry bonus ids (random-suffix gear). The table is keyed by the plain item
  id; this version makes no attempt to price variants apart (non-goal).

### 4.3 Item names

- The feeds carry item ids only. Names come from: the recipe list (what a recipe makes, its reagents), the loot
  item data, and — for an id nothing local names — Blizzard's item endpoint in the static namespace, looked up
  lazily and cached under `out/` (never at request time on a Discord interaction; an unnamed id is simply not
  searchable until named). The builder decides the pace of that background naming.

### 4.4 Cost to make (Professions page)

- Where a recipe's reagents show (the finder's hits, "What the guild makes"), each reagent shows its lowest price
  × the quantity needed, and the recipe shows the cost to make. What the recipe makes shows its own lowest price
  and N listed. No profit figure (non-goal).
- A reagent with none listed says so; the sum then reads "at least …" and counts the missing reagents.
- "Prices as of <when>" once per card; stale after six hours (C3) in the same line.
- No market selected (C8): no price columns; one line for the owner ("Pick the guild's auction house on the Config
  page to see prices."), nothing for others beyond the absence.
- Discord: `/professions who` adds the cost to make to a recipe's answer when prices are on.

### 4.5 The price check

- Site: a price box on the Professions page — part of an item's name → matching items (25 + the total), each with
  lowest price, N listed, or "none listed".
- Discord: `/price` (every member, ephemeral, item autocomplete from the same search; names only, no model).
- Plain text (C5): the help answer gets a `prices` section when the question names an item and asks about
  price / cost / "how much" — code computes the rows, the model only words them. Same snapshot pattern as the
  professions section.
- MCP: one read tool over the same route.

### 4.6 Wowhead links

- `integrations.yaml: wowhead_item_url` for Forever becomes `https://www.wowhead.com/forever/item={id}` (confirmed
  by the owner, C12). The bot itself never fetches Wowhead (project rule).
- The template reaches the site through `/api/meta`. An item name with an id becomes a link (new tab, no referrer)
  on: the Professions page (what a recipe makes, reagents, the price check), the loot pages (Tonight, History, the
  loot log, My loot, Items) and wishlists. An item known only by name stays text. A version with no template: no links.
- One shared component for an item's name so every page links the same way. No Wowhead tooltip script (it would
  load third-party code into the app).

## 5. Non-goals

Price history or trends; sale-price estimates; profit per craft; alerts or watch lists; vendor prices; pricing
random-suffix variants apart; the neutral house; prices in loot scoring or any decision the bot makes.

## 6. Anti-goals

- A price that looks more certain than it is: every price is "lowest listed, as of <when>", never "worth".
- Any DM, notice or channel post from a fetch.
- A fetch on a member's request path (a page load or a slash command never waits on Blizzard).
- Scraping Wowhead or loading its scripts.
- Price data in the data repo's git history.

## 7. Constraints

- CLAUDE.md principles: Game version is data (no `if forever:`), Code computes / Claude explains, Copy, Confirm,
  Wizard, DM, Iconography (no text label beside an in-game icon — prices sit in their own column or line), Clock.
- Several guilds per process: state keyed by guild key; route through `guild_routing.need`.
- Tests never call Blizzard: recorded, anonymised responses under `tests/fixtures/`, an injectable fetch.
- No token in a log, ops line or error. Discord interactions answer within 3 s.
- `docs/manual.md` changes in the same commit as any behaviour a person can see; the parity matrix gets its rows.

## 8. What the API gives (probed 2026-11-13 with the bot's own credentials)

- Namespace `dynamic-forever-us`. `GET /data/wow/ruleset/index` → four rulesets (ids 1, 2, 3, 15 — the same ids the
  armory's guild page reports).
- `GET /data/wow/ruleset/{id}/auctions/index` → 15 houses `{id, name}`: five markets × Alliance / Horde /
  Blackwater. On ruleset 1: two large markets (~100,000–120,000 listings per faction house), two small (~20,000–25,000),
  one empty. Nothing ties a guild to a market.
- `GET /data/wow/ruleset/{id}/auctions/{house}` → `auctions[]` of `{id, item {id, bonus_lists?}, buyout, quantity,
  time_left}` (copper; 8.8 MB, under a second) plus a `commodities` link.
- `GET …/{house}/commodities` → `auctions[]` of `{id, item {id}, quantity, unit_price, time_left}` (~90,000 rows).
- An empty house answers without an `auctions` key. All houses carried a Last-Modified within the same 15 seconds.
- The same item's lowest price differed by more than 2× between markets, which is why D3 matters.
- No seller, no sales, no history. The API is undocumented and may change: every shape is validated.

## 9. Left to the builder

The module and route names; the table's file format; how the picker's sample prices are fetched (on open, in a
thread, behind the wizard's working screen); the item-naming pace (§4.3); the exact placement of price text on the
Professions cards within the Iconography and Copy rules; whether bid-only listings exist in the feed at all (C2 is
the rule if they do); the task breakdown.
