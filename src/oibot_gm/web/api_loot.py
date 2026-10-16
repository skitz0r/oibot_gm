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
- D12: tiering an item that already has awards asks "re-weight them?" (answer `reweight`: default no, logged)."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

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
        }

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
    async def me_loot(request: Request, limit: int = 50):
        """My loot: what my characters received (by my Discord id; by character name for rows recorded without one)."""
        v = await who(request)
        reg = v.reg
        ctx = ctx_of(reg)
        mine = {c.label.lower() for c in (v.member.characters if v.member else [])}
        chars = char_index(reg)
        rows = [a for a in ctx.ledger if a.discord_id == v.uid or (a.discord_id is None and a.raider.lower() in mine)]
        rows.sort(key=lambda a: (a.received, a.confirmed_at or ""), reverse=True)
        return {"rows": [log_row(reg, a, chars, False, False, ctx, v.uid, mine) for a in rows[:max(1, min(int(limit), 200))]]}

    # ------------------------------------------------------------ wishlists (D5)
    def received_map(reg, ctx, m) -> dict[tuple[str, str], str]:
        """(character label lower, item key) → the day it was received: an awarded wish shows as received, greyed —
        never deleted (the next Save would undo it)."""
        out = {}
        labels = {c.label.lower() for c in m.characters} if m else set()
        for a in ctx.ledger:
            if a.raider.lower() in labels and (a.discord_id in (None, m.discord_id)):
                it = reg.item(a.item_id or None, a.item_name)
                keys = {item_key(a.item_id, a.item_name or "")} | ({item_key(it.id, it.name)} if it is not None else set()) | {"n:" + (a.item_name or "").lower()}
                for k in keys:
                    prev = out.get((a.raider.lower(), k))
                    if prev is None or a.received.isoformat() > prev:
                        out[(a.raider.lower(), k)] = a.received.isoformat()
        return out

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

    # ------------------------------------------------------------ items and tiers (D4, D12)
    def item_brief(reg, key: str, it, ctx=None) -> dict:
        meta = reg.item_meta(key)
        out = {"key": key, "id": it.id, "name": it.name, "boss": it.boss, "slot": it.slot, "type": it.type, "tiered": bool(it.tiers), "learned": bool(meta)}
        if meta:
            out.update(first_seen_label=reg.local12(meta["first_seen"]) if meta.get("first_seen") else "", times_dropped=int(meta.get("times_dropped") or 0),
                       first_run=meta.get("first_run"))
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
        return {**item_brief(reg, key, it, ctx), "rows": rows, "wide": wide, "own": own, "tiers": list(TIER_VALUES), "roles": list(ROLES),
                "awards": len(ctx.awards_of_item(it))}

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
            await ops_mod.audited(bot.ops, reg, by_of(v), f"tiers: {line}", via=v.via, by_id=v.uid)
            publish("items", reg.key)
        return {"message": line, "reweighted": done, "item": item_detail(reg, k, reg.items().get(k) or it)}
