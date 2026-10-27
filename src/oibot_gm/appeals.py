"""Loot appeals (L32; dual interfaces phase 4 — design §5.49): a member asks the council about one of THEIR OWN awards.

- **One appeal per award.** The appeal's id IS the award's id (`award_id`: a short hash of lootctx.award_ref; two
  identical imported rows without an import_id are told apart by their place in the ledger — `ledger_ids`), stored as
  `<guild>/appeals/<id>.yaml` in the data repo (file per entity, commit per change — git log is the audit trail). A
  second question about the same award is refused, open or resolved; an award that was corrected later keeps its appeal.
- **The member's words are data.** Prose from `/me loot-ask`'s modal or Me › My loot › *Ask the council* is validated
  (length) and never parsed; it is shown as a quote under "<member> wrote:" — Discord markdown and mentions escaped, the
  card sent with no mentions allowed. **No LLM anywhere in this flow**: no model reads, routes or answers an appeal.
- **Where it goes.** A card in the officer channel (`RaidMixin.officer_channel`: the roster channel, else the ops channel —
  the council's own rule, D1) with a PUBLIC thread off it named "loot · <member> · <item>" for the officers to talk it
  over. The card carries only what the public loot log already shows (character, item, date, boss, MS/OS) plus the words:
  scores, candidates, the bot's pick, reasons and who recorded the award (D2: officer data) are never in the card or the
  thread — an officer gets them from the card's *Details* button (ephemeral) or on Loot › Appeals. No officer channel
  set: the appeal is still recorded and waits on Loot › Appeals (one ops warn).
- **Closing it.** An officer resolves it with ONE outcome from a fixed choice (`OUTCOMES`) and an optional note that stays
  with the officers. The member hears it as a NOTICE (kind `appeal`, Me page and `/me view`) — never a DM (the DM rule:
  nothing for them to act on) — and the card is edited to its final state, the thread archived.
- **Who.** Only the member opens an appeal, about their own award (an officer who disagrees with an award corrects it —
  this is the member's voice, not an officer's lever; MCP's token is the owner's, so MCP never opens one). Only officers
  resolve, re-checked live on every press; from MCP resolving is a decision about a person and needs X-Oibot-Actor (D8).
  The verbs below never check permissions; their callers do (§2.2)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
import yaml
from pydantic import BaseModel

from . import ops as ops_mod
from .lootctx import award_ref, fold_ledger
from .registry import Registry, RegistryError

log = logging.getLogger(__name__)

APPEALS_DIR = "appeals"
TEXT_MIN, TEXT_MAX = 10, 1000  # the member's words (validated, never parsed)
NOTE_MAX = 300  # the resolving officer's note (officers only)
# the closed state's one outcome line — what the member reads on their Me page (the Choice an officer picks)
OUTCOMES: dict[str, str] = {
    "stands": "The award stands.",
    "corrected": "The award was corrected — the loot log shows the change.",
    "noted": "The award stands; the council noted your point for the next drop.",
    "withdrawn": "Closed at your request.",
}
OUTCOME_LABELS: dict[str, str] = {"stands": "The award stands", "corrected": "Corrected", "noted": "Noted for next time", "withdrawn": "Withdrawn"}
NOT_YOURS = "That award isn't one of yours — you can ask about your own loot only (My loot on the Me page, or /me view)."
FROM_MCP = "A loot question is a member's own words: they ask from /me loot-ask or the Me page. MCP can read and resolve them."
# fn(guild_key, appeal_id) after every appeal save (open, card posted, resolved): the web's `loot:appeals` topic
APPEAL_LISTENERS: list = []
_LOCKS: dict[str, threading.Lock] = {}


class AppealError(RegistryError):
    """A refusal a person reads as written (the same family as every verb's RegistryError)."""


class Appeal(BaseModel):
    id: str  # = award_id(the award): one appeal per award
    award_ref: str  # lootctx.award_ref of the award (its import_id, else raider|item|received)
    member_id: int
    member_name: str
    character: str
    item: str
    item_id: int = 0
    boss: Optional[str] = None
    instance: Optional[str] = None
    received: str  # ISO date of the award
    offspec: bool = False
    text: str  # the member's words, as written (data — never parsed, never read by a model)
    state: str = "open"  # open | resolved
    outcome: Optional[str] = None  # an OUTCOMES key
    note: Optional[str] = None  # the resolving officer's note (officers only)
    created_at: str
    via: str = "discord"
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_by_id: Optional[int] = None
    channel_id: Optional[int] = None  # the card in the officer channel, and its thread
    message_id: Optional[int] = None
    thread_id: Optional[int] = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def award_id(a, n: int = 0) -> str:
    """The id an award is asked about by (URLs, button ids, the appeal file): 12 hex characters of its award_ref. `n` =
    how many earlier ledger rows WITHOUT an import_id share that award_ref ('raider|item|received': two identical
    imported rows) — the n-th repeat hashes '<ref>#<n>' so each has its own appeal. An award with an import_id is unique
    already. Callers with a ledger take the id from `ledger_ids`, never award_id(a) alone."""
    ref = award_ref(a)
    if n and not getattr(a, "import_id", None):
        ref = f"{ref}#{n}"
    return hashlib.sha1(ref.encode()).hexdigest()[:12]


def ledger_ids(awards) -> dict[int, str]:
    """id(award object) → its appeal id, over a LEDGER-ORDERED list (RegistryLootContext.ledger, fold_ledger's output —
    the file is append-only and rows without an import_id are never voided, so their places agree between the two)."""
    seen: Counter = Counter()
    out: dict[int, str] = {}
    for a in awards:
        ref = award_ref(a)
        out[id(a)] = award_id(a, 0 if a.import_id else seen[ref])
        if not a.import_id:
            seen[ref] += 1
    return out


class AppealStore:
    def __init__(self, store, guild_key: str):
        self.store, self.key = store, guild_key
        self.dir = Path(guild_key) / APPEALS_DIR

    def path(self, aid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", aid or ""):
            raise AppealError(f"No loot question {aid!r}.")
        return self.dir / f"{aid}.yaml"

    def get(self, aid: str) -> Appeal | None:
        p = self.store.root / self.path(aid)
        return Appeal.model_validate(yaml.safe_load(p.read_text())) if p.exists() else None

    def all(self) -> list[Appeal]:
        d = self.store.root / self.dir
        return [Appeal.model_validate(yaml.safe_load(f.read_text())) for f in sorted(d.glob("*.yaml"))] if d.exists() else []

    def of_member(self, uid: int) -> list[Appeal]:
        return [a for a in self.all() if a.member_id == int(uid)]

    def save(self, ap: Appeal, message: str) -> Appeal:
        self.store.write_text(self.path(ap.id), yaml.safe_dump(ap.model_dump(), sort_keys=False, allow_unicode=True))
        self.store.commit(f"{self.key}: loot question {ap.id}: {message}")
        for fn in list(APPEAL_LISTENERS):  # every save publishes, whichever surface moved it (§2.4)
            try:
                fn(self.key, ap.id)
            except Exception:  # noqa: BLE001 — a listener never breaks a save
                log.debug("appeal listener failed", exc_info=True)
        return ap


def store_of(reg: Registry) -> AppealStore:
    return AppealStore(reg.store, reg.key)


def _lock(reg: Registry) -> threading.Lock:
    return _LOCKS.setdefault(reg.key, threading.Lock())


# ---------------------------------------------------------------- words (one place; every surface prints these)

def check_text(text) -> str:
    """The member's words, validated (never parsed): trimmed, TEXT_MIN..TEXT_MAX characters."""
    t = str(text or "").strip()
    if len(t) < TEXT_MIN:
        raise AppealError(f"Say a little more — at least {TEXT_MIN} characters, so the officers know what you're asking.")
    if len(t) > TEXT_MAX:
        raise AppealError(f"That's {len(t)} characters — keep it to {TEXT_MAX}.")
    return t


def check_outcome(outcome) -> str:
    o = str(outcome or "").strip().lower()
    if o not in OUTCOMES:
        raise AppealError("Pick one outcome: " + ", ".join(f"{k} ({v})" for k, v in OUTCOME_LABELS.items()) + ".")
    return o


def award_words(reg: Registry, ap: Appeal | None = None, *, character=None, item=None, received=None, boss=None, offspec=None) -> str:
    """'Kestrel · Robe of Tests · Tue 6 Oct · Gatekeeper · MS' — exactly what the public loot log shows, nothing more."""
    if ap is not None:
        character, item, received, boss, offspec = ap.character, ap.item, ap.received, ap.boss, ap.offspec
    parts = [str(character), str(item), reg.day_label(str(received)[:10])] + ([str(boss)] if boss else []) + ["OS" if offspec else "MS"]
    return " · ".join(parts)


def open_consequence(reg: Registry, award) -> str:
    """What sending the question does (the wizard's review screen and the web's Confirm print exactly this)."""
    name = award.item_name or f"item {award.item_id}"
    where = "in a thread in the officers' channel" if (reg.config.roster_channel_id or reg.config.ops_channel_id) else "on the officers' Loot page"
    return (f"Your words about {name} go to the officers as written, {where}. "
            "Their answer comes back as a notice on your Me page and in /me view. "
            "You get one question per award, and it can't be edited once sent.")


def resolve_consequence(reg: Registry, ap: Appeal, outcome: str) -> str:
    return (f"{ap.member_name}'s question about {ap.item} closes, and the card and thread close with it. "
            f"They see “{OUTCOME_LABELS[outcome]}” on their Me page. Your note stays with the officers.")


def notice_text(ap: Appeal) -> str:
    return f"The council answered your question about {ap.item} ({ap.character}): {OUTCOMES.get(ap.outcome or '', '')}"


def state_words(reg: Registry, ap: Appeal) -> str:
    """For the member: 'asked Tue 6 Oct 7:30 PM — the officers have it' / 'answered: The award stands.'"""
    if ap.state == "resolved":
        return f"answered {reg.local12(ap.resolved_at)}: {OUTCOMES.get(ap.outcome or '', '')}" if ap.resolved_at else f"answered: {OUTCOMES.get(ap.outcome or '', '')}"
    return f"asked {reg.local12(ap.created_at)} — the officers have it"


def thread_close_line(ap: Appeal) -> str:
    """The thread's last line: who resolved it, the outcome and the officer's note — the note and the name are people's
    text, so they are escaped (markdown, mentions) and can't open or close a fence (llm.provider.unfence, the same
    neutraliser the prompts use), on one line."""
    from .llm.provider import unfence

    def safe(t) -> str:
        return discord.utils.escape_mentions(discord.utils.escape_markdown(unfence(" ".join(str(t or "").split()), "data", "member-text")))
    return f"✅ Resolved by {safe(ap.resolved_by) or 'an officer'}: {OUTCOME_LABELS.get(ap.outcome or '', ap.outcome)}" + (f" — {safe(ap.note)}" if ap.note else "")


def thread_name(ap: Appeal) -> str:
    return f"loot · {ap.member_name} · {ap.item}"[:100]


def quote(name: str, text: str) -> str:
    """The member's words as a Discord quote block under '<name> wrote:' — markdown and mentions escaped (data, §2)."""
    body = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return f"**{discord.utils.escape_markdown(name)} wrote:**\n" + "\n".join("> " + line if line.strip() else ">" for line in body.splitlines())


# ---------------------------------------------------------------- reads

def member_awards_all(reg: Registry, ctx, m) -> list:
    from .loot_ops import member_awards

    return member_awards(reg, ctx, m, 100_000)


def own_awards(reg: Registry, ctx, m) -> list[tuple[str, object]]:
    """(appeal id, award) for each of the member's live awards, newest first — ids over the whole ledger (ledger_ids)."""
    ids = ledger_ids(ctx.ledger)
    return [(ids[id(a)], a) for a in member_awards_all(reg, ctx, m)]


def find_award(reg: Registry, ctx, uid: int, aid: str):
    """One of the member's own live awards by id; anything else (someone else's, voided, unknown) is NOT_YOURS."""
    m = reg.members.get(int(uid))
    if m is None:
        raise AppealError("Nothing registered yet — /register to start.")
    hit = next((a for i, a in own_awards(reg, ctx, m) if i == aid), None)
    if hit is None:
        raise AppealError(NOT_YOURS)
    return hit


def all_awards(reg: Registry) -> dict:
    """Every ledger award by appeal id, voided ones too (an appeal can outlive a correction) — read once per listing."""
    awards, _ = fold_ledger(reg.store.read_jsonl(Path(reg.key) / "ledger.jsonl"))
    ids = ledger_ids(awards)
    return {ids[id(a)]: a for a in awards}


def officer_details(reg: Registry, ctx, ap: Appeal, awards: dict | None = None) -> dict:
    """The council's record of the award (D2: officers only — Loot › Appeals and the card's Details button): how it was
    decided, the bot's pick, who recorded it, tier and weight, the candidates at the time and the precedent's reason.
    `awards`: all_awards(reg) when the caller lists several."""
    a = (awards if awards is not None else all_awards(reg)).get(ap.id)
    if a is None:
        return {"found": False}
    prec = ctx.precedent_for(a.import_id) if (ctx is not None and a.import_id) else None
    return {"found": True, "void": bool(a.void), "source": a.source, "bot": a.bot_pick, "by": a.by, "tier": a.tier, "weight": a.total_weight,
            "confirmed_label": reg.local12(a.confirmed_at) if a.confirmed_at else "", "candidates": list(a.candidates_snapshot)[:8],
            "reason": (prec or {}).get("reason")}


def details_text(reg: Registry, ctx, ap: Appeal) -> str:
    """officer_details in words for Discord's ephemeral Details reply."""
    d = officer_details(reg, ctx, ap)
    lines = [f"**{ap.item}** → {ap.character} ({award_words(reg, ap)})"]
    if not d["found"]:
        return "\n".join(lines + ["The ledger has no record of this award any more."])
    if d["void"]:
        lines.append("⚠ This award was corrected after the question was asked.")
    how = {"bot": "the bot's pick, confirmed", "override": "an override", "companion": "recorded by the companion", "manual": "recorded by hand",
           "correct": "a correction"}.get(d["source"] or "", d["source"] or "?")
    lines.append(f"How: {how}" + (f" · recorded by {d['by']}" if d["by"] else "") + (f" · {d['confirmed_label']}" if d["confirmed_label"] else ""))
    if d["bot"] and d["bot"] != ap.character:
        lines.append(f"The bot picked: {d['bot']}")
    lines.append(f"Tier {d['tier']} · weight {d['weight']:g}")
    if d["reason"]:
        lines.append(f"Reason on record: {d['reason']}")
    if d["candidates"]:
        lines.append("Candidates then: " + ", ".join(f"{c.get('character')} ({c.get('tier', '?')}" + (f", {c['base']:.2f})" if isinstance(c.get("base"), (int, float)) else ")")
                                                      for c in d["candidates"]))
    return "\n".join(lines)[:1900]


# ---------------------------------------------------------------- writes (each on a worker thread: a git commit)

def record_open(reg: Registry, m, award, text: str, *, via: str, aid: str | None = None) -> Appeal:
    """Store a new appeal — refused when the award already has one (open or resolved), under the guild's appeal lock.
    `aid` = the award's id from ledger_ids (find_award's caller has it); award_id(award) only for a unique award."""
    st = store_of(reg)
    aid = aid or award_id(award)
    with _lock(reg):
        old = st.get(aid)
        if old is not None:
            raise AppealError(f"You've already asked about this award ({state_words(reg, old)}). One question per award.")
        c = next((c for c in m.characters if c.label.lower() == award.raider.lower()), None)
        ap = Appeal(id=aid, award_ref=award_ref(award), member_id=m.discord_id, member_name=m.display_name, character=c.label if c else award.raider,
                    item=award.item_name or f"item {award.item_id}", item_id=award.item_id or 0, boss=award.boss or None, instance=award.instance or None,
                    received=award.received.isoformat(), offspec=bool(award.offspec), text=text, created_at=now(), via=via or "discord")
        return st.save(ap, f"asked by {m.display_name} ({ap.item})")


def record_resolve(reg: Registry, aid: str, outcome: str, note: str | None, by: str, by_id: int | None) -> Appeal:
    """Close an open appeal with one outcome and the member's notice, in one commit."""
    st = store_of(reg)
    with _lock(reg):
        ap = st.get(aid)
        if ap is None:
            raise AppealError(f"No loot question {aid}.")
        if ap.state != "open":
            raise AppealError(f"Already resolved by {ap.resolved_by or 'an officer'}" + (f" at {reg.local12(ap.resolved_at)}" if ap.resolved_at else "")
                              + f": {OUTCOME_LABELS.get(ap.outcome or '', ap.outcome)}.")
        ap.state, ap.outcome, ap.note = "resolved", outcome, (note or "").strip()[:NOTE_MAX] or None
        ap.resolved_at, ap.resolved_by, ap.resolved_by_id = now(), by, by_id
        with reg.store.batch(f"{reg.key}: loot question {ap.id} resolved ({outcome}) by {by}"):
            st.save(ap, f"resolved ({outcome}) by {by}")
            reg.notify(ap.member_id, "appeal", notice_text(ap))  # D26: an outcome to KNOW — never a DM
        return ap


def save_card(reg: Registry, aid: str, channel_id: int, message_id: int, thread_id: int | None) -> Appeal | None:
    st = store_of(reg)
    with _lock(reg):
        ap = st.get(aid)
        if ap is None:
            return None
        ap.channel_id, ap.message_id, ap.thread_id = channel_id, message_id, thread_id
        return st.save(ap, "card posted")


# ---------------------------------------------------------------- the verbs (one verb, every surface a caller — §2.2)

async def open_appeal(bot, reg: Registry, uid: int, aid: str, text, *, by: str, by_id: int | None, via: str = "discord") -> tuple[Appeal, str]:
    """The member `uid` asks about their award `aid`: validated, stored, the card + thread posted in the officer channel,
    one ops line. The caller decided it is the member themself (never an officer for them, never MCP). Returns the appeal
    and the member's receipt."""
    from .loot_ops import guild_ctx

    words = check_text(text)
    m = reg.members.get(int(uid))
    if m is None:
        raise AppealError("Nothing registered yet — /register to start.")
    award = find_award(reg, guild_ctx(bot, reg), uid, aid)
    ap = await asyncio.to_thread(record_open, reg, m, award, words, via=via, aid=aid)
    posted = await post_card(bot, reg, ap)
    await ops_mod.audited(bot.ops, reg, by, f"loot question from {ap.member_name} about {ap.item} ({ap.character})" + ("" if posted else " — no officer channel: it waits on Loot › Appeals"),
                          via=via, by_id=by_id)
    return ap, "Sent to the council. Their answer will show on your Me page and in /me view (no DM)."


async def resolve_appeal(bot, reg: Registry, aid: str, outcome, *, note: str | None = None, by: str, by_id: int | None, via: str = "discord") -> tuple[Appeal, str]:
    """An officer closes appeal `aid` with one outcome: stored with the member's notice, the card edited to its final
    state and the thread archived, one ops line. The caller checked officer rights."""
    o = check_outcome(outcome)
    if note is not None and len(str(note)) > NOTE_MAX:
        raise AppealError(f"Keep the note to {NOTE_MAX} characters.")
    ap = await asyncio.to_thread(record_resolve, reg, aid, o, note, by, by_id)
    await update_card(bot, reg, ap)
    await ops_mod.audited(bot.ops, reg, by, f"loot question from {ap.member_name} about {ap.item} resolved: {OUTCOME_LABELS[o]}", via=via, by_id=by_id)
    return ap, f"Resolved: {OUTCOME_LABELS[o]}. {ap.member_name} sees it on their Me page."


# ---------------------------------------------------------------- the card (Discord)

def card_embed(reg: Registry, ap: Appeal) -> discord.Embed:
    """The officer-channel card: the award as the public loot log shows it, the member's words as a quote, the state.
    Nothing officer-only (scores, candidates, reasons, who recorded it) — that is the Details button's."""
    done = ap.state == "resolved"
    e = discord.Embed(title=f"Loot question · {ap.item}"[:256], colour=0x6B7785 if done else 0x38B2A0)
    e.description = (award_words(reg, ap) + "\n\n" + quote(ap.member_name, ap.text))[:4000]
    if done:
        e.add_field(name="Resolved", value=f"{OUTCOME_LABELS.get(ap.outcome or '', ap.outcome)} · by {ap.resolved_by or 'an officer'} · {reg.local12(ap.resolved_at) if ap.resolved_at else ''}"[:1000],
                    inline=False)
        e.set_footer(text="The member sees the outcome on their Me page. Nobody was DMed.")
    else:
        e.set_footer(text="Officers: Details shows the council's record (only to you). Resolve closes it and tells the member on their Me page — no DM.")
    return e


def card_view(reg: Registry, ap: Appeal) -> discord.ui.View | None:
    if ap.state != "open":
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(AppealButton(reg.key, "details", ap.id))
    v.add_item(AppealButton(reg.key, "resolve", ap.id))
    return v


async def post_card(bot, reg: Registry, ap: Appeal) -> bool:
    """Post the card and its public thread in the officer channel (roster, else ops); False when there is none (the
    appeal waits on Loot › Appeals — one ops warn) or Discord refused (the appeal stands; the warn says so)."""
    find = getattr(bot, "officer_channel", None)
    ch = find(reg) if find is not None else None
    if ch is None:
        await bot.ops.emit(reg.config, "warn", f"loot question from {ap.member_name} about {ap.item}: no roster or ops channel — it waits on Loot › Appeals on the site")
        return False
    try:
        msg = await ch.send(embed=card_embed(reg, ap), view=card_view(reg, ap), allowed_mentions=discord.AllowedMentions.none())
        thread = None
        try:
            thread = await msg.create_thread(name=thread_name(ap))
        except Exception:  # noqa: BLE001 — the card stands without its thread
            log.warning("appeal thread not created", exc_info=True)
        await asyncio.to_thread(save_card, reg, ap.id, ch.id, msg.id, getattr(thread, "id", None))
        ap.channel_id, ap.message_id, ap.thread_id = ch.id, msg.id, getattr(thread, "id", None)
        return True
    except Exception as e:  # noqa: BLE001 — recorded first: Discord being down never loses the question (§2.9)
        await bot.ops.emit(reg.config, "warn", f"loot question from {ap.member_name} about {ap.item}: couldn't post the card ({type(e).__name__}) — it waits on Loot › Appeals")
        return False


async def update_card(bot, reg: Registry, ap: Appeal) -> None:
    """Edit the card to its final state (no buttons), say the outcome in the thread and archive it. Best effort."""
    if not (ap.channel_id and ap.message_id):
        return
    ch = bot.get_channel(ap.channel_id) if hasattr(bot, "get_channel") else None
    if ch is None:
        return
    try:
        msg = await ch.fetch_message(ap.message_id)
        await msg.edit(embed=card_embed(reg, ap), view=None)
    except Exception:  # noqa: BLE001
        log.debug("appeal card edit failed", exc_info=True)
    th = bot.get_channel(ap.thread_id) if ap.thread_id else None
    if th is not None:
        try:
            await th.send(thread_close_line(ap),
                          allowed_mentions=discord.AllowedMentions.none())
            await th.edit(archived=True, locked=True)
        except Exception:  # noqa: BLE001
            log.debug("appeal thread close failed", exc_info=True)


class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"appeal(?:\.(?P<g>[a-z0-9_-]{1,16}))?:(?P<action>details|resolve):(?P<aid>[0-9a-f]{12})"):
    """The card's Details (the council's record, ephemeral) and Resolve… (the decide flow) — officers only, re-checked live
    on every press. The id carries the guild key (`appeal.<gk>:<action>:<appeal id>`)."""

    LABELS = {"details": ("Details", discord.ButtonStyle.secondary), "resolve": ("Resolve…", discord.ButtonStyle.primary)}

    def __init__(self, gk: str | None, action: str, aid: str):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"appeal{'.' + gk if gk else ''}:{action}:{aid}"))
        self.gk, self.action, self.aid = gk, action, aid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["g"], match["action"], match["aid"])

    async def callback(self, interaction: discord.Interaction):
        from .discord_registry import channel_card_guild

        reg = channel_card_guild(interaction, self.gk)  # a channel-only card: strict, never a DM fallback
        if reg is None or not await interaction.client.is_officer_anywhere(reg, interaction.user.id):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        if self.action == "details":
            from .loot_ops import guild_ctx

            ap = store_of(reg).get(self.aid)
            text = details_text(reg, guild_ctx(interaction.client, reg), ap) if ap else "That question is gone."
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        from .wizard_flows import FLOWS

        await FLOWS["appeal_resolve"](interaction, reg, aid=self.aid)
