"""Companion tokens (dual interfaces phase 4, W3 — design §5.50; out/dual_interface_design.md §3.1 L08/S-03): a guild's
owner issues a token for a raid leader's loot companion; guild.yaml keeps only its HASH (`feed_tokens[]`); the value is
shown exactly once (DMed by /gm config companion, or in the one web response) and never reaches a file, the request log
or an ops line; FeedServer.auth takes it beside the .env tokens and binds the companion to the token's guild; a revoke
refuses the companion at its next frame without a restart. Surfaces: the Discord hub (harness), /api/companions (owner
only; MCP may list and revoke, never issue) and the MCP tools."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import types
from pathlib import Path

import discord
import httpx
import pytest
from conftest import OFFICER_ROLE, OWNER
from fastapi.testclient import TestClient
from test_loot_web import OFFICER, TOKEN, WebLootBot, as_user, env  # noqa: F401 — env is a fixture
from test_parity_matrix import discord_path_exists, slash_paths, web_routes  # noqa: F401 — fixtures
from wizard_harness import Harness

from oibot_gm import configops, guild_routing
from oibot_gm import flows_config  # noqa: F401 — registers the flows
from oibot_gm import mcp_server as ms
from oibot_gm.discord_feed import FeedMixin
from oibot_gm.discord_pool import SetupMixin
from oibot_gm.feed import BAD_TOKEN, REVOKED, FeedServer, authorize_any, hash_token, token_rows
from oibot_gm.registry import FEED_LABEL_MAX, FEED_TOKEN_MAX, RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS

MCP = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}


def run(coro):
    return asyncio.run(coro)


def files_holding(root: Path, needle: str) -> list[str]:
    """Every file under the data repo (its working tree) whose bytes contain `needle`, plus 'git history' when any
    commit's diff or message does — the plaintext must be in neither."""
    hits = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and needle.encode() in p.read_bytes()]
    log = subprocess.run(["git", "log", "-p", "--all"], cwd=root, capture_output=True, text=True).stdout
    return hits + (["git history"] if needle in log else [])


# ---------------------------------------------------------------- Registry: hashed, shown once, revocable

def test_issue_stores_only_the_hash_and_the_value_is_in_no_file(reg, store):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner", OWNER)
    assert token.startswith("oibot-feed-") and len(token) > 40
    assert rec.hash == hash_token(token) and rec.hash != token and token not in rec.hash
    assert rec.id.startswith("ft-") and rec.label == "Jon's PC" and rec.issued_by == "Owner" and rec.active
    assert not files_holding(store.root, token), "the plaintext must never be written anywhere"
    guild = (store.root / reg.key / "guild.yaml").read_text()
    assert rec.hash in guild and rec.id in guild and "Jon's PC" in guild
    last = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=store.root, capture_output=True, text=True).stdout
    assert rec.id in last and "Jon's PC" in last, "the commit names the label and the id"
    reread = type(reg)(reg.store, reg.key, reg.base_profile)
    assert reread.match_feed_token(token).id == rec.id, "a restart reads the hash back"


def test_two_issues_never_share_a_value_or_an_id(reg):
    a, ra = reg.issue_feed_token("A", "Owner")
    b, rb = reg.issue_feed_token("B", "Owner")
    assert a != b and ra.id != rb.id and ra.hash != rb.hash
    assert reg.match_feed_token(a).id == ra.id and reg.match_feed_token(b).id == rb.id
    assert reg.match_feed_token("oibot-feed-guess") is None and reg.match_feed_token("") is None


@pytest.mark.parametrize("label, why", [("", "Give the token a label"), ("   ", "Give the token a label"), ("x" * (FEED_LABEL_MAX + 1), "at most")])
def test_a_label_is_checked(reg, label, why):
    with pytest.raises(RegistryError, match=why):
        reg.issue_feed_token(label, "Owner")
    assert reg.config.feed_tokens == []


def test_a_label_in_use_and_the_cap_are_refused(reg):
    reg.issue_feed_token("Jon's PC", "Owner")
    with pytest.raises(RegistryError, match="already in use"):
        reg.issue_feed_token("jon's   pc", "Owner")
    for n in range(FEED_TOKEN_MAX - 1):
        reg.issue_feed_token(f"PC {n}", "Owner")
    with pytest.raises(RegistryError, match="revoke one"):
        reg.issue_feed_token("one more", "Owner")
    reg.revoke_feed_token("PC 0", "Owner")
    reg.issue_feed_token("one more", "Owner")  # a revoke frees a place


def test_revoke_by_id_or_label_and_refusals(reg):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    with pytest.raises(RegistryError, match="No companion token"):
        reg.revoke_feed_token("nope", "Owner")
    out = reg.revoke_feed_token("JON'S PC", "Owner")
    assert out.id == rec.id and out.revoked_at and out.revoked_by == "Owner"
    assert reg.match_feed_token(token) is None and not reg.feed_token_active(rec.id)
    with pytest.raises(RegistryError, match="already revoked"):
        reg.revoke_feed_token(rec.id, "Owner")
    again, rec2 = reg.issue_feed_token("Jon's PC", "Owner")  # the label is free again; the old record stays
    assert reg.feed_token("Jon's PC").id == rec2.id and len(reg.feed_tokens()) == 2 and len(reg.feed_tokens(include_revoked=False)) == 1


def test_tokens_never_move_the_settings_rev(reg):
    """feed_tokens sit outside every section rev: a companion's last_seen (or an issue) must never 409 an owner's edit."""
    rev = reg.config_rev("settings")
    _, rec = reg.issue_feed_token("Jon's PC", "Owner")
    reg.touch_feed_token(rec.id)
    reg.revoke_feed_token(rec.id, "Owner")
    assert reg.config_rev("settings") == rev


def test_last_seen_is_saved_at_most_every_window(reg, store):
    _, rec = reg.issue_feed_token("Jon's PC", "Owner")
    count = lambda: int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout)  # noqa: E731
    n0 = count()
    assert reg.touch_feed_token(rec.id) is True and count() == n0 + 1, "the first sighting is written"
    first = reg.feed_token(rec.id).last_seen
    assert reg.touch_feed_token(rec.id) is False and count() == n0 + 1, "then at most every 15 minutes"
    assert reg.feed_token(rec.id).last_seen >= first, "in memory it moves on every frame"
    assert reg.touch_feed_token(rec.id, save_after=0) is True, "past the window it is written again"
    assert reg.touch_feed_token("ft-none") is False


def test_hub_rows_carry_presence_and_never_a_value(reg):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    _, old = reg.issue_feed_token("Old PC", "Owner")
    reg.revoke_feed_token(old.id, "Owner")
    pres = [{"character": "Rhozy", "state": "live", "events": 3, "last_seen": "2026-10-22T03:00:00+00:00", "token_id": rec.id}]
    rows = {r["label"]: r for r in token_rows(reg, pres)}
    assert rows["Jon's PC"]["state"] == "live" and rows["Jon's PC"]["characters"] == ["Rhozy"] and rows["Jon's PC"]["events"] == 3
    assert "cut off at its next message" in rows["Jon's PC"]["revoke_consequence"]
    assert re.search(r"\d:\d\d [AP]M$", rows["Jon's PC"]["last_seen_label"]), "12-hour guild time"
    assert rows["Old PC"]["state"] == "revoked" and rows["Old PC"]["revoke_consequence"] is None
    assert [r["label"] for r in token_rows(reg, [])] == ["Old PC", "Jon's PC"], "newest first"
    assert {r["label"]: r["state"] for r in token_rows(reg, [])}["Jon's PC"] == "never"
    text = repr(token_rows(reg, pres))
    assert token not in text and rec.hash not in text


# ---------------------------------------------------------------- the feed: auth by hash, bound to the guild, revoked at the next frame

class FeedBot(FeedMixin):
    def __init__(self, reg):
        from oibot_gm.discord_registry import Guilds

        self.registries = Guilds.of(reg.store, reg)


def feed_server(reg, seen: list):
    bot = FeedBot(reg)

    async def handler(ev, c):
        seen.append((ev.get("type"), c.guild, c.bound, c.token_id, c.label))
        return {"routes_to": None} if ev.get("type") == "hello" else {}

    fs = FeedServer(handler, "shared-env", auth=lambda t: authorize_any(t, "shared-env", {"DEMO": "demo-env"}, lambda: [reg.key], bot.feed_issued))
    fs.still_valid, fs.on_seen = bot.feed_token_valid, bot.feed_seen
    return fs


async def session(fs, script):
    """Connect, send `script` frames one by one and collect each reply (None once the socket closed)."""
    from aiohttp import WSMsgType, web as aioweb
    from aiohttp.test_utils import TestClient as AioClient, TestServer

    app = aioweb.Application()
    app.router.add_get("/feed", fs._ws)
    out = []
    async with AioClient(TestServer(app)) as cl:
        ws = await cl.ws_connect("/feed")
        for frame in script:
            if callable(frame):
                frame()
                continue
            await ws.send_json(frame)
            msg = await ws.receive()
            out.append(json.loads(msg.data) if msg.type == WSMsgType.TEXT else None)
        await ws.close()
    return out


def test_a_guild_token_binds_the_companion_to_its_guild(reg):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    seen: list = []
    fs = feed_server(reg, seen)
    out = run(session(fs, [{"type": "hello", "token": token, "character": "Rhozy", "guild": "elsewhere"}, {"type": "heartbeat", "idem": "h1"}]))
    assert out[0]["type"] == "welcome" and out[1]["type"] == "ack"
    assert seen[0] == ("hello", reg.key, reg.key, rec.id, "Jon's PC"), "the token decides the guild, whatever the hello named"
    assert reg.feed_token(rec.id).last_seen, "last_seen moves with the frames"
    rows = fs.presence(reg.key)
    assert rows and rows[0]["token_id"] == rec.id and rows[0]["label"] == "Jon's PC"


def test_a_revoked_token_is_refused_at_the_next_frame_without_a_restart(reg):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    seen: list = []
    fs = feed_server(reg, seen)
    out = run(session(fs, [{"type": "hello", "token": token, "character": "Rhozy"}, {"type": "heartbeat", "idem": "h1"},
                           lambda: reg.revoke_feed_token(rec.id, "Owner"),
                           {"type": "drop", "boss": "X", "items": [], "idem": "d1"}]))
    assert out[0]["type"] == "welcome" and out[1]["type"] == "ack"
    assert out[2] == {"type": "error", "auth": True, "idem": "d1", "message": REVOKED}
    assert [s[0] for s in seen] == ["hello", "heartbeat"], "the frame after the revoke never reaches the handler"
    again = run(session(fs, [{"type": "hello", "token": token, "character": "Rhozy"}]))
    assert again[0] == {"type": "error", "auth": True, "message": BAD_TOKEN}, "and it can't say hello again"


def test_a_revoke_closes_a_live_socket_now(reg):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    fs = feed_server(reg, [])

    async def go():
        from aiohttp import web as aioweb
        from aiohttp.test_utils import TestClient as AioClient, TestServer

        app = aioweb.Application()
        app.router.add_get("/feed", fs._ws)
        async with AioClient(TestServer(app)) as cl:
            ws = await cl.ws_connect("/feed")
            await ws.send_json({"type": "hello", "token": token, "character": "Rhozy"})
            await ws.receive_json()
            reg.revoke_feed_token(rec.id, "Owner")
            closed = await fs.close_token(rec.id)
            msg = await ws.receive_json()
            return closed, msg
    closed, msg = run(go())
    assert closed == 1 and msg["auth"] is True and msg["message"] == REVOKED


def test_the_env_tokens_still_work(reg):
    reg.issue_feed_token("Jon's PC", "Owner")
    seen: list = []
    fs = feed_server(reg, seen)
    for tok in ("demo-env", "shared-env"):
        out = run(session(fs, [{"type": "hello", "token": tok, "character": "Rhozy"}, {"type": "heartbeat", "idem": "h"}]))
        assert [o["type"] for o in out] == ["welcome", "ack"], tok
    assert seen[0][2:4] == (reg.key, None) and seen[2][2:4] == (None, None), "the guild's .env token binds; the shared one binds none"
    assert run(session(fs, [{"type": "hello", "token": "wrong", "character": "X"}]))[0]["auth"] is True


def test_authorize_any_order():
    keys = lambda: ["demo"]  # noqa: E731
    issued = lambda t: ("demo", "ft-1", "PC") if t == "issued" else None  # noqa: E731
    assert authorize_any("env", "shared", {"DEMO": "env"}, keys, issued) == (True, "demo", None, None)
    assert authorize_any("issued", "shared", {}, keys, issued) == (True, "demo", "ft-1", "PC")
    assert authorize_any("shared", "shared", {}, keys, issued) == (True, None, None, None)
    assert authorize_any("nope", "shared", {}, keys, issued) == (False, None, None, None)
    assert authorize_any("", None, {}, keys, issued) == (False, None, None, None)


def test_a_failing_check_refuses_rather_than_lets_through():
    """A lookup that throws never lets the frame through — and is answered as 'can't tell now' (None), not as a revoke."""
    fs = FeedServer(lambda ev, c: None, None)

    def boom(c):
        raise RuntimeError("lookup failed")
    fs.still_valid = boom
    assert fs.valid(types.SimpleNamespace(token_id="ft-1")) is None and fs.valid(types.SimpleNamespace(token_id=None)) is True


def test_a_gone_guilds_token_feeds_nothing(reg):
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    bot = FeedBot(reg)
    assert bot.feed_issued(token) == (reg.key, rec.id, "Jon's PC")
    bot.registries = types.SimpleNamespace(ordered=lambda: [], by_key={}, detached={}, load_errors=[])
    assert bot.feed_issued(token) is None and bot.feed_token_valid(types.SimpleNamespace(bound=reg.key, token_id=rec.id)) is False


# ---------------------------------------------------------------- auth:true only for a genuine no-match or revoke (fixer finding 3)

def test_a_parked_guilds_live_token_is_unverifiable_not_dead(reg):
    """The guild is parked (or not served yet): its unrevoked token feeds nothing, but the companion is told 'can't check
    now' (it retries) — not that its token is dead. Once revoked, it IS dead."""
    from oibot_gm.feed import TokenUnverifiable

    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    bot = FeedBot(reg)
    bot.registries = types.SimpleNamespace(ordered=lambda: [], by_key={}, detached={reg.key: reg}, load_errors=[])
    with pytest.raises(TokenUnverifiable):
        bot.feed_issued(token)
    comp = types.SimpleNamespace(bound=reg.key, token_id=rec.id)
    assert bot.feed_token_valid(comp) is None
    reg.revoke_feed_token(rec.id, "Owner")
    assert bot.feed_token_valid(comp) is False and bot.feed_issued(token) is None


def test_a_guild_that_did_not_load_makes_an_issued_looking_token_unverifiable(reg):
    from oibot_gm.feed import TOKEN_PREFIX, TokenUnverifiable

    bot = FeedBot(reg)
    bot.registries = types.SimpleNamespace(ordered=lambda: [reg], by_key={reg.key: reg}, detached={}, load_errors=["other: ValueError: bad"])
    with pytest.raises(TokenUnverifiable):
        bot.feed_issued(TOKEN_PREFIX + "x" * 43)
    assert bot.feed_issued("not-an-issued-token") is None, "a token that can't be an issued one is just wrong"
    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    reg.revoke_feed_token(rec.id, "Owner")
    assert bot.feed_issued(token) is None, "a served guild's revoked token is dead, whatever else didn't load"
    assert bot.feed_token_valid(types.SimpleNamespace(bound="other", token_id="ft-1")) is None


def test_authorize_any_unsure():
    from oibot_gm.feed import TokenUnverifiable

    def unsure(t):
        raise TokenUnverifiable("parked")
    assert authorize_any("x", "shared", {}, lambda: [], unsure) == (None, None, None, None)
    assert authorize_any("shared", "shared", {}, lambda: [], unsure) == (True, None, None, None), "the shared token still holds"


def test_a_detached_guilds_env_token_is_unverifiable_not_dead(reg):
    """Regression: an OIBOT_FEED_TOKEN_<KEY> whose guild isn't served now (parked, or its guild.yaml failed to load after a
    restart) fell through to `return False` → {"auth": true} → the companion exited for good mid-raid. It is the guild's
    own token: 'can't check now' (ok None, a plain error), and it works again once the guild is served."""
    from oibot_gm.feed import UNAVAILABLE, token_suffix

    env = {token_suffix(reg.key): "guild-env-token"}
    assert authorize_any("guild-env-token", "shared", env, lambda: [], None) == (None, None, None, None)
    assert authorize_any("guild-env-token", "shared", env, lambda: [reg.key], None) == (True, reg.key, None, None)
    assert authorize_any("guild-env-token", "guild-env-token", env, lambda: [], None) == (True, None, None, None), "the shared value still holds"
    assert authorize_any("wrong", "shared", env, lambda: [], None) == (False, None, None, None), "a token nobody holds is still dead"
    served: list = []
    seen: list = []
    fs = feed_server(reg, seen)
    fs.auth = lambda t: authorize_any(t, None, env, lambda: list(served), None)
    out = run(session(fs, [{"type": "hello", "token": "guild-env-token", "character": "Rhozy"}]))
    assert out[0] == {"type": "error", "message": UNAVAILABLE} and "auth" not in out[0] and seen == []
    served.append(reg.key)
    assert run(session(fs, [{"type": "hello", "token": "guild-env-token", "character": "Rhozy"}]))[0]["type"] == "welcome"


def test_an_unverifiable_hello_or_frame_is_a_plain_error_and_the_socket_closes(reg):
    """The wire: a hello the bot can't check now, and a frame whose guild went away, get {type:error} WITHOUT auth (the
    companion reconnects and re-sends) and nothing reaches the handler; a genuinely unknown token still gets auth:true."""
    from oibot_gm.feed import UNAVAILABLE

    token, rec = reg.issue_feed_token("Jon's PC", "Owner")
    seen: list = []
    fs = feed_server(reg, seen)
    bot = FeedBot(reg)
    parked = types.SimpleNamespace(ordered=lambda: [], by_key={}, detached={reg.key: reg}, load_errors=[])
    fs.auth = lambda t: authorize_any(t, "shared-env", {}, lambda: [], bot.feed_issued)
    fs.still_valid = bot.feed_token_valid
    bot.registries = parked
    out = run(session(fs, [{"type": "hello", "token": token, "character": "Rhozy"}]))
    assert out[0] == {"type": "error", "message": UNAVAILABLE} and "auth" not in out[0] and seen == []
    bot.registries = FeedBot(reg).registries  # served again: the same token works
    served = bot.registries
    out = run(session(fs, [{"type": "hello", "token": token, "character": "Rhozy"},
                           lambda: setattr(bot, "registries", parked),
                           {"type": "loot", "idem": "l1"}]))
    assert out[0]["type"] == "welcome" and out[1] == {"type": "error", "message": UNAVAILABLE}, "no idem: the event stays in flight"
    assert [s[0] for s in seen] == ["hello"], "the frame is never applied"
    bot.registries = served
    assert run(session(fs, [{"type": "hello", "token": "oibot-feed-unknown", "character": "X"}]))[0] == {"type": "error", "auth": True, "message": BAD_TOKEN}


def test_a_throwing_auth_is_a_plain_error_and_never_logs_the_token(reg, caplog):
    from oibot_gm.feed import UNAVAILABLE

    fs = FeedServer(lambda ev, c: None, None)

    def boom(t):
        raise RuntimeError(f"lookup failed for {t}")
    fs.auth = boom
    with caplog.at_level(logging.DEBUG):
        out = run(session(fs, [{"type": "hello", "token": "oibot-feed-SECRETVALUE", "character": "X"}]))
    assert out[0] == {"type": "error", "message": UNAVAILABLE}
    assert "SECRETVALUE" not in caplog.text


def test_the_companion_retries_a_plain_hello_error():
    """The companion's sender: an auth refusal raises TokenRefused (exit); a plain error at the hello is NOT a token
    refusal (it reconnects with the backoff) — the source raises ConnectionError for it."""
    comp = _companion()
    assert comp.auth_refusal({"type": "error", "message": "the bot can't check"}) is None
    src = (Path(__file__).resolve().parents[1] / "companion" / "oibot_companion.py").read_text()
    assert 'if welcome.get("type") == "error":' in src and "raise ConnectionError" in src


def test_the_listener_starts_for_an_issued_token(reg, monkeypatch):
    """No .env token: start_feed stays off until a guild holds a token (or ensure_feed after the first issue)."""
    for k in ("OIBOT_FEED_TOKEN", "OIBOT_FEED_TOKEN_DEMO"):
        monkeypatch.delenv(k, raising=False)
    started = []

    async def fake_start(self):
        started.append(self)
    monkeypatch.setattr(FeedServer, "start", fake_start)
    bot = FeedBot(reg)
    bot.handle_feed_event = lambda ev, c: None
    bot.feed_disconnected = lambda c: None
    assert run(bot.start_feed()) is False and bot.feed is None
    reg.issue_feed_token("Jon's PC", "Owner")
    assert run(bot.start_feed()) is True and bot.feed is not None and len(started) == 1
    assert run(bot.ensure_feed()) is True and len(started) == 1, "once"
    assert bot.feed.still_valid == bot.feed_token_valid


# ---------------------------------------------------------------- web: /api/companions (owner only; the value once)

class CompBot(WebLootBot):
    companion_rows = SetupMixin.companion_rows
    issue_companion = SetupMixin.issue_companion
    revoke_companion = SetupMixin.revoke_companion

    async def ensure_feed(self):  # never a real listener in a test
        return True


@pytest.fixture
def cw(reg, rs, env):
    bot = CompBot(reg, rs)
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg)


def test_the_web_issue_carries_the_value_exactly_once(cw, caplog, store):
    h = as_user(cw.c, OWNER)
    assert cw.c.get("/api/companions", headers=h).json()["tokens"] == []
    with caplog.at_level(logging.INFO):
        r = cw.c.post("/api/companions/issue", json={"label": "Jon's PC"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    token = d["token"]
    assert token and r.text.count(token) == 1 and r.headers.get("cache-control") == "no-store"
    rec = cw.reg.feed_token(d["id"])
    assert rec.hash not in r.text and d["label"] == "Jon's PC" and [t["label"] for t in d["tokens"]] == ["Jon's PC"]
    again = cw.c.get("/api/companions", headers=h)
    assert token not in again.text and rec.hash not in again.text and again.json()["tokens"][0]["state"] == "never"
    assert token not in caplog.text, "the request log never holds the value"
    assert any("/api/companions/issue" in m for m in caplog.messages), "the issue itself is logged (path and status)"
    lines = [t for _, _, t in cw.bot.ops.lines]
    assert any(rec.id in t and "Jon's PC" in t and "shown once on the site" in t for t in lines)
    assert not any(token in t for t in lines), "no ops line holds the value"
    assert not files_holding(store.root, token)


def test_officers_members_and_mcp_may_not_issue(cw, reg):
    reg_members = [m for m in reg.members.values() if m.test]
    off = as_user(cw.c, OFFICER)
    assert cw.c.get("/api/companions", headers=off).status_code == 403
    assert cw.c.post("/api/companions/issue", json={"label": "x"}, headers=off).status_code == 403
    assert cw.c.post("/api/companions/revoke", json={"id": "x"}, headers=off).status_code == 403
    mem = as_user(cw.c, reg_members[0].discord_id)
    assert cw.c.get("/api/companions", headers=mem).status_code == 403
    cw.c.cookies.clear()
    r = cw.c.post("/api/companions/issue", json={"label": "via mcp"}, headers=MCP)
    assert r.status_code == 403 and "never issues" in r.text and reg.config.feed_tokens == []
    assert cw.c.get("/api/companions", headers=MCP).status_code == 200, "MCP (the owner's token) lists"


def test_the_web_revoke_and_refusals(cw):
    h = as_user(cw.c, OWNER)
    d = cw.c.post("/api/companions/issue", json={"label": "Jon's PC"}, headers=h).json()
    dup = cw.c.post("/api/companions/issue", json={"label": "Jon's PC"}, headers=h)
    assert dup.status_code == 400 and "already in use" in dup.json()["error"]
    r = cw.c.post("/api/companions/revoke", json={"id": d["id"]}, headers=h)
    assert r.status_code == 200 and "revoked" in r.json()["message"] and r.json()["tokens"][0]["state"] == "revoked"
    assert cw.c.post("/api/companions/revoke", json={"id": d["id"]}, headers=h).status_code == 400
    assert any("revoked companion token" in t and d["id"] in t for _, _, t in cw.bot.ops.lines)


def test_mcp_lists_and_revokes_but_never_sees_a_value(cw, monkeypatch):
    token, rec = cw.reg.issue_feed_token("Jon's PC", "Owner")
    cw.c.cookies.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        r = cw.c.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "application/json")})
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        out = ms.list_companions()
        assert [t["label"] for t in out["tokens"]] == ["Jon's PC"] and token not in repr(out) and rec.hash not in repr(out)
        assert "stops working" in ms.revoke_companion("jon's pc", preview=True) and cw.reg.feed_token(rec.id).active, "a preview changes nothing"
        assert "revoked" in ms.revoke_companion(rec.id)
        assert not cw.reg.feed_token(rec.id).active
        with pytest.raises(ms.ToolError, match="isn't an active"):
            ms.revoke_companion(rec.id)
    finally:
        ms.use(None)
    names = {t.name for t in run(ms.mcp.list_tools())}
    assert {"list_companions", "revoke_companion"} <= names and not any("issue" in n and "companion" in n for n in names)


# ---------------------------------------------------------------- Discord: /gm config companion (harness)

class HubBot(SetupMixin):
    def __init__(self, reg):
        from test_loot_verbs import RecOps

        self.ops = RecOps()
        self.feed = None
        self.raids = types.SimpleNamespace(store=lambda r: None)

    async def ensure_feed(self):
        return True


class Leader:
    def __init__(self, uid=555, name="Rhozy", fail=None):
        self.id, self.display_name, self.bot, self.mention, self.fail = uid, name, False, f"@{name}", fail
        self.dms: list[str] = []

    async def send(self, content=None, **kw):
        if self.fail:
            raise self.fail
        self.dms.append(content)


def owner_harness(reg, rs):
    h = Harness(reg, rs, user=types.SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"), client=HubBot(reg))
    return h


@pytest.fixture
def in_server(monkeypatch):
    async def member_of(bot, reg, uid, timeout=None, max_age=None):
        return ("yes", None)
    monkeypatch.setattr(guild_routing, "member_of", member_of)


def test_the_hub_dms_the_token_once_to_the_chosen_member(reg, rs, in_server):
    h = owner_harness(reg, rs)
    leader = Leader()

    async def go():
        await FLOWS["companion"](h.command(), reg)
        assert "No companion token yet" in h.text and h.buttons() == ["Issue…", "Revoke…", "Done"]
        await h.press("Issue…")
        await h.submit(label="Jon's PC")
        assert "DMed to them once" in h.text
        await h.pick_member("Pick the raid leader", leader)
        assert "DMed once to **Rhozy**" in h.text and "Issue and DM" in h.buttons()
        await h.press("Issue and DM")
        assert "✅ Token “Jon's PC” sent to Rhozy." in h.text
    run(go())
    assert len(leader.dms) == 1, "exactly one DM, to the chosen member"
    token = re.search(r"\|\|`(oibot-feed-[^`]+)`\|\|", leader.dms[0]).group(1)
    rec = reg.match_feed_token(token)
    assert rec is not None and rec.label == "Jon's PC" and rec.sent_to == "Rhozy"
    assert not any(token in s["text"] for s in h.screens), "the owner never sees the value"
    assert not any(token in t for _, _, t in h.client.ops.lines) and any("DMed to Rhozy" in t and rec.id in t for _, _, t in h.client.ops.lines)
    assert not files_holding(reg.store.root, token)


def test_the_hub_revokes_with_the_decide_screen(reg, rs, in_server):
    _, rec = reg.issue_feed_token("Jon's PC", "Owner")
    h = owner_harness(reg, rs)

    async def go():
        await FLOWS["companion"](h.command(), reg)
        assert "**Jon's PC** · never connected" in h.text
        await h.press("Revoke…")
        await h.pick("Pick the token to revoke", rec.id)
        assert "stops working" in h.text and "Revoke" in h.buttons() and "Back" in h.buttons()
        await h.press("Revoke")
        assert "✅ “Jon's PC” revoked." in h.text
    run(go())
    assert not reg.feed_token(rec.id).active


def test_a_dm_that_cant_be_delivered_issues_nothing(reg, rs, in_server):
    h = owner_harness(reg, rs)
    leader = Leader(fail=discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user"))

    async def go():
        await FLOWS["companion"](h.command(), reg)
        await h.press("Issue…")
        await h.submit(label="Jon's PC")
        await h.pick_member("Pick the raid leader", leader)
        await h.press("Issue and DM")
    run(go())
    assert "Couldn't DM Rhozy" in h.text and "nothing was issued" in h.text
    assert reg.feed_tokens(include_revoked=False) == [], "the token was withdrawn at once"


def test_the_hub_refuses_a_bot_a_bad_label_and_non_owners(reg, rs, in_server):
    reg.issue_feed_token("Jon's PC", "Owner")
    h = owner_harness(reg, rs)
    robot = Leader(name="Robot")
    robot.bot = True

    async def go():
        await FLOWS["companion"](h.command(), reg)
        await h.press("Issue…")
        await h.submit(label="jon's pc")
        assert "already in use" in h.text and "Pick another label" in h.buttons()
        await h.press("Pick another label")
        await h.submit(label="Spare PC")
        await h.pick_member("Pick the raid leader", robot)
        assert "A bot can't" in h.text
    run(go())
    assert len(reg.feed_tokens()) == 1
    member = next(m for m in reg.members.values() if m.test)
    other = Harness(reg, rs, user=types.SimpleNamespace(id=member.discord_id, display_name="M", mention="@M"), client=HubBot(reg))
    run(FLOWS["companion"](other.command(), reg))
    assert "Owner only" in other.text


# ---------------------------------------------------------------- parity: every surface, the DM delivery Discord-only by design

def test_companion_tokens_are_on_every_surface(slash_paths, web_routes):  # noqa: F811
    assert discord_path_exists("flow:companion", slash_paths) and "gm config companion" in slash_paths
    assert {"GET /api/companions", "POST /api/companions/issue", "POST /api/companions/revoke"} <= web_routes
    names = {t.name for t in run(ms.mcp.list_tools())}
    assert {"list_companions", "revoke_companion"} <= names
    row = configops.BY_DESIGN_ONLY["companion_token_dm"]
    assert row["surface"] == "discord" and row["path"] == "flow:companion" and "transcript" in row["reason"]
    assert "companion_tokens" not in configops.BY_DESIGN_ONLY, "the issued tokens are no longer a web-only card"


def test_the_companion_command_fits_discords_limits(reg):
    """Discord refuses a description over 100 characters at sync (check_commands caught a 101-character one)."""
    from discord import app_commands

    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree, Guilds.of(reg.store, reg), Ops(client), lambda k, v: "")
    gm = next(c for c in tree.get_commands() if c.name == "gm")
    config = next(c for c in gm.commands if c.name == "config")
    cmd = next(c for c in config.commands if c.name == "companion")
    assert len(cmd.description) <= 100 and not cmd.parameters, "no typed argument: the hub picks everything"


# ---------------------------------------------------------------- the companion: --token / --token-file / env; a refused token ends it

def _companion():
    import importlib.util

    spec = importlib.util.spec_from_file_location("oibot_companion_w3", Path(__file__).resolve().parents[1] / "companion" / "oibot_companion.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_companion_reads_its_token_flag_file_or_env(tmp_path):
    comp = _companion()
    f = tmp_path / "token.txt"
    f.write_text("from-file\nignored\n")
    ns = lambda **kw: types.SimpleNamespace(**{"token": "", "token_file": None, **kw})  # noqa: E731
    assert comp.token_from(ns(token="flag", token_file=str(f)), {"OIBOT_COMPANION_TOKEN": "env"}) == "flag"
    assert comp.token_from(ns(token_file=str(f)), {"OIBOT_COMPANION_TOKEN": "env"}) == "from-file"
    assert comp.token_from(ns(), {"OIBOT_COMPANION_TOKEN": "env", "OIBOT_FEED_TOKEN": "old"}) == "env"
    assert comp.token_from(ns(), {"OIBOT_FEED_TOKEN": "old"}) == "old" and comp.token_from(ns(), {}) == ""


def test_a_revoked_token_ends_the_companion_instead_of_retrying():
    comp = _companion()

    class WS:
        def __init__(self, replies):
            self.replies, self.sent = list(replies), []

        async def send(self, frame):
            self.sent.append(frame)

        async def recv(self):
            return json.dumps(self.replies.pop(0))
    box = comp.Outbox()
    box.put({"type": "loot", "idem": "a"})
    with pytest.raises(comp.TokenRefused, match="revoked"):
        run(comp.deliver(WS([{"type": "error", "auth": True, "idem": "zzz", "message": REVOKED}]), box.next(0), box))
    assert len(box) == 1, "the event stays (a new token re-sends it)"
    assert comp.auth_refusal({"type": "error", "message": "several councils"}) is None, "a routing refusal is retried as before"


# ---------------------------------------------------------------- the token records never leave the owner's hub (fixer findings 1-2)

def test_officers_config_view_holds_no_token_record(cw):
    """GET /api/config (officers) dumps guild.yaml as 'As stored': never a companion token's hash, label, issuer or sent_to."""
    token, rec = cw.reg.issue_feed_token("Jon's PC", "Owner", 4242, sent_to="Raidlead Rhozy")
    server = cw.bot.get_guild

    def get_guild(gid):  # the member directory's server, with its officer role to list (the config route reads the roles)
        g = server(gid)
        if g is not None and not hasattr(g, "roles"):
            g.roles = [types.SimpleNamespace(id=OFFICER_ROLE, name="Officer", position=1, managed=False, is_default=lambda: False)]
        return g
    cw.bot.get_guild = get_guild
    for who in ("officer", "mcp"):  # an officer's session, and the owner's bearer (MCP guild_overview reads this route)
        if who == "officer":
            h = as_user(cw.c, OFFICER)
        else:
            cw.c.cookies.clear()
            h = MCP
        r = cw.c.get("/api/config", headers=h)
        assert r.status_code == 200, (who, r.text)
        assert r.json()["yaml"].strip(), "the 'As stored' dump is still there"
        for needle in (token, rec.hash, rec.hash.split(":", 1)[1], "Jon's PC", "Raidlead Rhozy", rec.id, "feed_tokens"):
            assert needle not in r.text, (who, needle)
    assert "feed_tokens" in (cw.reg.store.root / cw.reg.key / "guild.yaml").read_text(), "guild.yaml itself keeps the records"


def test_the_config_parsers_prompt_holds_no_token_record(reg):
    """configops.current_config_text is the config_change parser's context — a member's plain-text request gets the
    parser's reply, so the token records (hash, label, sent_to, last_seen) are never in it, for anyone."""
    token, rec = reg.issue_feed_token("Jon's PC", "Owner", 4242, sent_to="Raidlead Rhozy")
    reg.touch_feed_token(rec.id, save_after=0)
    for officer in (False, True):
        text = configops.current_config_text(reg, officer=officer)
        for needle in (token, rec.hash, rec.hash.split(":", 1)[1], "Jon's PC", "Raidlead Rhozy", rec.id, "feed_tokens"):
            assert needle not in text, (officer, needle)


def test_readable_config_drops_every_private_field(reg):
    from oibot_gm.registry import PRIVATE_CONFIG_FIELDS, readable_config

    reg.issue_feed_token("Jon's PC", "Owner")
    d = readable_config(reg.config)
    assert not set(PRIVATE_CONFIG_FIELDS) & set(d) and reg.config.feed_tokens, "a copy: the live config keeps them"
