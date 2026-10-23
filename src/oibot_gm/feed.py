"""Loot feed listener: the companion on the raider's PC streams chat-log events
over a WebSocket (tailnet), the bot routes them to the active raid.

Protocol (JSON text frames):
  client → {"type":"hello","token":"…","character":"Rhozy","client":"oibot-companion/0.1","guild":"oi"}
  client → {"type":"drop","boss":"Supremus"|null,"items":[{"id":32262,"name":"…"}],"source":"gargul","ts":"…","idem":"…"}
  client → {"type":"loot","recipient":"Boviche","item_id":32262,"item_name":"…","ts":"…","idem":"…"}
  client → {"type":"kill","boss":"Supremus","ts":"…","idem":"…"}
  client → {"type":"heartbeat","ts":"…"}
  server → {"type":"ack","idem":"…","session":"<guild>:<run>"} | {"type":"error","idem":"…","message":"…"} | {"type":"welcome","routes_to":"<run>","session":"<guild>:<run>"}
`ts` is epoch seconds (the companion turns the chat log's local stamp into one on the raider's own PC, whose clock and
zone wrote it; the bot renders a native Discord stamp, <t:unix:t>, in each reader's own zone). An ISO stamp WITH an offset
is read as is. A KILL whose `ts` is a clock time only ('20:15', or a zone-less ISO stamp — an old companion) is refused
rather than guessed into the guild's zone (design §3.5 L12): the companion gets an error naming the update, the guild's
ops channel one "companion too old" warn per companion (`on_warn`). The companion
keeps an event until its ack and re-sends it after a reconnect with the same idem; the bot dedupes per loot SESSION
(LootSession.seen_idem), not per connection, so a re-send is acked ("dup") and never applied twice. An error reply is
not an ack (the companion may re-send). Presence per companion: live · quiet (no event for QUIET_AFTER_S) · disconnected.
Bind to the tailnet address (OIBOT_FEED_BIND=100.x.y.z:8787); never expose publicly.

Tokens (design §5.32 "Feed"): `OIBOT_FEED_TOKEN` is the shared one (one guild served: it feeds that guild, as always);
`OIBOT_FEED_TOKEN_<KEY>` belongs to one guild (key upper-cased, anything not a letter or digit → `_`). A companion
authenticated with a guild's token feeds that guild only, whatever its hello names; with several guilds the shared
token feeds no real raid (mock sessions only), so a token handed to one guild's raid leader can't reach another guild.

Issued tokens (design §5.50): the owner issues a guild's companion token from Config › Companions or /gm config companion
— stored HASHED in guild.yaml (`feed_tokens[]`, Registry.issue_feed_token), shown once, revocable. `authorize_any` takes
them beside the .env ones (the .env per-guild token first, then the issued ones by hash in constant time, then the shared
one) and binds the companion to the token's guild; the server re-checks an issued token at EVERY frame (`still_valid`), so
a revoke disconnects the companion at its next message without a restart ({"type":"error","auth":true,…}: the companion
stops retrying). `auth:true` is kept for a token that is genuinely unknown or revoked: when the bot can't tell for now (the
token's guild didn't load, is parked, or the lookup failed) the answer is a plain error and the socket closes, so the
companion reconnects with its backoff instead of exiting mid-raid (UNAVAILABLE; nothing is applied meanwhile). Token
values are never logged, printed, stored in the data repo or written to an ops line — only the id and the label.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], "Companion"], Awaitable[dict[str, Any] | None]]
QUIET_AFTER_S = 120  # no event for this long: "quiet" (breaks and trash pulls are quiet every night — no line, amber)
GONE_KEEP_S = 6 * 3600  # a disconnected companion stays listed (state "disconnected") this long


REVOKED = "this companion's token was revoked — ask the guild's owner for a new one (Config › Companions)"
BAD_TOKEN = "hello with a valid token first"
UNAVAILABLE = "the bot can't check this companion's token right now (its guild isn't being served) — retrying"


class TokenUnverifiable(Exception):
    """An issued-token lookup can't answer for now (a guild that didn't load or is parked might hold it): the hello is
    refused WITHOUT auth:true, so the companion retries instead of exiting."""


class Companion:
    def __init__(self, character: str, client: str, guild: str, peer: str, bound: str | None = None, token_id: str | None = None,
                 label: str | None = None):
        self.character, self.client, self.guild, self.peer = character, client, guild, peer
        self.named = guild  # what its hello said (the note when it differs from its token's guild)
        self.bound = bound  # the guild key its token belongs to (OIBOT_FEED_TOKEN_<KEY> or an issued token); None = the shared token
        self.token_id, self.label = token_id, label  # an ISSUED token's record id and label (None: a .env token)
        self.ws = None  # the socket (FeedServer.close_token closes it on a revoke)
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
                 auth: Callable[[str], tuple] | None = None):
        """`auth(token) → (ok, guild key the token belongs to | None[, issued token id | None, label])`: the bot's
        per-guild tokens; without it, the one shared `token`. `still_valid(companion)`: is its issued token still
        unrevoked (read at every frame); `on_seen(companion)`: a frame arrived (the token's last_seen)."""
        self.handler, self.token, self.auth = handler, token, auth
        self.still_valid: Callable[["Companion"], bool] | None = None
        self.on_seen: Callable[["Companion"], None] | None = None
        host, _, port = bind.rpartition(":")
        self.host, self.port = host or "127.0.0.1", int(port or 8787)
        self.companions: dict[str, Companion] = {}  # peer -> companion
        self.gone: dict[tuple, Companion] = {}  # (character, guild) -> a companion whose socket closed (presence: disconnected)
        self.on_disconnect: Callable[["Companion"], Awaitable[None]] | None = None  # the bot's thread line
        self.on_warn: Callable[["Companion", str], Awaitable[None]] | None = None  # the bot's ops warn (ops_warner)
        self.warned: set[tuple] = set()  # (character, guild, why): a warn goes out once per companion
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
                ok, bound, tid, label = self.authorize(ev.get("token")) if ev.get("type") == "hello" else (False, None, None, None)
                if ok is None:  # can't tell now (guild not served, lookup failed): a plain error — the companion reconnects
                    await ws.send_json({"type": "error", "message": UNAVAILABLE})
                    await ws.close()
                    break
                if not ok:
                    await ws.send_json({"type": "error", "auth": True, "message": BAD_TOKEN})
                    await ws.close()
                    break
                comp = Companion(ev.get("character", "?"), ev.get("client", "?"), ev.get("guild", ""), peer, bound=bound, token_id=tid, label=label)
                comp.ws = ws
                if self.gone.pop((comp.character, comp.guild), None) is not None:
                    comp.was_disconnected = True
                self.companions[peer] = comp
                self.seen(comp)
                reply = await self.handler(ev, comp)
                await ws.send_json({"type": "welcome", **(reply or {})})
                continue
            valid = self.valid(comp)
            if valid is False:  # revoked since the hello: refused at this frame, no restart (design §5.50)
                await ws.send_json({"type": "error", "auth": True, "idem": ev.get("idem"), "message": REVOKED})
                await ws.close()
                break
            if valid is None:  # can't tell now: not applied, not acked, no auth — the event stays in flight and the
                await ws.send_json({"type": "error", "message": UNAVAILABLE})  # companion re-sends it after reconnecting
                await ws.close()
                break
            comp.touch()
            self.seen(comp)
            idem = ev.get("idem")
            refusal = refuse_event(ev)
            if refusal is not None:  # not an ack: the companion prints it (and gives up after its retry window)
                await ws.send_json({"type": "error", "idem": idem, "message": refusal})
                await self.warn_once(comp, "too_old", f"companion {comp.character} ({comp.client}) is too old: {refusal}")
                continue
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

    async def warn_once(self, comp: "Companion", why: str, text: str) -> bool:
        """One warn per (companion, reason) for the life of the process — the companion re-sends a refused event every
        30 s, the ops channel hears of it once. False when it was said already."""
        key = (comp.character, comp.guild, why)
        if key in self.warned:
            return False
        self.warned.add(key)
        if self.on_warn is not None:
            try:
                await self.on_warn(comp, text)
            except Exception:  # noqa: BLE001 — a warn is a courtesy
                log.exception("feed warn failed")
        else:
            log.warning(text)
        return True

    def authorize(self, token: Any) -> tuple[bool | None, str | None, str | None, str | None]:
        """(ok, guild key, issued token id, label) — `auth` may answer the older (ok, guild) pair. ok None = can't tell
        now (the check failed or the token's guild isn't served): refused, but not as a bad token."""
        if self.auth is not None:
            try:
                got = tuple(self.auth(str(token or "")))
            except Exception as e:  # noqa: BLE001 — never the token in the log: the exception's type only
                log.error("feed token check failed: %s", type(e).__name__)
                return None, None, None, None
            return (*got, None, None, None)[:4]
        return same(token, self.token), None, None, None

    def valid(self, comp: "Companion") -> bool | None:
        """An issued token is re-checked at every frame (a revoke takes effect at once); a .env token always holds.
        True = good, False = revoked (or never issued), None = can't tell now (its guild isn't served, the lookup failed):
        the frame is refused either way — only False tells the companion its token is dead."""
        if comp.token_id is None or self.still_valid is None:
            return True
        try:
            got = self.still_valid(comp)
        except Exception as e:  # noqa: BLE001 — a lookup that fails never lets a frame through, nor ends the companion
            log.error("feed token check failed: %s", type(e).__name__)
            return None
        return None if got is None else bool(got)

    def seen(self, comp: "Companion") -> None:
        if comp.token_id is not None and self.on_seen is not None:
            try:
                self.on_seen(comp)
            except Exception:  # noqa: BLE001 — last_seen is bookkeeping
                log.exception("feed last_seen failed")

    async def close_token(self, token_id: str) -> int:
        """Disconnect every companion using the issued token `token_id` now (the revoke's courtesy — the per-frame check is
        the guarantee). How many sockets were closed."""
        n = 0
        for comp in [c for c in self.companions.values() if c.token_id == token_id]:
            ws = comp.ws
            if ws is None or ws.closed:
                continue
            try:
                await ws.send_json({"type": "error", "auth": True, "message": REVOKED})
                await ws.close()
                n += 1
            except Exception:  # noqa: BLE001 — a socket already gone
                pass
        return n

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
                 "last_seen": (c.disconnected_at or c.last_seen).isoformat(timespec="seconds"), "silent_s": int(c.silent_for),
                 "token_id": getattr(c, "token_id", None), "label": getattr(c, "label", None)} for c in comps]


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


def authorize_any(token: str, shared: str | None, per_guild: dict[str, str], keys: Callable[[], list[str]],
                  issued: Callable[[str], tuple[str, str, str] | None] | None = None) -> tuple[bool, str | None, str | None, str | None]:
    """A hello's token → (ok, guild key, issued token id, label): a guild's .env token first (as `authorize`), then an
    issued token by hash (`issued(token)` → (guild key, token id, label) or None — Registry.match_feed_token over the
    guilds served), then the shared one. ok None: no match, but `issued` raised TokenUnverifiable (a guild that might
    hold it isn't served now) — refused without auth:true, so the companion retries."""
    for suffix, t in per_guild.items():
        if same(token, t):
            key = next((k for k in keys() if token_suffix(k) == suffix), None)
            if key:
                return True, key, None, None
    unsure = False
    if token and issued is not None:
        try:
            hit = issued(str(token))
        except TokenUnverifiable:
            hit, unsure = None, True
        if hit:
            return True, hit[0], hit[1], hit[2]
    if same(token, shared):
        return True, None, None, None
    return (None if unsure else False), None, None, None


TOKEN_PREFIX = "oibot-feed-"


def new_token() -> str:
    """A fresh companion token: 256 random bits, URL-safe, with a recognisable prefix (a leak scanner can spot it)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """What guild.yaml keeps of a companion token: 'sha256:<hex>'. The token is 256 random bits, so a plain digest is
    enough (there is no dictionary to guess from); it is never reversed, only compared (hmac.compare_digest)."""
    return "sha256:" + hashlib.sha256(str(token).encode()).hexdigest()


# ---------------------------------------------------------------- stamps (design §3.5 L12)
TOO_OLD = ("companion too old: a kill must carry epoch seconds (ts), not a clock time the bot would have to guess a zone "
           "for — update oibot_companion.py from the repo's companion/ folder")
CLOCK_ONLY = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*$")


def ts_kind(ts: Any) -> str | None:
    """'epoch' (seconds since 1970) · 'zoned' (ISO with an offset) · 'clock' (a time of day, or a zone-less ISO stamp —
    the raider's PC clock with no zone) · None (no stamp)."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, bool):
        return "clock"
    if isinstance(ts, (int, float)):
        return "epoch"
    text = str(ts).strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return "epoch"
    if CLOCK_ONLY.match(text):
        return "clock"
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return "clock"
    return "zoned" if t.tzinfo is not None else "clock"


def ts_epoch(ts: Any) -> float | None:
    """The stamp as epoch seconds when it names an instant (ts_kind 'epoch' — a number or a digit string — or 'zoned'),
    else None (a clock time or a zone-less stamp: the raider's PC clock, no instant to name)."""
    kind = ts_kind(ts)
    if kind == "epoch":
        try:
            return float(ts)
        except (TypeError, ValueError):
            return None
    if kind == "zoned":
        return datetime.fromisoformat(str(ts).strip()).timestamp()
    return None


def refuse_event(ev: dict) -> str | None:
    """Why the bot refuses a companion event before routing it, or None: a KILL stamped with a clock time only (an old
    companion) — the bot never guesses which zone the raider's PC was in. A kill with no stamp is taken (received now)."""
    if ev.get("type") == "kill" and ts_kind(ev.get("ts")) == "clock":
        return TOO_OLD
    return None


def ops_warner(bot) -> Callable[["Companion", str], Awaitable[None]]:
    """FeedServer.on_warn for the bot: the warn goes to the ops channel of the guild the companion feeds
    (bot.companion_guild), else to the process log."""
    async def warn(comp: "Companion", text: str) -> None:
        gk = bot.companion_guild(comp) if hasattr(bot, "companion_guild") else None
        cfg_for = getattr(getattr(bot, "registries", None), "cfg_for", None)
        cfg = cfg_for(gk) if (gk and cfg_for) else None
        if cfg is not None and getattr(bot, "ops", None) is not None:
            await bot.ops.emit(cfg, "warn", text)
        else:
            log.warning(text)
    return warn


# ---------------------------------------------------------------- the companion tokens card (Loot › Companions)
def token_state(key: str, *, many: bool, env: dict | None = None) -> dict:
    """What the owner's card shows about the feed tokens for guild `key` — WHICH variables are set, never a value:
    {shared: OIBOT_FEED_TOKEN set, shared_feeds: whether the shared token feeds this guild's raids (one guild served),
    var: 'OIBOT_FEED_TOKEN_<KEY>', set: that variable set, listener: the feed listens at all, bind_set: OIBOT_FEED_BIND
    set (the address itself is a host fact and not shown)}."""
    env = os.environ if env is None else env
    shared = bool(env.get("OIBOT_FEED_TOKEN"))
    var = GUILD_TOKEN_PREFIX + token_suffix(key)
    own = bool(env.get(var))
    return {"shared": shared, "shared_feeds": shared and not many, "var": var, "set": own,
            "listener": shared or bool(guild_tokens(env)), "bind_set": bool(env.get("OIBOT_FEED_BIND"))}


# ---------------------------------------------------------------- issued tokens on the surfaces (design §5.50)
STATE_RANK = {"live": 0, "quiet": 1, "disconnected": 2}
STATE_WORDS = {"live": "live", "quiet": "quiet", "disconnected": "disconnected", "offline": "not connected", "never": "never connected",
               "revoked": "revoked"}


def token_rows(reg, presence: list[dict]) -> list[dict]:
    """The Companions hub's rows (Config › Companions, /gm config companion, MCP list_companions): one per issued token,
    newest first — {id, label, state (live | quiet | disconnected | offline | never | revoked), state_words, characters,
    events, last_seen (ISO), last_seen_label (12-hour guild time), issued_by, issued_label, sent_to, active, revoked_label,
    revoked_by, revoke_consequence}. `presence` = feed_presence rows (they carry token_id). Never a value, never a hash."""
    out = []
    for rec in reg.feed_tokens():
        mine = [p for p in presence if p.get("token_id") == rec.id]
        live = sorted(mine, key=lambda p: STATE_RANK.get(p.get("state"), 9))
        if not rec.active:
            state = "revoked"
        elif live:
            state = live[0].get("state") or "offline"
        else:
            state = "offline" if rec.last_seen else "never"
        stamps = [p.get("last_seen") for p in mine if p.get("last_seen")] + ([rec.last_seen] if rec.last_seen else [])
        seen = max(stamps, key=lambda t: datetime.fromisoformat(t)) if stamps else None
        row = {"id": rec.id, "label": rec.label, "state": state, "state_words": STATE_WORDS.get(state, state),
               "characters": sorted({str(p.get("character") or "?") for p in mine}), "events": sum(int(p.get("events") or 0) for p in mine),
               "last_seen": seen, "last_seen_label": reg.local12(seen) if seen else None, "issued_by": rec.issued_by,
               "issued_label": reg.local12(rec.issued_at), "sent_to": rec.sent_to, "active": rec.active,
               "revoked_label": reg.local12(rec.revoked_at) if rec.revoked_at else None, "revoked_by": rec.revoked_by}
        row["revoke_consequence"] = revoke_consequence(reg, rec, row) if rec.active else None
        out.append(row)
    return out


def token_line(row: dict) -> str:
    """One hub row in words (Discord, MCP): 'Jon's PC · live (Rhozy) · last seen Tue 9 Dec 8:14 PM'."""
    who = f" ({', '.join(row['characters'])})" if row.get("characters") and row["state"] in ("live", "quiet", "disconnected") else ""
    if row["state"] == "revoked":
        return f"~~{row['label']}~~ · revoked {row.get('revoked_label') or ''}".rstrip()
    seen = f" · last seen {row['last_seen_label']}" if row.get("last_seen_label") else ""
    return f"**{row['label']}** · {row['state_words']}{who}{seen}"


def issue_consequence(reg, label: str | None, name: str | None = None) -> str:
    """THE sentence of an issue: DMed once to `name` (Discord's confirm), or shown once to the owner (the site's hint)."""
    where = (f"DMed once to **{name}** — they start the companion with it on their PC. You won't see it." if name else
             "shown to you ONCE on this page — copy it to the raid leader's PC; it can't be shown again.")
    what = f"A new companion token “{label}”" if label else "A new companion token"
    return (f"{what} for **{reg.config.name}** is {where} It feeds this guild's raids only, works at once "
            "(no restart) and stops at its next message when you revoke it.")


def revoke_consequence(reg, rec, row: dict | None = None) -> str:
    """THE confirm sentence of a revoke (the decide screen, the site's confirm, MCP's preview)."""
    state = (row or {}).get("state")
    who = ", ".join((row or {}).get("characters") or [])
    on = f"connected now{f' ({who})' if who else ''} — it is cut off at its next message"
    now = {"live": on, "quiet": on, "disconnected": "not connected — it can't reconnect"}.get(state or "", "it can't connect again")
    return f"“{rec.label}” stops working: {now}. Whoever runs that companion needs a new token. Nothing else changes; nobody is DMed."
