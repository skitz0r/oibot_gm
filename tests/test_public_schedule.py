"""The public side (dual interfaces phase 4 — design §5.49, B10 / PUB-01 / PUB-05): the homepage with no invite says how to
get in ("Invitations are closed — ask an officer in game"), "schedules" not "availability"; GuildConfig.public = the
unlisted flag (an unlisted guild leaves the homepage cards, the not-a-member page, GET /api/public, /schedule and a
non-member's guild list — its members still log in); the public /schedule page: the next three runs per schedule in
words, the raid name and size, nothing about members, no login."""
from __future__ import annotations

import re

import pytest
from conftest import GUILD, GUILD2
from fastapi.testclient import TestClient
from test_mcp import FakeBot
from test_web_multiguild import BOTH, DEMO_ONLY, DUO_ONLY, INVITE_A, WebBot, as_user, client_for, default_members, env  # noqa: F401 — env is a fixture

from oibot_gm import configops
from oibot_gm.configops import ConfigOp
from oibot_gm.web import app as web

AMPM = re.compile(r"\b\d{1,2}:\d{2} (AM|PM)\b")
H24 = re.compile(r"\b([01]\d|2[0-3]):[0-5]\d\b(?! (AM|PM))")


@pytest.fixture
def one(reg, rs, env):  # noqa: F811
    reg.set_schedule("barrow_deeps", "fri", {"kind": "weekly", "slots": "Fri 20:00", "name": "Fridays"}, "t")
    bot = FakeBot(reg, rs)
    return bot, TestClient(web.create_app(bot), follow_redirects=False)


@pytest.fixture
def two(duo, env):  # noqa: F811
    duo.a.set_recruitment("t", invite_url=INVITE_A)
    bot = WebBot(duo, default_members())
    return bot, client_for(bot), duo


def member_words(reg) -> list[str]:
    return [m.display_name for m in reg.members.values()] + [c.label for m in reg.members.values() for c in m.characters if c.label]


# ---------------------------------------------------------------- the public /schedule page

def test_the_schedule_page_is_words_only_for_anyone_without_a_login(one, reg):
    bot, c = one
    c.cookies.clear()
    r = c.get("/schedule")
    assert r.status_code == 200 and "login" not in r.url.path
    body = r.text
    name = reg.raid_def("barrow_deeps")["name"]
    assert name in body and f"{int(reg.raid_def('barrow_deeps')['size'])}-player" in body and "next:" in body and "Fridays" in body
    nxt = body.split("next:", 1)[1].split("</span>", 1)[0]
    assert len(AMPM.findall(nxt)) == 3, "the next three runs, 12-hour (the clock rule)"
    assert not H24.search(re.sub(r"<[^>]+>", " ", body).replace("fonts.googleapis.com/css2?family=Manrope:wght@700", "")), "no 24-hour time a person reads"
    for word in member_words(reg):
        assert f">{word}<" not in body and f" {word} " not in body, f"{word!r}: nothing about members on the public page"
    assert "regular" not in body.lower() and "signed" not in body.lower()
    assert c.get(f"/schedule?g={GUILD}").text == body, "?g=<key> names the same guild"
    assert c.get("/schedule?g=nope").status_code == 404


def test_paused_and_ad_hoc_schedules_read_in_words(one, reg):
    bot, c = one
    reg.set_schedule("barrow_deeps", "pug", {"kind": "pickup", "name": "Pickups"}, "t")
    body = c.get("/schedule").text
    assert "Pickups" in body and "first come" in body


def test_an_unlisted_guild_has_no_schedule_page_no_homepage_card_and_no_public_facts(one, reg):
    bot, c = one
    reg.config.public = False
    reg.save_config("t: unlisted")
    c.cookies.clear()
    assert c.get(f"/schedule?g={GUILD}").status_code == 404 and c.get("/schedule").status_code == 404
    home = c.get("/").text
    assert reg.config.name not in home and "oibot_GM" in home, "the homepage names no unlisted guild"
    assert c.get("/api/public").json()["name"] == "oibot_GM"


def test_several_guilds_list_only_the_listed_ones_everywhere(two):
    bot, c, duo = two
    duo.b.config.public = False
    duo.b.save_config("t: unlisted")
    c.cookies.clear()
    home = c.get("/").text
    assert duo.a.config.name in home and duo.b.config.name not in home
    pub = c.get("/api/public").json()
    assert pub["name"] == duo.a.config.name and "guilds" not in pub, "one listed guild: the one-guild shape"
    assert c.get(f"/schedule?g={GUILD2}").status_code == 404 and c.get("/schedule").status_code == 200
    # a non-member of duo, logged in: the not-a-member page and their own guild list never name it
    as_user(c, DEMO_ONLY)
    assert [g["key"] for g in c.get("/api/guilds").json()["guilds"]] == [GUILD]
    # duo's own member still opens it (unlisted is not closed)
    as_user(c, DUO_ONLY)
    assert [g["key"] for g in c.get("/api/guilds").json()["guilds"]] == [GUILD2]
    assert c.get("/api/meta", headers={"X-Oibot-Guild": GUILD2}).status_code == 200
    as_user(c, 300_000_000_000_000_999)  # in neither server
    page = c.get("/app/me")
    assert duo.b.config.name not in page.text


def test_two_listed_guilds_get_a_schedule_index(two):
    bot, c, duo = two
    c.cookies.clear()
    idx = c.get("/schedule").text
    assert f"/schedule?g={GUILD}" in idx and f"/schedule?g={GUILD2}" in idx


# ---------------------------------------------------------------- homepage copy (B10)

def test_homepage_without_an_invite_says_invitations_are_closed_and_says_schedules(one, reg):
    bot, c = one
    c.cookies.clear()
    home = c.get("/").text
    assert reg.config.invite_url is None and web.INVITES_CLOSED in home
    assert "schedules" in home and "availability" not in home
    assert f'href="/schedule?g={GUILD}"' in home, "the card links its public schedule"
    reg.set_recruitment("t", invite_url="https://discord.gg/abc123")
    home = c.get("/").text
    assert web.INVITES_CLOSED not in home and "Join our Discord" in home
    assert web.INVITES_CLOSED in web.not_member_page([{"name": "G", "about": None, "invite_url": None, "recruiting": False, "icon": None}])


# ---------------------------------------------------------------- the owner's switch (one `set` path, every surface)

def test_the_public_flag_is_an_owner_setting_on_the_structured_path(reg):
    ops = configops.ops_from_fields(reg, "settings", None, {"public": False})
    assert [(o.op, o.path, o.value) for o in ops] == [("set", "public", "false")]
    d = configops.describe_ops(reg, ops)
    (line,) = d.lines
    assert line.parts[0].before == "listed" and line.parts[0].after == "unlisted" and line.parts[0].label == "Listed on the homepage"
    assert "set" in configops.OWNER_OPS, "every `set` is the owner's"
    configops.apply(reg, ConfigOp(op="set", path="public", value="false"), "Owner", True)
    assert reg.config.public is False
    configops.apply(reg, ConfigOp(op="set", path="public", value="true"), "Owner", True)
    assert reg.config.public is True
