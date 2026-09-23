"""Officer roster wizards: `/roster confirm` (walk the unconfirmed characters), `/roster rank`, `/roster set-main`,
`/roster add` and `/roster remove`. The member comes from the slash command's native picker; everything else — which
character, which rank, which roster — is picked here from what the registry holds, so nothing is typed and nothing
can name a character or roster that doesn't exist.

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


# ---------------------------------------------------------------- /roster add

class RosterAddWizard(MemberWizard):
    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User):
        super().__init__(reg, owner_id, member, title="Add to a roster")
        self.page = 0

    def roster_opts(self) -> list[Opt]:
        m = self.member()
        on = {k for c in (m.active() if m else []) for k in c.rosters}
        return [Opt(k, roster_name(self.reg, k)[:100], "already on it" if k in on else None, default=k == self.draft.get("roster"))
                for k in self.reg.config.roster_keys()]

    async def start(self, interaction: discord.Interaction) -> None:
        if not self.indexed():
            await self.finish(interaction, f"{self.member_name} has no registered characters — they register first (the registration card or /register).")
            return
        keys = self.reg.config.roster_keys()
        self.draft["many_rosters"] = len(keys) > 1
        if len(keys) == 1:
            self.draft["roster"] = keys[0]
            await self.pick_char(interaction)
            return
        await self.pick_roster(interaction)

    async def pick_roster(self, interaction: discord.Interaction) -> None:
        await self.show(interaction, "Which roster?",
                        *self.paged("Pick the roster", self.roster_opts(), self.page, self.roster_picked, self.to_page),
                        self.button("Cancel", self.cancel))

    def to_page(self, page: int):
        async def go(interaction: discord.Interaction) -> None:
            self.page = page
            await self.pick_roster(interaction)
        return go

    async def roster_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["roster"] = values[0]
        await self.pick_char(interaction)

    async def pick_char(self, interaction: discord.Interaction) -> None:
        chars = self.indexed()
        if len(chars) == 1:
            await self.char_picked(interaction, [str(chars[0][0])])
            return
        self.draft["many_chars"] = True
        m = self.member()
        main_idx = next((i for i, c in chars if c is m.main), None)
        items = [self.select("Pick the character", self.char_opts(chars, _ico(interaction), self.draft.get("idx", main_idx)), self.char_picked),
                 self.button(f"Their main ({m.main.label})" if m.main else "Their main", self.main_picked, discord.ButtonStyle.primary, disabled=main_idx is None)]
        if self.draft.get("many_rosters"):
            items.append(self.button("Back", self.pick_roster))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, f"Which of {self.member_name}'s characters raids on **{roster_name(self.reg, self.draft['roster'])}**? Usually the main.", *items)

    async def main_picked(self, interaction: discord.Interaction) -> None:
        m = self.member()
        idx = next(i for i, c in enumerate(m.characters) if c is m.main)
        await self.char_picked(interaction, [str(idx)])

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        idx = int(values[0])
        m = self.member()
        c = m.characters[idx]
        key = self.draft["roster"]
        self.draft.update(idx=idx, label=c.label)
        name = roster_name(self.reg, key)
        back = self.pick_char if self.draft.get("many_chars") else (self.pick_roster if self.draft.get("many_rosters") else None)
        if key in c.rosters:
            items = [self.button("Back", back)] if back else []
            await self.show(interaction, f"**{c.label}** is already on **{name}** — nothing to add.", *items, self.button("Cancel", self.cancel))
            return
        lines = [f"Add **{c.label}** · {c.cls} {c.spec} ({self.member_name}) to **{name}**?"]
        other = next((x for x in m.active() if x is not c and key in x.rosters), None)
        if other:
            lines.append(f"-# One character per member per roster: this replaces **{other.label}** there.")
        items = [self.button("Confirm", self.save, discord.ButtonStyle.success)]
        if back:
            items.append(self.button("Back", back))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, "\n".join(lines), *items)

    async def save(self, interaction: discord.Interaction) -> None:
        key, by = self.draft["roster"], interaction.user.display_name
        try:
            if key not in self.reg.config.roster_keys():
                raise RegistryError(f"The roster {roster_name(self.reg, key)} is gone (the run ended or was cancelled).")
            m, c = self.reread()
            if key in c.rosters:
                raise RegistryError(f"{c.label} is already on {roster_name(self.reg, key)}.")
            self.check_unique_on_member(m, c)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.working(interaction, "Saving…")
        try:
            m, c = self.reg.roster_add(self.member_id, key, by, c.label, display_name=self.member_name)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        name = roster_name(self.reg, key)
        await _emit(interaction, self.reg, f"{by} added {m.display_name} ({c.label}) to roster {name}")
        await self.finish(interaction, f"✅ **{c.label}** ({m.display_name}) → **{name}** ({len(self.reg.roster_members(key))} characters).")


@flow("roster_add")
async def open_roster_add(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await RosterAddWizard(reg, interaction.user.id, member).start(interaction)


# ---------------------------------------------------------------- /roster remove

class RosterRemoveWizard(MemberWizard):
    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User):
        super().__init__(reg, owner_id, member, title="Remove from a roster")
        self.page = 0

    def on(self) -> list[tuple[str, str]]:
        """(roster key, the character on it) for every roster the member is on."""
        m = self.member()
        out: dict[str, str] = {}
        for c in (m.characters if m else []):
            for k in c.rosters:
                out.setdefault(k, c.label)
        return sorted(out.items(), key=lambda kv: roster_name(self.reg, kv[0]).lower())

    async def start(self, interaction: discord.Interaction) -> None:
        on = self.on()
        if not on:
            await self.finish(interaction, f"{self.member_name} isn't on any roster — nothing to remove.")
            return
        self.draft["many"] = len(on) > 1
        if len(on) == 1:
            await self.roster_picked(interaction, [on[0][0]])
            return
        await self.pick_roster(interaction)

    async def pick_roster(self, interaction: discord.Interaction) -> None:
        opts = [Opt(k, roster_name(self.reg, k)[:100], f"as {label}") for k, label in self.on()]
        await self.show(interaction, f"Which roster should {self.member_name} come off?",
                        *self.paged("Pick the roster", opts, self.page, self.roster_picked, self.to_page),
                        self.button("Cancel", self.cancel))

    def to_page(self, page: int):
        async def go(interaction: discord.Interaction) -> None:
            self.page = page
            await self.pick_roster(interaction)
        return go

    async def roster_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        key = values[0]
        self.draft["roster"] = key
        label = dict(self.on()).get(key, "?")
        items = [self.button("Confirm", self.save, discord.ButtonStyle.danger)]
        if self.draft.get("many"):
            items.append(self.button("Back", self.pick_roster))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, f"Remove **{self.member_name}** ({label}) from **{roster_name(self.reg, key)}**?\n-# They can still sign up as a sub.", *items)

    async def save(self, interaction: discord.Interaction) -> None:
        key, by = self.draft["roster"], interaction.user.display_name
        name = roster_name(self.reg, key)
        await self.working(interaction, "Saving…")
        try:
            m = self.reg.roster_remove(self.member_id, key, by)  # raises on an unknown roster or a member not on it
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await _emit(interaction, self.reg, f"{by} removed {m.display_name} from roster {name}")
        await self.finish(interaction, f"✅ {m.display_name} removed from **{name}**. They can still sign up as a sub.")


@flow("roster_remove")
async def open_roster_remove(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await RosterRemoveWizard(reg, interaction.user.id, member).start(interaction)


__all__ = ["ConfirmWizard", "RankWizard", "SetMainWizard", "RosterAddWizard", "RosterRemoveWizard", "confirm_pending_view", "roster_name",
           "open_roster_confirm", "open_roster_rank", "open_roster_set_main", "open_roster_add", "open_roster_remove"]
