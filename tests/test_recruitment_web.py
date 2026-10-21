"""Recruitment on the web (design §5.29): the public homepage and /api/public, the friendly not-a-member page, the
Config page's recruitment routes (officers read, the owner writes), the applications list and verdict, Create invite,
and the MCP tools' request shapes. Applications themselves happen in Discord; nothing here builds a web form."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from mcp.server.fastmcp.exceptions import ToolError
from test_mcp import TOKEN, FakeBot, bearer

from oibot_gm import mcp_server as ms
from oibot_gm.web import app as web

SECRET = "s" * 40
OFFICER_UID, MEMBER_UID, OUTSIDER_UID = 300_000_000_000_000_001, 300_000_000_000_000_002, 300_000_000_000_000_003
INVITE = "https://discord.gg/abc123"


class FakeRole:
    def __init__(self, rid, name, position):
        self.id, self.name, self.position, self.managed = rid, name, position, False

    def is_default(self):
        return False


class FakeGuild:
    def __init__(self, gid):
        self.id, self.icon = gid, None
        self.roles = [FakeRole(400_000_000_000_000_001, "Raider", 2), FakeRole(400_000_000_000_000_002, "Social", 1)]


class FakeMember:
    def __init__(self, uid, name):
        self.id, self.display_name = uid, name


class RecruitBot(FakeBot):
    """FakeBot plus a Discord guild (sessions need one), the verdict and the invite the other half of the feature adds."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.guild = FakeGuild(reg.config.discord_guild_id)
        self.people = {OFFICER_UID: FakeMember(OFFICER_UID, "Offi"), MEMBER_UID: FakeMember(MEMBER_UID, "Mem")}
        self.decided: list = []
        self.invites: list = []

    def get_guild(self, gid):
        return self.guild

    async def cached_member(self, guild, uid, max_age=300):
        return self.people.get(int(uid))

    def officiates(self, member, guild):
        return member.id == OFFICER_UID

    async def decide_application(self, reg, discord_id, accept, by, note=None):
        self.decided.append((discord_id, accept, by, note))
        a, _ = reg.decide(discord_id, accept, by, note)
        return f"{a.label}: {a.status} by {by}"

    async def create_invite(self, reg, channel_id):
        self.invites.append(channel_id)
        if channel_id == 1:
            raise ValueError("I can't create invites in #lobby: give the bot the Create Invite permission there")
        return "https://discord.gg/newcode"


@pytest.fixture
def bot(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    return RecruitBot(reg, rs)


@pytest.fixture
def client(bot):
    return TestClient(web.create_app(bot), follow_redirects=False)


def as_user(client, uid):
    client.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


# ---- the public face

def test_api_public_is_anonymous_and_holds_nothing_personal(client, reg):
    reg.set_recruitment("t", invite_url=INVITE, note="looking for healers")
    reg.config.about = "We raid twice a week."
    r = client.get("/api/public")
    assert r.status_code == 200
    d = r.json()
    assert d == {"name": "Demo Guild", "about": "We raid twice a week.", "invite_url": INVITE, "recruiting": True, "icon": None}
    text = r.text.lower()
    for m in reg.members.values():
        assert m.display_name.lower() not in text


def test_homepage_join_button_only_with_an_invite(client, reg):
    r = client.get("/")
    assert r.status_code == 200 and "Log in with Discord" in r.text and "Demo Guild" in r.text
    assert "Join our Discord" not in r.text and "discord.gg" not in r.text
    reg.set_recruitment("t", invite_url=INVITE)
    reg.config.about = "Tue & Thu <raids>"
    r = client.get("/")
    assert "Join our Discord" in r.text and f'href="{INVITE}"' in r.text
    assert "Tue &amp; Thu &lt;raids&gt;" in r.text, "about is escaped"
    assert 'name="viewport"' in r.text


def test_logged_in_member_still_goes_to_the_app(client):
    as_user(client, MEMBER_UID)
    r = client.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/app/me"


def test_non_member_gets_the_friendly_page_not_json(client, reg):
    reg.set_recruitment("t", invite_url=INVITE)
    as_user(client, OUTSIDER_UID)
    for path in ("/", "/app/me"):
        r = client.get(path)
        assert r.status_code == 403 and r.headers["content-type"].startswith("text/html"), path
        assert "not in the Demo Guild Discord yet" in r.text and "Join our Discord" in r.text and "/auth/logout" in r.text
    api = client.get("/api/meta")
    assert api.status_code == 403 and api.json()["not_member"] is True and "isn't a member" in api.json()["error"]
    reg.set_recruitment("t", invite_url="")
    r = client.get("/")
    assert "Join our Discord" not in r.text and "Ask a member" in r.text


# ---- recruitment settings

def test_recruitment_get_for_officers_post_for_the_owner(client, reg):
    h = as_user(client, MEMBER_UID)
    assert client.get("/api/recruitment").status_code == 403
    h = as_user(client, OFFICER_UID)
    d = client.get("/api/recruitment").json()
    assert d["open"] is True and d["questions"] == [] and d["owner"] is False and d["questions_max"] == 15
    assert [r["name"] for r in d["guild_roles"]] == ["Raider", "Social"]
    r = client.post("/api/admin/recruitment", json={"open": False}, headers=h)
    assert r.status_code == 403 and reg.config.recruitment.open is True


def test_owner_saves_everything_in_one_write_and_sees_refusals(client, reg):
    client.cookies.clear()
    body = {"open": False, "note": "healers please", "invite_url": INVITE, "member_role_ids": ["400000000000000001"],
            "questions": [{"label": "Why us?", "hint": "a sentence is fine", "required": True}, {"label": "Warcraft Logs link", "hint": None, "required": False}]}
    r = client.post("/api/admin/recruitment", json=body, headers=bearer())
    assert r.status_code == 200, r.text
    d = r.json()
    assert "applications closed" in d["message"] and d["open"] is False and d["invite_url"] == INVITE and d["member_role_ids"] == ["400000000000000001"]
    assert [q["label"] for q in d["questions"]] == ["Why us?", "Warcraft Logs link"] and d["questions"][0]["required"] is True
    assert reg.config.member_role_ids == [400_000_000_000_000_001]
    before = reg.config.model_dump()
    for bad, needle in (({"questions": [{"label": "x" * 46}]}, "45 characters"), ({"invite_url": "https://evil.example/x"}, "discord.gg"),
                        ({"note": "n" * 201}, "200 characters"), ({"questions": [{"label": "a"}] * 16}, "At most 15"), ({"questions": "nope"}, "list")):
        r = client.post("/api/admin/recruitment", json={**bad, "open": True}, headers=bearer())
        assert r.status_code == 400 and needle in r.json()["error"], (bad, r.text)
    assert reg.config.model_dump() == before, "a refusal saves nothing"


# ---- applications

def test_applications_list_shape_and_decide(client, bot, reg):
    reg.set_recruitment("t", questions=[{"label": "Why us?", "required": True}])
    spec = next(iter(reg.profile.classes["Priest"]))
    rid, sid = "barrow_deeps", "alt"
    reg.set_schedule(rid, sid, {"slots": "Tue 19:30, Thu 19:30", "name": "Alt run"}, "t")
    a = reg.apply(500_000_000_000_000_001, "discNewbie", "Newbie", "Priest", spec, None, None, None, None, surname="Brightwood",
                  schedules=[f"{rid}:{sid}"], answers=[{"label": "Why us?", "answer": "Friends raid here."}])
    reg.apply(500_000_000_000_000_002, "discOther", "Otherone", "Priest", spec, None, None, None, None, answers=[{"label": "Why us?", "answer": "x"}])
    reg.decide(500_000_000_000_000_002, False, "t", "full on priests")
    h = as_user(client, OFFICER_UID)
    d = client.get("/api/applications").json()
    assert d["recruiting"] is True and len(d["open"]) == 1 and len(d["decided"]) == 1
    o = d["open"][0]
    assert o["uid"] == str(a.discord_id) and o["label"] == "Newbie Brightwood" and o["cls"] == "Priest" and o["spec"] == spec and o["status"] == "open"
    assert o["answers"] == [{"label": "Why us?", "answer": "Friends raid here."}] and o["decided"] is None
    assert o["created"].endswith(("AM", "PM")) and ":" in o["created"]
    assert o["schedules"] == [f"{reg.raid_def(rid)['name']} · Alt run: Tuesdays and Thursdays 7:30 PM"], o["schedules"]
    dec = d["decided"][0]
    assert dec["status"] == "declined" and dec["note"] == "full on priests" and dec["decided"].endswith(("AM", "PM"))

    r = client.post("/api/admin/application/decide", json={"uid": str(a.discord_id), "accept": True, "note": "welcome"}, headers=h)
    assert r.status_code == 200, r.text
    assert bot.decided == [(a.discord_id, True, "Offi", "welcome")] and "accepted" in r.json()["message"]
    r = client.post("/api/admin/application/decide", json={"uid": str(a.discord_id), "accept": False}, headers=h)
    assert r.status_code == 400 and "No open application" in r.json()["error"]
    as_user(client, MEMBER_UID)
    assert client.post("/api/admin/application/decide", json={"uid": "1", "accept": True}, headers=h).status_code == 403


def test_create_invite_owner_only_with_a_clear_refusal(client, bot):
    h = as_user(client, OFFICER_UID)
    assert client.post("/api/admin/invite", json={"channel_id": "55"}, headers=h).status_code == 403
    client.cookies.clear()
    r = client.post("/api/admin/invite", json={"channel_id": "55"}, headers=bearer())
    assert r.status_code == 200 and r.json()["invite_url"] == "https://discord.gg/newcode" and bot.invites == [55]
    r = client.post("/api/admin/invite", json={"channel_id": "1"}, headers=bearer())
    assert r.status_code == 400 and "Create Invite" in r.json()["error"]


def test_setup_mixin_create_invite_needs_the_permission(reg):
    """The real SetupMixin.create_invite against stand-in channel objects."""
    import asyncio

    import discord

    from oibot_gm.discord_pool import SetupMixin

    class Inv:
        url = "https://discord.gg/zzz999"

    class Ch(discord.TextChannel):
        def __init__(self, allowed):
            self.name, self.allowed, self.calls = "lobby", allowed, []
            self.guild = type("G", (), {"id": reg.config.discord_guild_id, "me": object()})()

        def permissions_for(self, who):
            return type("P", (), {"create_instant_invite": self.allowed})()

        async def create_invite(self, **kw):
            self.calls.append(kw)
            return Inv()

    ok_ch, no_ch = Ch(True), Ch(False)
    bot = type("B", (SetupMixin,), {"get_channel": lambda self, cid: {1: ok_ch, 2: no_ch}.get(cid)})()
    assert asyncio.run(bot.create_invite(reg, 1)) == Inv.url and ok_ch.calls[0]["max_age"] == 0 and ok_ch.calls[0]["max_uses"] == 0
    with pytest.raises(ValueError, match="Create Invite"):
        asyncio.run(bot.create_invite(reg, 2))
    with pytest.raises(ValueError, match="text channel"):
        asyncio.run(bot.create_invite(reg, 3))


# ---- MCP tools

def fake_api(calls: list):
    roles = [{"id": "11", "name": "Raider"}, {"id": "12", "name": "Social"}]
    opens = [{"uid": "77", "display_name": "discNewbie", "label": "Newbie Brightwood"}, {"uid": "78", "display_name": "discNora", "label": "Nora"}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read() or b"{}") if request.method == "POST" else None
        calls.append((request.method, request.url.path, body, request.headers.get("x-requested-with")))
        p = request.url.path
        if p == "/api/recruitment":
            return httpx.Response(200, json={"open": True, "note": "", "invite_url": INVITE, "questions": [], "member_role_ids": ["11"], "questions_max": 15, "guild_roles": roles, "owner": True})
        if p == "/api/admin/recruitment":
            return httpx.Response(200, json={"message": "recruitment: applications closed"})
        if p == "/api/ops/change":  # the setup tools' one write path (design §5.43): the bot resolves role names server-side
            names = {r["name"].lower(): r["id"] for r in roles}
            ids = [str(x).lstrip("@") for x in body["changes"][0]["fields"].get("member_role_ids", [])]
            bad = [x for x in ids if not x.isdigit() and x.lower() not in names]
            if bad:
                return httpx.Response(200, json={"applied": [], "refused": [f"recruit member_roles — no role “{bad[0]}” I know of"]})
            return httpx.Response(200, json={"applied": ["recruitment: applications closed"], "refused": []})
        if p == "/api/applications":
            return httpx.Response(200, json={"open": opens, "decided": [], "recruiting": True})
        if p == "/api/admin/application/decide":
            return httpx.Response(200, json={"message": "Newbie Brightwood: accepted"})
        return httpx.Response(404, json={"error": "no such route"})

    return httpx.MockTransport(handler)


@pytest.fixture
def mocked():
    calls: list = []
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=fake_api(calls)))
    yield calls
    ms.use(None)


def test_mcp_recruitment_tools_request_shapes(mocked):
    r = ms.recruitment()
    assert r["member_roles"] == [{"id": "11", "name": "Raider"}] and r["invite_url"] == INVITE
    assert ms.set_recruitment(open=False, note="", member_roles=["@Social", "11"], questions=[{"label": "Why us?"}]) == "recruitment: applications closed"
    post = [c for c in mocked if c[1] == "/api/ops/change"][-1]  # typed changes; role names are resolved by the bot (design §5.43)
    assert post[2] == {"changes": [{"kind": "recruitment", "fields": {"open": False, "note": "", "questions": [{"label": "Why us?"}], "member_role_ids": ["Social", "11"]}}],
                       "apply": True, "confirmed": True} and post[3] == "oibot"
    with pytest.raises(ToolError, match="no role “Nobody” I know of"):
        ms.set_recruitment(member_roles=["Nobody"])
    assert ms.list_applications()["open"][0]["uid"] == "77"
    assert ms.decide_application("newbie brightwood", True, note="welcome") == "Newbie Brightwood: accepted"
    assert [c for c in mocked if c[1] == "/api/admin/application/decide"][-1][2] == {"uid": "77", "accept": True, "note": "welcome"}
    assert ms.decide_application("78", False) and mocked[-1][2] == {"uid": "78", "accept": False}
    with pytest.raises(ToolError, match="which one"):
        ms.decide_application("n", True)
    with pytest.raises(ToolError, match="no open application for Zed"):
        ms.decide_application("Zed", True)


def test_a_newcomer_applies_in_discord_before_the_me_page_takes_characters(client, bot, reg):
    """MEMBER_UID is in the server but has nothing registered: the Me page says apply first and its save refuses;
    an officer, or a newcomer holding a member role, is not held back."""
    reg.members.pop(MEMBER_UID, None)
    h = as_user(client, MEMBER_UID)
    me = client.get("/api/me", headers=h).json()
    assert me["must_apply"] is True
    row = {"label": None, "cls": "Mage", "spec": "Frost", "offspec": None, "name": "Newbie", "surname": "", "main": True}
    r = client.post("/api/me/characters", json={"rows": [row]}, headers=h)
    assert r.status_code == 400 and "Apply" in r.json()["error"] and MEMBER_UID not in reg.members
    reg.set_recruitment("t", member_role_ids=[77])
    bot.guild.get_member = lambda uid: type("M", (), {"roles": [type("R", (), {"id": 77})()]})()
    assert client.get("/api/me", headers=h).json()["must_apply"] is False
