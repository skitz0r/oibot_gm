"""Absence wizards: `/me absent add`, the absences card's *I'll be away*, and `/roster absent` (an officer, on someone's
behalf). One modal — From (a day) · For how long (presets.DURATIONS, the list the web modals share) · Reason — then a
confirm screen that previews which live sheets it touches (rc.absence_effects, the words the web preview and plain text
print). No date is typed; an end before the start or past the 120-day cap cannot be picked; an overlap with an absence
already on file and a reason past the cap are refused at the verb (Registry.add_absence) and shown on the preview.

And "I'm back" (`absence_clear`): `/me absent clear`, the card's *I'm back*, a Clear on *My absences* and `/roster back`
(an officer, for someone): pick the absence → THE decide screen with what re-opens → discord_people.im_back."""
from __future__ import annotations

from datetime import date, timedelta

import discord

from .presets import duration_words
from .registry import Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, day_opts, duration_opts, flow, span_end, week_opts


def _officer_guard(reg: Registry, what: str):
    async def still_officer(i: discord.Interaction) -> str | None:
        from .discord_registry import is_officer

        return None if is_officer(i, reg) else f"Only officers can {what} for someone else."
    return still_officer


class AbsenceWizard(Wizard):
    def __init__(self, reg: Registry, rs, owner_id: int, member_id: int, member_name: str, *, officer: bool):
        title = f"Away · {member_name}" if officer else "I'll be away"
        super().__init__(reg, owner_id, title=title, guard=_officer_guard(reg, "record an absence") if officer else None)
        self.rs, self.member_id, self.member_name, self.officer = rs, member_id, member_name, officer
        today = reg.now_local().date()
        self.draft = {"day0": today, "start": today, "days": 1, "reason": ""}

    # ---- the form: From · For how long · Reason
    def form(self) -> Form:
        d = self.draft
        who = "their" if self.officer else "your"
        return Form(self.title, [
            Field("start", "From", day_opts(self.reg, d["day0"], default=d["start"], note=self._day_note, later=True),
                  description=f"the first day {who} away"),
            Field("days", "For how long", duration_opts(default=d["days"]), description="counted from the first day"),
            Field("reason", "Reason (officers only, optional)", required=False, paragraph=True, default=d["reason"] or None, max_length=200,
                  placeholder="only officers see this"),
        ], self.submitted)

    def _day_note(self, day: date) -> str | None:
        """What a day means here — a run that night — so the list is more than dates."""
        hits = [ev for ev in (self.rs.live() if self.rs else []) if ev.start.astimezone(self.reg.tz).date() == day]
        if not hits:
            return None
        from .raid_views import raid_name

        return ", ".join(f"{raid_name(self.reg, ev)} {self.reg.local12(ev.start, '%I:%M %p')}" for ev in hits)[:100]

    async def start(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.form())

    async def reopen(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.form())

    # ---- after the form
    async def submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["days"] = int(v.get("days") or 1)
        self.draft["reason"] = v.get("reason", "")
        if v.get("start") == "later":  # past the window: pick a week, then the form reopens on it
            await self.show(interaction, "Which week does it start?",
                            self.select("Pick the week", week_opts(self.reg, self.draft["day0"] + timedelta(days=MAX_OPTIONS - 1)), self.week_picked),
                            self.button("Cancel", self.cancel))
            return
        self.draft["start"] = date.fromisoformat(v["start"])
        await self.confirm(interaction)

    async def week_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        monday = date.fromisoformat(values[0])
        self.draft["day0"], self.draft["start"] = monday, monday
        await self.open_form(interaction, self.form())  # a select can open a modal: straight back to the form

    async def confirm(self, interaction: discord.Interaction) -> None:
        """The preview: the span in words, what it does to each live sheet — or the verb's refusal (an overlap, the
        reason cap) before anything is pressed."""
        start, days = self.draft["start"], self.draft["days"]
        end = span_end(start, days)
        try:
            self.reg.check_absence(self.reg.members.get(self.member_id), start.isoformat(), end.isoformat(), self.draft["reason"] or None)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen, "Change the dates")
            return
        span = self.reg.span_label(start.isoformat(), end.isoformat())
        lines = [f"**{self.member_name if self.officer else 'You'}** away **{span}** ({duration_words(days)})."]
        if self.draft["reason"]:
            lines.append(f"-# Reason (officers only): {self.draft['reason']}")
        effects = self.effects(start, end)
        lines += [f"• {e}" for e in effects] or ["-# No live sheet falls in those days."]
        await self.show(interaction, "\n".join(lines),
                        self.button("Confirm", self.save, discord.ButtonStyle.success),
                        self.button("Change", self.reopen),
                        self.button("Cancel", self.cancel))

    def effects(self, start: date, end: date) -> list[str]:
        """Which live sheets the absence reaches (rc.absence_effects — the same words as the web and plain text)."""
        from .discord_people import absence_lines

        return absence_lines(self.reg, self.rs, self.reg.members.get(self.member_id), start.isoformat(), end.isoformat())

    async def save(self, interaction: discord.Interaction) -> None:
        from . import discord_people

        start, days = self.draft["start"], self.draft["days"]
        end = span_end(start, days)
        await self.working(interaction, "Saving…")
        by = interaction.user.display_name
        try:
            m, a, _touched = await discord_people.record_absence(interaction.client, self.reg, self.member_id, start.isoformat(), end.isoformat(),
                                                                 self.draft["reason"] or None, by, by_id=interaction.user.id,
                                                                 display_name=self.member_name if self.officer else by)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen, "Change the dates")
            return
        who = f"{self.member_name} is" if self.officer else "You're"
        await self.finish(interaction, f"✅ {who} away **{self.reg.span_label(a.start, a.end)}**. Sheets on those days are marked No thanks, and a rostered seat is handed back.")


@flow("absence")
async def open_absence(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User | None = None) -> None:
    """Your own absence, or — with `member` — an officer recording one for someone else."""
    rs = interaction.client.raids.store(reg) if hasattr(interaction.client, "raids") else None
    if member is None or member.id == interaction.user.id:
        wiz = AbsenceWizard(reg, rs, interaction.user.id, interaction.user.id, interaction.user.display_name, officer=False)
    else:
        wiz = AbsenceWizard(reg, rs, interaction.user.id, member.id, member.display_name, officer=True)
    await wiz.start(interaction)


# ---------------------------------------------------------------- I'm back

class AbsenceClearWizard(Wizard):
    """Pick one of the member's upcoming absences (skipped when there is one, or when a Clear button named it), then THE
    decide screen: the span, what re-opens (discord_people.absence_clear_lines), [I'm back] [Back]."""

    def __init__(self, reg: Registry, rs, owner_id: int, member_id: int, member_name: str, *, officer: bool):
        super().__init__(reg, owner_id, title=f"Back · {member_name}" if officer else "I'm back", guard=_officer_guard(reg, "clear an absence") if officer else None)
        self.rs, self.member_id, self.member_name, self.officer = rs, member_id, member_name, officer

    def upcoming(self) -> list:
        m = self.reg.members.get(self.member_id)
        return m.upcoming_absences(self.reg.now_local().date().isoformat()) if m else []

    async def start(self, interaction: discord.Interaction, start: str | None = None) -> None:
        ups = self.upcoming()
        if not ups:
            who = f"{self.member_name} has" if self.officer else "You have"
            await self.finish(interaction, f"{who} no upcoming absence — nothing to clear.")
            return
        hit = next((a for a in ups if a.start == start), None) if start else (ups[0] if len(ups) == 1 else None)
        if start and hit is None:
            await self.finish(interaction, "That absence is no longer on file (cleared meanwhile) — nothing to do.")
            return
        if hit is not None:
            await self.ask(interaction, hit.start)
            return
        await self.pick(interaction)

    async def pick(self, interaction: discord.Interaction) -> None:
        opts = [Opt(a.start, self.reg.span_label(a.start, a.end)[:100], (a.reason or "")[:100] or None) for a in self.upcoming()]
        who = f"Which of {self.member_name}'s absences" if self.officer else "Which absence"
        await self.show(interaction, f"{who} is over?", self.select("Pick the absence", opts, self.picked), self.button("Cancel", self.cancel))

    async def picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        await self.ask(interaction, values[0])

    async def ask(self, interaction: discord.Interaction, start: str) -> None:
        from .discord_people import absence_clear_lines

        m = self.reg.members.get(self.member_id)
        a = next((x for x in (m.absences if m else []) if x.start == start), None)
        if a is None:
            await self.finish(interaction, "That absence is no longer on file (cleared meanwhile) — nothing to do.")
            return
        self.draft["start"] = a.start
        whose = f"**{self.member_name}**'s" if self.officer else "Your"
        lines = [f"{whose} absence **{self.reg.span_label(a.start, a.end)}** is cleared."]
        if a.reason and self.officer:
            lines.append(f"-# Reason, for officers only: {a.reason}")
        effects = absence_clear_lines(self.reg, self.rs, m, a)
        lines += [f"• {e}" for e in effects] or ["-# No sheet changes, because no live sheet was answered for the absence."]
        await self.decide(interaction, "\n".join(lines), do=self.save, do_label="I'm back")

    async def save(self, interaction: discord.Interaction, _note: str | None) -> None:
        from . import discord_people

        try:
            a, lines = await discord_people.im_back(interaction.client, self.reg, self.member_id, self.draft["start"], interaction.user.display_name,
                                                    by_id=interaction.user.id)
        except RegistryError as e:
            await self.finish(interaction, f"❌ {e}")
            return
        who = f"{self.member_name}'s" if self.officer else "Your"
        await self.finish(interaction, f"✅ {who} absence {self.reg.span_label(a.start, a.end)} is cleared." + "".join(f"\n• {line}" for line in lines))


@flow("absence_clear")
async def open_absence_clear(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User | None = None, start: str | None = None) -> None:
    """I'm back: your own absence, or — with `member` (`/roster back`) — an officer clearing someone else's."""
    rs = interaction.client.raids.store(reg) if hasattr(interaction.client, "raids") else None
    if member is None or member.id == interaction.user.id:
        wiz = AbsenceClearWizard(reg, rs, interaction.user.id, interaction.user.id, interaction.user.display_name, officer=False)
    else:
        wiz = AbsenceClearWizard(reg, rs, interaction.user.id, member.id, member.display_name, officer=True)
    await wiz.start(interaction, start)


__all__ = ["AbsenceWizard", "AbsenceClearWizard", "open_absence", "open_absence_clear"]
