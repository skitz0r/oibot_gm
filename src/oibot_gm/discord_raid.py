"""Discord surface for the signup-driven cycle — the `RaidMixin` on the client: DM routing (test puppets → the
tester), the sheet and officer cards kept current, set_answer (the one path for a member's or officer's answer),
lock_run → roster cards + Confirm/Can't DMs, the fill engine's DMs, drop_seated, opening / cancelling runs, and the
absence ripple. The rest of the surface lives next door and is re-exported here so older imports keep working:
`raid_views` (layouts + text helpers), `raid_buttons` (persistent buttons), `raid_scheduler` (RaidSchedulerMixin),
`raid_commands` (/raid …). `raid_views.BotProto` lists what the mixins expect from the client."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import discord

from . import raidcycle as rc
from .discord_registry import Guilds
from .raid_buttons import FillButton, PlaceButton, RunButton, SignupButton, fill_view, place_view, sheet_view
from .raid_commands import register_raid_commands
from .raid_scheduler import RaidSchedulerMixin
from .raid_views import (CLOCK12, OUT_MARK, RELEASE_WHY, BotProto, _aura_line, _group_block, _group_summaries, _header,
                         _raidwide_line, _role_counts, ask_line, board_url, clock12, closed_layout, confirm_layout, fill_layout,
                         gaps_text, health_layout, lock_layout, raid_name, run_actions_row, run_label, run_times, run_title, sheet_message,
                         sheet_state)
from .registry import Registry

log = logging.getLogger(__name__)

__all__ = ["RaidContext", "RaidMixin", "RaidSchedulerMixin", "BotProto", "register_raid_commands",
           # buttons
           "FillButton", "PlaceButton", "RunButton", "SignupButton", "fill_view", "place_view", "sheet_view",
           # views + helpers
           "CLOCK12", "OUT_MARK", "RELEASE_WHY", "ask_line", "board_url", "clock12", "closed_layout", "confirm_layout", "fill_layout", "gaps_text",
           "health_layout", "lock_layout", "raid_name", "run_actions_row", "run_label", "run_times", "run_title", "sheet_message", "sheet_state",
           "_aura_line", "_group_block", "_group_summaries", "_header", "_raidwide_line", "_role_counts"]


class RaidContext:
    """Per-guild raid store + helpers, attached to the bot."""

    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, rc.RaidStore] = {k: rc.RaidStore(guilds.store, reg.key) for k, reg in ((r.key, r) for r in guilds.by_discord.values())}

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

    async def send_member_dm(self, reg, m, text: str | None = None, view=None) -> bool:
        """DM a member — text + buttons, or a layout card (`view` a LayoutView, `text` None). For a test member the same
        goes to whoever is running the test, with the puppet's name in front. Returns True when delivered."""
        target, prefix = m.discord_id, None
        if getattr(m, "test", False):
            tester = next((t.get("test_by") for t in reg.config.rosters if t.get("test") and t.get("test_by")), None) or reg.config.owner_discord_id
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
                elif key.startswith("lock:"):
                    i = int(key[5:])
                    if i < len(ev.all_rosters):
                        await msg.edit(view=lock_layout(reg, ev, team, self.ico, i))
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
        label = run_label(reg, ev).replace("**", "")  # plain text: the line is shown in Discord replies and on the web alike
        who = "" if mine else f"{m.display_name}: "
        if sheet_state(ev) == "open":
            s = rc.set_signup(reg, rs, ev, m, character, status, source=source)
            await self.refresh_sheet(reg, ev)
            line = f"✅ {who}{s.character} {rc.LABELS[s.status]} for {label}" + (f" — {s.note}" if s.note and s.status != status else "")
        elif status == "out":
            if ev.seat_of(m.display_name):
                await self.drop_seated(reg, rs, ev, team, m, "callout" if mine else "officer", None)
                line = f"✅ {who or 'you are '}off the {label} roster — seat freed" + ("; the bot is looking for a replacement" if rc.team_setting(team, "autofill") else "")
            else:
                rc.set_signup(reg, rs, ev, m, character, "out", source=source)
                await self.refresh_sheet(reg, ev)
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
                    await self.post_run_update(reg, ev, f"✏️ {by} rostered {m.display_name} ({s.character})" + (" — asked to confirm" if sent else " — DMs off, the ask waits on the site" if m.dm_opt_out else ""))
                    line = f"✅ {who}{s.character} rostered for {label}" + (" — asked to confirm by DM" if sent else " — DMs off: the confirmation waits on the site" if m.dm_opt_out else "")
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
        """Optional per-run: DM every registered raider when the sheet opens, with the same buttons."""
        unix = int(ev.start.timestamp())
        _soft, hard, _confirm = run_times(reg, ev, team)
        sent = 0
        for m in reg.team_pool(team["key"]):
            if not m.main or m.dm_opt_out:
                continue
            s = ev.signups.get(str(m.discord_id))
            status = f"You're pre-filled as **{rc.LABELS.get(s.status, s.status)}** on {s.character}" if s else "You haven't answered yet"
            if await self.send_member_dm(reg, m, f"{reg.config.name} · {run_label(reg, ev)} (<t:{unix}:R>). {status}. Join / Bench / No thanks:" + (f" (sheet: <#{ev.channel_id}>)" if ev.channel_id else "") + f"\n-# The roster locks <t:{int(hard.timestamp())}:f>; no answer by then means you're not on it.", sheet_view(ev.key)):
                sent += 1
        ev.log.append(f"open DMs sent to {sent}")
        rs.save(ev, f"open DMs {sent}")
        return sent

    async def post_health(self, reg, rs, ev, channel, nudge: bool) -> None:
        team = rc.run_team(reg, ev)
        if channel is not None:
            try:
                msg = await channel.send(view=health_layout(reg, rs, ev, team, self.ico))
                ev.cards["health"], ev.cards_channel_id = msg.id, channel.id
            except Exception as e:  # noqa: BLE001
                log.warning("health card failed (%s): %s", ev.key, e)
        if nudge and rc.team_setting(team, "reminders") != "none":
            targets = [m for m in reg.team_pool(team["key"]) if m.main and str(m.discord_id) not in ev.signups and m.discord_id not in ev.nudged and not m.dm_opt_out]
            _soft, hard, _confirm = run_times(reg, ev, team)
            for m in targets:  # same buttons as the sheet (and the open DM), so they can answer right here
                if await self.send_member_dm(reg, m, f"{reg.config.name}: the {run_label(reg, ev)} sheet is still waiting for you — Join, Bench or No thanks here or in <#{ev.channel_id}>.\n-# The roster locks <t:{int(hard.timestamp())}:f>; no answer by then means you're not on it.", sheet_view(ev.key)):
                    ev.nudged.append(m.discord_id)
            if targets:
                rs.save(ev, f"nudged {len(targets)}")
        ev.health_posted = True
        rs.save(ev, "health posted")

    # ---- filling gaps by DM
    async def run_fill(self, reg, rs, ev, team, by: str = "scheduler") -> tuple[list, dict]:
        """Send the next batch of fill DMs. Returns (asks sent, needs). The fill engine only runs on a locked roster:
        before lock nothing is sent, whoever calls (scheduler, card button, /raid fill, the web board)."""
        nd = rc.needs(reg, ev, team)
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            return [], nd
        if not nd["headcount"] and not nd["roles"]:
            if ev.fill_state == "asking":
                ev.fill_state = "filled"
                rs.save(ev, "fill: complete")
            return [], nd
        batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
        sent = []
        deadline = rc.ask_deadline(reg, ev).isoformat()
        for ask in batch:  # tied pairs sit next to each other in the batch; both are on the event before either DM goes out
            ask.expires_at = deadline
            ev.fill_asks.append(ask)
        for ask in batch:
            m = reg.members.get(ask.discord_id)
            if m and await self.send_member_dm(reg, m, None, fill_layout(reg, ev, team, self.ico, ask)):
                sent.append(ask)
            else:
                ask.answer, ask.answered_at = "expired", rc.now()
                rc.release_partner(reg, rs, ev, ask)
        if sent:
            ev.fill_state = "asking"
            ev.log.append(f"fill ({by}): asked {', '.join(a.display_name for a in sent)}")
            rs.save(ev, f"fill asked {len(sent)}")
        elif not [a for a in ev.fill_asks if a.open]:
            ev.fill_state = "exhausted"
            rs.save(ev, "fill: nobody left to ask")
        return sent, nd

    async def withdraw_ask(self, reg, ev, ask, because: str) -> None:
        """Tell the other half of a tied pair the question is off."""
        m = reg.members.get(ask.discord_id)
        if m:
            await self.send_member_dm(reg, m, f"Never mind the {'swap' if ask.swap else 'seat'} question for **{reg.raid_def(ev.instance).get('name', ev.instance)}** <t:{int(ev.start.timestamp())}:F> — {because}. Nothing to do.")
        await self.post_run_update(reg, ev, f"↩️ withdrew the ask to {ask.display_name} ({because})")

    async def after_fill_answer(self, reg, rs, ev, ask, line: str) -> None:
        await self.refresh_sheet(reg, ev)
        cfg = reg.config
        team = rc.run_team(reg, ev)
        if ask.answer != "yes":
            other = rc.release_partner(reg, rs, ev, ask)
            if other:
                await self.withdraw_ask(reg, ev, other, f"{ask.display_name} said no")
        nd = rc.needs(reg, ev, team)
        await self.post_run_update(reg, ev, f"🧩 {line}" + (f" · still short {gaps_text(self.ico, nd)}" if nd["headcount"] or nd["roles"] else " · **gaps filled**"))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info", f"fill {ev.key}: {line}")
        if ask.answer == "yes" and ev.state != "open":
            m = reg.members.get(ask.discord_id)
            if m:
                try:
                    reg.roster_add(m.discord_id, ev.team, "fill", ask.character if ask.kind == "alt" else None)
                except Exception:  # noqa: BLE001
                    pass
                a = reg.add_placement_ask(m.discord_id, ev.team, ask.character, "fill")
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
        """Drop run rosters whose sheet is done/cancelled, and their memberships."""
        cfg = reg.config
        gone = []
        for t in list(cfg.rosters):
            if not t.get("ephemeral"):
                continue
            evs = [e for e in rs.events.values() if e.team == t["key"]]
            if evs and all(e.state in ("done", "cancelled") for e in evs):
                for m in list(reg.members.values()):
                    if reg.on_roster(m, t["key"]):
                        reg.roster_remove(m.discord_id, t["key"], "cleanup")
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
        if gone:
            reg.save_config(f"runs closed: {gone}", notify=False)

    # ---- lock → roster(s) → confirmations
    def officer_channel(self, reg, ev=None):
        """Where officer-facing posts go: the roster channel, else the ops channel, else nowhere (never the public sheet channel)."""
        cfg = reg.config
        return (self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else None) or (self.get_channel(cfg.ops_channel_id) if cfg.ops_channel_id else None)

    async def lock_run(self, reg, rs, ev, by: str = "scheduler") -> str:
        """Lock the sheet, build the roster(s) from the signups (pins honoured), show them on the sheet and in the
        officer channel, and DM every rostered member for confirmation."""
        team = rc.run_team(reg, ev)
        # solve on a copy first: the run only becomes locked once a roster exists, so a failed solve (or a crash
        # mid-way) leaves the sheet open rather than a locked run with no roster
        trial = ev.model_copy(deep=True)
        ev.lock_tried_at = rc.now()
        try:
            await asyncio.to_thread(rc.propose, reg, rs, trial, False)
        except Exception as e:  # noqa: BLE001 — solver infeasible / timed out
            why = rc.solver_error_text(e)
            first = ev.lock_error != why
            ev.lock_error = why
            ev.log.append(f"lock ({by}) failed: {why}")
            rs.save(ev, "lock failed")
            if first:
                await self.post_run_update(reg, ev, f"⚠️ lock by {by} failed — {why}. The sheet stays open; fix the board or the raid rules and lock again.")
            await self.ops.emit(reg.config, "warn", f"{ev.key}: lock failed — {why}")
            return f"{ev.key}: lock failed — {why}"
        _, _, confirm = run_times(reg, ev, team)
        ev.rosters, ev.log = trial.rosters, trial.log
        ev.state, ev.locked_at, ev.confirm_by, ev.lock_error = "locked", rc.now(), confirm.isoformat(), None
        ev.log.append(f"locked by {by}")
        rs.save(ev, f"locked by {by}")
        await self.refresh_sheet(reg, ev)
        ch = self.officer_channel(reg, ev)
        if ch:
            for i, r in enumerate(ev.all_rosters):
                try:
                    msg = await ch.send(view=lock_layout(reg, ev, team, self.ico, i))
                    ev.cards[f"lock:{i}"], ev.cards_channel_id = msg.id, ch.id
                except Exception as e:  # noqa: BLE001
                    log.warning("lock card failed (%s roster %d): %s", ev.key, i + 1, e)
            rs.save(ev, "lock cards")
        else:
            await self.ops.emit(reg.config, "warn", f"{ev.key}: no roster/ops channel set — lock cards not posted")
        sent = await self.send_confirmations(reg, rs, ev, team, by)
        sig = self.get_channel(ev.channel_id) if ev.channel_id else None
        if sig and sig is not ch:
            try:
                await sig.send(f"🔒 {run_label(reg, ev)} is locked: {sum(len(r.selected) for r in ev.all_rosters)} rostered" + (f" in {len(ev.all_rosters)} rosters" if len(ev.all_rosters) > 1 else "") + ". Rostered? confirm in your DMs. Not rostered? nothing to do.", allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:  # noqa: BLE001
                log.warning("lock line failed (%s): %s", ev.key, e)
        return f"{ev.key}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)"

    async def confirm_one(self, reg, ev, sg, roster_i: int, group_i: int, by: str) -> bool:
        """Seat one signup on the run's roster and ask them to confirm: by DM, or — DMs off — as a pending ask on the
        Me page (channel "web"). Never auto-confirmed: the tally counts them as waiting until they answer, and an
        unanswered ask expires like any other. Returns True when a DM went out."""
        m = reg.members.get(sg.discord_id)
        if not m:
            return False
        try:
            reg.roster_add(m.discord_id, ev.team, by, sg.character)
        except Exception:  # noqa: BLE001
            pass
        reg.add_placement_ask(m.discord_id, ev.team, sg.character, by, channel="web" if m.dm_opt_out else "dm")
        if m.dm_opt_out:
            return False
        team = rc.run_team(reg, ev)
        return await self.send_member_dm(reg, m, None, confirm_layout(reg, ev, team, self.ico, sg, roster_i, group_i, m.discord_id))

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
        for name in removed:
            m = next((mm for mm in reg.members.values() if mm.display_name == name), None)
            if m and reg.on_roster(m, ev.team):
                reg.roster_remove(m.discord_id, ev.team, by)
        if added or removed:
            await self.post_run_update(reg, ev, f"✏️ board by {by}: " + (f"in {', '.join(s.display_name for s in added)} (asked to confirm)" if added else "") + (" · " if added and removed else "") + (f"out {', '.join(removed)}" if removed else ""))
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if removed and rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=by)
            if sent:
                await self.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        await self.ops.emit(reg.config, "info", f"[web] {by} edited the {ev.key} board" + (f": +{len(added)}" if added else "") + (f" -{len(removed)}" if removed else ""))

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
        return await self.send_member_dm(reg, m, f"Your seat on {run_label(reg, ev)} was released ({reason}). Nothing else to do — if that's wrong, tell an officer.")

    async def drop_seated(self, reg, rs, ev, team, m, why: str, note: str | None, announce: bool = True) -> None:
        """A rostered member is out after lock: free the seat, tell them, post it, ask the bench to fill."""
        rc.free_seat(reg, rs, ev, m.display_name, why)
        if reg.on_roster(m, ev.team):
            reg.roster_remove(m.discord_id, ev.team, why)
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
    async def open_run_and_post(self, reg, rs, instance: str, start: datetime, by: str) -> rc.RaidEvent:
        """Open (or find) the run for `instance` at `start`, post its sheet in the signup channel when one is set
        (else the event stays unposted: channel_id None, `/raid sheet` can place it), and say so on the ops feed."""
        ev = rc.open_run(reg, rs, instance, start, by=by)
        channel = self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None
        if channel and not ev.message_id:
            await self.post_sheet(reg, rs, ev, channel)
        await self.ops.emit(reg.config, "info", f"{by} opened {ev.key} ({len(ev.signups)} pre-filled out from absences)" + ("" if ev.message_id else " — no signup channel: sheet not posted"))
        return ev

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
                lines.append(f"{label}: can answer the sheet again")
            else:
                lines.append(f"{label}: the seat freed by the absence was not handed back")
        return lines

    async def after_absence(self, reg, m, start: str, end: str, by: str) -> list[str]:
        """Mark every live sheet inside the absence: open → out; locked and rostered → seat freed + fill."""
        rs = self.raids.store(reg)
        touched = []
        for ev in rs.live():
            day = ev.start.astimezone(reg.tz).date().isoformat()
            if not (start <= day <= end):
                continue
            team = rc.run_team(reg, ev)
            sg = ev.signups.get(str(m.discord_id))
            if ev.state != "open" and ev.seat_of(m.display_name):
                await self.drop_seated(reg, rs, ev, team, m, "absence", None)
            elif not sg or sg.status != "out":
                if m.main:
                    rc.set_signup(reg, rs, ev, m, None, "out", source="absence")
                    await self.refresh_sheet(reg, ev)
            touched.append(ev.key)
        return touched
