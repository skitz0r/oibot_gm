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


# ---------------------------------------------------------------- Group make-up: the TARGETS block (design §3.5 RD-03 / RD-12)
# One screen, two tiers, the same words everywhere: BOUNDS ("at least / at most" the solver seats — the owner's, raid_set,
# the Group make-up section above) and TARGETS ("we aim for", what readiness measures — officers, comp_target). /gm config
# raid › Group make-up (owner) opens this block from its comp screen; /roster comp (officers) opens only this block. A Save
# goes through configops (ops_from_fields "comp" → authorize_ops by privilege → apply_ops = Registry.set_comp_targets, one
# write) — never the registry verb directly, so the officer/owner split lives in configops.

TARGET_MAX_PICK = 24  # selects hold 25 options: 0…24 (a target past that is a plain-text one)


def target_numbers(top: int, keep, room: int = MAX_OPTIONS, drop_low: bool = False) -> list[int]:
    """The numbers a target select offers: 0…top, plus the stored value `keep` when it lies outside (a max equal to the
    run size, a target set on the web or in plain text) so the form preselects it and a Submit never drops it; trimmed to
    `room` options from the high end (`drop_low`: from the low end — a max of 0 or 1 is the rarest pick), never `keep`."""
    vals = list(range(0, max(0, int(top)) + 1))
    k = None if keep in (None, "") else int(keep)
    if k is not None and k not in vals:
        vals.append(k)
        vals.sort()
    while len(vals) > room:
        cut = next(v for v in (vals if drop_low else reversed(vals)) if v != k)
        vals.remove(cut)
    return vals


class CompTargetsBlock:
    """The targets block, mixed into a Wizard that has `reg` and `raid`. `tdraft` = {slot: entry | None} not yet saved;
    `trev` = the raid section rev when the block opened (a change meanwhile refuses the Save); `targets_back` = where Back
    goes (the raid wizard's comp screen), None = the block is the whole wizard (/roster comp)."""

    targets_back = None

    def _tinit(self) -> None:
        if not hasattr(self, "tdraft"):
            self.tdraft: dict = {}
            self.trev = self.reg.config_rev(f"raids:{self.raid}")

    def t_current(self) -> dict:
        from .configops import _comp_current

        cur = _comp_current(self.reg, self.raid)
        for slot, t in self.tdraft.items():
            if t is None:
                cur.pop(slot, None)
            else:
                cur[slot] = {k: v for k, v in t.items() if v not in (None, "")}
        return cur

    def t_size(self) -> int:
        return int(self.reg.raid_def(self.raid).get("size") or DEFAULT_RAID_SIZE)

    def bounds_line(self) -> str:
        b = self.reg.role_bounds(self.raid, self.t_size())
        return "-# Bounds — what the solver seats (the owner's): " + " · ".join(f"{ROLE_WORDS[r]} {b[r]['min']}–{b[r]['max']}" for r in ROLES if r in b)

    async def targets(self, i: discord.Interaction, note: str = "") -> None:
        from .configops import _target_words

        self._tinit()
        rd = self.reg.raid_def(self.raid)
        cur = self.t_current()
        lines = [note] if note else []
        lines += [f"**Desired comp** · {rd.get('name', self.raid)} — what readiness aims for; officers set it.", self.bounds_line()]
        for slot in sorted(cur, key=lambda s: (s not in ("tank", "healer", "melee", "ranged"), s)):
            t = cur[slot]
            lines.append(f"• {self.reg.comp_slot_words(slot)}: {_target_words(t)}" + (f" — {t['note']}" if t.get("note") else " — (no reason given)"))
        if not cur:
            lines.append("No officer targets: readiness uses the derived ones (the bounds and the buff matrix).")
        n = len(self.tdraft)
        lines.append(f"✎ **{n} unsaved change{'' if n == 1 else 's'}** — nothing is saved until Save." if n else "-# Pick a slot to set its target. Nothing is saved until Save.")
        slots = [Opt(r, self.reg.comp_slot_words(r), "a role") for r in ("tank", "healer", "melee", "ranged")]
        slots += [Opt(c, self.reg.comp_slot_words(c), "a class") for c in self.reg.profile.classes]
        slots.append(Opt("__spec", "A particular spec…", "pick the class, then the spec"))
        items: list = [self.select("Set a target for…", slots[:MAX_OPTIONS], self.t_slot_picked)]
        if cur:
            items.append(self.select("Remove a target", [Opt(s, self.reg.comp_slot_words(s), _target_words(cur[s])) for s in sorted(cur)][:MAX_OPTIONS], self.t_removed))
        items.append(self.button("Save targets", self.t_save, discord.ButtonStyle.success, disabled=not n))
        if n:
            items.append(self.button("Discard changes", self.t_discard))
        items.append(self.button("Back", self.targets_back) if self.targets_back else self.button("Cancel", self.cancel))
        await self.show(i, "\n".join(lines), *items)

    async def t_slot_picked(self, i: discord.Interaction, values: list[str]) -> None:
        if values[0] == "__spec":
            from .wizard_opts import classes

            await self.show(i, "Which class?", self.select("Pick a class", classes(self.reg), self.t_class_picked), self.button("Back", self.t_back))
            return
        await self.t_form(i, values[0])

    async def t_class_picked(self, i: discord.Interaction, values: list[str]) -> None:
        from .wizard_opts import specs_of

        cls = values[0]

        async def spec_picked(it: discord.Interaction, vals: list[str]) -> None:
            await self.t_form(it, f"{cls}:{vals[0]}")
        await self.show(i, f"Which {cls} spec?", self.select("Pick a spec", specs_of(self.reg, cls), spec_picked), self.button("Back", self.t_back))

    async def t_back(self, i: discord.Interaction) -> None:
        await self.targets(i)

    async def t_form(self, i: discord.Interaction, slot: str) -> None:
        from .registry import COMP_NOTE_MAX

        cur = self.t_current().get(slot) or {}
        top = min(self.t_size(), TARGET_MAX_PICK)
        lo = int(cur.get("min") or 0)
        hi = cur.get("max")
        words = self.reg.comp_slot_words(slot)

        async def submitted(it: discord.Interaction, v: dict[str, str]) -> None:
            entry = {"min": int(v.get("min") or 0), "max": None if v.get("max") in (None, "", "none") else int(v["max"]), "note": (v.get("note") or "").strip() or None}
            raid = self.raid
            try:  # the registry's own rules on a throw-away copy (bounds, max ≥ min, the run size, the reason's cap)
                self.reg.preview(lambda sh: sh.set_comp_targets(raid, {slot: entry}, "preview"))
            except RegistryError as e:
                async def again(ix: discord.Interaction) -> None:
                    await self.t_form(ix, slot)
                await self.refuse(it, str(e), again)
                return
            self.tdraft[slot] = entry
            await self.targets(it, f"{words}: {'at least ' + str(entry['min'])}{', at most ' + str(entry['max']) if entry['max'] is not None else ''} — not saved yet.")
        await self.open_form(i, Form(f"Target · {words}"[:45], [
            Field("min", "We aim for at least", [Opt(str(n), str(n), default=n == lo) for n in target_numbers(top, lo)], description="what readiness counts as enough"),
            Field("max", "And at most", [Opt("none", "No upper limit", default=hi in (None, ""))]
                  + [Opt(str(n), str(n), default=hi not in (None, "") and n == int(hi)) for n in target_numbers(top, hi, MAX_OPTIONS - 1, drop_low=True)],
                  description="more than this shows as over"),
            Field("note", "Why", required=False, default=cur.get("note") or None, max_length=COMP_NOTE_MAX, placeholder="e.g. Windfury for every melee group",
                  description="shown on the readiness card (optional)"),
        ], submitted))

    async def t_removed(self, i: discord.Interaction, values: list[str]) -> None:
        from .configops import _comp_current

        slot = values[0]
        self._tinit()
        if self.tdraft.get(slot) is not None and slot not in _comp_current(self.reg, self.raid):
            self.tdraft.pop(slot)  # a target drafted here and never saved: just drop it
        else:
            self.tdraft[slot] = None
        await self.targets(i, f"{self.reg.comp_slot_words(slot)}: back to the derived target — not saved yet.")

    async def t_discard(self, i: discord.Interaction) -> None:
        self.tdraft = {}
        await self.targets(i, "Changes discarded.")

    async def t_save(self, i: discord.Interaction) -> None:
        from . import configops, discord_registry

        if not self.tdraft:
            await self.targets(i)
            return
        await self.working(i, "Saving…")
        role = "owner" if self.reg.is_owner_id(i.user.id) else ("officer" if discord_registry.is_officer(i, self.reg) else "member")
        try:
            ops = configops.ops_from_fields(self.reg, "comp", self.raid, dict(self.tdraft))
        except RegistryError as e:
            await self.refuse(i, str(e), self.t_back, "Back to the targets")
            return
        allowed, refused = configops.authorize_ops(self.reg, ops, i.user.id, role=role)
        res = await configops.apply_ops(self.reg, allowed, by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None), via="discord",
                                        revs={f"{configops.CONFIG_REV}raids:{self.raid}": self.trev})
        bad = [*refused, *(r["line"] for r in res["results"] if not r["ok"])]
        if not res["applied"]:
            self.trev = self.reg.config_rev(f"raids:{self.raid}")
            await self.refuse(i, "; ".join(bad) or "Nothing was changed.", self.t_back, "Back to the targets")
            return
        self.tdraft = {}
        self.trev = self.reg.config_rev(f"raids:{self.raid}")
        text = "✅ " + "\n✅ ".join(res["applied"]) + ("".join(f"\n⛔ {b}" for b in bad))
        if self.targets_back:
            await self.targets(i, text)
        else:
            await self.finish(i, text + "\n-# The Readiness page and the analytics card measure against these now.")


class RaidConfigWizard(CompTargetsBlock, OwnerWizard):
    def __init__(self, reg: Registry, owner_id: int, raid: str | None = None):
        super().__init__(reg, owner_id, title="Raid rules", guard=_owner_guard(reg))
        self.raid = raid
        self.draft: dict = {}
        self.sid = DEFAULT_SCHEDULE  # the schedule the Schedules section is on
        self._attempt: dict = {}  # values a section refused, prefilled when the person presses Fix this
        self._fo_day0: date = reg.now_local().date()
        self.targets_back = self.comp  # the Group make-up screen's targets block comes back here (CompTargetsBlock)

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
        paused = not s.get("active", True)
        cad = self.sched_cadence(sid)
        if cad:
            lines.append(cad)
        if s["kind"] != "pickup":
            runs = self.sched_runs(s)
            if paused:  # never "Next runs" on a paused schedule: it would contradict the pause (design §3.4 RD-10)
                lines.append(f"⏸ Paused — would next run {self.reg.local12(runs[0])} · nothing opens" if runs else "⏸ Paused · nothing opens")
            else:
                lines.append(("Next runs: " + " · ".join(self.reg.local12(t) for t in runs)) if runs else "-# No run comes up yet.")
        elif paused:
            lines.append("⏸ Paused · /raid open doesn't offer it")
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
                Opt("rosters", "Rosters per run", f"{n} now — the sheet advertises {n} × {self.size()} seats from the next run"),
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
            opts = [Opt(str(n), f"{n} roster{'s' if n > 1 else ''}", f"{n} × {self.size()} seats on the sheet", default=n == cur) for n in range(1, MAX_SCHEDULE_ROSTERS + 1)]
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
        from .raidcycle import rosters_copy

        snapshot, n = dict(self.draft), int(values[0])
        self.entry_put(self.sid, rosters=None if n == 1 else n)
        await self.sched_checked(i, snapshot, self.sid, f"{n} roster{'s' if n > 1 else ''} per run. {rosters_copy(self.reg, self.raid, self.sid, n)}")

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
        from .configops import _comp_current, _target_words

        opts = [Opt(r, ROLE_WORDS[r].capitalize(), f"{self.cur(f'{r}_min')}–{self.cur(f'{r}_max')} now") for r in ROLES]
        targets = _comp_current(self.reg, self.raid)
        lines = [f"**Group make-up** for a {self.size()}-player run.",
                 "**Bounds** (the solver; yours): the fewest and the most of each role a run seats — "
                 + " · ".join(f"{ROLE_WORDS[r]} {self.cur(f'{r}_min')}–{self.cur(f'{r}_max')}" for r in ROLES) + ". Saved with this wizard's Save.",
                 "**Targets** (readiness; officers): what the guild aims for — "
                 + ("; ".join(f"{self.reg.comp_slot_words(k)} {_target_words(t)}" for k, t in sorted(targets.items())) or "none of the officers' own, the derived ones apply")
                 + ". Saved on their own (officers set them with /roster comp too)."]
        await self.show(i, "\n".join(lines), self.select("Pick a role", opts, self.comp_role),
                        self.button("Desired comp (targets)…", self.targets, discord.ButtonStyle.primary),
                        self.button("Back to the sections", self.to_hub))

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


class CompTargetsWizard(CompTargetsBlock, OwnerWizard):
    """/roster comp (officers): only the targets block of the Group make-up screen, for one raid."""

    def __init__(self, reg: Registry, owner_id: int, raid: str | None = None):
        async def guard(i: discord.Interaction) -> str | None:
            from . import discord_registry

            return None if discord_registry.is_officer(i, reg) else "Officers only."
        super().__init__(reg, owner_id, title="Desired comp", guard=guard)
        self.raid = raid

    async def start(self, i: discord.Interaction) -> None:
        if self.raid not in self.reg.profile.raids:
            self.raid = None
        if self.raid is None and len(self.reg.profile.raids) == 1:
            self.raid = next(iter(self.reg.profile.raids))
        if self.raid is None:
            from .wizard_opts import raids

            async def picked(it: discord.Interaction, values: list[str]) -> None:
                self.raid = values[0]
                await self.targets(it)
            await self.show(i, "Which raid's desired comp?", self.select("Pick a raid", raids(self.reg), picked), self.button("Cancel", self.cancel))
            return
        await self.targets(i)


@flow("roster_comp")
async def open_roster_comp(interaction: discord.Interaction, reg: Registry, *, raid: str | None = None, **_seed) -> None:
    """/roster comp: officers set the comp targets (what readiness aims for). The bounds stay the owner's (/gm config raid)."""
    from . import discord_registry

    if not discord_registry.is_officer(interaction, reg):
        await interaction.response.send_message("Officers only (Manage Server or a configured officer role).", ephemeral=True)
        return
    await CompTargetsWizard(reg, interaction.user.id, raid).start(interaction)


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

        rs = _run_store(i, self.reg)
        try:  # the same words plain text's `set timezone` shows (a shadow registry, nothing saved), plus Discord's own
            # <t:…:f> stamps so each reader sees the new and the old moment in their own locale (the clock rule)
            lines += [f"• {r['line']} · <t:{int(r['start'].timestamp())}:f> your time" + (f" (was <t:{int(r['was'].timestamp())}:f>)" if r["was"] else "")
                      for r in rc.timezone_preview_rows(self.reg, zone)]
            live = self.reg.timezone_runs(zone, rs)  # the runs already open or locked whose clock reading changes
            lines += [f"⚠ {x}" for x in self.reg.timezone_change_effects(zone, rs=rs)]
        except RegistryError as e:
            await self.refuse(i, str(e), self.start, "Pick another")
            return
        lines.append("-# Run times keep their clock time (Tue 7:30 PM stays Tue 7:30 PM), so the real moment moves with the zone.")
        if not live:
            await self.show(i, "\n".join(lines), self._saver(zone), self.button("Pick another", self.start), self.button("Cancel", self.cancel))
            return
        # live runs: the owner picks what happens to them (the save carries it — a locked run is never changed unasked)
        items = [self._saver(zone, "keep", "Save · keep runs at their moment")]
        if any(r["movable"] for r in live):
            moving = [f"⚠ {x}" for x in self.reg.timezone_change_effects(zone, rs=rs, mode="move") if "moves with the clock" in x]
            lines += ["-# Or move the open runs with the clock:", *[f"-# {x}" for x in moving]]
            items.append(self._saver(zone, "move", "Save · move open runs with the clock"))
        await self.show(i, "\n".join(lines), *items, self.button("Pick another", self.start), self.button("Cancel", self.cancel))

    def _saver(self, zone: str, runs: str | None = None, label: str = "Save") -> discord.ui.Button:
        async def save(i: discord.Interaction) -> None:
            await self.working(i, "Saving…")
            if self.reg.config.timezone == zone:
                await self.finish(i, f"The guild is already on **{zone}**; nothing changed.")
                return
            fields = {"timezone": zone, **({"timezone_runs": runs} if runs else {})}
            res = await _save_settings(self.reg, i, fields, self.rev)  # THE structured core: config_lock, rev, one audit line
            if not res["applied"]:
                self.rev = self.reg.config_rev("settings")
                await self.refuse(i, "; ".join(r["line"] for r in res["results"] if not r["ok"]) or "Nothing was changed.", self.start, "Look again")
                return
            await self.finish(i, f"✅ Timezone **{zone}** — it's {fmt12(self.reg.now_local())} there." + "".join(f"\n{x}" for x in res["applied"] if "moved with the clock" in x))
        return self.button(label, save, discord.ButtonStyle.success)


def _run_store(i: discord.Interaction, reg: Registry):
    """The client's run store for this guild (the live runs), or None — the registry then reads it from the store."""
    raids = getattr(getattr(i, "client", None), "raids", None)
    try:
        return raids.store(reg) if raids is not None else None
    except Exception:  # noqa: BLE001
        return None


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


# ---------------------------------------------------------------- channels and officer roles (design §3.4 X3, §3.5 O-12 / O-13; §5.46)

def _text_channel_select(placeholder: str, on_pick) -> discord.ui.ChannelSelect:
    """Discord's own channel picker, text channels only: searchable, no 25-option ceiling, nothing to spell."""
    s = discord.ui.ChannelSelect(placeholder=placeholder[:150], channel_types=[discord.ChannelType.text], min_values=1, max_values=1)

    async def cb(interaction: discord.Interaction) -> None:
        await on_pick(interaction, list(s.values))
    s.callback = cb  # type: ignore[method-assign]
    return s


def _role_select(placeholder: str, on_pick, *, current: Sequence[int] = (), max_values: int = 10, min_values: int = 0) -> discord.ui.RoleSelect:
    """Discord's own role picker (the current roles preselected)."""
    defaults = [discord.SelectDefaultValue(id=int(r), type=discord.SelectDefaultValueType.role) for r in list(current)[:max_values]]
    s = discord.ui.RoleSelect(placeholder=placeholder[:150], min_values=min_values, max_values=max_values, default_values=defaults)

    async def cb(interaction: discord.Interaction) -> None:
        await on_pick(interaction, list(s.values))
    s.callback = cb  # type: ignore[method-assign]
    return s


def _results_text(res: dict) -> str:
    """apply_ops' result as the person reads it: one ✅ / ⛔ line per write, then '3 saved, 1 refused'."""
    from .configops import result_summary

    lines = [("✅ " if r["ok"] else "⛔ ") + (f"{r['label']}: " if r.get("label") and r["label"] != "Guild settings" else "") + str(r["line"]) for r in res.get("results", [])]
    return "\n".join(lines + [f"-# {result_summary(res)}"])


async def _news_webhook_line(client, reg: Registry) -> str:
    """After the news channel is set (D39): the webhook for the owner's news source (ephemeral, spoilered — a secret), or
    what to add so the bot can create it."""
    hook = getattr(client, "news_webhook", None)
    if hook is None:
        return ""
    try:
        w = await hook(reg)
    except Exception:  # noqa: BLE001 — the channel is set either way
        return ""
    return (f"\nWebhook for your news source: ||{w['url']}||\n-# {w['note']}" if w.get("url") else f"\n-# {w['note']}")


SAME = "same"  # ChannelSetWizard: the channel stays as it is (loot-channel log: alone)


class ChannelSetWizard(OwnerWizard):
    """THE one-screen confirm of `/gm config <kind>-channel channel: | clear:` (the eight shortcuts and `/gm config
    loot-channel`): the consequence from code (discord_pool.channel_consequences — what is posted, pinned, forgotten, falls
    back; 'I can't post in #x: missing …'), then [Save] [Back]. Save → SetupMixin.set_channel (its permission check, post
    first, its own ops line) under the settings rev the screen was drawn from (a change meanwhile refuses it). `log` (the
    loot channel only): the public loot log on/off, said on the same screen and saved with it (configops `set loot_log`)."""

    def __init__(self, reg: Registry, owner_id: int, kind: str, channel_id: int | None, *, log: bool | None = None):
        from .discord_pool import CHANNEL_LABELS

        super().__init__(reg, owner_id, title=f"{CHANNEL_LABELS.get(kind, (kind,))[0]} channel", guard=_owner_guard(reg))
        self.kind, self.cid, self.log = kind, channel_id, log
        self.rev = reg.config_rev("settings")

    def log_op(self):
        from .configops import ConfigOp

        if self.log is None or bool(self.reg.config.loot_log) == bool(self.log):
            return None
        return ConfigOp(op="set", path="loot_log", value="true" if self.log else "false")

    async def start(self, i: discord.Interaction) -> None:
        from . import perms
        from .configops import describe
        from .discord_pool import channel_consequences

        self.rev = self.reg.config_rev("settings")
        log_op = self.log_op()
        lines: list[str] = []
        if self.cid != SAME:
            lines = channel_consequences(self.reg, self.kind, self.cid, i.client)
        if self.cid not in (None, SAME):
            ch = i.client.get_channel(self.cid) if getattr(i.client, "get_channel", None) else None
            missing = perms.missing_for(ch, self.kind) if ch is not None else []
            if missing:  # set_channel would refuse it: say so now, nothing to confirm
                await self.finish(i, f"❌ {perms.cant_post(ch.name, missing)} Nothing was changed.")
                return
            if self.cid == getattr(self.reg.config, f"{self.kind}_channel_id", None):
                if log_op is None:
                    await self.finish(i, lines[0] if lines else "Nothing changes.")
                    return
                self.cid, lines = SAME, []  # the channel stays: only the log changes
        if self.cid == SAME and log_op is None:
            from .loot_ops import loot_log_state

            await self.finish(i, "Nothing changes. " + loot_log_state(self.reg)["text"])
            return
        head = ("Change the public loot log?" if self.cid == SAME else f"Use <#{self.cid}> for this?" if self.cid else "Stop using a channel for this?")
        if log_op is not None:
            lines = lines + [describe(self.reg, log_op)]
        clearing = self.cid is None
        await self.decide(i, "\n".join([head] + [f"• {x}" for x in lines]), do=self.save,
                          do_label="Clear it" if clearing else "Save", danger=clearing)

    async def save(self, i: discord.Interaction, _note: str | None) -> None:
        from .configops import apply_async

        out = []
        try:
            if self.cid != SAME:
                out.append(await i.client.set_channel(self.reg, self.kind, self.cid, i.user.display_name, by_id=i.user.id, rev=self.rev))
                self.rev = self.reg.config_rev("settings")  # this screen's own write: the log below goes on from it
            log_op = self.log_op()
            if log_op is not None:
                out.append(await apply_async(self.reg, log_op, i.user.display_name, True, bot=i.client, by_id=i.user.id, rev=self.rev, via="discord"))
        except (ValueError, RegistryError) as e:
            await self.finish(i, ("✅ " + " · ".join(out) + "\n" if out else "") + f"❌ {e}")
            return
        extra = await _news_webhook_line(i.client, self.reg) if self.kind == "news" and self.cid not in (None, SAME) else ""
        if self.kind == "loot":
            from .loot_ops import loot_log_state

            extra += "\n" + loot_log_state(self.reg)["text"]
        await self.finish(i, f"✅ {' · '.join(out)}{extra}")


@flow("channel_set")
async def open_channel_set(interaction: discord.Interaction, reg: Registry, *, kind: str, channel=None, clear: bool = False,
                           log: bool | None = None, **_seed) -> None:
    """The `/gm config <kind>-channel` commands: `channel` (Discord's picker) or `clear` — neither or both is refused with
    what to do; then the one-screen confirm. `log` (loot-channel): the public loot log on/off, alone or with a channel."""
    if not await _owner_or_refuse(interaction, reg):
        return
    if channel is not None and clear:
        await interaction.response.send_message("Pick a channel or tick clear — not both.", ephemeral=True)
        return
    if channel is None and not clear:
        if kind == "loot" and log is not None:
            await ChannelSetWizard(reg, interaction.user.id, kind, SAME, log=log).start(interaction)
            return
        if kind == "loot":
            from .loot_ops import loot_log_state

            await interaction.response.send_message(f"{loot_log_state(reg)['text']}\nPick a channel, tick clear, or set log. `/gm config channels` shows them all.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message("Pick a channel or tick clear. `/gm config channels` shows them all.", ephemeral=True)
        return
    await ChannelSetWizard(reg, interaction.user.id, kind, None if clear else int(channel.id), log=log).start(interaction)


class ChannelsWizard(OwnerWizard):
    """`/gm config channels`: a hub listing every bot channel (mentions; a ⚠ line for one the bot can't post in), 'Change
    which?' → Discord's channel picker or '— not set —' → a DRAFT (nothing written) → Review (describe_ops as Discord
    text + the channel consequences) → Save (configops.apply_ops: set_channel per channel — its permission check, its card
    posted first, its one ops line — under the settings rev the hub opened with; a change meanwhile refuses that line)."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Bot channels", guard=_owner_guard(reg))
        self.draft: dict[str, int | None] = {}
        self.rev = reg.config_rev("settings")

    def current(self, kind: str) -> int | None:
        from .discord_pool import CHANNEL_KINDS

        return getattr(self.reg.config, CHANNEL_KINDS[kind], None)

    def ops(self) -> list:
        from .configops import ops_from_fields

        return ops_from_fields(self.reg, "settings", None, {f"channel:{k}": v for k, v in self.draft.items()})

    async def start(self, i: discord.Interaction, note: str = "") -> None:
        from .discord_pool import CHANNEL_KINDS, CHANNEL_LABELS

        probs = i.client.channel_problems(self.reg) if getattr(i.client, "channel_problems", None) else {}
        rows = []
        for kind in CHANNEL_LABELS:
            cur = self.current(kind)
            label = CHANNEL_LABELS[kind][0]
            shown = f"<#{cur}>" if cur else "— not set —"
            if kind in self.draft:
                new = self.draft[kind]
                shown += f" → ✏️ **{f'<#{new}>' if new else '— not set —'}**"
            rows.append(f"**{label}** · {shown}" + (f"\n-# ⚠ {probs[kind]}" if kind in probs and kind not in self.draft else ""))
        n = len(self.draft)
        opts = [Opt(k, CHANNEL_LABELS[k][0], CHANNEL_LABELS[k][1][:100]) for k in CHANNEL_KINDS if k in CHANNEL_LABELS]
        items = [self.select("Change which?", opts, self.kind_picked)]
        items.append(self.button(f"Review {n} change{'s' if n != 1 else ''}", self.review, discord.ButtonStyle.primary, disabled=not n))
        if n:
            items.append(self.button("Discard edits", self.discard))
        items.append(self.button("Done" if not n else "Cancel", self.cancel))
        await self.show(i, "\n".join(rows) + (f"\n\n{note}" if note else "") + ("\n-# Not saved yet — Review shows what each change does." if n else ""), *items)

    async def discard(self, i: discord.Interaction) -> None:
        self.draft = {}
        await self.start(i, "Edits discarded.")

    async def kind_picked(self, i: discord.Interaction, values: list[str]) -> None:
        await self.pick(i, values[0])

    async def pick(self, i: discord.Interaction, kind: str) -> None:
        from .discord_pool import CHANNEL_LABELS

        label, hint = CHANNEL_LABELS[kind]
        cur = self.current(kind)

        async def picked(it: discord.Interaction, chans: list) -> None:
            await self.put(it, kind, int(chans[0].id) if chans else None)

        async def unset(it: discord.Interaction) -> None:
            await self.put(it, kind, None)

        await self.show(i, f"**{label}** — {hint}.\nNow: {f'<#{cur}>' if cur else '— not set —'}. Pick a text channel:",
                        _text_channel_select(f"{label} channel", picked), self.button("— not set —", unset, disabled=cur is None and kind not in self.draft),
                        self.button("Back", self.start))

    async def put(self, i: discord.Interaction, kind: str, cid: int | None) -> None:
        from . import perms
        from .discord_pool import CHANNEL_LABELS

        if cid == self.current(kind):
            self.draft.pop(kind, None)
        else:
            self.draft[kind] = cid
        note = ""
        if cid is not None:
            ch = i.client.get_channel(cid) if getattr(i.client, "get_channel", None) else None
            missing = perms.missing_for(ch, kind) if ch is not None else []
            if missing:
                note = f"⚠ {perms.cant_post(ch.name, missing)} Save would refuse it — fix the permission first, or pick another channel."
        await self.start(i, note or f"{CHANNEL_LABELS[kind][0]} drafted.")

    async def review(self, i: discord.Interaction) -> None:
        from .configops import describe_ops
        from .discord_pool import channel_consequences

        if not self.draft:
            await self.start(i)
            return
        try:
            described = describe_ops(self.reg, self.ops())
        except RegistryError as e:
            await self.refuse(i, str(e), self.start, "Back to edits")
            return
        cons: list[str] = []
        for kind, cid in self.draft.items():
            cons += [c for c in channel_consequences(self.reg, kind, cid, i.client) if c not in cons]
        body = described.model_copy(update={"consequences": []}).as_discord()  # the consequences are said below, per channel
        await self.show(i, body + ("\n" + "\n".join(f"⚠ {c}" for c in cons) if cons else ""),
                        self.button("Save", self.save, discord.ButtonStyle.success), self.button("Back to edits", self.start), self.button("Cancel", self.cancel))

    async def save(self, i: discord.Interaction) -> None:
        from .configops import apply_ops

        await self.working(i, "Saving… (cards are posted where a channel holds one)")
        res = await apply_ops(self.reg, self.ops(), by=i.user.display_name, by_id=i.user.id, bot=i.client, via="discord",
                              revs={"config:settings": self.rev})
        text = _results_text(res)
        if self.draft.get("news") and self.current("news") == self.draft["news"]:
            text += await _news_webhook_line(i.client, self.reg)
        self.draft = {k: v for k, v in self.draft.items() if v != self.current(k)}  # what didn't land stays drafted
        if res.get("refused"):
            self.rev = self.reg.config_rev("settings")
            await self.show(i, text, self.button("Back to edits", self.start, discord.ButtonStyle.primary), self.button("Done", self.cancel))
            return
        await self.finish(i, text)


@flow("channels")
async def open_channels(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await _owner_or_refuse(interaction, reg):
        return
    await ChannelsWizard(reg, interaction.user.id).start(interaction)


class OfficersWizard(OwnerWizard):
    """`/gm config officers`: the officer roles by mention, legacy names that never resolved (from an older config) each
    with "Map <name>…"; Discord's role picker (the current roles preselected) → a draft → Review (describe_ops +
    op_consequences: "No officer roles now: only the server owner and Manage Server count.") → Save (apply_ops; @everyone,
    a bot's or integration's managed role refused — SetupMixin.role_refusal)."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Officer roles", guard=_owner_guard(reg))
        self.want: list[int] | None = None  # the drafted whole list (None = untouched)
        self.mapped: dict[str, int] = {}  # legacy name -> the role it becomes
        self.rev = reg.config_rev("settings")

    def ids(self) -> list[int]:
        base = list(self.want) if self.want is not None else list(self.reg.config.officer_role_ids)
        return list(dict.fromkeys(base + list(self.mapped.values())))

    def ops(self) -> list:
        from .configops import ConfigOp, ops_from_fields

        ops = ops_from_fields(self.reg, "settings", None, {"officer_roles": self.ids()}) if (self.want is not None or self.mapped) else []
        return ops + [ConfigOp(op="role_remove", value=name) for name in self.mapped]  # the legacy name goes once it is mapped

    async def start(self, i: discord.Interaction, note: str = "") -> None:
        cur = self.reg.config.officer_role_ids
        pending = [n for n in self.reg.config.officer_roles_pending() if n not in self.mapped]
        lines = ["Now: " + (", ".join(f"<@&{r}>" for r in cur) if cur else "none — only the server owner and Manage Server count.")]
        if self.want is not None or self.mapped:
            lines.append("✏️ Drafted: " + (", ".join(f"<@&{r}>" for r in self.ids()) or "none"))
        if pending:
            lines.append("From an older config, by name (not found in the server): " + ", ".join(f"**{discord.utils.escape_markdown(n)}**" for n in pending)
                         + " — map each to a role.")
        items: list = [_role_select("Officer roles (pick every role that counts)", self.picked, current=self.ids())]
        for name in pending[:3]:
            items.append(self.button(f"Map {name[:40]}…", self._mapper(name)))
        dirty = self.want is not None or bool(self.mapped)
        items.append(self.button("Review", self.review, discord.ButtonStyle.primary, disabled=not dirty))
        items.append(self.button("Cancel" if dirty else "Done", self.cancel))
        await self.show(i, "\n".join(lines) + (f"\n\n{note}" if note else ""), *items)

    async def picked(self, i: discord.Interaction, roles: list) -> None:
        refusal = getattr(i.client, "role_refusal", None)
        for r in roles:
            why = refusal(self.reg, int(r.id)) if refusal is not None else None
            if why:
                await self.start(i, f"⚠ {why}")
                return
        self.want = [int(r.id) for r in roles]
        await self.start(i, "Drafted — Review shows what changes.")

    def _mapper(self, name: str):
        async def go(i: discord.Interaction) -> None:
            async def picked(it: discord.Interaction, roles: list) -> None:
                refusal = getattr(it.client, "role_refusal", None)
                why = refusal(self.reg, int(roles[0].id)) if roles and refusal is not None else None
                if why:
                    await self.start(it, f"⚠ {why}")
                    return
                if roles:
                    self.mapped[name] = int(roles[0].id)
                await self.start(it, f"{name} → <@&{roles[0].id}> drafted." if roles else "")
            await self.show(i, f"Which role is **{discord.utils.escape_markdown(name)}** now?", _role_select(f"The role for {name}"[:150], picked, max_values=1, min_values=1),
                            self.button("Back", self.start))
        return go

    async def review(self, i: discord.Interaction) -> None:
        from .configops import describe_ops

        ops = self.ops()
        if not ops:
            await self.start(i, "Nothing to change.")
            return
        described = describe_ops(self.reg, ops)
        await self.show(i, described.as_discord(), self.button("Save", self.save, discord.ButtonStyle.success), self.button("Back to edits", self.start),
                        self.button("Cancel", self.cancel))

    async def save(self, i: discord.Interaction) -> None:
        from .configops import apply_ops

        await self.working(i, "Saving…")
        res = await apply_ops(self.reg, self.ops(), by=i.user.display_name, by_id=i.user.id, bot=i.client, via="discord", revs={"config:settings": self.rev})
        text = _results_text(res)
        if res.get("refused"):
            self.rev = self.reg.config_rev("settings")
            await self.show(i, text, self.button("Back to edits", self.start, discord.ButtonStyle.primary), self.button("Done", self.cancel))
            return
        await self.finish(i, text)


@flow("officers")
async def open_officers(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    if not await _owner_or_refuse(interaction, reg):
        return
    if interaction.guild is not None:
        import asyncio as _asyncio

        await _asyncio.to_thread(reg.resolve_officer_roles, interaction.guild)  # legacy names → ids first (and the name cache)
    await OfficersWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- companion tokens (design §5.50; out/dual_interface_design.md §3.1 L08/S-03)

class CompanionsWizard(OwnerWizard):
    """`/gm config companion` (owner): the Companions hub — every active token as one line (label · presence · last seen)
    → **Issue…** (a label modal: prose, checked by Registry.issue_feed_token on a shadow) → Discord's member picker (the raid
    leader who installs it) → THE decide screen (feed.issue_consequence) → SetupMixin.issue_companion DMs the token to them
    ONCE (a credential they must install: an act under the DM rule); the owner only ever reads "sent to <name>".
    **Revoke…** → a select of the active tokens → decide (feed.revoke_consequence) → SetupMixin.revoke_companion.
    Owner-only presses, re-checked on every press. The site's Config › Companions is the same verbs."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Companions", guard=_owner_guard(reg))

    def rows(self, i: discord.Interaction) -> list[dict]:
        fn = getattr(i.client, "companion_rows", None)
        if fn is not None:
            return fn(self.reg)
        from .feed import token_rows

        return token_rows(self.reg, [])

    async def start(self, i: discord.Interaction, note: str = "") -> None:
        from .feed import token_line
        from .registry import FEED_TOKEN_MAX

        rows = self.rows(i)
        active = [r for r in rows if r["active"]]
        gone = len(rows) - len(active)
        lines = [token_line(r) for r in active] or ["No companion token yet."]
        text = ("The loot companion on a raid leader's PC streams drops and awards from their chat log. Each token feeds "
                f"**{discord.utils.escape_markdown(self.reg.config.name)}** only; a revoked one stops at its next message.\n\n" + "\n".join(lines)
                + (f"\n-# {gone} revoked — the site lists them" if gone else "") + (f"\n\n{note}" if note else ""))
        await self.show(i, text, self.button("Issue…", self.issue_form, discord.ButtonStyle.primary, disabled=len(active) >= FEED_TOKEN_MAX),
                        self.button("Revoke…", self.pick_revoke, discord.ButtonStyle.secondary, disabled=not active),
                        self.button("Done", self.done))

    async def done(self, i: discord.Interaction) -> None:
        await self.finish(i, "Done.")

    # ---- Issue: label → raid leader → decide → DM
    async def issue_form(self, i: discord.Interaction) -> None:
        from .registry import FEED_LABEL_MAX

        await self.open_form(i, Form("Issue a companion token", [Field("label", "Label: whose PC runs it", placeholder="Jon's PC", max_length=FEED_LABEL_MAX,
                                                                       description="a name for this token — you'll see it in the list and when revoking")],
                                     self.labelled))

    async def labelled(self, si: discord.Interaction, values: dict[str, str]) -> None:
        if si.user.id != self.owner_id or (await self.guard(si)):
            await si.response.send_message("Owner only.", ephemeral=True)
            return
        label = " ".join((values.get("label") or "").split())
        try:  # the verb's own checks (empty, too long, a label in use, the cap) on a throw-away copy: nothing written
            self.reg.preview(lambda sh: sh.issue_feed_token(label, "preview"))
        except RegistryError as e:
            await self.refuse(si, str(e), self.issue_form, "Pick another label")
            return
        self.draft["label"] = label
        await self.pick_leader(si)

    async def pick_leader(self, i: discord.Interaction) -> None:
        await self.show(i, f"Who installs “{discord.utils.escape_markdown(self.draft['label'])}”? The token is DMed to them once — you won't see it.",
                        self.members("Pick the raid leader", self.picked), self.button("Back", self.start))

    async def picked(self, i: discord.Interaction, users: list) -> None:
        from . import guild_routing
        from .feed import issue_consequence

        user = users[0]
        if getattr(user, "bot", False):
            await self.refuse(i, "A bot can't run the companion — pick the raid leader.", self.pick_leader, "Pick someone else")
            return
        answer, member = await guild_routing.member_of(i.client, self.reg, user.id)
        if isinstance(user, discord.Member):  # the picker handed us a member of this server
            answer, member = "yes", user
        if answer == "no":
            await self.refuse(i, f"{getattr(user, 'display_name', user.id)} isn't in this server.", self.pick_leader, "Pick someone else")
            return
        name = getattr(member or user, "display_name", None) or str(user.id)
        self.draft.update(user=member or user, name=name)
        await self.decide(i, issue_consequence(self.reg, self.draft["label"], name), do=self.issue, do_label="Issue and DM")

    async def issue(self, i: discord.Interaction, _note: str | None) -> None:
        try:
            res = await i.client.issue_companion(self.reg, self.draft["label"], i.user.display_name, by_id=i.user.id, via="discord", deliver_to=self.draft["user"])
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        await self.finish(i, f"✅ {res['message']}")

    # ---- Revoke: pick → decide
    async def pick_revoke(self, i: discord.Interaction) -> None:
        rows = [r for r in self.rows(i) if r["active"]]
        if not rows:
            await self.start(i, "No active token to revoke.")
            return
        opts = [Opt(r["id"], r["label"], description=r["state_words"] + (f" · last seen {r['last_seen_label']}" if r.get("last_seen_label") else "")) for r in rows]
        await self.show(i, "Which token stops working?", self.select("Pick the token to revoke", opts, self.revoke_picked), self.button("Back", self.start))

    async def revoke_picked(self, i: discord.Interaction, values: list[str]) -> None:
        row = next((r for r in self.rows(i) if r["id"] == values[0] and r["active"]), None)
        if row is None:
            await self.start(i, "That token was revoked meanwhile.")
            return
        self.draft["revoke"] = row["id"]
        await self.decide(i, row["revoke_consequence"], do=self.revoke, do_label="Revoke", danger=True)

    async def revoke(self, i: discord.Interaction, _note: str | None) -> None:
        try:
            line = await i.client.revoke_companion(self.reg, self.draft["revoke"], i.user.display_name, by_id=i.user.id, via="discord")
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        await self.finish(i, f"✅ {line}")


@flow("companion")
async def open_companion(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """`/gm config companion`: the owner's Companions hub (issue a token DMed to the raid leader, revoke)."""
    if not await _owner_or_refuse(interaction, reg):
        return
    await CompanionsWizard(reg, interaction.user.id).start(interaction)


__all__ = ["MultiForm", "RaidConfigWizard", "CompanionsWizard", "open_companion", "AuraWizard", "TimezoneWizard", "AboutWizard", "GuildClaimWizard", "OwnerTransferWizard", "open_raid_config", "open_aura",
           "open_timezone", "open_about", "open_guild_claim", "open_owner_transfer", "ChannelsWizard", "ChannelSetWizard", "OfficersWizard", "open_channels", "open_channel_set", "open_officers", "claim_refusal", "next_runs", "hours_words", "tz_cities"]
