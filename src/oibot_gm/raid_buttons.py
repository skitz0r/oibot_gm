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

import asyncio
import logging
import re

import discord

from . import raidcycle as rc
from .guild_routing import resolve_button
from .registry import RegistryError

log = logging.getLogger(__name__)


class RunButton(discord.ui.DynamicItem[discord.ui.Button], template=r"runact(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<key>[A-Za-z0-9_\-]+):(?P<action>fill|lock|cancel|suggest|ask|loot|remind|pause|end|reopen)(?::(?P<rev>[0-9a-f]{6,16}))?"):
    """Officer buttons on the health and lock cards: Fill seats (the suggestions screen: the batch, then Send), Lock now, Cancel run, and
    the Suggested block's Ask (sends what the card shows; the plan fingerprint it was drawn with rides in the id, so a
    stale card is refused and redrawn) and Suggest… (every seat with its alternates: pick, then send). The lock card's
    raid-night row (dual interfaces phase 2): Remind the unconfirmed (once an hour), Pause / Resume autofill, End the
    run now (after the start; the decide screen); the closed card's Reopen (an end by mistake, within the hour)."""
    LABELS = {"fill": ("Fill seats", discord.ButtonStyle.secondary), "lock": ("Lock now", discord.ButtonStyle.secondary), "cancel": ("Cancel run", discord.ButtonStyle.danger),
              "loot": ("Loot", discord.ButtonStyle.secondary),
              "ask": ("Ask", discord.ButtonStyle.success), "suggest": ("Suggest…", discord.ButtonStyle.secondary),
              "remind": ("Remind the unconfirmed", discord.ButtonStyle.secondary), "pause": ("Pause autofill", discord.ButtonStyle.secondary),
              "end": ("End the run now", discord.ButtonStyle.danger), "reopen": ("Reopen", discord.ButtonStyle.secondary)}

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
        if self.action == "reopen":  # the one press a closed run's card keeps
            await reopen_press(interaction, bot, reg, rs, ev)
            return
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
            await interaction.followup.send(sent_words(sent) + rc.held_back_words(plan), ephemeral=True)
            return
        if self.action == "suggest":
            from .wizard import FLOWS

            await FLOWS["raid_suggest"](interaction, reg, key=ev.key)
            return
        if self.action == "loot":  # the lock card is the raid-night hub: the same flow as /raid loot, this run
            if ev.state != "locked":
                await interaction.response.send_message("The loot council opens once the roster is locked.", ephemeral=True)
                return
            from .wizard import FLOWS

            await FLOWS["raid_loot"](interaction, reg, key=ev.key)
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
            await lock_decide(interaction, bot, reg, rs, ev)
            return
        if self.action == "cancel":
            await cancel_decide(interaction, bot, reg, rs, ev)
            return
        if self.action == "remind":  # reversible, low ceremony: the press is the send (once an hour per run)
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                line = await bot.remind(reg, rs, ev, by=by, by_id=interaction.user.id)
            except RegistryError as e:
                line = str(e)
            await interaction.followup.send(line, ephemeral=True)
            return
        if self.action == "pause":
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                line = await bot.set_autofill_paused(reg, rs, ev, not ev.autofill_paused, by=by, by_id=interaction.user.id)
            except RegistryError as e:
                line = str(e)
            await interaction.followup.send(line, ephemeral=True)
            return
        if self.action == "end":
            await end_decide(interaction, bot, reg, rs, ev)


def sent_words(sent: list) -> str:
    """What an officer's Ask did, from the asks themselves: by DM, or waiting on the site (DMs off / the DM failed)."""
    if not sent:
        return "Nobody could be asked (nobody left)."
    dm = [a.display_name for a in sent if getattr(a, "channel", "dm") != "web"]
    site = [a.display_name for a in sent if getattr(a, "channel", "dm") == "web"]
    return (("Asked " + ", ".join(dm) + " by DM" if dm else "")
            + ("; " if dm and site else "") + (", ".join(site) + " wait on the site (DMs off or the DM failed)" if site else "")
            + " — their answers post in the run's thread.")


# ---------------------------------------------------------------- the ONE confirm screen for lock, cancel and end (§2.3)

def officer_wizard(bot, reg, interaction: discord.Interaction, title: str):
    """A Wizard for an officer's card press: the guard re-checks officer standing (is_officer_anywhere) at every press."""
    from .wizard import Wizard

    async def guard(i: discord.Interaction) -> str | None:
        return None if await bot.is_officer_anywhere(reg, i.user.id) else "Officers only."

    return Wizard(reg, interaction.user.id, title=title, guard=guard)


async def lock_decide(interaction: discord.Interaction, bot, reg, rs, ev, wiz=None) -> None:
    """Lock — the card's Lock now and /raid lock alike: the consequence sentence (consequences.consequence — the same
    words the site's confirm shows), then Lock now · Preview lock · Back. Preview lock draws the roster the lock would
    build now (rc.split_preview under the run's solver single flight; nothing saved but `ev.last_proposal`), one member
    per line with the spec icon, and offers Lock now / Back from there. The lock itself is RaidMixin.lock_run, after the
    screen has acknowledged the press."""
    from .configops import report_error
    from .consequences import consequence

    wiz = wiz or officer_wizard(bot, reg, interaction, f"Lock {raid_name(reg, ev)}")
    key = ev.key

    async def do(i: discord.Interaction, _note: str | None) -> None:
        cur = rs.events.get(key)  # re-read: the scheduler or another officer may have locked it meanwhile
        if not cur or cur.state in ("done", "cancelled"):
            await wiz.finish(i, "That run is closed — nothing was changed.")
            return
        if cur.state != "open":
            await wiz.finish(i, "Already locked — nothing was changed.")
            return
        by = i.user.display_name
        try:
            line = await bot.lock_run(reg, rs, cur, by=by)
        except RegistryError as e:  # someone else is locking it (rc.SolverBusy), or it stopped being open: nothing happened
            await wiz.finish(i, str(e))
            return
        except Exception as e:  # noqa: BLE001 — lock_run reports solver failures itself; this is anything else
            await wiz.finish(i, await report_error(bot.ops, reg.config, e, f"{key}: lock by {by}"))
            return
        await wiz.finish(i, line)
        if not cur.lock_error:
            await bot.ops.emit(reg.config, "info", line)

    async def lock_now(i: discord.Interaction) -> None:
        await wiz.working(i, "Locking…")
        await do(i, None)

    async def preview(i: discord.Interaction) -> None:
        cur = rs.events.get(key)
        if not cur or cur.state != "open":
            await wiz.finish(i, "That run is no longer open — nothing to preview.")
            return
        await wiz.working(i, "Building the preview — up to 8 s…")
        try:
            rosters = await bot.lock_preview(reg, rs, cur, by=i.user.display_name)
        except RegistryError as e:  # the solver is busy on this run, or no roster satisfies the rules
            await wiz.refuse(i, str(e), retry=lambda ri: lock_decide(ri, bot, reg, rs, rs.events.get(key) or cur, wiz=wiz), retry_label="Back")
            return
        await wiz.show(i, f"{run_label(reg, cur)}\n" + "\n".join(consequence("lock", reg, cur)), wiz.button("Lock now", lock_now, discord.ButtonStyle.success),
                       wiz.button("Back", lambda bi: lock_decide(bi, bot, reg, rs, rs.events.get(key) or cur, wiz=wiz)),
                       embed=lock_preview_embed(reg, cur, rosters, bot.ico))

    await wiz.decide(interaction, f"{run_label(reg, ev)}\n" + "\n".join(consequence("lock", reg, ev)), do=do, do_label="Lock now",
                     extra=[wiz.button("Preview lock", preview)])


async def cancel_decide(interaction: discord.Interaction, bot, reg, rs, ev, wiz=None) -> None:
    """Cancel — ONE screen on every Discord path (the card's Cancel run, /raid cancel): the consequence (who is rostered,
    which asks close — rc.cancel_effects through consequences.consequence), then Cancel the run… · Back. The button opens
    one Form: the optional reason (officers only: the run's thread and the ops feed — never the public channel) and
    "Tell the channel?" (post a fixed line in the signup channel — the default — or not). Nobody is DMed (the DM rule).
    Submit cancels (RaidMixin.cancel_run)."""
    from .consequences import consequence
    from .wizard import Field, Form, Opt

    wiz = wiz or officer_wizard(bot, reg, interaction, f"Cancel {raid_name(reg, ev)}")
    key = ev.key

    async def submitted(si: discord.Interaction, v: dict[str, str]) -> None:
        if si.user.id != wiz.owner_id:
            await si.response.send_message("This belongs to someone else's wizard.", ephemeral=True)
            return
        why = await wiz.guard(si) if wiz.guard else None
        if why:
            await wiz.finish(si, why)
            return
        cur = rs.events.get(key)  # re-read: it may have closed while the box was open
        if not cur or cur.state in ("done", "cancelled"):
            await wiz.finish(si, "That run already closed — nothing was changed.")
            return
        await wiz.working(si, "Cancelling…")
        try:
            line = await bot.cancel_run(reg, rs, cur, by=si.user.display_name, reason=(v.get("reason") or "").strip() or None,
                                        notify=v.get("notify", "post") != "quiet", by_id=si.user.id)
        except RegistryError as e:
            line = str(e)
        await wiz.finish(si, line)

    def form() -> Form:
        return Form(f"Cancel {raid_name(reg, ev)} {reg.local12(ev.start, '%a %d %b')}", [
            Field("reason", "Why? (optional)", required=False, paragraph=True, max_length=300,
                  description="Officers only: the run's thread and the ops feed.", placeholder="never posted in the signup channel"),
            Field("notify", "Tell the channel?", [Opt("post", "Post a line in the signup channel", "fixed text, no reason", default=True),
                                                  Opt("quiet", "No line — the sheet and the thread show it")]),
        ], submitted)

    async def open_box(i: discord.Interaction) -> None:
        cur = rs.events.get(key)
        if not cur or cur.state in ("done", "cancelled"):
            await wiz.finish(i, "That run already closed — nothing was changed.")
            return
        await wiz.open_form(i, form())

    async def back(i: discord.Interaction) -> None:
        await wiz.finish(i, "Nothing was changed.")

    await wiz.show(interaction, f"{run_label(reg, ev)}\n" + "\n".join(consequence("cancel", reg, ev)),
                   wiz.button("Cancel the run…", open_box, discord.ButtonStyle.danger), wiz.button("Back", back))


async def end_decide(interaction: discord.Interaction, bot, reg, rs, ev) -> None:
    """End the run now (the lock card, after the scheduled start): the decide screen, then LootMixin.end_run — the verb
    /raid end, the site, MCP and the scheduler share (its loot council is archived with its summary). Reopen stays
    possible for an hour (an end by mistake)."""
    why = rc.end_refusal(ev)
    if why:
        await interaction.response.send_message(why, ephemeral=True)
        return
    wiz = officer_wizard(bot, reg, interaction, f"End {raid_name(reg, ev)}")
    key = ev.key

    async def do(i: discord.Interaction, _note: str | None) -> None:
        cur = rs.events.get(key)
        if not cur or cur.state in ("done", "cancelled"):
            await wiz.finish(i, "That run already closed — nothing was changed.")
            return
        try:
            await bot.end_run(reg, rs, cur, by=i.user.display_name, by_id=i.user.id, via="discord")
        except RegistryError as e:
            await wiz.finish(i, str(e))
            return
        await wiz.finish(i, f"Ended {run_title(reg, cur)}. Pressed by mistake? The closed card's Reopen works for an hour.")

    told = bot.end_consequence(reg, rs, ev) if hasattr(bot, "end_consequence") else f"End {run_title(reg, ev)}."
    text = (f"{run_label(reg, ev)}\n{told}\nThe sheet and the lock card close; no more fill asks go out. Reopen works for an hour after.")
    await wiz.decide(interaction, text, do=do, do_label="End the run now", danger=True)


async def reopen_press(interaction: discord.Interaction, bot, reg, rs, ev) -> None:
    """The closed card's Reopen (a run ended by mistake, within the hour; never a cancelled run — D19)."""
    if ev is None:
        await interaction.response.send_message("That run is gone.", ephemeral=True)
        return
    why = rc.reopen_refusal(reg, ev)
    if why:
        await interaction.response.send_message(why, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        line = await bot.reopen_run(reg, rs, ev, by=interaction.user.display_name, by_id=interaction.user.id)
    except RegistryError as e:
        line = str(e)
    await interaction.followup.send(line, ephemeral=True)

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
            if not ev.seat_of(m):
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
        try:  # the check-and-set runs on the loop (claim), only the git commit in a thread — never two "first" answers
            m, _ask, line = reg.claim_placement(self.uid, self.roster, self.answer == "yes", interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"{e}", ephemeral=True)
            return
        await show_answer(card, interaction, line)
        await asyncio.to_thread(reg.save, m, line)
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


# ---------------------------------------------------------------- the loot council (dual interfaces phase 1, design §3.1)
# Every button on the council's cards and lines is a LootButton: `loot.<gk>:<session id>:<drop id | ->:<action>[:<arg>]`
# (the session id is the run key for a whole run's council, `<run>-r<N>` for one roster of a split run). A press after a
# restart reads the session from disk (bot.events is loaded at start), so nothing is lost. `arg`: the batch_rev the card
# was drawn with (Override…, Confirm the rest, Discard batch) or a hash of the recipient a Confirm showed (expect_to) — a
# stale press is refused and the card redrawn. The callbacks live in flows_loot (one implementation per action).



class LootButton(discord.ui.DynamicItem[discord.ui.Button], template=r"loot(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<sid>[A-Za-z0-9_\-]+):(?P<drop>d\d+|-):(?P<action>[a-z]{2,8})(?::(?P<arg>[A-Za-z0-9_\-]{1,24}))?"):
    """A council button. Actions — session: tick · dist · scores · end · all (Confirm the rest) · discard (Discard batch)
    · keep (Keep open); per drop: ok (Confirm) · ovr (Override…) · later (Decide later) · info (Details) · iok / iovr /
    ide (the ephemeral item card's Confirm / Override… / Disenchant, which edit that card) · why (Give the reason) ·
    whyr (replace a reason) · ovrr (replace another officer's override) · fix (Correct… / Mistake — correct…) · rm (Remove a
    candidate who left, arg = their id) · guest (record a typed name nobody knows as a new guest)."""
    LABELS = {
        "tick": ("Tick drops…", discord.ButtonStyle.secondary), "dist": ("Distribute", discord.ButtonStyle.primary),
        "scores": ("Scores only", discord.ButtonStyle.secondary), "end": ("End council", discord.ButtonStyle.danger),
        "all": ("Confirm the rest", discord.ButtonStyle.success), "discard": ("Discard batch", discord.ButtonStyle.secondary),
        "keep": ("Keep open", discord.ButtonStyle.secondary),
        "ok": ("Confirm", discord.ButtonStyle.success), "ovr": ("Override…", discord.ButtonStyle.secondary),
        "later": ("Decide later", discord.ButtonStyle.secondary), "info": ("Details", discord.ButtonStyle.secondary),
        "iok": ("Confirm", discord.ButtonStyle.success), "iovr": ("Override…", discord.ButtonStyle.secondary),
        "ide": ("Disenchant", discord.ButtonStyle.danger),
        "why": ("Give the reason", discord.ButtonStyle.primary), "whyr": ("Replace with my reason…", discord.ButtonStyle.danger),
        "fix": ("Correct…", discord.ButtonStyle.secondary), "rm": ("Remove", discord.ButtonStyle.secondary),
        "ovrr": ("Replace their override…", discord.ButtonStyle.danger),
        "guest": ("Record as a guest", discord.ButtonStyle.secondary),
    }

    def __init__(self, gk: str | None, sid: str, drop: str, action: str, *, arg: str | None = None, label: str | None = None,
                 disabled: bool = False):
        """`label` / `disabled` are render-time only (the cards are redrawn on every change)."""
        text, style = self.LABELS[action]
        cid = loot_custom_id("loot", gk, sid, drop, action, arg)
        if arg and not cid.endswith(f":{arg}"):
            arg = None  # too long for Discord's 100: the press checks without it (a fresh read decides)
        super().__init__(discord.ui.Button(label=(label or text)[:80], style=style, custom_id=cid, disabled=disabled))
        self.gk, self.sid, self.drop, self.action, self.arg = gk, sid, drop, action, arg

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["sid"], match["drop"], match["action"], arg=match["arg"])

    async def callback(self, interaction: discord.Interaction):
        from .flows_loot import loot_press

        await loot_press(interaction, self)


class LootPick(discord.ui.DynamicItem[discord.ui.Select], template=r"lootpick(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<sid>[A-Za-z0-9_\-]+):(?P<kind>[a-z]{2,8}(?:\.d\d+)?)"):
    """A council select: `owed` (the session card's Give a reason or correct… over every award still owed one —
    values `why.<drop>` / `fix.<drop>`), `remove` (Remove a candidate… who left the roster — values their id or name),
    `bind` (Bind companion… — values the companion's character),
    `whynot.<drop>` (the item card's Why not… — values candidate names)."""

    def __init__(self, gk: str | None, sid: str, kind: str, options: list[discord.SelectOption], placeholder: str = "Pick…"):
        opts = list(options)[:25] or [discord.SelectOption(label="—", value="-")]
        super().__init__(discord.ui.Select(custom_id=loot_custom_id("lootpick", gk, sid, kind, None, None), placeholder=placeholder[:150],
                                           options=opts, min_values=1, max_values=1))
        self.gk, self.sid, self.kind = gk, sid, kind

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match[str], /):
        me = cls(match["g"], match["sid"], match["kind"], list(getattr(item, "options", None) or []), getattr(item, "placeholder", None) or "Pick…")
        me.item._values = list(getattr(item, "values", None) or [])
        return me

    async def callback(self, interaction: discord.Interaction):
        from .flows_loot import loot_pick

        await loot_pick(interaction, self, list(self.item.values))


class LootStartButton(discord.ui.DynamicItem[discord.ui.Button], template=r"lootstart(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<what>open)"):
    """'Drops arriving from Jon's log but no run is live — Start an ad hoc run for tonight?': opens /raid open's wizard
    (the officer picks the raid and Ad hoc run…; nothing opens before its Confirm)."""

    def __init__(self, gk: str | None, what: str = "open"):
        super().__init__(discord.ui.Button(label="Start an ad hoc run…", style=discord.ButtonStyle.primary, custom_id=f"lootstart{_g(gk)}:{what}"))
        self.gk = gk

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["what"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg, interaction = await resolve_button(interaction, self.gk)
        if reg is None:
            if not interaction.response.is_done():
                await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        from . import guild_routing

        if await bot.role_of(reg, interaction.user, max_age=guild_routing.PRIV_LIVE) not in ("owner", "officer"):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        from .wizard import FLOWS

        await FLOWS["raid_open"](interaction, reg)


def loot_custom_id(prefix: str, gk: str | None, sid: str, part: str, action: str | None, arg: str | None) -> str:
    """`<prefix>.<gk>:<sid>:<part>[:<action>][:<arg>]`, the optional arg dropped when the id would pass Discord's 100."""
    base = f"{prefix}{_g(gk)}:{sid}:{part}" + (f":{action}" if action else "")
    full = base + (f":{arg}" if arg else "")
    if len(full) <= 100:
        return full
    if len(base) > 100:
        log.warning("loot custom id over 100 characters: %s", base)
    return base[:100]


# text helpers the callbacks format with; imported last so `raid_views` (which places these buttons and imports them at
# its own bottom) always finds the classes above bound, whichever module is imported first
from .raid_views import SUGGEST_LINES, lock_preview_embed, raid_name, run_label, run_title, sheet_state  # noqa: E402
