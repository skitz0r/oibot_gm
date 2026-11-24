"""The Events page's routes (phase 4, design §7a) — thin callers of event_view's builders, officers only (the page is an
officer page, as /api/rosters is), each run in a thread. GET /api/events stays the live stream; the page reloads on the
existing topics `run:` (any run saved), `member` and `config` (a schedule edited).

- GET /api/event-list?history_offset=0 → event_view.event_list
- GET /api/event/{key} → event_view.event_json (any part's key answers as its event; finished and cancelled too)
- GET /api/event/coming/{sid}/{occurrence} → event_view.coming_json; 404 {error, key} when the night has an event now
  (`key`: the page goes there) or is no longer on its schedule (`key` null)."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import event_view

NO_EVENT = "There is no event by that name."


def install_events_api(app: FastAPI, bot, *, who, ev_json) -> None:
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
        if ev is None:
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        return await asyncio.to_thread(event_view.event_json, v.reg, rs, ev, v.uid, sheet=lambda p: ev_json(v.reg, rs, p))
