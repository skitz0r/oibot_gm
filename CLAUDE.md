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
- **Clock rule (user decision 2026-09-22):** no military time anywhere a person reads. One formatter, `Registry.local12`
  (`CLOCK12 = "%a %d %b %I:%M %p"`, leading zero stripped), plus `t12`/`clock12` on the web and `run_label` in Discord;
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
- **Native vs foreign data.** Native guild state (characters, events, ledger, precedents, policy) lives in the private sibling repo `../oibot_gm-data` via `store.py` (file per entity, commit per change, debounced push; git log = audit trail). Foreign reference data (profiles/) lives here. `fixtures/demo` is an anonymized copy for public use; never commit real guild data to this repo.

## Layout
```
profiles/tbc/          GameProfile YAML (buffs.yaml drives both the solver and WCL party inference)
profiles/forever/      Classic-era buff matrix: `slot` = one totem per element per shaman, `scope: raid` = cast buffs, `status` = evidence level,
                       `families:` = stacking families with the beneficiary map (buffs point at one; guild overrides in guild.yaml buffs/families → Registry.profile)
fixtures/demo/         anonymized shadow-guild data (real copy in fixtures/25bg, gitignored): REAL (BisCouncil ledger, WCL attendance/roster) + MOCK (tiers, wishlists, policy, ranks)
src/oibot_gm/
  constants.py         ROLES / MARK / LEVEL_COLOUR / TEAL shared by Discord, web and render (no per-module copies); CLOCK12 + fmt12 = the one 12-hour formatter behind every clock that is not Registry.local12
  consequences.py      consequence(verb, reg, ev) for lock / cancel / clear / release: the ONE confirm sentence from code (design §5.33); ev_json.consequences carries it to the site, wizard.decide shows it in Discord
  inbox.py             the member inbox (design §5.40): asks_of (every open ask, wherever it waits: dm | web), notices_of / inbox_lines
                       (Registry.notify = the outcome half of the DM rule), DM_ON/OFF_SENTENCE; officers on behalf: action_consequence(by_id=) +
                       do_member_action over rc.member_actions (the SetWizard, the row ⋮ menu, POST /api/admin/action); pin_write / pin_member =
                       THE pin write (every surface; re-renders the sheet's pin footer). Never a DM from here
  presets.py           shared presets: raid cadence PRESETS (standard/short/same_week), test-run TEMPOS, absence DURATIONS (the wizard's For how long
                       and /api/meta.durations); used by the wizards, plain text and the web
  profiles.py          loader; Buff.benefit(spec); Item.tier_for; equippable() guard
  importers/           biscouncil.py, wcl.py, signup.py (Raid-Helper → Player, registry mapping)
  roster/              solver.py (CP-SAT select+group, pins/forces, role min/max, per-player bonus), explain.py (advisories), coverage.py (slot-aware)
  loot/                scoring.py (candidates + base score; untiered items, neutral attendance), recommend.py (Claude structured output + fallback),
                       session.py (LootSession keyed (guild, run), Drop/drop_id, Proposal + pending Override, batch_rev/drops_rev, LootChanged/LootBusy,
                       run_distribution on a snapshot + merge, make_award/sync_ledger, load_sessions/migrate_sessions, LOOT_LISTENERS) — design §5.35
  llm/provider.py      provider boundary, per-workload routing, usage log, budget cap
  nl.py                NL → RosterRequest / LootFeedback schemas
  discord_bot.py       OibotGM client: the gateway events and on_message routing (an addressed message in a council thread → loot_chat);
                       bot.events[(gk, session id)]; GuildContext = the start directory's key, profile and the LLM provider (rehearse loot with a
                       test run — its council is session.test, D11)
  discord_loot.py      LootMixin: THE loot verbs open_loot · tick · distribute · override · confirm · discard · set_reason · correct · end_run
                       (+ remove_candidate · set_scores_only · keep_open), each (reg, session, …, by, by_id, via) under loot_lock; loot_ctx / loot_roster
                       (live from the run) / loot_live (run locked); open_council (one session per roster of a split run, <run>-r<N>); auto-distribute;
                       held companion events in <guild>/loot/held.json (expired → ops warn "NOT recorded"); loot_restore; loot_may_close (scheduler auto-end:
                       only LootSession.blocks_close — picks/undistributed — holds it, once announced; reasons/conflicts outlive it: set_reason/correct take a
                       session id after the end); loot_ensure_thread (a pending thread retried on /raid loot, on_resumed, ready); hooks the surfaces replace:
                       loot_notify(…, kind, drop_id) / loot_view / loot_open_thread / loot_ended / loot_closing / loot_held (minimal defaults);
                       retire_precedent = THE retire / restore (wraps RegistryLootContext.set_precedent_status; /gm precedents, the site, plain text, MCP)
  discord_loot_surface.py  LootSurfaceMixin (OibotGM, before LootMixin; design §5.36): the council's Discord surface replacing those hooks —
                       a PUBLIC thread on officer_channel (refused when none), the pinned session card (raid_views.loot_layout) and the proposal
                       cards (batch_layout: ≤5 items, per boss, Confirm the rest · Discard on the newest) edited after every write from any surface,
                       session.cards = message ids; persistent thread-line buttons; distribute's card ticker; loot_scheduler_step ("Start an ad hoc
                       run…" prompt in the officer channel, the one after-end DM to officers); addressed-only thread chat (loot_addressed / loot_chat →
                       help_answer(data=) or `loot` ConfigOps in LootChatConfirmView = ConfigConfirmView + Give the reason…; the revs carry the batch /
                       the drop's pick as the officer saw them); loot_cards_restore on ready
  flows_loot.py        every LootButton / LootPick press and lootmodal submit (loot_press / loot_pick / loot_modal_submit; loot_modal_raw =
                       the restart path from on_interaction); wizards loot_tick (TickWizard), loot_override (grouped select → reason modal,
                       also Correct…), loot_distribute (/raid distribute), loot_end (/raid end + End council: the decide screen), gm_precedents
  lootctx.py           RegistryLootContext (one per guild, reload(); void / reweight rows and superseding / status precedents folded; wishlists derived
                       live from Registry.wishlists; set_precedent_status = Retire/Restore; reweight (D12)); LEDGER_LISTENERS on every new award row
  loot_ops.py          the `loot` / `wishlist` / `tier` plain-text ops (configops' loot branch → LootMixin verbs / Registry wishlist + tier verbs),
                       SCHEMA (into SCHEMA_TEXT), needs_actor (D8), loot_revs (batch_rev on a confirm-all diff), loot_log_state, member_awards, loot_block
  registry.py          Member/RegisteredCharacter/Applicant/Absence + GuildConfig on the git store (one file per member);
                       schedules (norm_schedule/schedules_of/schedule_def_of; set_schedule/remove_schedule = one set_raid_overrides write, _check_schedules);
                       Recruitment on GuildConfig.recruitment (open, note, questions ≤15; invite_url; member_role_ids) + must_apply (a newcomer
                       applies in Discord before registering: every first-character path checks it — Discord, plain text, the Me page);
                       PlainPolicy on GuildConfig.plain (who per PLAIN_GROUPS, mode per channel / DMs / default), may_plain = THE plain-text check
                       Owner rights = Registry.is_owner_id(uid): the guild's owner OR a host superuser (OIBOT_SUPERUSERS, every guild) — never compare
                       owner_discord_id directly; a server claim defaults the owner to the Discord server's owner.
                       Dual interfaces phase 0 (design §5.33): `config_lock` (RLock) held by save_config and every config verb (`_config_verb`);
                       `config_rev(section)` for settings | raids:<id> | buffs | recruitment | plain (computed, excludes rosters[] / *_message_id),
                       `characters_rev(member)`, verbs take `rev=` and raise `ConfigChanged`; `preview(fn)` runs on shadow(); `member(uid, name)`
                       never renames — `rename(uid, name, by)` is the explicit step.
                       People (design §5.41): save_character_table(officer=, by_id=, rev=) validates the whole table on a copy (check_character_table)
                       then one commit; CharactersChanged on a stale rev; rank per MEMBER on the main (Member.rank, settle_ranks — alts always "alt"),
                       set_rank(uid, rank, by, by_id) / confirm(uid, label, by, by_id) refuse your own (owner aside) and another officer's live puppet
                       (puppet_refusal); add_absence refuses overlaps + a reason past ABSENCE_REASON_MAX (check_absence = no write); set_dm;
                       remove_schedule takes its regulars off (D30, no DM); owner_refusal / owner_consequence / set_owner (D50).
                       Auras (§5.43): set_buff_overrides / set_family_overrides = one validated write per buff / family, restored on refusal (the
                       single-field verbs call them); family_slug(name) (ids never typed; taken → -2); aura_override_counts / aura_reset_sentence.
                       Phase 3 (§5.44): set_comp_targets(raid|run, {slot: {min,max,note}|None}, by, rev=) = THE writer of officer comp targets (bounds
                       stay the owner's; outside them → "change the bounds first (owner)"), comp_slot / comp_slot_words; schedule_removal_effects →
                       Effects(regulars, open_runs).lines()/sentence(); schedule_runs_open; timezone_change_effects(zone) (live runs that keep their moment)
  discord_registry.py  real commands. Layout (keep it to these groups): /register, /apply (public);
                       /me view|wishlist|plan main|alt|char [action]|absent add|list|clear (members; /me wishlist = flows_wishlist, the Me page builds long lists;
                       /me char = ONE command on flows_roster.CharWizard's self mode — `plan roles` and the char subcommands are gone);
                       /roster overview (always ephemeral, per-schedule fields, officers' Post button)|add|remove|members|list|confirm|rank|set-main|char|
                       absences|absent|back|comp|applicants|applicant (officers; `comp` = flows_config.CompTargetsWizard, the desired comp's targets;
                       `poll` retired, PlanButton is a stub); /gm config show = config_show_embed (one field per configops.config_summary section);
                       Rosters (design §5.30, out of the roster redesign): a run's roster is ephemeral in config `rosters[]` (key/size/cutoffs per run) and WHO
                       holds a seat lives only in the run file (rc.rostered_runs reads it); RegisteredCharacter.rosters holds only REGULARS, as series keys
                       "<raid>:<schedule>" (Registry.regulars / set_regular; officers edit, members may step down). Schedules are RECURRING (weekly, lockout)
                       or AD HOC (pickup templates, ADHOC_SCHEDULE one-offs) — rc.run_kind; ad hoc = first come (Signup.joined_at), no confirmations, lighter
                       timing. Regulars are never pre-filled; officers see who hasn't answered. Officer posts go to `roster_channel_id`. Internal helpers still
                       say "team" (aliases) — don't rename them casually.
                       /gm status|config …(owner: ops/applications/registration/analytics/roster/signup/absences/news/loot channels, recruitment, raid, aura, officer-role, timezone, ask-audience, about; no standing rosters)|change|policy …|rule loot|comp|precedents|test …; /raid … (discord_raid.py)
                       `Guilds` = the one index of the process's guilds (design §5.32): by_discord (copy-on-write `add`, loop thread only), by_key, ordered(),
                       get(key|name), for_guild (strict), only(), many, cfg_for, load_errors; `/gm config owner` in an UNCONFIGURED server is the bootstrap
                       (flows_config `guild_claim` → OibotGM.claim_guild → registry.create_guild; any number of servers — Guilds.ALLOW_SECOND is on since S3)
  guild_routing.py     THE resolver: need(interaction, reply=True) → (reg, interaction) — every command rebinds both (and sync_name: a Member's
                       changed nick is the ONE trigger of Registry.rename; verbs never rename); an unconfigured server is told
                       "isn't set up" and never reaches another guild; DMs: only() → else guilds_of (owner / membership yes|no|unknown) → one, none (Join links),
                       several → the DM server picker (guild_picker.choose_guild: asked EVERY time, asker only, 2 min; the command continues on
                       the press); resolve_button(interaction, g, legacy) for persistent buttons; may_access = the strict "belongs to" (the web uses it).
                       THE identity predicate (design §2.6/§5.33): resolve_role(bot, reg, who, max_age=) → Resolved(role owner|officer|member|outside,
                       role_ids, stale); role_of / identity are its names (bot.role_of, bot.identity); windows PRIV_LIVE=0 (writes) / PRIV_ROUTING=10 /
                       PRIV_WEB=60; role_sync = the no-lookup form. is_officer, is_officer_anywhere, plain_identity, asker_identity and app.py privilege
                       are aliases — never a sixth check
  raidcycle.py         signup-driven cycle (design.md §5.20): raid SCHEDULES (§5.27: Registry.schedules/schedule_def; weekly slots · day N of each
                       lockout · pickup templates; `default` = the raid's own slots) + cadence (schedule_def: slots, signup_lead/lock/confirm hours,
                       weights) → upcoming/schedule_starts/open_run(schedule=) (ephemeral roster per run, key suffix -<schedule id> unless default);
                       run_rosters(ev) = the schedule's expected rosters N (health/needs/sheet measure against N × size, the lock aims for N), Join/Bench/No thanks (in/sub/out), propose() at lock (weights, pins,
                       role min/max, splits into several rosters per slot), confirmations (placement asks on the run key), free_seat/seat_player/
                       expire_confirmations, fill engine (after lock only: benched joiners + Bench first → pool → offspec → alt swaps, paired with a backfill
                       when the vacated role would go short; asks expire after fill_ask_hours). Per-raid `nudge`/`nudge_hours_before`/`autofill`/`open_dm`
                       come from raid_def. No LLM. Run states open | locked | done | cancelled; the member-facing word is "rostered", never "seated".
                       ONE planner (rc.role_plan; fill engine extended to before lock): ranked moves with alternates and a still-short line; nothing is
                       sent before an officer's Ask (RaidMixin.send_plan with the shown plan rev; rc.send_batch = exactly what a send DMs); after lock on a
                       run (recurring or ad hoc) same-role asks are automatic and ROLE SWAPS wait for an officer (D3, out/roster_decisions.md; D22). Series
                       coverage + 4-week forecast: Registry.series_coverage / rc.series_outlook (officer-only).
                       rosters_copy(reg, raid, sid, n) = the rosters-per-run sentence (the next run, never the open one — §5.44);
                       split_reason / split_reason_text: why the joiners make fewer runs than the headcount (what the next run is short, in code);
                       leftovers() = joiners the full roster(s) left out; another_run_hint = one line when leftovers + bench + pool could field another run.
                       People (§5.41): character_changes / character_effects / character_preview / after_character_change (a respec rewrites the
                       sheet answers; a retired / deleted / respecced seat is freed — a role flip on a locked RECURRING run keeps it); join_while_away.
                       Raid night (design §5.39): expire_confirmations expires only a held ask (locked runs only); unasked_seats (the lock's recovery: only
                       RaidEvent.lock_seats, the seats at the flip — never a fill-seated or later seat; legacy_lock_sent = a pre-field run is stamped); cancel_effects;
                       reopen_refusal/reopen_run (an END by mistake, REOPEN_GRACE_HOURS, never a cancelled run); remind_refusal; end_refusal;
                       open_when/which_run/open_preview (THE open-time resolver, DST-safe); spec_switches / regulars_not_rostered / fill_state_text /
                       asks_on_site / log_rows (RaidEvent.log_at stamped by RaidStore.save) for ev_json; FillAsk.channel dm|web (a failed DM = web)
  discord_raid.py      RaidMixin on the client: send_member_dm (test puppets → the tester), set_answer / call_out (the one path for an answer; `by` = a
                       display name to read, `by_id` = the actor's Discord id decides "their own" — never a name compare; seats found by id, ev.seat_of(member)), lock_run → roster
                       cards + Confirm/Can't DMs, run_fill DMs, drop_seated, open/cancel runs, after_absence ripple; re-exports the raid_* names below.
                       Phase 2 (§5.39): send_member_card + edit_ask_card (an outcome edits the ask's DM card — never an FYI DM; seat_released),
                       record_placement (check-and-set on the loop), lock_run resumable (lock_seats + confirmations_sent_at; recover_lock in the scheduler),
                       lock_preview, cancel_run(notify) under board_lock (no DMs), reopen_run, remind (the older card goes dead), set_autofill_paused, withdraw_sent_ask;
                       every writer of ev.fill_asks holds plan_lock (_send_asks is the one sender)
  raid_views.py        pure rendering: sheet_message (the signup sheet is an EMBED: inline fields are the only columns Discord has — a class per column open, a group per column locked, one member per line) / health_layout / lock_layout / confirm_layout / fill_layout / closed_layout (Discord layout components:
                       container/section/thumbnail/text/separators/buttons, real icons as app emojis via discord_bot.ensure_icon_emojis → ico()),
                       run_times, run_label/run_title/clock12, gaps_text/ask_line, leftover_block, split_away (the sheet's Away row); BotProto = what the mixins expect from the client;
                       the loot council: loot_layout (session card) / batch_layout + loot_chunks (proposal cards) / item_layout (the ephemeral item card) /
                       loot_closed_layout / loot_stub_layout, loot_status_text + presence_line (the companion chip), why_not_text
  raid_buttons.py      persistent DynamicItem buttons: SignupButton (Join/Bench/No thanks/Can't make it + character picker), FillButton, PlaceButton,
                       RunButton (officer Lock/Fill/Loot/Cancel on the cards; the lock card's second row Remind/Pause/End; the closed card's
                       Reopen; lock_decide = THE lock screen with Preview lock, cancel_decide = THE cancel screen, end_decide), LootButton `loot.<gk>:<session>:<drop|->:<action>[:<arg>]`,
                       LootPick `lootpick.<gk>:…`, LootStartButton `lootstart.<gk>:open`; constructors take the guild key first (`raid.<gk>:…` ids; old ids still match,
                       g=None → guild_routing.resolve_button + each button's legacy matcher); raid_views ⇄ raid_buttons import each other at the bottom
  raid_scheduler.py    RaidSchedulerMixin: the minute loop (open on cadence → health → lock → expire → fill → close), calls RaidMixin
  raid_commands.py     register_raid_commands: /raid open|sheet|health|lock|loot|distribute|end|set|cancel|list|out|fill
  policy.py            policy docs (<guild>/policy/*.md) + Claude compile → *.compiled.json, confirmed by an officer
  discord_pool.py      dedicated channels the bot keeps current: registration card (public, read-only); analytics channel with one native
                       card per raid (pool_layout: readiness + comp shortfalls, edited in place, no images) + change log (Registry.listeners, debounced);
                       absences channel (AbsencesMixin: card with I'll be away · I'm back · My absences → AbsenceButton `abs.<gk>:new|back|mine` and a Clear
                       `abs.<gk>:clear:<uid>:<start>`; announce_absence → sheets updated; puppets never on the card or the channel; AbsencesMixin inherits
                       discord_people.PeopleMixin);
                       SetupMixin: set_channel (web Config page + plain-text `set …_channel` via configops.apply_async), guild_channels/roles, test_bench_clear.
                       LootLogMixin: the public loot log (LEDGER_LISTENERS → one line per award in the loot channel, else the run's updates thread; never
                       the signup channel; loot_log off = nothing) with LootWishButton (`lootwish.<gk>:<item key>` → /me wishlist on that item)
                       Test bench (design §5.42): Member.test puppets; SetupMixin.test_seed/test_compose/test_comp/test_answer/puppet_asks/test_bench_clear =
                       THE verbs of /gm test seed|run|answer|compose|comp|clear, /api/test/*, plain text and MCP test_bench; test run keys end '-test'
                       (rc.TEST_SCHEDULE; ensure_run refuses a roster whose test flag differs, and another tester's run — a rehearsal is its test_by's), cards in the asking channel (roster test_channel),
                       puppet DMs → RaidMixin.puppet_dm: the feed + ONE digest per batch to the tester (TestAnswerButton); may_answer_for lets the tester
                       press their buttons, a test run's pool is puppets + tester only and its sheet refuses presses from real members
  testfeed.py          the test feed (D20): TestFeed ring per (guild, run) of card | dm | step | answer events, in memory only (never the data repo);
                       observe() on RUN_LISTENERS saves of a test run, note() from post_run_update / puppet_dm; LISTENERS → publish("test:<run>");
                       plain() = Discord text as site words; tempo_of = a TEMPOS name or minutes → the four marks
  discord_people.py    THE people verbs (design §5.41), module functions taking the client (bare test clients work) + PeopleMixin:
                       save_characters (the character table + its ripple: rc.character_changes/effects/preview/after_character_change → drop_seated
                       for a freed seat, a run-update + warn line for a kept role flip; preview=), record_absence, im_back (THE "I'm back"),
                       transfer_owner (D50; Discord only — /gm config owner, BY_DESIGN_ONLY), absence_lines / absence_clear_lines (the preview words);
                       a member's own retire / delete frees the seat with no self-notice (by_id decides). No DMs anywhere
  comp.py              pool → Players, solver run at roster size, raid-buff status, ideal_comp (targets with justifications)
  help.py              the bot explains itself: docs/manual.md + live command tree + guild settings + the asker's record → Claude (route `help`)
  discord_help.py      /help (no LLM, by tier), /ask; help_answer(may_act) flags requests to act (routing is discord_bot.plain_text:
                       @mentions and DMs routed by intent under the plain-text policy)
  wizard.py            the wizard core: one ephemeral message advancing in place (Wizard.show/working/finish/refuse), Forms; Wizard.decide = THE confirm screen (consequence · Do it · Do it with a note… · Back; guard re-run on every press)
                       (a select inside ui.Label, or a TextInput only for prose), owner-only presses + a guard re-checked on
                       every press, FLOWS registry (@flow), no-typing dates/times (day/hour/minute selects, 12-hour,
                       resolve_local = DST-safe), lint() = what the Discord API would refuse, checked offline
  wizard_opts.py       registry data as select options (raids, live runs, a member's characters, classes, specs, ranks)
  wizard_flows.py      imports every flows_*.py so FLOWS is complete; flows_absence (the reference flow + AbsenceClearWizard = I'm back) · flows_raid ·
                       flows_config (+ OwnerTransferWizard; AuraWizard = a draft hub per buff, one Save this buff through configops.apply_ops; CompTargetsBlock = the
                       Group make-up screen's targets block, in RaidConfigWizard and in CompTargetsWizard = /roster comp) · flows_roster (MemberWizard/CharWizard: /me char, /roster char|rank|set-main) · flows_register (incl. the Discord application: picks → names → schedules → the guild's
                       questions five per modal → review) · flows_recruit (/gm config recruitment) · flows_wishlist (/me wishlist) · flows_misc · flows_test (/gm test compose|comp, the digest's TestAnswerButton). Commands and card buttons call FLOWS[name].
                       tests/wizard_harness.py drives a flow end to end with fake interactions and lints every screen
  configops.py         plain-text config: whitelisted ConfigOp schema (flat, ≤13 fields; `target` = raid id / run key / buff id / family id; schedule_set field = <schedule id>.<setting>), describe() diff,
                       apply() via the same code paths as commands, apply_async(bot=) for card-posting channels and announced absences; request text in <request>;
                       OP_GROUP (every op in one capability group), bind_self (own-record scope decided in code, never by the model),
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
                       kind → comp_target ops → Registry.set_comp_targets; schedule_pause_sentence; the timezone CONSEQUENCE_HOOK
  cli.py               `oibot roster|loot|demo|discord`; root logger → out/oibot.log (rotating) + stderr
  mcp_server.py        `oibot-mcp` (stdio MCP server, FastMCP): tools named by intent (guild_overview, get_run, set_answer, lock_run, plain_change …)
                       that call the RUNNING bot's /api/* over HTTP as the OWNER OF THE CHOSEN GUILD (every tool's optional `guild` = key or name,
                       default OIBOT_MCP_GUILD / --guild, else the only guild; names → keys client side via GET /api/guilds; header X-Oibot-Guild;
                       list_guilds; the bot never reads OIBOT_MCP_GUILD) (`Authorization: Bearer $OIBOT_MCP_TOKEN` → web/app.py `mcp_authorized` + `mcp_guild` → `mcp_viewer`,
                       via=mcp in the request log; LOCAL ONLY by default — OIBOT_MCP_LOCAL_ONLY, web/app.py local_request: loopback client and no proxy
                       headers, so the token never works through the tunnel). Never a second code path: every write is the site's route. `.mcp.json` (repo root, no secrets)
                       registers it for Claude Code; the token lives in `.env` only. Design §5.24, manual §8b, tests/test_mcp.py.
                       Loot tools (§5.36): reads / tick / distribute / discard / end as the token; DECISIONS (loot_confirm/award/reason/correct,
                       retire_precedent, tier_item, set_wishlist, import_wishlists) send X-Oibot-Actor = OIBOT_MCP_ACTOR (D8; app.py mcp_actor validates it).
                       Setup tools (raid_set, schedule_*, raid_reset, aura_*, family_set, set_channel, set_config, set_recruitment, set_plain_permissions) send
                       TYPED changes through ops_change(guild, changes, preview) → POST /api/ops/change (`confirmed`; preview=True returns the Described lines);
                       set_comp_target (a `comp` change), list_regulars (GET /api/readiness series), config_summary (GET /api/config/summary) — §5.44
  discord_policy.py    /gm policy show|edit|reload, /gm rule loot|comp, /gm change; handle_change for every plain-text path (ConfigConfirmView re-authorizes the presser;
                       diff_text = the diff's body: setup ops as configops.describe_ops(…).as_discord(), other ops their preview_line)
  news.py              news review (design §5.26): keyword filter of #news webhook embeds (profiles/<v>/news.yaml version_terms + raid names +
                       guild news_keywords) → <guild>/news.jsonl (dedupe by url); Proposal/ProposalStore (<guild>/proposals/<id>.yaml,
                       proposed → approved|dismissed → applying → applied|failed|reverted; officers decide, only the MCP bearer reports job steps)
  discord_news.py      NewsMixin: news_message (the ONE hook at the top of on_message; webhooks are bots), news_backfill, proposal_card_update
                       (ops-channel card edited in place); ProposalButton (DynamicItem Approve/Dismiss, officers only)
  agents.py            local jobs on the Mac mini: out/agents/<job>.json status + runs/<job>-<stamp>.jsonl transcripts (last 30), stream-json → steps,
                       launchctl print/kickstart, claude command lines (allow/deny lists, dontAsk; never a permission bypass)
  feed.py              companion listener: aiohttp WebSocket on the tailnet (OIBOT_FEED_TOKEN/BIND; OIBOT_FEED_TOKEN_<KEY> binds a companion to one guild), hello+token;
                       dedupe is per loot SESSION (seen_idem), an `error` reply is not an ack; presence(guild) live | quiet | disconnected; status(guild) one line
  discord_feed.py      FeedMixin through the LootMixin verbs: drop → tick (+ auto-distribute); loot → confirm / mismatch (recorded, reason pending) / manual /
                       conflict; kill. loot_route(comp): the token's guild → one live council (test only from a puppet/tester companion; several → by
                       seated character; else refused; a council past its run's close never competes with tonight's); no live run → held 10 min
                       (persisted); feed_presence(bot, reg) rows, feed_status the line
  ops.py               ops feed (channel line per action; errors DM the owner); the recent-lines ring is per guild (recent_for(key)), LISTENERS get the key.
                       ONE audit format: audit_line / audited(ops, reg, by, line, via, level, by_id) → "[web] Jon (host): …" — `by` is a display name,
                       the surface is the prefix from via (web|mcp|chat; none for Discord), " (host)" = a superuser in a guild they don't own, no DM.
                       set_channel, apply_async(via=) and the web's mutation helpers write through it
scripts/agents/        loop EVERY guild (Api().guilds()): review.py (daily 9:00 AM; one tagged run per guild, its own since, a Claude session only for a
                       guild with new news, pinned with OIBOT_MCP_GUILD), apply.py (every 15 min; oldest approval across guilds; guild settings by the bot, profile
                       edits by Claude then checks/commit/push/restart/health/revert by the SCRIPT; --pr), menubar.py (rumps via uv --with),
                       install/uninstall.sh; plists in scripts/launchd/
companion/             Windows-side client: tails WoWChatLog.txt, parses loot/drop/kill, streams to feed.py; --replay for tests
  web/app.py           FastAPI served inside the bot (OIBOT_WEB_BIND): Discord OAuth2, landing page, icon/emblem/card images, old URLs → /app redirects;
                       published by a Cloudflare Tunnel (~/.cloudflared/config.yml → gm.earlyandoften.gg). Several guilds: `selected_reg` picks ONE guild per
                       request — `X-Oibot-Guild` header / `?g=<key>` (explicit: honoured or 403 not_member, never switched) > `oibot_guild` cookie > first
                       guild the person may open (guild_routing.member_of); homepage/not-member page = one card per server (one guild: the old page byte for byte)
  web/api_agents.py    /api/news, /api/proposals…, /api/agents (the monitor), /api/agents/profile (effective profile, overrides marked)
  web/api_ops.py       install_ops_api (called from api.py): POST /api/ops/change — `text` (officers, Claude) or typed `changes`/`ops` (tiered: a Save applies
                       unless Described.review, then needs_review + token; `confirmed`; `revs` config:<section> → 409; BOOT-prefixed tokens: expired → silent
                       re-preview when the fingerprint matches, restarted → shown again); the admin setup routes (/api/admin/config|aura|family|aura/reset|raid|
                       raid/reset|raid/schedule|raid/schedule/remove|recruitment|plain-permissions) are TRANSLATORS into changes through apply_ops
  web/api_me.py        install_me_api (called from api.py): the inbox routes — asks/notices on GET /api/me (api.py), POST /api/me/run/{key}/answer|out,
                       /api/me/fill/{key}, /api/me/ask/{id}, /api/me/notices/read (a member's own answers through the RaidMixin verbs, by_id = me);
                       officers: GET /api/admin/inbox|actions, POST /api/admin/action (rev → 409); sheet_row_extras
  web/api_loot.py      the loot council on the web (design §5.36): /api/loot (+ /run/{run}), /api/loot/{run}/open|tick|distribute|override|confirm|
                       discard|reason|correct|end|… (the LootMixin verbs; LootChanged/LootBusy → 409 + `state`), history, precedents (+ retire), items
                       (+ tiers with the D12 re-weight ask), wishlists (+ TMB import), /api/me/loot, /api/me/wishlist, /api/members/wishlist, /api/loot/log
                       (members logged in; officers' `scores` filtered server-side, D2). D8: decisions from MCP need X-Oibot-Actor (app.py mcp_actor)
  web/api.py           JSON API (/api/*) for the React app + SPA mount at /app (history fallback to index.html). POSTs need `X-Requested-With: oibot` (CSRF).
                       Mutations reuse Registry/configops exactly like the Discord commands. Every route reads `v.reg` (the selected guild); `POST /api/guild`
                       sets the selection; SSE is per guild (`publish(topic, guild)`; None = every page); Discord-side site URLs carry `?g=<key>`.
                       Phase 3 (§5.44): POST /api/ask (TellTheBot's question half over HelpMixin.help_answer: officer view for officers, member view
                       otherwise; an officer's request to act → {act: true}), GET /api/config/summary, /api/readiness + targets/bounds/rev/slots,
                       /api/meta timezones (grouped, picked never typed) + about_max + cadence_presets, schedule_json next_if_active + the server's sentences
frontend/              React + Mantine app (Vite). components/board/* (SheetCard, BoardView, SplitModal, BoardPreview, FillModal), components/Cells.tsx (icon-only cells),
                       components/CharacterTable.tsx (the Me page's characters), AbsencesModal/AbsencesCard/AbsencesTable.tsx (From picked on a calendar + For how long, I'm back),
                       components/ConfirmModal.tsx (useConfirm), components/ReviewModal.tsx (THE edit-mode review: ReviewModal + useOpsSave = the tiered Save over
                       /api/ops/change, the result sheet, stale-token / restart handling; api.ts opsApi + OpsChange / Described / OpsReply), components/PlainPermissions.tsx (Ops page: who may do what, where), components/ScheduleCard.tsx (Raids page schedules + editor: Pause/Resume, diff-only Save, presets, the server's preview), components/TellTheBot.tsx (the officers' box: /api/ask → or the plain-text preview + Apply; officer pages' rail + each SheetCard; NEVER on Me — D40), components/CompTargets.tsx (the Desired comp table + Edit targets, a `comp` change; Readiness + Raids), hooks/useLive.ts (reload on /api/events server-sent events; board drags are single `/move` ops, whole-board writes carry `rev`) + hooks/usePoll.ts (45 s fallback); game constants come from /api/meta (roles, statuses, group caps, class colours); the server selector (rail, when `meta.guilds` > 1) and `api.ts setGuild` → every call sends `X-Oibot-Guild`, images/EventSource add `guildQuery()`. `npm run build` writes src/oibot_gm/web/static/app (committed, so the bot runs without Node);
                       `npm run dev` proxies /api,/img,/auth to the bot on :8788. Pages: Me (+ My loot, Wishlist) · Loot log (members) · Rosters · Raids · Readiness (pages/Readiness.tsx: GET /api/readiness, officers' targets) · Members (bank + admin merged; officers edit anyone's characters/grid; components/TestBench.tsx = seed/compose, open and answer test runs, the comp sandbox, TestFeed.tsx = the live test feed; Rosters' per-raid Test tab) · Loot (components/loot/*: Tonight · History · Precedents · Items · Wishlists) · Agents (the news review's monitor) · Ops · Config (Overview from config_summary · Settings edit mode · Channels + News · Recruitment and more; Mantine, left rail,
                       tables switch to an edit mode with one Save). Screenshot audit: `uv run python scripts/shots.py` (Playwright, desktop + phone)
  store.py             GitStore: atomic writes, append-only jsonl, commit + debounced push; resolve_data_root()
  render.py            raid thumbnails and generated class / role / buff badges (the emoji fallback; no roster or analytics images any more);  report_html.py → out/coverage.html
  cli.py               `oibot roster|loot|demo|discord`
out/                   generated reports/logs only (event state is in the data repo)
```

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
