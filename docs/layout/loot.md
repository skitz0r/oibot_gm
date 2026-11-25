# Layout — the loot council, appeals, the ledger context, the companion feed

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  loot/                scoring.py (candidates + base score; untiered items; candidates(reg=) reads loot_cfg / items / attendance_lookup(reg) —
                       Registry.attendance_known, unknown → neutral 1.0; attendance_factor), recommend.py (Claude structured output + fallback),
                       session.py (LootSession keyed (guild, run), Drop/drop_id, Proposal + pending Override, batch_rev/drops_rev, LootChanged/LootBusy,
                       run_distribution on a snapshot + merge, make_award/sync_ledger, load_sessions/migrate_sessions, LOOT_LISTENERS; undistributed() =
                       killed bosses first; raid_bosses / boss_progress / run_boss_progress = "Bosses 3 / 6" from the session files) — design §5.35;
                       tierdraft.py (design §5.47): draft = Claude's tier PROPOSAL for one item (route tier_draft, validated, nothing written —
                       the officer's tiers Save is the write), export_csv / parse_csv / apply_csv = the tier grid's CSV (preview, dedupe, rev);
                       scoring.rescore = "re-score last run" (old vs new weights, numbers only, nothing written); distil.py (§5.48) = Claude's ≤5
                       policy sentences from the ACTIVE precedents (route distil, refs validated, nothing written — policy.append_rule is the write;
                       the <policy>/<rules>/<precedents> blocks go through llm.provider.unfence)
  discord_loot.py      LootMixin: THE loot verbs open_loot · tick · distribute · override · confirm · discard · set_reason · correct · end_run
                       (+ remove_candidate · set_scores_only · keep_open), each (reg, session, …, by, by_id, via) under loot_lock; loot_ctx / loot_roster
                       (live from the run) / loot_live (run locked); open_council (one session per roster of a split run, <run>-r<N>); auto-distribute;
                       held companion events in <guild>/loot/held.json (expired → ops warn "NOT recorded"); loot_restore; loot_may_close (scheduler auto-end:
                       only LootSession.blocks_close — picks/undistributed — holds it, once announced; reasons/conflicts outlive it: set_reason/correct take a
                       session id after the end); loot_ensure_thread (a pending thread retried on /raid loot, on_resumed, ready); hooks the surfaces replace:
                       loot_notify(…, kind, drop_id) / loot_view / loot_open_thread / loot_ended / loot_closing / loot_held (minimal defaults);
                       retire_precedent = THE retire / restore (wraps RegistryLootContext.set_precedent_status; /gm precedents, the site, plain text, MCP);
                       loot_boss_down (after a NEW kill: the raid's last boss → ALL_DOWN_LINE once + the lock card redrawn; its End = end_decide → end_run)
  discord_loot_surface.py  LootSurfaceMixin (OibotGM, before LootMixin; design §5.36): the council's Discord surface replacing those hooks —
                       a PUBLIC thread on officer_channel (refused when none), the pinned session card (raid_views.loot_layout) and the proposal
                       cards (batch_layout: ≤5 items, per boss, Confirm the rest · Discard on the newest) edited after every write from any surface,
                       session.cards = message ids; persistent thread-line buttons; distribute's card ticker; loot_scheduler_step ("Start an ad hoc
                       run…" prompt in the officer channel, the one after-end DM to officers); addressed-only thread chat (loot_addressed / loot_chat →
                       help_answer(data=) or `loot` ConfigOps in LootChatConfirmView = ConfigConfirmView + Give the reason…; the revs carry the batch /
                       the drop's pick as the officer saw them); loot_cards_restore on ready
  flows_loot.py        every LootButton / LootPick press and lootmodal submit (loot_press / loot_pick / loot_modal_submit; loot_modal_raw =
                       the restart path from on_interaction); wizards loot_tick (TickWizard), loot_override (grouped select → reason modal,
                       also Correct…), loot_distribute (/raid distribute), loot_end (/raid end + End council: LootMixin.end_box through Wizard.decide_box, End the run · Back), gm_precedents
  appeals.py           loot appeals (L32, design §5.49): Appeal / AppealStore (<guild>/appeals/<award id>.yaml — ONE per award, id =
                       award_id = a hash of lootctx.award_ref); open_appeal (the member's OWN award only; words validated, never parsed, quoted
                       and escaped; the card + a PUBLIC thread "loot · <member> · <item>" on officer_channel; no channel → waits on Loot › Appeals)
                       and resolve_appeal (one OUTCOMES choice, officers' note, the member's `appeal` NOTICE — never a DM — card to its final state,
                       thread archived, thread_close_line escapes the note); officer_details = the council's record (D2: Details button / Loot ›
                       Appeals only, never the card); appeal_owner / owner_name = whose the question is NOW (Registry.owner_of on the asker: a
                       moved character's questions follow it — of_member(uid, reg), the resolve notice, the officers' row; the file is never rewritten);
                       AppealButton `appeal.<gk>:details|resolve:<id>`; APPEAL_LISTENERS → `loot:appeals`; MCP list_appeals / resolve_appeal
                       (actor, D8) — never opens one. NO LLM anywhere
  flows_appeal.py      me_loot_ask (/me loot-ask: award select — the newest 25, its first line points at the site for older ones — → one prose
                       modal → decide "Send to the council") · appeal_resolve (officers, rights decided LIVE on open, every press and the note's
                       submit — flows_loot.is_loot_officer, PRIV_LIVE: outcome select → decide, note optional)
  lootctx.py           RegistryLootContext (one per guild, reload(); void / reweight rows and superseding / status precedents folded; wishlists derived
                       live from Registry.wishlists; set_precedent_status = Retire/Restore; reweight (D12)); LEDGER_LISTENERS on every new award row
  loot_ops.py          the `loot` / `wishlist` / `tier` plain-text ops (configops' loot branch → LootMixin verbs / Registry wishlist + tier verbs),
                       SCHEMA (into SCHEMA_TEXT), needs_actor (D8), loot_revs (batch_rev on a confirm-all diff), loot_log_state, member_awards, loot_block;
                       phase 4 (§5.48): the `loot_weights` op's words (weight_rows / weight_part / WEIGHT_GROUPS) + apply_weights, received_map
                       (the Me page's and /me wishlist's "received" marks), last_council (what re-score reads)
  feed.py              companion listener: aiohttp WebSocket on the tailnet (OIBOT_FEED_TOKEN/BIND; OIBOT_FEED_TOKEN_<KEY> binds a companion to one guild), hello+token;
                       dedupe is per loot SESSION (seen_idem), an `error` reply is not an ack; presence(guild) live | quiet | disconnected; status(guild) one line;
                       ts = epoch seconds (ts_kind); refuse_event = a clock-only KILL refused + warn_once → on_warn (ops_warner: "companion too old", once);
                       token_state(key, many=) = which feed-token variables are set (names only — the Loot › Companions card).
                       listen_state(feed) = THE reachability words (a loopback bind: "listening on this machine only — set OIBOT_FEED_BIND …").
                       Issued tokens (§5.50): authorize_any — THE token check (no other) — (.env guild token → an issued token by hash → the shared one) binds the companion to the
                       token's guild (Companion.token_id/label); still_valid re-checked at EVERY frame (a revoke = {"type":"error","auth":true}, no
                       restart), on_seen → last_seen, close_token; hash_token / new_token; token_rows / token_line / issue_consequence /
                       revoke_consequence = the hub's rows and THE sentences. Values never logged, stored or in an ops line. auth:true only for a
                       genuine no-match / revoke; can't-tell-now (parked or unloaded guild, a check that raised: TokenUnverifiable, still_valid None)
                       = a plain UNAVAILABLE error + close (the companion reconnects). The records never leave the hub: registry.readable_config
                       (PRIVATE_CONFIG_FIELDS) is what /api/config's "As stored" and the config parser's prompt dump
  discord_feed.py      FeedMixin through the LootMixin verbs: drop → tick (+ auto-distribute); loot → confirm / mismatch (recorded, reason pending) / manual /
                       conflict; kill. loot_route(comp): the token's guild → one live council (test only from a puppet/tester companion; several → by
                       seated character; else refused; a council past its run's close never competes with tonight's); no live run → held 10 min
                       (persisted); feed_presence(bot, reg) rows (token_id / label per companion), feed_status the line; start_feed (setup_hook: a .env token
                       or any guild's issued token) / ensure_feed (after the first issue), feed_issued / feed_token_valid / feed_seen (§5.50)
companion/             Windows-side client: tails WoWChatLog.txt, parses loot/drop/kill, streams to feed.py; --replay for tests; the token from --token,
                       --token-file or OIBOT_COMPANION_TOKEN; an auth error (revoked) exits instead of reconnecting (TokenRefused); a plain hello
                       error (UNAVAILABLE) reconnects
  web/api_loot.py      the loot council on the web (design §5.36): /api/loot (+ /run/{run}), /api/loot/{run}/open|tick|distribute|override|confirm|
                       discard|reason|correct|end|… (the LootMixin verbs; LootChanged/LootBusy → 409 + `state`), history, precedents (+ retire), items
                       (+ tiers with the D12 re-weight ask), wishlists (+ TMB import), /api/me/loot, /api/me/wishlist, /api/members/wishlist, /api/loot/log
                       (members logged in; officers' `scores` filtered server-side, D2). D8: decisions from MCP need X-Oibot-Actor (app.py mcp_actor).
                       Phase 3 (§5.47): /api/loot/items/{key}/draft (DRAFTS in memory), /api/loot/items/export|import (CSV), /api/loot/companions,
                       /api/me/attendance, /api/members/attendance; councils carry bosses_progress. Phase 4 (§5.48): POST /api/loot/import (preview →
                       rev → apply; MCP apply needs the actor), GET/POST /api/loot/weights (POST = a translator through app.state.ops_translate),
                       POST /api/loot/rescore, POST /api/loot/distil + /api/loot/distil/append (the review's token = exactly the confirmed sentences)
                       Phase 4 (§5.49): /api/me/loot (offset/limit/q over ALL the member's own awards; total, more) rows carry `award` + `appeal`
                       and `appeals` (the member's own questions, never the note or who resolved); POST /api/me/loot/{award}/appeal (the member only; never MCP), GET /api/loot/appeals + POST
                       /api/loot/appeals/{aid}/resolve (officers; D8 actor from MCP) → appeals.py
```
