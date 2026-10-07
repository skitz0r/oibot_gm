"""Bot-side handling of companion events (see feed.py for the protocol).

Routes to the active raid (today: the mock event in `raid` state) and:
  drop  → tick the items on that boss's table, offer Distribute
  loot  → matches a proposal: confirm it; differs: award to the in-game recipient as an
          override with the reason pending (next reply in the thread records it);
          no proposal: record a manual award
  kill  → mark the boss done
  hello/heartbeat → presence; the scheduler warns when a companion goes silent mid-raid
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import discord

from .feed import Companion
from .models import LootAward

log = logging.getLogger(__name__)


def feed_status(bot, reg, off: str = "disabled (OIBOT_FEED_TOKEN unset)") -> str:
    """The companions that feed THIS guild (FeedServer.status filtered by the companion's routed guild): one whose hello
    named no guild we serve counts for the only guild, never for one of several. With several guilds a companion's
    `guild` is its token's guild (Companion sets it from `bound`), and a shared-token one counts for none."""
    feed = getattr(bot, "feed", None)
    if feed is None:
        return off
    registries = getattr(bot, "registries", None)
    only = registries.only() if registries is not None and hasattr(registries, "only") else reg
    key_of = getattr(bot, "companion_key", None)
    many = getattr(registries, "many", False)

    def resolve(c) -> str | None:
        if many:
            return getattr(c, "bound", None)  # only a guild's own token feeds it
        g = c.guild
        k = key_of(g) if key_of else (g or None)
        return k or (reg.key if only is reg else None)
    return feed.status(reg.key, resolve=resolve)


class FeedMixin:
    feed = None  # FeedServer, set in setup_hook when OIBOT_FEED_TOKEN is present

    # ---- routing (design §5.32): each companion feeds the live session of ITS guild
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

    def active_raid(self, comp: Companion | None = None):
        """The live loot session a companion's events go to. One guild served (or none): today's rule — a real raid
        first, else any live session. Several: the guild its TOKEN belongs to (`comp.bound`, OIBOT_FEED_TOKEN_<KEY>;
        what its hello names never decides) — that guild's real session, else a mock session started in that guild's
        server; the shared token (bound to no guild) reaches mock sessions only, never a real raid: no guessing, and a
        token handed to one guild's raid leader can't feed another guild."""
        live = [e for e in self.events.values() if e.state == "raid"]
        if not live:
            return None
        today = next((e for e in live if e.origin == "raid"), live[0])
        registries = self.registries
        if not getattr(registries, "many", False):
            return today
        real = [e for e in live if e.origin == "raid"]
        mock = [e for e in live if e.origin != "raid"]
        want = getattr(comp, "bound", None) if comp is not None else None
        if want:
            return (next((e for e in real if e.guild == want), None) or next((e for e in mock if e.guild == want), None)
                    or next((e for e in mock if self._mock_in_server_of(e, want)), None))
        return mock[0] if mock else None

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

    async def feed_post(self, ev, text: str, view: discord.ui.View | None = None) -> None:
        ch = self.get_channel(ev.raid_thread_id) if ev.raid_thread_id else None
        if ch:
            await ch.send(text[:1900], view=view)

    def _resolve_item(self, item_id: int, raid=None):
        profile = self.loot_ctx(raid).profile if raid else self.ctx.profile
        return profile.items.get(int(item_id))

    def _resolve_boss(self, name: str | None, item, raid=None) -> str:
        bosses = self.loot_ctx(raid).profile.raids.get(raid.instance, {}) if raid else {}
        if name:
            for b in bosses.get("bosses", []):
                if b["name"].lower().startswith(name.lower()[:8]):
                    return b["name"]
        return item.boss if item else (name or "?")

    def _resolve_recipient(self, name: str, raid=None) -> str:
        """Chat-log names are character names; the ledger uses the same. Match case-insensitively to the roster."""
        ev = raid
        if ev and ev.roster:
            for p in ev.roster.selected:
                if (p.character or "").lower() == name.lower() or p.signup_name.lower() == name.lower():
                    return p.character or p.signup_name
        return name

    # ---- entry point
    async def handle_feed_event(self, ev: dict[str, Any], comp: Companion) -> dict[str, Any] | None:
        kind = ev.get("type")
        raid = self.active_raid(comp)
        many = getattr(self.registries, "many", False)
        mine = getattr(comp, "bound", None) if many else self.companion_key(comp.guild)  # several: only its token names a guild
        cfg = self.registries.cfg_for(raid.guild if raid else mine)  # None: the log only
        if kind == "hello":
            note = self.feed_note(comp)
            if cfg:
                await self.ops.emit(cfg, "info", f"companion connected: {comp.character} ({comp.client}) → {raid.id if raid else 'no active raid'}" + (f" · {note}" if note else ""))
            else:
                log.info("companion connected: %s (%s) → %s%s", comp.character, comp.client, raid.id if raid else "no active raid", f" · {note}" if note else "")
            return {"routes_to": raid.id if raid else None, **({"note": note} if note else {})}
        if kind == "heartbeat":
            return None
        if raid is None:
            return {"note": "no active raid; event ignored"}
        from . import discord_bot as db

        ctx = self.loot_ctx(raid)

        if kind == "drop":
            ticked = []
            learned = []
            for it in ev.get("items", []):
                item = self._resolve_item(it.get("id"), raid)
                if not item:
                    # unknown to the profile: remember boss → item so it can be onboarded (`oibot items add`)
                    if it.get("id"):
                        db._store().append_jsonl(Path(raid.guild) / "items_learned.jsonl", {"id": int(it["id"]), "name": it.get("name"), "boss": ev.get("boss"), "instance": raid.instance, "event": raid.id, "ts": ev.get("ts")})
                        learned.append(f"{it.get('name') or it['id']} ({it['id']})")
                    continue
                boss = self._resolve_boss(ev.get("boss"), item, raid)
                lst = raid.drops.setdefault(boss, [])
                if item.id not in lst:
                    lst.append(item.id)
                    ticked.append(item.name)
            if ticked:
                raid.save(f"{raid.id}: companion drops {len(ticked)}")
                await self.feed_post(raid, f"📥 {comp.character}'s log: dropped from **{self._resolve_boss(ev.get('boss'), None, raid)}** — " + ", ".join(ticked), view=db.DistributeView(self, raid))
            if learned:
                db._store().commit(f"{raid.guild}: learned {len(learned)} item(s) from {ev.get('boss') or 'unknown boss'}")
                await self.feed_post(raid, f"🆕 Not in the item table yet (recorded for onboarding from **{ev.get('boss') or '?'}**): " + ", ".join(learned) + ". An officer can run `oibot items add` to pull their facts from Blizzard.")
            return {"ticked": len(ticked), "learned": len(learned)}
        if kind == "kill":
            boss = self._resolve_boss(ev.get("boss"), None, raid)
            if boss not in raid.bosses_done:
                raid.bosses_done.append(boss)
                raid.save(f"{raid.id}: {boss} down")
            await self.feed_post(raid, f"☠ **{boss}** down ({ev.get('ts', '')[11:16]})")
            return {"boss": boss}
        if kind == "loot":
            item = self._resolve_item(ev.get("item_id"), raid)
            if not item:
                return {"note": "unknown item"}
            who = self._resolve_recipient(ev.get("recipient", "?"), raid)
            prop = next((p for p in raid.proposals if p.item_id == item.id), None)
            if prop and prop.award_to == who:
                db.confirm_awards(ctx, raid, only={item.id})
                await self.feed_post(raid, f"✅ **{item.name}** → **{who}** (matches the proposal; recorded)")
                return {"result": "confirmed"}
            if prop:
                bot_pick = prop.result.recommendation.primary
                prop.award_to, prop.source, prop.reason = who, "override", "(reason pending)"
                raid.pending_reasons.append({"item_id": item.id, "item": item.name, "bot": bot_pick, "human": who})
                db.confirm_awards(ctx, raid, only={item.id})
                await self.feed_post(raid, f"⚠ **{item.name}** → **{who}** in game, but the bot proposed **{bot_pick}**. Recorded as an override; **reply here with the reason** so it becomes a precedent.")
                return {"result": "override_pending_reason"}
            # no proposal: manual award straight to the ledger
            cands = self._candidates_for(raid, item)
            c = next((c for c in cands if c.character == who), None)
            award = LootAward(raider=who, item_id=item.id, tier=c.tier if c else "?", total_weight=c.upgrade_value if c else 0.5, offspec=bool(c and c.offspec), received=date.fromisoformat(raid.date), instance=raid.raid_name, boss=item.boss)
            raid.awards.append(award)
            ctx.ledger.append(award)
            db._store().append_jsonl(Path(raid.guild) / "ledger.jsonl", {**award.model_dump(), "event": raid.id, "source": "manual", "bot_pick": None, "import_id": f"{raid.id}-{item.id}-{who}"})
            raid.drops.setdefault(item.boss, [])
            if item.id not in raid.drops[item.boss]:
                raid.drops[item.boss].append(item.id)
            if item.id not in raid.distributed:
                raid.distributed.append(item.id)
            raid.save(f"{raid.id}: manual award {item.name} → {who}")
            await self.feed_post(raid, f"📒 **{item.name}** → **{who}** (awarded in game without a proposal; recorded as manual)")
            return {"result": "manual"}
        return {"note": f"unknown event type {kind}"}

    def _candidates_for(self, raid, item):
        from .loot import scoring

        ctx = self.loot_ctx(raid)
        return scoring.candidates(ctx.profile, item, raid.roster.selected if raid.roster else [], ctx.ledger + raid.awards, ctx.wishlists, date.fromisoformat(raid.date))

    async def record_pending_reason(self, raid, text: str, by: str) -> str | None:
        """A plain reply in the raid thread while an override awaits its reason."""
        if not raid.pending_reasons:
            return None
        pr = raid.pending_reasons.pop(0)
        from . import discord_bot as db

        precedent = {"date": raid.date, "event": raid.id, "item": pr["item"], "item_id": pr["item_id"], "bot": pr["bot"], "human": pr["human"], "reason": text.strip(), "by": by, "status": "active"}
        raid.overrides.append(precedent)
        self.loot_ctx(raid).precedents.append(precedent)
        db._store().append_jsonl(Path(raid.guild) / "precedents.jsonl", precedent)
        raid.save(f"{raid.id}: precedent {pr['item']} → {pr['human']}")
        return f"📌 Precedent recorded: {pr['item']} → {pr['human']} over {pr['bot']} — “{text.strip()}”"
