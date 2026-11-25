# Event words (phase 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Every string a person reads says *event*, *lineup*, *one-off* and *Priority*; *run*, *follow-on*, *ad hoc* and *Who gets a seat* are retired, and the copy lint keeps them out.

**Architecture:** A wording sweep only: no behaviour, storage, route, id or command name changes. The copy lint (`tests/test_copy.py`) gains the retired words behind a shrinking `WORDS_PENDING` list of files, so each task turns its files green and the last task empties the list.

**Tech Stack:** Python 3.12 / uv, discord.py, React + Mantine (committed bundle).

**Spec:** `docs/superpowers/specs/2026-11-22-raids-events-schedules-design.md` (§2, C15, §7 "Help and the manual", §9 phase 6).

## Global Constraints

- CLAUDE.md Principles bind every string: Copy, Confirm (≤ 3 sentences, ≤ 20 words each), Clock, Iconography, DM rules. A sentence that grows past twenty words with the new word is rewritten, never allow-listed.
- **Words only.** No change to: slash command or parameter NAMES (`/raid open`, `run:` …; their descriptions DO change), plain-text op kinds (`run_open` …), MCP tool names and fields, routes, JSON keys, `custom_id`s, PAGE_WORDS / RUN_WORDS dictionary KEYS, Python / TS identifiers, log lines (`log.*`), commit messages, docstrings and comments (leave them; no churn).
- **The word table (binding; use exactly these):**

  | Retired | Now | Notes |
  |---|---|---|
  | a run, the run, runs (the dated occasion) | event(s) | "Cancel event…", "End the event", "Open this event early", "test event", "locked event" |
  | to run / it runs / "When it runs" (verb), "run a command" | unchanged | a verb is not the retired noun |
  | a run's week | an event's week | CLAUDE.md's Copy rule sentence is updated to match |
  | run times (a weekly schedule's times) | event times | |
  | ad hoc run / ad hoc | one-off event / one-off | |
  | pickup template / pickup | a schedule opened by an officer | PAGE_WORDS `kinds.pickup` already says "Opened by an officer" |
  | recurring run | a schedule's event | |
  | follow-on (raid / run) | later raid (of the event); the whole = lineup | |
  | lead (run) | first raid | |
  | Who gets a seat | Priority | |
  | attendance "N of M runs" | N of M raids | attendance counts once per raid of an event (spec §8), so "events" would be false — verify in `Registry.attendance` and say "raids" |
  | a job's run, "runs every 15 min" (Agents page, Host card) | unchanged | operator surface: one scoped `ALLOW` entry each, with a reason |

- **People still say "run".** The plain-text parser (`configops` SCHEMA text, `nl.py`, the listening gate's state, `help.py` SYSTEM) must keep understanding "run", "ad hoc" and "follow-on" as the old words for event, one-off and later raid, and answer in the new words. One line in each prompt says so; a test pins the line.
- **Stored words stay readable.** Nothing in the data repo is rewritten. Any code that RECOGNISES a stored sentence (`rc.lock_failure_kind` and stored `lock_error`, a notice's text, a closed card) must recognise both the old and the new wording; a test for each recogniser you touch.
- No real guild data, member names or Discord ids anywhere. Never touch the live bot or the main checkout; never run `scripts/check_commands.py` (the controller does).
- Tests: `uv run --with pytest-xdist pytest -q -n 8 -m "not slow" tests` (about 7 minutes; a failure under `-n` is re-run alone before it counts). A test that asserts an old sentence is updated to the new one — never deleted, never loosened to a substring that would pass both.
- Shared-docs protocol: `docs/manual.md`, `docs/design.md`, `CLAUDE.md` and `docs/layout/*` are edited only by the tasks that name them.

## Review Focus

1. A verb "run" wrongly turned into "event" ("the bot events hourly"), or a noun missed because the lint's verb exception was too wide.
2. A confirm sentence pushed past twenty words, or reading wrongly aloud ("the event's event").
3. A stored old-word sentence no longer recognised after the constant changed (a second run-update line at a retried lock).
4. Plain text: "cancel tonight's run" / "open an ad hoc Barrow Deeps" no longer parsed, or answered in the old word.
5. Attendance sentences claiming "events" where the count is per raid.

---

### Task 1: The lint, the glossary, the shared word tables

**Files:** `tests/test_copy.py`, `src/oibot_gm/constants.py`, `src/oibot_gm/raid_buttons.py` (RUN_WORDS values and its labels), `docs/manual.md` §1a only, `CLAUDE.md` (the Copy rule's glossary sentence only), tests that assert these words.

**Produces:**
- `tests/test_copy.py`: `RETIRED = re.compile(...)` matching the retired nouns of the word table (`run`/`runs` as a noun, `follow-on`, `ad hoc`/`adhoc`, `pickup`, `who gets a seat`, `recurring run`, `lead run`); `VERB_RUN` = the explicit verb phrases removed before matching (it runs, runs on / at / every / hourly, to run, run it / this / the command / again, is running, ran) — narrow, each alternative exercised by a test in `test_extractors_take_prose_not_code`'s style, with a paired noun case that must still be caught ("the run", "this run", "runs this week", "3 runs", "a run's", "test run"); `WORDS_PENDING: set[str]` = the repo-relative files still holding a retired word (every file with a hit today except this task's); `test_retired_words_are_gone()` fails for a hit in a file NOT in `WORDS_PENDING`, and fails for a file IN it with no hit left (so the list only shrinks). The findings writer lists retired-word hits too.
- The lint's Python file list (`PY_FILES` / `_flowfiles`) is widened to every module that builds person-read text and is not covered today: check at least `constants.py`, `raid_buttons.py`, `event_view.py`, `schedule_view.py`, `raid_page.py`, `event_board.py`, `flows_event.py`, `flows_lineup.py`, `flows_schedule.py`, `raid_scheduler.py`, `raid_commands.py`, `testfeed.py`, `discord_loot.py`, `loot_ops.py`, `appeals.py`, `discord_people.py`, `web/api*.py`, `presets.py`. A newly covered file's hits on the OLD lint are fixed or, when they are this plan's retired words, the file joins `WORDS_PENDING`.
- `constants.py` (PAGE_WORDS, CONFIRM_WORDS, every other word table there) and `RUN_WORDS` in the new words: "Cancel event…", "End the event". Keys unchanged.
- `docs/manual.md` §1a: entries **Event**, **Lineup**, **One-off**, **Priority**, **An event's week**, **An event's state**; *run*, *follow-on*, *ad hoc*, *Who gets a seat* retired (one sentence says people may still say "run" and the bot understands). The rest of the manual is Task 7's.

- [ ] Write the failing lint tests (verb / noun pairs; the shrinking-list rule), run, see them fail.
- [ ] Implement `RETIRED`, `VERB_RUN`, `WORDS_PENDING`, the widened file list.
- [ ] Reword constants.py and RUN_WORDS; update the tests that assert them (grep the old strings under `tests/`).
- [ ] §1a and the CLAUDE.md sentence.
- [ ] Fast suite green; commit.

### Task 2: The engine's words

**Files:** `src/oibot_gm/raidcycle.py`, `consequences.py`, `inbox.py`, `event_view.py`, `schedule_view.py`, `raid_page.py`, `event_board.py`, `presets.py`, `roster/explain.py` and `roster/solver.py` (only strings a person reads); their tests; `tests/test_confirm_readability.py` stays green.

- [ ] Reword every person-read string per the table; remove these files from `WORDS_PENDING`.
- [ ] `rc.lock_failure_kind` and every other recogniser of a stored sentence: old and new wording both recognised, one test each.
- [ ] Confirm sentences re-checked by `tests/test_confirm_readability.py` (it renders every generator); a sentence over the limit is rewritten.
- [ ] Fast suite green; commit.

### Task 3: Settings, plain text and help

**Files:** `registry.py`, `configops.py`, `loot_ops.py`, `nl.py`, `help.py`, `discord_help.py`, `discord_policy.py`, `conversation.py`, the listening state in `discord_bot.py` (LISTEN_* words only), `mcp_server.py` (ONLY sentences returned to a person as `message`; tool names, fields and docstrings stay); their tests.

- [ ] Reword refusals, ops / audit lines, diff labels, summaries, help snapshot lines.
- [ ] The prompts' one line: "run, ad hoc and follow-on are older words for event, one-off and a later raid; answer in the new words." in configops' schema text, help's SYSTEM and the listening state; a test pins each. Parser tests with "run" phrasing still pass unchanged.
- [ ] Remove these files from `WORDS_PENDING`; fast suite green; commit.

### Task 4: Discord's raid surfaces

**Files:** `discord_raid.py`, `raid_views.py`, `raid_buttons.py` (the rest), `raid_commands.py` and `discord_registry.py` (command and option DESCRIPTIONS only), `raid_scheduler.py` (ops warns), `flows_raid.py`, `flows_event.py`, `flows_lineup.py`, `flows_schedule.py`, `flows_config.py`, `flows_misc.py`, `flows_roster.py`, `flows_register.py`, `flows_test.py`, `discord_pool.py`, `discord_people.py`, `testfeed.py`, `wizard_opts.py`; their tests; `scripts/check_commands.py`'s expectations if it pins a description.

- [ ] Reword cards, sheets, wizard screens, DMs, thread lines, command descriptions (Discord's 100-character limit — `wizard.lint` and the command-tree build check it).
- [ ] Remove these files from `WORDS_PENDING`; fast suite green; commit.

### Task 5: Loot and the web API's sentences

**Files:** `discord_loot.py`, `discord_loot_surface.py`, `flows_loot.py`, `flows_appeal.py`, `appeals.py`, `loot/session.py`, `loot/scoring.py`, `loot/distil.py`, `loot/tierdraft.py`, `lootctx.py`, `discord_feed.py`, `feed.py`, `web/*.py` (messages and errors a page prints), `discord_armory.py`, `discord_bot.py` (the rest), `news.py`, `discord_news.py`, `flows_news.py` (a proposal's "raid guard" words), `agents.py` (only words shown outside the Agents page); their tests.

- [ ] Reword per the table ("End the event", "re-score the last event", "no live event"); the Agents page / Host card job words stay and get their scoped ALLOW entries.
- [ ] Remove these files from `WORDS_PENDING`; fast suite green; commit.

### Task 6: The site

**Files:** `frontend/src/**` (strings only), the committed bundle `src/oibot_gm/web/static/app`, `tests/test_frontend_source.py` and any test pinning a page string.

- [ ] Reword every page-held string per the table (most words already come from the server; a string still held in a `.tsx` file follows the table). The Test bench says "test event"; `Rehearse` stays.
- [ ] `cd frontend && npx tsc --noEmit && npm run build`; `uv run python scripts/check_bundle.py`; commit the bundle with the source.
- [ ] Remove these files from `WORDS_PENDING`; fast suite green; commit.

### Task 7: The manual, the docs, the empty list

**Files:** `docs/manual.md` (all of it), `docs/design.md` (§5.67 gains "Phase 6 built": the word table, what stays, the calls made), `docs/layout/*.md` (only where an entry QUOTES a person-read string that changed), `tests/test_copy.py`, `tests/test_doc_refs.py`, `tests/test_help*.py` if they pin manual text.

- [ ] The whole manual in the new words (it is what the bot answers from): every "run" noun, "ad hoc", "follow-on", "pickup template", "Who gets a seat"; slash command names and typed examples stay as typed. Button names quoted in the manual match RUN_WORDS and PAGE_WORDS exactly (grep each quoted label against the code).
- [ ] A manual lint in `tests/test_copy.py`: `RETIRED` (after `VERB_RUN` and after dropping backticked text) finds nothing in `docs/manual.md`.
- [ ] `WORDS_PENDING` is empty and deleted; `test_retired_words_are_gone` covers every file with no list. Any file still listed is fixed here.
- [ ] Fast suite green; commit.
