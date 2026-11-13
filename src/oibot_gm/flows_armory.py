"""The armory link on Discord (armory link §5.4 "Discord."), thin callers of discord_armory / armory_seed:

- `CharArmory`: the character hub's armory actions (flows_roster.CharWizard mixes it in, `/me char` and `/roster char`):
  Link to the armory… (a box: first name and surname, prose the verb validates → the lookup → THE decide screen with
  consequences.armory_link_consequence), Unlink…, Keep professions by hand / Follow the armory again (the switch), Sync now
  (no confirm: a refresh); officers also Move to another member… (Discord's own user select → move_consequence) on every
  version (R11). Everything but Move is hidden where `armory.available` is false. A lookup, a link and Sync now may wait
  for the armory and for the guild's lock, so each press shows the working state first.
- `/roster armory` (officers): the counts (linked · sure · likely · no match · in the guild and not claimed) and Link the
  sure matches → a decide screen listing them → armory_seed.apply → how many were linked and each refusal. The likely
  rows, the not-claimed list and moves are the site's (the Members page). The count's `rev` is compared at every press: a
  changed count is shown again, nothing linked.

Nothing here DMs anyone; nothing is written before the decide screen's press."""
from __future__ import annotations

from datetime import datetime

import discord

from . import armory, armory_seed, discord_armory
from . import consequences as cq
from .armory import ArmoryError
from .registry import RegisteredCharacter, Registry, RegistryError
from .wizard import Field, Form, Wizard, flow

ARMORY_ACTIONS = {"link": "Link to the armory…", "unlink": "Unlink…", "by_hand": "Keep professions by hand",
                  "follow": "Follow the armory again", "sync": "Sync now"}
MOVE_LABEL = "Move to another member…"
LIST_BUDGET = 1800  # characters a screen's list may take with its sentences (Discord's 2,000 less the title and buttons' room)
CHANGED = "The roster or the records changed since you looked, so nothing was linked. Here are the new counts."


def _why(e: Exception) -> str:
    return e.words if isinstance(e, ArmoryError) else str(e)


def updated_stamp(reg: Registry, c: RegisteredCharacter) -> str:
    """'Updated <t:unix:R>' (Discord renders it in the reader's own words), or 'Not updated from the armory yet'."""
    at = discord_armory.last_checked(reg, c)
    if not at:
        return "Not updated from the armory yet"
    return f"Updated <t:{int(datetime.fromisoformat(at).timestamp())}:R>"


def armory_line(reg: Registry, c: RegisteredCharacter, *, officer: bool = False) -> str:
    """A character's armory state under its line in the hub: 'Linked to the armory · Updated <t:…:R> · in the guild'
    (in / not in the guild only when it is known), professions kept by hand, and the officers' flags."""
    if c.armory is None:
        return "Not linked to the armory yet"
    bits = ["Linked to the armory", updated_stamp(reg, c)]
    where = discord_armory.in_guild(reg, c)
    if where is not None:
        bits.append("in the guild" if where else "not in the guild")
    if not c.profession_sync:
        bits.append("professions kept by hand")
    if officer:
        flags = discord_armory.flags(reg, c)
        if "class" in flags:
            bits.append(f"the armory shows a {c.armory.cls}")
        if "missing" in flags:
            bits.append("the armory no longer finds it")
    return " · ".join(bits)


def synced(c: RegisteredCharacter) -> bool:
    """The armory keeps this character's professions: they are shown, never edited by hand."""
    return c.armory is not None and bool(c.profession_sync)


def synced_sentence(c: RegisteredCharacter, *, self_mode: bool) -> str:
    """Why /me professions shows a synced character's list without edit buttons, and what to do."""
    where = "/me char" if self_mode else "/roster char"
    return (f"{c.label}'s professions come from the armory, so they are not changed here. "
            f"To edit them, press Keep professions by hand in {where}.")


class CharArmory:
    """The armory actions of the character hub (flows_roster.CharWizard): needs reg, draft, self_mode, member(),
    member_name, indexed(), reread(), guard and the Wizard's screens."""

    def armory_on(self) -> bool:
        m = self.member()
        return armory.available(self.reg) and m is not None and not m.test

    def armory_eligible(self, action: str) -> list:
        named = lambda c: c.status == "active" and bool(c.name)  # noqa: E731 — a plan has nothing to link or move
        keep = {"link": lambda c: named(c) and c.armory is None,
                "unlink": lambda c: named(c) and c.armory is not None,
                "sync": lambda c: named(c) and c.armory is not None,
                "by_hand": lambda c: named(c) and c.armory is not None and c.profession_sync,
                "follow": lambda c: named(c) and c.armory is not None and not c.profession_sync,
                "move": named}[action]
        return self.indexed(keep)

    def armory_buttons(self) -> list[discord.ui.Item]:
        out = []
        if self.armory_on():
            out += [self.button(label, self._goto(a)) for a, label in ARMORY_ACTIONS.items() if self.armory_eligible(a)]
        if not self.self_mode and self.armory_eligible("move"):
            out.append(self.button(MOVE_LABEL, self._goto("move")))
        return out

    def armory_lines(self, c: RegisteredCharacter) -> str:
        return f"\n-# {armory_line(self.reg, c, officer=not self.self_mode)}" if self.armory_on() and c.name and c.status == "active" else ""

    async def armory_picked(self, interaction: discord.Interaction) -> None:
        """The character is picked (draft idx / label): the action's first screen."""
        await {"link": self.link_form, "unlink": self.unlink_review, "by_hand": self.switch_review, "follow": self.switch_review,
               "sync": self.sync_now, "move": self.move_pick}[self.draft["action"]](interaction)

    async def _still_allowed(self, interaction: discord.Interaction) -> bool:
        """A modal has no interaction_check: the presser and the guard again, on the submit."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This belongs to someone else's wizard.", ephemeral=True)
            return False
        why = await self.guard(interaction) if self.guard else None
        if why:
            await self.finish(interaction, why)
            return False
        return True

    # ---- Link to the armory…: the box (prose, validated by the verb) → the lookup → decide → the link
    async def link_form(self, interaction: discord.Interaction) -> None:
        try:
            _m, c = self.reread()
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.hub, "Back")
            return
        fields = [Field("name", "First name", description="As the armory shows it.", default=c.name, max_length=40),
                  Field("surname", "Surname", description="Leave it empty when the character has none.", required=False, max_length=40)]
        await self.open_form(interaction, Form(f"Link {c.label} to the armory", fields, self.link_typed))

    async def link_typed(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        if not await self._still_allowed(interaction):
            return
        await self.working(interaction, "Looking in the armory…")
        bot = interaction.client
        if not discord_armory.lookup_allowed(self.reg, interaction.user.id):  # THE limit the site's find / link use too
            await self.refuse(interaction, discord_armory.TOO_MANY_LOOKUPS, self.link_form, "Try again")
            return
        try:
            m, c = self.reread()
            found = await discord_armory.find_for_link(bot, self.reg, values.get("name") or c.name, values.get("surname") or None)
            said = await discord_armory.link_character(bot, self.reg, self.member_id, c.label, found, interaction.user.display_name,
                                                       by_id=interaction.user.id, via="discord", preview=True)
        except (RegistryError, ArmoryError) as e:
            await self.refuse(interaction, _why(e), self.link_form, "Try again")
            return
        self.draft["found"] = found
        await self.decide(interaction, "\n".join(said), do=self.link_save, do_label="Link it")

    async def link_save(self, interaction: discord.Interaction, _note: str | None) -> None:
        found = self.draft["found"]  # exactly what the decide screen showed: never a fresh, unseen lookup
        try:
            c = await discord_armory.link_character(interaction.client, self.reg, self.member_id, self.draft["label"], found,
                                                    interaction.user.display_name, by_id=interaction.user.id, via="discord")
        except (RegistryError, ArmoryError) as e:
            await self.refuse(interaction, _why(e), self.start, "Start over")
            return
        await self.finish(interaction, f"✅ {c.label} is linked to the armory.")

    # ---- Unlink…
    async def unlink_review(self, interaction: discord.Interaction) -> None:
        try:
            said = await discord_armory.unlink_character(interaction.client, self.reg, self.member_id, self.draft["label"],
                                                         interaction.user.display_name, by_id=interaction.user.id, preview=True)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.decide(interaction, "\n".join(said), do=self.unlink_save, do_label="Unlink it", danger=True)

    async def unlink_save(self, interaction: discord.Interaction, _note: str | None) -> None:
        try:
            c = await discord_armory.unlink_character(interaction.client, self.reg, self.member_id, self.draft["label"],
                                                      interaction.user.display_name, by_id=interaction.user.id, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.finish(interaction, f"✅ {c.label} is unlinked from the armory.")

    # ---- Keep professions by hand / Follow the armory again
    async def switch_review(self, interaction: discord.Interaction) -> None:
        on = self.draft["action"] == "follow"
        try:
            said = await discord_armory.set_profession_sync(interaction.client, self.reg, self.member_id, self.draft["label"], on,
                                                            interaction.user.display_name, by_id=interaction.user.id, preview=True)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.decide(interaction, "\n".join(said), do=self.switch_save, do_label="Follow the armory" if on else "Keep them by hand")

    async def switch_save(self, interaction: discord.Interaction, _note: str | None) -> None:
        on = self.draft["action"] == "follow"
        try:  # on: the armory's professions are read at once, behind the guild's lock (the decide screen showed working)
            c = await discord_armory.set_profession_sync(interaction.client, self.reg, self.member_id, self.draft["label"], on,
                                                         interaction.user.display_name, by_id=interaction.user.id, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Start over")
            return
        await self.finish(interaction, f"✅ {c.label}'s professions follow the armory." if on else f"✅ {c.label}'s professions are kept by hand.")

    # ---- Sync now: no confirm (a refresh), the working state first
    async def sync_now(self, interaction: discord.Interaction) -> None:
        await self.working(interaction, "Asking the armory…")
        label = self.draft["label"]
        m = self.reg.members.get(self.member_id)
        held = self.reg.own_character(m.characters, label) if m else None
        aid = held.armory.id if held is not None and held.armory is not None else None
        try:
            res = await discord_armory.sync_character(interaction.client, self.reg, self.member_id, label, force=True, by_id=interaction.user.id)
        except (RegistryError, ArmoryError) as e:
            await self.refuse(interaction, _why(e), self.start, "Back")
            return
        now = self.reg.armory_character(self.member_id, aid) if aid is not None else None  # by its link: the check may have renamed it
        if now is not None:
            label = self.draft["label"] = now.label
        said = cq.armory_sync_sentence(label, res)
        await self.finish(interaction, f"✅ {said}" if res in ("updated", "unchanged") else said)

    # ---- Move to another member… (officers, every version — R11): Discord's own user select → decide → the move
    async def move_pick(self, interaction: discord.Interaction) -> None:
        await self.show(interaction, f"Who does **{self.draft['label']}** move to?", self.members("Pick the member it moves to", self.move_to),
                        self.button("Back", self.hub), self.button("Cancel", self.cancel))

    async def move_to(self, interaction: discord.Interaction, users: list) -> None:
        to = users[0]
        try:
            said = await discord_armory.move_character(interaction.client, self.reg, self.member_id, self.draft["label"], to.id,
                                                       interaction.user.display_name, by_id=interaction.user.id, preview=True)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.move_pick, "Pick someone else")
            return
        self.draft["to"] = to.id
        await self.decide(interaction, "\n".join(said), do=self.move_save, do_label="Move it")

    async def move_save(self, interaction: discord.Interaction, _note: str | None) -> None:
        try:
            c = await discord_armory.move_character(interaction.client, self.reg, self.member_id, self.draft["label"], self.draft["to"],
                                                    interaction.user.display_name, by_id=interaction.user.id, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.move_pick, "Pick someone else")
            return
        to = self.reg.members.get(self.draft["to"])
        await self.finish(interaction, f"✅ {c.label} moved to {to.display_name if to else 'them'}.")


# ---------------------------------------------------------------- /roster armory

def _officer_guard(reg: Registry):
    async def still_officer(i: discord.Interaction) -> str | None:
        from .flows_roster import OFFICERS_ONLY, _is_officer

        return None if _is_officer(i, reg) else OFFICERS_ONLY
    return still_officer


def _propose(reg: Registry) -> armory_seed.Seed:
    rows, at = discord_armory.roster_snapshot(reg)
    return armory_seed.propose(reg, rows, taken_at=at)


def _empty_words(reg: Registry) -> str:
    """The site's read views say the same (discord_armory's words)."""
    return discord_armory.NO_GUILD_YET if reg.config.armory is None else discord_armory.NOT_READ_YET


def bounded(lines: list[str], budget: int, noun: str = "") -> list[str]:
    """Whole `lines` while they fit in `budget` characters, then one line "and N more<noun>" — a list never pushes the
    sentences around it past Discord's 2,000 characters, and never stops mid-line (a <t:…:R> stamp stays whole)."""
    def tail(left: int) -> str:
        return f"and {left} more{noun[:-1] if left == 1 and noun.endswith('s') else noun}"

    out, used = [], 0
    for i, line in enumerate(lines):
        after = len(lines) - i - 1  # the lines a tail would count if this one is the last shown
        room = budget - (len(tail(after)) + 1 if after else 0)
        if used + len(line) + 1 > room:
            return [*out, tail(len(lines) - i)]
        out.append(line)
        used += len(line) + 1
    return out


batch_words = armory_seed.batch_words  # the sure batch's result head: ONE generator, the site's toast says the same


def _esc(text: str) -> str:
    return discord.utils.escape_markdown(str(text))


def _row_words(r) -> str:
    """'Tessa's Bram → Bram Kettle, level 60' — a sure match as the decide screen lists it (member text escaped)."""
    cand = r.candidate
    full = f"{cand.name} {cand.surname}" if cand.surname else cand.name
    return f"• {_esc(r.member)}'s {_esc(r.label)} → {_esc(full)}, level {cand.level}"


class ArmoryOverview(Wizard):
    """`/roster armory`: the counts, then Link the sure matches. Officers only, re-checked on every press."""

    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="The armory", guard=_officer_guard(reg))
        self.rev: str | None = None

    async def start(self, interaction: discord.Interaction, note: str = "") -> None:
        reg = self.reg
        rows, at = discord_armory.roster_snapshot(reg)
        lines = [note] if note else []
        lines.append(f"Linked to the armory: **{len(reg.armory_linked())}**")
        items: list[discord.ui.Item] = []
        if at is None:
            lines.append(_empty_words(reg))
            self.rev = None
        else:
            seed = armory_seed.propose(reg, rows, taken_at=at)
            self.rev = seed.rev
            g = reg.config.armory
            lines.insert(len(lines) - 1, f"**{_esc(g.name)}** · roster read <t:{int(datetime.fromisoformat(at).timestamp())}:R>")
            lines.append(f"Not linked yet: **{len(seed.sure)}** sure · **{len(seed.likely)}** likely · **{len(seed.none)}** with no match on the roster")
            # the site's count (discord_armory.unclaimed: roster characters no record is linked to), one number on both
            lines.append(f"In the guild and not claimed: **{len(discord_armory.unclaimed(reg))}**")
            if seed.sure:
                lines.append("-# " + armory_seed.sure_share_words(len(seed.sure)))
            if seed.sure:
                items.append(self.button("Link the sure matches", self.review, discord.ButtonStyle.primary))
        from .help import site_url

        url = site_url(reg, "/app/members", "armory")
        lines.append("-# The likely matches, the guild characters no record is linked to and moves are on the Members page of the site.")
        if url:
            items.append(discord.ui.Button(label="Open the Members page", style=discord.ButtonStyle.link, url=url))
        items.append(self.button("Done", self.done))
        await self.show(interaction, "\n".join(lines), *items)

    async def done(self, interaction: discord.Interaction) -> None:
        await self.finish(interaction, "Nothing was changed.")

    async def review(self, interaction: discord.Interaction) -> None:
        seed = _propose(self.reg)
        if seed.rev != self.rev or not seed.sure:
            await self.start(interaction, CHANGED)
            return
        picks = [(r.uid, r.label, r.candidate.id) for r in seed.sure]
        checked = armory_seed.check(self.reg, picks, by_id=interaction.user.id)  # what apply refuses before asking the armory
        why = {(r["uid"], r["label"]): r["text"] for r in checked if not r["ok"]}
        rows = [_row_words(r) + (f" · {why[(r.uid, r.label)]}" if (r.uid, r.label) in why else "") for r in seed.sure]
        n = len(picks) - len(why)
        if n == 0:  # every pick refused: nothing to press that links
            head = "Nothing can be linked, because every sure match is refused. Each reason is beside it."
            await self.show(interaction, "\n".join([head, "", *bounded(rows, LIST_BUDGET - len(head))]), self.button("Back", self.start))
            return
        said = "\n".join(cq.armory_seed_consequence(n, len(why)))  # the confirm first: no list can push it off the screen
        await self.decide(interaction, "\n".join([said, "", *bounded(rows, LIST_BUDGET - len(said))]),
                          do=self.apply, do_label="Link them" if n > 1 else "Link it")

    async def apply(self, interaction: discord.Interaction, _note: str | None) -> None:
        seed = _propose(self.reg)
        if seed.rev != self.rev:  # the count moved between the decide screen and the press
            await self.start(interaction, CHANGED)
            return
        picks = [(r.uid, r.label, r.candidate.id) for r in seed.sure]
        try:
            results = await armory_seed.apply(interaction.client, self.reg, picks, interaction.user.display_name, by_id=interaction.user.id, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Back")
            return
        head, refused = armory_seed.result_lines(self.reg, results)
        if all(r["ok"] for r in results) and results:
            head = f"✅ {head}"
        refused = [_esc(x) for x in refused]
        await self.finish(interaction, "\n".join([head, *bounded(refused, LIST_BUDGET - len(head))]))


@flow("roster_armory")
async def open_roster_armory(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """`/roster armory` (officers)."""
    from .flows_roster import _stop_at_entry

    if await _stop_at_entry(interaction, reg):
        return
    w = ArmoryOverview(reg, interaction.user.id)
    if not armory.available(reg):
        await w.finish(interaction, armory.NO_ARMORY_WORDS if not reg.profile.armory else armory.ACCESS_WORDS)
        return
    await w.start(interaction)


__all__ = ["ARMORY_ACTIONS", "ArmoryOverview", "CharArmory", "MOVE_LABEL", "armory_line", "synced", "synced_sentence", "updated_stamp"]
