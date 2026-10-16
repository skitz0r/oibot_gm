"""Bot-side handling of companion events (see feed.py for the protocol), through the loot council's verbs.

Routing (design §3.1 L10/L11): within the guild the companion's TOKEN belongs to (several guilds) or the one guild
served — exactly one live non-test council → there; a test council only from a companion playing a puppet or the
tester; several live → the one whose roster seats the companion's character; none or several still → refused (an ops
warn and an error ack the companion prints). No live run at all → the event is held 10 minutes (LootMixin.feed_buffer,
replayed when a council opens) and the guild's 'no run live' state shows it.

  drop  → tick (unknown items by name, learned) + auto-distribute ~20 s after the boss's last drop (per raid switch)
  loot  → matched to ONE copy of the item under the session lock: the open copy whose pick names the recipient →
          confirm; a copy an officer already recorded to them → already recorded; else the first open copy: someone
          else → the ledger row at once, precedent pending a reason (Give the reason / Mistake — correct…), no
          proposal → recorded as a manual award; every copy recorded to others → a conflict line (Correct?)
  kill  → the boss is down (and starts the auto-distribute wait)
  hello/heartbeat → presence (live / quiet / disconnected, feed_presence)
Idempotency: the session keeps the companion's idem keys (`seen_idem`), so a re-send after a reconnect is acked, not
applied twice; every ack names the session (`session`: "<guild>:<id>")."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .feed import Companion
from .loot import session as ls

log = logging.getLogger(__name__)


def _routing_key(bot, reg, c) -> str | None:
    """The guild a connected companion counts for: several guilds → its token's guild only; one → what it named, or
    the only guild."""
    registries = getattr(bot, "registries", None)
    many = getattr(registries, "many", False)
    if many:
        return getattr(c, "bound", None)
    only = registries.only() if registries is not None and hasattr(registries, "only") else reg
    key_of = getattr(bot, "companion_key", None)
    g = c.guild
    k = key_of(g) if key_of else (g or None)
    return k or (reg.key if only is reg else None)


def feed_status(bot, reg, off: str = "disabled (OIBOT_FEED_TOKEN unset)") -> str:
    """The companions that feed THIS guild as one line (FeedServer.status filtered by the companion's routed guild):
    one whose hello named no guild we serve counts for the only guild, never for one of several. With several guilds a
    companion's `guild` is its token's guild (Companion sets it from `bound`), and a shared-token one counts for none.
    The structured form (three presence states) is `feed_presence`."""
    feed = getattr(bot, "feed", None)
    if feed is None:
        return off
    return feed.status(reg.key, resolve=lambda c: _routing_key(bot, reg, c))


def feed_presence(bot, reg) -> list[dict]:
    """This guild's companions as rows: {character, client, state: live|quiet|disconnected, last_seen (ISO),
    last_seen_label (12-hour guild time), events, routes_to} — the loot card's chip and the site's header."""
    feed = getattr(bot, "feed", None)
    if feed is None:
        return []
    rows = feed.presence(reg.key, resolve=lambda c: _routing_key(bot, reg, c))
    for r in rows:
        try:
            r["last_seen_label"] = reg.local12(r["last_seen"], "%I:%M %p")
        except Exception:  # noqa: BLE001
            r["last_seen_label"] = ""
    return rows


class FeedMixin:
    feed = None  # FeedServer, set in setup_hook when OIBOT_FEED_TOKEN is present

    # ---- routing (design §5.32 + §3.1): each companion feeds a live council of ITS guild
    def companion_key(self, ref: str | None) -> str | None:
        """The guild a companion's hello named (`--guild`: a key, or a unique name; or the mock fixtures' key), or None
        when it named none we serve — an unknown value counts as empty (a stale $OIBOT_GUILD on the Windows box)."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        reg = self.registries.get(ref) if hasattr(self.registries, "get") else None
        if reg is not None:
            return reg.key
        mock = getattr(getattr(self, "ctx", None), "guild_key", None)
        return ref if mock and ref == mock else None

    def companion_guild(self, comp: Companion | None) -> str | None:
        """The guild key a companion feeds: several guilds → its token's (`bound`; what its hello names never decides);
        one guild → what it named, else the only guild."""
        registries = self.registries
        if getattr(registries, "many", False):
            return getattr(comp, "bound", None) if comp is not None else None
        named = self.companion_key(comp.guild) if comp is not None else None
        if named:
            return named
        only = registries.only() if hasattr(registries, "only") else None
        return only.key if only is not None else None

    def _is_tester_companion(self, reg, comp: Companion) -> bool:
        """A companion playing a puppet (Member.test) or the tester of a live test run."""
        if reg is None or comp is None:
            return False
        name = str(comp.character or "").strip().lower()
        if not name:
            return False
        testers = {int(t.get("test_by")) for t in reg.config.rosters if t.get("test") and t.get("test_by")}
        for m in reg.members.values():
            if not (getattr(m, "test", False) or m.discord_id in testers):
                continue
            if any(c.matches(name) for c in m.characters) or m.display_name.lower() == name:
                return True
        return False

    def _seats_character(self, session, name: str) -> bool:
        low = str(name or "").strip().lower()
        return bool(low) and any((p.character or "").lower() == low or p.signup_name.lower() == low for p in self.loot_roster(session))

    def loot_route(self, comp: Companion | None):
        """(session, None) or (None, why) — why is 'no run live' (the event may be held) or a refusal to show."""
        gk = self.companion_guild(comp)
        live = [s for s in self.events.values() if self.loot_live(s)]
        real = [s for s in live if s.origin == "raid" and gk and s.guild == gk]
        reg = (getattr(self.registries, "by_key", {}) or {}).get(gk) if gk else None
        if real:
            # routing by run window first (design §3.1): a council kept open past its run's close (picks still waiting)
            # never takes a companion event — not even when it is the only one live: tonight's events wait (held) for
            # tonight's council instead of landing in last week's run
            real = [s for s in real if not self._past_close(reg, s)]
            if not real:
                return None, "no run live"
            tester = self._is_tester_companion(reg, comp)
            pool = [s for s in real if s.test] if tester else [s for s in real if not s.test]
            if tester and not pool:
                pool = [s for s in real if not s.test and self._seats_character(s, comp.character)]
            if len(pool) == 1:
                return pool[0], None
            if len(pool) > 1:
                hit = [s for s in pool if self._seats_character(s, comp.character)]
                if len(hit) == 1:
                    return hit[0], None
                return None, (f"several loot councils are live and {comp.character} is rostered on " + ("none of them" if not hit else "more than one")
                              + " — bind it on the council card")
            if not pool:
                return None, ("only a test council is live; a rehearsal takes drops from a puppet's or the tester's companion only"
                              if not tester else "no test council is live")
        mock = [s for s in live if s.origin != "raid"]
        if mock and (not gk or not any(s.guild == gk for s in self.events.values() if s.origin == "raid")):
            want = getattr(comp, "bound", None) if comp is not None else None
            if getattr(self.registries, "many", False) and want:
                mine = [s for s in mock if s.guild == want or self._mock_in_server_of(s, want)]
                if mine:
                    return mine[0], None
            elif not getattr(self.registries, "many", False) or not want:
                return mock[0], None
        return None, "no run live"

    def _past_close(self, reg, session) -> bool:
        if reg is None:
            return False
        _, ev = self.loot_run(session)
        if ev is None:
            return False
        from . import raidcycle as rc

        return datetime.now(ev.start.tzinfo) >= rc.run_close_at(reg, ev)

    def active_raid(self, comp: Companion | None = None):
        """The council a companion's events go to now (None: nowhere) — loot_route without the reason."""
        return self.loot_route(comp)[0]

    def _mock_in_server_of(self, ev, key: str) -> bool:
        """A mock session whose thread is in guild `key`'s server (or whose server can't be told: the demo flow)."""
        get = getattr(self, "get_channel", None)
        ch = get(ev.channel_id) if get and getattr(ev, "channel_id", None) else None
        sid = getattr(getattr(ch, "guild", None), "id", None)
        reg = self.registries.by_key.get(key) if hasattr(self.registries, "by_key") else None
        return sid is None or (reg is not None and sid == reg.config.discord_guild_id)

    def feed_note(self, comp: Companion) -> str | None:
        """Why a companion routes nowhere (or ignores what it named) — the hello reply's `note`."""
        bound, named = getattr(comp, "bound", None), getattr(comp, "named", comp.guild)
        if getattr(self.registries, "many", False):
            if not bound:
                return ("several guilds are served: a companion feeds the guild its token belongs to — "
                        "start it with that guild's own token (OIBOT_FEED_TOKEN_<KEY> on the bot)")
            if named and self.companion_key(named) not in (None, bound):
                return f"this token feeds {bound}; --guild {named!r} is ignored"
            return None
        if comp.guild and self.companion_key(comp.guild) is None:
            return f"guild {comp.guild!r} isn't one I serve; routing as if none was given"
        return None

    async def feed_post(self, ev, text: str, view=None) -> None:
        await self.loot_post(ev, text, view=view)

    def _resolve_boss(self, name: str | None, item, raid=None) -> str:
        """A boss as the companion named it, matched to the raid's boss list when there is one (8-letter prefix)."""
        reg = self.loot_reg(raid) if raid is not None else None
        rd = reg.raid_def(raid.instance) if reg is not None else (self.loot_ctx(raid).profile.raids.get(raid.instance, {}) if raid is not None else {})
        if name:
            for b in rd.get("bosses", []) or []:
                if b["name"].lower().startswith(name.lower()[:8]):
                    return b["name"]
            return name
        return item.boss if item is not None and item.boss not in ("", "?") else "?"

    # ---- entry point
    async def handle_feed_event(self, ev: dict[str, Any], comp: Companion) -> dict[str, Any] | None:
        kind = ev.get("type")
        raid, why = self.loot_route(comp)
        gk = raid.guild if raid is not None else self.companion_guild(comp)
        cfg = self.registries.cfg_for(gk) if gk else None  # None: the log only
        reg = (getattr(self.registries, "by_key", {}) or {}).get(raid.guild) if raid is not None else ((getattr(self.registries, "by_key", {}) or {}).get(gk) if gk else None)
        skey = f"{raid.guild}:{raid.id}" if raid is not None else None
        if kind == "hello":
            note = self.feed_note(comp)
            text = f"companion connected: {comp.character} ({comp.client}) → {raid.id if raid else 'no live council'}" + (f" · {note}" if note else "")
            if cfg:
                await self.ops.emit(cfg, "info", text)
            else:
                log.info(text)
            if raid is not None and getattr(comp, "was_disconnected", False):
                await self.loot_notify(reg, raid, f"🔌 {comp.character}'s companion is back", kind="presence")
            return {"routes_to": raid.id if raid else None, "session": skey, **({"note": note} if note else {})}
        if kind == "heartbeat":
            return None
        if raid is None:
            if why == "no run live":
                if gk and kind in ("drop", "loot", "kill"):
                    self.feed_buffer(gk, ev, comp)  # persisted: survives a restart; what expires unrecorded is announced
                    return {"note": "no run live; held for 10 minutes — open the loot council (/raid loot), or the ops channel lists it as not recorded", "held": True}
                return {"note": "no active raid; event ignored"}
            if cfg and self._feed_warn_due(gk, comp.character, why):  # the companion retries a refusal: one warn per 10 min
                await self.ops.emit(cfg, "warn", f"companion {comp.character}: {why}")
            return {"error": why}
        if not raid.remember_idem(ev.get("idem")):
            return {"dup": True, "session": skey}
        by = f"companion:{comp.character}"
        try:
            if kind == "drop":
                out = await self._feed_drop(reg, raid, ev, comp, by)
            elif kind == "kill":
                out = await self._feed_kill(reg, raid, ev)
            elif kind == "loot":
                out = await self._feed_loot(reg, raid, ev, comp, by)
            else:
                out = {"note": f"unknown event type {kind}"}
        except Exception:
            raid.seen_idem = [x for x in raid.seen_idem if x != ev.get("idem")]  # not applied: a re-send may try again
            raise
        return {**out, "session": skey}

    def _feed_warn_due(self, gk, character, why: str, every_s: float = 600) -> bool:
        seen = self.__dict__.setdefault("_feed_warned", {})
        key, now = (gk, character, why), datetime.now().timestamp()
        if now - seen.get(key, 0) < every_s:
            return False
        seen[key] = now
        return True

    async def _feed_drop(self, reg, raid, ev, comp, by) -> dict:
        boss = self._resolve_boss(ev.get("boss"), None, raid)
        add = [{"item_id": it.get("id"), "name": it.get("name"), "boss": boss} for it in ev.get("items", []) if it.get("id") or it.get("name")]
        if not add:
            raid.save(f"{raid.guild}: loot {raid.id}: companion event", touch=False)
            return {"ticked": 0}
        res = await self.tick(reg, raid, add=add, by=by, source="companion", raw=ev.get("line"))
        learned = [d for d in res["added"] if not d.tiered]
        if learned:
            await self.loot_notify(reg, raid, "🆕 " + ", ".join(d.item_name for d in learned) + " isn't tiered yet — recorded by name, scored the same for everyone; tier it on Loot › Items.", kind="untiered")
        self.loot_auto_soon(reg, raid, boss)
        return {"ticked": len(res["added"]), "learned": len(learned)}

    async def _feed_kill(self, reg, raid, ev) -> dict:
        boss = self._resolve_boss(ev.get("boss"), None, raid)
        async with self.loot_lock(*raid.key):
            self.loot_check_live(raid)
            if boss not in raid.bosses_done:
                raid.bosses_done.append(boss)
                raid.current_boss = boss
            raid.save(f"{raid.guild}: loot {raid.id}: {boss} down")
        await self.loot_notify(reg, raid, f"☠ **{boss}** down" + (f" ({_log_clock(ev['ts'])})" if ev.get("ts") else ""), kind="kill")
        self.loot_auto_soon(reg, raid, boss)
        return {"boss": boss}

    async def _feed_loot(self, reg, raid, ev, comp, by) -> dict:
        """An in-game award, matched to ONE copy of the item — the choice and the write in one section under the
        session lock (two copies of a token in one kill is ordinary loot, and the master looter hands them out in any
        order):
          1. an open copy whose pending pick names the recipient (the effective recipient; or the bot's pick under an
             override the master looter didn't follow — the override is dropped) → confirmed;
          2. a copy already recorded to the recipient that no in-game award has matched yet (an officer confirmed it
             first) → already recorded (the copy is marked seen);
          3. the first open copy (one with a pending pick first) → a mismatch (someone else got it: recorded, reason
             pending) or, with no pick, a manual award;
          4. every copy recorded: to the recipient → already recorded; else a conflict on the copy the game has not
             matched yet.
        An item never ticked is ticked first (the drop is game truth), then matched."""
        ctx = self.loot_ctx(raid)
        iid = int(ev.get("item_id") or 0)
        name = str(ev.get("item_name") or "").strip()
        item = ctx.item(iid or None, name or None)

        def same(d) -> bool:
            if iid and d.item_id:
                return d.item_id == iid
            n = (item.name if item is not None else name).lower()
            return bool(n) and d.item_name.lower() == n
        out = None
        for attempt in range(2):
            async with self.loot_lock(*raid.key):
                self.loot_check_live(raid)
                drops = [d for d in raid.drop_list if same(d)]
                if drops or attempt:
                    roster = self.loot_roster(raid)
                    who, _uid, _seat = ls.recipient_of(reg, roster, ev.get("recipient", "?"))
                    out = self._feed_match(reg, raid, drops, who, ctx, roster, comp, by)
                    break
            # never ticked: tick it now (the tick verb: learned item, thread line, card), then match under the lock again
            await self.tick(reg, raid, add=[{"item_id": iid or None, "name": name or None, "boss": raid.current_boss}], by=by, source="companion")
        res, d, who = out["result"], out.get("drop"), out.get("who")
        if res == "confirmed":
            await self._after_record(reg, raid, out["awards"], by, None, "discord")
        elif res == "conflict":
            await self.loot_notify(reg, raid, f"⚠ **{d.item_name}**: in game it went to **{who}**, recorded earlier to **{out['recorded']}** — Correct?", kind="conflict", drop_id=d.drop_id)
        elif res == "recorded_reason_pending":
            bot = out["bot"]
            await self._audit(reg, by, f"loot {raid.run_key or raid.id}: recorded {d.item_name} → {who} in game (bot picked {bot}); reason pending", "discord", None)
            await self.loot_notify(reg, raid, f"⚠ **{d.item_name}** → **{who}** in game, but the bot proposed **{bot}**. Recorded — give the reason so it becomes a precedent (or correct it if it was a mistake).", kind="mismatch", drop_id=d.drop_id)
        elif res == "manual":
            await self._audit(reg, by, f"loot {raid.run_key or raid.id}: recorded {d.item_name} → {who} (in game, no proposal)", "discord", None)
            await self.loot_notify(reg, raid, f"📒 **{d.item_name}** → **{who}** · recorded — wrong? Correct…", kind="manual", drop_id=d.drop_id)
        return {"result": res}

    def _feed_match(self, reg, raid, drops, who: str, ctx, roster, comp, by: str) -> dict:
        """_feed_loot's choice and write (the caller holds the session lock): {result, drop, who, …}."""
        from .discord_loot import NOBODY

        low = who.lower()
        open_ = [d for d in drops if not raid.awarded(d.drop_id)]
        hit = next((d for d in open_ if (p := raid.pending(d.drop_id)) is not None and p.to != NOBODY and p.to.lower() == low), None) or next(
            (d for d in open_ if (p := raid.pending(d.drop_id)) is not None and p.override and p.award_to.lower() == low), None)
        if hit is not None:  # 1. the pick the council made (or the bot's, an override not followed) — confirmed
            prop = raid.pending(hit.drop_id)
            if prop.override and prop.award_to.lower() == low:
                prop.override = None  # the master looter gave it to the bot's pick after all: no override happened, no precedent
            hit.seen_in_game = True
            return {"result": "confirmed", "drop": hit, "who": who, "awards": self._record(reg, raid, [prop], by, None)}
        mine = next((d for d in drops if (a := raid.awarded(d.drop_id)) is not None and a.raider.lower() == low and not d.seen_in_game), None)
        if mine is not None:  # 2. an officer recorded this copy to them before the game did
            mine.seen_in_game = True
            raid.save(f"{raid.guild}: loot {raid.id}: {mine.item_name} → {who} seen in game (already recorded)")
            return {"result": "already_recorded", "drop": mine, "who": who}
        target = next((d for d in open_ if raid.pending(d.drop_id)), None) or (open_[0] if open_ else None)
        if target is None:  # 4. every copy is recorded already
            if any((a := raid.awarded(d.drop_id)) is not None and a.raider.lower() == low for d in drops):
                return {"result": "already_recorded", "drop": None, "who": who}
            d = next((d for d in reversed(drops) if not d.seen_in_game), drops[-1])
            a = raid.awarded(d.drop_id)
            d.seen_in_game = True
            raid.conflicts.append({"drop_id": d.drop_id, "item": d.item_name, "in_game": who, "recorded": a.raider, "at": ls.now_iso()})
            raid.save(f"{raid.guild}: loot {raid.id}: {d.item_name} went to {who} in game, recorded to {a.raider}")
            return {"result": "conflict", "drop": d, "who": who, "recorded": a.raider}
        target.seen_in_game = True
        prop = raid.pending(target.drop_id)
        if prop is not None:  # 3a. someone neither the bot nor a pending override named: recorded now, the reason pending
            a = ls.make_award(ctx, raid, target, who, roster, source="companion", by=by, cands=prop.result.candidates, bot_pick=prop.award_to, reg=reg)
            with raid.store().batch(f"{raid.guild}: loot {raid.id}: {target.item_name} → {who} in game (bot: {prop.award_to})"):
                raid.awards.append(a)
                raid.proposals = [x for x in raid.proposals if x.drop_id != target.drop_id]
                raid.pending_reasons.append({"drop_id": target.drop_id, "item": target.item_name, "recipient": who, "bot": prop.award_to,
                                             "override": prop.override.model_dump() if prop.override else None, "import_id": a.import_id, "at": ls.now_iso()})
                raid.note_change(by, f"{target.item_name} went to {who} in game (the bot picked {prop.award_to})")
                raid.save(f"{raid.guild}: loot {raid.id}: in-game award {target.item_name} → {who}, reason pending")
                if not raid.test:
                    ls.sync_ledger(ctx, raid)
            return {"result": "recorded_reason_pending", "drop": target, "who": who, "bot": prop.award_to}
        # 3b. no proposal: record first, correct second (the Dec 9 common case)
        a = ls.make_award(ctx, raid, target, who, roster, source="manual", by=by, reg=reg)
        with raid.store().batch(f"{raid.guild}: loot {raid.id}: {target.item_name} → {who} (in game, no proposal)"):
            raid.awards.append(a)
            raid.note_change(by, f"{comp.character}'s log recorded {target.item_name} → {who}")
            raid.save(f"{raid.guild}: loot {raid.id}: manual award {target.item_name} → {who}")
            if not raid.test:
                ls.sync_ledger(ctx, raid)
        return {"result": "manual", "drop": target, "who": who}

    async def feed_disconnected(self, comp: Companion) -> None:
        """A companion's socket closed: one line on the council it fed (and one more when it reconnects)."""
        raid, _ = self.loot_route(comp)
        if raid is None:
            return
        await self.loot_notify(self.loot_reg(raid), raid, f"🔌 {comp.character}'s companion disconnected", kind="presence")


def _log_clock(ts) -> str:
    """The companion's stamp as a person reads it: epoch seconds → a native Discord stamp (the reader's own clock); an
    ISO chat-log stamp (the raider's PC clock, no zone) → '8:15 PM', never '20:15'."""
    from .constants import fmt12

    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return f"<t:{int(ts)}:t>"
    try:
        return fmt12(datetime.fromisoformat(str(ts)), "%I:%M %p")
    except ValueError:
        return str(ts)[11:16]

