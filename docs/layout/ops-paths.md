# Layout — plain-text changes and the structured ops path (configops), the MCP server and its tools

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  configops.py         plain-text config: whitelisted ConfigOp schema (flat, ≤13 fields; `target` = raid id / run key / buff id / family id / schedule id; schedule_set / schedule_remove target = the schedule id ALONE
                       Channels (2026-11-27): schedule_set and run_reschedule take sheet_channel / officers_channel / loot_log_channel / board_channel (the fourth, Post board's) (a mention, an id or none); apply_ops' pure() sends a schedule_set naming a channel to _apply_async (with a bot), which runs bot.channel_setting_refusal before it is saved — every surface; run_open's `path` carries a one-off's channels (_open_channels); _schedule_value_words / _schedule_words name them (consequences.channel_words). MCP: schedule_set, reschedule_run and open_run(channels=) name them.
                       (SCHEDULE_ID_ALONE), field = the setting alone (SETTING_ALONE), field `raid` = a NEW schedule's first raid), describe() diff,
                       apply() via the same code paths as commands, apply_async(bot=) for card-posting channels and announced absences; request text in <request>;
                       OP_GROUP (every op in one capability group), bind_self (own-record scope decided in code, never by the model),
                       narrow_characters (Task 11: a `character` spec op naming a member but not which character binds the one whose class has
                       the new spec in the profile and holds the from-spec in `text`; none → ConfigOp._no_fit = the one-sentence refusal
                       _character_rows raises; several → a question listing only those; a name in `character` with no member means the
                       character somebody holds by it (_named_character over Registry.find, a shared first name left alone), a member's
                       display name only when no character has it, both for different members → "<character> (<member>)" asked unless
                       the specs decide; a named character is the only one checked ("<X> is a Warrior, which has no Holy spec." / "<X> is already Shadow."); a
                       from-spec no class has is ignored; may(op) = authorize first: a record the asker can't change is left for authorize to
                       refuse, no question; run by handle_change and the site's text box before authorize),
                       authorize → Registry.may_plain; confirm_for (an officer records a member's Confirm/Can't through answer_placement_for);
                       user_error/report_error = THE refusal wording (§2.7); BY_DESIGN_EXEMPT / PARITY_PENDING / BY_DESIGN_ONLY (single-surface features, with their path) = the parity matrix's exemptions (tests/test_parity_matrix.py); config_summary = /gm config show in words.
                       THE structured ops path (design §5.43): ops_from_fields(reg, kind, target, fields) / ops_from_changes = typed drafts → ops server-side
                       (no client carries the value grammar); describe_ops → Described (lines: label, parts current → new, error from a shadow dry run, warn,
                       consequences; json_for / as_text / as_discord render mentions per surface; review, fingerprint, summary); authorize_ops by PRIVILEGE
                       (owner / officer / own record — never may_plain); WARN_OPS + is_warn; op_consequences + CONSEQUENCE_HOOKS; apply_ops = THE core
                       (coalesce → pure writes in one worker-thread store.batch under config_lock, bot ops on the loop, revs, one audit line);
                       coalesce merges raid / schedule / buff / family / plain / absolute recruit / comp ops into one write each; aura_reset_one_sentence;
                       flows_config._save_settings = the settings wizards' (timezone, about) Save through apply_ops.
                       Phase 3 (§5.44): config_summary(reg, bot, audience) → [SummarySection] (owner: host_facts) + summary_text; the `comp` structured
                       kind → comp_target ops → Registry.set_minimums (raid_set / raid_reset / schedule_set / schedule_remove / comp are officers', not
                       OWNER_OPS — news.owner_setting reads it; plain text's `comp` group "Raids, schedules and policy", `raids` = "Auras"; a schedule's
                       run's-week preset is `schedule_set` field `cadence`); schedule_pause_sentence; the timezone CONSEQUENCE_HOOK
                       §5.67 phase 3: config_section = schedule:<id> for schedule ops, raid_all:<rid> for raid_reset (a schedule added since its
                       preview is a 409), raids:<rid> otherwise; _schedule_raid / _schedule_write = THE schedule write (Registry.schedule_raid's create
                       rule: ID_TAKEN / SCHEDULE_EXISTS); _schedule_ops: a structured `lineup` naming no raid and no kind on an EXISTING id is an EDIT
                       (one `lineup` field) when it names no CREATE_SHAPED field or carries the schedule's rev; with run times, no rev and its own first
                       raid → SCHEDULE_EDIT_SPLIT ("… Change its raids and its event times in two saves, one after the other." — MCP's schedule_set
                       carries no rev), another first raid → ID_TAKEN; else it creates (3–4 raids travel as one `lineup` field); a plain-text lineup
                       edit keeps a kept raid's start while the raid before it is unchanged; the C10 sentence is box 14's (op_box). Runs:
                       run_reschedule (target = run key; field `start` = 'YYYY-MM-DD HH:MM' guild time through rc.move_start, or an rc.EVENT_TIMING
                       setting, or weight_<key> (one priority weight); split_policy = the target raid's; several on one run coalesce into one reschedule_event) and run_lineup (value = the later raids, parse_lineup_text;
                       "none" = the first raid alone) — RUN_OPS / group `runs`, EVENT_OPS (Apply passes the `event:<key>` rev = rc.event_rev),
                       RUN_VERBS_AUDITED, WARN_OPS (always reviewed); run_open's text takes the same later-raid list (lineup_of_op); PARITY_PENDING =
                       their Discord rows (phase 5 wizards; /gm change reaches them meanwhile); run_words(reg, ev) = a run as a person reads it in a
                       plain-text line or refusal (raid + 12-hour start, never the key); every ops / audit line names runs the same way (raid_views.run_title,
                       rc.run_words, LootMixin.loot_where; the help snapshot too) — MCP fields, log.* lines and commit messages keep the key
                       Issue 14: run_unlock (target; RUN_OPS, group `runs`, RUN_VERBS_AUDITED, WARN_OPS always) — _unlock_event finds any event by
                       its key (an ended one answers with rc.unlock_refusal's sentence) else the live one; its review line is "unlock <event>. " +
                       consequences.unlock_box's sentences; apply = RaidMixin.unlock_run. SCHEMA_TEXT tells it from run_reopen (an END undone).
                       BOX_OPS (run_unlock, run_start): board_revs carries the box's fp under BOX_REV + key, op_rev hands it to Apply as
                       `seen` (unlock_run / start_now re-check it under the locks → rc.RUN_CHANGED), as the site's `seen`; REOPEN_LOCKED /
                       UNLOCK_ENDED = each refusal names the other verb (_box_refusal).
                       run_start (Task 6; the same registrations, found by _unlock_event): review "start <event> now. " + consequences.start_now_box's
                       sentences; apply = RaidMixin.start_now. SCHEMA_TEXT keeps it apart from run_open ("start a sheet") and loot field=open.
                       Phase 4 (§5.67): op_box(reg, ops, diff, rs=) → Described.box (and each line's) for one schedule's or one raid's write (boxes
                       13–17 and 19); Described.asks_first (a box with a verb: the site's Save always previews) beside review, and Described.quiet (a
                       routine box's sentences never make the TIERED Save reviewed alone); Described.fingerprint covers the box's title, sentences and
                       diff; _diff_lines = "label: old → new" in the pages' words (PAGE_WORDS schedule_fields / raid_fields through
                       _schedule_value_words / _raid_field_words / _amount); minimum_unchanged_box ("Nothing to save", no verb) for a comp change of
                       only no-op clears; a comp minimum of 0 previews as what applies (Registry.minimum_words); `run_answer`'s text = the ONE raid a
                       spec is for (issue 15: rc.event_raid → RaidMixin.set_raid_switch; RAID_SWITCH_NEEDS_SPEC; a run_answer with a spec is in
                       WARN_OPS: always reviewed); `run_answer`'s field = the spec
                       Professions (the owner, 2026-11-16): NOT written from plain text — the schema tells the parser so ("Not settable here") and
                       _character_rows refuses the four old fields (PROFESSION_FIELDS) with PROFESSIONS_NOT_HERE; the editors are /me professions and the
                       Me page, a linked character follows the armory. The listening gate (discord_bot.LISTEN_QUESTION / LISTEN_CRITERIA) treats "I have /
                       learned / can make X" as chat between people; scripts/listen_probe.py's NEWS set guards it (under 50 %)
  mcp_server.py        `oibot-mcp` (stdio MCP server, FastMCP): tools named by intent (guild_overview, get_run, set_answer, lock_run, unlock_run, start_now, plain_change …)
                       that call the RUNNING bot's /api/* over HTTP as the OWNER OF THE CHOSEN GUILD (every tool's optional `guild` = key or name,
                       default OIBOT_MCP_GUILD / --guild, else the only guild; names → keys client side via GET /api/guilds; header X-Oibot-Guild;
                       list_guilds; the bot never reads OIBOT_MCP_GUILD) (`Authorization: Bearer $OIBOT_MCP_TOKEN` → web/app.py `mcp_authorized` + `mcp_guild` → `mcp_viewer`,
                       via=mcp in the request log; LOCAL ONLY by default — OIBOT_MCP_LOCAL_ONLY, web/app.py local_request: loopback client and no proxy
                       headers, so the token never works through the tunnel). Never a second code path: every write is the site's route. `.mcp.json` (repo root, no secrets)
                       registers it for Claude Code; the token lives in `.env` only. Design §5.24, manual §8b, tests/test_mcp.py.
                       Loot tools (§5.36): reads / tick / distribute / discard / end as the token; DECISIONS (loot_confirm/award/reason/correct,
                       retire_precedent, tier_item, set_wishlist, import_wishlists, import_ledger's apply) send X-Oibot-Actor = OIBOT_MCP_ACTOR (D8; app.py
                       mcp_actor validates it); §5.48: import_ledger (csv | path, preview → rev), import_batches / undo_import (void rows per batch, actor), loot_weights / set_loot_weights (owner, ops_change), distil.
                       list_companions / revoke_companion (§5.50: never a value; no issue tool — a token must not land in a transcript).
                       armory_guild (read) / set_armory_guild (§5.1: preview → what the armory found + its guild_id and the settings rev; the link / unlink carries them; actor = the owner).
                       price_check(text) (GET /api/prices/find's `message`) / market() (GET /api/admin/market) — §5.65: reads only; no tool picks or clears the auction house.
                       Armory link (§5.64): armory_map / armory_seed (reads), link_character (preview → find, then the armory_id it showed) / unlink_character /
                       move_character (every game version, R11) / apply_armory_seed (armory_seed's rev) — decisions need the actor (D8); no tool for the
                       professions switch, Sync now or Give to a member…
                       list_guild_guides / withdraw_guild_guide / restore_guild_guide (§5.63: actor; no upload or get tool — no BattleTag in a transcript).
                       Setup tools (raid_set, schedule_*, raid_reset, aura_*, family_set, set_channel, set_config, set_recruitment, set_plain_permissions) send
                       TYPED changes through ops_change(guild, changes, preview) → POST /api/ops/change (`confirmed`; preview=True returns the Described lines);
                       set_minimum (a `comp` change), list_regulars (GET /api/readiness series), config_summary (GET /api/config/summary) — §5.44
                       §5.67 phase 3: schedule_set(schedule, field, value, fields, raid, preview) / schedule_remove(schedule) by the id alone (`raid`
                       only for a NEW schedule; a 3–4 raid lineup as fields.lineup); raid_reset(raid, preview, revs=) hands the preview's revs back
                       (raid_all:<rid>; without them the reset is unchecked, like every setup tool); list_regulars rows keyed by schedule id;
                       open_run(…, lineup=) (the later raids); reschedule_run(run, when, timing, preview, rev) and edit_lineup(run, lineup, preview,
                       rev) → POST /api/run/{key}/reschedule | /lineup (preview first and send its `rev` back; no actor — the bearer is the owner,
                       as lock_run / cancel_run). Phase 4: set_answer(spec=, raid=) (a role switch; `raid` = that raid of the event alone, issue 15,
                       through POST /api/run/{key}/set's `raid`; after the lock a rostered seat switches at once, nobody told),
                       fill_seats(seats=) (one ask, as the Events page's Switch); set_strategy is refused after the lock (STRATEGY_LOCKED); no tool
                       sends `seen` (MCP writes go ahead as before the boxes)
```
