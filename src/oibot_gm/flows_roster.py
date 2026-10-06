"""Officer roster wizards: `/roster confirm` (walk the unconfirmed characters), `/roster rank`, `/roster set-main`,
and the REGULARS of a recurring schedule: `/roster add`, `/roster remove`, `/roster members` (schedule → member →
character), plus a member's own Step down (/me view). The member comes from the slash command's native picker (or a
picker here); everything else — which character, which rank, which schedule — is picked from what the registry holds,
so nothing is typed and nothing can name a character or schedule that doesn't exist.

Every flow is officer-only (checked at entry and on every press) and writes nothing before the final Confirm; the
save re-reads the registry, because another officer may have confirmed, retired or moved the character meanwhile."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

import discord

from .registry import RANKS, Member, RegisteredCharacter, Registry, RegistryError
from .wizard import MAX_OPTIONS, Opt, Wizard, flow

log = logging.getLogger(__name__)

OFFICERS_ONLY = "Officers only (Manage Server or a configured officer role)."
PLAYER_RANKS = ("trial", "raider", "core")  # the ranks set_main carries from the old main to the new one


# ---------------------------------------------------------------- shared pieces

def _ico(interaction: discord.Interaction):
    return getattr(interaction.client, "ico", None)


async def _emit(interaction: discord.Interaction, reg: Registry, text: str) -> None:
    ops = getattr(interaction.client, "ops", None)
    if ops is None:
        return
    try:
        await ops.emit(reg.config, "info", text)
    except Exception:  # noqa: BLE001 — the ops line never undoes a saved change
        log.exception("ops emit failed")


def _is_officer(interaction: discord.Interaction, reg: Registry) -> bool:
    from .discord_registry import is_officer

    return is_officer(interaction, reg)


async def _stop_at_entry(interaction: discord.Interaction, reg: Registry) -> bool:
    """True (and the person told) when they are not an officer."""
    if _is_officer(interaction, reg):
        return False
    await interaction.response.send_message(OFFICERS_ONLY, ephemeral=True)
    return True


def roster_name(reg: Registry, key: str) -> str:
    """A roster as a person reads it: its configured name (a run's roster is named after the run), else its key."""
    return (reg.config.roster(key) or {}).get("name") or key


def _char_desc(c: RegisteredCharacter) -> str:
    bits = [f"{c.cls} {c.spec}"]
    if c.is_main:
        bits.append("main")
    bits.append(c.rank)
    if c.status == "planned":
        bits.append("planned")
    elif not c.confirmed_by:
        bits.append("unconfirmed")
    return " · ".join(bits)


class OfficerWizard(Wizard):
    """A Wizard whose every press re-checks that the presser is still an officer."""

    def __init__(self, reg: Registry, owner_id: int, *, title: str):
        async def still_officer(i: discord.Interaction) -> str | None:
            return None if _is_officer(i, reg) else OFFICERS_ONLY

        super().__init__(reg, owner_id, title=title, guard=still_officer)

    def paged(self, placeholder: str, opts: Sequence[Opt], page: int, on_pick: Callable[[discord.Interaction, list[str]], Awaitable[None]],
              on_page: Callable[[int], Callable[[discord.Interaction], Awaitable[None]]]) -> list[discord.ui.Item]:
        """A select over `opts`, 25 at a time with ← / More → buttons: a long list is paged, never clipped."""
        opts = list(opts)
        pages = max(1, -(-len(opts) // MAX_OPTIONS))
        page = min(max(0, page), pages - 1)
        chunk = opts[page * MAX_OPTIONS:(page + 1) * MAX_OPTIONS]
        ph = placeholder if pages == 1 else f"{placeholder} ({page + 1}/{pages})"
        items: list[discord.ui.Item] = [self.select(ph, chunk, on_pick)]
        if page > 0:
            items.append(self.button("← Previous", on_page(page - 1)))
        if page < pages - 1:
            items.append(self.button("More →", on_page(page + 1)))
        return items


class MemberWizard(OfficerWizard):
    """An officer acting on one member's characters. Characters are picked by their index in the member's list and
    re-found by label at save, so a character retired, renamed or reordered meanwhile is refused, never guessed."""

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User, *, title: str):
        super().__init__(reg, owner_id, title=f"{title} · {member.display_name}")
        self.member_id, self.member_name = member.id, member.display_name

    def member(self) -> Member | None:
        return self.reg.members.get(self.member_id)

    def char_opts(self, chars: list[tuple[int, RegisteredCharacter]], ico, current: int | None = None) -> list[Opt]:
        return [Opt(str(i), c.label, _char_desc(c), emoji=ico("class", c.cls) if ico else None, default=i == current) for i, c in chars]

    def indexed(self, keep: Callable[[RegisteredCharacter], bool] = lambda c: True) -> list[tuple[int, RegisteredCharacter]]:
        m = self.member()
        if not m:
            return []
        rows = [(i, c) for i, c in enumerate(m.characters) if c.status in ("active", "planned") and keep(c)]
        return sorted(rows, key=lambda ic: (not ic[1].is_main, ic[1].label.lower()))

    def reread(self) -> tuple[Member, RegisteredCharacter]:
        """The picked character as the registry holds it NOW, or a readable refusal."""
        m = self.member()
        idx, label = self.draft["idx"], self.draft["label"]
        if not m:
            raise RegistryError(f"{self.member_name} has no registered characters any more.")
        c = m.characters[idx] if 0 <= idx < len(m.characters) else None
        if c is None or c.label != label:
            c = next((x for x in m.characters if x.label == label), None)
        if c is None:
            raise RegistryError(f"{label} is no longer registered to {m.display_name}.")
        if c.status not in ("active", "planned"):
            raise RegistryError(f"{label} was retired meanwhile.")
        return m, c

    def check_unique_on_member(self, m: Member, c: RegisteredCharacter) -> None:
        """Registry calls take a name; make sure that name resolves to THIS character on this member."""
        first = next((x for x in m.active() if x.matches(c.label)), None)
        if first is not c:
            raise RegistryError(f"{m.display_name} has two characters called {c.label}; change this one on the Members page.")

    def check_unique_in_guild(self, m: Member, c: RegisteredCharacter) -> None:
        """set_rank / confirm find a character guild-wide by name; refuse rather than touch someone else's."""
        hit = self.reg.find(c.label)
        if not hit or hit[1] is not c:
            raise RegistryError(f"Another character is also called {c.label}; change this one on the Members page.")


# ---------------------------------------------------------------- /roster confirm

class ConfirmWizard(OfficerWizard):
    """The unconfirmed characters as a list; pick one, confirm it, and land back on the rest ('Confirm next')."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Confirm characters")
        self.page = 0
        self.done: list[str] = []

    def pending_opts(self, ico) -> list[Opt]:
        return [Opt(c.label, f"{c.label} · {c.cls} {c.spec} · {m.display_name}"[:100], f"{c.rank}" + (" · main" if c.is_main else " · alt"),
                    emoji=ico("class", c.cls) if ico else None) for m, c in self.reg.pending()]

    async def start(self, interaction: discord.Interaction, name: str | None = None) -> None:
        if name and any(c.label == name for _, c in self.reg.pending()):
            await self.ask(interaction, name)
            return
        await self.listing(interaction)

    async def listing(self, interaction: discord.Interaction, head: str = "") -> None:
        opts = self.pending_opts(_ico(interaction))
        if not opts:
            await self.finish(interaction, (head + "\n" if head else "") + "Nothing left to confirm — every registered character is confirmed.")
            return
        n = len(opts)
        text = (head + "\n" if head else "") + f"{n} character{'s' if n != 1 else ''} waiting for an officer. Pick one to confirm."
        items = self.paged("Pick a character to confirm", opts, self.page, self.picked, self.to_page)
        if head:  # after a confirm: the next one is one press away
            items.append(self.button("Confirm next", self.next_one, discord.ButtonStyle.primary))
        items.append(self.button("Done" if self.done else "Cancel", self.close if self.done else self.cancel))
        await self.show(interaction, text, *items)

    def to_page(self, page: int):
        async def go(interaction: discord.Interaction) -> None:
            self.page = page
            await self.listing(interaction)
        return go

    async def picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        await self.ask(interaction, values[0])

    async def next_one(self, interaction: discord.Interaction) -> None:
        pend = self.reg.pending()
        if not pend:
            await self.listing(interaction)
            return
        await self.ask(interaction, pend[0][1].label)

    async def ask(self, interaction: discord.Interaction, label: str) -> None:
        hit = next(((m, c) for m, c in self.reg.pending() if c.label == label), None)
        if not hit:
            await self.listing(interaction, f"❌ {label} is no longer waiting (confirmed or retired meanwhile).")
            return
        m, c = hit
        self.draft = {"label": label, "member_id": m.discord_id}
        others = [x.label for x in m.active() if x is not c]
        lines = [f"Confirm **{c.label}** · {c.cls} {c.spec}{'/' + c.offspec if c.offspec else ''} · {'main' if c.is_main else 'alt'}, registered by **{m.display_name}**?",
                 f"-# Their other characters: {', '.join(others)}" if others else "-# Their only character.",
                 "-# They get a DM saying you confirmed it."]
        await self.show(interaction, "\n".join(lines),
                        self.button("Confirm", self.save, discord.ButtonStyle.success),
                        self.button("Back", self.back),
                        self.button("Cancel" if not self.done else "Done", self.cancel if not self.done else self.close))

    async def back(self, interaction: discord.Interaction) -> None:
        await self.listing(interaction, f"Confirmed so far: {', '.join(self.done)}" if self.done else "")

    async def close(self, interaction: discord.Interaction) -> None:
        await self.finish(interaction, f"✅ Confirmed {len(self.done)}: {', '.join(self.done)}.")

    async def save(self, interaction: discord.Interaction) -> None:
        label, member_id = self.draft["label"], self.draft["member_id"]
        hit = self.reg.find(label)  # re-read: another officer may have got here first
        if not hit or hit[0].discord_id != member_id:
            await self.refuse(interaction, f"{label} is no longer registered (retired or removed meanwhile).", self.back, "Back to the list")
            return
        m, c = hit
        if c.status != "active":
            await self.refuse(interaction, f"{label} was retired meanwhile.", self.back, "Back to the list")
            return
        if c.confirmed_by:
            await self.refuse(interaction, f"{label} was already confirmed by {c.confirmed_by}.", self.back, "Back to the list")
            return
        await self.working(interaction, f"Confirming {label}…")
        by = interaction.user.display_name
        try:
            m, c = self.reg.confirm(label, by)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.back, "Back to the list")
            return
        self.done.append(c.label)
        await _emit(interaction, self.reg, f"{by} confirmed {c.label} ({m.display_name})")
        try:
            user = await interaction.client.fetch_user(m.discord_id)
            await user.send(f"✅ {c.label} was confirmed by {by}. Welcome aboard.")
        except Exception:  # noqa: BLE001 — closed DMs, a test puppet, a user who left
            pass
        self.page = 0
        await self.listing(interaction, f"✅ Confirmed **{c.label}** ({m.display_name}).")


@flow("roster_confirm")
async def open_roster_confirm(interaction: discord.Interaction, reg: Registry, *, name: str | None = None) -> None:
    """`/roster confirm` and the roster list's *Confirm N characters* button. `name` jumps straight to one."""
    if await _stop_at_entry(interaction, reg):
        return
    await ConfirmWizard(reg, interaction.user.id).start(interaction, name)


flow("roster_confirm_all")(open_roster_confirm)  # the same walk, opened from the /roster list button


def confirm_pending_view(reg: Registry, count: int) -> discord.ui.View | None:
    """The `/roster list` reply's button: a plain ephemeral View (not persistent) that opens the confirm walk."""
    if count <= 0:
        return None
    view = discord.ui.View(timeout=600)
    b = discord.ui.Button(label=f"Confirm {count} character{'s' if count != 1 else ''}", style=discord.ButtonStyle.primary)

    async def press(interaction: discord.Interaction) -> None:
        await open_roster_confirm(interaction, reg)
    b.callback = press  # type: ignore[method-assign]
    view.add_item(b)
    return view


# ---------------------------------------------------------------- /roster rank

class RankWizard(MemberWizard):
    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User):
        super().__init__(reg, owner_id, member, title="Rank")

    async def start(self, interaction: discord.Interaction) -> None:
        chars = self.indexed()
        if not chars:
            await self.finish(interaction, f"{self.member_name} has no registered characters.")
            return
        if len(chars) == 1:
            await self.char_picked(interaction, [str(chars[0][0])])
            return
        await self.pick_char(interaction)

    async def pick_char(self, interaction: discord.Interaction) -> None:
        chars = self.indexed()
        await self.show(interaction, "Which character?",
                        self.select("Pick the character", self.char_opts(chars, _ico(interaction), self.draft.get("idx")), self.char_picked),
                        self.button("Cancel", self.cancel))

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        idx = int(values[0])
        m = self.member()
        c = m.characters[idx]
        self.draft.update(idx=idx, label=c.label, old=c.rank, many=len(self.indexed()) > 1)
        await self.pick_rank(interaction)

    def _follows(self, c: RegisteredCharacter) -> str:
        if c.is_main:
            return "-# This is their **main**: rank belongs to the player and follows the main — if they switch mains, the rank moves with them."
        return "-# This is an **alt** (alts are usually rank alt; the player's standing is the main's rank)."

    async def pick_rank(self, interaction: discord.Interaction) -> None:
        c = self.member().characters[self.draft["idx"]]
        items = [self.select("Pick the rank", [Opt(r, r, "current" if r == c.rank else None, default=r == c.rank) for r in RANKS], self.rank_picked)]
        if self.draft["many"]:
            items.append(self.button("Back", self.pick_char))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, f"**{c.label}** · {c.cls} {c.spec} is **{c.rank}**. New rank?\n{self._follows(c)}", *items)

    async def rank_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["rank"] = values[0]
        m = self.member()
        c = m.characters[self.draft["idx"]]
        if values[0] == c.rank:
            await self.show(interaction, f"**{c.label}** is already **{c.rank}** — pick a different rank, or cancel.",
                            self.button("Pick again", self.pick_rank, discord.ButtonStyle.primary), self.button("Cancel", self.cancel))
            return
        await self.show(interaction, f"**{c.label}** ({self.member_name}): **{c.rank}** → **{values[0]}**?\n{self._follows(c)}",
                        self.button("Confirm", self.save, discord.ButtonStyle.success),
                        self.button("Back", self.pick_rank),
                        self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        rank, by = self.draft["rank"], interaction.user.display_name
        try:
            m, c = self.reread()
            if c.rank != self.draft["old"]:
                raise RegistryError(f"{c.label}'s rank changed meanwhile (now {c.rank}).")
            self.check_unique_in_guild(m, c)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.working(interaction, "Saving…")
        try:
            m, c = self.reg.set_rank(c.label, rank, by)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await _emit(interaction, self.reg, f"{by} set {c.label} → {rank}")
        await self.finish(interaction, f"✅ **{c.label}** ({m.display_name}) is now **{rank}**.")


@flow("roster_rank")
async def open_roster_rank(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await RankWizard(reg, interaction.user.id, member).start(interaction)


# ---------------------------------------------------------------- /roster set-main

class SetMainWizard(MemberWizard):
    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User):
        super().__init__(reg, owner_id, member, title="Set main")

    async def start(self, interaction: discord.Interaction) -> None:
        chars = self.indexed(lambda c: not c.is_main)
        if not chars:
            has = bool(self.indexed())
            await self.finish(interaction, f"{self.member_name} has no other character to make main." if has else f"{self.member_name} has no registered characters.")
            return
        self.draft["many"] = len(chars) > 1
        if len(chars) == 1:
            await self.char_picked(interaction, [str(chars[0][0])])
            return
        await self.pick_char(interaction)

    async def pick_char(self, interaction: discord.Interaction) -> None:
        m = self.member()
        cur = f"Their main is **{m.main.label}**." if m and m.main else "They have no main."
        await self.show(interaction, f"{cur} Which character becomes the main?",
                        self.select("Pick the new main", self.char_opts(self.indexed(lambda c: not c.is_main), _ico(interaction)), self.char_picked),
                        self.button("Cancel", self.cancel))

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        idx = int(values[0])
        m = self.member()
        c, old = m.characters[idx], m.main
        self.draft.update(idx=idx, label=c.label, old=old.label if old else None)
        lines = [f"Make **{c.label}** · {c.cls} {c.spec} {self.member_name}'s main" + (f" (instead of **{old.label}**)?" if old else "?")]
        if old and old.rank in PLAYER_RANKS:
            lines.append(f"-# Rank follows the player: {c.label} becomes **{old.rank}**, {old.label} becomes **alt**.")
        items = [self.button("Confirm", self.save, discord.ButtonStyle.success)]
        if self.draft.get("many"):
            items.append(self.button("Back", self.pick_char))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, "\n".join(lines), *items)

    async def save(self, interaction: discord.Interaction) -> None:
        by = interaction.user.display_name
        try:
            m, c = self.reread()
            if c.is_main:
                raise RegistryError(f"{c.label} is already {m.display_name}'s main.")
            cur = m.main.label if m.main else None
            if cur != self.draft["old"]:
                raise RegistryError(f"{m.display_name}'s main changed meanwhile (now {cur or 'none'}).")
            self.check_unique_on_member(m, c)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.working(interaction, "Saving…")
        try:
            m, c, old = self.reg.officer_set_main(self.member_id, c.label, by)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await _emit(interaction, self.reg, f"{by} set {m.display_name}'s main → {c.label}")
        await self.finish(interaction, f"✅ {m.display_name}'s main is now **{c.label}**" + (f" (rank {c.rank}; {old.label} is {old.rank})." if old else "."))


@flow("roster_set_main")
async def open_roster_set_main(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await SetMainWizard(reg, interaction.user.id, member).start(interaction)


# ---------------------------------------------------------------- regulars: /roster add · remove · members, Step down
# A recurring series ("<raid>:<schedule>") has REGULARS: officers pick the schedule, then the member (the slash command's
# optional member widget skips that step), then the character. Nothing is typed (the wizard rule); the save re-reads the
# registry and goes through Registry.regular_add / regular_remove — the verbs the site, plain text and MCP call too.

def _spec_line(ico, c: RegisteredCharacter) -> str:
    """One member per line: spec icon + character (the iconography rule — the icon stands for role + spec)."""
    return ((ico("spec", f"{c.cls}:{c.spec}") + " ") if ico else "") + c.label


class RegularsWizard(OfficerWizard):
    """Schedule → member → character → confirm. `member` (a discord user, optional) is pre-picked."""

    verb = "add"

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User | None, *, title: str):
        super().__init__(reg, owner_id, title=title + (f" · {member.display_name}" if member else ""))
        self.member_id = member.id if member else None
        self.preset = member is not None
        self.page = 0

    # ---- shared
    def member(self) -> Member | None:
        return self.reg.members.get(self.member_id) if self.member_id else None

    def label(self) -> str:
        return self.reg.series_label(self.draft["series"])

    def series_opts(self) -> list[Opt]:
        out = []
        for key, label in self.reg.series("recurring"):
            n = len(self.reg.regulars(key))
            m = self.member()
            on = next((c.label for c in (m.active() if m else []) if key in c.rosters), None)
            out.append(Opt(key, label[:100], (f"already a regular as {on} · " if on else "") + f"{n} regular{'s' if n != 1 else ''}", default=key == self.draft.get("series")))
        return out

    def to_page(self, screen):
        def make(page: int):
            async def go(interaction: discord.Interaction) -> None:
                self.page = page
                await screen(interaction)
            return go
        return make

    async def pick_series(self, interaction: discord.Interaction) -> None:
        opts = self.series_opts()
        if not opts:
            await self.finish(interaction, "No recurring schedule yet — the owner adds weekly run times on the Raids page (or /gm config raid). "
                                           "Ad hoc runs (pickup templates, one-offs) have no regulars: first come, first rostered.")
            return
        if len(opts) == 1:
            await self.series_picked(interaction, [opts[0].value])
            return
        self.draft["many_series"] = True
        await self.show(interaction, "Which recurring schedule?", *self.paged("Pick the schedule", opts, self.page, self.series_picked, self.to_page(self.pick_series)),
                        self.button("Cancel", self.cancel))

    async def series_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["series"] = values[0]
        self.page = 0
        if self.preset:
            await self.after_member(interaction)
        else:
            await self.pick_member(interaction)

    def back_to_series(self):
        return self.pick_series if self.draft.get("many_series") else None


class RegularAddWizard(RegularsWizard):
    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User | None):
        super().__init__(reg, owner_id, member, title="Add a regular")

    async def start(self, interaction: discord.Interaction) -> None:
        m = self.member()
        if self.preset and (m is None or not m.active()):
            await self.finish(interaction, "They have no registered characters — they register first (the registration card or /register).")
            return
        await self.pick_series(interaction)

    def member_opts(self, ico) -> list[Opt]:
        key = self.draft["series"]
        rows = sorted((m for m in self.reg.members.values() if not m.test and m.active()), key=lambda m: m.display_name.lower())
        out = []
        for m in rows:
            on = next((c for c in m.active() if key in c.rosters), None)
            main = m.main or m.active()[0]
            out.append(Opt(str(m.discord_id), m.display_name[:100], (f"regular as {on.label}" if on else f"{main.label} · {main.cls} {main.spec}")[:100],
                           emoji=ico("class", main.cls) if ico else None))
        return out

    async def pick_member(self, interaction: discord.Interaction) -> None:
        opts = self.member_opts(_ico(interaction))
        if not opts:
            await self.finish(interaction, "Nobody has registered a character yet.")
            return
        items = self.paged("Pick the member", opts, self.page, self.member_picked, self.to_page(self.pick_member))
        back = self.back_to_series()
        await self.show(interaction, f"Who becomes a regular of **{self.label()}**?", *items, *([self.button("Back", back)] if back else []), self.button("Cancel", self.cancel))

    async def member_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.member_id = int(values[0])
        await self.after_member(interaction)

    async def after_member(self, interaction: discord.Interaction) -> None:
        m = self.member()
        chars = sorted(m.active(), key=lambda c: (not c.is_main, c.label.lower())) if m else []
        if not chars:
            await self.refuse(interaction, "They have no registered characters any more.", self.pick_series, "Start over")
            return
        if len(chars) == 1:
            await self.char_picked(interaction, [chars[0].label])
            return
        self.draft["many_chars"] = True
        ico = _ico(interaction)
        opts = [Opt(c.label, c.label, _char_desc(c), emoji=ico("class", c.cls) if ico else None, default=c.label == self.draft.get("char")) for c in chars][:MAX_OPTIONS]
        items = [self.select("Pick the character", opts, self.char_picked)]
        if m.main:
            items.append(self.button(f"Their main ({m.main.label})", self._main, discord.ButtonStyle.primary))
        back = self.pick_member if not self.preset else self.back_to_series()
        await self.show(interaction, f"Which of {m.display_name}'s characters raids on **{self.label()}**? Usually the main.",
                        *items, *([self.button("Back", back)] if back else []), self.button("Cancel", self.cancel))

    async def _main(self, interaction: discord.Interaction) -> None:
        await self.char_picked(interaction, [self.member().main.label])

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        m = self.member()
        c = next((x for x in m.active() if x.label == values[0]), None) if m else None
        if c is None:
            await self.refuse(interaction, f"{values[0]} is no longer registered to them.", self.pick_series, "Start over")
            return
        self.draft["char"] = c.label
        key = self.draft["series"]
        back = self.after_member if self.draft.get("many_chars") else (self.pick_member if not self.preset else self.back_to_series())
        if key in c.rosters:
            await self.show(interaction, f"**{c.label}** is already a regular of **{self.label()}** — nothing to add.",
                            *([self.button("Back", back)] if back else []), self.button("Cancel", self.cancel))
            return
        lines = [f"Make **{c.label}** · {c.cls} {c.spec} ({m.display_name}) a regular of **{self.label()}**?",
                 "-# Regulars are expected on every sheet of it: officers see who hasn't answered, the reminder goes to them, and they're seated ahead of others at lock. Nothing is answered for them."]
        other = next((x for x in m.active() if x is not c and key in x.rosters), None)
        if other:
            lines.append(f"-# One character per member per schedule: this replaces **{other.label}** there.")
        rid = key.partition(":")[0]
        twin = [k for k in c.rosters if k != key and k.partition(":")[0] == rid]
        if twin:
            lines.append(f"-# ⚠️ {c.label} is also a regular of {', '.join(self.reg.series_label(k) for k in twin)} — the same raid, so the same lockout.")
        await self.show(interaction, "\n".join(lines), self.button("Confirm", self.save, discord.ButtonStyle.success),
                        *([self.button("Back", back)] if back else []), self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        key, by = self.draft["series"], interaction.user.display_name
        await self.working(interaction, "Saving…")
        try:
            m, c, warn = self.reg.regular_add(self.member_id, key, by, self.draft["char"])
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.pick_series, "Start over")
            return
        await _emit(interaction, self.reg, f"{by} made {m.display_name} ({c.label}) a regular of {self.label()}")
        n = len(self.reg.regulars(key))
        await self.finish(interaction, f"✅ **{c.label}** ({m.display_name}) is a regular of **{self.label()}** ({n} regular{'s' if n != 1 else ''})."
                          + "".join(f"\n-# ⚠️ {w}" for w in warn))


class RegularRemoveWizard(RegularsWizard):
    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User | None, *, self_service: bool = False):
        super().__init__(reg, owner_id, member, title="Step down" if self_service else "Remove a regular")
        self.self_service = self_service

    def held(self) -> list[tuple[str, str]]:
        """(series key, label) to pick from: the preset member's series (orphans included), else every series with regulars."""
        m = self.member()
        if self.preset:
            return [(k, self.reg.series_label(k)) for k, _c in (self.reg.regular_series_of(m) if m else [])]
        keys = {k for k, _l in self.reg.series("recurring") if self.reg.regulars(k)} | {k for k, _m, _c in self.reg.orphan_regulars()}
        return [(k, self.reg.series_label(k)) for k in sorted(keys, key=lambda k: self.reg.series_label(k).lower())]

    def series_opts(self) -> list[Opt]:
        m = self.member()
        on = dict(self.reg.regular_series_of(m)) if m else {}
        return [Opt(k, label[:100], (f"as {on[k].label}" if k in on else f"{len(self.reg.roster_members(k))} regular(s)"), default=k == self.draft.get("series")) for k, label in self.held()]

    async def start(self, interaction: discord.Interaction) -> None:
        if not self.held():
            who = "You aren't" if self.self_service else (f"{self.member().display_name} isn't" if self.member() else "Nobody is")
            await self.finish(interaction, f"{who} a regular of any recurring raid — nothing to remove.")
            return
        await self.pick_series(interaction)

    async def pick_member(self, interaction: discord.Interaction) -> None:
        key = self.draft["series"]
        ico = _ico(interaction)
        opts = [Opt(str(m.discord_id), m.display_name[:100], f"{c.label} · {c.cls} {c.spec}"[:100], emoji=ico("class", c.cls) if ico else None)
                for m, c in sorted(self.reg.roster_members(key), key=lambda mc: mc[0].display_name.lower())]
        if not opts:
            await self.refuse(interaction, f"{self.label()} has no regulars any more.", self.pick_series, "Start over")
            return
        back = self.back_to_series()
        await self.show(interaction, f"Who comes off the regulars of **{self.label()}**?", *self.paged("Pick the regular", opts, self.page, self.member_picked, self.to_page(self.pick_member)),
                        *([self.button("Back", back)] if back else []), self.button("Cancel", self.cancel))

    async def member_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.member_id = int(values[0])
        await self.after_member(interaction)

    async def after_member(self, interaction: discord.Interaction) -> None:
        m = self.member()
        key = self.draft["series"]
        c = next((x for x in (m.characters if m else []) if key in x.rosters), None)
        back = self.pick_member if not self.preset else self.back_to_series()
        who = "you" if self.self_service else m.display_name if m else "they"
        text = (f"Step down from the regulars of **{self.label()}**" + (f" ({c.label})" if c else "") + "?\n-# You can still Join any sheet; you just won't be expected (or reminded) any more. To come back, ask an officer."
                if self.self_service else
                f"Take **{who}**" + (f" ({c.label})" if c else "") + f" off the regulars of **{self.label()}**?\n-# They can still Join any sheet; they're just not expected (or reminded) any more.")
        await self.show(interaction, text, self.button("Step down" if self.self_service else "Confirm", self.save, discord.ButtonStyle.danger),
                        *([self.button("Back", back)] if back else []), self.button("Cancel", self.cancel))

    async def save(self, interaction: discord.Interaction) -> None:
        key, by = self.draft["series"], interaction.user.display_name
        await self.working(interaction, "Saving…")
        try:
            m = self.reg.regular_remove(self.member_id, key, by)  # raises when they're no longer on it
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await _emit(interaction, self.reg, f"{by}: {m.display_name} off the regulars of {self.label()}" + (" (stepped down)" if self.self_service else ""))
        await self.finish(interaction, (f"✅ You stepped down from **{self.label()}**. You can still Join its sheets." if self.self_service
                                        else f"✅ {m.display_name} is no longer a regular of **{self.label()}**. They can still Join its sheets."))


@flow("roster_add")
async def open_roster_add(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User | None = None) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await RegularAddWizard(reg, interaction.user.id, member).start(interaction)


@flow("roster_remove")
async def open_roster_remove(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User | None = None) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await RegularRemoveWizard(reg, interaction.user.id, member).start(interaction)


@flow("regular_step_down")
async def open_regular_step_down(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """A member steps down from a series' regulars (D7) — /me view's Step down button. Self-service: no officer check."""
    m = reg.members.get(interaction.user.id)
    if m is None:
        await interaction.response.send_message("Nothing registered yet — /register to start.", ephemeral=True)
        return

    class _Self(RegularRemoveWizard):
        def __init__(self):
            super().__init__(reg, interaction.user.id, interaction.user, self_service=True)
            self.guard = None  # their own record: no officer check on the presses

    await _Self().start(interaction)


class RegularListWizard(RegularsWizard):
    """/roster members: pick the schedule (only when several), then its regulars by role, one member per line."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, None, title="Regulars")

    async def start(self, interaction: discord.Interaction) -> None:
        self.preset = True  # no member step: the listing comes straight after the schedule
        await self.pick_series(interaction)

    async def after_member(self, interaction: discord.Interaction) -> None:
        key = self.draft["series"]
        ico = _ico(interaction)
        regs = self.reg.regulars(key)
        rid, sid = key.split(":", 1)
        rd = self.reg.raid_def(rid)
        if not regs:
            text = (f"**{self.label()}** has no regulars yet — until it does, every registered main counts as expected on its sheets."
                    "\n-# `/roster add` picks them (or the Rosters page).")
        else:
            by_role: dict[str, list[str]] = {}
            for m, c in regs:
                by_role.setdefault(self.reg.profile.spec(c.cls, c.spec).role, []).append(_spec_line(ico, c) + f" · {m.display_name}")
            bounds = self.reg.role_bounds(rid, int(rd.get("size") or 20))
            head = f"**{self.label()}** · {len(regs)} regular{'s' if len(regs) != 1 else ''} for {rd.get('size', '?')} seats"
            counts = "   ".join(f"{(ico('role', r) if ico else r)} {len(by_role.get(r, []))}" + (f"/{bounds[r]['min']}" if r in ("tank", "healer") and bounds[r]["min"] else "") for r in ("tank", "healer", "melee", "ranged"))
            lines = [head, counts] + [ln for r in ("tank", "healer", "melee", "ranged") for ln in by_role.get(r, [])]
            text = "\n".join(lines)
        back = self.back_to_series()
        if back:
            await self.show(interaction, text[:1900], self.button("Another schedule", back), self.button("Done", self.cancel))
        else:
            await self.finish(interaction, text[:1900])


@flow("roster_members")
async def open_roster_members(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """Read-only, open to members (help's MEMBER_PATHS lists it): the regulars of a schedule, by role."""
    wiz = RegularListWizard(reg, interaction.user.id)
    if not _is_officer(interaction, reg):
        wiz.guard = None
    await wiz.start(interaction)


__all__ = ["ConfirmWizard", "RankWizard", "SetMainWizard", "RegularAddWizard", "RegularRemoveWizard", "RegularListWizard", "confirm_pending_view", "roster_name",
           "open_roster_confirm", "open_roster_rank", "open_roster_set_main", "open_roster_add", "open_roster_remove", "open_roster_members", "open_regular_step_down"]
