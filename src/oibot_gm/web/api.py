"""JSON API for the React front end (frontend/ → static/app, served at /app).

Same identity as the HTML pages (session cookie, guild membership as the whitelist). Mutations go through the
same Registry / raidcycle functions the Discord commands call. CSRF: every POST must carry
`X-Requested-With: oibot` — a header a cross-site form cannot set — and the session cookie is SameSite."""
from __future__ import annotations

import asyncio
import logging
import contextlib
import inspect
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TypeVar
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from .. import comp as comp_mod, raid_views as dr, raidcycle as rc, render
from ..registry import RAID_BOOL_FIELDS, RAID_HOURS_FIELDS, RAID_WEIGHT_DEFAULTS, SPLIT_POLICIES, RegistryError
from ..roster import coverage as cov_mod

HERE = Path(__file__).parent
APP_DIR = HERE / "static" / "app"
STARTED = time.time()  # fallback when the bot has no start stamp
from ..constants import ROLES  # noqa: E402 — the order role counts are shown in

M = TypeVar("M", bound=BaseModel)


# ---- request bodies (S8): the few POSTs that need a field use a model; a missing/bad field is a 400, not a 500
class LabelBody(BaseModel):
    label: str


class StartBody(BaseModel):
    start: str


class UidStartBody(BaseModel):
    uid: int
    start: str


class PlacementBody(BaseModel):
    roster: str
    answer: Optional[str] = None


class StrategyBody(BaseModel):
    strategy: str


class InstanceBody(BaseModel):
    instance: str


class IdBody(BaseModel):
    id: str


def parse(d: dict, model: type[M]) -> M:
    """Validate a JSON body against a model; a ValidationError becomes a 400 naming the field."""
    try:
        return model.model_validate(d)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ())) or "body"
        raise HTTPException(400, f"{loc}: {first.get('msg', 'invalid')}")


def as_int(value, what: str = "id") -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what} must be a number")


def need_owner(v) -> None:
    """Owner-only endpoints: 403 for everyone else (officers included)."""
    if not v.owner:
        raise HTTPException(403, "Owner only.")


log = logging.getLogger(__name__)


def install_api(app: FastAPI, bot, *, viewer, icon_url, privilege) -> None:
    solver_locks: dict[str, asyncio.Lock] = {}  # one per run key: two officers can't solve the same run at once (S4)

    @contextlib.asynccontextmanager
    async def solving(key: str):
        """Hold the run's solver lock for the block; a second caller gets a 409 instead of queueing."""
        lock = solver_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "the solver is already working on this run — try again in a moment")
        async with lock:
            yield

    async def who(request: Request, officer: bool = False):
        v = await viewer(request)
        if v is None:
            raise HTTPException(401, "login required")
        if officer and not v.officer:
            raise HTTPException(403, "Officers only.")
        return v

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        """Every mutation on the API is logged (who, what, status) — the file is the audit trail next to the ops feed."""
        response = await call_next(request)
        if request.method != "GET" and request.url.path.startswith("/api/"):
            try:
                v = await viewer(request)
            except Exception:  # noqa: BLE001 — not logged in / not a member: still worth a line
                v = None
            via = getattr(v, "via", "web")
            log.info("api %s %s uid=%s status=%s%s", request.method, request.url.path, getattr(v, "uid", None), response.status_code, "" if via == "web" else f" via={via}")
        return response

    # ---- resolvers: the MCP server (and any client) may name people and runs the way officers do
    def resolve_member(reg, ref, what: str = "member"):
        """A member from a Discord id, a display name or a character name (case-insensitive; a unique prefix or
        substring also works). Ambiguous → 400 listing the candidates; unknown → 400."""
        raw = str(ref if ref is not None else "").strip()
        if not raw:
            raise HTTPException(400, f"{what} required")
        if raw.lstrip("<@!>").isdigit():
            m = reg.members.get(int(raw.strip("<@!>")))
            if not m:
                raise HTTPException(400, "unknown member")
            return m
        n = raw.lower()
        exact = [m for m in reg.members.values() if m.display_name.lower() == n]
        if len(exact) == 1:
            return exact[0]
        hit = reg.find(raw)
        if hit:
            return hit[0]
        loose = [m for m in reg.members.values() if n in m.display_name.lower() or any(c.name and n in c.name.lower() for c in m.characters)]
        if len(loose) == 1:
            return loose[0]
        if loose:
            raise HTTPException(400, f"{raw}: which one? " + ", ".join(sorted(m.display_name for m in loose)[:12]))
        raise HTTPException(400, f"unknown member {raw}")

    def member_ref(d: dict):
        """The body names a member as `uid` (the site) or `member` (name / character / id)."""
        return d.get("member") if d.get("member") not in (None, "") else d.get("uid")

    def find_live(reg, rs, ref: str):
        """A live run from its key, its roster key, or the raid it is for (id or name; 'tonight's Barrow Deeps').
        Several live runs for that raid → 400 listing their keys; nothing live → 404."""
        key = (ref or "").strip()
        ev = rs.events.get(key) or rs.for_team(key)
        if ev and ev.state not in ("done", "cancelled"):
            return ev
        n = key.lower()
        raids = [rid for rid in reg.profile.raids if rid.lower() == n or reg.raid_def(rid).get("name", rid).lower() == n]
        if not raids:
            raids = [rid for rid in reg.profile.raids if n and (n in rid.lower() or n in reg.raid_def(rid).get("name", rid).lower())]
        evs = [e for e in rs.live() if e.instance in raids] if len(raids) == 1 else []
        if len(evs) == 1:
            return evs[0]
        if len(evs) > 1:
            raise HTTPException(400, f"several live runs for {reg.raid_def(raids[0]).get('name', raids[0])}: " + ", ".join(e.key for e in evs))
        if len(raids) > 1:
            raise HTTPException(400, f"{key}: which raid? " + ", ".join(raids))
        raise HTTPException(404, "that run is closed" if key in rs.events else f"no live run {key or '?'}" + (" (live: " + ", ".join(e.key for e in rs.live()) + ")" if rs.live() else ""))

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

    async def maybe_await(value):
        """Bot verbs are coroutines when they talk to Discord; registry verbs are plain. Callers don't care which."""
        return await value if inspect.isawaitable(value) else value

    def table_rows(rows) -> list[dict]:
        """The character table as the registry verb takes it: one dict per row, `main` and `slot` both present
        (the Me page sends `slot: main|alt`, the Members page sends `main: bool`)."""
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                raise HTTPException(400, "rows must be objects")
            main = bool(row.get("main")) if "main" in row else row.get("slot") == "main"
            out.append({**row, "main": main, "slot": "main" if main else "alt"})
        return out

    async def clear_absence(v, uid: int, start: str):
        """Registry clears it, the bot re-opens the sheets it had answered for them; the lines say what changed."""
        reg = v.reg
        m = reg.members.get(uid)
        if not m:
            raise HTTPException(400, "unknown member")
        try:
            a = await asyncio.to_thread(reg.clear_absence, uid, start, v.name)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        lines = list(await maybe_await(bot.absence_cleared(reg, m, a, v.name)) or [])
        head = f"{m.display_name} back: cleared {a.start}" + (f" → {a.end}" if a.end != a.start else "")
        await bot.ops.emit(reg.config, "info", f"[web] {v.name}: {head}" + (" — " + "; ".join(lines) if lines else ""))
        return {"message": "\n".join([head, *lines]), "lines": lines}

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
        grouping = reg.profile.comp_rules.get("grouping") or {}
        return {
            "guild": reg.config.name, "tz": reg.config.timezone,
            "classes": {c: {s: a.get("role") for s, a in specs.items()} for c, specs in reg.profile.classes.items()},
            "icons": {k: dict(reg.profile.icons.get(k, {})) for k in ("classes", "roles", "specs")},
            "raids": [{"id": rid, "name": rd.get("name", rid), "size": int(rd.get("size") or 20), "lockout_days": int(reg.raid_def(rid).get("lockout_days", 7))} for rid, rd in reg.profile.raids.items()],
            "viewer": {"uid": str(v.uid), "name": v.name, "officer": v.officer, "owner": v.owner},
            "labels": dict(rc.LABELS),
            # the vocabulary the pages used to hard-code (C3): answer statuses in order, split philosophies, role order,
            # per-group role caps from the profile, class colours from render.py (tuned for dark surfaces)
            "statuses": list(rc.STATUSES), "split_policies": list(SPLIT_POLICIES), "roles": list(ROLES),
            "group_caps": {"tank": int(grouping.get("tank_max_per_group", 2)), "healer": int(grouping.get("healer_max_per_group", 3))},
            "class_colours": dict(render.CLASS),
            "started_at": float(getattr(bot, "started_at", STARTED)),
        }

    def run_label(reg, rs, team_key: str) -> dict:
        """Placement asks are keyed by the run's roster key; people see the raid name and the start time instead (B3)."""
        evs = sorted((e for e in rs.live() if e.team == team_key), key=lambda e: e.starts_at)
        if not evs:
            return {"raid": team_key, "when": "", "key": None}
        ev = evs[0]
        return {"raid": reg.raid_def(ev.instance).get("name", ev.instance) if ev.instance else team_key, "when": t12(reg, ev.starts_at), "key": ev.key}

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
                           "rostered": bool(seat), "roster": (seat[0] + 1) if seat else None})
        primary, flex = reg.roles_of(m) if m else (None, [])
        today = reg.now_local().date().isoformat()
        return {
            "display_name": m.display_name if m else v.name, "registered": m is not None,
            "characters": [char_json(reg, c) for c in (m.active() if m else [])],
            "roles": {"primary": primary, "flex": list(flex)},
            "absences": [absence_json(a) for a in (m.upcoming_absences(today) if m else [])],
            "dm": not (m.dm_opt_out if m else False),
            "sheets": sheets,
            # `channel` says where the ask went: "dm" / "web" (DMs off, so the Me page is the only place to answer)
            "asks": [{"roster": a["roster"], "character": a["character"], "asked_at": a.get("asked_at"), "channel": a.get("channel"), **run_label(reg, rs, a["roster"])} for a in reg.open_placement_asks(v.uid)],
        }

    # ---- member self-service
    @app.post("/api/me/characters")
    async def me_characters(request: Request):
        """One save for the table (spec/offspec per existing row, names for planned rows, new rows added or planned):
        the same registry verb the Members page and the Discord commands use."""
        def go(v, d):
            return "; ".join(v.reg.save_character_table(v.uid, table_rows(d.get("rows")), v.name)) or "no changes"
        return await run(request, go)

    @app.post("/api/me/main")
    async def me_main(request: Request):
        return await run(request, lambda v, d: f"main is now {v.reg.set_main(v.uid, parse(d, LabelBody).label)[1].label}")

    @app.post("/api/me/character/delete")
    async def me_delete(request: Request):
        return await run(request, lambda v, d: f"deleted {v.reg.delete_character(v.uid, parse(d, LabelBody).label).label}")

    @app.post("/api/me/absence")
    async def me_absence(request: Request):
        v, d = await body(request)
        try:
            m, a = await asyncio.to_thread(v.reg.add_absence, v.uid, d.get("start") or "", d.get("end") or None, d.get("reason") or None, v.name, v.name)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.announce_absence(v.reg, m, a, v.name)
        return {"message": f"away {v.reg.span_label(a.start, a.end)}"}

    @app.post("/api/me/absence/clear")
    async def me_absence_clear(request: Request):
        """Back early: the absence goes, and the sheets it had answered No thanks on re-open for them (the lines say which)."""
        v, d = await body(request)
        return await clear_absence(v, v.uid, parse(d, StartBody).start)

    @app.post("/api/me/placement")
    async def me_placement(request: Request):
        v, d = await body(request)
        b = parse(d, PlacementBody)
        yes = b.answer == "yes"
        try:
            line = await asyncio.to_thread(v.reg.answer_placement, v.uid, b.roster, yes, v.name)
        except (RegistryError, ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "bad request"}, status_code=400)
        await bot.after_placement_answer(v.reg, v.uid, b.roster, yes, line)
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

    def seat_json(p, conf: dict, uids: dict, signed: dict | None = None) -> dict:
        c = conf.get(p.signup_name) or {}
        return {"display_name": p.signup_name, "character": p.character or p.signup_name, "cls": p.cls, "spec": p.spec, "role": p.role, "uid": c.get("uid") or uids.get(p.signup_name), "answer": c.get("answer"), "signed_at": (signed or {}).get(p.signup_name)}

    def board_json(reg, ev, rosters, conf_rows) -> dict:
        """The bank + groups the officers drag on: one entry per roster, groups of seats, aura summary per roster."""
        conf = {c["display_name"]: c for c in conf_rows}
        uids = {s.display_name: str(s.discord_id) for s in ev.signups.values()}
        signed = {s.display_name: s.updated_at for s in ev.signups.values()}  # when they answered; the bank sorts on it
        team = reg.config.team(ev.team) or {}
        size = int(team.get("size") or reg.raid_def(ev.instance).get("size") or 20)
        boards = []
        for i, r in enumerate(rosters):
            by = {p.signup_name: p for p in r.selected}
            boards.append({"n": i + 1, "rostered": len(r.selected), "synergy": r.synergy_value or None, "advisories": list(r.advisories[:6]),
                           "groups": [[seat_json(by[m], conf, uids) for m in g if m in by] for g in r.groups], "summary": roster_summary(reg, r)})
        bench = rosters[0].benched if rosters else []
        # joiners the full roster(s) left out (listed apart from the Bench answers), and whether the leftovers + bench +
        # pool could make another run: one line, no action
        live = ev.state not in ("done", "cancelled")
        left = rc.leftovers(reg, ev, rosters) if live else []
        another = rc.another_run_hint(reg, ev, rosters, rc.run_split_reason(reg, ev)) if left else None
        return {"n_groups": rc.groups_per_roster(reg, size), "group_size": int(reg.profile.comp_rules["group_size"]), "size": size,
                "bank": [seat_json(p, conf, uids, signed) for p in bench], "rosters": boards,
                "leftovers": [p.signup_name for p in left], "another": another}

    def split_json(reg, ev) -> dict:
        """How many runs the joiners support and, when the headcount allows more than the tank/healer minimums do, why
        (structured for the builder's icons, plus the plain line for MCP and the help context)."""
        rd = reg.raid_def(ev.instance)
        size = rc.run_size(reg, ev)
        players = rc.players_for(reg, ev)
        rb = reg.role_bounds(ev.instance, size)
        reason = rc.split_reason(reg, players, size, rb)
        return {"strategy": ev.split_strategy or rd.get("split_policy", "balanced"), "policy": rd.get("split_policy", "balanced"),
                "runs": rc.how_many_rosters(reg, players, size, rb), "reason": reason, "reason_text": rc.split_reason_text(reason) if reason else None}

    def ev_json(reg, rs, ev, full: bool = True) -> dict:
        t = reg.config.team(ev.team) or {"key": ev.team, "size": reg.raid_def(ev.instance).get("size", 20)}
        live = ev.state not in ("done", "cancelled")
        rd = reg.raid_def(ev.instance)
        day = ev.start.astimezone(reg.tz).date().isoformat()
        base = {"key": ev.key, "run": ev.team, "name": t.get("name") or t["key"], "size": rc.run_size(reg, ev), "instance": ev.instance, "raid": rd.get("name", ev.instance),
                "starts_at": ev.starts_at, "when": t12(reg, ev.starts_at), "rel": rel(reg, ev.start), "state": ev.state, "live": live, "fill_state": ev.fill_state,
                "counts": {st: len(ev.by_status(st)) for st in rc.STATUSES}, "rostered": len(ev.seated()) if ev.all_rosters else 0, "n_rosters": len(ev.all_rosters)}
        if not full:
            return base
        from ..raid_views import run_times

        soft, hard, confirm = run_times(reg, ev, t)
        conf_rows = rc.confirmations(reg, ev) if ev.all_rosters else []
        board_rosters = ev.all_rosters if ev.state != "open" and ev.all_rosters else rc.board_rosters(reg, ev, ev.layout or [], rc.run_size(reg, ev))
        absences = [{"display_name": m.display_name, "start": a.start, "end": a.end, "reason": a.reason, "signed": str(m.discord_id) in ev.signups}
                    for m in reg.members.values() for a in m.absences if a.start <= day <= a.end]
        busy = rc.conflicts(rs, ev) if live else {}
        return {**base,
                "timeline": {"nudge": t12(reg, soft), "lock": t12(reg, hard), "confirm": clock12(reg, confirm) if confirm.date() == ev.start.astimezone(reg.tz).date() else t12(reg, confirm), "locked_at": clock12(reg, ev.locked_at) if ev.locked_at else None},
                "signups": [signup_json(s, ev.pins) for s in sorted(ev.signups.values(), key=lambda s: (rc.STATUSES.index(s.status) if s.status in rc.STATUSES else 9, s.updated_at))],
                "not_answered": [{"uid": str(m.discord_id), "display_name": m.display_name, "character": m.main.label, "cls": m.main.cls, "spec": m.main.spec, "role": reg.profile.spec(m.main.cls, m.main.spec).role}
                                 for m in reg.members.values() if m.main and str(m.discord_id) not in ev.signups] if live else [],
                "absences": absences, "double_booked": [reg.members[u].display_name for u in busy if u in reg.members],
                "needs": rc.needs(reg, ev, t) if live else None,
                "board": board_json(reg, ev, board_rosters, conf_rows), "has_layout": bool(ev.layout), "rev": rc.board_rev(reg, ev),
                "split": split_json(reg, ev) if live else {"strategy": ev.split_strategy or rd.get("split_policy", "balanced"), "policy": rd.get("split_policy", "balanced"), "runs": 1, "reason": None, "reason_text": None},
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
                        for slot, start in rc.slot_starts(reg, rid, now, 24 * rc.OPEN_HORIZON_DAYS) if f"{rc.run_key(rid, start)}-{start.date().isoformat()}" not in rs.events][:6]
            out.append({"id": rid, "name": rd.get("name", rid), "size": int(rd.get("size") or 20), "slots": list(rd["slots"]), "slot_labels": [reg.slot_label(x) for x in rd["slots"]], "lockout_days": int(rd["lockout_days"]),
                        "opened": bool(fo and fo <= now), "first_open": reg.local12(fo, "%a %d %b %Y %I:%M %p") if fo else None,
                        "open": [e for e in live if e["state"] == "open"], "locked": [e for e in live if e["state"] != "open"], "past": past, "upcoming": upcoming})
        orphans = [ev_json(reg, rs, e, full=False) for e in events if e.instance not in reg.profile.raids and e.state not in ("done", "cancelled")][:6]
        return {"raids": out, "orphans": orphans, "tz": reg.config.timezone}

    @app.get("/api/rosters")
    async def rosters(request: Request):
        v = await who(request, officer=True)
        return await asyncio.to_thread(rosters_data, v.reg)

    def live_event(reg, key: str):
        rs = bot.raids.store(reg)
        ev = find_live(reg, rs, key)
        return rs, ev, (rc.run_team(reg, ev))

    @app.get("/api/run/{key}")
    async def run_get(request: Request, key: str):
        """One run in full (the sheet, board, confirmations, fill asks, absences that day, log). Closed runs too, by key."""
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None:
            ev = find_live(v.reg, rs, key)
        return await asyncio.to_thread(ev_json, v.reg, rs, ev)

    def solver_text(e: Exception) -> str:
        """A RuntimeError from the solver as one sentence an officer can act on (H2)."""
        fn = getattr(rc, "solver_error_text", None)
        s = fn(e) if fn else ("no roster satisfies the rules: check the raid's tank/healer minimums against who joined, pins that overfill a group, and keep-apart pairs" if ("INFEASIBLE" in str(e) or "no roster found" in str(e)) else f"the solver failed: {e}")
        return s[:1].upper() + s[1:]

    def board_reply(reg, ev):
        t = rc.run_team(reg, ev)
        rosters = ev.all_rosters if ev.state != "open" and ev.all_rosters else rc.board_rosters(reg, ev, ev.layout or [], rc.run_size(reg, ev))
        return {"board": board_json(reg, ev, rosters, rc.confirmations(reg, ev) if ev.all_rosters else []), "needs": rc.needs(reg, ev, t), "rev": rc.board_rev(reg, ev)}

    board_locks: dict[str, asyncio.Lock] = {}

    def board_lock(key: str) -> asyncio.Lock:
        """One writer at a time per run's board: concurrent officers queue here (short), they never overwrite each other."""
        return board_locks.setdefault(key, asyncio.Lock())

    async def save_board(v, rs, ev, t, groups: list[list[str]]):
        """The one way a board is written (whole-board or after a single move)."""
        names = {s.display_name for s in ev.signups.values() if s.status == "in"}
        groups = [[n for n in g if n in names] for g in groups]
        if ev.state == "open":
            ev.layout = groups if any(groups) else None
            await asyncio.to_thread(rs.save, ev, f"board ({v.name})")
            return board_reply(v.reg, ev)
        added, removed = await asyncio.to_thread(rc.apply_layout_locked, v.reg, rs, ev, groups)
        await bot.after_board_change(v.reg, rs, ev, t, added, removed, v.name)
        return {**board_reply(v.reg, ev), "message": (f"asked {', '.join(s.display_name for s in added)} to confirm" if added else "") + (f"; freed {', '.join(removed)}" if removed else "") or "groups updated"}

    def stale(v, ev):
        return JSONResponse({"error": "Someone else changed this board a moment ago — it has been reloaded; check it and try again.", **board_reply(v.reg, ev)}, status_code=409)

    @app.post("/api/run/{key}/layout")
    async def run_layout(request: Request, key: str):
        """The board after a drag: before lock it is saved as the layout the lock will use; after lock it is the roster
        (group moves are free, someone dragged in from the bench is asked to confirm, someone dragged out is freed)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        groups = [[str(n) for n in g] for g in (d.get("groups") or [])]
        async with board_lock(key):
            # a whole-board write (Clear, Use this split) is only safe on the board its author was looking at
            if d.get("rev") and d["rev"] != rc.board_rev(v.reg, ev):
                return stale(v, ev)
            return await save_board(v, rs, ev, t, groups)

    @app.post("/api/run/{key}/move")
    async def run_move(request: Request, key: str):
        """One drag, applied to the board as it is NOW: {member, to: {r, g, i} | null (= bank)}. Two officers dragging
        at once merge; nothing is overwritten. Same effects as a whole-board write after lock (confirm DM / seat freed)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        name, to = str(d.get("member") or ""), d.get("to")
        async with board_lock(key):
            n = rc.groups_per_roster(v.reg, rc.run_size(v.reg, ev))
            try:
                target = None if not to else (as_int(to.get("r"), "to.r") * n + as_int(to.get("g"), "to.g"), as_int(to.get("i", 99), "to.i"))
                groups = rc.apply_move(v.reg, ev, name, target)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:1].upper() + str(e)[1:], **board_reply(v.reg, ev)}, status_code=409)
            return await save_board(v, rs, ev, t, groups)

    # ---- live updates: one event stream per open page; a run save anywhere (site, Discord, scheduler) wakes them
    subscribers: set[asyncio.Queue] = set()

    def publish(topic: str) -> None:
        """topic: `run:<key>` | `member` | `config` | `ops`. Safe from worker threads."""
        def fan_out():
            for q in list(subscribers):
                if q.qsize() < 50:
                    q.put_nowait(topic)
        try:
            bot.loop.call_soon_threadsafe(fan_out)
        except Exception:  # noqa: BLE001 — loop not running yet / shutting down
            pass

    def run_saved(guild_key: str, run_key: str) -> None:
        publish(f"run:{run_key}")

    def reg_saved(reg, kind: str, lines) -> None:
        publish(kind)

    def ops_line() -> None:
        publish("ops")

    from .. import ops as ops_mod, registry as registry_mod

    for hooks, fn in ((rc.RUN_LISTENERS, run_saved), (registry_mod.SAVE_LISTENERS, reg_saved), (ops_mod.LISTENERS, ops_line)):
        hooks[:] = [f for f in hooks if getattr(f, "__name__", "") != fn.__name__] + [fn]  # one per process, the newest app wins

    @app.get("/api/events")
    async def events(request: Request):
        """Server-sent events: `data: run:<key> | member | config | ops` whenever that changes. Pages reload on the topics they show."""
        from fastapi.responses import StreamingResponse

        await who(request)
        q: asyncio.Queue = asyncio.Queue()
        subscribers.add(q)

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        key = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"data: {key}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # keeps the tunnel from idling the connection out
            finally:
                subscribers.discard(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/run/{key}/split")
    async def run_split(request: Request, key: str):
        """Preview a fresh layout (nothing saved): one roster, or a split under a strategy. `avoid` = previous previews to differ from."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked"}, status_code=400)
        strategy = d.get("strategy") or ev.split_strategy or v.reg.raid_def(ev.instance)["split_policy"]
        if strategy not in SPLIT_POLICIES:
            return JSONResponse({"error": f"strategy must be one of {', '.join(SPLIT_POLICIES)}"}, status_code=400)
        avoid = d.get("avoid") if isinstance(d.get("avoid"), list) else None
        async with solving(key):
            try:
                layout, rosters = await asyncio.to_thread(rc.split_preview, v.reg, rs, ev, strategy, avoid or None)
            except RuntimeError as e:
                return JSONResponse({"error": solver_text(e)}, status_code=409)
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"solver: {e}"}, status_code=400)
        syn = [r.synergy_value for r in rosters]
        sp = split_json(v.reg, ev)  # why there aren't more rosters, when the roles are what holds it back
        return {"strategy": strategy, "layout": layout, "board": board_json(v.reg, ev, rosters, []), "synergy": syn, "total": sum(syn), "gap": (max(syn) - min(syn)) if syn else 0,
                "reason": sp["reason"], "reason_text": sp["reason_text"]}

    @app.post("/api/run/{key}/strategy")
    async def run_strategy(request: Request, key: str):
        """Remember the split philosophy on the run (Auto-fill and the scheduled lock use it)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        b = parse(d, StrategyBody)
        if b.strategy not in SPLIT_POLICIES:
            return JSONResponse({"error": "unknown strategy"}, status_code=400)
        async with solving(key):
            ev.split_strategy = b.strategy
            rs.save(ev, f"split strategy {b.strategy}")
        return {"message": f"split strategy: {b.strategy}"}

    @app.post("/api/run/{key}/autofill")
    async def run_autofill(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked — move people on the board instead"}, status_code=400)
        async with solving(key):
            try:
                layout = await asyncio.to_thread(rc.autofill, v.reg, rs, ev)
            except RuntimeError as e:
                return JSONResponse({"error": solver_text(e)}, status_code=409)
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
        """Pin someone to the roster / keep them on the bench for the lock — the same verb the board's menu uses."""
        pin = d.get("pin")
        uid = str(resolve_member(v.reg, member_ref(d)).discord_id)
        if pin not in ("in", "out", None):
            return JSONResponse({"error": "pin must be in, out or null"}, status_code=400)
        if uid not in ev.signups:
            return JSONResponse({"error": "they haven't answered this sheet"}, status_code=400)
        name = ev.signups[uid].display_name
        try:
            rc.set_pin(ev, name, pin)  # writes the log line; the caller saves
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        rs.save(ev, f"pin {name} {pin} by {v.name}")
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {ev.key} {name} {'pinned ' + pin if pin else 'unpinned'}")
        return {"message": f"{name}: {'pinned ' + pin if pin else 'unpinned'}"}

    @app.post("/api/run/{key}/set")
    async def run_set(request: Request, key: str):
        """Officer sets someone's answer (Join / Bench / No thanks) or swaps their character — the board's verb, so
        Join after lock seats them and asks them to confirm, No thanks after lock frees the seat and fills it."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        m = resolve_member(v.reg, member_ref(d))
        status = d.get("status")
        if status not in rc.STATUSES:
            return JSONResponse({"error": f"status must be one of {', '.join(rc.STATUSES)}"}, status_code=400)
        try:  # set_answer refreshes the sheet and cards and writes the ops line itself; its reply is Discord-flavoured
            line = await maybe_await(bot.set_answer(v.reg, rs, ev, m, status, d.get("character") or None, v.name))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line.replace("**", "").removeprefix("✅ ")}

    @app.post("/api/run/{key}/lock")
    async def run_lock(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked"}, status_code=400)
        async with solving(key):
            try:
                line = await bot.lock_run(v.reg, rs, ev, by=v.name)
            except RuntimeError as e:  # lock_run catches the solver itself; this is the belt to its braces
                return JSONResponse({"error": solver_text(e)}, status_code=409)
        if ev.lock_error:
            return JSONResponse({"error": solver_text(RuntimeError(ev.lock_error))}, status_code=409)
        await bot.ops.emit(v.reg.config, "info", f"[web] {line}")
        return {"message": line}

    def ask_json(reg, a) -> dict:
        found = reg.find(a.character)
        return {"display_name": a.display_name, "kind": a.kind, "character": a.character, "spec": a.spec, "cls": found[1].cls if found else None, "role": a.role, "reason": a.reason, "pair": a.pair}

    @app.post("/api/run/{key}/fill/preview")
    async def run_fill_preview(request: Request, key: str):
        """What Fill seats would send, without sending: the shortfall, who is still being waited on, and the next batch (D2)."""
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state == "open":
            return JSONResponse({"error": "the fill engine works after lock — before that, shape the board"}, status_code=400)
        nd = rc.needs(v.reg, ev, t)
        batch = await asyncio.to_thread(rc.fill_batch, v.reg, rs, ev, t) if (nd["headcount"] or nd["roles"]) else []
        return {"short": nd, "waiting": [a.display_name for a in ev.fill_asks if a.open], "batch": [ask_json(v.reg, a) for a in batch]}

    @app.post("/api/run/{key}/fill")
    async def run_fill(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state == "open":
            return JSONResponse({"error": "the fill engine works after lock — before that, shape the board"}, status_code=400)
        sent, nd = await bot.run_fill(v.reg, rs, ev, t, by=v.name)
        if sent:
            await bot.post_run_update(v.reg, ev, "🧩 " + "\n🧩 ".join(dr.ask_line(v.reg, bot.ico, a) for a in sent))
        return {"message": ("asked " + ", ".join(f"{a.display_name} ({'swap' if a.swap else a.kind})" for a in sent)) if sent else ("nothing to fill" if not (nd["headcount"] or nd["roles"]) else "nobody left to ask")}

    @app.post("/api/run/{key}/cancel")
    async def run_cancel(request: Request, key: str):
        """Cancel the run the way /raid cancel does (sheet closed, everyone on it told); the line says what was done."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        try:  # cancel_run re-renders the sheet and cards, withdraws open confirmations and writes the ops line itself
            line = await maybe_await(bot.cancel_run(v.reg, rs, ev, v.name, (d.get("reason") or "").strip() or None))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    @app.post("/api/raid/{rid}/open")
    async def raid_open(request: Request, rid: str):
        """Open a sheet now: the raid's next slot, or a one-off 'YYYY-MM-DD HH:MM' in guild time."""
        v, d = await body(request, officer=True)
        reg = v.reg
        if rid not in reg.profile.raids:
            return JSONResponse({"error": "unknown raid"}, status_code=400)
        now, rd = reg.now_local(), reg.raid_def(rid)
        when = (d.get("when") or "").strip()
        try:
            if when:
                start = datetime.fromisoformat(when.replace(" ", "T", 1)).replace(tzinfo=ZoneInfo(reg.config.timezone))
            else:
                nxt = rc.slot_starts(reg, rid, now, 24 * rc.OPEN_HORIZON_DAYS)
                if not nxt:
                    fo = reg.first_open(rid)
                    why = (f"its slots all fall before it opens on {reg.local12(fo)}" if rd["slots"] and fo and fo > now
                           else "it has no recurring slots yet")
                    return JSONResponse({"error": f"No next slot for this raid: {why}. Pick a date and time instead, or set it up on the Raids page."}, status_code=400)
                start = nxt[0][1]
        except ValueError:
            return JSONResponse({"error": "that date and time didn't parse — pick one with the picker"}, status_code=400)
        if start < now - timedelta(minutes=5):  # a mistyped year would open a run the scheduler may close at once
            return JSONResponse({"error": f"{reg.local12(start)} is in the past — pick a time from now on"}, status_code=400)
        rs = bot.raids.store(reg)
        try:  # what /raid open does: open (or find) the run, post the sheet, write the ops line
            ev = await maybe_await(bot.open_run_and_post(reg, rs, rid, start, v.name))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": f"opened {ev.key}" + ("" if ev.message_id else " (no signup channel set — sheet not posted)")}

    @app.post("/api/admin/confirm")
    async def admin_confirm(request: Request):
        return await run(request, lambda v, d: f"confirmed {v.reg.confirm(parse(d, LabelBody).label, v.name)[1].label}", officer=True)

    @app.post("/api/admin/placement")
    async def admin_placement(request: Request):
        """Officer answers a Confirm / Can't make it ask on someone's behalf (what the test bench's may_answer_for
        allows on the buttons): same verb and ripple as the member pressing it."""
        v, d = await body(request, officer=True)
        m = resolve_member(v.reg, member_ref(d))
        rs, ev, t = live_event(v.reg, str(d.get("run") or d.get("roster") or ""))
        yes = d.get("answer") in ("yes", True, "true")
        try:
            line = await asyncio.to_thread(v.reg.answer_placement, m.discord_id, ev.team, yes, v.name)
        except (RegistryError, ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "bad request"}, status_code=400)
        await bot.after_placement_answer(v.reg, m.discord_id, ev.team, yes, line)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name} answered for {m.display_name}: {line}")
        return {"message": line}

    @app.post("/api/admin/test")
    async def admin_test(request: Request):
        """The test bench over HTTP — the same steps as /gm test seed|run|answer|clear (discord_registry.py)."""
        v, d = await body(request, officer=True)
        reg, action = v.reg, str(d.get("action") or "")
        rs = bot.raids.store(reg)
        if action == "seed":
            made = await asyncio.to_thread(reg.seed_test_members, as_int(d.get("count", 20), "count"), v.name)
            total = len(reg.test_members())
            await bot.ops.emit(reg.config, "warn", f"test bench: {len(made)} test members seeded by {v.name} ({total} total)")
            return {"message": f"{len(made)} test member(s) created, {total} in total: " + ", ".join(f"{m.display_name} ({m.main.cls} {m.main.spec})" for m in reg.test_members()[:30])}
        if action == "run":
            rid = str(d.get("raid") or "")
            if rid not in reg.profile.raids:
                return JSONResponse({"error": f"unknown raid; options: {', '.join(reg.profile.raids)}"}, status_code=400)
            start_in, lock_in, confirm_in, nudge_in = (as_int(d.get(k, dflt), k) for k, dflt in (("start_in", 40), ("lock_in", 25), ("confirm_in", 15), ("nudge_in", 32)))
            if not (start_in > lock_in > confirm_in >= 0) or nudge_in <= lock_in:
                return JSONResponse({"error": "need start_in > nudge_in > lock_in > confirm_in ≥ 0 (all minutes)"}, status_code=400)
            start = (reg.now_local() + timedelta(minutes=start_in)).replace(second=0, microsecond=0)
            ev = await asyncio.to_thread(rc.open_run, reg, rs, rid, start, by=v.name, cutoffs={"soft": nudge_in / 60, "hard": lock_in / 60, "confirm": confirm_in / 60, "open_dm": bool(d.get("dm_open")), "test_by": v.uid})
            channel = bot.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None
            if not ev.message_id and channel is not None:
                await bot.post_sheet(reg, rs, ev, channel)
            await bot.ops.emit(reg.config, "warn", f"test bench: {v.name} opened test run {ev.key} (lock in {start_in - lock_in} min)")
            return {"message": f"test run {ev.key} open: starts {t12(reg, ev.starts_at)}, nudge {nudge_in} min before, lock {lock_in} min before, confirm by {confirm_in} min before" + ("" if ev.message_id else " (no signup channel set — sheet not posted)"), "key": ev.key}
        if action == "answer":
            ev = find_live(reg, rs, str(d.get("run") or "")) if d.get("run") else next(iter(rs.live()), None)
            if not ev:
                return JSONResponse({"error": "no live run"}, status_code=400)
            team = reg.config.roster(ev.team) or {"key": ev.team, "size": 20}
            done = []
            if d.get("member"):
                m, status = resolve_member(reg, d.get("member")), str(d.get("status") or "in")
                if not m.test:
                    return JSONResponse({"error": "pick a test member"}, status_code=400)
                if status not in rc.STATUSES:
                    return JSONResponse({"error": f"status must be one of {', '.join(rc.STATUSES)}"}, status_code=400)
                if ev.state != "open" and status == "out" and ev.seat_of(m.display_name):
                    await bot.drop_seated(reg, rs, ev, team, m, "callout (test)", None)
                    done.append(f"{m.display_name} called out")
                else:
                    s = rc.set_signup(reg, rs, ev, m, None, status, source="test")
                    done.append(f"{m.display_name} {rc.LABELS[s.status]}")
            else:
                import random

                pool = [m for m in reg.test_members() if str(m.discord_id) not in ev.signups]
                random.shuffle(pool)
                for st, n in (("in", as_int(d.get("join", 0), "join")), ("sub", as_int(d.get("bench", 0), "bench")), ("out", as_int(d.get("out", 0), "out"))):
                    for _ in range(n):
                        if not pool:
                            break
                        m = pool.pop()
                        rc.set_signup(reg, rs, ev, m, None, st, source="test")
                        done.append(f"{m.display_name} {rc.LABELS[st]}")
            await bot.refresh_sheet(reg, ev)
            await bot.ops.emit(reg.config, "info", f"test bench: {len(done)} answer(s) on {ev.key} by {v.name}")
            return {"message": ("; ".join(done)) if done else "nobody left to answer (seed more, or they've all answered)", "key": ev.key}
        if action == "clear":
            line = await bot.test_bench_clear(reg, v.name)
            await bot.ops.emit(reg.config, "warn", f"test bench: {line} (by {v.name})")
            return {"message": line}
        return JSONResponse({"error": "action must be seed, run, answer or clear"}, status_code=400)

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
                        "slots": list(eff["slots"]), "slot_labels": [reg.slot_label(x) for x in eff["slots"]], "signup_lead_hours": eff["signup_lead_hours"], "lock_hours_before": eff["lock_hours_before"], "confirm_hours_before": eff["confirm_hours_before"],
                        "weights": dict(eff["weights"]), "split_policy": eff.get("split_policy", "balanced"), "nudge": bool(eff.get("nudge", True)), "nudge_hours_before": eff["nudge_hours_before"], "fill_ask_hours": eff["fill_ask_hours"], "autofill": bool(eff.get("autofill", True)), "open_dm": bool(eff.get("open_dm", False)), "notes": eff.get("notes") or "", "comp": {r: dict((eff.get("comp") or {}).get(r) or {}) for r in ("tank", "healer", "dps")},
                        "overridden": sorted(k for k in over if k not in ("comp", "weights")) + [f"{r}_{b}" for r, bb in ((over.get("comp") or {}).items()) for b in bb] + [f"weight_{k}" for k in (over.get("weights") or {})],
                        "comp_targets": over.get("comp_targets") or {}, "comp_groups": over.get("comp_groups") or [],
                        "first_open_local": fo.astimezone(z).strftime("%Y-%m-%dT%H:%M") if fo else "", "opened": bool(fo and fo <= now),
                        "window": [reg.local12(ws), reg.local12(we)], "live": sum(1 for e in rs.live() if e.instance == rid)})
        return {"raids": out, "tz": reg.config.timezone, "owner": v.owner, "weight_keys": list(RAID_WEIGHT_DEFAULTS), "split_policies": list(SPLIT_POLICIES)}

    @app.post("/api/admin/raid")
    async def admin_raid(request: Request):
        def go(v, d):
            need_owner(v)
            inst = parse(d, InstanceBody).instance
            cur, want = v.reg.raid_def(inst), {}  # collect every changed field, then ONE atomic write
            if "slots" in d:
                raw = d["slots"] if isinstance(d["slots"], list) else str(d["slots"])
                from ..registry import parse_slots

                if parse_slots(raw) != list(cur["slots"]):
                    want["slots"] = raw
            for f in RAID_HOURS_FIELDS + RAID_BOOL_FIELDS + ("lockout_days", "duration_hours", "notes", "split_policy"):
                val = d.get(f)
                if val not in (None, "") and str(val) != str(cur.get(f, "")):
                    want[f] = str(val)
            if "nudge" in d and d["nudge"] is not None and bool(d["nudge"]) != bool(cur.get("nudge", True)):
                want["nudge"] = "true" if d["nudge"] else "false"
            for k, val in (d.get("weights") or {}).items():
                if k in RAID_WEIGHT_DEFAULTS and val not in (None, "") and as_int(val, f"weight {k}") != int(cur["weights"].get(k, 0)):
                    want[f"weight_{k}"] = str(val)
            fo = d.get("first_open") or ""
            cur_fo = v.reg.first_open(inst)
            if fo and (cur_fo is None or fo[:16] != cur_fo.astimezone(ZoneInfo(v.reg.config.timezone)).strftime("%Y-%m-%dT%H:%M")):
                want["first_open"] = fo
            for role in ("tank", "healer", "dps"):
                for bound in ("min", "max"):
                    val = (d.get("comp") or {}).get(role, {}).get(bound)
                    if val not in (None, "") and str(val) != str(((cur.get("comp") or {}).get(role) or {}).get(bound, "")):
                        want[f"{role}_{bound}"] = str(val)
            done = v.reg.set_raid_overrides(inst, want, v.name)  # a refused field leaves the raid exactly as it was
            return "; ".join(done) or f"{inst}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/admin/raid/reset")
    async def admin_raid_reset(request: Request):
        def go(v, d):
            need_owner(v)
            return v.reg.clear_raid_override(parse(d, InstanceBody).instance, v.name)
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
            need_owner(v)
            bid, done = parse(d, IdBody).id, []
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
            need_owner(v)
            fid, done = parse(d, IdBody).id, []
            cur = v.reg.profile.families.get(fid)
            for f in ("name", "status", "note"):
                if f in d and d[f] is not None and (cur is None or str(d[f]) != str(getattr(cur, f))):
                    done.append(v.reg.set_family_override(fid, f, d[f], v.name))
            if "value" in d and d["value"] is not None:
                if not isinstance(d["value"], dict):
                    raise HTTPException(400, "value must be a map of beneficiary → number")
                try:
                    want = {k: float(x) for k, x in d["value"].items() if x not in (None, "")}
                except (TypeError, ValueError):
                    raise HTTPException(400, "value entries must be numbers")
                if cur is None or want != {k: float(x) for k, x in cur.value.items()}:
                    done.append(v.reg.set_family_override(fid, "value", want, v.name))
            return "; ".join(done) or f"{fid}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/admin/aura/reset")
    async def admin_aura_reset(request: Request):
        def go(v, d):
            need_owner(v)
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
        rs = bot.raids.store(reg)
        rows = []
        for m, p in zip(ms, privs):
            chars = sorted(m.active(), key=lambda c: (not c.is_main, c.created_at))
            if not chars:
                continue
            rows.append({"uid": str(m.discord_id), "display_name": m.display_name, "verification": reg.verification(m.discord_id), "privilege": "test" if m.test else p,
                         "characters": [char_json(reg, c) for c in chars], "absences": [absence_json(a) for a in m.upcoming_absences(today)],
                         "asks": [{"roster": a["roster"], "answer": a.get("answer"), **run_label(reg, rs, a["roster"])} for a in m.placement_asks[-3:] if a.get("answer") is None]})
        return {"rows": rows, "members": len(reg.members), "tz": reg.config.timezone}

    @app.post("/api/members/save")
    async def members_save(request: Request):
        """One save for the whole table: per member — deletes, then the same table verb the Me page uses
        (spec/offspec, names, new characters, main)."""
        def go(v, d):
            reg, done = v.reg, []
            for row in d.get("rows") or []:
                if not isinstance(row, dict):
                    raise HTTPException(400, "rows must be objects")
                if row.get("member"):  # the MCP server names people; the site sends ids
                    m = resolve_member(reg, row["member"])
                else:
                    m = reg.members.get(as_int(row.get("uid"), "uid"))
                if not m:
                    continue
                uid = m.discord_id
                rows = [{"label": str(label), "delete": True} for label in row.get("deletes") or []] + table_rows(row.get("characters"))
                if rows:
                    done.extend(f"{line} ({m.display_name})" for line in reg.save_character_table(uid, rows, v.name))
            return "; ".join(done) or "no changes"
        return await run(request, go, officer=True)

    @app.get("/api/member")
    async def member_get(request: Request, name: str = ""):
        """One member by display name, character name or id (`?name=`): characters, every absence on file, open asks, DM setting."""
        v = await who(request, officer=True)
        reg = v.reg
        m = resolve_member(reg, name)
        rs = bot.raids.store(reg)
        primary, flex = reg.roles_of(m)
        return {"uid": str(m.discord_id), "display_name": m.display_name, "verification": reg.verification(m.discord_id), "privilege": "test" if m.test else await privilege(reg, m.discord_id),
                "test": bool(m.test), "dm": not m.dm_opt_out, "roles": {"primary": primary, "flex": list(flex)},
                "characters": [char_json(reg, c) for c in sorted(m.active(), key=lambda c: (not c.is_main, c.created_at))],
                "absences": [absence_json(a) for a in sorted(m.absences, key=lambda a: a.start)],
                "asks": [{"roster": a["roster"], "answer": a.get("answer"), "asked_at": a.get("asked_at"), **run_label(reg, rs, a["roster"])} for a in m.placement_asks[-5:]],
                "sheets": [{"key": e.key, "status": e.signups[str(m.discord_id)].status, "character": e.signups[str(m.discord_id)].character} for e in rs.live() if str(m.discord_id) in e.signups]}

    @app.get("/api/absences")
    async def absences(request: Request, all: bool = False):
        """Every upcoming absence (`?all=true` = past ones too), newest start first, with the member it belongs to."""
        v = await who(request, officer=True)
        reg = v.reg
        today = reg.now_local().date().isoformat()
        rows = [{"uid": str(m.discord_id), "display_name": m.display_name, "start": a.start, "end": a.end, "reason": a.reason, "by": a.by, "current": a.start <= today <= a.end}
                for m in reg.members.values() for a in (m.absences if all else m.upcoming_absences(today))]
        return {"rows": sorted(rows, key=lambda r: (r["start"], r["display_name"])), "today": today, "tz": reg.config.timezone}

    @app.post("/api/members/set")
    async def members_set(request: Request):
        """Officer edits one member's record the way the Discord commands do: `rank` (on `character`, default the main),
        `confirm` (true: the character is verified), `main` (a character name becomes the main). Any subset of the three."""
        def go(v, d):
            reg, done = v.reg, []
            m = resolve_member(reg, member_ref(d))
            char = str(d.get("character") or "").strip() or (m.main.label if m.main else "")
            if d.get("main"):
                name = str(d["main"]).strip() if isinstance(d["main"], str) else char
                mm, c, old = reg.officer_set_main(m.discord_id, name, v.name)
                done.append(f"{m.display_name}: main is now {c.label}")
                char = str(d.get("character") or "").strip() or c.label
            if d.get("rank"):
                if not char:
                    raise HTTPException(400, "character required (no main to default to)")
                mm, c = reg.set_rank(char, str(d["rank"]), v.name)
                done.append(f"{c.label}: rank {c.rank}")
            if d.get("confirm"):
                if not char:
                    raise HTTPException(400, "character required (no main to default to)")
                hit = reg.find(char)
                if hit and hit[1].confirmed_by:  # a "set" is declarative: already confirmed is already done
                    done.append(f"{hit[1].label} already confirmed by {hit[1].confirmed_by}")
                else:
                    mm, c = reg.confirm(char, v.name)
                    done.append(f"confirmed {c.label}")
            return "; ".join(done) or f"{m.display_name}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/members/dm")
    async def members_dm(request: Request):
        """Officer switches someone's DMs on/off (the Me page's toggle, for them)."""
        def go(v, d):
            m = resolve_member(v.reg, member_ref(d))
            m.dm_opt_out = not bool(d.get("on"))
            v.reg.save(m, f"{m.display_name} DMs {'off' if m.dm_opt_out else 'on'} (by {v.name})")
            return f"{m.display_name}: DMs {'off' if m.dm_opt_out else 'on'}"
        return await run(request, go, officer=True)

    @app.post("/api/members/absence")
    async def members_absence(request: Request):
        """Officer records an absence for someone (same ripple as the member doing it)."""
        v, d = await body(request, officer=True)
        m0 = resolve_member(v.reg, member_ref(d))
        try:
            m, a = await asyncio.to_thread(v.reg.add_absence, m0.discord_id, d.get("start") or "", d.get("end") or None, d.get("reason") or None, v.name, m0.display_name)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.announce_absence(v.reg, m, a, v.name)
        return {"message": f"{m.display_name} away {v.reg.span_label(a.start, a.end)}"}

    @app.post("/api/members/absence/clear")
    async def members_absence_clear(request: Request):
        """Officer clears someone's absence: same ripple as the member doing it (sheets re-opened for them; the lines say which)."""
        v, d = await body(request, officer=True)
        m = resolve_member(v.reg, member_ref(d))
        return await clear_absence(v, m.discord_id, parse(d, StartBody).start)

    # ---- ops, config (read-only)
    @app.get("/api/ops")
    async def ops(request: Request):
        v = await who(request, officer=True)
        st, prov, feed = bot.registries.store, bot.ctx.provider, getattr(bot, "feed", None)
        precedents = st.read_jsonl(Path(v.reg.key) / "precedents.jsonl")[-50:]
        ledger = st.read_jsonl(Path(v.reg.key) / "ledger.jsonl")[-100:]
        started = float(getattr(bot, "started_at", STARTED))
        return {"head": st.head(), "push": st.push_enabled, "llm": prov.summary() if prov else "off", "feed": feed.status() if feed else "disabled",
                "up": int(time.time() - started), "started_at": started,
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
        guild = bot.get_guild(cfg.discord_guild_id)
        if guild is not None:
            await asyncio.to_thread(reg.resolve_officer_roles, guild)  # legacy names → ids (no-op once migrated); refreshes the name cache
        # officer roles by id (ids as strings: JS numbers can't hold a snowflake); the pickable roles likewise
        guild_roles = [{"id": str(r.id), "name": r.name} for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed] if guild else []
        officer_roles = [{"id": str(rid), "name": reg.role_names.get(rid, f"role:{rid}")} for rid in cfg.officer_role_ids]
        return {"yaml": yaml.safe_dump(cfg.model_dump(), sort_keys=False),
                "docs": {d: {"text": ps.read(d), "compiled": bool(ps.compiled(d)), "summary": ((ps.compiled(d) or {}).get("summary") if isinstance(ps.compiled(d), dict) else None)} for d in ("loot", "comp", "persona")},
                "channels": {k: {"id": str(getattr(cfg, attr)) if getattr(cfg, attr) else None, "name": chans.get(str(getattr(cfg, attr)))} for k, attr in CHANNEL_KINDS.items()},
                "guild_channels": bot.guild_channels(reg), "guild_roles": guild_roles,
                "settings": {"timezone": cfg.timezone, "ask_audience": cfg.ask_audience, "about": cfg.about or "", "officer_roles": officer_roles,
                             "officer_roles_pending": cfg.officer_roles_pending(), "owner_id": str(cfg.owner_discord_id) if cfg.owner_discord_id else None},
                "test_bench": {"members": len(reg.test_members()), "runs": [e.key for e in bot.raids.store(reg).live() if (cfg.roster(e.team) or {}).get("test")]}, "owner": v.owner}

    @app.post("/api/admin/config")
    async def admin_config(request: Request):
        """Owner: one setting at a time — channel:<kind> (channel id or null), timezone, ask_audience, about, officer_roles (list of role ids)."""
        v, d = await body(request, officer=True)
        need_owner(v)
        field, value = d.get("field") or "", d.get("value")
        try:
            if field.startswith("channel:"):
                if value and not str(value).strip("<#>").isdigit():  # a channel *name* (the MCP server) → its id
                    want = str(value).strip().lstrip("#").lower()
                    hits = [c for c in bot.guild_channels(v.reg) if c["name"].lower() == want]
                    if len(hits) != 1:
                        return JSONResponse({"error": f"no channel named #{want}" if not hits else f"#{want}: several channels match"}, status_code=400)
                    value = hits[0]["id"]
                msg = await bot.set_channel(v.reg, field[8:], as_int(str(value).strip("<#>"), "channel id") if value else None, v.name)
            elif field == "officer_roles":
                # the editor sends the whole list of role IDS (as /gm config officer-role stores them); applied as the
                # command's role_add / role_remove ops so the same code path and audit lines are used
                from .. import configops

                want = {as_int(r, "role id") for r in (value or []) if str(r).strip()}
                have = set(v.reg.config.officer_role_ids)
                for rid in sorted(have - want):
                    await configops.apply_async(v.reg, configops.ConfigOp(op="role_remove", value=str(rid)), v.name, True, bot=bot)
                for rid in sorted(want - have):
                    await configops.apply_async(v.reg, configops.ConfigOp(op="role_add", value=str(rid)), v.name, True, bot=bot)
                msg = ("officer roles: " if want != have else "officer roles unchanged: ") + (", ".join(bot.officer_role_names(v.reg)) or "(none; Manage Server only)")
            elif field in ("timezone", "ask_audience", "about"):
                from .. import configops

                msg = await configops.apply_async(v.reg, configops.ConfigOp(op="set", path=field, value=str(value or "")), v.name, True, bot=bot)
            else:
                return JSONResponse({"error": f"unknown setting {field}"}, status_code=400)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {msg}")
        return {"message": msg}

    @app.post("/api/ops/change")
    async def ops_change(request: Request):
        """Plain-text configuration (what `/gm change` and the ops-channel @mention do): Claude parses the text into
        whitelisted ConfigOps, `describe()` says current → new per op. Nothing is applied unless `apply: true`;
        questions from the parser come back instead of ops, and owner-only ops need the owner."""
        from .. import configops
        from ..policy import PolicyStore

        v, d = await body(request, officer=True)
        text = str(d.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        provider = getattr(getattr(bot, "ctx", None), "provider", None)
        if provider is None:
            return JSONResponse({"error": "the LLM provider is off (ANTHROPIC_API_KEY unset)"}, status_code=503)
        try:
            req = await asyncio.to_thread(configops.parse, provider, v.reg, text, v.name)
        except Exception as e:  # noqa: BLE001 — provider/schema errors are 502s the caller can retry
            return JSONResponse({"error": f"could not parse the request: {e}"}, status_code=502)
        ops = [op.model_dump(exclude_none=True) for op in req.ops]
        desc = []
        for op in req.ops:
            try:
                desc.append(configops.describe(v.reg, op))
            except Exception as e:  # noqa: BLE001
                desc.append(f"{op.op}: {e}")
        out = {"kind": req.kind, "reply": req.reply, "questions": list(req.questions), "ops": ops, "describe": desc, "applied": []}
        if not d.get("apply") or req.kind != "change" or not req.ops:
            return out
        ps = PolicyStore(bot.registries.store, v.reg.key)
        for op in req.ops:
            try:
                line = await configops.apply_async(v.reg, op, v.name, v.owner, ps, bot=bot)
            except (RegistryError, ValueError) as e:
                out["applied"].append(f"{op.op}: {e}")
                out["error"] = str(e)
                break
            out["applied"].append(line)
            await bot.ops.emit(v.reg.config, "info", f"[web] {v.name}: {line}")
        return out

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
