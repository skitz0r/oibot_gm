# Prices tab, watch lists and price history — design

Date: 2026-11-14. Status: draft for the owner's approval.
Builds on: auction prices (`docs/superpowers/specs/2026-11-13-auction-prices-design.md`, `docs/design.md` §5.65).
Design home when built: `docs/design.md` (a new §5.66).

## 1. Why

Prices are live: the owner picks the guild's auction house, the bot reads it every hour, and the Professions page
shows each recipe's cost to make. Two things are missing for a member who buys and sells:

- A place to look up one material's price that isn't inside the Professions page.
- A way to keep an eye on the handful of items they care about, and to see whether today's price is high or low
  against the last days and weeks.

The bot keeps only the latest table today, so it cannot say how a price has moved. This design adds a Prices tab,
a personal watch list, and a kept history of price changes.

## 2. Decisions (the owner's, 2026-11-14)

- **D1 A Prices tab for every member:** a search for one item's price, and under it the member's own watch list.
  No browse table, no categories.
- **D2 Search stays on both pages.** The Professions page keeps its *What does it go for?* box as it is; the Prices
  tab has the same search.
- **D3 A watch list per member.** A search result can be watched. The list is the member's alone.
- **D4 History is kept for every item, not only watched ones,** as changes (a row only when an item's lowest price or
  quantity listed differs from the last one kept), provided the footprint is light on the host. Measured 2026-11-14:
  5,899 priced items on the Horde house, about 100 KB per whole table, 121 GB free on the host (§8).
- **D5 Discord, through the one `/price` command:** with an item it answers as today plus a Watch button; with no
  item it shows the member's watch list. Every answer is visible to the asker only.

Calls made by the designer (the owner saw C1–C5 in chat and said "sounds good" / "yes continue"; C6–C12 are new
here — flagged so they can be overruled):

- C1 No price alerts. Nothing pings, DMs or posts when a watched price moves (the DM rule). The list is opened, not pushed.
- C2 Retention: every change is kept for 90 days; older than that, one lowest and one highest price per item per
  day. Nothing is kept past one year. The chart's longest span is one year, so everything kept can be read.
- C3 The same list on both sides: an item watched in Discord shows on the site and the other way round.
- C4 Search covers named items only, as today (§5.65 C6).
- C5 A watched item's history shows on the site as a small chart plus the lowest and highest price of the last 7 and
  30 days in words; Discord shows the words only.
- C6 History shows for watched items only. A search result shows today's price and a Watch button; watching it is
  what opens its history. (The history exists for every item, so this can be widened later without new data.)
- C7 A watch list holds at most 25 items (Discord's select holds 25). The 26th is refused in one sentence.
- C8 Watching and stopping are instant — no confirm screen. Each is the member's own preference and is undone by
  one press.
- C9 The watch list lives on the member's own record in the data repo as a new field of item ids (on the member,
  not on a character — the wishlist is per character and is not the pattern here). The history lives under `out/`
  only, never the data repo.
- C10 History belongs to an auction house, not to a guild. Two guilds on one house share it; a guild whose owner
  picks another house reads that house's history from then on (empty at first). Turning prices off deletes nothing.
- C11 Nobody else reads a member's watch list on any surface: no officer view, no MCP tool, not in the help answer.
- C12 An item with nothing listed can still be watched. Its line reads *none listed*, and a stretch with nothing
  listed is a gap in its chart, never a zero.
- C13 Registered members only. Anyone in the server can search, as today; watching needs a record ("Register first
  with /register to keep a watch list."). A watch never creates a record, so nobody appears on the Members page
  because they watched something, and the must-apply gate is not bypassed.
- C14 A watch is written quietly: no change-log line in any channel, no ops line, and the data repo's commit message
  says only that the member's watch list changed — never which item.
- C15 Items are added only while prices show (fresh or stale): with prices off or a day old the search finds
  nothing, as today, so there is nothing to press Watch on. A list made earlier still shows its names (§4.2).
- C16 History shows only while prices show. With prices off or more than a day old, the list shows names and the
  status sentence — no chart, no history sentences.
- C17 The chart is drawn by the site's own code (inline SVG). No chart library is added.
- C18 The Watch button and the list's select live on an ephemeral answer and need not survive a restart; a press
  after one says so in the existing "The bot restarted" way and changes nothing.

## 3. The words (Copy rule)

- **Prices** — the tab. **Watch** / **Stop watching** — the buttons. **Your watch list** — the list's heading.
- **Lowest price**, **N listed**, **none listed**, **Prices as of <when>** — as §5.65, unchanged.
- **Lowest in the last 7 days: 12s 40c · highest: 19s 2c** — the history's words (and the same for 30 days). One
  formatter for money (`auctions.money`), the server sends the sentences.
- **The bot has kept prices since <day>.** — shown when the history is shorter than the span asked for, so a short
  history doesn't read as a calm one.
- Empty list: "You aren't watching anything yet. Search for an item above and press Watch." (Discord: "…Use /price
  with an item and press Watch.")
- Full list: "Your watch list holds 25 items. Stop watching one first."
- Never: snapshot, delta, row, history table, house id.

## 4. What gets built

### 4.1 The history (kept by the bot)

- After every hourly read that brings a new table from Blizzard and is taken as the guild's table, the bot compares
  it with the last values kept for that auction house and keeps one row per item that changed: the item, the feed's
  time, the lowest price and the quantity listed. An item that had listings and now has none gets a row saying so.
  Not on an unchanged feed, not when a guild adopts another guild's read of the same house (that read already wrote
  it — once per house per feed time), and not for a read thrown away because the market changed while it ran.
- One small database file per auction house under `out/auctions/` (C10), named from the house's full key (game
  namespace, ruleset, house). Written in a thread, off every request path. A failed write keeps the price table
  working and warns ops at most once per hour, in the feed of the guild whose read it was.
- The first read after this ships keeps every listed item once (the starting point).
- Once a day the bot folds rows older than 90 days into one lowest and one highest price per item per day, and
  drops anything older than one year (C2). The fold runs over every history file under `out/auctions/`, whether its
  house is still picked or not; a file with nothing left in it is deleted. So a house nobody reads any more is gone
  a year after its last read.
- What a span's figures mean: the price at the span's start is the last one kept before it, carried forward (a
  price unchanged for ten days still counts in the last 7). **Lowest** and **highest** are the lowest and the highest
  of the hourly lowest prices in the span — never the dearest listing. Time with nothing listed counts for neither;
  a span with nothing listed throughout reads "none listed in the last 7 days". "Kept since" is the house's first
  kept read.
- Gated like prices: only where the profile has `auctions:` and a house is picked. `tbc` keeps nothing.
- No DM, no notice, no channel post, no ops line on success.

### 4.2 The watch list (per member)

- Stored on the member's record: the watched item ids, newest first, at most 25 (C7, C9). Two verbs — watch and
  stop watching — each the member's own (decided by the actor's Discord id, never a name), each one commit,
  each harmless to repeat. No officer does it for someone else.
- Any named item can be watched, listed right now or not (C12). An id the bot can't name is refused.
- A member needs no character to keep a list. A list goes with the member's record if the record is removed.
- Game-version gate: the verbs exist only where the profile has auction houses. With prices off (no house picked,
  or prices more than a day old) the list still shows its items' names with the usual status sentence, and no prices.

### 4.3 The Prices tab (site, every member)

- In the left rail for every member, where the game version has auction houses (`/api/meta.prices.market` is the
  existing flag). On `tbc` the tab is absent.
- **Search** at the top: the same search as the Professions box (D2) — part of a name → up to 25 items with lowest
  price and N listed (or *none listed*) and how many more match. Each row has **Watch** (or **Stop watching** when
  it is on the list). Item names are links, as everywhere (ItemName).
- **Your watch list** below: one row per item — name, lowest price, N listed — opened in place to show its history
  (C5): a chart of the lowest price with a 7 days / 30 days / 90 days / 1 year switch, and under it the two sentences of §3.
  A gap where nothing was listed (C12). Times on the chart follow the Clock rule.
- *Prices as of <when>* once on the page; the stale tail after six hours; with prices off, the owner's one line and
  nothing else for a member besides their list's names.
- A phone shows the same things as cards (the list a card per item).
- The `prices` live topic reloads the tab when a new table arrives.
- The Professions page is unchanged.

### 4.4 Discord (`/price`)

- `item` becomes optional.
- **With an item** (picked from the autocomplete or typed): today's answer, and when it names exactly one item, a
  **Watch** button — **Stop watching** when it is already on the list. A press edits the answer in place to say what
  happened ("You're watching Plaguebloom." / "You've stopped watching Plaguebloom.") and flips the button.
- **With no item**: *Your watch list* — one line per item (name, lowest price, N listed or *none listed*), the
  *Prices as of* line once, and one select of the items. Picking one shows that item alone: its price line, the two
  history sentences (C5), **Stop watching** and **Back**.
- Everything is ephemeral and the asker's only. Nothing is typed for a parser (the Wizard rule holds: the item is
  prose matched by search, the rest are buttons and a select). No model call. No Blizzard call on the request path.
- On `tbc`: "This game version has no auction houses.", as today.

### 4.5 Other surfaces

- MCP: nothing new (C11). The parity matrix records the watch list as the member's own, site and Discord only, with
  a written reason per missing row.
- Plain text / the help answer: unchanged. The manual gains the Prices tab, the watch list and `/price`'s two forms.

## 5. Non-goals

Price alerts of any kind; a browse table or item categories; a profit figure; sharing or comparing watch lists;
an officer's or the owner's view of who watches what; a chart image in Discord; history for a version without
auction houses; history of anything but the lowest price and the quantity listed; sale prices (Blizzard gives none).

## 6. Anti-goals

- A DM, ping, notice or channel post because a price moved.
- Another member, an officer or a tool reading someone's watch list.
- Price history in the data repo's git history.
- A page load or a slash command waiting on Blizzard, or on a slow history write.
- A history that grows without bound on the host.
- A short history read as a quiet one (the "kept since" sentence exists for this).

## 7. Constraints

- CLAUDE.md's principles and conventions: Game version is data, Code computes / Claude explains (no model anywhere
  here), Copy, Confirm (no confirm sentences are added — C8), Wizard, DM, Iconography, Clock; several guilds per
  process (state keyed by guild key, history by auction house); slow work in `asyncio.to_thread`; Discord answers in 3 s.
- Everything §5.65 fixed stays: the lowest price is the price; six hours stale; a day old hidden; no profit figure;
  the bot never fetches Wowhead.
- Tests never call Blizzard and never use a real member's, character's or guild's name.
- `docs/manual.md` changes in the same commit as any behaviour a person can see; the parity matrix gets its rows.
- No new runtime dependency: SQLite ships with Python; the chart is inline SVG (C17), legible in the light and the
  dark theme.

## 8. Footprint (measured 2026-11-14 on the host)

- One whole table: 5,899 items, about 100 KB as JSON.
- Worst case, every item changing every hour: 5,899 × 24 ≈ 140,000 rows a day; over the 90 days kept in full that
  is about 13 million rows, a few hundred MB in SQLite. Real churn is a fraction of that.
- Past 90 days: at most two numbers per item per day, for nine more months.
- Free disk: 121 GB. The builder records the file's real size after a week in the ledger / the manual's operator notes.

## 9. Left to the builder

The module, route and table names; the database's schema and indexes; where the daily fold runs; how the chart is
drawn (within the site's existing stack and its light and dark themes); the Discord views' classes and their
behaviour after a restart (the existing wizard core and pending-views pattern); the exact layout of the tab within
the Copy and Iconography rules; the task breakdown.
