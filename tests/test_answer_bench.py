"""The question bench (answer tools, design §10) run whole: every case's candidates from the resolver, and every
scripted case end to end through answer_loop.answer with the REAL lookups — the scripted model stands in for the model,
nothing else does."""
import pytest

from answer_bench import BENCH, asker_of
from answer_fakes import ScriptedProvider

from oibot_gm import answer_loop as al
from oibot_gm import answer_tools as at
from oibot_gm.answer_names import resolve


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


@pytest.mark.parametrize("case", [c for c in BENCH if c.script], ids=lambda c: c.id)
def test_bench_end_to_end(reg, rs, case):
    fmt = case.setup(reg, rs)
    question = case.question.format(**fmt)
    uid, officer = asker_of(case)
    viewer = at.Viewer.of(reg, rs, uid, officer, public=case.where == "public", question=question)
    pv = ScriptedProvider(list(case.script))
    res = al.answer(pv, None, viewer)
    sent = "\n".join(
        b.get("text") or b.get("content") or ""
        for r in pv.requests for m in r["messages"] if m["role"] == "user" for b in m["content"])
    for m in case.must:
        assert m.format(**fmt) in sent, (case.id, m, sent)
    for n in case.never:
        assert n.format(**fmt) not in sent and n.format(**fmt) not in res.answer.answer, (case.id, n)
    for t in case.expect_tools:
        assert t in res.tools, (case.id, t, res.tools)
    assert res.ending == ("asked" if case.asks_back else "answered"), (case.id, res.ending, res.answer.answer)
    assert res.requests == len(case.script)
