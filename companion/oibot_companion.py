#!/usr/bin/env python3
"""oibot companion — tails WoWChatLog.txt on the raider's PC and streams loot
events to the bot over a WebSocket (tailnet). Reads a file the game already
writes; touches nothing else.

    python oibot_companion.py --server ws://mini.tailnet:8787/feed --token … --character Rhozy \
        --log "C:/Program Files (x86)/World of Warcraft/_classic_/Logs/WoWChatLog.txt"
    python oibot_companion.py --replay sample_chatlog.txt --dry-run      # parse only, no network

Turn on logging in game with /chatlog (the companion warns when the file goes stale).
Dependency: `pip install websockets`.

Delivery: an event stays in the Outbox until the bot acks it (matched by its idem). A dropped socket re-sends the
in-flight event first after the reconnect; an error reply (the bot refused it — e.g. several loot councils are live and
this character is rostered on none, until an officer binds the companion on a council card) is retried every RETRY_S
seconds for RETRY_FOR_S. Always with the same idem: the bot dedupes per loot session, so a re-send is never applied twice.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

ITEM_LINK = re.compile(r"\|Hitem:(\d+)[^|]*\|h\[([^\]]+)\]\|h")
LOOT = re.compile(r"^(?P<who>[^\s:]+) receives? loot: (?P<rest>.*)$")
SELF_LOOT = re.compile(r"^You receive loot: (?P<rest>.*)$")
DROP_HINT = re.compile(r"(gargul|rclootcouncil|rclc|loot council|loot session|dropped|\bdrops?\b)", re.I)
KILL = re.compile(r"(?:\[(?:DBM|BigWigs)\]\s*)?(?P<boss>[A-Z][^!.:]{2,40}?) (?:has been defeated|defeated|down!|dies\b)", re.I)
TS = re.compile(r"^(?P<m>\d{1,2})/(?P<d>\d{1,2}) (?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})\s+(?P<body>.*)$")


def parse_line(line: str, year: int | None = None) -> dict | None:
    """One chat-log line → event dict or None."""
    line = line.rstrip("\r\n")
    m = TS.match(line)
    if not m:
        return None
    year = year or datetime.now().year
    ts = datetime(year, int(m["m"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"]), int(m["ms"]) * 1000).isoformat(timespec="milliseconds")
    body = m["body"].strip()
    # strip "[Raid] Name: " / "[Raid Warning] Name: " / "Name whispers: " prefixes for announcements
    speaker = None
    mm = re.match(r"^\[(?P<chan>[^\]]+)\]\s+(?P<who>[^:]+?):\s+(?P<text>.*)$", body)
    if mm:
        speaker, text = mm["who"].strip(), mm["text"]
    else:
        text = body
    idem = hashlib.sha1(line.encode("utf-8", "ignore")).hexdigest()[:16]

    lm = LOOT.match(text)
    if lm:
        items = ITEM_LINK.findall(lm["rest"])
        if items:
            return {"type": "loot", "recipient": lm["who"], "item_id": int(items[0][0]), "item_name": items[0][1], "ts": ts, "idem": idem, "raw": text[:200]}
    sm = SELF_LOOT.match(text)
    if sm:
        items = ITEM_LINK.findall(sm["rest"])
        if items:
            return {"type": "loot", "recipient": "@self", "item_id": int(items[0][0]), "item_name": items[0][1], "ts": ts, "idem": idem, "raw": text[:200]}
    items = ITEM_LINK.findall(text)
    if items and DROP_HINT.search(text):
        boss = None
        bm = re.search(r"(?:from|on|for)\s+([A-Z][\w' ]{2,40}?)\s*[:\-]", text)
        if bm:
            boss = bm.group(1).strip()
        return {"type": "drop", "boss": boss, "items": [{"id": int(i), "name": n} for i, n in items], "source": ("gargul" if "gargul" in text.lower() else "rclc" if "rclootcouncil" in text.lower() else "chat"), "speaker": speaker, "ts": ts, "idem": idem, "raw": text[:300]}
    km = KILL.search(text)
    if km and not items:
        return {"type": "kill", "boss": km["boss"].strip(), "ts": ts, "idem": idem, "raw": text[:200]}
    return None


RETRY_S = 30  # an event the bot refused (an error reply) is sent again this often …
RETRY_FOR_S = 600  # … for this long, then dropped with a line (the bot's ops channel names what it never recorded)


class Outbox:
    """Events waiting for the bot's ack. `next(now)` is the event to send now (the in-flight one first, then retries
    that are due, then new ones); `acked` drops it; `refused` keeps it for a retry in RETRY_S (dropped after RETRY_FOR_S);
    a disconnect leaves the in-flight event at the front, so it is re-sent first after the reconnect."""

    def __init__(self, retry_s: float = RETRY_S, retry_for_s: float = RETRY_FOR_S):
        self.retry_s, self.retry_for_s = retry_s, retry_for_s
        self.pending: list[dict] = []  # in order; [0] is in flight once sent
        self.later: list[tuple[float, float, dict]] = []  # (due, first refused at, event)

    def put(self, ev: dict) -> None:
        self.pending.append(ev)

    def next(self, now: float) -> dict | None:
        due = [t for t in self.later if t[0] <= now]
        if due:
            self.later = [t for t in self.later if t[0] > now]
            self.pending[0:0] = [ev for _, _, ev in due]
        return self.pending[0] if self.pending else None

    def acked(self, ev: dict) -> None:
        if self.pending and self.pending[0] is ev:
            self.pending.pop(0)

    def refused(self, ev: dict, now: float, first: float | None = None) -> bool:
        """Keep a refused event for a retry; False when it has been retried long enough (dropped)."""
        self.acked(ev)
        first = ev.get("_first_refused", now) if first is None else first
        if now - first >= self.retry_for_s:
            return False
        self.later.append((now + self.retry_s, first, {**ev, "_first_refused": first}))
        return True

    def __len__(self) -> int:
        return len(self.pending) + len(self.later)


def wire(ev: dict) -> str:
    """The frame sent for an event (the outbox's own bookkeeping keys stay local)."""
    return json.dumps({k: v for k, v in ev.items() if not k.startswith("_")})


async def deliver(ws, ev: dict, outbox: Outbox, verbose: bool = False) -> str:
    """Send one event and wait for ITS reply (an ack or error with its idem; a heartbeat's reply is skipped):
    'ack' | 'refused' | 'dropped'. A socket error propagates and leaves the event in flight (re-sent after the reconnect)."""
    await ws.send(wire(ev))
    while True:
        reply = json.loads(await ws.recv())
        if reply.get("idem") != ev.get("idem"):
            continue  # a heartbeat's ack, or a late reply to something else
        if reply.get("type") == "error":
            kept = outbox.refused(ev, time.time())
            print("server refused:", reply.get("message"), "— retrying" if kept else "— giving up on it")
            return "refused" if kept else "dropped"
        outbox.acked(ev)
        if verbose:
            print("ack", reply)
        return "ack"


class Tailer:
    """Follows a file that the game appends to; handles truncation/rotation."""

    def __init__(self, path: str, from_start: bool = False):
        self.path, self.pos = path, 0
        self.from_start = from_start
        self.last_size = 0

    def read_new(self) -> list[str]:
        if not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if self.pos == 0 and not self.from_start:
            self.pos = size  # start at the end: only new lines
        if size < self.pos:
            self.pos = 0  # truncated
        with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return data.splitlines()

    def age(self) -> float:
        return time.time() - os.path.getmtime(self.path) if os.path.exists(self.path) else 1e9


async def run(args: argparse.Namespace) -> None:
    events: list[dict] = []
    if args.replay:
        year = datetime.fromtimestamp(os.path.getmtime(args.replay)).year
        with open(args.replay, encoding="utf-8", errors="ignore") as f:
            for line in f:
                ev = parse_line(line, year)
                if ev:
                    events.append(ev)
        print(f"parsed {len(events)} events from {args.replay}")
        if args.dry_run:
            for ev in events:
                print(json.dumps(ev))
            return
    if args.dry_run:
        print("dry-run needs --replay")
        return

    import websockets  # type: ignore

    queue: asyncio.Queue = asyncio.Queue()
    for ev in events:
        queue.put_nowait(ev)
    outbox = Outbox()

    def own(ev: dict) -> dict:
        return {**ev, "recipient": args.character} if ev.get("recipient") in ("@self", "You") else ev
    tailer = Tailer(args.log, from_start=False) if args.log else None

    async def producer():
        stale_warned = False
        while True:
            if tailer:
                for line in tailer.read_new():
                    ev = parse_line(line)
                    if ev:
                        queue.put_nowait(ev)
                if tailer.age() > 600 and not stale_warned:
                    print("chat log hasn't changed in 10 minutes — is /chatlog on?")
                    stale_warned = True
                elif tailer.age() <= 600:
                    stale_warned = False
            await asyncio.sleep(1)

    async def sender():
        backoff = 2
        while True:
            try:
                async with websockets.connect(args.server, max_size=2**20) as ws:
                    await ws.send(json.dumps({"type": "hello", "token": args.token, "character": args.character, "client": "oibot-companion/0.1", "guild": args.guild}))
                    print("connected:", await ws.recv())
                    backoff = 2

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(60)
                            await ws.send(json.dumps({"type": "heartbeat", "ts": datetime.now().isoformat(timespec="seconds"), "log_age": tailer.age() if tailer else None}))

                    hb = asyncio.create_task(heartbeat())
                    try:
                        while True:
                            while not queue.empty():
                                outbox.put(own(queue.get_nowait()))
                            ev = outbox.next(time.time())
                            if ev is None:  # nothing to send: wait for a new event (or a retry coming due)
                                try:
                                    outbox.put(own(await asyncio.wait_for(queue.get(), timeout=5)))
                                except asyncio.TimeoutError:
                                    pass
                                continue
                            await deliver(ws, ev, outbox, args.verbose)  # a dropped socket leaves ev in flight
                    finally:
                        hb.cancel()
            except Exception as e:  # noqa: BLE001
                print(f"disconnected ({e}); retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    await asyncio.gather(producer(), sender())


def main() -> None:
    p = argparse.ArgumentParser(description="oibot companion: stream WoW chat-log loot events to oibot_GM")
    p.add_argument("--server", default=os.environ.get("OIBOT_SERVER", "ws://127.0.0.1:8787/feed"))
    p.add_argument("--token", default=os.environ.get("OIBOT_FEED_TOKEN", ""))
    p.add_argument("--character", default=os.environ.get("OIBOT_CHARACTER", "?"), help="your character (the master looter)")
    p.add_argument("--guild", default=os.environ.get("OIBOT_GUILD", ""), help="guild key on the bot (optional)")
    p.add_argument("--log", default=os.environ.get("OIBOT_CHATLOG"), help="path to WoWChatLog.txt")
    p.add_argument("--replay", help="parse a saved chat log and send its events (testing)")
    p.add_argument("--dry-run", action="store_true", help="with --replay: print parsed events, no network")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.token:
        sys.exit("need --token (or OIBOT_FEED_TOKEN)")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
