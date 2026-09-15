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

CLASS_CHOICES = [app_commands.Choice(name=c, value=c) for c in ("Warrior", "Paladin", "Hunter", "Rogue", "Priest", "Shaman", "Mage", "Warlock", "Druid")]
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
    return any(r.name in reg.config.officer_roles for r in m.roles)


def is_owner(interaction: discord.Interaction, reg: Registry) -> bool:
    return reg.config.owner_discord_id is not None and interaction.user.id == reg.config.owner_discord_id


ROLES = ("tank", "healer", "melee", "ranged")
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


class PlanButton(discord.ui.DynamicItem[discord.ui.Button], template=r"plan:(?P<cls>[A-Za-z]+)"):
    """Poll button: pick a class → ephemeral spec select → role select. Survives restarts."""

    def __init__(self, cls: str, ico=None):
        emoji = None
        if ico:
            raw = ico("class", cls)
            try:
                emoji = discord.PartialEmoji.from_str(raw) if raw else None
            except Exception:  # noqa: BLE001
                emoji = None
        super().__init__(discord.ui.Button(label=cls, style=discord.ButtonStyle.secondary, custom_id=f"plan:{cls}", emoji=emoji))
        self.cls = cls

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["cls"])

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        reg = bot.registries.for_interaction(interaction)
        if not reg:
            await interaction.response.send_message("Not configured here.", ephemeral=True)
            return
        specs = list(reg.profile.classes.get(self.cls, {}))
        view = discord.ui.View(timeout=300)
        spec_sel = discord.ui.Select(placeholder=f"{self.cls}: which spec?", options=[discord.SelectOption(label=s, value=s, description=reg.profile.spec(self.cls, s).role) for s in specs])
        role_sel = discord.ui.Select(placeholder="Roles you're happy to play (first = primary)", min_values=1, max_values=4, options=[discord.SelectOption(label=r, value=r) for r in ROLES])
        state: dict = {}

        async def on_spec(i: discord.Interaction):
            state["spec"] = spec_sel.values[0]
            await i.response.defer()

        async def on_roles(i: discord.Interaction):
            spec = state.get("spec") or specs[0]
            roles = list(role_sel.values)
            primary = reg.profile.spec(self.cls, spec).role if reg.profile.spec(self.cls, spec).role in roles else roles[0]
            try:
                m, c = reg.set_plan(i.user.id, i.user.display_name, self.cls, spec, None, "main")
                reg.set_roles(i.user.id, i.user.display_name, primary, [r for r in roles if r != primary])
            except RegistryError as e:
                await i.response.send_message(f"❌ {e}", ephemeral=True)
                return
            await i.response.edit_message(content=f"✅ Planned main **{self.cls} {spec}**, roles: {primary}" + (f" (+{', '.join(r for r in roles if r != primary)})" if len(roles) > 1 else "") + ". Change any time with `/plan …`.", view=None)
            await bot.ops.emit(reg.config, "info", f"{m.display_name} plans to main {self.cls} {spec} · roles {roles}")

        spec_sel.callback, role_sel.callback = on_spec, on_roles
        view.add_item(spec_sel)
        view.add_item(role_sel)
        await interaction.response.send_message(f"**{self.cls}** — pick the spec, then the roles you'd play (the first one you pick is your primary):", view=view, ephemeral=True)


class NameModal(discord.ui.Modal):
    """Last step of the registration wizard: optional character name (blank = planned, name it at launch)."""

    name = discord.ui.TextInput(label="Character name (blank = not created yet)", required=False, max_length=12)  # labels max 45 chars

    def __init__(self, reg: Registry, cls: str, spec: str, offspec: str | None, roles: list[str], slot: str):
        super().__init__(title=f"{cls} {spec} — almost done"[:45])
        self.reg, self.cls, self.spec, self.offspec, self.roles, self.slot = reg, cls, spec, offspec, roles, slot

    async def on_submit(self, interaction: discord.Interaction):
        reg, bot = self.reg, interaction.client
        name = str(self.name.value).strip()
        try:
            if name:
                m, c = reg.add_character(interaction.user.id, interaction.user.display_name, name, self.cls, self.spec, self.offspec, self.slot == "main")
            else:
                m, c = reg.set_plan(interaction.user.id, interaction.user.display_name, self.cls, self.spec, self.offspec, self.slot)
            if self.roles:
                primary = self.roles[0]
                reg.set_roles(interaction.user.id, interaction.user.display_name, primary, self.roles[1:])
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        what = f"{'Registered' if name else 'Planned'} {'main' if self.slot == 'main' else 'alt'}: **{c.label}** · {c.spec}{'/' + c.offspec if c.offspec else ''}"
        roles = f" · roles: {self.roles[0]}" + (f" (+{', '.join(self.roles[1:])})" if len(self.roles) > 1 else "") if self.roles else ""
        tail = " An officer will confirm the character." if name else " Add the name at launch with `/me char name`."
        await interaction.response.send_message(f"✅ {what}{roles}.{tail} `/me view` shows everything.", ephemeral=True)
        await bot.ops.emit(reg.config, "info", f"**{m.display_name}** {'registered' if name else 'planned'} {c.label} ({c.cls} {c.spec}{', main' if c.is_main else ', alt'})" + (f" · roles {self.roles}" if self.roles else "") + ("" if not name else " — pending confirmation"))


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
            rp = m.role_prefs
            lines = [char_line(bot.ico, c) for c in m.active()]
            lines.append(("Roles: " + rp.get("primary", "?") + (f" (+{', '.join(rp.get('flex', []))})" if rp.get("flex") else "")) if rp else "Roles: unset")
            rosters = sorted({k for c in m.active() for k in c.rosters})
            if rosters:
                lines.append("Rosters: " + ", ".join(rosters))
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return
        slot = "main" if self.action == "register" else "alt"
        view = discord.ui.View(timeout=600)
        cls_sel = discord.ui.Select(placeholder="Class", options=[discord.SelectOption(label=c, value=c, emoji=_emoji(bot.ico("class", c))) for c in reg.profile.classes])
        spec_sel = discord.ui.Select(placeholder="Main spec (pick a class first)", options=[discord.SelectOption(label="—", value="-")], disabled=True)
        off_sel = discord.ui.Select(placeholder="Offspec (optional)", options=[discord.SelectOption(label="none", value="-")], disabled=True)
        role_sel = discord.ui.Select(placeholder="Roles you'd play (first = primary)", min_values=1, max_values=4, options=[discord.SelectOption(label=r, value=r, emoji=_emoji(bot.ico("role", r))) for r in ROLES])
        go = discord.ui.Button(label="Continue → name", style=discord.ButtonStyle.primary, disabled=True)
        state: dict = {"roles": []}

        def specs_for(c):
            return [discord.SelectOption(label=s, value=s, description=reg.profile.spec(c, s).role) for s in reg.profile.classes[c]]

        async def on_cls(i: discord.Interaction):
            state["cls"] = cls_sel.values[0]
            state.pop("spec", None)
            spec_sel.options = specs_for(state["cls"])
            spec_sel.placeholder = f"{state['cls']}: main spec"
            spec_sel.disabled = False
            off_sel.options = [discord.SelectOption(label="none", value="-")] + specs_for(state["cls"])
            off_sel.disabled = False
            go.disabled = True
            await i.response.edit_message(view=view)

        async def on_spec(i: discord.Interaction):
            state["spec"] = spec_sel.values[0]
            go.disabled = False
            await i.response.edit_message(view=view)

        async def on_off(i: discord.Interaction):
            state["offspec"] = None if off_sel.values[0] == "-" else off_sel.values[0]
            await i.response.defer()

        async def on_roles(i: discord.Interaction):
            state["roles"] = list(role_sel.values)
            await i.response.defer()

        async def on_go(i: discord.Interaction):
            await i.response.send_modal(NameModal(reg, state["cls"], state["spec"], state.get("offspec"), state["roles"], slot))

        cls_sel.callback, spec_sel.callback, off_sel.callback, role_sel.callback, go.callback = on_cls, on_spec, on_off, on_roles, on_go
        for item in (cls_sel, spec_sel, off_sel, role_sel, go):
            view.add_item(item)
        await interaction.response.send_message(f"**{'Register or plan your main' if slot == 'main' else 'Add an alt'}** — class, spec, optional offspec, the roles you'd play, then a name (or leave it blank until launch).", view=view, ephemeral=True)


def _emoji(raw: str):
    try:
        return discord.PartialEmoji.from_str(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def registration_card(reg: Registry) -> discord.Embed:
    e = discord.Embed(title=f"{reg.config.name} · character registration", colour=0x2B7A78,
                      description="**Register / plan my main** — class, spec, roles, and your character's name if it exists (leave it blank before launch).\n"
                                  "**Add an alt** — same flow for an alt.\n**My status** — what the bot has for you.\n\n"
                                  "Keyboard route: `/me plan …`, `/me char …`, `/me view`. Availability and absences: `/me availability`, `/me absent add`.")
    e.set_footer(text="Your Discord account is your identity; character names can be added later with /me char name.")
    return e


def registration_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for a in ("register", "alt", "status"):
        v.add_item(RegisterButton(a))
    return v


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

    async def spec_autocomplete(interaction: discord.Interaction, current: str):
        """Specs filtered by the class picked in the same command (or by the named character's class)."""
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        cls = _ns(interaction, "class", "class_", "cls")
        if not cls:
            name = _ns(interaction, "name")
            hit = reg.find(name) if name else None
            if not hit and name and interaction.user.id in reg.members:
                hit = next(((reg.members[interaction.user.id], c) for c in reg.members[interaction.user.id].active() if c.label.lower() == name.lower()), None)
            cls = hit[1].cls if hit else None
        specs = list(reg.profile.classes.get(cls, {})) if cls else sorted({s for c in reg.profile.classes.values() for s in c})
        return [app_commands.Choice(name=s, value=s) for s in specs if current.lower() in s.lower()][:25]

    async def own_char_autocomplete(interaction: discord.Interaction, current: str):
        """The caller's characters, planned ones by label."""
        reg = guilds.for_interaction(interaction)
        if reg is None or interaction.user.id not in reg.members:
            return []
        out = []
        for c in reg.members[interaction.user.id].active():
            key = c.name or c.label
            if current.lower() in key.lower():
                out.append(app_commands.Choice(name=f"{c.label} · {c.cls} {c.spec}"[:100], value=key))
        return out[:25]

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
    @tree.command(name="register", description="Register your character (first one becomes your main)")
    @app_commands.describe(name="Character name", class_="Class", spec="Main spec", offspec="Offspec (optional)", main="Make this your main")
    @app_commands.rename(class_="class")
    @app_commands.choices(class_=CLASS_CHOICES)
    @app_commands.autocomplete(spec=spec_autocomplete, offspec=spec_autocomplete)
    async def register(interaction: discord.Interaction, name: str, class_: app_commands.Choice[str], spec: str, offspec: str | None = None, main: bool = False):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m, c = reg.add_character(interaction.user.id, interaction.user.display_name, name, class_.value, spec, offspec, main)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Registered {char_line(ico, c)}. An officer will confirm it. Your characters: " + ", ".join(x.label + (" (main)" if x.is_main else "") for x in m.active()), ephemeral=True)
        await ops.emit(reg.config, "info", f"**{m.display_name}** registered {c.label} ({c.cls} {c.spec}{', main' if c.is_main else ''}) — pending confirmation")

    # ---------------- /plan (pre-launch: what are you going to play?)
    me = app_commands.Group(name="me", description="Your plan, characters, availability and absences")
    plan = app_commands.Group(name="plan", description="Pre-launch: what you're planning to play (no character name needed)", parent=me)

    @plan.command(name="main", description="What you plan to main")
    @app_commands.rename(class_="class")
    @app_commands.choices(class_=CLASS_CHOICES)
    @app_commands.autocomplete(spec=spec_autocomplete, offspec=spec_autocomplete)
    async def plan_main(interaction: discord.Interaction, class_: app_commands.Choice[str], spec: str, offspec: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m, c = reg.set_plan(interaction.user.id, interaction.user.display_name, class_.value, spec, offspec, "main")
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Planned main: {char_line(ico, c)}. Set your role preferences with `/me plan roles` (or the poll buttons).", ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} plans to main {c.cls} {c.spec}")

    @plan.command(name="alt", description="What you plan to play as an alt")
    @app_commands.rename(class_="class")
    @app_commands.choices(class_=CLASS_CHOICES)
    @app_commands.autocomplete(spec=spec_autocomplete, offspec=spec_autocomplete)
    async def plan_alt(interaction: discord.Interaction, class_: app_commands.Choice[str], spec: str, offspec: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m, c = reg.set_plan(interaction.user.id, interaction.user.display_name, class_.value, spec, offspec, "alt")
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Planned alt: {char_line(ico, c)}", ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} plans an alt {c.cls} {c.spec}")

    @plan.command(name="roles", description="Your primary role and what else you'd be happy to play")
    @app_commands.choices(primary=ROLE_CHOICES, flex1=ROLE_CHOICES, flex2=ROLE_CHOICES, flex3=ROLE_CHOICES)
    async def plan_roles(interaction: discord.Interaction, primary: app_commands.Choice[str], flex1: app_commands.Choice[str] | None = None, flex2: app_commands.Choice[str] | None = None, flex3: app_commands.Choice[str] | None = None):
        reg = await need(interaction)
        if not reg:
            return
        flex = [f.value for f in (flex1, flex2, flex3) if f]
        m = reg.set_roles(interaction.user.id, interaction.user.display_name, primary.value, flex)
        await interaction.response.send_message(f"✅ Roles: **{primary.value}**" + (f", also {', '.join(m.role_prefs['flex'])}" if m.role_prefs["flex"] else ""), ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} roles: {primary.value}" + (f" (+{', '.join(m.role_prefs['flex'])})" if m.role_prefs["flex"] else ""))


    # ---------------- /char
    char = app_commands.Group(name="char", description="Manage your characters", parent=me)

    @char.command(name="name", description="At launch: give your planned main (or alt) its real character name")
    @app_commands.choices(slot=[app_commands.Choice(name="main", value="main"), app_commands.Choice(name="alt", value="alt")])
    async def char_name(interaction: discord.Interaction, name: str, slot: app_commands.Choice[str] | None = None):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m, c = reg.name_character(interaction.user.id, name, slot.value if slot else "main")
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {char_line(ico, c)} — an officer will confirm it.", ephemeral=True)
        await ops.emit(reg.config, "info", f"**{m.display_name}** named their planned {slot.value if slot else 'main'}: {c.name} ({c.cls} {c.spec}) — pending confirmation")

    @char.command(name="add", description="Register another character (alt)")
    @app_commands.rename(class_="class")
    @app_commands.choices(class_=CLASS_CHOICES)
    @app_commands.autocomplete(spec=spec_autocomplete, offspec=spec_autocomplete)
    async def char_add(interaction: discord.Interaction, name: str, class_: app_commands.Choice[str], spec: str, offspec: str | None = None):
        await register.callback(interaction, name, class_, spec, offspec, False)

    @char.command(name="main", description="Make one of your characters your main (rank follows you)")
    @app_commands.autocomplete(name=own_char_autocomplete)
    async def char_main(interaction: discord.Interaction, name: str):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m, c, old = reg.set_main(interaction.user.id, name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Main is now {char_line(ico, c)}" + (f" (was {old.name}, now alt)" if old else ""), ephemeral=True)
        await ops.emit(reg.config, "info", f"**{m.display_name}** changed main {old.label if old else '-'} → {c.label}")

    @char.command(name="spec", description="Change a character's spec/offspec")
    @app_commands.autocomplete(name=own_char_autocomplete, spec=spec_autocomplete, offspec=spec_autocomplete)
    async def char_spec(interaction: discord.Interaction, name: str, spec: str, offspec: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        try:
            c = reg.set_spec(interaction.user.id, name, spec, offspec)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {char_line(ico, c)}", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name}: {c.label} now {c.spec}{'/' + c.offspec if c.offspec else ''}")

    @char.command(name="retire", description="Retire a character (history is kept)")
    @app_commands.autocomplete(name=own_char_autocomplete)
    async def char_retire(interaction: discord.Interaction, name: str):
        reg = await need(interaction)
        if not reg:
            return
        try:
            c = reg.retire(interaction.user.id, name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Retired {c.label}.", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} retired {c.label}")

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


    # ---------------- /availability, /me absent, /me
    async def roster_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        return [app_commands.Choice(name=t, value=t) for t in (reg.config.roster_keys() if reg else []) if current.lower() in t.lower()][:25]

    team_autocomplete = roster_autocomplete

    @me.command(name="availability", description="Your standing default for a roster's raids: in, out, or sub-only")
    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.choices(value=[app_commands.Choice(name=v, value=v) for v in ("in", "out", "sub")])
    async def availability(interaction: discord.Interaction, value: app_commands.Choice[str], roster: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        team = roster or reg.config.roster_keys()[0]
        try:
            m = reg.set_availability(interaction.user.id, team, value.value)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {team}: **{value.value}** (sheets for this team will start with you {value.value}).", ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} availability {team}={value.value}")

    absent = app_commands.Group(name="absent", description="Future absences (reason is officer-only)", parent=me)

    @absent.command(name="add", description="Register an absence: one day or a range")
    @app_commands.describe(start="YYYY-MM-DD", end="YYYY-MM-DD (optional)", reason="Optional; only officers see it")
    async def absent_add(interaction: discord.Interaction, start: str, end: str | None = None, reason: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m, a = reg.add_absence(interaction.user.id, start, end, reason, interaction.user.display_name, display_name=interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        span = a.start + (f" → {a.end}" if a.end != a.start else "")
        await interaction.response.send_message(f"✅ Absent {span}. You'll be left off sheets for those dates.", ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} absent {span}" + (f" — {a.reason}" if a.reason else ""))

    @absent.command(name="list", description="Your upcoming absences")
    async def absent_list(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        today = discord.utils.utcnow().date().isoformat()
        ups = m.upcoming_absences(today) if m else []
        await interaction.response.send_message("\n".join(f"• {a.start}" + (f" → {a.end}" if a.end != a.start else "") + (f" — {a.reason}" if a.reason else "") for a in ups) or "No upcoming absences.", ephemeral=True)

    @absent.command(name="clear", description="Remove an absence by its start date")
    async def absent_clear(interaction: discord.Interaction, start: str):
        reg = await need(interaction)
        if not reg:
            return
        try:
            m = reg.clear_absence(interaction.user.id, start)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Cleared absence starting {start}.", ephemeral=True)
        await ops.emit(reg.config, "info", f"{m.display_name} cleared absence {start}")


    # ---------------- /apply (recruitment intake)
    def application_embed(reg: Registry, a) -> discord.Embed:
        e = discord.Embed(title=f"Application · {a.name}", colour=0x2B7A78, description=f"{ico('class', a.cls)} **{a.cls} {a.spec}**{' / ' + a.offspec if a.offspec else ''} · from **{a.display_name}** (<@{a.discord_id}>)")
        if a.logs_url:
            e.add_field(name="Logs", value=a.logs_url[:200], inline=False)
        if a.availability:
            e.add_field(name="Availability", value=a.availability[:300], inline=False)
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
        await ops.emit(reg.config, "info", f"{interaction.user.display_name}: {verdict}" + (f" — {note}" if note else ""))
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
                await msg.edit(embed=application_embed(reg, a), view=None)
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

    @tree.command(name="apply", description="Apply to join the guild")
    @app_commands.describe(name="Character name", class_="Class", spec="Main spec", offspec="Offspec (optional)", logs="Warcraft Logs / armory link (optional)", availability="When you can raid (optional)", about="Anything else (optional)")
    @app_commands.rename(class_="class")
    @app_commands.choices(class_=CLASS_CHOICES)
    @app_commands.autocomplete(spec=spec_autocomplete, offspec=spec_autocomplete)
    async def apply(interaction: discord.Interaction, name: str, class_: app_commands.Choice[str], spec: str, offspec: str | None = None, logs: str | None = None, availability: str | None = None, about: str | None = None):
        reg = await need(interaction)
        if not reg:
            return
        try:
            a = reg.apply(interaction.user.id, interaction.user.display_name, name, class_.value, spec, offspec, logs, availability, about)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Application received for **{a.name}** ({a.cls} {a.spec}). An officer will review it; you'll get a DM either way.", ephemeral=True)
        ch_id = reg.config.applications_channel_id or reg.config.ops_channel_id
        ch = interaction.client.get_channel(ch_id) if ch_id else None
        if ch:
            msg = await ch.send(embed=application_embed(reg, a), view=ApplicationView(reg, a.discord_id))
            a.message_id = msg.id
            reg.save_applicant(a, f"application card posted for {a.name}")
        else:
            await ops.emit(reg.config, "warn", f"application from {a.display_name} ({a.name}, {a.cls} {a.spec}) — no applications/ops channel configured; use /roster applicants")

    @me.command(name="view", description="Your plan, characters, availability and upcoming absences")
    async def me(interaction: discord.Interaction):
        reg = await need(interaction)
        if not reg:
            return
        m = reg.members.get(interaction.user.id)
        if not m:
            await interaction.response.send_message("Nothing registered yet — /register to start.", ephemeral=True)
            return
        today = discord.utils.utcnow().date().isoformat()
        e = discord.Embed(title=f"{m.display_name} · {reg.config.name}", colour=0x2B7A78)
        e.add_field(name="Characters", value="\n".join(char_line(ico, c) for c in m.active()) or "none", inline=False)
        rosters = sorted({k for c in m.active() for k in c.rosters})
        if rosters:
            e.add_field(name="Rosters", value=", ".join(f"{k} ({next(c.label for c in m.active() if k in c.rosters)})" for k in rosters), inline=True)
        rp = m.role_prefs
        e.add_field(name="Roles", value=(f"{rp.get('primary')}" + (f" (+{', '.join(rp.get('flex', []))})" if rp.get("flex") else "")) if rp else "unset — /me plan roles", inline=True)
        e.add_field(name="Availability", value=", ".join(f"{t}: {m.availability.get(t, 'unset')}" for t in reg.config.team_keys()), inline=True)
        ups = m.upcoming_absences(today)
        e.add_field(name="Upcoming absences", value="\n".join(f"{a.start}" + (f" → {a.end}" if a.end != a.start else "") for a in ups) or "none", inline=True)
        e.set_footer(text="Attendance and loot history appear here once the raid ledger is live.")
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
        e = discord.Embed(title=f"{reg.config.name} · {len(chars)} characters · {len(reg.members)} members", colour=0x2B7A78)
        by_cls: dict[str, list[str]] = {}
        for m, c in chars:
            by_cls.setdefault(c.cls, []).append(f"{'★ ' if c.is_main else ''}{c.label} · {c.spec} · {c.rank}{' ⏳' if not c.confirmed_by else ''} · {m.display_name}")
        for cls, lines in by_cls.items():
            e.add_field(name=f"{ico('class', cls)} {cls} ({len(lines)})", value="\n".join(lines)[:1000], inline=True)
        pend = reg.pending()
        e.set_footer(text=f"★ main · ⏳ unconfirmed ({len(pend)}) · /roster confirm <name>")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @roster.command(name="confirm", description="Confirm a registered character")
    @app_commands.autocomplete(name=any_char_autocomplete)
    async def roster_confirm(interaction: discord.Interaction, name: str):
        reg = await officer(interaction)
        if not reg:
            return
        try:
            m, c = reg.confirm(name, interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Confirmed {char_line(ico, c)} ({m.display_name})", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} confirmed {c.label} ({m.display_name})")
        try:
            user = await interaction.client.fetch_user(m.discord_id)
            await user.send(f"✅ {c.label} was confirmed by {interaction.user.display_name}. Welcome aboard.")
        except Exception:  # noqa: BLE001
            pass

    @roster.command(name="rank", description="Set a character's rank")
    @app_commands.autocomplete(name=any_char_autocomplete)
    @app_commands.choices(rank=RANK_CHOICES)
    async def roster_rank(interaction: discord.Interaction, name: str, rank: app_commands.Choice[str]):
        reg = await officer(interaction)
        if not reg:
            return
        try:
            m, c = reg.set_rank(name, rank.value, interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {char_line(ico, c)}", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} set {c.label} → {rank.value}")

    @roster.command(name="set-main", description="Set a member's main on their behalf")
    @app_commands.autocomplete(name=member_char_autocomplete)
    async def roster_set_main(interaction: discord.Interaction, member: discord.User, name: str):
        reg = await officer(interaction)
        if not reg:
            return
        try:
            m, c, old = reg.officer_set_main(member.id, name, interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {m.display_name}'s main is now {c.label}", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} set {m.display_name}'s main → {c.label}")

    @roster.command(name="overview", description="Who is planning to play what: classes, roles, flexibility, buff coverage")
    @app_commands.describe(size="raid size to check against (default: first team's size or 20)")
    async def roster_plan(interaction: discord.Interaction, size: int | None = None):
        reg = await need(interaction)
        if not reg:
            return
        s = reg.plan_summary()
        if not s["mains"]:
            await interaction.response.send_message("Nobody has planned a main yet — post the poll with `/roster poll` or use `/me plan main`.", ephemeral=True)
            return
        from .roster.solver import scaled_role_bounds

        team = reg.config.rosters[0] if reg.config.rosters else {}
        n = size or int(team.get("size") or 20)
        bounds = scaled_role_bounds(reg.profile.comp_rules, n)
        e = discord.Embed(title=f"{reg.config.name} · plan · {s['mains']} mains", colour=0x2B7A78)
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
        from .discord_pool import pool_card

        card, file = await asyncio.to_thread(pool_card, reg, {**team, "size": n} if team else {"key": "main", "name": "main", "size": n}, ico)
        e.set_image(url=card.image.url)
        await interaction.response.send_message(embed=e, file=file, ephemeral=not is_officer(interaction, reg))

    @roster.command(name="add", description="Add a member's character to a roster (defaults to their main)")
    @app_commands.autocomplete(roster=roster_autocomplete, character=member_char_autocomplete)
    async def roster_add(interaction: discord.Interaction, member: discord.User, character: str | None = None, roster: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        key = roster or reg.config.roster_keys()[0]
        try:
            m, c = reg.roster_add(member.id, key, interaction.user.display_name, character, display_name=member.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {char_line(ico, c)} ({m.display_name}) → roster **{key}** ({len(reg.roster_members(key))} characters)", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} added {m.display_name} ({c.label}) to roster {key}")

    @roster.command(name="remove", description="Remove a member from a roster (they can still sign as sub)")
    @app_commands.autocomplete(roster=roster_autocomplete)
    async def roster_remove(interaction: discord.Interaction, member: discord.User, roster: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        key = roster or reg.config.roster_keys()[0]
        try:
            m = reg.roster_remove(member.id, key, interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {m.display_name} removed from **{key}**", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} removed {m.display_name} from roster {key}")

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
            role = m.role_prefs.get("primary") or reg.profile.spec(c.cls, c.spec).role
            by_role.setdefault(role, []).append(f"{ico('class', c.cls)} **{c.label}** · {c.spec}{'/' + c.offspec if c.offspec else ''} · {m.display_name}")
        cfg = reg.config.roster(key) or {}
        e = discord.Embed(title=f"Roster {key} · {len(members)}/{cfg.get('size', '?')} · {cfg.get('schedule') or 'no schedule'}", colour=0x2B7A78)
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

        today = discord.utils.utcnow().date()
        rows = reg.absences_between(today.isoformat(), (today + timedelta(days=days)).isoformat())
        lines = [f"• **{m.display_name}** ({(m.main.name if m.main else '-')}) {a.start}" + (f" → {a.end}" if a.end != a.start else "") + (f" — {a.reason}" if a.reason else "") + (f" _(by {a.by})_" if a.by != m.display_name else "") for m, a in rows]
        await interaction.response.send_message("\n".join(lines)[:1900] or f"No absences in the next {days} days.", ephemeral=True)

    @roster.command(name="availability", description="Standing availability counts per roster")
    async def roster_availability(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        s = reg.availability_summary()
        await interaction.response.send_message("\n".join(f"**{t}**: in {v['in']} · sub {v['sub']} · out {v['out']} · unset {v['unset']}" for t, v in s.items()), ephemeral=True)

    @roster.command(name="absent", description="Record an absence on a member's behalf")
    async def roster_absent(interaction: discord.Interaction, member: discord.User, start: str, end: str | None = None, reason: str | None = None):
        reg = await officer(interaction)
        if not reg:
            return
        try:
            m, a = reg.add_absence(member.id, start, end, reason, interaction.user.display_name, display_name=member.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {m.display_name} absent {a.start}" + (f" → {a.end}" if a.end != a.start else ""), ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} recorded {m.display_name} absent {a.start}" + (f" → {a.end}" if a.end != a.start else ""))

    @roster.command(name="applicants", description="Open applications")
    async def roster_applicants(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        opens = reg.open_applicants()
        if not opens:
            await interaction.response.send_message("No open applications.", ephemeral=True)
            return
        await interaction.response.send_message(embeds=[application_embed(reg, a) for a in opens[:10]], ephemeral=True)

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
        roles = set(reg.config.officer_roles)
        (roles.discard if remove else roles.add)(role.name)
        reg.config.officer_roles = sorted(roles)
        reg.save_config(f"officer roles: {reg.config.officer_roles}")
        await interaction.response.send_message(f"✅ Officer roles: {', '.join(reg.config.officer_roles) or '(none; Manage Server only)'}", ephemeral=True)
        await ops.emit(reg.config, "warn", f"officer roles now {reg.config.officer_roles} (by {interaction.user.display_name})")

    @config.command(name="roster", description="Owner: add/update a roster (size, schedule like 'Tue 19:30', instance, cutoffs)")
    @app_commands.describe(key="short id, e.g. main", size="10 / 20 / 25 / 40", schedule="'Tue 19:30' in the guild's timezone", instance="raid from the game profile", soft_cutoff="hours before raid: health check + nudges", hard_cutoff="hours before raid: lock + propose", open_days="days before the raid to open the sheet")
    @app_commands.autocomplete(instance=instance_autocomplete)
    async def cfg_team(interaction: discord.Interaction, key: str, size: int = 20, schedule: str = "", instance: str | None = None, soft_cutoff: int = 48, hard_cutoff: int = 24, open_days: int = 6, open_dm: bool = False, remove: bool = False):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        key = key.strip().lower()
        if schedule:
            from .raidcycle import parse_schedule

            try:
                parse_schedule(schedule)
            except ValueError as e:
                await interaction.response.send_message(f"❌ {e}", ephemeral=True)
                return
        if instance and instance not in reg.profile.raids:
            await interaction.response.send_message(f"❌ Unknown instance. Options: {', '.join(reg.profile.raids)}", ephemeral=True)
            return
        teams = [t for t in reg.config.raid_teams if t["key"] != key]
        if not remove:
            teams.append({"key": key, "name": key, "size": size, "schedule": schedule, "instance": instance, "cutoff_soft_hours": soft_cutoff, "cutoff_hard_hours": hard_cutoff, "open_days_before": open_days, "reminders": "dm", "open_dm": open_dm})
        reg.config.raid_teams = teams
        reg.save_config(f"rosters: {[t['key'] for t in teams]}")
        await interaction.response.send_message("✅ Rosters: " + (", ".join(f"{t['key']} ({t['size']}, {t['schedule'] or 'no schedule'}, lock {t.get('cutoff_hard_hours', 24)}h)" for t in teams) or "none (default 'main')"), ephemeral=True)
        await ops.emit(reg.config, "info", f"rosters now {[t['key'] for t in teams]} (by {interaction.user.display_name})")

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

    @config.command(name="timezone", description="Owner: IANA timezone for schedules, e.g. America/Chicago")
    async def cfg_tz(interaction: discord.Interaction, timezone: str):
        reg = await need(interaction)
        if not reg:
            return
        if not is_owner(interaction, reg):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(timezone)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message("❌ Unknown timezone (use IANA names like America/Chicago).", ephemeral=True)
            return
        reg.config.timezone = timezone
        reg.save_config(f"timezone → {timezone}")
        await interaction.response.send_message(f"✅ Timezone {timezone}", ephemeral=True)

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

    @roster.command(name="registration-card", description="Officer: post the persistent registration card (pin it in a public channel)")
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

    @roster.command(name="poll", description="Officer: post the 'what are you planning to play?' poll with class buttons")
    async def gm_plan_poll(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        e = discord.Embed(title="What are you planning to play?", colour=0x2B7A78, description="Pick your **main's class** below, then the spec and your role preferences. No character name needed — that comes at launch (`/me char name`). Alts and changes: `/plan alt`, `/me plan main`, `/me plan roles`. See where the guild stands with `/roster overview`.")
        view = discord.ui.View(timeout=None)
        for cls in ("Warrior", "Paladin", "Hunter", "Rogue", "Priest", "Shaman", "Mage", "Warlock", "Druid"):
            view.add_item(PlanButton(cls, ico))
        await interaction.response.send_message(embed=e, view=view)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} posted the plan poll")

    @gm.command(name="status", description="Bot health: data repo, registry, spend, recent ops")
    async def gm_status(interaction: discord.Interaction):
        reg = await officer(interaction)
        if not reg:
            return
        st = guilds.store
        up = int(time.time() - STARTED)
        provider = getattr(interaction.client, "ctx", None) and interaction.client.ctx.provider
        chars = reg.all_characters()
        e = discord.Embed(title=f"oibot_GM status · {reg.config.name}", colour=0x2B7A78)
        e.add_field(name="Bot", value=f"up {up // 3600}h{(up % 3600) // 60}m · profile {reg.profile.name}", inline=True)
        e.add_field(name="Data repo", value=f"{st.root.name} @ {st.head()} · push {'on' if st.push_enabled else 'off'}", inline=True)
        e.add_field(name="Registry", value=f"{len(reg.members)} members · {len(chars)} characters · {len(reg.pending())} unconfirmed", inline=True)
        e.add_field(name="Config", value=f"owner {'<@%d>' % reg.config.owner_discord_id if reg.config.owner_discord_id else '—'} · ops {'<#%d>' % reg.config.ops_channel_id if reg.config.ops_channel_id else '—'} · officer roles {', '.join(reg.config.officer_roles) or '—'}", inline=False)
        e.add_field(name="LLM", value=provider.summary()[:1000] if provider else "off", inline=False)
        feed = getattr(interaction.client, "feed", None)
        e.add_field(name="Loot feed", value=(feed.status() if feed else "disabled (OIBOT_FEED_TOKEN unset)")[:500], inline=False)
        if ops.recent:
            e.add_field(name="Recent ops", value="\n".join(f"`{t}` {LEVEL[l]} {x}"[:120] for t, l, x in ops.recent[-8:])[:1000], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    tree.add_command(gm)
    # exported for other command modules
    register_commands.member_char_autocomplete = member_char_autocomplete  # type: ignore[attr-defined]
    register_commands.roster_autocomplete = roster_autocomplete  # type: ignore[attr-defined]


LEVEL = ops_mod.LEVEL_ICON
