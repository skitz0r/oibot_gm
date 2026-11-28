"""The event's board post (design 2026-11-27-post-board): an officer's Post board on the event page posts every raid's
board to a Discord channel — one message per raid, in lineup order (raid_views.board_post_embeds) — and the bot keeps it
true as the event changes, until the event ends or is cancelled.

`BoardPostMixin` sits under RaidMixin (every client and test bot that has the raid verbs has it):

- `post_board` = THE press of the box (POST /api/event/{key}/board): the first post, an update in place, or — another
  channel picked while a post is up — the move, through RaidMixin.reschedule_event (the event's own Board posts channel,
  `board_channel`, is the fourth place of the event-channels machinery: channels_pending, move_channels → move_board,
  finish_channel_moves after a restart). A first post to a picked channel writes that channel as the event's own
  (rc.set_own_channel) once the post is up.
- `remove_board_post` = More › Remove board post: the messages deleted, the updates stop.
- `refresh_board_post` = the live update: every RaidStore.save of a raid of an event with a post (rc.RUN_LISTENERS,
  `install_board_posts`) schedules ONE debounced redraw (BOARD_DEBOUNCE_S) per event. It edits each raid's messages in
  place (a message whose content is unchanged is not touched), posts a message someone deleted by hand again, adds a
  message for a raid added to the lineup and deletes a removed raid's. Once every raid has ended or is cancelled it edits
  one last time (`board_done`) and stops. It never runs inside an interaction: the store write comes first, the Discord
  edits after the debounce.
- `move_board` (called by move_channels, under every raid's board lock): post new, verify, delete old.

Discipline: the event's own post lock (one writer of its messages at a time), and each raid's board lock only around a
render and around writing the ids — never across Discord calls — so a lock, an unlock, a move or a board drag never
waits on Discord here. A redraw that finds the post moved or removed while it was editing takes back what it posted.
Nobody is DMed, told or mentioned by any of it; the first post, a move and a removal write one line in the event's log
and the ops feed (a live update writes none)."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging

import discord

from . import ops as ops_mod, raidcycle as rc
from .raid_views import board_post_embeds, run_title
from .registry import RegistryError

log = logging.getLogger(__name__)

BOARD_DEBOUNCE_S = 3.0  # a burst of changes (a drag, Auto-fill groups, a lock's saves) redraws the post once

# the words the site shows (event_view.event_json `board_post`, consequences.board_post_box) and the press's answers
BOARD_POST = "Post board"
BOARD_UPDATE = "Update board post"
BOARD_REMOVE = "Remove board post"
BOARD_OVER = "This event is over, so its board can't be posted."
BOARD_NONE = "This event has no board post to remove."
BOARD_NOWHERE = "No channel is set for board posts or the signup sheet, so pick one from the list."
BOARD_POSTED = "The board is posted in {channel}, and it stays current as the event changes."
BOARD_CURRENT = "The board post in {channel} is up to date."
BOARD_REMOVED = "The board post is removed from {channel}, and it no longer updates."
BOARD_STAYS = "This event is over, so its board post stays as it ended."
BOARD_REPOSTED_ONE = "A message of the board post was gone, so it is posted again in {channel}."
BOARD_REPOSTED = "{n} messages of the board post were gone, so they are posted again in {channel}."
BOARD_FAILED = "The bot couldn't edit the board post in {channel}, so it tries again at the next change."
# the ops line when the post's own channel was deleted (or closed to the bot) and it went up again where Board posts go now
BOARD_GONE = "{title}: the board post's channel is gone, so it is posted again in {channel}."


def _closed(p) -> bool:
    return p.state in ("done", "cancelled")


def board_where(reg, first) -> int | None:
    """The channel id the box opens on: where the post is now, else where it would go (rc.channel_of). Discord's select
    opens on it and plain text's review names it."""
    return first.board_channel_id or rc.channel_of(reg, first, "board_channel")


def remove_refusal(rs, first) -> str | None:
    """Why Remove board post can't be done on `first`'s event (one sentence), else None: no post up (BOARD_NONE), or the
    event is over and its post stays as it ended (BOARD_STAYS). remove_board_post and plain text's review read it."""
    if not first.board_channel_id:
        return BOARD_NONE
    if all(_closed(p) for p in rc.event_parts(rs, first)):
        return BOARD_STAYS
    return None


def _fp(embed: discord.Embed) -> str:
    return hashlib.sha1(json.dumps(embed.to_dict(), sort_keys=True, default=str).encode()).hexdigest()[:16]


class BoardPostMixin:
    BOARD_DEBOUNCE_S = BOARD_DEBOUNCE_S

    # ---- where it goes
    def board_channel(self, reg, first, *, quiet: bool = False):
        """THE channel a board post of `first`'s event goes to when it is (re)posted: the event's own Board posts channel
        (rc.event_channel; gone or closed to the bot → one ops line and the next), else the guild's, else the event's
        signup sheet channel (RaidMixin.sheet_channel). None = nowhere."""
        mine = rc.event_channel(reg, first, "board_channel")
        if mine:
            if (ch := self._usable(mine)) is not None:
                return ch
            if not quiet:  # a redraw says it in its own line (BOARD_GONE)
                self._own_channel_gone(reg, first, "board_channel", mine)
        g = reg.config.board_channel_id
        if g and (ch := self._usable(g)) is not None:
            return ch
        return self.sheet_channel(reg, first)

    def board_where(self, reg, first) -> int | None:
        """The channel id the box opens on (module board_where)."""
        return board_where(reg, first)

    # ---- locks
    def _board_post_lock(self, reg, key: str) -> asyncio.Lock:
        return self.__dict__.setdefault("_board_post_locks", {}).setdefault((reg.key, key), asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _boards_held(self, reg, rs, first):
        """Every raid's board lock of `first`'s event, in the one order (no lock cycle with a lock, an unlock or a move)."""
        async with contextlib.AsyncExitStack() as stack:
            for key in sorted(p.key for p in rc.event_parts(rs, first)):
                await stack.enter_async_context(self.board_lock(reg, key))
            yield

    def _dropped_of(self, rs, first) -> list:
        """The raids taken out of `first`'s event's lineup that still hold a board message (deleted at the next redraw)."""
        eid = rc.event_id(first)
        return [e for e in rs.events.values() if e.dropped and e.board_posts and (e.event or e.follows) == eid]

    def _render(self, reg, rs, first) -> dict[str, list[discord.Embed]]:
        return {p.key: board_post_embeds(reg, p, self.ico) for p in rc.event_parts(rs, first)}

    def _sent(self) -> dict:
        """(guild, message id) → the fingerprint of what it shows now (an unchanged message is never edited)."""
        return self.__dict__.setdefault("_board_sent", {})

    async def _board_channel_obj(self, cid: int | None):
        ch = self.get_channel(int(cid)) if cid else None
        if ch is None and cid and hasattr(self, "fetch_channel"):
            with contextlib.suppress(Exception):
                ch = await self.fetch_channel(int(cid))
        return ch

    async def _board_edit(self, ch, mid: int, embed) -> str:
        """One board message edited through a partial message (no history read: the "board" kind needs only View, Send
        and Embed) → "ok", "gone" (ONLY discord.NotFound: deleted by hand, posted again by the caller) or "failed" (any
        other error — Forbidden, a 5xx, a timeout: the id is kept, nothing new is posted, the next change tries again)."""
        try:
            await ch.get_partial_message(int(mid)).edit(embed=embed)
            return "ok"
        except discord.NotFound:
            return "gone"
        except Exception as e:  # noqa: BLE001 — kept, tried again at the next change
            log.warning("board message %s not edited (%s): %s", mid, getattr(ch, "id", None), e)
            return "failed"

    async def _board_delete(self, cid: int | None, mid: int, key: str) -> None:
        """One board message deleted through a partial message (no history read); one already gone is fine."""
        ch = await self._board_channel_obj(cid)
        if ch is None:
            return
        try:
            await ch.get_partial_message(int(mid)).delete()
        except discord.NotFound:
            pass
        except Exception as e:  # noqa: BLE001 — a message left behind is logged, never raised
            log.warning("board message delete failed (%s, %s/%s): %s", key, cid, mid, e)

    # ---- the press
    async def post_board(self, reg, rs, ev, *, channel: int | None = None, by: str, by_id: int | None = None, via: str = "web",
                         seen: str | None = None, fp=None) -> str:
        """The box's Post (officers; the route checks who), under the event's post lock: `seen` = the fp of the box
        pressed, checked against `fp(first)` (the box as it is now) once the lock is held — a press another officer's
        got ahead of raises rc.BoardChanged and writes nothing. No post yet → every raid's board posted in `channel` (the
        box's pick; None = where the event's Board posts resolve), and a picked channel that is not that one becomes the
        event's own once the post is up; a post up in that channel → every message checked now (a missing one posted
        again); a post up elsewhere → moved there (RaidMixin.reschedule_event with the event's own board_channel). Refused
        (RegistryError, one sentence) once every raid has ended or is cancelled. → the answer, true to what happened."""
        from .discord_pool import channel_name

        first = rc.first_part(rs, ev)
        async with self._board_post_lock(reg, first.key):
            first = rs.events.get(first.key) or first
            if fp is not None and seen is not None and fp(first) != seen:
                raise rc.BoardChanged()
            if all(_closed(p) for p in rc.event_parts(rs, first)):
                raise RegistryError(BOARD_OVER)
            if first.board_channel_id:
                if channel and int(channel) != int(first.board_channel_id):
                    said = await self.reschedule_event(reg, rs, first, timing={"board_channel": str(int(channel))}, by=by, by_id=by_id, via=via)
                    return " ".join(said)
                res = await self._refresh_locked(reg, rs, first.key, verify=True)
                where = channel_name(self, rs.events[first.key].board_channel_id or first.board_channel_id)
                if res["failed"]:
                    return BOARD_FAILED.format(channel=where)
                if res["reposted"] == 1:
                    return BOARD_REPOSTED_ONE.format(channel=where)
                if res["reposted"]:
                    return BOARD_REPOSTED.format(n=res["reposted"], channel=where)
                return BOARD_CURRENT.format(channel=where)
            resolved = rc.channel_of(reg, first, "board_channel")
            own = None
            if channel and int(channel) != (resolved or 0):
                if why := await self.channel_setting_refusal(reg, {"board_channel": str(int(channel))}):
                    raise RegistryError(why)
                ch = self.get_channel(int(channel))
                own = str(int(channel))
            else:
                ch = self.board_channel(reg, first)
            if ch is None:
                raise RegistryError(BOARD_NOWHERE)
            async with self._boards_held(reg, rs, first):
                first = rs.events.get(first.key) or first
                parts = rc.event_parts(rs, first)
                drawn = self._render(reg, rs, first)
            posted: dict[str, list] = {}

            async def take_back():
                for m in [m for ms in posted.values() for m in ms]:
                    with contextlib.suppress(Exception):
                        await m.delete()
            try:
                for p in parts:
                    posted[p.key] = [await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none()) for e in drawn[p.key]]
            except Exception as e:  # noqa: BLE001 — nothing is kept: every message just posted comes down again
                log.warning("board post failed (%s → %s): %s", first.key, getattr(ch, "id", None), e)
                await take_back()
                raise RegistryError(self._send_refusal(ch)) from e
            where = f"#{getattr(ch, 'name', ch.id)}"
            try:
                async with self._boards_held(reg, rs, first):
                    if own:  # the event remembers where its post is: its own Board posts channel (M6: under config_lock)
                        await asyncio.to_thread(rc.set_own_channel, reg, rs, first, "board_channel", own)
                    for p in sorted(parts, key=lambda x: x is first):  # the first raid last: its save carries the log line
                        p = rs.events.get(p.key) or p
                        p.board_posts = [m.id for m in posted[p.key]]
                        if p.key == first.key:
                            p.board_channel_id, p.board_done = ch.id, False
                            p.log.append(f"board posted in {where} by {by}" + (f", who made {where} this event's Board posts channel" if own else ""))
                        await asyncio.to_thread(rs.save, p, f"board posted in {where}")
            except Exception:
                await take_back()  # nothing remembers these messages: they come down rather than stay orphaned
                raise
            for p in parts:
                for m, e in zip(posted[p.key], drawn[p.key]):
                    self._sent()[(reg.key, m.id)] = _fp(e)
        await ops_mod.audited(self.ops, reg, by, f"posted the board of {run_title(reg, first)} in {where}"
                              + (f" and made {where} its Board posts channel" if own else ""), via=via, by_id=by_id)
        return BOARD_POSTED.format(channel=where)

    async def remove_board_post(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "web") -> str:
        """More › Remove board post on a live event: every raid's messages deleted (a later raid's and a removed raid's
        too) and the updates stop; nobody is told. Refused (RegistryError) when there is none, and once the event is over
        (nothing could post it again: its last state stays). → the answer."""
        first = rc.first_part(rs, ev)
        if why := remove_refusal(rs, first):
            raise RegistryError(why)
        from .discord_pool import channel_name

        async with self._board_post_lock(reg, first.key):
            async with self._boards_held(reg, rs, first):
                first = rs.events.get(first.key) or first
                cid = first.board_channel_id
                if not cid:
                    raise RegistryError(BOARD_NONE)
                where = channel_name(self, cid)
                gone = []
                for p in sorted([*rc.event_parts(rs, first), *self._dropped_of(rs, first)], key=lambda x: x is first):
                    gone += p.board_posts
                    p.board_posts = []
                    if p.key == first.key:
                        p.board_channel_id, p.board_done = None, False
                        p.log.append(f"board post removed from {where} by {by}")
                    await asyncio.to_thread(rs.save, p, "board post removed")
            for mid in gone:
                await self._board_delete(cid, mid, first.key)
                self._sent().pop((reg.key, mid), None)
        await ops_mod.audited(self.ops, reg, by, f"removed the board post of {run_title(reg, first)} from {where}", via=via, by_id=by_id)
        return BOARD_REMOVED.format(channel=where)

    # ---- the move (move_channels, under every raid's board lock)
    async def move_board(self, reg, rs, first) -> str | None:
        """The event's board post into where its Board posts resolve now (board_channel) — POST NEW, VERIFY, THEN DELETE
        OLD: every raid's messages are posted there first; one send failing takes the new ones down again and leaves the
        old post and its ids as they were (→ the sentence why; move_channels puts the setting back). Then the ids and the
        channel are written and the old messages deleted. No post up, or already there: nothing to do. The caller
        (move_channels) holds every raid's board lock, so this never takes them — nor the post lock (a redraw holding it
        waits for these board locks): a redraw still editing the old messages finds the channel changed when it writes,
        and takes back what it posted. Also called when the signup sheet moves and the post inherits its channel."""
        if not first.board_channel_id:
            return None
        ch = self.board_channel(reg, first)
        if ch is None or int(first.board_channel_id) == ch.id:
            return None
        parts = rc.event_parts(rs, first)
        drawn = self._render(reg, rs, first)
        posted: dict[str, list] = {}
        try:
            for p in parts:
                posted[p.key] = [await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none()) for e in drawn[p.key]]
        except Exception as e:  # noqa: BLE001 — every new message comes down again; the old post stands
            log.warning("board move failed (%s → %s): %s", first.key, ch.id, e)
            for m in [m for ms in posted.values() for m in ms]:
                with contextlib.suppress(Exception):
                    await m.delete()
            return self._send_refusal(ch)
        old_cid = first.board_channel_id
        old = [mid for p in [*parts, *self._dropped_of(rs, first)] for mid in p.board_posts]
        for p in parts:
            for m, e in zip(posted[p.key], drawn[p.key]):
                self._sent()[(reg.key, m.id)] = _fp(e)
        for p in sorted([*parts, *self._dropped_of(rs, first)], key=lambda x: x is first):
            p.board_posts = [m.id for m in posted.get(p.key, [])]
            if p.key == first.key:
                p.board_channel_id = ch.id
            await asyncio.to_thread(rs.save, p, f"board post moved to #{getattr(ch, 'name', ch.id)}")
        for mid in old:
            await self._board_delete(old_cid, mid, first.key)
            self._sent().pop((reg.key, mid), None)
        return None

    # ---- live
    async def refresh_board_post(self, reg, rs, key: str) -> dict:
        """Redraw the board post of the event `key` belongs to (any raid's key), under the event's post lock
        (_refresh_locked). → {edited, reposted, failed}."""
        ev = rs.events.get(key)
        if ev is None:
            return {"edited": 0, "reposted": 0, "failed": 0}
        if ev.dropped:  # a raid taken out of the lineup: its event's redraw deletes its message
            ev = rs.events.get(rc.event_id(ev)) or ev
        first = rc.first_part(rs, ev)
        async with self._board_post_lock(reg, first.key):
            return await self._refresh_locked(reg, rs, first.key, verify=False)

    async def _refresh_locked(self, reg, rs, key: str, *, verify: bool) -> dict:
        """The redraw (the caller holds the post lock): each raid's messages edited in place when what they show changed
        — every one when `verify` (Update board post: each is checked, whatever it last showed) —, a message Discord says
        is gone (NotFound) posted again, one Discord refused or failed kept as it is (tried again at the next change), a
        raid added to the lineup given its message, a removed raid's deleted, extra messages trimmed. The post's channel
        gone: posted again where Board posts resolve now, with ONE ops line — never once the event is over. Every raid
        ended or cancelled: this last edit marks the post done and later redraws do nothing (a reopened raid starts it
        again). Board locks only around the render and the id write; every Discord call outside them."""
        out = {"edited": 0, "reposted": 0, "failed": 0}
        first = rs.events.get(key)
        if first is None:
            return out
        async with self._boards_held(reg, rs, first):
            first = rs.events.get(first.key) or first
            cid = first.board_channel_id
            parts = rc.event_parts(rs, first)
            closed = all(_closed(p) for p in parts)
            if not cid or (closed and first.board_done):
                return out
            drawn = self._render(reg, rs, first)
            ids = {p.key: list(p.board_posts) for p in parts}
            dropped = {p.key: list(p.board_posts) for p in self._dropped_of(rs, first)}
        ch = await self._board_channel_obj(cid)
        moved_to = None
        if ch is None:
            if closed:  # an ended event's post is never posted again elsewhere: it is done where it was
                async with self._boards_held(reg, rs, first):
                    first = rs.events.get(first.key) or first
                    if first.board_channel_id == cid and not first.board_done:
                        first.board_done = True
                        await asyncio.to_thread(rs.save, first, "board post done (its channel is gone)")
                return out
            ch = self.board_channel(reg, first, quiet=True)
            if ch is None:
                log.warning("board post of %s/%s: its channel is gone and there is nowhere to post it", reg.key, first.key)
                return out
            moved_to, ids = ch, {k: [] for k in ids}
        sent = self._sent()
        new_ids: dict[str, list[int]] = {}
        fresh: list = []  # messages posted by this redraw (taken back if the post moved meanwhile)
        trim: list[int] = []
        for p in parts:
            got = []
            for i, e in enumerate(drawn[p.key]):
                mid = ids[p.key][i] if i < len(ids[p.key]) else None
                fp = _fp(e)
                if mid and not verify and sent.get((reg.key, mid)) == fp:
                    got.append(mid)
                    continue
                if mid:
                    r = await self._board_edit(ch, mid, e)
                    if r == "ok":
                        sent[(reg.key, mid)] = fp
                        got.append(mid)
                        out["edited"] += 1
                        continue
                    if r == "failed":
                        sent.pop((reg.key, mid), None)
                        got.append(mid)
                        out["failed"] += 1
                        continue
                    sent.pop((reg.key, mid), None)  # gone: posted again
                try:
                    m = await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
                except Exception as ex:  # noqa: BLE001 — the next change tries again
                    log.warning("board post of %s not posted again (%s): %s", p.key, getattr(ch, "id", None), ex)
                    out["failed"] += 1
                    if mid:
                        got.append(mid)
                    continue
                fresh.append(m)
                sent[(reg.key, m.id)] = fp
                got.append(m.id)
                out["reposted" if mid else "edited"] += 1
            trim += ids[p.key][len(drawn[p.key]):]
            new_ids[p.key] = got
        takeback = False
        async with self._boards_held(reg, rs, first):
            first = rs.events.get(first.key) or first
            if first.board_channel_id != cid:  # moved or removed while this redraw ran: take back what it posted
                takeback = True
            else:
                saves = []
                for p in rc.event_parts(rs, first):
                    if p.key in new_ids and p.board_posts != new_ids[p.key]:
                        p.board_posts = new_ids[p.key]
                        saves.append(p)
                for k in dropped:
                    if (d := rs.events.get(k)) is not None:
                        d.board_posts = []
                        saves.append(d)
                done = all(_closed(p) for p in rc.event_parts(rs, first)) and not out["failed"]
                if first.board_channel_id != ch.id or first.board_done != done:
                    first.board_channel_id, first.board_done = ch.id, done
                    if first not in saves:
                        saves.append(first)
                for p in sorted(saves, key=lambda x: x is first):
                    await asyncio.to_thread(rs.save, p, "board post kept current")
        if takeback:  # no board lock held for these Discord calls (M5)
            for m in fresh:
                with contextlib.suppress(Exception):
                    await m.delete()
                sent.pop((reg.key, m.id), None)
            return {"edited": 0, "reposted": 0, "failed": 0}
        if moved_to is None:
            for mid in trim + [m for ms in dropped.values() for m in ms]:
                await self._board_delete(cid, mid, first.key)
                sent.pop((reg.key, mid), None)
        else:
            await self.ops.emit(reg.config, "warn", BOARD_GONE.format(title=run_title(reg, first), channel=f"#{getattr(moved_to, 'name', moved_to.id)}"))
        return out

    def install_board_posts(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Listen to every run save (rc.RUN_LISTENERS) on `loop` (the client's, or the running one): a raid of an event
        with a board post schedules its event's redraw. One listener per process, the newest client wins."""
        self.__dict__["_board_loop"] = loop or asyncio.get_running_loop()

        def board_run_saved(gk: str, run_key: str, bot=self) -> None:
            bot.board_saved(gk, run_key)

        rc.RUN_LISTENERS[:] = [f for f in rc.RUN_LISTENERS if getattr(f, "__name__", "") != board_run_saved.__name__] + [board_run_saved]

    def board_saved(self, gk: str, run_key: str) -> None:
        """A run was saved (any thread): its event's redraw, debounced, on the client's loop."""
        loop = self.__dict__.get("_board_loop")
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._board_schedule, gk, run_key)
        except RuntimeError:  # the loop is shutting down
            pass

    def _board_schedule(self, gk: str, run_key: str) -> None:
        regs = getattr(self, "registries", None)
        reg = regs.by_key.get(gk) if regs is not None and hasattr(regs, "by_key") else None
        if reg is None:
            return
        rs = self.raids.store(reg)
        ev = rs.events.get(run_key)
        if ev is None:
            return
        first = rs.events.get(rc.event_id(ev)) if ev.dropped else rc.first_part(rs, ev)
        if first is None or not first.board_channel_id or (first.board_done and all(_closed(p) for p in rc.event_parts(rs, first))):
            return
        loop = self.__dict__["_board_loop"]
        timers: dict = self.__dict__.setdefault("_board_timers", {})
        k = (gk, first.key)
        if (t := timers.pop(k, None)) is not None:
            t.cancel()

        def fire():
            timers.pop(k, None)
            tasks: set = self.__dict__.setdefault("_board_tasks", set())
            kept: set = self.__dict__.setdefault("_finishing", set())  # finish_pending waits for it at shutdown
            task = loop.create_task(self._board_redraw(reg, rs, first.key), name=f"the board post of {gk}/{first.key}")
            for s in (tasks, kept):
                s.add(task)
                task.add_done_callback(s.discard)

        timers[k] = loop.call_later(self.BOARD_DEBOUNCE_S, fire)

    async def _board_redraw(self, reg, rs, key: str) -> None:
        try:
            await self.refresh_board_post(reg, rs, key)
        except Exception:  # noqa: BLE001 — the next change tries again
            log.warning("board post of %s/%s not redrawn", reg.key, key, exc_info=True)

    async def board_settle(self, rounds: int = 20) -> None:
        """Wait for every scheduled redraw and the ones they schedule (tests, and a shutdown that wants them out)."""
        for _ in range(rounds):
            tasks = [t for t in self.__dict__.get("_board_tasks", set()) if not t.done()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            elif self.__dict__.get("_board_timers"):
                await asyncio.sleep(self.BOARD_DEBOUNCE_S + 0.01)
            else:
                await asyncio.sleep(0)
                if not self.__dict__.get("_board_timers") and not any(not t.done() for t in self.__dict__.get("_board_tasks", set())):
                    return

    async def boards_restore(self, reg) -> int:
        """The first ready: every event with a board post not yet done redrawn as this version draws it (an event that
        ended while the bot was down gets its last edit). → events touched."""
        rs = self.raids.store(reg)
        n = 0
        for ev in list(rs.events.values()):
            if ev.board_channel_id and not ev.board_done and not rc.is_follow(ev):
                try:
                    await self.refresh_board_post(reg, rs, ev.key)
                    n += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("board post restore failed (%s %s): %s", reg.key, ev.key, e)
        return n
