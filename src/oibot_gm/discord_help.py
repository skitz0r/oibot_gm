"""/help (no LLM: the command list by tier) and /ask (Claude, with the manual and live state in context).
@mentioning the bot anywhere except the ops/analytics channels — or DMing it — is the same as /ask."""
from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands

from . import guild_routing, help as help_mod
from .discord_registry import Guilds
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
                me = reg.members.get(user_id) if user_id else None
                mine = next((c.label for c in (me.active() if me else []) if f"{rid}:{s['id']}" in c.rosters), None)
                head = (f"  ◦ {s['name']} ({'ad hoc: first come' if s['kind'] == 'pickup' else 'recurring'}): {reg.schedule_label(rid, s)}"
                        + ("" if s["kind"] == "pickup" else f" ({cfg.timezone})") + (f" — you're a regular ({mine})" if mine else ""))
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


class GuideSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"guide(?:\.(?P<g>[a-z0-9_-]{1,16}))?:menu"):
    """Persistent static guide for people outside the ask audience. The id carries the guild (`guide.<gk>:menu`); an
    old `guide:menu` resolves as before (several guilds in a DM: the person's guilds, the picker when more than one)."""

    def __init__(self, gk: str | None):
        super().__init__(discord.ui.Select(placeholder="What do you want to know?", custom_id=f"guide{'.' + gk if gk else ''}:menu", options=[discord.SelectOption(label=lab, value=k, description=d) for k, lab, d in GUIDE_OPTIONS]))
        self.gk = gk

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match, /):
        return cls(match["g"])

    async def callback(self, interaction: discord.Interaction):
        values = list(self.item.values)  # read before a picker: the press interaction carries no select values
        reg, interaction = await guild_routing.resolve_button(interaction, self.gk)
        if not reg:
            if not interaction.response.is_done():
                await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        key = values[0]
        if key == "apply":
            officer, role_ids = await asker_identity(interaction, reg)
            view = apply_view(reg, interaction.user.id, officer=officer, role_ids=role_ids)
        else:
            view = None
        await interaction.response.send_message(guide_text(reg, key, interaction.user.id)[:1950], ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **({"view": view} if view else {}))


async def asker_identity(interaction: discord.Interaction, reg) -> tuple[bool, list[int]]:
    """(officer, role ids) of whoever pressed or typed. In the server the interaction's Member has them; in a DM Discord
    hands over a bare User with no roles, so they come from the guild member, as plain text reads them
    (OibotGM.plain_identity) — else an officer or a member-role holder with no character got the Apply button."""
    if interaction.guild_id is None and getattr(interaction.client, "plain_identity", None) is not None:
        return await interaction.client.plain_identity(reg, interaction.user)
    r = await guild_routing.resolve_role(interaction.client, reg, interaction.user)  # a Member decides live; same predicate as is_officer
    return r.officer, r.role_ids


def guide_view(reg) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(GuideSelect(reg.key))
    return v


def guide_intro(reg) -> str:
    aud = reg.config.ask_audience
    who = {"officers": "officers", "confirmed": "members with a confirmed character", "registered": "registered members", "everyone": "everyone"}[aud]
    where = f" in <#{reg.config.registration_channel_id}>" if reg.config.registration_channel_id else ""
    unlock = f"; new here? apply{where} (**Apply to join**, or `/apply`) — members register there — to unlock them" if aud != "officers" else ""
    return f"Hi — I'm the {reg.config.name} raid bot. Pick a topic below. (Free-form questions are open to {who}{unlock}.)"


def apply_view(reg, user_id: int, *, officer: bool, role_ids=(), base: discord.ui.View | None = None) -> discord.ui.View | None:
    """`base` (or a new view) with the persistent **Apply to join** button (RegisterButton "apply", custom_id reg.<gk>:apply —
    it opens the application wizard from a DM too) when this person must apply first, applications are open and they
    have no application waiting; else `base` unchanged (None when there was none). Typed class/spec is never parsed."""
    from .discord_registry import RegisterButton

    a = reg.applicants.get(user_id)
    if not reg.config.recruitment.open or (a and a.status == "open") or not reg.must_apply(user_id, officer=officer, role_ids=role_ids):
        return base
    view = base or discord.ui.View(timeout=None)
    view.add_item(RegisterButton("apply", reg.key))
    return view


def register_help_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot) -> None:
    @tree.command(name="help", description="What the bot does and which commands you can use (no AI)")
    @app_commands.describe(topic="a section of the manual")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def help_cmd(interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None):
        reg, interaction = await guild_routing.need(interaction, reply=False, guilds=guilds, ask=True)  # None still shows the member tier
        if reg is None and interaction.response.is_done():
            return  # the DM server picker ran out: it already said "No server picked" — one response per interaction
        officer = bool(reg and (await asker_identity(interaction, reg))[0])  # in a DM too: the tiers follow the guild's roles
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
        e = discord.Embed(title="oibot_GM — what I do", colour=TEAL, description="I run registration, one sheet per raid run, roster health with suggestions for short tank/healer seats (an officer sends them), groups, filling freed seats by DM after lock, and (later) the loot council. Ask me anything in plain words with `/ask`, or @mention me. `/help topic:<name>` shows one part of the manual.")
        e.add_field(name="Everyone", value="\n".join(tiers["member"])[:1024], inline=False)
        if officer:
            e.add_field(name="Officers", value="\n".join(tiers["officer"])[:1024], inline=False)
            e.add_field(name="Owner", value="\n".join(tiers["owner"])[:1024], inline=False)
        if reg is None and interaction.guild_id:  # an unconfigured server: one response, with the way to set it up
            e.add_field(name="Not set up here", value=guild_routing.NOT_SET_UP, inline=False)
        e.set_footer(text="Buttons: #register card (Register / Add an alt / My status) · sheets (Join / Bench / No thanks; locked: Can't make it) · DMs (Confirm / Can't make it, fill: Confirm / Can't make it)")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @tree.command(name="ask", description="Ask the bot how something works or what to do next (it answers from its own manual and your record)")
    async def ask_cmd(interaction: discord.Interaction, question: str):
        reg, interaction = await guild_routing.need(interaction, guilds=guilds)
        if not reg:
            return
        officer, role_ids = await asker_identity(interaction, reg)
        if not reg.may_ask(interaction.user.id, officer):
            view = apply_view(reg, interaction.user.id, officer=officer, role_ids=role_ids, base=guide_view(reg))
            await interaction.response.send_message(guide_intro(reg), view=view, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await bot.help_answer(reg, interaction.user.id, officer, question)
        await interaction.followup.send(text, ephemeral=True)


ACT = "\x00act"  # help_answer's "this was a request to do something" (never shown)


def _bot_role(role) -> bool:
    """A role Discord made for a bot integration (the bot's own role, or another bot's): only that bot can hold it.
    Other managed roles (Server Booster, a Twitch/YouTube integration) hold people, so they still count."""
    try:
        return bool(role.is_bot_managed())
    except Exception:  # noqa: BLE001 — can't tell: judge it like any other role
        return False


def officer_only_channel(reg, channel) -> bool:
    """True when no non-officer can view `channel`: every guild role that isn't an officer role (nor Administrator /
    Manage Server, nor a bot's own managed role) is denied View Channel there, and no other overwrite lets a non-officer
    in. A non-Role overwrite that allows View Channel is fine only for the owner's id, the bot itself, a cached bot
    Member or a cached Member who is an officer: the bot runs without
    the members intent, so an uncached member's overwrite is a discord.Object nobody can vouch for and reads as public
    (so does an overwrite for a role the guild no longer resolves). Threads answer for their parent. Any error reads
    as False (public): unknown never leaks."""
    try:
        base = getattr(channel, "parent", None) or channel
        guild = channel.guild
        owner = reg.config.owner_discord_id
        me = getattr(guild, "me", None)
        for role in guild.roles:
            perms = role.permissions
            if perms.administrator or perms.manage_guild or reg.config.officer_by_roles([role]):
                continue
            if _bot_role(role):  # a bot's own managed role holds only that bot: no member reads through it
                continue
            if base.permissions_for(role).view_channel:
                return False
        for target, ow in (getattr(base, "overwrites", None) or {}).items():
            if isinstance(target, discord.Role) or not ow.view_channel:  # a live role is judged above, whatever its overwrite
                continue
            if target.id == owner or (me is not None and target.id == me.id):
                continue
            if isinstance(target, discord.Member):
                if getattr(target, "bot", False):  # a bot's own overwrite (the owner let the bot in): not a member
                    continue
                gp = target.guild_permissions
                if gp.administrator or gp.manage_guild or reg.config.officer_by_roles(target.roles):
                    continue
            return False  # a non-officer, or someone (or some role) that can't be resolved to check
        return True
    except Exception:  # noqa: BLE001
        return False


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
        x.ops = list(ops.recent_for(reg.key) if ops is not None and hasattr(ops, "recent_for") else [])[-15:]  # this guild's lines only
        provider = getattr(getattr(self, "ctx", None), "provider", None)
        x.llm = provider.summary() if provider is not None else "LLM off"
        from .discord_feed import feed_status

        x.feed = feed_status(self, reg, off="disabled")
        started = getattr(self, "started_at", None)
        x.uptime_s = int(time.time() - started) if started else None
        x.privilege, x.officers = self.help_privilege(reg, guild)
        from . import agents

        scope = agents.runs_scope(getattr(self, "registries", None), reg)  # this guild's runs only (all of them while it is the only one)
        x.agents = lambda: agents.overview(tz=reg.tz, guild=scope)
        x.agent_steps = lambda run_id: agents.steps(agents.read_run(run_id)) if agents.run_visible(run_id, scope) else []
        x.agent_runs = lambda job: [{**r, "started_label": agents.clock12(r["started"], reg.tz)} for r in agents.list_runs(job, guild=scope)[:3]]  # the Agents page's past runs
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

    def reply_private(self, reg, channel) -> bool:
        """Can only officers read a reply posted in `channel`? True for a DM (None, or a channel without a guild), the
        ops, applications and roster channels (and threads in them), and any channel no non-officer role or member can
        view (the analytics channel when the owner made it officer-only). Anything unknown counts as public.
        The help snapshot and the parser context follow this, not the asker's rank (design §5.25)."""
        if channel is None or getattr(channel, "guild", None) is None:
            return True
        cfg = reg.config
        ids = {getattr(channel, "id", None), getattr(channel, "parent_id", None)} - {None}
        if ids & ({cfg.ops_channel_id, cfg.applications_channel_id, cfg.roster_channel_id} - {None}):
            return True
        return officer_only_channel(reg, channel)

    def private_note(self, reg) -> str:
        """The line under an officer's answer in a public channel: where the officer view is."""
        where = f"<#{reg.config.ops_channel_id}>" if reg.config.ops_channel_id else "the ops channel"
        return f"-# Posted where members read it, so this is the member view — officers can ask in {where} or by DM for the officer view."

    async def help_answer(self, reg, user_id: int, officer: bool, question: str, may_act: bool = False, public: bool = False):
        """The answer text; None outside the ask audience (the caller shows the static guide). `may_act`: plain text
        may act for this person here — a message the model reads as a request to DO something returns ACT instead
        (the caller hands it to the change flow), so a plain question costs one model call. `public`: the reply is
        posted where members read it (not reply_private) — an officer then gets the member-level snapshot (no
        officer sections, no officer index, no other people's reasons) and a line saying where the officer view is."""
        audience_public = bool(public)  # anyone's reply in a channel others read: their own absence reasons stay out too
        public = bool(public and officer)
        if not reg.may_ask(user_id, officer):
            return None  # caller shows the static guide
        provider = self.ctx.provider
        if provider is None:
            return "I can't answer free-form questions right now (no LLM configured). `/help` lists what I do."
        rs = self.raids.store(reg)
        try:
            extras = self.help_extras(reg, officer and not public)
        except Exception:  # noqa: BLE001 — the snapshot still works without the bot-side lines
            extras = None
        try:
            ans = await asyncio.to_thread(help_mod.answer, provider, self.tree, reg, rs, user_id, officer, question, extras, audience_public)
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
        if public:
            note = self.private_note(reg)
            return text[:1950 - len(note) - 1] + "\n" + note
        return text[:1950]
