"""Discord surface for the signup-driven cycle — the `RaidMixin` on the client: DM routing (test puppets → the
tester), the sheet and officer cards kept current, set_answer (the one path for a member's or officer's answer),
lock_run → roster cards + Confirm/Can't DMs, the fill engine's DMs, drop_seated, opening / cancelling runs, and the
absence ripple. The rest of the surface lives next door and is re-exported here so older imports keep working:
`raid_views` (layouts + text helpers), `raid_buttons` (persistent buttons), `raid_scheduler` (RaidSchedulerMixin),
`raid_commands` (/raid …). `raid_views.BotProto` lists what the mixins expect from the client."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

import discord

from . import raidcycle as rc
from .discord_registry import Guilds
from .raid_buttons import FillButton, PlaceButton, RunButton, SignupButton, fill_view, place_view, sheet_view
from .raid_commands import register_raid_commands
from .raid_scheduler import RaidSchedulerMixin
from .raid_views import (CLOCK12, OUT_MARK, RELEASE_WHY, BotProto, _aura_line, _group_block, _group_summaries, _header,
                         _raidwide_line, _role_counts, ask_line, board_url, clock12, closed_layout, confirm_layout, fill_layout,
                         gaps_text, guild_prefix, health_layout, health_locked_layout, lock_layout, raid_name, run_actions_row, run_label, run_times, run_title, sheet_message,
                         sheet_state)
from .registry import Registry, RegistryError

log = logging.getLogger(__name__)

__all__ = ["RaidContext", "RaidMixin", "RaidSchedulerMixin", "BotProto", "register_raid_commands",
           # buttons
           "FillButton", "PlaceButton", "RunButton", "SignupButton", "fill_view", "place_view", "sheet_view",
           # views + helpers
           "CLOCK12", "OUT_MARK", "RELEASE_WHY", "ask_line", "board_url", "clock12", "closed_layout", "confirm_layout", "fill_layout", "gaps_text",
           "health_layout", "health_locked_layout", "lock_layout", "raid_name", "run_actions_row", "run_label", "run_times", "run_title", "sheet_message", "sheet_state",
           "_aura_line", "_group_block", "_group_summaries", "_header", "_raidwide_line", "_role_counts"]


class RaidContext:
    """Per-guild raid store + helpers, attached to the bot."""

    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, rc.RaidStore] = {r.key: rc.RaidStore(guilds.store, r.key) for r in guilds.ordered()}

    def store(self, reg: Registry) -> rc.RaidStore:
        return self.stores.setdefault(reg.key, rc.RaidStore(self.guilds.store, reg.key))


# ---------------------------------------------------------------- bot mixin

class RaidMixin:
    """Methods the bot needs; mixed into OibotGM."""

    async def may_answer_for(self, interaction: discord.Interaction, reg, uid: int) -> bool:
        """The person asked — or an officer answering for a test member (the test bench's puppets)."""
        if interaction.user.id == uid:
            return True
        m = reg.members.get(uid) if reg else None
        return bool(m and getattr(m, "test", False) and await self.is_officer_anywhere(reg, interaction.user.id))

    async def send_member_dm(self, reg, m, text: str | None = None, view=None, ev=None) -> bool:
        """DM a member — text + buttons, or a layout card (`view` a LayoutView, `text` None). For a test member the same
        goes to whoever is running the test (`ev`'s own tester when the DM is about a run, so two officers testing at
        once each get their own puppets' DMs), with the puppet's name in front. Returns True when delivered.
        While the process serves several guilds a text DM starts with the guild's name (raid_views.guild_prefix; a card
        names it in its header), unless the text already opens with it; with one guild nothing changes."""
        if text and not str(text).startswith(str(reg.config.name)):
            text = guild_prefix(reg) + text
        target, prefix = m.discord_id, None
        if getattr(m, "test", False):
            own = (reg.config.team(ev.team) or {}).get("test_by") if ev is not None else None
            tester = own or next((t.get("test_by") for t in reg.config.rosters if t.get("test") and t.get("test_by")), None) or reg.config.owner_discord_id
            if not tester:
                return False
            target, prefix = int(tester), f"🧪 **{m.display_name}** would get:"
        try:
            user = await self.fetch_user(target)
            if isinstance(view, discord.ui.LayoutView):
                if prefix:
                    await user.send(prefix)
                await user.send(view=view)
            else:
                await user.send((prefix + "\n" + (text or "")) if prefix else (text or ""), view=view)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- officer cards + the run's updates thread
    async def post_run_update(self, reg, ev, text: str) -> None:
        """One line in the run's thread (under its health or lock card; created on first use); falls back to the officer channel."""
        ch = self.officer_channel(reg, ev)
        thread = self.get_channel(ev.updates_thread_id) if ev.updates_thread_id else None
        if thread is None and ev.cards and ch:
            mid = ev.cards.get("lock:0") or ev.cards.get("health")
            try:
                msg = await ch.fetch_message(mid)
                thread = await msg.create_thread(name=f"{run_title(reg, ev)} · updates"[:100])
                ev.updates_thread_id = thread.id
                rs = self.raids.store(reg)
                rs.save(ev, "updates thread")
            except Exception:  # noqa: BLE001
                thread = None
        try:
            await (thread or ch).send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:  # noqa: BLE001
            pass

    async def refresh_cards(self, reg, ev) -> None:
        """Re-render the run's officer cards in place after anything that changes seats or confirmations."""
        if not ev.cards or not ev.cards_channel_id:
            return
        ch = self.get_channel(ev.cards_channel_id)
        if not ch:
            return
        team = rc.run_team(reg, ev)
        rs = self.raids.store(reg)
        for key, mid in list(ev.cards.items()):
            try:
                msg = await ch.fetch_message(mid)
                if ev.state in ("done", "cancelled"):
                    await msg.edit(view=closed_layout(reg, ev, self.ico))
                elif key == "health":
                    if ev.state == "open":
                        await msg.edit(view=health_layout(reg, rs, ev, team, self.ico))
                    else:  # locked: the card's Suggested block and Ask were for the open sheet — it now points at the lock card, once
                        await msg.edit(view=health_locked_layout(reg, ev, self.ico))
                        ev.cards.pop("health", None)
                        rs.save(ev, "health card closed at lock")
                elif key.startswith("lock:"):
                    i = int(key[5:])
                    if i < len(ev.all_rosters):
                        await msg.edit(view=lock_layout(reg, ev, team, self.ico, i, rs))
            except Exception as e:  # noqa: BLE001
                log.warning("card refresh failed (%s %s): %s", ev.key, key, e)

    async def apply_signup(self, interaction: discord.Interaction, reg, rs, ev, m, character, status):
        """A member's own press (or an officer pressing for a test puppet): set_answer, the line back ephemerally."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            line = await self.set_answer(reg, rs, ev, m, status, character, by=m.display_name)
        except ValueError as e:
            line = f"❌ {e}"
        await interaction.followup.send(line, ephemeral=True)

    async def set_answer(self, reg, rs, ev, m, status: str, character: str | None, by: str) -> str:
        """The one path for setting someone's answer on a sheet — the member (by == their name) or an officer.
        Open sheet: the signup + sheet refresh. Locked: Join seats them (if not already) and asks them to confirm;
        No thanks frees a rostered seat (drop_seated: DM, thread line, fill); Bench is the signup only. Returns a line."""
        if status not in rc.STATUSES:
            raise ValueError(f"status must be one of {rc.STATUSES}")
        mine = by == m.display_name
        source = "member" if mine else "officer"
        team = rc.run_team(reg, ev)
        if team.get("test") and not getattr(m, "test", False) and str(m.discord_id) != str(team.get("test_by") or ""):
            # a rehearsal: its pool is the puppets plus the officer running it — never a real member (web, chat, MCP alike)
            raise RegistryError(f"{run_title(reg, ev)} is a test run: only test members and the officer running it can answer it")
        label = run_label(reg, ev).replace("**", "")  # plain text: the line is shown in Discord replies and on the web alike
        who = "" if mine else f"{m.display_name}: "
        released = status == "out" and await self.release_asks_of(reg, rs, ev, m, "answered No thanks")  # their ask covers nothing now
        if sheet_state(ev) == "open":
            s = rc.set_signup(reg, rs, ev, m, character, status, source=source)
            await self.refresh_sheet(reg, ev)
            if "health" in ev.cards:  # the officer card's counts and Suggested block follow the answers
                await self.refresh_cards(reg, ev)
            line = f"✅ {who}{s.character} {rc.LABELS[s.status]} for {label}" + (f" — {s.note}" if s.note and s.status != status else "")
        elif status == "out":
            if ev.seat_of(m.display_name):
                if mine:  # the sheet's Can't make it after lock is a (late) callout: recorded like /raid out's
                    rc.callout(reg, rs, ev, m, team, None)
                await self.drop_seated(reg, rs, ev, team, m, "callout" if mine else "officer", None)
                line = f"✅ {who or 'you are '}off the {label} roster — seat freed" + ("; the bot is looking for a replacement" if rc.team_setting(team, "autofill") else "")
            else:
                rc.set_signup(reg, rs, ev, m, character, "out", source=source)
                await self.refresh_sheet(reg, ev)
                await self.refresh_cards(reg, ev)
                await self.refill_after_release(reg, rs, ev, released)
                line = f"✅ {who}No thanks for {label} (wasn't rostered)"
        elif status == "in":
            s = rc.set_signup(reg, rs, ev, m, character, "in", source=source)
            seat = ev.seat_of(m.display_name)
            if seat:
                rc._reseat(ev, s)  # a character swap keeps the seat
                rs.save(ev, f"{m.display_name} seat updated")
                line = f"✅ {who}{s.character} stays rostered for {label}"
            else:
                i = rc.seat_player(reg, ev, s)
                if i is None:
                    rs.save(ev, f"{m.display_name} in, no seat")
                    line = f"✅ {who}{s.character} Join for {label} — every seat is taken, so not rostered (move them in on the board)"
                else:
                    rs.save(ev, f"{m.display_name} rostered by {by}")
                    gi = next((k for k, g in enumerate(ev.all_rosters[i].groups) if m.display_name in g), 0)
                    sent = await self.confirm_one(reg, ev, s, i, gi, by)
                    asks = rc.run_confirms(reg, ev)
                    await self.post_run_update(reg, ev, f"✏️ {by} rostered {m.display_name} ({s.character})" + ("" if not asks else " — asked to confirm" if sent else " — DMs off, the ask waits on the site" if m.dm_opt_out else ""))
                    line = f"✅ {who}{s.character} rostered for {label}" + ("" if not asks else " — asked to confirm by DM" if sent else " — DMs off: the confirmation waits on the site" if m.dm_opt_out else "")
            await self.refresh_sheet(reg, ev)
            await self.refresh_cards(reg, ev)
        else:  # sub after lock: the signup only; a rostered member keeps the seat until set out
            s = rc.set_signup(reg, rs, ev, m, character, "sub", source=source)
            await self.refresh_sheet(reg, ev)
            line = f"✅ {who}{s.character} Bench for {label}" + (" — still rostered; set No thanks to free the seat" if ev.seat_of(m.display_name) else "")
        if not mine:
            await self.ops.emit(reg.config, "info", f"{by} set {m.display_name} {status} on {ev.key}")
        return line

    async def refresh_sheet(self, reg, ev) -> None:
        team = rc.run_team(reg, ev)
        if ev.channel_id and ev.message_id:
            ch = self.get_channel(ev.channel_id)
            try:
                msg = await ch.fetch_message(ev.message_id)
                embed, view = sheet_message(reg, ev, team, self.ico)
                if msg.flags.components_v2:  # a sheet from the layout-component era can't become an embed: replace it once
                    new = await ch.send(embed=embed, view=view)
                    ev.message_id = new.id
                    self.raids.store(reg).save(ev, "sheet re-posted (embed layout)")
                    await msg.delete()
                else:
                    await msg.edit(embed=embed, view=view)
            except Exception as e:  # noqa: BLE001
                log.warning("sheet refresh failed (%s): %s", ev.key, e)

    async def post_sheet(self, reg, rs, ev, channel) -> None:
        team = rc.run_team(reg, ev)
        embed, view = sheet_message(reg, ev, team, self.ico)
        msg = await channel.send(embed=embed, **({"view": view} if view else {}))
        ev.channel_id, ev.message_id = channel.id, msg.id
        rs.save(ev, "sheet posted")
        if rc.team_setting(team, "open_dm"):
            await self.dm_on_open(reg, rs, ev, team)

    async def dm_on_open(self, reg, rs, ev, team) -> int:
        """Optional per-run: DM everyone the sheet is for (rc.run_pool: the series' regulars, or every main) when it
        opens, with the same buttons."""
        unix = int(ev.start.timestamp())
        _soft, hard, _confirm = run_times(reg, ev, team)
        sent = 0
        for m, _c in rc.run_pool(reg, ev):
            if not m.main or m.dm_opt_out:
                continue
            s = ev.signups.get(str(m.discord_id))
            status = f"You're marked **{rc.LABELS.get(s.status, s.status)}** ({'away that day' if s.source == 'absence' else s.source})" if s else "You haven't answered yet"
            if await self.send_member_dm(reg, m, f"{reg.config.name} · {run_label(reg, ev)} (<t:{unix}:R>). {status}. Join / Bench / No thanks:" + (f" (sheet: <#{ev.channel_id}>)" if ev.channel_id else "") + f"\n-# The roster locks <t:{int(hard.timestamp())}:f>; no answer by then means you're not on it.", sheet_view(reg, ev.key), ev=ev):
                sent += 1
        ev.log.append(f"open DMs sent to {sent}")
        rs.save(ev, f"open DMs {sent}")
        return sent

    async def post_health(self, reg, rs, ev, channel, nudge: bool, early: bool = False) -> None:
        """The officer health card (with its Suggested block) and, at reminder time, the nudge DMs. `early`: the early
        warning a recurring sheet posts when it opens and its regulars can't cover a minimum — the card only (no
        nudge, the reminder time still comes). At reminder time an early card is replaced by a fresh one (rebuilt
        from the answers by then), so the current card sits at the bottom of the channel."""
        team = rc.run_team(reg, ev)
        if channel is not None:
            old = ev.cards.get("health") if not early and ev.health_early else None
            try:
                msg = await channel.send(view=health_layout(reg, rs, ev, team, self.ico))
                ev.cards["health"], ev.cards_channel_id = msg.id, channel.id
            except Exception as e:  # noqa: BLE001
                log.warning("health card failed (%s): %s", ev.key, e)
            if old and old != ev.cards.get("health"):
                with contextlib.suppress(Exception):
                    await (await channel.fetch_message(old)).delete()
        if early:
            ev.health_early = True
            ev.log.append("early warning: the regulars can't cover a minimum — health card posted at open")
            rs.save(ev, "early warning")
            return
        if nudge and rc.team_setting(team, "reminders") != "none":
            # the expected who haven't answered: a recurring run's regulars (every main while the series has none);
            # an ad hoc run expects nobody (and its reminder is off by default)
            targets = [m for m, _c in rc.not_answered(reg, ev) if m.discord_id not in ev.nudged and not m.dm_opt_out]
            _soft, hard, _confirm = run_times(reg, ev, team)
            for m in targets:  # same buttons as the sheet (and the open DM), so they can answer right here
                if await self.send_member_dm(reg, m, f"{reg.config.name}: the {run_label(reg, ev)} sheet is still waiting for you — Join, Bench or No thanks here or in <#{ev.channel_id}>.\n-# The roster locks <t:{int(hard.timestamp())}:f>; no answer by then means you're not on it.", sheet_view(reg, ev.key), ev=ev):
                    ev.nudged.append(m.discord_id)
            if targets:
                rs.save(ev, f"nudged {len(targets)}")
        ev.health_posted = True
        rs.save(ev, "health posted")

    # ---- filling gaps by DM
    async def run_fill(self, reg, rs, ev, team, by: str = "scheduler", approved_by: str | None = None, swaps: bool | None = None) -> tuple[list, dict]:
        """Send the next batch of fill DMs. Returns (asks sent, needs). The fill engine only runs on a locked roster:
        before lock nothing is sent this way (an officer sends suggestions with send_plan). `approved_by` = the officer
        who pressed Send: their batch may include role swaps. On its own (the scheduler, a freed seat, an answer) the
        engine sends role swaps on an ad hoc run only — owner decision D3: on a recurring run a swap waits for an
        officer's Ask (fill_state "suggested", the lock card lists it). `swaps=False` keeps role swaps out whoever
        sends (the site's / MCP's send without a preview may only send same-role asks). An officer's send from a
        screen goes through send_plan instead: the batch they were shown, nothing else."""
        nd = rc.needs(reg, ev, team)
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            return [], nd
        if not nd["headcount"] and not nd["roles"]:
            if ev.fill_state in ("asking", "suggested"):
                ev.fill_state = "filled"
                rs.save(ev, "fill: complete")
            return [], nd
        swaps = (approved_by is not None or rc.auto_swaps(reg, ev)) if swaps is None else swaps
        batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team, swaps=swaps)
        sent = []
        deadline = rc.ask_deadline(reg, ev).isoformat()
        for ask in batch:  # tied pairs sit next to each other in the batch; both are on the event before either DM goes out
            ask.expires_at, ask.approved_by = deadline, approved_by
            ev.fill_asks.append(ask)
        for ask in batch:
            m = reg.members.get(ask.discord_id)
            if m and await self.send_member_dm(reg, m, None, fill_layout(reg, ev, team, self.ico, ask), ev=ev):
                sent.append(ask)
            else:
                ask.answer, ask.answered_at = "expired", rc.now()
                rc.release_partner(reg, rs, ev, ask)
        if sent:
            ev.fill_state = "asking"
            ev.log.append(f"fill ({by}): asked {', '.join(a.display_name for a in sent)}")
            rs.save(ev, f"fill asked {len(sent)}")
        elif not [a for a in ev.fill_asks if a.pending]:
            if not swaps and await asyncio.to_thread(rc.withheld_swaps, reg, rs, ev):
                if ev.fill_state != "suggested":  # said once: the swap needs an officer's Ask on the lock card
                    ev.fill_state = "suggested"
                    ev.log.append("fill: a role swap would cover it — waiting for an officer's Ask")
                    rs.save(ev, "fill: role swap suggested")
                    await self.post_run_update(reg, ev, f"🧩 short {gaps_text(self.ico, nd)} — a role swap would cover it; it needs an officer's **Ask** (the Suggested block on the lock card)")
                    await self.refresh_cards(reg, ev)
            else:
                ev.fill_state = "exhausted"
                rs.save(ev, "fill: nobody left to ask")
        return sent, nd

    def plan_lock(self, reg, key: str) -> asyncio.Lock:
        """One officer send per run at a time (two presses of Ask never ask anyone twice). Keyed (guild, run): two
        guilds can hold runs with the same key."""
        return self.__dict__.setdefault("_plan_locks", {}).setdefault((reg.key, key), asyncio.Lock())

    async def send_plan(self, reg, rs, ev, *, rev: str, by: str, picks: dict | None = None, limit: int | None = None) -> tuple[list, "rc.Plan"]:
        """An officer's Ask: send the planner's suggestions (each seat's suggestion, or the alternate in `picks` =
        {seat index: option index}) as DMs — the one path for the health / lock card's Ask, the Suggest wizard,
        /raid fill on an open run, the site's Suggest, plain text and MCP. `rev` = the plan fingerprint the officer was
        shown (rc.plan_rev): when the plan is different now, rc.PlanChanged is raised carrying the current plan and
        nothing is sent. Before lock: pre-lock asks (one per member per run, expire at lock; a yes changes the signup
        and seats nobody). After lock: fill asks the officer approved — role swaps included (D3). `limit`: only the first
        that many seats (the card's Ask: the seats the card draws, never one it left under "+N more"). Returns (sent, plan)."""
        async with self.plan_lock(reg, ev.key):
            if ev.state not in ("open", "locked"):
                raise RegistryError(f"{run_title(reg, ev)} is {ev.state}")
            plan = rc.role_plan(reg, rs, ev)
            if not rev or rev != plan.rev:
                raise rc.PlanChanged(plan)
            # exactly the batch the officer was shown (rc.send_batch: after lock the FILL_MAX_OPEN cap, which is part of
            # the rev, so the preview listed these same asks)
            asks, plan.held_back = rc.send_batch(plan, picks, limit)
            if not asks:
                return [], plan
            team = rc.run_team(reg, ev)
            _soft, hard, _confirm = run_times(reg, ev, team)
            prelock = plan.phase == "prelock"
            deadline = (min(hard, ev.start) if prelock else rc.ask_deadline(reg, ev)).isoformat()  # pre-lock asks expire at lock time
            for a in asks:
                a.phase, a.approved_by, a.expires_at, a.asked_at = plan.phase, by, deadline, rc.now()
                ev.fill_asks.append(a)
            sent = []
            for a in asks:
                m = reg.members.get(a.discord_id)
                if m and await self.send_member_dm(reg, m, None, fill_layout(reg, ev, team, self.ico, a), ev=ev):
                    sent.append(a)
                else:
                    a.answer, a.answered_at = "expired", rc.now()
                    rc.release_partner(reg, rs, ev, a)
            if sent and not prelock:
                ev.fill_state = "asking"
            ev.log.append(f"{'suggestions' if prelock else 'fill'} sent by {by}: " + (", ".join(a.display_name for a in sent) or "nobody reachable"))
            rs.save(ev, f"{'suggestions' if prelock else 'fill'} sent by {by}")
        if sent:
            await self.post_run_update(reg, ev, f"🧩 {by} asked:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent)
                                       + (f"\n-# {plan.held_back} more wait — at most {rc.FILL_MAX_OPEN} asks out at once; the card keeps them" if plan.held_back else ""))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(reg.config, "info", f"{by} sent {len(sent)} {'pre-lock ' if prelock else ''}ask(s) on {ev.key}")
        return sent, plan

    async def close_prelock_asks(self, reg, rs, ev) -> list:
        """Lock time reached: pre-lock asks still out close (`released`, as lock_run does — a lock that keeps failing
        leaves none open); a yes held for a tied partner who never answered is told nothing changed."""
        held = [a for a in ev.fill_asks if a.held and a.phase == "prelock"]
        gone = rc.release_prelock_asks(ev)
        if gone:
            rs.save(ev, "pre-lock asks closed at lock time")
        for a in held:
            await self.withdraw_ask(reg, ev, a, "the roster locks before the other half of the swap was answered")
        return gone

    async def release_asks_of(self, reg, rs, ev, m, why: str) -> bool:
        """`m` can't come any more (No thanks, a callout, an absence): close their asks still in play (rc.release_member_asks)
        and tell the tied half of each pair it's off. The fill engine's next batch is the caller's (drop_seated, or
        refill_after_release) — so a seat their ask was holding is asked for again. True when an ask was closed."""
        had = any(a.discord_id == m.discord_id and a.pending for a in ev.fill_asks)
        partners = rc.release_member_asks(reg, rs, ev, m.discord_id, why)
        for other in partners:
            await self.withdraw_ask(reg, ev, other, f"{m.display_name} can't come")
        if had:
            await self.post_run_update(reg, ev, f"↩️ {m.display_name}'s ask closed ({why})")
        return had

    async def refill_after_release(self, reg, rs, ev, released: bool) -> None:
        """After lock, a seat a released ask was holding goes back to the fill engine (when the run fills on its own)."""
        if not released:
            return
        team = rc.run_team(reg, ev)
        if rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, _nd = await self.run_fill(reg, rs, ev, team, by="ask closed")
            if sent:
                await self.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))

    async def withdraw_ask(self, reg, ev, ask, because: str) -> None:
        """Tell the other half of a tied pair the question is off."""
        m = reg.members.get(ask.discord_id)
        if m:
            await self.send_member_dm(reg, m, f"Never mind the {'swap' if ask.swap else 'seat'} question for **{reg.raid_def(ev.instance).get('name', ev.instance)}** <t:{int(ev.start.timestamp())}:F> — {because}. Nothing to do.", ev=ev)
        await self.post_run_update(reg, ev, f"↩️ withdrew the ask to {ask.display_name} ({because})")

    async def after_fill_answer(self, reg, rs, ev, ask, line: str) -> None:
        await self.refresh_sheet(reg, ev)
        cfg = reg.config
        team = rc.run_team(reg, ev)
        if ask.answer != "yes":
            other = rc.release_partner(reg, rs, ev, ask)
            if other:
                await self.withdraw_ask(reg, ev, other, f"{ask.display_name} said no")
        if ask.held:  # the first yes of a tied pair: nothing changed yet, the partner's answer decides
            await self.post_run_update(reg, ev, f"🧩 {line}")
            await self.refresh_cards(reg, ev)
            await self.ops.emit(cfg, "info", f"fill {ev.key}: {line}")
            return
        both = rc.tied_ask(ev, ask) if ask.answer == "yes" else None
        both = both if both is not None and both.answer == "yes" else None  # the partner's held yes took effect just now
        if both is not None:
            om = reg.members.get(both.discord_id)
            if om:
                await self.send_member_dm(reg, om, f"{ask.display_name} said yes too, so it's on for {run_label(reg, ev)}: you're down as **{both.character}** ({both.spec})"
                                          + (" — the lock builds the roster." if ev.state == "open" else "."), ev=ev)
        if ask.phase == "prelock" and ev.state == "open":  # before lock: the signup changed, nobody was seated
            plan = rc.role_plan(reg, rs, ev)
            await self.post_run_update(reg, ev, f"🧩 {line}" + (f" · still short {rc.still_short_words(plan)}" if plan.seats else " · **minimums covered**"))
            await self.refresh_cards(reg, ev)
            await self.ops.emit(cfg, "info", f"suggestion {ev.key}: {line}")
            return
        nd = rc.needs(reg, ev, team)
        await self.post_run_update(reg, ev, f"🧩 {line}" + (f" · still short {gaps_text(self.ico, nd)}" if nd["headcount"] or nd["roles"] else " · **gaps filled**"))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info", f"fill {ev.key}: {line}")
        if ask.answer == "yes" and ev.state != "open":
            for done in [ask] + ([both] if both is not None else []):
                m = reg.members.get(done.discord_id)
                if m and ev.seat_of(m.display_name):  # a yes that found no free seat is a Join, not a rostered seat
                    a = reg.add_placement_ask(m.discord_id, ev.team, done.character, "fill")
                    a["answer"], a["answered_at"] = "yes", rc.now()
                    reg.save(m, f"{m.display_name} rostered on {ev.team} via fill")
        if ask.answer == "no":
            await self.run_fill(reg, rs, ev, team, by="answer")

    async def is_officer_anywhere(self, reg, uid: int) -> bool:
        if reg.config.owner_discord_id == uid:
            return True
        guild = self.get_guild(reg.config.discord_guild_id)
        if not guild:
            return False
        m = await self.cached_member(guild, uid)  # TTL cache on the client: button presses don't hit the API each time
        return bool(m) and self.officiates(m, guild)

    async def cleanup_ephemeral(self, reg, rs) -> None:
        """Drop run rosters whose sheet is done/cancelled (who held a seat stays in the run file; no member file
        carries a run key any more)."""
        cfg = reg.config
        gone = []
        for t in list(cfg.rosters):
            if not t.get("ephemeral"):
                continue
            evs = [e for e in rs.events.values() if e.team == t["key"]]
            if evs and all(e.state in ("done", "cancelled") for e in evs):
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
        if gone:
            reg.save_config(f"runs closed: {gone}", notify=False)

    # ---- lock → roster(s) → confirmations
    def officer_channel(self, reg, ev=None):
        """Where officer-facing posts go: the roster channel, else the ops channel, else nowhere (never the public sheet channel)."""
        cfg = reg.config
        return (self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else None) or (self.get_channel(cfg.ops_channel_id) if cfg.ops_channel_id else None)

    @contextlib.asynccontextmanager
    async def solving(self, reg, key: str):
        """Single flight for the solver per run, whoever asks (a lock from chat, MCP, the scheduler, the card or the
        site; an auto-fill; a split preview): a second caller gets rc.SolverBusy instead of a second solve. Keyed
        (guild, run key)."""
        lock = self.__dict__.setdefault("_solver_locks", {}).setdefault((reg.key, key), asyncio.Lock())
        if lock.locked():
            raise rc.SolverBusy()
        async with lock:
            yield

    async def lock_run(self, reg, rs, ev, by: str = "scheduler") -> str:
        """Lock the sheet, build the roster(s) from the signups (pins honoured), show them on the sheet and in the
        officer channel, and DM every rostered member for confirmation. One lock per run at a time (solving: a second
        caller gets rc.SolverBusy); the board lock is held from the snapshot to the state flip, so a move made during
        the solve waits and is then refused as stale (never silently lost); a run no longer open is refused. Answers
        and pins take no lock while the sheet is open, so one written during the solve (a No thanks, a pin out) makes
        the lock solve again on the live run (rc.solve_inputs), up to rc.LOCK_SOLVES times; still moving after that,
        the lock fails and the sheet stays open — nobody is rostered from answers they already changed."""
        team = rc.run_team(reg, ev)
        async with self.solving(reg, ev.key), self.board_lock(reg, ev.key):
            if ev.state != "open":
                raise RegistryError(f"{ev.key} is already {ev.state}")
            # solve on a copy first: the run only becomes locked once a roster exists, so a failed solve (or a crash
            # mid-way) leaves the sheet open rather than a locked run with no roster
            for _ in range(rc.LOCK_SOLVES):
                trial = ev.model_copy(deep=True)
                seen, inputs = len(trial.log), rc.solve_inputs(trial)
                ev.lock_tried_at = rc.now()
                try:
                    await asyncio.to_thread(rc.propose, reg, rs, trial, False)
                except Exception as e:  # noqa: BLE001 — solver infeasible / timed out
                    why = rc.solver_error_text(e)
                    break
                why = None
                if ev.state != "open" or rc.solve_inputs(ev) == inputs:
                    break
                ev.log.append(f"lock ({by}): answers or pins changed during the solve — solving again")
            else:
                why = "the answers or pins kept changing while the roster was being built; lock again in a moment"
            if why is not None:
                first = ev.lock_error != why
                ev.lock_error = why
                ev.log.append(f"lock ({by}) failed: {why}")
                rs.save(ev, "lock failed")
                if first:
                    await self.post_run_update(reg, ev, f"⚠️ lock by {by} failed — {why}. The sheet stays open; fix the board or the raid rules and lock again.")
                await self.ops.emit(reg.config, "warn", f"{ev.key}: lock failed — {why}")
                return f"{ev.key}: lock failed — {why}"
            if ev.state != "open":  # cancelled while the roster was being built
                raise RegistryError(f"{ev.key} was {ev.state} before the roster was ready — not locked")
            _, _, confirm = run_times(reg, ev, team)
            confirms = rc.run_confirms(reg, ev)  # off (ad hoc): no confirmation DMs and no deadline — no seat ever expires
            ev.rosters = trial.rosters
            ev.log.extend(trial.log[seen:])  # the solver's lines; lines written on the live run meanwhile stay
            ev.state, ev.locked_at, ev.confirm_by, ev.lock_error = "locked", rc.now(), confirm.isoformat() if confirms else None, None
            ev.log.append(f"locked by {by}")
            held = [a for a in ev.fill_asks if a.held and a.phase == "prelock"]
            rc.release_prelock_asks(ev)  # pre-lock asks expire at lock time (a yes held for a tied partner too)
            short = rc.needs(reg, ev, team)["roles"]
            if short:  # the solver caps a minimum at what the sheet can supply: say so instead of locking quietly
                ev.log.append("locked short " + ", ".join(f"{k} {r}" for r, k in short.items()))
            rs.save(ev, f"locked by {by}")
        if short:
            await self.ops.emit(reg.config, "warn", f"{ev.key}: locked short " + ", ".join(f"{k} {r}" for r, k in short.items()))
        for a in held:  # they said yes, but their partner never answered: nothing changed for them
            await self.withdraw_ask(reg, ev, a, "the roster locked before the other half of the swap was answered")
        await self.refresh_sheet(reg, ev)
        if "health" in ev.cards:  # the open sheet's card: its Suggested block and Ask go; it points at the lock card
            await self.refresh_cards(reg, ev)
        ch = self.officer_channel(reg, ev)
        if ch:
            for i, r in enumerate(ev.all_rosters):
                try:
                    msg = await ch.send(view=lock_layout(reg, ev, team, self.ico, i, rs))
                    ev.cards[f"lock:{i}"], ev.cards_channel_id = msg.id, ch.id
                except Exception as e:  # noqa: BLE001
                    log.warning("lock card failed (%s roster %d): %s", ev.key, i + 1, e)
            rs.save(ev, "lock cards")
        else:
            await self.ops.emit(reg.config, "warn", f"{ev.key}: no roster/ops channel set — lock cards not posted")
        sent = await self.send_confirmations(reg, rs, ev, team, by) if confirms else 0
        sig = self.get_channel(ev.channel_id) if ev.channel_id else None
        if sig and sig is not ch:
            then = ("Rostered? confirm in your DMs. Not rostered? nothing to do." if confirms
                    else "Rostered? you're in — press Can't make it on the sheet if that changes. Not rostered? nothing to do.")
            try:
                await sig.send(f"🔒 {run_label(reg, ev)} is locked: {sum(len(r.selected) for r in ev.all_rosters)} rostered" + (f" in {len(ev.all_rosters)} rosters" if len(ev.all_rosters) > 1 else "") + f". {then}", allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:  # noqa: BLE001
                log.warning("lock line failed (%s): %s", ev.key, e)
        return f"{ev.key}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)"

    async def confirm_one(self, reg, ev, sg, roster_i: int, group_i: int, by: str) -> bool:
        """Seat one signup on the run's roster and ask them to confirm: by DM, or — DMs off — as a pending ask on the
        Me page (channel "web"). Never auto-confirmed: the tally counts them as waiting until they answer, and an
        unanswered ask expires like any other. Returns True when a DM went out."""
        m = reg.members.get(sg.discord_id)
        if not m or not rc.run_confirms(reg, ev):  # confirmations off (ad hoc, D5): Join is the commitment, nothing to ask
            return False
        # the seat itself is in the run file (ev.rosters); the member file only gets the confirmation ask
        reg.add_placement_ask(m.discord_id, ev.team, sg.character, by, channel="web" if m.dm_opt_out else "dm")
        if m.dm_opt_out:
            return False
        team = rc.run_team(reg, ev)
        return await self.send_member_dm(reg, m, None, confirm_layout(reg, ev, team, self.ico, sg, roster_i, group_i, m.discord_id), ev=ev)

    async def send_confirmations(self, reg, rs, ev, team, by: str) -> int:
        sent = 0
        for i, r in enumerate(ev.all_rosters):
            for gi, g in enumerate(r.groups):
                for n in g:
                    sg = next((s for s in ev.signups.values() if s.display_name == n), None)
                    if sg and await self.confirm_one(reg, ev, sg, i, gi, by):
                        sent += 1
        ev.log.append(f"confirmation DMs sent to {sent}")
        rs.save(ev, f"confirmations {sent}")
        return sent

    async def after_board_change(self, reg, rs, ev, team, added, removed: list[str], by: str) -> None:
        """Officer edits on a locked board: substitutions get a confirmation DM, removals free the seat and ask the bench."""
        for sg in added:
            seat = ev.seat_of(sg.display_name)
            gi = next((k for k, g in enumerate(ev.all_rosters[seat[0]].groups) if sg.display_name in g), 0) if seat else 0
            await self.confirm_one(reg, ev, sg, seat[0] if seat else 0, gi, by)
        if added or removed:
            asks = " (asked to confirm)" if rc.run_confirms(reg, ev) else ""  # confirmations off (ad hoc, D5): nobody is asked
            await self.post_run_update(reg, ev, f"✏️ board by {by}: " + (f"in {', '.join(s.display_name for s in added)}{asks}" if added else "") + (" · " if added and removed else "") + (f"out {', '.join(removed)}" if removed else ""))
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if removed and rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=by)
            if sent:
                await self.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        await self.ops.emit(reg.config, "info", f"[web] {by} edited the {ev.key} board" + (f": +{len(added)}" if added else "") + (f" -{len(removed)}" if removed else ""))

    # ---- the board: one writer at a time per run, whoever writes (the site's drags, MCP, plain text)
    def board_lock(self, reg, key: str) -> asyncio.Lock:
        """One writer at a time per run's board: concurrent officers queue here (short), they never overwrite each other.
        Keyed (guild, run key)."""
        return self.__dict__.setdefault("_board_locks", {}).setdefault((reg.key, key), asyncio.Lock())

    async def _write_board(self, reg, rs, ev, groups: list[list[str]], by: str) -> str:
        """The one way a board is written (whole board, or the result of a single move); the caller holds board_lock.
        Before lock the groups are the layout the lock will use; after lock they ARE the roster (someone brought in
        from the bench is asked to confirm, someone taken out is freed and the fill engine asked)."""
        names = {s.display_name for s in ev.signups.values() if s.status == "in"}
        groups = [[n for n in g if n in names] for g in groups]
        if ev.state == "open":
            ev.layout = groups if any(groups) else None
            await asyncio.to_thread(rs.save, ev, f"board ({by})")
            return f"{run_title(reg, ev)}: board " + ("saved" if ev.layout else "cleared") + " (the lock uses it)"
        # on the loop, not a worker thread (stage C L3): a Can't make it, a fill yes or the scheduler's expiry changes the
        # same rosters on the loop, and a thread could put a seat freed meanwhile back; list work plus one batch commit
        added, removed = rc.apply_layout_locked(reg, rs, ev, groups)
        await self.after_board_change(reg, rs, ev, rc.run_team(reg, ev), added, removed, by)
        return ((f"asked {', '.join(s.display_name for s in added)} to confirm" if added else "") + (f"; freed {', '.join(removed)}" if removed else "")).lstrip("; ") or "groups updated"

    async def save_board(self, reg, rs, ev, groups: list[list[str]], *, rev: str | None = None, by: str) -> str:
        """A whole-board write (the site's Clear / Use this split, MCP set_layout, plain text run_board). `rev` = the
        rc.board_rev the author was looking at: when the board changed since, rc.BoardChanged is raised and nothing is
        written. Only joiners count; returns the line to show."""
        state = ev.state
        async with self.board_lock(reg, ev.key):
            if (rev and rev != rc.board_rev(reg, ev)) or ev.state != state:  # a write queued behind a lock is stale
                raise rc.BoardChanged()
            return await self._write_board(reg, rs, ev, groups, by)

    async def board_move(self, reg, rs, ev, name: str, to: tuple[int, int] | None, by: str, *, rev: str | None = None) -> str:
        """One move applied to the board as it is NOW (so two officers moving at once merge): `to` = (flat group
        index, position) — group index = roster × groups per roster + group — or None for the bank. Dropping on
        someone swaps (the displaced person stays a joiner). ValueError when the group is full or `name` hasn't joined.
        `rev`: plain text's preview fingerprint — the move was worded against that board, so a changed one raises
        rc.BoardChanged (the site's drags pass none: they always act on the board as it is)."""
        state = ev.state
        async with self.board_lock(reg, ev.key):
            if (rev and rev != rc.board_rev(reg, ev)) or ev.state != state:  # a move queued behind a lock is stale
                raise rc.BoardChanged()
            groups = rc.apply_move(reg, ev, name, to)
            return await self._write_board(reg, rs, ev, groups, by)

    # ---- a member's "can't make it" with a note, and fill answers from anywhere but the DM button
    async def call_out(self, reg, rs, ev, m, note: str | None, by: str) -> str:
        """Someone can't make a run, with an optional note for the officers (/raid out, the site's No thanks with a
        note, MCP set_answer(note), plain text): recorded as a callout (hours before, late after the lock cutoff); a
        rostered seat after lock is freed (drop_seated: DM, thread line, fill); otherwise the sheet is refreshed and
        one line goes to the run's updates thread — never the note in the public channel. Returns the line to show."""
        t = rc.run_team(reg, ev)
        if t.get("test") and not getattr(m, "test", False) and str(m.discord_id) != str(t.get("test_by") or ""):
            raise RegistryError(f"{run_title(reg, ev)} is a test run: only test members and the officer running it can answer it")
        co = rc.callout(reg, rs, ev, m, t, note)
        released = await self.release_asks_of(reg, rs, ev, m, "called out")
        when = f"{co.hours_before:.0f}h before{', after lock' if co.late else ''}"
        mine = by == m.display_name
        line = (f"Noted: out for {run_label(reg, ev)} ({when}). Thanks for saying so." if mine
                else f"{m.display_name} is out of {run_title(reg, ev)} ({when})" + (" — noted for the officers" if note else ""))
        if sheet_state(ev) == "locked" and ev.seat_of(m.display_name):
            await self.drop_seated(reg, rs, ev, t, m, "callout" if mine else "officer", note)
            return line + " — seat freed"
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.refill_after_release(reg, rs, ev, released)
        await self.post_run_update(reg, ev, f"⚑ {m.display_name} ({co.character}) called out, {co.hours_before:.0f}h before" + ("" if mine else f" (by {by})"))
        await self.ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {ev.key} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else "") + ("" if mine else f" (by {by})"))
        return line

    async def answer_fill_for(self, reg, rs, ev, m, yes: bool, by: str) -> str:
        """A fill ask's Confirm / Can't make it from anywhere but the DM button (an officer on the site or MCP, plain
        text's confirm_for): the button's two steps — rc.apply_fill_answer, then after_fill_answer (the tied partner
        released on a no, the next batch asked, thread line, sheet and cards). RegistryError when nothing is waiting."""
        ask = next((a for a in ev.fill_asks if a.discord_id == m.discord_id and a.open), None)
        if ask is None:
            raise RegistryError(f"no open fill ask for {m.display_name} on {run_title(reg, ev)}")
        line = rc.apply_fill_answer(reg, rs, ev, ask, bool(yes))
        await self.after_fill_answer(reg, rs, ev, ask, line + ("" if by == m.display_name else f" (answered by {by})"))
        return line

    async def repost_sheet(self, reg, rs, ev, channel) -> str:
        """Post a fresh copy of a live sheet in `channel` (/raid sheet, the site, MCP); the sheet's buttons and later
        refreshes follow the new message. No open DMs (that was the first posting)."""
        embed, view = sheet_message(reg, ev, rc.run_team(reg, ev), self.ico)
        msg = await channel.send(embed=embed, **({"view": view} if view else {}))
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")
        return f"Re-posted the sheet for {run_title(reg, ev)} in #{getattr(channel, 'name', channel.id)}."

    async def answer_placement_for(self, reg, uid: int, roster: str, yes: bool, by: str) -> str:
        """A Confirm / Can't make it answer from anywhere but the DM button itself (the Me page, an officer on the
        site or MCP, plain text's confirm_for): the button's two steps — `Registry.answer_placement`, then
        `after_placement_answer` (seat freed on a no, run thread line, sheet and cards). The button runs the same two
        with its interaction reply in between. Raises RegistryError when there is nothing to answer."""
        line = await asyncio.to_thread(reg.answer_placement, uid, roster, yes, by)
        await self.after_placement_answer(reg, uid, roster, yes, line)
        return line

    async def after_placement_answer(self, reg, uid: int, roster: str, yes: bool, line: str) -> None:
        cfg = reg.config
        rs = self.raids.store(reg)
        ev = next((e for e in rs.live() if e.team == roster), None)
        m = reg.members.get(uid)
        if ev and m and not yes:
            team = rc.run_team(reg, ev)
            await self.drop_seated(reg, rs, ev, team, m, "declined", None, announce=False)
        if ev:
            await self.post_run_update(reg, ev, f"{'✅' if yes else '↩️'} {line}")
            await self.refresh_sheet(reg, ev)
            await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info" if yes else "warn", line)

    async def dm_seat_released(self, reg, ev, m, why: str) -> bool:
        """One short DM whenever a member's seat on a locked roster is released, whoever released it."""
        reason = RELEASE_WHY.get(why, why)
        return await self.send_member_dm(reg, m, f"Your seat on {run_label(reg, ev)} was released ({reason}). Nothing else to do — if that's wrong, tell an officer.", ev=ev)

    async def drop_seated(self, reg, rs, ev, team, m, why: str, note: str | None, announce: bool = True) -> None:
        """A rostered member is out after lock: free the seat, tell them, post it, ask the bench to fill."""
        rc.free_seat(reg, rs, ev, m.display_name, why)
        await self.dm_seat_released(reg, ev, m, why)
        if announce:
            await self.post_run_update(reg, ev, f"↩️ {m.display_name} is out ({why}{f': {note}' if note else ''}) — seat freed")
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=why)
            if sent:
                await self.post_run_update(reg, ev, f"🧩 replacement for {m.display_name}:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))

    # ---- opening and cancelling runs (commands, the web and plain-text ops share these)
    async def open_run_and_post(self, reg, rs, instance: str, start: datetime, by: str, schedule: str | None = None) -> rc.RaidEvent:
        """Open (or find) the run for `instance` at `start` (from `schedule`, None = the raid's own cadence), post its
        sheet in the signup channel when one is set (else the event stays unposted: channel_id None, `/raid sheet` can
        place it), and say so on the ops feed. A start more than a few minutes in the past is refused
        (rc.refuse_past_start) — every on-demand open comes through here; the scheduler only opens future starts."""
        rc.refuse_past_start(reg, start)
        ev = rc.open_run(reg, rs, instance, start, by=by, schedule=schedule)
        channel = self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None
        if channel and not ev.message_id:
            await self.post_sheet(reg, rs, ev, channel)
        if not ev.health_early and not ev.health_posted and rc.early_warning(reg, rs, ev):
            officer_ch = self.officer_channel(reg, ev)
            if officer_ch is not None:
                await self.post_health(reg, rs, ev, officer_ch, nudge=False, early=True)
        await self.ops.emit(reg.config, "info", f"{by} opened {ev.key} ({len(ev.signups)} pre-filled out from absences)" + ("" if ev.message_id else " — no signup channel: sheet not posted"))
        return ev

    async def open_test_run(self, reg, rs, raid: str, *, start_in: int, nudge_in: int, lock_in: int, confirm_in: int, dm: bool, by: str, by_id: int | None,
                            channel=None) -> tuple[rc.RaidEvent, object]:
        """The one test-run opener (/gm test run, plain text's `test run …`, the site's test bench and so MCP): a real
        sheet with minute-level cadence (`*_in` = minutes; start > nudge > lock > confirm), the pool = puppets + the
        tester (`by_id`), `dm` = DM every puppet when it opens (the DMs reach the tester). The sheet goes to the signup
        channel, else to `channel` (where it was asked for; None = nowhere, e.g. the site); the ops feed gets the warn
        line. Returns (the run, the channel it was posted in or None). rc.open_run runs on the event loop, as in
        open_run_and_post: it adds to the live events the loop's handlers iterate, so never from a worker thread (L3);
        its one git commit is the same short block every on-demand open has."""
        start = (reg.now_local() + timedelta(minutes=start_in)).replace(second=0, microsecond=0)
        ev = rc.open_run(reg, rs, raid, start, by=by, cutoffs={"soft": nudge_in / 60, "hard": lock_in / 60, "confirm": confirm_in / 60, "open_dm": bool(dm), "test_by": by_id})
        target = self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else channel
        if target is not None and not ev.message_id:
            await self.post_sheet(reg, rs, ev, target)
        await self.ops.emit(reg.config, "warn", f"test bench: {by} opened test run {ev.key} (lock in {start_in - lock_in} min)")
        return ev, target

    async def cancel_run(self, reg, rs, ev, by: str, reason: str | None) -> str:
        """Cancel a run: state, sheet + officer cards re-rendered, open confirmations withdrawn, thread line, ops warn."""
        ev.state = "cancelled"
        ev.log.append(f"cancelled by {by}" + (f": {reason}" if reason else ""))
        rs.save(ev, "cancelled")
        waiting = rc.withdraw_confirmations(reg, ev, "cancelled") if ev.all_rosters else []
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"🛑 run cancelled by {by}" + (f": {reason}" if reason else "") + (f" · confirmations withdrawn for {len(waiting)}" if waiting else ""))
        await self.ops.emit(reg.config, "warn", f"{by} cancelled {ev.key}" + (f": {reason}" if reason else ""))
        return f"Cancelled {run_label(reg, ev)}" + (f": {reason}" if reason else "") + " — rostered members are not told automatically; say so in the channel."

    # ---- absences ripple into sheets
    async def after_absence_cleared(self, reg, m, a) -> list[str]:
        """The cleared absence's span: on every live open sheet the absence-sourced No thanks is dropped (they can
        answer again); on a locked run whose seat was handed back for it, say so — nobody is re-seated. Returns lines."""
        rs = self.raids.store(reg)
        lines = []
        for ev in rs.live():
            day = ev.start.astimezone(reg.tz).date().isoformat()
            if not (a.start <= day <= a.end) or m.absent_on(day):  # another absence still covers the day
                continue
            sg = ev.signups.get(str(m.discord_id))
            if not sg or sg.status != "out" or sg.source != "absence":
                continue
            label = run_label(reg, ev)
            if sheet_state(ev) == "open":
                del ev.signups[str(m.discord_id)]
                ev.log.append(f"{m.display_name}'s absence cleared: answer reset")
                rs.save(ev, f"{m.display_name} absence cleared")
                await self.refresh_sheet(reg, ev)
                await self.refresh_cards(reg, ev)  # the officers' No answer list, counts and Suggested block follow
                lines.append(f"{label}: can answer the sheet again")
            else:
                lines.append(f"{label}: the seat freed by the absence was not handed back")
        return lines

    async def after_absence(self, reg, m, start: str, end: str, by: str) -> list[str]:
        """Mark every live sheet inside the absence: open → out; locked and rostered → seat freed + fill."""
        rs = self.raids.store(reg)
        touched = []
        for ev in rc.runs_in_span(reg, rs, start, end):  # the same runs describe(absence) and MCP's preview list
            if not rc.on_bench_side(reg, ev, m):
                continue  # puppets belong to test runs only (and a real member to real runs, the tester aside)
            team = rc.run_team(reg, ev)
            sg = ev.signups.get(str(m.discord_id))
            released = await self.release_asks_of(reg, rs, ev, m, "away that day")  # before drop_seated, so its fill sees the seat free
            if ev.state != "open" and ev.seat_of(m.display_name):
                await self.drop_seated(reg, rs, ev, team, m, "absence", None)
            else:
                if not sg or sg.status != "out":
                    c = rc.expected_character(reg, ev, m)  # a regular is marked away on their regular character
                    if c:
                        rc.set_signup(reg, rs, ev, m, c.label, "out", source="absence")
                        await self.refresh_sheet(reg, ev)
                await self.refresh_cards(reg, ev)  # the officers' No answer list, counts and Suggested block follow
                await self.refill_after_release(reg, rs, ev, released)
            touched.append(ev.key)
        return touched
