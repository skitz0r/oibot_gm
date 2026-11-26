"""The live probe (answer tools, design §10) driven offline: scripts/answer_probe.main with the scripted stand-in in
place of the real model — its table, its JSON, its exit codes, its temp-folder outputs (nothing under the repo's out/),
the no-key exit, the budget stop, a 400 reported and carried past, and today's path beside the new one."""
import dataclasses
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from answer_bench import LOOT_REASON
from answer_fakes import ScriptedProvider, reply

from oibot_gm.help import HelpAnswer
from oibot_gm.llm import provider as llm

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("answer_probe", ROOT / "scripts" / "answer_probe.py")
probe = importlib.util.module_from_spec(_spec)
sys.modules["answer_probe"] = probe  # dataclasses read the module back by name
_spec.loader.exec_module(probe)


def out_snapshot() -> dict:
    out = ROOT / "out"
    return {p: p.stat().st_mtime_ns for p in out.rglob("*")} if out.exists() else {}


@pytest.fixture
def work(tmp_path):
    return tmp_path / "probe"


def run(argv, provider, work, capsys):
    code = probe.main([*argv, "--work", str(work)], provider=provider)
    return code, capsys.readouterr().out, json.loads((work / "results.json").read_text())


def test_probe_scores_a_passing_case(work, capsys):
    code, text, res = run(["--case", "ironfeather_breastplate"],
                          ScriptedProvider([[reply(answer="Thornwick makes it, at 300.")]]), work, capsys)
    assert code == 0, text
    row = next(ln for ln in text.splitlines() if ln.startswith("ironfeather_breastplate"))
    assert "pass" in row and "answered" in row and "FAIL" not in row
    assert "pass rate 1/1 (100%)" in text
    assert "tag shorthand: 0/0 (not run)" in text  # the owner's decision line is always printed
    r = res["runs"][0]
    assert r["passed"] and r["judged"] and r["ending"] == "answered" and r["requests"] == 1 and r["usage"]["cost"] == pytest.approx(0.001)
    assert r["answer_literal_missing"] == [] and r["answer_missing"] == []
    assert res["spend"] == pytest.approx(0.001) and res["totals"]["new · claude-sonnet-5"]["passed"] == 1


def test_a_missing_fact_fails_with_its_reason(work, capsys):
    code, text, res = run(["--case", "ironfeather_breastplate"],
                          ScriptedProvider([[reply(answer="Nobody in the guild has that recipe yet.")]]), work, capsys)
    assert code == 1
    assert "FAIL" in text and "answer 'Thornwick' lacks thornwick" in text and "UNDER THE MARK" in text
    assert res["runs"][0]["answer_missing"] == [{"fact": "Thornwick", "words": ["thornwick"]}]
    assert res["runs"][0]["answer_literal_missing"] == ["Thornwick"]


def test_a_skill_or_a_second_name_a_good_answer_leaves_out_does_not_fail_it(work, capsys):
    """'who's our best lw' is answered by naming the best leatherworker: no skill, no second leatherworker, no member
    — the bench's `must` (what the lookup delivered) is not what the probe judges."""
    code, text, res = run(["--case", "lw_who"], ScriptedProvider([
        [{"name": "crafters", "input": {"profession": "Leatherworking"}}], [reply(answer="Thornwick.")]]), work, capsys)
    assert code == 0, text
    assert res["runs"][0]["passed"] and "tag shorthand: 1/1" in text


def test_a_skipped_lookup_is_reported_not_failed(work, capsys):
    """The right answer from the start alone passes; the skipped lookup shows in the lookups column and the tag's count."""
    code, text, res = run(["--case", "bs_in_the_guild"],
                          ScriptedProvider([[reply(answer="Briarwood is our blacksmith.")]]), work, capsys)
    assert code == 0, text
    r = res["runs"][0]
    assert r["passed"] and r["tools_missing"] == ["crafters"] and r["reasons"] == []
    row = next(ln for ln in text.splitlines() if ln.startswith("bs_in_the_guild"))
    assert "pass" in row and "-crafters" in row
    assert "tag shorthand: 1/1 · an expected lookup skipped in 1" in text
    assert "an expected lookup was skipped in 1 of 1 runs" in text
    tot = res["totals"]["new · claude-sonnet-5"]
    assert tot["lookups_skipped"] == 1 and tot["per_tag"]["shorthand"]["lookups_skipped"] == 1


def test_a_case_without_answer_facts_is_not_judged(work, capsys, monkeypatch):
    """A case naming no answer facts is shown 'n/j' and left out of the pass rate — never a pass."""
    bench = [dataclasses.replace(c, answer=()) if c.id == "price" else c for c in probe.BENCH]
    monkeypatch.setattr(probe, "BENCH", bench)
    pv = ScriptedProvider([[reply(answer="Thornwick, 300.")], [reply(answer="Whatever it goes for.")]])
    code, text, res = run(["--only", "ironfeather_breastplate", "--only", "price"], pv, work, capsys)
    assert code == 0, text
    judged, unjudged = res["runs"]
    assert judged["passed"] and not unjudged["passed"] and not unjudged["judged"]
    assert "pass rate 1/1 (100%) · not judged 1" in text
    row = next(ln for ln in text.splitlines() if ln.startswith("price"))
    assert "n/j" in row and "not judged" in text
    tot = res["totals"]["new · claude-sonnet-5"]
    assert tot["runs"] == 1 and tot["not_judged"] == 1 and tot["pass_rate"] == 1.0


def test_nothing_judged_is_under_the_mark(work, capsys, monkeypatch):
    monkeypatch.setattr(probe, "BENCH", [dataclasses.replace(c, answer=()) for c in probe.BENCH])
    code, text, res = run(["--case", "price"], ScriptedProvider([[reply(answer="It goes for a lot.")]]), work, capsys)
    assert code == 1 and "nothing judged" in text and "UNDER THE MARK" in text
    assert res["totals"]["new · claude-sonnet-5"]["pass_rate"] is None


def test_probe_fails_on_a_never_hit(work, capsys):
    """A never hit fails the probe even with the pass mark at zero: a visibility failure always fails."""
    code, text, res = run(["--case", "away_member", "--pass-mark", "0"],
                          ScriptedProvider([[reply(answer="Wrenfield is away for the *Dentist*.")]]), work, capsys)
    assert code == 1
    assert "NEVER 'dentist' appeared" in text and "NEVER HITS 1" in text
    assert res["runs"][0]["never_hit"] == ["dentist"]


def test_probe_never_writes_out(work, capsys):
    before = out_snapshot()
    code, _text, _res = run(["--case", "price"], ScriptedProvider([
        [{"name": "price", "input": {"name": "Arcanite Bar"}}], [reply(answer="An Arcanite Bar goes for 48g 99s 98c.")]]),
        work, capsys)
    assert code == 0
    assert out_snapshot() == before
    assert (work / "results.json").exists() and (work / "cases").is_dir()


def test_the_real_provider_writes_only_into_the_work_folder(work, monkeypatch, capsys):
    """The usage log and the budget caps the real provider reads point into the work folder for the run and are put
    back after it."""
    monkeypatch.setenv("OIBOT_USAGE_LOG", "/should/not/be/used.jsonl")
    seen = {}

    class Spy(ScriptedProvider):
        def converse(self, *a, **k):
            seen.update(log=os.environ["OIBOT_USAGE_LOG"], answers=os.environ["OIBOT_ANSWERS_LOG"],
                        cap=os.environ["OIBOT_BUDGET_USD_HELP"])
            return super().converse(*a, **k)

    probe.main(["--case", "ironfeather_breastplate", "--budget", "1.5", "--work", str(work)],
               provider=Spy([[reply(answer="Thornwick, 300.")]]))
    assert seen == {"log": str(work / "llm_usage.jsonl"), "answers": str(work / "answers.jsonl"), "cap": "1.5000"}
    assert os.environ["OIBOT_USAGE_LOG"] == "/should/not/be/used.jsonl"


def test_no_key_exits_2(work, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    code = probe.main(["--case", "price", "--work", str(work), "--env-file", str(tmp_path / "none.env")])
    text = capsys.readouterr().out
    assert code == 2
    assert text.strip() == "ANTHROPIC_API_KEY is not set in the environment or the .env file, so the probe ran nothing."
    assert not work.exists()


def test_a_work_folder_outside_the_temp_directory_is_refused(capsys):
    code = probe.main(["--case", "price", "--work", str(ROOT / "out" / "probe")], provider=ScriptedProvider([]))
    assert code == 2 and "not under the system temp directory" in capsys.readouterr().out
    assert not (ROOT / "out" / "probe").exists()


def test_an_unknown_case_runs_nothing(work, capsys):
    assert probe.main(["--only", "nope", "--work", str(work)], provider=ScriptedProvider([])) == 2
    assert "No bench case or tag is called nope" in capsys.readouterr().out


def test_the_budget_stops_the_probe(work, capsys):
    """Each scripted request costs $0.001: with $0.0025 three runs are sent, then the probe's cap is spent and the
    other two repeats are never sent."""
    steps = [[reply(answer="Thornwick, 300.")]] * 5
    code, text, res = run(["--case", "ironfeather_breastplate", "--repeat", "5", "--budget", "0.0025"],
                          ScriptedProvider(steps), work, capsys)
    assert code == 3
    assert "Stopped: the probe's budget of $0.0025 was reached after 3 runs; the rest were not run." in text
    assert [r["ending"] for r in res["runs"]] == ["answered"] * 3
    assert res["stopped_by_budget"] and res["spend"] == pytest.approx(0.003)


def test_the_budget_ends_a_run_midway(work, capsys):
    """A run whose second request would cross the cap ends "budget" (the loop's own ending) and the probe stops."""
    look = [{"name": "crafters", "input": {"profession": "Leatherworking"}}]
    code, text, res = run(["--only", "crafters", "--only", "price", "--budget", "0.001"],
                          ScriptedProvider([look, [reply(answer="never sent")]]), work, capsys)
    assert code == 3
    assert [(r["case"], r["ending"]) for r in res["runs"]] == [("crafters", "budget")]
    assert "reached after 1 run;" in text and res["spend"] == pytest.approx(0.001)


class FakeBadRequest(Exception):
    status_code = 400
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "secret request body here"}}


def test_a_400_is_an_error_ending_and_the_next_case_runs(work, capsys):
    pv = ScriptedProvider([FakeBadRequest("tool_choice not taken"), [reply(answer="Thornwick (Wrenfield, 300) knows "
                                                                                  "Leatherworking.")]])
    code, text, res = run(["--only", "ironfeather_breastplate", "--only", "crafters", "--pass-mark", "0"], pv, work, capsys)
    first, second = res["runs"]
    assert first["ending"] == "error" and first["error"] == "400 invalid_request_error" and first["requests"] == 0
    assert "error (400 invalid_request_error)" in text and "secret request body" not in text
    assert second["case"] == "crafters" and second["ending"] == "answered"
    assert code == 0  # the pass mark is 0 here and no never was hit


def test_final_request_forced_is_recorded(work, capsys, monkeypatch):
    """Three lookup rounds make the fourth request the last: forced where the model takes it."""
    look = [{"name": "crafters", "input": {"profession": "Leatherworking"}}]
    steps = [look, look, look, [reply(answer="Thornwick (Wrenfield, 300).")]]
    _code, _text, res = run(["--case", "crafters", "--model", "claude-sonnet-5"], ScriptedProvider(list(steps)), work, capsys)
    assert res["runs"][0]["final_forced"] is True and res["runs"][0]["rounds"] == 3
    _code, _text, res = run(["--case", "crafters", "--model", "claude-sonnet-5-5"], ScriptedProvider(list(steps)),
                            work.parent / "probe2", capsys)
    assert res["runs"][0]["final_forced"] is False


def test_compare_prints_two_columns_and_restores_the_route(work, capsys):
    route = dict(llm.ROUTES)
    code, text, res = run(["--case", "ironfeather_breastplate", "--model", "claude-sonnet-5", "--effort", "medium",
                           "--compare", "claude-sonnet-5-5"],
                          ScriptedProvider([[reply(answer="Thornwick, 300.")], [reply(answer="Nobody can.")]]), work, capsys)
    assert res["configs"] == ["new · claude-sonnet-5", "new · claude-sonnet-5-5"] and res["effort"] == "medium"
    assert "== side by side" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("ironfeather_breastplate") and "$" in ln and "/" in ln.split()[1])
    assert "1/1" in line and "0/1" in line
    assert code == 1  # the second setting is under the mark
    assert llm.ROUTES == route


class OldPathFake:
    """Today's path: help.answer's complete, one HelpAnswer a call, a usage row each."""
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.last_usage = None

    def complete(self, workload, system, user, schema):
        self.last_usage = {"input": 900, "output": 30, "cache_read": 0, "cache_write": 0, "cost": 0.004}
        return self.answers.pop(0)

    def converse(self, *a, **k):
        return ScriptedProvider([[reply(answer="Thornwick makes it, at 300.")]]).converse(*a, **k)


def test_baseline_runs_todays_path_beside_the_new_one(work, capsys):
    pv = OldPathFake([HelpAnswer(answer="Thornwick (300) can make it.")])
    code, text, res = run(["--case", "ironfeather_breastplate", "--baseline"], pv, work, capsys)
    assert res["configs"] == ["new · claude-sonnet-5", "today · claude-sonnet-5"]
    new, old = res["runs"]
    assert new["path"] == "loop" and old["path"] == "old"
    assert old["passed"] and old["requests"] == 1 and old["usage"]["cost"] == pytest.approx(0.004) and old["tools"] == []
    assert "== today · claude-sonnet-5" in text and "== side by side" in text
    assert code == 0


def test_a_visibility_case_reason_never_reaches_the_answer_unflagged(work, capsys):
    code, text, _res = run(["--case", "loot_reason_member", "--pass-mark", "0"],
                           ScriptedProvider([[reply(answer=f"It was {LOOT_REASON}.")]]), work, capsys)
    assert code == 1 and "NEVER" in text


def test_fact_words_and_matching():
    assert probe.fact_words("made by Thornwick (300)") == ["thornwick", "300"]
    assert probe.fact_words("lock Wed 9 Dec 3:00 PM") == ["wed", "9", "dec", "3:00", "pm"]
    assert probe.missing_facts("lock Wed 9 Dec 3:00 PM", "It locks on **Wednesday**, December 9 at 3 PM.") == []
    assert probe.missing_facts("I'll be away", "Press *I’ll be away*.") == []
    assert probe.missing_facts("made by Thornwick (300)", "Thornwick at 3000") == ["300"]
