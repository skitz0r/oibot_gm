"""LootMixin: the loot council's verbs on the client (design §3.1, dual interfaces phase 1).

One verb set, every surface a caller (§2.2): the thread's buttons and wizards, the web's /api/loot routes, plain text's
`loot` op, MCP and the companion feed all call

    open_loot · tick · distribute · override · confirm · discard · set_reason · correct · end_run
    (+ remove_candidate · set_scores_only · keep_open)

each `(reg, session, …, by, by_id, via)`. Verbs never check permissions (callers do, with guild_routing.role_of). Every
verb runs under the per-(guild, session) asyncio.Lock; distribute judges a SNAPSHOT in a worker thread and merges on
the loop (single flight per session). After a write: the session is saved (LOOT_LISTENERS → the web's `loot:<run>`
topic), one ops line through ops.audited, and `loot_notify` (the Discord surfaces' hook: the thread line / card edit).
A test session (a test-bench run) writes nothing to the ledger, precedents or learned items.

Game truth first (decision 3): an override lives on the proposal until Confirm; the precedent is written AT Confirm,
in one GitStore.batch with the session and the ledger rows (only rows whose import_id is absent); Discard drops pending
overrides; a companion mismatch records the ledger row at once and leaves the precedent pending a reason bound to
(drop_id, recipient) — `set_reason` (idempotent) or `correct` (void + new award, no precedent)."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import discord

from . import raidcycle as rc
from .loot import session as ls
from .loot.session import DISENCHANT, Drop, LootBusy, LootChanged, LootSession, Override, now_iso
from .registry import RegistryError, parse_bool

log = logging.getLogger(__name__)

AUTO_DISTRIBUTE_S = 20.0  # auto-distribute: this long after the last drop of a boss (per raid: loot_auto_distribute)
IDLE_END_MIN = 30  # the scheduler ends a council only past the run's close, idle this long, nothing pending
CLOSING_NOTICE_MIN = 15  # … after one "closing in 15 min — Keep open" notice
FEED_BUFFER_S = 600  # companion events with no live run are held this long (replayed when a council opens)
FEED_LEAD_S = 2 * 3600  # a held event replays into a council only if it arrived after its run's start minus this …
FEED_AFTER_END_S = 3600  # … and an event held this soon after a council ended belongs to that ended run, never the next
_REPLAYING: contextvars.ContextVar[bool] = contextvars.ContextVar("loot_replaying", default=False)  # feed_replay's own task
NAME_MAX = 64
NOBODY = "(nobody)"  # recommend._fallback's pick when no candidate is eligible: never recorded


def _run_of(session_id: str) -> str:
    """The run key of a session id: a split run's roster council is '<run>-r<N>', a whole run's council is the run key."""
    return re.sub(r"-r\d+$", "", session_id)


def precedent_any(ctx, import_id: str) -> dict | None:
    """The latest precedent row of an award, whatever its status (active, superseded, retired) — "was a reason ever
    given?" (the needs-a-reason list, set_reason's replace step). What the model cites stays `ctx.precedent_for`
    (active only)."""
    return next((p for p in reversed(getattr(ctx, "precedents", None) or []) if p.get("import_id") == import_id), None)


def _utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class LootMixin:
    """Mixed into OibotGM. Expects: events (dict keyed (guild key, session id)), registries, raids, ops, contexts, ctx
    (its `provider` when loot_provider is unset; may be None)."""

    AUTO_DISTRIBUTE_S = AUTO_DISTRIBUTE_S

    # ------------------------------------------------------------ index, locks, contexts
    def _loot_state(self) -> dict:
        return self.__dict__.setdefault("_loot", {"locks": {}, "flight": {}, "auto": {}, "tasks": set(), "threads": {}, "buffer": {}})

    def loot_lock(self, guild_key: str, session_id: str) -> asyncio.Lock:
        """The per-(guild, session) lock every loot verb holds for its check-and-write."""
        return self._loot_state()["locks"].setdefault((guild_key, session_id), asyncio.Lock())

    def loot_index(self, session: LootSession) -> None:
        self.events[session.key] = session
        if session.raid_thread_id:
            self._loot_state()["threads"][session.raid_thread_id] = session.key

    def loot_forget(self, session: LootSession) -> None:
        self.events.pop(session.key, None)
        threads = self._loot_state()["threads"]
        for tid in [t for t, k in threads.items() if k == session.key]:
            threads.pop(tid, None)

    def event_for(self, channel_id: int | None) -> LootSession | None:
        """The session a Discord channel belongs to: its loot thread."""
        if channel_id is None:
            return None
        key = self._loot_state()["threads"].get(channel_id)
        if key and key in self.events:
            return self.events[key]
        return next((s for s in self.events.values() if s.raid_thread_id == channel_id), None)

    def loot_reg(self, session: LootSession):
        by_key = getattr(self.registries, "by_key", {}) or {}
        return by_key.get(session.guild)

    def loot_ctx(self, session: LootSession):
        """The context a session reads and writes: the registered guild's (one per guild, cached). There is no other
        kind: /mock and its fixtures context are retired (D11) — a rehearsal is a test session of a real guild."""
        contexts = self.__dict__.setdefault("contexts", {})
        reg = self.loot_reg(session)
        if reg is None:
            raise RuntimeError(f"no registry for guild {session.guild}")
        if reg.key not in contexts or getattr(contexts[reg.key], "reg", None) is not reg:
            from .lootctx import RegistryLootContext

            provider = getattr(getattr(self, "ctx", None), "provider", None) if getattr(self, "loot_provider", None) is None else self.loot_provider
            contexts[reg.key] = RegistryLootContext(reg, reg.store, provider)
        return contexts[reg.key]

    def loot_run(self, session: LootSession):
        """(RaidStore, RaidEvent) of a real session's run, or (None, None)."""
        reg = self.loot_reg(session)
        if reg is None or not session.run_key:
            return None, None
        rs = self.raids.store(reg)
        return rs, rs.events.get(session.run_key)

    def loot_live(self, session: LootSession) -> bool:
        """A session is live iff its run is locked (no separate session state)."""
        if session.origin != "raid":
            return False  # a retired /mock session (D11) is never live
        _, ev = self.loot_run(session)
        return ev is not None and ev.state == "locked"

    def loot_roster(self, session: LootSession) -> list:
        """The candidates' Players, read LIVE from the run file (a fill yes, an alt swap or a board move after open is a
        candidate at once), plus members who left the roster after open (`departed`, until an officer removes them)."""
        if session.origin != "raid":
            return []
        _, ev = self.loot_run(session)
        if ev is None:
            return list(session.departed)
        rosters = ev.all_rosters if session.roster_index is None else ev.all_rosters[session.roster_index:session.roster_index + 1]
        seated = [p for r in rosters for p in r.selected]
        uids = {rc.player_uid(ev, p) for p in seated}
        names = {(p.character or p.signup_name).lower() for p in seated}
        return seated + [p for p in session.departed if (p.discord_id is None or p.discord_id not in uids) and (p.character or p.signup_name).lower() not in names]

    def loot_session(self, reg, run_key: str, roster_index: int | None = None) -> LootSession | None:
        sid = run_key if roster_index is None else f"{run_key}-r{roster_index + 1}"
        return self.events.get((reg.key, sid))

    def loot_sessions(self, reg=None, live: bool = True) -> list[LootSession]:
        out = [s for s in self.events.values() if (reg is None or s.guild == reg.key) and (not live or self.loot_live(s))]
        return sorted(out, key=lambda s: (s.guild, s.opened_at))

    def loot_clock(self, reg, iso: str | None = None) -> str:
        """'8:12 PM' in guild time (now, or an ISO stamp)."""
        t = _utc(iso) or datetime.now(timezone.utc)
        if reg is not None:
            return reg.local12(t, "%I:%M %p")
        from .constants import fmt12

        return fmt12(t, "%I:%M %p")

    def loot_settle(self):
        """The background loot tasks (auto-distribute, re-judge after an override) — tests await them."""
        return asyncio.gather(*list(self._loot_state()["tasks"]), return_exceptions=True)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro)
        tasks = self._loot_state()["tasks"]
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        return t

    # ------------------------------------------------------------ hooks the Discord surfaces (L2) replace
    async def loot_notify(self, reg, session: LootSession, line: str | None = None, *, kind: str = "", drop_id: str | None = None) -> None:
        """After every write: tell the session's surfaces (the Discord surfaces replace this). Default: the line into
        the loot thread with the buttons it needs (loot_view: Give the reason / Mistake — correct… on a companion
        mismatch, Correct… on a conflict or an in-game award, Distribute on ticked drops); a distribute posts the
        proposals with their Confirm / Discard buttons. `drop_id`: the drop the line is about."""
        if kind == "distribute" and session.origin == "raid" and session.proposals and hasattr(self, "post_proposals"):
            get = getattr(self, "get_channel", None)
            ch = get(session.raid_thread_id) if get and session.raid_thread_id else None
            if ch is not None:
                try:
                    await self.post_proposals(session, ch, prefix=line)
                    return
                except Exception as e:  # noqa: BLE001 — the write already happened; fall back to the plain line
                    log.warning("loot proposals post failed: %s", e)
        if line:
            await self.loot_post(session, line, view=self.loot_view(reg, session, kind, drop_id))

    async def loot_post(self, session: LootSession, text: str, view=None) -> None:
        get = getattr(self, "get_channel", None)
        ch = get(session.raid_thread_id) if get and session.raid_thread_id else None
        if ch is None:
            return
        try:
            await ch.send(text[:1900], view=view) if view is not None else await ch.send(text[:1900])
        except Exception as e:  # noqa: BLE001 — the write already happened; the line is a courtesy
            log.warning("loot thread post failed: %s", e)

    async def loot_open_thread(self, reg, rs, ev, session: LootSession) -> int | None:
        """Where the council lives (decision 5 = the Discord surfaces' to refine): a public thread on the officer
        channel (roster, else ops). No channel configured → refused; configured but unreachable (Discord down) → the
        session opens with no thread (thread pending)."""
        cfg = reg.config
        if not (cfg.roster_channel_id or cfg.ops_channel_id):
            raise RegistryError("Set a roster or ops channel first — the loot council's thread goes there (/gm config roster-channel).")
        ch = self.officer_channel(reg, ev) if hasattr(self, "officer_channel") else None
        if ch is None:
            return None
        from .constants import TEAL
        from .raid_views import _group_block, run_title

        ico = getattr(self, "ico", lambda kind, key: "")
        idx = session.roster_index
        rosters = ev.all_rosters if idx is None else ev.all_rosters[idx:idx + 1]
        label = f"Roster {idx + 1} · " if idx is not None else ""
        groups = [_group_block(reg, ico, r, gi, show_missing=False) for r in rosters for gi in range(len(r.groups or [])) if r.groups[gi]]
        e = discord.Embed(title=f"⚔ {label}{run_title(reg, ev)}", colour=TEAL,
                          description=(f"<t:{int(ev.start.timestamp())}:F> · {sum(len(r.selected) for r in rosters)} rostered\n\n" + "\n\n".join(groups))[:3800])
        e.set_footer(text="loot council: tick drops (or let the companion do it) → Distribute → Confirm · /raid end for the summary")
        msg = await ch.send(embed=e)
        thread = await msg.create_thread(name=(("rehearsal · " if session.test else "loot · ") + label + run_title(reg, ev))[:100])
        return thread.id

    async def loot_ended(self, reg, session: LootSession, text: str) -> None:
        await self.loot_post(session, text)

    async def loot_closing(self, reg, session: LootSession) -> None:
        """The scheduler's one notice before an auto-end, with a Keep open button."""
        owed = len(session.pending_reasons) + len(session.conflicts)
        await self.loot_post(session, f"⏳ This council closes in {CLOSING_NOTICE_MIN} min — the run is over and no pick is waiting."
                             + (f" {owed} award(s) still need a reason or a correction; they stay fixable after the end." if owed else ""),
                             view=self.loot_view(reg, session, "closing"))

    async def loot_held(self, reg, session: LootSession) -> None:
        """The scheduler's one line when a council past its run's close is idle but still holds picks / drops nobody
        decided: it is never auto-ended with a pick pending, so an officer is told once what waits."""
        c = session.pending_count()
        parts = ([f"{c['proposals']} pick(s) waiting for Confirm"] if c["proposals"] else []) + ([f"{c['undistributed']} drop(s) nobody judged"] if c["undistributed"] else [])
        await self.loot_post(session, "⏳ The raid is over, but this council still has " + (" and ".join(parts) or "a judge running")
                             + " — Confirm or Discard them, or end the council with /raid end (the summary keeps what was pending).")

    async def _audit(self, reg, by: str, line: str, via: str, by_id: int | None, level: str = "info") -> None:
        if reg is None or getattr(self, "ops", None) is None:
            log.info("[loot] %s: %s", by, line)
            return
        try:
            await self.ops.audited(reg, by, line, via=via, level=level, by_id=by_id)
        except AttributeError:  # a test double with emit only
            from .ops import audited

            await audited(self.ops, reg, by, line, via=via, level=level, by_id=by_id)

    def _changed(self, session: LootSession, fallback: str) -> LootChanged:
        last = session.last_change()
        return LootChanged(f"{fallback} ({last}) — look again." if last else f"{fallback} — look again.")

    def loot_check_live(self, session: LootSession) -> None:
        """Every verb, right after taking the session lock: a council that ended meanwhile (end_run archived and forgot
        it) is never written again — a stale reference would re-create its live file and bring it back on a restart."""
        if self.events.get(session.key) is not session:
            raise RegistryError("This loot council has ended — nothing was changed. Its awards can still be corrected from the site.")

    def _rescoring(self) -> dict:
        """(guild, session id) → drops a re-judge after an override is about to re-score (set before the task starts)."""
        return self._loot_state().setdefault("rescoring", {})

    # ------------------------------------------------------------ open
    async def open_loot(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "discord",
                        roster: int | None = None, replay: bool = True) -> tuple[LootSession, bool]:
        """Open the council for a locked run (idempotent: an open session is returned, created=False — two officers at
        once get the same one — and a thread still pending is tried again). `roster`: one session per roster of a split
        run (None = the whole run; `open_council` picks). Refuses a run that isn't locked, or an instance the profile
        doesn't define. Buffered companion events are replayed (`replay`)."""
        if ev.state != "locked" or not ev.all_rosters:
            raise RegistryError("Lock the roster first (/raid lock).")
        if ev.instance not in reg.profile.raids:
            raise RegistryError(f"No raid definition for {ev.instance} in profile {reg.profile.name}.")
        if roster is not None and not 0 <= roster < len(ev.all_rosters):
            raise RegistryError(f"This run has {len(ev.all_rosters)} roster(s).")
        sid = ev.key if roster is None else f"{ev.key}-r{roster + 1}"
        have = None
        async with self.loot_lock(reg.key, sid):
            have = self.events.get((reg.key, sid))
            if have is None:
                s = LootSession(id=sid, run_key=ev.key, roster_index=roster, instance=ev.instance, date=ev.start.astimezone(reg.tz).date().isoformat(),
                                guild=reg.key, origin="raid", state="raid", test=rc.is_test_run(reg, ev), opened_by=by,
                                label=f"Roster {roster + 1}" if roster is not None else None).attach(reg.store)
                thread_id = await self.loot_open_thread(reg, rs, ev, s)
                s.raid_thread_id = thread_id
                s.channel_id = thread_id or 0
                s.note_change(by, f"{by} opened the council at {self.loot_clock(reg)}")
                with reg.store.batch(f"{reg.key}: loot {sid}: opened by {by}"):
                    s.save(f"{reg.key}: loot {sid}: opened by {by}")
                    if thread_id and roster in (None, 0):
                        ev.thread_id = thread_id
                        rs.save(ev, "loot thread opened")
                self.loot_index(s)
        if have is not None:
            if not have.raid_thread_id:
                await self.loot_ensure_thread(reg, have)
            return have, False
        await self._audit(reg, by, f"opened the loot council for {ev.key}" + (f" (roster {roster + 1})" if roster is not None else "")
                          + (" (rehearsal)" if s.test else "") + ("" if thread_id else " — thread pending"), via, by_id)
        if replay:
            await self.feed_replay(reg, ev)
        return s, True

    async def open_council(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "discord") -> list[tuple[LootSession, bool]]:
        """/raid loot, the lock card, the web and MCP: the run's council(s) — one per roster of a split run (session
        `<run>-r<N>`, thread "loot · Roster N · …", candidates that roster's seats only), else one for the run.
        Idempotent per session; a whole-run council opened before the run was split stays the one council. Held
        companion events replay once every council is open (routed by the companion's seated character)."""
        whole = self.loot_session(reg, ev.key)
        if whole is not None or len(ev.all_rosters) <= 1:
            out = [await self.open_loot(reg, rs, ev, by=by, by_id=by_id, via=via, replay=False)]
        else:
            out = [await self.open_loot(reg, rs, ev, by=by, by_id=by_id, via=via, roster=i, replay=False) for i in range(len(ev.all_rosters))]
        if any(created for _, created in out):
            await self.feed_replay(reg, ev)
        return out

    async def loot_ensure_thread(self, reg, session: LootSession) -> bool:
        """A council whose thread is pending (Discord was unreachable at open): try to create it now — the next
        /raid loot, a gateway resume (on_resumed) and every ready call this. True when a thread was made."""
        if session.raid_thread_id or session.origin != "raid" or not self.loot_live(session):
            return False
        busy = self._loot_state().setdefault("threading", set())
        if session.key in busy:
            return False
        busy.add(session.key)
        try:
            rs, ev = self.loot_run(session)
            if ev is None:
                return False
            try:
                tid = await self.loot_open_thread(reg, rs, ev, session)
            except RegistryError:
                return False  # no officer channel configured (any more): open_loot said so at the time
            except Exception as e:  # noqa: BLE001 — still unreachable: the next try
                log.warning("loot thread still pending for %s: %s", session.id, e)
                return False
            if not tid:
                return False
            async with self.loot_lock(*session.key):
                session.raid_thread_id = tid
                session.channel_id = tid
                session.note_change("bot", f"the council's thread was created at {self.loot_clock(reg)}")
                with session.store().batch(f"{reg.key}: loot {session.id}: thread created (was pending)"):
                    session.save(f"{reg.key}: loot {session.id}: thread created (was pending)", touch=False)
                    if session.roster_index in (None, 0):
                        ev.thread_id = tid
                        rs.save(ev, "loot thread opened")
                self.loot_index(session)
        finally:
            busy.discard(session.key)
        await self._audit(reg, "bot", f"loot {session.id}: the council's thread is up (it was pending)", "discord", None)
        thread = self.get_channel(tid) if hasattr(self, "get_channel") else None
        if thread is not None and hasattr(self, "post_boss_tables"):
            try:
                await self.post_boss_tables(session, thread)
            except Exception as e:  # noqa: BLE001
                log.warning("boss tables after a pending thread: %s", e)
        await self.loot_repost(reg, session)
        return True

    async def loot_threads_pending(self) -> int:
        """Every live council still without a thread gets one (if Discord answers now). Returns threads made."""
        n = 0
        for s in list(self.events.values()):
            if s.origin == "raid" and not s.raid_thread_id:
                reg = self.loot_reg(s)
                if reg is not None and await self.loot_ensure_thread(reg, s):
                    n += 1
        return n

    async def on_resumed(self) -> None:
        """discord.Client event: the gateway came back — a council opened while Discord was down gets its thread."""
        await self.loot_threads_pending()

    async def loot_repost(self, reg, session: LootSession) -> int:
        """Re-post what still waits into the council's thread, each with its buttons (an /raid loot on an open council,
        a thread created late, a restart that lost the old buttons): reasons owed, conflicts, ticked drops nobody
        judged, the pending proposals. Returns the messages posted."""
        get = getattr(self, "get_channel", None)
        ch = get(session.raid_thread_id) if get and session.raid_thread_id else None
        if ch is None:
            return 0
        n = 0
        for pr in session.pending_reasons:
            await self.loot_post(session, f"⚠ **{pr.get('item')}** → **{pr.get('recipient')}** in game, but the bot proposed **{pr.get('bot')}** — still waiting for a reason.",
                                 view=self.loot_view(reg, session, "mismatch", pr.get("drop_id")))
            n += 1
        for cf in session.conflicts:
            await self.loot_post(session, f"⚠ **{cf.get('item')}**: in game it went to **{cf.get('in_game')}**, recorded to **{cf.get('recorded')}** — Correct?",
                                 view=self.loot_view(reg, session, "conflict", cf.get("drop_id")))
            n += 1
        und = session.undistributed()
        if und:
            await self.loot_post(session, "📥 Ticked, not judged yet: " + ", ".join(d.item_name for d in und), view=self.loot_view(reg, session, "tick"))
            n += 1
        if session.proposals and hasattr(self, "post_proposals"):
            try:
                await self.post_proposals(session, ch, prefix=f"{len(session.proposals)} proposal(s) still waiting for Confirm:")
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("loot repost proposals: %s", e)
        return n

    # ------------------------------------------------------------ tick
    async def tick(self, reg, session: LootSession, *, add: Iterable[dict] = (), remove: Iterable[str] = (), by: str,
                   by_id: int | None = None, via: str = "discord", source: str = "officer", drops_rev: str | None = None,
                   raw: str | None = None) -> dict:
        """Tick drops (`add`: [{item_id?, name?, boss?, copies?}] — an unknown item is ticked by name, learned into
        <guild>/items/, tiered=False) and un-tick (`remove`: drop ids; a proposed drop loses its proposal; an awarded
        one is refused — use Correct). A merge op: `drops_rev` only when the caller wants a stale refusal."""
        add, remove = list(add), list(remove)
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            if drops_rev is not None and drops_rev != ls.drops_rev(session):
                raise self._changed(session, "The drops changed since you looked")
            ctx = self.loot_ctx(session)
            added: list[Drop] = []
            removed: list[Drop] = []
            for did in remove:
                d = session.drop(did)
                if d is None:
                    continue
                aw = session.awarded(did)
                if aw is not None:
                    raise RegistryError(f"{d.item_name} is already awarded to {aw.raider} — use Correct.")
            store = session.store()
            with store.batch(f"{session.guild}: loot {session.id}: drops ticked by {by}"):
                for a in add:
                    iid = int(a.get("item_id") or 0)
                    name = str(a.get("name") or "").strip()[:100]
                    item = ctx.item(iid or None, name or None)
                    if item is None and not (iid or name):
                        raise RegistryError("Name the item that dropped.")
                    boss = str(a.get("boss") or "").strip()[:80] or (item.boss if item is not None and item.boss not in ("?", "") else "") or session.current_boss or "?"
                    if (item is None or not item.tiers) and not session.test and session.origin == "raid":
                        item = ctx.learn(iid or None, name or (item.name if item else None), boss=None if boss == "?" else boss,
                                         instance=session.instance, run=session.run_key, source=source)
                    nm = item.name if item is not None else (name or f"item {iid}")
                    for _ in range(max(1, min(int(a.get("copies") or 1), 5))):
                        d = Drop(drop_id=session.new_drop_id(), item_id=item.id if item is not None else iid, item_name=nm, boss=boss,
                                 tiered=bool(item is not None and item.tiers), source=source, by=by, raw=raw)
                        session.drop_list.append(d)
                        added.append(d)
                for did in remove:
                    d = session.drop(did)
                    if d is None:
                        continue
                    session.proposals = [p for p in session.proposals if p.drop_id != did]
                    session.drop_list = [x for x in session.drop_list if x.drop_id != did]
                    removed.append(d)
                if added or removed:
                    if added:
                        session.current_boss = added[-1].boss
                    what = (f"ticked {', '.join(d.item_name for d in added)}" if added else "") + ("; " if added and removed else "") + (f"un-ticked {', '.join(d.item_name for d in removed)}" if removed else "")
                    session.note_change(by, f"{by} {what} at {self.loot_clock(reg)}")
                    session.save(f"{session.guild}: loot {session.id}: {what}"[:150])
        line = None
        if added or removed:
            parts = []
            if added:
                parts.append("📥 " + ", ".join(f"**{d.item_name}**" + ("" if d.tiered else " 🆕") for d in added) + (f" from **{added[0].boss}**" if added[0].boss != "?" else ""))
            if removed:
                parts.append("↩ un-ticked " + ", ".join(d.item_name for d in removed))
            line = " · ".join(parts)
            await self._audit(reg, by, f"loot {session.run_key or session.id}: {line.replace('**', '')}", via, by_id)
            await self.loot_notify(reg, session, line, kind="tick")
        return {"added": added, "removed": removed, "line": line, "drops_rev": ls.drops_rev(session)}

    # ------------------------------------------------------------ distribute
    async def distribute(self, reg, session: LootSession, *, by: str, by_id: int | None = None, via: str = "discord",
                         drops_rev: str | None = None, only: Iterable[str] | None = None, progress=None,
                         rejudge: bool = False) -> dict:
        """Judge the ticked drops nobody has proposed or awarded (`only`: some of them; `rejudge`: the pending
        proposals too — after an override moved the table). APPENDS a batch; earlier pending picks count as provisional
        awards. Single flight per session ("Distributing already (Jonny, 8:12 PM)"); the judge runs in a worker thread
        on a snapshot and the results merge on the loop into drops still pending whose table didn't move."""
        st = self._loot_state()
        key = session.key
        flight = st["flight"].get(key)
        if flight:
            raise LootBusy(f"Distributing already ({flight['by']}, {flight['clock']}).")
        only = set(only) if only is not None else None
        async with self.loot_lock(*key):
            self.loot_check_live(session)
            if st["flight"].get(key):
                f = st["flight"][key]
                raise LootBusy(f"Distributing already ({f['by']}, {f['clock']}).")
            if drops_rev is not None and drops_rev != ls.drops_rev(session):
                raise self._changed(session, "The drops changed since you looked")
            todo = [d.drop_id for d in session.undistributed() if only is None or d.drop_id in only]
            if rejudge:
                todo += [p.drop_id for p in session.proposals if (only is None or p.drop_id in only) and p.drop_id not in todo]
            if not todo:
                raise RegistryError("Nothing to distribute — tick what dropped first (every ticked drop is proposed or awarded).")
            roster = self.loot_roster(session)
            if not roster:
                raise RegistryError("Nobody is rostered on this run, so there is nobody to judge for.")
            ctx = self.loot_ctx(session)
            fkey = ",".join(sorted(todo))
            scores_only = bool(session.scores_only) or session.failures.get(fkey, 0) >= 2 or ctx.provider is None
            n = max([int(b.get("n", 0)) for b in session.batches], default=0) + 1
            st["flight"][key] = {"by": by, "clock": self.loot_clock(reg), "since": now_iso(), "rejudge": rejudge, "drops": len(todo)}
            session.distributing = {"by": by, "by_id": by_id, "since": now_iso(), "drops": todo}
            session.save(f"{session.guild}: loot {session.id}: distributing {len(todo)} ({by})")
            snap = ls.snapshot(session)
        try:
            results = await asyncio.to_thread(ls.run_distribution, ctx, snap, roster, todo, provider=None if scores_only else "ctx", progress=progress, batch=n)
        except BaseException:
            st["flight"].pop(key, None)
            async with self.loot_lock(*key):
                session.distributing = None
                if self.events.get(key) is session:  # an ended council is never written again
                    session.save(f"{session.guild}: loot {session.id}: distribute failed")
            raise
        async with self.loot_lock(*key):
            st["flight"].pop(key, None)
            if self.events.get(key) is not session:  # ended while the judge ran (end_run refuses that; a stale reference)
                session.distributing = None
                raise RegistryError("This loot council ended while it was judging — nothing was recorded.")
            merged, stale = ls.merge(ctx, session, snap, self.loot_roster(session), results)
            new = [p for p in merged if p.batch == n]
            if new:
                session.batches.append({"n": n, "by": by, "at": now_iso(), "drops": [p.drop_id for p in new], "bosses": sorted({p.boss for p in new})})
            session.distributing = None
            session.failures.pop(fkey, None)
            session.interrupted = None
            if merged:
                session.note_change(by, f"{by} distributed {len(merged)} at {self.loot_clock(reg)}")
            session.save(f"{session.guild}: loot {session.id}: {len(merged)} proposals ({by})" + (f", {len(stale)} stale" if stale else ""))
        line = (f"⚖️ {len(merged)} proposal(s)" + (" (re-judged)" if rejudge else "") + (f" · {len(stale)} drop(s) changed while judging — distribute again" if stale else "")) if merged or stale else None
        if merged or stale:
            await self._audit(reg, by, f"loot {session.run_key or session.id}: distributed {len(merged)}" + (" (scores only)" if scores_only else ""), via, by_id)
            await self.loot_notify(reg, session, line, kind="distribute")
        return {"batch": n if new else None, "proposals": merged, "stale": stale, "line": line, "scores_only": scores_only, "batch_rev": ls.batch_rev(session)}

    # ------------------------------------------------------------ override
    async def override(self, reg, session: LootSession, drop_id: str, to: str, reason: str, *, by: str, by_id: int | None = None,
                       via: str = "discord", batch_rev: str | None = None, replace: bool = False, rejudge: bool = True) -> str:
        """Award a drop to someone other than the bot's pick (or `DISENCHANT`). Lives on the proposal until Confirm (the
        precedent is written then); a reason is required. A drop not yet distributed gets a deterministic proposal on the
        spot. A second override by another officer needs `replace`; a recorded drop is refused (use Correct). The other
        pending picks are re-judged afterwards (`rejudge`) — the batch_rev moves when a pick moved."""
        reason = ls.check_reason(reason)
        who_in = str(to or "").strip()
        if not who_in or len(who_in) > NAME_MAX:
            raise RegistryError("Name who gets it (a character), or Disenchant.")
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            d = session.drop(drop_id)
            if d is None:
                raise RegistryError("That drop isn't on this council any more.")
            aw = session.awarded(drop_id)
            if aw is not None:
                raise RegistryError(f"{d.item_name} was recorded at {self.loot_clock(reg, aw.confirmed_at)} by {aw.by or '?'} — use Correct.")
            if batch_rev is not None and batch_rev != ls.batch_rev(session):
                raise self._changed(session, "The batch changed")
            ctx = self.loot_ctx(session)
            roster = self.loot_roster(session)
            p = session.pending(drop_id)
            if p is None:  # never distributed: a deterministic proposal now, so the override sits on something
                (p,) = ls.run_distribution(ctx, session, roster, [drop_id], provider=None, concurrency=1,
                                           batch=max([int(b.get("n", 0)) for b in session.batches], default=0) + 1)
                session.proposals.append(p)
            if p.override and not replace and p.override.by != by:
                raise LootChanged(f"{p.override.by} already set this to {p.override.to} ('{p.override.reason}') — replace?")
            who = DISENCHANT if who_in.lower() in ("disenchant", DISENCHANT) else ls.recipient_of(reg, roster, who_in)[0]
            if who == p.award_to:
                p.override = None
                what = f"{by} put the {d.item_name} back to the bot's pick {who} at {self.loot_clock(reg)}"
            else:
                p.override = Override(to=who, reason=reason, by=by, by_id=by_id)
                what = f"{by} overrode the {d.item_name} → {who} at {self.loot_clock(reg)}"
            session.note_change(by, what)
            session.save(f"{session.guild}: loot {session.id}: {what}"[:150])
        target = "disenchant" if who == DISENCHANT else who
        line = f"⚠ **{d.item_name}** → **{target}** (bot picked {p.award_to}) — “{reason}” · not recorded until Confirm" if p.override else f"↩ **{d.item_name}** back to the bot's pick **{who}**"
        await self._audit(reg, by, f"loot {session.run_key or session.id}: {d.item_name} → {target} (bot: {p.award_to}) — {reason}", via, by_id)
        await self.loot_notify(reg, session, line, kind="override")
        others = [x.drop_id for x in session.proposals if x.drop_id != drop_id and not x.override]
        if rejudge and others:
            # marked BEFORE the task starts: Confirm the rest refuses from the moment the override returns (decision 7)
            self._rescoring()[session.key] = len(others)
            self._spawn(self._rejudge(reg, session, others, by))
        return line

    async def _rejudge(self, reg, session: LootSession, drop_ids: list[str], by: str) -> None:
        try:
            await self.distribute(reg, session, by=by, only=drop_ids, rejudge=True)
        except LootBusy:
            pass  # a distribute is running: it reads the override from the live session at merge time (stale → again)
        except RegistryError as e:
            log.info("loot re-judge skipped: %s", e)
        finally:
            self._rescoring().pop(session.key, None)

    # ------------------------------------------------------------ confirm
    async def confirm(self, reg, session: LootSession, *, only: Iterable[str] | None = None, expect_to: str | dict | None = None,
                      batch_rev: str | None = None, by: str, by_id: int | None = None, via: str = "discord",
                      source: str | None = None) -> dict:
        """Record pending picks as awards: one drop (`only={d}`, `expect_to` = the recipient the presser saw) or the rest
        (`batch_rev` of what they saw). Inside one GitStore.batch and under the session lock: the session first, then
        the ledger rows the awards imply that the ledger lacks (by import_id), then the precedents of the overrides
        confirmed (with their typed reasons). A drop already recorded → "already recorded (Mira, 8:14 PM)"."""
        only = set(only) if only is not None else None
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            if only is None:
                busy = self.loot_rescoring(session)
                if busy:  # decision 7: a re-score may move the very picks Confirm the rest would record
                    raise LootBusy(f"Re-scoring {busy} item(s) — Confirm the rest waits until it's done.")
                if batch_rev is not None and batch_rev != ls.batch_rev(session):
                    raise self._changed(session, "The batch changed")
                chosen = list(session.proposals)
            else:
                chosen = []
                for did in sorted(only, key=lambda x: [d.drop_id for d in session.drop_list].index(x) if session.drop(x) else 0):
                    p = session.pending(did)
                    if p is None:
                        aw = session.awarded(did)
                        if aw is not None:
                            raise RegistryError(f"Already recorded ({aw.by or '?'}, {self.loot_clock(reg, aw.confirmed_at)}): {aw.item_name} → {aw.raider}.")
                        raise RegistryError("Nothing pending on that drop — distribute it first.")
                    want = expect_to.get(did) if isinstance(expect_to, dict) else expect_to
                    if want is not None and p.to.lower() != str(want).lower():
                        raise self._changed(session, f"The pick changed: {p.item_name} now goes to {p.to}")
                    chosen.append(p)
            if not chosen:
                raise RegistryError("Nothing pending to confirm.")
            nobody = [p for p in chosen if p.to == NOBODY]
            if nobody and only is not None:
                raise RegistryError(f"Nobody rostered is a candidate for {nobody[0].item_name} — override it to someone, or disenchant it.")
            chosen = [p for p in chosen if p.to != NOBODY]  # Confirm-all leaves those waiting for an override
            if not chosen:
                raise RegistryError("Nothing to confirm: every pick waiting has no candidate — override them to someone, or disenchant.")
            awards = self._record(reg, session, chosen, by, source)
        lines = await self._after_record(reg, session, awards, by, by_id, via)
        return {"awards": awards, "lines": lines, "batch_rev": ls.batch_rev(session)}

    def loot_rescoring(self, session: LootSession) -> int:
        """Drops a judge is re-scoring on this council now (a re-judge after an override, about to start or running, or
        any Distribute in flight) — 0 when none. Confirm the rest refuses while it is not 0; per-item Confirm (with the
        pick the presser saw) stays allowed."""
        st = self._loot_state()
        f = st["flight"].get(session.key)
        return int(self._rescoring().get(session.key) or (f or {}).get("drops") or len((session.distributing or {}).get("drops") or []) or 0)

    async def _after_record(self, reg, session: LootSession, awards, by: str, by_id: int | None, via: str) -> list[str]:
        """After confirm's write (the caller released the lock): one ops line per award and the thread line."""
        lines = [f"{a.item_name} → {'disenchant' if a.raider == DISENCHANT else a.raider}" + (" (override)" if a.source == "override" else "") for a in awards]
        for a, ln in zip(awards, lines):
            await self._audit(reg, by, f"loot {session.run_key or session.id}: recorded {ln}" + (" (rehearsal)" if session.test else ""), via, by_id)
        await self.loot_notify(reg, session, "✅ " + " · ".join(lines), kind="confirm")
        return lines

    def _record(self, reg, session: LootSession, chosen, by: str, source: str | None) -> list:
        """The write behind confirm (caller holds the lock): awards into the session, then ledger + precedents."""
        ctx = self.loot_ctx(session)
        roster = self.loot_roster(session)
        awards, precs = [], []
        store = session.store()
        with store.batch(f"{session.guild}: loot {session.id}: {by} recorded {len(chosen)}"):
            for p in chosen:
                d = session.drop(p.drop_id)
                if d is None:
                    continue
                a = ls.make_award(ctx, session, d, p.to, roster, source=source or p.source, by=by, cands=p.result.candidates, bot_pick=p.award_to, reg=reg)
                session.awards.append(a)
                if p.override:
                    prec = ls.precedent_row(session, a, p.override.reason, p.override.by, bot=p.award_to)
                    session.overrides.append(prec)
                    precs.append(prec)
                session.proposals = [x for x in session.proposals if x.drop_id != p.drop_id]
                awards.append(a)
            session.note_change(by, f"{by} recorded {', '.join(a.item_name or '?' for a in awards)} at {self.loot_clock(reg)}")
            session.save(f"{session.guild}: loot {session.id}: {by} recorded {len(awards)}")
            if not session.test:
                ls.sync_ledger(ctx, session)
                for prec in precs:
                    ctx.append_precedent(prec)
        return awards

    # ------------------------------------------------------------ discard
    async def discard(self, reg, session: LootSession, *, only: Iterable[str] | None = None, batch_rev: str | None = None,
                      by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Drop pending proposals (all — carrying `batch_rev` — or `only` some: "Decide later", the drop shows held).
        Ticks stay; pending overrides go with their proposals; nothing was recorded, so nothing is undone."""
        only = set(only) if only is not None else None
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            if only is None and batch_rev is not None and batch_rev != ls.batch_rev(session):
                raise self._changed(session, "The batch changed")
            gone = [p for p in session.proposals if only is None or p.drop_id in only]
            if not gone:
                raise RegistryError("Nothing pending to discard.")
            session.proposals = [p for p in session.proposals if p not in gone]
            if only is not None:
                for p in gone:
                    d = session.drop(p.drop_id)
                    if d is not None:
                        d.held = True
            what = f"{by} {'held' if only is not None else 'discarded'} {len(gone)} proposal(s) at {self.loot_clock(reg)}"
            session.note_change(by, what)
            session.save(f"{session.guild}: loot {session.id}: {what}")
        n_ov = sum(1 for p in gone if p.override)
        line = (f"⏸ {', '.join(p.item_name for p in gone)} held — still waiting" if only is not None else
                f"🗑 {len(gone)} proposal(s) discarded — the ticks stay" + (f"; {n_ov} pending override(s) dropped" if n_ov else "") + "; nothing was recorded")
        await self._audit(reg, by, f"loot {session.run_key or session.id}: {line}", via, by_id)
        await self.loot_notify(reg, session, line, kind="discard")
        return line

    # ------------------------------------------------------------ reasons and corrections
    async def set_reason(self, reg, session: LootSession, drop_id: str, reason: str, *, by: str, by_id: int | None = None,
                         via: str = "discord", replace: bool = False) -> str:
        """The reason for an award the companion recorded against the bot's pick (bound to the drop and its recipient):
        becomes the precedent. Idempotent on the award's import_id — a second reason needs `replace` ("Mira already gave
        a reason at 8:20 PM — replace?"); a replaced reason supersedes the old precedent (append-only). `session` may be
        a run key (str) for a council that has ended: the award is found in the ledger by its import_id (days later, from
        the site's "needs a reason" list)."""
        reason = ls.check_reason(reason)
        if isinstance(session, str):
            return await self._reason_after_end(reg, session, drop_id, reason, by=by, by_id=by_id, via=via, replace=replace)
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            aw = session.awarded(drop_id)
            if aw is None:
                raise RegistryError("Nothing is recorded on that drop.")
            ctx = self.loot_ctx(session)
            pr = next((x for x in session.pending_reasons if x.get("drop_id") == drop_id), None)
            # ANY precedent of the award (active, superseded or retired): a retired one stays retired unless an officer
            # deliberately replaces it — the "needs a reason" list never undoes a retire
            have = next((o for o in reversed(session.overrides) if o.get("import_id") == aw.import_id), None)
            if have is None and not session.test:
                have = precedent_any(ctx, aw.import_id)
            if have is not None and not replace:
                raise LootChanged(f"{have.get('by', '?')} already gave a reason at {self.loot_clock(reg, have.get('at'))} — replace?")
            prec = ls.precedent_row(session, aw, reason, by, bot=(pr or {}).get("bot") or aw.bot_pick)
            store = session.store()
            with store.batch(f"{session.guild}: loot {session.id}: reason for {aw.item_name} ({by})"):
                for o in session.overrides:
                    if o.get("import_id") == aw.import_id:
                        o["status"] = "superseded"
                session.overrides.append(prec)
                session.pending_reasons = [x for x in session.pending_reasons if x.get("drop_id") != drop_id]
                session.note_change(by, f"{by} gave the reason for {aw.item_name} at {self.loot_clock(reg)}")
                session.save(f"{session.guild}: loot {session.id}: reason for {aw.item_name}")
                if not session.test:
                    ctx.append_precedent(prec)
        line = f"📌 Precedent: {aw.item_name} → {aw.raider} over {prec['bot']} — “{reason}”"
        await self._audit(reg, by, f"loot {session.run_key or session.id}: {line}", via, by_id)
        await self.loot_notify(reg, session, line, kind="reason")
        return line

    def loot_reasons_pending(self, reg) -> list[dict]:
        """Awards the companion recorded against the bot's pick with no reason yet (the "needs a reason" list): ledger
        rows with source `companion` whose import_id has no precedent at all (a retired one counts: retiring a reason
        never puts the award back on the list) — live councils and ended ones alike."""
        from .lootctx import RegistryLootContext

        ctx = self.__dict__.setdefault("contexts", {}).get(reg.key) or RegistryLootContext(reg, reg.store, None)
        out = []
        for a in ctx.ledger:
            if a.source == "companion" and a.import_id and not a.void and precedent_any(ctx, a.import_id) is None:
                sid = a.import_id.rsplit("-", 1)[0]
                out.append({"import_id": a.import_id, "run": sid, "session": sid, "drop_id": a.drop_id, "item": a.item_name,
                            "recipient": a.raider, "bot": a.bot_pick, "received": a.received.isoformat()})
        return out

    def loot_conflicts_pending(self, reg) -> list[dict]:
        """In-game awards that disagree with what the council recorded and nobody corrected yet — live councils and
        ended ones (their archives) alike: {session, run, drop_id, item, in_game, recorded, at, ended}. Fixed with
        `correct(reg, <session id>, drop_id, …)`; a conflict is closed by any correction of that drop, or when the
        live record already names the in-game recipient."""
        ctx = self._guild_ctx(reg)
        found: list[tuple[str, dict, bool]] = []
        for s in self.events.values():
            if s.guild == reg.key and s.origin == "raid":
                found += [(s.id, cf, False) for cf in s.conflicts]
        arch = reg.store.root / reg.key / "events" / "archive"
        if arch.is_dir():
            for f in sorted(arch.glob("*.json")):
                try:
                    body = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if body.get("origin") == "raid":
                    found += [(str(body.get("id")), cf, True) for cf in body.get("conflicts") or []]
        out = []
        for sid, cf, ended in found:
            base = f"{sid}-{cf.get('drop_id')}"
            if any(i.startswith(base + "-c") for i in ctx.import_ids):
                continue
            rows = [a for a in ctx.ledger if a.import_id == base and not a.void]
            if rows and rows[-1].raider.lower() == str(cf.get("in_game") or "").lower():
                continue
            out.append({**cf, "session": sid, "run": _run_of(sid), "ended": ended})
        return out

    def _guild_ctx(self, reg):
        from .lootctx import RegistryLootContext

        return self.__dict__.setdefault("contexts", {}).get(reg.key) or RegistryLootContext(reg, reg.store, None)

    async def _correct_after_end(self, reg, sid: str, drop_id: str, who_in: str, reason: str, *, by: str, by_id, via: str) -> str:
        live = next((s for s in self.events.values() if s.guild == reg.key and s.id == sid), None)
        if live is not None:
            return await self.correct(reg, live, drop_id, who_in, reason, by=by, by_id=by_id, via=via)
        from .loot import scoring

        ctx = self.loot_ctx(LootSession(id=sid, run_key=_run_of(sid), instance="", date="1970-01-01", guild=reg.key, origin="raid"))
        base = f"{sid}-{drop_id}"
        async with self.loot_lock(reg.key, sid):
            rows = [a for a in ctx.ledger if a.import_id and (a.import_id == base or a.import_id.startswith(base + "-c")) and not a.void]
            if not rows:
                raise RegistryError("Nothing is recorded on that drop.")
            aw = rows[-1]
            if who_in.lower() in ("disenchant", DISENCHANT):
                who, uid, tier, weight = DISENCHANT, None, "-", 0.0
            else:
                who, uid, _ = ls.recipient_of(reg, [], who_in)
                spec = None
                m = reg.members.get(uid) if uid is not None else None
                c = next((c for c in (m.characters if m else []) if c.label.lower() == who.lower()), None)
                try:
                    spec = ctx.profile.spec(c.cls, c.spec) if c else None
                except KeyError:
                    spec = None
                item = ctx.item(aw.item_id or None, aw.item_name)
                tier, weight = scoring.weight_for(ctx.profile, item, spec, ctx.loot_cfg) if item is not None else ("?", float(ctx.loot_cfg.get("untiered_weight", 0.5)))
            if who.lower() == aw.raider.lower():
                raise RegistryError(f"It is already recorded to {aw.raider}.")
            n = 1 + sum(1 for i in ctx.import_ids if i.startswith(base + "-c"))
            new = aw.model_copy(update={"raider": who, "discord_id": uid, "tier": tier, "total_weight": weight, "offspec": False, "source": "correct",
                                        "by": by, "confirmed_at": now_iso(), "import_id": f"{base}-c{n}", "void": False})
            msg = f"{reg.key}: loot {sid}: {aw.item_name} corrected to {who} ({by})"
            with reg.store.batch(msg):
                ctx.void(aw.import_id, reason, by, now_iso())
                ctx.append_ledger({**json.loads(new.model_dump_json()), "event": _run_of(sid)}, new)
                self._retire_corrected(ctx, aw.import_id, reason, by)
                reg.store.commit(msg)
        target = "disenchant" if who == DISENCHANT else who
        line = f"✎ **{aw.item_name}**: was {aw.raider}, now **{target}** — “{reason}”"
        await self._audit(reg, by, f"loot {sid}: {line.replace('**', '')}", via, by_id)
        return line

    async def _reason_after_end(self, reg, run_key: str, drop_id: str, reason: str, *, by: str, by_id, via: str, replace: bool) -> str:
        live = next((s for s in self.events.values() if s.guild == reg.key and s.id == run_key), None) or next(
            (s for s in self.events.values() if s.guild == reg.key and s.run_key == run_key and s.roster_index is None), None)
        if live is not None:
            return await self.set_reason(reg, live, drop_id, reason, by=by, by_id=by_id, via=via, replace=replace)
        ctx = self.loot_ctx(LootSession(id=run_key, run_key=run_key, instance="", date="1970-01-01", guild=reg.key, origin="raid"))
        import_id = f"{run_key}-{drop_id}"
        async with self.loot_lock(reg.key, run_key):
            aw = next((a for a in ctx.ledger if a.import_id == import_id and not a.void), None)
            if aw is None:
                raise RegistryError("Nothing is recorded on that drop.")
            have = precedent_any(ctx, import_id)
            if have is not None and not replace:
                raise LootChanged(f"{have.get('by', '?')} already gave a reason at {self.loot_clock(reg, have.get('at'))} — replace?")
            prec = {"date": aw.received.isoformat(), "event": _run_of(run_key), "item": aw.item_name, "item_id": aw.item_id, "drop_id": drop_id,
                    "import_id": import_id, "bot": aw.bot_pick or "?", "human": aw.raider, "reason": reason, "by": by, "at": now_iso(),
                    "policy_version": aw.policy_version, "status": "active"}
            with reg.store.batch(f"{reg.key}: loot {run_key}: reason for {aw.item_name} ({by})"):
                ctx.append_precedent(prec)
                reg.store.commit(f"{reg.key}: loot {run_key}: reason for {aw.item_name} ({by})")
        line = f"📌 Precedent: {aw.item_name} → {aw.raider} over {prec['bot']} — “{reason}”"
        await self._audit(reg, by, f"loot {run_key}: {line}", via, by_id)
        return line

    async def correct(self, reg, session: LootSession, drop_id: str, to: str, reason: str, *, by: str, by_id: int | None = None,
                      via: str = "discord") -> str:
        """Fix a recorded award (a mistake, not a decision): the old ledger row is voided (a `void` row carrying the
        reason) and a new award recorded; no precedent. A pending reason or a conflict on that drop is closed.
        `session` may be a session id (str) for a council that has ended: the award is found in the ledger by its
        import_id (a conflict days later, from loot_conflicts_pending)."""
        reason = ls.check_reason(reason)
        who_in = str(to or "").strip()
        if not who_in or len(who_in) > NAME_MAX:
            raise RegistryError("Name who it should be recorded to (a character), or Disenchant.")
        if isinstance(session, str):
            return await self._correct_after_end(reg, session, drop_id, who_in, reason, by=by, by_id=by_id, via=via)
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            d = session.drop(drop_id)
            aw = session.awarded(drop_id)
            if d is None or aw is None:
                raise RegistryError("Nothing is recorded on that drop yet — Override or Confirm it instead.")
            ctx = self.loot_ctx(session)
            roster = self.loot_roster(session)
            who = DISENCHANT if who_in.lower() in ("disenchant", DISENCHANT) else ls.recipient_of(reg, roster, who_in)[0]
            if who.lower() == aw.raider.lower():
                raise RegistryError(f"It is already recorded to {aw.raider}.")
            n = 1 + sum(1 for a in session.awards if a.drop_id == drop_id and a.source == "correct")
            new = ls.make_award(ctx, session, d, who, roster, source="correct", by=by, bot_pick=aw.bot_pick, reg=reg)
            new.import_id = f"{session.id}-{drop_id}-c{n}"
            store = session.store()
            with store.batch(f"{session.guild}: loot {session.id}: {d.item_name} corrected to {who} ({by})"):
                aw.void = True
                session.awards.append(new)
                session.pending_reasons = [x for x in session.pending_reasons if x.get("drop_id") != drop_id]
                session.conflicts = [x for x in session.conflicts if x.get("drop_id") != drop_id]
                for o in session.overrides:  # the council decision the correction undid is no longer cited (run_distribution, the summary)
                    if o.get("import_id") == aw.import_id and o.get("status", "active") == "active":
                        o["status"] = "retired"
                session.note_change(by, f"{by} corrected the {d.item_name} to {who} at {self.loot_clock(reg)}")
                session.save(f"{session.guild}: loot {session.id}: {d.item_name} corrected")
                if not session.test:
                    ctx.void(aw.import_id, reason, by, now_iso())
                    ls.sync_ledger(ctx, session)
                    self._retire_corrected(ctx, aw.import_id, reason, by)
        target = "disenchant" if who == DISENCHANT else who
        line = f"✎ **{d.item_name}**: was {aw.raider}, now **{target}** — “{reason}”"
        await self._audit(reg, by, f"loot {session.run_key or session.id}: {line.replace('**', '')}", via, by_id)
        await self.loot_notify(reg, session, line, kind="correct")
        return line

    @staticmethod
    def _retire_corrected(ctx, import_id: str, reason: str, by: str) -> int:
        """A corrected award's precedent (the override's, or a reason given for a companion award) describes a council
        decision that, per the correction, never happened: an appended `kind: status` row retires it (append-only stays
        append-only; lootctx folds it) so policy_for_llm stops citing it. The caller holds the lock and a batch."""
        hit = [p for p in ctx.precedents if p.get("import_id") == import_id and p.get("status", "active") == "active"]
        if not hit:
            return 0
        ctx.store.append_jsonl(Path(ctx.guild_key) / "precedents.jsonl", {"kind": "status", "import_id": import_id, "status": "retired", "by": by,
                                                                          "reason": f"award corrected: {reason}", "at": now_iso()})
        for p in hit:
            p["status"] = "retired"
        return len(hit)

    async def remove_candidate(self, reg, session: LootSession, who, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """Someone who left the roster after open stops being a candidate ("still a candidate? Remove")."""
        key = str(who).lower()
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            keep = [p for p in session.departed if str(p.discord_id) != key and (p.character or p.signup_name).lower() != key]
            if len(keep) == len(session.departed):
                raise RegistryError("They aren't on the left-the-roster list.")
            gone = [p for p in session.departed if p not in keep]
            session.departed = keep
            session.note_change(by, f"{by} removed {gone[0].character or gone[0].signup_name} from the candidates")
            session.save(f"{session.guild}: loot {session.id}: candidate removed")
        name = gone[0].character or gone[0].signup_name
        await self._audit(reg, by, f"loot {session.run_key or session.id}: {name} is no longer a candidate", via, by_id)
        await self.loot_notify(reg, session, f"➖ {name} is no longer a candidate", kind="roster")
        return name

    async def set_scores_only(self, reg, session: LootSession, on: bool, *, by: str, by_id: int | None = None, via: str = "discord") -> str:
        """The Scores-only toggle: Distribute picks by score without a model call (the proposals' source says so)."""
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            session.scores_only = {"by": by, "at": now_iso()} if on else None
            session.note_change(by, f"{by} turned scores only {'on' if on else 'off'}")
            session.save(f"{session.guild}: loot {session.id}: scores only {'on' if on else 'off'}")
        line = "✎ Scores only " + ("on — picks by score, no model call" if on else "off")
        await self._audit(reg, by, f"loot {session.run_key or session.id}: scores only {'on' if on else 'off'}", via, by_id)
        await self.loot_notify(reg, session, line, kind="scores_only")
        return line

    async def keep_open(self, reg, session: LootSession, *, by: str, minutes: int = 60, by_id: int | None = None, via: str = "discord") -> str:
        """"Keep open": the scheduler's auto-end waits `minutes` more."""
        async with self.loot_lock(*session.key):
            self.loot_check_live(session)
            session.keep_open_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            session.closing_notice_at = None
            session.note_change(by, f"{by} kept the council open")
            session.save(f"{session.guild}: loot {session.id}: kept open ({by})")
        await self._audit(reg, by, f"loot {session.run_key or session.id}: kept open {minutes} min", via, by_id)
        return f"Kept open until {self.loot_clock(reg, session.keep_open_until)}."

    # ------------------------------------------------------------ end
    def loot_summary(self, reg, session: LootSession) -> dict:
        """The end summary (also the decide screen's sentence): awards, overrides, what was never confirmed /
        distributed / given a reason. Pending proposals and overrides are kept inside it, so nothing decided vanishes."""
        live = [a for a in session.awards if not a.void]
        never = [{"drop_id": p.drop_id, "item": p.item_name, "to": p.to, "override": p.override.model_dump() if p.override else None} for p in session.proposals]
        undist = [{"drop_id": d.drop_id, "item": d.item_name, "boss": d.boss} for d in session.undistributed()]
        overrides = [o for o in session.overrides if o.get("status", "active") == "active"]
        agreed = sum(1 for a in live if a.source == "bot")
        by_char: dict[str, list[str]] = {}
        for a in live:
            by_char.setdefault("disenchant" if a.raider == DISENCHANT else a.raider, []).append(a.item_name or "?")
        lines = [f"**Loot council · {session.run_key or session.id}" + (f" · {session.label}" if session.roster_index is not None and session.label else "") + "**" + (" (rehearsal — nothing was recorded)" if session.test else "")]
        lines.append(f"{len(live)} award(s) to {len(by_char)} recipient(s)" + (f" · {len(overrides)} override(s)" if overrides else "") + (f" · the council agreed with the bot on {agreed}/{len(live)}" if live else ""))
        lines += [f"• **{w}**: {', '.join(i)}" for w, i in sorted(by_char.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
        if overrides:
            lines.append("Overrides: " + "; ".join(f"{o['item']}: {o['bot']} → {o['human']} — “{o['reason']}”" for o in overrides))
        if never:
            lines.append(f"{len(never)} proposal(s) never confirmed: " + ", ".join(f"{x['item']} → {x['to']}" + (" (override)" if x["override"] else "") for x in never))
        if undist:
            lines.append(f"{len(undist)} drop(s) never distributed: " + ", ".join(x["item"] for x in undist))
        if session.pending_reasons:
            lines.append(f"{len(session.pending_reasons)} award(s) still waiting for a reason: " + ", ".join(f"{x.get('item')} → {x.get('recipient')}" for x in session.pending_reasons))
        return {"awards": len(live), "recipients": len(by_char), "overrides": len(overrides), "agreed": agreed, "never_confirmed": never,
                "undistributed": undist, "reasons_pending": list(session.pending_reasons), "conflicts": list(session.conflicts),
                "test": session.test, "ended_at": now_iso(), "text": "\n".join(lines)}

    def end_consequence(self, reg, rs, ev) -> str:
        """The End decide screen's sentence: 'End <run>: 6 awards, 1 override, 2 drops never distributed, 1 award
        waiting for a reason.'"""
        from .raid_views import run_title

        sessions = [s for s in self.events.values() if s.guild == reg.key and s.run_key == ev.key]
        if not sessions:
            return f"End {run_title(reg, ev)}: no loot council is open — the run is marked done."
        parts = []
        for key, label in (("awards", "award(s)"), ("overrides", "override(s)")):
            n = sum(self.loot_summary(reg, s)[key] for s in sessions)
            parts.append(f"{n} {label}")
        for key, label in (("never_confirmed", "proposal(s) never confirmed"), ("undistributed", "drop(s) never distributed"), ("reasons_pending", "award(s) waiting for a reason")):
            n = sum(len(self.loot_summary(reg, s)[key]) for s in sessions)
            if n:
                parts.append(f"{n} {label}")
        return f"End {run_title(reg, ev)}: " + ", ".join(parts) + "."

    async def end_run(self, reg, rs, ev, *, by: str, by_id: int | None = None, via: str = "discord", refresh: bool = True) -> str:
        """The run is over: every session of it is archived with its summary (pending proposals and overrides kept
        inside), the run flips to done, the sheet and lock card are redrawn closed (`refresh`; the scheduler redraws
        them itself), the summary text is returned (posting it is the surfaces': loot_ended). /raid end, the End
        button, the web, MCP, plain text and the scheduler all come here — one rule for all: only a LOCKED run (or one
        whose council is still open) ends, and never while a Distribute is judging (wait or retry)."""
        sessions = [s for s in list(self.events.values()) if s.guild == reg.key and s.run_key == ev.key]
        if ev.state != "locked" and not sessions:
            raise RegistryError("No locked run to end." if ev.state not in ("done", "cancelled") else f"That run is already {ev.state}.")
        self._end_busy(sessions)
        texts = []
        for s in sessions:
            async with self.loot_lock(*s.key):
                if self.events.get(s.key) is not s:
                    continue  # another End got here first
                self._end_busy([s])
                summary = self.loot_summary(reg, s)
                s.note_change(by, f"{by} ended the council")
                with s.store().batch(f"{reg.key}: loot {s.id}: ended by {by}"):
                    s.archive(summary)
                self.loot_forget(s)
            texts.append(summary["text"])
            try:
                await self.loot_ended(reg, s, summary["text"])
            except Exception as e:  # noqa: BLE001 — the archive is written; the post is the surfaces'
                log.warning("loot end post failed: %s", e)
        changed = rc.end_run(reg, rs, ev, by)
        await self._audit(reg, by, f"ended {ev.key}" + (f" and its loot council ({len(texts)})" if texts else ""), via, by_id)
        if changed and refresh and hasattr(self, "refresh_sheet"):  # the sheet and lock card leave their live buttons
            try:
                await self.refresh_sheet(reg, ev)
            except Exception as e:  # noqa: BLE001 — the run is done; the redraw is the surfaces'
                log.warning("sheet refresh after end failed: %s", e)
        from .raid_views import run_title

        return "\n\n".join(texts) if texts else (f"{run_title(reg, ev)} is done." if changed else f"{run_title(reg, ev)} was already {ev.state}.")

    def _end_busy(self, sessions) -> None:
        """End refuses while a judge is in flight on one of the run's councils: its merge would write the archived
        session back (and a restart would load it as live)."""
        st = self._loot_state()
        for s in sessions:
            f = st["flight"].get(s.key) or s.distributing
            if f:
                raise LootBusy(f"Distributing already ({f.get('by', '?')}, {f.get('clock') or self.loot_clock(self.loot_reg(s), f.get('since'))}) — wait for it, or retry End.")

    def loot_closing_state(self, reg, rs, ev, now: datetime | None = None) -> dict | None:
        """The scheduler's view of a council on a run past its close: {'state': 'open' | 'held' | 'closing' | 'end',
        'until', 'notice_due', 'pending', 'idle'}. None when the run has no council or isn't past close yet.
        'open' = used in the last IDLE_END_MIN minutes, or an officer pressed Keep open; 'held' = idle, but a pick waits
        for Confirm or a drop nobody judged (LootSession.blocks_close) — never auto-ended, one "still pending" line
        (`notice_due`) and it waits for an officer; 'closing' = the one "closing in 15 min" notice is (due to be) out and
        the council ends at `until` unless someone uses it. A reason still owed or an in-game conflict does not hold the
        council open: both outlive it (set_reason / correct by run key)."""
        now = now or datetime.now(timezone.utc)
        sessions = [s for s in self.events.values() if s.guild == reg.key and s.run_key == ev.key]
        if not sessions or now < rc.run_close_at(reg, ev):
            return None
        last = max((_utc(s.last_activity) or now) for s in sessions)
        pending = any(s.blocks_close() for s in sessions)
        idle = now - last >= timedelta(minutes=IDLE_END_MIN)
        notices = [_utc(s.closing_notice_at) for s in sessions if s.closing_notice_at]
        notice = max(notices) if notices else None
        held = [t for t in (_utc(s.keep_open_until) for s in sessions) if t is not None]
        if not idle or (notice is None and held and max(held) > now):
            return {"state": "open", "until": None, "notice_due": False, "pending": pending, "idle": idle}
        if pending:
            return {"state": "held", "until": None, "notice_due": not any(s.held_notice_at for s in sessions), "pending": True, "idle": True}
        if notice is None or notice < last:  # no notice yet, or the council was used after it
            return {"state": "closing", "until": None, "notice_due": True, "pending": False, "idle": True}
        until = notice + timedelta(minutes=CLOSING_NOTICE_MIN)
        return {"state": "end" if now >= until else "closing", "until": until.isoformat(timespec="seconds"), "notice_due": False, "pending": False, "idle": True}

    async def loot_may_close(self, reg, rs, ev, now: datetime | None = None) -> bool:
        """The scheduler, past a run's close time: True = close it now (no council, or the council just ended); False =
        a council is in use / holds a pick or drop nobody decided (one "still pending" line, then it waits for an
        officer's Confirm, Discard or End) / just got its one "closing in 15 min" notice. Never auto-ends a council with
        a pick pending; the scheduler's fill engine and confirmations stop at the close either way."""
        st = self.loot_closing_state(reg, rs, ev, now)
        if st is None:
            return True
        if st["state"] == "open":
            return False
        stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        sessions = [s for s in self.events.values() if s.guild == reg.key and s.run_key == ev.key]
        if st["state"] == "held":
            if st["notice_due"]:
                for s in sessions:
                    async with self.loot_lock(*s.key):
                        s.held_notice_at = stamp
                        s.save(f"{reg.key}: loot {s.id}: still pending past the run's close", touch=False)  # not activity
                    if s.blocks_close():
                        await self.loot_held(reg, s)
            return False
        if st["notice_due"]:
            for s in sessions:
                async with self.loot_lock(*s.key):
                    s.closing_notice_at = stamp
                    s.keep_open_until = None
                    s.save(f"{reg.key}: loot {s.id}: closing in {CLOSING_NOTICE_MIN} min", touch=False)  # the notice is not activity
                await self.loot_closing(reg, s)
            return False
        if st["state"] == "end":
            try:
                await self.end_run(reg, rs, ev, by="scheduler", refresh=False)  # _close_run redraws the sheet
            except LootBusy:
                return False  # a judge started meanwhile: the next minute
            return True
        return False

    # ------------------------------------------------------------ restarts and the roster hook
    async def loot_restore(self) -> list[str]:
        """On ready: a judge that was in flight when the bot stopped is cleared and remembered as interrupted (never
        re-run: "Distribute was interrupted at 8:12 PM — press Distribute"; the same drops interrupted twice judge by
        score only next time); ledger rows a crash left behind are derived again (by import_id)."""
        out = []
        for s in list(self.events.values()):
            reg = self.loot_reg(s)
            if s.distributing:
                fkey = ",".join(sorted(s.distributing.get("drops") or []))
                s.failures[fkey] = s.failures.get(fkey, 0) + 1
                s.interrupted = {**s.distributing, "at": now_iso()}
                s.distributing = None
                s.save(f"{s.guild}: loot {s.id}: distribute interrupted by a restart")
                line = f"⚠ Distribute was interrupted at {self.loot_clock(reg, s.interrupted.get('since'))} — press Distribute to judge the {len(s.interrupted.get('drops') or [])} drop(s) again."
                out.append(line)
                await self.loot_notify(reg, s, line, kind="interrupted")
            if s.thread_id:
                self._loot_state()["threads"][s.thread_id] = s.key
            try:
                if not s.test and s.awards:
                    ctx = self.loot_ctx(s)
                    n = ls.sync_ledger(ctx, s)
                    if n:
                        s.store().commit(f"{s.guild}: loot {s.id}: {n} ledger row(s) restored")
                        out.append(f"{s.id}: {n} ledger row(s) restored")
            except RuntimeError:
                pass
        for gk in list((getattr(self.registries, "by_key", {}) or {})):
            lost = await self.feed_expire(gk)  # held across the restart: still fresh ones wait for a council
            if lost:
                out.append(f"{gk}: {len(lost)} held companion event(s) expired")
        made = await self.loot_threads_pending()
        if made:
            out.append(f"{made} pending loot thread(s) created")
        return out

    def attach_loot_listeners(self) -> None:
        """rc.ROSTER_LISTENERS → loot_roster_changed on the loop (seat changes may happen in a worker thread)."""
        def on_change(guild_key: str, run_key: str, change: dict) -> None:
            if not any(s.guild == guild_key and s.run_key == run_key for s in list(self.events.values())):
                return
            loop = getattr(self, "loop", None)
            try:
                loop.call_soon_threadsafe(lambda: self._spawn(self.loot_roster_changed(guild_key, run_key, change)))
            except Exception:  # noqa: BLE001 — loop not running (offline tools)
                pass
        on_change.__name__ = "loot_on_roster_change"
        rc.ROSTER_LISTENERS[:] = [f for f in rc.ROSTER_LISTENERS if getattr(f, "__name__", "") != on_change.__name__] + [on_change]

    async def loot_roster_changed(self, guild_key: str, run_key: str, change: dict) -> None:
        """A seat changed on a run with a council: a newcomer is a candidate at once (read live); someone who left
        stays a candidate (`departed`) until an officer removes them. A split run: only the council of the roster the
        seat is on (added) or was freed from (`change["roster_of"]`, free_seat) hears of it — never every roster's."""
        reg = (getattr(self.registries, "by_key", {}) or {}).get(guild_key)
        freed_from = {str(k): v for k, v in (change.get("roster_of") or {}).items()}
        for s in [s for s in list(self.events.values()) if s.guild == guild_key and s.run_key == run_key]:
            rs, ev = self.loot_run(s)
            if ev is None:
                continue
            lines = []
            async with self.loot_lock(*s.key):
                if self.events.get(s.key) is not s:
                    continue
                for uid in change.get("added", []) + change.get("changed", []):
                    hit = ev.seat_of(uid)
                    if hit is None or (s.roster_index is not None and hit[0] != s.roster_index):
                        continue
                    p = hit[1]
                    s.departed = [x for x in s.departed if x.discord_id != p.discord_id]
                    s.roster_notes.append({"at": now_iso(), "kind": "added" if uid in change.get("added", []) else "changed", "uid": uid, "name": p.character or p.signup_name})
                    lines.append(f"➕ {p.character or p.signup_name} rostered — now a candidate")
                for uid in change.get("removed", []):
                    i = freed_from.get(str(uid))
                    if s.roster_index is not None and i is not None and i != s.roster_index:
                        continue  # left another roster of the split: never a candidate here
                    p = next((x for x in ev.all_rosters[0].benched if rc.player_uid(ev, x) == uid or x.signup_name == uid), None) if ev.all_rosters else None
                    if p is None or any(x.discord_id == p.discord_id and x.signup_name == p.signup_name for x in s.departed):
                        continue
                    s.departed.append(p.model_copy())
                    s.roster_notes.append({"at": now_iso(), "kind": "removed", "uid": uid, "name": p.character or p.signup_name})
                    lines.append(f"➖ {p.character or p.signup_name} left the roster — still a candidate?")
                if lines:
                    s.roster_notes = s.roster_notes[-50:]
                    s.save(f"{guild_key}: loot {s.id}: roster changed")
            for ln in lines:
                await self.loot_notify(reg, s, ln, kind="roster")

    # ------------------------------------------------------------ auto-distribute (decision 6)
    def loot_auto_on(self, reg, session: LootSession) -> bool:
        if reg is None:
            return False
        return parse_bool(reg.raid_def(session.instance).get("loot_auto_distribute", True))

    def loot_auto_soon(self, reg, session: LootSession, boss: str | None) -> None:
        """A companion drop / kill: Distribute that boss's ticked drops AUTO_DISTRIBUTE_S after its last drop (each new
        drop of the boss restarts the wait). Per raid `loot_auto_distribute` (default on)."""
        if not self.loot_auto_on(reg, session):
            return
        auto = self._loot_state()["auto"]
        k = (session.guild, session.id, boss or "?")
        old = auto.get(k)
        if old is not None and not old.done():
            old.cancel()
        auto[k] = self._spawn(self._auto_distribute(reg, session, boss or "?"))

    async def _auto_distribute(self, reg, session: LootSession, boss: str) -> None:
        for _ in range(10):
            await asyncio.sleep(self.AUTO_DISTRIBUTE_S)
            if session.key not in self.events:
                return
            todo = [d.drop_id for d in session.undistributed() if d.boss == boss or boss == "?"]
            if not todo:
                return
            try:
                await self.distribute(reg, session, by="auto", only=todo)
                return
            except LootBusy:
                continue  # another judge is running: try again after the next wait
            except RegistryError as e:
                log.info("auto-distribute skipped: %s", e)
                return

    # ------------------------------------------------------------ companion buffering (no run live)
    # Held events live in the data repo (<guild>/loot/held.json), not in process memory: the companion drops its copy
    # on the ack, so a restart must not lose them. An event no council took within FEED_BUFFER_S is never dropped
    # silently — the ops channel gets what was NOT recorded, so an officer can record it by hand.
    @staticmethod
    def _held_rel(guild_key: str) -> Path:
        return Path(guild_key) / "loot" / "held.json"

    def _held_reg(self, guild_key: str):
        return (getattr(self.registries, "by_key", {}) or {}).get(guild_key)

    def _held_rows(self, guild_key: str) -> list[dict]:
        reg = self._held_reg(guild_key)
        if reg is None:
            return list(self._loot_state()["buffer"].get(guild_key) or [])
        p = reg.store.root / self._held_rel(guild_key)
        try:
            rows = json.loads(p.read_text()) if p.exists() else []
        except (OSError, ValueError):
            log.warning("%s: unreadable held companion events", guild_key)
            return []
        return rows if isinstance(rows, list) else []

    def _held_write(self, guild_key: str, rows: list[dict], message: str) -> None:
        reg = self._held_reg(guild_key)
        if reg is None:
            self._loot_state()["buffer"][guild_key] = list(rows)
            return
        reg.store.write_text(self._held_rel(guild_key), json.dumps(rows, indent=1, default=str))
        reg.store.commit(f"{guild_key}: {message}")

    @staticmethod
    def _held_split(rows: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
        fresh, old = [], []
        for r in rows:
            t = _utc(r.get("at"))
            (fresh if t is not None and now - t < timedelta(seconds=FEED_BUFFER_S) else old).append(r)
        return fresh, old

    def feed_buffer(self, guild_key: str, ev: dict, comp) -> None:
        """Hold a companion event that arrived with no live council (persisted; replayed when one opens). The row keeps
        the guild it was routed to, when it arrived (`at`) and — when a council of the guild ended within
        FEED_AFTER_END_S — that ended run (`after_end`): the master looter handing out the last item after End belongs to
        that run, never to the next council that opens. A replay re-holding a refused event writes nothing here (the
        replay keeps the original row with its original time)."""
        if _REPLAYING.get():
            return
        row = {"at": now_iso(), "guild": guild_key, "ev": ev, "comp": {"character": getattr(comp, "character", "?"), "client": getattr(comp, "client", "?"),
                                                                        "guild": getattr(comp, "named", getattr(comp, "guild", "")) or "", "peer": getattr(comp, "peer", "held"),
                                                                        "bound": getattr(comp, "bound", None)}}
        ended = self.loot_recent_end(guild_key, FEED_AFTER_END_S)
        if ended is not None:
            row["after_end"] = ended
        self._held_write(guild_key, self._held_rows(guild_key) + [row], f"companion event held (no run live): {ev.get('type')}")
        try:
            self._spawn(self._feed_expire_later(guild_key))
        except RuntimeError:  # no running loop (offline tools): the next ready / replay sweeps it
            pass

    def loot_recent_end(self, guild_key: str, within_s: float) -> str | None:
        """The run key of this guild's council that ended most recently, within `within_s` (from the archives' end
        summaries, so it survives a restart), or None."""
        reg = self._held_reg(guild_key)
        if reg is None:
            return None
        arch = reg.store.root / guild_key / "events" / "archive"
        if not arch.is_dir():
            return None
        now = datetime.now(timezone.utc)
        best: tuple[datetime, str] | None = None
        for f in arch.glob("*.json"):
            try:
                if now.timestamp() - f.stat().st_mtime > within_s + 60:
                    continue
                body = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            t = _utc((body.get("summary") or {}).get("ended_at"))
            if body.get("origin") != "raid" or t is None or (now - t).total_seconds() > within_s:
                continue
            if best is None or t > best[0]:
                best = (t, str(body.get("run_key") or _run_of(str(body.get("id")))))
        return best[1] if best else None

    async def _feed_expire_later(self, guild_key: str) -> None:
        await asyncio.sleep(FEED_BUFFER_S + 1)
        await self.feed_expire(guild_key)

    def feed_waiting(self, reg) -> dict | None:
        """'No run live' state for the loot card / site: drops arriving with nowhere to go (held 10 minutes)."""
        fresh, _ = self._held_split(self._held_rows(reg.key), datetime.now(timezone.utc))
        if not fresh:
            return None
        return {"count": len(fresh), "since": fresh[0]["at"], "characters": sorted({(r.get("comp") or {}).get("character", "?") for r in fresh})}

    async def feed_expire(self, guild_key: str) -> list[dict]:
        """Held events past FEED_BUFFER_S leave the file and the ops channel says what was NOT recorded (a warn line,
        no DM). Returns the expired rows."""
        async with self.loot_lock(guild_key, "__held__"):
            rows = self._held_rows(guild_key)
            fresh, old = self._held_split(rows, datetime.now(timezone.utc))
            if not old:
                return []
            self._held_write(guild_key, fresh, f"{len(old)} held companion event(s) expired with no council taking them")
        await self._feed_announce_lost(guild_key, old)
        return old

    async def _feed_announce_lost(self, guild_key: str, rows: list[dict]) -> None:
        def what(ev: dict) -> str:
            k = ev.get("type")
            if k == "drop":
                items = ", ".join(str(it.get("name") or it.get("id")) for it in ev.get("items") or [])
                return f"{items} dropped" + (f" ({ev['boss']})" if ev.get("boss") else "")
            if k == "loot":
                return f"{ev.get('item_name') or ev.get('item_id')} → {ev.get('recipient', '?')}"
            if k == "kill":
                return f"{ev.get('boss', '?')} down"
            return str(k)

        def row_what(r: dict) -> str:
            ended = r.get("after_end")
            return what(r.get("ev") or {}) + (f" (after {ended}'s council ended — record it on that run)" if ended else "")
        chars = ", ".join(sorted({(r.get("comp") or {}).get("character", "?") for r in rows}))
        text = (f"companion events from {chars}'s log waited {FEED_BUFFER_S // 60} min with no loot council taking them and were NOT recorded: "
                + "; ".join(row_what(r) for r in rows) + " — record them by hand (the run's loot council, or the site).")
        cfg_for = getattr(self.registries, "cfg_for", None)
        cfg = cfg_for(guild_key) if cfg_for else None
        if cfg is not None and getattr(self, "ops", None) is not None:
            await self.ops.emit(cfg, "warn", text[:1800])
        else:
            log.warning("[%s] %s", guild_key, text)

    def _held_for_run(self, reg, row: dict, ev) -> str | None:
        """Why a held row does NOT belong to the run whose council is opening (None: it does): it was held after
        another run's council ended (`after_end`), or it arrived outside this run's window (its start minus
        FEED_LEAD_S … its close). Such a row stays held — another council may take it — and is announced when it expires."""
        if ev is None:
            return None
        ended = row.get("after_end")
        if ended and ended != ev.key:
            return f"held after {ended}'s council ended"
        t = _utc(row.get("at"))
        if t is None or t < ev.start - timedelta(seconds=FEED_LEAD_S) or t > rc.run_close_at(reg, ev):
            return f"outside {ev.key}'s run window"
        return None

    async def feed_replay(self, reg, ev=None) -> int:
        """A council opened for run `ev`: the companion events held for this guild that belong to it go through again
        (oldest first). The file is rewritten AFTER the replay with what was not applied — an event the council refused
        (a test council, several councils with the companion seated on none) or held again stays held with its original
        time, and one for another run (`_held_for_run`) is left for that run's council — so nothing is lost: what
        expires unrecorded is announced in the ops channel. Rows past the hold are announced now."""
        async with self.loot_lock(reg.key, "__held__"):  # one replay at a time per guild (an event without idem applies once)
            rows = self._held_rows(reg.key)
            if not rows:
                return 0
            fresh, old = self._held_split(rows, datetime.now(timezone.utc))
            seen = {json.dumps(r, sort_keys=True, default=str) for r in rows}
            from .feed import Companion

            handle = getattr(self, "handle_feed_event", None)
            keep: list[dict] = []
            n = 0
            for r in fresh:
                if handle is None or self._held_for_run(reg, r, ev) is not None:
                    keep.append(r)
                    continue
                c = r.get("comp") or {}
                comp = Companion(c.get("character", "?"), c.get("client", "?"), c.get("guild", ""), c.get("peer", "held"), bound=c.get("bound"))
                token = _REPLAYING.set(True)
                try:
                    out = await handle(r.get("ev") or {}, comp)
                except Exception:  # noqa: BLE001
                    log.exception("held companion event failed")
                    out = {"error": "failed"}
                finally:
                    _REPLAYING.reset(token)
                if not isinstance(out, dict) or out.get("error") or out.get("held"):
                    keep.append(r)  # refused or nowhere to go: still held, never dropped
                else:
                    n += 1
            arrived = [x for x in self._held_rows(reg.key) if json.dumps(x, sort_keys=True, default=str) not in seen]  # held meanwhile
            self._held_write(reg.key, keep + arrived, f"{n} held companion event(s) replayed, {len(keep)} still held")
        if old:
            await self._feed_announce_lost(reg.key, old)
        return n

    # ------------------------------------------------------------ the thread's buttons (interim: the Discord surfaces replace them)
    def loot_view(self, reg, session: LootSession, kind: str, drop_id: str | None = None):
        """The buttons a thread line carries (None: none): Give the reason + Mistake — correct… under a companion
        mismatch, Correct… under a conflict or an in-game award, Distribute under ticked drops, Keep open under the
        closing notice."""
        if session.origin != "raid":
            return None
        try:
            if kind == "mismatch" and drop_id:
                return LootDropView(self, session, drop_id, reason=True)
            if kind in ("conflict", "manual") and drop_id:
                return LootDropView(self, session, drop_id, reason=False)
            if kind == "closing":
                return LootKeepOpenView(self, session)
            if kind == "tick" and session.undistributed():
                from .discord_bot import DistributeView

                return DistributeView(self, session)
        except RuntimeError:  # a View needs a running loop (offline tools)
            return None
        return None

    # ------------------------------------------------------------ helpers for callers
    @staticmethod
    def loot_revs(session: LootSession) -> dict[str, str]:
        return {"batch_rev": ls.batch_rev(session), "drops_rev": ls.drops_rev(session)}

    def loot_state_json(self, session: LootSession) -> dict[str, Any]:
        """The session as the surfaces read it (the web's /api/loot is L2/L3's; this is the shared shape): drops with
        their state, pending proposals (effective recipient, override), awards, pending reasons, revs, flags."""
        reg = self.loot_reg(session)
        return {
            "id": session.id, "run": session.run_key, "roster_index": session.roster_index, "label": session.label,
            "guild": session.guild, "test": session.test, "live": self.loot_live(session),
            "thread_id": session.raid_thread_id, "drops": [{**d.model_dump(), "state": session.drop_state(d)} for d in session.drop_list],
            "proposals": [{"drop_id": p.drop_id, "item": p.item_name, "boss": p.boss, "bot": p.award_to, "to": p.to, "source": p.source,
                           "override": p.override.model_dump() if p.override else None, "batch": p.batch, "tiered": p.tiered,
                           "close_call": p.result.recommendation.close_call, "order_sensitive": p.order_sensitive, "judged_by": p.result.source}
                          for p in session.proposals],
            "awards": [a.model_dump(mode="json") for a in session.awards],
            "pending_reasons": list(session.pending_reasons), "conflicts": list(session.conflicts),
            "departed": [p.character or p.signup_name for p in session.departed],
            "distributing": session.distributing, "interrupted": session.interrupted, "scores_only": session.scores_only,
            "closing": {"notice_at": session.closing_notice_at, "until": session.keep_open_until, "held_notice_at": session.held_notice_at},
            "feed_waiting": self.feed_waiting(reg) if reg is not None else None,
            **self.loot_revs(session),
        }


# ---------------------------------------------------------------- the thread's buttons (interim)
# Non-persistent Views (timeout=None, like the boss tables' Distribute and the proposals' Confirm): a restart drops them;
# /raid loot on an open council re-posts every pending line with fresh buttons (LootMixin.loot_repost). The Discord
# surfaces (L2) replace them with persistent buttons and lootmodal ids.

async def _officer_press(bot, interaction: discord.Interaction, reg) -> bool:
    """Writes need an officer or the owner, decided live (PRIV_LIVE) by THE identity predicate."""
    if reg is None:
        gate = getattr(bot, "gate", None)
        return bool(gate and await gate(interaction))
    from . import guild_routing

    role = await bot.role_of(reg, interaction.user, max_age=guild_routing.PRIV_LIVE)
    if role in ("owner", "officer"):
        return True
    await interaction.response.send_message("Officers only.", ephemeral=True)
    return False


class _LootTarget:
    """A button bound to (guild, session id, drop): the press finds the live council, or — once it ended — passes the
    session id, which set_reason and correct take for an ended council."""

    def __init__(self, bot, session: LootSession, drop_id: str | None = None):
        self.bot, self.gk, self.sid, self.drop_id = bot, session.guild, session.id, drop_id

    @property
    def reg(self):
        return (getattr(self.bot.registries, "by_key", {}) or {}).get(self.gk)

    def target(self):
        return self.bot.events.get((self.gk, self.sid)) or self.sid


class LootReasonModal(discord.ui.Modal, title="Why did it go to them?"):
    reason = discord.ui.TextInput(label="The reason (becomes the precedent)", style=discord.TextStyle.paragraph,
                                  min_length=ls.REASON_MIN, max_length=ls.REASON_MAX)

    def __init__(self, t: _LootTarget, replace: bool = False):
        super().__init__()
        self.t, self.replace = t, replace

    async def on_submit(self, interaction: discord.Interaction):
        await _submit_reason(self.t, interaction, str(self.reason.value), self.replace)


async def _submit_reason(t: _LootTarget, interaction: discord.Interaction, reason: str, replace: bool) -> None:
    from . import configops

    try:
        line = await t.bot.set_reason(t.reg, t.target(), t.drop_id, reason, by=interaction.user.display_name, by_id=interaction.user.id, replace=replace)
    except LootChanged as e:  # someone already gave a reason: replace it only on a second, deliberate press
        await interaction.response.send_message(configops.user_error(e), view=LootReplaceView(t, reason), ephemeral=True)
        return
    except RegistryError as e:
        await interaction.response.send_message(configops.user_error(e), ephemeral=True)
        return
    await interaction.response.send_message(line[:1900], ephemeral=True)


class LootReplaceView(discord.ui.View):
    def __init__(self, t: _LootTarget, reason: str):
        super().__init__(timeout=300)
        self.t, self.reason = t, reason

    @discord.ui.button(label="Replace with my reason", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _officer_press(self.t.bot, interaction, self.t.reg):
            return
        await _submit_reason(self.t, interaction, self.reason, True)
        self.stop()


class LootCorrectModal(discord.ui.Modal, title="Correct the record"):
    to = discord.ui.TextInput(label="Record it to (a character, or Disenchant)", max_length=NAME_MAX)
    reason = discord.ui.TextInput(label="What went wrong", style=discord.TextStyle.paragraph, min_length=ls.REASON_MIN, max_length=ls.REASON_MAX)

    def __init__(self, t: _LootTarget):
        super().__init__()
        self.t = t

    async def on_submit(self, interaction: discord.Interaction):
        from . import configops

        try:
            line = await self.t.bot.correct(self.t.reg, self.t.target(), self.t.drop_id, str(self.to.value), str(self.reason.value),
                                            by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await interaction.response.send_message(configops.user_error(e), ephemeral=True)
            return
        await interaction.response.send_message(line[:1900], ephemeral=True)


class LootDropView(discord.ui.View):
    """Under an award the companion recorded: Give the reason (→ the precedent, bound to this drop and its recipient)
    and/or Mistake — correct… (→ void + a new award, no precedent)."""

    def __init__(self, bot, session: LootSession, drop_id: str, reason: bool = True):
        super().__init__(timeout=None)
        self.t = _LootTarget(bot, session, drop_id)
        if reason:
            b = discord.ui.Button(label="Give the reason", style=discord.ButtonStyle.primary)
            b.callback = self._reason
            self.add_item(b)
        c = discord.ui.Button(label="Mistake — correct…" if reason else "Correct…", style=discord.ButtonStyle.secondary)
        c.callback = self._correct
        self.add_item(c)

    async def _reason(self, interaction: discord.Interaction):
        if await _officer_press(self.t.bot, interaction, self.t.reg):
            await interaction.response.send_modal(LootReasonModal(self.t))

    async def _correct(self, interaction: discord.Interaction):
        if await _officer_press(self.t.bot, interaction, self.t.reg):
            await interaction.response.send_modal(LootCorrectModal(self.t))


class LootKeepOpenView(discord.ui.View):
    """Under the scheduler's "closing in 15 min" line."""

    def __init__(self, bot, session: LootSession):
        super().__init__(timeout=None)
        self.t = _LootTarget(bot, session)

    @discord.ui.button(label="Keep open", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await _officer_press(self.t.bot, interaction, self.t.reg):
            return
        s = self.t.target()
        if isinstance(s, str):
            await interaction.response.send_message("This council has ended.", ephemeral=True)
            return
        line = await self.t.bot.keep_open(self.t.reg, s, by=interaction.user.display_name, by_id=interaction.user.id)
        await interaction.response.send_message(line, ephemeral=True)
