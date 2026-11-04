"""The chat-memory spike (design §5.61): read a guild's listened channels' history AS THE BOT, ask Jev which messages
are worth remembering, keep those in a scratch SQLite store with local embeddings, then answer questions against it
and show every score — nothing touches the live bot, the data repo or Discord (reads only).

    uv run --with fastembed python scripts/memory_probe.py capture --guild big-guys-for --limit 400 --db <scratch>/memory.db --report <scratch>/capture.md
    uv run --with fastembed python scripts/memory_probe.py ask --guild big-guys-for --db <scratch>/memory.db --channel <id> "where does the pattern drop?"
    uv run --with fastembed python scripts/memory_probe.py ask --guild big-guys-for --db <scratch>/memory.db --questions <file> --report <scratch>/ask.md

The report names members (it is for the owner's eyes, in the scratch directory — never committed). Channels default
to the guild's listened ones (`plain.listen`); the ops/analytics channel is skipped (bot output). The bot's own
messages, empty messages and other bots are skipped. Mentions become registered names (else "@someone").
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from oibot_gm import memory as mem  # noqa: E402
from oibot_gm.discord_bot import LISTEN_BOT_WORDS  # noqa: E402
from oibot_gm.llm import decisions  # noqa: E402

API = "https://discord.com/api/v10"
DATA = Path(os.environ.get("OIBOT_DATA_ROOT", str(Path.home() / "dev" / "oibot_gm-data")))


def headers() -> dict:
    return {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"}


def guild_yaml(key: str) -> dict:
    return yaml.safe_load((DATA / key / "guild.yaml").read_text())


def member_names(key: str) -> dict[int, str]:
    """discord id → the registered display name (read-only, straight from the data repo's member files)."""
    out = {}
    for f in (DATA / key / "members").glob("*.json"):
        try:
            m = json.loads(f.read_text()) or {}
        except Exception:  # noqa: BLE001
            continue
        did = m.get("discord_id")
        if did and not m.get("test"):
            out[int(did)] = str(m.get("display_name") or f.stem)
    return out


MENTION = re.compile(r"<@!?(\d+)>")
ROLE = re.compile(r"<@&\d+>")
CHAN = re.compile(r"<#(\d+)>")


def plain_text(content: str, names: dict[int, str], channels: dict[int, str]) -> str:
    t = MENTION.sub(lambda m: "@" + names.get(int(m.group(1)), "someone"), content or "")
    t = ROLE.sub("@role", t)
    t = CHAN.sub(lambda m: "#" + channels.get(int(m.group(1)), "channel"), t)
    return t.strip()


def fetch_history(cid: int, limit: int) -> list[dict]:
    out: list[dict] = []
    before = None
    with httpx.Client(headers=headers(), timeout=30) as c:
        while len(out) < limit:
            params = {"limit": min(100, limit - len(out))}
            if before:
                params["before"] = before
            r = c.get(f"{API}/channels/{cid}/messages", params=params)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "1")) + 0.2)
                continue
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            out.extend(page)
            before = page[-1]["id"]
    return out


def channel_info(cid: int) -> dict:
    with httpx.Client(headers=headers(), timeout=30) as c:
        r = c.get(f"{API}/channels/{cid}")
        return r.json() if r.status_code == 200 else {"name": str(cid)}


async def capture(args) -> None:
    g = guild_yaml(args.guild)
    listen = {int(k): v for k, v in ((g.get("plain") or {}).get("listen") or {}).items()}
    skip = {g.get("ops_channel_id"), g.get("analytics_channel_id")}
    cids = [int(c) for c in args.channels] if args.channels else [c for c in listen if c not in skip]
    names = member_names(args.guild)
    infos = {c: channel_info(c) for c in cids}
    chan_names = {c: i.get("name", str(c)) for c, i in infos.items()}
    dec = decisions.DecisionProvider(timeout=8.0)
    if not dec.available:
        sys.exit("TYPESAFE_API_KEY is not set")
    store = mem.MemoryStore(args.db)
    lines = [f"# Memory capture — {args.guild}", "", f"Channels: " + ", ".join(f"#{chan_names[c]} ({listen.get(c, '?')})" for c in cids), ""]
    kept_total = seen_total = 0
    tokens = 0
    sem = asyncio.Semaphore(args.parallel)
    for cid in cids:
        hist = [m for m in reversed(fetch_history(cid, args.limit)) if not m["author"].get("bot") and (m.get("content") or "").strip()]
        print(f"#{chan_names[cid]}: {len(hist)} human messages", file=sys.stderr)
        texts = [plain_text(m["content"], names, chan_names) for m in hist]
        authors = [names.get(int(m["author"]["id"]), m["author"].get("global_name") or m["author"]["username"]) for m in hist]
        contexts = []
        for m in hist:  # a Discord reply carries the message it answers; alone, "yep i do" remembers nothing
            ref = m.get("referenced_message") or {}
            if ref.get("content"):
                who = names.get(int(ref["author"]["id"]), ref["author"].get("global_name") or ref["author"]["username"])
                contexts.append(f"{who}: {plain_text(ref['content'], names, chan_names)[:160]}")
            else:
                contexts.append("")

        async def one(i: int):
            async with sem:
                recent = [{"author": authors[j], "text": texts[j][:200]} for j in range(max(0, i - 2), i)]
                st = mem.remember_state(LISTEN_BOT_WORDS, f"#{chan_names[cid]}", recent, authors[i], texts[i])
                if contexts[i]:
                    st["message"]["replying_to"] = contexts[i]
                p = await dec.noul(st, mem.REMEMBER_QUESTION, mem.REMEMBER_CRITERIA, workload="memory")
                return i, p, dict(dec.last_usage)

        results = await asyncio.gather(*(one(i) for i in range(len(hist))))
        rows = []
        for i, p, usage in results:
            tokens += int(usage.get("input_tokens") or 0)
            m = hist[i]
            seen_total += 1
            kept = p is not None and p >= mem.REMEMBER_AT
            if kept:
                kept_total += 1
                scope = "channel" if cid == g.get("officer_channel_id") or "officer" in chan_names[cid] else "shared"
                store.add(args.guild, channel=cid, channel_name=chan_names[cid], scope=scope, author_id=int(m["author"]["id"]),
                          author=authors[i], at=m["timestamp"], text=texts[i], vec=None, p=p, message_id=int(m["id"]), context=contexts[i])
            rows.append((p, kept, m["timestamp"][:16].replace("T", " "), authors[i], texts[i] + (f"  ↩ {contexts[i]}" if contexts[i] else "")))
        lines += [f"## #{chan_names[cid]} — {sum(1 for r in rows if r[1])} kept of {len(rows)}", "", "| p | kept | when (UTC) | who | text |", "|---|---|---|---|---|"]
        for p, kept, when, who, text in sorted(rows, key=lambda r: -(r[0] or 0)):
            lines.append(f"| {'' if p is None else f'{p:.2f}'} | {'✔' if kept else ''} | {when} | {who} | {text[:160].replace('|', '¦').replace(chr(10), ' ')} |")
        lines.append("")
    print("embedding…", file=sys.stderr)
    t = time.perf_counter()
    n = store.embed_missing(args.guild, mem.Embedder(cache_dir=args.model_cache))
    lines.insert(3, f"Kept {kept_total} of {seen_total} messages · {n} embedded in {time.perf_counter() - t:.1f}s · Jev calls {dec.calls}, ~{tokens / 1000:.0f}K input tokens (~${tokens * 0.042 / 1e6:.3f})")
    Path(args.report).write_text("\n".join(lines))
    print(f"kept {kept_total}/{seen_total}; report {args.report}", file=sys.stderr)


async def ask(args) -> None:
    store = mem.MemoryStore(args.db)
    chan_names = {int(r[0]): r[1] for r in store.db.execute("SELECT DISTINCT channel, channel_name FROM memory WHERE guild=?", (args.guild,))}
    emb = mem.Embedder(cache_dir=args.model_cache)
    dec = decisions.DecisionProvider(timeout=8.0)
    questions = [q.strip() for q in Path(args.questions).read_text().splitlines() if q.strip() and not q.startswith("#")] if args.questions else [args.question]
    lines = [f"# Memory recall — {args.guild} ({store.count(args.guild)} memories)", ""]
    for q in questions:
        channel = args.channel
        if "|" in q:  # "<channel id>|question" asks as if written in that channel
            c, q = q.split("|", 1)
            channel = int(c.strip())
            q = q.strip()
        t = time.perf_counter()
        cands = store.recall(args.guild, q, emb.query(q), channel=channel)
        t_recall = time.perf_counter() - t
        t = time.perf_counter()
        judged = await mem.judge(dec, q, cands, workload="memory")
        t_judge = time.perf_counter() - t
        lines += [f"## {q}" + (f"  _(asked in #{chan_names.get(channel, channel)})_" if channel else "  _(asked in a DM)_"), "",
                  f"recall {t_recall * 1000:.0f} ms · judge {t_judge * 1000:.0f} ms", "",
                  "| judge | cosine | vec rank | bm25 rank | who / when | text |", "|---|---|---|---|---|---|"]
        for c in cands:
            j = "" if c.judged is None else f"**{c.judged:.2f}**" if c.judged >= mem.RELEVANT else f"{c.judged:.2f}"
            shown = c.memory.text[:140] + (f"  ↩ {c.memory.context[:60]}" if c.memory.context else "")
            lines.append(f"| {j} | {c.cosine:.2f} | {c.vec_rank} | {c.bm25_rank or ''} | {c.memory.author} {c.memory.when()} | {shown.replace('|', '¦').replace(chr(10), ' ')} |")
        if not cands:
            lines.append("_no candidates in scope_")
        cited = [c for c in judged]
        lines += ["", "**Would cite:** " + ("; ".join(f"“{c.memory.citation()[:160]}”" for c in cited) if cited else "nothing (none wins)"), ""]
        print(f"{q[:60]:62} cited {len(cited)}  top judge {max((c.judged or 0) for c in cands) if cands else 0:.2f}", file=sys.stderr)
    out = "\n".join(lines)
    if args.report:
        Path(args.report).write_text(out)
        print(f"report {args.report}", file=sys.stderr)
    else:
        print(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--guild", required=True)
    c.add_argument("--channels", nargs="*", help="channel ids (default: the guild's listened channels, ops skipped)")
    c.add_argument("--limit", type=int, default=400, help="messages per channel, newest first")
    c.add_argument("--db", required=True)
    c.add_argument("--report", required=True)
    c.add_argument("--parallel", type=int, default=4)
    c.add_argument("--model-cache", default=None)
    a = sub.add_parser("ask")
    a.add_argument("--guild", required=True)
    a.add_argument("--db", required=True)
    a.add_argument("--channel", type=int, default=None)
    a.add_argument("--questions", default=None, help="a file, one question per line; '<channel id>|question' sets the channel")
    a.add_argument("--report", default=None)
    a.add_argument("--model-cache", default=None)
    a.add_argument("question", nargs="?")
    args = ap.parse_args()
    if args.cmd == "ask" and not (args.question or args.questions):
        ap.error("a question or --questions")
    asyncio.run(capture(args) if args.cmd == "capture" else ask(args))


if __name__ == "__main__":
    main()
