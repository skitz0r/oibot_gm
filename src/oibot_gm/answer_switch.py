"""The answer tools' switch and their answers log (answer tools, design §9). `enabled(guild_key)` = does this guild's
question go through the lookup loop (answer_loop.answer) instead of help.answer? Read from OIBOT_ANSWER_TOOLS at call
time: unset, empty or `off` → no guild; `on` → every guild; otherwise a comma- or space-separated list of guild keys.
A value that is neither reads as off and is logged once. `log_answer` writes one row per loop answer to
out/answers.jsonl (OIBOT_ANSWERS_LOG overrides the path, read at call time) — as out/listen.jsonl, NO question, NO
answer text and no member names. answer_loop re-exports every name here."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .answer_loop import AnswerResult

log = logging.getLogger(__name__)

SWITCH_ENV = "OIBOT_ANSWER_TOOLS"
LOG_ENV = "OIBOT_ANSWERS_LOG"
ANSWERS_LOG_DEFAULT = Path(__file__).resolve().parents[2] / "out" / "answers.jsonl"  # the bot's out/, as listen.jsonl
ANSWERS_MAX_BYTES = 5_000_000  # past it the file becomes answers.jsonl.1 (an older .1 replaced) before the append

# where a question came in: an @mention, a DM, an overheard message (listening), /ask, the site's box, an act channel
# (the parser first), /gm change, the loot council's thread
ENTRIES = ("mention", "dm", "listen", "ask", "site", "act", "change", "loot")

_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_warned: set[str] = set()  # malformed switch values already logged (once each)


def enabled(guild_key: str) -> bool:
    """Is the lookup loop on for this guild? The environment is read now, never cached."""
    raw = (os.environ.get(SWITCH_ENV) or "").strip()
    if not raw or raw.lower() == "off":
        return False
    if raw.lower() == "on":
        return True
    keys = [k for k in re.split(r"[,\s]+", raw) if k]
    if not keys or not all(_KEY.match(k) for k in keys):
        if raw not in _warned:
            _warned.add(raw)
            log.warning("%s is neither off, on nor a list of guild keys, so it reads as off", SWITCH_ENV)
        return False
    return guild_key in keys


def entry_kw(guild_key: str, entry: str) -> dict:
    """help_answer's `entry` keyword for a caller, only while the switch is on for the guild: with it off every call
    is made exactly as before the switch existed."""
    return {"entry": entry} if enabled(guild_key) else {}


def answers_log() -> Path:
    """The answers log's path, read now."""
    raw = os.environ.get(LOG_ENV)
    return Path(raw) if raw else ANSWERS_LOG_DEFAULT


def log_answer(guild_key: str, entry: str, result: "AnswerResult") -> None:
    """One row for one loop answer: when, the guild, the entry point, rounds, requests, the lookups by name, how it
    ended and the usage. Never the question, the answer or a name. A failure to write is logged, never raised."""
    from .answer_tools import TOOLS, UNKNOWN_SOURCE

    u = result.usage or {}
    known = set(TOOLS) | {"reply"}  # a name the model made up is its words: logged as "unknown", never as written
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "guild": guild_key, "entry": entry,
           "rounds": result.rounds, "requests": result.requests, "tools": [t if t in known else UNKNOWN_SOURCE for t in map(str, result.tools)],
           "ending": result.ending, "input": u.get("input") or 0, "output": u.get("output") or 0,
           "cache_read": u.get("cache_read") or 0, "cache_write": u.get("cache_write") or 0,
           "cost": round(float(u.get("cost") or 0.0), 6)}
    path = answers_log()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > ANSWERS_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.warning("answers log not written (%s)", type(e).__name__)
