"""Persistent buttons for the cycle (DynamicItems with a `custom_id` template, so they survive restarts):
SignupButton (Join / Bench / No thanks, Can't make it once locked, with the character picker for members with several
characters), FillButton (Confirm / Can't make it on a fill DM, same pair as the confirmation DM), PlaceButton (Confirm / Can't make it on a confirmation DM),
RunButton (officer Fill seats / Lock now / Cancel run on the health and lock cards), and the plain-View factories
the DMs use (`place_view`, `fill_view`, `sheet_view`). Callbacks call the `RaidMixin` methods on `interaction.client`.

Import cycle: see `raid_views` — the helpers this module formats replies with are imported at the bottom."""
from __future__ import annotations

import asyncio
import re

import discord

from . import raidcycle as rc
from .registry import RegistryError


class RunButton(discord.ui.DynamicItem[discord.ui.Button], template=r"runact:(?P<key>[A-Za-z0-9_\-]+):(?P<action>fill|lock|cancel)"):
    """Officer buttons on the health and lock cards: Fill seats (with a preview + confirm), Lock now, Cancel run."""
    LABELS = {"fill": ("Fill seats", discord.ButtonStyle.secondary), "lock": ("Lock now", discord.ButtonStyle.secondary), "cancel": ("Cancel run", discord.ButtonStyle.danger)}

    def __init__(self, key: str, action: str, disabled: bool = False, label: str | None = None):
        """`disabled`/`label` are render-time only (the card is re-rendered on every state change): a button that does
        not apply yet stays in its place, greyed, instead of disappearing."""
        text, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label or text, style=style, custom_id=f"runact:{key}:{action}", disabled=disabled))
        self.key, self.action = key, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg or not await bot.is_officer_anywhere(reg, interaction.user.id):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if not ev or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That run is closed.", ephemeral=True)
            return
        team = rc.run_team(reg, ev)
        by = interaction.user.display_name
        if self.action == "fill":
            if sheet_state(ev) != "locked" or not ev.all_rosters:
                await interaction.response.send_message("Fill works after lock — the sheet is still open.", ephemeral=True)
                return
            nd = rc.needs(reg, ev, team)
            if not nd["headcount"] and not nd["roles"]:
                await interaction.response.send_message("Nothing to fill — every seat is taken.", ephemeral=True)
                return
            batch = await asyncio.to_thread(rc.fill_batch, reg, rs, ev, team)
            outstanding = [a for a in ev.fill_asks if a.open]
            lines = [f"**Short** {gaps_text(bot.ico, nd)}"]
            if outstanding:
                lines.append(f"**Already asked, waiting:** {', '.join(a.display_name for a in outstanding)}")
            if batch:
                lines.append("**Will DM now:**\n" + "\n".join("• " + ask_line(reg, bot.ico, a) + (" — tied to " + next((b.display_name for b in batch if b.discord_id == a.pair), "?") if a.pair else "") for a in batch))
                hrs = reg.raid_def(ev.instance).get("fill_ask_hours")
                lines.append(f"-# Each gets Confirm / Can't make it. A yes takes the seat at once; a no asks the next person; no answer within {hrs:g} h counts as no. Tied asks go out together and a no from either withdraws the other. Never more than 3 questions out at a time. Answers post in this run's thread.")
            else:
                lines.append("**Nobody to ask right now** — either the 3 outstanding questions already cover it, or everyone eligible has been asked.")
            view = discord.ui.View(timeout=120)
            go = discord.ui.Button(label=f"Send {len(batch)} ask{'s' if len(batch) != 1 else ''}", style=discord.ButtonStyle.success, disabled=not batch)
            no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

            async def send(i: discord.Interaction):
                await i.response.edit_message(content="Sending…", view=None)
                sent, nd2 = await bot.run_fill(reg, rs, ev, team, by=by)
                await i.edit_original_response(content=("Asked " + ", ".join(a.display_name for a in sent)) if sent else "Nobody could be asked.")
                if sent:
                    await bot.post_run_update(reg, ev, "🧩 " + "\n🧩 ".join(ask_line(reg, bot.ico, a) for a in sent))

            async def cancel(i: discord.Interaction):
                await i.response.edit_message(content="Cancelled — nothing sent.", view=None)

            go.callback, no.callback = send, cancel
            view.add_item(go); view.add_item(no)
            await interaction.response.send_message("\n".join(lines), view=view, ephemeral=True)
            return
        if self.action == "lock":
            if ev.state != "open":
                await interaction.response.send_message("Already locked.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                line = await bot.lock_run(reg, rs, ev, by=by)
            except Exception as e:  # noqa: BLE001
                line = f"lock failed: {e}"
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

class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid:(?P<key>[A-Za-z0-9_\-]+):(?P<status>in|tentative|out|sub|cant)"):
    """The sheet's member buttons: Join / Bench / No thanks while open; a single Can't make it once locked, which
    releases a rostered member's seat (anyone else: nothing to do)."""
    STYLES = {"in": discord.ButtonStyle.success, "sub": discord.ButtonStyle.primary, "out": discord.ButtonStyle.secondary, "cant": discord.ButtonStyle.secondary}  # grey like the same choice in a DM; red is for destructive officer actions

    def __init__(self, key: str, status: str):
        status = "in" if status == "tentative" else status
        super().__init__(discord.ui.Button(label="Can't make it" if status == "cant" else rc.LABELS[status], style=self.STYLES[status], custom_id=f"raid:{key}:{status}"))
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
            await interaction.response.send_message("The roster is locked — answer your confirmation DM, or press Can't make it on the sheet.", ephemeral=True)
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


class FillButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fill:(?P<key>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Yes/No on a fill DM. Survives restarts; only the person asked can answer."""

    def __init__(self, key: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Confirm" if answer == "yes" else "Can't make it", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"fill:{key}:{uid}:{answer}"))
        self.key, self.uid, self.answer = key, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["key"], int(match["uid"]), match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
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
            await interaction.response.send_message("Already answered (or the gap was filled).", ephemeral=True)
            return
        line = rc.apply_fill_answer(reg, rs, ev, ask, self.answer == "yes")
        try:
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(line, ephemeral=True)
        await bot.after_fill_answer(reg, rs, ev, ask, line)


class PlaceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"place:(?P<roster>[A-Za-z0-9_\-]+):(?P<uid>\d+):(?P<answer>yes|no)"):
    """Confirm / can't make it on a locked roster (DM after lock). Persistent; only the person asked can answer."""

    def __init__(self, roster: str, uid: int, answer: str):
        super().__init__(discord.ui.Button(label="Confirm" if answer == "yes" else "Can't make it", style=discord.ButtonStyle.success if answer == "yes" else discord.ButtonStyle.secondary, custom_id=f"place:{roster}:{uid}:{answer}"))
        self.roster, self.uid, self.answer = roster, uid, answer

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["roster"], int(match["uid"]), match["answer"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if not await bot.may_answer_for(interaction, reg, self.uid):
            await interaction.response.send_message("That question was for someone else.", ephemeral=True)
            return
        try:
            line = reg.answer_placement(self.uid, self.roster, self.answer == "yes", interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"{e}", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(content=interaction.message.content + f"\n\n**→ {line}**", view=None)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(line, ephemeral=True)
        await bot.after_placement_answer(reg, self.uid, self.roster, self.answer == "yes", line)


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
    for s in rc.STATUSES:
        v.add_item(SignupButton(key, s))
    return v


# text helpers the callbacks format with; imported last so `raid_views` (which places these buttons and imports them at
# its own bottom) always finds the classes above bound, whichever module is imported first
from .raid_views import ask_line, gaps_text, run_label, sheet_state  # noqa: E402
