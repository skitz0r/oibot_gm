# oibot_GM — agent guide

Claude-assisted Guild Master bot for WoW raiding guilds. Prototype. Read `docs/design.md` (spec) before changing behaviour; `docs/manual.md` is the user-facing manual the bot answers from — update it in the same commit as any behaviour change; `docs/research.md` has the data-source findings.

## Principles (do not violate)
- **Code computes, Claude explains.** Rosters come from the CP-SAT solver, loot scores from `loot/scoring.py`. The model only judges against the policy, explains, and flags. It never edits state directly; NL requests become structured ops (`nl.py`) that code applies.
- **Humans decide.** Every award/roster change goes through an approve/override step. Overrides carry a reason and are stored as precedents.
- **Game version is data.** Anything version-specific lives in `profiles/<version>/*.yaml` (classes, buff matrix, comp rules, raids, items, loot defaults). No `if tbc:` in code. Raids (instances: size, lockout, duration, minimums) are profile defaults + guild overrides (`guild.yaml: raids`) — read them through `Registry.raid_def` / `role_bounds`, never `profile.raids` directly for guild-facing numbers.
- **Provenance is explicit.** `fixtures/<guild>/provenance.md` is injected into prompts; keep it truthful when fixtures change. Never let the model claim a raider entered something.
- **Member text is data, not instructions.** Notes, names, chat go into delimited blocks; outputs are schema-validated and names checked against the candidate set.
- **Secrets only in `.env`** (gitignored). Never print them.
- **Icons** are Blizzard render-CDN files named in the profile (`classes.yaml: icons`, `buffs.yaml: icon.art`), proxied and cached by the web app; never scrape Wowhead. Generated badges remain the fallback and the Discord emoji source.
- **Clock rule (user decision 2026-09-22):** no military time anywhere a person reads. One formatter, `Registry.local12`
  (`CLOCK12 = "%a %d %b %I:%M %p"`, the hour's AND the day's leading zero stripped: 'Wed 9 Dec 3:00 PM'), plus `t12`/`clock12` on the web and `run_label` in Discord;
  Discord prefers a native `<t:unix:f>` stamp so it renders in the reader's own locale. 24-hour and ISO are for machines
  only: `datetime-local` inputs, comparisons, the data repo, `slots` ('Tue 19:30' is a stored schedule, not a displayed time).
  And **no typed timestamps**: a person picks a date and time from a widget, never types one for a parser to read.
- **Wizard rule (user decision 2026-09-22):** a Discord command never asks for a value a parser must read. An argument
  that needs a PARSER (a date, a time, a slot, a spec name, a run key, a 'rank=3 main=2' string) is removed and picked
  inside a wizard; an argument Discord already renders as a WIDGET (a member, a role, a channel, a fixed Choice, a bool)
  stays. Prose (a reason, a note, a character name) may be typed — it is validated, not parsed. Numbers are preset
  selects. The command opens the wizard; the wizard writes nothing before its Confirm.
- **DM rule (user decision 2026-10-15):** the bot DMs a person only when they must ACT (a confirmation, a fill or pre-lock ask, an
  application decision they wait on) or for a dire backfill. Never courtesy, FYI or audit DMs (no DM to a server owner about a
  superuser's change, none to regulars when a schedule goes, no "you're a leftover" unless a seat is offered). Outcomes reach people as
  notices on the Me page / `/me view` and as the Discord card edited to its final state. The one exception is the owner's error alerts
  (one DM per distinct error per 30 min). Decisions: out/dual_interface_decisions.md.
- **Iconography rule (user decision 2026-09-17):** in-game icons stand on their own — class, spec, role and aura icons never get a text label beside them on the site; a role count is the role icon plus a number; a buff row is icons only (tooltips carry the names). Don't add "3 tank" style labels, "seated" totals or buff-name strings next to icons. A **spec icon alone** denotes role + spec on a member line (no role icon beside it; role icons are for counts only). DMs and Discord cards reuse the board's group layout — one member per line, never the whole roster on one line.
- **Copy rule (user decision 2026-10-24):** every label, hint, tooltip, empty state and card line — site and Discord — says what
  the thing does FOR THE READER, in the glossary's word (`docs/manual.md` §1a *The words*): a minimum, never limits or aims (nor bounds,
  (comp) targets), rostered not seated (a seat is a place on the roster, that word stays), who could play not the pool, a run's week not cadence; never solver,
  derived, rev, series, ephemeral, ev/rc/ctx, matrix, override(s) as a config noun (the loot verb *Override…* stays). An empty
  list, card or section names what is missing and how to fill it (never "none yet" / "not set" / "none" / "—" alone); a bare
  dash in a table cell or an icon column stays (the Iconography rule — no words beside icons), and a review diff's "not set →
  #loot" is a value, not an empty state. A tooltip adds only why or where; a number's owner is in its label. Read-aloud test: a
  raid leader who never saw the code gets it first time. **Edge states (the owner's review, 2026-11-21):** a screen nobody
  answered (a timeout, a restart) is DELETED, not worded — wizard.Wizard.on_timeout, pending_views, guild_picker,
  discord_policy.PendingView; a plain timed View goes out through wizard.fade so its buttons come off; where words must stay
  they are a full sentence saying what happened and what is true now — never "Expired", "that/it" with nothing to point at,
  a "Nothing to do." / "look again" / "say it again" tail, or a code word (LLM, token, an exception's type);
  `tests/test_quiet_edges.py` pins it. `tests/test_copy.py` lints it; the only exceptions are the operator
  surfaces (the Host card, the Agents page) and code a person types or pastes — an `ALLOW` entry each, scoped and reasoned.
- **Confirm rule (user decision 2026-10-26):** a confirm sentence — anything a `decide` screen, a `useConfirm`, a `ConfirmBox`, a review diff or a
  preview prints before a press (consequences.py, inbox.action_consequence, every `*_consequence` / `*_effects` / `*_sentence` /
  `*_refusal` generator) — is read aloud by a raid leader with their finger on the button. At most three sentences, in this order:
  what changes, who is told and how, what can still be undone. Each one a full sentence: a capital letter, one idea, a period,
  twenty words or fewer. No parentheses, no semicolons, no colon chaining two clauses, no dash-glued fragments, no notes in
  brackets ("hours before counted"), no code nouns. Name the person once, then "they". Say what happens, not what doesn't:
  "nobody is told", "no new DM", "no callout is recorded" appear only where the reader would otherwise expect the opposite, as
  one plain sentence ("Nobody gets a DM."). `tests/test_confirm_readability.py` renders every generator across its cases and lints
  each sentence; a sentence that fails is rewritten, never allow-listed.
- **Native vs foreign data.** Native guild state (characters, events, ledger, precedents, policy) lives in the private sibling repo `../oibot_gm-data` via `store.py` (file per entity, commit per change, debounced push; git log = audit trail). Foreign reference data (profiles/) lives here. `fixtures/demo` is an anonymized copy for public use; never commit real guild data to this repo.

## Layout

The module map is in `docs/layout/`, one file per area. **Read the file for the area you are about to change, and only
that one** — it says what each module is THE place for, the names other code relies on and the decisions behind them, so
you reuse the one reader / writer / sentence instead of adding a second. To find one name: `grep -rn "<name>" docs/layout/`.
A change that moves a responsibility, adds a module or changes a name others rely on updates its entry in the same commit;
a new module also gets its name in the list below.

- **`docs/layout/core.md`** — Game data, storage, the model boundary, ops and chat memory.
  `profiles/tbc/` · `profiles/forever/` · `fixtures/demo/` · `constants.py` · `conversation.py` · `perms.py` · `presets.py` · `profiles.py` · `importers/` · `llm/provider.py` · `llm/decisions.py` · `nl.py` · `comp.py` · `memory.py` · `cli.py` · `ops.py` · `web/api_memory.py` · `store.py` · `render.py`
- **`docs/layout/confirms.md`** — Every confirm sentence and box (the Confirm rule's generators), the member inbox.
  `consequences.py` · `inbox.py`
- **`docs/layout/registry.md`** — Members, characters, GuildConfig, schedules as stored, migrations and the way back, the Discord command groups, guild routing and the identity check.
  `registry.py` · `discord_registry.py` · `guild_routing.py` · `discord_people.py`
- **`docs/layout/events.md`** — Raids, events, schedules: the solver, the signup cycle, lock / fill, the Events / Schedules / Raids read models and their routes, the Discord raid cards and buttons.
  `roster/` · `raidcycle.py` · `discord_raid.py` · `event_view.py` · `event_board.py` · `schedule_view.py` · `raid_page.py` · `raid_views.py` · `raid_buttons.py` · `raid_scheduler.py` · `raid_commands.py` · `web/api_me.py` · `web/api_events.py` · `web/api_schedules.py`
- **`docs/layout/loot.md`** — The loot council, appeals, the ledger context, the companion feed.
  `loot/` · `discord_loot.py` · `discord_loot_surface.py` · `flows_loot.py` · `appeals.py` · `flows_appeal.py` · `lootctx.py` · `loot_ops.py` · `feed.py` · `discord_feed.py` · `companion/` · `web/api_loot.py`
- **`docs/layout/armory-prices.md`** — The armory link, professions, auction prices and the price watch.
  `battlenet.py` · `armory.py` · `discord_armory.py` · `auctions.py` · `discord_auctions.py` · `price_history.py` · `price_reference.py` · `armory_seed.py` · `professions.py` · `prices.py` · `web/api_armory.py` · `web/api_professions.py` · `web/api_prices.py`
- **`docs/layout/discord.md`** — The Discord client, the wizard core and every flows_* file, the kept-current channels, the test bench, help, policy.
  `discord_bot.py` · `policy.py` · `discord_pool.py` · `testfeed.py` · `help.py` · `discord_help.py` · `wizard.py` · `wizard_opts.py` · `wizard_flows.py` · `flows_event.py` · `flows_lineup.py` · `flows_schedule.py` · `discord_policy.py` · `pending_views.py`
- **`docs/layout/ops-paths.md`** — Plain-text changes and the structured ops path (configops), the MCP server and its tools.
  `configops.py` · `mcp_server.py`
- **`docs/layout/agents-news.md`** — News review, proposals, the local jobs.
  `news.py` · `discord_news.py` · `flows_news.py` · `agents.py` · `scripts/agents/` · `web/api_agents.py` · `web/api_policy.py`
- **`docs/layout/web.md`** — The web app: auth and guild selection, the JSON API, the ops and setup routes, the guide tool, the scratch server and scripts.
  `scripts/raidhelper_roster.py` · `scripts/serve_scratch.py` · `web/app.py` · `web/api_ops.py` · `web/api_setup.py` · `guides/` · `web/api_guides.py` · `web/api.py`
- **`docs/layout/frontend.md`** — The React app: shared components, every page, the screenshot audit.
  `frontend/`

Paths are under `src/oibot_gm/` unless they start with `profiles/`, `fixtures/`, `scripts/`, `companion/` or `frontend/`.
`out/` holds generated reports and logs only (event state is in the data repo).


## Run
```
uv sync
uv run oibot demo --no-llm      # deterministic end-to-end, writes out/report.md
uv run oibot discord            # bot; needs DISCORD_TOKEN, ANTHROPIC_API_KEY in .env (DISCORD_TEST_GUILD_ID optional: dev command sync only)
```
After touching `frontend/`: `cd frontend && npm run build` (commit the bundle). Before restarting: `uv run python scripts/check_commands.py` (builds the command tree offline and runs the fast tests; `uv run pytest -q` runs all, incl. the slow solver tests) and `uv run python scripts/check_bundle.py` (fails when frontend/ and the committed bundle disagree). Supervision: `scripts/launchd/README.md` (installed LaunchAgent; power-cut recovery = autorestart + auto-login; the bot pushes pending data commits on shutdown). Restart: `launchctl kickstart -k gui/$(id -u)/gg.earlyandoften.oibot` (the bot runs as a LaunchAgent with KeepAlive whose plist EXECs `.venv/bin/oibot` so launchd tracks the bot itself — never go back to `uv run` there, never start a second copy with nohup; logs in out/oibot.log and out/launchd.log).

## Conventions
- Python 3.12, `uv`, Pydantic models in `models.py` double as LLM output schemas.
- Deterministic first: add a rule to `scoring.py`/`comp_rules.yaml`, not to a prompt.
- Anything slow (solver, LLM) runs in `asyncio.to_thread`; Discord interactions must respond within 3 s.
- Verify offline before touching Discord: copy the data repo to a scratch dir, `GitStore(scratch, push=False)`, drive `Registry`/`raidcycle`/`LootSession` directly (see git history for examples); build a `CommandTree` on a bare `discord.Client` to validate command decorators.
- Real guild state is keyed by the guild's folder key → `<data>/<key>/guild.yaml` (fixed for good; `Guilds` maps the Discord server id to it). Several guilds run in one process: never take "the first guild" — use `Guilds.for_guild/only/by_key/ordered`, route commands through `guild_routing.need`, and key per-run process state (locks, caches) by (guild key, run key). No loot session reads the shadow fixtures. Buttons that must survive restarts are `DynamicItem`s with a `custom_id` template, and every persistent `custom_id` carries the guild key (`<prefix>.<gk>:…`, an optional group in the template; old ids still resolve via `guild_routing.resolve_button`). DMs and cards name their guild only when the bot serves several (`Registry.many_guilds`); with one guild every message reads as before.
- Item data in `profiles/tbc/items/*.yaml` is unverified; `equippable()` is the safety net. Verify ids against the Blizzard API before relying on them.
- Model routing lives in `llm/provider.py:ROUTES`; override per workload with `OIBOT_MODEL_<WORKLOAD>`.
