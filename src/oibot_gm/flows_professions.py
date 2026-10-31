"""/me professions · /roster professions (design §5.60): a hub per character — its professions, skills and notable
recipes — with Add profession… · Change skill… · Add recipe… · Remove…, each ending on THE decide screen with
consequences.profession_consequence's sentences, written as a one-row table through MemberWizard.save_rows (THE
character verb). Profession and skill are selects (the Wizard rule: a number is a preset select); a recipe's name is
one prose field, validated by Registry.check_professions, never parsed. Never a DM."""
from __future__ import annotations

import discord

from . import consequences as cq
from .flows_roster import MemberWizard, _ico, _stop_at_entry
from .registry import RECIPE_NAME_MAX, RegisteredCharacter, Registry, RegistryError
from .wizard import Field, Form, Opt, flow

NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four"}


def skill_steps(cap: int) -> list[int]:
    """The skill a person picks: 1 (just learned), every 25 up to 25 below the cap, then every 5 to the cap — the last
    stretch is where the notable recipes sit (285, 290, 300)."""
    top = list(range(max(cap - 20, 25), cap + 1, 5))
    return [1] + [s for s in range(25, cap - 20, 25) if s < top[0]] + top


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class ProfessionsWizard(MemberWizard):
    """One member's characters' professions: `/me professions` (self mode) and `/roster professions member:` (an officer)."""

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User, *, self_mode: bool):
        super().__init__(reg, owner_id, member, title="Your professions" if self_mode else "Professions", self_mode=self_mode)

    # ---- entry and the character
    async def start(self, interaction: discord.Interaction) -> None:
        m = self.member()
        chars = self.indexed()
        if not m or not chars:
            await self.finish(interaction, "No characters yet — `/register` (or the registration card) adds your main, then its professions." if self.self_mode
                              else f"{self.member_name} has no registered characters, so there is nothing to record professions on.")
            return
        keep = self.draft.get("label")
        self.draft = {"rev": self.reg.characters_rev(m)}
        hit = next(((i, c) for i, c in chars if c.label == keep), None) or (chars[0] if len(chars) == 1 else None)
        if hit:
            self.draft.update(idx=hit[0], label=hit[1].label)
            await self.hub(interaction)
            return
        await self.pick_char(interaction)

    async def pick_char(self, interaction: discord.Interaction) -> None:
        await self.show(interaction, "Whose professions?", self.select("Pick the character", self.char_opts(self.indexed(), _ico(interaction), self.draft.get("idx")), self.char_picked),
                        self.button("Cancel", self.cancel))

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        idx = int(values[0])
        self.draft.update(idx=idx, label=self.member().characters[idx].label)
        await self.hub(interaction)

    def char(self) -> RegisteredCharacter:
        return self.reread()[1]

    def entries(self, c: RegisteredCharacter) -> list[dict]:
        return [p.model_dump() for p in c.professions]

    def name(self, pid: str) -> str:
        return self.reg.profile.profession(pid).name

    def trades_full(self, c: RegisteredCharacter) -> bool:
        prof = self.reg.profile
        return sum(1 for p in c.professions if prof.profession(p.id).group != "secondary") >= prof.profession_rules["trade_slots"]

    def open_professions(self, c: RegisteredCharacter):
        have, full = {p.id for p in c.professions}, self.trades_full(c)
        return [d for d in self.reg.profile.professions.values() if d.id not in have and not (full and d.group != "secondary")]

    def makers(self, c: RegisteredCharacter):
        return [p for p in c.professions if self.reg.profile.profession(p.id).makes]

    # ---- the hub
    async def hub(self, interaction: discord.Interaction) -> None:
        try:
            c = self.char()
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        self.draft.pop("pid", None)
        prof = self.reg.profile
        if c.professions:
            lines = [f"**{c.label}**"] + [f"• **{self.name(p.id)} {p.skill}**" + (" · " + ", ".join(r.name for r in p.recipes) if p.recipes else "")
                                          for p in c.professions]
        else:
            lines = [f"**{c.label}** · {self.reg.professions_summary(c)}. Press Add profession to record the first one."]
        if self.trades_full(c):
            slots = prof.profession_rules["trade_slots"]
            free = [d.name for d in self.open_professions(c)]
            lines.append(f"{NUMBER_WORDS.get(slots, str(slots))} trades are taken." + (f" {_join(free)} {'is' if len(free) == 1 else 'are'} open." if free else ""))
        items = []
        if self.open_professions(c):
            items.append(self.button("Add profession…", self.add_profession, discord.ButtonStyle.primary))
        if c.professions:
            items.append(self.button("Change skill…", self.change_skill))
        if self.makers(c):
            items.append(self.button("Add recipe…", self.add_recipe))
        if c.professions:
            items.append(self.button("Remove…", self.remove))
        if len(self.indexed()) > 1:
            items.append(self.button("Other character…", self.pick_char))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, "\n".join(lines)[:1800], *items)

    # ---- add a profession / change a skill: profession select → skill select → decide
    async def add_profession(self, interaction: discord.Interaction) -> None:
        c = self.char()
        opts = [Opt(d.id, d.name, d.group) for d in self.open_professions(c)]
        if not opts:
            await self.refuse(interaction, f"{c.label} already has every profession it can take.", self.hub, "Back")
            return
        await self.show(interaction, f"Which profession does **{c.label}** have?", self.select("Pick the profession", opts, self.profession_picked),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def change_skill(self, interaction: discord.Interaction) -> None:
        c = self.char()
        opts = [Opt(p.id, self.name(p.id), f"skill {p.skill}") for p in c.professions]
        await self.show(interaction, f"Which of **{c.label}**'s professions changed?", self.select("Pick the profession", opts, self.profession_picked),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def profession_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["pid"] = values[0]
        c = self.char()
        now = next((p.skill for p in c.professions if p.id == values[0]), None)
        steps = skill_steps(self.reg.profile.profession_rules["skill_cap"])
        if now is not None and now not in steps:
            steps = sorted({*steps, now})
        opts = [Opt(str(s), str(s), "now" if s == now else None, default=s == now) for s in steps]
        await self.show(interaction, f"How far along is **{c.label}**'s {self.name(values[0])}?", self.select("Pick the skill", opts, self.skill_picked),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def skill_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        c, pid = self.char(), self.draft["pid"]
        entries = self.entries(c)
        hit = next((e for e in entries if e["id"] == pid), None)
        if hit is None:
            entries.append({"id": pid, "skill": int(values[0]), "recipes": []})
        else:
            hit["skill"] = int(values[0])
        await self.review(interaction, entries)

    # ---- add a recipe: (which profession makes it) → one prose field → decide
    async def add_recipe(self, interaction: discord.Interaction) -> None:
        c = self.char()
        makers = self.makers(c)
        if not makers:
            await self.refuse(interaction, f"None of {c.label}'s professions makes things, so it has no recipes to add.", self.hub, "Back")
            return
        if len(makers) == 1:
            self.draft["pid"] = makers[0].id
            await self.recipe_form(interaction)
            return
        await self.show(interaction, "Which of the professions makes the recipe?",
                        self.select("Which profession makes it?", [Opt(p.id, self.name(p.id), f"skill {p.skill}") for p in makers], self.recipe_profession),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def recipe_profession(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["pid"] = values[0]
        await self.recipe_form(interaction)

    async def recipe_form(self, interaction: discord.Interaction) -> None:
        field = Field("recipe", "The recipe's name", description="Kept as you write it, like Devilsaur Gauntlets.", max_length=RECIPE_NAME_MAX)
        await self.open_form(interaction, Form(f"A {self.name(self.draft['pid'])} recipe", [field], self.recipe_typed))

    async def recipe_typed(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        if interaction.user.id != self.owner_id:  # a Modal has no interaction_check
            await interaction.response.send_message("This belongs to someone else's wizard.", ephemeral=True)
            return
        why = await self.guard(interaction) if self.guard else None
        if why:
            await self.finish(interaction, why)
            return
        c = self.char()
        entries = self.entries(c)
        hit = next((e for e in entries if e["id"] == self.draft["pid"]), None)
        if hit is None:
            await self.refuse(interaction, f"{self.name(self.draft['pid'])} is no longer on {c.label}'s record.", self.hub, "Back")
            return
        hit["recipes"].append({"name": values.get("recipe", ""), "id": None})
        await self.review(interaction, entries)

    # ---- remove a profession or one recipe
    async def remove(self, interaction: discord.Interaction, page: int = 0) -> None:
        c = self.char()
        opts = [Opt(f"p:{p.id}", self.name(p.id), "the profession and its recipes") for p in c.professions]
        opts += [Opt(f"r:{p.id}:{i}", r.name, f"a {self.name(p.id)} recipe") for p in c.professions for i, r in enumerate(p.recipes)]
        await self.show(interaction, f"What comes off **{c.label}**'s record?", *self.paged("Pick what to remove", opts, page, self.remove_picked, self._remove_page),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    def _remove_page(self, page: int):
        async def go(interaction: discord.Interaction) -> None:
            await self.remove(interaction, page)
        return go

    async def remove_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        c = self.char()
        entries = self.entries(c)
        kind, _, rest = values[0].partition(":")
        if kind == "p":
            entries = [e for e in entries if e["id"] != rest]
        else:
            pid, _, idx = rest.partition(":")
            hit = next((e for e in entries if e["id"] == pid), None)
            if hit is not None:
                hit["recipes"] = [r for i, r in enumerate(hit["recipes"]) if i != int(idx)]
        await self.review(interaction, entries)

    # ---- THE confirm: profession_consequence's sentences, then one table save
    async def review(self, interaction: discord.Interaction, entries: list[dict]) -> None:
        try:
            c = self.char()
            said = cq.profession_consequence(self.reg, self.member(), c.label, entries, by_id=interaction.user.id)
            summary = self.reg.professions_summary(c.model_copy(update={"professions": self.reg.check_professions(entries)}))
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.hub, "Back")
            return
        self.draft["entries"] = entries
        label = c.label

        async def save(i: discord.Interaction, _note: str | None) -> None:
            await self.save_rows(i, [{"label": label, "professions": self.draft["entries"]}], f"{label}: {summary}.")

        await self.decide(interaction, "\n".join(said), do=save)


@flow("me_professions")
async def open_me_professions(interaction: discord.Interaction, reg: Registry) -> None:
    """`/me professions`: your own characters' professions."""
    await ProfessionsWizard(reg, interaction.user.id, interaction.user, self_mode=True).start(interaction)


@flow("roster_professions")
async def open_roster_professions(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User) -> None:
    """`/roster professions member:`: an officer on someone's record (yourself → your own, self mode)."""
    if member.id == interaction.user.id:
        await ProfessionsWizard(reg, interaction.user.id, interaction.user, self_mode=True).start(interaction)
        return
    if await _stop_at_entry(interaction, reg):
        return
    await ProfessionsWizard(reg, interaction.user.id, member, self_mode=False).start(interaction)
