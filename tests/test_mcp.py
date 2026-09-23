"""The MCP server (design.md §5.24): bearer auth in web/app.py, the name/run resolvers the API grew for it, the tool
layer over a mocked HTTP client, and a stdio smoke (initialize + list_tools against the real entry point).
No network to the real bot: the web app runs on a fake bot over the demo fixtures (conftest)."""
from __future__ import annotations

import asyncio
import json
import string
import sys
from collections import Counter

import httpx
import pytest
from conftest import OWNER, join, open_test_run
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from oibot_gm import mcp_server as ms
from oibot_gm import raidcycle as rc
from oibot_gm.web import app as web

TOKEN = "a" * 64


class FakeOps:
    def __init__(self):
        self.lines = []
        self.recent = []

    async def emit(self, cfg, level, text, exc=None):
        self.lines.append((level, text))


class FakeBot:
    """Just enough of OibotGM for create_app + the API: one registry, no Discord guild (the bearer path never needs one)."""

    def __init__(self, reg, rs):
        self.registries = type("R", (), {"by_discord": {reg.config.discord_guild_id: reg}, "store": reg.store})()
        self.raids = type("S", (), {"store": lambda _self, r: rs})()
        self.ops = FakeOps()
        self.user = None
        self.started_at = 0.0
        self.ctx = None

    def get_guild(self, gid):
        return None

    async def cached_member(self, guild, uid, max_age=300):
        return None

    def officiates(self, member, guild):
        return False

    def guild_channels(self, reg):
        return []

    async def refresh_sheet(self, reg, ev):
        self.refreshed = [*getattr(self, "refreshed", []), ev.key]

    async def set_answer(self, reg, rs, ev, m, status, character, by):
        return rc.LABELS[rc.set_signup(reg, rs, ev, m, character, status, source="officer").status] + f" for {m.display_name}"


@pytest.fixture
def client(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FakeBot(reg, rs)))


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "oibot"}


# ---- bearer auth

def test_bearer_token_is_the_owner(client):
    r = client.get("/api/meta", headers=bearer())
    assert r.status_code == 200, r.text
    v = r.json()["viewer"]
    assert v == {"uid": str(OWNER), "name": "mcp", "officer": True, "owner": True}


def test_wrong_or_missing_token_is_nobody(client):
    assert client.get("/api/meta", headers=bearer("b" * 64)).status_code == 401
    assert client.get("/api/meta", headers=bearer("")).status_code == 401
    assert client.get("/api/meta", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/api/meta").status_code == 401


def test_bearer_disabled_without_configured_token(reg, rs, monkeypatch):
    monkeypatch.delenv(web.MCP_TOKEN_ENV, raising=False)
    c = TestClient(web.create_app(FakeBot(reg, rs)))
    assert c.get("/api/meta", headers=bearer()).status_code == 401
    assert not web.bearer_ok(httpx.Request("GET", "http://x", headers={"Authorization": f"Bearer {TOKEN}"}), None)


def test_csrf_marker_still_required_for_writes(client):
    r = client.post("/api/members/dm", json={"member": "nobody", "on": True}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403


# ---- the routes and resolvers the MCP tools lean on

def test_get_run_by_key_and_by_raid_name(client, reg, rs):
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:3])
    r = client.get(f"/api/run/{ev.key}", headers=bearer())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["key"] == ev.key and d["counts"]["in"] == 3 and "board" in d and "timeline" in d and "signups" in d
    name = reg.raid_def(ev.instance)["name"]
    assert client.get(f"/api/run/{name}", headers=bearer()).json()["key"] == ev.key
    assert client.get("/api/run/nope-0000", headers=bearer()).status_code == 404


def test_several_live_runs_for_a_raid_is_an_error(client, reg, rs):
    a, b = open_test_run(reg, rs, days=2), open_test_run(reg, rs, days=5)
    r = client.get(f"/api/run/{a.instance}", headers=bearer())
    assert r.status_code == 400 and a.key in r.json()["error"] and b.key in r.json()["error"]


def test_member_resolution_ambiguous_and_unknown(client, reg, rs):
    ev = open_test_run(reg, rs)
    names = [m.display_name.lower() for m in reg.test_members()]
    letter, n = Counter(ch for ch in string.ascii_lowercase for x in names if ch in x).most_common(1)[0]
    assert n >= 2
    r = client.post(f"/api/run/{ev.key}/set", json={"member": letter, "status": "in"}, headers=bearer())
    assert r.status_code == 400 and "which one" in r.json()["error"], r.text
    r = client.post(f"/api/run/{ev.key}/set", json={"member": "zzz-nobody", "status": "in"}, headers=bearer())
    assert r.status_code == 400 and "unknown member" in r.json()["error"]
    m = reg.test_members()[0]
    r = client.post(f"/api/run/{ev.key}/set", json={"member": m.main.name, "status": "sub"}, headers=bearer())
    assert r.status_code == 200 and m.display_name in r.json()["message"], r.text
    assert ev.signups[str(m.discord_id)].status == "sub"


def test_member_get_and_absences(client, reg):
    m = reg.test_members()[0]
    reg.add_absence(m.discord_id, "2099-01-02", "2099-01-03", "holiday", "t")
    d = client.get("/api/member", params={"name": m.display_name}, headers=bearer()).json()
    assert d["display_name"] == m.display_name and d["absences"][0]["start"] == "2099-01-02" and d["characters"]
    rows = client.get("/api/absences", headers=bearer()).json()["rows"]
    assert any(r["display_name"] == m.display_name and r["end"] == "2099-01-03" for r in rows)


def test_members_set_rank_confirm_main(client, reg):
    m = reg.test_members()[1]
    r = client.post("/api/members/set", json={"member": m.display_name, "rank": "core", "confirm": True}, headers=bearer())
    assert r.status_code == 200, r.text
    assert "rank core" in r.json()["message"] and "confirmed" in r.json()["message"]
    assert reg.members[m.discord_id].main.rank == "core"
    r = client.post("/api/members/set", json={"member": m.display_name, "rank": "emperor"}, headers=bearer())
    assert r.status_code == 400


# ---- the tool layer over a mocked bot

RUN = {"key": "bd-1209-1930-2026-12-09", "run": "bd-1209-1930", "raid": "Barrow Deeps", "when": "Wed 09 Dec 7:30 PM", "rel": "in 2 days", "state": "open", "fill_state": None,
       "size": 20, "counts": {"in": 12, "sub": 2, "out": 1}, "rostered": 0, "n_rosters": 0, "signups": [], "board": {"bank": [], "rosters": []}}


def fake_bot(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization"), request.headers.get("x-requested-with")))
        p = request.url.path
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "login required"})
        if p == "/api/config":
            return httpx.Response(200, json={"settings": {"timezone": "America/Los_Angeles", "owner_id": "1", "officer_roles": [{"id": "2", "name": "Officer"}]},
                                             "channels": {"signup": {"id": "3", "name": "raid-signups"}}, "docs": {}, "test_bench": {"members": 0, "runs": []}})
        if p == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps", "size": 20, "slots": ["Tue 19:30"], "lockout_days": 7, "signup_lead_hours": 96,
                                                        "lock_hours_before": 6, "confirm_hours_before": 2, "split_policy": "balanced", "opened": True, "live": 1}], "weight_keys": ["main"], "split_policies": ["balanced"]})
        if p == "/api/rosters":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps", "size": 20, "opened": True, "open": [RUN], "locked": [], "past": [], "upcoming": []}], "orphans": [], "tz": "UTC"})
        if p.startswith("/api/run/") and request.method == "GET":
            key = p.split("/")[3]
            if key in (RUN["key"], "Barrow Deeps"):
                return httpx.Response(200, json=RUN)
            return httpx.Response(404, json={"error": f"no live run {key}"})
        if p.endswith("/set"):
            if json.loads(request.read()).get("member") == "K":
                return httpx.Response(400, json={"error": "K: which one? Kessa, Korrin"})
            return httpx.Response(200, json={"message": "Kessa Join on bd-1209-1930-2026-12-09"})
        return httpx.Response(404, json={"error": "no such route"})

    return httpx.MockTransport(handler)


def call(name: str, **args):
    return asyncio.run(ms.mcp.call_tool(name, args))


@pytest.fixture
def mocked():
    calls: list = []
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=fake_bot(calls)))
    yield calls
    ms.use(None)


def test_tools_read_through_the_api(mocked):
    out = ms.guild_overview()
    assert out["guild"]["timezone"] == "America/Los_Angeles" and out["channels"]["signup"] == "raid-signups"
    assert out["live_runs"][0]["key"] == RUN["key"] and out["raids"][0]["id"] == "barrow_deeps"
    assert ms.get_run("Barrow Deeps")["counts"]["in"] == 12
    assert ms.get_raid("barrow")["name"] == "Barrow Deeps"
    assert all(c[2] == f"Bearer {TOKEN}" and c[3] == "oibot" for c in mocked), "every call carries the token and the CSRF marker"


def test_tool_write_returns_the_message_line(mocked):
    assert ms.set_answer(RUN["key"], "Kessa", "in") == "Kessa Join on bd-1209-1930-2026-12-09"
    with pytest.raises(ToolError, match="status must be one of"):
        ms.set_answer(RUN["key"], "Kessa", "maybe")
    assert mocked[-1][1].endswith("/set")


def test_tool_errors_carry_the_servers_reason(mocked):
    with pytest.raises(ToolError, match="which one\\? Kessa, Korrin"):
        ms.set_answer(RUN["key"], "K", "in")
    with pytest.raises(ToolError, match="no live run"):
        ms.get_run("nope")
    with pytest.raises(ToolError, match="no live run nope"):  # through the MCP layer too (the transport turns it into an error result)
        call("get_run", run="nope")
    assert call("get_raid", raid="barrow")[0].text
    ms.use(ms.Api(url="http://bot", token="wrong", transport=fake_bot([])))
    with pytest.raises(ToolError, match="rejected the token"):
        ms.list_runs()


def test_bot_down_is_a_clear_error():
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    ms.use(ms.Api(url="http://127.0.0.1:1", token=TOKEN, transport=httpx.MockTransport(down)))
    try:
        with pytest.raises(ToolError, match="oibot is not running on http://127.0.0.1:1; start it with `uv run oibot discord`"):
            ms.list_runs()
        ms.use(ms.Api(url="http://127.0.0.1:1", token="", transport=httpx.MockTransport(down)))
        with pytest.raises(ToolError, match="OIBOT_MCP_TOKEN is not set"):
            ms.list_runs()
    finally:
        ms.use(None)


def test_stdio_initialize_and_list_tools():
    """The real entry point over stdio: a client handshake and the tool list (no bot needed for either)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "oibot_gm.mcp_server"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                return init.serverInfo.name, [t.name for t in tools.tools]

    name, tools = asyncio.run(asyncio.wait_for(go(), 60))
    assert name == "oibot_gm"
    assert {"guild_overview", "list_runs", "get_run", "set_answer", "lock_run", "plain_change", "test_bench"} <= set(tools)


def test_board_moves_merge_and_stale_whole_board_write_is_refused(client, reg, rs):
    """Two officers on one board: single moves merge; a whole-board write from an old view is refused with the current board."""
    ev = open_test_run(reg, rs)
    members = reg.test_members()[:6]
    join(reg, rs, ev, members)
    a, b, c = (m.display_name for m in members[:3])
    seen = client.get(f"/api/run/{ev.key}", headers=bearer()).json()["rev"]  # officer B loads the empty board
    r = client.post(f"/api/run/{ev.key}/move", json={"member": a, "to": {"r": 0, "g": 0, "i": 0}}, headers=bearer())
    assert r.status_code == 200 and r.json()["rev"] != seen
    r = client.post(f"/api/run/{ev.key}/move", json={"member": b, "to": {"r": 0, "g": 1, "i": 0}}, headers=bearer())  # B's drag, made on the stale view
    groups = [[s["display_name"] for s in g] for g in r.json()["board"]["rosters"][0]["groups"]]
    assert groups[0] == [a] and groups[1] == [b]  # A's placement survived
    r = client.post(f"/api/run/{ev.key}/layout", json={"groups": [[c]], "rev": seen}, headers=bearer())  # B presses Clear/Use split on the old view
    assert r.status_code == 409 and "board" in r.json() and "changed" in r.json()["error"]
    r = client.post(f"/api/run/{ev.key}/layout", json={"groups": [[c]], "rev": r.json()["rev"]}, headers=bearer())
    assert r.status_code == 200
    r = client.post(f"/api/run/{ev.key}/move", json={"member": "Nobody", "to": None}, headers=bearer())
    assert r.status_code == 409 and "board" in r.json()
