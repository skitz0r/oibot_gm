"""The DM server picker (design §5.32 "DMs and buttons"): when someone who belongs to several served guilds DMs the bot
(plain text, a slash command used in DMs, an old-format button), the bot asks which server first — buttons, every time,
never remembered (owner decision D2) — and then handles the original message or command for the chosen guild.

Non-persistent on purpose: a picker that outlives a restart simply stops answering, which is fine for a two-minute
question. Server names are data: escaped in the text and cut to Discord's 80-character button label."""
from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger(__name__)

PROMPT = "You're in more than one server I look after. Which one is this for?"
OFF_MARK = " (plain text off here)"  # D41: a server whose plain-text policy ignores DMs is listed, marked — never hidden
EXPIRED = "No server picked. Ask again any time."
PICKER_TIMEOUT_S = 120
MAX_LABEL = 80
EDIT_WAIT_S = 1.0


def guild_name(reg) -> str:
    """A guild's name as text a person reads (markdown escaped: a server name is member text)."""
    return discord.utils.escape_markdown(str(reg.config.name))


class GuildPicker(discord.ui.View):
    """One button per guild (in Guilds.ordered() order, at most 25); only the person asked may press. The two-minute
    limit is choose_guild's own (`wait`): it stops the view (out of discord.py's store) and edits the question."""

    def __init__(self, regs: list, asker_id: int, marks: dict[str, str] | None = None):
        super().__init__(timeout=None)
        self.asker_id = asker_id
        self.chosen = None
        self.press: discord.Interaction | None = None
        self.message: discord.Message | None = None
        marks = marks or {}
        for reg in regs[:25]:
            mark = marks.get(reg.key, "")
            name = str(reg.config.name)[:MAX_LABEL - len(mark)] or reg.key
            btn = discord.ui.Button(label=(name + mark)[:MAX_LABEL], style=discord.ButtonStyle.secondary if mark else discord.ButtonStyle.primary)
            btn.callback = self._picked(reg)
            self.add_item(btn)

    def _picked(self, reg):
        async def cb(interaction: discord.Interaction):
            if self.chosen is not None:  # a double press: the first one counts
                return
            self.chosen, self.press = reg, interaction
            self.stop()
        return cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.asker_id:
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return False
        return True

    async def wait_for_pick(self, timeout: float, expire) -> bool:
        """True when someone picked within `timeout`; otherwise stop listening and run `expire` (edit the question)."""
        try:
            await asyncio.wait_for(self.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        if self.chosen is not None:
            return True
        self.stop()
        try:
            await expire()
        except discord.HTTPException as e:
            log.debug("picker expiry edit failed: %s", e)
        return False


async def choose_guild(target, regs: list, prompt: str = PROMPT, timeout: float = PICKER_TIMEOUT_S, marks: dict[str, str] | None = None):
    """Ask which guild; return (reg, press) — the press interaction the caller continues on (unanswered for a slash
    command or a button, so the command answers it; already acknowledged for a plain message) — or (None, None) when
    nobody picked in time.

    `target` is an Interaction (a slash command or a button pressed in a DM: the picker is its ephemeral response) or a
    Message (a plain DM: the picker is a reply)."""
    if isinstance(target, discord.Message) or not hasattr(target, "response"):
        asker = target.author.id
        view = GuildPicker(regs, asker, marks)
        msg = await target.reply(prompt, view=view, allowed_mentions=discord.AllowedMentions.none())
        view.message = msg

        async def expire():
            await msg.edit(content=EXPIRED, view=None)
        if not await view.wait_for_pick(timeout, expire):
            return None, None
        try:
            await view.press.response.edit_message(content=f"For **{guild_name(view.chosen)}**:", view=None)
        except discord.HTTPException as e:
            log.debug("picker ack failed: %s", e)
        return view.chosen, view.press
    view = GuildPicker(regs, target.user.id, marks)
    sent = None
    if target.response.is_done():
        sent = await target.followup.send(prompt, view=view, ephemeral=True, wait=True)
    else:
        await target.response.send_message(prompt, view=view, ephemeral=True)

    async def edit(content):
        if sent is not None:
            await sent.edit(content=content, view=None)
        else:
            await target.edit_original_response(content=content, view=None)

    async def expire():
        await edit(EXPIRED)
    if not await view.wait_for_pick(timeout, expire):
        return None, None
    try:  # the question stays readable without its buttons; the command answers the press itself — within Discord's
        # three seconds, so this edit never waits long
        await asyncio.wait_for(edit(f"For **{guild_name(view.chosen)}**:"), EDIT_WAIT_S)
    except (discord.HTTPException, asyncio.TimeoutError) as e:
        log.debug("picker edit failed: %s", e)
    return view.chosen, view.press
