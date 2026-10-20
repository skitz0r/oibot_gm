"""JSON API for the React front end (frontend/ → static/app, served at /app).

Same identity as the HTML pages (session cookie, guild membership as the whitelist). Every route acts on the guild
`web/app.py selected_reg` picked for the request (`v.reg`: the X-Oibot-Guild header, ?g=, the oibot_guild cookie, or
the first guild the person may open); live updates are scoped to it too. Mutations go through the
same Registry / raidcycle functions the Discord commands call. CSRF: every POST must carry
`X-Requested-With: oibot` — a header a cross-site form cannot set — and the session cookie is SameSite."""
from __future__ import annotations

import asyncio
import logging
import contextlib
import inspect
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TypeVar
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from .. import comp as comp_mod, inbox, ops as ops_mod, raid_views as dr, raidcycle as rc, render
from ..consequences import consequence as consequence_lines, consequences_json
from ..presets import DURATIONS
from ..registry import ABSENCE_REASON_MAX, MAX_ABSENCE_DAYS, RANKS, CharactersChanged
from ..registry import CLASS_WORDS, MAX_SCHEDULE_ROSTERS, PLAIN_GROUPS, schedule_class, series_key, QUESTIONS_MAX, PLAIN_MODES, PLAIN_WEB, PLAIN_WHO, RAID_BOOL_FIELDS, RAID_HOURS_FIELDS, RAID_WEIGHT_DEFAULTS, SCHEDULE_CADENCE, SCHEDULE_KINDS, SPLIT_POLICIES, RegistryError
from ..roster import coverage as cov_mod
from .api_me import sheet_row_extras
from .app import GUILD_COOKIE, NotMember, login_href, set_guild_cookie

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


def public_info(bot, reg) -> dict:
    """What anyone may see (the homepage, the not-a-member page, GET /api/public): the guild's name, its public blurb,
    the Discord invite and whether applications are open, plus the server's icon. Nothing personal, no member names."""
    if reg is None:
        return {"name": "oibot_GM", "about": None, "invite_url": None, "recruiting": False, "icon": None}
    cfg = reg.config
    guild = bot.get_guild(cfg.discord_guild_id)
    icon = getattr(guild, "icon", None) if guild is not None else None
    return {"name": cfg.name, "about": cfg.about or None, "invite_url": cfg.invite_url or None, "recruiting": bool(cfg.recruitment.open),
            "icon": str(icon.with_size(128).url) if icon is not None and hasattr(icon, "with_size") else None}


def public_card(bot, reg) -> dict:
    """public_info plus the guild key: one card on the several-guild homepage, the not-a-member page and the selector."""
    return {**public_info(bot, reg), "key": reg.key}


def schedule_choice_label(reg, key: str) -> str:
    """An applicant's '<raid id>:<schedule id>' as words: 'Barrow Deeps · Tuesdays and Thursdays 7:30 PM'."""
    rid, _, sid = str(key).partition(":")
    if rid not in reg.profile.raids:
        return key
    name = reg.raid_def(rid).get("name", rid)
    sc = next((s for s in reg.schedules(rid) if s["id"] == sid), None)
    if sc is None:
        return name
    title = sc.get("name") if sid != "default" and sc.get("name") else None
    return f"{name} · " + (f"{title}: " if title else "") + reg.schedule_label(rid, sc)


def recruitment_json(reg) -> dict:
    cfg = reg.config
    return {"open": cfg.recruitment.open, "note": cfg.recruitment.note or "", "invite_url": cfg.invite_url or "",
            "questions": [q.model_dump() for q in cfg.recruitment.questions], "member_role_ids": [str(r) for r in cfg.member_role_ids],
            "questions_max": QUESTIONS_MAX}


def application_json(reg, a) -> dict:
    return {"uid": str(a.discord_id), "display_name": a.display_name, "label": a.label, "cls": a.cls, "spec": a.spec, "offspec": a.offspec,
            "schedules": [schedule_choice_label(reg, s) for s in a.schedules], "answers": list(a.answers), "availability": a.availability,
            "about": a.about, "logs_url": a.logs_url, "source": a.source, "status": a.status,
            "created": reg.local12(a.created_at), "decided": reg.local12(a.decided_at) if a.decided_at else None,
            "decided_by": a.decided_by, "note": a.decision_note}


HISTORY_PAGE = 8  # past runs per raid on the Rosters page; GET /api/runs pages the rest
PREVIEW_TTL = 600  # seconds a plain_change preview token stays good (Discord's Apply view waits as long)


def install_api(app: FastAPI, bot, *, viewer, privilege, guild_choices, live_officer=None) -> None:
    @contextlib.asynccontextmanager
    async def solving(reg, key: str):
        """The run's solver single-flight (RaidMixin.solving — shared with chat, MCP, the scheduler and the cards);
        a second caller gets a 409 instead of queueing. Keyed (guild, run key)."""
        try:
            async with bot.solving(reg, key):
                yield
        except rc.SolverBusy as e:
            raise HTTPException(409, str(e))

    async def who(request: Request, officer: bool = False):
        v = await viewer(request)
        if v is None:
            raise HTTPException(401, "login required")
        if officer and not v.officer:
            raise HTTPException(403, "Officers only.")
        request.state.reg, request.state.via = v.reg, v.via  # the guild and surface an unexpected error's ops line goes to (_api_errors)
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
            # two guilds share out/oibot.log; POST /api/guild names the guild it switched TO
            g = getattr(request.state, "oibot_guild", None) or getattr(getattr(v, "reg", None), "key", None)
            log.info("api %s %s uid=%s status=%s%s%s", request.method, request.url.path, getattr(v, "uid", None), response.status_code,
                     f" guild={g}" if g else "", "" if via == "web" else f" via={via}")
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
        """Apply a mutation and answer {message}; RegistryError/ValueError → 400 with the reason. Anything else is a bug:
        500 with configops.GENERIC_ERROR for the person and an ops error line (the owner's DM) with the details — never
        a type name in a reply (dual interfaces §2.7, configops.user_error)."""
        from ..configops import GENERIC_ERROR, report_error

        v, d = await body(request, officer=officer)
        try:
            msg = await asyncio.to_thread(fn, v, d)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — refusals read as written; bugs read as GENERIC_ERROR
            text = await report_error(bot.ops, v.reg.config, e, f"[{v.via}] {v.name} {request.url.path}")
            return JSONResponse({"error": text}, status_code=500 if text == GENERIC_ERROR else 400)
        await ops_mod.audited(bot.ops, v.reg, v.name, msg, via=v.via, by_id=v.uid)  # '[web] Jon: …' / '[mcp] Jon: …'
        return {"message": msg}

    @app.exception_handler(Exception)
    async def _api_errors(request: Request, exc: Exception):
        """A route that raised anything else under /api/: the person (and an MCP tool, whose ToolError repeats the
        `error` text) reads configops.user_error, never a traceback; the owner gets the details through ops."""
        from ..configops import GENERIC_ERROR, user_error

        if not request.url.path.startswith("/api/"):
            raise exc
        text = user_error(exc)
        if text == GENERIC_ERROR:
            log.exception("api %s failed", request.url.path, exc_info=exc)
            try:
                state = getattr(request, "state", None)
                reg = getattr(state, "reg", None)
                cfg = reg.config if reg is not None else None
                if cfg is not None:  # the surface tag comes from the viewer when one was resolved, so an MCP call is tagged as MCP
                    via = getattr(state, "via", None) or ("mcp" if request.headers.get("authorization") else "web")
                    await bot.ops.emit(cfg, "error", f"[{via}] {request.url.path}: {type(exc).__name__}: {str(exc)[:300]}", exc=exc)
            except Exception:  # noqa: BLE001 — the reply still goes out
                log.exception("ops emit failed")
        return JSONResponse({"error": text}, status_code=500 if text == GENERIC_ERROR else 400)

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

    def sheets_touched(reg, keys) -> str:
        """announce_absence's touched run keys as words — the same words the chat reply uses (configops.sheets_words)."""
        from ..configops import sheets_words

        return sheets_words(reg, bot, keys)

    async def refused(v, request: Request, e: Exception):
        """A people verb's refusal as JSON: CharactersChanged → 409 with the member's current table (the editor marks the
        rows whose base moved); a RegistryError → 400 with its words; anything else → 500 GENERIC_ERROR + the owner's ops
        line (configops.report_error — never a type name in a reply)."""
        from ..configops import GENERIC_ERROR, report_error

        if isinstance(e, CharactersChanged):
            m = e.member
            rs = bot.raids.store(v.reg)
            return JSONResponse({"error": str(e), "uid": str(m.discord_id), "characters_rev": v.reg.characters_rev(m),
                                 "characters": [char_json(v.reg, c, seat_runs(v.reg, rs, m)) for c in m.active()]}, status_code=409)
        text = await report_error(bot.ops, v.reg.config, e, f"[{v.via}] {v.name} {request.url.path}")
        return JSONResponse({"error": text}, status_code=500 if text == GENERIC_ERROR else 400)

    def absence_end(d: dict) -> str | None:
        """The end day of an absence body: `end` (ISO), or `days` (From + For how long — presets.DURATIONS, the
        Discord wizard's list) counted from `start`, inclusive."""
        if d.get("days") not in (None, ""):
            from datetime import date as _date

            try:
                days = int(d["days"])
                start = _date.fromisoformat(str(d.get("start") or "").strip())
            except (TypeError, ValueError):
                raise RegistryError("Dates are YYYY-MM-DD and days a whole number.")
            if days < 1:
                raise RegistryError("An absence is at least one day.")
            return (start + timedelta(days=days - 1)).isoformat()
        return d.get("end") or None

    async def absence_route(request: Request, v, d: dict, m):
        """POST an absence for `m` (the Me page for yourself, the Members page for someone): {start, end | days, reason,
        preview}. Preview = everything the verb refuses (overlap, reason cap, span) + what it does to each live sheet —
        the words the Discord wizard's confirm screen prints; nothing written."""
        from .. import discord_people

        reg = v.reg
        try:
            end = absence_end(d)
            if d.get("preview"):
                s, e = await asyncio.to_thread(reg.check_absence, m, d.get("start") or "", end, d.get("reason") or None)
                rs0 = bot.raids.store(reg)
                lines = discord_people.absence_lines(reg, rs0, m, s, e)
                return {"preview": True, "span": reg.span_label(s, e), "start": s, "end": e, "lines": lines,
                        "sheets": [{"key": ev.key, "effect": what} for ev, what in rc.absence_effects(reg, rs0, m, s, e)],
                        "message": f"preview — nothing saved: {m.display_name if m else v.name} away {reg.span_label(s, e)}" + ("".join(f"\n{ln}" for ln in lines) or " · no live sheet on those days")}
            mm, a, touched = await discord_people.record_absence(bot, reg, m.discord_id if m else v.uid, d.get("start") or "", end, d.get("reason") or None, v.name,
                                                                 by_id=v.uid, display_name=m.display_name if m else v.name, via=v.via)
        except Exception as e:  # noqa: BLE001 — refusals read as written
            return await refused(v, request, e)
        who = "" if mm.discord_id == v.uid else f"{mm.display_name} "
        return {"message": f"{who}away {reg.span_label(a.start, a.end)}" + sheets_touched(reg, touched), "sheets": list(touched or []), "span": reg.span_label(a.start, a.end)}

    async def clear_absence(request: Request, v, uid: int, start: str, preview: bool = False):
        """I'm back (discord_people.im_back): the absence goes, the sheets it had answered for them re-open; the lines
        say what changed. `preview`: those lines, nothing written."""
        from .. import discord_people

        reg = v.reg
        m = reg.members.get(uid)
        if not m:
            raise HTTPException(400, "unknown member")
        if preview:
            a = next((x for x in m.absences if x.start == str(start).strip()), None)
            if a is None:
                return JSONResponse({"error": f"No absence starting {reg.day_label(str(start).strip())}."}, status_code=400)
            lines = discord_people.absence_clear_lines(reg, bot.raids.store(reg), m, a)
            return {"preview": True, "lines": lines, "span": reg.span_label(a.start, a.end),
                    "message": f"preview — nothing saved: {m.display_name} back, {reg.span_label(a.start, a.end)} cleared" + "".join(f"\n{ln}" for ln in lines)}
        try:
            a, lines = await discord_people.im_back(bot, reg, uid, start, v.name, by_id=v.uid, via=v.via)
        except Exception as e:  # noqa: BLE001
            return await refused(v, request, e)
        head = f"{m.display_name} back: cleared {reg.span_label(a.start, a.end)}"
        await ops_mod.audited(bot.ops, reg, v.name, head + (" — " + "; ".join(lines) if lines else ""), via=v.via, by_id=v.uid)
        return {"message": "\n".join([head, *lines]), "lines": lines}

    def people_actor(v, rows: list[dict], preview: bool) -> None:
        """D8: a rank or a confirmation is a decision about a person — from MCP it needs X-Oibot-Actor naming an officer
        (web/app.py mcp_actor validated it); the owner's token alone may read, preview and edit the rest of a table."""
        if preview or v.via != "mcp" or getattr(v, "actor", None):
            return
        if any(isinstance(r, dict) and (r.get("rank") or r.get("confirm")) for r in rows or []):
            raise HTTPException(403, f"A rank or a confirmation is a decision a person makes: from MCP it needs the X-Oibot-Actor header naming "
                                     f"an officer of {v.reg.config.name} (set OIBOT_MCP_ACTOR to their Discord id) — the owner's token alone can "
                                     "read, preview and edit the rest of a character table.")

    async def save_table(request: Request, v, uid: int, rows: list[dict], *, officer: bool, rev: str | None, preview: bool):
        """THE character verb over HTTP (discord_people.save_characters): {message, lines, ripple} or the refusal."""
        from .. import discord_people

        people_actor(v, rows, preview)
        try:
            res = await discord_people.save_characters(bot, v.reg, uid, rows, v.name, by_id=v.uid, officer=officer, rev=rev or None, via=v.via, preview=preview)
        except Exception as e:  # noqa: BLE001
            return await refused(v, request, e)
        m = v.reg.members.get(uid)
        return {**res, "preview": preview, "characters_rev": v.reg.characters_rev(m) if m else None}

    def visible(v, m) -> bool:
        """A test puppet shows only to the officers who may change it (Registry.puppet_refusal: the live rehearsal's tester
        or the owner; any officer while no test run is live) — real members always."""
        return not m.test or v.reg.puppet_refusal(m, v.uid) is None

    @app.exception_handler(HTTPException)
    async def _json_errors(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    # ---- read
    async def meta_json(v) -> dict:
        reg = v.reg
        grouping = reg.profile.comp_rules.get("grouping") or {}
        label = reg.profile.label
        return {
            "guild": reg.config.name, "tz": reg.config.timezone,
            # the server selector (design §5.32 "Web"): the selected guild, every guild this person may open with their
            # role there, and `moved` = the name of the guild the browser had selected that they can no longer open
            "guild_key": reg.key, "guilds": await guild_choices(v) if v.via == "web" else [{"key": reg.key, "name": reg.config.name, "icon": None, "role": "owner"}],
            "moved": v.moved, "profile": label, "game": label.split(": ", 1)[-1],
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
            # absences: the Discord wizard's For how long list (presets.DURATIONS) and the verb's caps, so the web modals can't drift
            "durations": [{"days": d, "label": label} for d, label in DURATIONS], "max_absence_days": MAX_ABSENCE_DAYS, "absence_reason_max": ABSENCE_REASON_MAX,
            "ranks": list(RANKS),
            # the DM toggle's one sentence each way (design B2; inbox.DM_ON/OFF_SENTENCE — /me view and /me dms say the same)
            "dm_sentences": {"on": inbox.DM_ON_SENTENCE, "off": inbox.DM_OFF_SENTENCE},
        }

    @app.get("/api/meta")
    async def meta(request: Request):
        v = await who(request)
        out = JSONResponse(await meta_json(v))
        if v.moved:  # landed on another guild: remember it, so the notice shows once
            set_guild_cookie(out, v.reg.key)
        return out

    @app.get("/api/guilds")
    async def guilds_list(request: Request):
        """The guilds this caller may act on (design §5.32 "MCP"). The MCP bearer (valid and local; no guild needed):
        every configured guild, with its Discord ids — the MCP server resolves a guild name to its key from this and
        computes its own default (the bot never reads OIBOT_MCP_GUILD). A web session: the guilds the person may open,
        with their role there (the selector's own list stays /api/meta.guilds)."""
        import os as _os

        from .app import MCP_TOKEN_ENV, mcp_authorized

        if request.headers.get("authorization"):
            if not mcp_authorized(request, _os.environ.get(MCP_TOKEN_ENV) or None):
                raise HTTPException(401, "login required")
            return {"guilds": [{"key": r.key, "name": r.config.name, "discord_guild_id": str(r.config.discord_guild_id),
                                "owner_discord_id": str(r.config.owner_discord_id) if r.config.owner_discord_id else None,
                                "game_profile": r.config.game_profile} for r in bot.registries.ordered()]}
        v = await who(request)
        return {"guilds": await guild_choices(v)}

    @app.post("/api/guild")
    async def choose_guild(request: Request):
        """The server selector: {key} → that guild becomes this browser's selection (the oibot_guild cookie) when the
        person may open it, and the answer is its meta; otherwise 403 not_member and nothing changes."""
        if request.headers.get("x-requested-with") != "oibot":
            raise HTTPException(403, "Missing request marker.")
        try:
            d = await request.json()
        except ValueError:
            d = {}
        key = str((d or {}).get("key") or "").strip() if isinstance(d, dict) else ""
        if not key:
            raise HTTPException(400, "key required")
        v = await viewer(request, guild=key)
        if v is None:
            raise HTTPException(401, "login required")
        if v.via != "web":
            raise HTTPException(400, "the MCP server names its guild on every call")
        request.state.oibot_guild = v.reg.key
        out = JSONResponse(await meta_json(v))
        set_guild_cookie(out, v.reg.key)
        return out

    def run_label(reg, rs, team_key: str) -> dict:
        """Placement asks are keyed by the run's roster key; people see the raid name and the start time instead (B3)."""
        evs = sorted((e for e in rs.live() if e.team == team_key), key=lambda e: e.starts_at)
        if not evs:
            return {"raid": team_key, "when": "", "key": None}
        ev = evs[0]
        return {"raid": reg.raid_def(ev.instance).get("name", ev.instance) if ev.instance else team_key, "when": t12(reg, ev.starts_at), "key": ev.key}

    def seat_runs(reg, rs, m) -> dict[str, list[str]]:
        """character label -> the locked live runs it holds a seat on (from the run files: rc.rostered_runs), as a
        person reads them: raid name + 12-hour start (dr.run_title), never the run key (its tail is the 24-hour start)."""
        out: dict[str, list[str]] = {}
        for ev, char in (rc.rostered_runs(reg, rs, m) if m else []):
            out.setdefault(char, []).append(dr.run_title(reg, ev))
        return out

    def char_json(reg, c, runs: dict[str, list[str]] | None = None) -> dict:
        """`rosters` = the runs this character is rostered on (seat_runs), shown by the delete confirmations;
        `regular_on` = the recurring schedules it is a regular of, in words ('Barrow Deeps · Raid nights')."""
        role = reg.profile.spec(c.cls, c.spec).role
        off = reg.profile.spec(c.cls, c.offspec).role if c.offspec else None
        return {"label": c.label, "name": c.name, "surname": c.surname, "cls": c.cls, "spec": c.spec, "offspec": c.offspec, "is_main": c.is_main,
                "status": c.status, "rank": c.rank, "rosters": list((runs or {}).get(c.label, [])), "confirmed": bool(c.confirmed_by), "role": role, "off_role": off,
                "flex": list(c.flex), "regular_on": [reg.series_label(k) for k in c.rosters if ":" in k]}

    def regulars_json(reg, m) -> list[dict]:
        """A member's regular series: {series, label, character, cls, spec, role} (the Me page's My regular raids)."""
        return [{"series": k, "label": reg.series_label(k), "character": c.label, "cls": c.cls, "spec": c.spec, "role": reg.profile.spec(c.cls, c.spec).role}
                for k, c in (reg.regular_series_of(m) if m else [])]

    def absence_json(reg, a) -> dict:
        return {"start": a.start, "end": a.end, "reason": a.reason, "span": reg.span_label(a.start, a.end)}  # `span` = the days in words (the clock rule); ISO is for the clear call

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
            seat = ev.seat_of(m) if m and ev.state != "open" else None
            sheets.append({"key": ev.key, "raid": rd.get("name") or team.get("name", ev.team), "starts_at": ev.starts_at, "when": t12(reg, ev.starts_at), "state": ev.state,
                           "status": s.status if s else None, "label": rc.LABELS.get(s.status, s.status) if s else None, "character": s.character if s else None, "note": s.note if s else None,
                           "rostered": bool(seat), "roster": (seat[0] + 1) if seat else None, "kind": rc.run_kind(reg, ev),
                           "regular": bool(m) and any(mm.discord_id == m.discord_id for mm, _c in rc.run_regulars(reg, ev)) and not rc.is_test_run(reg, ev),
                           "confirms": rc.run_confirms(reg, ev), **sheet_row_extras(reg, ev, m)})
        primary, flex = reg.roles_of(m) if m else (None, [])
        today = reg.now_local().date().isoformat()
        return {
            "display_name": m.display_name if m else v.name, "registered": m is not None,
            "characters_rev": reg.characters_rev(m) if m else None, "rank": m.rank if m else None,  # the table's base (a stale save is refused, 409)
            "must_apply": must_apply(v),  # a newcomer: the page points to the Discord application instead of the character table
            "characters": [char_json(reg, c, seat_runs(reg, rs, m)) for c in (m.active() if m else [])],
            "roles": {"primary": primary, "flex": list(flex)},
            "absences": [absence_json(reg, a) for a in (m.upcoming_absences(today) if m else [])],
            "dm": not (m.dm_opt_out if m else False),
            "dm_sentence": inbox.DM_OFF_SENTENCE if (m and m.dm_opt_out) else inbox.DM_ON_SENTENCE,
            "sheets": sheets,
            "regulars": regulars_json(reg, m),  # the recurring raids they're a regular of (Step down on the Me page)
            # every open ask wherever it waits (`channel` dm | web): Confirm / Can't, fill, swap, pre-lock (inbox.asks_of), and
            # the notices (D26: outcomes that are never a DM) — the Me page's AskCards and its inbox
            "asks": inbox.asks_of(reg, rs, v.uid),
            "notices": inbox.notices_of(reg, v.uid),
        }

    # ---- member self-service
    APPLY_FIRST_WEB = "New here? Apply in Discord first: press Apply to join on the registration card, or use /apply."

    def role_ids_of(reg, uid: int) -> list[int]:
        """The viewer's Discord role ids from the gateway cache (the member-role bypass of `must_apply`)."""
        guild = bot.get_guild(reg.config.discord_guild_id) if hasattr(bot, "get_guild") else None
        m = guild.get_member(uid) if guild is not None and hasattr(guild, "get_member") else None
        return [r.id for r in getattr(m, "roles", []) or []]

    def must_apply(v) -> bool:
        return v.reg.must_apply(v.uid, officer=v.officer, role_ids=role_ids_of(v.reg, v.uid))

    @app.post("/api/me/characters")
    async def me_characters(request: Request):
        """One save for your table {rows, rev, preview}: the same verb as the Members page, /me char, plain text and MCP
        (discord_people.save_characters — validated on a copy, rank and confirm refused for a member, the ripple into
        sheets and seats). `preview` writes nothing and answers the ripple sentences. A newcomer applies in Discord first."""
        v, d = await body(request)
        if must_apply(v):
            return JSONResponse({"error": APPLY_FIRST_WEB}, status_code=400)
        try:
            rows = table_rows(d.get("rows"))
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=400)
        return await save_table(request, v, v.uid, rows, officer=False, rev=d.get("rev"), preview=bool(d.get("preview")))

    @app.post("/api/me/main")
    async def me_main(request: Request):
        """Make one of your characters the main (the rank moves with it): a one-row table save."""
        v, d = await body(request)
        label = parse(d, LabelBody).label
        return await save_table(request, v, v.uid, [{"label": label, "main": True}], officer=False, rev=d.get("rev"), preview=bool(d.get("preview")))

    @app.post("/api/me/character/delete")
    async def me_delete(request: Request):
        """Delete (or with retire: true, retire) one of your characters — a one-row table save, so a seat it holds is
        handed back and the bench asked; `preview` says so first."""
        v, d = await body(request)
        label = parse(d, LabelBody).label
        row = {"label": label, "retire": True} if d.get("retire") else {"label": label, "delete": True}
        return await save_table(request, v, v.uid, [row], officer=False, rev=d.get("rev"), preview=bool(d.get("preview")))

    @app.post("/api/me/absence")
    async def me_absence(request: Request):
        """Your absence: {start, days | end, reason, preview} — From + For how long like the Discord wizard."""
        v, d = await body(request)
        return await absence_route(request, v, d, v.reg.members.get(v.uid))

    @app.post("/api/me/absence/clear")
    async def me_absence_clear(request: Request):
        """I'm back: the absence goes, and the sheets it had answered No thanks on re-open for you (the lines say which)."""
        v, d = await body(request)
        return await clear_absence(request, v, v.uid, parse(d, StartBody).start, bool(d.get("preview")))

    @app.post("/api/me/placement")
    async def me_placement(request: Request):
        v, d = await body(request)
        b = parse(d, PlacementBody)
        yes = b.answer == "yes"
        try:
            line = await bot.answer_placement_for(v.reg, v.uid, b.roster, yes, v.name, by_id=v.uid)
        except (RegistryError, ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "bad request"}, status_code=400)
        return {"message": line}

    @app.post("/api/me/regular/remove")
    async def me_regular_remove(request: Request):
        """Step down from a series' regulars (D7: a member may step down; volunteering is a request to officers).
        The same registry verb as an officer's removal (Registry.regular_remove)."""
        def go(v, d):
            key = str(d.get("series") or "").strip()
            v.reg.regular_remove(v.uid, key, v.name)
            return f"stepped down from {v.reg.series_label(key)}"
        return await run(request, go)

    @app.post("/api/admin/raid/regulars")
    async def admin_regulars(request: Request):
        """Officers add or remove a regular of a recurring schedule: {raid, schedule (default "default"), member | uid,
        character (default their main), on (true = add, false = remove)}. The one path for the Rosters page, Discord's
        /roster add|remove wizards (same verbs), plain text's op=regular and MCP set_regular."""
        def go(v, d):
            reg = v.reg
            rid = str(d.get("raid") or "").strip()
            if rid not in reg.profile.raids:
                n = rid.lower()
                hits = [r for r in reg.profile.raids if reg.raid_def(r).get("name", r).lower() == n]
                if len(hits) != 1:
                    raise RegistryError(f"unknown raid {rid or '?'} (one of {', '.join(reg.profile.raids)})")
                rid = hits[0]
            sid = str(d.get("schedule") or "default").strip().lower()
            key = series_key(rid, sid)
            m = resolve_member(reg, member_ref(d))
            if d.get("on", True) in (False, "false", "off", "remove", 0):
                reg.regular_remove(m.discord_id, key, v.name)
                return f"{m.display_name} is no longer a regular of {reg.series_label(key)}"
            mm, c, warn = reg.regular_add(m.discord_id, key, v.name, str(d.get("character") or "").strip() or None)
            return f"{mm.display_name} ({c.label}) is a regular of {reg.series_label(key)}" + "".join(f" — warning: {w}" for w in warn)
        return await run(request, go, officer=True)

    @app.post("/api/me/dm")
    async def me_dm(request: Request):
        """My DMs on / off — Registry.set_dm, the verb /me dms, the Members page, plain text and MCP share."""
        return await run(request, lambda v, d: v.reg.set_dm(v.uid, bool(d.get("on")), v.reg.members[v.uid].display_name if v.uid in v.reg.members else v.name))

    # ---- runs: the officer panel (sheets per raid, the board, lock, confirmations)
    def t12(reg, value) -> str:
        """'Wed 09 Dec 4:00 PM' in guild time (12-hour clock everywhere the site shows a run time)."""
        return reg.local12(value)  # THE formatter (Registry.local12); nothing here composes a clock of its own

    def clock12(reg, value) -> str:
        return reg.local12(value, "%I:%M %p")

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

    def schedule_json(reg, rid: str, s: dict) -> dict:
        """One schedule for the pages: its stored settings (machine form, for the editors), what it inherits, and every
        time a person reads already in words (`label`, `slot_labels`, `time_label`, `next`) — no page formats a clock."""
        sd = reg.schedule_def(rid, s["id"])
        size = int(sd.get("size") or 20)
        cls = schedule_class(s["kind"])
        key = series_key(rid, s["id"])
        return {"id": s["id"], "name": s.get("name") or s["id"], "kind": s["kind"], "class": cls, "class_label": CLASS_WORDS[cls],
                "series": key if cls == "recurring" else None, "regulars": len(reg.regulars(key)) if cls == "recurring" else 0,
                "source": dict(sd["schedule"]["source"]), "own_weights": dict(sd["schedule"].get("own_weights") or {}), "weights": dict(sd["weights"]),
                "active": bool(s.get("active", True)), "rosters": int(s.get("rosters") or 1),
                "seats": size * int(s.get("rosters") or 1), "slots": list(s.get("slots") or []), "slot_labels": [reg.slot_label(x) for x in s.get("slots") or []],
                "days": list(s.get("days") or []), "time": s.get("time") or "", "time_label": reg.time_label(s.get("time")) if s.get("time") else "",
                "label": reg.schedule_label(rid, s), "own": dict(sd["schedule"]["own"]), "is_default": s["id"] == "default",
                "effective": {k: sd[k] for k in SCHEDULE_CADENCE},
                "next": [t12(reg, t) for t in rc.next_runs_of(reg, rid, s)]}

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
                "runs": rc.how_many_rosters(reg, players, size, rb, want=rc.run_rosters(reg, ev)), "reason": reason, "reason_text": rc.split_reason_text(reason) if reason else None}

    def ask_json(reg, a) -> dict:
        found = reg.find(a.character)
        return {"uid": str(a.discord_id), "display_name": a.display_name, "kind": a.kind, "character": a.character, "spec": a.spec, "cls": found[1].cls if found else None,
                "role": a.role, "reason": a.reason, "why": a.why, "pair": str(a.pair) if a.pair is not None else None, "vacates": a.vacates}

    def plan_json(reg, plan) -> dict:
        """rc.role_plan as data: each seat (role, "" = a plain seat after lock) with its options — [0] the suggestion,
        then alternates; an option is one ask or a tied swap + backfill — plus what stays short and the `rev` a send carries."""
        return {"phase": plan.phase, "short": plan.short, "have": plan.have, "need": plan.need, "full": plan.full, "expected": plan.expected, "waiting": plan.waiting,
                "seats": [{"role": st.role, "options": [[ask_json(reg, a) for a in opt] for opt in st.options]} for st in plan.seats],
                "short_after": plan.short_after, "still_short": rc.still_short_words(plan), "rev": plan.rev, "lines": rc.plan_lines(reg, plan),
                "room": plan.room if plan.phase == "fill" else None, "batch_lines": rc.batch_preview(plan)}

    def ev_json(reg, rs, ev, full: bool = True) -> dict:
        t = reg.config.team(ev.team) or {"key": ev.team, "size": reg.raid_def(ev.instance).get("size", 20)}
        live = ev.state not in ("done", "cancelled")
        rd = reg.raid_def(ev.instance)
        day = ev.start.astimezone(reg.tz).date().isoformat()
        base = {"key": ev.key, "run": ev.team, "name": t.get("name") or t["key"], "size": rc.run_size(reg, ev), "instance": ev.instance, "raid": rd.get("name", ev.instance),
                "starts_at": ev.starts_at, "when": t12(reg, ev.starts_at), "rel": rel(reg, ev.start), "state": ev.state, "live": live, "fill_state": ev.fill_state,
                "counts": {st: len(ev.by_status(st)) for st in rc.STATUSES}, "rostered": len(ev.seated()) if ev.all_rosters else 0, "n_rosters": len(ev.all_rosters), "schedule": ev.schedule, "rosters_expected": rc.run_rosters(reg, ev),
                "kind": rc.run_kind(reg, ev), "series": rc.run_series(reg, ev), "series_name": reg.series_label(rc.run_series(reg, ev)) if rc.run_series(reg, ev) else None,
                "confirms": rc.run_confirms(reg, ev), "test": rc.is_test_run(reg, ev)}
        if not full:
            return base
        from ..raid_views import run_times

        soft, hard, confirm = run_times(reg, ev, t)
        conf_rows = rc.confirmations(reg, ev) if ev.all_rosters else []
        board_rosters = ev.all_rosters if ev.state != "open" and ev.all_rosters else rc.board_rosters(reg, ev, ev.layout or [], rc.run_size(reg, ev))
        absences = [{"display_name": m.display_name, "start": a.start, "end": a.end, "reason": a.reason, "signed": str(m.discord_id) in ev.signups}
                    for m in reg.members.values() for a in m.absences if a.start <= day <= a.end]
        busy = rc.conflicts(rs, ev) if live else {}
        plan = rc.role_plan(reg, rs, ev) if live else None  # the planner's suggestions (computed on view, never stored), once per run
        return {**base,
                "timeline": {"nudge": t12(reg, soft) if rc.team_setting(t, "reminders") != "none" else None, "lock": t12(reg, hard),
                             "confirm": (clock12(reg, confirm) if confirm.date() == ev.start.astimezone(reg.tz).date() else t12(reg, confirm)) if rc.run_confirms(reg, ev) else None,
                             "locked_at": clock12(reg, ev.locked_at) if ev.locked_at else None},
                "signups": [signup_json(s, ev.pins) for s in sorted(ev.signups.values(), key=lambda s: (rc.STATUSES.index(s.status) if s.status in rc.STATUSES else 9, s.updated_at))],
                # officer-facing: who the bot expects an answer from and hasn't heard from — a recurring run's regulars
                # (every main while its series has none: `expected_from` says which); an ad hoc run expects nobody
                "not_answered": [{"uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec, "role": reg.profile.spec(c.cls, c.spec).role}
                                 for m, c in rc.not_answered(reg, ev)] if live else [],
                "expected_from": rc.expected_from(reg, ev),
                "regulars": rc.regular_answers(reg, ev),  # each regular and what they answered (status None = no answer)
                "absences": absences, "double_booked": [reg.members[u].display_name for u in busy if u in reg.members],
                "needs": rc.needs(reg, ev, t) if live else None,
                "board": board_json(reg, ev, board_rosters, conf_rows), "has_layout": bool(ev.layout), "rev": rc.board_rev(reg, ev),
                "split": split_json(reg, ev) if live else {"strategy": ev.split_strategy or rd.get("split_policy", "balanced"), "policy": rd.get("split_policy", "balanced"), "runs": 1, "reason": None, "reason_text": None},
                "confirmations": conf_rows,
                "fill_asks": [{"uid": str(a.discord_id), "display_name": a.display_name, "kind": a.kind, "character": a.character, "spec": a.spec, "role": a.role, "reason": a.reason, "why": a.why, "answer": a.answer,
                               "expires_at": a.expires_at, "deadline": t12(reg, a.expires_at) if a.expires_at else None, "pair": a.pair, "phase": a.phase, "approved_by": a.approved_by,
                               "answered_by": a.answered_by, "channel": a.channel, "pending": a.pending} for a in ev.fill_asks],
                # the planner's suggestions (computed on view, never stored): what an officer's Ask / Suggest would send
                "plan": plan_json(reg, plan) if plan is not None else None,
                "callouts": [{"display_name": c.display_name, "hours_before": c.hours_before, "late": c.late, "note": c.note} for c in ev.callouts],
                "log": list(ev.log),  # the machine form; `log_rows` = the same lines with stamps in words, officer-only marked
                "log_rows": rc.log_rows(reg, ev),
                "health": health_json(reg, ev, t) if ev.state == "open" else None,
                # the confirm sentences for lock / cancel / clear / release, from code (consequences.py): the browser prints, never composes
                "consequences": consequences_json(reg, ev),
                # where the run lives in Discord (sheet, updates thread, loot thread) and its loot council in one line (design §2.5)
                "links": links_json(reg, ev), "loot": loot_json(reg, ev),
                **night_json(reg, rs, ev, t, plan)}

    def night_json(reg, rs, ev, t, plan=None) -> dict:
        """The raid-night state the SheetCard renders (dual interfaces phase 2, design §2.5 / B4–B8): every one computed
        from the run, words already written (no clock or sentence composed in the browser)."""
        live = ev.state not in ("done", "cancelled")
        locked = ev.state == "locked" and bool(ev.all_rosters)
        failed = None
        if ev.lock_error and ev.state == "open":
            failed = f"Lock failed {t12(reg, ev.lock_tried_at)} — the sheet stays open, retrying: {ev.lock_error}" if ev.lock_tried_at else f"Lock failed — {ev.lock_error}"
        remind_no = rc.remind_refusal(reg, ev) if locked else None
        end_no = rc.end_refusal(ev)
        reopen_no = rc.reopen_refusal(reg, ev) if ev.state in ("done", "cancelled") else None
        return {"lock_error": failed,
                "fill_state_text": rc.fill_state_text(reg, ev),
                "early_warning": rc.early_warning(reg, rs, ev, plan) if ev.state == "open" else False,
                "spec_switches": rc.spec_switches(reg, ev) if locked else [],
                "regulars_not_rostered": rc.regulars_not_rostered(reg, ev) if locked else [],
                "confirm_by": t12(reg, ev.confirm_by) if ev.confirm_by else None,
                "confirmations_sent_at": t12(reg, ev.confirmations_sent_at) if ev.confirmations_sent_at else None,
                "confirmations_pending": locked and rc.run_confirms(reg, ev) and not ev.confirmations_sent_at,
                "asks_on_site": rc.asks_on_site(reg, ev) if live else 0,
                "autofill_paused": bool(ev.autofill_paused), "autofill_paused_by": ev.autofill_paused_by,
                "reminded_at": t12(reg, ev.reminded_at) if ev.reminded_at else None, "reminded_by": ev.reminded_by,
                "may_remind": locked and remind_no is None, "remind_note": remind_no,
                "may_end": end_no is None, "end_note": end_no,
                "may_reopen": ev.state == "done" and reopen_no is None, "reopen_note": reopen_no,
                "closed_at": t12(reg, ev.closed_at) if ev.closed_at else None}

    def discord_url(reg, channel_id, message_id=None) -> str | None:
        if not channel_id:
            return None
        return f"https://discord.com/channels/{reg.config.discord_guild_id}/{channel_id}" + (f"/{message_id}" if message_id else "")

    def run_sessions(reg, ev) -> list:
        return [s for s in getattr(bot, "events", {}).values() if getattr(s, "guild", None) == reg.key and getattr(s, "run_key", None) == ev.key]

    def links_json(reg, ev) -> dict:
        sessions = run_sessions(reg, ev)
        thread = next((s.raid_thread_id for s in sessions if s.raid_thread_id), None)
        card = ev.cards.get("lock:0") or ev.cards.get("health")
        return {"sheet": discord_url(reg, ev.channel_id, ev.message_id) if ev.message_id else None,
                "card": discord_url(reg, ev.cards_channel_id, card) if card and ev.cards_channel_id else None,  # the lock card (the raid-night hub), else the health card
                "thread": discord_url(reg, ev.updates_thread_id),  # the run's updates thread (design §2.5 links{sheet,card,thread})
                "updates": discord_url(reg, ev.updates_thread_id), "loot": discord_url(reg, thread)}

    def loot_json(reg, ev) -> dict:
        """The run's loot council at a glance (the SheetCard / lock card hub): open or not, its thread, awards recorded,
        picks waiting for Confirm, reasons owed. Loot › Tonight has the whole council (GET /api/loot)."""
        sessions = run_sessions(reg, ev)
        if not sessions:
            return {"open": False, "councils": 0, "thread": None, "awards": 0, "pending": 0, "reasons": 0, "may_open": ev.state == "locked"}
        return {"open": True, "councils": len(sessions), "thread": discord_url(reg, next((s.raid_thread_id for s in sessions if s.raid_thread_id), None)),
                "awards": sum(1 for s in sessions for a in s.awards if not a.void), "pending": sum(len(s.proposals) for s in sessions),
                "reasons": sum(len(s.pending_reasons) for s in sessions), "may_open": False}

    def health_json(reg, ev, t) -> dict:
        """The open sheet's health card as data (MCP get_run, the help context): headcount against the seats the run
        expects, per role have/need/level with who could cover a shortfall, and the party buffs nobody on it provides."""
        h = rc.health_data(reg, ev, t)
        arts = {b.id: getattr(b, "art", None) for b in reg.profile.party_buffs()}
        n, size, _legacy, subs = h["headcount"]
        return {"headcount": {"in": n, "seats": size, "bench": subs, "level": h["headcount_level"]},
                "roles": [{k: r[k] for k in ("role", "have", "need", "level", "hint")} for r in h["roles"]],
                "no_provider": [b["name"] for b in h["buffs"] if not b["providers"]],
                # the page draws these as icon-only chips (the iconography rule: the name is the tooltip, never a label)
                "no_provider_buffs": [{"id": b["id"], "abbr": b["abbr"], "colour": b["colour"], "name": b["name"], "art": arts.get(b["id"])} for b in h["buffs"] if not b["providers"]],
                "unanswered": len(h["unresponsive"]), "not_answered": list(h["unresponsive"]),
                "expected_from": h.get("expected_from"), "rosters": h["rosters"]}

    def rosters_data(reg) -> dict:
        rs = bot.raids.store(reg)
        now = reg.now_local()
        events = sorted(rs.events.values(), key=lambda e: e.starts_at)
        out = []
        for rid in reg.profile.raids:
            rd = reg.raid_def(rid)
            evs = [e for e in events if e.instance == rid]
            live = [ev_json(reg, rs, e) for e in evs if e.state not in ("done", "cancelled")]
            closed = [e for e in reversed(evs) if e.state in ("done", "cancelled")]
            past = [ev_json(reg, rs, e, full=False) for e in closed[:HISTORY_PAGE]]  # the rest: GET /api/runs (paged)
            fo = reg.first_open(rid)
            scheds = reg.schedules(rid)
            many = len([s for s in scheds if s["kind"] != "pickup"]) > 1
            no_channel = not reg.config.signup_channel_id

            def opens_at(s, start):
                return start - timedelta(hours=float(reg.schedule_def(rid, s["id"])["signup_lead_hours"]))
            # each upcoming run: its exact instant (`start_iso`, what the row's Open carries back to the open route) and,
            # with no signup channel set, `needs_channel` once its sheet is due — the scheduler opens nothing then
            upcoming = [{"slot": (s["name"] if many else reg.slot_label(start.astimezone(reg.tz).strftime("%a %H:%M"))), "schedule": s["id"], "schedule_name": s["name"], "start": t12(reg, start),
                         "start_iso": start.isoformat(), "key": rc.event_key(rid, start, s["id"]), "opens": t12(reg, opens_at(s, start)),
                         "needs_channel": no_channel and opens_at(s, start) <= now}
                        for s, start in rc.upcoming(reg, rid, now, 24 * rc.OPEN_HORIZON_DAYS) if rc.event_key(rid, start, s["id"]) not in rs.events][:6]
            series = rc.series_outlook(reg, rid)  # one card per recurring series: its regulars (one member per line on the page) against the minimums, level, forecast
            out.append({"id": rid, "name": rd.get("name", rid), "size": int(rd.get("size") or 20), "slots": list(rd["slots"]), "slot_labels": [reg.slot_label(x) for x in rd["slots"]], "lockout_days": int(rd["lockout_days"]),
                        "schedules": [{"id": s["id"], "name": s["name"], "kind": s["kind"], "class": schedule_class(s["kind"]), "label": reg.schedule_label(rid, s), "active": bool(s.get("active", True))} for s in scheds],
                        "series": series,
                        "orphan_regulars": [{"key": k, "label": reg.series_label(k), "uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec}
                                            for k, m, c in reg.orphan_regulars() if k.partition(":")[0] == rid],
                        "opened": bool(fo and fo <= now), "first_open": reg.local12(fo, "%a %d %b %Y %I:%M %p") if fo else None,
                        "open": [e for e in live if e["state"] == "open"], "locked": [e for e in live if e["state"] != "open"], "past": past, "past_total": len(closed),
                        "upcoming": upcoming, "signup_channel": bool(reg.config.signup_channel_id)})
        orphans = [ev_json(reg, rs, e, full=False) for e in events if e.instance not in reg.profile.raids and e.state not in ("done", "cancelled")][:6]
        # who can be made a regular (the Rosters page's member and character pickers): real members with active characters
        people = [{"uid": str(m.discord_id), "display_name": m.display_name,
                   "characters": [{"label": c.label, "cls": c.cls, "spec": c.spec, "role": reg.profile.spec(c.cls, c.spec).role, "is_main": c.is_main}
                                  for c in sorted(m.active(), key=lambda c: (not c.is_main, c.label.lower()))]}
                  for m in sorted(reg.members.values(), key=lambda m: m.display_name.lower()) if not m.test and m.active()]
        return {"raids": out, "orphans": orphans, "people": people, "tz": reg.config.timezone}

    @app.get("/api/rosters")
    async def rosters(request: Request):
        v = await who(request, officer=True)
        return await asyncio.to_thread(rosters_data, v.reg)

    @app.get("/api/runs")
    async def runs_history(request: Request, raid: str | None = None, offset: int = 0, limit: int = 20):
        """Past runs (done / cancelled), newest first, paged beyond the Rosters page's first HISTORY_PAGE (officers):
        `raid` = one raid id (default every raid), `offset` / `limit` (≤ 50). Rows are ev_json's short form; a row opens
        the read-only run drawer through GET /api/run/{key}."""
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        if raid and raid not in v.reg.profile.raids:
            return JSONResponse({"error": f"unknown raid {raid}"}, status_code=404)
        closed = sorted([e for e in rs.events.values() if e.state in ("done", "cancelled") and (not raid or e.instance == raid)],
                        key=lambda e: e.starts_at, reverse=True)
        offset, limit = max(0, int(offset)), max(1, min(50, int(limit)))
        page = closed[offset:offset + limit]
        return {"runs": [ev_json(v.reg, rs, e, full=False) for e in page], "total": len(closed), "offset": offset, "limit": limit,
                "more": offset + limit < len(closed)}

    @app.get("/api/readiness")
    async def readiness(request: Request, raid: str | None = None):
        """Pool readiness per raid (officers): what the analytics card draws plus the words behind it — headcount,
        roles with who could cover, buffs nobody brings, the desired comp with its reasons, short / over lists and the
        pool plan (comp.readiness; no solver). `raid` = one raid id; default every raid of the profile."""
        from .. import comp

        v = await who(request, officer=True)
        ids = [raid] if raid else list(v.reg.profile.raids)
        if raid and raid not in v.reg.profile.raids:
            return JSONResponse({"error": f"unknown raid {raid} (one of {', '.join(v.reg.profile.raids)})"}, status_code=404)
        rs = bot.raids.store(v.reg)
        rows = await asyncio.to_thread(lambda: [comp.readiness(v.reg, rid, rs) for rid in ids])
        return {"raids": rows}

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

    def stale(v, ev):
        return JSONResponse({"error": "Someone else changed this board a moment ago — it has been reloaded; check it and try again.", **board_reply(v.reg, ev)}, status_code=409)

    def board_written(v, ev, was_open: bool, line: str) -> dict:
        """The board after a write; after lock the line says who was asked / freed (before lock the drag speaks for itself)."""
        return board_reply(v.reg, ev) if was_open else {**board_reply(v.reg, ev), "message": line}

    @app.post("/api/run/{key}/layout")
    async def run_layout(request: Request, key: str):
        """The board after a drag: before lock it is saved as the layout the lock will use; after lock it is the roster
        (group moves are free, someone dragged in from the bench is asked to confirm, someone dragged out is freed).
        The write is RaidMixin.save_board — the verb MCP and plain text use; a stale `rev` is refused (409)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        groups = [[str(n) for n in g] for g in (d.get("groups") or [])]
        strategy = (d.get("strategy") or "").strip() or None  # Use this split: the layout and its strategy in ONE write (design B6)
        if strategy and strategy not in SPLIT_POLICIES:
            return JSONResponse({"error": f"strategy must be one of {', '.join(SPLIT_POLICIES)}"}, status_code=400)
        was_open = ev.state == "open"
        try:  # a whole-board write (Clear, Use this split) is only safe on the board its author was looking at
            line = await bot.save_board(v.reg, rs, ev, groups, rev=d.get("rev") or None, by=v.name, via=v.via, strategy=strategy)
        except rc.BoardChanged:
            return stale(v, ev)
        return board_written(v, ev, was_open, line)

    @app.post("/api/run/{key}/move")
    async def run_move(request: Request, key: str):
        """One drag, applied to the board as it is NOW: {member, to: {r, g, i} | null (= bank), state}. Two officers
        dragging at once merge; nothing is overwritten. Same effects as a whole-board write after lock (confirm DM / seat
        freed). `state` = the state the board was rendered in (open | locked): when the run's state differs (the
        scheduler locked it mid-drag) the move is refused with 409 + the current board, never applied as a post-lock
        roster edit (design B6). The write is RaidMixin.board_move."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        name, to = str(d.get("member") or ""), d.get("to")
        seen = (d.get("state") or "").strip() or None
        if seen and seen not in ("open", "locked"):
            return JSONResponse({"error": "state is open or locked (the state the board was drawn in)"}, status_code=400)
        n = rc.groups_per_roster(v.reg, rc.run_size(v.reg, ev))
        target = None if not to else (as_int(to.get("r"), "to.r") * n + as_int(to.get("g"), "to.g"), as_int(to.get("i", 99), "to.i"))
        was_open = ev.state == "open"
        try:
            line = await bot.board_move(v.reg, rs, ev, name, target, by=v.name, via=v.via, expect_state=seen)
        except rc.BoardChanged as e:
            return JSONResponse({"error": str(e), "state": ev.state, **board_reply(v.reg, ev)}, status_code=409)
        except ValueError as e:
            return JSONResponse({"error": str(e)[:1].upper() + str(e)[1:], **board_reply(v.reg, ev)}, status_code=409)
        return board_written(v, ev, was_open, line)

    # ---- live updates: one event stream per open page, tagged with the page's guild; a run save anywhere (site,
    # Discord, scheduler) wakes that guild's pages only — two guilds with the same run key never wake each other
    subscribers: dict[asyncio.Queue, tuple[str, bool]] = {}  # queue -> (guild key, the page's viewer is an officer)

    def publish(topic: str, guild: str | None = None, *, officers: bool = False) -> None:
        """topic: `run:<key>` | `loot:<run key>` | `test:<run key>` | `member` | `config` | `ops` | `agents`. `guild` None = every
        page (process-wide news: the agents' job status); otherwise only that guild's pages. `officers`: only pages an
        officer opened (the test feed: what it names is officers' business). Safe from worker threads."""
        def fan_out():
            for q, (g, officer) in list(subscribers.items()):
                if (guild is None or g == guild) and (officer or not officers) and q.qsize() < 50:
                    q.put_nowait(topic)
        try:
            bot.loop.call_soon_threadsafe(fan_out)
        except Exception:  # noqa: BLE001 — loop not running yet / shutting down
            pass

    def run_saved(guild_key: str, run_key: str) -> None:
        publish(f"run:{run_key}", guild_key)

    def reg_saved(reg, kind: str, lines) -> None:
        publish(kind, reg.key)

    def ops_line(guild_key: str | None = None) -> None:
        publish("ops", guild_key)

    def loot_saved(guild_key: str, session_id: str, run_key: str) -> None:
        publish(f"loot:{run_key}", guild_key)  # LootSession.save / archive (design §2.4: every save publishes through listeners)

    app.state.publish = publish  # the agents API and the tests reach the one fan-out through here
    from .. import ops as ops_mod, registry as registry_mod
    from ..loot import session as loot_session_mod

    def test_event(guild_key: str, run_key: str) -> None:
        publish(f"test:{run_key}", guild_key, officers=True)  # the test feed (testfeed.FEED): the Test bench card and the Rosters Test tab reload

    from .. import testfeed

    for hooks, fn in ((rc.RUN_LISTENERS, run_saved), (registry_mod.SAVE_LISTENERS, reg_saved), (ops_mod.LISTENERS, ops_line),
                      (loot_session_mod.LOOT_LISTENERS, loot_saved), (testfeed.LISTENERS, test_event)):
        hooks[:] = [f for f in hooks if getattr(f, "__name__", "") != fn.__name__] + [fn]  # one per process, the newest app wins

    def test_lookup(guild_key: str, run_key: str):
        """(reg, ev) for a saved run, so the test feed can observe a TEST run (testfeed.install)."""
        reg = bot.registries.by_key.get(guild_key) if getattr(bot, "registries", None) is not None else None
        ev = bot.raids.store(reg).events.get(run_key) if reg is not None else None
        return (reg, ev) if ev is not None else None

    def channel_name(cid):
        ch = bot.get_channel(int(cid)) if cid and hasattr(bot, "get_channel") else None
        return getattr(ch, "name", None)

    testfeed.install(test_lookup, channel_name)

    @app.get("/api/events")
    async def events(request: Request):
        """Server-sent events: `data: run:<key> | member | config | ops` whenever that changes. Pages reload on the topics they show."""
        from fastapi.responses import StreamingResponse

        v = await who(request)  # honours ?g= (EventSource can't send headers): the stream is that guild's
        q: asyncio.Queue = asyncio.Queue()
        subscribers[q] = (v.reg.key, bool(v.officer))

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
                subscribers.pop(q, None)

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
        async with solving(v.reg, ev.key):  # the run key, never the URL ref: an alias must hit the same single-flight
            try:
                layout, rosters = await asyncio.to_thread(rc.split_preview, v.reg, rs, ev, strategy, avoid or None)
            except RuntimeError as e:
                return JSONResponse({"error": solver_text(e)}, status_code=409)
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"solver: {e}"}, status_code=400)
        syn = [r.synergy_value for r in rosters]
        ev.last_proposal = {"layout": layout, "strategy": strategy, "rev": rc.board_rev(v.reg, ev), "by": v.name, "by_id": v.uid, "at": rc.now(), "kind": "split"}
        await asyncio.to_thread(rs.save, ev, f"split preview ({strategy}) by {v.name}")  # plain text's "use it" finds it after a restart
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
        async with solving(v.reg, ev.key):  # the run key, never the URL ref: an alias must hit the same single-flight
            ev.split_strategy = b.strategy
            rs.save(ev, f"split strategy {b.strategy}")
        return {"message": f"split strategy: {b.strategy}"}

    @app.post("/api/run/{key}/autofill")
    async def run_autofill(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked — move people on the board instead"}, status_code=400)
        async with solving(v.reg, ev.key):  # the run key, never the URL ref: an alias must hit the same single-flight
            rev = rc.board_rev(v.reg, ev)  # the board the solve fills around: a drag during the solve wins (409), never lost
            try:
                layout = await asyncio.to_thread(rc.autofill, v.reg, rs, ev)
            except RuntimeError as e:
                return JSONResponse({"error": solver_text(e)}, status_code=409)
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"solver: {e}"}, status_code=400)
            ev.log.append(f"{v.name}: auto-filled the board")
            try:  # the one board write (RaidMixin.save_board), as plain text's run_autofill
                await bot.save_board(v.reg, rs, ev, layout, rev=rev, by=v.name, via=v.via)
            except rc.BoardChanged:
                ev.log.pop()
                return stale(v, ev)
        return board_reply(v.reg, ev)

    @app.post("/api/run/{key}/pin")
    async def run_pin(request: Request, key: str):
        """Pin someone to the roster / keep them on the bench for the lock — inbox.pin_member, the one pin write (the
        board's row menu, the SetWizard and plain text's pin): saved, audited, the Discord sheet's pin footer re-rendered."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        pin = d.get("pin")
        m = resolve_member(v.reg, member_ref(d))
        if pin not in ("in", "out", None):
            return JSONResponse({"error": "pin must be in, out or null"}, status_code=400)
        if str(m.discord_id) not in ev.signups:
            return JSONResponse({"error": "they haven't answered this sheet"}, status_code=400)
        try:
            line = await inbox.pin_member(bot, v.reg, rs, ev, m, pin, by=v.name, by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

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
        note = str(d.get("note") or "").strip()[:300] or None
        if note and status != "out":
            return JSONResponse({"error": "a note goes with No thanks (status out): it is the reason they can't make it"}, status_code=400)
        try:  # set_answer / call_out refresh the sheet and cards and write the ops line themselves; the reply is Discord-flavoured
            if note or (status == "out" and d.get("callout")):  # a callout (/raid out's verb): with a reason (officers only), or MCP's No thanks
                line = await bot.call_out(v.reg, rs, ev, m, note, v.name, by_id=v.uid)
            else:
                line = await maybe_await(bot.set_answer(v.reg, rs, ev, m, status, d.get("character") or None, v.name, by_id=v.uid))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line.replace("**", "").removeprefix("✅ ")}

    @app.post("/api/run/{key}/lock")
    async def run_lock(request: Request, key: str):
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked"}, status_code=400)
        try:  # lock_run holds the run's solver single-flight and board lock itself
            line = await bot.lock_run(v.reg, rs, ev, by=v.name)
        except RegistryError as e:  # rc.SolverBusy (someone else is locking it), or no longer open
            return JSONResponse({"error": str(e)}, status_code=409)
        except RuntimeError as e:  # lock_run catches the solver itself; this is the belt to its braces
            return JSONResponse({"error": solver_text(e)}, status_code=409)
        if ev.lock_error:
            return JSONResponse({"error": solver_text(RuntimeError(ev.lock_error))}, status_code=409)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line}

    @app.post("/api/run/{key}/lock/preview")
    async def run_lock_preview(request: Request, key: str):
        """The lock modal's Preview (design B6): the roster(s) the lock would build NOW (RaidMixin.lock_preview —
        rc.split_preview with the run's strategy, the board and the pins; nothing saved but ev.last_proposal), as the
        board shows it, plus the consequence sentence the confirm prints and the leftovers. 409 when the solver is busy
        on this run or no roster satisfies the rules (the sentence says which)."""
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if ev.state != "open":
            return JSONResponse({"error": "already locked"}, status_code=400)
        try:
            rosters = await bot.lock_preview(v.reg, rs, ev, by=v.name, by_id=v.uid)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        left = rc.leftovers(v.reg, ev, rosters)
        return {"board": board_json(v.reg, ev, rosters, []), "consequence": " ".join(consequence_lines("lock", v.reg, ev)),
                "leftovers": [p.signup_name for p in left], "strategy": (ev.last_proposal or {}).get("strategy"), "rev": rc.board_rev(v.reg, ev)}

    def send_batch_json(reg, plan) -> list[dict]:
        try:
            return [ask_json(reg, a) for a in rc.send_batch(plan)[0]]
        except RegistryError:  # the cap is full: nothing can go out now (plan.batch_lines says why)
            return []

    @app.post("/api/run/{key}/fill/preview")
    async def run_fill_preview(request: Request, key: str):
        """What would be asked, without sending — before and after lock the planner's plan (`plan`: each seat's
        suggestion with its code-written reason and alternates, what stays short, `batch_lines`, the `rev` a send must
        carry) and `batch`: exactly what a send with that rev and no picks DMs now (rc.send_batch — after lock at most
        FILL_MAX_OPEN out, never a swap without its backfill)."""
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        plan = await asyncio.to_thread(rc.role_plan, v.reg, rs, ev)
        waiting = [a.display_name for a in ev.fill_asks if a.pending]
        short = plan.short if ev.state == "open" else rc.needs(v.reg, ev, t)
        return {"short": short, "waiting": waiting, "batch": send_batch_json(v.reg, plan), "plan": plan_json(v.reg, plan)}

    @app.post("/api/run/{key}/fill")
    async def run_fill(request: Request, key: str):
        """Send. With `rev` (the plan fingerprint the preview returned) → RaidMixin.send_plan with the officer's
        `picks` ({seat index: option index}; 0 = the suggestion): exactly the batch the preview showed; a plan that
        changed since is refused (409, the current plan in the reply — nothing sent). Without `rev`: refused before
        lock (400), and after lock refused with 409 "preview first" when the batch holds a role swap (an officer sees
        every swap before it goes out); a same-role-only batch is sent directly (the fill engine's batch, swaps off)."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        rev = str(d.get("rev") or "").strip()
        if not rev and ev.state != "open":
            batch = await asyncio.to_thread(rc.fill_batch, v.reg, rs, ev, t)  # what an officer's blind send would ask
            if any(a.swap for a in batch):
                plan = await asyncio.to_thread(rc.role_plan, v.reg, rs, ev)
                return JSONResponse({"error": "preview first — this batch holds a role swap; preview (send=false) and send with the returned rev",
                                     "plan": plan_json(v.reg, plan)}, status_code=409)
        if ev.state == "open" or rev:
            if not rev:
                return JSONResponse({"error": "preview the suggestions first — a send carries the plan's rev"}, status_code=400)
            try:
                sent, plan = await bot.send_plan(v.reg, rs, ev, rev=rev, by=v.name, picks=d.get("picks") or None)
            except rc.PlanChanged as e:
                return JSONResponse({"error": str(e), "plan": plan_json(v.reg, e.plan)}, status_code=409)
            except (RegistryError, ValueError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return {"message": (("asked " + ", ".join(f"{a.display_name} ({'swap' if a.swap else a.kind})" for a in sent)) if sent else ("nothing to ask" if not plan.asks else "nobody could be reached (DMs off)")) + rc.held_back_words(plan)}
        sent, nd = await bot.run_fill(v.reg, rs, ev, t, by=v.name, approved_by=v.name, swaps=False)  # same-role asks only: a swap needs the preview
        if sent:
            await bot.post_run_update(v.reg, ev, "🧩 " + "\n🧩 ".join(dr.ask_line(v.reg, bot.ico, a) for a in sent))
        return {"message": ("asked " + ", ".join(f"{a.display_name} ({'swap' if a.swap else a.kind})" for a in sent)) if sent else ("nothing to fill" if not (nd["headcount"] or nd["roles"]) else "nobody left to ask")}

    @app.post("/api/run/{key}/cancel")
    async def run_cancel(request: Request, key: str):
        """Cancel the run — the one verb every surface calls (RaidMixin.cancel_run): {reason?, notify?}. Nobody is DMed
        (the DM rule): the sheet is edited to its final state, every open ask card says Cancelled, `notify` (default true)
        posts one fixed line in the signup channel (never the reason, which stays in the run's thread and the ops feed).
        A done / cancelled run is refused. The returned line says what was done."""
        v, d = await body(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key) or find_live(v.reg, rs, key)
        notify = d.get("notify", True) not in (False, "false", "no", 0, "0")
        try:  # cancel_run re-renders the sheet and cards, closes every open ask and writes the audit line itself
            line = await maybe_await(bot.cancel_run(v.reg, rs, ev, v.name, (d.get("reason") or "").strip()[:300] or None, notify=notify, by_id=v.uid, via=v.via))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    @app.post("/api/run/{key}/reopen")
    async def run_reopen(request: Request, key: str):
        """Reopen a run ENDED by mistake, within the hour (design B6; RaidMixin.reopen_run). A cancelled run is refused
        (owner decision D19 — open a new one), as is one past the hour or its scheduled close."""
        v, _ = await body(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None:
            return JSONResponse({"error": f"no run {key}"}, status_code=404)
        try:
            line = await bot.reopen_run(v.reg, rs, ev, by=v.name, by_id=v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    @app.post("/api/run/{key}/remind")
    async def run_remind(request: Request, key: str):
        """Remind the unconfirmed (D15; RaidMixin.remind): the Confirm / Can't DM again for every unanswered ask, against
        the EXISTING ask; once per hour per run (409 with the time it may go again)."""
        v, _ = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        try:
            line = await bot.remind(v.reg, rs, ev, by=v.name, by_id=v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return {"message": line}

    @app.post("/api/run/{key}/pause")
    async def run_pause(request: Request, key: str):
        """Pause / resume the fill engine for this run: {paused: bool} (D18; RaidMixin.set_autofill_paused). Paused, the
        scheduler / a freed seat / an answer send nothing on their own; an officer's Fill still sends."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        if "paused" not in d:
            return JSONResponse({"error": "paused: true (pause) or false (resume)"}, status_code=400)
        try:
            line = await bot.set_autofill_paused(v.reg, rs, ev, d.get("paused") in (True, "true", 1, "1", "yes"), by=v.name, by_id=v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    @app.post("/api/run/{key}/withdraw")
    async def run_withdraw(request: Request, key: str):
        """Withdraw a sent ask (D15; RaidMixin.withdraw_sent_ask): {member} — their ask still out closes, the tied half
        is released, both DM cards say "Never mind". 400 when nothing is out for them."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        m = resolve_member(v.reg, member_ref(d))
        try:
            line = await bot.withdraw_sent_ask(v.reg, rs, ev, m.discord_id, by=v.name, by_id=v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    def open_args(reg, rid: str, d: dict) -> tuple[str | None, str | None]:
        """The open / preview body → (when, schedule) for rc.open_when: `start` (the preview's exact ISO instant, aware) wins
        over `when` ('YYYY-MM-DD HH:MM' guild wall time from the picker, resolved DST-safely); neither = the next run."""
        return ((d.get("start") or d.get("when") or "").strip() or None), ((d.get("schedule") or "").strip() or None)

    @app.post("/api/raid/{rid}/open/preview")
    async def raid_open_preview(request: Request, rid: str):
        """What Open would do, nothing written (design B4; rc.open_preview — the /raid open wizard says the same):
        {kind: scheduled | adhoc, schedule, schedule_name, key, start (ISO: carry it back to Open), label (12-hour guild
        time), when_note (the DST line), exists, channel, rules, past}. Body as Open's: {when? | start?, schedule?}."""
        v, d = await body(request, officer=True)
        if rid not in v.reg.profile.raids:
            return JSONResponse({"error": "unknown raid"}, status_code=400)
        when, sid = open_args(v.reg, rid, d)
        try:
            p = rc.open_preview(v.reg, bot.raids.store(v.reg), rid, when, sid)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        ch = bot.get_channel(int(p["channel"])) if p.get("channel") else None
        p["channel_name"] = getattr(ch, "name", None)
        return p

    @app.post("/api/raid/{rid}/open")
    async def raid_open(request: Request, rid: str):
        """Open a sheet now: the raid's next run (of `schedule` when given, else of any schedule), or a picked time —
        `start` (the preview's exact instant) or `when` ('YYYY-MM-DD HH:MM' guild time from the picker; DST-safe through
        rc.open_when / wizard.resolve_local, never replace(tzinfo=)). With `schedule`: that schedule's settings (a pickup
        template); without: the upcoming run at exactly that time if there is one, else a one-off (ad hoc). `key` (from
        the preview): the run Open is expected to make — refused (409) when the time now resolves to another run."""
        v, d = await body(request, officer=True)
        reg = v.reg
        if rid not in reg.profile.raids:
            return JSONResponse({"error": "unknown raid"}, status_code=400)
        when, sid = open_args(reg, rid, d)
        try:
            start, sid, _note = rc.open_when(reg, rid, when, sid)
            rc.refuse_past_start(reg, start, reg.now_local())  # a mistyped year would open a run the scheduler may close at once
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        expect = (d.get("key") or "").strip()
        if expect:
            got = rc.which_run(reg, rid, start, sid if sid is not None else "default")["key"]
            if got != expect:
                return JSONResponse({"error": "That time now opens a different run than the preview showed — check it and open again.",
                                     "preview": rc.open_preview(reg, bot.raids.store(reg), rid, start.isoformat(), sid)}, status_code=409)
        rs = bot.raids.store(reg)
        try:  # what /raid open does: open (or find) the run, post the sheet, write the ops line
            ev = await maybe_await(bot.open_run_and_post(reg, rs, rid, start, v.name, **rc.schedule_kw(sid)))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": f"opened {ev.key}" + ("" if ev.message_id else " (no signup channel set — sheet not posted)"), "key": ev.key}

    @app.post("/api/admin/confirm")
    async def admin_confirm(request: Request):
        """Confirm one character: {label, uid | member} (the member names whose — two members may share a first name; a
        bare label still resolves guild-wide). Your own character is refused (the owner aside)."""
        v, d = await body(request, officer=True)
        try:
            label = parse(d, LabelBody).label
            if member_ref(d) not in (None, ""):
                m = resolve_member(v.reg, member_ref(d))
            else:
                hit = v.reg.find_unique(label)  # a planned label several members share is refused, never guessed
                if not hit:
                    raise RegistryError(f"No character named {label}.")
                m = hit[0]
            c = next((x for x in m.active() if x.label == label), None) or next((x for x in m.active() if x.matches(label)), None)
            if c is None:
                raise RegistryError(f"{m.display_name} has no active character named {label}.")
            if c.confirmed_by:
                raise RegistryError(f"{c.label} was already confirmed by {c.confirmed_by}.")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            return await refused(v, request, e)
        out = await save_table(request, v, m.discord_id, [{"label": c.label, "confirm": True}], officer=True, rev=None, preview=False)  # THE character verb
        return out if isinstance(out, JSONResponse) else {**out, "message": f"confirmed {c.label}"}

    @app.post("/api/admin/placement")
    async def admin_placement(request: Request):
        """Officer answers a Confirm / Can't make it ask on someone's behalf (what the test bench's may_answer_for
        allows on the buttons): same verb and ripple as the member pressing it."""
        v, d = await body(request, officer=True)
        m = resolve_member(v.reg, member_ref(d))
        rs, ev, t = live_event(v.reg, str(d.get("run") or d.get("roster") or ""))
        yes = d.get("answer") in ("yes", True, "true")
        try:  # the member's Confirm / Can't make it buttons' own verb (plain text's confirm_for uses it too)
            line = await bot.answer_placement_for(v.reg, m.discord_id, ev.team, yes, v.name, by_id=v.uid)
        except (RegistryError, ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "bad request"}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"answered for {m.display_name}: {line}", via=v.via, by_id=v.uid)
        return {"message": line}

    @app.post("/api/admin/fill-answer")
    async def admin_fill_answer(request: Request):
        """Officer answers a FILL ask (the seat / swap question the fill engine DMed) on someone's behalf:
        {run, member, yes} → RaidMixin.answer_fill_for, the fill DM buttons' own verb (tied partner released on a no,
        the next batch asked, thread line, sheet and cards)."""
        v, d = await body(request, officer=True)
        m = resolve_member(v.reg, member_ref(d))
        rs, ev, t = live_event(v.reg, str(d.get("run") or ""))
        if "yes" not in d:
            return JSONResponse({"error": "yes: true (they take it) or false (they can't)"}, status_code=400)
        try:
            line = await bot.answer_fill_for(v.reg, rs, ev, m, d.get("yes") in (True, "true", "yes", 1), v.name, by_id=v.uid)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"answered a fill ask for {m.display_name}: {line}", via=v.via, by_id=v.uid)
        return {"message": line}

    def channel_ref(reg, value) -> int | None:
        """A channel given as an id, <#id> or a name (#raids) → its id; None when empty. Unknown / ambiguous → 400."""
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.strip("<#>").isdigit():
            return int(raw.strip("<#>"))
        want = raw.lstrip("#").lower()
        hits = [c for c in bot.guild_channels(reg) if c["name"].lower() == want]
        if len(hits) != 1:
            raise HTTPException(400, f"no channel named #{want}" if not hits else f"#{want}: several channels match")
        return int(hits[0]["id"])

    @app.post("/api/run/{key}/sheet")
    async def run_sheet(request: Request, key: str):
        """Re-post a live sheet (/raid sheet's verb, RaidMixin.repost_sheet): {channel?} = a channel id or name,
        default the signup channel. The sheet's buttons and later refreshes follow the new message."""
        v, d = await body(request, officer=True)
        rs, ev, t = live_event(v.reg, key)
        cid = channel_ref(v.reg, d.get("channel")) or v.reg.config.signup_channel_id
        if not cid:
            return JSONResponse({"error": "no signup channel is set: name the channel to post in"}, status_code=400)
        channel = bot.get_channel(int(cid))
        if channel is None:
            return JSONResponse({"error": "I can't see that channel"}, status_code=400)
        try:
            line = await bot.repost_sheet(v.reg, rs, ev, channel)  # refuses a channel of another server
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line}

    # ---- the test bench (dual interfaces phase 2, B9): /api/test/* are the same verbs as /gm test … (SetupMixin.test_seed /
    # test_answer / test_compose / test_comp / test_bench_clear, RaidMixin.open_test_run) and MCP's test_bench; GET
    # /api/test is the bench's state, GET /api/test/feed the live feed (testfeed.FEED: every card, puppet DM, step and
    # answer of a test run; SSE topic `test:<run key>`). /api/admin/test stays as the old one-route form.
    def test_runs(reg, rs) -> list:
        return [e for e in rs.live() if rc.is_test_run(reg, e)]

    def test_run_row(reg, e) -> dict:
        return {"key": e.key, "raid": e.instance, "title": f"{reg.raid_def(e.instance).get('name', e.instance)} · {t12(reg, e.starts_at)}",
                "state": e.state, "when": t12(reg, e.starts_at), "joined": len(e.by_status("in")), "rostered": len(e.seated()) if e.all_rosters else 0,
                "posted": bool(e.message_id), "asks": len(bot.puppet_asks(reg, e)) if e.state != "open" and hasattr(bot, "puppet_asks") else 0}

    def test_target(reg, rs, ref) -> rc.RaidEvent:
        """The test run a request names (key, roster key or raid), else the only live test run; a real run is refused."""
        if ref not in (None, ""):
            ev = find_live(reg, rs, str(ref))
            if not rc.is_test_run(reg, ev):
                raise HTTPException(400, f"{ev.key} is a real run: test members answer test runs only")
            return ev
        live = test_runs(reg, rs)
        if not live:
            raise HTTPException(400, "no live test run: open one first")
        if len(live) > 1:
            raise HTTPException(400, "several live test runs — name one: " + ", ".join(e.key for e in live))
        return live[0]

    async def test_seed_do(v, d) -> dict:
        count = as_int(d.get("count", 20), "count")  # 0 = the whole built-in roster; above it is capped to it
        line = await bot.test_seed(v.reg, count, v.name, via=v.via, by_id=v.uid)
        return {"message": line, "members": len(v.reg.test_members())}

    async def test_run_do(v, d) -> dict:
        from .. import testfeed

        reg, rs = v.reg, bot.raids.store(v.reg)
        rid = str(d.get("raid") or "")
        if rid not in reg.profile.raids:
            return JSONResponse({"error": f"unknown raid; options: {', '.join(reg.profile.raids)}"}, status_code=400)
        try:  # tempo: a presets.TEMPOS name or minutes to the start; without it the four marks (the old form)
            _label, start_in, nudge_in, lock_in, confirm_in = testfeed.tempo_of(d.get("tempo"), start_in=d.get("start_in"), nudge_in=d.get("nudge_in"),
                                                                                lock_in=d.get("lock_in"), confirm_in=d.get("confirm_in"))
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:  # RaidMixin.open_test_run: the wizard's and plain text's opener; from the site it needs the signup channel (no channel of its own)
            ev, _posted = await bot.open_test_run(reg, rs, rid, start_in=start_in, nudge_in=nudge_in, lock_in=lock_in, confirm_in=confirm_in,
                                                  dm=bool(d.get("dm_open") or d.get("dm")), by=v.name, by_id=v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": f"test run {ev.key} open: starts {t12(reg, ev.starts_at)}, nudge {nudge_in} min before, lock {lock_in} min before, confirm by {confirm_in} min before",
                "key": ev.key}

    async def test_answer_do(v, d) -> dict:
        reg, rs = v.reg, bot.raids.store(v.reg)
        ev = test_target(reg, rs, d.get("run"))
        m = resolve_member(reg, member_ref(d)) if member_ref(d) not in (None, "") else None
        try:
            done = await bot.test_answer(reg, rs, ev, member=m, status=str(d.get("status") or "in") if m else None,
                                         join=as_int(d.get("join", 0), "join"), bench=as_int(d.get("bench", 0), "bench"), out=as_int(d.get("out", 0), "out"),
                                         yes=as_int(d.get("yes", 0), "yes"), no=as_int(d.get("no", 0), "no"), by=v.name, by_id=v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": ("; ".join(done)) if done else "nobody left to answer (seed more, or they've all answered)", "key": ev.key, "answered": len(done)}

    async def test_clear_do(v, d) -> dict:
        from ..flows_misc import test_clear_consequence

        lines = test_clear_consequence(v.reg, bot.raids.store(v.reg))  # the decide screen's words: the browser never composes them
        if d.get("preview"):
            return {"consequence": lines, "empty": lines[0].startswith("Nothing to clear")}
        line = await bot.test_bench_clear(v.reg, v.name, via=v.via, by_id=v.uid)  # the verb writes its own audit line
        return {"message": line, "consequence": lines}

    async def test_compose_do(v, d) -> dict:
        reg, rs = v.reg, bot.raids.store(v.reg)
        try:
            line = await bot.test_compose(reg, rs, d.get("add") or [], [int(u) for u in d.get("remove") or []], v.name, via=v.via, by_id=v.uid)
        except (RegistryError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    async def test_comp_do(v, d) -> dict:
        reg, rs = v.reg, bot.raids.store(v.reg)
        try:
            ev = await bot.test_comp(reg, rs, str(d.get("raid") or ""), v.name, v.uid, via=v.via)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": f"comp sandbox {ev.key}: {len(reg.test_members())} test members joined — Propose roster on its board builds the comp", "key": ev.key}

    TEST_ACTIONS = {"seed": test_seed_do, "run": test_run_do, "answer": test_answer_do, "clear": test_clear_do, "compose": test_compose_do, "comp": test_comp_do}

    @app.get("/api/test")
    async def test_state(request: Request):
        """The bench as the site's Test bench card shows it: puppets, live test runs, whether a test run can open from
        the site (the signup channel, named), the tempos, the seed presets and the clear consequence (from code)."""
        from ..flows_misc import SEED_COUNTS, test_clear_consequence
        from ..presets import DEFAULT_TEMPO, TEMPOS

        v = await who(request, officer=True)
        reg, rs = v.reg, bot.raids.store(v.reg)
        from ..discord_raid import bench_channel_refusal

        sid = reg.config.signup_channel_id
        get_channel = getattr(bot, "get_channel", None) or (lambda _id: None)
        ch = get_channel(int(sid)) if sid else None
        n = len(reg.test_roster())
        return {"members": len(reg.test_members()), "max": reg.TEST_MAX, "roster": n,
                "seed": [{"count": c, "label": str(c)} for c in SEED_COUNTS if c < n] + [{"count": 0, "label": f"all {n}"}],
                "runs": [test_run_row(reg, e) for e in test_runs(reg, rs)],
                "signup_channel": {"set": bool(sid), "name": getattr(ch, "name", None)},
                "open_refusal": bench_channel_refusal(reg, get_channel),  # the same words open_test_run refuses with
                "tempos": [{"id": k, "label": t[0], "start_in": t[1], "nudge_in": t[2], "lock_in": t[3], "confirm_in": t[4]} for k, t in TEMPOS.items()],
                "default_tempo": DEFAULT_TEMPO, "clear_consequence": test_clear_consequence(reg, rs)}

    @app.get("/api/test/feed")
    async def test_feed(request: Request, run: str | None = None, after: int = 0):
        """The test feed: every card, puppet DM, scheduler step and answer of the guild's test runs, oldest first
        (`run` = one run's; `after` = only events newer than that seq). In this process only — nothing is stored."""
        from .. import testfeed

        v = await who(request, officer=True)
        return {"events": testfeed.FEED.events(v.reg.key, run or None, after=int(after or 0)), "runs": testfeed.FEED.runs(v.reg.key)}

    @app.post("/api/test/seed")
    async def test_seed(request: Request):
        """{count}: 5 / 10 / 20 / 0 = the whole bench (existing puppets are kept)."""
        v, d = await body(request, officer=True)
        return await test_seed_do(v, d)

    @app.post("/api/test/run")
    async def test_run(request: Request):
        """{raid, tempo (quick | standard | slow, or minutes to the start), dm}: open a test run now — refused without a
        signup channel (the site has no channel of its own), naming the setting."""
        v, d = await body(request, officer=True)
        return await test_run_do(v, d)

    @app.post("/api/test/answer")
    async def test_answer(request: Request):
        """{run?, member?, status?} one puppet (in | sub | out on the sheet; yes | no answers their open ask), or a random
        mix {join, bench, out, yes, no}. Test runs only."""
        v, d = await body(request, officer=True)
        return await test_answer_do(v, d)

    @app.post("/api/test/clear")
    async def test_clear(request: Request):
        """{preview: true} → the consequence lines (the confirm reads them); otherwise cancel every test run and delete
        every puppet."""
        v, d = await body(request, officer=True)
        return await test_clear_do(v, d)

    @app.post("/api/test/compose")
    async def test_compose(request: Request):
        """{add: [{cls, spec, offspec?, count}], remove: [puppet uid]}: the bench by composition."""
        v, d = await body(request, officer=True)
        return await test_compose_do(v, d)

    @app.post("/api/test/comp")
    async def test_comp(request: Request):
        """{raid}: the comp sandbox (every puppet joined to a test run two weeks out, never posted)."""
        v, d = await body(request, officer=True)
        return await test_comp_do(v, d)

    @app.post("/api/admin/test")
    async def admin_test(request: Request):
        """The test bench in one route: {action: seed | run | answer | clear | compose | comp, …} — the /api/test/* bodies."""
        v, d = await body(request, officer=True)
        fn = TEST_ACTIONS.get(str(d.get("action") or ""))
        if fn is None:
            return JSONResponse({"error": "action must be seed, run, answer, clear, compose or comp"}, status_code=400)
        return await fn(v, d)

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
                        "weights": dict(eff["weights"]), "split_policy": eff.get("split_policy", "balanced"), "nudge": bool(eff.get("nudge", True)), "nudge_hours_before": eff["nudge_hours_before"], "fill_ask_hours": eff["fill_ask_hours"], "autofill": bool(eff.get("autofill", True)), "open_dm": bool(eff.get("open_dm", False)), "confirm": bool(eff.get("confirm", True)), "notes": eff.get("notes") or "", "comp": {r: dict((eff.get("comp") or {}).get(r) or {}) for r in ("tank", "healer", "dps")},
                        "overridden": sorted(k for k in over if k not in ("comp", "weights")) + [f"{r}_{b}" for r, bb in ((over.get("comp") or {}).items()) for b in bb] + [f"weight_{k}" for k in (over.get("weights") or {})],
                        "comp_targets": over.get("comp_targets") or {}, "comp_groups": over.get("comp_groups") or [],
                        "first_open_local": fo.astimezone(z).strftime("%Y-%m-%dT%H:%M") if fo else "", "opened": bool(fo and fo <= now),
                        "window": [reg.local12(ws), reg.local12(we)], "live": sum(1 for e in rs.live() if e.instance == rid),
                        "schedules": [schedule_json(reg, rid, s) for s in reg.schedules(rid)]})
        return {"raids": out, "tz": reg.config.timezone, "owner": v.owner, "weight_keys": list(RAID_WEIGHT_DEFAULTS), "split_policies": list(SPLIT_POLICIES),
                "schedule_kinds": list(SCHEDULE_KINDS), "schedule_cadence": list(SCHEDULE_CADENCE), "max_rosters": MAX_SCHEDULE_ROSTERS}

    @app.post("/api/admin/raid/schedule")
    async def admin_raid_schedule(request: Request):
        """Change or create one schedule (owner): {instance, id, fields: {setting: value}} — a cadence value of null goes
        back to the raid's. One registry write (Registry.set_schedule): validated as a whole, one commit."""
        def go(v, d):
            need_owner(v)
            inst, sid = parse(d, InstanceBody).instance, str(d.get("id") or "").strip()
            fields = d.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise HTTPException(400, "fields: a map of schedule settings to change")
            return "; ".join(v.reg.set_schedule(inst, sid, fields, v.name)) or f"{inst}: no changes"
        return await run(request, go, officer=True)

    @app.post("/api/admin/raid/schedule/remove")
    async def admin_raid_schedule_remove(request: Request):
        def go(v, d):
            need_owner(v)
            return v.reg.remove_schedule(parse(d, InstanceBody).instance, str(d.get("id") or ""), v.name)
        return await run(request, go, officer=True)

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
        """Every member (characterless ones too — an officer may still record an absence, switch DMs or see a pending
        ask), their characters, rank (D25: the member's, on the main), DM setting, upcoming absences and the table's base
        rev. Test puppets only for the officers who may change them (the live rehearsal's tester, the owner)."""
        v = await who(request, officer=True)
        reg = v.reg
        ms = sorted((m for m in reg.members.values() if visible(v, m)), key=lambda m: m.display_name.lower())
        privs = await asyncio.gather(*(privilege(reg, m.discord_id) if not m.test else asyncio.sleep(0, "test") for m in ms))
        today = reg.now_local().date().isoformat()
        rs = bot.raids.store(reg)
        rows = []
        for m, p in zip(ms, privs):
            chars = sorted(m.active(), key=lambda c: (not c.is_main, c.created_at))
            rows.append({"uid": str(m.discord_id), "display_name": m.display_name, "verification": reg.verification(m.discord_id), "privilege": "test" if m.test else p,
                         "rank": m.rank, "dm": not m.dm_opt_out, "characters_rev": reg.characters_rev(m),
                         "characters": [char_json(reg, c, seat_runs(reg, rs, m)) for c in chars], "absences": [absence_json(reg, a) for a in m.upcoming_absences(today)],
                         "asks": [{"roster": a["roster"], "answer": a.get("answer"), **run_label(reg, rs, a["roster"])} for a in m.placement_asks[-3:] if a.get("answer") is None]})
        return {"rows": rows, "members": len(reg.members), "tz": reg.config.timezone}

    @app.post("/api/members/save")
    async def members_save(request: Request):
        """One save for the whole table: per member {uid | member, characters, deletes, rev} through THE character verb
        (officer: rank on the main and confirm allowed; your own rank refused). Members whose `rev` moved are refused and
        listed in `refused` (their rows stay drafted on the page); the others apply. `preview`: the ripple per member."""
        v, d = await body(request, officer=True)
        reg, done, ripple, refused_rows = v.reg, [], [], []
        preview = bool(d.get("preview"))
        people_actor(v, [c for row in d.get("rows") or [] if isinstance(row, dict) for c in (row.get("characters") or [])], preview)  # before any member saves
        for row in d.get("rows") or []:
            if not isinstance(row, dict):
                return JSONResponse({"error": "rows must be objects"}, status_code=400)
            try:
                m = resolve_member(reg, row["member"]) if row.get("member") else reg.members.get(as_int(row.get("uid"), "uid"))
                rows = [{"label": str(label), "delete": True} for label in row.get("deletes") or []] + table_rows(row.get("characters"))
            except HTTPException as e:
                return JSONResponse({"error": e.detail}, status_code=e.status_code)
            if not m or not rows:
                continue
            out = await save_table(request, v, m.discord_id, rows, officer=True, rev=row.get("rev"), preview=preview)
            if isinstance(out, JSONResponse):
                import json as _json

                err = _json.loads(out.body)
                refused_rows.append({"uid": str(m.discord_id), "display_name": m.display_name, "error": err.get("error"), "stale": out.status_code == 409})
                continue
            done.extend(f"{line} ({m.display_name})" for line in out["lines"])
            ripple.extend(f"{m.display_name}: {r}" for r in out["ripple"])
        head = ("preview — nothing saved" if preview else "; ".join(done) or "no changes")
        if refused_rows and not done and not ripple and not preview:
            return JSONResponse({"error": "; ".join(f"{r['display_name']}: {r['error']}" for r in refused_rows), "refused": refused_rows}, status_code=409 if all(r["stale"] for r in refused_rows) else 400)
        return {"message": head + "".join(f"\n{r}" for r in ripple) + "".join(f"\n⛔ {r['display_name']}: {r['error']}" for r in refused_rows),
                "lines": done, "ripple": ripple, "refused": refused_rows, "preview": preview}

    @app.get("/api/member")
    async def member_get(request: Request, name: str = ""):
        """One member by display name, character name or id (`?name=`): characters, every absence on file, open asks, DM setting."""
        v = await who(request, officer=True)
        reg = v.reg
        m = resolve_member(reg, name)
        if not visible(v, m):  # another officer's rehearsal puppet: hidden like on /api/members and /api/absences
            raise HTTPException(400, "unknown member")
        rs = bot.raids.store(reg)
        primary, flex = reg.roles_of(m)
        return {"uid": str(m.discord_id), "display_name": m.display_name, "verification": reg.verification(m.discord_id), "privilege": "test" if m.test else await privilege(reg, m.discord_id),
                "test": bool(m.test), "dm": not m.dm_opt_out, "roles": {"primary": primary, "flex": list(flex)}, "rank": m.rank, "characters_rev": reg.characters_rev(m),
                "characters": [char_json(reg, c, seat_runs(reg, rs, m)) for c in sorted(m.active(), key=lambda c: (not c.is_main, c.created_at))],
                "absences": [absence_json(reg, a) for a in sorted(m.absences, key=lambda a: a.start)],
                "asks": [{"roster": a["roster"], "answer": a.get("answer"), "asked_at": a.get("asked_at"), **run_label(reg, rs, a["roster"])} for a in m.placement_asks[-5:]],
                "sheets": [{"key": e.key, "status": e.signups[str(m.discord_id)].status, "character": e.signups[str(m.discord_id)].character} for e in rs.live() if str(m.discord_id) in e.signups]}

    @app.get("/api/absences")
    async def absences(request: Request, all: bool = False):
        """Every upcoming absence (`?all=true` = past ones too), by start, with the member, the span in words, the reason
        and who recorded it (officers only). Test puppets only for the officers who may change them."""
        v = await who(request, officer=True)
        reg = v.reg
        today = reg.now_local().date().isoformat()
        rows = [{"uid": str(m.discord_id), "display_name": m.display_name, "start": a.start, "end": a.end, "span": reg.span_label(a.start, a.end), "reason": a.reason,
                 "by": a.by, "current": a.start <= today <= a.end, "test": bool(m.test)}
                for m in reg.members.values() if visible(v, m) for a in (m.absences if all else m.upcoming_absences(today))]
        return {"rows": sorted(rows, key=lambda r: (r["start"], r["display_name"])), "today": today, "tz": reg.config.timezone}

    @app.post("/api/members/set")
    async def members_set(request: Request):
        """Officer edits of one member the way the Discord commands do: `rank` (D25: the member's, on the main — a
        `character` that is an alt is refused), `confirm` (true: `character`, default the main, is verified), `main` (a
        character becomes the main; the rank moves with it). Any subset, as ONE character-table save through THE character
        verb (discord_people.save_characters: under the registry's lock, validated on a copy, one audit line) — main first,
        then rank, then confirm. A `character` that isn't one of theirs is refused, never read as the main; your own rank
        and your own characters' confirm are refused (the owner aside)."""
        v, d = await body(request, officer=True)
        reg = v.reg
        try:
            m = resolve_member(reg, member_ref(d))
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=e.status_code)

        def own(ref: str):
            c = next((x for x in m.active() if x.label == ref), None) or next((x for x in m.active() if x.matches(ref)), None)
            if c is None:
                raise RegistryError(f"{m.display_name} has no active character named {ref or '?'} (theirs: {', '.join(x.label for x in m.active()) or 'none'}).")
            return c

        rows: list[dict] = []
        notes: list[str] = []
        try:
            ref = str(d.get("character") or "").strip()
            named = own(ref) if ref else None
            main = m.main
            if d.get("main"):
                c = own(str(d["main"]).strip()) if isinstance(d["main"], str) else (named or main)
                if c is None:
                    raise RegistryError(f"{m.display_name} has no main yet — name the character that becomes it.")
                if not c.is_main:
                    rows.append({"label": c.label, "main": True})
                main, named = c, named or c
            if d.get("rank"):
                c = named or main
                if c is None:
                    raise RegistryError(f"{m.display_name} has no main yet — rank lives on the main.")
                if c is not main:
                    raise RegistryError(f"{c.label} is an alt — rank lives on the main ({main.label if main else 'none yet'}).")
                rows.append({"label": c.label, "rank": str(d["rank"]).strip().lower()})
            if d.get("confirm"):
                c = named or main
                if c is None:
                    raise RegistryError(f"{m.display_name} has no main yet — name the character to confirm.")
                if c.confirmed_by:  # a "set" is declarative: already confirmed is already done
                    notes.append(f"{c.label} already confirmed by {c.confirmed_by}")
                else:
                    rows.append({"label": c.label, "confirm": True})
        except Exception as e:  # noqa: BLE001
            return await refused(v, request, e)
        if not rows:
            return {"message": "; ".join(notes) or f"{m.display_name}: no changes"}
        out = await save_table(request, v, m.discord_id, rows, officer=True, rev=None, preview=False)
        if isinstance(out, JSONResponse):
            return out
        return {**out, "message": "; ".join([out["message"], *notes])}

    @app.post("/api/members/dm")
    async def members_dm(request: Request):
        """Officer switches someone's DMs on/off (the Me page's toggle, for them): Registry.set_dm."""
        def go(v, d):
            m = resolve_member(v.reg, member_ref(d))
            return v.reg.set_dm(m.discord_id, bool(d.get("on")), v.name)
        return await run(request, go, officer=True)

    @app.post("/api/members/absence")
    async def members_absence(request: Request):
        """Officer records an absence for someone (same verb and ripple as the member doing it): {member | uid, start,
        days | end, reason, preview}. MCP add_absence(preview=true) reads the same preview."""
        v, d = await body(request, officer=True)
        return await absence_route(request, v, d, resolve_member(v.reg, member_ref(d)))

    @app.post("/api/members/absence/clear")
    async def members_absence_clear(request: Request):
        """Officer: someone is back (same verb as the member's I'm back: the sheets re-open for them; the lines say which)."""
        v, d = await body(request, officer=True)
        m = resolve_member(v.reg, member_ref(d))
        return await clear_absence(request, v, m.discord_id, parse(d, StartBody).start, bool(d.get("preview")))

    # ---- the owner (design §3.5 O-03, D50): the Config page's transfer; /gm config owner is the Discord one
    @app.get("/api/owner")
    async def owner_get(request: Request):
        """Who owns the guild and, for the owner (or a host superuser), who could take over: the registered real members."""
        v = await who(request)
        if not (v.officer or v.owner):
            raise HTTPException(403, "Officers only.")
        reg = v.reg
        oid = reg.config.owner_discord_id
        from .app import member_name

        out = {"owner": {"uid": str(oid), "name": member_name(bot, reg, int(oid))} if oid else None, "may_transfer": bool(v.owner and v.via == "web")}
        if out["may_transfer"]:
            out["candidates"] = [{"uid": str(m.discord_id), "name": m.display_name} for m in sorted(reg.members.values(), key=lambda m: m.display_name.lower())
                                 if not m.test and m.discord_id != oid and m.discord_id != v.uid]
        return out

    @app.post("/api/owner")
    async def owner_set(request: Request):
        """Hand the guild over: {member | uid, preview}. The owner (or a host superuser) only, from the site; never MCP —
        the token IS the owner and can't hand itself over. Preview = THE confirm sentence (Registry.owner_consequence) and
        the refusals (a bot, the owner, yourself, someone outside the server); then discord_people.transfer_owner."""
        from .. import discord_people
        from .app import member_name

        v, d = await body(request)
        if v.via != "web":
            return JSONResponse({"error": "Ownership is handed over by the owner in person (the Config page or /gm config owner) — MCP acts as the owner and can't hand itself over."}, status_code=403)
        if not v.owner:
            return JSONResponse({"error": "Only the owner can hand the guild over."}, status_code=403)
        reg = v.reg
        try:
            m = resolve_member(reg, member_ref(d))
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=e.status_code)
        name = m.display_name or member_name(bot, reg, m.discord_id)
        if d.get("preview"):
            from .. import guild_routing

            answer, member = await guild_routing.member_of(bot, reg, m.discord_id, timeout=None)
            why = reg.owner_refusal(m.discord_id, by_id=v.uid, bot=bool(getattr(member, "bot", False)), membership=answer, name=name)
            if why:
                return JSONResponse({"error": why}, status_code=400)
            return {"preview": True, "message": reg.owner_consequence(name, v.uid).replace("**", "")}
        try:
            line = await discord_people.transfer_owner(bot, reg, m.discord_id, v.name, by_id=v.uid, name=name, via=v.via)
        except Exception as e:  # noqa: BLE001
            return await refused(v, request, e)
        return {"message": line}

    # ---- ops, config (read-only)
    @app.get("/api/ops")
    async def ops(request: Request, changes_limit: int = 50):
        """Bot health: status, the ops feed and the registry change lines (`changes`, newest first, since the bot started —
        the analytics channel's change log). The loot precedents and the award log live on the Loot pages now
        (GET /api/loot/precedents, /api/loot/log — design §3.1: Ops keeps bot health only)."""
        from ..discord_feed import feed_status
        from ..registry import recent_changes

        v = await who(request, officer=True)
        st, prov = bot.registries.store, getattr(getattr(bot, "ctx", None), "provider", None)
        started = float(getattr(bot, "started_at", STARTED))
        return {"head": st.head(), "push": st.push_enabled, "llm": prov.summary() if prov else "off", "feed": feed_status(bot, v.reg, off="disabled"),
                "up": int(time.time() - started), "started_at": started,
                # several guilds: data repo, LLM and uptime are the whole bot's, not this guild's (K17: absent for one)
                **({"scope": "bot"} if getattr(bot.registries, "many", False) else {}),
                "rows": [{"time": t, "level": lvl, "text": text} for t, lvl, text in reversed(bot.ops.recent_for(v.reg.key))],
                "changes": [{**c, "time_label": v.reg.local12(c["time"])} for c in recent_changes(v.reg.key, max(0, min(int(changes_limit), 200)))]}

    @app.get("/api/config")
    async def config(request: Request):
        v = await who(request, officer=True)
        import yaml

        from ..discord_pool import CHANNEL_KINDS
        from ..policy import PolicyStore
        from .api_loot import loot_log_state

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
                # `missing`: set, but not a channel the bot can see any more (deleted, or its permissions changed)
                "channels": {k: {"id": str(getattr(cfg, attr)) if getattr(cfg, attr) else None, "name": chans.get(str(getattr(cfg, attr))),
                                 "missing": bool(getattr(cfg, attr)) and guild is not None and str(getattr(cfg, attr)) not in chans} for k, attr in CHANNEL_KINDS.items()},
                "guild_channels": bot.guild_channels(reg), "guild_roles": guild_roles,
                "settings": {"timezone": cfg.timezone, "ask_audience": cfg.ask_audience, "about": cfg.about or "", "officer_roles": officer_roles, "news_keywords": list(cfg.news_keywords),
                             "loot_log": bool(cfg.loot_log), "loot_log_state": loot_log_state(reg),
                             "officer_roles_pending": cfg.officer_roles_pending(), "owner_id": str(cfg.owner_discord_id) if cfg.owner_discord_id else None,
                             "member_roles": [{"id": str(rid), "name": reg.role_names.get(rid, f"role:{rid}")} for rid in cfg.member_role_ids]},
                "test_bench": {"members": len(reg.test_members()), "runs": [e.key for e in bot.raids.store(reg).live() if (cfg.roster(e.team) or {}).get("test")]}, "owner": v.owner,
                "guild": {"key": reg.key, "name": cfg.name, "discord_guild_id": str(cfg.discord_guild_id)}}  # which guild answered (MCP guild_overview)

    # ---- policy documents (loot, comp, persona): edit → compile → confirm | discard, what /gm policy edit does
    def policy_store(reg):
        from ..policy import PolicyStore

        return PolicyStore(bot.registries.store, reg.key)

    def policy_doc(doc: str) -> str:
        from ..policy import DOCS

        if doc not in DOCS:
            raise HTTPException(400, f"doc is one of {', '.join(DOCS)}")
        return doc

    def policy_json(reg, doc: str) -> dict:
        from .. import policy as pol

        ps = policy_store(reg)
        c = ps.compiled(doc)
        p = pol.pending(ps, doc)
        return {"doc": doc, "text": ps.read(doc), "compiled": bool(c), "summary": c.get("summary") if isinstance(c, dict) else None,
                "pending": bool(p), "pending_compiled": bool(p and p.get("compiled") is not None)}

    @app.get("/api/policy/{doc}")
    async def policy_get(request: Request, doc: str):
        """One policy document: its text, whether a compiled reading is live (and its summary), and whether an edit is pending."""
        v = await who(request, officer=True)
        return policy_json(v.reg, policy_doc(doc))

    @app.put("/api/policy/{doc}")
    async def policy_put(request: Request, doc: str):
        """Officers: {text} replaces the document as a DRAFT (the live compiled reading is unchanged until confirmed;
        the persona is live as written). Next: compile, then confirm or discard (policy.begin_edit)."""
        from .. import policy as pol

        v, d = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            line = await asyncio.to_thread(pol.begin_edit, policy_store(v.reg), doc, str(d.get("text") or ""), v.name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

    @app.post("/api/policy/{doc}/compile")
    async def policy_compile(request: Request, doc: str):
        """Officers: the bot's reading of the document as it is now (an LLM call; nothing goes live). `reading` is the
        rules/constraints in words plus the questions it could not settle; confirm makes it live, discard reverts."""
        from .. import policy as pol

        v, _ = await body(request, officer=True)
        doc = policy_doc(doc)
        provider = getattr(getattr(bot, "ctx", None), "provider", None)
        try:
            compiled, reading = await asyncio.to_thread(pol.compile_pending, policy_store(v.reg), doc, provider)
        except pol.PolicyPendingError as e:
            return JSONResponse({"error": str(e)}, status_code=400 if doc == "persona" or provider is not None else 503)
        except Exception as e:  # noqa: BLE001 — provider/schema errors: the caller can retry
            return JSONResponse({"error": f"could not compile the {doc} policy: {e}"}, status_code=502)
        return {"doc": doc, "reading": reading, "compiled": compiled.model_dump() if compiled is not None else None,
                "message": "confirm to make this reading live, or discard the edit" if compiled is not None else reading}

    @app.post("/api/policy/{doc}/confirm")
    async def policy_confirm(request: Request, doc: str):
        from .. import policy as pol

        v, _ = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            line = await asyncio.to_thread(pol.confirm_pending, policy_store(v.reg), doc, v.name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"confirmed the compiled {doc} policy: {line}", via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

    @app.post("/api/policy/{doc}/discard")
    async def policy_discard(request: Request, doc: str):
        from .. import policy as pol

        v, _ = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            line = await asyncio.to_thread(pol.discard_pending, policy_store(v.reg), doc, v.name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

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
                msg = await bot.set_channel(v.reg, field[8:], as_int(str(value).strip("<#>"), "channel id") if value else None, v.name, via=v.via, by_id=v.uid)
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
            elif field in ("timezone", "ask_audience", "about", "news_keywords", "loot_log"):
                from .. import configops

                text = ", ".join(str(w) for w in value) if isinstance(value, list) else ("true" if value is True else "false" if value is False else str(value or ""))  # news_keywords: a list from the page
                msg = await configops.apply_async(v.reg, configops.ConfigOp(op="set", path=field, value=text), v.name, True, bot=bot)
            else:
                return JSONResponse({"error": f"unknown setting {field}"}, status_code=400)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, msg, via=v.via, by_id=v.uid)  # '[web] Jon: …' / '[mcp] Jon: …'
        return {"message": msg}

    # ---- plain-text permissions (design §5.25): officers read, the owner edits
    def plain_json(reg) -> dict:
        from ..configops import OP_GROUP

        cfg = reg.config
        guild = bot.get_guild(cfg.discord_guild_id)
        if guild is not None:
            reg.cache_roles(guild)  # role ids → names for the "who" lines
        roles = [{"id": str(r.id), "name": r.name} for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed] if guild else []
        chans = bot.guild_channels(reg)
        return {"groups": [{"id": g, "label": label, "example": example, "who": reg.plain_who(g), "default": default, "who_text": reg.plain_who_text(g),
                            "ops": sorted(op for op, grp in OP_GROUP.items() if grp == g)} for g, (label, default, example) in PLAIN_GROUPS.items()],
                "channels": [{"id": c["id"], "name": c["name"], "category": c.get("category"), "mode": reg.plain_mode(c["id"]), "listed": c["id"] in cfg.plain.channels,
                              "role": "ops" if str(cfg.ops_channel_id) == c["id"] else "analytics" if str(cfg.analytics_channel_id) == c["id"] else None} for c in chans],
                "listed": cfg.plain.channels, "dm": cfg.plain.dm, "default": cfg.plain.default,
                "tiers": list(PLAIN_WHO), "modes": list(PLAIN_MODES), "guild_roles": roles, "owner": False}

    @app.get("/api/plain-permissions")
    async def plain_get(request: Request):
        v = await who(request, officer=True)
        return {**plain_json(v.reg), "owner": v.owner}

    @app.post("/api/admin/plain-permissions")
    async def plain_set(request: Request):
        """Owner: the whole policy — {groups: {group: [who…]}, channels: {channel id: mode}, dm, default}; or
        `set_channels` {channel id: mode | null} to change some channels and keep the rest. Groups or fields left out
        keep their current value."""
        v, d = await body(request, officer=True)
        need_owner(v)
        guild = bot.get_guild(v.reg.config.discord_guild_id)
        if guild is not None:
            v.reg.cache_roles(guild)
        patch = {k: d[k] for k in ("groups", "channels", "set_channels", "dm", "default") if k in d}
        try:  # the merge plain text's `plain` op uses too (Registry.merge_plain_policy), then one validated write
            msg = await asyncio.to_thread(v.reg.set_plain_policy, v.reg.merge_plain_policy(patch), v.name)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e).splitlines()[0]}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, msg, via=v.via, by_id=v.uid)
        return {"message": msg, **plain_json(v.reg), "owner": True}

    # ---- recruitment (design §5.29): the homepage's public facts, the owner's settings, the officers' applications
    @app.get("/api/public")
    async def public(request: Request):
        """Anonymous: the guild's name, blurb, invite and whether it is recruiting. Nothing personal. One guild (or none):
        exactly that shape, as always (K13); several: {"guilds": [each one's facts + key]} in Guilds.ordered() order."""
        regs = bot.registries.ordered()
        if len(regs) <= 1:
            return public_info(bot, regs[0] if regs else None)
        return {"guilds": [public_card(bot, r) for r in regs]}

    def guild_role_refs(reg) -> list[dict]:
        guild = bot.get_guild(reg.config.discord_guild_id)
        return [{"id": str(r.id), "name": r.name} for r in sorted(guild.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed] if guild else []

    @app.get("/api/recruitment")
    async def recruitment_get(request: Request):
        v = await who(request, officer=True)
        return {**recruitment_json(v.reg), "guild_roles": guild_role_refs(v.reg), "owner": v.owner}

    @app.post("/api/admin/recruitment")
    async def recruitment_set(request: Request):
        """Owner: any of {open, note, questions: [{id?, label, hint, required}], invite_url, member_role_ids} in one
        validated write (Registry.set_recruitment); a refusal names the part and nothing is saved."""
        v, d = await body(request, officer=True)
        need_owner(v)
        kw: dict = {}
        if "open" in d:
            kw["open"] = bool(d["open"])
        for k in ("note", "invite_url"):
            if k in d:
                kw[k] = str(d[k] or "")
        if "questions" in d:
            if not isinstance(d["questions"], list) or not all(isinstance(q, dict) for q in d["questions"]):
                return JSONResponse({"error": "questions must be a list of {label, hint, required}"}, status_code=400)
            kw["questions"] = d["questions"]
        if "member_role_ids" in d:
            kw["member_role_ids"] = [as_int(r, "role id") for r in (d["member_role_ids"] or []) if str(r).strip()]
        try:
            msg = await asyncio.to_thread(v.reg.set_recruitment, v.name, **kw)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        refresh = getattr(bot, "refresh_registration_card", None)
        if refresh is not None:  # open/closed and the note show on the registration card, as with /gm config recruitment
            await maybe_await(refresh(v.reg))
        await ops_mod.audited(bot.ops, v.reg, v.name, msg, via=v.via, by_id=v.uid)
        return {"message": msg, **recruitment_json(v.reg), "guild_roles": guild_role_refs(v.reg), "owner": True}

    @app.get("/api/applications")
    async def applications(request: Request):
        """Officers: the open applications (oldest first) and the last 20 decided (newest first)."""
        v = await who(request, officer=True)
        reg = v.reg
        decided = sorted([a for a in reg.applicants.values() if a.status != "open"], key=lambda a: a.decided_at or a.created_at, reverse=True)[:20]
        return {"open": [application_json(reg, a) for a in reg.open_applicants()], "decided": [application_json(reg, a) for a in decided],
                "recruiting": reg.config.recruitment.open}

    @app.post("/api/admin/application/decide")
    async def application_decide(request: Request):
        """Officers: {uid, accept, note} → the bot's decide_application (the same verdict as the Discord card: the
        applicant is DMed, the card updated, a character added on accept). Answers with the verdict line."""
        v, d = await body(request, officer=True)
        uid = as_int(d.get("uid"), "uid")
        if "accept" not in d:
            return JSONResponse({"error": "accept: true or false"}, status_code=400)
        note = str(d.get("note") or "").strip() or None
        a = v.reg.applicants.get(uid)
        if not a or a.status != "open":
            return JSONResponse({"error": "No open application for that member."}, status_code=400)
        try:
            line = await maybe_await(bot.decide_application(v.reg, uid, bool(d["accept"]), v.name, note))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        line = str(line or f"application from {a.display_name} {'accepted' if d['accept'] else 'declined'}")
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line}

    @app.post("/api/admin/invite")
    async def create_invite(request: Request):
        """Owner: {channel_id} → a permanent, unlimited-use invite to that channel. Returned, not saved: the Config page
        puts it in the invite field and the owner saves the section."""
        v, d = await body(request, officer=True)
        need_owner(v)
        cid = channel_ref(v.reg, d.get("channel_id") or d.get("channel"))  # an id, <#id> or a name (MCP names channels)
        if cid is None:
            return JSONResponse({"error": "channel_id: which channel should the invite open?"}, status_code=400)
        try:
            url = await maybe_await(bot.create_invite(v.reg, cid))
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"created invite {url}", via=v.via, by_id=v.uid)
        return {"invite_url": url, "message": f"invite created: {url} (save the section to use it)"}

    previews: dict[str, dict] = {}  # plain_change preview token -> the authorized ops + board revs it showed (single use)

    def mcp_actor_rule(v, allowed, refused):
        """D8: from MCP without X-Oibot-Actor, the loot decisions (an award, a confirm, a reason, a correction, a retired
        precedent, tiers, someone else's wishlist) are refused — the owner's token is not the officer who decided."""
        if v.via != "mcp" or getattr(v, "actor", None):
            return allowed, refused
        from ..configops import people_needs_actor
        from ..loot_ops import needs_actor

        keep = [op for op in allowed if not needs_actor(op, v.uid)]
        if len(keep) != len(allowed):
            refused = [*refused, "Loot decisions (an award, a confirm, a reason, a correction, tiers, someone else's wishlist) from MCP need the "
                                 "X-Oibot-Actor header naming an officer of this guild (OIBOT_MCP_ACTOR)."]
        people = [op for op in keep if not people_needs_actor(op)]
        if len(people) != len(keep):  # D8: a rank or a character's confirmation is a decision about a person
            refused = [*refused, "A rank or a confirmation from MCP needs the X-Oibot-Actor header naming an officer of this guild (OIBOT_MCP_ACTOR)."]
        return people, refused

    @app.post("/api/ops/change")
    async def ops_change(request: Request):
        """Plain-text configuration (what `/gm change` and the ops-channel @mention do), in two calls like Discord's
        diff + Apply. Preview `{text}`: Claude parses it into whitelisted ConfigOps, `authorize` keeps what this
        caller may do, `describe()` says current → new per op; a change also returns `token` (single use, valid
        PREVIEW_TTL seconds) holding exactly those ops and the boards' revs as shown. Apply `{token, apply: true}`:
        those ops — never a re-parse — authorized again in code for the same caller and applied with the revs, so a
        board someone changed since the preview is refused. Questions come back instead of ops."""
        from .. import configops
        from ..configops import ConfigOp
        from ..policy import PolicyStore

        v, d = await body(request, officer=True)
        now = time.monotonic()
        for k in [k for k, p in previews.items() if p["expires"] < now]:
            previews.pop(k, None)
        raids = bot.raids.store(v.reg) if getattr(bot, "raids", None) is not None else None
        if d.get("apply"):
            held = previews.pop(str(d.get("token") or ""), None)
            if held is None or held["uid"] != v.uid or held["guild"] != v.reg.key:
                return JSONResponse({"error": "apply needs the token of a preview you made in the last "
                                              f"{PREVIEW_TTL // 60} minutes — preview again (without apply) and apply that"}, status_code=400)
            ops = [ConfigOp(**o) for o in held["ops"]]
            if live_officer is not None:  # a write: the rights are decided now (PRIV_LIVE), not from the login's lookup
                await live_officer(v)
            allowed, refused = configops.authorize(v.reg, ops, v.uid, officer=v.officer, channel=PLAIN_WEB, raids=raids)
            allowed, refused = mcp_actor_rule(v, allowed, refused)
            out = {"kind": "change", "reply": held["reply"], "questions": [], "ops": [op.model_dump(exclude_none=True) for op in allowed],
                   "describe": held["describe"], "applied": [], "refused": refused, "error": None}
            revs, stale = dict(held["revs"]), set()
            ps = PolicyStore(bot.registries.store, v.reg.key)

            def failed(op, e) -> None:  # as Discord's Apply: listed with what it was, and the rest of the request still applies
                out["refused"].append(f"{configops.describe(v.reg, op, PLAIN_WEB)} — {e}")
                out["error"] = out["error"] or str(e)

            for op in configops.coalesce(allowed):  # consecutive raid/schedule settings save as one write (as on Discord)
                key = configops.op_run_key(v.reg, op, raids)
                if op.op in configops.BOARD_OPS and key in stale:  # an earlier step found someone else's change on this board
                    failed(op, rc.BoardChanged())
                    continue
                before = configops.snapshot_revs(v.reg, revs, raids)
                try:
                    line = await configops.apply_async(v.reg, op, v.name, True, ps, bot=bot, by_id=v.uid, channel_id=PLAIN_WEB,  # authorize() allowed it
                                                       rev=configops.op_rev(op, key, revs), via=v.via)
                except (RegistryError, ValueError) as e:
                    failed(op, e)
                    if isinstance(e, rc.BoardChanged) and key:
                        stale.add(key)  # keep the old rev: the rest of this request can't overwrite that change either
                    continue
                except Exception as e:  # noqa: BLE001 — Discord or the solver: never a 500 after a partial apply; the caller sees what landed
                    failed(op, await configops.report_error(bot.ops, v.reg.config, e, f"[{v.via}] plain change {op.op}"))
                    continue
                configops.refresh_revs(v.reg, revs, raids, stale, before)  # this request's own write is not someone else's change
                out["applied"].append(line)  # apply_async(via=) wrote the audit line
            return out
        text = str(d.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        provider = getattr(getattr(bot, "ctx", None), "provider", None)
        if provider is None:
            return JSONResponse({"error": "the LLM provider is off (ANTHROPIC_API_KEY unset)"}, status_code=503)
        try:
            req = await asyncio.to_thread(configops.parse, provider, v.reg, text, v.name, officer=v.officer)
        except Exception as e:  # noqa: BLE001 — provider/schema errors are 502s the caller can retry
            return JSONResponse({"error": f"could not parse the request: {e}"}, status_code=502)
        # the same policy check as Discord: the site (and MCP, as the owner) counts as a channel where plain text acts
        allowed, refused = configops.authorize(v.reg, req.ops, v.uid, officer=v.officer, channel=PLAIN_WEB, raids=raids)
        allowed, refused = mcp_actor_rule(v, allowed, refused)
        req.ops = allowed
        ops = [op.model_dump(exclude_none=True) for op in req.ops]
        desc = []
        for op in req.ops:
            try:
                desc.append(configops.describe(v.reg, op, PLAIN_WEB))
            except Exception as e:  # noqa: BLE001
                desc.append(f"{op.op}: {e}")
        out = {"kind": req.kind, "reply": req.reply, "questions": list(req.questions), "ops": ops, "describe": desc, "applied": [], "refused": refused}
        if req.kind == "change" and req.ops:
            token = secrets.token_urlsafe(16)
            previews[token] = {"uid": v.uid, "guild": v.reg.key, "ops": ops, "describe": desc, "reply": req.reply,
                               "revs": configops.board_revs(v.reg, req.ops, raids), "expires": now + PREVIEW_TTL}
            out.update(token=token, expires_in=PREVIEW_TTL)
        return out

    from .api_agents import install_agents_api  # news, proposals, the local jobs (design §5.26)

    install_agents_api(app, bot, who=who, body=body, publish=publish)

    from .api_me import install_me_api  # the member inbox (asks, notices, own answers) and the officers' on-behalf routes (design §5.40)

    install_me_api(app, bot, who=who, body=body, find_live=find_live, resolve_member=resolve_member)

    from .api_loot import install_loot_api  # the loot council, history, precedents, the award log, items, wishlists (design §3.1)

    install_loot_api(app, bot, who=who, body=body, publish=publish, find_live=find_live, resolve_member=resolve_member, live_officer=live_officer)

    # ---- the built SPA (history-mode routes fall back to index.html)
    @app.get("/app")
    @app.get("/app/{path:path}")
    async def spa(request: Request, path: str = ""):
        if not APP_DIR.exists():
            raise HTTPException(503, "front end not built (cd frontend && npm run build)")
        target = (APP_DIR / path).resolve() if path else None
        if target and target.is_file() and APP_DIR.resolve() in target.parents:
            return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else None)
        g = (request.query_params.get("g") or "").strip()
        try:
            v = await viewer(request)
        except NotMember as e:
            if not (g and e.guilds):
                raise
            v = await viewer(request, query=False)  # a link to a server this person isn't in: open theirs (the app says so)
        if v is None:
            from fastapi.responses import RedirectResponse

            here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(login_href(here), status_code=307)  # the query (?g=) survives the login (K14)
        resp = FileResponse(APP_DIR / "index.html", headers={"Cache-Control": "no-cache"})
        if g and v.reg.key == g and v.via == "web" and request.cookies.get(GUILD_COOKIE) != g:
            set_guild_cookie(resp, g)  # a deep link selects its guild for the pages that follow
        return resp
