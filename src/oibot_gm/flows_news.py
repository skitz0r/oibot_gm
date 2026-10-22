"""The decide flow for news proposals (dual interfaces phase 3, design §3.4 X5 / §5.45): the card's Approve, Dismiss and
Withdraw approval never write — each opens THE confirm screen (wizard.Wizard.decide) with the kind-specific consequence
from code (NewsMixin.proposal_consequence → news.consequence):

- approve / guild setting: "the apply job carries it out at its next run (<time>), no restart: <the diff line>";
- approve / profile: "Claude edits the game data for <version>, the tests run, and the bot restarts for every server it
  serves (<every name>)…" plus "⚠ <server> has a run right now — the job will wait" for each busy guild (D33);
- dismiss (D34: the same screen): "nothing changes, and the review is told not to propose it again"; Withdraw approval
  when it is approved (§2.7).

`[Approve] [Approve with a note…] [Back]`; the note's Submit IS the write. The press re-checks officer standing (and the
owner, for a game-data approval) on every press; the write is NewsMixin.decide_proposal, the one verb every surface calls.
A refusal (already decided by someone else — named with the time —, the owner-only rule, a shared game version) finishes
the screen AND redraws the card, so a stale card catches up. No edit-before-approve (D35)."""
from __future__ import annotations

import logging

import discord

from . import news
from .news import AlreadyDecided, ProposalError, ProposalStore
from .registry import Registry
from .wizard import Wizard, flow

log = logging.getLogger(__name__)

NOTE = {"label": "Note (optional)", "max_length": 300, "placeholder": "kept on the proposal and its card"}


def verb_of(p: news.Proposal, state: str) -> str:
    if state == "approved":
        return "Approve"
    return "Withdraw approval" if p.state == "approved" else "Dismiss"


async def _catch_up(bot, reg: Registry, pid: str) -> None:
    """Redraw the card after a refusal: whatever the store says now is what the card shows."""
    update = getattr(bot, "proposal_card_update", None)
    if update is None:
        return
    try:
        await update(reg, ProposalStore(bot.registries.store, reg.key).get(pid))
    except Exception:  # noqa: BLE001 — a vanished proposal or Discord down: the refusal still reached the presser
        log.debug("proposal card catch-up failed", exc_info=True)


@flow("proposal_decide")
async def proposal_decide(interaction: discord.Interaction, reg: Registry, *, pid: str, state: str, card=None, **_seed) -> None:
    """Approve / Dismiss / Withdraw approval of proposal `pid` (state approved | dismissed) on the decide screen."""
    bot = interaction.client
    uid = interaction.user.id
    if not await bot.is_officer_anywhere(reg, uid):
        await interaction.response.send_message("Officers only.", ephemeral=True)
        return
    store = bot.registries.store
    why = news.decision_refusal(store, reg, pid, state, uid, owner=reg.is_owner_id(uid))
    if why:
        await interaction.response.send_message(why, ephemeral=True)
        await _catch_up(bot, reg, pid)
        return
    p = ProposalStore(store, reg.key).get(pid)
    verb = verb_of(p, state)
    needs_owner = state == "approved" and p.kind == "profile"

    async def guard(i: discord.Interaction) -> str | None:
        if not await bot.is_officer_anywhere(reg, i.user.id):
            return "Officers only."
        if needs_owner and not reg.is_owner_id(i.user.id):
            return news.PROFILE_OWNER_ONLY[:1].upper() + news.PROFILE_OWNER_ONLY[1:] + "."
        return None

    wiz = Wizard(reg, uid, title=f"{verb}: proposal {p.id}", guard=guard)

    async def do(i: discord.Interaction, note: str | None) -> None:
        try:
            moved, card_ok = await bot.decide_proposal(reg, pid, state, by=i.user.display_name, by_id=i.user.id, officer=True,
                                                       owner=reg.is_owner_id(i.user.id), note=note)
        except AlreadyDecided as e:
            await wiz.finish(i, news.already_text(e.p))
            await _catch_up(bot, reg, pid)
            return
        except (ProposalError, PermissionError) as e:
            text = str(e)
            await wiz.finish(i, text[:1].upper() + text[1:])
            await _catch_up(bot, reg, pid)
            return
        from .discord_news import STATE_TEXT

        tail = "the card is updated." if card_ok else "saved; the card in the ops channel couldn't be updated (no ops channel, or Discord refused)."
        await wiz.finish(i, f"{STATE_TEXT.get(moved.state, moved.state)} — {tail}")

    text = "\n".join(bot.proposal_consequence(reg, p, state)) + f"\n-# {p.title[:150]}"
    await wiz.decide(interaction, text, do=do, note=NOTE, do_label=verb, danger=state == "dismissed" and p.state == "approved")
