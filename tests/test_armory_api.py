"""The armory link on the site and through MCP (armory link §5.2, §5.4, §5.5; web/api_armory.py): a member links,
unlinks, switches and syncs their OWN characters; officers do it for anyone, see the map, run the seed, give a guild
character nobody claims to a member and move a character to another member. Every write previews first; MCP writes need
the actor (D8); a version without an armory answers 404 everywhere. A fake armory stands in for Blizzard; every name is
invented (the "Lantern Wardens" roster fixture; members Tessa, Orla, Wren)."""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from conftest import OFFICER_ROLE, OWNER
from test_armory_link import FakeArmory
from test_people_web import OFFICER, TOKEN, as_user, person, w  # noqa: F401 — `w` is the fixture

from oibot_gm import armory, armory_seed, configops, discord_armory
from oibot_gm import consequences as cq
from oibot_gm import mcp_server as ms
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import Registry
from oibot_gm.web import api_armory

TESSA, ORLA, WREN = 470_000_000_000_000_101, 470_000_000_000_000_102, 470_000_000_000_000_103
ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeArmory()
    monkeypatch.setattr(armory, "client_for", lambda reg: f)
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    monkeypatch.setattr(discord_armory, "OUT", tmp_path / "armory-out")
    discord_armory.reset_state()
    yield f
    discord_armory.reset_state()


@pytest.fixture
def g(w, fake):
    """Tessa registered "Bram" (Warrior), Orla "Brom" (Warrior), Wren "Pip" (Rogue) — all in the Discord server; the
    guild is linked and its roster read."""
    reg = w.reg
    for uid, name, char, cls, spec in ((TESSA, "Tessa", "Bram", "Warrior", "Protection"), (ORLA, "Orla", "Brom", "Warrior", "Arms"),
                                       (WREN, "Wren", "Pip", "Rogue", "Combat")):
        reg.add_character(uid, name, char, cls, spec, None, True)
        w.bot.admit(reg.members[uid])
    reg.set_armory_guild(fake.guild_facts, "Owner", by_id=OWNER)
    run(discord_armory.roster_refresh(w.bot, reg))
    w.sent = []
    w.c.app.state.publish = lambda topic, guild=None, **kw: w.sent.append((topic, guild))
    return w


def audits(w, word: str) -> list[str]:
    return [t for _lvl, t in w.bot.ops.lines if word in t]


# ---------------------------------------------------------------- the member's own character

def test_a_member_finds_previews_and_links_their_own_character(g):
    reg, h = g.reg, as_user(g, TESSA)
    before = commits(reg)
    r = g.c.post("/api/me/armory/find", json={"label": "Bram", "surname": "Kettle"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["found"] == {"id": 700101, "name": "Bram", "surname": "Kettle", "label": "Bram Kettle", "cls": "Warrior", "level": 60, "source": "profile"}
    m = reg.members[TESSA]
    found = discord_armory.Found(id=700101, slug="bram-kettle", name="Bram", surname="Kettle", cls="Warrior", level=60, source="profile")
    assert d["sentences"] == cq.armory_link_consequence(reg, m, m.main, found, TESSA)
    assert g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle", "preview": True}, headers=h).json()["sentences"] == d["sentences"]
    assert commits(reg) == before and reg.members[TESSA].main.armory is None, "find and preview write nothing"
    r = g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == api_armory.LOOK_FIRST
    r = g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle", "armory_id": 5}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == api_armory.FOUND_ANOTHER and r.json()["found"]["id"] == 700101
    assert reg.members[TESSA].main.armory is None and not g.sent
    r = g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["message"] == "Bram Kettle is linked to the armory."
    a = out["character"]["armory"]
    assert a["linked"] and a["id"] == 700101 and a["level"] == 60 and a["in_guild"] is True and a["missing"] is False
    assert a["updated"].startswith("Updated from the armory ") and out["character"]["profession_sync"] is True and out["character"]["flags"] == []
    assert reg.members[TESSA].main.label == "Bram Kettle" and reg.members[TESSA].main.armory.id == 700101
    assert len(audits(g, "linked Tessa's Bram to the armory")) == 1 and ("member", reg.key) in g.sent
    me = g.c.get("/api/me", headers=h).json()["characters"][0]
    assert me["armory"]["linked"] and me["profession_sync"] is True and me["flags"] == []


def test_not_found_is_one_sentence(g):
    r = g.c.post("/api/me/armory/find", json={"label": "Bram", "surname": "Nobody"}, headers=as_user(g, TESSA))
    assert r.status_code == 400 and r.json()["error"] == discord_armory.NOT_FOUND


def test_a_member_never_acts_on_another_members_character(g):
    reg, h = g.reg, as_user(g, ORLA)
    for verb in ("find", "link", "unlink", "switch", "sync"):
        r = g.c.post(f"/api/members/armory/{verb}", json={"uid": str(TESSA), "label": "Bram", "surname": "Kettle", "on": False, "armory_id": 700101}, headers=h)
        assert r.status_code == 403, (verb, r.text)
    # the member's route never reads a uid: it acts on Orla's own record, which has no Bram
    r = g.c.post("/api/me/armory/link", json={"uid": str(TESSA), "label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=h)
    assert r.status_code == 400 and "Orla has no character named Bram" in r.json()["error"]
    assert reg.members[TESSA].main.armory is None
    for path in ("/api/armory/map", "/api/armory/seed"):
        assert g.c.get(path, headers=h).status_code == 403
    for path in ("/api/armory/seed/apply", "/api/members/move", "/api/members/armory/give"):
        assert g.c.post(path, json={"preview": True}, headers=h).status_code == 403


def test_unlink_switch_and_sync_now_preview_then_write(g, fake):
    reg, h = g.reg, as_user(g, TESSA)
    assert g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=h).status_code == 200
    before, g.sent[:] = commits(reg), []
    p = g.c.post("/api/me/armory/switch", json={"label": "Bram Kettle", "on": False, "preview": True}, headers=h).json()
    assert p == {"preview": True, "sentences": cq.profession_sync_consequence(False, "Bram Kettle", current=True)}
    p = g.c.post("/api/me/armory/unlink", json={"label": "Bram Kettle", "preview": True}, headers=h).json()
    m = reg.members[TESSA]
    assert p["sentences"] == cq.armory_unlink_consequence(reg, m, m.main, TESSA)
    assert commits(reg) == before and not g.sent and reg.members[TESSA].main.profession_sync is True
    assert g.c.post("/api/me/armory/switch", json={"label": "Bram Kettle", "on": "no"}, headers=h).status_code == 400
    r = g.c.post("/api/me/armory/switch", json={"label": "Bram Kettle", "on": False}, headers=h)
    assert r.status_code == 200 and r.json()["message"] == "Bram Kettle's professions are kept by hand." and r.json()["character"]["profession_sync"] is False
    assert ("member", reg.key) in g.sent
    # Sync now: the armory answers with a new level → updated; again at once → limited
    fake.chars["bram-kettle"] = armory.CharacterFacts(id=700101, name="Bram", surname="Kettle", cls="Warrior", level=61, guild_id=418362,
                                                      modified="Fri, 13 Nov 2026 10:00:00 GMT")
    g.sent[:] = []
    r = g.c.post("/api/me/armory/sync", json={"label": "Bram Kettle"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "updated" and r.json()["message"] == "Bram Kettle is updated from the armory." and r.json()["character"]["armory"]["level"] == 61
    assert ("member", reg.key) in g.sent
    r = g.c.post("/api/me/armory/sync", json={"label": "Bram Kettle"}, headers=h).json()
    assert r["result"] == "limited" and r["message"] == discord_armory.SYNC_LIMITED
    r = g.c.post("/api/me/armory/unlink", json={"label": "Bram Kettle"}, headers=h)
    assert r.status_code == 200 and r.json()["message"] == "Bram Kettle is unlinked from the armory." and r.json()["character"]["armory"]["linked"] is False
    r = g.c.post("/api/me/armory/unlink", json={"label": "Bram Kettle"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "Bram Kettle isn't linked to the armory."


# ---------------------------------------------------------------- officers

def test_an_officer_links_for_a_member_and_a_stale_officer_waits(g):
    reg, h = g.reg, as_user(g, OFFICER)
    d = g.c.post("/api/members/armory/find", json={"uid": str(TESSA), "label": "Bram", "surname": "Kettle"}, headers=h).json()
    assert d["sentences"][-1] == "They see the link on their Me page, and you can unlink it at any time."
    sid = reg.config.discord_guild_id
    g.bot.__dict__["_member_last_yes"] = {(sid, OFFICER): (time.time(), person(OFFICER, OFFICER_ROLE))}
    g.bot.directory.flaky.add((sid, OFFICER))
    r = g.c.post("/api/members/armory/link", json={"uid": str(TESSA), "label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=as_user(g, OFFICER))
    assert r.status_code == 503 and "can't confirm your officer role" in r.json()["error"]
    assert reg.members[TESSA].main.armory is None
    g.bot.directory.flaky.clear()
    r = g.c.post("/api/members/armory/link", json={"uid": str(TESSA), "label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=as_user(g, OFFICER))
    assert r.status_code == 200, r.text
    assert reg.members[TESSA].main.armory.id == 700101 and reg.members[TESSA].main.armory.linked_by == "u015"


def test_the_map_shows_officers_every_link_and_the_unclaimed(g):
    reg = g.reg
    assert g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=as_user(g, TESSA)).status_code == 200
    d = g.c.get("/api/armory/map", headers=as_user(g, OFFICER)).json()
    by = {m["display_name"]: m for m in d["members"]}
    [bram] = by["Tessa"]["characters"]
    assert bram["label"] == "Bram Kettle" and bram["linked"] and bram["armory_id"] == 700101 and bram["in_guild"] is True
    assert bram["level"] == 60 and bram["updated"].startswith("Updated from the armory") and bram["profession_sync"] and bram["flags"] == []
    [brom] = by["Orla"]["characters"]
    assert brom["linked"] is False and brom["in_guild"] is None and brom["updated"] is None and brom["flags"] == []
    ids = [r["id"] for r in d["unclaimed"]]
    assert 700101 not in ids and 700105 in ids and len(ids) == 7
    assert d["unclaimed"][0].keys() == {"id", "name", "surname", "label", "cls", "level"}
    assert d["roster"]["guild"] == "Lantern Wardens" and d["roster"]["read"] and d["roster"]["words"] is None
    assert d["counts"] == {"linked": 1, "not_linked": len([c for m in d["members"] for c in m["characters"]]) - 1, "unclaimed": 7}
    assert any(m["test"] for m in d["members"]), "the test bench shows as the Members page shows it (no rehearsal live: every officer)"
    reg.clear_armory_guild("Owner", by_id=OWNER)
    d = g.c.get("/api/armory/map", headers=as_user(g, OFFICER)).json()
    assert d["roster"] == {"guild": None, "read": None, "words": api_armory.NO_GUILD_YET} and d["unclaimed"] == []
    rows = g.c.get("/api/members", headers=as_user(g, OFFICER)).json()["rows"]
    tessa = next(r for r in rows if r["display_name"] == "Tessa")
    assert tessa["characters"][0]["armory"]["linked"] and tessa["characters"][0]["armory"]["in_guild"] is None, "not known without a guild"


def test_the_seed_previews_refuses_a_stale_rev_and_links_the_picks(g):
    reg, h = g.reg, as_user(g, OFFICER)
    d = g.c.get("/api/armory/seed", headers=h).json()
    sure = {r["label"]: r for r in d["sure"]}
    assert sure["Bram"]["candidate"]["id"] == 700101 and sure["Bram"]["member"] == "Tessa" and d["counts"]["sure"] == len(d["sure"])
    assert d["roster"]["read"] and d["rev"]
    picks = [{"uid": sure["Bram"]["uid"], "label": "Bram", "armory_id": 700101}, {"uid": str(WREN), "label": "Pip", "armory_id": 700101}]
    before = commits(reg)
    r = g.c.post("/api/armory/seed/apply", json={"rev": "stale", "picks": picks}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == api_armory.SEED_CHANGED and r.json()["rev"] == d["rev"]
    assert g.c.post("/api/armory/seed/apply", json={"rev": d["rev"], "picks": []}, headers=h).json()["error"] == api_armory.NO_PICKS
    p = g.c.post("/api/armory/seed/apply", json={"rev": d["rev"], "picks": picks, "preview": True}, headers=h).json()
    # the preview checks each pick before the armory is asked (no row is held yet, so both pass; the apply then finds
    # 700101 held by the first link and refuses the second)
    assert p["sentences"] == cq.armory_seed_consequence(2, 0) and [r["ok"] for r in p["results"]] == [True, True] and commits(reg) == before
    r = g.c.post("/api/armory/seed/apply", json={"rev": d["rev"], "picks": [{"uid": "x"}]}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == api_armory.PICK_SHAPE and "{" not in r.json()["error"]
    r = g.c.post("/api/armory/seed/apply", json={"rev": d["rev"], "picks": picks}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["linked"] == 1 and [x["ok"] for x in out["results"]] == [True, False]
    assert out["results"][1]["text"].endswith("Unlink it there first.") and out["rev"] != d["rev"]
    assert reg.members[TESSA].main.armory.id == 700101 and ("member", reg.key) in g.sent


def test_give_links_an_existing_character_or_registers_one_through_the_add_path(g):
    reg, h = g.reg, as_user(g, OFFICER)
    before = commits(reg)
    # an existing character of that member: the link
    p = g.c.post("/api/members/armory/give", json={"uid": str(WREN), "armory_id": 700105, "label": "Pip", "preview": True}, headers=h).json()
    assert p["found"]["source"] == "roster" and p["sentences"][0] == "The guild's roster shows Pip Wobble, a level 1 Rogue."
    # a new character: the spec is required, the member must exist, nothing is written by a preview
    r = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": 700103, "preview": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "Pick Tamsin Pell's spec to register it."
    r = g.c.post("/api/members/armory/give", json={"uid": "470000000000000999", "armory_id": 700103, "spec": "Balance"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == Registry.GIVE_NOT_REGISTERED and 470000000000000999 not in reg.members
    r = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": 1, "spec": "Balance"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == armory_seed.ROW_GONE
    r = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": 700103, "spec": "Nonsense", "preview": True}, headers=h)
    assert r.status_code == 400, "the add path's own refusal"
    p = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": 700103, "spec": "Balance", "preview": True}, headers=h).json()
    assert p["sentences"][0] == "Tamsin Pell, a level 60 Druid, is registered to Orla as an alt."
    assert commits(reg) == before and len(reg.members[ORLA].active()) == 1 and not g.sent
    r = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": 700103, "spec": "Balance"}, headers=h)
    assert r.status_code == 200, r.text
    c = next(x for x in reg.members[ORLA].active() if x.label == "Tamsin Pell")
    assert c.cls == "Druid" and c.spec == "Balance" and not c.is_main and c.armory.id == 700103
    assert r.json()["message"] == "Tamsin Pell is registered to Orla and linked to the armory." and ("member", reg.key) in g.sent
    assert r.json()["registered"] is True and r.json()["linked"] is True and r.json()["character"]["armory"]["linked"]
    r = g.c.post("/api/members/armory/give", json={"uid": str(WREN), "armory_id": 700103, "spec": "Balance"}, headers=h)
    assert r.status_code == 400 and "already registered to Orla" in r.json()["error"], "the add path's own refusal, nothing written"
    r = g.c.post("/api/members/armory/give", json={"uid": str(WREN), "armory_id": 700103, "label": "Pip", "preview": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"].endswith("Unlink it there first."), "link_refusal's own sentence"


def test_move_previews_then_moves_for_officers_only(g):
    reg, h = g.reg, as_user(g, OFFICER)
    body = {"uid": str(WREN), "label": "Pip", "to": str(ORLA)}
    p = g.c.post("/api/members/move", json={**body, "preview": True}, headers=h).json()
    assert p["sentences"] == cq.move_consequence(reg, reg.members[WREN], reg.members[WREN].main, reg.members[ORLA])
    assert any(c.label == "Pip" for c in reg.members[WREN].active())
    assert g.c.post("/api/members/move", json=body, headers=as_user(g, TESSA)).status_code == 403
    r = g.c.post("/api/members/move", json=body, headers=as_user(g, OFFICER))  # the session is a cookie: back to the officer
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Pip moved to Orla." and any(c.label == "Pip" for c in reg.members[ORLA].active())
    assert len(audits(g, "moved Wren's Pip to Orla")) == 1 and ("member", reg.key) in g.sent


# ---------------------------------------------------------------- MCP, tbc, parity, SSE

def test_mcp_reads_freely_and_writes_only_with_the_actor(g, monkeypatch):
    reg = g.reg

    def handler(request: httpx.Request) -> httpx.Response:
        r = g.c.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "application/json")})
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    monkeypatch.delenv(ms.ACTOR_ENV, raising=False)
    try:
        assert ms.armory_map()["counts"]["unclaimed"] == 8
        seed = ms.armory_seed()
        assert seed["counts"]["sure"] >= 1 and seed["rev"]
        p = ms.link_character("Tessa", "Bram", "Kettle", preview=True)
        assert "The armory shows Bram Kettle, a level 60 Warrior." in p and "armory_id=700101" in p
        with pytest.raises(ms.ToolError, match="Preview first"):
            ms.link_character("Tessa", "Bram", "Kettle")
        with pytest.raises(ms.ToolError, match="OIBOT_MCP_ACTOR"):
            ms.link_character("Tessa", "Bram", "Kettle", armory_id=700101)
        with pytest.raises(ms.ToolError, match="OIBOT_MCP_ACTOR"):
            ms.move_character("Wren", "Pip", "Orla")
        with pytest.raises(ms.ToolError, match="Preview first"):
            ms.apply_armory_seed([{"member": "Tessa", "character": "Bram", "armory_id": 700101}])
        with pytest.raises(ms.ToolError, match="OIBOT_MCP_ACTOR"):
            ms.apply_armory_seed([{"member": str(TESSA), "character": "Bram", "armory_id": 700101}], rev=seed["rev"])
        assert reg.members[TESSA].main.armory is None and reg.members[WREN].main.label == "Pip"
        assert "moves to Orla" in ms.move_character("Wren", "Pip", "Orla", preview=True)
        monkeypatch.setenv(ms.ACTOR_ENV, str(OFFICER))
        assert ms.link_character("Tessa", "Bram", "Kettle", armory_id=700101) == "Bram Kettle is linked to the armory."
        assert "stops following the armory" in ms.unlink_character("Tessa", "Bram Kettle", preview=True)
        assert ms.unlink_character("Tessa", "Bram Kettle") == "Bram Kettle is unlinked from the armory."
        seed = ms.armory_seed()
        assert "Linked 1." in ms.apply_armory_seed([{"member": "Tessa", "character": "Bram Kettle", "armory_id": 700101}], rev=seed["rev"])
        assert ms.move_character("Wren", "Pip", "Orla") == "Pip moved to Orla."
    finally:
        ms.use(None)
    assert any(t.startswith("[mcp]") and "linked Tessa's Bram" in t for _l, t in g.bot.ops.lines)


def test_tbc_has_no_armory_routes_and_no_armory_on_a_row(w, fake, monkeypatch):
    reg = w.reg
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)
    w.bot.admit(reg.members[TESSA])
    oh = as_user(w, OFFICER)
    for path in ("/api/armory/map", "/api/armory/seed"):
        assert w.c.get(path, headers=oh).status_code == 404, path
    for verb in ("find", "link", "unlink", "switch", "sync"):
        assert w.c.post(f"/api/me/armory/{verb}", json={"label": "Bram", "preview": True}, headers=as_user(w, TESSA)).status_code == 404, verb
        assert w.c.post(f"/api/members/armory/{verb}", json={"uid": str(TESSA), "label": "Bram", "preview": True}, headers=as_user(w, OFFICER)).status_code == 404, verb
    for path in ("/api/armory/seed/apply", "/api/members/armory/give"):
        assert w.c.post(path, json={"uid": str(TESSA), "preview": True}, headers=as_user(w, OFFICER)).status_code == 404, path
    # R11: a move needs no armory — it previews and moves on tbc too
    reg.add_character(ORLA, "Orla", "Brom", "Warrior", "Arms", None, True)
    w.bot.admit(reg.members[ORLA])
    p = w.c.post("/api/members/move", json={"uid": str(TESSA), "label": "Bram", "to": str(ORLA), "preview": True}, headers=as_user(w, OFFICER))
    assert p.status_code == 200 and p.json()["sentences"], p.text
    r = w.c.post("/api/members/move", json={"uid": str(TESSA), "label": "Bram", "to": str(ORLA)}, headers=as_user(w, OFFICER))
    assert r.status_code == 200 and r.json()["message"] == "Bram moved to Orla." and r.json()["character"]["armory"] is None
    row = w.c.get("/api/me", headers=as_user(w, ORLA)).json()["characters"][0]
    assert row["armory"] is None and row["flags"] == [] and row["profession_sync"] is True
    assert not fake.calls, "nothing asked of the armory"


def test_the_armory_link_is_web_and_mcp_now_and_never_plain_text(reg, rs):
    from test_parity_matrix import ARMORY, ARMORY_DISCORD_PENDING  # the matrix rows (the Discord side lands with the wizards)

    names = {t.name for t in run(ms.mcp.list_tools())}
    assert {"armory_map", "armory_seed", "link_character", "unlink_character", "move_character", "apply_armory_seed"} <= names
    assert not {"set_profession_sync", "sync_character", "give_character"} & names, "only the brief's tools"
    for feature in ("armory_link", "armory_move", "armory_seed"):
        row = configops.BY_DESIGN_ONLY[feature]
        assert row["surface"] == "web" and "plain text" in row["reason"]
    assert set(ARMORY_DISCORD_PENDING) <= set(ARMORY)


def test_a_roster_read_publishes_the_member_topic(g, fake):
    assert any(getattr(f, "__name__", "") == "roster_read" for f in discord_armory.ROSTER_LISTENERS)
    g.sent[:] = []
    run(discord_armory.roster_refresh(g.bot, g.reg, force=True))
    assert ("member", g.reg.key) in g.sent


# ---------------------------------------------------------------- fix round 1

def test_give_says_when_it_registered_but_could_not_link(g, monkeypatch):
    """The add landed, the link was refused afterwards (a race, the armory, a bug): 200 with registered true, linked
    false and why — never a refusal that reads as "nothing happened"."""
    reg, h = g.reg, as_user(g, OFFICER)

    async def refused(*a, **k):
        if k.get("preview"):
            return ["x"]
        raise RuntimeError("boom")
    monkeypatch.setattr(discord_armory, "link_character", refused)
    r = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": 700103, "spec": "Balance"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["registered"] is True and d["linked"] is False and d["character"]["label"] == "Tamsin Pell"
    assert d["message"] == f"Tamsin Pell is registered to Orla but not linked. {armory_seed.LINK_FAILED}"
    assert reg.members[ORLA].active()[-1].armory is None


def test_the_seed_preview_counts_the_picks_that_will_go(g):
    reg, h = g.reg, as_user(g, OFFICER)
    assert g.c.post("/api/me/armory/link", json={"label": "Bram", "surname": "Kettle", "armory_id": 700101}, headers=as_user(g, TESSA)).status_code == 200
    rev = g.c.get("/api/armory/seed", headers=as_user(g, OFFICER)).json()["rev"]
    picks = [{"uid": str(WREN), "label": "Pip", "armory_id": 700101}, {"uid": str(WREN), "label": "Pip", "armory_id": 700105}]
    p = g.c.post("/api/armory/seed/apply", json={"rev": rev, "picks": picks, "preview": True}, headers=as_user(g, OFFICER)).json()
    assert p["sentences"] == cq.armory_seed_consequence(1, 1) and [r["ok"] for r in p["results"]] == [False, True]
    assert p["results"][0]["text"].endswith("Unlink it there first.")


def test_lookups_are_limited_per_person(g, fake):
    h = as_user(g, TESSA)
    for _ in range(api_armory.LOOKUPS_PER_MINUTE):
        assert g.c.post("/api/me/armory/find", json={"label": "Bram", "surname": "Kettle"}, headers=h).status_code == 200
    n = fake.count("character")
    r = g.c.post("/api/me/armory/find", json={"label": "Bram", "surname": "Kettle"}, headers=h)
    assert r.status_code == 429 and r.json()["error"] == api_armory.TOO_MANY_LOOKUPS and fake.count("character") == n
    assert g.c.post("/api/me/armory/find", json={"label": "Brom", "surname": "Kettle"}, headers=as_user(g, ORLA)).status_code != 429, "per person"


def test_an_armory_outage_is_a_503_and_a_refusal_stays_400(g, fake):
    fake.fail = armory.ArmoryDown("The armory isn't answering right now. Try again later.", "503")
    r = g.c.post("/api/me/armory/find", json={"label": "Bram", "surname": "Kettle"}, headers=as_user(g, TESSA))
    assert r.status_code == 503 and r.json()["error"] == "The armory isn't answering right now. Try again later."
    fake.fail = None
    r = g.c.post("/api/me/armory/find", json={"label": "Bram", "surname": "Nobody"}, headers=as_user(g, TESSA))
    assert r.status_code == 400


def test_shape_errors_are_plain_sentences(g):
    h = as_user(g, TESSA)
    r = g.c.post("/api/me/armory/switch", json={"label": "Bram", "on": "no"}, headers=h)
    assert r.json()["error"] == api_armory.PICK_ON
    oh = as_user(g, OFFICER)
    r = g.c.post("/api/members/armory/give", json={"uid": str(ORLA), "armory_id": "x"}, headers=oh)
    assert r.status_code == 400 and r.json()["error"] == api_armory.PICK_ROW
    r = g.c.post("/api/members/move", json={"uid": str(WREN), "label": "Pip"}, headers=oh)
    assert r.status_code == 400 and r.json()["error"] == api_armory.PICK_TO
    for text in (api_armory.PICK_ON, api_armory.PICK_ROW, api_armory.PICK_TO, api_armory.PICK_SHAPE):
        assert text[0].isupper() and text.endswith(".") and not set("{}:_") & set(text)


def test_the_roster_words_never_promise_a_time(g):
    assert "keeps trying" in api_armory.NOT_READ_YET and "minute" not in api_armory.NOT_READ_YET
