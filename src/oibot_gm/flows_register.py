"""Registration wizards: one flow for class → spec → offspec → name, and the small character flows around it.

`register` serves the registration card (Register / plan my main · Add an alt), /register, /me char add, /me plan
main|alt (`plan_only`: no name step) and /apply (`mode="apply"`: the application form instead of the name form).
The class list and each class's specs come from the game profile (wizard_opts), never a hardcoded list, and the spec
list always belongs to the class picked. A name is prose with one rule (2–12 letters): a TextInput whose rule is on
its label; a refusal keeps every pick and reopens the SAME form with the typed names filled in.

`char_name`, `char_spec`, `char_main`, `char_retire` pick one of your own characters (always a handful) instead of
typing it. Nothing is written before the last Submit / Confirm; the Registry re-checks everything when it saves."""
from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from . import wizard_opts as opts
from .registry import RegisteredCharacter, Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, flow

log = logging.getLogger(__name__)

NAME_RULE = "2–12 letters, no spaces or numbers"


# ---------------------------------------------------------------- shared bits

def _ico(interaction: discord.Interaction):
    """The client's icon lookup (app emojis); a no-op where there is none (tests, a half-started bot)."""
    fn = getattr(interaction.client, "ico", None)
    return fn if callable(fn) else (lambda kind, key: "")


def _char_line(interaction: discord.Interaction, c: RegisteredCharacter) -> str:
    from .discord_registry import char_line

    return char_line(_ico(interaction), c)


async def _emit(interaction: discord.Interaction, reg: Registry, text: str, level: str = "info") -> None:
    ops = getattr(interaction.client, "ops", None)
    if ops is None:
        log.info("ops (no feed): %s", text)
        return
    await ops.emit(reg.config, level, text)


def _specline(spec: str | None, offspec: str | None) -> str:
    return f"{spec or '?'}{'/' + offspec if offspec else ''}"


def char_opts(reg: Registry, chars: Sequence[RegisteredCharacter], ico, *, current: str | None = None) -> list[Opt]:
    """A member's characters, main first. The value is the full label (a two-word name is one character; a first
    name alone could match two), which is what Registry's `matches` compares."""
    ordered = sorted(chars, key=lambda c: (not c.is_main, c.label.lower()))
    return [Opt(c.label, c.label, f"{c.cls} {_specline(c.spec, c.offspec)}" + (" · main" if c.is_main else "") + (" · planned" if c.status == "planned" else ""),
                emoji=ico("class", c.cls) if ico else None, default=c.label == current) for c in ordered][:MAX_OPTIONS]


def name_fields(*, first: str = "", last: str = "", first_optional: bool) -> list[Field]:
    """First + last name. Discord enforces the length before the submit; the Registry checks the letters."""
    return [
        Field("first", "First name (blank = not created yet)" if first_optional else "First name", required=not first_optional,
              description=NAME_RULE + (" · leave blank to plan it and name it at launch" if first_optional else ""),
              default=first or None, min_length=2, max_length=12),
        Field("last", "Last name (blank if none)", required=False, description=NAME_RULE, default=last or None, min_length=2, max_length=12),
    ]


# ---------------------------------------------------------------- register / plan / apply

class RegisterWizard(Wizard):
    """Class · main spec · offspec on one screen (the spec list follows the class), then Continue: the name form
    (register), a confirm screen (plan only) or the application form (apply)."""

    def __init__(self, reg: Registry, owner_id: int, *, slot: str, plan_only: bool, mode: str, roles: list[str] | None):
        if mode == "apply":
            title = f"Apply to {reg.config.name}"
        elif plan_only:
            title = "Plan your main" if slot == "main" else "Plan an alt"
        else:
            title = "Register or plan your main" if slot == "main" else "Add an alt"
        super().__init__(reg, owner_id, title=title)
        self.slot, self.plan_only, self.mode, self.roles = slot, plan_only, mode, list(roles or [])
        self.draft = {"cls": None, "spec": None, "offspec": None, "first": "", "last": "", "logs": "", "times": "", "about": ""}

    # ---- screen 1: class · spec · offspec
    def intro(self) -> str:
        d = self.draft
        if self.mode == "apply":
            head = "Pick your class, main spec and an optional offspec, then **Continue** for your name and a few words about you."
        elif self.plan_only:
            head = "Pick the class, main spec and an optional offspec — no name needed yet."
        else:
            head = ("Pick the class, main spec and an optional offspec, then **Continue** to name it "
                    "(or leave the name blank until launch). Your role follows the spec; an offspec in another role counts as flexibility.")
        if d["cls"] and d["spec"]:
            head += f"\n-# So far: **{d['cls']} {_specline(d['spec'], d['offspec'])}**"
        return head

    def pick_items(self, interaction: discord.Interaction) -> list[discord.ui.Item]:
        d, ico = self.draft, _ico(interaction)
        specs = opts.specs_of(self.reg, d["cls"], ico, current=d["spec"]) if d["cls"] else []
        offs = [o for o in opts.specs_of(self.reg, d["cls"], ico, current=d["offspec"], none_label="none") if o.value != d["spec"]] if d["cls"] else []
        return [
            self.select("Class", opts.classes(self.reg, ico, current=d["cls"]), self.on_class),
            self.select("Main spec", specs, self.on_spec),
            self.select("Offspec (optional)", offs, self.on_offspec),
            self.button("Confirm…" if self.plan_only else "Continue", self.on_continue, discord.ButtonStyle.primary, disabled=not d["spec"]),
            self.button("Cancel", self.cancel),
        ]

    async def start(self, interaction: discord.Interaction) -> None:
        await self.show(interaction, self.intro(), *self.pick_items(interaction))

    async def on_class(self, interaction: discord.Interaction, values: list[str]) -> None:
        if values[0] != self.draft["cls"]:
            self.draft.update(cls=values[0], spec=None, offspec=None)  # another class: its own specs, picked afresh
        await self.show(interaction, self.intro(), *self.pick_items(interaction))

    async def on_spec(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["spec"] = values[0]
        if self.draft["offspec"] == values[0]:
            self.draft["offspec"] = None
        await self.show(interaction, self.intro(), *self.pick_items(interaction))

    async def on_offspec(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["offspec"] = None if values[0] == "-" else values[0]
        await self.show(interaction, self.intro(), *self.pick_items(interaction))

    async def on_continue(self, interaction: discord.Interaction) -> None:
        if self.mode == "apply":
            await self.open_form(interaction, self.apply_form())
        elif self.plan_only:
            await self.confirm_plan(interaction)
        else:
            await self.open_form(interaction, self.name_form())

    async def back_to_picks(self, interaction: discord.Interaction) -> None:
        await self.start(interaction)

    # ---- register: the name form (a blank first name = planned)
    def name_form(self) -> Form:
        d = self.draft
        return Form(f"{d['cls']} {d['spec']} — almost done", name_fields(first=d["first"], last=d["last"], first_optional=True), self.names_submitted)

    async def reopen_names(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.name_form())

    async def names_submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["first"], self.draft["last"] = v.get("first", ""), v.get("last", "")
        if self.draft["last"] and not self.draft["first"]:
            await self.refuse(interaction, "A last name needs a first name — or leave both blank to plan the character and name it at launch.", self.reopen_names)
            return
        await self.save_register(interaction)

    async def save_register(self, interaction: discord.Interaction) -> None:
        reg, d, user = self.reg, self.draft, interaction.user
        name = d["first"]
        await self.working(interaction, "Saving…")
        try:
            if name:
                m, c = reg.add_character(user.id, user.display_name, name, d["cls"], d["spec"], d["offspec"], self.slot == "main", surname=d["last"] or None)
            else:
                m, c = reg.set_plan(user.id, user.display_name, d["cls"], d["spec"], d["offspec"], self.slot)
            if self.roles:
                reg.set_roles(user.id, user.display_name, self.roles[0], self.roles[1:])
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen_names)
            return
        what = f"{'Registered' if name else 'Planned'} {'main' if self.slot == 'main' else 'alt'}: **{c.label}** · {_specline(c.spec, c.offspec)}"
        roles = (f" · roles: {self.roles[0]}" + (f" (+{', '.join(self.roles[1:])})" if len(self.roles) > 1 else "")) if self.roles else ""
        tail = " An officer will confirm the character." if name else " Add the name at launch with `/me char name`."
        await self.finish(interaction, f"✅ {what}{roles}.{tail} `/me view` shows everything.")
        await _emit(interaction, reg, f"**{m.display_name}** {'registered' if name else 'planned'} {c.label} ({c.cls} {c.spec}{', main' if c.is_main else ', alt'})"
                    + (f" · roles {self.roles}" if self.roles else "") + ("" if not name else " — pending confirmation"))

    # ---- plan only: confirm, then set_plan
    async def confirm_plan(self, interaction: discord.Interaction) -> None:
        d = self.draft
        m = self.reg.members.get(interaction.user.id)
        prev = next((c for c in (m.planned() if m else []) if c.is_main == (self.slot == "main")), None)
        lines = [f"Plan {'your main' if self.slot == 'main' else 'an alt'}: **{d['cls']} {_specline(d['spec'], d['offspec'])}**."]
        if prev:
            lines.append(f"-# Replaces the planned {prev.cls} {_specline(prev.spec, prev.offspec)}.")
        await self.show(interaction, "\n".join(lines),
                        self.button("Confirm", self.save_plan, discord.ButtonStyle.success),
                        self.button("Change", self.back_to_picks),
                        self.button("Cancel", self.cancel))

    async def save_plan(self, interaction: discord.Interaction) -> None:
        reg, d, user = self.reg, self.draft, interaction.user
        await self.working(interaction, "Saving…")
        try:
            m, c = reg.set_plan(user.id, user.display_name, d["cls"], d["spec"], d["offspec"], self.slot)
            if self.roles:
                reg.set_roles(user.id, user.display_name, self.roles[0], self.roles[1:])
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.back_to_picks, "Change")
            return
        if self.slot == "main":
            await self.finish(interaction, f"✅ Planned main: {_char_line(interaction, c)}. Your role follows the spec; extra roles you'd play: `/me plan roles`.")
            await _emit(interaction, reg, f"{m.display_name} plans to main {c.cls} {c.spec}")
        else:
            await self.finish(interaction, f"✅ Planned alt: {_char_line(interaction, c)}")
            await _emit(interaction, reg, f"{m.display_name} plans an alt {c.cls} {c.spec}")

    # ---- apply: the application form, then reg.apply + the officers' card
    def apply_form(self) -> Form:
        d = self.draft
        return Form(f"{d['cls']} {d['spec']} — application", [
            Field("name", "Character name", description=NAME_RULE, default=d["first"] or None, min_length=2, max_length=12),
            Field("logs", "Logs or armory link (optional)", required=False, default=d["logs"] or None, max_length=200,
                  placeholder="Warcraft Logs / armory"),
            Field("times", "When you can raid (optional)", required=False, paragraph=True, default=d["times"] or None, max_length=300,
                  placeholder="the nights and times that suit you"),
            Field("about", "About you (optional)", required=False, paragraph=True, default=d["about"] or None, max_length=600,
                  placeholder="experience, what you're looking for"),
        ], self.apply_submitted)

    async def reopen_apply(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.apply_form())

    async def apply_submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        d = self.draft
        d["first"], d["logs"], d["times"], d["about"] = v.get("name", ""), v.get("logs", ""), v.get("times", ""), v.get("about", "")
        await self.working(interaction, "Sending your application…")
        user = interaction.user
        try:
            a = self.reg.apply(user.id, user.display_name, d["first"], d["cls"], d["spec"], d["offspec"], d["logs"] or None, d["times"] or None, d["about"] or None)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen_apply)
            return
        where = ""
        hook = getattr(interaction.client, "post_application", None)
        if hook is not None:
            try:
                where = await hook(self.reg, a) or ""
            except Exception:  # noqa: BLE001 — the application is saved; officers still see it in /roster applicants
                log.exception("posting the application card failed")
                where = "The officers' card couldn't be posted; they'll still see it in `/roster applicants`."
        else:
            log.warning("client has no post_application; application %s saved without a card", a.name)
        await self.finish(interaction, f"✅ Application received for **{a.name}** ({a.cls} {a.spec}). An officer will review it; you'll get a DM either way."
                          + (f"\n-# {where}" if where else ""))


@flow("register")
async def open_register(interaction: discord.Interaction, reg: Registry, *, slot: str = "main", plan_only: bool = False, mode: str = "register",
                        roles: list[str] | None = None) -> None:
    """The registration card's Register / Add an alt, /register, /me char add (slot="alt"), /me plan main|alt
    (plan_only) and /apply (mode="apply")."""
    if slot not in ("main", "alt") or mode not in ("register", "apply"):
        raise ValueError(f"register flow: bad seed slot={slot!r} mode={mode!r}")
    if mode == "apply":
        m = reg.members.get(interaction.user.id)
        if m and m.active():
            await interaction.response.send_message("You're already a member — add characters with `/register` or `/me char add`.", ephemeral=True)
            return
    await RegisterWizard(reg, interaction.user.id, slot=slot, plan_only=plan_only, mode=mode, roles=roles).start(interaction)


# ---------------------------------------------------------------- /me char name

class CharNameWizard(Wizard):
    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Name your character")
        self.draft = {"slot": None, "cls": None, "first": "", "last": ""}

    def planned(self, uid: int) -> list[RegisteredCharacter]:
        m = self.reg.members.get(uid)
        return m.planned() if m else []

    async def start(self, interaction: discord.Interaction) -> None:
        chars = self.planned(interaction.user.id)
        if not chars:
            await interaction.response.send_message("You have no planned character to name — `/register` adds a character with its name.", ephemeral=True)
            return
        if len(chars) == 1:  # the usual case at launch: straight to the names
            self.choose(chars[0])
            await self.open_form(interaction, self.form())
            return
        choices = [Opt("main" if c.is_main else "alt", c.label, f"planned {'main' if c.is_main else 'alt'}", emoji=_ico(interaction)("class", c.cls) or None)
                   for c in sorted(chars, key=lambda c: not c.is_main)]
        await self.show(interaction, "Which planned character are you naming?", self.select("Planned character", choices, self.picked), self.button("Cancel", self.cancel))

    def choose(self, c: RegisteredCharacter) -> None:
        self.draft.update(slot="main" if c.is_main else "alt", cls=c.cls, spec=c.spec)

    async def picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        c = next((c for c in self.planned(interaction.user.id) if ("main" if c.is_main else "alt") == values[0]), None)
        if not c:
            await self.refuse(interaction, "That planned character is gone. Run `/me char name` again.")
            return
        self.choose(c)
        await self.open_form(interaction, self.form())

    def form(self) -> Form:
        d = self.draft
        return Form(f"Name your {d['cls']} ({d['slot']})", name_fields(first=d["first"], last=d["last"], first_optional=False), self.submitted)

    async def reopen(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.form())

    async def submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        d, uid = self.draft, interaction.user.id
        d["first"], d["last"] = v.get("first", ""), v.get("last", "")
        if not d["first"]:
            await self.refuse(interaction, f"A first name is needed ({NAME_RULE}).", self.reopen)
            return
        await self.working(interaction, "Saving…")
        now = next((c for c in self.planned(uid) if c.is_main == (d["slot"] == "main")), None)
        if not now or now.cls != d["cls"]:  # re-planned meanwhile: don't name a different character than the one shown
            await self.finish(interaction, f"❌ Your planned {d['slot']} changed since you started. Run `/me char name` again.")
            return
        try:
            m, c = self.reg.name_character(uid, d["first"], d["slot"], surname=d["last"] or None)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen)
            return
        await self.finish(interaction, f"✅ {_char_line(interaction, c)} — an officer will confirm it.")
        await _emit(interaction, self.reg, f"**{m.display_name}** named their planned {d['slot']}: {c.label} ({c.cls} {c.spec}) — pending confirmation")


@flow("char_name")
async def open_char_name(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    await CharNameWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- pick one of your characters (spec · main · retire)

class OwnCharWizard(Wizard):
    """Pick one of your own characters (skipped when there is only one candidate), then `picked_one`."""

    empty = "You have no characters yet — `/register` to start."
    prompt = "Which character?"

    def candidates(self, uid: int) -> list[RegisteredCharacter]:
        m = self.reg.members.get(uid)
        return m.active() if m else []

    def current(self, uid: int) -> RegisteredCharacter | None:
        """Re-read the picked character from the Registry (never trust the screen)."""
        label = self.draft.get("label")
        return next((c for c in self.candidates(uid) if c.label == label), None)

    async def start(self, interaction: discord.Interaction) -> None:
        chars = self.candidates(interaction.user.id)
        if not chars:
            await interaction.response.send_message(self.empty, ephemeral=True)
            return
        if len(chars) == 1:
            self.draft["label"] = chars[0].label
            await self.picked_one(interaction)
            return
        await self.show_pick(interaction)

    async def show_pick(self, interaction: discord.Interaction) -> None:
        chars = self.candidates(interaction.user.id)
        await self.show(interaction, self.prompt, self.select("Character", char_opts(self.reg, chars, _ico(interaction), current=self.draft.get("label")), self.on_pick),
                        self.button("Cancel", self.cancel))

    async def on_pick(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["label"] = values[0]
        await self.picked_one(interaction)

    async def gone(self, interaction: discord.Interaction) -> None:
        await self.finish(interaction, f"❌ {self.draft.get('label')} is no longer one of your active characters. Nothing was changed.")

    async def picked_one(self, interaction: discord.Interaction) -> None:  # pragma: no cover — each flow overrides
        raise NotImplementedError


class CharSpecWizard(OwnCharWizard):
    prompt = "Which character's spec are you changing?"

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Change spec")

    async def picked_one(self, interaction: discord.Interaction) -> None:
        c = self.current(interaction.user.id)
        if not c:
            await self.gone(interaction)
            return
        self.draft.setdefault("spec", c.spec)
        self.draft.setdefault("offspec", c.offspec)
        self.draft["cls"] = c.cls
        await self.screen(interaction)

    async def screen(self, interaction: discord.Interaction) -> None:
        d, ico = self.draft, _ico(interaction)
        c = self.current(interaction.user.id)
        now = f"{c.cls} {_specline(c.spec, c.offspec)}" if c else "?"
        offs = [o for o in opts.specs_of(self.reg, d["cls"], ico, current=d["offspec"], none_label="none") if o.value != d["spec"]]
        text = f"**{d['label']}** — now {now}.\nNew: **{d['cls']} {_specline(d['spec'], d['offspec'])}**"
        items = [self.select("Main spec", opts.specs_of(self.reg, d["cls"], ico, current=d["spec"]), self.on_spec),
                 self.select("Offspec (optional)", offs, self.on_offspec),
                 self.button("Confirm", self.save, discord.ButtonStyle.success)]
        if len(self.candidates(interaction.user.id)) > 1:
            items.append(self.button("Other character", self.other))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, text, *items)

    async def other(self, interaction: discord.Interaction) -> None:
        for k in ("spec", "offspec", "cls"):
            self.draft.pop(k, None)
        await self.show_pick(interaction)

    async def on_spec(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["spec"] = values[0]
        if self.draft.get("offspec") == values[0]:
            self.draft["offspec"] = None
        await self.screen(interaction)

    async def on_offspec(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["offspec"] = None if values[0] == "-" else values[0]
        await self.screen(interaction)

    async def save(self, interaction: discord.Interaction) -> None:
        d = self.draft
        await self.working(interaction, "Saving…")
        c = self.current(interaction.user.id)
        if not c or c.cls != d["cls"]:
            await self.gone(interaction)
            return
        try:
            c = self.reg.set_spec(interaction.user.id, c.label, d["spec"], d["offspec"])
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.screen, "Change")
            return
        await self.finish(interaction, f"✅ {_char_line(interaction, c)}")
        await _emit(interaction, self.reg, f"{interaction.user.display_name}: {c.label} now {_specline(c.spec, c.offspec)}")


class CharMainWizard(OwnCharWizard):
    prompt = "Which character becomes your main? Your rank moves with you."
    empty = "You have no other character to make your main — `/me char add` registers an alt."

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Change your main")

    def candidates(self, uid: int) -> list[RegisteredCharacter]:
        return [c for c in super().candidates(uid) if not c.is_main]

    async def picked_one(self, interaction: discord.Interaction) -> None:
        m = self.reg.members.get(interaction.user.id)
        old = m.main if m else None
        text = f"Make **{self.draft['label']}** your main?" + (f"\n-# {old.label} becomes an alt; your rank follows you." if old else "")
        await self.show(interaction, text, self.button("Confirm", self.save, discord.ButtonStyle.success), self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        await self.working(interaction, "Saving…")
        c = self.current(interaction.user.id)
        if not c:
            await self.gone(interaction)
            return
        try:
            m, c, old = self.reg.set_main(interaction.user.id, c.label)
        except RegistryError as e:
            await self.finish(interaction, f"❌ {e}")
            return
        await self.finish(interaction, f"✅ Main is now {_char_line(interaction, c)}" + (f" (was {old.label}, now alt)" if old else ""))
        await _emit(interaction, self.reg, f"**{m.display_name}** changed main {old.label if old else '-'} → {c.label}")


class CharRetireWizard(OwnCharWizard):
    prompt = "Which character are you retiring? Its history is kept."

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Retire a character")

    async def picked_one(self, interaction: discord.Interaction) -> None:
        c = self.current(interaction.user.id)
        if not c:
            await self.gone(interaction)
            return
        lines = [f"Retire **{c.label}** ({c.cls} {_specline(c.spec, c.offspec)})? It leaves sheets and rosters; its history is kept."]
        if c.is_main:
            nxt = next((x for x in self.candidates(interaction.user.id) if x is not c), None)
            lines.append(f"-# It is your main — {nxt.label} becomes your main." if nxt else "-# It is your only character.")
        await self.show(interaction, "\n".join(lines), self.button(f"Retire {c.label}", self.save, discord.ButtonStyle.danger), self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        await self.working(interaction, "Saving…")
        c = self.current(interaction.user.id)
        if not c:
            await self.gone(interaction)
            return
        try:
            c = self.reg.retire(interaction.user.id, c.label)
        except RegistryError as e:
            await self.finish(interaction, f"❌ {e}")
            return
        await self.finish(interaction, f"✅ Retired {c.label}.")
        await _emit(interaction, self.reg, f"{interaction.user.display_name} retired {c.label}")


@flow("char_spec")
async def open_char_spec(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    await CharSpecWizard(reg, interaction.user.id).start(interaction)


@flow("char_main")
async def open_char_main(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    await CharMainWizard(reg, interaction.user.id).start(interaction)


@flow("char_retire")
async def open_char_retire(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    await CharRetireWizard(reg, interaction.user.id).start(interaction)


__all__ = ["RegisterWizard", "CharNameWizard", "CharSpecWizard", "CharMainWizard", "CharRetireWizard", "char_opts", "name_fields",
           "open_register", "open_char_name", "open_char_spec", "open_char_main", "open_char_retire"]
