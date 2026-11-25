"""/raid reschedule and /raid edit (phase 5, Task 3): one event moved to another day and time, or its own settings
changed, on the server's box 3 (consequences.reschedule_pick_box / reschedule_box, `edit` for Edit event) — the same
box the Events page prints. The day and time are picked from selects (15-minute steps) and resolved by rc.move_start,
which refuses a wall time the clocks skip or pass twice. Both write through RaidMixin.reschedule_event with the
event's rev as the box showed it; nobody is DMed (the verb's notices and card edits are the outcome)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta

import discord

from . import consequences as cq
from . import raidcycle as rc
from .constants import PAGE_WORDS, schedule_line_label
from .flows_raid import officer_guard, officer_only
from .raid_views import run_title
from .registry import LINEUP_MAX, LINEUP_MAX_WORDS, MAX_SCHEDULE_ROSTERS, Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, box_text, flow, week_opts, when_fields

NO_EVENT = "No event is open. Open one with /raid open."
PICK_WHEN = "Pick the day and time"
PICK_AGAIN = "Pick another time"
CHANGE = "Change a setting"
REVIEW = "Review changes"
HOURS = ("lock_hours_before", "nudge_hours_before", "confirm_hours_before", "fill_ask_hours")
SWITCHES = ("nudge", "confirm", "autofill")


async def pick_event(interaction: discord.Interaction, reg: Registry, *, title: str,
                     then: Callable[[discord.Interaction, Wizard, rc.RaidStore, rc.RaidEvent | None], Awaitable[None]],
                     states: tuple[str, ...] = ("open", "locked"), wizard: type[Wizard] = Wizard) -> None:
    """The event a command is about: live FIRST raids only (a later raid stands for its event), soonest first, ≤ 25.
    One → straight to `then`; several → one select (run_title as the label, the key as the value only); none →
    NO_EVENT."""
    rs = interaction.client.raids.store(reg)
    evs = sorted((ev for ev in rs.live() if ev.state in states and rc.first_part(rs, ev) is ev), key=lambda e: e.start)[:MAX_OPTIONS]
    wiz = wizard(reg, interaction.user.id, title=title, guard=officer_guard(reg))
    if not evs:
        await wiz.finish(interaction, NO_EVENT)
        return
    if len(evs) == 1:
        await then(interaction, wiz, rs, evs[0])
        return

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        await then(i, wiz, rs, rs.events.get(values[0]))  # re-read: it may have closed since the list was drawn

    await wiz.show(interaction, "Which event?", wiz.select("Pick the event", [Opt(ev.key, run_title(reg, ev)) for ev in evs], picked),
                   wiz.button("Back", wiz.cancel))


def adopt(picker: Wizard, wiz: "EventWizard") -> "EventWizard":
    """`wiz` takes over the picker's message (an event picked from the select advances the same screen)."""
    wiz.message_id = picker.message_id
    picker.stop()
    return wiz


class EventWizard(Wizard):
    """A wizard about one event (its first raid's key): re-read at every screen, officers re-checked at every press."""

    def __init__(self, reg: Registry, rs: rc.RaidStore, owner_id: int, key: str, *, title: str):
        super().__init__(reg, owner_id, title=title, guard=officer_guard(reg))
        self.rs, self.key = rs, key

    def ev(self) -> rc.RaidEvent | None:
        return self.rs.events.get(self.key)

    async def plain(self, interaction: discord.Interaction, text: str, *items: discord.ui.Item) -> None:
        """A screen that is a box's text: its bold title, never the wizard's too."""
        heading, self.heading = self.heading, False
        try:
            await self.show(interaction, text, *items)
        finally:
            self.heading = heading

    async def act(self, i: discord.Interaction, coro_fn: Callable[[], Awaitable[list[str]]]) -> None:
        """The write: `working` (unless the box already said it), then the verb's line; its refusal is its sentence."""
        if not i.response.is_done():
            await self.working(i)
        try:
            lines = await coro_fn()
        except RegistryError as e:
            await self.finish(i, str(e))
            return
        await self.finish(i, "✅ " + " ".join(lines))


# ---------------------------------------------------------------- /raid reschedule

class RescheduleWizard(EventWizard):
    """Box 3 asking for the time → day · hour · minutes (Later than this… → the week) → rc.move_start (a skipped or
    doubled wall time is the refusal box, Back alone) → box 3 with Move · Pick another time · Back."""

    rev: str | None = None
    day0: date | None = None

    async def start(self, interaction: discord.Interaction) -> None:
        ev = self.ev()
        if ev is None:
            await self.finish(interaction, NO_EVENT)
            return
        self.day0 = None
        await self.plain(interaction, box_text(cq.reschedule_pick_box(self.reg, self.rs, ev)),
                         self.button(PICK_WHEN, self.pick, discord.ButtonStyle.primary), self.button("Back", self.cancel))

    async def pick(self, interaction: discord.Interaction) -> None:
        ev = self.ev()
        if ev is None:
            await self.finish(interaction, NO_EVENT)
            return
        day0 = self.day0 or self.reg.now_local().date()
        await self.open_form(interaction, Form(PICK_WHEN, when_fields(self.reg, day0=day0, default=ev.start, step=15), self.picked))

    async def picked(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        if v.get("day") == "later":
            day0 = self.day0 or self.reg.now_local().date()
            await self.show(interaction, "Which week?",
                            self.select("Pick the week", week_opts(self.reg, day0 + timedelta(days=MAX_OPTIONS - 1)), self.week_picked),
                            self.button("Back", self.start))
            return
        try:
            start = rc.move_start(self.reg, f"{v['day']} {int(v.get('hour') or 0):02d}:{int(v.get('minute') or 0):02d}")
        except RegistryError as e:
            await self.decide_box(interaction, cq.refusal_box_for(str(e)), do=self._nothing, back=self.start)
            return
        await self.decide_box(interaction, self.box(start), do=lambda i, _v: self.move(i, start), refresh=lambda: self.box(start),
                              extra=[self.button(PICK_AGAIN, self.pick)], back=self.start)

    async def week_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.day0 = date.fromisoformat(values[0])
        await self.pick(interaction)

    def box(self, start) -> dict:
        """Box 3 for `start`, and the rev it was built on (what the Move press carries)."""
        ev = self.ev()
        if ev is None:
            raise RegistryError(rc.MOVE_CLOSED)
        self.rev = rc.event_rev(self.reg, self.rs, ev)
        return cq.reschedule_box(self.reg, self.rs, ev, start)

    async def move(self, i: discord.Interaction, start) -> None:
        ev, rev = self.ev(), self.rev
        await self.act(i, lambda: i.client.reschedule_event(self.reg, self.rs, ev, start=start, by=i.user.display_name, by_id=i.user.id, rev=rev))

    async def _nothing(self, i: discord.Interaction, _v: dict) -> None:  # a refusal box has no verb: never pressed
        await self.finish(i, "Nothing was changed.")


@flow("raid_reschedule")
async def open_raid_reschedule(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def then(i: discord.Interaction, _wiz: Wizard, rs: rc.RaidStore, ev: rc.RaidEvent | None) -> None:
        if ev is None:  # it closed while the list was open
            await _wiz.finish(i, NO_EVENT)
            return
        await adopt(_wiz, RescheduleWizard(reg, rs, i.user.id, ev.key, title="Move an event")).start(i)

    await pick_event(interaction, reg, title="Move an event", then=then)


# ---------------------------------------------------------------- /raid edit

def setting_label(field: str) -> str:
    """A setting's name as the schedule editor and the Edit box's lines say it."""
    return schedule_line_label(field) or field


def trim_hours(choices: list[dict], keep) -> list[dict]:
    """flows_config.target_numbers' rule on the hour choices: ≤ 25, cut from the high end, never the value held now."""
    out = list(choices)
    while len(out) > MAX_OPTIONS:
        cut = next(c for c in reversed(out) if keep is None or float(c["value"]) != float(keep))
        out.remove(cut)
    return out


def _num(v: str):
    f = float(v)
    return int(f) if f.is_integer() else f


class EditEventWizard(EventWizard):
    """Edit event (the owner's tweaks, B) as a hub: a line per setting allowed now (old → new once drafted), Change a
    setting → one screen per setting, Review changes → box 3 with `edit` (Save). The draft `timing` holds only what
    differs from the event now: EVENT_TIMING's keys, the weights whole, the split as {raid key: strategy}."""

    def __init__(self, reg: Registry, rs: rc.RaidStore, owner_id: int, key: str, *, title: str):
        super().__init__(reg, rs, owner_id, key, title=title)
        self.timing: dict = {}
        self.rev: str | None = None

    # ---- what the event holds now, and the draft over it
    def options(self, ev: rc.RaidEvent) -> dict:
        from .event_view import edit_options

        return edit_options(self.reg, self.rs, ev, rc.run_timing(self.reg, ev))

    def value(self, opts: dict, field: str):
        return self.timing.get(field, opts["values"].get(field))

    def set(self, opts: dict, field: str, v) -> None:
        now = opts["values"].get(field)
        same = (float(v) == float(now)) if field in HOURS and now is not None else v == now
        if same:
            self.timing.pop(field, None)
        else:
            self.timing[field] = v

    def words(self, field: str, v) -> str:
        if field == "weights":
            P = PAGE_WORDS["priority"]
            return ", ".join(f"{P[k]} {v.get(k)}" for k in P)
        if v is None:
            return PAGE_WORDS["off"]
        return cq.event_value_words(field, v)

    def lines(self, opts: dict) -> list[str]:
        out = []
        splits = opts["splits"]
        for f in opts["fields"]:
            if f == "split_policy":
                drafted = self.timing.get("split_policy") or {}
                for s in splits:
                    label = (f"{s['raid']} · " if len(splits) > 1 else "") + setting_label(f)
                    was, now = self.words(f, s["value"]), self.words(f, drafted.get(s["key"], s["value"]))
                    out.append(f"{label}: {was}" + (f" → {now}" if now != was else ""))
                continue
            was, now = self.words(f, opts["values"].get(f)), self.words(f, self.value(opts, f))
            out.append(f"{setting_label(f)}: {was}" + (f" → {now}" if f in self.timing and now != was else ""))
        return out

    # ---- the hub
    async def start(self, interaction: discord.Interaction) -> None:
        await self.hub(interaction)

    async def hub(self, interaction: discord.Interaction) -> None:
        ev = self.ev()
        if ev is None or ev.state not in ("open", "locked") or ev.start <= self.reg.now_local():
            await self.finish(interaction, PAGE_WORDS["edit_over"])
            return
        opts = self.options(ev)
        allowed = set(opts["fields"])
        self.timing = {k: v for k, v in self.timing.items() if k in allowed}  # a lock meanwhile narrows the draft
        head = [f"**{run_title(self.reg, ev)}**"] + ([PAGE_WORDS["edit_locked"]] if ev.state != "open" else [])
        choices = [Opt(f, setting_label(f)) for f in opts["fields"]]
        await self.plain(interaction, "\n".join(head + self.lines(opts)),
                         self.select(CHANGE, choices, self.setting),
                         self.button(REVIEW, self.review, discord.ButtonStyle.primary, disabled=not self.timing),
                         self.button("Back", self.cancel))

    async def setting(self, interaction: discord.Interaction, values: list[str]) -> None:
        ev = self.ev()
        if ev is None:
            await self.hub(interaction)
            return
        opts, f = self.options(ev), values[0]
        if f not in opts["fields"]:  # locked since the hub was drawn
            await self.hub(interaction)
            return
        back = self.button("Back", self.hub)
        if f == "weights":
            await self.open_form(interaction, self.weights_form(opts))
            return
        if f == "split_policy":
            await self.split_screen(interaction)
            return
        cur = self.value(opts, f)
        if f in HOURS:
            choices = [Opt(str(c["value"]), c["words"], default=cur is not None and float(c["value"]) == float(cur))
                       for c in trim_hours(opts["hours"][f], cur)]
        elif f == "first_come":
            choices = [Opt("first", PAGE_WORDS["first_to_join"], default=bool(cur)), Opt("priority", PAGE_WORDS["by_priority"], default=not cur)]
        else:
            choices = [Opt("on", PAGE_WORDS["on"], default=bool(cur)), Opt("off", PAGE_WORDS["off"], default=not cur)]

        async def picked(i: discord.Interaction, vals: list[str]) -> None:
            ev2 = self.ev()
            if ev2 is not None:
                o = self.options(ev2)
                v = vals[0]
                self.set(o, f, _num(v) if f in HOURS else v == "first" if f == "first_come" else v == "on")
            await self.hub(i)

        await self.plain(interaction, f"**{run_title(self.reg, ev)}**\n{setting_label(f)}: {self.words(f, cur)}",
                         self.select(setting_label(f), choices, picked), back)

    def weights_form(self, opts: dict) -> Form:
        from .schedule_view import MAX_WEIGHT

        w = self.value(opts, "weights") or {}
        fields = [Field(k, label, [Opt(str(n), str(n), default=n == w.get(k)) for n in range(MAX_WEIGHT + 1)])
                  for k, label in PAGE_WORDS["priority"].items()]

        async def submitted(i: discord.Interaction, values: dict[str, str]) -> None:
            ev = self.ev()
            if ev is not None:
                o = self.options(ev)
                now = o["values"].get("weights") or {}
                self.set(o, "weights", {k: int(values[k]) if values.get(k) not in (None, "") else now.get(k) for k in PAGE_WORDS["priority"]})
            await self.hub(i)

        return Form(setting_label("weights"), fields, submitted)

    async def split_screen(self, interaction: discord.Interaction) -> None:
        ev = self.ev()
        if ev is None:
            await self.hub(interaction)
            return
        opts = self.options(ev)
        splits = opts["splits"][:4]
        drafted = self.timing.get("split_policy") or {}
        items: list[discord.ui.Item] = []
        for s in splits:
            cur = drafted.get(s["key"], s["value"])
            label = (f"{s['raid']} · " if len(splits) > 1 else "") + setting_label("split_policy")
            choices = [Opt(p, PAGE_WORDS["split"].get(p, p), default=p == cur) for p in opts["split_policies"]]
            items.append(self.select(label, choices, self._split_pick(s["key"])))
        await self.plain(interaction, "\n".join([f"**{run_title(self.reg, ev)}**"] + self.lines({**opts, "fields": ["split_policy"]})),
                         *items, self.button("Back", self.hub))

    def _split_pick(self, key: str):
        async def picked(i: discord.Interaction, vals: list[str]) -> None:
            ev = self.ev()
            if ev is not None:
                now = {s["key"]: s["value"] for s in self.options(ev)["splits"]}
                want = {**(self.timing.get("split_policy") or {}), key: vals[0]}
                want = {k: v for k, v in want.items() if now.get(k) != v}
                if want:
                    self.timing["split_policy"] = want
                else:
                    self.timing.pop("split_policy", None)
            await self.split_screen(i)
        return picked

    # ---- review and save
    def box(self) -> dict:
        ev = self.ev()
        if ev is None:
            raise RegistryError(rc.MOVE_CLOSED)
        self.rev = rc.event_rev(self.reg, self.rs, ev)
        return cq.reschedule_box(self.reg, self.rs, ev, None, dict(self.timing), edit=True)

    async def review(self, interaction: discord.Interaction) -> None:
        try:
            box = self.box()
        except RegistryError as e:
            box = cq.refusal_box_for(str(e))
        await self.decide_box(interaction, box, do=self.save, refresh=self.box, back=self.hub)

    async def save(self, i: discord.Interaction, _v: dict) -> None:
        ev, rev, timing = self.ev(), self.rev, dict(self.timing)
        await self.act(i, lambda: i.client.reschedule_event(self.reg, self.rs, ev, timing=timing, by=i.user.display_name, by_id=i.user.id, rev=rev))


@flow("raid_edit")
async def open_raid_edit(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def then(i: discord.Interaction, _wiz: Wizard, rs: rc.RaidStore, ev: rc.RaidEvent | None) -> None:
        if ev is None:  # it closed while the list was open
            await _wiz.finish(i, NO_EVENT)
            return
        await adopt(_wiz, EditEventWizard(reg, rs, i.user.id, ev.key, title="Edit event")).start(i)

    await pick_event(interaction, reg, title="Edit event", then=then)


# ---------------------------------------------------------------- the lineup block (/raid lineup; Tasks 5 and 6 reuse it)

STEP = 15  # a later raid's start is picked in 15-minute steps
START_CHOICES = 24  # after the previous raid's start: 15 minutes to 6 hours


class LineupBlock:
    """A lineup drafted on one screen, mixed into a Wizard: `self.lineup` = [{raid, rosters[, starts]}], entry 0 the
    first raid (it never changes — rc.LINEUP_FIRST), a later entry's `starts` in minutes after the first (absent = the
    Registry.lineup_offsets default: when the raid before it ends). It knows nothing of events: the host gives
    `lineup_start()` (the first raid's start for clock words; None for a schedule) and `lineup_done(i)` (Review)."""

    lineup: list[dict]
    lineup_page: int = 0

    def lineup_start(self):
        return None

    async def lineup_done(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

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

        async def goto(i: discord.Interaction, page: int) -> None:
            self.lineup_page = page
            await self.lineup_hub(i)

        from .flows_config import OwnerWizard

        add, page_note = OwnerWizard.paged(self, "Add a raid", free, self._lineup_add, self.lineup_page, goto)
        if full:
            add[0].disabled = True
        text = "\n".join(([note] if note else []) + lines + ([page_note] if page_note else []))
        await self.show(interaction, text, self.select("Pick a step", steps, self._lineup_pick), *add,
                        self.button(REVIEW, self.lineup_done, discord.ButtonStyle.primary), self.button("Back", self.cancel))

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
        previous raid's start plus 15 minutes. Values are minutes after the first raid; "end" = the default."""
        e = self.lineup[n]
        default = self.reg.lineup_offsets([*self.lineup[:n], {k: v for k, v in e.items() if k != "starts"}])[n]
        prev = offsets[n - 1]
        out = [Opt("end", f"When {self._raid_name(self.lineup[n - 1]['raid'])} ends · {self._at_words(default)}",
                   default=e.get("starts") is None)]
        for s in range(1, START_CHOICES + 1):
            at = prev + STEP * s
            out.append(Opt(str(at), self._at_words(at), default=e.get("starts") is not None and int(e["starts"]) == at))
        return out[:MAX_OPTIONS]

    def _set_start(self, n: int):
        async def picked(i: discord.Interaction, vals: list[str]) -> None:
            if vals[0] == "end":
                self.lineup[n].pop("starts", None)
            else:
                self.lineup[n]["starts"] = int(vals[0])
            for e in self.lineup[n + 1:]:  # a later raid follows from here again
                e.pop("starts", None)
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


class EditLineupWizard(LineupBlock, EventWizard):
    """/raid lineup: the run's lineup now (rc.run_lineup) drafted on the lineup block, saved through box 4
    (consequences.lineup_box, Save lineup) → RaidMixin.edit_lineup with the rev the box showed. A run whose lineup is
    fixed (rc.lineup_fixed: locked, finished) is its refusal box at once."""

    def __init__(self, reg: Registry, rs: rc.RaidStore, owner_id: int, key: str, *, title: str):
        super().__init__(reg, rs, owner_id, key, title=title)
        ev = self.ev()
        self.lineup = [dict(e) for e in rc.run_lineup(reg, rs, ev)] if ev is not None else []
        self.rev: str | None = None

    def lineup_start(self):
        ev = self.ev()
        return ev.start if ev is not None else None

    async def start(self, interaction: discord.Interaction) -> None:
        ev = self.ev()
        if ev is None:
            await self.finish(interaction, NO_EVENT)
            return
        if (why := rc.lineup_fixed(self.rs, ev)):
            await self.decide_box(interaction, cq.refusal_box_for(why), do=self._nothing)
            return
        await self.lineup_hub(interaction)

    def box(self) -> dict:
        ev = self.ev()
        if ev is None:
            raise RegistryError(rc.LINEUP_CLOSED)
        self.rev = rc.event_rev(self.reg, self.rs, ev)
        return cq.lineup_box(self.reg, self.rs, ev, [dict(e) for e in self.lineup])

    async def lineup_done(self, interaction: discord.Interaction) -> None:
        try:
            box = self.box()
        except RegistryError as e:
            box = cq.refusal_box_for(str(e))
        await self.decide_box(interaction, box, do=self.save, refresh=self.box, back=self.lineup_hub)

    async def save(self, i: discord.Interaction, _v: dict) -> None:
        ev, rev, lineup = self.ev(), self.rev, [dict(e) for e in self.lineup]
        await self.act(i, lambda: i.client.edit_lineup(self.reg, self.rs, ev, lineup, by=i.user.display_name, by_id=i.user.id, rev=rev))

    async def _nothing(self, i: discord.Interaction, _v: dict) -> None:  # a refusal box has no verb: never pressed
        await self.finish(i, "Nothing was changed.")


@flow("raid_lineup")
async def open_raid_lineup(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await officer_only(interaction, reg):
        return

    async def then(i: discord.Interaction, _wiz: Wizard, rs: rc.RaidStore, ev: rc.RaidEvent | None) -> None:
        if ev is None:  # it closed while the list was open
            await _wiz.finish(i, NO_EVENT)
            return
        await adopt(_wiz, EditLineupWizard(reg, rs, i.user.id, ev.key, title="Edit lineup")).start(i)

    await pick_event(interaction, reg, title="Edit lineup", then=then)
