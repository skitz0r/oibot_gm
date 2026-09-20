"""FastAPI app factory: Discord OAuth, icon/emblem/card images, the JSON API (api.py) and the React app at /app.
The old server-rendered pages are gone; their URLs redirect into the app.

Env: OIBOT_WEB_BIND (host:port, unset = web off), OIBOT_WEB_URL (public base, e.g. https://gm.earlyandoften.gg),
DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET (OAuth2), OIBOT_WEB_SECRET (cookie signing),
OIBOT_WEB_SESSION_EPOCH (unix time or ISO datetime: every session issued before it is logged out),
OIBOT_WEB_DEV_USER (a Discord id: skip OAuth on localhost while developing)."""
from __future__ import annotations

import asyncio
import hashlib
import html
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

from .. import render

HERE = Path(__file__).parent
DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH = "https://discord.com/oauth2/authorize"
COOKIE = "oibot_session"
NONCE_COOKIE = "oibot_oauth"  # short-lived: binds the OAuth round trip to the browser that started it
STATE_TTL_S = 600  # a login must complete within 10 minutes of the redirect
SESSION_MAX_AGE_S = 30 * 86400
CONFIRM_TTL_S = 300  # the "continue as <name>" page (login finished in another browser than it started in)
MOBILE_LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>oibot_GM · sign in</title>
<body style="margin:0;background:#171a1c;color:#e6e2d8;font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh">
<div style="max-width:22rem;padding:24px;text-align:center">
<p style="font-size:20px;font-weight:700;margin:0 0 8px">Sign in with Discord</p>
<p style="color:#a3a89f;margin:0 0 20px">If the Discord app is installed it opens and asks you to authorize; otherwise you sign in on Discord's page.</p>
<a href="{url}" style="display:inline-block;font-weight:600;padding:10px 22px;border-radius:8px;background:#5865f2;color:#fff;text-decoration:none">Continue with Discord</a>
</div></body>"""
CONFIRM_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>oibot_GM · sign in</title>
<body style="margin:0;background:#171a1c;color:#e6e2d8;font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh">
<form method="post" action="/auth/confirm" style="max-width:22rem;padding:24px;text-align:center">
<p style="font-size:20px;font-weight:700;margin:0 0 8px">Continue as {name}?</p>
<p style="color:#a3a89f;margin:0 0 20px">Discord finished signing you in from a different app or browser than the one you started in, so please confirm this is you.</p>
<input type="hidden" name="ticket" value="{ticket}">
<button style="font:inherit;font-weight:600;padding:10px 22px;border:0;border-radius:8px;background:#38b2a0;color:#0f1416;cursor:pointer">Continue</button>
<p style="margin:16px 0 0"><a href="/auth/logout" style="color:#a3a89f">Not you? Cancel</a></p>
</form></body>"""


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
    def __init__(self, uid: int, name: str, reg, officer: bool, owner: bool, via: str = "web"):
        self.uid, self.name, self.reg, self.officer, self.owner, self.via = uid, name, reg, officer, owner, via
        self.member = reg.members.get(uid) if reg else None


MCP_TOKEN_ENV = "OIBOT_MCP_TOKEN"


def bearer_ok(request, token: str | None) -> bool:
    """`Authorization: Bearer <OIBOT_MCP_TOKEN>` — the MCP server's identity (§5.24). No token configured = bearer auth
    off; the compare is constant-time so a probe learns nothing from timing."""
    if not token:
        return False
    auth = request.headers.get("authorization") or ""
    scheme, _, presented = auth.partition(" ")
    return scheme.lower() == "bearer" and bool(presented.strip()) and secrets.compare_digest(presented.strip(), token)


def mcp_viewer(request, bot, token: str | None) -> Viewer | None:
    """The bearer identity: the guild owner (every write is attributed to them, name 'mcp', via='mcp')."""
    if not bearer_ok(request, token):
        return None
    reg = next(iter(bot.registries.by_discord.values()), None)  # single-guild deployment
    if reg is None or not reg.config.owner_discord_id:
        return None
    return Viewer(int(reg.config.owner_discord_id), "mcp", reg, officer=True, owner=True, via="mcp")


def create_app(bot) -> FastAPI:
    app = FastAPI(title="oibot_GM", docs_url=None, redoc_url=None)
    signer = URLSafeSerializer(os.environ.get("OIBOT_WEB_SECRET") or secrets.token_hex(32), salt="session")
    _, public_url = web_config()
    client_id, client_secret = os.environ.get("DISCORD_CLIENT_ID"), os.environ.get("DISCORD_CLIENT_SECRET")
    # Dev login (no Discord OAuth) only when explicitly switched on AND the site is not public: never behind the tunnel.
    _bind, _url = web_config()
    dev_user = os.environ.get("OIBOT_WEB_DEV_USER") if os.environ.get("OIBOT_WEB_DEV") == "1" and not _url.startswith("https") else None

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
        if request.headers.get("authorization"):  # the MCP server: bearer token → the owner; a wrong token is nobody
            return mcp_viewer(request, bot, os.environ.get(MCP_TOKEN_ENV) or None)
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
        # Phones hand a discord.com link to the Discord app only when it is TAPPED (universal / app links ignore
        # redirects), so on mobile show a button instead of redirecting: one tap, and the app's "Authorize" sheet
        # opens with the account already signed in. Desktop keeps the straight redirect.
        ua = request.headers.get("user-agent", "")
        if re.search(r"iPhone|iPad|iPod|Android|Mobile", ua):
            resp = HTMLResponse(MOBILE_LOGIN_PAGE.format(url=html.escape(f"{DISCORD_OAUTH}?{q}", quote=True)))
        else:
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
        same_browser = bool(nonce) and secrets.compare_digest(hashlib.sha256(nonce.encode()).hexdigest(), str(st.get("n") or ""))
        if not code:
            raise HTTPException(400, "Discord refused the login")
        async with httpx.AsyncClient(timeout=15) as hc:
            tok = await hc.post(f"{DISCORD_API}/oauth2/token", data={"client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": f"{public_url}/auth/callback"})
            if tok.status_code != 200:
                raise HTTPException(400, "Discord refused the login")
            me = await hc.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        u = me.json()
        name = u.get("global_name") or u["username"]
        if not same_browser:
            # Phones: the login often starts in an in-app browser and Discord finishes it in the Discord app or the
            # system browser — a different cookie jar, so the nonce isn't here. The nonce exists to stop someone being
            # signed into an account silently; an explicit "continue as <name>" does the same job, so ask instead of failing.
            ticket = signer.dumps({"uid": u["id"], "name": name, "next": safe_next(st.get("next")), "t": time.time(), "k": "confirm"})
            return HTMLResponse(CONFIRM_PAGE.format(name=html.escape(name), ticket=html.escape(ticket, quote=True)))
        resp = RedirectResponse(safe_next(st.get("next")), status_code=303)
        resp.set_cookie(COOKIE, signer.dumps({"uid": u["id"], "name": u.get("global_name") or u["username"], "iat": time.time()}), httponly=True, secure=public_url.startswith("https"), samesite="lax", max_age=SESSION_MAX_AGE_S)
        resp.delete_cookie(NONCE_COOKIE, path="/auth")
        return resp

    @app.post("/auth/confirm")
    async def confirm(request: Request):
        """Second half of a login that finished in a different browser than it started in: the person pressed Continue."""
        from urllib.parse import parse_qs

        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        try:
            t = signer.loads((form.get("ticket") or [""])[0])
        except BadSignature:
            raise HTTPException(400, "bad ticket; start again")
        if t.get("k") != "confirm" or not (0 <= time.time() - float(t.get("t") or 0) <= CONFIRM_TTL_S):
            raise HTTPException(400, "that took too long; start again")
        resp = RedirectResponse(safe_next(t.get("next")), status_code=303)
        resp.set_cookie(COOKIE, signer.dumps({"uid": t["uid"], "name": t["name"], "iat": time.time()}), httponly=True, secure=public_url.startswith("https"), samesite="lax", max_age=SESSION_MAX_AGE_S)
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
