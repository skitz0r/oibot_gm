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
from .wizard_flows import FLOWS

from .constants import TEAL  # noqa: E402


class PolicyContext:
    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, policy_mod.PolicyStore] = {}

    def store(self, reg: Registry) -> policy_mod.PolicyStore:
        return self.stores.setdefault(reg.key, policy_mod.PolicyStore(self.guilds.store, reg.key))


# ---------------------------------------------------------------- compile-and-confirm

class CompileConfirmView(discord.ui.View):
    """Confirm / Discard under the bot's reading of a policy edit. Every Discord policy edit is the one pending edit
    in policy.PENDING (begin_edit / compile_pending — the site, MCP and plain text share it). `text` is the exact
    text the reading was compiled from: both buttons refuse once the document no longer reads that way (someone
    edited it since — this reading is stale); `previous` is the text before the edit, Discard's fallback when the
    pending entry is gone."""

    def __init__(self, ps: policy_mod.PolicyStore, doc: str, compiled, previous: str, ops: Ops, reg: Registry, text: str | None = None):
        super().__init__(timeout=900)
        self.ps, self.doc, self.compiled, self.previous, self.ops, self.reg = ps, doc, compiled, previous, ops, reg
        self.text = text if text is not None else ps.read(doc)

    async def _stale(self, interaction: discord.Interaction) -> bool:
        if self.ps.read(self.doc) == self.text:
            return False
        await interaction.response.edit_message(content=f"The {self.doc} policy changed since this reading — nothing done. Compile it again "
                                                        f"(`/gm policy reload`, or ask me to) and confirm that reading.", embed=None, view=None)
        self.stop()
        return True

    @discord.ui.button(label="Confirm this reading", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._stale(interaction):
            return
        self.ps.confirm(self.doc, self.compiled, interaction.user.display_name)
        policy_mod.PENDING.pop((self.ps.key, self.doc), None)  # the text now has a live reading: nothing left pending
        await interaction.response.edit_message(content=f"✅ {self.doc} policy is live with this reading.", embed=None, view=None)
        await self.ops.emit(self.reg.config, "info", f"{interaction.user.display_name} confirmed the compiled {self.doc} policy ({configops.compiled_count(self.compiled)} items)")
        self.stop()

    @discord.ui.button(label="Discard edit", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._stale(interaction):
            return
        held = policy_mod.PENDING.pop((self.ps.key, self.doc), None)
        previous = held["previous"] if held else self.previous
        if self.ps.read(self.doc) != previous:
            self.ps.revert(self.doc, previous, interaction.user.display_name)
        await interaction.response.edit_message(content=f"Edit to {self.doc} discarded; previous text restored.", embed=None, view=None)
        self.stop()


async def send_compiled(send, reg: Registry, ps: policy_mod.PolicyStore, doc: str, compiled, previous: str, ops: Ops, text: str | None = None, **kw):
    """The reading embed + Confirm / Discard through any `send` (an interaction response or followup, a channel or a
    message reply): the one rendering for /gm policy edit|reload, /gm rule and a plain-text policy change.
    `text`: what the reading was compiled from (the view refuses once the document differs)."""
    e = discord.Embed(title=f"My reading of the {doc} policy", colour=TEAL, description=policy_mod.render_compiled(doc, compiled)[:4000])
    e.set_footer(text="Confirm to make this the live compiled form; Discard restores the previous text. Questions above need an edit, not a guess.")
    return await send(embed=e, view=CompileConfirmView(ps, doc, compiled, previous, ops, reg, text=text), **kw)


async def compile_and_confirm(interaction: discord.Interaction, reg: Registry, ps: policy_mod.PolicyStore, doc: str, provider, ops: Ops, followup: bool = False):
    """/gm policy edit|reload and /gm rule after their write (policy.begin_edit): compile the doc's current text as the
    pending reading (policy.compile_pending, off the loop) and show it with Confirm / Discard."""
    send = interaction.followup.send if followup or interaction.response.is_done() else interaction.response.send_message
    if doc == "persona" or provider is None:
        note = "Persona saved (no compile step)." if doc == "persona" else "Saved. No LLM available to compile; run `/gm policy reload` when it is."
        await send(note, ephemeral=True)
        return
    try:
        await asyncio.to_thread(policy_mod.compile_pending, ps, doc, provider)
    except policy_mod.PolicyPendingError as e:
        await send(f"⛔ {e}", ephemeral=True)
        return
    if not await pending_reading(send, reg, ps, doc, ops, ephemeral=True):
        await send(f"The {doc} policy changed while I was reading it — run `/gm policy reload`.", ephemeral=True)


async def pending_reading(send, reg: Registry, ps: policy_mod.PolicyStore, doc: str, ops: Ops, **kw) -> bool:
    """The reading held in policy.PENDING (compile_pending) with Confirm / Discard. False when nothing is waiting
    (the persona, no LLM, or an edit since the compile)."""
    p = policy_mod.pending(ps, doc)
    if not p or p.get("compiled") is None:
        return False
    await send_compiled(send, reg, ps, doc, p["compiled"], p["previous"], ops, text=p.get("text"), **kw)
    return True


class PolicyEditModal(discord.ui.Modal):
    text = discord.ui.TextInput(label="Document", style=discord.TextStyle.paragraph, max_length=4000, required=True)

    def __init__(self, reg: Registry, ps: policy_mod.PolicyStore, doc: str, provider, ops: Ops):
        super().__init__(title=f"Edit {doc} policy"[:45])
        self.reg, self.ps, self.doc, self.provider, self.ops = reg, ps, doc, provider, ops
        self.text.default = ps.read(doc)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        try:  # the one pending edit per doc (policy.begin_edit): remembers the text before it, for Discard
            policy_mod.begin_edit(self.ps, self.doc, str(self.text.value), interaction.user.display_name)
        except policy_mod.PolicyPendingError as e:
            await interaction.response.send_message(f"⛔ {e}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await compile_and_confirm(interaction, self.reg, self.ps, self.doc, self.provider, self.ops, followup=True)


# ---------------------------------------------------------------- plain-text config

LEFT_SERVER = "You're no longer in this guild's Discord server, so plain text can't change anything for you."


async def left_server(bot, reg: Registry, user) -> bool:
    """Someone no longer in the guild's Discord server (a DM still reaches the bot through an old channel) is treated as
    unregistered by every plain-text path, whatever record the registry still holds — as the web treats server
    membership as the whitelist. The owner never is; a client without `in_guild` (tests, offline) counts everyone in."""
    check = getattr(bot, "in_guild", None)
    return check is not None and reg.config.owner_discord_id != user.id and not await check(reg, user)


class ConfigConfirmView(discord.ui.View):
    """Apply / Cancel under a plain-text diff. The requester or an officer may press; the presser's rights decide
    (the ops are authorized again for them, in the channel the request was made in). `revs`: the board fingerprint of
    each run a board op touches, as the diff showed it (configops.board_revs) — Apply refuses a board changed since.
    Single-flight: the first Apply claims the view before anything awaits (buttons gone, "Applying…"); a second press
    (a double click, another officer) and a Cancel mid-apply are refused, so nothing is applied twice."""

    def __init__(self, reg: Registry, req: configops.ConfigRequest, ps: policy_mod.PolicyStore, ops: Ops, requester_id: int, requester: str, channel,
                 revs: dict[str, str] | None = None):
        super().__init__(timeout=600)
        self.reg, self.req, self.ps, self.ops = reg, req, ps, ops
        self.requester_id, self.requester, self.channel = requester_id, requester, channel
        self.revs = dict(revs or {})
        self.busy = False  # set by the first Apply, never cleared: this diff applies once

    async def _may_press(self, interaction: discord.Interaction) -> tuple[bool, list[int]] | None:
        if await left_server(interaction.client, self.reg, interaction.user):
            await interaction.response.send_message(LEFT_SERVER, ephemeral=True)
            return None
        officer, role_ids = await interaction.client.plain_identity(self.reg, interaction.user)
        if interaction.user.id != self.requester_id and not officer:
            await interaction.response.send_message(f"Only {self.requester} or an officer can apply this.", ephemeral=True)
            return None
        return officer, role_ids

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        who = await self._may_press(interaction)
        if who is None:
            return
        officer, role_ids = who
        if self.busy:  # checked and claimed with no await in between: one Apply per diff, whoever presses
            await interaction.response.send_message("Already applying (or applied) — each change applies once.", ephemeral=True)
            return
        self.busy = True
        self.stop()
        from . import raidcycle as rc

        ops = [op.model_copy() for op in self.req.ops]
        bot = interaction.client
        raids = bot.raids.store(self.reg) if getattr(bot, "raids", None) is not None else None
        allowed, refused = configops.authorize(self.reg, ops, interaction.user.id, officer=officer, role_ids=role_ids, channel=self.channel, raids=raids)
        # a lock, a fill send or a compile takes longer than Discord's 3 seconds: acknowledge the press first (the
        # buttons go, so nobody presses again), then edit the result in
        await interaction.response.edit_message(content="⏳ Applying…", view=None)
        me = self.reg.members.get(interaction.user.id)
        by = me.display_name if me else interaction.user.display_name  # the registry's name: a member's own answer counts as theirs
        done, docs, stale = [], [], set()
        for op in configops.coalesce(allowed):  # consecutive raid/schedule settings save as one write
            key = configops.op_run_key(self.reg, op, raids)
            if op.op in configops.BOARD_OPS and key in stale:  # an earlier step found someone else's change on this board
                refused.append(f"{configops.describe(self.reg, op, self.channel)} — {rc.BoardChanged()}")
                continue
            before = configops.snapshot_revs(self.reg, self.revs, raids)
            try:  # authorize() allowed it, so owner-only ops pass when the policy opens them
                done.append(await configops.apply_async(self.reg, op, by, True, self.ps, bot=bot, by_id=interaction.user.id,
                                                        rev=self.revs.get(key) if op.op in configops.BOARD_OPS else None, channel_id=self.channel))
                if op.op in ("policy", "policy_append") and (op.doc or "").strip().lower() in ("loot", "comp"):
                    docs.append((op.doc or "").strip().lower())
            except (RegistryError, ValueError, Exception) as e:  # noqa: BLE001
                refused.append(f"{configops.describe(self.reg, op, self.channel)} — {e}")
                if isinstance(e, rc.BoardChanged) and key:
                    stale.add(key)  # keep the old rev: the rest of this request can't overwrite that change either
                continue
            configops.refresh_revs(self.reg, self.revs, raids, stale, before)  # this request's own write (a pin, a move…) is not someone else's change
        text = ("✅ " + "; ".join(done) if done else "") + ("\n⛔ " + "\n⛔ ".join(refused) if refused else "")
        try:
            await interaction.edit_original_response(content=text[:1900] or "Nothing applied.", view=None, embed=None)
        except discord.HTTPException:  # the change is made either way; the ops line below still records it
            pass
        where = "DMs" if self.channel is None else f"<#{self.channel}>"
        asked = f" (asked by {self.requester})" if interaction.user.id != self.requester_id else ""
        await self.ops.emit(self.reg.config, "info" if not refused else "warn", f"{by} plain-text change in {where}{asked}: {'; '.join(done) or '-'}" + (f" (refused: {len(refused)})" if refused else ""))
        for doc in dict.fromkeys(docs):  # the bot's reading of each edited policy, with Confirm / Discard
            try:
                await pending_reading(interaction.followup.send, self.reg, self.ps or configops._policy_store(self.reg), doc, self.ops, ephemeral=True)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._may_press(interaction) is None:
            return
        if self.busy:
            await interaction.response.send_message("Too late to cancel — it is already being applied.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Cancelled.", view=None, embed=None)
        self.stop()


def _reply_private(bot, reg: Registry, message: discord.Message) -> bool:
    """HelpMixin.reply_private for a Message's channel (a DM is private); without the bot's check, only a DM is."""
    channel = message.channel if message.guild else None
    check = getattr(bot, "reply_private", None)
    return check(reg, channel) if check is not None else channel is None


async def handle_change(interaction_or_message, reg: Registry, ps: policy_mod.PolicyStore, provider, ops: Ops, text: str, *, bot, channel,
                        req: configops.ConfigRequest | None = None, to_help: bool | None = None):
    """Shared by /gm change, @mentions and DMs. `interaction_or_message` is an Interaction (deferred) or a Message;
    `channel`: where it was asked (channel id, None = DMs) — Registry.may_plain decides per op what may act there;
    `req`: an already parsed request (the router parsed first). Refused ops are listed; the rest can still apply.
    `to_help`: a plain question (no ops, nothing to clarify) goes to bot.help_answer — the full /ask snapshot — instead
    of the parser's short reply; by default in an act channel and on /gm change (elsewhere help already answered)."""
    is_msg = isinstance(interaction_or_message, discord.Message)

    async def send(content=None, **kw):
        if is_msg:
            return await interaction_or_message.reply(content, allowed_mentions=discord.AllowedMentions.none(), **{k: v for k, v in kw.items() if k != "ephemeral"})
        return await interaction_or_message.followup.send(content, **kw)

    if provider is None and req is None:
        await send("Plain-text changes need the LLM; use the slash commands or the site.", ephemeral=True)
        return
    author = interaction_or_message.author if is_msg else interaction_or_message.user
    if await left_server(bot, reg, author):
        await send(LEFT_SERVER, ephemeral=True)
        return
    me = reg.members.get(author.id)
    name = me.display_name if me else author.display_name
    officer, role_ids = await bot.plain_identity(reg, author)  # decided in code, before the parser sees anything
    # who can READ the reply decides how much it may carry: a Message outside DMs and officer-only channels is public,
    # so the parser (whose reply and questions go back verbatim) and the help snapshot get the member-level context
    public = is_msg and not _reply_private(bot, reg, interaction_or_message)
    if req is None:
        req = await asyncio.to_thread(configops.parse, provider, reg, text, name, officer=officer and not public)
    if req.kind == "ignore":
        return
    if to_help is None:  # an act channel or /gm change: a plain question gets the full /ask answer, not the parser's line
        to_help = not is_msg or reg.plain_mode(channel) == "act"
    if req.kind == "question" and not req.ops and not req.questions and to_help and getattr(bot, "help_answer", None) is not None:
        answer = await bot.help_answer(reg, author.id, officer, text, public=public)
        if answer:
            await send(answer[:1950], ephemeral=True)
            return
    if req.kind == "question" or req.questions:
        await send((req.reply + ("\n" + "\n".join(f"• {q}" for q in req.questions) if req.questions else ""))[:1900] or "Could you say that another way?", ephemeral=True)
        return
    if not req.ops:
        await send(req.reply[:1900] or "Nothing to change.", ephemeral=True)
        return
    raids = bot.raids.store(reg) if getattr(bot, "raids", None) is not None else None
    allowed, refused = configops.authorize(reg, req.ops, author.id, officer=officer, role_ids=role_ids, channel=channel, raids=raids)

    def apply_button(base=None):  # a newcomer refused with APPLY_FIRST gets the Apply to join button right on the reply
        if configops.APPLY_FIRST not in refused:
            return base
        from .discord_help import apply_view

        return apply_view(reg, author.id, officer=officer, role_ids=role_ids, base=base)

    if not allowed:
        view = apply_button()
        await send("⛔ " + "\n⛔ ".join(refused), ephemeral=True, **({"view": view} if view is not None else {}))
        return
    req.ops = allowed
    lines = [f"• {preview_line(reg, op, channel)}" for op in allowed] + [f"⛔ {r}" for r in refused]
    e = discord.Embed(title="Proposed changes", colour=TEAL, description="\n".join(lines)[:4000])
    if any(op.op in configops.RUN_OPS or op.op == "test" for op in allowed):
        e.set_footer(text="Run actions happen at once on Apply: DMs, cards and the sheet, exactly as the /raid command would.")
    revs = configops.board_revs(reg, allowed, raids)  # the boards as this diff shows them
    view = apply_button(ConfigConfirmView(reg, req, ps, ops, author.id, name, channel, revs=revs))
    await send(req.reply[:500] if req.reply else None, embed=e, view=view, ephemeral=True)


def preview_line(reg: Registry, op: configops.ConfigOp, channel=configops._UNSET) -> str:
    """describe() for the Discord diff. A run about to open also gets Discord's own <t:…:f> stamp, so each reader
    sees the start in their own locale (here only: the site and MCP reuse describe() and would show the raw tag).
    `channel`: where it was asked (the plain op warns when it takes plain text away from there)."""
    line = configops.describe(reg, op, channel)
    if op.op == "run_open":
        try:
            start, _ = configops._open_time(reg, op.target or "", op.value, op.field)
            line += f" · <t:{int(start.timestamp())}:f>"
        except (RegistryError, ValueError):
            pass
    return line


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
        await compile_and_confirm(interaction, reg, pctx.store(reg), doc.value, bot.ctx.provider, ops, followup=True)


    @rule.command(name="comp", description="Add a standing composition instruction in plain English")
    async def comp_rule(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["rule"](interaction, reg, doc="comp")

    @rule.command(name="loot", description="Add a loot policy rule in plain English")
    async def loot_rule(interaction: discord.Interaction):
        reg = await officer(interaction)
        if reg:
            await FLOWS["rule"](interaction, reg, doc="loot")


    @gm.command(name="change", description="Change things in plain English (shows a diff, applies on confirm; plain-text permissions decide)")
    async def gm_change(interaction: discord.Interaction, text: str):
        reg = guilds.for_interaction(interaction)
        if reg is None:
            await interaction.response.send_message("This server isn't configured for oibot_GM.", ephemeral=True)
            return
        where = None if interaction.guild_id is None else interaction.channel_id  # a DM is the policy's DMs entry, not an unlisted channel (stage C L9)
        if reg.plain_mode(where) == "ignore" and not is_owner(interaction, reg):
            await interaction.response.send_message("Plain text is switched off " + ("in DMs." if where is None else "in this channel."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await handle_change(interaction, reg, pctx.store(reg), bot.ctx.provider, ops, text, bot=bot, channel=where)
