"""The /raid slash commands (open, sheet, health, lock, loot, end, set, cancel, list, out, fill): thin handlers that
resolve the guild + run and call the `RaidMixin` methods on the bot. `register_raid_commands` adds the group to the tree."""
from __future__ import annotations

import discord
from discord import app_commands

from . import raidcycle as rc
from .discord_registry import Guilds, is_officer
from .ops import Ops
from .raid_views import run_label, sheet_state
from .registry import Registry
from .wizard_flows import FLOWS


def register_raid_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot) -> None:
    async def need(interaction: discord.Interaction) -> Registry | None:
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
        return reg

    async def officer(interaction: discord.Interaction) -> Registry | None:
        reg = await need(interaction)
        if reg and not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return None
        return reg

    raid = app_commands.Group(name="raid", description="Raid sheets and rosters")

    @raid.command(name="open", description="Officer: open a sheet — the raid's next run, another upcoming one, or a day and time you pick")
    async def raid_open(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["raid_open"](interaction, reg)

    @raid.command(name="sheet", description="Re-post a live sheet in this channel")
    async def raid_sheet(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["raid_sheet"](interaction, reg)

    @raid.command(name="health", description="Roster health for an open sheet")
    async def raid_health(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["raid_health"](interaction, reg)

    @raid.command(name="lock", description="Officer: lock a sheet now — roster from the signups, confirmation DMs to everyone rostered")
    async def raid_lock(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["raid_lock"](interaction, reg)

    @raid.command(name="loot", description="Officer: open the loot council thread for the locked roster")
    async def raid_loot(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["raid_loot"](interaction, reg)

    @raid.command(name="end", description="Officer: close the loot council and summarise awards")
    async def raid_end(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        session = bot.event_for(interaction.channel_id)
        if not session or session.origin != "raid":
            await interaction.response.send_message("Run this in the raid's loot thread.", ephemeral=True)
            return
        await bot.end_session(session, interaction)
        rs = bot.raids.store(reg)
        ev = rs.events.get(session.id)
        if ev and ev.state != "done":
            ev.state = "done"
            rs.save(ev, "done (loot closed)")
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} closed loot council {session.id}: {len(session.awards)} awards, {len(session.overrides)} overrides")

    @raid.command(name="set", description="Officer: set someone's answer on a sheet (join / bench / out)")
    @app_commands.choices(status=[app_commands.Choice(name=rc.LABELS[s], value=s) for s in rc.STATUSES])
    async def raid_set(interaction: discord.Interaction, member: discord.User, status: app_commands.Choice[str]):
        reg = await officer(interaction)
        if reg:
            await FLOWS["raid_set"](interaction, reg, member=member, status=status.value)

    @raid.command(name="cancel", description="Officer: cancel a run (an optional reason goes in a box)")
    async def raid_cancel(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["raid_cancel"](interaction, reg)

    @raid.command(name="list", description="Live runs")
    async def raid_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        rs = bot.raids.store(reg)
        live = rs.live()
        await interaction.response.send_message("\n".join(f"• {run_label(reg, e)} · {sheet_state(e)} · {len(e.seated()) if sheet_state(e) == 'locked' else len(e.by_status('in'))} {'rostered' if sheet_state(e) == 'locked' else 'joined'}" for e in live) or "No live runs.", ephemeral=True)

    @raid.command(name="out", description="Can't make a run you joined (frees your seat if the roster is locked)")
    async def callout_cmd(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["raid_out"](interaction, reg)

    @raid.command(name="fill", description="Officer: ask the next best people to cover a sheet's gaps (bench, pool, offspec/alt)")
    @app_commands.describe(preview="only show who would be asked")
    async def raid_fill(interaction: discord.Interaction, preview: bool = False):
        reg = await officer(interaction)
        if reg:
            await FLOWS["raid_fill"](interaction, reg, preview=preview)

    tree.add_command(raid)
