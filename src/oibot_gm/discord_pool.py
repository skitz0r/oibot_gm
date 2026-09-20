"""Dedicated channels the bot keeps current on its own:

- registration channel (public, read-only): one pinned registration card; members only press buttons.
- analytics channel (officers): one pinned pool-readiness card per roster, edited in place whenever the
  registry changes, plus a change-log line under it for every diff (who changed what, from → to).

Registry commits drive both through Registry.listeners; refreshes are debounced per guild."""
from __future__ import annotations

import asyncio
import os
import re

import discord

from . import comp as comp_mod
from .discord_registry import registration_card, registration_view
from .registry import Registry, pool_health_data

from .constants import LEVEL_COLOUR, ROLES, TEAL  # noqa: E402
DEBOUNCE_S = 5.0  # a burst of registrations re-posts the cards once
CARD_PREFIX = "pool2:"  # analytics_message_ids key of a raid's native card (anything else is a legacy image card, removed on refresh)


def _comp_icon(ico, key: str) -> str:
    """The icon for a desired-comp line: a role, a class or a spec (icons stand alone: no name beside them)."""
    if key in ROLES:
        return ico("role", key)
    return ico("spec", key) if ":" in key else ico("class", key)


def _site(path: str) -> str | None:
    web = os.environ.get("OIBOT_WEB_URL", "")
    return f"{web}{path}" if web.startswith("https") else None


def pool_layout(reg: Registry, roster: dict, ico) -> tuple[discord.ui.LayoutView, str]:
    """One native card per raid: who the pool could field at this size (roles have/need, buffs nobody brings) and
    where the desired comp is short or over. Icons + numbers only; the Members and Raids pages hold the detail.
    Returns (view, level)."""
    ui = discord.ui
    key, name = roster.get("key", "main"), roster.get("name") or roster.get("key", "main")
    h = pool_health_data(reg, roster)
    n, size, alts, _on = h["headcount"]
    players = comp_mod.pool_players(reg)
    ic = comp_mod.ideal_comp(reg.profile, size, players, roster.get("comp_targets") or {}, roster.get("instance"), reg)
    short = [ln for ln in ic.lines if ln.level == "red" and ln.key not in ROLES]
    over = [ln for ln in ic.lines if ln.max is not None and ln.have > ln.max]
    levels = [h["headcount_level"]] + [r["level"] for r in h["roles"] if r["need"]] + (["red"] if short else [])
    worst = "red" if "red" in levels else ("amber" if "amber" in levels or over else "green")
    head = f"## {name}\n**{n}** / {size} mains" + (f" · {alts} alts" if alts else "") + (f" · {h['unnamed']} unnamed" if h.get("unnamed") else "")
    web = os.environ.get("OIBOT_WEB_URL", "")
    top = ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(media=f"{web}/img/raid/{roster.get('instance') or key}.png")) if web.startswith("https") and (roster.get("instance") or key) in reg.profile.raids else ui.TextDisplay(head)
    lines = ["   ".join(f"{ico('role', r['role'])} {r['have']}/{r['need']}" + (" ⚠" if r["level"] == "red" else "") for r in h["roles"] if r["need"])]
    missing = [b for b in h["buffs"] if not b["providers"] and ico("buff", b["id"])]
    if missing:
        lines.append("⛔ " + " ".join(ico("buff", b["id"]) for b in missing[:12]))
    if short:
        lines.append("**Short** " + "   ".join(f"{_comp_icon(ico, ln.key)} {ln.have}/{ln.want}" for ln in short[:8]))
    if over:
        lines.append("**Over cap** " + "   ".join(f"{_comp_icon(ico, ln.key)} {ln.have}/{ln.max}" for ln in over[:8]))
    parts = [top, ui.Separator(), ui.TextDisplay("\n".join(x for x in lines if x.strip()) or "every slot filled")]
    links = [ui.Button(label=label, style=discord.ButtonStyle.link, url=url) for label, url in (("Members", _site("/app/members")), ("Raid rules", _site("/app/raids"))) if url]
    parts += [ui.Separator(), ui.TextDisplay(f"-# Change the ideals in plain text here: @mention me, e.g. “{roster.get('instance') or key}: we want 3 tanks”, “cap hunters at 3”.")]
    if links:
        parts.append(ui.ActionRow(*links))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=LEVEL_COLOUR[worst]))
    return view, worst


class PoolMixin:
    """Needs self.registries, self.ops, self.ico, self.loop (discord.Client)."""

    def attach_pool_listeners(self) -> None:
        self._pool_timers: dict[str, asyncio.TimerHandle] = {}
        for reg in self.registries.by_discord.values():
            reg.listeners.append(self.on_registry_change)

    # ---- listener (sync; may be called from a worker thread)
    def on_registry_change(self, reg: Registry, kind: str, lines: list[str]) -> None:
        self.loop.call_soon_threadsafe(self._pool_changed, reg, kind, lines)

    def _pool_changed(self, reg: Registry, kind: str, lines: list[str]) -> None:
        if not reg.config.analytics_channel_id:
            return
        if reg.test_members() and not any("test bench" in l for l in lines):
            return  # the test bench is running: keep the analytics cards honest until /gm test clear
        if kind == "member":
            self.loop.create_task(self.post_pool_log(reg, lines))
        elif not any(l.startswith(("roster", "rosters", "team", "raid", "analytics", "aura")) for l in lines):
            return  # config commits that can't move the numbers (channels, timezone, owner)
        t = self._pool_timers.pop(reg.key, None)
        if t:
            t.cancel()
        self._pool_timers[reg.key] = self.loop.call_later(DEBOUNCE_S, lambda: self.loop.create_task(self.refresh_pool(reg)))

    # ---- analytics channel
    async def post_pool_log(self, reg: Registry, lines: list[str]) -> None:
        ch = self.get_channel(reg.config.analytics_channel_id)
        if not ch:
            return
        stamp = f"<t:{int(reg.now_local().timestamp())}:t>"
        try:
            await ch.send("\n".join(f"{stamp} {l}" for l in lines)[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:  # noqa: BLE001
            await self.ops.emit(reg.config, "warn", f"analytics log post failed: {e}")

    async def refresh_pool(self, reg: Registry, announce: bool = False) -> list[discord.Message]:
        """Keep one native card per raid current in the analytics channel: edited in place (no delete/re-post noise);
        a card is only posted when it doesn't exist yet. Cards from the old image era are removed once."""
        ch = self.get_channel(reg.config.analytics_channel_id) if reg.config.analytics_channel_id else None
        if not ch:
            return []
        locks = self.__dict__.setdefault("_pool_locks", {})
        lock = locks.setdefault(reg.key, asyncio.Lock())
        async with lock:
            out, ids = [], dict(reg.config.analytics_message_ids)
            wanted = {f"{CARD_PREFIX}{rid}": reg.raid_shell(rid) for rid in reg.profile.raids}
            for key in [k for k in ids if k not in wanted]:  # the bank / pool / comp / groups PNG cards, or a raid that left the profile
                try:
                    await (await ch.fetch_message(ids[key])).delete()
                except Exception:  # noqa: BLE001 — already gone
                    pass
                ids.pop(key)
            for key, roster in wanted.items():
                try:
                    view, _level = await asyncio.to_thread(pool_layout, reg, roster, self.ico)
                except Exception as e:  # noqa: BLE001
                    await self.ops.emit(reg.config, "error", f"analytics card for {key} failed", e)
                    continue
                msg = None
                if key in ids:
                    try:
                        msg = await (await ch.fetch_message(ids[key])).edit(view=view)
                    except Exception:  # noqa: BLE001 — deleted by hand: post it again
                        ids.pop(key)
                if msg is None:
                    try:
                        msg = await ch.send(view=view)
                    except Exception as e:  # noqa: BLE001
                        await self.ops.emit(reg.config, "warn", f"analytics card post for {key} failed: {e}")
                        continue
                ids[key] = msg.id
                out.append(msg)
            if ids != reg.config.analytics_message_ids:
                reg.config.analytics_message_ids = ids
                reg.save_config("analytics card messages", notify=False)
            if announce:
                await self.ops.emit(reg.config, "info", f"analytics cards refreshed in #{ch.name}")
            return out

    async def post_registration_card(self, reg: Registry, ch: discord.TextChannel, by: str) -> str:
        """Post (or move) the persistent card and try to make the channel read-only for members. Returns a status note."""
        notes = []
        if reg.config.registration_channel_id and reg.config.registration_message_id:
            old = self.get_channel(reg.config.registration_channel_id)
            if old:
                try:
                    await (await old.fetch_message(reg.config.registration_message_id)).delete()
                except Exception:  # noqa: BLE001
                    pass
        msg = await ch.send(embed=registration_card(reg), view=registration_view())
        try:
            await msg.pin()
        except Exception:  # noqa: BLE001
            notes.append("couldn't pin (needs Manage Messages)")
        try:
            ow = ch.overwrites_for(ch.guild.default_role)
            if ow.send_messages is not False:
                ow.update(send_messages=False, create_public_threads=False, create_private_threads=False, add_reactions=False)
                await ch.set_permissions(ch.guild.default_role, overwrite=ow, reason=f"oibot_GM: registration channel is read-only (by {by})")
                notes.append("@everyone can no longer post here (buttons still work)")
        except discord.Forbidden:
            notes.append("couldn't set read-only (give the bot Manage Channels, or deny Send Messages for @everyone yourself)")
        except Exception as e:  # noqa: BLE001
            notes.append(f"read-only not set: {e}")
        reg.config.registration_channel_id, reg.config.registration_message_id = ch.id, msg.id
        reg.save_config(f"registration channel → #{ch.name}", notify=False)
        return " · ".join(notes)


# ---------------------------------------------------------------- absences channel

class AbsenceModal(discord.ui.Modal, title="I'll be away"):
    start = discord.ui.TextInput(label="From (YYYY-MM-DD)", placeholder="2026-12-24", min_length=10, max_length=10)
    end = discord.ui.TextInput(label="To (YYYY-MM-DD, blank = one day)", required=False, max_length=10)
    reason = discord.ui.TextInput(label="Reason (officers only, optional)", required=False, max_length=120)

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        from .registry import RegistryError

        try:
            m, a = reg.add_absence(interaction.user.id, str(self.start.value).strip(), str(self.end.value).strip() or None, str(self.reason.value).strip() or None, interaction.user.display_name, display_name=interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        span = a.start + (f" → {a.end}" if a.end != a.start else "")
        await interaction.response.send_message(f"✅ Away {span}. Sheets on those days will have you as No thanks; if you're already rostered, the seat is handed back.", ephemeral=True)
        await bot.announce_absence(reg, m, a, interaction.user.display_name)


class AbsenceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"abs:(?P<action>new|mine)"):
    def __init__(self, action: str):
        label, style = ("I'll be away", discord.ButtonStyle.primary) if action == "new" else ("My absences", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"abs:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if self.action == "new":
            await interaction.response.send_modal(AbsenceModal())
            return
        m = reg.members.get(interaction.user.id)
        today = reg.now_local().date().isoformat()
        ups = m.upcoming_absences(today) if m else []
        if not ups:
            await interaction.response.send_message("No upcoming absences.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(f"• {a.start}" + (f" → {a.end}" if a.end != a.start else "") + (f" — {a.reason}" if a.reason else "") for a in ups), view=clear_absences_view(reg, m, ups[:5]), ephemeral=True)


def clear_absences_view(reg: Registry, m, absences: list) -> discord.ui.View:
    """One Clear button per listed absence on the ephemeral 'My absences' reply: clears it and ripples the span into the sheets."""
    view = discord.ui.View(timeout=180)
    for a in absences:
        btn = discord.ui.Button(label=f"Clear {a.start}" + (f" → {a.end}" if a.end != a.start else ""), style=discord.ButtonStyle.secondary)

        async def clear(i: discord.Interaction, start=a.start):
            bot = i.client
            from .registry import RegistryError

            await i.response.edit_message(content="Clearing…", view=None)
            try:
                gone = reg.clear_absence(m.discord_id, start, i.user.display_name)
            except RegistryError as e:
                await i.edit_original_response(content=f"❌ {e}")
                return
            lines = await bot.absence_cleared(reg, m, gone, i.user.display_name)
            await i.edit_original_response(content=f"✅ Cleared absence {start}" + (f" → {gone.end}" if gone.end != gone.start else "") + ("\n" + "\n".join("• " + l for l in lines) if lines else ""))

        btn.callback = clear
        view.add_item(btn)
    return view


def absences_card(reg: Registry) -> discord.Embed:
    today = reg.now_local().date().isoformat()
    rows = sorted(((m, a) for m in reg.members.values() for a in m.absences if a.end >= today), key=lambda x: x[1].start)
    e = discord.Embed(title=f"{reg.config.name} · away", colour=TEAL,
                      description="Going to miss some days? Press **I'll be away**. Sheets on those days have you as *No thanks* automatically, and if you were already rostered the seat is handed back. Reasons stay with the officers.")
    if rows:
        e.add_field(name="Upcoming", value="\n".join(f"**{m.display_name}** · {a.start}" + (f" → {a.end}" if a.end != a.start else "") for m, a in rows[:25])[:1000], inline=False)
    e.set_footer(text="Also: /me absent add · the Me page on the website")
    return e


def absences_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(AbsenceButton("new"))
    v.add_item(AbsenceButton("mine"))
    return v


class AbsencesMixin:
    async def post_absences_card(self, reg: Registry, ch: discord.TextChannel, by: str) -> None:
        if reg.config.absences_channel_id and reg.config.absences_message_id:
            old = self.get_channel(reg.config.absences_channel_id)
            if old:
                try:
                    await (await old.fetch_message(reg.config.absences_message_id)).delete()
                except Exception:  # noqa: BLE001
                    pass
        msg = await ch.send(embed=absences_card(reg), view=absences_view())
        try:
            await msg.pin()
        except Exception:  # noqa: BLE001
            pass
        reg.config.absences_channel_id, reg.config.absences_message_id = ch.id, msg.id
        reg.save_config(f"absences channel → #{ch.name} (by {by})", notify=False)

    async def refresh_absences_card(self, reg: Registry) -> None:
        if not (reg.config.absences_channel_id and reg.config.absences_message_id):
            return
        ch = self.get_channel(reg.config.absences_channel_id)
        if not ch:
            return
        try:
            await (await ch.fetch_message(reg.config.absences_message_id)).edit(embed=absences_card(reg), view=absences_view())
        except Exception:  # noqa: BLE001
            pass

    async def announce_absence(self, reg: Registry, m, a, by: str) -> None:
        """Public line (no reason), sheets updated, card refreshed, ops line with the reason."""
        span = a.start + (f" → {a.end}" if a.end != a.start else "")
        touched = await self.after_absence(reg, m, a.start, a.end, by)
        ch = self.get_channel(reg.config.absences_channel_id) if reg.config.absences_channel_id else None
        if ch:
            try:
                await ch.send(f"🛫 **{m.display_name}** is away {span}" + (f" · off {len(touched)} sheet(s)" if touched else ""), allowed_mentions=discord.AllowedMentions.none())
            except Exception:  # noqa: BLE001
                pass
        await self.refresh_absences_card(reg)
        await self.ops.emit(reg.config, "info", f"{m.display_name} absent {span}" + (f" — {a.reason}" if a.reason else "") + (f" (by {by})" if by != m.display_name else "") + (f" · sheets: {', '.join(touched)}" if touched else ""))

    async def absence_cleared(self, reg: Registry, m, a, by: str) -> list[str]:
        """After Registry.clear_absence: the cleared span ripples into the live sheets (after_absence_cleared), the
        card is refreshed, one ops line. Returns the member-facing lines (which sheets they can answer again)."""
        span = a.start + (f" → {a.end}" if a.end != a.start else "")
        lines = await self.after_absence_cleared(reg, m, a)
        await self.refresh_absences_card(reg)
        await self.ops.emit(reg.config, "info", f"{m.display_name} cleared absence {span}" + (f" (by {by})" if by != m.display_name else "") + (f" · {len(lines)} sheet(s) touched" if lines else ""))
        return lines


# ---------------------------------------------------------------- setup from the web + test bench

CHANNEL_KINDS = {"ops": "ops_channel_id", "applications": "applications_channel_id", "signup": "signup_channel_id", "roster": "roster_channel_id",
                 "registration": "registration_channel_id", "analytics": "analytics_channel_id", "absences": "absences_channel_id"}


class SetupMixin:
    async def set_channel(self, reg: Registry, kind: str, channel_id: int | None, by: str) -> str:
        """The web's version of /gm config <kind>-channel: sets the id and does what the command does (post the
        registration or absences card, refresh the analytics cards)."""
        attr = CHANNEL_KINDS.get(kind)
        if not attr:
            raise ValueError(f"unknown channel kind {kind}")
        ch = self.get_channel(channel_id) if channel_id else None
        if channel_id and (ch is None or not isinstance(ch, discord.TextChannel)):
            raise ValueError("that channel isn't a text channel I can see")
        if not ch:
            setattr(reg.config, attr, None)
            if kind == "registration":
                reg.config.registration_message_id = None
            if kind == "absences":
                reg.config.absences_message_id = None
            if kind == "analytics":
                reg.config.analytics_message_ids = {}
            reg.save_config(f"{kind} channel cleared (by {by})")
            return f"{kind} channel cleared"
        if kind == "registration":
            note = await self.post_registration_card(reg, ch, by)
            return f"registration card posted in #{ch.name}" + (f" · {note}" if note else "")
        if kind == "absences":
            await self.post_absences_card(reg, ch, by)
            return f"absence card posted in #{ch.name}"
        if kind == "analytics":
            if reg.config.analytics_channel_id != ch.id:
                reg.config.analytics_channel_id, reg.config.analytics_message_ids = ch.id, {}
                reg.save_config(f"analytics channel → #{ch.name} (by {by})", notify=False)
            msgs = await self.refresh_pool(reg)
            return f"{len(msgs)} analytics card(s) posted in #{ch.name}"
        setattr(reg.config, attr, ch.id)
        reg.save_config(f"{kind} channel → #{ch.name} (by {by})")
        return f"{kind} channel → #{ch.name}"

    def guild_channels(self, reg: Registry) -> list[dict]:
        g = self.get_guild(reg.config.discord_guild_id)
        if not g:
            return []
        return [{"id": str(c.id), "name": c.name, "category": c.category.name if c.category else None} for c in sorted(g.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position))]

    def guild_roles(self, reg: Registry) -> list[str]:
        g = self.get_guild(reg.config.discord_guild_id)
        return [r.name for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed] if g else []

    async def test_bench_clear(self, reg: Registry, by: str) -> str:
        """Cancel test runs, remove test members and their run rosters."""
        from . import raidcycle as rc  # noqa: F401

        rs = self.raids.store(reg)
        cancelled = []
        for ev in rs.live():
            t = reg.config.roster(ev.team) or {}
            if t.get("test"):
                await self.cancel_run(reg, rs, ev, by=by, reason="test bench cleared")
                cancelled.append(ev.key)
        gone = await asyncio.to_thread(reg.clear_test_members, by)
        await self.cleanup_ephemeral(reg, rs)
        return f"{gone} test member(s) removed, {len(cancelled)} test run(s) cancelled" + (f" ({', '.join(cancelled)})" if cancelled else "")
