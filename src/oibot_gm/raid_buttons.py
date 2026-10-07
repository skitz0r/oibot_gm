"""Persistent buttons for the cycle (DynamicItems with a `custom_id` template, so they survive restarts):
SignupButton (Join / Bench / No thanks, Can't make it once locked, with the character picker for members with several
characters), FillButton (Confirm / Can't make it on a fill DM, same pair as the confirmation DM), PlaceButton (Confirm / Can't make it on a confirmation DM),
RunButton (officer Fill seats / Lock now / Cancel run on the health and lock cards), and the plain-View factories
the DMs use (`place_view`, `fill_view`, `sheet_view`). Callbacks call the `RaidMixin` methods on `interaction.client`.

Every id carries its guild (`raid.<gk>:…`, design §5.32): a press resolves to that guild without asking, wherever it is
pressed (a DM included), and is refused in any other server. Old ids (`raid:…`) still match — `g` is None — and resolve
as before (guild_routing.resolve_button), with each button's own matcher deciding between several guilds in a DM.

Import cycle: see `raid_views` — the helpers this module formats replies with are imported at the bottom."""
from __future__ import annotations

import logging
import re

import discord

from . import raidcycle as rc
from .guild_routing import resolve_button
from .registry import RegistryError

log = logging.getLogger(__name__)


class RunButton(discord.ui.DynamicItem[discord.ui.Button], template=r"runact(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<key>[A-Za-z0-9_\-]+):(?P<action>fill|lock|cancel|suggest|ask)(?::(?P<rev>[0-9a-f]{6,16}))?"):
    """Officer buttons on the health and lock cards: Fill seats (the suggestions screen: the batch, then Send), Lock now, Cancel run, and
    the Suggested block's Ask (sends what the card shows; the plan fingerprint it was drawn with rides in the id, so a
    stale card is refused and redrawn) and Suggest… (every seat with its alternates: pick, then send)."""
    LABELS = {"fill": ("Fill seats", discord.ButtonStyle.secondary), "lock": ("Lock now", discord.ButtonStyle.secondary), "cancel": ("Cancel run", discord.ButtonStyle.danger),
              "ask": ("Ask", discord.ButtonStyle.success), "suggest": ("Suggest…", discord.ButtonStyle.secondary)}

    def __init__(self, gk: str | None, key: str, action: str, disabled: bool = False, label: str | None = None, rev: str | None = None):
        """`gk`: the guild key the id carries (None = the old format, from a match only). `disabled`/`label` are
        render-time only (the card is re-rendered on every state change): a button that does not apply yet stays in its
        place, greyed, instead of disappearing. `rev`: Ask's plan fingerprint."""
        text, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label or text, style=style, custom_id=f"runact{_g(gk)}:{key}:{action}" + (f":{rev}" if rev else ""), disabled=disabled))
        self.gk, self.key, self.action, self.rev = gk, key, action, rev

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["key"], match["action"], rev=match["rev"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client

        async def legacy(r):
            return _live(bot, r, self.key) and await bot.is_officer_anywhere(r, interaction.user.id)
        reg, interaction = await resolve_button(interaction, self.gk, legacy)
        if not reg or not await bot.is_officer_anywhere(reg, interaction.user.id):
            if not interaction.response.is_done():
                await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That run is closed.", ephemeral=True)
            return
        team = rc.run_team(reg, ev)
        by = interaction.user.display_name
        if self.action == "ask":  # the card's suggestions as drawn — refused (and the card redrawn) when the plan moved
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                # only the seats the card draws (SUGGEST_LINES) — never one it left under "+N more"
                sent, plan = await bot.send_plan(reg, rs, ev, rev=self.rev or "", by=by, limit=SUGGEST_LINES)
            except rc.PlanChanged as e:
                await bot.refresh_cards(reg, ev)
                lock_cards = {mid for k, mid in ev.cards.items() if k.startswith("lock:")}
                if ev.state != "open" and getattr(interaction.message, "id", None) not in lock_cards:  # the open sheet's health card, pressed after lock
                    await interaction.followup.send("This run has locked, so this card's suggestions are closed — the lock card has the roster and its own Ask.", ephemeral=True)
                    return
                await interaction.followup.send(f"{e} The card shows them as they are now.", ephemeral=True)
                return
            except RegistryError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            await interaction.followup.send((("Asked " + ", ".join(a.display_name for a in sent) + " — their answers post in the run's thread.") if sent else "Nobody could be asked (DMs off or nobody left).") + rc.held_back_words(plan), ephemeral=True)
            return
        if self.action == "suggest":
            from .wizard import FLOWS

            await FLOWS["raid_suggest"](interaction, reg, key=ev.key)
            return
        if self.action == "fill":
            if sheet_state(ev) != "locked" or not ev.all_rosters:
                await interaction.response.send_message("Fill works after lock — the sheet is still open.", ephemeral=True)
                return
            nd = rc.needs(reg, ev, team)
            if not nd["headcount"] and not nd["roles"]:
                await interaction.response.send_message("Nothing to fill — every seat is taken.", ephemeral=True)
                return
            from .wizard import FLOWS

            # the suggestions screen: the exact batch Send DMs (send_plan with the plan's rev — a stale one is refused
            # and re-shown), nothing sent before Send
            await FLOWS["raid_suggest"](interaction, reg, key=ev.key)
            return
        if self.action == "lock":
            if ev.state != "open":
                await interaction.response.send_message("Already locked.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                line = await bot.lock_run(reg, rs, ev, by=by)
            except RegistryError as e:  # someone else is locking it (rc.SolverBusy), or it stopped being open: nothing happened
                await interaction.followup.send(str(e), ephemeral=True)
                return
            except Exception as e:  # noqa: BLE001 — lock_run reports solver failures itself; this is anything else
                await interaction.followup.send(f"lock failed: {e}", ephemeral=True)
                await bot.ops.emit(reg.config, "warn", f"{ev.key}: lock by {by} failed — {e}")
                return
            await interaction.followup.send(line, ephemeral=True)
            if not ev.lock_error:
                await bot.ops.emit(reg.config, "info", line)
            return
        if self.action == "cancel":
            view = discord.ui.View(timeout=60)
            keep = discord.ui.Button(label="Keep the run", style=discord.ButtonStyle.secondary)
            yes = discord.ui.Button(label="Cancel the run", style=discord.ButtonStyle.danger)

            async def kept(i: discord.Interaction):
                await i.response.edit_message(content="Nothing changed — the run stays.", view=None)

            keep.callback = kept

            async def do(i: discord.Interaction):
                await i.response.edit_message(content="Cancelling…", view=None)
                line = await bot.cancel_run(reg, rs, ev, by=by, reason=None)
                await i.edit_original_response(content=line)

            yes.callback = do
            view.add_item(keep)  # the safe choice first, the destructive one last and red
            view.add_item(yes)
            await interaction.response.send_message(f"Cancel {run_label(reg, ev)}? Rostered members are not told automatically.", view=view, ephemeral=True)


# ---------------------------------------------------------------- persistent buttons

class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<key>[A-Za-z0-9_\-]+):(?P<status>in|tentative|out|sub|cant)"):
    """The sheet's member buttons: Join / Bench / No thanks while open; a single Can't make it once locked, which
    releases a rostered member's seat (anyone else: nothing to do)."""
    STYLES = {"in": discord.ButtonStyle.success, "sub": discord.ButtonStyle.primary, "out": discord.ButtonStyle.secondary, "cant": discord.ButtonStyle.secondary}  # grey like the same choice in a DM; red is for destructive officer actions

    def __init__(self, gk: str | None, key: str, status: str):
        status = "in" if status == "tentative" else status
        super().__init__(discord.ui.Button(label="Can't make it" if status == "cant" else rc.LABELS[status], style=self.STYLES[status], custom_id=f"raid{_g(gk)}:{key}:{status}"))
        self.gk, self.key, self.status = gk, key, status

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["key"], match["status"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg, interaction = await resolve_button(interaction, self.gk, lambda r: _live(bot, r, self.key))
        if not reg:
            if not interaction.response.is_done():
                await interaction.response.send_message("This sheet is closed." if not interaction.guild_id else "Not configured here.", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if not ev or sheet_state(ev) in ("done", "cancelled"):
            await interaction.response.send_message("This sheet is closed.", ephemeral=True)
            return
        team = rc.run_team(reg, ev)
        m = reg.members.get(interaction.user.id)
        if team.get("test") and not (m and getattr(m, "test", False)) and str(interaction.user.id) != str(team.get("test_by") or ""):
            await interaction.response.send_message("This is a rehearsal sheet.", ephemeral=True)
            return
        if not m or not m.active():
            where = f"in <#{reg.config.registration_channel_id}>" if reg.config.registration_channel_id else "with `/register`"
            await interaction.response.send_message(f"You're not registered yet — register a character {where} first, then press again.", ephemeral=True)
            return
        if self.status == "cant":
            if sheet_state(ev) == "open":
                await interaction.response.send_message("The sheet is still open — press **No thanks** instead.", ephemeral=True)
                return
            if not ev.seat_of(m.display_name):
                await interaction.response.send_message("You're not rostered for this run — nothing to do.", ephemeral=True)
                return
            await bot.apply_signup(interaction, reg, rs, ev, m, None, "out")
            return
        if sheet_state(ev) != "open":
            await interaction.response.send_message(rc.LOCKED_JOIN, ephemeral=True)
            return
        chars = m.active()
        if len(chars) > 1 and self.status == "in":
            # one press per character: no default to second-guess
            view = discord.ui.View(timeout=120)
            for c in chars[:5]:
                btn = discord.ui.Button(label=f"{c.label} · {c.cls} {c.spec}"[:80], style=discord.ButtonStyle.success if c.is_main else discord.ButtonStyle.secondary, emoji=bot.ico("class", c.cls) or None)

                async def pick(i: discord.Interaction, label=c.label):
                    await bot.apply_signup(i, reg, rs, ev, m, label, self.status)

                btn.callback = pick
                view.add_item(btn)
            await interaction.response.send_message(f"**{rc.LABELS[self.status]}** as which character?", view=view, ephemeral=True)
            return
        await bot.apply_signup(interaction, reg, rs, ev, m, None, self.status)


async def show_answer(card: discord.Interaction, press: discord.Interaction, line: str) -> None:
    """The answer goes on the DM card and its buttons go. `press` is `card` itself, or — an old id with several guilds —
    the DM server picker's press: then the picker message shows the line and the card is edited directly (the bot's
    own DM message), so its Yes/No never stay behind as if unanswered."""
    try:
        if press is card:
            await press.response.edit_message(content=press.message.content + f"\n\n**→ {line}**", view=None)
            return
        await press.response.edit_message(content=f"**→ {line}**", view=None)
    except Exception:  # noqa: BLE001
        if not press.response.is_done():
            await press.response.send_message(line, ephemeral=True)
    msg = getattr(card, "message", None)
    if press is not card and msg is not None:
        try:
            await msg.edit(content=(msg.content or "") + f"\n\n**→ {line}**", view=None)
        except Exception as e:  # noqa: BLE001 — the answer is saved; only the old card keeps its buttons
            log.debug("card edit after the server picker failed: %s", e)


class FillButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fill(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<key>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Yes/No on a fill DM. Survives restarts; only the person asked can answer."""

    def __init__(self, gk: str | None, key: str, uid: int, answer: str, label: str | None = None):
        """`label` is render-time only (a pre-lock ask reads "Yes, as Holy" / "No")."""
        super().__init__(discord.ui.Button(label=label or ("Confirm" if answer == "yes" else "Can't make it"), style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"fill{_g(gk)}:{key}:{uid}:{answer}"))
        self.gk, self.key, self.uid, self.answer = gk, key, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["key"], int(match["uid"]), match["answer"])

    def _asked(self, bot, r) -> bool:
        """An old id with several guilds: does this guild's run `key` hold an ask (open or last) for `uid`?"""
        ev = bot.raids.store(r).events.get(self.key)
        return bool(ev and any(a.discord_id == self.uid for a in ev.fill_asks))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        card = interaction  # the DM card's press; after the server picker (an old id, several guilds) `interaction` is the picker's
        reg, interaction = await resolve_button(interaction, self.gk, lambda r: self._asked(bot, r))
        if reg is None and interaction.response.is_done():
            return
        if not await bot.may_answer_for(interaction, reg, self.uid):
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        rs = bot.raids.store(reg) if reg else None
        ev = rs.events.get(self.key) if rs else None
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That raid is closed — thanks anyway.", ephemeral=True)
            return
        ask = next((a for a in ev.fill_asks if a.discord_id == self.uid and a.open), None)
        if not ask:
            last = next((a for a in reversed(ev.fill_asks) if a.discord_id == self.uid), None)
            closed = last is not None and last.phase == "prelock" and last.answer in ("released", "expired")
            await interaction.response.send_message("That question closed when the roster locked — nothing changed." if closed else "Already answered (or the gap was filled).", ephemeral=True)
            return
        line = rc.apply_fill_answer(reg, rs, ev, ask, self.answer == "yes")
        await show_answer(card, interaction, line)
        await bot.after_fill_answer(reg, rs, ev, ask, line)


class PlaceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"place(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<roster>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Confirm / can't make it on a locked roster (DM after lock). Persistent; only the person asked can answer."""

    def __init__(self, gk: str | None, roster: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Confirm" if answer == "yes" else "Can't make it", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"place{_g(gk)}:{roster}:{uid}:{answer}"))
        self.gk, self.roster, self.uid, self.answer = gk, roster, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["roster"], int(match["uid"]), match["answer"])

    def _asked(self, r) -> bool:
        """An old id with several guilds: does member `uid` hold an unanswered placement ask for `roster` here?"""
        return any(a["roster"] == self.roster for a in r.open_placement_asks(self.uid))

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        card = interaction  # see FillButton.callback
        reg, interaction = await resolve_button(interaction, self.gk, self._asked)
        if not reg:
            if not interaction.response.is_done():
                await interaction.response.send_message("That question has closed — nothing changed." if not interaction.guild_id else "Not configured here.", ephemeral=True)
            return
        if not await bot.may_answer_for(interaction, reg, self.uid):
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        try:
            line = reg.answer_placement(self.uid, self.roster, self.answer == "yes", interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"{e}", ephemeral=True)
            return
        await show_answer(card, interaction, line)
        await bot.after_placement_answer(reg, self.uid, self.roster, self.answer == "yes", line)


def place_view(reg, roster: str, uid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(PlaceButton(reg.key, roster, uid, "yes"))
    v.add_item(PlaceButton(reg.key, roster, uid, "no"))
    return v


def fill_view(reg, key: str, uid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(FillButton(reg.key, key, uid, "yes"))
    v.add_item(FillButton(reg.key, key, uid, "no"))
    return v


def sheet_view(reg, key: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for s in rc.STATUSES:
        v.add_item(SignupButton(reg.key, key, s))
    return v


def _g(gk: str | None) -> str:
    """The guild part of a custom id: `.<key>` (every id the bot emits now), or nothing (an old id re-created from its match)."""
    return f".{gk}" if gk else ""


def _live(bot, reg, key: str) -> bool:
    """An old id with several guilds: is run `key` live in this guild?"""
    ev = bot.raids.store(reg).events.get(key)
    return bool(ev and ev.state not in ("done", "cancelled"))


# text helpers the callbacks format with; imported last so `raid_views` (which places these buttons and imports them at
# its own bottom) always finds the classes above bound, whichever module is imported first
from .raid_views import SUGGEST_LINES, run_label, sheet_state  # noqa: E402
