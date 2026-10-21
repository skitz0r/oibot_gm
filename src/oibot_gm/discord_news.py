"""NewsMixin: the #news channel and the proposal cards (design.md §5.26; dual interfaces phase 3, design §5.44).

- `news_message`: the one hook at the top of `OibotGM.on_message`. A message in the configured news channel is
  keyword-filtered (news.ingest) and the relevant items are kept in `<guild>/news.jsonl`. Webhook posts count as
  bots, which is why this runs before on_message's `author.bot` return.
- `news_backfill`: re-reads the channel's recent history (missed while the bot was down); the review job calls it
  through the API before it reads the list, so a restart never loses a day's news.
- `decide_proposal`: THE proposal verb (design §3.4 X2) — ProposalStore.move (or `annotate` when `state` is None) →
  proposal_card_update → ops.audited → the shared-profile fan-out to the other guilds on the game version. Callers: the
  card's decide screen (flows_news), POST /api/proposals/{pid}/resolve (and MCP through it), the `proposal` op. `officer`,
  `owner` and `job` come from identity at the boundary (role_of, the X-Oibot-Job header), never defaulted True. The save
  publishes `agents` through news.PROPOSAL_LISTENERS (every page, every path).
- `proposal_card_update`: posts a proposal's card in the ops channel, or edits it in place when its state moves; returns
  whether Discord took the edit (the site says "the Discord card updates when the bot is back" otherwise).
- `proposal_cards_restore`: on ready / resumed, every non-terminal card is redrawn (a decision made on the site while
  Discord was down reaches the card).
- `ProposalButton`: Approve / Dismiss (Withdraw approval once approved) on the card (`newsprop.<gk>:<id>:<action>`).
  A press never writes: it opens the decide screen (flows_news.proposal_decide)."""
from __future__ import annotations

import asyncio
import logging
import re

import discord

from . import news
from .news import Proposal, ProposalStore, stamp  # noqa: F401 — stamp is re-exported (older callers import it from here)

log = logging.getLogger(__name__)

STATE_COLOUR = {"proposed": 0x38B2A0, "approved": 0x5C9BFF, "dismissed": 0x5A6472, "applying": 0xE0A526, "applied": 0x2E9E5B, "failed": 0xD9534F, "reverted": 0xD9534F}
STATE_TEXT = {"proposed": "Waiting for an officer", "approved": "Approved — the apply job picks it up at its next run", "dismissed": "Dismissed",
              "applying": "Applying now", "applied": "Applied", "failed": "Failed — nothing changed", "reverted": "Reverted — the change was rolled back"}
KIND_TEXT = {"guild_setting": "guild setting (applied without a restart)", "profile": "game data file (a commit and a bot restart)",
             "needs_developer": "needs a developer (the apply job won't touch it)"}
NEEDS_DEVELOPER = "Needs a developer — the apply job won't touch it; dismiss it once it's handled"
TERMINAL = ("dismissed", "applied", "reverted")
BACKFILL_LIMIT = 100


def proposal_embed(p: Proposal, news_rows: dict[str, dict] | None = None, shared_with: list[str] | None = None) -> discord.Embed:
    """The card: what the news says, what it would change, where it stands. Pure (tests render it offline).
    `shared_with`: the other servers a profile edit's game data is shared with (Affects names them)."""
    e = discord.Embed(title=f"📰 {p.title}"[:256], description=p.change[:3500], colour=STATE_COLOUR.get(p.state, 0x98A3B5))
    affects = f"{p.affects[:300]}\n-# {KIND_TEXT.get(p.kind, p.kind)}"
    if p.kind == "profile" and shared_with:
        affects += f"\n-# Game data shared with: {', '.join(discord.utils.escape_markdown(n) for n in shared_with)}"
    e.add_field(name="Affects", value=affects[:1024], inline=False)
    if p.evidence:
        e.add_field(name="Evidence", value="\n".join("> " + ln for ln in p.evidence[:900].splitlines() if ln.strip()) or "—", inline=False)
    rows = news_rows or {}
    src = []
    for u in p.news[:5]:
        r = rows.get(news.NewsStore.ident({"url": u}))
        title = (r or {}).get("title") or u
        src.append(f"• [{title[:90]}]({u})" if u.startswith("http") else f"• {title[:90]}")
    if src:
        e.add_field(name="News", value="\n".join(src)[:1024], inline=False)
    e.add_field(name="Confidence", value=p.confidence, inline=True)
    line = STATE_TEXT.get(p.state, p.state)
    if p.kind == "needs_developer" and p.state in ("proposed", "failed"):
        line = NEEDS_DEVELOPER
    if p.state in ("approved", "dismissed") and p.decided_by:
        line = f"{'Approved' if p.state == 'approved' else 'Dismissed'} by {p.decided_by} {stamp(p.decided_at)}" + (" — the apply job picks it up at its next run" if p.state == "approved" else "")
    elif p.decided_by and p.state in ("applying", "applied", "failed", "reverted"):
        line += f" · approved by {p.decided_by}"
    if p.commit:
        line += f" · commit `{p.commit[:10]}`"
    if p.status_note and p.state not in TERMINAL:
        line += f"\n⏳ {p.status_note[:300]}"
    if p.note:
        line += f"\n-# {p.note[:300]}"
    e.add_field(name="Status", value=line[:1024], inline=False)
    e.set_footer(text=f"proposal {p.id} · from the daily news review · the site's Agents page has the full run")
    return e


def proposal_view(p: Proposal, gk: str | None) -> discord.ui.View:
    """Approve / Dismiss while it is waiting (a failed one can be approved again); greyed once decided. Once approved the
    second button reads Withdraw approval (§2.7: two names for one act, never); a needs-developer proposal has no
    Approve at all (its Status says why). `gk`: the proposal's guild (ids are per guild and may repeat across guilds)."""
    v = discord.ui.View(timeout=None)
    if p.kind != "needs_developer":
        v.add_item(ProposalButton(gk, p.id, "approve", disabled=p.state not in ("proposed", "failed")))
    v.add_item(ProposalButton(gk, p.id, "dismiss", disabled=p.state not in ("proposed", "approved", "failed"),
                              label="Withdraw approval" if p.state == "approved" else None))
    return v


class ProposalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"newsprop(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<pid>[0-9]{6}-[0-9a-f]{4}):(?P<action>approve|dismiss)"):
    LABELS = {"approve": ("Approve", discord.ButtonStyle.success), "dismiss": ("Dismiss", discord.ButtonStyle.secondary)}

    def __init__(self, gk: str | None, pid: str, action: str, disabled: bool = False, label: str | None = None):
        text, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label or text, style=style, custom_id=f"newsprop{'.' + gk if gk else ''}:{pid}:{action}", disabled=disabled))
        self.gk, self.pid, self.action = gk, pid, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["pid"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        """Approve / Dismiss open the ONE confirm screen (flows_news.proposal_decide → wizard.Wizard.decide, D34: Dismiss
        too); the press itself never writes."""
        from .discord_registry import channel_card_guild
        from .wizard_flows import FLOWS

        reg = channel_card_guild(interaction, self.gk)  # a channel-only card: strict, never a DM fallback
        if reg is None:
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        await FLOWS["proposal_decide"](interaction, reg, pid=self.pid, state="approved" if self.action == "approve" else "dismissed",
                                       card=getattr(interaction, "message", None))


class NewsMixin:
    def news_rows(self, reg) -> dict[str, dict]:
        return {news.NewsStore.ident(r): r for r in news.NewsStore(self.registries.store, reg.key).all()}

    async def news_message(self, message: discord.Message) -> bool:
        """True when the message was a news post the bot consumed (nothing else should route it). A person's message
        in the news channel is still read for news but falls through to the normal routing."""
        if not message.guild:
            return False
        reg = self.registries.by_discord.get(message.guild.id)
        if not reg or not reg.config.news_channel_id or message.channel.id != reg.config.news_channel_id:
            return False
        if self.user and message.author.id == self.user.id:
            return True
        try:
            rows = await asyncio.to_thread(news.ingest, reg, self.registries.store, message)
        except Exception as e:  # noqa: BLE001 — a bad embed must not break on_message
            log.exception("news ingest failed")
            await self.ops.emit(reg.config, "error", f"news ingest failed: {e}", exc=e)
            rows = []
        if rows:
            await self.ops.emit(reg.config, "info", f"news: kept {len(rows)} item(s) — " + "; ".join(r["title"][:80] for r in rows))
        return bool(message.author.bot or message.webhook_id)

    async def news_backfill(self, reg, limit: int = BACKFILL_LIMIT) -> int:
        """Re-read the news channel's recent history (dedupe makes it idempotent). Returns the number of new items."""
        ch = self.get_channel(reg.config.news_channel_id) if reg.config.news_channel_id else None
        if ch is None:
            return 0
        added = 0
        async for m in ch.history(limit=limit, oldest_first=True):
            if self.user and m.author.id == self.user.id:
                continue
            added += len(await asyncio.to_thread(news.ingest, reg, self.registries.store, m))
        return added

    # ---------------------------------------------------------------- the raid guard (D33) and the job's next run
    def guild_busy(self, reg, now=None) -> dict:
        """news.guild_busy for one guild from the bot's own state: its live runs and its live loot councils."""
        rs = self.raids.store(reg) if getattr(self, "raids", None) is not None else None
        sessions = getattr(self, "loot_sessions", None)
        loot = [s.run_key for s in (sessions(reg) if sessions is not None else []) if getattr(s, "run_key", None)]
        return news.guild_busy(reg, rs.live() if rs is not None else [], loot, now)

    def busy_map(self, now=None) -> dict[str, dict]:
        """{guild key: guild_busy} for every served guild — the whole process restarts, so every guild counts (D33)."""
        return {r.key: self.guild_busy(r, now) for r in self.registries.ordered()}

    def apply_next_label(self, reg) -> str | None:
        """When the apply job polls next, in the guild's words (None when it never ran on this host)."""
        from . import agents

        from datetime import datetime, timezone

        try:
            nxt = agents.next_run("apply", agents.read_status("apply"))
        except Exception:  # noqa: BLE001 — a missing status file: no time to promise
            nxt = None
        if nxt is None or nxt < datetime.now(timezone.utc):  # never ran here, or overdue (not installed / stopped): promise no time
            return None
        return reg.local12(nxt)

    def proposal_consequence(self, reg, p: Proposal, state: str) -> list[str]:
        """news.consequence with the live inputs: every served guild's busy state and the job's next run."""
        return news.consequence(p, reg, state, guilds=self.registries, busy=self.busy_map() if p.kind == "profile" and state == "approved" else None,
                                next_run=self.apply_next_label(reg))

    # ---------------------------------------------------------------- THE verb
    async def decide_proposal(self, reg, pid: str, state: str | None, *, by: str, by_id: int | None = None, officer: bool = False, job: bool = False,
                              owner: bool | None = None, note: str | None = None, commit: str | None = None, via: str = "discord") -> tuple[Proposal, bool]:
        """Move proposal `pid` of `reg` to `state` (or, `state` None, annotate it with `note` — the apply job's
        "waiting: …" line) and follow through everywhere: the ops-channel card, one audit line, and for a profile
        edit that landed or was rolled back, a line in every other guild on the game version. Raises
        news.ProposalError (AlreadyDecided for a repeat) / PermissionError — the caller words them per surface.
        Returns (proposal, whether the Discord card took the edit)."""
        ps = ProposalStore(self.registries.store, reg.key)

        async def card(p) -> bool:
            update = getattr(self, "proposal_card_update", None)
            if update is None:
                return False
            return (await update(reg, p)) is not False

        if state is None:
            p, written = await asyncio.to_thread(ps.annotate, pid, note, by)
            return p, (await card(p)) if written else True
        p = await asyncio.to_thread(ps.move, pid, state, by, officer=officer, job=job, by_id=by_id, commit=commit, note=note, owner=owner)
        card_ok = await card(p)
        tail = (f" @ {p.commit}" if p.commit and state in ("applied", "reverted") else "") + (f" — {p.note}" if p.note and note else "")
        from . import ops as ops_mod

        await ops_mod.audited(self.ops, reg, by, f"news proposal {p.id} → {p.state}: {p.title}{tail}", via=via, by_id=by_id,
                              level="warn" if state in ("failed", "reverted") else "info")
        if p.kind == "profile" and p.state in ("applied", "reverted"):  # shared game data: every guild on it hears it landed
            for other in self.registries.ordered():
                if other is not reg and other.config.game_profile == reg.config.game_profile:
                    await self.ops.emit(other.config, "info", f"profile change from {reg.config.name}'s news {p.state}: {p.title}" + (f" @ {p.commit}" if p.commit else ""))
        return p, card_ok

    async def proposal_card_update(self, reg, p: Proposal) -> bool:
        """Post the card in the ops channel the first time; edit it in place afterwards. Never raises (a missing
        channel or a deleted card is an ops line; the proposal itself is already saved). True when Discord took it."""
        rows = self.news_rows(reg)
        shared = news.shared_profile_others(self.registries, reg) if p.kind == "profile" else []
        try:
            if p.message_id and p.channel_id:
                ch = self.get_channel(p.channel_id)
                if ch is not None:
                    msg = ch.get_partial_message(p.message_id)
                    await msg.edit(embed=proposal_embed(p, rows, shared), view=proposal_view(p, reg.key))
                    return True
            ch = self.get_channel(reg.config.ops_channel_id) if reg.config.ops_channel_id else None
            if ch is None:
                return False
            msg = await ch.send(embed=proposal_embed(p, rows, shared), view=proposal_view(p, reg.key))
            await asyncio.to_thread(ProposalStore(self.registries.store, reg.key).set_card, p, ch.id, msg.id)
            return True
        except discord.HTTPException as e:
            await self.ops.emit(reg.config, "warn", f"news proposal {p.id}: card not updated ({e})")
            return False

    async def proposal_cards_restore(self) -> int:
        """Redraw every non-terminal proposal card (on ready and on resume): a decision made on the site or by the job
        while Discord was unreachable reaches the card. Returns how many were redrawn."""
        n = 0
        for reg in self.registries.ordered():
            try:
                rows = await asyncio.to_thread(ProposalStore(self.registries.store, reg.key).all, "proposed,approved,applying,failed")
            except Exception:  # noqa: BLE001 — a bad proposal file must not stop the others
                log.exception("proposal cards: could not list %s", reg.key)
                continue
            for p in rows:
                if p.message_id and await self.proposal_card_update(reg, p):
                    n += 1
        return n
