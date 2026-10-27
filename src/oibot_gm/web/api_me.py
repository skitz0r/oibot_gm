"""The member inbox on the web (dual interfaces phase 2 B, design §5.40) and the officers' on-behalf routes.

The Me page answers its own sheets and asks here, through the SAME verbs as the sheet's buttons and the DM cards — the
person is the actor (`by_id` = their Discord id decides "their own", never a name): Join / Bench / No thanks
(RaidMixin.set_answer), Can't make it with a note (call_out), a fill or pre-lock ask (answer_fill_for), a Confirm /
Can't ask (answer_placement_for). Every reply is the receipt (rc.answer_receipt: the state, not the action), and a
refusal is the verb's own sentence ("Already answered (yes, by Kestrel at 5:12 PM)."). Notices (D26) are read and
marked read here.

Officers: GET /api/admin/inbox (a member's asks and notices — MCP member_inbox), GET /api/admin/actions (the on-behalf
union rc.member_actions with each action's consequence — the site's row ⋮ menu, the same list the SetWizard shows) and
POST /api/admin/action (inbox.do_member_action — the verbs every surface shares; a stale `rev` is a 409 naming what
changed)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import inbox
from .. import raidcycle as rc
from ..registry import RegistryError

QUEUE_NOTE = "You'll go to the back of the queue if you change. Change anyway?"


def cant_consequence(reg, ev, seated: bool) -> str:
    """The Me page's Can't make it… confirm, from code (the Confirm rule, design §5.57): a member's own callout
    (RaidMixin.call_out) — a rostered seat is released and the bench asked when automatic asks are on."""
    run = rc.run_on(reg, ev)
    if seated:
        return f"Your seat for {run} is released, and the officers see it as a callout. {rc.refill_sentence(reg, ev)}"
    return f"You go to No thanks for {run}, and the officers see it as a callout."


def sheet_row_extras(reg, ev, m) -> dict:
    """What the Me page's My sheets row needs to be actionable (rendered from server state only): may_answer (open: Join /
    Bench / No thanks), may_cant (locked and rostered: Can't make it… with a note) and cant_consequence (its confirm's
    sentences), queue_note (ad hoc and joined: a change away from Join asks once), receipt (rc.answer_receipt, plain)."""
    s = ev.signups.get(str(m.discord_id)) if m else None
    locked = ev.state == "locked"
    seated = bool(m and locked and ev.seat_of(m))
    adhoc = rc.run_kind(reg, ev) == "adhoc"
    return {"may_answer": ev.state == "open" and rc.on_bench_side(reg, ev, m) if m else False,
            "may_cant": seated or (locked and s is not None and s.status == "in"),
            "cant_consequence": cant_consequence(reg, ev, seated) if locked else None,
            "queue_note": QUEUE_NOTE if adhoc and ev.state == "open" and s is not None and s.status == "in" else None,
            "receipt": rc.answer_receipt(reg, ev, m).replace("**", "") if m else None}


def install_me_api(app: FastAPI, bot, *, who, body, find_live, resolve_member) -> None:
    def me_of(v):
        m = v.reg.members.get(v.uid)
        if m is None or not m.active():
            raise HTTPException(400, "You're not registered yet — register a character first (the Me page's character table, or /register).")
        return m

    def run_of(v, key: str):
        rs = bot.raids.store(v.reg)
        return rs, find_live(v.reg, rs, key)

    def receipt(v, ev, m, line: str | None = None) -> dict:
        r = rc.answer_receipt(v.reg, ev, m)
        return {"message": r.replace("**", ""), "receipt": r, "line": (line or "").replace("**", "").removeprefix("✅ ")}

    @app.post("/api/me/run/{key}/answer")
    async def me_answer(request: Request, key: str):
        """My own Join / Bench / No thanks on a sheet {status in|sub|out, character?}: RaidMixin.set_answer as myself (the
        sheet button's verb). After lock a Join / Bench of my own is refused like the sheet refuses it (rc.LOCKED_JOIN).
        Two devices: the last press wins and each reply is the receipt of the state at that moment."""
        v, d = await body(request)
        m = me_of(v)
        rs, ev = run_of(v, key)
        status = d.get("status")
        if status not in rc.STATUSES:
            return JSONResponse({"error": f"status must be one of {', '.join(rc.STATUSES)}"}, status_code=400)
        if ev.state != "open" and status in ("in", "sub"):
            return JSONResponse({"error": rc.LOCKED_JOIN}, status_code=400)
        try:
            line = await bot.set_answer(v.reg, rs, ev, m, status, (d.get("character") or None), m.display_name, by_id=v.uid)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return receipt(v, ev, m, line)

    @app.post("/api/me/run/{key}/out")
    async def me_out(request: Request, key: str):
        """My Can't make it {note?}: RaidMixin.call_out as myself — a callout (hours before; late after the lock), a
        rostered seat freed, the note for the officers only (never the public sheet)."""
        v, d = await body(request)
        m = me_of(v)
        rs, ev = run_of(v, key)
        note = str(d.get("note") or "").strip()[:300] or None
        try:
            line = await bot.call_out(v.reg, rs, ev, m, note, m.display_name, by_id=v.uid)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return receipt(v, ev, m, line)

    @app.post("/api/me/fill/{key}")
    async def me_fill(request: Request, key: str):
        """My answer to a fill / swap / pre-lock ask on run `key` {answer yes|no}: RaidMixin.answer_fill_for as myself (the
        DM card's verb; its card is edited to the outcome). Nothing open → the verb's refusal."""
        v, d = await body(request)
        m = me_of(v)
        rs, ev = run_of(v, key.removeprefix("fill:"))
        if d.get("answer") not in ("yes", "no"):
            return JSONResponse({"error": "answer: yes or no"}, status_code=400)
        try:
            line = await bot.answer_fill_for(v.reg, rs, ev, m, d.get("answer") == "yes", m.display_name, by_id=v.uid)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return receipt(v, ev, m, line)

    @app.post("/api/me/ask/{ask_id}")
    async def me_ask(request: Request, ask_id: str):
        """My Confirm / Can't make it on a locked run {answer yes|no}: `ask_id` = the ask's id from GET /api/me
        ("place:<run key>"), a run key or its roster key → RaidMixin.answer_placement_for as myself (the check-and-set
        on the loop; a seat released or an ask already answered is refused from one place)."""
        v, d = await body(request)
        m = me_of(v)
        rs = bot.raids.store(v.reg)
        ref = ask_id.removeprefix("place:")
        ev = rs.events.get(ref) or rs.for_team(ref)
        if ev is None:
            return JSONResponse({"error": "That run is closed — nothing to answer."}, status_code=400)
        if d.get("answer") not in ("yes", "no"):
            return JSONResponse({"error": "answer: yes or no"}, status_code=400)
        try:
            line = await bot.answer_placement_for(v.reg, v.uid, ev.team, d.get("answer") == "yes", m.display_name, by_id=v.uid)
        except (RegistryError, ValueError, KeyError) as e:
            return JSONResponse({"error": str(e) or "bad request"}, status_code=400)
        return receipt(v, ev, m, line)

    @app.post("/api/me/notices/read")
    async def me_notices_read(request: Request):
        """Mark my notices read {ids?: [...]} (none = all)."""
        v, d = await body(request)
        ids = d.get("ids")
        if ids is not None and not isinstance(ids, list):
            return JSONResponse({"error": "ids: a list"}, status_code=400)
        n = v.reg.mark_read(v.uid, [str(i) for i in ids] if ids is not None else None)
        return {"message": f"{n} notice(s) marked read", "notices": inbox.notices_of(v.reg, v.uid)}

    # ---- officers: someone's inbox, and anything a member can do recorded for them (B5)
    @app.get("/api/admin/inbox")
    async def admin_inbox(request: Request, member: str = ""):
        """A member's open asks (wherever they wait) and notices, as their Me page shows them (officers; MCP member_inbox)."""
        v = await who(request, officer=True)
        m = resolve_member(v.reg, member)
        rs = bot.raids.store(v.reg)
        return {"uid": str(m.discord_id), "display_name": m.display_name, "dm": not m.dm_opt_out,
                "asks": inbox.asks_of(v.reg, rs, m.discord_id), "notices": inbox.notices_of(v.reg, m.discord_id)}

    @app.get("/api/admin/actions")
    async def admin_actions(request: Request, run: str = "", member: str = ""):
        """What an officer may record for `member` on `run` now (rc.member_actions — the SetWizard's list), each with the
        consequence sentence from code; `rev` = the member's state the list was drawn from (POST /api/admin/action)."""
        v = await who(request, officer=True)
        m = resolve_member(v.reg, member)
        rs, ev = run_of(v, run)
        acts = [{**a, "consequence": inbox.action_consequence(v.reg, ev, m, a["key"], by_id=v.uid),
                 # "Join as <alt>": the sentence for each character, so the site never edits one (design §2.3)
                 **({"by_character": {c: inbox.action_consequence(v.reg, ev, m, a["key"], c, by_id=v.uid) for c in a["characters"]}}
                    if a.get("needs") == "character" else {})}
                for a in rc.member_actions(v.reg, ev, m)] if rc.on_bench_side(v.reg, ev, m) else []
        s = ev.signups.get(str(m.discord_id))
        return {"uid": str(m.discord_id), "display_name": m.display_name, "run": ev.key, "actions": acts, "rev": rc.member_state_rev(ev, m),
                "now": f"{rc.LABELS.get(s.status, s.status)} as {s.character}" if s else "no answer yet"}

    @app.post("/api/admin/action")
    async def admin_action(request: Request):
        """Record one on-behalf action {run, member, action, character?, note?, rev?} — inbox.do_member_action, the verbs
        the SetWizard and every other surface call. A `rev` that no longer matches the member's state is a 409 saying
        what they changed to ('Kestrel is now Bench as Kestrel (since 5:12 PM)'), nothing written."""
        v, d = await body(request, officer=True)
        m = resolve_member(v.reg, d.get("member") if d.get("member") not in (None, "") else d.get("uid"))
        rs, ev = run_of(v, str(d.get("run") or ""))
        key = str(d.get("action") or "")
        if d.get("rev") and d["rev"] != rc.member_state_rev(ev, m):
            return JSONResponse({"error": rc.member_change_words(v.reg, ev, m) + " — look again.", "rev": rc.member_state_rev(ev, m)}, status_code=409)
        try:
            line = await inbox.do_member_action(bot, v.reg, rs, ev, m, key, character=(d.get("character") or None), note=d.get("note"),
                                                by=v.name, by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}
