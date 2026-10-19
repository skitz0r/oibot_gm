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
from . import ops as ops_mod
from . import raid_views as views
from .discord_registry import registration_card, registration_view
from .registry import Registry, RegistryError

from .constants import LEVEL_COLOUR, ROLES, TEAL  # noqa: E402
DEBOUNCE_S = 5.0  # a burst of registrations re-posts the cards once
CARD_PREFIX = "pool2:"  # analytics_message_ids key of a raid's native card (anything else is a legacy image card, removed on refresh)


def _comp_icon(ico, key: str) -> str:
    """The icon for a desired-comp line: a role, a class or a spec (icons stand alone: no name beside them)."""
    if key in ROLES:
        return ico("role", key)
    if key == "dps":  # comp bounds speak of dps; the icons are per damage role
        return ico("role", "melee") + ico("role", "ranged")
    return ico("spec", key) if ":" in key else ico("class", key)


def _site(reg: Registry, path: str) -> str | None:
    """A site page for this guild (?g= selects it in the server selector), or None off https."""
    web = os.environ.get("OIBOT_WEB_URL", "")
    return f"{web}{path}?g={reg.key}" if web.startswith("https") else None


def pool_layout(reg: Registry, roster: dict, ico, rs=None) -> tuple[discord.ui.LayoutView, str]:
    """One native card per raid: who the pool could field at this size (roles have/need, buffs nobody brings) and
    where the desired comp is short or over. Icons + numbers only; the Members and Raids pages hold the detail.
    Returns (view, level). The numbers are comp.readiness (one computation for the card, the API, MCP and /ask);
    `rs` is the bot's RaidStore, so "on roster" counts the seats in the run files exactly as GET /api/readiness does."""
    ui = discord.ui
    key = roster.get("key", "main")
    d = comp_mod.readiness(reg, roster.get("instance") or key, rs)  # the same numbers GET /api/readiness, MCP and /ask read
    name, size, worst, hc = d["name"], d["size"], d["level"], d["headcount"]
    short = [c for c in d["comp"] if c["level"] == "red" and c["key"] not in ROLES]
    over = d["over"]
    head = f"## {name}\n**{hc['mains']}** / {size} mains" + (f" · {hc['alts']} alts" if hc["alts"] else "") + (f" · {hc['unnamed']} unnamed" if hc["unnamed"] else "")
    rid = roster.get("instance") or key
    thumb = views.raid_thumb_url(reg, rid) if rid in reg.profile.raids else None
    top = ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(media=thumb)) if thumb else ui.TextDisplay(head)
    lines = ["   ".join(f"{ico('role', r['role'])} {r['have']}/{r['need']}" + (" ⚠" if r["level"] == "red" else "") for r in d["roles"] if r["need"])]
    missing = [b for b in d["missing_buffs"] if ico("buff", b["id"])]
    if missing:
        lines.append("⛔ " + " ".join(ico("buff", b["id"]) for b in missing[:12]))
    if short:
        lines.append("**Short** " + "   ".join(f"{_comp_icon(ico, c['key'])} {c['have']}/{c['want']}" for c in short[:8]))
    for cv in d.get("series") or []:  # regulars coverage per recurring series (replaces the old "on roster" count)
        lines.append(f"**{cv['name']}**{' (paused)' if not cv['active'] else ''} {views.coverage_line(ico, cv)}")
        lines += views.forecast_lines(ico, cv)
    if over:
        lines.append("**Over cap** " + "   ".join(f"{_comp_icon(ico, c['key'])} {c['have']}/{c['max']}" for c in over[:8]))
    parts = [top, ui.Separator(), ui.TextDisplay("\n".join(x for x in lines if x.strip()) or "every slot filled")]
    links = [ui.Button(label=label, style=discord.ButtonStyle.link, url=url) for label, url in (("Members", _site(reg, "/app/members")), ("Raid rules", _site(reg, "/app/raids"))) if url]
    parts += [ui.Separator(), ui.TextDisplay(f"-# Change the ideals in plain text here: @mention me, e.g. “{roster.get('instance') or key}: we want 3 tanks”, “cap hunters at 3”.")]
    if links:
        parts.append(ui.ActionRow(*links))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*parts, accent_colour=LEVEL_COLOUR[worst]))
    return view, worst


class PoolMixin:
    """Needs self.registries, self.ops, self.ico, self.loop (discord.Client)."""

    def attach_pool_listeners(self) -> None:
        """Boot: every configured guild's change listener (a guild claimed later gets its own via attach_pool_listener)."""
        for reg in self.registries.ordered():
            self.attach_pool_listener(reg)

    def attach_pool_listener(self, reg: Registry) -> None:
        """One guild's analytics listener; idempotent (boot and a claim may both call it)."""
        self.__dict__.setdefault("_pool_timers", {})
        if self.on_registry_change not in reg.listeners:
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
                    raids = getattr(self, "raids", None)
                    view, _level = await asyncio.to_thread(pool_layout, reg, roster, self.ico, raids.store(reg) if raids is not None else None)
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
        msg = await ch.send(embed=registration_card(reg), view=registration_view(reg))
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

class AbsenceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"abs(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<action>new|mine)"):
    """The absences card's buttons; the id carries the guild (`abs.<gk>:new`), and the card works only in its server."""

    def __init__(self, action: str, gk: str | None):
        label, style = ("I'll be away", discord.ButtonStyle.primary) if action == "new" else ("My absences", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"abs{'.' + gk if gk else ''}:{action}"))
        self.action, self.gk = action, gk

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], match["g"])

    async def callback(self, interaction: discord.Interaction):
        from .discord_registry import channel_card_guild

        reg = channel_card_guild(interaction, self.gk)  # a channel-only card: strict, never a DM fallback
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if self.action == "new":  # the same wizard as /me absent add: pick the first day and how long, nothing typed
            from .wizard_flows import FLOWS

            await FLOWS["absence"](interaction, reg)
            return
        m = reg.members.get(interaction.user.id)
        today = reg.now_local().date().isoformat()
        ups = m.upcoming_absences(today) if m else []
        if not ups:
            await interaction.response.send_message("No upcoming absences.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(f"• {reg.span_label(a.start, a.end)}" + (f" — {a.reason}" if a.reason else "") for a in ups), view=clear_absences_view(reg, m, ups[:5]), ephemeral=True)  # days in words (the clock rule)


def clear_absences_view(reg: Registry, m, absences: list) -> discord.ui.View:
    """One Clear button per listed absence on the ephemeral 'My absences' reply: clears it and ripples the span into the sheets."""
    view = discord.ui.View(timeout=180)
    for a in absences:
        btn = discord.ui.Button(label=f"Clear {reg.span_label(a.start, a.end)}"[:80], style=discord.ButtonStyle.secondary)

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
            await i.edit_original_response(content=f"✅ Cleared your absence {reg.span_label(gone.start, gone.end)}" + ("\n" + "\n".join("• " + l for l in lines) if lines else ""))

        btn.callback = clear
        view.add_item(btn)
    return view


def absences_card(reg: Registry) -> discord.Embed:
    today = reg.now_local().date().isoformat()
    rows = sorted(((m, a) for m in reg.members.values() for a in m.absences if a.end >= today), key=lambda x: x[1].start)
    e = discord.Embed(title=f"{reg.config.name} · away", colour=TEAL,
                      description="Going to miss some days? Press **I'll be away**. Sheets on those days have you as *No thanks* automatically, and if you were already rostered the seat is handed back. Reasons stay with the officers.")
    if rows:
        e.add_field(name="Upcoming", value="\n".join(f"**{m.display_name}** · {reg.span_label(a.start, a.end)}" for m, a in rows[:25])[:1000], inline=False)  # D28: names + days in words
    e.set_footer(text="Also: /me absent add · the Me page on the website")
    return e


def absences_view(reg: Registry) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(AbsenceButton("new", reg.key))
    v.add_item(AbsenceButton("mine", reg.key))
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
        msg = await ch.send(embed=absences_card(reg), view=absences_view(reg))
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
            await (await ch.fetch_message(reg.config.absences_message_id)).edit(embed=absences_card(reg), view=absences_view(reg))
        except Exception:  # noqa: BLE001
            pass

    async def announce_absence(self, reg: Registry, m, a, by: str) -> list[str]:
        """Public line (no reason), sheets updated, card refreshed, ops line with the reason. Returns the keys of the
        live sheets it touched (answered No thanks, or a rostered seat freed) so every caller can say which."""
        span = reg.span_label(a.start, a.end)  # people read it: "Sat 1 Nov → Mon 3 Nov", never ISO
        touched = await self.after_absence(reg, m, a.start, a.end, by)
        ch = self.get_channel(reg.config.absences_channel_id) if reg.config.absences_channel_id else None
        if ch:
            try:
                await ch.send(f"🛫 **{m.display_name}** is away {span}" + (f" · off {len(touched)} sheet(s)" if touched else ""), allowed_mentions=discord.AllowedMentions.none())
            except Exception:  # noqa: BLE001
                pass
        await self.refresh_absences_card(reg)
        await self.ops.emit(reg.config, "info", f"{m.display_name} absent {span}" + (f" — {a.reason}" if a.reason else "") + (f" (by {by})" if by != m.display_name else "") + (f" · sheets: {', '.join(touched)}" if touched else ""))
        return touched

    async def absence_cleared(self, reg: Registry, m, a, by: str) -> list[str]:
        """After Registry.clear_absence: the cleared span ripples into the live sheets (after_absence_cleared), the
        card is refreshed, one ops line. Returns the member-facing lines (which sheets they can answer again)."""
        span = reg.span_label(a.start, a.end)  # people read it: "Sat 1 Nov → Mon 3 Nov", never ISO
        lines = await self.after_absence_cleared(reg, m, a)
        await self.refresh_absences_card(reg)
        await self.ops.emit(reg.config, "info", f"{m.display_name} cleared absence {span}" + (f" (by {by})" if by != m.display_name else "") + (f" · {len(lines)} sheet(s) touched" if lines else ""))
        return lines


# ---------------------------------------------------------------- setup from the web + test bench

CHANNEL_KINDS = {"ops": "ops_channel_id", "applications": "applications_channel_id", "signup": "signup_channel_id", "roster": "roster_channel_id",
                 "registration": "registration_channel_id", "analytics": "analytics_channel_id", "absences": "absences_channel_id", "news": "news_channel_id",
                 "loot": "loot_channel_id"}  # loot: the public award log (one line per confirmed award; LootLogMixin)


class SetupMixin:
    async def set_channel(self, reg: Registry, kind: str, channel_id: int | None, by: str, *, via: str = "discord", by_id: int | None = None) -> str:
        """THE channel-setting verb (/gm config <kind>-channel, the Config page, plain text, MCP): sets the id and does what
        the command does (post the registration or absences card, refresh the analytics cards), then writes the one
        audit line (ops.audited: `by` is the person, `via` the surface, `by_id` marks a host superuser)."""
        line = await self._set_channel(reg, kind, channel_id, by)
        ops = getattr(self, "ops", None)
        if ops is not None:
            await ops_mod.audited(ops, reg, by, line, via=via, by_id=by_id)
        return line

    async def _set_channel(self, reg: Registry, kind: str, channel_id: int | None, by: str) -> str:
        attr = CHANNEL_KINDS.get(kind)
        if not attr:
            raise ValueError(f"unknown channel kind {kind}")
        ch = self.get_channel(channel_id) if channel_id else None
        if channel_id and (ch is None or not isinstance(ch, discord.TextChannel)):
            raise ValueError("that channel isn't a text channel I can see")
        if ch is not None and getattr(getattr(ch, "guild", None), "id", None) != reg.config.discord_guild_id:
            raise ValueError("That channel is in another server.")  # the site, MCP and plain text all come through here
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

    async def create_invite(self, reg: Registry, channel_id: int) -> str:
        """A permanent, unlimited-use invite to one of the guild's text channels (the homepage's Join button). Needs
        the Create Instant Invite permission there; a missing permission is a ValueError that says so. Nothing is
        saved: the caller puts the link in the recruitment settings."""
        ch = self.get_channel(int(channel_id)) if channel_id else None
        if ch is None or not isinstance(ch, discord.TextChannel) or ch.guild.id != reg.config.discord_guild_id:
            raise ValueError("that channel isn't a text channel of this server I can see")
        if not ch.permissions_for(ch.guild.me).create_instant_invite:
            raise ValueError(f"I can't create invites in #{ch.name}: give the bot the Create Invite permission there (or server-wide) and try again")
        try:
            inv = await ch.create_invite(max_age=0, max_uses=0, unique=False, reason=f"oibot_GM homepage invite for {reg.config.name}")
        except discord.Forbidden:
            raise ValueError(f"Discord refused: I don't have the Create Invite permission in #{ch.name}") from None
        except discord.HTTPException as e:
            raise ValueError(f"Discord refused the invite: {e.text or e}") from None
        return inv.url

    def guild_channels(self, reg: Registry) -> list[dict]:
        g = self.get_guild(reg.config.discord_guild_id)
        if not g:
            return []
        return [{"id": str(c.id), "name": c.name, "category": c.category.name if c.category else None} for c in sorted(g.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position))]

    def guild_roles(self, reg: Registry) -> list[str]:
        g = self.get_guild(reg.config.discord_guild_id)
        return [r.name for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed] if g else []

    # ---- the test bench (dual interfaces phase 2, B9): the verbs every surface calls — /gm test seed|run|answer|compose|
    # comp|clear (flows_misc, flows_test), plain text's `test …` (configops.apply_async), the site's Test bench
    # (/api/test/*), MCP test_bench through those routes. Each writes its own audit line (ops.audited: `by` the person,
    # `via` the surface → the web / mcp / chat prefix, `by_id` marks a host superuser), so no caller composes one.
    # Puppets only ever touch TEST runs (rc.is_test_run). Sheet writes (forget_test_members, open_comp_sandbox,
    # test_answer_mix) run ON THE LOOP, never in a worker thread: they change the live events the loop's handlers
    # iterate (L3), like every on-demand open (open_test_run, open_run_and_post).

    async def _test_ops(self, reg: Registry, level: str, line: str, *, by: str, via: str = "discord", by_id: int | None = None) -> None:
        ops = getattr(self, "ops", None)
        if ops is not None:
            await ops_mod.audited(ops, reg, by, line, via=via, level=level, by_id=by_id)

    async def test_seed(self, reg: Registry, count: int, by: str, *, via: str = "discord", by_id: int | None = None) -> str:
        """Seed `count` puppets from the bench's roster (0 = all of it; existing ones are kept; capped to the roster)."""
        n = len(reg.test_roster())
        made = await asyncio.to_thread(reg.seed_test_members, count if count > 0 else n, by)  # member files only: no sheet
        total = len(reg.test_members())
        await self._test_ops(reg, "warn", f"test bench: {len(made)} test members seeded ({total} total)", by=by, via=via, by_id=by_id)
        return f"{len(made)} test member(s) created, {total} in total. Analytics cards pause until the bench is cleared."

    async def test_compose(self, reg: Registry, rs, add: list[dict], remove: list[int], by: str, *, via: str = "discord", by_id: int | None = None) -> str:
        """The bench by composition: remove the chosen puppets (taken off every open sheet; refused while one is rostered
        on a locked run), then add rows of {cls, spec, offspec?, count}. The site's spec grid, /gm test compose and plain
        text's `test add …` / `test remove …`."""
        from . import raidcycle as rc

        remove = [int(u) for u in remove or []]
        changed = rc.forget_test_members(reg, rs, remove, by) if remove else []  # on the loop: it writes open sheets (L3)
        made = await asyncio.to_thread(reg.add_test_members, list(add or []), by) if add else []
        for ev in changed:
            await self.refresh_sheet(reg, ev)
        line = f"test bench: {len(made)} added, {len(remove)} removed · {len(reg.test_members())} test members"
        await self._test_ops(reg, "warn", line, by=by, via=via, by_id=by_id)
        return line

    async def test_comp(self, reg: Registry, rs, raid: str, by: str, by_id: int | None, *, via: str = "discord"):
        """The comp sandbox: a test run two weeks out with every puppet joined, never posted (rc.open_comp_sandbox);
        the board's Propose roster then builds the best comp. Returns the run. rc.open_comp_sandbox opens a run and
        joins every puppet: on the loop, never a worker thread (L3)."""
        from . import raidcycle as rc

        ev = rc.open_comp_sandbox(reg, rs, raid, by, by_id)
        await self._test_ops(reg, "warn", f"test bench: opened a comp sandbox {ev.key} with {len(reg.test_members())} test members joined (not posted)",
                             by=by, via=via, by_id=by_id)
        return ev

    @staticmethod
    def puppet_asks(reg: Registry, ev) -> list[tuple]:
        """(puppet, 'confirm' | 'fill') for every open ask a puppet holds on this run: a Confirm / Can't make it after
        lock, or a fill / pre-lock ask — what Answer as puppets… answers."""
        from . import raidcycle as rc

        out = [(m, "confirm") for m, _a in rc.open_placement_rows(reg, ev) if m.test]
        seen = {m.discord_id for m, _ in out}
        for a in ev.fill_asks:
            m = reg.members.get(a.discord_id)
            if a.open and m is not None and m.test and m.discord_id not in seen:
                out.append((m, "fill"))
                seen.add(m.discord_id)
        return sorted(out, key=lambda mk: mk[0].display_name.lower())

    async def puppet_answer_ask(self, reg: Registry, rs, ev, m, yes: bool, by: str, by_id: int | None = None) -> str:
        """A puppet's Confirm / Can't make it (or a fill ask's yes / no), through the verbs a member's own answer uses
        (answer_placement_for / answer_fill_for). RegistryError when the puppet has no open ask on this run."""
        from . import raidcycle as rc

        if any(mm.discord_id == m.discord_id for mm, _a in rc.open_placement_rows(reg, ev)):
            return await self.answer_placement_for(reg, m.discord_id, ev.team, yes, by, by_id=by_id)
        if any(a.discord_id == m.discord_id and a.open for a in ev.fill_asks):
            return await self.answer_fill_for(reg, rs, ev, m, yes, by, by_id=by_id)
        raise RegistryError(f"{m.display_name} has no open ask on {views.run_title(reg, ev)}")

    async def test_answer(self, reg: Registry, rs, ev, *, member=None, status: str | None = None, join: int = 0, bench: int = 0, out: int = 0,
                          yes: int = 0, no: int = 0, by: str, by_id: int | None = None, via: str = "discord", rng=None) -> list[str]:
        """Puppets answer a TEST run: one `member` with `status` (in | sub | out on the sheet — out on a locked run where
        they hold a seat is a callout —; yes | no answers their open ask), or a random mix — `join`/`bench`/`out` of
        the puppets who haven't answered the sheet, `yes`/`no` of the puppets holding an open ask. Returns one line per
        answer; the sheet is refreshed and one ops line written. A real run is refused (puppets never answer one)."""
        import random

        from . import raidcycle as rc

        if ev is None or ev.state in ("done", "cancelled"):
            raise RegistryError("That run is no longer live.")
        if not rc.is_test_run(reg, ev):
            raise RegistryError(f"{views.run_title(reg, ev)} is a real run: test members answer test runs only.")
        done: list[str] = []
        if member is not None:
            m = member
            if not getattr(m, "test", False):
                raise RegistryError("pick a test member")
            st = str(status or "in")
            if st in ("yes", "no"):
                done.append(await self.puppet_answer_ask(reg, rs, ev, m, st == "yes", by, by_id))
            elif st not in rc.STATUSES:
                raise RegistryError(f"status must be one of {', '.join((*rc.STATUSES, 'yes', 'no'))}")
            elif ev.state != "open" and st == "out" and ev.seat_of(m):
                await self.drop_seated(reg, rs, ev, rc.run_team(reg, ev), m, "callout (test)", None)
                done.append(f"{m.display_name} called out")
            else:
                sg = rc.set_signup(reg, rs, ev, m, None, st, source="test")
                done.append(f"{m.display_name} {rc.LABELS[sg.status]}")
        else:
            if any(int(n or 0) < 0 for n in (join, bench, out, yes, no)):
                raise RegistryError("answer counts can't be negative")
            if join or bench or out:
                done += rc.test_answer_mix(reg, rs, ev, join, bench, out, rng)  # on the loop, as every sheet write (L3)
            if yes or no:
                asks = self.puppet_asks(reg, ev)
                (rng or random).shuffle(asks)
                for want, n in ((True, int(yes or 0)), (False, int(no or 0))):
                    for _ in range(n):
                        if not asks:
                            break
                        m, _kind = asks.pop()
                        try:
                            done.append(await self.puppet_answer_ask(reg, rs, ev, m, want, by, by_id))
                        except RegistryError as e:  # answered meanwhile (a press, the scheduler): say so, go on
                            done.append(str(e))
        if done:
            await self.refresh_sheet(reg, ev)
            await self._test_ops(reg, "info", f"test bench: {len(done)} answer(s) on {ev.key}", by=by, via=via, by_id=by_id)
        return done

    async def test_bench_clear(self, reg: Registry, by: str, *, via: str = "discord", by_id: int | None = None) -> str:
        """Cancel test runs, remove test members and their run rosters; writes its own audit line (every surface: the
        /gm test clear decide screen, the site's Delete all, plain text's `test clear`, MCP)."""
        rs = self.raids.store(reg)
        cancelled = []
        for ev in rs.live():
            t = reg.config.roster(ev.team) or {}
            if t.get("test"):
                await self.cancel_run(reg, rs, ev, by=by, reason="test bench cleared", notify=False, by_id=by_id, via=via)  # never a line where real members read
                cancelled.append(ev.key)
        gone = await asyncio.to_thread(reg.clear_test_members, by)
        await self.cleanup_ephemeral(reg, rs)
        line = f"{gone} test member(s) removed, {len(cancelled)} test run(s) cancelled" + (f" ({', '.join(cancelled)})" if cancelled else "")
        await self._test_ops(reg, "warn", f"test bench: {line}", by=by, via=via, by_id=by_id)
        return line


# ---------------------------------------------------------------- the public loot log (design §3.1 "Members", D2)

def loot_log_line(reg: Registry, award, was: str | None, ico=None) -> str:
    """One public line per award, as members read it: '🎁 **Robe of Tests** → <spec icon> **Kestrel** · Gatekeeper',
    a correction '✎ **Robe of Tests**: was Kestrel, now **Mira**', a disenchant '🎁 **Robe of Tests** → disenchanted'.
    Never a reason, a score or who recorded it (officer data)."""
    from .loot.session import DISENCHANT

    item = award.item_name or (reg.item(award.item_id).name if reg.item(award.item_id) is not None else f"item {award.item_id}")
    if award.raider == DISENCHANT:
        return f"✎ **{item}**: was {was}, now disenchanted" if was else f"🎁 **{item}** → disenchanted"
    icon = ""
    if ico is not None:
        hit = None
        for m in reg.members.values():
            if award.discord_id is not None and m.discord_id != award.discord_id:
                continue
            hit = next((c for c in m.characters if c.label.lower() == award.raider.lower()), None)
            if hit is not None:
                break
        if hit is not None:
            icon = (ico("spec", f"{hit.cls}:{hit.spec}") or "") + " "
    who = f"{icon}**{award.raider}**"
    if was:
        return f"✎ **{item}**: was {was}, now {who}"
    return f"🎁 **{item}** → {who}" + (" (offspec)" if award.offspec else "") + (f" · {award.boss}" if award.boss and award.boss != "?" else "")


class LootWishButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lootwish(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<item>[a-z0-9-]{1,64})"):
    """*Add to my wishlist* under a public loot-log line (where members actually see items): opens the /me wishlist
    wizard on that item for the presser. The id carries the guild and the item key (`lootwish.<gk>:<item key>`)."""

    def __init__(self, gk: str, item: str):
        super().__init__(discord.ui.Button(label="Add to my wishlist", style=discord.ButtonStyle.secondary, emoji="⭐",
                                           custom_id=f"lootwish.{gk}:{item}"[:100]))
        self.gk, self.wish_item = gk, item  # never `self.item`: that is the DynamicItem's button

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["item"])

    async def callback(self, interaction: discord.Interaction):
        from .discord_registry import channel_card_guild

        reg = channel_card_guild(interaction, self.gk)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        from .wizard_flows import FLOWS

        await FLOWS["me_wishlist"](interaction, reg, item=self.wish_item)


class LootLogMixin:
    """The public loot log: one line per award recorded in a guild's ledger (lootctx.LEDGER_LISTENERS — Confirm, a
    companion award, a correction, the crash repair alike; a test council writes no ledger rows, so nothing), in the
    loot channel, else (log on, no channel) the run's updates thread — never the signup channel, where 30 lines would
    bury the sheet. Log off: nothing is posted (the site's Loot log still shows every award). The first line that has
    to fall back says so once per guild: an ops warn and a line in the council's thread."""

    def attach_loot_log(self) -> None:
        from .lootctx import LEDGER_LISTENERS

        def loot_log_award(guild_key: str, award, row: dict, was: str | None) -> None:
            coro = self.loot_log_post(guild_key, award, row.get("event"), was)
            try:
                asyncio.get_running_loop().create_task(coro)
            except RuntimeError:  # a writer off the loop (a web route's thread): hand it to the bot's loop
                loop = getattr(self, "loop", None)
                if loop is None or loop.is_closed():
                    coro.close()
                    return
                asyncio.run_coroutine_threadsafe(coro, loop)

        LEDGER_LISTENERS[:] = [f for f in LEDGER_LISTENERS if getattr(f, "__name__", "") != loot_log_award.__name__] + [loot_log_award]

    def loot_log_target(self, reg: Registry, run_key: str | None):
        """(channel, fell_back) for an award line, or (None, False): log off, or nowhere to post."""
        cfg = reg.config
        if not cfg.loot_log:
            return None, False
        get = getattr(self, "get_channel", None)
        if cfg.loot_channel_id:
            ch = get(cfg.loot_channel_id) if get else None
            return ch, False
        ev = self.raids.store(reg).events.get(run_key) if run_key and hasattr(self, "raids") else None
        tid = getattr(ev, "updates_thread_id", None) if ev is not None else None
        return (get(tid) if get and tid else None), True

    async def loot_log_post(self, guild_key: str, award, run_key: str | None, was: str | None) -> bool:
        reg = (getattr(self.registries, "by_key", {}) or {}).get(guild_key)
        if reg is None:
            return False
        ch, fell_back = self.loot_log_target(reg, run_key)
        if fell_back:
            warned = self.__dict__.setdefault("_loot_log_warned", set())
            if guild_key not in warned:
                warned.add(guild_key)
                note = ("The public loot log is on but no loot channel is set — award lines go to each run's updates thread. "
                        "Pick one with /gm config loot-channel or on the Config page.")
                ops = getattr(self, "ops", None)
                if ops is not None:
                    await ops.emit(reg.config, "warn", note)
                for s in [s for s in getattr(self, "events", {}).values() if s.guild == guild_key and s.run_key == run_key][:1]:
                    await self.loot_post(s, "ℹ️ " + note)
        if ch is None:
            return False
        from .registry import item_key

        text = loot_log_line(reg, award, was, getattr(self, "ico", None))
        view = discord.ui.View(timeout=None)
        if award.raider:  # members see items here: one press puts it on their wishlist
            view.add_item(LootWishButton(reg.key, item_key(award.item_id, award.item_name or "")))
        try:
            await ch.send(text[:1900], view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:  # noqa: BLE001 — the award is recorded; the public line is a courtesy
            ops = getattr(self, "ops", None)
            if ops is not None:
                await ops.emit(reg.config, "warn", f"loot log: couldn't post in #{getattr(ch, 'name', ch)} ({type(e).__name__}) — check the bot's Send Messages there")
            return False
        return True
