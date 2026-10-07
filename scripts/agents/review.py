"""The daily news review (design.md §5.26; launchd: gg.earlyandoften.oibot.news-review, daily at 9:00 AM).

For EACH guild the bot serves (design §5.32; GET /api/guilds, then a guild-scoped Api), one after another:
1. Asks the running bot to re-read that guild's #news channel (posts made while it was down), then for the news it
   kept since that guild's last good run. Nothing new → done, no Claude session.
2. Otherwise runs `claude -p` headless in the repo with the project MCP server, read tools plus post_proposal only
   (see agents.review_command), prompt = scripts/agents/news_review.md, with OIBOT_MCP_GUILD pinned to the guild in
   the child's environment. Every step goes to that guild's transcript (a run tagged with the guild).
3. Writes out/agents/review.json as it goes (the Agents page and the menu bar read it); `last_ok_started` is a map
   {guild key: ISO} (an older single value is every guild's `since` until it has its own).

    uv run python scripts/agents/review.py            # what launchd runs
    uv run python scripts/agents/review.py --dry-run  # print the claude command and the prompt head, run nothing
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oibot_gm import agents  # noqa: E402

JOB = "review"
PROMPT = Path(__file__).with_name("news_review.md")
FIRST_LOOKBACK_DAYS = 7  # the very first run reads a week back


LEGACY = "_legacy"  # last_ok_started from before runs were per guild: the `since` of every guild without its own entry


def since_map(st: dict) -> dict[str, str]:
    """last_ok_started as {guild key: ISO}; an old single value becomes the `_legacy` entry."""
    v = st.get("last_ok_started")
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items() if x}
    return {LEGACY: str(v)} if v else {}


def since_for(st: dict, key: str | None = None) -> str:
    """A guild's own last good start, else the one from before guilds were told apart (`""`: a bot without
    /api/guilds — mcp_server.Api.guilds — or the old single value), else a week back."""
    m = since_map(st)
    return (m.get(key) if key else None) or m.get("") or m.get(LEGACY) or agents.iso(datetime.now(timezone.utc) - timedelta(days=FIRST_LOOKBACK_DAYS))


def build(since: str, n_items: int, budget: float, model: str | None, all_mcp: list[str], guild: dict | None = None) -> tuple[list[str], str]:
    prompt = PROMPT.read_text() + f"\n\n## This run\n- since: {since} (pass it to list_news)\n- new items: {n_items}\n"
    if guild:
        prompt += f"- guild: {guild.get('name')} ({guild['key']})\n"
    return agents.review_command(all_mcp, budget, model), prompt


def review_guild(g: dict, st: dict, a, api_for, run_claude=None, named: bool = True) -> tuple[bool, str, int, int]:
    """One guild: its own run (tagged), backfill, its news since its own last good run, and a Claude session only when
    there is news — pinned to the guild (OIBOT_MCP_GUILD in the child's environment, so the MCP server it starts acts
    on this guild only and the model never picks one). Returns (ok, result line, items, proposals posted). `named`:
    the result line starts with the guild's name (several guilds)."""
    from oibot_gm.mcp_server import GUILD_ENV, LOCK_ENV, ToolError

    key, name = g["key"], g.get("name") or g["key"] or "the guild"  # key "": a bot from before several guilds (no header)
    since = a.since or since_for(st, key)
    rid, tr = agents.new_run(JOB, guild=key or None)
    agents.write_status(JOB, run_id=rid, step=f"{name}: checking the news")

    def done(ok: bool, result: str, n: int = 0, posted: int = 0):
        agents.runner_line(tr, result, "info" if ok else "error", done=True)
        return ok, (f"{name}: {result}" if named else result), n, posted

    api = api_for(key)
    try:
        agents.runner_line(tr, f"{name}: re-reading the news channel for posts made while the bot was down")
        back = api.post("/api/news/backfill")
        items = api.get("/api/news", since=since)["items"]
    except ToolError as e:
        return done(False, f"bot not reachable: {e}")
    agents.runner_line(tr, f"{back.get('added', 0)} recovered from history · {len(items)} new item(s) since {agents.clock12(since)}")
    if not items:
        return done(True, f"no new news since {agents.clock12(since)} — Claude not started")
    cmd, prompt = build(since, len(items), a.budget, a.model, agents.mcp_tool_names(), g)
    agents.runner_line(tr, f"Starting Claude to review {len(items)} item(s) for {name}")
    agents.write_status(JOB, step=f"{name}: reviewing {len(items)} news item(s)")
    env = {**os.environ, GUILD_ENV: key, LOCK_ENV: "1"}  # locked: a news post naming another guild can't steer the session there
    code = (run_claude or agents.run_claude)(cmd, prompt, tr, on_step=lambda s: agents.write_status(JOB, step=s["title"][:160]), timeout_s=20 * 60, env=env)
    posted = agents.proposals_posted(agents.read_run(rid))
    if code != 0:
        return done(False, f"Claude exited with code {code} after {len(items)} item(s), {posted} proposal(s) posted", len(items), posted)
    return done(True, f"{len(items)} item(s) reviewed, {posted} proposal(s) posted", len(items), posted)


def main(argv: list[str] | None = None, api_factory=None, run_claude=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the claude command and exit (no API call, no Claude)")
    p.add_argument("--since", help="ISO time to read news from (default: each guild's last good run's start)")
    p.add_argument("--budget", type=float, default=float(os.environ.get("OIBOT_REVIEW_BUDGET_USD", "1.0")), help="max USD for each guild's session")
    p.add_argument("--model", default=os.environ.get("OIBOT_AGENT_MODEL") or None)
    a = p.parse_args(argv)
    st = agents.read_status(JOB)
    if a.dry_run:
        cmd, prompt = build(a.since or since_for(st), 0, a.budget, a.model, agents.mcp_tool_names(), {"key": "<guild>", "name": "<one session per guild with news>"})
        print(shlex.join(cmd) + " < prompt")
        print(f"\n--- prompt: {PROMPT.name} ({len(prompt)} chars), ending with ---\n" + prompt[prompt.index("## This run"):])
        return 0
    if st["state"] == "running":
        print("review already running", file=sys.stderr)
        return 0

    from oibot_gm.mcp_server import Api, ToolError

    make = api_factory or (lambda guild=None: Api(guild=guild))
    started = agents.iso()
    agents.write_status(JOB, state="running", started=started, finished=None, pid=os.getpid(), step="Listing the guilds", result=None)
    try:
        guilds = make().guilds()
    except ToolError as e:
        rid, tr = agents.new_run(JOB)
        agents.runner_line(tr, f"bot not reachable: {e}", "error", done=True)
        agents.write_status(JOB, state="failed", finished=agents.iso(), run_id=rid, step=None, result=f"bot not reachable: {e}", pid=None)
        return 1
    sinces = since_map(st)
    results, all_ok, total_items, total_posted = [], True, 0, 0
    for g in guilds:  # one guild after another: each its own run, `since` and (only with news) session
        ok, line, n, posted = review_guild(g, st, a, lambda key: make(guild=key), run_claude, named=len(guilds) > 1)
        results.append(line)
        total_items, total_posted = total_items + n, total_posted + posted
        if ok:
            sinces[g["key"]] = started
        all_ok = all_ok and ok
    head = f"{len(guilds)} guild(s) · {total_items} item(s) reviewed, {total_posted} proposal(s) posted" if len(guilds) > 1 else ""
    result = " · ".join(([head] if head else []) + results) or "no guild configured"
    agents.write_status(JOB, state="idle" if all_ok else "failed", finished=agents.iso(), step=None, result=result[:900], pid=None, last_ok_started=sinces)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
