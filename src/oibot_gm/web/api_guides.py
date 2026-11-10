"""The leveling-guide tool on the Me page (design §5.55): a member pastes the RestedXP import string issued to their own
account and its BattleTag, the bot unlocks it in a worker thread and keeps the outputs in that member's own folder under
out/guides/<guild>/<uid>/ — the companion addon, the one-file route explorer and the readable text, plus the dataset.
Nobody else can read them: every route resolves the folder from the session's uid, never from a parameter. The inputs
(the string, the BattleTag) are not stored or logged. The build is web only (configops.BY_DESIGN_ONLY `leveling_guide`): a
paste of this size has no Discord shape, and MCP's token is the owner's, so it may read a status but never build — the import
string must not land in a transcript. POST /api/me/guide/reissue (addendum 2026-11-05) locks the pasted guide to the
member's own other Battle.net account and answers the new string as a download: nothing written, nothing logged, the
folder untouched; the same core answers a DM (discord_bot.guide_convert).

The guild guide library (design §5.63) lives under
/api/guides/library: every member lists it, uploads a guild-made guide (unlocked once by their own BattleTag) and downloads
a version locked to their own BattleTag; officers withdraw and restore versions (a preview first; from MCP the write is
the named officer's, D8). Upload and download are never MCP: the string and the BattleTag stay out of transcripts. D7: no
BattleTag and no import string reaches a log record, an ops line or any response but the download's body — an unexpected
failure is logged by its type alone."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .. import guides as g
from .. import ops as ops_mod
from ..guides import library as lib

log = logging.getLogger(__name__)

NO_TOOL = lib.NO_TOOL  # the library's words: /guide answers the same
NOT_BUILT = "Nothing is built yet: paste your import string and BattleTag first."
FROM_MCP = ("A guild guide is uploaded and fetched by a member on the Me page or with /guide, locked to their own BattleTag; "
            "the import string and the BattleTag stay out of transcripts.")
ACTOR_NEEDED = ("Withdrawing or restoring a guild guide is an officer's decision: from MCP it needs OIBOT_MCP_ACTOR set to "
                "the officer's Discord id.")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")  # a confirm text → its sentences, as the readability harness splits them
EXPLORER_CSP = ("sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src data: blob:")


def sentences(text: str) -> list[str]:
    return [x for x in (p.strip() for p in _SENTENCE.split(text or "")) if x]


def install_guides_api(app: FastAPI, bot, *, who, body, publish=None, live_officer=None) -> None:
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

    @app.post("/api/me/guide/reissue")
    async def guide_reissue(request: Request):
        """The import string + the BattleTag it was issued to + the member's other account's BattleTag → the string that
        account imports, as a file. Read once and dropped: never written, never logged; the built folder is not touched."""
        v, d = await body(request)
        if v.via == "mcp":
            raise HTTPException(403, "An import string is re-issued from the Me page by the person who bought it; the string stays out of transcripts.")
        text, tag, new = str(d.get("text") or ""), str(d.get("battletag") or ""), str(d.get("new_battletag") or "")
        try:
            out = await asyncio.to_thread(g.reissue_guarded, text, tag, new)
        except g.GuideError as e:
            raise HTTPException(400, str(e)) from None
        fn = g.reissue_filename(new)
        ascii_fn = fn.encode("ascii", "replace").decode().replace("?", "_")
        return Response(out.encode(), media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{quote(fn)}", "Cache-Control": "no-store"})

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

    # ------------------------------------------------------------ the guild guide library (design §5.63)
    def library_json(reg, officer: bool) -> dict:
        """What the Me page's library reads: the rows (members never see a withdrawn version), the faction lines, whether
        the reader may withdraw, and the confirm sentence above each faction's Upload."""
        ok = lib.available(reg)
        return {"available": ok, "rows": lib.rows(reg, officer=bool(officer)) if ok else [],
                "factions": [{"id": f, "words": lib.FACTION_WORDS[f]} for f in lib.FACTIONS], "can_withdraw": bool(officer),
                "upload_sentences": {f: lib.upload_consequence(reg, f) for f in lib.FACTIONS}}

    def version_words(reg, v: lib.GuideVersion) -> str:
        return f"{lib.FACTION_WORDS[v.faction]} · {lib.when(reg, v)}"

    async def failed(v, what: str, e: BaseException) -> JSONResponse:
        """An unexpected failure under the library: the person reads configops.GENERIC_ERROR; the log and the owner's
        alert carry the exception's TYPE only — its words may hold the BattleTag or the string (D7), so neither the
        message nor a traceback is recorded."""
        from ..configops import GENERIC_ERROR

        kind = type(e).__name__
        log.error("guild guide %s failed: %s (guild=%s)", what, kind, v.reg.key)
        try:
            await bot.ops.emit(v.reg.config, "error", f"[{v.via}] guild guide {what} failed: {kind}")
        except Exception:  # noqa: BLE001 — the reply still goes out
            log.error("ops emit failed")
        return JSONResponse({"error": GENERIC_ERROR}, status_code=500)

    def guide_error(e: g.GuideError) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=404 if str(e) == lib.NO_VERSION else 400)

    @app.get("/api/guides/library")
    async def library_get(request: Request):
        v = await who(request)
        return await asyncio.to_thread(library_json, v.reg, v.officer)

    @app.post("/api/guides/library/upload")
    async def library_upload(request: Request):
        """{text, battletag, faction}: a member keeps a guild-made guide as a new version of the faction's line. The string
        is unlocked once with the BattleTag and dropped; only the guide texts are kept."""
        v, d = await body(request)
        if v.via == "mcp":
            raise HTTPException(403, FROM_MCP)
        if not lib.available(v.reg):
            raise HTTPException(400, NO_TOOL)
        try:
            gv = await asyncio.to_thread(lib.upload, v.reg, str(d.get("text") or ""), str(d.get("battletag") or ""), str(d.get("faction") or ""), v.uid, v.name)
        except g.GuideError as e:
            return guide_error(e)
        except Exception as e:  # noqa: BLE001 — D7: never the exception's words
            return await failed(v, "upload", e)
        finally:
            d.clear()
        words = version_words(v.reg, gv)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"guild guide uploaded: {words}", via=v.via, by_id=v.uid)
        return {"message": f"Uploaded: {words}.", **await asyncio.to_thread(library_json, v.reg, v.officer)}

    @app.post("/api/guides/library/{vid}/download")
    async def library_download(request: Request, vid: str):
        """{battletag}: the version locked to that BattleTag, as a file. Nothing is written and no ops line. A withdrawn
        version is an officer's only, decided live (live_officer: a demoted officer's cached role never fetches one)."""
        v, d = await body(request)
        if v.via == "mcp":
            raise HTTPException(403, FROM_MCP)
        if not lib.available(v.reg):
            raise HTTPException(400, NO_TOOL)
        tag = str(d.get("battletag") or "")
        gv = await asyncio.to_thread(lib.get, v.reg, vid)
        officer = False
        if gv is not None and gv.withdrawn_at:  # an officer's only: decided now (PRIV_LIVE), never the page's cached role
            officer = bool(await live_officer(v)) if live_officer is not None else bool(v.officer)
        try:
            out, gv = await asyncio.to_thread(lambda: (lib.issue(v.reg, vid, tag, officer=officer), lib.get(v.reg, vid)))
        except g.GuideError as e:
            return guide_error(e)
        except Exception as e:  # noqa: BLE001 — D7: never the exception's words
            return await failed(v, "download", e)
        fn = lib.issue_filename(v.reg, gv, tag)
        ascii_fn = fn.encode("ascii", "replace").decode().replace("?", "_")
        return Response(out.encode(), media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{quote(fn)}", "Cache-Control": "no-store"})

    async def officer_verb(request: Request, vid: str, verb: str):
        """Withdraw / restore: officer rights decided now (PRIV_LIVE; a stale membership answers 503 in `who`), `preview`
        answers the confirm sentences and writes nothing; from MCP the write needs the actor (D8) and is theirs."""
        v, d = await body(request, officer=True)
        if live_officer is not None and not await live_officer(v):
            raise HTTPException(403, "Officers only.")
        reg = v.reg
        if not lib.available(reg):
            raise HTTPException(400, NO_TOOL)
        preview = bool(d.get("preview"))
        if not preview and v.via == "mcp" and not getattr(v, "actor", None):
            raise HTTPException(403, ACTOR_NEEDED)
        gv = await asyncio.to_thread(lib.get, reg, vid)
        if gv is None:
            raise HTTPException(404, lib.NO_VERSION)
        if preview:
            if verb == "withdraw" and gv.withdrawn_at:  # the verb's own refusal, before anyone presses
                raise HTTPException(400, f"Already withdrawn by {gv.withdrawn_by or 'an officer'} on {reg.local12(gv.withdrawn_at)}.")
            if verb == "restore" and not gv.withdrawn_at:
                raise HTTPException(400, lib.NOT_WITHDRAWN)
            text = await asyncio.to_thread(lambda: lib.withdraw_consequence(reg, gv, by_id=v.uid) if verb == "withdraw"
                                           else lib.restore_consequence(reg, gv))
            return {"preview": True, "sentences": sentences(text)}
        fn = lib.withdraw if verb == "withdraw" else lib.restore
        try:
            gv = await asyncio.to_thread(fn, reg, vid, v.name, v.uid)
        except g.GuideError as e:
            return guide_error(e)
        except Exception as e:  # noqa: BLE001
            return await failed(v, verb, e)
        words = version_words(reg, gv)
        done = "withdrawn" if verb == "withdraw" else "restored"
        await ops_mod.audited(bot.ops, reg, v.name, f"guild guide {done}: {words}", via=v.via, by_id=v.uid)
        return {"message": f"{done.title()}: {words}.", **await asyncio.to_thread(library_json, reg, True)}

    @app.post("/api/guides/library/{vid}/withdraw")
    async def library_withdraw(request: Request, vid: str):
        return await officer_verb(request, vid, "withdraw")

    @app.post("/api/guides/library/{vid}/restore")
    async def library_restore(request: Request, vid: str):
        return await officer_verb(request, vid, "restore")

    if publish is not None:
        def guides_saved(guild_key: str) -> None:
            publish("guides", guild_key)  # the Me page's library (useLive 'guides') reloads on every upload / withdraw / restore

        lib.GUIDE_LISTENERS[:] = [f for f in lib.GUIDE_LISTENERS if getattr(f, "__name__", "") != guides_saved.__name__] + [guides_saved]
