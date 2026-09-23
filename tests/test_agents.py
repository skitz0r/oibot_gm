"""The local jobs and their monitor (design.md §5.26): status files, transcripts and rotation, the stream-json →
readable steps parser (on a recorded sample built here), launchctl parsing, the claude command lines and their
tool restrictions (--dry-run), the menu-bar snapshot, the API routes and their permissions, and the MCP tools'
request shapes. No network, no real claude, no launchctl."""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import OWNER, ROOT
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer

from oibot_gm import agents, news
from oibot_gm import mcp_server as ms
from oibot_gm.news import NewsStore, ProposalStore
from oibot_gm.web import app as web

URL = "https://www.wowhead.com/news/blood-pact-stacks-456"
SAMPLE = [  # what `claude -p --output-format stream-json --verbose` prints, trimmed to the fields the parser reads
    {"type": "runner", "at": "2026-09-22T16:00:00+00:00", "text": "1 new item(s) since Mon 21 Sep 9:00 AM", "level": "info"},
    {"type": "system", "subtype": "init", "model": "claude-sonnet", "tools": ["Read", "Grep", "Glob", "mcp__oibot_gm__list_news", "mcp__oibot_gm__post_proposal"]},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll read the news first."},
                                                   {"type": "tool_use", "id": "t1", "name": "mcp__oibot_gm__list_news", "input": {"since": "2026-09-23T03:00:00+00:00"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "{\"items\": [1]}"}]}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "mcp__oibot_gm__get_profile", "input": {"section": "buffs"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "{...}"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t3", "name": "mcp__oibot_gm__post_proposal", "input": {"title": "Blood Pact stacks with Fortitude"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "{\"id\": \"260922-ab12\"}"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t4", "name": "mcp__oibot_gm__post_proposal", "input": {"title": "Dup"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t4", "is_error": True, "content": "not in the news list"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 5, "total_cost_usd": 0.1234, "result": "Read 1 item, posted 1 proposal."},
]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"agents_{name}", ROOT / "scripts" / "agents" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- stream-json → steps

def test_steps_turn_the_stream_into_readable_lines():
    from zoneinfo import ZoneInfo

    la = ZoneInfo("America/Los_Angeles")
    s = agents.steps(SAMPLE, lambda v: agents.clock12(v, la))
    titles = [x["title"] for x in s]
    assert titles[0].startswith("1 new item") and titles[1].startswith("Claude started (claude-sonnet), 5 tools")
    assert "Reading news since Tue 22 Sep 8:00 PM" in titles, titles
    assert "Called get_profile(buffs)" in titles and "Posted proposal: Blood Pact stacks with Fortitude" in titles
    assert "Proposal refused: Dup" in titles
    tool = next(x for x in s if x["title"] == "Called get_profile(buffs)")
    assert tool["ok"] is True and tool["result"] == "{...}" and '"section": "buffs"' in tool["detail"]
    assert s[-1]["kind"] == "result" and s[-1]["ok"] and "$0.12" in s[-1]["title"] and "5 turns" in s[-1]["title"]
    assert agents.proposals_posted(SAMPLE) == 1
    assert not any(":00 " in t and ("20:" in t or "15:" in t) for t in titles), "no 24-hour clock"


def test_describe_tool_for_builtins():
    assert agents.describe_tool("Read", {"file_path": str(agents.ROOT / "profiles/forever/buffs.yaml")}) == "Read profiles/forever/buffs.yaml"
    assert agents.describe_tool("Bash", {"command": "uv run pytest -q"}) == "Ran uv run pytest -q"
    assert agents.describe_tool("mcp__oibot_gm__list_raids", {}) == "Called list_raids()"


# ---- status files and transcripts

def test_status_roundtrip_and_a_dead_running_job_reads_failed(tmp_path):
    agents.write_status("review", tmp_path, state="running", started=agents.iso(), pid=999_999_9, step="x")
    st = agents.read_status("review", tmp_path)
    assert st["state"] == "failed" and "interrupted" in st["result"]
    import os

    agents.write_status("review", tmp_path, state="running", pid=os.getpid())
    assert agents.read_status("review", tmp_path)["state"] == "running"
    assert agents.read_status("apply", tmp_path)["state"] == "idle", "no file yet = idle"
    assert agents.light({"state": "idle"}, {"loaded": False}) == "amber" and agents.light({"state": "failed"}) == "red" and agents.light({"state": "idle"}, {"loaded": True}) == "green"


def test_runs_rotate_and_list_newest_first(tmp_path):
    for i in range(agents.KEEP_RUNS + 3):
        rid, p = agents.new_run("apply", tmp_path, datetime(2026, 9, 1, 0, i, tzinfo=timezone.utc))
        agents.runner_line(p, f"run {i}", done=True)
    rid, p = agents.new_run("review", tmp_path, datetime(2026, 9, 2, tzinfo=timezone.utc))
    for ev in SAMPLE:
        agents.append_event(p, ev)
    rows = agents.list_runs(None, tmp_path)
    assert len([r for r in rows if r["job"] == "apply"]) == agents.KEEP_RUNS
    assert rows[0]["id"] == rid and rows[0]["outcome"] == "finished" and rows[1]["outcome"] == f"run {agents.KEEP_RUNS + 2}"
    with pytest.raises(ValueError):
        agents.read_run("../../etc/passwd", tmp_path)


def test_next_run_and_schedule():
    now = datetime(2026, 9, 22, 10, 0).astimezone()
    assert agents.next_run("review", {}, now).hour == 9 and agents.next_run("review", {}, now).day == 23
    assert agents.schedule_label("review") == "daily at 9:00 AM" and agents.schedule_label("apply") == "every 15 minutes"
    assert agents.next_run("apply", {"finished": "2026-09-22T17:00:00+00:00"}) == datetime(2026, 9, 22, 17, 15, tzinfo=timezone.utc)


def test_parse_launchctl():
    text = "gui/501/gg.earlyandoften.oibot = {\n\tactive count = 1\n\tstate = running\n\tpid = 4321\n\tlast exit code = 0\n}"
    assert agents.parse_launchctl(text) == {"loaded": True, "state": "running", "pid": 4321, "last_exit": "0"}
    assert agents.parse_launchctl("")["loaded"] is False
    calls = []
    ok, _ = agents.kickstart("x.y", kill=True, run=lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert ok and calls[0][:3] == ["launchctl", "kickstart", "-k"]


def test_run_claude_streams_into_the_transcript(tmp_path):
    lines = "\n".join(json.dumps(e) for e in SAMPLE[1:]) + "\n"

    class FakeProc:
        def __init__(self, *a, **kw):
            import io

            self.stdin, self.stdout, self.stderr, self.returncode = io.StringIO(), io.StringIO(lines), io.StringIO(""), None

        def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            pass

    seen = []
    tr = tmp_path / "t.jsonl"
    tr.touch()
    code = agents.run_claude(["claude"], "prompt", tr, on_step=lambda s: seen.append(s["title"]), popen=FakeProc)
    assert code == 0 and len(tr.read_text().splitlines()) == len(SAMPLE) - 1
    assert any(t.startswith("Posted proposal") for t in seen)


# ---- the command lines (--dry-run) and their tool restrictions

def test_review_command_allows_reads_and_post_proposal_only():
    tools = agents.mcp_tool_names()
    for t in ("list_news", "get_profile", "post_proposal", "list_proposals", "resolve_proposal", "get_proposal", "job_status"):
        assert t in tools
    cmd = agents.review_command(tools)
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
    denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--permission-mode")]
    assert cmd[:2] == ["claude", "-p"] and "stream-json" in cmd and "--verbose" in cmd and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob" and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "mcp__oibot_gm__post_proposal" in allowed and not any(a.startswith("Bash") or a in ("Edit", "Write") for a in allowed)
    writes = {"plain_change", "raid_set", "aura_set", "family_set", "set_config", "set_channel", "resolve_proposal", "lock_run", "set_answer", "test_bench"}
    assert all(f"mcp__oibot_gm__{w}" in denied for w in writes)
    assert "WebFetch" in denied and "WebSearch" in denied
    assert set(allowed).isdisjoint(denied)
    assert all(f"mcp__oibot_gm__{t}" in allowed + denied for t in tools), "every MCP tool is either allowed or denied"


def test_apply_command_edits_and_self_checks_only():
    tools = agents.mcp_tool_names()
    cmd = agents.apply_command(tools)
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
    denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--permission-mode")]
    assert "Edit" in allowed and "Bash(uv run python scripts/check_commands.py)" in allowed
    assert not any(a in ("Bash", "Bash(*)") or a.startswith("Bash(git push") or a.startswith("Bash(git commit") for a in allowed)
    assert "mcp__oibot_gm__post_proposal" in denied and "mcp__oibot_gm__plain_change" in denied and "Write" in denied


def test_dry_runs_print_the_restricted_command(capsys):
    review = load_script("review")
    assert review.main(["--dry-run", "--since", "2026-09-21T16:00:00+00:00"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("claude -p") and "--allowedTools" in out and "mcp__oibot_gm__post_proposal" in out and "--disallowedTools" in out
    assert "since: 2026-09-21T16:00:00+00:00" in out
    apply = load_script("apply")
    assert apply.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Edit" in out and "dontAsk" in out and "check_bundle.py" in out and "launchctl kickstart -k" in out
    assert apply.outside_profiles(["profiles/forever/raids.yaml", "src/x.py"]) == ["src/x.py"]
    assert "news proposal 260922-ab12" in apply.commit_message({"id": "260922-ab12", "title": "t", "change": "c", "news": [URL], "decided_by": "Kessa"})


def test_plists_match_the_jobs():
    import plistlib

    for job, spec in agents.JOBS.items():
        pl = plistlib.loads((ROOT / "scripts" / "launchd" / f"{spec['label']}.plist").read_bytes())
        assert pl["Label"] == spec["label"] and spec["script"] in pl["ProgramArguments"]
        if "hour" in spec:
            assert pl["StartCalendarInterval"] == {"Hour": spec["hour"], "Minute": spec["minute"]}
        else:
            assert pl["StartInterval"] == spec["interval"]
    mb = plistlib.loads((ROOT / "scripts" / "launchd" / f"{agents.MENUBAR_LABEL}.plist").read_bytes())
    assert "--with" in mb["ProgramArguments"] and "rumps" in mb["ProgramArguments"]
    assert "rumps" not in (ROOT / "pyproject.toml").read_text()


# ---- the menu bar (no rumps needed for the data)

def test_menubar_snapshot_reflects_the_worst_state(tmp_path):
    mb = load_script("menubar")
    up = lambda label: {"loaded": True, "state": "running", "pid": 10, "last_exit": "0"}  # noqa: E731
    (tmp_path / "data" / "g" / "proposals").mkdir(parents=True)
    (tmp_path / "data" / "g" / "proposals" / "260922-ab12.yaml").write_text("state: proposed\n")
    s = mb.snapshot(up, tmp_path / "data", tmp_path / "out", healthy=lambda: True)
    assert s["title"] == "GM ✓" and s["lines"][2] == "Approvals waiting: 1" and s["lines"][0].startswith("Bot: running")
    agents.write_status("apply", tmp_path / "out", state="failed", result="bot not reachable", finished=agents.iso())
    assert mb.snapshot(up, tmp_path / "data", tmp_path / "out", healthy=lambda: True)["title"] == "GM ⚠"
    down = lambda label: {"loaded": True, "state": "not running", "pid": None, "last_exit": "1"}  # noqa: E731
    s = mb.snapshot(down, tmp_path / "data", tmp_path / "out", healthy=lambda: False)
    assert s["title"] == "GM ✕" and "not running" in s["lines"][0]
    assert not any(":00 " in ln and "PM" not in ln and "AM" not in ln for ln in s["lines"])


# ---- API routes and permissions

class Bot:
    """The FakeBot of test_mcp plus a guild whose members the viewer resolves (officer or not)."""

    def __init__(self, reg, rs, officer: bool):
        from test_mcp import FakeBot

        self.__dict__.update(FakeBot(reg, rs).__dict__)
        self._officer = officer
        self.cards = []

    def get_guild(self, gid):
        return SimpleNamespace(id=gid)

    async def cached_member(self, guild, uid, max_age=300):
        return SimpleNamespace(id=uid, display_name="Viewer")

    def officiates(self, member, guild):
        return self._officer

    def guild_channels(self, reg):
        return []

    async def proposal_card_update(self, reg, p):
        self.cards.append((p.id, p.state))


TOKEN = "t" * 64


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "OUT", tmp_path / "agents")
    monkeypatch.setattr(agents, "launchd_status", lambda label: {"loaded": False, "state": None, "pid": None, "last_exit": None})
    return tmp_path / "agents"


def clients(reg, rs, monkeypatch, officer=True):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", "s" * 40)
    bot = Bot(reg, rs, officer)
    c = TestClient(web.create_app(bot))
    cookie = URLSafeSerializer("s" * 40, salt="session").dumps({"uid": "42", "name": "Viewer", "iat": time.time()})
    c.cookies.set(web.COOKIE, cookie)
    job = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}
    site = {"X-Requested-With": "oibot"}
    return bot, c, job, site


def seed(reg):
    NewsStore(reg.store, reg.key).add({"url": URL, "title": "WoW: Forever — Blood Pact now stacks with Fortitude", "description": "Hotfix."}, ["WoW: Forever", "Blood Pact"], news.now())


PROP = {"title": "Blood Pact stacks with Fortitude", "news": [URL], "affects": "profiles/forever/buffs.yaml: blood_pact.family", "kind": "guild_setting",
        "change": "Blood Pact and Fortitude stack", "edit": {"op": "aura_set", "target": "blood_pact", "field": "family", "value": "own"}, "evidence": "Blood Pact now stacks", "confidence": "high"}


def test_members_cannot_read_news_or_proposals(reg, rs, monkeypatch, out_dir):
    _, c, _, site = clients(reg, rs, monkeypatch, officer=False)
    for path in ("/api/news", "/api/proposals", "/api/agents", "/api/agents/profile"):
        assert c.get(path).status_code == 403, path
    assert c.post("/api/proposals", json=PROP, headers=site).status_code == 403


def test_the_review_posts_and_officers_decide(reg, rs, monkeypatch, out_dir):
    seed(reg)
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    items = c.get("/api/news", headers=job).json()
    assert items["items"][0]["url"] == URL and items["items"][0]["posted_label"][-2:] in ("AM", "PM")
    assert c.post("/api/proposals", json={**PROP, "news": ["https://elsewhere/x"]}, headers=job).status_code == 400
    r = c.post("/api/proposals", json=PROP, headers=job)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert bot.cards == [(pid, "proposed")] and any("news proposal" in t for _, t in bot.ops.lines)
    assert c.get("/api/proposals?state=proposed").json()["proposals"][0]["id"] == pid
    # the site's officer cannot report the job's steps or apply
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": "applying"}, headers=site).status_code == 400  # proposed → applying isn't a move
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": "approved"}, headers=site).status_code == 200
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": "applying"}, headers=site).status_code == 403
    assert c.post(f"/api/proposals/{pid}/apply", headers=site).status_code == 403
    # the job applies the typed op through configops: the guild's aura override is written, no restart
    r = c.post(f"/api/proposals/{pid}/apply", headers=job)
    assert r.status_code == 200, r.text
    p = ProposalStore(reg.store, reg.key).get(pid)
    assert p.state == "applied" and p.decided_by == "Viewer" and "blood_pact" in reg.config.buffs and p.note
    assert [s for _, s in bot.cards] == ["proposed", "approved", "applying", "applied"]


def test_a_bad_setting_fails_cleanly(reg, rs, monkeypatch, out_dir):
    seed(reg)
    _, c, job, site = clients(reg, rs, monkeypatch)
    r = c.post("/api/proposals", json={**PROP, "edit": {"op": "raid_set", "target": "barrow_deeps", "field": "lockout_days", "value": "banana"}}, headers=job)
    assert r.status_code == 200, "describe() only renders the diff: a bad value is caught when the op is applied"
    pid = r.json()["id"]
    c.post(f"/api/proposals/{pid}/resolve", json={"state": "approved"}, headers=site)
    r = c.post(f"/api/proposals/{pid}/apply", headers=job)
    assert r.status_code == 400 and ProposalStore(reg.store, reg.key).get(pid).state == "failed"


def test_profile_slice_marks_overrides(reg, rs, monkeypatch, out_dir):
    reg.set_buff_override("blood_pact", "scope", "raid", "t")
    _, c, job, _ = clients(reg, rs, monkeypatch)
    d = c.get("/api/agents/profile?section=buffs", headers=job).json()
    bp = next(b for b in d["buffs"]["items"] if b["id"] == "blood_pact")
    assert bp["scope"] == "raid" and bp["overridden"] == ["scope"] and bp["default"]["scope"] == "party"
    assert d["files"]["buffs"] == "profiles/forever/buffs.yaml" and "aura_set" in d["buffs"]["setting_op"]
    raids = c.get("/api/agents/profile?section=raids", headers=job).json()["raids"]["items"]
    assert any(r["id"] == "barrow_deeps" and r["lockout_days"] for r in raids)
    assert c.get("/api/agents/profile?section=nope", headers=job).status_code == 400


def test_agents_overview_runs_and_kick(reg, rs, monkeypatch, out_dir):
    rid, p = agents.new_run("review", out_dir)
    for ev in SAMPLE:
        agents.append_event(p, ev)
    agents.write_status("review", out_dir, state="idle", started=agents.iso(), finished=agents.iso(), run_id=rid, result="1 item(s) reviewed, 1 proposal(s) posted")
    _, c, job, site = clients(reg, rs, monkeypatch)
    d = c.get("/api/agents").json()
    review = next(j for j in d["jobs"] if j["job"] == "review")
    assert review["light"] == "amber" and not review["launchd"]["loaded"]  # not installed
    assert review["next_label"].endswith("9:00 AM") and d["runs"][0]["id"] == rid
    run = c.get(f"/api/agents/run/{rid}").json()
    assert any(s["title"] == "Called get_profile(buffs)" for s in run["steps"]) and run["outcome"] == "finished"
    assert c.get("/api/agents/run/nope").status_code == 404
    # "Run review now": owner only, and it needs the job installed
    assert c.post("/api/agents/run", json={"job": "review"}, headers=site).status_code == 403  # an officer, not the owner
    r = c.post("/api/agents/run", json={"job": "review"}, headers=job)
    assert r.status_code == 409 and "isn't installed" in r.json()["error"]
    monkeypatch.setattr(agents, "launchd_status", lambda label: {"loaded": True, "state": "waiting", "pid": None, "last_exit": "0"})
    kicked = []
    monkeypatch.setattr(agents, "kickstart", lambda label, kill=False: kicked.append((label, kill)) or (True, ""))
    assert c.post("/api/agents/run", json={"job": "review"}, headers=job).status_code == 200 and kicked == [(agents.JOBS["review"]["label"], False)]


def test_backfill_route_uses_the_bot(reg, rs, monkeypatch, out_dir):
    bot, c, job, _ = clients(reg, rs, monkeypatch)

    async def backfill(r):
        return 3

    bot.news_backfill = backfill
    assert c.post("/api/news/backfill", headers=job).json()["added"] == 3


# ---- the MCP tools are thin calls

def test_mcp_tools_request_shapes():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"message": "ok", "items": [], "proposals": []})

    ms.use(ms.Api(url="http://bot", token="x", transport=httpx.MockTransport(handler)))
    try:
        ms.list_news("2026-09-22T00:00:00Z")
        ms.get_profile("buffs")
        ms.post_proposal(PROP["title"], [URL], PROP["affects"], "guild_setting", PROP["change"], PROP["edit"], PROP["evidence"], "high")
        ms.list_proposals("approved")
        ms.get_proposal("260922-ab12")
        ms.resolve_proposal("260922-ab12", "applied", "abc1234", "done")
        ms.job_status()
    finally:
        ms.use(None)
    assert calls[0][:3] == ("GET", "/api/news", {"since": "2026-09-22T00:00:00Z"})
    assert calls[1][:3] == ("GET", "/api/agents/profile", {"section": "buffs"})
    assert calls[2][:2] == ("POST", "/api/proposals") and calls[2][3]["kind"] == "guild_setting" and calls[2][3]["news"] == [URL]
    assert calls[3][:3] == ("GET", "/api/proposals", {"state": "approved"})
    assert calls[4][1] == "/api/proposals/260922-ab12"
    assert calls[5][1] == "/api/proposals/260922-ab12/resolve" and calls[5][3] == {"state": "applied", "commit": "abc1234", "note": "done"}
    assert calls[6][:2] == ("GET", "/api/agents")
    assert OWNER  # the bearer identity these calls run as (test_mcp covers it)
