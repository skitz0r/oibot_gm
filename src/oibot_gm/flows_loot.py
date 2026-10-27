"""The loot council's Discord flows (dual interfaces phase 1, design §3.1): what every LootButton / LootPick press and
persistent lootmodal submit does, and the wizards behind them.

  · Tick drops… (`loot_tick`): boss → the boss's items, paged 25 at a time → "Not on the table…" (a modal: the item's
    name, prose) → how many copies (a preset select) → Tick. Un-tick… lists the drops not yet recorded.
  · Override… / Correct… (`loot_override`): ONE select grouped In the table · Rostered tonight · Disenchant · Someone
    not on the roster…, then the reason modal whose Submit IS the write — two presses, no third screen. A name is
    typed only for someone not on the roster (validated as a character name, recorded by name when unregistered).
  · /raid distribute (`loot_distribute`), End council and /raid end (`loot_end`: the decide screen with
    LootMixin.end_consequence), /gm precedents (`gm_precedents`: pick one → Retire → the reason).

Every write calls a LootMixin verb with the presser's display name (`by`) and id (`by_id`); officer standing is decided
live (role_of, PRIV_LIVE) on every press and every modal submit (a Modal has no interaction_check). The reason modals
carry persistent ids (`lootmodal.<gk>:<session>:<drop>:<kind>[:<arg>]`): a modal opened before a restart and submitted
after it is resolved by OibotGM.on_interaction through `loot_modal_raw`, the same `loot_modal_submit` as a live one."""
from __future__ import annotations

import logging
import re

import discord

from .loot import session as ls
from .loot.session import DISENCHANT, LootBusy, LootChanged
from .registry import Registry, RegistryError
from .wizard import MAX_OPTIONS, Field, Form, Opt, Wizard, flow

log = logging.getLogger(__name__)

TABLE_TOP = 7  # the Override select's "In the table" rows (the rest of the 25 are rostered tonight)
NAME_RE = re.compile(r"[^\W\d_]{2,24}( [^\W\d_]{2,24})?")  # a character name: letters only (accents allowed), 2–24 — WoW: Forever's "First Last" too
COPIES = [Opt(str(n), f"{n} cop{'y' if n == 1 else 'ies'}", default=n == 1) for n in range(1, 6)]
ENDED = "This council has ended — its summary is in the thread; the site keeps its history."
NEW_BOSS, TRASH, NOT_LISTED, BY_MEMBER = "__new", "?", "__name", "__member"


# ---------------------------------------------------------------- shared

async def is_loot_officer(bot, reg: Registry, user, *, live: bool = True) -> bool:
    from . import guild_routing

    return await bot.role_of(reg, user, max_age=guild_routing.PRIV_LIVE if live else guild_routing.PRIV_ROUTING) in ("owner", "officer")


def loot_wizard(bot, reg: Registry, interaction: discord.Interaction, title: str) -> Wizard:
    """A Wizard whose guard re-checks officer standing live at every press."""
    async def guard(i: discord.Interaction) -> str | None:
        return None if await is_loot_officer(bot, reg, i.user) else "Officers only."
    return Wizard(reg, interaction.user.id, title=title, guard=guard)


async def say(interaction: discord.Interaction, text: str, *, view: discord.ui.View | None = None) -> None:
    """An ephemeral answer, whether or not the press was acknowledged already."""
    kw = {"view": view} if view is not None else {}
    if interaction.response.is_done():
        await interaction.followup.send(text[:1900], ephemeral=True, **kw)
    else:
        await interaction.response.send_message(text[:1900], ephemeral=True, **kw)


def when_label(reg: Registry, value) -> str:
    """A stored date or stamp as a person reads it (the clock rule): an ISO date → 'Thu 24 Dec', a stamp →
    'Thu 24 Dec 8:12 PM' (Registry.local12). Precedent rows carry either (a council override: the session's date; a
    companion award's reason: when it was received)."""
    v = str(value or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"\d{4}-\d\d-\d\d", v):
        return reg.day_label(v)
    try:
        return reg.local12(v)
    except (TypeError, ValueError):
        return v


def _err(e: BaseException) -> str:
    from .configops import user_error

    return user_error(e)


def _target(bot, reg: Registry, sid: str):
    return bot.events.get((reg.key, sid))


def _drop_name(session, drop_id: str) -> str:
    if isinstance(session, str):
        return drop_id
    d = session.drop(drop_id)
    return d.item_name if d is not None else drop_id


# ---------------------------------------------------------------- button presses

async def loot_press(interaction: discord.Interaction, btn) -> None:
    """Every LootButton press: the guild from the id (never asked), officer standing live, the session (or — for a
    reason / a correction — the id of a council that has ended), then the action."""
    from .guild_routing import resolve_button

    bot = interaction.client
    reg, interaction = await resolve_button(interaction, btn.gk)
    if reg is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(ENDED, ephemeral=True)
        return
    if not await is_loot_officer(bot, reg, interaction.user, live=btn.action != "info"):
        await say(interaction, "Officers only.")
        return
    session = _target(bot, reg, btn.sid)
    if session is None and btn.action not in ("why", "whyr", "fix", "guest"):
        await say(interaction, ENDED)
        return
    handler = ACTIONS.get(btn.action)
    if handler is None:
        await say(interaction, "That button is from another version — run /raid loot for fresh cards.")
        return
    await handler(interaction, bot, reg, session if session is not None else btn.sid, btn)


async def _defer(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()


async def _press_tick(interaction, bot, reg, session, btn) -> None:
    from .wizard import FLOWS

    await FLOWS["loot_tick"](interaction, reg, sid=session.id)


async def _press_dist(interaction, bot, reg, session, btn) -> None:
    await distribute_press(interaction, bot, reg, session)


async def _press_scores(interaction, bot, reg, session, btn) -> None:
    await _defer(interaction)
    try:
        await bot.set_scores_only(reg, session, not session.scores_only, by=interaction.user.display_name, by_id=interaction.user.id)
    except RegistryError as e:
        await say(interaction, _err(e))


async def _press_end(interaction, bot, reg, session, btn) -> None:
    rs, ev = bot.loot_run(session)
    if ev is None:
        await say(interaction, "That council's run is gone.")
        return
    await end_decide(interaction, bot, reg, rs, ev)


async def _refreshed(bot, reg, session, interaction, e: BaseException) -> None:
    """A stale press: the cards are redrawn as they are now and the presser is told what changed."""
    try:
        await bot.loot_batches_sync(reg, session)
        await bot.loot_card_refresh(reg, session)
    except Exception as x:  # noqa: BLE001
        log.debug("loot refresh after a stale press: %s", x)
    await say(interaction, f"{_err(e)} The cards show it as it is now.")


async def _press_all(interaction, bot, reg, session, btn) -> None:
    await _defer(interaction)
    try:
        await bot.confirm(reg, session, batch_rev=btn.arg, by=interaction.user.display_name, by_id=interaction.user.id)
    except LootChanged as e:
        await _refreshed(bot, reg, session, interaction, e)
    except RegistryError as e:
        await say(interaction, _err(e))


async def _press_discard(interaction, bot, reg, session, btn) -> None:
    n = len(session.proposals)
    n_ov = sum(1 for p in session.proposals if p.override)
    wiz = loot_wizard(bot, reg, interaction, "Discard the proposals")
    rev = btn.arg

    async def do(i: discord.Interaction, _note: str | None) -> None:
        cur = _target(bot, reg, session.id)
        if cur is None:
            await wiz.finish(i, ENDED)
            return
        try:
            line = await bot.discard(reg, cur, batch_rev=rev, by=i.user.display_name, by_id=i.user.id)
        except RegistryError as e:
            await wiz.finish(i, _err(e))
            return
        await wiz.finish(i, line)

    text = (f"Discard {n} proposal(s)? Ticks stay" + (f"; {n_ov} pending override(s) are dropped" if n_ov else "")
            + " — nothing was recorded, so nothing is undone. Distribute judges the drops again.")
    await wiz.decide(interaction, text, do=do, do_label="Discard", danger=True)


async def _press_keep(interaction, bot, reg, session, btn) -> None:
    await _defer(interaction)  # answered before the write (a git commit + the ops line): Discord's 3 seconds
    try:
        line = await bot.keep_open(reg, session, by=interaction.user.display_name, by_id=interaction.user.id)
    except RegistryError as e:
        await say(interaction, _err(e))
        return
    try:
        await interaction.edit_original_response(content=f"⏳ {line} ({interaction.user.display_name})", view=None)
    except Exception:  # noqa: BLE001
        await say(interaction, line)


async def _press_ok(interaction, bot, reg, session, btn) -> None:
    """Confirm one pick as the card showed it (the recipient's hash rides in the id: a moved pick is refused)."""
    await _defer(interaction)
    p = session.pending(btn.drop)
    want = None
    if p is not None:
        from .raid_views import loot_pick_hash

        want = p.to if btn.arg is None or loot_pick_hash(p.to) == btn.arg else "\x00moved"
    try:
        await bot.confirm(reg, session, only={btn.drop}, expect_to=want, by=interaction.user.display_name, by_id=interaction.user.id)
    except LootChanged as e:
        await _refreshed(bot, reg, session, interaction, e)
    except RegistryError as e:
        await say(interaction, _err(e))


async def _press_later(interaction, bot, reg, session, btn) -> None:
    await _defer(interaction)
    try:
        await bot.discard(reg, session, only={btn.drop}, by=interaction.user.display_name, by_id=interaction.user.id)
    except RegistryError as e:
        await say(interaction, _err(e))


def _item_card(bot, reg, session, drop_id: str, outcome: str | None = None):
    from .raid_views import item_layout

    ctx = bot.loot_ctx(session)
    d = session.drop(drop_id)
    item = ctx.item(d.item_id or None, d.item_name) if d is not None else None
    url = None
    try:
        from .discord_bot import wowhead

        url = wowhead(item, ctx.profile)
    except Exception:  # noqa: BLE001
        url = None
    return item_layout(reg, session, drop_id, bot.ico, item=item, url=url, outcome=outcome)


async def _press_info(interaction, bot, reg, session, btn) -> None:
    if session.drop(btn.drop) is None:
        await say(interaction, "That drop isn't on this council any more.")
        return
    await interaction.response.send_message(view=_item_card(bot, reg, session, btn.drop), ephemeral=True)


async def _press_iok(interaction, bot, reg, session, btn) -> None:
    """The item card's Confirm: the card edits itself to the outcome (buttons gone)."""
    await _defer(interaction)
    p = session.pending(btn.drop)
    from .raid_views import loot_pick_hash

    want = p.to if p is not None and (btn.arg is None or loot_pick_hash(p.to) == btn.arg) else ("\x00moved" if p is not None else None)
    try:
        await bot.confirm(reg, session, only={btn.drop}, expect_to=want, by=interaction.user.display_name, by_id=interaction.user.id)
        outcome = f"✅ recorded by you · {bot.loot_clock(reg)}"
    except RegistryError as e:
        outcome = f"⛔ {_err(e)}"
    await interaction.edit_original_response(view=_item_card(bot, reg, session, btn.drop, outcome=outcome))


async def _press_ovr(interaction, bot, reg, session, btn) -> None:
    from .wizard import FLOWS

    # always a new ephemeral message: a card (thread or item card) is a components-v2 layout, which Discord never lets an
    # edit turn back into a text + buttons screen
    await FLOWS["loot_override"](interaction, reg, sid=session.id, drop=btn.drop, rev=btn.arg, mode="override")


async def _press_ovrr(interaction, bot, reg, session, btn) -> None:
    """Replace another officer's pending override: the modal again, prefilled with what this officer submitted."""
    held = bot._surface()["reasons"].get((reg.key, session.id, btn.drop, interaction.user.id, "ovr")) or {}
    who = held.get("who")
    if not who:
        await say(interaction, "Pick who gets it again with Override… on the card.")
        return
    await interaction.response.send_modal(reason_modal(reg.key, session.id, btn.drop, "ovr", arg="r", item=_drop_name(session, btn.drop),
                                                       who=who, default=held.get("reason")))


async def _press_ide(interaction, bot, reg, session, btn) -> None:
    p = session.pending(btn.drop)
    if p is None:
        await say(interaction, "Nothing pending on that drop any more.")
        return
    await interaction.response.send_modal(reason_modal(reg.key, session.id, btn.drop, "de", arg=ls.batch_rev(session), item=p.item_name, who=DISENCHANT,
                                                       hint=f"Disenchant instead of giving it to {p.award_to} — recorded at once"))


async def _press_why(interaction, bot, reg, target, btn) -> None:
    held = bot._surface()["reasons"].get((reg.key, btn.sid, btn.drop, interaction.user.id, "why")) or {}
    await interaction.response.send_modal(reason_modal(reg.key, btn.sid, btn.drop, "why", arg="r" if btn.action == "whyr" else None,
                                                       item=_drop_name(target, btn.drop), default=held.get("reason") if btn.action == "whyr" else None))


async def _press_fix(interaction, bot, reg, target, btn) -> None:
    from .wizard import FLOWS

    await FLOWS["loot_override"](interaction, reg, sid=btn.sid, drop=btn.drop, mode="correct")


async def _press_rm(interaction, bot, reg, session, btn) -> None:
    await _defer(interaction)
    try:
        await bot.remove_candidate(reg, session, btn.arg or "", by=interaction.user.display_name, by_id=interaction.user.id)
    except RegistryError as e:
        await say(interaction, _err(e))


ACTIONS = {"tick": _press_tick, "dist": _press_dist, "scores": _press_scores, "end": _press_end, "all": _press_all, "discard": _press_discard,
           "keep": _press_keep, "ok": _press_ok, "ovr": _press_ovr, "iovr": _press_ovr, "ovrr": _press_ovrr, "later": _press_later,
           "info": _press_info, "iok": _press_iok, "ide": _press_ide, "why": _press_why, "whyr": _press_why, "fix": _press_fix, "rm": _press_rm}


async def loot_pick(interaction: discord.Interaction, pick, values: list[str]) -> None:
    """Every LootPick choice: `owed` (why.<drop> → the reason modal, fix.<drop> → Correct…), `remove` (a candidate who
    left the roster), `bind` (a companion no council could take → this council), `whynot.<drop>` (code's comparison of
    the pick with the chosen candidate)."""
    from .guild_routing import resolve_button

    bot = interaction.client
    reg, interaction = await resolve_button(interaction, pick.gk)
    if reg is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(ENDED, ephemeral=True)
        return
    kind = pick.kind
    if not await is_loot_officer(bot, reg, interaction.user, live=not kind.startswith("whynot")):
        await say(interaction, "Officers only.")
        return
    session = _target(bot, reg, pick.sid)
    value = values[0] if values else ""
    if kind == "owed":
        what, _, drop = value.partition(".")
        btn = type("Pick", (), {"gk": reg.key, "sid": pick.sid, "drop": drop, "action": what, "arg": None})()
        await (_press_why if what == "why" else _press_fix)(interaction, bot, reg, session if session is not None else pick.sid, btn)
        return
    if session is None:
        await say(interaction, ENDED)
        return
    if kind == "remove":
        await _defer(interaction)
        try:
            await bot.remove_candidate(reg, session, value, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await say(interaction, _err(e))
        return
    if kind == "bind":  # Bind companion…: that companion's events go to this council
        await _defer(interaction)
        try:
            line = await bot.loot_bind_companion(reg, session, value, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await say(interaction, _err(e))
            return
        await say(interaction, line)
        return
    if kind.startswith("whynot."):
        from .raid_views import why_not_text

        p = session.pending(kind.split(".", 1)[1])
        await say(interaction, why_not_text(p, value) if p is not None else "That pick isn't pending any more.")
        return
    await say(interaction, "That menu is from another version — run /raid loot for fresh cards.")


# ---------------------------------------------------------------- the reason modals (persistent ids)

class LootModal(discord.ui.Modal):
    """A reason box (and, for an override or a correction, who): its custom id names the guild, the council, the drop
    and the kind, so its submit means the same before and after a restart. `who` is a one-option select (the person
    picked on the screen before — shown, not typed) or a text box (someone not on the roster)."""

    def __init__(self, gk: str, sid: str, drop: str, kind: str, *, title: str, arg: str | None = None, who: str | None = None,
                 who_text: bool = False, label: str = "The reason", hint: str | None = None, default: str | None = None):
        from .raid_buttons import loot_custom_id

        super().__init__(title=title[:45], timeout=None, custom_id=loot_custom_id("lootmodal", gk, sid, drop, kind, arg))
        self.ids = (gk, sid, drop, kind, arg)
        self.who_item = None
        if who_text:
            self.who_item = discord.ui.TextInput(custom_id="who", max_length=49, min_length=2, placeholder="their character's name (first and last, if they have one)")
            self.add_item(discord.ui.Label(text="Who (not on the roster)", description="a character name — checked against the signups, the roster and past guests", component=self.who_item))
        elif who:
            from .raid_views import loot_who

            self.who_item = discord.ui.Select(custom_id="who", options=[discord.SelectOption(label=loot_who(who)[:100], value=who[:100], default=True)],
                                              min_values=1, max_values=1, required=True)
            self.add_item(discord.ui.Label(text="Award to" if kind in ("ovr", "de") else "Record it to", component=self.who_item))
        self.reason = discord.ui.TextInput(custom_id="reason", style=discord.TextStyle.paragraph, min_length=ls.REASON_MIN, max_length=ls.REASON_MAX,
                                           default=(default or None) and default[:ls.REASON_MAX])
        self.add_item(discord.ui.Label(text=label[:45], description=(hint or None) and hint[:100], component=self.reason))
        self._items = {**({"who": self.who_item} if self.who_item is not None else {}), "reason": self.reason}  # by key, like wizard.Form
    def values(self) -> dict[str, str]:
        who = ""
        if isinstance(self.who_item, discord.ui.Select):
            who = self.who_item.values[0] if self.who_item.values else ""
        elif self.who_item is not None:
            who = str(self.who_item.value or "")
        return {"who": who.strip(), "reason": str(self.reason.value or "").strip()}

    async def on_submit(self, interaction: discord.Interaction) -> None:
        interaction.extras["loot_modal"] = True  # claimed before any await: on_interaction's restart path leaves it alone
        await loot_modal_submit(interaction, self.ids, self.values())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("loot modal failed", exc_info=error)
        await say(interaction, f"❌ {_err(error)}")


def reason_modal(gk: str, sid: str, drop: str, kind: str, *, item: str, arg: str | None = None, who: str | None = None, who_text: bool = False,
                 hint: str | None = None, default: str | None = None) -> LootModal:
    from .raid_views import loot_who

    if kind == "why":
        return LootModal(gk, sid, drop, kind, title=f"Why {item}?", arg=arg, label="Why it went to them",
                         hint=hint or "becomes the precedent the bot cites next time", default=default)
    if kind == "fix":
        return LootModal(gk, sid, drop, kind, title=f"Correct {item}", arg=arg, who=who, who_text=who_text, label="What went wrong",
                         hint=hint or "a mistake, not a decision: no precedent is written", default=default)
    title = f"{item} → {loot_who(who)}" if who and not who_text else f"Award {item}"
    return LootModal(gk, sid, drop, kind, title=title, arg=arg, who=who, who_text=who_text, label="Why (required)",
                     hint=hint or "becomes the precedent at Confirm", default=default)


def _modal_values(data: dict) -> dict[str, str]:
    """A modal submit's raw components → {custom_id: value} (a text box's value, a select's first value), Labels and
    action rows alike — the restart path, where no Modal object exists."""
    out: dict[str, str] = {}

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        cid = node.get("custom_id")
        if cid and ("value" in node or "values" in node):
            out[cid] = str(node.get("value") if "value" in node else ((node.get("values") or [""])[0])).strip()
        walk(node.get("component"))
        walk(node.get("components"))
    walk(data.get("components") or [])
    return out


MODAL_RE = re.compile(r"lootmodal(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<sid>[A-Za-z0-9_\-]+):(?P<drop>d\d+|-):(?P<kind>[a-z]{2,8})(?::(?P<arg>[A-Za-z0-9_\-]{1,24}))?")


async def loot_modal_raw(interaction: discord.Interaction) -> bool:
    """OibotGM.on_interaction: a lootmodal submit no live Modal claimed (opened before a restart) → the same submit.
    Returns True when it was a loot modal."""
    data = getattr(interaction, "data", None) or {}
    m = MODAL_RE.fullmatch(str(data.get("custom_id") or ""))
    if m is None:
        return False
    if interaction.extras.get("loot_modal") or interaction.response.is_done():
        return True
    interaction.extras["loot_modal"] = True
    v = _modal_values(data)
    await loot_modal_submit(interaction, (m["g"], m["sid"], m["drop"], m["kind"], m["arg"]), {"who": v.get("who", ""), "reason": v.get("reason", "")})
    return True


async def _answer_submit(interaction: discord.Interaction, text: str, *, kind: str, session=None) -> None:
    """Where a submit's outcome goes: the wizard message it came from (edited to the outcome), else an ephemeral line;
    a thread line it came from loses its buttons (its final state) — never a card, which redraws itself."""
    msg = getattr(interaction, "message", None)
    cards = set((session.cards or {}).values()) if session is not None and not isinstance(session, str) else set()
    if kind in ("ovr", "fix") and msg is not None:
        try:
            if interaction.response.is_done():  # acknowledged before the write (loot_modal_submit)
                await interaction.edit_original_response(content=text[:2000], view=None, embed=None)
            else:
                await interaction.response.edit_message(content=text[:2000], view=None, embed=None)
            return
        except Exception:  # noqa: BLE001
            pass
    await say(interaction, text)
    if kind == "why" and msg is not None and getattr(msg, "id", None) not in cards and hasattr(msg, "edit"):
        try:
            await msg.edit(view=None)
        except Exception:  # noqa: BLE001 — a line that keeps its buttons answers "already gave a reason — replace?"
            pass


def offroster_name(bot, reg: Registry, target, who: str) -> str | None:
    """A typed 'not on the roster' name checked against who it can be (design §3.1 L16): the run's signups (any answer:
    bench, no thanks), the guild's registered characters, then the guests the ledger already records by name (a sub or a
    pug, no member). Returns the spelling found there, or None (nobody by that name). Two registered characters sharing
    the name → the registry's "use the full name" refusal."""
    low = who.strip().lower()
    if not isinstance(target, str):
        _rs, ev = bot.loot_run(target)
        for sg in (ev.signups.values() if ev is not None else ()):
            if (sg.character or "").lower() == low:
                return sg.character
    hit = reg.find(who)
    if hit and hit[1].name:
        return hit[1].label
    try:
        ctx = bot.loot_ctx(target) if not isinstance(target, str) else bot._guild_ctx(reg)
        ledger = list(getattr(ctx, "ledger", None) or [])
    except Exception:  # noqa: BLE001 — no ledger to read: only the signups and the registry decide
        ledger = []
    return next((a.raider for a in ledger if a.discord_id is None and (a.raider or "").lower() == low), None)


async def _press_guest(interaction, bot, reg, target, btn) -> None:
    """'Record <name> as a guest': the override / correction the officer submitted, with the unknown name accepted."""
    held = bot._surface()["reasons"].pop((reg.key, btn.sid, btn.drop, interaction.user.id, "guest"), None)
    if not held:
        await say(interaction, "That expired — pick them again with Override… (or Correct…).")
        return
    await loot_modal_submit(interaction, held["ids"], {"who": held["who"], "reason": held["reason"]}, guest_ok=True)


ACTIONS["guest"] = _press_guest


async def _ack_submit(interaction: discord.Interaction) -> None:
    """Answer a submit before its write (a git commit, the ops line and the thread's card edits can outlast Discord's
    3 seconds on a busy raid night): a submit from a message defers as an update of it (the outcome edits it or follows
    up), one without a message as "thinking" (the outcome is the follow-up)."""
    if interaction.response.is_done():
        return
    try:
        if getattr(interaction, "message", None) is not None:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException as e:
        log.debug("loot submit defer: %s", e)


async def loot_modal_submit(interaction: discord.Interaction, ids: tuple, values: dict[str, str], *, guest_ok: bool = False) -> None:
    """The write behind every reason modal: why (set_reason; `r` = replace), ovr (override; arg = the batch_rev seen,
    `r` = replace another officer's), de (disenchant and record at once), fix (correct). Acknowledged before any write
    (`_ack_submit`); every outcome is then an edit of the original response or a follow-up."""
    from .guild_routing import resolve_button
    from .raid_buttons import LootButton

    gk, sid, drop, kind, arg = ids
    bot = interaction.client
    reg, interaction = await resolve_button(interaction, gk)
    if reg is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(ENDED, ephemeral=True)
        return
    if not await is_loot_officer(bot, reg, interaction.user):
        await say(interaction, "Officers only.")
        return
    session = _target(bot, reg, sid)
    if session is None and kind not in ("why", "fix"):
        await say(interaction, ENDED)
        return
    by, by_id = interaction.user.display_name, interaction.user.id
    who, reason = values.get("who", ""), values.get("reason", "")
    target = session if session is not None else sid
    stash = bot._surface()["reasons"]
    await _ack_submit(interaction)
    try:
        if kind == "why":
            try:
                line = await bot.set_reason(reg, target, drop, reason, by=by, by_id=by_id, replace=arg == "r")
            except LootChanged as e:  # someone already gave one: replacing it is a second, deliberate press
                stash[(reg.key, sid, drop, by_id, "why")] = {"reason": reason}
                view = discord.ui.View(timeout=None)
                view.add_item(LootButton(reg.key, sid, drop, "whyr"))
                await say(interaction, _err(e), view=view)
                return
            await _answer_submit(interaction, line, kind=kind, session=session)
            return
        if kind in ("ovr", "de", "fix") and not who:
            raise RegistryError("Pick who gets it.")
        if kind in ("ovr", "fix") and who not in (DISENCHANT,) and not _on_roster(bot, reg, target, who):
            if not NAME_RE.fullmatch(who):
                raise RegistryError("That isn't a character name (one or two words, letters only, 2–24 each) — check the spelling.")
            known = offroster_name(bot, reg, target, who)
            if known:
                who = known  # the spelling the run's signups, the registry or the ledger already have
            elif not guest_ok:
                # nobody by that name: a typo, or a new guest — recording them is a second, deliberate press
                stash[(reg.key, sid, drop, by_id, "guest")] = {"ids": tuple(ids), "who": who, "reason": reason}
                view = discord.ui.View(timeout=None)
                view.add_item(LootButton(reg.key, sid, drop, "guest", label=f"Record {who} as a guest"[:80]))
                await say(interaction, f"Nobody called **{who}** signed up for this run, is registered or was recorded as a guest before — "
                                       "check the spelling (Override… again), or record them as a new guest.", view=view)
                return
        if kind == "ovr":
            try:
                line = await bot.override(reg, session, drop, who, reason, by=by, by_id=by_id, batch_rev=None if arg in (None, "r") else arg, replace=arg == "r")
            except LootChanged as e:
                if "replace?" not in str(e):
                    await _refreshed_submit(bot, reg, session, interaction, e)
                    return
                stash[(reg.key, sid, drop, by_id, "ovr")] = {"who": who, "reason": reason}
                view = discord.ui.View(timeout=None)
                view.add_item(LootButton(reg.key, sid, drop, "ovrr", label="Replace their override…"))
                await say(interaction, _err(e), view=view)
                return
            await _answer_submit(interaction, line.replace("**", ""), kind=kind, session=session)
            return
        if kind == "de":
            await bot.override(reg, session, drop, DISENCHANT, reason, by=by, by_id=by_id, batch_rev=arg, rejudge=False)
            await bot.confirm(reg, session, only={drop}, expect_to=DISENCHANT, by=by, by_id=by_id)
            try:
                await interaction.edit_original_response(view=_item_card(bot, reg, session, drop, outcome=f"✅ disenchanted — recorded by you · {bot.loot_clock(reg)}"))
            except Exception:  # noqa: BLE001 — not opened from the item card
                await say(interaction, f"✅ {_drop_name(session, drop)} disenchanted — recorded.")
            return
        if kind == "fix":
            line = await bot.correct(reg, target, drop, who, reason, by=by, by_id=by_id)
            await _answer_submit(interaction, line.replace("**", ""), kind=kind, session=session)
            return
    except LootChanged as e:
        await _refreshed_submit(bot, reg, session, interaction, e)
        return
    except RegistryError as e:
        await say(interaction, f"⛔ {_err(e)}")
        return
    await say(interaction, "That box is from another version — nothing was changed.")


async def _refreshed_submit(bot, reg, session, interaction, e) -> None:
    if session is not None and not isinstance(session, str):
        await _refreshed(bot, reg, session, interaction, e)
    else:
        await say(interaction, _err(e))


def _on_roster(bot, reg, target, who: str) -> bool:
    if isinstance(target, str):
        return False
    low = who.lower()
    return any((p.character or "").lower() == low or p.signup_name.lower() == low for p in bot.loot_roster(target))


# ---------------------------------------------------------------- Override… / Correct…

def recipient_opts(bot, reg: Registry, target, drop: str, *, exclude: str | None, ico) -> list[Opt]:
    """The grouped select: In the table (the top of the candidate table) · Rostered tonight (the rest of the live
    roster) · Disenchant · Someone not on the roster…; past 25, a "Someone else rostered…" row opens Discord's member
    picker instead."""
    table, rostered = [], []
    seen = {str(exclude or "").lower()}
    p = target.pending(drop) if not isinstance(target, str) else None
    cands = list(p.result.candidates) if p is not None else []
    if not cands and not isinstance(target, str):
        a = target.awarded(drop)
        cands = []
        snap = list(a.candidates_snapshot) if a is not None else []
        for c in snap:
            nm = str(c.get("character") or "")
            if nm and nm.lower() not in seen:
                seen.add(nm.lower())
                cls, _, spec = str(c.get("spec") or "").partition(":")
                table.append(Opt(nm, nm, f"In the table · {c.get('tier', '?')} · {float(c.get('base') or 0):.2f}", emoji=ico("spec", f"{cls}:{spec}") or None))
    for c in cands:
        if len(table) >= TABLE_TOP:
            break
        if c.character.lower() in seen or c.already_has:
            continue
        seen.add(c.character.lower())
        table.append(Opt(c.character, c.character, f"In the table · {c.tier} · {c.base_score:.2f}", emoji=ico("spec", f"{c.cls}:{c.spec}") or None))
    roster = bot.loot_roster(target) if not isinstance(target, str) else []
    for pl in roster:
        nm = pl.character or pl.signup_name
        if nm.lower() in seen:
            continue
        seen.add(nm.lower())
        rostered.append(Opt(nm, nm, "Rostered tonight", emoji=ico("spec", f"{pl.cls}:{pl.spec}") or None))
    tail = [Opt(DISENCHANT, "Disenchant", "nobody gets it — recorded as disenchanted"),
            Opt(NOT_LISTED, "Someone not on the roster…", "type their character's name")]
    room = MAX_OPTIONS - len(table) - len(tail)
    if len(rostered) > room:
        tail.insert(0, Opt(BY_MEMBER, "Someone else rostered…", f"{len(rostered) - room + 1} more: pick the member"))
        room -= 1
    if exclude == DISENCHANT:
        tail = [o for o in tail if o.value != DISENCHANT]
    return table + rostered[:max(0, room)] + tail


@flow("loot_override")
async def open_loot_override(interaction: discord.Interaction, reg: Registry, *, sid: str, drop: str, rev: str | None = None,
                             mode: str = "override", **_seed) -> None:
    """Override… (mode override) / Correct… (mode correct): one grouped select, then the reason modal whose Submit is
    the write."""
    from .raid_views import loot_who

    bot = interaction.client
    target = _target(bot, reg, sid) or sid
    item = _drop_name(target, drop)
    if mode == "override":
        if isinstance(target, str):
            await say(interaction, ENDED)
            return
        p = target.pending(drop)
        a = target.awarded(drop)
        if p is None and a is not None:
            await say(interaction, f"{a.item_name} was recorded at {bot.loot_clock(reg, a.confirmed_at)} by {a.by or '?'} — use Correct.")
            return
        bot_pick = p.award_to if p is not None else None
        current = p.to if p is not None else None
        text = f"**{item}**: " + (f"the bot picked **{loot_who(bot_pick)}**" if bot_pick else "not judged yet")
        if p is not None and p.override:
            text += f"; {p.override.by} set it to **{loot_who(p.to)}** (“{p.override.reason}”)"
        text += ". Award it to…\n-# the reason comes next; nothing is recorded until Confirm"
        placeholder, title, kind = "Award to…", f"Override · {item}", "ovr"
    else:
        a = target.awarded(drop) if not isinstance(target, str) else None
        current = a.raider if a is not None else None
        text = f"**{item}**" + (f" is recorded to **{loot_who(current)}**" if current else "") + ". Record it to…\n-# a mistake, not a decision: the old record is kept, marked void; no precedent"
        placeholder, title, kind = "Record it to…", f"Correct · {item}", "fix"
    wiz = loot_wizard(bot, reg, interaction, title)
    opts = recipient_opts(bot, reg, target, drop, exclude=current, ico=bot.ico)
    hint = f"instead of {loot_who(current)}" if current else None

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        v = values[0] if values else ""
        if v == NOT_LISTED:
            await wiz.open_form(i, reason_modal(reg.key, sid, drop, kind, arg=rev, item=item, who_text=True, hint=hint))
            return
        if v == BY_MEMBER:
            await wiz.show(i, text, wiz.members("Pick the member", member_picked), wiz.button("Back", back))
            return
        await wiz.open_form(i, reason_modal(reg.key, sid, drop, kind, arg=rev, item=item, who=v, hint=hint))

    async def member_picked(i: discord.Interaction, users: list) -> None:
        u = users[0]
        m = reg.members.get(u.id)
        name = None
        if m is not None and not isinstance(target, str):
            _, ev = bot.loot_run(target)
            hit = ev.seat_of(m) if ev is not None else None
            name = (hit[1].character or hit[1].signup_name) if hit else (m.main.label if m.main else None)
        if not name:
            await wiz.refuse(i, f"{getattr(u, 'display_name', 'They')} has no registered character — use Someone not on the roster… and type it.", back)
            return
        await wiz.open_form(i, reason_modal(reg.key, sid, drop, kind, arg=rev, item=item, who=name, hint=hint))

    async def back(i: discord.Interaction) -> None:
        await wiz.show(i, text, wiz.select(placeholder, opts, picked), wiz.button("Cancel", wiz.cancel))

    await wiz.show(interaction, text, wiz.select(placeholder, opts, picked), wiz.button("Cancel", wiz.cancel))


# ---------------------------------------------------------------- Tick drops…

class TickWizard(Wizard):
    """Boss → the boss's items (25 a page) → copies → Tick; "Not on the table…" names an item the tables don't know
    (every item on a new raid's first night); Un-tick… removes drops not yet recorded."""

    PAGE = MAX_OPTIONS

    def __init__(self, bot, reg: Registry, session, owner_id: int):
        async def guard(i: discord.Interaction) -> str | None:
            return None if await is_loot_officer(bot, reg, i.user) else "Officers only."
        super().__init__(reg, owner_id, title="Tick drops", guard=guard)
        self.bot, self.sid = bot, session.id
        self.draft = {"boss": session.current_boss or TRASH, "page": 0, "items": [], "copies": 1}

    @property
    def session(self):
        return _target(self.bot, self.reg, self.sid)

    def bosses(self) -> list[Opt]:
        s = self.session
        names: list[str] = []
        for b in self.reg.raid_def(s.instance).get("bosses", []) or []:
            nm = b.get("name") if isinstance(b, dict) else str(b)
            if nm and nm not in names:
                names.append(nm)
        for nm in s.boss_order():
            if nm not in ("?", "") and nm not in names:
                names.append(nm)
        cur = self.draft.get("boss")
        opts = [Opt(nm, nm, default=nm == cur) for nm in names[:MAX_OPTIONS - 2]]
        return opts + [Opt(TRASH, "Trash / other", "not from a boss", default=cur == TRASH), Opt(NEW_BOSS, "A boss not listed…", "type its name")]

    def items_for(self, boss: str) -> list:
        from .registry import item_key

        its = [(item_key(it.id, it.name), it) for it in self.reg.items().values() if (it.boss or "?") == boss or (boss == TRASH and it.boss in ("", "?"))]
        return sorted(its, key=lambda kv: kv[1].name.lower())

    async def start(self, interaction: discord.Interaction) -> None:
        s = self.session
        if s is None:
            await self.finish(interaction, ENDED)
            return
        items = [self.select("Which boss?", self.bosses(), self.boss_picked)]
        if any(s.drop_state(d) != "awarded" for d in s.drop_list):
            items.append(self.button("Un-tick…", self.untick))
        items.append(self.button("Cancel", self.cancel))
        await self.show(interaction, "What dropped? Pick the boss first.", *items)

    async def boss_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        v = values[0] if values else TRASH
        if v == NEW_BOSS:
            await self.open_form(interaction, Form("A boss not listed", [Field("boss", "The boss's name", max_length=80, placeholder="as it shows in game")], self.boss_named))
            return
        self.draft.update(boss=v, page=0)
        await self.items_screen(interaction)

    async def boss_named(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        name = (v.get("boss") or "").strip()[:80]
        if not name:
            await self.refuse(interaction, "Name the boss.", self.start)
            return
        self.draft.update(boss=name, page=0)
        await self.items_screen(interaction)

    async def items_screen(self, interaction: discord.Interaction) -> None:
        from .raid_views import boss_name

        boss = self.draft["boss"]
        its = self.items_for(boss)
        if not its:
            await self.show(interaction, f"**{boss_name(boss)}**: no loot table yet — name the item.",
                            self.button("Not on the table…", self.not_listed, discord.ButtonStyle.primary), self.button("Back", self.start))
            return
        pages = (len(its) - 1) // self.PAGE + 1
        page = min(self.draft["page"], pages - 1)
        chunk = its[page * self.PAGE:(page + 1) * self.PAGE]
        opts = [Opt(k, it.name, f"{it.slot.replace('_', ' ')} · {it.type}" if it.slot != "unknown" else "untiered" if not it.tiers else None) for k, it in chunk]
        items = [self.select(f"What dropped from {boss_name(boss)}?", opts, self.items_picked, max_values=len(opts)),
                 self.button("Not on the table…", self.not_listed)]
        if pages > 1:
            items += [self.button("◀ Prev", self.prev, disabled=page == 0), self.button("Next ▶", self.next, disabled=page >= pages - 1)]
        items.append(self.button("Back", self.start))
        await self.show(interaction, f"**{boss_name(boss)}**" + (f" · page {page + 1} of {pages}" if pages > 1 else "") + " — pick what dropped (several at once is fine).", *items)

    async def prev(self, interaction: discord.Interaction) -> None:
        self.draft["page"] = max(0, self.draft["page"] - 1)
        await self.items_screen(interaction)

    async def next(self, interaction: discord.Interaction) -> None:
        self.draft["page"] += 1
        await self.items_screen(interaction)

    async def items_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        items = self.reg.items()
        self.draft["items"] = [{"key": k, "name": items[k].name if k in items else k} for k in values]
        self.draft["copies"] = 1
        await self.confirm_screen(interaction)

    async def not_listed(self, interaction: discord.Interaction) -> None:
        await self.open_form(interaction, Form("Not on the table", [
            Field("name", "The item's name", max_length=100, placeholder="as it shows in game", description="recorded by name; tier it later on Loot › Items"),
            Field("copies", "How many dropped?", COPIES),
        ], self.named))

    async def named(self, interaction: discord.Interaction, v: dict[str, str]) -> None:
        name = (v.get("name") or "").strip()[:100]
        if not name:
            await self.refuse(interaction, "Name the item.", self.items_screen)
            return
        self.draft["items"] = [{"key": None, "name": name}]
        self.draft["copies"] = int(v.get("copies") or 1)
        await self.confirm_screen(interaction)

    async def confirm_screen(self, interaction: discord.Interaction) -> None:
        from .raid_views import boss_name

        names = ", ".join(f"**{x['name']}**" for x in self.draft["items"])
        n = self.draft["copies"]
        opts = [Opt(o.value, o.label, default=o.value == str(n)) for o in COPIES]
        await self.show(interaction, f"Tick {names} from **{boss_name(self.draft['boss'])}**" + (f" — {n} of each" if n > 1 else "") + "?",
                        self.select("How many of each?", opts, self.copies_picked),
                        self.button("Tick", self.save, discord.ButtonStyle.success), self.button("Back", self.items_screen))

    async def copies_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["copies"] = int(values[0]) if values else 1
        await self.confirm_screen(interaction)

    async def save(self, interaction: discord.Interaction) -> None:
        s = self.session
        if s is None:
            await self.finish(interaction, ENDED)
            return
        boss = self.draft["boss"]
        add = []
        for x in self.draft["items"]:
            k = x.get("key")
            row = {"item_id": int(k)} if k and str(k).isdigit() else {"name": x["name"]}
            add.append({**row, "boss": boss if boss != TRASH else "?", "copies": self.draft["copies"]})
        await self.working(interaction, "Ticking…")  # answered before the write: Discord's 3 seconds
        try:
            res = await self.bot.tick(self.reg, s, add=add, by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await self.refuse(interaction, _err(e), self.confirm_screen)
            return
        from .raid_views import boss_name

        got = ", ".join(d.item_name + ("" if d.tiered else " 🆕") for d in res["added"])
        await self.show(interaction, f"📥 Ticked {got}." + (" Distribute when the council is ready." if not self.bot.loot_auto_on(self.reg, s) else ""),
                        self.button(f"More from {boss_name(boss)}"[:80], self.items_screen, discord.ButtonStyle.primary),
                        self.button("Another boss", self.start), self.button("Done", self.done))

    async def done(self, interaction: discord.Interaction) -> None:
        await self.finish(interaction, "Done — the council card shows the drops.")

    async def untick(self, interaction: discord.Interaction) -> None:
        from .raid_views import DROP_MARK, boss_name

        s = self.session
        rows = [d for d in s.drop_list if s.drop_state(d) != "awarded"][-MAX_OPTIONS:]
        if not rows:
            await self.refuse(interaction, "Nothing to un-tick — every drop is recorded (use Correct… for a recorded one).", self.start)
            return
        opts = [Opt(d.drop_id, f"{d.item_name}", f"{DROP_MARK[s.drop_state(d)]} {boss_name(d.boss)} · {s.drop_state(d)}") for d in rows]
        await self.show(interaction, "Un-tick which? A proposed drop loses its proposal; a recorded one can't be un-ticked (use Correct…).",
                        self.select("Un-tick…", opts, self.untick_picked, max_values=len(opts)), self.button("Back", self.start))

    async def untick_picked(self, interaction: discord.Interaction, values: list[str]) -> None:
        self.draft["remove"] = list(values)
        s = self.session
        names = ", ".join(f"**{_drop_name(s, d)}**" for d in values)
        await self.show(interaction, f"Un-tick {names}?", self.button("Un-tick", self.untick_save, discord.ButtonStyle.danger), self.button("Back", self.untick))

    async def untick_save(self, interaction: discord.Interaction) -> None:
        s = self.session
        if s is None:
            await self.finish(interaction, ENDED)
            return
        await self.working(interaction, "Un-ticking…")
        try:
            res = await self.bot.tick(self.reg, s, remove=self.draft.get("remove") or [], by=interaction.user.display_name, by_id=interaction.user.id)
        except RegistryError as e:
            await self.refuse(interaction, _err(e), self.untick)
            return
        await self.show(interaction, ("↩ Un-ticked " + ", ".join(d.item_name for d in res["removed"]) + ".") if res["removed"] else "↩ Nothing was un-ticked.",
                        self.button("Tick drops", self.start, discord.ButtonStyle.primary), self.button("Done", self.done))


@flow("loot_tick")
async def open_loot_tick(interaction: discord.Interaction, reg: Registry, *, sid: str, **_seed) -> None:
    bot = interaction.client
    session = _target(bot, reg, sid)
    if session is None:
        await say(interaction, ENDED)
        return
    await TickWizard(bot, reg, session, interaction.user.id).start(interaction)


# ---------------------------------------------------------------- Distribute

async def distribute_press(interaction: discord.Interaction, bot, reg: Registry, session) -> None:
    """The card's Distribute and /raid distribute: the presser hears how long; the card's status line is the ticker;
    the proposals arrive on cards in the thread. A second press while it judges: "Distributing already (Jonny, …)"."""
    und = session.undistributed()
    if not und:
        await say(interaction, "Nothing to distribute — tick what dropped first (every ticked drop is proposed or recorded).")
        return
    secs = min(60, max(10, 5 * len(und)))
    text = f"⚖️ Judging {len(und)} drop(s) — about {secs} s. The proposals appear on cards in the council's thread."
    if interaction.response.is_done():
        await interaction.edit_original_response(content=text, view=None)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    try:
        res = await bot.distribute(reg, session, by=interaction.user.display_name, by_id=interaction.user.id)
    except LootBusy as e:
        await interaction.edit_original_response(content=str(e))
        return
    except RegistryError as e:
        await interaction.edit_original_response(content=_err(e))
        return
    out = f"⚖️ {len(res['proposals'])} proposal(s) — on the cards in the thread." + (" (scores only)" if res.get("scores_only") else "")
    if res["stale"]:
        out += f" {len(res['stale'])} drop(s) changed while judging — press Distribute again."
    await interaction.edit_original_response(content=out)


@flow("loot_distribute")
async def open_loot_distribute(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """/raid distribute: the live council with drops nobody judged (a picker when several do)."""
    bot = interaction.client
    if not await is_loot_officer(bot, reg, interaction.user):
        await say(interaction, "Officers only.")
        return
    todo = [s for s in bot.loot_sessions(reg) if s.origin == "raid" and s.undistributed()]
    if not todo:
        await say(interaction, "Nothing to distribute — tick what dropped first (Tick drops… on the council card).")
        return
    if len(todo) == 1:
        await distribute_press(interaction, bot, reg, todo[0])
        return
    from .raid_views import loot_label, raid_name

    wiz = loot_wizard(bot, reg, interaction, "Distribute")
    opts = []
    for s in todo[:MAX_OPTIONS]:
        _, ev = bot.loot_run(s)
        name = raid_name(reg, ev) if ev is not None else s.instance
        opts.append(Opt(s.id, f"{loot_label(s)}{name} · {reg.local12(ev.start) if ev is not None else when_label(reg, s.date)}", f"{len(s.undistributed())} to judge"))

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        s = _target(bot, reg, values[0])
        if s is None:
            await wiz.finish(i, ENDED)
            return
        await wiz.working(i, "Distributing…")
        await distribute_press(i, bot, reg, s)

    await wiz.show(interaction, "Which council?", wiz.select("Pick the council", opts, picked), wiz.button("Cancel", wiz.cancel))


# ---------------------------------------------------------------- End

async def end_decide(interaction: discord.Interaction, bot, reg: Registry, rs, ev, wiz: Wizard | None = None) -> None:
    """End the run and its council: the decide screen with LootMixin.end_consequence ('End <run>: 6 awards, 1
    override, 2 drops never distributed…'); End archives every council of the run, posts the summary, marks it done."""
    from .raid_views import raid_name

    wiz = wiz or loot_wizard(bot, reg, interaction, f"End {raid_name(reg, ev)}")
    key = ev.key

    async def do(i: discord.Interaction, _note: str | None) -> None:
        cur = rs.events.get(key)
        if cur is None or cur.state in ("done", "cancelled"):
            await wiz.finish(i, "That run already closed — nothing was changed.")
            return
        try:
            text = await bot.end_run(reg, rs, cur, by=i.user.display_name, by_id=i.user.id)
        except RegistryError as e:
            await wiz.finish(i, _err(e))
            return
        await wiz.finish(i, text[:1800])

    text = bot.end_consequence(reg, rs, ev)  # THE End sentence (council + run, consequences.consequence("end")), as the lock card's End
    await wiz.decide(interaction, text, do=do, do_label="End", danger=True, back_label="Not yet")


@flow("loot_end")
async def open_loot_end(interaction: discord.Interaction, reg: Registry, *, key: str | None = None, **_seed) -> None:
    """/raid end: the locked run (the one with a council first; a picker when several), then the decide screen."""
    from .flows_raid import with_run

    bot = interaction.client
    if not await is_loot_officer(bot, reg, interaction.user):
        await say(interaction, "Officers only.")
        return

    def has_session(ev) -> bool:
        return any(s.guild == reg.key and s.run_key == ev.key for s in bot.events.values())

    async def body(i: discord.Interaction, wiz: Wizard, rs, ev) -> None:
        if ev is None or ev.state != "locked":
            await wiz.finish(i, "No locked run to end.")
            return
        await end_decide(i, bot, reg, rs, ev, wiz=wiz)

    if key:
        rs = bot.raids.store(reg)
        await body(interaction, loot_wizard(bot, reg, interaction, "End the run"), rs, rs.events.get(key))
        return
    await with_run(interaction, reg, title="End the run", then=body, states=("locked",), officer=True, prefer=has_session)


# ---------------------------------------------------------------- /gm precedents → Retire

@flow("gm_precedents")
async def open_gm_precedents(interaction: discord.Interaction, reg: Registry, **_seed) -> None:
    """The last 25 active precedents (council overrides with their reasons) → one → Retire (a reason): the bot stops
    citing it. Append-only: a status row, never a deletion."""
    bot = interaction.client
    if not await is_loot_officer(bot, reg, interaction.user):
        await say(interaction, "Officers only.")
        return
    wiz = loot_wizard(bot, reg, interaction, "Precedents")
    rows = [p for p in bot.loot_precedents(reg) if p.get("import_id")][-MAX_OPTIONS:][::-1]
    if not rows:
        await wiz.finish(interaction, "No active precedents yet — one is written when the council confirms an override, or gives the reason for a companion award.")
        return
    by_id = {p["import_id"]: p for p in rows}

    def line(p: dict) -> str:
        return f"**{p.get('item')}**: bot picked {p.get('bot')}, council gave {p.get('human')} — “{p.get('reason')}” · {p.get('by', '?')}, {when_label(reg, p.get('date'))}"

    async def picked(i: discord.Interaction, values: list[str]) -> None:
        p = by_id.get(values[0])
        if p is None:
            await wiz.finish(i, "That precedent is gone.")
            return
        wiz.draft["pick"] = p["import_id"]
        await wiz.show(i, line(p) + "\n-# Retiring keeps it on record; the bot stops citing it.", wiz.button("Retire…", retire, discord.ButtonStyle.danger),
                       wiz.button("Back", start))

    async def retire(i: discord.Interaction) -> None:
        await wiz.open_form(i, Form("Retire a precedent", [Field("reason", "Why retire it?", paragraph=True, min_length=ls.REASON_MIN, max_length=ls.REASON_MAX,
                                                                   description="kept with the record")], submitted))

    async def submitted(si: discord.Interaction, v: dict[str, str]) -> None:
        why = await wiz.guard(si) if wiz.guard else None
        if why:
            await wiz.finish(si, why)
            return
        try:
            text = await bot.retire_precedent(reg, wiz.draft["pick"], v.get("reason") or "", by=si.user.display_name, by_id=si.user.id)
        except RegistryError as e:
            await wiz.refuse(si, _err(e), start)
            return
        await wiz.finish(si, text)

    async def start(i: discord.Interaction) -> None:
        opts = [Opt(p["import_id"], f"{p.get('item')}: {p.get('bot')} → {p.get('human')}"[:100], f"“{p.get('reason')}”"[:100]) for p in rows]
        await wiz.show(i, "Pick a precedent to read or retire (newest first).", wiz.select("Pick a precedent", opts, picked), wiz.button("Cancel", wiz.cancel))

    await start(interaction)


__all__ = ["loot_press", "loot_pick", "loot_modal_submit", "loot_modal_raw", "LootModal", "reason_modal", "distribute_press", "end_decide",
           "recipient_opts", "TickWizard"]
