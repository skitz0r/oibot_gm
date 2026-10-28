"""The loot council on the web (design §3.1, dual interfaces phase 1): the HTTP faces of discord_loot.LootMixin's verbs,
the after-the-fact views (history, precedents, the award log), the data on-ramp (items and tiers) and wishlists.

One verb, every surface a caller (§2.2): every write here calls the SAME LootMixin verb the thread's buttons, plain
text's `loot` op, MCP and the companion call — the verb audits (ops.audited with `via`) and saves (LOOT_LISTENERS →
the `loot:<run>` topic). Routes only decide who may (§2.6) and translate:

- Council writes (open · tick · distribute · override · confirm · discard · reason · correct · end, plus scores-only,
  keep-open, remove-candidate) are officers'. A stale `batch_rev` / `drops_rev` / `expect_to` (LootChanged) or a judge
  already running (LootBusy) is a 409 carrying the councils' CURRENT state, so the page redraws without a second call.
- D8: a write that is a decision about an award or a person (override, confirm, reason, correct, tiers, someone's
  wishlist) from MCP needs the X-Oibot-Actor header (web/app.py validates it as an officer of the guild and makes the
  viewer that officer); the row and the ops line then name "<name> via MCP". Without it MCP reads, previews, ticks,
  distributes, discards and ends.
- D2: members (logged in, `who` = member_of) see their own loot and the guild award log; scores, candidates, the bot's
  pick, reasons and who recorded what are officers' only, filtered HERE by `v.officer` — never client-side. Nothing
  loot-related is on /api/public.
- D12: tiering an item that already has awards asks "re-weight them?" (answer `reweight`: default no, logged).
- Phase 3 (G4): Loot › Items' "Draft with Claude" (a PROPOSAL — nothing written; the officer saves it per item through
  the tiers route) and the CSV export / import of the tier grid (preview first, then apply with the preview's rev);
  attendance (Me › Attendance, an officer's view of a member: Registry.attendance, D49); the companions card (presence,
  and for the owner WHICH feed-token variables are set — never a value); the council's "Bosses 3 / 6".
- Phase 4 (design §5.49): loot appeals — a member's question about their OWN award (POST /api/me/loot/{award}/appeal;
  never for someone else, never from MCP), Loot › Appeals for officers (list + resolve, the D8 actor from MCP); the verbs
  are appeals.py's. No LLM, no DM: the member hears the outcome as a notice."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import appeals
from .. import ops as ops_mod
from ..raid_views import run_title
from ..raidcycle import is_test_run
from ..loot.session import DISENCHANT, LootBusy, LootChanged
from ..loot_ops import loot_log_state  # noqa: F401 — re-exported (web/api.py's Config)
from ..registry import ROLES, TIER_VALUES, WISHLIST_MAX, RegistryError, WishlistChanged, item_key

ACTOR_NEEDED = ("{what} is a decision a person makes: from MCP it needs the X-Oibot-Actor header naming an officer of "
                "{guild} (set OIBOT_MCP_ACTOR to their Discord id) — the owner's token alone can read, preview, tick, "
                "distribute, discard and end.")
LOG_LIMIT = 200
HISTORY_LIMIT = 50
# the last Claude draft per (guild key, item key): a PROPOSAL shown on the grid until an officer saves tiers for that
# item; process memory only (a restart forgets it — draft again), never the data repo
DRAFTS: dict[tuple[str, str], dict] = {}


def install_loot_api(app: FastAPI, bot, *, who, body, publish, find_live, resolve_member, live_officer=None) -> None:
    # ------------------------------------------------------------ helpers
    def ctx_of(reg):
        """The guild's loot context — the bot's cached one (a write here is what the council reads next)."""
        from ..loot_ops import guild_ctx

        return guild_ctx(bot, reg)

    def by_of(v) -> str:
        """Who a write is attributed to: the person's display name; an MCP actor reads '<name> via MCP' (D8)."""
        return f"{v.name} via MCP" if v.via == "mcp" and getattr(v, "actor", None) else v.name

    def need_actor(v, what: str) -> None:
        if v.via == "mcp" and not getattr(v, "actor", None):
            raise HTTPException(403, ACTOR_NEEDED.format(what=what, guild=v.reg.config.name))

    async def writer(request: Request, actor: str | None = None, preview_ok: bool = False):
        """An officer's loot write: officer rights decided now (PRIV_LIVE, as the plain-text Apply), and for a decision
        about an award or a person from MCP, the actor header (D8) — on EVERY such route, whatever the body says. Only
        a route that takes `preview_ok` (its handler answers a preview before any verb runs and writes nothing: confirm,
        the TMB import) lets `"preview": true` through without the actor; anywhere else a preview flag decides nothing."""
        v, d = await body(request, officer=True)
        if live_officer is not None and not await live_officer(v):
            raise HTTPException(403, "Officers only.")
        if actor and not (preview_ok and isinstance(d, dict) and d.get("preview")):
            need_actor(v, actor)
        return v, d

    def clock(reg, iso) -> str:
        return bot.loot_clock(reg, iso) if iso else ""

    def day(reg, value) -> str:
        try:
            return reg.day_label(str(value)[:10], with_year=False) if value else ""
        except Exception:  # noqa: BLE001 — a label is a courtesy
            return str(value or "")

    def run_of(reg, ref: str):
        """(RaidStore, RaidEvent, its sessions) — any run by key (a done one too: reasons and corrections outlive the
        council), else a live run by the names find_live understands."""
        rs = bot.raids.store(reg)
        key = (ref or "").strip()
        m = re.fullmatch(r"(.+)-r\d+", key)
        if key not in rs.events and m and m.group(1) in rs.events:  # a split run's council by its session id ('<run>-r2')
            key = m.group(1)
        ev = rs.events.get(key) or find_live(reg, rs, key)
        sessions = sorted([s for s in bot.events.values() if s.guild == reg.key and s.run_key == ev.key and s.origin == "raid"],
                          key=lambda s: s.roster_index or 0)
        return rs, ev, sessions

    def pick(ev, sessions, d, ref: str | None = None, ended_ok: bool = False):
        """The council a write is for: `session` (its id, its label 'Roster 2', or the roster number) — or the route's
        run named by a council id ('<run>-r2') — when the run has one per roster; the only one otherwise. `ended_ok`: a
        reason / correction may name a council that has ended (its id), which the verb takes as a string."""
        want = str(d.get("session") or "").strip() or (str(ref).strip() if ref and str(ref).strip() != ev.key and str(ref).startswith(ev.key + "-r") else "")
        if want:
            s = next((s for s in sessions if s.id == want or (s.label or "").lower() == want.lower() or str((s.roster_index or 0) + 1) == want), None)
            if s is not None:
                return s
            if ended_ok and (want == ev.key or want.startswith(ev.key + "-r")):
                return want
            raise HTTPException(404, f"No loot council {want} on this run.")
        if len(sessions) == 1:
            return sessions[0]
        if not sessions:
            if ended_ok:
                return ev.key
            raise HTTPException(404, "No loot council is open on this run — open it first.")
        raise HTTPException(400, "This run has one council per roster — say which: session = " + ", ".join(s.id for s in sessions))

    def cand_json(c) -> dict:
        return {"character": c.character, "cls": c.cls, "spec": c.spec, "rank": c.rank, "tier": c.tier, "offspec": c.offspec, "upgrade": c.upgrade_value,
                "attendance": c.attendance_str, "attendance_known": c.attendance_known, "wishlist_rank": c.wishlist_rank, "recent_power": c.recent_power,
                "recent_items": list(c.recent_items), "base": c.base_score, "factors": dict(c.factors), "already_has": c.already_has, "unmapped": c.unmapped}

    def proposal_json(reg, p) -> dict:
        rec = p.result.recommendation
        ov = p.override
        return {"drop_id": p.drop_id, "item": p.item_name, "item_id": p.item_id, "boss": p.boss, "bot": p.award_to, "to": p.to, "source": p.source,
                "override": {"to": ov.to, "reason": ov.reason, "by": ov.by, "at_label": clock(reg, ov.at)} if ov else None,
                "batch": p.batch, "order": p.order, "tiered": p.tiered, "close_call": rec.close_call, "order_sensitive": p.order_sensitive,
                "judged_by": p.result.source, "justification": rec.justification, "alternates": list(rec.alternates), "warnings": list(rec.warnings),
                "candidates": [cand_json(c) for c in p.result.candidates[:12]], "proposed_label": clock(reg, p.proposed_at)}

    def award_json(reg, a) -> dict:
        return {"drop_id": a.drop_id, "item": a.item_name, "item_id": a.item_id, "boss": a.boss, "to": "disenchant" if a.raider == DISENCHANT else a.raider,
                "disenchant": a.raider == DISENCHANT, "source": a.source, "bot": a.bot_pick, "by": a.by, "at_label": clock(reg, a.confirmed_at),
                "tier": a.tier, "weight": a.total_weight, "offspec": a.offspec, "tiered": a.tiered, "void": a.void, "import_id": a.import_id}

    def state_json(reg, rs, ev, s) -> dict:
        """One council as Loot › Tonight reads it (officers): the shared shape (LootMixin.loot_state_json) plus labels in
        words, drops grouped by boss with their state, each pick with its candidate table, the roster (for Override),
        the End sentence and the revs a write carries."""
        base = bot.loot_state_json(s)
        props = {p.drop_id: p for p in s.proposals}
        bosses: dict[str, dict] = {}
        for d in s.drop_list:
            st = s.drop_state(d)
            aw = s.awarded(d.drop_id)
            p = props.get(d.drop_id)
            b = bosses.setdefault(d.boss, {"boss": d.boss, "drops": [], "counts": {"ticked": 0, "proposed": 0, "awarded": 0}})
            b["counts"][st] += 1
            b["drops"].append({"drop_id": d.drop_id, "item": d.item_name, "item_id": d.item_id, "key": d.key, "tiered": d.tiered, "source": d.source, "by": d.by,
                               "at_label": clock(reg, d.at), "held": d.held, "state": st,
                               "to": ("disenchant" if aw.raider == DISENCHANT else aw.raider) if aw else (p.to if p else None)})
        roster = []
        for pl in bot.loot_roster(s):
            try:
                role = reg.profile.spec(pl.cls, pl.spec).role
            except KeyError:
                role = None
            roster.append({"character": pl.character or pl.signup_name, "cls": pl.cls, "spec": pl.spec, "role": role})
        dist = s.distributing
        return {
            "id": s.id, "run": s.run_key, "label": s.label, "roster_index": s.roster_index, "test": s.test, "live": base["live"],
            "title": run_title(reg, ev), "when": reg.local12(ev.start), "run_state": ev.state,
            "thread_id": str(s.raid_thread_id) if s.raid_thread_id else None, "thread_pending": not s.raid_thread_id,
            "bosses": list(bosses.values()), "proposals": [proposal_json(reg, p) for p in s.proposals],
            "awards": [award_json(reg, a) for a in s.awards if not a.void], "pending_reasons": [{**r, "at_label": clock(reg, r.get("at"))} for r in s.pending_reasons],
            "conflicts": [{**r, "at_label": clock(reg, r.get("at"))} for r in s.conflicts], "departed": base["departed"], "roster": roster,
            "pending": s.pending_count(), "untiered": sum(1 for d in s.drop_list if not d.tiered and s.drop_state(d) != "awarded"),
            "distributing": {"by": dist.get("by"), "since_label": clock(reg, dist.get("since")), "drops": len(dist.get("drops") or [])} if dist else None,
            "interrupted": {"since_label": clock(reg, s.interrupted.get("since")), "drops": len(s.interrupted.get("drops") or [])} if s.interrupted else None,
            "scores_only": {"by": s.scores_only.get("by"), "at_label": clock(reg, s.scores_only.get("at"))} if s.scores_only else None,
            "closing": {"notice_label": clock(reg, s.closing_notice_at), "keep_open_label": clock(reg, s.keep_open_until)},
            "last_change": s.last_change(), "opened_by": s.opened_by, "opened_label": clock(reg, s.opened_at),
            "batch_rev": base["batch_rev"], "drops_rev": base["drops_rev"],
            "bosses_progress": boss_progress_of(reg, s),
        }

    def boss_progress_of(reg, s) -> dict:
        """'Bosses 3 / 6' for one council (L12): its kills against the raid's bosses (total None while unlisted)."""
        from ..loot.session import boss_progress, raid_bosses

        return boss_progress(list(s.bosses_done), raid_bosses(reg, s.instance))

    def run_json(reg, rs, ev, sessions) -> dict:
        return {"key": ev.key, "title": run_title(reg, ev), "when": reg.local12(ev.start), "state": ev.state, "instance": ev.instance,
                "test": is_test_run(reg, ev), "rosters": len(ev.all_rosters),
                "councils": [state_json(reg, rs, ev, s) for s in sessions], "end_consequence": bot.end_consequence(reg, rs, ev)}

    def companion_json(reg) -> list[dict]:
        from ..discord_feed import feed_presence

        try:
            return feed_presence(bot, reg)
        except Exception:  # noqa: BLE001 — the chip is a courtesy
            return []

    def changed(reg, e, ref: str | None) -> JSONResponse:
        """A stale write or a judge already running: 409 with what changed and the run's councils as they are now."""
        out = {"error": str(e), "changed": isinstance(e, LootChanged), "busy": isinstance(e, LootBusy)}
        if ref:
            try:
                rs, ev, sessions = run_of(reg, ref)
                out["state"] = run_json(reg, rs, ev, sessions)
            except HTTPException:
                pass
        return JSONResponse(out, status_code=409)

    async def act(v, ref: str, fn):
        """Run a verb call; a refusal is a 400 with its words, a stale rev or a busy judge a 409 with the state."""
        try:
            return await fn()
        except (LootChanged, LootBusy) as e:
            return changed(v.reg, e, ref)
        except HTTPException:
            raise
        except (RegistryError, ValueError) as e:
            from ..configops import user_error

            return JSONResponse({"error": user_error(e)}, status_code=400)

    def reply(v, ref: str, message, **extra) -> dict:
        rs, ev, sessions = run_of(v.reg, ref)
        return {"message": message, "state": run_json(v.reg, rs, ev, sessions), **extra}

    def ids(d, key: str) -> list[str] | None:
        val = d.get(key)
        if val is None:
            return None
        if isinstance(val, str):
            val = [x.strip() for x in val.split(",") if x.strip()]
        if not isinstance(val, list):
            raise HTTPException(400, f"{key} is a list of drop ids")
        return [str(x) for x in val]

    # ------------------------------------------------------------ reads (officers): Tonight
    @app.get("/api/loot")
    async def loot_overview(request: Request):
        """Loot › Tonight: every locked run (with its councils, or none yet: "Open loot council"), the reasons still
        owed and the in-game conflicts (live and ended councils alike), the companions feeding this guild, drops held
        with no run live, and where the public log goes."""
        v = await who(request, officer=True)
        reg = v.reg
        rs = bot.raids.store(reg)
        runs = []
        keys = {s.run_key for s in bot.events.values() if s.guild == reg.key and s.origin == "raid"}
        for ev in sorted(rs.events.values(), key=lambda e: e.starts_at):
            if ev.state == "locked" or ev.key in keys:
                _, _, sessions = run_of(reg, ev.key)
                runs.append(run_json(reg, rs, ev, sessions))
        return {"runs": runs, "reasons_pending": [{**r, "received_label": day(reg, r.get("received"))} for r in bot.loot_reasons_pending(reg)],
                "conflicts": bot.loot_conflicts_pending(reg), "companions": companion_json(reg), "feed_waiting": bot.feed_waiting(reg),
                "log": loot_log_state(reg)}

    @app.get("/api/loot/run/{run}")
    async def loot_run(request: Request, run: str):
        """One run's councils (get_loot)."""
        v = await who(request, officer=True)
        rs, ev, sessions = run_of(v.reg, run)
        return run_json(v.reg, rs, ev, sessions)

    # ------------------------------------------------------------ council writes (officers; D8 for decisions)
    @app.post("/api/loot/{run}/open")
    async def loot_open(request: Request, run: str):
        """Open the council on a locked run (idempotent; one per roster of a split run). The thread goes to the
        officer channel; Discord unreachable → the council opens with its thread pending."""
        v, d = await writer(request)
        rs, ev, _ = run_of(v.reg, run)

        async def go():
            out = await bot.open_council(v.reg, rs, ev, by=by_of(v), by_id=v.uid, via=v.via)
            made = [s for s, created in out if created]
            words = ("Loot council open" if made else "Loot council already open") + (" — thread pending (Discord didn't answer; it is made on the next try)" if any(not s.raid_thread_id for s, _ in out) else "")
            return reply(v, run, words)
        return await act(v, run, go)

    @app.post("/api/loot/{run}/tick")
    async def loot_tick(request: Request, run: str):
        """Tick drops (`add`: [{item_id?, name?, boss?, copies?}] — an item the tables don't know is ticked by name)
        and un-tick (`remove`: drop ids). A merge op; `drops_rev` only when the caller wants a stale refusal."""
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)
        add = d.get("add") or []
        if not isinstance(add, list) or any(not isinstance(a, dict) for a in add):
            raise HTTPException(400, "add is a list of {name, item_id?, boss?, copies?}")

        async def go():
            out = await bot.tick(v.reg, s, add=add, remove=ids(d, "remove") or [], by=by_of(v), by_id=v.uid, via=v.via, drops_rev=d.get("drops_rev") or None)
            return reply(v, run, (out["line"] or "nothing changed").replace("**", ""), drops_rev=out["drops_rev"])
        return await act(v, run, go)

    @app.post("/api/loot/{run}/distribute")
    async def loot_distribute(request: Request, run: str):
        """Judge the ticked drops nobody proposed (`only`: some of them). Single flight: a second press is a 409."""
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)

        async def go():
            out = await bot.distribute(v.reg, s, by=by_of(v), by_id=v.uid, via=v.via, drops_rev=d.get("drops_rev") or None, only=ids(d, "only"))
            return reply(v, run, (out["line"] or "nothing to judge").replace("**", ""), batch_rev=out["batch_rev"], stale=out["stale"])
        return await act(v, run, go)

    @app.post("/api/loot/{run}/override")
    async def loot_override(request: Request, run: str):
        """Award a drop to someone else (or `to` = disenchant), with the required reason: pending until Confirm, the
        precedent written then. Another officer's override needs `replace`; a recorded drop needs Correct."""
        v, d = await writer(request, actor="An override")
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)
        drop = str(d.get("drop_id") or "").strip()
        if not drop:
            raise HTTPException(400, "drop_id required")

        async def go():
            line = await bot.override(v.reg, s, drop, str(d.get("to") or ""), str(d.get("reason") or ""), by=by_of(v), by_id=v.uid, via=v.via,
                                      batch_rev=d.get("batch_rev") or None, replace=bool(d.get("replace")))
            return reply(v, run, line.replace("**", ""))
        return await act(v, run, go)

    @app.post("/api/loot/{run}/confirm")
    async def loot_confirm(request: Request, run: str):
        """Record picks: one drop (`only` + `expect_to` = the recipient you saw) or every pick still pending (`batch_rev`
        of what you saw). `preview`: what would be recorded, nothing written."""
        v, d = await writer(request, actor="Confirming an award", preview_ok=True)  # the preview below returns before the verb
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)
        only = ids(d, "only")
        if d.get("preview"):
            chosen = [p for p in s.proposals if only is None or p.drop_id in only]
            return {"preview": ("Record " + ", ".join(f"{p.item_name} → {'disenchant' if p.to == DISENCHANT else p.to}" + (" (override)" if p.override else "") for p in chosen)
                                + (" — nothing is undone after this; a mistake is fixed with Correct." if chosen else "")) if chosen else "Nothing pending to confirm.",
                    "batch_rev": bot.loot_revs(s)["batch_rev"]}

        async def go():
            out = await bot.confirm(v.reg, s, only=only, expect_to=d.get("expect_to"), batch_rev=d.get("batch_rev") or None, by=by_of(v), by_id=v.uid, via=v.via)
            return reply(v, run, "recorded " + "; ".join(out["lines"]), batch_rev=out["batch_rev"])
        return await act(v, run, go)

    @app.post("/api/loot/{run}/discard")
    async def loot_discard(request: Request, run: str):
        """Drop pending proposals — all (with `batch_rev`) or `only` some ("Decide later": the drop shows held). The
        ticks stay; nothing was recorded, so nothing is undone. `preview`: the sentence, nothing written."""
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)
        only = ids(d, "only")
        if d.get("preview"):
            gone = [p for p in s.proposals if only is None or p.drop_id in only]
            n_ov = sum(1 for p in gone if p.override)
            return {"preview": (f"Discard {len(gone)} proposal(s)? Ticks stay" + (f"; {n_ov} pending override(s) are dropped" if n_ov else "") + " — nothing was recorded.") if gone else "Nothing pending to discard.",
                    "batch_rev": bot.loot_revs(s)["batch_rev"]}

        async def go():
            line = await bot.discard(v.reg, s, only=only, batch_rev=d.get("batch_rev") or None, by=by_of(v), by_id=v.uid, via=v.via)
            return reply(v, run, line)
        return await act(v, run, go)

    @app.post("/api/loot/{run}/reason")
    async def loot_reason(request: Request, run: str):
        """The reason for an award recorded against the bot's pick (the companion's mismatch): becomes the precedent.
        Works after the council ended (`session` = its id). A second reason needs `replace`."""
        v, d = await writer(request, actor="A reason for an award")
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run, ended_ok=True)
        drop = str(d.get("drop_id") or "").strip()
        if not drop:
            raise HTTPException(400, "drop_id required")

        async def go():
            line = await bot.set_reason(v.reg, s, drop, str(d.get("reason") or ""), by=by_of(v), by_id=v.uid, via=v.via, replace=bool(d.get("replace")))
            return reply(v, run, line.replace("**", ""))
        return await act(v, run, go)

    @app.post("/api/loot/{run}/correct")
    async def loot_correct(request: Request, run: str):
        """Fix a recorded award (a mistake, not a decision): the old row is voided with the reason, a new award is
        recorded, no precedent. Works after the council ended (`session` = its id)."""
        v, d = await writer(request, actor="Correcting an award")
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run, ended_ok=True)
        drop = str(d.get("drop_id") or "").strip()
        if not drop:
            raise HTTPException(400, "drop_id required")

        async def go():
            line = await bot.correct(v.reg, s, drop, str(d.get("to") or ""), str(d.get("reason") or ""), by=by_of(v), by_id=v.uid, via=v.via)
            return reply(v, run, line.replace("**", ""))
        return await act(v, run, go)

    @app.post("/api/loot/{run}/end")
    async def loot_end(request: Request, run: str):
        """End the run and its council(s): archived with the summary (what was never confirmed stays inside it), the
        run marked done. `preview`: the End sentence ("End …: 6 awards, 1 override, 2 drops never distributed")."""
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        if ev.state != "locked" and not sessions:  # the verb's rule, shown at the preview too (Discord: "No locked run to end.")
            return JSONResponse({"error": "No locked run to end." if ev.state not in ("done", "cancelled") else f"That run is already {ev.state}."}, status_code=400)
        if d.get("preview"):
            return {"preview": bot.end_consequence(v.reg, rs, ev)}

        async def go():
            text = await bot.end_run(v.reg, rs, ev, by=by_of(v), by_id=v.uid, via=v.via)
            return {"message": text.replace("**", "")}
        return await act(v, run, go)

    @app.post("/api/loot/{run}/scores-only")
    async def loot_scores_only(request: Request, run: str):
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)

        async def go():
            return reply(v, run, await bot.set_scores_only(v.reg, s, bool(d.get("on", True)), by=by_of(v), by_id=v.uid, via=v.via))
        return await act(v, run, go)

    @app.post("/api/loot/{run}/keep-open")
    async def loot_keep_open(request: Request, run: str):
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)

        async def go():
            return reply(v, run, await bot.keep_open(v.reg, s, by=by_of(v), by_id=v.uid, via=v.via))
        return await act(v, run, go)

    @app.post("/api/loot/{run}/candidate/remove")
    async def loot_remove_candidate(request: Request, run: str):
        """Someone who left the roster after open stops being a candidate ("still a candidate? Remove")."""
        v, d = await writer(request)
        rs, ev, sessions = run_of(v.reg, run)
        s = pick(ev, sessions, d, ref=run)

        async def go():
            name = await bot.remove_candidate(v.reg, s, str(d.get("who") or ""), by=by_of(v), by_id=v.uid, via=v.via)
            return reply(v, run, f"{name} is no longer a candidate")
        return await act(v, run, go)

    # ------------------------------------------------------------ after the fact: history, precedents
    def archives(reg) -> list[tuple[Path, dict]]:
        d = reg.store.root / reg.key / "events" / "archive"
        out = []
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                body_ = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if body_.get("origin") == "raid":
                out.append((f, body_))
        return out

    @app.get("/api/loot/history")
    async def loot_history(request: Request, limit: int = HISTORY_LIMIT):
        """Loot › History: every ended council, newest first — its run, date, awards, overrides and the end summary."""
        v = await who(request, officer=True)
        reg = v.reg
        rows = []
        for f, b in archives(reg):
            s = b.get("summary") or {}
            awards = [a for a in b.get("awards") or [] if not a.get("void")]
            rows.append({"id": b.get("id"), "file": f.stem, "run": b.get("run_key"), "label": b.get("label"), "date": b.get("date"), "date_label": day(reg, b.get("date")),
                         "ended_label": reg.local12(s["ended_at"]) if s.get("ended_at") else "", "test": bool(b.get("test")), "awards": len(awards),
                         "overrides": s.get("overrides", 0), "agreed": s.get("agreed", 0), "never_confirmed": len(s.get("never_confirmed") or []),
                         "undistributed": len(s.get("undistributed") or []), "reasons_pending": len(s.get("reasons_pending") or []),
                         "text": str(s.get("text") or "").replace("**", "")})
        rows.sort(key=lambda r: (r["date"] or "", r["file"]), reverse=True)
        return {"rows": rows[:max(1, min(int(limit), 200))]}

    @app.get("/api/loot/history/{file}")
    async def loot_history_one(request: Request, file: str):
        """One ended council in full: drops, awards (with who recorded them), overrides and the summary."""
        v = await who(request, officer=True)
        hit = next((b for f, b in archives(v.reg) if f.stem == file or b.get("id") == file), None)
        if hit is None:
            raise HTTPException(404, "no such council")
        reg = v.reg
        return {"id": hit.get("id"), "run": hit.get("run_key"), "label": hit.get("label"), "date_label": day(reg, hit.get("date")), "test": bool(hit.get("test")),
                "drops": [{"drop_id": x.get("drop_id"), "item": x.get("item_name"), "boss": x.get("boss"), "tiered": x.get("tiered", True)} for x in hit.get("drop_list") or []],
                "awards": [{"drop_id": a.get("drop_id"), "item": a.get("item_name"), "to": "disenchant" if a.get("raider") == DISENCHANT else a.get("raider"),
                            "source": a.get("source"), "bot": a.get("bot_pick"), "by": a.get("by"), "at_label": clock(reg, a.get("confirmed_at")), "void": bool(a.get("void")),
                            "tier": a.get("tier"), "offspec": a.get("offspec")} for a in hit.get("awards") or []],
                "overrides": [{**o, "at_label": clock(reg, o.get("at"))} for o in hit.get("overrides") or []],
                "summary": str((hit.get("summary") or {}).get("text") or "").replace("**", "")}

    @app.get("/api/loot/precedents")
    async def loot_precedents(request: Request, status: str | None = None, member: str | None = None, limit: int = 200):
        """Loot › Precedents: council overrides with their reasons, newest first (`status` active | retired | superseded;
        `member` narrows to one member's characters), plus the "needs a reason" list (awards recorded against the
        bot's pick with no reason yet)."""
        v = await who(request, officer=True)
        reg = v.reg
        ctx = ctx_of(reg)
        rows = list(reversed(ctx.precedents))
        if status:
            rows = [p for p in rows if p.get("status", "active") == status]
        if member:
            m = resolve_member(reg, member)
            names = {m.display_name.lower(), *(c.label.lower() for c in m.characters), *((c.name or "").lower() for c in m.characters if c.name)}
            rows = [p for p in rows if str(p.get("human") or "").lower() in names or str(p.get("bot") or "").lower() in names]
        out = [{**p, "date_label": day(reg, p.get("date")), "at_label": clock(reg, p.get("at")), "status": p.get("status", "active"),
                "status_at_label": clock(reg, p.get("status_at"))} for p in rows[:max(1, min(int(limit), 500))]]
        return {"rows": out, "needs_reason": [{**r, "received_label": day(reg, r.get("received"))} for r in bot.loot_reasons_pending(reg)],
                "member": m.display_name if member else None}

    @app.post("/api/loot/precedents/retire")
    async def loot_retire(request: Request):
        """Retire a precedent (the bot stops citing it) or `restore` it; the reason is required and kept on it."""
        v, d = await writer(request, actor="Retiring a precedent")
        ref = str(d.get("ref") or d.get("import_id") or "").strip()
        if not ref:
            raise HTTPException(400, "ref required (the precedent's ref from /api/loot/precedents)")
        try:  # the one verb: the guild's precedents lock, the status row, the commit off the loop, the audit line
            line = await bot.retire_precedent(v.reg, ref, str(d.get("reason") or ""), by=by_of(v), by_id=v.uid, via=v.via, restore=bool(d.get("restore")))
        except RegistryError as e:
            from ..configops import user_error

            return JSONResponse({"error": user_error(e)}, status_code=400)
        publish("loot:precedents", v.reg.key)
        return {"message": line}

    # ------------------------------------------------------------ the award log (members, logged in — D2)
    def char_index(reg) -> dict[str, tuple]:
        out = {}
        for m in reg.members.values():
            for c in m.characters:
                out.setdefault(c.label.lower(), (m.discord_id, c.cls, c.spec))
        return out

    def log_row(reg, a, chars, officer: bool, scores: bool, ctx, viewer_uid: int | None, mine_names: set[str]) -> dict:
        it = reg.item(a.item_id or None, a.item_name)
        name = a.item_name or (it.name if it is not None else f"item {a.item_id}")
        uid, cls, spec = chars.get(a.raider.lower(), (a.discord_id, None, None))
        row = {"item": name, "item_id": a.item_id, "key": item_key(a.item_id, name), "character": "disenchant" if a.raider == DISENCHANT else a.raider,
               "disenchant": a.raider == DISENCHANT, "cls": cls, "spec": spec, "boss": a.boss, "instance": a.instance, "received": a.received.isoformat(),
               "date_label": day(reg, a.received.isoformat()), "offspec": a.offspec,
               "mine": bool(viewer_uid is not None and (a.discord_id == viewer_uid or (a.discord_id is None and a.raider.lower() in mine_names)))}
        if officer and scores:
            prec = ctx.precedent_for(a.import_id) if a.import_id else None
            row.update({"tier": a.tier, "weight": a.total_weight, "source": a.source, "bot": a.bot_pick, "by": a.by, "import_id": a.import_id,
                        "candidates": list(a.candidates_snapshot), "reason": (prec or {}).get("reason")})
        return row

    @app.get("/api/loot/log")
    async def loot_log(request: Request, limit: int = LOG_LIMIT, scores: bool = False, member: str | None = None):
        """The guild award log for anyone logged in to this guild: item, character (spec), date, boss, MS/OS — newest
        first. Officers asking `scores` also get tier, weight, source, the bot's pick, who recorded it, the candidates
        at the time and the precedent's reason; members never do (filtered here, not in the browser)."""
        v = await who(request)
        reg = v.reg
        ctx = ctx_of(reg)
        chars = char_index(reg)
        mine = {c.label.lower() for c in (v.member.characters if v.member else [])}
        rows = sorted(ctx.ledger, key=lambda a: (a.received, a.confirmed_at or ""), reverse=True)
        if member:
            m = resolve_member(reg, member)
            names = {c.label.lower() for c in m.characters}
            rows = [a for a in rows if a.discord_id == m.discord_id or a.raider.lower() in names]
        out = [log_row(reg, a, chars, v.officer, scores, ctx, v.uid, mine) for a in rows[:max(1, min(int(limit), 1000))]]
        return {"rows": out, "scores": bool(v.officer and scores), "officer": v.officer}

    @app.get("/api/me/loot")
    async def me_loot(request: Request, limit: int = 50, offset: int = 0, q: str = ""):
        """My loot: what my characters received (by my Discord id; by character name for rows recorded without one),
        newest first, a page at a time — `offset` / `limit` (≤200) over ALL of them, `q` narrows to the awards whose item,
        character or boss holds that text — so any award can be asked about (Discord's select holds the newest 25).
        {rows, total (matching), more, appeals, appeal_text}."""
        v = await who(request)
        reg = v.reg
        ctx = ctx_of(reg)
        mine = {c.label.lower() for c in (v.member.characters if v.member else [])}
        chars = char_index(reg)
        rows = [a for a in ctx.ledger if a.discord_id == v.uid or (a.discord_id is None and a.raider.lower() in mine)]
        rows.sort(key=lambda a: (a.received, a.confirmed_at or ""), reverse=True)
        needle = " ".join(str(q or "").split()).lower()[:60]
        if needle:  # the member's own awards only — a search never widens what they see
            rows = [a for a in rows if needle in " ".join(str(x or "") for x in (a.item_name, a.item_id, a.raider, a.boss)).lower()]
        asked = {ap.id: ap for ap in appeals.store_of(reg).of_member(v.uid)} if v.member else {}
        out = []
        ids = appeals.ledger_ids(ctx.ledger)  # the appeal id of each award (two identical imported rows apart)
        start, size = max(0, int(offset)), max(1, min(int(limit), 200))
        for a in rows[start:start + size]:
            aid = ids[id(a)]
            ap = asked.get(aid)
            out.append({**log_row(reg, a, chars, False, False, ctx, v.uid, mine), "award": aid, "appeal": member_appeal_json(reg, ap) if ap else None})
        # my questions to the council (L32), newest first — also those about an award corrected since (no longer a row above)
        mine_q = sorted(asked.values(), key=lambda ap: ap.created_at, reverse=True)
        return {"rows": out, "total": len(rows), "more": start + size < len(rows), "appeals": [member_appeal_json(reg, ap) for ap in mine_q],
                "appeal_text": {"min": appeals.TEXT_MIN, "max": appeals.TEXT_MAX}}

    # ------------------------------------------------------------ loot appeals (L32, design §5.49 — no LLM, no DM)
    def member_appeal_json(reg, ap) -> dict:
        """A member's own question as THEY see it: the award, their words, the state and the outcome line — never the
        officer's note or who resolved it (D2: who recorded what is officer data)."""
        return {"id": ap.id, "item": ap.item, "character": ap.character, "award": appeals.award_words(reg, ap), "text": ap.text, "state": ap.state,
                "outcome": ap.outcome, "outcome_label": appeals.OUTCOME_LABELS.get(ap.outcome or "") if ap.outcome else None,
                "state_text": appeals.state_words(reg, ap)}

    def officer_appeal_json(reg, ap, ctx, chars=None, awards=None) -> dict:
        chars = chars if chars is not None else char_index(reg)
        _uid, cls, spec = chars.get(ap.character.lower(), (None, None, None))
        gid = reg.config.discord_guild_id
        link = f"https://discord.com/channels/{gid}/{ap.thread_id or ap.channel_id}" + ("" if ap.thread_id else f"/{ap.message_id}") if (ap.thread_id or ap.message_id) else None
        out = {**member_appeal_json(reg, ap), "member": ap.member_name, "member_id": str(ap.member_id), "cls": cls, "spec": spec, "item_id": ap.item_id,
               "boss": ap.boss, "date_label": day(reg, ap.received), "offspec": ap.offspec, "asked_label": reg.local12(ap.created_at), "via": ap.via,
               "note": ap.note, "resolved_by": ap.resolved_by, "resolved_label": reg.local12(ap.resolved_at) if ap.resolved_at else None, "link": link,
               "details": appeals.officer_details(reg, ctx, ap, awards)}
        if ap.state == "open":  # THE consequence sentence per outcome (the browser never composes it)
            out["consequences"] = {k: appeals.resolve_consequence(reg, ap, k) for k in appeals.OUTCOMES}
        return out

    @app.post("/api/me/loot/{award}/appeal")
    async def me_loot_appeal(request: Request, award: str):
        """Me › My loot › Ask the council: {text, preview?} about one of MY awards. `preview` answers the consequence
        sentence and writes nothing. Only the member themself (never an officer for them, never MCP); one per award."""
        v, d = await body(request)
        if v.via == "mcp":
            raise HTTPException(403, appeals.FROM_MCP)
        reg = v.reg
        try:
            a = appeals.find_award(reg, ctx_of(reg), v.uid, award)
            if d.get("preview"):
                return {"preview": True, "consequence": appeals.open_consequence(reg, a), "award": appeals.award_words(reg, character=a.raider, item=a.item_name or a.item_id,
                                                                                                                     received=a.received.isoformat(), boss=a.boss, offspec=a.offspec)}
            ap, receipt = await appeals.open_appeal(bot, reg, v.uid, award, d.get("text"), by=v.name, by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": receipt, "appeal": member_appeal_json(reg, ap)}

    @app.get("/api/loot/appeals")
    async def loot_appeals(request: Request, limit: int = 50):
        """Loot › Appeals (officers): open questions oldest first, then the last `limit` resolved, each with the award,
        the member's words, the council's record of the award (officer details) and the card / thread link."""
        v = await who(request, officer=True)
        reg = v.reg
        ctx = ctx_of(reg)

        def build() -> dict:  # file reads (the appeals, the ledger once): off the event loop
            rows = appeals.store_of(reg).all()
            open_ = sorted([ap for ap in rows if ap.state == "open"], key=lambda ap: ap.created_at)
            done = sorted([ap for ap in rows if ap.state != "open"], key=lambda ap: ap.resolved_at or ap.created_at, reverse=True)[:max(1, min(int(limit), 500))]
            chars, awards = char_index(reg), appeals.all_awards(reg)
            return {"open": [officer_appeal_json(reg, ap, ctx, chars, awards) for ap in open_], "resolved": [officer_appeal_json(reg, ap, ctx, chars, awards) for ap in done],
                    "outcomes": [{"value": k, "label": appeals.OUTCOME_LABELS[k], "text": t} for k, t in appeals.OUTCOMES.items()], "note_max": appeals.NOTE_MAX}

        return await asyncio.to_thread(build)

    @app.post("/api/loot/appeals/{aid}/resolve")
    async def loot_appeal_resolve(request: Request, aid: str):
        """An officer closes a question: {outcome, note?}. `preview` answers the consequence sentence. From MCP it is a
        decision about a person (D8): the X-Oibot-Actor header names the officer."""
        v, d = await writer(request, actor="Resolving a loot question", preview_ok=True)
        reg = v.reg
        try:
            if d.get("preview"):
                ap = appeals.store_of(reg).get(aid)
                if ap is None:
                    raise appeals.AppealError(f"No loot question {aid}.")
                return {"preview": True, "consequence": appeals.resolve_consequence(reg, ap, appeals.check_outcome(d.get("outcome")))}
            ap, line = await appeals.resolve_appeal(bot, reg, aid, d.get("outcome"), note=d.get("note") or None, by=by_of(v), by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line, "appeal": officer_appeal_json(reg, ap, ctx_of(reg))}

    def appeal_saved(guild_key: str, _aid: str) -> None:
        publish("loot:appeals", guild_key)  # Loot › Appeals and Me › My loot (useLive "loot:") reload on every save

    appeals.APPEAL_LISTENERS[:] = [f for f in appeals.APPEAL_LISTENERS if getattr(f, "__name__", "") != appeal_saved.__name__] + [appeal_saved]

    # ------------------------------------------------------------ the companions card (officers; token names: owner)
    @app.get("/api/loot/companions")
    async def loot_companions(request: Request):
        """Loot › Companions: this guild's companions (live · quiet · disconnected, last seen in words, events) for
        officers; for the owner also which feed-token variables are set (OIBOT_FEED_TOKEN, OIBOT_FEED_TOKEN_<KEY>) —
        names and yes/no only, never a value (secrets stay in .env, §2.8)."""
        from ..feed import listen_state, token_state

        v = await who(request, officer=True)
        many = bool(getattr(bot.registries, "many", False))
        ls = listen_state(getattr(bot, "feed", None))
        return {"companions": companion_json(v.reg), "listening": ls["listening"], "local_only": ls["local_only"], "listen_words": ls["words"], "many": many,
                "tokens": token_state(v.reg.key, many=many) if v.owner else None, "owner": v.owner}

    # ------------------------------------------------------------ attendance (design §3.5 L35/M43, D49)
    OUTCOME_WORDS = {"rostered": "rostered", "unconfirmed": "rostered, never confirmed", "benched": "benched", "away": "away",
                     "called_out": "can't make it", "no_thanks": "no thanks", "no_answer": "no answer"}

    def attendance_json(reg, m, officer: bool) -> dict:
        """One member's attendance: the sentence, the view's count (bench counts), one row per run of the window; the
        loot factor's count (rostered + confirmed, unknown → None) for officers only (it is a scoring input)."""
        days = reg.attendance_window()
        att, total = reg.attendance(m, days, bench=True)
        rows = [{**{k: r[k] for k in ("key", "when", "raid", "kind", "outcome", "counted", "character", "cls", "spec")},
                 "outcome_label": OUTCOME_WORDS.get(r["outcome"], r["outcome"])} for r in reg.attendance_runs(m, days)]
        out = {"sentence": reg.attendance_text(m, days), "window_days": days, "attended": att, "total": total, "runs": rows,
               "tracked": reg.attendance_tracked(days)["runs"]}
        if officer:
            known = reg.attendance_known(m, days)
            out["loot"] = {"attended": known[0], "total": known[1]} if known else None
        return out

    @app.get("/api/me/attendance")
    async def me_attendance(request: Request):
        """Me › Attendance: my runs in the loot policy's window (every character of mine counts) and the sentence."""
        v = await who(request)
        if v.member is None:
            return {"sentence": "nothing registered yet", "window_days": v.reg.attendance_window(), "attended": 0, "total": 0, "runs": [], "tracked": 0}
        return await asyncio.to_thread(attendance_json, v.reg, v.member, False)  # parses run files: off the loop

    @app.get("/api/members/attendance")
    async def member_attendance(request: Request, member: str):
        """An officer's view of one member's attendance (MCP get_member adds it): the Me card's data plus the loot
        factor's count."""
        v = await who(request, officer=True)
        m = resolve_member(v.reg, member)
        return {"member": m.display_name, **(await asyncio.to_thread(attendance_json, v.reg, m, True))}

    # ------------------------------------------------------------ wishlists (D5)
    def received_map(reg, ctx, m) -> dict[tuple[str, str], str]:
        """(character label lower, item key) → the day it was received (loot_ops.received_map: the /me wishlist wizard
        greys the same entries)."""
        from ..loot_ops import received_map as rm

        return rm(reg, ctx, m)

    def wishlist_json(reg, m, ctx=None) -> list[dict]:
        ctx = ctx or ctx_of(reg)
        got = received_map(reg, ctx, m)
        out = []
        for c in [c for c in (m.characters if m else []) if c.status in ("active", "planned")]:
            entries = []
            for e in c.wishlist:
                it = reg.item(e.item_id or None, e.name)
                rec = got.get((c.label.lower(), e.key)) or got.get((c.label.lower(), "n:" + e.name.lower()))
                entries.append({"item_id": e.item_id, "name": e.name, "key": e.key, "rank": e.rank, "offspec": e.offspec, "note": e.note, "source": e.source,
                                "known": it is not None, "tiered": bool(it is not None and it.tiers), "boss": it.boss if it is not None else None,
                                "received": rec, "received_label": day(reg, rec) if rec else None})
            out.append({"label": c.label, "cls": c.cls, "spec": c.spec, "is_main": c.is_main, "rev": reg.wishlist_rev(m, c.label), "entries": entries})
        return out

    @app.get("/api/me/wishlist")
    async def me_wishlist(request: Request):
        """Me › Wishlist: each of my characters' ranked list with its rev (Save carries it) and received marks."""
        v = await who(request)
        return {"characters": wishlist_json(v.reg, v.member) if v.member else [], "max": WISHLIST_MAX}

    async def save_wishlist(v, uid: int, d: dict) -> JSONResponse | dict:
        rows = d.get("rows")
        if not isinstance(rows, list):
            raise HTTPException(400, "rows is the whole list: [{item_id?, name, rank?, offspec?, note?}]")
        try:
            line = await asyncio.to_thread(v.reg.set_wishlist, uid, d.get("character") or None, rows, by_of(v), rev=d.get("rev") or None)
        except WishlistChanged as e:
            m = v.reg.members.get(uid)
            return JSONResponse({"error": str(e), "changed": True, "characters": wishlist_json(v.reg, m) if m else []}, status_code=409)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, v.reg, by_of(v), line, via=v.via, by_id=v.uid)
        m = v.reg.members.get(uid)
        return {"message": line, "characters": wishlist_json(v.reg, m) if m else []}

    @app.post("/api/me/wishlist")
    async def me_wishlist_save(request: Request):
        """One Save of one of my characters' wishlists (edit mode): {character, rows, rev}. A list changed since you
        opened it (another tab, the Discord wizard) → 409 with the current lists."""
        v, d = await body(request)
        if v.member is None:
            return JSONResponse({"error": "Register a character first — the wishlist belongs to a character."}, status_code=400)
        return await save_wishlist(v, v.uid, d)

    @app.post("/api/members/wishlist")
    async def member_wishlist_save(request: Request):
        """An officer sets someone's wishlist (MCP set_wishlist): {member, character, rows, rev?}."""
        v, d = await writer(request, actor="Setting someone's wishlist")
        m = resolve_member(v.reg, d.get("member") if d.get("member") not in (None, "") else d.get("uid"))
        return await save_wishlist(v, m.discord_id, d)

    @app.get("/api/members/wishlist")
    async def member_wishlist(request: Request, member: str):
        v = await who(request, officer=True)
        m = resolve_member(v.reg, member)
        return {"member": m.display_name, "characters": wishlist_json(v.reg, m), "max": WISHLIST_MAX}

    @app.get("/api/loot/wishlists")
    async def loot_wishlists(request: Request):
        """Loot › Wishlists (officers): every member's characters with a wishlist, and who wants each item most."""
        v = await who(request, officer=True)
        reg = v.reg
        ctx = ctx_of(reg)
        people, wanted = [], {}
        for m in sorted(reg.members.values(), key=lambda m: m.display_name.lower()):
            if not any(c.wishlist for c in m.characters):  # a puppet with a list (a rehearsal) shows too, marked test
                continue
            chars = [c for c in wishlist_json(reg, m, ctx) if c["entries"]]
            people.append({"uid": str(m.discord_id), "display_name": m.display_name, "test": bool(m.test), "characters": chars})
            for c in chars:
                for e in c["entries"]:
                    if not e["received"]:
                        w = wanted.setdefault(e["key"], {"key": e["key"], "name": e["name"], "item_id": e["item_id"], "boss": e["boss"], "wants": []})
                        w["wants"].append({"character": c["label"], "cls": c["cls"], "spec": c["spec"], "rank": e["rank"], "offspec": e["offspec"]})
        items = sorted(wanted.values(), key=lambda w: (-len(w["wants"]), w["name"].lower()))
        for w in items:
            w["wants"].sort(key=lambda x: (x["offspec"], x["rank"]))
        return {"people": people, "items": items, "max": WISHLIST_MAX}

    @app.post("/api/loot/wishlists/import")
    async def loot_wishlists_import(request: Request):
        """A ThatsMyBIS wishlist export (CSV text) into the native wishlists (D5): `preview` first (rows, characters,
        duplicates, unknown characters), then without it to write. An import never deletes an entry made on the site."""
        v, d = await writer(request, actor="Importing wishlists", preview_ok=True)  # import_tmb_wishlists(preview=True) writes nothing
        text = str(d.get("csv") or "")
        if not text.strip():
            raise HTTPException(400, "csv required (the TMB wishlist export's text)")
        report = await asyncio.to_thread(v.reg.import_tmb_wishlists, text, by_of(v), preview=bool(d.get("preview")))
        line = (f"TMB wishlists {'preview' if d.get('preview') else 'imported'}: {report['added']} item(s) for {report['characters']} character(s), "
                f"{report['duplicates']} already on a list, {report['over_cap']} over the cap of {WISHLIST_MAX}"
                + (f", unknown characters: {', '.join(report['unknown'])}" if report["unknown"] else ""))
        if not d.get("preview"):
            await ops_mod.audited(bot.ops, v.reg, by_of(v), line, via=v.via, by_id=v.uid)
        return {"message": line, "report": report}

    # ------------------------------------------------------------ phase 4 (design §5.48): the data on-ramp
    def live_councils(reg) -> list[str]:
        """The guild's live, non-test loot councils by run name (what Registry.import_awards refuses an apply over)."""
        sessions = getattr(bot, "loot_sessions", None)
        if sessions is None:
            return []
        out = []
        for s in sessions(reg):  # live only (LootMixin.loot_live: the run is locked)
            if s.test:
                continue  # a rehearsal writes no ledger rows
            _rs, ev = bot.loot_run(s)
            out.append(run_title(reg, ev) if ev is not None else str(s.run_key or s.id))
        return out

    @app.post("/api/loot/import")
    async def loot_import(request: Request):
        """Loot › Items › Import (L07; MCP import_ledger): a loot history CSV into the ledger. {csv, kind: auto |
        biscouncil | tmb | generic (character,item,received,boss,ms_os,note), preview (default true), rev}. Preview
        writes nothing and answers the groups — new rows · duplicates · unknown items — with 'N rows, M duplicates, K
        unknown items (recorded by name)' and a `rev`; the apply (`preview: false`) needs that rev (none → 400; the rows
        that would be added changed since → 409 with the fresh preview) and APPENDS exactly the new rows (Registry.
        import_awards: dedupe on (character, item, guild day ±1 when the file gives no zone), one for one, for imports only —
        never an edit, never a delete; refused while a council of the guild is live (the preview says so); an untiered
        or unknown item is learned onto Loot › Items › Untiered so it can be tiered there), then the council's context reloads. Officers; from MCP the apply records awards in someone's name: X-Oibot-Actor (D8)."""
        from ..importers import ledger_csv

        v, d = await writer(request)  # officers; the apply below is the decision that needs the actor from MCP (D8)
        reg = v.reg
        text = d.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, f"csv is the file's text (a BisCouncil or ThatsMyBIS export, or {ledger_csv.GENERIC_HEADER})")
        preview = d.get("preview", True) is not False  # a preview writes nothing
        if not preview:
            need_actor(v, "Importing loot history")
        if not preview and not (isinstance(d.get("rev"), str) and d["rev"]):
            return JSONResponse({"error": "Preview the file first — an import applies the preview it was shown (send its rev)."}, status_code=400)
        try:
            parsed = await asyncio.to_thread(ledger_csv.parse, text, str(d.get("kind") or "auto"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:  # an apply while a council of this guild is live is refused (it writes tonight's rows as it confirms them)
            report = await asyncio.to_thread(reg.import_awards, parsed.rows, by_of(v), v.uid, preview=preview, kind=parsed.kind,
                                             rev=None if preview else d.get("rev"), live_councils=live_councils(reg))
        except (RegistryError, ValueError) as e:
            from ..configops import user_error

            return JSONResponse({"error": user_error(e)}, status_code=400)
        report.update(errors=parsed.errors[:100], skipped=parsed.skipped, kind_words=ledger_csv.KIND_WORDS[parsed.kind])
        if preview:  # a live council: the preview says Confirm waits until it ends
            return {"preview": report, "message": report["line"] + (f" — {report['live_council']}" if report.get("live_council") else "")}
        if report.get("changed"):
            return JSONResponse({"error": "The ledger changed since you looked — here is the file against it now; import again if it still reads right.",
                                 "preview": report}, status_code=409)
        line = (f"loot history imported from a {ledger_csv.KIND_WORDS[parsed.kind]}: {report['new']} award(s) added — {report['line']}"
                + (f" (import {report['batch']}: Undo on Loot › Items › Import)" if report.get("batch") else ""))
        if report.get("learned"):
            line += f"; {report['learned']} untiered item(s) now on Loot › Items › Untiered to tier"
        if report["new"]:
            ctx_of(reg).reload()  # the council reads the new rows at its next judgement (recent loot, already-has)
            await ops_mod.audited(bot.ops, reg, by_of(v), line, via=v.via, by_id=v.uid)
            publish("loot:ledger", reg.key)
            if report.get("learned"):
                publish("items", reg.key)  # Loot › Items lists the items the import learned
        else:
            line = "Nothing to import — " + report["line"]
        return {"message": line, "report": report}

    @app.get("/api/loot/imports")
    async def loot_imports(request: Request):
        """Loot › Items › Import's 'Earlier imports' (MCP import_batches): the ledger's imports, newest first, each with
        its batch id, who, when (a time a person reads), how many rows are still live and the Undo confirm's sentence."""
        v = await who(request, officer=True)
        return {"batches": await asyncio.to_thread(v.reg.import_batches)}

    @app.post("/api/loot/import/undo")
    async def loot_import_undo(request: Request):
        """Undo one loot history import (MCP undo_import): {batch, reason} — a void row for every award of that import
        still live (Registry.undo_import, the council's void row), never an edit or a delete; idempotent. Officers; from
        MCP the awards are in people's names, so it needs X-Oibot-Actor (D8). The council's context reloads."""
        v, d = await writer(request, actor="Undoing a loot import")
        batch = str(d.get("batch") or "").strip()
        if not batch:
            raise HTTPException(400, "batch required (an import's id from /api/loot/imports)")
        try:
            out = await asyncio.to_thread(v.reg.undo_import, batch, by_of(v), str(d.get("reason") or ""), v.uid)
        except RegistryError as e:
            from ..configops import user_error

            return JSONResponse({"error": user_error(e)}, status_code=400)
        if out["voided"]:
            ctx_of(v.reg).reload()  # the council stops counting the rows at its next judgement
            await ops_mod.audited(bot.ops, v.reg, by_of(v), out["line"], via=v.via, by_id=v.uid)
            publish("loot:ledger", v.reg.key)
        return {"message": out["line"], "voided": out["voided"]}

    @app.get("/api/loot/weights")
    async def loot_weights(request: Request):
        """Config › Loot scoring (L06): every weight with its value now and the game profile's default beside it, the
        groups in words, the `config:loot` rev a Save carries, and whether this viewer may edit (the owner)."""
        from ..loot_ops import WEIGHT_GROUPS, WEIGHT_NOTE, weight_rows

        v = await who(request, officer=True)
        reg = v.reg
        return {"rows": weight_rows(reg), "groups": [{"key": k, "title": t, "hint": h} for k, t, _p, h in WEIGHT_GROUPS],
                "rev": reg.config_rev("loot"), "owner": bool(v.owner), "note": WEIGHT_NOTE}

    @app.post("/api/loot/weights")
    async def loot_weights_save(request: Request):
        """The weights through the ONE structured core (a translator, like /api/admin/*): {fields: {path: number | null},
        revs?, confirmed?}. Weights are warn class (every future score): the first call answers `review_required` with
        the Described preview (each weight current → new, the default named) and writes nothing; send it again with
        `confirmed: true` and the revs it returned. The owner's; officers get the refusal. The site's card saves through
        POST /api/ops/change (useOpsSave → the ReviewModal); MCP set_loot_weights too."""
        v, d = await body(request, officer=True)
        fields = d.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise HTTPException(400, "fields: {weight path: number | null}, e.g. {'tier_weight.S': 0.8}")
        res = await request.app.state.ops_translate(v, [{"kind": "loot_weights", "fields": fields}], d)
        if isinstance(res, JSONResponse) or res.get("review_required"):
            return res
        from ..loot_ops import weight_rows

        return {"message": "; ".join(res["applied"]) or "loot scoring: unchanged", "rows": weight_rows(v.reg), "rev": v.reg.config_rev("loot")}

    @app.post("/api/loot/rescore")
    async def loot_rescore(request: Request):
        """"Re-score last run" (Config › Loot scoring): the newest council with drops scored again by the numbers alone
        — the weights now vs the draft `fields` ({path: number | null}, validated by the weights verb on a shadow copy).
        No model call, nothing written. Officers (the card is read-only for them; the owner edits)."""
        from ..loot import scoring
        from ..loot_ops import last_council

        v, d = await body(request, officer=True)
        reg = v.reg
        fields = d.get("fields") or {}
        if not isinstance(fields, dict):
            raise HTTPException(400, "fields: {weight path: number | null}")
        ctx = ctx_of(reg)

        def work():
            old = reg.loot_cfg()
            if fields:
                sh = reg.shadow()  # the verb validates the draft; the shadow never writes
                sh.set_loot_weights(fields, "preview")
                new = sh.loot_cfg()
            else:
                new = old
            last = last_council(bot, reg, ctx)
            if last is None:
                return None
            out = scoring.rescore(reg.profile, last, old, new, wishlists=ctx.wishlists, items=reg.items(), attendance=scoring.attendance_lookup(reg))
            out["run"] = {"label": last["label"], "day_label": last["day_label"], "roster": len(last["roster"])}
            return out

        try:
            out = await asyncio.to_thread(work)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if out is None:
            return {"run": None, "rows": [], "moved": 0, "drops": 0, "message": "No council has recorded a drop yet — nothing to re-score."}
        if not out["run"]["roster"]:
            out["message"] = f"{out['run']['label']}: its roster isn't on file any more — nobody to score."
        else:
            out["message"] = (f"{out['run']['label']} ({out['run']['day_label']}): {out['moved']} of {out['drops']} top pick(s) would change"
                              if fields else f"{out['run']['label']} ({out['run']['day_label']}): the weights as they are")
        return out

    @app.post("/api/loot/distil")
    async def loot_distil(request: Request):
        """Loot › Precedents › Distil (L29; MCP distil): Claude reads the ACTIVE precedents and the compiled loot policy and
        DRAFTS at most five clarification sentences, each with the precedents it rests on (code drops a sentence that rests
        on none it was shown). Nothing is written — an officer picks sentences and appends them through
        POST /api/loot/distil/append (the policy_append verb). Officers; a draft decides nothing (no MCP actor needed)."""
        from ..loot import distil

        v, _d = await writer(request)
        reg = v.reg
        ctx = ctx_of(reg)
        try:
            out = await asyncio.to_thread(distil.distil, reg.key, ctx, ctx.provider)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=503 if str(e) == distil.LLM_DOWN else 400)
        for p in out["proposals"]:
            for r in p["rests_on"]:
                r["date_label"] = day(reg, r.get("date"))
        return {"draft": out}

    @app.post("/api/loot/distil/append")
    async def loot_distil_append(request: Request):
        """The officer's pick from a distil: {sentences: [the exact text], doc: loot, token?}. Without `token`: the review —
        a Described preview (one line per sentence, the refusal its append would give) and the `token` of exactly those
        sentences; nothing written. With the token: each sentence appended through THE policy_append verb
        (policy.append_rule: '- <text>' as a draft the officers then compile and confirm), one commit, one audit line —
        a sentence changed after the review (another token) is refused (409), so the policy gets exactly what was
        confirmed. Officers."""
        from .. import policy as pol
        from ..configops import Described, DescribedLine, DescribedPart, result_summary
        from ..loot.distil import SENTENCE_MAX, batch_token

        v, d = await writer(request)
        reg = v.reg
        doc = str(d.get("doc") or "loot").strip().lower()
        if doc not in ("loot", "comp"):
            raise HTTPException(400, "doc is loot or comp")
        raw = d.get("sentences")
        if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
            raise HTTPException(400, "sentences: the list of sentences you picked")
        sentences = [re.sub(r"\s+", " ", x).strip().lstrip("-*• ").strip()[:SENTENCE_MAX] for x in raw]
        sentences = [s for s in dict.fromkeys(sentences) if s]
        if not sentences or len(sentences) > 5:
            raise HTTPException(400, "pick one to five sentences")
        ps = ctx_of(reg).policy_store
        token = batch_token(doc, sentences)
        held = ps.load_pending(doc)
        blocked = (pol.waiting_words(held, reg.local12) + " — confirm or discard it first") if pol.held_by_other(held, by_of(v), v.uid) else None
        if not d.get("token"):
            lines = [DescribedLine(op="policy_append", target=doc, label=f"{doc.capitalize()} policy", parts=[DescribedPart(label="Add", after=f"- {s}")],
                                   text=f"append to the {doc} policy: “{s}”", error=blocked[:1].upper() + blocked[1:] if blocked else None) for s in sentences]
            desc = Described(lines=lines, consequences=[f"Added as a draft: the officers compile and confirm the {doc} policy (Policy page) before the bot reads it."])
            return {"kind": "change", "described": {**desc.json_for(reg, "mcp" if v.via == "mcp" else "web"), "fingerprint": token}, "token": token,
                    "refused": [], "review": True, "fingerprint": token}
        if str(d.get("token")) != token:
            return JSONResponse({"error": "These sentences changed since you reviewed them — review them again.", "changed": ["policy"]}, status_code=409)
        if blocked:
            return JSONResponse({"error": blocked[:1].upper() + blocked[1:], "refused": [blocked]}, status_code=400)

        def work() -> list[str]:
            out = []
            with reg.store.batch(f"{reg.key}: {len(sentences)} clarification(s) from a distil added to the {doc} policy by {by_of(v)}"):
                for s in sentences:
                    _new, line = pol.append_rule(ps, doc, s, by_of(v), by_id=v.uid, when=reg.local12)
                    out.append(line)
            return out

        try:
            applied = await asyncio.to_thread(work)
        except (pol.PolicyPendingError, ValueError) as e:
            return JSONResponse({"error": str(e), "refused": [str(e)]}, status_code=400)
        await ops_mod.audited(bot.ops, reg, by_of(v), f"{doc} policy: {len(applied)} clarification(s) from the distil added as a draft — "
                              + "; ".join(f"“{s}”" for s in sentences), via=v.via, by_id=v.uid)
        res = {"applied": applied, "refused": [], "results": [{"label": f"{doc.capitalize()} policy", "ok": True, "line": ln} for ln in applied]}
        return {"kind": "change", **res, "summary": result_summary(res), "note": "Compile and confirm it on the Policy page to make it live."}

    # ------------------------------------------------------------ items and tiers (D4, D12)
    def item_brief(reg, key: str, it, ctx=None) -> dict:
        meta = reg.item_meta(key)
        out = {"key": key, "id": it.id, "name": it.name, "boss": it.boss, "slot": it.slot, "type": it.type, "tiered": bool(it.tiers), "learned": bool(meta)}
        if meta:
            out.update(first_seen_label=reg.local12(meta["first_seen"]) if meta.get("first_seen") else "", times_dropped=int(meta.get("times_dropped") or 0),
                       first_run=meta.get("first_run"), source=meta.get("source") or "companion")
        if ctx is not None:
            out["awards"] = len(ctx.awards_of_item(it))
        return out

    @app.get("/api/loot/items/search")
    async def loot_item_search(request: Request, q: str = "", limit: int = 25):
        """The wishlist's item picker (members too): items on the tables and the guild's learned ones, by name."""
        v = await who(request)
        n = q.strip().lower()
        hits = [(k, it) for k, it in v.reg.items().items() if not n or n in it.name.lower() or (n.isdigit() and str(it.id) == n)]
        hits.sort(key=lambda kv: (not kv[1].name.lower().startswith(n), kv[1].name.lower()))
        return {"items": [{"key": k, "id": it.id, "name": it.name, "boss": it.boss, "slot": it.slot, "tiered": bool(it.tiers)} for k, it in hits[:max(1, min(int(limit), 50))]]}

    @app.get("/api/loot/items/export")
    async def loot_items_export(request: Request):
        """The tier grid as CSV (officers; MCP export_tiers): item_id, item, boss, one column per spec key."""
        from ..loot import tierdraft

        v = await who(request, officer=True)
        text = await asyncio.to_thread(tierdraft.export_csv, v.reg)
        name = re.sub(r"[^a-z0-9]+", "-", v.reg.key.lower()).strip("-") or "guild"
        return Response(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}-tiers.csv"'})

    @app.post("/api/loot/items/import")
    async def loot_items_import(request: Request):
        """Import a tier CSV: {csv, preview?: true (default), rev?, reweight?}. Preview writes nothing and answers what
        would change per item, duplicates (merged), unknown items, bad cells, the items that already have awards and a
        `rev`. Apply (`preview: false`) needs that `rev` (none → 400; a tier changed since → 409, preview again); with past awards
        and no `reweight` answer nothing is written and the reply asks (D12, default no). From MCP the apply is a
        decision: X-Oibot-Actor (D8)."""
        from ..loot import tierdraft

        v, d = await writer(request)  # officers; the apply below is the decision that needs the actor from MCP (D8)
        reg = v.reg
        applying = d.get("preview", True) is False
        if applying:
            need_actor(v, "Importing tiers")
            if not isinstance(d.get("rev"), str) or not d["rev"]:
                return JSONResponse({"error": "Preview the file first — an import applies the preview it was shown (send its rev)."}, status_code=400)
        text = d.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "csv is the file's text (export the grid first to see the columns)")
        try:
            pv = await asyncio.to_thread(tierdraft.parse_csv, reg, text)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        ctx = ctx_of(reg)
        items = reg.items()
        for row in pv["items"]:
            it = items.get(row["key"])
            row["awards"] = len(ctx.awards_of_item(it)) if it is not None else 0
        pv["with_awards"] = sum(1 for r in pv["items"] if r["awards"])
        if not applying:
            return {"preview": pv}
        if not pv["items"]:
            return {"message": "Nothing to change — every tier in the file is already set.", "preview": pv}
        if pv["with_awards"] and d.get("reweight") is None:
            n = sum(r["awards"] for r in pv["items"])
            return {"ask": f"{n} past award(s) of {pv['with_awards']} item(s) used the old weight — re-weight them? (changes recent-loot scores only, not who got what)",
                    "preview": pv}
        try:
            done = await asyncio.to_thread(tierdraft.apply_csv, reg, pv, by_of(v), d.get("rev"))
        except RegistryError as e:
            return JSONResponse({"error": str(e), "preview": pv}, status_code=409 if "changed since" in str(e) else 400)
        reweighted = 0
        if d.get("reweight"):
            for key, _line, _ch in done:
                it = reg.items().get(key)
                if it is not None and ctx.awards_of_item(it):
                    reweighted += ctx.reweight(it, by_of(v))
            if reweighted:
                reg.store.commit(f"{reg.key}: {reweighted} award(s) re-weighted after a tier import ({by_of(v)})")
        line = f"tiers imported from a CSV: {sum(len(c) for _k, _l, c in done)} tier(s) on {len(done)} item(s)" + (f", {reweighted} past award(s) re-weighted" if reweighted else "")
        if done:
            for key, _l, _c in done:
                DRAFTS.pop((reg.key, key), None)
            await ops_mod.audited(bot.ops, reg, by_of(v), line, via=v.via, by_id=v.uid)
            publish("items", reg.key)
        return {"message": line, "items": [{"key": k, "line": ln} for k, ln, _c in done], "reweighted": reweighted}

    @app.get("/api/loot/items")
    async def loot_items(request: Request):
        """Loot › Items: the untiered items (learned from drops, or on the tables without tiers) with when they were
        first seen, how often they dropped and how many awards they have; every item; the spec rows of the tier grid."""
        v = await who(request, officer=True)
        reg = v.reg
        ctx = ctx_of(reg)
        items = reg.items()
        untiered = [item_brief(reg, k, it, ctx) for k, it in items.items() if not it.tiers]
        untiered.sort(key=lambda r: (-(r.get("times_dropped") or 0), r["name"].lower()))
        tiered = [item_brief(reg, k, it) for k, it in items.items() if it.tiers]
        tiered.sort(key=lambda r: r["name"].lower())
        return {"untiered": untiered, "tiered": tiered, "tiers": list(TIER_VALUES), "specs": spec_rows(reg)}

    def spec_rows(reg) -> list[dict]:
        out = []
        for cls, specs in reg.profile.classes.items():
            for sp, attrs in specs.items():
                out.append({"key": f"{cls}:{sp}", "cls": cls, "spec": sp, "role": attrs.get("role")})
        return out

    def item_of(reg, key: str):
        it = reg.items().get(key) or reg.item(key, key)
        if it is None:
            raise HTTPException(404, f"no item {key}")
        return item_key(it.id, it.name), it

    def item_detail(reg, key: str, it) -> dict:
        from ..profiles import equippable

        ctx = ctx_of(reg)
        own_doc = {}
        p = reg.store.root / reg.key / "items" / f"{key}.yaml"
        if p.exists():
            import yaml

            own_doc = yaml.safe_load(p.read_text()) or {}
        own = {str(k): str(v) for k, v in (own_doc.get("tiers") or {}).items()}
        prof = reg.profile.items.get(int(it.id)) if it.id else None
        rows = []
        for r in spec_rows(reg):
            spec = reg.profile.spec(r["cls"], r["spec"])
            ok, why = equippable(it, spec, reg.profile)
            rows.append({**r, "tier": it.tier_for(spec), "own": own.get(r["key"]), "profile": (prof.tiers.get(r["key"]) if prof else None),
                         "equippable": ok, "why": why})
        wide = {k: v for k, v in it.tiers.items() if k.endswith(":*") or k.startswith("role:")}
        dr = DRAFTS.get((reg.key, key))
        return {**item_brief(reg, key, it, ctx), "rows": rows, "wide": wide, "own": own, "tiers": list(TIER_VALUES), "roles": list(ROLES),
                "awards": len(ctx.awards_of_item(it)), "draft": draft_json(reg, dr) if dr else None}

    def draft_json(reg, dr: dict) -> dict:
        return {**dr, "at_label": reg.local12(dr["at"]) if dr.get("at") else ""}

    @app.post("/api/loot/items/{key}/draft")
    async def loot_item_draft(request: Request, key: str):
        """Loot › Items "Draft with Claude": the model PROPOSES tiers for this item from its facts, the guild's specs
        and the loot policy — validated (profile spec keys only, specs that can't equip it dropped, S+ | S | A | B),
        marked "drafted, unverified" and NOTHING is written: an officer reviews it on the grid and saves it per item
        through POST /api/loot/items/{key}/tiers (which asks the D12 re-weight question). Officers; from MCP no actor
        is needed — a draft decides nothing (draft_tiers)."""
        from ..loot import tierdraft

        v, _d = await writer(request)
        reg = v.reg
        k, it = item_of(reg, key)
        ctx = ctx_of(reg)
        try:
            dr = await asyncio.to_thread(tierdraft.draft, reg, it, ctx.provider, ctx.policy)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=503 if str(e) == tierdraft.LLM_DOWN else 400)
        dr["by"] = by_of(v)
        DRAFTS[(reg.key, k)] = dr
        return {"draft": draft_json(reg, dr), "item": item_detail(reg, k, it)}

    @app.get("/api/loot/items/{key}")
    async def loot_item(request: Request, key: str):
        """One item's tier grid: every spec with its tier (the guild's over the profile's), whether it can equip the
        item at all (equippable: impossible rows are greyed), and how many awards it already has (the re-weight ask)."""
        v = await who(request, officer=True)
        k, it = item_of(v.reg, key)
        return item_detail(v.reg, k, it)

    @app.post("/api/loot/items/{key}/tiers")
    async def loot_item_tiers(request: Request, key: str):
        """Save tiers for an item: {tiers: {spec key: tier | '-'}, reweight?: bool}. With past awards and no `reweight`
        answer, nothing is written: the reply asks (D12 — "N past awards used the default weight — re-weight them?",
        default no). `reweight` true appends re-weight rows (recent-loot scores only, never who got what)."""
        v, d = await writer(request, actor="Tiering an item")
        reg = v.reg
        k, it = item_of(reg, key)
        tiers = d.get("tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise HTTPException(400, "tiers is {spec key: tier}, e.g. {'Priest:Holy': 'S', 'role:tank': 'A'}")
        ctx = ctx_of(reg)
        n = len(ctx.awards_of_item(it))
        if n and d.get("reweight") is None:
            return {"ask": f"{n} past award(s) of {it.name} used the old weight — re-weight them? (changes recent-loot scores only, not who got what)",
                    "awards": n}
        try:
            line, changes = await asyncio.to_thread(reg.set_item_tiers, k, tiers, by_of(v))
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        done = 0
        if changes and n:
            new_it = reg.items().get(k) or it
            if d.get("reweight"):
                done = ctx.reweight(new_it, by_of(v))  # on the loop: the council reads this context
                if done:
                    reg.store.commit(f"{reg.key}: {done} award(s) of {it.name} re-weighted ({by_of(v)})")
                line += f" — re-weighted {done} past award(s)"
            else:
                line += " — past awards kept their weight"
        if changes:
            DRAFTS.pop((reg.key, k), None)  # the draft became tiers (or was overruled): nothing left to confirm
            await ops_mod.audited(bot.ops, reg, by_of(v), f"tiers: {line}", via=v.via, by_id=v.uid)
            publish("items", reg.key)
        return {"message": line, "reweighted": done, "item": item_detail(reg, k, reg.items().get(k) or it)}
