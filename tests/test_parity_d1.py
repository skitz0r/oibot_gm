"""Stage C D1 fixes (out/parity/stageC_report.md H1, M1–M8): who can read a reply decides how much it carries; a
member can't roster themselves on a locked run by chat; a policy reading goes live only against the text it read;
Apply is single-flight and keeps its board revs honest; lock_run and the web autofill share the board lock; MCP
plain_change applies exactly what it previewed; the raid_set Apply line reads in 12-hour words; team_member is gone.
No Discord, no LLM."""
from __future__ import annotations

import asyncio
import contextlib
import types
from datetime import timedelta

import discord
import pytest
from conftest import OFFICER_ROLE, OWNER

from oibot_gm import configops, discord_policy, help as help_mod
from oibot_gm.configops import ConfigRequest
from oibot_gm.discord_help import HelpMixin, officer_only_channel

OPS_CH, GENERAL_CH, ANALYTICS_CH, ROSTER_CH, APPS_CH = 910_001, 910_002, 910_003, 910_004, 910_005


# ---------------------------------------------------------------- H1: the reply's readers decide the snapshot

@pytest.fixture
def secret(reg):
    """Another member's absence with a reason (officer-only) inside the next week."""
    m = reg.test_members()[3]
    reg.add_absence(m.discord_id, (reg.now_local().date() + timedelta(days=2)).isoformat(), None, "hospital appointment", "t")
    return m


def test_public_officer_snapshot_is_the_member_view(reg, rs, secret):
    q = "who's away next week and why?"
    private = help_mod.guild_state(reg, rs, OWNER, True, q)
    assert "hospital appointment" in private and "officer index:" in private and "### Absences" in private
    public = help_mod.guild_state(reg, rs, OWNER, True, q, public=True)
    for leak in ("hospital appointment", "officer index:", "### Absences", "(officers"):
        assert leak not in public, leak
    assert "## Asker: <@%d> (officer)" % OWNER in public and "## Reply audience: a channel members can read" in public
    # a member's snapshot is unchanged by the flag
    uid = reg.test_members()[0].discord_id
    assert help_mod.guild_state(reg, rs, uid, False, q, public=True) == help_mod.guild_state(reg, rs, uid, False, q)


class HelpBot(HelpMixin):
    def __init__(self, reg, rs):
        self.ctx = types.SimpleNamespace(provider=types.SimpleNamespace(summary=lambda: "LLM spend $0"))
        self.tree = None
        self.raids = types.SimpleNamespace(store=lambda r: rs)
        self.ops = types.SimpleNamespace(recent=[("9:00 PM", "info", "ops secret line")])


def test_help_answer_in_public_uses_the_member_view_and_says_where_the_officer_view_is(reg, rs, secret, monkeypatch):
    reg.config.ops_channel_id = OPS_CH
    seen = []

    def fake_answer(provider, tree, reg_, rs_, uid, officer, question, extras=None, public=False):
        seen.append(help_mod.guild_state(reg_, rs_, uid, officer, question, extras, public=public) + f"\nextras ops: {extras.ops if extras else None}")
        return help_mod.HelpAnswer(answer="Nobody you need to know about.")

    monkeypatch.setattr(help_mod, "answer", fake_answer)
    bot = HelpBot(reg, rs)
    q = "who's away next week and why? any ops errors?"
    text = asyncio.run(bot.help_answer(reg, OWNER, True, q, public=True))
    assert "hospital appointment" not in seen[0] and "ops secret line" not in seen[0]
    assert text.startswith("Nobody you need to know about.") and f"officers can ask in <#{OPS_CH}> or by DM for the officer view" in text
    text = asyncio.run(bot.help_answer(reg, OWNER, True, q))
    assert "hospital appointment" in seen[1] and "ops secret line" in seen[1] and "officer view" not in text


# officer_only_channel / reply_private on stand-in Discord objects

class Perms:
    def __init__(self, view=False, admin=False, manage=False):
        self.view_channel, self.administrator, self.manage_guild = view, admin, manage


class Role:
    def __init__(self, rid, name, *, admin=False, manage=False):
        self.id, self.name, self.permissions = rid, name, Perms(admin=admin, manage=manage)


class Chan:
    def __init__(self, cid, guild, visible_to=(), parent=None, overwrites=None):
        self.id, self.guild, self.parent, self.parent_id = cid, guild, parent, getattr(parent, "id", None)
        self.visible_to, self.overwrites = set(visible_to), overwrites or {}

    def permissions_for(self, role):
        return Perms(view=role.id in self.visible_to)


def guild_with_roles():
    everyone, raider, officer, admin = Role(1, "@everyone"), Role(2, "Raider"), Role(OFFICER_ROLE, "Officer"), Role(4, "Admin", admin=True)
    return types.SimpleNamespace(roles=[everyone, raider, officer, admin]), everyone, raider, officer


def test_reply_private_follows_the_channels_readers(reg):
    cfg = reg.config
    cfg.ops_channel_id, cfg.roster_channel_id, cfg.applications_channel_id, cfg.analytics_channel_id = OPS_CH, ROSTER_CH, APPS_CH, ANALYTICS_CH
    guild, everyone, raider, officer = guild_with_roles()
    bot = HelpBot(reg, None)
    assert bot.reply_private(reg, None), "a DM"
    for cid in (OPS_CH, ROSTER_CH, APPS_CH):
        assert bot.reply_private(reg, Chan(cid, guild, visible_to={1, 2})), cid
    assert bot.reply_private(reg, Chan(99, guild, visible_to={1}, parent=Chan(OPS_CH, guild))), "a thread in #ops"
    assert not bot.reply_private(reg, Chan(GENERAL_CH, guild, visible_to={1, 2, OFFICER_ROLE}))
    assert not bot.reply_private(reg, Chan(ANALYTICS_CH, guild, visible_to={2, OFFICER_ROLE})), "raiders can read analytics"
    assert bot.reply_private(reg, Chan(ANALYTICS_CH, guild, visible_to={OFFICER_ROLE})), "analytics made officer-only"
    assert not officer_only_channel(reg, types.SimpleNamespace(guild=None)), "anything unreadable counts as public"


# handle_change on a real Message type: the parser context and the to_help snapshot follow the channel

class Msg(discord.Message):
    def __init__(self, **kw):  # a stand-in: only what handle_change reads
        for k, v in kw.items():
            setattr(self, k, v)


class CapturingProvider:
    def __init__(self, reply):
        self.seen, self.reply = [], reply

    def complete(self, workload, system, user, schema):
        self.seen.append(user)
        return self.reply


def test_handle_change_in_a_public_channel_parses_and_answers_with_the_member_view(reg):
    reg.apply(7_070_000_001, "nyxie", "Nyx", "Priest", "Holy", None, None, None, None, surname="Starfall")
    replies, helped = [], []

    async def plain_identity(reg_, user):
        return True, []

    async def help_answer(reg_, uid, officer, text, may_act=False, public=False):
        helped.append((officer, public))
        return None  # fall through to the parser's own reply

    async def reply(content=None, **kw):
        replies.append(content)

    private = {"yes": False}
    bot = types.SimpleNamespace(plain_identity=plain_identity, help_answer=help_answer, raids=None, reply_private=lambda r, ch: private["yes"])
    prov = CapturingProvider(ConfigRequest(kind="question", reply="Which applicant?", questions=[]))

    def ask():
        msg = Msg(author=types.SimpleNamespace(id=OWNER, display_name="Owner"), guild=object(), channel=object(), reply=reply)
        asyncio.run(discord_policy.handle_change(msg, reg, None, prov, None, "who applied?", bot=bot, channel=GENERAL_CH, to_help=True))

    ask()
    assert "nyxie" not in prov.seen[-1] and "Open applications" not in prov.seen[-1] and "Plain-text permissions" not in prov.seen[-1]
    assert helped[-1] == (True, True)
    private["yes"] = True
    ask()
    assert "nyxie" in prov.seen[-1] and helped[-1] == (True, False)


class Typing:
    @contextlib.asynccontextmanager
    async def typing(self):
        yield


def test_plain_text_mention_by_an_officer_in_a_public_channel_is_answered_publicly(reg):
    from oibot_gm import discord_bot as db

    helped, replies = [], []

    async def help_answer(reg_, uid, officer, text, may_act=False, public=False):
        helped.append((officer, public))
        return "an answer"

    async def plain_identity(reg_, user):
        return True, []

    async def reply(content=None, **kw):
        replies.append(content)

    private = {"yes": False}
    stub = types.SimpleNamespace(ctx=types.SimpleNamespace(provider=object()), policies=types.SimpleNamespace(store=lambda r: None), ops=None,
                                 help_answer=help_answer, plain_identity=plain_identity, reply_private=lambda r, ch: private["yes"] or ch is None)

    def send(guild):
        msg = types.SimpleNamespace(author=types.SimpleNamespace(id=OWNER, display_name="Owner"), channel=Typing(), reply=reply, guild=guild)
        asyncio.run(db.OibotGM.plain_text(stub, msg, reg, "who's away and why?", None if guild is None else GENERAL_CH))

    send(object())
    send(None)  # a DM
    private["yes"] = True
    send(object())  # an officer-only channel
    assert helped == [(True, True), (True, False), (True, False)] and replies == ["an answer"] * 3


# ---------------------------------------------------------------- M1: no self-rostering on a locked run by chat

def test_member_join_after_lock_is_refused_with_the_sheets_words(reg, rs):
    from conftest import join, lock_with_board, open_test_run

    from oibot_gm import raidcycle as rc
    from oibot_gm.configops import ConfigOp

    ms = reg.test_members()
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, ms[:10])
    me = ms[12]

    def check(value, officer=False):
        return configops.authorize(reg, [ConfigOp(op="run_answer", target=ev.key, value=value)], me.discord_id, officer=officer, channel=None, raids=rs)

    assert check("join")[0] and check("bench")[0], "open: a member answers their own sheet"
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:10]])
    for value in ("join", "bench"):
        allowed, refused = check(value)
        assert not allowed and refused == [rc.LOCKED_JOIN], value
    assert check("no thanks")[0], "saying no stays self-service after lock"
    assert check("join", officer=True)[0], "an officer may still seat themselves"
    assert "Can't make it" in rc.LOCKED_JOIN
