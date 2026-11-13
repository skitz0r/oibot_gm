"""A character's armory link on the site and through MCP (armory link §5.2, §5.4, §5.5): thin callers of the verbs in
discord_armory (find / link / unlink / the professions switch / Sync now / move) and armory_seed (propose / apply).
No business rule lives here: the verbs decide rights live, refuse in one sentence and write the one audit line.

- A member acts on their OWN characters only: `/api/me/armory/<verb>` takes a `label`, never a uid.
- Officers act for anyone: `/api/members/armory/<verb>` takes `uid` (or `member`: a name / character / id, as MCP sends).
- Every write takes `preview` (the confirm sentences, nothing written; Sync now has no confirm). From MCP a write needs
  X-Oibot-Actor (D8); a preview needs none.
- Every route answers 404 where the guild's version has no armory or the host has no Blizzard keys.
- After every write the `member` topic is published (a sync that changed a record publishes it through the registry's
  SAVE_LISTENERS; a new roster read through discord_armory.ROSTER_LISTENERS, installed here)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import armory as armory_mod
from .. import armory_seed, discord_armory
from .. import consequences as cq
from ..armory import ArmoryAccess, ArmoryDown, ArmoryError
from ..discord_armory import LOOKUPS_PER_MINUTE, NO_GUILD_YET, NOT_READ_YET, TOO_MANY_LOOKUPS  # noqa: F401 — the core's words, re-exported
from ..registry import CharactersChanged, ConfigChanged, RegistryError

ACTOR_NEEDED = ("Linking, unlinking or moving a character is a decision a person makes: from MCP it needs OIBOT_MCP_ACTOR set to "
                "an officer's Discord id (sent as X-Oibot-Actor). A preview needs none.")
LOOK_FIRST = "Look the character up first, then link the one it found."
FOUND_ANOTHER = "That name now finds a different character in the armory. Look it up again before linking."
SEED_CHANGED = "The roster or the records changed since you looked. Read the new proposal, then confirm again."
NO_PICKS = "Pick at least one character to link."
PICK_ON = "Choose whether the armory keeps this character's professions."
PICK_SHAPE = "Each pick names a member, one of their characters and a character from the guild's roster."
PICK_ROW = "Pick a character from the guild's roster."
PICK_TO = "Pick the member it moves to."


def armory_json(reg, c, fl: list[str] | None = None) -> dict | None:
    """A character's link as every character row carries it (`char_json`: the Me and Members pages; the map). None where
    the version has no armory. `linked` false = not linked yet. `name` / `surname` = the record's (a linked record takes
    the armory's), `level` / `cls` = the armory's (null before a link), `updated` = "Updated from the armory <when>",
    `when` = that <when> alone (Registry.local12; null before the first check — the site's "Updated <when>"),
    `in_guild` = on the linked guild's roster (true / false / null = not known), `missing` = the armory has not found it
    for a day (the "missing" flag)."""
    if not armory_mod.available(reg):
        return None
    a = c.armory
    if a is None:
        return {"linked": False, "id": None, "name": c.name, "surname": c.surname, "level": None, "cls": None, "updated": None,
                "when": None, "in_guild": None, "missing": False}
    fl = discord_armory.flags(reg, c) if fl is None else fl  # `fl`: the row's flags when the caller has them already
    at = discord_armory.last_checked(reg, c)
    return {"linked": True, "id": a.id, "name": c.name, "surname": c.surname, "level": a.level, "cls": a.cls,
            "updated": discord_armory.updated_words(reg, c), "when": reg.local12(at) if at else None, "in_guild": discord_armory.in_guild(reg, c), "missing": "missing" in fl}


def char_extras(reg, c) -> dict:
    """The armory facts on a character row: `armory` (armory_json), `profession_sync` (true = the armory keeps its
    professions), `flags` ("class" · "missing" · "outside"; [] without an armory)."""
    if not armory_mod.available(reg):
        return {"armory": None, "profession_sync": bool(getattr(c, "profession_sync", True)), "flags": []}
    fl = discord_armory.flags(reg, c)
    return {"armory": armory_json(reg, c, fl), "profession_sync": bool(getattr(c, "profession_sync", True)), "flags": fl}


def row_json(r) -> dict:
    """A roster row: {id, name, surname, label, cls, level}."""
    return {"id": r.id, "name": r.name, "surname": r.surname, "label": f"{r.name} {r.surname}" if r.surname else r.name, "cls": r.cls, "level": r.level}


def found_json(f) -> dict:
    """What a lookup found: {id, name, surname, label, cls, level, source: profile | roster}."""
    return {"id": f.id, "name": f.name, "surname": f.surname, "label": f.label, "cls": f.cls, "level": f.level, "source": f.source}


def roster_state(reg) -> dict:
    """The guild's roster as last read: {guild: name | null, read: "<local12>" | null, words: the sentence when there is
    none (no guild linked / not read yet), else null}."""
    _rows, at = discord_armory.roster_snapshot(reg)
    g = reg.config.armory
    if at:
        return {"guild": g.name if g else None, "read": reg.local12(at), "words": None}
    return {"guild": g.name if g else None, "read": None, "words": NO_GUILD_YET if g is None else NOT_READ_YET}


def seed_row_json(r) -> dict:
    return {"uid": str(r.uid), "member": r.member, "label": r.label, "cls": r.cls, "candidate": row_json(r.candidate) if r.candidate else None,
            "why": r.why, "others": [row_json(x) for x in r.others]}


def seed_json(reg, seed) -> dict:
    return {"sure": [seed_row_json(r) for r in seed.sure], "likely": [seed_row_json(r) for r in seed.likely],
            "none": [seed_row_json(r) for r in seed.none], "unclaimed": [row_json(r) for r in seed.unclaimed], "rev": seed.rev,
            "counts": {"sure": len(seed.sure), "likely": len(seed.likely), "none": len(seed.none), "unclaimed": len(seed.unclaimed)},
            "roster": roster_state(reg)}


def propose_now(reg):
    rows, at = discord_armory.roster_snapshot(reg)
    return armory_seed.propose(reg, rows, taken_at=at)


def armory_error_response(e: ArmoryError) -> JSONResponse:
    """THE armory failure as JSON, on every route that asks the armory (here and the guild link in api_setup): down or
    refusing the host's keys → 503 with Retry-After (try again later); anything else the armory said → 400. Its words."""
    if isinstance(e, (ArmoryDown, ArmoryAccess)):
        return JSONResponse({"error": e.words}, status_code=503, headers={"Retry-After": "60"})
    return JSONResponse({"error": e.words}, status_code=400)


def install_armory_api(app: FastAPI, bot, *, who, body, publish=None, visible=None, char_json=None, resolve_member=None, live_officer=None) -> None:
    def published(guild_key: str) -> None:
        fn = getattr(app.state, "publish", None) or publish  # app.state: the one fan-out (tests read it there)
        if fn is not None:
            fn("member", guild_key)  # an open Me / Members page reloads

    def tell(reg) -> None:
        published(reg.key)

    def roster_read(guild_key: str) -> None:
        published(guild_key)  # a new roster read: "in the guild" and "not claimed" may have moved

    hooks = discord_armory.ROSTER_LISTENERS
    hooks[:] = [f for f in hooks if getattr(f, "__name__", "") != roster_read.__name__] + [roster_read]  # one per process, the newest app wins

    def need_armory(reg) -> None:
        if not armory_mod.available(reg):
            raise HTTPException(404, armory_mod.NO_ARMORY_WORDS if not reg.profile.armory else armory_mod.ACCESS_WORDS)

    def need_actor(v, preview: bool) -> None:
        if not preview and v.via == "mcp" and not getattr(v, "actor", None):
            raise HTTPException(403, ACTOR_NEEDED)

    async def fail(v, request: Request, e: Exception):
        """A verb's refusal as JSON: its sentence (400), a stale record (409), the armory's words (400); anything else is
        a bug: GENERIC_ERROR (500) and the owner's ops line (configops.report_error)."""
        from ..configops import GENERIC_ERROR, report_error

        if isinstance(e, HTTPException):
            raise e
        if isinstance(e, (CharactersChanged, ConfigChanged)):
            return JSONResponse({"error": str(e)}, status_code=409)
        if isinstance(e, RegistryError):
            return JSONResponse({"error": str(e)}, status_code=400)
        if isinstance(e, ArmoryError):
            return armory_error_response(e)
        text = await report_error(bot.ops, v.reg.config, e, f"[{v.via}] {v.name} {request.url.path}")
        return JSONResponse({"error": text}, status_code=500 if text == GENERIC_ERROR else 400)

    def member_of(v, d: dict):
        """The member an officer route acts on: `uid` (the site) or `member` (a name, character or id — MCP)."""
        ref = d.get("member") if d.get("member") not in (None, "") else d.get("uid")
        if resolve_member is not None:
            return resolve_member(v.reg, ref)
        try:
            m = v.reg.members.get(int(str(ref)))
        except (TypeError, ValueError):
            m = None
        if m is None:
            raise HTTPException(400, "unknown member")
        return m

    async def target(request: Request, officer: bool):
        """(v, d, uid): a member's own uid on /api/me/armory/*, the named member's on /api/members/armory/*."""
        v, d = await body(request, officer=officer)
        need_armory(v.reg)
        uid = member_of(v, d).discord_id if officer else v.uid  # a member's body never names another uid
        return v, d, uid

    def row_out(reg, c) -> dict:
        return char_json(reg, c) if char_json is not None else {"label": c.label, **char_extras(reg, c)}

    # ---- the verbs, one handler each for both surfaces (the member's own / an officer's for anyone)

    async def do_find(request: Request, officer: bool):
        """{label, name?, surname?} → what the armory shows under that name and the link's confirm sentences; nothing
        written. `name` defaults to the character's first name."""
        v, d, uid = await target(request, officer)
        return await find_preview(request, v, d, uid)

    async def lookup(v, d: dict, uid: int):
        """(label, Found): the member's character `label` (the verb's own refusal when there is none — before any request),
        then what the armory shows under `name` (default: the character's first name) and `surname`. At most
        LOOKUPS_PER_MINUTE a minute per person (429 TOO_MANY_LOOKUPS)."""
        label = str(d.get("label") or "").strip()
        _m, c = discord_armory.character_of(v.reg, uid, label)
        name = str(d.get("name") or "").strip() or c.name
        if not discord_armory.lookup_allowed(v.reg, v.uid):
            raise HTTPException(429, TOO_MANY_LOOKUPS)
        return label, await discord_armory.find_for_link(bot, v.reg, name, d.get("surname") or None)

    async def find_preview(request: Request, v, d: dict, uid: int):
        try:
            label, found = await lookup(v, d, uid)
            said = await discord_armory.link_character(bot, v.reg, uid, label, found, v.name, by_id=v.uid, via=v.via, preview=True)
        except Exception as e:  # noqa: BLE001 — refusals read as written
            return await fail(v, request, e)
        return {"preview": True, "found": found_json(found), "sentences": said}

    async def do_link(request: Request, officer: bool):
        """{label, name?, surname?, armory_id, preview?}: preview = find; a link names the armory_id its preview showed
        (D1: never a fresh unseen lookup) and is refused (409) when the name now finds another character."""
        v, d, uid = await target(request, officer)
        preview = bool(d.get("preview"))
        need_actor(v, preview)
        if preview:
            return await find_preview(request, v, d, uid)
        if d.get("armory_id") in (None, ""):
            return JSONResponse({"error": LOOK_FIRST}, status_code=400)
        reg = v.reg
        try:
            label, found = await lookup(v, d, uid)
            if str(found.id) != str(d.get("armory_id")):
                return JSONResponse({"error": FOUND_ANOTHER, "found": found_json(found)}, status_code=409)
            c = await discord_armory.link_character(bot, reg, uid, label, found, v.name, by_id=v.uid, via=v.via)
        except Exception as e:  # noqa: BLE001
            return await fail(v, request, e)
        tell(reg)
        return {"message": f"{c.label} is linked to the armory.", "character": row_out(reg, c)}

    async def do_unlink(request: Request, officer: bool):
        """{label, preview?}: the record keeps what it last had; its professions are kept by hand from now on."""
        v, d, uid = await target(request, officer)
        preview = bool(d.get("preview"))
        need_actor(v, preview)
        try:
            out = await discord_armory.unlink_character(bot, v.reg, uid, str(d.get("label") or ""), v.name, by_id=v.uid, via=v.via, preview=preview)
        except Exception as e:  # noqa: BLE001
            return await fail(v, request, e)
        if preview:
            return {"preview": True, "sentences": out}
        tell(v.reg)
        return {"message": f"{out.label} is unlinked from the armory.", "character": row_out(v.reg, out)}

    async def do_switch(request: Request, officer: bool):
        """{label, on: bool, preview?}: on = the armory keeps the professions (it replaces the hand edits now); off = kept
        by hand. A switch already in that state previews one sentence saying so and writes nothing."""
        v, d, uid = await target(request, officer)
        preview = bool(d.get("preview"))
        need_actor(v, preview)
        if not isinstance(d.get("on"), bool):
            return JSONResponse({"error": PICK_ON}, status_code=400)
        on = d["on"]
        try:
            out = await discord_armory.set_profession_sync(bot, v.reg, uid, str(d.get("label") or ""), on, v.name, by_id=v.uid, via=v.via, preview=preview)
        except Exception as e:  # noqa: BLE001
            return await fail(v, request, e)
        if preview:
            return {"preview": True, "sentences": out}
        tell(v.reg)
        said = f"{out.label}'s professions follow the armory." if on else f"{out.label}'s professions are kept by hand."
        return {"message": said, "character": row_out(v.reg, out)}

    async def do_sync(request: Request, officer: bool):
        """{label}: Sync now — read this character from the armory at once (at most every five minutes per character).
        → {result: updated | unchanged | missing | limited, message, character}. No confirm, so no preview."""
        v, d, uid = await target(request, officer)
        need_actor(v, False)
        label = str(d.get("label") or "")
        try:
            res = await discord_armory.sync_character(bot, v.reg, uid, label, force=True, by_id=v.uid)
        except Exception as e:  # noqa: BLE001
            return await fail(v, request, e)
        m = v.reg.members.get(uid)
        c = v.reg.own_character([x for x in m.active() if x.name], label) if m else None
        if res in ("updated", "missing"):
            tell(v.reg)
        return {"result": res, "message": cq.armory_sync_sentence(c.label if c else label, res), "character": row_out(v.reg, c) if c else None}

    VERBS = {"find": do_find, "link": do_link, "unlink": do_unlink, "switch": do_switch, "sync": do_sync}

    def route(verb: str, officer: bool):
        async def handler(request: Request):
            return await VERBS[verb](request, officer)
        handler.__name__ = f"armory_{'members' if officer else 'me'}_{verb}"
        return handler

    for verb in VERBS:
        app.post(f"/api/me/armory/{verb}")(route(verb, False))
        app.post(f"/api/members/armory/{verb}")(route(verb, True))

    # ---- Give to a member… (§5.4, the not-claimed fold): link their character, or register it for them, then link

    @app.post("/api/members/armory/give")
    async def armory_give(request: Request):
        """Officers: {uid | member, armory_id, label?, spec?, offspec?, preview?} → armory_seed.give (every rule is the
        verb's). preview → {preview, found, sentences}; a write → {registered, linked, message, character}: `linked` false
        with `registered` true = the character was registered but the link was refused afterwards (the message says why)."""
        v, d = await body(request, officer=True)
        reg = v.reg
        need_armory(reg)
        preview = bool(d.get("preview"))
        need_actor(v, preview)
        try:
            m = member_of(v, d) if (d.get("member") or d.get("uid")) not in (None, "") else None
        except HTTPException as e:
            if "unknown member" not in str(e.detail):  # an ambiguous name says which ones it could be
                raise
            m = None
        try:
            aid = int(d.get("armory_id"))
        except (TypeError, ValueError):
            return JSONResponse({"error": PICK_ROW}, status_code=400)
        uid = m.discord_id if m is not None else int(str(d.get("uid") or 0)) if str(d.get("uid") or "").isdigit() else 0
        try:
            out = await armory_seed.give(bot, reg, uid, aid, v.name, label=str(d.get("label") or "").strip() or None, spec=d.get("spec"),
                                         offspec=d.get("offspec"), by_id=v.uid, via=v.via, preview=preview)
        except Exception as e:  # noqa: BLE001 — refusals read as written
            return await fail(v, request, e)
        if preview:
            return {"preview": True, "found": found_json(out["found"]), "sentences": out["sentences"]}
        tell(reg)
        c = out["character"]
        return {"registered": out["registered"], "linked": out["linked"], "message": out["text"], "character": row_out(reg, c) if c else None}

    # ---- the officers' map (§5.4)

    @app.get("/api/armory/map")
    async def armory_map(request: Request):
        """Officers: the guild's characters nobody claims, the roster's age and the counts — linked / not yet over exactly
        the characters the seed considers (armory_seed.considered: no test puppets, nobody who left), so "not yet" reaches
        zero when every one is linked. MCP alone also gets `members` (per member as the Members page lists them, each
        named, active character's link state); the page reads each link from its own row."""
        v = await who(request, officer=True)
        reg = v.reg
        need_armory(reg)
        linked = unlinked = 0
        for m in reg.members.values():
            for c in m.characters:
                if armory_seed.considered(m, c):
                    linked += c.armory is not None
                    unlinked += c.armory is None
        unclaimed = [row_json(r) for r in sorted(discord_armory.unclaimed(reg), key=lambda r: (r.name.lower(), (r.surname or "").lower()))]
        out = {"unclaimed": unclaimed, "roster": roster_state(reg), "counts": {"linked": linked, "not_linked": unlinked, "unclaimed": len(unclaimed)}}
        if v.via == "mcp":
            out["members"] = map_members(v, reg)
        return out

    def map_members(v, reg) -> list[dict]:
        rows = []
        for m in sorted((m for m in reg.members.values() if visible is None or visible(v, m)), key=lambda m: m.display_name.lower()):
            chars = []
            for c in sorted((c for c in m.active() if c.name), key=lambda c: (not c.is_main, c.created_at)):
                a = armory_json(reg, c) or {}
                chars.append({"label": c.label, "cls": c.cls, "is_main": c.is_main, "linked": bool(a.get("linked")), "armory_id": a.get("id"),
                              "level": a.get("level"), "armory_cls": a.get("cls"), "updated": a.get("updated"), "in_guild": a.get("in_guild"),
                              "profession_sync": bool(c.profession_sync), "flags": discord_armory.flags(reg, c)})
            rows.append({"uid": str(m.discord_id), "display_name": m.display_name, "test": bool(m.test), "left": bool(m.left_at), "characters": chars})
        return rows

    # ---- the seed (§5.5)

    @app.get("/api/armory/seed")
    async def armory_seed_get(request: Request):
        """Officers: the registered characters set against the roster — sure / likely / none, the not-claimed rows, and
        the proposal's `rev` (the apply names it). Nothing written, nothing asked of the armory."""
        v = await who(request, officer=True)
        need_armory(v.reg)
        return seed_json(v.reg, propose_now(v.reg))

    @app.post("/api/armory/seed/apply")
    async def armory_seed_apply(request: Request):
        """Officers: {rev, picks: [{uid, label, armory_id}], preview?} (MCP may name `member` / `character` in a pick).
        `rev` = the proposal the officer read; a proposal
        that moved since answers 409 with the new one. Each pick is linked on its own; a refusal is one sentence in its
        result row and never stops the others."""
        v, d = await body(request, officer=True)
        reg = v.reg
        need_armory(reg)
        preview = bool(d.get("preview"))
        need_actor(v, preview)
        picks = []
        for p in d.get("picks") or []:
            try:  # `uid` (the site) or `member` (MCP: a name, character or id); `label` or `character`
                uid = member_of(v, p).discord_id if isinstance(p, dict) and p.get("member") not in (None, "") else int(str(p["uid"]))
                picks.append((uid, str(p.get("label") or p["character"]), int(p["armory_id"])))
            except (KeyError, TypeError, ValueError, AttributeError):
                return JSONResponse({"error": PICK_SHAPE}, status_code=400)
        if not picks:
            return JSONResponse({"error": NO_PICKS}, status_code=400)
        fresh = propose_now(reg)
        if str(d.get("rev") or "") != fresh.rev:
            return JSONResponse({"error": SEED_CHANGED, **seed_json(reg, fresh)}, status_code=409)
        if preview:  # every check made before asking the armory, so the sentence counts the picks that will go
            checked = armory_seed.check(reg, picks, by_id=v.uid)
            ok = sum(1 for r in checked if r["ok"])
            return {"preview": True, "sentences": cq.armory_seed_consequence(ok, len(checked) - ok),
                    "results": [{**r, "uid": str(r["uid"])} for r in checked]}
        try:
            results = await armory_seed.apply(bot, reg, picks, v.name, by_id=v.uid, via=v.via)
        except Exception as e:  # noqa: BLE001
            return await fail(v, request, e)
        tell(reg)
        ok = sum(1 for r in results if r["ok"])
        head, refused = armory_seed.result_lines(reg, results)  # Discord's words: the head, then only the refused picks
        return {"message": "\n".join([head, *refused]), "head": head, "refused": refused, "linked": ok,
                "results": [{**r, "uid": str(r["uid"])} for r in results], **seed_json(reg, propose_now(reg))}

    # ---- Move to another member… (§5.4, officers)

    @app.post("/api/members/move")
    async def members_move(request: Request):
        """Officers: {uid | member (from), label, to (a uid or name), preview?} — the character and everything that
        follows it (past loot, attendance, signups, wishlist, regular spots, its armory link) become `to`'s. Both get a
        notice, nobody a DM; the verb writes the one audit line."""
        v, d = await body(request, officer=True)
        reg = v.reg  # R11: a move needs no armory — it works on every game version
        preview = bool(d.get("preview"))
        need_actor(v, preview)
        m_from = member_of(v, d)
        if d.get("to") in (None, ""):
            return JSONResponse({"error": PICK_TO}, status_code=400)
        m_to = member_of(v, {"member": d.get("to")})
        try:
            out = await discord_armory.move_character(bot, reg, m_from.discord_id, str(d.get("label") or ""), m_to.discord_id, v.name,
                                                      by_id=v.uid, via=v.via, preview=preview)
        except Exception as e:  # noqa: BLE001
            return await fail(v, request, e)
        if preview:
            return {"preview": True, "sentences": out}
        tell(reg)
        return {"message": f"{out.label} moved to {m_to.display_name}.", "character": row_out(reg, out)}
