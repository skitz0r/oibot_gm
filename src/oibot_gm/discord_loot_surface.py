"""LootSurfaceMixin: the loot council's Discord surface (dual interfaces phase 1, design §3.1 decisions 5, 6, 8, 9).

The verbs are LootMixin's (discord_loot.py); this mixin replaces the hooks they call after every write, so the thread
always shows the state the verbs left:

  · the thread: a PUBLIC thread on the officer channel (roster channel, else ops — refused when neither is set), started
    from a one-line "⚔ Loot council · <run> → #thread" message in that channel (edited to the summary at End); its
    first message is the SESSION CARD (raid_views.loot_layout: roster groups one member per line with the spec icon
    alone, drops per boss, the status line with the companion chip, what still needs an officer, Tick drops… ·
    Distribute · Scores only · End council), pinned, edited in place after every write from any surface;
  · the PROPOSAL CARDS (raid_views.batch_layout: ≤5 items per message, grouped by boss, Confirm as each item's
    accessory, Override… · Decide later · Details under it, Confirm the rest (N) · Discard batch on the last card):
    edited in place after every write; re-posted at the bottom (the old ones stubbed) when a pick appears that no card
    shows, so the live controls are always on the newest messages;
  · thread LINES for what happened (ticks, kills, companion awards, corrections), with the buttons a line needs
    (Give the reason · Mistake — correct… · Correct… · Distribute · Keep open), all persistent LootButtons;
  · the end: the summary in the thread, the session card's final state, the starter line edited, the thread archived.

The council sends NO DMs (the DM rule), except one: a companion award arriving after the council ended DMs the officers
once with a link to correct or record it — they must act. The scheduler's loot steps (the "Start an ad hoc run for
tonight?" prompt for drops arriving with no run live, that DM) are `loot_scheduler_step`. The addressed-only thread chat
(@mention, a leading "?", a reply to a card) is `loot_addressed` / `loot_chat`: a question goes to help_answer with the
proposals in a <data> block; a sentence that asks for an override or a confirm becomes `loot` ConfigOps shown as a
diff with Apply (LootChatConfirmView, the plain-text pipeline's ConfigConfirmView: describe → authorize → apply_async)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import discord

from .loot import session as ls
from .loot.session import DISENCHANT, LootSession
from .registry import RegistryError

log = logging.getLogger(__name__)

TICK_S = 4.0  # the session card's judging ticker
AFTER_END_H = 6  # a companion award this long after a council ended DMs the officers (once)
FRESH_CARD_S = 60  # a session card posted this recently is not re-posted by loot_repost (a thread created late)
LLM_DOWN = "The bot can't read sentences right now — use the buttons on the cards."


def _utc(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def loot_site_url(reg, run_key: str | None = None) -> str | None:
    """The council on the site's Loot page (Tonight / History), in this guild."""
    web = os.environ.get("OIBOT_WEB_URL", "")
    if not web.startswith("http"):
        return None
    return f"{web}/app/loot?g={reg.key}" + (f"#run-{run_key}" if run_key else "")


def loot_digest(session: LootSession, *, numbered: bool = False) -> str:
    """The council as data for the model (help_answer's <data> block, the override parser): one line per pending pick
    (with its drop number as `item_id` when `numbered` — the parser answers with it, so two copies or two unknown items
    never collide), what was recorded this session, what still needs a reason. Names and reasons are member text:
    the caller puts the whole thing in a delimited block."""
    out = []
    for p in session.proposals:
        r = p.result
        top = "; ".join(f"{c.character} ({c.cls} {c.spec}, {c.tier}, {c.base_score:.2f}{', has it' if c.already_has else ''})" for c in r.candidates[:4])
        why = (r.recommendation.justification or "").split(". ")[0][:160]
        head = f"- item_id {int(p.drop_id[1:]) if p.drop_id[1:].isdigit() else 0} = drop {p.drop_id}" if numbered else f"- drop {p.drop_id}"
        ov = f" (override by {p.override.by}: {p.override.reason}; bot picked {p.award_to})" if p.override else " (bot pick)"
        out.append(f"{head}: {p.item_name} [{p.boss}] → {p.to}{ov} · top: {top} · why: {why}")
    for a in session.awards:
        if not a.void:
            out.append(f"- recorded: {a.item_name} → {a.raider} ({a.source}, by {a.by})")
    for x in session.pending_reasons:
        out.append(f"- waiting for a reason: {x.get('item')} → {x.get('recipient')} in game (bot picked {x.get('bot')})")
    for d in session.undistributed():
        out.append(f"- ticked, not judged yet: {d.item_name} [{d.boss}]")
    return "\n".join(out) or "(nothing ticked yet)"


BUDGET_WARN = 0.9  # L36: the thread is told once when this much of the month's loot budget is spent


def loot_budget_note(provider) -> str | None:
    """'⚠ 92% of this month's loot budget is spent …' when the provider's month (overall, or the loot judge's own cap)
    is at BUDGET_WARN or past it; None otherwise (or with no provider / no budget)."""
    try:
        overall = provider.cost_usd / provider.budget_usd if provider.budget_usd else 0.0
        cap = provider.cap_for("loot_recommend")
        own = provider.spent("loot_recommend") / cap if cap else 0.0
    except Exception:  # noqa: BLE001 — no provider, or a stand-in without a budget
        return None
    frac = max(overall, own)
    if frac < BUDGET_WARN:
        return None
    return (f"⚠ {min(100, round(frac * 100))}% of this month's LLM budget for loot is spent — when it runs out, picks go by score "
            "alone (each says so); Scores only on the card saves what is left.")


class LootSurfaceMixin:
    """Mixed into OibotGM before LootMixin: its hooks replace LootMixin's defaults for every council (origin raid —
    the only kind loaded since /mock was retired; the origin guards only keep a stray old object on the defaults)."""

    def _surface(self) -> dict:
        return self.__dict__.setdefault("_loot_surface", {"judging": {}, "prompts": {}, "after_end": set(), "fresh": {}, "reasons": {}})

    # ------------------------------------------------------------ where things are
    async def loot_thread(self, session: LootSession):
        """The council's thread (the gateway cache, else one fetch), or None."""
        tid = session.raid_thread_id
        if not tid:
            return None
        get = getattr(self, "get_channel", None)
        ch = get(tid) if get else None
        if ch is None and hasattr(self, "fetch_channel"):
            try:
                ch = await self.fetch_channel(tid)
            except Exception:  # noqa: BLE001 — gone or unreachable: the next write tries again
                return None
        return ch

    async def _loot_edit(self, ch, mid: int | None, **kw) -> bool:
        if ch is None or not mid:
            return False
        try:
            msg = ch.get_partial_message(mid) if hasattr(ch, "get_partial_message") else await ch.fetch_message(mid)
            await msg.edit(**kw)
            return True
        except Exception as e:  # noqa: BLE001 — the write happened; a card that can't be edited is redrawn next time
            log.warning("loot card edit failed (%s): %s", mid, e)
            return False

    async def _loot_cards_save(self, session: LootSession, update: dict, drop: list[str] = (), message: str = "cards") -> None:
        async with self.loot_lock(*session.key):
            for k in drop:
                session.cards.pop(k, None)
            session.cards.update(update)
            if self.events.get(session.key) is not session:
                return  # the council ended (archived): never write its live file again — a restart would load it as live
            session.save(f"{session.guild}: loot {session.id}: {message}", touch=False)  # card ids are not activity

    def loot_presence(self, reg, session: LootSession) -> list[dict]:
        """The companions feeding this council (feed_presence rows routed here; else the guild's, when it has any)."""
        from .discord_feed import feed_presence

        try:
            rows = feed_presence(self, reg)
        except Exception:  # noqa: BLE001
            return []
        mine = [r for r in rows if r.get("routes_to") in (session.id, f"{session.guild}:{session.id}")]
        return mine or rows

    def loot_card_view(self, reg, session: LootSession, ev=None):
        from .raid_views import loot_layout, loot_status_text

        if ev is None:
            _, ev = self.loot_run(session)
        presence = self.loot_presence(reg, session)
        auto = self.loot_auto_on(reg, session)
        provider = getattr(getattr(self, "ctx", None), "provider", None) is not None or getattr(self, "loot_provider", None) is not None
        status = loot_status_text(reg, session, ev, judging=self._surface()["judging"].get(session.key), provider=provider, auto=auto, presence=presence)
        live = any(r.get("state") == "live" for r in presence)
        by_hand = any(d.source != "companion" for d in session.undistributed())
        unrouted = self.loot_unrouted(reg.key) if hasattr(self, "loot_unrouted") and self.loot_live(session) else []
        return loot_layout(reg, ev, session, self.ico, status=status, show_distribute=not (auto and live) or by_hand, unrouted=unrouted)

    async def loot_unrouted_changed(self, reg, character: str | None = None) -> None:
        """A companion several live councils could take (FeedMixin.loot_route refused it): every live council card of
        the guild offers "Bind companion…" — redrawn once per companion, not on each of its events."""
        shown = self._surface().setdefault("unrouted_shown", set())
        if character is not None:
            if (reg.key, character.lower()) in shown:
                return
            shown.add((reg.key, character.lower()))
        for s in list(self.events.values()):
            if s.origin == "raid" and s.guild == reg.key and self.loot_live(s):
                await self.loot_card_refresh(reg, s)

    async def loot_bind_companion(self, reg, session: LootSession, character: str, *, by: str, by_id: int | None = None) -> str:
        """The card's Bind companion…: that companion's events go to this council from now on (until a restart). A
        thread line, an ops line; every council card of the guild is redrawn (the select leaves the others)."""
        from . import ops as opsmod
        from .raid_views import loot_label

        if not self.loot_live(session):
            raise RegistryError("This council isn't live any more — bind the companion on tonight's card.")
        if not self.loot_bind(reg.key, session.id, character):
            raise RegistryError(f"{character}'s companion isn't waiting to be bound any more.")
        self._surface().setdefault("unrouted_shown", set()).discard((reg.key, character.lower()))
        line = f"🔌 {character}'s companion now feeds this council (bound by {by})"
        await opsmod.audited(self.ops, reg, by, f"loot {session.run_key}: {character}'s companion bound to {loot_label(session) or 'its council'}", via="discord", by_id=by_id)
        await self.loot_notify(reg, session, line, kind="presence")
        await self.loot_unrouted_changed(reg)
        return line

    # ------------------------------------------------------------ hooks: open
    async def loot_open_thread(self, reg, rs, ev, session: LootSession):
        """A public thread on the officer channel (roster, else ops; refused when neither is set — the lock cards'
        rule), started from a one-line message in that channel; the session card is its first message, pinned. Called
        under the session lock by open_loot: the card ids are set on the session, which open_loot saves."""
        if session.origin != "raid":
            return await super().loot_open_thread(reg, rs, ev, session)
        cfg = reg.config
        if not (cfg.roster_channel_id or cfg.ops_channel_id):
            raise RegistryError("Set a roster or ops channel first — the loot council's thread goes there (/gm config roster-channel).")
        ch = self.officer_channel(reg, ev)
        if ch is None:
            return None  # configured but unreachable (Discord down): the council opens, its thread is pending
        from .raid_views import guild_prefix, loot_label, run_label, run_title

        label = loot_label(session)
        starter = await ch.send(f"{guild_prefix(reg)}⚔ Loot council · {label}{run_label(reg, ev)} — opening…")
        thread = await starter.create_thread(name=(("rehearsal · " if session.test else "loot · ") + label + run_title(reg, ev))[:100])
        session.cards["channel"] = ch.id
        session.cards["starter"] = starter.id
        try:
            await starter.edit(content=f"{guild_prefix(reg)}⚔ Loot council · {label}{run_label(reg, ev)} → <#{thread.id}>")
        except Exception as e:  # noqa: BLE001
            log.debug("loot starter edit: %s", e)
        await self._loot_post_card(reg, session, thread, ev)
        return thread.id

    async def _loot_post_card(self, reg, session: LootSession, thread, ev=None) -> int | None:
        try:
            card = await thread.send(view=self.loot_card_view(reg, session, ev))
        except Exception as e:  # noqa: BLE001 — the council is open; /raid loot re-posts the card
            log.warning("loot session card post failed for %s: %s", session.id, e)
            return None
        try:
            await card.pin()
        except Exception as e:  # noqa: BLE001 — no Manage Messages: the card is still the first message
            log.debug("loot card pin: %s", e)
        session.cards["loot"] = card.id
        self._surface()["fresh"][session.key] = (card.id, time.monotonic())
        return card.id

    async def loot_repost(self, reg, session: LootSession) -> int:
        """/raid loot on an open council (or a thread created late, or a restart that lost the old buttons): a fresh
        session card at the bottom (the old one stubbed and unpinned) and every pending pick re-posted on fresh cards.
        Returns the messages posted."""
        if session.origin != "raid":
            return await super().loot_repost(reg, session)
        thread = await self.loot_thread(session)
        if thread is None:
            return 0
        n = 0
        fresh = self._surface()["fresh"].get(session.key)
        if not (fresh and fresh[0] == session.cards.get("loot") and time.monotonic() - fresh[1] < FRESH_CARD_S):
            from .raid_views import loot_stub_layout

            old = session.cards.get("loot")
            if old:
                await self._loot_edit(thread, old, view=loot_stub_layout("this card moved — the council's card is the newest one below"))
                try:
                    await thread.get_partial_message(old).unpin()
                except Exception:  # noqa: BLE001
                    pass
            mid = await self._loot_post_card(reg, session, thread)
            if mid:
                await self._loot_cards_save(session, {"loot": mid}, message="session card re-posted")
                n += 1
        n += await self.loot_batches_sync(reg, session, repost=True)
        return n

    # ------------------------------------------------------------ hooks: after every write
    async def loot_notify(self, reg, session: LootSession, line: str | None = None, *, kind: str = "", drop_id: str | None = None) -> None:
        """The verbs' after-write hook: the line into the thread (with the buttons it needs), the proposal cards synced
        (in place; re-posted at the bottom when a pick appears that no card shows), the session card redrawn. Discord
        errors never reach the verb (the write already happened)."""
        if session.origin != "raid":
            return await super().loot_notify(reg, session, line, kind=kind, drop_id=drop_id)
        if self.events.get(session.key) is not session:
            return  # ended between the write and this hook: its card is the summary; no batch cards in an archived thread
        try:
            if kind == "distribute":
                stale = bool(line and "changed while judging" in line)
                note = "re-judged" if line and "re-judged" in line else None
                if stale:
                    await self.loot_post(session, line)
                await self.loot_batches_sync(reg, session, note=note)
            else:
                if line:
                    await self.loot_post(session, line, view=self.loot_view(reg, session, kind, drop_id))
                await self.loot_batches_sync(reg, session)
            await self.loot_card_refresh(reg, session)
        except Exception as e:  # noqa: BLE001
            log.warning("loot notify (%s) failed for %s: %s", kind, session.id, e)

    async def loot_post(self, session: LootSession, text: str, view=None):
        """A line in the council's thread (long text split at 1900 characters; the view on the last part)."""
        ch = await self.loot_thread(session)
        if ch is None:
            return None
        chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [""]
        msg = None
        for i, part in enumerate(chunks):
            try:
                msg = await ch.send(part, view=view) if (view is not None and i == len(chunks) - 1) else await ch.send(part)
            except Exception as e:  # noqa: BLE001 — the write already happened; the line is a courtesy
                log.warning("loot thread post failed: %s", e)
                return None
        return msg

    def loot_view(self, reg, session: LootSession, kind: str, drop_id: str | None = None):
        """The persistent buttons a thread line carries (None: none): Give the reason + Mistake — correct… under a
        companion award against the bot's pick, Correct… under a conflict or an in-game award with no proposal,
        Distribute under ticked drops / the interrupted line, Keep open under the closing notice."""
        if session.origin != "raid":
            return None
        from .raid_buttons import LootButton

        items = []
        if kind == "mismatch" and drop_id:
            items = [LootButton(session.guild, session.id, drop_id, "why"), LootButton(session.guild, session.id, drop_id, "fix", label="Mistake — correct…")]
        elif kind == "conflict" and drop_id:
            items = [LootButton(session.guild, session.id, drop_id, "fix")]
        elif kind == "manual" and drop_id:  # an in-game award with no proposal: recorded first, fixed second (L21)
            items = [LootButton(session.guild, session.id, drop_id, "fix", label="Wrong? Fix…")]
        elif kind == "closing":
            items = [LootButton(session.guild, session.id, "-", "keep")]
        elif kind in ("tick", "interrupted") and session.undistributed() and not session.distributing:
            if kind == "interrupted" or not (self.loot_auto_on(reg, session) and any(d.source == "companion" for d in session.undistributed())):
                items = [LootButton(session.guild, session.id, "-", "dist")]
        if not items:
            return None
        try:
            v = discord.ui.View(timeout=None)
        except RuntimeError:  # a View needs a running loop (offline tools)
            return None
        for it in items:
            v.add_item(it)
        return v

    async def loot_card_refresh(self, reg, session: LootSession) -> bool:
        thread = await self.loot_thread(session)
        _, ev = self.loot_run(session)
        if thread is None or ev is None or not session.cards.get("loot"):
            return False
        return await self._loot_edit(thread, session.cards["loot"], view=self.loot_card_view(reg, session, ev))

    async def loot_batches_sync(self, reg, session: LootSession, *, note: str | None = None, repost: bool = False) -> int:
        """The proposal cards follow the session: edited in place while every pending pick is on one of them; otherwise
        (a new batch, `repost`) the old cards become stubs and every pending pick is posted at the bottom, ≤5 per card,
        grouped by boss, the batch controls on the last one. Returns the cards posted."""
        from .raid_views import batch_layout, loot_chunks, loot_stub_layout

        thread = await self.loot_thread(session)
        if thread is None:
            return 0
        cur = [(k, mid) for k, mid in session.cards.items() if k.startswith("batch:")]
        covered = {d for k, _ in cur for d in k[6:].split(",") if d}
        pending = {p.drop_id for p in session.proposals}
        if cur and not repost and pending <= covered:
            for i, (k, mid) in enumerate(cur):
                ids = [d for d in k[6:].split(",") if d]
                await self._loot_edit(thread, mid, view=batch_layout(reg, session, ids, self.ico, last=i == len(cur) - 1, note=note if i == 0 else None))
            return 0
        chunks = loot_chunks(session)
        if not chunks and not cur:
            return 0
        for k, mid in cur:
            await self._loot_edit(thread, mid, view=loot_stub_layout("superseded — the current proposals are below"))
        new: dict[str, int] = {}
        for i, ids in enumerate(chunks):
            try:
                msg = await thread.send(view=batch_layout(reg, session, ids, self.ico, last=i == len(chunks) - 1, note=note if i == 0 else None))
            except Exception as e:  # noqa: BLE001
                log.warning("loot proposal card post failed: %s", e)
                continue
            new["batch:" + ",".join(ids)] = msg.id
        await self._loot_cards_save(session, new, drop=[k for k, _ in cur], message=f"{len(new)} proposal card(s) posted")
        return len(new)

    async def loot_cards_restore(self) -> int:
        """On ready: every live council's cards redrawn in place with this version's buttons (DynamicItems keep
        working across restarts; this makes the drawing current). Returns the councils touched."""
        n = 0
        for s in list(self.events.values()):
            if s.origin != "raid" or not s.raid_thread_id or not self.loot_live(s):
                continue
            reg = self.loot_reg(s)
            if reg is None:
                continue
            try:
                await self.loot_card_refresh(reg, s)
                await self.loot_batches_sync(reg, s)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("loot cards restore failed for %s: %s", s.id, e)
        return n

    # ------------------------------------------------------------ distribute with the card's ticker
    async def distribute(self, reg, session: LootSession, *, progress=None, **kw):
        """LootMixin.distribute, with the session card's status line as the ticker (every TICK_S while it judges) —
        for every caller: the Distribute button, /raid distribute, the auto-distribute, the web and MCP."""
        if session.origin != "raid" or reg is None or self._loot_state()["flight"].get(session.key):
            return await super().distribute(reg, session, progress=progress, **kw)
        judging = self._surface()["judging"]
        state = {"done": 0, "total": 0, "last": ""}

        def cb(done: int, total: int, name: str) -> None:  # the worker thread: plain assignments only
            state["done"], state["total"], state["last"] = done, total, name
            if progress is not None:
                progress(done, total, name)

        async def ticker() -> None:
            await asyncio.sleep(0.5)
            while True:
                if not state["total"]:
                    state["total"] = len((session.distributing or {}).get("drops") or [])
                await self.loot_card_refresh(reg, session)
                await asyncio.sleep(TICK_S)

        note = None if session.scores_only else loot_budget_note(self.loot_ctx(session).provider)
        warned = self._surface().setdefault("budget", set())
        if note and session.key not in warned:  # L36: once per council, before the judge that may run it out
            warned.add(session.key)
            await self.loot_post(session, note)
        judging[session.key] = state
        task = asyncio.get_running_loop().create_task(ticker())
        try:
            return await super().distribute(reg, session, progress=cb, **kw)
        finally:
            task.cancel()
            judging.pop(session.key, None)

    # ------------------------------------------------------------ hooks: closing and the end
    async def loot_closing(self, reg, session: LootSession) -> None:
        """The scheduler's one "closing in 15 min" notice, with Keep open (the message id is kept so the press can
        edit it to its final state)."""
        if session.origin != "raid":
            return await super().loot_closing(reg, session)
        from .discord_loot import CLOSING_NOTICE_MIN

        owed = len(session.pending_reasons) + len(session.conflicts)
        msg = await self.loot_post(session, f"⏳ This council closes in {CLOSING_NOTICE_MIN} min — the run is over and no pick is waiting."
                                   + (f" {owed} award(s) still need a reason or a correction; they stay fixable after the end." if owed else ""),
                                   view=self.loot_view(reg, session, "closing"))
        if msg is not None and getattr(msg, "id", None):
            await self._loot_cards_save(session, {"closing": msg.id}, message="closing notice posted")

    async def loot_ended(self, reg, session: LootSession, text: str) -> None:
        """The summary into the thread; the session card becomes the summary (no buttons), the proposal cards and the
        closing line lose theirs, the starter line in the channel says it ended, the thread is archived."""
        if session.origin != "raid":
            return await super().loot_ended(reg, session, text)
        from .raid_views import guild_prefix, loot_closed_layout, loot_label, loot_stub_layout, run_label

        await self.loot_post(session, text)
        thread = await self.loot_thread(session)
        _, ev = self.loot_run(session)
        if thread is not None:
            await self._loot_edit(thread, session.cards.get("loot"), view=loot_closed_layout(reg, ev, session, text))
            for k, mid in session.cards.items():
                if k.startswith("batch:"):
                    await self._loot_edit(thread, mid, view=loot_stub_layout("the council ended — the summary is below"))
            if session.cards.get("closing"):
                await self._loot_edit(thread, session.cards["closing"], content="⏳ The council ended.", view=None)
        ch_id, starter = session.cards.get("channel"), session.cards.get("starter")
        get = getattr(self, "get_channel", None)
        ch = get(ch_id) if get and ch_id else None
        if ch is not None and starter and ev is not None:
            first = text.split("\n")[1] if "\n" in text else ""
            await self._loot_edit(ch, starter, content=f"{guild_prefix(reg)}⚔ Loot council · {loot_label(session)}{run_label(reg, ev)} — ended" + (f": {first}" if first else "") + f" · <#{session.raid_thread_id}>")
        if thread is not None and hasattr(thread, "edit"):
            try:
                await thread.edit(archived=True)
            except Exception as e:  # noqa: BLE001
                log.debug("loot thread archive: %s", e)

    # ------------------------------------------------------------ the scheduler's loot steps
    def _loot_prompt_channel(self, reg):
        """Where an officer-only loot prompt goes: the officer channel (roster, else ops) — never the public loot log
        (`loot_channel_id` carries only the one award line per Confirm and the correction line, LootLogMixin)."""
        return self.officer_channel(reg, None)

    async def loot_scheduler_step(self, reg) -> None:
        """Every minute, per guild: in-game awards held with no council live shortly after one ended → one DM to the
        officers with the link to correct or record them (they must act — and no ad hoc prompt when that is all that
        waits); other companion events held while no council is live → one line in the officer channel (roster, else
        ops; never the public loot log) with "Start an ad hoc run…", edited once the held events went somewhere."""
        st = self._surface()["prompts"]
        w = self.feed_waiting(reg)
        live = [s for s in self.loot_sessions(reg) if s.origin == "raid"]
        have = st.get(reg.key)
        if not w or live:
            if have:
                st.pop(reg.key, None)
                await self._loot_edit(have.get("channel"), have.get("message"), content="📡 No longer waiting — the held drops went to the council, or the ops channel lists them as not recorded.", view=None)
            return
        if have is not None and have.get("since") == w["since"]:
            return
        late = await self._loot_after_end_dm(reg)
        if late and all((r.get("ev") or {}).get("type") == "loot" for r in self._held_rows(reg.key)):
            st[reg.key] = {"since": w["since"], "channel": None, "message": None}
            return
        from .raid_buttons import LootStartButton
        from .raid_views import guild_prefix

        ch = self._loot_prompt_channel(reg)
        msg = None
        if ch is not None:
            who = ", ".join(w.get("characters") or ["a companion"])
            view = discord.ui.View(timeout=None)
            view.add_item(LootStartButton(reg.key))
            try:
                msg = await ch.send(f"{guild_prefix(reg)}📡 Drops arriving from {who}'s log but no run is live — start an ad hoc run for tonight? "
                                    "They're held 10 minutes; a loot council opened by then takes them (/raid loot).", view=view)
            except Exception as e:  # noqa: BLE001
                log.warning("loot no-run prompt failed: %s", e)
        st[reg.key] = {"since": w["since"], "channel": ch, "message": getattr(msg, "id", None)}

    def _loot_recent_end(self, reg) -> dict | None:
        """The real council of this guild that ended most recently, within AFTER_END_H (from its archive), or None — a
        rehearsal never counts (its puppet companion's late events must not DM every officer)."""
        arch = reg.store.root / reg.key / "events" / "archive"
        if not arch.is_dir():
            return None
        cutoff = time.time() - AFTER_END_H * 3600
        best = None
        import json

        for f in arch.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    continue
                body = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            ended = _utc((body.get("summary") or {}).get("ended_at"))
            if body.get("origin") != "raid" or body.get("test") or ended is None or ended < datetime.now(timezone.utc) - timedelta(hours=AFTER_END_H):
                continue  # a rehearsal's late events are test traffic: never a DM to the officers
            if best is None or ended > best[0]:
                best = (ended, body)
        return best[1] if best else None

    def loot_officer_ids(self, reg) -> list[int]:
        """Who the one after-end DM goes to: the members of the guild's officer roles the client can see, else the owner."""
        ids: set[int] = set()
        get_guild = getattr(self, "get_guild", None)
        guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
        for rid in reg.config.officer_role_ids if guild is not None else []:
            role = guild.get_role(rid)
            ids |= {m.id for m in (getattr(role, "members", None) or []) if not getattr(m, "bot", False)}
        if not ids and reg.config.owner_discord_id:
            ids.add(int(reg.config.owner_discord_id))
        return sorted(ids)

    async def _loot_after_end_dm(self, reg) -> int:
        """In-game awards held with no council live, shortly after one ended: the officers must record or correct them
        (a must-act DM, the council's one exception to the DM rule) — once per held batch."""
        rows = [r for r in self._held_rows(reg.key) if (r.get("ev") or {}).get("type") == "loot"]
        if not rows:
            return 0
        key = (reg.key, rows[0].get("at"))
        sent = self._surface()["after_end"]
        if key in sent:
            return 0
        ended = self._loot_recent_end(reg)
        if ended is None:
            return 0
        sent.add(key)
        from .raid_views import guild_prefix

        what = "; ".join(f"{(r.get('ev') or {}).get('item_name') or (r.get('ev') or {}).get('item_id')} → {(r.get('ev') or {}).get('recipient', '?')}" for r in rows[:8])
        run = ended.get("run_key") or ended.get("id")
        url = loot_site_url(reg, run)
        text = (f"{guild_prefix(reg)}In-game awards arrived after the loot council for {run} ended: {what}. They are not recorded yet — "
                + (f"record or correct them on the site: {url}" if url else "open a council (/raid loot) to record them, or record them on the site")
                + " (held 10 minutes, then listed in the ops channel).")
        n = 0
        for uid in self.loot_officer_ids(reg):
            try:
                user = await self.fetch_user(uid)
                await user.send(text[:1900])
                n += 1
            except Exception as e:  # noqa: BLE001
                log.info("after-end loot DM to %s failed: %s", uid, e)
        return n

    # ------------------------------------------------------------ the thread chat (addressed only, decision 8 / D9)
    def loot_addressed(self, message, session: LootSession) -> str | None:
        """The text an officer addressed to the bot in a council thread, or None: an @mention (of the bot or its role),
        a leading "?", or a reply to one of the council's cards. Replies to companion lines and everything else are
        banter — no model call."""
        text = message.content or ""
        me = getattr(self, "user", None)
        mentioned = me is not None and me in (getattr(message, "mentions", None) or [])
        guild_me = getattr(getattr(message, "guild", None), "me", None)
        if not mentioned and guild_me is not None:
            mentioned = any(r in (getattr(message, "role_mentions", None) or []) for r in getattr(guild_me, "roles", []) or [])
        if mentioned:
            return re.sub(r"<@[!&]?\d+>", "", text).strip() or None
        if text.lstrip().startswith("?"):
            return text.lstrip()[1:].strip() or None
        ref = getattr(message, "reference", None)
        mid = getattr(ref, "message_id", None) if ref is not None else None
        if mid and mid in {v for k, v in session.cards.items() if k not in ("channel",)}:
            return text.strip() or None
        return None

    async def loot_chat(self, message, session: LootSession, text: str | None = None) -> None:
        """An addressed officer message in a council thread. A question → help_answer with the council as data; a
        request to act (help says so) → parsed against the proposals into overrides / a confirm and shown as a diff
        with Apply (nothing is written before Apply; the batch the officer saw is what Apply checks)."""
        from . import configops, guild_routing, nl
        from .discord_help import ACT
        from .llm.provider import for_guild

        text = text if text is not None else self.loot_addressed(message, session)
        if not text:
            return
        reg = self.loot_reg(session)
        if reg is None:
            return
        if await self.role_of(reg, message.author, max_age=guild_routing.PRIV_ROUTING) not in ("owner", "officer"):
            return  # the council's data is officer-only; members' chat in the thread is banter
        ctx = self.loot_ctx(session)
        provider = ctx.provider
        if provider is None:
            await message.reply(LLM_DOWN)
            return
        # what the officer was looking at when they wrote: the batch (a confirm-all) and each drop's pick (an award) —
        # a pick added or moved while the model reads is refused at Apply with what changed, never recorded unseen
        seen = ls.batch_rev(session)
        seen_to = {p.drop_id: p.to for p in session.proposals}
        uid, by = message.author.id, message.author.display_name
        parent = loot_parent(session, message.channel)  # plain_mode reads a thread as its parent channel
        async with message.channel.typing():
            try:
                answer = await self.help_answer(reg, uid, True, text, may_act=True, public=False, data=loot_digest(session))
            except Exception as e:  # noqa: BLE001
                await message.reply(await configops.report_error(self.ops, reg.config, e, "loot chat"))
                return
            if answer is not None and answer != ACT:
                await message.reply(answer[:1900])
                return
            # a request to act: the plain-text check (Registry.may_plain) on the `loot` group in the parent channel's
            # mode — the same check configops.authorize makes at Apply
            bad = await loot_plain_refusal(self, reg, message.author, parent, max_age=guild_routing.PRIV_ROUTING)
            if bad:
                await message.reply("⛔ " + bad + " The card's buttons still work.")
                return
            if not session.proposals:
                await message.reply("Nothing is proposed yet — tick the drops and press Distribute; then tell me who should get what.")
                return
            try:
                with for_guild(reg.key):  # D45: the parse is the council's guild's spend (to_thread copies the context)
                    fb = await asyncio.to_thread(nl.parse_loot_feedback, provider, ctx.policy_for_llm(), "<data>\n" + loot_digest(session, numbered=True) + "\n</data>", text, by)
            except Exception as e:  # noqa: BLE001
                await message.reply(await configops.report_error(self.ops, reg.config, e, "loot chat"))
                return
        if fb.kind == "ignore":
            return
        if fb.kind == "question":
            await message.reply((fb.reply or "Ask me about a proposal — who, why, or why not someone else.")[:1900])
            return
        changes, refused = loot_changes(self, session, fb, text)
        if not changes:
            await message.reply("⛔ " + ("\n⛔ ".join(refused) if refused else "I couldn't tell which drop or who — use Override… on the card."))
            return
        from . import loot_ops

        revs = {}
        for c in changes:
            op = loot_op(session, c)
            revs[loot_ops.rev_key(op)] = seen if c["kind"] == "confirm" else "to:" + (seen_to.get(c["drop_id"]) or c["bot_to"])
        view = LootChatConfirmView(self, reg, session, changes, requester_id=uid, requester=by, revs=revs, channel=parent)
        sent = await message.reply(view.text(refused), view=view)
        view.bind(sent)


def loot_changes(bot, session: LootSession, fb, message: str) -> tuple[list[dict], list[str]]:
    """The parser's reading → changes the diff shows: {kind: override|confirm, drop_id, to, reason, inferred}. Names are
    checked against the candidate table and tonight's roster (never a name the model made up); a reason the officer
    didn't write is marked inferred (Apply waits until it is edited)."""
    if fb.kind == "confirm":
        return [{"kind": "confirm", "count": len(session.proposals)}], []
    roster = bot.loot_roster(session)
    names = {(p.character or p.signup_name).lower(): (p.character or p.signup_name) for p in roster}
    out, refused, used = [], [], set()
    low = message.lower()
    for ch in fb.changes:
        did = f"d{ch.item_id}"
        p = session.pending(did)
        if p is None or did in used:
            refused.append(f"no pending pick numbered {ch.item_id}")
            continue
        who = str(ch.award_to or "").strip()
        cands = {c.character.lower(): c.character for c in p.result.candidates}
        if who.lower() in ("disenchant", DISENCHANT):
            to = DISENCHANT
        elif who.lower() in cands or who.lower() in names:
            to = cands.get(who.lower()) or names[who.lower()]
        else:
            refused.append(f"{p.item_name}: {who or '?'} isn't rostered or in the table — use Override… on the card for someone off the roster")
            continue
        reason = (ch.reason or "").strip()
        inferred = not reason or reason.lower() not in low
        used.add(did)
        out.append({"kind": "override", "drop_id": did, "item": p.item_name, "bot": p.award_to, "bot_to": p.to, "to": to, "reason": reason, "inferred": inferred})
    return out, refused


def loot_parent(session: LootSession, channel) -> int | None:
    """The channel a council thread's plain-text mode is read from: the thread's parent (the officer channel)."""
    return getattr(channel, "parent_id", None) or session.cards.get("channel") or getattr(channel, "id", None)


async def loot_identity(bot, reg, user, *, max_age: float) -> tuple[bool, list[int]]:
    """(officer, role ids) for Registry.may_plain — guild_routing.identity, the one predicate."""
    r = await bot.identity(reg, user, max_age=max_age)
    return r.officer, list(r.role_ids)


async def loot_plain_refusal(bot, reg, user, channel, *, max_age: float) -> str | None:
    """Registry.may_plain for a loot write from the thread (the `loot` capability group, officers by default): None when
    allowed, else the refusal line (who may, where). configops.authorize makes the same check again at Apply."""
    officer, role_ids = await loot_identity(bot, reg, user, max_age=max_age)
    return reg.may_plain("loot", user.id, officer=officer, role_ids=role_ids, channel=channel, what="loot awards")


def loot_op(session: LootSession, ch: dict):
    """The `loot` ConfigOp a chat change is (loot_ops): field = confirm (every pick still pending) | award, target = the
    council (session id), value = the drop, member = the recipient, reason = the reason."""
    from .configops import ConfigOp

    if ch["kind"] == "confirm":
        return ConfigOp(op="loot", field="confirm", target=session.id, value="all")
    return ConfigOp(op="loot", field="award", target=session.id, value=ch["drop_id"], member=ch["to"], reason=ch.get("reason") or "")


WHY = "why? — press Give the reason… (it becomes the precedent)"


def _confirm_view_base():
    from .discord_policy import ConfigConfirmView  # lazily: discord_policy imports every flow

    return ConfigConfirmView


class LootChatConfirmView(_confirm_view_base()):
    """The diff under an officer's loot sentence in a council thread: the plain-text pipeline's own Apply
    (ConfigConfirmView: one `loot` ConfigOp per change, its line from configops.describe, Apply = configops.authorize in
    the thread's PARENT channel + configops.apply_async with the rev each op showed — the batch_rev for "confirm the
    rest", 'to:<pick>' for an award, both taken when the officer wrote — so a pick added or moved since is refused with
    what changed, and an override never replaces another officer's).

    Why a subclass and not ConfigConfirmView itself: the reason of an override can be missing or only the model's guess.
    Then the line asks "why?" in the reason's place and a third button, Give the reason… (the reason modal), must be
    answered before Apply — the reason becomes a precedent, so the bot never records one an officer didn't write. The
    diff is a reply in the thread (not an ephemeral /gm change answer), with CfgApplyButtons like every pending diff."""

    def __init__(self, bot, reg, session: LootSession, changes: list[dict], *, requester_id: int, requester: str, revs: dict[str, str],
                 channel: int | None = None):
        from . import configops

        self.ACTIONS = ("apply", "cancel", "edit") if any(c.get("inferred") for c in changes) else ("apply", "cancel")
        req = configops.ConfigRequest(kind="change", reply="", ops=[loot_op(session, c) for c in changes])
        super().__init__(reg, req, None, bot.ops, requester_id, requester, channel, revs=revs)
        self.bot, self.session, self.changes = bot, session, changes
        self._sync_apply()

    def _sync_apply(self) -> None:
        apply = getattr(self, "apply", None)
        if apply is not None:
            apply.item.disabled = any(c.get("inferred") for c in self.changes)

    def line(self, ch: dict) -> str:
        """configops.describe of the change's op — the same words /gm change and the site show; an override whose reason
        is missing or a guess asks "why?" in the reason's place (never an error line)."""
        from . import configops, loot_ops

        op = loot_op(self.session, ch)
        if ch["kind"] == "override" and ch.get("inferred"):
            guess = f" (my guess: “{ch['reason']}”)" if ch.get("reason") else ""
            try:
                return loot_ops.describe(self.reg, op, why=WHY.replace("why?", "why?" + guess, 1))
            except RegistryError:
                pass  # not describable at all (the drop is gone): configops' line says why
        return configops.describe(self.reg, op)

    def text(self, refused: list[str] | None = None) -> str:
        lines = ["**Proposed loot changes**"] + [f"• {self.line(c)}" for c in self.changes] + [f"⛔ {r}" for r in (refused or [])]
        return "\n".join(lines)[:1900]

    async def press_edit(self, interaction: discord.Interaction) -> None:
        if await self._may_press(interaction) is None:
            return
        view = self
        todo = [c for c in self.changes if c["kind"] == "override"][:5]

        class GiveReasons(discord.ui.Modal, title="The reasons (they become precedents)"):
            def __init__(self):
                super().__init__(timeout=600)
                self.boxes = []
                for c in todo:
                    box = discord.ui.TextInput(label=f"{c['item']} → {c['to']}"[:45], style=discord.TextStyle.paragraph, min_length=ls.REASON_MIN,
                                               max_length=ls.REASON_MAX, default=(c.get("reason") or None) and c["reason"][:ls.REASON_MAX])
                    self.boxes.append((c, box))
                    self.add_item(box)

            async def on_submit(self, si: discord.Interaction):
                for c, box in self.boxes:
                    c["reason"], c["inferred"] = str(box.value).strip(), False
                view._sync_apply()
                await si.response.edit_message(content=view.text(), view=view)

        await interaction.response.send_modal(GiveReasons())

    async def press_apply(self, interaction: discord.Interaction) -> None:
        if any(c.get("inferred") for c in self.changes):
            await interaction.response.send_message("Give the reason first (Give the reason…) — it becomes a precedent, so it has to be yours.", ephemeral=True)
            return
        self.req.ops = [loot_op(self.session, c) for c in self.changes]  # with the reasons as given
        await super().press_apply(interaction)


__all__ = ["LootSurfaceMixin", "LootChatConfirmView", "loot_changes", "loot_digest", "loot_op", "loot_site_url"]
