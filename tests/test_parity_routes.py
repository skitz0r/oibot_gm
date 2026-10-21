"""Chat/MCP parity, stage A1: the web routes the MCP tools wrap (fill answers, sheet re-post, the callout note on
/set, policy edit → compile → confirm | discard, the ops changes ring and loot filters, run health, config's missing
channels and member roles, the test bench's count=0, plain-permission patches, the recruitment card refresh) and the
MCP tools' request shapes over a mocked HTTP client. The bot is test_parity_verbs.Bot: the real RaidMixin verbs."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, bearer
from test_parity_verbs import Bot, Chan, fill_ask

from oibot_gm import agents, policy as pol
from oibot_gm import mcp_server as ms
from oibot_gm import registry as registry_mod
from oibot_gm.policy import CompCompiled, LootCompiled, LootRule
from oibot_gm.web import app as web


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, workload, system, user, schema):
        self.calls += 1
        if schema is LootCompiled:
            return LootCompiled(rules=[LootRule(id="R1", text="Mainspec before offspec.", kind="ordering")], summary="Mainspec first.")
        return CompCompiled(constraints=[], summary="Nothing.")

    def summary(self):
        return "fake"


@pytest.fixture
def bot(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return Bot(reg, rs)


@pytest.fixture
def c(bot):
    return TestClient(web.create_app(bot))


H = bearer()


# ---------------------------------------------------------------- runs

def test_set_with_a_note_is_a_callout(c, reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    join(reg, rs, ev, [m])
    r = c.post(f"/api/run/{ev.key}/set", json={"member": m.display_name, "status": "in", "note": "late"}, headers=H)
    assert r.status_code == 400 and "No thanks" in r.json()["error"]
    r = c.post(f"/api/run/{ev.key}/set", json={"member": m.display_name, "status": "out", "note": "dentist"}, headers=H)
    assert r.status_code == 200, r.text
    assert ev.signups[str(m.discord_id)].status == "out" and ev.callouts[-1].note == "dentist"
    d = c.get(f"/api/run/{ev.key}", headers=H).json()
    assert d["callouts"][-1]["note"] == "dentist"


def test_open_run_carries_health_locked_does_not(c, reg, rs):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:5])
    h = c.get(f"/api/run/{ev.key}", headers=H).json()["health"]
    assert h["headcount"]["in"] == 5 and h["headcount"]["seats"] >= 5
    assert {r["role"] for r in h["roles"]} >= {"tank", "healer"} and isinstance(h["no_provider"], list)
    lock_with_board(reg, rs, ev, [m.display_name for m in reg.test_members()[:5]])
    assert c.get(f"/api/run/{ev.key}", headers=H).json()["health"] is None


def test_fill_answer_route(c, reg, rs):
    ev = open_test_run(reg, rs)
    ms_ = reg.test_members()[:6]
    join(reg, rs, ev, ms_[:5])
    lock_with_board(reg, rs, ev, [m.display_name for m in ms_[:4]])
    fill_ask(reg, ev, ms_[5])
    assert c.post("/api/admin/fill-answer", json={"run": ev.key, "member": ms_[5].display_name}, headers=H).status_code == 400
    r = c.post("/api/admin/fill-answer", json={"run": ev.key, "member": ms_[5].display_name, "yes": True}, headers=H)
    assert r.status_code == 200, r.text
    assert ev.seat_of(ms_[5].display_name)
    r = c.post("/api/admin/fill-answer", json={"run": ev.key, "member": ms_[0].display_name, "yes": False}, headers=H)
    assert r.status_code == 400 and "no open fill ask" in r.json()["error"]


def test_sheet_repost_route(c, bot, reg, rs):
    ev = open_test_run(reg, rs)
    r = c.post(f"/api/run/{ev.key}/sheet", json={}, headers=H)
    assert r.status_code == 400 and "no signup channel" in r.json()["error"]
    bot.channels[61] = Chan(61, "raid-signups")
    reg.config.signup_channel_id = 61
    r = c.post(f"/api/run/{ev.key}/sheet", json={}, headers=H)
    assert r.status_code == 200 and ev.channel_id == 61, r.text
    bot.channels[62] = Chan(62, "officers")
    bot.guild_channels = lambda reg: [{"id": "62", "name": "officers"}]
    r = c.post(f"/api/run/{ev.key}/sheet", json={"channel": "#officers"}, headers=H)
    assert r.status_code == 200 and ev.channel_id == 62 and "#officers" in r.json()["message"]
    assert c.post(f"/api/run/{ev.key}/sheet", json={"channel": "#nowhere"}, headers=H).status_code == 400


def test_channel_names_resolve_on_the_sheet_and_invite_routes(c, bot, reg, rs):
    """channel_ref: '#name', a bare name, an id and <#id> resolve; unknown and ambiguous names are 400s that say so."""
    ev = open_test_run(reg, rs)
    bot.channels[81] = Chan(81, "welcome")
    bot.channels[82] = Chan(82, "general")
    bot.guild_channels = lambda reg: [{"id": "81", "name": "welcome"}, {"id": "82", "name": "general"},
                                      {"id": "83", "name": "general"}]
    made: list[int] = []

    async def create_invite(reg, cid):
        made.append(cid)
        return f"https://discord.gg/x{cid}"

    bot.create_invite = create_invite
    r = c.post("/api/admin/invite", json={"channel": "#welcome"}, headers=H)
    assert r.status_code == 200 and r.json()["invite_url"] == "https://discord.gg/x81", r.text
    assert c.post("/api/admin/invite", json={"channel": "Welcome"}, headers=H).status_code == 200
    assert c.post("/api/admin/invite", json={"channel_id": "<#82>"}, headers=H).status_code == 200
    assert made == [81, 81, 82]
    r = c.post("/api/admin/invite", json={"channel": "#nowhere"}, headers=H)
    assert r.status_code == 400 and "no channel named #nowhere" in r.text
    r = c.post("/api/admin/invite", json={"channel": "#general"}, headers=H)
    assert r.status_code == 400 and "several channels match" in r.text
    assert c.post("/api/admin/invite", json={}, headers=H).status_code == 400 and made == [81, 81, 82]
    r = c.post(f"/api/run/{ev.key}/sheet", json={"channel": "#welcome"}, headers=H)
    assert r.status_code == 200 and ev.channel_id == 81, r.text
    r = c.post(f"/api/run/{ev.key}/sheet", json={"channel": "#general"}, headers=H)
    assert r.status_code == 400 and "several channels match" in r.text and ev.channel_id == 81
    r = c.post(f"/api/run/{ev.key}/sheet", json={"channel": "#nowhere"}, headers=H)
    assert r.status_code == 400 and "no channel named #nowhere" in r.text and ev.channel_id == 81


def test_members_absence_says_which_sheets(c, reg, rs):
    ev = open_test_run(reg, rs, days=3)
    m = reg.test_members()[1]
    day = ev.start.astimezone(reg.tz).date().isoformat()
    r = c.post("/api/members/absence", json={"member": m.display_name, "start": day, "reason": "trip"}, headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["sheets"] == [ev.key] and "off 1 sheet" in d["message"] and day not in d["message"]


# ---------------------------------------------------------------- setup and ops

def test_recruitment_save_refreshes_the_registration_card(c, bot):
    r = c.post("/api/admin/recruitment", json={"open": False, "note": "full for now"}, headers=H)
    assert r.status_code == 200, r.text
    assert bot.card_refreshes == 1


def test_plain_permissions_patch_keeps_other_channels(c, reg):
    reg.config.plain.channels = {"11": "act", "12": "answer"}
    r = c.post("/api/admin/plain-permissions", json={"set_channels": {"12": "ignore", "13": "self"}}, headers=H)
    assert r.status_code == 200, r.text
    assert reg.config.plain.channels == {"11": "act", "12": "ignore", "13": "self"}


def test_test_bench_seed_zero_means_all(c, reg):
    reg.clear_test_members("t")
    r = c.post("/api/admin/test", json={"action": "seed", "count": 0}, headers=H)
    assert r.status_code == 200 and len(reg.test_members()) == len(reg.test_roster())


def test_config_marks_missing_channels_and_names_member_roles(c, bot, reg):
    reg.config.signup_channel_id, reg.config.ops_channel_id = 71, 72
    reg.config.member_role_ids = [81]
    reg.role_names = {81: "Raider"}
    guild = SimpleNamespace(roles=[], id=reg.config.discord_guild_id)
    bot.get_guild = lambda gid: guild
    bot.guild_channels = lambda reg: [{"id": "71", "name": "signups", "category": None}]
    reg.resolve_officer_roles = lambda g: []
    d = c.get("/api/config", headers=H).json()
    assert d["channels"]["signup"] == {"id": "71", "name": "signups", "missing": False}
    assert d["channels"]["ops"]["missing"] is True and d["channels"]["roster"]["missing"] is False
    assert d["settings"]["member_roles"] == [{"id": "81", "name": "Raider"}]


def test_ops_changes_ring_and_no_loot_tables(c, reg):
    """Ops keeps bot health only (design §3.1): the precedents and the award log moved to the Loot pages
    (tests/test_loot_web.py covers /api/loot/precedents and /api/loot/log with their member filters)."""
    registry_mod.RECENT_CHANGES.clear()
    reg.save_config("officer roles changed (test)")
    reg.store.append_jsonl(Path(reg.key) / "precedents.jsonl", {"item": "I0", "bot": "Someone", "human": "Other", "reason": "r"})
    d = c.get("/api/ops", headers=H).json()
    assert d["changes"][0]["text"] == "officer roles changed (test)" and d["changes"][0]["time_label"]
    assert "precedents" not in d and "ledger" not in d


# ---------------------------------------------------------------- policy: edit → compile → confirm | discard

def test_policy_edit_compile_confirm_and_discard(c, bot, reg):
    pol.PENDING.clear()
    before = c.get("/api/policy/loot", headers=H).json()["text"]
    assert c.post("/api/policy/loot/confirm", headers=H).status_code == 400  # nothing compiled yet
    r = c.put("/api/policy/loot", json={"text": "# Loot\n1. Mainspec first."}, headers=H)
    assert r.status_code == 200 and r.json()["pending"] and "draft" in r.json()["message"]
    assert c.post("/api/policy/loot/compile", headers=H).status_code == 503  # no provider: says so
    bot.ctx = SimpleNamespace(provider=FakeProvider())
    r = c.post("/api/policy/loot/compile", headers=H)
    assert r.status_code == 200 and "R1" in r.json()["reading"] and r.json()["compiled"]["summary"] == "Mainspec first."
    r = c.post("/api/policy/loot/confirm", headers=H)
    assert r.status_code == 200 and r.json()["compiled"] and not r.json()["pending"]
    # a second edit, discarded: the text goes back, the live reading stays
    c.put("/api/policy/loot", json={"text": "# Loot\nSomething else."}, headers=H)
    r = c.post("/api/policy/loot/discard", headers=H)
    assert r.status_code == 200 and r.json()["text"].strip() == "# Loot\n1. Mainspec first." and r.json()["compiled"]
    assert c.post("/api/policy/loot/discard", headers=H).status_code == 400
    assert before != "# Loot\n1. Mainspec first."
    r = c.put("/api/policy/persona", json={"text": "Be brief."}, headers=H)
    assert r.status_code == 200 and "no compile step" in r.json()["message"] and not r.json()["pending"]
    assert c.put("/api/policy/nope", json={"text": "x"}, headers=H).status_code == 400
    assert c.put("/api/policy/comp", json={"text": "   "}, headers=H).status_code == 400


# ---------------------------------------------------------------- agents.start_job

def test_start_job_refusals(tmp_path, monkeypatch):
    out = tmp_path / "agents"
    monkeypatch.setattr(agents, "launchd_status", lambda label: {"loaded": False})
    assert agents.start_job("nope", out) == (False, f"job is one of {', '.join(agents.JOBS)}")
    ok, line = agents.start_job("review", out)
    assert not ok and "isn't installed" in line
    monkeypatch.setattr(agents, "launchd_status", lambda label: {"loaded": True})
    monkeypatch.setattr(agents, "kickstart", lambda label, kill=False: (False, "boom"))
    assert agents.start_job("review", out) == (False, "launchd refused: boom")
    monkeypatch.setattr(agents, "kickstart", lambda label, kill=False: (True, ""))
    assert agents.start_job("review", out)[0]
    import os

    agents.write_status("review", out, state="running", pid=os.getpid())
    ok, line = agents.start_job("review", out)
    assert not ok and "already running" in line


# ---------------------------------------------------------------- MCP tools: one route each

@pytest.fixture
def mocked():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read() or b"{}") if request.method != "GET" else dict(request.url.params)
        calls.append((request.method, request.url.path, body))
        p = request.url.path
        if p == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps"}]})
        if p == "/api/admin/invite":
            return httpx.Response(200, json={"invite_url": "https://discord.gg/new", "message": "invite created"})
        return httpx.Response(200, json={"message": f"ok {p}", "reading": "R1", "steps": []})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    yield calls
    ms.use(None)


def test_new_tools_hit_their_routes(mocked):
    ms.fill_answer("bd", "Kessa", yes=False)
    assert mocked[-1] == ("POST", "/api/admin/fill-answer", {"run": "bd", "member": "Kessa", "yes": False})
    ms.repost_sheet("bd", "#raids")
    assert mocked[-1] == ("POST", "/api/run/bd/sheet", {"channel": "#raids"})
    ms.get_job_run("review-20260928")
    assert mocked[-1][:2] == ("GET", "/api/agents/run/review-20260928")
    ms.run_job("apply")
    assert mocked[-1] == ("POST", "/api/agents/run", {"job": "apply"})
    out = ms.create_invite("#welcome")
    assert out["invite_url"] == "https://discord.gg/new" and mocked[-1][1] == "/api/admin/invite" and "saved" not in out
    out = ms.create_invite("#welcome", save=True)
    assert out["saved"] and mocked[-1] == ("POST", "/api/admin/recruitment", {"invite_url": "https://discord.gg/new"})
    ms.policy_get("loot")
    assert mocked[-1][:2] == ("GET", "/api/policy/loot")
    ms.policy_edit("loot", "text")
    assert mocked[-1] == ("PUT", "/api/policy/loot", {"text": "text"})
    assert ms.policy_compile("loot")["reading"] == "R1" and mocked[-1][:2] == ("POST", "/api/policy/loot/compile")
    ms.policy_confirm("loot")
    ms.policy_confirm("comp", confirm=False)
    assert [c[1] for c in mocked[-2:]] == ["/api/policy/loot/confirm", "/api/policy/comp/discard"]


def test_changed_tools_send_one_body(mocked):
    # the setup tools send TYPED changes in one body (design §5.43): the bot builds the ops and coalesces them into one write
    ms.raid_set("barrow", fields={"signup_lead_hours": 48, "nudge_hours_before": 36, "tank_min": 2, "weight_rank": 4, "autofill": "no"})
    assert mocked[-1] == ("POST", "/api/ops/change", {"changes": [{"kind": "raid", "target": "barrow_deeps", "fields": {"signup_lead_hours": 48, "nudge_hours_before": 36,
                                                                  "tank_min": 2, "weight_rank": 4, "autofill": "no"}}], "apply": True, "confirmed": True})
    ms.raid_set("barrow", "notes", "bring flasks")
    assert mocked[-1][2]["changes"] == [{"kind": "raid", "target": "barrow_deeps", "fields": {"notes": "bring flasks"}}]
    ms.schedule_set("barrow", "alt", fields={"kind": "weekly", "slots": "Sat 20:00"})
    assert mocked[-1] == ("POST", "/api/ops/change", {"changes": [{"kind": "schedule", "target": "barrow_deeps:alt", "fields": {"kind": "weekly", "slots": "Sat 20:00"}}],
                                                      "apply": True, "confirmed": True})
    ms.set_answer("bd", "Kessa", "out", note="sick")
    assert mocked[-1][2]["note"] == "sick"
    ms.set_layout("bd", [["A"]], rev="abc")
    assert mocked[-1][2] == {"groups": [["A"]], "rev": "abc"}
    ms.test_bench("seed", count=0)
    assert mocked[-1][1:] == ("/api/test/seed", {"count": 0})  # one route per action (phase 2, B9)
    ms.test_bench("run", raid="barrow", tempo="quick")
    assert mocked[-1][1:] == ("/api/test/run", {"raid": "barrow_deeps", "dm_open": False, "tempo": "quick"})
    ms.ops_log(member="Kessa", precedents_limit=99)
    assert [c[1] for c in mocked[-3:]] == ["/api/ops", "/api/loot/precedents", "/api/loot/log"]
    assert mocked[-2][2] == {"member": "Kessa", "limit": "50"} and mocked[-1][2]["member"] == "Kessa" and mocked[-1][2]["scores"] == "true"


def test_the_tool_list_has_the_new_tools():
    names = {t.name for t in asyncio.run(ms.mcp.list_tools())}
    assert {"fill_answer", "repost_sheet", "get_job_run", "run_job", "create_invite", "policy_get", "policy_edit", "policy_compile", "policy_confirm"} <= names


def test_sheet_repost_refuses_a_channel_of_another_server(c, bot, reg, rs):
    """An officer can't make the bot post their guild's sheet in another server's channel (by id or <#id>): the same
    rule set_channel and create_invite keep, enforced in RaidMixin.repost_sheet so every caller gets it."""
    ev = open_test_run(reg, rs)
    before = (ev.channel_id, ev.message_id)
    other = Chan(91, "general")
    other.guild = SimpleNamespace(id=(reg.config.discord_guild_id or 0) + 12345)
    bot.channels[91] = other
    for ref in ("91", "<#91>"):
        r = c.post(f"/api/run/{ev.key}/sheet", json={"channel": ref}, headers=H)
        assert r.status_code == 400 and "another server" in r.json()["error"], r.text
    assert other.sent == [] and (ev.channel_id, ev.message_id) == before
    mine = Chan(92, "raids")
    mine.guild = SimpleNamespace(id=reg.config.discord_guild_id)
    bot.channels[92] = mine
    r = c.post(f"/api/run/{ev.key}/sheet", json={"channel": "<#92>"}, headers=H)
    assert r.status_code == 200 and ev.channel_id == 92, r.text
