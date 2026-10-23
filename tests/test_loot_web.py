"""The loot council's other surfaces (dual interfaces phase 1, L3): the web routes (web/api_loot.py) over the REAL
LootMixin verbs, member visibility (D2), wishlists (D5) incl. the ThatsMyBIS import, items and tiers with the re-weight
ask (D12), precedents (retire / restore), the MCP actor rule (D8), the `loot` / `wishlist` / `tier` plain-text ops,
the public loot log (LootLogMixin) and the /me wishlist wizard (harness)."""
from __future__ import annotations

import asyncio
import time
import types
from pathlib import Path

import httpx
import pytest
from conftest import GUILD, OFFICER_ROLE, OWNER, MemberDirectory
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_loot_verbs import LootBot, ledger, precedents, real_run, run, tick_one, tiered_item

from oibot_gm import configops, lootctx
from oibot_gm import mcp_server as ms
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_pool import LootLogMixin, loot_log_line
from oibot_gm.discord_registry import Guilds
from oibot_gm.loot.session import DISENCHANT
from oibot_gm.registry import PLAIN_WEB, WISHLIST_MAX, RegistryError, WishlistChanged, item_key
from oibot_gm.web import app as web

TOKEN = "a" * 64
SECRET = "s" * 40
OFFICER = 300_000_000_000_000_005
OUTSIDER = 300_000_000_000_000_009
DATA = Path(__file__).resolve().parent  # tests/tmb_wishlists.csv (a data/ folder is gitignored)


def person(uid, *roles):
    return types.SimpleNamespace(id=uid, display_name=f"u{str(uid)[-2:]}", roles=[types.SimpleNamespace(id=r) for r in roles])


class Ring:
    def __init__(self):
        self.lines: list = []

    def recent_for(self, key):
        return []

    async def emit(self, cfg, level, text, exc=None, dm=True):
        self.lines.append((cfg.key, level, text))


class WebLootBot(LootBot):
    """LootBot (the real verbs, Discord recorded) behind the real web app: the guild index, a member directory (who is
    in the server, who holds the officer role) and the few client methods create_app reads."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.registries = Guilds.of(reg.store, reg)
        self.ops = Ring()
        self.user, self.started_at = None, 0.0
        people = {(1, OFFICER): person(OFFICER, OFFICER_ROLE), (1, OWNER): person(OWNER)}
        for m in reg.test_members():
            people[(1, m.discord_id)] = person(m.discord_id)
        MemberDirectory(people).install(self, servers=(1,))

    def officiates(self, member, guild):
        return any(r.id == OFFICER_ROLE for r in getattr(member, "roles", []))

    def guild_channels(self, reg):
        return []

    async def refresh_sheet(self, reg, ev):
        return None


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)


@pytest.fixture
def w(reg, rs, env):
    """SimpleNamespace(bot, c (TestClient), reg, rs, ev (a locked real run), ms (its members))."""
    ev, members = real_run(reg, rs)
    bot = WebLootBot(reg, rs)
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, rs=rs, ev=ev, ms=members)


H = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}


def as_user(c, uid) -> dict:
    c.cookies.clear()
    c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


def opened(w):
    r = w.c.post(f"/api/loot/{w.ev.key}/open", json={}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def tick(w, *names, boss="Gatekeeper"):
    r = w.c.post(f"/api/loot/{w.ev.key}/tick", json={"add": [{"name": n, "boss": boss} for n in names]}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def distribute(w):
    r = w.c.post(f"/api/loot/{w.ev.key}/distribute", json={}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def council(d):
    (c,) = d["state"]["councils"]
    return c


# ---------------------------------------------------------------- the council over HTTP (one verb, every surface)

def test_open_tick_distribute_confirm_end_through_the_routes(w):
    d = opened(w)
    assert "Loot council open" in d["message"] and council(d)["id"] == w.ev.key
    assert "already open" in opened(w)["message"], "idempotent: a second officer gets the same council"
    d = tick(w, "Shard of Doom", "Wand of Tests")
    c = council(d)
    assert [x["drop_id"] for x in c["bosses"][0]["drops"]] == ["d1", "d2"] and c["untiered"] == 2 and d["drops_rev"]
    d = distribute(w)
    c = council(d)
    assert len(c["proposals"]) == 2 and d["batch_rev"] == c["batch_rev"]
    p = c["proposals"][0]
    assert p["candidates"] and "base" in p["candidates"][0] and p["justification"], "officers see the candidate table"
    # a stale Confirm-all: 409 with what changed and the councils as they are now
    r = w.c.post(f"/api/loot/{w.ev.key}/override", json={"drop_id": "d1", "to": w.ms[1].main.label, "reason": "tank progression"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200, r.text
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"batch_rev": c["batch_rev"]}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    # the latest change is the override, or — when the re-judge it spawned (discord_loot._rejudge) finished inside the
    # override request's loop — that re-judge's "distributed": a race in the test client, both name what changed
    err = r.json()["error"]
    assert r.status_code == 409 and r.json()["changed"] and ("overrode" in err or "distributed 1" in err) and r.json()["state"]["councils"], r.text
    fresh = r.json()["state"]["councils"][0]["batch_rev"]
    pv = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"preview": True}, headers=H).json()
    assert "Shard of Doom → " + w.ms[1].main.label + " (override)" in pv["preview"], "a preview needs no actor and writes nothing"
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"batch_rev": fresh}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200, r.text
    rows = ledger(w.reg)
    assert len(rows) == 2 and rows[0]["raider"] == w.ms[1].main.label and rows[0]["by"] == "u05 via MCP", "D8: named, via MCP"
    assert precedents(w.reg)[0]["reason"] == "tank progression"
    # a second Confirm of the same drop: refused, never a duplicate row
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"only": ["d1"], "expect_to": w.ms[1].main.label}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 400 and "Already recorded" in r.json()["error"] and len(ledger(w.reg)) == 2
    assert "End " in w.c.post(f"/api/loot/{w.ev.key}/end", json={"preview": True}, headers=H).json()["preview"]
    r = w.c.post(f"/api/loot/{w.ev.key}/end", json={}, headers=H)
    assert r.status_code == 200 and w.rs.events[w.ev.key].state == "done", r.text
    hist = w.c.get("/api/loot/history", headers=H).json()["rows"]
    assert hist and hist[0]["run"] == w.ev.key and hist[0]["awards"] == 2 and hist[0]["overrides"] == 1
    one = w.c.get(f"/api/loot/history/{hist[0]['file']}", headers=H).json()
    assert {a["drop_id"] for a in one["awards"]} == {"d1", "d2"} and one["overrides"][0]["reason"] == "tank progression"


def test_override_needs_a_reason_discard_holds_and_tick_refuses_an_awarded_untick(w):
    opened(w)
    tick(w, "Shard of Doom")
    c = council(distribute(w))
    r = w.c.post(f"/api/loot/{w.ev.key}/override", json={"drop_id": "d1", "to": w.ms[2].main.label, "reason": ""}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 400 and "reason" in r.json()["error"]
    pv = w.c.post(f"/api/loot/{w.ev.key}/discard", json={"preview": True}, headers=H).json()
    assert pv["preview"].startswith("Discard 1 proposal") and "nothing was recorded" in pv["preview"]
    r = w.c.post(f"/api/loot/{w.ev.key}/discard", json={"batch_rev": c["batch_rev"]}, headers=H)
    assert r.status_code == 200 and not council(r.json())["proposals"], r.text
    c = council(distribute(w))
    w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"only": ["d1"], "expect_to": c["proposals"][0]["to"]}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    r = w.c.post(f"/api/loot/{w.ev.key}/tick", json={"remove": ["d1"]}, headers=H)
    assert r.status_code == 400 and "Correct" in r.json()["error"]


def test_reason_and_correct_after_the_council_ended(w):
    """A companion mismatch owes a reason; both a reason and a correction work days later on an ended council (by id)."""
    opened(w)
    tick(w, "Shard of Doom", "Wand of Tests")
    c = council(distribute(w))
    s = w.bot.events[(GUILD, w.ev.key)]
    bot_pick = c["proposals"][0]["to"]
    other = next(m.main.label for m in w.ms if m.main.label != bot_pick)
    comp = types.SimpleNamespace(character=w.ms[0].main.label, guild=GUILD, bound=None, idem=None)
    run(w.bot._feed_loot(w.reg, s, {"item_name": "Shard of Doom", "recipient": other}, comp, "companion:x"))
    assert s.pending_reasons
    lst = w.c.get("/api/loot", headers=H).json()
    assert lst["reasons_pending"] and lst["reasons_pending"][0]["recipient"] == other
    w.c.post(f"/api/loot/{w.ev.key}/end", json={}, headers=H)
    r = w.c.post(f"/api/loot/{w.ev.key}/reason", json={"drop_id": "d1", "reason": "she tanks tonight"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and "Precedent" in r.json()["message"], r.text
    r = w.c.post(f"/api/loot/{w.ev.key}/reason", json={"drop_id": "d1", "reason": "again"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 409 and "replace?" in r.json()["error"]
    r = w.c.post(f"/api/loot/{w.ev.key}/correct", json={"drop_id": "d1", "to": bot_pick, "reason": "misclick in game"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and "was " + other in r.json()["message"], r.text


# ---------------------------------------------------------------- D2: who sees what

def test_members_see_their_own_loot_and_the_log_without_scores(w):
    opened(w)
    tick(w, "Shard of Doom")
    c = council(distribute(w))
    winner = c["proposals"][0]["to"]
    w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"batch_rev": c["batch_rev"]}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    m = next(m for m in w.ms if m.main.label == winner)
    other = next(x for x in w.ms if x is not m)
    h = as_user(w.c, m.discord_id)
    mine = w.c.get("/api/me/loot", headers=h).json()["rows"]
    assert [r["item"] for r in mine] == ["Shard of Doom"] and mine[0]["mine"] and "reason" not in mine[0] and "candidates" not in mine[0]
    log = w.c.get("/api/loot/log", params={"scores": "true"}, headers=h).json()
    assert log["scores"] is False and log["rows"][0]["character"] == winner
    assert not {"tier", "weight", "source", "bot", "by", "candidates", "reason"} & set(log["rows"][0]), "server-side filter (D2)"
    assert w.c.get("/api/loot", headers=h).status_code == 403 and w.c.get("/api/loot/precedents", headers=h).status_code == 403
    assert w.c.post(f"/api/loot/{w.ev.key}/tick", json={"add": [{"name": "X"}]}, headers=h).status_code == 403
    h2 = as_user(w.c, other.discord_id)
    assert w.c.get("/api/me/loot", headers=h2).json()["rows"] == [], "never anyone else's"
    assert w.c.get("/api/loot/log", headers=as_user(w.c, OUTSIDER)).status_code in (401, 403), "logged-in members of the guild only"
    w.c.cookies.clear()
    pub = w.c.get("/api/public").json()
    assert "Shard of Doom" not in str(pub), "nothing loot-related on /api/public"
    off = w.c.get("/api/loot/log", params={"scores": "true"}, headers=as_user(w.c, OFFICER)).json()
    assert off["scores"] and off["rows"][0]["source"] and off["rows"][0]["candidates"]


def test_ops_no_longer_carries_loot_and_precedents_filter_by_member(w):
    m = w.ms[0]
    for i in range(6):
        w.reg.store.append_jsonl(Path(w.reg.key) / "precedents.jsonl", {"item": f"I{i}", "bot": "Someone", "human": m.main.label if i % 2 else "Other", "reason": "r"})
    w.bot.__dict__.get("contexts", {}).clear()
    d = w.c.get("/api/ops", headers=H).json()
    assert "precedents" not in d and "ledger" not in d
    p = w.c.get("/api/loot/precedents", params={"member": m.display_name}, headers=H).json()
    assert len(p["rows"]) == 3 and all(r["human"] == m.main.label for r in p["rows"]) and p["member"] == m.display_name
    assert p["rows"][0]["ref"] == "row-5", "newest first, named by place when there is no import id"


def test_retire_and_restore_a_precedent(w):
    w.reg.store.append_jsonl(Path(w.reg.key) / "precedents.jsonl", {"item": "Syphon", "bot": "Torsha", "human": "Eltam", "reason": "tanks", "import_id": "r1-d1"})
    w.bot.__dict__.get("contexts", {}).clear()
    hdr = H | {"X-Oibot-Actor": str(OFFICER)}
    assert w.c.post("/api/loot/precedents/retire", json={"ref": "r1-d1", "reason": ""}, headers=hdr).status_code == 400
    assert w.c.post("/api/loot/precedents/retire", json={"ref": "r1-d1", "reason": "old tier"}, headers=H).status_code == 403, "D8: a decision"
    r = w.c.post("/api/loot/precedents/retire", json={"ref": "r1-d1", "reason": "old tier"}, headers=hdr)
    assert r.status_code == 200 and "retired" in r.json()["message"], r.text
    rows = w.c.get("/api/loot/precedents", params={"status": "retired"}, headers=H).json()["rows"]
    assert rows[0]["status_reason"] == "old tier" and rows[0]["status_by"] == "u05 via MCP"
    ctx = lootctx.RegistryLootContext(w.reg, w.reg.store, None)
    assert "Syphon" not in ctx.policy_for_llm(), "the bot cites active precedents only"
    r = w.c.post("/api/loot/precedents/retire", json={"ref": "r1-d1", "reason": "back in", "restore": True}, headers=hdr)
    assert r.status_code == 200 and "restored" in r.json()["message"]
    assert "Syphon" in lootctx.RegistryLootContext(w.reg, w.reg.store, None).policy_for_llm()


# ---------------------------------------------------------------- D8: MCP decisions need a named officer

def test_mcp_decisions_need_an_officer_actor(w):
    opened(w)
    tick(w, "Shard of Doom")
    c = council(distribute(w))
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"batch_rev": c["batch_rev"]}, headers=H)
    assert r.status_code == 403 and "X-Oibot-Actor" in r.json()["error"], "the owner's token alone does not decide"
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"batch_rev": c["batch_rev"]}, headers=H | {"X-Oibot-Actor": str(w.ms[0].discord_id)})
    assert r.status_code == 403 and "not an officer" in r.json()["error"]
    assert w.c.get("/api/loot", headers=H | {"X-Oibot-Actor": "nope"}).status_code == 403
    assert not ledger(w.reg)
    assert w.c.post(f"/api/loot/{w.ev.key}/discard", json={"preview": True}, headers=H).status_code == 200, "previews and discards need no actor"
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"batch_rev": c["batch_rev"]}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and ledger(w.reg)[0]["by"] == "u05 via MCP"
    assert any("[mcp] u05 via MCP" in t for _, _, t in w.bot.ops.lines), "audited with the surface prefix and the named officer"


def test_mcp_tools_send_the_actor_only_on_decisions(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("x-oibot-actor")))
        return httpx.Response(200, json={"message": "ok", "preview": "p", "drops_rev": "x", "batch_rev": "y"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        monkeypatch.setenv(ms.ACTOR_ENV, str(OFFICER))
        ms.loot_tick("bd", ["Shard of Doom"], boss="Gatekeeper")
        ms.loot_confirm("bd", preview=True)
        ms.loot_confirm("bd", batch_rev="abc")
        ms.loot_award("bd", "d1", "Eltam", "tanks")
        ms.set_wishlist("Kessa", [{"name": "Robe"}])
        ms.tier_item("Robe of Tests", {"Priest:Holy": "S"})
        ms.end_loot("bd", preview=True)
    finally:
        ms.use(None)
    actor = {p: a for _, p, a in calls}
    assert actor["/api/loot/bd/tick"] is None and actor["/api/loot/bd/end"] is None
    assert actor["/api/loot/bd/override"] == str(OFFICER) and actor["/api/members/wishlist"] == str(OFFICER)
    assert [a for _, p, a in calls if p.startswith("/api/loot/items/")] == [str(OFFICER)]
    confirms = [a for _, p, a in calls if p == "/api/loot/bd/confirm"]
    assert confirms == [None, str(OFFICER)], "a preview sends no actor; the confirm does"
    names = {t.name for t in asyncio.run(ms.mcp.list_tools())}
    assert {"get_loot", "open_loot", "loot_tick", "loot_distribute", "loot_discard", "end_loot", "loot_confirm", "loot_award", "loot_reason",
            "loot_correct", "tier_item", "set_wishlist", "list_items", "loot_history", "loot_log", "list_precedents", "retire_precedent",
            "get_wishlists", "import_wishlists"} <= names


def test_plain_change_from_mcp_refuses_loot_decisions_without_an_actor(w):
    ops = [ConfigOp(op="loot", field="confirm", target=w.ev.key), ConfigOp(op="loot", field="tick", target=w.ev.key, value="Shard")]
    from oibot_gm.loot_ops import needs_actor

    assert needs_actor(ops[0], OWNER) and not needs_actor(ops[1], OWNER)
    assert needs_actor(ConfigOp(op="tier", target="x", field="Priest:Holy", value="S"), OWNER)
    assert not needs_actor(ConfigOp(op="wishlist", member=f"<@{OWNER}>", value="Robe"), OWNER), "one's own wishlist is not a decision"


# ---------------------------------------------------------------- D5: wishlists

def test_wishlist_save_rev_cap_duplicates_and_received(w):
    m = w.ms[0]
    h = as_user(w.c, m.discord_id)
    d = w.c.get("/api/me/wishlist", headers=h).json()
    (ch,) = d["characters"]
    assert d["max"] == WISHLIST_MAX and ch["entries"] == []
    rows = [{"name": "Shard of Doom", "offspec": False, "note": "BiS"}, {"name": "Wand of Tests", "offspec": True}]
    r = w.c.post("/api/me/wishlist", json={"character": ch["label"], "rows": rows, "rev": ch["rev"]}, headers=h)
    assert r.status_code == 200 and "+ Shard of Doom" in r.json()["message"], r.text
    got = r.json()["characters"][0]["entries"]
    assert [(e["name"], e["rank"], e["offspec"], e["known"]) for e in got] == [("Shard of Doom", 1, False, False), ("Wand of Tests", 2, True, False)]
    stale = w.c.post("/api/me/wishlist", json={"character": ch["label"], "rows": rows[:1], "rev": ch["rev"]}, headers=h)
    assert stale.status_code == 409 and stale.json()["changed"] and stale.json()["characters"][0]["entries"][1]["name"] == "Wand of Tests"
    dup = w.c.post("/api/me/wishlist", json={"character": ch["label"], "rows": rows + [{"name": "shard of doom"}]}, headers=h)
    assert dup.status_code == 400 and "twice" in dup.json()["error"]
    many = [{"name": f"Item {i}"} for i in range(WISHLIST_MAX + 1)]
    assert w.c.post("/api/me/wishlist", json={"character": ch["label"], "rows": many}, headers=h).status_code == 400
    # the item drops and goes to this character: the entry shows received, never deleted
    opened(w)
    tick(w, "Shard of Doom")
    s = w.bot.events[(GUILD, w.ev.key)]
    run(w.bot.distribute(w.reg, s, by="Officer"))
    run(w.bot.override(w.reg, s, "d1", m.main.label, "wishlist #1", by="Officer", rejudge=False))
    run(w.bot.confirm(w.reg, s, by="Officer"))
    got = w.c.get("/api/me/wishlist", headers=h).json()["characters"][0]["entries"]
    assert got[0]["received"] and got[0]["received_label"] and len(got) == 2
    # scoring reads the native lists: the map RegistryLootContext derives
    assert lootctx.RegistryLootContext(w.reg, w.reg.store, None).wishlists[m.main.label][1] == {"item": "Wand of Tests", "rank": 2, "offspec": True}
    # an officer's view of someone's list; officers set it (MCP set_wishlist) with the actor
    o = w.c.get("/api/members/wishlist", params={"member": m.display_name}, headers=H).json()
    assert o["member"] == m.display_name and o["characters"][0]["entries"][0]["name"] == "Shard of Doom"
    r = w.c.post("/api/members/wishlist", json={"member": m.display_name, "rows": [{"name": "Wand of Tests"}]}, headers=H)
    assert r.status_code == 403
    r = w.c.post("/api/members/wishlist", json={"member": m.display_name, "rows": [{"name": "Wand of Tests"}]}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and "− Shard of Doom" in r.json()["message"]
    ov = w.c.get("/api/loot/wishlists", headers=H).json()
    assert ov["items"][0]["name"] == "Wand of Tests" and ov["items"][0]["wants"][0]["character"] == m.main.label


def test_wishlist_registry_verbs(reg):
    m = reg.test_members()[0]
    line = reg.wish_add(m.discord_id, None, "Robe of Tests", by="t", offspec=True, note="pvp")
    assert "+ Robe of Tests (OS)" in line
    reg.wish_add(m.discord_id, None, "Wand", by="t")
    reg.wish_add(m.discord_id, None, "Robe of Tests", by="t", rank=2)
    c = reg.wish_character(m.discord_id)[1]
    assert [(e.name, e.rank, e.offspec) for e in c.wishlist] == [("Wand", 1, False), ("Robe of Tests", 2, False)], "moved, not duplicated"
    rev = reg.wishlist_rev(m, c.label)
    with pytest.raises(WishlistChanged):
        reg.set_wishlist(m.discord_id, c.label, [{"name": "X"}], "t", rev="stale")
    assert reg.wishlist_rev(m, c.label) == rev
    reg.wish_remove(m.discord_id, None, "wand", by="t")
    assert [e.name for e in reg.wish_character(m.discord_id)[1].wishlist] == ["Robe of Tests"]
    with pytest.raises(RegistryError, match="isn't on"):
        reg.wish_remove(m.discord_id, None, "Wand", by="t")
    tiered_item(reg, 90001, "Robe of Tests")
    assert reg.wishlists()[c.label] == [{"item": 90001, "rank": 1, "offspec": False}], "kept by name, scored by id once known"


def test_tmb_import_shapes_and_dedupes_and_never_deletes_native_entries(reg):
    """D5: a ThatsMyBIS wishlist export lines up with the native shape — per character, TMB's order, MS/OS, a note;
    received rows skipped; an item already on the native list is a duplicate (the native entry stays); a re-import
    replaces only the earlier TMB rows; past the cap the rest is counted, not written."""
    a, b = reg.test_members()[:2]
    reg.wish_add(a.discord_id, None, "Native Ring", by="t")
    text = (DATA / "tmb_wishlists.csv").read_text().replace("{A}", a.main.label).replace("{B}", b.main.label)
    pv = reg.import_tmb_wishlists(text, "Officer", preview=True)
    assert pv["characters"] == 2 and pv["received"] == 1 and pv["duplicates"] == 2 and pv["unknown"] == ["Nobodyhere"]
    assert [e.name for e in reg.wish_character(a.discord_id)[1].wishlist] == ["Native Ring"], "a preview writes nothing"
    rep = reg.import_tmb_wishlists(text, "Officer")
    wl = reg.wish_character(a.discord_id)[1].wishlist
    assert [(e.name, e.offspec, e.source) for e in wl] == [("Native Ring", False, "native"), ("Gloves of the Council", False, "tmb"), ("Cloak of Tests", True, "tmb")]
    assert wl[1].note == "BiS hands" and [e.rank for e in wl] == [1, 2, 3]
    assert rep["added"] == 3 and [e.name for e in reg.wish_character(b.discord_id)[1].wishlist] == ["Helm of Order"]
    again = reg.import_tmb_wishlists(text.replace("Cloak of Tests", "Belt of Tests"), "Officer")
    wl = reg.wish_character(a.discord_id)[1].wishlist
    assert [e.name for e in wl] == ["Native Ring", "Gloves of the Council", "Belt of Tests"] and again["replaced"] == 3, "TMB rows replaced, native kept"


def test_wishlist_import_route_previews_then_writes(w):
    text = (DATA / "tmb_wishlists.csv").read_text().replace("{A}", w.ms[0].main.label).replace("{B}", w.ms[1].main.label)
    r = w.c.post("/api/loot/wishlists/import", json={"csv": text, "preview": True}, headers=H)
    assert r.status_code == 200 and "preview" in r.json()["message"] and not w.reg.wish_character(w.ms[0].discord_id)[1].wishlist
    assert w.c.post("/api/loot/wishlists/import", json={"csv": text}, headers=H).status_code == 403, "D8: writing needs the actor"
    r = w.c.post("/api/loot/wishlists/import", json={"csv": text}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and r.json()["report"]["added"] == 4  # no native list here: the Native Ring row imports too


# ---------------------------------------------------------------- D4 / D12: items and tiers

def test_items_untiered_list_tier_grid_and_the_reweight_ask(w):
    opened(w)
    tick(w, "Shard of Doom")
    s = w.bot.events[(GUILD, w.ev.key)]
    run(w.bot.distribute(w.reg, s, by="Officer"))
    winner = s.proposals[0].to
    run(w.bot.confirm(w.reg, s, by="Officer"))
    items = w.c.get("/api/loot/items", headers=H).json()
    key = item_key(0, "Shard of Doom")
    row = next(i for i in items["untiered"] if i["key"] == key)
    assert row["learned"] and row["times_dropped"] == 1 and row["awards"] == 1 and items["specs"]
    grid = w.c.get(f"/api/loot/items/{key}", headers=H).json()
    assert grid["awards"] == 1 and all("equippable" in r for r in grid["rows"])
    hdr = H | {"X-Oibot-Actor": str(OFFICER)}
    win = next(m for m in w.ms if m.main.label == winner)
    spec_key = f"{win.main.cls}:{win.main.spec}"
    r = w.c.post(f"/api/loot/items/{key}/tiers", json={"tiers": {spec_key: "S"}}, headers=hdr).json()
    assert "re-weight" in r["ask"] and r["awards"] == 1, "D12: asked, nothing written"
    assert not w.reg.items()[key].tiers
    r = w.c.post(f"/api/loot/items/{key}/tiers", json={"tiers": {spec_key: "S"}, "reweight": False}, headers=hdr).json()
    assert "kept their weight" in r["message"] and w.reg.items()[key].tiers == {spec_key: "S"}
    assert ledger(w.reg)[0]["total_weight"] == 0.5
    r = w.c.post(f"/api/loot/items/{key}/tiers", json={"tiers": {spec_key: "A"}, "reweight": True}, headers=hdr).json()
    assert r["reweighted"] == 1 and "re-weighted 1" in r["message"]
    rows = ledger(w.reg)
    assert rows[-1]["kind"] == "reweight" and rows[0]["raider"] == winner, "who got what never changes"
    ctx = lootctx.RegistryLootContext(w.reg, w.reg.store, None)
    assert ctx.ledger[0].tier == "A" and ctx.ledger[0].total_weight != 0.5
    bad = w.c.post(f"/api/loot/items/{key}/tiers", json={"tiers": {"Priest:Nope": "S"}, "reweight": False}, headers=hdr)
    assert bad.status_code == 400 and "no spec" in bad.json()["error"]
    found = w.c.get("/api/loot/items/search", params={"q": "shard"}, headers=as_user(w.c, w.ms[3].discord_id)).json()["items"]
    assert found[0]["name"] == "Shard of Doom", "members search items for their wishlist"


# ---------------------------------------------------------------- plain text: the loot / wishlist / tier ops

def test_loot_ops_describe_and_apply_through_the_verbs(w):
    reg, ev = w.reg, w.ev
    bot = w.bot
    open_op = ConfigOp(op="loot", field="open", target=ev.key)
    reg.config.roster_channel_id = 55
    assert "open the loot council" in configops.describe(reg, open_op)
    assert "loot council for" in run(configops.apply_async(reg, open_op, "Officer", False, bot=bot, by_id=OFFICER, via="chat"))
    op = ConfigOp(op="loot", field="tick", target=ev.key, value="Shard of Doom, Wand of Tests", text="Gatekeeper")
    assert "recorded by name" in configops.describe(reg, op)
    run(configops.apply_async(reg, op, "Officer", False, bot=bot, by_id=OFFICER, via="chat"))
    s = bot.events[(GUILD, ev.key)]
    assert [d.item_name for d in s.drop_list] == ["Shard of Doom", "Wand of Tests"] and s.drop_list[0].boss == "Gatekeeper"
    run(configops.apply_async(reg, ConfigOp(op="loot", field="distribute", target=ev.key), "Officer", False, bot=bot, by_id=OFFICER, via="chat"))
    assert len(s.proposals) == 2
    to = next(m.main.label for m in w.ms if m.main.label != s.pending("d1").to)
    award = ConfigOp(op="loot", field="award", target=ev.key, value="Shard of Doom", member=to)
    assert "needs a reason" in configops.describe(reg, award)
    award.reason = "tank progression"
    assert f"council awards {to}" in configops.describe(reg, award)
    run(configops.apply_async(reg, award, "Officer", False, bot=bot, by_id=OFFICER, via="chat"))
    conf = ConfigOp(op="loot", field="confirm", target=ev.key, value="the rest")
    revs = configops.board_revs(reg, [conf])
    assert any(k.startswith("loot:") for k in revs), "Apply carries the batch the diff showed"
    run(configops.apply_async(reg, conf, "Officer", False, bot=bot, by_id=OFFICER, via="chat", rev=configops.op_rev(conf, None, revs)))
    assert len(ledger(reg)) == 2 and precedents(reg)[0]["reason"] == "tank progression"
    lines = [t for _, _, t in bot.ops.lines]
    assert sum("recorded" in t for t in lines) == 2 and not any(t.startswith("[chat] Officer: recorded") for t in lines), "the verb audits once"
    assert configops.OP_GROUP["loot"] == "loot" and "loot" in configops.SCHEMA_TEXT and "wishlist" in configops.SELF_OPS


def test_a_stale_confirm_all_from_chat_is_refused(w):
    reg, bot = w.reg, w.bot
    reg.config.roster_channel_id = 55
    s, _ = run(bot.open_loot(reg, w.rs, w.ev, by="Officer"))
    tick_one(types.SimpleNamespace(bot=bot, reg=reg, s=s))
    run(bot.distribute(reg, s, by="Officer"))
    conf = ConfigOp(op="loot", field="confirm", target=w.ev.key)
    revs = configops.board_revs(reg, [conf])
    run(bot.override(reg, s, "d1", next(m.main.label for m in w.ms if m.main.label != s.pending("d1").to), "changed my mind", by="Mira", rejudge=False))
    with pytest.raises(RegistryError, match="changed"):
        run(configops.apply_async(reg, conf, "Officer", False, bot=bot, by_id=OFFICER, via="chat", rev=configops.op_rev(conf, None, revs)))
    assert not ledger(reg)


def test_wishlist_op_is_self_and_tier_op_is_officers(reg):
    m = reg.test_members()[0]
    me = ConfigOp(op="wishlist", field="add", value="Robe of Tests", rank="1", text="os", reason="pvp")
    allowed, refused = configops.authorize(reg, [me], m.discord_id, officer=False, channel=PLAIN_WEB)
    assert allowed and not refused and me.member == f"<@{m.discord_id}>", refused  # bind_self: an empty member is the requester
    assert "(OS)" in configops.describe(reg, me)
    assert "+ Robe of Tests (OS)" in configops.apply(reg, me, m.display_name, False)
    other = ConfigOp(op="wishlist", field="add", value="Wand", member=f"<@{reg.test_members()[1].discord_id}>")
    allowed, refused = configops.authorize(reg, [other], m.discord_id, officer=False, channel=PLAIN_WEB)
    assert not allowed and refused
    tiered_item(reg, 90001, "Robe of Tests", tiers={"role:tank": "S"})
    t = ConfigOp(op="tier", target="Robe of Tests", field="Priest:Holy", value="A")
    allowed, _ = configops.authorize(reg, [t], m.discord_id, officer=False, channel=PLAIN_WEB)
    assert not allowed, "tiers are the loot group's (officers)"
    assert "Priest:Holy — → A" in configops.describe(reg, t)
    assert "Priest:Holy A" in configops.apply(reg, t, "Officer", False)
    assert reg.items()["90001"].tiers["Priest:Holy"] == "A"


# ---------------------------------------------------------------- the public loot log

class Chan:
    def __init__(self, cid, name="loot-log"):
        self.id, self.name, self.sent = cid, name, []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw.get("view")))
        return types.SimpleNamespace(id=len(self.sent))


class LogBot(LootLogMixin, LootBot):
    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.channels: dict[int, Chan] = {}

    def get_channel(self, cid):
        return self.channels.get(cid)


def test_public_loot_log_lines_channel_fallback_off_and_rehearsal(reg, rs):
    ev, members = real_run(reg, rs)
    bot = LogBot(reg, rs)
    bot.attach_loot_log()
    try:
        log_ch, upd = Chan(70), Chan(71, "updates")
        bot.channels = {70: log_ch, 71: upd}
        reg.config.loot_channel_id = 70
        ev.updates_thread_id = 71
        s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
        c = types.SimpleNamespace(bot=bot, reg=reg, s=s)
        tick_one(c, copies=3)

        async def flow():
            await bot.distribute(reg, s, by="Officer")
            await bot.confirm(reg, s, only={"d1"}, by="Officer")
            await asyncio.sleep(0)
        run(flow())
        (text, view), = log_ch.sent
        winner = s.awarded("d1").raider
        assert text.startswith("🎁 **Shard of Doom** → ") and winner in text and "reason" not in text.lower()
        assert view.children[0].custom_id == f"lootwish.{GUILD}:{item_key(0, 'Shard of Doom')}"
        other = next(m.main.label for m in members if m.main.label != winner)

        async def correct():
            await bot.correct(reg, s, "d1", other, "misclick in game", by="Officer")
            await asyncio.sleep(0)
        run(correct())
        assert log_ch.sent[-1][0] == f"✎ **Shard of Doom**: was {winner}, now **{other}**"
        reg.config.loot_channel_id = None  # log on, no channel: the run's updates thread + one warn (never the signup channel)

        async def second():
            await bot.confirm(reg, s, only={"d2"}, by="Officer")
            await asyncio.sleep(0)
            await bot.confirm(reg, s, only={"d3"}, by="Officer")
            await asyncio.sleep(0)
        run(second())
        assert len(upd.sent) == 2 and sum("no loot channel is set" in t for _, _, t in bot.ops.lines) == 1
        reg.config.loot_log = False
        n = len(upd.sent) + len(log_ch.sent)
        assert not run(bot.loot_log_post(GUILD, s.awarded("d2"), ev.key, None)) and len(upd.sent) + len(log_ch.sent) == n
    finally:
        lootctx.LEDGER_LISTENERS.clear()


def test_loot_log_line_words(reg):
    from oibot_gm.models import LootAward

    a = LootAward(raider="Kestrel", item_id=0, item_name="Robe", tier="?", total_weight=0.5, offspec=True, received="2026-12-09", instance="BD", boss="Gatekeeper")
    assert loot_log_line(reg, a, None) == "🎁 **Robe** → **Kestrel** (offspec) · Gatekeeper"
    de = a.model_copy(update={"raider": DISENCHANT})
    assert loot_log_line(reg, de, None) == "🎁 **Robe** → disenchanted" and loot_log_line(reg, de, "Kestrel") == "✎ **Robe**: was Kestrel, now disenchanted"


def test_config_carries_the_loot_log_state_and_settings(w):
    w.bot.get_guild = lambda gid: None  # no gateway: channel and role names can't be shown (the config route copes)
    d = w.c.get("/api/config", headers=H).json()
    assert d["settings"]["loot_log"] is True and d["settings"]["loot_log_state"]["state"] == "no_channel" and "loot" in d["channels"]
    # UI audit: Config › Settings printed "Owner: Discord id 1376…" — the page prints the owner's name, never the snowflake
    from oibot_gm.web.app import owner_name

    assert d["settings"]["owner_name"] == owner_name(w.bot, w.reg) and d["settings"]["owner_name"] != d["settings"]["owner_id"]
    r = w.c.post("/api/admin/config", json={"field": "loot_log", "value": False}, headers=H)
    assert r.status_code == 200 and w.reg.config.loot_log is False, r.text
    assert w.c.get("/api/config", headers=H).json()["settings"]["loot_log_state"]["state"] == "off"


def test_ev_json_carries_the_council_and_its_links(w):
    d = w.c.get(f"/api/run/{w.ev.key}", headers=H).json()
    assert d["loot"] == {"open": False, "councils": 0, "thread": None, "awards": 0, "pending": 0, "reasons": 0, "may_open": True}
    opened(w)
    d = w.c.get(f"/api/run/{w.ev.key}", headers=H).json()
    assert d["loot"]["open"] and d["loot"]["thread"].startswith("https://discord.com/channels/1/") and d["links"]["loot"] == d["loot"]["thread"]


# ---------------------------------------------------------------- /me wishlist (the harness)

def test_me_wishlist_wizard_adds_by_select_by_search_and_removes(reg, rs):
    from wizard_harness import Harness

    from oibot_gm.wizard_flows import FLOWS

    tiered_item(reg, 90001, "Robe of Tests")
    m = reg.test_members()[0]
    user = types.SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention="@x")
    h = Harness(reg, rs, user=user)

    async def go():
        await FLOWS["me_wishlist"](h.command(), reg)
        assert "0/10" in h.text and "Me › Wishlist" in h.text
        await h.pick("Add an item…", "90001")
        await h.submit(h.modal, rank="1", kind="os", note="pvp set")
        assert "Add **Robe of Tests** at **#1**" in h.text and "offspec" in h.text
        assert not reg.wish_character(m.discord_id)[1].wishlist, "nothing before Confirm"
        await h.press("Confirm")
        assert "✅" in h.text and "1. Robe of Tests · OS — pvp set" in h.text
        await h.pick("Add an item…", "__search")
        await h.submit(h.modal, q="Sunfire Pauldrons")
        assert "No item called" in h.text
        await h.pick("Pick the item", "__byname")
        await h.submit(h.modal, rank="1", kind="ms", note="")
        assert "Not on the tables yet" in h.text
        await h.press("Confirm")
        wl = reg.wish_character(m.discord_id)[1].wishlist
        assert [(e.name, e.rank, e.item_id) for e in wl] == [("Sunfire Pauldrons", 1, 0), ("Robe of Tests", 2, 90001)]
        await h.press("Remove one…")
        await h.pick("Pick the item", "Robe of Tests")
        await h.press("Remove")
        assert [e.name for e in reg.wish_character(m.discord_id)[1].wishlist] == ["Sunfire Pauldrons"]
        await h.press("Done")
    asyncio.run(go())


def test_the_log_buttons_item_opens_the_wizard_on_it(reg, rs):
    from wizard_harness import Harness

    from oibot_gm.wizard_flows import FLOWS

    tiered_item(reg, 90001, "Robe of Tests")
    m = reg.test_members()[0]
    h = Harness(reg, rs, user=types.SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention="@x"))

    async def go():
        await FLOWS["me_wishlist"](h.command(), reg, item="90001")
        assert "**Robe of Tests**" in h.text
        await h.press("Place and MS/OS…")
        await h.submit(h.modal, rank="1", kind="ms", note="")
        await h.press("Confirm")
    asyncio.run(go())
    assert reg.wish_character(m.discord_id)[1].wishlist[0].item_id == 90001


# ---------------------------------------------------------------- the rest of the routes; a split run's councils

def test_scores_only_keep_open_remove_candidate_and_one_run(w):
    opened(w)
    r = w.c.post(f"/api/loot/{w.ev.key}/scores-only", json={"on": True}, headers=H)
    assert r.status_code == 200 and council(r.json())["scores_only"]["by"] == "the owner", r.text
    r = w.c.post(f"/api/loot/{w.ev.key}/keep-open", json={}, headers=H)
    assert r.status_code == 200 and "Kept open until" in r.json()["message"]
    s = w.bot.events[(GUILD, w.ev.key)]
    gone = next(p for p in w.bot.loot_roster(s))
    s.departed.append(gone)
    r = w.c.post(f"/api/loot/{w.ev.key}/candidate/remove", json={"who": gone.character}, headers=H)
    assert r.status_code == 200 and "no longer a candidate" in r.json()["message"] and not s.departed
    assert w.c.post(f"/api/loot/{w.ev.key}/candidate/remove", json={"who": "nobody"}, headers=H).status_code == 400
    one = w.c.get(f"/api/loot/run/{w.ev.key}", headers=H).json()
    assert one["key"] == w.ev.key and one["councils"][0]["id"] == w.ev.key and "End " in one["end_consequence"]


def test_a_split_runs_councils_are_addressed_by_session_or_by_their_id(reg, rs, env):
    from test_loot_verbs import split_run

    ev, members = split_run(reg, rs)
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))
    r = c.post(f"/api/loot/{ev.key}/open", json={}, headers=H)
    assert r.status_code == 200 and [x["label"] for x in r.json()["state"]["councils"]] == ["Roster 1", "Roster 2"]
    r = c.post(f"/api/loot/{ev.key}/tick", json={"add": [{"name": "Split Shard"}]}, headers=H)
    assert r.status_code == 400 and "one council per roster" in r.json()["error"]
    assert c.post(f"/api/loot/{ev.key}/tick", json={"add": [{"name": "Split Shard"}], "session": "Roster 2"}, headers=H).status_code == 200
    r = c.post(f"/api/loot/{ev.key}-r1/tick", json={"add": [{"name": "Other Shard"}]}, headers=H)
    assert r.status_code == 200, r.text
    s1, s2 = bot.events[(GUILD, f"{ev.key}-r1")], bot.events[(GUILD, f"{ev.key}-r2")]
    assert [d.item_name for d in s1.drop_list] == ["Other Shard"] and [d.item_name for d in s2.drop_list] == ["Split Shard"]


def test_the_commands_exist(reg):
    """/me wishlist and /gm config loot-channel are on the command tree (built offline, as check_commands does); /mock is gone."""
    import discord
    from discord import app_commands

    from oibot_gm.discord_help import register_help_commands
    from oibot_gm.discord_policy import PolicyContext, register_policy_commands
    from oibot_gm.discord_raid import register_raid_commands
    from oibot_gm.discord_registry import register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guilds = Guilds.of(reg.store, reg)
    bot = types.SimpleNamespace(raids=types.SimpleNamespace(store=lambda r: None))
    register_commands(tree, guilds, Ops(client), lambda k, v: "")
    register_raid_commands(tree, guilds, Ops(client), bot)
    register_policy_commands(tree, guilds, Ops(client), bot, PolicyContext(guilds))
    register_help_commands(tree, guilds, Ops(client), bot)
    names = set()

    def walk(c, prefix=""):
        n = f"{prefix} {c.name}".strip()
        for s in getattr(c, "commands", None) or []:
            walk(s, n)
        if not getattr(c, "commands", None):
            names.add(n)
    for c in tree.get_commands():
        walk(c)
    assert {"me wishlist", "gm config loot-channel", "raid loot"} <= names and not any(n.startswith("mock") for n in names)
    from oibot_gm import discord_bot

    assert not hasattr(discord_bot, "RosterView") and not hasattr(discord_bot.OibotGM, "_register"), "/mock's views and commands are gone"


def test_add_to_my_wishlist_button_opens_the_wizard_on_its_item(reg, rs):
    from wizard_harness import Harness

    from oibot_gm.discord_pool import LootWishButton

    tiered_item(reg, 90001, "Robe of Tests")
    m = reg.test_members()[0]
    h = Harness(reg, rs, user=types.SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention="@x"))
    h.client.registries = Guilds.of(reg.store, reg)
    btn = LootWishButton(GUILD, "90001")
    assert btn.item.custom_id == f"lootwish.{GUILD}:90001"

    async def go():
        await btn.callback(h.command())
        assert "**Robe of Tests**" in h.text
        h.guild_id = 999  # another server: nobody's card
        await LootWishButton(GUILD, "90001").callback(h.command())
        assert "Not configured here" in h.text
    asyncio.run(go())


def test_a_single_confirm_from_chat_records_only_the_pick_the_diff_showed(w):
    """The diff said 'record the Shard → X'; someone overrode it to Y before Apply: Apply is refused, nothing recorded."""
    reg, bot = w.reg, w.bot
    reg.config.roster_channel_id = 55
    s, _ = run(bot.open_loot(reg, w.rs, w.ev, by="Officer"))
    tick_one(types.SimpleNamespace(bot=bot, reg=reg, s=s))
    run(bot.distribute(reg, s, by="Officer"))
    shown = s.pending("d1").to
    conf = ConfigOp(op="loot", field="confirm", target=w.ev.key, value="d1")
    assert f"→ {shown}" in configops.describe(reg, conf)
    revs = configops.board_revs(reg, [conf])
    assert configops.op_rev(conf, None, revs) == f"to:{shown}"
    other = next(m.main.label for m in w.ms if m.main.label != shown)
    run(bot.override(reg, s, "d1", other, "changed my mind", by="Mira", rejudge=False))
    with pytest.raises(RegistryError, match="changed"):
        run(configops.apply_async(reg, conf, "Officer", False, bot=bot, by_id=OFFICER, via="chat", rev=configops.op_rev(conf, None, revs)))
    assert not ledger(reg)
    revs = configops.board_revs(reg, [conf])
    run(configops.apply_async(reg, conf, "Officer", False, bot=bot, by_id=OFFICER, via="chat", rev=configops.op_rev(conf, None, revs)))
    assert ledger(reg)[0]["raider"] == other


# ---------------------------------------------------------------- L3 review fixes (regressions)

def test_a_precedent_recorded_at_confirm_can_be_retired_in_the_same_process(w):
    """A precedent written live (an override at Confirm) carries its `ref` at once, like one folded at load: Loot ›
    Precedents, MCP retire_precedent and the plain-text `retire` by item name all reach it without a restart."""
    hdr = H | {"X-Oibot-Actor": str(OFFICER)}
    opened(w)
    tick(w, "Shard of Doom")
    c = council(distribute(w))
    to = next(m.main.label for m in w.ms if m.main.label != c["proposals"][0]["bot"])
    assert w.c.post(f"/api/loot/{w.ev.key}/override", json={"drop_id": "d1", "to": to, "reason": "tank progression"}, headers=hdr).status_code == 200
    r = w.c.post(f"/api/loot/{w.ev.key}/confirm", json={"only": ["d1"], "expect_to": to}, headers=hdr)
    assert r.status_code == 200, r.text
    rows = w.c.get("/api/loot/precedents", headers=H).json()["rows"]
    assert rows[0]["ref"] == f"{w.ev.key}-d1", rows[0]
    r = w.c.post("/api/loot/precedents/retire", json={"ref": rows[0]["ref"], "reason": "old tier"}, headers=hdr)
    assert r.status_code == 200 and "retired" in r.json()["message"], r.text
    back = ConfigOp(op="loot", field="retire", value="Shard of Doom", reason="back in", text="restore")
    assert "restored" in run(configops.apply_async(w.reg, back, "Officer", False, bot=w.bot, by_id=OFFICER, via="chat"))
    assert w.c.get("/api/loot/precedents", headers=H).json()["rows"][0]["status"] == "active"


def test_append_precedent_names_a_row_without_an_import_id_by_its_place(reg):
    ctx = lootctx.RegistryLootContext(reg, reg.store, None)
    ctx.append_precedent({"item": "A", "bot": "x", "human": "y", "reason": "r"})
    ctx.append_precedent({"item": "B", "bot": "x", "human": "y", "reason": "r", "import_id": "r1-d2"})
    assert [p["ref"] for p in ctx.precedents] == ["row-0", "r1-d2"]
    assert [p["ref"] for p in lootctx.RegistryLootContext(reg, reg.store, None).precedents] == ["row-0", "r1-d2"], "the same names after a reload"


def test_a_tmb_preview_changes_nothing_not_even_in_memory(reg):
    """Regression: the preview renumbered the live entries (a native entry after the TMB rows jumped to #1), so the
    member's open Me › Wishlist got a 409 and the next save of that member wrote duplicate ranks."""
    a = reg.test_members()[0]
    text = ("character_name,item_name,item_id,sort_order,is_offspec,note,received_at\n"
            f"{a.main.label},Gloves A,0,1,0,,\n{a.main.label},Helm B,0,2,0,,\n")
    reg.import_tmb_wishlists(text, "Officer")
    reg.wish_add(a.discord_id, None, "Sash", by="t")
    c = reg.wish_character(a.discord_id)[1]
    before = [(e.name, e.rank, e.source) for e in c.wishlist]
    assert before == [("Gloves A", 1, "tmb"), ("Helm B", 2, "tmb"), ("Sash", 3, "native")]
    rev = reg.wishlist_rev(a, c.label)
    pv = reg.import_tmb_wishlists(text, "Officer", preview=True)
    assert pv["characters"] == 1
    assert [(e.name, e.rank, e.source) for e in reg.wish_character(a.discord_id)[1].wishlist] == before
    assert reg.wishlist_rev(a, c.label) == rev


def _off_loop(monkeypatch, reg, name):
    import threading

    seen: list = []
    real = getattr(reg, name)

    def rec(*a, **k):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(reg, name, rec)
    return seen


def test_plain_text_wishlist_and_tier_ops_commit_off_the_event_loop(w, monkeypatch):
    m = w.ms[0]
    seen = _off_loop(monkeypatch, w.reg, "wish_add")
    op = ConfigOp(op="wishlist", field="add", value="Robe of Tests", member=f"<@{m.discord_id}>")
    assert "+ Robe of Tests" in run(configops.apply_async(w.reg, op, m.display_name, False, bot=w.bot, by_id=m.discord_id, via="chat"))
    assert seen == [False], "the git commit ran on the event loop"
    tiered_item(w.reg, 90001, "Robe of Tests")
    tiers = _off_loop(monkeypatch, w.reg, "set_item_tiers")
    t = ConfigOp(op="tier", target="Robe of Tests", field="Priest:Holy", value="A")
    assert "Priest:Holy A" in run(configops.apply_async(w.reg, t, "Officer", False, bot=w.bot, by_id=OFFICER, via="chat"))
    assert tiers == [False]


def test_me_wishlist_wizard_saves_and_removes_off_the_event_loop(reg, rs, monkeypatch):
    from wizard_harness import Harness

    from oibot_gm.wizard_flows import FLOWS

    tiered_item(reg, 90001, "Robe of Tests")
    m = reg.test_members()[0]
    adds, removes = _off_loop(monkeypatch, reg, "wish_add"), _off_loop(monkeypatch, reg, "wish_remove")
    h = Harness(reg, rs, user=types.SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention="@x"))

    async def go():
        await FLOWS["me_wishlist"](h.command(), reg)
        await h.pick("Add an item…", "90001")
        await h.submit(h.modal, rank="1", kind="ms", note="")
        await h.press("Confirm")
        await h.press("Remove one…")
        await h.pick("Pick the item", "Robe of Tests")
        await h.press("Remove")
    asyncio.run(go())
    assert adds == [False] and removes == [False]
    assert not reg.wish_character(m.discord_id)[1].wishlist


def test_wishlist_provenance_says_where_the_lists_came_from(reg):
    """The provenance the model reads names who entered the wishlists (D5): through the bot, from a TMB import, or the
    older guild file — never 'guild file' for lists the members made."""
    ctx = lootctx.RegistryLootContext(reg, reg.store, None)
    a, b = reg.test_members()[:2]
    ctx._legacy_wishlists = {}
    assert "Wishlists: none recorded." in ctx.provenance
    ctx._legacy_wishlists = {"Oldname": [{"item": 1, "rank": 1}], a.main.label: [{"item": 2, "rank": 1}]}
    assert "Wishlists: 2 from an older guild wishlist file." in ctx.provenance
    reg.wish_add(a.discord_id, None, "Sash", by="t")
    reg.import_tmb_wishlists(f"character_name,item_name,item_id,sort_order,is_offspec,note,received_at\n{b.main.label},Helm B,0,1,0,,\n", "Officer")
    p = ctx.provenance
    assert "1 character(s) with entries made through the bot" in p and "1 with entries imported from a ThatsMyBIS export" in p
    assert "1 from an older guild wishlist file" in p, "a character with its own list no longer counts as the file's"


# ---------------------------------------------------------------- final review fixes (each fails without its fix)

def _replaced_reason(w):
    """A companion award against the bot's pick, its reason given and then replaced: two precedent rows, one ref."""
    opened(w)
    tick(w, "Shard of Doom")
    c = council(distribute(w))
    p = c["proposals"][0]
    other = next(m.main.label for m in w.ms if m.main.label != p["to"])
    from oibot_gm.feed import Companion

    run(w.bot.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "fx1"}, Companion("Scribe", "c", "", "p")))
    s = w.bot.events[(GUILD, w.ev.key)]
    run(w.bot.set_reason(w.reg, s, "d1", "first reason", by="Mira"))
    run(w.bot.set_reason(w.reg, s, "d1", "replaced reason", by="Mira", replace=True))
    return f"{w.ev.key}-d1"


def test_a_precedent_whose_reason_was_replaced_can_be_retired_on_every_surface(w):
    ref = _replaced_reason(w)
    ctx = lootctx.RegistryLootContext(w.reg, w.reg.store, None)
    assert [(p["ref"], p["status"], p["reason"]) for p in ctx.precedents] == [(ref, "superseded", "first reason"), (ref, "active", "replaced reason")]
    hdr = H | {"X-Oibot-Actor": str(OFFICER)}
    r = w.c.post("/api/loot/precedents/retire", json={"ref": ref, "reason": "old tier"}, headers=hdr)
    assert r.status_code == 200 and "retired" in r.json()["message"], r.text
    assert lootctx.RegistryLootContext(w.reg, w.reg.store, None).precedent_for(ref) is None, "the live (newer) one is retired"
    back = ConfigOp(op="loot", field="retire", value=ref, reason="back in", text="restore")
    assert "replaced reason" in configops.describe(w.reg, back), "the plain-text diff names the newer reason"
    assert "restored" in run(configops.apply_async(w.reg, back, "Officer", False, bot=w.bot, by_id=OFFICER, via="chat"))
    assert lootctx.RegistryLootContext(w.reg, w.reg.store, None).precedent_for(ref)["reason"] == "replaced reason"


def test_retiring_a_companion_awards_reason_never_puts_it_back_on_the_needs_a_reason_list(w):
    ref = _replaced_reason(w)
    hdr = H | {"X-Oibot-Actor": str(OFFICER)}
    assert w.c.post("/api/loot/precedents/retire", json={"ref": ref, "reason": "old tier"}, headers=hdr).status_code == 200
    assert w.c.get("/api/loot/precedents", headers=H).json()["needs_reason"] == [], "a retired reason was still given"


def test_end_refuses_an_unlocked_run_on_the_web_mcp_and_plain_text(w):
    from datetime import timedelta

    from oibot_gm import raidcycle as rc

    start = (w.reg.now_local() + timedelta(days=9)).replace(second=0, microsecond=0)
    sheet = rc.open_run(w.reg, w.rs, "barrow_deeps", start, by="t")  # next week's open signup sheet
    r = w.c.post(f"/api/loot/{sheet.key}/end", json={}, headers=H)
    assert r.status_code == 400 and "No locked run to end" in r.json()["error"], r.text
    assert w.c.post(f"/api/loot/{sheet.key}/end", json={"preview": True}, headers=H).status_code == 400
    op = ConfigOp(op="loot", field="end", target=sheet.key)
    from oibot_gm import loot_ops

    with pytest.raises(RegistryError, match="No locked run to end"):
        loot_ops.describe(w.reg, op)
    assert "No locked run to end" in configops.describe(w.reg, op), "the plain-text diff says so before Apply"
    with pytest.raises(RegistryError, match="No locked run to end"):
        run(configops.apply_async(w.reg, op, "Officer", False, bot=w.bot, by_id=OFFICER, via="chat"))  # Apply: the verb refuses too
    assert w.rs.events[sheet.key].state == "open", "the signup sheet lives on"
    assert w.c.post(f"/api/loot/{w.ev.key}/end", json={}, headers=H).status_code == 200, "a locked run still ends"


def test_the_precedent_retire_route_commits_off_the_event_loop(w, monkeypatch):
    w.reg.store.append_jsonl(Path(w.reg.key) / "precedents.jsonl", {"item": "Syphon", "bot": "Torsha", "human": "Eltam", "reason": "tanks", "import_id": "r1-d1"})
    w.bot.__dict__.get("contexts", {}).clear()
    seen: list[bool] = []
    real = w.reg.store.commit

    def rec(*a, **k):
        try:
            asyncio.get_running_loop()
            seen.append(True)  # called on the event loop's thread
        except RuntimeError:
            seen.append(False)
        return real(*a, **k)
    monkeypatch.setattr(w.reg.store, "commit", rec)
    r = w.c.post("/api/loot/precedents/retire", json={"ref": "r1-d1", "reason": "old tier"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and seen and not any(seen), "the git commit ran on the event loop"


def test_me_wishlist_wizard_answers_before_the_write(reg, rs, monkeypatch):
    from wizard_harness import Harness

    from oibot_gm.wizard_flows import FLOWS

    tiered_item(reg, 90001, "Robe of Tests")
    m = reg.test_members()[0]
    h = Harness(reg, rs, user=types.SimpleNamespace(id=m.discord_id, display_name=m.display_name, mention="@x"))
    shown: list[str] = []
    for name in ("wish_add", "wish_remove"):
        real = getattr(reg, name)

        def rec(*a, _real=real, **k):
            shown.append(h.text)  # what the person sees while the write runs
            return _real(*a, **k)
        monkeypatch.setattr(reg, name, rec)

    async def go():
        await FLOWS["me_wishlist"](h.command(), reg)
        await h.pick("Add an item…", "90001")
        await h.submit(h.modal, rank="1", kind="ms", note="")
        await h.press("Confirm")
        await h.press("Remove one…")
        await h.pick("Pick the item", "Robe of Tests")
        await h.press("Remove")
    asyncio.run(go())
    assert len(shown) == 2 and "Saving…" in shown[0] and "Removing…" in shown[1], shown


# ---------------------------------------------------------------- integration cleanup (each fails without its fix)

def test_every_surface_retires_a_precedent_through_the_one_verb(w, monkeypatch):
    """Regression (integration item 2): /gm precedents had LootMixin.retire_precedent (its own status row, by import_id
    only) while the site, plain text and MCP wrote through RegistryLootContext.set_precedent_status — two
    implementations. Now every surface calls the verb, which wraps the context's; a precedent with no import_id (an
    imported row, ref 'row-<n>') is reached too."""
    w.reg.store.append_jsonl(Path(w.reg.key) / "precedents.jsonl", {"item": "Syphon", "bot": "Torsha", "human": "Eltam", "reason": "tanks"})
    w.bot.__dict__.get("contexts", {}).clear()
    calls: list[tuple] = []
    real = type(w.bot).retire_precedent

    async def spy(self, reg, ref, reason, **kw):
        calls.append((ref, kw.get("via"), kw.get("restore", False)))
        return await real(self, reg, ref, reason, **kw)
    monkeypatch.setattr(type(w.bot), "retire_precedent", spy)
    r = w.c.post("/api/loot/precedents/retire", json={"ref": "row-0", "reason": "old tier"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and "Precedent retired" in r.json()["message"], r.text
    back = ConfigOp(op="loot", field="retire", value="Syphon", reason="back in", text="restore")
    assert "Precedent restored" in run(configops.apply_async(w.reg, back, "Officer", False, bot=w.bot, by_id=OFFICER, via="chat"))
    assert calls == [("row-0", "mcp", False), ("row-0", "chat", True)]
    assert lootctx.RegistryLootContext(w.reg, w.reg.store, None).precedents[0]["status"] == "active"
    lines = [t for _, _, t in w.bot.ops.lines if "Precedent" in t]
    assert len(lines) == 2 and lines[0].startswith("[mcp] u05 via MCP") and lines[1].startswith("[chat] Officer"), "one audit line each, on its surface"


def _data_files(reg) -> dict[str, bytes]:
    root = reg.store.root / reg.key
    return {str(f.relative_to(root)): f.read_bytes() for f in sorted(root.rglob("*")) if f.is_file()}


def test_a_preview_flag_never_skips_the_actor_on_a_decision_route(w):
    """Regression (integration item 5, D8): `"preview": true` skipped the X-Oibot-Actor check on EVERY decision route,
    and only confirm handled a preview — an MCP override / reason / correction / retire / tiers / someone's wishlist
    with that flag was written as the owner's token alone. Now a preview passes without the actor only where the route
    answers it before any verb (confirm, the TMB import); everywhere else it is a 403 and nothing is written."""
    from oibot_gm.feed import Companion

    opened(w)
    tick(w, "Shard of Doom", "Wand of Tests")
    c = council(distribute(w))
    p1, p2 = c["proposals"]
    other = {p["drop_id"]: next(m.main.label for m in w.ms if m.main.label != p["to"]) for p in (p1, p2)}
    run(w.bot.handle_feed_event({"type": "loot", "recipient": other["d1"], "item_name": p1["item"], "idem": "pv1"}, Companion("Scribe", "c", "", "p")))
    w.reg.store.append_jsonl(Path(w.reg.key) / "precedents.jsonl", {"item": "Syphon", "bot": "Torsha", "human": "Eltam", "reason": "tanks", "import_id": "r1-d1"})
    w.bot.__dict__.get("contexts", {}).clear()
    tiered_item(w.reg, 90001, "Robe of Tests")
    m = w.ms[0]
    csv = f"character_name,item_name,item_id,sort_order,is_offspec,note,received_at\n{m.main.label},Gloves A,0,1,0,,\n"
    s = w.bot.events[(GUILD, w.ev.key)]
    before, pending = _data_files(w.reg), [(p.drop_id, p.to, p.override) for p in s.proposals]
    key = w.ev.key
    refused = [(f"/api/loot/{key}/override", {"drop_id": "d2", "to": other["d2"], "reason": "tank progression"}),
               (f"/api/loot/{key}/reason", {"drop_id": "d1", "reason": "she tanks tonight"}),
               (f"/api/loot/{key}/correct", {"drop_id": "d1", "to": p1["to"], "reason": "misclick in game"}),
               ("/api/loot/precedents/retire", {"ref": "r1-d1", "reason": "old tier"}),
               ("/api/members/wishlist", {"member": m.display_name, "rows": [{"name": "Robe of Tests"}]}),
               ("/api/loot/items/90001/tiers", {"tiers": {"Priest:Holy": "B"}, "reweight": False})]
    for path, body_ in refused:
        r = w.c.post(path, json=body_ | {"preview": True}, headers=H)
        assert r.status_code == 403 and "X-Oibot-Actor" in r.json()["error"], (path, r.status_code, r.text)
    r = w.c.post(f"/api/loot/{key}/confirm", json={"preview": True}, headers=H)
    assert r.status_code == 200 and r.json()["preview"].startswith("Record "), r.text
    r = w.c.post("/api/loot/wishlists/import", json={"csv": csv, "preview": True}, headers=H)
    assert r.status_code == 200 and "preview" in r.json()["message"], r.text
    assert _data_files(w.reg) == before, "no session, ledger, precedent, member or item file changed"
    assert [(p.drop_id, p.to, p.override) for p in s.proposals] == pending, "nothing changed in memory either"
