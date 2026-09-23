# News review and apply jobs (Mac mini)

Two small jobs run next to the bot, each as its own launchd agent, plus a menu-bar item that shows what they are
doing. Design: `docs/design.md` §5.25. Officer side: `docs/manual.md` §7a.

## What happens, end to end
1. **The bot keeps the news.** A Wowhead webhook posts in the guild's news channel (set it on the Config page or with
   `/gm config news-channel`). The bot reads only the post (title and summary, never the linked article) and keeps
   the ones about our game version, our raids or the extra *news keywords* in `<data>/<guild>/news.jsonl`.
2. **The review (daily, 9:00 AM).** `review.py` asks the bot for the news kept since the last good run. Nothing new →
   it stops there: **no Claude session, no cost**. Otherwise it starts `claude -p` with `news_review.md`, which can
   only read (the news, the effective profile, the guild's settings, the repo) and post proposals. Each proposal
   becomes a card in the ops channel with **Approve** / **Dismiss**.
3. **An officer approves.** That only records who and when.
4. **The apply poller (every 15 minutes).** `apply.py` makes one API call. Nothing approved → done. Otherwise, oldest first:
   - a **guild setting** is applied by the bot itself (the same code as the Raids / Auras pages), no restart;
   - a **game data** change: Claude edits the YAML under `profiles/` and nothing else; then the script runs
     `check_commands.py`, `pytest -q` and `check_bundle.py`, commits to main, pushes, restarts the bot and waits up to
     90 s for it to come back. Tests fail → the edit is undone and the proposal marked *failed*. Bot not healthy →
     the commit is reverted, the bot restarted, the proposal marked *reverted*;
   - **needs a developer** → refused.
   It refuses to touch a working tree with uncommitted changes, or a checkout that isn't on `main`: the proposal
   waits and the status says why. Set `OIBOT_APPLY_MODE=pr` in the apply plist (or run `apply.py --pr`) to get a
   branch and a pull request instead of a commit to main (no restart).

Claude runs with the project MCP server only (`.mcp.json`, token from `.env`), `--permission-mode dontAsk` and an
explicit allow list; anything else — other MCP writes, web access, Write — is refused, never prompted. See the exact
command lines with `--dry-run` (below). It uses the Claude Code login of the user the agents run as.

## Install / uninstall
```
sh scripts/agents/install.sh               # review + apply + menu bar (or name some: review apply menubar)
sh scripts/agents/uninstall.sh             # all, or name some; status files and transcripts stay in out/agents
```
The plists live in `scripts/launchd/` next to the bot's (`gg.earlyandoften.oibot.news-review`, `.apply`, `.menubar`).
After editing one, run install.sh again. The review time is in its plist (`StartCalendarInterval`, 9:00) and in
`src/oibot_gm/agents.py` (`JOBS`, what the Agents page shows) — change both.

Before the first run: the bot is running with the web API on (`OIBOT_WEB_BIND`), `OIBOT_MCP_TOKEN` is in `.env`, the
news channel is set, and `claude` works for this user (`claude -p "hi"`).

## Reading the monitor
- **Agents page** (site → Agents, officers): a dot per job — green idle, amber running or not installed, red last run
  failed — with the schedule, last run and its result, next run and whether launchd has it. The **Run** card follows
  the current run step by step (every 3 s while one runs): lines starting `›` are the script's own steps, `▶` Claude
  starting, `✓` / `✕` a tool call that worked / failed (click to see its input and result), `■` Claude's finish with
  turns and cost. **Past runs** opens any of the last 30 runs per job. **Run review now** (owner) starts the review
  at once through launchd.
- **Menu bar** (`GM …`): `✓` all idle · `⟳` a job is running · `⚠` a job's last run failed · `✕` the bot is down. The
  menu lists the bot, the review's last/next run, approvals waiting and the apply job, plus *Open Agents page*,
  *Run review now*, *Open log folder*. It reads local files and launchctl, so it works when the bot is down.
- **Files** (`out/agents/`): `review.json` / `apply.json` (current status), `runs/<job>-<time>.jsonl` (full
  transcripts), `review.log` / `apply.log` / `menubar.log` (anything the processes printed).

## Running by hand
```
uv run python scripts/agents/review.py --dry-run     # the claude command it would run, and the prompt's tail
uv run python scripts/agents/review.py               # a review now, in this terminal
uv run python scripts/agents/apply.py --dry-run
launchctl kickstart gui/$(id -u)/gg.earlyandoften.oibot.news-review   # what "Run review now" does
```

## Stopping it
- Pause everything: `sh scripts/agents/uninstall.sh` (the bot keeps collecting news; nothing is reviewed or applied).
- Stop only the changes: `sh scripts/agents/uninstall.sh apply` — proposals still arrive, approvals wait.
- Stop a run in progress: `launchctl kill TERM gui/$(id -u)/gg.earlyandoften.oibot.apply` (or `.news-review`). An
  interrupted apply may leave an edit in `profiles/`: check `git status` and `git checkout -- profiles` before the
  next poll (the job refuses to run on a dirty tree anyway).
- Undo an applied game data change: `git revert <commit>` (the commit is on the card), then restart the bot.
