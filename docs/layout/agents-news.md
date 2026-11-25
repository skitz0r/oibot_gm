# Layout — news review, proposals, the local jobs

One entry per module: what it is THE place for, the names other code relies on, and the decisions behind them.
Part of the agent guide (`CLAUDE.md` › Layout). Keep an entry true in the same commit that changes its module.

```
  news.py              news review (design §5.26): keyword filter of #news webhook embeds (profiles/<v>/news.yaml version_terms + raid names +
                       guild news_keywords) → <guild>/news.jsonl (dedupe by url); Proposal/ProposalStore (<guild>/proposals/<id>.yaml,
                       proposed → approved|dismissed → applying → applied|failed|reverted; officers decide — a profile approval is the OWNER's —,
                       only the job (bearer + X-Oibot-Job) reports job steps; AlreadyDecided/already_text; annotate = the job's "waiting: …";
                       consequence(p, reg, state, guilds, busy=, next_run=); guild_busy = D33's raid guard (every live loot council counts on its
                       own, past close too); dismissed_cover refuses a profile proposal citing only dismissed news; RECOVER_MOVE/INTERRUPTED
                       = the owner's Mark as interrupted (move(recover=True)); MCP_ACTOR_NEEDED (D8: no bare-bearer decision); PROPOSAL_LISTENERS → `agents`)
  discord_news.py      NewsMixin: news_message (the ONE hook at the top of on_message; webhooks are bots), news_backfill, proposal_card_update
                       (ops-channel card edited in place, returns whether Discord took it); decide_proposal = THE proposal verb (move | annotate →
                       card → audit → shared-profile fan-out); guild_busy / busy_map / proposal_consequence; proposal_cards_restore (ready, resume);
                       agents_status_lines / governance_status = /gm status's News review + Apply lines;
                       ProposalButton (DynamicItem; opens flows_news.proposal_decide, never writes)
  flows_news.py        proposal_decide: the decide screen for Approve / Dismiss / Withdraw approval (news.consequence; owner-only game data)
  agents.py            local jobs on the Mac mini: out/agents/<job>.json status + runs/<job>-<stamp>.jsonl transcripts (last 30), stream-json → steps,
                       launchctl print/kickstart, claude command lines (allow/deny lists, dontAsk; never a permission bypass)
scripts/agents/        loop EVERY guild (Api().guilds()): review.py (daily 9:00 AM; one tagged run per guild, its own since, a Claude session only for a
                       guild with new news, pinned with OIBOT_MCP_GUILD), apply.py (every 15 min; X-Oibot-Job: apply; recovery of `applying`; every guild
                       setting this poll, then the first profile edit whose preflight + D33 guard pass: resolve("applying") gate, Claude, checks/commit/push,
                       re-read + guard again before the restart (restart_pending otherwise — owed to the game VERSION, kept when its guild is no longer listed;
                       `auto: False` = the proposal moved meanwhile: restarted at the next quiet poll / the owner's Apply now without moving it), health/revert — by the
                       SCRIPT; the gate refuses a profile approval that wasn't the owner's (news.profile_approval_refusal → failed); --pr), menubar.py (rumps via uv --with),
                       install/uninstall.sh; plists in scripts/launchd/
  web/api_agents.py    /api/news, /api/proposals… (resolve via decide_proposal; job_of = bearer + X-Oibot-Job; a bare bearer never approves or
                       dismisses (D8: X-Oibot-Actor); /interrupted = the owner's recovery; `consequence` per proposal), /api/agents
                       (the monitor + apply_mode, elsewhere/install per job, owner's mcp card and llm usage), /api/agents/quiet (quiet_state),
                       busy_json (GET /api/guilds `busy`), /api/agents/profile (effective profile, overrides marked)
  web/api_policy.py    the Policy page's routes (GET /api/policy, /api/policy/{doc} PUT · append · compile · confirm · discard) and GET /api/manual (Help)
```
