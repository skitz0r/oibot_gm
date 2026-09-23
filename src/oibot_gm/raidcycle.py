"""Signup-driven raid cycle: one sheet per raid slot per lockout window (opened on cadence), Join / Bench / No thanks,
health check, lock → roster(s) from the signups (weights + officer pins), confirmations, seat freeing and filling.
No LLM anywhere in this module."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import RANK_ORDER, ROLES
from .models import Player, RosterResult
from .profiles import GameProfile
from .registry import DEFAULT_RAID_SIZE, RAID_DEFAULTS, Member, Registry, RegisteredCharacter, now
from .roster import explain, solver
from .store import GitStore

log = logging.getLogger(__name__)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
STATUSES = ("in", "sub", "out")  # Join / Bench / No thanks (legacy "tentative" is read as "in")
LABELS = {"in": "Join", "sub": "Bench", "out": "No thanks"}
STATES = ("open", "locked", "done", "cancelled")
LEGACY_STATES = {"proposed": "locked", "accepted": "locked"}  # pre-2026-09 files
MAX_ROSTERS_PER_SLOT = 4
RUN_LISTENERS: list = []  # callables (guild_key, run_key) told after every RaidStore.save
TEAM_DEFAULTS = {"cutoff_soft_hours": 48, "cutoff_hard_hours": 24, "open_days_before": 6, "reminders": "dm", "open_dm": False, "autofill": True}
FILL_OVERASK = 1  # ask one more person than the shortfall per batch
FILL_MAX_OPEN = 3  # never more than this many unanswered asks per event
FILL_ASK_HOURS_DEFAULT = RAID_DEFAULTS["fill_ask_hours"]
CONFLICT_WINDOW_HOURS = 4.0  # two runs closer than this count as the same night
OPEN_HORIZON_DAYS = 21  # how far ahead "upcoming runs" lists look
CLOSE_AFTER_HOURS = 3  # a run is closed this long after its scheduled end (start + duration)
SOLVE_TIME_LIMIT_S = 12.0  # the lock solve
PREVIEW_TIME_LIMIT_S = 8.0  # split previews in the modal


class Signup(BaseModel):
    discord_id: int
    display_name: str
    character: str
    cls: str
    spec: str
    offspec: Optional[str] = None
    role: str
    status: str  # in | sub | out (STATUSES; "tentative" in old files reads as "in")
    source: str = "member"  # member | prefill | officer | callout | absence
    note: Optional[str] = None
    updated_at: str = Field(default_factory=now)


class Callout(BaseModel):
    discord_id: int
    display_name: str
    character: str
    at: str
    hours_before: float
    note: Optional[str] = None
    late: bool  # after hard cutoff


class FillAsk(BaseModel):
    """One DM asking someone to fill a gap: come as a sub, switch to an offspec, or bring an alt."""

    discord_id: int
    display_name: str
    kind: str  # sub | pool | other_roster | offspec | alt
    role: str  # the role this ask covers
    character: str  # what they'd play
    spec: str
    reason: str  # "short 1 healer", "short 2"
    asked_at: str = Field(default_factory=now)
    answer: Optional[str] = None  # yes | no | expired | released (the paired ask fell through)
    answered_at: Optional[str] = None
    expires_at: Optional[str] = None  # ISO: no answer by then counts as no
    pair: Optional[int] = None  # discord_id of the ask this one is tied to (a swap and the backfill covering the vacated seat)
    vacates: Optional[str] = None  # swaps: the role the person leaves

    @property
    def open(self) -> bool:
        return self.answer is None

    @property
    def swap(self) -> bool:
        return self.kind in ("offspec", "alt")


class RaidEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")  # older files carry a `roster` key (folded into `rosters` below)

    key: str  # <run roster key>-<YYYY-MM-DD>
    team: str  # roster key; for cadence runs an ephemeral roster named like the run (bd-1209-1930)
    instance: Optional[str] = None
    starts_at: str  # ISO with offset
    state: Literal["open", "locked", "done", "cancelled"] = "open"
    signups: dict[str, Signup] = Field(default_factory=dict)  # discord_id -> signup
    pins: dict[str, str] = Field(default_factory=dict)  # discord_id -> "in" | "out": officer decisions the solver must honour
    layout: Optional[list[list[str]]] = None  # the officers' board before lock: groups (across rosters) of signup display names
    split_strategy: Optional[str] = None  # balanced | first | rotation chosen for this run (default: the raid's split_policy)
    cards: dict[str, int] = Field(default_factory=dict)  # officer cards in the roster channel: "health" | "lock:<i>" -> message id
    cards_channel_id: Optional[int] = None
    updates_thread_id: Optional[int] = None  # thread under the officer card with fill / confirmation updates
    rosters: list[RosterResult] = Field(default_factory=list)  # after lock: one or more rosters for this slot (roster = rosters[0])
    locked_at: Optional[str] = None
    confirm_by: Optional[str] = None  # ISO: unanswered confirmations expire here
    callouts: list[Callout] = Field(default_factory=list)
    channel_id: Optional[int] = None
    message_id: Optional[int] = None
    thread_id: Optional[int] = None
    health_posted: bool = False
    nudged: list[int] = Field(default_factory=list)
    fill_asks: list[FillAsk] = Field(default_factory=list)
    fill_state: str = "idle"  # idle | asking | filled | exhausted
    lock_error: Optional[str] = None  # the last failed lock attempt (sheet stays open); cleared on success
    lock_tried_at: Optional[str] = None
    log: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        """Old files: `state: proposed|accepted` → locked; a lone `roster` → rosters[0]; signup `tentative` → in."""
        if isinstance(data, dict):
            data = dict(data)
            if data.get("state") in LEGACY_STATES:
                data["state"] = LEGACY_STATES[data["state"]]
            legacy = data.pop("roster", None)
            if legacy and not data.get("rosters"):
                data["rosters"] = [legacy]
            for sg in (data.get("signups") or {}).values():
                if isinstance(sg, dict) and sg.get("status") == "tentative":
                    sg["status"] = "in"
        return data

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.starts_at)

    @property
    def roster(self) -> Optional[RosterResult]:
        """The first (or only) roster; rosters[] is the source of truth."""
        return self.rosters[0] if self.rosters else None


    def by_status(self, status: str) -> list[Signup]:
        return sorted([s for s in self.signups.values() if s.status == status], key=lambda s: s.updated_at)

    @property
    def all_rosters(self) -> list[RosterResult]:
        return self.rosters  # kept for older call sites; the same list, not a copy

    def seated(self) -> list[Player]:
        return [p for r in self.all_rosters for p in r.selected]

    def seat_of(self, display_name: str) -> tuple[int, Player] | None:
        for i, r in enumerate(self.all_rosters):
            for p in r.selected:
                if p.signup_name == display_name:
                    return i, p
        return None

    def rel_path(self, guild_key: str) -> Path:
        return Path(guild_key) / "raids" / f"{self.key}.json"


# ---------------------------------------------------------------- schedule math

def parse_schedule(text: str) -> tuple[int, int, int]:
    """'Tue 19:30' -> (weekday, hour, minute)."""
    parts = text.strip().split()
    if len(parts) != 2 or parts[0][:3].lower() not in WEEKDAYS or ":" not in parts[1]:
        raise ValueError("schedule looks like 'Tue 19:30'")
    h, m = parts[1].split(":")
    return WEEKDAYS[parts[0][:3].lower()], int(h), int(m)


def next_raid_time(schedule: str, tz: str, after: datetime | None = None) -> datetime:
    wd, h, m = parse_schedule(schedule)
    z = ZoneInfo(tz)
    ref = (after or datetime.now(z)).astimezone(z)
    day = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (wd - day.weekday()) % 7
    cand = day + timedelta(days=delta)
    if cand <= ref:
        cand += timedelta(days=7)
    return cand


def team_setting(team: dict, key: str):
    return team.get(key, TEAM_DEFAULTS.get(key))


def run_team(reg: Registry, ev: "RaidEvent") -> dict:
    """The roster dict a run belongs to; a bare stand-in when the ephemeral roster is gone (cleanup, old files)."""
    return reg.config.team(ev.team) or {"key": ev.team, "size": int(reg.raid_def(ev.instance).get("size") or DEFAULT_RAID_SIZE)}


def run_size(reg: Registry, ev: "RaidEvent") -> int:
    """Seats per roster for a run: the roster's size, else the raid's, else DEFAULT_RAID_SIZE."""
    team = reg.config.team(ev.team) or {}
    return int(team.get("size") or reg.raid_def(ev.instance).get("size") or DEFAULT_RAID_SIZE)


def run_close_at(reg: Registry, ev: "RaidEvent") -> datetime:
    """When the scheduler marks a locked run done: start + the raid's duration + CLOSE_AFTER_HOURS."""
    return ev.start + timedelta(hours=float(reg.raid_def(ev.instance).get("duration_hours") or RAID_DEFAULTS["duration_hours"]) + CLOSE_AFTER_HOURS)


def abbr(instance: str) -> str:
    return "".join(w[0] for w in instance.split("_"))[:4]


def run_key(instance: str, start: datetime) -> str:
    return f"{abbr(instance)}-{start.strftime('%m%d-%H%M')}"


def slot_starts(reg: Registry, instance: str, now: datetime, horizon_hours: float) -> list[tuple[str, datetime]]:
    """(slot, start) for every occurrence of the raid's slots within `horizon_hours` of now (and never before first_open)."""
    rd = reg.raid_def(instance)
    fo = reg.first_open(instance)
    out = []
    for slot in rd.get("slots") or []:
        try:
            t = next_raid_time(slot, reg.config.timezone, after=now - timedelta(minutes=1))
        except ValueError:
            continue
        while t <= now + timedelta(hours=horizon_hours):
            if fo is None or t >= fo:
                out.append((slot, t))
            t += timedelta(days=7)
    return sorted(out, key=lambda x: x[1])


# a run opened closer than its cadence assumes keeps this much of the remaining time for each step
FIT_NUDGE, FIT_LOCK, FIT_CONFIRM = 0.6, 0.3, 0.1


def fit_cutoffs(available_h: float, nudge_h: float, lock_h: float, confirm_h: float) -> tuple[float, float, float]:
    """Cadence hours-before-start, squeezed into the time a run actually has. A pickup opened two hours out would
    otherwise inherit a 24 h lock, putting nudge and lock in the past so the scheduler locks it on the next tick with
    nobody on the sheet. When the cadence fits (the usual case) nothing changes, because each min() keeps the cadence."""
    if available_h <= 0:
        return 0.0, 0.0, 0.0
    return (min(nudge_h, available_h * FIT_NUDGE), min(lock_h, available_h * FIT_LOCK), min(confirm_h, available_h * FIT_CONFIRM))


def ensure_run(reg: Registry, instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None) -> dict:
    """The ephemeral roster that carries one run's settings (size, cutoffs from the raid's cadence); created once.
    `cutoffs` = {soft, hard, confirm} in hours to override the cadence (test runs compress it to minutes)."""
    key = run_key(instance, start)
    t = reg.config.roster(key)
    if t:
        return t
    rd = reg.raid_def(instance)
    lead, lock, confirm = float(rd["signup_lead_hours"]), float(rd["lock_hours_before"]), float(rd["confirm_hours_before"])
    c = cutoffs or {}
    if not c:  # test runs pass their own compressed cutoffs and mean them
        available = (start - reg.now_local()).total_seconds() / 3600
        nudge_h, lock, confirm = fit_cutoffs(available, float(rd["nudge_hours_before"]), lock, confirm)
        rd = {**rd, "nudge_hours_before": nudge_h}
    t = {"key": key, "name": f"{rd.get('name', instance)} {reg.local12(start)}", "size": int(rd.get("size") or DEFAULT_RAID_SIZE), "schedule": start.strftime("%a %H:%M"), "instance": instance,
         "cutoff_soft_hours": c.get("soft", float(rd["nudge_hours_before"])), "cutoff_hard_hours": c.get("hard", lock), "confirm_hours": c.get("confirm", confirm), "open_days_before": lead / 24,
         "reminders": "dm" if c.get("nudge", rd["nudge"]) else "none", "open_dm": bool(c.get("open_dm", rd["open_dm"])), "autofill": bool(rd["autofill"]), "ephemeral": True}
    if cutoffs:
        t["test"] = True
        t["test_by"] = c.get("test_by")
    reg.config.rosters.append(t)
    reg.save_config(f"run {key} ({t['name']}) created by {by}", notify=False)
    return t


def open_run(reg: Registry, rs: "RaidStore", instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None) -> "RaidEvent":
    return open_event(reg, rs, ensure_run(reg, instance, start, by, cutoffs), start)


# ---------------------------------------------------------------- events on the store

class RaidStore:
    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key
        self.events: dict[str, RaidEvent] = {}
        d = store.root / guild_key / "raids"
        if d.exists():
            for f in d.glob("*.json"):
                ev = RaidEvent.model_validate_json(f.read_text())  # legacy states / roster / tentative normalised by the model
                self.events[ev.key] = ev

    def save(self, ev: RaidEvent, message: str) -> None:
        self.store.write_text(ev.rel_path(self.key), ev.model_dump_json(indent=1))
        self.events[ev.key] = ev
        self.store.commit(f"{self.key}: raid {ev.key}: {message}")
        for fn in list(RUN_LISTENERS):  # e.g. the web's event stream; may be called from a worker thread
            try:
                fn(self.key, ev.key)
            except Exception:  # noqa: BLE001 — a listener must never break a save
                log.exception("run listener failed")

    def live(self) -> list[RaidEvent]:
        return sorted([e for e in self.events.values() if e.state not in ("done", "cancelled")], key=lambda e: e.starts_at)

    def for_team(self, team: str) -> Optional[RaidEvent]:
        return next((e for e in self.live() if e.team == team), None)

    def by_message(self, message_id: int) -> Optional[RaidEvent]:
        return next((e for e in self.events.values() if e.message_id == message_id), None)


# ---------------------------------------------------------------- building an event

def open_event(reg: Registry, rs: RaidStore, team: dict, starts_at: datetime) -> RaidEvent:
    key = f"{team['key']}-{starts_at.date().isoformat()}"
    if key in rs.events:
        return rs.events[key]
    ev = RaidEvent(key=key, team=team["key"], instance=team.get("instance"), starts_at=starts_at.isoformat())
    prefill(reg, ev, team)
    ev.log.append(f"opened; {len(ev.signups)} pre-filled from absences")
    rs.save(ev, "opened")
    return ev


def prefill(reg: Registry, ev: RaidEvent, team: dict) -> None:
    """Absences (and curated roster membership) seed the sheet so people act on exceptions."""
    day = ev.start.date().isoformat()
    explicit = bool(reg.roster_members(team["key"]))
    for m, main in reg.roster_pool(team["key"]):
        if not main:
            continue
        if m.absent_on(day):
            status, source = "out", "absence"
        elif explicit:
            status, source = "in", "prefill"  # members of a curated roster default in
        else:
            continue  # open pool: they must respond
        ev.signups[str(m.discord_id)] = _signup(reg, m, main, status, source)


def _signup(reg: Registry, m: Member, c: RegisteredCharacter, status: str, source: str, note: str | None = None) -> Signup:
    return Signup(discord_id=m.discord_id, display_name=m.display_name, character=c.label, cls=c.cls, spec=c.spec, offspec=c.offspec, role=reg.profile.spec(c.cls, c.spec).role, status=status, source=source, note=note)


def set_signup(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, character: str | None, status: str, source: str = "member", note: str | None = None) -> Signup:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    want = (character or "").strip().lower()
    c = (next((c for c in m.active() if c.label.lower() == want), None) or next((c for c in m.active() if (c.name or "").lower() == want), None)) if want else m.main
    if not c:
        raise ValueError("no such active character")
    team = run_team(reg, ev)
    if status == "in" and reg.roster_members(team["key"]) and not reg.on_roster(m, team["key"]) and source == "member":
        status, note = "sub", "not on this roster; subs are picked when needed"
    s = _signup(reg, m, c, status, source, note)
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"{m.display_name} {status} as {c.label} ({source})")
    rs.save(ev, f"{m.display_name} {status}")
    return s


def signup_by_name(ev: RaidEvent, display_name: str) -> Optional[Signup]:
    n = display_name.strip().lower()
    return next((s for s in ev.signups.values() if s.display_name.lower() == n), None)


def set_pin(ev: RaidEvent, display_name: str, pin: str | None) -> None:
    """Officer decision the lock solve must honour: "in" | "out" | None (clear). Pins are keyed by discord id (as
    the web API keys them); the person must be on the sheet. Appends the log line; the caller saves the event."""
    if pin not in ("in", "out", None):
        raise ValueError("pin must be in, out or nothing (clear)")
    sg = signup_by_name(ev, display_name)
    if not sg:
        raise ValueError(f"{display_name} isn't on this sheet")
    uid = str(sg.discord_id)
    if pin:
        ev.pins[uid] = pin
    else:
        ev.pins.pop(uid, None)
    ev.log.append(f"{sg.display_name} {'pinned ' + pin if pin else 'unpinned'}")


def pin_summary(ev: RaidEvent) -> str:
    """'pinned in: A, B · pinned out: C' (people no longer on the sheet are skipped), or 'no pins'."""
    by_uid = {str(sg.discord_id): sg.display_name for sg in ev.signups.values()}
    parts = []
    for pin in ("in", "out"):
        names = sorted(by_uid[u] for u, v in ev.pins.items() if v == pin and u in by_uid)
        if names:
            parts.append(f"pinned {pin}: {', '.join(names)}")
    return " · ".join(parts) or "no pins"


def callout(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, team: dict, note: str | None) -> Callout:
    s = ev.signups.get(str(m.discord_id))
    c = s.character if s else (m.main.name if m.main else "?")
    hours = (ev.start - datetime.now(ev.start.tzinfo)).total_seconds() / 3600
    late = hours < team_setting(team, "cutoff_hard_hours")
    co = Callout(discord_id=m.discord_id, display_name=m.display_name, character=c, at=now(), hours_before=round(hours, 1), note=note, late=late)
    ev.callouts.append(co)
    if s:
        s.status, s.source, s.note, s.updated_at = "out", "callout", note, now()
    else:
        ev.signups[str(m.discord_id)] = _signup(reg, m, m.main, "out", "callout", note) if m.main else None  # type: ignore[assignment]
        ev.signups = {k: v for k, v in ev.signups.items() if v is not None}
    ev.log.append(f"callout {m.display_name} {hours:.0f}h before{' (late)' if late else ''}")
    rs.save(ev, f"callout {m.display_name}")
    return co


# ---------------------------------------------------------------- filling gaps

def conflicts(rs: RaidStore, ev: RaidEvent, window_hours: float = CONFLICT_WINDOW_HOURS) -> dict[int, str]:
    """discord_id -> other raid key, for people In on another live raid within the window."""
    out: dict[int, str] = {}
    for other in rs.live():
        if other.key == ev.key:
            continue
        if abs((other.start - ev.start).total_seconds()) > window_hours * 3600:
            continue
        for s in other.signups.values():
            if s.status == "in":
                out[s.discord_id] = other.key
    return out


def needs(reg: Registry, ev: RaidEvent, team: dict) -> dict:
    """What the sheet is short: headcount and per-role minimums. Before lock: Join signups vs the raid size.
    After lock: freed seats across the locked roster(s)."""
    size = run_size(reg, ev)
    if ev.state != "open" and ev.all_rosters:
        bounds = reg.role_bounds(ev.instance, size)
        seated = ev.seated()
        n = len(ev.all_rosters)
        role_short = {}
        for role in ("tank", "healer"):
            have = sum(1 for p in seated if p.role == role)
            if bounds[role]["min"] * n > have:
                role_short[role] = bounds[role]["min"] * n - have
        return {"headcount": max(0, size * n - len(seated)), "roles": role_short, "size": size}
    h = health_data(reg, ev, team)
    n_in, size, _legacy, _subs = h["headcount"]
    role_short = {r["role"]: r["need"] - r["have"] for r in h["roles"] if r["need"] and r["have"] < r["need"]}
    return {"headcount": max(0, size - n_in), "roles": role_short, "size": size}


def _rank_key(reg: Registry, m: Member) -> tuple:
    c = m.main
    return (RANK_ORDER.get(c.rank if c else "trial", 9), m.created_at)


def ask_deadline(reg: Registry, ev: RaidEvent, at: datetime | None = None) -> datetime:
    """When an unanswered fill ask counts as no: `fill_ask_hours` after it was sent, never past the run start."""
    at = at or datetime.now(ev.start.tzinfo)
    hours = float(reg.raid_def(ev.instance).get("fill_ask_hours") or FILL_ASK_HOURS_DEFAULT)
    return min(at + timedelta(hours=hours), ev.start)


def would_short(reg: Registry, ev: RaidEvent, team: dict, role: str) -> bool:
    """Would losing one `role` player (a Join before lock, a seated player after) drop that role under its minimum?"""
    size = run_size(reg, ev)
    n = len(ev.all_rosters) or 1
    bounds = reg.role_bounds(ev.instance, size)
    players = ev.seated() if ev.state != "open" and ev.all_rosters else ev.by_status("in")
    key = "dps" if role in ("melee", "ranged") else role
    have = sum(1 for p in players if (p.role in ("melee", "ranged") if key == "dps" else p.role == role))
    return have - 1 < int(bounds.get(key, {}).get("min") or 0) * n


def fill_candidates(reg: Registry, rs: RaidStore, ev: RaidEvent, team: dict) -> list[FillAsk]:
    """Ordered list of people to ask, best first, for the sheet's current needs. Pure; nothing is sent.
    Order: role gaps first (a sub/pool member of that role, else an In player's offspec or alt — a swap is only
    proposed when the role it vacates stays covered, or someone on the bench/pool can backfill it: then both asks
    go out tied together, and a no from either releases the other), then plain headcount
    (subs → roster pool not on the sheet → other rosters' members free that night)."""
    nd = needs(reg, ev, team)
    if not nd["headcount"] and not nd["roles"]:
        return []
    day = ev.start.date().isoformat()
    busy = conflicts(rs, ev)
    asked = {a.discord_id for a in ev.fill_asks}
    locked = ev.state != "open" and bool(ev.all_rosters)
    seated_names = {p.signup_name for p in ev.seated()} if locked else set()
    # after lock, "in" means seated; joiners the solver benched are the first people to ask
    ins = {s.discord_id for s in ev.by_status("in") if not locked or s.display_name in seated_names}
    benched_joiners = [s for s in ev.by_status("in") if locked and s.display_name not in seated_names]
    out: list[FillAsk] = []
    used: set[int] = set()

    def ok(m: Member) -> bool:
        return m.discord_id not in asked and m.discord_id not in used and not m.dm_opt_out and not m.absent_on(day) and m.discord_id not in busy

    def role_of(c: RegisteredCharacter) -> str:
        return reg.profile.spec(c.cls, c.spec).role

    pool_ids = {m.discord_id for m, _ in reg.roster_pool(team["key"])}
    # bench tier: joiners the solver left off, then Bench answers — never someone already seated (the solver may seat a Bench answer)
    subs = [(m, c) for s in benched_joiners + ev.by_status("sub") if s.display_name not in seated_names and (m := reg.members.get(s.discord_id)) and (c := next((c for c in m.active() if c.label == s.character), m.main))]
    unresponsive = [(m, c) for m, c in reg.roster_pool(team["key"]) if str(m.discord_id) not in ev.signups and c]
    others = [] if team.get("test") else sorted([(m, m.main) for m in reg.members.values() if m.main and m.discord_id not in pool_ids and str(m.discord_id) not in ev.signups], key=lambda mc: _rank_key(reg, mc[0]))
    tiers = [("sub", subs), ("pool", unresponsive), ("other_roster", others)]

    def first_of(role: str, skip: int | None = None):
        for kind, cands in tiers:
            for m, c in cands:
                if ok(m) and m.discord_id != skip and role_of(c) == role:
                    return kind, m, c
        return None

    # 1. role gaps
    for role, short in nd["roles"].items():
        got = 0
        for kind, cands in tiers:
            for m, c in cands:
                if got >= short:
                    break
                if ok(m) and role_of(c) == role:
                    out.append(FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind=kind, role=role, character=c.label, spec=c.spec, reason=f"short {short} {role}"))
                    used.add(m.discord_id)
                    got += 1
        # switches by people already In: offspec, then an alt of the right role
        for sid in ins:
            if got >= short:
                break
            m = reg.members.get(sid)
            s = ev.signups.get(str(sid))
            if not m or not s or not ok(m) or s.role == role:
                continue
            ask = None
            if s.offspec and reg.profile.spec(s.cls, s.offspec).role == role:
                ask = FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="offspec", role=role, character=s.character, spec=s.offspec, reason=f"short {short} {role}", vacates=s.role)
            else:
                alt = next((a for a in m.active() if a.label != s.character and role_of(a) == role), None)
                if alt:
                    ask = FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="alt", role=role, character=alt.label, spec=alt.spec, reason=f"short {short} {role}", vacates=s.role)
            if not ask:
                continue
            # look ahead: does the swap open a hole where they are now? then it only goes out with a backfill tied to it
            if would_short(reg, ev, team, s.role):
                back = first_of(s.role, skip=sid)
                if not back:
                    continue
                bk, bm, bc = back
                ask.pair = bm.discord_id
                out.append(ask)
                out.append(FillAsk(discord_id=bm.discord_id, display_name=bm.display_name, kind=bk, role=s.role, character=bc.label, spec=bc.spec, reason=f"covering {m.display_name}'s {s.role} seat", pair=sid))
                used.update({sid, bm.discord_id})
            else:
                out.append(ask)
                used.add(sid)
            got += 1
    # 2. headcount (role-gap asks that bring a new body count towards it)
    remaining = nd["headcount"] - sum(1 for a in out if not a.swap and not a.pair)
    for kind, cands in tiers:
        for m, c in cands:
            if remaining <= 0:
                break
            if ok(m):
                out.append(FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind=kind, role=role_of(c), character=c.label, spec=c.spec, reason=f"short {nd['headcount']}"))
                used.add(m.discord_id)
                remaining -= 1
    return out


def fill_batch(reg: Registry, rs: RaidStore, ev: RaidEvent, team: dict) -> list[FillAsk]:
    """The next asks to send now: shortfall + FILL_OVERASK, capped so at most FILL_MAX_OPEN are outstanding."""
    open_asks = [a for a in ev.fill_asks if a.open]
    nd = needs(reg, ev, team)
    want = nd["headcount"] + sum(nd["roles"].values())
    if want <= 0:
        return []
    room = max(0, min(want + FILL_OVERASK, FILL_MAX_OPEN) - len(open_asks))
    return fill_candidates(reg, rs, ev, team)[:room]


def _reseat(ev: RaidEvent, sg: Signup) -> bool:
    """A swap by someone already seated: update their seat in place (character, spec, role) instead of seating twice."""
    hit = ev.seat_of(sg.display_name)
    if not hit:
        return False
    p = hit[1]
    p.cls, p.spec, p.role, p.offspec, p.character = sg.cls, sg.spec, sg.role, sg.offspec, sg.character
    return True


def partner_of(ev: RaidEvent, ask: FillAsk) -> Optional[FillAsk]:
    """The open ask tied to this one (swap ↔ backfill), if any."""
    if ask.pair is None:
        return None
    return next((a for a in ev.fill_asks if a.open and a.discord_id == ask.pair and a.pair == ask.discord_id), None)


def release_partner(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk) -> Optional[FillAsk]:
    """A no (or silence) on one half of a tied pair withdraws the other half. Returns the released ask."""
    other = partner_of(ev, ask)
    if other:
        other.answer, other.answered_at = "released", now()
        ev.log.append(f"fill: {other.display_name}'s ask withdrawn ({ask.display_name} said no)")
        rs.save(ev, f"fill {other.display_name} released")
    return other


def expire_fill_asks(reg: Registry, rs: RaidStore, ev: RaidEvent) -> tuple[list[FillAsk], list[FillAsk]]:
    """Open asks past their deadline count as no. Returns (expired, released partners)."""
    t = datetime.now(ev.start.tzinfo)
    expired, released = [], []
    for a in ev.fill_asks:
        if a.open and a.expires_at and datetime.fromisoformat(a.expires_at) <= t:
            a.answer, a.answered_at = "expired", now()
            expired.append(a)
            ev.log.append(f"fill: no answer from {a.display_name} in time")
    if not expired:
        return expired, released
    with rs.store.batch(f"{rs.key}: raid {ev.key}: fill expired {len(expired)}"):
        for a in expired:
            other = release_partner(reg, rs, ev, a)
            if other:
                released.append(other)
        rs.save(ev, f"fill expired {len(expired)}")
    return expired, released


def apply_fill_answer(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk, yes: bool) -> str:
    """Record the answer and, on yes, put the person on the sheet the way they were asked."""
    ask.answer, ask.answered_at = ("yes" if yes else "no"), now()
    m = reg.members.get(ask.discord_id)
    if not yes or not m:
        ev.log.append(f"fill: {ask.display_name} declined ({ask.kind})")
        rs.save(ev, f"fill {ask.display_name} no")
        return f"{ask.display_name} can't ({'swap' if ask.swap else ask.kind})"
    if ask.kind == "offspec":
        s = ev.signups[str(ask.discord_id)]
        s.spec, s.offspec, s.role, s.note, s.source, s.updated_at = ask.spec, s.spec, ask.role, f"switched to {ask.spec} at request", "fill", now()
        _reseat(ev, s)
        ev.log.append(f"fill: {ask.display_name} switches to {ask.spec}")
        rs.save(ev, f"fill {ask.display_name} offspec")
        return f"{ask.display_name} swaps to {ask.spec} ({ask.role})"
    c = next((c for c in m.active() if c.label == ask.character), m.main)
    s = _signup(reg, m, c, "in", "fill", "filled in at request" if ask.kind != "alt" else f"swapped to {c.label} at request")
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"fill: {ask.display_name} in as {c.label} ({ask.kind})")
    if ev.state != "open" and ev.all_rosters and not _reseat(ev, s):
        seat_player(reg, ev, s)
    rs.save(ev, f"fill {ask.display_name} in")
    return f"{ask.display_name} {'swaps to' if ask.kind == 'alt' else 'is in as'} {c.label} ({ask.role})"


# ---------------------------------------------------------------- health

def health_data(reg: Registry, ev: RaidEvent, team: dict) -> dict:
    """Structured health check: headcount, per-role tiles, per-buff providers, non-responders."""
    profile = reg.profile
    ins, subs = ev.by_status("in"), ev.by_status("sub")
    size = run_size(reg, ev)
    bounds = reg.role_bounds(ev.instance, size)
    counts = {r: sum(1 for s in ins if s.role == r) for r in ROLES}
    need = {"tank": bounds["tank"]["min"], "healer": bounds["healer"]["min"], "melee": 0, "ranged": 0}
    roles = []
    for r in ROLES:
        have, n = counts[r], need[r]
        cover = {"offspec": [], "flex": [], "alt": [], "sub": []}
        if n and have < n:
            for sg in ins:
                m = reg.members.get(sg.discord_id)
                if sg.offspec and profile.spec(sg.cls, sg.offspec).role == r and sg.role != r:
                    cover["offspec"].append(f"{sg.display_name} ({sg.offspec})")
                elif m and r in reg.roles_of(m)[1] and sg.role != r:
                    cover["flex"].append(sg.display_name)
                elif m and any(c.status in ("active", "planned") and not c.is_main and profile.spec(c.cls, c.spec).role == r for c in m.characters) and sg.role != r:
                    cover["alt"].append(sg.display_name)
            cover["sub"] = [sg.display_name for sg in subs if sg.role == r]
        coverable = sum(len(v) for v in cover.values())
        level = "green" if not n or have >= n else ("amber" if have + coverable >= n else "red")
        hint = " · ".join(f"+{len(v)} {k}: {', '.join(x.split(' (')[0] for x in v[:3])}" for k, v in cover.items() if v) if (n and have < n) else ""
        if n and have < n and not coverable:
            off = [m.display_name for m in reg.team_pool(team["key"]) if str(m.discord_id) not in ev.signups and any(profile.spec(c.cls, c.spec).role == r for c in m.active())]
            hint = ("not on sheet: " + ", ".join(off[:3])) if off else "nobody can cover → recruit"
        roles.append({"role": r, "have": have, "need": n, "level": level, "hint": hint, "cover": cover})
    buffs = []
    for b in profile.party_buffs():
        providers = [s.display_name for s in ins if b.provided_by(profile.spec(s.cls, s.spec))]
        buffs.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "name": b.short, "providers": providers})
    unresp = [m.display_name for m in reg.team_pool(team["key"]) if m.main and str(m.discord_id) not in ev.signups]
    hc_level = "green" if len(ins) >= size else ("amber" if len(ins) + len(subs) >= size else "red")
    # headcount is a 4-tuple (in, size, 0 [was tentative], sub): the cards and the pool data share the shape
    return {"headcount": (len(ins), size, 0, len(subs)), "headcount_level": hc_level, "roles": roles, "buffs": buffs, "unresponsive": unresp}


def health(reg: Registry, ev: RaidEvent, team: dict) -> list[tuple[str, str]]:
    """[(level, line)] text form of health_data, for logs and ops lines."""
    h = health_data(reg, ev, team)
    n, size, _legacy, subs = h["headcount"]
    out = [(h["headcount_level"], f"Headcount {n}/{size} in, {subs} sub")]
    for r in h["roles"]:
        if r["need"]:
            out.append((r["level"], f"{r['role'].title()}s {r['have']}/{r['need']}" + (f" · {r['hint']}" if r["hint"] else "")))
    missing = [b["name"] for b in h["buffs"] if not b["providers"]]
    if missing:
        out.append(("amber", "No provider for: " + ", ".join(missing)))
    if h["unresponsive"]:
        out.append(("amber" if len(h["unresponsive"]) > 3 else "green", f"No response from {len(h['unresponsive'])}"))
    return out


# ---------------------------------------------------------------- lock → propose

def players_for(reg: Registry, ev: RaidEvent) -> list[Player]:
    players = []
    for i, s in enumerate(sorted(ev.signups.values(), key=lambda s: s.updated_at)):
        if s.status == "out":
            continue
        c = reg.find(s.character)
        rank = c[1].rank if c else "unknown"
        tank_alt = None
        if s.offspec and reg.profile.spec(s.cls, s.offspec).role == "tank" and s.role != "tank":
            tank_alt = f"{s.character} offspec {s.offspec}"
        else:
            m = reg.members.get(s.discord_id)
            alt = next((c for c in (m.active() if m else []) if not c.is_main and reg.profile.spec(c.cls, c.spec).role == "tank"), None)
            if alt:
                tank_alt = f"{alt.label} ({alt.cls} {alt.spec})"
        players.append(Player(signup_name=s.display_name, pos=i + 1, status="signed" if s.status == "in" else "bench", cls=s.cls, spec=s.spec, role=s.role, offspec=s.offspec, character=s.character, map_confidence="high", unmapped=False, rank=rank, tank_capable_main=tank_alt))
    return players


def seat_bonus(reg: Registry, rs: RaidStore, ev: RaidEvent, players: list[Player]) -> dict[str, int]:
    """Per-player selection bonus from the raid's weights: rank, main over alt, sat out last window, signup order."""
    w = reg.raid_def(ev.instance)["weights"]
    lockout = int(reg.raid_def(ev.instance)["lockout_days"])
    prev = [e for e in rs.events.values() if e.instance == ev.instance and e.key != ev.key and e.state == "done" and ev.start - timedelta(days=lockout) <= e.start < ev.start]
    seated_prev = {p.signup_name for e in prev for p in e.seated()}
    signed_prev = {sg.display_name for e in prev for sg in e.signups.values() if sg.status == "in"}
    n = max(1, len(players))
    out = {}
    for p in players:
        b = int(w.get("rank", 0)) * (4 - RANK_ORDER.get(p.rank, 3))
        found = reg.find(p.character) if p.character else None
        if found and found[1].is_main:
            b += int(w.get("main", 0))
        if p.signup_name in signed_prev and p.signup_name not in seated_prev:
            b += int(w.get("sat_out", 0))
        b += round(int(w.get("signup_order", 0)) * (n - p.pos) / n)
        out[p.signup_name] = b
    return out


def _capable(players: list[Player], role: str, reg: Registry) -> int:
    n = 0
    for p in players:
        if p.role == role:
            n += 1
        elif p.offspec:
            try:
                n += reg.profile.spec(p.cls, p.offspec).role == role
            except KeyError:
                pass
    return n


def how_many_rosters(reg: Registry, players: list[Player], size: int, rb: dict) -> int:
    """How many full runs the Join answers support: bodies, and tank/healer minimums per run."""
    signed = [p for p in players if p.status == "signed"]
    n = min(MAX_ROSTERS_PER_SLOT, len(signed) // size)
    for role in ("tank", "healer"):
        if rb[role]["min"]:
            n = min(n, _capable(signed, role, reg) // rb[role]["min"])
    return max(1, n)


ORDINALS = ("first", "second", "third", "fourth", "fifth")


def split_reason(reg: Registry, players: list[Player], size: int, rb: dict) -> dict | None:
    """Why the Join answers don't make more runs, when the headcount allows more rosters than the tank/healer minimums
    do — or not even one roster meets them. None when the roles keep up with the bodies.
    {"bodies_allow": 2, "roles_allow": 1, "run": 2, "short": {"tank": 2, "healer": 2}} = enough people for 2 runs,
    run 2 is short 2 tanks and 2 healers; roles_allow 0 = the first run is already short (run 1)."""
    signed = [p for p in players if p.status == "signed"]
    bodies = min(MAX_ROSTERS_PER_SLOT, len(signed) // size)
    mins = {r: int(rb[r]["min"]) for r in ("tank", "healer") if rb[r]["min"]}
    have = {r: _capable(signed, r, reg) for r in mins}
    roles = min([have[r] // mins[r] for r in mins], default=MAX_ROSTERS_PER_SLOT)
    if roles >= max(1, bodies):
        return None
    run = roles + 1
    short = {r: mins[r] * run - have[r] for r in mins if mins[r] * run > have[r]}
    return {"bodies_allow": bodies, "roles_allow": roles, "run": run, "short": short}


def split_reason_text(reason: dict, role=None) -> str:
    """The reason as one line. `role(role, n)` formats a shortfall — the cards and the site pass role icon + number
    (the Iconography rule); the default spells it out for logs, MCP and the help context."""
    if role:
        need = "   ".join(role(r, n) for r, n in reason["short"].items())
    else:
        need = " and ".join(f"{n} {r}{'s' if n != 1 else ''}" for r, n in reason["short"].items())
    if reason["roles_allow"] == 0:
        return f"1 run is short {need}" if reason["bodies_allow"] <= 1 else f"Enough people for {reason['bodies_allow']} runs — even one is short {need}"
    nth = ORDINALS[reason["run"] - 1] if reason["run"] <= len(ORDINALS) else f"run {reason['run']}"
    return f"Enough people for {reason['bodies_allow']} runs — a {nth} is short {need}"


def run_split_reason(reg: Registry, ev: "RaidEvent") -> dict | None:
    size = run_size(reg, ev)
    return split_reason(reg, players_for(reg, ev), size, reg.role_bounds(ev.instance, size))


def leftovers(reg: Registry, ev: "RaidEvent", rosters: list[RosterResult]) -> list[Player]:
    """Joiners (Join answers) the roster(s) left out: not silently bench — the cards and the board list them apart.
    Only once the rosters are full (or locked): while an open board still has seats, the bank is just unplaced."""
    if not rosters:
        return []
    size = run_size(reg, ev)
    if ev.state == "open" and any(len(r.selected) < size for r in rosters):
        return []
    seated = {p.signup_name for r in rosters for p in r.selected}
    return [p for p in rosters[0].benched if p.status == "signed" and p.signup_name not in seated
            and (sg := signup_by_name(ev, p.signup_name)) is not None and sg.status == "in"]


def _can_cover(people: list[set[str]], need: list[str]) -> bool:
    """Distinct people for every role seat in `need` (bipartite matching, people → the roles they can play)."""
    match: dict[int, int] = {}  # person → seat

    def place(seat: int, seen: set[int]) -> bool:
        for i, roles in enumerate(people):
            if need[seat] in roles and i not in seen:
                seen.add(i)
                holder = next((s for s, p in match.items() if p == i), None)
                if holder is None or place(holder, seen):
                    match[seat] = i
                    return True
        return False

    return all(place(s, set()) for s in range(len(need)))


def another_run_hint(reg: Registry, ev: "RaidEvent", rosters: list[RosterResult], reason: dict | None) -> str | None:
    """One line, no action: could the leftovers + Bench answers + the roster's pool (not on the sheet, not away that
    day) make one more run if some came on an offspec or an alt? Only when the joiners alone fall short on roles."""
    if not reason or reason["roles_allow"] < 1:
        return None
    size = run_size(reg, ev)
    rb = reg.role_bounds(ev.instance, size)
    day = ev.start.date().isoformat()
    role_of = lambda cls, spec: reg.profile.spec(cls, spec).role  # noqa: E731
    main_only: list[set[str]] = []
    any_role: list[set[str]] = []

    def add(m: Member | None, main: str, off: set[str] = frozenset()) -> None:
        main_only.append({main})
        any_role.append({main} | set(off) | {role_of(c.cls, c.spec) for c in (m.active() if m else [])})

    seated = {p.signup_name for r in rosters for p in r.selected}
    for p in leftovers(reg, ev, rosters):
        sg = signup_by_name(ev, p.signup_name)
        add(reg.members.get(sg.discord_id) if sg else None, p.role, {role_of(p.cls, p.offspec)} if p.offspec else set())
    for sg in ev.by_status("sub"):
        if sg.display_name not in seated:
            add(reg.members.get(sg.discord_id), sg.role, {role_of(sg.cls, sg.offspec)} if sg.offspec else set())
    for m, c in reg.roster_pool(run_team(reg, ev)["key"]):
        if c and str(m.discord_id) not in ev.signups and not m.absent_on(day):
            add(m, role_of(c.cls, c.spec))
    need = [r for r in ("tank", "healer") for _ in range(int(rb[r]["min"] or 0))]
    if len(any_role) < size or not _can_cover(any_role, need):
        return None
    how = "" if _can_cover(main_only, need) else " with offspecs or alts"
    return f"Leftovers, bench and pool ({len(any_role)}) could make another run{how}."


def _solve_run(reg: Registry, rs: RaidStore, ev: RaidEvent, strategy: str | None = None, avoid: list[dict[str, int]] | None = None, time_limit: float = SOLVE_TIME_LIMIT_S, whatif: bool = True, avoid_groups: list[dict[str, int]] | None = None) -> tuple[list[Player], list[RosterResult], int]:
    """The joint solve behind propose(), autofill() and split previews: as many rosters as the signups support,
    the officers' layout as hard pins, weights as seat bonuses, the strategy shaping the objective."""
    players = players_for(reg, ev)
    raid_id = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
    rd = reg.raid_def(raid_id)
    size = run_size(reg, ev)
    rb = reg.role_bounds(raid_id, size)
    from .policy import PolicyStore

    cc = PolicyStore(rs.store, rs.key).comp_constraints()
    names = {p.character: p.signup_name for p in players} | {p.signup_name: p.signup_name for p in players}
    resolve = lambda n: names.get(n) or next((v for k, v in names.items() if k.lower() == n.lower()), None)  # noqa: E731
    by_uid = {str(sg.discord_id): sg.display_name for sg in ev.signups.values()}
    pin_in = [by_uid[u] for u, v in ev.pins.items() if v == "in" and u in by_uid]
    pin_out = [by_uid[u] for u, v in ev.pins.items() if v == "out" and u in by_uid]
    k = groups_per_roster(reg, size)
    layout = [[n for n in g if n in names] for g in (ev.layout or [])]
    placed = {n: gi for gi, g in enumerate(layout) for n in g}
    n_rosters = max(how_many_rosters(reg, players, size, rb), -(-len(layout) // k) if any(layout) else 1)
    strategy = strategy or ev.split_strategy or rd.get("split_policy") or "balanced"
    bonus = seat_bonus(reg, rs, ev, players)
    roster_bonus = None
    if strategy == "rotation":
        w = int(rd["weights"].get("sat_out", 2))
        lockout = int(rd["lockout_days"])
        prev = [e for e in rs.events.values() if e.instance == ev.instance and e.key != ev.key and e.state == "done" and ev.start - timedelta(days=lockout) <= e.start < ev.start]
        seated_first = {p.signup_name for e in prev for p in (e.all_rosters[0].selected if e.all_rosters else [])}
        signed_prev = {sg.display_name for e in prev for sg in e.signups.values() if sg.status == "in"}
        roster_bonus = {0: {p.signup_name: 3 * w for p in players if p.signup_name in signed_prev and p.signup_name not in seated_first}}
    pool = {p.signup_name for p in players}
    opts = solver.SolveOptions(
        time_limit_s=time_limit, raid_size=size,
        keep_together=tuple((resolve(a), resolve(b)) for a, b in cc["keep_together"] if resolve(a) and resolve(b)),
        keep_apart=tuple((resolve(a), resolve(b)) for a, b in cc["keep_apart"] if resolve(a) and resolve(b)),
        force_in=tuple(x for x in pin_in + list(placed) + [resolve(n) for n in cc["never_bench"]] if x and x in pool and x not in pin_out),
        force_out=tuple(x for x in pin_out + [resolve(n) for n in cc["always_bench"]] if x and x in pool and x not in placed),
        pins={n: gi for n, gi in placed.items() if gi < k * n_rosters} or None,
        prefer_group={resolve(n): g for n, g in cc["prefer_group"].items() if resolve(n)},
        role_min=cc["role_min"] or {r: rb[r]["min"] for r in ("tank", "healer") if rb[r]["min"]} or None,
        role_max={r: rb[r]["max"] for r in ("tank", "healer") if rb[r].get("max")},
        bonus=bonus,
    )
    # "another one": for a split the rosters must differ, for a single run the groups must
    rosters = solver.solve_rosters(reg.profile, players, raid_id, opts, rosters=n_rosters, strategy=strategy, roster_bonus=roster_bonus, avoid=avoid if n_rosters > 1 else None, avoid_groups=avoid_groups if n_rosters == 1 else None)
    # bench what-ifs re-solve the model per benched player (the bench hangs off rosters[0]): capped by explain.WHATIF_*,
    # only at lock (never for previews), skipped for big splits / sheets
    do_whatif = whatif and explain.whatif_allowed(n_rosters, len(players))
    rosters = [explain.annotate(reg.profile, players, raid_id, r, whatif=do_whatif and i == 0) for i, r in enumerate(rosters)]
    return players, rosters, n_rosters


def propose(reg: Registry, rs: RaidStore, ev: RaidEvent, save: bool = True) -> tuple[list[Player], RosterResult]:
    """Roster(s) for a sheet from its signups: one joint solve over as many runs as the Join answers support,
    weights + officer pins + the officers' board, the raid's role bounds, the comp policy, the run's split strategy."""
    players, rosters, _ = _solve_run(reg, rs, ev)
    seated = {p.signup_name for r in rosters for p in r.selected}
    rosters[0].benched = [p for p in players if p.signup_name not in seated]
    ev.rosters = rosters
    if save:  # the state is the caller's (lock_run sets "locked"); a proposal on its own just stores the rosters
        reason = run_split_reason(reg, ev)
        ev.log.append("proposed " + " + ".join(str(len(r.selected)) for r in rosters) + f" in / {len(rosters[0].benched)} bench"
                      + (f" · {split_reason_text(reason)}" if reason else ""))
        rs.save(ev, "proposed")
    return players, rosters[0]


def solver_error_text(e: Exception) -> str:
    """A sentence an officer can act on instead of the solver's status word."""
    msg = str(e)
    if "INFEASIBLE" in msg or "no roster found" in msg:
        return "no roster satisfies the rules: check the raid's tank/healer minimums against who joined, pins that overfill a group, and keep-apart pairs"
    if "UNKNOWN" in msg or "time" in msg.lower():
        return "the solver ran out of time before finding a roster; try again or reduce pins"
    return f"the solver failed: {msg}"


def split_preview(reg: Registry, rs: RaidStore, ev: RaidEvent, strategy: str, avoid: list[list[list[str]]] | None = None) -> tuple[list[list[str]], list[RosterResult]]:
    """A split under `strategy` for the modal: the layout (flat groups) and the rosters, nothing saved.
    `avoid` = earlier previews (flat groups) the answer must differ from."""
    k = groups_per_roster(reg, run_size(reg, ev))
    prev = [{n: gi // k for gi, g in enumerate(lay) for n in g} for lay in (avoid or [])]
    prev_g = [{n: gi for gi, g in enumerate(lay) for n in g} for lay in (avoid or [])]
    trial = ev.model_copy(deep=True)
    players, rosters, _ = _solve_run(reg, rs, trial, strategy=strategy, avoid=prev, time_limit=PREVIEW_TIME_LIMIT_S, whatif=False, avoid_groups=prev_g)
    seated = {p.signup_name for r in rosters for p in r.selected}
    rosters[0].benched = [p for p in players if p.signup_name not in seated]
    return [list(g) for r in rosters for g in r.groups], rosters


# ---------------------------------------------------------------- the officers' board (bank + groups) before and after lock

def groups_per_roster(reg: Registry, size: int) -> int:
    return max(1, -(-size // int(reg.profile.comp_rules["group_size"])))


def board_layout(reg: Registry, ev: RaidEvent) -> list[list[str]]:
    """The board as it stands, flat groups across rosters: the locked roster(s), else the officers' layout.
    Always padded to whole rosters so (roster, group) addresses are stable."""
    n = groups_per_roster(reg, run_size(reg, ev))
    if ev.state != "open" and ev.all_rosters:
        flat = [list(g) for r in ev.all_rosters for g in (list(r.groups) + [[] for _ in range(n - len(r.groups))])[:max(n, len(r.groups))]]
    else:
        flat = [list(g) for g in (ev.layout or [])]
    want = max(n, -(-len(flat) // n) * n)
    return flat + [[] for _ in range(want - len(flat))]


def board_rev(reg: Registry, ev: RaidEvent) -> str:
    """A fingerprint of the board (groups + pins + state). A whole-board write carrying an older one is stale."""
    import hashlib
    import json

    blob = json.dumps([ev.state, board_layout(reg, ev), sorted(ev.pins.items())], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def apply_move(reg: Registry, ev: RaidEvent, name: str, to: tuple[int, int] | None) -> list[list[str]]:
    """One drag applied to the CURRENT board (so concurrent officers merge instead of overwriting each other):
    `to` = (flat group index, position) or None for the bank. Dropping on someone swaps (they take the mover's old
    place, or go to the bank when the mover came from it). Raises ValueError when the group is full or the name
    is not a joiner. Returns the new flat layout; the caller saves it the way a whole-board write would be."""
    if signup_by_name(ev, name) is None or signup_by_name(ev, name).status != "in":
        raise ValueError(f"{name} hasn't joined this run")
    layout = board_layout(reg, ev)
    gsize = int(reg.profile.comp_rules["group_size"])
    origin = next(((gi, g.index(name)) for gi, g in enumerate(layout) if name in g), None)
    if origin:
        layout[origin[0]].remove(name)
    if to is None:
        return layout
    gi, pos = to
    while gi >= len(layout):
        layout.append([])
    g = layout[gi]
    if origin and origin[0] == gi and pos > origin[1]:
        pos -= 1  # the list got shorter when the mover left it
    if 0 <= pos < len(g) and (not origin or origin[0] != gi):  # dropped on someone in another group: swap
        other = g[pos]
        g[pos] = name
        if origin:
            layout[origin[0]].insert(min(origin[1], len(layout[origin[0]])), other)
        return layout
    if len(g) >= gsize:
        raise ValueError(f"group {gi % groups_per_roster(reg, run_size(reg, ev)) + 1} is full")
    g.insert(max(0, min(pos, len(g))), name)
    return layout


def board_rosters(reg: Registry, ev: RaidEvent, layout: list[list[str]], size: int) -> list[RosterResult]:
    """RosterResults straight from a layout (no solver): one per `groups_per_roster` groups. Unplaced joiners are bench."""
    players = {p.signup_name: p for p in players_for(reg, ev)}
    n = groups_per_roster(reg, size)
    boards = [layout[i:i + n] for i in range(0, max(len(layout), n), n)] or [[[] for _ in range(n)]]
    placed = {m for g in layout for m in g}
    out = []
    for b in boards:
        groups = [[m for m in g if m in players] for g in b] + [[] for _ in range(n - len(b))]
        selected = [players[m] for g in groups for m in g]
        counts = {r: sum(1 for p in selected if p.role == r) for r in ROLES}
        out.append(RosterResult(selected=selected, benched=[], groups=groups, group_reports=[], objective=0, synergy_value=0, role_counts=counts, advisories=[]))
    if out:
        out[0].benched = [p for m, p in players.items() if m not in placed and p.status == "signed"]
    return out


def autofill(reg: Registry, rs: RaidStore, ev: RaidEvent) -> list[list[str]]:
    """Solver fills whatever the officers left empty (their placements are fixed) and returns the full layout."""
    trial = ev.model_copy(deep=True)
    propose(reg, rs, trial, save=False)
    return [list(g) for r in trial.all_rosters for g in r.groups]


def apply_layout_locked(reg: Registry, rs: RaidStore, ev: RaidEvent, layout: list[list[str]]) -> tuple[list[Signup], list[str]]:
    """After lock the board is the roster: group moves are free; someone dragged in from the bench is a substitution
    (returned so the caller asks them to confirm); someone dragged out has their seat freed. One commit."""
    with rs.store.batch(f"{rs.key}: raid {ev.key}: board updated"):
        return _apply_layout_locked(reg, rs, ev, layout)


def _apply_layout_locked(reg: Registry, rs: RaidStore, ev: RaidEvent, layout: list[list[str]]) -> tuple[list[Signup], list[str]]:
    n = groups_per_roster(reg, run_size(reg, ev))
    before = {p.signup_name for p in ev.seated()}
    after = {m for g in layout for m in g}
    removed = [m for m in before if m not in after]
    for m in removed:
        free_seat(reg, rs, ev, m, "moved to bench")
    players = {p.signup_name: p for p in players_for(reg, ev)}
    added: list[Signup] = []
    for i in range(max(len(ev.all_rosters), -(-len(layout) // n))):
        groups = [[m for m in g if m in players] for g in layout[i * n:(i + 1) * n]] + [[] for _ in range(n - len(layout[i * n:(i + 1) * n]))]
        if i >= len(ev.rosters):
            ev.rosters.append(RosterResult(selected=[], benched=[], groups=groups, group_reports=[], objective=0, synergy_value=0, role_counts={}, advisories=[]))
        r = ev.rosters[i]
        r.groups = groups
        want = [m for g in groups for m in g]
        keep = {p.signup_name: p for p in r.selected if p.signup_name in want}
        for m in want:
            if m not in keep:
                sg = next((s for s in ev.signups.values() if s.display_name == m), None)
                if sg and m not in before:
                    added.append(sg)
                keep[m] = players[m]
        r.selected = [keep[m] for m in want]
        r.role_counts = {role: sum(1 for p in r.selected if p.role == role) for role in ROLES}
    ev.rosters = [r for r in ev.rosters if r.selected or r is ev.rosters[0]]
    seated = {p.signup_name for p in ev.seated()}
    ev.rosters[0].benched = [p for m, p in players.items() if m not in seated and p.status == "signed"]
    for sg in added:
        sg.status, sg.source, sg.updated_at = "in", "officer", now()
    ev.log.append("board: " + (f"+{', '.join(s.display_name for s in added)} " if added else "") + (f"-{', '.join(removed)}" if removed else "") or "board: groups moved")
    rs.save(ev, "board updated")
    return added, removed


# ---------------------------------------------------------------- after lock: confirmations and freed seats

def confirmations(reg: Registry, ev: RaidEvent) -> list[dict]:
    """One row per seated player: their confirmation state for this run (placement ask on the run's roster key)."""
    out = []
    for i, r in enumerate(ev.all_rosters):
        for p in r.selected:
            found = reg.find(p.character) if p.character else None
            m = found[0] if found else next((mm for mm in reg.members.values() if mm.display_name == p.signup_name), None)
            ask = next((a for a in reversed(m.placement_asks) if a["roster"] == ev.team), None) if m else None
            out.append({"uid": str(m.discord_id) if m else None, "display_name": p.signup_name, "character": p.character or p.signup_name, "cls": p.cls, "spec": p.spec, "role": p.role,
                        "roster": i + 1, "answer": (ask or {}).get("answer"), "asked_at": (ask or {}).get("asked_at"), "channel": (ask or {}).get("channel", "dm")})
    return out


def withdraw_confirmations(reg: Registry, ev: RaidEvent, why: str) -> list[str]:
    """A cancelled run: every open placement ask on its roster key is closed as `why` so nobody is left with a
    pending question on the site or in the tallies. Returns who was waiting."""
    gone = []
    for m in reg.members.values():
        asks = [a for a in m.placement_asks if a["roster"] == ev.team and a.get("answer") is None]
        if asks:
            for a in asks:
                a["answer"], a["answered_at"] = why, now()
            reg.save(m, f"{m.display_name}'s confirmation for {ev.team} withdrawn ({why})")
            gone.append(m.display_name)
    return gone


def free_seat(reg: Registry, rs: RaidStore, ev: RaidEvent, display_name: str, why: str) -> Player | None:
    """Take someone off the locked roster (decline, callout, absence, missed confirmation): seat opens, signup → out."""
    hit = ev.seat_of(display_name)
    if not hit:
        return None
    i, p = hit
    r = ev.all_rosters[i]
    r.selected = [x for x in r.selected if x.signup_name != display_name]
    r.groups = [[n for n in g if n != display_name] for g in r.groups]
    p.status = "bench"
    ev.all_rosters[0].benched.append(p)
    sg = next((s for s in ev.signups.values() if s.display_name == display_name), None)
    if sg:
        sg.status, sg.source, sg.updated_at = "out", why, now()
    ev.log.append(f"seat freed: {display_name} ({why})")
    rs.save(ev, f"seat freed {display_name}")
    return p


def seat_player(reg: Registry, ev: RaidEvent, sg: Signup) -> int | None:
    """Put a signup into the first roster with a free seat (after a fill yes or an officer add). Returns the roster index."""
    size = run_size(reg, ev)
    found = reg.find(sg.character)
    p = Player(signup_name=sg.display_name, pos=len(ev.signups), status="signed", cls=sg.cls, spec=sg.spec, role=sg.role, offspec=sg.offspec, character=sg.character, rank=found[1].rank if found else "unknown")
    for i, r in enumerate(ev.all_rosters):
        if len(r.selected) < size:
            r.selected.append(p)
            r.benched = [x for x in r.benched if x.signup_name != p.signup_name]
            smallest = min(range(len(r.groups)), key=lambda g: len(r.groups[g])) if r.groups else None
            if smallest is not None and len(r.groups[smallest]) < reg.profile.comp_rules["group_size"]:
                r.groups[smallest].append(p.signup_name)
            elif r.groups is not None:
                r.groups.append([p.signup_name])
            ev.log.append(f"seated {sg.display_name} in roster {i + 1}")
            return i
    return None


def expire_confirmations(reg: Registry, rs: RaidStore, ev: RaidEvent) -> list[str]:
    """Past confirm_by: unanswered confirmations count as out and their seats open. One commit for the lot."""
    if not ev.confirm_by or datetime.fromisoformat(ev.confirm_by) > datetime.now(ev.start.tzinfo):
        return []
    gone = []
    with rs.store.batch(f"{rs.key}: raid {ev.key}: confirmations expired"):
        for row in confirmations(reg, ev):
            if row["answer"] is None and row["uid"]:
                m = reg.members.get(int(row["uid"]))
                ask = next((a for a in reversed(m.placement_asks) if a["roster"] == ev.team and a.get("answer") is None), None) if m else None
                if ask:
                    ask["answer"], ask["answered_at"] = "expired", now()
                    reg.save(m, f"{m.display_name} didn't confirm {ev.team} in time")
                if reg.on_roster(m, ev.team) if m else False:
                    reg.roster_remove(m.discord_id, ev.team, "no-confirm")
                free_seat(reg, rs, ev, row["display_name"], "no-confirm")
                gone.append(row["display_name"])
    return gone
