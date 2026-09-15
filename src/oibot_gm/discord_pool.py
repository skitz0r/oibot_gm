"""Dedicated channels the bot keeps current on its own:

- registration channel (public, read-only): one pinned registration card; members only press buttons.
- analytics channel (officers): one pinned pool-readiness card per roster, edited in place whenever the
  registry changes, plus a change-log line under it for every diff (who changed what, from → to).

Registry commits drive both through Registry.listeners; refreshes are debounced per guild."""
from __future__ import annotations

import asyncio
from datetime import datetime
from io import BytesIO

import discord

from . import render
from .discord_registry import registration_card, registration_view
from .registry import Registry, bank_rows, pool_health_data

LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}
DEBOUNCE_S = 3.0
BANK_KEY = "_bank"  # analytics_message_ids slot for the character bank card


def bank_card(reg: Registry) -> tuple[discord.Embed, discord.File]:
    rows = bank_rows(reg)
    mains = sum(1 for r in rows if r["main"])
    alts = sum(len(r["alts"]) for r in rows)
    unnamed = sum(1 for r in rows if r["main"] and not r["main"]["name"])
    png = render.bank_png(f"Character bank · {reg.config.name}", f"{len(rows)} members · {mains} mains · {alts} alts" + (f" · {unnamed} mains unnamed" if unnamed else "") + f" · updated {datetime.now().strftime('%a %b %d %H:%M')}",
                          rows, footer="sorted by role then class · grey name = planned, not yet created · rank/rosters are officer-set")
    file = discord.File(BytesIO(png), filename="bank.png")
    e = discord.Embed(colour=0x2B7A78, description=f"**{len(rows)}** members · **{mains}** mains · **{alts}** alts")
    e.set_image(url="attachment://bank.png")
    e.set_footer(text="Kept current by the bot")
    return e, file


def pool_card(reg: Registry, roster: dict, ico) -> tuple[discord.Embed, discord.File]:
    h = pool_health_data(reg, roster)
    levels = [h["headcount_level"]] + [r["level"] for r in h["roles"] if r["need"]]
    worst = "red" if "red" in levels else ("amber" if "amber" in levels else "green")
    colour = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]
    n, size, alts, on_roster = h["headcount"]
    png = render.health_png(
        f"Pool readiness · {roster.get('name', roster.get('key', 'main'))} ({size}-man)",
        f"every planned or active main counts · updated {datetime.now().strftime('%a %b %d %H:%M')}",
        h["headcount"], h["roles"], h["buffs"], h["unresponsive"],
        footer="tiles: mains by primary role / needed at this size · amber = offspec/flex/alt could cover · red = recruit",
        headcount_text=f"{n}/{size} mains · {alts} alts · {on_roster} on roster" + (f" · {h['unnamed']} unnamed" if h["unnamed"] else ""),
        unresponsive_label="Not on this roster", buff_hint="badge = buff · name = provider · red outline = nobody in the pool brings it")
    file = discord.File(BytesIO(png), filename=f"pool-{roster.get('key', 'main')}.png")
    e = discord.Embed(colour=colour, description=f"{LEVEL_DOT[worst]} **{n}/{size}** mains registered · {on_roster} placed on **{roster.get('key', 'main')}** · {alts} alts")
    e.set_image(url=f"attachment://pool-{roster.get('key', 'main')}.png")
    missing = [b for b in h["buffs"] if not b["providers"]]
    if missing:
        e.add_field(name="Nobody brings", value=" ".join(f"{ico('buff', b['id'])}" for b in missing) + "\n" + ", ".join(b["name"] for b in missing)[:900], inline=False)
    asks = [f"{r['need'] - r['have']} {r['role']}" for r in h["roles"] if r["need"] and r["have"] < r["need"]]
    if asks:
        e.add_field(name="Recruiting ask", value=", ".join(asks), inline=False)
    e.set_footer(text="Kept current by the bot · changes are logged below this card")
    return e, file


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
        if kind == "member":
            self.loop.create_task(self.post_pool_log(reg, lines))
        elif not any(l.startswith(("roster", "rosters", "analytics")) for l in lines):
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
        stamp = datetime.now().strftime("%H:%M")
        try:
            await ch.send("\n".join(f"`{stamp}` {l}" for l in lines)[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:  # noqa: BLE001
            await self.ops.emit(reg.config, "warn", f"analytics log post failed: {e}")

    async def refresh_pool(self, reg: Registry, announce: bool = False) -> list[discord.Message]:
        """Edit every roster's card in place; (re)post and pin any that are missing."""
        ch = self.get_channel(reg.config.analytics_channel_id) if reg.config.analytics_channel_id else None
        if not ch:
            return []
        out = []
        changed = False
        rosters = reg.config.rosters or [{"key": "main", "name": "main", "size": 20}]
        for key in [BANK_KEY] + [r["key"] for r in rosters]:
            try:
                if key == BANK_KEY:
                    embed, file = await asyncio.to_thread(bank_card, reg)
                else:
                    embed, file = await asyncio.to_thread(pool_card, reg, next(r for r in rosters if r["key"] == key), self.ico)
            except Exception as e:  # noqa: BLE001
                await self.ops.emit(reg.config, "error", f"analytics card for {key} failed", e)
                continue
            msg = None
            mid = reg.config.analytics_message_ids.get(key)
            if mid:
                try:
                    msg = await ch.fetch_message(mid)
                    await msg.edit(embed=embed, attachments=[file])
                except discord.NotFound:
                    msg = None
                except Exception as e:  # noqa: BLE001
                    await self.ops.emit(reg.config, "warn", f"pool card edit for {key} failed: {e}")
                    continue
            if msg is None:
                msg = await ch.send(embed=embed, file=file)
                try:
                    await msg.pin()
                except Exception:  # noqa: BLE001
                    pass
                reg.config.analytics_message_ids[key] = msg.id
                changed = True
            out.append(msg)
        # drop cards for rosters that no longer exist
        for key in [k for k in reg.config.analytics_message_ids if k != BANK_KEY and k not in {r["key"] for r in rosters}]:
            try:
                await (await ch.fetch_message(reg.config.analytics_message_ids[key])).delete()
            except Exception:  # noqa: BLE001
                pass
            del reg.config.analytics_message_ids[key]
            changed = True
        if changed:
            reg.save_config("analytics card messages", notify=False)
        if announce:
            await self.ops.emit(reg.config, "info", f"pool readiness cards refreshed in #{ch.name}")
        return out

    # ---- registration channel
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
