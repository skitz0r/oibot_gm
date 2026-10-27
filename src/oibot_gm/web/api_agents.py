"""The news review's API (design.md §5.26; dual interfaces phase 3, design §5.45): news kept from the #news channel,
proposals and their states, the effective profile slice the review compares against, and the local jobs' status for
the Agents page.

Officers read everything and approve/dismiss through NewsMixin.decide_proposal (the same verb as the Discord card's
decide screen and the `proposal` op); approving a game-data (`profile`) proposal is the owner's. The apply job's steps
(applying / applied / failed / reverted, and the "waiting: …" annotation) need the MCP bearer AND the job header
(`X-Oibot-Job: apply|review`, which the job scripts send) — the owner's token alone never reports a job step, and a
call carrying the job header never makes an officer's decision. GET /api/agents/quiet answers whether a restart now
would cut into anything (the apply job polls it); GET /api/guilds carries each guild's D33 `busy` (api.py calls
`busy_json`). Nothing here fetches a news article: the items are the webhook's own text."""
from __future__ import annotations

import asyncio
import os
import plistlib
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import agents, news
from ..news import AlreadyDecided, ProposalError, ProposalStore
from ..registry import RegistryError

JOB_HEADER = "x-oibot-job"
QUIET_WINDOW_H = 2  # GET /api/agents/quiet: a live run within ±2 h of its lock or its start is not quiet (design §3.4 (6))
LOG_FILE = agents.ROOT / "out" / "oibot.log"
LOG_TAIL_BYTES = 2_000_000
INSTALL_LINE = "sh scripts/agents/install.sh"


# ---------------------------------------------------------------- pure helpers (tests call them directly)

def busy_json(bot, reg, now: datetime | None = None) -> dict:
    """news.guild_busy for one guild, from the bot's runs and live loot councils (NewsMixin.guild_busy when the client
    has it; a bare client: its RaidStore, no councils)."""
    fn = getattr(bot, "guild_busy", None)
    if fn is not None:
        return fn(reg, now)
    raids = getattr(bot, "raids", None)
    rs = raids.store(reg) if raids is not None else None
    return news.guild_busy(reg, rs.live() if rs is not None else [], (), now)


def apply_mode(label: str | None = None) -> dict:
    """{mode: main | pr | unknown, source}: the apply job's OIBOT_APPLY_MODE from its plist — the installed copy in
    ~/Library/LaunchAgents when there is one, else the repo's (scripts/launchd)."""
    label = label or agents.JOBS["apply"]["label"]
    for path in (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist", agents.ROOT / "scripts" / "launchd" / f"{label}.plist"):
        try:
            env = (plistlib.loads(path.read_bytes()).get("EnvironmentVariables") or {})
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        mode = str(env.get("OIBOT_APPLY_MODE") or "main").strip().lower()
        return {"mode": mode if mode in ("main", "pr") else "unknown", "source": "installed" if "LaunchAgents" in str(path) else "repo"}
    return {"mode": "unknown", "source": None}


def mcp_last_used(log: Path | None = None) -> str | None:
    """When the MCP bearer last wrote through the API (the request log's newest `via=mcp` line; GETs are not logged),
    as the log's own local time string — None when never seen or no log."""
    path = log or LOG_FILE
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if "via=mcp" in line:
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            return m.group(1) if m else None
    return None


def mcp_card(reg, tz=None, log: Path | None = None) -> dict:
    """The Agents page's MCP card: configured (a token in the bot's environment), local-only, when the token last wrote."""
    from .app import MCP_TOKEN_ENV, mcp_local_only

    seen = mcp_last_used(log)
    label = ""
    if seen:
        try:
            label = reg.local12(datetime.strptime(seen, "%Y-%m-%d %H:%M:%S").astimezone())
        except ValueError:
            label = ""
    # the log's 24-hour stamp stays here: a person reads last_used_label (12-hour, the guild's clock) and nothing else
    return {"configured": bool(os.environ.get(MCP_TOKEN_ENV)), "local_only": mcp_local_only(), "last_used_label": label,
            "config_file": (agents.ROOT / ".mcp.json").exists()}


def llm_usage(bot, viewer_reg) -> dict | None:
    """D45: one shared monthly cap, spend shown per guild. None without a provider."""
    provider = getattr(getattr(bot, "ctx", None), "provider", None)
    if provider is None or not hasattr(provider, "cost_usd"):
        return None
    by = provider.usage_by_guild() if hasattr(provider, "usage_by_guild") else {}
    names = {r.key: str(r.config.name) for r in bot.registries.ordered()}
    rows = [{"key": k, "name": names.get(k, k), "spent": round(v, 2)} for k, v in sorted(by.items(), key=lambda kv: -kv[1]) if k]
    return {"spent": round(provider.cost_usd, 2), "cap": round(float(getattr(provider, "budget_usd", 0.0)), 2), "by_guild": rows,
            "unattributed": round(by.get("", 0.0), 2), "shared": True}


def quiet_state(bot, now: datetime | None = None, *, views: int = 0, previews: int = 0) -> dict:
    """{quiet, why[]}: a restart now would cut into nothing (design §3.4 (6)) — no live run within ±QUIET_WINDOW_H of its
    lock or start (test runs aside), no live loot council, no solver / board / plan lock held, no live wizard or confirm view (`views`,
    pending_views.live_count) and no unexpired preview token (`previews`)."""
    from .. import raidcycle as rc

    now = now or datetime.now(timezone.utc)
    win = timedelta(hours=QUIET_WINDOW_H)
    why: list[str] = []
    for reg in bot.registries.ordered():
        raids = getattr(bot, "raids", None)
        rs = raids.store(reg) if raids is not None else None
        for ev in (rs.live() if rs is not None else []):
            if getattr(ev, "test", False):
                continue
            lock = ev.start - timedelta(hours=float(rc.team_setting(rc.run_team(reg, ev), "cutoff_hard_hours") or 0))
            if abs(now - lock) <= win or abs(now - ev.start) <= win:
                name = reg.raid_def(ev.instance).get("name", ev.instance) if ev.instance else ev.team
                why.append(f"{reg.config.name}: {name} {reg.local12(ev.start)} locks or starts within {QUIET_WINDOW_H} h")
        sessions = getattr(bot, "loot_sessions", None)
        for s in (sessions(reg) if sessions is not None else []):  # a live council, however late it runs (D33)
            if getattr(s, "test", False) or not getattr(s, "run_key", None):
                continue
            name = reg.raid_def(s.instance).get("name", s.instance) if getattr(s, "instance", None) else s.run_key
            why.append(f"{reg.config.name}: {name}: the loot council is open")
    for attr in ("_solver_locks", "_board_locks", "_plan_locks"):
        held = [k for k, lk in (getattr(bot, "__dict__", {}).get(attr) or {}).items() if lk.locked()]
        if held:
            why.append(f"{len(held)} {attr.strip('_').replace('_locks', '')} lock(s) held")
    if views:
        why.append(f"{views} wizard or confirm screen(s) open in Discord")
    if previews:
        why.append(f"{previews} preview(s) waiting for Apply on the site")
    return {"quiet": not why, "why": why}


def install_agents_api(app: FastAPI, bot, *, who, body, publish) -> None:
    def store():
        return bot.registries.store

    def is_bearer(v) -> bool:
        return getattr(v, "via", "web") == "mcp"

    def job_of(request: Request, v) -> str | None:
        """The local job a call comes from: the MCP bearer AND `X-Oibot-Job: review|apply` (the job scripts send it). The
        owner's token alone is no job (it gets officer moves, as the owner), and a header without the bearer is nothing."""
        if not is_bearer(v):
            return None
        j = (request.headers.get(JOB_HEADER) or "").strip().lower()
        return j if j in agents.JOBS else None

    def saved(guild_key: str, _pid: str) -> None:
        publish("agents", guild_key)  # this guild's pages only (the jobs' own status pushes reach every guild)

    if saved not in news.PROPOSAL_LISTENERS:
        news.PROPOSAL_LISTENERS[:] = [fn for fn in news.PROPOSAL_LISTENERS if getattr(fn, "__name__", "") != "saved"] + [saved]

    async def decide(reg, pid: str, state: str | None, **kw):
        """NewsMixin.decide_proposal on the bot (a bare test client borrows the mixin's function)."""
        from ..discord_news import NewsMixin

        fn = getattr(bot, "decide_proposal", None)
        if fn is None:
            return await NewsMixin.decide_proposal(bot, reg, pid, state, **kw)
        return await fn(reg, pid, state, **kw)

    async def card(reg, p) -> bool:
        """Post or edit the proposal's Discord card (the bot's NewsMixin); tests run without one."""
        fn = getattr(bot, "proposal_card_update", None)
        if fn is None:
            return False
        r = fn(reg, p)
        r = await r if asyncio.iscoroutine(r) else r
        return r is not False

    def agents_guild(v) -> str | None:
        """Whose runs a caller sees: the MCP bearer sees every run (the owner's jobs); a web viewer, their selected
        guild's (every run while one guild is configured — agents.runs_scope)."""
        return None if is_bearer(v) else agents.runs_scope(bot.registries, v.reg)

    def news_json(reg, r: dict) -> dict:
        return {**r, "posted_label": reg.local12(r["posted_at"]) if r.get("posted_at") else "", "ingested_label": reg.local12(r["ingested_at"]) if r.get("ingested_at") else ""}

    def consequence_of(reg, p) -> dict:
        """The decide screen's words for each verdict the proposal can still get (the site never composes them)."""
        fn = getattr(bot, "proposal_consequence", None)
        out = {}
        for state in ("approved", "dismissed"):
            if (p.state, state) not in news.OFFICER_MOVES or (state == "approved" and p.kind == "needs_developer"):
                continue
            if fn is not None:
                out[state] = fn(reg, p, state)
            else:
                out[state] = news.consequence(p, reg, state, guilds=bot.registries, busy={r.key: busy_json(bot, r) for r in bot.registries.ordered()})
        if p.state == news.RECOVER_MOVE[0] and not news.restart_waiting(agents.read_status("apply", agents.OUT), reg.key, p.id):
            out["interrupted"] = news.consequence(p, reg, "interrupted")  # the owner's Mark as interrupted (offered while the apply job isn't running)
        return out

    def proposal_json(reg, p) -> dict:
        d = p.model_dump()
        d["created_label"] = reg.local12(p.created_at)
        d["decided_label"] = reg.local12(p.decided_at) if p.decided_at else ""
        d["history"] = [{**h, "at_label": reg.local12(h["at"]) if h.get("at") else "", "annotation": h.get("state") is None} for h in p.history]
        d["consequence"] = consequence_of(reg, p)
        d["owner_only"] = news.needs_owner(p)
        d["shared_with"] = news.shared_profile_others(bot.registries, reg) if p.kind == "profile" else []
        return d

    def refusal(v, e: Exception) -> HTTPException:
        if isinstance(e, AlreadyDecided):
            return HTTPException(409, news.already_text(e.p, v.reg.local12))
        text = str(e)
        if isinstance(e, PermissionError):
            return HTTPException(403, text[:1].upper() + text[1:] + ("" if text.endswith(".") else "."))
        return HTTPException(400, text)

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
            publish("agents", v.reg.key)
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
        job = job_of(request, v) or ("review" if is_bearer(v) else None)  # the review session's MCP server: the bearer, by the review
        dup = news.duplicate_profile_proposal(bot.registries, v.reg, d)
        if dup:  # a profile edit is shared by every guild on the game version: one proposal is enough (design §5.26)
            if is_bearer(v):  # the owner's review job may look it up; a person gets the other guild's name, never its proposal id
                return {"message": f"already proposed by another guild on this game version ({dup}) — nothing to do", "duplicate_of": dup}
            other = bot.registries.by_key.get(dup.split("/", 1)[0])
            return {"message": f"already proposed by {other.config.name if other else 'another guild'} on this game version — nothing to do",
                    "duplicate": True}
        if job != "review" and not v.owner and str(d.get("kind") or "") == "guild_setting" \
                and news.owner_setting(news.Proposal.model_construct(kind="guild_setting", edit=d.get("edit") or {})):
            raise HTTPException(403, news.SETTING_OWNER_ONLY)  # no owner write by proposal
        try:
            p = await asyncio.to_thread(ProposalStore(store(), v.reg.key).create, v.reg, d, job or v.name)
        except (ProposalError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await card(v.reg, p)
        await bot.ops.emit(v.reg.config, "info", f"news proposal {p.id}: {p.title} ({p.kind}, {p.confidence})")
        return {"message": f"proposal {p.id} posted: {p.title}", "id": p.id, "proposal": proposal_json(v.reg, p)}

    async def move(request: Request, v, pid: str, state: str | None, commit: str | None = None, note: str | None = None):
        job = job_of(request, v)
        if is_bearer(v) and not job and getattr(v, "actor", None) is None and state in news.DECISIONS:
            raise HTTPException(403, news.MCP_ACTOR_NEEDED.format(guild=v.reg.config.name))  # D8: the token is not who decided
        try:
            return await decide(v.reg, pid, state, by=job and "the apply job" or v.name, by_id=None if job else v.uid, officer=bool(v.officer and not job),
                                job=bool(job), owner=bool(v.owner and not job), note=note, commit=commit, via=v.via)
        except (ProposalError, PermissionError) as e:
            raise refusal(v, e)

    @app.post("/api/proposals/{pid}/resolve")
    async def proposal_resolve(request: Request, pid: str):
        """Move a proposal: approved | dismissed (officers; a game-data approval is the owner's), or the apply job's
        applying | applied | failed | reverted with the commit hash and a note — and `state: null` + `note`: the job's
        annotation ("waiting: …"), no move. The job's calls carry X-Oibot-Job. `card_updated` False = the Discord card
        couldn't be edited (the page says it updates when the bot is back)."""
        v, d = await body(request, officer=True)
        raw = d.get("state")
        state = None if raw is None else str(raw)
        if state is None and not job_of(request, v):
            raise HTTPException(403, "Only the apply job annotates a proposal; officers approve or dismiss it.")
        note = str(d["note"]) if d.get("note") is not None else None
        if state == "applying" and job_of(request, v):  # the gate: a game-data approval stands only when the owner made it
            try:
                held = ProposalStore(store(), v.reg.key).get(pid)
            except ProposalError as e:
                raise HTTPException(404, str(e))
            why = news.profile_approval_refusal(getattr(bot, "registries", None), v.reg, held) if held.state == "approved" else None
            if why:
                held, _ = await move(request, v, pid, "failed", note=why)
                return JSONResponse({"error": why, "proposal": proposal_json(v.reg, held)}, status_code=403)
        p, card_ok = await move(request, v, pid, state, (str(d.get("commit") or "").strip() or None), note)
        word = "noted" if state is None else p.state
        return {"message": f"proposal {p.id} is {word}" + ("" if card_ok else " — the Discord card updates when the bot is back"),
                "proposal": proposal_json(v.reg, p), "card_updated": card_ok}

    @app.post("/api/proposals/{pid}/interrupted")
    async def proposal_interrupted(request: Request, pid: str):
        """The owner, on the Agents page: a proposal left `applying` by an apply run that is gone → failed with the
        job's own recovery words (design §3.4 apply job (5); the job does the same at its next poll). Refused while the
        apply job is running (it reports its own proposal) and from MCP (a person's recovery press, not the token's)."""
        v, _ = await body(request, officer=True)
        if is_bearer(v) or not v.owner:
            raise HTTPException(403, "Only the owner marks an apply run as interrupted, on the Agents page.")
        st = agents.read_status("apply", agents.OUT)
        if st.get("state") == "running":
            raise HTTPException(409, "The apply job is running — it reports its own proposal; wait for it to finish.")
        waiting = news.restart_waiting(st, v.reg.key, pid)
        if waiting:  # committed: only the restart is owed — failing it would leave the commit to go live unannounced
            raise HTTPException(409, waiting)
        try:
            p, card_ok = await decide(v.reg, pid, news.RECOVER_MOVE[1], by=v.name, by_id=v.uid, officer=True, owner=True, recover=True,
                                      note=news.INTERRUPTED, via=v.via)
        except (ProposalError, PermissionError) as e:
            raise refusal(v, e)
        return {"message": f"proposal {p.id} is failed: {news.INTERRUPTED}" + ("" if card_ok else " — the Discord card updates when the bot is back"),
                "proposal": proposal_json(v.reg, p), "card_updated": card_ok}

    @app.post("/api/proposals/{pid}/apply")
    async def proposal_apply(request: Request, pid: str):
        """The apply job, for a guild setting: the typed config op runs through configops (the plain-text path),
        no restart. approved → applying → applied | failed, the card following each step."""
        from .. import configops

        v, _ = await body(request, officer=True)
        if job_of(request, v) != "apply":
            raise HTTPException(403, "Only the apply job applies proposals; approve it and it runs at the job's next poll.")
        try:
            p = ProposalStore(store(), v.reg.key).get(pid)
        except ProposalError as e:
            raise HTTPException(404, str(e))
        if p.kind != "guild_setting":
            raise HTTPException(400, f"{p.id} is a {p.kind} proposal: only guild settings are applied here")
        if news.owner_setting(p) and not (p.decided_by_id is not None and v.reg.is_owner_id(p.decided_by_id)):
            # re-authorized by privilege at the apply, whoever approved it and however (an approval from before this check)
            why = f"{news.SETTING_OWNER_ONLY} It was approved by {p.decided_by or 'someone else'}, so it wasn't applied."
            if p.state == "approved":
                p, _ = await move(request, v, pid, "failed", note=why)
            return JSONResponse({"error": why, "proposal": proposal_json(v.reg, p)}, status_code=403)
        p, _ = await move(request, v, pid, "applying")
        try:
            op = configops.ConfigOp.model_validate(p.edit)
            line = await configops.apply_async(v.reg, op, f"news review (approved by {p.decided_by or '?'})", True, bot=bot)
        except (RegistryError, ValueError) as e:
            p, _ = await move(request, v, pid, "failed", note=f"not applied: {e}")
            return JSONResponse({"error": str(e), "proposal": proposal_json(v.reg, p)}, status_code=400)
        p, _ = await move(request, v, pid, "applied", note=line)
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
        ov = await asyncio.to_thread(agents.overview, agents.OUT, tz, None, agents_guild(v))  # this guild's steps and results only (K7)
        ps = ProposalStore(store(), v.reg.key)
        runs = [{**r, "started_label": agents.clock12(r["started"], tz)} for r in await asyncio.to_thread(agents.list_runs, None, agents.OUT, guild=agents_guild(v))]
        many = len(bot.registries.ordered()) > 1
        for j in ov["jobs"]:
            st = agents.read_status(j["job"], agents.OUT)
            g = st.get("guild")
            j["elsewhere"] = bool(many and st.get("state") == "running" and g and g != v.reg.key)  # "busy with another server's proposal"
            j["install"] = None if j["launchd"].get("loaded") else f"{INSTALL_LINE} {j['job']}"
        out = {**ov, "runs": runs, "owner": v.owner, "waiting": len(ps.all("proposed")), "approved": len(ps.all("approved")), "guild": v.reg.key,
               "apply_mode": apply_mode(), "busy": busy_json(bot, v.reg), "ops_channel": bool(v.reg.config.ops_channel_id),
               "install_line": INSTALL_LINE}
        pending = agents.read_status("apply", agents.OUT).get("restart_pending") or None
        if v.owner and pending:  # a committed game-data edit the bot hasn't restarted for (the job clears it at the restart)
            out["restart_pending"] = {"id": pending.get("id"), "guild": pending.get("guild"), "commit": pending.get("commit"), "auto": bool(pending.get("auto")),
                                      "title": pending.get("title"), "text": news.restart_pending_text(pending)}
        if v.owner:  # host facts: the owner only (§2.8)
            out["mcp"] = mcp_card(v.reg)
            out["llm"] = llm_usage(bot, v.reg)
        return out

    @app.get("/api/agents/quiet")
    async def agents_quiet(request: Request):
        """{quiet, why[]}: whether restarting the bot now would cut into anything (the apply job asks before a restart)."""
        await who(request, officer=True)
        from .. import pending_views

        now_m = time.monotonic()
        previews = sum(1 for p in (getattr(app.state, "ops_previews", None) or {}).values() if float(p.get("expires", now_m + 1)) > now_m)
        return quiet_state(bot, views=pending_views.live_count(), previews=previews)

    @app.get("/api/agents/run/{run_id}")
    async def agents_run(request: Request, run_id: str):
        v = await who(request, officer=True)
        if not agents.run_visible(run_id, agents_guild(v), agents.OUT):  # another guild's transcript (K7)
            raise HTTPException(404, f"no run {run_id}")
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
        never runs Claude itself, and a job that is already running is left alone. The answer carries the status the
        page polls against (`before`: started / finished as they were) to tell when the kicked run started or ended."""
        v, d = await body(request, officer=True)
        if not v.owner:
            raise HTTPException(403, "Owner only.")
        job = str(d.get("job") or "review")
        if job not in agents.JOBS:
            raise HTTPException(400, f"job is one of {', '.join(agents.JOBS)}")
        st = agents.read_status(job, agents.OUT)
        ok, line = await asyncio.to_thread(agents.start_job, job)  # the same checks MCP run_job and plain text hit
        if not ok:
            return JSONResponse({"error": line}, status_code=409)
        from .. import ops as ops_mod

        await ops_mod.audited(bot.ops, v.reg, v.name, line, via=v.via, by_id=v.uid)
        return {"message": line, "before": {"started": st.get("started"), "finished": st.get("finished"), "state": st.get("state")}}


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
