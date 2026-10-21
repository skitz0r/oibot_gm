"""The Policy page and the Help page on the web (dual interfaces phase 3, design §5.44; D36 Policy gets its own page, D38
the manual shows officer sections to members, tagged).

Policy (officers, as in Discord): GET /api/policy = every document's state (policy.policy_state: Written vs Applied with
`compiled_at`, `differs` when the text's sha isn't the one the live reading was made from, the pending edit with its
`pending_by`); GET /api/policy/{doc} one of them; PUT /api/policy/{doc} {text} saves a DRAFT (no length cap here — the
site is where long text is edited); POST /api/policy/{doc}/append {text} = THE policy_append verb (refused while another
officer's draft waits, naming them); /compile → the reading; /confirm and /discard. Every step is the same function the
Discord flows and plain text call (policy.begin_edit / append_rule / compile_pending / confirm_pending / discard_pending),
on disk (`<doc>.pending.json`), audited through ops.audited.

Help (everyone logged in): GET /api/manual = docs/manual.md split by its own `## ` headings (help.manual_sections,
mtime-cached), each tagged officers | owner | none but never hidden, plus the live command tree filtered by the viewer's
tier (help.command_rows)."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import ops as ops_mod


def install_policy_api(app: FastAPI, bot, *, who, body) -> None:
    from .. import help as help_mod, policy as pol

    def policy_store(reg):
        return pol.PolicyStore(bot.registries.store, reg.key)

    def policy_doc(doc: str) -> str:
        if doc not in pol.DOCS:
            raise HTTPException(400, f"doc is one of {', '.join(pol.DOCS)}")
        return doc

    def policy_json(reg, doc: str) -> dict:
        return pol.policy_state(policy_store(reg), doc, reg.local12)

    def refused(e: Exception) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/policy")
    async def policy_all(request: Request):
        """Every policy document's state (the Policy page)."""
        v = await who(request, officer=True)
        return {"docs": [policy_json(v.reg, d) for d in pol.DOCS]}

    @app.get("/api/policy/{doc}")
    async def policy_get(request: Request, doc: str):
        """One policy document: its text, the live reading (when, by whom, whether the text still reads that way) and
        the pending edit."""
        v = await who(request, officer=True)
        return policy_json(v.reg, policy_doc(doc))

    @app.put("/api/policy/{doc}")
    async def policy_put(request: Request, doc: str):
        """Officers: {text} replaces the document as a DRAFT (the live reading is unchanged until confirmed; the persona
        too). Next: compile, then confirm or discard (policy.begin_edit)."""
        v, d = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            line = await asyncio.to_thread(pol.begin_edit, policy_store(v.reg), doc, str(d.get("text") or ""), v.name, by_id=v.uid)
        except ValueError as e:
            return refused(e)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

    @app.post("/api/policy/{doc}/append")
    async def policy_append(request: Request, doc: str):
        """Officers: {text} = one rule, appended as '- <text>' to the loot or comp document as a draft (the
        `policy_append` verb; Discord's /gm rule and plain text 'add a loot rule: …' write the same). Refused while
        another officer's draft waits for confirmation — it names them."""
        v, d = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            _, line = await asyncio.to_thread(pol.append_rule, policy_store(v.reg), doc, str(d.get("text") or ""), v.name, by_id=v.uid, when=v.reg.local12)
        except ValueError as e:
            return refused(e)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

    @app.post("/api/policy/{doc}/compile")
    async def policy_compile(request: Request, doc: str):
        """Officers: the bot's reading of the document as it is now (an LLM call for loot / comp; the persona's reading
        is its own text). Nothing goes live: confirm makes it live, discard reverts the draft."""
        v, _ = await body(request, officer=True)
        doc = policy_doc(doc)
        provider = getattr(getattr(bot, "ctx", None), "provider", None)
        try:
            compiled, reading = await asyncio.to_thread(pol.compile_pending, policy_store(v.reg), doc, provider)
        except pol.PolicyPendingError as e:
            return JSONResponse({"error": str(e)}, status_code=400 if doc == "persona" or provider is not None else 503)
        except Exception as e:  # noqa: BLE001 — provider/schema errors: the caller can retry
            from ..configops import report_error

            text = await report_error(bot.ops, v.reg.config, e, f"[{v.via}] {v.name} policy compile {doc}")
            return JSONResponse({"error": f"could not compile the {doc} policy — try again in a minute ({text})"}, status_code=502)
        return {"doc": doc, "reading": reading, "compiled": compiled.model_dump() if compiled is not None else None,
                "message": "confirm to make this reading live, or discard the edit", "state": policy_json(v.reg, doc)}

    @app.post("/api/policy/{doc}/confirm")
    async def policy_confirm(request: Request, doc: str):
        v, _ = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            line = await asyncio.to_thread(pol.confirm_pending, policy_store(v.reg), doc, v.name)
        except ValueError as e:
            return refused(e)
        await ops_mod.audited(bot.ops, v.reg, v.name, f"confirmed the {doc} policy: {line}", via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

    @app.post("/api/policy/{doc}/discard")
    async def policy_discard(request: Request, doc: str):
        v, _ = await body(request, officer=True)
        doc = policy_doc(doc)
        try:
            line = await asyncio.to_thread(pol.discard_pending, policy_store(v.reg), doc, v.name)
        except ValueError as e:
            return refused(e)
        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line, **policy_json(v.reg, doc)}

    # ---- the manual (members and officers)
    @app.get("/api/manual")
    async def manual(request: Request):
        """The manual by its own sections (officer / owner sections tagged, never hidden — D38) and the commands this
        viewer may run (the live command tree by tier)."""
        v = await who(request)
        tier = "owner" if v.owner else "officer" if v.officer else "member"
        sections = await asyncio.to_thread(help_mod.manual_sections)
        return {"sections": sections, "commands": help_mod.command_rows(getattr(bot, "tree", None), tier), "tier": tier}
