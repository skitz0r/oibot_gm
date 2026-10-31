"""Discord surface for §5.15: /policy show|edit|reload, /loot rule, /comp rule,
/gm change <text> (+ @mention in the ops channel)."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from . import configops, guild_routing, policy as policy_mod
from .discord_registry import Guilds, is_officer, is_owner
from .ops import Ops
from .registry import Registry, RegistryError
from .wizard_flows import FLOWS

from .constants import TEAL  # noqa: E402
from .raid_views import guild_prefix  # noqa: E402 — "**Guild** · " when the bot serves several (§2.9)

log = logging.getLogger(__name__)


class PolicyContext:
    def __init__(self, guilds: Guilds):
        self.guilds = guilds
        self.stores: dict[str, policy_mod.PolicyStore] = {}

    def store(self, reg: Registry) -> policy_mod.PolicyStore:
        return self.stores.setdefault(reg.key, policy_mod.PolicyStore(self.guilds.store, reg.key))


# ---------------------------------------------------------------- pending confirms that survive a restart gracefully (§2.9)

EXPIRED = "Expired — say it again."
EXPIRED_RESTART = "Expired after a restart — nothing was changed. Say it again."
PENDING_CONFIRMS: dict[str, "PendingView"] = {}  # token -> the live diff / reading; process memory by design (a restart forgets them)
PENDING_KEEP = 50  # finished entries kept so a late press on an applied diff still answers "each change applies once"


class CfgApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cfgapply(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<token>[A-Za-z0-9_-]{8,32}):(?P<action>apply|cancel|confirm|discard|edit)"):
    """Apply / Cancel under a plain-text diff and Confirm / Discard under a policy reading, as persistent ids: the press
    resolves its token to the pending view in this process (`PendingView.press`), and a press after a restart — when
    the token is gone — edits the message to EXPIRED_RESTART instead of Discord's bare "interaction failed"."""
    LABELS = {"apply": ("Apply", discord.ButtonStyle.success), "cancel": ("Cancel", discord.ButtonStyle.secondary),
              "confirm": ("Confirm this reading", discord.ButtonStyle.success), "discard": ("Discard edit", discord.ButtonStyle.secondary),
              "edit": ("Give the reason…", discord.ButtonStyle.primary)}  # the loot chat diff: a missing / guessed reason is given before Apply

    def __init__(self, gk: str | None, token: str, action: str):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"cfgapply{'.' + gk if gk else ''}:{token}:{action}"))
        self.gk, self.token, self.action = gk, token, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["g"], match["token"], match["action"])

    async def callback(self, interaction: discord.Interaction):
        view = PENDING_CONFIRMS.get(self.token)
        if view is None:
            try:
                await interaction.response.edit_message(content=EXPIRED_RESTART, embed=None, view=None)
            except discord.HTTPException:
                if not interaction.response.is_done():
                    await interaction.response.send_message(EXPIRED_RESTART, ephemeral=True)
            return
        await view.press(interaction, self.action)


class PendingView(discord.ui.View):
    """A View whose buttons are CfgApplyButtons bound to this instance by a token. `bind(editor)` gives it the way to
    edit its own message (a Message.edit, a WebhookMessage.edit or edit_original_response) so `on_timeout` can turn
    the buttons into EXPIRED; `press(interaction, action)` dispatches to `press_<action>`."""

    ACTIONS: tuple[str, ...] = ()

    def __init__(self, reg: Registry, timeout: float):
        super().__init__(timeout=timeout)
        import secrets

        self.reg = reg
        self.token = secrets.token_urlsafe(12)
        self.editor = None
        for a in self.ACTIONS:
            item = CfgApplyButton(reg.key, self.token, a)
            self.add_item(item)
            setattr(self, a, item)  # view.apply.callback(interaction) is how a press reaches it (and how tests drive it)
        _register(self)

    def bind(self, sent) -> "PendingView":
        """Remember how to edit the message this view went out on: `sent` is what `send` returned (a Message or
        WebhookMessage with .edit, an InteractionCallbackResponse with .resource), or an edit coroutine function."""
        edit = sent if callable(sent) and not hasattr(sent, "edit") else getattr(sent, "edit", None) or getattr(getattr(sent, "resource", None), "edit", None)
        self.editor = edit
        from . import pending_views

        msg = sent if hasattr(sent, "channel") and hasattr(sent, "id") else getattr(sent, "resource", None)
        owner = getattr(sent, "__self__", None)  # interaction.edit_original_response: the ephemeral reply, by its interaction
        if msg is not None and getattr(getattr(msg, "channel", None), "id", None) and not getattr(getattr(msg, "flags", None), "ephemeral", False):
            pending_views.track(f"pv:{self.token}", channel_id=msg.channel.id, message_id=msg.id)
        elif owner is not None:
            pending_views.track_interaction(f"pv:{self.token}", owner)
        return self

    async def expire(self, text: str = EXPIRED) -> None:
        """Buttons off, the text says to say it again. Called by on_timeout; safe to call twice."""
        from . import pending_views

        PENDING_CONFIRMS.pop(self.token, None)
        pending_views.untrack(f"pv:{self.token}")
        if self.editor is None:
            return
        try:
            await self.editor(content=text, embed=None, view=None)
        except Exception as e:  # noqa: BLE001 — the message may be gone; nothing was changed either way
            log.debug("confirm view expiry edit failed: %s", e)
        self.editor = None

    async def on_timeout(self) -> None:
        await self.expire(EXPIRED)

    async def press(self, interaction: discord.Interaction, action: str) -> None:
        if action not in self.ACTIONS:
            await interaction.response.send_message(EXPIRED, ephemeral=True)
            return
        await getattr(self, f"press_{action}")(interaction)
        if self.is_finished():
            from . import pending_views

            pending_views.untrack(f"pv:{self.token}")


def _register(view: PendingView) -> None:
    PENDING_CONFIRMS[view.token] = view
    done = [t for t, v in PENDING_CONFIRMS.items() if v.is_finished()]
    for t in done[:-PENDING_KEEP] if len(done) > PENDING_KEEP else []:
        PENDING_CONFIRMS.pop(t, None)


# ---------------------------------------------------------------- compile-and-confirm

class CompileConfirmView(PendingView):
    """Confirm / Discard under the bot's reading of a policy edit. Every Discord policy edit is the one pending edit
    in policy.PENDING (begin_edit / compile_pending — the site, MCP and plain text share it). `text` is the exact
    text the reading was compiled from: both buttons refuse once the document no longer reads that way (someone
    edited it since — this reading is stale); `previous` is the text before the edit, Discard's fallback when the
    pending entry is gone. The buttons are CfgApplyButtons (a press after a restart says so; a timeout says
    EXPIRED)."""

    ACTIONS = ("confirm", "discard")

    def __init__(self, ps: policy_mod.PolicyStore, doc: str, compiled, previous: str, ops: Ops, reg: Registry, text: str | None = None):
        super().__init__(reg, timeout=900)
        self.ps, self.doc, self.compiled, self.previous, self.ops = ps, doc, compiled, previous, ops
        self.text = text if text is not None else ps.read(doc)

    async def _stale(self, interaction: discord.Interaction) -> bool:
        if self.ps.read(self.doc) == self.text:
            return False
        held = self.ps.load_pending(self.doc)
        who = f" ({policy_mod.waiting_words(held, self.reg.local12)})" if held and held.get("by") else ""
        await interaction.response.edit_message(content=f"The {self.doc} policy changed since this reading{who} — nothing done. Compile it again "
                                                        f"(`/gm policy reload`, or ask me to) and confirm that reading.", embed=None, view=None)
        self.stop()
        return True

    async def press_confirm(self, interaction: discord.Interaction):
        if await self._stale(interaction):
            return
        self.ps.confirm(self.doc, self.compiled, interaction.user.display_name, self.text)  # the pending edit on disk goes in the same commit
        policy_mod.PENDING.pop((self.ps.key, self.doc), None)
        await interaction.response.edit_message(content=f"✅ {self.doc} policy is live with this reading.", embed=None, view=None)
        await self.ops.emit(self.reg.config, "info", f"{interaction.user.display_name} confirmed the compiled {self.doc} policy ({configops.compiled_count(self.compiled)} items)")
        self.stop()

    async def press_discard(self, interaction: discord.Interaction):
        if await self._stale(interaction):
            return
        held = self.ps.load_pending(self.doc)
        policy_mod.PENDING.pop((self.ps.key, self.doc), None)
        previous = held["previous"] if held else self.previous
        if self.ps.read(self.doc) != previous:
            self.ps.revert(self.doc, previous, interaction.user.display_name)  # drops the pending file too
        elif held is not None:
            policy_mod.discard_pending(self.ps, self.doc, interaction.user.display_name)
        await interaction.response.edit_message(content=f"Edit to {self.doc} discarded; previous text restored.", embed=None, view=None)
        self.stop()


async def send_compiled(send, reg: Registry, ps: policy_mod.PolicyStore, doc: str, compiled, previous: str, ops: Ops, text: str | None = None, **kw):
    """The reading embed + Confirm / Discard through any `send` (an interaction response or followup, a channel or a
    message reply): the one rendering for /gm policy edit|reload, /gm rule and a plain-text policy change.
    `text`: what the reading was compiled from (the view refuses once the document differs)."""
    e = discord.Embed(title=f"My reading of the {doc} policy", colour=TEAL, description=policy_mod.render_compiled(doc, compiled)[:4000])
    e.set_footer(text="Confirm to make this the live compiled form; Discard restores the previous text. Questions above need an edit, not a guess.")
    view = CompileConfirmView(ps, doc, compiled, previous, ops, reg, text=text)
    sent = await send(embed=e, view=view, **kw)
    view.bind(sent)
    return sent


async def compile_and_confirm(interaction: discord.Interaction, reg: Registry, ps: policy_mod.PolicyStore, doc: str, provider, ops: Ops, followup: bool = False):
    """/gm policy edit|reload and /gm rule after their write (policy.begin_edit): compile the doc's current text as the
    pending reading (policy.compile_pending, off the loop) and show it with Confirm / Discard."""
    send = interaction.followup.send if followup or interaction.response.is_done() else interaction.response.send_message
    if provider is None and doc != "persona":  # the persona's reading is its own text: no model needed
        await send("Saved as a draft. No LLM available to compile; run `/gm policy reload` when it is.", ephemeral=True)
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
    """/gm policy edit. A document that fits Discord's 4000-character box is edited whole (prefilled); a longer one is
    NEVER prefilled truncated — the box becomes append-only ("the full text is on the site") and what is typed is added
    at the end, so a save can't cut the document."""

    text = discord.ui.TextInput(label="Document", style=discord.TextStyle.paragraph, max_length=policy_mod.MODAL_MAX, required=True)

    def __init__(self, reg: Registry, ps: policy_mod.PolicyStore, doc: str, provider, ops: Ops):
        current = ps.read(doc)
        self.append_only = len(current) > policy_mod.MODAL_MAX
        super().__init__(title=(f"Add to the {doc} policy" if self.append_only else f"Edit {doc} policy")[:45])
        self.reg, self.ps, self.doc, self.provider, self.ops = reg, ps, doc, provider, ops
        if self.append_only:
            self.text.label = "Add at the end (full text on the site)"
            self.text.placeholder = "The document is longer than Discord's box: what you type here is added at the end."[:100]
        else:
            self.text.default = current

    async def on_submit(self, interaction: discord.Interaction):
        typed = str(self.text.value)
        try:  # the one pending edit per doc (policy.begin_edit): remembers the text before it, for Discard
            if self.append_only:  # THE append verb, as a paragraph (policy.append_rule, bullet=False)
                policy_mod.append_rule(self.ps, self.doc, typed, interaction.user.display_name, by_id=interaction.user.id, when=self.reg.local12,
                                       bullet=False)
            else:
                policy_mod.begin_edit(self.ps, self.doc, typed, interaction.user.display_name, by_id=interaction.user.id, when=self.reg.local12)
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
    return check is not None and not reg.is_owner_id(user.id) and not await check(reg, user)


class ConfigConfirmView(PendingView):
    """Apply / Cancel under a plain-text diff. The requester or an officer may press; the presser's rights decide
    (the ops are authorized again for them, in the channel the request was made in). `revs`: the board fingerprint of
    each run a board op touches, as the diff showed it (configops.board_revs) — Apply refuses a board changed since.
    Single-flight: the first Apply claims the view before anything awaits (buttons gone, "Applying…"); a second press
    (a double click, another officer) and a Cancel mid-apply are refused, so nothing is applied twice. The buttons are
    CfgApplyButtons (`cfgapply.<gk>:<token>:apply|cancel`): a timeout edits the diff to EXPIRED, a press after a
    restart answers EXPIRED_RESTART — never Discord's "interaction failed"."""

    ACTIONS = ("apply", "cancel")

    def __init__(self, reg: Registry, req: configops.ConfigRequest, ps: policy_mod.PolicyStore, ops: Ops, requester_id: int, requester: str, channel,
                 revs: dict[str, str] | None = None):
        super().__init__(reg, timeout=600)
        self.req, self.ps, self.ops = req, ps, ops
        self.requester_id, self.requester, self.channel = requester_id, requester, channel
        self.revs = dict(revs or {})
        self.busy = False  # set by the first Apply, never cleared: this diff applies once

    async def _may_press(self, interaction: discord.Interaction) -> tuple[bool, list[int]] | None:
        if await left_server(interaction.client, self.reg, interaction.user):
            await interaction.response.send_message(LEFT_SERVER, ephemeral=True)
            return None
        officer, role_ids = await interaction.client.plain_identity(self.reg, interaction.user, max_age=guild_routing.PRIV_LIVE)  # a write: decided now, never from the cache
        if interaction.user.id != self.requester_id and not officer:
            await interaction.response.send_message(f"Only {self.requester} or an officer can apply this.", ephemeral=True)
            return None
        return officer, role_ids


    async def press_apply(self, interaction: discord.Interaction):
        who = await self._may_press(interaction)
        if who is None:
            return
        officer, role_ids = who
        if self.busy:  # checked and claimed with no await in between: one Apply per diff, whoever presses
            await interaction.response.send_message("Already applying, applied or cancelled — each change applies once.", ephemeral=True)
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
                                                        rev=configops.op_rev(op, key, self.revs), channel_id=self.channel,
                                                        self_id=self.requester_id))
                if op.op in ("policy", "policy_append") and (op.doc or "").strip().lower() in policy_mod.DOCS:
                    docs.append((op.doc or "").strip().lower())
            except (RegistryError, ValueError, Exception) as e:  # noqa: BLE001
                refused.append(f"{configops.describe(self.reg, op, self.channel)} — {e}")
                if isinstance(e, rc.BoardChanged) and key:
                    stale.add(key)  # keep the old rev: the rest of this request can't overwrite that change either
                continue
            configops.refresh_revs(self.reg, self.revs, raids, stale, before)  # this request's own write (a pin, a move…) is not someone else's change
        text = ("✅ " + "; ".join(done) if done else "") + ("\n⛔ " + "\n⛔ ".join(refused) if refused else "")
        text = guild_prefix(self.reg) + (text or "Nothing applied.")
        if refused:
            text = configops.plain_footer(text[:1850])
        try:
            await interaction.edit_original_response(content=text[:1950], view=None, embed=None)
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


    async def press_cancel(self, interaction: discord.Interaction):
        if await self._may_press(interaction) is None:
            return
        if self.busy:
            await interaction.response.send_message("Too late to cancel — it is already being applied.", ephemeral=True)
            return
        self.busy = True  # claimed like Apply claims it (no await in between): an Apply press still checking permissions now refuses
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None, embed=None)


def pick_question(recipe: str, character: str, *, on_record: bool = False) -> str:
    """The words above ProfessionPickView's select (the Confirm rule: what a pick does, and that nothing happens before it).
    `on_record`: every choice is already on the character, so a pick only adds the recipe."""
    what = f"Picking one adds the recipe to that profession on {character}." if on_record else f"Picking one records it on {character} and adds the recipe."
    return f"Which profession makes {recipe}?\n{what} Nothing changes until you pick."


def pick_result(character: str, profession: str, recipe: str, *, added: bool) -> str:
    """The picker's message once the pick is applied: the profession recorded with the recipe, or the recipe alone."""
    return f"{character} now has {profession} on record and makes {recipe}." if added else f"{character}'s {profession} gains {recipe}."


def pick_changed(character: str) -> str:
    return f"{character}'s record changed since the question. Say it again."


class ProfessionPickView(PendingView):
    """The bot's clarification when a plain-text recipe names no profession it can tell (design §5.60 follow-up, owner
    decision 2026-10-30: ask, never gatekeep): a select of the candidate professions bound to the asker — a component,
    never a conversational turn. A pick sets the op's profession (op.text), authorizes it again for the presser now
    (PRIV_LIVE) and applies it through the path Apply uses, then edits the message to what was recorded. Ten minutes,
    then EXPIRED; mirrored in pending_views like the confirm view (a restart edits it to "The bot restarted; nothing was
    changed."). Single-flight: the first pick claims it."""

    def __init__(self, reg: Registry, op: configops.ConfigOp, need, ps: policy_mod.PolicyStore | None, ops: Ops, requester_id: int, requester: str, channel):
        super().__init__(reg, timeout=600)
        self.op, self.need, self.ps, self.ops = op, need, ps, ops
        self.requester_id, self.requester, self.channel = requester_id, requester, channel
        self.busy = False
        self.on_record = False
        try:
            _, m = configops._character_member(reg, op)
            self.rev = reg.characters_rev(m) if m is not None else None
            c = next((x for x in m.active() if x.label == need.character), None) if m is not None else None
            self.on_record = c is not None and set(need.candidates) <= {p.id for p in c.professions}
        except RegistryError:
            self.rev = None
        names = {d.id: d.name for d in reg.profile.professions.values()}
        options = [discord.SelectOption(label=names[pid], value=pid) for pid in reg.profile.professions if pid in need.candidates][:25]
        self.select = discord.ui.Select(placeholder="Pick the profession", options=options, min_values=1, max_values=1)
        self.select.callback = self.picked
        self.add_item(self.select)

    @property
    def text(self) -> str:
        return pick_question(self.need.recipe, self.need.character, on_record=self.on_record)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(f"Only {self.requester} can pick this.", ephemeral=True)
            return False
        return True

    async def picked(self, interaction: discord.Interaction, values: list[str] | None = None):
        if interaction.user.id != self.requester_id:  # interaction_check runs first in Discord; a direct call (tests) checks here
            await interaction.response.send_message(f"Only {self.requester} can pick this.", ephemeral=True)
            return
        if await left_server(interaction.client, self.reg, interaction.user):
            await interaction.response.send_message(LEFT_SERVER, ephemeral=True)
            return
        if self.busy:
            await interaction.response.send_message("Already picked — each change applies once.", ephemeral=True)
            return
        self.busy = True
        self.stop()
        from . import pending_views, professions

        pending_views.untrack(f"pv:{self.token}")
        pid = (values or self.select.values or [""])[0]
        op = self.op.model_copy(update={"text": pid})
        bot = interaction.client
        officer, role_ids = await bot.plain_identity(self.reg, interaction.user, max_age=guild_routing.PRIV_LIVE)  # a write: decided now
        raids = bot.raids.store(self.reg) if getattr(bot, "raids", None) is not None else None
        allowed, refused = configops.authorize(self.reg, [op], interaction.user.id, officer=officer, role_ids=role_ids, channel=self.channel, raids=raids)

        me = self.reg.members.get(interaction.user.id)
        by = me.display_name if me else interaction.user.display_name
        where = "DMs" if self.channel is None else f"<#{self.channel}>"

        async def refused_line(text: str) -> None:  # as Apply's view: a refusal after the press is in the audit trail too
            await self.ops.emit(self.reg.config, "warn", f"{by} plain-text change in {where}: - (refused: {text})")

        async def finish(text: str) -> None:
            await interaction.response.edit_message(content=guild_prefix(self.reg) + text, view=None, embed=None)
            await refused_line(text)

        if not allowed:
            await finish(refused[0] if refused else "Nothing applied.")
            return
        try:
            _, m = configops._character_member(self.reg, op)
            if self.rev is not None and m is not None and self.reg.characters_rev(m) != self.rev:
                raise RegistryError(pick_changed(self.need.character))
            rows, _ = configops._character_rows(self.reg, m, op)
            c = next(x for x in m.active() if x.label == rows[0]["label"])
            added = professions.added_with_recipe(c, rows)
            name = self.reg.profile.profession(pid).name
        except RegistryError as e:
            await finish(str(e))
            return
        await interaction.response.edit_message(content="⏳ Applying…", view=None, embed=None)
        try:
            line = await configops.apply_async(self.reg, op, by, True, self.ps, bot=bot, by_id=interaction.user.id, channel_id=self.channel, self_id=self.requester_id)
        except Exception as e:  # noqa: BLE001 — the one sentence, as Apply prints a refusal
            text = str(e) if isinstance(e, (RegistryError, ValueError)) else configops.GENERIC_ERROR
            try:
                await interaction.edit_original_response(content=guild_prefix(self.reg) + text, view=None, embed=None)
            except discord.HTTPException:
                pass
            await refused_line(text)
            return
        try:
            await interaction.edit_original_response(content=guild_prefix(self.reg) + pick_result(c.label, name, self.need.recipe, added=bool(added)), view=None, embed=None)
        except discord.HTTPException:  # the change is made either way; the ops line below still records it
            pass
        await self.ops.emit(self.reg.config, "info", f"{by} plain-text change in {where}: {line}")


def _reply_private(bot, reg: Registry, message: discord.Message) -> bool:
    """HelpMixin.reply_private for a Message's channel (a DM is private); without the bot's check, only a DM is."""
    channel = message.channel if message.guild else None
    check = getattr(bot, "reply_private", None)
    return check(reg, channel) if check is not None else channel is None


async def handle_change(interaction_or_message, reg: Registry, ps: policy_mod.PolicyStore, provider, ops: Ops, text: str, *, bot, channel,
                        req: configops.ConfigRequest | None = None, to_help: bool | None = None, conversation: str | None = None):
    """Shared by /gm change, @mentions and DMs. `interaction_or_message` is an Interaction (deferred) or a Message;
    `channel`: where it was asked (channel id, None = DMs) — Registry.may_plain decides per op what may act there;
    `req`: an already parsed request (the router parsed first). Refused ops are listed; the rest can still apply.
    `to_help`: a plain question (no ops, nothing to clarify) goes to bot.help_answer — the full /ask snapshot — instead
    of the parser's short reply; by default in an act channel and on /gm change (elsewhere help already answered).
    `conversation`: the recent exchange (conversation.context_block); None = recalled here — a Discord reply to one of
    the bot's plain-text messages continues that exchange, else the same person in the same place within the window
    (design §5.51). Every reply is remembered with the bot's message id; a /gm change shares the channel's ring."""
    from . import conversation as conv_mod

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
    if conversation is None:
        conversation = conv_mod.context_block(conv_mod.recall(reg, channel, author.id, reply_to_message_id=conv_mod.reply_to_id(interaction_or_message) if is_msg else None))
    conv_kw = {"conversation": conversation} if conversation else {}

    def keep(sent, reply: str, questions=()) -> None:  # the exchange, in memory only (a restart forgets)
        conv_mod.remember(reg, channel, author.id, text, reply, list(questions), message_id=getattr(interaction_or_message, "id", None) if is_msg else None,
                          reply_message_id=getattr(sent, "id", None))
    officer, role_ids = await bot.plain_identity(reg, author)  # decided in code, before the parser sees anything
    # who can READ the reply decides how much it may carry: a Message outside DMs and officer-only channels is public,
    # so the parser (whose reply and questions go back verbatim) and the help snapshot get the member-level context
    public = is_msg and not _reply_private(bot, reg, interaction_or_message)
    if req is None:
        from .llm.provider import for_guild

        try:
            with for_guild(reg.key):  # D45: the parse is this guild's spend
                req = await asyncio.to_thread(configops.parse, provider, reg, text, name, officer=officer and not public, **conv_kw)
        except Exception as e:  # noqa: BLE001 — §2.7: the model being down is a sentence to the asker + an ops error, never silence
            await send(await configops.report_error(ops, reg.config, e, "plain-text parse"), ephemeral=True)
            return
    if req.kind == "ignore":
        return
    if to_help is None:  # an act channel or /gm change: a plain question gets the full /ask answer, not the parser's line
        to_help = not is_msg or reg.plain_mode(channel) == "act"
    if req.kind == "question" and not req.ops and not req.questions and to_help and getattr(bot, "help_answer", None) is not None:
        answer = await bot.help_answer(reg, author.id, officer, text, public=public, **conv_kw)
        if answer:
            keep(await send(answer[:1950], ephemeral=True), answer[:1950])
            return
    if req.kind == "question" or req.questions:
        keep(await send((req.reply + ("\n" + "\n".join(f"• {q}" for q in req.questions) if req.questions else ""))[:1900] or "Could you say that another way?", ephemeral=True),
             req.reply, req.questions)
        return
    if not req.ops:
        keep(await send(req.reply[:1900] or "Nothing to change.", ephemeral=True), req.reply or "Nothing to change.")
        return
    raids = bot.raids.store(reg) if getattr(bot, "raids", None) is not None else None
    allowed, refused = configops.authorize(reg, req.ops, author.id, officer=officer, role_ids=role_ids, channel=channel, raids=raids)
    # a people or run action the registry already refuses (an unknown field, a character that isn't theirs) is a refusal,
    # never a proposed change with an Apply button under it
    from .professions import NeedsProfession

    sound, pick = [], None
    for op in allowed:
        try:
            configops.describe(reg, op, channel, strict=True, by_id=author.id)
            sound.append(op)
        except NeedsProfession as e:  # the recipe's profession is unclear: ask with a picker (one per reply), never a refusal
            if pick is None:
                pick = (op, e)
            else:
                refused.append(str(e))
        except RegistryError as e:
            refused.append(str(e))
    allowed = sound

    async def send_pick(first: bool) -> None:
        if pick is None:
            return
        view = ProfessionPickView(reg, pick[0], pick[1], ps, ops, author.id, name, channel)
        sent = await send(guild_prefix(reg) + view.text, view=view, ephemeral=True)
        keep(sent, view.text)
        view.bind(sent if is_msg or not first else getattr(interaction_or_message, "edit_original_response", None))

    def apply_button(base=None):  # a newcomer refused with APPLY_FIRST gets the Apply to join button right on the reply
        if configops.APPLY_FIRST not in refused:
            return base
        from .discord_help import apply_view

        return apply_view(reg, author.id, officer=officer, role_ids=role_ids, base=base)

    if not allowed:
        if pick is not None and not refused:
            await send_pick(True)
            return
        view = apply_button()
        keep(await send(configops.plain_footer(guild_prefix(reg) + "⛔ " + "\n⛔ ".join(refused))[:1990], ephemeral=True, **({"view": view} if view is not None else {})),
             "Refused: " + "; ".join(refused))
        await send_pick(False)
        return
    req.ops = allowed
    body = await diff_text(reg, allowed, refused, channel, by_id=author.id)
    if refused:
        body = configops.plain_footer(body[:3900])
    e = discord.Embed(title=f"{reg.config.name} · proposed changes" if reg.many_guilds else "Proposed changes", colour=TEAL, description=body[:4000])
    if any(op.op in configops.RUN_OPS or op.op == "test" for op in allowed):
        e.set_footer(text="Run actions happen at once on Apply: DMs, cards and the sheet, exactly as the /raid command would.")
    revs = configops.board_revs(reg, allowed, raids)  # the boards as this diff shows them
    confirm = ConfigConfirmView(reg, req, ps, ops, author.id, name, channel, revs=revs)
    view = apply_button(confirm)
    sent = await send(req.reply[:500] if req.reply else None, embed=e, view=view, ephemeral=True)
    keep(sent, ((req.reply or "") + "\nProposed changes (shown with Apply; nothing written yet):\n" + body).strip())
    # how the view edits its own message when it expires (EXPIRED): the reply Message, or the interaction's original response
    confirm.bind(sent if is_msg else getattr(interaction_or_message, "edit_original_response", None))
    await send_pick(False)


async def diff_text(reg: Registry, allowed: list, refused: list[str], channel, *, by_id: int | None = None) -> str:
    """The plain-text diff's body. Setup writes (configops.PURE_OPS) are the STRUCTURE every surface shares —
    configops.describe_ops → Described.as_discord(): one bullet per write as apply_ops will make it (coalesced), current →
    new, the dry run's refusal, the consequence sentences; the site's review and MCP's preview print the same lines. Run,
    people and loot actions keep their describe() line (preview_line: a run about to open carries its <t:…:f> stamp); a
    profession change prints its confirm sentences, "your" for the asker (`by_id`)."""
    setup = [op for op in allowed if op.op in configops.PURE_OPS]
    lines = [f"• {preview_line(reg, op, channel, by_id=by_id)}" for op in allowed if op.op not in configops.PURE_OPS]
    if setup:
        d = await asyncio.to_thread(configops.describe_ops, reg, setup, None, channel)
        lines.append(d.as_discord())
    return "\n".join(lines + [f"⛔ {r}" for r in refused])


def preview_line(reg: Registry, op: configops.ConfigOp, channel=configops._UNSET, *, by_id: int | None = None) -> str:
    """describe() for the Discord diff. A run about to open also gets Discord's own <t:…:f> stamp, so each reader
    sees the start in their own locale (here only: the site and MCP reuse describe() and would show the raw tag).
    `channel`: where it was asked (the plain op warns when it takes plain text away from there)."""
    line = configops.describe(reg, op, channel, by_id=by_id)
    if op.op == "run_open":
        try:
            start, _ = configops._open_time(reg, op.target or "", op.value, op.field)
            line += f" · <t:{int(start.timestamp())}:f>"
        except (RegistryError, ValueError):
            pass
    return line


# ---------------------------------------------------------------- commands

POLICY_SHOW_PARTS = 3  # /gm policy show: the text in at most this many messages, then "continued on the site"
RULES_SHOWN = 15  # the Compiled reading embed's numbered rules; "+N more on the site" after


def help_site_url(reg: Registry, path: str, anchor: str | None = None) -> str | None:
    from .help import site_url

    return site_url(reg, path, anchor)


def policy_show_messages(reg: Registry, ps: policy_mod.PolicyStore, doc: str) -> tuple[list[str], discord.Embed]:
    """/gm policy show: the written text in whole paragraphs (≤POLICY_SHOW_PARTS messages, the last saying it is
    continued on the site), then ONE "Compiled reading" embed — the live rules numbered (≤RULES_SHOWN, "+N more on the
    site") and a state footer (compiled when and by whom · the text reads differently now · a draft waits, by whom). The
    numbers and words come from policy.policy_state, as the site's Policy page prints them."""
    from .discord_help import chunk_text

    st = policy_mod.policy_state(ps, doc, reg.local12)
    url = help_site_url(reg, "/app/policy", doc)
    parts = chunk_text(st["text"].replace("```", "ʼʼʼ"), 1800) or ["(empty)"]
    shown = parts[:POLICY_SHOW_PARTS]
    texts = [f"```md\n{p}\n```" for p in shown]
    texts[0] = f"{guild_prefix(reg)}**The {doc} policy — written**\n" + texts[0]
    if len(parts) > POLICY_SHOW_PARTS:
        texts[-1] += "\n-# … continued on the site" + (f": {url}" if url else " (the Policy page)")
    e = discord.Embed(title=f"Compiled reading — {doc}" if doc != "persona" else "The persona the bot speaks with", colour=TEAL)
    if doc == "persona":
        e.description = (st["applied_text"] or "")[:3500] or "(none)"
    elif st["compiled"]:
        items = st["items"]
        lines = [f"{i}. {t}" for i, t in enumerate(items[:RULES_SHOWN], 1)]
        if len(items) > RULES_SHOWN:
            lines.append(f"+{len(items) - RULES_SHOWN} more on the site" + (f" — [the Policy page]({url})" if url else ""))
        e.description = ("\n".join(lines) or "(no rules)")[:3800] + (f"\n\n_{st['summary']}_" if st.get("summary") else "")
    else:
        e.description = "No confirmed reading yet: `/gm policy reload` compiles the text and shows the reading to confirm."
    foot = []
    if st["compiled"] and st["compiled_label"]:
        foot.append(f"compiled {st['compiled_label']}" + (f" by {st['confirmed_by']}" if st.get("confirmed_by") else ""))
    if st["differs"]:
        foot.append("the written text no longer reads this way — compile and confirm to apply it")
    if st["pending"]:
        foot.append(f"a draft by {st['pending_by'] or 'an officer'} waits ({'its reading is below' if st['pending_compiled'] else 'not compiled yet'})")
    e.set_footer(text=" · ".join(foot)[:2000] or "the written text and the live reading agree")
    return texts, e


def register_policy_commands(tree: app_commands.CommandTree, guilds: Guilds, ops: Ops, bot, pctx: PolicyContext) -> None:
    async def officer(interaction: discord.Interaction) -> tuple[Registry | None, discord.Interaction]:
        reg, interaction = await guild_routing.need(interaction, guilds=guilds)
        if reg is None:
            return None, interaction
        if not is_officer(interaction, reg):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return None, interaction
        return reg, interaction

    doc_choices = [app_commands.Choice(name=d, value=d) for d in policy_mod.DOCS]
    gm = next(c for c in tree.get_commands() if c.name == "gm")
    policy = app_commands.Group(name="policy", description="Officer: loot policy, standing comp instructions, persona", parent=gm)
    rule = app_commands.Group(name="rule", description="Officer: add a rule in plain English (compiled with confirmation)", parent=gm)

    @policy.command(name="show", description="Show a policy document and its compiled reading")
    @app_commands.choices(doc=doc_choices)
    async def policy_show(interaction: discord.Interaction, doc: app_commands.Choice[str]):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        ps = pctx.store(reg)
        texts, e = policy_show_messages(reg, ps, doc.value)
        await interaction.response.send_message(texts[0], ephemeral=True)
        for t in texts[1:]:
            await interaction.followup.send(t, ephemeral=True)
        await interaction.followup.send(embed=e, ephemeral=True)
        await pending_reading(interaction.followup.send, reg, ps, doc.value, ops, ephemeral=True)  # a waiting reading: Confirm / Discard here too

    config = next((c for c in gm.commands if c.name == "config"), None)
    if config is not None and not any(c.name == "plain" for c in config.commands):
        @config.command(name="plain", description="Read the plain-text permissions: who may change what by chat, and where (edited on the site)")
        async def config_plain(interaction: discord.Interaction):
            """D37: a READ view for the owner and officers — this channel's mode and why first, then the groups, then
            the other listed channels. The site's Ops page is the editor; there is no Discord editor."""
            reg, interaction = await officer(interaction)
            if not reg:
                return
            from .discord_pool import plain_read_text

            where = None if interaction.guild_id is None else interaction.channel_id
            text = plain_read_text(reg, bot.plain_table(reg), where)
            url = help_site_url(reg, "/app/ops", "plain")
            await interaction.response.send_message((guild_prefix(reg) + text + (f"\n-# [Edit on the site]({url})" if url else ""))[:2000], ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())

    @policy.command(name="edit", description="Edit a policy document (the bot shows its reading for confirmation)")
    @app_commands.choices(doc=doc_choices)
    async def policy_edit(interaction: discord.Interaction, doc: app_commands.Choice[str]):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await interaction.response.send_modal(PolicyEditModal(reg, pctx.store(reg), doc.value, bot.ctx.provider, ops))

    @policy.command(name="reload", description="Recompile a document from the file (after a PR edit)")
    @app_commands.choices(doc=doc_choices)
    async def policy_reload(interaction: discord.Interaction, doc: app_commands.Choice[str]):
        reg, interaction = await officer(interaction)
        if not reg:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await compile_and_confirm(interaction, reg, pctx.store(reg), doc.value, bot.ctx.provider, ops, followup=True)


    @rule.command(name="comp", description="Add a standing composition instruction in plain English")
    async def comp_rule(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["rule"](interaction, reg, doc="comp")

    @rule.command(name="loot", description="Add a loot policy rule in plain English")
    async def loot_rule(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["rule"](interaction, reg, doc="loot")


    @gm.command(name="precedents", description="Officer: the council's precedents (overrides with their reasons) — read one, retire it")
    async def gm_precedents(interaction: discord.Interaction):
        reg, interaction = await officer(interaction)
        if reg:
            await FLOWS["gm_precedents"](interaction, reg)

    @gm.command(name="change", description="Change things in plain English (shows a diff, applies on confirm; plain-text permissions decide)")
    async def gm_change(interaction: discord.Interaction, text: str):
        reg, interaction = await guild_routing.need(interaction, guilds=guilds)
        if reg is None:
            return
        where = None if interaction.guild_id is None else interaction.channel_id  # a DM is the policy's DMs entry, not an unlisted channel (stage C L9)
        if reg.plain_mode(where) == "ignore" and not is_owner(interaction, reg):
            await interaction.response.send_message(configops.plain_footer("Plain text is switched off " + ("in DMs." if where is None else "in this channel.")), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await handle_change(interaction, reg, pctx.store(reg), bot.ctx.provider, ops, text, bot=bot, channel=where)
