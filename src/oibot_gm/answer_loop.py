"""The answer loop (answering questions with lookup tools, design §3, §6): one bounded conversation with the model, in
which it reads guild facts through the read-only lookups of answer_tools and ends with the `reply` tool.

Code, not the prompt, enforces every limit of §6: at most MAX_ROUNDS lookup rounds, MAX_CALLS_PER_ROUND lookups a round
(the rest answered TOO_MANY_LOOKUPS), TOTAL_RESULT_CHARS of lookup results in one answer (a result that would cross it
is answered RESULTS_FULL and the next request is the last), MAX_REQUESTS model requests (a hard stop), and the reply's
ANSWER_CHARS. The last request offers the model the forced `reply` (where the model takes a forced tool — converse
decides, provider.forced_tool_ok) and always carries FINAL_NUDGE; a model that still only looks something up gets the
honest ANSWER_NOT_FOUND, never a guess (text it wrote beside the lookup is its answer). A failed request ends with
ANSWER_FAILED: nothing was looked up, so "couldn't find" would be untrue.

Messages: the first user message is one text block (`first_user`); each assistant message is the turn's `content`
exactly as converse gave it (thinking blocks and their signatures included, never edited); every lookup result of a
round goes back in ONE user message, a tool_result per call in the order called, each with its call's id. The tool list
(answer_tools.tools_for(profile) + reply_spec()) and the system text never change within one answer, so the prompt
cache holds. `reply`'s fields are checked here: the page against help.PAGES (an officers' page dropped for a viewer who
is not one), each choice against NAMES — the resolver's candidates exactly, or a whole-word phrase of the lookup
results (never the question, the wrapper lines or the system text) —, the answer cut to ANSWER_CHARS. Nothing here writes anything."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Literal

from . import help as help_mod
from .answer_tools import BIG_RESULT_CAP, RESULT_CAP, Viewer, run_tool, tools_for
from .help import HelpAnswer
from .llm.provider import BudgetExceeded, CutShort, Refused

log = logging.getLogger(__name__)

__all__ = ["MAX_ROUNDS", "MAX_CALLS_PER_ROUND", "RESULT_CAP", "BIG_RESULT_CAP", "TOTAL_RESULT_CHARS", "MAX_REQUESTS",
           "ANSWER_CHARS", "MAX_CHOICES", "REPLY", "reply_spec", "ANSWER_NOT_FOUND", "ANSWER_FAILED", "TOO_MANY_LOOKUPS", "RESULTS_FULL",
           "FINAL_NUDGE", "KIND_PAGE", "AnswerResult", "run", "answer"]

MAX_ROUNDS = 3              # lookup rounds per answer (design §6)
MAX_CALLS_PER_ROUND = 4     # lookups in one round; the rest are answered TOO_MANY_LOOKUPS
TOTAL_RESULT_CHARS = 12_000  # every lookup result of one answer together
MAX_REQUESTS = 4            # model requests per answer: a hard stop
ANSWER_CHARS = 1_200        # the reply, as today
MAX_CHOICES = 5             # the names a clarifying question offers
MAX_COMMANDS = 4            # HelpAnswer's commands, as today
CHOICE_MIN = 3              # a choice shorter than this matches almost any text, so it is never kept
CHOICE_MAX = 100            # nor one longer than a name

REPLY = "reply"

# what the asker reads when the loop ends without an answer (the Copy rule: say what happened, never a guess)
ANSWER_NOT_FOUND = "I couldn't find the answer in the guild's records, so I haven't guessed."
# what the asker reads when a request failed (refused, cut off, the API or the network): nothing was looked up, so
# ANSWER_NOT_FOUND would be untrue. The one copy: discord_help.help_answer reads it from here.
ANSWER_FAILED = "Something went wrong, so I couldn't answer. The officers have the details."
# what the model reads (lint-clean too)
TOO_MANY_LOOKUPS = "Too many lookups in one step, so this one was skipped."
RESULTS_FULL = "The lookups for this answer have used their room. Reply now with what you have."
FINAL_NUDGE = "This is the last step: call reply now with the answer, or with choices."

# the page with the full view of a kind of thing the resolver knew — the link under an honest ending
KIND_PAGE = {"recipe": "professions", "profession": "professions", "item": "prices", "member": "members",
             "character": "members", "raid": "raids", "schedule": "schedules", "command": "help", "manual": "help"}

Ending = Literal["answered", "asked", "acts", "cap", "budget", "error"]
USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "cost")


def _field_doc(name: str) -> str:
    return HelpAnswer.model_fields[name].description or ""


def reply_spec() -> dict:
    """The `reply` tool: HelpAnswer's fields plus `choices`, strict, every field required. The array limits (commands,
    choices) and the answer's length are in the descriptions and enforced in code (_checked): strict tool schemas
    don't take array or string length constraints."""
    props = {
        "answer": {"type": "string", "description": _field_doc("answer")},
        "commands": {"type": "array", "items": {"type": "string"},
                     "description": f"At most {MAX_COMMANDS}. " + _field_doc("commands")},
        "page": {"type": "string", "enum": list(help_mod.PAGES) + [""], "description": _field_doc("page")},
        "officer_view": {"type": "boolean", "description": _field_doc("officer_view")},
        "for_officer": {"type": "boolean", "description": _field_doc("for_officer")},
        "acts": {"type": "boolean", "description": _field_doc("acts")},
        "choices": {"type": "array", "items": {"type": "string"},
                    "description": f"Only for a clarifying question, else empty: at most {MAX_CHOICES} names exactly as "
                                   "a lookup or the candidates showed them; `answer` is then the question. A name no "
                                   "lookup showed is dropped."},
    }
    return {"name": REPLY, "strict": True,
            "description": "Ends the answer. Call it once, with the answer, or with a clarifying question and its choices.",
            "input_schema": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}}


@dataclass
class AnswerResult:
    """What one answer came to. `answer` = what help_answer posts today (answer text with the choices rendered,
    commands, page, officer_view, for_officer, acts); the rest is for the answers log: `ending` how it ended, `rounds`
    lookup rounds, `requests` model requests sent (a refused or cut-off one counts — it was billed), `tools` every
    lookup the model asked for, in order — `not_run` those of them that never ran (asked beside `reply`, or on the
    last request) —, `usage` the requests' tokens and cost summed, `error` the failure's type name."""
    answer: HelpAnswer
    ending: Ending
    rounds: int
    requests: int
    tools: list[str]
    usage: dict
    error: str | None = None
    choices: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _strings(v) -> list[str]:
    return [s.strip() for s in v if isinstance(s, str) and s.strip()] if isinstance(v, list) else []


def _page(key, viewer: Viewer) -> str:
    """A PAGES key (a gone page's name read as the page that took its place), or "" for none, a made-up page or an
    officers' page for a viewer who is not one — help.page_link's rule."""
    k = _text(key).lower()
    k = help_mod.PAGE_ALIASES.get(k, k)
    row = help_mod.PAGES.get(k)
    return "" if row is None or (row[2] and not viewer.officer) else k


def _cut_answer(text: str, room: int) -> str:
    """At most `room` characters: at the last sentence's end when that keeps at least half, else on a whole word
    with an ellipsis."""
    if len(text) <= room:
        return text
    if room <= 1:
        return "…"[:room]
    head = text[: room - 1]
    end = max(head.rfind(p) for p in (". ", "! ", "? ", "\n"))
    if end >= room // 2:
        return head[: end + 1].rstrip()
    space = head.rfind(" ")
    if space > 0:
        head = head[:space]
    return head.rstrip(" ,;:—-") + "…"


_WRAPPER = re.compile(r'^<data source="[^"]*">$|^</data>$', re.M)


def _result_body(text: str) -> str:
    """A lookup result without its data-block wrapper lines: a choice is never found in the wrapper's own words."""
    return _WRAPPER.sub("", text)


def _is_name(choice: str, names: set[str], results: str) -> bool:
    """A choice is a NAME the answer was shown: one of the resolver's names exactly, or a whole-word-bounded phrase of
    the lookup results' text (never a fragment of a longer word, never the asker's question nor the system text)."""
    c = choice.casefold()
    if c in names:
        return True
    return re.search(r"(?<!\w)" + re.escape(c) + r"(?!\w)", results) is not None


def _checked(fields: dict, viewer: Viewer, names: set[str], results: str) -> tuple[HelpAnswer, list[str]]:
    """The reply's fields as code allows them (design §7): (the HelpAnswer, the choices kept). `names` = the resolver's
    names (case-folded), `results` = the lookup results' bodies (case-folded)."""
    choices: list[str] = []
    for c in _strings(fields.get("choices")):
        if CHOICE_MIN <= len(c) <= CHOICE_MAX and _is_name(c, names, results) and c.casefold() not in (x.casefold() for x in choices):
            choices.append(c)
        if len(choices) == MAX_CHOICES:
            break
    lines = "".join(f"\n• {c}" for c in choices)
    answer = _cut_answer(_text(fields.get("answer")), ANSWER_CHARS - len(lines))
    return HelpAnswer(answer=(answer + lines).strip(), commands=_strings(fields.get("commands"))[:MAX_COMMANDS],
                      page=_page(fields.get("page"), viewer), officer_view=fields.get("officer_view") is True,
                      for_officer=fields.get("for_officer") is True, acts=fields.get("acts") is True), choices


def run(provider, system: str, first_user: str, viewer: Viewer, *, known_kind: str | None = None,
        names: Iterable[str] = (), workload: str = "help") -> AnswerResult:
    """One question answered through the lookups, every limit enforced here. BLOCKING (model requests and lookups):
    callers run it in a thread. Never raises: a spent budget ends "budget" with the provider's own sentence (no page);
    a refused or cut-off response or any other failure ends "error" with ANSWER_FAILED; an empty turn ends "error" with
    ANSWER_NOT_FOUND; a model that keeps looking things up past its last request ends "cap" with ANSWER_NOT_FOUND,
    unless that last turn also wrote text, which is then the answer. `names` = the resolver's candidates' names for
    this question (NameMatch.name): with the lookup results, what a choice may be."""
    tools = [t.spec() for t in tools_for(viewer.reg.profile)] + [reply_spec()]
    messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": first_user}]}]
    shown = {str(n).casefold() for n in names if str(n).strip()}
    results_seen = ""
    rounds = requests = used = 0
    full = False  # a result was refused for room: the next request is the last
    called: list[str] = []
    not_run: list[str] = []
    usage = {k: 0 for k in USAGE_KEYS}
    usage["cost"] = 0.0

    def add_usage(row) -> None:
        for k in USAGE_KEYS:
            usage[k] += (row or {}).get(k) or 0

    def done(ending: Ending, answer: HelpAnswer | None = None, error: str | None = None,
             choices: list[str] | None = None, sentence: str = ANSWER_NOT_FOUND) -> AnswerResult:
        if answer is None:  # an ending without an answer: its honest sentence, linking the page of the kind when known
            answer = HelpAnswer(answer=sentence, page=_page(KIND_PAGE.get(known_kind or "", ""), viewer))
        return AnswerResult(answer, ending, rounds, requests, called, usage, error, choices or [], not_run)

    def ended_by(fields: dict) -> AnswerResult:
        ans, choices = _checked(fields, viewer, shown, results_seen)
        if ans.acts:
            return done("acts", ans, choices=choices)
        if choices:
            return done("asked", ans, choices=choices)
        if ans.answer:
            return done("answered", ans)
        return done("cap")  # an empty reply: nothing to post

    try:
        while True:
            # the requests clause is a safeguard: with MAX_ROUNDS = 3 and MAX_REQUESTS = 4 the rounds clause reaches it first
            final = rounds >= MAX_ROUNDS or used >= TOTAL_RESULT_CHARS or full or requests >= MAX_REQUESTS - 1
            if final:
                messages[-1]["content"].append({"type": "text", "text": FINAL_NUDGE})
            try:
                turn = provider.converse(workload, system, tools, messages, force=REPLY if final else None)
            except (Refused, CutShort):  # sent and billed: its usage row is the provider's last
                requests += 1
                add_usage(getattr(provider, "last_usage", None))
                raise
            requests += 1
            add_usage(turn.usage)
            messages.append({"role": "assistant", "content": turn.content})
            reply_call = next((c for c in turn.calls if c.name == REPLY), None)
            if reply_call is not None:  # the ending; any other call of that turn is never run, only listed
                others = [str(c.name)[:64] for c in turn.calls if c is not reply_call]
                called.extend(others)
                not_run.extend(others)
                return ended_by(reply_call.input if isinstance(reply_call.input, dict) else {})
            if turn.calls:
                called.extend(str(c.name)[:64] for c in turn.calls)
                if final:  # told to reply, and didn't: a written answer is still the answer, else the honest sentence
                    not_run.extend(str(c.name)[:64] for c in turn.calls)
                    return ended_by({"answer": turn.text}) if turn.text.strip() else done("cap")
                rounds += 1
                results = []
                for i, c in enumerate(turn.calls):
                    if i >= MAX_CALLS_PER_ROUND:
                        text, is_error = TOO_MANY_LOOKUPS, True
                    elif full or used >= TOTAL_RESULT_CHARS:
                        text, is_error = RESULTS_FULL, True
                    else:
                        text, is_error = run_tool(viewer, c.name, c.input if isinstance(c.input, dict) else {})
                        if used + len(text) > TOTAL_RESULT_CHARS:  # would cross the answer's room: refused in one line
                            text, is_error, full = RESULTS_FULL, True, True
                        else:
                            used += len(text)
                            results_seen += "\n" + _result_body(text).casefold()
                    results.append({"type": "tool_result", "tool_use_id": c.id, "content": text, "is_error": is_error})
                messages.append({"role": "user", "content": results})
                continue
            if turn.text.strip():
                return ended_by({"answer": turn.text})
            return done("error", error="EmptyTurn")
    except BudgetExceeded as e:  # its sentence is written for the asker (provider._check_budget); no page under it
        return done("budget", HelpAnswer(answer=str(e)), error=type(e).__name__)
    except (Refused, CutShort) as e:
        log.warning("answer loop: %s", e)
        return done("error", error=type(e).__name__, sentence=ANSWER_FAILED)
    except Exception as e:  # noqa: BLE001 — any failure ends with its own sentence, never a crash or a guess
        log.exception("answer loop failed")
        return done("error", error=type(e).__name__, sentence=ANSWER_FAILED)


def answer(provider, tree, viewer: Viewer, *, conversation: str | None = None, memory: str | None = None) -> AnswerResult:
    """One question, end to end (design §3): the names it holds resolved in code (answer_names.resolve), the small
    start (answer_start.user_text) under the constant system text, then the loop — inside for_guild, so every usage
    row is the guild's. What help_answer hands help.answer today travels in the Viewer (who asks, where the reply is
    read, the question, the bot-side extras and the loot council's data); `conversation` / `memory` as help.answer
    takes them. BLOCKING: callers run it in a thread. Never raises (run's endings; a failure before the first request
    ends "error" with ANSWER_FAILED)."""
    from .answer_names import resolve
    from .answer_start import system_text, user_text
    from .llm.provider import for_guild

    reg = viewer.reg
    with for_guild(reg.key):  # D45: the usage rows are this guild's
        try:
            matches, total = resolve(reg, viewer.question, tree=tree)
            system = system_text(tree, reg.profile)
            first = user_text(viewer, viewer.question, matches, total, conversation=conversation, memory=memory)
        except Exception as e:  # noqa: BLE001 — nothing was asked yet: the failed-request sentence, never a crash
            log.exception("answer start failed")
            return AnswerResult(HelpAnswer(answer=ANSWER_FAILED), "error", 0, 0, [], {**{k: 0 for k in USAGE_KEYS}, "cost": 0.0},
                                type(e).__name__)
        return run(provider, system, first, viewer, known_kind=matches[0].kind if matches else None,
                   names=[m.name for m in matches])
