# Layout — members, characters, GuildConfig, schedules as stored, migrations and the way back, the Discord command groups, guild routing and the identity check

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  registry.py          Member/RegisteredCharacter/Applicant/Absence + GuildConfig on the git store (one file per member);
                       schedules (norm_schedule/schedules_of/schedule_def_of; set_schedule(sid, fields, by, raid=) / remove_schedule(sid) through
                       _write_schedules = THE whole-list write of a raid's schedules, _check_schedules; follow {raid, rosters} (parse_follow,
                       follow_words) — §5.62);
                       STORED at guild level (§5.67): GuildConfig.schedules [{id, lineup [{raid, rosters[, starts]}] ≤ LINEUP_MAX 4, …}] —
                       a row's follow = lineup[1], `then` = lineup[2:] (row_lineup / parse_then); addressed by its guild-unique id alone (phase 3);
                       to_stored_schedule / from_stored_schedule (exact inverses), parse_lineup (LINEUP_MAX_WORDS, LINEUP_ORDER) / parse_lineup_text
                       (plain text's "barrow_deeps x2, onyxias_lair"); lineup_offsets (C12: a later raid starts when the one before ends) /
                       later_words; raid_rows(raid) = its rows in the old shape, _set_raid_rows = THE write,
                       raid_override(raid) = config.raids[raid] + `schedules` (its rows; no `slots` since phase 3 — the profile's raid slots are no
                       longer read) as an override looked before (raid_def merges it, so schedules_of and every caller are unchanged; never read
                       profile/config raids' schedules directly); revs: raids:<id> = the raid's own changes only, schedule:<id> = one schedule
                       (absent = the id is free), raid_all:<id> = the raid AND its schedules (the reset's alone, clear_raid_override);
                       lift_schedules(cfg, raids, refused, emptied) = an older file's raids.<id>.schedules / slots moved at every load (schedules_lifted /
                       schedules_refused / schedules_unread), migrate_schedules() saves it once at start (one commit, FIRST in setup_hook:
                       discord_bot._schedules_save → rc.migrate_at_start, with the live runs' stamps; _schedules_migrate on ready = the ops lines), _lift_raid at every raid write (a raid the
                       lift refused takes no raid write until fixed by hand or reset); an unreadable entry (unread_schedule) stays in the file, skipped;
                       GOING BACK: older code loads a lifted file, DROPS the list (GuildConfig ignores unknown keys), shows no schedules and
                       writes the file without them at its next save — so stop the bot, run scripts/lower_schedules.py (registry.lower_schedules:
                       each raid's entries back under raids.<id> as raid_override reads them, the list removed, one commit per guild; an
                       unreadable entry refuses that guild), then start the older code; back to phase 2's code = --ids-only, then commit 8b3bc81
                       (= 1821e4d's code) — never a55a112 (it holds phase 3's first task and would lift the ids again at its start);
                       Phase 2 (§5.67, C7): SCHEDULE_TIMING = the nine values each stored schedule carries written out (write_out_timing on every
                       _set_raid_rows; timing_suggestion(raid) seeds a new one and is what "inherit" means); a raid refuses timing or a cadence preset
                       (TIMING_ON_SCHEDULE); effective_raid(base, over) = raid_def's pure merge; lift_timing(cfg, raids, skip) at every load (on a copy:
                       timing off every raid onto its schedules (since phase 3 never a `default` entry from the game's run times), live run dicts stamped with
                       fill_ask_hours, a refused raid kept) → timing_moved / timing_dropped, saved by migrate_schedules in the same commit, the start's
                       second ops line from timing_saved; minimums (D16): raids.<id>.minimums {slot: {min, note?}}, minimums(raid, size) = THE reader
                       (the game's tank / healer scaled up, a raid's own half up, never below 1), set_minimums = THE writer (NO_UPPER_LIMIT,
                       MINIMUM_ON_RAID, role_limit_refusal; a minimum of 0 is never saved — it clears the guild's own — and a STORED 0 reads as absent,
                       §5.67 phase 4: game_minimum / cleared_words / minimum_words / GAME_MINIMUM_WORDS / NO_MINIMUM_WORDS = what a slot reads without
                       the guild's own; section_words(section, reg) names a raid, "schedule's settings", "raid's settings", never an id), lift_minimums at load (comp / comp_targets → minimums; maxima and tank / healer aims
                       dropped; its ops line), lower_minimums = scripts/lower_schedules.py --minimums-only (the way back to phase 1 = main 53b9153,
                       never 4ef2e35); a lift that fails at load leaves the file and is one ops warning (lift_failed_line);
                       Phase 3 (§5.67): schedule ids are GUILD-UNIQUE (^[a-z0-9][a-z0-9_-]{0,15}$; RESERVED_SCHEDULE_IDS default / adhoc / test / after;
                       no DEFAULT_SCHEDULE, series_key or parse_series any more); all_schedules / schedule_entry(sid) / first_raid(sid) /
                       schedule_name(row) (a schedule is NAMED, never shown by its id: an unnamed one by its kind); schedule_raid(sid, raid) = naming a
                       raid (or lineup) means CREATE — an existing id → ID_TAKEN, SCHEDULE_EXISTS only on an edit-shaped write naming the schedule's own raid
                       (plain text, no kind; a create-shaped one — it names a kind — is ID_TAKEN for its own raid too); a new id needs
                       its raid (NO_SCHEDULE); RUN_TIMES_ON_SCHEDULE refuses a raid's `slots` / `schedules`. lift_schedule_ids(cfg, raids) at every load
                       (after phase 2's lifts, on a copy): an old `default` entry → lifted_schedule_id(raid) ('barrow-deeps', ≤ 16, '-2' while taken)
                       named DEFAULT_SCHEDULE_NAME "Raid nights", kind weekly; a duplicate id → '<id>-2'; an id an alias already gives another raid's
                       schedule stays with it; GuildConfig.schedule_aliases "<raid>:<old id>" → id, one per ENTRY no alias answers for yet (an entry
                       fixed by hand later gets its own; _write_schedules gives a new schedule its alias in the same write; outside the settings rev);
                       run dicts' series / schedule_id translated. Regulars and applicants' schedules are schedule IDS: remap_regulars (end of reload,
                       in memory) translates an old key through the aliases, keeps one whose raid the aliases don't fully answer for (_aliased_raids:
                       a refused, unreadable or failed lift), drops and names one of a removed schedule, drops a stray bare key at the first lift
                       only; old_key_raid(key) = THE one reader of an old key's raid; series_label of a gone key = "A removed Barrow Deeps schedule";
                       unread_entry_name names an unreadable entry by raid + name. migrate_schedules saves it all in the one commit (guild.yaml +
                       every changed member / applicant file) → ids_saved → schedule_ids_line ("Each schedule now stands on its own instead of under one
                       raid, and its regulars came with it: …" — no id); the commit message = migration_message(stamped) (what the save changes,
                       each clause once: lift, timing, make-up, ids + aliases, regulars / applications re-keyed, spots dropped, runs stamped).
                       Way back: lower_schedule_ids(cfg, members, applicants) via lowered_id (an alias counts only for its own first raid) =
                       scripts/lower_schedules.py --ids-only (refuses a lineup of 3–4 raids, two schedules lowering to one id, or a run phase 2's
                       scheduler would get wrong — _run_refusal, one sentence per run by raid + time: a live run whose key it wouldn't build, a
                       cancelled / finished schedule run with such a key whose own night is ahead (it would open it again: wait, or pause / remove
                       the schedule), a run that stands in for another night still ahead (wait until it passes)); --minimums-only and no flag lower the ids first.
                       The script is ALL OR NOTHING (the final fix wave): every guild checked and lowered in memory first, nothing written unless
                       every guild passes (exit 1), guild.yaml written last; --guild KEY (repeatable) lowers chosen guilds, an unknown key is refused;
                       a finished run file naming no raid is skipped, a live one refuses its guild in one sentence; a guild already lowered (an
                       interrupted run) reads as nothing to lower. NOTICE_KINDS gains `moved`
                       Recruitment on GuildConfig.recruitment (open, note, questions ≤15; invite_url; member_role_ids) + must_apply (a newcomer
                       applies in Discord before registering: every first-character path checks it — Discord, plain text, the Me page);
                       PlainPolicy on GuildConfig.plain (who per PLAIN_GROUPS, mode per channel / DMs / default), may_plain = THE plain-text check;
                       PlainPolicy.listen {channel id: watch|reply} (LISTEN_MODES / LISTEN_WORDS, §5.59; merge listen / set_listen, refused in an ignore
                       channel), listen_mode(channel) / listen_channels()
                       Owner rights = Registry.is_owner_id(uid): the guild's owner OR a host superuser (OIBOT_SUPERUSERS, every guild) — never compare
                       owner_discord_id directly; a server claim defaults the owner to the Discord server's owner.
                       Dual interfaces phase 0 (design §5.33): `config_lock` (RLock) held by save_config and every config verb (`_config_verb`);
                       `config_rev(section)` for settings | raids:<id> | schedule:<id> | raid_all:<id> | buffs | recruitment | plain | loot (computed, excludes rosters[] / *_message_id),
                       `characters_rev(member)`, verbs take `rev=` and raise `ConfigChanged`; `preview(fn)` runs on shadow(); `member(uid, name)`
                       never renames — `rename(uid, name, by)` is the explicit step.
                       People (design §5.41): save_character_table(officer=, by_id=, rev=) validates the whole table on a copy (check_character_table)
                       then one commit; CharactersChanged on a stale rev; rank per MEMBER on the main (Member.rank, settle_ranks — alts always "alt"),
                       set_rank(uid, rank, by, by_id) / confirm(uid, label, by, by_id) refuse your own (owner aside) and another officer's live puppet
                       (puppet_refusal); add_absence refuses overlaps + a reason past ABSENCE_REASON_MAX (check_absence = no write); set_dm;
                       remove_schedule takes its regulars off (D30, no DM); owner_refusal / owner_consequence / set_owner (D50).
                       Auras (§5.43): set_buff_overrides / set_family_overrides = one validated write per buff / family, restored on refusal (the
                       single-field verbs call them); family_slug(name) (ids never typed; taken → -2); aura_override_counts / aura_reset_sentence.
                       Attendance (§5.47, D49): done_runs (cached), attendance(member, window_days, bench=) → (attended, total) = rostered (+ confirmed on
                       recurring) of the runs they were a regular of / joined and not away; attendance_runs (rows), attendance_tracked, attendance_known
                       (None until ATTENDANCE_MIN_RUNS real runs: neutral), attendance_text = THE sentence; window = loot_cfg attendance.window_days
                       Phase 3 (§5.44): comp_slot / comp_slot_words (a minimum's slot; set_minimums replaced set_comp_targets); schedule_removal_effects →
                       Effects(regulars, open_runs, loses).lines()/sentence() (loses = the open runs' lock fate: the raid's weights, no regulars);
                       schedule_runs_open; timezone_runs(zone, rs) = THE rows (old/new/moved, locked, movable) behind timezone_change_effects(zone, rs,
                       mode) and timezone_refusal (a locked run + no mode → refused); set timezone carries field keep|move (ops_from_fields
                       timezone_runs), rc.move_runs_with_clock moves only starts_at (same key/sheet/signups), on the loop via _apply_async
                       Setup (§5.46): GuildConfig.error_dm_ids (None = the owner, [] = nobody; set_error_dms / error_dm_targets / error_dm_words) and
                       detached_at (set_detached; Guilds parks it); Member.left_at (member_left) + remove_record (officers only, someone who left, no
                       live seat — D29: no self-service delete); applications: decide adds the character FIRST, caps the note (DECISION_NOTE_MAX),
                       decided_refusal ("Already declined by X"), Applicant.history → prior_decision_line, role_note (D31), application_state (own first)
                       Companion tokens (§5.50): GuildConfig.feed_tokens[] FeedToken {id ft-…, hash, label, issued_by, issued_at, sent_to, revoked_at,
                       last_seen} — HASHED, in REV_SKIP; issue_feed_token(label, by, by_id) → (token, record) = the value returned ONCE, never stored;
                       revoke_feed_token(id|label); match_feed_token (constant-time over every hash); feed_token_active; touch_feed_token (last_seen,
                       saved at most every 15 min). Lines and commits name the label and the id, never the value
                       The server's guild (armory link §5.1, D1): GuildConfig.armory ArmoryGuild {region, guild_id, slug, name, faction, linked_by,
                       linked_at} | None (in the `settings` rev); set_armory_guild(facts, by, *, by_id, rev=) / clear_armory_guild(by, *, by_id, rev=) =
                       owner only (is_owner_id), one commit each, the caller audits; armory_guild_words = the link in words (or how to make one)
                       The auction house (§5.65, D3): GuildConfig.market Market {ruleset_id, house_id, name, faction, picked_by, picked_at} | None (the
                       `settings` rev); set_market(house, ruleset_id, by, *, by_id, rev=) / clear_market(by, *, by_id, rev=) = owner only, one commit,
                       the caller audits; refused without auction houses, without a linked guild, or for another faction's house; the house already
                       picked = no write (callers skip the audit and the read); market_words = the choice in words or what is missing;
                       market_survives(facts) (C11) — set_armory_guild clears a market that doesn't survive, clear_armory_guild always, same commit
                       The watch list (§5.66, C7, C9, C11, C13, C14): Member.watching = item ids, newest first, ≤ WATCH_MAX 100 (25 until 2026-11-15; /price shows the newest flows_prices.LIST_SHOWN 25 + newest_words, the site all); watching(uid) /
                       watch(uid, item) (refused without auction houses, without a record — WATCH_REGISTER_FIRST, a watch never creates one — and
                       full — WATCH_FULL; already there = no write) / unwatch (never refused); one save_quiet "<name>: watch list changed" each
                       (never the item; no ops / change-log line, no notice, no DM); a puppet has none; read by nobody else (no officer, MCP, help) — the guild's list (prices.guild_watch) is a count per item, never who
                       Loot data on-ramp (§5.48): import_awards(rows, by, by_id, preview=, kind=, rev=) = THE history import (dedupe on (character,
                       item id|name, day) for imports only; appends source=import rows, never edits); loot_cfg merges `loot:` at every depth;
                       loot_weight_defaults / loot_weights / loot_overridden / loot_weight_check; set_loot_weights(fields, by, rev=) = one write of the
                       new `loot` config section; import_tmb_wishlists reports per_character
                       Professions (§5.60): RegisteredCharacter.professions [Profession {id, skill, recipes [Recipe {name, id}]}]; check_professions = THE
                       validation (a profile profession, skill 1..skill_cap, ≤ trade_slots crafting + gathering, once each; without the game's list: recipe
                       names ≤ RECIPE_NAME_MAX and unique after norm_recipe, none where ProfessionDef.makes is false); set_professions = the one setter, called by
                       save_character_table's `professions` row key (the whole list replaces); professions_summary.
                       With the game's list (§5.64, _list_recipes): Recipe.id = the recipe's IDENTITY (name = the list's spelling); an id must be on the list
                       and of that profession, a name alone resolves within its profession (twins → the lowest id, R6) or is refused ("isn't a recipe in the
                       game's list" / "isn't a <P> recipe"); an id-less name round-trips only when already STORED on the character (stored_unmatched →
                       keep_unmatched; `unmatched: true` from professions_entries = THE round trip every editing surface reads); "has recipes" = the
                       list gives the profession any (R7); dedupe by id; trusted=True (phase 2's sync) skips slots, cap and the list.
                       check_hand_professions(c, entries) = THE hand check (set_professions and every preview): professions_refusal (a synced
                       character, the write's own sentence) then check_professions(held=c.professions) — what the character holds round-trips
                       as stored (an off-list recipe by id or name, a skill over the cap unchanged, slots exceeded with none added); the hand
                       rules apply to what is new or changed
                       migrate_recipes = the once-per-start match of typed recipes to the list (one commit per guild, nothing when nothing matches;
                       discord_bot runs it per guild at ready in a thread → professions.migrated_line, one ops line)
                       Character names (armory link, spec §5.2, C9): RegisteredCharacter.armory (ArmoryLink {id, slug, linked_by, linked_at, checked_at,
                       modified, level, cls, missing_since}; written only by the verbs below; an old in_guild key is ignored) / profession_sync / former_names (old labels, oldest first);
                       same_character(c, label, former=) = THE by-name predicate (the label now or a former one); own_character(chars, ref) = one member's
                       (a current name → a first name → a former name); holder_of = who holds a full name NOW (the taken-name check: a former name never
                       blocks — find(full, former=False) at register / apply / add / name / import); find_full = a full name now, else a former one (two
                       characters that had it → None, never raises); find and matches answer to a former name (a current name wins); award_holder(a) =
                       whose an award is — an award WITH a Discord id is decided by the id, never by its name; an id-less row by the full name — My loot,
                       received marks, scoring's history, the loot log (member filter, class, `mine`), help, reweight read it (the import dedupe resolves
                       an id-less ledger row with `find`, as its file rows); award_is_for(a, name) = the companion's / correct's / a conflict's compare;
                       same_name(a, b) = two id-less names. rename_character(uid, label, name, surname, by, *, rs (REQUIRED: the bot's RaidStore), by_id,
                       officer, relabelled) = THE character rename: refused with
                       nothing written for a name another record holds — a full name is unique across every record, puppets included (R10); name_taken_refusal = the sentence ("<Name> is registered to <member>. Move it or retire it first." / a puppet's: "<Name> is held by a test character. Clear the test bench first." / "That name is
                       already registered here. Ask an officer."); the old label → former_names (a name taken back leaves it); ONE commit = the member +
                       every live run's labels (rc.relabel_runs: signup, seats, fill asks, callouts; `relabelled` gets the keys to redraw) + their
                       placement asks; the ledger, award ids, appeals and finished / cancelled runs keep the old name; the same name = no write
                       A move (§5.4, the owner's decision 2026-11-12): move_character(from_id, label, to_id, by, *, rs (REQUIRED), by_id, moved) = THE
                       move of a record to another member (wishlist, regular spots — one per member per series: a spot the new member holds stays theirs —,
                       armory link, former names travel; an alt, or the main when they have none; the old member's next character is their main, R3);
                       ONE commit = both members + a row of <guild>/character_moves.jsonl {label, name, names, firsts, armory_id, from, to, by, by_id,
                       at} (append-only, character_moves() cached by stat; names / firsts = _move_names FROZEN at the move: its full names, and its first
                       names no other character of the old member answered to) + every live run (rc.move_on_runs) + the answered placement asks on them +
                       a `character` notice to each (never a DM); move_effects(m, c, to) = {kept, dropped (spots the new member already holds), next_main,
                       arrives_main} — the verb and the confirm read it; move_refusal (one sentence: an open ask, a name the new member's records answer
                       to, not registered / left / the same member / puppet ↔ real, a move TO yourself — owner aside —, the new member already on a live
                       run the character is on). owner_of(uid, label, when) = THE resolver of "whose award / attendance", from the moves rows alone: an
                       entry before the move by a frozen name → the member it went to; one at / after it by a full name the old member had no own
                       character for by then (created_at) → too; chains fold; a day = its guild-local first moment; no moves = uid, a stat only;
                       award_time(a) (confirmed_at, else the day) = the one time owner_of_award and appeals.appeal_owner read; owner_of_award feeds
                       award_holder, My loot, received marks, scoring, the loot log, help, appeals and the loot-log notice; _attendance_row_now = one row
                       per run, the BETTER of the member's own and a moved character's (_row_rank: attended > played > no answer / away; the old member
                       keeps a row only for another character). The ledger, finished runs and appeal files are never rewritten
                       The link's writes (§5.2/§5.3, one commit each, under the member-write lock): armory_holder(id) (one claim per armory character per
                       guild: an ACTIVE character of a real member, never a puppet), armory_linked() (what the hourly check reads: no puppets, nobody who left, no retired character),
                       armory_character(uid, id) (found by the link, never a name), link_refusal(uid, label, id, name, surname, by_id=, officer=) (a test puppet /
                       already linked / another's claim — names the holder to an officer only / a taken name), link_armory(uid, label, link, by,
                       professions=) (profession_sync back on; the caller renames first in the same batch), unlink_armory (professions stay, kept by
                       hand), set_profession_sync(uid, label, on, by) → changed?, armory_update(uid, id, changes, professions=, ride=) = THE check's write
                       (nothing differs → no write; `ride` = Last-Modified / checked_at stored only with a commit; save_quiet "<member>: <label> updated
                       from the armory"). set_professions refuses a CHANGED list on a linked, sync-on character in one sentence (R2; unchanged passes).
                       give_refusal(uid, armory_id, name, surname, *, label=, row=, by=, by_id=) = Give to a member…'s every refusal before a write (D2:
                       registered here, not left; link_refusal; a new row through check_character_table + link_refusal on a shadow copy)
  discord_registry.py  real commands. Layout (keep it to these groups): /register, /apply (public);
                       /me view|wishlist|loot-ask|plan main|alt|char [action]|professions|absent add|list|clear (members; /me wishlist = flows_wishlist, the Me page builds long lists;
                       /me loot-ask = flows_appeal, a question about one of your own awards;
                       /me char = ONE command on flows_roster.CharWizard's self mode — `plan roles` and the char subcommands are gone);
                       /roster overview (always ephemeral, per-schedule fields, officers' Post button)|add|remove|members|list|confirm|rank|set-main|char|professions|armory (flows_armory)|
                       absences|absent|back|comp|applicants|applicant (officers; `comp` = flows_config.CompTargetsWizard, a raid's minimums;
                       `poll` retired, PlanButton is a stub); /gm config show = config_show_embed (one field per configops.config_summary section);
                       Rosters (design §5.30, out of the roster redesign): a run's roster is ephemeral in config `rosters[]` (key/size/cutoffs per run) and WHO
                       holds a seat lives only in the run file (rc.rostered_runs reads it); RegisteredCharacter.rosters holds only REGULARS, as schedule
                       ids (§5.67 phase 3; an older "<raid>:<schedule>" key is translated at load through schedule_aliases, never split) (Registry.regulars /
                       set_regular; officers edit, members may step down). Schedules are RECURRING (weekly, lockout)
                       or AD HOC (pickup templates, ADHOC_SCHEDULE one-offs) — rc.run_kind; ad hoc = first come (Signup.joined_at), no confirmations, lighter
                       timing. Regulars are never pre-filled; officers see who hasn't answered. Officer posts go to `roster_channel_id`. Internal helpers still
                       say "team" (aliases) — don't rename them casually.
                       /gm status|config …(owner: owner (first claim / transfer), the eight <kind>-channel shortcuts = CHANNEL_SHORTCUTS → ONE helper (channel: | clear:)
                       → FLOWS["channel_set"] (the one-screen confirm), channels (the hub), officers (the role hub), officer-role (apply_async), errors-to, loot-channel,
                       recruitment, raid, aura, timezone, ask-audience, about, companion (flows_config.CompanionsWizard, §5.50), armory (flows_config.ArmoryGuildWizard, §5.1 — the 25th and last subcommand), show (config_summary sections), plain (the D37 READ view, discord_policy.py); no command
                       writes reg.config — set_channel / Registry verbs do; no standing rosters)|change|policy …|rule loot|comp|precedents|test …; /raid … (discord_raid.py); /professions who|list (members: recipe autocomplete, ephemeral, names only; `who` adds prices.cost_line when prices show);
                       /price (members, §5.65: item autocomplete over prices.search, ephemeral, no model; "This game version has no auction houses." on tbc;
                       §5.66: item optional → FLOWS["price"]: with one, Watch; with none, Your watch list);
                       /guide list|get|upload|withdraw|restore (discord_guides.py → flows_guides; withdraw|restore officers — §5.63);
                       /roster remove-record (decide screen → remove_record). Applications: settle_application(…, by_id, via) = THE verdict (one audit line,
                       assign_member_role D31, the DM they wait on), application_decide (card + /roster applicant), application_consequence, client_decide
                       `Guilds` = the one index of the process's guilds (design §5.32): by_discord (copy-on-write `add`, loop thread only; detach(key) /
                       attach_server(gid) / detached_list() — a parked guild is out of every index, §5.46), by_key, ordered(),
                       get(key|name), for_guild (strict), only(), many, cfg_for, load_errors; `/gm config owner` in an UNCONFIGURED server is the bootstrap
                       (flows_config `guild_claim` → OibotGM.claim_guild → registry.create_guild; any number of servers — Guilds.ALLOW_SECOND is on since S3)
  guild_routing.py     THE resolver: need(interaction, reply=True) → (reg, interaction) — every command rebinds both (and sync_name: a Member's
                       changed nick is the ONE trigger of Registry.rename; verbs never rename); an unconfigured server is told
                       "isn't set up" and never reaches another guild; DMs: only() → else guilds_of (owner / membership yes|no|unknown) → one, none (Join links of LISTED guilds only, B10),
                       several → the DM server picker (guild_picker.choose_guild: asked EVERY time, asker only, 2 min; the command continues on
                       the press); resolve_button(interaction, g, legacy) for persistent buttons; may_access = the strict "belongs to" (the web uses it).
                       THE identity predicate (design §2.6/§5.33): resolve_role(bot, reg, who, max_age=) → Resolved(role owner|officer|member|outside,
                       role_ids, stale); role_of / identity are its names (bot.role_of, bot.identity); windows PRIV_LIVE=0 (writes) / PRIV_ROUTING=10 /
                       PRIV_WEB=60; role_sync = the no-lookup form. is_officer, is_officer_anywhere, plain_identity, asker_identity and app.py privilege
                       are aliases — never a sixth check
  discord_people.py    THE people verbs (design §5.41), module functions taking the client (bare test clients work) + PeopleMixin:
                       save_characters (the character table + its ripple: rc.character_changes/effects/preview/after_character_change → drop_seated
                       for a freed seat, a run-update + warn line for a kept role flip; preview=), record_absence, im_back (THE "I'm back"),
                       transfer_owner (D50; Discord only — /gm config owner, BY_DESIGN_ONLY), absence_lines / absence_clear_lines (the preview words:
                       one sentence per EVENT, named once — lineup words up to LINEUP_NAME_WORDS, else 'the <first raid> event', the time when two
                       read alike —; past three sentences summed into counts, a released seat always said);
                       a member's own retire / delete — and their own absence (record_absence → announce_absence → RaidMixin.after_absence(by_id=)) — frees the seat with no self-notice (by_id decides). No DMs anywhere
```
