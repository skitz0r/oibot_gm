"""/help (no LLM: the command list by tier) and /ask (Claude, with the manual and live state in context).
@mentioning the bot anywhere except the ops/analytics channels — or DMing it — is the same as /ask."""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from . import help as help_mod
from .discord_registry import Guilds, is_officer
from .ops import Ops

TEAL = 0x2B7A78
TOPICS = {
    "register": "## 3", "characters": "## 3", "availability": "## 3", "absences": "## 3",
    "rosters": "## 4", "sheets": "## 4", "fill": "## 4", "cycle": "## 4", "groups": "## 5", "analytics": "## 6",
    "officers": "## 7", "setup": "## 8", "loot": "## 9", "data": "## 10", "limits": "## 11", "channels": "## 2",
}


def manual_section(marker: str) -> str:
    text = help_mod.MANUAL.read_text() if help_mod.MANUAL.exists() else ""
    parts = text.split("\n## ")
    for p in parts:
        if ("## " + p).startswith(marker + " ") or ("## " + p).startswith(marker + "."):
            return "## " + p
    return ""


def register_help_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot) -> None:
    @tree.command(name="help", description="What the bot does and which commands you can use (no AI)")
    @app_commands.describe(topic="register, characters, availability, rosters, sheets, fill, groups, analytics, officers, setup, loot, limits")
    async def help_cmd(interaction: discord.Interaction, topic: str | None = None):
        reg = guilds.for_interaction(interaction)
        officer = bool(reg and is_officer(interaction, reg))
        if topic:
            key = topic.strip().lower()
            marker = TOPICS.get(key)
            text = manual_section(marker) if marker else ""
            if not text:
                await interaction.response.send_message("Topics: " + ", ".join(sorted(set(TOPICS))) + ". Or just ask: `/ask how do I …`", ephemeral=True)
                return
            await interaction.response.send_message(text[:1950], ephemeral=True)
            return
        lines = help_mod.command_lines(tree).splitlines()
        tiers = {"member": [], "officer": [], "owner": []}
        for l in lines:
            path, _, rest = l.partition(" — ")
            desc, _, who = rest.rpartition(" — ")
            if path.startswith("/mock"):
                continue
            tiers.setdefault(who, []).append(f"`{path}` — {desc}")
        e = discord.Embed(title="oibot_GM — what I do", colour=TEAL, description="I run registration, the weekly sheets, roster health, groups, gap-filling by DM, and (later) the loot council. Ask me anything in plain words with `/ask`, or @mention me. `/help topic:<name>` shows one part of the manual.")
        e.add_field(name="Everyone", value="\n".join(tiers["member"])[:1024], inline=False)
        if officer:
            e.add_field(name="Officers", value="\n".join(tiers["officer"])[:1024], inline=False)
            e.add_field(name="Owner", value="\n".join(tiers["owner"])[:1024], inline=False)
        e.set_footer(text="Buttons: #register card (Register / Add an alt / My status) · sheets (In / Tentative / Sub / Out) · fill DMs (Yes / Can't)")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @tree.command(name="ask", description="Ask the bot how something works or what to do next (it answers from its own manual and your record)")
    async def ask_cmd(interaction: discord.Interaction, question: str):
        reg = guilds.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await bot.help_answer(reg, interaction.user.id, is_officer(interaction, reg), question)
        await interaction.followup.send(text, ephemeral=True)


class HelpMixin:
    """Needs self.ctx.provider, self.tree, self.raids, self.ops."""

    async def help_answer(self, reg, user_id: int, officer: bool, question: str) -> str:
        provider = self.ctx.provider
        if provider is None:
            return "I can't answer free-form questions right now (no LLM configured). `/help` lists what I do."
        rs = self.raids.store(reg)
        try:
            ans = await asyncio.to_thread(help_mod.answer, provider, self.tree, reg, rs, user_id, officer, question)
        except Exception as e:  # noqa: BLE001
            await self.ops.emit(reg.config, "warn", f"help answer failed: {type(e).__name__}: {str(e)[:120]}")
            return "Something went wrong answering that; the officers have been told. `/help` lists what I do."
        text = ans.answer.strip()
        if ans.commands:
            text += "\n\n" + " · ".join(f"`{c}`" for c in ans.commands[:4])
        if ans.for_officer:
            text += "\n-# This needs an officer."
        return text[:1950]
