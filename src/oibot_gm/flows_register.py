"""Registration wizards: one flow for class → spec → offspec → name, and the small character flows around it.

`register` serves the registration card (Apply to join · Register / plan my main · Add an alt), /register, /me char
action:Add an alt, /me plan main|alt (`plan_only`: no name step) and /apply (`mode="apply"`). A newcomer (Registry.must_apply) who
tries to register is sent to the application instead. The application is: the class/spec picks (with the guild's
recruitment note) → first and last name + logs → the raid schedules they can make (a multi-select) → the guild's own
questions, five per modal with a button between pages → a review with every answer → Submit (reg.apply + the
officers' card via client.post_application).
The class list and each class's specs come from the game profile (wizard_opts), never a hardcoded list, and the spec
list always belongs to the class picked. A name is prose with one rule (2–12 letters): a TextInput whose rule is on
its label; a refusal keeps every pick and reopens the SAME form with the typed names filled in.

`char_name` (the /me char hub's Name) names a planned character: the names, THE decide screen with the ripple from code,
then a one-row table through the character verb (discord_people.save_characters), so open sheets and locked seats take
the new name. Spec · Main · Flex · Retire · Delete are the hub's own screens (flows_roster.CharWizard) — there is no
second path. Nothing is written before the last Submit / Confirm; the Registry re-checks everything when it saves."""
from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from . import wizard_opts as opts
from .constants import TEAL
from .registry import ANSWER_MAX, Question, RegisteredCharacter, Registry, RegistryError
from .wizard import MAX_MODAL_ITEMS, MAX_OPTIONS, Field, Form, Opt, Wizard, flow

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

    def __init__(self, reg: Registry, owner_id: int, *, slot: str, plan_only: bool, mode: str, roles: list[str] | None, lead: str | None = None):
        if mode == "apply":
            title = f"Apply to {reg.config.name}"
        elif plan_only:
            title = "Plan your main" if slot == "main" else "Plan an alt"
        else:
            title = "Register or plan your main" if slot == "main" else "Add an alt"
        super().__init__(reg, owner_id, title=title)
        self.slot, self.plan_only, self.mode, self.roles = slot, plan_only, mode, list(roles or [])
        self.draft = {"cls": None, "spec": None, "offspec": None, "first": "", "last": "", "logs": "", "about": "", "schedules": [], "answers": {}}
        self.lead = lead  # one line above the first screen (a newcomer sent here from Register)
        self.questions: list[Question] = list(reg.config.recruitment.questions) if mode == "apply" else []  # as asked when the wizard opened
        self.reviewing = False  # after the review screen, a changed step returns to it

    # ---- screen 1: class · spec · offspec
    def intro(self) -> str:
        d = self.draft
        if self.mode == "apply":
            head = "Pick your class, main spec and an optional offspec, then **Continue** for your name"
            head += (", the raid schedules you can make and the officers' questions." if self.questions else " and the raid schedules you can make.")
            note = self.reg.config.recruitment.note
            if note:
                head += f"\n> {note}"
            if self.lead:
                head = f"**{self.lead}**\n{head}"
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
            if self.reviewing:
                await self.review(interaction)
            else:
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
        tail = " An officer will confirm the character." if name else " Add the name at launch with `/me char` → Name."
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
            await self.finish(interaction, f"✅ Planned main: {_char_line(interaction, c)}. Your role follows the spec; extra roles you'd play: `/me char` → Flex.")
            await _emit(interaction, reg, f"{m.display_name} plans to main {c.cls} {c.spec}")
        else:
            await self.finish(interaction, f"✅ Planned alt: {_char_line(interaction, c)}")
            await _emit(interaction, reg, f"{m.display_name} plans an alt {c.cls} {c.spec}")

    # ---- apply: names → schedules → the guild's questions (five per modal) → review → Submit (reg.apply + the card)
    def pages(self) -> list[list[Question]]:
        return [self.questions[i:i + MAX_MODAL_ITEMS] for i in range(0, len(self.questions), MAX_MODAL_ITEMS)]

    def apply_form(self) -> Form:
        d = self.draft
        fields = [
            Field("first", "First name", description=NAME_RULE, default=d["first"] or None, min_length=2, max_length=12),
            Field("last", "Last name", description=NAME_RULE, default=d["last"] or None, min_length=2, max_length=12),
            Field("logs", "Logs or armory link (optional)", required=False, default=d["logs"] or None, max_length=200, placeholder="Warcraft Logs / armory"),
        ]
        if not self.questions:  # no guild questions: keep the free box the application always had
            fields.append(Field("about", "Anything else? (optional)", required=False, paragraph=True, default=d["about"] or None, max_length=600,
                                placeholder="experience, what you're looking for"))
        return Form(f"{d['cls']} {d['spec']} — application", fields, self.apply_names_submitted)

    async def reopen_apply(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.apply_form())

    async def apply_names_submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        d = self.draft
        d["first"], d["last"], d["logs"] = v.get("first", ""), v.get("last", ""), v.get("logs", "")
        if "about" in v:
            d["about"] = v.get("about", "")
        if not d["first"] or not d["last"]:
            await self.refuse(interaction, f"Your character's first and last name, please ({NAME_RULE}).", self.reopen_apply)
            return
        if self.reviewing:
            await self.review(interaction)
        elif opts.schedules(self.reg):
            await self.schedule_screen(interaction)
        elif self.questions:
            await self.question_gate(interaction, 0)
        else:
            await self.review(interaction)

    async def schedule_screen(self, interaction: discord.Interaction) -> None:
        choices = opts.schedules(self.reg, current=self.draft["schedules"])
        picked = opts.schedule_names(self.reg, self.draft["schedules"])
        text = ("Which of our raid schedules can you make? Pick every one that fits — or none — then **Continue**."
                + (f"\n-# Picked: {'; '.join(picked)}" if picked else ""))
        await self.show(interaction, text,
                        self.select("Raid schedules you can make", choices, self.on_schedules, min_values=0, max_values=len(choices)),
                        self.button("Continue", self.after_schedules, discord.ButtonStyle.primary),
                        self.button("Cancel", self.cancel))

    async def on_schedules(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["schedules"] = [x for x in values if x != "-"]
        await self.schedule_screen(interaction)

    async def after_schedules(self, interaction: discord.Interaction) -> None:
        if self.questions and not self.reviewing:
            await self.open_form(interaction, self.question_form(0))  # a button may open a modal directly
        else:
            await self.review(interaction)

    async def question_gate(self, interaction: discord.Interaction, page: int) -> None:
        """Between two modals (a modal submit can't open another): one button to the next page of questions."""
        n = len(self.pages())
        label = "Answer the questions" if n == 1 else (f"Answer the questions (1 of {n})" if page == 0 else f"Next questions ({page + 1} of {n})")
        text = "Next, a few questions from the officers." if page == 0 else f"Thanks — page {page + 1} of {n} of the officers' questions is next. Your answers so far are kept."
        await self.show(interaction, text, self.button(label, lambda i: self.open_form(i, self.question_form(page)), discord.ButtonStyle.primary),
                        self.button("Cancel", self.cancel))

    def question_form(self, page: int) -> Form:
        pages = self.pages()
        page = max(0, min(page, len(pages) - 1))
        answers = self.draft["answers"]
        fields = [Field(f"q{i}", q.label, description=q.hint, required=q.required, paragraph=True, default=answers.get(q.label) or None,
                        max_length=ANSWER_MAX) for i, q in enumerate(pages[page])]
        title = f"Questions ({page + 1} of {len(pages)})" if len(pages) > 1 else "Questions"

        async def done(interaction: discord.Interaction, v: dict[str, str]) -> None:
            await self.questions_submitted(interaction, page, v)
        return Form(title, fields, done)

    async def questions_submitted(self, interaction: discord.Interaction, page: int, v: dict[str, str]) -> None:
        batch = self.pages()[page]
        for i, q in enumerate(batch):
            self.draft["answers"][q.label] = v.get(f"q{i}", "")
        missing = next((q for q in batch if q.required and not self.draft["answers"].get(q.label)), None)
        if missing:
            await self.refuse(interaction, f"Please answer “{missing.label}”.", lambda i: self.open_form(i, self.question_form(page)))
            return
        if not self.reviewing and page + 1 < len(self.pages()):
            await self.question_gate(interaction, page + 1)
        else:
            await self.review(interaction)

    def review_embed(self, interaction: discord.Interaction) -> discord.Embed:
        d, ico = self.draft, _ico(interaction)
        spec_key = f"{d['cls']}:{d['spec']}"
        e = discord.Embed(title=clip(f"Your application · {d['first']} {d['last']}".strip(), 256), colour=TEAL,
                          description=f"{ico('class', d['cls'])}{ico('spec', spec_key)} **{d['cls']} {_specline(d['spec'], d['offspec'])}**")
        if d["logs"]:
            e.add_field(name="Logs", value=clip(d["logs"], 200), inline=False)
        if opts.schedules(self.reg):
            e.add_field(name="Can raid", value=clip("\n".join(opts.schedule_names(self.reg, d["schedules"])), 1000) or "none picked", inline=False)
        for q in self.questions:
            ans = d["answers"].get(q.label, "")
            e.add_field(name=q.label, value=clip(ans, 200) if ans else "—", inline=False)
        if not self.questions and d["about"]:
            e.add_field(name="Anything else", value=clip(d["about"], 300), inline=False)
        return e

    async def review(self, interaction: discord.Interaction) -> None:
        self.reviewing = True
        steps = [Opt("picks", "Class and spec"), Opt("names", "Name and logs" if self.questions else "Name, logs and anything else")]
        if opts.schedules(self.reg):
            steps.append(Opt("schedules", "Raid schedules"))
        n = len(self.pages())
        steps += [Opt(f"q{p}", "Questions" if n == 1 else f"Questions ({p + 1} of {n})") for p in range(n)]
        await self.show(interaction, "Check your application, then **Submit**. Nothing is sent before that; long answers are shortened here only.",
                        self.select("Change a step", steps, self.on_change),
                        self.button("Submit", self.submit_application, discord.ButtonStyle.success),
                        self.button("Cancel", self.cancel), embed=self.review_embed(interaction))

    async def on_change(self, interaction: discord.Interaction, values: list[str]) -> None:
        step = values[0]
        if step == "picks":
            await self.start(interaction)
        elif step == "names":
            await self.reopen_apply(interaction)
        elif step == "schedules":
            await self.schedule_screen(interaction)
        elif step.startswith("q") and step[1:].isdigit():
            await self.open_form(interaction, self.question_form(int(step[1:])))
        else:
            await self.review(interaction)

    def fix_for(self, error: str):
        """The step a refusal points at, so "Fix this" reopens it with the draft kept (None: nothing to fix here)."""
        if "taking applications" in error or "open application" in error or "already a member" in error:
            return None
        for p, batch in enumerate(self.pages()):
            if any(q.label in error for q in batch):
                return lambda i, p=p: self.open_form(i, self.question_form(p))
        if "class" in error.lower() or "spec" in error.lower():
            return self.start
        return self.reopen_apply

    async def submit_application(self, interaction: discord.Interaction) -> None:
        d, user = self.draft, interaction.user
        await self.working(interaction, "Sending your application…")
        answers = [{"label": q.label, "answer": d["answers"].get(q.label, "")} for q in self.questions if d["answers"].get(q.label)]
        try:
            a = self.reg.apply(user.id, user.display_name, d["first"], d["cls"], d["spec"], d["offspec"], d["logs"] or None, None,
                               (d["about"] or None) if not self.questions else None, surname=d["last"] or None, schedules=list(d["schedules"]),
                               answers=answers, source="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.fix_for(str(e)))
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
            log.warning("client has no post_application; application %s saved without a card", a.label)
        await self.finish(interaction, f"✅ Application received for **{a.label}** ({a.cls} {a.spec}). An officer will review it; you'll get a DM either way."
                          + (f"\n-# {where}" if where else ""))


NEWCOMER = "New here? Apply first — the officers take it from there."


def clip(text: str, n: int) -> str:
    """Shorten for display with a clean '…' (the stored text is never cut)."""
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _role_ids(interaction: discord.Interaction) -> list[int]:
    return [r.id for r in (getattr(interaction.user, "roles", None) or []) if getattr(r, "id", None) is not None]


async def must_apply(interaction: discord.Interaction, reg: Registry) -> bool:
    """A newcomer (nothing registered, not an officer, no member role) applies instead of registering. In a DM Discord
    hands over a bare User without roles, so officer and roles come from the guild member (OibotGM.plain_identity, as
    plain text and the /ask reply read them) — else a member-role holder pressing Register in a DM was sent to Apply."""
    from .discord_registry import is_officer

    if interaction.guild_id is None and getattr(interaction.client, "plain_identity", None) is not None:
        officer, role_ids = await interaction.client.plain_identity(reg, interaction.user)
        return reg.must_apply(interaction.user.id, officer=officer, role_ids=role_ids)
    return reg.must_apply(interaction.user.id, officer=is_officer(interaction, reg), role_ids=_role_ids(interaction))


@flow("register")
async def open_register(interaction: discord.Interaction, reg: Registry, *, slot: str = "main", plan_only: bool = False, mode: str = "register",
                        roles: list[str] | None = None) -> None:
    """The registration card's Register / Add an alt / Apply to join, /register, /me char action:Add an alt (slot="alt"), /me plan
    main|alt (plan_only) and /apply (mode="apply"). A newcomer's first character goes through the application instead."""
    if slot not in ("main", "alt") or mode not in ("register", "apply"):
        raise ValueError(f"register flow: bad seed slot={slot!r} mode={mode!r}")
    lead = None
    if mode == "register" and await must_apply(interaction, reg):
        mode, plan_only, lead = "apply", False, NEWCOMER
    if mode == "apply":
        m = reg.members.get(interaction.user.id)
        if m and m.active():
            await interaction.response.send_message("You're already a member — add characters with `/register` or `/me char` → Add an alt.", ephemeral=True)
            return
        a = reg.applicants.get(interaction.user.id)  # their OWN application first (design §3.3 D): pending is never "not taking applications"
        if a and a.status == "open":
            await interaction.response.send_message(f"You already have an open application for **{a.label}**. An officer will get to it; you'll get a DM either way.",
                                                    ephemeral=True)
            return
        if not reg.config.recruitment.open:
            own = reg.application_state(interaction.user.id)
            await interaction.response.send_message((f"{lead}\n" if lead else "") + (f"{own['text']}\n" if own else "")
                                                    + f"{reg.config.name} isn't taking applications right now — check back later, or ask an officer.", ephemeral=True)
            return
    await RegisterWizard(reg, interaction.user.id, slot=slot, plan_only=plan_only, mode=mode, roles=roles, lead=lead).start(interaction)


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
        self.draft.update(slot="main" if c.is_main else "alt", cls=c.cls, spec=c.spec, offspec=c.offspec, label=c.label)

    async def picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        c = next((c for c in self.planned(interaction.user.id) if ("main" if c.is_main else "alt") == values[0]), None)
        if not c:
            await self.refuse(interaction, "That planned character is gone. Run `/me char` → Name again.")
            return
        self.choose(c)
        await self.open_form(interaction, self.form())

    def form(self) -> Form:
        d = self.draft
        return Form(f"Name your {d['cls']} ({d['slot']})", name_fields(first=d["first"], last=d["last"], first_optional=False), self.submitted)

    async def reopen(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.form())

    def current(self, uid: int) -> RegisteredCharacter | None:
        """The planned character shown, as the registry holds it NOW (re-planned or named meanwhile → None)."""
        d = self.draft
        now = next((c for c in self.planned(uid) if c.is_main == (d["slot"] == "main")), None)
        return now if now is not None and now.cls == d["cls"] and now.label == d.get("label") else None

    def rows(self) -> list[dict]:
        d = self.draft
        return [{"label": d["label"], "cls": d["cls"], "spec": d["spec"], "offspec": d.get("offspec"), "name": d["first"], "surname": d["last"] or None}]

    async def submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        from . import discord_people

        d, uid = self.draft, interaction.user.id
        d["first"], d["last"] = v.get("first", ""), v.get("last", "")
        if not d["first"]:
            await self.refuse(interaction, f"A first name is needed ({NAME_RULE}).", self.reopen)
            return
        if self.current(uid) is None:  # re-planned meanwhile: don't name a different character than the one shown
            await self.finish(interaction, f"❌ Your planned {d['slot']} changed since you started. Run `/me char` → Name again.")
            return
        try:  # every refusal the save would give (the letters, a name already registered), nothing written
            await discord_people.save_characters(interaction.client, self.reg, uid, self.rows(), interaction.user.display_name,
                                                 by_id=uid, preview=True, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen)
            return
        full = f"{self.reg.normalise_name(d['first'])}" + (f" {self.reg.normalise_name(d['last'])}" if d["last"] else "")
        effects = discord_people.character_preview(interaction.client, self.reg, uid, self.rows())
        what = f"Name your planned {d['slot']} **{d['label']}** → **{full}**. An officer confirms it afterwards."
        text = "\n".join([what, *[f"• {e}" for e in effects]]) if effects else what + "\n-# Nothing else changes: no sheet or seat holds it."
        await self.decide(interaction, text, do=self.save, do_label=f"Name it {full}"[:80])

    async def save(self, interaction: discord.Interaction, _note: str | None) -> None:
        """THE character verb: a one-row table naming the planned character — the ripple re-labels open sheet answers
        and locked seats that hold it, and the verb writes the one audit line."""
        from . import discord_people

        d, uid = self.draft, interaction.user.id
        if self.current(uid) is None:
            await self.finish(interaction, f"❌ Your planned {d['slot']} changed since you started. Run `/me char` → Name again.")
            return
        try:
            res = await discord_people.save_characters(interaction.client, self.reg, uid, self.rows(), interaction.user.display_name, by_id=uid, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.reopen)
            return
        m = self.reg.members.get(uid)
        c = next((x for x in (m.active() if m else []) if x.name and x.label.lower() == f"{d['first']} {d['last']}".strip().lower()), None) if m else None
        head = f"✅ {_char_line(interaction, c)} — an officer will confirm it." if c else "✅ Named — an officer will confirm it."
        await self.finish(interaction, head + "".join(f"\n• {line}" for line in res["ripple"]))


@flow("char_name")
async def open_char_name(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    await CharNameWizard(reg, interaction.user.id).start(interaction)


__all__ = ["RegisterWizard", "CharNameWizard", "char_opts", "name_fields", "open_register", "open_char_name"]
