"""OIBOT_SUPERUSERS: the host's superusers have owner rights in every guild — owner-only plain text, the site (even for a
server they aren't in), and setting up an unconfigured server; nobody else gains anything, and a server claim defaults
the guild's owner to the Discord server's owner."""
from types import SimpleNamespace

import pytest

from oibot_gm import configops
from oibot_gm.configops import ConfigOp

SU = 424242424242424242


@pytest.fixture
def su(monkeypatch):
    monkeypatch.setenv("OIBOT_SUPERUSERS", f"{SU}, 5")
    return SU


def test_owner_rights_for_the_owner_and_superusers_only(reg, su, monkeypatch):
    owner = reg.config.owner_discord_id or 1
    reg.config.owner_discord_id = owner
    assert reg.is_owner_id(owner) and reg.is_owner_id(su) and not reg.is_owner_id(999) and not reg.is_owner_id(None)
    monkeypatch.delenv("OIBOT_SUPERUSERS")
    assert not reg.is_owner_id(su)


def test_superuser_passes_the_hard_owner_gate(reg, su):
    reg.config.owner_discord_id = 1
    op = ConfigOp(op="plain", target="dm", value="answer")
    allowed, refused = configops.authorize(reg, [op.model_copy()], su, officer=False)
    assert allowed and not refused
    allowed, refused = configops.authorize(reg, [op.model_copy()], 999, officer=True)
    assert not allowed and refused


def _claim_itx(uid, server_owner=1, manage=False):
    regs = SimpleNamespace(for_guild=lambda g: None, claim_refusal=lambda g: None)
    return SimpleNamespace(guild_id=77, guild=SimpleNamespace(owner_id=server_owner, owner=None, name="New"), client=SimpleNamespace(registries=regs),
                           user=SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(manage_guild=manage)))


def test_superuser_may_set_up_any_server_others_may_not(su):
    from oibot_gm.flows_config import claim_refusal
    assert claim_refusal(_claim_itx(su)) is None
    assert claim_refusal(_claim_itx(999)) is not None  # not the server owner, no Manage Server, not a superuser
    assert claim_refusal(_claim_itx(1)) is None  # the Discord server owner


def test_site_opens_a_guild_for_a_superuser_who_is_not_in_the_server(reg, rs, su, monkeypatch):
    from test_recruitment_web import RecruitBot, as_user, SECRET, TOKEN  # the web app on a fake bot with sessions
    from fastapi.testclient import TestClient
    from oibot_gm.web import app as web
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    c = TestClient(web.create_app(RecruitBot(reg, rs)), follow_redirects=False)
    h = as_user(c, su)
    r = c.get("/api/meta", headers=h)
    assert r.status_code == 200 and r.json()["viewer"]["owner"] is True
    h = as_user(c, 987654321987654321)  # a stranger is still refused
    assert c.get("/api/meta", headers=h).status_code == 403
