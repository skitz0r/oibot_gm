"""Several Discord servers in one process, stage S1 (design §5.32): the loader, strict routing (an unconfigured server
never reaches another guild), the DM interim, claiming an unconfigured server, and every process-wide piece made per
guild — scheduler, companion feed, ops ring and owner DMs, run locks, split proposals, channels, command sync, /healthz.
Isolation tests stand on the two-guild fixture (`duo`): demo (server 1) and duo (server 2) hold runs with ONE key."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import discord
import pytest
import yaml
from conftest import DUO_SERVER, GUILD, GUILD2, OWNER, OWNER2, ROOT, MemberDirectory, add_duo, lock_with_board, same_run
from wizard_harness import Harness

from oibot_gm import configops, discord_registry as dr, guild_routing, ops as ops_mod, raidcycle as rc
from oibot_gm import flows_config  # noqa: F401 — registers guild_claim
from oibot_gm.discord_bot import OibotGM
from oibot_gm.discord_feed import FeedMixin, feed_status
from oibot_gm.discord_registry import Guilds
from oibot_gm.raid_scheduler import RaidSchedulerMixin
from oibot_gm.registry import GUILD_KEY_RE, GuildConfig, RegistryError, create_guild, guild_key_for
from oibot_gm.store import GitStore
from oibot_gm.wizard import FLOWS


def run(coro):
    return asyncio.run(coro)


class RecOps:
    def __init__(self):
        self.lines: list[tuple[str, str, str, bool]] = []  # (guild key, level, text, dm)

    def recent_for(self, key):
        return [(None, lvl, text) for k, lvl, text, _ in self.lines if k == key]

    async def emit(self, cfg, level, text, exc=None, dm=True):
        self.lines.append((cfg.key, level, text, dm))


class Said:
    """A fake interaction response that records every reply (and fails on a second response, as Discord does)."""

    def __init__(self):
        self.replies: list[str] = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kw):
        assert not self._done, "a second response to one interaction (InteractionResponded)"
        self._done = True
        self.replies.append(content if content is not None else kw.get("embed"))

    async def send_modal(self, modal):
        self._done = True

    async def defer(self, **kw):
        self._done = True


def itx(client, *, guild_id=None, user_id=42, manage=False, guild=None):
    resp = Said()
    user = types.SimpleNamespace(id=user_id, display_name=f"u{user_id}", mention=f"<@{user_id}>", roles=[],
                                 guild_permissions=types.SimpleNamespace(manage_guild=manage))

    async def followup(content=None, **kw):
        resp.replies.append(content if content is not None else kw.get("embed"))
    return types.SimpleNamespace(client=client, guild_id=guild_id, guild=guild, user=user, response=resp, extras={},
                                 followup=types.SimpleNamespace(send=followup), channel_id=None, namespace=types.SimpleNamespace())


def scratch_root(tmp_path: Path, *guild_rows: dict) -> GitStore:
    root = tmp_path / "scratch"
    root.mkdir()
    for row in guild_rows:
        (root / row["key"]).mkdir(exist_ok=True)
        (root / row["key"] / "guild.yaml").write_text(yaml.safe_dump(row, sort_keys=False))
    (root / "25bg").mkdir()  # the mock shadow data: a folder that is not a guild
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed", "--allow-empty"], cwd=root, check=True)
    return GitStore(root, push=False)


def row(key, gid, name=None, **kw):
    return {"key": key, "name": name or key.title(), "game_profile": "forever", "discord_guild_id": gid, **kw}


# ---------------------------------------------------------------- the loader

def test_loader_loads_both_and_orders_them_stably(duo):
    g = duo.guilds
    assert set(g.by_key) == {GUILD, GUILD2} and g.many and g.only() is None and not g.load_errors
    assert [r.key for r in g.ordered()] == [GUILD, GUILD2] == [r.key for r in g.ordered()]  # by name: "Demo Guild" < "Duo Guild"
    assert g.for_guild(1) is duo.a and g.for_guild(DUO_SERVER) is duo.b and g.for_guild(3) is None and g.for_guild(None) is None
    assert g.get("duo") is duo.b and g.get("DUO GUILD") is duo.b and g.get("nope") is None
    assert g.cfg_for("duo") is duo.b.config and g.cfg_for("25bg") is None, "no 'first guild' fallback with several"


def test_loader_skips_a_broken_guild_and_refuses_two_folders_on_one_server(tmp_path):
    store = scratch_root(tmp_path, row("good", 10), row("twina", 20), row("twinb", 20), row("Bad Key", 30), row("other", 40))
    (store.root / "renamed").mkdir()
    (store.root / "renamed" / "guild.yaml").write_text(yaml.safe_dump(row("notrenamed", 50)))
    (store.root / "broken").mkdir()
    (store.root / "broken" / "guild.yaml").write_text("key: broken\nname: [unclosed\n")
    (store.root / "noprof").mkdir()
    (store.root / "noprof" / "guild.yaml").write_text(yaml.safe_dump({**row("noprof", 60), "game_profile": "wotlk"}))
    (store.root / "other" / "guild.yaml").write_text(yaml.safe_dump({**row("other", 40), "key": "other"}))
    g = Guilds(store, ROOT)
    assert set(g.by_key) == {"good", "other"}, "every broken guild is skipped, the others still load"
    errs = "\n".join(g.load_errors)
    assert "twina and twinb" in errs and "neither loaded" in errs
    for bad in ("renamed", "broken", "noprof", "Bad Key"):
        assert bad in errs, bad


def test_check_commands_fails_on_a_guild_that_would_not_load(tmp_path):
    store = scratch_root(tmp_path, row("twina", 20), row("twinb", 20))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_commands.py")], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "OIBOT_DATA_DIR": str(store.root)}, timeout=120)
    assert r.returncode == 1 and "would not load" in r.stderr and "twina and twinb" in r.stderr


# ---------------------------------------------------------------- strict routing

def test_an_unconfigured_server_is_told_and_never_reaches_another_guild(duo):
    client = types.SimpleNamespace(registries=duo.guilds)
    i = itx(client, guild_id=999, manage=True)
    assert duo.guilds.for_interaction(i) is None
    reg, same = run(guild_routing.need(i))
    assert reg is None and same is i and i.response.replies == [guild_routing.NOT_SET_UP]


def test_single_guild_unconfigured_server_no_longer_falls_through(reg, store):
    g = Guilds(store, ROOT)  # one guild: the old fallback handed every unknown server to it
    client = types.SimpleNamespace(registries=g)
    i = itx(client, guild_id=999, manage=True)
    assert run(guild_routing.need(i))[0] is None and "isn't set up" in i.response.replies[0]
    dm = itx(client, guild_id=None, user_id=777)  # a DM from someone who isn't even a member: the only guild, as before
    got, _ = run(guild_routing.need(dm))
    assert got is not None and got.key == GUILD and dm.extras["guild"] == GUILD


def test_officiates_gives_manage_server_no_rights_in_an_unconfigured_server(duo):
    bot = types.SimpleNamespace(registries=duo.guilds)
    officiates = OibotGM.officiates.__get__(bot)

    class M(discord.Member):  # isinstance(user, discord.Member) without a gateway
        id = 555
        guild_permissions = discord.Permissions(manage_guild=True)
        roles: list = []

        def __init__(self):
            pass

        def __repr__(self):
            return "<admin>"
    admin = M()
    assert not officiates(admin, types.SimpleNamespace(id=999)), "an unconfigured server's admin is nobody's officer (K24)"
    assert officiates(admin, types.SimpleNamespace(id=999), mock=True), "/mock keeps its old rule"
    assert officiates(admin, types.SimpleNamespace(id=DUO_SERVER)), "Manage Server in a configured server still counts"
    assert officiates(types.SimpleNamespace(id=OWNER2), types.SimpleNamespace(id=DUO_SERVER))
    assert not officiates(types.SimpleNamespace(id=OWNER2), types.SimpleNamespace(id=1)), "owning duo grants nothing in demo"


def test_command_error_goes_to_the_resolved_guild_never_the_first(duo):
    sent = []

    class Bot:
        registries = duo.guilds
        ops = RecOps()
        _on_command_error = OibotGM._on_command_error
    bot = Bot()
    i = itx(bot, guild_id=None)
    i.command = types.SimpleNamespace(qualified_name="raid lock")
    i.extras["guild"] = GUILD2
    run(bot._on_command_error(i, discord.app_commands.AppCommandError("boom")))
    assert [k for k, *_ in bot.ops.lines] == [GUILD2]
    j = itx(bot, guild_id=999)
    j.command = None
    run(bot._on_command_error(j, discord.app_commands.AppCommandError("boom")))
    assert len(bot.ops.lines) == 1, "an unknown server: the log only"
    del sent


def test_command_error_in_a_dm_before_need_reaches_the_only_guild_as_before(reg, store, duo):
    """A transformer/check error in a DM fails before need() tags the guild: with one guild it still reaches that
    guild's ops line and owner (unchanged behaviour, D6/K17); with several, the log only."""
    class Bot:
        ops = RecOps()
        _on_command_error = OibotGM._on_command_error
    one = Bot()
    one.registries = Guilds.of(store, reg)
    i = itx(one, guild_id=None)
    i.command = types.SimpleNamespace(qualified_name="me view")
    run(one._on_command_error(i, discord.app_commands.CheckFailure("nope")))
    assert [(k, lvl) for k, lvl, *_ in one.ops.lines] == [(GUILD, "error")] and i.response.replies
    many = Bot()
    many.ops, many.registries = RecOps(), duo.guilds
    j = itx(many, guild_id=None)
    j.command = None
    run(many._on_command_error(j, discord.app_commands.CheckFailure("nope")))
    assert many.ops.lines == [], "several guilds and no tag: never a guess"
    k = itx(many, guild_id=DUO_SERVER)
    k.command = None
    run(many._on_command_error(k, discord.app_commands.CheckFailure("nope")))
    assert [key for key, *_ in many.ops.lines] == [GUILD2]


def test_channel_only_cards_resolve_their_server_strictly(duo, monkeypatch):
    """app:/abs:/newsprop: cards live in a server channel: the server's own guild, never a DM fallback (design §4.3)."""
    from oibot_gm import discord_news, discord_pool

    seen = []
    monkeypatch.setattr(dr, "is_officer", lambda i, r: seen.append(("officer", r.key)) or False)

    async def fake_absence(interaction, reg, **kw):
        seen.append(("absence", reg.key))
    monkeypatch.setitem(FLOWS, "absence", fake_absence)

    async def anywhere(reg, uid):
        seen.append(("anywhere", reg.key))
        return False
    client = types.SimpleNamespace(registries=duo.guilds, is_officer_anywhere=anywhere)
    cards = [(dr.ApplicationButton("accept", 5, None), "Officers only."), (discord_pool.AbsenceButton("new", None), "Not configured here."),
             (discord_news.ProposalButton(None, "260101-abcd", "approve"), "Officers only.")]  # old ids (no guild): the server decides
    for card, refusal in cards:
        for server in (None, 999):  # a DM, an unconfigured server
            i = itx(client, guild_id=server)
            run(card.callback(i))
            assert i.response.replies == [refusal], (card, server)
    assert seen == []
    for card, _ in cards:
        run(card.callback(itx(client, guild_id=DUO_SERVER)))
    assert seen == [("officer", GUILD2), ("absence", GUILD2), ("anywhere", GUILD2)]
    one = types.SimpleNamespace(registries=Guilds.of(duo.a.store, duo.a), is_officer_anywhere=anywhere)
    i = itx(one, guild_id=None)
    run(dr.ApplicationButton("decline", 5, None).callback(i))
    assert i.response.replies == ["Officers only."], "one guild too: a channel card pressed outside its server is refused"


def test_api_ops_feed_shows_only_this_guilds_companions(duo, monkeypatch):
    from fastapi.testclient import TestClient

    from oibot_gm.feed import FeedServer
    from oibot_gm.web import app as web
    from test_mcp import TOKEN, FakeBot

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = FakeBot(duo.a, duo.rs_a)
    bot.registries = duo.guilds
    fs = FeedServer(lambda ev, c: None, "t")
    for peer, g in (("p1", GUILD2), ("p2", GUILD), ("p3", "")):
        fs.companions[peer] = types.SimpleNamespace(character=f"c-{peer}", client="x", events=0, silent_for=1, guild=g)
    bot.feed = fs
    for me, mine, theirs in ((GUILD, "c-p2", "c-p1"), (GUILD2, "c-p1", "c-p2")):  # the bearer names its guild (S3)
        body = TestClient(web.create_app(bot)).get("/api/ops", headers={"Authorization": f"Bearer {TOKEN}", "X-Oibot-Guild": me}).json()
        assert mine in body["feed"] and theirs not in body["feed"] and "c-p3" not in body["feed"], body["feed"]
    one = FakeBot(duo.a, duo.rs_a)
    one.feed = fs
    body = TestClient(web.create_app(one)).get("/api/ops", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert "c-p2" in body["feed"] and "c-p3" in body["feed"] and "c-p1" not in body["feed"], "one guild: an untagged companion counts for it"
    assert TestClient(web.create_app(FakeBot(duo.a, duo.rs_a))).get("/api/ops", headers={"Authorization": f"Bearer {TOKEN}"}).json()["feed"] == "disabled"


# ---------------------------------------------------------------- DMs (the picker itself: test_multiguild_dm.py)

def dm_client(duo, members=None, flaky=None):
    client = types.SimpleNamespace(registries=duo.guilds)
    MemberDirectory(members, flaky).install(client)
    return client


def test_dm_from_someone_in_one_guild_routes_there(duo):
    client = dm_client(duo, {(DUO_SERVER, 42): object()})
    i = itx(client)
    reg, _ = run(guild_routing.need(i))
    assert reg is duo.b and i.extras["guild"] == GUILD2 and not i.response.replies


def test_dm_with_a_discord_hiccup_asks_instead_of_picking_the_other_guild(duo, monkeypatch):
    asked = []

    async def picker(target, regs, *a, **kw):
        asked.append([r.key for r in regs])
        return None, None
    monkeypatch.setattr("oibot_gm.guild_picker.choose_guild", picker)
    client = dm_client(duo, {(DUO_SERVER, 42): object()}, flaky={(1, 42)})  # in duo; demo's lookup failed transiently
    i = itx(client)
    assert run(guild_routing.need(i))[0] is None and asked == [[GUILD, GUILD2]], "unknown counts (K4): the bot asks"
    assert run(guild_routing.may_access(client, duo.a, 42)) is False and run(guild_routing.membership(client, duo.a, 42)) == "unknown"


def test_a_transient_fetch_failure_is_never_cached_as_not_a_member(duo, monkeypatch):
    """The real cached_member behind member_of: a 503 from Discord reads 'unknown' only until the short retry (never for
    the cache's 5 minutes, which would be a 503 page for that long on the web); a success and a 404 are cached as before."""
    import functools

    from oibot_gm import discord_bot as db

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(db.time, "time", lambda: clock["t"])
    calls, answer = [], {"r": discord.HTTPException(types.SimpleNamespace(status=503, reason="Service Unavailable"), "later")}

    class Server:
        id = 1

        def get_member(self, _uid):
            return None

        async def fetch_member(self, uid):
            calls.append(uid)
            if isinstance(answer["r"], Exception):
                raise answer["r"]
            return answer["r"]

    server = Server()
    stub = types.SimpleNamespace(get_guild=lambda gid: server if gid == 1 else None)
    stub.cached_member = functools.partial(OibotGM.cached_member, stub)
    look = lambda: run(guild_routing.member_of(stub, duo.a, 42, timeout=None))[0]  # noqa: E731
    assert look() == "unknown" and calls == [42]
    assert look() == "unknown" and calls == [42], "throttled for a few seconds: no stream of fetches"
    assert (1, 42) not in stub.__dict__.get("_member_cache", {}), "a blip is not an answer"
    clock["t"] += db.MEMBER_BLIP_RETRY_S
    member = types.SimpleNamespace(id=42)
    answer["r"] = member
    assert look() == "yes" and len(calls) == 2, "asked again after the retry, long before the 5-minute cache would"
    clock["t"] += 60
    assert look() == "yes" and len(calls) == 2, "a real answer is cached as before"
    clock["t"] += 300
    answer["r"] = discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), {"code": 10007, "message": "Unknown Member"})
    assert look() == "no" and len(calls) == 3
    clock["t"] += 60
    assert look() == "no" and len(calls) == 3, "Discord's own 404 is remembered for the cache's lifetime"


def test_dm_from_someone_in_neither_lists_every_join_link(duo):
    duo.b.config.invite_url = "https://discord.gg/duo"
    client = dm_client(duo, {})
    i = itx(client)
    assert run(guild_routing.need(i))[0] is None
    text = i.response.replies[0]
    assert "Demo Guild" in text and "Duo Guild" in text and "https://discord.gg/duo" in text


def test_the_owner_belongs_to_their_guild_without_being_in_its_server(duo):
    client = dm_client(duo, {})
    assert run(guild_routing.may_access(client, duo.b, OWNER2)) and not run(guild_routing.may_access(client, duo.a, OWNER2))
    assert [r.key for r in run(guild_routing.guilds_of(client, OWNER2))] == [GUILD2]


def test_plain_dm_from_someone_in_one_guild_needs_no_question(duo):
    replies = []
    msg = types.SimpleNamespace(author=types.SimpleNamespace(id=42), reply=lambda text, **kw: _rec(replies, text))
    bot1 = dm_client(duo, {(1, 42): object()})
    assert run(OibotGM.dm_guild(bot1, msg)) is duo.a and not replies


async def _rec(out, text):
    out.append(text)


def test_help_in_an_unconfigured_server_answers_once_with_the_setup_line(duo):
    from discord import app_commands

    from oibot_gm.discord_help import register_help_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_help_commands(tree, duo.guilds, None, None)
    i = itx(types.SimpleNamespace(registries=duo.guilds), guild_id=999)
    run(tree.get_command("help").callback(i))
    assert len(i.response.replies) == 1, "K9: need(reply=False) leaves the one response to /help"
    assert any(f.name == "Not set up here" for f in i.response.replies[0].fields)


def test_every_need_and_officer_call_site_rebinds_the_interaction():
    """K10: the DM picker answers on a NEW interaction (the press), so every caller unpacks `reg, interaction`."""
    bad = []
    for f in (ROOT / "src" / "oibot_gm").glob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"await (need|officer)\(interaction", line) and "reg, interaction = await" not in line and "return await" not in line:
                bad.append(f"{f.name}:{n}: {line.strip()}")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------- claiming an unconfigured server

class ClaimBot:
    claim_guild = OibotGM.claim_guild

    def __init__(self, guilds, mock_key="mockdata"):
        self.registries = guilds
        self.ctx = types.SimpleNamespace(guild_key=mock_key)
        self.ops = RecOps()
        self.added: list[str] = []
        self.dms: list[tuple[int, str]] = []

    async def guild_added(self, reg):
        self.added.append(reg.key)

    def get_user(self, uid):
        bot = self

        class U:
            async def send(self, text):
                bot.dms.append((uid, text))
        return U()


def claim_harness(bot, *, server_id=5, name="Shiny New Server", owner_id=None, user_id=4242, manage=True):
    user = types.SimpleNamespace(id=user_id, display_name="Claimer", mention="@Claimer", guild_permissions=types.SimpleNamespace(manage_guild=manage))
    h = Harness(None, client=bot, user=user)
    h.guild_id = server_id
    h.guild = types.SimpleNamespace(id=server_id, name=name, owner_id=owner_id if owner_id is not None else 1)
    return h


def test_claim_wizard_creates_the_guild_in_one_commit_and_serves_it(tmp_path):
    store = scratch_root(tmp_path)  # a fresh deployment: no guild yet
    g = Guilds(store, ROOT)
    bot = ClaimBot(g)
    h = claim_harness(bot)
    commits = int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout)
    run(FLOWS["guild_claim"](h.command(), None, owner=None))
    assert "Shiny New Server" in h.text and "`shiny-new-se`" in h.text and "WoW: Forever" in h.text and "Confirm" in h.buttons()
    assert not (store.root / "shiny-new-se").exists(), "nothing written before Confirm"
    run(h.pick("Game version", "tbc"))
    assert "WoW: TBC Anniversary" in h.text
    run(h.pick("Game version", "forever"))
    run(h.press("Confirm"))
    assert "is set up" in h.text and "/gm config ops-channel" in h.text
    cfg = yaml.safe_load((store.root / "shiny-new-se" / "guild.yaml").read_text())
    assert cfg["key"] == "shiny-new-se" and cfg["discord_guild_id"] == 5 and cfg["owner_discord_id"] == 4242 and cfg["game_profile"] == "forever"
    assert int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout) == commits + 1
    assert "guild created for Shiny New Server" in subprocess.run(["git", "log", "-1", "--format=%s"], cwd=store.root, capture_output=True, text=True).stdout
    assert g.for_guild(5).key == "shiny-new-se" and "shiny-new-se" in g.by_key and bot.added == ["shiny-new-se"]
    assert any("guild created for Shiny New Server" in t for _, _, t, _ in bot.ops.lines)
    again = claim_harness(bot)
    run(FLOWS["guild_claim"](again.command(), None))
    assert again.text == "This server is already set up."


def test_claim_owner_can_be_someone_else(tmp_path):
    g = Guilds(scratch_root(tmp_path), ROOT)
    h = claim_harness(ClaimBot(g))
    run(FLOWS["guild_claim"](h.command(), None))
    run(h.pick_member("Someone else will own it…", types.SimpleNamespace(id=777, display_name="Boss", bot=False)))
    assert "<@777>" in h.text
    run(h.press("Confirm"))
    assert g.for_guild(5).config.owner_discord_id == 777


def test_claim_refusals(tmp_path):
    g = Guilds(scratch_root(tmp_path), ROOT)
    bot = ClaimBot(g)
    nobody = claim_harness(bot, manage=False)
    run(FLOWS["guild_claim"](nobody.command(), None))
    assert nobody.text == flows_config.CLAIMERS
    owner = claim_harness(bot, manage=False, owner_id=4242)  # the Discord server owner needs no Manage Server
    run(FLOWS["guild_claim"](owner.command(), None))
    assert "Confirm" in owner.buttons()
    dm = claim_harness(bot)
    dm.guild_id, dm.guild = None, None
    run(FLOWS["guild_claim"](dm.command(), None))
    assert dm.text.startswith("Run this in the server you want to set up")


def test_cfg_owner_in_an_unconfigured_server_opens_the_claim_and_refuses_in_a_dm(tmp_path, monkeypatch):
    from discord import app_commands

    g = Guilds(scratch_root(tmp_path), ROOT)
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    dr.register_commands(tree, g, ops_mod.Ops(client), lambda k, v: "")
    owner_cmd = tree.get_command("gm").get_command("config").get_command("owner")
    opened = []

    async def fake_claim(interaction, reg, **kw):
        opened.append((interaction.guild_id, reg, kw))
    monkeypatch.setitem(FLOWS, "guild_claim", fake_claim)
    run(owner_cmd.callback(itx(types.SimpleNamespace(registries=g), guild_id=5), user=None))
    assert opened == [(5, None, {"owner": None})]
    dm = itx(types.SimpleNamespace(registries=g), guild_id=None)
    run(owner_cmd.callback(dm, user=None))
    assert dm.response.replies == ["Run this in the server you want to set up."]


def test_a_second_server_may_be_claimed_since_s3_and_gets_a_unique_key(duo, monkeypatch):
    assert Guilds.ALLOW_SECOND, "K11: S3 (the DM picker, guild-carrying button ids) shipped, so a second claim is allowed"
    srv = types.SimpleNamespace(id=7, name="Demo")
    monkeypatch.setattr(Guilds, "ALLOW_SECOND", False)  # the switch still works (as S1 shipped it)
    with pytest.raises(RegistryError, match="second server"):
        duo.guilds.create(srv, 1, "forever")
    monkeypatch.setattr(Guilds, "ALLOW_SECOND", True)
    reg = duo.guilds.create(srv, 1, "forever", ("mockdata",))
    assert reg.key == "demo-2", "demo is taken"
    with pytest.raises(RegistryError, match="already set up"):  # a double press before add() lands
        duo.guilds.create(srv, 1, "forever")
    duo.guilds.add(reg)
    with pytest.raises(RegistryError, match="already set up"):
        duo.guilds.create(srv, 1, "forever")


def test_a_claim_in_flight_on_an_empty_deployment_blocks_a_second_server(tmp_path, monkeypatch):
    """K11 with nothing configured yet (as S1 shipped it): two servers claiming in the same window must not both get folders."""
    monkeypatch.setattr(Guilds, "ALLOW_SECOND", False)
    g = Guilds(scratch_root(tmp_path), ROOT)
    assert g.claim_refusal(5) is None
    g._claiming.add(5)  # server 5's claim is writing its folder (create() ran, add() hasn't)
    assert g.claim_refusal(5) == "This server is already set up."
    assert "second server" in g.claim_refusal(6)
    with pytest.raises(RegistryError, match="second server"):
        g.create(types.SimpleNamespace(id=6, name="Other"), 1, "forever")
    assert not g._claiming - {5} and [p.name for p in g.store.root.iterdir() if p.name != ".git"] == ["25bg"]
    monkeypatch.setattr(Guilds, "ALLOW_SECOND", True)
    assert g.claim_refusal(6) is None


def test_claim_tells_every_other_owner_once(duo, monkeypatch):
    monkeypatch.setattr(Guilds, "ALLOW_SECOND", True)
    duo.b.config.owner_discord_id = OWNER  # one person owns both: told once
    bot = ClaimBot(duo.guilds)
    reg = run(bot.claim_guild(types.SimpleNamespace(id=9, name="Third *Server*"), 4242, "forever", types.SimpleNamespace(display_name="Claimer")))
    assert reg.key == "third-server" and bot.added == [reg.key]
    assert [uid for uid, _ in bot.dms] == [OWNER] and "Third \\*Server\\*" in bot.dms[0][1], "names are data (escaped)"
    with pytest.raises(RegistryError, match="Invite the bot"):
        run(bot.claim_guild(None, 1, "forever", "x"))


def test_guild_key_rules():
    assert guild_key_for("Demo", {"demo"}) == "demo-2"
    assert guild_key_for("Demo", {"demo", "demo-2"}) == "demo-3"
    assert guild_key_for("25bg", {"25bg"}) == "25bg-2" and guild_key_for("out", set()) == "out-2"
    assert guild_key_for("Ünïcodé Ràid Ålliance of the North", set()) == "unicode-raid"
    assert guild_key_for("🐉🔥", set()) == "guild" and guild_key_for("", {"guild"}) == "guild-2"
    long = guild_key_for("Averyveryverylongservername", {"averyveryver"} | {f"averyveryver-{i}" for i in range(2, 200)})
    assert GUILD_KEY_RE.fullmatch(long) and len(long) <= 16
    for name in ("Demo", "a" * 100, "-- weird -- name --", "x"):
        k = guild_key_for(name, set())
        assert GUILD_KEY_RE.fullmatch(k) and len(k) <= 16


def test_create_guild_refuses_a_fixture_root_and_a_taken_folder(tmp_path, profile):
    fixtures = GitStore(ROOT / "fixtures", push=False)
    with pytest.raises(RegistryError, match="fixtures"):
        create_guild(fixtures, GuildConfig(key="newone", name="N", game_profile="forever", discord_guild_id=3), profile, ROOT)
    store = scratch_root(tmp_path)
    with pytest.raises(RegistryError, match="already exists"):
        create_guild(store, GuildConfig(key="25bg", name="N", game_profile="forever", discord_guild_id=3), profile, ROOT)
    with pytest.raises(RegistryError, match="bad guild key"):
        create_guild(store, GuildConfig(key="Bad Key", name="N", game_profile="forever", discord_guild_id=3), profile, ROOT)


def test_add_while_a_loop_iterates_is_safe(duo, monkeypatch):
    """K6: add() swaps a new dict in, so an ordered() snapshot being walked across awaits never fails."""
    monkeypatch.setattr(Guilds, "ALLOW_SECOND", True)
    seen = []

    async def walk():
        for r in duo.guilds.ordered():
            seen.append(r.key)
            await asyncio.sleep(0)
            if len(seen) == 1:
                duo.guilds.add(duo.guilds.create(types.SimpleNamespace(id=11, name="Late"), 1, "forever"))
        for _ in duo.guilds.by_discord.values():  # and the live dict is a fresh one, iterable as usual
            await asyncio.sleep(0)
    run(walk())
    assert seen == [GUILD, GUILD2] and "late" in duo.guilds.by_key


# ---------------------------------------------------------------- the scheduler, per guild

class TickBot(RaidSchedulerMixin):
    def __init__(self, guilds):
        self.registries = guilds
        self.ops = RecOps()
        self.done: list[str] = []
        self.fail: dict[str, Exception] = {}

    async def scheduler_tick_guild(self, reg):
        if reg.key in self.fail:
            raise self.fail[reg.key]
        self.done.append(reg.key)


def test_one_guilds_failure_never_skips_the_next_and_is_reported_to_it_once(duo):
    bot = TickBot(duo.guilds)
    bot.fail[GUILD] = RuntimeError("boom")
    run(bot.scheduler_tick())
    run(bot.scheduler_tick())
    assert bot.done == [GUILD2, GUILD2], "duo ran both minutes"
    errors = [(k, lvl) for k, lvl, *_ in bot.ops.lines]
    assert errors == [(GUILD, "error")], "to demo only, once for the same failure"
    bot.fail.clear()
    run(bot.scheduler_tick())
    bot.fail[GUILD] = RuntimeError("boom")
    run(bot.scheduler_tick())
    assert len(bot.ops.lines) == 2, "after a clean minute the failure is news again"


def test_a_process_wide_failure_reaches_every_guild_and_dms_a_shared_owner_once(duo):
    duo.b.config.owner_discord_id = OWNER
    dms = []

    class Client:
        registries = duo.guilds

        def get_channel(self, cid):
            return None

        def get_user(self, uid):
            class U:
                async def send(self, text):
                    dms.append((uid, text))
            return U()
    bot = TickBot(duo.guilds)
    bot.ops = ops_mod.Ops(Client())

    async def broken():
        raise RuntimeError("feed gone")
    bot.companion_check = broken
    run(bot.scheduler_tick())
    assert bot.done == [GUILD, GUILD2]
    assert [k for k, *_ in bot.ops.recent_for(GUILD)] and bot.ops.recent_for(GUILD2), "an ops line in each guild"
    assert len(dms) == 1 and dms[0][0] == OWNER
    assert dms[0][1].startswith("**Demo Guild** · "), "with several guilds the owner's DM says which"


def test_nobody_left_is_said_once_per_guild_even_with_one_run_key(duo):
    from test_parity_verbs import Bot

    a, b = duo.a, duo.b
    ev_a, ev_b = same_run(a, duo.rs_a, b, duo.rs_b)
    lock_with_board(a, duo.rs_a, ev_a, [m.display_name for m in a.test_members()[:5]])
    lock_with_board(b, duo.rs_b, ev_b, [m.display_name for m in b.test_members()[:5]])
    stores = {GUILD: duo.rs_a, GUILD2: duo.rs_b}

    class SchedBot(RaidSchedulerMixin, Bot):
        events: dict = {}

        def officer_channel(self, reg, ev=None):
            return None
    bot = SchedBot(a, duo.rs_a)
    bot.registries = duo.guilds
    bot.raids = types.SimpleNamespace(store=lambda r: stores[r.key])
    posted = []

    async def post_run_update(reg, ev, text):
        posted.append((reg.key, text))

    async def run_fill(reg, rs, ev, team, **kw):
        ev.fill_state = "exhausted"
        return [], {"headcount": 1, "roles": {}}
    bot.post_run_update, bot.run_fill = post_run_update, run_fill
    run(bot.scheduler_tick())
    nobody = [k for k, t in posted if "nobody left to ask" in t]
    assert nobody == [GUILD, GUILD2], "demo's line must not suppress duo's (same run key)"
    assert not [x for x in bot.ops.lines if x[1] == "error"]


# ---------------------------------------------------------------- the companion feed

class FeedBot(FeedMixin):
    def __init__(self, guilds, events):
        self.registries = guilds
        self.events = {i: e for i, e in enumerate(events)}
        self.ctx = types.SimpleNamespace(guild_key="mockdata")
        self.ops = RecOps()
        self.feed = None


def session(guild, origin="raid", sid="BAR-1010"):
    return types.SimpleNamespace(state="raid", origin=origin, guild=guild, id=f"{guild}:{sid}", raid_thread_id=None, roster=None)


def comp(guild=""):
    return types.SimpleNamespace(guild=guild, character="Scribe", client="oibot-companion/0.1")


def test_a_companion_routes_to_its_own_guilds_session(duo):
    bot = FeedBot(duo.guilds, [session(GUILD), session(GUILD2)])
    assert bot.active_raid(comp("duo")).guild == GUILD2 and bot.active_raid(comp("Duo Guild")).guild == GUILD2
    reply = run(bot.handle_feed_event({"type": "hello"}, comp("duo")))
    assert reply["routes_to"] == f"{GUILD2}:BAR-1010" and bot.ops.lines[-1][0] == GUILD2, "the line goes to the session's guild"


def test_a_companion_naming_no_guild_never_guesses_between_two_raiding_guilds(duo):
    bot = FeedBot(duo.guilds, [session(GUILD), session(GUILD2)])
    assert bot.active_raid(comp("")) is None and bot.active_raid(comp("stale name")) is None
    reply = run(bot.handle_feed_event({"type": "hello"}, comp("")))
    assert reply["routes_to"] is None and "--guild" in reply["note"] and not bot.ops.lines, "no guild to tell: the log only"
    one = FeedBot(duo.guilds, [session(GUILD2)])
    assert one.active_raid(comp("")).guild == GUILD2, "only one guild raiding: today's rule"
    mock = FeedBot(duo.guilds, [session(GUILD), session("mockdata", origin="mock")])
    assert mock.active_raid(comp("duo")).guild == "mockdata", "mock sessions stay the fallback (K12)"


def test_single_guild_feed_routes_as_before_whatever_the_companion_says(reg, store):
    g = Guilds(store, ROOT)
    bot = FeedBot(g, [session("mockdata", origin="mock")])
    assert bot.active_raid(comp("Oi Guild Name")).guild == "mockdata" and bot.active_raid(comp(GUILD)).guild == "mockdata"
    bot2 = FeedBot(g, [session("mockdata", origin="mock"), session(GUILD)])
    assert bot2.active_raid(comp("whatever")).guild == GUILD, "a real raid first, as before"
    reply = run(bot.handle_feed_event({"type": "hello"}, comp("Oi Guild Name")))
    assert "isn't one I serve" in reply["note"] and bot.ops.lines[-1][0] == GUILD


def test_feed_status_is_per_guild(duo):
    from oibot_gm.feed import FeedServer

    fs = FeedServer(lambda ev, c: None, "t")
    for peer, g in (("p1", "duo"), ("p2", "demo"), ("p3", "")):
        fs.companions[peer] = types.SimpleNamespace(character=f"c-{peer}", client="x", events=0, silent_for=1, guild=g)
    bot = FeedBot(duo.guilds, [])
    bot.feed = fs
    assert "c-p1" in feed_status(bot, duo.b) and "c-p2" not in feed_status(bot, duo.b) and "c-p3" not in feed_status(bot, duo.b)
    assert feed_status(types.SimpleNamespace(feed=None), duo.a) == "disabled (OIBOT_FEED_TOKEN unset)"


# ---------------------------------------------------------------- ops, locks, split proposals, channels

def test_ops_ring_is_per_guild_and_listeners_hear_the_key(duo):
    heard = []
    client = types.SimpleNamespace(registries=duo.guilds, get_channel=lambda cid: None)
    ops = ops_mod.Ops(client)
    ops_mod.LISTENERS.append(heard.append)
    try:
        run(ops.emit(duo.a.config, "info", "demo line"))
        run(ops.emit(duo.b.config, "warn", "duo line"))
    finally:
        ops_mod.LISTENERS.remove(heard.append)
    assert [t for _, _, t in ops.recent_for(GUILD)] == ["demo line"] and [t for _, _, t in ops.recent_for(GUILD2)] == ["duo line"]
    assert heard == [GUILD, GUILD2] and not hasattr(ops, "recent")


def test_owner_dm_names_the_guild_only_when_there_are_several(reg, store):
    dms = []

    class U:
        async def send(self, text):
            dms.append(text)
    g = Guilds(store, ROOT)
    client = types.SimpleNamespace(registries=g, get_channel=lambda cid: None, get_user=lambda uid: U())
    run(ops_mod.Ops(client).emit(g.only().config, "error", "x"))
    run(ops_mod.Ops(client).emit(g.only().config, "error", "y", dm=False))
    assert len(dms) == 1 and dms[0].startswith("✖ `"), "one guild: exactly as before"


def test_owner_dm_names_the_guild_when_there_are_several(duo):
    dms = []

    class U:
        async def send(self, text):
            dms.append(text)
    client = types.SimpleNamespace(registries=duo.guilds, get_channel=lambda cid: None, get_user=lambda uid: dms.append(uid) or U())
    duo.b.config.name = "Duo *Guild*"  # a server name is data: escaped, never formatting
    run(ops_mod.Ops(client).emit(duo.b.config, "error", "x"))
    assert dms[0] == OWNER2 and dms[1].startswith("**Duo \\*Guild\\*** · ✖ `"), dms


def test_run_locks_are_per_guild_with_one_run_key(duo):
    from oibot_gm.discord_raid import RaidMixin

    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    bot = RaidMixin()

    async def go():
        async with bot.solving(duo.a, ev_a.key):
            async with bot.solving(duo.b, ev_b.key):  # not busy: another guild's run
                pass
            with pytest.raises(rc.SolverBusy):
                async with bot.solving(duo.a, ev_a.key):
                    pass
    run(go())
    assert bot.board_lock(duo.a, ev_a.key) is not bot.board_lock(duo.b, ev_b.key)
    assert bot.plan_lock(duo.a, ev_a.key) is not bot.plan_lock(duo.b, ev_b.key)
    assert bot.board_lock(duo.a, ev_a.key) is bot.board_lock(duo.a, ev_a.key)


def test_split_proposals_are_remembered_per_guild(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    bot = types.SimpleNamespace()
    configops.split_seen(bot)[(GUILD, ev_a.key, 7)] = {"strategy": "balanced", "layouts": [[["x"]]]}
    op = configops.ConfigOp(op="run_propose", target=ev_b.key, field="use")
    with pytest.raises(RegistryError, match="no proposal to use yet"):
        run(configops._propose(duo.b, duo.rs_b, ev_b, op, "Officer", bot, 7, None))


def test_set_channel_refuses_a_channel_from_another_server(duo):
    from oibot_gm.discord_pool import SetupMixin

    class Ch(discord.TextChannel):
        def __init__(self, cid, gid):
            self.id, self.name, self.guild = cid, "general", types.SimpleNamespace(id=gid)
    chans = {50: Ch(50, 1), 60: Ch(60, DUO_SERVER)}

    class Bot(SetupMixin):
        def get_channel(self, cid):
            return chans.get(cid)
    with pytest.raises(ValueError, match="another server"):
        run(Bot().set_channel(duo.b, "signup", 50, "Owner"))
    assert run(Bot().set_channel(duo.b, "signup", 60, "Owner")) == "signup channel → #general" and duo.b.config.signup_channel_id == 60


def test_guild_scope_sync_covers_the_test_server_and_every_configured_one(duo):
    bot = types.SimpleNamespace(registries=duo.guilds, test_guild=DUO_SERVER)
    assert OibotGM.sync_servers(bot) == [DUO_SERVER, 1], "each server once"
    bot.test_guild = 77
    assert OibotGM.sync_servers(bot) == [77, 1, DUO_SERVER]


def test_pool_listener_attach_is_idempotent(duo):
    from oibot_gm.discord_pool import PoolMixin

    bot = PoolMixin()
    bot.registries = duo.guilds
    bot.attach_pool_listeners()
    bot.attach_pool_listener(duo.b)
    assert duo.b.listeners.count(bot.on_registry_change) == 1 and duo.a.listeners.count(bot.on_registry_change) == 1


def test_loot_thread_check_is_per_guild(duo, monkeypatch):
    """/raid loot in duo opens its own thread although demo already has a loot session on the SAME run key; a second
    /raid loot in demo still finds demo's."""
    from conftest import join
    from test_flows_raid import FakeBot as RaidBot, FakeChannel

    from oibot_gm import flows_raid  # noqa: F401 — registers raid_loot
    from oibot_gm.discord_bot import MockEvent

    monkeypatch.setattr(dr, "is_officer", lambda i, r: True)
    monkeypatch.setattr(MockEvent, "save", lambda self, msg=None: None)
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b)
    for reg, rs, ev in ((duo.a, duo.rs_a, ev_a), (duo.b, duo.rs_b, ev_b)):
        join(reg, rs, ev, reg.test_members()[:10])
        lock_with_board(reg, rs, ev, [m.display_name for m in reg.test_members()[:10]])
    bot = RaidBot(duo.rs_a)
    stores = {GUILD: duo.rs_a, GUILD2: duo.rs_b}
    bot.raids = types.SimpleNamespace(store=lambda r: stores[r.key])
    ch = FakeChannel()  # one channel object: its fake thread ids stay unique, as Discord's are

    def loot(reg, server):
        h = Harness(reg, stores[reg.key], client=bot)
        h.guild_id, h.channel = server, ch
        run(FLOWS["raid_loot"](h.command(), reg))
        return h
    first = loot(duo.a, 1)
    assert "Loot thread open" in first.text
    (thread_a,) = [t for t, s in bot.events.items() if s.guild == GUILD]
    second = loot(duo.b, DUO_SERVER)
    assert "Loot thread open" in second.text and "already open" not in second.text, "demo's session is not duo's"
    assert sorted((s.id, s.guild) for s in bot.events.values()) == [(ev_a.key, GUILD), (ev_b.key, GUILD2)]
    assert duo.rs_b.events[ev_b.key].thread_id and duo.rs_a.events[ev_a.key].thread_id == thread_a
    again = loot(duo.a, 1)
    assert again.text.endswith(f"Loot thread already open: <#{thread_a}>") and len(bot.events) == 2


def test_healthz_reports_each_guild(duo):
    from fastapi.testclient import TestClient

    from oibot_gm.web import app as web
    from test_mcp import FakeBot

    bot = FakeBot(duo.a, duo.rs_a)
    bot.registries = duo.guilds
    bot.get_guild = lambda gid: object() if gid == DUO_SERVER else None
    body = TestClient(web.create_app(bot)).get("/healthz").json()
    assert body["guilds"] == 2 and body["per_guild"] == {GUILD: {"server": False}, GUILD2: {"server": True}} and body["guilds_failed"] == 0


def test_two_guild_fixture_holds_a_colliding_schedule_key(duo):
    ev_a, ev_b = same_run(duo.a, duo.rs_a, duo.b, duo.rs_b, schedule="fri")
    assert "-fri-" in ev_a.key and duo.rs_a.events[ev_a.key] is not duo.rs_b.events[ev_b.key]
    shared = {m.display_name for m in duo.a.test_members()} & {m.display_name for m in duo.b.test_members()}
    assert len(shared) == len(duo.b.test_members()), "the same puppet names in both guilds"
    assert duo.a.config.owner_discord_id == OWNER and duo.b.config.owner_discord_id == OWNER2
    add_duo  # noqa: B018 — the helper the fixture uses (no second folder is ever written)
