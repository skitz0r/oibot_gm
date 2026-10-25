"""Plain-text conversational memory (design §5.51): the last few exchanges of one person in one place, so a follow-up
("can you just @ him here after opening it?", "7:30 PM", "yes, tank") is read with what it follows.

In memory only — never the data repo; a restart forgets. One ring per (guild key, channel id or DM, Discord id) of the
last N exchanges. `recall` returns what a new message continues: a Discord REPLY to one of the bot's plain-text messages
continues that exchange whatever its age (while the ring still holds it); otherwise the same person in the same place
within CONTEXT_MINUTES. Another person's exchanges in the same channel are never someone's context (the ring is keyed
by uid). `context_block` renders them as ONE delimited data block for the parser / the help answer: every piece of text
(the person's and the bot's own replies, which can quote member text) goes through llm.provider.unfence, so nothing in
it can close the block. The web keeps no server memory: the Tell the bot box sends its own last exchanges
(`exchanges_from_client`)."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .llm.provider import unfence

N = 4  # exchanges kept per ring
CONTEXT_MINUTES = 20  # a follow-up that is not a Discord reply continues only within this window
MAX_RINGS = 2000  # a bound on the process's memory: the least recently used ring is dropped beyond it
REQUEST_CAP = 1500  # each turn's text in the block
REPLY_CAP = 1200
QUESTION_CAP = 300
FENCE_TAGS = ("conversation", "request", "question", "data", "member-text")

DM = "dm"


@dataclass
class Exchange:
    at: float  # epoch seconds
    uid: int
    channel_id: int | None  # None = DMs (or the site)
    request: str
    reply: str = ""
    questions: list[str] = field(default_factory=list)
    message_id: int | None = None  # the person's message (None for a slash command or the site)
    reply_message_id: int | None = None  # the bot's reply (what a Discord reply references)


_rings: OrderedDict[tuple, deque] = OrderedDict()
_lock = threading.Lock()


def _key(reg_or_key, channel_id, uid) -> tuple:
    gk = reg_or_key if isinstance(reg_or_key, str) else getattr(reg_or_key, "key", str(reg_or_key))
    return (gk, DM if channel_id is None else int(channel_id), int(uid))


def remember(reg, channel_id, uid: int, request: str, reply: str = "", questions=None, *, message_id: int | None = None,
             reply_message_id: int | None = None, at: float | None = None) -> Exchange:
    """Keep one exchange (in memory only). The caller remembers after it replied, with the bot's reply message id."""
    ex = Exchange(at=time.time() if at is None else at, uid=int(uid), channel_id=channel_id, request=str(request or "")[:2000],
                  reply=str(reply or "")[:2000], questions=[str(q)[:QUESTION_CAP] for q in (questions or [])][:6],
                  message_id=message_id, reply_message_id=reply_message_id)
    k = _key(reg, channel_id, uid)
    with _lock:
        ring = _rings.get(k)
        if ring is None:
            ring = _rings[k] = deque(maxlen=N)
        ring.append(ex)
        _rings.move_to_end(k)
        while len(_rings) > MAX_RINGS:
            _rings.popitem(last=False)
    return ex


def recall(reg, channel_id, uid: int, *, reply_to_message_id: int | None = None, now: float | None = None) -> list[Exchange]:
    """The exchanges a new message from `uid` in `channel_id` continues, oldest first. A reply to one of the bot's
    plain-text messages: that exchange and the ones before it within CONTEXT_MINUTES of it, regardless of its age.
    Otherwise the exchanges within CONTEXT_MINUTES of now. [] when there is nothing to continue."""
    now = time.time() if now is None else now
    window = CONTEXT_MINUTES * 60
    with _lock:
        ring = list(_rings.get(_key(reg, channel_id, uid)) or ())
    if not ring:
        return []
    if reply_to_message_id is not None:
        for i, ex in enumerate(ring):
            if ex.reply_message_id is not None and ex.reply_message_id == reply_to_message_id:
                return [e for e in ring[: i + 1] if ex.at - e.at <= window]
    return [e for e in ring if now - e.at <= window]


def forget(reg=None) -> None:
    """Drop every ring (of one guild when `reg` is given) — tests, and a guild that leaves."""
    with _lock:
        if reg is None:
            _rings.clear()
            return
        gk = reg if isinstance(reg, str) else reg.key
        for k in [k for k in _rings if k[0] == gk]:
            del _rings[k]


def context_block(exchanges) -> str:
    """ONE delimited data block: each turn as `asker:` / `bot:` lines and the bot's open questions under `bot asked:`.
    Every piece of text is fenced (unfence) — a request holding '</conversation>' cannot close the block. '' for none."""
    rows = []
    for ex in exchanges or ():
        req = unfence(str(getattr(ex, "request", "") or "").strip()[:REQUEST_CAP], *FENCE_TAGS)
        rep = unfence(str(getattr(ex, "reply", "") or "").strip()[:REPLY_CAP], *FENCE_TAGS)
        if not req and not rep:
            continue
        rows.append("asker: " + req.replace("\n", "\n  "))
        if rep:
            rows.append("bot: " + rep.replace("\n", "\n  "))
        qs = [unfence(str(q).strip()[:QUESTION_CAP], *FENCE_TAGS) for q in (getattr(ex, "questions", None) or []) if str(q).strip()]
        if qs:
            rows.append("bot asked:\n" + "\n".join(f"  - {q}" for q in qs))
    if not rows:
        return ""
    return "<conversation>\n" + "\n".join(rows) + "\n</conversation>"


def exchanges_from_client(items, uid: int = 0) -> list[Exchange]:
    """The site's Tell the bot box sends its own last exchanges as [{request, reply, questions?}]: shaped (at most N,
    text capped) into Exchanges for context_block. Never stored."""
    out = []
    if not isinstance(items, list):
        return out
    for it in items[-N:]:
        if not isinstance(it, dict):
            continue
        qs = it.get("questions")
        out.append(Exchange(at=time.time(), uid=uid, channel_id=None, request=str(it.get("request") or "")[:2000],
                            reply=str(it.get("reply") or "")[:2000],
                            questions=[str(q) for q in qs][:6] if isinstance(qs, list) else []))
    return out


def reply_to_id(message) -> int | None:
    """The message id a Discord message replies to (None when it is not a reply)."""
    ref = getattr(message, "reference", None)
    mid = getattr(ref, "message_id", None) if ref is not None else None
    return int(mid) if mid else None
