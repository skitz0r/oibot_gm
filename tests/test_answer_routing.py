"""One path for questions (answer tools, design §8, Task 8): with the switch on for the guild, the change parser never
answers a question. Its verdict `question` (with or without questions of its own) goes to the lookup loop once, with
acting off; its verdict `change` keeps everything, its questions for a missing detail included. The same rule on
plain text (DMs, @mentions, act channels, /gm change), the site's box and the loot thread; /ask carries the recent
exchange. Every rule has a twin with the switch off proving today's behaviour. No Discord, no model, no network: the
scripted model of tests/answer_fakes.py and a faked parser returning what the real one returned live."""
from __future__ import annotations

import asyncio
import contextlib
import json
import types

import discord
import pytest
from answer_fakes import ScriptedProvider, call, reply
from conftest import OWNER
from test_answer_switch import make_bot, rows
from test_help import real_member
from test_loot_surface import OFFICER, Msg as LootMsg, night, run, tick  # noqa: F401 — night is the loot thread's fixture
from test_parity_ops_chat import Feed, interaction

from oibot_gm import answer_switch, configops, conversation, discord_bot as db, discord_help, discord_policy
from oibot_gm.configops import ConfigOp, ConfigRequest

WREN = 700_000_000_000_000_001  # a member whose main Thornwick knows Leatherworking 300 with Ironfeather Breastplate
GENERAL_CH, ACT_CH = 920_001, 920_002
LOOP_SAYS = "Thornwick can make the Ironfeather Breastplate."
PARSER_SAYS = "I don't track which recipes a character knows."


@pytest.fixture(autouse=True)
def _fresh_rings():
    conversation.forget()
    yield
    conversation.forget()


def switch(monkeypatch, on: bool):
    if on:
        monkeypatch.setenv(answer_switch.SWITCH_ENV, "on")
    else:
        monkeypatch.delenv(answer_switch.SWITCH_ENV, raising=False)


def crafter(reg):
    m = real_member(reg, WREN, "Thornwick", "Hunter", "Survival")
    reg.set_professions(WREN, "Thornwick", [{"id": "leatherworking", "skill": 300, "recipes": [{"id": 10790}]}])
    return m


class Msg(discord.Message):
    def __init__(self, **kw):  # a stand-in: only what plain_text and handle_change read
        for k, v in kw.items():
            setattr(self, k, v)


class Chan:
    def __init__(self, cid=None):
        self.id = cid

    @contextlib.asynccontextmanager
    async def typing(self):
        yield


def chat_bot(reg, rs, provider, *, officer_ids=()):
    """The real HelpMixin.help_answer (the loop when the switch is on) under the real OibotGM.plain_text."""
    bot = make_bot(reg, rs, provider)

    async def plain_identity(reg_, user, **_):
        return user.id == OWNER or user.id in officer_ids, []

    bot.plain_identity = plain_identity
    bot.policies = types.SimpleNamespace(store=lambda r: None)
    bot.recall_memories = lambda reg_, text, channel: asyncio.sleep(0, result=[])
    bot.reply_private = lambda reg_, ch: ch is None
    bot.plain_text = types.MethodType(db.OibotGM.plain_text, bot)
    return bot


def message(uid, *, guild=True, cid=GENERAL_CH, mid=5000):
    said = []

    async def reply_fn(content=None, **kw):
        said.append((content, kw))
        return types.SimpleNamespace(id=mid + 1 + len(said))

    msg = Msg(author=types.SimpleNamespace(id=uid, display_name="Wrenfield"), channel=Chan(cid if guild else None),
              guild=types.SimpleNamespace(id=1) if guild else None, reply=reply_fn, id=mid, reference=None)
    return msg, said


def fake_parser(monkeypatch, req: ConfigRequest):
    parsed = []

    def parse(provider, reg_, text, by=None, officer=False, **kw):
        parsed.append(text)
        return req

    monkeypatch.setattr(configops, "parse", parse)
    return parsed


def no_parser(monkeypatch):
    def parse(*a, **k):
        raise AssertionError("the parser is never called for this message")

    monkeypatch.setattr(configops, "parse", parse)


def tool_results(provider) -> str:
    return json.dumps(provider.requests[-1]["messages"])


# ---------------------------------------------------------------- the three live failures, end to end

def test_bench_ironfeather_who_has_never_gets_the_parsers_words(reg, rs, monkeypatch):
    """Live 2026-11-25: the answer step flagged it as a request to act, the parser said kind=question with a question of
    its own, and its line went out. Now: the loop answers from its lookup, the second time with acting off — and the
    re-ask runs once even though the scripted model sets `acts` again."""
    switch(monkeypatch, True)
    crafter(reg)
    parsed = fake_parser(monkeypatch, ConfigRequest(kind="question", reply="I don't see any loot award records for Ironfeather.",
                                                    questions=["Do you mean an item someone received?"]))
    provider = ScriptedProvider([[reply(answer="Let me change that.", acts=True)],
                                 [call("recipe", name="Ironfeather Breastplate")],
                                 [reply(answer=LOOP_SAYS, acts=True)]])
    bot = chat_bot(reg, rs, provider)
    asked = []
    real = bot.help_answer

    async def spy(*a, **k):
        asked.append(k.get("may_act"))
        return await real(*a, **k)

    bot.help_answer = spy
    msg, said = message(WREN)
    asyncio.run(bot.plain_text(msg, reg, "who has ironfeather", GENERAL_CH))
    assert [c for c, _ in said] == [LOOP_SAYS]
    assert "loot award" not in said[0][0] and "Do you mean" not in said[0][0]
    assert asked == [True, False], "the second answer had acting off"
    assert parsed == ["who has ironfeather"], "the parser ran once: no loop between the answer and the parser"
    assert provider.n == 3 and "Thornwick" in tool_results(provider), "the answer came from the recipe lookup"


def test_bench_ironfeather_breastplate_answers_from_the_loop(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    crafter(reg)
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply=PARSER_SAYS))
    provider = ScriptedProvider([[reply(answer="Changing it.", acts=True)], [reply(answer=LOOP_SAYS)]])
    msg, said = message(WREN)
    asyncio.run(chat_bot(reg, rs, provider).plain_text(msg, reg, "who can make ironfeather breastplate", GENERAL_CH))
    assert [c for c, _ in said] == [LOOP_SAYS]
    assert "Thornwick" in json.dumps(provider.requests[-1]["messages"]), "the start's matched names carry the crafter"


def test_bench_ironfeather_pieces_answers_from_its_lookups(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    crafter(reg)
    no_parser(monkeypatch)
    answer = "Thornwick can make the Ironfeather Breastplate. Nobody has the leggings or shoulders yet."
    provider = ScriptedProvider([[call("find", text="ironfeather", kinds=["recipe"])], [reply(answer=answer)]])
    msg, said = message(WREN)
    asyncio.run(chat_bot(reg, rs, provider).plain_text(msg, reg, "who can make ironfeather leather working pieces", GENERAL_CH))
    assert [c for c, _ in said] == [answer]
    seen = tool_results(provider)
    assert all(n in seen for n in ("Ironfeather Breastplate", "Ironfeather Leggings", "Ironfeather Shoulders"))


def test_switch_off_the_live_path_posts_the_parsers_line_as_today(reg, rs, monkeypatch):
    """The twin: switch off, help.answer says act, the parser's question goes out exactly as before."""
    switch(monkeypatch, False)
    crafter(reg)
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply=PARSER_SAYS))
    bot = chat_bot(reg, rs, ScriptedProvider([]))

    async def old_help(*a, **k):
        assert "entry" not in k, "switch off: today's arguments"
        return discord_help.ACT

    bot.help_answer = old_help
    msg, said = message(WREN)
    asyncio.run(bot.plain_text(msg, reg, "who can make ironfeather breastplate", GENERAL_CH))
    assert [c for c, _ in said] == [PARSER_SAYS]


# ---------------------------------------------------------------- handle_change: question vs change

def recorder(answer="the loop's answer"):
    asked = []

    async def help_answer(reg_, uid, officer, text, may_act=False, public=False, **kw):
        asked.append({"text": text, "may_act": may_act, **kw})
        return answer

    return asked, help_answer


def test_a_parser_question_with_its_own_questions_goes_to_the_loop(reg, monkeypatch):
    switch(monkeypatch, True)
    log = []
    asked, help_answer = recorder()
    it = interaction(OWNER, log, help_answer=help_answer)
    req = ConfigRequest(kind="question", reply="parser's line", questions=["Which item?"])
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "who has ironfeather", bot=it.client, channel=GENERAL_CH, req=req, to_help=False))
    assert log[-1] == ("followup", "the loop's answer", {"ephemeral": True})
    assert [a["may_act"] for a in asked] == [False] and asked[0]["entry"] == "change"


def test_a_change_that_asks_for_a_missing_detail_keeps_asking(reg, monkeypatch):
    switch(monkeypatch, True)
    log = []
    asked, help_answer = recorder()
    it = interaction(OWNER, log, help_answer=help_answer)
    req = ConfigRequest(kind="change", reply="", ops=[], questions=["Which dates?"])
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "mark me away", bot=it.client, channel=GENERAL_CH, req=req))
    assert not asked and log[-1][1] == "\n• Which dates?"
    (ex,) = conversation.recall(reg, GENERAL_CH, OWNER)
    assert ex.questions == ["Which dates?"], "the follow-up carries the detail asked for"


def test_act_channel_and_gm_change_route_questions_to_the_loop_even_with_parser_questions(reg, monkeypatch):
    switch(monkeypatch, True)
    reg.config.plain.channels[str(ACT_CH)] = "act"
    clar = ConfigRequest(kind="question", reply="Which run?", questions=["bd-1 or bd-2?"])
    # /gm change (an Interaction)
    log = []
    asked, help_answer = recorder()
    it = interaction(OWNER, log, help_answer=help_answer)
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "who is rostered?", bot=it.client, channel=ACT_CH, req=clar))
    assert log[-1][1] == "the loop's answer" and asked[-1]["entry"] == "change" and asked[-1]["may_act"] is False
    # a message in an act channel, through plain_text (the parser runs first there)
    fake_parser(monkeypatch, clar)
    bot = types.SimpleNamespace()
    msg, said = message(OWNER, cid=ACT_CH)

    async def plain_identity(reg_, user, **_):
        return True, []

    asked2, help_answer2 = recorder()
    bot = types.SimpleNamespace(ctx=types.SimpleNamespace(provider=object()), policies=types.SimpleNamespace(store=lambda r: None), ops=Feed(),
                                plain_identity=plain_identity, help_answer=help_answer2, raids=None, reply_private=lambda r, ch: False)
    asyncio.run(db.OibotGM.plain_text(bot, msg, reg, "who is rostered?", ACT_CH))
    assert [c for c, _ in said] == ["the loop's answer"] and asked2[0]["entry"] == "act" and asked2[0]["may_act"] is False


def test_switch_off_handle_change_posts_the_parser_reply_as_today(reg, monkeypatch):
    switch(monkeypatch, False)
    log = []
    asked, help_answer = recorder()
    it = interaction(OWNER, log, help_answer=help_answer)
    clar = ConfigRequest(kind="question", reply="Which run?", questions=["bd-1 or bd-2?"])
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "lock the run", bot=it.client, channel=GENERAL_CH, req=clar))
    assert not asked and log[-1][1] == "Which run?\n• bd-1 or bd-2?"
    plain = ConfigRequest(kind="question", reply="parser's short line")
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "x?", bot=it.client, channel=GENERAL_CH, req=plain, to_help=False))
    assert not asked and log[-1][1] == "parser's short line"


def test_outside_the_ask_audience_gets_the_static_guide(reg, monkeypatch):
    switch(monkeypatch, True)
    log = []
    _asked, help_answer = recorder(answer=None)
    it = interaction(OWNER, log, help_answer=help_answer)
    req = ConfigRequest(kind="question", reply="parser's short line")
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "x?", bot=it.client, channel=GENERAL_CH, req=req))
    kind, content, kw = log[-1]
    assert content == discord_help.guide_intro(reg) and "parser's short line" not in content and kw.get("view") is not None
    switch(monkeypatch, False)  # the twin: today the parser's line stands when help says None
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "x?", bot=it.client, channel=GENERAL_CH, req=req))
    assert log[-1][1] == "parser's short line"


def test_a_change_verdict_still_shows_its_diff_with_the_switch_on(reg, monkeypatch):
    switch(monkeypatch, True)
    log = []
    asked, help_answer = recorder()
    it = interaction(OWNER, log, help_answer=help_answer)
    who = reg.test_members()[0].display_name
    req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="dm", member=who, value="off")])
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), f"{who}'s DMs off", bot=it.client, channel=GENERAL_CH, req=req))
    assert not asked and log[-1][2].get("embed") is not None, "the diff with Apply, as today"


# ---------------------------------------------------------------- /ask

def ask_tree(reg, bot):
    import discord
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guilds = types.SimpleNamespace(for_interaction=lambda i: reg)
    discord_help.register_help_commands(tree, guilds, None, bot)
    said = []

    async def plain_identity(reg_, user, **_):
        return user.id == OWNER, []

    async def send(content=None, **kw):
        said.append(content)

    async def defer(**kw):
        pass

    def dm(uid):
        return types.SimpleNamespace(guild_id=None, guild=None, channel_id=None, user=types.SimpleNamespace(id=uid), client=types.SimpleNamespace(plain_identity=plain_identity),
                                     response=types.SimpleNamespace(send_message=send, defer=defer), followup=types.SimpleNamespace(send=send))

    return {c.name: c for c in tree.get_commands()}["ask"], dm, said


@pytest.mark.parametrize("on", [True, False])
def test_ask_gains_the_recent_exchange(reg, monkeypatch, on):
    switch(monkeypatch, on)
    calls = []

    async def help_answer(*a, **k):
        calls.append(k)
        return "Which one: the Ironfeather Breastplate or the Ironfeather Leggings?" if len(calls) == 1 else "Thornwick makes it."

    ask, dm, said = ask_tree(reg, types.SimpleNamespace(help_answer=help_answer))
    asyncio.run(ask.callback(dm(OWNER), "who can make ironfeather?"))
    asyncio.run(ask.callback(dm(OWNER), "the breastplate"))
    assert said[-1] == "Thornwick makes it."
    if on:
        assert calls[0] == {"entry": "ask"}
        assert calls[1]["entry"] == "ask" and "who can make ironfeather?" in calls[1]["conversation"]
    else:
        assert calls == [{}, {}], "switch off: /ask is called exactly as today, no conversation"


# ---------------------------------------------------------------- the site's box

@pytest.mark.parametrize("on", [True, False])
def test_site_box_question_verdict_answers_from_the_loop(reg, rs, monkeypatch, on):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, bearer
    from test_parity_verbs import Bot

    from oibot_gm.web import app as web

    switch(monkeypatch, on)
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = Bot(reg, rs)
    bot.ctx = types.SimpleNamespace(provider=object())
    asked = []

    async def help_answer(reg_, uid, officer, text, **k):
        asked.append(k)
        return "the loop's answer"

    bot.help_answer = help_answer
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply=PARSER_SAYS, questions=["Which recipe?"]))
    c = TestClient(web.create_app(bot))
    d = c.post("/api/ops/change", json={"text": "who can make ironfeather breastplate"}, headers=bearer()).json()
    assert d["kind"] == "question" and not d["ops"] and "token" not in d
    if on:
        assert d["reply"] == "the loop's answer" and d["questions"] == []
        assert asked == [{"may_act": False, "public": False, "link": False, "entry": "site"}]
    else:
        assert d["reply"] == PARSER_SAYS and d["questions"] == ["Which recipe?"] and not asked


# ---------------------------------------------------------------- the loot thread

@pytest.mark.parametrize("on", [True, False])
def test_loot_thread_question_verdict_answers_from_the_loop(night, monkeypatch, on):
    from oibot_gm import nl
    from oibot_gm.discord_help import ACT

    switch(monkeypatch, on)
    n = night
    n.bot.loot_provider = object()
    tick(n)
    run(n.bot.distribute(n.reg, n.s, by="Mira", by_id=OFFICER))
    asked = []

    async def help_answer(reg_, uid, officer, text, may_act=False, public=False, data=None, **k):
        asked.append({"may_act": may_act, "data": data, **k})
        return ACT if len(asked) == 1 else "Because the table says so."

    n.bot.help_answer = help_answer
    monkeypatch.setattr(n.bot, "loot_ctx", lambda s: types.SimpleNamespace(provider=object(), policy_for_llm=lambda: "policy"))
    monkeypatch.setattr(nl, "parse_loot_feedback", lambda *a: nl.LootFeedback(kind="question", reply="the parser's line", changes=[]))
    n.reg.config.plain.channels[str(n.ch.id)] = "act"
    msg = LootMsg(n, "? why them")
    run(n.bot.loot_chat(msg, n.s))
    if on:
        assert [r[0] for r in msg.replies] == ["Because the table says so."]
        assert asked[1]["may_act"] is False and "Shard of Doom" in asked[1]["data"] and asked[1]["entry"] == "loot"
        assert len(asked) == 2
    else:
        assert [r[0] for r in msg.replies] == ["the parser's line"] and len(asked) == 1


# ---------------------------------------------------------------- the answers log names each entry

def test_entry_names_reach_the_answers_log(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    crafter(reg)
    no_parser(monkeypatch)
    provider = ScriptedProvider([[reply(answer="One.")], [reply(answer="Two.")], [reply(answer="Three.")]])
    bot = chat_bot(reg, rs, provider)
    msg, _ = message(WREN, guild=False)
    asyncio.run(bot.plain_text(msg, reg, "status?", None))
    msg, _ = message(WREN, mid=6000)
    asyncio.run(bot.plain_text(msg, reg, "status?", GENERAL_CH))
    ask, dm, said = ask_tree(reg, bot)
    asyncio.run(ask.callback(dm(OWNER), "status?"))
    assert said == ["Three."]
    assert [r["entry"] for r in rows()] == ["dm", "mention", "ask"]


# ---------------------------------------------------------------- a change missing a detail (Task 8 fix, the ruling)

OFF_PROMPT_SHA = "5f5aa459b84f6591b8c62d776a0a393455ab2496cbdc8da810898cc4ac983975"  # SYSTEM.format(schema=…); run_cancel's quiet line is on the sheet's thread; Task 11 added the spec op's from-spec and its never-ask-which-character line; its fix round, the kind=ignore sentence; issue 14, auto_lock / auto_lock_fill on schedule_set and run_reschedule; Task 3, run_lock value=fill ("lock and fill …"); Task 5, run_unlock and run_reopen told apart; Task 6, run_start apart from run_open and the loot council


class PromptSpy:
    """A provider whose complete() records the parser's system text and answers a fixed verdict."""

    def __init__(self, req):
        self.systems, self.req = [], req

    def complete(self, workload, system, user, schema):
        self.systems.append(system)
        return self.req


def test_the_parsers_prompt_gains_the_change_rule_only_with_the_switch_on(reg, monkeypatch):
    import hashlib

    spy = PromptSpy(ConfigRequest(kind="ignore", reply=""))
    switch(monkeypatch, False)
    configops.parse(spy, reg, "mark me away")
    off = spy.systems[-1]
    assert off == configops.SYSTEM.format(schema=configops.SCHEMA_TEXT), "switch off: today's prompt, byte for byte"
    assert hashlib.sha256(off.encode()).hexdigest() == OFF_PROMPT_SHA
    assert configops.CHANGE_NEEDS_DETAIL not in off
    switch(monkeypatch, True)
    configops.parse(spy, reg, "mark me away")
    on = spy.systems[-1]
    assert configops.CHANGE_NEEDS_DETAIL in on and "is ambiguous (which raid? which\nmember?), return kind=question" not in on
    monkeypatch.setenv(answer_switch.SWITCH_ENV, "someotherguild")
    configops.parse(spy, reg, "mark me away")
    assert spy.systems[-1] == off, "on for another guild only: this guild's prompt is today's"


def owner_bot(reg, rs, provider):
    real_member(reg, OWNER, "Oakhollow", "Mage", "Frost")
    return chat_bot(reg, rs, provider)


def test_mark_me_away_asks_for_the_dates_then_the_answer_makes_the_change(reg, rs, monkeypatch):
    """(a) The parser (switch on) says change, no ops, "Which dates?": the person reads the question, the ring keeps it,
    and the next message reaches the parser with the exchange and shows the diff with Apply. Nothing is written."""
    switch(monkeypatch, True)
    parsed = []
    verdicts = [ConfigRequest(kind="change", reply="", ops=[], questions=["Which dates?"]),
                ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="absence", start="2026-12-04", end="2026-12-06", reason="away")])]

    def parse(provider, reg_, text, by=None, officer=False, **kw):
        parsed.append((text, kw.get("conversation") or ""))
        return verdicts[len(parsed) - 1]

    monkeypatch.setattr(configops, "parse", parse)
    provider = ScriptedProvider([[reply(answer="Use I'll be away.", acts=True)], [reply(answer="Use I'll be away.", acts=True)]])
    bot = owner_bot(reg, rs, provider)
    head = reg.store.head()
    msg, said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH))
    assert len(said) == 1 and "Which dates?" in said[0][0] and "I'll be away" not in said[0][0]
    (ex,) = conversation.recall(reg, GENERAL_CH, OWNER)
    assert ex.questions == ["Which dates?"]
    msg2, said2 = message(OWNER, mid=7000)
    asyncio.run(bot.plain_text(msg2, reg, "friday to sunday", GENERAL_CH))
    assert parsed[1][0] == "friday to sunday"
    assert "mark me away" in parsed[1][1] and "Which dates?" in parsed[1][1], "the parser reads the exchange"
    assert said2[-1][1].get("embed") is not None and said2[-1][1].get("view") is not None, "the diff with Apply"
    assert not reg.members[OWNER].absences, "nothing is written before Apply"
    assert reg.store.head() == head


def test_a_parser_that_still_says_question_for_a_change_gets_the_loops_question(reg, rs, monkeypatch):
    """(b) The safety net: the parser answers kind=question (with its own question) for a change; the loop is asked
    again with acting off, its system text carries the acting-off rule, the person reads the loop's words, and nothing
    is written."""
    from oibot_gm import answer_start

    switch(monkeypatch, True)
    parsed = fake_parser(monkeypatch, ConfigRequest(kind="question", reply="the parser's line", questions=["Which dates?"]))
    loop_says = "Which dates will you be away? Tell me the first and last day."
    provider = ScriptedProvider([[reply(answer="Marking you away.", acts=True)], [reply(answer=loop_says, acts=True)]])
    bot = owner_bot(reg, rs, provider)
    msg, said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH))
    assert [c for c, _ in said] == [loop_says]
    assert parsed == ["mark me away"] and provider.n == 2
    assert answer_start.CANNOT_ACT in provider.requests[-1]["system"]
    assert not reg.members[OWNER].absences


def test_site_box_a_change_missing_a_detail_keeps_its_question_and_its_follow_up(reg, rs, monkeypatch):
    """The site's box with the switch on: change, no ops, "Which dates?" → the questions come back (the box shows them
    and keeps them in its own exchange), no loop, no token; the follow-up sends that exchange to the parser."""
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, bearer
    from test_parity_verbs import Bot

    from oibot_gm.web import app as web

    switch(monkeypatch, True)
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = Bot(reg, rs)
    bot.ctx = types.SimpleNamespace(provider=object())
    asked = []

    async def help_answer(*a, **k):
        asked.append(k)
        return "the loop's answer"

    bot.help_answer = help_answer
    seen = []

    def parse(provider, reg_, text, by=None, officer=False, **kw):
        seen.append(kw.get("conversation") or "")
        return ConfigRequest(kind="change", reply="", ops=[], questions=["Which dates?"])

    monkeypatch.setattr(configops, "parse", parse)
    c = TestClient(web.create_app(bot))
    d = c.post("/api/ops/change", json={"text": "mark me away"}, headers=bearer()).json()
    assert d["kind"] == "change" and d["questions"] == ["Which dates?"] and "token" not in d and not asked
    turn = [{"request": "mark me away", "reply": d["reply"], "questions": d["questions"]}]
    c.post("/api/ops/change", json={"text": "friday to sunday", "conversation": turn}, headers=bearer())
    assert "mark me away" in seen[-1] and "Which dates?" in seen[-1]


# ---------------------------------------------------------------- Task 8 fix 2

def first_user_text(request) -> str:
    content = request["messages"][0]["content"]
    return content if isinstance(content, str) else json.dumps(content)


def test_the_reask_is_told_acting_is_off_and_a_reply_that_still_acts_gets_its_words_alone(reg, rs, monkeypatch):
    """(1) The re-ask's first user text says nothing can be changed from this message (the first answer's doesn't),
    the system text is the same string, and a re-ask that still sets `acts` with words is posted as those words alone
    (no commands, page or officer line appended) and counted in the answers log as ending acts_off."""
    from oibot_gm import answer_start

    switch(monkeypatch, True)
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply="the parser's line"))
    says = "I've marked you away."
    provider = ScriptedProvider([[reply(answer="Marking you away.", acts=True)],
                                 [reply(answer=says, acts=True, commands=["/me absent add"], for_officer=True)]])
    bot = owner_bot(reg, rs, provider)
    msg, said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH))
    assert [c for c, _ in said] == [says], "the words alone: nothing appended"
    first, second = provider.requests
    assert answer_start.ACTING_OFF not in first_user_text(first) and answer_start.ACTING_OFF in first_user_text(second)
    assert first["system"] == second["system"] and answer_start.ACTING_OFF not in first["system"]
    assert [r["ending"] for r in rows()] == ["acts", "acts_off"]
    assert not reg.members[OWNER].absences


def test_a_reask_that_still_acts_with_no_words_gets_the_fixed_sentence(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply="the parser's line"))
    provider = ScriptedProvider([[reply(answer="Marking you away.", acts=True)], [reply(answer="", acts=True, commands=["/me absent add"])]])
    msg, said = message(OWNER)
    asyncio.run(owner_bot(reg, rs, provider).plain_text(msg, reg, "mark me away", GENERAL_CH))
    assert [c for c, _ in said] == [discord_help.ACTS_OFF_EMPTY]


def test_switch_off_a_help_answer_with_acting_off_appends_as_today(reg, rs, monkeypatch):
    """The twin: switch off, help.answer's `acts` with acting off is ignored and its commands are appended as today."""
    from oibot_gm import help as help_mod

    switch(monkeypatch, False)
    bot = make_bot(reg, rs, ScriptedProvider([]))
    monkeypatch.setattr(help_mod, "answer", lambda *a, **k: help_mod.HelpAnswer(answer="Use the command.", acts=True, commands=["/me absent add"]))
    assert asyncio.run(bot.help_answer(reg, OWNER, True, "mark me away")) == "Use the command.\n\n`/me absent add`"


@pytest.mark.parametrize("on", [True, False])
def test_a_change_with_no_ops_and_no_questions_goes_to_the_loop(reg, monkeypatch, on):
    """(2) kind=change with nothing in it: the parser's own words never reach a person alone (switch on); today's
    line with the switch off."""
    switch(monkeypatch, on)
    log = []
    asked, help_answer = recorder()
    it = interaction(OWNER, log, help_answer=help_answer)
    req = ConfigRequest(kind="change", reply="Done, you're away.", ops=[], questions=[])
    asyncio.run(discord_policy.handle_change(it, reg, None, None, Feed(), "mark me away", bot=it.client, channel=GENERAL_CH, req=req))
    if on:
        assert log[-1][1] == "the loop's answer" and [a["may_act"] for a in asked] == [False] and asked[0]["entry"] == "change"
    else:
        assert not asked and log[-1][1] == "Done, you're away."


def site_bot(reg, rs, monkeypatch, req):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, bearer
    from test_parity_verbs import Bot

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = Bot(reg, rs)
    bot.ctx = types.SimpleNamespace(provider=object())
    asked = []

    async def help_answer(reg_, uid, officer, text, **k):
        asked.append(k)
        return "the loop's answer"

    bot.help_answer = help_answer
    fake_parser(monkeypatch, req)
    c = TestClient(web.create_app(bot))
    return (lambda text: c.post("/api/ops/change", json={"text": text}, headers=bearer()).json()), asked


@pytest.mark.parametrize("on", [True, False])
def test_site_box_a_change_with_nothing_in_it_answers_from_the_loop(reg, rs, monkeypatch, on):
    switch(monkeypatch, on)
    post, asked = site_bot(reg, rs, monkeypatch, ConfigRequest(kind="change", reply="Done, you're away.", ops=[], questions=[]))
    d = post("mark me away")
    assert "token" not in d and not d["ops"]
    if on:
        assert d["reply"] == "the loop's answer" and asked == [{"may_act": False, "public": False, "link": False, "entry": "site"}]
    else:
        assert d["reply"] == "Done, you're away." and not asked


def test_site_box_a_refused_change_keeps_its_refusals(reg, rs, monkeypatch):
    """A change whose ops were all refused is not a bare change: its refusals stand, no loop."""
    switch(monkeypatch, True)
    req = ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="job", target="review")])
    post, asked = site_bot(reg, rs, monkeypatch, req)
    monkeypatch.setattr(configops, "authorize", lambda *a, **k: ([], ["Only the owner can run a job."]))
    d = post("run the review")
    assert d["refused"] == ["Only the owner can run a job."] and not asked and d["reply"] == ""


def held_bot(reg, rs, provider):
    bot = owner_bot(reg, rs, provider)
    held = []

    async def hold(req):
        held.append(req)

    return bot, held, hold


@pytest.mark.parametrize("verdict", [ConfigRequest(kind="change", reply="", ops=[], questions=["Which dates?"]),
                                     ConfigRequest(kind="change", reply="Sure.", ops=[], questions=[]),
                                     ConfigRequest(kind="change", reply="", ops=[ConfigOp(op="absence", start="2026-12-04", end="2026-12-06")])],
                         ids=["questions-only", "nothing", "with-ops"])
def test_an_overheard_change_is_held_whatever_it_carries(reg, rs, monkeypatch, verdict):
    """(3) Listening, switch on: every change verdict goes to `hold` (👀 only) — nothing is posted in the channel."""
    switch(monkeypatch, True)
    fake_parser(monkeypatch, verdict)
    bot, held, hold = held_bot(reg, rs, ScriptedProvider([[reply(answer="Marking you away.", acts=True)]]))
    msg, said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH, hold=hold, entry="listen"))
    assert said == [] and held == [verdict]
    assert not conversation.recall(reg, GENERAL_CH, OWNER), "nothing said, nothing kept"


def test_an_overheard_question_verdict_is_answered_by_the_loop(reg, rs, monkeypatch):
    """(3, 9) Listening, switch on: the parser's question verdict is answered by the loop, as an overheard question is."""
    switch(monkeypatch, True)
    crafter(reg)
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply=PARSER_SAYS, questions=["Which recipe?"]))
    bot, held, hold = held_bot(reg, rs, ScriptedProvider([[reply(answer="Changing it.", acts=True)], [reply(answer=LOOP_SAYS)]]))
    msg, said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "who can make ironfeather breastplate", GENERAL_CH, hold=hold, entry="listen"))
    assert [c for c, _ in said] == [LOOP_SAYS] and held == []
    assert [r["entry"] for r in rows()] == ["listen", "listen"]


def test_switch_off_an_overheard_change_asking_a_detail_posts_its_question_as_today(reg, rs, monkeypatch):
    switch(monkeypatch, False)
    fake_parser(monkeypatch, ConfigRequest(kind="change", reply="", ops=[], questions=["Which dates?"]))
    bot, held, hold = held_bot(reg, rs, ScriptedProvider([]))

    async def old_help(*a, **k):
        return discord_help.ACT

    bot.help_answer = old_help
    msg, said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH, hold=hold))
    assert [c for c, _ in said] == ["\n• Which dates?"] and held == []


def test_an_overheard_change_from_someone_outside_the_ask_audience_is_held(reg, rs, monkeypatch):
    """plain_text's own parse (may act, may not ask): a change verdict asking a detail is held, not answered with the guide."""
    switch(monkeypatch, True)
    member = reg.test_members()[0]
    reg.config.ask_audience = "confirmed"
    reg.config.plain.channels[str(GENERAL_CH)] = "self"
    monkeypatch.setattr(type(reg), "may_ask", lambda self, uid, officer: False)
    monkeypatch.setattr(type(reg), "may_plain", lambda self, *a, **k: None)
    verdict = ConfigRequest(kind="change", reply="", ops=[], questions=["Which dates?"])
    fake_parser(monkeypatch, verdict)
    bot, held, hold = held_bot(reg, rs, ScriptedProvider([]))
    msg, said = message(member.discord_id)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH, hold=hold, entry="listen"))
    assert said == [] and held == [verdict]
    switch(monkeypatch, False)  # the twin: today the guide is posted
    msg, said = message(member.discord_id, mid=8000)
    asyncio.run(bot.plain_text(msg, reg, "mark me away", GENERAL_CH, hold=hold))
    assert [c for c, _ in said] == [discord_help.guide_intro(reg)] and held == [verdict]


def test_a_listened_message_in_an_act_channel_is_logged_as_listen(reg, monkeypatch):
    """(8) plain_text's act branch passes the entry through: an overheard question there is answered as `listen`."""
    switch(monkeypatch, True)
    reg.config.plain.channels[str(ACT_CH)] = "act"
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply="Which event?"))

    async def plain_identity(reg_, user, **_):
        return True, []

    asked, help_answer = recorder()
    bot = types.SimpleNamespace(ctx=types.SimpleNamespace(provider=object()), policies=types.SimpleNamespace(store=lambda r: None), ops=Feed(),
                                plain_identity=plain_identity, help_answer=help_answer, raids=None, reply_private=lambda r, ch: False)

    async def hold(req):
        raise AssertionError("a question is never held")

    msg, said = message(OWNER, cid=ACT_CH)
    asyncio.run(db.OibotGM.plain_text(bot, msg, reg, "who is rostered?", ACT_CH, hold=hold, entry="listen"))
    assert [c for c, _ in said] == ["the loop's answer"] and asked[0]["entry"] == "listen"
    msg, said = message(OWNER, cid=ACT_CH, mid=9000)  # an @mention there still names the act channel
    asyncio.run(db.OibotGM.plain_text(bot, msg, reg, "who is rostered?", ACT_CH))
    assert asked[1]["entry"] == "act"


def test_the_reask_carries_the_judged_memory(reg, rs, monkeypatch):
    """(5) The memory block the first answer had is in the re-ask's user text too."""
    from oibot_gm import memory as mem

    switch(monkeypatch, True)
    fake_parser(monkeypatch, ConfigRequest(kind="question", reply="the parser's line"))
    block = "<memory>\nMira said the raid moved to Thursday.\n</memory>"
    monkeypatch.setattr(mem, "memory_block", lambda cands, tz=None: block if cands else "")
    provider = ScriptedProvider([[reply(answer="Moving it.", acts=True)], [reply(answer="It moved to Thursday.")]])
    msg, said = message(OWNER)
    asyncio.run(owner_bot(reg, rs, provider).plain_text(msg, reg, "move the raid", GENERAL_CH, memory=[object()]))
    assert [c for c, _ in said] == ["It moved to Thursday."]
    assert all("Mira said the raid moved" in first_user_text(r) for r in provider.requests)


def test_the_guide_reply_carries_apply_to_join_for_a_newcomer(reg, monkeypatch):
    """(7) handle_change's guide (switch on, outside the ask audience) offers Apply to join, as plain_text's does."""
    switch(monkeypatch, True)
    log = []
    _asked, help_answer = recorder(answer=None)

    async def go():
        it = interaction(42, log, help_answer=help_answer)
        req = ConfigRequest(kind="question", reply="parser's short line")
        await discord_policy.handle_change(it, reg, None, None, Feed(), "x?", bot=it.client, channel=GENERAL_CH, req=req)

    asyncio.run(go())
    _kind, content, kw = log[-1]
    assert content == discord_help.guide_intro(reg)
    assert [getattr(c, "custom_id", None) for c in kw["view"].children] == [f"guide.{reg.key}:menu", f"reg.{reg.key}:apply"]


# ---------------------------------------------------------------- /ask keeps its answer private (4, 9)

PRIVATE_CH, OTHER_CH = 920_101, 920_102


def guild_ask(reg, bot, monkeypatch):
    from discord import app_commands

    async def identity(interaction, reg_):
        return interaction.user.id == OWNER, []

    monkeypatch.setattr(discord_help, "asker_identity", identity)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    discord_help.register_help_commands(tree, types.SimpleNamespace(for_interaction=lambda i: reg), None, bot)
    said = []

    async def send(content=None, **kw):
        said.append(content)

    async def defer(**kw):
        pass

    def at(uid, cid):
        return types.SimpleNamespace(guild_id=1, guild=types.SimpleNamespace(id=1), channel_id=cid, channel=types.SimpleNamespace(id=cid, guild=object()),
                                     user=types.SimpleNamespace(id=uid, display_name="Oak"), client=bot,
                                     response=types.SimpleNamespace(send_message=send, defer=defer), followup=types.SimpleNamespace(send=send))

    return {c.name: c for c in tree.get_commands()}["ask"], at, said


def test_ask_where_others_read_recalls_but_never_remembers_its_answer(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    secret = "Your absence reason is the dentist."
    provider = ScriptedProvider([[reply(answer=secret)], [reply(answer="Next event is Friday.")], [reply(answer="Asked back.")]])
    bot = owner_bot(reg, rs, provider)
    ask, at, said = guild_ask(reg, bot, monkeypatch)
    asyncio.run(ask.callback(at(OWNER, GENERAL_CH), "why am I away?"))
    assert said == [secret]
    assert not conversation.recall(reg, GENERAL_CH, OWNER), "a channel others read: the /ask answer stays out of its ring"
    msg, _said = message(OWNER)
    asyncio.run(bot.plain_text(msg, reg, "when is the next event?", GENERAL_CH))
    assert "dentist" not in first_user_text(provider.requests[1]) and "why am I away" not in first_user_text(provider.requests[1])
    # it still recalls: the public exchange just made reaches a following /ask there
    asyncio.run(ask.callback(at(OWNER, GENERAL_CH), "and after that?"))
    assert "when is the next event?" in first_user_text(provider.requests[2])
    assert [ex.request for ex in conversation.recall(reg, GENERAL_CH, OWNER)] == ["when is the next event?"]


def test_ask_where_replies_are_private_remembers_and_never_feeds_another_channel(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    provider = ScriptedProvider([[reply(answer="Your reason is the dentist.")], [reply(answer="Friday.")]])
    bot = owner_bot(reg, rs, provider)
    bot.reply_private = lambda reg_, ch: ch is None or getattr(ch, "id", None) == PRIVATE_CH
    ask, at, said = guild_ask(reg, bot, monkeypatch)
    asyncio.run(ask.callback(at(OWNER, PRIVATE_CH), "why am I away?"))
    assert [ex.request for ex in conversation.recall(reg, PRIVATE_CH, OWNER)] == ["why am I away?"]
    msg, _said = message(OWNER, cid=OTHER_CH)
    asyncio.run(bot.plain_text(msg, reg, "when is the next event?", OTHER_CH))
    assert "dentist" not in first_user_text(provider.requests[1]), "channel A never feeds channel B"
    assert not any(ex.request == "why am I away?" for ex in conversation.recall(reg, OTHER_CH, OWNER))


def test_ask_in_a_dm_remembers(reg, rs, monkeypatch):
    switch(monkeypatch, True)
    bot = owner_bot(reg, rs, ScriptedProvider([[reply(answer="Friday.")]]))
    ask, dm, said = ask_tree(reg, bot)
    asyncio.run(ask.callback(dm(OWNER), "when is the next event?"))
    assert said == ["Friday."] and [ex.request for ex in conversation.recall(reg, None, OWNER)] == ["when is the next event?"]
