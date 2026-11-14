# Prices tab, watch lists and price history — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every member gets a Prices tab (search + their own watch list with price history) and `/price` gains Watch and the list; the bot keeps every price change per auction house.

**Architecture:** A new blocking store `price_history.py` (one SQLite file per auction house under `out/auctions/history/`) is fed by `discord_auctions.refresh` after each new table and folded daily. The watch list is a new `Member.watching` field written quietly by two Registry verbs. `prices.py` owns every word and read; `web/api_prices.py`, a new Discord flow and a new React page are thin callers.

**Tech Stack:** Python 3.12 (`sqlite3` from the standard library), discord.py, FastAPI, React + Mantine (inline SVG chart, no new dependency).

**Spec:** `docs/superpowers/specs/2026-11-14-price-watch-design.md` (approved 2026-11-14). It builds on `docs/superpowers/specs/2026-11-13-auction-prices-design.md` (§5.65).

## Global Constraints

- CLAUDE.md's Principles and Conventions bind every task (Game version is data, Code computes / Claude explains — no model call anywhere here, Copy, Confirm, Wizard, DM, Iconography, Clock; several guilds per process; slow work in `asyncio.to_thread`; Discord answers within 3 s).
- Gate: every behaviour here on `profile.auctions`; `tbc` behaves exactly as today — a test proves it. Prices and history SHOW only in state `fresh` or `stale` (C15, C16).
- History is keyed by the auction house's full key `(namespace, ruleset_id, house_id)` (`discord_auctions._house_key`), never by guild (C10). Files live under `out/auctions/history/` only — never the data repo.
- A history row is kept only when an item's lowest price or quantity listed differs from the last kept; an item that had listings and now has none gets a row with no price. Written once per house per feed time: only for a new table from Blizzard that is taken as the guild's table — not on a 304, not on a shared read being adopted, not for a read discarded because the market changed.
- Retention (C2): every change for 90 days; older → one lowest and one highest per item per day; nothing older than 365 days; the fold covers every file under `out/auctions/history/` and deletes an empty one.
- A span's figures: the price at the span's start is the last kept before it, carried forward; lowest / highest are of the hourly lowest prices; time with nothing listed counts for neither.
- The watch list: `Member.watching`, item ids, newest first, at most 25 (`WATCH_MAX`); registered members only — a watch NEVER creates a member record (C13); written with `Registry.save_quiet`, commit message `"<display name>: watch list changed"` — never the item (C14); no ops line, no change-log line, no notice, no DM.
- Nobody else reads a watch list: no officer route, no MCP tool, not in the help answer (C11). The watch routes refuse an MCP caller.
- The words (spec §3), verbatim: **Watch**, **Stop watching**, **Your watch list**, "Lowest in the last 7 days: 12s 40c · highest: 19s 2c", "The bot has kept prices since <day>.", "You aren't watching anything yet. Search for an item above and press Watch." (Discord: "You aren't watching anything yet. Use /price with an item and press Watch."), "Your watch list holds 25 items. Stop watching one first.", "Register first with /register to keep a watch list.", "You're watching <name>.", "You've stopped watching <name>." Never snapshot, delta, row, history table, house id where a person reads (`tests/test_copy.py` stays at 0 hits). Money through `auctions.money` only.
- No Blizzard request and no history WRITE on a member's request path. History reads are SQLite reads in `asyncio.to_thread`.
- No DM, notice, channel post or ops line because a price moved or a table was kept. A failed history write keeps the price table working and warns ops at most once per hour in the feed of the guild whose read it was (the existing `_emit_warn`).
- Tests never call Blizzard, never read `.env`, never use the network. Never a real member's, character's or guild's name in code, tests, fixtures or docs.
- No new runtime dependency (Python or npm). The chart is inline SVG, legible in the light and the dark theme.
- The Professions page's price box is unchanged (D2).
- `docs/manual.md` changes in the same commit as a behaviour a person can see. `docs/design.md` §5.66 and CLAUDE.md's Layout entries are written in Task 6.
- The parity matrix passes at every commit: Task 4 lists the Discord half of its new PRICES rows in `PRICES_DISCORD_PENDING` (dated, naming Task 5); Task 5 empties it.

## Review Focus

1. The same feed time recorded twice (a restart replays the stored table; two guilds on one house) → no second set of rows. Test in Task 1.
2. A feed time earlier than the last one kept (a clock or header oddity) → nothing is written, nothing raises. Test in Task 1.
3. An item delisted then relisted → a gap in its points, lowest / highest ignore the gap, and a span with nothing listed throughout reads "None listed in the last 7 days." Tests in Tasks 1 and 3.
4. A watched id the bot can no longer name (`out/` wiped) → the list still shows a line ("An item the bot can't name yet") with Stop watching; nothing crashes. Tests in Tasks 3 and 4.
5. A corrupt or locked history file → the hourly read still saves the table and warns once; the history read answers "no history yet", never a 500. Tests in Tasks 1 and 4.
6. The owner switches auction house → the watched items read the new house's history; before its first kept read the sentence is "The bot hasn't kept any prices for this auction house yet." Test in Task 3.
7. Two presses at once at 24 items (site and Discord) → at most 25 kept, the second refused in the full-list sentence. Test in Task 2.

---

### Task 1: The history store and its feed

**Files:**
- Create: `src/oibot_gm/price_history.py`, `tests/test_price_history.py`
- Modify: `src/oibot_gm/discord_auctions.py` (the keep after a new table; the daily fold from `auctions_tick`), `tests/test_auction_table.py`

**Interfaces:**
- Consumes: `auctions.Price(low, qty)`; `discord_auctions._house_key`, `_save`, `_emit_warn`, `_now`, `OUT`.
- Produces (all BLOCKING, callers use `asyncio.to_thread`; `key = (namespace: str, ruleset_id: int, house_id: int)`; times are aware UTC datetimes):
  - `price_history.KEEP_FULL = timedelta(days=90)`, `KEEP_DAILY = timedelta(days=365)`
  - `price_history.root() -> Path` (= `discord_auctions.OUT / "history"`, read at call time) and `path(key) -> Path` (a file name built from the three parts, letters, digits and dashes only)
  - `Point(at: datetime, low: int | None, high: int | None, qty: int | None)` — a change: `high == low`; a folded day: `at` = the day's start, `qty` None; `low is None` = nothing listed from `at`
  - `record(key, at, prices: dict[int, Price]) -> int` — rows kept; 0 when `at` is not later than the last kept feed time for the house
  - `points(key, item_id, since, until) -> list[Point]` — oldest first; the first point is the last one kept before `since`, moved to `since` (carried forward); `[]` when the item has nothing kept by `until`
  - `Span(low: int | None, high: int | None)`; `span(key, item_id, days: int, now) -> Span` — both None when nothing was listed throughout
  - `kept_since(key) -> datetime | None` — the house's first kept feed time
  - `fold(now) -> None` — every file under `root()`: rows older than `KEEP_FULL` → one (lowest, highest) per item per UTC day; days older than `KEEP_DAILY` dropped; a file left with nothing is deleted
  - `HistoryError(Exception)` — raised by every function above for a file that can't be opened or read (wraps `sqlite3.Error` / `OSError`)
  - `discord_auctions.history_key(reg) -> tuple | None` — the selected house's key, None without a market
  - `discord_auctions.HISTORY_FAILED = "Keeping the price history stopped. The prices themselves still update."`

- [ ] **Step 1: Write the failing store tests** in `tests/test_price_history.py` (a `tmp_path` root via `monkeypatch.setattr(discord_auctions, "OUT", tmp_path)`; `K = ("dynamic-forever-us", 1, 6)`; `T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)`; `P = auctions.Price`):

```python
def test_first_record_keeps_every_item_then_only_changes():
    assert ph.record(K, T0, {1: P(100, 5), 2: P(50, 1)}) == 2
    assert ph.record(K, T0 + H, {1: P(100, 5), 2: P(40, 1)}) == 1          # item 1 unchanged
    assert ph.record(K, T0 + 2 * H, {1: P(100, 7), 2: P(40, 1)}) == 1      # quantity alone is a change

def test_the_same_or_an_earlier_feed_time_writes_nothing():
    ph.record(K, T0 + H, {1: P(100, 5)})
    assert ph.record(K, T0 + H, {1: P(90, 5)}) == 0
    assert ph.record(K, T0, {1: P(80, 5)}) == 0
    assert [p.low for p in ph.points(K, 1, T0, T0 + 3 * H)] == [100]

def test_delisted_then_relisted_is_a_gap():
    ph.record(K, T0, {1: P(100, 5)}); ph.record(K, T0 + H, {}); ph.record(K, T0 + 2 * H, {1: P(120, 2)})
    assert [(p.low, p.qty) for p in ph.points(K, 1, T0, T0 + 3 * H)] == [(100, 5), (None, 0), (120, 2)]
    assert ph.span(K, 1, 1, T0 + 3 * H) == ph.Span(low=100, high=120)

def test_a_price_unchanged_before_the_span_is_carried_forward():
    ph.record(K, T0, {1: P(100, 5)})
    now = T0 + timedelta(days=10)
    pts = ph.points(K, 1, now - timedelta(days=7), now)
    assert [(p.at, p.low) for p in pts] == [(now - timedelta(days=7), 100)]
    assert ph.span(K, 1, 7, now) == ph.Span(low=100, high=100)

def test_nothing_listed_all_span_and_an_unknown_item():
    ph.record(K, T0, {1: P(100, 5)}); ph.record(K, T0 + H, {})
    assert ph.span(K, 1, 7, T0 + timedelta(days=9)) == ph.Span(None, None)
    assert ph.points(K, 99, T0, T0 + H) == [] and ph.span(K, 99, 7, T0 + H) == ph.Span(None, None)

def test_houses_are_kept_apart_and_kept_since():
    other = ("dynamic-forever-us", 1, 7)
    ph.record(K, T0, {1: P(100, 5)}); ph.record(other, T0 + H, {1: P(300, 1)})
    assert ph.kept_since(K) == T0 and ph.kept_since(other) == T0 + H and ph.kept_since(("x", 9, 9)) is None
    assert ph.path(K) != ph.path(other) and ph.path(K).parent == ph.root()

def test_fold_keeps_a_low_and_high_per_day_then_drops_a_year():
    old = T0 - timedelta(days=100)
    ph.record(K, old, {1: P(100, 5)}); ph.record(K, old + H, {1: P(140, 5)}); ph.record(K, old + 2 * H, {1: P(90, 5)})
    ph.record(K, T0, {1: P(110, 5)})
    ph.fold(T0 + timedelta(days=1))
    day = [p for p in ph.points(K, 1, old, old + timedelta(days=1)) if p.qty is None]
    assert [(p.low, p.high) for p in day] == [(90, 140)]
    assert [p.low for p in ph.points(K, 1, T0, T0 + H)][-1] == 110            # recent changes untouched
    ph.fold(T0 + timedelta(days=400))
    assert not ph.path(K).exists()                                            # nothing left: the file goes

def test_fold_carries_the_last_value_when_a_day_had_no_change():
    """A price set 100 days ago and never changed still reads after the fold (the fold never loses the standing price)."""
    ph.record(K, T0 - timedelta(days=100), {1: P(100, 5)})
    ph.fold(T0)
    assert ph.span(K, 1, 7, T0) == ph.Span(100, 100)

def test_a_broken_file_raises_history_error():
    ph.path(K).parent.mkdir(parents=True, exist_ok=True); ph.path(K).write_bytes(b"not a database")
    for call in (lambda: ph.record(K, T0, {1: P(1, 1)}), lambda: ph.points(K, 1, T0, T0 + H), lambda: ph.kept_since(K)):
        with pytest.raises(ph.HistoryError):
            call()
    ph.fold(T0)                                                               # the fold skips a file it can't read
```

- [ ] **Step 2: Run them** — `uv run pytest -q tests/test_price_history.py` → FAIL (no module).
- [ ] **Step 3: Implement `price_history.py`.** One connection per call (`sqlite3.connect(path, timeout=5)`, closed in `finally`), the schema created on first use, a write in one transaction. The builder picks tables and indexes; the lookup "last kept value per item" must not scan the whole file per `record` (keep a `last(item, low, qty)` table or an index that makes it one pass). `fold` logs and skips a file that raises.
- [ ] **Step 4: Run them** → PASS.
- [ ] **Step 5: Write the failing feed tests** in `tests/test_auction_table.py` (its existing fake client and guild helpers):

```python
async def test_a_new_table_is_kept_once_per_house(...):
    # two guilds on one house; guild A reads from Blizzard, guild B adopts the shared read
    # → price_history.kept_since(key) is the feed's time and record was called exactly once (spy on price_history.record)

async def test_a_not_modified_answer_keeps_nothing(...):       # a 304 → record not called
async def test_a_read_discarded_for_a_changed_market_keeps_nothing(...)
async def test_a_failed_keep_still_saves_the_table_and_warns_once(...):
    # record raises HistoryError → refresh returns "updated", table(reg) is the new one,
    # ONE ops warn == discord_auctions.HISTORY_FAILED, a second failure within the hour emits none
async def test_the_fold_runs_once_a_day_off_the_tick(...):
    # auctions_tick twice on one UTC day → price_history.fold called once (in a thread); the next day → again
async def test_tbc_keeps_no_history(...):                      # a tbc guild: no file under OUT / "history"
```

- [ ] **Step 6: Run them** → FAIL.
- [ ] **Step 7: Wire the feed.** In `refresh`, on the branch where `prices is not None`, after `await _save(reg, t)`: `await asyncio.to_thread(price_history.record, key, t.at, t.prices)` inside `try/except HistoryError` → `log.exception` + `_emit_warn(bot, reg, HISTORY_FAILED)`. The warn throttle must not swallow a read-failure warning's hour or the reverse: give `_emit_warn` an optional `kind` so each kind has its own hour. In `auctions_tick`, before the market check: when the UTC date differs from the module's last fold date, set it and start ONE background task running `price_history.fold` in a thread (kept in `_RUNNING`, failures logged). Add `history_key`. `reset_state()` clears the fold date.
- [ ] **Step 8: Run** `uv run pytest -q tests/test_price_history.py tests/test_auction_table.py` → PASS.
- [ ] **Step 9: The gate** (context file) and commit: `git commit -m "Price watch 1: the kept price history per auction house and its daily fold"`.

### Task 2: The watch list on the member's record

**Files:**
- Modify: `src/oibot_gm/registry.py`
- Create: `tests/test_watch_list.py`

**Interfaces:**
- Produces:
  - `registry.WATCH_MAX = 25`; `WATCH_FULL = "Your watch list holds 25 items. Stop watching one first."`; `WATCH_REGISTER_FIRST = "Register first with /register to keep a watch list."`
  - `Member.watching: list[int] = []` (newest first)
  - `Registry.watching(discord_id: int) -> list[int]` — `[]` for someone with no record (never creates one)
  - `Registry.watch(discord_id: int, item_id: int) -> bool` — True = added (put first); False = already there (no write). Raises `RegistryError(WATCH_REGISTER_FIRST)` without a record, `RegistryError(WATCH_FULL)` at 25, `RegistryError("This game version has no auction houses.")` (`auctions.NO_AUCTIONS_WORDS`) where `profile.auctions` is None
  - `Registry.unwatch(discord_id: int, item_id: int) -> bool` — True = removed; False = wasn't there (no write, no raise for no record)

- [ ] **Step 1: Write the failing tests** (the repo's scratch-store Registry helper used by `tests/test_market.py`):

```python
def test_watch_adds_newest_first_and_is_harmless_to_repeat(reg):
    assert reg.watch(UID, 13466) is True and reg.watch(UID, 8925) is True
    assert reg.watching(UID) == [8925, 13466]
    assert reg.watch(UID, 13466) is False and reg.watching(UID) == [8925, 13466]

def test_unwatch(reg):
    reg.watch(UID, 13466)
    assert reg.unwatch(UID, 13466) is True and reg.unwatch(UID, 13466) is False and reg.watching(UID) == []

def test_a_watch_never_creates_a_record(reg):
    with pytest.raises(RegistryError, match="Register first with /register to keep a watch list."):
        reg.watch(STRANGER, 13466)
    assert STRANGER not in reg.members and reg.watching(STRANGER) == [] and reg.unwatch(STRANGER, 1) is False

def test_the_26th_is_refused(reg):
    for i in range(25): reg.watch(UID, 1000 + i)
    with pytest.raises(RegistryError, match="Your watch list holds 25 items. Stop watching one first."):
        reg.watch(UID, 2000)
    assert len(reg.watching(UID)) == 25

def test_two_presses_at_24_keep_at_most_25(reg):
    for i in range(24): reg.watch(UID, 1000 + i)
    results = run_in_threads(lambda: try_watch(reg, UID, 3001), lambda: try_watch(reg, UID, 3002))
    assert len(reg.watching(UID)) == 25 and sorted(results) == ["full", "ok"]

def test_the_write_is_quiet(reg, monkeypatch):
    heard = []; monkeypatch.setattr(reg, "listeners", [lambda *a, **k: heard.append(a)])   # the change-log listeners
    reg.watch(UID, 13466)
    assert heard == []
    assert reg.store.log()[0].endswith(f"{reg.member(UID).display_name}: watch list changed")
    assert "13466" not in reg.store.log()[0]

def test_tbc_has_no_watch_list(tbc_reg):
    with pytest.raises(RegistryError, match="This game version has no auction houses."):
        tbc_reg.watch(UID, 1)

def test_an_old_member_file_loads_with_an_empty_list(reg): ...   # a file without the key → []
```

(Adapt `store.log()` / the listeners attribute to the names the Registry really uses; the assertions stay.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the field and the three methods under `config_lock` (people writes run in web worker threads and on the loop), saving with `save_quiet(m, "watch list changed")` — check what `save_quiet` prefixes and make the message read `"<display name>: watch list changed"`. A puppet (`Member.test`) is refused like no record.
- [ ] **Step 4: Run** → PASS. **Step 5: The gate**, commit: `"Price watch 2: the member's watch list (quiet, at most 25, never creates a record)"`.

### Task 3: The words and the reads (`prices.py`)

**Files:**
- Modify: `src/oibot_gm/prices.py`, `tests/test_prices.py`, `tests/test_copy.py` (only if a new module needs listing)

**Interfaces:**
- Consumes: Task 1's `price_history` + `discord_auctions.history_key`; Task 2's verbs.
- Produces:
  - `prices.SPANS: dict[str, tuple[int, str]] = {"7": (7, "7 days"), "30": (30, "30 days"), "90": (90, "90 days"), "365": (365, "1 year")}`
  - `prices.UNNAMED = "An item the bot can't name yet"`; `EMPTY_LIST_SITE`, `EMPTY_LIST_DISCORD` (the two sentences of Global Constraints); `NO_HISTORY_YET = "The bot hasn't kept any prices for this auction house yet."`
  - `prices.watch(reg, discord_id, item_id) -> str` → "You're watching <name>." (also when already watching). Raises `RegistryError(NO_MATCH)` for an id `names(reg)` lacks, and Registry's refusals. `prices.unwatch(reg, discord_id, item_id) -> str` → "You've stopped watching <name>." (an unnamed id: "You've stopped watching that item.")
  - `prices.watch_list(reg, discord_id) -> list[ItemPrice]` — the member's list in order; a name the bot lacks → `UNNAMED`; `low None, qty 0` when prices don't show
  - `prices.is_watching(reg, discord_id, item_id) -> bool`
  - `prices.history_sentences(reg, item_id, *, now=None) -> list[str]` (BLOCKING — reads the history) — `[]` when prices don't show; else, in order: the 7-day sentence, the 30-day sentence, and "The bot has kept prices since <day>." (`reg.local12(at, "%a %d %b")`-style day, the Clock rule) only when the history is shorter than 30 days; `[NO_HISTORY_YET]` when the house has none; a `HistoryError` → `[NO_HISTORY_YET]`
    - a span's sentence: "Lowest in the last 7 days: 12s 40c · highest: 19s 2c" / "None listed in the last 7 days." (30: "the last 30 days")
  - `prices.chart(reg, item_id, span: str, *, now=None) -> dict` (BLOCKING) → `{"span": "30", "points": [{"t": <epoch seconds>, "when": <local12 words>, "low": <copper | None>, "high": <copper | None>, "words": "12s 40c" | "none listed"}], "low": <copper | None>, "high": <copper | None>, "low_words": str, "high_words": str}`; an unknown span → `RegistryError("That isn't one of the spans.")`; no history / prices not shown / `HistoryError` → `points: []`

- [ ] **Step 1: Write the failing tests** in `tests/test_prices.py` (its existing table helpers; history seeded through `price_history.record` under a tmp `OUT`):

```python
def test_watch_and_unwatch_sentences(...):
    assert prices.watch(reg, UID, 13466) == "You're watching Plaguebloom."
    assert prices.watch(reg, UID, 13466) == "You're watching Plaguebloom."
    assert prices.unwatch(reg, UID, 13466) == "You've stopped watching Plaguebloom."
    with pytest.raises(RegistryError, match="No item the bot knows has that name."): prices.watch(reg, UID, 999_999)

def test_watch_list_rows_in_order_with_prices(...)            # [(8925, "Crystal Vial", 640, n), (13466, "Plaguebloom", …)]
def test_watch_list_without_prices_shows_names_only(...)      # state off / old → low None, qty 0, names kept
def test_a_watched_id_the_bot_cannot_name(...):               # → name == prices.UNNAMED; unwatch → "You've stopped watching that item."

def test_history_sentences(...):
    # 40 days of history, lowest 1240 / highest 1902 in the last 7, 1100 / 2500 in the last 30
    assert prices.history_sentences(reg, 13466, now=NOW) == [
        "Lowest in the last 7 days: 12s 40c · highest: 19s 2c", "Lowest in the last 30 days: 11s · highest: 25s"]

def test_a_short_history_says_since_when(...):                # 3 days kept → a third sentence "The bot has kept prices since Wed 11 Nov."
def test_nothing_listed_in_a_span(...):                       # → "None listed in the last 7 days."
def test_a_new_house_has_no_history_yet(...):                 # the owner picks another house → [prices.NO_HISTORY_YET]
def test_history_is_hidden_when_prices_are(...):              # state old / off → [] and chart(...)["points"] == []
def test_a_broken_history_file_reads_as_none(...):            # → [prices.NO_HISTORY_YET], chart points []

def test_chart_points(...):
    c = prices.chart(reg, 13466, "7", now=NOW)
    assert c["span"] == "7" and [p["low"] for p in c["points"]][:2] == [1500, None] and c["points"][1]["words"] == "none listed"
    assert all(" AM" in p["when"] or " PM" in p["when"] for p in c["points"])       # the Clock rule
    with pytest.raises(RegistryError): prices.chart(reg, 13466, "14", now=NOW)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q tests/test_prices.py tests/test_copy.py` → PASS.
- [ ] **Step 5: The gate**, commit: `"Price watch 3: the watch list's and the history's words and reads"`.

### Task 4: The site's routes

**Files:**
- Modify: `src/oibot_gm/web/api_prices.py`, `tests/test_prices_web.py`, `tests/test_parity_matrix.py`, `tests/test_mcp.py`

**Interfaces:**
- Consumes: Task 3.
- Produces (all need a logged-in server member; 404 `auctions.NO_AUCTIONS_WORDS` where `profile.auctions` is None; POSTs carry `X-Requested-With: oibot`; every one refuses an MCP caller with 403 "A watch list is its member's own." — the pattern `api_guides` uses for "never MCP"):
  - `GET /api/prices/find` rows gain `"watching": bool` (false for someone with no record)
  - `GET /api/prices/watching` → `{"rows": [{id, name, low_words, qty, words}], "words": <status sentence>, "shown": bool, "max": 25, "empty": prices.EMPTY_LIST_SITE, "registered": bool}`
  - `POST /api/prices/watch` body `{"id": int, "on": bool}` → `{"message": str, "rows": [...]}`; a refusal → 400 with the sentence
  - `GET /api/prices/history?id=&span=` → `{"id", "name", "spans": [{"id": "7", "label": "7 days"}, …], **prices.chart(...), "sentences": prices.history_sentences(...)}`; an item not on the caller's list → 403 "Watch the item first to see its history." (C6); an unknown span → 400; both history reads in ONE `asyncio.to_thread`
- Parity rows in `PRICES`: `watch_list` (`GET /api/prices/watching`), `watch` and `unwatch` (`POST /api/prices/watch`), `history` (`GET /api/prices/history`) — each with a `PRICES_MCP_EXEMPT` reason ("a watch list is its member's own: no tool reads or changes one (C11)") and a `PRICES_DISCORD_PENDING` entry "2026-11-14: Task 5 lands /price's Watch button and list". Remove the final `assert not PRICES_DISCORD_PENDING` line's "(Task 6)" wording so it reads for this plan; Task 5 restores the empty dict.

- [ ] **Step 1: Write the failing tests** in `tests/test_prices_web.py` (its client helpers):

```python
def test_watch_then_list_then_stop(client): ...            # POST on → message "You're watching Plaguebloom.", rows has it; GET watching; POST off
def test_find_rows_say_whether_you_watch(client): ...
def test_history_of_a_watched_item(client): ...            # spans == the four, sentences == prices.history_sentences, points non-empty
def test_history_of_an_item_you_dont_watch_is_refused(client): ...   # 403 "Watch the item first to see its history."
def test_an_unregistered_server_member_can_search_but_not_watch(client): ...   # find 200, watching {"rows": [], "registered": False}, POST 400 the register sentence
def test_the_full_list_refusal(client): ...                # 400 "Your watch list holds 25 items. Stop watching one first."
def test_another_member_never_reads_your_list(client): ...  # B's GET watching has none of A's; no route takes a uid / member parameter
def test_mcp_is_refused_on_every_watch_route(mcp_client): ...   # 403 on the three routes; find still answers, without "watching": true
def test_a_broken_history_file_is_not_a_500(client): ...   # 200, points [], sentences [NO_HISTORY_YET]
def test_an_unnamed_watched_item_lists_and_stops(client): ...
def test_tbc_has_none_of_it(tbc_client): ...               # 404 on all four
def test_no_watch_ever_reaches_ops_or_a_notice(client, ops): ...
```

and in `tests/test_mcp.py`: no tool name contains "watch".

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (thin: no rule in a route). **Step 4: Run** `uv run pytest -q tests/test_prices_web.py tests/test_parity_matrix.py tests/test_mcp.py` → PASS.
- [ ] **Step 5: The gate**, commit: `"Price watch 4: the watch list and history routes; parity rows"`.

### Task 5: Discord — `/price` with Watch and the list

**Files:**
- Create: `src/oibot_gm/flows_prices.py`, `tests/test_price_watch_discord.py`
- Modify: `src/oibot_gm/discord_registry.py` (`/price`: `item` optional; it opens the flow), `src/oibot_gm/wizard_flows.py` (import), `tests/test_parity_matrix.py`, `tests/test_prices_discord.py`, `docs/manual.md`

**Interfaces:**
- Consumes: Task 3; the wizard core (`wizard.Wizard`, `@flow`, `Wizard.show/working/refuse`, owner-only presses); `tests/wizard_harness.py`.
- Produces: `FLOWS["price"]` (flow `price`), opened by `/price` with `item: str | None`:
  - **with an item**: the lines of `prices.price_answer` as today; when the text is `#<id>` of a named item or the search matches exactly one item, a **Watch** / **Stop watching** button (by `prices.is_watching`). A press calls `prices.watch` / `prices.unwatch`, puts the returned sentence as the screen's first line and flips the button. A refusal (not registered, full list) is shown as that sentence with the button unchanged. No button when prices don't show.
  - **with no item**: **Your watch list** — one line per item (`prices.price_words`), the status sentence once (Discord's `<t:…:R>` stamp), one select of the items (label = the name; ≤ 25). Empty → `prices.EMPTY_LIST_DISCORD`. Picking one → that item's screen: its price line, `prices.history_sentences` (read in a thread behind `Wizard.working` only if the read is slow — the builder measures; it must answer within 3 s either way), **Stop watching** and **Back**.
  - ephemeral, the asker's only; `allowed_mentions` none; every screen ≤ 2,000 characters through the existing whole-line cut (`flows_armory.bounded`); item names through `discord.utils.escape_markdown`.
  - on a version without auction houses: "This game version has no auction houses." as today; unregistered with no item: `WATCH_REGISTER_FIRST`.
  - after a restart a press says the wizard core's "The bot restarted; nothing was changed" (C18) — no persistent `DynamicItem`.
- `PRICES_DISCORD_PENDING` back to `{}`; the four rows gain `"discord": "slash:price"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_price_watch_discord.py` (the harness drives the flow and lints every screen):

```python
async def test_price_with_one_item_offers_watch_and_flips(...)     # Watch → first line "You're watching Plaguebloom.", button "Stop watching"; press → "You've stopped watching Plaguebloom.", "Watch"
async def test_typed_text_matching_several_has_no_button(...)
async def test_price_alone_lists_the_watch_list_and_opens_one(...) # heading "Your watch list", a line per item, select of names; pick → price line + the two history sentences + Stop watching + Back
async def test_an_empty_list(...)                                  # == prices.EMPTY_LIST_DISCORD
async def test_the_full_list_and_the_unregistered_refusals(...)
async def test_prices_off_shows_names_and_no_history(...)
async def test_every_answer_is_ephemeral_and_mentions_nobody(...)
async def test_only_the_asker_may_press(...)
async def test_no_model_and_no_blizzard_call(...)                  # the provider and the auction client are never touched
async def test_tbc(...)
def test_the_command_tree_still_builds_with_item_optional(...)     # 91 commands, /price's `item` not required
```

Update `tests/test_prices_discord.py` only where `/price`'s old direct reply is asserted (the lines must be unchanged; the transport is now the flow).

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q tests/test_price_watch_discord.py tests/test_prices_discord.py tests/test_parity_matrix.py` → PASS.
- [ ] **Step 5: `docs/manual.md`**: `/price`'s two forms, Watch / Stop watching, that the list is yours alone and holds 25, that nothing pings you when a price moves.
- [ ] **Step 6: The gate**, commit: `"Price watch 5: /price gains Watch and the watch list"`.

### Task 6: The Prices tab, and the record

**Files:**
- Create: `frontend/src/pages/Prices.tsx`, `frontend/src/pages/prices.module.css`, `frontend/src/components/PriceChart.tsx`
- Modify: `frontend/src/App.tsx` (the rail entry + route), `frontend/src/api.ts` (types), `tests/test_frontend_source.py`, the built bundle under `src/oibot_gm/web/static/app`, `docs/manual.md`, `docs/design.md` (§5.66), `CLAUDE.md` (Layout entries: `price_history.py`, `flows_prices.py`, the registry / prices / api_prices / discord_auctions / frontend lines)

**Interfaces:**
- Consumes: Task 4's routes exactly as given.
- Produces:
  - Rail entry `{ to: "/prices", label: "Prices" }` for every member, only when `meta.prices?.market` (absent on tbc); route `/prices`.
  - `Prices.tsx`: the search (`/api/prices/find`: rows with ItemName, `low_words`, N listed / none listed, the server's `more`, and **Watch** / **Stop watching** per row from `watching`); **Your watch list** (`/api/prices/watching`): a row per item (a card below `sm`), opened in place → `PriceChart` + the server's `sentences` + **Stop watching**; the server's `empty` sentence when the list is empty; the status sentence (`words`) once on the page; with `shown` false, names only. A refusal's sentence is shown as the server sent it. `useLive` reloads on the `prices` topic; the watch list also reloads after each press.
  - `PriceChart.tsx`: inline SVG from `points` (`t`, `low`, `high`): a step line of the lowest price (a price holds until the next point), broken where `low` is null, a band between low and high on folded days, the span switch from the server's `spans` (labels as sent), the highest and lowest as the two axis labels (`high_words`, `low_words`), the first and last point's `when` as the time labels, a hover / focus read-out of a point's `when` and `words`. Colours from Mantine CSS variables only (both themes). With no points: the server's sentences alone, no empty frame.
  - No money or date is formatted in the page; no string of §3 is composed in the page beyond the fixed button and heading labels.

- [ ] **Step 1: Write the failing source tests** in `tests/test_frontend_source.py`:

```python
def test_the_prices_tab(...):
    app = (SRC / "App.tsx").read_text(); page = _code_only((SRC / "pages" / "Prices.tsx").read_text())
    assert 'path="/prices"' in app and '"/prices", label: "Prices"' in app and "meta.prices?.market" in app
    for route in ("/api/prices/find", "/api/prices/watching", "/api/prices/watch", "/api/prices/history"): assert route in page
    for label in ("Watch", "Stop watching", "Your watch list"): assert label in page
    assert "<ItemName " in page and "<PriceChart" in page
    assert "toLocale" not in page and "new Date(" not in page, "times and money are the server's words"
    chart = _code_only((SRC / "components" / "PriceChart.tsx").read_text())
    assert "<svg" in chart and "var(--mantine-" in chart and "#" not in re.sub(r"&#\d+;", "", chart).replace("href=\"#", ""), "theme colours only"
    assert not any(lib in (ROOT / "frontend" / "package.json").read_text() for lib in ("recharts", "@mantine/charts", "chart.js", "d3"))
```

  Add `pages/Prices.tsx` to `ITEM_PAGES`; assert the Professions page still holds its `PriceBox`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Build the page and the chart.** Load the `dataviz` skill before writing the chart if it is available to you; otherwise follow the interface above.
- [ ] **Step 4:** `cd frontend && npm ci` (first time in this worktree), `npx tsc --noEmit && npm run build`, then `uv run python scripts/check_bundle.py` → OK; `uv run pytest -q tests/test_frontend_source.py tests/test_copy.py` → PASS.
- [ ] **Step 5: The record.** `docs/manual.md` (the Prices tab, the watch list, history's meaning: lowest and highest of the lowest prices, kept 90 days in full and a year by day, "kept since"), `docs/design.md` §5.66 (decisions D1–D5, calls C1–C18, the store, the gates, what is not built), CLAUDE.md Layout entries.
- [ ] **Step 6: The gate**, commit: `"Price watch 6: the Prices tab with the watch list and its chart; the record (manual, design §5.66, CLAUDE.md)"`.
