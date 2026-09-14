"""Real (non-mock) commands: /register, /char, /roster, /gm.

Resolves the guild from the Discord server. Member commands reply ephemerally
and also work in DMs; officer commands need Manage Server or a configured
officer role; /gm config needs the owner."""
from __future__ import annotations

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


def char_line(ico, c) -> str:
    flags = []
    if c.is_main:
        flags.append("main")
    flags.append(c.rank)
    if not c.confirmed_by:
        flags.append("unconfirmed")
    return f"{ico('class', c.cls)} **{c.name}** · {c.spec}{'/' + c.offspec if c.offspec else ''} · _{', '.join(flags)}_"


def register_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: ops_mod.Ops, ico) -> None:
    async def need(interaction: discord.Interaction) -> Registry | None:
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
        return reg

    async def spec_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        cls = getattr(interaction.namespace, "class_", None) or getattr(interaction.namespace, "cls", None)
        if reg is None:
            return []
        if not cls:
            hit = None
            name = getattr(interaction.namespace, "name", None)
            if name:
                hit = reg.find(name)
            cls = hit[1].cls if hit else None
        specs = list(reg.profile.classes.get(cls, {})) if cls else sorted({s for c in reg.profile.classes.values() for s in c})
        return [app_commands.Choice(name=s, value=s) for s in specs if current.lower() in s.lower()][:25]

    async def own_char_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        if reg is None or interaction.user.id not in reg.members:
            return []
        return [app_commands.Choice(name=c.name, value=c.name) for c in reg.members[interaction.user.id].active() if current.lower() in c.name.lower()][:25]

    async def any_char_autocomplete(interaction: discord.Interaction, current: str):
        reg = guilds.for_interaction(interaction)
        if reg is None:
            return []
        return [app_commands.Choice(name=f"{c.name} ({c.cls})", value=c.name) for _, c in reg.all_characters() if current.lower() in c.name.lower()][:25]

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
        await interaction.response.send_message(f"✅ Registered {char_line(ico, c)}. An officer will confirm it. Your characters: " + ", ".join(x.name + (" (main)" if x.is_main else "") for x in m.active()), ephemeral=True)
        await ops.emit(reg.config, "info", f"**{m.display_name}** registered {c.name} ({c.cls} {c.spec}{', main' if c.is_main else ''}) — pending confirmation")

    # ---------------- /char
    char = app_commands.Group(name="char", description="Manage your characters")

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
        await ops.emit(reg.config, "info", f"**{m.display_name}** changed main {old.name if old else '-'} → {c.name}")

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
        await ops.emit(reg.config, "info", f"{interaction.user.display_name}: {c.name} now {c.spec}{'/' + c.offspec if c.offspec else ''}")

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
        await interaction.response.send_message(f"✅ Retired {c.name}.", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} retired {c.name}")

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

    tree.add_command(char)

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
            by_cls.setdefault(c.cls, []).append(f"{'★ ' if c.is_main else ''}{c.name} · {c.spec} · {c.rank}{' ⏳' if not c.confirmed_by else ''} · {m.display_name}")
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
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} confirmed {c.name} ({m.display_name})")
        try:
            user = await interaction.client.fetch_user(m.discord_id)
            await user.send(f"✅ {c.name} was confirmed by {interaction.user.display_name}. Welcome aboard.")
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
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} set {c.name} → {rank.value}")

    @roster.command(name="set-main", description="Set a member's main on their behalf")
    async def roster_set_main(interaction: discord.Interaction, member: discord.User, name: str):
        reg = await officer(interaction)
        if not reg:
            return
        try:
            m, c, old = reg.officer_set_main(member.id, name, interaction.user.display_name)
        except RegistryError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {m.display_name}'s main is now {c.name}", ephemeral=True)
        await ops.emit(reg.config, "info", f"{interaction.user.display_name} set {m.display_name}'s main → {c.name}")

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
        if ops.recent:
            e.add_field(name="Recent ops", value="\n".join(f"`{t}` {LEVEL[l]} {x}"[:120] for t, l, x in ops.recent[-8:])[:1000], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    tree.add_command(gm)


LEVEL = ops_mod.LEVEL_ICON
