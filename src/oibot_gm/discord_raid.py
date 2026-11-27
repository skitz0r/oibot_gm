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
                         _raidwide_line, _role_counts, ask_closed_layout, ask_line, event_url, clock12, closed_layout, confirm_layout, confirm_open_layout, fill_layout,
                         dm_locks_line, gaps_text, guild_prefix, health_layout, health_locked_layout, lock_layout, raid_name, run_actions_row, run_label, run_times, run_title, sheet_message,
                         sheet_state)
from .registry import Registry, RegistryError

log = logging.getLogger(__name__)
UNLOCK_WAIT_S = 20.0  # a person's lock waits this long for an unlock still editing its Discord cards (lock_run)

__all__ = ["RaidContext", "RaidMixin", "RaidSchedulerMixin", "BotProto", "register_raid_commands",
           # buttons
           "FillButton", "PlaceButton", "RunButton", "SignupButton", "fill_view", "place_view", "sheet_view",
           # views + helpers
           "CLOCK12", "OUT_MARK", "RELEASE_WHY", "ask_line", "event_url", "clock12", "closed_layout", "confirm_layout", "fill_layout", "gaps_text",
           "health_layout", "health_locked_layout", "lock_layout", "raid_name", "run_actions_row", "run_label", "run_times", "run_title", "sheet_message", "sheet_state",
           "_aura_line", "_group_block", "_group_summaries", "_header", "_raidwide_line", "_role_counts"]


NO_TEST_CHANNEL = ("No signup channel is set, so a test event's sheet has nowhere to go: set the signup channel (Config › Channels, "
                   "or /gm config signup-channel), or use `/gm test run` in a server channel.")
UNREACHABLE_TEST_CHANNEL = ("The signup channel is set but the bot can't reach it (deleted, or hidden from the bot), so a test event's sheet "
                            "has nowhere to go: pick the signup channel again (Config › Channels, or /gm config signup-channel), "
                            "or use `/gm test run` in a server channel.")


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

def _cq():
    """consequences, imported late (it imports raid_views, which this module imports)."""
    from . import consequences

    return consequences


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
        ask closes (answered elsewhere, expired, withdrawn, the run cancelled), never followed by another DM. A DETACHED
        guild (the bot was removed from its server: Guilds.detach) sends nothing — its runs' pending asks stop here."""
        if getattr(getattr(reg, "config", None), "detached_at", None):
            return False, None
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
        head = f"🧪 {guild_prefix(reg)}**{run_title(reg, ev)}** (test event) · " if ev is not None else f"🧪 {guild_prefix(reg)}"
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

    async def end_ask_cards(self, reg, ev, cards: list[tuple[int, int]]) -> int:
        """The Confirm asks an End closed (rc.close_asks_at_end): each DM card edited to its final state (rc.ENDED_ASK),
        never a new DM. LootMixin.end_run and the scheduler's close call it. → cards edited."""
        text = rc.ENDED_ASK.format(label=run_label(reg, ev))
        return sum([await self.edit_ask_card(reg, cid, mid, text) for cid, mid in cards])

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

    async def edit_live_card(self, reg, channel_id: int | None, message_id: int | None, view) -> bool:
        """Edit an ask's DM card that is still in play to `view` — its buttons kept, so the same card still answers the
        same ask (a run moved: the card shows the new time). edit_ask_card stays for final states. False when the card is
        unknown or Discord refused; nothing else changes either way."""
        if not channel_id or not message_id:
            return False
        try:
            ch = self.get_channel(int(channel_id))
            if ch is None and hasattr(self, "fetch_channel"):
                ch = await self.fetch_channel(int(channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(view=view)
            return True
        except Exception as e:  # noqa: BLE001 — the record is written; only the card keeps the old time
            log.debug("live card edit failed (%s/%s): %s", channel_id, message_id, e)
            return False

    @staticmethod
    def card_ids(msg) -> tuple[int | None, int | None]:
        """(channel id, message id) of a sent DM card, or (None, None)."""
        if msg is None:
            return None, None
        ch = getattr(msg, "channel", None)
        return (getattr(ch, "id", None), getattr(msg, "id", None))

    # ---- officer cards + the event's threads
    async def post_run_update(self, reg, ev, text: str, *, whole: bool = False) -> None:
        """One officers' line about `ev` (a pause, a cancel and its reason, a failed lock, a move …) in its event's
        updates thread (updates_thread: under the first raid's officer card, made on first use). The thread is the
        whole event's, so in an event of several raids a line about one raid starts with that raid's name; `whole`
        (an unlock, a move, Start now, a lineup edit, a pause, a cancel) says the line is the event's. With no card or no
        thread to be had, the line goes to the ops feed (officers only) — never loose in a channel, never the signup
        channel. A test run's line also goes to the test feed (the site's Test bench follows the rehearsal), which is
        where it stays when the rehearsal has no thread: a test event never writes the ops feed this way."""
        from . import testfeed

        if not whole:
            try:
                parts = rc.event_parts(self.raids.store(reg), ev)
            except Exception:  # noqa: BLE001 — a store-less fake: the line as it is
                parts = [ev]
            if len(parts) > 1:
                text = f"{reg.raid_def(ev.instance).get('name', ev.instance)}: {text}"
        testfeed.note(reg, ev, "step", text, name_of=self._channel_name)
        thread = await self.updates_thread(reg, ev)
        if thread is not None:
            try:
                await thread.send(text, allowed_mentions=discord.AllowedMentions.none())
                return
            except Exception as e:  # noqa: BLE001 — the line goes to the ops feed instead
                log.warning("updates line failed (%s): %s", ev.key, e)
        ops = getattr(self, "ops", None)
        if ops is not None and not ev.test:
            await ops.emit(reg.config, "info", f"{run_title(reg, ev)}: {text}")

    async def _channel(self, cid: int | None):
        """A channel or thread by id: the cache, else fetched (an archived thread is not cached). None when it is gone."""
        if not cid:
            return None
        ch = self.get_channel(int(cid))
        if ch is None and hasattr(self, "fetch_channel"):
            try:
                ch = await self.fetch_channel(int(cid))
            except Exception as e:  # noqa: BLE001
                log.debug("channel %s not fetched: %s", cid, e)
                return None
        return ch

    async def _thread_on(self, msg, name: str):
        """A thread started from `msg`: made, or the one it already has (a thread started from a message has the
        message's id, so an id lost or never stored is found again — Discord refuses a second thread on one message)."""
        try:
            return await msg.create_thread(name=name[:100])
        except Exception as e:  # noqa: BLE001
            thread = getattr(msg, "thread", None) or await self._channel(getattr(msg, "id", None))
            if thread is None:
                raise
            log.debug("thread on %s reused: %s", getattr(msg, "id", None), e)
            return thread

    async def updates_thread(self, reg, ev):
        """THE event's updates thread (post_run_update): one for every raid of the event, kept on its first raid
        (rc.updates_thread_of). A stored one is fetched — archived too — and used; else it is started from the first
        raid's officer card (lock card, else health card; a later raid's own card when the first raid has none) in
        the channel the cards are really in (`cards_channel_id`). Never under a card in the public sheet channel
        (a test run's own channel aside, D20). None — logged with the event — when there is no card or no thread."""
        rs = self.raids.store(reg)
        first = rc.first_part(rs, ev)
        public = set() if ev.test else self._sheet_channels(reg, first)
        for tid in [t for t in (first.updates_thread_id, ev.updates_thread_id) if t]:
            th = await self._channel(tid)
            if th is not None and getattr(th, "parent_id", None) not in public:
                return th
        for owner in [first] + ([ev] if ev is not first else []):
            mid = owner.cards.get("lock:0") or owner.cards.get("health")
            if not mid or not owner.cards_channel_id or int(owner.cards_channel_id) in public:
                continue
            try:
                ch = await self._channel(owner.cards_channel_id)
                if ch is None:
                    raise LookupError(f"channel {owner.cards_channel_id} is gone")
                thread = await self._thread_on(await ch.fetch_message(mid), f"{run_title(reg, first)} · updates")
            except Exception as e:  # noqa: BLE001
                log.warning("updates thread not made (%s, card %s): %s", owner.key, mid, e)
                continue
            first.updates_thread_id = thread.id
            await asyncio.to_thread(rs.save, first, "updates thread")
            return thread
        # the usual state before the reminder time (no card yet): the line goes to the ops feed, nothing failed
        log.info("no updates thread for %s yet: no officer card to start it from", ev.key)
        return None

    async def sheet_thread(self, reg, ev):
        """THE event's public thread on its signup sheet: the members' lines about the event (the "is locked" line,
        the cancel line) go here, never loose in the signup channel. Kept on the first raid (`sheet_thread_id`) and
        reused by every raid of the event and by a relock; a stored one is fetched (archived too). A thread started
        from the sheet has the sheet's id, so after the sheet moved (a re-post) a new one starts on the new sheet.
        None — logged — when the event has no sheet, the sheet is gone or no thread can be made."""
        rs = self.raids.store(reg)
        first = rc.first_part(rs, ev)
        if not (first.channel_id and first.message_id):
            return None
        if first.sheet_thread_id and first.sheet_thread_id == first.message_id and (th := await self._channel(first.sheet_thread_id)) is not None:
            return th
        try:
            ch = await self._channel(first.channel_id)
            if ch is None:
                raise LookupError(f"channel {first.channel_id} is gone")
            thread = await self._thread_on(await ch.fetch_message(first.message_id), run_title(reg, first))
        except Exception as e:  # noqa: BLE001
            log.warning("sheet thread not made (%s): %s", first.key, e)
            return None
        first.sheet_thread_id = thread.id
        await asyncio.to_thread(rs.save, first, "sheet thread")
        return thread

    async def rename_threads(self, reg, rs, first) -> None:
        """The event's two threads (sheet_thread, updates_thread) renamed to its title as it is now — after a move or
        Start now, whose title carries the start. Best effort: a thread missing is skipped, a refused rename logged."""
        title = run_title(reg, first)
        for tid, name in ((first.sheet_thread_id, title), (first.updates_thread_id, f"{title} · updates")):
            th = await self._channel(tid) if tid else None
            if th is None or not hasattr(th, "edit") or getattr(th, "name", None) == name[:100]:
                continue
            try:
                await th.edit(name=name[:100])
            except Exception as e:  # noqa: BLE001 — the event moved; only the thread's name is behind
                log.warning("thread rename failed (%s, thread %s): %s", first.key, tid, e)

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

    async def set_answer(self, reg, rs, ev, m, status: str, character: str | None, by: str, by_id: int | None = None, *,
                         spec: str | None = None, via: str = "discord") -> str:
        """The one path for setting someone's answer on a sheet — the member themself or an officer. `by` is a display
        name people read; `by_id` is the Discord id of whoever acts and DECIDES whose answer it is: the member's own when
        `by_id == m.discord_id` (never a name compare — nicks change, design §2.2). No `by_id` = an officer acting.
        Open sheet: the signup + sheet refresh. Locked: Join seats them (if not already) and asks them to confirm;
        No thanks frees a rostered seat (drop_seated: DM, thread line, fill); Bench is the signup only. `spec` (Join /
        Bench: an officer's role switch, the Events page's Switch) = the spec the character plays — its spec, offspec or
        a flex role's (rc.spec_role: "Nyx doesn't play Holy." otherwise); the signup's spec and role follow. Refused
        (rc.SPEC_SEATED) once the run is locked and they hold a seat: that switch is an ask (send_plan). Answers live on
        the first raid: sync_parts carries them to the later raids. A switch is written to the run's log with who made
        it and to the ops feed through ops.audited (`via`: web | mcp | chat | discord). Returns a line."""
        if status not in rc.STATUSES:
            raise ValueError(f"status must be one of {rc.STATUSES}")
        mine = by_id is not None and int(by_id) == int(m.discord_id)
        source = "member" if mine else "officer"
        team = rc.run_team(reg, ev)
        if team.get("test") and not getattr(m, "test", False) and str(m.discord_id) != str(team.get("test_by") or ""):
            # a rehearsal: its pool is the puppets plus the officer running it — never a real member (web, chat, MCP alike)
            raise RegistryError(f"{run_title(reg, ev)} is a test event: only test members and the officer running it can answer it")
        label = run_label(reg, ev).replace("**", "")  # plain text: the line is shown in Discord replies and on the web alike
        who = "" if mine else f"{m.display_name}: "
        released = status == "out" and await self.release_asks_of(reg, rs, ev, m, "answered No thanks")  # their ask covers nothing now
        if sheet_state(ev) == "open":
            s = rc.set_signup(reg, rs, ev, m, character, status, source=source, spec=spec if status != "out" else None, by=by if spec else None)
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
                if ev.followed_by:  # a lead's No thanks / Can't is the whole night
                    await self.after_lead_out(reg, rs, ev, m, "callout" if mine else "officer", None)
                await self.refresh_sheet(reg, ev)
                await self.refresh_cards(reg, ev)
                await self.refill_after_release(reg, rs, ev, released)
                line = f"✅ {who}No thanks for {label} (wasn't rostered)"
        elif status == "in":
            s = rc.set_signup(reg, rs, ev, m, character, "in", source=source, spec=spec, by=by if spec else None)
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
            s = rc.set_signup(reg, rs, ev, m, character, "sub", source=source, spec=spec, by=by if spec else None)
            await self.refresh_sheet(reg, ev)
            line = f"✅ {who}{s.character} Bench for {label}" + (" — still rostered; set No thanks to free the seat" if ev.seat_of(m) else "")
        if status == "in":  # Join while away: Join stays pure, the absence stands for the other sheets — the receipt says so
            note = rc.join_while_away(reg, ev, m, mine=mine)
            if note:
                line += f"\n{note}"
        s = ev.signups.get(str(m.discord_id))
        if spec and status != "out" and s is not None:  # a role switch: who made it, in the audit format every run verb uses
            await ops_mod.audited(self.ops, reg, by, f"switched {m.display_name} to {s.spec} ({rc.LABELS[status]}) on {run_title(reg, ev)}", via=via, by_id=by_id)
        elif not mine:
            await self.ops.emit(reg.config, "info", f"{by} set {m.display_name} {status} on {run_title(reg, ev)}")
        if not mine:
            s = ev.signups.get(str(m.discord_id))  # B5: the member's notice (never a DM)
            rc.on_behalf_notice(reg, ev, m.discord_id, f"set you to {rc.LABELS[status]}" + (f" as {s.character}" if s and status != "out" else "")
                                + (f", playing {s.spec}" if spec and s and status != "out" else ""), by)
        return line

    async def set_raid_switch(self, reg, rs, part, m, character: str, spec: str, by: str, by_id: int | None = None, *,
                              via: str = "discord") -> str:
        """A role switch for ONE raid of an event (issue 15) — every surface's verb (the board ⋮ menu, the Role
        switches box with "this raid", /raid set's "For which raid?", plain text, MCP `set_answer` with `raid`):
        rc.set_raid_switch writes it (or the way back to their answer) on `part` alone, with a log line naming `by`.
        The member gets ONE notice naming the raid and the spec (never a DM; none on the officer's own line), the ops
        feed gets the audited line, and the officer card follows. The public sheet keeps their answer. Returns a line."""
        mine = by_id is not None and int(by_id) == int(m.discord_id)
        done = rc.set_raid_switch(reg, rs, part, m, character, spec, by)
        sg = part.signups.get(str(m.discord_id))
        raid = rc._raid_name(reg, part.instance)
        plays = rc._plays_words(character, spec, sg.character if sg else character)
        what = f"switched you back to {plays}" if done == "cleared" else f"switched you to {plays}"
        if not mine:
            rc.on_behalf_notice(reg, part, m.discord_id, what, by)
        await ops_mod.audited(self.ops, reg, by, f"switched {m.display_name} to {plays} for {raid} alone on {run_title(reg, part)}", via=via, by_id=by_id)
        if "health" in part.cards:
            await self.refresh_cards(reg, part)
        who = "You play" if mine else f"{m.display_name} plays"
        return f"✅ {who} {plays}" + (" again" if done == "cleared" else "") + f" for {raid}"

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

    async def sheets_restore(self) -> int:
        """On the first ready of a process: every live (open or locked) sheet of every guild redrawn in place as this
        version draws it — a change to the sheet's look (its title link, a new line) reaches the sheets already posted
        at the next restart instead of at the next answer on each. One edit per sheet; a failure is logged and the
        rest go on. Returns the sheets touched."""
        n = 0
        for reg in self.registries.ordered():
            rs = self.raids.store(reg)
            for ev in list(rs.live()):
                if not (ev.channel_id and ev.message_id):
                    continue
                try:
                    await self.refresh_sheet(reg, ev)
                    n += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("sheet restore failed (%s %s): %s", reg.key, ev.key, e)
        return n

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
            if await self.send_member_dm(reg, m, f"{reg.config.name} · {run_label(reg, ev)} (<t:{unix}:R>). {status}. Join / Bench / No thanks:" + (f" (sheet: <#{ev.channel_id}>)" if ev.channel_id else "") + "\n-# " + dm_locks_line(reg, ev, hard), sheet_view(reg, ev.key), ev=ev):
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
                if await self.send_member_dm(reg, m, f"{reg.config.name}: the {run_label(reg, ev)} sheet is still waiting for you — Join, Bench or No thanks here or in <#{ev.channel_id}>.\n-# " + dm_locks_line(reg, ev, hard), sheet_view(reg, ev.key), ev=ev):
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
                              + ("" if ev.state == "locked" else f" — the event was {ev.state} meanwhile; nobody after that was asked"))
                rs.save(ev, f"fill asked {len(sent)}")
            elif batch and ev.state != "locked":
                rs.save(ev, f"fill stopped: the event is {ev.state}")  # the unsent asks closed by _send_asks
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
                    await self.post_run_update(reg, ev, "🧩 " + (f"short {gaps} — " if (gaps := gaps_text(self.ico, nd)) else "") + "a role swap would cover it; it needs an officer's **Ask** (the Suggested block on the lock card)")
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
            await self.ops.emit(reg.config, "warn", f"{run_title(reg, ev)}: DM failed — waits on the site: {names}")
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
            return f"Cancelled — {label} was cancelled."
        return f"Never mind the {'swap' if a.swap else 'seat'} question for {label} — the event is {ev.state} now."

    def plan_lock(self, reg, key: str) -> asyncio.Lock:
        """One officer send per run at a time (two presses of Ask never ask anyone twice). Keyed (guild, run): two
        guilds can hold runs with the same key."""
        return self.__dict__.setdefault("_plan_locks", {}).setdefault((reg.key, key), asyncio.Lock())

    async def send_plan(self, reg, rs, ev, *, rev: str, by: str, picks: dict | None = None, limit: int | None = None,
                        seats: list[int] | None = None) -> tuple[list, "rc.Plan"]:
        """An officer's Ask: send the planner's suggestions (each seat's suggestion, or the alternate in `picks` =
        {seat index: option index}) as DMs — the one path for the health / lock card's Ask, the Suggest wizard,
        /raid fill on an open run, the site's Suggest, plain text and MCP. `rev` = the plan fingerprint the officer was
        shown (rc.plan_rev): when the plan is different now, rc.PlanChanged is raised carrying the current plan and
        nothing is sent. Before lock: pre-lock asks (one per member per run, expire at lock; a yes changes the signup
        and seats nobody). After lock: fill asks the officer approved — role swaps included (D3). `limit`: only the first
        that many seats (the card's Ask: the seats the card draws, never one it left under "+N more"). `seats`: only those
        seat indexes (the Events page's Switch on one row, with its `picks`). Returns (sent, plan)."""
        async with self.plan_lock(reg, ev.key):
            if ev.state not in ("open", "locked"):
                raise RegistryError(f"{run_title(reg, ev)} is {ev.state}")
            if ev.state == "open" and reg.now_local() >= ev.start:  # an Auto Lock off event still open: its asks would be born expired
                raise RegistryError(rc.PRELOCK_STARTED)
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
            asks, plan.held_back = rc.send_batch(plan, picks, limit, seats)
            if not asks:
                return [], plan
            team = rc.run_team(reg, ev)
            _soft, hard, _confirm = run_times(reg, ev, team)
            prelock = plan.phase == "prelock"
            deadline = (rc.prelock_deadline(reg, ev, hard) if prelock else rc.ask_deadline(reg, ev)).isoformat()  # pre-lock: at the lock, or the start with Auto Lock off
            sent = await self._send_asks(reg, rs, ev, team, asks, phase=plan.phase, approved_by=by, deadline=deadline)
            if sent and not prelock and ev.state == "locked":  # a cancel during the fan-out keeps its own fill state
                ev.fill_state = "asking"
            ev.log.append(f"{'suggestions' if prelock else 'fill'} sent by {by}: " + (", ".join(a.display_name + (" (site)" if a.channel == "web" else "") for a in sent) or "nobody to ask"))
            rs.save(ev, f"{'suggestions' if prelock else 'fill'} sent by {by}")
        if sent:
            await self.post_run_update(reg, ev, f"🧩 {by} asked:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent)
                                       + (f"\n-# {plan.held_back} more wait — at most {rc.FILL_MAX_OPEN} asks out at once; the card keeps them" if plan.held_back else ""))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(reg.config, "info", f"{by} sent {len(sent)} {'pre-lock ' if prelock else ''}ask(s) on {run_title(reg, ev)}")
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
        await self.edit_ask_card(reg, ask.dm_channel_id, ask.dm_message_id, f"Never mind the {'swap' if ask.swap else 'seat'} question for {run_label(reg, ev)} — {why}.")
        if other is not None:
            await self.withdraw_ask(reg, ev, other, f"{ask.display_name}'s half of the swap was withdrawn")
        await self.post_run_update(reg, ev, f"↩️ {by} withdrew the ask to {ask.display_name}")
        await self.refresh_cards(reg, ev)
        await ops_mod.audited(self.ops, reg, by, f"withdrew the ask to {ask.display_name} on {run_title(reg, ev)}", via=via, by_id=by_id)
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
        """Edit one fill / pre-lock ask's DM card to its final state ("Never mind … — <because>.", no
        buttons): every path that closes an ask without the member's press (their own No thanks / callout / absence,
        expiry, the lock closing pre-lock asks, a withdrawn partner) ends the card this way — never a new DM."""
        text = f"Never mind the {'swap' if ask.swap else 'seat'} question for **{reg.raid_def(ev.instance).get('name', ev.instance)}** <t:{int(ev.start.timestamp())}:F> — {because}."
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
                    await self.edit_ask_card(reg, tw.dm_channel_id, tw.dm_message_id, f"Never mind the seat question for {run_label(reg, ev)} — someone else said yes first.")
        if ask.held:  # the first yes of a tied pair: nothing changed yet, the partner's answer decides
            await self.post_run_update(reg, ev, f"🧩 {line}")
            await self.refresh_cards(reg, ev)
            await self.ops.emit(cfg, "info", f"fill {run_title(reg, ev)}: {line}")
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
            await self.ops.emit(cfg, "info", f"suggestion {run_title(reg, ev)}: {line}")
            return
        nd = rc.needs(reg, ev, team)
        await self.post_run_update(reg, ev, f"🧩 {line}" + (f" · still short {gaps_text(self.ico, nd)}" if nd["headcount"] or nd["roles"] else " · **gaps filled**"))
        await self.refresh_cards(reg, ev)
        await self.ops.emit(cfg, "info", f"fill {run_title(reg, ev)}: {line}")
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
        (D19), so its entry goes at once. A first raid's entry with no run file DICT_WITHOUT_RUN_HOURS after it was
        made (an open cut off before its file) goes too."""
        cfg = reg.config
        gone = []
        for t in list(cfg.rosters):
            if not t.get("ephemeral"):
                continue
            if t.get("follows") and cfg.team(t["follows"]) is None:  # an orphaned later raid: its first raid's entry is gone
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
                continue
            # an entry waits for the runs of every raid after it too, so a run's raids go together (design §5.62): the
            # first raid's entry outlives every later raid's run, so a later raid is never an orphan while it plays
            keys, nxt = {t["key"]}, t.get("followed_by")
            while nxt and nxt not in keys:
                keys.add(nxt)
                nxt = (cfg.team(nxt) or {}).get("followed_by")
            evs = [e for e in rs.events.values() if e.team in keys]
            made = t.get("created_at")
            if not evs and not t.get("follows") and made and reg.now_local() - datetime.fromisoformat(made) > timedelta(hours=rc.DICT_WITHOUT_RUN_HOURS):
                # an open cut off before its run file was written: no run will ever take this dict (its later raids'
                # dicts go with it, as orphans, on the next pass)
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
                continue
            if evs and all(e.state == "cancelled" or (e.state == "done" and not rc.closed_recently(e)) for e in evs):
                for e in evs:  # what the run stood for outlives its dict: a replaced night never opens after it closed
                    if not rc.is_follow(e):
                        rc.sync_stands_for(reg, rs, e)
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
        if gone:
            reg.save_config(f"events closed: {gone}", notify=False)

    # ---- lock → roster(s) → confirmations
    def officer_channel(self, reg, ev=None):
        """THE officers' channel for new officer posts (the health and lock cards, the loot council's starter): the
        roster channel, else the ops channel, else None — never the public sheet channel: a roster or ops channel that
        IS the signup channel (or the channel `ev`'s sheet is in) is skipped, so a guild whose roster channel is its
        signup channel posts its cards in the ops channel. Lines about a posted card read the channel the cards are
        really in (`ev.cards_channel_id`, updates_thread), not this. A TEST run's cards go to the channel its /gm test
        run was used in (D20: the rehearsal stays where it was asked for, with the test badge; a DM can't host the
        updates thread, so a run opened from a DM or the site uses the officer channel as any run does)."""
        cfg = reg.config
        own = (cfg.team(ev.team) or {}).get("test_channel") if ev is not None else None
        if own and (ch := self.get_channel(int(own))) is not None:
            return ch
        public = self._sheet_channels(reg, ev)
        for cid in (cfg.roster_channel_id, cfg.ops_channel_id):
            if cid and int(cid) not in public and (ch := self.get_channel(int(cid))) is not None:
                return ch
        return None

    def no_officer_channel_why(self, reg, ev=None) -> str:
        """Why officer_channel found none, in a sentence naming the setting to change: none set, or the ones set are the
        public sheet channel (the officers' channel can't be the signup channel), or the bot can't see them."""
        cfg = reg.config
        set_ = [int(c) for c in (cfg.roster_channel_id, cfg.ops_channel_id) if c]
        if not set_:
            return "No roster or ops channel is set. Set one on the Config page."
        if all(c in self._sheet_channels(reg, ev) for c in set_):
            return ("The officers' channel can't be the signup channel, and the channels set for roster and ops are the "
                    "signup channel. Set the ops channel to another channel on the Config page.")
        return "The bot can't see the roster or ops channel set. Check its permissions there, or set another on the Config page."

    @staticmethod
    def _sheet_channels(reg, ev=None) -> set[int]:
        """The public channels an officer line must never land in: the signup channel and the one `ev`'s sheet is in."""
        return {int(c) for c in (reg.config.signup_channel_id, getattr(ev, "channel_id", None)) if c}

    @contextlib.asynccontextmanager
    async def solving(self, reg, key: str, *, lock: bool = False):
        """Single flight for the solver per run, whoever asks (a lock from chat, MCP, the scheduler, the card or the
        site; a split preview): a second caller gets rc.SolverBusy instead of a second solve. Keyed (guild, run key).
        The lock comes first: once it holds the run, `lock=True` (lock_run: it needs the CPU) abandons every Auto-fill
        groups still solving in the process (event_board stop_all); a Preview lock or split preview only that event's
        (stop_event). A stopped auto-fill writes nothing, and none starts while either runs."""
        is_lock = lock
        flight = self.__dict__.setdefault("_solver_locks", {}).setdefault((reg.key, key), asyncio.Lock())
        if flight.locked():
            raise rc.SolverBusy()
        from .event_board import autofill_of

        async with flight:
            try:
                af = autofill_of(self)
                if is_lock:
                    af.stop_all()
                else:
                    run = self.raids.store(reg).events.get(key) if getattr(self, "raids", None) is not None else None
                    af.stop_event(reg.key, rc.event_id(run) if run is not None else None)
            except Exception:  # noqa: BLE001 — an auto-fill bug never touches the lock / fill it wraps
                log.exception("auto-fill stop failed (%s %s)", reg.key, key)
            yield

    async def lock_preview(self, reg, rs, ev, *, by: str, by_id: int | None = None, fill: bool = True) -> list:
        """The roster(s) the lock would build NOW (rc.split_preview with the run's strategy, honouring the board and the
        pins), under the run's solver single flight — the Discord Preview lock and the site's lock preview share it. A
        later raid whose earlier raid is still open solves with who those raids will roster (rc.earlier_counts), so it
        shows what its lock builds once they lock.
        Nothing about the run changes but `ev.last_proposal` (the layout shown, with who saw it: the same officer's plain
        text "use it" puts it on the board, also after a restart — configops._propose). RegistryError (SolverBusy, or the
        solver's own sentence) when no preview can be made. `fill=False`: the lock as it stands (rc.board_as_it_stands,
        no solve) — an empty board previews as the empty roster a person's lock writes (F3)."""
        if ev.state != "open":
            raise RegistryError(f"{run_title(reg, ev)} is already {rc.RUN_STATE_WORDS.get(ev.state, ev.state).split(' — ')[0]}, and the preview is for an open sheet.")
        strategy = rc.run_strategy(reg, ev)  # the lock's own strategy and budget: the preview is the roster the lock builds
        if not fill:  # exactly the board: nothing to solve
            rosters = rc.board_as_it_stands(reg, rs, ev)
            ev.last_proposal = {"layout": [list(g) for r in rosters for g in r.groups], "strategy": strategy, "rev": rc.board_rev(reg, ev),
                                "by": by, "by_id": by_id, "at": rc.now(), "kind": "lock preview"}
            await asyncio.to_thread(rs.save, ev, f"lock preview by {by}")
            return rosters
        async with self.solving(reg, ev.key):
            try:  # a later raid whose earlier raid is still open: their lock solves first, as the board and the lock read them
                earlier = await asyncio.to_thread(rc.earlier_counts, reg, rs, ev)
                layout, rosters = await asyncio.to_thread(rc.split_preview, reg, rs, ev, strategy, None, rc.SOLVE_TIME_LIMIT_S, earlier)
            except RuntimeError as e:
                raise RegistryError(rc.solver_error_text(e)) from e
        ev.last_proposal = {"layout": layout, "strategy": strategy, "rev": rc.board_rev(reg, ev), "by": by, "by_id": by_id, "at": rc.now(), "kind": "lock preview"}
        await asyncio.to_thread(rs.save, ev, f"lock preview by {by}")
        return rosters

    async def lock_run(self, reg, rs, ev, by: str = "scheduler", fill: bool = True, by_person: bool = False) -> str:
        """Lock the sheet, build the roster(s) from the signups (pins honoured), show them on the sheet and in the
        officer channel, and DM every rostered member for confirmation. One lock per run at a time (solving: a second
        caller gets rc.SolverBusy); the board lock is held from the snapshot to the state flip, so a move made during
        the solve waits and is then refused as stale (never silently lost); a run no longer open is refused. Answers
        and pins take no lock while the sheet is open, so one written during the solve (a No thanks, a pin out) makes
        the lock solve again on the live run (rc.solve_inputs), up to rc.LOCK_SOLVES times; still moving after that,
        the lock fails and the sheet stays open — nobody is rostered from answers they already changed.
        `fill` (issue 14): True fills the open seats with the solver (the board's places kept, the Bench last); False locks
        the board AS IT STANDS (rc.board_as_it_stands: exactly the placed names in their groups, nobody added) — the
        clock's lock fails with rc.LOCK_EMPTY_BOARD when people answered and nobody is placed, a person's writes that
        raid's roster empty (F3: the owner's choice) — and, seats left open with Automated Fill Requests on, pauses those
        asks for the event. Every later raid of the event locks with the same choice
        (lock_parts; stored as `ev.lock_fill`). A confirm deadline already past at the lock becomes rc.late_confirm_by's.
        `by_person` (a lock a person pressed: the site, Discord, plain text, MCP): a lock the board can't make — everyone
        who joined kept off (rc.lock_all_off) — raises RegistryError with that sentence and writes nothing (no lock_error,
        no "lock failed" line): the box refused it a moment ago or the board changed since; an empty board locks empty
        (every later raid of the event too, lock_parts carrying `by_person`). The scheduler keeps the failure path for
        an empty board (its retry and back-off read it).
        A lock after an unlock reconciles the Confirm asks: a rostered person with a yes on the event is not asked again,
        one with an ask still open keeps it (its card redrawn with the new group), a newly rostered one is asked, and one
        who held an ask or a yes and is off the roster now has it closed (`unrostered`, the card edited) and gets the Not
        rostered notice — at most one per person per raid."""
        if rc.is_follow(ev) and any(p.state == "open" for p in rc.event_parts(rs, ev) if rc.part_no(p) < rc.part_no(ev)):
            raise RegistryError(rc.LATER_PART_LOCK.format(first=run_title(reg, rc.first_part(rs, ev))))
        if (tail := self.unlock_finishing(reg, rc.first_part(rs, ev))) is not None:
            # an unlock still editing its Discord cards: a person's lock waits for it (bounded), the clock's tries next minute
            if by_person:
                await asyncio.wait({tail}, timeout=UNLOCK_WAIT_S)
            if not tail.done():
                raise RegistryError(rc.LOCK_UNLOCKING)
        team = rc.run_team(reg, ev)
        async with self.solving(reg, ev.key, lock=True), self.board_lock(reg, ev.key):
            if ev.state != "open":
                raise RegistryError(f"{run_title(reg, ev)} is already {ev.state}.")
            if by_person:  # read under board_lock: the board can't change between this and the lock
                if fill and rc.lock_all_off(reg, rs, ev):
                    raise RegistryError(rc.SOLVER_WORDS["all_off"])
            # solve on a copy first: the run only becomes locked once a roster exists, so a failed solve (or a crash
            # mid-way) leaves the sheet open rather than a locked run with no roster
            for _ in range(rc.LOCK_SOLVES):
                trial = ev.model_copy(deep=True)
                seen, inputs = len(trial.log), rc.solve_inputs(trial)
                ev.lock_tried_at = rc.now()
                if not fill:  # as it stands: read from the board under board_lock, so nothing moves meanwhile
                    # the clock's lock: an empty board anywhere in the event fails it whole, before any raid locks; a
                    # person's lock writes an empty raid's roster empty (F3)
                    why = None if by_person else rc.lock_empty_refusal(reg, rs, trial)
                    if why is None:
                        trial.rosters = rc.board_as_it_stands(reg, rs, trial)
                    break
                try:
                    await asyncio.to_thread(rc.propose, reg, rs, trial, False)
                except Exception as e:  # noqa: BLE001 — solver infeasible / timed out
                    why = rc.solver_error_text(e)
                    break
                why = None
                if ev.state != "open" or rc.solve_inputs(ev) == inputs:
                    break
                ev.log.append(f"lock ({by}): answers or pins changed while the groups were being built — building again")
            else:
                why = rc.SOLVER_WORDS["changing"]
            if why is not None:
                first = rc.lock_failure_kind(ev.lock_error) != rc.lock_failure_kind(why)  # old words, same failure: no new line
                ev.lock_error = why
                ev.log.append(f"lock ({by}) failed: {why}")
                rs.save(ev, "lock failed")
                # names in the sentence (a conflict names people) and `by` are member text: escaped on the Discord lines
                esc = lambda t: discord.utils.escape_mentions(discord.utils.escape_markdown(str(t)))  # noqa: E731
                if first:  # the SAME failure retried (the clock every 15 minutes) is said once: thread line and ops warning
                    await self.post_run_update(reg, ev, f"⚠️ The lock by {esc(by)} failed. {esc(why)} The sheet stays open.")
                    await self.refresh_sheet(reg, ev)  # the sheet's footer says so too: the signup channel's truth matches the thread's
                    await self.ops.emit(reg.config, "warn", f"{run_title(reg, ev)}: lock failed — {esc(why)}")
                return f"{run_title(reg, ev)}: lock failed — {why}"
            if ev.state != "open":  # cancelled while the roster was being built
                raise RegistryError(f"{run_title(reg, ev)} was {ev.state} before the roster was ready, so it was not locked.")
            _, _, confirm = run_times(reg, ev, team)
            confirm = rc.late_confirm_by(confirm, ev.start, reg.now_local(), lock=True)  # a late lock never asks with a deadline already past
            confirms = rc.run_confirms(reg, ev)  # off (ad hoc): no confirmation DMs and no deadline — no seat ever expires
            ev.rosters = trial.rosters
            ev.log.extend(trial.log[seen:])  # the solver's lines; lines written on the live run meanwhile stay
            ev.state, ev.locked_at, ev.confirm_by, ev.lock_error = "locked", rc.now(), confirm.isoformat() if confirms else None, None
            ev.placed_by, ev.drag_pins, ev.auto_placed, ev.placed_as, ev.played_as = {}, {}, [], {}, {}  # who placed whom was the board before the lock; the roster is the board now
            ev.lock_seats = [uid for r in ev.all_rosters for g in r.groups for n in g if (uid := rc.uid_of_name(ev, n)) is not None]
            ev.lock_fill = fill
            ev.log.append(f"locked by {by}" + ("" if fill else " as the board stood"))
            paused_too = []  # the event's other raids this lock pauses (saved once the flip is committed)
            if not fill and rc.open_seat_count(reg, ev) and rc.team_setting(team, "autofill"):
                # the officer left seats open on purpose: nobody is DMed for them until an officer resumes the asks —
                # on every raid of the event, so no later raid's seats are asked for either
                for p in (ev if p.key == ev.key else p for p in rc.event_parts(rs, ev)):
                    if p.state in ("done", "cancelled") or p.autofill_paused:
                        continue
                    p.autofill_paused, p.autofill_paused_by = True, by
                    p.log.append(f"Automated Fill Requests paused by {by}: the lock left seats open for an officer to fill")
                    if p is not ev:
                        paused_too.append(p)
            held = [a for a in ev.fill_asks if a.held and a.phase == "prelock"]
            pre_gone = rc.release_prelock_asks(ev)  # pre-lock asks expire at lock time (a yes held for a tied partner too)
            short = rc.needs(reg, ev, team)["roles"]
            if short:  # the solver caps a minimum at what the sheet can supply: say so instead of locking quietly
                ev.log.append("locked short " + ", ".join(f"{k} {r}" for r, k in short.items()))
            rs.save(ev, f"locked by {by}")  # flipped AND committed before anything is posted or sent: resumable (recover_lock)
            for p in paused_too:
                rs.save(p, f"autofill paused by {by} at the lock")
        confirming = self._confirming()
        confirming.add((reg.key, ev.key))  # the scheduler's recovery step leaves the run alone while this fan-out runs
        posting = self._lock_posting()  # an unlock waits until the cards, the asks and the "is locked" line are all out
        posting.add((reg.key, ev.key))
        try:
            try:
                if short:
                    await self.ops.emit(reg.config, "warn", f"{run_title(reg, ev)}: locked short " + ", ".join(f"{k} {r}" for r, k in short.items()))
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
                    await self.ops.emit(reg.config, "warn", f"{run_title(reg, ev)}: lock cards not posted. {self.no_officer_channel_why(reg, ev)}")
                dropped = await self.reconcile_asks(reg, ev, team) if ev.state == "locked" else []
                sent = await self.send_confirmations(reg, rs, ev, team, by) if confirms and ev.state == "locked" else 0
            finally:
                confirming.discard((reg.key, ev.key))
            if ev.state == "locked":  # joiners left off: one notice each (D26), never a DM — a seat offered later is the DM
                rc.leftover_notices(reg, ev, also=dropped)  # on the loop: member files are mutated where the buttons mutate them
            if ev.state != "locked":  # cancelled during the fan-out: the cancel carried its own lines; no "is locked" line now
                return f"{run_title(reg, ev)}: locked by {by}, then {ev.state} before the confirmations finished ({sent} sent)"
            if confirms and ev.state == "locked":
                await self.refresh_cards(reg, ev)  # the lock card's "confirmations not yet sent" line goes
            sig = await self.sheet_thread(reg, ev) if ev.channel_id else None  # the event's thread on its sheet, never loose
            if sig is not None:
                head = f"🔒 {run_label(reg, ev)}" + (f" · after {rc.lead_name_of(reg, ev)}" if rc.is_follow(ev) else "")
                n_seated = sum(len(r.selected) for r in ev.all_rosters)
                if not n_seated:  # an empty roster an officer locked on purpose (F3): nobody to confirm, an officer seats people
                    text = f"{head} is locked with nobody rostered yet. An officer decides who takes a seat, and nothing is asked of you now."
                else:
                    then = ("Rostered? confirm in your DMs. Not rostered? nothing to do." if confirms
                            else "Rostered? you're in — press Can't make it on the sheet if that changes. Not rostered? nothing to do.")
                    text = f"{head} is locked: {n_seated} rostered" + (f" in {len(ev.all_rosters)} rosters" if len(ev.all_rosters) > 1 else "") + f". {then}"
                try:
                    line = await sig.send(text, allowed_mentions=discord.AllowedMentions.none())
                    if getattr(line, "id", None):  # kept, so an unlock can take the line back
                        ev.lock_line = [sig.id, line.id]
                        await asyncio.to_thread(rs.save, ev, "lock line posted")
                except Exception as e:  # noqa: BLE001
                    log.warning("lock line failed (%s): %s", ev.key, e)
        finally:
            posting.discard((reg.key, ev.key))
        first = rc.first_part(rs, ev)
        chaining: set = self.__dict__.setdefault("_parts_locking", set())
        tail = None if (reg.key, first.key) in chaining else await self.lock_parts(reg, rs, first, by, fill=fill, by_person=by_person)
        return f"{run_title(reg, ev)}: locked by {by}, {len(ev.all_rosters)} roster(s), {sent} confirmation DM(s)" + (f" · {tail}" if tail else "")

    async def lock_parts(self, reg, rs, first, by: str, fill: bool = True, by_person: bool = False) -> str | None:
        """The first raid's lock builds every later raid of the run (design §5.62), in order: each solves from the run's
        answers with the people rostered on the raids before it preferred (rc.LEAD_SEAT_BONUS), flips to locked and posts
        its own lock cards; it never confirms. A short role never stops one (the solver rosters whoever can play and the
        part locks short). It stops at the first part that stays open: a failed solve leaves that part open with its
        lock_error, like any run, and every part after it waits (the scheduler retries the first open part, then the
        rest). Anything a part's lock raises (rc.SolverBusy, a cancel between the state check and the lock, a Discord
        error) never turns an earlier lock into a failure: one ops warn per part and the tail "follow-on not locked:
        <why>". Also called after a later part's own lock (the scheduler's retry) to carry on down the chain. None when
        nothing was tried. `fill`: the first raid's lock choice (issue 14), every later raid locks with it; `by_person`
        too (a person's lock writes an empty later raid empty, F3). → the parts' lines, joined."""
        if first.state != "locked":
            return None
        chaining: set = self.__dict__.setdefault("_parts_locking", set())
        chaining.add((reg.key, first.key))
        tails: list[str] = []
        try:
            for fev in rc.later_parts(rs, first):
                if fev.state == "locked":
                    continue
                if fev.state != "open":  # a later part cancelled or ended on its own (older runs): nothing after it is built
                    break
                try:
                    line = await self.lock_run(reg, rs, fev, by=by, fill=fill, by_person=by_person)
                except Exception as e:  # noqa: BLE001 — an earlier part stays locked whatever happens to a later one
                    why = str(e) or type(e).__name__
                    log.warning("later raid lock failed (%s after %s): %s", fev.key, first.key, why)
                    warned: set = self.__dict__.setdefault("_follow_lock_warned", set())
                    if (reg.key, fev.key) not in warned:
                        warned.add((reg.key, fev.key))
                        await self.ops.emit(reg.config, "warn", f"{run_title(reg, fev)}: later raid not locked with {run_title(reg, first)}: {why}")
                    tails.append(f"later raid not locked: {why}")
                    break
                tails.append(line)
                if fev.state != "locked":  # its solve failed: it stays open with its lock_error, the parts after it wait
                    break
        finally:
            chaining.discard((reg.key, first.key))
        return " · ".join(tails) or None

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
        if rc.open_ask(m, ev) is not None:
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
            if ev.state != "locked" or ask.get("answer") is not None:
                # closed while this DM was in flight (a cancel collects the cards it edits before this one had an id;
                # a freed seat closes the ask): the card goes to its final state at once — never a live Confirm
                await self.edit_ask_card(reg, cid, mid, f"Cancelled — {run_label(reg, ev)} was cancelled." if ev.state == "cancelled"
                                         else f"{run_label(reg, ev)} is {ev.state} now." if ev.state != "locked"
                                         else f"Your seat on {run_label(reg, ev)} was released. If that's wrong, tell an officer."
                                         if ask.get("answer") == "released" else f"You already answered for {run_label(reg, ev)}.")
        elif not ok:  # a failed DM is a channel change, never an answer: the ask waits on the site, same deadline
            ask["channel"] = "web"
            reg.save(m, f"{m.display_name}'s confirmation for {ev.team} waits on the site (DM failed)")
            ev.log.append(f"DM failed — waits on the site: {m.display_name}'s confirmation")
        return ok

    async def reconcile_asks(self, reg, ev, team) -> list[int]:
        """A lock's Confirm asks against the asks the event already holds (a lock after an unlock; on a first lock there
        are none): everyone with an open ask or a standing yes who is off the roster now has the ask closed
        (`unrostered`) or the yes lapsed, and their card edited to say so — never a new DM; everyone rostered with an ask
        still open keeps it, its card redrawn with the group they are in now. → the ids left off (their Not rostered
        notice is leftover_notices')."""
        label = run_label(reg, ev)
        dropped: list[int] = []
        for m, ask in rc.relock_drops(reg, ev):
            if ask.get("answer") is None:
                reg.close_placement_ask(m.discord_id, ev.team, "unrostered", "the lock")
            else:
                rc.lapse_yes(reg, ev, m.discord_id)
            ev.log.append(f"confirmation closed: {m.display_name} is not on the roster at this lock")
            dropped.append(m.discord_id)
            if ask.get("dm"):
                await self.edit_ask_card(reg, ask["dm"][0], ask["dm"][1], f"{label} locked again without you on the roster, so this question is closed.")
        for m, ask in rc.open_placement_rows(reg, ev):
            seat, sg = ev.seat_of(m), ev.signups.get(str(m.discord_id))
            if not ask.get("dm") or seat is None or sg is None:
                continue
            gi = next((k for k, g in enumerate(ev.all_rosters[seat[0]].groups) if seat[1].signup_name in g), 0)
            await self.edit_live_card(reg, ask["dm"][0], ask["dm"][1],
                                      confirm_layout(reg, ev, team, self.ico, sg, seat[0], gi, m.discord_id, reminder=bool(ask.get("reminded_at"))))
        return dropped

    async def send_confirmations(self, reg, rs, ev, team, by: str, *, resume: bool = False) -> int:
        """The lock's Confirm / Can't fan-out (confirm_one per seat), re-checking the run is still locked before every
        ask (a cancel during the fan-out leaves no orphan asks). `resume`: the scheduler's recovery step — only the seats
        nobody asked (rc.unasked_seats). Stamps `confirmations_sent_at` once every seat has been reached."""
        sent = 0
        seats = rc.unasked_seats(reg, ev) if resume else [
            (i, gi, ev.signups.get(str(uid))) for i, r in enumerate(ev.all_rosters) for gi, g in enumerate(r.groups) for n in g
            if (uid := rc.uid_of_name(ev, n)) is not None]
        # rc.confirm_plan, the reader the lock box counts with: a standing yes is not asked again (an open ask is
        # confirm_one's to skip)
        confirmed = set() if resume else set(rc.confirm_plan(reg, ev, [sg.discord_id for _i, _g, sg in seats if sg]).confirmed)
        for i, gi, sg in seats:
            if ev.state != "locked":
                break
            if sg and sg.discord_id in confirmed:
                continue  # a lock after an unlock: they already confirmed this event, and their seat never went
            if sg and await self.confirm_one(reg, ev, sg, i, gi, by):
                sent += 1
        if ev.state == "locked":
            ev.confirmations_sent_at = rc.now()
        ev.log.append(f"confirmation DMs sent to {sent}" + (" (resumed after an interrupted lock)" if resume else ""))
        rs.save(ev, f"confirmations {sent}")
        return sent

    def _lock_posting(self) -> set:
        """(guild key, run key) whose lock is still posting after its flip — lock cards, Confirm asks, notices, the "is
        locked" line — in this process: unlock_run refuses it (rc.UNLOCK_SENDING), so nothing lands on an open event."""
        return self.__dict__.setdefault("_lock_posting_runs", set())

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
        if ev.state != "locked" or rc.lock_unfinished(ev) or (reg.key, ev.key) in self._confirming():
            return 0  # (an empty roster a person locked on purpose is finished: its card is checked, nobody is asked)
        posting = self._lock_posting()  # the resumed fan-out is the lock's: an unlock waits for it (rc.UNLOCK_SENDING)
        posting.add((reg.key, ev.key))
        try:
            return await self._recover_lock(reg, rs, ev)
        finally:
            posting.discard((reg.key, ev.key))

    async def _recover_lock(self, reg, rs, ev) -> int:
        """recover_lock's work, inside the lock's still-posting guard."""
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
        await self.ops.emit(reg.config, "warn", f"{run_title(reg, ev)}: lock recovery — {len(missing)} rostered had no confirmation ask; asked them ({sent} by DM)")
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
        await ops_mod.audited(self.ops, reg, by, f"edited the {run_title(reg, ev)} board" + (f": +{len(added)}" if added else "") + (f" -{len(removed)}" if removed else ""), via=via)

    # ---- the board: one writer at a time per run, whoever writes (the site's drags, MCP, plain text)
    def board_lock(self, reg, key: str) -> asyncio.Lock:
        """One writer at a time per run's board: concurrent officers queue here (short), they never overwrite each other.
        Keyed (guild, run key)."""
        return self.__dict__.setdefault("_board_locks", {}).setdefault((reg.key, key), asyncio.Lock())

    async def _write_board(self, reg, rs, ev, groups: list[list[str]], by: str, via: str = "web", auto: list[str] | None = None) -> str:
        """The one way a board is written (whole board, or the result of a single move); the caller holds board_lock.
        Before lock the groups are the layout the lock will use; after lock they ARE the roster (someone brought in
        from the bench is asked to confirm, someone taken out is freed and the fill engine asked). `auto` (Auto-fill
        groups' write, event_board.AutoFill): the names it placed, recorded as ev.auto_placed."""
        names = rc.board_names(ev)  # Join and Bench while open; Join and the rostered once locked
        groups = [[n for n in g if n in names] for g in groups]
        if ev.state == "open":
            ev.layout = groups if any(groups) else None
            placed = {n for g in groups for n in g}
            # an officer's own move stays theirs while it stands: a name still in the layout, a pin still as their drag left it
            ev.placed_by = {n: u for n, u in ev.placed_by.items() if n in placed}
            ev.drag_pins = {u: d for u, d in ev.drag_pins.items() if ev.pins.get(u) == d.get("now")}
            ev.auto_placed = [n for n in (ev.auto_placed if auto is None else auto) if n in placed]
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
        written. Join and Bench names count (rc.board_names); a Bench name on it is recorded as auto-placed, so the
        lock decides it again — except one an officer dragged that the write leaves in the same group; returns the line
        to show. `via`: the surface, for the audit line's prefix.
        `strategy`: the split philosophy the layout came from (Use this split) — written in the SAME step, under the same
        lock and rev check, so a strategy can't be saved while its layout is refused (design B6, one request)."""
        state = ev.state
        async with self.board_lock(reg, ev.key):
            if (rev and rev != rc.board_rev(reg, ev)) or ev.state != state:  # a write queued behind a lock is stale
                raise rc.BoardChanged()
            if strategy:
                ev.split_strategy = strategy
            # its Bench names are decided again at the lock, except an officer's drag the write leaves in its group
            return await self._write_board(reg, rs, ev, groups, by, via=via, auto=rc.board_write_auto(ev, groups))

    async def board_move(self, reg, rs, ev, name: str, to: tuple[int, int] | None, by: str, *, rev: str | None = None, via: str = "web",
                         expect_state: str | None = None, by_id: int | None = None) -> str:
        """One move applied to the board as it is NOW (so two officers moving at once merge): `to` = (flat group
        index, position) — group index = roster × groups per roster + group — or None for the bank. Dropping on
        someone swaps (the displaced person stays a joiner). ValueError when the group is full or `name` hasn't joined.
        `rev`: plain text's preview fingerprint — the move was worded against that board, so a changed one raises
        rc.BoardChanged (the site's drags pass none: they always act on the board as it is). `expect_state`: the state
        the board was RENDERED in (the site sends it): a drag made on the open board and dropped after the scheduler
        locked it is refused (BoardChanged) instead of being applied as a post-lock roster edit (a confirmation DM, a
        freed seat) — design B6.
        Before the lock the board is the officers' own (the owner's tweaks C): a drop on Not placed only un-places the
        name (no pin — the lock still places them; keeping someone off is the ⋮ pin), a swapped partner from there goes
        back to Not placed, and a place in a group lifts a kept-off pin (recorded, so Undo my moves puts it back).
        `by_id`: the officer, recorded per name moved (ev.placed_by) so Undo my moves drops only theirs; any moved
        name stops being an Auto-fill placement (rc.record_moves)."""
        state = ev.state
        async with self.board_lock(reg, ev.key):
            if (rev and rev != rc.board_rev(reg, ev)) or ev.state != state or (expect_state and ev.state != expect_state):
                raise rc.BoardChanged("The event locked while you were dragging — the board is the roster now; check it and move again." if expect_state == "open" and ev.state != "open" else rc.BoardChanged().args[0])
            pins_before = dict(ev.pins)
            at_before = {n: gi for gi, g in enumerate(rc.board_layout(reg, ev)) for n in g}
            groups = rc.apply_move(reg, ev, name, to)
            if ev.state == "open":
                sg = rc.signup_by_name(ev, name)
                if to is not None and sg is not None and ev.pins.get(str(sg.discord_id)) == "out":
                    ev.pins.pop(str(sg.discord_id))  # placed in a group: no longer kept off
                rc.record_moves(ev, name, at_before, groups, pins_before, by_id)
            return await self._write_board(reg, rs, ev, groups, by, via=via)

    async def undo_moves(self, reg, rs, ev, by_id: int, by: str, *, rev: str | None = None, via: str = "web") -> str:
        """Undo my moves (the Events board, before the lock): exactly what the officer `by_id`'s drags did and still
        stands is reversed — the names they placed leave the officers' layout, and each pin their drag changed goes back
        to what it was (rc.own_pins: only while the pin is still as the drag left it; a pin set from the ⋮ menu or in
        plain text is never touched). Everyone else's moves stay; the names undone go back to Not placed. `rev` =
        rc.undo_rev the officer saw (the board and their moves): a changed one raises rc.BoardChanged, nothing written."""
        async with self.board_lock(reg, ev.key):
            if ev.state != "open" or (rev and rev != rc.undo_rev(reg, ev, by_id)):
                raise rc.BoardChanged()
            placed = {n for n, u in ev.placed_by.items() if int(u) == int(by_id)}
            pins = rc.own_pins(ev, by_id)
            for u, d in pins.items():
                if d.get("was") is None:
                    ev.pins.pop(u, None)
                else:
                    ev.pins[u] = d["was"]
            ev.drag_pins = {u: d for u, d in ev.drag_pins.items() if u not in pins}
            ev.placed_by = {n: u for n, u in ev.placed_by.items() if n not in placed}
            groups = [[n for n in g if n not in placed] for g in rc.board_layout(reg, ev)]
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
            raise RegistryError(f"{run_title(reg, ev)} is a test event: only test members and the officer running it can answer it")
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
        if ev.followed_by:  # a lead's No thanks / Can't is the whole night
            await self.after_lead_out(reg, rs, ev, m, "callout" if mine else "officer", note)
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.refill_after_release(reg, rs, ev, released)
        await self.post_run_update(reg, ev, f"⚑ {m.display_name} ({co.character}) called out, {co.hours_before:.0f}h before" + ("" if mine else f" (by {by})"))
        await self.ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {run_title(reg, ev)} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else "") + ("" if mine else f" (by {by})"))
        return line

    async def after_lead_out(self, reg, rs, lead, m, why: str, note: str | None) -> None:
        """A No thanks or Can't on a lead from someone NOT rostered on it (a bench joiner covering a follow-on gap) is
        the whole night too (design §5.62): the follow-on's row is already out (rc.sync_follow), and
        a seat they hold on the locked follow-on is freed the way the lead's is (drop_seated: thread line, fill). A seat
        freed on the lead takes the follow-on's with it in drop_seated / rc.free_seat."""
        if rc.is_follow(lead):
            return
        for fev in rc.later_parts(rs, lead):  # every later raid of the run where they hold a seat
            if sheet_state(fev) == "locked" and fev.seat_of(m):
                await self.drop_seated(reg, rs, fev, rc.run_team(reg, fev), m, why, note)

    async def stay_for_follow(self, reg, rs, ev, m, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """I'm staying: undo leaving early on a follow-on (rc.stay_for_follow). The site's Me row (POST /api/me/run/{key}/stay) is its one caller."""
        if not rc.stay_for_follow(reg, rs, ev, m):
            lead = rc.lead_name_of(reg, ev) or "the first raid"
            if by_id is not None and int(by_id) == m.discord_id:  # their own press: second person
                raise RegistryError(f"You're not marked as leaving after {lead}.")
            raise RegistryError(f"{m.display_name} isn't marked as leaving after {lead}.")
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"↩ {m.display_name} is staying for {run_label(reg, ev)}")
        await ops_mod.audited(self.ops, reg, by, f"{m.display_name} is staying for {run_title(reg, ev)}", via=via, by_id=by_id)
        return f"Noted: you're staying for {run_label(reg, ev).replace('**', '')}. A freed seat can come back to you through Fill."

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
        if ev and m and not yes and ev.state == "open":  # an unlocked event (issue 14): the ask stayed open; a Can't is
            await self.release_asks_of(reg, rs, ev, m, "answered Can't make it")  # their answer out, off the board
            async with self.board_lock(reg, ev.key):
                sg = ev.signups.get(str(uid))
                if sg is not None and sg.status != "out":
                    sg.status, sg.source, sg.joined_at, sg.benched_at, sg.updated_at = "out", "declined", None, None, rc.now()
                    ev.log.append(f"{m.display_name} can't make it (answered their confirmation while the sheet was open)")
                    await asyncio.to_thread(rs.save, ev, f"{m.display_name} out")  # prune_board takes the name off the board
        elif ev and m and not yes:
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
        return await self.edit_ask_card(reg, ask["dm"][0], ask["dm"][1], f"Your seat on {run_label(reg, ev)} was released ({reason}). If that's wrong, tell an officer.")

    async def drop_seated(self, reg, rs, ev, team, m, why: str, note: str | None, announce: bool = True, *, notice: bool = True) -> None:
        """A rostered member is out after lock: free the seat, edit their ask card, post it, ask the bench to fill.
        `notice=False`: their own act (rc.seat_gone writes them no notice about it). On a lead the follow-on's seat goes
        too (rc.free_seat), and the follow-on's thread line and fill follow the lead's (design §5.62)."""
        held = ([p for p in rc.later_parts(rs, ev) if sheet_state(p) == "locked" and p.seat_of(m) is not None]
                if ev.followed_by and not rc.is_follow(ev) else [])  # out of the first raid is out of every later one
        rc.free_seat(reg, rs, ev, m, why, notice=notice)
        await self.seat_released(reg, ev, m, why)
        if announce:
            await self.post_run_update(reg, ev, f"↩️ {m.display_name} is out ({why}{f': {note}' if note else ''}) — seat freed")
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if rc.team_setting(team, "autofill") and sheet_state(ev) == "locked":
            sent, nd = await self.run_fill(reg, rs, ev, team, by=why)
            if sent:
                await self.post_run_update(reg, ev, f"🧩 replacement for {m.display_name}:\n🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        for fev in held:  # rc.free_seat freed them with the first raid's: each later raid's own line, cards and fill
            await self.drop_seated(reg, rs, fev, rc.run_team(reg, fev), m, why, note, announce, notice=False)

    # ---- opening and cancelling runs (commands, the web and plain-text ops share these)
    async def open_run_and_post(self, reg, rs, instance: str, start: datetime, by: str, schedule: str | None = None, follow: dict | None = None,
                                lineup: list[dict] | None = None, lock_hours: float | None = None, first_rosters: int | None = None,
                                auto_lock: bool | None = None, auto_lock_fill: bool | None = None) -> rc.RaidEvent:
        """Open (or find) the run for `instance` at `start` (from `schedule`, None = the schedule with a run at exactly
        that start, else a one-off — rc.open_run), post its
        sheet in the signup channel when one is set (else the event stays unposted: channel_id None, `/raid sheet` can
        place it), and say so on the ops feed. A start more than a few minutes in the past is refused
        (rc.refuse_past_start) — every on-demand open comes through here; the scheduler only opens future starts. A
        schedule's night asked as a one-off, or one a live one-off holds, is refused (rc.open_refusal); however far ahead, it opens.
        `lock_hours` / `first_rosters` / `auto_lock` / `auto_lock_fill`: the one-off's open form (the New event page,
        /raid open): its Locks choice, first raid's rosters, Auto Lock and auto-fill (None = the schedule's)."""
        rc.refuse_past_start(reg, start)
        if (why := rc.open_refusal(reg, rs, instance, start, schedule)) is not None:  # a schedule's night: never a one-off beside it
            raise RegistryError(why)
        ev = rc.open_run(reg, rs, instance, start, by=by, schedule=schedule, follow=follow, lineup=lineup, lock_hours=lock_hours, first_rosters=first_rosters,
                         auto_lock=auto_lock, auto_lock_fill=auto_lock_fill)
        channel = self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None
        if channel and not ev.message_id:
            await self.post_sheet(reg, rs, ev, channel)
        if not ev.health_early and not ev.health_posted and rc.early_warning(reg, rs, ev):
            officer_ch = self.officer_channel(reg, ev)
            if officer_ch is not None:
                await self.post_health(reg, rs, ev, officer_ch, nudge=False, early=True)
        await self.ops.emit(reg.config, "info", f"{by} opened {run_title(reg, ev)} ({len(ev.signups)} pre-filled out from absences)" + ("" if ev.message_id else " — no signup channel: sheet not posted")
                             + "".join(f" then {run_title(reg, p)}" for p in rc.later_parts(rs, ev)))
        return ev

    async def open_test_run(self, reg, rs, raid: str, *, start_in: int, nudge_in: int, lock_in: int, confirm_in: int, dm: bool, by: str, by_id: int | None,
                            channel=None, via: str = "discord") -> tuple[rc.RaidEvent, object]:
        """The one test-run opener (/gm test run, plain text's `test run …`, the site's test bench and so MCP): a real
        sheet with minute-level cadence (`*_in` = minutes; start > nudge > lock > confirm), the pool = puppets + the
        tester (`by_id`), `dm` = DM every puppet when it opens (the puppets' DMs reach the tester as one digest). The
        sheet goes to the signup channel, else to `channel` (where it was asked for); with neither (the site, MCP, a DM
        without a signup channel) it is refused before anything is written, naming the setting (NO_TEST_CHANNEL). Its
        officer cards post in `channel` when it is a server channel (D20; officer_channel), and a rehearsal is found only
        by a rehearsal (rc.live_at with test=True), so it never shares a real run's roster. The audit line (ops.audited, `via` = the surface) is the verb's own. Returns (the run, the channel it was posted in or None). rc.open_run runs on the event loop, as in
        open_run_and_post: it adds to the live events the loop's handlers iterate, so never from a worker thread (L3);
        its one git commit is the same short block every on-demand open has."""
        start = (reg.now_local() + timedelta(minutes=start_in)).replace(second=0, microsecond=0)
        in_guild = guild_routing.in_server_channel(channel)
        target = (self.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else None) or (channel if in_guild else None)
        if target is None:  # nowhere to post the sheet: refused before anything is written (the site, MCP, a DM), naming why
            raise RegistryError(bench_channel_refusal(reg, self.get_channel) or NO_TEST_CHANNEL)
        cut = {"soft": nudge_in / 60, "hard": lock_in / 60, "confirm": confirm_in / 60, "open_dm": bool(dm), "test_by": by_id}
        if in_guild:
            cut["test_channel"] = channel.id  # its health / lock cards post where it was asked for (D20)
        ev = rc.open_run(reg, rs, raid, start, by=by, cutoffs=cut)
        if target is not None and not ev.message_id:
            await self.post_sheet(reg, rs, ev, target)
        await ops_mod.audited(self.ops, reg, by, f"test bench: opened test event {run_title(reg, ev)} (lock in {start_in - lock_in} min)", via=via, level="warn", by_id=by_id)
        return ev, target

    async def cancel_run(self, reg, rs, ev, by: str, reason: str | None, *, notify: bool = True, by_id: int | None = None, via: str = "discord",
                         audit: bool = True, _tally: list | None = None) -> str:
        """Cancel a run — the ONE verb behind the card's Cancel, /raid cancel, the site, plain text and MCP. Under the run's
        board lock, so a lock in progress finishes its flip first (lock_run then re-checks the state before every
        confirmation); refused when the run is already done / cancelled. Nobody is DMed (the DM rule): every open ask
        (Confirm / Can't, fill, pre-lock) closes as `cancelled` and its DM card is EDITED to say so; the sheet is edited to
        its final state; `notify` posts one fixed line in the event's thread on its sheet (sheet_thread; never the reason
        — that stays in the updates thread and the ops feed, for officers; never loose in the signup channel); the Me page's notices are derived from the record. Returns a plain line.
        The fill / pre-lock asks close under plan_lock (every writer of ev.fill_asks holds it), taken AFTER the state
        flip: a fan-out in flight sees the cancel before its next DM (RaidMixin._send_asks), stops, edits a card that
        went out meanwhile, and releases the lock — so the cancel waits for one DM at most, never a whole batch.
        Clearing the test bench passes notify=False: its cancels post nothing where real members read.
        A run of several raids (design §5.62) is cancelled whole from ANY of its raids (rc.cancel_target): a later raid's
        cancel is its first raid's while that one is live; a raid already done / cancelled whose event still has a live
        raid (the first raid ended, a later one still locked) cancels the raids still to play; and the cancel takes every
        other live raid of the run with it — one audit line naming them all. Refused only when no raid of it is live."""
        if audit and (target := rc.cancel_target(rs, ev)) is not ev:
            return await self.cancel_run(reg, rs, target, by, reason, notify=notify, by_id=by_id, via=via, audit=audit)
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
            await self.edit_ask_card(reg, cid, mid, f"Cancelled — {label} was cancelled.")
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        if _tally is not None:  # a raid the event's cancel takes with it: its counts join the ONE line below
            _tally.append((run_title(reg, ev), len(waiting), len(fills)))
            return f"Cancelled {run_title(reg, ev)}."
        posted = False
        sig = await self.sheet_thread(reg, ev) if notify and ev.channel_id else None  # the sheet's thread, never loose
        if sig is not None:
            try:  # fixed text: the reason is for officers, never echoed publicly
                await sig.send(f"🛑 {label} is cancelled." + (" Rostered seats are released." if fx["locked"] else ""),
                               allowed_mentions=discord.AllowedMentions.none())
                posted = True
            except Exception as e:  # noqa: BLE001
                log.warning("cancel line failed (%s): %s", ev.key, e)
        gone, tally = [], []
        if audit:  # the cancel takes every other LIVE raid of the run with it (design §5.62) — a later raid's too, once
            for fev in rc.event_parts(rs, ev):  # the first raid has closed (it ends before raids 3–4 of a long lineup play)
                if fev is not ev and fev.state not in ("done", "cancelled"):
                    await self.cancel_run(reg, rs, fev, by, reason, notify=False, by_id=by_id, via=via, audit=False, _tally=tally)
                    gone.append(run_title(reg, fev))
        n_wait, n_fill = len(waiting) + sum(t[1] for t in tally), len(fills) + sum(t[2] for t in tally)
        raids = [reg.raid_def(p.instance).get("name", p.instance) for p in [ev] + [p for p in rc.event_parts(rs, ev) if run_title(reg, p) in gone]]
        await self.post_run_update(reg, ev, f"🛑 event cancelled by {by}" + (f" ({rc.join_words(raids)})" if len(raids) > 1 else "")
                                   + (f": {reason}" if reason else "")
                                   + (f" · confirmations withdrawn for {n_wait}" if n_wait else "") + (f" · {n_fill} fill ask(s) closed" if n_fill else ""),
                                   whole=True)
        tail = (f" and its later raid {gone[0]}" if len(gone) == 1 else f" and its later raids {', '.join(gone)}") if gone else ""
        if audit:
            await ops_mod.audited(self.ops, reg, by, f"cancelled {run_title(reg, ev)}{tail}" + (f": {reason}" if reason else ""), via=via, level="warn", by_id=by_id)
        sheet = not rc.is_follow(ev)  # a later raid reached here once its first raid has closed: no sheet, no channel of its own
        told = ["the sheet shows it"] if sheet else []
        told += [f"{len(cards) + len(fills)} open ask card(s) now say Cancelled"] if cards or fills else []
        if sheet:
            told.append("a line went to the thread on the sheet" if posted else "no line on the sheet's thread")
        return f"Cancelled {run_title(reg, ev)}. Nobody was DMed" + (": " + ", ".join(told) if told else "") + "."

    async def reopen_run(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Undo an end by mistake, within the hour (design B6; never a cancelled run, D19): rc.reopen_run restores the
        state it ended from, then the sheet and the officer cards are redrawn live and the thread hears it. The loot
        council stays as it was archived (reopen a council with /raid loot). RegistryError with the reason otherwise."""
        async with self.board_lock(reg, ev.key):
            back = rc.reopen_run(reg, rs, ev, by)
        await self.refresh_sheet(reg, ev)
        await self.refresh_cards(reg, ev)
        await self.post_run_update(reg, ev, f"↺ reopened by {by} — {'locked again' if back == 'locked' else 'the sheet is open again'}")
        await ops_mod.audited(self.ops, reg, by, f"reopened {run_title(reg, ev)} ({back})", via=via, by_id=by_id)
        return f"Reopened {run_title(reg, ev)} — {'locked, roster as it was' if back == 'locked' else 'the sheet is open again'}."

    def finish_later(self, reg, ev, what: str, work) -> asyncio.Task:
        """Run `work` (a coroutine: a verb's Discord edits, after its state and audit line are written) as a task the
        process keeps a reference to, so it is never collected mid-way; a failure is logged as a warning naming the
        event, never raised (the verb is done; a card left stale is redrawn by the event's next refresh). F5: a route
        answers once the state is written, and the cards follow."""
        tasks: set = self.__dict__.setdefault("_finishing", set())

        async def run():
            try:
                await work
            except Exception:  # noqa: BLE001 — the verb is written; only its Discord edits are behind
                log.warning("%s of %s/%s: its Discord edits did not all finish", what, reg.key, ev.key, exc_info=True)

        task = asyncio.get_running_loop().create_task(run(), name=f"{what} of {reg.key}/{ev.key}")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def finish_pending(self, timeout: float = 5.0) -> list[str]:
        """Shutdown: wait (at most `timeout` seconds) for every finish_later task still running; the ones left are
        abandoned, each logged as a warning by its name (the verb and `<guild>/<event key>`). → their names."""
        tasks = [t for t in self.__dict__.get("_finishing", set()) if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        left = [t.get_name() for t in tasks if not t.done()]
        for name in left:
            log.warning("shutdown: %s abandoned before its Discord edits finished", name)
        return left

    def unlock_finishing(self, reg, first) -> "asyncio.Task | None":
        """The unlock of `first`'s event whose Discord edits are still running (unlock_run's finish_later task), or None."""
        t = self.__dict__.get("_unlock_finishing", {}).get((reg.key, first.key))
        return t if t is not None and not t.done() else None

    async def unlock_run(self, reg, rs, ev, by: str, by_id: int | None = None, *, via: str = "discord", seen: str | None = None,
                         answer_within: float | None = None) -> str:
        """THE unlock (issue 14) behind every surface: the event `ev` belongs to goes back to open signup with its rosters
        on the board (rc.unlock — every locked raid, any raid of it standing for its first). Under the first raid's solver
        single flight and every raid's board lock (one order, as reschedule takes them), so a lock, a cancel or a board
        write never interleaves; RegistryError with rc.unlock_refusal's sentence, nothing written. Then, as cancel_run
        does: fill asks a fan-out added meanwhile close under plan_lock and every withdrawn fill ask's DM card is edited
        to its final state; the officer channel's lock cards and the signup channel's "is locked" line are deleted (an id
        missing or a message gone fails nothing); a fresh health card goes up when the event had one; the sheet and the
        cards are redrawn. Confirmation asks stay as they are. One thread line, one audit line. Nobody is DMed.
        `answer_within` (the site's route, F5): the state, the cards' bookkeeping and the audit line are written first;
        the Discord edits (deletes, card edits, the health card, the sheet, the thread line) then run as finish_later's
        task and this returns after at most that many seconds, the edits going on behind it. None: they are awaited."""
        first = rc.first_part(rs, ev)
        parts = rc.event_parts(rs, first)

        def sending() -> bool:  # a lock of the event still posting its cards, asks or line (rc.UNLOCK_SENDING)
            chaining = self.__dict__.get("_parts_locking", set())
            return any((reg.key, p.key) in self._lock_posting() or (reg.key, p.key) in chaining for p in parts)

        if (why := rc.unlock_refusal(reg, rs, first, sending=sending())) is not None:
            raise RegistryError(why)
        async with self.solving(reg, first.key), contextlib.AsyncExitStack() as stack:
            for key in sorted(p.key for p in parts):
                await stack.enter_async_context(self.board_lock(reg, key))
            if (why := rc.unlock_refusal(reg, rs, first, sending=sending())) is not None:
                raise RegistryError(why)
            if seen and _cq().unlock_box(reg, first, rs)["fp"] != seen:  # plain text's reviewed box changed since (the site's `seen`)
                raise RegistryError(rc.RUN_CHANGED)
            had = {p.key: ({k: v for k, v in p.cards.items() if k.startswith("lock:")}, p.cards_channel_id, p.lock_line) for p in parts}
            done = rc.unlock(reg, rs, first, by, fills=False)  # the fill asks close below, under plan_lock
        fills: list = []
        gone: list[tuple] = []  # (channel id, message id) of the lock cards and the "is locked" line, taken off the files
        for p in (x for x in parts if x.key in done.raids):
            async with self.plan_lock(reg, p.key):  # an ask a fan-out in flight added after the flip closes too
                late = rc.withdraw_fill_asks(p, by)
                cards, ch_id, line = had[p.key]
                for k in cards:
                    p.cards.pop(k, None)
                p.lock_line = None
                await asyncio.to_thread(rs.save, p, "unlock: lock cards taken down" + (f", {len(late)} ask(s) withdrawn" if late else ""))
            fills += late
            gone += [(ch_id, m) for m in cards.values()] + ([tuple(line)] if line else [])
        await ops_mod.audited(self.ops, reg, by, f"unlocked {run_title(reg, first)}", via=via, by_id=by_id)

        def still_open(p) -> bool:  # each step re-reads: a lock that got in meanwhile owns the cards from then on
            return (rs.events.get(p.key) or p).state == "open"

        async def discord_edits():
            for cid, mid in gone:  # the OLD lock's cards and line: theirs to delete whatever happens next
                await self._delete_message(cid, mid, key=first.key)
            label = run_label(reg, first)
            for a in fills:  # withdrawn for good: the card's final words stay true
                await self.edit_ask_card(reg, a.dm_channel_id, a.dm_message_id,
                                         f"Never mind the {'swap' if a.swap else 'seat'} question for {label} — an officer unlocked the event to change the roster.")
            for m, ask in rc.open_placement_rows(reg, first):  # the Confirm / Can't cards kept open: no locked roster, no deadline
                sg = first.signups.get(str(m.discord_id))
                if ask.get("dm") and sg is not None and still_open(first):
                    await self.edit_live_card(reg, ask["dm"][0], ask["dm"][1], confirm_open_layout(reg, first, self.ico, sg, m.discord_id))
            # the officers' open card again (rc.unlock counts the reminder as sent) — not when it was locked again meanwhile
            if still_open(first) and first.health_posted and "health" not in first.cards:
                ch = self.officer_channel(reg, first)
                if ch is not None:
                    await self.post_health(reg, rs, first, ch, nudge=False)
            await self.refresh_sheet(reg, first)  # drawn from the event as it is now, locked again or not
            for p in (x for x in parts if x.key in done.raids):
                if still_open(p):
                    await self.refresh_cards(reg, p)
            if not still_open(first):
                return  # locked again meanwhile: its own lines and cards stand, no "unlocked" line after them
            bits = [f"{done.asks_kept} confirmation ask(s) still open"] if done.asks_kept else []
            bits += [f"{len(fills)} fill ask(s) withdrawn"] if fills else []  # done.fills is empty: fills=False
            bits += [f"back to Join: {', '.join(done.returned)}"] if done.returned else []
            await self.post_run_update(reg, first, f"🔓 unlocked by {by}: back to open signup with the rosters on the board, Auto Lock off"
                                       + (" · " + " · ".join(bits) if bits else ""), whole=True)

        if answer_within is None:
            await discord_edits()
        else:
            task = self.finish_later(reg, first, "the unlock", discord_edits())
            finishing: dict = self.__dict__.setdefault("_unlock_finishing", {})
            finishing[(reg.key, first.key)] = task  # a lock of the event waits for it (lock_run, unlock_finishing)
            task.add_done_callback(lambda t, k=(reg.key, first.key): finishing.pop(k, None) if finishing.get(k) is t else None)
            await asyncio.wait({task}, timeout=answer_within)
        return f"Unlocked {run_title(reg, first)}: back to open signup with its rosters on the board. Auto Lock is off. Nobody was DMed."

    async def _delete_message(self, channel_id: int | None, message_id: int | None, *, key: str | None = None) -> bool:
        """Delete one message the bot posted (a lock card, the "is locked" line). An id missing, a channel or message
        gone, or Discord refusing: False, nothing raised."""
        if not channel_id or not message_id:
            return False
        try:
            ch = self.get_channel(int(channel_id))
            if ch is None and hasattr(self, "fetch_channel"):
                ch = await self.fetch_channel(int(channel_id))
            if getattr(ch, "archived", False) and hasattr(ch, "edit"):  # Discord refuses a delete in an archived thread
                await ch.edit(archived=False)
            await (await ch.fetch_message(int(message_id))).delete()
            return True
        except Exception as e:  # noqa: BLE001 — the unlock is written; a message left behind is cosmetic
            (log.warning if key else log.debug)("delete failed (%s, %s/%s): %s", key, channel_id, message_id, e)
            return False

    async def reschedule_event(self, reg, rs, ev, *, start: datetime | None = None, timing: dict | None = None, by: str,
                               by_id: int | None = None, via: str = "discord", preview: bool = False, rev: str | None = None) -> list[str]:
        """THE move of a run to another start and / or its own sheet timing (phase 3, Task 5) — any raid of it stands for
        its first. Officers, decided live (PRIV_LIVE; `by_id` None = code acting on the guild's own behalf). Refused →
        RegistryError with rc.reschedule_refusal's sentence, nothing written. `preview` → the confirm's sentences
        (consequences.reschedule_consequence), nothing written. Else, under the board lock of every raid of the run:
        rc.reschedule; then the sheet is edited in place (same message), every raid's officer cards, and every card
        still in play — an open Confirm / Can't ask and an open fill ask — is edited to the new time with its buttons
        (edit_live_card: the ask stands, nobody answers again); a moved start gives everyone who answered a `moved`
        notice (Me page, /me view). One line in the run's thread, one audit line. Nobody is DMed. → the done line.
        `rev` (rc.event_rev, what the preview showed): a run changed since is refused (rc.RUN_CHANGED) under the locks."""
        from .consequences import reschedule_consequence

        if by_id is not None and (await guild_routing.resolve_role(self, reg, int(by_id), max_age=guild_routing.PRIV_LIVE)).role not in ("owner", "officer"):
            raise RegistryError(rc.MOVE_OFFICER_ONLY)
        timing = rc.named_split(rs, ev, timing)  # a bare split is the raid the caller named (any raid of the run)
        first = rc.first_part(rs, ev)
        why = rc.reschedule_refusal(reg, rs, first, start, timing)
        if why:
            raise RegistryError(why)
        if preview:
            return reschedule_consequence(reg, rs, first, start, timing)
        parts = rc.event_parts(rs, first)
        async with contextlib.AsyncExitStack() as stack:
            for key in sorted(p.key for p in parts):  # one order everywhere: no lock cycle with a cancel or a lock
                await stack.enter_async_context(self.board_lock(reg, key))
            why = rc.reschedule_refusal(reg, rs, first, start, timing)  # again, now that nothing else writes the run
            if why:
                raise RegistryError(why)
            if rev and rev != rc.event_rev(reg, rs, first):  # changed since the confirm was shown: never overwritten unseen
                raise RegistryError(rc.RUN_CHANGED)
            old = first.start
            rc.stamp_before_change(reg, rs, parts)  # a run the start's stamping missed: stamped from its night, written below
            mv = rc.reschedule(reg, rs, first, start=start, timing=timing, by=by)
        name = reg.raid_def(first.instance).get("name", first.instance)
        when = reg.local12(first.start)
        moved = first.start != old
        edited = await self._after_move(reg, rs, first, parts, mv, moved)
        cards = "" if not edited else " · 1 open card shows the new time" if edited == 1 else f" · {edited} open cards show the new time"
        timing_only = set(mv.timing) <= {"cutoff_hard_hours", "cutoff_soft_hours", "confirm_hours", "reminders", "autofill", "fill_ask_hours",
                                         "auto_lock", "auto_lock_fill"} and not mv.splits
        own = "sheet timing" if timing_only else "settings"
        line = (f"🕒 {name} moved by {by} to {when}" if moved else f"🕒 {name}: its own {own} set by {by}") + cards
        await self.post_run_update(reg, first, line, whole=True)
        what = f"moved {name} from {reg.local12(old)} to {when}" if moved else f"set {name}'s own {own} for {when}"
        if moved and (mv.timing or mv.splits):
            what += f" and set its own {own}"
        await ops_mod.audited(self.ops, reg, by, what, via=via, by_id=by_id)
        return [f"{name} now starts {when}." if moved else f"{name} keeps its start. Its own {own} {'is' if timing_only else 'are'} saved."]

    async def _after_move(self, reg, rs, first, parts, mv, moved: bool, notice: str | None = None) -> int:
        """What a moved start (or a re-timed run) tells, shared by reschedule_event and start_now: the sheet edited in
        place (same message), every live raid's officer cards, every card still in play — an open Confirm / Can't ask
        and an open fill ask — edited to the new time with its buttons (edit_live_card: the ask stands, nobody answers
        again), and the `moved` notice (Me page, /me view) to everyone in `mv.told` — `notice` its words when given (Start
        now's: the event has started), else the new start or lock. Nobody is DMed. A moved start renames the event's
        threads to its new title (rename_threads). → cards edited."""
        if moved:
            await self.rename_threads(reg, rs, first)
        name = reg.raid_def(first.instance).get("name", first.instance)
        when = reg.local12(first.start)
        live = [p for p in parts if p.state in ("open", "locked")]
        await self.refresh_sheet(reg, first)
        edited = 0
        for p in live:
            await self.refresh_cards(reg, p)
            team = rc.run_team(reg, p)
            for m, ask in rc.open_placement_rows(reg, p):
                seat, sg = p.seat_of(m), p.signups.get(str(m.discord_id))
                if not ask.get("dm") or seat is None or sg is None:
                    continue
                gi = next((k for k, g in enumerate(p.all_rosters[seat[0]].groups) if seat[1].signup_name in g), 0)
                view = confirm_layout(reg, p, team, self.ico, sg, seat[0], gi, m.discord_id, reminder=bool(ask.get("reminded_at")))
                edited += await self.edit_live_card(reg, ask["dm"][0], ask["dm"][1], view)
            for a in p.fill_asks:
                if a.open and a.dm_message_id:
                    edited += await self.edit_live_card(reg, a.dm_channel_id, a.dm_message_id, fill_layout(reg, p, team, self.ico, a))
        locks = reg.local12(first.start - timedelta(hours=float(rc.team_setting(rc.run_team(reg, first), "cutoff_hard_hours"))))
        for uid in mv.told:  # an outcome, never a DM (the DM rule): the Me page and /me view say it — one member save each
            reg.notify(uid, "moved", notice or (f"{name} now starts {when}." if moved else f"{name} now locks {locks}."), run=first.key)
        return edited

    async def start_now(self, reg, rs, ev, by: str, by_id: int | None = None, *, via: str = "discord", seen: str | None = None) -> str:
        """THE Start now (issue 14) behind every surface: a locked event before its start starts this minute
        (rc.start_now — the first raid's start becomes now, later raids move by the same amount, every confirm-by stays,
        so no unanswered confirmation expires and no seat is freed by it). Under every raid's board lock (one order, as
        reschedule takes them); RegistryError with rc.start_now_refusal's sentence, nothing written. Then reschedule's
        tells (_after_move: the sheet and cards edited in place, open ask cards show the new time, the `moved` notice to
        everyone who answered). One thread line, one audit line. Nobody is DMed. → the done line."""
        first = rc.first_part(rs, ev)
        if (why := rc.start_now_refusal(reg, rs, first)) is not None:
            raise RegistryError(why)
        parts = rc.event_parts(rs, first)
        async with contextlib.AsyncExitStack() as stack:
            for key in sorted(p.key for p in parts):  # one order everywhere: no lock cycle with a cancel or a lock
                await stack.enter_async_context(self.board_lock(reg, key))
            if (why := rc.start_now_refusal(reg, rs, first)) is not None:  # again, now that nothing else writes the event
                raise RegistryError(why)
            if seen and _cq().start_now_box(reg, first, rs)["fp"] != seen:  # plain text's reviewed box changed since
                raise RegistryError(rc.RUN_CHANGED)
            old = first.start
            rc.stamp_before_change(reg, rs, parts)  # a run the start's stamping missed: stamped from its night, written below
            mv = rc.start_now(reg, rs, first, by)
        name = reg.raid_def(first.instance).get("name", first.instance)
        when = reg.local12(first.start)
        was = reg.local12(old)
        edited = await self._after_move(reg, rs, first, parts, mv, True, notice=rc.STARTED_NOTICE.format(raid=name, when=when, was=was))
        cards = "" if not edited else " · 1 open card shows the new time" if edited == 1 else f" · {edited} open cards show the new time"
        await self.post_run_update(reg, first, f"▶️ {name} has started: {by} started it at {when}, earlier than its planned {was}" + cards, whole=True)
        await ops_mod.audited(self.ops, reg, by, f"started {name} now: moved from {reg.local12(old)} to {when}", via=via, by_id=by_id)
        told = "Everyone who answered sees a notice. " if mv.told else ""
        return f"{name} started now, at {when}. {told}Nobody was DMed."

    async def edit_lineup(self, reg, rs, ev, lineup: list[dict], *, by: str, by_id: int | None = None, via: str = "discord",
                          preview: bool = False, rev: str | None = None) -> list[str]:
        """THE edit of a run's lineup before the lock (phase 3, Task 6): add, remove or reorder its later raids, or
        change a raid's roster count — any raid of it stands for its first. Officers, decided live (PRIV_LIVE; `by_id`
        None = code acting on the guild's own behalf). Refused → RegistryError with rc.lineup_refusal's sentence,
        nothing written. `preview` → the confirm's sentences (consequences.lineup_consequence). Else, under the board
        lock of every raid of the run: rc.plan_lineup + rc.apply_lineup (run keys never change; a removed raid's run is
        cancelled and dropped, its dict dropped). Then each removed raid's open fill asks close as `cancelled` under its
        plan_lock (every writer of ev.fill_asks holds it — a fan-out in flight sees the cancelled run before its next DM,
        as with cancel_run) and their cards are edited to say so; its officer cards go to their final state. When a kept
        raid's start changes, everyone who answered gets reschedule's `moved` notice (Me page, /me view). The sheet is
        edited in place (same message), one line in the run's thread, one audit line. Nobody is DMed. → the done line.
        `rev` (rc.event_rev, what the preview showed): a run changed since is refused (rc.RUN_CHANGED) under the locks."""
        from .consequences import lineup_consequence, lineup_words

        if by_id is not None and (await guild_routing.resolve_role(self, reg, int(by_id), max_age=guild_routing.PRIV_LIVE)).role not in ("owner", "officer"):
            raise RegistryError(rc.LINEUP_OFFICER_ONLY)
        first = rc.first_part(rs, ev)
        why = rc.lineup_refusal(reg, rs, first, lineup)
        if why:
            raise RegistryError(why)
        if preview:
            return lineup_consequence(reg, rs, first, lineup)
        parts = rc.event_parts(rs, first)
        async with contextlib.AsyncExitStack() as stack:
            for key in sorted(p.key for p in parts):  # one order everywhere: no lock cycle with a cancel or a lock
                await stack.enter_async_context(self.board_lock(reg, key))
            why = rc.lineup_refusal(reg, rs, first, lineup)  # again, now that nothing else writes the run
            if why:
                raise RegistryError(why)
            if rev and rev != rc.event_rev(reg, rs, first):  # changed since the confirm was shown: never overwritten unseen
                raise RegistryError(rc.RUN_CHANGED)
            rc.stamp_before_change(reg, rs, rc.event_parts(rs, first))  # a run the start's stamping missed, written below
            change = rc.plan_lineup(reg, rs, first, lineup)
            removed = [rs.events[k] for k in change.removed]
            rc.apply_lineup(reg, rs, first, change, by)
        name = reg.raid_def(first.instance).get("name", first.instance)
        for p in removed:
            raid = reg.raid_def(p.instance).get("name", p.instance)
            async with self.plan_lock(reg, p.key):  # taken AFTER the state flip: an ask a fan-out added meanwhile closes too
                fills = [a for a in p.fill_asks if a.pending]
                for a in fills:
                    a.answer, a.answered_at, a.held = "cancelled", rc.now(), False
                if fills:
                    rs.save(p, f"removed from the lineup: {len(fills)} ask(s) closed")
            for a in fills:
                await self.edit_ask_card(reg, a.dm_channel_id, a.dm_message_id, f"{raid} was taken out of the {name} event, so this ask is closed.")
            await self.refresh_cards(reg, p)
        moved = []
        for key, at in change.moved.items():
            p = rs.events[key]
            moved.append((reg.raid_def(p.instance).get("name", p.instance), rc.after_minutes(first.start, at)))
        for uid in change.told:  # an outcome, never a DM (the DM rule): reschedule's notice, one member save each
            reg.notify(uid, "moved", " ".join(f"{raid} now starts {reg.local12(t)}." for raid, t in moved), run=first.key)
        await self.refresh_sheet(reg, first)
        now_words = lineup_words(reg, rs, first, change)
        await self.post_run_update(reg, first, f"🗂 lineup changed by {by}: {now_words}"
                                   + (f" · {len(change.told)} who answered see a notice" if change.told else ""), whole=True)
        await ops_mod.audited(self.ops, reg, by, f"changed {run_title(reg, first)}'s lineup to {now_words}"
                              + (f", removing {', '.join(run_title(reg, p) for p in removed)}" if removed else ""), via=via, by_id=by_id)
        return [f"The event is now {now_words}."]

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
                await self.edit_ask_card(reg, cid, mid, f"Cancelled — {run_label(reg, ev)} was cancelled." if ev.state == "cancelled"
                                         else f"You already answered for {run_label(reg, ev)}.")
                continue
            sent += 1
            prev = ask.get("dm")
            if mid:
                ask["dm"] = [cid, mid]  # the newest card is the one an outcome edits
                if prev and list(prev) != [cid, mid]:  # the older card goes dead: one live Confirm per ask, never two
                    await self.edit_ask_card(reg, prev[0], prev[1], f"Reminded — see the newer message below for {run_label(reg, ev)}.")
            ask["reminded_at"] = rc.now()
            await asyncio.to_thread(reg.save, m, f"{m.display_name} reminded to confirm {ev.team}")
        ev.log.append(f"reminded the unconfirmed ({sent} by DM) — {by}" + (f"; stopped, the event is {stopped}" if stopped else ""))
        rs.save(ev, f"reminded {sent} by {by}")
        await self.post_run_update(reg, ev, f"🔔 {by} reminded the unconfirmed: {sent} by DM" + (f" · {site} wait on the site" if site else "")
                                   + (f" · stopped: the event is {stopped}" if stopped else ""))
        await self.refresh_cards(reg, ev)
        await ops_mod.audited(self.ops, reg, by, f"reminded {sent} unconfirmed on {run_title(reg, ev)}", via=via, by_id=by_id)
        if stopped:
            return f"Reminded {sent} by DM, then stopped: the event is {stopped}."
        return f"Reminded {sent} by DM" + (f"; {site} wait on the site (DMs off)" if site else "") + ". Next reminder possible in an hour."

    async def set_autofill_paused(self, reg, rs, ev, paused: bool, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Pause / resume the fill engine (D18) for EVERY raid of `ev`'s event still live (issue 14: one press resumes
        after a lock as it stands, which pauses them all): `autofill_paused` on each, gated inside run_fill — the
        scheduler, a freed seat, an answer and the site's blind send all respect it; an officer's own Ask / Send still
        goes. The card toggle, plain text run_pause, POST /api/run/{key}/pause and MCP pause_autofill all land here."""
        if ev.state in ("done", "cancelled"):
            raise RegistryError(f"{run_title(reg, ev)} is {ev.state}.")
        parts, flip = rc.pause_scope(rs, ev, paused)
        whom = f"every raid of {run_title(reg, rc.first_part(rs, ev))}" if len(parts) > 1 else run_title(reg, ev)
        if not flip:
            return f"Automated Fill Requests are already {'paused' if paused else 'on'} for {whom}."
        for p in flip:
            p.autofill_paused, p.autofill_paused_by = bool(paused), (by if paused else None)
            p.log.append(f"Automated Fill Requests {'paused' if paused else 'resumed'} by {by}")
            rs.save(p, f"autofill {'paused' if paused else 'resumed'} by {by}")
        await self.post_run_update(reg, ev, f"{'⏸️' if paused else '▶️'} Automated Fill Requests {'paused' if paused else 'resumed'} by {by}", whole=True)
        for p in flip:
            await self.refresh_cards(reg, p)
        await ops_mod.audited(self.ops, reg, by, f"{'paused' if paused else 'resumed'} Automated Fill Requests on {whom}", via=via, by_id=by_id)
        if not paused:
            for p in flip:
                if p.state == "locked" and rc.team_setting(rc.run_team(reg, p), "autofill"):
                    sent, _nd = await self.run_fill(reg, rs, p, rc.run_team(reg, p), by=f"resumed by {by}")
                    if sent:
                        await self.post_run_update(reg, p, "🧩 " + "\n🧩 ".join(ask_line(reg, self.ico, a) for a in sent))
        return (f"Automated Fill Requests are paused for {whom} — nothing goes out on its own; your Ask / Fill seats… still sends." if paused
                else f"Automated Fill Requests are on again for {whom}.")

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

    async def after_absence(self, reg, m, start: str, end: str, by: str, *, by_id: int | None = None) -> list[str]:
        """Mark every live sheet inside the absence: open → out; locked and rostered → seat freed + fill. A freed seat
        writes the member a "seat released (you're marked away)" notice only when someone else recorded the absence
        (an officer); their own absence (`by_id` = the member — decided by id, never a name) tells them nothing about
        what they just did. `by_id` None (who is unknown): the notice is written, as before."""
        rs = self.raids.store(reg)
        notice = by_id is None or int(by_id) != int(m.discord_id)
        touched = []
        for ev in rc.runs_in_span(reg, rs, start, end):  # the same runs describe(absence) and MCP's preview list
            if not rc.on_bench_side(reg, ev, m):
                continue  # puppets belong to test runs only (and a real member to real runs, the tester aside)
            team = rc.run_team(reg, ev)
            sg = ev.signups.get(str(m.discord_id))
            released = await self.release_asks_of(reg, rs, ev, m, "away that day")  # before drop_seated, so its fill sees the seat free
            if ev.state != "open" and ev.seat_of(m):
                await self.drop_seated(reg, rs, ev, team, m, "absence", None, notice=notice)
            else:
                if not sg or sg.status != "out":
                    c = rc.expected_character(reg, ev, m)  # a regular is marked away on their regular character
                    if c:
                        rc.set_signup(reg, rs, ev, m, c.label, "out", source="absence")
                        await self.refresh_sheet(reg, ev)
                if ev.followed_by:  # rostered on the follow-on only: the absence takes that seat
                    await self.after_lead_out(reg, rs, ev, m, "absence", None)
                await self.refresh_cards(reg, ev)  # the officers' No answer list, counts and Suggested block follow
                await self.refill_after_release(reg, rs, ev, released)
            touched.append(ev.key)
        return touched
