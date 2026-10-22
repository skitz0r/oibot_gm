"""Dual interfaces phase 3 — setup (design §5.46; out/dual_interface_design.md §3.4 X3, §3.5 O-01/O-12/O-13/OPS-01/S-01/
S-05/S-06/M-01, §3.3 cluster D; owner decisions D29, D31, D32, D39, D44, D46).

perms.missing_for (the matrix); SetupMixin.set_channel (permission refusal, "Discord is unreachable", post first then
save, ONE audit line); the eight channel shortcuts → one helper (lint); the hourly check warns once; detach / attach;
on_guild_join / on_guild_remove / on_guild_available; the error-alert throttle (one DM, the edit, error_dm_ids, the ops
channel failure alert); applications (ordering, cap, the decided refusal, the prior line, the member role, the own-
application state); on_member_remove → left_at and the officers' Remove record (no member delete route); a superuser's
change DMs no owner (D44); the channels / officers hubs and the channel confirm (harness); the web routes (TestClient)
and the MCP tools' shapes."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import types
from pathlib import Path

import discord
import pytest
from conftest import GUILD, OFFICER_ROLE, OWNER, open_test_run
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from wizard_harness import Harness
from web_helpers import post_confirmed

from oibot_gm import configops, discord_registry, ops as ops_mod, perms
from oibot_gm.configops import ConfigOp
from oibot_gm.discord_pool import AbsencesMixin, PoolMixin, SetupMixin, channel_consequences
from oibot_gm.discord_registry import Guilds
from oibot_gm.registry import Registry, RegistryError
from oibot_gm.web import app as web

SRC = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
SUPER = 777_000_000_000_000_001
OFFICER_UID, MEMBER_UID, OUTSIDER_UID = 300_000_000_000_000_001, 300_000_000_000_000_002, 300_000_000_000_000_003
SECRET = "s" * 40
TOKEN = "a" * 64
ALL = dict(view_channel=True, send_messages=True, embed_links=True, manage_messages=True, create_public_threads=True,
           send_messages_in_threads=True, read_message_history=True, manage_webhooks=True, attach_files=True, use_external_emojis=True,
           create_instant_invite=True)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- Discord doubles

class Msg:
    _ids = iter(range(900_000, 999_999))

    def __init__(self, ch, content=None):
        self.id, self.channel, self.content, self.deleted, self.pinned, self.edits = next(Msg._ids), ch, content, False, False, []

    async def pin(self):
        self.pinned = True

    async def delete(self):
        self.deleted = True

    async def edit(self, **kw):
        self.edits.append(kw)
        return self


class Me:
    def __init__(self, **flags):
        self.id = 4_000
        self.guild_permissions = discord.Permissions(**flags)
        self.top_role = types.SimpleNamespace(position=50)


class Role:
    def __init__(self, rid, name, *, default=False, managed=False, position=1):
        self.id, self.name, self._default, self.managed, self.position = rid, name, default, managed, position

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position


class FakeGuild:
    def __init__(self, gid=1, name="Demo Server", **flags):
        self.id, self.name, self.unavailable = gid, name, False
        self.me = Me(**(flags or ALL))
        self.default_role = Role(gid, "@everyone", default=True)
        self.roles = [self.default_role, Role(OFFICER_ROLE, "Officer", position=5), Role(55, "Bots", managed=True), Role(66, "Member", position=3)]
        self.text_channels: list = []
        self.members: dict[int, object] = {}

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_channel(self, cid):
        return None

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        if uid in self.members:
            return self.members[uid]
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nope"), "Unknown Member")


class Chan(discord.TextChannel):
    """A text channel: `perms` = what the bot holds there; sends are recorded; `fail` makes the next send raise."""

    def __init__(self, cid, name, guild, **perms_):
        self.id, self.name, self.guild = cid, name, guild
        self._perms = discord.Permissions(**(perms_ if perms_ else ALL))
        self.sent: list[Msg] = []
        self.msgs: dict[int, Msg] = {}
        self.fail = False
        self.hooks: list = []
        self.position = cid
        self.category_id = None

    def permissions_for(self, who):
        return self._perms

    async def send(self, content=None, **kw):
        if self.fail:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        m = Msg(self, content)
        self.sent.append(m)
        self.msgs[m.id] = m
        return m

    async def fetch_message(self, mid):
        return self.msgs[mid]

    def overwrites_for(self, target):
        return discord.PermissionOverwrite()

    async def set_permissions(self, *a, **kw):
        return None

    async def webhooks(self):
        return list(self.hooks)

    async def create_webhook(self, name, reason=None):
        h = types.SimpleNamespace(name=name, user=types.SimpleNamespace(id=4_000), url=f"https://discord.com/api/webhooks/1/{name.replace(' ', '')}")
        self.hooks.append(h)
        return h


class Lines:
    """The ops feed as a list (guild key, level, text)."""

    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None, dm=True):
        self.lines.append((getattr(cfg, "key", None), level, text))

    async def audited(self, reg, by, line, via="discord", level="info", by_id=None):
        return await ops_mod.audited(self, reg, by, line, via=via, level=level, by_id=by_id)

    def recent_for(self, key):
        return []


class SetupBot(SetupMixin, PoolMixin, AbsencesMixin):
    """The setup verbs on a fake gateway: one server (FakeGuild) with a few channels."""

    def __init__(self, reg, guild=None, ready=True):
        self.reg = reg
        self.guild = guild or FakeGuild(reg.config.discord_guild_id)
        self.chans: dict[int, Chan] = {}
        self.ops = Lines()
        self._ready = ready
        self.registries = Guilds.of(reg.store, reg)
        self.user = types.SimpleNamespace(id=4_000)
        self.raids = None
        self.dms: list[tuple[int, str]] = []

    def add(self, cid, name, **flags):
        ch = Chan(cid, name, self.guild, **flags)
        self.chans[cid] = ch
        self.guild.text_channels.append(ch)
        return ch

    def get_channel(self, cid):
        return self.chans.get(cid)

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def is_ready(self):
        return self._ready

    async def refresh_pool(self, reg, announce=False):
        return []

    def ico(self, kind, key):
        return ""


# ---------------------------------------------------------------- perms: the matrix

@pytest.mark.parametrize("kind,need", [
    ("signup", {"view_channel", "send_messages", "embed_links"}),
    ("ops", {"view_channel", "send_messages", "embed_links"}),
    ("registration", {"view_channel", "send_messages", "embed_links", "manage_messages"}),
    ("absences", {"view_channel", "send_messages", "embed_links", "manage_messages"}),
    ("roster", {"view_channel", "send_messages", "embed_links", "create_public_threads", "send_messages_in_threads"}),
    ("news", {"view_channel", "send_messages", "embed_links", "read_message_history"}),
])
def test_perms_matrix(kind, need):
    g = FakeGuild()
    assert set(perms.required(kind)) == need
    assert perms.missing_for(Chan(1, "x", g), kind) == [], "everything held: nothing missing"
    for p in need:  # each one alone is reported, by Discord's own name
        held = {k: v for k, v in ALL.items() if k != p}
        assert perms.missing_for(Chan(1, "x", g, **held), kind) == [p]
    assert perms.missing_for(Chan(1, "x", types.SimpleNamespace(id=1, me=None)), kind) == [], "no gateway member: an outage is not a missing permission"


def test_perms_server_wide_invite_and_words():
    v = discord.Permissions(perms.permissions_value())
    assert v.manage_webhooks and v.send_messages and v.create_instant_invite and not v.manage_roles and not v.administrator, "D39 in, D31 optional"
    assert perms.missing_for(FakeGuild()) == []
    g = FakeGuild(**{k: v for k, v in ALL.items() if k not in ("manage_webhooks", "embed_links")})
    assert set(perms.missing_for(g)) == {"manage_webhooks", "embed_links"}
    assert perms.optional_missing(g) == ["manage_roles", "manage_channels"]
    assert perms.cant_post("raids", ["send_messages", "manage_messages"]) == "I can't post in #raids: missing Send Messages, Manage Messages (to pin the card)."
    assert "permissions=" + str(perms.permissions_value()) in perms.invite_link("42") and "guild_id=9" in perms.invite_link("42", 9)


# ---------------------------------------------------------------- set_channel

def test_set_channel_refuses_a_channel_it_cant_post_in_and_saves_nothing(reg):
    bot = SetupBot(reg)
    bot.add(50, "raids", view_channel=True, send_messages=True)  # no Embed Links
    with pytest.raises(ValueError, match=r"I can't post in #raids: missing Embed Links\."):
        run(bot.set_channel(reg, "signup", 50, "Jon"))
    assert reg.config.signup_channel_id is None and bot.ops.lines == []
    bot.add(51, "cards", view_channel=True, send_messages=True, embed_links=True)  # no Manage Messages: the card can't be pinned
    with pytest.raises(ValueError, match="Manage Messages"):
        run(bot.set_channel(reg, "registration", 51, "Jon"))


def test_set_channel_says_discord_is_unreachable_apart_from_a_bad_channel(reg):
    bot = SetupBot(reg, ready=False)
    with pytest.raises(ValueError, match="Discord is unreachable right now"):
        run(bot.set_channel(reg, "signup", 99, "Jon"))
    bot._ready = True
    with pytest.raises(ValueError, match="isn't a text channel I can see"):
        run(bot.set_channel(reg, "signup", 99, "Jon"))
    bot.guild.unavailable = True
    with pytest.raises(ValueError, match="unreachable"):
        run(bot.set_channel(reg, "signup", 99, "Jon"))


def test_registration_card_is_posted_first_then_the_old_one_dropped(reg):
    bot = SetupBot(reg)
    old, new = bot.add(60, "old-reg"), bot.add(61, "register")
    run(bot.set_channel(reg, "registration", 60, "Jon"))
    first = old.sent[-1]
    assert reg.config.registration_message_id == first.id and first.pinned
    new.fail = True  # the new channel refuses the send: the old card and the config stay as they were
    with pytest.raises(discord.Forbidden):
        run(bot.set_channel(reg, "registration", 61, "Jon"))
    assert not first.deleted and reg.config.registration_channel_id == 60 and reg.config.registration_message_id == first.id
    new.fail = False
    run(bot.set_channel(reg, "registration", 61, "Jon"))
    assert first.deleted and reg.config.registration_channel_id == 61 and reg.config.registration_message_id == new.sent[-1].id


def test_absences_card_posts_first_too(reg):
    bot = SetupBot(reg)
    a, b = bot.add(70, "away"), bot.add(71, "away2")
    run(bot.set_channel(reg, "absences", 70, "Jon"))
    card = a.sent[-1]
    b.fail = True
    with pytest.raises(discord.Forbidden):
        run(bot.set_channel(reg, "absences", 71, "Jon"))
    assert not card.deleted and reg.config.absences_message_id == card.id


def test_one_audit_line_per_channel_change(reg):
    bot = SetupBot(reg)
    bot.add(50, "raids")
    assert run(bot.set_channel(reg, "signup", 50, "Jon", via="web", by_id=7)) == "signup channel → #raids"
    assert bot.ops.lines == [(GUILD, "info", "[web] Jon: signup channel → #raids")]
    bot.ops.lines.clear()
    run(configops.apply_async(reg, ConfigOp(op="set", path="ops_channel", value="<#50>"), "Jon", True, bot=bot, by_id=7, via="mcp"))
    assert bot.ops.lines == [(GUILD, "info", "[mcp] Jon: ops channel → #raids")], "apply_async never doubles set_channel's line"


def test_a_superusers_channel_change_dms_no_owner(reg, monkeypatch):
    """D44: a host superuser's change is an ops line marked (host) — never a DM to the guild's owner."""
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(SUPER))
    sent = []

    class U:
        async def send(self, text):
            sent.append(text)
    client = types.SimpleNamespace(get_channel=lambda cid: None, get_user=lambda uid: U(), registries=None, is_ready=lambda: True)
    bot = SetupBot(reg)
    bot.ops = ops_mod.Ops(client)
    bot.add(50, "raids")
    run(bot.set_channel(reg, "signup", 50, "Host", via="web", by_id=SUPER))
    assert bot.ops.recent_for(GUILD)[-1][2] == "[web] Host (host): signup channel → #raids" and sent == []


def test_audit_helpers_never_dm():
    """The lint behind D44: ops.audited / audit_line write a line; only an ERROR reaches the DM path (Ops._alert)."""
    src = (SRC / "ops.py").read_text()
    body = src.split("def audit_line", 1)[1].split("class Ops", 1)[0]
    assert ".send(" not in body and "_dm_all" not in body and "_alert" not in body
    emit = src.split("    async def emit", 1)[1].split("    async def _post", 1)[0]
    assert 'if level == "error" and dm:' in emit, "only an error alerts anyone"


def test_channel_consequences_from_code(reg):
    bot = SetupBot(reg)
    bot.add(50, "raids")
    bot.add(51, "nopost", view_channel=True)
    reg.config.signup_channel_id = 50
    assert channel_consequences(reg, "signup", None, bot) == ["Sheets will have nowhere to post until a signup channel is set."]
    assert channel_consequences(reg, "signup", 50, bot) == ["Signups is already #raids — nothing changes."]
    reg.config.ops_channel_id = 50
    reg.config.applications_channel_id = 51
    assert channel_consequences(reg, "applications", None, bot) == ["Applications fall back to the ops channel #raids."]
    lines = channel_consequences(reg, "registration", 51, bot)
    assert lines[0] == "Posts and pins the registration card in #nopost." and any("I can't post in #nopost: missing Send Messages" in x for x in lines)
    assert "creates a webhook" in channel_consequences(reg, "news", 50, bot)[0]
    # the same sentences ride into every surface's review through configops.CONSEQUENCE_HOOKS
    perms.CLIENT = bot
    try:
        d = configops.describe_ops(reg, configops.ops_from_fields(reg, "settings", None, {"channel:registration": "51"}))
        assert d.review and any("I can't post in #nopost" in c for c in d.consequences)
    finally:
        perms.CLIENT = None


def test_eight_shortcuts_are_one_helper_and_no_command_writes_config():
    """Lint: the channel commands and cfg_owner never write reg.config (Registry verbs / set_channel do, under the lock),
    and every shortcut is the one helper (FLOWS['channel_set'])."""
    src = (SRC / "discord_registry.py").read_text()
    reg_cmds = src.split("def register_commands", 1)[1]
    for name in ("channel_command", "channel_shortcut", "cfg_owner", "cfg_channels", "cfg_officers", "cfg_role", "cfg_errors_to"):
        body = reg_cmds.split(f"def {name}", 1)[1].split("\n    @", 1)[0].split("\n    def ", 1)[0]
        assert not re.search(r"reg\.config\.\w+\s*=(?!=)", body) and "setattr(reg.config" not in body, name
        assert "ops.emit(" not in body, f"{name}: set_channel / apply_async write the one line"
    assert len(discord_registry.CHANNEL_SHORTCUTS) == 8
    assert {k for k, _n, _d in discord_registry.CHANNEL_SHORTCUTS} == {"ops", "applications", "roster", "registration", "analytics", "absences", "signup", "news"}
    assert 'FLOWS["channel_set"](interaction, reg, kind=kind, channel=channel, clear=clear)' in reg_cmds


def test_the_command_tree_builds_with_the_new_commands(store, reg):
    tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.default()))
    discord_registry.register_commands(tree, Guilds.of(store, reg), Lines(), lambda k, v: "")
    gm = next(c for c in tree.get_commands() if c.name == "gm")
    config = next(c for c in gm.commands if c.name == "config")
    names = {c.name for c in config.commands}
    assert {"channels", "officers", "errors-to", "officer-role", "signup-channel", "news-channel"} <= names
    sig = next(c for c in config.commands if c.name == "signup-channel")
    assert [p.name for p in sig.parameters] == ["channel", "clear"] and not any(p.required for p in sig.parameters)
    roster = next(c for c in tree.get_commands() if c.name == "roster")
    assert "remove-record" in {c.name for c in roster.commands}


# ---------------------------------------------------------------- the hourly permission check

def test_hourly_check_warns_once_and_again_after_a_relapse(reg):
    bot = SetupBot(reg)
    ch = bot.add(50, "raids")
    reg.config.signup_channel_id = 50
    assert run(bot.check_channel_perms(reg)) == {} and bot.ops.lines == []
    ch._perms = discord.Permissions(view_channel=True)
    probs = run(bot.check_channel_perms(reg))
    assert "signup" in probs and len(bot.ops.lines) == 1 and "I can't post in #raids" in bot.ops.lines[0][2]
    assert bot.ops.lines[0][1] == "error", "a required channel (signups): the owner must act — the throttled error alert"
    run(bot.check_channel_perms(reg))
    assert len(bot.ops.lines) == 1, "the same problem is not repeated"
    ch._perms = discord.Permissions(**ALL)
    run(bot.check_channel_perms(reg))
    ch._perms = discord.Permissions(view_channel=True)
    run(bot.check_channel_perms(reg))
    assert len(bot.ops.lines) == 2, "fixed, then broken again: reported again"
    bot.chans.pop(50)
    run(bot.check_channel_perms(reg))
    assert "is gone" in bot.ops.lines[-1][2]


def test_setup_tick_runs_hourly_and_skips_an_outage(reg):
    bot = SetupBot(reg)
    bot.add(50, "raids", view_channel=True)
    reg.config.signup_channel_id = 50
    run(bot.setup_tick(now=1000.0))
    run(bot.setup_tick(now=1500.0))
    assert len([x for x in bot.ops.lines if "Channel check" in x[2]]) == 1 and perms.CLIENT is bot
    bot.guild.unavailable = True
    assert bot.channel_problems(reg) == {}, "an unavailable server reports nothing"
    perms.CLIENT = None


def test_setup_checklist_rows(reg):
    bot = SetupBot(reg)
    bot.add(50, "raids")
    reg.config.signup_channel_id = 50
    rows = {r["key"]: r for r in bot.setup_checklist(reg)}
    assert rows["owner"]["state"] == "ok" and rows["channel:signup"]["state"] == "ok" and rows["channel:ops"]["state"] == "bad"
    assert rows["channel:roster"]["state"] == "warn" and rows["permissions"]["state"] == "ok"
    assert rows["error_dms"]["text"] == "the owner" and "optional:manage_roles" in rows
    reg.config.officer_role_ids = []
    assert next(r for r in bot.setup_checklist(reg) if r["key"] == "officer_roles")["state"] == "warn", "none set: amber, Manage Server only"


# ---------------------------------------------------------------- detach / attach, guild events

def test_detach_parks_the_guild_and_attach_serves_it_again(duo, monkeypatch):
    g = duo.guilds
    assert g.detach(duo.b.key) is duo.b
    assert duo.b.key not in g.by_key and duo.b not in g.ordered() and g.for_guild(2) is None and g.only() is duo.a
    assert [r.key for r in g.detached_list()] == [duo.b.key] and duo.b.config.detached_at
    assert "parked" in (g.claim_refusal(2) or ""), "a parked server can't be claimed as a second guild"
    reloaded = Guilds(duo.a.store, Path(__file__).resolve().parents[1])
    assert duo.b.key in reloaded.detached and duo.b.key not in reloaded.by_key, "a restart keeps it parked"
    assert g.attach_server(2) is duo.b and g.for_guild(2) is duo.b and not duo.b.config.detached_at and not g.detached
    assert g.attach_server(2) is None


def test_a_detached_guild_sends_no_dm(reg):
    from oibot_gm.discord_raid import RaidMixin

    class B(RaidMixin):
        async def fetch_user(self, uid):
            raise AssertionError("no DM from a detached guild")
    reg.config.detached_at = "2026-10-20T10:00:00"
    m = real_member(reg)
    assert run(B().send_member_card(reg, m, "hello")) == (False, None)


class EventsBot:
    """OibotGM's guild events on a fake client."""
    from oibot_gm.discord_bot import OibotGM as _G
    on_guild_join = _G.on_guild_join
    on_guild_remove = _G.on_guild_remove
    on_guild_available = _G.on_guild_available
    on_member_remove = _G.on_member_remove
    on_member_join = _G.on_member_join
    reattach = _G.reattach
    host_ids = _G.host_ids

    def __init__(self, guilds):
        self.registries = guilds
        self.ops = Lines()
        self.dms: list[tuple[int, str]] = []
        self.added: list[str] = []

    async def guild_added(self, reg):
        self.added.append(reg.key)

    def get_user(self, uid):
        bot = self

        class U:
            async def send(self, text):
                bot.dms.append((uid, text))
        return U()


def test_on_guild_join_dms_the_host_once_with_missing_permissions(duo, monkeypatch):
    monkeypatch.setenv("OIBOT_SUPERUSERS", f"{SUPER}")
    bot = EventsBot(duo.guilds)
    g = FakeGuild(9, "New *Place*", **{k: v for k, v in ALL.items() if k != "manage_webhooks"})
    run(bot.on_guild_join(g))
    assert [u for u, _ in bot.dms] == [SUPER]
    text = bot.dms[0][1]
    assert "I was added to **New \\*Place\\***. It isn't a guild yet — `/gm config owner` there claims it." in text and "Manage Webhooks" in text
    monkeypatch.setenv("OIBOT_SUPERUSERS", "")
    bot.dms.clear()
    run(bot.on_guild_join(FakeGuild(10, "Other")))
    # regression (final review): no superusers used to DM EVERY guild's owner about a server they can't act on (DM rule)
    assert bot.dms == [], "no superusers and several guilds: nobody is DMed (the log and the Host card carry it)"
    lone = EventsBot(Guilds.of(duo.a.store, duo.a))
    run(lone.on_guild_join(FakeGuild(11, "Third")))
    assert [u for u, _ in lone.dms] == [duo.a.config.owner_discord_id], "one guild: its owner is effectively the host"


def test_on_guild_remove_detaches_but_an_outage_does_not(duo):
    bot = EventsBot(duo.guilds)
    out = FakeGuild(2)
    out.unavailable = True
    run(bot.on_guild_remove(out))
    assert duo.guilds.for_guild(2) is duo.b and bot.ops.lines == []
    run(bot.on_guild_remove(FakeGuild(2)))
    assert duo.guilds.for_guild(2) is None and bot.ops.lines[-1][:2] == (duo.b.key, "error") and "parked" in bot.ops.lines[-1][2]
    run(bot.on_guild_available(FakeGuild(2)))
    assert duo.guilds.for_guild(2) is duo.b and bot.added == [duo.b.key] and "served again" in bot.ops.lines[-1][2]
    run(bot.on_guild_remove(FakeGuild(2)))
    run(bot.on_guild_join(FakeGuild(2)))  # invited back: re-attached, no "isn't a guild yet" DM
    assert duo.guilds.for_guild(2) is duo.b and bot.dms == []


def test_on_member_remove_sets_left_at_and_join_clears_it(reg):
    bot = EventsBot(Guilds.of(reg.store, reg))
    m = reg.test_members()[0]
    who = types.SimpleNamespace(id=m.discord_id, guild=types.SimpleNamespace(id=1))
    run(bot.on_member_remove(who))
    assert reg.members[m.discord_id].left_at
    run(bot.on_member_join(who))
    assert reg.members[m.discord_id].left_at is None


def test_claim_tells_every_other_guilds_ops_channel_no_owner_dm(duo, monkeypatch):
    from oibot_gm.discord_bot import OibotGM

    monkeypatch.setenv("OIBOT_SUPERUSERS", "")

    class B(EventsBot):
        claim_guild = OibotGM.claim_guild
        ctx = types.SimpleNamespace(guild_key="mock")
    bot = B(duo.guilds)
    reg = run(bot.claim_guild(types.SimpleNamespace(id=9, name="Third"), 4242, "forever", types.SimpleNamespace(display_name="C", id=4242)))
    others = [k for k, lvl, t in bot.ops.lines if "also serves another server: Third" in t]
    assert sorted(others) == sorted([duo.a.key, duo.b.key]) and reg.key not in others and bot.dms == [], "D44: ops lines, never an owner DM"


# ---------------------------------------------------------------- the error-alert throttle (D46)

class AlertClient:
    def __init__(self):
        self.sent: list[tuple[int, Msg]] = []
        self.registries = None
        self.chan = None

    def get_channel(self, cid):
        return self.chan

    def is_ready(self):
        return True

    def get_user(self, uid):
        client = self

        class U:
            async def send(self, text):
                m = Msg(None, text)
                client.sent.append((uid, m))
                return m
        return U()


def test_second_error_inside_the_window_edits_the_first_dm(reg):
    c = AlertClient()
    o = ops_mod.Ops(c)
    reg.config.ops_channel_id = None
    run(o.emit(reg.config, "error", "/raid lock by Jon: KeyError: 123"))
    run(o.emit(reg.config, "error", "/raid lock by Ana: KeyError: 456"))
    run(o.emit(reg.config, "error", "/raid lock by Bo: KeyError: 789"))
    assert len(c.sent) == 1 and c.sent[0][0] == OWNER
    first = c.sent[0][1]
    assert len(first.edits) == 1 and "and 1 more like this since" in first.edits[-1]["content"] and "details in the bot's log" in first.edits[-1]["content"]
    assert o.suppressed_for(GUILD) == 2
    for k in o._alerts:  # a minute later the next repeat edits again, with the count caught up
        o._alerts[k]["edited"] -= ops_mod.EDIT_EVERY_S
    run(o.emit(reg.config, "error", "/raid lock by Di: KeyError: 9"))
    assert len(first.edits) == 2 and "and 3 more like this since" in first.edits[-1]["content"]
    run(o.emit(reg.config, "error", "a different failure"))
    assert len(c.sent) == 2, "another signature: its own DM"
    run(o.emit(reg.config, "warn", "just a warning"))
    assert len(c.sent) == 2, "errors only"
    for k in o._alerts:  # the window passed
        o._alerts[k]["at"] -= ops_mod.ERROR_WINDOW_S + 1
    run(o.emit(reg.config, "error", "/raid lock by Cy: KeyError: 1"))
    assert len(c.sent) == 3


def test_the_alert_window_opens_only_on_a_delivered_dm_and_a_burst_edits_once(reg):
    """G3 review: the throttle opened its 30-minute window even when the DM failed (closed DMs: no alert for half an
    hour, though nobody was told) and edited the DM on every repeat (a burst of 50 errors = 50 edits). The window opens
    only on a delivered DM; a burst edits at most once per EDIT_EVERY_S; the ops line still goes every time."""
    c = AlertClient()
    o = ops_mod.Ops(c)
    reg.config.ops_channel_id = 50
    c.chan = Chan(50, "ops", FakeGuild())
    sent_ops = []
    c.chan.send = lambda text, **kw: _async(sent_ops.append(text))
    closed = {"on": True}
    real_user = c.get_user

    def get_user(uid):
        if closed["on"]:
            class Closed:
                async def send(self, text):
                    raise RuntimeError("Cannot send messages to this user")
            return Closed()
        return real_user(uid)
    c.get_user = get_user
    run(o.emit(reg.config, "error", "boom: KeyError 1"))
    assert c.sent == [] and not o._alerts, "nothing delivered: no window"
    closed["on"] = False
    run(o.emit(reg.config, "error", "boom: KeyError 2"))
    assert len(c.sent) == 1, "the next one is DMed: the earlier failure didn't hold it back"
    for i in range(20):
        run(o.emit(reg.config, "error", f"boom: KeyError {i + 3}"))
    assert len(c.sent) == 1 and len(c.sent[0][1].edits) == 1, "one edit for the burst"
    assert len(sent_ops) == 22, "the ops line goes every time"


async def _async(x=None):
    return x


def test_error_dm_ids_route_the_alert(reg):
    c = AlertClient()
    o = ops_mod.Ops(c)
    reg.config.ops_channel_id = None
    reg.set_error_dms([11, 22], "Jon")
    run(o.emit(reg.config, "error", "boom"))
    assert sorted(u for u, _ in c.sent) == [11, 22]
    reg.set_error_dms([], "Jon")
    run(o.emit(reg.config, "error", "boom two"))
    assert len(c.sent) == 2, "[] = nobody: the ops channel only"
    reg.set_error_dms(None, "Jon")
    assert reg.error_dm_targets() == [OWNER] and reg.error_dm_words() == "the owner"


def test_an_ops_channel_the_bot_cant_post_in_alerts_after_three_tries(reg):
    c = AlertClient()
    o = ops_mod.Ops(c)
    reg.config.ops_channel_id = 50  # the client can't see it
    for i in range(5):
        run(o.emit(reg.config, "info", f"line {i}"))
    assert len(c.sent) == 1 and "I can't post in the ops channel <#50> (3 tries in a row" in c.sent[0][1].content
    assert o.ops_channel_failing(GUILD) == 5
    c.chan = Chan(50, "ops", FakeGuild())
    run(o.emit(reg.config, "info", "back"))
    assert o.ops_channel_failing(GUILD) == 0


def test_a_flapping_ops_channel_is_one_dm_per_window(reg):
    """Regression (final review): one good post reset the guard, so fail ×3 / ok / fail ×3 DMed again and again; an
    outage counted as a permissions problem; the hourly check's report and the post failures were two DMs."""
    c = AlertClient()
    o = ops_mod.Ops(c)
    reg.config.ops_channel_id = 50
    for _ in range(2):
        c.chan = None
        for i in range(3):
            run(o.emit(reg.config, "info", f"line {i}"))
        c.chan = Chan(50, "ops", FakeGuild())
        run(o.emit(reg.config, "info", "back"))
    assert len(c.sent) == 1, "the second streak inside 30 minutes edits the first DM"
    # an outage: the server is unavailable — not counted, no DM
    o2 = ops_mod.Ops(AlertClient())
    c2 = o2.client
    down = FakeGuild()
    down.unavailable = True
    c2.chan = Chan(50, "ops", down)
    for i in range(5):
        run(o2.emit(reg.config, "info", f"line {i}"))
    assert c2.sent == [] and o2.ops_channel_failing(GUILD) == 0
    # the hourly check already reported the ops channel: the failing posts that follow are folded into it
    o3 = ops_mod.Ops(AlertClient())
    o3.known(GUILD, ops_mod.OPS_CHANNEL_SIG)
    for i in range(4):
        run(o3.emit(reg.config, "info", f"line {i}"))
    assert o3.client.sent == []


def test_error_dms_through_configops(reg):
    ops = configops.ops_from_fields(reg, "settings", None, {"error_dm_ids": ["11", 22]})
    assert ops[0].path == "error_dms" and ops[0].value == "<@11>, <@22>"
    assert configops.describe(reg, ops[0]) == "error alerts: the owner → <@11>, <@22>"
    configops.apply(reg, ops[0], "Jon", True)
    assert reg.config.error_dm_ids == [11, 22]
    configops.apply(reg, ConfigOp(op="set", path="error_dms", value="owner"), "Jon", True)
    assert reg.config.error_dm_ids is None
    configops.apply(reg, ConfigOp(op="set", path="error_dms", value="none"), "Jon", True)
    assert reg.config.error_dm_ids == []
    with pytest.raises(RegistryError, match="isn't a member"):
        configops.apply(reg, ConfigOp(op="set", path="error_dms", value="Jonny"), "Jon", True)
    with pytest.raises(RegistryError, match="whole list"):
        configops.ops_from_fields(reg, "settings", None, {"error_dm_ids": "11"})


def test_leaked_mcp_token_alerts_the_host_only(reg, rs, monkeypatch):
    """Regression (final review): the leak alert went to the guild named by the request's own X-Oibot-Guild header (or
    every guild) — its owner DMed to rotate a .env they can't touch, its officers reading the caller's IP in the ops
    channel, the host maybe never told. Now: the host (host_ids), throttled, and no guild ops line."""
    from test_mcp import FakeBot

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    bot = FakeBot(reg, rs)
    alerts = []

    async def host_alert(ids, sig, text):
        alerts.append((list(ids), sig, text))
    bot.ops.host_alert = host_alert
    bot.host_ids = lambda: [SUPER]
    c = TestClient(web.create_app(bot), client=("203.0.113.9", 4000))
    r = c.get("/api/meta", headers={"Authorization": f"Bearer {TOKEN}", "cf-connecting-ip": "203.0.113.9", "X-Oibot-Guild": reg.key})
    assert r.status_code == 401
    assert [(a[0], a[1]) for a in alerts] == [([SUPER], "mcp-leak")] and "the token has leaked" in alerts[0][2] and TOKEN not in alerts[0][2]
    assert bot.ops.lines == [], "no guild's ops channel (its officers) and no guild error targets"


def test_host_alert_is_one_dm_per_window(reg):
    c = AlertClient()
    o = ops_mod.Ops(c)
    run(o.host_alert([SUPER], "mcp-leak", "leak 1"))
    run(o.host_alert([SUPER], "mcp-leak", "leak 2"))
    assert [u for u, _ in c.sent] == [SUPER]
    o._alerts[("", "host|mcp-leak")]["at"] -= ops_mod.ERROR_WINDOW_S + 1
    run(o.host_alert([SUPER], "mcp-leak", "leak 3"))
    assert len(c.sent) == 2


# ---------------------------------------------------------------- applications

def apply_one(reg, uid=7777, name="Kessa", surname="Dawnfire"):
    return reg.apply(uid, f"User{uid}", name, "Mage", "Frost", None, None, None, None, surname=surname)


def test_decide_adds_the_character_first_and_caps_the_note(reg):
    a = apply_one(reg)
    reg.add_character(5555, "Other", "Kessa", "Mage", "Frost", None, True, surname="Dawnfire")  # the name is taken since
    with pytest.raises(RegistryError, match="already"):
        reg.decide(a.discord_id, True, "Jon")
    assert reg.applicants[a.discord_id].status == "open", "never accepted in memory while open on disk"
    assert Registry(reg.store, reg.key, reg.base_profile).applicants[a.discord_id].status == "open"
    with pytest.raises(RegistryError, match="limited to 400"):
        reg.decide(a.discord_id, False, "Jon", "x" * 401)
    reg.decide(a.discord_id, False, "Jon", "full on mages")
    with pytest.raises(RegistryError, match=r"Already declined by Jon \("):
        reg.decide(a.discord_id, True, "Ana")


def test_a_second_application_carries_the_prior_decision(reg):
    a = apply_one(reg)
    assert reg.prior_decision_line(a) is None
    reg.decide(a.discord_id, False, "Jon", "full on mages")
    b = apply_one(reg, name="Kessia")
    line = reg.prior_decision_line(b)
    assert line.startswith("Applied before: declined ") and line.endswith("by Jon — full on mages") and b.history[0]["label"] == "Kessa Dawnfire"
    from oibot_gm.discord_registry import application_embed

    assert "Applied before: declined" in application_embed(reg, b).description


def test_application_state_is_their_own_first(reg):
    a = apply_one(reg)
    st = reg.application_state(a.discord_id)
    assert st["state"] == "open" and st["text"].startswith("Your application for Kessa Dawnfire is waiting for an officer")
    reg.decide(a.discord_id, False, "Jon", "next time")
    reg.set_recruitment("t", open=False)
    st = reg.application_state(a.discord_id)
    assert "wasn't accepted this time" in st["text"] and "Note: next time" in st["text"] and st["text"].endswith("You can apply again later.")
    assert reg.application_state(1) is None


def test_open_register_tells_a_pending_applicant_before_closed(reg, monkeypatch):
    from oibot_gm.wizard_flows import FLOWS

    a = apply_one(reg, uid=8888)
    reg.set_recruitment("t", open=False)
    user = types.SimpleNamespace(id=8888, display_name="User8888", mention="@x")
    h = Harness(reg, user=user)
    run(FLOWS["register"](h.command(), reg, mode="apply"))
    assert "You already have an open application for **Kessa Dawnfire**" in h.text and "isn't taking applications" not in h.text


class RoleBot:
    def __init__(self, guild):
        self.guild = guild
        self.ops = Lines()

    def get_guild(self, gid):
        return self.guild

    def get_channel(self, cid):
        return None

    async def fetch_user(self, uid):
        async def send(text):
            return None
        return types.SimpleNamespace(send=send)


def test_accept_gives_the_member_role_when_the_bot_can(reg):
    from oibot_gm.discord_registry import settle_application

    g = FakeGuild(**{**ALL, "manage_roles": True})
    added = []

    class M:
        roles: list = []

        async def add_roles(self, role, reason=None):
            added.append(role.name)
    a = apply_one(reg, uid=8001)
    g.members[a.discord_id] = M()
    reg.set_recruitment("t", member_role_ids=[66])
    line = run(settle_application(RoleBot(g), reg, a.discord_id, True, "Jon", None, by_id=1))
    assert added == ["Member"] and "gave them @Member" in line and reg.applicants[a.discord_id].role_note == "gave them @Member"
    b = apply_one(reg, uid=8002, name="Borin", surname="Stone")
    g.members[b.discord_id] = M()
    line = run(settle_application(RoleBot(FakeGuild()), reg, b.discord_id, True, "Jon", None))
    assert "the bot needs Manage Roles" in line, "without Manage Roles it says so; the acceptance stands"
    assert reg.applicants[b.discord_id].status == "accepted"


def test_a_failed_role_grant_says_words_never_a_type_name(reg):
    """Regression (final review, §2.7): add_roles failing put 'Forbidden' / 'HTTPException' in the Accept verdict."""
    import discord as d

    from oibot_gm.discord_registry import assign_member_role

    g = FakeGuild(**{**ALL, "manage_roles": True})
    resp = types.SimpleNamespace(status=403, reason="Forbidden")

    class Denied:
        roles: list = []

        async def add_roles(self, role, reason=None):
            raise d.Forbidden(resp, "Missing Permissions")

    class Broken(Denied):
        async def add_roles(self, role, reason=None):
            raise d.HTTPException(types.SimpleNamespace(status=500, reason="Server Error"), "oops")
    reg.set_recruitment("t", member_role_ids=[66])
    g.members[1] = Denied()
    g.members[2] = Broken()
    bot = RoleBot(g)
    a = run(assign_member_role(bot, reg, 1))
    b = run(assign_member_role(bot, reg, 2))
    assert a == "couldn't give them @Member: the bot isn't allowed to give that role (add it by hand)"
    assert b == "couldn't give them @Member: something went wrong (add it by hand)"
    for text in (a, b):
        assert "Forbidden" not in text and "HTTPException" not in text
    assert any(lvl == "error" for _k, lvl, *_ in bot.ops.lines), "the unexpected one is an ops error for the owner"


def test_the_application_verdict_is_one_audit_line(reg):
    from oibot_gm.discord_registry import settle_application

    a = apply_one(reg, uid=8003)
    bot = RoleBot(FakeGuild())
    run(settle_application(bot, reg, a.discord_id, False, "Jon", "sorry", by_id=1, via="web"))
    assert bot.ops.lines == [(GUILD, "info", "[web] Jon: Declined User8003 (Kessa Dawnfire) — sorry")]


def test_roster_applicant_opens_the_one_decide_screen(reg, monkeypatch):
    a = apply_one(reg, uid=8004)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    decided = []

    async def decide(r, uid, accept, by, note, by_id=None, via="discord"):
        decided.append((uid, accept, note, by_id))
        return "✅ done"
    h = Harness(reg, user=types.SimpleNamespace(id=1, display_name="Off", mention="@Off"))
    h.client = types.SimpleNamespace(decide_application=decide, ico=lambda k, v: "")
    run(discord_registry.application_decide(h.command(), reg, a.discord_id, False, "typed note"))
    assert "Your note: typed note" in h.text and h.buttons() == ["Decline", "Decline with a note…", "Back"]
    run(h.press("Decline"))
    assert decided == [(a.discord_id, False, "typed note", 1)]


# ---------------------------------------------------------------- Remove record (D29: officers only)

def real_member(reg, uid=9100, name="Realname"):
    m, _c = reg.add_character(uid, f"User{uid}", name, "Mage", "Frost", None, True)
    return m


def test_remove_record_only_for_someone_who_left(reg):
    m = real_member(reg)
    with pytest.raises(RegistryError, match="still in the server"):
        reg.remove_record(m.discord_id, "Jon")
    reg.member_left(m.discord_id)
    with pytest.raises(RegistryError, match="rostered on Barrow"):
        reg.remove_record(m.discord_id, "Jon", seats=["Barrow Deeps Tue 7:30 PM"])
    path = reg.store.root / reg.key / "members" / f"{m.discord_id}.json"
    assert path.exists()
    assert reg.remove_record(m.discord_id, "Jon") == f"{m.display_name}'s record removed"
    assert not path.exists() and m.discord_id not in reg.members
    with pytest.raises(RegistryError, match="test member"):
        t = reg.test_members()[0]
        reg.member_left(t.discord_id)
        reg.remove_record(t.discord_id, "Jon")


def test_no_member_route_deletes_their_own_record():
    """D29 lint: the only record removal is the officers' (/api/admin/member/remove → discord_registry.remove_record)."""
    callers = [p.name for p in SRC.rglob("*.py") if "remove_record(" in p.read_text() and p.name not in ("registry.py", "discord_registry.py")]
    assert sorted(callers) == ["api_setup.py", "mcp_server.py"]  # the MCP tool POSTs the officers' route
    assert '"/api/admin/member/remove"' in (SRC / "mcp_server.py").read_text()
    setup_src = (SRC / "web" / "api_setup.py").read_text()
    route = setup_src.split('@app.post("/api/admin/member/remove")', 1)[1][:900]
    assert "body(request, officer=True)" in route, "officers only"
    for p in (SRC / "web").glob("*.py"):
        for path in re.findall(r'@app\.(?:post|delete)\("(/api/me[^"]*)"', p.read_text()):
            assert not re.search(r"/api/me/(record|delete|leave|account)", path), path


# ---------------------------------------------------------------- the hubs (harness)

def owner_harness(reg, bot):
    h = Harness(reg, client=bot, user=types.SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"))
    return h


async def pick_widget(h, kind, placeholder, *values):
    sel = next((s for s in h._items() if isinstance(s, kind) and s.placeholder == placeholder), None)
    assert sel is not None, f"no {kind.__name__} {placeholder!r}; have {[getattr(s, 'placeholder', None) for s in h._items()]}"
    sel._values = list(values)
    itx = h._on_screen()
    assert await h.screen["view"].interaction_check(itx)
    await sel.callback(itx)


def test_channel_shortcut_confirm_then_set_channel(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    ch = bot.add(50, "raids")
    h = owner_harness(reg, bot)
    run(FLOWS["channel_set"](h.command(), reg, kind="signup"))
    assert h.text.endswith("Pick a channel or tick clear. `/gm config channels` shows them all.")
    run(FLOWS["channel_set"](h.command(), reg, kind="signup", channel=ch))
    assert "Use <#50> for this?" in h.text and "New sheets post in #raids." in h.text and h.buttons() == ["Save", "Back"]
    run(h.press("Save"))
    assert reg.config.signup_channel_id == 50 and "✅ signup channel → #raids" in h.text and len(bot.ops.lines) == 1
    run(FLOWS["channel_set"](h.command(), reg, kind="signup", clear=True))
    assert "Sheets will have nowhere to post" in h.text and h.buttons() == ["Clear it", "Back"]
    bot.add(51, "mute", view_channel=True)
    run(FLOWS["channel_set"](h.command(), reg, kind="ops", channel=bot.chans[51]))
    assert "❌ I can't post in #mute: missing Send Messages, Embed Links." in h.text and reg.config.ops_channel_id is None


def test_news_channel_hands_the_owner_the_webhook(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    ch = bot.add(80, "news")
    h = owner_harness(reg, bot)
    run(FLOWS["channel_set"](h.command(), reg, kind="news", channel=ch))
    run(h.press("Save"))
    assert "Webhook for your news source: ||https://discord.com/api/webhooks/1/oibotnews||" in h.text
    assert "webhooks/" not in str(bot.ops.lines), "the URL never reaches an ops line"
    noperm = bot.add(81, "news2", **{k: v for k, v in ALL.items() if k != "manage_webhooks"})
    reg.config.news_channel_id = 81
    assert "give the bot Manage Webhooks in #news2" in run(bot.news_webhook(reg))["note"]
    assert noperm.hooks == []


def test_channels_hub_drafts_reviews_and_saves(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    bot.add(50, "raids")
    bot.add(51, "officers")
    h = owner_harness(reg, bot)
    run(FLOWS["channels"](h.command(), reg))
    assert "**Signups** · — not set —" in h.text and "Review 0 changes" in h.buttons()
    run(h.pick("Change which?", "signup"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Signups channel", bot.chans[50]))
    run(h.pick("Change which?", "roster"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Roster (officers) channel", bot.chans[51]))
    assert "→ ✏️ **<#50>**" in h.text and reg.config.signup_channel_id is None, "a draft: nothing written"
    run(h.press("Review 2 changes"))
    assert "Signup channel" in h.text and "⚠ New sheets post in #raids." in h.text and h.buttons() == ["Save", "Back to edits", "Cancel"]
    run(h.press("Save"))
    assert reg.config.signup_channel_id == 50 and reg.config.roster_channel_id == 51
    assert "✅" in h.text and "2 saved" in h.text
    assert [lvl for _k, lvl, _t in bot.ops.lines] == ["info", "info"], "one line per channel, written by set_channel"


def test_channels_hub_keeps_a_refused_channel_drafted(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    bot.add(50, "raids")
    bot.add(52, "mute", view_channel=True)
    h = owner_harness(reg, bot)
    run(FLOWS["channels"](h.command(), reg))
    run(h.pick("Change which?", "ops"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Ops (officers) channel", bot.chans[52]))
    assert "⚠ I can't post in #mute" in h.text
    run(h.pick("Change which?", "signup"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Signups channel", bot.chans[50]))
    run(h.press("Review 2 changes"))
    run(h.press("Save"))
    assert reg.config.signup_channel_id == 50 and reg.config.ops_channel_id is None
    assert "⛔" in h.text and "1 saved, 1 refused" in h.text and "Back to edits" in h.buttons()
    run(h.press("Back to edits"))
    assert "→ ✏️ **<#52>**" in h.text and "<#50> →" not in h.text


def test_officers_hub_role_picker_review_and_save(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    reg.cache_roles(bot.guild)
    reg.config.officer_roles = ["Old Officers"]
    h = owner_harness(reg, bot)
    run(FLOWS["officers"](h.command(), reg))
    assert "From an older config, by name" in h.text and "Map Old Officers…" in h.buttons()
    run(pick_widget(h, discord.ui.RoleSelect, "Officer roles (pick every role that counts)", bot.guild.get_role(55)))
    assert "belongs to a bot or an integration" in h.text, "managed roles are refused"
    run(pick_widget(h, discord.ui.RoleSelect, "Officer roles (pick every role that counts)"))
    run(h.press("Map Old Officers…"))
    run(pick_widget(h, discord.ui.RoleSelect, "The role for Old Officers", bot.guild.get_role(66)))
    run(h.press("Review"))
    assert "Officer roles" in h.text and "No officer roles now" not in h.text
    run(h.press("Save"))
    assert reg.config.officer_role_ids == [66] and reg.config.officer_roles == []


def test_officer_role_command_says_the_empty_list_consequence(reg):
    op = ConfigOp(op="role_remove", value=str(OFFICER_ROLE))
    assert configops.op_consequences(reg, op) == ["No officer roles now: only the server owner and Manage Server count."]
    bot = SetupBot(reg)
    with pytest.raises(RegistryError, match="@everyone can't be an officer role"):
        run(configops.apply_async(reg, ConfigOp(op="role_add", value=str(bot.guild.id)), "Owner", True, bot=bot, by_id=OWNER, via="discord"))


# ---------------------------------------------------------------- the web (TestClient)

class WebBot(SetupBot):
    """SetupBot + what create_app reads: the members with roles, the decide path, the raid store."""

    def __init__(self, reg, rs):
        super().__init__(reg)
        self.raids = type("S", (), {"store": lambda _s, r: rs})()
        self.started_at = 0.0
        self.ctx = None
        self.people = {OFFICER_UID: types.SimpleNamespace(id=OFFICER_UID, display_name="Offi", roles=[]), MEMBER_UID: types.SimpleNamespace(id=MEMBER_UID, display_name="Mem", roles=[])}
        self.flaky: set[int] = set()

    async def cached_member(self, guild, uid, max_age=300):
        if uid in self.flaky:
            raise RuntimeError("discord 503")
        m = self.people.get(int(uid))
        if m is None:
            self.__dict__.setdefault("_member_gone", set()).add((guild.id, int(uid)))
        return m

    def officiates(self, member, guild):
        return member.id == OFFICER_UID or self.reg.is_owner_id(member.id)

    async def decide_application(self, reg, discord_id, accept, by, note=None, by_id=None, via="discord"):
        return await discord_registry.settle_application(self, reg, discord_id, accept, by, note, by_id=by_id, via=via)

    async def fetch_user(self, uid):
        async def send(text):
            return None
        return types.SimpleNamespace(send=send)


@pytest.fixture
def web_bot(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    return WebBot(reg, rs)


@pytest.fixture
def wc(web_bot):
    return TestClient(web.create_app(web_bot), follow_redirects=False)


def as_user(client, uid):
    client.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


def test_channels_route_with_the_missing_badge(wc, web_bot, reg):
    web_bot.add(50, "raids")
    web_bot.add(51, "mute", view_channel=True)
    reg.config.signup_channel_id, reg.config.ops_channel_id, reg.config.roster_channel_id = 50, 51, 99
    h = as_user(wc, OFFICER_UID)
    d = wc.get("/api/admin/channels", headers=h).json()
    rows = {r["kind"]: r for r in d["channels"]}
    assert d["ready"] and not d["owner"] and rows["signup"]["name"] == "raids" and rows["signup"]["problem"] is None
    assert rows["ops"]["missing"] == ["Send Messages", "Embed Links"] and rows["roster"]["problem"] == "deleted, or the bot can't see it"
    assert wc.post("/api/admin/channels", json={"channels": {"signup": None}}, headers=h).status_code == 403, "owner only"
    web_bot._ready = False
    d = wc.get("/api/admin/channels", headers=h).json()
    assert not d["ready"] and d["guild_channels"] == [] and "Discord is unreachable" in d["unreachable"]
    assert all(r["problem"] is None for r in d["channels"]), "never 'not found' while Discord is unreachable"


def test_channels_translator_applies_through_the_core(wc, web_bot, reg, monkeypatch):
    web_bot.add(50, "raids")
    web_bot.people[OWNER] = types.SimpleNamespace(id=OWNER, display_name="Own", roles=[])
    h = as_user(wc, OWNER)
    first = wc.post("/api/admin/channels", json={"channels": {"signup": "50"}}, headers=h)
    assert first.status_code == 200 and first.json()["review_required"] and reg.config.signup_channel_id is None, "D51: a channel is reviewed first"
    assert web_bot.ops.lines == []
    r = post_confirmed(wc, "/api/admin/channels", {"channels": {"signup": "50"}}, headers=h)
    assert r.status_code == 200, r.text
    assert reg.config.signup_channel_id == 50 and r.json()["message"] == "signup channel → #raids"
    assert web_bot.ops.lines == [(GUILD, "info", "[web] Own: signup channel → #raids")]
    assert wc.post("/api/admin/channels", json={"channels": {"bogus": "50"}}, headers=h).status_code == 400
    web_bot.add(51, "mute", view_channel=True)
    r = post_confirmed(wc, "/api/admin/channels", {"channels": {"ops": "51"}}, headers=h)
    assert r.status_code == 400 and "I can't post in #mute" in r.json()["error"]


def test_structured_save_of_a_channel_reviews_with_the_consequences(wc, web_bot, reg):
    web_bot.add(50, "raids")
    web_bot.people[OWNER] = types.SimpleNamespace(id=OWNER, display_name="Own", roles=[])
    perms.CLIENT = web_bot
    try:
        h = as_user(wc, OWNER)
        r = wc.post("/api/ops/change", json={"changes": [{"kind": "settings", "fields": {"channel:registration": "50"}}], "apply": True}, headers=h).json()
        assert r["needs_review"] and any("Posts and pins the registration card in #raids" in c for c in r["described"]["consequences"])
        assert reg.config.registration_channel_id is None
        done = wc.post("/api/ops/change", json={"token": r["token"], "apply": True}, headers=h).json()
        assert done["applied"] and reg.config.registration_channel_id == 50
    finally:
        perms.CLIENT = None


def test_news_webhook_route_is_owner_only_and_never_audited_with_the_url(wc, web_bot, reg):
    web_bot.add(80, "news")
    reg.config.news_channel_id = 80
    assert wc.post("/api/admin/news-webhook", json={}, headers=as_user(wc, OFFICER_UID)).status_code == 403
    web_bot.people[OWNER] = types.SimpleNamespace(id=OWNER, display_name="Own", roles=[])
    d = wc.post("/api/admin/news-webhook", json={}, headers=as_user(wc, OWNER)).json()
    assert d["url"].startswith("https://discord.com/api/webhooks/") and d["created"]
    assert web_bot.ops.lines == [(GUILD, "info", "[web] Own: created the news webhook")]


def test_setup_route_and_the_host_card(wc, web_bot, reg, monkeypatch):
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(SUPER))
    monkeypatch.setenv("DISCORD_CLIENT_ID", "42")
    web_bot.add(50, "raids")
    reg.config.signup_channel_id = 50
    d = wc.get("/api/admin/setup", headers=as_user(wc, OFFICER_UID)).json()
    rows = {r["key"]: r for r in d["checklist"]}
    assert rows["channel:signup"]["text"] == "#raids", "mentions rendered for the web" and d["host"] is None
    assert d["invite"]["state"] == "none" and d["errors"]["words"] == "the owner" and d["errors"]["suppressed"] == 0
    web_bot.guilds = [web_bot.guild, FakeGuild(77, "Stray")]
    web_bot.people[SUPER] = types.SimpleNamespace(id=SUPER, display_name="Host", roles=[])
    d = wc.get("/api/admin/setup", headers=as_user(wc, SUPER)).json()
    assert d["host"]["invite_link"].startswith("https://discord.com/oauth2/authorize?client_id=42")
    assert [g["name"] for g in d["host"]["unclaimed"]] == ["Stray"] and d["host"]["detached"] == []


def test_left_and_remove_record_routes(wc, web_bot, reg):
    m = real_member(reg)
    reg.member_left(m.discord_id)
    h = as_user(wc, OFFICER_UID)
    rows = wc.get("/api/admin/left", headers=h).json()["left"]
    assert [r["uid"] for r in rows] == [str(m.discord_id)] and "record goes" in rows[0]["consequence"]
    assert wc.post("/api/admin/member/remove", json={"uid": str(m.discord_id)}, headers=as_user(wc, MEMBER_UID)).status_code == 403
    r = wc.post("/api/admin/member/remove", json={"uid": str(m.discord_id)}, headers=as_user(wc, OFFICER_UID))
    assert r.status_code == 200 and m.discord_id not in reg.members
    assert web_bot.ops.lines[-1] == (GUILD, "info", f"[web] Offi: {m.display_name}'s record removed")


def test_applications_routes(wc, web_bot, reg):
    a = apply_one(reg, uid=8100)
    h = as_user(wc, OFFICER_UID)
    d = wc.get("/api/applications", headers=h).json()
    o = d["open"][0]
    assert d["note_max"] == 400 and o["prior"] is None and o["consequence"]["accept"][0].startswith("User8100 is registered as **Kessa Dawnfire**")
    assert wc.post("/api/admin/application/decide", json={"uid": "8100", "accept": False, "note": "x" * 401}, headers=h).json()["error"].startswith("The note is limited")
    r = wc.post("/api/admin/application/decide", json={"uid": "8100", "accept": False, "note": "full"}, headers=h)
    assert r.status_code == 200 and r.json()["application"]["status"] == "declined"
    assert web_bot.ops.lines == [(GUILD, "info", "[web] Offi: Declined User8100 (Kessa Dawnfire) — full")], "one line, from the verb"
    r = wc.post("/api/admin/application/decide", json={"uid": "8100", "accept": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"].startswith("Already declined by Offi")
    apply_one(reg, uid=8100, name="Kessia")
    assert wc.get("/api/applications", headers=h).json()["open"][0]["prior"].endswith("by Offi — full")


def test_me_carries_the_own_application(wc, web_bot, reg):
    web_bot.people[8200] = types.SimpleNamespace(id=8200, display_name="New", roles=[])
    apply_one(reg, uid=8200)
    d = wc.get("/api/me", headers=as_user(wc, 8200)).json()
    assert d["application"]["state"] == "open" and "waiting for an officer" in d["application"]["text"]


@pytest.mark.parametrize("accept", [True, False])
def test_the_applicant_never_reads_the_officers_name(wc, web_bot, reg, accept):
    """G3 review: the applicant's own view (GET /api/me.application, /me view, the Me notice) named the officer who
    decided. They read the decision and its date; officers see who on the Applications page."""
    web_bot.people[8300] = types.SimpleNamespace(id=8300, display_name="New", roles=[])
    apply_one(reg, uid=8300)
    reg.decide(8300, accept, "Officerella", "see you soon")
    st = reg.application_state(8300)
    assert "Officerella" not in json.dumps(st) and st["when"] and "by" not in st
    d = wc.get("/api/me", headers=as_user(wc, 8300)).json()
    assert d["application"]["state"] == ("accepted" if accept else "declined") and "Officerella" not in json.dumps(d["application"])
    assert bool(reg.notices(8300)) is accept and not any("Officerella" in json.dumps(n) for n in reg.notices(8300)), "nor on the Me page's notice"
    rows = wc.get("/api/applications", headers=as_user(wc, OFFICER_UID)).json()["decided"]
    assert any(r["decided_by"] == "Officerella" for r in rows), "officers still see who decided"


def test_meta_discord_ready_and_unsure_with_one_guild(wc, web_bot, reg):
    h = as_user(wc, MEMBER_UID)
    assert wc.get("/api/meta", headers=h).json()["discord"] == {"ready": True, "stale": False}
    web_bot._ready = False
    assert wc.get("/api/meta", headers=h).json()["discord"]["ready"] is False
    web_bot._ready = True
    web_bot.flaky.add(OUTSIDER_UID)
    assert wc.get("/api/meta", headers=as_user(wc, OUTSIDER_UID)).status_code == 503, "§2.9: unknown is never 'not a member', one guild too"
    web_bot.flaky.discard(OUTSIDER_UID)
    assert wc.get("/api/meta", headers=as_user(wc, OUTSIDER_UID)).status_code == 403


def test_stale_membership_keeps_reads_and_refuses_officer_writes(wc, web_bot, reg):
    web_bot.__dict__["_member_last_yes"] = {(1, OFFICER_UID): (time.time(), web_bot.people[OFFICER_UID])}
    web_bot.flaky.add(OFFICER_UID)
    h = as_user(wc, OFFICER_UID)
    meta = wc.get("/api/meta", headers=h)
    assert meta.status_code == 200 and meta.json()["discord"]["stale"] is True
    r = wc.post("/api/ops/change", json={"changes": [{"kind": "settings", "fields": {"about": "x"}}], "apply": True}, headers=h).json()
    assert not r.get("applied") and r["refused"], "an officer write re-resolves live and Discord can't confirm it"


def test_guilds_route_shows_parked_guilds_to_the_host_only(duo, monkeypatch, rs):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(SUPER))
    bot = WebBot(duo.a, rs)
    bot.registries = duo.guilds
    bot.people[SUPER] = types.SimpleNamespace(id=SUPER, display_name="Host", roles=[])
    duo.guilds.detach(duo.b.key)
    c = TestClient(web.create_app(bot))
    d = c.get("/api/guilds", headers={"Authorization": f"Bearer {TOKEN}"}).json()["guilds"]
    assert [g["key"] for g in d if not g.get("detached")] == [duo.a.key] and [g["key"] for g in d if g.get("detached")] == [duo.b.key]
    assert c.get("/api/guilds", headers=as_user(c, SUPER)).json()["detached"][0]["key"] == duo.b.key
    assert "detached" not in c.get("/api/guilds", headers=as_user(c, OFFICER_UID)).json()


def test_ops_route_carries_the_suppressed_count(wc, web_bot, reg):
    web_bot.ops = ops_mod.Ops(AlertClient())
    reg.config.ops_channel_id = None
    run(web_bot.ops.emit(reg.config, "error", "x 1"))
    run(web_bot.ops.emit(reg.config, "error", "x 2"))
    d = wc.get("/api/ops", headers=as_user(wc, OFFICER_UID)).json()
    assert d["suppressed"] == 1 and d["ops_failing"] == 0


# ---------------------------------------------------------------- MCP

def test_mcp_tool_shapes(monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    from oibot_gm import mcp_server as ms

    calls = []

    class A:
        parked = [{"key": "gone", "name": "Gone", "detached": True, "detached_at": "2026-10-20"}]

        def guilds(self, fresh=False):
            return [{"key": "demo", "name": "Demo"}]

        def default_key(self):
            return "demo"

        def get(self, path, **kw):
            calls.append(("GET", path))
            return {"left": [{"uid": "5", "name": "Ana"}]} if path == "/api/admin/left" else {}

        def post(self, path, body=None, actor=False):
            calls.append(("POST", path, body))
            return {"message": "ok", "applied": ["x"], "refused": []}
    monkeypatch.setattr(ms, "api", lambda guild=None: A())
    assert ms.list_guilds()["detached"] == [{"key": "gone", "name": "Gone", "detached_at": "2026-10-20"}]
    with pytest.raises(ToolError, match="not both, not neither"):
        ms.set_channel("signup")
    ms.set_channel("signup", clear=True)
    assert calls[-1][2]["changes"] == [{"kind": "settings", "fields": {"channel:signup": None}}]
    ms.set_channel("signup", "#raids")
    assert calls[-1][2]["changes"][0]["fields"] == {"channel:signup": "#raids"}
    assert ms.remove_record("ana") == "ok" and calls[-1] == ("POST", "/api/admin/member/remove", {"uid": "5"})
    with pytest.raises(ToolError, match="isn't one of the people who left"):
        ms.remove_record("Bo")


def test_mcp_client_never_targets_a_parked_guild():
    import httpx

    from oibot_gm import mcp_server as ms

    def handler(request):
        return httpx.Response(200, json={"guilds": [{"key": "demo", "name": "Demo"}, {"key": "gone", "name": "Gone", "detached": True}]})
    a = ms.Api(url="http://bot", token="t", transport=httpx.MockTransport(handler))
    assert [g["key"] for g in a.guilds()] == ["demo"] and a.default_key() == "demo" and [g["key"] for g in a.parked] == ["gone"]


def test_a_batch_that_swaps_officer_roles_never_says_none_left(reg):
    """Regression: op_consequences reads each op alone, so remove A + add B said 'No officer roles now' (the officers hub,
    the web's MultiSelect). Only a batch that leaves none says it."""
    reg.role_names = {OFFICER_ROLE: "Officer", 66: "Member"}
    swap = configops.describe_ops(reg, configops.ops_from_fields(reg, "settings", None, {"officer_roles": ["66"]}))
    assert configops.NO_OFFICER_ROLES not in swap.consequences and all(configops.NO_OFFICER_ROLES not in ln.consequences for ln in swap.lines)
    none = configops.describe_ops(reg, configops.ops_from_fields(reg, "settings", None, {"officer_roles": []}))
    assert none.consequences == [configops.NO_OFFICER_ROLES] and none.review


# ---------------------------------------------------------------- review fixes (G3): stale writes, loot-channel, invite, left_at, host facts

def test_hourly_check_warns_for_an_optional_channel_and_alerts_for_a_required_one(reg):
    bot = SetupBot(reg)
    bot.add(60, "officers", view_channel=True)
    reg.config.roster_channel_id = 60
    run(bot.check_channel_perms(reg))
    assert [lvl for _k, lvl, _t in bot.ops.lines] == ["warn"], "the roster channel: an ops warn, no DM"
    bot.add(61, "ops", view_channel=True)
    reg.config.ops_channel_id = 61
    run(bot.check_channel_perms(reg))
    assert bot.ops.lines[-1][1] == "error" and "#ops" in bot.ops.lines[-1][2] and "#officers" not in bot.ops.lines[-1][2], "only what is new"


def test_stale_officer_reads_but_no_officer_route_writes(wc, web_bot, reg):
    """§2.9 regression: a lookup Discord can't answer falls back to the last-known "yes" — reads go on, but EVERY officer
    write refuses (503, nothing written), not only the routes that call live_officer."""
    apply_one(reg, uid=8300)
    gone = real_member(reg, uid=9300)
    reg.member_left(gone.discord_id)
    web_bot.__dict__["_member_last_yes"] = {(1, OFFICER_UID): (time.time(), web_bot.people[OFFICER_UID])}
    web_bot.flaky.add(OFFICER_UID)
    h = as_user(wc, OFFICER_UID)
    assert wc.get("/api/applications", headers=h).status_code == 200, "reads go on"
    assert wc.get("/api/admin/left", headers=h).status_code == 200
    r = wc.post("/api/admin/application/decide", json={"uid": "8300", "accept": False, "note": "full"}, headers=h)
    assert r.status_code == 503 and "can't confirm your officer role" in r.json()["error"], r.text
    assert reg.applicants[8300].status == "open", "nothing decided"
    r = wc.post("/api/admin/member/remove", json={"uid": str(gone.discord_id)}, headers=h)
    assert r.status_code == 503 and gone.discord_id in reg.members
    assert web_bot.ops.lines == []
    web_bot.flaky.discard(OFFICER_UID)  # Discord answers again: the officer writes
    assert wc.post("/api/admin/application/decide", json={"uid": "8300", "accept": False, "note": "full"}, headers=h).status_code == 200


def test_stale_member_still_reaches_their_own_routes(wc, web_bot, reg):
    """The refusal is officer rights only: a member's own reads and the owner are untouched."""
    web_bot.__dict__["_member_last_yes"] = {(1, MEMBER_UID): (time.time(), web_bot.people[MEMBER_UID])}
    web_bot.flaky.add(MEMBER_UID)
    assert wc.get("/api/me", headers=as_user(wc, MEMBER_UID)).status_code == 200


def test_channels_hub_refuses_a_save_on_a_stale_settings_rev(reg):
    """Regression: the hub's Save used to write a channel although the settings changed after Review (set_channel ignored
    the rev). Now set_channel checks it before posting anything; the line is refused and stays drafted."""
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    bot.add(50, "raids")
    h = owner_harness(reg, bot)
    run(FLOWS["channels"](h.command(), reg))
    run(h.pick("Change which?", "signup"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Signups channel", bot.chans[50]))
    run(h.press("Review 1 change"))
    reg.set_error_dms([OWNER, 4242], "someone else")  # another settings write lands meanwhile
    run(h.press("Save"))
    assert reg.config.signup_channel_id is None and "1 saved" not in h.text and "changed since you looked" in h.text
    assert bot.chans[50].sent == [] and bot.ops.lines == []
    run(h.press("Back to edits"))
    assert "→ ✏️ **<#50>**" in h.text, "the refused channel stays drafted"
    run(h.press("Review 1 change"))
    run(h.press("Save"))
    assert reg.config.signup_channel_id == 50, "a fresh look saves"


def test_apply_ops_channel_write_checks_the_rev_on_every_surface(reg):
    bot = SetupBot(reg)
    bot.add(50, "raids")
    bot.add(51, "officers")
    old = reg.config_rev("settings")
    ops = configops.ops_from_fields(reg, "settings", None, {"channel:signup": "50", "channel:roster": "51"})
    res = run(configops.apply_ops(reg, ops, by="Owner", by_id=OWNER, bot=bot, via="discord", revs={"config:settings": old}))
    assert reg.config.signup_channel_id == 50 and reg.config.roster_channel_id == 51 and not res["refused"], "its own writes carry the rev on"
    reg.config.timezone = "Europe/Paris"
    reg.save_config("tz")
    ops = configops.ops_from_fields(reg, "settings", None, {"channel:signup": None})
    res = run(configops.apply_ops(reg, ops, by="Owner", by_id=OWNER, bot=bot, via="discord", revs={"config:settings": old}))
    assert res["changed"] == ["settings"] and reg.config.signup_channel_id == 50, res
    with pytest.raises(RegistryError, match="changed since you looked"):
        run(bot.set_channel(reg, "signup", None, "Owner", rev=old))
    from oibot_gm.registry import ConfigChanged

    with pytest.raises(ConfigChanged):  # plain text's Apply (ConfigConfirmView) passes op_rev through apply_async
        run(configops.apply_async(reg, ConfigOp(op="set", path="signup_channel", value="none"), "Owner", True, bot=bot, by_id=OWNER, rev=old))
    assert reg.config.signup_channel_id == 50


def test_channel_confirm_refuses_a_stale_save(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    ch = bot.add(50, "raids")
    h = owner_harness(reg, bot)
    run(FLOWS["channel_set"](h.command(), reg, kind="signup", channel=ch))
    reg.set_error_dms([], "someone else")
    run(h.press("Save"))
    assert "❌" in h.text and "changed since you looked" in h.text and reg.config.signup_channel_id is None


def test_loot_channel_is_the_ninth_kind_through_the_one_confirm(reg):
    from oibot_gm.wizard_flows import FLOWS

    bot = SetupBot(reg)
    ch = bot.add(70, "loot")
    h = owner_harness(reg, bot)
    run(FLOWS["channel_set"](h.command(), reg, kind="loot"))
    assert "Pick a channel, tick clear, or set log" in h.text
    run(FLOWS["channel_set"](h.command(), reg, kind="loot", channel=ch))
    assert "Use <#70> for this?" in h.text and h.buttons() == ["Save", "Back"] and reg.config.loot_channel_id is None, "a confirm first"
    run(h.press("Save"))
    assert reg.config.loot_channel_id == 70 and "✅ loot channel → #loot" in h.text and len(bot.ops.lines) == 1
    was = bool(reg.config.loot_log)
    run(FLOWS["channel_set"](h.command(), reg, kind="loot", log=not was))
    assert "Change the public loot log?" in h.text and bool(reg.config.loot_log) == was
    run(h.press("Save"))
    assert bool(reg.config.loot_log) == (not was) and reg.config.loot_channel_id == 70, "the log alone: the channel stays"
    run(FLOWS["channel_set"](h.command(), reg, kind="loot", log=not was))
    assert "Nothing changes." in h.text
    run(FLOWS["channel_set"](h.command(), reg, kind="loot", clear=True, log=was))
    assert "Stop using a channel for this?" in h.text and h.buttons() == ["Clear it", "Back"]
    run(h.press("Clear it"))
    assert reg.config.loot_channel_id is None and bool(reg.config.loot_log) == was
    src = (SRC / "discord_registry.py").read_text().split("def cfg_loot_channel", 1)[1].split("\n    @", 1)[0]
    assert "set_channel(" not in src and 'FLOWS["channel_set"]' in src and "setattr(reg.config" not in src


def test_invite_create_is_the_last_op_and_replaces_a_typed_link(reg):
    ops = configops.ops_from_fields(reg, "recruitment", None, {"invite_create": "55", "invite_url": "https://discord.gg/typed", "note": "x"})
    assert [(o.field, o.value) for o in ops] == [("note", "x"), ("invite_create", "<#55>")]
    ops = configops.ops_from_fields(reg, "recruitment", None, {"invite_url": "https://discord.gg/typed"})
    assert [o.field for o in ops] == ["invite"]


def _left_bot(reg):
    from oibot_gm.discord_bot import OibotGM

    bot = SetupBot(reg)
    bot.cached_member = types.MethodType(OibotGM.cached_member, bot)
    return bot


async def _settled(bot):
    await asyncio.gather(*list(bot.__dict__.get("_left_tasks", ())))


def test_discords_own_404_marks_left_and_a_sighting_clears_it(reg):
    """Regression: on_member_remove needs the members intent the bot doesn't request, so left_at was never set. Now
    cached_member's 404 for a registered member marks it; seeing them again clears it; a lookup Discord didn't answer
    changes nothing; test puppets are never marked."""
    bot = _left_bot(reg)
    m = real_member(reg)

    async def look(uid, max_age=300):
        out = await bot.cached_member(bot.guild, uid, max_age=max_age)
        await _settled(bot)
        return out

    assert run(look(m.discord_id)) is None and reg.members[m.discord_id].left_at
    bot.guild.members[m.discord_id] = types.SimpleNamespace(id=m.discord_id, display_name=m.display_name, roles=[])
    assert run(look(m.discord_id, max_age=0)) is not None and reg.members[m.discord_id].left_at is None
    bot.guild.members.pop(m.discord_id)

    async def flaky(uid):
        raise RuntimeError("discord 503")

    bot.guild.fetch_member = flaky
    bot.__dict__.pop("_member_cache", None)
    assert run(look(m.discord_id, max_age=0)) is None and reg.members[m.discord_id].left_at is None, "no answer is not a departure"
    t = reg.test_members()[0]
    bot.note_membership(bot.guild.id, t.discord_id, False)
    assert reg.members[t.discord_id].left_at is None


def test_saw_member_clears_left_at(reg):
    from oibot_gm.discord_bot import OibotGM

    bot = SetupBot(reg)
    m = real_member(reg)
    reg.member_left(m.discord_id)
    OibotGM.saw_member(bot, bot.guild.id, types.SimpleNamespace(id=m.discord_id))
    assert reg.members[m.discord_id].left_at is None


def test_hourly_sweep_finds_who_left_without_the_members_intent(reg):
    bot = _left_bot(reg)
    here, gone = real_member(reg, uid=9401, name="Stays"), real_member(reg, uid=9402, name="Gone")
    bot.guild.members[here.discord_id] = types.SimpleNamespace(id=here.discord_id, display_name="Stays", roles=[])

    async def sweep(limit):
        n = await bot.sweep_left(reg, limit=limit)
        await _settled(bot)
        return n

    real = [x for x in reg.members.values() if not x.test]
    assert run(sweep(limit=500)) == len(real)
    assert reg.members[gone.discord_id].left_at and reg.members[here.discord_id].left_at is None
    assert all(not x.left_at for x in reg.members.values() if x.test), "puppets are never looked up"
    assert run(sweep(limit=500)) == len([x for x in reg.members.values() if not x.test and not x.left_at]), "who left is not looked up again"
    assert run(sweep(limit=1)) == 1


def test_config_show_has_the_host_section_for_the_owner_only(reg, monkeypatch):
    """One config_summary (sections; G1b's shape) carries G3's host facts — who gets the error alerts, the superusers,
    MCP local-only through web.app.mcp_local_only (the one reader of OIBOT_MCP_LOCAL_ONLY), a parked guild — and an
    officer reads only that the Host section is the owner's (merge fix: two config_summary signatures)."""
    from oibot_gm.web import app as web_app

    monkeypatch.setenv("OIBOT_SUPERUSERS", str(SUPER))
    monkeypatch.setenv("OIBOT_MCP_TOKEN", "x" * 64)
    owner = configops.config_summary(reg, None, "owner")
    host = next(x for x in owner if x.key == "host").lines
    text = "\n".join(host)
    assert "Error alerts: the owner" in text and f"<@{SUPER}>" in text and "works only on the host" in text
    monkeypatch.setattr(web_app, "mcp_local_only", lambda: False)
    assert "also works through the tunnel" in "\n".join(next(x for x in configops.config_summary(reg, None, "owner") if x.key == "host").lines)
    reg.config.detached_at = "2026-10-01T19:00:00+00:00"
    assert any(ln.startswith("Parked since") for ln in next(x for x in configops.config_summary(reg, None, "owner") if x.key == "host").lines)
    officer = configops.config_summary(reg)
    assert next(x for x in officer if x.key == "host").lines == ["Host facts: owner only."]
    assert f"<@{SUPER}>" not in configops.summary_text(officer)
    assert not hasattr(configops, "host_summary"), "one summary, one Host section"


def test_setup_route_lets_the_owner_change_the_error_alerts(wc, web_bot, reg):
    d = wc.get("/api/admin/setup", headers=as_user(wc, OFFICER_UID)).json()
    assert d["errors"]["ids"] is None and d["errors"]["choices"] == [] and d["rev"] == reg.config_rev("settings")
    web_bot.people[OWNER] = types.SimpleNamespace(id=OWNER, display_name="Own", roles=[])
    h = as_user(wc, OWNER)
    d = wc.get("/api/admin/setup", headers=h).json()
    m = real_member(reg, uid=9500, name="Alerty")
    d = wc.get("/api/admin/setup", headers=h).json()
    assert {"id": str(m.discord_id), "name": m.display_name} in d["errors"]["choices"]
    assert all(c["id"] not in {str(t.discord_id) for t in reg.test_members()} for c in d["errors"]["choices"]), "no puppets"
    r = wc.post("/api/ops/change", json={"changes": [{"kind": "settings", "fields": {"error_dm_ids": [str(m.discord_id)]}}], "apply": True,
                                         "revs": {"config:settings": d["rev"]}}, headers=h).json()
    assert r.get("applied"), r
    assert reg.config.error_dm_ids == [m.discord_id]
    assert wc.get("/api/admin/setup", headers=h).json()["errors"]["ids"] == [str(m.discord_id)]
