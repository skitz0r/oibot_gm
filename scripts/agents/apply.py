"""The apply poller (design.md §5.25; launchd: gg.earlyandoften.oibot.apply, every 15 minutes).

Cheap by default: one API call. Only when a proposal is approved does it do anything, oldest first, one per poll:
- needs_developer → refused (marked failed; the card says a developer has to do it).
- guild_setting   → the bot applies the typed config op itself (POST /api/proposals/<id>/apply). No Claude, no restart.
- profile         → Claude edits the YAML under profiles/ (Edit + read tools + self-checks only, see
                    agents.apply_command); then THIS script, not the model: runs check_commands, pytest and
                    check_bundle, commits to main (or opens a PR with --pr), pushes, restarts the bot with launchctl
                    and waits up to 90 s for /healthz from the new process. Checks fail → the edit is reverted,
                    the proposal marked failed. Bot not healthy → the commit is reverted, the bot restarted, the
                    proposal marked reverted.
Refuses to run on a dirty working tree or off main (the proposal stays approved; the status file says why).

    uv run python scripts/agents/apply.py             # what launchd runs
    uv run python scripts/agents/apply.py --dry-run   # print the claude command, run nothing
    uv run python scripts/agents/apply.py --pr        # profile edits go to a branch + PR instead of main (no restart)
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oibot_gm import agents  # noqa: E402

JOB = "apply"
ROOT = agents.ROOT
PROMPT = Path(__file__).with_name("apply_profile.md")
HEALTH_TIMEOUT_S = 90
TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


def sh(*args: str, check: bool = False, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=str(ROOT), capture_output=True, text=True, check=check, timeout=timeout)


def porcelain_paths(text: str) -> list[str]:
    return [ln[3:].strip() for ln in text.splitlines() if ln.strip()]


def dirty_paths() -> list[str]:
    """Changes to tracked files anywhere, plus anything new under profiles/ (untracked files elsewhere — the
    .claude/ worktrees, scratch files — are not ours to judge; ignored paths like out/ never count)."""
    tracked = porcelain_paths(sh("git", "status", "--porcelain", "--untracked-files=no").stdout)
    profiles = porcelain_paths(sh("git", "status", "--porcelain", "--", "profiles").stdout)
    return list(dict.fromkeys(tracked + profiles))


def outside_profiles(paths: list[str]) -> list[str]:
    return [p for p in paths if not p.startswith("profiles/")]


def revert_worktree() -> None:
    sh("git", "checkout", "--", ".")
    sh("git", "clean", "-fdq", "--", "profiles")


def commit_message(p: dict) -> str:
    return (f"profile: {p['title'][:60]} (news proposal {p['id']})\n\n{p['change']}\n\n"
            f"Approved by {p.get('decided_by') or '?'}. News: {', '.join(p.get('news') or [])}\n\n{TRAILER}")


def healthz_url() -> str:
    return (os.environ.get("OIBOT_MCP_URL") or "http://127.0.0.1:8788").rstrip("/") + "/healthz"


def wait_healthy(old_pid: int | None, timeout_s: int = HEALTH_TIMEOUT_S) -> bool:
    """The bot is back when launchd shows a NEW pid and /healthz answers with the Discord user logged in."""
    import httpx

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ld = agents.launchd_status(agents.BOT_LABEL)
        if ld.get("pid") and ld["pid"] != old_pid:
            try:
                r = httpx.get(healthz_url(), timeout=3)
                if r.status_code == 200 and r.json().get("bot"):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
        time.sleep(3)
    return False


def restart_bot() -> int | None:
    old = agents.launchd_status(agents.BOT_LABEL).get("pid")
    agents.kickstart(agents.BOT_LABEL, kill=True)
    return old


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the claude command for a profile edit and exit")
    p.add_argument("--pr", action="store_true", default=os.environ.get("OIBOT_APPLY_MODE") == "pr", help="profile edits: branch + PR, no restart")
    p.add_argument("--budget", type=float, default=float(os.environ.get("OIBOT_APPLY_BUDGET_USD", "2.0")))
    p.add_argument("--model", default=os.environ.get("OIBOT_AGENT_MODEL") or None)
    a = p.parse_args(argv)
    if a.dry_run:
        print(shlex.join(agents.apply_command(agents.mcp_tool_names(), a.budget, a.model)) + " < prompt")
        print("then: " + " && ".join(shlex.join(c) for c in agents.CHECKS) + f" && git commit && git push && launchctl kickstart -k gui/<uid>/{agents.BOT_LABEL}" + (" (PR mode: branch + gh pr create, no restart)" if a.pr else ""))
        return 0
    st = agents.read_status(JOB)
    if st["state"] == "running":
        return 0

    from oibot_gm.mcp_server import Api, ToolError

    api = Api()
    polled = agents.iso()
    try:
        approved = api.get("/api/proposals", state="approved")["proposals"]
    except ToolError as e:
        agents.write_status(JOB, state="failed", started=polled, finished=agents.iso(), result=f"bot not reachable: {e}", step=None, pid=None)
        return 1
    if not approved:
        agents.write_status(JOB, state="idle", started=polled, finished=agents.iso(), result="nothing approved", step=None, pid=None, waiting=0)
        return 0
    prop = sorted(approved, key=lambda x: x["created_at"])[0]
    if prop["kind"] == "profile":  # preflight before a transcript: a waiting proposal must not fill the run history
        dirty = dirty_paths()
        branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        why = (f"the working tree has uncommitted changes ({', '.join(dirty[:5])}) — commit or stash them" if dirty
               else f"the checkout is on {branch}, not main" if branch != "main" else None)
        if why:
            agents.write_status(JOB, state="failed", started=polled, finished=agents.iso(), step=None, pid=None, waiting=len(approved), result=f"{prop['id']} waits: {why}")
            return 1
    rid, tr = agents.new_run(JOB)
    agents.write_status(JOB, state="running", started=polled, finished=None, run_id=rid, pid=os.getpid(), step=f"Applying {prop['id']}: {prop['title'][:80]}", result=None, waiting=len(approved))
    agents.runner_line(tr, f"Approved by {prop.get('decided_by') or '?'}: {prop['title']} ({prop['kind']})")

    def resolve(state: str, commit: str | None = None, note: str | None = None) -> None:
        try:
            api.post(f"/api/proposals/{prop['id']}/resolve", {"state": state, "commit": commit, "note": note})
        except ToolError as e:
            agents.runner_line(tr, f"could not mark the proposal {state}: {e}", "error")

    def finish(state: str, result: str, ok: bool) -> int:
        agents.runner_line(tr, result, "info" if ok else "error", done=True)
        agents.write_status(JOB, state=state, finished=agents.iso(), step=None, result=result, pid=None)
        return 0 if ok else 1

    if prop["kind"] == "needs_developer":
        resolve("failed", note="needs a developer: the apply job doesn't change code")
        return finish("idle", f"{prop['id']}: refused — needs a developer", True)
    if prop["kind"] == "guild_setting":
        try:
            r = api.post(f"/api/proposals/{prop['id']}/apply")
            return finish("idle", f"{prop['id']}: {r.get('message')}", True)
        except ToolError as e:
            return finish("failed", f"{prop['id']}: guild setting not applied — {e}", False)

    # ---- a profile edit (the tree is clean and on main: checked above)
    head0 = sh("git", "rev-parse", "HEAD").stdout.strip()
    resolve("applying")
    prompt = PROMPT.read_text() + "\n\n## The proposal (data)\n```json\n" + json.dumps(prop, indent=1, ensure_ascii=False) + "\n```\n"
    cmd = agents.apply_command(agents.mcp_tool_names(), a.budget, a.model)
    agents.runner_line(tr, "Starting Claude to edit the profile")
    code = agents.run_claude(cmd, prompt, tr, on_step=lambda s: agents.write_status(JOB, step=s["title"][:160]), timeout_s=30 * 60)
    if sh("git", "rev-parse", "HEAD").stdout.strip() != head0:
        sh("git", "reset", "--hard", head0)
        resolve("failed", note="the session moved HEAD; reset")
        return finish("failed", f"{prop['id']}: the session committed on its own — reset to {head0[:10]}", False)
    changed = dirty_paths()
    if code != 0 or not changed:
        revert_worktree()
        resolve("failed", note="no change made" if code == 0 else f"Claude exited with code {code}")
        return finish("failed", f"{prop['id']}: no edit ({'Claude exited ' + str(code) if code else 'nothing changed'})", False)
    if outside_profiles(changed):
        revert_worktree()
        resolve("failed", note="edited files outside profiles/: reverted")
        return finish("failed", f"{prop['id']}: edits outside profiles/ ({', '.join(outside_profiles(changed)[:5])}) — reverted", False)
    agents.runner_line(tr, "Edited " + ", ".join(changed))
    for check in agents.CHECKS:
        agents.write_status(JOB, step="Running " + " ".join(check[2:]))
        agents.runner_line(tr, "Running " + shlex.join(check))
        r = sh(*check, timeout=1800)
        if r.returncode != 0:
            revert_worktree()
            tail = (r.stdout + r.stderr).strip()[-600:]
            agents.runner_line(tr, tail, "error")
            resolve("failed", note=f"{' '.join(check[2:])} failed; edit reverted")
            return finish("failed", f"{prop['id']}: {' '.join(check[2:])} failed — edit reverted", False)
    msg = commit_message(prop)
    if a.pr:
        br = f"news/{prop['id']}"
        sh("git", "switch", "-c", br)
        sh("git", "add", "--", "profiles")
        sh("git", "commit", "-q", "-m", msg)
        commit = sh("git", "rev-parse", "--short", "HEAD").stdout.strip()
        push = sh("git", "push", "-u", "origin", br)
        pr = sh("gh", "pr", "create", "--fill", "--head", br) if push.returncode == 0 else push
        sh("git", "switch", "main")
        url = pr.stdout.strip().splitlines()[-1] if pr.returncode == 0 and pr.stdout.strip() else None
        resolve("applied", commit, f"pull request {url}" if url else "branch committed; push or PR failed")
        return finish("idle", f"{prop['id']}: PR {url or '(not opened)'} @ {commit}", bool(url))
    sh("git", "add", "--", "profiles")
    sh("git", "commit", "-q", "-m", msg)
    commit = sh("git", "rev-parse", "--short", "HEAD").stdout.strip()
    agents.runner_line(tr, f"Committed {commit}")
    push = sh("git", "push", "--quiet")
    if push.returncode != 0:
        agents.runner_line(tr, "push failed (the commit is local; the bot runs from this checkout): " + push.stderr.strip()[-200:], "warn")
    agents.write_status(JOB, step="Restarting the bot")
    agents.runner_line(tr, "Restarting the bot and waiting for it to come back")
    old = restart_bot()
    if wait_healthy(old):
        resolve("applied", commit, "checks passed, bot restarted healthy")
        return finish("idle", f"{prop['id']}: applied @ {commit}, bot healthy", True)
    agents.runner_line(tr, f"The bot did not come back within {HEALTH_TIMEOUT_S} s — reverting {commit}", "error")
    sh("git", "revert", "--no-edit", "HEAD")
    sh("git", "push", "--quiet")
    old = restart_bot()
    back = wait_healthy(old)
    resolve("reverted", commit, "the bot did not come back healthy after the change; reverted and restarted" + ("" if back else " — STILL DOWN, check out/launchd.log"))
    return finish("failed", f"{prop['id']}: reverted {commit}" + ("" if back else "; the bot is still down"), False)


if __name__ == "__main__":
    sys.exit(main())
