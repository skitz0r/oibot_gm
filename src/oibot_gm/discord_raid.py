"""Discord surface for the weekly cycle: signup sheets with persistent buttons,
/raid commands, /callout, and the scheduler loop."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from . import raidcycle as rc, render
from .discord_registry import Guilds, is_officer
from .ops import Ops
from .registry import Registry, RegistryError

TEAL = 0x2B7A78
LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}


class RaidContext:
    """Per-guild raid store + helpers, attached to the bot."""

    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, rc.RaidStore] = {k: rc.RaidStore(guilds.store, reg.key) for k, reg in ((r.key, r) for r in guilds.by_discord.values())}

    def store(self, reg: Registry) -> rc.RaidStore:
        return self.stores.setdefault(reg.key, rc.RaidStore(self.guilds.store, reg.key))


# ---------------------------------------------------------------- rendering

def sheet_embed(reg: Registry, ev: rc.RaidEvent, team: dict, ico) -> discord.Embed:
    start = ev.start
    unix = int(start.timestamp())
    ins, tent, subs, outs = (ev.by_status(s) for s in ("in", "tentative", "sub", "out"))
    counts = {r: sum(1 for s in ins if s.role == r) for r in ("tank", "healer", "melee", "ranged")}
    inst = reg.profile.raids.get(ev.instance or "", {}).get("name", ev.instance or "raid")
    e = discord.Embed(title=f"{team.get('name', team['key'])} · {inst} · <t:{unix}:D>", colour=TEAL,
                      description=f"<t:{unix}:t> server (<t:{unix}:R>) · **{len(ins)}**/{team.get('size', 20)} in · {len(tent)} tentative · {len(subs)} sub · " + " · ".join(f"{ico('role', r)} {n}" for r, n in counts.items()))
    by_cls: dict[str, list[str]] = {}
    for s in ins:
        by_cls.setdefault(s.cls, []).append(f"{ico('role', s.role)} **{s.character}** · {s.spec}" + (" ᵖ" if s.source == "prefill" else ""))
    for cls, lines in sorted(by_cls.items()):
        e.add_field(name=f"{ico('class', cls)} {cls} ({len(lines)})", value="\n".join(lines)[:1000], inline=True)
    if tent:
        e.add_field(name=f"Tentative ({len(tent)})", value=", ".join(f"{s.character} ({s.spec})" for s in tent)[:1000], inline=False)
    if subs:
        e.add_field(name=f"Sub ({len(subs)})", value=", ".join(f"{s.character} ({s.spec})" for s in subs)[:1000], inline=False)
    if outs:
        e.add_field(name=f"Out ({len(outs)})", value=", ".join(s.character + (" ⚑" if s.source == "callout" else "") for s in outs)[:1000], inline=False)
    hard = start - timedelta(hours=rc.team_setting(team, "cutoff_hard_hours"))
    e.set_footer(text=f"{ev.key} · {ev.state} · locks {hard.strftime('%a %H:%M')} server · ᵖ prefilled from availability · buttons use your main; pick an alt from the menu")
    return e


def health_embed(reg: Registry, ev: rc.RaidEvent, team: dict) -> discord.Embed:
    rows = rc.health(reg, ev, team)
    worst = "red" if any(l == "red" for l, _ in rows) else ("amber" if any(l == "amber" for l, _ in rows) else "green")
    colour = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]
    e = discord.Embed(title=f"Roster health · {ev.key}", colour=colour, description="\n".join(f"{LEVEL_DOT[l]} {t}" for l, t in rows))
    return e


# ---------------------------------------------------------------- persistent buttons

class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid:(?P<key>[A-Za-z0-9_\-]+):(?P<status>in|tentative|out|sub)"):
    LABELS = {"in": ("In", discord.ButtonStyle.success), "tentative": ("Tentative", discord.ButtonStyle.primary), "out": ("Out", discord.ButtonStyle.secondary), "sub": ("Sub only", discord.ButtonStyle.secondary)}

    def __init__(self, key: str, status: str):
        label, style = self.LABELS[status]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"raid:{key}:{status}"))
        self.key, self.status = key, status

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], match["status"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("This sheet is closed.", ephemeral=True)
            return
        m = reg.members.get(interaction.user.id)
        if not m or not m.active():
            await interaction.response.send_message("Register a character first: `/register`.", ephemeral=True)
            return
        if ev.state != "open" and self.status != "out":
            await interaction.response.send_message("Signups are locked; use `/callout` if you can't make it, or ask an officer.", ephemeral=True)
            return
        chars = m.active()
        if len(chars) > 1 and self.status in ("in", "tentative", "sub"):
            view = discord.ui.View(timeout=120)
            sel = discord.ui.Select(placeholder="Which character?", options=[discord.SelectOption(label=f"{c.label} · {c.cls} {c.spec}", value=c.name or c.label, default=c.is_main) for c in chars[:25]])

            async def pick(i: discord.Interaction):
                await bot.apply_signup(i, reg, rs, ev, m, sel.values[0], self.status)

            sel.callback = pick
            view.add_item(sel)
            await interaction.response.send_message("Which character?", view=view, ephemeral=True)
            return
        await bot.apply_signup(interaction, reg, rs, ev, m, None, self.status)


def sheet_view(key: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for s in ("in", "tentative", "sub", "out"):
        v.add_item(SignupButton(key, s))
    return v


# ---------------------------------------------------------------- bot mixin

class RaidMixin:
    """Methods the bot needs; mixed into OibotGM."""

    async def apply_signup(self, interaction: discord.Interaction, reg, rs, ev, m, character, status):
        try:
            s = rc.set_signup(reg, rs, ev, m, character, status)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        msg = f"✅ {s.character}: **{status}** for {ev.key}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        await self.refresh_sheet(reg, ev)

    async def refresh_sheet(self, reg, ev) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        if ev.channel_id and ev.message_id:
            ch = self.get_channel(ev.channel_id)
            try:
                msg = await ch.fetch_message(ev.message_id)
                await msg.edit(embed=sheet_embed(reg, ev, team, self.ico), view=sheet_view(ev.key) if ev.state == "open" else None)
            except Exception as e:  # noqa: BLE001
                print(f"sheet refresh failed: {e}")

    async def post_sheet(self, reg, rs, ev, channel) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        msg = await channel.send(embed=sheet_embed(reg, ev, team, self.ico), view=sheet_view(ev.key))
        ev.channel_id, ev.message_id = channel.id, msg.id
        rs.save(ev, "sheet posted")

    async def post_health(self, reg, rs, ev, channel, nudge: bool) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        await channel.send(embed=health_embed(reg, ev, team))
        if nudge and rc.team_setting(team, "reminders") != "none":
            targets = [m for m in reg.members.values() if m.main and str(m.discord_id) not in ev.signups and m.discord_id not in ev.nudged and not m.dm_opt_out]
            unix = int(ev.start.timestamp())
            for m in targets:
                try:
                    user = await self.fetch_user(m.discord_id)
                    await user.send(f"{reg.config.name}: the {team.get('name', ev.team)} sheet for <t:{unix}:F> is still waiting for you. Reply on the sheet in <#{ev.channel_id}>: In / Tentative / Out / Sub.")
                    ev.nudged.append(m.discord_id)
                except Exception:  # noqa: BLE001
                    pass
            if targets:
                rs.save(ev, f"nudged {len(targets)}")
        ev.health_posted = True
        rs.save(ev, "health posted")

    async def lock_and_propose(self, reg, rs, ev, channel) -> None:
        ev.state = "locked"
        rs.save(ev, "locked")
        await self.refresh_sheet(reg, ev)
        players, result = await asyncio.to_thread(rc.propose, reg, rs, ev)
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        png = await asyncio.to_thread(render.roster_png, reg.profile, players, result, f"{team.get('name', ev.team)} · {ev.key}", f"{len(result.selected)} in · synergy {result.synergy_value}")
        e = discord.Embed(title=f"Proposed roster · {ev.key}", colour=TEAL, description=f"{len(result.selected)} in · " + " · ".join(f"{self.ico('role', k)} {v}" for k, v in result.role_counts.items()) + f" · synergy **{result.synergy_value}**")
        e.set_image(url="attachment://roster.png")
        e.add_field(name="Bench", value=", ".join(f"{p.character} ({p.spec})" for p in result.benched) or "nobody", inline=False)
        if result.advisories:
            e.add_field(name="Advisories", value="\n".join(f"• {a[:300]}" for a in result.advisories[:4])[:1000], inline=False)
        e.set_footer(text="Officers: /raid accept to lock the roster, or /raid lock again after changes")
        await channel.send(embed=e, file=discord.File(BytesIO(png), filename="roster.png"))

    async def scheduler(self) -> None:
        """Every minute: open sheets, post health/nudges, lock at the hard cutoff, close after the raid."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.scheduler_tick()
            except Exception as e:  # noqa: BLE001
                print(f"scheduler error: {e}")
            await asyncio.sleep(60)

    async def scheduler_tick(self) -> None:
        # companion presence during an active mock raid
        feed = getattr(self, "feed", None)
        raid = next((e for e in self.events.values() if e.state == "raid"), None)
        if feed and raid:
            for c in list(feed.companions.values()):
                if c.silent_for > 300 and not getattr(c, "warned", False):
                    c.warned = True
                    cfg = next(iter(self.registries.by_discord.values())).config if self.registries.by_discord else None
                    if cfg:
                        await self.ops.emit(cfg, "warn", f"companion {c.character} silent for {int(c.silent_for // 60)} min during {raid.id} — is /chatlog on?")
                elif c.silent_for <= 300:
                    c.warned = False
        for reg in self.registries.by_discord.values():
            cfg = reg.config
            rs = self.raids.store(reg)
            channel = self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None
            for team in cfg.raid_teams:
                if not team.get("schedule"):
                    continue
                try:
                    nxt = rc.next_raid_time(team["schedule"], cfg.timezone)
                except ValueError:
                    continue
                now = datetime.now(ZoneInfo(cfg.timezone))
                key = f"{team['key']}-{nxt.date().isoformat()}"
                if key not in rs.events and channel and now >= nxt - timedelta(days=rc.team_setting(team, "open_days_before")):
                    ev = rc.open_event(reg, rs, team, nxt)
                    await self.post_sheet(reg, rs, ev, channel)
                    await self.ops.emit(cfg, "info", f"opened sheet {ev.key} ({len(ev.signups)} prefilled)")
            for ev in rs.live():
                team = cfg.team(ev.team) or {"key": ev.team, "size": 20}
                now = datetime.now(ev.start.tzinfo)
                ch = self.get_channel(ev.channel_id) if ev.channel_id else channel
                if not ch:
                    continue
                if ev.state == "open" and not ev.health_posted and now >= ev.start - timedelta(hours=rc.team_setting(team, "cutoff_soft_hours")):
                    await self.post_health(reg, rs, ev, ch, nudge=True)
                    await self.ops.emit(cfg, "info", f"{ev.key}: health check posted, nudged {len(ev.nudged)}")
                if ev.state == "open" and now >= ev.start - timedelta(hours=rc.team_setting(team, "cutoff_hard_hours")):
                    await self.lock_and_propose(reg, rs, ev, ch)
                    await self.ops.emit(cfg, "info", f"{ev.key}: locked and proposed")
                if ev.state in ("locked", "proposed", "accepted") and now >= ev.start + timedelta(hours=6):
                    ev.state = "done"
                    rs.save(ev, "done")
                    await self.refresh_sheet(reg, ev)
                    await self.ops.emit(cfg, "info", f"{ev.key}: closed")


# ---------------------------------------------------------------- commands

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

    async def team_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=t, value=t) for t in (reg.config.team_keys() if reg else []) if current.lower() in t.lower()][:25]

    def current_event(reg: Registry, team: str | None):
        rs = bot.raids.store(reg)
        key = team or reg.config.team_keys()[0]
        return rs, rs.for_team(key), reg.config.team(key) or {"key": key, "size": 20}

    raid = app_commands.Group(name="raid", description="Raid sheets and rosters")

    @raid.command(name="open", description="Officer: open the next sheet for a team now (date optional, YYYY-MM-DD)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_open(interaction: discord.Interaction, team: str | None = None, date: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        key = team or reg.config.team_keys()[0]
        t = reg.config.team(key)
        if not t or not t.get("schedule"):
            await interaction.response.send_message(f"Team {key} has no schedule. `/gm config team key:{key} schedule:'Tue 19:30'` first.", ephemeral=True)
            return
        try:
            start = rc.next_raid_time(t["schedule"], reg.config.timezone)
            if date:
                wd, h, mi = rc.parse_schedule(t["schedule"])
                start = datetime.fromisoformat(date).replace(hour=h, minute=mi, tzinfo=ZoneInfo(reg.config.timezone))
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rc.open_event(reg, rs, t, start)
        channel = bot.get_channel(reg.config.signup_channel_id) if reg.config.signup_channel_id else interaction.channel
        await interaction.response.send_message(f"Opened {ev.key} in {channel.mention}", ephemeral=True)
        if not ev.message_id:
            await bot.post_sheet(reg, rs, ev, channel)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} opened {ev.key} ({len(ev.signups)} prefilled)")

    @raid.command(name="sheet", description="Re-post the current sheet")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_sheet(interaction: discord.Interaction, team: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        await interaction.response.send_message(embed=sheet_embed(reg, ev, t, bot.ico), view=sheet_view(ev.key) if ev.state == "open" else None)
        msg = await interaction.original_response()
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")

    @raid.command(name="health", description="Roster health for the current sheet")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_health(interaction: discord.Interaction, team: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        await interaction.response.send_message(embed=health_embed(reg, ev, t), ephemeral=not is_officer(interaction, reg))

    @raid.command(name="lock", description="Officer: lock signups now and propose a roster")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_lock(interaction: discord.Interaction, team: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        await interaction.response.send_message(f"Locking {ev.key} and proposing…", ephemeral=True)
        await bot.lock_and_propose(reg, rs, ev, interaction.channel)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} locked {ev.key}")

    @raid.command(name="accept", description="Officer: accept the proposed roster")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_accept(interaction: discord.Interaction, team: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        if not ev or not ev.roster:
            await interaction.response.send_message("Nothing proposed yet.", ephemeral=True)
            return
        ev.state = "accepted"
        rs.save(ev, "accepted")
        await interaction.response.send_message(f"🔒 Roster accepted for {ev.key}: {len(ev.roster.selected)} in, {len(ev.roster.benched)} bench.")
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} accepted roster {ev.key}")

    @raid.command(name="loot", description="Officer: open the loot council thread for the accepted roster")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_loot(interaction: discord.Interaction, team: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        if not ev or not ev.roster or ev.state not in ("proposed", "accepted", "locked"):
            await interaction.response.send_message("Need a proposed/accepted roster first (/raid lock, /raid accept).", ephemeral=True)
            return
        from .discord_bot import MockEvent as LootSession

        existing = next((s for s in bot.events.values() if s.id == ev.key and s.state == "raid"), None)
        if existing:
            await interaction.response.send_message(f"Loot thread already open: <#{existing.raid_thread_id}>", ephemeral=True)
            return
        instance = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
        players = rc.players_for(reg, ev)
        e = discord.Embed(title=f"⚔ {reg.profile.raids[instance]['name']} · {ev.key}", colour=TEAL, description=f"Roster ({len(ev.roster.selected)}): " + ", ".join(p.character or p.signup_name for p in ev.roster.selected)[:3800])
        e.set_footer(text="loot council: tick drops (or let the companion feed do it) → Distribute → chat to adjust → Confirm · /raid end for the summary")
        await interaction.response.send_message(embed=e)
        msg = await interaction.original_response()
        thread = await msg.create_thread(name=f"loot · {ev.key}"[:100])
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

    @raid.command(name="set", description="Officer: set someone's status on the sheet")
    @app_commands.choices(status=[app_commands.Choice(name=s, value=s) for s in rc.STATUSES])
    @app_commands.autocomplete(team=team_autocomplete, character=_rc.member_char_autocomplete)
    async def raid_set(interaction: discord.Interaction, member: discord.User, status: app_commands.Choice[str], character: str | None = None, team: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        m = reg.members.get(member.id)
        if not ev or not m:
            await interaction.response.send_message("No open sheet, or that member isn't registered.", ephemeral=True)
            return
        try:
            s = rc.set_signup(reg, rs, ev, m, character, status.value, source="officer")
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {m.display_name}: {s.character} {status.value}", ephemeral=True)
        await bot.refresh_sheet(reg, ev)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} set {m.display_name} {status.value} on {ev.key}")

    @raid.command(name="cancel", description="Officer: cancel the current raid")
    @app_commands.autocomplete(team=team_autocomplete)
    async def raid_cancel(interaction: discord.Interaction, team: str | None = None, reason: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        ev.state = "cancelled"
        ev.log.append(f"cancelled by {interaction.user.display_name}: {reason or ''}")
        rs.save(ev, "cancelled")
        await bot.refresh_sheet(reg, ev)
        await interaction.response.send_message(f"Cancelled {ev.key}" + (f": {reason}" if reason else ""))
        await ops.emit(reg.config, "warn", f"{interaction.user.display_name} cancelled {ev.key}" + (f": {reason}" if reason else ""))

    @raid.command(name="list", description="Live raids")
    async def raid_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        rs = bot.raids.store(reg)
        live = rs.live()
        await interaction.response.send_message("\n".join(f"• {e.key} · {e.state} · <t:{int(e.start.timestamp())}:F> · {len(e.by_status('in'))} in" for e in live) or "No live raids.", ephemeral=True)

    tree.add_command(raid)

    @tree.command(name="callout", description="Can't make the raid you're signed for (records the time relative to the cutoff)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def callout_cmd(interaction: discord.Interaction, note: str | None = None, team: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, team)
        m = reg.members.get(interaction.user.id)
        if not ev or not m:
            await interaction.response.send_message("No live raid, or you're not registered.", ephemeral=True)
            return
        co = rc.callout(reg, rs, ev, m, t, note)
        await interaction.response.send_message(f"Noted: out for {ev.key} ({co.hours_before:.0f}h before{', after lock' if co.late else ''}). Thanks for saying so.", ephemeral=True)
        await bot.refresh_sheet(reg, ev)
        ch = bot.get_channel(ev.channel_id) if ev.channel_id else None
        if ch:
            await ch.send(f"⚑ {m.display_name} ({co.character}) called out for {ev.key}, {co.hours_before:.0f}h before" + (" — **after lock**" if co.late else "") + (f": {note}" if note else ""))
        await ops.emit(reg.config, "warn" if co.late else "info", f"callout {m.display_name} {ev.key} {co.hours_before:.0f}h before{' LATE' if co.late else ''}" + (f" — {note}" if note else ""))
