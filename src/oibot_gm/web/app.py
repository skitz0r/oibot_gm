"""FastAPI app factory. Read-mostly in this first cut: everything an officer would otherwise dig out of
slash commands, on one site. Edits will come through the same code paths as the commands.

Env: OIBOT_WEB_BIND (host:port, unset = web off), OIBOT_WEB_URL (public base, e.g. https://gm.earlyandoften.gg),
DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET (OAuth2), OIBOT_WEB_SECRET (cookie signing),
OIBOT_WEB_DEV_USER (a Discord id: skip OAuth on localhost while developing)."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from .. import comp as comp_mod, raidcycle as rc, render
from ..registry import bank_rows, pool_health_data

HERE = Path(__file__).parent
DISCORD_API = "https://discord.com/api/v10"
DISCORD_OAUTH = "https://discord.com/oauth2/authorize"
COOKIE = "oibot_session"


def web_config() -> tuple[str | None, str]:
    """(bind, public url); bind None disables the dashboard."""
    return os.environ.get("OIBOT_WEB_BIND") or None, os.environ.get("OIBOT_WEB_URL", "http://127.0.0.1:8788")


class Viewer:
    def __init__(self, uid: int, name: str, reg, officer: bool, owner: bool):
        self.uid, self.name, self.reg, self.officer, self.owner = uid, name, reg, officer, owner
        self.member = reg.members.get(uid) if reg else None


def create_app(bot) -> FastAPI:
    app = FastAPI(title="oibot_GM", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    signer = URLSafeSerializer(os.environ.get("OIBOT_WEB_SECRET") or secrets.token_hex(32), salt="session")
    _, public_url = web_config()
    client_id, client_secret = os.environ.get("DISCORD_CLIENT_ID"), os.environ.get("DISCORD_CLIENT_SECRET")
    dev_user = os.environ.get("OIBOT_WEB_DEV_USER")
    card_cache: dict[str, tuple[float, str, bytes]] = {}  # key -> (time, data head, png)

    # ---- identity
    def session(request: Request) -> dict | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            return signer.loads(raw)
        except BadSignature:
            return None

    def viewer(request: Request) -> Viewer | None:
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
        member = guild.get_member(uid) if guild else None
        officer = bot.officiates(member, guild) if member else (reg.config.owner_discord_id == uid)
        owner = reg.config.owner_discord_id == uid
        name = member.display_name if member else (reg.members[uid].display_name if uid in reg.members else s.get("name", str(uid)))
        return Viewer(uid, name, reg, officer, owner)

    def need(request: Request, officer: bool = False) -> Viewer:
        v = viewer(request)
        if v is None:
            raise HTTPException(status_code=307, headers={"Location": "/auth/login?next=" + request.url.path})
        if officer and not v.officer:
            raise HTTPException(status_code=403, detail="Officers only.")
        return v

    def page(request: Request, name: str, v: Viewer | None, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"v": v, "now": datetime.now().strftime("%a %d %b %H:%M"), **ctx})

    # ---- auth
    @app.get("/auth/login")
    async def login(request: Request, next: str = "/"):
        if not client_id:
            return HTMLResponse("<p>Login isn't configured yet (DISCORD_CLIENT_ID missing).</p>", status_code=503)
        state = signer.dumps({"next": next, "t": time.time()})
        q = urlencode({"client_id": client_id, "redirect_uri": f"{public_url}/auth/callback", "response_type": "code", "scope": "identify", "state": state, "prompt": "none"})
        return RedirectResponse(f"{DISCORD_OAUTH}?{q}")

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        try:
            st = signer.loads(state)
        except BadSignature:
            raise HTTPException(400, "bad state")
        async with httpx.AsyncClient(timeout=15) as hc:
            tok = await hc.post(f"{DISCORD_API}/oauth2/token", data={"client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": f"{public_url}/auth/callback"})
            if tok.status_code != 200:
                raise HTTPException(400, "Discord refused the login")
            me = await hc.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        u = me.json()
        resp = RedirectResponse(st.get("next") or "/", status_code=303)
        resp.set_cookie(COOKIE, signer.dumps({"uid": u["id"], "name": u.get("global_name") or u["username"]}), httponly=True, secure=public_url.startswith("https"), samesite="lax", max_age=30 * 86400)
        return resp

    @app.get("/auth/logout")
    async def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "bot": str(bot.user) if bot.user else None, "guilds": len(bot.registries.by_discord)}

    # ---- pages
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        v = viewer(request)
        if v is None:
            reg = next(iter(bot.registries.by_discord.values()), None)
            return page(request, "landing.html", None, guild=reg.config if reg else None)
        reg = v.reg
        rs = bot.raids.store(reg)
        mine = []
        for ev in rs.live():
            s = ev.signups.get(str(v.uid))
            asks = [a for a in ev.fill_asks if a.discord_id == v.uid]
            mine.append({"ev": ev, "signup": s, "asks": asks, "team": reg.config.team(ev.team) or {"key": ev.team, "size": 20}})
        roles = reg.roles_of(v.member) if v.member else (None, [])
        return page(request, "home.html", v, member=v.member, roles=roles, raids=mine, today=datetime.now().date().isoformat())

    @app.get("/bank", response_class=HTMLResponse)
    async def bank(request: Request):
        v = need(request, officer=True)
        rows = bank_rows(v.reg)
        return page(request, "bank.html", v, rows=rows, members=v.reg.members, verification=v.reg.verification)

    @app.get("/rosters", response_class=HTMLResponse)
    async def rosters(request: Request):
        v = need(request, officer=True)
        reg = v.reg
        out = []
        for t in reg.config.rosters or [{"key": "main", "name": "main", "size": 20}]:
            h = pool_health_data(reg, t)
            members = reg.roster_members(t["key"])
            ic = comp_mod.ideal_comp(reg.profile, int(t.get("size") or 20), comp_mod.pool_players(reg), t.get("comp_targets") or {}, t.get("instance"))
            out.append({"t": t, "health": h, "members": members, "comp": ic})
        return page(request, "rosters.html", v, rosters=out)

    @app.get("/raids", response_class=HTMLResponse)
    async def raids(request: Request):
        v = need(request, officer=True)
        reg = v.reg
        rs = bot.raids.store(reg)
        out = []
        for ev in sorted(rs.events.values(), key=lambda e: e.starts_at, reverse=True)[:12]:
            t = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
            nd = rc.needs(reg, ev, t) if ev.state not in ("done", "cancelled") else None
            out.append({"ev": ev, "team": t, "needs": nd, "busy": rc.conflicts(rs, ev) if nd else {}, "by": {s: ev.by_status(s) for s in rc.STATUSES}})
        return page(request, "raids.html", v, raids=out)

    @app.get("/config", response_class=HTMLResponse)
    async def config(request: Request):
        v = need(request, officer=True)
        import yaml

        from ..policy import PolicyStore

        ps = PolicyStore(bot.registries.store, v.reg.key)
        docs = {d: {"text": ps.read(d), "compiled": ps.compiled(d)} for d in ("loot", "comp", "persona")}
        return page(request, "config.html", v, yaml=yaml.safe_dump(v.reg.config.model_dump(), sort_keys=False), docs=docs)

    @app.get("/ops", response_class=HTMLResponse)
    async def ops(request: Request):
        v = need(request, officer=True)
        st = bot.registries.store
        prov = bot.ctx.provider
        feed = getattr(bot, "feed", None)
        rows = list(reversed(bot.ops.recent))
        precedents = st.read_jsonl(Path(v.reg.key) / "precedents.jsonl")[-50:]
        ledger = st.read_jsonl(Path(v.reg.key) / "ledger.jsonl")[-100:]
        return page(request, "ops.html", v, rows=rows, head=st.head(), push=st.push_enabled, llm=prov.summary() if prov else "off",
                    feed=feed.status() if feed else "disabled", precedents=list(reversed(precedents)), ledger=list(reversed(ledger)), up=int(time.time() - bot.started_at) if hasattr(bot, "started_at") else 0)

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
        v = need(request, officer=True)
        reg = v.reg

        def build():
            rows = bank_rows(reg)
            return render.bank_png(f"Character bank · {reg.config.name}", f"{len(rows)} members", rows)

        return Response(await cached_png("bank", build), media_type="image/png")

    @app.get("/card/{kind}/{key}.png")
    async def card(request: Request, kind: str, key: str):
        v = need(request, officer=True)
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
                ic = comp_mod.ideal_comp(reg.profile, int(t.get("size") or 20), players, t.get("comp_targets") or {}, t.get("instance"))
                return render.comp_png(ic.lines, f"Desired comp · {t.get('name', key)} ({ic.size}-man, {ic.groups} groups)", "derived from the buff matrix and comp rules", ic.notes)
            if kind == "health":
                rs = bot.raids.store(reg)
                ev = rs.events.get(key)
                if not ev:
                    raise HTTPException(404)
                tt = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
                h = rc.health_data(reg, ev, tt)
                return render.health_png(f"Roster health · {tt.get('name', ev.team)} · {ev.key}", ev.start.strftime("%a %b %d %H:%M"), h["headcount"], h["roles"], h["buffs"], h["unresponsive"])
            raise HTTPException(404)

        return Response(await cached_png(f"{kind}:{key}", build), media_type="image/png")

    return app


async def serve(bot) -> None:
    """Run uvicorn inside the bot's event loop when OIBOT_WEB_BIND is set."""
    bind, url = web_config()
    if not bind:
        print("web dashboard disabled (set OIBOT_WEB_BIND, e.g. 127.0.0.1:8788)")
        return
    import uvicorn

    host, _, port = bind.rpartition(":")
    config = uvicorn.Config(create_app(bot), host=host or "127.0.0.1", port=int(port), log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
    server = uvicorn.Server(config)
    print(f"web dashboard on http://{bind} (public {url})")
    await server.serve()
