"""JSON API for the React front end (frontend/ → static/app, served at /app).

Same identity as the HTML pages (session cookie, guild membership as the whitelist). Mutations go through the
same Registry functions the Discord commands call. CSRF: every POST must carry `X-Requested-With: oibot` —
a header a cross-site form cannot set — and the session cookie is SameSite, so no per-form token is needed."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..registry import RegistryError

HERE = Path(__file__).parent
APP_DIR = HERE / "static" / "app"


def install_api(app: FastAPI, bot, *, viewer, icon_url) -> None:
    async def who(request: Request, officer: bool = False):
        v = await viewer(request)
        if v is None:
            raise HTTPException(401, "login required")
        if officer and not v.officer:
            raise HTTPException(403, "Officers only.")
        return v

    async def body(request: Request, officer: bool = False):
        if request.headers.get("x-requested-with") != "oibot":
            raise HTTPException(403, "Missing request marker.")
        v = await who(request, officer=officer)
        try:
            d = await request.json()
        except ValueError:
            d = {}
        return v, (d if isinstance(d, dict) else {})

    async def run(request: Request, fn, officer: bool = False):
        """Apply a mutation and answer {message}; RegistryError/ValueError → 400 with the reason."""
        v, d = await body(request, officer=officer)
        try:
            msg = await asyncio.to_thread(fn, v, d)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {msg}")
        return {"message": msg}

    @app.exception_handler(HTTPException)
    async def _json_errors(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    # ---- read
    @app.get("/api/meta")
    async def meta(request: Request):
        v = await who(request)
        reg = v.reg
        return {
            "guild": reg.config.name, "tz": reg.config.timezone,
            "classes": {c: {s: a.get("role") for s, a in specs.items()} for c, specs in reg.profile.classes.items()},
            "icons": {k: dict(reg.profile.icons.get(k, {})) for k in ("classes", "roles", "specs")},
            "raids": [{"id": rid, "name": rd.get("name", rid), "size": int(rd.get("size") or 20), "lockout_days": int(reg.raid_def(rid).get("lockout_days", 7))} for rid, rd in reg.profile.raids.items()],
            "viewer": {"uid": str(v.uid), "name": v.name, "officer": v.officer, "owner": v.owner},
        }

    def char_json(reg, c) -> dict:
        role = reg.profile.spec(c.cls, c.spec).role
        off = reg.profile.spec(c.cls, c.offspec).role if c.offspec else None
        return {"label": c.label, "name": c.name, "surname": c.surname, "cls": c.cls, "spec": c.spec, "offspec": c.offspec, "is_main": c.is_main,
                "status": c.status, "rank": c.rank, "rosters": list(c.rosters), "confirmed": bool(c.confirmed_by), "role": role, "off_role": off}

    @app.get("/api/me")
    async def me(request: Request):
        v = await who(request)
        reg, m = v.reg, v.member
        rs = bot.raids.store(reg)
        sheets = []
        for ev in rs.live():
            s = ev.signups.get(str(v.uid))
            team = reg.config.team(ev.team) or {"key": ev.team}
            rd = reg.raid_def(team.get("instance")) if team.get("instance") else {}
            sheets.append({"key": ev.key, "raid": rd.get("name") or team.get("name", ev.team), "starts_at": ev.starts_at, "when": reg.local(ev.starts_at, "%a %d %b %H:%M"), "state": ev.state,
                           "status": s.status if s else None, "character": s.character if s else None, "note": s.note if s else None})
        primary, flex = reg.roles_of(m) if m else (None, [])
        today = reg.now_local().date().isoformat()
        return {
            "display_name": m.display_name if m else v.name, "registered": m is not None,
            "characters": [char_json(reg, c) for c in (m.active() if m else [])],
            "roles": {"primary": primary, "flex": list(flex)},
            "week": list(m.week) if m else [],
            "absences": [{"start": a.start, "end": a.end, "reason": a.reason} for a in (m.upcoming_absences(today) if m else [])],
            "dm": not (m.dm_opt_out if m else False),
            "sheets": sheets,
            "asks": [{"roster": a["roster"], "character": a["character"], "asked_at": a.get("asked_at")} for a in reg.open_placement_asks(v.uid)],
            "raid_windows": [{"slot": t["schedule"], "name": t.get("name", t["key"])} for t in reg.config.rosters if t.get("schedule")],
        }

    # ---- member self-service
    @app.post("/api/me/characters")
    async def me_characters(request: Request):
        """One save for the table: spec/offspec per existing row, names for planned rows, new rows added or planned."""
        def go(v, d):
            reg, done = v.reg, []
            m = reg.members.get(v.uid)
            current = {c.label: c for c in (m.active() if m else [])}
            for row in d.get("rows") or []:
                cls, spec, off = (row.get("cls") or "").strip(), (row.get("spec") or "").strip(), (row.get("offspec") or "").strip() or None
                name, surname = (row.get("name") or "").strip(), (row.get("surname") or "").strip() or None
                slot = "main" if row.get("slot") == "main" else "alt"
                c = current.get(row.get("label") or "")
                if c is None:
                    if not cls or not spec:
                        continue
                    if name:
                        _, c = reg.add_character(v.uid, v.name, name, cls, spec, off, slot == "main", surname=surname)
                        done.append(f"added {c.label}")
                    else:
                        _, c = reg.set_plan(v.uid, v.name, cls, spec, off, slot)
                        done.append(f"planned {c.cls} {c.spec}")
                    continue
                if spec and (spec, off) != (c.spec, c.offspec):
                    reg.set_spec(v.uid, c.label, spec, off)
                    done.append(f"{c.label}: {spec}" + (f"/{off}" if off else ""))
                if not c.name and name:
                    _, named = reg.name_character(v.uid, name, "main" if c.is_main else "alt", surname=surname, label=c.label)
                    done.append(f"named {named.label}")
            return "; ".join(done) or "no changes"
        return await run(request, go)

    @app.post("/api/me/main")
    async def me_main(request: Request):
        return await run(request, lambda v, d: f"main is now {v.reg.set_main(v.uid, d['label'])[1].label}")

    @app.post("/api/me/character/delete")
    async def me_delete(request: Request):
        return await run(request, lambda v, d: f"deleted {v.reg.delete_character(v.uid, d['label']).label}")

    @app.post("/api/me/week")
    async def me_week(request: Request):
        def go(v, d):
            m = v.reg.set_week(v.uid, list(d.get("week") or []), v.name)
            hours = sum((r["end"] - r["start"]) for r in m.week) / 60
            return f"availability saved: {hours:.0f}h/week across {len(m.week)} block(s)"
        return await run(request, go)

    @app.post("/api/me/absence")
    async def me_absence(request: Request):
        return await run(request, lambda v, d: f"absent {v.reg.add_absence(v.uid, d['start'], d.get('end') or None, d.get('reason') or None, v.name, v.name)[1].start}")

    @app.post("/api/me/absence/clear")
    async def me_absence_clear(request: Request):
        return await run(request, lambda v, d: (v.reg.clear_absence(v.uid, d["start"]) and f"cleared absence {d['start']}"))

    @app.post("/api/me/placement")
    async def me_placement(request: Request):
        def go(v, d):
            yes = d.get("answer") == "yes"
            line = v.reg.answer_placement(v.uid, d["roster"], yes, v.name)
            bot.loop.create_task(bot.after_placement_answer(v.reg, v.uid, d["roster"], yes, line))
            return line
        return await run(request, go)

    @app.post("/api/me/dm")
    async def me_dm(request: Request):
        def go(v, d):
            m = v.reg.member(v.uid)
            m.dm_opt_out = not bool(d.get("on"))
            v.reg.save(m, f"{m.display_name} DMs {'off' if m.dm_opt_out else 'on'}")
            return f"DMs {'off' if m.dm_opt_out else 'on'}"
        return await run(request, go)

    # ---- the built SPA (history-mode routes fall back to index.html)
    @app.get("/app")
    @app.get("/app/{path:path}")
    async def spa(request: Request, path: str = ""):
        if not APP_DIR.exists():
            raise HTTPException(503, "front end not built (cd frontend && npm run build)")
        target = (APP_DIR / path).resolve() if path else None
        if target and target.is_file() and APP_DIR.resolve() in target.parents:
            return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else None)
        if await viewer(request) is None:
            from fastapi.responses import RedirectResponse

            return RedirectResponse("/auth/login?next=" + request.url.path, status_code=307)
        return FileResponse(APP_DIR / "index.html", headers={"Cache-Control": "no-cache"})
