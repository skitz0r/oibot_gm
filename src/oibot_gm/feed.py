"""Loot feed listener: the companion on the raider's PC streams chat-log events
over a WebSocket (tailnet), the bot routes them to the active raid.

Protocol (JSON text frames):
  client → {"type":"hello","token":"…","character":"Rhozy","client":"oibot-companion/0.1","guild":"oi"}
  client → {"type":"drop","boss":"Supremus"|null,"items":[{"id":32262,"name":"…"}],"source":"gargul","ts":"…","idem":"…"}
  client → {"type":"loot","recipient":"Boviche","item_id":32262,"item_name":"…","ts":"…","idem":"…"}
  client → {"type":"kill","boss":"Supremus","ts":"…","idem":"…"}
  client → {"type":"heartbeat","ts":"…"}
  server → {"type":"ack","idem":"…","session":"<guild>:<run>"} | {"type":"error","idem":"…","message":"…"} | {"type":"welcome","routes_to":"<run>","session":"<guild>:<run>"}
`ts` may be epoch seconds (preferred: the bot renders a native Discord stamp) or the chat log's ISO stamp. The companion
keeps an event until its ack and re-sends it after a reconnect with the same idem; the bot dedupes per loot SESSION
(LootSession.seen_idem), not per connection, so a re-send is acked ("dup") and never applied twice. An error reply is
not an ack (the companion may re-send). Presence per companion: live · quiet (no event for QUIET_AFTER_S) · disconnected.
Bind to the tailnet address (OIBOT_FEED_BIND=100.x.y.z:8787); never expose publicly.

Tokens (design §5.32 "Feed"): `OIBOT_FEED_TOKEN` is the shared one (one guild served: it feeds that guild, as always);
`OIBOT_FEED_TOKEN_<KEY>` belongs to one guild (key upper-cased, anything not a letter or digit → `_`). A companion
authenticated with a guild's token feeds that guild only, whatever its hello names; with several guilds the shared
token feeds no real raid (mock sessions only), so a token handed to one guild's raid leader can't reach another guild.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

Handler = Callable[[dict[str, Any], "Companion"], Awaitable[dict[str, Any] | None]]
QUIET_AFTER_S = 120  # no event for this long: "quiet" (breaks and trash pulls are quiet every night — no line, amber)
GONE_KEEP_S = 6 * 3600  # a disconnected companion stays listed (state "disconnected") this long


class Companion:
    def __init__(self, character: str, client: str, guild: str, peer: str, bound: str | None = None):
        self.character, self.client, self.guild, self.peer = character, client, guild, peer
        self.named = guild  # what its hello said (the note when it differs from its token's guild)
        self.bound = bound  # the guild key its token belongs to (OIBOT_FEED_TOKEN_<KEY>); None = the shared token
        if bound:
            self.guild = bound  # the token decides; status and routing read this
        self.connected_at = datetime.now(timezone.utc)
        self.last_seen = self.connected_at
        self.events = 0
        self.disconnected_at: datetime | None = None
        self.was_disconnected = False  # the same character/guild reconnected after a drop (the "back" line)

    def touch(self) -> None:
        self.last_seen = datetime.now(timezone.utc)

    @property
    def silent_for(self) -> float:
        return (datetime.now(timezone.utc) - self.last_seen).total_seconds()

    @property
    def state(self) -> str:
        """live | quiet | disconnected (design §3.1 L22)."""
        if self.disconnected_at is not None:
            return "disconnected"
        return "quiet" if self.silent_for > QUIET_AFTER_S else "live"


class FeedServer:
    def __init__(self, handler: Handler, token: str | None, bind: str = "127.0.0.1:8787",
                 auth: Callable[[str], tuple[bool, str | None]] | None = None):
        """`auth(token) → (ok, guild key the token belongs to | None)`: the bot's per-guild tokens; without it, the one
        shared `token`."""
        self.handler, self.token, self.auth = handler, token, auth
        host, _, port = bind.rpartition(":")
        self.host, self.port = host or "127.0.0.1", int(port or 8787)
        self.companions: dict[str, Companion] = {}  # peer -> companion
        self.gone: dict[tuple, Companion] = {}  # (character, guild) -> a companion whose socket closed (presence: disconnected)
        self.on_disconnect: Callable[["Companion"], Awaitable[None]] | None = None  # the bot's thread line
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
                ok, bound = self.authorize(ev.get("token")) if ev.get("type") == "hello" else (False, None)
                if not ok:
                    await ws.send_json({"type": "error", "message": "hello with a valid token first"})
                    await ws.close()
                    break
                comp = Companion(ev.get("character", "?"), ev.get("client", "?"), ev.get("guild", ""), peer, bound=bound)
                if self.gone.pop((comp.character, comp.guild), None) is not None:
                    comp.was_disconnected = True
                self.companions[peer] = comp
                reply = await self.handler(ev, comp)
                await ws.send_json({"type": "welcome", **(reply or {})})
                continue
            comp.touch()
            idem = ev.get("idem")
            try:
                reply = await self.handler(ev, comp) or {}
                if reply.get("error"):  # refused (routing): not an ack — the companion prints it and may re-send
                    await ws.send_json({"type": "error", "idem": idem, "message": str(reply["error"])[:300]})
                    continue
                comp.events += 1
                await ws.send_json({"type": "ack", "idem": idem, **reply})
            except Exception as e:  # noqa: BLE001
                await ws.send_json({"type": "error", "idem": idem, "message": str(e)[:200]})
        if comp is not None and self.companions.get(peer) is comp:
            self.companions.pop(peer, None)
            comp.disconnected_at = datetime.now(timezone.utc)
            self.gone[(comp.character, comp.guild)] = comp
            if self.on_disconnect is not None:
                try:
                    await self.on_disconnect(comp)
                except Exception:  # noqa: BLE001 — presence is a courtesy
                    pass
        return ws

    def authorize(self, token: Any) -> tuple[bool, str | None]:
        if self.auth is not None:
            return self.auth(str(token or ""))
        return same(token, self.token), None

    def status(self, guild: str | None = None, resolve: Callable[["Companion"], str | None] | None = None) -> str:
        """The connected companions as one line; `guild` (a key) keeps only those feeding that guild — the companion's
        `guild` as is, or through `resolve(companion)` (the bot's routing: the guild it counts for, or None)."""
        comps = list(self.companions.values())
        if guild is not None:
            comps = [c for c in comps if (resolve(c) if resolve else c.guild) == guild]
        if not comps:
            return "no companion connected"
        return "; ".join(f"{c.character} ({c.client}) {c.events} events, seen {int(c.silent_for)}s ago" for c in comps)

    def presence(self, guild: str | None = None, resolve: Callable[["Companion"], str | None] | None = None) -> list[dict]:
        """Connected and recently disconnected companions as rows: {character, client, guild, state, last_seen (ISO),
        silent_s, events} — `guild`/`resolve` filter as in `status`. Disconnected ones are kept GONE_KEEP_S."""
        now = datetime.now(timezone.utc)
        self.gone = {k: c for k, c in self.gone.items() if c.disconnected_at and (now - c.disconnected_at).total_seconds() < GONE_KEEP_S}
        live_keys = {(c.character, c.guild) for c in self.companions.values()}
        comps = list(self.companions.values()) + [c for k, c in self.gone.items() if k not in live_keys]
        if guild is not None:
            comps = [c for c in comps if (resolve(c) if resolve else c.guild) == guild]
        return [{"character": c.character, "client": c.client, "guild": c.guild, "state": c.state, "events": c.events,
                 "last_seen": (c.disconnected_at or c.last_seen).isoformat(timespec="seconds"), "silent_s": int(c.silent_for)} for c in comps]


GUILD_TOKEN_PREFIX = "OIBOT_FEED_TOKEN_"


def same(a: Any, b: str | None) -> bool:
    """A token comparison in constant time; an empty token never matches."""
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def token_suffix(key: str) -> str:
    """A guild key as the suffix of its feed token's variable: OIBOT_FEED_TOKEN_<this>."""
    return re.sub(r"[^A-Za-z0-9]", "_", key).upper()


def feed_config() -> tuple[str | None, str]:
    """(shared token, bind) from the environment; no shared token and no guild token disables the listener."""
    return os.environ.get("OIBOT_FEED_TOKEN") or None, os.environ.get("OIBOT_FEED_BIND", "127.0.0.1:8787")


def guild_tokens(env: dict | None = None) -> dict[str, str]:
    """{suffix: token} for every OIBOT_FEED_TOKEN_<KEY> set (values never printed)."""
    env = os.environ if env is None else env
    return {k[len(GUILD_TOKEN_PREFIX):].upper(): v for k, v in env.items() if k.startswith(GUILD_TOKEN_PREFIX) and v}


def authorize(token: str, shared: str | None, per_guild: dict[str, str], keys: Callable[[], list[str]]) -> tuple[bool, str | None]:
    """A hello's token → (ok, guild key it belongs to). A guild's own token first (so the owner may give the guild token
    the shared value and leave the companion as it is); a guild token naming no guild served is refused; then the
    shared one (bound to no guild). `keys()`: the guild keys served now (a guild claimed at run time counts at once)."""
    for suffix, t in per_guild.items():
        if same(token, t):
            key = next((k for k in keys() if token_suffix(k) == suffix), None)
            if key:
                return True, key
    return same(token, shared), None
