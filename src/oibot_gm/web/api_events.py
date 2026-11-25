"""The Events page's routes (phase 4, design §7a) — thin callers of event_view's builders, officers only (the page is an
officer page, as /api/rosters is), each run in a thread. GET /api/events stays the live stream; the page reloads on the
existing topics `run:` (any run saved), `member` and `config` (a schedule edited).

- GET /api/event-list?history_offset=0 → event_view.event_list
- GET /api/event-new → event_view.new_event_options (the New event page's raids, lineups to copy, lock choices, the
  guild's date)
- GET /api/event/{key} → event_view.event_json (any part's key answers as its event; finished and cancelled too): each
  open raid's board is the officers' layout — nothing is solved for a view — with its Auto-fill groups line
  (event_board.AutoFill.state; the page hears `run:<raid key>` when an auto-fill starts or ends)
- POST /api/event/{key}/clear → More › Clear the board: every open raid's groups back to Not placed (pins stay), through
  consequences.clear_board_box (`preview` → {box}; `seen` → 409 with the fresh box; refused → 400 with the refusal box)
- GET /api/event/{key}/lineup → event_view.lineup_edit_json (Edit lineup's start); POST it {lineup} →
  event_view.lineup_draft_json (its "What this makes" and Save box, nothing written — the Save is POST /api/run/{key}/lineup)
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
from .. import ops as ops_mod
from .. import raid_views as rv
from .. import raidcycle as rc
from ..constants import CONFIRM_WORDS
from ..registry import RegistryError

# a link to an event that isn't one any more: its record is gone, or (a raid taken out of a lineup) it is no event of its own
NO_EVENT = "This event is no longer on the Events list."
# the Role switches route's refusals (each a refusal box)
SWITCH_OVER = "This event is over, so nobody can switch roles on it."
PICK_A_ROW = "Pick someone from the list of role switches."
SWITCH_GONE = "That switch is no longer on the list. The list now shows what is short."
NOBODY_ASKED = "Nobody could be asked, so nothing was sent."
CLEARED = "The board is clear. Everyone who joined is in Not placed."


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

    @app.get("/api/event-new")
    async def event_new(request: Request):
        """The New event page's choices (event_view.new_event_options): the guild's date, the raids, the lineups to copy,
        the lock choices. Its preview and Open are POST /api/raid/{rid}/open(/preview)."""
        v = await who(request, officer=True)
        return await asyncio.to_thread(event_view.new_event_options, v.reg)

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
        af = event_board.autofill_of(bot)  # the board is the officers' layout: nothing solves here, Auto-fill groups' line only
        return await asyncio.to_thread(event_view.event_json, v.reg, rs, ev, v.uid,
                                       sheet=lambda p, rosters=None, open_seats=True: ev_json(v.reg, rs, p, rosters=rosters, open_seats=open_seats),
                                       autofill=lambda p: af.state(v.reg, p))

    @app.post("/api/event/{key}/clear")
    async def event_clear(request: Request, key: str):
        """More › Clear the board: every OPEN raid of the event emptied back to Not placed — hand and Auto-fill
        placements alike, pins untouched — one whole-board write per raid (RaidMixin.save_board with the board's rev).
        `preview` → {box} (consequences.clear_board_box); `seen` = its fp (a board changed meanwhile → 409 with the
        fresh box); a refusal (locked, nothing placed) → 400 with its box. Nobody is told."""
        v, d = await body(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None or ev.dropped:
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        fresh = cq.clear_board_box(v.reg, rs, ev)
        if d.get("preview"):
            return {"box": fresh} if fresh["verb"] else JSONResponse({"error": fresh["sentences"][0], "box": fresh}, status_code=400)
        if fresh["verb"] is None:
            return JSONResponse({"error": fresh["sentences"][0], "box": fresh}, status_code=400)
        if d.get("seen") and str(d["seen"]) != fresh["fp"]:
            return JSONResponse({"error": CONFIRM_WORDS["changed"], "box": fresh}, status_code=409)
        first = rc.first_part(rs, ev)
        for p in [p for p in rc.event_parts(rs, first) if p.state == "open"]:
            try:
                await bot.save_board(v.reg, rs, p, [[] for _ in rc.board_layout(v.reg, p)], rev=rc.board_rev(v.reg, p), by=v.name, via=v.via)
            except rc.BoardChanged:
                again = cq.clear_board_box(v.reg, rs, ev)
                return JSONResponse({"error": CONFIRM_WORDS["changed"], "box": again}, status_code=409)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"cleared the board of {rv.run_title(v.reg, first)}", via=v.via, by_id=v.uid)
        return {"message": CLEARED}

    @app.get("/api/event/{key}/lineup")
    async def event_lineup(request: Request, key: str):
        """The Edit lineup page's start (event_view.lineup_edit_json): the event's own lineup and clock, the palette, and
        on a locked or finished event the server's refusal box. Reads only — no board solve."""
        v = await who(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None or ev.dropped:
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        return await asyncio.to_thread(event_view.lineup_edit_json, v.reg, rs, ev)

    @app.post("/api/event/{key}/lineup")
    async def event_lineup_draft(request: Request, key: str):
        """{lineup: [{raid, rosters, starts}] — the WHOLE draft, its first raid first} → Edit lineup's "What this makes"
        (event_view.lineup_draft_json: facts, each step's start in words, the Save box, `same`). Nothing written: the
        Save is POST /api/run/{key}/lineup."""
        v, d = await body(request, officer=True)
        rs = bot.raids.store(v.reg)
        ev = rs.events.get(key)
        if ev is None or ev.dropped:
            return JSONResponse({"error": NO_EVENT}, status_code=404)
        lineup = d.get("lineup")
        if not isinstance(lineup, list) or not all(isinstance(e, dict) for e in lineup):
            return JSONResponse({"error": "Name the event's raids, its first raid first, each with its rosters."}, status_code=400)
        return await asyncio.to_thread(event_view.lineup_draft_json, v.reg, rs, ev, lineup)

    @app.post("/api/run/{key}/switch")
    async def event_switch(request: Request, key: str):
        """Switch / Answer for them / Ask on one Role switches row: {uid, character, spec, part, seat, option, rev, seen?,
        preview?}. The row must still be on the list (rc.event_switches), else 409 with the fresh `switches` and nothing
        written. Before the lock → set_answer(Join, character, spec) on the first raid (the answer is one for the
        evening; the member gets a notice, nobody a DM). After the lock → send_plan(rev, picks {seat: option}, seats
        [seat]) on `part`: that one ask, a DM with Yes and No (or the Me page with DMs off). `preview` → {box}; `seen` =
        the box's fp → 409 with the fresh box when it changed. The switch is written to the run's log and the ops feed
        with who made it (ops.audited, `via`). Answers {message, switches}."""
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
            return _refused(SWITCH_OVER)
        try:
            m = reg.members.get(int(d.get("uid")))
        except (TypeError, ValueError):
            m = None
        character, spec = str(d.get("character") or "").strip(), str(d.get("spec") or "").strip()
        c = next((c for c in m.active() if c.label == character), None) if m else None
        if m is None or c is None or not spec:
            return _refused(PICK_A_ROW)
        try:
            rc.spec_role(reg, c, spec)
        except RegistryError as e:
            return _refused(e)

        async def fresh_switches():
            return await asyncio.to_thread(rc.event_switches, reg, rs, first, viewer=v.uid)

        def gone(switches, error: str = SWITCH_GONE):
            return JSONResponse({"error": error, "switches": switches}, status_code=409)

        if first.state == "open":
            now = await fresh_switches()
            row = next((r for r in now["rows"] if (r["uid"], r["character"], r["spec"]) == (str(m.discord_id), character, spec)), None)
            if row is None:  # the raid stopped being short, or someone else covers it now
                return gone(now)
            fresh = row["box"]
            if d.get("preview"):
                return {"box": fresh}
            if d.get("seen") and str(d["seen"]) != fresh["fp"]:
                return JSONResponse({"error": CONFIRM_WORDS["changed"], "box": fresh}, status_code=409)
            try:
                line = await bot.set_answer(reg, rs, first, m, "in", character, v.name, by_id=v.uid, spec=spec, via=v.via)
            except (RegistryError, ValueError) as e:
                return _refused(e)
            return {"message": rc.site_words(reg, line), "switches": await fresh_switches()}  # local12 words, never a Discord stamp
        try:
            seat, option = int(d.get("seat")), int(d.get("option"))
        except (TypeError, ValueError):
            return _refused(PICK_A_ROW)
        if m.discord_id == v.uid:  # after the lock a switch is an ask: never one to the officer pressing it
            return _refused(PICK_A_ROW)
        plan = await asyncio.to_thread(rc.role_plan, reg, rs, part)
        opt = plan.seats[seat].options[option] if 0 <= seat < len(plan.seats) and 0 <= option < len(plan.seats[seat].options) else None
        same = opt is not None and len(opt) == 1 and (opt[0].discord_id, opt[0].character, opt[0].spec) == (m.discord_id, character, spec)
        if str(d.get("rev") or "") != plan.rev or not same:
            return gone(await fresh_switches(), str(rc.PlanChanged(plan)))
        fresh, _button = rc.switch_box_for(reg, rs, part, m, character, spec, plan_rev=plan.rev, viewer=v.uid, swap=opt[0].swap)
        if d.get("preview"):
            return {"box": fresh}
        if d.get("seen") and str(d["seen"]) != fresh["fp"]:
            return JSONResponse({"error": CONFIRM_WORDS["changed"], "box": fresh}, status_code=409)
        try:
            sent, plan = await bot.send_plan(reg, rs, part, rev=plan.rev, by=v.name, picks={seat: option}, seats=[seat])
        except rc.PlanChanged as e:
            return gone(await fresh_switches(), str(e))
        except (RegistryError, ValueError) as e:
            return _refused(e)
        if sent:
            part.log.append(f"switch: {v.name} asked {m.display_name} to play {spec}")
            rs.save(part, f"switch ask to {m.display_name} by {v.name}")
            await ops_mod.audited(bot.ops, reg, v.name, f"asked {m.display_name} to play {spec} on {rv.run_title(reg, part)}", via=v.via, by_id=v.uid)
        names = ", ".join(a.display_name for a in sent)
        return {"message": f"Asked {names}." if sent else NOBODY_ASKED, "switches": await fresh_switches()}
