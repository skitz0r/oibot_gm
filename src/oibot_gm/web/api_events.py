"""The Events page's routes (phase 4, design §7a) — thin callers of event_view's builders, officers only (the page is an
officer page, as /api/rosters is), each run in a thread. GET /api/events stays the live stream; the page reloads on the
existing topics `run:` (any run saved), `member` and `config` (a schedule edited).

- GET /api/event-list?history_offset=0 → event_view.event_list
- GET /api/event/{key} → event_view.event_json (any part's key answers as its event; finished and cancelled too), its
  open raids' boards from event_board's cache (the lock's placement; `board_state` working while it is solved, and the
  page hears `run:<event key>` when it is ready)
- GET /api/event/coming/{sid}/{occurrence} → event_view.coming_json; 404 {error, key} when the night has an event now
  (`key`: the page goes there) or is no longer on its schedule (`key` null).
- POST /api/run/{key}/switch → one Role switches row (rc.event_switches): before the lock RaidMixin.set_answer (Join
  as that spec, on the first raid: the whole evening; a notice), after it RaidMixin.send_plan of that one seat on its
  raid (the ask). `preview` → {box}; `seen` → 409 with the fresh box; a changed plan → 409 with the fresh switches."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import consequences as cq
from .. import event_board, event_view
from .. import raidcycle as rc
from ..constants import CONFIRM_WORDS
from ..registry import RegistryError

# a link to an event that isn't one any more: its record is gone, or (a raid taken out of a lineup) it is no event of its own
NO_EVENT = "This event is no longer on the Events list."


def _refused(why) -> JSONResponse:
    """A refusal as the box shows it: the verb's own sentence, only Back."""
    why = why.args[0] if isinstance(why, BaseException) and why.args and isinstance(why.args[0], str) else str(why)
    return JSONResponse({"error": why, "box": cq.refusal_box_for(why)}, status_code=400)


def install_events_api(app: FastAPI, bot, *, who, body, ev_json) -> None:
    @app.get("/api/event-list")
    async def event_list(request: Request, history_offset: int = 0):
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        return await asyncio.to_thread(event_view.event_list, v.reg, rs, v.reg.now_local(), history_offset)

    @app.get("/api/event/coming/{sid}/{occurrence}")
    async def event_coming(request: Request, sid: str, occurrence: str):
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        try:
            return await asyncio.to_thread(event_view.coming_json, v.reg, rs, sid, occurrence, v.reg.now_local())
        except event_view.ComingGone as e:
            return JSONResponse({"error": str(e), "key": e.key}, status_code=404)

    @app.get("/api/event/{key}")
    async def event_get(request: Request, key: str):
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None or ev.dropped:  # a dropped raid answers as nothing: no event of its own and no part of one
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        # the board as the lock would place it (event_board): the current picture, else the last one while it is placed again
        board = await event_board.cache_of(bot).get(v.reg, rs, ev)
        return await asyncio.to_thread(event_view.event_json, v.reg, rs, ev, v.uid,
                                       sheet=lambda p, rosters=None: ev_json(v.reg, rs, p, rosters=rosters), board=board)

    @app.post("/api/run/{key}/switch")
    async def event_switch(request: Request, key: str):
        """Switch / Answer for them on one Role switches row: {uid, character, spec, part, seat, option, rev, seen?,
        preview?}. Before the lock → set_answer(Join, character, spec) on the first raid (the answer is one for the whole
        evening; the member gets a notice, nobody a DM). After the lock → send_plan(rev, picks {seat: option}, seats
        [seat]) on `part`: that one ask, a DM with Yes and No (or the Me page with DMs off). `preview` → {box}; `seen` =
        the box's fp → 409 with the fresh box when it changed; a plan that changed (`rev`) → 409 with the fresh
        `switches`. Answers {message, switches}."""
        v, d = await body(request, officer=True)
        reg, rs = v.reg, bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None or ev.dropped:
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        first = rc.first_part(rs, ev)
        part = rs.events.get(str(d.get("part") or "")) or first
        if not rc.same_event(part, first) or part.dropped:
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        if first.state not in ("open", "locked") or part.state not in ("open", "locked"):
            return _refused("This event is over, so nobody can switch roles on it.")
        try:
            m = reg.members.get(int(d.get("uid")))
        except (TypeError, ValueError):
            m = None
        character, spec = str(d.get("character") or "").strip(), str(d.get("spec") or "").strip()
        c = next((c for c in m.active() if c.label == character), None) if m else None
        if m is None or c is None or not spec:
            return JSONResponse({"error": "Pick someone from the list of role switches."}, status_code=400)
        try:
            rc.spec_role(reg, c, spec)
        except RegistryError as e:
            return _refused(e)

        async def fresh_switches():
            return await asyncio.to_thread(rc.event_switches, reg, rs, first, viewer=v.uid)

        if first.state == "open":
            fresh = rc.switch_box_for(reg, rs, first, m, character, spec, viewer=v.uid)
            if d.get("preview"):
                return {"box": fresh}
            if d.get("seen") and str(d["seen"]) != fresh["fp"]:
                return JSONResponse({"error": CONFIRM_WORDS["changed"], "box": fresh}, status_code=409)
            try:
                line = await bot.set_answer(reg, rs, first, m, "in", character, v.name, by_id=v.uid, spec=spec)
            except (RegistryError, ValueError) as e:
                return _refused(e)
            return {"message": str(line).replace("**", "").removeprefix("✅ "), "switches": await fresh_switches()}
        try:
            seat, option = int(d.get("seat")), int(d.get("option"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Pick someone from the list of role switches."}, status_code=400)
        plan = await asyncio.to_thread(rc.role_plan, reg, rs, part)
        opt = plan.seats[seat].options[option] if 0 <= seat < len(plan.seats) and 0 <= option < len(plan.seats[seat].options) else None
        same = opt is not None and len(opt) == 1 and (opt[0].discord_id, opt[0].character, opt[0].spec) == (m.discord_id, character, spec)
        if str(d.get("rev") or "") != plan.rev or not same:
            return JSONResponse({"error": str(rc.PlanChanged(plan)), "switches": await fresh_switches()}, status_code=409)
        fresh = rc.switch_box_for(reg, rs, part, m, character, spec, plan_rev=plan.rev, viewer=v.uid, swap=opt[0].swap)
        if d.get("preview"):
            return {"box": fresh}
        if d.get("seen") and str(d["seen"]) != fresh["fp"]:
            return JSONResponse({"error": CONFIRM_WORDS["changed"], "box": fresh}, status_code=409)
        try:
            sent, plan = await bot.send_plan(reg, rs, part, rev=plan.rev, by=v.name, picks={seat: option}, seats=[seat])
        except rc.PlanChanged as e:
            return JSONResponse({"error": str(e), "switches": await fresh_switches()}, status_code=409)
        except (RegistryError, ValueError) as e:
            return _refused(e)
        names = ", ".join(a.display_name for a in sent)
        return {"message": f"Asked {names}." if sent else "Nobody could be asked.", "switches": await fresh_switches()}
