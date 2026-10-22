"""The apply poller (design.md §5.26, dual interfaces phase 3 §5.45; launchd: gg.earlyandoften.oibot.apply, every 15 min).

Cheap by default: one API call per guild (design §5.32: one job serves every guild). Every call carries the job header
(`X-Oibot-Job: apply`): the bot lets only the job report its steps, and never lets the job make an officer's decision.
Each poll:
1. Recovery: a proposal left `applying` by a run that died (this job isn't running, so nothing is applying it) → failed,
   "the apply run was interrupted; approve again to retry" — except the one whose restart is pending.
2. A pending restart (a profile edit committed while a guild was raiding): restart now when no served guild is busy and
   the bot is quiet (GET /api/guilds `busy`, GET /api/agents/quiet), else annotate the proposal "waiting: …" and keep it.
3. Walk the approved proposals across every guild, oldest first:
   - needs_developer → refused (marked failed) and guild_setting → applied by the bot (POST /api/proposals/<id>/apply,
     no Claude, no restart): every one of them this poll, they are cheap and independent;
   - then the FIRST profile proposal whose preflight (clean tree, on main) and raid guard pass (D33: no served guild
     busy — the restart takes the whole process down): `resolve("applying")` is the gate (refused = it was dismissed
     meanwhile → nothing done, Claude never starts); Claude edits the YAML under profiles/ (Edit + read tools + self-checks
     only, agents.apply_command); then THIS script runs check_commands, pytest and check_bundle, commits to main (or a PR
     with --pr), pushes; re-reads the proposal right before restarting (no longer applying → the commit stays, no restart,
     `restart_pending` recorded for the owner); re-checks the guard and the bot's quiet state (busy → the commit stays,
     the restart waits for a later poll); restarts the bot with launchctl and waits up to 90 s for /healthz from the new
     process. Checks fail → the edit is reverted, the proposal failed. Bot not healthy → the commit is reverted, the bot
     restarted, the proposal reverted. The other profile proposals wait, listed in the result with their guild key.

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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oibot_gm import agents  # noqa: E402
from oibot_gm.news import INTERRUPTED  # noqa: E402 — the same words as the owner's Mark as interrupted

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


def commit_message(p: dict, guild: dict | None = None) -> str:
    where = f" from {guild.get('name') or guild['key']} ({guild['key']})" if guild else ""
    return (f"profile: {p['title'][:60]} (news proposal {p['id']}{where})\n\n{p['change']}\n\n"
            f"Approved by {p.get('decided_by') or '?'}. News: {', '.join(p.get('news') or [])}\n\n{TRAILER}")


def gname(g: dict) -> str:
    return g.get("name") or g["key"] or "the guild"


def gather_approved(make, guilds: list[dict] | None = None) -> tuple[list[tuple[dict, dict]], dict, list[str]]:
    """Every guild's approved proposals as (guild row, proposal), oldest first, each guild's scoped Api, and one line
    per guild whose list failed (design §5.32: one apply job serves every guild; the oldest approval overall goes
    first). One guild answering an error (no owner set, a bad config) never holds up the others' approvals; only the
    guild listing itself failing means the bot is unreachable (raised)."""
    from oibot_gm.mcp_server import ToolError

    guilds = guilds if guilds is not None else make().guilds()
    apis = {g["key"]: make(guild=g["key"]) for g in guilds}
    rows, errors = [], []
    for g in guilds:
        try:
            rows += [(g, p) for p in apis[g["key"]].get("/api/proposals", state="approved")["proposals"]]
        except ToolError as e:
            errors.append(f"{gname(g)}: {e}")
    return sorted(rows, key=lambda gp: gp[1]["created_at"]), apis, errors


def busy_guilds(guilds: list[dict]) -> str | None:
    """D33 across EVERY served guild (the restart takes the whole process down): the line naming each busy guild, or
    None when none is. A bot from before `busy` existed says nothing: not busy."""
    busy = [g for g in guilds if (g.get("busy") or {}).get("raiding")]
    if not busy:
        return None
    return "; ".join(f"{gname(g)} raids until {(g['busy'].get('until_label') or 'the run closes')} ({g['busy'].get('why') or 'raid time'})" for g in busy)


def not_quiet(api) -> str | None:
    """GET /api/agents/quiet: why a restart now would cut into something, or None. A bot without the route (older) is quiet."""
    from oibot_gm.mcp_server import ToolError

    try:
        q = api.get("/api/agents/quiet")
    except ToolError as e:
        return None if getattr(e, "status", None) == 404 else f"the bot couldn't say whether it is quiet ({e})"
    except AssertionError:  # a test double without the route
        return None
    return None if q.get("quiet", True) else "; ".join(q.get("why") or ["the bot is busy"])


def restart_block(make, api) -> str | None:
    """Re-checked right before a restart (Claude + the checks can take 30 minutes): a fresh guild list's `busy`, then
    the bot's own quiet state. None = go ahead."""
    from oibot_gm.mcp_server import ToolError

    try:
        why = busy_guilds(make().guilds())
    except ToolError as e:
        return f"the bot didn't answer ({e})"
    return why or not_quiet(api)


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


class Run:
    """One proposal's handling: its transcript (tagged with its guild, K7), its status lines and its resolve calls."""

    def __init__(self, g: dict, prop: dict, api, named: bool):
        self.g, self.prop, self.api, self.named = g, prop, api, named
        self.rid, self.tr = agents.new_run(JOB, guild=g["key"] or None)

    def line(self, text: str, level: str = "info", **kw) -> None:
        agents.runner_line(self.tr, text, level, **kw)

    def resolve(self, state: str | None, commit: str | None = None, note: str | None = None, *, strict: bool = False) -> bool:
        """Move (or, state None, annotate) the proposal in its guild. `strict`: a refusal is returned (the gate);
        otherwise it is written to the transcript and the run goes on."""
        from oibot_gm.mcp_server import ToolError

        try:
            self.api.post(f"/api/proposals/{self.prop['id']}/resolve", {"state": state, "commit": commit, "note": note})
            return True
        except ToolError as e:
            if not strict:
                self.line(f"could not mark the proposal {state or 'noted'}: {e}", "error")
            self.refusal = str(e)
            return False

    def state_now(self) -> str | None:
        from oibot_gm.mcp_server import ToolError

        try:
            return self.api.get(f"/api/proposals/{self.prop['id']}").get("state")
        except (ToolError, AssertionError):
            return None

    def done(self, result: str, ok: bool) -> str:
        self.line(result, "info" if ok else "error", done=True)
        return (f"{self.g['key']}/" if self.named and self.g["key"] else "") + result


def annotate(api, prop: dict, note: str) -> None:
    """The proposal's "waiting: …" line (no move; the bot writes it only when it changed)."""
    from oibot_gm.mcp_server import ToolError

    try:
        api.post(f"/api/proposals/{prop['id']}/resolve", {"state": None, "note": note})
    except (ToolError, AssertionError):
        pass


def committed_for(key: str, pid: str, named: bool) -> str | None:
    """The short hash of this job's commit for proposal `pid` among the last commits on HEAD (commit_message's subject
    names it; a revert's subject starts 'Revert'), or None — also None when that commit was reverted since."""
    log_ = sh("git", "log", "-n", "50", "--format=%h%x09%s").stdout
    mark = f"(news proposal {pid}"
    for row in log_.splitlines():
        h, _, subject = row.partition("\t")
        if subject.startswith("Revert ") and mark in subject:
            return None  # reverted after it landed: nothing is owed
        if subject.startswith("profile: ") and mark in subject and (not named or not key or f"({key}))" in subject):
            return h.strip() or None
    return None


def recover(apis: dict, skip: tuple[str, str] | None, named: bool = False) -> list[str]:
    """(5) Every proposal still `applying` while this job isn't running was left by a run that died → failed with
    INTERRUPTED (the officer approves again to retry) — unless its commit is already on main (the run died after the
    commit: a push that hung, say): then the restart is owed, recorded as `restart_pending` like a deferred one.
    `skip`: (guild key, id) of the one whose restart is pending."""
    from oibot_gm.mcp_server import ToolError

    out = []
    for key, api in apis.items():
        try:
            rows = api.get("/api/proposals", state="applying")["proposals"]
        except (ToolError, AssertionError, KeyError):
            continue
        for p in rows:
            if skip and (key, p["id"]) == skip:
                continue
            commit = committed_for(key, p["id"], named) if p.get("kind", "profile") == "profile" and skip is None else None
            if commit:
                skip = (key, p["id"])
                agents.write_status(JOB, restart_pending={"guild": key, "id": p["id"], "title": (p.get("title") or p["id"])[:120], "commit": commit,
                                                          "since": agents.iso(), "auto": True})
                out.append(f"{p['id']}: committed {commit} by a run that stopped — the restart is owed")
                continue
            try:
                api.post(f"/api/proposals/{p['id']}/resolve", {"state": "failed", "note": INTERRUPTED})
                out.append(f"{p['id']}: {INTERRUPTED}")
            except ToolError as e:
                out.append(f"{p['id']}: still applying, could not mark it failed ({e})")
    return out


def revert_commit(commit: str) -> str | None:
    """`git revert` THE recorded commit (not HEAD: hours after a deferred restart HEAD is whatever landed since), only
    when it is on the current branch and the tree is clean. None = reverted; else why not."""
    if not commit:
        return "no commit recorded"
    if sh("git", "merge-base", "--is-ancestor", commit, "HEAD").returncode != 0:
        return f"{commit} is not on the checked-out branch"
    dirty = dirty_paths()
    if dirty:
        return f"the working tree has uncommitted changes ({', '.join(dirty[:5])})"
    r = sh("git", "revert", "--no-edit", commit)
    if r.returncode != 0:
        sh("git", "revert", "--abort")
        return (r.stderr or r.stdout).strip()[-200:] or f"git revert exited {r.returncode}"
    return None


def restart_and_check(run: Run, commit: str) -> tuple[str, bool]:
    """Restart the bot, wait for the new process; applied, or revert + restart + reverted. (result line, ok)."""
    pid = run.prop["id"]
    agents.write_status(JOB, step="Restarting the bot", restart_pending=None)
    run.line("Restarting the bot and waiting for it to come back")
    old = restart_bot()
    if wait_healthy(old):
        run.resolve("applied", commit, "checks passed, bot restarted healthy")
        return f"{pid}: applied @ {commit}, bot healthy", True
    run.line(f"The bot did not come back within {HEALTH_TIMEOUT_S} s — reverting {commit}", "error")
    why = revert_commit(commit)
    if why:  # HEAD may be someone else's commit by now (a deferred restart): never revert blind, never push a guess
        run.line(f"could not revert {commit}: {why}", "error")
        run.resolve("failed", commit, f"the bot did not come back healthy after the change and {commit} could not be reverted ({why}) — "
                                      "revert it by hand and restart (out/launchd.log)")
        return f"{pid}: could not revert {commit} ({why}); the bot is not healthy", False
    sh("git", "push", "--quiet")
    old = restart_bot()
    back = wait_healthy(old)
    run.resolve("reverted", commit, "the bot did not come back healthy after the change; reverted and restarted" + ("" if back else " — STILL DOWN, check out/launchd.log"))
    return f"{pid}: reverted {commit}" + ("" if back else "; the bot is still down"), False


def pending_restart(make, apis: dict, guilds: list[dict], pending: dict, named: bool) -> tuple[str | None, bool]:
    """(2) A committed profile edit waiting for its restart: (result line, ok) when it was handled now, (line, True)
    when it still waits — None as the line when there is nothing to do (an owner-only pending restart)."""
    if not pending.get("auto"):
        return None, True
    g = next((x for x in guilds if x["key"] == pending.get("guild")), None)
    api = apis.get(pending.get("guild"))
    if g is None or api is None:
        agents.write_status(JOB, restart_pending=None)
        return f"{pending.get('id')}: its guild is gone — the pending restart was dropped (the commit stays)", False
    prop = {"id": pending["id"], "title": pending.get("title") or pending["id"]}
    why = restart_block(make, api)
    if why:
        annotate(api, prop, f"committed {pending.get('commit')}; the restart waits: {why}")
        return (f"{g['key']}/" if named and g["key"] else "") + f"{prop['id']}: committed {pending.get('commit')}, restart waits — {why}", True
    tree = preflight()  # the bot runs from this checkout: a restart deploys whatever is in it
    if tree:
        annotate(api, prop, f"committed {pending.get('commit')}; the restart waits: {tree}")
        return (f"{g['key']}/" if named and g["key"] else "") + f"{prop['id']}: committed {pending.get('commit')}, restart waits — {tree}", False
    run = Run(g, prop, api, named)
    agents.write_status(JOB, state="running", run_id=run.rid, pid=os.getpid(), step=f"Restarting for {prop['id']}", guild=g["key"] or None)
    run.line(f"The raid guard is clear: restarting for {prop['id']} (committed {pending.get('commit')})")
    if run.state_now() not in (None, "applying"):
        agents.write_status(JOB, restart_pending={**pending, "auto": False, "why": "the proposal is no longer applying"})
        return run.done(f"{prop['id']}: no longer applying — the commit {pending.get('commit')} stays, not restarted (restart when convenient)", True), True
    result, ok = restart_and_check(run, pending.get("commit") or "")
    return run.done(result, ok), ok


def apply_setting(g: dict, prop: dict, api, named: bool) -> tuple[str, bool]:
    from oibot_gm.mcp_server import ToolError

    run = Run(g, prop, api, named)
    agents.write_status(JOB, state="running", run_id=run.rid, pid=os.getpid(), step=f"Applying {prop['id']}: {prop['title'][:80]}", guild=g["key"] or None)
    run.line((f"{gname(g)}: approved" if named else "Approved") + f" by {prop.get('decided_by') or '?'}: {prop['title']} ({prop['kind']})")
    if prop["kind"] == "needs_developer":
        run.resolve("failed", note="needs a developer: the apply job doesn't change code")
        return run.done(f"{prop['id']}: refused — needs a developer", True), True
    try:
        r = api.post(f"/api/proposals/{prop['id']}/apply")
        return run.done(f"{prop['id']}: {r.get('message')}", True), True
    except ToolError as e:
        return run.done(f"{prop['id']}: guild setting not applied — {e}", False), False


def apply_profile(g: dict, prop: dict, api, make, a, run_claude, named: bool) -> tuple[str, bool]:
    """One profile edit end to end (the tree is clean, on main and no guild was busy: checked by the caller)."""
    from oibot_gm.mcp_server import GUILD_ENV, LOCK_ENV

    run = Run(g, prop, api, named)
    pid = prop["id"]
    agents.write_status(JOB, state="running", run_id=run.rid, pid=os.getpid(), step=f"Applying {pid}: {prop['title'][:80]}", result=None, guild=g["key"] or None)
    run.line((f"{gname(g)}: approved" if named else "Approved") + f" by {prop.get('decided_by') or '?'}: {prop['title']} ({prop['kind']})")
    head0 = sh("git", "rev-parse", "HEAD").stdout.strip()
    if not run.resolve("applying", strict=True):  # (1) the gate: dismissed (or withdrawn) between the list and now → nothing at all
        state = run.state_now() or "moved"
        return run.done(f"{pid} was {state} before the job started — nothing done", True), True
    prompt = PROMPT.read_text() + "\n\n## The proposal (data)\n```json\n" + json.dumps(prop, indent=1, ensure_ascii=False) + "\n```\n"
    cmd = agents.apply_command(agents.mcp_tool_names(), a.budget, a.model)
    run.line("Starting Claude to edit the profile")
    env = {**os.environ, GUILD_ENV: g["key"], LOCK_ENV: "1"}  # the session's MCP reads (get_proposal, get_raid …) are the proposal's guild's, never a default
    env.pop("OIBOT_MCP_JOB", None)  # the session is not the job: it never reports the job's steps
    code = (run_claude or agents.run_claude)(cmd, prompt, run.tr, on_step=lambda s: agents.write_status(JOB, step=s["title"][:160]), timeout_s=30 * 60, env=env)
    if sh("git", "rev-parse", "HEAD").stdout.strip() != head0:
        sh("git", "reset", "--hard", head0)
        run.resolve("failed", note="the session moved HEAD; reset")
        return run.done(f"{pid}: the session committed on its own — reset to {head0[:10]}", False), False
    changed = dirty_paths()
    if code != 0 or not changed:
        revert_worktree()
        run.resolve("failed", note="no change made" if code == 0 else f"Claude exited with code {code}")
        return run.done(f"{pid}: no edit ({'Claude exited ' + str(code) if code else 'nothing changed'})", False), False
    if outside_profiles(changed):
        revert_worktree()
        run.resolve("failed", note="edited files outside profiles/: reverted")
        return run.done(f"{pid}: edits outside profiles/ ({', '.join(outside_profiles(changed)[:5])}) — reverted", False), False
    run.line("Edited " + ", ".join(changed))
    for check in agents.CHECKS:
        agents.write_status(JOB, step="Running " + " ".join(check[2:]))
        run.line("Running " + shlex.join(check))
        try:
            r = sh(*check, timeout=1800)
        except (subprocess.TimeoutExpired, OSError) as e:  # never die with unchecked edits in the tree the bot runs from
            revert_worktree()
            what = "timed out" if isinstance(e, subprocess.TimeoutExpired) else f"could not run ({e})"
            run.resolve("failed", note=f"{' '.join(check[2:])} {what}; edit reverted")
            return run.done(f"{pid}: {' '.join(check[2:])} {what} — edit reverted", False), False
        if r.returncode != 0:
            revert_worktree()
            run.line((r.stdout + r.stderr).strip()[-600:], "error")
            run.resolve("failed", note=f"{' '.join(check[2:])} failed; edit reverted")
            return run.done(f"{pid}: {' '.join(check[2:])} failed — edit reverted", False), False
    msg = commit_message(prop, g if named else None)
    if a.pr:
        br = f"news/{pid}"
        sh("git", "switch", "-c", br)
        sh("git", "add", "--", "profiles")
        sh("git", "commit", "-q", "-m", msg)
        commit = sh("git", "rev-parse", "--short", "HEAD").stdout.strip()
        push = sh("git", "push", "-u", "origin", br)
        pr = sh("gh", "pr", "create", "--fill", "--head", br) if push.returncode == 0 else push
        sh("git", "switch", "main")
        url = pr.stdout.strip().splitlines()[-1] if pr.returncode == 0 and pr.stdout.strip() else None
        run.resolve("applied", commit, f"pull request {url}" if url else "branch committed; push or PR failed")
        return run.done(f"{pid}: PR {url or '(not opened)'} @ {commit}", bool(url)), bool(url)
    try:
        sh("git", "add", "--", "profiles")
        made = sh("git", "commit", "-q", "-m", msg)
    except (subprocess.TimeoutExpired, OSError) as e:
        made = SimpleNamespace(returncode=1, stderr=str(e), stdout="")
    if made.returncode != 0:
        revert_worktree()
        run.resolve("failed", note="the commit failed; edit reverted")
        return run.done(f"{pid}: the commit failed ({(made.stderr or '').strip()[-200:]}) — edit reverted", False), False
    commit = sh("git", "rev-parse", "--short", "HEAD").stdout.strip()
    run.line(f"Committed {commit}")
    pending = {"guild": g["key"], "id": pid, "title": prop["title"][:120], "commit": commit, "since": agents.iso()}
    agents.write_status(JOB, restart_pending={**pending, "auto": True})  # from here on a restart is owed, whatever happens to this run
    try:
        push = sh("git", "push", "--quiet")
    except (subprocess.TimeoutExpired, OSError) as e:
        push = SimpleNamespace(returncode=1, stderr=str(e), stdout="")
    if push.returncode != 0:
        run.line("push failed (the commit is local; the bot runs from this checkout): " + push.stderr.strip()[-200:], "warn")
    state = run.state_now()  # (2) re-read right before restarting: someone may have moved it while Claude and the checks ran
    if state not in (None, "applying"):
        agents.write_status(JOB, restart_pending={**pending, "auto": False, "why": f"the proposal became {state} while it was applied"})
        return run.done(f"{pid}: became {state} meanwhile — the commit {commit} stays, the bot was not restarted (restart when convenient)", True), True
    why = restart_block(make, api)  # (3) the raid guard again, across every guild, plus the bot's quiet state
    if why:
        agents.write_status(JOB, restart_pending={**pending, "auto": True})
        run.resolve(None, note=f"committed {commit}; the restart waits: {why}")
        return run.done(f"{pid}: committed {commit}; the restart waits — {why}", True), True
    result, ok = restart_and_check(run, commit)
    return run.done(result, ok), ok


def preflight() -> str | None:
    dirty = dirty_paths()
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return (f"the working tree has uncommitted changes ({', '.join(dirty[:5])}) — commit or stash them" if dirty
            else f"the checkout is on {branch}, not main" if branch != "main" else None)


def main(argv: list[str] | None = None, api_factory=None, run_claude=None) -> int:
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

    make = api_factory or (lambda guild=None: Api(guild=guild, job=JOB))
    polled = agents.iso()
    try:
        guilds = make().guilds()
        approved_rows, apis, errors = gather_approved(make, guilds)
    except ToolError as e:
        agents.write_status(JOB, state="failed", started=polled, finished=agents.iso(), result=f"bot not reachable: {e}", step=None, pid=None)
        return 1
    named = len(apis) > 1
    pending = st.get("restart_pending") or None
    lines = recover(apis, (pending["guild"], pending["id"]) if pending else None, named)
    pending = agents.read_status(JOB).get("restart_pending") or None  # recovery may have found a commit that is owed its restart
    for err in errors:  # another guild's list failed: the others go ahead; the failure goes to the job's log, not a guild's transcript
        print(f"apply: could not list approvals for {err}", file=sys.stderr)
    all_ok = True
    if pending:
        line, ok = pending_restart(make, apis, guilds, pending, named)
        if line:
            lines.append(line)
        all_ok = all_ok and ok
        pending = agents.read_status(JOB).get("restart_pending") or None  # handled now → cleared
    cheap = [(g, prop) for g, prop in approved_rows if prop["kind"] in ("guild_setting", "needs_developer")]
    profiles = [(g, prop) for g, prop in approved_rows if prop["kind"] == "profile"]
    for g, prop in cheap:  # (4) every guild setting and every needs-developer refusal: cheap and independent
        line, ok = apply_setting(g, prop, apis[g["key"]], named)
        lines.append(line)
        all_ok = all_ok and ok
    waiting: list[str] = []
    tree_bad = False  # a dirty checkout or another branch: the owner must act, so the job reads failed (as it always did)
    if profiles:
        why = "a restart is pending" if pending and pending.get("auto") else None
        if why is None:
            why = preflight()
            tree_bad = why is not None
        if why is None:
            busy = busy_guilds(guilds)  # (3) D33: any served guild busy → every game-data change waits, annotated
            if busy:
                why = f"raid time — {busy}"
                for g, prop in profiles:
                    annotate(apis[g["key"]], prop, f"waiting: {busy}")
        if why is None:
            g, prop = profiles[0]
            line, ok = apply_profile(g, prop, apis[g["key"]], make, a, run_claude, named)
            lines.append(line)
            all_ok = all_ok and ok
            rest, why = profiles[1:], "one game-data change per poll"
        else:
            rest = profiles
        waiting = [f"{g['key']}/{prop['id']}" if named and g["key"] else prop["id"] for g, prop in rest]
        if waiting:
            lines.append(f"waiting ({why}): {', '.join(waiting)}")
    if not lines:
        agents.write_status(JOB, state="failed" if errors else "idle", started=polled, finished=agents.iso(), step=None, pid=None, waiting=0,
                            result="nothing approved" + (" · could not list " + " · ".join(errors) if errors else ""))
        return 1 if errors else 0
    failed = (not all_ok) or tree_bad
    agents.write_status(JOB, state="failed" if failed else "idle", started=polled, finished=agents.iso(), step=None, pid=None, waiting=len(waiting),
                        result=" · ".join(lines)[:900])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
