"""The scheduler loop: every minute, per guild — open sheets on cadence, post the health check + nudges, lock at the
hard cutoff (recovering a lock that never finished), expire confirmations and fill asks, run the fill engine, close.
`RaidSchedulerMixin` sits next to `RaidMixin` on the client (see `raid_views.BotProto` for what it expects) and calls
RaidMixin's methods (open_run_and_post, post_health, lock_run, run_fill, …)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from . import raidcycle as rc
from .raid_views import ask_line, gaps_text, run_times, sheet_state

log = logging.getLogger(__name__)


class RaidSchedulerMixin:
    """The scheduler loop; mixed into OibotGM next to RaidMixin (the client starts `self.scheduler()` on ready)."""

    async def scheduler(self) -> None:
        """Every minute: open sheets on cadence, post health/nudges, lock, fill, expire confirmations, close."""
        await self.wait_until_ready()
        self._last_tick_error: str | None = None
        while not self.is_closed():
            try:
                await self.scheduler_tick()
                self._last_tick_error = None
            except Exception as e:  # noqa: BLE001
                text = f"scheduler tick failed: {type(e).__name__}: {e}"
                log.exception("scheduler tick failed")
                if text != self._last_tick_error:  # the same failure every minute is reported once
                    self._last_tick_error = text
                    cfg = next(iter(self.registries.by_discord.values())).config if self.registries.by_discord else None
                    if cfg:
                        try:
                            await self.ops.emit(cfg, "error", text, e)
                        except Exception:  # noqa: BLE001
                            log.warning("ops emit failed for the scheduler error")
            await asyncio.sleep(60)

    async def scheduler_tick(self) -> None:
        feed = getattr(self, "feed", None)
        raid = next((e for e in self.events.values() if e.state == "raid"), None)
        if feed and raid:
            for c in list(feed.companions.values()):
                if c.silent_for > 300 and not getattr(c, "warned", False):
                    c.warned = True
                    cfg = next(iter(self.registries.by_discord.values())).config if self.registries.by_discord else None
                    if cfg:
                        await self.ops.emit(cfg, "warn", f"companion {c.character} silent for {int(c.silent_for // 60)} min during {raid.id} — is /chatlog on?")
                elif c.silent_for <= 300:
                    c.warned = False
        exhausted_posted: set[str] = self.__dict__.setdefault("_fill_exhausted_posted", set())  # run keys whose "nobody left to ask" line went out
        for reg in self.registries.by_discord.values():
            cfg = reg.config
            rs = self.raids.store(reg)
            channel = self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None
            await self.cleanup_ephemeral(reg, rs)
            now = reg.now_local()
            # open sheets on cadence: every slot occurrence within its raid's signup lead (needs the signup channel)
            if channel:
                for inst in reg.profile.raids:
                    rd = reg.raid_def(inst)
                    for _slot, start in rc.slot_starts(reg, inst, now, float(rd["signup_lead_hours"])):
                        key = f"{rc.run_key(inst, start)}-{start.date().isoformat()}"
                        if key in rs.events:
                            continue
                        await self.open_run_and_post(reg, rs, inst, start, by="scheduler")
            # every live run is processed even when its sheet channel can't be resolved: the steps that post to a
            # channel (refresh_sheet, post_run_update, cards) fail soft on their own
            for ev in rs.live():
                team = rc.run_team(reg, ev)
                officer_ch = self.officer_channel(reg, ev)
                soft, hard, confirm = run_times(reg, ev, team)
                state = sheet_state(ev)
                if state == "open" and not ev.health_posted and now >= soft:
                    await self.post_health(reg, rs, ev, officer_ch, nudge=True)
                    await self.ops.emit(cfg, "info", f"{ev.key}: health check posted, nudged {len(ev.nudged)}")
                if state == "locked" and not ev.all_rosters:  # a lock that never finished (old code path / crash): back to open, retry below
                    ev.state, ev.locked_at, ev.confirm_by = "open", None, None
                    rs.save(ev, "lock recovered")
                    state = "open"
                    await self.ops.emit(cfg, "warn", f"{ev.key}: locked without a roster — reopened, retrying the lock")
                if state == "open" and now >= hard:
                    tried = datetime.fromisoformat(ev.lock_tried_at) if ev.lock_tried_at else None
                    if ev.lock_error and tried and now - tried < timedelta(minutes=15):
                        continue  # failed recently; give the officers time to fix the board before trying again
                    line = await self.lock_run(reg, rs, ev)
                    if not ev.lock_error:
                        await self.ops.emit(cfg, "info", line)
                    continue
                if state != "locked":
                    continue  # the fill engine, confirmations and closing only apply to a locked roster
                gone = rc.expire_confirmations(reg, rs, ev)
                if gone:
                    for name in gone:
                        m = next((mm for mm in reg.members.values() if mm.display_name == name), None)
                        if m:
                            await self.dm_seat_released(reg, ev, m, "no-confirm")
                    await self.post_run_update(reg, ev, f"⌛ no confirmation from {', '.join(gone)} — seats freed")
                    await self.refresh_sheet(reg, ev)
                    await self.refresh_cards(reg, ev)
                    await self.ops.emit(cfg, "warn", f"{ev.key}: confirmations expired for {len(gone)}")
                expired, released = rc.expire_fill_asks(reg, rs, ev)
                if expired:
                    await self.post_run_update(reg, ev, f"⌛ no answer from {', '.join(a.display_name for a in expired)} — counts as no")
                    for a in released:
                        await self.withdraw_ask(reg, ev, a, "the other half of the question timed out")
                    await self.refresh_cards(reg, ev)
                if rc.team_setting(team, "autofill") and ev.fill_state in ("idle", "asking"):
                    sent, nd = await self.run_fill(reg, rs, ev, team)
                    if sent:
                        exhausted_posted.discard(ev.key)
                        await self.post_run_update(reg, ev, f"🧩 short {gaps_text(self.ico, nd)}:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
                    elif ev.fill_state == "exhausted" and ev.key not in exhausted_posted:  # just transitioned: say so once
                        exhausted_posted.add(ev.key)
                        await self.post_run_update(reg, ev, f"🧩 nobody left to ask — short {gaps_text(self.ico, nd)}")
                elif ev.fill_state != "exhausted":
                    exhausted_posted.discard(ev.key)
                if now >= rc.run_close_at(reg, ev):
                    ev.state = "done"
                    rs.save(ev, "done")
                    exhausted_posted.discard(ev.key)
                    await self.refresh_sheet(reg, ev)
                    await self.ops.emit(cfg, "info", f"{ev.key}: closed")
