"""The /raid slash commands (open, sheet, health, lock, loot, end, set, cancel, list, out, fill): thin handlers that
resolve the guild + run and call the `RaidMixin` methods on the bot. `register_raid_commands` adds the group to the tree."""
from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands

from . import raidcycle as rc
from .constants import TEAL
from .discord_registry import Guilds, is_officer
from .ops import Ops
from .raid_views import ask_line, gaps_text, health_layout, raid_name, run_label, run_title, sheet_message, sheet_state
from .registry import Registry


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

    async def run_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        live = bot.raids.store(reg).live() if reg else []
        return [app_commands.Choice(name=f"{run_title(reg, e)} · {sheet_state(e)}"[:100], value=e.team) for e in live if current.lower() in e.team.lower() or current.lower() in raid_name(reg, e).lower()][:25]

    async def raid_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=reg.raid_def(r).get("name", r), value=r) for r in (reg.profile.raids if reg else []) if current.lower() in r.lower() or current.lower() in reg.raid_def(r).get("name", "").lower()][:25]

    def current_event(reg: Registry, run: str | None):
        rs = bot.raids.store(reg)
        ev = rs.for_team(run) if run else next(iter(rs.live()), None)
        return rs, ev, (reg.config.roster(ev.team) if ev else None) or {"key": run or "?", "size": 20}

    raid = app_commands.Group(name="raid", description="Raid sheets and rosters")

    @raid.command(name="open", description="Officer: open a sheet now — the raid's next slot, or a one-off 'YYYY-MM-DD HH:MM'")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_open(interaction: discord.Interaction, raid: str, when: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        if raid not in reg.profile.raids:
            await interaction.response.send_message(f"Unknown raid. Options: {', '.join(reg.profile.raids)}", ephemeral=True)
            return
        now = reg.now_local()
        try:
            if when:
                start = datetime.fromisoformat(when.strip().replace(" ", "T", 1)).replace(tzinfo=reg.tz)
            else:
                nxt = rc.slot_starts(reg, raid, now, 24 * rc.OPEN_HORIZON_DAYS)
                if not nxt:
                    await interaction.response.send_message(f"{raid} has no slots yet (or none before it opens). Set them: `/gm config raid raid:{raid} slots:'Tue 19:30'` — or pass a date/time.", ephemeral=True)
                    return
                start = nxt[0][1]
        except ValueError:
            await interaction.response.send_message("Time looks like `2026-12-10 19:30` (guild time).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rs = bot.raids.store(reg)
        ev = await bot.open_run_and_post(reg, rs, raid, start, by=interaction.user.display_name)
        if not ev.message_id:  # no signup channel configured: the sheet goes where the officer asked
            await bot.post_sheet(reg, rs, ev, interaction.channel)
        await interaction.followup.send(f"Opened {run_label(reg, ev)} in <#{ev.channel_id}>", ephemeral=True)

    @raid.command(name="sheet", description="Re-post a live sheet")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_sheet(interaction: discord.Interaction, run: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live sheet.", ephemeral=True)
            return
        embed, view = sheet_message(reg, ev, t, bot.ico)
        await interaction.response.send_message(embed=embed, **({"view": view} if view else {}))
        msg = await interaction.original_response()
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")

    @raid.command(name="health", description="Roster health for a live sheet")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_health(interaction: discord.Interaction, run: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live sheet.", ephemeral=True)
            return
        if sheet_state(ev) != "open":
            await interaction.response.send_message(f"{run_label(reg, ev)} is {sheet_state(ev)} — the health check is for an open sheet; see its lock cards in the roster channel.", ephemeral=True)
            return
        await interaction.response.send_message(view=health_layout(reg, rs, ev, t, bot.ico), ephemeral=not is_officer(interaction, reg))

    @raid.command(name="lock", description="Officer: lock a sheet now — roster from the signups, confirmation DMs to everyone rostered")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_lock(interaction: discord.Interaction, run: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev or ev.state != "open":
            await interaction.response.send_message("No open sheet to lock.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        line = await bot.lock_run(reg, rs, ev, by=interaction.user.display_name)
        await interaction.followup.send(line, ephemeral=True)
        await ops.emit(reg.config, "info", line)

    @raid.command(name="loot", description="Officer: open the loot council thread for the locked roster")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_loot(interaction: discord.Interaction, run: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev or not ev.roster or ev.state == "open":
            await interaction.response.send_message("Lock the roster first (/raid lock).", ephemeral=True)
            return
        from .discord_bot import MockEvent as LootSession

        existing = next((s for s in bot.events.values() if s.id == ev.key and s.state == "raid"), None)
        if existing:
            await interaction.response.send_message(f"Loot thread already open: <#{existing.raid_thread_id}>", ephemeral=True)
            return
        instance = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
        players = rc.players_for(reg, ev)
        e = discord.Embed(title=f"⚔ {run_title(reg, ev)}", colour=TEAL, description=f"<t:{int(ev.start.timestamp())}:F>\nRoster ({len(ev.roster.selected)}): " + ", ".join(p.character or p.signup_name for p in ev.roster.selected)[:3800])
        e.set_footer(text="loot council: tick drops (or let the companion feed do it) → Distribute → chat to adjust → Confirm · /raid end for the summary")
        await interaction.response.send_message(embed=e)
        msg = await interaction.original_response()
        thread = await msg.create_thread(name=f"loot · {run_title(reg, ev)}"[:100])
        session = LootSession(id=ev.key, channel_id=thread.id, instance=instance, date=ev.start.date().isoformat(), guild=reg.key, origin="raid", signups=players, roster=ev.roster, state="raid", raid_thread_id=thread.id)
        bot.events[thread.id] = session
        ev.thread_id = thread.id
        rs.save(ev, "loot thread opened")
        session.save(f"{session.id}: loot session opened")
        await bot.post_boss_tables(session, thread)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} opened loot council for {ev.key}")

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

    from .discord_registry import register_commands as _rc

    @raid.command(name="set", description="Officer: set someone's answer on a sheet (join / bench / out)")
    @app_commands.choices(status=[app_commands.Choice(name=rc.LABELS[s], value=s) for s in rc.STATUSES])
    @app_commands.autocomplete(run=run_autocomplete, character=_rc.member_char_autocomplete)
    async def raid_set(interaction: discord.Interaction, member: discord.User, status: app_commands.Choice[str], character: str | None = None, run: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        m = reg.members.get(member.id)
        if not ev or not m:
            await interaction.response.send_message("No live sheet, or that member isn't registered.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            line = await bot.set_answer(reg, rs, ev, m, status.value, character, by=interaction.user.display_name)
        except ValueError as e:
            line = f"❌ {e}"
        await interaction.followup.send(line, ephemeral=True)

    @raid.command(name="cancel", description="Officer: cancel a run")
    @app_commands.autocomplete(run=run_autocomplete)
    async def raid_cancel(interaction: discord.Interaction, run: str | None = None, reason: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live sheet.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        line = await bot.cancel_run(reg, rs, ev, by=interaction.user.display_name, reason=reason)
        await interaction.followup.send(line)

    @raid.command(name="list", description="Live runs")
    async def raid_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        rs = bot.raids.store(reg)
        live = rs.live()
        await interaction.response.send_message("\n".join(f"• {run_label(reg, e)} · {sheet_state(e)} · {len(e.seated()) if sheet_state(e) == 'locked' else len(e.by_status('in'))} {'rostered' if sheet_state(e) == 'locked' else 'joined'}" for e in live) or "No live runs.", ephemeral=True)

    @raid.command(name="out", description="Can't make a run you joined (frees your seat if the roster is locked)")
    @app_commands.autocomplete(run=run_autocomplete)
    async def callout_cmd(interaction: discord.Interaction, note: str | None = None, run: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        m = reg.members.get(interaction.user.id)
        if not ev or not m:
            await interaction.response.send_message("No live run, or you're not registered.", ephemeral=True)
            return
        co = rc.callout(reg, rs, ev, m, t, note)
        await interaction.response.send_message(f"Noted: out for {run_label(reg, ev)} ({co.hours_before:.0f}h before{', after lock' if co.late else ''}). Thanks for saying so.", ephemeral=True)
        if sheet_state(ev) == "locked" and ev.seat_of(m.display_name):
            await bot.drop_seated(reg, rs, ev, t, m, "callout", note)
            return
        await bot.refresh_sheet(reg, ev)
        # the note is for the officers only: nothing in the public signup channel, one line in the run's updates thread
        await bot.post_run_update(reg, ev, f"⚑ {m.display_name} ({co.character}) called out, {co.hours_before:.0f}h before")
        await ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {ev.key} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else ""))

    @raid.command(name="fill", description="Officer: ask the next best people to cover a sheet's gaps (bench, pool, offspec/alt)")
    @app_commands.autocomplete(run=run_autocomplete)
    @app_commands.describe(preview="only show who would be asked")
    async def raid_fill(interaction: discord.Interaction, run: str | None = None, preview: bool = False):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, run)
        if not ev:
            await interaction.response.send_message("No live run.", ephemeral=True)
            return
        if sheet_state(ev) != "locked" or not ev.all_rosters:
            await interaction.response.send_message("Fill works after lock — the sheet is still open.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        nd = rc.needs(reg, ev, t)
        gaps = f"short {gaps_text(bot.ico, nd)}" if nd["headcount"] or nd["roles"] else "nothing short"
        busy = rc.conflicts(rs, ev)
        if preview:
            cands = await asyncio.to_thread(rc.fill_candidates, reg, rs, ev, t)
            lines = [f"{i + 1}. " + ask_line(reg, bot.ico, a) + (" — tied to " + next((b.display_name for b in cands if b.discord_id == a.pair), "?") if a.pair else "") for i, a in enumerate(cands[:15])]
            open_asks = [a for a in ev.fill_asks if a.open]
            await interaction.followup.send(f"{run_label(reg, ev)} · {gaps}" + (f" · double-booked: {', '.join(reg.members[u].display_name for u in busy if u in reg.members)}" if busy else "") + f"\nOutstanding asks: {', '.join(a.display_name for a in open_asks) or 'none'}\nWould ask next:\n" + ("\n".join(lines) or "nobody left"), ephemeral=True)
            return
        sent, nd = await bot.run_fill(reg, rs, ev, t, by=interaction.user.display_name)
        if sent:
            await bot.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, bot.ico, a) for a in sent))
        await interaction.followup.send(f"{run_label(reg, ev)} · {gaps}\n" + ("Asked: " + ", ".join(ask_line(reg, bot.ico, a) for a in sent) if sent else ("Nothing to fill." if not (nd["headcount"] or nd["roles"]) else "Nobody left to ask (or the asks outstanding already cover it).")), ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} ran fill for {ev.key}: asked {len(sent)}")

    tree.add_command(raid)
