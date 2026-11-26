"""Task 11 (2026-11-25): the bot hears the answer to its own question, and binds the one character a spec change can mean.

A. An open question keeps the conversation open: when the bot's last reply to a person in a channel asked them something
   (the conversation ring's newest exchange has `questions`, younger than LISTEN_FOLLOWUP_S), their NEXT message there
   skips the listening gate and goes down the @mention path, once. Driven through the real on_message, listen gate,
   plain_text and handle_change; the decision service, the help answer and the parser are fakes. No Discord, no model.
B. A spec change that names a member but not which character binds the one character whose class has the new spec
   (and that holds the spec it changes from), in code: configops.narrow_characters.
All names are invented."""
from __future__ import annotations

import asyncio
import contextlib
import json
import types

import discord
import pytest
from conftest import OWNER
from test_help import FakeClient

from oibot_gm import answer_switch, configops, conversation, discord_bot as db, discord_help, discord_policy
from oibot_gm.configops import ConfigOp, ConfigRequest
from oibot_gm.llm import decisions

ASHGROVE = 700_000_000_000_000_101  # the member whose spec changes: a Protection Warrior alt and a Shadow Priest main
BYSTANDER = 700_000_000_000_000_102  # another member in the same channel
LISTEN_CH, OTHER_CH, WATCH_CH = 930_001, 930_002, 930_003
BOT_ID = 777
QUESTION = "Which character should have its spec set to Holy?"
LOOP_ASKS = "Which character should have its spec set to Holy?\n• Ironvale\n• Duskmere"


@pytest.fixture(autouse=True)
def _fresh():
    conversation.forget()
    yield
    conversation.forget()


def ashgrove(reg, *, second_priest=False, paladin=False):
    reg.add_character(ASHGROVE, "Ashgrove", "Ironvale", "Warrior", "Protection", None, False)
    reg.add_character(ASHGROVE, "Ashgrove", "Duskmere", "Priest", "Shadow", None, True)
    if second_priest:
        reg.add_character(ASHGROVE, "Ashgrove", "Gloamwick", "Priest", "Shadow", None, False)
    if paladin:
        reg.add_character(ASHGROVE, "Ashgrove", "Brightfen", "Paladin", "Retribution", None, False)
    return reg.members[ASHGROVE]


# ---------------------------------------------------------------- A: the bot and its messages

class Decide:
    """The listening gate's decision service: a fixed probability, every call counted."""

    available = True

    def __init__(self, p):
        self.p, self.calls = p, 0

    async def ask(self, state, questions, workload="listen"):
        self.calls += 1
        return {"decision": {"type": "noul", "noul": self.p}}


class Chan:
    def __init__(self, cid):
        self.id, self.name, self.topic, self.category = cid, "raid-chat", None, None

    @contextlib.asynccontextmanager
    async def typing(self):
        yield


class Msg(discord.Message):
    def __init__(self, **kw):  # a stand-in: only what on_message, the gate, plain_text and handle_change read
        for k, v in kw.items():
            setattr(self, k, v)


class FollowBot(db.ListenMixin, FakeClient):
    on_message = db.OibotGM.on_message
    _addressed = db.OibotGM._addressed
    plain_text = db.OibotGM.plain_text


_ids = iter(range(50_000, 90_000, 10))


def make(reg, rs, monkeypatch, tmp_path, verdicts):
    """The real on_message → gate / follow-up → plain_text → handle_change; the help answer says "this asks to act" (as
    it did live), the parser returns `verdicts` in turn and records what it read."""
    monkeypatch.setattr(db, "LISTEN_LOG", tmp_path / "listen.jsonl")
    monkeypatch.delenv("OIBOT_LISTEN_SURE", raising=False)
    guild = types.SimpleNamespace(id=1, me=None, channels=[], get_member=lambda uid: None, members=[])
    bot = FollowBot(reg, rs, guild)
    bot.ctx = types.SimpleNamespace(provider=object())
    lines = []

    async def emit(cfg, level, line, *a, **k):
        lines.append((level, line))

    bot.ops = types.SimpleNamespace(recent_for=lambda key: [], emit=emit)
    bot.registries = types.SimpleNamespace(by_key={reg.key: reg}, for_guild=lambda gid: reg)
    bot.user = types.SimpleNamespace(id=BOT_ID)
    bot.policies = types.SimpleNamespace(store=lambda r: None)
    bot.recall_memories = lambda reg_, text, channel: asyncio.sleep(0, result=[])
    bot.reply_private = lambda reg_, ch: False

    async def plain_identity(reg_, user, **_):
        return user.id == OWNER, []

    async def help_answer(*a, **k):
        bot.helped.append(a[3])
        if k.get("may_act"):
            return db.HELP_ACT
        # the loop's re-ask (switch on) ends on a clarifying question with its choices; elsewhere a plain answer
        return discord_help.Asked(LOOP_ASKS, ["Ironvale", "Duskmere"]) if a[1] == OWNER else "Ask an officer."

    async def news_message(message):
        return False

    bot.plain_identity, bot.help_answer, bot.news_message = plain_identity, help_answer, news_message
    bot.saw_member = lambda gid, author: None
    bot.event_for = lambda cid: None
    bot.helped = []
    parsed = []

    def parse(provider, reg_, text, by=None, officer=False, **kw):
        parsed.append((text, kw.get("conversation") or ""))
        return bot.verdicts[len(parsed) - 1].model_copy(deep=True)

    monkeypatch.setattr(configops, "parse", parse)
    bot.parsed, bot.verdicts, bot.greg = parsed, list(verdicts), reg
    return bot


def say(bot, uid, text, cid=LISTEN_CH, *, p=None):
    """One message on the real on_message; `p` = the gate's probability (a fresh fake service per message)."""
    said = []

    async def reply(content=None, **kw):
        said.append((content, kw))
        return types.SimpleNamespace(id=next(_ids))

    async def add_reaction(emoji):
        said.append(("react", emoji))

    msg = Msg(id=next(_ids), content=text, author=types.SimpleNamespace(id=uid, display_name=f"user{uid}", bot=False),
              channel=Chan(cid), guild=types.SimpleNamespace(id=1, me=None), mentions=[], role_mentions=[], reference=None,
              reply=reply, add_reaction=add_reaction)
    dec = Decide(0.0 if p is None else p)
    bot.decisions = dec
    asyncio.run(bot.on_message(msg))
    return said, dec


def listen(reg, **chans):
    reg.set_plain_policy(reg.merge_plain_policy({"set_listen": chans}), "owner")


FIRST = ConfigRequest(kind="question", reply="Ashgrove has two characters.", questions=[QUESTION])
ANSWERED = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="character", member="Ashgrove", character="Duskmere", field="spec", value="Holy")])


@pytest.fixture
def live(reg, rs, monkeypatch, tmp_path):
    """The live shape: a reply channel, a sure first message whose parse asks which character, then the bare answer."""
    monkeypatch.delenv(answer_switch.SWITCH_ENV, raising=False)
    ashgrove(reg)
    reg.add_character(BYSTANDER, "Fernhollow", "Fernhollow", "Mage", "Frost", None, True)
    listen(reg, **{str(LISTEN_CH): "reply", str(OTHER_CH): "reply", str(WATCH_CH): "watch"})
    monkeypatch.setattr(decisions, "DECISIONS", Decide(0.99))
    bot = make(reg, rs, monkeypatch, tmp_path, [FIRST, ANSWERED, ANSWERED])

    def gate(p):
        dec = Decide(p)
        monkeypatch.setattr(decisions, "DECISIONS", dec)
        return dec

    bot.gate = gate
    bot.log = tmp_path / "listen.jsonl"
    return bot


def first_question(bot):
    dec = bot.gate(0.99)
    said, _ = say(bot, OWNER, "bot can you update ashgrove from shadow to holy")
    assert dec.calls == 1 and len(said) == 1 and QUESTION in said[0][0], "the gate replied and the parser (or the loop) asked"
    return said


def rows(bot):
    return [json.loads(x) for x in bot.log.read_text().splitlines()] if bot.log.exists() else []


@pytest.mark.parametrize("on", [False, True])
def test_the_bare_answer_skips_the_gate_and_reaches_the_parser_with_its_question(live, monkeypatch, on):
    """The live exchange end to end, with the answer tools' switch off (today) and on: the answer is not overheard, so no
    decision is asked for it, no 👀, and the change card is shown."""
    if on:
        monkeypatch.setenv(answer_switch.SWITCH_ENV, "on")
    first_question(live)
    dec = live.gate(0.12)  # what the gate scored the bare answer live
    said, _ = say(live, OWNER, "Duskmere")
    assert dec.calls == 0, "no decision request for the follow-up"
    assert live.parsed[1][0] == "Duskmere" and QUESTION in live.parsed[1][1] and "from shadow to holy" in live.parsed[1][1]
    assert ("react", "👀") not in said
    content, kw = said[-1]
    assert kw.get("embed") is not None and kw.get("view") is not None, "the diff with Apply"
    assert "Duskmere: spec Shadow → Holy" in kw["embed"].description
    assert [r["did"] for r in rows(live)] == ["replied", "followup"]
    assert rows(live)[1]["p"] is None and set(rows(live)[1]) == {"at", "guild", "channel", "author_id", "p", "mode", "did", "remembered"}
    assert "Duskmere" not in live.log.read_text(), "the log row carries no text"
    assert live.listen_recent(live.greg)[0]["did"] == "followup"


def test_a_second_message_is_not_a_follow_up(live):
    first_question(live)
    say(live, OWNER, "Duskmere")
    dec = live.gate(0.1)
    say(live, OWNER, "lol thanks")
    assert dec.calls == 1 and len(live.parsed) == 2, "the diff asked nothing: the question was taken once"


def test_a_follow_up_that_is_not_an_answer_still_takes_the_question(live):
    """Whatever the one message says, it takes the question: the next one is judged by the gate again."""
    live.verdicts[1] = ConfigRequest(kind="ignore", reply="")
    first_question(live)
    said, _ = say(live, OWNER, "brb dinner")
    assert said == [] and len(live.parsed) == 2, "addressed once: the parser read it and said ignore, nothing posted"
    dec = live.gate(0.1)
    say(live, OWNER, "back")
    assert dec.calls == 1 and len(live.parsed) == 2


def test_another_person_is_not_the_answer(live):
    first_question(live)
    dec = live.gate(0.1)
    say(live, BYSTANDER, "Duskmere")
    assert dec.calls == 1 and len(live.parsed) == 1
    dec = live.gate(0.1)
    say(live, OWNER, "Duskmere")  # the asker's own next message still answers it
    assert dec.calls == 0 and len(live.parsed) == 2


def test_the_same_person_in_another_channel_is_not_the_answer(live):
    first_question(live)
    dec = live.gate(0.1)
    say(live, OWNER, "Duskmere", cid=OTHER_CH)
    assert dec.calls == 1 and len(live.parsed) == 1


def test_after_the_window_it_is_not_the_answer(live, monkeypatch):
    first_question(live)
    t = conversation.time.time()
    monkeypatch.setattr(conversation.time, "time", lambda: t + db.LISTEN_FOLLOWUP_S + 1)
    dec = live.gate(0.1)
    say(live, OWNER, "Duskmere")
    assert dec.calls == 1 and len(live.parsed) == 1 and db.LISTEN_FOLLOWUP_S == 300


def test_a_watch_only_channel_stays_silent(live):
    """An open question in a channel the bot only watches (asked there through an @mention) gets no follow-up."""
    conversation.remember(live.greg, WATCH_CH, OWNER, "which raid", "Which?", [QUESTION], message_id=1)
    dec = live.gate(0.99)
    said, _ = say(live, OWNER, "Duskmere", cid=WATCH_CH)
    assert said == [] and len(live.parsed) == 0 and dec.calls == 1, "the gate only records it, as in any watch channel"
    assert [r["did"] for r in rows(live)] == ["would reply"]


def test_a_bot_never_answers_and_an_at_mention_takes_the_question(live):
    reg = live.greg
    first_question(live)
    # a bot's message never counts (on_message drops every bot author before routing)
    bot_msg = Msg(id=next(_ids), content="Duskmere", author=types.SimpleNamespace(id=OWNER, display_name="x", bot=True), channel=Chan(LISTEN_CH),
                  guild=types.SimpleNamespace(id=1, me=None), mentions=[], role_mentions=[], reference=None)
    asyncio.run(live.on_message(bot_msg))
    assert len(live.parsed) == 1
    # an @mention is the person's next message: it takes the question, so the message after it is judged again
    mention = Msg(id=next(_ids), content=f"<@{BOT_ID}> Duskmere", author=types.SimpleNamespace(id=OWNER, display_name="x", bot=False), channel=Chan(LISTEN_CH),
                  guild=types.SimpleNamespace(id=1, me=None), mentions=[live.user], role_mentions=[], reference=None,
                  reply=lambda content=None, **kw: asyncio.sleep(0, result=types.SimpleNamespace(id=next(_ids))), add_reaction=None)
    asyncio.run(live.on_message(mention))
    assert len(live.parsed) == 2
    assert conversation.take_open_question(reg, LISTEN_CH, OWNER, window_s=300) is None


def test_who_may_act_is_unchanged_for_a_follow_up(live):
    """The follow-up is authorised exactly as an @mention from that person: a member answering with a change to someone
    else's character is refused, no card."""
    reg = live.greg
    conversation.remember(reg, LISTEN_CH, BYSTANDER, "make ashgrove holy", "Which?", [QUESTION], message_id=1)
    live.verdicts[0] = ANSWERED
    said, dec = say(live, BYSTANDER, "Duskmere")
    assert dec.calls == 0 and len(live.parsed) == 1
    assert len(said) == 1 and said[0][1].get("embed") is None and "⛔" in said[0][0]


def test_a_slash_commands_private_question_is_not_answered_in_public(live):
    """/gm change shares the channel's ring, but its question was private: an untagged message is judged by the gate."""
    conversation.remember(live.greg, LISTEN_CH, OWNER, "make ashgrove holy", "Which?", [QUESTION])  # no message id: a slash command
    dec = live.gate(0.1)
    said, _ = say(live, OWNER, "Duskmere")
    assert dec.calls == 1 and said == [] and live.parsed == []


# ---------------------------------------------------------------- the ring's open question

def test_take_open_question_is_the_newest_exchange_once(reg):
    conversation.remember(reg, LISTEN_CH, OWNER, "a", "b", ["Which?"], at=1000.0)
    assert conversation.take_open_question(reg, LISTEN_CH, OWNER, window_s=300, now=1301.0) is None, "past the window"
    conversation.remember(reg, LISTEN_CH, OWNER, "a", "b", ["Which?"], at=2000.0)
    assert conversation.take_open_question(reg, LISTEN_CH, OWNER, window_s=300, now=2100.0) is not None
    assert conversation.take_open_question(reg, LISTEN_CH, OWNER, window_s=300, now=2100.0) is None, "taken once"
    conversation.remember(reg, LISTEN_CH, OWNER, "a", "b", ["Which?"], at=3000.0)
    conversation.remember(reg, LISTEN_CH, OWNER, "c", "Done.", at=3010.0)
    assert conversation.take_open_question(reg, LISTEN_CH, OWNER, window_s=300, now=3020.0) is None, "a reply that asks nothing closes it"


def test_the_loops_choices_are_kept_as_the_open_question(reg, rs, monkeypatch):
    """Switch on: a loop answer that ended on a clarifying question returns its choices with the text (discord_help.Asked),
    and plain_text keeps them in the ring, so the next message is the answer."""
    from answer_fakes import ScriptedProvider, call, reply
    from test_answer_routing import WREN, chat_bot, crafter, message

    monkeypatch.setenv(answer_switch.SWITCH_ENV, "on")
    crafter(reg)
    provider = ScriptedProvider([[call("recipe", name="ironfeather")],
                                 [reply(answer="Which one do you mean?", choices=["Ironfeather Breastplate"])]])
    bot = chat_bot(reg, rs, provider)
    msg, said = message(WREN, cid=LISTEN_CH)
    asyncio.run(bot.plain_text(msg, reg, "who makes ironfeather", LISTEN_CH))
    assert said and said[0][0].startswith("Which one do you mean?")
    (ex,) = conversation.recall(reg, LISTEN_CH, WREN)
    assert ex.questions == ["Ironfeather Breastplate"]
    assert conversation.take_open_question(reg, LISTEN_CH, WREN, window_s=db.LISTEN_FOLLOWUP_S) is ex


# ---------------------------------------------------------------- B: the one character a spec change can mean

def narrow(reg, op, author=OWNER):
    req = ConfigRequest(kind="change", reply="Changing the spec.", ops=[op])
    configops.narrow_characters(reg, req, author)
    return req


def test_from_shadow_to_holy_binds_the_priest(reg):
    ashgrove(reg)
    req = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="holy", text="shadow"))
    (op,) = req.ops
    assert (op.character, op.value) == ("Duskmere", "Holy") and req.questions == []
    assert configops.describe(reg, op, strict=True, by_id=OWNER) == "Duskmere: spec Shadow → Holy"


def test_the_member_named_where_a_character_goes_still_binds(reg):
    ashgrove(reg)
    (op,) = narrow(reg, ConfigOp(op="character", character="Ashgrove", field="spec", value="Holy")).ops
    assert (op.member, op.character) == ("Ashgrove", "Duskmere")


def test_two_priests_ask_about_only_the_two(reg):
    ashgrove(reg, second_priest=True)
    req = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="Holy", text="Shadow"))
    assert req.ops == [] and req.reply == ""
    assert req.questions == ["Which of Ashgrove's characters should play Holy: Duskmere or Gloamwick?"]


def test_the_spec_it_changes_from_narrows_further(reg):
    """A Retribution Paladin can be Holy too; "from Shadow" leaves the priest alone."""
    ashgrove(reg, paladin=True)
    (op,) = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="Holy", text="Shadow")).ops
    assert op.character == "Duskmere"
    req = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="Holy"))
    assert req.questions == ["Which of Ashgrove's characters should play Holy: Duskmere or Brightfen?"]


def test_a_spec_no_character_can_take_is_one_sentence(reg, rs, monkeypatch, tmp_path):
    ashgrove(reg)
    (op,) = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="restoration")).ops
    with pytest.raises(configops.RegistryError, match=r"^None of Ashgrove's characters is of a class with a Restoration spec\.$"):
        configops.describe(reg, op, strict=True, by_id=OWNER)
    (op,) = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="Holy", text="Discipline")).ops
    with pytest.raises(configops.RegistryError, match=r"^None of Ashgrove's characters that can play Holy is Discipline now\.$"):
        configops.describe(reg, op, strict=True, by_id=OWNER)
    # through handle_change: the refusal line, no card
    bot = make(reg, rs, monkeypatch, tmp_path, [])
    said = []

    async def reply(content=None, **kw):
        said.append((content, kw))
        return types.SimpleNamespace(id=1)

    msg = Msg(id=1, content="x", author=types.SimpleNamespace(id=OWNER, display_name="o", bot=False), channel=Chan(LISTEN_CH),
              guild=types.SimpleNamespace(id=1), reference=None, reply=reply)
    req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="character", member="Ashgrove", field="spec", value="restoration")])
    asyncio.run(discord_policy.handle_change(msg, reg, None, object(), bot.ops, "make ashgrove resto", bot=bot, channel=LISTEN_CH, req=req))
    assert len(said) == 1 and said[0][1].get("embed") is None
    assert "None of Ashgrove's characters is of a class with a Restoration spec." in said[0][0]


def test_your_own_record_binds_your_priest(reg):
    ashgrove(reg)
    req = narrow(reg, ConfigOp(op="character", field="spec", value="Holy", text="Shadow"), author=ASHGROVE)
    (op,) = req.ops
    assert op.character == "Duskmere" and not op.member
    allowed, refused = configops.authorize(reg, req.ops, ASHGROVE, officer=False, channel=None)
    assert allowed == [op] and refused == []
    assert op.member == f"<@{ASHGROVE}>", "bind_self pins the requester, the character is theirs"
    req = narrow(reg, ConfigOp(op="character", field="spec", value="Restoration"), author=ASHGROVE)
    configops.authorize(reg, req.ops, ASHGROVE, officer=False, channel=None)
    with pytest.raises(configops.RegistryError, match=r"^None of your characters is of a class with a Restoration spec\.$"):
        configops.describe(reg, req.ops[0], strict=True, by_id=ASHGROVE)


def test_another_members_change_is_authorised_as_before(reg):
    """Binding the character widens nothing: a member asking about someone else is refused exactly as before."""
    ashgrove(reg)
    reg.add_character(BYSTANDER, "Fernhollow", "Fernhollow", "Mage", "Frost", None, True)
    req = narrow(reg, ConfigOp(op="character", member="Ashgrove", field="spec", value="Holy", text="Shadow"), author=BYSTANDER)
    assert req.ops[0].character == "Duskmere"
    allowed, refused = configops.authorize(reg, req.ops, BYSTANDER, officer=False, channel=LISTEN_CH)
    assert allowed == [] and refused
    allowed, refused = configops.authorize(reg, req.ops, OWNER, officer=True, channel=LISTEN_CH)
    assert len(allowed) == 1


def test_a_named_character_and_other_ops_are_left_alone(reg):
    ashgrove(reg)
    named = ConfigOp(op="character", member="Ashgrove", character="Ironvale", field="spec", value="Holy")
    other = ConfigOp(op="character", member="Ashgrove", character="Duskmere", field="offspec", value="Holy")
    req = narrow(reg, named)
    assert req.ops == [named] and req.ops[0].character == "Ironvale" and req.ops[0]._no_fit is None, "the registry decides"
    req = narrow(reg, other)
    assert req.ops == [other]


def test_the_parsers_prompt_carries_the_rule_in_both_variants(monkeypatch):
    line = "never ask which character when the member's listed characters and the specs the request names leave only one that fits: name it"
    assert line in configops.SYSTEM.format(schema=configops.SCHEMA_TEXT)
    assert line in configops.SYSTEM_ANSWER_TOOLS.format(schema=configops.SCHEMA_TEXT)
    assert "spec: text = the spec it changes from" in configops.SCHEMA_TEXT


def test_the_members_block_lists_class_and_spec(reg):
    ashgrove(reg)
    text = configops.current_config_text(reg)
    assert "Ironvale:" in text and "Warrior Protection" in text and "Duskmere*:" in text and "Priest Shadow" in text


def test_the_site_box_binds_the_priest_and_refuses_in_one_sentence(reg, rs, monkeypatch):
    """The site's Tell the bot box (and MCP's plain_change) reads the same narrowing: one op on the priest; a spec no
    character can take is the one sentence among the refusals."""
    from test_answer_routing import site_bot

    monkeypatch.delenv(answer_switch.SWITCH_ENV, raising=False)
    ashgrove(reg)
    post, _ = site_bot(reg, rs, monkeypatch, ConfigRequest(kind="change", reply="", ops=[
        ConfigOp(op="character", member="Ashgrove", field="spec", value="holy", text="shadow")]))
    d = post("update ashgrove from shadow to holy")
    assert [o.get("character") for o in d["ops"]] == ["Duskmere"] and d["describe"] == ["Duskmere: spec Shadow → Holy"]
    post, _ = site_bot(reg, rs, monkeypatch, ConfigRequest(kind="change", reply="", ops=[
        ConfigOp(op="character", member="Ashgrove", field="spec", value="Restoration")]))
    d = post("make ashgrove resto")
    assert d["ops"] == [] and d["refused"] == ["None of Ashgrove's characters is of a class with a Restoration spec."]
