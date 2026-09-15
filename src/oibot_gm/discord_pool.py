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

from . import comp as comp_mod, render
from .discord_registry import registration_card, registration_view
from .registry import Registry, bank_rows, pool_health_data

LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}
DEBOUNCE_S = 5.0  # a burst of registrations re-posts the cards once
BANK_KEY = "_bank"  # analytics_message_ids slot for the character bank card


def _stamp() -> str:
    return datetime.now().strftime("%a %b %d %H:%M")


def groups_card(reg: Registry, roster: dict, ico) -> tuple[discord.Embed, discord.File]:
    """Optimised groups for the pool at this roster's size, with per-group aura coverage and raid buffs."""
    key = roster.get("key", "main")
    players, result, cov, labels = comp_mod.optimize(reg, roster)
    rb = comp_mod.raid_buff_status(reg.profile, players)
    fname = f"groups-{key}.png"
    if result is None or cov is None:
        e = discord.Embed(colour=0x98A3B5, description=f"**Groups · {key}** — no mains in the pool yet" if not players else f"**Groups · {key}** — the solver couldn't build groups from {len(players)} main(s) yet")
        png = render.health_png(f"Optimised groups · {key}", "waiting for registrations", (len(players), int(roster.get("size") or 20), 0, 0), [], [], [], headcount_text=f"{len(players)} mains")
        return e, discord.File(BytesIO(png), filename=fname)
    open_slots = int(roster.get("size") or 20) - len(result.selected)
    png = render.groups_png(reg.profile, players, result, cov, rb,
                            f"Optimised groups · {roster.get('name', key)} ({roster.get('size', 20)}-man)",
                            f"{len(result.selected)} of {len(players)} mains placed · {open_slots} open slot{'s' if open_slots != 1 else ''} · groups seeded by archetype, synergy {result.synergy_value} · updated {_stamp()}",
                            reg.profile.buff_assumptions(), labels)
    file = discord.File(BytesIO(png), filename=fname)
    missing_raid = [r for r in rb if not r["providers"]]
    worst = "red" if missing_raid or any(g.missing_summary and "nobody on roster" in " ".join(g.missing_summary) for g in cov.groups) else ("amber" if any(g.missing_summary for g in cov.groups) else "green")
    colour = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]
    e = discord.Embed(colour=colour, description=f"{LEVEL_DOT[worst]} **{len(result.groups)}** groups from **{len(result.selected)}** mains · " + " · ".join(f"{ico('role', r)} {n}" for r, n in result.role_counts.items()))
    e.set_image(url=f"attachment://{fname}")
    if cov.unmet_raidwide:
        e.add_field(name="No provider in the pool", value=", ".join(cov.unmet_raidwide)[:900], inline=False)
    if missing_raid:
        e.add_field(name="Raid buffs nobody brings", value=" ".join(f"{ico('buff', r['id'])}" for r in missing_raid) + "\n" + ", ".join(r["name"] for r in missing_raid)[:800], inline=False)
    adv = [a for a in result.advisories if a.startswith(("🔴", "🟡"))][:3]
    if adv:
        e.add_field(name="Advisories", value="\n".join(adv)[:900], inline=False)
    e.set_footer(text="Solver output on the whole pool; the real sheet decides who plays. Badges: coloured = aura present in group, red outline = wanted but missing")
    return e, file


def comp_card(reg: Registry, roster: dict) -> tuple[discord.Embed, discord.File]:
    """Desired comp for this roster's size vs the pool, with justifications; officer targets from roster config."""
    key = roster.get("key", "main")
    players = comp_mod.pool_players(reg)
    size = int(roster.get("size") or 20)
    ic = comp_mod.ideal_comp(reg.profile, size, players, roster.get("comp_targets") or {}, roster.get("instance"))
    n_off = sum(1 for l in ic.lines if l.source == "officer")
    png = render.comp_png(ic.lines, f"Desired comp · {roster.get('name', key)} ({size}-man, {ic.groups} groups)",
                          f"derived from the buff matrix and comp rules · {n_off} officer target(s) · updated {_stamp()}", ic.notes)
    fname = f"comp-{key}.png"
    file = discord.File(BytesIO(png), filename=fname)
    short = [l for l in ic.lines if l.level == "red"]
    over = [l for l in ic.lines if l.max is not None and l.have > l.max]
    worst = "red" if short else ("amber" if over or any(l.level == "amber" for l in ic.lines) else "green")
    colour = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}[worst]
    e = discord.Embed(colour=colour, description=f"{LEVEL_DOT[worst]} **Desired comp · {key}** — " + (("short: " + ", ".join(f"{l.key} {l.have}/{l.want}" for l in short[:6])) if short else "every slot filled"))
    e.set_image(url=f"attachment://{fname}")
    if over:
        e.add_field(name="Over cap", value=", ".join(f"{l.key} {l.have}/{l.max}" for l in over)[:900], inline=False)
    e.set_footer(text=f"Change the ideals in plain text: @mention me here, e.g. “{key}: we want 3 tanks”, “cap hunters at 3 because of Trueshot”, “clear the paladin target”")
    return e, file


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
    e.set_footer(text="Re-posted by the bot after every registry change; the log above says what moved")
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
        elif not any(l.startswith(("roster", "rosters", "team", "analytics")) for l in lines):
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
        """Re-post every card at the bottom of the channel (old copies deleted) so the cards are always the
        newest messages, under the change log. One refresh at a time per guild."""
        ch = self.get_channel(reg.config.analytics_channel_id) if reg.config.analytics_channel_id else None
        if not ch:
            return []
        locks = self.__dict__.setdefault("_pool_locks", {})
        lock = locks.setdefault(reg.key, asyncio.Lock())
        async with lock:
            out = []
            rosters = reg.config.rosters or [{"key": "main", "name": "main", "size": 20}]
            keys = [BANK_KEY]
            for r in rosters:
                keys += [r["key"], f"comp:{r['key']}", f"groups:{r['key']}"]
            cards = []
            for key in keys:
                try:
                    if key == BANK_KEY:
                        cards.append((key, await asyncio.to_thread(bank_card, reg)))
                    elif key.startswith("comp:"):
                        cards.append((key, await asyncio.to_thread(comp_card, reg, next(r for r in rosters if r["key"] == key[5:]))))
                    elif key.startswith("groups:"):
                        cards.append((key, await asyncio.to_thread(groups_card, reg, next(r for r in rosters if r["key"] == key[7:]), self.ico)))
                    else:
                        cards.append((key, await asyncio.to_thread(pool_card, reg, next(r for r in rosters if r["key"] == key), self.ico)))
                except Exception as e:  # noqa: BLE001
                    await self.ops.emit(reg.config, "error", f"analytics card for {key} failed", e)
            # delete the previous copies, then post the new set in order
            for key, mid in list(reg.config.analytics_message_ids.items()):
                try:
                    await (await ch.fetch_message(mid)).delete()
                except Exception:  # noqa: BLE001
                    pass
            reg.config.analytics_message_ids = {}
            for key, (embed, file) in cards:
                try:
                    msg = await ch.send(embed=embed, file=file)
                except Exception as e:  # noqa: BLE001
                    await self.ops.emit(reg.config, "warn", f"analytics card post for {key} failed: {e}")
                    continue
                reg.config.analytics_message_ids[key] = msg.id
                out.append(msg)
            reg.save_config("analytics card messages", notify=False)
            if announce:
                await self.ops.emit(reg.config, "info", f"analytics cards refreshed in #{ch.name}")
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
