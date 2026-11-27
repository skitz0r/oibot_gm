# Layout — the Discord client, the wizard core and every flows_* file, the kept-current channels, the test bench, help, policy

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  discord_bot.py       OibotGM client: the gateway events and on_message routing (an addressed message in a council thread → loot_chat);
                       mention_names(message, reg, bot_user) = THE text of every plain-text path (@mention, DM, listening): the bot's own
                       mention/role go, another user's <@id> → their registered name (else Discord's, else "@someone"), <@&id> → "@role name", <#id> kept;
                       on_guild_join (re-attach a parked guild, else ONE DM to host_ids: "I was added to X … Missing permissions: …"), on_guild_remove
                       (Guilds.detach unless guild.unavailable; one ops error), on_guild_available (re-attach), on_member_remove/join (Member.left_at);
                       claim_guild adds an ops line to every other guild (D44: no owner DM); cached_member keeps _member_last_yes (the web's 6 h "stale" read);
                       list_members / refresh_members(guild, max_age=MEMBERS_LIST_AGE_S 5 min, wait=) = ONE REST list of the server that feeds cached_member for
                       everyone (§5.58 performance: GET /api/members and the hourly sweep_left never fetch per member; wait=False reads the old snapshot while
                       a new one is taken; MEMBERSHIP_LISTENERS fire on a changed roster → the web's `member` topic);
                       bot.events[(gk, session id)]; GuildContext = the start directory's key, profile and the LLM provider (rehearse loot with a
                       test run — its council is session.test, D11)
                       ListenMixin (design §5.59): listen_message = the gate run LAST in on_message for an untagged message in a listened channel
                       (PlainPolicy.listen) → decisions.noul on listen_state (LISTEN_BOT_WORDS + raids + glossary + 2 recent + the message, data only)
                       with LISTEN_QUESTION; ≥ LISTEN_SURE (0.8): reply → plain_text as an @mention, watch → recorded; ≥ LISTEN_MAYBE (0.5): reply →
                       👀 and the AUTHOR's 👀 click (on_raw_reaction_add, LISTEN_ASK_S 1 hour) → plain_text; an overheard message that
                       parses to a CHANGE is never answered with a card (the owner, 2026-11-21): plain_text(hold=) → handle_change(hold=) →
                       _listen_hold = 👀 only + the parsed request kept, the author's click → handle_change(req=) shows the diff, no click →
                       the 👀 comes off, nothing posted; an overheard question is answered as before; listen_followup (Task 11) = run in
                       on_message BEFORE the gate for an untagged message: when the bot's last reply to that person in that channel asked
                       something (conversation.take_open_question, LISTEN_FOLLOWUP_S 5 min, taken once — an @mention takes it too), the
                       message goes back to WHOEVER ASKED (Exchange.asked_by: conversation.PARSER = handle_change's question, LOOP = the
                       answer loop's choices, discord_help.Asked): the parser's → plain_text(to_parser=True) → handle_change with the
                       conversation, skipping the help-first step (only where an @mention could reach the parser: not ignore, in the
                       server, may act there; then authorize, describe(strict), the live check at Apply); the loop's → plain_text as an
                       @mention (no decision request, no 👀, no hold; entry "listen" in a listened channel). A message that mentions another
                       user / a role / everyone, replies to a message that isn't the bot's, or has no words only CLOSES the question
                       (then the gate judges it as any other); an ADDRESSED message (an @mention, a DM, a Discord reply to the bot's
                       message) routes the same way: answers_parser(reg, message, channel) → conversation.take_answered (a reply → the
                       exchange of the message it replies to, at any age; else the open question within LISTEN_FOLLOWUP_S) — a parser
                       question → plain_text(to_parser=True) → handle_change(to_help=True: the parser's kind=question gets the full help
                       answer; its prompt defines kind=ignore for chat → silence), a loop question or none → today's path; the untagged
                       follow-up is bounded (Exchange.depth, conversation.FOLLOW_DEPTH 1: a question asked again of an answer is
                       followable once, a third in a row is closed); never in a watch or ignore channel; a listened channel logs did "followup" (p null); LISTEN_PER_HOUR cap; listen_recent(reg) = the ring of
                       50 verdicts (memory, text ≤120) and out/listen.jsonl rows WITHOUT text; no key → one ops warn per guild per hour. Never a DM;
                       LISTEN_LISTENERS fn(guild key) on every verdict → the web's officers-only `listen` topic (the Ops page's Recent decisions)
  policy.py            policy docs (<guild>/policy/*.md) + Claude compile → *.compiled.json (compiled_at, text_sha, confirmed_by), confirmed by an officer;
                       the waiting edit ON DISK (<doc>.pending.json, one commit with the draft; PENDING = a rev cache only); policy_state = Written vs
                       Applied (differs) + the pending edit; persona two-step (PersonaReading, live_text); append_rule = THE policy_append verb
                       and appended/rule_bullet THE text it writes (every surface; refused while another officer's draft waits);
                       POLICY_LISTENERS on every committed write → the web's officers-only `policy` topic — design §5.45
  discord_pool.py      dedicated channels the bot keeps current: registration card (public, read-only); analytics channel with one native
                       card per raid (pool_layout: readiness + comp shortfalls, edited in place, no images) + change log (Registry.listeners, debounced);
                       absences channel (AbsencesMixin: card with I'll be away · I'm back · My absences → AbsenceButton `abs.<gk>:new|back|mine` and a Clear
                       `abs.<gk>:clear:<uid>:<start>`; announce_absence → sheets updated; puppets never on the card or the channel; AbsencesMixin inherits
                       discord_people.PeopleMixin);
                       SetupMixin: set_channel(…, rev=) = THE channel write (perms.missing_for refusal, "Discord is unreachable" apart, the settings rev
                       checked before posting and at the save → ConfigChanged, the card posted FIRST then the old dropped, ONE ops.audited line — callers
                       write none; the eight shortcuts AND /gm config loot-channel open flows_config.ChannelSetWizard), channel_consequences (+ configops CONSEQUENCE_HOOKS entry), CHANNEL_LABELS,
                       news_webhook (D39), role_refusal, invite_status, discord_links, channel_problems / check_channel_perms (an error for ops/signup,
                       else a warn) / setup_tick (hourly, from raid_scheduler.scheduler_tick), note_membership + sweep_left (Member.left_at from Discord's
                       own answers — the bot has no members intent, so on_member_remove never arrives; the sweep is one refresh_members list per hour, the
                       per-member fetches only its fallback), setup_checklist (the Setup card / /gm status), guild_channels/roles, test_bench_clear,
                       plain_table (the plain-text policy in words; plain_read_text / plain_why render /gm config plain).
                       Companion tokens (§5.50): issue_companion(reg, label, by, by_id, via, deliver_to=) = THE issue (deliver_to → the token DMed ONCE
                       via companion_dm_text — a credential the raid leader must install, an act under the DM rule — and never returned; a refused DM
                       withdraws it; else the value in the result for the site to show once), revoke_companion (sockets closed now), companion_rows
                       LootLogMixin: the public loot log (LEDGER_LISTENERS → one line per award in the loot channel, else the event's updates thread (rc.updates_thread_of); never
                       the signup channel; loot_log off = nothing) with LootWishButton (`lootwish.<gk>:<item key>` → /me wishlist on that item)
                       Test bench (design §5.42): Member.test puppets; SetupMixin.test_seed/test_compose/test_comp/test_answer/puppet_asks/test_bench_clear =
                       THE verbs of /gm test seed|run|answer|compose|comp|clear, /api/test/*, plain text and MCP test_bench; a test run is keyed like
                       any run (rc.new_run_key) and found by rc.live_at(test=True), never by its key — a rehearsal never meets a real run, and open_run refuses
                       another tester's at that minute (a rehearsal is its test_by's); one raid only, cards in the asking channel (roster test_channel),
                       puppet DMs → RaidMixin.puppet_dm: the feed + ONE digest per batch to the tester (TestAnswerButton); may_answer_for lets the tester
                       press their buttons, a test run's pool is puppets + tester only and its sheet refuses presses from real members
  testfeed.py          the test feed (D20): TestFeed ring per (guild, run) of card | dm | step | answer events, in memory only (never the data repo);
                       observe() on RUN_LISTENERS saves of a test run, note() from post_run_update / puppet_dm; LISTENERS → publish("test:<run>");
                       plain() = Discord text as site words; tempo_of = a TEMPOS name or minutes → the four marks
  help.py              the bot explains itself: docs/manual.md + live command tree + guild settings + the asker's record → Claude (route `help`);
                       the manual is PROGRESSIVELY read (2026-11-19: the whole manual in every prompt was ~108K tokens, $0.28 an answer — the
                       month's help share gone in 11 answers): manual_parts = each section's whole blocks grouped to PART_CHARS (ids '4.3'),
                       manual_index = the contents list in the system prompt (id · the bold lead-ins), HelpAnswer.how_to / .read → answer()'s
                       second and last request with manual_read(ids, question=) = the parts named + manual_match's (the question's words,
                       rarest first; code) — a data question is ONE request, a how-to two; never put the whole manual back in a prompt;
                       help_answer returns BudgetExceeded's own sentence (never "Something went wrong");
                       the answer's shape (the owner, 2026-11-18; SYSTEM): the fact first, never where it comes from (armory / by hand are reasoning
                       notes), nothing about what is NOT there; HelpAnswer.page (a PAGES key) → page_link = '-# See it all on the [Professions page](<url>).'
                       (officers' pages for an officer only; off for the site's box — help_answer(link=False)); `commands` only when the asker must DO
                       something, never a slash command for a read; `officer_view` = the public-channel footer only when officer data was asked for;
                       page_words(reg, key) = a page's name as a link inside text code writes ("…and N more — the [Professions page](<url>) has them all.");
                       small print (the review of 2026-11-18): no standing how-to under a reply or card, nothing about what does NOT happen ("no DM",
                       "nothing to do"), no slash command for a read where a page shows it; empty fields and mechanics notes in the snapshot are never volunteered;
                       officers' `config` section = configops.summary_text(config_summary(audience)) (owner: Host too);
                       `prices` section (every member, §5.65: prices.help_section, gated by prices.help_hit alone — code computes, the model words it);
                       `who` section (every member, any channel: who plays what + rank, named members first, no officer marks; an officer
                       in private gets `bank` instead — owner decision 2026-10-29, design §5.25);
                       `professions` section (every member, any channel: each profession's crafters with skill, then every registered recipe with its
                       makers, then with the game's list up to HELP_HITS recipes the question names; gated by professions.help_hit — a profession named
                       as a word, a registered recipe's or a listed recipe's whole words — design §5.60, §5.64);
                       armory_lines(reg, m) = the ASKER's own characters' armory link in the snapshot (linked or not, updated_words, in the guild when
                       known, professions followed or kept by hand; one <member-text source="armory"> block; where armory.available; never another member's — §5.64);
                       `guides` section (every member, gated on "guide" as a word: each faction's latest guild guide, uploader unfenced — §5.63);
                       manual_sections (the manual by its own ## headings, mtime-cached, tiers tagged — the Help page), command_rows(tree, tier),
                       site_url(reg, path, anchor) = THE site link (?g= before the #anchor; & when the path has a query); PAGES names the officers'
                       events / schedules / raids pages (no rosters / readiness), PAGE_ALIASES rosters → events, readiness → raids;
                       shared with the answer lookups (design §5.68, behaviour-preserving extractions): who_line(reg, m) (the `who` section's
                       line, pinned), role_of(reg, c, spec=), who_chars(m), who_char_words(reg, c); the public names safe_text / whole_words /
                       clip_lines / council_loot = _safe / _words / _clip / _loot (the same functions); MEMORY_LINE stays the old path's words
  discord_help.py      /help (no LLM, by tier), /ask; help_answer(may_act) flags requests to act (routing is discord_bot.plain_text:
                       @mentions and DMs routed by intent under the plain-text policy). help_answer(…, entry=) (design §5.68): with
                       answer_loop.enabled(reg.key) and a provider that converses, answer_loop.answer on Viewer.of(…, may_act=) instead of
                       help.answer — the provider-None and ask-audience returns first, the same post-processing after; log_answer per answer;
                       an `error` ending warns ops as today; acting off and the reply still acts → its words alone (ACTS_OFF_EMPTY when none).
                       /ask (on): conversation.recall for the place, remembered only where ask_private (a DM or reply_private)
  wizard.py            the wizard core: one ephemeral message advancing in place (Wizard.show/working/finish/refuse), Forms; Wizard.decide = THE confirm screen (consequence · <verb> · <verb> with a note · Back; do_label = the verb, required; guard re-run on every press);
                       Wizard.decide_box(box, do=, refresh=, note=, extra=, back=, hint=) = the server's box (consequences.box) as given: box_text = [CONFIRM_WORDS changed] · **title** · diff lines · sentences (one heading, never the wizard's too); the verb (red iff danger) · '<verb> with a note' (a Form whose Submit is the write) · extra · Back (back=None → "Nothing was changed."); embed= under this screen only (the lock's preview roster; a box shown again goes without it); a refusal box (verb None) = Back alone, or `refused=` buttons before it (the lock's empty board keeps Lock and auto-fill); refresh() re-run at the press and the note's submit — another fp is shown again under CONFIRM_WORDS['changed'], nothing written; button_label = THE ellipsis drop (Wizard.button and the persistent RunButton / LootButton / CfgApplyButton apply it; the site's shared words keep theirs, D1); when_fields(step=) reaches minute_opts
                       (a select inside ui.Label, or a TextInput only for prose), owner-only presses + a guard re-checked on
                       every press, FLOWS registry (@flow), no-typing dates/times (day/hour/minute selects, 12-hour,
                       resolve_local = DST-safe), lint() = what the Discord API would refuse, checked offline (+ a button label ending in an ellipsis); opt-in class attributes
                       `heading = False` (a screen is its text alone, no bold title — /price) and `mentions` (AllowedMentions on every send/edit);
                       the defaults leave every other wizard unchanged
  wizard_opts.py       registry data as select options (raids, live runs, a member's characters, classes, specs, ranks)
  wizard_flows.py      imports every flows_*.py so FLOWS is complete; flows_absence (the reference flow + AbsenceClearWizard = I'm back) · flows_raid (OpenWizard(Wizard, LineupBlock) = /raid open: a schedule's night (Next,
                       another upcoming one, a one-off minute rc.which_run calls scheduled — then hint = event_view.SCHEDULED_NIGHT) =
                       decide_box(consequences.open_box_for(here=)) refreshed from rc.open_preview, its refusals (closed, already open, past,
                       OPEN_UNREAD) as boxes; a one-off = when_fields(step=15) → lineup_hub → Locks (event_view.lock_choices) + Rosters beside
                       What this makes (event_view.new_event_view: facts, notes, lineup) = its confirmation, Open the sheet · Change · Back →
                       open_run_and_post(lineup=, lock_hours=, first_rosters=); a pickup template keeps its own confirm; draft["here"] = asked
                       in a server channel (guild_routing.in_server_channel, open_test_run's too): with no signup channel every path says the sheet posts
                       here (consequences.no_channel_sentence) and opened() — THE outcome of every open — posts it there (POSTED_HERE); from
                       a DM it waits, as the site's box says; no horizon words; a raid with no night to open offers ADD_SCHEDULE "Add a
                       schedule" = flow `schedule` on that raid, taking the screen over) ·
                       flows_lineup (no @flow; imports no flows_* module, so any wizard mixes it in at class definition — flows_event
                       re-exports LineupBlock / REVIEW / STEP / START_CHOICES / EVERY_RAID_IN): LineupBlock, below ·
                       flows_event (phase 5, Task 3: pick_event = THE event picker — live FIRST raids, soonest first, ≤ 25, one goes straight in,
                       none = NO_EVENT —; EventWizard (ev() re-read, act = working → the verb's ✅ line or its refusal); flow raid_reschedule =
                       RescheduleWizard: reschedule_pick_box's text + Pick the day and time → when_fields(step=15) (Later than this… → the week)
                       → rc.move_start (MOVE_GAP / MOVE_TWICE = refusal_box_for, Back alone) → decide_box(reschedule_box) Move · Pick another
                       time · Back, the rev taken with each box; flow raid_edit = EditEventWizard: a hub of edit_options' fields as 'label: value'
                       (event_value_words, old → new once drafted; edit_locked / edit_over), Change a setting → one screen each (hours trimmed
                       to 25 keeping the value, on / off, First to join / By priority, the weights = one Form of five selects, a split select
                       per live raid), Review changes → decide_box(reschedule_box(edit=True)) Save → reschedule_event(timing=, rev=);
                       Task 4 (moved to flows_lineup.py): LineupBlock = THE lineup draft mixed into a Wizard (no event knowledge — Tasks 5 and 6 reuse it): self.lineup
                       [{raid, rosters[, starts]}], the host's lineup_start() (None = a schedule: 'N h M min after the first') and
                       lineup_done(i), and lineup_back(i) (the hub's Back; default the wizard's cancel — a host returns to its previous
                       screen); lineup_hub = a line per step + Pick a step + Add a raid (OwnerWizard.paged over 25; disabled at
                       LINEUP_MAX with LINEUP_MAX_WORDS, or with EVERY_RAID_IN when the game has no raid left) + Review changes · Back;
                       lineup_step(n) = Rosters 1–MAX_SCHEDULE_ROSTERS, for n ≥ 1 Starts (the lineup_offsets default 'When <raid> ends ·
                       9:15 PM' first, then 24 in 15-minute steps after the previous start, a start already set off that list added in
                       its place), Move earlier (n ≥ 2) / Move later / Remove — step 1 never changes (LINEUP_FIRST); a new start shifts
                       every later SET start by the same delta (they keep their gaps, as the site's LineupBuilder setStart); a move or a
                       removal clears the later starts back to their default; _tidy drops a start no longer after the one before;
                       flow raid_lineup = EditLineupWizard(LineupBlock, EventWizard) from rc.run_lineup, rc.lineup_fixed → its refusal box at
                       once, Review changes → decide_box(lineup_box, refresh) Save lineup → edit_lineup(rev=)) ·
                       flows_schedule (phase 5, Task 6, D4: flow `schedule` = ScheduleWizard(LineupBlock, OwnerWizard), officers re-checked per
                       press; /gm config raid › Schedules hands its screen over (message_id + a `back` that rebuilds the raid's hub with its
                       draft); the list = Active · Paused by registry.schedule_name + New schedule (kind → a name Form → the same hub, a
                       create-shaped draft seeded from timing_suggestion / the pickup's, its id new_id(name), never shown); one schedule's hub =
                       stored_entry(row) as the draft, a 'label: was → now' line per setting in PAGE_WORDS["schedule_fields"] (a pickup hides
                       what it never uses), its next event or paused_next; Change: Name (prose) · Repeats (run times = day + hour + 15-minute
                       Form, ✕ to drop; a lockout schedule's days + start) · Lineup (LineupBlock; lineup_done / lineup_back = the hub) · Sheet
                       timing (editor_options presets, or one setting's hours trimmed to 25) · the four switches (one select field=on|off) ·
                       Priority (a Form of five selects, "Same as the raid") · the split; Review changes / Pause / Resume / Remove = configops
                       ops_from_fields (an edit sends only what differs; a new pickup never its hidden settings) → box_of = describe_ops AND
                       config:schedule:<id> read together under config_lock in a thread behind working → decide_box(box 13 / 14 / 15 / 16 /
                       17, refresh = box_of again; a line's refusal = its refusal box, Back to the hub) → apply_ops(revs = the rev of the box
                       LAST SHOWN, expect = that box's fp re-described under the lock; stale_box → the box again) → the ✅ line or the
                       refusal's line; Pause / Resume / Remove / Back with unsaved edits ask box 20 first (Discard edits · Keep editing); the
                       list's text is paged with its select) ·
                       flows_config (+ OwnerTransferWizard; AuraWizard = a draft hub per buff, one Save this buff through configops.apply_ops; CompTargetsBlock = the
                       Minimums block (officers), in RaidConfigWizard (officers; no sheet timing; Schedules = the hand-off to flow `schedule`;
                       Review changes = raid_ops (ops_from_fields "raid" + a notes / first opens clear) → decide_box(box 19, refresh) → apply_ops
                       with the config:raids:<id> read with the box last shown + expect; Reset = decide_box(raid_reset_box, raid_all:<id>, built
                       in a thread); set_raid_overrides only previews) and in CompTargetsWizard = /roster comp; ChannelsWizard
                       (/gm config channels: ChannelSelect / — not set — → draft → Review → apply_ops), ChannelSetWizard (the eight shortcuts' one confirm),
                       OfficersWizard (/gm config officers: RoleSelect, Map <legacy name>…), CompanionsWizard (/gm config companion: the hub → Issue… label
                       modal → member picker → decide → the DM; Revoke… → decide), ArmoryGuildWizard (/gm config armory = flow armory_guild, §5.1: unlinked → the name
                       modal at once → armory.find_guild in a thread behind working → decide (consequences.armory_guild_consequence) → set_armory_guild; linked →
                       the link + Change… / Unlink… (armory_guild_clear_consequence); refused in one sentence where armory.available is false;
                       the auction house branch (§5.65, auctions.available): the hub's market line + Auction house… → working → house_choices in threads
                       → one select "Auction house N of M" (prices.choice_description) → decide (market_consequence) → set_market with the rev →
                       refresh_soon; Turn prices off… → decide (market_clear_consequence) → clear_market; owner only, one audit line each)) · flows_roster (MemberWizard/CharWizard: /me char, /roster char|rank|set-main; CharWizard mixes in flows_armory.CharArmory; SetWizard = /raid set: run → what applies (rc.member_actions) → character → for Join / Bench "Plays which spec?" (wizard_opts.specs_of, only the specs that character plays — one = no select; the spec they play now = no switch) → on an event of several raids, a Join answer's switch asks "For which raid?" (issue 15: raid_parts / raid_step, rc.SCOPE_LABEL; WHOLE_EVENT or a raid's key, only the raids where they play something else) — a raid = consequences.raid_switch_box and RaidMixin.set_raid_switch → decide_box(consequences.member_action_box, refresh = the same, a callout's note Field) — a refusal (rc.seated_switch_refusal: a spec of ANOTHER role on a seat of the locked roster = rc.SPEC_SEATED, a same-role spec passes; no_longer_applies) = Back alone; the box's fp replaces the old "keep going?" re-read; the write = inbox.do_member_action(spec=)) · flows_armory (armory link §5.4 "Discord.": CharArmory = the hub's Link to the armory… (box: first name + surname, prose → discord_armory.lookup_allowed (THE per-person limit the site's find / link use too) → find_for_link behind Wizard.working → decide with the verb's preview → link_character with the Found the screen showed) · Unlink… · Keep professions by hand / Follow the armory again (set_profession_sync) · Sync now (no confirm, working first) — hidden where armory.available is false or on a puppet — and officers' Move to another member… on EVERY version (R11: UserSelect → move_consequence → move_character); armory_line = the hub's 'Linked to the armory · Updated <t:R> · in the guild' (in_guild only when known); synced / synced_sentence = flows_professions' no-edit view; ArmoryOverview = /roster armory (officers, re-checked per press): counts from armory_seed.propose, empty states = discord_armory.NO_GUILD_YET / NOT_READ_YET (the site re-exports them), Link the sure matches → decide (armory_seed_consequence FIRST, then the rows with armory_seed.check's refusals; every pick refused → no link button, only Back) → armory_seed.apply → armory_seed.result_lines (batch_words "N characters are linked to the armory." + only the refused picks — the site's toast too; the not-claimed count = discord_armory.unclaimed, the site's, + sure_share_words; member text escape_markdown); bounded(lines, budget, noun) = THE whole-line cut with "and N more" (the batch lists, the character hub's lines); the count's rev compared at both presses (changed → the new counts, nothing linked); the rest is the site's Members page) · flows_register (incl. the Discord application: picks → names → schedules → the guild's
                       questions five per modal → review) · flows_recruit (/gm config recruitment) · flows_wishlist (/me wishlist) · flows_professions (§5.60: /me professions + /roster professions = one hub per character — Add profession… / Change skill… / Add recipe… (with the game's list, §5.64: part of the name → a select of that profession's recipes ≤ 25, value = the recipe id, category as the description, the character's own left out; none → NOT_A_RECIPE + Search again…) / Remove…, skill in 17 preset steps, every write on the decide screen with profession_consequence — and /professions who|list, ephemeral, no model) · flows_prices (§5.66: flow `price` = /price, heading off, no mentions: with an item price_answer's lines + Watch / Stop watching when it names one item while prices show — a press edits the answer in place, a refusal is its sentence —; with none Your watch list = a line per item, the status line once, a select → the item's line + history_sentences (in a thread) + Stop watching · Back; no confirm (C8), nothing survives a restart (C18)) · flows_guides (§5.63: /guide list · get = faction → version → BattleTag modal → the .txt DMed, a refused DM = the private reply · upload = the file read after the modal → decide · withdraw|restore, officers live) · flows_misc · flows_news (the proposal decide screen) · flows_loot · flows_test (/gm test compose|comp, the digest's TestAnswerButton). Commands and card buttons call FLOWS[name].
                       tests/wizard_harness.py drives a flow end to end with fake interactions and lints every screen
  discord_policy.py    /gm policy show|edit|reload, /gm rule loot|comp, /gm change, /gm config plain (the D37 READ view, registered here); handle_change for every
                       plain-text path (ConfigConfirmView re-authorizes the presser; a diff nobody presses in 10 min DELETES itself —
                       PendingView.expire, DELETE_ON_TIMEOUT; a policy reading nobody confirms in 15 min UNWINDS its edit as Discard would
                       (CompileConfirmView.on_timeout → policy.discard_pending, one ops line; only the edit it showed: same text, same
                       compiled_at) and is deleted too; words only where Discord won't delete: TIMED_OUT / READING_TIMED_OUT / READING_DROPPED /
                       EXPIRED_RESTART / BUTTON_GONE each say what happened and what is true now, never "Expired — say it again"; diff_text = the diff's body: setup ops as configops.describe_ops(…).as_discord(),
                       other ops their preview_line; refusals end with configops.plain_footer;
                       guild name when many_guilds); the question rule (design §5.68, switch on — answer_loop.enabled): the parser's verdict
                       question (with or without its own questions), or a change with no ops and no questions, goes to help_answer once with
                       may_act=False (entry act | change | the caller's), never the parser's words; a change missing a detail keeps its
                       questions; with `hold` (listening) any change verdict is held behind 👀 first; policy_show_messages
                       (≤3 text messages + one Compiled reading embed ≤15 rules); PolicyEditModal append-only past 4000
  pending_views.py     out/pending_views.jsonl: open wizards and plain-text diffs mirrored while they wait; restore_pending_views on ready edits the
                       last process's leftovers ("The bot restarted; nothing was changed"); live_count for /api/agents/quiet
```
