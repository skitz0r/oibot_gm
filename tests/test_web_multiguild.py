"""Several guilds on the website (design §5.32 "Web", stage S2): which guild a request acts on (X-Oibot-Guild header >
?g= > the oibot_guild cookie > the first guild the person may open), never a silent switch on an explicit choice,
owner/officer per guild, the selector's meta, POST /api/guild, the homepage and not-a-member page with every server,
live updates scoped per guild (two guilds holding the SAME run key), raid emblems per guild, /api/ops per guild, plain
change previews per guild, deep links through login — and the single-guild site byte for byte as before."""
from __future__ import annotations

import asyncio
import hashlib
import time
import types

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer

from conftest import DUO_SERVER, GUILD, GUILD2, OFFICER_ROLE, OFFICER_ROLE2, OWNER, OWNER2, MemberDirectory, same_run
from test_mcp import FakeBot

from oibot_gm import configops, render
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.web import app as web

SECRET = "s" * 40
BOTH, DEMO_ONLY, DUO_ONLY, NEITHER, DEMO_OFFICER = (300_000_000_000_000_000 + n for n in (1, 2, 3, 4, 5))
INVITE_A, INVITE_B = "https://discord.gg/demo1", "https://discord.gg/duo2"


def person(uid, *roles):
    return types.SimpleNamespace(id=uid, display_name=f"u{str(uid)[-2:]}", roles=[types.SimpleNamespace(id=r) for r in roles])


class OpsRing:
    def __init__(self):
        self.by_key: dict[str, list] = {}
        self.lines: list = []

    def recent_for(self, key):
        return list(self.by_key.get(key, []))

    async def emit(self, cfg, level, text, exc=None, dm=True):
        self.lines.append((cfg.key, level, text))


class WebBot(FakeBot):
    """Two guilds: demo on server 1, duo on server 2, each with its own RaidStore; membership from a MemberDirectory;
    officers = a member holding that server's officer role (per server, as the real officiates)."""

    def __init__(self, duo, members, flaky=None):
        super().__init__(duo.a, duo.rs_a)
        self.registries = duo.guilds
        stores = {GUILD: duo.rs_a, GUILD2: duo.rs_b}
        self.raids = type("S", (), {"store": lambda _self, r: stores[r.key]})()
        self.ops = OpsRing()
        self.directory = MemberDirectory(members, flaky)
        self.directory.install(self)

    def officiates(self, member, guild):
        reg = self.registries.for_guild(guild.id) if guild is not None else None
        return bool(reg) and any(r.id in reg.config.officer_role_ids for r in getattr(member, "roles", []))


def default_members():
    return {(1, BOTH): person(BOTH), (DUO_SERVER, BOTH): person(BOTH), (1, DEMO_ONLY): person(DEMO_ONLY),
            (DUO_SERVER, DUO_ONLY): person(DUO_ONLY), (1, DEMO_OFFICER): person(DEMO_OFFICER, OFFICER_ROLE),
            (1, OWNER2): person(OWNER2),  # duo's owner is a plain member of demo
            (DUO_SERVER, OWNER2): person(OWNER2, OFFICER_ROLE2)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    monkeypatch.delenv(web.MCP_TOKEN_ENV, raising=False)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")  # not https: the test client keeps non-Secure cookies


@pytest.fixture
def bot(duo, env):
    duo.a.set_recruitment("t", invite_url=INVITE_A)
    duo.b.set_recruitment("t", invite_url=INVITE_B)
    return WebBot(duo, default_members())


def client_for(bot):
    return TestClient(web.create_app(bot), follow_redirects=False)


def session_cookie(uid) -> str:
    return URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()})


def as_user(c, uid, guild_cookie=None):
    c.cookies.clear()
    c.cookies.set(web.COOKIE, session_cookie(uid))
    if guild_cookie:
        c.cookies.set(web.GUILD_COOKIE, guild_cookie)
    return {"X-Requested-With": "oibot"}


def meta(c, **headers):
    return c.get("/api/meta", headers=headers)


# ---------------------------------------------------------------- which guild a request acts on

def test_precedence_header_then_query_then_cookie_then_first(bot):
    c = client_for(bot)
    as_user(c, BOTH)
    assert meta(c).json()["guild_key"] == GUILD, "no choice: the first in ordered() (Demo Guild < Duo Guild)"
    as_user(c, BOTH, guild_cookie=GUILD2)
    assert meta(c).json()["guild_key"] == GUILD2, "the cookie"
    assert c.get(f"/api/meta?g={GUILD}").json()["guild_key"] == GUILD, "?g= beats the cookie"
    r = c.get(f"/api/meta?g={GUILD2}", headers={"X-Oibot-Guild": GUILD})
    assert r.json()["guild_key"] == GUILD, "the header beats ?g="
    assert r.json()["moved"] is None


def test_an_explicit_guild_the_person_cannot_open_is_refused_never_switched(bot):
    c = client_for(bot)
    h = as_user(c, DEMO_ONLY, guild_cookie=GUILD)
    for r in (meta(c, **{"X-Oibot-Guild": GUILD2}), c.get(f"/api/meta?g={GUILD2}"),
              c.post("/api/me/dm", json={"dm": False}, headers={**h, "X-Oibot-Guild": GUILD2})):
        assert r.status_code == 403, r.text
        d = r.json()
        assert d["not_member"] is True and d["guild"] == GUILD2 and d["guilds"] == [GUILD], d
        assert "Duo Guild" in d["error"]
    unknown = meta(c, **{"X-Oibot-Guild": "nope"})
    assert unknown.status_code == 403 and unknown.json()["guild"] == "nope" and unknown.json()["guilds"] == [GUILD]


def test_a_cookie_for_a_guild_left_behind_moves_with_a_notice_and_remembers(bot):
    c = client_for(bot)
    as_user(c, DEMO_ONLY, guild_cookie=GUILD2)
    r = meta(c)
    assert r.status_code == 200 and r.json()["guild_key"] == GUILD and r.json()["moved"] == "Duo Guild"
    assert f"{web.GUILD_COOKIE}={GUILD}" in r.headers.get("set-cookie", ""), "the landing guild is remembered"
    as_user(c, DEMO_ONLY, guild_cookie="gone-guild")
    assert meta(c).json()["moved"] == "gone-guild"


def test_a_discord_hiccup_on_the_selected_guild_is_a_503_not_a_move(duo, env):
    bot = WebBot(duo, default_members(), flaky={(DUO_SERVER, BOTH)})
    c = client_for(bot)
    as_user(c, BOTH, guild_cookie=GUILD2)
    r = meta(c)
    assert r.status_code == 503 and "try again" in r.json()["error"], (r.status_code, r.text)
    assert meta(c, **{"X-Oibot-Guild": GUILD2}).status_code == 503
    as_user(c, BOTH, guild_cookie=GUILD)
    assert meta(c).json()["guild_key"] == GUILD, "the other guild answered: fine"


def test_nobody_in_either_server_gets_the_page_with_every_join_button(bot):
    c = client_for(bot)
    as_user(c, NEITHER)
    for path in ("/", "/app/me"):
        r = c.get(path)
        assert r.status_code == 403 and r.headers["content-type"].startswith("text/html"), path
        assert "Demo Guild" in r.text and "Duo Guild" in r.text and INVITE_A in r.text and INVITE_B in r.text
        assert r.text.count("Join our Discord") == 2 and "/auth/logout" in r.text and "not in any of these" in r.text
    api = meta(c)
    assert api.status_code == 403 and api.json()["not_member"] is True and api.json()["guilds"] == [] and api.json()["guild"] is None


def test_owner_and_officer_rights_are_per_guild(bot):
    c = client_for(bot)
    as_user(c, OWNER2, guild_cookie=GUILD)
    d = meta(c).json()
    assert d["guild_key"] == GUILD and d["viewer"]["officer"] is False and d["viewer"]["owner"] is False
    assert c.get("/api/ops").status_code == 403, "owning duo grants nothing in demo"
    d = meta(c, **{"X-Oibot-Guild": GUILD2}).json()
    assert d["viewer"]["owner"] is True and d["viewer"]["officer"] is True
    assert c.get("/api/ops", headers={"X-Oibot-Guild": GUILD2}).status_code == 200
    as_user(c, DEMO_OFFICER)
    assert c.get("/api/ops").status_code == 200
    assert c.get("/api/ops", headers={"X-Oibot-Guild": GUILD2}).status_code == 403, "not in duo at all"
    as_user(c, OWNER)  # demo's owner is in neither server's member list: still demo's owner, and nothing in duo
    assert meta(c).json()["viewer"]["owner"] is True and meta(c).json()["guilds"] == [{"key": GUILD, "name": "Demo Guild", "icon": None, "role": "owner"}]


def test_meta_lists_the_guilds_with_roles_and_the_profile(bot):
    c = client_for(bot)
    as_user(c, OWNER2)
    d = meta(c).json()
    assert d["guilds"] == [{"key": GUILD, "name": "Demo Guild", "icon": None, "role": "member"},
                           {"key": GUILD2, "name": "Duo Guild", "icon": None, "role": "owner"}]
    assert d["profile"] == "WoW: Forever" and d["game"] == "Forever" and d["moved"] is None
    as_user(c, DUO_ONLY)
    assert [g["key"] for g in meta(c).json()["guilds"]] == [GUILD2]


def test_post_guild_sets_the_cookie_only_for_a_guild_the_person_may_open(bot, caplog):
    c = client_for(bot)
    as_user(c, BOTH)
    assert c.post("/api/guild", json={"key": GUILD2}).status_code == 403, "CSRF marker required"
    with caplog.at_level("INFO", logger="oibot_gm.web.api"):
        r = c.post("/api/guild", json={"key": GUILD2}, headers={"X-Requested-With": "oibot", "X-Oibot-Guild": GUILD})
    assert r.status_code == 200 and r.json()["guild_key"] == GUILD2, "the body's key wins over the tab's header"
    assert any("POST /api/guild" in m and f"guild={GUILD2}" in m for m in caplog.messages), caplog.messages
    assert f"{web.GUILD_COOKIE}={GUILD2}" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"] and "Path=/" in r.headers["set-cookie"]
    assert meta(c).json()["guild_key"] == GUILD2, "remembered"
    as_user(c, DEMO_ONLY)
    r = c.post("/api/guild", json={"key": GUILD2}, headers={"X-Requested-With": "oibot"})
    assert r.status_code == 403 and r.json()["not_member"] is True and "set-cookie" not in r.headers
    assert c.post("/api/guild", json={}, headers={"X-Requested-With": "oibot"}).status_code == 400
    c.cookies.clear()
    assert c.post("/api/guild", json={"key": GUILD}, headers={"X-Requested-With": "oibot"}).status_code == 401


def test_writes_land_in_the_selected_guild_only(bot, duo):
    """Someone registered in both guilds turns DMs off with the duo header: duo's record changes, demo's doesn't; the
    ops line goes to duo."""
    for r in (duo.a, duo.b):
        r.save(r.member(BOTH, "Both", create=True), "registered")
    c = client_for(bot)
    h = as_user(c, BOTH)
    r = c.post("/api/me/dm", json={"on": False}, headers={**h, "X-Oibot-Guild": GUILD2})
    assert r.status_code == 200, r.text
    assert duo.b.members[BOTH].dm_opt_out and not duo.a.members[BOTH].dm_opt_out
    assert [k for k, _, _ in bot.ops.lines] == [GUILD2]


# ---------------------------------------------------------------- anonymous pages

def test_api_public_lists_both_guilds_anonymously(bot, duo):
    d = client_for(bot).get("/api/public").json()
    assert d == {"guilds": [
        {"name": "Demo Guild", "about": None, "invite_url": INVITE_A, "recruiting": True, "icon": None, "key": GUILD},
        {"name": "Duo Guild", "about": None, "invite_url": INVITE_B, "recruiting": True, "icon": None, "key": GUILD2}]}
    text = client_for(bot).get("/api/public").text.lower()
    for m in [*duo.a.members.values(), *duo.b.members.values()]:
        assert m.display_name.lower() not in text


def test_homepage_shows_both_servers_side_by_side(bot):
    r = client_for(bot).get("/")
    assert r.status_code == 200
    t = r.text
    assert t.index("Demo Guild") < t.index("Duo Guild") and t.count('class="card"') == 2
    assert f'href="{INVITE_A}"' in t and f'href="{INVITE_B}"' in t
    assert f"/auth/login?next=/app/me%3Fg%3D{GUILD}" in t and f"/auth/login?next=/app/me%3Fg%3D{GUILD2}" in t
    assert "grid-template-columns" in t and 'name="viewport"' in t


def test_homepage_login_link_round_trips_the_guild(bot, monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_ID", "123")
    c = client_for(bot)
    r = c.get(f"/auth/login?next=/app/me%3Fg%3D{GUILD2}", headers={"user-agent": "Mozilla/5.0 (Macintosh)"})
    state = r.headers["location"].split("state=")[1].split("&")[0]
    from urllib.parse import unquote

    assert URLSafeSerializer(SECRET, salt="session").loads(unquote(state))["next"] == f"/app/me?g={GUILD2}"


def test_deep_link_keeps_its_guild_through_login(bot):
    c = client_for(bot)
    r = c.get(f"/app/rosters?g={GUILD2}")
    assert r.status_code == 307 and r.headers["location"] == f"/auth/login?next=/app/rosters%3Fg%3D{GUILD2}"
    assert c.get("/app/me").headers["location"] == "/auth/login?next=/app/me", "no query: the link as before"


def test_deep_link_selects_its_guild_or_opens_yours(bot):
    c = client_for(bot)
    as_user(c, BOTH)
    r = c.get(f"/app/rosters?g={GUILD2}")
    assert r.status_code == 200 and f"{web.GUILD_COOKIE}={GUILD2}" in r.headers.get("set-cookie", "")
    as_user(c, DEMO_ONLY)
    r = c.get(f"/app/rosters?g={GUILD2}")
    assert r.status_code == 200 and "set-cookie" not in r.headers, "a link to someone else's server opens yours (the app says so)"
    as_user(c, NEITHER)
    assert c.get(f"/app/rosters?g={GUILD2}").status_code == 403


# ---------------------------------------------------------------- live updates, images, ops, plain change

def test_live_updates_reach_only_their_guild_even_with_one_run_key(bot, duo):
    """Two event streams (demo and duo) open at once; saving the run whose key both guilds hold in duo wakes duo's
    page only; ops and member topics are scoped; a process-wide publish reaches both."""
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    app = web.create_app(bot)

    async def scenario():
        bot.loop = asyncio.get_running_loop()
        got = {GUILD: [], GUILD2: []}
        gone = {GUILD: asyncio.Event(), GUILD2: asyncio.Event()}

        async def stream(key):
            sent = False
            cookie = f"{web.COOKIE}={session_cookie(BOTH)}".encode()

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await gone[key].wait()
                return {"type": "http.disconnect"}

            async def send(msg):
                if msg["type"] == "http.response.body" and msg.get("body", b"").startswith(b"data: "):
                    got[key].append(msg["body"].decode().split("data: ", 1)[1].strip())

            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                     "path": "/api/events", "raw_path": b"/api/events", "query_string": f"g={key}".encode(), "root_path": "",
                     "headers": [(b"host", b"testserver"), (b"cookie", cookie)], "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
            await app(scope, receive, send)

        tasks = [asyncio.create_task(stream(k)) for k in (GUILD, GUILD2)]
        await asyncio.sleep(0.3)
        await asyncio.to_thread(duo.rs_b.save, ev_b, "touch duo's run")
        await bot.ops.emit(duo.b.config, "info", "x")  # FakeOps doesn't fan out; call the listeners as Ops.emit does
        from oibot_gm import ops as ops_mod, registry as registry_mod

        for fn in ops_mod.LISTENERS:
            fn(GUILD2)
        for fn in registry_mod.SAVE_LISTENERS:
            fn(duo.a, "member", [])
        app.state.publish("agents")
        await asyncio.sleep(0.3)
        for k in gone:
            gone[k].set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 25)
        return got

    got = asyncio.run(scenario())
    assert got[GUILD2] == [f"run:{ev_b.key}", "ops", "agents"], got
    assert got[GUILD] == ["member", "agents"], got


def test_raid_emblem_reads_the_named_guilds_overrides(bot, duo, monkeypatch):
    rid = next(iter(duo.b.profile.raids))
    default = int(duo.b.raid_def(rid).get("lockout_days") or 7)
    duo.b.set_raid_override(rid, "lockout_days", default + 3, "t")
    seen = []
    monkeypatch.setattr(render, "raid_thumb_png", lambda r, name, size, lockout: seen.append((r, lockout)) or b"png")
    c = client_for(bot)
    assert c.get(f"/img/raid/{rid}.png?g={GUILD2}").status_code == 200
    assert c.get(f"/img/raid/{rid}.png?g={GUILD}").status_code == 200
    assert c.get(f"/img/raid/{rid}.png").status_code == 200, "no guild named: the profile defaults (the same picture as demo's: cached)"
    assert seen == [(rid, default + 3), (rid, default)], seen
    c.cookies.set(web.GUILD_COOKIE, GUILD2)
    assert c.get(f"/img/raid/{rid}.png").status_code == 200 and len(seen) == 2, "the cookie → duo's (cached)"
    assert c.get(f"/img/raid/nope.png?g={GUILD2}").status_code == 404


def test_api_ops_lists_only_the_selected_guilds_lines(bot):
    bot.ops.by_key = {GUILD: [("t", "info", "demo line")], GUILD2: [("t", "info", "duo line")]}
    c = client_for(bot)
    as_user(c, OWNER2)
    d = c.get("/api/ops", headers={"X-Oibot-Guild": GUILD2}).json()
    assert [r["text"] for r in d["rows"]] == ["duo line"] and d["scope"] == "bot"


def test_a_plain_change_preview_from_one_guild_cannot_be_applied_in_the_other(bot, duo, monkeypatch):
    bot.ctx = types.SimpleNamespace(provider=object())
    c = client_for(bot)
    h = as_user(c, BOTH)
    bot.directory.members[(1, BOTH)] = person(BOTH, OFFICER_ROLE)
    bot.directory.members[(DUO_SERVER, BOTH)] = person(BOTH, OFFICER_ROLE2)
    who = duo.a.test_members()[0].display_name
    monkeypatch.setattr(configops, "parse", lambda *a, **k: ConfigRequest(kind="change", reply="ok", ops=[ConfigOp(op="dm", member=who, value="off")]))
    d = c.post("/api/ops/change", json={"text": "dm off"}, headers={**h, "X-Oibot-Guild": GUILD}).json()
    assert d.get("token"), d
    r = c.post("/api/ops/change", json={"token": d["token"], "apply": True}, headers={**h, "X-Oibot-Guild": GUILD2})
    assert r.status_code == 400 and "token" in r.json()["error"]


def test_discord_links_and_emblems_name_their_guild(duo, monkeypatch):
    from oibot_gm import discord_pool, raid_views

    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    assert raid_views.board_url(duo.b, ev_b) == f"https://gm.example/app/rosters?g={GUILD2}#run-{ev_b.key}"
    assert raid_views.raid_thumb_url(duo.a, ev_a.instance) == f"https://gm.example/img/raid/{ev_a.instance}.png?g={GUILD}"
    assert discord_pool._site(duo.b, "/app/members") == f"https://gm.example/app/members?g={GUILD2}"
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    assert raid_views.raid_thumb_url(duo.a, ev_a.instance) is None and discord_pool._site(duo.a, "/x") is None


# ---------------------------------------------------------------- one guild: as before

def test_single_guild_pages_are_byte_for_byte_the_old_ones():
    info = {"name": "Demo <Guild>", "about": "Tue & Thu", "invite_url": "https://discord.gg/x?a=1&b=2", "recruiting": True, "icon": "https://cdn/x.png"}
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731 — goldens from the pre-S2 functions
    assert sha(web.landing_page([info])) == sha(web.landing_page([{**info, "key": "demo"}])) == "6035d0443af9d9690aedd9e524fa53bf32b41020810919cc7e818f4210e3931a"
    assert sha(web.not_member_page([info])) == "112a47be51ec067f3bbc680d32bbe8894af5103c5d594fde42db13ec727b7d8f"
    assert sha(web.landing_page([])) == "8cd8b72c40ea38b366668ee9a8da08ed393750092d9d5ef20ae9322cdcf59955", "no guild yet: the generic card (K22)"


def test_single_guild_needs_no_header_or_cookie_and_has_one_choice(reg, rs, env):
    bot = FakeBot(reg, rs)
    MemberDirectory({(1, BOTH): person(BOTH)}, None).install(bot, servers=(1,))
    c = client_for(bot)
    as_user(c, BOTH)
    d = meta(c).json()
    assert d["guild_key"] == GUILD and d["moved"] is None and d["guilds"] == [{"key": GUILD, "name": "Demo Guild", "icon": None, "role": "member"}]
    assert set(client_for(bot).get("/api/public").json()) == {"name", "about", "invite_url", "recruiting", "icon"}, "K13: the old shape"
    as_user(c, NEITHER)
    assert meta(c).status_code == 403 and "Demo Guild" in meta(c).json()["error"]
