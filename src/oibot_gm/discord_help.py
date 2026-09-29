"""/help (no LLM: the command list by tier) and /ask (Claude, with the manual and live state in context).
@mentioning the bot anywhere except the ops/analytics channels — or DMing it — is the same as /ask."""
from __future__ import annotations

import asyncio
import time

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


def guide_text(reg, key: str, user_id: int | None = None) -> str:
    """Static answers for people who may not ask free-form questions: config + manual excerpts, no LLM. `user_id`
    makes *How to apply* speak to the asker (already a member · an application waiting · a past decision)."""
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
        return f"**How to register**\nNew to the guild? Apply first (`/apply` or **Apply to join**); members register and add alts:\n1. Go to {where} and press **Register / plan my main**.\n2. Pick class → spec → optional offspec; leave the name blank if the character doesn't exist yet.\n3. Your role follows your spec. An officer confirms named characters.\n4. There is nothing else to fill in: answer each run's sheet in {sheets} (Join / Bench / No thanks). `/me view` shows what the bot has on you."
    if key == "signups":
        parts = chunk_text(manual_section("## 4"), 1800)  # the start of the section, cut on a paragraph, and where the rest is
        return (parts[0] + ("\n\n-# The rest: `/help` → topic *Raids, sheets, lock, confirm and fill*." if len(parts) > 1 else "")) if parts else "See `/help` → topic *Raids, sheets, lock, confirm and fill*."
    if key == "apply":
        rec = cfg.recruitment
        m = reg.members.get(user_id) if user_id else None
        a = reg.applicants.get(user_id) if user_id else None
        if m and m.active():
            return "**Applying**\nYou're already a member — nothing to apply for. Add characters with **Register / plan my main** or **Add an alt** on the registration card, or `/me char add`."
        if a and a.status == "open":
            return (f"**Applying**\nYour application for **{a.label}** ({a.cls} {a.spec}) is waiting for an officer — sent {reg.local12(a.created_at)}. "
                    "You'll get a DM either way; there is nothing else to fill in.")
        if a and a.status == "accepted":
            return f"**Applying**\nYour application for **{a.label}** was accepted — no need to apply again. Register your characters with **Register / plan my main** on the registration card."
        if not rec.open:
            return f"**Applying**\n{cfg.name} isn't taking applications right now. Ask an officer if you'd like to be told when it opens."
        where = f" (or **Apply to join** on the card in <#{cfg.registration_channel_id}>)" if cfg.registration_channel_id else ""
        asks = f", then {len(rec.questions)} question(s) from the officers" if rec.questions else ""
        before = (f"Your last application ({a.label}) was {a.status}" + (f": “{a.decision_note}”" if a.decision_note else "") + ". You can apply again.\n") if a else ""
        return (f"**Applying**\n{before}New here? Use `/apply`{where}: class and spec, your character's first and last name and a logs link, "
                f"the raid schedules you can make{asks}, then a review screen before you submit. Officers review it on a card and "
                "you'll get a DM with the decision. Accepted applicants are registered as trial." + (f"\n> {rec.note}" if rec.note else ""))
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
        key = self.item.values[0]
        view = apply_view(reg, interaction.user.id, officer=is_officer(interaction, reg), role_ids=[r.id for r in getattr(interaction.user, "roles", None) or []]) if key == "apply" else None
        await interaction.response.send_message(guide_text(reg, key, interaction.user.id)[:1950], ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **({"view": view} if view else {}))


def guide_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(GuideSelect())
    return v


def guide_intro(reg) -> str:
    aud = reg.config.ask_audience
    who = {"officers": "officers", "confirmed": "members with a confirmed character", "registered": "registered members", "everyone": "everyone"}[aud]
    where = f" in <#{reg.config.registration_channel_id}>" if reg.config.registration_channel_id else ""
    unlock = f"; new here? apply{where} (**Apply to join**, or `/apply`) — members register there — to unlock them" if aud != "officers" else ""
    return f"Hi — I'm the {reg.config.name} raid bot. Pick a topic below. (Free-form questions are open to {who}{unlock}.)"


def apply_view(reg, user_id: int, *, officer: bool, role_ids=(), base: discord.ui.View | None = None) -> discord.ui.View | None:
    """`base` (or a new view) with the persistent **Apply to join** button (RegisterButton "apply", custom_id reg:apply —
    it opens the application wizard from a DM too) when this person must apply first, applications are open and they
    have no application waiting; else `base` unchanged (None when there was none). Typed class/spec is never parsed."""
    from .discord_registry import RegisterButton

    a = reg.applicants.get(user_id)
    if not reg.config.recruitment.open or (a and a.status == "open") or not reg.must_apply(user_id, officer=officer, role_ids=role_ids):
        return base
    view = base or discord.ui.View(timeout=None)
    view.add_item(RegisterButton("apply"))
    return view


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
            view = apply_view(reg, interaction.user.id, officer=officer, role_ids=[r.id for r in getattr(interaction.user, "roles", None) or []], base=guide_view())
            await interaction.response.send_message(guide_intro(reg), view=view, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await bot.help_answer(reg, interaction.user.id, officer, question)
        await interaction.followup.send(text, ephemeral=True)


ACT = "\x00act"  # help_answer's "this was a request to do something" (never shown)


class HelpMixin:
    """Needs self.ctx.provider, self.tree, self.raids, self.ops (and, when present: feed, started_at, get_guild,
    officiates — the running client)."""

    def help_extras(self, reg, officer: bool) -> help_mod.HelpExtras:
        """What only the running bot knows, collected on the event loop (cheap, no I/O) before help.answer's thread hop.
        A member's extras carry the visible channels only; ops, spend, feed, uptime, privilege and the jobs are for officers."""
        x = help_mod.HelpExtras()
        get_guild = getattr(self, "get_guild", None)
        guild = get_guild(reg.config.discord_guild_id) if get_guild and reg.config.discord_guild_id else None
        if guild is not None:
            me = getattr(guild, "me", None)
            try:
                x.channels = {c.id for c in guild.channels if me is None or c.permissions_for(me).view_channel} or None
            except Exception:  # noqa: BLE001 — unknown beats wrong: no "(not found)" marks
                x.channels = None
        if not officer:
            return x
        ops = getattr(self, "ops", None)
        x.ops = list(getattr(ops, "recent", None) or [])[-15:]
        provider = getattr(getattr(self, "ctx", None), "provider", None)
        x.llm = provider.summary() if provider is not None else "LLM off"
        feed = getattr(self, "feed", None)
        x.feed = feed.status() if feed is not None else "disabled"
        started = getattr(self, "started_at", None)
        x.uptime_s = int(time.time() - started) if started else None
        x.privilege, x.officers = self.help_privilege(reg, guild)
        from . import agents

        x.agents = lambda: agents.overview(tz=reg.tz)
        x.agent_steps = lambda run_id: agents.steps(agents.read_run(run_id))
        return x

    def help_privilege(self, reg, guild) -> tuple[dict[int, str], list[str]]:
        """owner | officer | member | outside per registered member, the web's privilege() rule, from what the client
        already has: the gateway cache and cached_member's recent fetches (a 404 cached there = outside). Members the bot
        hasn't seen are left out rather than guessed. Also the officers' display names."""
        owner = reg.config.owner_discord_id
        priv: dict[int, str] = {owner: "owner"} if owner else {}
        officiates = getattr(self, "officiates", None)
        if guild is None or officiates is None:
            return priv, []
        cache = self.__dict__.get("_member_cache", {})
        for uid, m in reg.members.items():
            if m.test or uid == owner:
                continue
            dm = guild.get_member(uid)
            if dm is None:
                hit = cache.get((guild.id, uid))
                if hit is None:
                    continue
                dm = hit[1]
            priv[uid] = "outside" if dm is None else ("officer" if officiates(dm, guild) else "member")
        names = {reg.members[uid].display_name for uid, p in priv.items() if p in ("owner", "officer") and uid in reg.members}
        for dm in getattr(guild, "members", None) or []:  # officers who never registered, if the cache has them
            if len(names) >= 20:
                break
            if not getattr(dm, "bot", False) and officiates(dm, guild):
                names.add(dm.display_name)
        return priv, sorted(names, key=str.lower)

    async def help_answer(self, reg, user_id: int, officer: bool, question: str, may_act: bool = False):
        """The answer text; None outside the ask audience (the caller shows the static guide). `may_act`: plain text
        may act for this person here — a message the model reads as a request to DO something returns ACT instead
        (the caller hands it to the change flow), so a plain question costs one model call."""
        if not reg.may_ask(user_id, officer):
            return None  # caller shows the static guide
        provider = self.ctx.provider
        if provider is None:
            return "I can't answer free-form questions right now (no LLM configured). `/help` lists what I do."
        rs = self.raids.store(reg)
        try:
            extras = self.help_extras(reg, officer)
        except Exception:  # noqa: BLE001 — the snapshot still works without the bot-side lines
            extras = None
        try:
            ans = await asyncio.to_thread(help_mod.answer, provider, self.tree, reg, rs, user_id, officer, question, extras)
        except Exception as e:  # noqa: BLE001
            await self.ops.emit(reg.config, "warn", f"help answer failed: {type(e).__name__}: {str(e)[:120]}")
            return "Something went wrong answering that; the officers have been told. `/help` lists what I do."
        if may_act and ans.acts:
            return ACT
        text = ans.answer.strip()
        if ans.commands:
            text += "\n\n" + " · ".join(f"`{c}`" for c in ans.commands[:4])
        if ans.for_officer:
            text += "\n-# This needs an officer."
        return text[:1950]
