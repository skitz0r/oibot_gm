"""Discord surface for the weekly cycle: signup sheets with persistent buttons,
/raid commands (raids belong to a roster), /raid out, and the scheduler loop."""
from __future__ import annotations

import asyncio
import os
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
    soft = start - timedelta(hours=rc.team_setting(team, "cutoff_soft_hours"))
    hard = start - timedelta(hours=rc.team_setting(team, "cutoff_hard_hours"))
    e.add_field(name="Timeline", value=f"health check + nudges <t:{int(soft.timestamp())}:R> · lock + roster <t:{int(hard.timestamp())}:R> · raid <t:{unix}:R>", inline=False)
    e.set_footer(text=f"{ev.key} · {ev.state} · ᵖ prefilled from availability · buttons use your main; pick an alt from the menu")
    return e


def health_card(reg: Registry, ev: rc.RaidEvent, team: dict, ico, rs=None) -> tuple[discord.Embed, discord.File]:
    """Image card + a one-line embed. Numbers are in the image; the embed carries the level and the next timers."""
    h = rc.health_data(reg, ev, team)
    levels = [h["headcount_level"]] + [r["level"] for r in h["roles"] if r["need"]]
    worst = "red" if "red" in levels else ("amber" if "amber" in levels else "green")
    colour = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]
    start = ev.start.astimezone(reg.tz)  # card text is guild time; the embed's <t:> stamps render per viewer
    soft = start - timedelta(hours=rc.team_setting(team, "cutoff_soft_hours"))
    hard = start - timedelta(hours=rc.team_setting(team, "cutoff_hard_hours"))
    png = render.health_png(f"Roster health · {team.get('name', ev.team)} · {ev.key}", f"{start.strftime('%a %b %d %H:%M %Z')} · locks {hard.strftime('%a %H:%M')}", h["headcount"], h["roles"], h["buffs"], h["unresponsive"], footer="tiles: have / need · amber = tentative/sub could cover · badges: party buffs from signed players")
    file = discord.File(BytesIO(png), filename="health.png")
    n, size, tent, subs = h["headcount"]
    e = discord.Embed(colour=colour, description=f"{LEVEL_DOT[worst]} **{n}/{size}** in · nudge <t:{int(soft.timestamp())}:R> · lock <t:{int(hard.timestamp())}:R>")
    e.set_image(url="attachment://health.png")
    missing = [b for b in h["buffs"] if not b["providers"]]
    if missing:
        e.add_field(name="Missing buffs", value=" ".join(f"{ico('buff', b['id'])}" for b in missing) + "\n" + ", ".join(b["name"] for b in missing)[:900], inline=False)
    if rs is not None:
        busy = rc.conflicts(rs, ev)
        double = [f"{reg.members[u].display_name} ({k})" for u, k in busy.items() if u in reg.members and str(u) in ev.signups and ev.signups[str(u)].status in ("in", "tentative")]
        if double:
            e.add_field(name="Double-booked", value=", ".join(double)[:900], inline=False)
        open_asks = [a for a in ev.fill_asks if a.open]
        answered = [a for a in ev.fill_asks if a.answer == "yes"]
        if open_asks or answered:
            e.add_field(name="Fill", value=(f"asked: {', '.join(a.display_name for a in open_asks)}" if open_asks else "") + (f"\nfilled: {', '.join(a.display_name for a in answered)}" if answered else ""), inline=False)
    return e, file


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
            await interaction.response.send_message("Signups are locked; use `/raid out` if you can't make it, or ask an officer.", ephemeral=True)
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


class FillButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fill:(?P<key>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Yes/No on a fill DM. Survives restarts; only the person asked can answer."""

    def __init__(self, key: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Yes, count me in" if answer == "yes" else "Can't this time", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"fill:{key}:{uid}:{answer}"))
        self.key, self.uid, self.answer = key, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], int(match["uid"]), match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        if interaction.user.id != self.uid:
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        reg = bot.registries.for_interaction(interaction)
        rs = bot.raids.store(reg) if reg else None
        ev = rs.events.get(self.key) if rs else None
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That raid is closed — thanks anyway.", ephemeral=True)
            return
        ask = next((a for a in ev.fill_asks if a.discord_id == self.uid and a.open), None)
        if not ask:
            await interaction.response.send_message("Already answered (or the gap was filled).", ephemeral=True)
            return
        line = rc.apply_fill_answer(reg, rs, ev, ask, self.answer == "yes")
        try:
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(line, ephemeral=True)
        await bot.after_fill_answer(reg, rs, ev, ask, line)


class PlaceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"place:(?P<roster>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Accept / decline a roster placement (DM after officers approve a build). Persistent; only the person asked can answer."""

    def __init__(self, roster: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Accept" if answer == "yes" else "Can't make it", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"place:{roster}:{uid}:{answer}"))
        self.roster, self.uid, self.answer = roster, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["roster"], int(match["uid"]), match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        if interaction.user.id != self.uid:
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        try:
            line = rc_answer = reg.answer_placement(self.uid, self.roster, self.answer == "yes", interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"{e}", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(line, ephemeral=True)
        await bot.after_placement_answer(reg, self.uid, self.roster, self.answer == "yes", line)


class ProposalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"prop:(?P<pid>[A-Za-z0-9_\-]+):(?P<answer>accept|reject)"):
    """Officer DM: accept or reject an auto-planned set of runs. Persistent; first officer to act decides."""

    def __init__(self, pid: str, answer: str):
        super().__init__(discord.ui.Button(label="Accept — open the sheets" if answer == "accept" else "Reject", style=discord.ButtonStyle.success if answer == "accept" else discord.ButtonStyle.secondary, custom_id=f"prop:{pid}:{answer}"))
        self.pid, self.answer = pid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["pid"], match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg or not await bot.is_officer_anywhere(reg, interaction.user.id):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        ps = bot.proposals(reg)
        p = ps.items.get(self.pid)
        if not p or p.state not in ("proposed", "draft"):
            await interaction.response.send_message(f"That proposal is {p.state if p else 'gone'}" + (f" (by {p.decided_by})" if p and p.decided_by else "") + ".", ephemeral=True)
            return
        await interaction.response.defer()
        if self.answer == "reject":
            p.state, p.decided_by, p.decided_at = "rejected", interaction.user.display_name, rc.now()
            ps.save(p, f"rejected by {p.decided_by}")
            line = f"proposal {p.id} rejected by {p.decided_by}"
        else:
            line = await bot.accept_proposal(reg, p, interaction.user.display_name)
        try:
            await interaction.edit_original_response(content=(interaction.message.content or "") + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            pass
        await bot.ops.emit(reg.config, "info", line)


def proposal_view(pid: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ProposalButton(pid, "accept"))
    v.add_item(ProposalButton(pid, "reject"))
    return v


def place_view(roster: str, uid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(PlaceButton(roster, uid, "yes"))
    v.add_item(PlaceButton(roster, uid, "no"))
    return v


def fill_view(key: str, uid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(FillButton(key, uid, "yes"))
    v.add_item(FillButton(key, uid, "no"))
    return v


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
        msg = f"✅ {s.character}: **{s.status}** for {ev.key}" + (f" — {s.note}" if s.note and s.status != status else "")
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
        if rc.team_setting(team, "open_dm"):
            await self.dm_on_open(reg, rs, ev, team)

    async def dm_on_open(self, reg, rs, ev, team) -> int:
        """Optional per-team: DM every registered raider when the sheet opens, with their
        prefilled status and the same buttons, so they confirm or change from the DM."""
        unix = int(ev.start.timestamp())
        sent = 0
        for m in reg.team_pool(team["key"]):
            if not m.main or m.dm_opt_out:
                continue
            s = ev.signups.get(str(m.discord_id))
            status = f"You're prefilled as **{s.status}** on {s.character}" if s else "You haven't responded yet"
            try:
                user = await self.fetch_user(m.discord_id)
                await user.send(f"**{reg.config.name} · {team.get('name', ev.team)}** raid <t:{unix}:F> (<t:{unix}:R>). {status}. Confirm or change:" + (f" (sheet: <#{ev.channel_id}>)" if ev.channel_id else ""), view=sheet_view(ev.key))
                sent += 1
            except Exception:  # noqa: BLE001
                pass
        ev.log.append(f"open DMs sent to {sent}")
        rs.save(ev, f"open DMs {sent}")
        return sent

    async def post_health(self, reg, rs, ev, channel, nudge: bool) -> None:
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        embed, file = await asyncio.to_thread(health_card, reg, ev, team, self.ico, rs)
        await channel.send(embed=embed, file=file)
        if nudge and rc.team_setting(team, "reminders") != "none":
            targets = [m for m in reg.team_pool(team["key"]) if m.main and str(m.discord_id) not in ev.signups and m.discord_id not in ev.nudged and not m.dm_opt_out]
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

    # ---- filling gaps by DM
    async def run_fill(self, reg, rs, ev, team, by: str = "scheduler") -> tuple[list, dict]:
        """Send the next batch of fill DMs. Returns (asks sent, needs)."""
        nd = rc.needs(reg, ev, team)
        if not nd["headcount"] and not nd["roles"]:
            if ev.fill_state == "asking":
                ev.fill_state = "filled"
                rs.save(ev, "fill: complete")
            return [], nd
        batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
        unix = int(ev.start.timestamp())
        name = f"{reg.config.name} · {team.get('name', ev.team)}"
        sent = []
        for ask in batch:
            what = {
                "sub": f"you signed as a sub — can you come as **{ask.character}** ({ask.spec})?",
                "pool": f"you haven't answered the sheet — can you come as **{ask.character}** ({ask.spec})?",
                "other_roster": f"could you help out on **{ask.character}** ({ask.spec})?",
                "offspec": f"would you play **{ask.spec}** on {ask.character} instead of your main spec?",
                "alt": f"could you bring your alt **{ask.character}** ({ask.spec}) instead?",
            }[ask.kind]
            text = f"**{name}** raid <t:{unix}:F> (<t:{unix}:R>) is {ask.reason.replace('short', 'short')} — {what}" + (f"\nSheet: <#{ev.channel_id}>" if ev.channel_id else "")
            try:
                user = await self.fetch_user(ask.discord_id)
                await user.send(text, view=fill_view(ev.key, ask.discord_id))
                ev.fill_asks.append(ask)
                sent.append(ask)
            except Exception:  # noqa: BLE001
                ask.answer, ask.answered_at = "expired", rc.now()
                ev.fill_asks.append(ask)
        if sent:
            ev.fill_state = "asking"
            ev.log.append(f"fill ({by}): asked {', '.join(a.display_name for a in sent)}")
            rs.save(ev, f"fill asked {len(sent)}")
        elif not [a for a in ev.fill_asks if a.open]:
            ev.fill_state = "exhausted"
            rs.save(ev, "fill: nobody left to ask")
        return sent, nd

    async def after_fill_answer(self, reg, rs, ev, ask, line: str) -> None:
        await self.refresh_sheet(reg, ev)
        cfg = reg.config
        team = cfg.team(ev.team) or {"key": ev.team, "size": 20}
        officer_ch = self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else (self.get_channel(ev.channel_id) if ev.channel_id else None)
        nd = rc.needs(reg, ev, team)
        still = (f"still short {nd['headcount']}" if nd["headcount"] else "") + ("".join(f", {n} {r}" for r, n in nd["roles"].items()))
        if officer_ch:
            await officer_ch.send(f"🧩 {ev.key}: {line}" + (f" · {still.strip(', ')}" if still else " · **gaps filled**") + (" · run `/raid lock` to re-propose" if ev.state != "open" and ask.answer == "yes" else ""))
        await self.ops.emit(cfg, "info", f"fill {ev.key}: {line}")
        if ask.answer == "no" and ev.state == "open":
            await self.run_fill(reg, rs, ev, team, by="answer")

    # ---- auto-planner: propose runs → officers accept by DM → dated rosters + sheets → members verify on the sheet
    def proposals(self, reg):
        from .roster.autoplan import ProposalStore

        cache = self.__dict__.setdefault("_proposal_stores", {})
        return cache.setdefault(reg.key, ProposalStore(self.registries.store, reg.key))

    async def is_officer_anywhere(self, reg, uid: int) -> bool:
        if reg.config.owner_discord_id == uid:
            return True
        guild = self.get_guild(reg.config.discord_guild_id)
        if not guild:
            return False
        m = guild.get_member(uid)
        if m is None:
            try:
                m = await guild.fetch_member(uid)
            except Exception:  # noqa: BLE001
                return False
        return self.officiates(m, guild)

    async def officer_ids(self, reg) -> list[int]:
        guild = self.get_guild(reg.config.discord_guild_id)
        ids = set()
        if reg.config.owner_discord_id:
            ids.add(reg.config.owner_discord_id)
        if guild:
            for m in guild.members:
                if not m.bot and self.officiates(m, guild):
                    ids.add(m.id)
        return sorted(ids)

    async def auto_propose(self, reg, instance: str, by: str = "scheduler") -> str:
        """Plan the raid's next lockout window and DM officers the proposal. Returns a status line."""
        from .roster import autoplan

        ps = self.proposals(reg)
        if ps.open_for(instance):
            return f"{instance}: a proposal is already waiting for an officer"
        rs = self.raids.store(reg)
        p = await asyncio.to_thread(autoplan.plan, reg, rs, instance)
        if p is None:
            return f"{instance}: no eligible characters at all for this window"
        for old in ps.drafts_for(instance):
            ps.drop(old, "superseded draft")
        rd = reg.raid_def(instance)
        if not p.viable:
            ps.save(p, f"best-effort draft: {len(p.runs)} run(s) ({by})")
            return f"{instance}: best effort only — " + "; ".join(p.problems[:3]) + f" — see {os.environ.get('OIBOT_WEB_URL', 'the website')}/rosters (you can open the sheets anyway)"
        lines = [f"**{rd.get('name', instance)}** — proposed runs for {p.window_start[:10]} → {p.window_end[:10]} ({rd.get('lockout_days')}-day lockout):"]
        for r in p.runs:
            unix = int(datetime.fromisoformat(r.starts_at).timestamp())
            roles = {k: sum(1 for s in r.seats if s["role"] == k) for k in ("tank", "healer", "melee", "ranged")}
            lines.append(f"• **{r.name}** <t:{unix}:F> — {len(r.seats)}/{r.size} · " + " · ".join(f"{k} {v}" for k, v in roles.items()))
            lines.append("  " + ", ".join(f"{s['character']} ({s['display_name']})" for s in r.seats)[:900])
        if p.unplaced:
            lines.append(f"Not seated ({len(p.unplaced)}): " + "; ".join(f"{n} — {w}" for n, w in p.unplaced[:6])[:600])
        lines.append("Accept opens a dated sheet per run in the roster channel, pre-filled In for everyone seated, and DMs them the buttons. Reject discards it; the planner tries again tomorrow.")
        text = "\n".join(lines)[:1900]
        officers = await self.officer_ids(reg)
        for uid in officers:
            try:
                user = await self.fetch_user(uid)
                await user.send(text, view=proposal_view(p.id))
                p.asked.append(uid)
            except Exception:  # noqa: BLE001
                pass
        ps.save(p, f"proposed {len(p.runs)} run(s), asked {len(p.asked)} officer(s) ({by})")
        return f"{instance}: proposed {len(p.runs)} run(s) to {len(p.asked)} officer(s)"

    async def accept_proposal(self, reg, p, by: str) -> str:
        """Each run → a dated roster (schedule = its weekday/time, one-off), seats placed, sheet opened in the roster
        channel pre-filled In, DMed to the seated members; the sheet is their verification."""
        from .roster.autoplan import ProposalStore  # noqa: F401

        ps = self.proposals(reg)
        p.state, p.decided_by, p.decided_at = "accepted", by, rc.now()
        rd = reg.raid_def(p.instance)
        rs = self.raids.store(reg)
        opened = []
        cfg = reg.config
        channel = (self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else None) or (self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None)
        for r in p.runs:
            start = datetime.fromisoformat(r.starts_at)
            if not cfg.roster(r.key):
                cfg.rosters.append({"key": r.key, "name": r.name, "size": r.size, "schedule": start.strftime("%a %H:%M"), "instance": p.instance,
                                    "cutoff_soft_hours": min(48, max(6, int((start - datetime.now(start.tzinfo)).total_seconds() // 3600 // 2))),
                                    "cutoff_hard_hours": 6, "open_days_before": 14, "reminders": "dm", "open_dm": True, "autofill": True, "ephemeral": True, "proposal": p.id})
                reg.save_config(f"auto roster {r.key} ({r.name}) from proposal {p.id} (by {by})", notify=False)
            for s in r.seats:
                try:
                    reg.roster_add(s["discord_id"], r.key, by, s["character"])
                except Exception:  # noqa: BLE001
                    pass
            team = cfg.roster(r.key)
            ev = rc.open_event(reg, rs, team, start)
            if channel:
                await self.post_sheet(reg, rs, ev, channel)
            opened.append(ev.key)
        ps.save(p, f"accepted by {by}: {len(opened)} sheet(s) opened")
        if channel:
            await channel.send(f"📋 **{rd.get('name', p.instance)}** — {len(opened)} run(s) accepted by {by}. Seated members: confirm on your sheet (In / Out); the bot fills gaps from the pool.")
        return f"proposal {p.id} accepted by {by}: opened {', '.join(opened)}"

    async def auto_propose_tick(self) -> None:
        """Once a day (guild-local hour `auto_propose_hour`), plan every raid with auto-propose on."""
        for reg in self.registries.by_discord.values():
            z = ZoneInfo(reg.config.timezone)
            now = datetime.now(z)
            stamp = now.strftime("%Y-%m-%d")
            done = self.__dict__.setdefault("_auto_done", {})
            if now.hour < int(getattr(reg.config, "auto_propose_hour", 12)) or done.get(reg.key) == stamp:
                continue
            done[reg.key] = stamp
            for inst in reg.profile.raids:
                if reg.raid_def(inst).get("auto"):
                    line = await self.auto_propose(reg, inst)
                    await self.ops.emit(reg.config, "info", f"auto-plan: {line}")

    async def cleanup_ephemeral(self, reg, rs) -> None:
        """Drop dated rosters whose sheet is done/cancelled, and their memberships."""
        cfg = reg.config
        gone = []
        for t in list(cfg.rosters):
            if not t.get("ephemeral"):
                continue
            evs = [e for e in rs.events.values() if e.team == t["key"]]
            if evs and all(e.state in ("done", "cancelled") for e in evs):
                for m in list(reg.members.values()):
                    if reg.on_roster(m, t["key"]):
                        reg.roster_remove(m.discord_id, t["key"], "cleanup")
                cfg.rosters = [x for x in cfg.rosters if x["key"] != t["key"]]
                gone.append(t["key"])
        if gone:
            reg.save_config(f"ephemeral rosters closed: {gone}", notify=False)

    # ---- placement confirmations after an approved build
    async def send_placement_asks(self, reg, adds: list[tuple[int, str, str]], by: str) -> int:
        """DM every newly placed member: accept keeps the seat (and defaults them In), decline gives it back."""
        from .roster.builder import shells_from_config

        shells = {sh.key: sh for sh in shells_from_config(reg)}
        sent = 0
        for uid, character, key in adds:
            m = reg.members.get(uid)
            t = reg.config.roster(key) or {"key": key}
            if not m:
                continue
            c = next((c for c in m.active() if c.label == character), None)
            if m.dm_opt_out:
                continue  # placed without asking; they opted out of DMs
            reg.add_placement_ask(uid, key, character, by)
            sh = shells.get(key)
            when = f"<t:{int(sh.start.timestamp())}:F> (<t:{int(sh.start.timestamp())}:R>)" if sh else (t.get("schedule") or "time TBD")
            what = f"**{character}**" + (f" ({c.cls} {c.spec}, {reg.profile.spec(c.cls, c.spec).role})" if c else "")
            text = (f"**{reg.config.name}**: you're placed on **{t.get('name', key)}** for the coming raid — {when} — as {what}.\n"
                    f"Accept to keep the seat (you'll be pre-filled In on its sheets). Can't make it and the seat goes back to the pool." + (f"\nManage it on {os.environ.get('OIBOT_WEB_URL', 'the website')}" if os.environ.get("OIBOT_WEB_URL") else ""))
            try:
                user = await self.fetch_user(uid)
                await user.send(text, view=place_view(key, uid))
                sent += 1
            except Exception:  # noqa: BLE001
                pass
        if sent:
            await self.ops.emit(reg.config, "info", f"placement confirmations sent to {sent} (build approved by {by})")
        return sent

    async def after_placement_answer(self, reg, uid: int, roster: str, yes: bool, line: str) -> None:
        cfg = reg.config
        ch = self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else None
        if ch:
            await ch.send(f"{'✅' if yes else '↩️'} {line}" + ("" if yes else f" — rebuild at {os.environ.get('OIBOT_WEB_URL', 'the website')}/admin/build or `/roster build`"))
        await self.ops.emit(cfg, "info" if yes else "warn", line)

    async def lock_and_propose(self, reg, rs, ev, channel) -> None:
        ev.state = "locked"
        rs.save(ev, "locked")
        await self.refresh_sheet(reg, ev)
        players, result = await asyncio.to_thread(rc.propose, reg, rs, ev)
        team = reg.config.team(ev.team) or {"key": ev.team, "size": 20}
        png = await asyncio.to_thread(render.roster_png, reg.profile, players, result, f"{team.get('name', ev.team)} · {ev.key}", f"{len(result.selected)} in · synergy {result.synergy_value}")
        e = discord.Embed(title=f"Proposed roster · {ev.key}", colour=TEAL, description=f"{len(result.selected)} in · " + " · ".join(f"{self.ico('role', k)} {v}" for k, v in result.role_counts.items()) + f" · synergy **{result.synergy_value}**")
        e.set_image(url="attachment://roster.png")
        e.add_field(name="Bench", value=", ".join(f"{self.ico('class', p.cls)} {p.character or p.signup_name}" for p in result.benched) or "nobody", inline=False)
        if result.advisories:
            e.add_field(name="Advisories", value="\n".join(a[:120] for a in result.advisories[:6])[:1000], inline=False)
        e.set_footer(text="/raid accept to lock the roster · /raid lock again after changes")
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
        try:
            await self.auto_propose_tick()
        except Exception as e:  # noqa: BLE001
            print(f"auto-plan error: {e}")
        for reg in self.registries.by_discord.values():
            cfg = reg.config
            rs = self.raids.store(reg)
            channel = self.get_channel(cfg.signup_channel_id) if cfg.signup_channel_id else None
            await self.cleanup_ephemeral(reg, rs)
            for team in cfg.raid_teams:
                if not team.get("schedule") or team.get("ephemeral"):
                    continue  # ephemeral (auto-planned) rosters are opened once by accept_proposal, never on a weekly cadence
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
                officer_ch = (self.get_channel(cfg.roster_channel_id) if cfg.roster_channel_id else None) or ch
                if ev.state == "open" and not ev.health_posted and now >= ev.start - timedelta(hours=rc.team_setting(team, "cutoff_soft_hours")):
                    await self.post_health(reg, rs, ev, officer_ch, nudge=True)
                    await self.ops.emit(cfg, "info", f"{ev.key}: health check posted, nudged {len(ev.nudged)}")
                if ev.state == "open" and ev.health_posted and rc.team_setting(team, "autofill") and ev.fill_state in ("idle", "asking"):
                    # between the soft and hard cutoffs: keep asking the next candidates until the gaps close
                    sent, nd = await self.run_fill(reg, rs, ev, team)
                    if sent:
                        await officer_ch.send(f"🧩 {ev.key}: short {nd['headcount']}" + "".join(f", {n} {r}" for r, n in nd["roles"].items()) + " — asked " + ", ".join(f"{a.display_name} ({a.kind})" for a in sent))
                    elif ev.fill_state == "exhausted" and "fill exhausted" not in ev.log:
                        ev.log.append("fill exhausted")
                        rs.save(ev, "fill exhausted")
                        await officer_ch.send(f"🧩 {ev.key}: nobody left to ask — short {nd['headcount']}" + "".join(f", {n} {r}" for r, n in nd["roles"].items()))
                if ev.state == "open" and now >= ev.start - timedelta(hours=rc.team_setting(team, "cutoff_hard_hours")):
                    await self.lock_and_propose(reg, rs, ev, officer_ch)
                    if officer_ch is not ch:
                        await ch.send(f"🔒 {ev.key}: signups locked; officers are reviewing the roster.")
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

    async def roster_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=t, value=t) for t in (reg.config.roster_keys() if reg else []) if current.lower() in t.lower()][:25]

    def current_event(reg: Registry, roster: str | None):
        rs = bot.raids.store(reg)
        key = roster or reg.config.roster_keys()[0]
        return rs, rs.for_team(key), reg.config.roster(key) or {"key": key, "size": 20}

    raid = app_commands.Group(name="raid", description="Raid sheets and rosters")

    @raid.command(name="open", description="Officer: open the next sheet for a team now (date optional, YYYY-MM-DD)")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_open(interaction: discord.Interaction, roster: str | None = None, date: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        key = roster or reg.config.roster_keys()[0]
        t = reg.config.roster(key)
        if not t or not t.get("schedule"):
            await interaction.response.send_message(f"Roster {key} has no schedule. `/gm config roster key:{key} schedule:'Tue 19:30'` first.", ephemeral=True)
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
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_sheet(interaction: discord.Interaction, roster: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        await interaction.response.send_message(embed=sheet_embed(reg, ev, t, bot.ico), view=sheet_view(ev.key) if ev.state == "open" else None)
        msg = await interaction.original_response()
        ev.channel_id, ev.message_id = msg.channel.id, msg.id
        rs.save(ev, "sheet re-posted")

    @raid.command(name="health", description="Roster health for the current sheet")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_health(interaction: discord.Interaction, roster: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        embed, file = await asyncio.to_thread(health_card, reg, ev, t, bot.ico, rs)
        await interaction.response.send_message(embed=embed, file=file, ephemeral=not is_officer(interaction, reg))

    @raid.command(name="lock", description="Officer: lock signups now and propose a roster")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_lock(interaction: discord.Interaction, roster: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
        if not ev:
            await interaction.response.send_message("No open sheet.", ephemeral=True)
            return
        await interaction.response.send_message(f"Locking {ev.key} and proposing…", ephemeral=True)
        target = (bot.get_channel(reg.config.roster_channel_id) if reg.config.roster_channel_id else None) or interaction.channel
        await bot.lock_and_propose(reg, rs, ev, target)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} locked {ev.key}")

    @raid.command(name="accept", description="Officer: accept the proposed roster")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_accept(interaction: discord.Interaction, roster: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
        if not ev or not ev.roster:
            await interaction.response.send_message("Nothing proposed yet.", ephemeral=True)
            return
        ev.state = "accepted"
        rs.save(ev, "accepted")
        await interaction.response.send_message(f"🔒 Roster accepted for {ev.key}: {len(ev.roster.selected)} in, {len(ev.roster.benched)} bench.")
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} accepted roster {ev.key}")

    @raid.command(name="loot", description="Officer: open the loot council thread for the accepted roster")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_loot(interaction: discord.Interaction, roster: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
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
    @app_commands.autocomplete(roster=roster_autocomplete, character=_rc.member_char_autocomplete)
    async def raid_set(interaction: discord.Interaction, member: discord.User, status: app_commands.Choice[str], character: str | None = None, roster: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
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
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def raid_cancel(interaction: discord.Interaction, roster: str | None = None, reason: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
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

    @raid.command(name="out", description="Can't make the raid you're signed for (records the time relative to the cutoff)")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def callout_cmd(interaction: discord.Interaction, note: str | None = None, roster: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
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
        if rc.team_setting(t, "autofill") and ev.health_posted:
            sent, nd = await bot.run_fill(reg, rs, ev, t, by="callout")
            officer_ch = bot.get_channel(reg.config.roster_channel_id) if reg.config.roster_channel_id else ch
            if sent and officer_ch:
                await officer_ch.send(f"🧩 {ev.key}: replacement for {m.display_name} — asked " + ", ".join(f"{a.display_name} ({a.kind})" for a in sent))

    async def raid_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=r, value=r) for r in (reg.profile.raids if reg else []) if current.lower() in r.lower()][:25]

    @raid.command(name="plan", description="Officer: auto-plan a raid's next lockout window now and DM officers the proposal")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_plan(interaction: discord.Interaction, raid: str):
        reg = await officer(interaction)
        if not reg:
            return
        if raid not in reg.profile.raids:
            await interaction.response.send_message(f"Unknown raid. Options: {', '.join(reg.profile.raids)}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        line = await bot.auto_propose(reg, raid, by=interaction.user.display_name)
        await interaction.followup.send(line, ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} ran the planner: {line}")

    @raid.command(name="fill", description="Officer: ask the next best people to cover the sheet's gaps (subs, pool, other rosters, offspec/alt)")
    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.describe(preview="only show who would be asked")
    async def raid_fill(interaction: discord.Interaction, roster: str | None = None, preview: bool = False):
        reg = await officer(interaction)
        if not reg:
            return
        rs, ev, t = current_event(reg, roster)
        if not ev:
            await interaction.response.send_message("No live raid for that roster.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        nd = rc.needs(reg, ev, t)
        gaps = (f"short {nd['headcount']}" if nd["headcount"] else "headcount ok") + "".join(f" · {n} {r} short" for r, n in nd["roles"].items())
        busy = rc.conflicts(rs, ev)
        if preview:
            cands = await asyncio.to_thread(rc.fill_candidates, reg, rs, ev, t)
            lines = [f"{i + 1}. {a.display_name} — {a.kind}: {a.character} ({a.spec}, {a.role}) for {a.reason}" for i, a in enumerate(cands[:15])]
            open_asks = [a for a in ev.fill_asks if a.open]
            await interaction.followup.send(f"**{ev.key}** · {gaps}" + (f" · double-booked: {', '.join(reg.members[u].display_name for u in busy if u in reg.members)}" if busy else "") + f"\nOutstanding asks: {', '.join(a.display_name for a in open_asks) or 'none'}\nWould ask next:\n" + ("\n".join(lines) or "nobody left"), ephemeral=True)
            return
        sent, nd = await bot.run_fill(reg, rs, ev, t, by=interaction.user.display_name)
        await interaction.followup.send(f"**{ev.key}** · {gaps}\n" + ("Asked: " + ", ".join(f"{a.display_name} ({a.kind}: {a.character})" for a in sent) if sent else ("Nothing to fill." if not (nd["headcount"] or nd["roles"]) else "Nobody left to ask (or the asks outstanding already cover it).")), ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} ran fill for {ev.key}: asked {len(sent)}")

    tree.add_command(raid)
