"""Dedicated channels the bot keeps current on its own:

- registration channel (public, read-only): one pinned registration card; members only press buttons.
- analytics channel (officers): one pinned pool-readiness card per roster, edited in place whenever the
  registry changes, plus a change-log line under it for every diff (who changed what, from → to).

Registry commits drive both through Registry.listeners; refreshes are debounced per guild."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import discord

from . import comp as comp_mod
from . import ops as ops_mod
from . import perms
from . import raid_views as views
from .discord_people import PeopleMixin
from .discord_registry import registration_card, registration_view
from .registry import Registry, RegistryError

log = logging.getLogger(__name__)

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
        """Post (or move) the persistent card and try to make the channel read-only for members. Returns a status note.
        The new card is posted FIRST; the old one is deleted only once the new one exists (a refused send loses nothing)."""
        notes = []
        old_ch, old_id = reg.config.registration_channel_id, reg.config.registration_message_id
        msg = await ch.send(embed=registration_card(reg), view=registration_view(reg))
        await self._drop_old_card(old_ch, old_id, msg.id)
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
        with reg.config_lock:
            reg.config.registration_channel_id, reg.config.registration_message_id = ch.id, msg.id
            reg.save_config(f"registration channel → #{ch.name}", notify=False)
        return " · ".join(notes)

    async def _drop_old_card(self, ch_id, msg_id, keep_id) -> None:
        """Delete a card the bot replaced (after the new one was posted); a card already gone is fine."""
        if not (ch_id and msg_id) or msg_id == keep_id:
            return
        old = self.get_channel(ch_id)
        if old:
            try:
                await (await old.fetch_message(msg_id)).delete()
            except Exception:  # noqa: BLE001 — deleted by hand already
                pass


# ---------------------------------------------------------------- absences channel

class AbsenceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"abs(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<action>new|mine|back|clear)(?::(?P<uid>[0-9]{1,20}):(?P<start>[0-9]{4}-[0-9]{2}-[0-9]{2}))?"):
    """The absences buttons; every id carries the guild (`abs.<gk>:new|mine|back`, a Clear `abs.<gk>:clear:<uid>:<start>`) and
    holds no state, so a press after a restart works. new/mine/back live on the card (a channel card: they work only in
    its server); a Clear sits on the "My absences" reply and opens the I'm back screen for that one absence."""

    LABELS = {"new": ("I'll be away", discord.ButtonStyle.primary), "back": ("I'm back", discord.ButtonStyle.secondary),
              "mine": ("My absences", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, gk: str | None, uid: int | None = None, start: str | None = None, label: str | None = None):
        text, style = self.LABELS.get(action, (label or "Clear", discord.ButtonStyle.secondary))
        cid = f"abs{'.' + gk if gk else ''}:{action}" + (f":{uid}:{start}" if action == "clear" else "")
        super().__init__(discord.ui.Button(label=(label or text)[:80], style=style, custom_id=cid))
        self.action, self.gk, self.uid, self.start = action, gk, uid, start

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], match["g"], int(match["uid"]) if match["uid"] else None, match["start"], label=item.label)

    async def callback(self, interaction: discord.Interaction):
        from .wizard_flows import FLOWS

        if self.action == "clear":  # the My absences reply (ephemeral, in a channel or a DM): the guild comes from the id
            from . import guild_routing

            reg, interaction = await guild_routing.resolve_button(interaction, self.gk, legacy=lambda r: self.uid in r.members)
            if reg is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message("That absence's server isn't served here any more.", ephemeral=True)
                return
            if self.uid != interaction.user.id:
                await interaction.response.send_message("That's someone else's absence.", ephemeral=True)
                return
            await FLOWS["absence_clear"](interaction, reg, start=self.start)
            return
        from .discord_registry import channel_card_guild

        reg = channel_card_guild(interaction, self.gk)  # a channel-only card: strict, never a DM fallback
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if self.action == "new":  # the same wizard as /me absent add: pick the first day and how long, nothing typed
            await FLOWS["absence"](interaction, reg)
            return
        if self.action == "back":  # the same screen as /me absent clear: pick the absence, see what re-opens, I'm back
            await FLOWS["absence_clear"](interaction, reg)
            return
        m = reg.members.get(interaction.user.id)
        today = reg.now_local().date().isoformat()
        ups = m.upcoming_absences(today) if m else []
        if not ups:
            await interaction.response.send_message("No upcoming absences.", ephemeral=True)
            return
        await interaction.response.send_message(my_absences_text(reg, ups), view=clear_absences_view(reg, m, ups), ephemeral=True)


def my_absences_text(reg: Registry, ups: list) -> str:
    """A member's own upcoming absences, days in words (the clock rule), with their reason (it's theirs)."""
    return "\n".join(f"• {reg.span_label(a.start, a.end)}" + (f" — {a.reason}" if a.reason else "") for a in ups)[:1900]


def clear_absences_view(reg: Registry, m, absences: list) -> discord.ui.View:
    """The "My absences" reply's buttons: one Clear per absence (≤5, DynamicItems — no held state), then I'm back… to pick
    from a longer list. Each opens the I'm back screen (what re-opens, then the clear)."""
    view = discord.ui.View(timeout=None)
    for a in absences[:5]:
        view.add_item(AbsenceButton("clear", reg.key, m.discord_id, a.start, label=f"Clear {reg.span_label(a.start, a.end)}"))
    if len(absences) > 5:
        view.add_item(AbsenceButton("back", reg.key, label="I'm back… (pick one)"))
    return view


def absences_card(reg: Registry) -> discord.Embed:
    """The public card (D28): names + days in words, no reasons; test puppets never appear (a rehearsal stays private)."""
    today = reg.now_local().date().isoformat()
    rows = sorted(((m, a) for m in reg.members.values() if not m.test for a in m.absences if a.end >= today), key=lambda x: x[1].start)
    e = discord.Embed(title=f"{reg.config.name} · away", colour=TEAL,
                      description="Going to miss some days? Press **I'll be away**. Sheets on those days have you as *No thanks* automatically, and if you were already rostered the seat is handed back. "
                                  "Back early? **I'm back** re-opens those sheets for you. Reasons stay with the officers.")
    if rows:
        lines = [f"**{m.display_name}** · {reg.span_label(a.start, a.end)}" for m, a in rows]
        text = ""
        for i, line in enumerate(lines):
            more = f"\n…and {len(lines) - i} more" if i < len(lines) else ""
            if len(text) + len(line) + len(more) + 1 > 1000:
                text += more
                break
            text += ("\n" if text else "") + line
        e.add_field(name="Upcoming", value=text, inline=False)  # D28: names + days in words
    e.set_footer(text="Also: /me absent add · the Me page on the website")
    return e


def absences_view(reg: Registry) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(AbsenceButton("new", reg.key))
    v.add_item(AbsenceButton("back", reg.key))
    v.add_item(AbsenceButton("mine", reg.key))
    return v


class AbsencesMixin(PeopleMixin):
    """The absences channel (card, public lines) — and, through PeopleMixin, the people verbs (save_characters, im_back,
    record_absence, transfer_owner), so the client's class list stays as it is."""

    async def post_absences_card(self, reg: Registry, ch: discord.TextChannel, by: str) -> None:
        """Post (or move) the absences card: the new card FIRST, then the old one is deleted (a refused send loses nothing)."""
        old_ch, old_id = reg.config.absences_channel_id, reg.config.absences_message_id
        msg = await ch.send(embed=absences_card(reg), view=absences_view(reg))
        drop = getattr(self, "_drop_old_card", None)
        if drop is not None:
            await drop(old_ch, old_id, msg.id)
        try:
            await msg.pin()
        except Exception:  # noqa: BLE001
            pass
        with reg.config_lock:
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
        ch = self.get_channel(reg.config.absences_channel_id) if reg.config.absences_channel_id and not m.test else None
        if ch:  # a test puppet's absence stays in the rehearsal: no public line, no card change (the ops line below remains)
            try:
                await ch.send(f"🛫 **{m.display_name}** is away {span}" + (f" · off {len(touched)} sheet(s)" if touched else ""), allowed_mentions=discord.AllowedMentions.none())
            except Exception:  # noqa: BLE001
                pass
        if not m.test:
            await self.refresh_absences_card(reg)
        await self.ops.emit(reg.config, "info", f"{m.display_name} absent {span}" + (f" — {a.reason}" if a.reason else "") + (f" (by {by})" if by != m.display_name else "") + (f" · sheets: {', '.join(touched)}" if touched else ""))
        return touched

    async def absence_cleared(self, reg: Registry, m, a, by: str) -> list[str]:
        """After Registry.clear_absence: the cleared span ripples into the live sheets (after_absence_cleared), the
        card is refreshed, one ops line. Returns the member-facing lines (which sheets they can answer again)."""
        span = reg.span_label(a.start, a.end)  # people read it: "Sat 1 Nov → Mon 3 Nov", never ISO
        lines = await self.after_absence_cleared(reg, m, a)
        if not m.test:
            await self.refresh_absences_card(reg)
        await self.ops.emit(reg.config, "info", f"{m.display_name} cleared absence {span}" + (f" (by {by})" if by != m.display_name else "") + (f" · {len(lines)} sheet(s) touched" if lines else ""))
        return lines


# ---------------------------------------------------------------- setup from the web + test bench

CHANNEL_KINDS = {"ops": "ops_channel_id", "applications": "applications_channel_id", "signup": "signup_channel_id", "roster": "roster_channel_id",
                 "registration": "registration_channel_id", "analytics": "analytics_channel_id", "absences": "absences_channel_id", "news": "news_channel_id",
                 "loot": "loot_channel_id"}  # loot: the public award log (one line per confirmed award; LootLogMixin)
# what each channel is for — the channels hub, the Config page's Channels card (GET /api/admin/channels) and the Setup card read these
CHANNEL_LABELS: dict[str, tuple[str, str]] = {
    "registration": ("Registration", "public, read-only: the registration card with its buttons"),
    "signup": ("Signups", "public: one sheet per run (Join / Bench / No thanks)"),
    "absences": ("Absences", "public: the I'll be away card; one line per absence"),
    "roster": ("Roster (officers)", "health, lock and fill cards; the loot council's thread"),
    "analytics": ("Analytics (officers)", "one readiness card per raid + the change log"),
    "applications": ("Applications (officers)", "review cards for /apply (defaults to ops)"),
    "ops": ("Ops (officers)", "one line per action the bot takes; plain-text config by @mention"),
    "news": ("News", "where the news webhook posts; the bot keeps the items about our game"),
    "loot": ("Loot log", "public: one line per confirmed award; empty = each run's updates thread"),
}
REQUIRED_KINDS = ("ops", "signup")  # without these the bot can't tell anyone anything / can't open a run (the Setup card's red rows)
CARD_KINDS = ("registration", "absences", "analytics")  # a card the bot posts there (set_channel posts first, then drops the old one)
NEWS_WEBHOOK_NAME = "oibot news"  # D39: the webhook the bot creates in the news channel
PERM_CHECK_S = 3600  # the hourly permission check (design §3.4 X3)
LEFT_SWEEP_N = 20  # members looked up per guild per hourly sweep (Member.left_at without the members intent)
LEFT_SWEEP_AGE_S = 6 * 3600  # … each at most one REST fetch per 6 h (cached_member's max_age)
INVITE_CHECK_S = 86400  # Registry invite validity, checked daily (design §3.3 R-06)
UNREACHABLE = "Discord is unreachable right now — try again in a minute; nothing was changed."


def channel_name(bot, cid) -> str:
    """'#raids' for a channel the bot can see, else its mention (each surface renders <#id>)."""
    ch = bot.get_channel(int(cid)) if bot is not None and cid and getattr(bot, "get_channel", None) else None
    return f"#{ch.name}" if ch is not None and getattr(ch, "name", None) else f"<#{cid}>"


def channel_consequences(reg: Registry, kind: str, channel_id: int | None, bot=None, *, include_clear: bool = True) -> list[str]:
    """THE consequence sentences of pointing a bot channel somewhere (design §3.4 X3, §3.5 O-12): what is posted, pinned,
    forgotten or falls back — and 'I can't post in #x: missing …' when the bot lacks a permission there (perms.missing_for,
    the test set_channel runs before saving). The Discord commands' confirm, the channels hub's Review, the web's
    ReviewModal (configops.CONSEQUENCE_HOOKS) and MCP's preview print these. `include_clear=False`: leave out the generic
    clear sentences configops.op_consequences already says."""
    attr = CHANNEL_KINDS.get(kind)
    if not attr:
        return []
    cfg = reg.config
    cur = getattr(cfg, attr, None)
    cid = int(channel_id) if channel_id else None
    out: list[str] = []
    if cid is not None and cur == cid:
        return [f"{CHANNEL_LABELS.get(kind, (kind,))[0]} is already {channel_name(bot, cid)} — nothing changes."]
    if cid is None:
        if not cur:
            return []
        old = channel_name(bot, cur)
        if kind == "signup":
            if include_clear:
                out.append("Sheets will have nowhere to post until a signup channel is set.")
        elif kind == "applications":
            out.append(f"Applications fall back to the ops channel {channel_name(bot, cfg.ops_channel_id)}." if cfg.ops_channel_id
                       else "Application cards have nowhere to go; officers find them under /roster applicants.")
        elif kind == "roster":
            out.append(f"Officer cards and the loot council's thread fall back to the ops channel {channel_name(bot, cfg.ops_channel_id)}." if cfg.ops_channel_id
                       else "Officer cards and the loot council have nowhere to go until a roster or ops channel is set.")
        elif kind == "ops":
            out.append("The ops feed stops: actions and warnings go to the bot's log only (error alerts still DM).")
        elif kind in ("registration", "absences"):
            out.append(f"The {kind} card in {old} is forgotten (not deleted).")
        elif kind == "analytics":
            out.append(f"The analytics cards in {old} are forgotten (not deleted); the change log stops.")
        elif kind == "news":
            out.append(f"News posts in {old} are no longer read.")
        elif kind == "loot":
            out.append("Award lines go to each run's updates thread instead.")
        return out
    new = channel_name(bot, cid)
    old = channel_name(bot, cur) if cur else None
    if kind in ("registration", "absences"):
        had = getattr(cfg, f"{kind}_message_id", None)
        out.append(f"Posts and pins the {kind} card in {new}" + (f" and removes the old one from {old}." if had and old else "."))
        if kind == "registration":
            out.append("@everyone is made read-only there when the bot has Manage Channels (the buttons still work).")
    elif kind == "analytics":
        out.append(f"Posts one readiness card per raid in {new}" + (f"; the cards in {old} are forgotten (not deleted)." if old else "."))
    elif kind == "signup":
        out.append(f"New sheets post in {new}" + ("; sheets already open stay where they are." if old else "."))
    elif kind == "news":
        ch = bot.get_channel(cid) if bot is not None and getattr(bot, "get_channel", None) else None
        can = bool(ch is not None and getattr(getattr(ch, "guild", None), "me", None) is not None and ch.permissions_for(ch.guild.me).manage_webhooks)
        out.append(f"The bot reads the news posts in {new}; " + ("it creates a webhook there for your news source." if can
                   else "give it Manage Webhooks there and it creates the webhook for your news source (or create one yourself)."))
    elif kind == "applications":
        out.append(f"New application cards post in {new}; cards already posted stay where they are.")
    elif kind == "roster":
        out.append(f"Officer cards and the loot council's thread post in {new}.")
    if bot is not None and getattr(bot, "get_channel", None):
        ch = bot.get_channel(cid)
        if ch is not None:
            missing = perms.missing_for(ch, kind)
            if missing:
                out.append(perms.cant_post(getattr(ch, "name", str(cid)), missing))
    return out


def _channel_hook(reg: Registry, op) -> list[str]:
    """configops.CONSEQUENCE_HOOKS: a `set <kind>_channel` op's sentences on every surface's review (the web's ReviewModal,
    MCP's preview, the plain-text diff) — the client the process runs (perms.CLIENT) answers the permission check."""
    from . import configops

    if op.op != "set" or op.path not in configops.CHANNEL_PATHS:
        return []
    kind = op.path.removesuffix("_channel")
    cid = None if configops._clear_word(op.value) else configops._channel_id(op.value)
    return channel_consequences(reg, kind, cid, perms.CLIENT, include_clear=False)


def _install_hook() -> None:
    from . import configops

    if _channel_hook not in configops.CONSEQUENCE_HOOKS:
        configops.CONSEQUENCE_HOOKS.append(_channel_hook)


_install_hook()


PLAIN_MODE_WORDS = {"act": "Acts", "self": "Own record only", "answer": "Answers only", "ignore": "Ignored"}


def plain_why(reg: Registry, channel) -> str:
    """Why plain text has its mode in a channel: listed on the site | the ops / analytics channel (acts unless listed) |
    the default for other channels | DMs."""
    if channel is None:
        return "the DMs setting"
    cid = str(channel)
    if cid in reg.config.plain.channels:
        return "listed"
    if cid == str(reg.config.ops_channel_id):
        return "the ops channel acts unless listed"
    if cid == str(reg.config.analytics_channel_id):
        return "the analytics channel acts unless listed"
    return "the default for other channels"


def plain_read_text(reg: Registry, table: dict, here) -> str:
    """/gm config plain (owner and officers): where you are first — 'Here (#raids): Answers only — the default for other
    channels' (a DM: the DMs line) —, then who may change what, then the other listed channels. A read view (D37): the
    site's Ops page edits it."""
    name = (table.get("names") or {}).get(str(here)) if here is not None else None
    where = "Here (DMs)" if here is None else (f"Here (#{name})" if name else f"Here (<#{here}>)")
    mode = reg.plain_mode(here)
    lines = [f"**{where}: {PLAIN_MODE_WORDS.get(mode, mode)}** — {plain_why(reg, here)}", "", "**Who may change what**"]
    lines += [f"• {g['label']}: {g['who']}" for g in table["groups"]]
    lines += ["", "**Where**", f"• DMs to the bot: {PLAIN_MODE_WORDS.get(table['dm'], table['dm'])}",
              f"• Any other channel: {PLAIN_MODE_WORDS.get(table['default'], table['default'])}"]
    for c in table["channels"]:
        if str(c["id"]) != str(here):
            label = "#" + c["name"] if c["name"] else "<#" + str(c["id"]) + ">"
            lines.append(f"• {label}: {PLAIN_MODE_WORDS.get(c['mode'], c['mode'])} — {c['why']}")
    lines += ["• The site and MCP: Acts (not configurable)", "", "-# The owner edits this on the site (Ops › Plain-text permissions). Refusals say who may and where."]
    return "\n".join(lines)


class SetupMixin:
    async def set_channel(self, reg: Registry, kind: str, channel_id: int | None, by: str, *, via: str = "discord", by_id: int | None = None,
                          rev: str | None = None) -> str:
        """THE channel-setting verb (/gm config <kind>-channel and the channels hub, the Config page, plain text, MCP):
        refuses a channel the bot can't post in (perms.missing_for) and says so apart from "Discord is unreachable right
        now"; posts the card FIRST (registration, absences, analytics) and only then saves and drops the old one — a
        refused send loses nothing; then writes the ONE audit line (ops.audited: `by` is the person, `via` the surface,
        `by_id` marks a host superuser). No caller writes its own line. `rev`: the settings rev the author saw (the channels
        hub, a confirm screen, a structured batch) — a settings change since raises ConfigChanged before anything is posted
        and again at the save, so a stale draft never overwrites someone else's change."""
        line = await self._set_channel(reg, kind, channel_id, by, rev)
        ops = getattr(self, "ops", None)
        if ops is not None:
            await ops_mod.audited(ops, reg, by, line, via=via, by_id=by_id)
        self.__dict__.setdefault("_perm_warned", {}).pop((reg.key, kind), None)  # the hourly check looks at the new channel afresh
        return line

    def _channel_or_refuse(self, reg: Registry, kind: str, channel_id: int):
        """The channel object for a set, or the refusal in words: Discord unreachable (the gateway isn't up, or the
        server is unavailable) vs. a channel that really isn't a text channel of this server the bot can post in."""
        ch = self.get_channel(channel_id)
        if ch is None:
            ready = getattr(self, "is_ready", None)
            guild = self.get_guild(reg.config.discord_guild_id) if getattr(self, "get_guild", None) else None
            if (ready is not None and not ready()) or (getattr(self, "get_guild", None) and (guild is None or getattr(guild, "unavailable", False))):
                raise ValueError(UNREACHABLE)
            raise ValueError("that channel isn't a text channel I can see")
        if not isinstance(ch, discord.TextChannel):
            raise ValueError(f"#{getattr(ch, 'name', channel_id)} isn't a text channel — pick a text channel")
        if getattr(getattr(ch, "guild", None), "id", None) != reg.config.discord_guild_id:
            raise ValueError("That channel is in another server.")  # the site, MCP and plain text all come through here
        missing = perms.missing_for(ch, kind)
        if missing:
            raise ValueError(perms.cant_post(ch.name, missing))
        return ch

    async def _set_channel(self, reg: Registry, kind: str, channel_id: int | None, by: str, rev: str | None = None) -> str:
        attr = CHANNEL_KINDS.get(kind)
        if not attr:
            raise ValueError(f"unknown channel kind {kind}")
        ch = self._channel_or_refuse(reg, kind, channel_id) if channel_id else None
        reg.check_rev("settings", rev)  # before any card is posted: a stale draft posts nothing
        if not ch:
            with reg.config_lock:
                reg.check_rev("settings", rev)
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
                with reg.config_lock:
                    reg.check_rev("settings", rev)
                    reg.config.analytics_channel_id, reg.config.analytics_message_ids = ch.id, {}
                    reg.save_config(f"analytics channel → #{ch.name} (by {by})", notify=False)
            msgs = await self.refresh_pool(reg)
            return f"{len(msgs)} analytics card(s) posted in #{ch.name}"
        with reg.config_lock:
            reg.check_rev("settings", rev)
            setattr(reg.config, attr, ch.id)
            reg.save_config(f"{kind} channel → #{ch.name} (by {by})")
        return f"{kind} channel → #{ch.name}"

    # ---- D39: the bot creates the news webhook
    async def news_webhook(self, reg: Registry, *, create: bool = True) -> dict:
        """The news channel's webhook for the owner's news source: the bot's own (created when missing and `create`) when it
        has Manage Webhooks there, else what to add. {url (a secret: the owner's eyes only — never an ops line), note,
        created}. url None = no webhook the bot can hand out."""
        cid = reg.config.news_channel_id
        ch = self.get_channel(cid) if cid else None
        if ch is None:
            return {"url": None, "created": False, "note": "No news channel is set (or I can't see it)."}
        me = getattr(getattr(ch, "guild", None), "me", None)
        if me is None or not ch.permissions_for(me).manage_webhooks:
            return {"url": None, "created": False,
                    "note": f"I can't create the news webhook: give the bot Manage Webhooks in #{ch.name} (or create one yourself: Edit Channel → "
                            "Integrations → Webhooks) and point your news source at it."}
        try:
            hooks = await ch.webhooks()
            own = getattr(self, "user", None)
            mine = next((h for h in hooks if h.name == NEWS_WEBHOOK_NAME and (own is None or getattr(h.user, "id", None) == own.id)), None)
            created = False
            if mine is None and create:
                mine = await ch.create_webhook(name=NEWS_WEBHOOK_NAME, reason=f"oibot_GM: the news source posts here ({reg.config.name})")
                created = True
        except discord.HTTPException as e:
            return {"url": None, "created": False, "note": f"Discord refused the webhook: {e.text or e}"}
        if mine is None:
            return {"url": None, "created": False, "note": f"No webhook of mine in #{ch.name} yet."}
        return {"url": mine.url, "created": created,
                "note": f"Point your news source at this webhook; posts in #{ch.name} are filtered by our game version, raids and the news keywords."}

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
        self.__dict__.setdefault("_invite_status", {}).pop(reg.key, None)
        return inv.url

    async def invite_status(self, reg: Registry, *, max_age: float = INVITE_CHECK_S) -> dict:
        """Is the homepage's invite still good? {state none | ok | dead | unknown, text, checked} — one fetch_invite per
        guild per `max_age` (cached; a new invite clears it). 'dead' = Discord's own Unknown Invite; anything else Discord
        didn't answer is 'unknown', never dead."""
        url = reg.config.invite_url
        if not url:
            return {"state": "none", "text": "No invite link — the homepage has no Join button.", "checked": None}
        cache: dict = self.__dict__.setdefault("_invite_status", {})
        hit = cache.get(reg.key)
        now = time.time()
        if hit and hit["url"] == url and now - hit["at"] < max_age:
            return hit["status"]
        fetch = getattr(self, "fetch_invite", None)
        if fetch is None:
            return {"state": "unknown", "text": "Couldn't check the invite.", "checked": None}
        try:
            await fetch(url)
            status = {"state": "ok", "text": "The invite works.", "checked": now}
        except discord.NotFound:
            status = {"state": "dead", "text": "The invite is dead (expired or deleted) — the homepage's Join button fails. Create a new one.", "checked": now}
        except Exception as e:  # noqa: BLE001 — a blip isn't a dead invite
            return {"state": "unknown", "text": f"Couldn't check the invite ({type(e).__name__}).", "checked": None}
        cache[reg.key] = {"url": url, "at": now, "status": status}
        return status

    def guild_channels(self, reg: Registry) -> list[dict]:
        g = self.get_guild(reg.config.discord_guild_id)
        if not g:
            return []
        return [{"id": str(c.id), "name": c.name, "category": c.category.name if c.category else None} for c in sorted(g.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position))]

    def guild_roles(self, reg: Registry) -> list[str]:
        g = self.get_guild(reg.config.discord_guild_id)
        return [r.name for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default() and not r.managed] if g else []

    def plain_table(self, reg: Registry) -> dict:
        """The plain-text permissions in words (D37: Discord READS them, the site edits): who may change what (the
        groups, role names resolved against the live guild), where plain text acts (DMs, other channels, every listed
        channel plus the ops / analytics channels that act unless listed) and why each mode holds. It needs the guild's
        roles and channel names, so it lives on the client, not on Registry. /gm config plain renders it."""
        from .registry import PLAIN_GROUPS

        cfg = reg.config
        g = self.get_guild(cfg.discord_guild_id)
        if g is not None:
            reg.cache_roles(g)
        names = {c["id"]: c["name"] for c in self.guild_channels(reg)}
        groups = [{"id": gid, "label": label, "who": reg.plain_who_text(gid)} for gid, (label, _default, _ex) in PLAIN_GROUPS.items()]
        ids = list(cfg.plain.channels) + [str(c) for c in (cfg.ops_channel_id, cfg.analytics_channel_id) if c and str(c) not in cfg.plain.channels]
        channels = [{"id": cid, "name": names.get(cid), "mode": reg.plain_mode(cid), "why": plain_why(reg, cid)} for cid in dict.fromkeys(ids)]
        return {"groups": groups, "channels": channels, "dm": cfg.plain.dm, "default": cfg.plain.default, "names": names}

    def role_refusal(self, reg: Registry, rid: int | None) -> str | None:
        """Why a role can't be an officer (or member) role: @everyone, a bot's or an integration's managed role, a role this
        server doesn't have. None = fine, or the server can't be seen (no gateway: the id is kept, resolved on ready)."""
        g = self.get_guild(reg.config.discord_guild_id) if getattr(self, "get_guild", None) else None
        if g is None or rid is None:
            return None
        role = g.get_role(int(rid)) if hasattr(g, "get_role") else None
        if role is None:
            return f"This server has no role with id {rid}."
        if role.is_default():
            return "@everyone can't be an officer role — everyone would be an officer."
        if role.managed:
            return f"@{role.name} belongs to a bot or an integration — pick a role people hold."
        return None

    def discord_links(self, reg: Registry) -> dict:
        """'Open in Discord' links: the server and each configured channel (discord.com/channels/<server>/<channel>)."""
        gid = reg.config.discord_guild_id
        base = f"https://discord.com/channels/{gid}"
        return {"server": base, "channels": {k: f"{base}/{getattr(reg.config, a)}" for k, a in CHANNEL_KINDS.items() if getattr(reg.config, a, None)}}

    # ---- who left the server (design §3.3 cluster D): without the privileged members intent on_member_remove never
    # arrives, so Member.left_at follows Discord's own answers: a 404 from fetch_member marks it, any sighting clears it
    def note_membership(self, guild_id: int | None, uid: int, present: bool) -> None:
        """Discord answered for (guild, uid): `present` False = its own 404 (not a timeout — cached_member never calls this
        for a lookup Discord didn't answer). A registered, non-test member's `left_at` is set or cleared to match (off the
        loop: a git commit). No notice, no DM."""
        regs = getattr(self, "registries", None)
        reg = regs.for_guild(guild_id) if regs is not None and guild_id else None
        m = reg.members.get(int(uid)) if reg is not None else None
        if m is None or m.test or bool(m.left_at) != present:  # nothing to change: already as Discord says
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            reg.member_left(m.discord_id, not present)
            return
        tasks: set = self.__dict__.setdefault("_left_tasks", set())
        t = loop.create_task(asyncio.to_thread(reg.member_left, m.discord_id, not present))
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def sweep_left(self, reg: Registry, limit: int = LEFT_SWEEP_N) -> int:
        """The hourly sweep: up to `limit` registered members not marked as left, longest unchecked first, looked up through
        `cached_member` (at most one REST fetch each per LEFT_SWEEP_AGE_S) — its 404s mark `left_at` (note_membership). A
        guild of a few hundred is covered every few hours without the members intent. Returns how many were looked up."""
        look = getattr(self, "cached_member", None)
        guild = self.get_guild(reg.config.discord_guild_id) if getattr(self, "get_guild", None) else None
        if look is None or guild is None or getattr(guild, "unavailable", False):
            return 0
        seen: dict = self.__dict__.setdefault("_left_swept", {})
        todo = sorted((m for m in reg.members.values() if not m.test and not m.left_at), key=lambda m: seen.get((reg.key, m.discord_id), 0.0))[:limit]
        for m in todo:
            seen[(reg.key, m.discord_id)] = time.time()
            try:
                await look(guild, m.discord_id, max_age=LEFT_SWEEP_AGE_S)
            except Exception:  # noqa: BLE001 — a lookup Discord didn't answer is no answer; the next sweep tries again
                continue
        return len(todo)

    # ---- the permission check (hourly) and the Setup card
    def channel_problems(self, reg: Registry) -> dict[str, str]:
        """kind → what is wrong with that configured channel now: gone (deleted / invisible) or 'I can't post in #x:
        missing …' (perms.missing_for). {} when the server itself isn't visible (an outage is not a problem to report)."""
        g = self.get_guild(reg.config.discord_guild_id) if getattr(self, "get_guild", None) else None
        if g is None or getattr(g, "unavailable", False):
            return {}
        out = {}
        for kind, attr in CHANNEL_KINDS.items():
            cid = getattr(reg.config, attr, None)
            if not cid:
                continue
            ch = self.get_channel(cid)
            if ch is None:
                out[kind] = f"the {CHANNEL_LABELS[kind][0].lower()} channel <#{cid}> is gone (deleted, or I can't see it)"
                continue
            missing = perms.missing_for(ch, kind)
            if missing:
                out[kind] = perms.cant_post(getattr(ch, "name", str(cid)), missing)
        return out

    async def check_channel_perms(self, reg: Registry) -> dict[str, str]:
        """One pass of the permission check: ONE ops line listing what newly went wrong (a problem already reported is not
        repeated; a fixed one is forgotten, so a relapse is reported again) — an ERROR when a REQUIRED_KINDS channel (ops,
        signups) is among them: without it the bot can't tell anyone anything or open a run, so the owner must act and gets
        the error alert (error_dm_ids, throttled by Ops.emit — the DM rule's one exception); a warn otherwise. Returns the
        current problems."""
        problems = self.channel_problems(reg)
        warned: dict = self.__dict__.setdefault("_perm_warned", {})
        for k in [k for k in warned if k[0] == reg.key and k[1] not in problems]:
            warned.pop(k, None)
        fresh = {kind: text for kind, text in problems.items() if warned.get((reg.key, kind)) != text}
        if fresh:
            for kind, text in fresh.items():
                warned[(reg.key, kind)] = text
            ops = getattr(self, "ops", None)
            if ops is not None:
                level = "error" if any(k in REQUIRED_KINDS for k in fresh) else "warn"
                await ops.emit(reg.config, level, "Channel check: " + " · ".join(fresh.values()) + " — /gm config channels or the Config page fixes it.")
                if "ops" in fresh and getattr(ops, "known", None) is not None:  # the posts that fail next say the same thing: no second DM
                    from .ops import OPS_CHANNEL_SIG

                    ops.known(reg.key, OPS_CHANNEL_SIG)
        return problems

    async def setup_tick(self, now: float | None = None) -> None:
        """The scheduler's hook (raid_scheduler.scheduler_tick): the permission check hourly and the invite daily, per guild;
        a dead invite warns once."""
        now = time.time() if now is None else now
        perms.CLIENT = self  # configops' channel consequence hook checks permissions with the running client
        last: dict = self.__dict__.setdefault("_setup_checked", {})
        regs = getattr(self, "registries", None)
        for reg in (regs.ordered() if regs is not None else []):
            if reg.key in last and now - last[reg.key] < PERM_CHECK_S:
                continue
            last[reg.key] = now
            try:
                await self.check_channel_perms(reg)
                await self.sweep_left(reg)
                st = await self.invite_status(reg)
                dead: set = self.__dict__.setdefault("_invite_dead_warned", set())
                if st["state"] == "dead" and (reg.key, reg.config.invite_url) not in dead:
                    dead.add((reg.key, reg.config.invite_url))
                    await self.ops.emit(reg.config, "warn", st["text"] + " (/gm config recruitment or the Config page)")
            except Exception:  # noqa: BLE001 — a check never stops the scheduler
                log.exception("setup check failed for %s", reg.key)

    def setup_checklist(self, reg: Registry) -> list[dict]:
        """The Setup card / `/gm status` checklist: rows {key, label, state ok | warn | bad, text, test} — every ✓ computed
        by the same test the runtime uses (perms.missing_for for channels and the server, the role list, the owner)."""
        cfg = reg.config
        g = self.get_guild(cfg.discord_guild_id) if getattr(self, "get_guild", None) else None
        rows = []
        rows.append({"key": "owner", "label": "Owner", "state": "ok" if cfg.owner_discord_id else "bad",
                     "text": f"<@{cfg.owner_discord_id}>" if cfg.owner_discord_id else "nobody — /gm config owner", "test": "config.owner_discord_id"})
        if cfg.detached_at:
            rows.append({"key": "detached", "label": "Server", "state": "bad", "text": "the bot is no longer in this guild's Discord server — invite it back",
                         "test": "config.detached_at (on_guild_remove)"})
        problems = self.channel_problems(reg)
        for kind, attr in CHANNEL_KINDS.items():
            cid = getattr(cfg, attr, None)
            label = CHANNEL_LABELS[kind][0]
            if not cid:
                rows.append({"key": f"channel:{kind}", "label": label, "state": "bad" if kind in REQUIRED_KINDS else "warn", "text": "not set",
                             "test": f"config.{attr}"})
            elif kind in problems:
                rows.append({"key": f"channel:{kind}", "label": label, "state": "bad", "text": problems[kind], "test": f"perms.missing_for(<#{cid}>, {kind!r})"})
            else:
                rows.append({"key": f"channel:{kind}", "label": label, "state": "ok", "text": f"<#{cid}>", "test": f"perms.missing_for(<#{cid}>, {kind!r})"})
        roles = reg.officer_role_names()
        rows.append({"key": "officer_roles", "label": "Officer roles", "state": "ok" if cfg.officer_role_ids else "warn",
                     "text": ", ".join(roles) if cfg.officer_role_ids else "none — Manage Server only", "test": "config.officer_role_ids"})
        if g is not None:
            missing = perms.missing_for(g)
            rows.append({"key": "permissions", "label": "Server permissions", "state": "bad" if missing else "ok",
                         "text": ("missing " + perms.words(missing)) if missing else "everything the invite asks for", "test": "perms.missing_for(guild)"})
            for p in perms.optional_missing(g):
                rows.append({"key": f"optional:{p}", "label": perms.WORDS.get(p, p), "state": "warn", "text": f"optional — with it, {perms.OPTIONAL[p]}",
                             "test": f"guild.me.guild_permissions.{p}"})
        targets = reg.error_dm_targets()
        rows.append({"key": "error_dms", "label": "Error alerts", "state": "ok" if targets else "warn", "text": reg.error_dm_words(), "test": "config.error_dm_ids"})
        return rows

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


def award_notice(reg: Registry, award, was: str | None) -> None:
    """The public loot-log line's member copy (D26): the recipient's inbox says what they got — never the reason or a
    score (officer data), never a DM. A correction ('was Kestrel, now Mira') is copied to BOTH: the new recipient and the
    member who held it (`was` = their character), so the one who lost the item hears of it too. A disenchant (as the new
    holder) or an award not tied to a member writes nothing for that side."""
    from .loot.session import DISENCHANT

    line = loot_log_line(reg, award, was).replace("**", "").removeprefix("🎁 ").removeprefix("✎ ")
    text = ("Loot correction: " if was else "Loot: ") + line
    to: list[int] = []
    if award.raider != DISENCHANT and award.discord_id is not None:
        to.append(int(award.discord_id))
    if was and was != DISENCHANT:
        try:
            hit = reg.find(was)
        except RegistryError:  # two characters share the name: nobody is guessed
            hit = None
        if hit is not None and hit[0].discord_id not in to:
            to.append(hit[0].discord_id)
    for uid in to:
        try:
            reg.notify(uid, "loot_award", text)
        except Exception:  # noqa: BLE001 — the award is recorded; the notice is a copy
            log.exception("loot award notice failed")


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
        award_notice(reg, award, was)  # the line's member copy on their Me page (D26), log on or off — never a DM
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
