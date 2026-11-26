# Layout — the web app: auth and guild selection, the JSON API, the ops and setup routes, the guide tool, the scratch server and scripts

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
scripts/raidhelper_roster.py  a Raid-Helper event export (JSON) → `<guild>/test_roster.yaml` rows ({name, character, cls, spec, rank}) for the
                       PRIVATE data repo: the test bench's seed then rehearses against the guild's real signup shape (§5.42); `--members` → the import rows
                       {discord_id, name, character, cls, spec} for Registry.import_members (POST /api/members/import, MCP import_members — §5.53: the
                       real roster from a signup list; skipped rows named, preview writes nothing, a rank needs the actor); specs → classes, never names here
scripts/answer_probe.py  the live probe of the answer lookups (design §5.68): the REAL model answers tests/answer_bench.BENCH through
                       answer_loop.answer on a scratch copy of fixtures/demo per case (bench_registry; never the data repo, nothing under out/:
                       a work folder under the system temp directory); judges each final answer on BenchCase.answer (each entry a fact or a
                       tuple of readings — unmet / readings; `answered_instead` lets an asks_back case pass by naming every match), `never` and
                       the ending; lookups reported, not judged; --model / --effort / --compare / --baseline (help.answer beside it) / --only /
                       --repeat / --budget (3 USD, its own usage file — never the bot's monthly cap) / --pass-mark 0.8; exit 0 pass, 1 under the
                       mark or a never hit, 2 nothing run, 3 its budget reached; results.json per run
scripts/serve_scratch.py  THIS worktree's site on a throw-away copy of fixtures/demo for screenshots (§5.67 phase 4; never the live bot):
                       a git-inited data repo under the temp directory (GitStore push=False), the demo characters imported, `--seed` = a two-raid open
                       one-off, a locked and a finished one, a schedule with regulars, a minimum; the bot = tests/test_raid_night.NightBot (Discord
                       recorded, never called), uvicorn on 127.0.0.1 only, the login = the test suite's bearer. Refuses with DISCORD_TOKEN set, on
                       LIVE_PORT, or on a folder outside the temp directory or holding anything it didn't write (MARKER: it deletes only what it
                       built); .env loading off and every secret out of the environment before AND after the imports; every module-level output path
                       in the scratch folder; the suite's refused network reads refused. scripts/shots.py <base> refuses without a URL
  web/app.py           FastAPI served inside the bot (OIBOT_WEB_BIND): Discord OAuth2, landing page, icon/emblem/card images, old URLs → /app redirects;
                       published by a Cloudflare Tunnel (~/.cloudflared/config.yml → gm.earlyandoften.gg). Several guilds: `selected_reg` picks ONE guild per
                       request — `X-Oibot-Guild` header / `?g=<key>` (explicit: honoured or 403 not_member, never switched) > `oibot_guild` cookie > first
                       guild the person may open (guild_routing.member_of); homepage/not-member page = one card per LISTED server (`listed(bot)`:
                       GuildConfig.public — an unlisted guild is never named on the homepage, the not-a-member page, /api/public or /schedule);
                       no invite → INVITES_CLOSED; GET /schedule[?g=<key>] = the PUBLIC raid schedule (no login: raid name + size, next three runs per
                       schedule in Registry.local12 words, nothing about members; schedule_sections, now schedule_view's, re-exported here) — design §5.49
  web/api_ops.py       install_ops_api (called from api.py): POST /api/ops/change — `text` (officers, Claude) or typed `changes`/`ops` (tiered: a Save applies
                       unless Described.review, then needs_review + token; `confirmed`; `revs` config:<section> → 409; BOOT-prefixed tokens: expired → silent
                       re-preview when the fingerprint matches, restarted → shown again; §5.67 phase 4: review = Described.review OR asks_first — a
                       schedule's or raid's Save always previews its box first —, and a held structured token is DESCRIBED AGAIN at the press: other
                       words → needs_review + the fresh box + a new token, nothing written); the admin setup routes (/api/admin/config|aura|family|aura/reset|raid|
                       raid/reset|raid/schedule|raid/schedule/remove|recruitment|plain-permissions, + api_setup's channels via app.state.ops_translate) are TRANSLATORS
                       into changes through apply_ops under the SAME tier (D51): a warn-class / consequence batch needs `confirmed` + the section revs it touches,
                       else it answers `review_required` with the Described preview and writes nothing; no page posts to them (tests/test_frontend_source.py);
                       the text branch with the answer tools on (design §5.68): a question verdict, or a bare change (no ops, no questions,
                       nothing refused), → `reply` = help_answer(may_act=False, link=False, entry="site"), `questions` [] (ASK_GUIDE outside
                       the ask audience; /api/ask shares the constant)
  web/api_setup.py     install_setup_api (called from api.py; design §5.46): GET/POST /api/admin/channels (rows with `missing`/`problem`, never while Discord is
                       unreachable; POST = a translator through app.state.ops_translate), POST /api/admin/news-webhook (owner; the URL never in an ops line),
                       GET /api/admin/setup (checklist, invite, error alerts + the owner's choices and the settings rev; Host card for a superuser / the
                       bearer), GET /api/admin/left + POST
                       /api/admin/member/remove (officers' Remove record); discord_ready(bot, reg) → /api/meta.discord. No /api/admin/invite: the invite
                       is created AT the recruitment Save (invite_create). A stale membership (§2.9): web/app.viewer drops v.officer for any write
                       (v.stale_write → api.who 503 STALE_OFFICER). Companion tokens (§5.50, owner): GET /api/companions (rows, never a value),
                       POST /api/companions/issue (the value in that body ONCE, no-store; refused for MCP), POST /api/companions/revoke.
                       The server's guild (armory link §5.1): GET /api/admin/armory (officers read) + POST (owner: {name, preview} → found + sentences;
                       {name, guild_id, rev} → linked (no guild_id → 400: the previewed guild only), another id now → 409; {clear, preview?, rev?}; MCP writes need the owner as actor; 404 where
                       armory.available is false — /api/meta.armory is that flag); one ops.audited line per write naming the guild
  guides/              the leveling-guide tool (design §5.55): rxp_decode (decode / encode an RXPGuides import string: RC4 keyed from the BattleTag +
                       zlib + Adler-32; GuideError = the refusal in words), rxp_md (Markdown per guide group), rxp_json (GuideGame from
                       GameProfile.guides; dataset / summaries), explorer.html (the vendored one-file route explorer, data inlined in #guides-data),
                       build.py (build → out/guides/<guild>/<uid>/: explorer.html · RXPGuides_LocalGuides.zip · guide-text.zip · guides.json ·
                       status.json, swapped in whole; FILES = the downloads; OIBOT_RXP_ADDON = the host's addon zip for the free starter guides;
                       the inputs are never written or logged; lock_for = one build per member at a time); reissue.py = THE re-issue to the
                       member's own other account (reissue(text, tag, new_tag) → the new string, nothing written; convert_request = the DM's
                       `convert guide from <Tag1> to <Tag2> [given …]` read in code; CHANNEL_REFUSAL); library.py (§5.63) = the guild guide library:
                       <guild>/guides/index.yaml + one unlocked text per guide, a VERSION per faction (alliance|horde|both, id <faction>-<UTC stamp>),
                       latest = newest not withdrawn; upload (any member, unlocked once by their tag, UPLOADS_PER_DAY 5, STORED_MAX 12 MB per upload,
                       LIBRARY_MAX 200 MB in all — no delete) / issue (locked to the requester's tag in the upload's section `layout`, a withdrawn one officers only) / withdraw (a `guide` notice to the uploader) / restore, one commit each under a
                       per-guild lock, decode/encode under reissue.SLOTS; rows / the *_consequence sentences; NO BattleTag or string is ever written (D7)
  web/api_guides.py    install_guides_api (called from api.py): GET/POST /api/me/guide (status / build in a thread; MCP never builds), /clear,
                       POST /reissue (the other-account string as a no-store download; MCP never; the DM twin is discord_bot.guide_convert,
                       run first in on_message so the string reaches no model, memory or log),
                       GET /api/me/guide/explorer (inline, its own CSP sandbox, no-store), /file/{name} (the three downloads) — the folder is
                       always the session's own uid; /api/meta.guides = the version has the tool. The library (§5.63): GET /api/guides/library,
                       POST …/upload and …/{id}/download (members, never MCP), …/{id}/withdraw|restore (officers live, `preview` → sentences, MCP actor)
  web/api.py           JSON API (/api/*) for the React app + SPA mount at /app (history fallback to index.html). POSTs need `X-Requested-With: oibot` (CSRF).
                       Mutations reuse Registry/configops exactly like the Discord commands. Every route reads `v.reg` (the selected guild); `POST /api/guild`
                       sets the selection; SSE is per guild (`publish(topic, guild)`; None = every page); Discord-side site URLs carry `?g=<key>`.
                       Phase 3 (§5.44): POST /api/ask (TellTheBot's question half over HelpMixin.help_answer: officer view for officers, member view
                       otherwise; an officer's request to act → {act: true}), GET /api/config/summary, /api/readiness + minimums/minimum/rev/slots,
                       /api/meta timezones (grouped, picked never typed) + about_max + cadence_presets, schedule_json next_if_active + the server's sentences
                       Listening (§5.59): GET /api/plain-permissions adds listen / listen_modes / listen_words / listen_ready / listen_recent (listen_json:
                       `when` in local12 words, channel_name); POST /api/admin/plain-permissions (api_ops) takes listen / set_listen into the same plain change
                       §5.67 phase 3: schedule_json carries `raids` (every raid of its lineup), `lineup`, `rev` (config_rev schedule:<id>), no is_default;
                       /api/raids `reset_rev` (raid_all:<rid>); POST /api/admin/raid/schedule {id, fields} / …/remove {id}; ev_json `part`, `event`,
                       `occurrence`, `lineup` (rc.run_lineup), `start_local` (the picker's machine value), `cancel_tells_channel`; POST
                       /api/run/{key}/reschedule {start?, timing?, preview?, rev?} and /api/run/{key}/lineup {lineup, preview?, rev?} (officers, the verb
                       decides live; preview → {sentences, rev}; a refusal 400, a stale rev 409 with the fresh rev); /api/raid/{rid}/open (+ /preview)
                       take `lineup` (lineup_arg) and carry `occurrence` back (a different one → 409 + the fresh preview); the open route's and the
                       test bench's messages name the run (raid_views.run_title), never its key
                       §5.67 phase 4: /api/meta `page_words` (PAGE_WORDS), `confirm_words` (CONFIRM_WORDS), `leave` (LEAVE_TEMPLATE); box_refused /
                       box_stale = THE `preview` / `seen` handling of /api/run/{key}/lock | cancel | reschedule | lineup | layout (Undo my moves:
                       undo_box_for, RaidMixin.undo_moves) | strategy (strategy_box) and /api/raid/{rid}/open (box 11 = consequences.open_box_for, as /raid open; OPEN_UNREAD); POST
                       /api/admin/raid/regulars takes preview / seen on a removal (box 18); /api/run/{key}/move `shown` (+ by_id); /api/run/{key}/set
                       `spec` and its line through rc.site_words; /api/raid/{rid}/open/preview adds event_view.new_event_view when a lineup is sent,
                       open takes one_off_args (timing.lock_hours_before ∈ NEW_EVENT_LOCKS, rosters 1–4); /api/raids `page` (raid_page.raid_row),
                       /api/readiness `view` (raid_page.readiness_view); POST /api/run/{key}/autofill = Auto-fill groups (event_board.AutoFill; answers
                       at once with autofill_working, `wait: true` — MCP — answers when done); event_board.AUTOFILL_LISTENERS → autofill_heard →
                       publish('run:<raid key>');
                       install_events_api / install_schedules_api; the final fix wave: /api/run/{key}/remind (remind_box), /api/loot/{run}/end
                       (api_loot: end_box) and /api/run/{key}/move on a locked board (board_move_box) take preview → {box} and seen → 409 with the
                       fresh box (move: + the board) / 400 refusal box; EVERY route that returns a raid verb's line answers in rc.site_words;
                       /strategy refused after the lock (consequences.strategy_refusal); api_ops' token apply re-describes its box under
                       config_lock in the write's step (apply_ops(expect=)): changed → a fresh box, nothing written
```
