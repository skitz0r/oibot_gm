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

    member_cache: dict[int, tuple[float, object]] = {}

    async def guild_member(guild, uid: int):
        """Live guild member (roles decide the tier). No members intent, so fall back to a REST fetch, cached 5 min."""
        m = guild.get_member(uid)
        if m is not None:
            return m
        hit = member_cache.get(uid)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        try:
            m = await guild.fetch_member(uid)
        except Exception:  # noqa: BLE001 — not a member (404) or transient
            m = None
        member_cache[uid] = (time.time(), m)
        return m

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

    async def need(request: Request, officer: bool = False) -> Viewer:
        v = await viewer(request)
        if v is None:
            raise HTTPException(status_code=307, headers={"Location": "/auth/login?next=" + request.url.path})
        if officer and not v.officer:
            raise HTTPException(status_code=403, detail="Officers only.")
        return v

    def csrf_token(v: Viewer) -> str:
        return signer.dumps({"csrf": v.uid})

    def page(request: Request, name: str, v: Viewer | None, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"v": v, "now": datetime.now().strftime("%a %d %b %H:%M"), "csrf": csrf_token(v) if v else "",
                                                          "ok": request.query_params.get("ok"), "err": request.query_params.get("err"), **ctx})

    async def form(request: Request, officer: bool = False) -> tuple[Viewer, dict]:
        """Viewer + POSTed fields, after the CSRF check (token is bound to the session's user id)."""
        v = await need(request, officer=officer)
        data = dict(await request.form())
        try:
            tok = signer.loads(data.get("csrf", ""))
        except BadSignature:
            tok = {}
        if tok.get("csrf") != v.uid:
            raise HTTPException(403, "Form expired — reload the page and try again.")
        return v, {k: (str(val).strip() if val is not None else "") for k, val in data.items()}

    def back(to: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
        q = urlencode({"ok": ok} if ok else {"err": err} if err else {})
        return RedirectResponse(f"{to}?{q}" if q else to, status_code=303)

    async def mutate(request: Request, to: str, fn, officer: bool = False):
        """Run a registry mutation from a form: same functions and validation as the Discord commands."""
        from ..registry import RegistryError

        v, d = await form(request, officer=officer)
        try:
            msg = await asyncio.to_thread(fn, v, d)
        except (RegistryError, ValueError) as e:
            return back(to, err=str(e))
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {msg}")
        return back(to, ok=msg)

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

    # ---- pages
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        v = await viewer(request)
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
        classes = {c: {s: a.get("role") for s, a in specs.items()} for c, specs in reg.profile.classes.items()}
        return page(request, "home.html", v, member=v.member, roles=roles, raids=mine, today=datetime.now().date().isoformat(), classes=classes,
                    rosters=reg.config.rosters or [{"key": "main", "name": "main"}], spec_role=lambda c: reg.profile.spec(c.cls, c.spec).role, off_role=lambda c: (reg.profile.spec(c.cls, c.offspec).role if c.offspec else None),
                    week=(v.member.week if v.member else []), tz=reg.config.timezone, placement_asks=reg.open_placement_asks(v.uid),
                    raid_windows=[{"slot": t["schedule"], "name": t.get("name", t["key"])} for t in reg.config.rosters if t.get("schedule")])

    # ---- member self-service (POST → Registry, exactly what the Discord buttons call)
    @app.post("/me/character/add")
    async def me_add(request: Request):
        def go(v, d):
            slot = "main" if d.get("slot") == "main" else "alt"
            if d.get("name"):
                m, c = v.reg.add_character(v.uid, v.name, d["name"], d["cls"], d["spec"], d.get("offspec") or None, slot == "main")
                return f"registered {c.label} ({c.cls} {c.spec}, {'main' if c.is_main else 'alt'}) — an officer will confirm it"
            m, c = v.reg.set_plan(v.uid, v.name, d["cls"], d["spec"], d.get("offspec") or None, slot)
            return f"planned {slot}: {c.cls} {c.spec}" + (f"/{c.offspec}" if c.offspec else "")
        return await mutate(request, "/", go)

    @app.post("/me/character/spec")
    async def me_spec(request: Request):
        def go(v, d):
            c = v.reg.set_spec(v.uid, d["label"], d["spec"], d.get("offspec") or None)
            return f"{c.label}: {c.spec}" + (f"/{c.offspec}" if c.offspec else "")
        return await mutate(request, "/", go)

    @app.post("/me/character/main")
    async def me_main(request: Request):
        return await mutate(request, "/", lambda v, d: f"main is now {v.reg.set_main(v.uid, d['label'])[1].label}")

    @app.post("/me/character/retire")
    async def me_retire(request: Request):
        return await mutate(request, "/", lambda v, d: f"retired {v.reg.retire(v.uid, d['label']).label}")

    @app.post("/me/character/name")
    async def me_name(request: Request):
        return await mutate(request, "/", lambda v, d: f"named your {d.get('slot', 'main')} {v.reg.name_character(v.uid, d['name'], d.get('slot') or 'main')[1].label} — pending confirmation")

    @app.post("/me/character/flex")
    async def me_flex(request: Request):
        def go(v, d):
            roles = [r for r in ("tank", "healer", "melee", "ranged") if d.get(f"flex_{r}")]
            c = v.reg.set_flex(v.uid, d["label"], roles)
            return f"{c.label}: flex " + (", ".join(c.flex) or "none")
        return await mutate(request, "/", go)

    @app.post("/me/availability")
    async def me_availability(request: Request):
        def go(v, d):
            for t in v.reg.config.team_keys():
                val = d.get(f"avail_{t}")
                cur = v.reg.members[v.uid].availability.get(t) if v.uid in v.reg.members else None
                if val in ("in", "out", "sub") and val != cur:
                    v.reg.set_availability(v.uid, t, val)
            return "availability saved"
        return await mutate(request, "/", go)

    @app.post("/me/absence/add")
    async def me_absence_add(request: Request):
        return await mutate(request, "/", lambda v, d: f"absent {v.reg.add_absence(v.uid, d['start'], d.get('end') or None, d.get('reason') or None, v.name, v.name)[1].start}")

    @app.post("/me/absence/clear")
    async def me_absence_clear(request: Request):
        return await mutate(request, "/", lambda v, d: (v.reg.clear_absence(v.uid, d["start"]) and f"cleared absence {d['start']}"))

    @app.post("/me/placement")
    async def me_placement(request: Request):
        def go(v, d):
            line = v.reg.answer_placement(v.uid, d["roster"], d.get("answer") == "yes", v.name)
            bot.loop.create_task(bot.after_placement_answer(v.reg, v.uid, d["roster"], d.get("answer") == "yes", line))
            return line
        return await mutate(request, "/", go)

    @app.post("/me/week")
    async def me_week(request: Request):
        import json

        def go(v, d):
            try:
                ranges = json.loads(d.get("week") or "[]")
            except ValueError:
                raise ValueError("couldn't read the grid")
            m = v.reg.set_week(v.uid, ranges, v.name)
            hours = sum((r["end"] - r["start"]) for r in m.week) / 60
            return f"availability saved: {hours:.0f}h/week across {len(m.week)} block(s)"
        return await mutate(request, "/", go)

    @app.post("/me/slots")
    async def me_slots(request: Request):
        def go(v, d):
            prefs = {slot: d.get(f"slot_{i}") for i, slot in enumerate(v.reg.config.slots)}
            v.reg.set_slot_prefs(v.uid, {k: val for k, val in prefs.items() if val}, v.name)
            return "raid-time preferences saved"
        return await mutate(request, "/", go)

    @app.post("/admin/slots")
    async def admin_slots(request: Request):
        return await mutate(request, "/admin", lambda v, d: _cfg_op(v, op="set", path="slots", value=d.get("value", "")), officer=True)

    @app.post("/me/dm")
    async def me_dm(request: Request):
        def go(v, d):
            m = v.reg.member(v.uid)
            m.dm_opt_out = d.get("dm") != "on"
            v.reg.save(m, f"{m.display_name} DMs {'off' if m.dm_opt_out else 'on'}")
            return f"DMs {'off' if m.dm_opt_out else 'on'}"
        return await mutate(request, "/", go)

    # ---- officer roster admin (placements, ranks, confirmations, roster settings, comp ideals)
    @app.get("/admin", response_class=HTMLResponse)
    async def admin(request: Request):
        v = await need(request, officer=True)
        reg = v.reg
        rosters = reg.config.rosters or []
        rows = []
        for m in sorted(reg.members.values(), key=lambda m: m.display_name.lower()):
            chars = m.active()
            if not chars:
                continue
            placed = {t["key"]: next((c for c in chars if t["key"] in c.rosters), None) for t in rosters}
            rows.append({"m": m, "chars": chars, "main": m.main, "placed": placed, "roles": reg.roles_of(m), "verification": reg.verification(m.discord_id)})
        instances = list(reg.profile.raids)
        return page(request, "admin.html", v, rows=rows, rosters=rosters, ranks=("trial", "raider", "core", "alt", "social"), instances=instances, owner=v.owner,
                    slots=reg.config.slots, heat=reg.slot_summary(), week_heat=reg.week_heat(), tz=reg.config.timezone,
                    grid_members=sum(1 for m in reg.members.values() if m.main and m.week))

    def _cfg_op(v: Viewer, **kw):
        from .. import configops

        op = configops.ConfigOp(**kw)
        return configops.apply(v.reg, op, v.name, v.owner, None)

    @app.post("/admin/place")
    async def admin_place(request: Request):
        def go(v, d):
            uid, key = int(d["uid"]), d["roster"]
            if d.get("action") == "remove":
                m = v.reg.roster_remove(uid, key, v.name)
                return f"{m.display_name} removed from {key}"
            m, c = v.reg.roster_add(uid, key, v.name, d.get("character") or None)
            return f"{m.display_name} ({c.label}) → {key}"
        return await mutate(request, "/admin", go, officer=True)

    @app.post("/admin/place-all")
    async def admin_place_all(request: Request):
        def go(v, d):
            key, n = d["roster"], 0
            for m in list(v.reg.members.values()):
                if m.main and not v.reg.on_roster(m, key):
                    v.reg.roster_add(m.discord_id, key, v.name)
                    n += 1
            return f"placed {n} main(s) on {key}"
        return await mutate(request, "/admin", go, officer=True)

    @app.post("/admin/rank")
    async def admin_rank(request: Request):
        return await mutate(request, "/admin", lambda v, d: f"{d['label']} → {v.reg.set_rank(d['label'], d['rank'], v.name)[1].rank}", officer=True)

    @app.post("/admin/confirm")
    async def admin_confirm(request: Request):
        return await mutate(request, "/admin", lambda v, d: f"confirmed {v.reg.confirm(d['label'], v.name)[1].label}", officer=True)

    @app.post("/admin/roster/settings")
    async def admin_roster_settings(request: Request):
        def go(v, d):
            key = d["key"]
            done = []
            if not v.reg.config.roster(key):
                _cfg_op(v, op="team_add", team=key)
                done.append("created")
            for f in ("size", "schedule", "instance", "cutoff_soft_hours", "cutoff_hard_hours", "open_days_before", "name"):
                if f in d and str(d[f]) != str((v.reg.config.roster(key) or {}).get(f, "")):
                    if f == "instance" and not d[f]:
                        continue
                    _cfg_op(v, op="team_set", team=key, field=f, value=d[f])
                    done.append(f)
            for f in ("open_dm", "autofill"):
                want = "true" if d.get(f) == "on" else "false"
                cur = (v.reg.config.roster(key) or {}).get(f, f == "autofill")
                if (want == "true") != bool(cur):
                    _cfg_op(v, op="team_set", team=key, field=f, value=want)
                    done.append(f)
            return f"roster {key}: " + (", ".join(done) or "no changes")
        return await mutate(request, "/admin", go, officer=True)

    @app.post("/admin/roster/remove")
    async def admin_roster_remove(request: Request):
        return await mutate(request, "/admin", lambda v, d: _cfg_op(v, op="team_remove", team=d["key"]), officer=True)

    @app.post("/admin/raid")
    async def admin_raid(request: Request):
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            inst = d["instance"]
            done = []
            cur = v.reg.raid_def(inst)
            for f in ("lockout_days", "duration_hours", "notes"):
                val = d.get(f, "")
                if val != "" and str(val) != str(cur.get(f, "")):
                    done.append(v.reg.set_raid_override(inst, f, val, v.name))
            fo = d.get("first_open", "")
            cur_fo = v.reg.first_open(inst)
            if fo and (cur_fo is None or fo[:16] != cur_fo.astimezone(__import__("zoneinfo").ZoneInfo(v.reg.config.timezone)).strftime("%Y-%m-%dT%H:%M")):
                done.append(v.reg.set_raid_override(inst, "first_open", fo, v.name))
            want_auto = d.get("auto") == "on"
            if want_auto != bool(cur.get("auto")):
                done.append(v.reg.set_raid_override(inst, "auto", "true" if want_auto else "false", v.name))
            for role in ("tank", "healer", "dps"):
                for bound in ("min", "max"):
                    val = d.get(f"{role}_{bound}", "")
                    if val != "" and str(val) != str(((cur.get("comp") or {}).get(role) or {}).get(bound, "")):
                        done.append(v.reg.set_raid_override(inst, f"{role}_{bound}", val, v.name))
            return "; ".join(done) or f"{inst}: no changes"
        return await mutate(request, "/raids", go, officer=True)

    @app.post("/admin/raid/reset")
    async def admin_raid_reset(request: Request):
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            return v.reg.clear_raid_override(d["instance"], v.name)
        return await mutate(request, "/raids", go, officer=True)

    @app.post("/admin/comp/target")
    async def admin_comp_target(request: Request):
        return await mutate(request, "/admin", lambda v, d: _cfg_op(v, op="comp_target", team=d["key"], field=d["slot"], value=d["value"], reason=d.get("reason") or None), officer=True)

    @app.post("/admin/comp/target/clear")
    async def admin_comp_target_clear(request: Request):
        return await mutate(request, "/admin", lambda v, d: _cfg_op(v, op="comp_target_clear", team=d["key"], field=d["slot"]), officer=True)

    @app.post("/admin/comp/groups")
    async def admin_comp_groups(request: Request):
        return await mutate(request, "/admin", lambda v, d: _cfg_op(v, op="comp_groups", team=d["key"], value=d.get("value", "")), officer=True)

    # ---- roster builder: propose every roster for the coming lockout window, approve → placements
    build_cache: dict[str, tuple[float, object]] = {}

    @app.get("/admin/build", response_class=HTMLResponse)
    async def admin_build(request: Request):
        from ..roster import builder

        v = await need(request, officer=True)
        reg = v.reg
        shells = builder.shells_from_config(reg)
        if not shells:
            return page(request, "build.html", v, result=None, shells=[], token="", adds=[], removes=[], names={})
        result = await asyncio.to_thread(builder.build, reg, bot.raids.store(reg), shells)
        token = secrets.token_hex(8)
        build_cache[token] = (time.time(), result)
        for k in [k for k, (t, _) in build_cache.items() if time.time() - t > 1800]:
            del build_cache[k]
        adds, removes = builder.diff_placements(reg, result)
        names = {m.discord_id: m.display_name for m in reg.members.values()}
        return page(request, "build.html", v, result=result, shells=shells, token=token, adds=adds, removes=removes, names=names)

    @app.post("/admin/build/apply")
    async def admin_build_apply(request: Request):
        from ..roster import builder

        from ..registry import RegistryError

        v, d = await form(request, officer=True)
        hit = build_cache.pop(d.get("token", ""), None)
        if not hit:
            return back("/admin", err="That proposal expired — build again.")
        try:
            adds, _removes = builder.diff_placements(v.reg, hit[1])
            done = await asyncio.to_thread(builder.apply, v.reg, hit[1], v.name)
        except (RegistryError, ValueError) as e:
            return back("/admin", err=str(e))
        sent = await bot.send_placement_asks(v.reg, adds, v.name)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name} approved a roster build: {len(done)} change(s), {sent} confirmation DM(s)")
        return back("/admin", ok=f"applied {len(done)} placement change(s); asked {sent} member(s) to confirm by DM")

    @app.get("/bank", response_class=HTMLResponse)
    async def bank(request: Request):
        v = await need(request, officer=True)
        rows = bank_rows(v.reg)
        return page(request, "bank.html", v, rows=rows, members=v.reg.members, verification=v.reg.verification)

    @app.get("/rosters", response_class=HTMLResponse)
    async def rosters(request: Request):
        """Runs per raid for the current/upcoming lockout: tentative (proposals) and accepted (dated rosters + sheets)."""
        from datetime import timedelta

        v = await need(request, officer=True)
        reg = v.reg
        rs = bot.raids.store(reg)
        ps = bot.proposals(reg)
        now = datetime.now().astimezone()
        events = sorted(rs.events.values(), key=lambda e: e.starts_at, reverse=True)

        def ev_row(ev):
            t = reg.config.team(ev.team) or {"key": ev.team, "size": reg.raid_def(ev.instance).get("size", 20)}
            live = ev.state not in ("done", "cancelled")
            return {"ev": ev, "team": t, "needs": rc.needs(reg, ev, t) if live else None, "busy": rc.conflicts(rs, ev) if live else {}, "by": {st: ev.by_status(st) for st in rc.STATUSES}, "live": live}

        out = []
        for rid in reg.profile.raids:
            rd = reg.raid_def(rid)
            horizon = now + timedelta(days=int(rd.get("lockout_days", 7)) + 1)
            evs = [ev_row(e) for e in events if e.instance == rid]
            current = [e for e in evs if e["live"] and e["ev"].start <= horizon]
            past = [e for e in evs if not e["live"] or e["ev"].start > horizon][:6]
            props = sorted([p for p in ps.items.values() if p.instance == rid], key=lambda p: p.created_at, reverse=True)
            ws, we = reg.lockout_window(rid, now)
            fo = reg.first_open(rid)
            out.append({"id": rid, "eff": rd, "current": current, "past": past, "open": [p for p in props if p.state in ("proposed", "draft")], "history": [p for p in props if p.state not in ("proposed", "draft")][:4],
                        "standing": [t for t in reg.config.rosters if t.get("instance") == rid and not t.get("ephemeral")], "window": (ws, we), "opened": bool(fo and fo <= now), "first_open": fo})
        orphans = [ev_row(e) for e in events if e.instance not in reg.profile.raids][:6]
        return page(request, "rosters.html", v, raids=out, orphans=orphans)

    @app.get("/raids", response_class=HTMLResponse)
    async def raids(request: Request):
        v = await need(request, officer=True)
        reg = v.reg
        ps = bot.proposals(reg)
        from zoneinfo import ZoneInfo

        z = ZoneInfo(reg.config.timezone)
        now = datetime.now(z)
        out = []
        for rid in reg.profile.raids:
            fo = reg.first_open(rid)
            ws, we = reg.lockout_window(rid, now)
            out.append({"id": rid, "eff": reg.raid_def(rid), "over": reg.config.raids.get(rid, {}), "runs": len(reg.run_keys(rid)), "open": len(ps.open_for(rid)),
                        "first_open_local": fo.astimezone(z).strftime("%Y-%m-%dT%H:%M") if fo else "", "window": (ws.astimezone(z), we.astimezone(z)), "opened": bool(fo and fo <= now)})
        return page(request, "raids.html", v, raids=out, owner=v.owner, tz=reg.config.timezone)

    @app.post("/admin/plan")
    async def admin_plan(request: Request):
        v, d = await form(request, officer=True)
        inst = d.get("instance", "")
        if inst not in v.reg.profile.raids:
            return back("/rosters", err="unknown raid")
        line = await bot.auto_propose(v.reg, inst, by=v.name)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name} ran the planner: {line}")
        return back("/rosters", ok=line)

    @app.post("/admin/proposal")
    async def admin_proposal(request: Request):
        v, d = await form(request, officer=True)
        ps = bot.proposals(v.reg)
        p = ps.items.get(d.get("pid", ""))
        if not p or p.state not in ("proposed", "draft"):
            return back("/rosters", err="that proposal is no longer open")
        if d.get("answer") == "accept":
            line = await bot.accept_proposal(v.reg, p, v.name)
        else:
            p.state, p.decided_by, p.decided_at = "rejected", v.name, rc.now()
            ps.save(p, f"rejected by {v.name}")
            line = f"proposal {p.id} rejected"
        await bot.ops.emit(v.reg.config, "info", f"[web] {line}")
        return back("/rosters", ok=line)

    @app.get("/config", response_class=HTMLResponse)
    async def config(request: Request):
        v = await need(request, officer=True)
        import yaml

        from ..policy import PolicyStore

        ps = PolicyStore(bot.registries.store, v.reg.key)
        docs = {d: {"text": ps.read(d), "compiled": ps.compiled(d)} for d in ("loot", "comp", "persona")}
        return page(request, "config.html", v, yaml=yaml.safe_dump(v.reg.config.model_dump(), sort_keys=False), docs=docs)

    @app.get("/ops", response_class=HTMLResponse)
    async def ops(request: Request):
        v = await need(request, officer=True)
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
