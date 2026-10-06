"""The MCP owner token works only from this machine (OIBOT_MCP_LOCAL_ONLY, default on): the right token arriving
through the Cloudflare tunnel — a non-loopback client, or any proxy header — is refused like a wrong one and logged
without the token; switching it off restores remote use; browser sessions are unaffected."""
import logging

import pytest
from fastapi.testclient import TestClient
from test_mcp import FakeBot, TOKEN, bearer

from oibot_gm.web import app as web


@pytest.fixture
def bot(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv(web.MCP_LOCAL_ENV, raising=False)
    return FakeBot(reg, rs)


def test_local_call_with_the_token_is_the_owner(bot):
    c = TestClient(web.create_app(bot), client=("127.0.0.1", 40000))
    assert c.get("/api/meta", headers=bearer()).status_code == 200
    c6 = TestClient(web.create_app(bot), client=("::1", 40000))
    assert c6.get("/api/meta", headers=bearer()).status_code == 200


@pytest.mark.parametrize("client,extra", [
    (("203.0.113.9", 4000), {}),                                   # uvicorn put the real (remote) client here
    (("127.0.0.1", 4000), {"CF-Connecting-IP": "203.0.113.9"}),     # came through cloudflared
    (("127.0.0.1", 4000), {"X-Forwarded-For": "203.0.113.9"}),
    (("127.0.0.1", 4000), {"CF-Ray": "8a1b2c3d4e5f-SJC"}),
    (("127.0.0.1", 4000), {"Forwarded": "for=203.0.113.9"}),
])
def test_the_right_token_from_outside_is_refused_and_logged(bot, client, extra, caplog):
    c = TestClient(web.create_app(bot), client=client)
    with caplog.at_level(logging.WARNING, logger="oibot_gm.web.app"):
        r = c.get("/api/meta", headers={**bearer(), **extra})
    assert r.status_code == 401
    assert any("local-only" in rec.getMessage() for rec in caplog.records)
    assert all(TOKEN not in rec.getMessage() for rec in caplog.records)


def test_a_wrong_token_from_outside_is_not_logged_as_a_leak(bot, caplog):
    c = TestClient(web.create_app(bot), client=("203.0.113.9", 4000))
    with caplog.at_level(logging.WARNING, logger="oibot_gm.web.app"):
        assert c.get("/api/meta", headers=bearer("b" * 64)).status_code == 401
    assert not any("local-only" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("off", ["0", "false", "off", "NO"])
def test_switching_it_off_accepts_the_token_from_outside(bot, monkeypatch, off):
    monkeypatch.setenv(web.MCP_LOCAL_ENV, off)
    c = TestClient(web.create_app(bot), client=("203.0.113.9", 4000))
    assert c.get("/api/meta", headers={**bearer(), "CF-Connecting-IP": "203.0.113.9"}).status_code == 200


def test_local_only_reads_the_env_each_request(monkeypatch):
    monkeypatch.delenv(web.MCP_LOCAL_ENV, raising=False)
    assert web.mcp_local_only()
    monkeypatch.setenv(web.MCP_LOCAL_ENV, "1")
    assert web.mcp_local_only()
    monkeypatch.setenv(web.MCP_LOCAL_ENV, "off")
    assert not web.mcp_local_only()
