"""Loot sessions: the state of one loot council (design §3.1, dual interfaces phase 1).

One `LootSession` per (guild key, run key) — per roster of a split run — stored at `<guild>/events/<id>.json` (a retired
/mock session's file, `mock-<channel>.json`, is left as written and never loaded). The unit is the DROP (decision 2): every drop has a session-scoped `drop_id` (`d1`,
`d2`, …); proposals, awards, companion matching and the ledger `import_id` (`<run>-<drop_id>`) key by it, and two
copies of one item are two drops. Code in here is pure state + scoring; the verbs that change a live session (under its
lock, with ops lines and listeners) are `discord_loot.LootMixin`.

Game truth first (decision 3): an override lives on its proposal until Confirm; the precedent is written at Confirm;
Discard drops pending overrides; a companion mismatch records the ledger row at once with the precedent pending a reason
bound to (drop_id, recipient)."""
from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from ..models import DropResult, LootAward, Player, RosterResult
from ..profiles import Item
from ..llm.provider import for_guild
from ..registry import RegistryError, item_key
from . import recommend as rec_mod, scoring

log = logging.getLogger(__name__)

SCHEMA = 2
STORE = None  # the process's GitStore (discord_bot.run sets it); a session loaded through load_sessions carries its own
LOOT_LISTENERS: list = []  # fn(guild_key, session_id, run_key) after every session save (the web's `loot:<run>` topic)
DISENCHANT = "(disenchant)"  # the recipient of a disenchanted drop (recorded, no character)
SEEN_IDEM_MAX = 500  # companion idempotency keys kept per session
CHANGES_MAX = 20  # the "what changed" ring a stale-rev refusal names
REASON_MIN, REASON_MAX = 3, 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LootChanged(RegistryError):
    """A loot write made against a batch / drop list that changed since its author looked (a stale `batch_rev`,
    `drops_rev` or `expect_to`). Nothing was written; the message names what changed."""


class LootBusy(RegistryError):
    """Single flight: a slow loot verb (distribute, open) is already running on this session."""


# ---------------------------------------------------------------- models

class Drop(BaseModel):
    drop_id: str
    item_id: int = 0  # 0: an item the tables don't know (ticked by name)
    item_name: str
    boss: str = "?"
    tiered: bool = True
    source: str = "officer"  # officer | companion | manual
    by: Optional[str] = None
    at: str = Field(default_factory=now_iso)
    held: bool = False  # "Decide later": dropped from its batch, still waiting
    raw: Optional[str] = None  # the companion's chat line (audit)
    seen_in_game: bool = False  # an in-game award (the companion's `loot` event) has been matched to this copy

    @property
    def key(self) -> str:
        return item_key(self.item_id, self.item_name)


class Override(BaseModel):
    to: str
    reason: str
    by: str
    by_id: Optional[int] = None
    at: str = Field(default_factory=now_iso)


class Proposal(BaseModel):
    drop_id: str = ""
    item_id: int = 0
    item_name: str
    boss: str
    award_to: str  # the BOT's pick (the recommendation); an override sits beside it until Confirm
    override: Optional[Override] = None
    result: DropResult
    order: int = 0  # judging order within the batch (1 = first)
    impact: float = 0.0  # weight × best eligible tier
    order_sensitive: Optional[str] = None  # who would win under kill order, if different
    batch: int = 1
    table_sig: str = ""  # the candidate table the pick was judged on (table_signature)
    tiered: bool = True
    proposed_at: str = Field(default_factory=now_iso)

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        """Pre-drop files: `source: override` with the council's pick in `award_to` and the reason beside it."""
        if isinstance(data, dict) and data.get("source") == "override" and not data.get("override"):
            data = dict(data)
            bot = ((data.get("result") or {}).get("recommendation") or {}).get("primary") or data.get("award_to")
            data["override"] = {"to": data.get("award_to"), "reason": data.get("reason") or "", "by": "?", "at": ""}
            data["award_to"] = bot
        return data

    @property
    def to(self) -> str:
        """The effective recipient: the override's, else the bot's pick."""
        return self.override.to if self.override else self.award_to

    @property
    def source(self) -> str:
        return "override" if self.override else "bot"

    @property
    def reason(self) -> Optional[str]:
        return self.override.reason if self.override else None


class LootSession(BaseModel):
    """A loot council. Its candidates are read LIVE from its run (`LootMixin.loot_roster`: ev.seated() of its roster);
    it is live iff its run is `locked` — there is no separate session state. `signups` / `roster` / `state` / the
    keep-together lists are the retired /mock flow's (D11), kept only so its old files still parse (load_sessions skips
    them: origin != raid)."""

    id: str
    run_key: Optional[str] = None  # the RaidEvent key (None for /mock)
    roster_index: Optional[int] = None  # a split run: one session per roster (None = the whole run)
    label: Optional[str] = None  # a /mock session's old display id ("BLA-0915"); "Roster N" for one roster of a split run
    channel_id: int = 0
    instance: str
    date: str
    guild: str = ""
    origin: str = "mock"  # mock | raid
    state: str = "signup_open"  # the /mock flow's: signup_open | roster_proposed | roster_locked | raid | ended
    test: bool = False  # a test-bench run's session: nothing reaches the ledger, precedents or the public log
    signups: list[Player] = Field(default_factory=list)
    roster: Optional[RosterResult] = None
    roster_log: list[str] = Field(default_factory=list)
    keep_together: list[list[str]] = Field(default_factory=list)
    keep_apart: list[list[str]] = Field(default_factory=list)
    force_out: list[str] = Field(default_factory=list)
    force_in: list[str] = Field(default_factory=list)
    raid_thread_id: Optional[int] = None
    current_boss: Optional[str] = None
    drop_list: list[Drop] = Field(default_factory=list)
    drop_seq: int = 0
    proposals: list[Proposal] = Field(default_factory=list)  # pending (not yet confirmed) picks, one per drop
    batches: list[dict] = Field(default_factory=list)  # {n, by, at, drops, boss}
    awards: list[LootAward] = Field(default_factory=list)
    overrides: list[dict] = Field(default_factory=list)  # precedents recorded this session (written at Confirm / reason)
    bosses_done: list[str] = Field(default_factory=list)
    pending_reasons: list[dict] = Field(default_factory=list)  # companion mismatches: {drop_id, item, recipient, bot, import_id, at}
    conflicts: list[dict] = Field(default_factory=list)  # in game ≠ recorded: {drop_id, item, in_game, recorded, at}
    departed: list[Player] = Field(default_factory=list)  # left the roster after open: still candidates until removed
    roster_notes: list[dict] = Field(default_factory=list)  # {at, kind: added|removed|changed, uid, name}
    seen_idem: list[str] = Field(default_factory=list)  # companion idempotency keys (bounded)
    distributing: Optional[dict] = None  # {by, by_id, since, drops}: a judge in flight (cleared on restart → interrupted)
    interrupted: Optional[dict] = None  # {by, since, drops, at}: the restart line "Distribute was interrupted at …"
    failures: dict[str, int] = Field(default_factory=dict)  # drop set → interrupted judges (2 → that batch is scores-only)
    scores_only: Optional[dict] = None  # {by, at}: the Scores-only toggle (no model call; deterministic picks)
    changes: list[dict] = Field(default_factory=list)  # {at, by, what}: what a stale-rev refusal names
    cards: dict[str, int] = Field(default_factory=dict)  # the Discord surfaces' message ids (L2)
    opened_by: Optional[str] = None
    opened_at: str = Field(default_factory=now_iso)
    last_activity: str = Field(default_factory=now_iso)
    closing_notice_at: Optional[str] = None  # the scheduler's one "closing in 15 min" line went out
    held_notice_at: Optional[str] = None  # past close and idle, but picks / drops still wait: the one "still pending" line went out
    keep_open_until: Optional[str] = None  # "Keep open": the auto-end waits until then
    schema_: int = Field(default=SCHEMA, alias="schema")
    _store = PrivateAttr(default=None)

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        """Pre-drop files (thread-keyed, item-keyed): `drops: {boss: [item ids]}` + `distributed` → drop_list with ids;
        proposals / awards / pending reasons matched to drops by item id; a /mock id becomes `mock-<channel>`; a real
        session's id is its run key (already was)."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        origin = data.get("origin", "mock")
        if origin == "raid":
            data.setdefault("run_key", data.get("id"))
        elif not str(data.get("id", "")).startswith("mock-") and data.get("channel_id") is not None:
            data.setdefault("label", data.get("id"))
            data["id"] = f"mock-{data['channel_id']}"
        legacy = data.pop("drops", None)
        distributed = list(data.pop("distributed", None) or [])
        data.pop("pending_drops", None)
        if isinstance(legacy, dict) and not data.get("drop_list"):
            names: dict[int, str] = {}
            for p in data.get("proposals") or []:
                names[int(p.get("item_id") or 0)] = p.get("item_name") or ""
            drops, seq = [], 0
            for boss, ids in legacy.items():
                for iid in ids:
                    seq += 1
                    drops.append({"drop_id": f"d{seq}", "item_id": int(iid), "item_name": names.get(int(iid)) or f"item {iid}", "boss": boss, "source": "officer", "at": ""})
            free = list(drops)

            def take(iid: int, want: Callable[[dict], bool] = lambda d: True) -> str | None:
                hit = next((d for d in free if d["item_id"] == iid and want(d)), None)
                if hit is None:
                    seq_id = f"d{len(drops) + 1}"
                    hit = {"drop_id": seq_id, "item_id": iid, "item_name": names.get(iid) or f"item {iid}", "boss": "?", "source": "manual", "at": ""}
                    drops.append(hit)
                else:
                    free.remove(hit)
                return hit["drop_id"]
            awards = []
            for a in data.get("awards") or []:
                a = dict(a)
                if not a.get("drop_id"):
                    a["drop_id"] = take(int(a.get("item_id") or 0))
                awards.append(a)
            data["awards"] = awards
            props = []
            for p in data.get("proposals") or []:
                p = dict(p)
                if not p.get("drop_id"):
                    p["drop_id"] = take(int(p.get("item_id") or 0))
                props.append(p)
            data["proposals"] = props
            for pr in data.get("pending_reasons") or []:
                if not pr.get("drop_id"):
                    hit = next((a for a in awards if int(a.get("item_id") or 0) == int(pr.get("item_id") or 0) and a.get("raider") == pr.get("human")), None)
                    pr["drop_id"] = hit["drop_id"] if hit else None
                    pr.setdefault("recipient", pr.get("human"))
            del distributed  # every awarded drop is in `awards`; nothing else to carry
            data["drop_list"] = drops
            data["drop_seq"] = len(drops)
        return data

    # ---- identity
    @property
    def key(self) -> tuple[str, str]:
        return (self.guild, self.id)

    @property
    def thread_id(self) -> Optional[int]:
        return self.raid_thread_id

    @property
    def raid_name(self) -> str:
        return self.instance.replace("_", " ").title()

    def active_signups(self) -> list[Player]:
        return [p for p in self.signups if p.status != "absent"]

    # ---- the store
    def rel_path(self) -> Path:
        return Path(self.guild) / "events" / f"{self.id}.json"

    def store(self):
        st = self._store or STORE
        if st is None:
            raise RuntimeError("loot session has no store")
        return st

    def attach(self, store) -> "LootSession":
        self._store = store
        return self

    def save(self, message: str | None = None, touch: bool = True) -> None:
        """Write and commit the session (inside a caller's GitStore.batch: one commit), then tell LOOT_LISTENERS.
        `touch` False: not activity (the scheduler's own notes don't keep a council from going idle)."""
        st = self.store()
        if touch:
            self.last_activity = now_iso()
        st.write_text(self.rel_path(), self.model_dump_json(indent=1, by_alias=True))
        st.commit(message or f"{self.guild}: loot {self.id}")
        for fn in list(LOOT_LISTENERS):
            try:
                fn(self.guild, self.id, self.run_key or self.id)
            except Exception:  # noqa: BLE001 — a listener must never break a save
                log.exception("loot listener failed")

    def archive(self, summary: dict | None = None) -> Path:
        """Move the session to events/archive/<id>-<date>.json (with its end summary) and drop the live file."""
        st = self.store()
        rel = Path(self.guild) / "events" / "archive" / f"{self.id}-{self.date}.json"
        n = 2
        while (st.root / rel).exists():
            rel = Path(self.guild) / "events" / "archive" / f"{self.id}-{self.date}-{n}.json"
            n += 1
        body = json.loads(self.model_dump_json(by_alias=True))
        if summary is not None:
            body["summary"] = summary
        st.write_text(rel, json.dumps(body, indent=1, default=str))
        live = st.root / self.rel_path()
        if live.exists():
            live.unlink()
        st.commit(f"{self.guild}: loot {self.id} archived")
        for fn in list(LOOT_LISTENERS):
            try:
                fn(self.guild, self.id, self.run_key or self.id)
            except Exception:  # noqa: BLE001
                log.exception("loot listener failed")
        return rel

    # ---- drops
    def new_drop_id(self) -> str:
        self.drop_seq = max(self.drop_seq, len(self.drop_list)) + 1
        return f"d{self.drop_seq}"

    def drop(self, drop_id: str) -> Optional[Drop]:
        return next((d for d in self.drop_list if d.drop_id == drop_id), None)

    def awarded(self, drop_id: str) -> Optional[LootAward]:
        return next((a for a in self.awards if a.drop_id == drop_id and not a.void), None)

    def pending(self, drop_id: str) -> Optional[Proposal]:
        return next((p for p in self.proposals if p.drop_id == drop_id), None)

    def drop_state(self, d: Drop) -> str:
        """awarded | proposed | ticked — the card's ✅ / 🟡 (proposed or waiting) / ▫️ (not ticked) per drop."""
        if self.awarded(d.drop_id):
            return "awarded"
        if self.pending(d.drop_id):
            return "proposed"
        return "ticked"

    def boss_order(self) -> list[str]:
        order = list(self.bosses_done)
        for d in self.drop_list:
            if d.boss not in order:
                order.append(d.boss)
        return order

    def undistributed(self, profile=None) -> list[Drop]:
        """Ticked drops neither awarded nor proposed, in boss (kill) order, then tick order."""
        order = self.boss_order()
        todo = [d for d in self.drop_list if self.drop_state(d) == "ticked"]
        return sorted(todo, key=lambda d: (order.index(d.boss) if d.boss in order else len(order), self.drop_list.index(d)))

    # ---- bookkeeping
    def remember_idem(self, idem: str | None) -> bool:
        """False when the companion already sent this event (a re-send after reconnect): ack it, do nothing."""
        if not idem:
            return True
        if idem in self.seen_idem:
            return False
        self.seen_idem = (self.seen_idem + [idem])[-SEEN_IDEM_MAX:]
        return True

    def note_change(self, by: str, what: str) -> None:
        self.changes = (self.changes + [{"at": now_iso(), "by": by, "what": what}])[-CHANGES_MAX:]

    def last_change(self) -> str:
        return self.changes[-1]["what"] if self.changes else ""

    def pending_count(self) -> dict[str, int]:
        return {"proposals": len(self.proposals), "overrides": sum(1 for p in self.proposals if p.override),
                "reasons": len(self.pending_reasons), "undistributed": len(self.undistributed()), "conflicts": len(self.conflicts)}

    def anything_pending(self) -> bool:
        c = self.pending_count()
        return bool(c["proposals"] or c["reasons"] or c["undistributed"] or c["conflicts"] or self.distributing)

    def blocks_close(self) -> bool:
        """What keeps the scheduler from ending the council past the run's close (decision 9 / D10): a pick waiting for
        Confirm (incl. a "(nobody)" pick waiting for an override), a ticked drop nobody judged, a judge in flight.
        A reason still owed and an in-game conflict do NOT block: both outlive the council (set_reason / correct take a
        run key after the end; loot_reasons_pending / loot_conflicts_pending list them) and the summary names them."""
        c = self.pending_count()
        return bool(c["proposals"] or c["undistributed"] or self.distributing)


# ---------------------------------------------------------------- revs (computed, never stored)

def _sha(blob) -> str:
    return hashlib.sha1(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:12]


def batch_rev(session: LootSession) -> str:
    """Over the picks in play: drop_id → effective recipient and source, for every pending proposal AND every award that
    came from a proposal (source bot / override). Guards Confirm-all, Discard and Override. Confirming a pick as shown
    turns (d3, Eltam, bot) pending into (d3, Eltam, bot) recorded — the rev does not move, so another officer's per-item
    Confirm never invalidates a Confirm-all; an override, a re-score that moved a pick, a discard or a new batch does."""
    rows = [[p.drop_id, p.to, p.source] for p in session.proposals]
    rows += [[a.drop_id, a.raider, a.source] for a in session.awards if not a.void and a.source in ("bot", "override")]
    return _sha(sorted(rows))


def drops_rev(session: LootSession) -> str:
    """Over the drop list and what Distribute would judge: guards Tick and Distribute."""
    todo = {d.drop_id for d in session.undistributed()}
    return _sha([[d.drop_id, d.key, d.boss, d.drop_id in todo] for d in session.drop_list])


def table_signature(cands) -> str:
    """What the model actually judged on: top candidates and their rounded scores."""
    return "|".join(f"{c.character}:{c.base_score:.2f}:{c.already_has}" for c in cands[:6])


# ---------------------------------------------------------------- items

def drop_item(ctx, d: Drop) -> Item:
    """The Item a drop is: the context's (profile + guild) item by id or name, else an untiered stand-in by name."""
    it = ctx.item(d.item_id or None, d.item_name)
    if it is not None:
        return it
    return Item(id=int(d.item_id or 0), name=d.item_name, slot="unknown", type="unknown", boss=d.boss, tiers={})


# ---------------------------------------------------------------- distribution (pure: a snapshot in, proposals out)

def _award(item: Item, cands, who: str, raid_date: date, instance: str, boss: str, drop_id: str) -> Optional[LootAward]:
    c = next((c for c in cands if c.character == who), None)
    if not c:
        return None
    return LootAward(raider=who, item_id=item.id, item_name=item.name, tier=c.tier, total_weight=c.upgrade_value, offspec=c.offspec,
                     received=raid_date, instance=instance, boss=boss, drop_id=drop_id, tiered=bool(item.tiers))


def base_awards(ctx, session: LootSession) -> list[LootAward]:
    return [a for a in ctx.ledger if not a.void] + [a for a in session.awards if not a.void]


def provisional_awards(ctx, session: LootSession, roster: list[Player], exclude: set[str], raid_date: date) -> list[LootAward]:
    """Earlier pending picks count as provisional awards (decision 6): the effective recipient of every pending
    proposal not being judged now."""
    out = []
    base = base_awards(ctx, session)
    for p in session.proposals:
        if p.drop_id in exclude:
            continue
        d = session.drop(p.drop_id)
        if d is None:
            continue
        item = drop_item(ctx, d)
        cands = scoring.candidates(ctx.profile, item, roster, base + out, ctx.wishlists, raid_date, cfg=ctx.loot_cfg, items=ctx.items)
        a = _award(item, cands, p.to, raid_date, session.raid_name, d.boss, d.drop_id)
        if a:
            out.append(a)
    return out


def plan(ctx, session: LootSession, roster: list[Player], drop_ids: list[str]) -> dict:
    """The deterministic part of a distribution: judging order (impact first), per-drop award context (awards before
    it in that order, incl. provisional picks of other pending proposals), candidate tables and their signatures, and
    the kill-order winners (order sensitivity). Run on the snapshot by the worker and on the live session at merge
    time — a drop whose live table signature differs from the judged one is not merged (stale)."""
    raid_date = date.fromisoformat(session.date)
    drops = [session.drop(i) for i in drop_ids]
    drops = [d for d in drops if d is not None]
    items = {d.drop_id: drop_item(ctx, d) for d in drops}
    base = base_awards(ctx, session) + provisional_awards(ctx, session, roster, set(items), raid_date)
    fixed = {p.drop_id: p.override.to for p in session.proposals if p.drop_id in items and p.override}
    kill_order = [d.drop_id for d in drops]
    cfg, lookup = ctx.loot_cfg, ctx.items

    def cands_for(did: str, extra: list[LootAward]):
        return scoring.candidates(ctx.profile, items[did], roster, base + extra, ctx.wishlists, raid_date, cfg=cfg, items=lookup)

    impact = {did: max((c.upgrade_value for c in cands_for(did, []) if not c.already_has), default=0.0) for did in kill_order}
    mode = (cfg.get("distribution") or {}).get("order", "impact")
    order = sorted(kill_order, key=lambda i: (-impact[i], kill_order.index(i))) if mode == "impact" else list(kill_order)
    by_drop = {d.drop_id: d for d in drops}

    def provisional(seq: list[str]):
        awards: list[LootAward] = []
        contexts: dict[str, list[LootAward]] = {}
        winners: dict[str, Optional[str]] = {}
        tables: dict[str, list] = {}
        for did in seq:
            contexts[did] = list(awards)
            cands = cands_for(did, awards)
            tables[did] = cands
            who = fixed.get(did) or next((c.character for c in cands if not c.already_has), None) or (cands[0].character if cands else None)
            winners[did] = who
            if who:
                a = _award(items[did], cands, who, raid_date, session.raid_name, by_drop[did].boss, did)
                if a:
                    awards.append(a)
        return contexts, winners, tables

    contexts, impact_winners, tables = provisional(order)
    _, kill_winners, _ = provisional(kill_order)
    return {"order": order, "kill_order": kill_order, "impact": impact, "mode": mode, "contexts": contexts, "items": items,
            "tables": tables, "sigs": {did: table_signature(t) for did, t in tables.items()}, "base": base,
            "impact_winners": impact_winners, "kill_winners": kill_winners, "raid_date": raid_date}


def run_distribution(ctx, snap: LootSession, roster: list[Player], drop_ids: list[str], *, provider="ctx", progress=None,
                     concurrency: int = 6, batch: int = 1) -> list[Proposal]:
    """Judge `drop_ids` of a SNAPSHOT session (never the live one: the caller merges). Two-pass: the deterministic plan
    gives every drop its table up front; Claude (or the deterministic fallback; `provider=None` = scores only) judges
    them in parallel; then the tables are re-scored against the actual picks and any ranking shift is flagged.
    A pending override on a judged drop is kept (re-scored, never re-picked); a previous model judgment whose table is
    unchanged is reused without a call."""
    provider = ctx.provider if provider == "ctx" else provider
    pl = plan(ctx, snap, roster, drop_ids)
    order, items, raid_date = pl["order"], pl["items"], pl["raid_date"]
    policy = ctx.policy_for_llm()
    cited = [o for o in snap.overrides if o.get("status", "active") == "active"]  # a replaced reason or a corrected award is never cited
    if cited:
        policy += "\n\n## Precedents (council overrides this raid, with reasons)\n" + "\n".join(
            f"- {o['item']}: bot picked {o['bot']}, council awarded {o['human']} — “{o['reason']}”" for o in cited)
    prior = {p.drop_id: p for p in snap.proposals if p.drop_id in items}
    done = 0

    def judge(did: str) -> Proposal:
        nonlocal done
        item, d = items[did], snap.drop(did)
        cands = pl["tables"][did]
        sig = pl["sigs"][did]
        old = prior.get(did)
        if old is not None and old.override:
            res = rec_mod.recommend(ctx.profile, item, cands, policy, None, cfg=ctx.loot_cfg)
            out = Proposal(drop_id=did, item_id=item.id, item_name=item.name, boss=d.boss, award_to=old.award_to, override=old.override,
                           result=DropResult(**{**res.model_dump(), "recommendation": old.result.recommendation}), tiered=bool(item.tiers))
        elif old is not None and old.table_sig == sig and old.result.source.startswith("claude"):
            out = Proposal(drop_id=did, item_id=item.id, item_name=item.name, boss=d.boss, award_to=old.award_to, tiered=bool(item.tiers),
                           result=DropResult(item_id=item.id, item_name=item.name, boss=d.boss, candidates=cands, recommendation=old.result.recommendation,
                                             source=old.result.source.replace(" (unchanged)", "") + " (unchanged)"))
        else:
            with for_guild(getattr(ctx, "guild_key", None)):  # D45: the judge's calls are the council's guild's (worker threads don't inherit it)
                res = rec_mod.recommend(ctx.profile, item, cands, policy, provider, cfg=ctx.loot_cfg)
            res.boss = d.boss
            out = Proposal(drop_id=did, item_id=item.id, item_name=item.name, boss=d.boss, award_to=res.recommendation.primary, result=res, tiered=bool(item.tiers))
            if provider is None and snap.scores_only:
                out.result.source = f"deterministic (scores only, {snap.scores_only.get('by', '?')})"
        out.table_sig = sig
        out.batch = old.batch if old is not None else batch
        out.order = order.index(did) + 1
        out.impact = pl["impact"][did]
        if pl["mode"] == "impact" and pl["kill_winners"].get(did) != pl["impact_winners"].get(did) and not out.override:
            out.order_sensitive = pl["kill_winners"].get(did)
            out.result.recommendation.warnings.append(f"order-sensitive: under kill order this would go to {pl['kill_winners'].get(did)}; decide this one deliberately")
        done += 1
        if progress:
            progress(done, len(order), item.name)
        return out

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(judge, order))

    # pass 3: re-score against actual picks (in judging order), flag shifts
    actual: list[LootAward] = []
    for p in sorted(results, key=lambda p: p.order):
        item, d = items[p.drop_id], snap.drop(p.drop_id)
        fresh = scoring.candidates(ctx.profile, item, roster, pl["base"] + actual, ctx.wishlists, raid_date, cfg=ctx.loot_cfg, items=ctx.items)
        top = next((c for c in fresh if not c.already_has), None)
        if top and not p.override and p.award_to != top.character and not p.result.recommendation.deviates_from_score:
            rank = next((i for i, c in enumerate(fresh, 1) if c.character == p.award_to), None)
            p.result.recommendation.warnings.insert(0, f"earlier awards moved the table: {p.award_to} now ranks #{rank}, top score is {top.character} — re-check")
            p.result.recommendation.close_call = True
        p.result.candidates = fresh
        a = _award(item, fresh, p.to, raid_date, snap.raid_name, d.boss, p.drop_id)
        if a:
            actual.append(a)
    kill = pl["kill_order"]
    return sorted(results, key=lambda p: kill.index(p.drop_id))


def merge(ctx, live: LootSession, snap: LootSession, roster: list[Player], results: list[Proposal]) -> tuple[list[Proposal], list[str]]:
    """Fold a distribution's results into the LIVE session (the caller holds its lock): a drop still pending (not
    awarded meanwhile), whose pending override did not change since the snapshot, and whose live table signature
    matches the judged one, takes its new proposal; anything else is left as it is now and reported stale (judge it
    again). A pick the council set aside while the judge ran — re-judged, but discarded or held ("Decide later") on the
    live session since the snapshot — is never brought back (neither merged nor stale: the council decided).
    Returns (merged, stale drop ids)."""
    want = [p.drop_id for p in results]
    live_plan = plan(ctx, live, roster, [d for d in want if live.drop(d) is not None and not live.awarded(d)])
    merged, stale = [], []
    for p in results:
        d = p.drop_id
        if live.drop(d) is None or live.awarded(d):
            stale.append(d)
            continue
        cur, was = live.pending(d), snap.pending(d)
        snap_d, live_d = snap.drop(d), live.drop(d)
        if (was is not None and cur is None) or (live_d.held and not (snap_d is not None and snap_d.held)):
            continue  # discarded or held after the snapshot: the council's decision stands
        cur_ov = cur.override.model_dump() if cur and cur.override else None
        was_ov = was.override.model_dump() if was and was.override else None
        if cur_ov != was_ov or live_plan["sigs"].get(d) != p.table_sig:
            stale.append(d)
            continue
        if cur is not None:
            live.proposals = [x for x in live.proposals if x.drop_id != d]
        live.proposals.append(p)
        dd = live.drop(d)
        if dd is not None:
            dd.held = False
        merged.append(p)
    kill = {dd.drop_id: i for i, dd in enumerate(live.drop_list)}
    live.proposals.sort(key=lambda x: kill.get(x.drop_id, 10 ** 6))
    return merged, stale


def snapshot(session: LootSession) -> LootSession:
    """A deep copy the worker thread may read freely (never saved: it carries no store)."""
    return LootSession.model_validate_json(session.model_dump_json(by_alias=True))


# ---------------------------------------------------------------- recording

def recipient_of(reg, roster: list[Player], name: str) -> tuple[str, Optional[int], Optional[Player]]:
    """(character, discord_id, seat Player) for a recipient named in chat or by the companion: tonight's roster first
    (character or signup name, case-insensitive), then the guild's registered characters, else the name as given with
    no member (a sub or a guest, stored by name)."""
    n = str(name or "").strip()
    low = n.lower()
    for p in roster:
        if (p.character or "").lower() == low or p.signup_name.lower() == low:
            return p.character or p.signup_name, p.discord_id, p
    if reg is not None and n:
        try:
            hit = reg.find(n)
        except RegistryError:  # two characters share the first name: keep the name as given, no member
            hit = None
        if hit:
            m, c = hit
            return c.label, m.discord_id, None
    return n, None, None


def make_award(ctx, session: LootSession, d: Drop, to: str, roster: list[Player], *, source: str, by: str,
               cands=None, bot_pick: str | None = None, reg=None) -> LootAward:
    """The award a recorded drop becomes: weight from the candidate table when the recipient is in it, else from the
    item's tier for the recipient's spec × slot weight (B when untiered for that spec; untiered_weight for an item
    without tier data) — never a literal 0.5 (decision 4)."""
    item = drop_item(ctx, d)
    raid_date = date.fromisoformat(session.date)
    if cands is None:
        cands = scoring.candidates(ctx.profile, item, roster, base_awards(ctx, session), ctx.wishlists, raid_date, cfg=ctx.loot_cfg, items=ctx.items)
    who, uid, seat = recipient_of(reg, roster, to) if to != DISENCHANT else (DISENCHANT, None, None)
    c = next((c for c in cands if c.character.lower() == who.lower()), None)
    if to == DISENCHANT:
        tier, weight, offspec = "-", 0.0, False
    elif c is not None:
        tier, weight, offspec = c.tier, c.upgrade_value, c.offspec
    else:
        spec = None
        if seat is not None:
            try:
                spec = ctx.profile.spec(seat.cls, seat.spec)
            except KeyError:
                spec = None
        elif reg is not None and uid is not None:
            m = reg.members.get(uid)
            c = next((c for c in (m.characters if m else []) if c.label.lower() == who.lower()), None)
            try:
                spec = ctx.profile.spec(c.cls, c.spec) if c else None
            except KeyError:
                spec = None
        tier, weight = scoring.weight_for(ctx.profile, item, spec, ctx.loot_cfg)
        offspec = False
    snap = [{"character": x.character, "spec": f"{x.cls}:{x.spec}", "tier": x.tier, "base": x.base_score} for x in cands[:7]]
    rid = session.id  # the run key for a whole run's council; '<run>-r<N>' per roster of a split run (drop ids are per session)
    return LootAward(raider=who, item_id=item.id, item_name=item.name, tier=tier, total_weight=weight, offspec=offspec, received=raid_date,
                     instance=session.raid_name, boss=d.boss, discord_id=uid, tiered=bool(item.tiers), by=by, policy_version=ctx.policy_version(),
                     candidates_snapshot=snap, drop_id=d.drop_id, import_id=f"{rid}-{d.drop_id}", source=source, bot_pick=bot_pick, confirmed_at=now_iso())


def ledger_row(session: LootSession, a: LootAward) -> dict:
    return {**json.loads(a.model_dump_json()), "event": session.run_key or session.id}


def precedent_row(session: LootSession, a: LootAward, reason: str, by: str, bot: str | None) -> dict:
    return {"date": session.date, "event": session.run_key or session.id, "item": a.item_name, "item_id": a.item_id, "drop_id": a.drop_id,
            "import_id": a.import_id, "bot": bot or "?", "human": a.raider, "reason": reason.strip(), "by": by, "at": now_iso(),
            "policy_version": a.policy_version, "status": "active"}


def sync_ledger(ctx, session: LootSession) -> int:
    """Append the ledger rows the session's awards imply and the ledger lacks (by import_id): Confirm's idempotent
    derivation, and the repair after a crash between the session write and the ledger write. A test session writes
    nothing. Returns rows appended."""
    if session.test:
        return 0
    n = 0
    for a in session.awards:
        if a.import_id and a.import_id not in ctx.import_ids:
            if a.void:
                continue
            ctx.append_ledger(ledger_row(session, a), a)
            n += 1
    return n


def check_reason(reason: str | None) -> str:
    r = (reason or "").strip()
    if len(r) < REASON_MIN:
        raise RegistryError("Give a reason (a few words) — it becomes the precedent the bot cites next time.")
    if len(r) > REASON_MAX:
        raise RegistryError(f"Keep the reason under {REASON_MAX} characters.")
    return r


# ---------------------------------------------------------------- loading and migration

def load_sessions(store) -> dict[tuple[str, str], LootSession]:
    """Live sessions from every guild directory in the data repo, keyed (guild key, session id). Reads old
    thread-keyed files too (the model converts them); `migrate_sessions` rewrites them under their new names."""
    out: dict[tuple[str, str], LootSession] = {}
    root = Path(store.root)
    if not root.is_dir():
        return out
    for gdir in sorted(root.iterdir()):
        d = gdir / "events"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                s = LootSession.model_validate_json(f.read_text())
            except Exception as e:  # noqa: BLE001
                log.warning("could not load loot session %s: %s", f, e)
                continue
            if s.guild != gdir.name:
                s.guild = gdir.name  # pre-refactor files had no guild field
            if s.origin != "raid":  # a retired /mock session (D11): its file stays as written, nothing loads it
                log.info("loot session %s is a retired /mock session — not loaded", f.name)
                continue
            s.attach(store)
            if s.key in out and len(out[s.key].awards) >= len(s.awards):
                continue  # two legacy files for one run: the one with more recorded wins (migrate archives the other)
            out[s.key] = s
    return out


def migrate_sessions(store, dry_run: bool = False) -> list[str]:
    """Rewrite every loot session file under its new name (<guild>/events/<id>.json, schema 2: drop ids, run key / mock
    key) — idempotent: a file already named by its id with schema 2 is left alone. Two old files mapping to one id:
    the one with more awards stays live, the other is archived as `<id>-legacy-<old name>.json`. One commit. Returns
    a line per file moved (empty when there was nothing to do)."""
    root = Path(store.root)
    lines: list[str] = []
    if not root.is_dir():
        return lines
    for gdir in sorted(root.iterdir()):
        d = gdir / "events"
        if not d.is_dir():
            continue
        found: dict[str, list[tuple[Path, LootSession, dict]]] = {}
        for f in sorted(d.glob("*.json")):
            try:
                raw = json.loads(f.read_text())
                s = LootSession.model_validate(raw)
            except Exception as e:  # noqa: BLE001
                lines.append(f"skipped {f.relative_to(root)}: {type(e).__name__}")
                continue
            s.guild = gdir.name
            found.setdefault(s.id, []).append((f, s, raw))
        for sid, files in found.items():
            files.sort(key=lambda t: -len(t[1].awards))
            keep_f, keep, keep_raw = files[0]
            target = d / f"{sid}.json"
            if keep_f == target and keep_raw.get("schema") == SCHEMA and len(files) == 1:
                continue
            lines.append(f"{keep_f.relative_to(root)} → {target.relative_to(root)}")
            if not dry_run:
                store.write_text(target.relative_to(root), keep.model_dump_json(indent=1, by_alias=True))
                if keep_f != target and keep_f.exists():
                    keep_f.unlink()
            for f, s, _ in files[1:]:
                arch = d / "archive" / f"{sid}-legacy-{f.stem}.json"
                lines.append(f"{f.relative_to(root)} → {arch.relative_to(root)} (a second file for {sid})")
                if not dry_run:
                    store.write_text(arch.relative_to(root), s.model_dump_json(indent=1, by_alias=True))
                    if f.exists() and f != target:
                        f.unlink()
    if lines and not dry_run:
        store.commit(f"loot sessions migrated to run keys and drop ids ({len(lines)} file(s))")
    return lines
