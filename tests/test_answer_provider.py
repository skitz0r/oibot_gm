"""AnthropicProvider.converse — the one multi-turn tool-use request (answer tools, design §3, §6): tools and the cached
system block go out as given, a forced tool only where the model takes one, the response blocks come back verbatim
(thinking signatures intact) with the tool calls, the budget is checked before the request and one usage row is
written per request, a refusal is an error. Also: the question bench is well formed. No network, no model."""
from __future__ import annotations

import json
import string
from types import SimpleNamespace

import pytest

from answer_bench import BENCH, LOOT_ITEM, LOOT_TO, asker_of
from answer_fakes import ScriptedProvider, call, reply

from oibot_gm import prices
from oibot_gm.llm import provider as pv

# the tools of answer tools' Tasks 3–4: the bench reaches each of them at least once
TOOL_NAMES = ("find", "recipe", "crafters", "price", "member", "event", "schedule", "loot", "attendance", "manual", "section")


DUMPS: list[dict] = []  # the kwargs of every block.model_dump call, so a test can see what converse asked for


def block(**fields):
    """A response block the way the SDK hands it back: attributes plus model_dump (which records its kwargs)."""
    def dump(**kw):
        DUMPS.append(kw)
        return {k: v for k, v in fields.items() if v is not None}
    return SimpleNamespace(**fields, model_dump=dump)


class FakeClient:
    def __init__(self, content=None, stop_reason="end_turn", tokens=1000, stop_details=None):
        self.content = content if content is not None else [block(type="text", text="hi")]
        self.stop_reason, self.tokens, self.calls, self.stop_details = stop_reason, tokens, [], stop_details
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        u = SimpleNamespace(input_tokens=self.tokens, output_tokens=10, cache_read_input_tokens=5, cache_creation_input_tokens=0)
        return SimpleNamespace(usage=u, content=self.content, stop_reason=self.stop_reason, stop_details=self.stop_details)


def make(tmp_path, monkeypatch, client=None, budget="5"):
    monkeypatch.setenv("OIBOT_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OIBOT_BUDGET_USD", budget)
    p = pv.AnthropicProvider.__new__(pv.AnthropicProvider)
    p.client, p.last_usage, p.name = client or FakeClient(), {}, "claude"
    p.budget_usd = float(budget)
    p.ledger = tmp_path / "usage.jsonl"
    p.log = p._this_month()
    return p


TOOLS = [{"name": "find", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


def test_converse_sends_tools_system_cache_and_auto_choice(tmp_path, monkeypatch):
    monkeypatch.delenv("OIBOT_MODEL_HELP", raising=False)
    p = make(tmp_path, monkeypatch)
    history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]
    before = json.dumps(history)
    p.converse("help", "the system", TOOLS, history)
    kw = p.client.calls[0]
    assert kw["tools"] == TOOLS and kw["model"] == "claude-sonnet-5"
    assert kw["system"][0] == {"type": "text", "text": "the system", "cache_control": {"type": "ephemeral"}}
    assert kw["thinking"] == {"type": "adaptive"} and kw["output_config"] == {"effort": "low"}
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(kw["messages"][:-1]), "one breakpoint in the history: the newest user message"
    assert json.dumps(history) == before, "the caller's history is never edited"
    p.converse("help", "s", TOOLS, [{"role": "user", "content": "a plain string"}])
    assert p.client.calls[1]["messages"][-1]["content"] == [{"type": "text", "text": "a plain string", "cache_control": {"type": "ephemeral"}}]


@pytest.mark.parametrize("model, choice", [("claude-sonnet-5", {"type": "tool", "name": "reply"}),
                                           ("claude-sonnet-5-5", {"type": "auto"}),
                                           ("claude-opus-5-5", {"type": "auto"})])
def test_converse_forces_reply_only_where_the_model_takes_it(tmp_path, monkeypatch, model, choice):
    monkeypatch.setenv("OIBOT_MODEL_HELP", model)
    p = make(tmp_path, monkeypatch)
    p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}], force="reply")
    assert p.client.calls[0]["tool_choice"] == choice and p.client.calls[0]["model"] == model


def test_converse_returns_blocks_verbatim_and_the_calls(tmp_path, monkeypatch):
    content = [block(type="thinking", thinking="", signature="sig"),
               block(type="text", text="Looking that up."),
               block(type="tool_use", id="t1", name="find", input={"text": "ironfeather", "kinds": ["recipe"]}),
               block(type="tool_use", id="t2", name="recipe", input={"name": "Ironfeather Breastplate"})]
    p = make(tmp_path, monkeypatch, FakeClient(content, stop_reason="tool_use"))
    DUMPS.clear()
    turn = p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
    assert DUMPS == [{"mode": "json", "exclude_none": True}] * 4, "every block dumped as JSON, None fields left out"
    assert turn.content == [{"type": "thinking", "thinking": "", "signature": "sig"},
                            {"type": "text", "text": "Looking that up."},
                            {"type": "tool_use", "id": "t1", "name": "find", "input": {"text": "ironfeather", "kinds": ["recipe"]}},
                            {"type": "tool_use", "id": "t2", "name": "recipe", "input": {"name": "Ironfeather Breastplate"}}]
    assert turn.calls == [pv.ToolCall("t1", "find", {"text": "ironfeather", "kinds": ["recipe"]}),
                          pv.ToolCall("t2", "recipe", {"name": "Ironfeather Breastplate"})]
    assert turn.text == "Looking that up." and turn.stop_reason == "tool_use"
    assert turn.usage["workload"] == "help" and turn.usage["cache_read"] == 5


def test_converse_checks_the_budget_first_and_writes_one_row_per_request(tmp_path, monkeypatch):
    p = make(tmp_path, monkeypatch, FakeClient(tokens=1_000_000), budget="2")  # 1M sonnet input tokens = $2: the cap
    p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
    with pytest.raises(pv.BudgetExceeded, match="the host raises it"):
        p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
    assert len(p.client.calls) == 1, "the budget is checked before the request"

    (tmp_path / "b").mkdir()
    q = make(tmp_path / "b", monkeypatch)
    with pv.for_guild("demo"):
        q.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
        q.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
    rows = [json.loads(ln) for ln in (tmp_path / "b" / "usage.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and all(r["workload"] == "help" and r["guild"] == "demo" for r in rows)
    assert set(rows[0]) == {"month", "guild", "workload", "model", "input", "output", "cache_read", "cache_write", "cost"}


def _rows(tmp_path):
    return [json.loads(ln) for ln in (tmp_path / "usage.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("details, category", [(SimpleNamespace(type="refusal", category="cyber", explanation="x"), "cyber"),
                                               (SimpleNamespace(type="refusal", category=None, explanation=None), None),
                                               (None, None)])
def test_converse_refusal_raises_refused_after_its_usage_row(tmp_path, monkeypatch, details, category):
    p = make(tmp_path, monkeypatch, FakeClient(stop_reason="refusal", stop_details=details))
    with pytest.raises(pv.Refused) as err:
        p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
    assert err.value.category == category
    assert "cyber" not in str(err.value), "the category is an attribute, not words"
    assert len(_rows(tmp_path)) == 1 and _rows(tmp_path)[0]["workload"] == "help", "a refused request is billed"


def test_converse_max_tokens_raises_cut_short_after_its_usage_row(tmp_path, monkeypatch):
    content = [block(type="text", text="Half a sen"), block(type="tool_use", id="t1", name="find", input={"te": None})]
    p = make(tmp_path, monkeypatch, FakeClient(content, stop_reason="max_tokens"))
    with pytest.raises(pv.CutShort):
        p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}])
    assert len(_rows(tmp_path)) == 1 and p.last_usage["workload"] == "help"


def test_refused_cut_short_and_budget_exceeded_are_told_apart():
    assert issubclass(pv.Refused, RuntimeError) and issubclass(pv.CutShort, RuntimeError)
    for a in (pv.Refused, pv.CutShort, pv.BudgetExceeded):
        for b in (pv.Refused, pv.CutShort, pv.BudgetExceeded):
            assert issubclass(a, b) == (a is b), (a, b)


@pytest.mark.parametrize("model, ok", [("claude-sonnet-5", True), ("claude-opus-5", True), ("claude-opus-5-20260301", True),
                                       ("claude-opus-5-5", False), ("claude-opus-5-5[1m]", False),
                                       ("claude-opus-5-5-20261001", False), ("us.anthropic.claude-sonnet-5-5-v1:0", False),
                                       ("claude-fable-5-2@20261101", False)])
def test_forced_tool_matches_the_model_not_the_exact_id(model, ok):
    assert pv.forced_tool_ok(model) is ok


def test_converse_sends_no_forced_tool_to_a_suffixed_id(tmp_path, monkeypatch):
    monkeypatch.setenv("OIBOT_MODEL_HELP", "claude-opus-5-5[1m]")
    p = make(tmp_path, monkeypatch)
    p.converse("help", "s", TOOLS, [{"role": "user", "content": "q"}], force="reply")
    assert p.client.calls[0]["tool_choice"] == {"type": "auto"} and p.client.calls[0]["model"] == "claude-opus-5-5[1m]"


def test_model_id_feeds_price_of():
    assert pv.model_id("us.anthropic.claude-sonnet-5-5-20261001-v1:0[1m]") == "claude-sonnet-5-5"
    assert pv.price_of("claude-sonnet-5-5[1m]") == pv.PRICES["claude-sonnet-5-5"]
    assert pv.model_id("jev-1.13.0") == "jev-1.13.0"


@pytest.mark.parametrize("content", ["", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}, {"type": "text", "text": ""}]])
def test_no_breakpoint_on_an_empty_text_block(tmp_path, monkeypatch, content):
    p = make(tmp_path, monkeypatch)
    p.converse("help", "s", TOOLS, [{"role": "user", "content": content}])
    assert "cache_control" not in json.dumps(p.client.calls[0]["messages"])


def test_new_prices_are_exact():
    assert pv.price_of("claude-sonnet-5-5") == (2.0, 10.0, 0.1, 2.5)


# ---------------------------------------------------------------- the scripted stand-in

def test_scripted_provider_plays_its_script_and_records_requests():
    sp = ScriptedProvider([[call("find", text="x")], "just text", [reply(answer="done")]])
    t1 = sp.converse("help", "sys", TOOLS, [{"role": "user", "content": "q"}], force="reply")
    assert t1.calls[0].name == "find" and t1.calls[0].input == {"text": "x"} and t1.content[0]["type"] == "tool_use"
    t2 = sp.converse("help", "sys", TOOLS, [])
    assert t2.calls == [] and t2.text == "just text"
    t3 = sp.converse("help", "sys", TOOLS, [])
    assert t3.calls[0].name == "reply" and t3.calls[0].input == {"answer": "done"} and t3.usage["model"] == "scripted"
    assert sp.requests[0] == {"system": "sys", "tools": TOOLS, "messages": [{"role": "user", "content": "q"}], "force": "reply"}
    with pytest.raises(AssertionError):
        sp.converse("help", "sys", TOOLS, [])
    with pytest.raises(AssertionError):
        sp.complete("help", "s", "u", object)


def test_scripted_provider_raises_a_scripted_exception_where_the_loop_reaches_it():
    for exc in (pv.BudgetExceeded("over"), pv.Refused("cyber"), pv.CutShort(), RuntimeError("boom")):
        sp = ScriptedProvider([[call("find", text="x")], exc, "after"])
        assert sp.converse("help", "s", TOOLS, []).calls[0].name == "find"
        with pytest.raises(type(exc)) as err:
            sp.converse("help", "s", TOOLS, [])
        assert err.value is exc and len(sp.requests) == 2, "the request is recorded before the step raises"
        assert sp.converse("help", "s", TOOLS, []).text == "after", "the script goes on after a raised step"
        with pytest.raises(AssertionError, match="ran past its script"):
            sp.converse("help", "s", TOOLS, [])


# ---------------------------------------------------------------- the bench

def _fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def _steps_tools(script) -> set[str]:
    return {c["name"] for step in script if not isinstance(step, str) for c in step}


def test_bench_is_well_formed(reg, rs):
    ids = [c.id for c in BENCH]
    assert len(ids) == len(set(ids))
    assert {"ironfeather_who_has", "ironfeather_breastplate", "ironfeather_pieces"} <= set(ids)
    for case in BENCH:
        values = case.setup(reg, rs)
        uid, _officer = asker_of(case)
        assert uid in reg.members and reg.members[uid].active(), f"{case.id}: the asker's record exists"
        for s in (case.question, *case.candidates, *case.must, *case.never):
            assert _fields(s) <= set(values), f"{case.id}: {s!r} has a placeholder its setup doesn't fill"
            assert "{" not in s.format(**values)
        recipes = {r.name for r in reg.profile.recipes.values()}
        names = {c.name for m in reg.members.values() for c in m.characters} | {m.display_name for m in reg.members.values()}
        items = set(prices.names(reg).values())
        for cand in case.candidates:
            cand = cand.format(**values)
            assert cand in recipes or cand in names or cand in items, f"{case.id}: candidate {cand!r} is no listed recipe, item or registry name"
    reached = {t for c in BENCH for t in c.expect_tools} | {t for c in BENCH for t in _steps_tools(c.script)}
    assert set(TOOL_NAMES) <= reached, set(TOOL_NAMES) - reached
    assert any(c.asks_back for c in BENCH) and any("manual" in c.expect_tools for c in BENCH)
    assert asker_of(next(c for c in BENCH if c.asker == "officer")) == (700_000_000_000_000_002, True)


def test_loot_scripts_and_must_come_from_the_same_award(reg, rs):
    for cid in ("loot", "loot_reason_member"):
        case = next(c for c in BENCH if c.id == cid)
        values = case.setup(reg, rs)
        assert values == {"item": LOOT_ITEM, "character": LOOT_TO}
        answer = case.script[-1][0]["input"]["answer"]
        assert values["character"] in answer and values["item"] in answer
        assert all(m.format(**values) in answer for m in case.must)
