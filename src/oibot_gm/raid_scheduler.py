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
from .registry import RegistryError

log = logging.getLogger(__name__)


class RaidSchedulerMixin:
    """The scheduler loop; mixed into OibotGM next to RaidMixin (the client starts `self.scheduler()` on ready)."""

    async def scheduler(self) -> None:
        """Every minute: open sheets on cadence, post health/nudges, lock, fill, expire confirmations, close — for every
        guild (scheduler_tick never raises: each guild runs in its own try)."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.scheduler_tick()
            except Exception:  # noqa: BLE001 — belt and braces: the loop itself never dies
                log.exception("scheduler tick failed outside the guild loop")
            await asyncio.sleep(60)

    def _tick_errors(self) -> dict[str, str]:
        """The last failure text per guild key ('' = process-wide): the same failure every minute is reported once."""
        return self.__dict__.setdefault("_tick_error_by_guild", {})

    async def _tick_failed(self, reg, e: BaseException) -> None:
        """A guild's tick failed: report it to THAT guild (ops line + its error targets' DM), once per distinct text. A
        failure outside the guild loop (reg None) goes to every guild's ops channel; each distinct error target (each
        guild's error_dm_ids, default its owner) is DMed once."""
        text = f"scheduler tick failed: {type(e).__name__}: {e}"
        log.exception("scheduler tick failed for %s", reg.key if reg is not None else "the process", exc_info=e)
        errors = self._tick_errors()
        slot = reg.key if reg is not None else ""
        if errors.get(slot) == text:
            return
        errors[slot] = text
        from .ops import error_targets

        targets = [reg] if reg is not None else self.registries.ordered()
        dmed: set[int] = set()
        for r in targets:  # each guild's OWN error targets (error_dm_ids), each person DMed once for one failure
            mine = set(error_targets(r.config))
            new = mine - dmed
            try:
                if not new:
                    await self.ops.emit(r.config, "error", text, e, dm=False)
                elif new == mine:
                    await self.ops.emit(r.config, "error", text, e)
                else:
                    await self.ops.emit(r.config, "error", text, e, dm_only=new)
                dmed |= new
            except Exception:  # noqa: BLE001
                log.warning("ops emit failed for the scheduler error (%s)", r.key)

    async def scheduler_tick(self) -> None:
        """One pass: the companion check, then each guild in its own try — one guild's exception never skips the next."""
        errors = self._tick_errors()
        try:
            await self.companion_check()
            setup = getattr(self, "setup_tick", None)  # SetupMixin: the hourly channel-permission check, the daily invite check
            if setup is not None:
                await setup()
            errors.pop("", None)
        except Exception as e:  # noqa: BLE001
            await self._tick_failed(None, e)
        for reg in self.registries.ordered():  # a snapshot: a guild claimed mid-tick is picked up next minute
            try:
                await self.scheduler_tick_guild(reg)
                errors.pop(reg.key, None)
            except Exception as e:  # noqa: BLE001
                await self._tick_failed(reg, e)

    async def companion_check(self) -> None:
        """A companion silent for 5 minutes while its raid is live: warn that raid's guild once (per silence)."""
        feed = getattr(self, "feed", None)
        if not feed:
            return
        for c in list(feed.companions.values()):
            raid = self.active_raid(c)
            if raid is None:
                continue
            if c.silent_for > 300 and not getattr(c, "warned", False):
                c.warned = True
                cfg = self.registries.cfg_for(raid.guild)
                if cfg:
                    await self.ops.emit(cfg, "warn", f"companion {c.character} silent for {int(c.silent_for // 60)} min during {raid.id} — is /chatlog on?")
            elif c.silent_for <= 300:
                c.warned = False

    async def _warn_unopened(self, reg, now) -> None:
        """No signup channel: the scheduler opens nothing (design B4 "scheduler visibility"). Each run that is due to open
        gets ONE ops warn ("Barrow Deeps Tue 7:30 PM did not open: no signup channel") — never repeated every minute, and
        no DM beyond the ops feed's own error path; the Rosters page's Upcoming row says "needs a signup channel"."""
        warned: set = self.__dict__.setdefault("_unopened_warned", set())
        for inst in reg.profile.raids:
            for sched, start in rc.upcoming(reg, inst, now, lead=True):
                key = (reg.key, rc.event_key(inst, start, sched["id"]))
                if key in warned or key[1] in self.raids.store(reg).events:
                    continue
                warned.add(key)
                await self.ops.emit(reg.config, "warn", f"{reg.raid_def(inst).get('name', inst)} {reg.local12(start)} did not open: no signup channel is set (/gm config signup-channel)")

    async def _warn_key_clash(self, reg, inst: str, start, key: str) -> None:
        """A real run's key is held by a test run (a rehearsal opened before the '-test' key space, at that very minute):
        the real run can't open until the test bench is cleared. ONE ops warn per (guild, key) naming the fix — never
        the minute-by-minute RegistryError (and its owner error alert) the tick would otherwise raise."""
        warned: set = self.__dict__.setdefault("_key_clash_warned", set())
        if (reg.key, key) in warned:
            return
        warned.add((reg.key, key))
        await self.ops.emit(reg.config, "warn", f"{reg.raid_def(inst).get('name', inst)} {reg.local12(start)} did not open: a test run holds its key {key} "
                                                "— clear the test bench (/gm test clear) and it opens on the next minute")

    async def _close_run(self, reg, rs, ev, now, exhausted_posted: set) -> None:
        """A locked run past its close: a loot council on it decides (LootMixin.loot_may_close — ended only idle 30 min
        with no pick or drop waiting, after one "closing in 15 min" notice; never with a pick pending), else done."""
        may = getattr(self, "loot_may_close", None)
        if may is not None and not await may(reg, rs, ev, now):
            return
        rc.end_run(reg, rs, ev, "scheduler")
        exhausted_posted.discard((reg.key, ev.key))
        await self.refresh_sheet(reg, ev)
        await self.ops.emit(reg.config, "info", f"{ev.key}: closed")

    async def scheduler_tick_guild(self, reg) -> None:
        """One guild's minute: open on cadence → health → lock → expire → fill → close."""
        # (guild key, run key) whose "nobody left to ask" line went out: two guilds can hold runs with the same key
        exhausted_posted: set[tuple[str, str]] = self.__dict__.setdefault("_fill_exhausted_posted", set())
        cfg = reg.config
        rs = self.raids.store(reg)
        channel = self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None
        await self.cleanup_ephemeral(reg, rs)
        now = reg.now_local()
        # open sheets on cadence: every active schedule's runs within that schedule's signup lead (needs the signup
        # channel); a pickup template never opens here
        if channel:
            for inst in reg.profile.raids:
                for sched, start in rc.upcoming(reg, inst, now, lead=True):
                    held = reg.config.roster(rc.run_key(inst, start, sched["id"])) or {}
                    if held.get("test"):  # a rehearsal from before the '-test' key space holds this real run's key
                        await self._warn_key_clash(reg, inst, start, rc.run_key(inst, start, sched["id"]))
                        continue
                    if rc.event_key(inst, start, sched["id"]) in rs.events:
                        continue
                    try:
                        await self.open_run_and_post(reg, rs, inst, start, by="scheduler", **rc.schedule_kw(sched["id"]))
                    except rc.RunKeyClash:
                        await self._warn_key_clash(reg, inst, start, rc.run_key(inst, start, sched["id"]))
        else:
            await self._warn_unopened(reg, now)
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
                await self.close_prelock_asks(reg, rs, ev)  # pre-lock asks close at lock time ("released", as lock_run does)
                tried = datetime.fromisoformat(ev.lock_tried_at) if ev.lock_tried_at else None
                if ev.lock_error and tried and now - tried < timedelta(minutes=15):
                    continue  # failed recently; give the officers time to fix the board before trying again
                try:
                    line = await self.lock_run(reg, rs, ev)
                except RegistryError:  # someone is locking it right now (rc.SolverBusy), or it stopped being open
                    continue
                if not ev.lock_error:
                    await self.ops.emit(cfg, "info", line)
                continue
            if state != "locked":
                continue  # the fill engine, confirmations and closing only apply to a locked roster
            if now >= rc.run_close_at(reg, ev):
                # past the close the raid is over: no confirmation expiry, no fill asks (a council kept open by a pick
                # still waiting must never send fill DMs after the raid) — only the close below
                await self._close_run(reg, rs, ev, now, exhausted_posted)
                continue
            # a lock interrupted after its flip (a crash, a Discord error mid fan-out): ask the rostered nobody asked and
            # re-post missing lock cards — BEFORE expiry, which only ever expires an ask that exists (design B6)
            await self.recover_lock(reg, rs, ev)
            async with self.board_lock(reg, ev.key):  # under the run's lock: a cancel mid-tick (during the awaits above) is seen
                if ev.state != "locked":
                    continue
                gone_ids = rc.expire_confirmation_asks(reg, rs, ev)
            gone = [name for _uid, name in gone_ids]
            if gone:
                if ev.fill_state == "exhausted":  # a freed seat is a new gap: the engine looks again
                    ev.fill_state = "idle"
                    rs.save(ev, "fill: seats freed")
                for uid, _name in gone_ids:  # their Confirm / Can't card says the seat was released (an edit — never a DM)
                    m = reg.members.get(uid)  # by id: a rename or two members with one name never edit the wrong card
                    if m:
                        await self.seat_released(reg, ev, m, "no-confirm")
                await self.post_run_update(reg, ev, f"⌛ no confirmation from {', '.join(gone)} — seats freed")
                await self.refresh_sheet(reg, ev)
                await self.refresh_cards(reg, ev)
                await self.ops.emit(cfg, "warn", f"{ev.key}: confirmations expired for {len(gone)}")
            if ev.state != "locked":  # cancelled while the expired seats' cards were being edited: no fill step
                continue
            expired, released = rc.expire_fill_asks(reg, rs, ev)
            if expired:
                await self.post_run_update(reg, ev, f"⌛ no answer from {', '.join(a.display_name for a in expired)} — counts as no")
                for a in expired:  # the expired ask's own card: its final state, no live Yes / No (never a new DM)
                    await self.close_ask_card(reg, ev, a, f"it closed <t:{int(now.timestamp())}:t> without an answer")
                for a in released:
                    await self.withdraw_ask(reg, ev, a, "the other half of the question timed out")
                await self.refresh_cards(reg, ev)
            # every state but "exhausted" (nobody left; a freed seat resets it): "suggested" (a role swap waits for an
            # officer, D3) and "filled" still get same-role asks for a seat freed later (an expired confirmation)
            if rc.team_setting(team, "autofill") and ev.fill_state != "exhausted":
                sent, nd = await self.run_fill(reg, rs, ev, team)
                if sent:
                    exhausted_posted.discard((reg.key, ev.key))
                    gaps = gaps_text(self.ico, nd)
                    await self.post_run_update(reg, ev, (f"🧩 short {gaps}:" if gaps else "🧩 asked:") + "\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
                elif ev.fill_state == "exhausted" and (reg.key, ev.key) not in exhausted_posted:  # just transitioned: say so once
                    exhausted_posted.add((reg.key, ev.key))
                    gaps = gaps_text(self.ico, nd)
                    await self.post_run_update(reg, ev, "🧩 nobody left to ask" + (f" — short {gaps}" if gaps else ""))
            elif ev.fill_state != "exhausted":
                exhausted_posted.discard((reg.key, ev.key))
        # the loot council's minute (LootSurfaceMixin.loot_scheduler_step): companion drops arriving with no run live →
        # one "Start an ad hoc run for tonight?" line; in-game awards after a council ended → the officers' one DM
        step = getattr(self, "loot_scheduler_step", None)
        if step is not None:
            await step(reg)
