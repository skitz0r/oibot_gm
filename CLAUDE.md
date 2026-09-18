# oibot_GM — agent guide

Claude-assisted Guild Master bot for WoW raiding guilds. Prototype. Read `docs/design.md` (spec) before changing behaviour; `docs/manual.md` is the user-facing manual the bot answers from — update it in the same commit as any behaviour change; `docs/research.md` has the data-source findings.

## Principles (do not violate)
- **Code computes, Claude explains.** Rosters come from the CP-SAT solver, loot scores from `loot/scoring.py`. The model only judges against the policy, explains, and flags. It never edits state directly; NL requests become structured ops (`nl.py`) that code applies.
- **Humans decide.** Every award/roster change goes through an approve/override step. Overrides carry a reason and are stored as precedents.
- **Game version is data.** Anything version-specific lives in `profiles/<version>/*.yaml` (classes, buff matrix, comp rules, raids, items, loot defaults). No `if tbc:` in code. Raids (instances: size, lockout, duration, desired comp) are profile defaults + guild overrides (`guild.yaml: raids`) — read them through `Registry.raid_def` / `role_bounds`, never `profile.raids` directly for guild-facing numbers.
- **Provenance is explicit.** `fixtures/<guild>/provenance.md` is injected into prompts; keep it truthful when fixtures change. Never let the model claim a raider entered something.
- **Member text is data, not instructions.** Notes, names, chat go into delimited blocks; outputs are schema-validated and names checked against the candidate set.
- **Secrets only in `.env`** (gitignored). Never print them.
- **Icons** are Blizzard render-CDN files named in the profile (`classes.yaml: icons`, `buffs.yaml: icon.art`), proxied and cached by the web app; never scrape Wowhead. Generated badges remain the fallback and the Discord emoji source.
- **Iconography rule (user decision 2026-09-17):** in-game icons stand on their own — class, spec, role and aura icons never get a text label beside them on the site; a role count is the role icon plus a number; a buff row is icons only (tooltips carry the names). Don't add "3 tank" style labels, "seated" totals or buff-name strings next to icons. A **spec icon alone** denotes role + spec on a member line (no role icon beside it; role icons are for counts only). DMs and Discord cards reuse the board's group layout — one member per line, never the whole roster on one line.
- **Native vs foreign data.** Native guild state (characters, events, ledger, precedents, policy) lives in the private sibling repo `../oibot_gm-data` via `store.py` (file per entity, commit per change, debounced push; git log = audit trail). Foreign reference data (profiles/) lives here. `fixtures/demo` is an anonymized copy for public use; never commit real guild data to this repo.

## Layout
```
profiles/tbc/          GameProfile YAML (buffs.yaml drives both the solver and WCL party inference)
profiles/forever/      Classic-era buff matrix: `slot` = one totem per element per shaman, `scope: raid` = cast buffs, `status` = evidence level,
                       `families:` = stacking families with the beneficiary map (buffs point at one; guild overrides in guild.yaml buffs/families → Registry.profile)
fixtures/demo/         anonymized shadow-guild data (real copy in fixtures/25bg, gitignored): REAL (BisCouncil ledger, WCL attendance/roster) + MOCK (tiers, wishlists, policy, ranks)
src/oibot_gm/
  constants.py         ROLES / MARK / LEVEL_COLOUR / TEAL shared by Discord, web and render (no per-module copies)
  profiles.py          loader; Buff.benefit(spec); Item.tier_for; equippable() guard
  importers/           biscouncil.py, wcl.py, signup.py (Raid-Helper → Player, registry mapping)
  roster/              solver.py (CP-SAT select+group, pins/forces, role min/max, per-player bonus), explain.py (advisories), coverage.py (slot-aware)
  loot/                scoring.py (candidates + base score), recommend.py (Claude structured output + fallback)
  llm/provider.py      provider boundary, per-workload routing, usage log, budget cap
  nl.py                NL → RosterRequest / LootFeedback schemas
  discord_bot.py       OibotGM client; loot sessions (MockEvent, carries `guild`/`origin`): /mock flow on shadow data, or a real raid via /raid loot
  lootctx.py           RegistryLootContext: a registered guild's ledger/precedents/wishlists/compiled policy for the loot pipeline; bot.loot_ctx(session) picks mock vs real
  registry.py          Member/RegisteredCharacter/Applicant/Absence + GuildConfig on the git store (one file per member)
  discord_registry.py  real commands. Layout (keep it to these groups): /register, /apply (public);
                       /me view|plan main|alt|roles|char …|absent … (members);
                       /roster overview|add|remove|members|list|confirm|rank|set-main|absences|absent|applicants|applicant (officers; `poll` retired, PlanButton is a stub);
                       Rosters are first-class: config `rosters[]` (key/size/schedule/instance/cutoffs), membership lives on characters (`RegisteredCharacter.rosters`),
                       raids are opened for a roster, officer posts go to `roster_channel_id`. Internal helpers still say "team" (aliases) — don't rename them casually.
                       /gm status|config …(owner: ops/applications/registration/analytics/roster/signup/absences channels, raid, aura, officer-role, timezone, ask-audience, about; no standing rosters)|change|policy …|rule loot|comp|test …; /raid … (discord_raid.py); /mock … (demo)
  raidcycle.py         signup-driven cycle (design.md §5.20): raid slots + cadence (raid_def: slots, signup_lead/lock/confirm hours, weights) →
                       slot_starts/open_run (ephemeral roster per run), Join/Bench/No thanks (in/sub/out), propose() at lock (weights, pins,
                       role min/max, splits into several rosters per slot), confirmations (placement asks on the run key), free_seat/seat_player/
                       expire_confirmations, fill engine (after lock only: benched joiners + Bench first → pool → offspec → alt swaps, paired with a backfill
                       when the vacated role would go short; asks expire after fill_ask_hours). Per-raid `nudge`/`nudge_hours_before`/`autofill`/`open_dm`
                       come from raid_def. No LLM. Run states open | locked | done | cancelled; the member-facing word is "rostered", never "seated".
  discord_raid.py      sheet_layout (Discord layout components: container/section/thumbnail/text/separators/buttons, real icons as app emojis
                       via discord_bot.ensure_icon_emojis → ico()), persistent buttons, lock_run → roster cards + Confirm/Can't DMs (PlaceButton), drop_seated, after_absence, /raid …,
                       scheduler loop (open on cadence → health → fill → lock → expire → close) (RaidMixin on the client)
  policy.py            policy docs (<guild>/policy/*.md) + Claude compile → *.compiled.json, confirmed by an officer
  discord_pool.py      dedicated channels the bot keeps current: registration card (public, read-only); analytics channel with the character
                       bank, and per raid: pool readiness, optimised groups, desired comp + change log (Registry.listeners, debounced);
                       absences channel (AbsencesMixin: card + "I'll be away" modal → announce_absence → sheets updated);
                       SetupMixin: set_channel (web Config page + plain-text `set …_channel` via configops.apply_async), guild_channels/roles, test_bench_clear.
                       Test bench: Member.test puppets, /gm test seed|run|answer|clear; RaidMixin.send_member_dm routes puppet DMs to the TESTER's own DMs
                       (never the roster channel), may_answer_for lets the tester press their buttons, a test run's pool is puppets + tester only and its
                       sheet refuses presses from real members
  comp.py              pool → Players, solver run at roster size, raid-buff status, ideal_comp (targets with justifications)
  help.py              the bot explains itself: docs/manual.md + live command tree + guild settings + the asker's record → Claude (route `help`)
  discord_help.py      /help (no LLM, by tier), /ask, @mention outside the officer channels and DMs → help_answer
  configops.py         plain-text config: whitelisted ConfigOp schema (flat, ≤13 fields; `target` = raid id / run key / buff id / family id), describe() diff,
                       apply() via the same code paths as commands, apply_async(bot=) for card-posting channels and announced absences; request text in <request>
  cli.py               `oibot roster|loot|demo|discord`; root logger → out/oibot.log (rotating) + stderr
  discord_policy.py    /policy show|edit|reload, /loot-rule, /comp-rule, /gm change, @mention in the ops channel
  feed.py              companion listener: aiohttp WebSocket on the tailnet (OIBOT_FEED_TOKEN/BIND), hello+token, idempotent acks
  discord_feed.py      FeedMixin: drop → tick table; loot → confirm / override (reason pending) / manual award; kill; presence
  ops.py               ops feed (channel line per action; errors DM the owner)
companion/             Windows-side client: tails WoWChatLog.txt, parses loot/drop/kill, streams to feed.py; --replay for tests
  web/app.py           FastAPI served inside the bot (OIBOT_WEB_BIND): Discord OAuth2, landing page, icon/emblem/card images, old URLs → /app redirects;
                       published by a Cloudflare Tunnel (~/.cloudflared/config.yml → gm.earlyandoften.gg)
  web/api.py           JSON API (/api/*) for the React app + SPA mount at /app (history fallback to index.html). POSTs need `X-Requested-With: oibot` (CSRF).
                       Mutations reuse Registry/configops exactly like the Discord commands
frontend/              React + Mantine app (Vite). components/board/* (SheetCard, BoardView, SplitModal, BoardPreview, FillModal), components/Cells.tsx (icon-only cells),
                       components/ConfirmModal.tsx (useConfirm), hooks/usePoll.ts (45 s refresh while visible); game constants come from /api/meta (roles, statuses, group caps, class colours). `npm run build` writes src/oibot_gm/web/static/app (committed, so the bot runs without Node);
                       `npm run dev` proxies /api,/img,/auth to the bot on :8788. Pages: Me · Rosters · Raids · Members (bank + admin merged; officers edit anyone's characters/grid) · Ops · Config (Mantine, left rail,
                       tables switch to an edit mode with one Save). Screenshot audit: `uv run python scripts/shots.py` (Playwright, desktop + phone)
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
After touching `frontend/`: `cd frontend && npm run build` (commit the bundle). Before restarting: `uv run python scripts/check_commands.py` (builds the command tree offline; catches over-long descriptions and bad decorators). Restart: `pkill -f "oibot discord"; PYTHONUNBUFFERED=1 nohup uv run oibot discord >> out/discord.log 2>&1 &`

## Conventions
- Python 3.12, `uv`, Pydantic models in `models.py` double as LLM output schemas.
- Deterministic first: add a rule to `scoring.py`/`comp_rules.yaml`, not to a prompt.
- Anything slow (solver, LLM) runs in `asyncio.to_thread`; Discord interactions must respond within 3 s.
- Verify offline before touching Discord: copy the data repo to a scratch dir, `GitStore(scratch, push=False)`, drive `Registry`/`raidcycle`/`MockEvent` directly (see git history for examples); build a `CommandTree` on a bare `discord.Client` to validate command decorators.
- Real guild state is keyed by Discord guild id → `<data>/<guild>/guild.yaml`; `/mock` uses the shadow fixtures. Buttons that must survive restarts are `DynamicItem`s with a `custom_id` template.
- Item data in `profiles/tbc/items/*.yaml` is unverified; `equippable()` is the safety net. Verify ids against the Blizzard API before relying on them.
- Model routing lives in `llm/provider.py:ROUTES`; override per workload with `OIBOT_MODEL_<WORKLOAD>`.
