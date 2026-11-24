"""The Schedules page's routes (phase 4, design §7a) — thin callers of schedule_view's builders, officers only (as
/api/raids is), each run in a thread. The page reloads on the existing `config` (a schedule edited) and `member` (a
regular added or removed) topics. Writes are elsewhere: a schedule's Save / Pause / Resume / Remove go through POST
/api/ops/change (kinds `schedule` / `schedule_remove`, always previewing the box first), a regular through POST
/api/admin/raid/regulars by the schedule's id.

- GET /api/schedules → schedule_view.schedule_list ({active, paused, reserved_ids})
- GET /api/schedules/{id} → schedule_view.schedule_json; 404 {error} for an id no schedule has."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import schedule_view
from ..registry import RegistryError

NO_SCHEDULE_HERE = "This schedule is no longer on the Schedules list."


def install_schedules_api(app: FastAPI, bot, *, who) -> None:
    @app.get("/api/schedules")
    async def schedules_list(request: Request):
        v = await who(request, officer=True)
        return await asyncio.to_thread(schedule_view.schedule_list, v.reg)

    @app.get("/api/schedules/{sid}")
    async def schedule_get(request: Request, sid: str):
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        try:
            v.reg.schedule_entry(sid)
        except RegistryError:
            return JSONResponse({"error": NO_SCHEDULE_HERE}, status_code=404)
        return await asyncio.to_thread(schedule_view.schedule_json, v.reg, rs, sid)
