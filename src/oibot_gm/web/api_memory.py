"""Chat memory on the web (design §5.61): officers read everything the bot remembers (GET /api/memories: search, a channel,
a member, paging) and forget any of it; a member reads what it remembers THEM saying on the Me page (GET /api/me
`memories`) and forgets their own. Every forget previews its confirm sentences first (consequences.forget_consequence)
and writes one audit line. Rows come from memory.MEMORIES; nothing here reads Discord."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import memory as mem
from .. import ops as ops_mod
from ..consequences import forget_consequence
from ..registry import MEMORY_WORDS

PAGE = 50
ACTOR_NEEDED = "A forget from MCP is made as an officer: set OIBOT_MCP_ACTOR to the officer's Discord id."
ALREADY = "That memory is already forgotten."


def row_json(reg, m: mem.Memory, names: dict[str, str] | None = None) -> dict:
    """One memory for a page: `when` in the guild's 12-hour words (the Clock rule), the channel's current name when the bot
    can see it, else the one recorded, `scope_word` from the channel's CURRENT setting."""
    try:
        when = reg.local12(m.at)
    except (TypeError, ValueError):
        when = m.when(mem.guild_tz(reg))
    mode = reg.memory_mode(m.channel) or m.scope
    return {"id": m.id, "when": when, "at": m.at, "channel": str(m.channel), "channel_name": (names or {}).get(str(m.channel)) or m.channel_name,
            "author": m.author, "author_id": str(m.author_id), "text": m.text, "context": m.context, "scope": mode, "scope_word": MEMORY_WORDS.get(mode, mode)}


def own_rows(reg, uid: int, limit: int = PAGE) -> dict:
    """The Me page's `memories`: what the bot remembers this member saying, newest first — selected by their Discord id,
    never their name (a display name may change after the message was kept)."""
    if mem.MEMORIES is None:
        return {"rows": [], "total": 0, "more": False}
    rows, total = mem.MEMORIES.list(reg, author_id=uid, limit=limit)
    return {"rows": [row_json(reg, m) for m in rows], "total": total, "more": total > len(rows)}


def install_memory_api(app: FastAPI, bot, *, who, body, publish=None) -> None:
    def names_of(reg) -> dict[str, str]:
        chans = bot.guild_channels(reg) if hasattr(bot, "guild_channels") else []
        return {str(c["id"]): c["name"] for c in chans}

    @app.get("/api/memories")
    async def memories_get(request: Request, q: str = "", channel: str = "", member: str = "", offset: int = 0, limit: int = PAGE):
        """Officers: what the bot remembers, newest first. `q` searches the words, `channel` / `member` are ids."""
        v = await who(request, officer=True)
        reg = v.reg
        if mem.MEMORIES is None:
            return {"rows": [], "total": 0, "more": False, "channels": reg.memory_channels(), "words": dict(MEMORY_WORDS), "ready": False, "model": "off"}
        kw = {"q": q.strip() or None, "channel": int(channel) if channel.strip().isdigit() else None, "author_id": int(member) if member.strip().isdigit() else None,
              "offset": max(0, offset), "limit": max(1, min(limit, 200))}
        rows, total = await asyncio.to_thread(mem.MEMORIES.list, reg, **kw)
        names = names_of(reg)
        from ..llm import decisions

        return {"rows": [row_json(reg, m, names) for m in rows], "total": total, "more": kw["offset"] + len(rows) < total,
                "channels": reg.memory_channels(), "words": dict(MEMORY_WORDS), "ready": decisions.key_set(), "model": mem.MEMORIES.model_state()}

    async def _forget(v, d: dict, key: str, *, officer: bool):
        if mem.MEMORIES is None:
            return JSONResponse({"error": "Memory is not running on this host."}, status_code=503)
        m = mem.MEMORIES.store(v.reg.key).get(v.reg.key, key)
        if m is None:
            return JSONResponse({"error": ALREADY}, status_code=404)
        if not officer and int(m.author_id) != int(v.uid):
            return JSONResponse({"error": "Only the officers can forget what someone else said."}, status_code=403)
        sentence = forget_consequence(m, v.uid, mem.guild_tz(v.reg))
        if d.get("preview"):
            return {"sentences": [sentence]}
        gone = await asyncio.to_thread(mem.MEMORIES.forget, v.reg, key, by=v.name, by_id=v.uid, via=v.via, officer=officer)
        if gone is None:
            return JSONResponse({"error": ALREADY}, status_code=404)
        whose = "their own words" if int(gone.author_id) == int(v.uid) else f"what {gone.author} said"
        await ops_mod.audited(bot.ops, v.reg, v.name, f"forgot {whose} on {gone.when(mem.guild_tz(v.reg))} in #{gone.channel_name}", via=v.via, by_id=v.uid)
        return {"message": sentence}

    @app.post("/api/memories/{key}/forget")
    async def memory_forget(request: Request, key: str):
        """Officers: forget any memory (preview → the sentences). From MCP the call acts as the named officer (D8)."""
        v, d = await body(request, officer=True)
        if v.via == "mcp" and getattr(v, "actor", None) is None:
            return JSONResponse({"error": ACTOR_NEEDED}, status_code=403)
        return await _forget(v, d, key, officer=True)

    @app.post("/api/me/memories/{key}/forget")
    async def my_memory_forget(request: Request, key: str):
        """A member forgets their OWN words (preview → the sentence). Never MCP: the token is nobody's own record."""
        v, d = await body(request)
        if v.via == "mcp":
            return JSONResponse({"error": "A member forgets their own words on the Me page, never through MCP."}, status_code=403)
        return await _forget(v, d, key, officer=False)
