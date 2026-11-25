"""The `/gm config` wizards: raid rules (officers, design D8), aura facts, the guild's timezone and the about blurb (owner).

`raid_config` is the flagship. It picks a raid, then a HUB of sections (schedules, the minimums, priority, lockout and
length, first opens, the split, notes, reset). Schedules hands the screen to the schedule wizard (flows_schedule, flow
`schedule`: boxes 13–17), whose list's Back returns here. Every other section but the minimums edits a working copy
(`self.draft`) and returns to the hub; nothing is written until Review changes shows box 19 (configops: ops_from_fields
"raid" → describe_ops(…).box, the Raids page's Save) and its Save runs apply_ops against the raid's rev — one write, all
or nothing. The minimums (CompTargetsBlock) are saved on their own, through configops' `comp` kind (Registry.set_minimums).

A section refuses a bad combination on the spot rather than at Save: `RaidConfigWizard.preview` applies the draft with
the registry's own validators on a throw-away copy (Registry.preview) — so the wizard never re-implements a rule.

No value here is typed except prose (notes, an aura note, the about blurb): hours are preset selects worded "5 days
before", run times are nights + hour + minutes, dates are day/hour/minute selects narrowed by week, and the timezone is a
region then a paged city list.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import discord

from .constants import hours_words  # noqa: F401 — moved to constants (registry's lift words); re-exported here
from .constants import PAGE_WORDS
from .presets import PRESETS  # noqa: F401 — (key, name, opens, nudge, lock, confirm), shared with plain text; re-exported here
from .registry import DEFAULT_RAID_SIZE, RAID_WEIGHT_DEFAULTS, SPLIT_POLICIES, Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, both_clocks, combine_when, flow, fmt12, week_opts, when_fields, when_note  # noqa: F401

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


OFFICERS_ONLY = "Officers only (Manage Server or a configured officer role)."


def _officer_guard(reg: Registry) -> Callable[[discord.Interaction], Awaitable[str | None]]:
    """Re-checked on every press: officers edit raids and schedules (design D8 / C17)."""
    async def guard(i: discord.Interaction) -> str | None:
        from .discord_registry import is_officer

        return None if is_officer(i, reg) else OFFICERS_ONLY
    return guard


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

WEIGHTS = tuple(RAID_WEIGHT_DEFAULTS)
WEIGHT_HINT = {"rank": "core > raider > trial", "main": "a main is picked over an alt", "sat_out": "benched in the last lockout",
               "signup_order": "an earlier signup", "regular": "being one of the schedule's regulars (on their regular character)"}  # the Raids page's WEIGHT_HINT, in words
WEIGHT_WORDS = {0: "doesn't count", 1: "a little", 2: "some", 3: "a lot", 4: "more", 5: "the most"}
SPLIT_WORDS = {"balanced": ("Balanced", "both runs equal in synergy, tanks, healers and seats"),
               "first": ("Raid one first", "roster 1 gets the best, roster 2 the rest"),
               "rotation": ("Rotation", "whoever sat out or was in the weaker run moves up")}
_RF = PAGE_WORDS["raid_fields"]
SECTIONS = [  # (key, label, fields it edits). Each schedule — its run times, lineup and sheet timing — is the schedule wizard's.
    ("times", "Schedules", ()),  # flows_schedule (flow `schedule`) takes the screen over; saved on its own boxes 13–17
    ("comp", "Minimums", ()),  # CompTargetsBlock: saved on its own (Registry.set_minimums), never in the hub's draft
    ("weights", PAGE_WORDS["schedule_fields"]["weights"], tuple(f"weight_{k}" for k in WEIGHTS)),
    ("length", f"{_RF['lockout_days']} and {_RF['duration_hours'].lower()}", ("lockout_days", "duration_hours")),
    ("first_open", _RF["first_open"], ("first_open",)),
    ("behaviour", PAGE_WORDS["schedule_fields"]["split_policy"], ("split_policy",)),
    ("notes", "Notes", ("notes",)),
    ("reset", "Reset to defaults", ()),
]


def length_words(h) -> str:
    h = float(h)
    whole = int(h)
    return f"{whole}½ hours" if h - whole == 0.5 else f"{num(h)} hour{'' if h == 1 else 's'}"


def lockout_words(d) -> str:
    d = int(d)
    return "a week" if d == 7 else ("2 weeks" if d == 14 else f"{d} day{'' if d == 1 else 's'}")


def field_value(eff: dict, field: str, tz: str):
    """A raid field's effective value, in the form the draft holds it."""
    if field == "duration_hours":
        return float(eff[field])
    if field == "lockout_days":
        return int(eff["lockout_days"])
    if field == "split_policy":
        return eff.get("split_policy") or "balanced"
    if field.startswith("weight_"):
        return int(eff["weights"][field[7:]])
    if field == "notes":
        return str(eff.get("notes") or "")
    if field == "first_open":
        fo = parse_first_open(eff.get("first_open"), tz)
        return fo.isoformat() if fo else ""
    raise KeyError(field)


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


# ---------------------------------------------------------------- Minimums: one number per role, class or spec (design D16)
# The fewest a run holds — the lock fills minimums first, readiness and the analytics card measure against them; no upper
# limit. /gm config raid › Minimums and /roster comp open this block (officers). A Save goes through configops
# (ops_from_fields "comp" → authorize_ops by privilege → apply_ops = Registry.set_minimums, one write).

TARGET_MAX_PICK = 24  # selects hold 25 options: 0…24 (a minimum past that is a plain-text one)


def target_numbers(top: int, keep, room: int = MAX_OPTIONS, drop_low: bool = False) -> list[int]:
    """The numbers a minimum select offers: 0…top, plus the stored value `keep` when it lies outside (one set on the web
    or in plain text) so the form preselects it and a Submit never drops it; trimmed to `room` options from the high end
    (`drop_low`: from the low end), never `keep`."""
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
    """The minimums block, mixed into a Wizard that has `reg` and `raid`. `tdraft` = {slot: {min, note} | None} not yet
    saved; `trev` = the raid section rev when the block opened (a change meanwhile refuses the Save); `targets_back` =
    where Back goes (the raid wizard's hub), None = the block is the whole wizard (/roster comp)."""

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

    def reads_line(self) -> str:
        """What a run reads now (Registry.minimums: the guild's own over the game's), from what is saved."""
        from .registry import slot_words

        mins = self.reg.minimums(self.raid)
        return ("-# A run needs at least " + ", ".join(slot_words(k, n) for k, n in mins.items()) + ".") if mins else "-# A run needs no minimum of any role yet."

    async def targets(self, i: discord.Interaction, note: str = "") -> None:
        from .configops import _target_words

        self._tinit()
        rd = self.reg.raid_def(self.raid)
        cur = self.t_current()
        lines = [note] if note else []
        lines += [f"**Minimums** · {rd.get('name', self.raid)} — the fewest of a role, class or spec a run should hold. The lock fills them first.",
                  self.reads_line()]
        for slot in sorted(cur, key=lambda s: (s not in ("tank", "healer", "melee", "ranged"), s)):
            t = cur[slot]
            lines.append(f"• {self.reg.comp_slot_words(slot)}: {_target_words(self.reg, self.raid, slot, t)}" + (f" — {t['note']}" if t.get("note") else ""))
        if not cur:
            lines.append("The guild has set no minimum of its own, so the game's minimums apply. Pick a role, class or spec below to set one.")
        n = len(self.tdraft)
        lines.append(f"✎ **{n} unsaved change{'' if n == 1 else 's'}** — nothing is saved until Save." if n else "-# Pick a role, class or spec to set its minimum. Nothing is saved until Save.")
        slots = [Opt(r, self.reg.comp_slot_words(r), "a role") for r in ("tank", "healer", "melee", "ranged")]
        slots += [Opt(c, self.reg.comp_slot_words(c), "a class") for c in self.reg.profile.classes]
        slots.append(Opt("__spec", "A particular spec…", "pick the class, then the spec"))
        items: list = [self.select("Set a minimum for…", slots[:MAX_OPTIONS], self.t_slot_picked)]
        if cur:
            items.append(self.select("Remove a minimum", [Opt(s, self.reg.comp_slot_words(s), _target_words(self.reg, self.raid, s, cur[s])) for s in sorted(cur)][:MAX_OPTIONS], self.t_removed))
        items.append(self.button("Save minimums", self.t_save, discord.ButtonStyle.success, disabled=not n))
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
        words = self.reg.comp_slot_words(slot)

        async def submitted(it: discord.Interaction, v: dict[str, str]) -> None:
            entry = {"min": int(v.get("min") or 0), "note": (v.get("note") or "").strip() or None}
            raid = self.raid
            if entry["min"] == 0:  # 0 = the guild's own goes (never saved as 0): drafted as the clear it is
                await self.t_removed(it, [slot])
                return
            try:  # the registry's own rules on a throw-away copy (the run size, the reason's cap)
                self.reg.preview(lambda sh: sh.set_minimums(raid, {slot: {k: v for k, v in entry.items() if v is not None}}, "preview"))
            except RegistryError as e:
                async def again(ix: discord.Interaction) -> None:
                    await self.t_form(ix, slot)
                await self.refuse(it, str(e), again)
                return
            self.tdraft[slot] = entry
            await self.targets(it, f"{words}: at least {entry['min']} — not saved yet.")
        await self.open_form(i, Form(f"Minimum · {words}"[:45], [
            Field("min", "At least", [Opt(str(n), str(n) if n else self.reg.minimum_words(self.raid, slot, None)[:100], default=n == lo)
                                      for n in target_numbers(top, lo)], description="the fewest a run should hold"),
            Field("note", "Why", required=False, default=cur.get("note") or None, max_length=COMP_NOTE_MAX, placeholder="e.g. Windfury for every melee group",
                  description="shown beside the minimum (optional)"),
        ], submitted))

    async def t_removed(self, i: discord.Interaction, values: list[str]) -> None:
        from .configops import _comp_current

        slot = values[0]
        self._tinit()
        if slot not in _comp_current(self.reg, self.raid):
            self.tdraft.pop(slot, None)  # none of the guild's own saved: a draft here is just dropped, nothing to clear
            await self.targets(i, f"{self.reg.comp_slot_words(slot)}: {self.reg.minimum_words(self.raid, slot, None)}.")
            return
        self.tdraft[slot] = None
        await self.targets(i, f"{self.reg.comp_slot_words(slot)}: {self.reg.cleared_words(self.raid, slot)} — not saved yet.")

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
            await self.refuse(i, str(e), self.t_back, "Back to the minimums")
            return
        allowed, refused = configops.authorize_ops(self.reg, ops, i.user.id, role=role)
        res = await configops.apply_ops(self.reg, allowed, by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None), via="discord",
                                        revs={f"{configops.CONFIG_REV}raids:{self.raid}": self.trev})
        bad = [*refused, *(r["line"] for r in res["results"] if not r["ok"])]
        if not res["applied"]:
            self.trev = self.reg.config_rev(f"raids:{self.raid}")
            await self.refuse(i, "; ".join(bad) or "Nothing was changed.", self.t_back, "Back to the minimums")
            return
        self.tdraft = {}
        self.trev = self.reg.config_rev(f"raids:{self.raid}")
        text = "✅ " + "\n✅ ".join(res["applied"]) + ("".join(f"\n⛔ {b}" for b in bad))
        if self.targets_back:
            await self.targets(i, text)
        else:
            await self.finish(i, text + "\n-# The next lock fills these first, and the Readiness page measures against them.")


class RaidConfigWizard(CompTargetsBlock, OwnerWizard):
    def __init__(self, reg: Registry, owner_id: int, raid: str | None = None):
        super().__init__(reg, owner_id, title="Raid rules", guard=_officer_guard(reg))
        self.raid = raid
        self.draft: dict = {}
        self.rev: str | None = None  # the raid's rev when its hub opened (the write's fallback)
        self._ops: list = []
        self._box_rev: str | None = None  # the raid's rev read with the box last shown: the Save carries it
        self._fp: str | None = None  # that box's fingerprint (apply_ops `expect`)
        self._reset_rev: str | None = None  # the Reset box's: raid_all:<rid> read with it, and its fingerprint
        self._reset_fp: str | None = None
        self._attempt: dict = {}  # values a section refused, prefilled when the person presses Fix this
        self._fo_day0: date = reg.now_local().date()
        self.targets_back = self.to_hub  # the Minimums section is the block itself (CompTargetsBlock); Back = the hub

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
        if field == "split_policy":
            return PAGE_WORDS["split"].get(v, v)
        if field.startswith("weight_"):
            return f"{v} ({WEIGHT_WORDS.get(int(v), '')})".replace(" ()", "")
        if field == "lockout_days":
            return lockout_words(v)
        if field == "duration_hours":
            return length_words(v)
        if field == "first_open":
            return self.reg.local12(datetime.fromisoformat(v)) if v else "no first lockout set — weekly runs start at the next run time; a day-of-lockout schedule needs one"
        if field == "notes":
            return f"“{v[:80]}{'…' if len(v) > 80 else ''}”" if v else "no notes"
        return str(v)

    def summary(self, key: str, eff: dict) -> str:
        tz = self.reg.config.timezone

        def fv(f):
            return field_value(eff, f, tz)
        if key == "times":
            from .registry import schedule_name

            scheds = [s for s in self.reg.all_schedules() if s["raid"] == self.raid]
            if scheds:
                return f"{len(scheds)} schedule{'s' if len(scheds) > 1 else ''}: " + ", ".join(schedule_name(s) for s in scheds)
            return "No schedule runs this raid yet. Add one here."
        if key == "comp":
            from .registry import slot_words

            mins = self.reg.minimums(self.raid)
            return ("at least " + ", ".join(slot_words(k, n) for k, n in mins.items())) if mins else "no minimum set"
        if key == "weights":
            return " · ".join(f"{PAGE_WORDS['priority'][k]} {fv(f'weight_{k}')}" for k in WEIGHTS)
        if key == "length":
            return f"lockout {lockout_words(fv('lockout_days'))} · {length_words(fv('duration_hours'))} long"
        if key == "first_open":
            return self.say("first_open", fv("first_open"))
        if key == "behaviour":
            split = SPLIT_WORDS.get(fv("split_policy"), ("?", ""))
            return f"{PAGE_WORDS['split'].get(fv('split_policy'), split[0])}: {split[1]}"
        if key == "notes":
            return self.say("notes", fv("notes"))
        return "drop this guild's changes for the raid" if self.reg.raid_override(self.raid) else "already on the game's defaults"

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
        lines.append(f"✎ **{n} unsaved change{'' if n == 1 else 's'}**. Nothing is saved until you review and save them." if n
                     else "Pick a section to change. Nothing is saved until you review and save it.")
        if err:
            lines.append(f"⚠️ {err}")
        if self.rev is None:
            self.rev = self.reg.config_rev(f"raids:{self.raid}")
        await self.show(i, "\n".join(lines),
                        self.select("Pick a section", opts, self.section_picked),
                        self.button("Review changes", self.review, discord.ButtonStyle.primary, disabled=not n),
                        self.button("Cancel", self.cancel))

    async def to_hub(self, i: discord.Interaction) -> None:
        await self.hub(i)

    async def section_picked(self, i: discord.Interaction, values: list[str]) -> None:
        await self.go(i, values[0])

    async def go(self, i: discord.Interaction, key: str) -> None:
        screens = {"times": self.times, "comp": self.targets, "weights": self.weights_form, "length": self.length_form,
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

    # ---- Schedules (the "times" section): the schedule wizard (flows_schedule, flow `schedule`) takes this screen over;
    # its list's Back returns to this raid's hub with the draft as it was.
    async def times(self, i: discord.Interaction) -> None:
        from . import flows_schedule  # noqa: F401 — registers the flow (it imports this module: never at the top)
        from .wizard import FLOWS

        draft, raid, reg = dict(self.draft), self.raid, self.reg

        async def back(it: discord.Interaction, message_id: int | None) -> None:
            w = RaidConfigWizard(reg, it.user.id, raid)
            w.draft, w.message_id = draft, message_id
            await w.hub(it)
        self._closed = True
        self.stop()
        await FLOWS["schedule"](i, reg, raid=raid, message_id=self.message_id, back=back)


    # ---- Selection weights
    async def weights_form(self, i: discord.Interaction) -> None:
        fields = []
        for k in WEIGHTS:
            cur = int(self.cur(f"weight_{k}"))
            vals = sorted(set(range(7)) | {cur})
            fields.append(Field(k, PAGE_WORDS["priority"][k], [Opt(str(n), f"{n} · {WEIGHT_WORDS.get(n, 'even more')}", default=n == cur) for n in vals],
                                description=f"how much {WEIGHT_HINT[k]} counts when seats are short"))
        await self.open_form(i, Form("Selection weights", fields, self.weights_submitted))

    async def weights_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        snapshot = dict(self.draft)
        for k in WEIGHTS:
            if v.get(k):
                self.put(f"weight_{k}", int(v[k]))
        await self.checked(i, snapshot, self.weights_form, f"{PAGE_WORDS['schedule_fields']['weights']}: " + ", ".join(f"{PAGE_WORDS['priority'][k]} {self.cur(f'weight_{k}')}" for k in WEIGHTS) + ".")

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

    # ---- How one night's groups are made (the split is the raid's default; a schedule may set its own — C6).
    # The switches and the fill-ask wait are each schedule's own (C7): Schedules › a schedule › Switches and split.
    async def behaviour(self, i: discord.Interaction, note: str = "") -> None:
        split = self.cur("split_policy")
        lines = [note] if note else []
        lines.append("**How one night's groups are made** — when more join than one run seats, the raid's default split. A schedule may set its own.")
        lines.append(f"• Now: **{SPLIT_WORDS[split][0]}** — {SPLIT_WORDS[split][1]}")
        lines.append("-# Each schedule's sheet timing and switches are under Schedules.")
        split_opts = [Opt(p, SPLIT_WORDS[p][0], SPLIT_WORDS[p][1], default=p == split) for p in SPLIT_POLICIES]
        await self.show(i, "\n".join(lines), self.select("How to split a full night", split_opts, self.split_picked),
                        self.button("Back to the sections", self.to_hub))

    async def split_picked(self, i: discord.Interaction, values: list[str]) -> None:
        self.put("split_policy", values[0])
        await self.behaviour(i, f"Split → {SPLIT_WORDS[values[0]][0]}.")

    # ---- Notes
    async def notes_form(self, i: discord.Interaction) -> None:
        await self.open_form(i, Form("Raid notes", [
            Field("notes", "Notes", required=False, paragraph=True, default=self.cur("notes") or None, max_length=1000,
                  description="shown with the raid's rules; leave empty to clear"),
        ], self.notes_submitted))

    async def notes_submitted(self, i: discord.Interaction, v: dict[str, str]) -> None:
        self.put("notes", v.get("notes", ""))
        await self.hub(i, "Notes updated." if v.get("notes") else "Notes cleared.")

    # ---- Reset: the same raid_reset op as the site and plain text, on the server's box (consequences.raid_reset_box,
    # checked against `raid_all:<rid>`: the raid AND its schedules), one audit line (configops.apply_ops)
    def reset_op(self):
        from .configops import ConfigOp

        return ConfigOp(op="raid_reset", target=self.raid)

    def reset_box(self) -> dict:
        """BLOCKING: the Reset box, its rev read with it under config_lock (the write carries the rev of the box last shown)."""
        from .consequences import raid_reset_box

        with self.reg.config_lock:
            self._reset_rev = self.reg.config_rev(f"raid_all:{self.raid}")
            box = raid_reset_box(self.reg, self.raid, unsaved=len(self.draft))
        self._reset_fp = box.get("fp")
        return box

    async def reset(self, i: discord.Interaction) -> None:
        import asyncio

        await self.working(i)
        box = await asyncio.to_thread(self.reset_box)
        await self.decide_box(i, box, do=self.do_reset, refresh=lambda: asyncio.to_thread(self.reset_box), back=self.to_hub)

    async def do_reset(self, i: discord.Interaction, _v: dict | None = None) -> None:
        import asyncio

        from . import configops
        from .consequences import raid_reset_box

        rid, fp, unsaved = self.raid, self._reset_fp, len(self.draft)

        def same_box() -> bool:  # under config_lock, in the write's step
            return raid_reset_box(self.reg, rid, unsaved=unsaved).get("fp") == fp
        name = self.reg.raid_def(self.raid).get("name", self.raid)
        res = await configops.apply_ops(self.reg, [self.reset_op()], by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None), via="discord",
                                        revs={f"{configops.CONFIG_REV}raid_all:{self.raid}": self._reset_rev}, expect=same_box)
        if res.get("stale_box"):  # it changed in the gap: the new box, nothing written
            box = await asyncio.to_thread(self.reset_box)
            await self.decide_box(i, box, do=self.do_reset, refresh=lambda: asyncio.to_thread(self.reset_box), back=self.to_hub,
                                  changed=box.get("verb") is not None)
            return
        if not res["applied"]:
            bad = "; ".join(r["line"] for r in res["results"] if not r["ok"]) or "Nothing was changed."
            await self.show(i, f"❌ {bad}", self.button("Back to the sections", self.to_hub, discord.ButtonStyle.primary), self.button("Cancel", self.cancel))
            return
        self.draft = {}
        await self.finish(i, f"✅ **{name}** is back to the game's defaults.")

    # ---- Save: the raid's draft as raid_set ops (configops, like the Raids page) on box 19, then ONE write
    def raid_ops(self) -> list:
        """The draft as raid_set ops: ops_from_fields for what it can say, and a clear of the notes or the first opening
        (a value it leaves out) as the op the Raids page's clear is."""
        from .configops import ConfigOp, ops_from_fields

        clears = [f for f in ("notes", "first_open") if f in self.draft and not self.draft[f]]
        ops = ops_from_fields(self.reg, "raid", self.raid, {f: v for f, v in self.draft.items() if f not in clears})
        return ops + [ConfigOp(op="raid_set", target=self.raid, field=f, value="") for f in clears if self.stored(f)]

    def box(self) -> dict:
        """BLOCKING: box 19 for the draft (describe_ops), or RegistryError with the line's refusal. The raid's rev is read
        with it under config_lock and kept with the ops: the write carries the rev of the box LAST SHOWN, so a box shown
        again after another officer's save goes through at its press."""
        from .configops import describe_ops

        with self.reg.config_lock:
            ops = self.raid_ops()
            if not ops:
                raise RegistryError("Nothing has changed yet, so there is nothing to save.")
            rev = self.reg.config_rev(f"raids:{self.raid}")
            d = describe_ops(self.reg, ops, by_id=self.owner_id)
        err = next((ln.error for ln in d.lines if ln.error), None) or (d.refused[0] if d.refused else None)
        if err or d.box is None:
            raise RegistryError(err or "Nothing has changed yet, so there is nothing to save.")
        self._ops, self._diff, self._box_rev, self._fp = ops, list(d.box.get("diff") or []), rev, d.box.get("fp")
        return d.box

    async def boxed(self) -> dict:
        """The box in a thread; a refusal as its box."""
        import asyncio

        from .consequences import refusal_box_for

        try:
            return await asyncio.to_thread(self.box)
        except RegistryError as e:
            return refusal_box_for(str(e))

    async def review(self, i: discord.Interaction) -> None:
        import asyncio

        if not self.draft:
            await self.hub(i)
            return
        await self.working(i)
        await self.decide_box(i, await self.boxed(), do=self.save, refresh=lambda: asyncio.to_thread(self.box), back=self.to_hub)

    async def save(self, i: discord.Interaction, _v: dict) -> None:
        import asyncio

        from .configops import CONFIG_REV, apply_ops, describe_ops

        ops, fp, by_id = list(self._ops), self._fp, self.owner_id

        def same_box() -> bool:  # under config_lock, in the write's step (the site's token apply)
            d = describe_ops(self.reg, ops, by_id=by_id)
            return d.box is not None and d.box.get("fp") == fp
        res = await apply_ops(self.reg, ops, by=i.user.display_name, by_id=i.user.id, bot=getattr(i, "client", None),
                              via="discord", revs={f"{CONFIG_REV}raids:{self.raid}": self._box_rev or self.rev}, expect=same_box)
        if res.get("stale_box"):  # it changed in the gap: the new box, nothing written
            box = await self.boxed()
            await self.decide_box(i, box, do=self.save, refresh=lambda: asyncio.to_thread(self.box), back=self.to_hub,
                                  changed=box.get("verb") is not None)
            return
        bad = [r["line"] for r in res["results"] if not r["ok"]]
        if bad or not res["applied"]:  # the draft is kept: Back to the sections shows it again
            await self.show(i, "❌ " + " ".join(bad or [res.get("error") or "Nothing was changed."]),
                            self.button("Back to the sections", self.to_hub, discord.ButtonStyle.primary), self.button("Cancel", self.cancel))
            return
        self.draft = {}
        name = self.reg.raid_def(self.raid).get("name", self.raid)
        await self.finish(i, "\n".join([f"✅ **{name}** is saved.", *getattr(self, "_diff", [])]))


class CompTargetsWizard(CompTargetsBlock, OwnerWizard):
    """/roster comp (officers): the raid wizard's Minimums block on its own, for one raid."""

    def __init__(self, reg: Registry, owner_id: int, raid: str | None = None):
        super().__init__(reg, owner_id, title="Minimums", guard=_officer_guard(reg))
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
            await self.show(i, "Which raid's minimums?", self.select("Pick a raid", raids(self.reg), picked), self.button("Cancel", self.cancel))
            return
        await self.targets(i)


@flow("roster_comp")
async def open_roster_comp(interaction: discord.Interaction, reg: Registry, *, raid: str | None = None, **_seed) -> None:
    """/roster comp: officers set a raid's minimums (the fewest of a role, class or spec a run should hold)."""
    from . import discord_registry

    if not discord_registry.is_officer(interaction, reg):
        await interaction.response.send_message(OFFICERS_ONLY, ephemeral=True)
        return
    await CompTargetsWizard(reg, interaction.user.id, raid).start(interaction)


@flow("raid_config")
async def open_raid_config(interaction: discord.Interaction, reg: Registry, *, raid: str | None = None, section: str | None = None) -> None:
    """`/gm config raid` (officers, D8), or another flow jumping straight to a raid's section (e.g. section="times")."""
    from . import discord_registry

    if not discord_registry.is_officer(interaction, reg):
        await interaction.response.send_message(OFFICERS_ONLY, ephemeral=True)
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
                Opt("note", "Note", (bnote or "no note — pick to add one")[:100]),
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
        items += [self.button("Another zone", self.regions), self.button("Cancel", self.cancel)]
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
                 f"Owner: <@{owner.id}> — the owner sets channels, auras and who may do what (`/gm config …` or the site's Config page)",
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
    op_consequences: "With no officer roles left, only the server owner and Manage Server count as officers.") → Save (apply_ops; @everyone,
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
            lines.append("✏️ Drafted: " + (", ".join(f"<@&{r}>" for r in self.ids()) or "no officer role — only the server owner and Manage Server will count"))
        if pending:
            lines.append("From an older config, by name (not found in the server): " + ", ".join(f"**{discord.utils.escape_markdown(n)}**" for n in pending)
                         + " — map each to a role.")
        items: list = [_role_select("Officer roles (pick every role that counts)", self.picked, current=self.ids())]
        for name in pending[:3]:
            items.append(self.button(f"Map {name[:40]}", self._mapper(name)))
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

        from .feed import listen_state

        rows = self.rows(i)
        active = [r for r in rows if r["active"]]
        gone = len(rows) - len(active)
        lines = [token_line(r) for r in active] or ["No companion token yet."]
        ls = listen_state(getattr(i.client, "feed", None))
        reach = f"\n-# {ls['words']}" if (active and (not ls["listening"] or ls["local_only"])) else ""  # off, or this machine only
        text = ("The loot companion on a raid leader's PC streams drops and awards from their chat log. Each token feeds "
                f"**{discord.utils.escape_markdown(self.reg.config.name)}** only; a revoked one stops at its next message.\n\n" + "\n".join(lines)
                + (f"\n-# {gone} revoked — the site lists them" if gone else "") + reach + (f"\n\n{note}" if note else ""))
        await self.show(i, text, self.button("Issue", self.issue_form, discord.ButtonStyle.primary, disabled=len(active) >= FEED_TOKEN_MAX),
                        self.button("Revoke", self.pick_revoke, discord.ButtonStyle.secondary, disabled=not active),
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


# ---------------------------------------------------------------- the server's guild in the armory (armory link §5.1)

class ArmoryGuildWizard(OwnerWizard):
    """`/gm config armory` (owner): link this server to its in-game guild. Unlinked: the command opens the name box at once
    (prose — validated by the lookup, never parsed) → the lookup in a thread behind `working` → THE decide screen
    (consequences.armory_guild_consequence: what was found, so a stranger's guild of the same name is seen first) →
    Registry.set_armory_guild. Linked: what is linked, with **Change…** (the same box) and **Unlink…** (decide with
    armory_guild_clear_consequence). Owner-only presses, re-checked on every press and on the box's submit; nothing is
    written before the press. The site's Config › Settings card and MCP set_armory_guild are the same verbs.
    The guild's auction house (auction prices §4.1, where auctions.available): the hub's line (market_words) with
    **Auction house…** → `working` → discord_auctions.house_choices in threads → one select ("Auction house N of M", what
    is listed and the reference items' lowest prices) → decide (market_consequence) → Registry.set_market with the rev
    the hub was shown at → the hub, the first read in the background; the current house picked again → said, nothing
    written. **Turn prices off…** → decide (market_clear_consequence) → clear_market. The site's market route is the same."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="The guild in the armory", guard=_owner_guard(reg))
        self.rev = reg.config_rev("settings")

    def form(self) -> Form:
        from .armory import GUILD_NAME_MAX

        return Form("The guild in the armory", [Field("name", "The guild's name in the game", max_length=GUILD_NAME_MAX,
                                                      description="as it is spelled in the game; the bot looks it up and shows what it finds")],
                    self.named)

    async def start(self, i: discord.Interaction) -> None:
        if self.reg.config.armory is None:
            await self.open_form(i, self.form())
            return
        await self.hub(i)

    async def hub(self, i: discord.Interaction, note: str = "") -> None:
        from . import auctions, prices

        esc = discord.utils.escape_markdown
        lines = [f"This server's guild in the armory: {esc(self.reg.armory_guild_words())}."]
        items = [self.button("Change", self.change, discord.ButtonStyle.primary), self.button("Unlink", self.unlink_decide)]
        if auctions.available(self.reg):  # the auction house: a version with auction houses, a linked guild (this hub)
            if self.reg.config.market is not None:
                lines.append(f"Prices come from the {esc(self.reg.market_words())}.")
                if status := prices.status_sentence(self.reg, True, stamp=True):
                    lines.append(status if status.endswith(".") else status + ".")  # "Prices as of …" has none; waiting / old do
            else:
                lines.append("No auction house is picked, so prices are off. Press Auction house… to pick the one the guild trades on.")
            items.append(self.button("Auction house", self.houses))
            if self.reg.config.market is not None:
                items.append(self.button("Turn prices off", self.prices_off_decide))
        await self.show(i, "\n".join(lines) + (f"\n\n{note}" if note else ""), *items, self.button("Done", self.done))

    # ---- the guild's auction house (auction prices §4.1): owner only, the same verbs as the Config page
    async def houses(self, i: discord.Interaction) -> None:
        """Read the faction's auction houses behind `working` (Blizzard, in threads), then one select."""
        from . import armory, discord_auctions, prices
        from .wizard import Opt

        await self.working(i, "Reading the guild's auction houses…")
        try:
            got = await discord_auctions.house_choices(i.client, self.reg)
        except armory.ArmoryError as e:
            await self.refuse(i, e.words, self.houses, "Try again")
            return
        except RegistryError as e:
            await self.refuse(i, str(e))
            return
        if not got:
            await self.refuse(i, prices.choices_words(got), self.houses, "Try again")
            return
        self.draft["choices"] = got
        opts = [Opt(str(n), f"Auction house {n} of {len(got)}", prices.choice_description(self.reg, c)) for n, c in enumerate(got, 1)]
        text = (f"Pick the auction house the guild trades on. Each one shows how much is listed, then the lowest price of "
                f"{prices.sample_names(self.reg, got)}. Match them against the game.")
        await self.show(i, text, self.select("Which auction house", opts, self.house_picked), self.button("Back", self.hub))

    async def house_picked(self, i: discord.Interaction, values: list[str]) -> None:
        from .consequences import market_consequence

        choices = self.draft.get("choices") or []
        n = int(values[0]) if values and values[0].isdigit() else 0
        if not 1 <= n <= len(choices):
            await self.houses(i)
            return
        c = choices[n - 1]
        old = self.reg.config.market
        if old is not None and (int(old.house_id), int(old.ruleset_id)) == (int(c.house.id), int(c.ruleset_id)):
            await self.hub(i, market_consequence(self.reg, c.house)[0])  # the current one: said so, nothing written
            return
        self.draft["choice"] = c
        text = discord.utils.escape_markdown(" ".join(market_consequence(self.reg, c.house)))
        await self.decide(i, text, do=self.pick_house, do_label="Pick this auction house")

    async def pick_house(self, i: discord.Interaction, _note: str | None) -> None:
        import asyncio

        from . import discord_auctions
        from . import ops as ops_mod

        c = self.draft["choice"]
        before = self.reg.config.market
        try:
            said = await asyncio.to_thread(lambda: self.reg.set_market(c.house, c.ruleset_id, i.user.display_name, by_id=i.user.id, rev=self.rev))
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        self.rev = self.reg.config_rev("settings")
        if self.reg.config.market is before:  # the house already picked: nothing was written, nothing to audit or read
            await self.hub(i, f"{said[:1].upper()}{said[1:]}.")
            return
        await ops_mod.audited(i.client.ops, self.reg, i.user.display_name, f"picked the {c.house.name} as the guild's auction house", via="discord", by_id=i.user.id)
        await discord_auctions.refresh_soon(i.client, self.reg)
        await self.hub(i, f"✅ {said[:1].upper()}{said[1:]}.")

    async def prices_off_decide(self, i: discord.Interaction) -> None:
        from .consequences import market_clear_consequence

        text = discord.utils.escape_markdown(" ".join(market_clear_consequence(self.reg)))
        await self.decide(i, text, do=self.prices_off, do_label="Turn prices off", danger=True)

    async def prices_off(self, i: discord.Interaction, _note: str | None) -> None:
        import asyncio

        from . import ops as ops_mod

        old = self.reg.config.market
        try:
            await asyncio.to_thread(lambda: self.reg.clear_market(i.user.display_name, by_id=i.user.id, rev=self.rev))
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        self.rev = self.reg.config_rev("settings")
        await ops_mod.audited(i.client.ops, self.reg, i.user.display_name, f"turned prices off (the auction house was the {old.name})", via="discord", by_id=i.user.id)
        await self.hub(i, "✅ Prices are off.")

    async def done(self, i: discord.Interaction) -> None:
        await self.finish(i, "Done.")

    async def change(self, i: discord.Interaction) -> None:
        await self.open_form(i, self.form())

    async def named(self, si: discord.Interaction, values: dict[str, str]) -> None:
        import asyncio

        from . import armory
        from .consequences import armory_guild_consequence

        if si.user.id != self.owner_id or (await self.guard(si)):
            await si.response.send_message("Owner only.", ephemeral=True)
            return
        name = " ".join((values.get("name") or "").split())
        await self.working(si, "Looking the guild up in the armory…")
        try:
            facts = await asyncio.to_thread(armory.find_guild, self.reg, name)
        except armory.ArmoryError as e:
            await self.refuse(si, e.words, self.change, "Try another name")
            return
        self.draft["facts"] = facts
        text = discord.utils.escape_markdown(" ".join(armory_guild_consequence(self.reg, facts)))
        await self.decide(si, text, do=self.link, do_label="Link this guild")

    async def link(self, i: discord.Interaction, _note: str | None) -> None:
        import asyncio

        from . import ops as ops_mod

        facts = self.draft["facts"]
        had_market = self.reg.config.market is not None
        try:
            await asyncio.to_thread(lambda: self.reg.set_armory_guild(facts, i.user.display_name, by_id=i.user.id, rev=self.rev))
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        tail = "; prices turned off" if had_market and self.reg.config.market is None else ""  # the auction house went with the link (C11)
        await ops_mod.audited(i.client.ops, self.reg, i.user.display_name,
                              f"linked the server to the armory guild {facts.name} ({facts.faction}, id {facts.id}){tail}", via="discord", by_id=i.user.id)
        await self.finish(i, f"✅ Linked to **{discord.utils.escape_markdown(facts.name)}** in the armory.")

    async def unlink_decide(self, i: discord.Interaction) -> None:
        from .consequences import armory_guild_clear_consequence

        text = discord.utils.escape_markdown(" ".join(armory_guild_clear_consequence(self.reg)))
        await self.decide(i, text, do=self.unlink, do_label="Unlink", danger=True)

    async def unlink(self, i: discord.Interaction, _note: str | None) -> None:
        import asyncio

        from . import ops as ops_mod

        old = self.reg.config.armory
        had_market = self.reg.config.market is not None
        try:
            await asyncio.to_thread(lambda: self.reg.clear_armory_guild(i.user.display_name, by_id=i.user.id, rev=self.rev))
        except RegistryError as e:
            await self.finish(i, f"❌ {e}")
            return
        tail = "; prices turned off" if had_market and self.reg.config.market is None else ""  # the auction house went with the link (C11)
        await ops_mod.audited(i.client.ops, self.reg, i.user.display_name, f"unlinked the armory guild {old.name} (id {old.guild_id}){tail}", via="discord", by_id=i.user.id)
        await self.finish(i, f"✅ {discord.utils.escape_markdown(old.name)} is no longer linked.")


@flow("armory_guild")
async def open_armory_guild(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """`/gm config armory`: the owner links the server to its guild in the game (or changes / unlinks it)."""
    from . import armory

    if not await _owner_or_refuse(interaction, reg):
        return
    if not armory.available(reg):
        await interaction.response.send_message(armory.NO_ARMORY_WORDS if not reg.profile.armory else armory.ACCESS_WORDS, ephemeral=True)
        return
    await ArmoryGuildWizard(reg, interaction.user.id).start(interaction)


__all__ = ["MultiForm", "ArmoryGuildWizard", "open_armory_guild", "RaidConfigWizard", "CompanionsWizard", "open_companion", "AuraWizard", "TimezoneWizard", "AboutWizard", "GuildClaimWizard", "OwnerTransferWizard", "open_raid_config", "open_aura",
           "open_timezone", "open_about", "open_guild_claim", "open_owner_transfer", "ChannelsWizard", "ChannelSetWizard", "OfficersWizard", "open_channels", "open_channel_set", "open_officers", "claim_refusal", "next_runs", "hours_words", "tz_cities"]
