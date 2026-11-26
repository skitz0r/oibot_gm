"""The answer tools' switch (design §9, Task 7): off by default and for any guild it doesn't name, help_answer runs
help.answer exactly as before; on for a guild, the lookup loop answers and help_answer's post-processing is the same
code. One row per loop answer in out/answers.jsonl — never the question, the answer or a name."""
from __future__ import annotations

import asyncio
import json
import re
import types
from pathlib import Path

import pytest

from answer_fakes import ScriptedProvider, reply
from conftest import OWNER
from test_help import FakeClient, real_member

from oibot_gm import answer_loop, answer_switch, discord_help, help as help_mod
from oibot_gm.answer_loop import ANSWER_FAILED, AnswerResult
from oibot_gm.help import HelpAnswer
from oibot_gm.llm.provider import BudgetExceeded

SRC = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
MEMBER = 700_000_000_000_000_011
NEWCOMER = 700_000_000_000_000_012
ROW_KEYS = {"at", "guild", "entry", "rounds", "requests", "tools", "ending", "input", "output", "cache_read", "cache_write", "cost"}


def make_bot(reg, rs, provider, cached=None):
    guild = types.SimpleNamespace(id=1, me=object(), channels=[], get_member=lambda uid: (cached or {}).get(uid), members=[])
    bot = FakeClient(reg, rs, guild)
    bot.ctx = types.SimpleNamespace(provider=provider)
    warns: list[str] = []

    async def emit(cfg, level, line):
        warns.append(line)

    bot.ops = types.SimpleNamespace(recent_for=lambda key: [], emit=emit)
    bot.warns = warns
    return bot


def ask(bot, reg, uid, officer, question, **kw):
    return asyncio.run(bot.help_answer(reg, uid, officer, question, **kw))


def rows(path=None) -> list[dict]:
    p = path or answer_switch.answers_log()
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def test_enabled_reads_off_on_and_a_list(monkeypatch, caplog):
    assert not answer_loop.enabled("demo"), "unset: off"
    for off in ("", "off", "OFF", "  "):
        monkeypatch.setenv("OIBOT_ANSWER_TOOLS", off)
        assert not answer_loop.enabled("demo")
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    assert answer_loop.enabled("demo") and answer_loop.enabled("other")
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "demo, duo")
    assert answer_loop.enabled("demo") and answer_loop.enabled("duo") and not answer_loop.enabled("other")
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "demo duo")
    assert answer_loop.enabled("duo")
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "demo;<on>")
    caplog.set_level("WARNING")
    assert not answer_loop.enabled("demo") and not answer_loop.enabled("demo")
    assert sum("reads as off" in r.getMessage() for r in caplog.records) == 1, "a malformed value is logged once"
    assert answer_loop.entry_kw("demo", "site") == {}, "off: the callers pass nothing new"
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "demo")
    assert answer_loop.entry_kw("demo", "site") == {"entry": "site"} and answer_loop.entry_kw("other", "site") == {}


def test_switch_off_keeps_the_old_path(reg, rs, monkeypatch):
    got = []

    def old(provider, tree, reg_, rs_, uid, officer, question, extras=None, public=False, **kw):
        got.append(question)
        return HelpAnswer(answer="old path")

    def loop(*a, **k):
        raise AssertionError("the loop never runs while the switch is off")

    monkeypatch.setattr(help_mod, "answer", old)
    monkeypatch.setattr(answer_loop, "answer", loop)
    bot = make_bot(reg, rs, ScriptedProvider([]))
    assert ask(bot, reg, OWNER, True, "status?") == "old path"
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "other, guilds")  # on for other guilds in the same process, not this one
    assert ask(bot, reg, OWNER, True, "status?", entry="mention") == "old path"
    assert got == ["status?", "status?"] and rows() == [], "no answers log row from the old path"


def test_switch_on_runs_the_loop_for_this_guild(reg, rs, monkeypatch):
    seen = []

    def old(*a, **k):
        raise AssertionError("help.answer never runs while the switch is on for this guild")

    def loop(provider, tree, viewer, *, conversation=None, memory=None):
        seen.append((viewer.reg.key, conversation, memory))
        return AnswerResult(HelpAnswer(answer="loop path"), "answered", 0, 1, [], {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0, "cost": 0.0})

    monkeypatch.setattr(help_mod, "answer", old)
    monkeypatch.setattr(answer_loop, "answer", loop)
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", f"other {reg.key}")
    bot = make_bot(reg, rs, ScriptedProvider([]))
    assert ask(bot, reg, OWNER, True, "status?", conversation="<conversation>\nq\n</conversation>", memory="<memory>\nm\n</memory>") == "loop path"
    assert seen == [(reg.key, "<conversation>\nq\n</conversation>", "<memory>\nm\n</memory>")], "conversation and memory pass through"


def test_switch_on_runs_the_loop_and_keeps_the_post_processing(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    step = [reply(answer="Use the wishlist command.", commands=["/me wishlist"], page="professions", for_officer=True)]
    bot = make_bot(reg, rs, ScriptedProvider([step]))
    text = ask(bot, reg, OWNER, True, "how do I add to my wishlist?")
    assert text.startswith("Use the wishlist command.")
    assert "\n\n`/me wishlist`" in text
    assert f"-# See it all on the [Professions page](<https://gm.example/app/professions?g={reg.key}>)." in text
    assert text.endswith("\n-# This needs an officer.")
    bot = make_bot(reg, rs, ScriptedProvider([step]))
    text = ask(bot, reg, OWNER, True, "how do I add to my wishlist?", link=False, entry="site")
    assert "Professions page" not in text and "`/me wishlist`" in text, "the site's box: no link"


def test_switch_on_acts_returns_act_only_when_may_act(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    step = [reply(answer="Use /me absent add.", acts=True)]
    assert ask(make_bot(reg, rs, ScriptedProvider([step])), reg, OWNER, True, "I'll be away Friday", may_act=True) == discord_help.ACT
    text = ask(make_bot(reg, rs, ScriptedProvider([step])), reg, OWNER, True, "I'll be away Friday", may_act=False)
    assert text == "Use /me absent add."


def test_switch_on_officer_in_public_gets_the_member_viewer_and_the_note(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    viewers = []
    real = answer_loop.answer

    def spy(provider, tree, viewer, **kw):
        viewers.append(viewer)
        return real(provider, tree, viewer, **kw)

    monkeypatch.setattr(answer_loop, "answer", spy)
    step = [reply(answer="Members see the member view.", officer_view=True)]
    bot = make_bot(reg, rs, ScriptedProvider([step]))
    text = ask(bot, reg, OWNER, True, "who has unconfirmed characters?", public=True, data="<loot council data>")
    v = viewers[-1]
    assert v.officer is False and v.public is True and v.audience_public is True
    assert v.data is None and (v.extras is None or v.extras.data is None), "the loot council's data never reaches a public reply"
    assert text.startswith("Members see the member view.") and text.endswith(bot.private_note(reg))
    # the same answer read privately: the officer viewer, the data, no note
    bot = make_bot(reg, rs, ScriptedProvider([step]))
    text = ask(bot, reg, OWNER, True, "who has unconfirmed characters?", data="<loot council data>")
    assert viewers[-1].officer is True and viewers[-1].data == "<loot council data>"
    assert text == "Members see the member view."


def test_a_member_in_public_reads_as_audience_public(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    real_member(reg, MEMBER, "Wrenfield")
    viewers = []
    real = answer_loop.answer
    monkeypatch.setattr(answer_loop, "answer", lambda p, t, v, **kw: (viewers.append(v), real(p, t, v, **kw))[1])
    ask(make_bot(reg, rs, ScriptedProvider([[reply(answer="ok")]])), reg, MEMBER, False, "hello", public=True)
    v = viewers[-1]
    assert (v.officer, v.public, v.audience_public) == (False, False, True), "help_answer's two meanings of public, as today"


def test_an_asker_without_a_record_is_named_as_discord_shows_them(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    provider = ScriptedProvider([[reply(answer="Welcome.")]])
    bot = make_bot(reg, rs, provider, cached={NEWCOMER: types.SimpleNamespace(display_name="Newface")})
    reg.config.ask_audience = "everyone"
    assert ask(bot, reg, NEWCOMER, False, "how do I join?") == "Welcome."
    first = provider.requests[0]["messages"][0]["content"][0]["text"]
    assert "Asker: Newface · " in first and "someone" not in first.split("## Live events")[0]


def test_a_provider_without_converse_uses_the_old_path(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    monkeypatch.setattr(help_mod, "answer", lambda *a, **k: HelpAnswer(answer="old path"))
    monkeypatch.setattr(answer_loop, "answer", lambda *a, **k: pytest.fail("no converse, no loop"))
    bot = make_bot(reg, rs, types.SimpleNamespace(complete=lambda *a, **k: None))
    assert ask(bot, reg, OWNER, True, "status?") == "old path"


def test_budget_on_the_loop_is_its_own_sentence(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    sentence = "This month's spend on answers is used up, so questions wait until the 1st."
    bot = make_bot(reg, rs, ScriptedProvider([BudgetExceeded(sentence)]))
    assert ask(bot, reg, OWNER, True, "who makes Ironfeather Breastplate?") == sentence
    assert bot.warns == ["help answer failed: BudgetExceeded"]


def test_an_error_ending_warns_as_today_and_says_its_sentence(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    bot = make_bot(reg, rs, ScriptedProvider([RuntimeError("secret detail about the question")]))
    text = ask(bot, reg, OWNER, True, "status?")
    assert text.startswith(ANSWER_FAILED)
    assert bot.warns == ["help answer failed: RuntimeError"], "the type only: never the question or the exception's words"


def test_one_row_per_answer_without_text(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    real_member(reg, MEMBER, "Wrenfield")
    q1, a1 = "who has Zorblaxquestion gear?", "Wrenfield has Quuxanswer gear."
    provider = ScriptedProvider([[{"name": "find", "input": {"text": "Zorblaxquestion"}}], [reply(answer=a1)],
                                 [reply(answer="Second Quuxanswer.")]])
    bot = make_bot(reg, rs, provider)
    ask(bot, reg, MEMBER, False, q1, entry="mention")
    ask(bot, reg, MEMBER, False, "and the other Zorblaxquestion?", public=True, entry="dm")
    got = rows()
    assert len(got) == 2 and all(set(r) == ROW_KEYS for r in got)
    assert [r["entry"] for r in got] == ["mention", "dm"] and {r["guild"] for r in got} == {reg.key}
    assert got[0]["tools"] == ["find"] and got[0]["rounds"] == 1 and got[0]["requests"] == 2 and got[0]["ending"] == "answered"
    assert got[0]["input"] == 200 and got[0]["output"] == 40 and got[0]["cost"] == pytest.approx(0.002)
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$", got[0]["at"])
    raw = answer_switch.answers_log().read_text()
    for word in ("Zorblaxquestion", "Quuxanswer", "Wrenfield", "who has"):
        assert word not in raw


def test_a_made_up_lookup_name_is_logged_as_unknown(reg, rs):
    res = AnswerResult(HelpAnswer(answer="x"), "answered", 1, 2, ["find", "who is Wrenfield"], {})
    answer_loop.log_answer(reg.key, "ask", res)
    assert rows()[-1]["tools"] == ["find", "unknown"]


def test_the_log_rotates_past_its_size(reg, monkeypatch):
    path = answer_switch.answers_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / (path.name + ".1")).write_text("older\n")
    path.write_text("x" * 200 + "\n")
    monkeypatch.setattr(answer_switch, "ANSWERS_MAX_BYTES", 100)
    answer_loop.log_answer(reg.key, "ask", AnswerResult(HelpAnswer(answer="x"), "answered", 0, 1, [], {}))
    assert (path.parent / (path.name + ".1")).read_text() == "x" * 200 + "\n", "the full file replaced the older .1"
    assert len(rows(path)) == 1


def test_a_failed_log_write_never_fails_the_answer(reg, rs, monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    monkeypatch.setenv("OIBOT_ANSWERS_LOG", str(blocker / "answers.jsonl"))
    monkeypatch.setenv("OIBOT_ANSWER_TOOLS", "on")
    caplog.set_level("WARNING")
    assert ask(make_bot(reg, rs, ScriptedProvider([[reply(answer="Still answered.")]])), reg, OWNER, True, "status?") == "Still answered."
    assert any("answers log not written" in r.getMessage() for r in caplog.records)


def test_every_caller_names_an_entry_point_from_the_list():
    """Each help_answer caller passes its entry through entry_kw (nothing new while the switch is off); /ask is the default."""
    found = {}
    for f in ("discord_bot.py", "discord_policy.py", "discord_loot_surface.py", "web/api.py"):
        for e in re.findall(r'entry_kw\([^,]+, ([^)]*)\)', (SRC / f).read_text()):
            found.setdefault(f, []).append(e)
    names = set(re.findall(r'"(\w+)"', " ".join(sum(found.values(), []))))
    assert names <= set(answer_loop.ENTRIES)
    assert names | {"ask"} == set(answer_loop.ENTRIES), names
