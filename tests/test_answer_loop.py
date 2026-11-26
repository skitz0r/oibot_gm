"""The answer loop (answer tools, design §6): every limit enforced in code, the `reply` ending and its checks, the
honest endings, and the three questions of §1 end to end — all with the scripted model, never a real one."""
import json

import pytest

from answer_bench import BENCH, MARROW, WREN, askers, asker_of, crafters
from answer_fakes import USAGE, ScriptedProvider, call, reply

from oibot_gm import answer_loop as al
from oibot_gm import answer_tools as at
from oibot_gm.llm.provider import BudgetExceeded, CutShort, Refused, ToolCall, Turn

SYSTEM = "You answer questions about the guild."


def member(reg, rs, question="who can make ironfeather?", public=False):
    crafters(reg, rs)
    return at.Viewer.of(reg, rs, WREN, False, public=public, question=question)


def officer(reg, rs, question="what's up?"):
    askers(reg)
    return at.Viewer.of(reg, rs, MARROW, True, public=False, question=question)


def results_of(message):
    return [b for b in message["content"] if b.get("type") == "tool_result"]


def stub(monkeypatch, text, cap=at.RESULT_CAP, name="stub"):
    monkeypatch.setitem(at.TOOLS, name, at.Tool(name, "A stand-in lookup for a test.", at._schema({}), lambda v, a: text, cap))


# ---------------------------------------------------------------- rounds and requests

def test_rounds_cap_forces_reply_after_three_lookup_rounds(reg, rs):
    pv = ScriptedProvider([[call("find", text="ironfeather")]] * 3 + [[reply(answer="Thornwick makes it.")]])
    res = al.run(pv, SYSTEM, "who makes ironfeather?", member(reg, rs))
    assert len(pv.requests) == 4 and res.requests == 4 and res.rounds == 3
    assert [r["force"] for r in pv.requests] == [None, None, None, "reply"]
    assert pv.requests[3]["messages"][-1]["content"][-1] == {"type": "text", "text": al.FINAL_NUDGE}
    assert all(al.FINAL_NUDGE not in json.dumps(r["messages"]) for r in pv.requests[:3])
    assert res.ending == "answered" and res.answer.answer == "Thornwick makes it."
    assert res.tools == ["find"] * 3


def test_a_lookup_after_the_last_round_ends_with_the_honest_sentence(reg, rs):
    pv = ScriptedProvider([[call("find", text="ironfeather")]] * 4)
    res = al.run(pv, SYSTEM, "who makes ironfeather?", member(reg, rs))
    assert res.ending == "cap" and res.requests == 4 and res.answer.answer == al.ANSWER_NOT_FOUND


def test_requests_cap_is_a_hard_stop(reg, rs):
    pv = ScriptedProvider([[call("find", text="ironfeather")]] * 10)
    res = al.run(pv, SYSTEM, "who makes ironfeather?", member(reg, rs))
    assert len(pv.requests) == al.MAX_REQUESTS == pv.n and res.requests == al.MAX_REQUESTS


def test_the_tool_list_and_system_never_change_within_an_answer(reg, rs):
    pv = ScriptedProvider([[call("find", text="ironfeather")], [call("recipe", name="Ironfeather Breastplate")], [reply(answer="Thornwick.")]])
    al.run(pv, SYSTEM, "who makes ironfeather?", member(reg, rs))
    assert all(r["tools"] == pv.requests[0]["tools"] and r["system"] == SYSTEM for r in pv.requests)
    assert pv.requests[0]["tools"][-1]["name"] == al.REPLY
    assert [t["name"] for t in pv.requests[0]["tools"][:-1]] == [t.name for t in at.tools_for(reg.profile)]


def test_calls_per_round_cap_answers_the_fifth_with_too_many(reg, rs):
    pv = ScriptedProvider([[call("find", text=f"ironfeather {i}") for i in range(5)], [reply(answer="Thornwick.")]])
    res = al.run(pv, SYSTEM, "who makes ironfeather?", member(reg, rs))
    msgs = pv.requests[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    got = results_of(msgs[2])
    assert len(got) == 5 and len(msgs[2]["content"]) == 5  # all five in one message, nothing else in it
    assert [b["tool_use_id"] for b in got] == [f"call-1-{i}" for i in range(5)]
    assert all(b["content"].startswith('<data source="find">') and b["is_error"] is False for b in got[:4])
    assert got[4]["content"] == al.TOO_MANY_LOOKUPS and got[4]["is_error"] is True
    assert res.ending == "answered" and res.rounds == 1


# ---------------------------------------------------------------- what a result may hold

def test_a_long_result_is_cut_on_a_whole_line(reg, rs, monkeypatch):
    stub(monkeypatch, "\n".join(f"line {i:03d} " + "x" * 40 for i in range(100)))  # about 5,000 characters
    pv = ScriptedProvider([[call("stub")], [reply(answer="Done.")]])
    al.run(pv, SYSTEM, "q", member(reg, rs))
    text = results_of(pv.requests[1]["messages"][-1])[0]["content"]
    wrapper = len('<data source="stub">\n') + len("\n</data>")
    assert len(text) <= at.RESULT_CAP + wrapper
    inner = text.split("\n")[1:-1]
    assert inner[-1].endswith("— narrow it")
    assert all(ln.startswith("line ") and len(ln) == len("line 000 ") + 40 for ln in inner[:-1])  # whole lines only


def test_total_results_cap_refuses_further_results(reg, rs, monkeypatch):
    stub(monkeypatch, "\n".join("y" * 77 for _ in range(50)), cap=at.BIG_RESULT_CAP)  # 3,899 characters a result
    pv = ScriptedProvider([[call("stub")] * 4, [reply(answer="Done.")]])
    res = al.run(pv, SYSTEM, "q", member(reg, rs))
    got = results_of(pv.requests[1]["messages"][-1])
    assert [b["content"] == al.RESULTS_FULL for b in got] == [False, False, False, True]
    assert sum(len(b["content"]) for b in got[:3]) <= al.TOTAL_RESULT_CHARS
    assert pv.requests[1]["force"] == "reply" and pv.requests[1]["messages"][-1]["content"][-1]["text"] == al.FINAL_NUDGE
    assert res.ending == "answered" and res.requests == 2


def test_a_result_that_would_cross_the_room_is_refused_and_the_next_request_is_the_last(reg, rs, monkeypatch):
    stub(monkeypatch, "\n".join("z" * 99 for _ in range(40)), cap=at.BIG_RESULT_CAP)  # 3,999 characters a result
    pv = ScriptedProvider([[call("stub")] * 2, [call("stub")] * 2, [reply(answer="Done.")]])
    res = al.run(pv, SYSTEM, "q", member(reg, rs))
    first, second = results_of(pv.requests[1]["messages"][-1]), results_of(pv.requests[2]["messages"][-1])
    # two wrapped results take 8,054; a third would make 12,081, past the room: refused before the total is reached
    assert [b["content"] == al.RESULTS_FULL for b in first + second] == [False, False, True, True]
    assert sum(len(b["content"]) for b in first) < al.TOTAL_RESULT_CHARS
    assert pv.requests[1]["force"] is None and pv.requests[2]["force"] == "reply"
    assert res.requests == 3


# ---------------------------------------------------------------- the reply

def test_answer_is_cut_to_1200(reg, rs):
    long = " ".join(["word"] * 600)
    res = al.run(ScriptedProvider([[reply(answer=long)]]), SYSTEM, "q", member(reg, rs))
    assert len(res.answer.answer) <= al.ANSWER_CHARS and res.answer.answer.endswith("word…")
    sentences = "This is one sentence of the answer. " * 60
    res = al.run(ScriptedProvider([[reply(answer=sentences)]]), SYSTEM, "q", member(reg, rs))
    assert len(res.answer.answer) <= al.ANSWER_CHARS and res.answer.answer.endswith("answer.")


def test_reply_page_is_checked_against_pages(reg, rs):
    def page(viewer, p):
        return al.run(ScriptedProvider([[reply(answer="Here.", page=p)]]), SYSTEM, "q", viewer).answer.page

    assert page(officer(reg, rs), "rosters") == "events"  # a gone page's name: the page that took its place
    assert page(officer(reg, rs), "nowhere") == ""
    assert page(member(reg, rs), "professions") == "professions"
    assert page(member(reg, rs), "rosters") == "" and page(member(reg, rs), "members") == ""  # officers' pages


def test_choices_must_be_names_the_lookups_returned(reg, rs):
    pv = ScriptedProvider([[call("recipe", name="ironfeather")],
                           [reply(answer="Which one?", choices=["Ironfeather Leggings", "Made Up Thing", "ironfeather leggings"])]])
    res = al.run(pv, SYSTEM, "who can make ironfeather?", member(reg, rs))
    assert res.ending == "asked" and res.choices == ["Ironfeather Leggings"]
    assert res.answer.answer == "Which one?\n• Ironfeather Leggings"


def test_a_choice_only_the_model_wrote_is_dropped_and_the_reply_is_an_answer(reg, rs):
    res = al.run(ScriptedProvider([[reply(answer="Thornwick makes it.", choices=["Made Up Thing"])]]), SYSTEM, "q", member(reg, rs))
    assert res.ending == "answered" and res.choices == [] and res.answer.answer == "Thornwick makes it."


def test_at_most_five_choices(reg, rs):
    names = [f"Name number {i}" for i in range(8)]
    res = al.run(ScriptedProvider([[reply(answer="Which?", choices=names)]]), SYSTEM, "q", member(reg, rs), names=names)
    assert res.choices == names[:al.MAX_CHOICES]


def test_acts_is_carried(reg, rs):
    res = al.run(ScriptedProvider([[reply(answer="", acts=True)]]), SYSTEM, "mark me away", member(reg, rs))
    assert res.answer.acts is True and res.ending == "acts"


def test_reply_fields_carry_as_help_answer_does(reg, rs):
    res = al.run(ScriptedProvider([[reply(answer="Ask an officer.", commands=["/a", "/b", "/c", "/d", "/e"], for_officer=True,
                                          officer_view=True)]]), SYSTEM, "q", member(reg, rs))
    assert res.answer.commands == ["/a", "/b", "/c", "/d"] and res.answer.for_officer and res.answer.officer_view


def test_a_text_only_turn_is_an_answer(reg, rs):
    res = al.run(ScriptedProvider(["Thornwick makes the breastplate."]), SYSTEM, "q", member(reg, rs))
    assert res.ending == "answered" and res.answer.answer == "Thornwick makes the breastplate." and res.requests == 1


@pytest.mark.parametrize("fields", [{"answer": "", "commands": [], "page": "", "choices": []}, {}, {"answer": "   "}])
def test_an_empty_reply_ends_with_the_honest_sentence(reg, rs, fields):
    res = al.run(ScriptedProvider([[reply(**fields)]]), SYSTEM, "bot?", member(reg, rs))
    assert res.ending == "cap" and res.answer.answer == al.ANSWER_NOT_FOUND


def test_a_turn_with_nothing_in_it_ends_with_the_honest_sentence(reg, rs):
    pv = ScriptedProvider([""])
    res = al.run(pv, SYSTEM, "q", member(reg, rs), known_kind="raid")
    assert res.ending == "error" and res.answer.answer == al.ANSWER_NOT_FOUND
    assert res.answer.page == ""  # the raids page is an officers' page


def test_a_reply_beside_other_calls_ends_and_runs_none_of_them(reg, rs):
    pv = ScriptedProvider([[call("find", text="x"), reply(answer="Done.")]])
    res = al.run(pv, SYSTEM, "q", member(reg, rs))
    assert res.ending == "answered" and res.requests == 1 and res.rounds == 0


# ---------------------------------------------------------------- the endings without an answer

def test_budget_checked_before_every_request(reg, rs):
    pv = ScriptedProvider([[call("find", text="ironfeather")], BudgetExceeded("The bot's monthly budget is used up.")])
    res = al.run(pv, SYSTEM, "q", member(reg, rs), known_kind="recipe")
    assert res.ending == "budget" and res.answer.answer == "The bot's monthly budget is used up." and res.requests == 1
    assert res.answer.page == ""  # the provider's sentence alone, as today: no page link under it


def test_a_model_error_ends_with_its_own_sentence_and_the_page_of_the_kind(reg, rs):
    res = al.run(ScriptedProvider([RuntimeError("boom")]), SYSTEM, "q", member(reg, rs), known_kind="recipe")
    assert res.ending == "error" and res.answer.answer == al.ANSWER_FAILED
    assert res.answer.page == "professions" and res.error == "RuntimeError" and res.requests == 0


@pytest.mark.parametrize("exc", [Refused("cyber"), CutShort()], ids=["refused", "cut-short"])
def test_a_refused_or_cut_off_response_ends_with_its_own_sentence(reg, rs, exc):
    pv = ScriptedProvider([[call("find", text="ironfeather")], exc])
    pv.last_usage = dict(USAGE)  # the provider writes the billed request's row before it raises
    res = al.run(pv, SYSTEM, "q", member(reg, rs), known_kind="item")
    assert res.ending == "error" and res.answer.answer == al.ANSWER_FAILED and res.error == type(exc).__name__
    assert res.answer.page == "prices" and res.requests == 2 and res.usage["input"] == 200


# ---------------------------------------------------------------- messages and usage

class ThinkingProvider(ScriptedProvider):
    """A scripted model whose first turn carries a thinking block before its lookup."""
    THINKING = {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJlLWJ5dGVz=="}

    def converse(self, workload, system, tools, messages, *, force=None):
        if self.n == 0:
            self.requests.append({"system": system, "tools": tools, "messages": json.loads(json.dumps(messages)), "force": force})
            self.n += 1
            content = [dict(self.THINKING), {"type": "tool_use", "id": "toolu_1", "name": "find", "input": {"text": "ironfeather"}}]
            return Turn(content=content, calls=[ToolCall("toolu_1", "find", {"text": "ironfeather"})], text="",
                        stop_reason="tool_use", usage=dict(USAGE))
        return super().converse(workload, system, tools, messages, force=force)


def test_thinking_blocks_go_back_unchanged(reg, rs):
    pv = ThinkingProvider([None, [reply(answer="Thornwick.")]])
    al.run(pv, SYSTEM, "q", member(reg, rs))
    sent = pv.requests[1]["messages"][1]
    assert sent["role"] == "assistant"
    assert json.dumps(sent["content"][0], sort_keys=True) == json.dumps(ThinkingProvider.THINKING, sort_keys=True)
    assert sent["content"][1]["id"] == "toolu_1"
    assert results_of(pv.requests[1]["messages"][2])[0]["tool_use_id"] == "toolu_1"


def test_usage_is_summed_over_requests(reg, rs):
    pv = ScriptedProvider([[call("find", text="ironfeather")], [reply(answer="Thornwick.")]])
    res = al.run(pv, SYSTEM, "q", member(reg, rs))
    assert res.usage["input"] == 200 and res.usage["output"] == 40 and res.usage["cost"] == pytest.approx(0.002)
    assert set(res.usage) == {"input", "output", "cache_read", "cache_write", "cost"}


def test_reply_spec_is_strict_and_carries_help_answers_words():
    spec = al.reply_spec()
    schema = spec["input_schema"]
    assert spec["name"] == al.REPLY and spec["strict"] is True and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == {"answer", "commands", "page", "officer_view", "for_officer",
                                                                    "acts", "choices"}
    assert schema["properties"]["page"]["enum"][-1] == "" and "professions" in schema["properties"]["page"]["enum"]
    assert schema["properties"]["acts"]["description"] == al.ACTS_DOC
    assert al.HelpAnswer.model_fields["commands"].description in schema["properties"]["commands"]["description"]


def test_reply_acts_says_a_how_to_question_is_answered_and_the_old_schema_keeps_its_words():
    """Task 10 (the probe: a how-to question was flagged acts): reply's acts says a question about HOW to do something is
    answered, never acts — one general rule; HelpAnswer's own description (the old path's schema) is unchanged."""
    acts = al.reply_spec()["input_schema"]["properties"]["acts"]["description"]
    assert "A question about HOW to do something is answered, never acts" in acts
    assert "only for a message that asks the bot to do it now" in acts
    assert al.HelpAnswer.model_fields["acts"].description == (
        "True when the message asks the bot to DO something now (record an absence, change a character or main, answer or "
        "confirm an event, change a setting) rather than asking how or what")
    assert al.reply_spec() == al.reply_spec(), "the same tool for every asker and question"


# ---------------------------------------------------------------- the three questions of §1

def _bench(reg, rs, case_id):
    case = next(c for c in BENCH if c.id == case_id)
    fmt = case.setup(reg, rs)
    question = case.question.format(**fmt)
    uid, is_officer = asker_of(case)
    viewer = at.Viewer.of(reg, rs, uid, is_officer, public=case.where == "public", question=question)
    pv = ScriptedProvider(list(case.script))
    res = al.answer(pv, None, viewer)
    sent = [b.get("text") or b.get("content") or "" for r in pv.requests for m in r["messages"] if m["role"] == "user"
            for b in m["content"]]
    return case, fmt, res, "\n".join(sent)


def _reaches_the_crafter(reg, rs, case_id):
    case, fmt, res, sent = _bench(reg, rs, case_id)
    assert case.must
    for m in case.must:
        assert m.format(**fmt) in sent, (case.id, m)
    for n in case.never:
        assert n.format(**fmt) not in sent and n.format(**fmt) not in res.answer.answer, (case.id, n)
    assert res.ending == "answered" and "Thornwick" in res.answer.answer
    assert res.tools == [c["name"] for step in case.script for c in step if c["name"] != al.REPLY]


def test_bench_ironfeather_who_has_reaches_the_crafter(reg, rs):
    _reaches_the_crafter(reg, rs, "ironfeather_who_has")


def test_bench_ironfeather_breastplate_reaches_the_crafter(reg, rs):
    _reaches_the_crafter(reg, rs, "ironfeather_breastplate")


def test_bench_ironfeather_pieces_reaches_the_crafter(reg, rs):
    _reaches_the_crafter(reg, rs, "ironfeather_pieces")


# ---------------------------------------------------------------- the fix round (Task 5 review)

def test_the_failed_request_sentence_is_todays_one_copy():
    from oibot_gm import discord_help
    assert al.ANSWER_FAILED == "Something went wrong, so I couldn't answer. The officers have the details."
    assert discord_help.ANSWER_FAILED is al.ANSWER_FAILED and al.ANSWER_FAILED != al.ANSWER_NOT_FOUND


class TextAndCallsProvider(ScriptedProvider):
    """A scripted model whose step may be ("text", [calls]): a turn that writes text AND asks for lookups."""

    def converse(self, workload, system, tools, messages, *, force=None):
        step = self.steps[self.n] if self.n < len(self.steps) else None
        if isinstance(step, tuple):
            self.requests.append({"system": system, "tools": tools, "messages": json.loads(json.dumps(messages)), "force": force})
            self.n += 1
            text, calls = step
            tc = [ToolCall(f"call-{self.n}-{i}", c["name"], dict(c["input"])) for i, c in enumerate(calls)]
            content = [{"type": "text", "text": text}] + [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tc]
            return Turn(content=content, calls=tc, text=text, stop_reason="tool_use", usage=dict(USAGE))
        return super().converse(workload, system, tools, messages, force=force)


def test_a_written_answer_on_the_last_request_is_never_dropped(reg, rs):
    look = [call("find", text="ironfeather")]
    pv = TextAndCallsProvider([look] * 3 + [("Thornwick makes the Ironfeather Breastplate.", look)])
    res = al.run(pv, SYSTEM, "q", member(reg, rs))
    assert pv.requests[3]["force"] == "reply"
    assert res.ending == "answered" and res.answer.answer == "Thornwick makes the Ironfeather Breastplate."
    assert res.not_run == ["find"]
    # with lookups and no text the last request still ends "cap"
    res = al.run(TextAndCallsProvider([look] * 3 + [("  ", look)]), SYSTEM, "q", member(reg, rs))
    assert res.ending == "cap" and res.answer.answer == al.ANSWER_NOT_FOUND


def test_choices_are_names_never_fragments_the_question_or_the_wrapper(reg, rs):
    question = "who can make ironfeather?"
    pv = ScriptedProvider([[call("recipe", name="ironfeather")],
                           [reply(answer="Which one?", choices=["Ironfeather Leg", "who can make", "data source", "Ironfeather Leggings"])]])
    res = al.run(pv, SYSTEM, question, member(reg, rs, question=question))
    assert res.choices == ["Ironfeather Leggings"]


def test_a_resolver_name_is_a_choice_without_a_lookup(reg, rs):
    names = ["Ironfeather Breastplate", "Ironfeather Leggings"]
    pv = ScriptedProvider([[reply(answer="Which one?", choices=[*names, "Ironfeather Shoulders"])]])
    res = al.run(pv, SYSTEM, "q", member(reg, rs), names=names)
    assert res.choices == names


def test_the_limits_of_section_six():
    assert (al.MAX_ROUNDS, al.MAX_CALLS_PER_ROUND, al.TOTAL_RESULT_CHARS, al.MAX_REQUESTS, al.ANSWER_CHARS,
            al.MAX_CHOICES) == (3, 4, 12_000, 4, 1_200, 5)


def test_lookups_beside_reply_are_listed_as_not_run(reg, rs):
    pv = ScriptedProvider([[call("find", text="x"), reply(answer="Done."), call("recipe", name="y")]])
    res = al.run(pv, SYSTEM, "q", member(reg, rs))
    assert res.tools == ["find", "recipe"] and res.not_run == ["find", "recipe"] and res.rounds == 0
