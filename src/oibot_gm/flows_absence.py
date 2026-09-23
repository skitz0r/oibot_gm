"""Absence wizards: `/me absent add`, the absences card's *I'll be away*, and `/roster absent` (an officer, on someone's
behalf). One modal — From (a day) · For how long · Reason — then a confirm screen that says which live sheets it
touches. No date is typed; an end before the start or past the 120-day cap cannot be picked."""
from __future__ import annotations

from datetime import date, timedelta

import discord

from .registry import Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Wizard, day_opts, duration_opts, flow, span_end, week_opts


class AbsenceWizard(Wizard):
    def __init__(self, reg: Registry, rs, owner_id: int, member_id: int, member_name: str, *, officer: bool):
        async def still_officer(i: discord.Interaction) -> str | None:
            from .discord_registry import is_officer

            return None if is_officer(i, reg) else "Only officers can record an absence for someone else."

        title = f"Away · {member_name}" if officer else "I'll be away"
        super().__init__(reg, owner_id, title=title, guard=still_officer if officer else None)
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
        start, days = self.draft["start"], self.draft["days"]
        end = span_end(start, days)
        span = self.reg.span_label(start.isoformat(), end.isoformat())
        lines = [f"**{self.member_name if self.officer else 'You'}** away **{span}** ({days} day{'s' if days != 1 else ''})."]
        if self.draft["reason"]:
            lines.append(f"-# Reason (officers only): {self.draft['reason']}")
        effects = self.effects(start, end)
        lines += effects or ["-# No live sheet falls in those days."]
        await self.show(interaction, "\n".join(lines),
                        self.button("Confirm", self.save, discord.ButtonStyle.success),
                        self.button("Change", self.reopen),
                        self.button("Cancel", self.cancel))

    def effects(self, start: date, end: date) -> list[str]:
        """Which live sheets the absence reaches: an open sheet pre-fills No thanks; a locked seat is released."""
        if not self.rs:
            return []
        from .raid_views import raid_name, sheet_state

        m = self.reg.members.get(self.member_id)
        out = []
        for ev in self.rs.live():
            day = ev.start.astimezone(self.reg.tz).date()
            if not (start <= day <= end):
                continue
            label = f"{raid_name(self.reg, ev)} {self.reg.local12(ev.start)}"
            if sheet_state(ev) == "locked" and m and ev.seat_of(m.display_name):
                out.append(f"• {label}: the seat is released and the bench is asked")
            elif sheet_state(ev) == "open":
                out.append(f"• {label}: marked No thanks")
        return out

    async def save(self, interaction: discord.Interaction) -> None:
        start, days = self.draft["start"], self.draft["days"]
        end = span_end(start, days)
        await self.working(interaction, "Saving…")
        bot, by = interaction.client, interaction.user.display_name
        try:
            m, a = self.reg.add_absence(self.member_id, start.isoformat(), end.isoformat(), self.draft["reason"] or None, by,
                                        display_name=self.member_name if self.officer else by)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen, "Change the dates")
            return
        await bot.announce_absence(self.reg, m, a, by)
        who = self.member_name if self.officer else "You're"
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


__all__ = ["AbsenceWizard", "open_absence"]
