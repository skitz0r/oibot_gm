# oibot_GM — agent guide

Claude-assisted Guild Master bot for WoW raiding guilds. Prototype. Read `docs/design.md` (spec) before changing behaviour; `docs/research.md` has the data-source findings.

## Principles (do not violate)
- **Code computes, Claude explains.** Rosters come from the CP-SAT solver, loot scores from `loot/scoring.py`. The model only judges against the policy, explains, and flags. It never edits state directly; NL requests become structured ops (`nl.py`) that code applies.
- **Humans decide.** Every award/roster change goes through an approve/override step. Overrides carry a reason and are stored as precedents.
- **Game version is data.** Anything version-specific lives in `profiles/<version>/*.yaml` (classes, buff matrix, comp rules, raids, items, loot defaults). No `if tbc:` in code.
- **Provenance is explicit.** `fixtures/<guild>/provenance.md` is injected into prompts; keep it truthful when fixtures change. Never let the model claim a raider entered something.
- **Member text is data, not instructions.** Notes, names, chat go into delimited blocks; outputs are schema-validated and names checked against the candidate set.
- **Secrets only in `.env`** (gitignored). Never print them.
- **Native vs foreign data.** Native guild state (characters, events, ledger, precedents, policy) lives in the private sibling repo `../oibot_gm-data` via `store.py` (file per entity, commit per change, debounced push; git log = audit trail). Foreign reference data (profiles/) lives here. `fixtures/demo` is an anonymized copy for public use; never commit real guild data to this repo.

## Layout
```
profiles/tbc/          GameProfile YAML (buffs.yaml drives both the solver and WCL party inference)
fixtures/demo/         anonymized shadow-guild data (real copy in fixtures/25bg, gitignored): REAL (BisCouncil ledger, WCL attendance/roster) + MOCK (tiers, wishlists, policy, ranks)
src/oibot_gm/
  profiles.py          loader; Buff.benefit(spec); Item.tier_for; equippable() guard
  importers/           biscouncil.py, wcl.py, signup.py (Raid-Helper → Player, registry mapping)
  roster/              solver.py (CP-SAT select+group, pins/forces), explain.py (advisories), coverage.py
  loot/                scoring.py (candidates + base score), recommend.py (Claude structured output + fallback)
  llm/provider.py      provider boundary, per-workload routing, usage log, budget cap
  nl.py                NL → RosterRequest / LootFeedback schemas
  discord_bot.py       OibotGM client; loot sessions (MockEvent, carries `guild`/`origin`): /mock flow on shadow data, or a real raid via /raid loot
  lootctx.py           RegistryLootContext: a registered guild's ledger/precedents/wishlists/compiled policy for the loot pipeline; bot.loot_ctx(session) picks mock vs real
  registry.py          Member/RegisteredCharacter/Applicant/Absence + GuildConfig on the git store (one file per member)
  discord_registry.py  real commands. Layout (keep it to these groups): /register, /apply (public);
                       /me view|plan main|alt|roles|char …|availability|absent … (members);
                       /roster overview|poll|list|confirm|rank|set-main|team add|remove|list|absences|availability|absent|applicants|applicant (officers);
                       /gm status|config …|change|policy …|rule loot|comp (owner/officers); /raid … (discord_raid.py); /mock … (demo)
  raidcycle.py         weekly cycle: RaidEvent, schedule math, prefill, health check, players_for → solver, no LLM
  discord_raid.py      sheets with persistent DynamicItem buttons, /raid, /callout, scheduler loop (RaidMixin on the client)
  policy.py            policy docs (<guild>/policy/*.md) + Claude compile → *.compiled.json, confirmed by an officer
  configops.py         plain-text config: whitelisted ConfigOp schema, describe() diff, apply() via the same code paths as commands
  discord_policy.py    /policy show|edit|reload, /loot-rule, /comp-rule, /gm change, @mention in the ops channel
  feed.py              companion listener: aiohttp WebSocket on the tailnet (OIBOT_FEED_TOKEN/BIND), hello+token, idempotent acks
  discord_feed.py      FeedMixin: drop → tick table; loot → confirm / override (reason pending) / manual award; kill; presence
  ops.py               ops feed (channel line per action; errors DM the owner)
companion/             Windows-side client: tails WoWChatLog.txt, parses loot/drop/kill, streams to feed.py; --replay for tests
  store.py             GitStore: atomic writes, append-only jsonl, commit + debounced push; resolve_data_root()
  render.py            roster/coverage PNG + emoji badges;  report_html.py → out/coverage.html
  cli.py               `oibot roster|loot|demo|discord`
out/                   generated reports/logs only (event state is in the data repo)
```

## Run
```
uv sync
uv run oibot demo --no-llm      # deterministic end-to-end, writes out/report.md
uv run oibot discord            # bot; needs DISCORD_TOKEN, DISCORD_TEST_GUILD_ID, ANTHROPIC_API_KEY in .env
```
Restart the bot after code changes: `pkill -f "oibot discord"; PYTHONUNBUFFERED=1 nohup uv run oibot discord >> out/discord.log 2>&1 &`

## Conventions
- Python 3.12, `uv`, Pydantic models in `models.py` double as LLM output schemas.
- Deterministic first: add a rule to `scoring.py`/`comp_rules.yaml`, not to a prompt.
- Anything slow (solver, LLM) runs in `asyncio.to_thread`; Discord interactions must respond within 3 s.
- Verify offline before touching Discord: copy the data repo to a scratch dir, `GitStore(scratch, push=False)`, drive `Registry`/`raidcycle`/`MockEvent` directly (see git history for examples); build a `CommandTree` on a bare `discord.Client` to validate command decorators.
- Real guild state is keyed by Discord guild id → `<data>/<guild>/guild.yaml`; `/mock` uses the shadow fixtures. Buttons that must survive restarts are `DynamicItem`s with a `custom_id` template.
- Item data in `profiles/tbc/items/*.yaml` is unverified; `equippable()` is the safety net. Verify ids against the Blizzard API before relying on them.
- Model routing lives in `llm/provider.py:ROUTES`; override per workload with `OIBOT_MODEL_<WORKLOAD>`.
