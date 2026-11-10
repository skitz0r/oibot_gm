"""/guide list|get|upload|withdraw|restore: the guild guide library's commands (design
§5.63). Each command resolves its guild (guild_routing.need) and
opens the flow in flows_guides.py; the attachment and the faction are Discord widgets, the BattleTag is typed only in the
flows' private modal. Members list, get and upload; withdraw and restore are officers', decided live in the flow."""
from __future__ import annotations

import discord
from discord import app_commands

from . import guild_routing
from .guides import library as lib
from .wizard import FLOWS

FACTION_CHOICES = [app_commands.Choice(name=lib.FACTION_WORDS[f], value=f) for f in lib.FACTIONS]


def register_guide_commands(tree: app_commands.CommandTree, guilds) -> None:
    group = app_commands.Group(name="guide", description="Guild guides: the guild's own leveling guides, locked to your BattleTag")

    async def need(interaction: discord.Interaction):
        return await guild_routing.need(interaction, guilds=guilds)

    @group.command(name="list", description="The guild's own leveling guides: each faction's latest and older versions")
    async def guide_list(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["guide_list"](interaction, reg)

    @group.command(name="get", description="Get a guild guide locked to your BattleTag, sent to you by DM")
    async def guide_get(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["guide_get"](interaction, reg)

    @group.command(name="upload", description="Upload a guild guide: the RestedXP import string as a .txt file")
    @app_commands.describe(file="the import string saved as a .txt file", faction="which faction's line it belongs to")
    @app_commands.choices(faction=FACTION_CHOICES)
    async def guide_upload(interaction: discord.Interaction, file: discord.Attachment, faction: app_commands.Choice[str]):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["guide_upload"](interaction, reg, file=file, faction=faction.value)

    @group.command(name="withdraw", description="Officers: take a guild guide version out of members' reach")
    async def guide_withdraw(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["guide_withdraw"](interaction, reg)

    @group.command(name="restore", description="Officers: bring a withdrawn guild guide version back")
    async def guide_restore(interaction: discord.Interaction):
        reg, interaction = await need(interaction)
        if reg:
            await FLOWS["guide_restore"](interaction, reg)

    tree.add_command(group)


__all__ = ["FACTION_CHOICES", "register_guide_commands"]
