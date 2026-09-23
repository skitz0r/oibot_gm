"""The daily news review (design.md §5.25; launchd: gg.earlyandoften.oibot.news-review, daily at 9:00 AM).

1. Asks the running bot to re-read the #news channel (posts made while it was down), then for the news it kept
   since the last good run. Nothing new → done, no Claude session.
2. Otherwise runs `claude -p` headless in the repo with the project MCP server, read tools plus post_proposal only
   (see agents.review_command), prompt = scripts/agents/news_review.md. Every step goes to a transcript.
3. Writes out/agents/review.json as it goes (the Agents page and the menu bar read it).

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


def since_for(st: dict) -> str:
    return st.get("last_ok_started") or agents.iso(datetime.now(timezone.utc) - timedelta(days=FIRST_LOOKBACK_DAYS))


def build(since: str, n_items: int, budget: float, model: str | None, all_mcp: list[str]) -> tuple[list[str], str]:
    prompt = PROMPT.read_text() + f"\n\n## This run\n- since: {since} (pass it to list_news)\n- new items: {n_items}\n"
    return agents.review_command(all_mcp, budget, model), prompt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the claude command and exit (no API call, no Claude)")
    p.add_argument("--since", help="ISO time to read news from (default: the last good run's start)")
    p.add_argument("--budget", type=float, default=float(os.environ.get("OIBOT_REVIEW_BUDGET_USD", "1.0")), help="max USD for the session")
    p.add_argument("--model", default=os.environ.get("OIBOT_AGENT_MODEL") or None)
    a = p.parse_args(argv)
    st = agents.read_status(JOB)
    since = a.since or since_for(st)
    if a.dry_run:
        cmd, prompt = build(since, 0, a.budget, a.model, agents.mcp_tool_names())
        print(shlex.join(cmd) + " < prompt")
        print(f"\n--- prompt: {PROMPT.name} ({len(prompt)} chars), ending with ---\n" + prompt[prompt.index("## This run"):])
        return 0
    if st["state"] == "running":
        print("review already running", file=sys.stderr)
        return 0

    from oibot_gm.mcp_server import Api, ToolError

    started = agents.iso()
    rid, tr = agents.new_run(JOB)
    agents.write_status(JOB, state="running", started=started, finished=None, run_id=rid, pid=os.getpid(), step="Checking the news", result=None)

    def finish(state: str, result: str, ok: bool) -> int:
        agents.runner_line(tr, result, "info" if ok else "error", done=True)
        extra = {"last_ok_started": started} if ok else {}
        agents.write_status(JOB, state=state, finished=agents.iso(), step=None, result=result, pid=None, **extra)
        return 0 if ok else 1

    api = Api()
    try:
        agents.runner_line(tr, "Re-reading the news channel for posts made while the bot was down")
        back = api.post("/api/news/backfill")
        items = api.get("/api/news", since=since)["items"]
    except ToolError as e:
        return finish("failed", f"bot not reachable: {e}", False)
    agents.runner_line(tr, f"{back.get('added', 0)} recovered from history · {len(items)} new item(s) since {agents.clock12(since)}")
    if not items:
        return finish("idle", f"no new news since {agents.clock12(since)} — Claude not started", True)

    cmd, prompt = build(since, len(items), a.budget, a.model, agents.mcp_tool_names())
    agents.runner_line(tr, f"Starting Claude to review {len(items)} item(s)")
    agents.write_status(JOB, step=f"Reviewing {len(items)} news item(s)")
    code = agents.run_claude(cmd, prompt, tr, on_step=lambda s: agents.write_status(JOB, step=s["title"][:160]), timeout_s=20 * 60)
    events = agents.read_run(rid)
    posted = agents.proposals_posted(events)
    if code != 0:
        return finish("failed", f"Claude exited with code {code} after {len(items)} item(s), {posted} proposal(s) posted", False)
    return finish("idle", f"{len(items)} item(s) reviewed, {posted} proposal(s) posted", True)


if __name__ == "__main__":
    sys.exit(main())
