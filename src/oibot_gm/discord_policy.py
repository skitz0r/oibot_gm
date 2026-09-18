"""Discord surface for §5.15: /policy show|edit|reload, /loot rule, /comp rule,
/gm change <text> (+ @mention in the ops channel)."""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from . import configops, policy as policy_mod
from .discord_registry import Guilds, is_officer, is_owner
from .ops import Ops
from .registry import Registry, RegistryError

from .constants import TEAL  # noqa: E402


class PolicyContext:
    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, policy_mod.PolicyStore] = {}

    def store(self, reg: Registry) -> policy_mod.PolicyStore:
        return self.stores.setdefault(reg.key, policy_mod.PolicyStore(self.guilds.store, reg.key))


# ---------------------------------------------------------------- compile-and-confirm

class CompileConfirmView(discord.ui.View):
    def __init__(self, ps: policy_mod.PolicyStore, doc: str, compiled, previous: str, ops: Ops, reg: Registry):
        super().__init__(timeout=900)
        self.ps, self.doc, self.compiled, self.previous, self.ops, self.reg = ps, doc, compiled, previous, ops, reg

    @discord.ui.button(label="Confirm this reading", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.ps.confirm(self.doc, self.compiled, interaction.user.display_name)
        await interaction.response.edit_message(content=f"✅ {self.doc} policy is live with this reading.", view=None)
        await self.ops.emit(self.reg.config, "info", f"{interaction.user.display_name} confirmed the compiled {self.doc} policy ({len(getattr(self.compiled, 'rules', getattr(self.compiled, 'constraints', [])))} items)")
        self.stop()

    @discord.ui.button(label="Discard edit", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.ps.revert(self.doc, self.previous, interaction.user.display_name)
        await interaction.response.edit_message(content=f"Edit to {self.doc} discarded; previous text restored.", view=None)
        self.stop()


async def compile_and_confirm(interaction: discord.Interaction, reg: Registry, ps: policy_mod.PolicyStore, doc: str, previous: str, provider, ops: Ops, followup: bool = False):
    send = interaction.followup.send if followup or interaction.response.is_done() else interaction.response.send_message
    if doc == "persona" or provider is None:
        note = "Persona saved (no compile step)." if doc == "persona" else "Saved. No LLM available to compile; run `/gm policy reload` when it is."
        await send(note, ephemeral=True)
        return
    compiled = await asyncio.to_thread(ps.compile, doc, provider)
    body = policy_mod.render_compiled(doc, compiled)
    e = discord.Embed(title=f"My reading of the {doc} policy", colour=TEAL, description=body[:4000])
    e.set_footer(text="Confirm to make this the live compiled form; Discard restores the previous text. Questions above need an edit, not a guess.")
    await send(embed=e, view=CompileConfirmView(ps, doc, compiled, previous, ops, reg), ephemeral=True)


class PolicyEditModal(discord.ui.Modal):
    text = discord.ui.TextInput(label="Document", style=discord.TextStyle.paragraph, max_length=4000, required=True)

    def __init__(self, reg: Registry, ps: policy_mod.PolicyStore, doc: str, provider, ops: Ops):
        super().__init__(title=f"Edit {doc} policy"[:45])
        self.reg, self.ps, self.doc, self.provider, self.ops = reg, ps, doc, provider, ops
        self.text.default = ps.read(doc)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        previous = self.ps.read(self.doc)
        self.ps.write_draft(self.doc, str(self.text.value), interaction.user.display_name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await compile_and_confirm(interaction, self.reg, self.ps, self.doc, previous, self.provider, self.ops, followup=True)


# ---------------------------------------------------------------- plain-text config

class ConfigConfirmView(discord.ui.View):
    def __init__(self, reg: Registry, req: configops.ConfigRequest, ps: policy_mod.PolicyStore, ops: Ops, owner: bool):
        super().__init__(timeout=600)
        self.reg, self.req, self.ps, self.ops, self.owner = reg, req, ps, ops, owner

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        # the presser's rights decide, not the requester's: the diff can be a public message in the ops channel
        if not is_officer(interaction, self.reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        owner = is_owner(interaction, self.reg)
        done, refused = [], []
        for op in self.req.ops:
            try:
                done.append(await configops.apply_async(self.reg, op, interaction.user.display_name, owner, self.ps, bot=interaction.client, by_id=interaction.user.id))
            except (RegistryError, ValueError, Exception) as e:  # noqa: BLE001
                refused.append(f"{configops.describe(self.reg, op)} — {e}")
        text = ("✅ " + "; ".join(done) if done else "") + ("\n⛔ " + "\n⛔ ".join(refused) if refused else "")
        await interaction.response.edit_message(content=text[:1900] or "Nothing applied.", view=None, embed=None)
        await self.ops.emit(self.reg.config, "info" if not refused else "warn", f"{interaction.user.display_name} config change: {'; '.join(done) or '-'}" + (f" (refused: {len(refused)})" if refused else ""))
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not is_officer(interaction, self.reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Cancelled.", view=None, embed=None)
        self.stop()


async def handle_change(interaction_or_message, reg: Registry, ps: policy_mod.PolicyStore, provider, ops: Ops, text: str, owner: bool, officer: bool):
    """Shared by /gm change and @mention. `interaction_or_message` is an Interaction (deferred) or a Message."""
    is_msg = isinstance(interaction_or_message, discord.Message)

    async def send(content=None, **kw):
        if is_msg:
            return await interaction_or_message.reply(content, **{k: v for k, v in kw.items() if k != "ephemeral"})
        return await interaction_or_message.followup.send(content, **kw)

    if not officer:
        await send("Officers only.", ephemeral=True)
        return
    if provider is None:
        await send("Plain-text config needs the LLM; use `/gm config …` commands.", ephemeral=True)
        return
    author = interaction_or_message.author if is_msg else interaction_or_message.user
    req = await asyncio.to_thread(configops.parse, provider, reg, text, author.display_name)
    if req.kind == "ignore":
        return
    if req.kind == "question" or req.questions:
        await send((req.reply + ("\n" + "\n".join(f"• {q}" for q in req.questions) if req.questions else ""))[:1900], ephemeral=True)
        return
    if not req.ops:
        await send(req.reply[:1900] or "Nothing to change.", ephemeral=True)
        return
    e = discord.Embed(title="Proposed changes", colour=TEAL, description="\n".join(f"• {configops.describe(reg, op)}" + (" _(owner)_" if op.op in configops.OWNER_OPS and not owner else "") for op in req.ops)[:4000])
    acts = any(op.op in configops.RUN_OPS or op.op == "test" for op in req.ops)
    if any(op.op in configops.OWNER_OPS for op in req.ops) and not owner:
        e.set_footer(text="Items marked (owner) will be refused for you." + (" Run actions happen at once on Apply (DMs, cards, sheet)." if acts else ""))
    elif acts:
        e.set_footer(text="Run actions happen at once on Apply: DMs, cards and the sheet, exactly as the /raid command would.")
    await send(req.reply[:500] if req.reply else None, embed=e, view=ConfigConfirmView(reg, req, ps, ops, owner), ephemeral=True)


# ---------------------------------------------------------------- commands

def register_policy_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot, pctx: PolicyContext) -> None:
    async def officer(interaction: discord.Interaction) -> Registry | None:
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
            return None
        if not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return None
        return reg

    doc_choices = [app_commands.Choice(name=d, value=d) for d in policy_mod.DOCS]
    gm = next(c for c in tree.get_commands() if c.name == "gm")
    policy = app_commands.Group(name="policy", description="Officer: loot policy, standing comp instructions, persona", parent=gm)
    rule = app_commands.Group(name="rule", description="Officer: add a rule in plain English (compiled with confirmation)", parent=gm)

    @policy.command(name="show", description="Show a policy document and its compiled reading")
    @app_commands.choices(doc=doc_choices)
    async def policy_show(interaction: discord.Interaction, doc: app_commands.Choice[str]):
        reg = await officer(interaction)
        if not reg:
            return
        ps = pctx.store(reg)
        text = ps.read(doc.value)
        comp = ps.compiled(doc.value)
        e = discord.Embed(title=f"{doc.value} policy", colour=TEAL, description=("```md\n" + text[:1800] + "\n```"))
        if comp:
            items = comp.get("rules") or comp.get("constraints") or []
            e.add_field(name=f"Compiled ({len(items)} items)", value=(comp.get("summary") or "")[:1000], inline=False)
        else:
            e.set_footer(text="No confirmed compiled form yet — /gm policy edit or /gm policy reload")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @policy.command(name="edit", description="Edit a policy document (the bot shows its reading for confirmation)")
    @app_commands.choices(doc=doc_choices)
    async def policy_edit(interaction: discord.Interaction, doc: app_commands.Choice[str]):
        reg = await officer(interaction)
        if not reg:
            return
        await interaction.response.send_modal(PolicyEditModal(reg, pctx.store(reg), doc.value, bot.ctx.provider, ops))

    @policy.command(name="reload", description="Recompile a document from the file (after a PR edit)")
    @app_commands.choices(doc=doc_choices)
    async def policy_reload(interaction: discord.Interaction, doc: app_commands.Choice[str]):
        reg = await officer(interaction)
        if not reg:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ps = pctx.store(reg)
        await compile_and_confirm(interaction, reg, ps, doc.value, ps.read(doc.value), bot.ctx.provider, ops, followup=True)


    async def rule_cmd(interaction: discord.Interaction, doc: str, text: str):
        reg = await officer(interaction)
        if not reg:
            return
        ps = pctx.store(reg)
        previous = ps.read(doc)
        ps.write_draft(doc, previous.rstrip() + f"\n- {text.strip()}\n", interaction.user.display_name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await compile_and_confirm(interaction, reg, ps, doc, previous, bot.ctx.provider, ops, followup=True)

    @rule.command(name="comp", description="Add a standing composition instruction in plain English")
    async def comp_rule(interaction: discord.Interaction, text: str):
        await rule_cmd(interaction, "comp", text)

    @rule.command(name="loot", description="Add a loot policy rule in plain English")
    async def loot_rule(interaction: discord.Interaction, text: str):
        await rule_cmd(interaction, "loot", text)


    @gm.command(name="change", description="Officer: change configuration in plain English (shows a diff, applies on confirm)")
    async def gm_change(interaction: discord.Interaction, text: str):
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await handle_change(interaction, reg, pctx.store(reg), bot.ctx.provider, ops, text, is_owner(interaction, reg), is_officer(interaction, reg))
