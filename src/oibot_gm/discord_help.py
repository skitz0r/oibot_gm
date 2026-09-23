"""/help (no LLM: the command list by tier) and /ask (Claude, with the manual and live state in context).
@mentioning the bot anywhere except the ops/analytics channels — or DMing it — is the same as /ask."""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from . import help as help_mod
from .discord_registry import Guilds, is_officer
from .ops import Ops

from .constants import TEAL  # noqa: E402
# /help topic: ONE choice per manual section (a picker, never a word to spell); the label says what is in it
TOPICS = {
    "channels": ("## 2", "Channels: where everything happens"),
    "website": ("## 2a", "The website"),
    "register": ("## 3", "Registering, characters and absences"),
    "cycle": ("## 4", "Raids, sheets, lock, confirm and fill"),
    "groups": ("## 5", "Groups and auras"),
    "analytics": ("## 6", "Analytics (officers)"),
    "officers": ("## 7", "Officer tools"),
    "setup": ("## 8", "Owner: setup"),
    "test": ("## 8a", "Rehearsing with the test bench"),
    "loot": ("## 9", "Loot"),
    "data": ("## 10", "Data, privacy, cost"),
    "limits": ("## 11", "What the bot can't do yet"),
}
TOPIC_CHOICES = [app_commands.Choice(name=label, value=key) for key, (_m, label) in TOPICS.items()]


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    """Split on paragraph boundaries so a long manual section is sent whole, in order, never cut mid-sentence."""
    out: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        if cur and len(cur) + 2 + len(para) > limit:
            out.append(cur)
            cur = ""
        while len(para) > limit:  # one paragraph longer than a message: break it on a line, else hard, in order
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(para[:cut])
            para = para[cut:].lstrip("\n")
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def manual_section(marker: str) -> str:
    text = help_mod.MANUAL.read_text() if help_mod.MANUAL.exists() else ""
    parts = text.split("\n## ")
    for p in parts:
        if ("## " + p).startswith(marker + " ") or ("## " + p).startswith(marker + "."):
            return "## " + p
    return ""


GUIDE_OPTIONS = [
    ("about", "About the guild", "who we are, what we raid, how big"),
    ("schedule", "Raid schedule", "raids, their run times and the next sheet"),
    ("register", "How to register", "the registration card and what happens next"),
    ("signups", "How signups work", "sheets, lock and confirmation, fill DMs"),
    ("apply", "How to apply", "joining as a recruit"),
    ("contact", "Who to contact", "officers and the owner"),
]


def guide_text(reg, key: str) -> str:
    """Static answers for people who may not ask free-form questions: config + manual excerpts, no LLM."""
    cfg = reg.config
    if key == "about":
        raids = ", ".join(f"{rd.get('name', rid)} ({rd.get('size')}-player{''.join(', ' + reg.schedule_label(rid, s) for s in reg.schedules(rid) if s['kind'] != 'pickup' and s.get('active', True))})" for rid in reg.profile.raids for rd in [reg.raid_def(rid)]) or "no raids configured yet"
        return f"**{cfg.name}**\n{cfg.about or 'A WoW: Forever raiding guild.'}\n\nRaids: {raids}.\nMembers registered: {len(reg.members)}."
    if key == "schedule":
        from . import raidcycle as rc

        lines = []
        for rid in reg.profile.raids:
            rd = reg.raid_def(rid)
            scheds = reg.schedules(rid)
            if not scheds:
                lines.append(f"• **{rd.get('name', rid)}** ({rd.get('size')}-player) — no run times yet")
                continue
            lines.append(f"• **{rd.get('name', rid)}** ({rd.get('size')}-player)")
            for s in scheds:  # every schedule, with its real next runs (Discord stamps render in the reader's own zone)
                sd = reg.schedule_def(rid, s["id"])
                head = f"  ◦ {s['name']}: {reg.schedule_label(rid, s)}" + ("" if s["kind"] == "pickup" else f" ({cfg.timezone})")
                if s["kind"] == "pickup" or not s.get("active", True):
                    lines.append(head)
                    continue
                nxt = rc.next_runs_of(reg, rid, s)
                lines.append(head + (" · next " + ", ".join(f"<t:{int(t.timestamp())}:f>" for t in nxt) + f"; each sheet opens {float(sd['signup_lead_hours']):g} h before" if nxt else " · nothing coming up"))
        where = f" in <#{cfg.signup_channel_id}>" if cfg.signup_channel_id else ""
        return "**Raid schedule**\n" + ("\n".join(lines) or "Nothing scheduled yet.") + f"\nEach run gets its own sheet{where}: answer Join / Bench / No thanks there."
    if key == "register":
        where = f"<#{cfg.registration_channel_id}>" if cfg.registration_channel_id else "the registration card an officer posts"
        sheets = f"<#{cfg.signup_channel_id}>" if cfg.signup_channel_id else "the signup channel"
        return f"**How to register**\n1. Go to {where} and press **Register / plan my main**.\n2. Pick class → spec → optional offspec; leave the name blank if the character doesn't exist yet.\n3. Your role follows your spec. An officer confirms named characters.\n4. There is nothing else to fill in: answer each run's sheet in {sheets} (Join / Bench / No thanks). `/me view` shows what the bot has on you."
    if key == "signups":
        parts = chunk_text(manual_section("## 4"), 1800)  # the start of the section, cut on a paragraph, and where the rest is
        return (parts[0] + ("\n\n-# The rest: `/help` → topic *Raids, sheets, lock, confirm and fill*." if len(parts) > 1 else "")) if parts else "See `/help` → topic *Raids, sheets, lock, confirm and fill*."
    if key == "apply":
        return "**Applying**\nUse `/apply` with your character, spec, logs link and a few words about you. Officers review it on a card and you'll get a DM with the decision. Accepted applicants are registered as trial."
    if key == "contact":
        officers = ", ".join(f"@{r}" for r in reg.officer_role_names()) or "anyone with Manage Server"
        owner = f"<@{cfg.owner_discord_id}>" if cfg.owner_discord_id else "not set"
        return f"**Who to contact**\nOfficers: {officers}. Owner: {owner}." + (f"\nOps channel: <#{cfg.ops_channel_id}>" if cfg.ops_channel_id else "")
    return "Pick a topic."


class GuideSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"guide:menu"):
    """Persistent static guide for people outside the ask audience."""

    def __init__(self):
        super().__init__(discord.ui.Select(placeholder="What do you want to know?", custom_id="guide:menu", options=[discord.SelectOption(label=lab, value=k, description=d) for k, lab, d in GUIDE_OPTIONS]))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        reg = interaction.client.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        await interaction.response.send_message(guide_text(reg, self.item.values[0])[:1950], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def guide_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(GuideSelect())
    return v


def guide_intro(reg) -> str:
    aud = reg.config.ask_audience
    who = {"officers": "officers", "confirmed": "members with a confirmed character", "registered": "registered members", "everyone": "everyone"}[aud]
    return f"Hi — I'm the {reg.config.name} raid bot. Pick a topic below. (Free-form questions are open to {who}; register in <#{reg.config.registration_channel_id}> to unlock them.)" if reg.config.registration_channel_id else f"Hi — I'm the {reg.config.name} raid bot. Pick a topic below. (Free-form questions are open to {who}.)"


def register_help_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot) -> None:
    @tree.command(name="help", description="What the bot does and which commands you can use (no AI)")
    @app_commands.describe(topic="a section of the manual")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def help_cmd(interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None):
        reg = guilds.for_interaction(interaction)
        officer = bool(reg and is_officer(interaction, reg))
        if topic:
            text = manual_section(TOPICS[topic.value][0])
            if not text:
                await interaction.response.send_message("That section is missing from the manual. `/ask` can still answer.", ephemeral=True)
                return
            parts = chunk_text(text)[:5]  # the whole section, in as many messages as it takes (Discord caps a message at 2000)
            await interaction.response.send_message(parts[0], ephemeral=True)
            for p in parts[1:]:
                await interaction.followup.send(p, ephemeral=True)
            return
        lines = help_mod.command_lines(tree).splitlines()
        tiers = {"member": [], "officer": [], "owner": []}
        for l in lines:
            path, _, rest = l.partition(" — ")
            desc, _, who = rest.rpartition(" — ")
            if path.startswith("/mock"):
                continue
            tiers.setdefault(who, []).append(f"`{path}` — {desc}")
        e = discord.Embed(title="oibot_GM — what I do", colour=TEAL, description="I run registration, one sheet per raid run, roster health, groups, filling freed seats by DM after lock, and (later) the loot council. Ask me anything in plain words with `/ask`, or @mention me. `/help topic:<name>` shows one part of the manual.")
        e.add_field(name="Everyone", value="\n".join(tiers["member"])[:1024], inline=False)
        if officer:
            e.add_field(name="Officers", value="\n".join(tiers["officer"])[:1024], inline=False)
            e.add_field(name="Owner", value="\n".join(tiers["owner"])[:1024], inline=False)
        e.set_footer(text="Buttons: #register card (Register / Add an alt / My status) · sheets (Join / Bench / No thanks; locked: Can't make it) · DMs (Confirm / Can't make it, fill: Confirm / Can't make it)")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @tree.command(name="ask", description="Ask the bot how something works or what to do next (it answers from its own manual and your record)")
    async def ask_cmd(interaction: discord.Interaction, question: str):
        reg = guilds.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
            return
        officer = is_officer(interaction, reg)
        if not reg.may_ask(interaction.user.id, officer):
            await interaction.response.send_message(guide_intro(reg), view=guide_view(), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await bot.help_answer(reg, interaction.user.id, officer, question)
        await interaction.followup.send(text, ephemeral=True)


class HelpMixin:
    """Needs self.ctx.provider, self.tree, self.raids, self.ops."""

    async def help_answer(self, reg, user_id: int, officer: bool, question: str) -> str:
        if not reg.may_ask(user_id, officer):
            return None  # caller shows the static guide
        provider = self.ctx.provider
        if provider is None:
            return "I can't answer free-form questions right now (no LLM configured). `/help` lists what I do."
        rs = self.raids.store(reg)
        try:
            ans = await asyncio.to_thread(help_mod.answer, provider, self.tree, reg, rs, user_id, officer, question)
        except Exception as e:  # noqa: BLE001
            await self.ops.emit(reg.config, "warn", f"help answer failed: {type(e).__name__}: {str(e)[:120]}")
            return "Something went wrong answering that; the officers have been told. `/help` lists what I do."
        text = ans.answer.strip()
        if ans.commands:
            text += "\n\n" + " · ".join(f"`{c}`" for c in ans.commands[:4])
        if ans.for_officer:
            text += "\n-# This needs an officer."
        return text[:1950]
