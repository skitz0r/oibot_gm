"""FastAPI app factory: Discord OAuth, icon/emblem/card images, the JSON API (api.py) and the React app at /app.
The old server-rendered pages are gone; their URLs redirect into the app.

Env: OIBOT_WEB_BIND (host:port, unset = web off), OIBOT_WEB_URL (public base, e.g. https://gm.earlyandoften.gg),
DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET (OAuth2), OIBOT_WEB_SECRET (cookie signing),
OIBOT_WEB_SESSION_EPOCH (unix time or ISO datetime: every session issued before it is logged out),
OIBOT_WEB_DEV_USER (a Discord id: skip OAuth on localhost while developing).

Several guilds (design §5.32 "Web"): every request acts on ONE guild, `selected_reg`: an explicit choice (the
`X-Oibot-Guild` header the app sends, or `?g=<key>` on EventSource / images / deep links) is honoured or refused, never
switched; else the `oibot_guild` cookie (a preference, re-checked every request); else the first guild the person can
open in `Guilds.ordered()`. One guild configured = exactly the old single-guild site."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer

from .. import guild_routing, render

HERE = Path(__file__).parent
DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH = "https://discord.com/oauth2/authorize"
COOKIE = "oibot_session"
NONCE_COOKIE = "oibot_oauth"  # short-lived: binds the OAuth round trip to the browser that started it
STATE_TTL_S = 600  # a login must complete within 10 minutes of the redirect
SESSION_MAX_AGE_S = 30 * 86400
GUILD_COOKIE = "oibot_guild"  # the selected guild's key: a preference (unsigned), access re-checked on every request
GUILD_HEADER = "x-oibot-guild"  # an explicit choice: the app sends it once it has rendered a guild (a tab stays on its guild)
GUILD_COOKIE_MAX_AGE_S = 365 * 86400
UNSURE = "Discord didn't answer whether you're in that server; try again in a moment."
UNSURE_RETRY_S = 5
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


class NotMember(HTTPException):
    """A logged-in Discord user who isn't in the guild's server. The API answers 403 JSON as before; an HTML page
    request (/, /app/*) gets the friendly 'not in the Discord yet' page with the Join button instead.
    `requested` = the guild key the request named (None = it named none); `guilds` = the keys this person CAN open
    (the app switches to one of those, saying so, instead of showing the page)."""

    def __init__(self, guild: str | None, requested: str | None = None, guilds: list[str] | None = None):
        detail = (f"Your Discord account isn't a member of the {guild} server." if guild
                  else "Your Discord account isn't a member of any server this site serves.")
        super().__init__(status_code=403, detail=detail)
        self.guild, self.requested, self.guilds = guild, requested, list(guilds or [])


class DiscordUnsure(HTTPException):
    """Several guilds: Discord didn't answer whether this person is in the server the request is about (K4). Never a
    move; the API answers 503 JSON with `retry`, a page request gets a page with Try again (never raw JSON)."""

    def __init__(self):
        super().__init__(status_code=503, detail=UNSURE, headers={"Retry-After": str(UNSURE_RETRY_S)})


def set_guild_cookie(resp, key: str, secure: bool | None = None) -> None:
    """Remember the selected guild for this browser (a year; logout keeps it; path / so every page and image sees it)."""
    if secure is None:
        secure = web_config()[1].startswith("https")
    resp.set_cookie(GUILD_COOKIE, key, max_age=GUILD_COOKIE_MAX_AGE_S, httponly=True, secure=secure, samesite="lax", path="/")


PAGE_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@700&family=Source+Sans+3:wght@400;600&display=swap">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0F1318;color:#EAEFF5;font:16px/1.5 "Source Sans 3",system-ui,sans-serif}
.card{box-sizing:border-box;background:#181E26;border:1px solid #313B48;border-radius:12px;padding:28px 32px;width:calc(100% - 32px);max-width:440px;margin:16px}
.head{display:flex;align-items:center;gap:14px;margin:0 0 10px}.head img{width:56px;height:56px;border-radius:14px;flex:none}
h1{font:700 26px Manrope,sans-serif;margin:0;overflow-wrap:anywhere}p{color:#8C97A8;margin:0 0 16px}p.about{color:#C9D1DC;white-space:pre-line}p.small{font-size:14px;margin:14px 0 0}
.btns{display:flex;flex-wrap:wrap;gap:10px}.btn{display:inline-block;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600}
.join{background:#5865F2;color:#fff}.login{background:#38B2A0;color:#0B1512}.alt{background:transparent;color:#C9D1DC;border:1px solid #313B48}
a.plain{color:#8C97A8}@media(max-width:420px){.card{padding:22px 20px}.btn{flex:1 1 100%;text-align:center}}</style></head><body><div class="card">"""


def join_button(invite_url: str | None) -> str:
    """The Join our Discord button, or nothing (no invite configured = no button, never a broken link)."""
    return f'<a class="btn join" href="{html.escape(invite_url, quote=True)}" rel="noopener">Join our Discord</a>' if invite_url else ""


def head_block(info: dict) -> str:
    icon = f'<img src="{html.escape(info["icon"], quote=True)}" alt="">' if info.get("icon") else ""
    return f'<div class="head">{icon}<h1>{html.escape(info["name"])}</h1></div>'


GENERIC_INFO = {"name": "oibot_GM", "about": None, "invite_url": None, "recruiting": False, "icon": None}  # no guild configured yet

# several guilds: the cards sit in a grid that stacks at phone width (the single-guild page keeps PAGE_HEAD's one card)
PAGE_HEAD_MANY = PAGE_HEAD.replace("</style></head><body><div class=\"card\">",
                                   ".wrap{box-sizing:border-box;width:100%;max-width:960px;padding:16px}.intro{text-align:center;margin:0 0 16px}"
                                   ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),440px));justify-content:center;gap:16px}"
                                   ".cards .card{width:auto;max-width:none;margin:0}</style></head><body><div class=\"wrap\">")


def login_href(next_path: str) -> str:
    """/auth/login with an encoded in-app `next` (a query such as ?g=<key> survives the round trip)."""
    return "/auth/login?next=" + quote(next_path, safe="/")


def landing_card(info: dict) -> str:
    """One guild's card on the several-guild homepage: icon, name, about, its own Join button and its own Log in."""
    about = f'<p class="about">{html.escape(info["about"])}</p>' if info.get("about") else ""
    login_cls = "alt" if info.get("invite_url") else "login"
    nxt = f"/app/me?g={info['key']}" if info.get("key") else "/app/me"
    tail = ("Anyone can join the Discord; new raiders apply there with /apply." if info.get("recruiting") else "Anyone can join the Discord.") if info.get("invite_url") else ""
    return ('<div class="card">' + head_block(info) + about
            + f'<div class="btns">{join_button(info.get("invite_url"))}<a class="btn {login_cls}" href="{html.escape(login_href(nxt), quote=True)}">Log in with Discord</a></div>'
            + (f'<p class="small">{tail}</p>' if tail else "") + "</div>")


def landing_page(infos: list[dict] | dict) -> str:
    """The public homepage. One guild (or none yet): who we are, Join our Discord (when an invite is set), Log in with
    Discord — the page as it always was. Several: the servers side by side, each with its own Join and Log in."""
    if isinstance(infos, dict):
        infos = [infos]
    if len(infos) > 1:
        return (PAGE_HEAD_MANY.replace("{title}", "oibot_GM")
                + '<p class="intro">World of Warcraft raiding guilds. Members keep their characters, availability and raid signups here.</p>'
                + '<div class="cards">' + "".join(landing_card(i) for i in infos) + "</div></div></body></html>")
    info = infos[0] if infos else GENERIC_INFO
    about = f'<p class="about">{html.escape(info["about"])}</p>' if info.get("about") else ""
    login_cls = "alt" if info.get("invite_url") else "login"
    tail = ""
    if info.get("invite_url"):
        tail = "Anyone can join the Discord; new raiders apply there with /apply." if info.get("recruiting") else "Anyone can join the Discord."
    return (PAGE_HEAD.replace("{title}", html.escape(info["name"])) + head_block(info)
            + "<p>A World of Warcraft raiding guild. Members keep their characters, availability and raid signups here.</p>" + about
            + f'<div class="btns">{join_button(info.get("invite_url"))}<a class="btn {login_cls}" href="/auth/login?next=/app/me">Log in with Discord</a></div>'
            + (f'<p class="small">{tail}</p>' if tail else "") + "</div></body></html>")


def not_member_page(infos: list[dict] | dict) -> str:
    """A logged-in Discord user who isn't in the server (several: in none of the servers): how to get in (anyone can
    join), every server's own Join button, and Log out."""
    if isinstance(infos, dict):
        infos = [infos]
    if len(infos) > 1:
        cards = "".join('<div class="card">' + head_block(i) + (f'<p class="about">{html.escape(i["about"])}</p>' if i.get("about") else "")
                        + (f'<div class="btns">{join_button(i.get("invite_url"))}</div>' if i.get("invite_url") else "<p>Ask a member of the guild for an invite.</p>")
                        + "</div>" for i in infos)
        return (PAGE_HEAD_MANY.replace("{title}", "oibot_GM")
                + "<p class=\"intro\">You're not in any of these Discord servers yet. The site is for members of a server. Join one, then come back and log in again.</p>"
                + f'<div class="cards">{cards}</div><p class="small intro"><a class="plain" href="/auth/logout">Log out</a></p></div></body></html>')
    info = infos[0] if infos else GENERIC_INFO
    how = "Join the server, then come back and log in again." if info.get("invite_url") else "Ask a member of the guild for an invite, then come back and log in again."
    return (PAGE_HEAD.replace("{title}", html.escape(info["name"])) + head_block(info)
            + f"<p>You're not in the {html.escape(info['name'])} Discord yet. The site is for members of the server. {how}</p>"
            + f'<div class="btns">{join_button(info.get("invite_url"))}</div>'
            + '<p class="small"><a class="plain" href="/auth/logout">Log out</a></p></div></body></html>')


def unsure_page(retry_href: str) -> str:
    """A page request whose guild lookup Discord didn't answer (K4): say so, Try again (the same page), Log out."""
    return (PAGE_HEAD.replace("{title}", "oibot_GM") + '<div class="head"><h1>Discord didn&#x27;t answer</h1></div>'
            + "<p>We couldn't check whether you're in that Discord server just now. Nothing changed; try again in a moment.</p>"
            + f'<div class="btns"><a class="btn login" href="{html.escape(retry_href, quote=True)}">Try again</a></div>'
            + '<p class="small"><a class="plain" href="/auth/logout">Log out</a></p></div></body></html>')


class Viewer:
    """Who is asking, about which guild (`reg`, the selected one). `moved` = the name of the guild the browser had
    selected that this person can no longer open (the request landed on another; the app says so), else None."""

    def __init__(self, uid: int, name: str, reg, officer: bool, owner: bool, via: str = "web", moved: str | None = None, actor: int | None = None):
        self.uid, self.name, self.reg, self.officer, self.owner, self.via, self.moved = uid, name, reg, officer, owner, via, moved
        self.member = reg.members.get(uid) if reg else None
        self.actor = actor  # MCP with X-Oibot-Actor: the officer the call acts as (D8); None for the owner's token alone and the web


MCP_TOKEN_ENV = "OIBOT_MCP_TOKEN"
ACTOR_HEADER = "x-oibot-actor"  # D8: the officer (a Discord id, the MCP server's OIBOT_MCP_ACTOR) an MCP decision is made by
MCP_LOCAL_ENV = "OIBOT_MCP_LOCAL_ONLY"  # on unless set to 0/false/no/off: the owner token works only from this host
LOOPBACK = frozenset({"127.0.0.1", "::1"})
# set by a proxy in front of the bot (the Cloudflare tunnel adds cf-connecting-ip / cf-ray / x-forwarded-for); a call made
# on this host by the MCP server sends none of them
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-ipcountry", "x-forwarded-for", "x-real-ip", "forwarded", "true-client-ip")
log = logging.getLogger(__name__)


def mcp_local_only() -> bool:
    return (os.environ.get(MCP_LOCAL_ENV) or "1").strip().lower() not in ("0", "false", "no", "off")


def local_request(request) -> bool:
    """Made on this machine and not relayed: a loopback client address (uvicorn rewrites it to the real client for
    proxied requests) and none of the headers a proxy adds — either alone marks a request from outside."""
    host = request.client.host if request.client else ""
    return host in LOOPBACK and not any(request.headers.get(h) for h in PROXY_HEADERS)


def bearer_ok(request, token: str | None) -> bool:
    """`Authorization: Bearer <OIBOT_MCP_TOKEN>` — the MCP server's identity (§5.24). No token configured = bearer auth
    off; the compare is constant-time so a probe learns nothing from timing."""
    if not token:
        return False
    auth = request.headers.get("authorization") or ""
    scheme, _, presented = auth.partition(" ")
    return scheme.lower() == "bearer" and bool(presented.strip()) and secrets.compare_digest(presented.strip(), token)


def mcp_authorized(request, token: str | None) -> bool:
    """The bearer token is right AND (with OIBOT_MCP_LOCAL_ONLY on, the default) the request was made on this host: the
    right token arriving through the tunnel is refused exactly like a wrong one, and logged (never the token) because
    it means the token got out. No guild needed (GET /api/guilds lists them all for the bearer)."""
    if not bearer_ok(request, token):
        return False
    if mcp_local_only() and not local_request(request):
        host = request.client.host if request.client else "?"
        log.warning("refused an MCP bearer request from %s (%s): bearer access is local-only (%s)", host,
                    request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or "no proxy header", MCP_LOCAL_ENV)
        return False
    return True


def guild_list_words(bot) -> str:
    return ", ".join(f"{r.key} ({r.config.name})" for r in bot.registries.ordered()) or "none configured"


def mcp_guild(request, bot):
    """The guild a bearer request acts on (design §5.32 "MCP"): the X-Oibot-Guild header (a key; a unique name only as
    an ASCII fallback — the MCP server always sends the key) → that guild; no header → the only guild; else 400 with
    the list. The bot never reads OIBOT_MCP_GUILD: a forgotten default can never act on the wrong guild."""
    ref = (request.headers.get(GUILD_HEADER) or "").strip()
    guilds = bot.registries
    if ref:
        reg = guilds.get(ref) if hasattr(guilds, "get") else guilds.by_key.get(ref)
        if reg is None:
            raise HTTPException(400, f"No guild {ref!r}. Pass guild= one of: {guild_list_words(bot)}")
        return reg
    reg = guilds.only()
    if reg is None:
        raise HTTPException(400, f"Which guild? Pass guild= one of: {guild_list_words(bot)}")
    return reg


def owner_name(bot, reg) -> str:
    """The guild owner's display name: their member record, else the gateway's user, else 'the owner'. What MCP writes
    are attributed to (`by`), the surface being the `[mcp]` prefix from `via` (design §2.6) — never the word "mcp" as a name."""
    uid = reg.config.owner_discord_id
    m = reg.members.get(uid) if uid is not None else None
    if m is not None:
        return m.display_name
    get_user = getattr(bot, "get_user", None)
    u = get_user(uid) if get_user is not None and uid is not None else None
    name = getattr(u, "display_name", None) or getattr(u, "global_name", None) or getattr(u, "name", None)
    return str(name) if name else "the owner"


def mcp_viewer(request, bot, token: str | None) -> Viewer | None:
    """The bearer identity: the OWNER OF THE CHOSEN GUILD (mcp_guild; every write is attributed to them by their display
    name, `via='mcp'` gives the audit prefix). None = not authorized (mcp_authorized); 400 = no guild chosen, an unknown
    one, or one without an owner."""
    if not mcp_authorized(request, token):
        return None
    reg = mcp_guild(request, bot)
    if not reg.config.owner_discord_id:
        raise HTTPException(400, f"{reg.config.name} has no owner yet")
    return Viewer(int(reg.config.owner_discord_id), owner_name(bot, reg), reg, officer=True, owner=True, via="mcp")


def member_name(bot, reg, uid: int) -> str:
    """A person's display name: their member record, else the gateway's user, else their id."""
    m = reg.members.get(uid)
    if m is not None:
        return m.display_name
    get_user = getattr(bot, "get_user", None)
    u = get_user(uid) if get_user is not None else None
    return str(getattr(u, "display_name", None) or getattr(u, "name", None) or uid)


async def mcp_actor(request, bot, v: Viewer) -> Viewer:
    """D8 / design §2.6: an MCP call carrying X-Oibot-Actor acts as THAT person — validated now (PRIV_LIVE) as an
    officer or the owner of the chosen guild — so a decision about an award or a person is theirs ("<name> via MCP"),
    never "the owner's token". Anyone else (a member, someone outside, a malformed id) is refused with a 403; the
    header never widens what the token may do."""
    raw = (request.headers.get(ACTOR_HEADER) or "").strip()
    if not raw.isdigit():
        raise HTTPException(403, "X-Oibot-Actor must be a Discord user id (OIBOT_MCP_ACTOR).")
    uid = int(raw)
    role = await guild_routing.role_of(bot, v.reg, uid, max_age=guild_routing.PRIV_LIVE)
    if role not in ("owner", "officer"):
        raise HTTPException(403, f"X-Oibot-Actor {uid} is not an officer of {v.reg.config.name} — MCP decisions are made as an officer of the guild.")
    name = member_name(bot, v.reg, uid)
    if name == str(uid):  # no member record, no gateway user: the server member the role check just looked up
        guild = bot.get_guild(v.reg.config.discord_guild_id) if hasattr(bot, "get_guild") else None
        found = await bot.cached_member(guild, uid) if guild is not None and hasattr(bot, "cached_member") else None
        name = str(getattr(found, "display_name", None) or name)
    return Viewer(uid, name, v.reg, officer=True, owner=role == "owner", via="mcp", actor=uid)


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

    async def look(reg, uid: int) -> tuple[bool, str, object | None]:
        """(may open it, membership answer, Discord member): the guild's owner, or in its server now (guild_routing —
        the one definition of "belongs to"; no timeout here, a page may wait for a slow fetch as it always did)."""
        answer, member = await guild_routing.member_of(bot, reg, uid, timeout=None)
        return answer == "yes" or reg.is_owner_id(uid), answer, member

    async def accessible_keys(uid: int, skip=None) -> list[str]:
        regs = [r for r in bot.registries.ordered() if r is not skip]
        looks = await asyncio.gather(*(look(r, uid) for r in regs))
        return [r.key for r, (ok, _, _) in zip(regs, looks) if ok]

    def requested(request: Request, guild: str | None, query: bool) -> str | None:
        if guild:
            return guild
        h = (request.headers.get(GUILD_HEADER) or "").strip()
        if h:
            return h
        return (request.query_params.get("g") or "").strip() or None if query else None

    async def selected_reg(request: Request, uid: int, guild: str | None = None, query: bool = True):
        """(reg, Discord member or None, moved-from name or None) for this request (design §5.32 "Web"):
        1. an explicit choice (`guild`, the X-Oibot-Guild header, or ?g= when `query`) — honoured or refused with
           NotMember naming what the person CAN open; a request that named its guild is never switched silently;
        2. the oibot_guild cookie, when this person may open it (otherwise fall through, moved-from = its name);
        3. the first guild they may open in Guilds.ordered() — the only "first" in the web, over their own list.
        Several guilds: a lookup Discord didn't answer ('unknown') on 1. or 2. is a 503, never a move (K4). One guild:
        as before (not in the server = NotMember)."""
        regs = bot.registries.ordered()
        many = len(regs) > 1
        by_key = {r.key: r for r in regs}
        want = requested(request, guild, query)
        if want is not None:
            reg = by_key.get(want)
            if reg is not None:
                ok, answer, member = await look(reg, uid)
                if ok:
                    return reg, member, None
                if answer == "unknown" and many:
                    raise DiscordUnsure()
            raise NotMember(reg.config.name if reg else None, requested=want, guilds=await accessible_keys(uid, skip=reg))
        moved, tried = None, None
        ck = (request.cookies.get(GUILD_COOKIE) or "").strip()
        if ck:
            tried = by_key.get(ck)
            if tried is not None:
                ok, answer, member = await look(tried, uid)
                if ok:
                    return tried, member, None
                if answer == "unknown" and many:
                    raise DiscordUnsure()
                moved = tried.config.name
            elif many:
                moved = ck  # a guild that isn't served any more
        rest = [r for r in regs if r is not tried]
        looks = await asyncio.gather(*(look(r, uid) for r in rest))
        for r, (ok, _, member) in zip(rest, looks):
            if ok:
                return r, member, moved
        if many and any(answer == "unknown" for _, answer, _ in looks):
            raise DiscordUnsure()
        raise NotMember(regs[0].config.name if not many else None)

    async def viewer(request: Request, guild: str | None = None, query: bool = True) -> Viewer | None:
        """None = not logged in (or no guild configured). Raises 403 (NotMember) for a Discord user who may not open
        the selected guild: the server's membership and roles are the whitelist — there is no separate user list.
        `guild` = an explicit choice (POST /api/guild); `query=False` ignores ?g= (a deep link to someone else's server)."""
        if request.headers.get("authorization"):  # the MCP server: bearer token → the owner; a wrong token is nobody
            v = mcp_viewer(request, bot, os.environ.get(MCP_TOKEN_ENV) or None)
            if v is not None and request.headers.get(ACTOR_HEADER):
                v = await mcp_actor(request, bot, v)  # D8: the named officer, validated now
            return v
        s = session(request)
        if not s and dev_user and request.client and request.client.host in ("127.0.0.1", "::1"):
            s = {"uid": int(dev_user), "name": "dev"}
        if not s:
            return None
        uid = int(s["uid"])
        if not bot.registries.ordered():
            return None
        reg, member, moved = await selected_reg(request, uid, guild, query)
        guild_obj = bot.get_guild(reg.config.discord_guild_id)
        officer = bot.officiates(member, guild_obj) if member else True  # per guild: owning B grants nothing in A
        owner = reg.is_owner_id(uid)
        name = member.display_name if member else (reg.members[uid].display_name if uid in reg.members else s.get("name", str(uid)))
        return Viewer(uid, name, reg, officer, owner, moved=moved)

    async def guild_choices(v: Viewer) -> list[dict]:
        """The selector's list: every guild this person may open, in ordered() order, with their role there
        (owner | officer | member, from that server's roles). Only /api/meta pays for the other guilds' lookups."""
        from .api import public_info

        regs = bot.registries.ordered()
        others = [r for r in regs if r is not v.reg]
        looks = dict(zip((r.key for r in others), await asyncio.gather(*(look(r, v.uid) for r in others))))
        out = []
        for r in regs:
            if r is v.reg:
                role = "owner" if v.owner else "officer" if v.officer else "member"
            else:
                ok, _, member = looks[r.key]
                if not ok:
                    continue
                role = "owner" if r.is_owner_id(v.uid) else "officer" if member is not None and bot.officiates(member, bot.get_guild(r.config.discord_guild_id)) else "member"
            out.append({"key": r.key, "name": r.config.name, "icon": public_info(bot, r)["icon"], "role": role})
        return out

    async def privilege(reg, uid: int, max_age: float = guild_routing.PRIV_WEB) -> str:
        """owner | officer | member | outside — from Discord roles, never stored: guild_routing.role_of (design §2.6)."""
        return await guild_routing.role_of(bot, reg, uid, max_age=max_age)

    async def live_officer(v: Viewer) -> bool:
        """`v.officer` decided NOW (PRIV_LIVE) for a write that needs officer or owner rights — the plain-text apply
        route calls it before `authorize`, as ConfigConfirmView's Apply does. The bearer (MCP, the owner) and the owner
        need no lookup; with the guild's server itself unknown to the gateway (the bot disconnected, or no Discord at
        all) the login-time answer stands. A member Discord can't confirm right now is no officer for this write (§2.9:
        reads continue on the cache, officer writes don't)."""
        if v.via == "mcp" or v.owner or v.reg is None or bot.get_guild(v.reg.config.discord_guild_id) is None:
            return v.officer
        r = await guild_routing.resolve_role(bot, v.reg, v.uid, max_age=guild_routing.PRIV_LIVE)
        v.officer = r.officer
        return v.officer

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
    async def raid_thumb(request: Request, rid: str, g: str | None = None):
        """The raid emblem with that guild's size / lockout / name: ?g=<key> (Discord fetches without cookies, so every
        caller names its guild), else the cookie, else the only guild; several guilds and none named → the game
        profile's defaults (never "the first guild", K15)."""
        guilds = bot.registries
        reg = guilds.by_key.get(g or "") or guilds.by_key.get(request.cookies.get(GUILD_COOKIE) or "") or guilds.only()
        if reg is not None:
            if rid not in reg.profile.raids:
                raise HTTPException(404)
            rd, prof = reg.raid_def(rid), reg.profile.name
        else:
            profile = next((r.profile for r in guilds.ordered() if rid in r.profile.raids), None)
            if profile is None:
                raise HTTPException(404)
            rd, prof = dict(profile.raids[rid] or {}), profile.name
        key = f"{prof}:{rid}:{rd.get('name', rid)}:{rd.get('size')}:{rd.get('lockout_days')}"
        if key not in thumb_cache:
            thumb_cache[key] = await asyncio.to_thread(render.raid_thumb_png, rid, rd.get("name", rid), int(rd.get("size") or 0), int(rd.get("lockout_days") or 7))
        return Response(thumb_cache[key], media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

    badge_cache: dict[str, bytes] = {}
    ICON_DIR = Path(__file__).resolve().parents[3] / "out" / "icons"
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    ICON_CDN = "https://render.worldofwarcraft.com/us/icons/56/{name}.jpg"

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
        """Liveness for launchd tooling (menubar, the apply job): the bot user, the guild count, and per guild whether
        the bot sees its Discord server. `guilds_failed` = guild folders that didn't load (their names are in the log)."""
        get_guild = getattr(bot, "get_guild", None)
        regs = bot.registries.ordered() if hasattr(bot.registries, "ordered") else list(bot.registries.by_discord.values())
        per = {r.key: {"server": bool(get_guild and get_guild(r.config.discord_guild_id) is not None)} for r in regs}
        return {"ok": True, "bot": str(bot.user) if bot.user else None, "guilds": len(regs), "per_guild": per,
                "guilds_failed": len(getattr(bot.registries, "load_errors", None) or [])}

    from .api import public_card

    def guild_infos() -> list[dict]:
        """Every configured guild's public facts (public_info + key), in Guilds.ordered() order."""
        return [public_card(bot, r) for r in bot.registries.ordered()]

    @app.exception_handler(NotMember)
    async def _not_member(request: Request, exc: NotMember):
        """Wins over the generic HTTPException handler (api.py) by class. The API keeps its 403 JSON (the SPA then
        switches to a guild in `guilds`, or shows the not-a-member screen from /api/public when there is none); a page
        request gets the friendly page with every server's Join button."""
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": exc.detail, "not_member": True, "guild": exc.requested, "guilds": exc.guilds}, status_code=403)
        return HTMLResponse(not_member_page(guild_infos()), status_code=403)

    @app.exception_handler(DiscordUnsure)
    async def _unsure(request: Request, exc: DiscordUnsure):
        """K4 503: the API keeps its JSON (with `retry`, the SPA offers Try again); a page request (/, /app/*) gets a
        page with Try again instead of raw JSON."""
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": exc.detail, "retry": True}, status_code=503, headers=exc.headers)
        if request.url.path == "/":
            g = (request.query_params.get("g") or "").strip()
            retry = "/?" + urlencode({"g": g}) if g else "/"
        else:
            retry = safe_next(request.url.path + (f"?{request.url.query}" if request.url.query else ""))  # in-app paths only
        return HTMLResponse(unsure_page(retry), status_code=503, headers=exc.headers)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        g = (request.query_params.get("g") or "").strip()
        try:
            v = await viewer(request)
        except NotMember as e:
            if not (g and e.guilds):
                raise
            # a link to a server this person isn't in, and they are in another: into the app, which opens one of
            # theirs and says so (spa() and the K16 notice) — never the "not in any of these servers" page
            return RedirectResponse("/app/me?" + urlencode({"g": g}), status_code=302)
        if v is None:
            return HTMLResponse(landing_page(guild_infos()))
        return RedirectResponse("/app/me?" + urlencode({"g": g}) if g else "/app/me", status_code=302)

    for _old, _new in (("/admin", "/app/members"), ("/admin/build", "/app/rosters"), ("/bank", "/app/members"), ("/rosters", "/app/rosters"), ("/raids", "/app/raids"), ("/config", "/app/config"), ("/ops", "/app/ops")):
        app.add_api_route(_old, (lambda new: (lambda: RedirectResponse(new, status_code=301)))(_new), methods=["GET"])

    from .api import install_api

    install_api(app, bot, viewer=viewer, privilege=privilege, guild_choices=guild_choices, live_officer=live_officer)
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
