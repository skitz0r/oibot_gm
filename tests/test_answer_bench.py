"""The question bench (answer tools, design §10) run whole: every case's candidates from the resolver, and every
scripted case end to end through answer_loop.answer with the REAL lookups — the scripted model stands in for the model,
nothing else does."""
import importlib.util
import string
import sys
from pathlib import Path

import pytest

from answer_bench import BENCH, asker_of, final_reply, scripted
from answer_fakes import ScriptedProvider

from oibot_gm import answer_loop as al
from oibot_gm import answer_tools as at
from oibot_gm import armory, discord_auctions, prices
from oibot_gm.answer_names import resolve

SCRIPTED = [c for c in BENCH if c.script]
LOOKUP = [c for c in SCRIPTED if c.expect_tools]


def names_of(matches):
    return [m.name for m in matches]


def in_order(want, got) -> bool:
    """`want` appears in `got` in the same order (other names may sit between)."""
    it = iter(got)
    return all(any(w == g for g in it) for w in want)


@pytest.mark.parametrize("case", [c for c in BENCH if c.candidates], ids=lambda c: c.id)
def test_resolver_candidates(reg, rs, case):
    fmt = case.setup(reg, rs)
    got = names_of(resolve(reg, case.question.format(**fmt))[0])
    want = [c.format(**fmt) for c in case.candidates]
    assert in_order(want, got), (case.id, got)


@pytest.fixture
def auction_house(monkeypatch, tmp_path):
    """The host's armory keys, stood in for (as tests/test_prices does): the bench's priced_bar picks a house and its
    table lives in memory; the item names cache in the test's own folder."""
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    monkeypatch.setattr(discord_auctions, "OUT", tmp_path / "auctions-out")
    monkeypatch.setattr(discord_auctions, "_STATE", {})
    monkeypatch.setattr(discord_auctions, "_SHARED", {})
    prices.reset_names()
    yield
    prices.reset_names()


def run_case(request, reg, rs, case):
    """One case through answer() → (fmt, the result, the first user message, the tool_result blocks' text)."""
    if "price" in case.expect_tools:
        request.getfixturevalue("auction_house")
    fmt = case.setup(reg, rs)
    question = case.question.format(**fmt)
    uid, officer = asker_of(case)
    viewer = at.Viewer.of(reg, rs, uid, officer, public=case.where == "public", question=question)
    pv = ScriptedProvider(scripted(case, fmt))
    res = al.answer(pv, None, viewer)
    first = pv.requests[0]["messages"][0]["content"][0]["text"]
    results = "\n".join(b.get("content") or "" for m in pv.requests[-1]["messages"] if m["role"] == "user"
                        for b in m["content"] if b.get("type") == "tool_result")
    sent = "\n".join(b.get("text") or b.get("content") or ""
                     for r in pv.requests for m in r["messages"] if m["role"] == "user" for b in m["content"])
    return fmt, res, first, results, sent


def missing_must(case, fmt, first, results) -> list[str]:
    """The `must` values not found where they have to come from: a case that expects lookups proves them in the
    lookups' results (never the start, never the scripted reply); a case that expects none, in the start."""
    where = results if case.expect_tools else first
    return [m.format(**fmt) for m in case.must if m.format(**fmt) not in where]


@pytest.mark.parametrize("case", SCRIPTED, ids=lambda c: c.id)
def test_bench_end_to_end(request, reg, rs, case):
    fmt, res, first, results, sent = run_case(request, reg, rs, case)
    assert not missing_must(case, fmt, first, results), (case.id, missing_must(case, fmt, first, results), results or first)
    if case.expect_tools:  # the value proves the lookup: the start alone could not have given it
        assert case.must, case.id
        for m in case.must:
            assert m.format(**fmt) not in first, (case.id, m, "can be read off the start")
    for n in case.never:
        assert n.format(**fmt) not in sent and n.format(**fmt) not in res.answer.answer, (case.id, n)
    for t in case.expect_tools:
        assert t in res.tools, (case.id, t, res.tools)
    assert res.ending == ("asked" if case.asks_back else "answered"), (case.id, res.ending, res.answer.answer)
    assert res.requests == len(case.script)


@pytest.mark.parametrize("case", LOOKUP, ids=lambda c: c.id)
def test_lookups_that_find_nothing_fail_the_must_check(request, reg, rs, case, monkeypatch):
    """With every lookup answering "nothing here", a case that expects lookups can no longer pass: the bench cannot
    go quiet when the lookups stop delivering."""
    monkeypatch.setattr(al, "run_tool", lambda viewer, name, args: ("nothing here", False))
    fmt, res, first, results, _sent = run_case(request, reg, rs, case)
    assert "nothing here" in results
    assert case.must and missing_must(case, fmt, first, results), (case.id, "passes without its lookups")


SHORTHAND = [c for c in BENCH if "shorthand" in c.tags]


def test_the_shorthand_group_is_the_three_cases():
    assert [c.id for c in SHORTHAND] == ["bs_in_the_guild", "pally_healers", "lw_who"]
    assert all(c.expect_tools and c.script for c in SHORTHAND)


@pytest.mark.parametrize("case", SHORTHAND, ids=lambda c: c.id)
def test_the_resolver_reads_no_shorthand(reg, rs, case):
    """No alias table: the short name resolves to nothing the case's answer needs, so only the model's mapping to the
    lookup's enum finds it."""
    fmt = case.setup(reg, rs)
    names = names_of(resolve(reg, case.question.format(**fmt))[0])
    if case.id == "bs_in_the_guild":
        assert names == [], names
    assert not {"Blacksmithing", "Paladin", "Leatherworking"} & set(names), names


def test_every_scripted_lookup_call_fits_the_profiles_schema(reg):
    """The scripted model makes only calls a real model could make: every argument is a property of that lookup's
    schema for the bench's profile, and an enum argument is one of the enum's values."""
    specs = {t.name: t.schema for t in at.tools_for(reg.profile)}
    for case in SCRIPTED:
        for step in case.script:
            for c in (step if isinstance(step, list) else []):
                if c["name"] == "reply":
                    continue
                props = specs[c["name"]]["properties"]
                for k, v in c["input"].items():
                    assert k in props, (case.id, c["name"], k)
                    if "enum" in props[k]:
                        assert v in props[k]["enum"], (case.id, c["name"], k, v)


# ---------------------------------------------------------------- `answer`: what the live probe judges on

def _probe():
    """scripts/answer_probe.py, loaded once (its fact-word matching is the rule the honesty check applies)."""
    if "answer_probe" not in sys.modules:
        spec = importlib.util.spec_from_file_location("answer_probe", Path(__file__).resolve().parents[1] / "scripts" / "answer_probe.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["answer_probe"] = mod  # dataclasses read the module back by name
        spec.loader.exec_module(mod)
    return sys.modules["answer_probe"]


def test_every_case_says_what_a_good_answer_holds():
    assert all(c.answer for c in BENCH), [c.id for c in BENCH if not c.answer]


@pytest.mark.parametrize("case", BENCH, ids=lambda c: c.id)
def test_answer_placeholders_are_filled_by_the_setup(reg, rs, case):
    values = case.setup(reg, rs)
    for a in case.answer:
        assert {f for _, f, _, _ in string.Formatter().parse(a) if f} <= set(values), (case.id, a)
        assert "{" not in a.format(**values) and a.format(**values).strip(), (case.id, a)


@pytest.mark.parametrize("case", SCRIPTED, ids=lambda c: c.id)
def test_the_scripted_reply_holds_its_answer_facts(reg, rs, case):
    """The honesty check: the bench's own final reply passes the probe's `answer` rule, so a fact no good answer
    could hold is never asked for."""
    fmt = case.setup(reg, rs)
    text = final_reply(case, fmt)
    assert text.strip(), case.id
    for a in case.answer:
        assert not _probe().missing_facts(a.format(**fmt), text), (case.id, a.format(**fmt), text)
    for n in case.never:
        assert not _probe().never_hits([n.format(**fmt)], text), (case.id, n)
