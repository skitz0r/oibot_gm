"""The Professions page's routes (design §5.60, armory link §4.3–4.4): the catalog and the crafters per profession for
every member, the finder over the game's whole list, and the officers' fix for recipes typed before the list (preview
first, then the write through professions.fix_unmatched → the table verb)."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import ops as ops_mod
from .. import professions as pr
from ..registry import RegistryError

FIND_LIMIT = 25  # rows the finder returns; `total` counts every match


def install_professions_api(app: FastAPI, bot, *, who, body, publish=None) -> None:
    def crafter_json(reg, c: pr.Crafter) -> dict:
        """`cls` = the character's class, for the page's class icon and colour (the name is never a ping)."""
        m = reg.members.get(c.uid)
        ch = next((x for x in m.active() if x.label == c.label), None) if m else None
        return {"label": c.label, "member": c.member, "uid": str(c.uid), "skill": c.skill, "profession": c.profession, "cls": ch.cls if ch else None}

    def entry_json(reg, e: pr.CatalogEntry) -> dict:
        """`listed` = a recipe of the game's list (then `category`, `makes` {id, name, quantity[, quantity_max]} or null for
        an enchant, and `reagents` [{id, name, quantity}]); an unmatched typed recipe has null / [] there."""
        d = e.recipe
        return {"key": e.key, "name": e.name, "profession": e.profession, "id": e.id, "crafters": [crafter_json(reg, c) for c in e.crafters],
                "listed": d is not None, "category": d.category if d else None, "makes": dict(d.makes) if d and d.makes else None,
                "reagents": [dict(x) for x in d.reagents] if d else []}

    @app.get("/api/professions")
    async def professions_get(request: Request):
        """Every member reads the catalog: each profession with its crafters (highest skill first) and every registered
        recipe with its makers. `officer` = the reader may fix recipes typed before the game's list; `unmatched` = those
        recipes ({uid, member, label, profession, name}; [] for a member and for a version without the list)."""
        v = await who(request)
        reg = v.reg
        return {"professions": [{"id": d.id, "name": d.name, "group": d.group, "icon": d.icon, "crafters": [crafter_json(reg, c) for c in pr.crafters_of(reg, d.id)]}
                                for d in reg.profile.professions.values()],
                "recipes": [entry_json(reg, e) for e in pr.catalog(reg)], "officer": bool(v.officer),
                "unmatched": [{**r, "uid": str(r["uid"])} for r in pr.unmatched(reg)] if v.officer else []}

    @app.get("/api/professions/find")
    async def professions_find(request: Request, q: str = "", profession: str = ""):
        """The finder (professions.who_can_make): the game's whole list and the typed recipes somebody makes, what somebody
        makes first; a listed recipe nobody makes has no crafters. `profession` = that profession's recipes only (the
        editor's search, Pick the recipe…). At most FIND_LIMIT rows; `total` = every match. `message` = the answer in
        words (answer_lines: MCP reads it); an empty `q` answers nothing."""
        v = await who(request)
        hits = pr.who_can_make(v.reg, q, None, profession=profession.strip() or None)
        shown = hits[:FIND_LIMIT]
        return {"q": q, "recipes": [entry_json(v.reg, e) for e in shown], "total": len(hits),
                "message": "\n".join(pr.answer_lines(v.reg, shown, len(hits))) if q.strip() else ""}

    @app.post("/api/professions/unmatched")
    async def professions_unmatched(request: Request):
        """Officers: {uid, label, profession, name, recipe_id (null = Remove), preview?} — a recipe typed before the game's
        list takes the picked recipe's place, or comes off. `preview` answers the sentences and writes nothing; a write
        saves the member's record once (the table verb) and writes one audit line."""
        v, d = await body(request, officer=True)
        try:
            uid = int(str(d.get("uid") or "0"))
            rid = d.get("recipe_id")
            rid = int(rid) if rid not in (None, "") else None
        except (TypeError, ValueError):
            return JSONResponse({"error": pr.NOT_A_RECIPE}, status_code=400)
        args = (v.reg, uid, str(d.get("label") or ""), str(d.get("profession") or ""), str(d.get("name") or ""), rid, v.name)
        preview = bool(d.get("preview"))
        try:
            if preview:
                said = pr.fix_unmatched(*args, by_id=v.uid, preview=True)
            else:  # a git commit: off the event loop
                said = await asyncio.to_thread(pr.fix_unmatched, *args, by_id=v.uid, preview=False)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not preview:
            await ops_mod.audited(bot.ops, v.reg, v.name, said[0], via=v.via, by_id=v.uid)
            if publish is not None:
                publish("member", v.reg.key)  # an open Professions / Members / Me page reloads
        return {"sentences": said, "message": said[0]}
