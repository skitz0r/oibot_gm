# Guide Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any member uploads a guild-made RestedXP guide (unlocked once with their BattleTag), the bot keeps the unlocked texts as dated versions per faction, and any member gets a version locked to their own BattleTag by DM (`/guide get`) or from the Me page; officers withdraw / restore.

**Architecture:** One core `guides/library.py` on the git store (`<guild>/guides/index.yaml` + one text file per guide per version), reusing `rxp_decode.decode/encode`, `build.validate_battletag`, `reissue.SLOTS`. Discord (`/guide …` wizards), the site (routes in `web/api_guides.py`, `GuideLibrary.tsx`) and MCP (list / withdraw / restore) are thin callers.

**Spec:** `docs/superpowers/specs/2026-11-09-guide-library-design.md` — the authority. Read it and CLAUDE.md first.

## Global Constraints

- **No BattleTag and no import string is ever stored, logged, put in an ops line, a commit message, an exception message that is logged, or a model prompt** (D7). Tests grep the scratch data repo and caplog for the tags they used.
- **The words** (spec §2): guild guide, version, latest, withdraw / restore, *locked to* / *unlocked by*. Never encrypt / decrypt / key / plaintext where a person reads.
- **Confirm rule / Copy rule / Clock rule / Wizard rule / DM rule / Iconography rule** (CLAUDE.md). Every `*_consequence` generator is added to `tests/test_confirm_readability.py`'s cases; `tests/test_copy.py` must stay at 0 hits. Times a person reads go through `Registry.local12`.
- Slow work (`decode` / `encode`, store writes) runs in `asyncio.to_thread`; a Discord interaction answers within 3 s (defer / `Wizard.working`).
- Several guilds: route through `guild_routing.need`; per-guild locks keyed by guild key; nothing global but `reissue.SLOTS`.
- Discord command descriptions ≤ 100 characters.
- Verify offline: scratch data repo with `GitStore(scratch, push=False)`; wizards through `tests/wizard_harness.py`.
- Gate per task: the task's tests pass, then `uv run python scripts/check_commands.py` (real exit code, never piped through `tail`); commit and push. After touching `frontend/`: `cd frontend && npm run build`, `uv run python scripts/check_bundle.py`, commit the bundle. Commit messages end with the session's attribution lines given in the task prompt.
- Do NOT restart the bot; the operator does that at the end.

## Tasks

### Task 1 — the core: `guides/library.py`
- [ ] Tests first in `tests/test_guide_library.py` (spec §8, core part): round trip (upload with tag A → `issue` for tag B → `decode(issued, B)` equals the stored texts; section count kept), wrong tag / empty / too big / bad faction / bad tag refusals in the existing words, `UPLOADS_PER_DAY`, `latest` skips withdrawn, id clash adds `-2`, withdraw / restore + their refusals, the uploader's notice on withdraw (not when an officer withdraws their own), `rows(reg, officer=False)` hides withdrawn, no tag in the repo files or `git log`.
- [ ] Implement `GuideVersion`, the store layout, `versions / latest / get / upload / issue / withdraw / restore / rows / issue_filename`, `FACTIONS`, `FACTION_WORDS`, `UPLOADS_PER_DAY`, refusal constants, the three `*_consequence` generators and `available(reg)` (the profile has the guide tool). Export through `guides/__init__.py`.
- [ ] Add the generators to the confirm readability harness; run it and read `out/confirm_sentences.txt`.

### Task 2 — the site's routes + MCP
- [ ] Tests (`tests/test_guide_library_api.py`): the five routes of spec §5 (member upload and download, no-store + attachment headers, officer gates, `preview`, MCP refused for upload / download, MCP withdraw needs the actor), `GET` rows for member vs officer, SSE topic `guides` published on every write.
- [ ] Implement in `web/api_guides.py`; ops lines through `ops.audited` (via web / mcp), never a tag.
- [ ] MCP tools `list_guild_guides`, `withdraw_guild_guide`, `restore_guild_guide` (+ `tests/test_mcp.py` entries); `configops` parity entries (spec §6) so `tests/test_parity_matrix.py` passes.

### Task 3 — Discord: `/guide list|get|upload|withdraw|restore`
- [ ] Tests through `tests/wizard_harness.py` (`tests/test_guide_library_discord.py`): every screen linted; get → DM with the file (decodable with the typed tag), DM refused → the file on the ephemeral reply; upload from an attachment (wrong type / too big refused before reading), the decide screen prints `upload_consequence`; withdraw / restore officer-only with rights re-checked on the press; nothing with the tag in caplog or ops lines.
- [ ] Implement `flows_guides.py` (+ import in `wizard_flows.py`) and `discord_guides.py` (`register_guide_commands`, called where the other groups register); `pending_views` mirroring as the other wizards do.

### Task 4 — the Me page: `GuideLibrary.tsx`
- [ ] Component per spec §5 (blocks per faction, Get it for my BattleTag, officers' ⋮ Withdraw… / Restore… with the server's sentences, the upload card, empty state, phone cards, `useDirty`, `useLive('guides')`); wire into the Leveling guide tab above `GuideTools`; `tests/test_frontend_source.py` conventions; build the bundle.

### Task 5 — help, manual, design, agent guide
- [ ] `help.py` `guild guides` section (every member; `by_name` through `unfence`) + test; `docs/manual.md` § Guild guides and §1a words; `docs/design.md` §5.63; CLAUDE.md layout lines (`guides/library.py`, `flows_guides.py`, `discord_guides.py`, routes, `GuideLibrary.tsx`, MCP tools).

### Final
- [ ] Whole-branch review (correctness + the D7 leak hunt), fix wave, full `uv run pytest -q`, `check_commands`, `check_bundle`, push, operator restart.

## Review Focus

1. **A tag or a string reaching disk or a log** — exception paths, FastAPI validation errors, `log.exception`, pending-view mirrors, ops lines, commit messages.
2. **Two uploads in the same second / concurrent upload and withdraw** — the per-guild lock, id clash.
3. **A withdrawn latest** — every surface falls back; a member cannot issue a withdrawn id by URL.
4. **Reading the attachment before the membership check** (never), size checked from metadata first.
5. **Section count on re-encode** — the issued string imports like the uploaded one.
