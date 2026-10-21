"""The owner's `/gm config` wizards: raid rules, aura facts, the guild's timezone and the about blurb.

`raid_config` is the flagship. It picks a raid, then a HUB of sections (run times, cadence, group make-up, weights,
lockout and length, first lockout, behaviour, notes, reset). Every section edits a working copy (`self.draft`, in the
exact field names `Registry._apply_raid_override` accepts) and returns to the hub; nothing is written until Save,
which shows the diff as sentences plus the next three runs computed from the DRAFT and then makes ONE
`reg.set_raid_overrides` call — one commit, all or nothing.

A section refuses a bad combination on the spot rather than at Save: `RaidConfigWizard.preview` applies the draft with
the registry's own validators (`_apply_raid_override(commit=False)` + `_check_raid`) and restores the config
immediately, exactly as `set_raid_overrides` does on a refusal — so the wizard never re-implements a rule and can never
drift from it.

No value here is typed except prose (notes, an aura note, the about blurb): hours are preset selects worded "5 days
before", run times are nights + hour + minutes, dates are day/hour/minute selects narrowed by week, and the timezone is a
region then a paged city list.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import discord

from .presets import PRESETS  # (key, name, opens, nudge, lock, confirm), shared with plain text
from .registry import (ADHOC_DEFAULTS, DEFAULT_RAID_SIZE, DEFAULT_SCHEDULE, DEFAULT_SCHEDULE_NAME, MAX_SCHEDULE_ROSTERS, RAID_WEIGHT_DEFAULTS, SPLIT_POLICIES, Registry,
                       RegistryError, parse_slots, schedule_def_of, schedule_value, schedules_of)
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, both_clocks, combine_when, flow, fmt12, hour_opts, minute_opts, week_opts, when_fields, when_note

# ---------------------------------------------------------------- shared pieces


class MultiForm(Form):
    """A Form whose named selects take several picks (`multi={key: max_values}`); `values()` joins them with commas.
    wizard.Form only does single picks — promote this into it if a second flow needs it."""

    def __init__(self, title: str, fields: Sequence[Field], on_submit, *, multi: dict[str, int]):
        super().__init__(title, fields, on_submit)
        for key, n in multi.items():
            item = self._items.get(key)
            if isinstance(item, discord.ui.Select):
                item.min_values, item.max_values = 1, max(1, min(n, len(item.options)))

    def values(self) -> dict[str, str]:
        out = super().values()
        for key, item in self._items.items():
            if isinstance(item, discord.ui.Select) and item.max_values > 1:
                out[key] = ",".join(item.values)
        return out


def _owner_guard(reg: Registry) -> Callable[[discord.Interaction], Awaitable[str | None]]:
    async def guard(i: discord.Interaction) -> str | None:
        from .discord_registry import is_owner

        return None if is_owner(i, reg) else "Owner only."
    return guard


async def _owner_or_refuse(i: discord.Interaction, reg: Registry) -> bool:
    from .discord_registry import is_owner

    if is_owner(i, reg):
        return True
    await i.response.send_message("Owner only.", ephemeral=True)
    return False




def num(x) -> str:
    return f"{float(x):g}"


def hours_words(h) -> str:
    """48 → '2 days', 168 → 'a week', 24 → '24 hours', 1 → '1 hour'."""
    h = float(h)
    if h == 168:
        return "a week"
    if h >= 48 and h % 24 == 0:
        return f"{int(h // 24)} days"
    return f"{num(h)} hour{'' if h == 1 else 's'}"


HOURS = (1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48, 72, 96, 120, 168)


def hours_opts(current=None, *, upto: float | None = None, suffix: str = " before") -> list[Opt]:
    vals = sorted({float(h) for h in HOURS if upto is None or h <= upto} | ({float(current)} if current is not None else set()))
    return [Opt(num(h), f"{hours_words(h)}{suffix}", default=current is not None and float(current) == h) for h in vals][:MAX_OPTIONS]


class OwnerWizard(Wizard):
    """Wizard + a pager: a list that can pass 25 options is shown a page at a time with More → / ← Previous."""

    def paged(self, placeholder: str, opts: Sequence[Opt], on_pick, page: int, goto: Callable[[discord.Interaction, int], Awaitable[None]]) -> tuple[list, str]:
        opts = list(opts)
        pages = max(1, -(-len(opts) // MAX_OPTIONS))
        page = max(0, min(page, pages - 1))
        items: list = [self.select(placeholder, opts[page * MAX_OPTIONS:(page + 1) * MAX_OPTIONS], on_pick)]
        if page > 0:
            items.append(self.button("← Previous", lambda i: goto(i, page - 1)))
        if page < pages - 1:
            items.append(self.button("More →", lambda i: goto(i, page + 1)))
        return items, (f"-# page {page + 1} of {pages}" if pages > 1 else "")


# ---------------------------------------------------------------- raid rules

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CADENCE = ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before")
ROLES = ("tank", "healer", "dps")
ROLE_WORDS = {"tank": "tanks", "healer": "healers", "dps": "dps"}
WEIGHTS = tuple(RAID_WEIGHT_DEFAULTS)
WEIGHT_LABEL = {"rank": "Rank", "main": "Main over alt", "sat_out": "Sat out last time", "signup_order": "Signed up early", "regular": "Regular of the series"}
WEIGHT_HINT = {"rank": "core > raider > trial", "main": "a main is picked over an alt", "sat_out": "benched in the last lockout",
               "signup_order": "an earlier signup", "regular": "being one of the series' regulars (on their regular character)"}  # the Raids page's WEIGHT_HINT, in words
WEIGHT_WORDS = {0: "doesn't count", 1: "a little", 2: "some", 3: "a lot", 4: "more", 5: "the most"}
SPLIT_WORDS = {"balanced": ("Balanced", "both runs equal in synergy, tanks, healers and seats"),
               "first": ("Raid one first", "roster 1 gets the best, roster 2 the rest"),
               "rotation": ("Rotation", "whoever sat out or was in the weaker run moves up")}
BOOL_WORDS = {"nudge": "Nudge the unanswered", "autofill": "Fill seats automatically", "open_dm": "DM everyone on open",
              "confirm": "Ask the rostered to confirm"}  # registry.RAID_BOOL_FIELDS, in words
SECTIONS = [  # (key, label, fields it edits)
    ("times", "Schedules", ("slots", "schedules")),
    ("cadence", "Signup cadence", CADENCE),
    ("comp", "Group make-up", tuple(f"{r}_{b}" for r in ROLES for b in ("min", "max"))),
    ("weights", "Selection weights", tuple(f"weight_{k}" for k in WEIGHTS)),
    ("length", "Lockout & length", ("lockout_days", "duration_hours")),
    ("first_open", "First lockout opens", ("first_open",)),
    ("behaviour", "Behaviour", ("nudge", "autofill", "open_dm", "confirm", "split_policy", "fill_ask_hours")),
    ("notes", "Notes", ("notes",)),
    ("reset", "Reset to defaults", ()),
]
MAX_SCHEDULES = 10
FIELD_WORDS = {"slots": "Run times", "schedules": "Schedules", "signup_lead_hours": "Sheet opens", "nudge_hours_before": "Nudge", "lock_hours_before": "Roster locks",
               "confirm_hours_before": "Confirm by", "fill_ask_hours": "A fill ask expires after", "nudge": "Nudge the unanswered",
               "autofill": "Fill seats automatically", "open_dm": "DM everyone on open", "confirm": "Ask the rostered to confirm",
               "split_policy": "Split policy", "lockout_days": "Lockout",
               "duration_hours": "Raid length", "first_open": "First lockout opens", "notes": "Notes",
               **{f"weight_{k}": f"Weight · {WEIGHT_LABEL[k].lower()}" for k in WEIGHTS},
               **{f"{r}_{b}": f"{'Fewest' if b == 'min' else 'Most'} {ROLE_WORDS[r]}" for r in ROLES for b in ("min", "max")}}


def slot_key(slot: str) -> tuple[int, str]:
    day, hm = slot.split()
    return DAYS.index(day[:3].title()), f"{int(hm.split(':')[0]):02d}:{hm.split(':')[1]}"


def length_words(h) -> str:
    h = float(h)
    whole = int(h)
    return f"{whole}½ hours" if h - whole == 0.5 else f"{num(h)} hour{'' if h == 1 else 's'}"


def lockout_words(d) -> str:
    d = int(d)
    return "a week" if d == 7 else ("2 weeks" if d == 14 else f"{d} day{'' if d == 1 else 's'}")


def field_value(eff: dict, field: str, tz: str):
    """A raid field's effective value, in the form the draft holds it."""
    if field == "slots":
        return list(eff.get("slots") or [])
    if field == "schedules":
        return copy.deepcopy(list(eff.get("schedules") or []))
    if field in CADENCE or field == "fill_ask_hours" or field == "duration_hours":
        return float(eff[field])
    if field == "lockout_days":
        return int(eff["lockout_days"])
    if field in BOOL_WORDS:
        return bool(eff[field])
    if field == "split_policy":
        return eff.get("split_policy") or "balanced"
    if field.startswith("weight_"):
        return int(eff["weights"][field[7:]])
    if field == "notes":
        return str(eff.get("notes") or "")
    if field == "first_open":
        fo = parse_first_open(eff.get("first_open"), tz)
        return fo.isoformat() if fo else ""
    role, bound = field.split("_")
    b = (eff.get("comp") or {}).get(role) or {}
    return int(b.get(bound, 0 if bound == "min" else int(eff.get("size") or DEFAULT_RAID_SIZE)))


def parse_first_open(raw, tz: str) -> datetime | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo(tz))


def same(field: str, a, b) -> bool:
    if field == "first_open":
        return (a or "") == (b or "") or (bool(a) and bool(b) and datetime.fromisoformat(a) == datetime.fromisoformat(b))
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return float(a) == float(b)
    return a == b


def next_runs(reg: Registry, slots: Sequence[str], first_open: datetime | None, n: int = 3) -> list[datetime]:
    """The next `n` run starts from these slots in the guild zone, never before the first lockout opens."""
    from .raidcycle import next_raid_time

    now = reg.now_local()
    after = max(now, first_open) if first_open else now
    out: list[datetime] = []
    for s in slots:
        try:
            t = next_raid_time(s, reg.config.timezone, after=after - timedelta(minutes=1))
        except ValueError:
            continue
        for _ in range(n):
            out.append(t)
            t = t + timedelta(days=7)  # same wall-clock time next week, across DST
    return sorted(out)[:n]


class RaidConfigWizard(OwnerWizard):
    def __init__(self, reg: Registry, owner_id: int, raid: str | None = None):
        super().__init__(reg, owner_id, title="Raid rules", guard=_owner_guard(reg))
        self.raid = raid
        self.draft: dict = {}
        self.sid = DEFAULT_SCHEDULE  # the schedule the Schedules section is on
        self._attempt: dict = {}  # values a section refused, prefilled when the person presses Fix this
        self._fo_day0: date = reg.now_local().date()

    # ---- values
    def eff(self) -> dict:
        eff, _ = self.preview()
        return eff or self.reg.raid_def(self.raid)

    def stored(self, field: str):
        return field_value(self.reg.raid_def(self.raid), field, self.reg.config.timezone)

    def cur(self, field: str):
        return self.draft[field] if field in self.draft else self.stored(field)

    def put(self, field: str, value) -> None:
        """Into the draft — or out of it when it equals what is stored, so the change count is honest."""
        if same(field, value, self.stored(field)):
            self.draft.pop(field, None)
        else:
            self.draft[field] = value

    def preview(self, draft: dict | None = None) -> tuple[dict | None, str | None]:
        """The raid as it would be with the draft applied, or the registry's refusal — validated by the registry's own
        rules (the same calls set_raid_overrides makes) and restored at once, so nothing stays in memory or git."""
        draft = self.draft if draft is None else draft
        raid = self.raid

        def applied(sh):  # Registry.preview: the same verb on a throw-away copy; the live config is never touched
            sh.set_raid_overrides(raid, dict(draft), "preview")
            return sh.raid_def(raid)

        try:
            return self.reg.preview(applied), None
        except RegistryError as e:
            return None, str(e)

    def size(self) -> int:
        return int(self.reg.raid_def(self.raid).get("size") or DEFAULT_RAID_SIZE)

    def say(self, field: str, v) -> str:
        """A field's value as a sentence fragment a person reads (12-hour, words)."""
        if field == "slots":
            return ", ".join(self.reg.slot_label(s) for s in v) or "none"
        if field == "schedules":
            return "; ".join(f"{s['name']}: {self.reg.schedule_label(self.raid, s)}" for s in schedules_of({**self.sview(), "schedules": v}) if s["id"] != DEFAULT_SCHEDULE) or "only the run times"
        if field in CADENCE:
            return f"{hours_words(v)} before"
        if field == "fill_ask_hours":
            return hours_words(v)
        if field in BOOL_WORDS:
            return "on" if v else "off"
        if field == "split_policy":
            return SPLIT_WORDS.get(v, (v,))[0]
        if field.startswith("weight_"):
            return f"{v} ({WEIGHT_WORDS.get(int(v), '')})".replace(" ()", "")
        if field == "lockout_days":
            return lockout_words(v)
        if field == "duration_hours":
            return length_words(v)
        if field == "first_open":
            return self.reg.local12(datetime.fromisoformat(v)) if v else "not set"
        if field == "notes":
            return f"“{v[:80]}{'…' if len(v) > 80 else ''}”" if v else "none"
        return str(v)

    def summary(self, key: str, eff: dict) -> str:
        tz = self.reg.config.timezone

        def fv(f):
            return field_value(eff, f, tz)
        if key == "times":
            scheds = schedules_of(eff)
            if len(scheds) > 1 or (scheds and scheds[0]["id"] != DEFAULT_SCHEDULE):
                return f"{len(scheds)} schedule{'s' if len(scheds) > 1 else ''}: " + ", ".join(s["name"] for s in scheds)
            return self.say("slots", fv("slots")) if fv("slots") else "no run times yet"
        if key == "cadence":
            return (f"opens {hours_words(fv('signup_lead_hours'))} · nudge {hours_words(fv('nudge_hours_before'))} · "
                    f"locks {hours_words(fv('lock_hours_before'))} · confirm {hours_words(fv('confirm_hours_before'))} before")
        if key == "comp":
            return " · ".join(f"{ROLE_WORDS[r]} {fv(f'{r}_min')}–{fv(f'{r}_max')}" for r in ROLES)
        if key == "weights":
            return " · ".join(f"{WEIGHT_LABEL[k].lower()} {fv(f'weight_{k}')}" for k in WEIGHTS)
        if key == "length":
            return f"lockout {lockout_words(fv('lockout_days'))} · {length_words(fv('duration_hours'))} long"
        if key == "first_open":
            return self.say("first_open", fv("first_open"))
        if key == "behaviour":
            return (" · ".join(f"{w.lower()} {'on' if fv(f) else 'off'}" for f, w in BOOL_WORDS.items())
                    + f" · split {SPLIT_WORDS.get(fv('split_policy'), ('?',))[0].lower()}")
        if key == "notes":
            return self.say("notes", fv("notes"))
        return "drop this guild's changes for the raid" if self.raid in self.reg.config.raids else "already on the game's defaults"

    # ---- screen 1: which raid
    async def start(self, i: discord.Interaction, section: str | None = None) -> None:
        if self.raid not in self.reg.profile.raids:
            self.raid = None
        if self.raid is None and len(self.reg.profile.raids) == 1:
            self.raid = next(iter(self.reg.profile.raids))
        if self.raid is None:
            from .wizard_opts import raids

            await self.show(i, "Which raid's rules?", self.select("Pick a raid", raids(self.reg), self.raid_picked), self.button("Cancel", self.cancel))
            return
        await self.go(i, section) if section else await self.hub(i)

    async def raid_picked(self, i: discord.Interaction, values: list[str]) -> None:
        self.raid = values[0]
        await self.hub(i)

    # ---- screen 2: the hub
    async def hub(self, i: discord.Interaction, note: str = "") -> None:
        eff, err = self.preview()
        shown = eff or self.reg.raid_def(self.raid)
        rd = self.reg.raid_def(self.raid)
        opts = []
        for key, label, fields in SECTIONS:
            changed = any(f in self.draft for f in fields)
            opts.append(Opt(key, f"{label}{' · changed' if changed else ''}", self.summary(key, shown)))
        n = len(self.draft)
        lines = [f"**{rd.get('name', self.raid)}** · {rd.get('size', '?')}-player"]
        if note:
            lines.append(note)
        lines.append(f"✎ **{n} unsaved change{'' if n == 1 else 's'}** — nothing is saved until you press Save." if n else "Pick a section to change. Nothing is saved until you press Save.")
        if err:
            lines.append(f"⚠️ {err}")
        await self.show(i, "\n".join(lines),
                        self.select("Pick a section", opts, self.section_picked),
                        self.button("Save", self.review, discord.ButtonStyle.success, disabled=not n),
                        self.button("Cancel", self.cancel))

    async def to_hub(self, i: discord.Interaction) -> None:
        await self.hub(i)

    async def section_picked(self, i: discord.Interaction, values: list[str]) -> None:
        await self.go(i, values[0])

    async def go(self, i: discord.Interaction, key: str) -> None:
        screens = {"times": self.times, "cadence": self.cadence, "comp": self.comp, "weights": self.weights_form, "length": self.length_form,
                   "first_open": self.first_open, "behaviour": self.behaviour, "notes": self.notes_form, "reset": self.reset}
        await screens.get(key, self.to_hub)(i)

    async def refuse_here(self, i: discord.Interaction, error: str, retry) -> None:
        """A section's refusal: Fix this (the same form, prefilled), Back to the sections (this change dropped), Cancel."""
        await self.show(i, f"❌ {error}", self.button("Fix this", retry, discord.ButtonStyle.primary),
                        self.button("Back to the sections", self.to_hub), self.button("Cancel", self.cancel))

    async def checked(self, i: discord.Interaction, snapshot: dict, retry, note: str) -> None:
        """After a section changed the draft: back to the hub, or — if the registry would refuse — undo and say why."""
        _, err = self.preview()
        if err:
            self.draft = snapshot
            await self.refuse_here(i, err, retry)
            return
        self._attempt = {}
        await self.hub(i, note)

    # ---- Schedules (the "times" section): WHEN the raid runs. `default` is the raid's own weekly slots (draft field
    # "slots"); every other schedule — and default's own settings — live in the draft field "schedules" (the stored list,
    # in the exact form Registry._apply_raid_override takes). A raid with just its run times opens straight on them.
    def sview(self) -> dict:
        """The raid as the draft would make it — schedules included even while another section's change is refused."""
        eff, _ = self.preview()
        base = eff or self.reg.raid_def(self.raid)
        return {**base, "slots": list(self.cur("slots")), "schedules": copy.deepcopy(self.cur("schedules"))}

    def scheds(self) -> list[dict]:
        return schedules_of(self.sview())

    def sched(self, sid: str) -> dict:
        return next((s for s in self.scheds() if s["id"] == sid), None) or {
            "id": DEFAULT_SCHEDULE, "name": DEFAULT_SCHEDULE_NAME, "kind": "weekly", "active": True, "rosters": 1, "slots": [], "days": [], "time": None}

    def sched_runs(self, s: dict) -> list[datetime]:
        from .raidcycle import next_runs_of

        return next_runs_of(self.reg, self.raid, s, rd=self.sview())

    def entry_put(self, sid: str, **changes) -> None:
        """Set (value) or clear (None) settings of one stored schedule entry in the draft."""
        rows = copy.deepcopy(self.cur("schedules"))
        e = next((r for r in rows if r.get("id") == sid), None)
        if e is None:
            e = {"id": sid}
            rows.append(e)
        for k, v in changes.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        self.put("schedules", [r for r in rows if not (r["id"] == DEFAULT_SCHEDULE and len(r) == 1)])

    async def sched_checked(self, i: discord.Interaction, snapshot: dict, sid: str, note: str) -> None:
        """After a schedule change: back to that schedule — or, if the registry would refuse the draft, undo and say why."""
        _, err = self.preview()
        if err:
            self.draft = snapshot

            async def back(it: discord.Interaction) -> None:
                await self.schedule(it, sid)
            await self.refuse_here(i, err, back)
            return
        self._attempt = {}
        await self.schedule(i, sid, note)

    def sched_cadence(self, sid: str) -> str:
        view = self.sview()
        try:
            sd = schedule_def_of(view, sid)
        except RegistryError:
            return ""
        own = sd["schedule"]["own"]
        bits = [f"{w} {hours_words(sd[f])} before" + ("" if f in own else " (the raid's)") for f, w in
                (("signup_lead_hours", "opens"), ("lock_hours_before", "locks"), ("confirm_hours_before", "confirm by"))]
        return "-# " + " · ".join(bits) + (f" · **{sd['schedule']['rosters']} rosters** per run" if int(sd["schedule"].get("rosters") or 1) > 1 else "")

    async def times(self, i: discord.Interaction, note: str = "") -> None:
        scheds = self.scheds()
        if not scheds or (len(scheds) == 1 and scheds[0]["id"] == DEFAULT_SCHEDULE):
            await self.schedule(i, DEFAULT_SCHEDULE, note)
            return
        lines = [note] if note else []
        lines.append("**Schedules** — when this raid runs (guild time):")
        for s in scheds:
            runs = self.sched_runs(s) if s["kind"] != "pickup" else []
            lines.append(f"• **{s['name']}** — {self.reg.schedule_label(self.raid, s)}" + (f" · next {self.reg.local12(runs[0])}" if runs else ""))
        opts = [Opt(s["id"], s["name"], self.reg.schedule_label(self.raid, s)) for s in scheds]
        await self.show(i, "\n".join(lines), self.select("Pick a schedule", opts, self.schedule_picked),
                        self.button("Add a schedule", self.add_schedule, discord.ButtonStyle.primary, disabled=len(scheds) >= MAX_SCHEDULES),
                        self.button("Back to the sections", self.to_hub))

    async def to_times(self, i: discord.Interaction) -> None:
        await self.times(i)

    async def schedule_picked(self, i: discord.Interaction, values: list[str]) -> None:
        await self.schedule(i, values[0])

    async def schedule(self, i: discord.Interaction, sid: str, note: str = "") -> None:
        """One schedule: its times (weekly: the run-time chips; lockout: days + time), cadence, the next three runs."""
        self.sid = sid
        s = self.sched(sid)
        many = len(self.scheds()) > 1
        lines = [note] if note else []
        if s["kind"] == "weekly":
            lines.append(("**Run times**" if not many else f"**{s['name']}** · run times") + " (guild time): "
                         + (", ".join(self.reg.slot_label(x) for x in s["slots"]) or "none yet — nothing opens until a raid has one."))
        else:
            lines.append(f"**{s['name']}** · {self.reg.schedule_label(self.raid, s)}")
        if not s.get("active", True):
            lines.append("⏸ Paused: nothing opens from it until it is resumed.")
        cad = self.sched_cadence(sid)
        if cad:
            lines.append(cad)
        if s["kind"] != "pickup":
            runs = self.sched_runs(s)
            lines.append(("Next runs: " + " · ".join(self.reg.local12(t) for t in runs)) if runs else "-# No run comes up yet.")
        else:
            lines.append("-# A template: `/raid open` offers it, and the run opens at the day and time you pick there.")
        if s["kind"] == "weekly":
            lines.append("-# Press a time to drop it.")
        items: list = [self.select("Change this schedule", self.change_opts(s), self.change_picked)]
        if s["kind"] == "weekly":
            items += [self.button(f"✕ {self.reg.slot_label(x)}", self._dropper(x)) for x in s["slots"][:14]]
            items.append(self.button("Add a run time", self.add_time_form, discord.ButtonStyle.primary, disabled=len(s["slots"]) >= 14))
        elif s["kind"] == "lockout":
            items.append(self.button("Pick the days and time", self.lockout_form_open, discord.ButtonStyle.primary))
        items.append(self.button("All schedules", self.to_times) if many else self.button("Add another schedule", self.add_schedule))
        items.append(self.button("Back to the sections", self.to_hub))
        await self.show(i, "\n".join(lines), *items)

    def change_opts(self, s: dict) -> list[Opt]:
        n = int(s.get("rosters") or 1)
        return [Opt("name", "Rename", f"now “{s['name']}”"),
                Opt("rosters", "Rosters per run", f"{n} now — the sheet advertises {n} × {self.size()} seats"),
                Opt("cadence", "Cadence", "when its sheet opens, nudges, locks and confirms"),
                Opt("behaviour", "Behaviour", "nudge, fill, DM on open, split — or the raid's"),
                Opt("active", "Resume" if not s.get("active", True) else "Pause", "nothing opens from it while paused"),
                Opt("remove", "Remove this schedule", "runs already open keep their times")]

    async def change_picked(self, i: discord.Interaction, values: list[str]) -> None:
        sid, what = self.sid, values[0]
        if what == "name":
            await self.open_form(i, Form("Rename the schedule", [Field("name", "Name", required=False, default=self.sched(sid)["name"], max_length=40,
                                                                         description="what officers and members read, e.g. Main night, Alt run")], self.renamed))
        elif what == "rosters":
            cur = int(self.sched(sid).get("rosters") or 1)
            opts = [Opt(str(n), f"{n} roster{'s' if n > 1 else ''}", f"{n * self.size()} seats on the sheet", default=n == cur) for n in range(1, MAX_SCHEDULE_ROSTERS + 1)]
            await self.show(i, f"**{self.sched(sid)['name']}** — how many rosters does a run expect? The sheet advertises that many × {self.size()} seats, "
                               "the health check measures against it, and the lock aims for it (as far as tanks and healers allow).",
                            self.select("Rosters per run", opts, self.rosters_picked), self.button("Back", self._to_sched))
        elif what == "cadence":
            await self.open_form(i, self.sched_cadence_form())
        elif what == "behaviour":
            await self.sched_behaviour(i)
        elif what == "active":
            snapshot = dict(self.draft)
            on = not self.sched(sid).get("active", True)
            self.entry_put(sid, active=None if on else False)
            await self.sched_checked(i, snapshot, sid, f"{self.sched(sid)['name']} {'resumed' if on else 'paused'}.")
        else:
            s = self.sched(sid)
            extra = " Its run times are the raid's own, so the raid is left with none." if sid == DEFAULT_SCHEDULE else ""
            await self.show(i, f"Remove **{s['name']}** ({self.reg.schedule_label(self.raid, s)})?{extra}\n-# Runs already open keep their times. Nothing is saved until Save.",
                            self.button("Remove", self.remove_sched, discord.ButtonStyle.danger), self.button("Back", self._to_sched))

    async def _to_sched(self, i: discord.Interaction) -> None:
        await self.schedule(i, self.sid)

    async def renamed(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        self.entry_put(self.sid, name=v.get("name", "").strip()[:40] or None)
        await self.sched_checked(i, snapshot, self.sid, f"Renamed to {self.sched(self.sid)['name']}.")

    async def rosters_picked(self, i: discord.Interaction, values: list[str]) -> None:
        snapshot, n = dict(self.draft), int(values[0])
        self.entry_put(self.sid, rosters=None if n == 1 else n)
        await self.sched_checked(i, snapshot, self.sid, f"{n} roster{'s' if n > 1 else ''} per run: the sheet advertises {n * self.size()} seats.")

    async def remove_sched(self, i: discord.Interaction) -> None:
        sid, name = self.sid, self.sched(self.sid)["name"]
        if sid == DEFAULT_SCHEDULE:
            self.put("slots", [])
        self.put("schedules", [r for r in copy.deepcopy(self.cur("schedules")) if r.get("id") != sid])
        self.sid = DEFAULT_SCHEDULE
        await self.times(i, f"Removed {name}.")

    def sched_cadence_form(self) -> Form:
        sd = schedule_def_of(self.sview(), self.sid)
        own, view = sd["schedule"]["own"], self.sview()
        fields = []
        for f, label, desc in (("signup_lead_hours", "The sheet opens", "before the run starts"), ("nudge_hours_before", "Nudge the unanswered", "between opening and the lock"),
                               ("lock_hours_before", "The roster locks", "before the run starts"), ("confirm_hours_before", "Confirm by", "no later than the lock"),
                               ("fill_ask_hours", "A fill ask expires after", "no answer counts as no")):
            suffix = "" if f == "fill_ask_hours" else " before"
            inherit = Opt("inherit", f"Same as the raid ({hours_words(view[f])}{suffix})", default=f not in own)
            fields.append(Field(f, label, [inherit] + [Opt(o.value, o.label, default=f in own and o.default) for o in hours_opts(own.get(f), suffix=suffix)][:MAX_OPTIONS - 1], description=desc))
        return Form(f"Cadence · {sd['schedule']['name']}"[:45], fields, self.sched_cadence_submitted)

    async def sched_cadence_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        self.entry_put(self.sid, **{f: (None if not v.get(f) or v[f] == "inherit" else schedule_value(f, v[f]))
                                    for f in ("signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours")})
        await self.sched_checked(i, snapshot, self.sid, "Cadence updated.")

    async def sched_behaviour(self, i: discord.Interaction, note: str = "") -> None:
        sd = schedule_def_of(self.sview(), self.sid)
        own, view = sd["schedule"]["own"], self.sview()
        lines = [note] if note else []
        adhoc = sd.get("class") == "adhoc"  # a pickup template inherits the ad hoc defaults, not the raid's cadence
        lines.append(f"**{sd['schedule']['name']}** · behaviour — pick one to change; " + ("“default” follows the ad hoc defaults (no reminder, no confirmations, first come)." if adhoc else "“the raid's” follows the raid's own setting."))
        base = {**view, **ADHOC_DEFAULTS} if adhoc else view
        inherit = "default" if adhoc else "the raid’s"
        lines += [f"• {w}: **{('on' if own[f] else 'off') if f in own else inherit + (' (on)' if base[f] else ' (off)')}**" for f, w in BOOL_WORDS.items()]
        # one select for every switch (a select per switch would need more rows than a message has): field=value
        opts = [Opt(f"{f}={v}", f"{w}: " + (f"{inherit} ({'on' if base[f] else 'off'})" if v == "inherit" else v))
                for f, w in BOOL_WORDS.items() for v in ("inherit", "on", "off")]
        items: list = [self.select("Change a switch", opts, self._switch_setter)]
        split = [Opt("inherit", f"Split: the raid's ({SPLIT_WORDS.get(view.get('split_policy') or 'balanced', ('?',))[0]})", default="split_policy" not in own)]
        split += [Opt(p, f"Split: {SPLIT_WORDS[p][0]}", SPLIT_WORDS[p][1], default=own.get("split_policy") == p) for p in SPLIT_POLICIES]
        items.append(self.select("How to split a full run", split, self._behaviour_setter("split_policy")))
        items.append(self.button("Back", self._to_sched))
        await self.show(i, "\n".join(lines), *items)

    async def _switch_setter(self, i: discord.Interaction, values: list[str]) -> None:
        field, v = values[0].split("=", 1)
        await self._behaviour_setter(field)(i, [v])

    def _behaviour_setter(self, field: str):
        async def pick(i: discord.Interaction, values: list[str]) -> None:
            v = values[0]
            val = None if v == "inherit" else (v == "on" if field in BOOL_WORDS else v)
            self.entry_put(self.sid, **{field: val})
            await self.sched_behaviour(i, f"{FIELD_WORDS.get(field, field)} → {'the raid’s' if val is None else ('on' if val is True else 'off' if val is False else SPLIT_WORDS[val][0])}.")
        return pick

    # ---- a new schedule: its kind, then one form
    async def add_schedule(self, i: discord.Interaction) -> None:
        opts = [Opt("weekly", "Weekly nights", "e.g. Saturdays 8:00 PM — its own cadence and rosters"),
                Opt("lockout", "Days of each lockout", f"e.g. day 1 of each {lockout_words(self.sview().get('lockout_days') or 7)} lockout"),
                Opt("pickup", "Pickup template", "never opens by itself; /raid open offers it with a day and time")]
        await self.show(i, "**Add a schedule** — what kind?", self.select("What kind of schedule?", opts, self.kind_picked), self.button("Back", self.to_times))

    async def kind_picked(self, i: discord.Interaction, values: list[str]) -> None:
        kind = values[0]
        name = Field("name", "Name", required=False, max_length=40, placeholder={"weekly": "Alt run", "lockout": "Reset night", "pickup": "Pickup"}[kind],
                     description="what officers and members read")
        if kind == "pickup":
            await self.open_form(i, Form("New pickup template", [name], self._new_submitted("pickup")))
            return
        if kind == "lockout":
            days = int(self.sview().get("lockout_days") or 7)
            await self.open_form(i, MultiForm("New lockout schedule", [
                name,
                Field("days", "Which day(s) of each lockout", [Opt(str(d), f"Day {d}", "the reset day" if d == 1 else None) for d in range(1, min(days, MAX_OPTIONS) + 1)],
                      description=f"day 1 = the day the {lockout_words(days)} lockout resets"),
                Field("hour", "Start time — hour", hour_opts(20), description=f"guild time ({self.reg.config.timezone}), 12-hour clock"),
                Field("minute", "Minutes", minute_opts(5, 0)),
            ], self._new_submitted("lockout"), multi={"days": days}))
            return
        await self.open_form(i, MultiForm("New weekly schedule", [
            name,
            Field("nights", "Nights", [Opt(d, n) for d, n in zip(DAYS, DAY_NAMES)], description="one or more nights, same start time"),
            Field("hour", "Start time — hour", hour_opts(20), description=f"guild time ({self.reg.config.timezone}), 12-hour clock"),
            Field("minute", "Minutes", minute_opts(5, 0)),
        ], self._new_submitted("weekly"), multi={"nights": 7}))

    def new_id(self, name: str, kind: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", (name or kind).lower()).strip("-")[:12] or kind
        taken = {s["id"] for s in self.scheds()} | {r.get("id") for r in self.cur("schedules")}
        sid, n = base, 2
        while sid in taken or sid == DEFAULT_SCHEDULE:
            sid, n = f"{base[:12]}-{n}", n + 1
        return sid

    def _new_submitted(self, kind: str):
        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            snapshot = dict(self.draft)
            name = (v.get("name") or "").strip()[:40]
            sid = self.new_id(name, kind)
            entry: dict = {"kind": kind, "name": name or None}
            try:
                if kind == "weekly":
                    h, m = int(v.get("hour") or 0), int(v.get("minute") or 0)
                    entry["slots"] = sorted(parse_slots([f"{n} {h:02d}:{m:02d}" for n in (v.get("nights") or "").split(",") if n]), key=slot_key)
                elif kind == "lockout":
                    entry["days"] = [int(d) for d in (v.get("days") or "").split(",") if d]
                    entry["time"] = f"{int(v.get('hour') or 0):02d}:{int(v.get('minute') or 0):02d}"
            except RegistryError as e:
                await self.refuse_here(i, str(e), self.add_schedule)
                return
            self.entry_put(sid, **entry)
            await self.sched_checked(i, snapshot, sid, f"Added **{self.sched(sid)['name']}**: {self.reg.schedule_label(self.raid, self.sched(sid))}.")
        return done

    # ---- a lockout schedule's days and time
    async def lockout_form_open(self, i: discord.Interaction) -> None:
        s = self.sched(self.sid)
        days = int(self.sview().get("lockout_days") or 7)
        h, m = (int(x) for x in (s.get("time") or "20:00").split(":"))
        await self.open_form(i, MultiForm(f"Days · {s['name']}"[:45], [
            Field("days", "Which day(s) of each lockout", [Opt(str(d), f"Day {d}", "the reset day" if d == 1 else None, default=d in (s.get("days") or []))
                                                             for d in range(1, min(days, MAX_OPTIONS) + 1)], description=f"day 1 = the day the {lockout_words(days)} lockout resets"),
            Field("hour", "Start time — hour", hour_opts(h), description=f"guild time ({self.reg.config.timezone}), 12-hour clock"),
            Field("minute", "Minutes", minute_opts(5, m)),
        ], self.lockout_submitted, multi={"days": days}))

    async def lockout_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        self.entry_put(self.sid, days=[int(d) for d in (v.get("days") or "").split(",") if d] or None,
                       time=f"{int(v.get('hour') or 0):02d}:{int(v.get('minute') or 0):02d}")
        await self.sched_checked(i, snapshot, self.sid, f"{self.sched(self.sid)['name']}: {self.reg.schedule_label(self.raid, self.sched(self.sid))}.")

    # ---- a weekly schedule's run times (the default schedule's are the raid's own `slots`)
    def sched_slots(self, sid: str) -> list[str]:
        return list(self.cur("slots")) if sid == DEFAULT_SCHEDULE else list(self.sched(sid).get("slots") or [])

    def put_slots(self, sid: str, slots: list[str]) -> None:
        if sid == DEFAULT_SCHEDULE:
            self.put("slots", slots)
        else:
            self.entry_put(sid, slots=slots)

    def _dropper(self, slot: str):
        async def drop(i: discord.Interaction) -> None:
            sid, snapshot = self.sid, dict(self.draft)
            self.put_slots(sid, [s for s in self.sched_slots(sid) if s != slot])
            await self.sched_checked(i, snapshot, sid, f"Dropped {self.reg.slot_label(slot)}.")
        return drop

    def time_form(self) -> MultiForm:
        a = self._attempt
        nights = set((a.get("nights") or "").split(","))
        return MultiForm("Add a run time", [
            Field("nights", "Nights", [Opt(d, n, default=d in nights) for d, n in zip(DAYS, DAY_NAMES)], description="one or more nights, same start time"),
            Field("hour", "Start time — hour", hour_opts(int(a.get("hour", 19))), description=f"guild time ({self.reg.config.timezone}), 12-hour clock"),
            Field("minute", "Minutes", minute_opts(5, int(a.get("minute", 0)))),
        ], self.time_added, multi={"nights": 7})

    async def add_time_form(self, i: discord.Interaction) -> None:
        await self.open_form(i, self.time_form())

    async def time_added(self, i: discord.Interaction, v: dict[str, str]) -> None:
        sid = self.sid
        nights = [n for n in (v.get("nights") or "").split(",") if n]
        h, m = int(v.get("hour") or 0), int(v.get("minute") or 0)
        new = [f"{n} {h:02d}:{m:02d}" for n in nights]
        cur = self.sched_slots(sid)
        try:
            slots = sorted(parse_slots(cur + new), key=slot_key)  # the registry's own check, on a machine-built list
        except RegistryError as e:
            self._attempt = dict(v)
            dup = [s for s in new if s in cur]
            await self.refuse_here(i, f"{', '.join(self.reg.slot_label(s) for s in dup)} is already a run time." if dup else str(e), self.add_time_form)
            return
        if not new:
            await self.schedule(i, sid)
            return
        snapshot = dict(self.draft)
        self.put_slots(sid, slots)
        await self.sched_checked(i, snapshot, sid, f"Added {', '.join(self.reg.slot_label(s) for s in new)}.")

    # ---- Signup cadence
    def cadence_words(self, lead, nudge, lock, confirm) -> str:
        return f"opens {hours_words(lead)} · nudge {hours_words(nudge)} · locks {hours_words(lock)} · confirm by {hours_words(confirm)} before"

    async def cadence(self, i: discord.Interaction) -> None:
        cur = [self.cur(f) for f in CADENCE]
        opts = [Opt("keep", "Keep current", self.cadence_words(*cur), default=True)]
        opts += [Opt(key, name, self.cadence_words(*hs)) for key, name, *hs in PRESETS]
        opts.append(Opt("custom", "Custom…", "pick each step yourself"))
        text = ("**Signup cadence**, counted back from each run's start:\n"
                f"• the sheet opens **{hours_words(cur[0])} before**\n• the unanswered are nudged **{hours_words(cur[1])} before**\n"
                f"• the roster locks **{hours_words(cur[2])} before**\n• rostered members confirm by **{hours_words(cur[3])} before**")
        await self.show(i, text, self.select("Pick a cadence", opts, self.cadence_picked), self.button("Back to the sections", self.to_hub))

    async def cadence_picked(self, i: discord.Interaction, values: list[str]) -> None:
        key = values[0]
        if key == "custom":
            await self.open_form(i, self.cadence_form())
            return
        if key == "keep":
            await self.hub(i)
            return
        _, name, *hs = next(p for p in PRESETS if p[0] == key)
        snapshot = dict(self.draft)
        for f, h in zip(CADENCE, hs):
            self.put(f, float(h))
        await self.checked(i, snapshot, self.custom_again, f"Signup cadence → **{name}**: {self.cadence_words(*hs)}.")

    def cadence_form(self) -> Form:
        a = self._attempt
        val = {f: float(a[f]) if f in a else self.cur(f) for f in CADENCE}
        return Form("Signup cadence", [
            Field("signup_lead_hours", "The sheet opens", hours_opts(val["signup_lead_hours"]), description="before the run starts"),
            Field("nudge_hours_before", "Nudge the unanswered", hours_opts(val["nudge_hours_before"]), description="between opening and the lock"),
            Field("lock_hours_before", "The roster locks", hours_opts(val["lock_hours_before"]), description="before the run starts"),
            Field("confirm_hours_before", "Confirm by", hours_opts(val["confirm_hours_before"]), description="no later than the lock"),
        ], self.cadence_submitted)

    async def custom_again(self, i: discord.Interaction) -> None:
        await self.open_form(i, self.cadence_form())

    async def cadence_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        self._attempt = {f: v[f] for f in CADENCE if v.get(f)}
        for f in CADENCE:
            if v.get(f):
                self.put(f, float(v[f]))
        await self.checked(i, snapshot, self.custom_again, "Signup cadence → " + self.cadence_words(*(self.cur(f) for f in CADENCE)) + ".")

    # ---- Group make-up (min first, then a max that starts at that min: min above max can't be picked)
    async def comp(self, i: discord.Interaction) -> None:
        opts = [Opt(r, ROLE_WORDS[r].capitalize(), f"{self.cur(f'{r}_min')}–{self.cur(f'{r}_max')} now") for r in ROLES]
        await self.show(i, f"**Group make-up** for a {self.size()}-player run: the fewest and the most of each role the solver seats.",
                        self.select("Pick a role", opts, self.comp_role), self.button("Back to the sections", self.to_hub))

    async def comp_role(self, i: discord.Interaction, values: list[str]) -> None:
        role = values[0]

        async def picked(it: discord.Interaction, lo: int) -> None:
            await self.comp_max(it, role, lo)
        await self.pick_number(i, f"**{ROLE_WORDS[role].capitalize()}** — the fewest a run seats (now {self.cur(f'{role}_min')}).",
                               f"Fewest {ROLE_WORDS[role]}", 0, self.size(), self.cur(f"{role}_min"), picked, self.comp)

    async def comp_max(self, i: discord.Interaction, role: str, lo: int) -> None:
        async def picked(it: discord.Interaction, hi: int) -> None:
            snapshot = dict(self.draft)
            self.put(f"{role}_min", lo)
            self.put(f"{role}_max", hi)
            await self.checked(it, snapshot, self.comp, f"{ROLE_WORDS[role].capitalize()} → {lo}–{hi}.")
        cur_hi = max(lo, self.cur(f"{role}_max"))
        await self.pick_number(i, f"**{ROLE_WORDS[role].capitalize()}**: at least **{lo}**. The most a run seats (now {self.cur(f'{role}_max')})?",
                               f"Most {ROLE_WORDS[role]}", lo, self.size(), cur_hi, picked, self.comp)

    async def pick_number(self, i: discord.Interaction, text: str, placeholder: str, lo: int, hi: int, current: int,
                          on_pick: Callable[[discord.Interaction, int], Awaitable[None]], back, page: int | None = None) -> None:
        nums = list(range(lo, hi + 1))
        if page is None:
            page = (nums.index(current) if current in nums else 0) // MAX_OPTIONS

        async def cb(it: discord.Interaction, vals: list[str]) -> None:
            await on_pick(it, int(vals[0]))

        async def goto(it: discord.Interaction, p: int) -> None:
            await self.pick_number(it, text, placeholder, lo, hi, current, on_pick, back, p)
        items, pageline = self.paged(placeholder, [Opt(str(n), str(n), default=n == current) for n in nums], cb, page, goto)
        await self.show(i, text + (f"\n{pageline}" if pageline else ""), *items, self.button("Back", back))

    # ---- Selection weights
    async def weights_form(self, i: discord.Interaction) -> None:
        fields = []
        for k in WEIGHTS:
            cur = int(self.cur(f"weight_{k}"))
            vals = sorted(set(range(7)) | {cur})
            fields.append(Field(k, WEIGHT_LABEL[k], [Opt(str(n), f"{n} · {WEIGHT_WORDS.get(n, 'even more')}", default=n == cur) for n in vals],
                                description=f"how much {WEIGHT_HINT[k]} counts when seats are short"))
        await self.open_form(i, Form("Selection weights", fields, self.weights_submitted))

    async def weights_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        for k in WEIGHTS:
            if v.get(k):
                self.put(f"weight_{k}", int(v[k]))
        await self.checked(i, snapshot, self.weights_form, "Weights → " + " · ".join(f"{WEIGHT_LABEL[k].lower()} {self.cur(f'weight_{k}')}" for k in WEIGHTS) + ".")

    # ---- Lockout & length
    async def length_form(self, i: discord.Interaction) -> None:
        days, hours = int(self.cur("lockout_days")), float(self.cur("duration_hours"))
        dvals = sorted(set(range(1, 15)) | {days})
        hvals = sorted({1 + x / 2 for x in range(11)} | {hours})
        await self.open_form(i, Form("Lockout & length", [
            Field("lockout_days", "Lockout", [Opt(str(d), lockout_words(d), default=d == days) for d in dvals][:MAX_OPTIONS], description="how long one lockout lasts"),
            Field("duration_hours", "Raid length", [Opt(num(h), length_words(h), default=h == hours) for h in hvals][:MAX_OPTIONS],
                  description="how long a run takes (the sheet closes after it)"),
        ], self.length_submitted))

    async def length_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        if v.get("lockout_days"):
            self.put("lockout_days", int(v["lockout_days"]))
        if v.get("duration_hours"):
            self.put("duration_hours", float(v["duration_hours"]))
        await self.checked(i, snapshot, self.length_form, f"Lockout {lockout_words(self.cur('lockout_days'))}, runs {length_words(self.cur('duration_hours'))}.")

    # ---- First lockout opens (a date often months away: day list + narrowing by week)
    async def first_open(self, i: discord.Interaction) -> None:
        v = self.cur("first_open")
        text = ("**First lockout opens** — the anchor lockout windows count from; no run opens before it.\n"
                + (f"Now: {both_clocks(self.reg, datetime.fromisoformat(v))}" if v else "Not set: lockouts roll from each run."))
        items = [self.button("Pick the date and time", self.fo_form, discord.ButtonStyle.primary)]
        if v:
            items.append(self.button("Clear it", self.fo_clear))
        items.append(self.button("Back to the sections", self.to_hub))
        await self.show(i, text, *items)

    def fo_form_obj(self) -> Form:
        v = self.cur("first_open")
        default = datetime.fromisoformat(v).astimezone(self.reg.tz) if v else None
        return Form("First lockout opens", when_fields(self.reg, day0=self._fo_day0, default=default, later=True), self.fo_submitted)

    async def fo_form(self, i: discord.Interaction) -> None:
        await self.open_form(i, self.fo_form_obj())

    async def fo_clear(self, i: discord.Interaction) -> None:
        self.put("first_open", "")
        await self.hub(i, "First lockout: cleared.")

    async def fo_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        if v.get("day") == "later":
            await self.show(i, "Which week is it in?",
                            self.select("Pick the week", week_opts(self.reg, self._fo_day0 + timedelta(days=MAX_OPTIONS - 1)), self.fo_week),
                            self.button("Back to the sections", self.to_hub))
            return
        dt, note = combine_when(self.reg, v)
        snapshot = dict(self.draft)
        self.put("first_open", dt.isoformat())
        await self.checked(i, snapshot, self.fo_form, "\n".join(x for x in (f"First lockout opens {both_clocks(self.reg, dt)}.", when_note(note, dt)) if x))

    async def fo_week(self, i: discord.Interaction, values: list[str]) -> None:
        self._fo_day0 = date.fromisoformat(values[0])
        await self.open_form(i, self.fo_form_obj())

    # ---- Behaviour
    async def behaviour(self, i: discord.Interaction, note: str = "") -> None:
        split, ask = self.cur("split_policy"), self.cur("fill_ask_hours")
        lines = [note] if note else []
        lines.append("**Behaviour** — press a switch to flip it.")
        lines += [f"• {w}: **{'on' if self.cur(f) else 'off'}**" for f, w in BOOL_WORDS.items()]
        lines.append(f"• When more join than one run seats: **{SPLIT_WORDS[split][0]}** — {SPLIT_WORDS[split][1]}")
        lines.append(f"• A fill ask with no answer counts as no after **{hours_words(ask)}**")
        split_opts = [Opt(p, SPLIT_WORDS[p][0], SPLIT_WORDS[p][1], default=p == split) for p in SPLIT_POLICIES]
        items: list = [self.select("How to split a full slot", split_opts, self.split_picked),
                       self.select("A fill ask expires after", hours_opts(ask, upto=48, suffix=""), self.ask_picked)]
        for f, w in BOOL_WORDS.items():
            items.append(self.button(f"{w}: {'on' if self.cur(f) else 'off'}", self._toggler(f),
                                     discord.ButtonStyle.success if self.cur(f) else discord.ButtonStyle.secondary))
        items.append(self.button("Back to the sections", self.to_hub))
        await self.show(i, "\n".join(lines), *items)

    def _toggler(self, field: str):
        async def flip(i: discord.Interaction) -> None:
            self.put(field, not self.cur(field))
            await self.behaviour(i, f"{BOOL_WORDS[field]} → {'on' if self.cur(field) else 'off'}.")
        return flip

    async def split_picked(self, i: discord.Interaction, values: list[str]) -> None:
        self.put("split_policy", values[0])
        await self.behaviour(i, f"Split → {SPLIT_WORDS[values[0]][0]}.")

    async def ask_picked(self, i: discord.Interaction, values: list[str]) -> None:
        self.put("fill_ask_hours", float(values[0]))
        await self.behaviour(i, f"A fill ask expires after {hours_words(values[0])}.")

    # ---- Notes
    async def notes_form(self, i: discord.Interaction) -> None:
        await self.open_form(i, Form("Raid notes", [
            Field("notes", "Notes", required=False, paragraph=True, default=self.cur("notes") or None, max_length=1000,
                  description="shown with the raid's rules; leave empty to clear"),
        ], self.notes_submitted))

    async def notes_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.put("notes", v.get("notes", ""))
        await self.hub(i, "Notes updated." if v.get("notes") else "Notes cleared.")

    # ---- Reset
    async def reset(self, i: discord.Interaction) -> None:
        has = self.raid in self.reg.config.raids
        name = self.reg.raid_def(self.raid).get("name", self.raid)
        text = (f"Reset **{name}** to the game's defaults? Every guild change for this raid goes — run times and schedules, cadence, make-up, weights, notes."
                + (f"\n-# Your {len(self.draft)} unsaved change(s) are dropped too." if self.draft else "")) if has else f"**{name}** is already on the game's defaults."
        await self.show(i, text, self.button("Reset", self.do_reset, discord.ButtonStyle.danger, disabled=not has),
                        self.button("Back to the sections", self.to_hub))

    async def do_reset(self, i: discord.Interaction) -> None:
        await self.working(i, "Resetting…")
        name = self.reg.raid_def(self.raid).get("name", self.raid)
        if self.raid not in self.reg.config.raids:  # re-read: someone may have reset it meanwhile
            await self.finish(i, f"**{name}** was already on the game's defaults; nothing changed.")
            return
        self.reg.clear_raid_override(self.raid, i.user.display_name)
        await self.finish(i, f"✅ **{name}** is back to the game's defaults.")

    # ---- Save: the diff as sentences, the next three runs from the DRAFT, then ONE write
    def diff_lines(self) -> list[str]:
        out = []
        for _, _, fields in SECTIONS:
            for f in fields:
                if f == "schedules" and f in self.draft:
                    out += self.schedule_diff()
                elif f in self.draft:
                    out.append(f"• {FIELD_WORDS[f]}: {self.say(f, self.stored(f))} → **{self.say(f, self.draft[f])}**")
        return out

    def schedule_diff(self) -> list[str]:
        """The Schedules draft as sentences: added, removed, and per schedule what changed (times, name, rosters, cadence)."""
        stored_rd, view = self.reg.raid_def(self.raid), self.sview()
        before = {s["id"]: s for s in schedules_of(stored_rd)}
        after = {s["id"]: s for s in schedules_of(view)}
        label = lambda s: self.reg.schedule_label(self.raid, s)  # noqa: E731

        def words(f: str, v) -> str:
            if v is None:
                return "the raid's"
            if f in BOOL_WORDS:
                return "on" if v else "off"
            if f == "split_policy":
                return SPLIT_WORDS.get(v, (v,))[0]
            return hours_words(v) + ("" if f == "fill_ask_hours" else " before")
        out = []
        for sid, s in after.items():
            b = before.get(sid)
            if b is None:
                out.append(f"• New schedule **{s['name']}**: {label(s)}")
                continue
            changes = []
            if label({**b, "slots": s["slots"]}) != label(s):  # the default's run times have their own line above
                changes.append(f"{label({**b, 'slots': s['slots']})} → **{label(s)}**")
            if b["name"] != s["name"]:
                changes.append(f"renamed **{s['name']}**")
            ob, oa = schedule_def_of(stored_rd, sid)["schedule"]["own"], schedule_def_of(view, sid)["schedule"]["own"]
            changes += [f"{FIELD_WORDS.get(f, f).lower()} {words(f, ob.get(f))} → **{words(f, oa.get(f))}**" for f in ob.keys() | oa.keys() if ob.get(f) != oa.get(f)]
            if changes:
                out.append(f"• {b['name']}: " + "; ".join(changes))
        out += [f"• Remove schedule **{b['name']}**" for sid, b in before.items() if sid not in after]
        return out

    async def review(self, i: discord.Interaction) -> None:
        if not self.draft:
            await self.hub(i)
            return
        eff, err = self.preview()
        if err:
            await self.show(i, f"❌ {err}\nYour changes are kept.", self.button("Back to the sections", self.to_hub, discord.ButtonStyle.primary),
                            self.button("Cancel", self.cancel))
            return
        name = eff.get("name", self.raid)
        scheds = [s for s in schedules_of(eff) if s["kind"] != "pickup" and s.get("active", True)]
        per = [(s, self.sched_runs(s)) for s in scheds]
        runs = [t for _s, ts in per for t in ts]
        lines = [f"Save these changes to **{name}**?", *self.diff_lines(), "",
                 "Next runs with these rules:" if runs else "-# No run times, so nothing will open."]
        if len(per) <= 1:
            lines += [f"• {both_clocks(self.reg, t)}" for t in runs]
        else:  # the next three runs of every schedule
            lines += [f"• {s['name']}: " + (" · ".join(self.reg.local12(t) for t in ts) or "none coming up") for s, ts in per]
        await self.show(i, "\n".join(lines), self.button("Save", self.save, discord.ButtonStyle.success),
                        self.button("Back to the sections", self.to_hub), self.button("Cancel", self.cancel))

    async def save(self, i: discord.Interaction) -> None:
        await self.working(i, "Saving…")
        diff = self.diff_lines()
        try:
            self.reg.set_raid_overrides(self.raid, dict(self.draft), i.user.display_name)  # one call, one commit, all or nothing
        except RegistryError as e:
            await self.show(i, f"❌ {e}\nNothing was saved; your changes are kept.", self.button("Back to the sections", self.to_hub, discord.ButtonStyle.primary),
                            self.button("Cancel", self.cancel))
            return
        self.draft = {}
        await self.finish(i, f"✅ Saved **{self.reg.raid_def(self.raid).get('name', self.raid)}**:\n" + "\n".join(diff))


@flow("raid_config")
async def open_raid_config(interaction: discord.Interaction, reg: Registry, *, raid: str | None = None, section: str | None = None) -> None:
    """`/gm config raid`, or another flow jumping straight to a raid's section (e.g. section="times")."""
    if not await _owner_or_refuse(interaction, reg):
        return
    await RaidConfigWizard(reg, interaction.user.id, raid).start(interaction, section)


# ---------------------------------------------------------------- aura facts

VALUE_WORDS = {"all": "Everyone", "physical": "Physical damage", "spell": "Spell damage", "mana": "Mana users", "melee": "Melee",
               "ranged": "Ranged", "healer": "Healers", "tank": "Tanks"}
STRENGTHS = (0.5, 1.0, 1.5, 2.0, 3.0)
POINTS = (1, 2, 3, 4, 5, 6, 8, 10)
STATUS_WORDS = {"confirmed": "tested in game", "reported": "someone reported it", "assumed": "our best guess"}
SCOPE_WORDS = {"party": "its party only", "raid": "the whole raid", "class": "its class", "self": "the caster only"}


def key_words(key: str) -> str:
    return key[5:] if key.startswith("spec:") else VALUE_WORDS.get(key, key)


class AuraWizard(OwnerWizard):
    """/gm config aura — a DRAFT HUB per buff (design §3.5 A-01): the menu shows the buff as it stands plus the draft line
    ('Scope party → raid · Who benefits: Healers 2 → 3 · 2 changes', configops.describe_ops(...).summary() — the same
    structure the site's review and MCP's preview print); a value select or the note modal only updates the draft (buff
    fields and its family's beneficiaries alike); ONE 'Save this buff' writes it through the structured core
    (configops.apply_ops: a buff = one set_buff_overrides write, its family one set_family_overrides write, one commit).
    'Another buff' / 'Done' with a dirty draft ask Save / Discard / Back. The buffs section rev is snapshotted at start
    and after every save; a save refused because someone else changed the auras offers 'Reload and keep my changes'
    (the draft is per-field deltas, so a re-base is a merge). Family ids are never typed: 'New family…' asks a name."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Aura facts", guard=_owner_guard(reg))
        self.bid: str | None = None
        self.saved: list[str] = []
        self._key: str | None = None
        self._cls: str | None = None
        self.buff_draft: dict = {}  # field → value ('family_new' = a new family's name)
        self.fam_draft: dict = {}  # 'value:<key>' → points ('' removes) — on the family the buff will be in
        self.new_fid: str | None = None  # a new family's id, fixed when it is named (family_slug), so a retry can't re-slug it
        self.rev = reg.config_rev("buffs")

    # ---- facts (as stored) and the draft over them
    def fam(self):
        b = self.reg.buff(self.bid)
        return b, self.reg.profile.families[b.family_id]

    def map_text(self, value: dict) -> str:
        return " · ".join(f"{key_words(k)} {num(v)}" for k, v in value.items()) or "nobody"

    def dirty(self) -> bool:
        return bool(self.buff_draft or self.fam_draft)

    def new_family_name(self) -> str | None:
        return self.buff_draft.get("family_new")

    def target_family(self) -> tuple[str | None, dict]:
        """(family id, its beneficiary map) the draft's benefit edits apply to: the drafted family, else the buff's own.
        'Stands alone' on a buff that shares a family targets the buff's OWN family (id = the buff's id) — the implicit
        family of one the profile builds from the buff's own value map — never the shared family it is leaving."""
        b, _ = self.fam()
        if self.new_family_name():
            return None, {}
        fid = self.buff_draft.get("family") or b.family_id
        if fid == "own":
            fid = b.id
        f = self.reg.profile.families.get(fid)
        if f is None:  # its own family doesn't exist yet: it starts from the buff's own map (Registry._family_field does the same)
            return fid, dict(b.value or {})
        return f.id, dict(f.value)

    def target_name(self) -> str:
        """The name of the family the benefit edits apply to (a new one's typed name, a stand-alone buff's own name)."""
        b, _ = self.fam()
        if self.new_family_name():
            return self.new_family_name()
        fid, _v = self.target_family()
        f = self.reg.profile.families.get(fid)
        return f.name if f is not None else b.short

    def drafted_map(self) -> dict:
        _fid, value = self.target_family()
        out = dict(value)
        for k, v in self.fam_draft.items():
            key = k[6:]
            if v in ("", None) or float(v) <= 0 and not key.startswith("spec:"):
                out.pop(key, None)
            else:
                out[key] = float(v)
        return out

    def changes(self) -> list[dict]:
        """The draft as typed changes (configops.ops_from_fields): a new family first (its id from its name), then its
        or the existing family's beneficiaries, then the buff."""
        out: list[dict] = []
        bf = {k: v for k, v in self.buff_draft.items() if k != "family_new"}
        name = self.new_family_name()
        if name:
            out.append({"kind": "family", "target": self.new_fid, "fields": {"name": name, **self.fam_draft}})
            bf["family"] = self.new_fid
        elif self.fam_draft:
            fid, _ = self.target_family()
            out.append({"kind": "family", "target": fid, "fields": dict(self.fam_draft)})
        if bf:
            out.append({"kind": "buff", "target": self.bid, "fields": bf})
        return out

    def described(self):
        from .configops import describe_ops, ops_from_changes

        return describe_ops(self.reg, ops_from_changes(self.reg, self.changes()))

    def draft_line(self) -> str:
        if not self.dirty():
            return ""
        try:
            return "✏️ Not saved yet: " + self.described().summary()
        except RegistryError as e:
            return f"✏️ Not saved yet — {e}"

    def put(self, field: str, value) -> None:
        """One buff field into the draft; a value equal to the stored one drops out of it (nothing to save)."""
        b, _ = self.fam()
        if field == "family_new":
            self.buff_draft.pop("family", None)
            self.buff_draft["family_new"] = value
            self.new_fid = self.reg.family_slug(value)
            self.fam_draft.clear()  # a new family starts with nobody benefiting
            return
        now = {"family": "own" if b.family_id == b.id else b.family_id, "strength": num(b.strength)}.get(field, getattr(b, field, None))
        if field == "family":
            self.buff_draft.pop("family_new", None)
            if self.fam_draft and value != self.buff_draft.get("family", now):
                self.fam_draft.clear()  # benefit edits belonged to the family the buff was going to be in
        if str(value) == str(now if now is not None else ""):
            self.buff_draft.pop(field, None)
        else:
            self.buff_draft[field] = value

    def drafted(self, field: str):
        b, _ = self.fam()
        if field in self.buff_draft:
            return self.buff_draft[field]
        return {"family": "own" if b.family_id == b.id else b.family_id, "strength": num(b.strength)}.get(field, getattr(b, field, None))

    # ---- which buff
    async def start(self, i: discord.Interaction, page: int = 0) -> None:
        buffs = self.reg.profile.buffs
        opts = [Opt(b.id, b.short, f"{b.scope} · {self.reg.profile.families[b.family_id].name} · {b.status}") for b in buffs]
        items, pageline = self.paged("Pick a buff", opts, self.buff_picked, page, self.start)
        counts = self.reg.aura_override_counts()
        extra = [self.button("Reset everything to game defaults", self.reset_all, discord.ButtonStyle.danger)] if counts["buffs"] or counts["families"] else []
        await self.show(i, "Which buff?" + (f"\n{pageline}" if pageline else ""), *items, *extra, self.button("Done", self.done))

    async def buff_picked(self, i: discord.Interaction, values: list[str]) -> None:
        self.bid = values[0]
        self.buff_draft, self.fam_draft = {}, {}
        await self.menu(i)

    async def done(self, i: discord.Interaction) -> None:
        if self.bid and self.dirty():
            await self.unsaved(i, then=self.done)
            return
        await self.finish(i, ("✅ " + "\n✅ ".join(self.saved)) if self.saved else "Closed — nothing was changed.")

    async def another(self, i: discord.Interaction) -> None:
        if self.dirty():
            await self.unsaved(i, then=self.start)
            return
        await self.start(i)

    async def unsaved(self, i: discord.Interaction, then) -> None:
        b, _ = self.fam()

        async def save_then(it: discord.Interaction) -> None:
            await self.save(it, after=then)

        async def discard_then(it: discord.Interaction) -> None:
            self.buff_draft, self.fam_draft = {}, {}
            await then(it)
        await self.show(i, f"**{b.short}** has changes that aren't saved:\n{self.draft_line()}", self.button("Save this buff", save_then, discord.ButtonStyle.success),
                        self.button("Discard them", discard_then, discord.ButtonStyle.danger), self.button("Back", self.back))

    # ---- the hub: the buff as it stands, the draft, what to change
    async def menu(self, i: discord.Interaction, note: str = "") -> None:
        b, fam = self.fam()
        shared = [x.short for x in self.reg.profile.buffs if x.family_id == b.family_id and x.id != b.id]
        lines = [note] if note else []
        lines += [f"**{b.short}** · reaches {SCOPE_WORDS.get(b.scope, b.scope)} · family **{fam.name}**"
                  + (f" (shared with {', '.join(shared)})" if shared else " (stands alone)") + f" · strength ×{num(b.strength)} · {b.status}",
                  f"Who benefits: {self.map_text(fam.value)}"]
        if b.note:
            lines.append(f"-# {b.note}")
        if self.dirty():
            lines.append(self.draft_line())
        scope, fid, strength, status, bnote = (self.drafted(f) for f in ("scope", "family", "strength", "status", "note"))
        fam_label = (f"{self.new_family_name()} (new)" if self.new_family_name() else
                     "stands alone" if fid in ("own", b.id) else self.reg.profile.families[fid].name if fid in self.reg.profile.families else str(fid))
        opts = [Opt("scope", "Scope", f"{scope} — {SCOPE_WORDS.get(scope, '')}"),
                Opt("family", "Stacking family", fam_label),
                Opt("strength", "Strength", f"×{strength} of the family's value"),
                Opt("status", "Status", f"{status} — {STATUS_WORDS.get(status, '')}"),
                Opt("benefits", "Who benefits", self.map_text(self.drafted_map())),
                Opt("note", "Note", (bnote or "none")[:100]),
                Opt("reset", "Reset to game defaults", "drop the guild's changes to this buff")]
        items = [self.select("What to change", opts, self.field_picked),
                 self.button("Save this buff", self.save_pressed, discord.ButtonStyle.success, disabled=not self.dirty())]
        if self.dirty():
            items.append(self.button("Discard changes", self.discard))
        items += [self.button("Another buff", self.another), self.button("Done", self.done)]
        await self.show(i, "\n".join(lines), *items)

    async def back(self, i: discord.Interaction) -> None:
        await self.menu(i)

    async def discard(self, i: discord.Interaction) -> None:
        self.buff_draft, self.fam_draft = {}, {}
        await self.menu(i, "Changes discarded — nothing was saved.")

    async def field_picked(self, i: discord.Interaction, values: list[str]) -> None:
        b, fam = self.fam()
        f = values[0]
        if f == "note":
            cur = self.drafted("note") or None
            await self.open_form(i, Form(f"Note · {b.short}"[:45], [Field("note", "Note", required=False, paragraph=True, default=cur, max_length=300,
                                                                          description="what we know and where it came from; empty clears it")], self.note_submitted))
            return
        if f == "benefits":
            await self.benefits(i)
            return
        if f == "reset":
            from .configops import describe_ops, ops_from_changes

            d = describe_ops(self.reg, ops_from_changes(self.reg, [{"kind": "aura_reset", "target": b.id}]))
            text = (f"Reset **{b.short}** to the game's defaults?" + (f"\n{d.as_discord()}" if d.lines else "")
                    + ("\n-# Your unsaved changes to it are dropped too." if self.dirty() else ""))
            await self.decide(i, text, do=self.do_reset_one, do_label="Reset", danger=True, back_label="Cancel")
            return
        cur = self.drafted(f)
        if f == "scope":
            scopes = ["party", "raid"] + ([b.scope] if b.scope not in ("party", "raid") else [])
            opts = [Opt(s, s.capitalize(), SCOPE_WORDS.get(s), default=s == cur) for s in scopes]
        elif f == "family":
            opts = [Opt("own", "Stands alone", "stacks with everything", default=cur in ("own", b.id))]
            for fid, fm in self.reg.profile.families.items():
                if fid == b.id:
                    continue
                members = [x.short for x in self.reg.profile.buffs if x.family_id == fid]
                opts.append(Opt(fid, fm.name, ("with " + ", ".join(members)) if members else "no buffs yet", default=fid == cur))
            opts = opts[:MAX_OPTIONS - 1] + [Opt("__new", "New family…", "name a new stacking family; its id is made from the name")]
        elif f == "strength":
            opts = [Opt(num(s), f"×{num(s)}", "the family's full value" if s == 1 else None, default=num(s) == str(cur))
                    for s in sorted(set(STRENGTHS) | {float(b.strength)})]
        else:
            opts = [Opt(s, s.capitalize(), STATUS_WORDS[s], default=s == cur) for s in self.reg.STATUSES]
        await self.value_screen(i, f, opts)

    async def value_screen(self, i: discord.Interaction, field: str, opts: list[Opt], page: int = 0) -> None:
        b, _ = self.fam()

        async def picked(it: discord.Interaction, vals: list[str]) -> None:
            await self.picked_value(it, field, vals[0])

        async def goto(it: discord.Interaction, p: int) -> None:
            await self.value_screen(it, field, opts, p)
        items, pageline = self.paged(f"New {field}", opts, picked, page, goto)
        await self.show(i, f"**{b.short}** — {field}?" + (f"\n{pageline}" if pageline else ""), *items, self.button("Back", self.back))

    async def picked_value(self, i: discord.Interaction, field: str, value: str) -> None:
        if field == "family" and value == "__new":
            await self.open_form(i, Form("A new stacking family", [Field("name", "Name", max_length=40, placeholder="e.g. Stamina buffs",
                                                                         description="the family's id is made from this name")], self.new_family_submitted))
            return
        self.put(field, value)
        await self.menu(i)

    async def new_family_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        name = (v.get("name") or "").strip()
        if not name:
            await self.menu(i, "A new family needs a name.")
            return
        self.put("family_new", name)
        await self.menu(i, f"**{name}** will be a new family ({self.new_fid}) once you save — set who benefits from it next.")

    async def note_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.put("note", (v.get("note") or "").strip())
        await self.menu(i)

    # ---- who benefits: one key at a time into the draft (value:<key>) — never the whole map
    async def benefits(self, i: discord.Interaction) -> None:
        fid, _value = self.target_family()
        cur = self.drafted_map()
        name = self.target_name()
        shared = [x.short for x in self.reg.profile.buffs if x.family_id == fid]
        keys = list(self.reg.VALUE_KEYS) + [k for k in cur if k.startswith("spec:")]
        opts = [Opt(k, key_words(k), f"{num(cur[k])} points" if k in cur else "no points", default=False) for k in keys][:MAX_OPTIONS - 1]
        opts.append(Opt("__spec", "A particular spec…", "pick a class, then the spec"))
        text = (f"**Who benefits** from the **{name}** family" + (f" (every buff in it: {', '.join(shared)})" if len(shared) > 1 else "")
                + f":\n{self.map_text(cur)}\n-# Points per beneficiary; the strongest matching key counts, a spec's own entry wins. Pick one to change — the others stay.")
        await self.show(i, text, self.select("Pick who", opts, self.key_picked), self.button("Back", self.back))

    async def key_picked(self, i: discord.Interaction, values: list[str]) -> None:
        if values[0] == "__spec":
            from .wizard_opts import classes

            await self.show(i, "Which class?", self.select("Pick a class", classes(self.reg), self.cls_picked), self.button("Back", self.benefits))
            return
        await self.points(i, values[0])

    async def cls_picked(self, i: discord.Interaction, values: list[str]) -> None:
        from .wizard_opts import specs_of

        self._cls = values[0]
        await self.show(i, f"Which {values[0]} spec?", self.select("Pick a spec", specs_of(self.reg, values[0]), self.spec_picked), self.button("Back", self.benefits))

    async def spec_picked(self, i: discord.Interaction, values: list[str]) -> None:
        await self.points(i, f"spec:{values[0]}")

    async def points(self, i: discord.Interaction, key: str) -> None:
        self._key = key
        cur = self.drafted_map().get(key)
        name = self.target_name()
        vals = sorted(set(POINTS) | ({float(cur)} if cur else set()))
        opts = [Opt("0", "Remove", "this key gets no points of its own", default=not cur)]
        opts += [Opt(num(p), f"{num(p)} point{'' if p == 1 else 's'}", default=cur is not None and float(cur) == float(p)) for p in vals]
        await self.show(i, f"**{key_words(key)}** — how much does the **{name}** family give them? (now {num(cur) + ' points' if cur else 'none'})",
                        self.select("How many points", opts, self.points_picked), self.button("Back", self.benefits))

    async def points_picked(self, i: discord.Interaction, values: list[str]) -> None:
        key, n = self._key, values[0]
        _fid, stored = self.target_family()
        want = n if float(n) > 0 else ""
        now = stored.get(key)
        if (want == "" and now is None) or (want != "" and now is not None and float(want) == float(now)):
            self.fam_draft.pop(f"value:{key}", None)  # back to what is stored: nothing to save for this key
        else:
            self.fam_draft[f"value:{key}"] = want
        await self.benefits(i)

    # ---- Save this buff: ONE batch through the structured core
    async def save_pressed(self, i: discord.Interaction) -> None:
        await self.save(i)

    async def save(self, i: discord.Interaction, after=None) -> None:
        from .configops import apply_ops, ops_from_changes
        from .registry import ConfigChanged

        b, _ = self.fam()
        try:
            ops = ops_from_changes(self.reg, self.changes())
        except RegistryError as e:
            await self.refuse(i, str(e), self.back, "Back to the buff")
            return
        await self.working(i, "Saving…")
        res = await apply_ops(self.reg, ops, by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None), via="discord",
                              revs={"config:buffs": self.rev})
        if res["changed"] and not res["applied"]:
            await self.show(i, f"❌ {ConfigChanged('buffs')}\nYour changes to **{b.short}** are kept:\n{self.draft_line()}",
                            self.button("Reload and keep my changes", self.rebase, discord.ButtonStyle.primary),
                            self.button("Discard my changes", self.discard), self.button("Cancel", self.cancel))
            return
        self.rev = self.reg.config_rev("buffs")
        if res["refused"]:
            self.saved += res["applied"]
            self.normalize()  # what did save drops out of the draft; what was refused stays for "Fix this"
            await self.refuse(i, "Not saved — " + "; ".join(r["line"] for r in res["results"] if not r["ok"]) + ("\n✅ " + "\n✅ ".join(res["applied"]) if res["applied"] else ""),
                              self.back, "Back to the buff")
            return
        self.saved += res["applied"]
        self.buff_draft, self.fam_draft = {}, {}
        if after is not None:
            await after(i)
            return
        await self.menu(i, "✅ " + "; ".join(res["applied"]))

    async def rebase(self, i: discord.Interaction) -> None:
        """Someone else saved the auras meanwhile: take the current state as the base and keep only the draft's deltas
        that still change something (per-field, never a whole-list overwrite)."""
        self.rev = self.reg.config_rev("buffs")
        self.normalize()
        await self.menu(i, "Reloaded — your changes are on top of the current facts. Save when ready.")

    def normalize(self) -> None:
        """Drop the draft's deltas that no longer change anything against what is stored now (a re-base, or what a partly
        refused save did write); a new family that now exists becomes a plain family pick."""
        if self.new_fid and self.new_fid in self.reg.profile.families and "family_new" in self.buff_draft:
            self.buff_draft.pop("family_new")
            self.buff_draft["family"] = self.new_fid
        fam_keep = dict(self.fam_draft)
        for f, v in list(self.buff_draft.items()):
            if f != "family_new":
                self.put(f, v)
        self.fam_draft = fam_keep
        if "family_new" in self.buff_draft:
            return
        _fid, stored = self.target_family()
        for k, v in list(self.fam_draft.items()):
            now = stored.get(k[6:])
            if (v == "" and now is None) or (v != "" and now is not None and float(v) == float(now)):
                self.fam_draft.pop(k)

    # ---- resets: the decide screen (THE confirm), one write each
    async def do_reset_one(self, i: discord.Interaction, _note: str | None) -> None:
        await self._reset(i, self.bid)

    async def reset_all(self, i: discord.Interaction) -> None:
        await self.decide(i, f"**Reset every aura to the game defaults?**\n{self.reg.aura_reset_sentence()}", do=self.do_reset_all, do_label="Reset everything",
                          danger=True, back_label="Cancel")

    async def do_reset_all(self, i: discord.Interaction, _note: str | None) -> None:
        await self._reset(i, None)

    async def _reset(self, i: discord.Interaction, target: str | None) -> None:
        from .configops import apply_ops, ops_from_changes

        res = await apply_ops(self.reg, ops_from_changes(self.reg, [{"kind": "aura_reset", "target": target}]), by=i.user.display_name, by_id=i.user.id,
                              bot=getattr(i, "client", None), via="discord", revs={"config:buffs": self.rev})
        self.rev = self.reg.config_rev("buffs")
        if res["refused"]:
            await self.refuse(i, "; ".join(r["line"] for r in res["results"] if not r["ok"]), self.back if target else self.start, "Back")
            return
        self.saved += res["applied"]
        self.buff_draft, self.fam_draft = {}, {}
        if target:
            await self.menu(i, "✅ " + "; ".join(res["applied"]))
        else:
            await self.start(i)


@flow("aura")
async def open_aura(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await _owner_or_refuse(interaction, reg):
        return
    await AuraWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- timezone

COMMON_ZONES = (("America/Los_Angeles", "Pacific"), ("America/Denver", "Mountain"), ("America/Chicago", "Central"),
                ("America/New_York", "Eastern"), ("Europe/London", "UK"))
TZ_REGIONS = ("America", "Europe", "Asia", "Australia", "Pacific", "Africa", "Atlantic", "Indian", "Antarctica", "Arctic")


def tz_cities(region: str) -> list[str]:
    return sorted(z for z in available_timezones() if z.startswith(region + "/"))


def city_words(zone: str) -> str:
    return zone.split("/", 1)[-1].replace("_", " ").replace("/", " · ")


class TimezoneWizard(OwnerWizard):
    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Guild timezone", guard=_owner_guard(reg))
        self.region: str | None = None
        self.rev = reg.config_rev("settings")  # the settings as this wizard showed them (a change meanwhile refuses the Save)

    async def start(self, i: discord.Interaction) -> None:
        cur = self.reg.config.timezone
        items = [self.button(f"{name} · {city_words(z)}", self._chooser(z), discord.ButtonStyle.primary if z == cur else discord.ButtonStyle.secondary)
                 for z, name in COMMON_ZONES]
        items += [self.button("Another zone…", self.regions), self.button("Cancel", self.cancel)]
        await self.show(i, f"Now **{cur}** — it's {fmt12(self.reg.now_local())} there. Every run time, lock and lockout is in this zone.", *items)

    def _chooser(self, zone: str):
        async def choose(i: discord.Interaction) -> None:
            await self.confirm(i, zone)
        return choose

    async def regions(self, i: discord.Interaction) -> None:
        opts = [Opt(r, r, f"{len(tz_cities(r))} zones") for r in TZ_REGIONS] + [Opt("UTC", "UTC", "no daylight saving")]
        await self.show(i, "Which part of the world?", self.select("Pick a region", opts, self.region_picked), self.button("Back", self.start))

    async def region_picked(self, i: discord.Interaction, values: list[str]) -> None:
        if values[0] == "UTC":
            await self.confirm(i, "UTC")
            return
        self.region = values[0]
        await self.cities(i, 0)

    async def cities(self, i: discord.Interaction, page: int) -> None:
        zones = tz_cities(self.region or "")
        opts = [Opt(z, city_words(z), fmt12(datetime.now(ZoneInfo(z)))) for z in zones]

        async def picked(it: discord.Interaction, vals: list[str]) -> None:
            await self.confirm(it, vals[0])
        items, pageline = self.paged("Pick a city", opts, picked, page, self.cities)
        await self.show(i, f"**{self.region}** — pick the city whose clock you keep ({len(zones)} zones, A–Z)." + (f"\n{pageline}" if pageline else ""),
                        *items, self.button("Other region", self.regions))

    async def confirm(self, i: discord.Interaction, zone: str) -> None:
        cur = self.reg.config.timezone
        if zone == cur:
            await self.show(i, f"The guild is already on **{zone}**.", self.button("Pick another", self.start), self.button("Cancel", self.cancel))
            return
        new = ZoneInfo(zone)
        lines = [f"Change the guild's timezone from **{cur}** to **{zone}**?",
                 f"• Now: **{fmt12(datetime.now(new))}** there (it's {fmt12(self.reg.now_local())} in {cur})."]
        from . import raidcycle as rc

        try:  # the same words plain text's `set timezone` shows (a shadow registry, nothing saved), plus Discord's own
            # <t:…:f> stamps so each reader sees the new and the old moment in their own locale (the clock rule)
            lines += [f"• {r['line']} · <t:{int(r['start'].timestamp())}:f> your time" + (f" (was <t:{int(r['was'].timestamp())}:f>)" if r["was"] else "")
                      for r in rc.timezone_preview_rows(self.reg, zone)]
        except RegistryError as e:
            await self.refuse(i, str(e), self.start, "Pick another")
            return
        lines.append("-# Run times keep their clock time (Tue 7:30 PM stays Tue 7:30 PM), so the real moment moves with the zone.")
        await self.show(i, "\n".join(lines), self._saver(zone), self.button("Pick another", self.start), self.button("Cancel", self.cancel))

    def _saver(self, zone: str) -> discord.ui.Button:
        async def save(i: discord.Interaction) -> None:
            await self.working(i, "Saving…")
            if self.reg.config.timezone == zone:
                await self.finish(i, f"The guild is already on **{zone}**; nothing changed.")
                return
            res = await _save_settings(self.reg, i, {"timezone": zone}, self.rev)  # THE structured core: config_lock, rev, one audit line
            if not res["applied"]:
                self.rev = self.reg.config_rev("settings")
                await self.refuse(i, "; ".join(r["line"] for r in res["results"] if not r["ok"]) or "Nothing was changed.", self.start, "Look again")
                return
            await self.finish(i, f"✅ Timezone **{zone}** — it's {fmt12(self.reg.now_local())} there.")
        return self.button("Save", save, discord.ButtonStyle.success)


async def _save_settings(reg: Registry, i: discord.Interaction, fields: dict, rev: str) -> dict:
    """A settings wizard's Save through configops.apply_ops (the web's and MCP's path): the write runs in a worker thread
    under config_lock against the settings rev the wizard snapshotted — never reg.config mutated on the loop."""
    from .configops import apply_ops, ops_from_fields

    ops = ops_from_fields(reg, "settings", None, fields)
    return await apply_ops(reg, ops, by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None), via="discord",
                           revs={"config:settings": rev})


@flow("timezone")
async def open_timezone(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await _owner_or_refuse(interaction, reg):
        return
    await TimezoneWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- about

ABOUT_MAX = 600


class AboutWizard(OwnerWizard):
    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="About the guild", guard=_owner_guard(reg))
        self.draft = {"text": reg.config.about or ""}
        self.rev = reg.config_rev("settings")

    def form(self) -> Form:
        return Form("About the guild", [Field("text", "About the guild", required=False, paragraph=True, default=self.draft["text"][:4000] or None,
                                              max_length=ABOUT_MAX, description=f"one paragraph for the static guide; at most {ABOUT_MAX} characters, empty clears it")],
                    self.submitted)

    async def start(self, i: discord.Interaction) -> None:
        await self.open_form(i, self.form())

    async def reopen(self, i: discord.Interaction) -> None:
        await self.open_form(i, self.form())

    async def submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        text = v.get("text", "")
        self.draft["text"] = text
        if len(text) > ABOUT_MAX:
            await self.refuse(i, f"That's {len(text)} characters; the about text holds {ABOUT_MAX}. Your text is kept — trim it.", self.reopen)
            return
        body = f"This becomes the about text:\n>>> {text}" if text else "The about text will be cleared."
        await self.show(i, body[:1900], self.button("Save", self.save, discord.ButtonStyle.success), self.button("Edit", self.reopen), self.button("Cancel", self.cancel))

    async def save(self, i: discord.Interaction) -> None:
        await self.working(i, "Saving…")
        text = self.draft["text"].strip()[:ABOUT_MAX]
        if text == (self.reg.config.about or ""):
            await self.finish(i, "The about text already reads that; nothing changed.")
            return
        res = await _save_settings(self.reg, i, {"about": text}, self.rev)
        if not res["applied"]:
            self.rev = self.reg.config_rev("settings")
            await self.refuse(i, ("; ".join(r["line"] for r in res["results"] if not r["ok"]) or "Nothing was changed.") + "\nYour text is kept.", self.reopen, "Edit")
            return
        await self.finish(i, "✅ About text saved." if self.reg.config.about else "✅ About text cleared.")


@flow("about")
async def open_about(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await _owner_or_refuse(interaction, reg):
        return
    await AboutWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- claiming a server (design §5.32)

CLAIMERS = "The Discord server owner (or someone with Manage Server, or the bot's host superuser) sets this server up."
DEFAULT_CLAIM_PROFILE = "forever"


def claim_refusal(i: discord.Interaction) -> str | None:
    """Why this person can't set up this server now (None = they can): it must be an unconfigured server the bot is
    in, and they its Discord owner or a holder of Manage Server. Re-checked at every press of the wizard."""
    guild = getattr(i, "guild", None)
    if not i.guild_id or guild is None:
        return "Run this in the server you want to set up (the bot must be in it)."
    registries = i.client.registries
    if registries.for_guild(i.guild_id) is not None:
        return "This server is already set up."
    from .registry import is_superuser

    manages = getattr(getattr(i.user, "guild_permissions", None), "manage_guild", False)
    if guild.owner_id != i.user.id and not manages and not is_superuser(i.user.id):
        return CLAIMERS
    return registries.claim_refusal(int(i.guild_id))


async def _claim_guard(i: discord.Interaction) -> str | None:
    return claim_refusal(i)


class GuildClaimWizard(Wizard):
    """Set up an unconfigured server: shows the server, the key the bot picked (fixed for good), the game version
    (Forever unless another is picked) and the owner (the caller, or the member given/picked). Nothing is written before
    Confirm, which hands off to the client's claim_guild (one folder, one commit, served at once)."""

    def __init__(self, presser_id: int, owner, profiles: list[tuple[str, str]]):
        super().__init__(None, presser_id, title="Set up this server", guard=_claim_guard)  # type: ignore[arg-type]
        names = [n for n, _ in profiles]
        self.profiles = profiles
        self.draft = {"profile": DEFAULT_CLAIM_PROFILE if DEFAULT_CLAIM_PROFILE in names else (names[0] if names else DEFAULT_CLAIM_PROFILE), "owner": owner}

    def _key(self, i: discord.Interaction) -> str:
        from .registry import guild_key_for

        mock = getattr(getattr(i.client, "ctx", None), "guild_key", None)
        return guild_key_for(i.guild.name, i.client.registries.taken_keys((mock,) if mock else ()))

    async def start(self, i: discord.Interaction) -> None:
        await self.screen(i)

    async def screen(self, i: discord.Interaction) -> None:
        owner = self.draft["owner"]
        label = dict(self.profiles).get(self.draft["profile"], self.draft["profile"])
        lines = [f"Server: **{discord.utils.escape_markdown(str(i.guild.name))}**",
                 f"Guild key: `{self._key(i)}` — picked from the server name; it never changes (links, the site and MCP use it)",
                 f"Game version: **{label}**",
                 f"Owner: <@{owner.id}> — the owner sets channels, raids and policy (`/gm config …` or the site's Config page)",
                 "-# Nothing is written until you press Confirm. The new guild starts empty: its own members, raids and settings."]
        opts = [Opt(n, lab, default=n == self.draft["profile"]) for n, lab in self.profiles]
        await self.show(i, "\n".join(lines), self.select("Game version", opts, self.picked_profile),
                        self.members("Someone else will own it…", self.picked_owner),
                        self.button("Confirm", self.confirm, discord.ButtonStyle.success), self.button("Cancel", self.cancel))

    async def picked_profile(self, i: discord.Interaction, values: list[str]) -> None:
        if values and values[0] in dict(self.profiles):
            self.draft["profile"] = values[0]
        await self.screen(i)

    async def picked_owner(self, i: discord.Interaction, users: list) -> None:
        if users:
            if getattr(users[0], "bot", False):
                await self.refuse(i, "A bot can't own a guild — pick a person.", self.screen, "Back")
                return
            self.draft["owner"] = users[0]
        await self.screen(i)

    async def confirm(self, i: discord.Interaction) -> None:
        await self.working(i, "Setting up…")
        try:
            reg = await i.client.claim_guild(i.guild, int(self.draft["owner"].id), self.draft["profile"], i.user)
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        import os

        web = os.environ.get("OIBOT_WEB_URL", "")
        where = f", or the Config page at {web}/app/config?g={reg.key}" if web.startswith("https") else ", or the site's Config page"
        await self.finish(i, f"✅ **{discord.utils.escape_markdown(str(i.guild.name))}** is set up (game version {reg.profile.label}). "
                             f"Next: `/gm config ops-channel`{where}.")


@flow("guild_claim")
async def open_guild_claim(interaction: discord.Interaction, reg: Registry | None = None, owner=None, **_seed) -> None:
    """`/gm config owner` in a server that isn't set up (the only command that works there)."""
    why = claim_refusal(interaction)
    if why:
        await interaction.response.send_message(why, ephemeral=True)
        return
    from pathlib import Path

    from .profiles import available_profiles

    profiles = available_profiles(Path(__file__).resolve().parents[2])
    if not profiles:
        await interaction.response.send_message("No game profile loads on this bot; nothing was set up.", ephemeral=True)
        return
    # the guild's owner defaults to the Discord server's owner (who runs the server), not necessarily whoever pressed —
    # so a superuser setting up someone else's server leaves it owned by them; the wizard can pick someone else
    default = owner
    gowner = getattr(interaction.guild, "owner_id", None)
    if default is None and gowner and gowner != interaction.user.id:
        from types import SimpleNamespace

        default = getattr(interaction.guild, "owner", None) or SimpleNamespace(id=int(gowner), bot=False)
    await GuildClaimWizard(interaction.user.id, default or interaction.user, profiles).start(interaction)


# ---------------------------------------------------------------- owner transfer (design §3.5 O-03, owner decision D50)

class OwnerTransferWizard(Wizard):
    """`/gm config owner [user]` in a set-up server: pick the person (Discord's member picker, or the command's `user`),
    then THE decide screen with Registry.owner_consequence, [Transfer ownership] [Back]. Refused before the screen and
    again at the press (Registry.owner_refusal): a bot, the current owner, yourself, someone not in the server.
    Owner-only presses (the owner or a host superuser, re-checked on every press); nobody is DMed (D50, the DM rule)."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Transfer ownership", guard=_owner_guard(reg))

    async def start(self, i: discord.Interaction, target=None) -> None:
        if target is not None:
            await self.picked(i, [target])
            return
        await self.pick(i)

    async def pick(self, i: discord.Interaction) -> None:
        cur = self.reg.config.owner_discord_id
        await self.show(i, (f"<@{cur}> owns **{self.reg.config.name}** now. " if cur else "") + "Who takes over? They must be a member of this server.",
                        self.members("Pick the new owner", self.picked), self.button("Cancel", self.cancel))

    async def picked(self, i: discord.Interaction, users: list) -> None:
        from . import guild_routing

        user = users[0]
        answer, member = await guild_routing.member_of(i.client, self.reg, user.id)
        if isinstance(user, discord.Member) and answer != "yes":  # the picker handed us a server member: that is a yes
            answer, member = "yes", user
        name = getattr(member or user, "display_name", None) or str(user.id)
        why = self.reg.owner_refusal(user.id, by_id=i.user.id, bot=bool(getattr(user, "bot", False)), membership=answer, name=name)
        if why:
            await self.refuse(i, why, self.pick, "Pick someone else")
            return
        self.draft.update(uid=user.id, name=name, bot=bool(getattr(user, "bot", False)), membership=answer)
        await self.decide(i, self.reg.owner_consequence(name, i.user.id), do=self.save, do_label="Transfer ownership", danger=True)

    async def save(self, i: discord.Interaction, _note: str | None) -> None:
        from . import discord_people

        d = self.draft
        try:
            line = await discord_people.transfer_owner(i.client, self.reg, d["uid"], i.user.display_name, by_id=i.user.id, name=d["name"],
                                                      is_bot=d["bot"], membership=d["membership"], via="discord")
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        await self.finish(i, f"✅ {line}. The Config page and `/gm config show` name them as the owner.")


@flow("owner_transfer")
async def open_owner_transfer(interaction: discord.Interaction, reg: Registry, *, user=None, **_seed) -> None:
    """`/gm config owner` in a server that is set up: the owner (or a host superuser) hands it over."""
    if not await _owner_or_refuse(interaction, reg):
        return
    await OwnerTransferWizard(reg, interaction.user.id).start(interaction, user)


__all__ = ["MultiForm", "RaidConfigWizard", "AuraWizard", "TimezoneWizard", "AboutWizard", "GuildClaimWizard", "OwnerTransferWizard", "open_raid_config", "open_aura",
           "open_timezone", "open_about", "open_guild_claim", "open_owner_transfer", "claim_refusal", "next_runs", "hours_words", "tz_cities"]
