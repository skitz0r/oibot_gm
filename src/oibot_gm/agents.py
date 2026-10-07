"""The local agent jobs on the Mac mini (design.md §5.26) and what the monitor reads about them.

Two jobs run headless `claude -p` next to the bot, each as its own LaunchAgent:
- `review` (daily): reads the news the bot kept since its last run, compares it with the effective profile and
  guild settings through the MCP server, and posts proposals for real contradictions only. No new news → no
  Claude session at all.
- `apply` (every 15 minutes): a cheap poll for approved proposals. A guild setting is applied by the bot itself
  (a typed config op, no restart); a profile edit starts Claude to edit the YAML, then the runner — not the model —
  runs the checks, commits, pushes, restarts the bot and waits for it to come back healthy (revert on failure).

Each job keeps a status file (`out/agents/<job>.json`: state idle|running|failed, started, finished, last result,
current step, run id) and one stream-json transcript per run (`out/agents/runs/<job>-<stamp>.jsonl`, the last 30
per job). The web Agents page and the menu-bar item read the same files; `steps()` turns a transcript into lines a
person reads. Times in the files are ISO (machines); everything shown goes through `clock12`."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .registry import CLOCK12

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "agents"
KEEP_RUNS = 30
BOT_LABEL = "gg.earlyandoften.oibot"
MENUBAR_LABEL = "gg.earlyandoften.oibot.menubar"
JOBS: dict[str, dict] = {
    # the review runs at 9:00 AM local: yesterday's news is in, officers see the cards in the morning
    "review": {"label": "gg.earlyandoften.oibot.news-review", "name": "News review", "hour": 9, "minute": 0, "script": "scripts/agents/review.py"},
    "apply": {"label": "gg.earlyandoften.oibot.apply", "name": "Apply approved", "interval": 900, "script": "scripts/agents/apply.py"},
}
MCP = "mcp__oibot_gm__"
BUILTIN_READ = ("Read", "Grep", "Glob")
# never available to either job, whatever the settings files say: no web access (the news article is never fetched), no writes but Edit
ALWAYS_DENY = ("WebFetch", "WebSearch", "Write", "NotebookEdit", "Task", "Agent")
REVIEW_MCP = ("list_news", "get_profile", "list_proposals", "guild_overview", "list_raids", "get_raid", "list_auras", "post_proposal")
APPLY_MCP = ("get_proposal", "get_profile", "list_raids", "get_raid", "list_auras")
# the apply session may edit and self-check; the runner does commit / push / restart / health / revert itself
APPLY_BASH = ("Bash(uv run python scripts/check_commands.py)", "Bash(uv run pytest *)", "Bash(git diff *)", "Bash(git status *)")
CHECKS = (["uv", "run", "python", "scripts/check_commands.py"], ["uv", "run", "pytest", "-q"], ["uv", "run", "python", "scripts/check_bundle.py"])


# ---------------------------------------------------------------- time

def aware(value) -> datetime | None:
    if not value:
        return None
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.astimezone()


def iso(t: datetime | None = None) -> str:
    return (t or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def clock12(value, tz=None) -> str:
    """'Tue 22 Sep 8:00 PM' — the site's and Registry.local12's form (no military time where a person reads)."""
    t = aware(value)
    if t is None:
        return ""
    return re.sub(r"\b0(\d:\d\d [AP]M)", r"\1", t.astimezone(tz).strftime(CLOCK12))


# ---------------------------------------------------------------- status files

def status_path(job: str, out: Path | None = None) -> Path:
    return (out or OUT) / f"{job}.json"  # OUT read at call time (tests and the scripts' callers can point it elsewhere)


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def read_status(job: str, out: Path | None = None) -> dict:
    """The job's status file, with defaults. A 'running' file whose process is gone reads as failed (interrupted)."""
    p = status_path(job, out)
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        st = {}
    st = {"job": job, "state": "idle", "started": None, "finished": None, "result": None, "step": None, "run_id": None, **st}
    if st["state"] == "running" and st.get("pid") and not pid_alive(st["pid"]):
        st.update(state="failed", result="interrupted (the process is gone)", step=None)
    return st


def write_status(job: str, out: Path | None = None, **fields) -> dict:
    """Merge fields into the status file (atomic replace, so a reader never sees half a file)."""
    out = out or OUT
    out.mkdir(parents=True, exist_ok=True)
    p = status_path(job, out)
    try:
        cur = json.loads(p.read_text())
    except (OSError, ValueError):
        cur = {"job": job}
    cur.update(fields, updated=iso())
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    os.replace(tmp, p)
    return cur


def next_run(job: str, st: dict | None = None, now: datetime | None = None) -> datetime | None:
    """When launchd starts the job next: the daily hour for the review, the last poll + 15 minutes for apply."""
    spec = JOBS[job]
    now = (now or datetime.now().astimezone())
    if "hour" in spec:
        t = now.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    last = aware((st or {}).get("finished") or (st or {}).get("started"))
    return (last + timedelta(seconds=spec["interval"])) if last else None


def schedule_label(job: str) -> str:
    spec = JOBS[job]
    if "hour" in spec:
        h, m = spec["hour"], spec["minute"]
        return f"daily at {h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    return f"every {spec['interval'] // 60} minutes"


def light(st: dict, launchd: dict | None = None) -> str:
    """green (idle, last run fine) | amber (running, or not loaded in launchd) | red (last run failed)."""
    if st.get("state") == "failed":
        return "red"
    if st.get("state") == "running" or (launchd is not None and not launchd.get("loaded")):
        return "amber"
    return "green"


# ---------------------------------------------------------------- transcripts

def runs_dir(out: Path | None = None) -> Path:
    return (out or OUT) / "runs"


def new_run(job: str, out: Path | None = None, now: datetime | None = None, guild: str | None = None) -> tuple[str, Path]:
    """A fresh transcript file; older ones beyond KEEP_RUNS for this job are removed. `guild`: the guild the run is for
    (design §5.32: one review session per guild) — its first line is a `meta` event naming it, which the transcript
    readers skip; the Agents page shows a guild only its own runs (`run_visible`). Two runs in one second (two guilds
    with nothing to do) get consecutive ids instead of sharing a file."""
    d = runs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    while True:
        rid = f"{job}-{t.strftime('%Y%m%d-%H%M%S')}"
        p = d / f"{rid}.jsonl"
        if not p.exists():
            break
        t += timedelta(seconds=1)
    p.touch()
    if guild:
        append_event(p, {"type": "meta", "guild": guild, "at": iso()})
    rotate(job, out)
    return rid, p


LEGACY_FILE = "legacy_guild"  # out/agents/legacy_guild: the guild every untagged (pre-multi-guild) run belongs to (K7)


def tag_legacy_runs(guild: str, out: Path | None = None) -> bool:
    """Once, while exactly one guild is configured (the bot's start): the runs written before runs carried a guild are
    that guild's. Never rewritten afterwards (a second guild must not inherit them). True when written now."""
    out = out or OUT
    f = out / LEGACY_FILE
    if f.exists():
        return False
    out.mkdir(parents=True, exist_ok=True)
    f.write_text(guild + "\n")
    return True


def legacy_guild(out: Path | None = None) -> str | None:
    try:
        return ((out or OUT) / LEGACY_FILE).read_text().strip() or None
    except OSError:
        return None


def run_guild(run_id: str, out: Path | None = None) -> str | None:
    """The guild a run was for (its first line's `meta` event), None for an untagged (older) run."""
    try:
        with run_path(run_id, out).open() as f:
            first = f.readline()
        ev = json.loads(first) if first.strip() else {}
    except (OSError, ValueError):
        return None
    return ev.get("guild") if isinstance(ev, dict) and ev.get("type") == "meta" else None


def runs_scope(guilds, reg) -> str | None:
    """Whose runs a guild's viewer sees: every run while one guild is configured (exactly as before), else `reg.key`
    (run_visible: its own runs, plus the untagged ones legacy_guild gives it)."""
    only = getattr(guilds, "only", None)
    if only is None:  # no guild index to ask (a bare client): as before
        return None
    return None if only() is not None else reg.key


def run_visible(run_id: str, guild: str | None, out: Path | None = None) -> bool:
    """`guild` None = every run (the MCP bearer: the owner's jobs). Otherwise a run tagged for that guild, or an untagged
    one when legacy_guild says the untagged runs are that guild's (K7: never another guild's transcript)."""
    if guild is None:
        return True
    tag = run_guild(run_id, out)
    return tag == guild if tag else legacy_guild(out) == guild


def rotate(job: str, out: Path | None = None, keep: int = KEEP_RUNS) -> None:
    for old in sorted(runs_dir(out).glob(f"{job}-*.jsonl"))[:-keep]:
        old.unlink(missing_ok=True)


def run_path(run_id: str, out: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-z]+-\d{8}-\d{6}", run_id or ""):
        raise ValueError(f"no run {run_id!r}")
    return runs_dir(out) / f"{run_id}.jsonl"


def list_runs(job: str | None = None, out: Path | None = None, guild: str | None = None) -> list[dict]:
    """Newest first: {id, job, started (ISO), outcome, guild} — the outcome is the transcript's last runner/result line.
    `guild`: only the runs that guild may see (run_visible); None = all."""
    rows = []
    for p in runs_dir(out).glob(f"{job or '*'}-*.jsonl"):
        rid = p.stem
        if not run_visible(rid, guild, out):
            continue
        j, d, t = rid.rsplit("-", 2)
        started = datetime.strptime(d + t, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        rows.append({"id": rid, "job": j, "started": iso(started), "outcome": outcome(read_run(rid, out)), "guild": run_guild(rid, out)})
    return sorted(rows, key=lambda r: r["started"], reverse=True)


def read_run(run_id: str, out: Path | None = None) -> list[dict]:
    p = run_path(run_id, out)
    if not p.exists():
        raise ValueError(f"no run {run_id}")
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append({"type": "runner", "text": line[:500], "level": "info"})
    return rows


def append_event(path: Path, event: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def runner_line(path: Path, text: str, level: str = "info", **extra) -> dict:
    """A step the runner script itself took (not Claude): written into the same transcript."""
    ev = {"type": "runner", "at": iso(), "text": text, "level": level, **extra}
    append_event(path, ev)
    return ev


def outcome(events: list[dict]) -> str:
    """One line: the runner's `done` line, else Claude's result, else the last thing that happened."""
    for ev in reversed(events):
        if ev.get("type") == "runner" and ev.get("done"):
            return str(ev.get("text"))
    for ev in reversed(events):
        if ev.get("type") == "result":
            return "finished" if not ev.get("is_error") else f"stopped: {ev.get('subtype', 'error')}"
    return "running" if events else "empty"


# ---------------------------------------------------------------- stream-json → readable steps

def _short(name: str) -> str:
    return name[len(MCP):] if name.startswith(MCP) else name


def _rel(path: str) -> str:
    p = str(path or "")
    return p[len(str(ROOT)) + 1:] if p.startswith(str(ROOT) + "/") else p


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def describe_tool(name: str, args: dict, when: Callable[[str], str] = clock12) -> str:
    """'Reading news since Tue 22 Sep 8:00 PM', 'Called get_profile(buffs)', 'Posted proposal: …'."""
    n, a = _short(name), args or {}
    if n == "list_news":
        return f"Reading news since {when(a['since'])}" if a.get("since") else "Reading all the news kept"
    if n == "get_profile":
        return f"Called get_profile({a.get('section') or 'all'})"
    if n == "post_proposal":
        return f"Posted proposal: {a.get('title') or '?'}"
    if n == "list_proposals":
        return f"Listed proposals ({a.get('state') or 'all'})"
    if n == "get_proposal":
        return f"Read proposal {a.get('id', '?')}"
    if n == "resolve_proposal":
        return f"Marked proposal {a.get('id', '?')} {a.get('state', '?')}"
    if n == "Read":
        return f"Read {_rel(a.get('file_path', '?'))}"
    if n == "Edit":
        return f"Edited {_rel(a.get('file_path', '?'))}"
    if n == "Grep":
        return f"Searched for \u201c{str(a.get('pattern', ''))[:60]}\u201d"
    if n == "Glob":
        return f"Listed files {a.get('pattern', '')}"
    if n == "Bash":
        return f"Ran {str(a.get('command', ''))[:100]}"
    inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:40]}" for k, v in a.items())
    return f"Called {n}({inner[:120]})"


def steps(events: list[dict], when: Callable[[str], str] = clock12) -> list[dict]:
    """The transcript as steps: {kind: runner|start|text|tool|result, title, detail, ok, at}. A tool's result is
    folded into its step (`result`, `ok`), collapsed on the page."""
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for ev in events:
        t = ev.get("type")
        if t == "runner":
            out.append({"kind": "runner", "title": str(ev.get("text", "")), "ok": ev.get("level") != "error", "level": ev.get("level", "info"), "at": ev.get("at")})
        elif t == "system" and ev.get("subtype") == "init":
            tools = ev.get("tools") or []
            out.append({"kind": "start", "title": f"Claude started ({ev.get('model', 'model?')}), {len(tools)} tools allowed", "detail": ", ".join(_short(x) for x in tools), "ok": True})
        elif t == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    text = block["text"].strip()
                    out.append({"kind": "text", "title": text.splitlines()[0][:160], "detail": text if "\n" in text or len(text) > 160 else None, "ok": True})
                elif block.get("type") == "tool_use":
                    step = {"kind": "tool", "tool": _short(block.get("name", "")), "title": describe_tool(block.get("name", ""), block.get("input") or {}, when),
                            "detail": json.dumps(block.get("input") or {}, indent=1, ensure_ascii=False)[:4000], "ok": None, "result": None}
                    by_id[block.get("id", "")] = step
                    out.append(step)
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    step = by_id.get(block.get("tool_use_id", ""))
                    if step is not None:
                        step["result"] = _result_text(block.get("content"))[:4000]
                        step["ok"] = not block.get("is_error")
                        if step["tool"] == "post_proposal" and not step["ok"]:
                            step["title"] = step["title"].replace("Posted proposal", "Proposal refused", 1)
        elif t == "result":
            bad = bool(ev.get("is_error")) or ev.get("subtype") not in (None, "success")
            cost = ev.get("total_cost_usd")
            tail = f" · {ev.get('num_turns', '?')} turns" + (f" · ${cost:.2f}" if isinstance(cost, (int, float)) else "")
            out.append({"kind": "result", "title": ("Claude stopped: " + str(ev.get("subtype", "error"))) if bad else ("Claude finished" + tail), "detail": str(ev.get("result") or "")[:4000] or None, "ok": not bad})
    return out


def proposals_posted(events: list[dict]) -> int:
    return sum(1 for s in steps(events) if s["kind"] == "tool" and s.get("tool") == "post_proposal" and s.get("ok"))


# ---------------------------------------------------------------- launchd

def _uid() -> int:
    return os.getuid()


def launchd_status(label: str, run=subprocess.run) -> dict:
    """{loaded, state, pid, last_exit} from `launchctl print gui/<uid>/<label>` (not loaded → loaded False)."""
    try:
        r = run(["launchctl", "print", f"gui/{_uid()}/{label}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": False, "state": None, "pid": None, "last_exit": None}
    return parse_launchctl(r.stdout if r.returncode == 0 else "")


def parse_launchctl(text: str) -> dict:
    def field(name: str) -> str | None:
        m = re.search(rf"^\s*{re.escape(name)} = (.+)$", text, re.MULTILINE)
        return m.group(1).strip() if m else None

    pid = field("pid")
    return {"loaded": bool(text.strip()), "state": field("state"), "pid": int(pid) if pid and pid.isdigit() else None, "last_exit": field("last exit code")}


def kickstart(label: str, kill: bool = False, run=subprocess.run) -> tuple[bool, str]:
    """Ask launchd to start a loaded job now (`-k` kills a running one first: the bot restart)."""
    args = ["launchctl", "kickstart", *(["-k"] if kill else []), f"gui/{_uid()}/{label}"]
    try:
        r = run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def start_job(job: str, out: Path | None = None) -> tuple[bool, str]:
    """Start a local job now (the Agents page's Run now, MCP run_job, plain text): refused when the job is unknown,
    already running or not installed with launchd; otherwise `launchctl kickstart` on the job's own LaunchAgent — the
    bot never runs Claude itself. Returns (started, the line to show). Blocking (launchctl): call it off the loop."""
    spec = JOBS.get(job)
    if spec is None:
        return False, f"job is one of {', '.join(JOBS)}"
    if read_status(job, out or OUT)["state"] == "running":
        return False, f"{spec['name']} is already running"
    if not launchd_status(spec["label"])["loaded"]:
        return False, f"{spec['name']} isn't installed on this Mac (scripts/agents/install.sh)"
    ok, text = kickstart(spec["label"])
    if not ok:
        return False, f"launchd refused: {text[:200]}"
    return True, f"{spec['name']} started"


def overview(out: Path | None = None, tz=None, launchctl: Callable[[str], dict] | None = None) -> dict:
    """What the monitor shows per job: state + light, last run and outcome, next run, launchd."""
    launchctl = launchctl or launchd_status
    jobs = []
    for job, spec in JOBS.items():
        st = read_status(job, out)
        ld = launchctl(spec["label"])
        nxt = next_run(job, st)
        jobs.append({"job": job, "name": spec["name"], "label": spec["label"], "state": st["state"], "light": light(st, ld), "step": st.get("step"),
                     "run_id": st.get("run_id"), "result": st.get("result"), "started": st.get("started"), "finished": st.get("finished"),
                     "started_label": clock12(st.get("started"), tz), "finished_label": clock12(st.get("finished"), tz),
                     "next": iso(nxt) if nxt else None, "next_label": clock12(nxt, tz) if nxt else "", "launchd": ld,
                     "schedule": schedule_label(job)})
    return {"jobs": jobs, "bot": launchctl(BOT_LABEL)}


# ---------------------------------------------------------------- the claude command lines

def mcp_tool_names() -> list[str]:
    """Every tool the project MCP server exposes (to deny the ones a job must not use)."""
    import asyncio

    from . import mcp_server

    return sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))


def claude_command(allowed: list[str], builtin: tuple[str, ...], all_mcp: list[str], budget: float, model: str | None = None) -> list[str]:
    """`claude -p` headless in the repo with the project MCP server only, the given built-in tools, only `allowed`
    pre-approved and everything else denied (dontAsk: an unlisted tool is refused, never prompted). The prompt goes
    on stdin (the tool lists are variadic, so nothing positional may follow them). stream-json (+ --verbose, which
    it requires) records every step."""
    deny = [f"{MCP}{t}" for t in all_mcp if f"{MCP}{t}" not in allowed] + [t for t in ALWAYS_DENY if t not in allowed]
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(ROOT / ".mcp.json"), "--strict-mcp-config",
           "--tools", ",".join(builtin),
           "--allowedTools", *allowed,  # one token per rule (the CLI's own examples: "Bash(git log *)" "Read")
           "--disallowedTools", *deny,
           "--permission-mode", "dontAsk",
           "--max-budget-usd", f"{budget:g}",
           "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    return cmd


def review_command(all_mcp: list[str], budget: float = 1.0, model: str | None = None) -> list[str]:
    allowed = [*BUILTIN_READ, *(f"{MCP}{t}" for t in REVIEW_MCP)]
    return claude_command(allowed, BUILTIN_READ, all_mcp, budget, model)


def apply_command(all_mcp: list[str], budget: float = 2.0, model: str | None = None) -> list[str]:
    allowed = [*BUILTIN_READ, "Edit", *APPLY_BASH, *(f"{MCP}{t}" for t in APPLY_MCP)]
    return claude_command(allowed, (*BUILTIN_READ, "Edit", "Bash"), all_mcp, budget, model)


def run_claude(cmd: list[str], prompt: str, transcript: Path, on_step: Callable[[dict], None] | None = None, timeout_s: int = 1800, popen=subprocess.Popen,
               env: dict | None = None) -> int:
    """Run the session, streaming every stream-json line into the transcript; `on_step` gets each readable step as
    it happens (the status file's current step). Returns the exit code (124 on timeout). `env`: the child's environment
    (the review pins OIBOT_MCP_GUILD there, so the MCP server the session starts acts on one guild); None = inherit."""
    import threading

    proc = popen(cmd, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    killed = threading.Event()

    def kill() -> None:
        killed.set()
        proc.kill()

    timer = threading.Timer(timeout_s, kill)
    timer.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        seen: list[dict] = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"type": "runner", "text": line[:500], "level": "info"}
            append_event(transcript, ev)
            seen.append(ev)
            if on_step:
                s = steps(seen)
                if s:
                    on_step(s[-1])
        err = proc.stderr.read().strip()
        code = proc.wait()
    finally:
        timer.cancel()
    if err:
        runner_line(transcript, "claude stderr: " + err[-1500:], "error" if code else "info")
    if killed.is_set():
        runner_line(transcript, f"stopped: the session ran longer than {timeout_s // 60} minutes", "error")
        return 124
    return code
