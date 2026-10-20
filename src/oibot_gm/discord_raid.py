"""Discord surface for the signup-driven cycle — the `RaidMixin` on the client: DM routing (test puppets → one
digest in the tester's DMs + the test feed), the sheet and officer cards kept current, set_answer (the one path for a member's or officer's answer),
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

from . import guild_routing, ops as ops_mod, raidcycle as rc
from .discord_registry import Guilds
from .raid_buttons import FillButton, PlaceButton, RunButton, SignupButton, fill_view, place_view, sheet_view
from .raid_commands import register_raid_commands
from .raid_scheduler import RaidSchedulerMixin
from .raid_views import (CLOCK12, OUT_MARK, RELEASE_WHY, BotProto, _aura_line, _group_block, _group_summaries, _header,
                         _raidwide_line, _role_counts, ask_closed_layout, ask_line, board_url, clock12, closed_layout, confirm_layout, fill_layout,
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


NO_TEST_CHANNEL = ("No signup channel is set, so a test run's sheet has nowhere to go: set the signup channel (Config › Channels, "
                   "or /gm config signup-channel), or use /gm test run in a server channel.")
UNREACHABLE_TEST_CHANNEL = ("The signup channel is set but the bot can't reach it (deleted, or hidden from the bot), so a test run's sheet "
                            "has nowhere to go: pick the signup channel again (Config › Channels, or /gm config signup-channel), "
                            "or use /gm test run in a server channel.")


def bench_channel_refusal(reg, get_channel) -> str | None:
    """Why a test run with no channel of its own (the site, MCP, a DM) can't open, or None: the signup channel unset
    (NO_TEST_CHANNEL) or set to one the bot can't resolve (UNREACHABLE_TEST_CHANNEL). The site's Test bench card and
    open_test_run read the same answer."""
    sid = reg.config.signup_channel_id
    if not sid:
        return NO_TEST_CHANNEL
    return None if get_channel(int(sid)) is not None else UNREACHABLE_TEST_CHANNEL


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
        """DM a member — text + buttons, or a layout card (`view` a LayoutView, `text` None). A test member's DM is never
        sent on its own: it goes to the test feed and into ONE digest per batch in the tester's DMs (`ev`'s own tester
        when the DM is about a run, so two officers testing at once each get their own; puppet_dm). Returns True when delivered.
        While the process serves several guilds a text DM starts with the guild's name (raid_views.guild_prefix; a card
        names it in its header), unless the text already opens with it; with one guild nothing changes."""
        ok, _msg = await self.send_member_card(reg, m, text, view, ev=ev)
        return ok

    async def send_member_card(self, reg, m, text: str | None = None, view=None, ev=None) -> tuple[bool, object]:
        """send_member_dm, also handing back the message sent (or None when the client returns none) — an ask's DM card
        is stored by id on the ask (FillAsk.dm_*, a placement ask's `dm`) so it can be EDITED to its final state when the
        ask closes (answered elsewhere, expired, withdrawn, the run cancelled), never followed by another DM."""
        if text and not str(text).startswith(str(reg.config.name)):
            text = guild_prefix(reg) + text
        target, prefix = m.discord_id, None
        if getattr(m, "test", False):
            if self.PUPPET_DIGEST:  # a puppet: never its own DM — one digest per batch in the tester's DMs + the test feed
                return await self.puppet_dm(reg, m, text, view, ev)
            tester = self.puppet_tester(reg, ev)  # the per-puppet route (PUPPET_DIGEST off): the card itself, to the tester
            if not tester:
                return False, None
            target, prefix = tester, f"🧪 **{m.display_name}** would get:"
        try:
            user = await self.fetch_user(target)
            if isinstance(view, discord.ui.LayoutView):
                if prefix:
                    await user.send(prefix)
                msg = await user.send(view=view)
            else:
                msg = await user.send((prefix + "\n" + (text or "")) if prefix else (text or ""), view=view)
            return True, msg
        except Exception:  # noqa: BLE001
            return False, None

    # ---- the test bench's puppet DMs (dual interfaces phase 2, B9 / D20): a digest per batch, the feed per DM
    PUPPET_DIGEST = True  # off: each puppet DM reaches the tester as its own card (the pre-phase-2 route) — kept for tests that
    # use puppets as stand-ins for members' DM cards (edits on expiry / cancel / withdraw); the bot always digests
    DIGEST_QUIET_S = 2.0  # a batch ends when no puppet DM followed for this long (one fan-out: the lock's confirmations, a fill batch)
    DIGEST_MAX_S = 10.0  # …or this long after its first DM, whichever comes first

    def puppet_tester(self, reg, ev=None) -> int | None:
        """Who a puppet's DM reaches: the run's own tester (two officers rehearsing at once each get their own), else
        whoever runs a live test run, else the owner."""
        own = (reg.config.team(ev.team) or {}).get("test_by") if ev is not None else None
        tester = own or next((t.get("test_by") for t in reg.config.rosters if t.get("test") and t.get("test_by")), None) or reg.config.owner_discord_id
        return int(tester) if tester else None

    def _digests(self) -> dict:
        return self.__dict__.setdefault("_puppet_digests", {})

    async def puppet_dm(self, reg, m, text, view, ev=None) -> tuple[bool, None]:
        """A puppet's DM: what it would have got goes to the test feed (testfeed.note, kind dm) and is COUNTED into the
        tester's digest for this run — one DM per batch ("12 Confirm asks sent to puppets · 3 fill asks" + Answer as
        puppets…), never twelve. Delivered = counted (an ask to a puppet waits like any DM ask; it has no card to edit:
        the tester answers through the digest's button, /gm test answer or the site's Test bench). False only when
        nobody runs the bench (no tester, no owner)."""
        from . import testfeed

        tester = self.puppet_tester(reg, ev)
        if not tester:
            return False, None
        kind = testfeed.dm_kind(view)
        body = text if text else testfeed.view_text(view)
        first = next((ln for ln in testfeed.plain(reg, body, self._channel_name).splitlines() if ln.strip()), "") if body else ""
        word = testfeed.DM_WORDS[kind][0]
        testfeed.note(reg, ev, "dm", f"{m.display_name} would get ({word}): {first}" if first else f"{m.display_name} would get a {word}")
        loop = asyncio.get_running_loop()
        key = (reg.key, tester, ev.key if ev is not None else "")
        d = self._digests().get(key)
        if d is None:
            d = self._digests()[key] = {"reg": reg, "counts": {}, "names": [], "first": loop.time(), "last": loop.time(), "task": None}
        d["counts"][kind] = d["counts"].get(kind, 0) + 1
        d["names"].append(m.display_name)
        d["last"] = loop.time()
        if d["task"] is None:
            d["task"] = asyncio.ensure_future(self._digest_later(key))
        return True, None

    async def _digest_later(self, key) -> None:
        d = self._digests().get(key)
        loop = asyncio.get_running_loop()
        while d is not None:
            now = loop.time()
            if now - d["last"] >= self.DIGEST_QUIET_S or now - d["first"] >= self.DIGEST_MAX_S:
                break
            await asyncio.sleep(max(0.05, min(self.DIGEST_QUIET_S - (now - d["last"]), self.DIGEST_MAX_S - (now - d["first"]))))
        await self.send_digest(key)

    async def flush_puppet_digests(self) -> int:
        """Send every waiting digest now (shutdown, tests). Returns how many went."""
        n = 0
        for key in list(self._digests()):
            d = self._digests().get(key)
            task = d.get("task") if d else None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            if await self.send_digest(key):
                n += 1
        return n

    async def send_digest(self, key) -> bool:
        """One DM to the tester for a batch of puppet DMs: the counts in words, the puppets' names, and the Answer as
        puppets… button (flows_test.TestAnswerButton) while the run is live."""
        d = self._digests().pop(key, None)
        if not d or not d["counts"]:
            return False
        from . import testfeed

        reg, (gk, tester, run_key) = d["reg"], key
        ev = self.raids.store(reg).events.get(run_key) if run_key else None
        names = d["names"]
        who = ", ".join(names[:10]) + (f" +{len(names) - 10}" if len(names) > 10 else "")
        head = f"🧪 {guild_prefix(reg)}**{run_title(reg, ev)}** (test run) · " if ev is not None else f"🧪 {guild_prefix(reg)}"
        text = head + testfeed.digest_line(d["counts"]) + f"\n-# {who}\n-# Answer as them below, with `/gm test answer`, or on the site's Test bench (its feed shows each DM)."
        view = None
        if ev is not None and ev.state in ("open", "locked"):
            from .flows_test import TestAnswerButton

            view = discord.ui.View(timeout=None)
            view.add_item(TestAnswerButton(gk, run_key))
        try:
            user = await self.fetch_user(tester)
            await user.send(text[:2000], **({"view": view} if view else {}))
            return True
        except Exception as e:  # noqa: BLE001 — the feed has every DM; the digest is the nudge
            log.warning("puppet digest failed (%s): %s", run_key or "-", e)
            return False

    def _channel_name(self, cid: int | None) -> str | None:
        try:
            ch = self.get_channel(int(cid)) if cid else None
        except Exception:  # noqa: BLE001
            ch = None
        return getattr(ch, "name", None) if ch is not None else None

    async def edit_ask_card(self, reg, channel_id: int | None, message_id: int | None, text: str) -> bool:
        """Edit an ask's DM card to its final state — `text` (what happened, nothing to do) and no buttons. The DM rule's
        other half: an outcome reaches the person as the card edited, never as a new DM. False when the card is unknown
        (an ask sent before cards were stored, a client that returned no message) or Discord refused."""
        if not channel_id or not message_id:
            return False
        try:
            ch = self.get_channel(int(channel_id))
            if ch is None and hasattr(self, "fetch_channel"):
                ch = await self.fetch_channel(int(channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(view=ask_closed_layout(text))
            return True
        except Exception as e:  # noqa: BLE001 — the record is written; only the old card keeps its buttons
            log.debug("ask card edit failed (%s/%s): %s", channel_id, message_id, e)
            return False

    @staticmethod
    def card_ids(msg) -> tuple[int | None, int | None]:
        """(channel id, message id) of a sent DM card, or (None, None)."""
        if msg is None:
            return None, None
        ch = getattr(msg, "channel", None)
        return (getattr(ch, "id", None), getattr(msg, "id", None))

    # ---- officer cards + the run's updates thread
    async def post_run_update(self, reg, ev, text: str) -> None:
        """One line in the run's thread (under its health or lock card; created on first use); falls back to the officer channel.
        A test run's line also goes to the test feed (the site's Test bench follows the rehearsal)."""
        from . import testfeed

        testfeed.note(reg, ev, "step", text, name_of=self._channel_name)
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
            open_before = sheet_state(ev) == "open"
            line = await self.set_answer(reg, rs, ev, m, status, character, by=m.display_name, by_id=m.discord_id)
            if open_before and sheet_state(ev) == "open":  # the receipt renders state (design §2.7): what they are now, as which character
                line = "✅ " + rc.answer_receipt(reg, ev, m)
        except ValueError as e:
            line = f"❌ {e}"
        away = status == "in" and not line.startswith("❌") and m.absent_on(rc.run_day(reg, ev)) is not None
        if away:  # Join while away (design §3.2 B1): the receipt carries I'm back — the normal clear flow, whole span
            from .discord_pool import AbsenceButton

            view = discord.ui.View(timeout=None)
            view.add_item(AbsenceButton("clear", reg.key, m.discord_id, m.absent_on(rc.run_day(reg, ev)).start, label="I'm back"))
            await interaction.followup.send(line, view=view, ephemeral=True)
            return
        await interaction.followup.send(line, ephemeral=True)

    async def set_answer(self, reg, rs, ev, m, status: str, character: str | None, by: str, by_id: int | None = None) -> str:
        """The one path for setting someone's answer on a sheet — the member themself or an officer. `by` is a display
        name people read; `by_id` is the Discord id of whoever acts and DECIDES whose answer it is: the member's own when
        `by_id == m.discord_id` (never a name compare — nicks change, design §2.2). No `by_id` = an officer acting.
        Open sheet: the signup + sheet refresh. Locked: Join seats them (if not already) and asks them to confirm;
        No thanks frees a rostered seat (drop_seated: DM, thread line, fill); Bench is the signup only. Returns a line."""
        if status not in rc.STATUSES:
            raise ValueError(f"status must be one of {rc.STATUSES}")
        mine = by_id is not None and int(by_id) == int(m.discord_id)
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
            if ev.seat_of(m):
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
            seat = ev.seat_of(m)
            if seat:
                rc._reseat(ev, s, reg.key)  # a character swap keeps the seat
                rs.save(ev, f"{m.display_name} seat updated")
                line = f"✅ {who}{s.character} stays rostered for {label}"
            else:
                i = rc.seat_player(reg, ev, s)
                if i is None:
                    rs.save(ev, f"{m.display_name} in, no seat")
                    line = f"✅ {who}{s.character} Join for {label} — every seat is taken, so not rostered (move them in on the board)"
                else:
                    rs.save(ev, f"{m.display_name} rostered by {by}")
                    gi = next((k for k, g in enumerate(ev.all_rosters[i].groups) if s.display_name in g), 0)
                    sent = await self.confirm_one(reg, ev, s, i, gi, by)
                    asks = rc.run_confirms(reg, ev)
                    await self.post_run_update(reg, ev, f"✏️ {by} rostered {m.display_name} ({s.character})" + ("" if not asks else " — asked to confirm" if sent else " — DMs off, the ask waits on the site" if m.dm_opt_out else ""))
                    line = f"✅ {who}{s.character} rostered for {label}" + ("" if not asks else " — asked to confirm by DM" if sent else " — DMs off: the confirmation waits on the site" if m.dm_opt_out else "")
            await self.refresh_sheet(reg, ev)
            await self.refresh_cards(reg, ev)
        else:  # sub after lock: the signup only; a rostered member keeps the seat until set out
            s = rc.set_signup(reg, rs, ev, m, character, "sub", source=source)
            await self.refresh_sheet(reg, ev)
            line = f"✅ {who}{s.character} Bench for {label}" + (" — still rostered; set No thanks to free the seat" if ev.seat_of(m) else "")
        if status == "in":  # Join while away: Join stays pure, the absence stands for the other sheets — the receipt says so
            note = rc.join_while_away(reg, ev, m, mine=mine)
            if note:
                line += f"\n{note}"
        if not mine:
            await self.ops.emit(reg.config, "info", f"{by} set {m.display_name} {status} on {ev.key}")
            s = ev.signups.get(str(m.discord_id))  # B5: the member's notice (never a DM)
            rc.on_behalf_notice(reg, ev, m.discord_id, f"set you to {rc.LABELS[status]}" + (f" as {s.character}" if s and status != "out" else ""), by)
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
        for cid, mid in list(ev.mirror_messages):  # members' read-only copies (D21) follow the sheet, never with buttons
            try:
                copy, _v = sheet_message(reg, ev, team, self.ico)
                copy.set_footer(text=f"copy — the live sheet is in {self._live_sheet_name(ev)}")
                await (await self.get_channel(cid).fetch_message(mid)).edit(embed=copy, view=None)
            except Exception as e:  # noqa: BLE001 — a deleted copy just stops following
                log.debug("sheet copy refresh failed (%s %s): %s", ev.key, mid, e)

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
        engine never sends a role swap — owner decisions D3 / D22, every run kind: a swap waits for an
        officer's Ask (fill_state "suggested", the lock card lists it). `swaps=False` keeps role swaps out whoever
        sends (the site's / MCP's send without a preview may only send same-role asks). An officer's send from a
        screen goes through send_plan instead: the batch they were shown, nothing else.
        Every writer of ev.fill_asks holds plan_lock (design §2.4): the batch is derived INSIDE it, so an officer's Ask
        appended during this solve is counted, never asked for twice. `autofill_paused` (D18) is honoured here, so every
        automatic entry (the scheduler, a freed seat, an answer, the site's blind send) respects it while an officer's
        explicit send (`approved_by` set) still goes. Returns the asks that went out — by DM, or waiting on the site
        (DMs off, or the DM failed: a channel change, never an answer)."""
        nd = rc.needs(reg, ev, team)
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            return [], nd
        if datetime.now(ev.start.tzinfo) >= rc.run_close_at(reg, ev):
            return [], nd  # the raid is over (a loot council may keep the run locked): never a fill DM after it
        if ev.autofill_paused and approved_by is None:
            return [], nd  # paused for this run: only an officer's own send goes out
        async with self.plan_lock(reg, ev.key):
            if ev.state != "locked":  # cancelled / ended while waiting for the lock
                return [], nd
            nd = rc.needs(reg, ev, team)
            if not nd["headcount"] and not nd["roles"]:
                if ev.fill_state in ("asking", "suggested"):
                    ev.fill_state = "filled"
                    rs.save(ev, "fill: complete")
                return [], nd
            swaps = (approved_by is not None or rc.auto_swaps(reg, ev)) if swaps is None else swaps
            batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team, swaps=swaps)
            if ev.state != "locked":  # cancelled during the solve: nothing goes out for a cancelled run
                return [], nd
            sent = await self._send_asks(reg, rs, ev, team, batch, phase="fill", approved_by=approved_by, deadline=rc.ask_deadline(reg, ev).isoformat())
            if sent:
                if ev.state == "locked":  # a cancel during the fan-out: the run's fill state stays as the cancel left it
                    ev.fill_state = "asking"
                ev.log.append(f"fill ({by}): asked {', '.join(a.display_name + (' (site)' if a.channel == 'web' else '') for a in sent)}"
                              + ("" if ev.state == "locked" else f" — the run was {ev.state} meanwhile; nobody after that was asked"))
                rs.save(ev, f"fill asked {len(sent)}")
            elif batch and ev.state != "locked":
                rs.save(ev, f"fill stopped: the run is {ev.state}")  # the unsent asks closed by _send_asks
        if sent:
            return sent, nd
        if ev.state != "locked":
            return [], nd
        if not [a for a in ev.fill_asks if a.pending]:
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

    async def _send_asks(self, reg, rs, ev, team, asks: list, *, phase: str, approved_by: str | None, deadline: str) -> list:
        """Put `asks` on the run (tied pairs both on the event before either DM goes out), then DM each — or leave it
        waiting on the member's Me page (`channel` "web") when their DMs are off or the DM fails: same deadline, still in
        play (design B2: a failed DM is a CHANNEL change, never an answer — role_plan keeps counting it, the partner is
        not released, nobody is "expired"). Each DM card is stored on its ask (dm_channel_id / dm_message_id) so it can
        be edited when the ask closes. The caller holds plan_lock. Returns the asks now out (by DM or on the site).
        The run's state is re-checked before EVERY DM and after it (design §2.4): a cancel (or, for pre-lock asks, the
        lock) during the fan-out stops it — the asks not yet sent close (`cancelled`, or `released` when the run locked)
        and a card that went out while the run changed is edited at once, so nothing is left to press on a run that is
        no longer asking (cancel_run then takes plan_lock and closes the rest)."""
        stamp = rc.now()
        allowed = "open" if phase == "prelock" else "locked"
        for a in asks:
            a.phase, a.approved_by, a.expires_at, a.asked_at = phase, approved_by, deadline, stamp
            ev.fill_asks.append(a)
        out, failed = [], []
        for a in asks:
            if not a.pending:  # closed meanwhile (a lock released pre-lock asks while an earlier DM was in flight)
                continue
            if ev.state != allowed:  # the run changed during the fan-out: nothing more goes out
                self._close_unsent(ev, a)
                continue
            m = reg.members.get(a.discord_id)
            if m is None:  # the member record is gone: nobody to ask
                a.answer, a.answered_at = "expired", rc.now()
                rc.release_partner(reg, rs, ev, a)
                continue
            if a.channel == "web" or m.dm_opt_out:
                a.channel = "web"
                out.append(a)
                continue
            ok, msg = await self.send_member_card(reg, m, None, fill_layout(reg, ev, team, self.ico, a), ev=ev)
            if ok:
                a.dm_channel_id, a.dm_message_id = self.card_ids(msg)
                if ev.state != allowed or not a.pending:  # the run changed while this DM was in flight: the card says so now
                    self._close_unsent(ev, a)
                    await self.edit_ask_card(reg, a.dm_channel_id, a.dm_message_id, self._ask_closed_text(reg, ev, a))
                    continue
            else:
                a.channel = "web"
                failed.append(a)
            out.append(a)
        if failed:
            names = ", ".join(a.display_name for a in failed)
            ev.log.append(f"DM failed — waits on the site: {names}")
            await self.ops.emit(reg.config, "warn", f"{ev.key}: DM failed — waits on the site: {names}")
        return out

    @staticmethod
    def _close_unsent(ev, a) -> None:
        """An ask the fan-out did not (or could no longer) leave open: `cancelled` on a cancelled run, else `released`."""
        if a.pending:
            a.answer, a.answered_at, a.held = ("cancelled" if ev.state == "cancelled" else "released"), rc.now(), False

    @staticmethod
    def _ask_closed_text(reg, ev, a) -> str:
        label = run_label(reg, ev)
        if ev.state == "cancelled":
            return f"Cancelled — {label} was cancelled. Nothing to do."
        return f"Never mind the {'swap' if a.swap else 'seat'} question for {label} — the run is {ev.state} now. Nothing to do."

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
                # two officers pressed Ask on the same plan: the second is told who sent it, then sees the redrawn plan
                recent = [a for a in ev.fill_asks if a.approved_by and a.approved_by != by and a.asked_at
                          and datetime.now(ev.start.tzinfo) - datetime.fromisoformat(a.asked_at) < timedelta(minutes=10)]
                if recent:
                    last = recent[-1]
                    raise rc.PlanChanged(plan, f"Already sent by {last.approved_by} at {reg.local12(last.asked_at, '%I:%M %p')} — the plan is redrawn; check it and send again if more is needed.")
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
            sent = await self._send_asks(reg, rs, ev, team, asks, phase=plan.phase, approved_by=by, deadline=deadline)
            if sent and not prelock and ev.state == "locked":  # a cancel during the fan-out keeps its own fill state
                ev.fill_state = "asking"
            ev.log.append(f"{'suggestions' if prelock else 'fill'} sent by {by}: " + (", ".join(a.display_name + (" (site)" if a.channel == "web" else "") for a in sent) or "nobody to ask"))
            rs.save(ev, f"{'suggestions' if prelock else 'fill'} sent by {by}")
        if sent:
            await self.post_run_update(reg, ev, f"🧩 {by} asked:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent)
                                       + (f"\n-# {plan.held_back} more wait — at most {rc.FILL_MAX_OPEN} asks out at once; the card keeps them" if plan.held_back else ""))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(reg.config, "info", f"{by} sent {len(sent)} {'pre-lock ' if prelock else ''}ask(s) on {ev.key}")
        return sent, plan

    async def withdraw_sent_ask(self, reg, rs, ev, uid: int, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """An officer closes a sent ask early (design B2 "Withdraw this ask", D15; the Suggest… screen, the site's Detail):
        under plan_lock, the member's ask still in play is `withdrawn`, its tied half released, both DM cards edited to
        "Never mind" (no new DM), a thread line, the cards redrawn. The seat it was holding counts as open again (the
        planner suggests someone else; after lock the fill engine asks on its next pass unless paused). RegistryError
        when nothing is out for them."""
        async with self.plan_lock(reg, ev.key):
            ask = next((a for a in ev.fill_asks if a.discord_id == int(uid) and a.pending), None)
            if ask is None:
                m = reg.members.get(int(uid))
                raise RegistryError(f"No ask is out for {m.display_name if m else 'them'} on {run_title(reg, ev)}.")
            ask.answer, ask.answered_at, ask.held, ask.answered_by = "withdrawn", rc.now(), False, by
            ev.log.append(f"{'suggestion' if ask.phase == 'prelock' else 'fill'}: {ask.display_name}'s ask withdrawn by {by}")
            other = rc.release_partner(reg, rs, ev, ask, because=f"{ask.display_name}'s ask was withdrawn")
            rs.save(ev, f"ask to {ask.display_name} withdrawn by {by}")
        why = f"withdrawn by an officer ({by})"
        await self.edit_ask_card(reg, ask.dm_channel_id, ask.dm_message_id, f"Never mind the {'swap' if ask.swap else 'seat'} question for {run_label(reg, ev)} — {why}. Nothing to do.")
        if other is not None:
            await self.withdraw_ask(reg, ev, other, f"{ask.display_name}'s half of the swap was withdrawn")
        await self.post_run_update(reg, ev, f"↩️ {by} withdrew the ask to {ask.display_name}")
        await self.refresh_cards(reg, ev)
        await ops_mod.audited(self.ops, reg, by, f"withdrew the ask to {ask.display_name} on {ev.key}", via=via, by_id=by_id)
        return f"Withdrew the ask to {ask.display_name}" + (f" (and {other.display_name}'s tied half)" if other is not None else "") + "."

    async def close_prelock_asks(self, reg, rs, ev) -> list:
        """Lock time reached: pre-lock asks still out close (`released`, as lock_run does — a lock that keeps failing
        leaves none open); a yes held for a tied partner who never answered is told nothing changed."""
        held = [a for a in ev.fill_asks if a.held and a.phase == "prelock"]
        gone = rc.release_prelock_asks(ev)
        if gone:
            rs.save(ev, "pre-lock asks closed at lock time")
        for a in held:
            await self.withdraw_ask(reg, ev, a, "the roster locks before the other half of the swap was answered")
        for a in gone:  # every other unanswered "Could you come?" card: its final state, no buttons
            if a not in held:
                await self.close_ask_card(reg, ev, a, "the roster locked before you answered")
        return gone

    async def release_asks_of(self, reg, rs, ev, m, why: str) -> bool:
        """`m` can't come any more (No thanks, a callout, an absence): close their asks still in play (rc.release_member_asks)
        and tell the tied half of each pair it's off. The fill engine's next batch is the caller's (drop_seated, or
        refill_after_release) — so a seat their ask was holding is asked for again. True when an ask was closed."""
        mine = [a for a in ev.fill_asks if a.discord_id == m.discord_id and a.pending]
        had = bool(mine)
        partners = rc.release_member_asks(reg, rs, ev, m.discord_id, why)
        for a in mine:  # their own card: edited to its final state too (it used to keep live Yes / No)
            if a.answer == "released":
                await self.close_ask_card(reg, ev, a, f"closed ({why})")
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
        """Tell the other half of a tied pair the question is off: their ask card is EDITED to say so (the DM rule — an
        outcome is the card in its final state, never a DM; the ask's own record carries it to the Me page). An ask
        whose card is unknown (sent before cards were stored) gets no DM either: a press on that old card is refused as
        closed, which says the same."""
        await self.close_ask_card(reg, ev, ask, because)
        await self.post_run_update(reg, ev, f"↩️ withdrew the ask to {ask.display_name} ({because})")

    async def close_ask_card(self, reg, ev, ask, because: str) -> bool:
        """Edit one fill / pre-lock ask's DM card to its final state ("Never mind … — <because>. Nothing to do.", no
        buttons): every path that closes an ask without the member's press (their own No thanks / callout / absence,
        expiry, the lock closing pre-lock asks, a withdrawn partner) ends the card this way — never a new DM."""
        text = f"Never mind the {'swap' if ask.swap else 'seat'} question for **{reg.raid_def(ev.instance).get('name', ev.instance)}** <t:{int(ev.start.timestamp())}:F> — {because}. Nothing to do."
        return await self.edit_ask_card(reg, ask.dm_channel_id, ask.dm_message_id, text)

    async def after_fill_answer(self, reg, rs, ev, ask, line: str) -> None:
        await self.refresh_sheet(reg, ev)
        cfg = reg.config
        team = rc.run_team(reg, ev)
        if ask.answer != "yes":
            other = rc.release_partner(reg, rs, ev, ask)
            if other:
                await self.withdraw_ask(reg, ev, other, f"{ask.display_name} said no")
        else:  # D14: a parallel ask the yes closed (rc.close_parallel) — its DM card says so, buttons gone
            for tw in rc.parallel_linked(ev, ask):
                if tw.answer == "released":
                    await self.edit_ask_card(reg, tw.dm_channel_id, tw.dm_message_id, f"Never mind the seat question for {run_label(reg, ev)} — someone else said yes first. Nothing to do.")
        if ask.held:  # the first yes of a tied pair: nothing changed yet, the partner's answer decides
            await self.post_run_update(reg, ev, f"🧩 {line}")
            await self.refresh_cards(reg, ev)
            await self.ops.emit(cfg, "info", f"fill {ev.key}: {line}")
            return
        both = rc.tied_ask(ev, ask) if ask.answer == "yes" else None
        both = both if both is not None and both.answer == "yes" else None  # the partner's held yes took effect just now
        if both is not None:  # the DM rule: the partner's own card is edited to the outcome, never a courtesy DM
            await self.edit_ask_card(reg, both.dm_channel_id, both.dm_message_id,
                                     f"{ask.display_name} said yes too, so it's on for {run_label(reg, ev)}: you're down as **{both.character}** ({both.spec})"
                                     + (" — the lock builds the roster." if ev.state == "open" else ".") + " Nothing more to do.")
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
                if m and ev.seat_of(m):  # a yes that found no free seat is a Join, not a rostered seat
                    a = reg.add_placement_ask(m.discord_id, ev.team, done.character, "fill")
                    a["answer"], a["answered_at"] = "yes", rc.now()
                    reg.save(m, f"{m.display_name} rostered on {ev.team} via fill")
        if ask.answer == "no":
            await self.run_fill(reg, rs, ev, team, by="answer")

    async def is_officer_anywhere(self, reg, uid: int) -> bool:
        """Officer or owner of `reg` by Discord id (a button press, a DM) — an alias of guild_routing.role_of (design §2.6)."""
        return await guild_routing.role_of(self, reg, uid, max_age=guild_routing.PRIV_ROUTING) in ("owner", "officer")

    async def cleanup_ephemeral(self, reg, rs) -> None:
        """Drop run rosters whose sheet is done/cancelled (who held a seat stays in the run file; no member file
        carries a run key any more) — a run ENDED keeps its entry for a grace hour (rc.REOPEN_GRACE_HOURS), so one ended
        by mistake can be reopened with its cutoffs and test flags intact (design B6); a cancelled run is never reopened
        (D19), so its entry goes at once."""
        cfg = reg.config
        gone = []
        for t in list(cfg.rosters):
            if not t.get("ephemeral"):
                continue
            evs = [e for e in rs.events.values() if e.team == t["key"]]
            if evs and all(e.state == "cancelled" or (e.state == "done" and not rc.closed_recently(e)) for e in evs):
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
        if gone:
            reg.save_config(f"runs closed: {gone}", notify=False)

    # ---- lock → roster(s) → confirmations
    def officer_channel(self, reg, ev=None):
        """Where officer-facing posts go: the roster channel, else the ops channel, else nowhere (never the public sheet channel).
        A TEST run's cards go to the channel its /gm test run was used in (D20: the rehearsal stays where it was asked
        for, with the test badge; a DM can't host the updates thread, so a run opened from a DM or the site uses the
        officer channel as any run does)."""
        cfg = reg.config
        own = (cfg.team(ev.team) or {}).get("test_channel") if ev is not None else None
        if own and (ch := self.get_channel(int(own))) is not None:
            return ch
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

    async def lock_preview(self, reg, rs, ev, *, by: str, by_id: int | None = None) -> list:
        """The roster(s) the lock would build NOW (rc.split_preview with the run's strategy, honouring the board and the
        pins), under the run's solver single flight — the Discord Preview lock and the site's lock preview share it.
        Nothing about the run changes but `ev.last_proposal` (the layout shown, with who saw it: the same officer's plain
        text "use it" puts it on the board, also after a restart — configops._propose). RegistryError (SolverBusy, or the
        solver's own sentence) when no preview can be made."""
        if ev.state != "open":
            raise RegistryError(f"{run_title(reg, ev)} is {ev.state} — the preview is for an open sheet.")
        strategy = ev.split_strategy or reg.raid_def(ev.instance).get("split_policy") or "balanced"
        async with self.solving(reg, ev.key):
            try:
                layout, rosters = await asyncio.to_thread(rc.split_preview, reg, rs, ev, strategy)
            except RuntimeError as e:
                raise RegistryError(rc.solver_error_text(e)[:1].upper() + rc.solver_error_text(e)[1:]) from e
        ev.last_proposal = {"layout": layout, "strategy": strategy, "rev": rc.board_rev(reg, ev), "by": by, "by_id": by_id, "at": rc.now(), "kind": "lock preview"}
        await asyncio.to_thread(rs.save, ev, f"lock preview by {by}")
        return rosters

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
                    await self.refresh_sheet(reg, ev)  # the sheet's footer says so too: the signup channel's truth matches the thread's
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
            pre_gone = rc.release_prelock_asks(ev)  # pre-lock asks expire at lock time (a yes held for a tied partner too)
            short = rc.needs(reg, ev, team)["roles"]
            if short:  # the solver caps a minimum at what the sheet can supply: say so instead of locking quietly
                ev.log.append("locked short " + ", ".join(f"{k} {r}" for r, k in short.items()))
            rs.save(ev, f"locked by {by}")  # flipped AND committed before anything is posted or sent: resumable (recover_lock)
        confirming = self._confirming()
        confirming.add((reg.key, ev.key))  # the scheduler's recovery step leaves the run alone while this fan-out runs
        try:
            if short:
                await self.ops.emit(reg.config, "warn", f"{ev.key}: locked short " + ", ".join(f"{k} {r}" for r, k in short.items()))
            for a in held:  # they said yes, but their partner never answered: nothing changed for them
                await self.withdraw_ask(reg, ev, a, "the roster locked before the other half of the swap was answered")
            for a in pre_gone:  # the unanswered pre-lock asks: each card edited to its final state (no live Yes / No)
                if a not in held:
                    await self.close_ask_card(reg, ev, a, "the roster locked before you answered")
            await self.refresh_sheet(reg, ev)
            if "health" in ev.cards:  # the open sheet's card: its Suggested block and Ask go; it points at the lock card
                await self.refresh_cards(reg, ev)
            ch = self.officer_channel(reg, ev)
            if ch:
                await self.post_lock_cards(reg, rs, ev, team, ch)
            else:
                await self.ops.emit(reg.config, "warn", f"{ev.key}: no roster/ops channel set — lock cards not posted")
            sent = await self.send_confirmations(reg, rs, ev, team, by) if confirms and ev.state == "locked" else 0
        finally:
            confirming.discard((reg.key, ev.key))
        if ev.state == "locked":  # joiners left off: one notice each (D26), never a DM — a seat offered later is the DM
            rc.leftover_notices(reg, ev)  # on the loop: member files are mutated where the buttons mutate them
        if ev.state != "locked":  # cancelled during the fan-out: the cancel carried its own lines; no "is locked" line now
            return f"{ev.key}: locked by {by}, then {ev.state} before the confirmations finished ({sent} sent)"
        if confirms and ev.state == "locked":
            await self.refresh_cards(reg, ev)  # the lock card's "confirmations not yet sent" line goes
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
        if ev.state != "locked":  # cancelled / ended meanwhile: never an ask for a run that is not locked
            return False
        seat = ev.seat_of(sg.discord_id)  # re-read NOW: a board move / Can't / absence may have freed it since the
        if seat is None:                  # caller listed the seats (the lock's fan-out runs outside board_lock)
            return False
        roster_i = seat[0]
        group_i = next((k for k, g in enumerate(ev.all_rosters[roster_i].groups)
                        if seat[1].signup_name in g or sg.display_name in g), group_i)
        if any(a["roster"] == ev.team and a.get("answer") is None for a in m.placement_asks):
            return False  # already asked and not answered: the card they hold still works (a re-ask would reset it)
        # the seat itself is in the run file (ev.rosters); the member file only gets the confirmation ask
        ask = reg.add_placement_ask(m.discord_id, ev.team, sg.character, by, channel="web" if m.dm_opt_out else "dm")
        if m.dm_opt_out:
            return False
        team = rc.run_team(reg, ev)
        ok, msg = await self.send_member_card(reg, m, None, confirm_layout(reg, ev, team, self.ico, sg, roster_i, group_i, m.discord_id), ev=ev)
        cid, mid = self.card_ids(msg)
        if ok and mid:
            ask["dm"] = [cid, mid]
            reg.save(m, f"{m.display_name}'s confirmation card for {ev.team}")
        elif not ok:  # a failed DM is a channel change, never an answer: the ask waits on the site, same deadline
            ask["channel"] = "web"
            reg.save(m, f"{m.display_name}'s confirmation for {ev.team} waits on the site (DM failed)")
            ev.log.append(f"DM failed — waits on the site: {m.display_name}'s confirmation")
        return ok

    async def send_confirmations(self, reg, rs, ev, team, by: str, *, resume: bool = False) -> int:
        """The lock's Confirm / Can't fan-out (confirm_one per seat), re-checking the run is still locked before every
        ask (a cancel during the fan-out leaves no orphan asks). `resume`: the scheduler's recovery step — only the seats
        nobody asked (rc.unasked_seats). Stamps `confirmations_sent_at` once every seat has been reached."""
        sent = 0
        seats = rc.unasked_seats(reg, ev) if resume else [
            (i, gi, ev.signups.get(str(uid))) for i, r in enumerate(ev.all_rosters) for gi, g in enumerate(r.groups) for n in g
            if (uid := rc.uid_of_name(ev, n)) is not None]
        for i, gi, sg in seats:
            if ev.state != "locked":
                break
            if sg and await self.confirm_one(reg, ev, sg, i, gi, by):
                sent += 1
        if ev.state == "locked":
            ev.confirmations_sent_at = rc.now()
        ev.log.append(f"confirmation DMs sent to {sent}" + (" (resumed after an interrupted lock)" if resume else ""))
        rs.save(ev, f"confirmations {sent}")
        return sent

    def _confirming(self) -> set:
        """(guild key, run key) whose lock fan-out is running in this process right now (recover_lock skips them)."""
        return self.__dict__.setdefault("_confirming_runs", set())

    async def post_lock_cards(self, reg, rs, ev, team, ch, only_missing: bool = False) -> int:
        """One lock card per roster in the officer channel (`only_missing`: just the rosters with no card yet — the
        recovery step re-posts what an interrupted lock never posted). Returns how many were posted."""
        posted = 0
        for i, _r in enumerate(ev.all_rosters):
            if only_missing and f"lock:{i}" in ev.cards:
                continue
            try:
                msg = await ch.send(view=lock_layout(reg, ev, team, self.ico, i, rs))
                ev.cards[f"lock:{i}"], ev.cards_channel_id = msg.id, ch.id
                posted += 1
            except Exception as e:  # noqa: BLE001
                log.warning("lock card failed (%s roster %d): %s", ev.key, i + 1, e)
        if posted or not only_missing:
            rs.save(ev, "lock cards")
        return posted

    async def recover_lock(self, reg, rs, ev) -> int:
        """The scheduler's recovery step for a lock interrupted after the flip (design B6 "lock is resumable"): re-post
        missing lock cards; and for a locked run whose fan-out never finished (no `confirmations_sent_at`) and is not
        running in this process, ask exactly the seats nobody asked (rc.unasked_seats; confirm_one skips anyone already
        asked) while the confirmation deadline is ahead. Returns how many were asked."""
        if ev.state != "locked" or not ev.all_rosters or (reg.key, ev.key) in self._confirming():
            return 0
        team = rc.run_team(reg, ev)
        ch = self.officer_channel(reg, ev)
        if ch is not None and any(f"lock:{i}" not in ev.cards for i in range(len(ev.all_rosters))):
            await self.post_lock_cards(reg, rs, ev, team, ch, only_missing=True)
        if not rc.run_confirms(reg, ev) or ev.confirmations_sent_at:
            return 0  # the fan-out finished (every later seat — a board add, a Join, a fill yes — asks on its own)
        _soft, _hard, confirm = run_times(reg, ev, team)
        missing = rc.unasked_seats(reg, ev)
        if not missing:
            if not ev.confirmations_sent_at:
                ev.confirmations_sent_at = rc.now()
                rs.save(ev, "confirmations: every seat asked")
            return 0
        if datetime.now(ev.start.tzinfo) >= confirm:
            return 0  # past the deadline: asking now would expire at once; the officers see the unasked on the card
        sent = await self.send_confirmations(reg, rs, ev, team, "recovery", resume=True)
        await self.post_run_update(reg, ev, f"🔁 the lock's confirmations were interrupted — asked the {len(missing)} not yet asked")
        await self.ops.emit(reg.config, "warn", f"{ev.key}: lock recovery — {len(missing)} rostered had no confirmation ask; asked them ({sent} by DM)")
        await self.refresh_cards(reg, ev)
        return len(missing)

    async def after_board_change(self, reg, rs, ev, team, added, removed: list[str], by: str, via: str = "web") -> None:
        """Officer edits on a locked board: substitutions get a confirmation DM, removals free the seat and ask the bench.
        `via` = the surface the edit came through (web | mcp | chat | discord): the audit line's prefix, never part of the verb."""
        for sg in added:
            seat = ev.seat_of(sg.discord_id)
            gi = next((k for k, g in enumerate(ev.all_rosters[seat[0]].groups) if sg.display_name in g), 0) if seat else 0
            await self.confirm_one(reg, ev, sg, seat[0] if seat else 0, gi, by)
        for name in removed:  # their Confirm card says the seat went (free_seat closed the ask and wrote the notice)
            uid = rc.uid_of_name(ev, name)
            m = reg.members.get(uid) if uid is not None else None
            if m is not None:
                await self.seat_released(reg, ev, m, "board")
        if added or removed:
            asks = " (asked to confirm)" if rc.run_confirms(reg, ev) else ""  # confirmations off (ad hoc, D5): nobody is asked
            await self.post_run_update(reg, ev, f"✏️ board by {by}: " + (f"in {', '.join(s.display_name for s in added)}{asks}" if added else "") + (" · " if added and removed else "") + (f"out {', '.join(removed)}" if removed else ""))
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if removed and rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=by)
            if sent:
                await self.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        await ops_mod.audited(self.ops, reg, by, f"edited the {ev.key} board" + (f": +{len(added)}" if added else "") + (f" -{len(removed)}" if removed else ""), via=via)

    # ---- the board: one writer at a time per run, whoever writes (the site's drags, MCP, plain text)
    def board_lock(self, reg, key: str) -> asyncio.Lock:
        """One writer at a time per run's board: concurrent officers queue here (short), they never overwrite each other.
        Keyed (guild, run key)."""
        return self.__dict__.setdefault("_board_locks", {}).setdefault((reg.key, key), asyncio.Lock())

    async def _write_board(self, reg, rs, ev, groups: list[list[str]], by: str, via: str = "web") -> str:
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
        await self.after_board_change(reg, rs, ev, rc.run_team(reg, ev), added, removed, by, via=via)
        return ((f"asked {', '.join(s.display_name for s in added)} to confirm" if added else "") + (f"; freed {', '.join(removed)}" if removed else "")).lstrip("; ") or "groups updated"

    async def save_board(self, reg, rs, ev, groups: list[list[str]], *, rev: str | None = None, by: str, via: str = "web",
                         strategy: str | None = None) -> str:
        """A whole-board write (the site's Clear / Use this split, MCP set_layout, plain text run_board). `rev` = the
        rc.board_rev the author was looking at: when the board changed since, rc.BoardChanged is raised and nothing is
        written. Only joiners count; returns the line to show. `via`: the surface, for the audit line's prefix.
        `strategy`: the split philosophy the layout came from (Use this split) — written in the SAME step, under the same
        lock and rev check, so a strategy can't be saved while its layout is refused (design B6, one request)."""
        state = ev.state
        async with self.board_lock(reg, ev.key):
            if (rev and rev != rc.board_rev(reg, ev)) or ev.state != state:  # a write queued behind a lock is stale
                raise rc.BoardChanged()
            if strategy:
                ev.split_strategy = strategy
            return await self._write_board(reg, rs, ev, groups, by, via=via)

    async def board_move(self, reg, rs, ev, name: str, to: tuple[int, int] | None, by: str, *, rev: str | None = None, via: str = "web",
                         expect_state: str | None = None) -> str:
        """One move applied to the board as it is NOW (so two officers moving at once merge): `to` = (flat group
        index, position) — group index = roster × groups per roster + group — or None for the bank. Dropping on
        someone swaps (the displaced person stays a joiner). ValueError when the group is full or `name` hasn't joined.
        `rev`: plain text's preview fingerprint — the move was worded against that board, so a changed one raises
        rc.BoardChanged (the site's drags pass none: they always act on the board as it is). `expect_state`: the state
        the board was RENDERED in (the site sends it): a drag made on the open board and dropped after the scheduler
        locked it is refused (BoardChanged) instead of being applied as a post-lock roster edit (a confirmation DM, a
        freed seat) — design B6."""
        state = ev.state
        async with self.board_lock(reg, ev.key):
            if (rev and rev != rc.board_rev(reg, ev)) or ev.state != state or (expect_state and ev.state != expect_state):
                raise rc.BoardChanged("The run locked while you were dragging — the board is the roster now; check it and move again." if expect_state == "open" and ev.state != "open" else rc.BoardChanged().args[0])
            groups = rc.apply_move(reg, ev, name, to)
            return await self._write_board(reg, rs, ev, groups, by, via=via)

    # ---- a member's "can't make it" with a note, and fill answers from anywhere but the DM button
    async def call_out(self, reg, rs, ev, m, note: str | None, by: str, by_id: int | None = None) -> str:
        """Someone can't make a run, with an optional note for the officers (/raid out, the site's No thanks with a
        note, MCP set_answer(note), plain text): recorded as a callout (hours before, late after the lock cutoff); a
        rostered seat after lock is freed (drop_seated: DM, thread line, fill); otherwise the sheet is refreshed and
        one line goes to the run's updates thread — never the note in the public channel. `by_id` (the actor's Discord
        id) decides whether it is the member's own callout, as in set_answer. Returns the line to show."""
        t = rc.run_team(reg, ev)
        if t.get("test") and not getattr(m, "test", False) and str(m.discord_id) != str(t.get("test_by") or ""):
            raise RegistryError(f"{run_title(reg, ev)} is a test run: only test members and the officer running it can answer it")
        co = rc.callout(reg, rs, ev, m, t, note)
        released = await self.release_asks_of(reg, rs, ev, m, "called out")
        when = f"{co.hours_before:.0f}h before{', after lock' if co.late else ''}"
        mine = by_id is not None and int(by_id) == int(m.discord_id)
        line = (f"Noted: out for {run_label(reg, ev)} ({when}). Thanks for saying so." if mine
                else f"{m.display_name} is out of {run_title(reg, ev)} ({when})" + (" — noted for the officers" if note else ""))
        if not mine:  # B5: the member's notice (never a DM); the note stays officers'
            rc.on_behalf_notice(reg, ev, m.discord_id, "recorded that you can't make it", by)
        if sheet_state(ev) == "locked" and ev.seat_of(m):
            await self.drop_seated(reg, rs, ev, t, m, "callout" if mine else "officer", note)
            return line + " — seat freed"
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.refill_after_release(reg, rs, ev, released)
        await self.post_run_update(reg, ev, f"⚑ {m.display_name} ({co.character}) called out, {co.hours_before:.0f}h before" + ("" if mine else f" (by {by})"))
        await self.ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {ev.key} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else "") + ("" if mine else f" (by {by})"))
        return line

    async def answer_fill_for(self, reg, rs, ev, m, yes: bool, by: str, *, by_id: int | None = None) -> str:
        """A fill ask's Confirm / Can't make it from anywhere but the DM button (an officer on the site or MCP, plain
        text's confirm_for): the button's two steps — rc.apply_fill_answer, then after_fill_answer (the tied partner
        released on a no, the next batch asked, thread line, sheet and cards). RegistryError when nothing is waiting.
        `by_id` (the actor's Discord id) decides whether it was their own answer — never a name compare."""
        ask = next((a for a in ev.fill_asks if a.discord_id == m.discord_id and a.open), None)
        if ask is None:
            raise RegistryError(f"no open fill ask for {m.display_name} on {run_title(reg, ev)}")
        mine = by_id is not None and int(by_id) == int(m.discord_id)
        if not mine:
            ask.answered_by = by  # saved with the answer
        line = rc.apply_fill_answer(reg, rs, ev, ask, bool(yes))
        if not mine:
            rc.on_behalf_notice(reg, ev, m.discord_id, f"said {'yes' if yes else 'no'} to your {'swap' if ask.swap else 'seat'} question for you", by)
        # the ask's DM card shows the outcome, buttons gone (an answer on the site, an officer's) — never a new DM
        await self.edit_ask_card(reg, ask.dm_channel_id, ask.dm_message_id, f"→ {line}" + ("" if mine else f" (answered for you by {by})"))
        await self.after_fill_answer(reg, rs, ev, ask, line + ("" if mine else f" (answered by {by})"))
        return line

    MIRRORS_KEPT = 5  # read-only copies a run keeps track of (older ones stay as they were posted)

    def _live_sheet_name(self, ev) -> str:
        ch = self.get_channel(ev.channel_id) if ev.channel_id else None
        return f"#{getattr(ch, 'name', None) or 'signups'}" if ev.channel_id else "the signup channel"

    async def repost_sheet(self, reg, rs, ev, channel, *, copy: bool = False) -> str:
        """Re-post a live sheet in `channel` (/raid sheet, the site, MCP, plain text). Two verbs in one (D21):
        an officer's re-post MOVES the live sheet — the buttons and later refreshes follow the new message and the old
        one is edited to "Re-posted in #channel" (no buttons); `copy=True` (a member's /raid sheet) posts a labelled
        READ-ONLY copy ("copy — the live sheet is in #signups", no buttons) that never touches ev.channel_id /
        message_id; it is kept in ev.mirror_messages and refreshed with the sheet. No open DMs either way. Refused: a
        channel of another server (one guild's sheet never lands in another's) and a channel whose plain-text mode is
        `ignore` (the bot keeps quiet there) unless it is the signup channel."""
        g = getattr(channel, "guild", None)
        if g is not None and getattr(g, "id", None) != reg.config.discord_guild_id:
            raise RegistryError("That channel is in another server.")
        cid = getattr(channel, "id", None)
        if cid is not None and cid != reg.config.signup_channel_id and reg.plain_mode(cid) == "ignore":
            raise RegistryError(f"The bot keeps quiet in #{getattr(channel, 'name', cid)} (its plain-text mode is ignore) — pick another channel.")
        embed, view = sheet_message(reg, ev, rc.run_team(reg, ev), self.ico)
        where = f"#{getattr(channel, 'name', cid)}"
        if copy:
            if cid is not None and cid == ev.channel_id:
                return f"The live sheet for {run_title(reg, ev)} is already in {where}."
            embed.set_footer(text=f"copy — the live sheet is in {self._live_sheet_name(ev)}")
            msg = await channel.send(embed=embed)
            ev.mirror_messages = (ev.mirror_messages + [[msg.channel.id, msg.id]])[-self.MIRRORS_KEPT:]
            rs.save(ev, "sheet copy posted")
            return f"Posted a read-only copy of the sheet for {run_title(reg, ev)} in {where} — answers go on the live sheet in {self._live_sheet_name(ev)}."
        old = (ev.channel_id, ev.message_id)
        msg = await channel.send(embed=embed, **({"view": view} if view else {}))
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")
        if old[0] and old[1] and old != (ev.channel_id, ev.message_id):
            with contextlib.suppress(Exception):  # the old message keeps its look, says where the sheet went, loses its buttons
                old_msg = await self.get_channel(old[0]).fetch_message(old[1])
                stale, _v = sheet_message(reg, ev, rc.run_team(reg, ev), self.ico)
                stale.set_footer(text=f"Re-posted in {where} — answer there.")
                await old_msg.edit(embed=stale, view=None)
        return f"Re-posted the sheet for {run_title(reg, ev)} in {where} — the old one now points there."

    async def record_placement(self, reg, uid: int, roster: str, yes: bool, by: str) -> str:
        """THE check-and-set for a Confirm / Can't make it answer (design B2 "atomic check-and-set"): Registry.claim_placement
        runs ON THE LOOP — where PlaceButton and the scheduler's expire_confirmations also run — so two writers can never
        both read "unanswered"; only the git commit goes to a worker thread. A second answer is refused from one place:
        "Already answered (yes, by Kestrel at 5:12 PM)". Returns the line."""
        m, _ask, line = reg.claim_placement(uid, roster, yes, by)
        await asyncio.to_thread(reg.save, m, line)
        return line

    async def answer_placement_for(self, reg, uid: int, roster: str, yes: bool, by: str, *, by_id: int | None = None) -> str:
        """A Confirm / Can't make it answer from anywhere but the DM button itself (the Me page, an officer on the
        site or MCP, plain text's confirm_for): the button's two steps — record_placement (check-and-set on the loop),
        then `after_placement_answer` (seat freed on a no, run thread line, sheet and cards). The member's DM card is
        edited to the outcome (no new DM). `by_id` = the actor's Discord id: it alone decides "their own answer" (the
        card's wording), never a name compare. Raises RegistryError when there is nothing to answer."""
        line = await self.record_placement(reg, uid, roster, yes, by)
        m = reg.members.get(uid)
        ask = next((a for a in reversed(m.placement_asks) if a["roster"] == roster), None) if m else None
        mine = by_id is not None and int(by_id) == int(uid)
        if ask and ask.get("dm"):
            await self.edit_ask_card(reg, ask["dm"][0], ask["dm"][1], f"→ {line}" + ("" if mine else f" (answered for you by {by})"))
        if not mine:  # B5: the member's notice (never a DM)
            ev = next((e for e in self.raids.store(reg).live() if e.team == roster), None)
            if ev is not None:
                rc.on_behalf_notice(reg, ev, uid, "confirmed your seat" if yes else "answered Can't make it for you", by)
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

    async def seat_released(self, reg, ev, m, why: str) -> bool:
        """A member's seat on a locked roster was released, whoever released it: their Confirm / Can't card is EDITED to
        say so (the DM rule — no "your seat was released" DM; the Me page's notices carry it too). A seat released by
        their own Can't make it press needs nothing: that press already edited the card. True when a card was edited."""
        if why == "declined":
            return False
        ask = next((a for a in reversed(m.placement_asks) if a["roster"] == ev.team), None)
        if not ask or not ask.get("dm"):
            return False
        reason = RELEASE_WHY.get(why, why)
        return await self.edit_ask_card(reg, ask["dm"][0], ask["dm"][1], f"Your seat on {run_label(reg, ev)} was released ({reason}). Nothing to do — if that's wrong, tell an officer.")

    async def drop_seated(self, reg, rs, ev, team, m, why: str, note: str | None, announce: bool = True) -> None:
        """A rostered member is out after lock: free the seat, edit their ask card, post it, ask the bench to fill."""
        rc.free_seat(reg, rs, ev, m, why)
        await self.seat_released(reg, ev, m, why)
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
                            channel=None, via: str = "discord") -> tuple[rc.RaidEvent, object]:
        """The one test-run opener (/gm test run, plain text's `test run …`, the site's test bench and so MCP): a real
        sheet with minute-level cadence (`*_in` = minutes; start > nudge > lock > confirm), the pool = puppets + the
        tester (`by_id`), `dm` = DM every puppet when it opens (the puppets' DMs reach the tester as one digest). The
        sheet goes to the signup channel, else to `channel` (where it was asked for); with neither (the site, MCP, a DM
        without a signup channel) it is refused before anything is written, naming the setting (NO_TEST_CHANNEL). Its
        officer cards post in `channel` when it is a server channel (D20; officer_channel), and its key is in the
        '-test' key space (rc.TEST_SCHEDULE), so it never shares a real run's roster. The audit line (ops.audited, `via` = the surface) is the verb's own. Returns (the run, the channel it was posted in or None). rc.open_run runs on the event loop, as in
        open_run_and_post: it adds to the live events the loop's handlers iterate, so never from a worker thread (L3);
        its one git commit is the same short block every on-demand open has."""
        start = (reg.now_local() + timedelta(minutes=start_in)).replace(second=0, microsecond=0)
        in_guild = (channel is not None and not isinstance(channel, (discord.DMChannel, discord.GroupChannel))
                    and getattr(channel, "guild", True) is not None)  # a server channel, never a DM
        target = (self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None) or (channel if in_guild else None)
        if target is None:  # nowhere to post the sheet: refused before anything is written (the site, MCP, a DM), naming why
            raise RegistryError(bench_channel_refusal(reg, self.get_channel) or NO_TEST_CHANNEL)
        for _ in range(60):  # a rehearsal cleared (cancelled) this minute keeps its key: start a minute later instead
            if not rc.closed_run_refusal(rs, rc.event_key(raid, start, test=True)):
                break
            start += timedelta(minutes=1)
        cut = {"soft": nudge_in / 60, "hard": lock_in / 60, "confirm": confirm_in / 60, "open_dm": bool(dm), "test_by": by_id}
        if in_guild:
            cut["test_channel"] = channel.id  # its health / lock cards post where it was asked for (D20)
        ev = rc.open_run(reg, rs, raid, start, by=by, cutoffs=cut)
        if target is not None and not ev.message_id:
            await self.post_sheet(reg, rs, ev, target)
        await ops_mod.audited(self.ops, reg, by, f"test bench: opened test run {ev.key} (lock in {start_in - lock_in} min)", via=via, level="warn", by_id=by_id)
        return ev, target

    async def cancel_run(self, reg, rs, ev, by: str, reason: str | None, *, notify: bool = True, by_id: int | None = None, via: str = "discord") -> str:
        """Cancel a run — the ONE verb behind the card's Cancel, /raid cancel, the site, plain text and MCP. Under the run's
        board lock, so a lock in progress finishes its flip first (lock_run then re-checks the state before every
        confirmation); refused when the run is already done / cancelled. Nobody is DMed (the DM rule): every open ask
        (Confirm / Can't, fill, pre-lock) closes as `cancelled` and its DM card is EDITED to say so; the sheet is edited to
        its final state; `notify` posts one fixed line in the signup channel (never the reason — that stays in the run's
        thread and the ops feed, for officers); the Me page's notices are derived from the record. Returns a plain line.
        The fill / pre-lock asks close under plan_lock (every writer of ev.fill_asks holds it), taken AFTER the state
        flip: a fan-out in flight sees the cancel before its next DM (RaidMixin._send_asks), stops, edits a card that
        went out meanwhile, and releases the lock — so the cancel waits for one DM at most, never a whole batch.
        Clearing the test bench passes notify=False: its cancels post nothing where real members read."""
        async with self.board_lock(reg, ev.key):
            if ev.state in ("done", "cancelled"):
                raise RegistryError(f"{run_title(reg, ev)} is already {ev.state} — nothing was changed.")
            fx = rc.cancel_effects(reg, ev)
            cards = [(a["dm"][0], a["dm"][1]) for _m, a in rc.open_placement_rows(reg, ev) if a.get("dm")] if ev.all_rosters else []
            ev.prev_state, ev.state, ev.closed_at = ev.state, "cancelled", rc.now()
            ev.log.append(f"cancelled by {by}" + (f": {reason}" if reason else ""))
            rs.save(ev, "cancelled")
        async with self.plan_lock(reg, ev.key):
            fills = [a for a in ev.fill_asks if a.pending]
            for a in fills:
                a.answer, a.answered_at, a.held = "cancelled", rc.now(), False
            if fills:
                rs.save(ev, f"cancelled: {len(fills)} ask(s) closed")
        waiting = rc.withdraw_confirmations(reg, ev, "cancelled") if ev.all_rosters else []
        rc.cancel_notices(reg, ev, by, by_id)  # the Me page's notice for everyone on it (D26) — nobody is DMed
        label = run_label(reg, ev)
        for cid, mid in cards + [(a.dm_channel_id, a.dm_message_id) for a in fills]:
            await self.edit_ask_card(reg, cid, mid, f"Cancelled — {label} was cancelled. Nothing to do.")
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"🛑 run cancelled by {by}" + (f": {reason}" if reason else "")
                                   + (f" · confirmations withdrawn for {len(waiting)}" if waiting else "") + (f" · {len(fills)} fill ask(s) closed" if fills else ""))
        posted = False
        sig = self.get_channel(ev.channel_id) if notify and ev.channel_id else None
        if sig is not None:
            try:  # fixed text: the reason is for officers, never echoed publicly
                await sig.send(f"🛑 {label} is cancelled." + (" Rostered seats are released — nothing to do." if fx["locked"] else " Nothing to do."),
                               allowed_mentions=discord.AllowedMentions.none())
                posted = True
            except Exception as e:  # noqa: BLE001
                log.warning("cancel line failed (%s): %s", ev.key, e)
        await ops_mod.audited(self.ops, reg, by, f"cancelled {ev.key}" + (f": {reason}" if reason else ""), via=via, level="warn", by_id=by_id)
        told = [f"{len(cards) + len(fills)} open ask card(s) now say Cancelled"] if cards or fills else []
        told.append("a line went to the signup channel" if posted else "no line in the signup channel")
        return f"Cancelled {run_title(reg, ev)}. Nobody was DMed: the sheet shows it, " + ", ".join(told) + "."

    async def reopen_run(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Undo an end by mistake, within the hour (design B6; never a cancelled run, D19): rc.reopen_run restores the
        state it ended from, then the sheet and the officer cards are redrawn live and the thread hears it. The loot
        council stays as it was archived (reopen a council with /raid loot). RegistryError with the reason otherwise."""
        async with self.board_lock(reg, ev.key):
            back = rc.reopen_run(reg, rs, ev, by)
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"↺ reopened by {by} — {'locked again' if back == 'locked' else 'the sheet is open again'}")
        await ops_mod.audited(self.ops, reg, by, f"reopened {ev.key} ({back})", via=via, by_id=by_id)
        return f"Reopened {run_title(reg, ev)} — {'locked, roster as it was' if back == 'locked' else 'the sheet is open again'}."

    async def remind(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Remind the unconfirmed (design B2 levers, D15): re-send the Confirm / Can't DM to everyone rostered whose ask is
        still unanswered — against their EXISTING ask (never add_placement_ask again: that would reset asked_at and break
        a press on the old card), stamping the ask's `reminded_at` and the run's; at most once per hour per run
        (rc.remind_refusal). DMs-off asks wait on the site and are not DMed. RegistryError when it can't go now.
        Check-and-set on the loop: the refusal check and the run's `reminded_at` stamp happen with no await between
        them, so two officers pressing Remind at once send ONE round (the second is told who reminded). The run's
        state and each ask are re-checked before every DM, and a reminder card that went out while the ask closed (an
        answer elsewhere, the run cancelled) is edited at once — never a live Confirm on a closed ask."""
        why = rc.remind_refusal(reg, ev)
        if why:
            raise RegistryError(why)
        ev.reminded_at, ev.reminded_by = rc.now(), by  # claimed before the first await (see above)
        team = rc.run_team(reg, ev)
        sent, site, stopped = 0, 0, None
        for m, ask in rc.open_placement_rows(reg, ev):
            if ev.state != "locked":  # cancelled / ended during the round: nobody after that is reminded
                stopped = ev.state
                break
            if ask.get("answer") is not None:  # answered since the round started
                continue
            if ask.get("channel", "dm") != "dm" or m.dm_opt_out:
                site += 1
                continue
            seat = ev.seat_of(m)
            sg = ev.signups.get(str(m.discord_id))
            if not seat or not sg:
                continue
            i = seat[0]
            gi = next((k for k, g in enumerate(ev.all_rosters[i].groups) if seat[1].signup_name in g), 0)
            ok, msg = await self.send_member_card(reg, m, None, confirm_layout(reg, ev, team, self.ico, sg, i, gi, m.discord_id, reminder=True), ev=ev)
            if not ok:
                continue
            cid, mid = self.card_ids(msg)
            if ask.get("answer") is not None or ev.state != "locked":  # closed while this DM was in flight
                await self.edit_ask_card(reg, cid, mid, f"Cancelled — {run_label(reg, ev)} was cancelled. Nothing to do." if ev.state == "cancelled"
                                         else f"Already answered — nothing to do for {run_label(reg, ev)}.")
                continue
            sent += 1
            if mid:
                ask["dm"] = [cid, mid]  # the newest card is the one an outcome edits
            ask["reminded_at"] = rc.now()
            await asyncio.to_thread(reg.save, m, f"{m.display_name} reminded to confirm {ev.team}")
        ev.log.append(f"reminded the unconfirmed ({sent} by DM) — {by}" + (f"; stopped, the run is {stopped}" if stopped else ""))
        rs.save(ev, f"reminded {sent} by {by}")
        await self.post_run_update(reg, ev, f"🔔 {by} reminded the unconfirmed: {sent} by DM" + (f" · {site} wait on the site" if site else "")
                                   + (f" · stopped: the run is {stopped}" if stopped else ""))
        await self.refresh_cards(reg, ev)
        await ops_mod.audited(self.ops, reg, by, f"reminded {sent} unconfirmed on {ev.key}", via=via, by_id=by_id)
        if stopped:
            return f"Reminded {sent} by DM, then stopped: the run is {stopped}."
        return f"Reminded {sent} by DM" + (f"; {site} wait on the site (DMs off)" if site else "") + ". Next reminder possible in an hour."

    async def set_autofill_paused(self, reg, rs, ev, paused: bool, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Pause / resume the fill engine for one run (D18): `ev.autofill_paused`, gated inside run_fill — the scheduler,
        a freed seat, an answer and the site's blind send all respect it; an officer's own Ask / Send still goes."""
        if ev.state in ("done", "cancelled"):
            raise RegistryError(f"{run_title(reg, ev)} is {ev.state}.")
        if bool(ev.autofill_paused) == bool(paused):
            return f"Autofill is already {'paused' if paused else 'on'} for {run_title(reg, ev)}."
        ev.autofill_paused, ev.autofill_paused_by = bool(paused), (by if paused else None)
        ev.log.append(f"autofill {'paused' if paused else 'resumed'} by {by}")
        rs.save(ev, f"autofill {'paused' if paused else 'resumed'} by {by}")
        await self.post_run_update(reg, ev, f"{'⏸️' if paused else '▶️'} autofill {'paused' if paused else 'resumed'} by {by}")
        await self.refresh_cards(reg, ev)
        await ops_mod.audited(self.ops, reg, by, f"{'paused' if paused else 'resumed'} autofill on {ev.key}", via=via, by_id=by_id)
        if not paused and ev.state == "locked" and rc.team_setting(rc.run_team(reg, ev), "autofill"):
            sent, _nd = await self.run_fill(reg, rs, ev, rc.run_team(reg, ev), by=f"resumed by {by}")
            if sent:
                await self.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        return (f"Autofill paused for {run_title(reg, ev)} — nothing goes out on its own; your Ask / Fill still sends." if paused
                else f"Autofill on again for {run_title(reg, ev)}.")

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
            if ev.state != "open" and ev.seat_of(m):
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
