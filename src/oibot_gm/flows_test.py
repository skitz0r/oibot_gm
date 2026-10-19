"""The test bench's other half in Discord (dual interfaces phase 2, B9): /gm test compose (the bench by composition:
class → spec → how many, repeat, review "3 to add · 2 to remove", Save — the site's spec grid as a wizard) and
/gm test comp (raid → Try a comp → a link to the sandbox run's board), plus the puppet digest's persistent
"Answer as puppets…" button (TestAnswerButton), which opens /gm test answer on that run.

The writes are SetupMixin's test verbs (discord_pool: test_compose, test_comp, test_answer) — the same the site's
/api/test/* routes and MCP's test_bench call; nothing here writes the registry itself. Nobody types a number: counts
are preset selects, the cap (Registry.TEST_MAX) limits the options."""
from __future__ import annotations

import logging
import re

import discord

from . import guild_routing, wizard_opts
from .registry import Registry, RegistryError
from .wizard import Opt, Wizard, flow

log = logging.getLogger(__name__)

COUNT_OPTS = (0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)


def officer_anywhere_guard(reg: Registry, what: str = "the test bench"):
    """The guard for a wizard that may run in a DM (the digest's button): officer or owner by Discord id, re-checked
    on every press (discord_registry.is_officer needs a server Member; a DM has a bare User)."""
    async def still_officer(i: discord.Interaction) -> str | None:
        return None if await i.client.is_officer_anywhere(reg, i.user.id) else f"Officers only: {what}."
    return still_officer


def _guard(reg: Registry):
    from .flows_misc import _officer_guard

    return _officer_guard(reg)


def bench_now(reg: Registry) -> dict[str, list[int]]:
    """"Class:Spec" → the puppets' ids holding it as their main, oldest first (a removal takes the newest — as the
    site's grid does)."""
    out: dict[str, list[int]] = {}
    for m in sorted(reg.test_members(), key=lambda m: m.discord_id):
        if m.main:
            out.setdefault(f"{m.main.cls}:{m.main.spec}", []).append(m.discord_id)
    return out


def compose_diff(reg: Registry, target: dict[str, int]) -> tuple[list[dict], list[int]]:
    """What Save does to reach `target` counts: rows to add ({cls, spec, count}) and puppet ids to remove."""
    now = bench_now(reg)
    add, remove = [], []
    for k in sorted(set(now) | set(target)):
        have, want = len(now.get(k, [])), int(target.get(k, len(now.get(k, []))))
        if want > have:
            cls, spec = k.split(":", 1)
            add.append({"cls": cls, "spec": spec, "count": want - have})
        elif want < have:
            remove += now[k][want:]
    return add, remove


def review_line(add: list[dict], remove: list[int]) -> str:
    return f"{sum(r['count'] for r in add)} to add · {len(remove)} to remove"


# ---------------------------------------------------------------- /gm test compose

class TestComposeWizard(Wizard):
    def __init__(self, reg: Registry, rs, owner_id: int, ico=None):
        super().__init__(reg, owner_id, title="Compose the test bench", guard=_guard(reg))
        self.rs, self.ico = rs, ico
        self.draft = {"target": {k: len(v) for k, v in bench_now(reg).items()}, "cls": None, "spec": None}

    def total(self) -> int:
        return sum(self.draft["target"].values())

    def _lines(self) -> list[str]:
        now = bench_now(self.reg)
        out = []
        for k in sorted(self.draft["target"], key=lambda k: (self._role_order(k), k)):
            want, have = self.draft["target"][k], len(now.get(k, []))
            if not want and not have:
                continue
            cls, spec = k.split(":", 1)
            out.append(f"{spec} {cls}: **{want}**" + (f" (now {have})" if want != have else ""))
        return out

    def _role_order(self, k: str) -> int:
        from .constants import ROLES

        cls, spec = k.split(":", 1)
        try:
            return ROLES.index(self.reg.profile.spec(cls, spec).role)
        except Exception:  # noqa: BLE001
            return 99

    async def start(self, interaction: discord.Interaction) -> None:
        add, remove = compose_diff(self.reg, self.draft["target"])
        changed = bool(add or remove)
        lines = self._lines() or ["No test members yet — pick a class to add some."]
        body = (f"{self.total()} of {self.reg.TEST_MAX} test members.\n" + "\n".join(lines[:30])
                + (f"\n+{len(lines) - 30} more specs" if len(lines) > 30 else "")
                + (f"\n\n**{review_line(add, remove)}** — Save writes it." if changed else "\n\n-# Pick a class to add puppets or change a count; nothing is written before Save."))
        await self.show(interaction, body,
                        self.select("Add or change a class", wizard_opts.classes(self.reg, self.ico), self.cls_picked),
                        self.button("Save", self.save, discord.ButtonStyle.success, disabled=not changed),
                        self.button("Start over", self.reset, disabled=not changed),
                        self.button("Cancel", self.cancel))

    async def reset(self, interaction: discord.Interaction) -> None:
        self.draft["target"] = {k: len(v) for k, v in bench_now(self.reg).items()}
        await self.start(interaction)

    async def cls_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["cls"] = values[0]
        cls = values[0]
        await self.show(interaction, f"Which **{cls}** spec?",
                        self.select("Which spec", wizard_opts.specs_of(self.reg, cls, self.ico), self.spec_picked),
                        self.button("Back", self.start), self.button("Cancel", self.cancel))

    async def spec_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        cls, spec = self.draft["cls"], values[0]
        if spec not in self.reg.profile.classes.get(cls or "", {}):
            await self.refuse(interaction, "That spec isn't in the game profile.", self.start, "Back")
            return
        self.draft["spec"] = spec
        k = f"{cls}:{spec}"
        cur = int(self.draft["target"].get(k, 0))
        room = self.reg.TEST_MAX - (self.total() - cur)  # the most this spec can hold under the bench's cap
        opts = [Opt(str(n), str(n), "now" if n == len(bench_now(self.reg).get(k, [])) else None, default=n == cur) for n in COUNT_OPTS if n <= room]
        await self.show(interaction, f"How many **{spec} {cls}** test members? (now {cur} in this draft · the bench holds {self.reg.TEST_MAX})",
                        self.select("How many", opts, self.count_picked),
                        self.button("Back", self.start), self.button("Cancel", self.cancel))

    async def count_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        k = f"{self.draft['cls']}:{self.draft['spec']}"
        self.draft["target"][k] = int(values[0])
        await self.start(interaction)

    async def save(self, interaction: discord.Interaction) -> None:
        add, remove = compose_diff(self.reg, self.draft["target"])  # re-read: the bench may have changed since the draft began
        if not add and not remove:
            await self.finish(interaction, "Nothing to change — the bench already looks like that.")
            return
        await self.working(interaction, "Saving the test bench…")
        try:
            line = await interaction.client.test_compose(self.reg, self.rs, add, remove, interaction.user.display_name, by_id=interaction.user.id)
        except (RegistryError, ValueError) as e:
            await self.refuse(interaction, str(e), self.start, "Back to the bench")
            return
        await self.finish(interaction, f"🧪 {line}.\nNext: `/gm test run` opens a rehearsal, `/gm test comp` the comp sandbox.")


@flow("test_compose")
async def open_test_compose(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    from .flows_misc import _refuse_non_officer

    if await _refuse_non_officer(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    await TestComposeWizard(reg, rs, interaction.user.id, getattr(interaction.client, "ico", None)).start(interaction)


# ---------------------------------------------------------------- /gm test comp

class TestCompWizard(Wizard):
    def __init__(self, reg: Registry, rs, owner_id: int):
        super().__init__(reg, owner_id, title="Try a comp", guard=_guard(reg))
        self.rs = rs
        raids = list(reg.profile.raids)
        twenty = next((r for r in raids if int(reg.raid_def(r).get("size") or 0) == 20), None)  # as the site's default
        self.draft = {"raid": twenty or (raids[0] if raids else None)}

    async def start(self, interaction: discord.Interaction) -> None:
        n = len(self.reg.test_members())
        if not n:
            await self.finish(interaction, "No test members yet: `/gm test compose` (any mix) or `/gm test seed` first.")
            return
        rid = self.draft["raid"]
        name = self.reg.raid_def(rid).get("name", rid) if rid else "no raid in the profile"
        items: list[discord.ui.Item] = []
        if len(self.reg.profile.raids) > 1:
            items.append(self.select("Which raid", wizard_opts.raids(self.reg, current=rid), self.raid_picked))
        items += [self.button("Try a comp", self.go, discord.ButtonStyle.success, disabled=not rid), self.button("Cancel", self.cancel)]
        await self.show(interaction, f"**{name}** — all {n} test members join a sandbox run two weeks out. It is never posted in Discord; "
                                     "Propose roster on its board builds the best comp from them.", *items)

    async def raid_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["raid"] = values[0]
        await self.start(interaction)

    async def go(self, interaction: discord.Interaction) -> None:
        from .raid_views import board_url

        rid = self.draft["raid"]
        if rid not in self.reg.profile.raids:
            await self.refuse(interaction, "That raid is no longer in the game profile.", self.start, "Pick again")
            return
        await self.working(interaction, "Opening the comp sandbox…")
        try:
            ev = await interaction.client.test_comp(self.reg, self.rs, rid, interaction.user.display_name, interaction.user.id)
        except (RegistryError, ValueError) as e:
            await self.refuse(interaction, str(e), self.start, "Back")
            return
        url = board_url(self.reg, ev)
        where = f"Open its board: {url}" if url else "Open it on the site: Rosters › the raid › Test."
        await self.finish(interaction, f"🧪 Comp sandbox {ev.key}: {len(self.reg.test_members())} test members joined. {where}")


@flow("test_comp")
async def open_test_comp(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    from .flows_misc import _refuse_non_officer

    if await _refuse_non_officer(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    await TestCompWizard(reg, rs, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- the digest's "Answer as puppets…"

class TestAnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"testans(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<key>[A-Za-z0-9_\-]+)"):
    """On the puppet digest in the tester's DMs: opens /gm test answer on that run (the puppets' sheet answers, and
    their Confirm / Can't or fill asks once it is locked). Officers of the run's guild only; survives restarts."""

    def __init__(self, gk: str | None, key: str):
        from .raid_buttons import _g

        super().__init__(discord.ui.Button(label="Answer as puppets…", style=discord.ButtonStyle.primary, custom_id=f"testans{_g(gk)}:{key}"[:100]))
        self.gk, self.key = gk, key

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["key"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client

        async def legacy(r):
            return self.key in bot.raids.store(r).events and await bot.is_officer_anywhere(r, interaction.user.id)
        reg, interaction = await guild_routing.resolve_button(interaction, self.gk, legacy)
        if not reg or not await bot.is_officer_anywhere(reg, interaction.user.id):
            if not interaction.response.is_done():
                await interaction.response.send_message("Officers only: the test bench.", ephemeral=True)
            return
        from .flows_misc import TestAnswerWizard

        rs = bot.raids.store(reg)
        ev = rs.events.get(self.key)
        if ev is None or ev.state in ("done", "cancelled"):
            await interaction.response.send_message("That test run is closed.", ephemeral=True)
            return
        await TestAnswerWizard(reg, rs, interaction.user.id, team=ev.team, guard=officer_anywhere_guard(reg)).start(interaction)


__all__ = ["COUNT_OPTS", "TestAnswerButton", "TestCompWizard", "TestComposeWizard", "bench_now", "compose_diff", "officer_anywhere_guard",
           "open_test_comp", "open_test_compose", "review_line"]
