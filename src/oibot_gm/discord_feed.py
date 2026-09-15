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

from datetime import date
from pathlib import Path
from typing import Any

import discord

from .feed import Companion
from .models import LootAward


class FeedMixin:
    feed = None  # FeedServer, set in setup_hook when OIBOT_FEED_TOKEN is present

    # ---- helpers
    def active_raid(self):
        live = [e for e in self.events.values() if e.state == "raid"]
        return next((e for e in live if e.origin == "raid"), live[0] if live else None)

    async def feed_post(self, ev, text: str, view: discord.ui.View | None = None) -> None:
        ch = self.get_channel(ev.raid_thread_id) if ev.raid_thread_id else None
        if ch:
            await ch.send(text[:1900], view=view)

    def _resolve_item(self, item_id: int):
        raid = self.active_raid()
        profile = self.loot_ctx(raid).profile if raid else self.ctx.profile
        return profile.items.get(int(item_id))

    def _resolve_boss(self, name: str | None, item) -> str:
        raid = self.loot_ctx(self.active_raid()).profile.raids.get(self.active_raid().instance, {}) if self.active_raid() else {}
        if name:
            for b in raid.get("bosses", []):
                if b["name"].lower().startswith(name.lower()[:8]):
                    return b["name"]
        return item.boss if item else (name or "?")

    def _resolve_recipient(self, name: str) -> str:
        """Chat-log names are character names; the ledger uses the same. Match case-insensitively to the roster."""
        ev = self.active_raid()
        if ev and ev.roster:
            for p in ev.roster.selected:
                if (p.character or "").lower() == name.lower() or p.signup_name.lower() == name.lower():
                    return p.character or p.signup_name
        return name

    # ---- entry point
    async def handle_feed_event(self, ev: dict[str, Any], comp: Companion) -> dict[str, Any] | None:
        kind = ev.get("type")
        raid = self.active_raid()
        cfg = next(iter(self.registries.by_discord.values())).config if self.registries.by_discord else None
        if kind == "hello":
            if cfg:
                await self.ops.emit(cfg, "info", f"companion connected: {comp.character} ({comp.client}) → {raid.id if raid else 'no active raid'}")
            return {"routes_to": raid.id if raid else None}
        if kind == "heartbeat":
            return None
        if raid is None:
            return {"note": "no active raid; event ignored"}
        from . import discord_bot as db

        ctx = self.loot_ctx(raid)

        if kind == "drop":
            ticked = []
            for it in ev.get("items", []):
                item = self._resolve_item(it.get("id"))
                if not item:
                    continue
                boss = self._resolve_boss(ev.get("boss"), item)
                lst = raid.drops.setdefault(boss, [])
                if item.id not in lst:
                    lst.append(item.id)
                    ticked.append(item.name)
            if ticked:
                raid.save(f"{raid.id}: companion drops {len(ticked)}")
                await self.feed_post(raid, f"📥 {comp.character}'s log: dropped from **{self._resolve_boss(ev.get('boss'), None)}** — " + ", ".join(ticked), view=db.DistributeView(self, raid))
            return {"ticked": len(ticked)}
        if kind == "kill":
            boss = self._resolve_boss(ev.get("boss"), None)
            if boss not in raid.bosses_done:
                raid.bosses_done.append(boss)
                raid.save(f"{raid.id}: {boss} down")
            await self.feed_post(raid, f"☠ **{boss}** down ({ev.get('ts', '')[11:16]})")
            return {"boss": boss}
        if kind == "loot":
            item = self._resolve_item(ev.get("item_id"))
            if not item:
                return {"note": "unknown item"}
            who = self._resolve_recipient(ev.get("recipient", "?"))
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
