"""The structured ops path on the web (dual interfaces §2.3, §3.5 S2; design §5.43).

`POST /api/ops/change` takes, beside the plain-text `text` (Claude parses it; officers), TYPED changes:
`changes: [{kind, target, fields}]` (configops.ops_from_fields builds the ops server-side) and/or raw `ops: [ConfigOp]`.
Preview = configops.describe_ops → `described` (STRUCTURE, mentions rendered for the caller); apply = authorize_ops BY
PRIVILEGE (role_of, decided live for a write) → coalesce → apply_ops (one write per raid / schedule / buff / family, one
commit, one audit line). Tiered review (D51): `apply: true` without a token saves directly unless the batch has a warn-class
op or a consequence line — then it answers `needs_review` + a token and writes nothing; `confirmed: true` (MCP after its own
preview, the admin translators) skips that. Section revs ride in `revs` as 'config:<section>' and a stale one is a 409.

The admin routes (/api/admin/config|aura|family|aura/reset|raid|raid/reset|raid/schedule|raid/schedule/remove|recruitment|
plain-permissions) are TRANSLATORS: their old bodies become changes through ops_from_fields and go through the one core —
same answers, same state, one code path with plain text and MCP."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import raidcycle as rc
from ..registry import PLAIN_WEB, ConfigChanged, RegistryError

log = logging.getLogger(__name__)

PREVIEW_TTL = 600  # seconds a preview token stays good (Discord's Apply view waits as long)
BOOT = secrets.token_hex(4)  # this process: a token minted before a restart is told apart from one that merely expired


def need_token_error(restarted: bool) -> dict:
    return {"error": f"apply needs the token of a preview you made in the last {PREVIEW_TTL // 60} minutes — preview again (without apply) and apply that",
            "restarted": restarted}


def viewer_role(v) -> str:
    """owner | officer | member, from the viewer (web/app.py privilege = guild_routing.role_of)."""
    return "owner" if v.owner else "officer" if v.officer else "member"


def install_ops_api(app: FastAPI, bot, *, who, body, live_officer=None, plain_json, recruitment_json, guild_role_refs, need_owner, parse, InstanceBody, IdBody) -> None:
    from .. import configops
    from ..configops import ConfigOp
    from ..policy import PolicyStore

    previews: dict[str, dict] = {}  # preview token -> the authorized ops + revs it showed (single use)
    app.state.ops_previews = previews  # the tests age a token out through here

    def mint() -> str:
        return f"{BOOT}.{secrets.token_urlsafe(16)}"

    async def live_role(v) -> str:
        """The viewer's role decided NOW for a write (PRIV_LIVE; the owner and the MCP bearer need no lookup)."""
        if live_officer is not None:
            await live_officer(v)
        return viewer_role(v)

    def mcp_actor_rule(v, allowed, refused):
        """D8: from MCP without X-Oibot-Actor, the loot decisions (an award, a confirm, a reason, a correction, a retired
        precedent, tiers, someone else's wishlist) are refused — the owner's token is not the officer who decided."""
        if v.via != "mcp" or getattr(v, "actor", None):
            return allowed, refused
        from ..configops import people_needs_actor
        from ..loot_ops import needs_actor

        keep = [op for op in allowed if not needs_actor(op, v.uid)]
        if len(keep) != len(allowed):
            refused = [*refused, "Loot decisions (an award, a confirm, a reason, a correction, tiers, someone else's wishlist) from MCP need the "
                                 "X-Oibot-Actor header naming an officer of this guild (OIBOT_MCP_ACTOR)."]
        people = [op for op in keep if not people_needs_actor(op)]
        if len(people) != len(keep):  # D8: a rank or a character's confirmation is a decision about a person
            refused = [*refused, "A rank or a confirmation from MCP needs the X-Oibot-Actor header naming an officer of this guild (OIBOT_MCP_ACTOR)."]
        return people, refused

    def warm_roles(reg) -> None:
        """Role names → ids against the live guild before a change names a role (the name cache; no write)."""
        guild = bot.get_guild(reg.config.discord_guild_id) if getattr(bot, "get_guild", None) else None
        if guild is not None:
            reg.cache_roles(guild)

    def channels_of(reg) -> list[dict]:
        try:
            return list(bot.guild_channels(reg) or [])
        except Exception:  # noqa: BLE001 — no gateway: ids stay ids, names can't be shown
            return []

    def raids_of(reg):
        return bot.raids.store(reg) if getattr(bot, "raids", None) is not None else None

    def surface(v) -> str:
        return "mcp" if v.via == "mcp" else "web"

    def stale_sections(reg, revs: dict) -> list[str]:
        return [k[len(configops.CONFIG_REV):] for k, val in revs.items() if k.startswith(configops.CONFIG_REV) and reg.config_rev(k[len(configops.CONFIG_REV):]) != val]

    def config_revs(d: dict) -> dict:
        return {str(k): str(x) for k, x in (d.get("revs") or {}).items() if str(k).startswith(configops.CONFIG_REV)} if isinstance(d.get("revs"), dict) else {}

    def changed_response(reg, sections: list[str]) -> JSONResponse:
        return JSONResponse({"error": str(ConfigChanged(sections[0])), "changed": sections,
                             "revs": {configops.CONFIG_REV + s: reg.config_rev(s) for s in sections}}, status_code=409)

    def finish(pv: dict, res: dict) -> JSONResponse | dict:
        out = {**pv, **res, "summary": configops.result_summary(res)}
        if res.get("refused") and res.get("applied"):
            out["note"] = "Everything else listed was saved; nothing else was changed."
        elif res.get("refused"):
            out["note"] = "Nothing was changed."
        if res.get("changed") and not res.get("applied"):
            return JSONResponse({**out, "error": res.get("error") or "changed since you looked"}, status_code=409)
        return out

    async def apply_now(v, allowed: list, refused: list, revs: dict) -> dict:
        res = await configops.apply_ops(v.reg, allowed, by=v.name, by_id=v.uid, bot=bot, via=v.via, revs=revs, raids=raids_of(v.reg),
                                        policy_store=PolicyStore(bot.registries.store, v.reg.key))
        res["refused"] = [*refused, *res["refused"]]
        return res

    async def typed_ops(v, changes, raw_ops=None) -> list | JSONResponse:
        """configops.ops_from_changes for a request (§2.7): a draft that can't be expressed is a 400 in its own words; an
        unexpected error is reported (report_error: logged, the owner alerted) and answers the generic line — never a bare 500."""
        try:
            return configops.ops_from_changes(v.reg, changes, raw_ops, channels=channels_of(v.reg))
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            msg = await configops.report_error(getattr(bot, "ops", None), v.reg.config, e, f"[{v.via}] structured change")
            return JSONResponse({"error": msg}, status_code=500 if msg == configops.GENERIC_ERROR else 400)

    async def structured(v, d: dict, *, reason: str | None = None) -> JSONResponse | dict:
        """Preview, tiered Save or confirmed apply of typed changes. `reason`: a token that was 'expired' (re-preview,
        and apply silently when the lines are the ones the person saw — `seen`) or from before a 'restarted' bot (shown
        again, never applied without a press)."""
        reg = v.reg
        warm_roles(reg)
        ops = await typed_ops(v, d.get("changes"), d.get("ops"))
        if isinstance(ops, JSONResponse):
            return ops
        if not ops:
            return {"kind": "change", "described": configops.Described().json_for(reg), "describe": [], "ops": [], "refused": [], "applied": [], "results": [],
                    "review": False, "warn": False, "summary": "nothing to change"}
        crevs = config_revs(d)
        stale = stale_sections(reg, crevs)
        if stale:
            return changed_response(reg, stale)
        want_apply = bool(d.get("apply"))
        role = await live_role(v) if want_apply else viewer_role(v)
        raids = raids_of(reg)
        allowed, refused = configops.authorize_ops(reg, ops, v.uid, role=role, raids=raids)
        allowed, refused = mcp_actor_rule(v, allowed, refused)
        described = await asyncio.to_thread(configops.describe_ops, reg, allowed, refused)
        chans = channels_of(reg)
        revs = {**configops.board_revs(reg, allowed, raids), **crevs}
        pv = {"kind": "change", "described": described.json_for(reg, surface(v), chans), "describe": [ln.text for ln in described.lines],
              "ops": [op.model_dump(exclude_none=True) for op in allowed], "refused": list(refused), "review": described.review, "warn": described.warn,
              "fingerprint": described.fingerprint}
        if not allowed:
            return {**pv, "applied": [], "results": [], "summary": configops.result_summary({"refused": refused})}
        silent = reason == "expired" and want_apply and d.get("seen") == described.fingerprint
        if want_apply and (silent or (reason is None and (d.get("confirmed") or not described.review))):
            return finish(pv, await apply_now(v, allowed, [], revs))
        token = mint()
        previews[token] = {"kind": "structured", "uid": v.uid, "guild": reg.key, "ops": pv["ops"], "revs": revs, "fingerprint": described.fingerprint,
                           "expires": time.monotonic() + PREVIEW_TTL}
        out = {**pv, "token": token, "expires_in": PREVIEW_TTL, "revs": {k: x for k, x in revs.items() if k.startswith(configops.CONFIG_REV)}}
        if want_apply:
            out["needs_review"] = True
        if reason:
            out.update(updated=reason == "expired", restarted=reason == "restarted")
        return out

    @app.post("/api/ops/change")
    async def ops_change(request: Request):
        """Two shapes, two calls each (preview, then apply with the token):
        - plain text `{text}` (officers; what `/gm change` does): Claude parses it into whitelisted ConfigOps, `authorize`
          keeps what this caller may do under the plain-text policy, `describe` says current → new; `{token, apply: true}`
          applies exactly those ops — never a re-parse — with the boards' revs as shown.
        - typed `{changes: [{kind, target, fields}], ops?: [ConfigOp], revs?}` (any member; authorized by privilege, a
          member only for their own record): `described` = configops.Described (lines with parts current → new, refused
          lines with who may and where, consequences, warn, review, fingerprint) + `token`. `{changes, apply: true}` = a
          Save: applied at once unless `review` (a warn-class op or a consequence line) — then `needs_review` + token and
          nothing written; `confirmed: true` applies regardless. `{token, apply: true}` applies a reviewed preview; with
          `changes` + `seen` (the fingerprint shown) a token that expired re-previews and applies when nothing moved,
          one from before a restart comes back with `restarted: true` for another look.
        Apply answers `applied`, `refused` (each with why — the rest still applied), `results` [{label, ok, line}],
        `error`, `changed` (config sections someone else changed: 409 when nothing applied) and `summary` ('3 saved, 1 refused')."""
        v, d = await body(request)
        now = time.monotonic()
        for k in [k for k, p in previews.items() if p["expires"] < now]:
            previews.pop(k, None)
        reg = v.reg
        raids = raids_of(reg)
        if d.get("apply") and d.get("token"):
            tok = str(d.get("token") or "")
            held = previews.pop(tok, None)
            if held is not None and (held["uid"] != v.uid or held["guild"] != reg.key):
                held = None
            if held is None:
                restarted = not tok.startswith(BOOT + ".")
                if d.get("changes") or d.get("ops"):
                    return await structured(v, d, reason="restarted" if restarted else "expired")
                return JSONResponse(need_token_error(restarted), status_code=400)
            if held["kind"] == "structured":
                ops = [ConfigOp(**o) for o in held["ops"]]
                allowed, refused = configops.authorize_ops(reg, ops, v.uid, role=await live_role(v), raids=raids)
                allowed, refused = mcp_actor_rule(v, allowed, refused)
                pv = {"kind": "change", "ops": [op.model_dump(exclude_none=True) for op in allowed]}
                return finish(pv, await apply_now(v, allowed, refused, dict(held["revs"])))
            return await text_apply(v, held, raids)
        if "changes" in d or "ops" in d:
            return await structured(v, d)
        if d.get("apply"):  # plain text applies only a preview's token — never a fresh parse of the text
            return JSONResponse(need_token_error(False), status_code=400)
        if not v.officer:
            raise HTTPException(403, "Officers only.")
        return await text_preview(v, d, raids, now)

    async def text_apply(v, held: dict, raids):
        """The plain-text token's apply (unchanged from phase 0): authorize again under the chat policy, then apply_ops."""
        if not v.officer:
            raise HTTPException(403, "Officers only.")
        ops = [ConfigOp(**o) for o in held["ops"]]
        if live_officer is not None:  # a write: the rights are decided now (PRIV_LIVE), not from the login's lookup
            await live_officer(v)
        allowed, refused = configops.authorize(v.reg, ops, v.uid, officer=v.officer, channel=PLAIN_WEB, raids=raids)
        allowed, refused = mcp_actor_rule(v, allowed, refused)
        out = {"kind": "change", "reply": held["reply"], "questions": [], "ops": [op.model_dump(exclude_none=True) for op in allowed],
               "describe": held["describe"], "applied": [], "refused": refused, "error": None}
        revs, stale = dict(held["revs"]), set()
        ps = PolicyStore(bot.registries.store, v.reg.key)

        def failed(op, e) -> None:  # as Discord's Apply: listed with what it was, and the rest of the request still applies
            out["refused"].append(f"{configops.describe(v.reg, op, PLAIN_WEB)} — {e}")
            out["error"] = out["error"] or str(e)

        for op in configops.coalesce(allowed):  # consecutive raid/schedule/buff/family settings save as one write (as on Discord)
            key = configops.op_run_key(v.reg, op, raids)
            if op.op in configops.BOARD_OPS and key in stale:  # an earlier step found someone else's change on this board
                failed(op, rc.BoardChanged())
                continue
            before = configops.snapshot_revs(v.reg, revs, raids)
            try:
                line = await configops.apply_async(v.reg, op, v.name, True, ps, bot=bot, by_id=v.uid, channel_id=PLAIN_WEB,  # authorize() allowed it
                                                   rev=configops.op_rev(op, key, revs), via=v.via)
            except (RegistryError, ValueError) as e:
                failed(op, e)
                if isinstance(e, rc.BoardChanged) and key:
                    stale.add(key)  # keep the old rev: the rest of this request can't overwrite that change either
                continue
            except Exception as e:  # noqa: BLE001 — Discord or the solver: never a 500 after a partial apply; the caller sees what landed
                failed(op, await configops.report_error(bot.ops, v.reg.config, e, f"[{v.via}] plain change {op.op}"))
                continue
            configops.refresh_revs(v.reg, revs, raids, stale, before)  # this request's own write is not someone else's change
            out["applied"].append(line)  # apply_async(via=) wrote the audit line
        return out

    async def text_preview(v, d: dict, raids, now: float):
        text = str(d.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text required (or changes: [{kind, target, fields}])"}, status_code=400)
        provider = getattr(getattr(bot, "ctx", None), "provider", None)
        if provider is None:
            return JSONResponse({"error": "the LLM provider is off (ANTHROPIC_API_KEY unset)"}, status_code=503)
        try:
            req = await asyncio.to_thread(configops.parse, provider, v.reg, text, v.name, officer=v.officer)
        except Exception as e:  # noqa: BLE001 — provider/schema errors are 502s the caller can retry
            return JSONResponse({"error": f"could not parse the request: {e}"}, status_code=502)
        # the same policy check as Discord: the site (and MCP, as the owner) counts as a channel where plain text acts
        allowed, refused = configops.authorize(v.reg, req.ops, v.uid, officer=v.officer, channel=PLAIN_WEB, raids=raids)
        allowed, refused = mcp_actor_rule(v, allowed, refused)
        req.ops = allowed
        ops = [op.model_dump(exclude_none=True) for op in req.ops]
        desc = []
        for op in req.ops:
            try:
                desc.append(configops.describe(v.reg, op, PLAIN_WEB))
            except Exception as e:  # noqa: BLE001
                desc.append(f"{op.op}: {e}")
        out = {"kind": req.kind, "reply": req.reply, "questions": list(req.questions), "ops": ops, "describe": desc, "applied": [], "refused": refused}
        if req.kind == "change" and req.ops:
            try:
                described = await asyncio.to_thread(configops.describe_ops, v.reg, req.ops, refused)
                out["described"] = described.json_for(v.reg, surface(v), channels_of(v.reg))
            except Exception:  # noqa: BLE001 — the text lines above still say it
                log.exception("describe_ops failed for a plain-text preview")
            token = mint()
            previews[token] = {"kind": "text", "uid": v.uid, "guild": v.reg.key, "ops": ops, "describe": desc, "reply": req.reply,
                               "revs": configops.board_revs(v.reg, req.ops, raids), "expires": now + PREVIEW_TTL}
            out.update(token=token, expires_in=PREVIEW_TTL)
        return out

    # ---- the admin routes: translators into ops (one core; their answers are what they always were)

    async def translate(v, changes: list[dict], d: dict | None = None) -> dict | JSONResponse:
        """The old body's changes through the one core, applied as the route always did (no review step: the page or
        tool asked already). {applied, refused, results, error, changed} or the refusal as JSON (400; 409 when a section
        changed since the caller's `revs`)."""
        reg = v.reg
        warm_roles(reg)
        ops = await typed_ops(v, changes)
        if isinstance(ops, JSONResponse):
            return ops
        crevs = config_revs(d or {})
        stale = stale_sections(reg, crevs)
        if stale:
            return changed_response(reg, stale)
        allowed, refused = configops.authorize_ops(reg, ops, v.uid, role=await live_role(v), raids=raids_of(reg))
        if refused:
            return JSONResponse({"error": refused[0], "refused": refused}, status_code=403)
        res = await apply_now(v, allowed, [], crevs)
        if res["refused"]:
            return JSONResponse({"error": res["error"], "applied": res["applied"], "refused": res["refused"], "changed": res["changed"]},
                                status_code=409 if res["changed"] and not res["applied"] else 400)
        return res

    app.state.ops_translate = translate  # the other translators (web/api_setup.py's channels) reach the one core through here

    @app.post("/api/admin/raid/schedule")
    async def admin_raid_schedule(request: Request):
        """Change or create one schedule (owner): {instance, id, fields: {setting: value}} — a cadence value of null goes
        back to the raid's. One registry write (Registry.set_schedule): validated as a whole, one commit."""
        v, d = await body(request, officer=True)
        need_owner(v)
        inst, sid = parse(d, InstanceBody).instance, str(d.get("id") or "").strip()
        fields = d.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise HTTPException(400, "fields: a map of schedule settings to change")
        res = await translate(v, [{"kind": "schedule", "target": f"{inst}:{sid}", "fields": fields}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"]) or f"{inst}: no changes"}

    @app.post("/api/admin/raid/schedule/remove")
    async def admin_raid_schedule_remove(request: Request):
        v, d = await body(request, officer=True)
        need_owner(v)
        inst = parse(d, InstanceBody).instance
        res = await translate(v, [{"kind": "schedule_remove", "target": f"{inst}:{str(d.get('id') or '').strip()}"}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"])}

    @app.post("/api/admin/raid")
    async def admin_raid(request: Request):
        """The Raids page's form (owner): only the fields that differ from the raid now, ONE set_raid_overrides write —
        a refused field leaves the raid exactly as it was."""
        v, d = await body(request, officer=True)
        need_owner(v)
        inst = parse(d, InstanceBody).instance
        res = await translate(v, [{"kind": "raid", "target": inst, "fields": {k: x for k, x in d.items() if k not in ("instance", "revs")}}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"]) or f"{inst}: no changes"}

    @app.post("/api/admin/raid/reset")
    async def admin_raid_reset(request: Request):
        v, d = await body(request, officer=True)
        need_owner(v)
        res = await translate(v, [{"kind": "raid_reset", "target": parse(d, InstanceBody).instance}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"])}

    @app.post("/api/admin/aura")
    async def admin_aura(request: Request):
        """One buff: any of scope / family / strength / status / note (only fields present in the body change) — ONE
        set_buff_overrides write, so a refused field leaves the buff as it was."""
        v, d = await body(request, officer=True)
        need_owner(v)
        bid = parse(d, IdBody).id
        fields = {f: d[f] for f in ("scope", "family", "strength", "status", "note", "family_new") if f in d and d[f] is not None}
        res = await translate(v, [{"kind": "buff", "target": bid, "fields": fields}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"]) or f"{bid}: no changes"}

    @app.post("/api/admin/family")
    async def admin_family(request: Request):
        """One family: name / status / note / value (whole beneficiary map) / value:<key> (one entry) — ONE
        set_family_overrides write. A new family without an id gets one from its name (never typed)."""
        v, d = await body(request, officer=True)
        need_owner(v)
        fid = str(d.get("id") or "").strip()
        if not fid and not str(d.get("name") or "").strip():
            parse(d, IdBody)  # the old 400: an id (or, new, a name) is required
        if "value" in d and d["value"] is not None and not isinstance(d["value"], dict):
            raise HTTPException(400, "value must be a map of beneficiary → number")
        fields = {f: x for f, x in d.items() if (f in ("name", "status", "note", "value") and x is not None) or f.startswith("value:")}
        res = await translate(v, [{"kind": "family", "target": fid or None, "fields": fields}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"]) or f"{fid or 'family'}: no changes"}

    @app.post("/api/admin/aura/reset")
    async def admin_aura_reset(request: Request):
        v, d = await body(request, officer=True)
        need_owner(v)
        res = await translate(v, [{"kind": "aura_reset", "target": d.get("id") or None}], d)
        return res if isinstance(res, JSONResponse) else {"message": "; ".join(res["applied"])}

    @app.post("/api/admin/config")
    async def admin_config(request: Request):
        """Owner: one setting at a time — channel:<kind> (channel id, name or null), timezone, ask_audience, about,
        news_keywords, loot_log, officer_roles (the whole list of role ids)."""
        v, d = await body(request, officer=True)
        need_owner(v)
        field, value = str(d.get("field") or ""), d.get("value")
        if not (field.startswith("channel:") or field in ("officer_roles", "timezone", "ask_audience", "about", "news_keywords", "loot_log")):
            return JSONResponse({"error": f"unknown setting {field}"}, status_code=400)
        have = set(v.reg.config.officer_role_ids)
        res = await translate(v, [{"kind": "settings", "fields": {field: value}}], d)
        if isinstance(res, JSONResponse):
            return res
        if field == "officer_roles":
            want = set(v.reg.config.officer_role_ids)
            names = getattr(bot, "officer_role_names", None)
            shown = names(v.reg) if names is not None else v.reg.officer_role_names()
            return {"message": ("officer roles: " if want != have else "officer roles unchanged: ") + (", ".join(shown) or "(none; Manage Server only)")}
        return {"message": "; ".join(res["applied"])}

    @app.post("/api/admin/plain-permissions")
    async def plain_set(request: Request):
        """Owner: the whole policy — {groups: {group: [who…]}, channels: {channel id: mode}, dm, default}; or
        `set_channels` {channel id: mode | null} to change some channels and keep the rest. Groups or fields left out
        keep their current value. One set_plain_policy write (the plain ops coalesce)."""
        v, d = await body(request, officer=True)
        need_owner(v)
        guild = bot.get_guild(v.reg.config.discord_guild_id)
        if guild is not None:
            v.reg.cache_roles(guild)
        fields = {k: d[k] for k in ("groups", "channels", "set_channels", "dm", "default") if k in d}
        res = await translate(v, [{"kind": "plain", "fields": fields}], d)
        if isinstance(res, JSONResponse):
            return res
        return {"message": "; ".join(res["applied"]) or "plain-text permissions: no change", **plain_json(v.reg), "owner": True}

    @app.post("/api/admin/recruitment")
    async def recruitment_set(request: Request):
        """Owner: any of {open, note, questions: [{id?, label, hint, required}], invite_url, member_role_ids} in one
        validated write (Registry.set_recruitment); a refusal names the part and nothing is saved."""
        v, d = await body(request, officer=True)
        need_owner(v)
        if "questions" in d and (not isinstance(d["questions"], list) or not all(isinstance(q, dict) for q in d["questions"])):
            return JSONResponse({"error": "questions must be a list of {label, hint, required}"}, status_code=400)
        fields = {k: d[k] for k in ("open", "note", "invite_url", "questions", "member_role_ids") if k in d}
        res = await translate(v, [{"kind": "recruitment", "fields": fields}], d)
        if isinstance(res, JSONResponse):
            return res
        return {"message": "; ".join(res["applied"]) or "recruitment: no change", **recruitment_json(v.reg), "guild_roles": guild_role_refs(v.reg), "owner": True}
