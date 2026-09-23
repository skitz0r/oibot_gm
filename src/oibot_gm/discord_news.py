"""NewsMixin: the #news channel and the proposal cards (design.md §5.26).

- `news_message`: the one hook at the top of `OibotGM.on_message`. A message in the configured news channel is
  keyword-filtered (news.ingest) and the relevant items are kept in `<guild>/news.jsonl`. Webhook posts count as
  bots, which is why this runs before on_message's `author.bot` return.
- `news_backfill`: re-reads the channel's recent history (missed while the bot was down); the review job calls it
  through the API before it reads the list, so a restart never loses a day's news.
- `proposal_card_update`: posts a proposal's card in the ops channel, or edits it in place when its state moves
  (approved by …, applying, applied @ commit, failed, reverted).
- `ProposalButton`: Approve / Dismiss on the card, officers only. Approving only flips the state and records who;
  the local apply job does the work."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import discord

from . import news
from .news import Proposal, ProposalError, ProposalStore

log = logging.getLogger(__name__)

STATE_COLOUR = {"proposed": 0x38B2A0, "approved": 0x5C9BFF, "dismissed": 0x5A6472, "applying": 0xE0A526, "applied": 0x2E9E5B, "failed": 0xD9534F, "reverted": 0xD9534F}
STATE_TEXT = {"proposed": "Waiting for an officer", "approved": "Approved — the apply job picks it up within 15 minutes", "dismissed": "Dismissed",
              "applying": "Applying now", "applied": "Applied", "failed": "Failed — nothing changed", "reverted": "Reverted — the change was rolled back"}
KIND_TEXT = {"guild_setting": "guild setting (applied without a restart)", "profile": "game data file (a commit and a bot restart)",
             "needs_developer": "needs a developer (the apply job won't touch it)"}
BACKFILL_LIMIT = 100


def stamp(value: str | None) -> str:
    """Discord renders `<t:unix:f>` in each reader's own locale and clock (never a typed or 24-hour time)."""
    if not value:
        return ""
    try:
        return f"<t:{int(datetime.fromisoformat(str(value)).timestamp())}:f>"
    except ValueError:
        return ""


def proposal_embed(p: Proposal, news_rows: dict[str, dict] | None = None) -> discord.Embed:
    """The card: what the news says, what it would change, where it stands. Pure (tests render it offline)."""
    e = discord.Embed(title=f"📰 {p.title}"[:256], description=p.change[:3500], colour=STATE_COLOUR.get(p.state, 0x98A3B5))
    e.add_field(name="Affects", value=f"{p.affects[:300]}\n-# {KIND_TEXT.get(p.kind, p.kind)}", inline=False)
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
    if p.state in ("approved", "dismissed") and p.decided_by:
        line = f"{'Approved' if p.state == 'approved' else 'Dismissed'} by {p.decided_by} {stamp(p.decided_at)}" + (" — the apply job picks it up within 15 minutes" if p.state == "approved" else "")
    elif p.decided_by and p.state in ("applying", "applied", "failed", "reverted"):
        line += f" · approved by {p.decided_by}"
    if p.commit:
        line += f" · commit `{p.commit[:10]}`"
    if p.note:
        line += f"\n-# {p.note[:300]}"
    e.add_field(name="Status", value=line[:1024], inline=False)
    e.set_footer(text=f"proposal {p.id} · from the daily news review · the site's Agents page has the full run")
    return e


def proposal_view(p: Proposal) -> discord.ui.View:
    """Approve / Dismiss while it is waiting (a failed one can be approved again); greyed once decided."""
    v = discord.ui.View(timeout=None)
    can_approve = p.state in ("proposed", "failed") and p.kind != "needs_developer"
    can_dismiss = p.state in ("proposed", "approved", "failed")
    v.add_item(ProposalButton(p.id, "approve", disabled=not can_approve, label="Needs a developer" if p.kind == "needs_developer" else None))
    v.add_item(ProposalButton(p.id, "dismiss", disabled=not can_dismiss))
    return v


class ProposalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"newsprop:(?P<pid>[0-9]{6}-[0-9a-f]{4}):(?P<action>approve|dismiss)"):
    LABELS = {"approve": ("Approve", discord.ButtonStyle.success), "dismiss": ("Dismiss", discord.ButtonStyle.secondary)}

    def __init__(self, pid: str, action: str, disabled: bool = False, label: str | None = None):
        text, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label or text, style=style, custom_id=f"newsprop:{pid}:{action}", disabled=disabled))
        self.pid, self.action = pid, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["pid"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg or not await bot.is_officer_anywhere(reg, interaction.user.id):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        ps = ProposalStore(bot.registries.store, reg.key)
        by = interaction.user.display_name
        state = "approved" if self.action == "approve" else "dismissed"
        try:
            p = await asyncio.to_thread(ps.move, self.pid, state, by, officer=True, job=False, by_id=interaction.user.id)
        except (ProposalError, PermissionError) as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.edit_message(embed=proposal_embed(p, bot.news_rows(reg)), view=proposal_view(p))
        await bot.ops.emit(reg.config, "info", f"news proposal {p.id} {state} by {by}: {p.title}")


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

    async def proposal_card_update(self, reg, p: Proposal) -> None:
        """Post the card in the ops channel the first time; edit it in place afterwards. Never raises (a missing
        channel or a deleted card is an ops line; the proposal itself is already saved)."""
        rows = self.news_rows(reg)
        try:
            if p.message_id and p.channel_id:
                ch = self.get_channel(p.channel_id)
                if ch is not None:
                    msg = ch.get_partial_message(p.message_id)
                    await msg.edit(embed=proposal_embed(p, rows), view=proposal_view(p))
                    return
            ch = self.get_channel(reg.config.ops_channel_id) if reg.config.ops_channel_id else None
            if ch is None:
                return
            msg = await ch.send(embed=proposal_embed(p, rows), view=proposal_view(p))
            await asyncio.to_thread(ProposalStore(self.registries.store, reg.key).set_card, p, ch.id, msg.id)
        except discord.HTTPException as e:
            await self.ops.emit(reg.config, "warn", f"news proposal {p.id}: card not updated ({e})")
