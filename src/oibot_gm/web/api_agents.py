"""The news review's API (design.md §5.25): news kept from the #news channel, proposals and their states, the
effective profile slice the review compares against, and the local jobs' status for the Agents page.

Officers read everything and approve/dismiss (the same verb as the Discord card's buttons). The apply job's steps
(applying / applied / failed / reverted) come only from the MCP bearer identity (`via == "mcp"`), which is what the
local jobs use. Nothing here fetches a news article: the items are the webhook's own text."""
from __future__ import annotations

import asyncio
import inspect

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import agents, news
from ..news import ProposalError, ProposalStore
from ..registry import RegistryError


def install_agents_api(app: FastAPI, bot, *, who, body, publish) -> None:
    def store():
        return bot.registries.store

    def is_job(v) -> bool:
        return getattr(v, "via", "web") == "mcp"

    async def card(reg, p) -> None:
        """Post or edit the proposal's Discord card (the bot's NewsMixin); tests run without one."""
        fn = getattr(bot, "proposal_card_update", None)
        if fn is not None:
            r = fn(reg, p)
            if inspect.isawaitable(r):
                await r
        publish("agents")

    def news_json(reg, r: dict) -> dict:
        return {**r, "posted_label": reg.local12(r["posted_at"]) if r.get("posted_at") else "", "ingested_label": reg.local12(r["ingested_at"]) if r.get("ingested_at") else ""}

    def proposal_json(reg, p) -> dict:
        d = p.model_dump()
        d["created_label"] = reg.local12(p.created_at)
        d["decided_label"] = reg.local12(p.decided_at) if p.decided_at else ""
        d["history"] = [{**h, "at_label": reg.local12(h["at"]) if h.get("at") else ""} for h in p.history]
        return d

    # ---- news
    @app.get("/api/news")
    async def news_list(request: Request, since: str | None = None, limit: int = news.MAX_ITEMS_PER_CALL):
        """Items kept since an ISO time (by when the bot recorded them), oldest first, plus the keyword lists."""
        v = await who(request, officer=True)
        try:
            rows = news.NewsStore(store(), v.reg.key).since(since, max(1, min(limit, news.MAX_ITEMS_PER_CALL)))
        except ValueError:
            raise HTTPException(400, "since must be an ISO date-time")
        return {"items": [news_json(v.reg, r) for r in rows], "keywords": news.keyword_sets(v.reg), "channel_id": str(v.reg.config.news_channel_id or "") or None}

    @app.post("/api/news/backfill")
    async def news_backfill(request: Request):
        """Re-read the news channel's recent history (posts made while the bot was down). The review job calls it first."""
        v, _ = await body(request, officer=True)
        fn = getattr(bot, "news_backfill", None)
        added = await fn(v.reg) if fn is not None else 0
        if added:
            publish("agents")
        return {"added": added, "message": f"{added} new news item(s) from the channel's history"}

    # ---- proposals
    @app.get("/api/proposals")
    async def proposals(request: Request, state: str | None = None):
        v = await who(request, officer=True)
        ps = ProposalStore(store(), v.reg.key)
        return {"proposals": [proposal_json(v.reg, p) for p in reversed(ps.all(state))], "states": list(news.STATES)}

    @app.get("/api/proposals/{pid}")
    async def proposal(request: Request, pid: str):
        v = await who(request, officer=True)
        try:
            return proposal_json(v.reg, ProposalStore(store(), v.reg.key).get(pid))
        except ProposalError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/proposals")
    async def proposal_post(request: Request):
        """A new proposal (the review job, through MCP post_proposal): validated, stored, carded in the ops channel."""
        v, d = await body(request, officer=True)
        try:
            p = await asyncio.to_thread(ProposalStore(store(), v.reg.key).create, v.reg, d, "review" if is_job(v) else v.name)
        except (ProposalError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await card(v.reg, p)
        await bot.ops.emit(v.reg.config, "info", f"news proposal {p.id}: {p.title} ({p.kind}, {p.confidence})")
        return {"message": f"proposal {p.id} posted: {p.title}", "id": p.id, "proposal": proposal_json(v.reg, p)}

    async def move(v, pid: str, state: str, commit: str | None = None, note: str | None = None):
        try:
            p = await asyncio.to_thread(ProposalStore(store(), v.reg.key).move, pid, state, v.name, officer=v.officer, job=is_job(v), by_id=v.uid, commit=commit, note=note)
        except PermissionError as e:
            raise HTTPException(403, str(e)[:1].upper() + str(e)[1:] + ".")
        except ProposalError as e:
            raise HTTPException(400, str(e))
        await card(v.reg, p)
        return p

    @app.post("/api/proposals/{pid}/resolve")
    async def proposal_resolve(request: Request, pid: str):
        """Move a proposal: approved | dismissed (officers, like the card's buttons); applying | applied | failed |
        reverted (the apply job only), with the commit hash and a note."""
        v, d = await body(request, officer=True)
        state = str(d.get("state") or "")
        p = await move(v, pid, state, (str(d.get("commit") or "").strip() or None), (str(d["note"]) if d.get("note") is not None else None))
        await bot.ops.emit(v.reg.config, "info", f"news proposal {p.id} → {p.state} (by {v.name})" + (f" @ {p.commit}" if p.commit and state in ("applied", "reverted") else "") + (f": {p.note}" if p.note else ""))
        return {"message": f"proposal {p.id} is {p.state}", "proposal": proposal_json(v.reg, p)}

    @app.post("/api/proposals/{pid}/apply")
    async def proposal_apply(request: Request, pid: str):
        """The apply job, for a guild setting: the typed config op runs through configops (the plain-text path),
        no restart. approved → applying → applied | failed, the card following each step."""
        from .. import configops

        v, _ = await body(request, officer=True)
        if not is_job(v):
            raise HTTPException(403, "Only the apply job applies proposals; approve it and it runs within 15 minutes.")
        try:
            p = ProposalStore(store(), v.reg.key).get(pid)
        except ProposalError as e:
            raise HTTPException(404, str(e))
        if p.kind != "guild_setting":
            raise HTTPException(400, f"{p.id} is a {p.kind} proposal: only guild settings are applied here")
        p = await move(v, pid, "applying")
        try:
            op = configops.ConfigOp.model_validate(p.edit)
            line = await configops.apply_async(v.reg, op, f"news review (approved by {p.decided_by or '?'})", True, bot=bot)
        except (RegistryError, ValueError) as e:
            p = await move(v, pid, "failed", note=f"not applied: {e}")
            await bot.ops.emit(v.reg.config, "warn", f"news proposal {p.id} failed: {e}")
            return JSONResponse({"error": str(e), "proposal": proposal_json(v.reg, p)}, status_code=400)
        p = await move(v, pid, "applied", note=line)
        await bot.ops.emit(v.reg.config, "info", f"news proposal {p.id} applied: {line}")
        return {"message": f"applied: {line}", "proposal": proposal_json(v.reg, p)}

    # ---- the effective profile the review compares against
    @app.get("/api/agents/profile")
    async def profile_slice(request: Request, section: str | None = None):
        v = await who(request, officer=True)
        sections = ("buffs", "families", "raids", "comp_rules")
        if section and section not in sections:
            raise HTTPException(400, f"section is one of {', '.join(sections)}")
        return effective_profile(v.reg, section)

    # ---- the local jobs (monitor)
    @app.get("/api/agents")
    async def agents_overview(request: Request):
        v = await who(request, officer=True)
        tz = v.reg.tz
        ov = await asyncio.to_thread(agents.overview, agents.OUT, tz)
        ps = ProposalStore(store(), v.reg.key)
        runs = [{**r, "started_label": agents.clock12(r["started"], tz)} for r in await asyncio.to_thread(agents.list_runs, None, agents.OUT)]
        return {**ov, "runs": runs, "owner": v.owner, "waiting": len(ps.all("proposed")), "approved": len(ps.all("approved"))}

    @app.get("/api/agents/run/{run_id}")
    async def agents_run(request: Request, run_id: str):
        v = await who(request, officer=True)
        try:
            events = await asyncio.to_thread(agents.read_run, run_id, agents.OUT)
        except ValueError as e:
            raise HTTPException(404, str(e))
        when = v.reg.local12
        st = agents.read_status(run_id.split("-", 1)[0], agents.OUT)
        live = st.get("run_id") == run_id and st.get("state") == "running"
        return {"id": run_id, "live": live, "outcome": agents.outcome(events), "steps": agents.steps(events, lambda x: when(x) if x else "")}

    @app.post("/api/agents/run")
    async def agents_kick(request: Request):
        """Owner: start a job now. launchd starts it (`launchctl kickstart`, the job's own LaunchAgent) — the bot
        never runs Claude itself, and a job that is already running is left alone."""
        v, d = await body(request, officer=True)
        if not v.owner:
            raise HTTPException(403, "Owner only.")
        job = str(d.get("job") or "review")
        if job not in agents.JOBS:
            raise HTTPException(400, f"job is one of {', '.join(agents.JOBS)}")
        st = agents.read_status(job, agents.OUT)
        if st["state"] == "running":
            return JSONResponse({"error": f"{agents.JOBS[job]['name']} is already running"}, status_code=409)
        label = agents.JOBS[job]["label"]
        ld = await asyncio.to_thread(agents.launchd_status, label)
        if not ld["loaded"]:
            return JSONResponse({"error": f"{agents.JOBS[job]['name']} isn't installed on this Mac (scripts/agents/install.sh)"}, status_code=409)
        ok, out = await asyncio.to_thread(agents.kickstart, label)
        if not ok:
            return JSONResponse({"error": f"launchd refused: {out[:200]}"}, status_code=500)
        await bot.ops.emit(v.reg.config, "info", f"{agents.JOBS[job]['name']} started by {v.name}")
        return {"message": f"{agents.JOBS[job]['name']} started"}


def effective_profile(reg, section: str | None = None) -> dict:
    """The profile as the guild sees it: game defaults with the guild's overrides applied and marked (`overridden`:
    the fields the guild changed, `default`: what the game file says). `file` is where the default lives;
    `setting_op` is the config op that overrides it (a guild setting) — anything else is a profile edit."""
    prof, base, cfg = reg.profile, reg.base_profile, reg.config
    root = f"profiles/{base.root.name}"
    out: dict = {"profile": base.name, "files": {"buffs": f"{root}/buffs.yaml", "families": f"{root}/buffs.yaml", "raids": f"{root}/raids.yaml", "comp_rules": f"{root}/comp_rules.yaml"}}
    if section in (None, "buffs"):
        rows = []
        for b in prof.buffs:
            over = cfg.buffs.get(b.id, {})
            d0 = next((x for x in base.buffs if x.id == b.id), None)
            rows.append({"id": b.id, "name": b.name, "providers": list(b.providers), "scope": b.scope, "kind": b.kind, "stacking": b.stacking, "slot": b.slot,
                         "family": b.family_id, "strength": b.strength, "status": b.status, "note": b.note, "value": dict(b.value),
                         "overridden": sorted(over), "default": {k: getattr(d0, "family_id" if k == "family" else k, None) for k in over} if d0 else {}})
        out["buffs"] = {"setting_op": "aura_set (target = buff id; field = scope|family|strength|status|note)", "items": rows}
    if section in (None, "families"):
        rows = []
        for fid, f in prof.families.items():
            over = cfg.families.get(fid, {})
            d0 = base.families.get(fid)
            rows.append({"id": fid, "name": f.name, "value": dict(f.value), "status": f.status, "note": f.note, "buffs": [b.id for b in prof.buffs if b.family_id == fid],
                         "overridden": sorted(over), "default": {k: (dict(getattr(d0, k)) if k == "value" else getattr(d0, k, None)) for k in over} if d0 else {}})
        out["families"] = {"setting_op": "family_set (target = family id; field = name|status|note|value|value:<key>)", "items": rows}
    if section in (None, "raids"):
        rows = []
        for rid, base_rd in base.raids.items():
            over = cfg.raids.get(rid, {})
            eff = reg.raid_def(rid)
            rows.append({"id": rid, **{k: v for k, v in eff.items() if k not in ("bosses",)}, "overridden": sorted(over),
                         "default": {k: base_rd.get(k) for k in over if k in base_rd}})
        out["raids"] = {"setting_op": "raid_set (target = raid id; field = lockout_days|duration_hours|slots|… see plain_change)", "items": rows}
    if section in (None, "comp_rules"):
        out["comp_rules"] = {"setting_op": None, "items": prof.comp_rules}
    return out
