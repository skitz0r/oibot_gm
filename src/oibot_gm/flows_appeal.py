"""The Discord side of loot appeals (L32, design §5.49; appeals.py holds the verbs and the words).

- `me_loot_ask` (`/me loot-ask`): pick one of YOUR awards (a select: item · character, date · boss · MS/OS — never a
  typed reference) → one prose modal (validated, never parsed) → THE confirm screen (wizard.Wizard.decide: the
  consequence from appeals.open_consequence, your words quoted, `[Send to the council]` `[Edit my words]` `[Back]`).
  Nothing is written before Send. Awards you already asked about are left out of the select.
- `appeal_resolve` (the card's *Resolve…*): officers only, re-checked on every press — pick ONE outcome (a select over
  appeals.OUTCOMES) → the decide screen (appeals.resolve_consequence; `[Resolve]` `[Resolve with a note…]` `[Back]`, the
  note stays with the officers) → appeals.resolve_appeal: the member's notice, the card's final state. No DM, no LLM."""
from __future__ import annotations

import discord

from . import appeals
from .registry import Registry, RegistryError
from .wizard import Field, Form, Opt, Wizard, flow


class AskWizard(Wizard):
    def __init__(self, reg: Registry, uid: int, name: str, awards: list):
        super().__init__(reg, uid, title="Ask the council about an award")
        self.name, self.awards = name, {appeals.award_id(a): a for a in awards}
        self.aid: str | None = None
        self.text = ""

    def award(self):
        return self.awards.get(self.aid or "")

    async def pick_screen(self, interaction: discord.Interaction) -> None:
        opts = [Opt(aid, f"{a.item_name or a.item_id} · {a.raider}"[:100], description=appeals.award_words(self.reg, character=a.raider, item=a.item_name or a.item_id,
                                                                                                          received=a.received.isoformat(), boss=a.boss, offspec=a.offspec)[:100])
                for aid, a in list(self.awards.items())[:25]]
        await self.show(interaction, "Pick the award your question is about. Your words go to the officers as you write them; their answer comes "
                                     "back as a notice on your Me page and in `/me view` (no DM).\n-# One question per award. The Me page (My loot › Ask the council) does the same.",
                        self.select("Pick the award…", opts, self.picked), self.button("Cancel", self.cancel))

    async def picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.aid = values[0]
        await self.open_form(interaction, self.form())

    def form(self) -> Form:
        a = self.award()
        return Form("Your question for the council", [Field("text", "What should the council know?", paragraph=True, min_length=appeals.TEXT_MIN,
                                                             max_length=appeals.TEXT_MAX, default=self.text or None,
                                                             description=f"About {a.item_name or a.item_id} ({a.raider}). Plain words — the officers read them as written."[:100],
                                                             placeholder="e.g. I was told I'm next for this after the last two resets")], self.submitted)

    async def edit_words(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, self.form())

    async def submitted(self, interaction: discord.Interaction, values: dict[str, str]) -> None:
        self.text = values.get("text", "")
        try:
            self.text = appeals.check_text(self.text)
        except RegistryError as e:
            await self.refuse(interaction, str(e), self.edit_words, "Fix my words")
            return
        await self.review(interaction)

    async def review(self, interaction: discord.Interaction) -> None:
        a = self.award()
        text = (appeals.open_consequence(self.reg, a) + "\n\n" + appeals.award_words(self.reg, character=a.raider, item=a.item_name or a.item_id,
                                                                                    received=a.received.isoformat(), boss=a.boss, offspec=a.offspec)
                + "\n" + appeals.quote(self.name, self.text))
        await self.decide(interaction, text, do=self.send, do_label="Send to the council", extra=[self.button("Edit my words", self.edit_words)])

    async def send(self, interaction: discord.Interaction, _note: str | None) -> None:
        try:
            _ap, receipt = await appeals.open_appeal(interaction.client, self.reg, interaction.user.id, self.aid, self.text,
                                                     by=interaction.user.display_name, by_id=interaction.user.id, via="discord")
        except RegistryError as e:
            await self.refuse(interaction, str(e))
            return
        self.mark_saved()
        await self.finish(interaction, f"✅ {receipt}")


@flow("me_loot_ask")
async def me_loot_ask(interaction: discord.Interaction, reg: Registry, *, award: str | None = None) -> None:
    """Your own question about one of your awards (`award`: an award id to start on — skips the select)."""
    from .loot_ops import guild_ctx

    uid = interaction.user.id
    m = reg.members.get(uid)
    if m is None:
        await interaction.response.send_message("Nothing registered yet — /register to start.", ephemeral=True)
        return
    asked = {ap.id for ap in appeals.store_of(reg).of_member(uid)}
    mine = [a for a in appeals.member_awards_all(reg, guild_ctx(interaction.client, reg), m) if appeals.award_id(a) not in asked]
    if not mine:
        await interaction.response.send_message("No award to ask about: nothing recorded for you yet, or you've asked about each one already "
                                                "(the answers are on your Me page and in `/me view`).", ephemeral=True)
        return
    wiz = AskWizard(reg, uid, interaction.user.display_name, mine)
    if award and award in wiz.awards:
        wiz.aid = award
        await wiz.open_form(interaction, wiz.form())
        return
    await wiz.pick_screen(interaction)


@flow("appeal_resolve")
async def appeal_resolve(interaction: discord.Interaction, reg: Registry, *, aid: str, **_seed) -> None:
    """An officer closes appeal `aid` with one outcome (the card's Resolve…)."""
    bot = interaction.client
    if not await bot.is_officer_anywhere(reg, interaction.user.id):
        await interaction.response.send_message("Officers only.", ephemeral=True)
        return
    try:
        ap = appeals.store_of(reg).get(aid)
    except RegistryError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    if ap is None or ap.state != "open":
        why = "That question is gone." if ap is None else (f"Already resolved by {ap.resolved_by or 'an officer'}: {appeals.OUTCOME_LABELS.get(ap.outcome or '', ap.outcome)}.")
        await interaction.response.send_message(why, ephemeral=True)
        if ap is not None:
            await appeals.update_card(bot, reg, ap)  # a stale card catches up
        return

    async def guard(i: discord.Interaction) -> str | None:
        return None if await bot.is_officer_anywhere(reg, i.user.id) else "Officers only."

    wiz = Wizard(reg, interaction.user.id, title=f"Resolve: {ap.member_name} · {ap.item}"[:80], guard=guard)

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        outcome = values[0]

        async def do(di: discord.Interaction, note: str | None) -> None:
            try:
                _ap, line = await appeals.resolve_appeal(bot, reg, aid, outcome, note=note, by=di.user.display_name, by_id=di.user.id, via="discord")
            except RegistryError as e:
                await wiz.finish(di, f"❌ {e}")
                return
            wiz.mark_saved()
            await wiz.finish(di, f"✅ {line}")

        await wiz.decide(i, appeals.resolve_consequence(reg, ap, outcome), do=do, do_label="Resolve",
                         note={"label": "Note for the officers (optional)", "max_length": appeals.NOTE_MAX, "placeholder": "kept on the question; the member never sees it"})

    opts = [Opt(k, appeals.OUTCOME_LABELS[k], description=v[:100]) for k, v in appeals.OUTCOMES.items()]
    await wiz.show(interaction, appeals.award_words(reg, ap) + "\n" + appeals.quote(ap.member_name, ap.text)[:1200]
                   + "\n\nPick the outcome the member will read on their Me page.", wiz.select("Pick the outcome…", opts, picked), wiz.button("Back", wiz.cancel))


__all__ = ["AskWizard", "appeal_resolve", "me_loot_ask"]
