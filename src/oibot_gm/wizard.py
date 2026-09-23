"""Wizards: the Discord flows that used to take typed arguments, as a few screens on ONE ephemeral message.

Every screen is pickers — selects, member pickers, buttons — and, only where a value has no option set (a name, a
reason), a modal. A date and time is never typed: it is three selects in one modal (day · hour · minutes), resolved
here DST-safely. Nothing is written before the final Confirm, so an abandoned wizard (timeout, bot restart) changes
nothing.

The rule this module serves (CLAUDE.md): an argument that needs a PARSER goes; an argument Discord already renders as
a widget (a member, a role, a channel, a fixed choice) stays. Registry validation (parse_slots, normalise_name,
set_raid_overrides' cross-field rules, the absence cap) is never re-implemented here — the wizard fronts it, and a
refusal comes back as a screen with the draft kept.

Discord facts the code depends on (verified against discord.py 2.7.1):
  · a StringSelect works inside a Modal when wrapped in `ui.Label`; the submit fills `select.values`
  · a modal's selects fire no callbacks, so two selects in ONE modal can never depend on each other
  · a modal submit cannot answer with another modal — a button always sits between two modals
  · a modal opened from a button on the wizard message can edit that message on submit
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord

from .registry import MAX_ABSENCE_DAYS, Registry, RegistryError

log = logging.getLogger(__name__)

# Discord limits (the API refuses payloads past these; `lint` checks them offline)
MAX_OPTIONS, MAX_MODAL_ITEMS, MAX_LABEL, MAX_DESCRIPTION, MAX_TITLE, MAX_BUTTON = 25, 5, 45, 100, 45, 80
WIZARD_TIMEOUT_S = 600  # an interaction token lives 15 minutes; expiring first lets the wizard say it expired


@dataclass
class Opt:
    """One choice in a select. `value` is a machine key a person never reads; `label` is what they read."""

    value: str
    label: str
    description: str | None = None
    emoji: str | None = None
    default: bool = False

    def option(self) -> discord.SelectOption:
        emoji = None
        if self.emoji:
            try:
                emoji = discord.PartialEmoji.from_str(self.emoji)
            except Exception:  # noqa: BLE001 — a missing app emoji degrades to none
                emoji = None
        return discord.SelectOption(label=self.label[:100], value=self.value[:100], description=(self.description or None) and self.description[:100], emoji=emoji, default=self.default)


@dataclass
class Field:
    """One question in a Form. `options` set → a select; `options` None → a text box (for values with no option set)."""

    key: str
    label: str
    options: Sequence[Opt] | None = None
    description: str | None = None
    required: bool = True
    paragraph: bool = False
    default: str | None = None
    placeholder: str | None = None
    min_length: int | None = None
    max_length: int | None = None


class Form(discord.ui.Modal):
    """A modal built from Fields; `on_submit(interaction, values)` gets {key: str} (a select's value, or the text)."""

    def __init__(self, title: str, fields: Sequence[Field], on_submit: Callable[[discord.Interaction, dict[str, str]], Awaitable[None]]):
        super().__init__(title=title[:MAX_TITLE], timeout=WIZARD_TIMEOUT_S)
        self._fields, self._done, self._items = list(fields), on_submit, {}
        for f in self._fields[:MAX_MODAL_ITEMS]:
            if f.options is not None:
                item = discord.ui.Select(custom_id=f"f_{f.key}", placeholder=(f.placeholder or f.label)[:150],
                                         options=[o.option() for o in list(f.options)[:MAX_OPTIONS]], required=f.required,
                                         min_values=1 if f.required else 0, max_values=1)
            else:
                item = discord.ui.TextInput(custom_id=f"f_{f.key}", style=discord.TextStyle.paragraph if f.paragraph else discord.TextStyle.short,
                                            required=f.required, default=f.default, placeholder=(f.placeholder or None) and f.placeholder[:100],
                                            min_length=f.min_length, max_length=f.max_length)
            self._items[f.key] = item
            self.add_item(discord.ui.Label(text=f.label[:MAX_LABEL], description=(f.description or None) and f.description[:MAX_DESCRIPTION], component=item))

    def values(self) -> dict[str, str]:
        out = {}
        for key, item in self._items.items():
            if isinstance(item, discord.ui.Select):
                out[key] = item.values[0] if item.values else ""
            else:
                out[key] = str(item.value or "").strip()
        return out

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._done(interaction, self.values())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("wizard form failed", exc_info=error)
        text = f"❌ {error}" if isinstance(error, RegistryError) else "❌ Something went wrong; nothing was changed."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class Wizard(discord.ui.View):
    """One ephemeral message that advances in place. A flow subclasses this and writes its screens as methods that end
    in `show(...)`; the last one calls `finish(...)`. Only the person who started it can press anything, and the
    `guard` (officer/owner status, test-bench rules) is checked again at every press, not only at the start."""

    def __init__(self, reg: Registry, owner_id: int, *, title: str, guard: Callable[[discord.Interaction], Awaitable[str | None]] | None = None):
        super().__init__(timeout=WIZARD_TIMEOUT_S)
        self.reg, self.owner_id, self.title = reg, owner_id, title
        self.draft: dict = {}
        self.guard = guard
        self.message_id: int | None = None
        self._last: discord.Interaction | None = None  # the newest interaction, so an expired wizard can say so
        self._closed = False

    # ---- permissions
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This belongs to someone else's wizard.", ephemeral=True)
            return False
        if self.guard:
            why = await self.guard(interaction)
            if why:
                await interaction.response.send_message(why, ephemeral=True)
                return False
        return True

    # ---- rendering
    async def show(self, interaction: discord.Interaction, text: str, *items: discord.ui.Item, embed: discord.Embed | None = None) -> None:
        """Replace the screen: the text, then `items` (selects take a whole row, buttons pack five to a row)."""
        self.clear_items()
        for it in items:
            self.add_item(it)
        body = f"**{self.title}**\n{text}"[:2000]
        self._last = interaction
        if interaction.response.is_done():
            await interaction.edit_original_response(content=body, embed=embed, view=self)
            return
        on_ours = interaction.message is not None and self.message_id is not None and interaction.message.id == self.message_id
        if on_ours and interaction.type in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            await interaction.response.edit_message(content=body, embed=embed, view=self)
            return
        await interaction.response.send_message(body, embed=embed, view=self, ephemeral=True)
        try:
            self.message_id = (await interaction.original_response()).id
        except discord.HTTPException:
            self.message_id = None

    async def working(self, interaction: discord.Interaction, text: str = "Working…") -> None:
        """Acknowledge within Discord's 3 seconds before something slow (a solver, a DM fan-out, a git commit)."""
        self.clear_items()
        self._last = interaction
        on_ours = interaction.message is not None and interaction.message.id == self.message_id
        if on_ours:
            await interaction.response.edit_message(content=f"**{self.title}**\n{text}", embed=None, view=None)
        else:
            await interaction.response.send_message(f"**{self.title}**\n{text}", ephemeral=True)
            try:
                self.message_id = (await interaction.original_response()).id
            except discord.HTTPException:
                pass

    async def finish(self, interaction: discord.Interaction, text: str) -> None:
        """The last screen: the outcome, no buttons. Safe after `working` (edits the original response)."""
        self._closed = True
        self.stop()
        body = f"**{self.title}**\n{text}"[:2000]
        if interaction.response.is_done():
            await interaction.edit_original_response(content=body, embed=None, view=None)
        elif interaction.message is not None and interaction.message.id == self.message_id:
            await interaction.response.edit_message(content=body, embed=None, view=None)
        else:
            await interaction.response.send_message(body, ephemeral=True)

    async def refuse(self, interaction: discord.Interaction, error: str, retry: Callable[[discord.Interaction], Awaitable[None]] | None = None, retry_label: str = "Fix this") -> None:
        """A validation refusal: say what is wrong, keep the draft, and offer to go back to the step that caused it."""
        items = [self.button(retry_label, retry, discord.ButtonStyle.primary)] if retry else []
        items.append(self.button("Cancel", self.cancel, discord.ButtonStyle.secondary))
        await self.show(interaction, f"❌ {error}", *items)

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.finish(interaction, "Cancelled — nothing was changed.")

    async def on_timeout(self) -> None:
        if self._closed or not self._last:
            return
        try:
            await self._last.edit_original_response(content=f"**{self.title}**\nThis wizard expired; nothing was changed. Run the command again to start over.", embed=None, view=None)
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("wizard %s failed", self.title, exc_info=error)
        text = f"❌ {error}" if isinstance(error, RegistryError) else "❌ Something went wrong; nothing was changed. Run the command again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    # ---- building blocks (callbacks are bound here, so a screen is a short list of these)
    def button(self, label: str, on_press: Callable[[discord.Interaction], Awaitable[None]], style: discord.ButtonStyle = discord.ButtonStyle.secondary,
               *, disabled: bool = False, emoji: str | None = None) -> discord.ui.Button:
        b = discord.ui.Button(label=label[:MAX_BUTTON], style=style, disabled=disabled, emoji=emoji)
        b.callback = on_press  # type: ignore[method-assign]
        return b

    def select(self, placeholder: str, opts: Sequence[Opt], on_pick: Callable[[discord.Interaction, list[str]], Awaitable[None]],
               *, min_values: int = 1, max_values: int = 1) -> discord.ui.Select:
        opts = list(opts)[:MAX_OPTIONS]
        s = discord.ui.Select(placeholder=placeholder[:150], options=[o.option() for o in opts] or [discord.SelectOption(label="—", value="-")],
                              min_values=min(min_values, len(opts) or 1), max_values=min(max_values, len(opts) or 1), disabled=not opts)

        async def cb(interaction: discord.Interaction) -> None:
            await on_pick(interaction, list(s.values))
        s.callback = cb  # type: ignore[method-assign]
        return s

    def members(self, placeholder: str, on_pick: Callable[[discord.Interaction, list[discord.abc.User]], Awaitable[None]], *, max_values: int = 1) -> discord.ui.UserSelect:
        """Discord's own member picker: searchable over the whole server, no 25-option ceiling, no names to spell."""
        s = discord.ui.UserSelect(placeholder=placeholder[:150], min_values=1, max_values=max_values)

        async def cb(interaction: discord.Interaction) -> None:
            await on_pick(interaction, list(s.values))
        s.callback = cb  # type: ignore[method-assign]
        return s

    async def open_form(self, interaction: discord.Interaction, form: Form) -> None:
        """Open a modal. Only from a button, a select or a command — never as the answer to another modal."""
        self._last = interaction
        await interaction.response.send_modal(form)


# ---------------------------------------------------------------- dates and times (never typed)

def resolve_local(naive: datetime, tz: ZoneInfo) -> tuple[datetime, str]:
    """A wall-clock time in the guild's zone → the real instant. `datetime(..., tzinfo=tz)` is wrong twice a year:
    on the spring-forward night 2:30 AM does not exist (it silently becomes 3:30), and on the fall-back night 1:30 AM
    happens twice. Returns the instant and a note ("", "gap", "ambiguous") the confirm screen explains."""
    naive = naive.replace(tzinfo=None)
    a, b = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if a.utcoffset() != b.utcoffset():
        if a.timestamp() > b.timestamp():  # the gap: this wall time never happens; move past it
            return (naive + timedelta(hours=1)).replace(tzinfo=tz), "gap"
        return a, "ambiguous"  # the overlap: take the first pass
    return a, ""


def when_note(note: str, dt: datetime) -> str:
    if note == "gap":
        return f"-# That time doesn't exist that night (the clocks go forward) — using {clock12(dt)}."
    if note == "ambiguous":
        return f"-# {clock12(dt)} happens twice that night (the clocks go back) — using the first."
    return ""


def clock12(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def day_opts(reg: Registry, start: date, n: int = MAX_OPTIONS, *, default: date | None = None, note: Callable[[date], str | None] | None = None,
             later: bool = False) -> list[Opt]:
    """`n` days from `start` ('Today · Tue 22 Sep', 'Tomorrow · Wed 23 Sep', 'Thu 24 Sep'); with `later`, the last
    option is 'Later than this…' so a date past the window is reached by a narrowing step, never by typing."""
    today = reg.now_local().date()
    count = n - 1 if later else n
    out = []
    for i in range(count):
        d = start + timedelta(days=i)
        name = d.strftime("%a %d %b")
        if d == today:
            name = f"Today · {name}"
        elif d == today + timedelta(days=1):
            name = f"Tomorrow · {name}"
        out.append(Opt(d.isoformat(), name, note(d) if note else None, default=d == default))
    if later:
        out.append(Opt("later", "Later than this…", f"after {(start + timedelta(days=count - 1)).strftime('%a %d %b')}"))
    return out


def week_opts(reg: Registry, start: date, n: int = MAX_OPTIONS) -> list[Opt]:
    """Weeks from `start` (Monday-aligned) for picking a later window: 'Week of Mon 26 Oct'."""
    monday = start - timedelta(days=start.weekday())
    return [Opt((monday + timedelta(weeks=i)).isoformat(), f"Week of {(monday + timedelta(weeks=i)).strftime('%a %d %b')}") for i in range(n)]


def hour_opts(default: int | None = None) -> list[Opt]:
    """All 24 hours as a person says them: 12 AM … 11 PM. Never 19:00."""
    return [Opt(str(h), f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}", default=h == default) for h in range(24)]


def minute_opts(step: int = 5, default: int | None = 0) -> list[Opt]:
    """Minutes at `step`, the quarter hours first (the ones people pick)."""
    quarters = [0, 15, 30, 45]
    rest = [m for m in range(0, 60, step) if m not in quarters]
    return [Opt(str(m), f":{m:02d}", default=m == default) for m in quarters + rest][:MAX_OPTIONS]


DURATIONS = [(1, "1 day"), (2, "2 days"), (3, "3 days"), (4, "4 days"), (5, "5 days"), (6, "6 days"), (7, "a week"), (10, "10 days"),
             (14, "2 weeks"), (21, "3 weeks"), (30, "about a month"), (42, "6 weeks"), (60, "2 months"), (90, "3 months"), (120, "4 months")]


def duration_opts(upto: int = MAX_ABSENCE_DAYS, default: int = 1) -> list[Opt]:
    """How long, independent of the start (a second date select can't depend on the first inside one modal), so an end
    before the start or past the cap is impossible to pick. Days are counted inclusive of the first."""
    out = [(d, name) for d, name in DURATIONS if d <= upto + 1]
    return [Opt(str(d), name, "the most we take" if d == out[-1][0] else None, default=d == default) for d, name in out]


def span_end(start: date, days: int) -> date:
    return start + timedelta(days=max(1, days) - 1)


def when_fields(reg: Registry, *, day0: date, default: datetime | None = None, later: bool = True, day_note: Callable[[date], str | None] | None = None) -> list[Field]:
    """The three selects that pick a start: which day, the hour, the minutes. `default` preselects the usual time."""
    tzname = reg.config.timezone
    return [
        Field("day", "Which day?", day_opts(reg, day0, default=default.date() if default else None, note=day_note, later=later), description=f"days in {tzname}"),
        Field("hour", "Start time — hour", hour_opts(default.hour if default else 19), description="guild time, 12-hour clock"),
        Field("minute", "Minutes", minute_opts(5, default.minute if default else 0)),
    ]


def combine_when(reg: Registry, values: dict[str, str]) -> tuple[datetime, str]:
    """The picked day + hour + minutes → an aware instant in the guild's zone, and the DST note for the confirm screen."""
    d = date.fromisoformat(values["day"])
    naive = datetime(d.year, d.month, d.day, int(values.get("hour") or 0), int(values.get("minute") or 0))
    return resolve_local(naive, reg.tz)


def both_clocks(reg: Registry, dt: datetime) -> str:
    """Guild time spelled out, plus a Discord stamp that renders in the reader's own zone — a wrong day, year or DST
    edge shows up here before anything is saved."""
    return f"**{reg.local12(dt)}** guild time · <t:{int(dt.timestamp())}:f> your time"


# ---------------------------------------------------------------- offline checks (scripts/check_commands.py)

def lint(obj: discord.ui.View | discord.ui.Modal) -> list[str]:
    """What the Discord API would refuse, checked without Discord: ≤5 modal items; nothing but Labels in a modal (a
    Button there builds fine locally and is rejected); no Label in a View; ≤25 options per select; Discord's text limits."""
    errs: list[str] = []
    is_modal = isinstance(obj, discord.ui.Modal)
    kids = list(obj.children)
    if is_modal:
        if len(obj.title or "") > MAX_TITLE:
            errs.append(f"modal title over {MAX_TITLE}: {obj.title!r}")
        if len(kids) > MAX_MODAL_ITEMS:
            errs.append(f"modal has {len(kids)} items (max {MAX_MODAL_ITEMS})")
    for it in obj.walk_children() if hasattr(obj, "walk_children") else kids:
        if is_modal and isinstance(it, discord.ui.Button):
            errs.append("a Button inside a modal (the API rejects it)")
        if not is_modal and isinstance(it, discord.ui.Label):
            errs.append("a Label inside a view (labels are modal-only)")
        if isinstance(it, discord.ui.Label):
            if len(it.text) > MAX_LABEL:
                errs.append(f"label over {MAX_LABEL}: {it.text!r}")
            if it.description and len(it.description) > MAX_DESCRIPTION:
                errs.append(f"label description over {MAX_DESCRIPTION}: {it.description!r}")
        if isinstance(it, discord.ui.Select):
            if len(it.options) > MAX_OPTIONS:
                errs.append(f"select {it.placeholder!r} has {len(it.options)} options (max {MAX_OPTIONS})")
            for o in it.options:
                if len(o.label) > 100 or len(o.value) > 100 or (o.description and len(o.description) > 100):
                    errs.append(f"select option too long: {o.label!r}")
        if isinstance(it, discord.ui.Button) and it.label and len(it.label) > MAX_BUTTON:
            errs.append(f"button label over {MAX_BUTTON}: {it.label!r}")
        cid = getattr(it, "custom_id", None)
        if isinstance(cid, str) and len(cid) > 100:
            errs.append(f"custom_id over 100: {cid!r}")
    if not is_modal:
        selects = sum(1 for it in kids if isinstance(it, discord.ui.Select | discord.ui.UserSelect))
        buttons = sum(1 for it in kids if isinstance(it, discord.ui.Button))
        if selects + -(-buttons // 5) > 5:
            errs.append(f"view needs {selects + -(-buttons // 5)} rows (max 5)")
    return errs


# ---------------------------------------------------------------- flows registry
Flow = Callable[..., Awaitable[None]]
FLOWS: dict[str, Flow] = {}


def flow(name: str) -> Callable[[Flow], Flow]:
    """Register a flow's entry point: `async def open_x(interaction, reg, **seed)`. The slash command and any card
    button both call FLOWS[name], so there is exactly one implementation of each flow."""
    def deco(fn: Flow) -> Flow:
        FLOWS[name] = fn
        return fn
    return deco


__all__ = ["Opt", "Field", "Form", "Wizard", "FLOWS", "flow", "resolve_local", "when_note", "clock12", "day_opts", "week_opts", "hour_opts",
           "minute_opts", "duration_opts", "span_end", "when_fields", "combine_when", "both_clocks", "lint", "DURATIONS", "Flow"]
