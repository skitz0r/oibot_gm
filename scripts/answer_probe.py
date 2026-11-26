"""The live probe of the answer tools (design §10, §11): the REAL model answers every question of the bench
(tests/answer_bench.py) through answer_loop.answer, on a scratch copy of the demo guild, and the probe reports per case
and in total whether the answer held what it must, held nothing it must never, and how many rounds, requests, tokens,
dollars and seconds it took. Its numbers are what the owner decides on before the switch (OIBOT_ANSWER_TOOLS) is turned
on for any guild. It is the retune check, as scripts/listen_probe.py is for listening.

    uv run python scripts/answer_probe.py                                   # the whole bench on the help route's model
    uv run python scripts/answer_probe.py --model claude-sonnet-5 --effort low
    uv run python scripts/answer_probe.py --model claude-sonnet-5-5 --effort low
    uv run python scripts/answer_probe.py --model claude-sonnet-5 --effort low --compare claude-sonnet-5-5
    uv run python scripts/answer_probe.py --baseline                        # today's path (help.answer) beside it
    uv run python scripts/answer_probe.py --only shorthand --repeat 5 --verbose
    uv run python scripts/answer_probe.py --budget 2 --out /tmp/probe.json

The two settings to compare before the switch widens: `--model claude-sonnet-5 --effort low` and
`--model claude-sonnet-5-5 --effort low` (or both in one run: `--compare`).

WHAT "PASS" MEANS, for one run of one case on the new path — all four must hold:
  1. MUST: every `must` fact of the case is in the FINAL ANSWER the person would read (the reply's text, its rendered
     choices and its commands; never the lookups' results, never the start). Read as facts, not as a sentence: the
     must is split into words; the joining words (made, by, lock, next, the, of, and, at, in, a, an) and punctuation
     are dropped; every word left must appear in the answer, case-insensitive, markdown emphasis ignored, at the start
     of a word ("Wed" is found in "Wednesday", "3:00" also as "3" before a non-digit). The JSON also records whether
     the must appeared word for word (`must_literal_missing`), for reading — it does not decide.
  2. NEVER: no `never` value of the case appears in the final answer (case-insensitive, emphasis ignored, word for
     word). A never hit is a visibility failure and fails the whole probe whatever the pass rate.
  3. LOOKUPS: every lookup in the case's `expect_tools` was called (which lookups the model called is always shown).
  4. ENDING: a case marked `asks_back` passes when the reply asked back — choices kept (ending "asked"), or an answer
     that is a question (holds "?"); any other case must end "answered" (asked, acts, acts_off, cap, budget and error
     fail).
On today's path (`--baseline`, help.answer) rules 1, 2 and 4 apply the same way (an answer holding "?" counts as
asking back; acts fails); rule 3 does not (today's path has no lookups) and its column shows "-".

Exit codes: 0 every new-path setting at or over --pass-mark (default 0.8) with no never hit; 1 a new-path setting
under the mark, or any never hit on the new path; 2 nothing run (no API key, an unknown case or tag, a refused work
folder); 3 the probe's own budget was reached before the end (the runs so far are still reported and written).

WHERE EVERYTHING GOES — nothing under the repo's out/, nothing of the live bot's, never the real guild data:
  - the work folder: a new folder under the system temp directory (--work names another; it must be under the system
    temp directory, empty or new, and nowhere inside the real data repo), holding
      cases/<n>-<case id>/data   a fresh copy of fixtures/demo per case (tests/answer_bench.bench_registry), git, no push
      llm_usage.jsonl            the usage rows (OIBOT_USAGE_LOG): the probe's spend, never the live bot's monthly cap
      answers.jsonl              OIBOT_ANSWERS_LOG (the loop itself writes none; set so nothing reaches out/)
      auctions/, armory/         the auction table and armory caches (discord_auctions.OUT, discord_armory.OUT)
      results.json               the full results, unless --out names another file
  - the budget: --budget USD (default 3) is the cap for the whole probe, every setting together; the provider's own
    monthly cap is set to it too, counted from the empty usage file above. Reaching it ends the probe cleanly.
  - the key: ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) from the environment or the repo's .env (--env-file names
    another), read as the bot reads it. It is never printed, logged or written.

Stand-ins: the auction house is the bench's in-memory one (armory.available reads the profile alone, as the bench's
`auction_house` fixture does): no Blizzard request. The command tree is built offline (as scripts/check_commands.py
does) so the system text is the bot's. For --baseline, help.answer gets extras=None (HelpMixin.help_extras needs the
Discord client: its channel links and setup lines are missing on BOTH paths alike) and no conversation or memory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from answer_bench import ASKERS, BENCH, BenchCase, asker_of, bench_registry  # noqa: E402

from oibot_gm import answer_loop, armory, discord_armory, discord_auctions, prices  # noqa: E402
from oibot_gm import help as help_mod  # noqa: E402
from oibot_gm.answer_tools import Viewer  # noqa: E402
from oibot_gm.llm import provider as llm  # noqa: E402

DEFAULT_BUDGET = 3.0
DEFAULT_PASS_MARK = 0.8
ENV_FILE = ROOT / ".env"
JOINING = {"made", "by", "lock", "next", "the", "of", "and", "at", "in", "a", "an"}
ENDINGS = ("answered", "asked", "acts", "acts_off", "cap", "budget", "error")
USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "cost")


# ---------------------------------------------------------------- scoring

def _plain(text: str) -> str:
    """Case-folded, markdown emphasis and code marks gone, curly apostrophes straight, whitespace collapsed."""
    t = (text or "").replace("’", "'").replace("‘", "'")
    t = re.sub(r"[*`~]|(?<!\w)_+|_+(?!\w)", "", t)
    return re.sub(r"\s+", " ", t).casefold().strip()


def fact_words(must: str) -> list[str]:
    """A must's fact words: split on anything that is not a letter, digit, ':' or an apostrophe; the joining words
    dropped."""
    words = re.findall(r"[\w:']+", _plain(must))
    return [w.strip(":'") for w in words if w.strip(":'") and w.strip(":'") not in JOINING]


def _word_in(word: str, text: str) -> bool:
    if re.fullmatch(r"\d{1,2}:00", word):  # "3:00" is also said "3" (never "30")
        pat = r"(?<![\w:])" + re.escape(word[:-3]) + r"(?::00)?(?!\d)"
    else:
        pat = r"(?<![\w'])" + re.escape(word) + (r"(?!\d)" if word[-1].isdigit() else "")
    return re.search(pat, text) is not None


def missing_facts(must: str, answer: str) -> list[str]:
    """The fact words of `must` not in `answer` (both read through _plain)."""
    text = _plain(answer)
    return [w for w in fact_words(must) if not _word_in(w, text)]


def never_hits(nevers: list[str], answer: str) -> list[str]:
    text = _plain(answer)
    return [n for n in nevers if _plain(n) and _plain(n) in text]


def score(case: BenchCase, fmt: dict, run: "Run", path: str) -> None:
    """Fills run.passed and run.reasons by the four rules of the module's docstring."""
    reasons: list[str] = []
    seen = run.answer + ("\n" + " ".join(run.commands) if run.commands else "")
    for m in case.must:
        want = m.format(**fmt)
        miss = missing_facts(want, seen)
        if miss:
            run.must_missing.append({"must": want, "words": miss})
            reasons.append(f"must {want!r} lacks {', '.join(miss)}")
        if _plain(want) not in _plain(seen):
            run.must_literal_missing.append(want)
    run.never_hit = never_hits([n.format(**fmt) for n in case.never], seen)
    reasons += [f"NEVER {n!r} appeared" for n in run.never_hit]
    if path == "loop":
        run.tools_missing = [t for t in case.expect_tools if t not in run.tools]
        reasons += [f"lookup {t} not called" for t in run.tools_missing]
    asked = run.ending == "asked" or (run.ending == "answered" and "?" in run.answer)
    if case.asks_back and not asked:
        reasons.append(f"should ask back, ended {run.ending_words()}")
    elif not case.asks_back and run.ending != "answered":
        reasons.append(f"ended {run.ending_words()}")
    run.reasons = reasons
    run.passed = not reasons


# ---------------------------------------------------------------- metering

class ProbeBudget(llm.BudgetExceeded):
    pass


@dataclass
class Run:
    case: str
    tags: list[str]
    config: str
    model: str
    path: str
    repeat: int
    answer: str = ""
    commands: list[str] = field(default_factory=list)
    choices: list[str] = field(default_factory=list)
    ending: str = "error"
    error: str | None = None
    rounds: int = 0
    requests: int = 0
    tools: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    final_forced: bool | None = None
    usage: dict = field(default_factory=lambda: {k: 0 for k in USAGE_KEYS})
    seconds: float = 0.0
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    must_missing: list[dict] = field(default_factory=list)
    must_literal_missing: list[str] = field(default_factory=list)
    never_hit: list[str] = field(default_factory=list)
    tools_missing: list[str] = field(default_factory=list)

    def ending_words(self) -> str:
        return f"{self.ending} ({self.error})" if self.error else self.ending


def _api_error(e: BaseException) -> str | None:
    """'400 invalid_request_error' for an API status error (its status and the API's short error type, never the
    request or the message), else None."""
    status = getattr(e, "status_code", None)
    if status is None:
        return None
    body = getattr(e, "body", None)
    kind = body.get("error", {}).get("type") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
    return f"{status} {kind or type(e).__name__}"


class Meter:
    """Wraps the provider: every request's usage row kept (per run and in all), the probe's budget checked before each
    request, whether the final request was forced, and an API status error's short form."""

    def __init__(self, inner, budget: float):
        self.inner = inner
        self.budget = budget
        self.rows: list[dict] = []
        self.run_rows: list[dict] = []
        self.forced: list[bool] = []
        self.api_error: str | None = None
        self.name = getattr(inner, "name", "provider")

    @property
    def total(self) -> float:
        return sum(float(r.get("cost") or 0.0) for r in self.rows)

    @property
    def last_usage(self):
        return getattr(self.inner, "last_usage", None)

    def start_run(self) -> None:
        self.run_rows, self.forced, self.api_error = [], [], None

    def _check(self) -> None:
        if self.total >= self.budget:
            raise ProbeBudget(f"The probe's budget of {money(self.budget)} is spent.")

    def _keep(self, row) -> None:
        if row:
            self.rows.append(dict(row))
            self.run_rows.append(dict(row))

    def _call(self, fn, row_of):
        self._check()
        before = getattr(self.inner, "last_usage", None)
        try:
            out = fn()
        except BaseException as e:
            after = getattr(self.inner, "last_usage", None)
            if after is not None and after is not before:  # billed, then raised (a refusal, a cut-off answer)
                self._keep(after)
            self.api_error = _api_error(e) or self.api_error
            raise
        self._keep(row_of(out, before))
        return out

    def converse(self, workload, system, tools, messages, *, force=None):
        self.forced.append(bool(force) and llm.forced_tool_ok(current_model()))
        return self._call(lambda: self.inner.converse(workload, system, tools, messages, force=force),
                          lambda turn, _b: getattr(turn, "usage", None))

    def complete(self, workload, system, user, schema):
        def row(_out, before):
            after = getattr(self.inner, "last_usage", None)
            return after if after is not before else None
        return self._call(lambda: self.inner.complete(workload, system, user, schema), row)


def money(x: float) -> str:
    return f"${x:.2f}" if x >= 1 else f"${x:.4f}"


def current_model() -> str:
    return os.environ.get("OIBOT_MODEL_HELP") or llm.ROUTES["help"][0]


# ---------------------------------------------------------------- the runs

def run_loop(meter: Meter, tree, reg, rs, case: BenchCase, question: str, run: Run) -> None:
    uid, officer = asker_of(case)
    viewer = Viewer.of(reg, rs, uid, officer, public=case.where == "public", question=question, name=ASKERS[case.asker][1])
    res = answer_loop.answer(meter, tree, viewer)
    run.answer, run.commands, run.choices = res.answer.answer, list(res.answer.commands), list(res.choices)
    run.ending, run.rounds, run.requests = res.ending, res.rounds, res.requests
    run.tools, run.not_run = list(res.tools), list(res.not_run)
    run.error = meter.api_error or res.error
    run.final_forced = meter.forced[-1] if meter.forced else None


def run_old(meter: Meter, tree, reg, rs, case: BenchCase, question: str, run: Run) -> None:
    """Today's path as HelpMixin.help_answer drives it with the switch off: help.answer, the same registry, asker and
    flags; extras None (see the module's docstring)."""
    uid, officer = asker_of(case)
    n0 = len(meter.rows)
    try:
        with llm.for_guild(reg.key):
            ans = help_mod.answer(meter, tree, reg, rs, uid, officer, question, None, case.where == "public")
    except llm.BudgetExceeded as e:
        run.ending, run.error, run.answer = "budget", type(e).__name__, str(e)
    except Exception as e:  # noqa: BLE001 — reported, never a crash of the probe
        run.ending, run.error = "error", meter.api_error or type(e).__name__
    else:
        run.answer, run.commands = ans.answer or "", list(ans.commands or [])
        run.ending = "acts" if ans.acts else ("answered" if run.answer.strip() else "error")
        if run.ending == "error":
            run.error = "EmptyAnswer"
    run.requests = len(meter.rows) - n0  # billed requests (a request the API refused with a status error is not billed)


# ---------------------------------------------------------------- environment

@contextmanager
def probe_env(work: Path, budget: float, model: str | None, effort: str | None):
    """Everything the probe changes in the process, put back on the way out: the env vars (usage log, answers log,
    budget caps, the model), the help route's effort, and the out/ folders the auction and armory code read."""
    saved_env = {k: os.environ.get(k) for k in ("OIBOT_USAGE_LOG", "OIBOT_ANSWERS_LOG", "OIBOT_BUDGET_USD",
                                                "OIBOT_BUDGET_USD_HELP", "OIBOT_MODEL_HELP", "OIBOT_ANSWER_TOOLS")}
    saved = (llm.ROUTES["help"], armory.available, discord_auctions.OUT, discord_armory.OUT,
             discord_auctions._STATE, discord_auctions._SHARED)
    os.environ["OIBOT_USAGE_LOG"] = str(work / "llm_usage.jsonl")
    os.environ["OIBOT_ANSWERS_LOG"] = str(work / "answers.jsonl")
    os.environ["OIBOT_BUDGET_USD"] = os.environ["OIBOT_BUDGET_USD_HELP"] = f"{budget:.4f}"
    os.environ.pop("OIBOT_ANSWER_TOOLS", None)
    if model:
        os.environ["OIBOT_MODEL_HELP"] = model
    if effort:
        llm.ROUTES["help"] = (llm.ROUTES["help"][0], effort)
    armory.available = lambda reg: bool(reg.profile.armory)
    discord_auctions.OUT = work / "auctions"
    discord_armory.OUT = work / "armory"
    try:
        yield
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        (llm.ROUTES["help"], armory.available, discord_auctions.OUT, discord_armory.OUT,
         discord_auctions._STATE, discord_auctions._SHARED) = saved
        prices.reset_names()


def _fresh_auctions() -> None:
    discord_auctions._STATE = {}
    discord_auctions._SHARED = {}
    prices.reset_names()


def build_tree(store):
    """The bot's command tree, built offline on the scratch store (scripts/check_commands.py's way); None if that
    fails, said in the header."""
    try:
        import discord
        from discord import app_commands

        from oibot_gm import raidcycle as rc
        from oibot_gm.discord_help import register_help_commands
        from oibot_gm.discord_policy import PolicyContext, register_policy_commands
        from oibot_gm.discord_raid import register_raid_commands
        from oibot_gm.discord_registry import Guilds, register_commands
        from oibot_gm.ops import Ops

        import logging

        logging.getLogger("discord").setLevel(logging.ERROR)  # its voice-support notices are not the probe's
        client = discord.Client(intents=discord.Intents.default())
        tree = app_commands.CommandTree(client)
        guilds = Guilds(store, ROOT)

        class Bot:
            raids = type("R", (), {"store": lambda self, reg: rc.RaidStore(store, reg.key)})()

        register_commands(tree, guilds, Ops(client), lambda k, v: "")
        register_raid_commands(tree, guilds, Ops(client), Bot())
        register_policy_commands(tree, guilds, Ops(client), Bot(), PolicyContext(guilds))
        register_help_commands(tree, guilds, Ops(client), Bot())
        return tree
    except Exception:  # noqa: BLE001
        return None


def _real_data_roots() -> list[Path]:
    roots = [Path(os.environ["OIBOT_DATA_DIR"])] if os.environ.get("OIBOT_DATA_DIR") else []
    roots += [p.parent / "oibot_gm-data" for p in (ROOT, *ROOT.parents)]
    return [r.resolve() for r in roots]


def work_refusal(work: Path) -> str | None:
    """Why the work folder can't be used, or None: it must sit under the system temp directory, never inside the real
    data repo, and be new or empty."""
    w = work.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if any(w == r or r in w.parents for r in _real_data_roots()):
        return f"The work folder {w} is inside the real data repo, so the probe will not use it."
    if not (w == tmp or tmp in w.parents) or w == tmp:
        return f"The work folder {w} is not under the system temp directory ({tmp}), so the probe will not use it."
    if w.exists() and any(w.iterdir()):
        return f"The work folder {w} is not empty, so the probe will not use it."
    return None


# ---------------------------------------------------------------- report

def _med(xs):
    return statistics.median(xs) if xs else 0


def config_totals(runs: list[Run]) -> dict:
    tags = sorted({t for r in runs for t in r.tags} | {"shorthand"})
    per_tag = {}
    for t in tags:
        rs_ = [r for r in runs if t in r.tags]
        per_tag[t] = {"passed": sum(r.passed for r in rs_), "runs": len(rs_)}
    endings = {e: sum(r.ending == e for r in runs) for e in ENDINGS if any(r.ending == e for r in runs)}
    cost = [float(r.usage["cost"]) for r in runs]
    return {
        "runs": len(runs), "passed": sum(r.passed for r in runs),
        "pass_rate": (sum(r.passed for r in runs) / len(runs)) if runs else 0.0,
        "never_hits": sum(bool(r.never_hit) for r in runs), "per_tag": per_tag, "endings": endings,
        "rounds": {"median": _med([r.rounds for r in runs]), "worst": max([r.rounds for r in runs], default=0)},
        "requests": {"median": _med([r.requests for r in runs]), "worst": max([r.requests for r in runs], default=0)},
        "cost": {"median": _med(cost), "worst": max(cost, default=0.0), "total": sum(cost)},
        "seconds": {"median": _med([r.seconds for r in runs]), "worst": max([r.seconds for r in runs], default=0.0)},
        "tokens": {k: sum(int(r.usage.get(k) or 0) for r in runs) for k in USAGE_KEYS[:4]},
        "final_forced": sum(r.final_forced is True for r in runs),
    }


def print_config(label: str, runs: list[Run], cases: list[BenchCase], repeat: int, verbose: bool) -> None:
    print(f"\n== {label}")
    print(f"{'case':<24} {'tags':<10} {'pass':>5} {'ending':<16} {'rnd':>3} {'req':>3} {'tools':<24} "
          f"{'in/out/cr/cw':<22} {'cost':>8} {'secs':>6}")
    for c in cases:
        for r in [x for x in runs if x.case == c.id]:
            tok = "/".join(str(int(r.usage.get(k) or 0)) for k in USAGE_KEYS[:4])
            tools = (",".join(r.tools) or "none") if r.path == "loop" else "-"
            idn = c.id if repeat == 1 else f"{c.id}#{r.repeat + 1}"
            print(f"{idn[:24]:<24} {','.join(r.tags)[:10]:<10} {'pass' if r.passed else 'FAIL':>5} "
                  f"{r.ending_words()[:16]:<16} {r.rounds:>3} {r.requests:>3} {tools[:24]:<24} {tok[:22]:<22} "
                  f"${float(r.usage['cost']):>7.4f} {r.seconds:>6.1f}")
            for why in r.reasons:
                print(f"{'':<4}- {why}")
            if verbose:
                print("    answer: " + (r.answer.replace("\n", "\n            ") or "(none)"))
    t = config_totals(runs)
    print(f"pass rate {t['passed']}/{t['runs']} ({t['pass_rate']:.0%})" + (f" · NEVER HITS {t['never_hits']}" if t["never_hits"] else ""))
    for tag, v in t["per_tag"].items():
        print(f"  tag {tag}: {v['passed']}/{v['runs']}" + ("" if v["runs"] else " (not run)"))
    if repeat > 1:
        print("  per case: " + " · ".join(f"{c.id} {sum(r.passed for r in runs if r.case == c.id)}/"
                                          f"{sum(1 for r in runs if r.case == c.id)}" for c in cases))
    print(f"  rounds median {t['rounds']['median']} worst {t['rounds']['worst']} · requests median {t['requests']['median']} "
          f"worst {t['requests']['worst']} · cost median ${t['cost']['median']:.4f} worst ${t['cost']['worst']:.4f} · "
          f"seconds median {t['seconds']['median']:.1f} worst {t['seconds']['worst']:.1f}")
    print("  endings: " + ", ".join(f"{k} {v}" for k, v in t["endings"].items()) + f" · final request forced in {t['final_forced']}")
    print(f"  spend ${t['cost']['total']:.4f}")


def print_side_by_side(configs: list[str], runs: list[Run], cases: list[BenchCase]) -> None:
    print("\n== side by side (passed/runs · mean cost · mean requests)")
    print(f"{'case':<24} " + " ".join(f"{c[:28]:<28}" for c in configs))
    for c in cases:
        cells = []
        for cf in configs:
            rs_ = [r for r in runs if r.case == c.id and r.config == cf]
            if not rs_:
                cells.append(f"{'not run':<28}")
                continue
            cost = sum(float(r.usage["cost"]) for r in rs_) / len(rs_)
            req = sum(r.requests for r in rs_) / len(rs_)
            cell = f"{sum(r.passed for r in rs_)}/{len(rs_)} ${cost:.4f} {req:.1f}"
            cells.append(f"{cell:<28}")
        print(f"{c.id[:24]:<24} " + " ".join(cells))
    tot = []
    for cf in configs:
        t = config_totals([r for r in runs if r.config == cf])
        cell = f"{t['pass_rate']:.0%} med ${t['cost']['median']:.4f}"
        tot.append(f"{cell:<28}")
    print(f"{'total':<24} " + " ".join(tot))


# ---------------------------------------------------------------- main

def parse(argv):
    ap = argparse.ArgumentParser(description="The live probe of the answer tools over the question bench.")
    ap.add_argument("--model", help="the help route's model for this run (sets OIBOT_MODEL_HELP)")
    ap.add_argument("--effort", help="the help route's effort for this run (low, medium, high)")
    ap.add_argument("--only", "--case", dest="only", action="append", default=[], metavar="ID_OR_TAG",
                    help="run only this case id or tag (repeatable)")
    ap.add_argument("--repeat", type=int, default=1, help="run each case N times")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="dollars for the whole probe (default 3)")
    ap.add_argument("--pass-mark", type=float, default=DEFAULT_PASS_MARK)
    ap.add_argument("--out", type=Path, help="the results JSON (default <work>/results.json)")
    ap.add_argument("--work", type=Path, help="the work folder (default: a new one under the system temp directory)")
    ap.add_argument("--compare", metavar="MODEL", help="run the selection again on a second model, side by side")
    ap.add_argument("--baseline", action="store_true", help="also answer through today's path (help.answer)")
    ap.add_argument("--env-file", type=Path, default=None, help="the .env to read the key from (default the repo's)")
    ap.add_argument("--verbose", action="store_true", help="print each final answer (demo data only)")
    return ap.parse_args(argv)


def select(only: list[str]) -> list[BenchCase] | None:
    if not only:
        return list(BENCH)
    ids, tags = {c.id for c in BENCH}, {t for c in BENCH for t in c.tags}
    if any(o not in ids and o not in tags for o in only):
        return None
    return [c for c in BENCH if c.id in only or set(c.tags) & set(only)]


def main(argv: list[str] | None = None, provider=None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    cases = select(args.only)
    if not cases:
        print(f"No bench case or tag is called {', '.join(args.only)}; the probe ran nothing.")
        return 2
    if args.repeat < 1:
        print("--repeat must be 1 or more; the probe ran nothing.")
        return 2
    if provider is None:
        from dotenv import load_dotenv

        load_dotenv(args.env_file or ENV_FILE)  # never overrides a value already in the environment
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print("ANTHROPIC_API_KEY is not set in the environment or the .env file, so the probe ran nothing.")
            return 2
    work = args.work or Path(tempfile.mkdtemp(prefix="oibot-answer-probe-"))
    why = work_refusal(work)
    if why:
        print(why)
        return 2
    work.mkdir(parents=True, exist_ok=True)
    out = args.out or work / "results.json"

    with probe_env(work, args.budget, args.model, args.effort):
        inner = provider if provider is not None else llm.AnthropicProvider()
        meter = Meter(inner, args.budget)
        primary = current_model()
        models = [primary] + ([args.compare] if args.compare and args.compare != primary else [])
        configs = [(m, p) for m in models for p in (("loop", "old") if args.baseline else ("loop",))]
        label = {(m, p): f"{'new' if p == 'loop' else 'today'} · {m}" for m, p in configs}
        effort = llm.ROUTES["help"][1]

        print(f"answer probe · {len(cases)} cases × {args.repeat} · effort {effort} · budget {money(args.budget)} · pass mark {args.pass_mark:.0%}")
        for m in models:
            known = llm.model_id(m) in llm.PRICES
            print(f"  model {m}: forced reply {'taken' if llm.forced_tool_ok(m) else 'not taken (auto on the last request)'}"
                  + ("" if known else " · NO PRICE ROW: cost counted at its family's or the dearest known price"))
        print(f"  work folder {work} · usage {work / 'llm_usage.jsonl'} · results {out}")
        print("  pass = every must's fact words in the final answer, no never in it, every expected lookup called, "
              "and it answered (or asked back where the case asks back).")

        runs: list[Run] = []
        stopped = False
        tree = None
        tree_said = False
        for i, case in enumerate(cases):
            if stopped:
                break
            reg, rs = bench_registry(work / "cases" / f"{i:02d}-{case.id}")
            if not tree_said:
                tree, tree_said = build_tree(reg.store), True
                if tree is None:
                    print("  the command tree could not be built offline; both paths answer without the command list.")
            _fresh_auctions()
            fmt = case.setup(reg, rs)
            question = case.question.format(**fmt)
            for m, p in configs:
                if stopped:
                    break
                os.environ["OIBOT_MODEL_HELP"] = m
                for k in range(args.repeat):
                    if meter.total >= args.budget:  # spent: nothing more is sent
                        stopped = True
                        break
                    run = Run(case.id, list(case.tags), label[(m, p)], m, p, k)
                    meter.start_run()
                    t0 = time.monotonic()
                    (run_loop if p == "loop" else run_old)(meter, tree, reg, rs, case, question, run)
                    run.seconds = round(time.monotonic() - t0, 2)
                    for row in meter.run_rows:
                        for key in USAGE_KEYS:
                            run.usage[key] += row.get(key) or 0
                    score(case, fmt, run, p)
                    runs.append(run)
                    if run.ending == "budget":
                        stopped = True
                        break
        os.environ["OIBOT_MODEL_HELP"] = primary

        order = [label[c] for c in configs]
        for cf in order:
            print_config(cf, [r for r in runs if r.config == cf], cases, args.repeat, args.verbose)
        if len(order) > 1:
            print_side_by_side(order, runs, cases)

        totals = {cf: config_totals([r for r in runs if r.config == cf]) for cf in order}
        failed = [cf for (m, p), cf in zip(configs, order)
                  if p == "loop" and (totals[cf]["pass_rate"] < args.pass_mark or totals[cf]["never_hits"])]
        spend = meter.total
        result = {"cases": [c.id for c in cases], "repeat": args.repeat, "effort": effort, "budget": args.budget,
                  "pass_mark": args.pass_mark, "configs": order, "totals": totals, "spend": spend,
                  "stopped_by_budget": stopped, "runs": [r.__dict__ for r in runs], "work": str(work),
                  "usage_log": str(work / "llm_usage.jsonl")}
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str))

        print(f"\nTotal spend ${spend:.4f} over {len(meter.rows)} requests. Results in {out}.")
        if stopped:
            print(f"Stopped: the probe's budget of {money(args.budget)} was reached after {len(runs)} run{'' if len(runs) == 1 else 's'}; the rest were not run.")
            return 3
        for cf in failed:
            print(f"UNDER THE MARK: {cf}" + (" (a never value appeared)" if totals[cf]["never_hits"] else ""))
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
