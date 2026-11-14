"""/price (price watch spec §4.4): the flow `price`, one ephemeral answer the asker alone can press.

- With an item: the lines of prices.price_answer exactly as /price always answered, and — when the answer names one
  item (a picked '#<id>' of a named item, or typed text the search matches once) while prices show — a Watch / Stop
  watching button. A press puts prices.watch / unwatch's sentence first and flips the button; a refusal (not
  registered, a full list) is that sentence with the button unchanged.
- With no item: Your watch list — a line per item (prices.price_words while prices show, else the name alone), the
  status sentence once (Discord's own stamp), one select of the items. Picking one shows its price line, its history
  sentences (prices.history_sentences, read in a thread), Stop watching and Back.

Watching is instant (C8): no confirm screen. Nothing here pings, DMs or posts; no model call; no Blizzard call (prices
and names come from memory, the history from the local file). The screens are the wizard core's, so a restart edits an
open one to "The bot restarted; nothing was changed" (or "what you saved stays" after a press wrote) and no button
survives it (C18). Item names go through escape_markdown; every screen is cut by whole lines (flows_armory.bounded)."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence

import discord

from . import auctions, discord_auctions, prices
from .flows_armory import bounded
from .registry import WATCH_REGISTER_FIRST, Registry, RegistryError
from .wizard import Opt, Wizard, flow

WATCH = "Watch"
STOP = "Stop watching"
BACK = "Back"
LIST_HEADING = "**Your watch list**"
PICK = "Pick an item"
SCREEN_MAX = 2000  # Discord's message limit


def _esc(text: str) -> str:
    return discord.utils.escape_markdown(str(text))


def _shown(reg: Registry) -> bool:
    return discord_auctions.state(reg) in ("fresh", "stale")


def _registered(reg: Registry, uid: int) -> bool:
    m = reg.members.get(int(uid))
    return m is not None and not m.test


def _one_item(reg: Registry, text: str) -> int | None:
    """The one item an answer names — a picked '#<id>' the bot can name, or typed text the search matches exactly once —
    while prices show; else None (no button)."""
    if not _shown(reg):
        return None
    if text.startswith("#") and text[1:].isdigit():
        return int(text[1:]) if prices.item_price(reg, int(text[1:])) else None
    shown, total = prices.search(reg, text, 1)
    return shown[0].id if total == 1 and shown else None


def _item(reg: Registry, item_id: int) -> prices.ItemPrice:
    """A watched item priced now (none listed while prices are hidden); an id the bot can't name reads UNNAMED."""
    p = discord_auctions.price_of(reg, item_id)
    name = prices.names(reg).get(int(item_id)) or prices.UNNAMED
    return prices.ItemPrice(id=int(item_id), name=name, low=p.low if p else None, qty=p.qty if p else 0)


class PriceWizard(Wizard):
    heading = False  # the answer is its lines alone, as /price always read
    mentions = discord.AllowedMentions.none()

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Price")
        self.owner = reg.is_owner_id(owner_id)

    async def on_timeout(self) -> None:
        """An expired answer keeps its words and loses its buttons (a watch it made stays: never "nothing was changed")."""
        from .pending_views import untrack

        untrack(f"wiz:{id(self)}")
        if self._closed or not self._last:
            return
        try:
            await self._last.edit_original_response(view=None)
        except discord.HTTPException:
            pass

    def _screen(self, head: list[str], items: Sequence[str] = (), noun: str = "") -> str:
        head = [x for x in head if x]
        room = SCREEN_MAX - sum(len(x) + 1 for x in head)
        return "\n".join([*head, *bounded(list(items), room, noun)])

    async def _toggle(self, item_id: int, on: bool) -> str:
        """Watch or stop watching (one quiet commit, in a thread) → the sentence; a refusal is its sentence."""
        verb = prices.watch if on else prices.unwatch
        try:
            said = await asyncio.to_thread(verb, self.reg, self.owner_id, item_id)
        except RegistryError as e:
            return str(e)
        self.mark_saved()
        return said

    # ---- /price <item>
    async def answer(self, interaction: discord.Interaction, text: str, item_id: int | None, said: str = "") -> None:
        reg = self.reg
        lines = prices.price_answer(reg, text, self.owner)
        if not _shown(reg) or item_id is None:  # nothing to press: the answer alone (a press's sentence still first)
            await self.finish(interaction, self._screen([_esc(said), *lines]))
            return
        lines = [lines[0], *(_esc(x) for x in lines[1:])]  # the status line is ours; the rest carry item names
        watching = prices.is_watching(reg, self.owner_id, item_id)

        async def press(i: discord.Interaction) -> None:
            if not watching and not _shown(self.reg):  # prices hidden since the answer: items are added only while they show
                await self.answer(i, text, item_id)
                return
            await self.answer(i, text, item_id, await self._toggle(item_id, not watching))

        b = self.button(STOP if watching else WATCH, press, discord.ButtonStyle.secondary if watching else discord.ButtonStyle.primary)
        await self.show(interaction, self._screen([_esc(said), *lines]), b)

    # ---- /price alone
    async def watch_list(self, interaction: discord.Interaction) -> None:
        reg = self.reg
        rows = prices.watch_list(reg, self.owner_id)
        if not rows:
            await self.finish(interaction, prices.EMPTY_LIST_DISCORD if _registered(reg, self.owner_id) else WATCH_REGISTER_FIRST)
            return
        shown = _shown(reg)
        items = [_esc(prices.price_words(p) if shown else p.name) for p in rows]
        text = self._screen([LIST_HEADING, prices.status_sentence(reg, self.owner, stamp=True)], items, " items")

        async def picked(i: discord.Interaction, values: list[str]) -> None:
            await self.item(i, int(values[0]))

        await self.show(interaction, text, self.select(PICK, [Opt(value=str(p.id), label=p.name) for p in rows], picked))

    async def item(self, interaction: discord.Interaction, item_id: int, said: str = "") -> None:
        reg = self.reg
        p = _item(reg, item_id)
        shown = _shown(reg)
        history = await asyncio.to_thread(prices.history_sentences, reg, item_id) if shown else []  # a SQLite read: ms
        watching = prices.is_watching(reg, self.owner_id, item_id)

        async def stop(i: discord.Interaction) -> None:
            await self.item(i, item_id, await self._toggle(item_id, False))

        async def watch(i: discord.Interaction) -> None:
            await self.item(i, item_id, await self._toggle(item_id, True) if _shown(self.reg) else "")

        async def back(i: discord.Interaction) -> None:
            await self.watch_list(i)

        buttons = []
        if watching:
            buttons.append(self.button(STOP, stop))
        elif shown and p.name != prices.UNNAMED:  # watch again; an item the bot can't name can't be watched
            buttons.append(self.button(WATCH, watch, discord.ButtonStyle.primary))
        buttons.append(self.button(BACK, back))
        line = _esc(prices.price_words(p) if shown else p.name)
        status = prices.status_sentence(reg, self.owner, stamp=True)
        await self.show(interaction, self._screen([_esc(said), line, *history, status]), *buttons)


@flow("price")
async def open_price(interaction: discord.Interaction, reg: Registry, *, item: str | None = None) -> None:
    """/price [item]: with an item, its price and Watch; with none, the asker's watch list."""
    w = PriceWizard(reg, interaction.user.id)
    if not auctions.available(reg):  # as /price always said: no auction houses, or no keys to read them
        await w.finish(interaction, prices.NO_AUCTIONS_WORDS)
        return
    text = str(item or "").strip()
    if text:
        await w.answer(interaction, text, _one_item(reg, text))
        return
    await w.watch_list(interaction)
