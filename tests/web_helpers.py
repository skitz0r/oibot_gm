"""Shared helpers for the web tests: the reviewed POST and the plain-permissions web client (one fake bot, one cookie format)."""
import time
import types

import pytest
from conftest import OFFICER_ROLE, OWNER

RAID_LEAD = 300_000_000_000_000_007  # a Discord role the owner can grant groups to


def post_confirmed(client, url: str, json: dict, **kw):
    """POST to an admin translator route the way a reviewed Save does (D51): a warn-class or consequence-bearing batch
    first answers `review_required` with the section revs; the second POST sends `confirmed: true` with those revs."""
    r = client.post(url, json=json, **kw)
    if r.status_code == 200 and r.json().get("review_required"):
        r = client.post(url, json={**json, "confirmed": True, "revs": {**(json.get("revs") or {}), **r.json()["revs"]}}, **kw)
    return r


# ---- the plain-permissions web client (test_plain_permissions, test_chat_memory)

class Guild:
    def __init__(self):
        self.id = 1
        self.roles = [types.SimpleNamespace(id=RAID_LEAD, name="Raid Lead", position=2, is_default=lambda: False, managed=False)]


@pytest.fixture
def web_client(g, rs, monkeypatch, request):
    """A TestClient over the app with a fake bot: `as_(uid)` sets a signed session cookie, `token` is the MCP bearer. The
    channels it lists are the requesting test module's own OPS_CH and GENERAL_CH; `g` is that module's guild fixture."""
    from fastapi.testclient import TestClient
    from itsdangerous import URLSafeSerializer
    from test_mcp import TOKEN, FakeBot

    from oibot_gm.web import app as web

    OPS_CH, GENERAL_CH = request.module.OPS_CH, request.module.GENERAL_CH
    officer_id = g.other.discord_id
    guild = Guild()

    class Bot(FakeBot):
        def get_guild(self, gid):
            return guild

        async def cached_member(self, guild, uid, max_age=300):
            return types.SimpleNamespace(id=uid, display_name=f"u{uid}", roles=[types.SimpleNamespace(id=OFFICER_ROLE)] if uid == officer_id else [])

        def officiates(self, member, guild):
            return member.id in (officer_id, OWNER)

        def guild_channels(self, reg):
            return [{"id": str(OPS_CH), "name": "ops", "category": None}, {"id": str(GENERAL_CH), "name": "general", "category": None}]

    monkeypatch.setenv("OIBOT_WEB_SECRET", "s" * 40)
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    sign = URLSafeSerializer("s" * 40, salt="session")
    bot = Bot(g.reg, rs)
    c = TestClient(web.create_app(bot))

    def as_(uid):
        c.cookies.set(web.COOKIE, sign.dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
        return {"X-Requested-With": "oibot"}

    return types.SimpleNamespace(c=c, as_=as_, officer=officer_id, member=g.me.discord_id, bot=bot, token=TOKEN)
