"""The server's guild (armory link, spec §5.1): the owner links the Discord server to its in-game guild —
`GuildConfig.armory` — on Discord (`/gm config armory`), the site (Config › Settings) and MCP. The owner types the
guild's name (prose); the bot looks it up and the confirm says what it found (name, faction, kind, members, created) so a
collision is seen before the press. Unlinking keeps every character's link. Tests never call Blizzard: the armory client
is the recorded, anonymised fixture (invented guild "Lantern Wardens") behind a fake transport."""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import httpx
import pytest
from conftest import OWNER
from fastapi.testclient import TestClient
from test_armory_client import Fake, client_for_profile
from test_loot_web import OFFICER, TOKEN, WebLootBot, as_user, env  # noqa: F401 — env is a fixture
from test_parity_matrix import discord_path_exists, slash_paths, web_routes  # noqa: F401 — fixtures
from wizard_harness import Harness

from oibot_gm import armory, configops
from oibot_gm import consequences as cq
from oibot_gm import flows_config  # noqa: F401 — registers the flows
from oibot_gm import mcp_server as ms
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import ArmoryGuild, ConfigChanged, RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS

ROOT = Path(__file__).resolve().parents[1]
GUILD_JSON = json.loads((ROOT / "tests" / "fixtures" / "armory" / "profile" / "guild.json").read_text(encoding="utf-8"))
MISSING = json.loads((ROOT / "tests" / "fixtures" / "armory" / "profile" / "missing.json").read_text(encoding="utf-8"))
G = "/data/wow/guild/lantern-wardens"
MCP = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def armory_on(monkeypatch):
    """The armory is available (credentials set) and answers from the recorded fixture; any other name is a 404."""
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "test-id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "test-secret")
    routes = {G: (200, GUILD_JSON), "/data/wow/guild/oi": (404, MISSING), "/data/wow/guild/stranger-hall": (200, {**GUILD_JSON, "id": 999001, "name": "Stranger Hall", "slug": "stranger-hall", "faction": {"type": "ALLIANCE", "name": "Alliance"}, "member_count": 1})}
    fake = Fake(routes)
    client = client_for_profile(fake)
    monkeypatch.setattr(armory, "client_for", lambda reg: client)
    return fake


@pytest.fixture
def tbc(monkeypatch, reg):
    """The same guild on a game version with no armory (tbc): everything about the guild link is off."""
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "test-id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "test-secret")
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    return reg


def facts() -> armory.GuildFacts:
    fake = Fake({G: (200, GUILD_JSON)})
    return client_for_profile(fake).guild("lantern-wardens")


# ---------------------------------------------------------------- Registry: owner only, the settings rev

def test_set_and_clear_are_owner_only_and_in_the_settings_rev(reg):
    rev = reg.config_rev("settings")
    with pytest.raises(RegistryError, match="owner"):
        reg.set_armory_guild(facts(), "Officer", by_id=OFFICER)
    assert reg.config.armory is None and reg.config_rev("settings") == rev
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER, rev=rev)
    a = reg.config.armory
    assert isinstance(a, ArmoryGuild)
    assert (a.region, a.guild_id, a.slug, a.name, a.faction, a.linked_by) == ("us", 418362, "lantern-wardens", "Lantern Wardens", "Horde", "Owner")
    assert a.linked_at and reg.config_rev("settings") != rev, "the link is part of the settings rev"
    with pytest.raises(ConfigChanged):
        reg.clear_armory_guild("Owner", by_id=OWNER, rev=rev)
    reread = type(reg)(reg.store, reg.key, reg.base_profile)
    assert reread.config.armory == a, "it is stored in guild.yaml"
    with pytest.raises(RegistryError, match="owner"):
        reg.clear_armory_guild("Officer", by_id=OFFICER)
    reg.clear_armory_guild("Owner", by_id=OWNER)
    assert reg.config.armory is None
    with pytest.raises(RegistryError, match="No guild is linked"):
        reg.clear_armory_guild("Owner", by_id=OWNER)


def test_an_old_guild_yaml_loads_with_no_link(reg):
    assert reg.config.armory is None
    assert "no guild linked yet" in reg.armory_guild_words()


def test_the_consequences_name_what_was_found_and_what_changes(reg):
    lines = cq.armory_guild_consequence(reg, facts())
    assert lines[0].startswith("Lantern Wardens is a Horde Normal guild with 8 members, created ")
    assert "2026" in lines[0] and "T" not in lines[0].split("created ")[1], "a date in words, never ISO"
    assert "who is in the guild" in " ".join(lines) and len(lines) <= 3
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    other = armory.GuildFacts(id=999001, slug="stranger-hall", name="Stranger Hall", faction="Alliance", kind="PvP", members=1, created=facts().created)
    lines = cq.armory_guild_consequence(reg, other)
    assert lines[0].startswith("Stranger Hall is an Alliance PvP guild with 1 member,") and "replaces Lantern Wardens" in lines[1]
    assert "already linked" in cq.armory_guild_consequence(reg, facts())[1]


# ---------------------------------------------------------------- Discord: /gm config armory

class LinkBot:
    def __init__(self):
        from test_loot_verbs import RecOps

        self.ops = RecOps()


def harness(reg, rs, uid=OWNER, name="Owner"):
    return Harness(reg, rs, user=types.SimpleNamespace(id=uid, display_name=name, mention=f"@{name}"), client=LinkBot())


def test_owner_links_the_guild_and_the_confirm_names_what_was_found(reg, rs, armory_on):
    h = harness(reg, rs)

    async def go():
        await FLOWS["armory_guild"](h.command(), reg)
        assert h.modal is not None, "unlinked: the command opens the name box"
        await h.submit(name="Lantern Wardens")
        assert reg.config.armory is None, "nothing is written before the press"
        for word in ("Lantern Wardens", "Horde", "Normal", "8 members", "created", "2026"):
            assert word in h.text, word
        assert h.buttons() == ["Link this guild", "Back"]
        await h.press("Link this guild")
        assert "✅" in h.text and "Lantern Wardens" in h.text
    run(go())
    a = reg.config.armory
    assert a.guild_id == 418362 and a.linked_by == "Owner" and a.faction == "Horde"
    lines = [t for _k, _lvl, t in h.client.ops.lines]
    assert len(lines) == 1 and "Lantern Wardens" in lines[0] and "418362" in lines[0]
    assert all(path == G for path, _q, _h in armory_on.calls), "the typed name became the guild's address"

    # linked: the command shows what is linked and offers Change… / Unlink…
    h2 = harness(reg, rs)

    async def again():
        await FLOWS["armory_guild"](h2.command(), reg)
        assert "Lantern Wardens" in h2.text and "Horde" in h2.text and h2.buttons() == ["Change…", "Unlink…", "Done"]
        await h2.press("Change…")
        await h2.submit(name="Stranger Hall")
        assert "replaces Lantern Wardens" in h2.text
        await h2.press("Back")
    run(again())
    assert reg.config.armory.guild_id == 418362, "Back changes nothing"


def test_an_officer_is_refused(reg, rs, armory_on, env):
    h = harness(reg, rs, uid=OFFICER, name="Officer")
    run(FLOWS["armory_guild"](h.command(), reg))
    assert "Owner only" in h.text and h.modal is None
    # the owner's open wizard re-checks rights on every press: a lost owner can't press Link
    ho = harness(reg, rs)

    async def go():
        await FLOWS["armory_guild"](ho.command(), reg)
        await ho.submit(name="Lantern Wardens")
        reg.config.owner_discord_id = OFFICER + 1
        view = ho.screen["view"]
        itx = ho._on_screen()
        assert not await view.interaction_check(itx)
    run(go())
    reg.config.owner_discord_id = OWNER
    assert reg.config.armory is None
    # the site and MCP: an officer reads, never links
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))
    off = as_user(c, OFFICER)
    assert c.get("/api/admin/armory", headers=off).status_code == 200
    r = c.post("/api/admin/armory", json={"name": "Lantern Wardens", "preview": True}, headers=off)
    assert r.status_code == 403
    assert c.post("/api/admin/armory", json={"name": "Lantern Wardens"}, headers=off).status_code == 403
    assert reg.config.armory is None


def test_not_found_is_one_sentence(reg, rs, armory_on, env):
    h = harness(reg, rs)

    async def go():
        await FLOWS["armory_guild"](h.command(), reg)
        await h.submit(name="Oi")
        assert armory.GUILD_MISSING_WORDS in h.text and "Try another name" in h.buttons()
        await h.press("Try another name")
        assert h.modal is not None
    run(go())
    assert reg.config.armory is None
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))
    r = c.post("/api/admin/armory", json={"name": "Oi", "preview": True}, headers=as_user(c, OWNER))
    assert r.status_code == 400 and r.json()["error"] == armory.GUILD_MISSING_WORDS
    r = c.post("/api/admin/armory", json={"name": "!!!", "preview": True}, headers=as_user(c, OWNER))
    assert r.status_code == 400 and r.json()["error"] == armory.GUILD_MISSING_WORDS, "a name with no letters is not a guild"


def test_clear_keeps_character_links(reg, rs, armory_on):
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    m = next(iter(reg.members.values()))
    before = [c.model_dump() for c in m.characters]
    words = " ".join(cq.armory_guild_clear_consequence(reg))
    assert "Every character keeps its link" in words and "Lantern Wardens" in words
    h = harness(reg, rs)

    async def go():
        await FLOWS["armory_guild"](h.command(), reg)
        await h.press("Unlink…")
        assert "Every character keeps its link" in h.text and "Unlink" in h.buttons()
        await h.press("Unlink")
        assert "✅" in h.text
    run(go())
    assert reg.config.armory is None
    assert [c.model_dump() for c in reg.members[m.discord_id].characters] == before
    assert any("Lantern Wardens" in t and "unlinked" in t for _k, _l, t in h.client.ops.lines)


def test_tbc_has_no_armory_command_or_card(tbc, rs, env):
    reg = tbc
    assert not armory.available(reg)
    h = harness(reg, rs)
    run(FLOWS["armory_guild"](h.command(), reg))
    assert h.text.endswith(armory.NO_ARMORY_WORDS) and h.modal is None
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))
    hd = as_user(c, OWNER)
    assert c.get("/api/admin/armory", headers=hd).status_code == 404
    assert c.post("/api/admin/armory", json={"name": "Lantern Wardens", "preview": True}, headers=hd).status_code == 404
    assert c.get("/api/meta", headers=hd).json()["armory"] is False
    assert not any("armory" in line.lower() for s in configops.config_summary(reg) for line in s.lines), "no armory line where there is none"


# ---------------------------------------------------------------- the site: preview, link, unlink

def test_the_site_previews_links_and_unlinks(reg, rs, armory_on, env):
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))
    h = as_user(c, OWNER)
    assert c.get("/api/meta", headers=h).json()["armory"] is True
    d = c.get("/api/admin/armory", headers=h).json()
    assert d["linked"] is None and d["owner"] is True and d["rev"] == reg.config_rev("settings")
    p = c.post("/api/admin/armory", json={"name": "Lantern Wardens", "preview": True}, headers=h).json()
    assert p["preview"] and p["found"]["name"] == "Lantern Wardens" and p["found"]["members"] == 8 and p["found"]["kind"] == "Normal"
    assert "2026" in p["found"]["created_label"] and p["found"]["guild_id"] == 418362
    assert p["sentences"] == cq.armory_guild_consequence(reg, facts()) and reg.config.armory is None
    # a guild id that isn't the one previewed is refused (the name now answers with another guild)
    r = c.post("/api/admin/armory", json={"name": "Lantern Wardens", "guild_id": 5, "rev": d["rev"]}, headers=h)
    assert r.status_code == 409 and reg.config.armory is None
    r = c.post("/api/admin/armory", json={"name": "Lantern Wardens", "guild_id": 418362, "rev": "stale"}, headers=h)
    assert r.status_code == 409 and reg.config.armory is None
    r = c.post("/api/admin/armory", json={"name": "Lantern Wardens", "guild_id": 418362, "rev": d["rev"]}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["linked"]["name"] == "Lantern Wardens" and reg.config.armory.guild_id == 418362
    assert out["clear_sentences"] == cq.armory_guild_clear_consequence(reg)
    assert sum("Lantern Wardens" in t and "[web]" in t for _k, _l, t in bot.ops.lines) == 1
    s = c.post("/api/admin/armory", json={"clear": True, "preview": True}, headers=h).json()
    assert "Every character keeps its link" in " ".join(s["sentences"]) and reg.config.armory is not None
    r = c.post("/api/admin/armory", json={"clear": True, "rev": out["rev"]}, headers=h)
    assert r.status_code == 200 and r.json()["linked"] is None and reg.config.armory is None
    summary = " ".join(line for s in configops.config_summary(reg) for line in s.lines)
    assert "no guild linked yet" in summary


def test_mcp_previews_with_the_token_and_links_only_with_the_owner_as_actor(reg, rs, armory_on, env, monkeypatch):
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))

    def handler(request: httpx.Request) -> httpx.Response:
        r = c.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "application/json")})
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        assert "no guild linked yet" in ms.armory_guild()["words"]
        p = ms.set_armory_guild("Lantern Wardens", preview=True)
        assert "Lantern Wardens is a Horde Normal guild" in p and "418362" in p and reg.config.armory is None
        monkeypatch.delenv(ms.ACTOR_ENV, raising=False)
        with pytest.raises(ms.ToolError, match="OIBOT_MCP_ACTOR"):
            ms.set_armory_guild("Lantern Wardens", guild_id=418362)
        monkeypatch.setenv(ms.ACTOR_ENV, str(OFFICER))
        with pytest.raises(ms.ToolError, match="Owner only"):
            ms.set_armory_guild("Lantern Wardens", guild_id=418362)
        assert reg.config.armory is None
        monkeypatch.setenv(ms.ACTOR_ENV, str(OWNER))
        assert "Lantern Wardens" in ms.set_armory_guild("Lantern Wardens", guild_id=418362)
        assert reg.config.armory.guild_id == 418362
        assert ms.armory_guild()["linked"]["name"] == "Lantern Wardens"
        assert "Every character keeps its link" in ms.set_armory_guild(clear=True, preview=True)
        ms.set_armory_guild(clear=True)
        assert reg.config.armory is None
    finally:
        ms.use(None)
    assert any("[mcp]" in t and "Lantern Wardens" in t for _k, _l, t in bot.ops.lines)


# ---------------------------------------------------------------- parity and Discord's limits

def test_the_guild_link_is_on_three_surfaces_and_never_plain_text(slash_paths, web_routes):  # noqa: F811
    assert discord_path_exists("flow:armory_guild", slash_paths) and "gm config armory" in slash_paths
    assert {"GET /api/admin/armory", "POST /api/admin/armory"} <= web_routes
    names = {t.name for t in run(ms.mcp.list_tools())}
    assert {"armory_guild", "set_armory_guild"} <= names
    row = configops.BY_DESIGN_ONLY["armory_guild"]
    assert row["path"] == "POST /api/admin/armory" and "plain text" in row["reason"]
    assert "armory" not in " ".join(configops.OP_GROUP)


def test_the_armory_command_fits_discords_limits(reg):
    import discord
    from discord import app_commands

    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_commands(tree, Guilds.of(reg.store, reg), Ops(client), lambda k, v: "")
    gm = next(c for c in tree.get_commands() if c.name == "gm")
    config = next(c for c in gm.commands if c.name == "config")
    cmd = next(c for c in config.commands if c.name == "armory")
    assert len(cmd.description) <= 100 and not cmd.parameters, "the guild's name is typed in the wizard's box, never an argument"
