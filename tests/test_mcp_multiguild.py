"""Several guilds and the MCP server (design §5.32 "MCP", §5.24): the bearer names its guild with X-Oibot-Guild (a key;
a unique name as an ASCII fallback), no header = the only guild, else a 400 listing them; each call acts as the OWNER
OF THAT GUILD; still local-only; GET /api/guilds; and the client side — every tool's optional `guild`, the
OIBOT_MCP_GUILD default (non-overriding .env), names resolved to keys before they reach a header, list_guilds."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import GUILD, GUILD2, OWNER, OWNER2
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from test_mcp import TOKEN, FakeBot

from oibot_gm import mcp_server as ms
from oibot_gm.web import app as web


@pytest.fixture
def two(duo, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv(ms.GUILD_ENV, raising=False)
    bot = FakeBot(duo.a, duo.rs_a)
    bot.registries = duo.guilds
    stores = {GUILD: duo.rs_a, GUILD2: duo.rs_b}
    bot.raids = type("S", (), {"store": lambda _self, r: stores[r.key]})()
    return TestClient(web.create_app(bot)), duo


def hdr(guild=None, token=TOKEN):
    h = {"Authorization": f"Bearer {token}", "X-Requested-With": "oibot"}
    if guild is not None:
        h["X-Oibot-Guild"] = guild
    return h


# ---------------------------------------------------------------- the bot side

def test_the_header_picks_the_guild_and_the_owner_of_that_guild_acts(two):
    c, duo = two
    for ref, uid, key in ((GUILD, OWNER, GUILD), (GUILD2, OWNER2, GUILD2), ("duo guild", OWNER2, GUILD2)):
        r = c.get("/api/meta", headers=hdr(ref))
        assert r.status_code == 200, r.text
        assert r.json()["viewer"]["uid"] == str(uid) and r.json()["guild_key"] == key, ref
    assert c.get("/api/config", headers=hdr(GUILD2)).json()["guild"] == {"key": GUILD2, "name": "Duo Guild", "discord_guild_id": "2"}


def test_no_header_with_two_guilds_is_a_400_listing_both(two):
    c, _ = two
    r = c.get("/api/meta", headers=hdr())
    assert r.status_code == 400 and "Which guild?" in r.json()["error"] and "demo (Demo Guild)" in r.json()["error"] and "duo (Duo Guild)" in r.json()["error"]
    r = c.get("/api/meta", headers=hdr("nope"))
    assert r.status_code == 400 and "No guild 'nope'" in r.json()["error"] and "duo (Duo Guild)" in r.json()["error"]


def test_a_guild_without_an_owner_is_refused(two):
    c, duo = two
    duo.b.config.owner_discord_id = None
    r = c.get("/api/meta", headers=hdr(GUILD2))
    assert r.status_code == 400 and "has no owner yet" in r.json()["error"]


def test_still_local_only_before_any_guild_is_chosen(duo, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    bot = FakeBot(duo.a, duo.rs_a)
    bot.registries = duo.guilds
    tunnel = TestClient(web.create_app(bot), client=("203.0.113.9", 4000))
    assert tunnel.get("/api/meta", headers=hdr(GUILD2)).status_code == 401
    assert tunnel.get("/api/meta", headers={**hdr(), "cf-connecting-ip": "1.2.3.4"}).status_code == 401, "refused as nobody, never a 400 that names the guilds"
    assert tunnel.get("/api/guilds", headers=hdr()).status_code == 401


def test_api_guilds_lists_every_guild_for_the_bearer(two):
    c, _ = two
    rows = c.get("/api/guilds", headers=hdr()).json()["guilds"]
    assert [(g["key"], g["name"], g["owner_discord_id"]) for g in rows] == [(GUILD, "Demo Guild", str(OWNER)), (GUILD2, "Duo Guild", str(OWNER2))]
    assert c.get("/api/guilds", headers=hdr(token="b" * 64)).status_code == 401


def test_writes_go_to_the_chosen_guild_only(two):
    c, duo = two
    r = c.post("/api/members/dm", json={"member": str(duo.b.test_members()[0].discord_id), "on": False}, headers=hdr(GUILD2))
    assert r.status_code == 200, r.text
    uid = duo.b.test_members()[0].discord_id
    assert duo.b.members[uid].dm_opt_out is True and duo.a.members[uid].dm_opt_out is not True, "the same puppet id in demo is untouched"


# ---------------------------------------------------------------- the client side

def through(client: TestClient, **kw) -> ms.Api:
    """An Api whose HTTP goes straight into the test app (TestClient: the loopback client address)."""
    def handler(request: httpx.Request) -> httpx.Response:
        r = client.request(request.method, request.url.path, params=dict(request.url.params), content=request.read(),
                           headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "application/json")})
    return ms.Api(url="http://testserver", token=TOKEN, transport=httpx.MockTransport(handler), **kw)


def test_every_tool_takes_a_guild_and_forwards_it():
    tools = asyncio.run(ms.mcp.list_tools())
    missing = [t.name for t in tools if t.name != "list_guilds" and "guild" not in t.inputSchema.get("properties", {})]
    assert not missing and len(tools) > 50
    desc = next(t for t in tools if t.name == "get_run").inputSchema["properties"]["guild"]["description"]
    assert "OIBOT_MCP_GUILD" in desc


def test_the_guild_argument_and_the_env_default_send_the_key(two, monkeypatch):
    c, duo = two
    ms.use(through(c))
    try:
        assert ms.guild_overview(guild="Duo Guild")["guild"]["key"] == GUILD2, "a name is resolved to the key"
        assert ms.guild_overview(guild="DEMO")["guild"]["key"] == GUILD, "case-insensitive"
        with pytest.raises(ToolError, match="Which guild\\?"):
            ms.guild_overview()
        with pytest.raises(ToolError, match="no guild 'nope'.*duo \\(Duo Guild\\)"):
            ms.guild_overview(guild="nope")
        monkeypatch.setenv(ms.GUILD_ENV, "duo guild")
        ms.use(through(c))
        assert ms.guild_overview()["guild"]["key"] == GUILD2, "OIBOT_MCP_GUILD is the default"
        assert ms.guild_overview(guild=GUILD)["guild"]["key"] == GUILD, "a call's own guild wins"
        out = ms.list_guilds()
        assert out["default"] == GUILD2 and [g["default"] for g in out["guilds"]] == [False, True]
    finally:
        ms.use(None)


def test_tools_that_call_tools_forward_the_guild(duo):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("x-oibot-guild")))
        if request.url.path == "/api/guilds":
            return httpx.Response(200, json={"guilds": [{"key": GUILD, "name": "Demo Guild"}, {"key": GUILD2, "name": "Düo Gíld"}]})
        if request.url.path == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps"}]})
        return httpx.Response(200, json={"message": "opened"})
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        assert ms.open_run("Barrow Deeps", guild="düo gíld") == "opened"
    finally:
        ms.use(None)
    assert ("/api/guilds", None) in seen, "the guild list is asked without a guild"
    assert [h for p, h in seen if p != "/api/guilds"] == [GUILD2, GUILD2], "get_raid inside open_run carries it; a non-ASCII name travels as its key"


def test_a_job_pinned_guild_beats_the_env_file_default(tmp_path, monkeypatch):
    """K8: load_dotenv never overrides, so the review job's OIBOT_MCP_GUILD (the child's environment) wins over .env."""
    from dotenv import load_dotenv

    env = tmp_path / ".env"
    env.write_text("OIBOT_MCP_GUILD=demo\n")
    monkeypatch.setenv(ms.GUILD_ENV, GUILD2)
    load_dotenv(env)
    assert ms.Api(url="http://bot", token=TOKEN).guild == GUILD2
    monkeypatch.delenv(ms.GUILD_ENV)
    load_dotenv(env)
    assert ms.Api(url="http://bot", token=TOKEN).guild == GUILD


def test_main_guild_flag_sets_the_default(monkeypatch):
    monkeypatch.delenv(ms.GUILD_ENV, raising=False)
    monkeypatch.setattr(ms.mcp, "run", lambda transport: None)
    ms.main(["--guild", "duo"])
    import os

    assert os.environ[ms.GUILD_ENV] == "duo"
    monkeypatch.delenv(ms.GUILD_ENV)
    ms.use(None)
