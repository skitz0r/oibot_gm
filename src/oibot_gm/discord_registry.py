"""Real (non-mock) commands: /register, /char, /roster, /gm.

Resolves the guild from the Discord server. Member commands reply ephemerally
and also work in DMs; officer commands need Manage Server or a configured
officer role; /gm config needs the owner."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import discord
from discord import app_commands

from . import ops as ops_mod
from .profiles import GameProfile
from .registry import RANKS, Registry, RegistryError
from .store import GitStore
from .wizard_flows import FLOWS

RANK_CHOICES = [app_commands.Choice(name=r, value=r) for r in RANKS]
STARTED = time.time()


class Guilds:
    """Discord guild id -> Registry (guild config + members + game profile)."""

    def __init__(self, store: GitStore, code_root: Path):
        self.store = store
        self.code_root = code_root
        self.by_discord: dict[int, Registry] = {}
        for gdir in store.root.iterdir():
            if (gdir / "guild.yaml").exists():
                import yaml

                cfg = yaml.safe_load((gdir / "guild.yaml").read_text())
                profile = GameProfile.load(code_root / "profiles" / cfg["game_profile"])
                reg = Registry(store, gdir.name, profile)
                self.by_discord[int(cfg["discord_guild_id"])] = reg

    def for_interaction(self, interaction: discord.Interaction) -> Registry | None:
        if interaction.guild_id and interaction.guild_id in self.by_discord:
            return self.by_discord[interaction.guild_id]
        # DMs: the member's guild is whichever one knows them (single-guild deployments: the only one)
        if len(self.by_discord) == 1:
            return next(iter(self.by_discord.values()))
        for reg in self.by_discord.values():
            if interaction.user.id in reg.members:
                return reg
        return None


def is_officer(interaction: discord.Interaction, reg: Registry) -> bool:
    if interaction.user.id == reg.config.owner_discord_id:
        return True
    m = interaction.user if isinstance(interaction.user, discord.Member) else None
    if m is None:
        return False
    if m.guild_permissions.manage_guild:
        return True
    return reg.config.officer_by_roles(m.roles)  # by role id (legacy names only until the bot resolved them)


def is_owner(interaction: discord.Interaction, reg: Registry) -> bool:
    return reg.config.owner_discord_id is not None and interaction.user.id == reg.config.owner_discord_id


from .constants import ROLES, TEAL  # noqa: E402
ROLE_CHOICES = [app_commands.Choice(name=r, value=r) for r in ROLES]


def char_line(ico, c) -> str:
    flags = []
    if c.is_main:
        flags.append("main")
    if c.status == "planned":
        flags.append("planned")
    flags.append(c.rank)
    if not c.confirmed_by and c.status != "planned":
        flags.append("unconfirmed")
    return f"{ico('class', c.cls)} **{c.label}** · {c.spec}{'/' + c.offspec if c.offspec else ''} · _{', '.join(flags)}_"


class RegisterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"reg:(?P<action>register|alt|status)"):
    """Persistent registration card: Register/Plan main → class → spec/offspec → roles → name (optional)."""

    LABELS = {"register": ("Register / plan my main", discord.ButtonStyle.success), "alt": ("Add an alt", discord.ButtonStyle.secondary), "status": ("My status", discord.ButtonStyle.secondary)}

    def __init__(self, action: str):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"reg:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        if self.action == "status":
            m = reg.members.get(interaction.user.id)
            if not m or not m.active():
                await interaction.response.send_message("Nothing registered yet — press **Register / plan my main**.", ephemeral=True)
                return
            primary, flex = reg.roles_of(m)
            lines = [char_line(bot.ico, c) for c in m.active()]
            lines.append(f"Roles: {primary or '?'}" + (f" (+{', '.join(flex)})" if flex else ""))
            rosters = sorted({k for c in m.active() for k in c.rosters})
            if rosters:
                lines.append("Rosters: " + ", ".join(rosters))
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return
        await FLOWS["register"](interaction, reg, slot="main" if self.action == "register" else "alt")


def registration_card(reg: Registry) -> discord.Embed:
    e = discord.Embed(title=f"{reg.config.name} · character registration", colour=TEAL,
                      description="**Register / plan my main** — class, spec, optional offspec, and the name if the character exists (leave it blank before launch; your role follows the spec).\n"
                                  "**Add an alt** — same flow for an alt.\n**My status** — what the bot has for you.\n\n"
                                  "Keyboard route: `/me plan …`, `/me char …`, `/me view`. Away for a while? `/me absent add` or the card in the absences channel.")
    e.set_footer(text="Your Discord account is your identity; character names can be added later with /me char name.")
    return e


def registration_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for a in ("register", "alt", "status"):
        v.add_item(RegisterButton(a))
    return v


# ---------------- /apply (recruitment intake): module level so the apply wizard can post the same card
def application_embed(reg: Registry, a, ico=None) -> discord.Embed:
    ico = ico or (lambda kind, key: "")
    e = discord.Embed(title=f"Application · {a.name}", colour=TEAL, description=f"{ico('class', a.cls)} **{a.cls} {a.spec}**{' / ' + a.offspec if a.offspec else ''} · from **{a.display_name}** (<@{a.discord_id}>)")
    if a.logs_url:
        e.add_field(name="Logs", value=a.logs_url[:200], inline=False)
    if a.availability:  # the applicant's free-text raid times (Applicant field, not a member setting)
        e.add_field(name="When they can raid", value=a.availability[:300], inline=False)
    if a.about:
        e.add_field(name="About", value=a.about[:600], inline=False)
    e.set_footer(text=f"status: {a.status}" + (f" · {a.decided_by}: {a.decision_note or ''}" if a.decided_by else " · officers: Accept / Decline below, or /roster applicant"))
    return e

async def decide_application(interaction: discord.Interaction, reg: Registry, discord_id: int, accept: bool, note: str | None):
    try:
        a, char = reg.decide(discord_id, accept, interaction.user.display_name, note)
    except RegistryError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True) if not interaction.response.is_done() else await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return
    verdict = f"✅ Accepted {a.display_name} as {a.name} (trial, confirmed)" if accept else f"⛔ Declined {a.display_name} ({a.name})"
    if interaction.response.is_done():
        await interaction.followup.send(verdict, ephemeral=True)
    else:
        await interaction.response.send_message(verdict, ephemeral=True)
    await interaction.client.ops.emit(reg.config, "info", f"{interaction.user.display_name}: {verdict}" + (f" — {note}" if note else ""))
    try:
        user = await interaction.client.fetch_user(a.discord_id)
        if accept:
            await user.send(f"✅ Your application for **{a.name}** to {reg.config.name} was accepted by {interaction.user.display_name}. You're registered as a trial; /me view shows your status." + (f"\n\n{note}" if note else ""))
        else:
            await user.send(f"Your application for **{a.name}** to {reg.config.name} wasn't accepted this time." + (f"\n\n{note}" if note else "") + "\n\nYou can apply again later.")
    except Exception:  # noqa: BLE001
        pass
    ch_id = reg.config.applications_channel_id or reg.config.ops_channel_id
    if a.message_id and ch_id:
        ch = interaction.client.get_channel(ch_id)
        try:
            msg = await ch.fetch_message(a.message_id)
            await msg.edit(embed=application_embed(reg, a, getattr(interaction.client, "ico", None)), view=None)
        except Exception:  # noqa: BLE001
            pass

class DeclineModal(discord.ui.Modal, title="Decline application"):
    note = discord.ui.TextInput(label="Note to the applicant (optional)", style=discord.TextStyle.paragraph, required=False, max_length=400)

    def __init__(self, reg: Registry, discord_id: int):
        super().__init__()
        self.reg, self.discord_id = reg, discord_id

    async def on_submit(self, interaction: discord.Interaction):
        await decide_application(interaction, self.reg, self.discord_id, False, str(self.note.value))

class ApplicationView(discord.ui.View):
    def __init__(self, reg: Registry, discord_id: int):
        super().__init__(timeout=None)
        self.reg, self.discord_id = reg, discord_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not is_officer(interaction, self.reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        await decide_application(interaction, self.reg, self.discord_id, True, None)

    @discord.ui.button(label="Decline…", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not is_officer(interaction, self.reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        await interaction.response.send_modal(DeclineModal(self.reg, self.discord_id))



async def post_application(client, reg: Registry, a) -> str:
    """Post an application's review card (Accept / Decline) where officers see it; used by /apply and the apply
    wizard. Returns a line saying where it went."""
    ch_id = reg.config.applications_channel_id or reg.config.ops_channel_id
    ch = client.get_channel(ch_id) if ch_id else None
    if not ch:
        await client.ops.emit(reg.config, "warn", f"application from {a.display_name} ({a.name}, {a.cls} {a.spec}) — no applications/ops channel configured; use /roster applicants")
        return "no applications channel is set, so officers will find it under /roster applicants"
    msg = await ch.send(embed=application_embed(reg, a, getattr(client, "ico", None)), view=ApplicationView(reg, a.discord_id))
    a.message_id = msg.id
    reg.save_applicant(a, f"application card posted for {a.name}")
    return f"posted for review in #{getattr(ch, 'name', 'applications')}"


def register_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: ops_mod.Ops, ico) -> None:
    async def need(interaction: discord.Interaction) -> Registry | None:
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
        return reg

    def _ns(interaction: discord.Interaction, *names: str):
        """First present option value from the interaction namespace (renamed options use their display name)."""
        for n in names:
            v = getattr(interaction.namespace, n, None)
            if v:
                return getattr(v, "value", v)
        return None

    async def member_char_autocomplete(interaction: discord.Interaction, current: str):
        """Characters of the member picked in the same command (officer commands with a `member` option)."""
        reg = guilds.for_interaction(interaction)
        member = _ns(interaction, "member")
        if reg is None or member is None:
            return []
        mid = getattr(member, "id", None) or (int(member) if str(member).isdigit() else None)
        m = reg.members.get(mid) if mid else None
        if not m:
            return []
        return [app_commands.Choice(name=f"{c.label} · {c.cls} {c.spec}"[:100], value=c.name or c.label) for c in m.active() if current.lower() in (c.name or c.label).lower()][:25]

    async def instance_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        return [app_commands.Choice(name=f"{r['name']} ({r.get('size', '?')})", value=rid) for rid, r in reg.profile.raids.items() if current.lower() in rid or current.lower() in r["name"].lower()][:25]

    async def any_char_autocomplete(interaction: discord.Interaction, current: str):
        """Every character in the guild (officers), planned ones by label with the owner's name."""
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        out = []
        for m, c in reg.all_characters():
            key = c.name or c.label
            if current.lower() in key.lower() or current.lower() in m.display_name.lower():
                out.append(app_commands.Choice(name=f"{c.label} · {c.cls} · {m.display_name}"[:100], value=key))
        return out[:25]

    # ---------------- /register
    @tree.command(name="register", description="Register a character: pick class and spec, then name it (blank = plan it)")
    @app_commands.describe(main="Make this your main")
    async def register(interaction: discord.Interaction, main: bool = False):
        reg = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        await FLOWS["register"](interaction, reg, slot="main" if main or not (m and m.main) else "alt")

    # ---------------- /plan (pre-launch: what are you going to play?)
    me = app_commands.Group(name="me", description="Your plan, characters and absences")
    plan = app_commands.Group(name="plan", description="Pre-launch: what you're planning to play (no character name needed)", parent=me)

    @plan.command(name="main", description="What you plan to main (no name needed yet)")
    async def plan_main(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, slot="main", plan_only=True)

    @plan.command(name="alt", description="What you plan to play as an alt")
    async def plan_alt(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, slot="alt", plan_only=True)

    @plan.command(name="roles", description="Extra roles your main can play besides its spec (flex); also on the website per character")
    @app_commands.choices(primary=ROLE_CHOICES, flex1=ROLE_CHOICES, flex2=ROLE_CHOICES, flex3=ROLE_CHOICES)
    async def plan_roles(interaction: discord.Interaction, primary: app_commands.Choice[str], flex1: app_commands.Choice[str] | None = None, flex2: app_commands.Choice[str] | None = None, flex3: app_commands.Choice[str] | None = None):
        reg = await need(interaction)
        if not reg:
            return
        flex = [f.value for f in (flex1, flex2, flex3) if f]
        try:
            m = reg.set_roles(interaction.user.id, interaction.user.display_name, primary.value, flex)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        main_role, flex_now = reg.roles_of(m)  # primary always follows the main's spec; the rest is flex on the main
        await interaction.response.send_message(f"✅ Roles: **{main_role or primary.value}**" + (f", also {', '.join(flex_now)}" if flex_now else ""), ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} roles: {main_role or primary.value}" + (f" (+{', '.join(flex_now)})" if flex_now else ""))


    # ---------------- /char
    char = app_commands.Group(name="char", description="Manage your characters", parent=me)

    @char.command(name="name", description="At launch: give your planned character its real name")
    async def char_name(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["char_name"](interaction, reg)

    @char.command(name="add", description="Register another character (alt)")
    async def char_add(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, slot="alt")

    @char.command(name="main", description="Make one of your characters your main (rank follows you)")
    async def char_main(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["char_main"](interaction, reg)

    @char.command(name="spec", description="Change a character's spec/offspec")
    async def char_spec(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["char_spec"](interaction, reg)

    @char.command(name="retire", description="Retire a character (history is kept)")
    async def char_retire(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["char_retire"](interaction, reg)

    @char.command(name="list", description="Show your registered characters")
    async def char_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        if not m or not m.active():
            await interaction.response.send_message("No characters yet — use /register.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(char_line(ico, c) for c in m.active()), ephemeral=True)


    # ---------------- /me absent, /me
    async def roster_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=t, value=t) for t in (reg.config.roster_keys() if reg else []) if current.lower() in t.lower()][:25]

    absent = app_commands.Group(name="absent", description="Future absences (reason is officer-only)", parent=me)

    @absent.command(name="add", description="Say you'll be away: pick the first day and how long")
    async def absent_add(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        await FLOWS["absence"](interaction, reg)

    @absent.command(name="list", description="Your upcoming absences")
    async def absent_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        await show_my_absences(interaction, reg)

    @absent.command(name="clear", description="Remove one of your absences (pick it from the list)")
    async def absent_clear(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        await show_my_absences(interaction, reg)

    async def show_my_absences(interaction: discord.Interaction, reg: Registry) -> None:
        """Your upcoming absences, each with its own Clear button — the same view as the absences card's *My absences*."""
        from .discord_pool import clear_absences_view

        m = reg.members.get(interaction.user.id)
        ups = m.upcoming_absences(reg.now_local().date().isoformat()) if m else []
        if not ups:
            await interaction.response.send_message("No upcoming absences. Add one with `/me absent add`.", ephemeral=True)
            return
        lines = [f"• {reg.span_label(a.start, a.end)}" + (f" — {a.reason}" if a.reason else "") for a in ups]
        await interaction.response.send_message("\n".join(lines)[:1900], view=clear_absences_view(reg, m, ups[:5]), ephemeral=True)


    @tree.command(name="apply", description="Apply to join the guild")
    async def apply(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["register"](interaction, reg, mode="apply")

    @me.command(name="view", description="Your plan, characters and upcoming absences")
    async def me(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        if not m:
            await interaction.response.send_message("Nothing registered yet — /register to start.", ephemeral=True)
            return
        today = reg.now_local().date().isoformat()
        e = discord.Embed(title=f"{m.display_name} · {reg.config.name}", colour=TEAL)
        e.add_field(name="Characters", value="\n".join(char_line(ico, c) for c in m.active()) or "none", inline=False)
        rosters = sorted({k for c in m.active() for k in c.rosters})
        if rosters:
            e.add_field(name="Rosters", value=", ".join(f"{k} ({next(c.label for c in m.active() if k in c.rosters)})" for k in rosters), inline=True)
        primary, flex = reg.roles_of(m)
        e.add_field(name="Roles", value=f"{primary or '?'}" + (f" (+{', '.join(flex)})" if flex else "") + "\n-# from your spec/offspec · `/me plan roles` to add more", inline=True)
        e.add_field(name="DMs", value="off (asks show on the Me page)" if m.dm_opt_out else "on", inline=True)
        ups = m.upcoming_absences(today)
        e.add_field(name="Upcoming absences", value="\n".join(reg.span_label(a.start, a.end) for a in ups) or "none", inline=True)
        e.set_footer(text="Sheets: answer each run in the signup channel (Join / Bench / No thanks). Attendance and loot history appear here once the raid ledger is live.")
        await interaction.response.send_message(embed=e, ephemeral=True)

    tree.add_command(me)

    # ---------------- /roster (officers)
    roster = app_commands.Group(name="roster", description="Officer: the guild's registered characters")

    async def officer(interaction: discord.Interaction) -> Registry | None:
        reg = await need(interaction)
        if reg and not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only (Manage Server or a configured officer role).", ephemeral=True)
            return None
        return reg

    @roster.command(name="list", description="All registered characters by class")
    async def roster_list(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        chars = reg.all_characters()
        if not chars:
            await interaction.response.send_message("Nobody has registered yet.", ephemeral=True)
            return
        e = discord.Embed(title=f"{reg.config.name} · {len(chars)} characters · {len(reg.members)} members", colour=TEAL)
        by_cls: dict[str, list[str]] = {}
        for m, c in chars:
            by_cls.setdefault(c.cls, []).append(f"{'★ ' if c.is_main else ''}{c.label} · {c.spec} · {c.rank}{' ⏳' if not c.confirmed_by else ''} · {m.display_name}")
        for cls, lines in by_cls.items():
            e.add_field(name=f"{ico('class', cls)} {cls} ({len(lines)})", value="\n".join(lines)[:1000], inline=True)
        pend = reg.pending()
        from .flows_roster import confirm_pending_view

        e.set_footer(text=f"★ main · ⏳ unconfirmed ({len(pend)})")
        view = confirm_pending_view(reg, len(pend))
        await interaction.response.send_message(embed=e, view=view or discord.utils.MISSING, ephemeral=True)

    @roster.command(name="confirm", description="Confirm registered characters: pick from the unconfirmed ones, one after another")
    async def roster_confirm(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_confirm"](interaction, reg)

    @roster.command(name="rank", description="Set a member's character rank: pick the character and rank")
    @app_commands.describe(member="whose character")
    async def roster_rank(interaction: discord.Interaction, member: discord.User):
        reg = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_rank"](interaction, reg, member=member)

    @roster.command(name="set-main", description="Set a member's main on their behalf: pick the character")
    @app_commands.describe(member="whose main")
    async def roster_set_main(interaction: discord.Interaction, member: discord.User):
        reg = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_set_main"](interaction, reg, member=member)

    @roster.command(name="overview", description="Who is planning to play what: classes, roles, flexibility, buff coverage")
    @app_commands.describe(size="raid size to check against (default: the first raid's size or 20)")
    async def roster_plan(interaction: discord.Interaction, size: int | None = None):
        reg = await need(interaction)
        if not reg:
            return
        s = reg.plan_summary()
        if not s["mains"]:
            await interaction.response.send_message("Nobody has planned a main yet — point people at the registration card (`/gm config registration-channel`) or `/me plan main`.", ephemeral=True)
            return
        team = reg.config.rosters[0] if reg.config.rosters else {}
        n = size or int(team.get("size") or reg.raid_def(team.get("instance")).get("size") or 20)
        bounds = reg.role_bounds(team.get("instance"), n)
        e = discord.Embed(title=f"{reg.config.name} · plan · {s['mains']} mains", colour=TEAL)
        for cls, lines in sorted(s["by_class"].items(), key=lambda kv: -len(kv[1])):
            e.add_field(name=f"{ico('class', cls)} {cls} ({len(lines)})", value="\n".join(lines)[:1000], inline=True)
        role_lines = []
        for r in ROLES:
            have = s["by_role"].get(r, 0)
            need_min = bounds.get(r, {}).get("min", 0)
            flex = s["flex"].get(r, [])
            mark = "🟢" if not need_min or have >= need_min else ("🟡" if have + len(flex) >= need_min else "🔴")
            role_lines.append(f"{mark} {ico('role', r)} {r}: **{have}**" + (f" / need {need_min}" if need_min else "") + (f" · flex: {', '.join(flex)}" if flex else ""))
        e.add_field(name=f"Roles vs a {n}-man", value="\n".join(role_lines), inline=False)
        ids = {b.short: b.id for b in reg.profile.party_buffs()}
        missing = [b for b, who in s["providers"].items() if not who]
        thin = [(b, who[0]) for b, who in s["providers"].items() if len(who) == 1]
        if missing or thin:
            val = ""
            if missing:
                val += "Nobody: " + " ".join(f"{ico('buff', ids.get(b, ''))}" for b in missing) + "\n" + ", ".join(missing) + "\n"
            if thin:
                val += "Only one: " + ", ".join(f"{ico('buff', ids.get(b, ''))} {b} ({who})" for b, who in thin)
            e.add_field(name="Buff coverage", value=val[:1000], inline=False)
        if s["alts"]:
            e.add_field(name=f"Alts ({len(s['alts'])})", value=", ".join(f"{w} · {c}" for w, c in s["alts"])[:1000], inline=False)
        asks = [f"{max(0, bounds[r]['min'] - s['by_role'].get(r, 0))} {r}" for r in ("tank", "healer") if bounds.get(r) and s["by_role"].get(r, 0) < bounds[r]["min"]]
        e.set_footer(text=(("Recruiting ask: " + ", ".join(asks) + " · ") if asks else "") + "counts use each member's primary role preference, else their main spec's role")
        await interaction.response.send_message(embed=e, ephemeral=not is_officer(interaction, reg))

    @roster.command(name="add", description="Add a member's character to a roster: pick the roster and character")
    @app_commands.describe(member="who to add")
    async def roster_add(interaction: discord.Interaction, member: discord.User):
        reg = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_add"](interaction, reg, member=member)

    @roster.command(name="remove", description="Take a member off a roster: pick one they're on (they can still sign as sub)")
    @app_commands.describe(member="who to remove")
    async def roster_remove(interaction: discord.Interaction, member: discord.User):
        reg = await officer(interaction)
        if not reg:
            return
        await FLOWS["roster_remove"](interaction, reg, member=member)

    @roster.command(name="members", description="A roster's characters by role")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def roster_members_cmd(interaction: discord.Interaction, roster: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        key = roster or reg.config.roster_keys()[0]
        members = reg.roster_members(key)
        if not members:
            await interaction.response.send_message(f"Roster **{key}** has no curated members yet, so every registered main counts. `/roster add @member` to curate it.", ephemeral=True)
            return
        by_role: dict[str, list[str]] = {}
        for m, c in members:
            role = reg.roles_of(m)[0] or reg.profile.spec(c.cls, c.spec).role
            by_role.setdefault(role, []).append(f"{ico('class', c.cls)} **{c.label}** · {c.spec}{'/' + c.offspec if c.offspec else ''} · {m.display_name}")
        cfg = reg.config.roster(key) or {}
        e = discord.Embed(title=f"Roster {key} · {len(members)}/{cfg.get('size', '?')} · {cfg.get('schedule') or 'no schedule'}", colour=TEAL)
        for r in ROLES:
            if by_role.get(r):
                e.add_field(name=f"{ico('role', r)} {r} ({len(by_role[r])})", value="\n".join(by_role[r])[:1000], inline=True)
        await interaction.response.send_message(embed=e, ephemeral=not is_officer(interaction, reg))

    @roster.command(name="absences", description="Upcoming absences across the guild (with reasons)")
    @app_commands.describe(days="How far ahead to look (default 30)")
    async def roster_absences(interaction: discord.Interaction, days: int = 30):
        reg = await officer(interaction)
        if not reg:
            return
        from datetime import timedelta

        today = reg.now_local().date()
        rows = reg.absences_between(today.isoformat(), (today + timedelta(days=days)).isoformat())
        lines = [f"• **{m.display_name}** ({(m.main.name if m.main else '-')}) {a.start}" + (f" → {a.end}" if a.end != a.start else "") + (f" — {a.reason}" if a.reason else "") + (f" _(by {a.by})_" if a.by != m.display_name else "") for m, a in rows]
        await interaction.response.send_message("\n".join(lines)[:1900] or f"No absences in the next {days} days.", ephemeral=True)

    @roster.command(name="absent", description="Record an absence on a member's behalf: pick the days, no typing")
    @app_commands.describe(member="who is away")
    async def roster_absent(interaction: discord.Interaction, member: discord.User):
        reg = await officer(interaction)
        if not reg:
            return
        await FLOWS["absence"](interaction, reg, member=member)

    @roster.command(name="applicants", description="Open applications")
    async def roster_applicants(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        opens = reg.open_applicants()
        if not opens:
            await interaction.response.send_message("No open applications.", ephemeral=True)
            return
        await interaction.response.send_message(embeds=[application_embed(reg, a, ico) for a in opens[:10]], ephemeral=True)

    @roster.command(name="applicant", description="Accept or decline an application by member")
    @app_commands.choices(decision=[app_commands.Choice(name="accept", value="accept"), app_commands.Choice(name="decline", value="decline")])
    async def roster_applicant(interaction: discord.Interaction, member: discord.User, decision: app_commands.Choice[str], note: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        await decide_application(interaction, reg, member.id, decision.value == "accept", note)

    tree.add_command(roster)

    # ---------------- /gm (owner / officers)
    gm = app_commands.Group(name="gm", description="oibot_GM administration")
    config = app_commands.Group(name="config", description="Owner: bot configuration", parent=gm)

    @config.command(name="owner", description="Claim ownership (first caller) or transfer it")
    async def cfg_owner(interaction: discord.Interaction, user: discord.User | None = None):
        reg = await need(interaction)
        if not reg:
            return
        if reg.config.owner_discord_id and not is_owner(interaction, reg):
            await interaction.response.send_message("Only the current owner can transfer ownership.", ephemeral=True)
            return
        if not reg.config.owner_discord_id:  # first claim: only the server owner or someone with Manage Server
            m = interaction.user if isinstance(interaction.user, discord.Member) else None
            if not (m and interaction.guild and (interaction.guild.owner_id == m.id or m.guild_permissions.manage_guild)):
                await interaction.response.send_message("Nobody owns this guild yet; the Discord server owner (or someone with Manage Server) claims it first.", ephemeral=True)
                return
        target = user or interaction.user
        reg.config.owner_discord_id = target.id
        reg.save_config(f"owner → {target.display_name}")
        await interaction.response.send_message(f"✅ Owner: {target.mention}", ephemeral=True)
        await ops.emit(reg.config, "warn", f"owner set to {target.display_name} by {interaction.user.display_name}")

    @config.command(name="ops-channel", description="Owner: where the bot mirrors every action")
    async def cfg_ops(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        reg.config.ops_channel_id = channel.id
        reg.save_config(f"ops channel → #{channel.name}")
        await interaction.response.send_message(f"✅ Ops feed → {channel.mention}", ephemeral=True)
        await ops.emit(reg.config, "info", f"ops feed connected by {interaction.user.display_name}")

    @config.command(name="applications-channel", description="Owner: where application review cards are posted (defaults to the ops channel)")
    async def cfg_apps(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        reg.config.applications_channel_id = channel.id
        reg.save_config(f"applications channel → #{channel.name}")
        await interaction.response.send_message(f"✅ Applications → {channel.mention}", ephemeral=True)

    @config.command(name="officer-role", description="Owner: add or remove a Discord role that counts as officer")
    async def cfg_role(interaction: discord.Interaction, role: discord.Role, remove: bool = False):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        # stored by role id: a rename keeps the officers, a same-named role someone else creates grants nothing
        if interaction.guild:
            await asyncio.to_thread(reg.resolve_officer_roles, interaction.guild)  # legacy names → ids first, and the name cache for the reply
        await asyncio.to_thread(reg.set_officer_role, role.id, not remove, interaction.user.display_name)
        names = ", ".join(reg.officer_role_names()) or "(none; Manage Server only)"
        await interaction.response.send_message(f"✅ Officer roles: {names}", ephemeral=True)
        await ops.emit(reg.config, "warn", f"officer roles now {names} (by {interaction.user.display_name})")

    @config.command(name="roster-channel", description="Owner: private channel for roster overviews, proposals and officer health cards")
    async def cfg_roster_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        reg.config.roster_channel_id = channel.id
        reg.save_config(f"roster channel → #{channel.name}")
        await interaction.response.send_message(f"✅ Roster management → {channel.mention}", ephemeral=True)

    @config.command(name="registration-channel", description="Owner: public read-only channel where the bot keeps the registration card (buttons open to everyone)")
    async def cfg_registration_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            note = await interaction.client.post_registration_card(reg, channel, interaction.user.display_name)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ I can't post in {channel.mention} (need View Channel, Send Messages, Embed Links there).", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Registration card posted and pinned in {channel.mention}" + (f"\n{note}" if note else ""), ephemeral=True)
        await ops.emit(reg.config, "info", f"registration channel → #{channel.name} (by {interaction.user.display_name})")

    @config.command(name="analytics-channel", description="Owner: officer channel with live pool-readiness cards per roster and a change log")
    async def cfg_analytics_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if reg.config.analytics_channel_id != channel.id:
            reg.config.analytics_channel_id, reg.config.analytics_message_ids = channel.id, {}
            reg.save_config(f"analytics channel → #{channel.name}", notify=False)
        try:
            msgs = await interaction.client.refresh_pool(reg)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ I can't post in {channel.mention} (need Send Messages, Embed Links, Attach Files, Manage Messages to pin).", ephemeral=True)
            return
        await interaction.followup.send(f"✅ {len(msgs)} analytics card(s) posted in {channel.mention}; they re-post at the bottom after every registry change, with a change-log line above.", ephemeral=True)
        await ops.emit(reg.config, "info", f"analytics channel → #{channel.name} (by {interaction.user.display_name})")

    @config.command(name="aura", description="Owner: what we know about a buff — scope, stacking family, strength, status, who benefits")
    async def cfg_aura(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["aura"](interaction, reg)

    @config.command(name="absences-channel", description="Owner: public channel with the 'I'll be away' card; the bot posts one line per absence there")
    async def cfg_absences_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.client.post_absences_card(reg, channel, interaction.user.display_name)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ I can't post in {channel.mention} (need View Channel, Send Messages, Embed Links there).", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Absence card posted and pinned in {channel.mention}. Members press *I'll be away*; each absence is announced there (reasons stay officer-only) and pre-fills Out on the sheets it overlaps.", ephemeral=True)
        await ops.emit(reg.config, "info", f"absences channel → #{channel.name} (by {interaction.user.display_name})")

    @config.command(name="signup-channel", description="Owner: where raid sheets are posted")
    async def cfg_signup(interaction: discord.Interaction, channel: discord.TextChannel):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        reg.config.signup_channel_id = channel.id
        reg.save_config(f"signup channel → #{channel.name}")
        await interaction.response.send_message(f"✅ Sheets → {channel.mention}", ephemeral=True)

    @config.command(name="ask-audience", description="Owner: who may ask the bot free-form questions (/ask, DMs, @mentions); others get the static guide")
    @app_commands.choices(audience=[app_commands.Choice(name=a, value=a) for a in ("officers", "confirmed", "registered", "everyone")])
    async def cfg_ask_audience(interaction: discord.Interaction, audience: app_commands.Choice[str]):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        reg.config.ask_audience = audience.value
        reg.save_config(f"ask audience → {audience.value}")
        await interaction.response.send_message(f"✅ Free-form questions: **{audience.value}**. Everyone else gets the static guide (about, schedule, how to register, signups, apply, contact).", ephemeral=True)

    @config.command(name="raid", description="Owner: a raid's rules — run times, signup cadence, make-up, weights, lockout, notes")
    async def cfg_raid(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["raid_config"](interaction, reg)

    @config.command(name="about", description="Owner: one-paragraph public blurb for the static guide ('About the guild')")
    async def cfg_about(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["about"](interaction, reg)

    @config.command(name="timezone", description="Owner: the guild's timezone for every run time, lock and lockout")
    async def cfg_tz(interaction: discord.Interaction):
        reg = await need(interaction)
        if reg:
            await FLOWS["timezone"](interaction, reg)

    @config.command(name="show", description="Effective configuration")
    async def cfg_show(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        if not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        import yaml as _y

        await interaction.response.send_message("```yaml\n" + _y.safe_dump(reg.config.model_dump(), sort_keys=False)[:1800] + "\n```", ephemeral=True)

    async def roster_regcard(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        await interaction.response.send_message(embed=registration_card(reg), view=registration_view())
        try:
            msg = await interaction.original_response()
            await msg.pin()
        except Exception:  # noqa: BLE001
            pass
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} posted the registration card in #{getattr(interaction.channel, 'name', '?')}")

    @gm.command(name="status", description="Bot health: data repo, registry, spend, recent ops")
    async def gm_status(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        st = guilds.store
        up = int(time.time() - STARTED)
        provider = getattr(interaction.client, "ctx", None) and interaction.client.ctx.provider
        chars = reg.all_characters()
        e = discord.Embed(title=f"oibot_GM status · {reg.config.name}", colour=TEAL)
        e.add_field(name="Bot", value=f"up {up // 3600}h{(up % 3600) // 60}m · profile {reg.profile.name}", inline=True)
        e.add_field(name="Data repo", value=f"{st.root.name} @ {st.head()} · push {'on' if st.push_enabled else 'off'}", inline=True)
        e.add_field(name="Registry", value=f"{len(reg.members)} members · {len(chars)} characters · {len(reg.pending())} unconfirmed", inline=True)
        e.add_field(name="Config", value=f"owner {'<@%d>' % reg.config.owner_discord_id if reg.config.owner_discord_id else '—'} · ops {'<#%d>' % reg.config.ops_channel_id if reg.config.ops_channel_id else '—'} · officer roles {', '.join(reg.officer_role_names()) or '—'}", inline=False)
        e.add_field(name="LLM", value=provider.summary()[:1000] if provider else "off", inline=False)
        feed = getattr(interaction.client, "feed", None)
        e.add_field(name="Loot feed", value=(feed.status() if feed else "disabled (OIBOT_FEED_TOKEN unset)")[:500], inline=False)
        if ops.recent:
            e.add_field(name="Recent ops", value="\n".join(f"`{t}` {LEVEL[l]} {x}"[:120] for t, l, x in ops.recent[-8:])[:1000], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ---------------- /gm test: seed puppet members and rehearse the whole cycle in Discord within the hour
    test = app_commands.Group(name="test", description="Officers: test bench — seed fake members, open a compressed run, answer for them", parent=gm)

    @test.command(name="seed", description="Officer: add test members to the bench (5, 10, 20 or all of them)")
    async def test_seed(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["test_seed"](interaction, reg)

    @test.command(name="run", description="Officer: open a short test run now (pick raid and tempo)")
    async def test_run(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["test_run"](interaction, reg)

    @test.command(name="answer", description="Officer: test members answer a sheet (a random mix, or one member)")
    async def test_answer(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["test_answer"](interaction, reg)

    @test.command(name="clear", description="Officer: end the test — cancel test runs, remove every test member and their placements")
    async def test_clear(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        await interaction.response.defer(ephemeral=True)
        line = await interaction.client.test_bench_clear(reg, interaction.user.display_name)
        await interaction.followup.send(f"🧪 {line}", ephemeral=True)
        await ops.emit(reg.config, "warn", f"test bench: {line} (by {interaction.user.display_name})")

    tree.add_command(gm)
    # exported for other command modules
    register_commands.member_char_autocomplete = member_char_autocomplete  # type: ignore[attr-defined]
    register_commands.roster_autocomplete = roster_autocomplete  # type: ignore[attr-defined]


LEVEL = ops_mod.LEVEL_ICON
