"""The schedule wizard (flow `schedule`, design D4 of phase 5): the guild's schedules — a raid's, from `/gm config raid`'s
Schedules section — saved through configops exactly like the site's schedule editor, on the server's boxes: create (13),
save (14), pause (15), resume (16), remove (17) (configops.describe_ops(…).box, consequences.schedule_*_box).

A list (Active · Paused, named by registry.schedule_name, never an id) → one schedule's hub. The hub's draft starts as
schedule_view.stored_entry(row) and holds every setting the editor holds; the Save sends only what differs from it, with
the schedule's rev read with the box (`config:schedule:<id>`, under config_lock), so another officer's save meanwhile is
shown again under CONFIRM_WORDS["changed"] (the box's fp carries the rev) and the next press saves what that box said;
the write re-describes under config_lock (apply_ops `expect`) and a box changed in the gap is shown again. A new schedule is
create-shaped (its kind and lineup, no rev). The lineup is flows_event.LineupBlock. Every word a person reads here is
the site's: PAGE_WORDS["schedule_fields"] for each setting, hours from schedule_view.editor_options."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

import discord

from . import consequences as cq
from . import raidcycle as rc
from .constants import PAGE_WORDS, hours_words, schedule_line_label
from .flows_config import OFFICERS_ONLY, MultiForm, OwnerWizard, _officer_guard
from .flows_event import REVIEW, LineupBlock, trim_hours
from .registry import RESERVED_SCHEDULE_IDS, Registry, RegistryError, parse_lineup, parse_slots, schedule_name
from .wizard import MAX_OPTIONS, Field, Form, Opt, flow, hour_opts, minute_opts

F = PAGE_WORDS["schedule_fields"]
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
KINDS = {"weekly": "Weekly", "lockout": "Day N of each lockout", "pickup": "Opened by an officer"}  # the site's KINDS
HOUR_FIELDS = ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours")
SWITCHES = ("nudge", "confirm", "autofill", "open_dm")
PICKUP_HIDDEN = {"signup_lead_hours", "nudge_hours_before", "nudge", "open_dm"}  # a pickup reminds nobody (the site's)
SHOWN = ("signup_lead_hours", "nudge", "nudge_hours_before", "lock_hours_before", "confirm", "confirm_hours_before",
         "autofill", "fill_ask_hours", "open_dm", "weights", "split_policy")  # the hub's lines after Repeats and Lineup
MAX_RUN_TIMES = 14
NEW = "New schedule"
CHANGE = "Change"
SHEET_TIMING = "Sheet timing"
NOTHING_TO_SAVE = "Nothing has changed yet, so there is nothing to save."
EMPTY_RAID = "No schedule runs {raid} yet. Add one with New schedule."


def _slot_key(slot: str) -> tuple[int, str]:
    day, hm = slot.split()
    return DAYS.index(day[:3].title()), f"{int(hm.split(':')[0]):02d}:{hm.split(':')[1]}"


def new_id(reg: Registry, name: str) -> str:
    """A new schedule's id from its name, unique across the guild, never a reserved one (the site's slug)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:12] or "schedule"
    taken = {s["id"] for s in reg.all_schedules()}
    sid, n = base, 2
    while sid in taken or sid in RESERVED_SCHEDULE_IDS:
        sid, n = f"{base}-{n}", n + 1
    return sid


class ScheduleWizard(LineupBlock, OwnerWizard):
    """The list, one schedule's hub, its screens, and the boxes. `back`: the raid wizard's hub (a hand-off), else the
    list's Back closes the wizard."""

    def __init__(self, reg: Registry, owner_id: int, raid: str | None = None,
                 back: Callable[[discord.Interaction, int | None], Awaitable[None]] | None = None):
        super().__init__(reg, owner_id, title="Schedules", guard=_officer_guard(reg))
        self.raid = raid if raid in reg.profile.raids else None
        self.back_to = back
        self.sid: str | None = None
        self.new = False
        self.base: dict = {}
        self.rev: str | None = None
        self.lineup: list[dict] = []
        self._ops: list = []
        self._box_rev: str | None = None  # the schedule's rev read with the box last shown
        self._fp: str | None = None  # that box's fingerprint (apply_ops `expect`)
        self._make: Callable[[], list] = lambda: []

    # ---- the list
    def rows(self) -> list[dict]:
        return [s for s in self.reg.all_schedules() if self.raid is None or s["raid"] == self.raid]

    async def start(self, i: discord.Interaction, note: str = "") -> None:
        from .schedule_view import repeats_words

        self.sid, self.new, self.draft, self.lineup = None, False, {}, []
        rows = sorted(self.rows(), key=lambda s: not s.get("active", True))  # Active, then Paused: the text and the select agree
        lines = [note] if note else []
        if not rows:
            lines.append(EMPTY_RAID.format(raid=self.raid_name(self.raid)) if self.raid else PAGE_WORDS["schedules_empty"])
        pages = max(1, -(-len(rows) // MAX_OPTIONS))
        self.page = max(0, min(getattr(self, "page", 0), pages - 1))
        shown = rows[self.page * MAX_OPTIONS:(self.page + 1) * MAX_OPTIONS]  # the page the select holds, and only it
        for head, active in (("Active", True), ("Paused", False)):
            group = [s for s in shown if bool(s.get("active", True)) == active]
            if group:
                lines.append(f"**{head}**")
                lines += [f"• {schedule_name(s)} · {repeats_words(self.reg, s)}" for s in group]
        items: list[discord.ui.Item] = []
        if rows:
            opts = [Opt(s["id"], schedule_name(s), None if s.get("active", True) else PAGE_WORDS["paused"]) for s in rows]
            sel, page_note = self.paged("Pick a schedule", opts, self.picked, self.page, self._goto)
            items += sel
            if page_note:
                lines.append(page_note)
        items += [self.button(NEW, self.new_schedule, discord.ButtonStyle.primary), self.button("Back", self.leave)]
        await self.show(i, "\n".join(lines), *items)

    async def _goto(self, i: discord.Interaction, page: int) -> None:
        self.page = page
        await self.start(i)

    async def leave(self, i: discord.Interaction) -> None:
        if self.back_to is None:
            await self.cancel(i)
            return
        self._closed = True
        self.stop()
        await self.back_to(i, self.message_id)

    async def picked(self, i: discord.Interaction, values: list[str]) -> None:
        try:
            row = self.reg.schedule_entry(values[0])
        except RegistryError as e:
            await self.start(i, str(e))
            return
        from .schedule_view import stored_entry

        self.sid, self.new = row["id"], False
        self.base = stored_entry(row)
        self.draft = {k: (list(v) if isinstance(v, list) else v) for k, v in self.base.items() if k != "lineup"}
        self.lineup = [dict(e) for e in self.base["lineup"]]
        self.rev = self.reg.config_rev(f"schedule:{self.sid}")
        await self.hub(i)

    # ---- a new schedule: its first raid, its kind, its name
    async def new_schedule(self, i: discord.Interaction) -> None:
        if self.raid is None:
            from .wizard_opts import raids

            async def raid_picked(it: discord.Interaction, values: list[str]) -> None:
                await self.kind_screen(it, values[0])
            await self.show(i, f"**{NEW}**", self.select("First raid", raids(self.reg), raid_picked), self.button("Back", self.start))
            return
        await self.kind_screen(i, self.raid)

    async def kind_screen(self, i: discord.Interaction, raid: str) -> None:
        async def kind_picked(it: discord.Interaction, values: list[str]) -> None:
            kind = values[0]

            async def named(si: discord.Interaction, v: dict[str, str]) -> None:
                self.seed_new(raid, kind, (v.get("name") or "").strip()[:40])
                await self.hub(si)
            await self.open_form(it, Form(NEW, [Field("name", F["name"], required=False, max_length=40,
                                                      description="What officers and members read, such as Main night")], named))
        await self.show(i, f"**{NEW}** · {self.raid_name(raid)}", self.select(F["kind"], [Opt(k, w) for k, w in KINDS.items()], kind_picked),
                        self.button("Back", self.start))

    def seed_new(self, raid: str, kind: str, name: str) -> None:
        from .registry import ADHOC_DEFAULTS, SCHEDULE_TIMING

        timing = ({k: ADHOC_DEFAULTS[k] for k in SCHEDULE_TIMING if k in ADHOC_DEFAULTS} if kind == "pickup"
                  else self.reg.timing_suggestion(raid))
        self.sid, self.new, self.rev, self.base = None, True, None, {}
        self.draft = {"name": name, "kind": kind, "active": True, "slots": [], "days": [], "time": "20:00" if kind == "lockout" else None,
                      "weights": None, "split_policy": None, **{k: timing.get(k) for k in (*HOUR_FIELDS, *SWITCHES)}}
        self.lineup = [{"raid": raid, "rosters": 1}]

    # ---- words
    def raid_name(self, rid: str | None) -> str:
        return self.reg.raid_def(rid).get("name", rid) if rid in self.reg.profile.raids else str(rid)

    def entry(self) -> dict:
        """The draft as a schedule row (what repeats_words and the boxes read)."""
        return {**self.draft, "id": self.sid or "", "raid": self.lineup[0]["raid"] if self.lineup else self.raid, "lineup": self.lineup}

    def name(self) -> str:
        return schedule_name(self.entry())

    def label(self, f: str) -> str:
        return schedule_line_label(f) or F.get(f, f)

    def words(self, f: str, v) -> str:
        if f in HOUR_FIELDS:
            return PAGE_WORDS["suggested"] if v is None else hours_words(v) + ("" if f == "fill_ask_hours" else " before")
        if f in SWITCHES:
            return PAGE_WORDS["suggested"] if v is None else PAGE_WORDS["on" if v else "off"]
        if f == "split_policy":
            return PAGE_WORDS["split"].get(v, v) if v else PAGE_WORDS["raids_own"]
        if f == "weights":
            P = PAGE_WORDS["priority"]
            return ", ".join(f"{P[k]} {v[k]}" for k in P if k in (v or {})) or PAGE_WORDS["raids_own"]
        if f == "lineup":
            return rc.lineup_words(self.reg, v)
        if f == "repeats":
            from .schedule_view import repeats_words

            return repeats_words(self.reg, v, full=True)
        return str(v) if v not in (None, "") else PAGE_WORDS["raids_own"]

    def shown(self) -> list[str]:
        pickup = self.draft.get("kind") == "pickup"
        return [f for f in SHOWN if not (pickup and f in PICKUP_HIDDEN)]

    def line(self, label: str, was: str, now: str) -> str:
        return f"{label}: {was}" + (f" → {now}" if not self.new and now != was else "")

    def lines(self) -> list[str]:
        base = self.entry() if self.new else {**self.base, "id": self.sid, "raid": self.base["lineup"][0]["raid"]}
        out = [self.line(F["kind"], self.words("repeats", base), self.words("repeats", self.entry())),
               self.line(F["lineup"], self.words("lineup", base["lineup"]), self.words("lineup", self.lineup))]
        out += [self.line(self.label(f), self.words(f, base.get(f)), self.words(f, self.draft.get(f))) for f in self.shown()]
        return out

    # ---- the hub
    def changed(self) -> dict:
        """What the Save sends: an edit's fields that differ from the schedule as the hub loaded it; a new one whole."""
        if self.new:  # only what the screen shows: a pickup's hidden reminder settings are left to the server
            hidden = PICKUP_HIDDEN if self.draft.get("kind") == "pickup" else set()
            out = {k: v for k, v in self.draft.items() if v not in (None, [], "") and k != "active" and k not in hidden}
            out["lineup"] = [dict(e) for e in self.lineup]
            return out
        out = {k: v for k, v in self.draft.items() if v != self.base.get(k)}
        try:
            same = parse_lineup(self.lineup) == parse_lineup(self.base["lineup"])
        except RegistryError:
            same = False
        if not same:
            out["lineup"] = [dict(e) for e in self.lineup]
        return out

    async def hub(self, i: discord.Interaction, note: str = "") -> None:
        if not self.new:
            try:
                self.reg.schedule_entry(self.sid)
            except RegistryError as e:
                await self.start(i, str(e))
                return
        head = [f"**{self.name()}**"] + ([note] if note else []) + self.lines()
        if not self.new and self.draft.get("kind") != "pickup":
            row = self.reg.schedule_entry(self.sid)
            if not row.get("active", True):
                head.append(PAGE_WORDS["paused_next"])
            else:
                nxt = rc.next_runs_of(self.reg, row["raid"], row, n=1)
                if nxt:
                    head.append(f"Next event: {self.reg.local12(nxt[0])}")
        names = ", ".join(F[f] for f in SWITCHES if f in self.shown())
        choices = [Opt("name", F["name"]), Opt("repeats", F["kind"]), Opt("lineup", F["lineup"]), Opt("timing", SHEET_TIMING),
                   Opt("switches", names), Opt("weights", F["weights"]), Opt("split_policy", F["split_policy"])]
        items: list[discord.ui.Item] = [self.select(CHANGE, choices, self.change),
                                        self.button(REVIEW, self.review, discord.ButtonStyle.primary, disabled=not self.changed())]
        if not self.new:
            active = self.reg.schedule_entry(self.sid).get("active", True)
            items += [self.button("Pause" if active else "Resume", self.unsaved_first(self.pause_or_resume)),
                      self.button("Remove", self.unsaved_first(self.remove), discord.ButtonStyle.secondary)]
        items.append(self.button("Back", self.unsaved_first(self.start)))
        heading, self.heading = self.heading, False  # the schedule's name is the screen's title
        try:
            await self.show(i, "\n".join(head), *items)
        finally:
            self.heading = heading

    def unsaved_first(self, then: Callable[[discord.Interaction], Awaitable[None]]):
        """Leaving the hub (Pause, Resume, Remove, Back) with edits not saved: box 20 first — Discard edits (red) drops
        them and goes on, Keep editing returns to the hub with them."""
        async def press(i: discord.Interaction) -> None:
            n = len(self.changed())
            if not n:
                await then(i)
                return
            from .constants import CONFIRM_WORDS
            from .wizard import box_text

            async def discard(it: discord.Interaction) -> None:
                if not self.new:
                    self.draft = {k: (list(v) if isinstance(v, list) else v) for k, v in self.base.items() if k != "lineup"}
                    self.lineup = [dict(e) for e in self.base["lineup"]]
                await then(it)
            heading, self.heading = self.heading, False
            try:
                await self.show(i, box_text(cq.leave_box(n, self.name())),
                                self.button(CONFIRM_WORDS["discard"], discard, discord.ButtonStyle.danger),
                                self.button(CONFIRM_WORDS["keep"], self.hub))
            finally:
                self.heading = heading
        return press

    async def change(self, i: discord.Interaction, values: list[str]) -> None:
        what = values[0]
        if what == "name":
            await self.open_form(i, Form(F["name"], [Field("name", F["name"], required=False, max_length=40, default=self.draft.get("name") or None,
                                                           description="What officers and members read, such as Main night")], self.renamed))
        elif what == "repeats":
            await self.repeats(i)
        elif what == "lineup":
            self.lineup_page = 0
            await self.lineup_hub(i)
        elif what == "timing":
            await self.timing(i)
        elif what == "switches":
            await self.switches(i)
        elif what == "weights":
            await self.open_form(i, self.weights_form())
        else:
            await self.split(i)

    async def renamed(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["name"] = (v.get("name") or "").strip()[:40] or None
        await self.hub(i)

    # ---- the lineup (LineupBlock's hooks)
    async def lineup_done(self, interaction: discord.Interaction) -> None:
        await self.hub(interaction)

    async def lineup_back(self, interaction: discord.Interaction) -> None:
        await self.hub(interaction)

    # ---- Repeats: a weekly schedule's run times, a lockout schedule's days and start
    async def repeats(self, i: discord.Interaction, note: str = "") -> None:
        kind = self.draft.get("kind") or "weekly"
        text = [f"**{self.name()}**"] + ([note] if note else []) + [f"{F['kind']}: {self.words('repeats', self.entry())}"]
        items: list[discord.ui.Item] = []
        if kind == "weekly":
            slots = list(self.draft.get("slots") or [])
            items += [self.button(f"✕ {self.reg.slot_label(s)}", self._dropper(s)) for s in slots[:MAX_RUN_TIMES]]
            items.append(self.button("Add a run time", self.add_time, discord.ButtonStyle.primary, disabled=len(slots) >= MAX_RUN_TIMES))
        elif kind == "lockout":
            items.append(self.button("Pick the days and time", self.lockout_form, discord.ButtonStyle.primary))
        items.append(self.button("Back", self.hub))
        heading, self.heading = self.heading, False
        try:
            await self.show(i, "\n".join(text), *items)
        finally:
            self.heading = heading

    def _dropper(self, slot: str):
        async def drop(i: discord.Interaction) -> None:
            self.draft["slots"] = [s for s in self.draft.get("slots") or [] if s != slot]
            await self.repeats(i)
        return drop

    async def add_time(self, i: discord.Interaction) -> None:
        await self.open_form(i, Form("Add a run time", [
            Field("day", "Day", [Opt(d, n) for d, n in zip(DAYS, DAY_NAMES)]),
            Field("hour", "Hour", hour_opts(20), description=f"Guild time, {self.reg.config.timezone}"),
            Field("minute", "Minutes", minute_opts(15, 0)),
        ], self.time_added))

    async def time_added(self, i: discord.Interaction, v: dict[str, str]) -> None:
        slot = f"{v.get('day') or 'Mon'} {int(v.get('hour') or 0):02d}:{int(v.get('minute') or 0):02d}"
        cur = list(self.draft.get("slots") or [])
        if slot in cur:
            await self.repeats(i, f"{self.reg.slot_label(slot)} is already a run time.")
            return
        try:
            self.draft["slots"] = sorted(parse_slots(cur + [slot]), key=_slot_key)
        except RegistryError as e:
            await self.repeats(i, str(e))
            return
        await self.repeats(i)

    async def lockout_form(self, i: discord.Interaction) -> None:
        raid = self.lineup[0]["raid"] if self.lineup else self.raid
        days = int(self.reg.raid_def(raid).get("lockout_days") or 7)
        held = set(self.draft.get("days") or [])
        h, m = (int(x) for x in (self.draft.get("time") or "20:00").split(":"))
        await self.open_form(i, MultiForm(F["days"], [
            Field("days", F["days"], [Opt(str(d), f"Day {d}", default=d in held) for d in range(1, min(days, MAX_OPTIONS) + 1)],
                  description=f"Day 1 is the day the {days}-day lockout resets"),
            Field("hour", "Hour", hour_opts(h), description=f"Guild time, {self.reg.config.timezone}"),
            Field("minute", "Minutes", minute_opts(15, m - m % 15)),
        ], self.lockout_picked, multi={"days": min(days, MAX_OPTIONS)}))

    async def lockout_picked(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["days"] = [int(d) for d in (v.get("days") or "").split(",") if d]
        self.draft["time"] = f"{int(v.get('hour') or 0):02d}:{int(v.get('minute') or 0):02d}"
        await self.repeats(i)

    # ---- Sheet timing: a preset, or one setting's hours
    async def timing(self, i: discord.Interaction) -> None:
        from .schedule_view import editor_options

        pickup = self.draft.get("kind") == "pickup"
        fields = [f for f in HOUR_FIELDS if f in self.shown()]
        text = [f"**{self.name()}** · {SHEET_TIMING}"] + [f"{self.label(f)}: {self.words(f, self.draft.get(f))}" for f in fields]
        items: list[discord.ui.Item] = []
        if not pickup:
            presets = editor_options(self.reg)["presets"]

            async def preset(it: discord.Interaction, values: list[str]) -> None:
                p = next((p for p in presets if p["name"] == values[0]), None)
                if p is not None:
                    self.draft.update(p["values"])
                await self.timing(it)
            items.append(self.select("Use a preset", [Opt(p["name"], p["name"]) for p in presets], preset))
        items.append(self.select("Pick a setting", [Opt(f, self.label(f)) for f in fields], self.hour_screen))
        items.append(self.button("Back", self.hub))
        heading, self.heading = self.heading, False
        try:
            await self.show(i, "\n".join(text), *items)
        finally:
            self.heading = heading

    async def hour_screen(self, i: discord.Interaction, values: list[str]) -> None:
        from .schedule_view import editor_options

        f = values[0]
        cur = self.draft.get(f)
        choices = trim_hours(editor_options(self.reg)["hours"][f], cur)

        async def picked(it: discord.Interaction, vals: list[str]) -> None:
            n = float(vals[0])
            self.draft[f] = int(n) if n.is_integer() else n
            await self.timing(it)
        await self.show(i, f"**{self.name()}**\n{self.label(f)}: {self.words(f, cur)}",
                        self.select(self.label(f), [Opt(str(c["value"]), c["words"], default=cur is not None and float(c["value"]) == float(cur))
                                                    for c in choices], picked),
                        self.button("Back", self.timing))

    # ---- the four switches: one select, field=on|off
    async def switches(self, i: discord.Interaction) -> None:
        fields = [f for f in SWITCHES if f in self.shown()]
        opts = [Opt(f"{f}={v}", f"{F[f]}: {PAGE_WORDS[v]}") for f in fields for v in ("on", "off")]  # the lines say what each is now

        async def picked(it: discord.Interaction, values: list[str]) -> None:
            f, v = values[0].split("=", 1)
            self.draft[f] = v == "on"
            await self.switches(it)
        await self.show(i, "\n".join([f"**{self.name()}**"] + [f"{F[f]}: {self.words(f, self.draft.get(f))}" for f in fields]),
                        self.select("Turn one on or off", opts, picked), self.button("Back", self.hub))

    # ---- Priority: five selects in one modal; "Same as the raid" leaves a weight to the raid
    def weights_form(self) -> Form:
        from .schedule_view import MAX_WEIGHT

        raid = self.lineup[0]["raid"] if self.lineup else self.raid
        raid_w = self.reg.raid_def(raid).get("weights") or {}
        own = self.draft.get("weights") or {}
        fields = [Field(k, label, [Opt("raid", PAGE_WORDS["weight_same"].format(n=raid_w.get(k, 0)), default=k not in own)]
                        + [Opt(str(n), str(n), default=own.get(k) == n) for n in range(MAX_WEIGHT + 1)])
                  for k, label in PAGE_WORDS["priority"].items()]

        async def submitted(i: discord.Interaction, v: dict[str, str]) -> None:
            want = {k: int(v[k]) for k in PAGE_WORDS["priority"] if v.get(k) not in (None, "", "raid")}
            self.draft["weights"] = want or None
            await self.hub(i)
        return Form(F["weights"], fields, submitted)

    # ---- When one night fills two rosters
    async def split(self, i: discord.Interaction) -> None:
        from .registry import SPLIT_POLICIES

        raid = self.lineup[0]["raid"] if self.lineup else self.raid
        own = self.reg.raid_def(raid).get("split_policy") or "balanced"
        cur = self.draft.get("split_policy")
        opts = [Opt("raid", f"{PAGE_WORDS['raids_own']} ({PAGE_WORDS['split'].get(own, own)})", default=cur is None)]
        opts += [Opt(p, PAGE_WORDS["split"].get(p, p), default=cur == p) for p in SPLIT_POLICIES]

        async def picked(it: discord.Interaction, values: list[str]) -> None:
            self.draft["split_policy"] = None if values[0] == "raid" else values[0]
            await self.split(it)
        await self.show(i, f"**{self.name()}**\n{F['split_policy']}: {self.words('split_policy', cur)}",
                        self.select(F["split_policy"], opts, picked), self.button("Back", self.hub))

    # ---- the boxes: the Save (13 / 14), Pause (15), Resume (16), Remove (17)
    def revs(self, rev: str | None = None) -> dict:
        """The schedule's rev as a write carries it: `rev` (the one read with the box shown), else the hub's."""
        from .configops import CONFIG_REV

        rev = rev or self.rev
        return {f"{CONFIG_REV}schedule:{self.sid}": rev} if not self.new and rev else {}

    def save_ops(self) -> list:
        from .configops import ops_from_fields

        fields = self.changed()
        if not fields:
            raise RegistryError(NOTHING_TO_SAVE)
        if self.new:
            return ops_from_fields(self.reg, "schedule", new_id(self.reg, self.draft.get("name") or ""), fields)
        return ops_from_fields(self.reg, "schedule", self.sid, fields, revs=self.revs())

    def box_of(self, make_ops: Callable[[], list]) -> dict:
        """BLOCKING: the server's box for these ops (describe_ops on a shadow copy), or RegistryError with the line's
        refusal. The schedule's rev is read with it under config_lock and kept with the ops, so the write carries the
        rev of the box LAST SHOWN: a box shown again after another officer's save goes through at its press."""
        from .configops import describe_ops

        with self.reg.config_lock:
            ops = make_ops()
            rev = None if self.new else self.reg.config_rev(f"schedule:{self.sid}")
            d = describe_ops(self.reg, ops, by_id=self.owner_id)
        err = next((ln.error for ln in d.lines if ln.error), None) or (d.refused[0] if d.refused else None)
        if err or d.box is None:
            raise RegistryError(err or NOTHING_TO_SAVE)
        self._ops, self._box_rev, self._fp = ops, rev, d.box.get("fp")
        return d.box

    async def ask(self, i: discord.Interaction, make_ops: Callable[[], list]) -> None:
        async def refresh() -> dict:
            return await asyncio.to_thread(self.box_of, make_ops)
        self._make = make_ops
        await self.working(i)
        try:
            box = await refresh()
        except RegistryError as e:
            box = cq.refusal_box_for(str(e))
        await self.decide_box(i, box, do=self.apply, refresh=refresh, back=self.hub)

    async def apply(self, i: discord.Interaction, _v: dict) -> None:
        from .configops import apply_ops, describe_ops

        ops, fp, by_id = list(self._ops), self._fp, self.owner_id

        def same_box() -> bool:  # under config_lock, in the write's step (the site's token apply)
            d = describe_ops(self.reg, ops, by_id=by_id)
            return d.box is not None and d.box.get("fp") == fp
        res = await apply_ops(self.reg, ops, by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None),
                              via="discord", revs=self.revs(self._box_rev), expect=same_box)
        if res.get("stale_box"):  # it changed in the gap: the new box, nothing written
            try:
                box = await asyncio.to_thread(self.box_of, self._make)
            except RegistryError as e:
                box = cq.refusal_box_for(str(e))
            await self.decide_box(i, box, do=self.apply, refresh=lambda: asyncio.to_thread(self.box_of, self._make),
                                  back=self.hub, changed=box.get("verb") is not None)
            return
        bad = [r["line"] for r in res["results"] if not r["ok"]]
        if bad or not res["applied"]:
            await self.finish(i, "❌ " + " ".join(bad or [res.get("error") or NOTHING_TO_SAVE]))
            return
        await self.finish(i, "✅ " + "\n".join(res["applied"]))

    async def review(self, i: discord.Interaction) -> None:
        await self.ask(i, self.save_ops)

    async def pause_or_resume(self, i: discord.Interaction) -> None:
        from .configops import ops_from_fields

        try:
            on = not self.reg.schedule_entry(self.sid).get("active", True)
        except RegistryError as e:  # removed meanwhile: the list, with its sentence
            await self.start(i, str(e))
            return
        await self.ask(i, lambda: ops_from_fields(self.reg, "schedule", self.sid, {"active": on}, revs=self.revs()))

    async def remove(self, i: discord.Interaction) -> None:
        from .configops import ops_from_fields

        await self.ask(i, lambda: ops_from_fields(self.reg, "schedule_remove", self.sid, {}, revs=self.revs()))


@flow("schedule")
async def open_schedule(interaction: discord.Interaction, reg: Registry, *, raid: str | None = None, **seed) -> None:
    """`/gm config raid` › Schedules (officers, D8): the raid's schedules (`raid`), or the guild's. `message_id` + `back`:
    the raid wizard's screen this wizard takes over, and its hub to return to."""
    from . import discord_registry

    if not discord_registry.is_officer(interaction, reg):
        await interaction.response.send_message(OFFICERS_ONLY, ephemeral=True)
        return
    wiz = ScheduleWizard(reg, interaction.user.id, raid, back=seed.get("back"))
    wiz.message_id = seed.get("message_id")
    await wiz.start(interaction)
