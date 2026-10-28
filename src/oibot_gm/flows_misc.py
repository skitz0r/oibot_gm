"""Small wizards: the /gm test bench (seed · run · answer · clear; compose and comp are in flows_test) and /gm rule loot|comp.

The test bench is what officers repeat all session, so every flow here is ONE screen with today's defaults
preselected: `/gm test run` is a raid (only when there is more than one), a tempo and one button; the tempo is a set
of four minute marks that satisfy start > nudge > lock > confirm ≥ 0 by construction, so no ordering can be typed
wrong. `/gm rule` keeps its free text (it is prose for Claude, not a parser) but writes it in a paragraph modal under
the current document's tail, then takes exactly the old compile → Confirm/Discard path in discord_policy.
"""
from __future__ import annotations

import logging

import discord

from . import policy as policy_mod, wizard_opts
from .presets import DEFAULT_TEMPO, TEMPOS  # name → (label, start_in, nudge_in, lock_in, confirm_in), shared with plain text
from .registry import Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, flow

log = logging.getLogger(__name__)

ANSWER_COUNTS = (0, 1, 2, 3, 5, 8, 10, 15, 20)
SEED_COUNTS = (5, 10, 20)
STATUS_BUTTONS = (("in", "Join", discord.ButtonStyle.success), ("sub", "Bench", discord.ButtonStyle.primary), ("out", "No thanks", discord.ButtonStyle.secondary))


def _officer_guard(reg: Registry, what: str = "the test bench"):
    async def still_officer(i: discord.Interaction) -> str | None:
        from .discord_registry import is_officer

        return None if is_officer(i, reg) else f"Officers only: {what}."
    return still_officer


async def _refuse_non_officer(interaction: discord.Interaction, reg: Registry) -> bool:
    """The entry check (the guard repeats it on every press). True = refused."""
    from .discord_registry import is_officer

    if is_officer(interaction, reg):
        return False
    await interaction.response.send_message("Officers only (Manage Server or a configured officer role).", ephemeral=True)
    return True


def tempo_note(key: str) -> str:
    _, start, nudge, lock, confirm = TEMPOS[key]
    return f"starts in {start} min · nudge {nudge} · lock {lock} · confirm by {confirm} min before"


# ---------------------------------------------------------------- /gm test run

class TestRunWizard(Wizard):
    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Test run", guard=_officer_guard(reg))
        raids = list(reg.profile.raids)
        self.draft = {"raid": raids[0] if raids else None, "tempo": DEFAULT_TEMPO, "dm": False}

    async def start(self, interaction: discord.Interaction) -> None:
        d = self.draft
        items: list[discord.ui.Item] = []
        if len(self.reg.profile.raids) > 1:
            items.append(self.select("Which raid", wizard_opts.raids(self.reg, current=d["raid"]), self.raid_picked))
        items.append(self.select("Tempo", [Opt(k, v[0], tempo_note(k), default=k == d["tempo"]) for k, v in TEMPOS.items()], self.tempo_picked))
        items.append(self.button(f"DM everyone when it opens: {'on' if d['dm'] else 'off'}", self.toggle_dm,
                                 discord.ButtonStyle.primary if d["dm"] else discord.ButtonStyle.secondary))
        items.append(self.button("Open the test run", self.open, discord.ButtonStyle.success, disabled=not d["raid"]))
        items.append(self.button("Cancel", self.cancel))
        name = self.reg.raid_def(d["raid"]).get("name", d["raid"]) if d["raid"] else "no raid in the profile"
        await self.show(interaction, f"**{name}** · {TEMPOS[d['tempo']][0]} — {tempo_note(d['tempo'])}.\n"
                                     "-# Its cards post here; the puppets' DMs reach *your* DMs as one digest per batch, and the site's Test bench feed shows each.", *items)

    async def raid_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["raid"] = values[0]
        await self.start(interaction)

    async def tempo_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["tempo"] = values[0] if values[0] in TEMPOS else DEFAULT_TEMPO
        await self.start(interaction)

    async def toggle_dm(self, interaction: discord.Interaction) -> None:
        self.draft["dm"] = not self.draft["dm"]
        await self.start(interaction)

    async def open(self, interaction: discord.Interaction) -> None:
        raid, dm = self.draft["raid"], self.draft["dm"]
        _, start_in, nudge_in, lock_in, confirm_in = TEMPOS[self.draft["tempo"]]
        if raid not in self.reg.profile.raids:  # re-read: the profile may have been reloaded
            await self.refuse(interaction, "That raid is no longer in the game profile.", self.start, "Pick again")
            return
        await self.working(interaction, "Opening the test run…")
        reg, client = self.reg, interaction.client
        # RaidMixin.open_test_run: the one opener (plain text and the site too) — signup channel, else this one (never a
        # DM: refused, naming the setting); its cards post here (D20); the ops line
        try:
            ev, channel = await client.open_test_run(reg, client.raids.store(reg), raid, start_in=start_in, nudge_in=nudge_in, lock_in=lock_in, confirm_in=confirm_in,
                                                     dm=dm, by=interaction.user.display_name, by_id=interaction.user.id, channel=interaction.channel)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.start, "Back")
            return
        where = f" in {channel.mention}" if channel is not None and getattr(channel, "mention", None) else ""
        await self.finish(interaction, f"🧪 Test run {ev.key} open{where}: starts <t:{int(ev.start.timestamp())}:t>, nudge {nudge_in} min before, lock {lock_in} min before, "
                                       f"confirm by {confirm_in} min before. Next: `/gm test answer`; the puppets' DMs reach *your* DMs as one digest per batch.")


@flow("test_run")
async def open_test_run(interaction: discord.Interaction, reg: Registry, **seed) -> None:
    if await _refuse_non_officer(interaction, reg):
        return
    await TestRunWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- /gm test answer

class TestAnswerWizard(Wizard):
    """Puppets answer a TEST run (never a real one): on an open sheet Join / Bench / No thanks (a random mix, or one
    puppet); once locked, their open Confirm / Can't make it or fill asks too (Confirm / Can't). Every answer is
    SetupMixin.test_answer — the site's Test bench and MCP's test_bench go through the same verb. `team` preselects
    the run (the digest's Answer as puppets…); `guard` replaces the server-only officer check (a DM needs the async one)."""

    def __init__(self, reg: Registry, rs, owner_id: int, *, team: str | None = None, guard=None):
        super().__init__(reg, owner_id, title="Test answers", guard=guard or _officer_guard(reg))
        self.rs = rs
        self.draft = {"team": team, "counts": {"in": "0", "sub": "0", "out": "0", "yes": "0", "no": "0"}, "member": None, "page": 0}

    def run(self):
        """The run, re-read from the store at every step (it may have locked, been cancelled or ended)."""
        return self.rs.for_team(self.draft["team"]) if self.draft["team"] else None

    def test_runs(self) -> list:
        from . import raidcycle as rc

        return [ev for ev in self.rs.live() if rc.is_test_run(self.reg, ev)]

    def asks(self, ev) -> list:
        from .discord_pool import SetupMixin

        return SetupMixin.puppet_asks(self.reg, ev)

    async def start(self, interaction: discord.Interaction) -> None:
        if self.draft["team"] and self.run() is not None:
            await self.menu(interaction)
            return
        live = self.test_runs()
        if not live:
            await self.finish(interaction, "No live test run. Open one with `/gm test run`.")
            return
        if len(live) == 1:
            self.draft["team"] = live[0].team
            await self.menu(interaction)
            return
        keys = {ev.team for ev in live}
        await self.show(interaction, "Which test run do the test members answer?",
                        self.select("Pick the run", [o for o in wizard_opts.live_runs(self.reg, self.rs) if o.value in keys], self.run_picked),
                        self.button("Cancel", self.cancel))

    async def run_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["team"] = values[0]
        await self.menu(interaction)

    def _run_line(self, ev) -> str:
        from .raid_views import raid_name, sheet_state

        counts = " · ".join(f"{len(ev.by_status(s))} {label}" for s, label, _ in STATUS_BUTTONS)
        return f"**{raid_name(self.reg, ev)} · {self.reg.local12(ev.start)}** ({sheet_state(ev)}) — {counts}"

    async def menu(self, interaction: discord.Interaction, done: str = "") -> None:
        ev = self.run()
        if ev is None:
            await self.finish(interaction, (done + "\n" if done else "") + "That run is no longer live.")
            return
        head = (done + "\n") if done else ""
        n_asks = len(self.asks(ev)) if ev.state != "open" else 0
        tail = f"\n{n_asks} test member(s) hold an open ask (Confirm / Can't make it, or a fill ask)." if n_asks else ""
        await self.show(interaction, head + self._run_line(ev) + tail + "\nA random mix of test members, or one member?",
                        self.button("Random mix", self.mix_form, discord.ButtonStyle.primary),
                        self.button("One member", self.member_screen, discord.ButtonStyle.primary),
                        self.button("Done", self.done))

    async def done(self, interaction: discord.Interaction) -> None:
        await self.finish(interaction, "🧪 Done.")

    # ---- a random mix
    def mix(self, locked: bool = False) -> Form:
        c = self.draft["counts"]

        def opts(cur: str) -> list[Opt]:
            return [Opt(str(n), str(n), default=str(n) == cur) for n in ANSWER_COUNTS]
        if locked:  # the roster is set: the puppets answer their open asks
            return Form("Random mix", [
                Field("yes", "How many Confirm", opts(c["yes"]), description="picked at random from test members holding an open ask"),
                Field("no", "How many Can't make it", opts(c["no"])),
            ], self.mix_submitted)
        return Form("Random mix", [
            Field("in", "How many Join", opts(c["in"]), description="picked at random from test members who haven't answered"),
            Field("sub", "How many Bench", opts(c["sub"])),
            Field("out", "How many No thanks", opts(c["out"])),
        ], self.mix_submitted)

    async def mix_form(self, interaction: discord.Interaction) -> None:
        ev = self.run()
        await self.open_form(interaction, self.mix(locked=ev is not None and ev.state != "open"))

    async def mix_submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        self.draft["counts"].update({k: v.get(k) or "0" for k in ("in", "sub", "out", "yes", "no") if k in v})
        ev = self.run()
        if ev is None:
            await self.finish(interaction, "That run is no longer live; nothing was answered.")
            return
        await self.working(interaction, "Answering…")
        c = {k: int(v.get(k) or 0) for k in ("in", "sub", "out", "yes", "no")}  # only this form's fields: the other phase's stay 0
        try:  # SetupMixin.test_answer: the verb the site and MCP share (refreshes the sheet, writes the ops line)
            done = await interaction.client.test_answer(self.reg, self.rs, ev, join=c["in"], bench=c["sub"], out=c["out"], yes=c["yes"], no=c["no"],
                                                        by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.menu, "Back")
            return
        await self.answered(interaction, ev, done)

    # ---- one member
    async def member_screen(self, interaction: discord.Interaction) -> None:
        ev = self.run()
        if ev is None:
            await self.finish(interaction, "That run is no longer live.")
            return
        from . import raidcycle as rc

        members = self.reg.test_members()
        if not members:
            await self.refuse(interaction, "No test members. Seed some with `/gm test seed`.")
            return
        page = self.draft["page"] = self.draft["page"] if self.draft["page"] * MAX_OPTIONS < len(members) else 0
        chunk = members[page * MAX_OPTIONS:(page + 1) * MAX_OPTIONS]
        opts = []
        for m in chunk:
            s = ev.signups.get(str(m.discord_id))
            bits = [f"{m.main.cls} {m.main.spec}"] if m.main else []
            if s:
                bits.append(rc.LABELS.get(s.status, s.status))
            opts.append(Opt(str(m.discord_id), m.display_name, " · ".join(bits) or None))
        items: list[discord.ui.Item] = [self.select("Pick a test member", opts, self.member_picked)]
        pages = -(-len(members) // MAX_OPTIONS)
        if pages > 1:
            items.append(self.button("More →", self.next_page))
        items += [self.button("Back", self.back), self.button("Cancel", self.cancel)]
        note = f" (page {page + 1} of {pages})" if pages > 1 else ""
        await self.show(interaction, self._run_line(ev) + f"\nWhich test member answers?{note}", *items)

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.draft["page"] += 1
        await self.member_screen(interaction)

    async def back(self, interaction: discord.Interaction) -> None:
        await self.menu(interaction)

    async def member_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        m = self.reg.members.get(int(values[0]))
        if not m or not m.test:
            await self.refuse(interaction, "That test member is gone (the bench was cleared?).", self.member_screen, "Pick again")
            return
        self.draft["member"] = m.discord_id
        ev = self.run()
        holds = ev is not None and ev.state != "open" and any(mm.discord_id == m.discord_id for mm, _k in self.asks(ev))
        ask_buttons = [self.button("Confirm", self._status("yes"), discord.ButtonStyle.success),
                       self.button("Can't make it", self._status("no"), discord.ButtonStyle.danger)] if holds else []
        await self.show(interaction, f"How does **{m.display_name}** answer?" + ("\n-# They hold an open ask: Confirm / Can't make it answers it." if holds else ""),
                        *ask_buttons, *[self.button(label, self._status(st), style) for st, label, style in STATUS_BUTTONS],
                        self.button("Back", self.member_screen), self.button("Cancel", self.cancel))

    def _status(self, status: str):
        async def press(interaction: discord.Interaction) -> None:
            await self.answer_one(interaction, status)
        return press

    async def answer_one(self, interaction: discord.Interaction, status: str) -> None:
        ev = self.run()
        m = self.reg.members.get(self.draft["member"] or 0)
        if ev is None:
            await self.finish(interaction, "That run is no longer live; nothing was answered.")
            return
        if not m or not m.test:
            await self.refuse(interaction, "That test member is gone (the bench was cleared?).", self.member_screen, "Pick again")
            return
        await self.working(interaction, "Answering…")
        try:  # SetupMixin.test_answer: a callout when a seated puppet says No thanks after lock; yes / no answers their ask
            done = await interaction.client.test_answer(self.reg, self.rs, ev, member=m, status=status, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.member_screen, "Pick again")
            return
        await self.answered(interaction, ev, done)

    async def answered(self, interaction: discord.Interaction, ev, done: list[str]) -> None:
        await self.menu(interaction, ("🧪 " + "; ".join(done))[:1500] if done else "Nobody left to answer (seed more, or they've all answered).")


@flow("test_answer")
async def open_test_answer(interaction: discord.Interaction, reg: Registry, **seed) -> None:
    if await _refuse_non_officer(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    await TestAnswerWizard(reg, rs, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- /gm test seed

def seed_all_words(reg: Registry) -> str:
    """The whole bench in words, for the Seed choice: "the guild's 30 from the signup list" when the host put the guild's
    own test_roster.yaml in its data folder, else "all 30 in the built-in mix". N is the bench's real size."""
    n = len(reg.test_roster())
    own = (reg.store.root / reg.key / "test_roster.yaml").exists()
    return f"the guild's {n} from the signup list" if own else f"all {n} in the built-in mix"


def seed_opts(reg: Registry) -> list[Opt]:
    """5 / 10 / 20 / all N — N is the bench's real size (a guild's own test_roster.yaml may set it), never a constant."""
    n = len(reg.test_roster())
    have = len(reg.test_members())
    out = [Opt(str(c), f"{c} test members", "already seeded" if c <= have else None) for c in SEED_COUNTS if c < n]
    out.append(Opt(str(n), f"All {n}", seed_all_words(reg) + (" · already seeded" if n <= have else "")))
    return out


class TestSeedWizard(Wizard):
    def __init__(self, reg: Registry, owner_id: int):
        super().__init__(reg, owner_id, title="Seed the test bench", guard=_officer_guard(reg))

    async def start(self, interaction: discord.Interaction) -> None:
        have = len(self.reg.test_members())
        await self.show(interaction, f"{have} test member(s) seeded now. How many should the bench have? Seeding keeps the ones already there.",
                        self.select("How many", seed_opts(self.reg), self.picked), self.button("Cancel", self.cancel))

    async def picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        count = int(values[0])
        await self.working(interaction, "Seeding…")
        reg = self.reg
        line = await interaction.client.test_seed(reg, count, interaction.user.display_name, by_id=interaction.user.id)  # SetupMixin.test_seed: the site's Seed too (its ops line)
        await self.finish(interaction, f"🧪 {line}\n" + ", ".join(f"{m.display_name} ({m.main.cls} {m.main.spec})" for m in reg.test_members()[:30]))


@flow("test_seed")
async def open_test_seed(interaction: discord.Interaction, reg: Registry, **seed) -> None:
    if await _refuse_non_officer(interaction, reg):
        return
    await TestSeedWizard(reg, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- /gm rule loot|comp

RULE_DOCS = {"loot": "loot policy", "comp": "composition instructions"}
TAIL_CHARS = 1200


def policy_tail(text: str, n: int = TAIL_CHARS) -> str:
    """The end of the document, where the new rule goes — cut at a line break so it opens on a whole line."""
    text = text.rstrip()
    if len(text) <= n:
        return text
    tail = text[-n:]
    cut = tail.find("\n")
    return "…\n" + (tail[cut + 1:] if 0 <= cut < 200 else tail)


as_bullet = policy_mod.rule_bullet  # the rule as ONE markdown bullet (policy.rule_bullet: what every append writes)


class RuleWizard(Wizard):
    def __init__(self, reg: Registry, ps, owner_id: int, doc: str):
        super().__init__(reg, owner_id, title=f"New {doc} rule", guard=_officer_guard(reg, "policy rules"))
        self.ps, self.doc = ps, doc
        self.draft = {"text": ""}

    async def start(self, interaction: discord.Interaction) -> None:
        current = self.ps.read(self.doc)
        body = f"The {RULE_DOCS[self.doc]} ends like this — your rule is added as a new line at the end:\n```md\n{policy_tail(current).replace('```', 'ʼʼʼ') or '(empty)'}\n```" \
            "Write it in plain English; I'll show you my reading to confirm or discard."
        await self.show(interaction, body, self.button("Write the rule", self.write, discord.ButtonStyle.primary), self.button("Cancel", self.cancel))

    def form(self) -> Form:
        return Form(self.title, [Field("text", "The rule, in plain English", paragraph=True, default=self.draft["text"] or None, min_length=3, max_length=1000,
                                       description=f"added to the end of the {RULE_DOCS[self.doc]}")], self.submitted)

    async def write(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.form())

    async def submitted(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        from . import discord_policy

        text = (v.get("text") or "").strip()
        self.draft["text"] = text
        if not text:
            await self.refuse(interaction, "The rule is empty.", self.write, "Write the rule")
            return
        await self.working(interaction, "Reading the rule…")
        client = interaction.client
        # the one pending edit per doc (policy.begin_edit, as the site, MCP and plain text): Discard restores the text before it;
        # an appended rule never rides on ANOTHER officer's waiting draft (refused, naming them — design §5.45)
        try:
            policy_mod.append_rule(self.ps, self.doc, text, interaction.user.display_name, by_id=interaction.user.id, when=self.reg.local12)
        except policy_mod.PolicyPendingError as e:
            await self.finish(interaction, f"⛔ {e}\n-# Your rule was not added:\n> {text[:1200]}")
            return
        provider = getattr(getattr(client, "ctx", None), "provider", None)
        await discord_policy.compile_and_confirm(interaction, self.reg, self.ps, self.doc, provider, client.ops, followup=True)
        await self.finish(interaction, f"Added to the {RULE_DOCS[self.doc]} draft:\n> {text[:1500]}\n-# Confirm or discard my reading below.")


@flow("rule")
async def open_rule(interaction: discord.Interaction, reg: Registry, *, doc: str = "loot", **seed) -> None:
    if await _refuse_non_officer(interaction, reg):
        return
    if doc not in RULE_DOCS:
        await interaction.response.send_message(f"Unknown policy document {doc!r}.", ephemeral=True)
        return
    ps = interaction.client.policies.store(reg)
    await RuleWizard(reg, ps, interaction.user.id, doc).start(interaction)


__all__ = ["TEMPOS", "TestRunWizard", "TestAnswerWizard", "TestSeedWizard", "RuleWizard", "seed_opts", "policy_tail", "as_bullet",
           "open_test_run", "open_test_answer", "open_test_seed", "open_rule"]


# ---------------------------------------------------------------- /gm test clear

TEST_BENCH_EMPTY = "The test bench is already empty."  # test_clear_consequence when there is nothing to clear


def test_clear_consequence(reg: Registry, rs) -> list[str]:
    """What clearing the test bench does, from the state (dual interfaces §2.3): the `decide` screen's text, and the
    site's Delete-all confirm reads the same numbers."""
    from . import raidcycle as rc

    puppets = len(reg.test_members())
    runs = [ev for ev in rs.live() if rc.is_test_run(reg, ev)]
    if not puppets and not runs:
        return [TEST_BENCH_EMPTY]
    parts = []
    if runs:
        parts.append(f"{len(runs)} test run{'s are' if len(runs) != 1 else ' is'} cancelled")
    if puppets:
        parts.append(f"{puppets} puppet{'s are' if puppets != 1 else ' is'} deleted")
    line = ", ".join(parts) + " and the analytics cards resume."
    return [line[:1].upper() + line[1:], "Real members and their answers stay as they are.",
            "The cancelled test runs post nothing where members read."]


@flow("test_clear")
async def open_test_clear(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """/gm test clear: the consequence, then Clear the test bench / Back — the same screen every destructive verb gets
    (wizard.Wizard.decide). The write is SetupMixin.test_bench_clear, as the site's Delete all and plain text use."""
    if await _refuse_non_officer(interaction, reg):
        return
    rs = interaction.client.raids.store(reg)
    lines = test_clear_consequence(reg, rs)
    if lines == [TEST_BENCH_EMPTY]:
        await interaction.response.send_message(lines[0], ephemeral=True)
        return
    wiz = Wizard(reg, interaction.user.id, title="Clear the test bench", guard=_officer_guard(reg))

    async def do(i: discord.Interaction, _note: str | None) -> None:
        line = await i.client.test_bench_clear(reg, i.user.display_name, by_id=i.user.id)  # the verb writes its own audit line
        await wiz.finish(i, f"🧪 {line}")

    await wiz.decide(interaction, "\n".join(lines), do=do, do_label="Clear the test bench", danger=True)
