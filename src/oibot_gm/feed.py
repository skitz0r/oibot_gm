"""Loot feed listener: the companion on the raider's PC streams chat-log events
over a WebSocket (tailnet), the bot routes them to the active raid.

Protocol (JSON text frames):
  client → {"type":"hello","token":"…","character":"Rhozy","client":"oibot-companion/0.1","guild":"oi"}
  client → {"type":"drop","boss":"Supremus"|null,"items":[{"id":32262,"name":"…"}],"source":"gargul","ts":"…","idem":"…"}
  client → {"type":"loot","recipient":"Boviche","item_id":32262,"item_name":"…","ts":"…","idem":"…"}
  client → {"type":"kill","boss":"Supremus","ts":"…","idem":"…"}
  client → {"type":"heartbeat","ts":"…"}
  server → {"type":"ack","idem":"…"} | {"type":"error","message":"…"} | {"type":"welcome","routes_to":"BLA-0915"}
Bind to the tailnet address (OIBOT_FEED_BIND=100.x.y.z:8787); never expose publicly.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

Handler = Callable[[dict[str, Any], "Companion"], Awaitable[dict[str, Any] | None]]


class Companion:
    def __init__(self, character: str, client: str, guild: str, peer: str):
        self.character, self.client, self.guild, self.peer = character, client, guild, peer
        self.connected_at = datetime.now(timezone.utc)
        self.last_seen = self.connected_at
        self.events = 0
        self.seen: set[str] = set()  # idempotency keys (bounded)

    def touch(self) -> None:
        self.last_seen = datetime.now(timezone.utc)

    @property
    def silent_for(self) -> float:
        return (datetime.now(timezone.utc) - self.last_seen).total_seconds()


class FeedServer:
    def __init__(self, handler: Handler, token: str, bind: str = "127.0.0.1:8787"):
        self.handler, self.token = handler, token
        host, _, port = bind.rpartition(":")
        self.host, self.port = host or "127.0.0.1", int(port or 8787)
        self.companions: dict[str, Companion] = {}  # peer -> companion
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/feed", self._ws)
        app.router.add_get("/health", lambda r: web.json_response({"ok": True, "companions": len(self.companions)}))
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        print(f"loot feed listening on ws://{self.host}:{self.port}/feed")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        peer = request.remote or "?"
        comp: Companion | None = None
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "bad json"})
                continue
            if comp is None:
                if ev.get("type") != "hello" or ev.get("token") != self.token:
                    await ws.send_json({"type": "error", "message": "hello with a valid token first"})
                    await ws.close()
                    break
                comp = Companion(ev.get("character", "?"), ev.get("client", "?"), ev.get("guild", ""), peer)
                self.companions[peer] = comp
                reply = await self.handler(ev, comp)
                await ws.send_json({"type": "welcome", **(reply or {})})
                continue
            comp.touch()
            idem = ev.get("idem")
            if idem and idem in comp.seen:
                await ws.send_json({"type": "ack", "idem": idem, "dup": True})
                continue
            try:
                reply = await self.handler(ev, comp)
                comp.events += 1
                if idem:
                    comp.seen.add(idem)
                    if len(comp.seen) > 5000:
                        comp.seen = set(list(comp.seen)[-2500:])
                await ws.send_json({"type": "ack", "idem": idem, **(reply or {})})
            except Exception as e:  # noqa: BLE001
                await ws.send_json({"type": "error", "idem": idem, "message": str(e)[:200]})
        self.companions.pop(peer, None)
        return ws

    def status(self) -> str:
        if not self.companions:
            return "no companion connected"
        return "; ".join(f"{c.character} ({c.client}) {c.events} events, seen {int(c.silent_for)}s ago" for c in self.companions.values())


def feed_config() -> tuple[str | None, str]:
    """(token, bind) from the environment; token None disables the listener."""
    return os.environ.get("OIBOT_FEED_TOKEN") or None, os.environ.get("OIBOT_FEED_BIND", "127.0.0.1:8787")
