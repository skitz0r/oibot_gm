"""FastAPI app factory: Discord OAuth, icon/emblem/card images, the JSON API (api.py) and the React app at /app.
The old server-rendered pages are gone; their URLs redirect into the app.

Env: OIBOT_WEB_BIND (host:port, unset = web off), OIBOT_WEB_URL (public base, e.g. https://gm.earlyandoften.gg),
DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET (OAuth2), OIBOT_WEB_SECRET (cookie signing),
OIBOT_WEB_SESSION_EPOCH (unix time or ISO datetime: every session issued before it is logged out),
OIBOT_WEB_DEV_USER (a Discord id: skip OAuth on localhost while developing)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer

from .. import comp as comp_mod, raidcycle as rc, render
from ..registry import bank_rows, pool_health_data

HERE = Path(__file__).parent
DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH = "https://discord.com/oauth2/authorize"
COOKIE = "oibot_session"
NONCE_COOKIE = "oibot_oauth"  # short-lived: binds the OAuth round trip to the browser that started it
STATE_TTL_S = 600  # a login must complete within 10 minutes of the redirect
SESSION_MAX_AGE_S = 30 * 86400


def web_config() -> tuple[str | None, str]:
    """(bind, public url); bind None disables the dashboard."""
    return os.environ.get("OIBOT_WEB_BIND") or None, os.environ.get("OIBOT_WEB_URL", "http://127.0.0.1:8788")


def session_epoch() -> float:
    """OIBOT_WEB_SESSION_EPOCH: sessions issued before this moment are invalid (a way to log everyone out).
    A unix timestamp or an ISO datetime; unset or unparsable = no cutoff."""
    raw = (os.environ.get("OIBOT_WEB_SESSION_EPOCH") or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone

        t = datetime.fromisoformat(raw)
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


def safe_next(target: str | None) -> str:
    """Only in-app paths are valid post-login destinations (no open redirect, no protocol-relative URLs)."""
    t = (target or "").strip()
    return t if t.startswith("/app") and not t.startswith("/app//") and "\\" not in t else "/app/me"


def session_valid(s: dict | None, now: float | None = None) -> bool:
    """A signed session is honoured only with an issue time inside the 30-day window and after the revocation epoch."""
    if not s or "uid" not in s:
        return False
    try:
        iat = float(s.get("iat"))
    except (TypeError, ValueError):
        return False  # pre-hardening cookies carry no iat: log in again
    now = time.time() if now is None else now
    return iat <= now + 60 and now - iat <= SESSION_MAX_AGE_S and iat >= session_epoch()


class Viewer:
    def __init__(self, uid: int, name: str, reg, officer: bool, owner: bool):
        self.uid, self.name, self.reg, self.officer, self.owner = uid, name, reg, officer, owner
        self.member = reg.members.get(uid) if reg else None


def create_app(bot) -> FastAPI:
    app = FastAPI(title="oibot_GM", docs_url=None, redoc_url=None)
    signer = URLSafeSerializer(os.environ.get("OIBOT_WEB_SECRET") or secrets.token_hex(32), salt="session")
    _, public_url = web_config()
    client_id, client_secret = os.environ.get("DISCORD_CLIENT_ID"), os.environ.get("DISCORD_CLIENT_SECRET")
    # Dev login (no Discord OAuth) only when explicitly switched on AND the site is not public: never behind the tunnel.
    _bind, _url = web_config()
    dev_user = os.environ.get("OIBOT_WEB_DEV_USER") if os.environ.get("OIBOT_WEB_DEV") == "1" and not _url.startswith("https") else None
    card_cache: dict[str, tuple[float, str, bytes]] = {}  # key -> (time, data head, png)

    # ---- identity
    def session(request: Request) -> dict | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            s = signer.loads(raw)
        except BadSignature:
            return None
        return s if session_valid(s) else None  # too old, or issued before OIBOT_WEB_SESSION_EPOCH → logged out

    async def guild_member(guild, uid: int, max_age: float = 300):
        """Live guild member (roles decide the tier): the bot's TTL-cached lookup (get_member, then a REST fetch)."""
        return await bot.cached_member(guild, uid, max_age)

    async def viewer(request: Request) -> Viewer | None:
        """None = not logged in. Raises 403 for a Discord user who is not in the guild's server:
        the server's membership and roles are the whitelist — there is no separate user list."""
        s = session(request)
        if not s and dev_user and request.client and request.client.host in ("127.0.0.1", "::1"):
            s = {"uid": int(dev_user), "name": "dev"}
        if not s:
            return None
        uid = int(s["uid"])
        reg = next(iter(bot.registries.by_discord.values()), None)  # single-guild deployment
        if reg is None:
            return None
        guild = bot.get_guild(reg.config.discord_guild_id)
        member = await guild_member(guild, uid) if guild else None
        if member is None and reg.config.owner_discord_id != uid:
            raise HTTPException(status_code=403, detail=f"Your Discord account isn't a member of the {reg.config.name} server.")
        officer = bot.officiates(member, guild) if member else True
        owner = reg.config.owner_discord_id == uid
        name = member.display_name if member else (reg.members[uid].display_name if uid in reg.members else s.get("name", str(uid)))
        return Viewer(uid, name, reg, officer, owner)

    async def privilege(reg, uid: int, max_age: float = 60) -> str:
        """owner | officer | member | outside — from Discord roles (Manage Server or the configured officer role), never stored."""
        if reg.config.owner_discord_id == uid:
            return "owner"
        guild = bot.get_guild(reg.config.discord_guild_id)
        m = await guild_member(guild, uid, max_age=max_age) if guild else None
        if m is None:
            return "outside"
        return "officer" if bot.officiates(m, guild) else "member"

    async def need(request: Request, officer: bool = False) -> Viewer:
        v = await viewer(request)
        if v is None:
            raise HTTPException(status_code=307, headers={"Location": "/auth/login?next=" + request.url.path})
        if officer and not v.officer:
            raise HTTPException(status_code=403, detail="Officers only.")
        return v

    def guild_reg():
        return next(iter(bot.registries.by_discord.values()), None)  # single-guild deployment

    # ---- auth
    @app.get("/auth/login")
    async def login(request: Request, next: str = "/"):
        if not client_id:
            return HTMLResponse("<p>Login isn't configured yet (DISCORD_CLIENT_ID missing).</p>", status_code=503)
        # The signed state carries a hash of a nonce that only this browser holds (short-lived cookie): a state
        # captured elsewhere can't complete a login here, and a stale one expires with the cookie.
        nonce = secrets.token_urlsafe(24)
        state = signer.dumps({"next": safe_next(next), "t": time.time(), "n": hashlib.sha256(nonce.encode()).hexdigest()})
        q = urlencode({"client_id": client_id, "redirect_uri": f"{public_url}/auth/callback", "response_type": "code", "scope": "identify", "state": state, "prompt": "none"})
        resp = RedirectResponse(f"{DISCORD_OAUTH}?{q}")
        resp.set_cookie(NONCE_COOKIE, nonce, max_age=STATE_TTL_S, httponly=True, secure=public_url.startswith("https"), samesite="lax", path="/auth")
        return resp

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        try:
            st = signer.loads(state)
        except BadSignature:
            raise HTTPException(400, "bad state")
        try:
            issued = float(st.get("t") or 0)
        except (TypeError, ValueError):
            issued = 0.0
        if not (0 <= time.time() - issued <= STATE_TTL_S):
            raise HTTPException(400, "login took too long; start again")
        nonce = request.cookies.get(NONCE_COOKIE) or ""
        if not nonce or not secrets.compare_digest(hashlib.sha256(nonce.encode()).hexdigest(), str(st.get("n") or "")):
            raise HTTPException(400, "login didn't start in this browser; start again")
        if not code:
            raise HTTPException(400, "Discord refused the login")
        async with httpx.AsyncClient(timeout=15) as hc:
            tok = await hc.post(f"{DISCORD_API}/oauth2/token", data={"client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": f"{public_url}/auth/callback"})
            if tok.status_code != 200:
                raise HTTPException(400, "Discord refused the login")
            me = await hc.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        u = me.json()
        resp = RedirectResponse(safe_next(st.get("next")), status_code=303)
        resp.set_cookie(COOKIE, signer.dumps({"uid": u["id"], "name": u.get("global_name") or u["username"], "iat": time.time()}), httponly=True, secure=public_url.startswith("https"), samesite="lax", max_age=SESSION_MAX_AGE_S)
        resp.delete_cookie(NONCE_COOKIE, path="/auth")
        return resp

    @app.get("/auth/logout")
    async def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    thumb_cache: dict[str, bytes] = {}

    @app.get("/img/raid/{rid}.png")
    async def raid_thumb(rid: str):
        reg = next(iter(bot.registries.by_discord.values()), None)
        if reg is None or rid not in reg.profile.raids:
            raise HTTPException(404)
        rd = reg.raid_def(rid)
        key = f"{rid}:{rd.get('size')}:{rd.get('lockout_days')}"
        if key not in thumb_cache:
            thumb_cache[key] = await asyncio.to_thread(render.raid_thumb_png, rid, rd.get("name", rid), int(rd.get("size") or 0), int(rd.get("lockout_days") or 7))
        return Response(thumb_cache[key], media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

    badge_cache: dict[str, bytes] = {}
    ICON_DIR = Path(__file__).resolve().parents[3] / "out" / "icons"
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    ICON_CDN = "https://render.worldofwarcraft.com/us/icons/56/{name}.jpg"

    def icon_url(kind: str, key: str) -> str:
        """Blizzard's icon art (proxied + cached) when the profile names one, else our generated badge."""
        reg = next(iter(bot.registries.by_discord.values()), None)
        icons = reg.profile.icons if reg else {}
        name = None
        if kind == "class":
            name = icons.get("classes", {}).get(key)
        elif kind == "role":
            name = icons.get("roles", {}).get(key)
        elif kind == "spec":
            name = icons.get("specs", {}).get(key)
        elif kind == "art":
            name = key or None
        if name:
            return f"/img/icon/{name}.jpg"
        return f"/img/{kind}/{key}.png" if kind in ("class", "role") else "/img/role/melee.png"

    icon_miss: dict[str, float] = {}  # names the CDN answered 404 for → when that answer expires (1 h): scanners can't drive upstream fetches
    ICON_MISS_TTL = 3600

    @app.get("/img/icon/{name}.jpg")
    async def cdn_icon(name: str):
        if not re.fullmatch(r"[a-z0-9_]{3,64}", name):
            raise HTTPException(404)
        f = ICON_DIR / f"{name}.jpg"
        if not f.exists():
            now = time.time()
            if icon_miss.get(name, 0) > now:
                raise HTTPException(404)
            try:
                async with httpx.AsyncClient(timeout=10) as hc:
                    r = await hc.get(ICON_CDN.format(name=name))
                if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                    if len(icon_miss) > 2000:  # keep the negative cache bounded; drop what has expired
                        for k in [k for k, t in icon_miss.items() if t <= now]:
                            icon_miss.pop(k, None)
                    icon_miss[name] = now + ICON_MISS_TTL
                    raise HTTPException(404)
                f.write_bytes(r.content)
            except httpx.HTTPError:
                raise HTTPException(502)
        return Response(f.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/img/class/{cls}.png")
    async def class_icon(cls: str):
        if cls not in render.CLASS:
            raise HTTPException(404)
        key = f"class:{cls}"
        if key not in badge_cache:
            badge_cache[key] = render.class_badge_png(cls, 64)
        return Response(badge_cache[key], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/img/role/{role}.png")
    async def role_icon(role: str):
        if role not in render.ROLE_COLOUR:
            raise HTTPException(404)
        key = f"role:{role}"
        if key not in badge_cache:
            badge_cache[key] = render.role_badge_png(role, 64)
        return Response(badge_cache[key], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "bot": str(bot.user) if bot.user else None, "guilds": len(bot.registries.by_discord)}

    LANDING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>oibot_GM</title><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@700&family=Source+Sans+3:wght@400;600&display=swap">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0F1318;color:#EAEFF5;font:16px/1.5 "Source Sans 3",system-ui,sans-serif}
.card{background:#181E26;border:1px solid #313B48;border-radius:12px;padding:28px 32px;max-width:420px;margin:16px}h1{font:700 26px Manrope,sans-serif;margin:0 0 6px}
p{color:#8C97A8;margin:0 0 18px}a{display:inline-block;background:#38B2A0;color:#0B1512;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600}</style></head>
<body><div class="card"><h1>{name}</h1><p>Raid administration for the guild: characters, availability, rosters, sheets. Log in with Discord — only members of the server get in.</p>
<a href="/auth/login?next=/app/me">Log in with Discord</a></div></body></html>"""

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        v = await viewer(request)
        if v is None:
            reg = guild_reg()
            return HTMLResponse(LANDING.replace("{name}", reg.config.name if reg else "oibot_GM"))
        return RedirectResponse("/app/me", status_code=302)

    for _old, _new in (("/admin", "/app/members"), ("/admin/build", "/app/rosters"), ("/bank", "/app/members"), ("/rosters", "/app/rosters"), ("/raids", "/app/raids"), ("/config", "/app/config"), ("/ops", "/app/ops")):
        app.add_api_route(_old, (lambda new: (lambda: RedirectResponse(new, status_code=301)))(_new), methods=["GET"])

    # ---- cards (PNG, rendered on demand, cached per data-repo head)
    async def cached_png(key: str, fn) -> bytes:
        head = bot.registries.store.head()
        hit = card_cache.get(key)
        if hit and hit[1] == head and time.time() - hit[0] < 600:
            return hit[2]
        png = await asyncio.to_thread(fn)
        card_cache[key] = (time.time(), head, png)
        return png

    @app.get("/card/bank.png")
    async def card_bank(request: Request):
        v = await need(request, officer=True)
        reg = v.reg

        def build():
            rows = bank_rows(reg)
            return render.bank_png(f"Character bank · {reg.config.name}", f"{len(rows)} members", rows)

        return Response(await cached_png("bank", build), media_type="image/png")

    @app.get("/card/{kind}/{key}.png")
    async def card(request: Request, kind: str, key: str):
        v = await need(request, officer=True)
        reg = v.reg
        t = reg.config.roster(key) or {"key": key, "name": key, "size": 20}

        def build():
            if kind == "pool":
                h = pool_health_data(reg, t)
                n, size, alts, on_roster = h["headcount"]
                return render.health_png(f"Pool readiness · {t.get('name', key)} ({size}-man)", "every planned or active main counts", h["headcount"], h["roles"], h["buffs"], h["unresponsive"],
                                         headcount_text=f"{n}/{size} mains · {alts} alts · {on_roster} on roster", unresponsive_label="Not on this roster", buff_hint="badge = buff · name = provider · red outline = nobody in the pool brings it")
            if kind == "groups":
                players, result, cov, labels = comp_mod.optimize(reg, t)
                if result is None:
                    return render.health_png(f"Optimised groups · {key}", "waiting for registrations", (len(players), int(t.get("size") or 20), 0, 0), [], [], [], headcount_text=f"{len(players)} mains")
                rb = comp_mod.raid_buff_status(reg.profile, players)
                return render.groups_png(reg.profile, players, result, cov, rb, f"Optimised groups · {t.get('name', key)} ({t.get('size', 20)}-man)", f"{len(result.selected)} of {len(players)} mains placed", reg.profile.buff_assumptions(), labels)
            if kind == "comp":
                players = comp_mod.pool_players(reg)
                ic = comp_mod.ideal_comp(reg.profile, int(t.get("size") or 20), players, t.get("comp_targets") or {}, t.get("instance"), reg)
                return render.comp_png(ic.lines, f"Desired comp · {t.get('name', key)} ({ic.size}-man, {ic.groups} groups)", "derived from the buff matrix and comp rules", ic.notes)
            if kind == "health":
                rs = bot.raids.store(reg)
                ev = rs.events.get(key)
                if not ev:
                    raise HTTPException(404)
                tt = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
                h = rc.health_data(reg, ev, tt)
                return render.health_png(f"Roster health · {tt.get('name', ev.team)} · {ev.key}", reg.local(ev.start, "%a %b %d %H:%M %Z"), h["headcount"], h["roles"], h["buffs"], h["unresponsive"])
            raise HTTPException(404)

        return Response(await cached_png(f"{kind}:{key}", build), media_type="image/png")

    from .api import install_api

    install_api(app, bot, viewer=viewer, icon_url=icon_url, privilege=privilege)
    return app


async def serve(bot) -> None:
    """Run uvicorn inside the bot's event loop when OIBOT_WEB_BIND is set."""
    bind, url = web_config()
    if not bind:
        print("web dashboard disabled (set OIBOT_WEB_BIND, e.g. 127.0.0.1:8788)")
        return
    import uvicorn

    host, _, port = bind.rpartition(":")
    # Trust X-Forwarded-* only from the local cloudflared, never from arbitrary clients
    config = uvicorn.Config(create_app(bot), host=host or "127.0.0.1", port=int(port), log_level="warning", proxy_headers=True, forwarded_allow_ips=os.environ.get("OIBOT_WEB_PROXY_IPS", "127.0.0.1"))
    server = uvicorn.Server(config)
    print(f"web dashboard on http://{bind} (public {url})")
    await server.serve()
