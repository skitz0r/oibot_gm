"""JSON API for the React front end (frontend/ → static/app, served at /app).

Same identity as the HTML pages (session cookie, guild membership as the whitelist). Mutations go through the
same Registry / raidcycle functions the Discord commands call. CSRF: every POST must carry
`X-Requested-With: oibot` — a header a cross-site form cannot set — and the session cookie is SameSite."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import comp as comp_mod, discord_raid as dr, raidcycle as rc
from ..registry import RAID_HOURS_FIELDS, RAID_WEIGHT_DEFAULTS, RegistryError
from ..roster import coverage as cov_mod

HERE = Path(__file__).parent
APP_DIR = HERE / "static" / "app"


def install_api(app: FastAPI, bot, *, viewer, icon_url, privilege) -> None:
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
            "labels": dict(rc.LABELS),
        }

    def char_json(reg, c) -> dict:
        role = reg.profile.spec(c.cls, c.spec).role
        off = reg.profile.spec(c.cls, c.offspec).role if c.offspec else None
        return {"label": c.label, "name": c.name, "surname": c.surname, "cls": c.cls, "spec": c.spec, "offspec": c.offspec, "is_main": c.is_main,
                "status": c.status, "rank": c.rank, "rosters": list(c.rosters), "confirmed": bool(c.confirmed_by), "role": role, "off_role": off}

    def absence_json(a) -> dict:
        return {"start": a.start, "end": a.end, "reason": a.reason}

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
            seat = ev.seat_of(m.display_name) if m and ev.state != "open" else None
            sheets.append({"key": ev.key, "raid": rd.get("name") or team.get("name", ev.team), "starts_at": ev.starts_at, "when": t12(reg, ev.starts_at), "state": ev.state,
                           "status": s.status if s else None, "label": rc.LABELS.get(s.status, s.status) if s else None, "character": s.character if s else None, "note": s.note if s else None,
                           "seated": bool(seat), "roster": (seat[0] + 1) if seat else None})
        primary, flex = reg.roles_of(m) if m else (None, [])
        today = reg.now_local().date().isoformat()
        return {
            "display_name": m.display_name if m else v.name, "registered": m is not None,
            "characters": [char_json(reg, c) for c in (m.active() if m else [])],
            "roles": {"primary": primary, "flex": list(flex)},
            "absences": [absence_json(a) for a in (m.upcoming_absences(today) if m else [])],
            "dm": not (m.dm_opt_out if m else False),
            "sheets": sheets,
            "asks": [{"roster": a["roster"], "character": a["character"], "asked_at": a.get("asked_at")} for a in reg.open_placement_asks(v.uid)],
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

    @app.post("/api/me/absence")
    async def me_absence(request: Request):
        v, d = await body(request)
        try:
            m, a = await asyncio.to_thread(v.reg.add_absence, v.uid, d.get("start") or "", d.get("end") or None, d.get("reason") or None, v.name, v.name)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.announce_absence(v.reg, m, a, v.name)
        return {"message": f"away {a.start}" + (f" → {a.end}" if a.end != a.start else "")}

    @app.post("/api/me/absence/clear")
    async def me_absence_clear(request: Request):
        return await run(request, lambda v, d: (v.reg.clear_absence(v.uid, d["start"]) and f"cleared absence {d['start']}"))

    @app.post("/api/me/placement")
    async def me_placement(request: Request):
        v, d = await body(request)
        yes = d.get("answer") == "yes"
        try:
            line = await asyncio.to_thread(v.reg.answer_placement, v.uid, d["roster"], yes, v.name)
        except (RegistryError, ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "bad request"}, status_code=400)
        await bot.after_placement_answer(v.reg, v.uid, d["roster"], yes, line)
        return {"message": line}

    @app.post("/api/me/dm")
    async def me_dm(request: Request):
        def go(v, d):
            m = v.reg.member(v.uid)
            m.dm_opt_out = not bool(d.get("on"))
            v.reg.save(m, f"{m.display_name} DMs {'off' if m.dm_opt_out else 'on'}")
            return f"DMs {'off' if m.dm_opt_out else 'on'}"
        return await run(request, go)

    # ---- runs: the officer panel (sheets per raid, the board, lock, confirmations)
    def t12(reg, value) -> str:
        """'Wed 09 Dec 4:00 PM' in guild time (12-hour clock everywhere the site shows a run time)."""
        return reg.local(value, "%a %d %b ") + clock12(reg, value)

    def clock12(reg, value) -> str:
        t = reg.local(value, "%I:%M %p")
        return t[1:] if t.startswith("0") else t

    def rel(reg, value) -> str:
        t = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
        secs = (t - reg.now_local()).total_seconds()
        if secs < -3600:
            return "started"
        if secs < 3600:
            return f"in {max(1, int(secs // 60))} min"
        if secs < 48 * 3600:
            return f"in {int(secs // 3600)} h"
        return f"in {int(secs // 86400)} days"

    def signup_json(s, pins: dict) -> dict:
        return {"uid": str(s.discord_id), "display_name": s.display_name, "character": s.character, "cls": s.cls, "spec": s.spec, "offspec": s.offspec, "role": s.role, "status": s.status,
                "label": rc.LABELS.get(s.status, s.status), "source": s.source, "note": s.note, "pin": pins.get(str(s.discord_id))}

    def roster_summary(reg, r) -> dict:
        try:
            cov = cov_mod.compute(reg.profile, r.selected, r)
            return comp_mod.groups_summary(reg, r.selected, r, cov)
        except Exception:  # noqa: BLE001
            return comp_mod.groups_summary(reg, r.selected, None, None)

    def seat_json(p, conf: dict, uids: dict) -> dict:
        c = conf.get(p.signup_name) or {}
        return {"display_name": p.signup_name, "character": p.character or p.signup_name, "cls": p.cls, "spec": p.spec, "role": p.role, "uid": c.get("uid") or uids.get(p.signup_name), "answer": c.get("answer")}

    def board_json(reg, ev, rosters, conf_rows) -> dict:
        """The bank + groups the officers drag on: one entry per roster, groups of seats, aura summary per roster."""
        conf = {c["display_name"]: c for c in conf_rows}
        uids = {s.display_name: str(s.discord_id) for s in ev.signups.values()}
        team = reg.config.team(ev.team) or {}
        size = int(team.get("size") or reg.raid_def(ev.instance).get("size") or 20)
        boards = []
        for i, r in enumerate(rosters):
            by = {p.signup_name: p for p in r.selected}
            boards.append({"n": i + 1, "seated": len(r.selected), "synergy": r.synergy_value or None, "advisories": list(r.advisories[:6]),
                           "groups": [[seat_json(by[m], conf, uids) for m in g if m in by] for g in r.groups], "summary": roster_summary(reg, r)})
        bench = rosters[0].benched if rosters else []
        return {"n_groups": rc.groups_per_roster(reg, size), "group_size": int(reg.profile.comp_rules["group_size"]), "size": size,
                "bank": [seat_json(p, conf, uids) for p in bench], "rosters": boards}

    def ev_json(reg, rs, ev, full: bool = True) -> dict:
        t = reg.config.team(ev.team) or {"key": ev.team, "size": reg.raid_def(ev.instance).get("size", 20)}
        live = ev.state not in ("done", "cancelled")
        rd = reg.raid_def(ev.instance)
        day = ev.start.astimezone(reg.tz).date().isoformat()
        base = {"key": ev.key, "run": ev.team, "name": t.get("name") or t["key"], "size": int(t.get("size") or 20), "instance": ev.instance, "raid": rd.get("name", ev.instance),
                "starts_at": ev.starts_at, "when": t12(reg, ev.starts_at), "rel": rel(reg, ev.start), "state": ev.state, "live": live, "fill_state": ev.fill_state,
                "counts": {st: len(ev.by_status(st)) for st in rc.STATUSES}, "seated": len(ev.seated()) if ev.all_rosters else 0, "n_rosters": len(ev.all_rosters)}
        if not full:
            return base
        from ..discord_raid import run_times

        soft, hard, confirm = run_times(reg, ev, t)
        conf_rows = rc.confirmations(reg, ev) if ev.all_rosters else []
        board_rosters = ev.all_rosters if ev.state != "open" and ev.all_rosters else rc.board_rosters(reg, ev, ev.layout or [], int(t.get("size") or 20))
        absences = [{"display_name": m.display_name, "start": a.start, "end": a.end, "reason": a.reason, "signed": str(m.discord_id) in ev.signups}
                    for m in reg.members.values() for a in m.absences if a.start <= day <= a.end]
        busy = rc.conflicts(rs, ev) if live else {}
        return {**base,
                "timeline": {"nudge": t12(reg, soft), "lock": t12(reg, hard), "confirm": clock12(reg, confirm) if confirm.date() == ev.start.astimezone(reg.tz).date() else t12(reg, confirm)},
                "signups": [signup_json(s, ev.pins) for s in sorted(ev.signups.values(), key=lambda s: (rc.STATUSES.index(s.status) if s.status in rc.STATUSES else 9, s.updated_at))],
                "not_answered": [{"uid": str(m.discord_id), "display_name": m.display_name, "character": m.main.label, "cls": m.main.cls, "spec": m.main.spec, "role": reg.profile.spec(m.main.cls, m.main.spec).role}
                                 for m in reg.members.values() if m.main and str(m.discord_id) not in ev.signups] if live else [],
                "absences": absences, "double_booked": [reg.members[u].display_name for u in busy if u in reg.members],
                "needs": rc.needs(reg, ev, t) if live else None,
                "board": board_json(reg, ev, board_rosters, conf_rows), "has_layout": bool(ev.layout),
                "split": {"strategy": ev.split_strategy or rd.get("split_policy", "balanced"), "policy": rd.get("split_policy", "balanced"),
                          "runs": rc.how_many_rosters(reg, rc.players_for(reg, ev), int(t.get("size") or 20), reg.role_bounds(ev.instance, int(t.get("size") or 20))) if live else 1},
                "confirmations": conf_rows,
                "fill_asks": [{"display_name": a.display_name, "kind": a.kind, "character": a.character, "spec": a.spec, "role": a.role, "reason": a.reason, "answer": a.answer, "expires_at": a.expires_at, "pair": a.pair} for a in ev.fill_asks],
                "callouts": [{"display_name": c.display_name, "hours_before": c.hours_before, "late": c.late} for c in ev.callouts], "log": list(ev.log)}

    def rosters_data(reg) -> dict:
        rs = bot.raids.store(reg)
        now = reg.now_local()
        events = sorted(rs.events.values(), key=lambda e: e.starts_at)
        out = []
        for rid in reg.profile.raids:
            rd = reg.raid_def(rid)
            evs = [e for e in events if e.instance == rid]
            live = [ev_json(reg, rs, e) for e in evs if e.state not in ("done", "cancelled")]
            past = [ev_json(reg, rs, e, full=False) for e in reversed(evs) if e.state in ("done", "cancelled")][:8]
            fo = reg.first_open(rid)
            upcoming = [{"slot": slot, "start": t12(reg, start), "opens": t12(reg, start - timedelta(hours=float(rd["signup_lead_hours"])))}
                        for slot, start in rc.slot_starts(reg, rid, now, 24 * 21) if f"{rc.run_key(rid, start)}-{start.date().isoformat()}" not in rs.events][:6]
            out.append({"id": rid, "name": rd.get("name", rid), "size": int(rd.get("size") or 20), "slots": list(rd["slots"]), "lockout_days": int(rd["lockout_days"]),
                        "opened": bool(fo and fo <= now), "first_open": reg.local(fo, "%a %d %b %Y %H:%M") if fo else None,
                        "open": [e for e in live if e["state"] == "open"], "locked": [e for e in live if e["state"] != "open"], "past": past, "upcoming": upcoming})
        orphans = [ev_json(reg, rs, e, full=False) for e in events if e.instance not in reg.profile.raids and e.state not in ("done", "cancelled")][:6]
        return {"raids": out, "orphans": orphans, "tz": reg.config.timezone}

    @app.get("/api/rosters")
    async def rosters(request: Request):
        v = await who(request, officer=True)
        return await asyncio.to_thread(rosters_data, v.reg)

    def live_event(reg, key: str):
        rs = bot.raids.store(reg)
        ev = rs.events.get(key)
        if not ev or ev.state in ("done", "cancelled"):
            raise HTTPException(404, "that run is closed")
        return rs, ev, (reg.config.team(ev.team) or {"key": ev.team, "size": 20})

    def board_reply(reg, ev):
        t = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        rosters = ev.all_rosters if ev.state != "open" and ev.all_rosters else rc.board_rosters(reg, ev, ev.layout or [], int(t.get("size") or 20))
        return {"board": board_json(reg, ev, rosters, rc.confirmations(reg, ev) if ev.all_rosters else []), "needs": rc.needs(reg, ev, t)}

    @app.post("/api/run/{key}/layout")
    async def run_layout(request: Request, key: str):
        """The board after a drag: before lock it is saved as the layout the lock will use; after lock it is the roster
        (group moves are free, someone dragged in from the bench is asked to confirm, someone dragged out is freed)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        groups = [[str(n) for n in g] for g in (d.get("groups") or [])]
        names = {s.display_name for s in ev.signups.values() if s.status == "in"}
        groups = [[n for n in g if n in names] for g in groups]
        if ev.state == "open":
            ev.layout = groups if any(groups) else None
            rs.save(ev, "board")
            return board_reply(v.reg, ev)
        added, removed = await asyncio.to_thread(rc.apply_layout_locked, v.reg, rs, ev, groups)
        await bot.after_board_change(v.reg, rs, ev, t, added, removed, v.name)
        return {**board_reply(v.reg, ev), "message": (f"asked {', '.join(s.display_name for s in added)} to confirm" if added else "") + (f"; freed {', '.join(removed)}" if removed else "") or "groups updated"}

    @app.post("/api/run/{key}/split")
    async def run_split(request: Request, key: str):
        """Preview a split under a strategy (nothing saved): the modal's step 2. `avoid` = previous previews to differ from."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked"}, status_code=400)
        strategy = d.get("strategy") or ev.split_strategy or v.reg.raid_def(ev.instance)["split_policy"]
        from ..registry import SPLIT_POLICIES

        if strategy not in SPLIT_POLICIES:
            return JSONResponse({"error": f"strategy must be one of {', '.join(SPLIT_POLICIES)}"}, status_code=400)
        try:
            layout, rosters = await asyncio.to_thread(rc.split_preview, v.reg, rs, ev, strategy, d.get("avoid") or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"solver: {e}"}, status_code=400)
        syn = [r.synergy_value for r in rosters]
        return {"strategy": strategy, "layout": layout, "board": board_json(v.reg, ev, rosters, []), "synergy": syn, "total": sum(syn), "gap": (max(syn) - min(syn)) if syn else 0}

    @app.post("/api/run/{key}/strategy")
    async def run_strategy(request: Request, key: str):
        """Remember the split philosophy on the run (Auto-fill and the scheduled lock use it)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        from ..registry import SPLIT_POLICIES

        if d.get("strategy") not in SPLIT_POLICIES:
            return JSONResponse({"error": "unknown strategy"}, status_code=400)
        ev.split_strategy = d["strategy"]
        rs.save(ev, f"split strategy {d['strategy']}")
        return {"message": f"split strategy: {d['strategy']}"}

    @app.post("/api/run/{key}/autofill")
    async def run_autofill(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked — move people on the board instead"}, status_code=400)
        try:
            layout = await asyncio.to_thread(rc.autofill, v.reg, rs, ev)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"solver: {e}"}, status_code=400)
        ev.layout = layout
        ev.log.append(f"{v.name}: auto-filled the board")
        rs.save(ev, "board auto-filled")
        return board_reply(v.reg, ev)

    @app.post("/api/run/{key}/pin")
    async def run_pin(request: Request, key: str):
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        uid, pin = str(d.get("uid") or ""), d.get("pin")
        if pin not in ("in", "out", None):
            return JSONResponse({"error": "pin must be in, out or null"}, status_code=400)
        if pin:
            ev.pins[uid] = pin
        else:
            ev.pins.pop(uid, None)
        name = ev.signups[uid].display_name if uid in ev.signups else uid
        ev.log.append(f"{v.name}: {name} {'pinned ' + pin if pin else 'unpinned'}")
        rs.save(ev, f"pin {name} {pin}")
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {ev.key} {name} {'pinned ' + pin if pin else 'unpinned'}")
        return {"message": f"{name}: {'pinned ' + pin if pin else 'unpinned'}"}

    @app.post("/api/run/{key}/set")
    async def run_set(request: Request, key: str):
        """Officer sets someone's answer (join / bench / out) or swaps their character."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        m = v.reg.members.get(int(d.get("uid") or 0))
        status = d.get("status")
        if not m or status not in rc.STATUSES:
            return JSONResponse({"error": "unknown member or status"}, status_code=400)
        if ev.state != "open" and status == "out" and ev.seat_of(m.display_name):
            await bot.drop_seated(v.reg, rs, ev, t, m, "officer", None)
            return {"message": f"{m.display_name} taken off the roster; filling the seat"}
        try:
            s = await asyncio.to_thread(rc.set_signup, v.reg, rs, ev, m, d.get("character") or None, status, "officer")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if ev.state != "open" and status == "in" and not ev.seat_of(m.display_name):
            rc.seat_player(v.reg, ev, s)
            rs.save(ev, f"{m.display_name} seated by {v.name}")
        await bot.refresh_sheet(v.reg, ev)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {ev.key} {m.display_name} {status} as {s.character}")
        return {"message": f"{m.display_name}: {s.character} {rc.LABELS[status]}"}

    @app.post("/api/run/{key}/lock")
    async def run_lock(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked"}, status_code=400)
        line = await bot.lock_run(v.reg, rs, ev, by=v.name)
        await bot.ops.emit(v.reg.config, "info", f"[web] {line}")
        return {"message": line}

    @app.post("/api/run/{key}/fill")
    async def run_fill(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        sent, nd = await bot.run_fill(v.reg, rs, ev, t, by=v.name)
        if sent:
            await bot.post_run_update(v.reg, ev, "🧩 " + "\n🧩 ".join(dr.ask_line(v.reg, bot.ico, a) for a in sent))
        return {"message": ("asked " + ", ".join(f"{a.display_name} ({'swap' if a.swap else a.kind})" for a in sent)) if sent else ("nothing to fill" if not (nd["headcount"] or nd["roles"]) else "nobody left to ask")}

    @app.post("/api/run/{key}/cancel")
    async def run_cancel(request: Request, key: str):
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        ev.state = "cancelled"
        ev.log.append(f"cancelled by {v.name}: {d.get('reason') or ''}")
        rs.save(ev, "cancelled")
        await bot.refresh_sheet(v.reg, ev)
        await bot.ops.emit(v.reg.config, "warn", f"[web] {v.name} cancelled {ev.key}")
        return {"message": f"cancelled {ev.key}"}

    @app.post("/api/raid/{rid}/open")
    async def raid_open(request: Request, rid: str):
        """Open a sheet now: the raid's next slot, or a one-off 'YYYY-MM-DD HH:MM' in guild time."""
        v, d = await body(request, officer=True)
        reg = v.reg
        if rid not in reg.profile.raids:
            return JSONResponse({"error": "unknown raid"}, status_code=400)
        now = reg.now_local()
        when = (d.get("when") or "").strip()
        try:
            if when:
                start = datetime.fromisoformat(when.replace(" ", "T", 1)).replace(tzinfo=ZoneInfo(reg.config.timezone))
            else:
                nxt = rc.slot_starts(reg, rid, now, 24 * 21)
                if not nxt:
                    return JSONResponse({"error": "no slots configured for this raid (or none before it opens) — pass a date and time"}, status_code=400)
                start = nxt[0][1]
        except ValueError:
            return JSONResponse({"error": "time looks like 2026-12-10 19:30"}, status_code=400)
        rs = bot.raids.store(reg)
        ev = rc.open_run(reg, rs, rid, start, by=v.name)
        ch = bot.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None
        if not ev.message_id and ch:
            await bot.post_sheet(reg, rs, ev, ch)
        await bot.ops.emit(reg.config, "info", f"[web] {v.name} opened {ev.key}")
        return {"message": f"opened {ev.key}" + ("" if ch else " (no signup channel set — sheet not posted)")}

    @app.post("/api/admin/confirm")
    async def admin_confirm(request: Request):
        return await run(request, lambda v, d: f"confirmed {v.reg.confirm(d['label'], v.name)[1].label}", officer=True)

    # ---- raids: rules per raid (officers read, owner edits)
    @app.get("/api/raids")
    async def raids(request: Request):
        v = await who(request, officer=True)
        reg = v.reg
        z = ZoneInfo(reg.config.timezone)
        now = reg.now_local()
        rs = bot.raids.store(reg)
        out = []
        for rid in reg.profile.raids:
            eff, over, fo = reg.raid_def(rid), reg.config.raids.get(rid, {}), reg.first_open(rid)
            ws, we = reg.lockout_window(rid, now)
            out.append({"id": rid, "name": eff.get("name", rid), "size": int(eff.get("size") or 20), "lockout_days": eff["lockout_days"], "duration_hours": eff["duration_hours"],
                        "slots": list(eff["slots"]), "signup_lead_hours": eff["signup_lead_hours"], "lock_hours_before": eff["lock_hours_before"], "confirm_hours_before": eff["confirm_hours_before"],
                        "weights": dict(eff["weights"]), "split_policy": eff.get("split_policy", "balanced"), "nudge": bool(eff.get("nudge", True)), "nudge_hours_before": eff["nudge_hours_before"], "fill_ask_hours": eff["fill_ask_hours"], "notes": eff.get("notes") or "", "comp": {r: dict((eff.get("comp") or {}).get(r) or {}) for r in ("tank", "healer", "dps")},
                        "overridden": sorted(k for k in over if k not in ("comp", "weights")) + [f"{r}_{b}" for r, bb in ((over.get("comp") or {}).items()) for b in bb] + [f"weight_{k}" for k in (over.get("weights") or {})],
                        "comp_targets": over.get("comp_targets") or {}, "comp_groups": over.get("comp_groups") or [],
                        "first_open_local": fo.astimezone(z).strftime("%Y-%m-%dT%H:%M") if fo else "", "opened": bool(fo and fo <= now),
                        "window": [reg.local(ws, "%a %d %b %H:%M"), reg.local(we, "%a %d %b %H:%M")], "live": sum(1 for e in rs.live() if e.instance == rid)})
        from ..registry import SPLIT_POLICIES

        return {"raids": out, "tz": reg.config.timezone, "owner": v.owner, "weight_keys": list(RAID_WEIGHT_DEFAULTS), "split_policies": list(SPLIT_POLICIES)}

    @app.post("/api/admin/raid")
    async def admin_raid(request: Request):
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            inst = d["instance"]
            cur, done = v.reg.raid_def(inst), []
            if "slots" in d:
                want = d["slots"] if isinstance(d["slots"], list) else str(d["slots"])
                from ..registry import parse_slots

                if parse_slots(want) != list(cur["slots"]):
                    done.append(v.reg.set_raid_override(inst, "slots", want, v.name))
            for f in RAID_HOURS_FIELDS + ("lockout_days", "duration_hours", "notes", "split_policy"):
                val = d.get(f)
                if val not in (None, "") and str(val) != str(cur.get(f, "")):
                    done.append(v.reg.set_raid_override(inst, f, str(val), v.name))
            if "nudge" in d and d["nudge"] is not None and bool(d["nudge"]) != bool(cur.get("nudge", True)):
                done.append(v.reg.set_raid_override(inst, "nudge", "true" if d["nudge"] else "false", v.name))
            for k, val in (d.get("weights") or {}).items():
                if k in RAID_WEIGHT_DEFAULTS and val not in (None, "") and int(val) != int(cur["weights"].get(k, 0)):
                    done.append(v.reg.set_raid_override(inst, f"weight_{k}", str(val), v.name))
            fo = d.get("first_open") or ""
            cur_fo = v.reg.first_open(inst)
            if fo and (cur_fo is None or fo[:16] != cur_fo.astimezone(ZoneInfo(v.reg.config.timezone)).strftime("%Y-%m-%dT%H:%M")):
                done.append(v.reg.set_raid_override(inst, "first_open", fo, v.name))
            for role in ("tank", "healer", "dps"):
                for bound in ("min", "max"):
                    val = (d.get("comp") or {}).get(role, {}).get(bound)
                    if val not in (None, "") and str(val) != str(((cur.get("comp") or {}).get(role) or {}).get(bound, "")):
                        done.append(v.reg.set_raid_override(inst, f"{role}_{bound}", str(val), v.name))
            return "; ".join(done) or f"{inst}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/admin/raid/reset")
    async def admin_raid_reset(request: Request):
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            return v.reg.clear_raid_override(d["instance"], v.name)
        return await run(request, go, officer=True)

    # ---- auras: the buff matrix with the guild's learned facts (owner edits; officers read)
    @app.get("/api/auras")
    async def auras(request: Request):
        v = await who(request, officer=True)
        reg = v.reg
        prof, base = reg.profile, reg.base_profile
        specs = [{"cls": c, "spec": sp, "role": a.get("role"), "key": f"spec:{sp}"} for c, ss in prof.classes.items() for sp, a in ss.items()]
        fams = []
        for fid, f in prof.families.items():
            over = reg.config.families.get(fid, {})
            fams.append({"id": fid, "name": f.name, "value": dict(f.value), "status": f.status, "note": f.note, "overridden": sorted(over), "declared": fid in base.families or fid in reg.config.families,
                         "buffs": [b.id for b in prof.buffs if b.family_id == fid]})
        buffs = []
        for b in prof.buffs:
            if b.kind != "aura" or not b.providers:
                continue  # procs and placeholders don't take part in grouping
            over = reg.config.buffs.get(b.id, {})
            buffs.append({"id": b.id, "name": b.short, "abbr": b.abbr, "colour": b.colour, "art": b.art, "providers": list(b.providers), "scope": b.scope, "kind": b.kind, "slot": b.slot,
                          "family": b.family_id, "strength": b.strength, "status": b.status, "note": b.note, "overridden": sorted(over), "choices": list(b.choices)})
        return {"families": fams, "buffs": buffs, "value_keys": list(reg.VALUE_KEYS), "specs": specs, "scopes": list(reg.BUFF_SCOPES), "statuses": list(reg.STATUSES), "owner": v.owner}

    @app.post("/api/admin/aura")
    async def admin_aura(request: Request):
        """One buff: any of scope / family / strength / status / note (only fields present in the body change)."""
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            bid, done = d["id"], []
            cur = v.reg.buff(bid)
            for f in ("scope", "family", "strength", "status", "note"):
                if f in d and d[f] is not None and str(d[f]) != str(getattr(cur, "family_id" if f == "family" else f)):
                    done.append(v.reg.set_buff_override(bid, f, d[f], v.name))
            return "; ".join(done) or f"{bid}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/admin/family")
    async def admin_family(request: Request):
        """One family: name / status / note / value (whole beneficiary map)."""
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            fid, done = d["id"], []
            cur = v.reg.profile.families.get(fid)
            for f in ("name", "status", "note"):
                if f in d and d[f] is not None and (cur is None or str(d[f]) != str(getattr(cur, f))):
                    done.append(v.reg.set_family_override(fid, f, d[f], v.name))
            if "value" in d and d["value"] is not None:
                want = {k: float(x) for k, x in d["value"].items() if x not in (None, "")}
                if cur is None or want != {k: float(x) for k, x in cur.value.items()}:
                    done.append(v.reg.set_family_override(fid, "value", want, v.name))
            return "; ".join(done) or f"{fid}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/admin/aura/reset")
    async def admin_aura_reset(request: Request):
        def go(v, d):
            if not v.owner:
                raise ValueError("Owner only.")
            return v.reg.clear_aura_overrides(v.name, d.get("id") or None)
        return await run(request, go, officer=True)

    # ---- members: every member's characters and absences; officers edit them like their own Me page
    @app.get("/api/members")
    async def members(request: Request):
        v = await who(request, officer=True)
        reg = v.reg
        ms = sorted(reg.members.values(), key=lambda m: m.display_name.lower())
        privs = await asyncio.gather(*(privilege(reg, m.discord_id) if not m.test else asyncio.sleep(0, "test") for m in ms))
        today = reg.now_local().date().isoformat()
        rows = []
        for m, p in zip(ms, privs):
            chars = sorted(m.active(), key=lambda c: (not c.is_main, c.created_at))
            if not chars:
                continue
            rows.append({"uid": str(m.discord_id), "display_name": m.display_name, "verification": reg.verification(m.discord_id), "privilege": "test" if m.test else p,
                         "characters": [char_json(reg, c) for c in chars], "absences": [absence_json(a) for a in m.upcoming_absences(today)],
                         "asks": [{"roster": a["roster"], "answer": a.get("answer")} for a in m.placement_asks[-3:] if a.get("answer") is None]})
        return {"rows": rows, "members": len(reg.members), "tz": reg.config.timezone}

    @app.post("/api/members/save")
    async def members_save(request: Request):
        """One save for the whole table: per member — deletes, spec/offspec, names, new characters, main."""
        def go(v, d):
            reg, done = v.reg, []
            for row in d.get("rows") or []:
                uid = int(row["uid"])
                m = reg.members.get(uid)
                if not m:
                    continue
                for label in row.get("deletes") or []:
                    reg.delete_character(uid, label)
                    done.append(f"deleted {label}")
                want_main = None
                for c in row.get("characters") or []:
                    cls, spec, off = (c.get("cls") or "").strip(), (c.get("spec") or "").strip(), (c.get("offspec") or "").strip() or None
                    name, surname = (c.get("name") or "").strip(), (c.get("surname") or "").strip() or None
                    cur = next((x for x in m.active() if x.label == (c.get("label") or "")), None)
                    if cur is None:
                        if not cls or not spec:
                            continue
                        if name:
                            _, cur = reg.add_character(uid, m.display_name, name, cls, spec, off, bool(c.get("main")), surname=surname)
                            done.append(f"added {cur.label} for {m.display_name}")
                        else:
                            _, cur = reg.set_plan(uid, m.display_name, cls, spec, off, "main" if c.get("main") else "alt")
                            done.append(f"planned {cur.cls} {cur.spec} for {m.display_name}")
                    else:
                        if spec and (spec, off) != (cur.spec, cur.offspec):
                            reg.set_spec(uid, cur.label, spec, off)
                            done.append(f"{cur.label}: {spec}" + (f"/{off}" if off else ""))
                        if not cur.name and name:
                            _, cur = reg.name_character(uid, name, "main" if cur.is_main else "alt", surname=surname, label=cur.label)
                            done.append(f"named {cur.label}")
                    if c.get("main"):
                        want_main = cur.label
                m = reg.members.get(uid) or m
                if want_main and not (m.main and m.main.label == want_main):
                    reg.set_main(uid, want_main)
                    done.append(f"{m.display_name}: main is {want_main}")
            return "; ".join(done) or "no changes"
        return await run(request, go, officer=True)

    @app.post("/api/members/absence")
    async def members_absence(request: Request):
        """Officer records an absence for someone (same ripple as the member doing it)."""
        v, d = await body(request, officer=True)
        m0 = v.reg.members.get(int(d.get("uid") or 0))
        if not m0:
            return JSONResponse({"error": "unknown member"}, status_code=400)
        try:
            m, a = await asyncio.to_thread(v.reg.add_absence, m0.discord_id, d.get("start") or "", d.get("end") or None, d.get("reason") or None, v.name, m0.display_name)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.announce_absence(v.reg, m, a, v.name)
        return {"message": f"{m.display_name} away {a.start}" + (f" → {a.end}" if a.end != a.start else "")}

    @app.post("/api/members/absence/clear")
    async def members_absence_clear(request: Request):
        return await run(request, lambda v, d: (v.reg.clear_absence(int(d["uid"]), d["start"]) and f"cleared absence {d['start']}"), officer=True)

    # ---- ops, config (read-only)
    @app.get("/api/ops")
    async def ops(request: Request):
        v = await who(request, officer=True)
        st, prov, feed = bot.registries.store, bot.ctx.provider, getattr(bot, "feed", None)
        precedents = st.read_jsonl(Path(v.reg.key) / "precedents.jsonl")[-50:]
        ledger = st.read_jsonl(Path(v.reg.key) / "ledger.jsonl")[-100:]
        return {"head": st.head(), "push": st.push_enabled, "llm": prov.summary() if prov else "off", "feed": feed.status() if feed else "disabled",
                "up": int(time.time() - bot.started_at) if hasattr(bot, "started_at") else 0,
                "rows": [{"time": t, "level": lvl, "text": text} for t, lvl, text in reversed(bot.ops.recent)],
                "precedents": list(reversed(precedents)), "ledger": list(reversed(ledger))}

    @app.get("/api/config")
    async def config(request: Request):
        v = await who(request, officer=True)
        import yaml

        from ..discord_pool import CHANNEL_KINDS
        from ..policy import PolicyStore

        reg, cfg = v.reg, v.reg.config
        ps = PolicyStore(bot.registries.store, reg.key)
        chans = {c["id"]: c["name"] for c in bot.guild_channels(reg)}
        return {"yaml": yaml.safe_dump(cfg.model_dump(), sort_keys=False),
                "docs": {d: {"text": ps.read(d), "compiled": bool(ps.compiled(d)), "summary": ((ps.compiled(d) or {}).get("summary") if isinstance(ps.compiled(d), dict) else None)} for d in ("loot", "comp", "persona")},
                "channels": {k: {"id": str(getattr(cfg, attr)) if getattr(cfg, attr) else None, "name": chans.get(str(getattr(cfg, attr)))} for k, attr in CHANNEL_KINDS.items()},
                "guild_channels": bot.guild_channels(reg), "guild_roles": bot.guild_roles(reg),
                "settings": {"timezone": cfg.timezone, "ask_audience": cfg.ask_audience, "about": cfg.about or "", "officer_roles": list(cfg.officer_roles), "owner_id": str(cfg.owner_discord_id) if cfg.owner_discord_id else None},
                "test_bench": {"members": len(reg.test_members()), "runs": [e.key for e in bot.raids.store(reg).live() if (cfg.roster(e.team) or {}).get("test")]}, "owner": v.owner}

    @app.post("/api/admin/config")
    async def admin_config(request: Request):
        """Owner: one setting at a time — channel:<kind> (channel id or null), timezone, ask_audience, about, officer_roles (list)."""
        v, d = await body(request, officer=True)
        if not v.owner:
            return JSONResponse({"error": "Owner only."}, status_code=403)
        field, value = d.get("field") or "", d.get("value")
        try:
            if field.startswith("channel:"):
                msg = await bot.set_channel(v.reg, field[8:], int(value) if value else None, v.name)
            elif field == "officer_roles":
                roles = [str(r) for r in (value or []) if str(r).strip()]
                v.reg.config.officer_roles = sorted(set(roles))
                v.reg.save_config(f"officer roles: {v.reg.config.officer_roles} (by {v.name})")
                msg = "officer roles: " + (", ".join(v.reg.config.officer_roles) or "(none; Manage Server only)")
            elif field in ("timezone", "ask_audience", "about"):
                from .. import configops

                msg = await asyncio.to_thread(configops.apply, v.reg, configops.ConfigOp(op="set", path=field, value=str(value or "")), v.name, True)
            else:
                return JSONResponse({"error": f"unknown setting {field}"}, status_code=400)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {msg}")
        return {"message": msg}

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
