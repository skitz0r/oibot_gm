"""The lineup block (phase 5): a lineup drafted on one Discord screen, mixed into a Wizard — /raid lineup
(flows_event.EditLineupWizard), a one-off's lineup in /raid open (flows_raid.OpenWizard) and the schedule editor reuse
it. It knows nothing of events or schedules and imports no flow module (flows_raid / flows_event import it), so any
wizard can mix it in at class definition. flows_event re-exports LineupBlock and REVIEW."""
from __future__ import annotations

import discord

from . import raidcycle as rc
from .registry import LINEUP_MAX, LINEUP_MAX_WORDS, MAX_SCHEDULE_ROSTERS
from .wizard import MAX_OPTIONS, Opt

REVIEW = "Review changes"
STEP = 15  # a later raid's start is picked in 15-minute steps
START_CHOICES = 24  # after the previous raid's start: 15 minutes to 6 hours
EVERY_RAID_IN = "Every raid of the game is in this lineup."


class LineupBlock:
    """A lineup drafted on one screen, mixed into a Wizard: `self.lineup` = [{raid, rosters[, starts]}], entry 0 the
    first raid (it never changes — rc.LINEUP_FIRST), a later entry's `starts` in minutes after the first (absent = the
    Registry.lineup_offsets default: when the raid before it ends). It knows nothing of events: the host gives
    `lineup_start()` (the first raid's start for clock words; None for a schedule), `lineup_done(i)` (Review) and, when
    the hub's Back should return to the host's previous screen, `lineup_back(i)` (default: the wizard's cancel)."""

    lineup: list[dict]
    lineup_page: int = 0

    def lineup_start(self):
        return None

    async def lineup_done(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    async def lineup_back(self, interaction: discord.Interaction) -> None:
        await self.cancel(interaction)

    # ---- words
    def _raid_name(self, rid: str) -> str:
        return self.reg.raid_def(rid).get("name", rid)

    def _offsets(self) -> list[int]:
        return self.reg.lineup_offsets(self.lineup)

    def _tidy(self) -> None:
        """A later start no longer after the one before it goes back to its default (lineup_offsets never refuses)."""
        prev = 0
        for i, e in enumerate(self.lineup):
            if i == 0:
                e.pop("starts", None)
                continue
            if e.get("starts") is not None and int(e["starts"]) <= prev:
                e.pop("starts")
            prev = self.reg.lineup_offsets(self.lineup[: i + 1])[i]

    def _at_words(self, minutes: int) -> str:
        start = self.lineup_start()
        if start is None:
            h, m = divmod(int(minutes), 60)
            return " ".join(x for x in (f"{h} h" if h else "", f"{m} min" if m else "") if x) + " after the first"
        t = rc.after_minutes(start, minutes)
        same = t.astimezone(self.reg.tz).date() == start.astimezone(self.reg.tz).date()
        return self.reg.local12(t, "%I:%M %p") if same else self.reg.local12(t)

    def _step_line(self, n: int, offsets: list[int]) -> str:
        e = self.lineup[n]
        k = int(e.get("rosters") or 1)
        if n == 0:
            start = self.lineup_start()
            when = self.reg.local12(start, "%I:%M %p") if start is not None else "at the schedule's time"
        else:
            when = self._at_words(offsets[n])
        return f"{n + 1}. {self._raid_name(e['raid'])} · {k} roster{'s' if k != 1 else ''} · {when}"

    # ---- the hub
    async def lineup_hub(self, interaction: discord.Interaction, note: str = "") -> None:
        self._tidy()
        offsets = self._offsets()
        lines = [self._step_line(n, offsets) for n in range(len(self.lineup))]
        full = len(self.lineup) >= LINEUP_MAX
        if full:
            lines.append(LINEUP_MAX_WORDS)
        steps = [Opt(str(n + 1), f"{n + 1}. {self._raid_name(e['raid'])}") for n, e in enumerate(self.lineup)]
        taken = {e["raid"] for e in self.lineup}
        free = [Opt(rid, self._raid_name(rid)) for rid in self.reg.profile.raids if rid not in taken]
        if not full and not free:
            lines.append(EVERY_RAID_IN)

        async def goto(i: discord.Interaction, page: int) -> None:
            self.lineup_page = page
            await self.lineup_hub(i)

        from .flows_config import OwnerWizard

        add, page_note = OwnerWizard.paged(self, "Add a raid", free, self._lineup_add, self.lineup_page, goto)
        if full:
            add[0].disabled = True
        text = "\n".join(([note] if note else []) + lines + ([page_note] if page_note else []))
        await self.show(interaction, text, self.select("Pick a step", steps, self._lineup_pick), *add,
                        self.button(REVIEW, self.lineup_done, discord.ButtonStyle.primary), self.button("Back", self.lineup_back))

    async def _lineup_add(self, interaction: discord.Interaction, values: list[str]) -> None:
        rid = values[0]
        if len(self.lineup) < LINEUP_MAX and rid in self.reg.profile.raids and rid not in {e["raid"] for e in self.lineup}:
            self.lineup.append({"raid": rid, "rosters": 1})
        await self.lineup_hub(interaction)

    async def _lineup_pick(self, interaction: discord.Interaction, values: list[str]) -> None:
        n = int(values[0]) - 1
        if not 0 <= n < len(self.lineup):
            await self.lineup_hub(interaction)
            return
        await self.lineup_step(interaction, n)

    # ---- one step
    async def lineup_step(self, interaction: discord.Interaction, n: int) -> None:
        self._tidy()
        offsets = self._offsets()
        e = self.lineup[n]
        k = int(e.get("rosters") or 1)
        rosters = [Opt(str(r), f"{r} roster{'s' if r != 1 else ''}", default=r == k) for r in range(1, MAX_SCHEDULE_ROSTERS + 1)]

        async def set_rosters(i: discord.Interaction, vals: list[str]) -> None:
            self.lineup[n]["rosters"] = int(vals[0])
            await self.lineup_step(i, n)

        items: list[discord.ui.Item] = [self.select("Rosters", rosters, set_rosters)]
        if n >= 1:
            items.append(self.select("Starts", self._start_opts(n, offsets), self._set_start(n)))
            if n >= 2:
                items.append(self.button("Move earlier", lambda i: self._lineup_move(i, n, n - 1)))
            if n < len(self.lineup) - 1:
                items.append(self.button("Move later", lambda i: self._lineup_move(i, n, n + 1)))
            items.append(self.button("Remove", lambda i: self._lineup_remove(i, n)))
        items.append(self.button("Back", self.lineup_hub))
        await self.show(interaction, self._step_line(n, offsets), *items)

    def _start_opts(self, n: int, offsets: list[int]) -> list[Opt]:
        """The default first ("When <previous raid> ends · 9:15 PM"), then 24 starts in 15-minute steps from the
        previous raid's start plus 15 minutes. Values are minutes after the first raid; "end" = the default. A start
        already set that is not among them (off the 15-minute grid, or later) is added in its place, so it shows picked."""
        e = self.lineup[n]
        default = self.reg.lineup_offsets([*self.lineup[:n], {k: v for k, v in e.items() if k != "starts"}])[n]
        prev = offsets[n - 1]
        out = [Opt("end", f"When {self._raid_name(self.lineup[n - 1]['raid'])} ends · {self._at_words(default)}",
                   default=e.get("starts") is None)]
        steps = [prev + STEP * s for s in range(1, START_CHOICES + 1)][: MAX_OPTIONS - 1]
        own = int(e["starts"]) if e.get("starts") is not None else None
        if own is not None and own not in steps:
            steps = sorted([*steps[:-1], own])  # the last step gives way, still ≤ 25 options
        out += [Opt(str(at), self._at_words(at), default=at == own) for at in steps]
        return out

    def _set_start(self, n: int):
        async def picked(i: discord.Interaction, vals: list[str]) -> None:
            was = self._offsets()[n]
            if vals[0] == "end":
                self.lineup[n].pop("starts", None)
            else:
                self.lineup[n]["starts"] = int(vals[0])
            delta = self._offsets()[n] - was
            for e in self.lineup[n + 1:]:  # a later raid set to its own start keeps its gap (LineupBuilder setStart)
                if e.get("starts") is not None:
                    e["starts"] = int(e["starts"]) + delta
            await self.lineup_step(i, n)
        return picked

    async def _lineup_move(self, interaction: discord.Interaction, a: int, b: int) -> None:
        if min(a, b) >= 1 and max(a, b) < len(self.lineup):
            self.lineup[a], self.lineup[b] = self.lineup[b], self.lineup[a]
            for e in self.lineup[min(a, b):]:  # each moved or later raid takes its default start again
                e.pop("starts", None)
        await self.lineup_hub(interaction)

    async def _lineup_remove(self, interaction: discord.Interaction, n: int) -> None:
        if 1 <= n < len(self.lineup):
            del self.lineup[n]
            for e in self.lineup[n:]:
                e.pop("starts", None)
        self.lineup_page = 0
        await self.lineup_hub(interaction)
