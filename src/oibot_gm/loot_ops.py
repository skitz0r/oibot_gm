"""The loot council, wishlists and item tiers as plain-text ops (design §3.1 "Plain / MCP", dual interfaces phase 1).

configops owns the pipeline (parse → authorize → describe → Apply → apply_async); this module is its loot branch:

- `loot` (capability group `loot`, officers by default): field = open | tick | distribute | award | confirm | discard |
  reason | correct | end | retire, target = the run (key, roster key, raid name; a split run's roster council by its
  session id '<run>-r2'). Every field calls the SAME LootMixin verb the thread's buttons and the site call (one verb,
  every surface a caller, §2.2) — the verb writes the audit line. An award needs a reason (it becomes the precedent).
- `wishlist` (members; a member's own list is "self"): field = add | remove, value = the item, character, rank (1–10),
  text = ms | os, reason = a note — Registry.wish_add / wish_remove.
- `tier` (group `loot`): target = the item, field = a spec key ('Priest:Holy', 'Priest:*', 'role:healer'), value =
  S+ | S | A | B | '-' (clear), text = 'reweight' to re-weight past awards (D12: default no) — Registry.set_item_tiers.

D8: from MCP (`via` mcp without X-Oibot-Actor) the decisions about an award or a person (award, confirm, reason, correct,
retire, tier, someone else's wishlist) are refused (`needs_actor`); the site and Discord decide by the person's role."""
from __future__ import annotations

import asyncio

import re

from .registry import TIER_VALUES, WISHLIST_MAX, Registry, RegistryError

LOOT_FIELDS = ("open", "tick", "distribute", "award", "confirm", "discard", "reason", "correct", "end", "retire")
DECISION_FIELDS = ("award", "confirm", "reason", "correct", "retire")  # D8: an MCP actor only
ALL_WORDS = {"", "all", "the rest", "rest", "everything", "all pending", "every pick", "*"}
LOOT_REV = "loot:"  # configops.board_revs key prefix: the batch_rev a confirm-all / discard-all diff showed

SCHEMA = """Loot council (officer, op=loot; target = the run key from the live-runs list, the raid name when it has one live run, or a split
  run's roster council '<run key>-r2'): field=open (open the council: a thread in the officer channel) | tick (value = what dropped, a comma
  list of item names or ids; text = the boss) | distribute (judge the ticked drops; value = drop ids to judge only those) |
  award (value = the drop id like d3 or the item's name, member = the character who gets it instead of the bot's pick, or
  "disenchant"; reason REQUIRED — it becomes the precedent: "give the Syphon to Eltam, tanks progression") | confirm (value = a drop
  id / item, or empty = every pick still pending: "confirm the rest") | discard (value = a drop id / item = decide it later, empty = the
  whole pending batch) | reason (value = the drop id / item the companion recorded against the bot's pick, reason = why) |
  correct (value = drop id / item, member = who it should be recorded to, reason = what went wrong — a mistake, no precedent) |
  end (end the run and its council: archived with its summary) | retire (value = a precedent's ref or item name, reason = why the bot
  should stop citing it; text = "restore" brings it back).
Wishlists (op=wishlist; a member's own list: member EMPTY): field=add|remove, value=<the item's name or id>, character=<which of their
  characters; blank = their main>, rank=<1-10, position on the list; blank = after the last>, text=ms|os (main or offspec; default ms),
  reason=<a short note>. Up to 10 items per character. "put the Robe of Tests on my wishlist as #2 for offspec" = add, rank 2, text os.
Item tiers (officer, op=tier): target=<the item's name or id>, field=<a spec 'Priest:Holy', a whole class 'Priest:*' or a role 'role:healer'>,
  value=S+|S|A|B|- (- clears a tier the guild set); text=reweight to re-weight the item's past awards to the new tier (default: they
  keep their weight). Several specs = several tier ops.
"""


def needs_actor(op, author_id: int | None) -> bool:
    """D8: a decision about an award or a person — from MCP it needs X-Oibot-Actor (the owner's token alone may read,
    preview, tick, distribute, discard and end)."""
    if op.op == "loot":
        return (op.field or "").strip().lower() in DECISION_FIELDS
    if op.op == "tier":
        return True
    if op.op == "wishlist":
        return (op.member or "").strip() != f"<@{author_id}>"
    return False


def loot_log_state(reg) -> dict:
    """The public log's three states (Config's loot card, Loot › Tonight): off · on with a channel · on with no channel
    (each run's updates thread — never the signup channel — or nowhere when the run has none)."""
    cfg = reg.config
    if not cfg.loot_log:
        return {"state": "off", "channel_id": str(cfg.loot_channel_id) if cfg.loot_channel_id else None,
                "text": "Public loot log off — awards are recorded and shown on the site only."}
    if cfg.loot_channel_id:
        return {"state": "channel", "channel_id": str(cfg.loot_channel_id), "text": "Each confirmed award gets one line in the loot channel."}
    return {"state": "no_channel", "channel_id": None,
            "text": "Log on, no channel — awards are posted in each run's updates thread (nowhere when the run has none). Pick a channel to keep one log."}


# ---------------------------------------------------------------- where an op points

def _store_sessions(reg: Registry, run_key: str) -> list:
    """The councils of a run as saved (describe() has no bot: the session files are what the bot last wrote)."""
    from .loot.session import LootSession

    d = reg.store.root / reg.key / "events"
    out = []
    for f in sorted(d.glob(f"{run_key}*.json")) if d.is_dir() else []:
        if f.stem != run_key and not re.fullmatch(re.escape(run_key) + r"-r\d+", f.stem):
            continue
        try:
            out.append(LootSession.model_validate_json(f.read_text()))
        except Exception:  # noqa: BLE001 — an unreadable file is no council
            continue
    return sorted(out, key=lambda s: s.roster_index or 0)


def _run_of(reg: Registry, target: str | None, raids=None):
    """(RaidStore, RaidEvent, roster index or None) for a loot op's target: a session id '<run>-r<N>', a run key (a
    done run too: reasons and corrections outlive the council), else a live run by configops' resolver."""
    from . import raidcycle as rc
    from .configops import _live_event

    rs = raids if raids is not None else rc.RaidStore(reg.store, reg.key)
    t = (target or "").strip()
    m = re.fullmatch(r"(.+)-r(\d+)", t)
    if m and m.group(1) in rs.events:
        return rs, rs.events[m.group(1)], int(m.group(2)) - 1
    if t in rs.events:
        return rs, rs.events[t], None
    rs, ev = _live_event(reg, t, rs)
    return rs, ev, None


def _pick(sessions: list, ev, idx: int | None, ended_ok: bool = False):
    """The council an op is for (a session, or — `ended_ok` — the id of one that has ended)."""
    if idx is not None:
        s = next((s for s in sessions if s.roster_index == idx), None)
        if s is not None:
            return s
        if ended_ok:
            return f"{ev.key}-r{idx + 1}"
        raise RegistryError(f"No loot council for roster {idx + 1} of {ev.key}.")
    if len(sessions) == 1:
        return sessions[0]
    if not sessions:
        if ended_ok:
            return ev.key
        raise RegistryError(f"No loot council is open on {ev.key} — open it first (loot open, /raid loot or the site).")
    raise RegistryError(f"{ev.key} has one council per roster — say which: target = " + " or ".join(s.id for s in sessions))


def _drop(s, ref: str | None, want: str):
    """A drop of a session by its id ('d3') or its item's name: `want` = pending (a pick to confirm / discard / award),
    open (anything not yet recorded), awarded (a recorded one, for a reason or a correction)."""
    r = (ref or "").strip()
    if not r:
        raise RegistryError("Which drop? Give its id (d3) or the item's name.")
    d = s.drop(r.lower()) or s.drop(r)
    if d is not None:
        return d
    low = r.lower()
    hits = [d for d in s.drop_list if d.item_name.lower() == low] or [d for d in s.drop_list if low in d.item_name.lower()]
    if want == "pending":
        hits = [d for d in hits if s.pending(d.drop_id)] or hits
    elif want == "open":
        hits = [d for d in hits if not s.awarded(d.drop_id)] or hits
    elif want == "awarded":
        hits = [d for d in hits if s.awarded(d.drop_id)] or hits
    if not hits:
        raise RegistryError(f"No drop {r} on this council.")
    return hits[0]


def _who(to: str | None) -> str:
    return "disenchant" if str(to or "").strip().lower() in ("disenchant", "(disenchant)", "de") else str(to or "").strip()


def rev_key(op) -> str:
    """The revs key a loot op's Apply reads (configops.op_rev): the batch for a confirm-all / discard-all, the one drop
    for a single confirm."""
    v = (op.value or "").strip().lower()
    return LOOT_REV + (op.target or "").strip() + ("" if v in ALL_WORDS else "#" + v)


def loot_revs(reg: Registry, ops, raids=None) -> dict[str, str]:
    """What a diff showed, for Apply to carry (configops.board_revs): a confirm-all / discard-all → the batch_rev (a
    batch someone overrode since is refused instead of recorded); a single confirm → 'to:<recipient>' (the pick shown:
    Apply records it only if it still goes there — expect_to)."""
    from .loot import session as ls

    out = {}
    for op in ops:
        f = (op.field or "").strip().lower()
        if op.op != "loot" or f not in ("confirm", "discard"):
            continue
        whole = (op.value or "").strip().lower() in ALL_WORDS
        if not whole and f != "confirm":
            continue
        try:
            _, ev, idx = _run_of(reg, op.target, raids)
            s = _pick(_store_sessions(reg, ev.key), ev, idx)
            if whole:
                out.setdefault(rev_key(op), ls.batch_rev(s))
            else:
                p = s.pending(_drop(s, op.value, "pending").drop_id)
                if p is not None:
                    out.setdefault(rev_key(op), "to:" + p.to)
        except RegistryError:
            continue
    return out


# ---------------------------------------------------------------- describe (the diff line)

def describe(reg: Registry, op) -> str:
    """'Syphon of the Nathrezim (d3): bot picked Torsha → council awards Eltam — “tanks progression”' — what Apply
    will do, from the saved state. Raises RegistryError when it can't apply (no reason on an award, no such drop)."""
    if op.op == "wishlist":
        return _describe_wish(reg, op)
    if op.op == "tier":
        return _describe_tier(reg, op)
    from .raid_views import run_title

    f = (op.field or "").strip().lower()
    if f not in LOOT_FIELDS:
        raise RegistryError(f"loot field is one of {', '.join(LOOT_FIELDS)}")
    if f == "retire":
        return _describe_retire(reg, op)
    _, ev, idx = _run_of(reg, op.target)
    title = run_title(reg, ev)
    sessions = _store_sessions(reg, ev.key)
    if f == "open":
        if ev.state != "locked":
            raise RegistryError(f"{title} isn't locked — lock the roster first.")
        if sessions:
            return f"loot council for {title}: already open — the card is re-posted in its thread"
        where = "the roster channel" if reg.config.roster_channel_id else "the ops channel" if reg.config.ops_channel_id else None
        if where is None:
            raise RegistryError("Set a roster or ops channel first — the loot council's thread goes there.")
        n = len(ev.all_rosters)
        return f"open the loot council for {title}: a thread in {where}" + (f" (one per roster: {n})" if n > 1 else "")
    if f == "end":
        if ev.state != "locked" and not sessions:  # LootMixin.end_run's rule, refused at the diff already (every surface)
            raise RegistryError("No locked run to end." if ev.state not in ("done", "cancelled") else f"{title} is already {ev.state}.")
        n_aw = sum(1 for s in sessions for a in s.awards if not a.void)
        n_p = sum(len(s.proposals) for s in sessions)
        n_u = sum(len(s.undistributed()) for s in sessions)
        n_r = sum(len(s.pending_reasons) for s in sessions)
        bits = [f"{n_aw} award(s)"] + ([f"{n_p} pick(s) never confirmed"] if n_p else []) + ([f"{n_u} drop(s) never distributed"] if n_u else []) + ([f"{n_r} award(s) waiting for a reason"] if n_r else [])
        return f"end {title}" + (": " + ", ".join(bits) if sessions else ": no loot council is open") + " — the run is marked done" + (" and the council archived with its summary" if sessions else "")
    s = _pick(sessions, ev, idx, ended_ok=f in ("reason", "correct"))
    if f == "tick":
        names = [x.strip() for x in (op.value or "").split(",") if x.strip()]
        if not names:
            raise RegistryError("Name what dropped (value = the item names).")
        words = []
        for n in names:
            it = reg.item(n if n.isdigit() else None, None if n.isdigit() else n)
            words.append(it.name if it is not None and it.tiers else (f"{it.name if it else n} (not tiered yet — recorded by name)"))
        return f"{title}: tick {', '.join(words)}" + (f" from {op.text.strip()}" if (op.text or "").strip() else "")
    if f == "distribute":
        todo = s.undistributed()
        if op.value:
            want = {x.strip().lower() for x in op.value.split(",")}
            todo = [d for d in todo if d.drop_id in want or d.item_name.lower() in want]
        if not todo:
            raise RegistryError("Nothing to distribute — every ticked drop is proposed or awarded.")
        return f"{title}: judge {len(todo)} drop(s) — " + ", ".join(d.item_name for d in todo) + (" (scores only)" if s.scores_only else "")
    if f == "award":
        from .loot.session import check_reason

        if not (op.reason or "").strip():
            raise RegistryError("An award needs a reason — it becomes the precedent the bot cites next time.")
        check_reason(op.reason)
        d = _drop(s, op.value, "open")
        aw = s.awarded(d.drop_id)
        if aw is not None:
            raise RegistryError(f"{d.item_name} is already recorded to {aw.raider} — use correct.")
        to = _who(op.member)
        if not to:
            raise RegistryError("Who gets it? (member = a character, or disenchant)")
        p = s.pending(d.drop_id)
        bot_pick = p.award_to if p else "not judged yet"
        return f"{d.item_name} ({d.drop_id}): bot picked {bot_pick} → council awards {to} — “{op.reason.strip()}” · recorded at Confirm"
    if f == "confirm":
        if (op.value or "").strip().lower() in ALL_WORDS:
            if not s.proposals:
                raise RegistryError("Nothing pending to confirm.")
            return f"{title}: record every pick still pending — " + ", ".join(f"{p.item_name} → {p.to}" + (" (override)" if p.override else "") for p in s.proposals)
        d = _drop(s, op.value, "pending")
        p = s.pending(d.drop_id)
        if p is None:
            aw = s.awarded(d.drop_id)
            raise RegistryError(f"{d.item_name}: already recorded to {aw.raider}." if aw else f"{d.item_name} has no pick yet — distribute it first.")
        return f"{title}: record {d.item_name} ({d.drop_id}) → {p.to}" + (f" (override: “{p.override.reason}”)" if p.override else "")
    if f == "discard":
        if (op.value or "").strip().lower() in ALL_WORDS:
            if not s.proposals:
                raise RegistryError("Nothing pending to discard.")
            n_ov = sum(1 for p in s.proposals if p.override)
            return f"{title}: discard {len(s.proposals)} pending pick(s) — the ticks stay" + (f", {n_ov} pending override(s) dropped" if n_ov else "") + "; nothing was recorded"
        d = _drop(s, op.value, "pending")
        return f"{title}: hold {d.item_name} ({d.drop_id}) — its pick is dropped, the drop waits (decide later)"
    if f in ("reason", "correct"):
        from .loot.session import check_reason

        check_reason(op.reason)
        if isinstance(s, str):
            return f"{s}: {f} for drop {op.value} (the council has ended: the award is found in the ledger)" + (f" → {_who(op.member)}" if f == "correct" else "") + f" — “{op.reason.strip()}”"
        d = _drop(s, op.value, "awarded")
        aw = s.awarded(d.drop_id)
        if aw is None:
            raise RegistryError(f"{d.item_name} isn't recorded yet — award or confirm it instead.")
        if f == "reason":
            return f"precedent: {d.item_name} → {aw.raider} over {aw.bot_pick or '?'} — “{op.reason.strip()}”"
        return f"correct {d.item_name}: recorded to {aw.raider} → {_who(op.member)} — “{op.reason.strip()}” (the old row is voided; no precedent)"
    raise RegistryError(f"loot {f}?")


def _describe_retire(reg: Registry, op) -> str:
    p = _precedent(reg, op.value)
    restore = (op.text or "").strip().lower() == "restore"
    if len((op.reason or "").strip()) < 3:
        raise RegistryError("Give a reason (a few words): it stays on the precedent.")
    return f"precedent {p.get('item')} → {p.get('human')} (over {p.get('bot')}, “{p.get('reason')}”): {p.get('status', 'active')} → {'active' if restore else 'retired'} — “{op.reason.strip()}”"


def _precedent(reg: Registry, ref: str | None, ctx=None) -> dict:
    from .lootctx import RegistryLootContext

    from .lootctx import find_precedent

    ctx = ctx or RegistryLootContext(reg, reg.store, None)
    r = (ref or "").strip()
    hit = find_precedent(ctx.precedents, r)
    if hit is None and r:
        low = r.lower()
        named = [p for p in reversed(ctx.precedents) if str(p.get("item") or "").lower() == low and p.get("status", "active") != "superseded"]
        hit = named[0] if named else None
    if hit is None:
        raise RegistryError(f"No precedent {r or '?'} (give its ref from Loot › Precedents, or the item's name).")
    return hit


def _describe_wish(reg: Registry, op) -> str:
    from .configops import _need_member

    m = _need_member(reg, op.member)
    m, c = reg.wish_character(m.discord_id, op.character)
    f = (op.field or "add").strip().lower()
    if f == "remove":
        ref = (op.value or "").strip()
        if not any(e.name.lower() == ref.lower() or (e.item_id and str(e.item_id) == ref) for e in c.wishlist):
            raise RegistryError(f"{ref} isn't on {c.label}'s wishlist.")
        return f"{c.label}'s wishlist: − {ref}"
    if f != "add":
        raise RegistryError("wishlist field is add or remove")
    ref = (op.value or "").strip()
    iid, nm = reg.wish_item(ref if ref.isdigit() else None, None if ref.isdigit() else ref)
    on = any((e.item_id and e.item_id == iid) or e.name.lower() == nm.lower() for e in c.wishlist)
    if not on and len(c.wishlist) >= WISHLIST_MAX:
        raise RegistryError(f"{c.label}'s wishlist is full ({WISHLIST_MAX}) — remove one first.")
    rank = _rank(op.rank)
    known = reg.item(iid or None, nm) is not None
    return (f"{c.label}'s wishlist: {'move' if on else '+'} {nm}" + (f" to #{rank}" if rank else (" at the end" if not on else ""))
            + (" (OS)" if _offspec(op.text) else " (MS)") + (f" — “{op.reason.strip()[:120]}”" if (op.reason or "").strip() else "")
            + ("" if known else " · not on the tables yet: kept by name"))


def _rank(value) -> int | None:
    v = str(value or "").strip().lstrip("#")
    if not v:
        return None
    if not v.isdigit() or not 1 <= int(v) <= WISHLIST_MAX:
        raise RegistryError(f"rank is a position 1–{WISHLIST_MAX}")
    return int(v)


def _offspec(text) -> bool:
    return str(text or "").strip().lower() in ("os", "offspec", "off-spec", "off spec")


def _describe_tier(reg: Registry, op) -> str:
    from .lootctx import RegistryLootContext

    it = reg.item(op.target, op.target)
    if it is None:
        raise RegistryError(f"No item {op.target or '?'} on the tables or among the learned items.")
    sk = reg._tier_key(op.field or "")
    v = (op.value or "").strip().upper()
    if v not in TIER_VALUES and v not in ("-", ""):
        raise RegistryError(f"a tier is one of {', '.join(TIER_VALUES)} (or - to clear)")
    n = len(RegistryLootContext(reg, reg.store, None).awards_of_item(it))
    re_w = (op.text or "").strip().lower() in ("reweight", "re-weight", "yes")
    return (f"{it.name}: {sk} {it.tiers.get(sk) or '—'} → {v if v not in ('', '-') else '—'}"
            + ((f" · {n} past award(s) re-weighted to the new tier" if re_w else f" · {n} past award(s) keep their weight (say re-weight to change them)") if n else ""))


# ---------------------------------------------------------------- apply

def apply_sync(reg: Registry, op, by: str, ctx=None) -> str:
    """wishlist and tier without the bot (registry writes); `loot` needs the bot (its verbs hold the live council)."""
    if op.op == "wishlist":
        from .configops import _need_member

        m = _need_member(reg, op.member)
        if (op.field or "add").strip().lower() == "remove":
            return reg.wish_remove(m.discord_id, op.character or None, op.value, by=by)
        ref = (op.value or "").strip()
        return reg.wish_add(m.discord_id, op.character or None, int(ref) if ref.isdigit() else ref, by=by, rank=_rank(op.rank),
                            offspec=_offspec(op.text), note=(op.reason or "").strip() or None)
    if op.op == "tier":
        from .lootctx import RegistryLootContext

        it = reg.item(op.target, op.target)
        if it is None:
            raise RegistryError(f"No item {op.target or '?'} on the tables or among the learned items.")
        line, changes = reg.set_item_tiers(op.target, {op.field or "": op.value}, by)
        ctx = ctx or RegistryLootContext(reg, reg.store, None)
        n = len(ctx.awards_of_item(it))
        if changes and n:
            if (op.text or "").strip().lower() in ("reweight", "re-weight", "yes"):
                from .registry import item_key

                done = ctx.reweight(reg.items().get(item_key(it.id, it.name)) or it, by)
                if done:
                    reg.store.commit(f"{reg.key}: {done} award(s) of {it.name} re-weighted ({by})")
                line += f" — re-weighted {done} past award(s)"
            else:
                line += " — past awards kept their weight"
        return line
    raise RegistryError("loot needs the bot (Discord side effects): apply it from /gm change, an @mention or the site, not offline")


async def apply_async(reg: Registry, op, by: str, bot, by_id: int | None, via: str, rev: str | None = None) -> str:
    """The `loot` op through LootMixin (the verb audits; configops.apply_async skips its own line for it)."""
    from .raid_views import run_title

    f = (op.field or "").strip().lower()
    if f not in LOOT_FIELDS:
        raise RegistryError(f"loot field is one of {', '.join(LOOT_FIELDS)}")
    if f == "retire":
        ctx = guild_ctx(bot, reg)
        p = _precedent(reg, op.value, ctx)
        if len((op.reason or "").strip()) < 3:
            raise RegistryError("Give a reason (a few words): it stays on the precedent.")
        status = "active" if (op.text or "").strip().lower() == "restore" else "retired"
        p = ctx.set_precedent_status(p["ref"], status, op.reason.strip()[:300], by)
        await asyncio.to_thread(reg.store.commit, f"{reg.key}: precedent {p['ref']} {status} ({by})")  # never a git commit on the loop
        line = f"precedent {p.get('item')} → {p.get('human')} {'restored' if status == 'active' else 'retired'} — “{op.reason.strip()[:120]}”"
        ops = getattr(bot, "ops", None)
        if ops is not None:
            from . import ops as ops_mod

            await ops_mod.audited(ops, reg, by, line, via=via, by_id=by_id)
        return line
    rs, ev, idx = _run_of(reg, op.target, bot.raids.store(reg))
    kw = {"by": by, "by_id": by_id, "via": via}
    if f == "open":
        out = await bot.open_council(reg, rs, ev, **kw)
        made = any(c for _, c in out)
        return f"loot council for {run_title(reg, ev)} " + ("open" if made else "already open") + (" — thread pending" if any(not s.raid_thread_id for s, _ in out) else "")
    if f == "end":
        return (await bot.end_run(reg, rs, ev, **kw)).replace("**", "")
    sessions = sorted([s for s in bot.events.values() if s.guild == reg.key and s.run_key == ev.key and s.origin == "raid"], key=lambda s: s.roster_index or 0)
    s = _pick(sessions, ev, idx, ended_ok=f in ("reason", "correct"))
    if f == "tick":
        add = []
        for n in [x.strip() for x in (op.value or "").split(",") if x.strip()]:
            add.append({"item_id": int(n), "boss": (op.text or "").strip() or None} if n.isdigit() else {"name": n, "boss": (op.text or "").strip() or None})
        if not add:
            raise RegistryError("Name what dropped (value = the item names).")
        out = await bot.tick(reg, s, add=add, **kw)
        return (out["line"] or "nothing ticked").replace("**", "")
    if f == "distribute":
        only = None
        if op.value:
            want = {x.strip().lower() for x in op.value.split(",")}
            only = [d.drop_id for d in s.drop_list if d.drop_id in want or d.item_name.lower() in want]
        out = await bot.distribute(reg, s, only=only, **kw)
        return (out["line"] or "nothing to judge").replace("**", "")
    if f == "award":
        d = _drop(s, op.value, "open")
        return (await bot.override(reg, s, d.drop_id, _who(op.member), op.reason or "", **kw)).replace("**", "")
    if f == "confirm":
        if (op.value or "").strip().lower() in ALL_WORDS:
            out = await bot.confirm(reg, s, batch_rev=rev if rev and not rev.startswith("to:") else None, **kw)
        else:
            d = _drop(s, op.value, "pending")
            p = s.pending(d.drop_id)
            shown = rev[3:] if rev and rev.startswith("to:") else (p.to if p else None)  # the recipient the diff showed
            out = await bot.confirm(reg, s, only={d.drop_id}, expect_to=shown, **kw)
        return "recorded " + "; ".join(out["lines"])
    if f == "discard":
        if (op.value or "").strip().lower() in ALL_WORDS:
            return await bot.discard(reg, s, batch_rev=rev, **kw)
        d = _drop(s, op.value, "pending")
        return await bot.discard(reg, s, only={d.drop_id}, **kw)
    if f in ("reason", "correct"):
        if isinstance(s, str):
            drop_id = (op.value or "").strip().lower()
            if not re.fullmatch(r"d\d+", drop_id):
                raise RegistryError("That council has ended: name the drop by its id (d3) from Loot › History.")
        else:
            drop_id = _drop(s, op.value, "awarded").drop_id
        if f == "reason":
            return (await bot.set_reason(reg, s, drop_id, op.reason or "", **kw)).replace("**", "")
        return (await bot.correct(reg, s, drop_id, _who(op.member), op.reason or "", **kw)).replace("**", "")
    raise RegistryError(f"loot {f}?")


def member_awards(reg: Registry, ctx, m, limit: int = 50) -> list:
    """A member's live awards, newest first: by Discord id, and by character name for rows recorded without one (a
    sub stored by name who registered later). What My loot shows — never anyone else's."""
    if m is None:
        return []
    labels = {c.label.lower() for c in m.characters}
    rows = [a for a in ctx.ledger if a.discord_id == m.discord_id or (a.discord_id is None and a.raider.lower() in labels)]
    rows.sort(key=lambda a: (a.received, a.confirmed_at or ""), reverse=True)
    return rows[:max(0, int(limit))]


def loot_block(reg: Registry, awards, ico=None) -> str:
    """/me view's Loot block: '<spec icon> Kestrel · Robe of Tests · <t:…:d> · MS' per award (one per line; the date in
    the reader's own locale; no override or reason flags — those are officer data)."""
    from datetime import datetime, time

    out = []
    for a in awards:
        icon = ""
        if ico is not None:
            c = next((c for mm in ([reg.members[a.discord_id]] if a.discord_id in reg.members else reg.members.values())
                      for c in mm.characters if c.label.lower() == a.raider.lower()), None)
            icon = (ico("spec", f"{c.cls}:{c.spec}") + " ") if c is not None and ico("spec", f"{c.cls}:{c.spec}") else ""
        stamp = int(datetime.combine(a.received, time(12), tzinfo=reg.tz).timestamp())
        out.append(f"{icon}{a.raider} · {a.item_name or a.item_id} · <t:{stamp}:d> · {'OS' if a.offspec else 'MS'}")
    return "\n".join(out)[:1000]


def guild_ctx(bot, reg):
    """The bot's cached loot context for the guild (what the council reads), made when there is none yet."""
    contexts = bot.__dict__.setdefault("contexts", {}) if bot is not None else {}
    c = contexts.get(reg.key)
    if c is None or getattr(c, "reg", None) is not reg:
        from .lootctx import RegistryLootContext

        c = RegistryLootContext(reg, reg.store, getattr(bot, "loot_provider", None) or getattr(getattr(bot, "ctx", None), "provider", None))
        if bot is not None:
            contexts[reg.key] = c
    return c


