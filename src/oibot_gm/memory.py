"""Chat memory (design §5.61, a spike for now — nothing in the live bot calls this yet).

What the bot remembers is what a member SAID, kept verbatim with who, where and when, and repeated later as their words
("Mara said on Oct 30 that …") — never restated as the bot's own claim. Three parts, all offline-capable:

- the REMEMBER question (`REMEMBER_QUESTION` / `REMEMBER_CRITERIA`): a Jev noul over a listened message, "is this a
  fact someone could ask about later?" — meant to ride in the same request as listening's gate;
- `MemoryStore`: one SQLite file per host (rows per guild) with FTS5 for the words and the embeddings as blobs, searched
  by brute force in numpy (a guild's memories are thousands, not millions); `recall` fuses the lexical and the vector
  ranks (reciprocal rank fusion) and honours the channel scope (`shared` recalled anywhere, `channel` only in its own);
- the RELEVANCE question (`relevance_question`): a Jev choice over the recalled candidates, "which one answers this,
  if any?" — the judge that replaces a cosine threshold; code keeps the options whose probability clears `RELEVANT`.

The embedder is local (bge-small-en-v1.5 through fastembed / ONNX Runtime; the model is ~130 MB, downloaded once):
member chat never leaves the machine. Member text is data: everything typed goes into the state as JSON values, and
the memory text in a prompt goes through `llm.provider.unfence`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIMS = 384
SCOPES = ("shared", "channel")  # shared: recalled anywhere the bot answers; channel: only when answering in that channel
REMEMBER_AT = 0.5  # the remember question's yes line
RELEVANT = 0.7  # a recalled memory is cited when the judge gives it at least this (the spike: true answers scored 0.85–1.0,
#   the wrong ones 0.53–0.62)
RECALL_K = 5  # candidates handed to the judge
RRF_K = 60  # reciprocal rank fusion's constant (the usual 60)
TEXT_MAX = 600  # a memory keeps at most this much of the message

REMEMBER_QUESTION = (
    "Does `message.text` state something a guild member might later ask the bot about: a fact about this guild, its "
    "people, its plans or the game — where something drops or is bought, what someone has or can do, how the guild does "
    "something, a decision, a tip? The bot would keep the message as something `message.author` said, with the date, "
    "and repeat it as their words when asked. Yes only for a statement worth repeating later. No for a question, a "
    "request, a reaction, a joke, small talk, a greeting, a link alone, a plan for the next few minutes, or a reply "
    "that makes no sense without the message before it. `bot` says what the bot already records in its own data; a "
    "message that only changes a record (a signup answer, an absence, a character change) is handled there and is "
    "not worth remembering unless it also states a fact others would ask about. `recent` is the chat just before."
)
REMEMBER_CRITERIA = {
    "true": "The message states a fact or a tip about the guild, its people, its plans or the game that someone could "
            "ask about later.",
    "false": "The message is a question, a request, a reaction, a joke, small talk, a link alone, or chat that makes no "
             "sense on its own.",
}
RELEVANCE_INSTRUCTIONS = (
    "Which remembered message in `memory`, if any, answers or directly informs `message.text`? Each option is one "
    "remembered message (who said it and when, then its words). Pick the one whose words answer the message best. Pick "
    "`none` when no remembered message answers it — a memory that only shares a word or a topic does not answer it."
)
NONE = "none"
REMEMBER_AT_ENV = "OIBOT_REMEMBER_AT"  # the host may RAISE the keep line (never lower it)


def remember_at() -> float:
    """The remember question's keep line: REMEMBER_AT, or the host's higher one."""
    try:
        v = float(os.environ.get(REMEMBER_AT_ENV, "") or REMEMBER_AT)
    except ValueError:
        return REMEMBER_AT
    return v if REMEMBER_AT <= v <= 1.0 else REMEMBER_AT


def memory_key(guild: str, message_id: int | None, at: str = "", author_id: int = 0) -> str:
    """A memory's id: stable across a rebuild of the index, never a row number. A Discord message has one; a row
    without a message id (an import) keys on who and when."""
    base = f"{guild}:{message_id}" if message_id else f"{guild}:{author_id}:{at}"
    return "m-" + hashlib.sha1(base.encode()).hexdigest()[:8]


@dataclass
class Memory:
    id: str  # the key (memory_key): stable across a rebuild of the index
    guild: str
    channel: int
    channel_name: str
    scope: str
    author_id: int
    author: str
    at: str  # ISO, UTC
    text: str
    p: float | None = None  # the remember question's answer when it was kept
    context: str = ""  # the message it replied to ("Mara: …"), when it was a Discord reply — a reply alone says little
    rowid: int = 0  # the store's own row number (its joins with the words index); never an id anyone sees

    def when(self) -> str:
        """'30 Oct' / '30 Oct 2025' — the date a citation carries (never a time: people quote days)."""
        try:
            d = datetime.fromisoformat(self.at)
        except ValueError:
            return self.at[:10]
        now = datetime.now(timezone.utc)
        return d.strftime("%-d %b") if d.year == now.year else d.strftime("%-d %b %Y")

    def citation(self) -> str:
        """The words an answer repeats: who said it, when, and what — verbatim (and what they replied to, when known)."""
        tail = f" (replying to {self.context})" if self.context else ""
        return f"{self.author} said on {self.when()}: {self.text}{tail}"


def index_text(author: str, text: str, context: str = "") -> str:
    """What the words index and the embedding see: the speaker's name is part of it (questions name people — "what is
    Rolf going?" must find Rolf's own words), and the message replied to when there is one."""
    return f"{author}: {text}" + (f" (replying to {context})" if context else "")


@dataclass
class Candidate:
    memory: Memory
    cosine: float
    bm25_rank: int | None  # 1-based rank in the lexical list, None when the words never matched
    vec_rank: int
    score: float  # reciprocal rank fusion
    judged: float | None = None  # the relevance judge's probability, once asked
    name_rank: int | None = None  # 1-based rank among the named authors' newest words, None when no named author wrote it


def remember_state(bot_words: str, channel_name: str, recent: list[dict], author: str, text: str) -> dict:
    """The remember question's state: what the bot records, where the message was written, the chat before, the message."""
    return {"bot": bot_words, "channel": {"name": channel_name}, "recent": recent[-2:],
            "message": {"author": author, "text": text[:TEXT_MAX]}}


def memory_state(candidates: list[Candidate]) -> list[dict]:
    """The `memory` key of a decision state: the recalled memories as data (id m1.., who, when, words, what it replied to)."""
    return [{"id": f"m{i + 1}", "author": c.memory.author, "when": c.memory.when(), "text": c.memory.text,
             **({"replying_to": c.memory.context} if c.memory.context else {})} for i, c in enumerate(candidates)]


def relevance_criteria(candidates: list[Candidate]) -> dict:
    """The judge's options: m1..mN carry each memory's citation, plus `none`."""
    criteria = {f"m{i + 1}": c.memory.citation() for i, c in enumerate(candidates)}
    criteria[NONE] = "No remembered message answers it."
    return criteria


def relevance_question(message: str, candidates: list[Candidate]) -> tuple[dict, str, dict]:
    """(state, instructions, criteria) for the judge alone (an @mention, a DM, the site)."""
    return {"message": {"text": message[:TEXT_MAX]}, "memory": memory_state(candidates)}, RELEVANCE_INSTRUCTIONS, relevance_criteria(candidates)


def questions(listen_instructions: str, listen_criteria: dict, candidates: list[Candidate], remember: bool) -> dict:
    """ONE Jev request's questions against the listening state: `decision` (is it for the bot), `remember` (worth
    keeping — only in a channel that remembers) and `relevance` (which recalled memory answers it — only with candidates)."""
    q = {"decision": {"type": "noul", "instructions": listen_instructions, "criteria": dict(listen_criteria)}}
    if remember:
        q["remember"] = {"type": "noul", "instructions": REMEMBER_QUESTION, "criteria": dict(REMEMBER_CRITERIA)}
    if candidates:
        q["relevance"] = {"type": "choice", "instructions": RELEVANCE_INSTRUCTIONS, "criteria": relevance_criteria(candidates)}
    return q


def _noul(answers: dict, name: str) -> float | None:
    try:
        v = float(answers[name]["noul"])
    except (KeyError, TypeError, ValueError):
        return None
    return v if 0.0 <= v <= 1.0 else None


def read_answers(answers: dict | None, candidates: list[Candidate]) -> tuple[float | None, float | None, list[Candidate]]:
    """(the listen probability, the remember probability or None when not asked, the judged candidates ≥ RELEVANT,
    best first). A failed request (None) is None, None, []."""
    if not answers:
        return None, None, []
    p, r = _noul(answers, "decision"), _noul(answers, "remember")
    judged: list[Candidate] = []
    probs = (answers.get("relevance") or {}).get("probabilities") if isinstance(answers.get("relevance"), dict) else None
    if isinstance(probs, dict):
        for i, c in enumerate(candidates):
            try:
                c.judged = float(probs.get(f"m{i + 1}", 0.0))
            except (TypeError, ValueError):
                c.judged = 0.0
            if c.judged >= RELEVANT:
                judged.append(c)
        judged.sort(key=lambda c: -(c.judged or 0))
    return p, r, judged


class Embedder:
    """bge-small through fastembed, loaded on first use (the import alone costs seconds). `passages` for what is
    stored, `query` for what is asked — the model takes a retrieval prefix on the query side."""

    def __init__(self, model: str = EMBED_MODEL, cache_dir: str | None = None):
        self.model = model
        self.cache_dir = cache_dir
        self._m = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        """The model is loaded (a first embed or `warm` loads it)."""
        return self._m is not None

    def _load(self):
        with self._lock:
            if self._m is None:
                from fastembed import TextEmbedding  # imported here, never at module load: the import alone costs seconds

                kw = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                self._m = TextEmbedding(model_name=self.model, **kw)
        return self._m

    def passages(self, texts: list[str]) -> np.ndarray:
        m = self._load()
        vecs = np.asarray(list(m.embed(texts)), dtype=np.float32)
        return _unit(vecs)

    def query(self, text: str) -> np.ndarray:
        m = self._load()
        v = np.asarray(list(m.query_embed([text])), dtype=np.float32)
        return _unit(v)[0]


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return v / n


_WORD = re.compile(r"[A-Za-z0-9']+")


def fts_query(text: str) -> str:
    """An FTS5 OR-query of the message's words (quoted, so apostrophes and hyphens never read as syntax)."""
    words = {w.lower() for w in _WORD.findall(text) if len(w) > 1}
    return " OR ".join(f'"{w}"' for w in sorted(words)) if words else '""'


class MemoryStore:
    """SQLite: `memory` rows keyed by `memory_key` (the row number is the store's own), an FTS5 index over the words,
    the embedding as a float32 blob. One file for the host; every read and write names the guild. Vectors are cached
    in RAM per guild (reloaded after a write). The index is derived: `replay` rebuilds it from the stored rows."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._vec_cache: dict[str, tuple[list[int], np.ndarray]] = {}
        with self._lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY, key TEXT NOT NULL DEFAULT '', guild TEXT NOT NULL, channel INTEGER NOT NULL,
                    channel_name TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'shared', author_id INTEGER NOT NULL,
                    author TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', p REAL,
                    message_id INTEGER, vec BLOB, forgotten_at TEXT);
                CREATE INDEX IF NOT EXISTS memory_guild ON memory(guild, forgotten_at);
                CREATE UNIQUE INDEX IF NOT EXISTS memory_key ON memory(guild, key);
                CREATE UNIQUE INDEX IF NOT EXISTS memory_message ON memory(guild, message_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(words);
                """
            )

    # ---------------------------------------------------------------- writes
    def add(self, guild: str, *, key: str, channel: int, channel_name: str, scope: str, author_id: int, author: str, at: str,
            text: str, vec: np.ndarray | None, p: float | None = None, message_id: int | None = None,
            context: str = "") -> Memory | None:
        """One memory; the same key or Discord message twice is kept once (None). `vec` may come later (`embed_missing`)."""
        if scope not in SCOPES:
            raise ValueError(f"scope {scope!r}")
        text = (text or "").strip()[:TEXT_MAX]
        if not text or not key:
            return None
        with self._lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO memory(key, guild, channel, channel_name, scope, author_id, author, at, text, context, p, message_id, vec) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, guild, int(channel), channel_name, scope, int(author_id), author, at, text, (context or "")[:200], p, message_id,
                     None if vec is None else np.asarray(vec, dtype=np.float32).tobytes()))
            except sqlite3.IntegrityError:
                return None
            self.db.execute("INSERT INTO memory_fts(rowid, words) VALUES (?, ?)", (cur.lastrowid, index_text(author, text, context)))
            self.db.commit()
            self._vec_cache.pop(guild, None)
            return self._by_rowid(guild, int(cur.lastrowid))

    def embed_missing(self, guild: str, embedder: Embedder, batch: int = 64) -> int:
        """Vectors for the rows that have none (a capture pass stores first, embeds once, in batches)."""
        with self._lock:
            rows = self.db.execute("SELECT id, author, text, context FROM memory WHERE guild=? AND vec IS NULL AND forgotten_at IS NULL", (guild,)).fetchall()
        n = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            vecs = embedder.passages([index_text(r["author"], r["text"], r["context"]) for r in chunk])
            with self._lock:
                self.db.executemany("UPDATE memory SET vec=? WHERE id=?", [(v.tobytes(), r["id"]) for v, r in zip(vecs, chunk)])
                self.db.commit()
            n += len(chunk)
        self._vec_cache.pop(guild, None)
        return n

    def forget(self, guild: str, key: str, *, by_id: int | None = None, officer: bool = False) -> Memory | None:
        """The writer forgets their own, an officer anyone's: the row is marked, never recalled, kept for the audit (and so
        a replay never re-inserts it). Returns the memory forgotten; None when there is none or it isn't theirs."""
        with self._lock:
            m = self.get(guild, key)
            if m is None or not (officer or (by_id is not None and m.author_id == int(by_id))):
                return None
            self.db.execute("UPDATE memory SET forgotten_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), m.rowid))
            self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (m.rowid,))
            self.db.commit()
            self._vec_cache.pop(guild, None)
            return m

    def forget_channel(self, guild: str, channel: int, *, before: str | None = None) -> list[Memory]:
        """A channel turned to Off forgets what it held (the setting means what it says); `before` (ISO) limits it to the
        memories written up to then — a replay's Off never reaches what the channel kept after it was turned back on."""
        q = "SELECT key FROM memory WHERE guild=? AND channel=? AND forgotten_at IS NULL"
        args: list = [guild, int(channel)]
        if before:
            q += " AND at <= ?"
            args.append(before)
        with self._lock:
            keys = [r["key"] for r in self.db.execute(q, args)]
            return [m for m in (self.forget(guild, k, officer=True) for k in keys) if m is not None]

    def replay(self, guild: str, rows: list[dict]) -> int:
        """The jsonl top to bottom into the index: a `memory` row the index lacks is inserted (without a vector — embed_missing
        later), a `forget` marks its row, a `forget_channel` marks the channel's live rows up to that point. Idempotent."""
        known = self.ids_of(guild)
        n = 0
        for r in rows:
            kind = r.get("kind")
            if kind == "memory" and r.get("id") and r["id"] not in known:
                if self.add(guild, key=r["id"], channel=int(r.get("channel") or 0), channel_name=str(r.get("channel_name") or ""),
                            scope=r.get("scope") or "shared", author_id=int(r.get("author_id") or 0), author=str(r.get("author") or ""),
                            at=str(r.get("at") or ""), text=str(r.get("text") or ""), vec=None, p=r.get("p"),
                            message_id=r.get("message_id"), context=str(r.get("context") or "")) is not None:
                    known.add(r["id"])
                    n += 1
            elif kind == "forget" and r.get("id"):
                if self.forget(guild, r["id"], officer=True) is not None:
                    n += 1
            elif kind == "forget_channel" and r.get("channel") is not None:
                if self.forget_channel(guild, int(r["channel"]), before=r.get("at") or None):
                    n += 1
        return n

    # ---------------------------------------------------------------- reads
    def _row(self, r) -> Memory:
        return Memory(id=r["key"], guild=r["guild"], channel=int(r["channel"]), channel_name=r["channel_name"], scope=r["scope"],
                      author_id=int(r["author_id"]), author=r["author"], at=r["at"], text=r["text"], p=r["p"], context=r["context"] or "",
                      rowid=int(r["id"]))

    def get(self, guild: str, key: str) -> Memory | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM memory WHERE guild=? AND key=? AND forgotten_at IS NULL", (guild, key)).fetchone()
        return self._row(r) if r else None

    def _by_rowid(self, guild: str, rowid: int) -> Memory | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM memory WHERE guild=? AND id=? AND forgotten_at IS NULL", (guild, rowid)).fetchone()
        return self._row(r) if r else None

    def ids_of(self, guild: str) -> set[str]:
        """Every key the index holds for the guild, the forgotten ones too (what a replay must not re-insert)."""
        with self._lock:
            return {r["key"] for r in self.db.execute("SELECT key FROM memory WHERE guild=?", (guild,))}

    def list(self, guild: str, *, author_id: int | None = None, channel: int | None = None, q: str | None = None,
             offset: int = 0, limit: int = 50) -> tuple[list[Memory], int]:
        """(a page of the live memories, newest first; the total that match) — by writer, channel and words."""
        where = "guild=? AND forgotten_at IS NULL"
        args: list = [guild]
        if author_id is not None:
            where += " AND author_id=?"
            args.append(int(author_id))
        if channel is not None:
            where += " AND channel=?"
            args.append(int(channel))
        if q:
            where += " AND id IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?)"
            args.append(fts_query(q))
        with self._lock:
            total = int(self.db.execute(f"SELECT count(*) FROM memory WHERE {where}", args).fetchone()[0])
            rows = self.db.execute(f"SELECT * FROM memory WHERE {where} ORDER BY at DESC LIMIT ? OFFSET ?", [*args, int(limit), int(offset)])
            return [self._row(r) for r in rows], total

    def count(self, guild: str, channel: int | None = None) -> int:
        q, args = "SELECT count(*) FROM memory WHERE guild=? AND forgotten_at IS NULL", [guild]
        if channel is not None:
            q += " AND channel=?"
            args.append(int(channel))
        with self._lock:
            return int(self.db.execute(q, args).fetchone()[0])

    def counts(self, guild: str) -> dict[str, int]:
        """Live memories per channel (the channel id as a string, as settings key channels)."""
        with self._lock:
            rows = self.db.execute("SELECT channel, count(*) AS n FROM memory WHERE guild=? AND forgotten_at IS NULL GROUP BY channel", (guild,))
            return {str(r["channel"]): int(r["n"]) for r in rows}

    def _vectors(self, guild: str) -> tuple[list[int], np.ndarray]:
        """(row numbers, their vectors) of the guild's live rows that have one."""
        cached = self._vec_cache.get(guild)
        if cached is not None:
            return cached
        with self._lock:
            rows = self.db.execute("SELECT id, vec FROM memory WHERE guild=? AND vec IS NOT NULL AND forgotten_at IS NULL ORDER BY id", (guild,)).fetchall()
        ids = [int(r["id"]) for r in rows]
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), EMBED_DIMS) if rows else np.zeros((0, EMBED_DIMS), np.float32)
        self._vec_cache[guild] = (ids, mat)
        return ids, mat

    @staticmethod
    def in_scope(m: Memory, channel: int | None, modes: dict[str, str] | None) -> bool:
        """The CURRENT setting of the memory's channel decides (a channel turned to "here only" takes effect at once): shared
        anywhere; channel only when answering in it (DMs and the site: None); a channel with no setting is out."""
        if modes is None:
            return m.scope == "shared" or (channel is not None and int(channel) == m.channel)
        mode = modes.get(str(m.channel))
        return mode == "shared" or (mode == "channel" and channel is not None and int(channel) == m.channel)

    def recall(self, guild: str, text: str, qvec: np.ndarray | None, *, channel: int | None = None, names: Iterable[int] = (),
               modes: dict[str, str] | None = None, k: int = RECALL_K, pool: int = 20) -> list[Candidate]:
        """The top `k` candidates for `text` in this scope: the vector list (cosine, when there is a query vector), the
        lexical list (FTS5 bm25) and the newest words of the authors the question names, fused by reciprocal rank — a
        shared rare name counts even when the embedding missed it. Nothing is judged here."""
        vec_rank: dict[int, int] = {}
        cosine: dict[int, float] = {}
        if qvec is not None:
            ids, mat = self._vectors(guild)
            if ids:
                sims = mat @ np.asarray(qvec, dtype=np.float32)
                cosine = {rid: float(s) for rid, s in zip(ids, sims)}
                vec_rank = {ids[int(i)]: r + 1 for r, i in enumerate(np.argsort(-sims)[:pool])}
        author_ids = sorted({int(a) for a in names})
        with self._lock:
            lex = self.db.execute(
                "SELECT m.id AS id FROM memory_fts f JOIN memory m ON m.id=f.rowid WHERE memory_fts MATCH ? AND m.guild=? "
                "AND m.forgotten_at IS NULL ORDER BY bm25(memory_fts) LIMIT ?", (fts_query(text), guild, pool)).fetchall()
            named = self.db.execute(
                f"SELECT id FROM memory WHERE guild=? AND author_id IN ({','.join('?' * len(author_ids))}) AND forgotten_at IS NULL "
                "ORDER BY at DESC LIMIT ?", (guild, *author_ids, pool)).fetchall() if author_ids else []
        bm_rank = {int(r["id"]): i + 1 for i, r in enumerate(lex)}
        name_rank = {int(r["id"]): i + 1 for i, r in enumerate(named)}
        fused: dict[int, float] = {}
        for ranks in (vec_rank, bm_rank, name_rank):
            for rid, r in ranks.items():
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + r)
        out: list[Candidate] = []
        for rid, score in sorted(fused.items(), key=lambda kv: -kv[1]):
            m = self._by_rowid(guild, rid)
            if m is None or not self.in_scope(m, channel, modes):
                continue
            out.append(Candidate(memory=m, cosine=cosine.get(rid, 0.0), bm25_rank=bm_rank.get(rid), vec_rank=vec_rank.get(rid, pool + 1), score=score,
                                 name_rank=name_rank.get(rid)))
            if len(out) >= k:
                break
        return out


async def judge(dec, message: str, candidates: list[Candidate], workload: str = "listen") -> list[Candidate]:
    """Ask the relevance question; returns the candidates with `judged` filled (those the judge gave ≥ RELEVANT keep
    their place, the rest are dropped). No decision provider, or a failed call → nothing is cited."""
    if not candidates or dec is None:
        return []
    state, instructions, criteria = relevance_question(message, candidates)
    probs = await dec.choice(state, instructions, criteria, workload=workload)
    if probs is None:
        return []
    kept = []
    for i, c in enumerate(candidates):
        c.judged = probs.get(f"m{i + 1}", 0.0)
        if c.judged >= RELEVANT:
            kept.append(c)
    kept.sort(key=lambda c: -(c.judged or 0))
    return kept


def memory_block(cands: Iterable[Candidate]) -> str:
    """The data block a help answer gets: one citation per line, member text fenced as data."""
    from .llm.provider import unfence

    lines = [unfence(c.memory.citation(), "memory") for c in cands]
    return "<memory>\n" + "\n".join(lines) + "\n</memory>" if lines else ""


# ------------------------------------------------------------------------------------------------------- the service
MEMORIES: "Memories | None" = None  # THE per-process service (discord_bot.run sets it; None offline and in most tests)
MEMORY_LISTENERS: list = []  # fn(guild key) after every write — the web's officers-only `memory` topic
log = logging.getLogger(__name__)


def _channels(reg) -> dict[str, str]:
    """The guild's remembering channels {channel id: shared | channel} (Registry.memory_channels; {} before it exists)."""
    return getattr(reg, "memory_channels", lambda: {})()


def enabled(reg) -> bool:
    """Memory does anything for this guild: a channel remembers and the host's TypeSafe key is set."""
    from .llm import decisions

    return bool(_channels(reg)) and decisions.key_set()


def on_save(reg, kind: str, lines) -> None:
    """registry.SAVE_LISTENERS: after a config commit, a channel no longer remembering forgets what it held."""
    if kind == "config" and MEMORIES is not None:
        try:
            MEMORIES.sync(reg)
        except Exception:  # noqa: BLE001 — a listener never breaks a save
            log.exception("memory sync failed (%s)", getattr(reg, "key", "?"))


class Memories:
    """The host's memory service: the data repo's `<guild>/memory.jsonl` is the record (append-only, one commit per
    row), `out/memory/<guild>.db` the derived index (rebuilt from the rows when missing). Every call is synchronous and
    may block for a git commit or an embedding: the bot calls it through asyncio.to_thread."""

    def __init__(self, out_dir: Path, embedder: Embedder | None = None):
        self.out = Path(out_dir)
        self.embedder = embedder or Embedder(cache_dir=str(self.out / "fastembed"))
        self._stores: dict[str, MemoryStore] = {}
        self._lock = threading.RLock()
        self._model = "off"  # off | loading | loaded | failed
        self._warned = 0.0

    # ---- plumbing
    def store(self, key: str) -> MemoryStore:
        with self._lock:
            s = self._stores.get(key)
            if s is None:
                s = self._stores[key] = MemoryStore(self.out / "memory" / f"{key}.db")
            return s

    @staticmethod
    def rel(reg) -> Path:
        return Path(reg.key) / "memory.jsonl"

    def rows(self, reg) -> list[dict]:
        return reg.store.read_jsonl(self.rel(reg))

    def _notify(self, key: str) -> None:
        for fn in list(MEMORY_LISTENERS):
            try:
                fn(key)
            except Exception:  # noqa: BLE001
                log.exception("memory listener failed")

    def model_state(self) -> str:
        return self._model

    def _embed(self, texts: list[str]) -> np.ndarray | None:
        """Vectors, or None when the model isn't there (logged once an hour; the rows wait for embed_missing_all)."""
        try:
            if self._model != "loaded":
                self._model = "loading"
            out = self.embedder.passages(texts)
            self._model = "loaded"
            return out
        except Exception as e:  # noqa: BLE001 — no network on first start, a bad cache: words still work
            self._model = "failed"
            if time.monotonic() - self._warned > 3600:
                self._warned = time.monotonic()
                log.warning("memory: the embedding model is not available (%s); words only until it is", type(e).__name__)
            return None

    def _query(self, text: str) -> np.ndarray | None:
        try:
            v = self.embedder.query(text)
            self._model = "loaded"
            return v
        except Exception:  # noqa: BLE001
            self._model = "failed"
            return None

    # ---- writes
    def remember(self, reg, *, channel: int, channel_name: str, scope: str, author_id: int, author: str, at: str, text: str,
                 context: str = "", p: float | None = None, message_id: int | None = None) -> Memory | None:
        key = memory_key(reg.key, message_id, at, author_id)
        store = self.store(reg.key)
        if key in store.ids_of(reg.key):
            return None
        text = (text or "").strip()[:TEXT_MAX]
        if not text:
            return None
        vecs = self._embed([index_text(author, text, context)])
        m = store.add(reg.key, key=key, channel=channel, channel_name=channel_name, scope=scope, author_id=author_id, author=author, at=at,
                      text=text, vec=None if vecs is None else vecs[0], p=p, message_id=message_id, context=context)
        if m is None:
            return None
        row = {"kind": "memory", "id": key, "channel": int(channel), "channel_name": channel_name, "scope": scope, "author_id": int(author_id),
               "author": author, "at": at, "text": text, "context": (context or "")[:200], "p": p, "message_id": message_id}
        with reg.store.lock:
            reg.store.append_jsonl(self.rel(reg), row)
            reg.store.commit(f"{reg.key}: memory: {author} in #{channel_name}")
        self._notify(reg.key)
        return m

    def forget(self, reg, key: str, *, by: str, by_id: int | None, via: str, officer: bool) -> Memory | None:
        m = self.store(reg.key).forget(reg.key, key, by_id=by_id, officer=officer)
        if m is None:
            return None
        row = {"kind": "forget", "id": key, "by": by, "by_id": by_id, "via": via, "at": _now(), "why": "member" if by_id == m.author_id else "officer"}
        with reg.store.lock:
            reg.store.append_jsonl(self.rel(reg), row)
            reg.store.commit(f"{reg.key}: memory: {by} forgot one of {m.author}'s")
        self._notify(reg.key)
        return m

    def forget_channel(self, reg, channel: int, *, why: str = "off") -> int:
        gone = self.store(reg.key).forget_channel(reg.key, int(channel))
        if not gone:
            return 0
        row = {"kind": "forget_channel", "channel": int(channel), "at": _now(), "count": len(gone), "why": why}
        with reg.store.lock:
            reg.store.append_jsonl(self.rel(reg), row)
            reg.store.commit(f"{reg.key}: memory: #{gone[0].channel_name} forgot {len(gone)}")
        self._notify(reg.key)
        return len(gone)

    def sync(self, reg) -> int:
        """Rows of a channel no longer remembering are forgotten (the setting means what it says); the count forgotten."""
        live = _channels(reg)
        n = 0
        for cid in list(self.store(reg.key).counts(reg.key)):
            if cid not in live:
                n += self.forget_channel(reg, int(cid), why="off")
        return n

    # ---- reads
    def recall_for(self, reg, text: str, *, channel: int | None = None) -> list[Candidate]:
        """The candidates for a question asked in `channel` (None = a DM or the site): the words, the meaning (when the
        model is there) and the words of any member the question names, in the channel scope the owner set."""
        from .help import mentioned_members

        store = self.store(reg.key)
        if not store.count(reg.key):
            return []
        names = [m.discord_id for m in mentioned_members(reg, text)]
        return store.recall(reg.key, text, self._query(text), channel=channel, names=names, modes=_channels(reg))

    def rebuild(self, reg) -> int:
        """At start: the rows the index lacks, then the vectors that are missing."""
        n = self.store(reg.key).replay(reg.key, self.rows(reg))
        self._embed_missing(reg)
        return n

    def _embed_missing(self, reg) -> int:
        store = self.store(reg.key)
        try:
            n = store.embed_missing(reg.key, self.embedder)
        except Exception as e:  # noqa: BLE001
            self._model = "failed"
            log.warning("memory: vectors not filled for %s (%s)", reg.key, type(e).__name__)
            return 0
        if n:
            self._model = "loaded"
        return n

    def embed_missing_all(self, regs) -> int:
        return sum(self._embed_missing(reg) for reg in regs)

    def warm(self, regs) -> None:
        """Load the model once (seconds) when any guild remembers, so the first capture doesn't wait."""
        if any(_channels(reg) for reg in regs):
            self._embed(["warm up"])

    def count(self, reg, channel: int | None = None) -> int:
        return self.store(reg.key).count(reg.key, channel=channel)

    def counts(self, reg) -> dict[str, int]:
        return self.store(reg.key).counts(reg.key)

    def list(self, reg, **kw) -> tuple[list[Memory], int]:
        return self.store(reg.key).list(reg.key, **kw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
