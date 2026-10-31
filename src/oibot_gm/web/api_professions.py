"""The Professions page's routes (design §5.60): the catalog and the crafters per profession for every member, the
finder, and the officers' rename / merge (preview first, then the write through professions.py → the table verb)."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import ops as ops_mod
from .. import professions as pr
from ..registry import RegistryError


def install_professions_api(app: FastAPI, bot, *, who, body, publish=None) -> None:
    def crafter_json(reg, c: pr.Crafter) -> dict:
        """`cls` = the character's class, for the page's class icon and colour (the name is never a ping)."""
        m = reg.members.get(c.uid)
        ch = next((x for x in m.active() if x.label == c.label), None) if m else None
        return {"label": c.label, "member": c.member, "uid": str(c.uid), "skill": c.skill, "profession": c.profession, "cls": ch.cls if ch else None}

    def entry_json(reg, e: pr.CatalogEntry) -> dict:
        return {"key": e.key, "name": e.name, "profession": e.profession, "id": e.id, "crafters": [crafter_json(reg, c) for c in e.crafters]}

    @app.get("/api/professions")
    async def professions_get(request: Request):
        """Every member reads the catalog: each profession with its crafters (highest skill first) and every registered
        recipe with its makers. `officer` = the reader may rename or merge."""
        v = await who(request)
        reg = v.reg
        return {"professions": [{"id": d.id, "name": d.name, "group": d.group, "icon": d.icon, "crafters": [crafter_json(reg, c) for c in pr.crafters_of(reg, d.id)]}
                                for d in reg.profile.professions.values()],
                "recipes": [entry_json(reg, e) for e in pr.catalog(reg)], "officer": bool(v.officer)}

    @app.get("/api/professions/find")
    async def professions_find(request: Request, q: str = ""):
        """The finder: the registered recipes matching a typed name (professions.who_can_make)."""
        v = await who(request)
        return {"q": q, "recipes": [entry_json(v.reg, e) for e in pr.who_can_make(v.reg, q)]}

    async def _maintain(v, d: dict, fn, *args):
        """An officer's rename / merge: `preview` answers the sentences and writes nothing; a write saves each member's
        record once (the table verb) and writes one audit line, the head sentence."""
        preview = bool(d.get("preview"))
        try:
            if preview:
                said = fn(v.reg, *args, v.name, by_id=v.uid, preview=True)
            else:  # one git commit per member holding the recipe: off the event loop
                said = await asyncio.to_thread(fn, v.reg, *args, v.name, by_id=v.uid, preview=False)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not preview:
            await ops_mod.audited(bot.ops, v.reg, v.name, said[0], via=v.via, by_id=v.uid)
            if publish is not None:
                publish("member", v.reg.key)  # an open Professions / Members / Me page reloads
        return {"sentences": said, "message": said[0]}

    @app.post("/api/professions/rename")
    async def professions_rename(request: Request):
        """Officers: {key, name, preview?} — one recipe's spelling on every record that holds it."""
        v, d = await body(request, officer=True)
        return await _maintain(v, d, pr.rename_recipe, str(d.get("key") or ""), str(d.get("name") or ""))

    @app.post("/api/professions/merge")
    async def professions_merge(request: Request):
        """Officers: {key, into, preview?} — two spellings of one recipe become the other's (same profession only)."""
        v, d = await body(request, officer=True)
        return await _maintain(v, d, pr.merge_recipe, str(d.get("key") or ""), str(d.get("into") or ""))
