"""Listening (design §5.59): the owner's list of channels the bot reads without being tagged (PlainPolicy.listen), the plain
op that sets it (text and structured), its words, and the gate in discord_bot (ListenMixin.listen_message) with a fake
decision service returning fixed probabilities. No network, no Discord, no LLM."""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from oibot_gm import configops
from oibot_gm import discord_bot as db
from oibot_gm.configops import ConfigOp
from oibot_gm.llm import decisions
from oibot_gm.registry import LISTEN_MODES, LISTEN_WORDS, RegistryError

OPS_CH, SIGNUP_CH, GENERAL_CH, QUIET_CH = 910_001, 910_002, 910_003, 910_004
BOT_ID = 777


@pytest.fixture
def g(reg):
    reg.config.ops_channel_id, reg.config.signup_channel_id = OPS_CH, SIGNUP_CH
    reg.save_config("channels (test)")
    me, other = reg.test_members()[:2]
    return types.SimpleNamespace(reg=reg, me=me, other=other)


def listen(reg, **chans):
    reg.set_plain_policy(reg.merge_plain_policy({"set_listen": chans}), "owner")


# ---- the registry: validation, merge, the verbs

def test_constants_and_default_is_off(g):
    assert LISTEN_MODES == ("watch", "reply") and LISTEN_WORDS == {"watch": "Watches", "reply": "Replies"}
    assert g.reg.listen_channels() == {} and g.reg.listen_mode(SIGNUP_CH) is None and g.reg.listen_mode(None) is None


def test_policy_validation(g):
    reg = g.reg
    with pytest.raises(RegistryError, match="watch or reply"):
        reg.set_plain_policy(reg.merge_plain_policy({"set_listen": {str(SIGNUP_CH): "shout"}}), "owner")
    with pytest.raises(RegistryError, match="by channel id"):
        reg.set_plain_policy(reg.merge_plain_policy({"listen": {"dm": "watch"}}), "owner")
    reg.set_plain_policy(reg.merge_plain_policy({"set_channels": {str(QUIET_CH): "ignore"}}), "owner")
    with pytest.raises(RegistryError, match="ignores plain text"):
        reg.set_plain_policy(reg.merge_plain_policy({"set_listen": {str(QUIET_CH): "watch"}}), "owner")
    assert reg.listen_channels() == {}
    line = reg.set_plain_policy(reg.merge_plain_policy({"set_listen": {str(SIGNUP_CH): "reply", str(GENERAL_CH): "watch"}}), "owner")
    assert "listening 2 changed" in line
    assert reg.listen_mode(SIGNUP_CH) == "reply" and reg.listen_mode(str(GENERAL_CH)) == "watch"


def test_merge_whole_map_and_set_listen(g):
    reg = g.reg
    listen(reg, **{str(SIGNUP_CH): "watch", str(GENERAL_CH): "watch"})
    m = reg.merge_plain_policy({"set_listen": {f"<#{SIGNUP_CH}>": "reply", str(GENERAL_CH): None}})
    assert m["listen"] == {str(SIGNUP_CH): "reply"}
    whole = reg.merge_plain_policy({"listen": {str(GENERAL_CH): "reply"}})
    assert whole["listen"] == {str(GENERAL_CH): "reply"}
    assert reg.merge_plain_policy({"dm": "answer"})["listen"] == {str(SIGNUP_CH): "watch", str(GENERAL_CH): "watch"}
    rev = reg.config_rev("plain")
    listen(reg, **{str(GENERAL_CH): "off"})
    assert reg.listen_channels() == {str(SIGNUP_CH): "watch"} and reg.config_rev("plain") != rev


# ---- the plain op: text forms, structured fields, words

@pytest.mark.parametrize("target,value,want", [
    (f"listen <#{SIGNUP_CH}>", "reply", "reply"),
    (f"listening in <#{SIGNUP_CH}>", "watch", "watch"),
    (f"listen in <#{SIGNUP_CH}>", "watches", "watch"),
    (f"listen <#{SIGNUP_CH}>", "off", None),
    (f"listen <#{SIGNUP_CH}>", "stop", None),
    (f"stop listening in <#{SIGNUP_CH}>", "", None),
])
def test_plain_patch_text_forms(g, target, value, want):
    patch, line = configops._plain_patch(g.reg, ConfigOp(op="plain", target=target, value=value))
    assert patch == {"set_listen": {str(SIGNUP_CH): want}}
    assert line.startswith(f"listening in <#{SIGNUP_CH}>: off → {configops.listen_word(want)}")


def test_plain_patch_refuses_an_unknown_mode(g):
    with pytest.raises(RegistryError, match="watch, reply or off"):
        configops._plain_patch(g.reg, ConfigOp(op="plain", target=f"listen <#{SIGNUP_CH}>", value="loud"))


def test_describe_words_and_the_key_note(g, monkeypatch):
    op = ConfigOp(op="plain", target=f"listen <#{SIGNUP_CH}>", value="watch")
    monkeypatch.setattr(decisions, "DECISIONS", None)
    monkeypatch.delenv(decisions.KEY_ENV, raising=False)
    assert configops.describe(g.reg, op) == f"listening in <#{SIGNUP_CH}>: off → watches — the host hasn't set the TypeSafe key, so nothing happens yet"
    monkeypatch.setenv(decisions.KEY_ENV, "x")
    assert configops.describe(g.reg, op) == f"listening in <#{SIGNUP_CH}>: off → watches"
    listen(g.reg, **{str(SIGNUP_CH): "reply"})
    off = ConfigOp(op="plain", target=f"listen <#{SIGNUP_CH}>", value="off")
    assert configops.describe(g.reg, off) == f"listening in <#{SIGNUP_CH}>: replies → off"
    d = configops.describe_ops(g.reg, [off])
    part = d.lines[0].parts[0]
    assert d.lines[0].label == "Plain-text permissions" and part.label == f"Listening in <#{SIGNUP_CH}>" and (part.before, part.after) == ("replies", "off")


def test_reply_carries_one_consequence_sentence(g, monkeypatch):
    monkeypatch.setenv(decisions.KEY_ENV, "x")
    op = ConfigOp(op="plain", target=f"listen <#{SIGNUP_CH}>", value="reply")
    want = f"The bot answers messages in <#{SIGNUP_CH}> that are clearly for it, without being tagged."
    assert configops.op_consequences(g.reg, op) == [want]
    assert configops.op_consequences(g.reg, ConfigOp(op="plain", target=f"listen <#{SIGNUP_CH}>", value="watch")) == []
    assert want in configops.describe_ops(g.reg, [op]).consequences
    listen(g.reg, **{str(SIGNUP_CH): "reply"})
    assert configops.op_consequences(g.reg, op) == []  # already replying: nothing new to say


def test_structured_fields_one_write(g):
    reg = g.reg
    ops = configops.ops_from_fields(reg, "plain", None, {"listen": {str(SIGNUP_CH): "reply", str(GENERAL_CH): "watch"}, "dm": "answer"})
    assert [(o.target, o.value) for o in ops] == [("dm", "answer"), (f"listen <#{SIGNUP_CH}>", "reply"), (f"listen <#{GENERAL_CH}>", "watch")]
    merged = configops.coalesce(ops)
    assert len(merged) == 1
    patch = configops._plain_merged(reg, merged[0].parts)
    reg.set_plain_policy(reg.merge_plain_policy(patch), "owner")
    assert reg.listen_channels() == {str(SIGNUP_CH): "reply", str(GENERAL_CH): "watch"} and reg.config.plain.dm == "answer"
    ops = configops.ops_from_fields(reg, "plain", None, {"listen": {str(SIGNUP_CH): "reply"}})
    assert [(o.target, o.value) for o in ops] == [(f"listen <#{GENERAL_CH}>", "off")]
    ops = configops.ops_from_fields(reg, "plain", None, {"set_listen": {str(SIGNUP_CH): None}})
    assert [(o.target, o.value) for o in ops] == [(f"listen <#{SIGNUP_CH}>", "off")]
    with pytest.raises(RegistryError, match="listen, set_listen"):
        configops.ops_from_fields(reg, "plain", None, {"hear": {}})


def test_summary_and_parser_policy_words(g):
    sec = next(s for s in configops.config_summary(g.reg) if s.key == "plain")
    assert "Listening: off in every channel — the owner turns it on per channel on the Ops page." in sec.lines
    listen(g.reg, **{str(SIGNUP_CH): "reply", str(GENERAL_CH): "watch"})
    sec = next(s for s in configops.config_summary(g.reg) if s.key == "plain")
    assert f"Listening: <#{SIGNUP_CH}> replies, <#{GENERAL_CH}> watches" in sec.lines
    text = configops.plain_text_policy(g.reg)
    assert f"- listening in <#{SIGNUP_CH}>: reply" in text and f"- listening in <#{GENERAL_CH}>: watch" in text


def test_the_parser_schema_stays_small():
    assert len(ConfigOp.model_fields) <= 13
    assert "listen <#channel>" in ConfigOp.model_fields["target"].description
    assert "target=listen <channel mention>" in configops.SCHEMA_TEXT


# ---- the gate

class Decide:
    """A fake decision service: fixed probability, counts calls, keeps the last state."""

    available = True

    def __init__(self, p):
        self.p, self.calls, self.states = p, 0, []

    async def noul(self, state, instructions, criteria=None, workload="listen"):
        self.calls += 1
        self.states.append(state)
        return self.p


class Ops:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None, **kw):
        self.lines.append((level, text))


class Bot(db.ListenMixin):
    on_message = db.OibotGM.on_message
    _addressed = db.OibotGM._addressed

    def __init__(self, reg):
        self.reg = reg
        self.registries = types.SimpleNamespace(by_key={reg.key: reg}, for_guild=lambda gid: reg)
        self.ops = Ops()
        self.user = types.SimpleNamespace(id=BOT_ID)
        self.handled = []

    async def plain_text(self, message, reg, text, channel):
        self.handled.append((message.id, text, channel))

    async def news_message(self, message):
        return False

    def saw_member(self, gid, author):
        pass

    def event_for(self, channel_id):
        return None


_next_id = iter(range(5000, 10_000))


def message(author, channel=SIGNUP_CH, text="when does Barrow Deeps run this week?", *, bot=False, mentions=()):
    reacts = []

    async def add_reaction(emoji):
        reacts.append(emoji)

    async def remove_reaction(emoji, member):
        reacts.remove(emoji)
        reacts.append(("removed", emoji, getattr(member, "id", member)))

    return types.SimpleNamespace(id=next(_next_id), content=text, author=types.SimpleNamespace(id=author, display_name=f"user{author}", bot=bot),
                                 channel=types.SimpleNamespace(id=channel, name="signups", topic="Sign up for raids here", category=types.SimpleNamespace(name="Raiding")),
                                 guild=types.SimpleNamespace(id=1, me=None),
                                 mentions=list(mentions), role_mentions=[], reference=None, add_reaction=add_reaction,
                                 remove_reaction=remove_reaction, reacts=reacts)


@pytest.fixture
def gate(g, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "LISTEN_LOG", tmp_path / "listen.jsonl")
    monkeypatch.delenv("OIBOT_LISTEN_SURE", raising=False)
    bot = Bot(g.reg)

    def run(p, msg, mode="reply"):
        listen(g.reg, **{str(msg.channel.id): mode})
        dec = Decide(p)
        monkeypatch.setattr(decisions, "DECISIONS", dec)
        asyncio.run(bot.on_message(msg))
        return dec

    return types.SimpleNamespace(bot=bot, run=run, log=tmp_path / "listen.jsonl", g=g)


def react(bot, msg, user):
    payload = types.SimpleNamespace(emoji="👀", message_id=msg.id, user_id=user, channel_id=msg.channel.id, guild_id=1)
    asyncio.run(bot.on_raw_reaction_add(payload))


def test_sure_in_a_reply_channel_goes_to_plain_text(gate):
    msg = message(gate.g.me.discord_id)
    dec = gate.run(0.93, msg)
    assert dec.calls == 1 and gate.bot.handled == [(msg.id, msg.content, SIGNUP_CH)]
    st = dec.states[0]
    assert st["bot"] == db.LISTEN_BOT_WORDS and st["message"]["text"] == msg.content and st["channel"]["purpose"] == "the signup channel"
    assert st["channel"]["topic"] == "Sign up for raids here" and st["channel"]["category"] == "Raiding", "what Discord knows about the room rides along"
    assert "who plays which class" in st["bot"] and "what class or spec someone plays" in db.LISTEN_QUESTION
    assert "Barrow Deeps" in st["guild"]["raids"] and "rostered" in st["guild"]["words"]
    assert gate.bot.listen_recent(gate.g.reg)[0]["did"] == "replied"


def test_sure_in_a_watch_channel_is_only_recorded(gate):
    msg = message(gate.g.me.discord_id)
    gate.run(0.95, msg, mode="watch")
    assert gate.bot.handled == [] and msg.reacts == []
    row = gate.bot.listen_recent(gate.g.reg)[0]
    assert row["did"] == "would reply" and row["mode"] == "watch" and row["p"] == 0.95 and row["text"] == msg.content[:120]


def test_unsure_adds_eyes_and_only_the_author_routes_it(gate):
    me, other = gate.g.me.discord_id, gate.g.other.discord_id
    msg = message(me)
    gate.run(0.6, msg)
    assert msg.reacts == ["👀"] and gate.bot.handled == [] and gate.bot.listen_recent(gate.g.reg)[0]["did"] == "asked"
    react(gate.bot, msg, other)
    assert gate.bot.handled == []
    react(gate.bot, msg, me)
    assert gate.bot.handled == [(msg.id, msg.content, SIGNUP_CH)]
    react(gate.bot, msg, me)  # once only
    assert len(gate.bot.handled) == 1


def test_an_unclicked_eyes_expires(gate, monkeypatch):
    me = gate.g.me.discord_id
    msg = message(me)
    gate.run(0.6, msg)
    t0 = db.time.monotonic()
    monkeypatch.setattr(db.time, "monotonic", lambda: t0 + db.LISTEN_ASK_S + 1)
    react(gate.bot, msg, me)
    assert gate.bot.handled == [] and gate.bot._listen()["asks"] == {}


def test_the_window_is_an_hour_and_the_eyes_come_off_when_it_closes(gate):
    """A 👀 still on a message is one that still works: when the hour passes with no click, the bot takes its own 👀 off
    (2026-10-30: a writer clicked a 40-minute-old 👀 and nothing happened, with no sign the ask was over)."""
    assert db.LISTEN_ASK_S == 3600
    me = gate.g.me.discord_id
    msg = message(me)
    gate.run(0.6, msg)
    assert msg.reacts == ["👀"]
    asyncio.run(gate.bot._listen_expire(msg.id, delay=0))
    assert msg.reacts == [("removed", "👀", BOT_ID)] and gate.bot._listen()["asks"] == {}
    react(gate.bot, msg, me)
    assert gate.bot.handled == []


def test_a_clicked_eyes_stays_as_the_mark_of_the_answer(gate):
    me = gate.g.me.discord_id
    msg = message(me)
    gate.run(0.6, msg)
    react(gate.bot, msg, me)
    asyncio.run(gate.bot._listen_expire(msg.id, delay=0))  # the window closing later finds nothing to take off
    assert msg.reacts == ["👀"] and len(gate.bot.handled) == 1


def test_the_question_names_profession_asks_and_news():
    """Retune 2026-10-30: "does anyone have the pattern X?" scored 56% and "i just got the leatherworking pattern X" 66%
    in #professions — under the reply line. Probed with these sentences: 96–97% each, banter unchanged at ≤15%."""
    q = db.LISTEN_QUESTION
    assert "who has, can make or craft a pattern, recipe or item" in q and "even when it is addressed to everyone" in q
    assert "got, learned or can now make a pattern or recipe" in q
    assert "recipe or pattern the writer has, learned or got" in db.LISTEN_CRITERIA["true"]


def test_unsure_in_a_watch_channel_and_below_are_quiet(gate):
    msg = message(gate.g.me.discord_id)
    gate.run(0.6, msg, mode="watch")
    assert msg.reacts == [] and gate.bot.listen_recent(gate.g.reg)[0]["did"] == "would ask"
    msg2 = message(gate.g.me.discord_id, text="lol nice pull")
    gate.run(0.1, msg2)
    assert msg2.reacts == [] and gate.bot.handled == [] and gate.bot.listen_recent(gate.g.reg)[0]["did"] == "quiet"


def test_an_at_mention_is_not_decided_twice(gate):
    bot_user = gate.bot.user
    msg = message(gate.g.me.discord_id, text=f"<@{BOT_ID}> when is the raid?", mentions=[bot_user])
    dec = gate.run(0.99, msg)
    assert dec.calls == 0 and gate.bot.handled == [(msg.id, "when is the raid?", SIGNUP_CH)]  # the @mention path, once
    assert gate.bot.listen_recent(gate.g.reg) == []


def test_a_bot_author_never_and_empty_text_never(gate):
    dec = gate.run(0.99, message(gate.g.me.discord_id, bot=True))
    assert dec.calls == 0
    dec = gate.run(0.99, message(gate.g.me.discord_id, text="   "))
    assert dec.calls == 0 and gate.bot.handled == []


def test_a_channel_not_listened_in_is_never_decided(gate, monkeypatch):
    dec = Decide(0.99)
    monkeypatch.setattr(decisions, "DECISIONS", dec)
    asyncio.run(gate.bot.on_message(message(gate.g.me.discord_id, channel=GENERAL_CH)))
    assert dec.calls == 0 and gate.bot.handled == []


def test_no_key_warns_once_and_decides_nothing(gate, monkeypatch):
    listen(gate.g.reg, **{str(SIGNUP_CH): "reply"})
    monkeypatch.setattr(decisions, "DECISIONS", None)
    for _ in range(3):
        asyncio.run(gate.bot.on_message(message(gate.g.me.discord_id)))
    assert gate.bot.handled == [] and len(gate.bot.ops.lines) == 1
    level, text = gate.bot.ops.lines[0]
    assert level == "warn" and "TYPESAFE_API_KEY" in text and gate.bot.listen_recent(gate.g.reg) == []


def test_the_hourly_cap_skips_quietly(gate, monkeypatch):
    monkeypatch.setattr(db, "LISTEN_PER_HOUR", 2)
    me = gate.g.me.discord_id
    listen(gate.g.reg, **{str(SIGNUP_CH): "reply"})
    dec = Decide(0.95)
    monkeypatch.setattr(decisions, "DECISIONS", dec)
    for _ in range(3):
        asyncio.run(gate.bot.on_message(message(me)))
    assert dec.calls == 2 and len(gate.bot.handled) == 2 and gate.bot.listen_recent(gate.g.reg)[0]["did"] == "skipped"


def test_the_log_file_carries_no_text_and_recent_is_context(gate):
    me = gate.g.me.discord_id
    first = message(me, text="anyone up for a dungeon")
    gate.run(0.1, first)
    second = message(me, text="when do we lock tonight's roster")
    dec = gate.run(0.9, second)
    assert dec.states[0]["recent"] == [{"author": f"user{me}", "text": "anyone up for a dungeon"}]
    rows = [json.loads(x) for x in gate.log.read_text().splitlines()]
    assert len(rows) == 2 and all(set(r) == {"at", "guild", "channel", "author_id", "p", "mode", "did"} for r in rows)
    assert "dungeon" not in gate.log.read_text() and rows[1]["did"] == "replied" and rows[1]["author_id"] == me


def test_the_host_may_raise_sure(gate, monkeypatch):
    monkeypatch.setenv("OIBOT_LISTEN_SURE", "0.95")
    assert db.listen_sure() == 0.95
    msg = message(gate.g.me.discord_id)
    gate.run(0.9, msg)
    assert gate.bot.handled == [] and msg.reacts == ["👀"]
    monkeypatch.setenv("OIBOT_LISTEN_SURE", "0.1")
    assert db.listen_sure() > db.LISTEN_MAYBE


def test_a_channel_without_a_topic_or_category_sends_empty_strings(gate):
    msg = message(gate.g.me.discord_id)
    msg.channel = types.SimpleNamespace(id=777_001, name="general", topic=None, category=None)  # no job of the bot's
    dec = gate.run(0.1, msg)
    assert dec.states[0]["channel"] == {"name": "#general", "purpose": "", "topic": "", "category": ""}


# ---- mentions become names (every plain-text path: the @mention path, DMs, listening)

def _guild(roles=None, members=None, me_roles=()):
    roles, members = roles or {}, members or {}
    return types.SimpleNamespace(id=1, me=types.SimpleNamespace(roles=[types.SimpleNamespace(id=r) for r in me_roles]),
                                 get_role=roles.get, get_member=members.get)


def test_mention_names(g):
    reg, bot_user = g.reg, types.SimpleNamespace(id=BOT_ID)
    other = g.other
    stranger = types.SimpleNamespace(id=4242, display_name="Wanderer")
    guild = _guild(roles={88: types.SimpleNamespace(name="Raiders")}, members={4343: types.SimpleNamespace(display_name="Lurker", name="lurk")}, me_roles=(99,))
    msg = message(g.me.discord_id, text=f"<@{BOT_ID}> is <@!{other.discord_id}> playing healer?  <@4242> <@4343> <@5151> <@&88> <@&99> <@&12> in <#{SIGNUP_CH}>",
                  mentions=[bot_user, stranger])
    msg.guild = guild
    assert db.mention_names(msg, reg, bot_user) == f"is {other.display_name} playing healer? Wanderer Lurker @someone @Raiders @role in <#{SIGNUP_CH}>"
    dm = message(g.me.discord_id, text=f"what does <@{other.discord_id}> play?")
    dm.guild = None
    assert db.mention_names(dm, reg, bot_user) == f"what does {other.display_name} play?", "a DM still names a registered member"


def test_a_listened_message_reads_the_name_everywhere(gate):
    other = gate.g.other
    msg = message(gate.g.me.discord_id, text=f"is <@{other.discord_id}> playing healer?")
    dec = gate.run(0.93, msg)
    want = f"is {other.display_name} playing healer?"
    assert dec.states[0]["message"]["text"] == want and gate.bot.listen_recent(gate.g.reg)[0]["text"] == want
    assert gate.bot.handled == [(msg.id, want, SIGNUP_CH)]


def test_listening_words_cover_professions(gate):
    from oibot_gm import discord_bot as db
    assert "professions" in db.LISTEN_BOT_WORDS and "notable recipes" in db.LISTEN_BOT_WORDS
    assert "who can make" in db.LISTEN_QUESTION
    assert {"recipe", "craft", "who can make", "profession", "skill"} <= set(db.LISTEN_GLOSSARY)
    assert not {"leatherworking", "alchemy", "tailoring"} & set(db.LISTEN_GLOSSARY), "game data lives in the profile"
    dec = gate.run(0.93, message(gate.g.me.discord_id, text="who can make a bag?"))
    words = dec.states[0]["guild"]["words"]
    assert {d.name.lower() for d in gate.g.reg.profile.professions.values()} <= set(words) and "leatherworking" in words
