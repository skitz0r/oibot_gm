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

import json
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


@dataclass
class Memory:
    id: int
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


def remember_state(bot_words: str, channel_name: str, recent: list[dict], author: str, text: str) -> dict:
    """The remember question's state: what the bot records, where the message was written, the chat before, the message."""
    return {"bot": bot_words, "channel": {"name": channel_name}, "recent": recent[-2:],
            "message": {"author": author, "text": text[:TEXT_MAX]}}


def relevance_question(message: str, candidates: list[Candidate]) -> tuple[dict, str, dict]:
    """(state, instructions, criteria) for the judge: options m1..mN carry each memory's citation, plus `none`."""
    mem = [{"id": f"m{i + 1}", "author": c.memory.author, "when": c.memory.when(), "text": c.memory.text,
            **({"replying_to": c.memory.context} if c.memory.context else {})} for i, c in enumerate(candidates)]
    criteria = {f"m{i + 1}": c.memory.citation() for i, c in enumerate(candidates)}
    criteria[NONE] = "No remembered message answers it."
    return {"message": {"text": message[:TEXT_MAX]}, "memory": mem}, RELEVANCE_INSTRUCTIONS, criteria


class Embedder:
    """bge-small through fastembed, loaded on first use (the import alone costs seconds). `passages` for what is
    stored, `query` for what is asked — the model takes a retrieval prefix on the query side."""

    def __init__(self, model: str = EMBED_MODEL, cache_dir: str | None = None):
        self.model = model
        self.cache_dir = cache_dir
        self._m = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._m is None:
                from fastembed import TextEmbedding  # the optional dependency: `uv run --with fastembed` for the probe

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
    """SQLite: `memory` rows, an FTS5 index over `text`, the embedding as a float32 blob. One file for the host; every
    read and write names the guild. Vectors are cached in RAM per guild (reloaded after a write)."""

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
                    id INTEGER PRIMARY KEY, guild TEXT NOT NULL, channel INTEGER NOT NULL, channel_name TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'shared', author_id INTEGER NOT NULL, author TEXT NOT NULL,
                    at TEXT NOT NULL, text TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', p REAL, message_id INTEGER, vec BLOB,
                    forgotten_at TEXT);
                CREATE INDEX IF NOT EXISTS memory_guild ON memory(guild, forgotten_at);
                CREATE UNIQUE INDEX IF NOT EXISTS memory_message ON memory(guild, message_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(words);
                """
            )

    # ---------------------------------------------------------------- writes
    def add(self, guild: str, *, channel: int, channel_name: str, scope: str, author_id: int, author: str, at: str,
            text: str, vec: np.ndarray | None, p: float | None = None, message_id: int | None = None, context: str = "") -> int | None:
        """One memory; the same Discord message twice is kept once (None). `vec` may come later (`embed_missing`)."""
        if scope not in SCOPES:
            raise ValueError(f"scope {scope!r}")
        text = (text or "").strip()[:TEXT_MAX]
        if not text:
            return None
        with self._lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO memory(guild, channel, channel_name, scope, author_id, author, at, text, context, p, message_id, vec) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (guild, int(channel), channel_name, scope, int(author_id), author, at, text, (context or "")[:200], p, message_id,
                     None if vec is None else np.asarray(vec, dtype=np.float32).tobytes()))
            except sqlite3.IntegrityError:
                return None
            self.db.execute("INSERT INTO memory_fts(rowid, words) VALUES (?, ?)", (cur.lastrowid, index_text(author, text, context)))
            self.db.commit()
            self._vec_cache.pop(guild, None)
            return int(cur.lastrowid)

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

    def forget(self, guild: str, memory_id: int, *, by_id: int | None = None, officer: bool = False) -> bool:
        """The writer forgets their own, an officer anyone's: the row is marked, never recalled, kept for the audit."""
        with self._lock:
            row = self.db.execute("SELECT author_id FROM memory WHERE guild=? AND id=? AND forgotten_at IS NULL", (guild, memory_id)).fetchone()
            if row is None or not (officer or (by_id is not None and int(row["author_id"]) == int(by_id))):
                return False
            self.db.execute("UPDATE memory SET forgotten_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), memory_id))
            self.db.execute("DELETE FROM memory_fts WHERE rowid=?", (memory_id,))
            self.db.commit()
            self._vec_cache.pop(guild, None)
            return True

    def forget_channel(self, guild: str, channel: int) -> int:
        """A channel turned to Off forgets what it held (the setting means what it says)."""
        with self._lock:
            ids = [r["id"] for r in self.db.execute("SELECT id FROM memory WHERE guild=? AND channel=? AND forgotten_at IS NULL", (guild, int(channel)))]
        return sum(self.forget(guild, i, officer=True) for i in ids)

    # ---------------------------------------------------------------- reads
    def _row(self, r) -> Memory:
        return Memory(id=int(r["id"]), guild=r["guild"], channel=int(r["channel"]), channel_name=r["channel_name"], scope=r["scope"],
                      author_id=int(r["author_id"]), author=r["author"], at=r["at"], text=r["text"], p=r["p"], context=r["context"] or "")

    def get(self, guild: str, memory_id: int) -> Memory | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM memory WHERE guild=? AND id=? AND forgotten_at IS NULL", (guild, memory_id)).fetchone()
        return self._row(r) if r else None

    def list(self, guild: str, *, author_id: int | None = None, channel: int | None = None, limit: int = 200) -> list[Memory]:
        q = "SELECT * FROM memory WHERE guild=? AND forgotten_at IS NULL"
        args: list = [guild]
        if author_id is not None:
            q += " AND author_id=?"
            args.append(int(author_id))
        if channel is not None:
            q += " AND channel=?"
            args.append(int(channel))
        q += " ORDER BY at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row(r) for r in self.db.execute(q, args)]

    def count(self, guild: str) -> int:
        with self._lock:
            return int(self.db.execute("SELECT count(*) FROM memory WHERE guild=? AND forgotten_at IS NULL", (guild,)).fetchone()[0])

    def _vectors(self, guild: str) -> tuple[list[int], np.ndarray]:
        cached = self._vec_cache.get(guild)
        if cached is not None:
            return cached
        with self._lock:
            rows = self.db.execute("SELECT id, vec FROM memory WHERE guild=? AND vec IS NOT NULL AND forgotten_at IS NULL ORDER BY id", (guild,)).fetchall()
        ids = [int(r["id"]) for r in rows]
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), EMBED_DIMS) if rows else np.zeros((0, EMBED_DIMS), np.float32)
        self._vec_cache[guild] = (ids, mat)
        return ids, mat

    def in_scope(self, m: Memory, channel: int | None) -> bool:
        """Shared anywhere; a channel-scoped memory only when answering in that channel (DMs and the site: None)."""
        return m.scope == "shared" or (channel is not None and int(channel) == m.channel)

    def recall(self, guild: str, text: str, qvec: np.ndarray, *, channel: int | None = None, k: int = RECALL_K,
               pool: int = 20) -> list[Candidate]:
        """The top `k` candidates for `text` in this scope: the vector list (cosine) and the lexical list (FTS5 bm25),
        fused by reciprocal rank — a shared rare name counts even when the embedding missed it. Nothing is judged here."""
        ids, mat = self._vectors(guild)
        if not ids:
            return []
        sims = mat @ np.asarray(qvec, dtype=np.float32)
        order = np.argsort(-sims)
        vec_rank = {ids[int(i)]: r + 1 for r, i in enumerate(order[:pool])}
        with self._lock:
            lex = self.db.execute(
                "SELECT m.id AS id FROM memory_fts f JOIN memory m ON m.id=f.rowid WHERE memory_fts MATCH ? AND m.guild=? "
                "AND m.forgotten_at IS NULL ORDER BY bm25(memory_fts) LIMIT ?", (fts_query(text), guild, pool)).fetchall()
        bm_rank = {int(r["id"]): i + 1 for i, r in enumerate(lex)}
        fused: dict[int, float] = {}
        for mid, r in vec_rank.items():
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + r)
        for mid, r in bm_rank.items():
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + r)
        pos = {mid: i for i, mid in enumerate(ids)}
        out: list[Candidate] = []
        for mid, score in sorted(fused.items(), key=lambda kv: -kv[1]):
            m = self.get(guild, mid)
            if m is None or not self.in_scope(m, channel):
                continue
            cos = float(sims[pos[mid]]) if mid in pos else 0.0
            out.append(Candidate(memory=m, cosine=cos, bm25_rank=bm_rank.get(mid), vec_rank=vec_rank.get(mid, pool + 1), score=score))
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
