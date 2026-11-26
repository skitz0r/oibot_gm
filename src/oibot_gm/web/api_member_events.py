"""The members' Events page routes (design 2026-11-25 §2) — thin callers of member_event_view, for ANY member of the
selected guild (`who(request)`, never officer-only; an officer gets the same member view here), each run in a thread.
The page reloads on the event's `run:` topic and `member`, as the Me page does. Answers go through the Me page's own
routes (POST /api/me/run/{key}/answer|out, /api/me/fill/{key}, /api/me/ask/{id}); nothing here writes.

- GET /api/me/events → member_event_view.member_event_list (Open · Coming up; no History)
- GET /api/me/event/{key} → member_event_view.member_event_json (any raid's key answers as its event) + the viewer's
  own `answers` (api_me.me_sheet_row per live raid, the first raid first), `asks` (inbox.asks_of on this event's raids)
  and `characters` [{label, is_main}] (the main first). A key the page doesn't list → 404 {error: event_over_member}."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import inbox
from .. import member_event_view as mev
from ..constants import PAGE_WORDS
from .api_me import me_sheet_row

LIVE = ("open", "locked")


def _event(reg, rs, key: str, uid: int) -> dict:
    ev = rs.events.get(key)
    if ev is None:
        raise mev.NotListed(key)
    j = mev.member_event_json(reg, rs, ev, uid)
    m = reg.members.get(int(uid))
    parts = [rs.events[k] for k in j["part_keys"] if k in rs.events]
    j["answers"] = [me_sheet_row(reg, rs, p, m, uid) for p in parts if p.state in LIVE]
    j["asks"] = [a for a in inbox.asks_of(reg, rs, uid) if a["run"] in j["part_keys"]]
    j["characters"] = [{"label": c.label, "is_main": c.is_main}
                       for c in sorted(m.active(), key=lambda c: (not c.is_main, c.label.lower()))] if m else []
    return j


def install_member_events_api(app: FastAPI, bot, *, who) -> None:
    @app.get("/api/me/events")
    async def member_events(request: Request):
        v = await who(request)
        rs = bot.raids.store(v.reg)
        return await asyncio.to_thread(mev.member_event_list, v.reg, rs, v.reg.now_local(), v.uid)

    @app.get("/api/me/event/{key}")
    async def member_event(request: Request, key: str):
        v = await who(request)
        rs = bot.raids.store(v.reg)
        try:
            return await asyncio.to_thread(_event, v.reg, rs, key, v.uid)
        except mev.NotListed:
            return JSONResponse({"error": PAGE_WORDS["event_over_member"]}, status_code=404)
