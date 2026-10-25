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

from .constants import ROLES
from .registry import RANKS, Member, RegisteredCharacter, Registry, RegistryError
from .wizard import MAX_OPTIONS, Opt, Wizard, flow

log = logging.getLogger(__name__)

OFFICERS_ONLY = "Officers only (Manage Server or a configured officer role)."


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
    """One member's characters — an officer on someone (`/roster char`, /roster rank, /roster set-main) or, in SELF mode,
    the member on their own record (`/me char`: member = the presser, no officer guard). Characters are picked by their
    index in the member's list and re-found by label at save, so a character retired, renamed or reordered meanwhile is
    refused, never guessed. Every write is a one-row table through THE character verb (discord_people.save_characters:
    validated on a copy, the ripple into sheets and seats, one audit line) — never a registry primitive."""

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User, *, title: str, self_mode: bool = False):
        super().__init__(reg, owner_id, title=title if self_mode else f"{title} · {member.display_name}")
        self.member_id, self.member_name, self.self_mode = member.id, member.display_name, self_mode
        if self_mode:
            self.guard = None  # their own record: no officer check on the presses

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
            raise RegistryError(f"{label} is no longer registered to {'you' if self.self_mode else m.display_name}.")
        if c.status not in ("active", "planned"):
            raise RegistryError(f"{label} was retired meanwhile.")
        return m, c

    def ripple(self, interaction: discord.Interaction, rows: list[dict]) -> list[str]:
        from .discord_people import character_preview

        return character_preview(interaction.client, self.reg, self.member_id, rows)

    async def save_rows(self, interaction: discord.Interaction, rows: list[dict], done: str, retry=None) -> None:
        """Write `rows` through THE character verb, then the outcome (with the ripple lines) — or the refusal, draft kept."""
        from . import discord_people

        try:
            res = await discord_people.save_characters(interaction.client, self.reg, self.member_id, rows, interaction.user.display_name,
                                                       by_id=interaction.user.id, officer=not self.self_mode, rev=self.draft.get("rev"), via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), retry or self.start, "Start over")
            return
        await self.finish(interaction, f"✅ {done}" + "".join(f"\n• {line}" for line in res["ripple"]))


# ---------------------------------------------------------------- /me char · /roster char: the one character hub

CHAR_ACTIONS = {"spec": "Spec", "main": "Main", "flex": "Flex", "retire": "Retire", "delete": "Delete", "name": "Name", "rank": "Rank"}
SELF_ACTIONS = ("spec", "main", "flex", "retire", "delete", "name")
OFFICER_ACTIONS = ("spec", "main", "flex", "rank", "retire", "delete")
DELETE_WORDS = "Deleting removes it outright — only the ops lines keep a trace. Prefer Retire if you may come back to it."


class CharWizard(MemberWizard):
    """The hub: `/me char [action]` (self mode) and `/roster char member: [action]` (an officer). The Choice pre-seeds
    the action and skips the hub's action row. Spec · Main · Flex · Retire · Delete on a character, Rank on the member
    (D25: rank lives on the main; officers only), Name hands over to the naming wizard (a planned character, self only).
    Every action ends on THE decide screen with the ripple from code (rc.character_preview), then one table save."""

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User, *, self_mode: bool):
        super().__init__(reg, owner_id, member, title="Your characters" if self_mode else "Characters", self_mode=self_mode)
        self.actions = SELF_ACTIONS if self_mode else OFFICER_ACTIONS

    async def start(self, interaction: discord.Interaction, action: str | None = None) -> None:
        m = self.member()
        if not m or not m.active():
            await self.finish(interaction, "No characters yet — `/register` (or the registration card) adds your main." if self.self_mode
                              else f"{self.member_name} has no registered characters.")
            return
        self.draft = {"rev": self.reg.characters_rev(m)}
        if action in self.actions:
            await self.act(interaction, action)
            return
        await self.hub(interaction)

    async def hub(self, interaction: discord.Interaction) -> None:
        from .discord_registry import char_line

        m = self.member()
        ico = _ico(interaction) or (lambda kind, key: "")
        lines = [char_line(ico, c) for c in sorted(m.active(), key=lambda c: (not c.is_main, c.label.lower()))]
        rank = f"\n-# Rank: **{m.rank}** (it lives on the main)" if m.main else ""
        buttons = [self.button(CHAR_ACTIONS[a], self._goto(a), discord.ButtonStyle.danger if a == "delete" else discord.ButtonStyle.secondary)
                   for a in self.actions if a != "name" or m.planned()]
        await self.show(interaction, "\n".join(lines)[:1500] + rank + "\nWhat do you want to change?", *buttons, self.button("Cancel", self.cancel))

    def _goto(self, action: str):
        async def go(interaction: discord.Interaction) -> None:
            await self.act(interaction, action)
        return go

    def eligible(self, action: str) -> list[tuple[int, RegisteredCharacter]]:
        if action == "main":
            return self.indexed(lambda c: not c.is_main)
        if action == "name":
            return self.indexed(lambda c: c.status == "planned")
        return self.indexed()

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        self.draft["action"] = action
        if action == "name":  # naming a planned character is the registration's own wizard (the name is prose, validated)
            from .wizard import FLOWS

            await FLOWS["char_name"](interaction, self.reg)
            return
        if action == "rank":
            await self.pick_rank(interaction)
            return
        chars = self.eligible(action)
        if not chars:
            text = {"main": "There is no other character to make the main — `/me char` can add an alt with Add (or `/register`)." if self.self_mode
                    else f"{self.member_name} has no other character to make main."}.get(action, "No character to change.")
            await self.finish(interaction, text)
            return
        if len(chars) == 1:
            await self.char_picked(interaction, [str(chars[0][0])])
            return
        await self.pick_char(interaction)

    async def pick_char(self, interaction: discord.Interaction) -> None:
        action = self.draft["action"]
        prompt = {"main": "Which character becomes the main? Rank moves with it.", "spec": "Whose spec changes?", "flex": "Which character's extra roles?",
                  "retire": "Which character retires?", "delete": "Which character is deleted?"}[action]
        await self.show(interaction, prompt, self.select("Pick the character", self.char_opts(self.eligible(action), _ico(interaction), self.draft.get("idx")), self.char_picked),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def char_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        idx = int(values[0])
        c = self.member().characters[idx]
        self.draft.update(idx=idx, label=c.label, cls=c.cls, spec=c.spec, offspec=c.offspec, flex=list(c.flex))
        await {"spec": self.spec_screen, "flex": self.flex_screen}.get(self.draft["action"], self.review)(interaction)

    # ---- spec
    async def spec_screen(self, interaction: discord.Interaction) -> None:
        from . import wizard_opts as wopts

        d, ico = self.draft, _ico(interaction)
        offs = [o for o in wopts.specs_of(self.reg, d["cls"], ico, current=d["offspec"], none_label="none") if o.value != d["spec"]]
        await self.show(interaction, f"**{d['label']}** · {d['cls']} — new: **{d['spec']}{'/' + d['offspec'] if d['offspec'] else ''}**",
                        self.select("Main spec", wopts.specs_of(self.reg, d["cls"], ico, current=d["spec"]), self.on_spec),
                        self.select("Offspec (optional)", offs, self.on_offspec),
                        self.button("Review", self.review, discord.ButtonStyle.primary), self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def on_spec(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["spec"] = values[0]
        if self.draft.get("offspec") == values[0]:
            self.draft["offspec"] = None
        await self.spec_screen(interaction)

    async def on_offspec(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["offspec"] = None if values[0] == "-" else values[0]
        await self.spec_screen(interaction)

    # ---- flex: roles the spec / offspec don't already give
    async def flex_screen(self, interaction: discord.Interaction) -> None:
        d = self.draft
        own = {self.reg.profile.spec(d["cls"], d["spec"]).role} | ({self.reg.profile.spec(d["cls"], d["offspec"]).role} if d["offspec"] else set())
        ico = _ico(interaction)
        extra = [r for r in ROLES if r not in own]
        opts = [Opt("none", "none", "no extra roles", default=not d["flex"])] + [Opt(r, r, emoji=ico("role", r) if ico else None, default=r in d["flex"]) for r in extra]
        await self.show(interaction, f"**{d['label']}** plays {', '.join(sorted(own))} from its spec. Which other roles could it fill if asked?",
                        self.select("Extra roles", opts, self.on_flex, min_values=1, max_values=len(opts)),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def on_flex(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["flex"] = [] if "none" in values else [v for v in values if v in ROLES]
        await self.review(interaction)

    # ---- rank (D25: on the member, i.e. the main; officers only)
    async def pick_rank(self, interaction: discord.Interaction) -> None:
        m = self.member()
        if not m.main:
            await self.finish(interaction, f"{self.member_name} has no main yet — rank lives on the main.")
            return
        self.draft.update(idx=m.characters.index(m.main), label=m.main.label, old=m.main.rank)
        await self.show(interaction, f"**{self.member_name}** is **{m.main.rank}** (main: {m.main.label}). New rank?\n"
                        "-# Rank belongs to the player and lives on the main — alts always read alt, and a new main takes the rank with it.",
                        self.select("Pick the rank", [Opt(r, r, "current" if r == m.main.rank else None, default=r == m.main.rank) for r in RANKS], self.rank_picked),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def rank_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["rank"] = values[0]
        if values[0] == self.draft["old"]:
            await self.show(interaction, f"**{self.member_name}** is already **{values[0]}** — pick a different rank, or cancel.",
                            self.button("Pick again", self.pick_rank, discord.ButtonStyle.primary), self.button("Cancel", self.cancel))
            return
        await self.review(interaction)

    # ---- the confirm: what changes, the ripple from code, THE decide screen
    def rows(self) -> list[dict]:
        d, action = self.draft, self.draft["action"]
        if action == "spec":
            return [{"label": d["label"], "cls": d["cls"], "spec": d["spec"], "offspec": d["offspec"]}]
        if action == "flex":
            return [{"label": d["label"], "flex": list(d["flex"])}]
        if action == "main":
            return [{"label": d["label"], "main": True}]
        if action == "rank":
            return [{"label": d["label"], "rank": d["rank"]}]
        return [{"label": d["label"], action: True}]  # retire | delete

    def summary(self) -> tuple[str, str, bool]:
        """(what changes, the button's words, danger?) for the decide screen."""
        d, action, m = self.draft, self.draft["action"], self.member()
        whose = "your" if self.self_mode else f"{self.member_name}'s"
        if action == "spec":
            return f"**{d['label']}**: {d['cls']} **{d['spec']}{'/' + d['offspec'] if d['offspec'] else ''}**.", "Save the spec", False
        if action == "flex":
            return f"**{d['label']}** could also play: **{', '.join(d['flex']) or 'nothing extra'}**.", "Save", False
        if action == "main":
            old = m.main.label if m and m.main else None
            return f"**{d['label']}** becomes {whose} main" + (f" instead of **{old}**." if old else "."), "Make it the main", False
        if action == "rank":
            return f"**{self.member_name}**: **{d['old']}** → **{d['rank']}**.", "Set the rank", False
        if action == "retire":
            return f"Retire **{d['label']}** — it leaves sheets and rosters; its history is kept.", f"Retire {d['label']}", True
        return f"Delete **{d['label']}**. {DELETE_WORDS}", f"Delete {d['label']}", True

    async def review(self, interaction: discord.Interaction) -> None:
        try:
            m, c = self.reread()
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        if self.draft["action"] == "spec" and (self.draft["spec"], self.draft["offspec"]) == (c.spec, c.offspec):
            await self.show(interaction, f"**{c.label}** is already {c.spec}{'/' + c.offspec if c.offspec else ''} — pick a different spec.",
                            self.button("Change", self.spec_screen, discord.ButtonStyle.primary), self.button("Cancel", self.cancel))
            return
        what, label, danger = self.summary()
        effects = self.ripple(interaction, self.rows())
        text = "\n".join([what, *[f"• {e}" for e in effects]]) if effects else what + "\n-# Nothing else changes: no sheet or seat holds it."
        await self.decide(interaction, text, do=self.save, do_label=label, danger=danger)

    async def save(self, interaction: discord.Interaction, _note: str | None) -> None:
        d, action = self.draft, self.draft["action"]
        done = {"spec": f"{d['label']} is {d.get('spec')}" + (f"/{d['offspec']}" if d.get("offspec") else "") + ".",
                "flex": f"{d['label']} extra roles: {', '.join(d.get('flex') or []) or 'none'}.",
                "main": f"{d['label']} is the main now (rank moved with it).",
                "rank": f"{self.member_name} is now **{d.get('rank')}**.",
                "retire": f"Retired {d['label']}.", "delete": f"Deleted {d['label']}."}[action]
        await self.save_rows(interaction, self.rows(), done)


@flow("member_char")
async def open_member_char(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User | None = None, action: str | None = None) -> None:
    """`/me char [action]` (no member, or yourself: self mode) and `/roster char member: [action]` (an officer)."""
    if member is None or member.id == interaction.user.id and action != "rank":
        if action == "add":  # a new alt is the registration wizard (class → spec → name)
            from .wizard import FLOWS

            await FLOWS["register"](interaction, reg, slot="alt")
            return
        await CharWizard(reg, interaction.user.id, interaction.user, self_mode=True).start(interaction, action)
        return
    if await _stop_at_entry(interaction, reg):
        return
    await CharWizard(reg, interaction.user.id, member, self_mode=False).start(interaction, action)


# ---------------------------------------------------------------- /roster confirm

class ConfirmWizard(OfficerWizard):
    """The unconfirmed characters as a list; pick one, confirm it, and land back on the rest ('Confirm next'). The member
    is told on their Me page (no DM — the DM rule); your own characters are not offered (self-confirm, the owner aside)."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Confirm characters")
        self.page = 0
        self.done: list[str] = []

    def pending(self) -> list[tuple[Member, RegisteredCharacter]]:
        mine = not self.reg.is_owner_id(self.owner_id)
        return [(m, c) for m, c in self.reg.pending() if not (mine and m.discord_id == self.owner_id)]

    def pending_opts(self, ico) -> list[Opt]:
        return [Opt(f"{m.discord_id}|{c.label}"[:100], f"{c.label} · {c.cls} {c.spec} · {m.display_name}"[:100], c.rank + (" · main" if c.is_main else " · alt"),
                    emoji=ico("class", c.cls) if ico else None) for m, c in self.pending()]

    async def start(self, interaction: discord.Interaction, name: str | None = None) -> None:
        hit = next(((m, c) for m, c in self.pending() if c.label == name), None) if name else None
        if hit:
            await self.ask(interaction, f"{hit[0].discord_id}|{hit[1].label}")
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
        pend = self.pending()
        if not pend:
            await self.listing(interaction)
            return
        await self.ask(interaction, f"{pend[0][0].discord_id}|{pend[0][1].label}")

    async def ask(self, interaction: discord.Interaction, key: str) -> None:
        uid, _, label = key.partition("|")
        hit = next(((m, c) for m, c in self.pending() if str(m.discord_id) == uid and c.label == label), None)
        if not hit:
            await self.listing(interaction, f"❌ {label} is no longer waiting (confirmed or retired meanwhile).")
            return
        m, c = hit
        self.draft = {"label": label, "member_id": m.discord_id}
        others = [x.label for x in m.active() if x is not c]
        lines = [f"Confirm **{c.label}** · {c.cls} {c.spec}{'/' + c.offspec if c.offspec else ''} · {'main' if c.is_main else 'alt'}, registered by **{m.display_name}**?",
                 f"-# Their other characters: {', '.join(others)}" if others else "-# Their only character.",
                 "-# They see it on their Me page (no DM)."]
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
        m = self.reg.members.get(member_id)
        c = next((x for x in (m.characters if m else []) if x.label == label), None)  # re-read: another officer may have got here first
        if c is None:
            await self.refuse(interaction, f"{label} is no longer registered (retired or removed meanwhile).", self.back, "Back to the list")
            return
        if c.status != "active":
            await self.refuse(interaction, f"{label} was retired meanwhile.", self.back, "Back to the list")
            return
        if c.confirmed_by:
            await self.refuse(interaction, f"{label} was already confirmed by {c.confirmed_by}.", self.back, "Back to the list")
            return
        await self.working(interaction, f"Confirming {label}…")
        from . import discord_people

        try:  # THE character verb (a one-row table: the registry's lock, validated on a copy, its own audit line)
            await discord_people.save_characters(interaction.client, self.reg, member_id, [{"label": label, "confirm": True}], interaction.user.display_name,
                                                 by_id=interaction.user.id, officer=True, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.back, "Back to the list")
            return
        self.done.append(label)
        # the member is told by a notice on their Me page (Registry.confirm writes it) — never a "welcome" DM (the DM rule)
        self.page = 0
        await self.listing(interaction, f"✅ Confirmed **{label}** ({m.display_name}).")


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


# ---------------------------------------------------------------- /roster rank · /roster set-main: entry points of the hub

class RankWizard(CharWizard):
    """`/roster rank member:` — the hub's Rank (D25: the member's rank, on the main)."""

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User):
        super().__init__(reg, owner_id, member, self_mode=False)

    async def start(self, interaction: discord.Interaction, action: str | None = None) -> None:
        await super().start(interaction, "rank")


class SetMainWizard(CharWizard):
    """`/roster set-main member:` — the hub's Main (the rank moves with it)."""

    def __init__(self, reg: Registry, owner_id: int, member: discord.abc.User):
        super().__init__(reg, owner_id, member, self_mode=False)

    async def start(self, interaction: discord.Interaction, action: str | None = None) -> None:
        await super().start(interaction, "main")


@flow("roster_rank")
async def open_roster_rank(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User) -> None:
    if await _stop_at_entry(interaction, reg):
        return
    await RankWizard(reg, interaction.user.id, member).start(interaction)


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
                 "-# Regulars are expected on every sheet of it: officers see who hasn't answered, the reminder goes to them, and they're weighted ahead of others at lock. Nothing is answered for them."]
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


__all__ = ["MemberWizard", "CharWizard", "ConfirmWizard", "RankWizard", "SetMainWizard", "RegularAddWizard", "RegularRemoveWizard", "RegularListWizard",
           "confirm_pending_view", "roster_name", "open_member_char", "open_roster_confirm", "open_roster_rank", "open_roster_set_main", "open_roster_add",
           "open_roster_remove", "open_roster_members", "open_regular_step_down", "absences_text"]


# ---------------------------------------------------------------- /roster absences

ABSENCE_HORIZONS = ((7, "the next week"), (14, "the next 2 weeks"), (30, "the next 30 days"), (60, "the next 2 months"), (90, "the next 3 months"))
DEFAULT_ABSENCE_HORIZON = 30


def absences_text(reg: Registry, days: int) -> str:
    """Upcoming absences across the guild for `days` ahead, days in words (reg.span_label — never ISO where a person
    reads), the reason and who recorded it (officers only see this list)."""
    from datetime import timedelta

    today = reg.now_local().date()
    rows = [(m, a) for m, a in reg.absences_between(today.isoformat(), (today + timedelta(days=days)).isoformat()) if not m.test]  # puppets stay in the rehearsal
    lines = [f"• **{m.display_name}** ({(m.main.name if m.main else '-')}) {reg.span_label(a.start, a.end)}" + (f" — {a.reason}" if a.reason else "")
             + (f" _(by {a.by})_" if a.by != m.display_name else "") for m, a in rows]
    label = next((name for d, name in ABSENCE_HORIZONS if d == days), f"the next {days} days")
    out = ""
    for i, line in enumerate(lines):
        more = f"\nand {len(lines) - i} more — the Members page lists them all"
        if len(out) + len(line) + len(more) + 1 > 1900:
            return out + more
        out += ("\n" if out else "") + line
    return out or f"No absences in {label}."


@flow("roster_absences")
async def open_roster_absences(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """/roster absences: how far ahead is a preset select (the wizard rule: a number is never typed), the list under it.
    Officer-only data (reasons, who recorded them): an OfficerWizard, so standing is re-checked on every horizon press."""
    if await _stop_at_entry(interaction, reg):
        return
    wiz = OfficerWizard(reg, interaction.user.id, title="Upcoming absences")

    async def show(i: discord.Interaction, days: int) -> None:
        opts = [Opt(str(d), name.capitalize(), default=d == days) for d, name in ABSENCE_HORIZONS]

        async def picked(pi: discord.Interaction, values: list[str]) -> None:
            await show(pi, int(values[0]))

        await wiz.show(i, absences_text(reg, days), wiz.select("How far ahead?", opts, picked))

    await show(interaction, DEFAULT_ABSENCE_HORIZON)


# ---------------------------------------------------------------- /raid set: anything a member can do, recorded for them (B5)

SET_WHAT = {"join": "Join", "bench": "Bench", "no_thanks": "No thanks", "callout": "Can't make it (a callout)", "confirm_yes": "Confirm for them",
            "confirm_no": "Can't make it for them (their Confirm ask)", "fill_yes": "Confirm their fill", "fill_no": "No to their fill",
            "pin_in": "Pin to roster", "pin_out": "Keep on bench", "unpin": "Unpin"}
STATUS_WHAT = {"in": "join", "sub": "bench", "out": "no_thanks"}  # the old `status` seed (Join / Bench / No thanks)


class SetWizard(OfficerWizard):
    """`/raid set member:<User> [what]` (design B5): the run first — only runs where something applies to that member
    (one → straight on) — then ONLY the verbs that apply to them on that run (rc.member_actions: their open Confirm ask,
    their fill ask, Record a callout, Join (as <alt>) · Bench · No thanks, the pins), in their own words; a character pick
    when the verb needs one; THE confirm screen (Wizard.decide) with the consequence from code (inbox.action_consequence:
    who is told, and how — a notice, their open card edited, never a DM); a callout's note on its "with a note…" button.
    At the write the member is re-read: an answer changed since the screen was drawn is shown first ("Kestrel is now
    Bench … — keep going?"). Every write is inbox.do_member_action → the verbs every surface shares."""

    def __init__(self, reg: Registry, rs, owner_id: int, member_id: int, what: str | None = None):
        super().__init__(reg, owner_id, title="Set for someone")
        self.rs = rs
        self.draft = {"member": member_id, "what": what, "key": None, "action": None, "character": None, "rev": None, "note": None}

    def member(self) -> Member | None:
        return self.reg.members.get(self.draft["member"])

    def run(self):
        return self.rs.events.get(self.draft["key"]) if self.draft["key"] else None

    def runs(self, m: Member, *, test: bool = False) -> list:
        """The live runs where something applies to `m` — never a test run (design B5: `/raid set` refuses them; the
        tester answers for puppets with `/gm test answer`). `test=True` lists the test runs instead (for the refusal)."""
        from . import raidcycle as rc

        out = []
        for ev in self.rs.live():
            if ev.state not in ("open", "locked") or rc.is_test_run(self.reg, ev) != test or not rc.on_bench_side(self.reg, ev, m):
                continue
            keys = {a["key"] for a in rc.member_actions(self.reg, ev, m)}
            if keys and (not self.draft["what"] or self.draft["what"] in keys):
                out.append(ev)
        return out

    async def start(self, interaction: discord.Interaction) -> None:
        from .raid_views import raid_name

        m = self.member()
        if m is None or not m.active():
            await self.finish(interaction, "They aren't registered (no active character) — nothing to set.")
            return
        self.title = f"Set for {m.display_name}"
        runs = self.runs(m)
        if not runs and self.runs(m, test=True):
            await self.finish(interaction, f"{m.display_name} is only on a test run, and `/raid set` leaves test runs alone — answer for "
                                           "puppets with `/gm test answer` (or the Test bench on the Members page).")
            return
        if not runs:
            what = SET_WHAT.get(self.draft["what"] or "", "")
            await self.finish(interaction, f"Nothing to set for {m.display_name}" + (f" ({what})" if what else "") + " on a live run right now.")
            return
        if len(runs) == 1:
            self.draft["key"] = runs[0].key
            await self.verbs(interaction)
            return
        opts = [Opt(ev.key, f"{raid_name(self.reg, ev)} · {self.reg.local12(ev.start)}", ev.state) for ev in runs][:MAX_OPTIONS]
        await self.show(interaction, f"Which run for **{m.display_name}**?", self.select("Pick the run", opts, self.run_picked), self.button("Cancel", self.cancel))

    async def run_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["key"] = values[0]
        await self.verbs(interaction)

    async def verbs(self, interaction: discord.Interaction) -> None:
        from . import raidcycle as rc

        m, ev = self.member(), self.run()
        if ev is None or m is None or ev.state not in ("open", "locked"):
            await self.finish(interaction, "That run closed — nothing was changed.")
            return
        acts = rc.member_actions(self.reg, ev, m)
        self.draft["rev"] = rc.member_state_rev(ev, m)
        want = self.draft["what"]
        hit = next((a for a in acts if a["key"] == want), None) if want else None
        if hit:
            await self.chosen(interaction, hit)
            return
        s = ev.signups.get(str(m.discord_id))
        now = f"{rc.LABELS.get(s.status, s.status)} as {s.character}" if s else "no answer yet"
        buttons = []
        for a in acts:
            async def press(i: discord.Interaction, a=a) -> None:
                await self.chosen(i, a)
            style = discord.ButtonStyle.danger if a["key"] in ("callout", "confirm_no", "fill_no", "no_thanks") else discord.ButtonStyle.secondary
            buttons.append(self.button(a["label"], press, style))
        await self.show(interaction, f"**{m.display_name}** on {rc.run_words(self.reg, ev)} — now: {now}.\nWhat do you record for them?",
                        *buttons[:20], self.button("Cancel", self.cancel))

    async def chosen(self, interaction: discord.Interaction, action: dict) -> None:
        self.draft["action"] = action["key"]
        chars = action.get("characters") or []
        if action.get("needs") == "character" and len(chars) > 1:
            m = self.member()
            opts = [o for o in _char_opts(self.reg, m, _ico(interaction)) if o.value in chars]
            await self.show(interaction, f"**{action['label']}** as which of {m.display_name}'s characters?",
                            self.select("Pick the character", opts, self.character_picked), self.button("Cancel", self.cancel))
            return
        self.draft["character"] = chars[0] if action.get("needs") == "character" and chars else None
        await self.confirm(interaction)

    async def character_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["character"] = values[0]
        await self.confirm(interaction)

    async def confirm(self, interaction: discord.Interaction) -> None:
        from . import inbox

        m, ev, key = self.member(), self.run(), self.draft["action"]
        if ev is None or m is None:
            await self.finish(interaction, "That run closed, or the member isn't registered any more — nothing was changed.")
            return
        text = inbox.action_consequence(self.reg, ev, m, key, self.draft["character"], by_id=interaction.user.id)
        note = {"label": "Note for the officers (optional)", "max_length": 300, "description": "Never shown on the public sheet"} if key == "callout" else None
        label = {"callout": "Record the callout"}.get(key) or SET_WHAT.get(key, "Do it")
        await self.decide(interaction, text, do=self.save, note=note, do_label=label[:40], danger=key in ("callout", "confirm_no", "fill_no", "no_thanks"))

    async def save(self, interaction: discord.Interaction, note: str | None, *, force: bool = False) -> None:
        from . import inbox
        from . import raidcycle as rc

        m, ev = self.member(), self.run()  # re-read: never trust the screen
        if ev is None or m is None or ev.state not in ("open", "locked"):
            await self.finish(interaction, "That run closed — nothing was changed.")
            return
        if note is not None:
            self.draft["note"] = note
        if not force and rc.member_state_rev(ev, m) != self.draft["rev"]:
            async def keep(i: discord.Interaction) -> None:
                await self.working(i)
                await self.save(i, self.draft["note"], force=True)
            await self.show(interaction, f"⚠️ {rc.member_change_words(self.reg, ev, m)} — keep going?",
                            self.button("Keep going", keep, discord.ButtonStyle.primary), self.button("Back", self.cancel))
            return
        try:
            line = await inbox.do_member_action(interaction.client, self.reg, self.rs, ev, m, self.draft["action"], character=self.draft["character"],
                                                note=self.draft["note"], by=interaction.user.display_name, by_id=interaction.user.id)
        except (RegistryError, ValueError) as e:
            await self.finish(interaction, f"❌ {e}")
            return
        await self.finish(interaction, f"✅ {line}")


def _char_opts(reg: Registry, m: Member, ico) -> list[Opt]:
    from . import wizard_opts as wo

    return [Opt(o.label, o.label, o.description, o.emoji) for o in wo.characters_of(reg, m, ico)]


@flow("raid_set")
async def open_raid_set(interaction: discord.Interaction, reg: Registry, *, member: discord.abc.User, what: str | None = None, status: str | None = None, **_seed) -> None:
    """/raid set member:<User> [what] — `what` (a fixed Choice) skips the verb screen when it applies; `status` is the old
    seed (Join / Bench / No thanks)."""
    if await _stop_at_entry(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    await SetWizard(reg, rs, interaction.user.id, member.id, what or STATUS_WHAT.get(status or "")).start(interaction)
