"""The leveling-guide tool on the Me page (design §5.55): a member pastes the RestedXP import string issued to their own
account and its BattleTag, the bot unlocks it in a worker thread and keeps the outputs in that member's own folder under
out/guides/<guild>/<uid>/ — the companion addon, the one-file route explorer and the readable text, plus the dataset.
Nobody else can read them: every route resolves the folder from the session's uid, never from a parameter. The inputs
(the string, the BattleTag) are not stored or logged. Web only (configops.BY_DESIGN_ONLY `leveling_guide`): a paste of
this size has no Discord shape, and MCP's token is the owner's, so it may read a status but never build — the import
string must not land in a transcript."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import guides as g

NO_TOOL = "There is no leveling-guide tool for this game version yet."
NOT_BUILT = "Nothing is built yet: paste your import string and BattleTag first."
EXPLORER_CSP = ("sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src data: blob:")


def install_guides_api(app: FastAPI, bot, *, who, body) -> None:
    def game_of(reg) -> g.GuideGame | None:
        doc = getattr(reg.profile, "guides", None) or {}
        return g.GuideGame.from_profile(doc) if doc.get("zones") else None

    def status_json(reg, uid: int) -> dict:
        game = game_of(reg)
        out = {"available": game is not None, "built": False, "files": [{"name": fn, "words": words} for fn, (_ct, words) in g.FILES.items()],
               "starter_pack": bool(g.addon_zip_path())}
        st = g.status(g.member_dir(reg.key, uid)) if game else None
        if st:
            built = reg.local12(datetime.fromisoformat(st["built_at"])) if st.get("built_at") else ""
            out.update({"built": True, "built_when": built, "guides": st["guides"], "steps": st["steps"], "starter": st.get("starter", 0),
                        "range": st.get("range", ""), "groups": st.get("groups", []), "sizes": st.get("sizes", {})})
        return out

    @app.get("/api/me/guide")
    async def guide_status(request: Request):
        v = await who(request)
        return status_json(v.reg, v.uid)

    @app.post("/api/me/guide")
    async def guide_build(request: Request):
        v, d = await body(request)
        if v.via == "mcp":
            raise HTTPException(403, "A guide is built from the Me page by the person it was issued to; the import string stays out of transcripts.")
        game = game_of(v.reg)
        if game is None:
            raise HTTPException(400, NO_TOOL)
        text, tag = str(d.get("text") or ""), str(d.get("battletag") or "")
        lock = g.lock_for(v.reg.key, v.uid)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, g.BUSY)
        try:
            st = await asyncio.to_thread(g.build, text, tag, g.member_dir(v.reg.key, v.uid), game, g.addon_zip_path())
        except g.GuideError as e:
            raise HTTPException(400, str(e)) from None
        finally:
            lock.release()
        n = st["guides"]
        return {"message": f"{n} guide{'' if n == 1 else 's'} unlocked · {st['steps']} steps", **status_json(v.reg, v.uid)}

    @app.post("/api/me/guide/clear")
    async def guide_clear(request: Request):
        v, _d = await body(request)
        had = await asyncio.to_thread(g.clear, g.member_dir(v.reg.key, v.uid))
        return {"message": "Your guide files are gone; paste the string again whenever you like." if had else NOT_BUILT, **status_json(v.reg, v.uid)}

    def built_file(reg, uid: int, name: str) -> Path:
        if name not in g.FILES:
            raise HTTPException(404, "No such file.")
        folder = g.member_dir(reg.key, uid)
        if g.status(folder) is None:
            raise HTTPException(404, NOT_BUILT)
        return folder / name

    @app.get("/api/me/guide/explorer")
    async def guide_explorer(request: Request):
        """The explorer inline on the Me page (an iframe): the same file as the download, sandboxed by its own CSP so the
        guide's text runs with no reach into the site."""
        v = await who(request)
        path = built_file(v.reg, v.uid, "explorer.html")
        html = await asyncio.to_thread(path.read_text)
        return HTMLResponse(html, headers={"Content-Security-Policy": EXPLORER_CSP, "Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN"})

    @app.get("/api/me/guide/file/{name}")
    async def guide_file(request: Request, name: str):
        v = await who(request)
        path = built_file(v.reg, v.uid, name)
        return FileResponse(path, media_type=g.FILES[name][0], filename=name, headers={"Cache-Control": "no-store"})
