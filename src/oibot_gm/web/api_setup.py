"""Setup on the web (dual interfaces phase 3 — design §5.46; out/dual_interface_design.md §3.4 X3, §3.5 O-01/O-12/O-13/
OPS-01/S-01/S-05/S-06, §3.3 D).

- `GET /api/admin/channels`: every bot channel — what it is for, the channel set, and `missing` (deleted, or the
  permissions the bot lacks there: perms.missing_for, the test SetupMixin.set_channel runs before saving) — plus the
  guild's text channels for the pickers, `ready` (Discord reachable: names can be shown) and the settings rev an edit-mode
  Save sends back. `POST /api/admin/channels {channels: {kind: id | null}}` is a TRANSLATOR into the structured ops path
  (api_ops' translate → configops.apply_ops → set_channel per channel). The Config page's edit mode itself saves through
  POST /api/ops/change (useOpsSave: the ReviewModal shows channel_consequences through configops.CONSEQUENCE_HOOKS).
- `POST /api/admin/news-webhook` (owner, D39): the news channel's webhook for the owner's news source (created when the
  bot has Manage Webhooks); the URL is returned to the owner only and never written to an ops line.
- `GET /api/admin/setup`: the Setup card (SetupMixin.setup_checklist — every ✓ by the runtime's own test), the invite's
  state (SetupMixin.invite_status), the error alerts (who, how many folded into an earlier alert), `discord_links()`;
  for a host superuser the Host card: the invite link (perms.invite_link), servers the bot is in that aren't guilds, parked
  (detached) guilds.
- `GET /api/admin/left` + `POST /api/admin/member/remove {uid}`: people who left the server (Member.left_at) and the
  officers' Remove record (discord_registry.remove_record — D29: never a member's own delete; there is no such route).
- `GET /api/companions` + `POST /api/companions/issue {label}` + `POST /api/companions/revoke {id}` (owner; design §5.50):
  the guild's loot-companion tokens through SetupMixin.issue_companion / revoke_companion — the value is in the issue
  reply's body ONCE (no-store) and nowhere else (stored hashed, never logged); MCP lists and revokes but never issues.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import ops as ops_mod
from ..registry import RegistryError, is_superuser


def discord_ready(bot, reg) -> bool:
    """Discord can answer for this guild now: the gateway is up and the server is visible (not unavailable)."""
    ready = getattr(bot, "is_ready", None)
    if ready is not None and not ready():
        return False
    guild = bot.get_guild(reg.config.discord_guild_id) if getattr(bot, "get_guild", None) else None
    return guild is not None and not getattr(guild, "unavailable", False)


def error_dm_choices(reg) -> list[dict]:
    """The people the owner may pick for the error alerts on the Setup card: every registered (non-test) member and the
    owner, by display name — the ids Registry.set_error_dms takes."""
    seen, out = set(), []
    owner = reg.config.owner_discord_id
    for m in sorted((m for m in reg.members.values() if not m.test), key=lambda m: m.display_name.lower()):
        seen.add(m.discord_id)
        out.append({"id": str(m.discord_id), "name": m.display_name})
    if owner and int(owner) not in seen:
        out.insert(0, {"id": str(owner), "name": "the owner"})
    return out


def host_viewer(v) -> bool:
    """The host: a superuser on the web, or the MCP bearer (the host's own token)."""
    return v.via == "mcp" or is_superuser(v.uid)


def install_setup_api(app: FastAPI, bot, *, who, body, need_owner, as_int) -> None:
    from .. import perms
    from ..discord_pool import CHANNEL_KINDS, CHANNEL_LABELS

    def channel_rows(reg) -> list[dict]:
        cfg = reg.config
        ready = discord_ready(bot, reg)
        names = {c["id"]: c["name"] for c in (bot.guild_channels(reg) if ready else [])}
        out = []
        for kind, attr in CHANNEL_KINDS.items():
            cid = getattr(cfg, attr, None)
            label, hint = CHANNEL_LABELS.get(kind, (kind, ""))
            row = {"kind": kind, "label": label, "hint": hint, "id": str(cid) if cid else None, "name": names.get(str(cid)) if cid else None,
                   "missing": [], "problem": None, "required": list(perms.required(kind))}
            if cid and ready:
                ch = bot.get_channel(int(cid))
                if ch is None:
                    row["problem"] = "deleted, or the bot can't see it"
                else:
                    miss = perms.missing_for(ch, kind)
                    row["missing"] = [perms.WORDS.get(p, p) for p in miss]
                    if miss:
                        row["problem"] = perms.cant_post(getattr(ch, "name", str(cid)), miss)
            out.append(row)
        return out

    @app.get("/api/admin/channels")
    async def channels_get(request: Request):
        """Officers read, the owner edits: the bot's channels with the `missing` badge (deleted / the permissions the bot
        lacks) — never "not found" while Discord is unreachable (`ready` false: the page disables editing and says so)."""
        v = await who(request, officer=True)
        reg = v.reg
        ready = discord_ready(bot, reg)
        return {"channels": channel_rows(reg), "guild_channels": bot.guild_channels(reg) if ready else [], "ready": ready, "owner": v.owner,
                "rev": reg.config_rev("settings"), "links": bot.discord_links(reg) if hasattr(bot, "discord_links") else {},
                "unreachable": None if ready else "Discord is unreachable; names can't be shown. Editing waits until it's back."}

    @app.post("/api/admin/channels")
    async def channels_set(request: Request):
        """Owner: {channels: {kind: channel id | name | null}, revs?} — a translator: the structured ops path applies it
        (set_channel per channel: its permission check, the card posted first, its one ops line). {message, results}."""
        v, d = await body(request, officer=True)
        need_owner(v)
        chans = d.get("channels")
        if not isinstance(chans, dict) or not chans:
            raise HTTPException(400, "channels: a map of kind → channel id (null clears it)")
        bad = [k for k in chans if k not in CHANNEL_KINDS]
        if bad:
            raise HTTPException(400, f"unknown channel kind {bad[0]} (one of {', '.join(CHANNEL_KINDS)})")
        translate = app.state.ops_translate
        res = await translate(v, [{"kind": "settings", "fields": {f"channel:{k}": val for k, val in chans.items()}}], d)
        if isinstance(res, JSONResponse):
            return res
        return {"message": "; ".join(res["applied"]) or "channels: no change", "results": res["results"], "channels": channel_rows(v.reg)}

    @app.post("/api/admin/news-webhook")
    async def news_webhook(request: Request):
        """Owner (D39): the news channel's webhook for the news source — the bot's own, created when it has Manage Webhooks
        there; else what to add. The URL is a secret: in this reply only, never in an ops line."""
        v, d = await body(request, officer=True)
        need_owner(v)
        hook = getattr(bot, "news_webhook", None)
        if hook is None:
            raise HTTPException(503, "the bot isn't connected")
        res = await hook(v.reg, create=bool(d.get("create", True)))
        if res.get("created"):
            await ops_mod.audited(bot.ops, v.reg, v.name, "created the news webhook", via=v.via, by_id=v.uid)
        return res

    @app.get("/api/admin/setup")
    async def setup_get(request: Request):
        """The Setup card (officers; the checklist every ✓ of which the runtime computes the same way), the invite's state,
        the error alerts and, for the host, the Host card."""
        v = await who(request, officer=True)
        reg = v.reg
        ready = discord_ready(bot, reg)
        from ..configops import render

        chans = bot.guild_channels(reg) if ready else []
        web = lambda t: render(t, reg, "web", chans)  # noqa: E731 — mentions as #name / @Role / @Member, never a snowflake
        rows = [{**r, "text": web(r["text"]), "test": web(r["test"])} for r in (bot.setup_checklist(reg) if hasattr(bot, "setup_checklist") else [])]
        invite = await bot.invite_status(reg) if hasattr(bot, "invite_status") and ready else {"state": "unknown", "text": "Couldn't check the invite (Discord unreachable).", "checked": None}
        ops = getattr(bot, "ops", None)
        out = {"ready": ready, "checklist": rows, "invite": invite, "links": bot.discord_links(reg) if hasattr(bot, "discord_links") else {},
               "rev": reg.config_rev("settings"),  # the owner's error-alerts edit saves through /api/ops/change with it
               "errors": {"to": [str(u) for u in reg.error_dm_targets()], "words": web(reg.error_dm_words()), "default": reg.config.error_dm_ids is None,
                          "ids": None if reg.config.error_dm_ids is None else [str(u) for u in reg.config.error_dm_ids],
                          # who may be picked: the registered people (no puppets) plus the owner — names, never bare ids
                          "choices": error_dm_choices(reg) if v.owner else [],
                          "suppressed": ops.suppressed_for(reg.key) if ops is not None and hasattr(ops, "suppressed_for") else 0,
                          "ops_failing": ops.ops_channel_failing(reg.key) if ops is not None and hasattr(ops, "ops_channel_failing") else 0},
               "owner": v.owner, "host": None}
        if host_viewer(v):
            served = {int(r.config.discord_guild_id) for r in bot.registries.ordered()}
            parked = bot.registries.detached_list() if hasattr(bot.registries, "detached_list") else []
            parked_ids = {int(r.config.discord_guild_id) for r in parked}
            cid = os.environ.get("DISCORD_CLIENT_ID")
            out["host"] = {"invite_link": perms.invite_link(cid) if cid else None,
                           "unclaimed": [{"id": str(g.id), "name": g.name, "missing": [perms.WORDS.get(p, p) for p in perms.missing_for(g)]}
                                         for g in getattr(bot, "guilds", []) if int(g.id) not in served and int(g.id) not in parked_ids],
                           "detached": [{"key": r.key, "name": r.config.name, "detached": reg.local12(r.config.detached_at) if r.config.detached_at else None} for r in parked]}
        return out

    @app.get("/api/admin/left")
    async def left_get(request: Request):
        """Officers: members who left the Discord server (Member.left_at), oldest first — each may get Remove record."""
        v = await who(request, officer=True)
        reg = v.reg
        rows = sorted((m for m in reg.members.values() if m.left_at and not m.test), key=lambda m: m.left_at)
        from ..discord_registry import remove_record_consequence

        return {"left": [{"uid": str(m.discord_id), "name": m.display_name, "left": reg.local12(m.left_at), "characters": [c.label for c in m.characters],
                          "consequence": remove_record_consequence(reg, m)} for m in rows]}

    @app.post("/api/admin/member/remove")
    async def member_remove(request: Request):
        """Officers: {uid} → Remove record of someone who LEFT the server (discord_registry.remove_record: refused while they
        are in the server or hold a live seat; one ops line). There is no route for a member to delete their own record (D29)."""
        v, d = await body(request, officer=True)
        uid = as_int(d.get("uid"), "uid")
        from ..discord_registry import remove_record

        try:
            line = await remove_record(bot, v.reg, uid, v.name, by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line}

    # ---- companion tokens (design §5.50): owner only; the value only ever in the issue response's body, once
    def companions_state(reg) -> dict:
        from ..feed import issue_consequence, listen_state, token_rows, token_state
        from ..registry import FEED_LABEL_MAX, FEED_TOKEN_MAX

        rows_fn = getattr(bot, "companion_rows", None)
        rows = rows_fn(reg) if rows_fn is not None else token_rows(reg, [])
        ls = listen_state(getattr(bot, "feed", None))  # loopback-only says "this machine only" (never the address)
        return {"tokens": rows, "listening": ls["listening"], "local_only": ls["local_only"], "listen_words": ls["words"],
                "max": FEED_TOKEN_MAX, "label_max": FEED_LABEL_MAX,
                "issue_consequence": issue_consequence(reg, None).replace("**", ""), "env": token_state(reg.key, many=bool(getattr(bot.registries, "many", False)))}

    @app.get("/api/companions")
    async def companions_get(request: Request):
        """Owner: the guild's companion tokens (label · presence · last seen · who issued it), never a value or a hash;
        `env` names which .env feed-token variables are set (names only); `listening`: the loot feed runs on this host,
        `local_only` + `listen_words` (feed.listen_state): on a loopback bind only this machine's companions reach it."""
        v = await who(request)
        need_owner(v)  # the owner (or a host superuser) — owner rights come from the id, not the officer roles
        return companions_state(v.reg)

    @app.post("/api/companions/issue")
    async def companions_issue(request: Request):
        """Owner: {label} → a new token. The reply's `token` is the ONLY time the value exists outside the companion: the
        page shows it once (Copy, "I've saved it"); it is stored hashed, never logged (the request log has the path and
        status only), never in the audit line. MCP never issues one (a value must not enter a Claude transcript)."""
        v, d = await body(request)
        need_owner(v)
        if v.via == "mcp":
            raise HTTPException(403, "MCP never issues a companion token (its value would land in a transcript) — issue it on the site or with /gm config companion.")
        issue = getattr(bot, "issue_companion", None)
        if issue is None:
            raise HTTPException(503, "the bot isn't connected")
        try:
            res = await issue(v.reg, str(d.get("label") or ""), v.name, by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        rec = res["record"]
        return JSONResponse({"token": res["token"], "id": rec.id, "label": rec.label, "message": res["message"], "listening": res.get("listening", False),
                             **companions_state(v.reg)}, headers={"Cache-Control": "no-store"})

    @app.post("/api/companions/revoke")
    async def companions_revoke(request: Request):
        """Owner (the site, MCP revoke_companion): {id} (a token's id, or its label) → revoked; the feed refuses it at the
        companion's next message, no restart. {message, tokens…}."""
        v, d = await body(request)
        need_owner(v)
        revoke = getattr(bot, "revoke_companion", None)
        if revoke is None:
            raise HTTPException(503, "the bot isn't connected")
        try:
            line = await revoke(v.reg, str(d.get("id") or ""), v.name, by_id=v.uid, via=v.via)
        except (RegistryError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"message": line, **companions_state(v.reg)}
