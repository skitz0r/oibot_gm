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
from .registry import (ADHOC_SCHEDULE, ADHOC_WEIGHTS, DEFAULT_RAID_SIZE, DEFAULT_SCHEDULE, RAID_DEFAULTS, RAID_WEIGHT_DEFAULTS, Member, Registry,
                       RegisteredCharacter, RegistryError, can_cover, now, schedule_class, series_key)
from .roster import explain, solver
from .store import GitStore

log = logging.getLogger(__name__)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
STATUSES = ("in", "sub", "out")  # Join / Bench / No thanks (legacy "tentative" is read as "in")
LABELS = {"in": "Join", "sub": "Bench", "out": "No thanks"}
# a member's own Join / Bench after lock: the sheet's SignupButton and plain text (configops.authorize) refuse it in these words
LOCKED_JOIN = "The roster is locked — answer your confirmation DM, or press Can't make it on the sheet."
STATES = ("open", "locked", "done", "cancelled")
LEGACY_STATES = {"proposed": "locked", "accepted": "locked"}  # pre-2026-09 files
MAX_ROSTERS_PER_SLOT = 4
RUN_LISTENERS: list = []  # callables (guild_key, run_key) told after every RaidStore.save
# callables (guild_key, run_key, change) told when a locked run's seats change (on_roster_change, design §3.1): change =
# {"added": [uid|name], "removed": [uid|name], "changed": [uid|name]}; the loot council reads its candidates live from
# the run and uses this to say "X rostered — now a candidate". May be called from a worker thread (the web).
ROSTER_LISTENERS: list = []
FILL_OVERASK = 1  # ask one more person than the shortfall per batch
FILL_MAX_OPEN = 3  # never more than this many unanswered asks per event
FILL_ASK_HOURS_DEFAULT = RAID_DEFAULTS["fill_ask_hours"]
SWAP_KINDS = ("flex", "offspec", "alt")  # asks that change what someone already coming plays (a role swap: D3)
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
    # when this Join began: kept while the member stays Join (pressing Join again, another character, a yes to an
    # officer's ask), reset by a fresh Join after Bench / No thanks. Ad hoc first come (D4) orders by it, never by
    # updated_at (the last touch). None = not Join, or a file from before it was stored (updated_at stands in)
    joined_at: Optional[str] = None

    @property
    def order_at(self) -> str:
        """The signup's place in the queue: when the Join began (joined_at), else when it was last set."""
        return self.joined_at or self.updated_at


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
    kind: str  # sub | regular | unanswered | other (bodies: the bench, the series' regulars who haven't answered — "unanswered" = every main on an ad hoc run or a series with no regulars —, everyone else) | flex | offspec | alt (switches)
    role: str  # the role this ask covers
    character: str  # what they'd play
    spec: str
    reason: str  # "short 1 healer", "short 2" — the member's DM shows it
    why: Optional[str] = None  # the code-written reason officers see (role_plan): "short 1 healer · Ana's declared flex Holy · DPS stays 6 of 5 minimum"
    asked_at: str = Field(default_factory=now)
    answer: Optional[str] = None  # yes | no | expired | released (the paired ask fell through) | withdrawn (an officer closed it) | cancelled (the run was)
    answered_at: Optional[str] = None
    answered_by: Optional[str] = None  # who answered when it wasn't the member (an officer on the site / MCP / plain text)
    expires_at: Optional[str] = None  # ISO: no answer by then counts as no
    pair: Optional[int] = None  # discord_id of the ask this one is tied to (a swap and the backfill covering the vacated seat)
    vacates: Optional[str] = None  # swaps: the role the person leaves
    phase: Literal["prelock", "fill"] = "fill"  # a suggestion an officer sent before the lock (a yes changes the signup, seats nobody) or a fill ask after it
    approved_by: Optional[str] = None  # the officer whose Ask / Send sent it (every pre-lock ask; D3: a recurring run's role swaps after lock); None = the fill engine on its own
    held: bool = False  # answered yes, but its tied partner hasn't answered yet: nothing changed (a pair needs both yeses)
    # where the ask waits (mirrors placement asks): "dm" = a DM card with the two buttons; "web" = on the member's Me page
    # (DMs off, or the DM failed — a failed DM is a CHANNEL change, never an answer: same deadline, still in play)
    channel: Literal["dm", "web"] = "dm"
    dm_channel_id: Optional[int] = None  # the DM card, so it can be edited to its final state when the ask closes
    dm_message_id: Optional[int] = None
    # D14: an ask sent IN PARALLEL with this member's (a DMs-off candidate in the last hours before start can't be counted
    # on to see the Me page in time): the discord_id of that primary ask. Both stay open, the first yes takes the seat and
    # the other closes (`released`); while the primary is in play this twin counts for nothing (parallel_shadowed)
    parallel_of: Optional[int] = None

    @field_validator("kind", mode="before")
    @classmethod
    def _old_tiers(cls, v):  # stored asks from before the tier rename read as the new names
        return {"pool": "regular", "other_roster": "other"}.get(v, v)

    @property
    def open(self) -> bool:
        return self.answer is None

    @property
    def pending(self) -> bool:
        """Still in play: unanswered, or a yes held until its tied partner answers (counts as an ask out)."""
        return self.answer is None or self.held

    @property
    def swap(self) -> bool:
        return self.kind in SWAP_KINDS


class RaidEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")  # older files carry a `roster` key (folded into `rosters` below)

    key: str  # <run roster key>-<YYYY-MM-DD>
    team: str  # roster key; for cadence runs an ephemeral roster named like the run (bd-1209-1930)
    instance: Optional[str] = None
    schedule: Optional[str] = None  # the schedule id it was opened from (None: the raid's own run times; "adhoc": a one-off)
    # stamped at open from the run dict (roster redesign Phase 1), so a later schedule edit never changes a run in flight:
    series: Optional[str] = None  # recurring: "<raid>:<schedule>" (whose regulars are expected); ad hoc: None
    kind: Optional[Literal["recurring", "adhoc"]] = None  # None = a file from before classes (read by run_kind as its schedule says)
    test: bool = False  # a test-bench run (puppets + the tester), identifiable after its run dict is cleaned up
    regulars: list[int] = Field(default_factory=list)  # the series' regulars when the sheet opened (history; behaviour reads them live)
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
    # labelled read-only copies a member posted with /raid sheet (D21): [channel id, message id] — never the live sheet
    # (channel_id / message_id stay where they are); refresh_sheet edits them too, without buttons
    mirror_messages: list[list[int]] = Field(default_factory=list)
    thread_id: Optional[int] = None
    health_posted: bool = False
    health_early: bool = False  # the early-warning health card went up when the sheet opened (the regulars can't cover a minimum)
    nudged: list[int] = Field(default_factory=list)
    fill_asks: list[FillAsk] = Field(default_factory=list)
    fill_state: str = "idle"  # idle | asking | filled | exhausted
    lock_error: Optional[str] = None  # the last failed lock attempt (sheet stays open); cleared on success
    lock_tried_at: Optional[str] = None
    # dual interfaces phase 2 (design §5.39): raid-night state
    confirmations_sent_at: Optional[str] = None  # the lock's Confirm / Can't fan-out finished (None on a locked run = not yet sent / interrupted)
    # the discord ids rostered at the lock flip — the only seats the lock's fan-out (and its recovery step) asks; a seat
    # that came later (a fill yes, an officer's add) carries its own ask or none. None = locked before it was recorded
    lock_seats: Optional[list[int]] = None
    autofill_paused: bool = False  # the fill engine sends nothing on its own for this run (an officer's explicit send still works, D18)
    autofill_paused_by: Optional[str] = None
    reminded_at: Optional[str] = None  # the last "Remind the unconfirmed" (once per hour per run)
    reminded_by: Optional[str] = None
    closed_at: Optional[str] = None  # when it became done / cancelled (cleanup_ephemeral's grace, reopen's window)
    prev_state: Optional[str] = None  # the state before done / cancelled (reopen_run restores it)
    last_proposal: Optional[dict] = None  # the last lock preview / split shown on any surface: {layout, strategy, rev, by, by_id, at, kind} — the same officer's plain-text "use it" falls back to it (configops._propose)
    log: list[str] = Field(default_factory=list)
    log_at: Optional[list[str]] = None  # ISO stamp per log line (RaidStore.save stamps new lines; "" = from before stamps were kept)
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
        return sorted([s for s in self.signups.values() if s.status == status], key=lambda s: s.order_at)

    @property
    def all_rosters(self) -> list[RosterResult]:
        return self.rosters  # kept for older call sites; the same list, not a copy

    def seated(self) -> list[Player]:
        return [p for r in self.all_rosters for p in r.selected]

    def seat_of(self, who) -> tuple[int, Player] | None:
        """(roster index, Player) of the seat `who` holds, or None. `who` is a Member (or anything with `discord_id`), a
        Discord id, or a display name. Seats are keyed by the member's id (Player.discord_id, else the run's own signup
        under the seated name: `player_uid`), so a nick change mid-run keeps the seat; a bare name matches a seat only
        when no id can be found for it (a run file from before ids were stored)."""
        uid = seat_uid(self, who)
        name = who if isinstance(who, str) else None
        for i, r in enumerate(self.all_rosters):
            for p in r.selected:
                if uid is not None and player_uid(self, p) == uid:
                    return i, p
                if uid is None and name is not None and p.signup_name == name:
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


def run_settings(rd: dict) -> dict:
    """A raid (or schedule) definition's timing and behaviour in the run dict's words — what ensure_run stamps on a
    run, and the fallback for a run dict that lacks one (a bare stand-in, an old file)."""
    lead, lock = float(rd.get("signup_lead_hours", RAID_DEFAULTS["signup_lead_hours"])), float(rd.get("lock_hours_before", RAID_DEFAULTS["lock_hours_before"]))
    nudge = float(rd["nudge_hours_before"]) if rd.get("nudge_hours_before") is not None else (lead + lock) / 2  # registry: halfway between open and lock
    return {"cutoff_soft_hours": nudge, "cutoff_hard_hours": lock, "open_days_before": lead / 24,
            "reminders": "dm" if rd.get("nudge", RAID_DEFAULTS["nudge"]) else "none",
            "open_dm": bool(rd.get("open_dm", RAID_DEFAULTS["open_dm"])), "autofill": bool(rd.get("autofill", RAID_DEFAULTS["autofill"]))}


def team_setting(team: dict, key: str):
    """A run's setting: the run dict's own value, else the guild-wide RAID_DEFAULTS in the run dict's words."""
    return team[key] if key in team else run_settings(RAID_DEFAULTS).get(key)


def run_team(reg: Registry, ev: "RaidEvent") -> dict:
    """The roster dict a run belongs to; a bare stand-in when the ephemeral roster is gone (cleanup, old files)."""
    if t := reg.config.team(ev.team):
        return t
    rd = reg.raid_def(ev.instance)  # the stand-in reads the raid's own settings (no hard-coded run defaults)
    return {"key": ev.team, "size": int(rd.get("size") or DEFAULT_RAID_SIZE), **run_settings(rd)}


def run_size(reg: Registry, ev: "RaidEvent") -> int:
    """Seats per roster for a run: the roster's size, else the raid's, else DEFAULT_RAID_SIZE."""
    team = reg.config.team(ev.team) or {}
    return int(team.get("size") or reg.raid_def(ev.instance).get("size") or DEFAULT_RAID_SIZE)


def run_close_at(reg: Registry, ev: "RaidEvent") -> datetime:
    """When the scheduler marks a locked run done: start + the raid's duration + CLOSE_AFTER_HOURS."""
    return ev.start + timedelta(hours=float(reg.raid_def(ev.instance).get("duration_hours") or RAID_DEFAULTS["duration_hours"]) + CLOSE_AFTER_HOURS)


def abbr(instance: str) -> str:
    return "".join(w[0] for w in instance.split("_"))[:4]


# A test run's key space (dual interfaces phase 2, B9): every test run's key ends '-test' ('bd-1209-1930-test'), so a
# rehearsal opened the same minute as a real run of the raid's own run times never shares its roster or its sheet
# (before, both were 'bd-1209-1930' and the puppets' answers landed on the real sheet). Not a schedule: schedule_def_of
# never sees it — ensure_run reads the raid's own cadence and only the key carries the suffix.
TEST_SCHEDULE = "test"


class RunKeyClash(RegistryError):
    """A run's key is held by a run of the other kind (a rehearsal's roster or sheet from before the '-test' key space
    at a real run's key, or the reverse): nothing was opened. The scheduler reports it ONCE per key as an ops warn
    naming the fix (clear the test bench), never as a minute-by-minute error alert."""


def run_key(instance: str, start: datetime, schedule: str | None = None, *, test: bool = False) -> str:
    """The run's roster key: 'bd-1209-1930'. A run from a schedule other than the raid's own run times carries the
    schedule id ('bd-1209-1930-alt'), so two schedules starting the same minute never share a run; a test run ends in
    TEST_SCHEDULE ('bd-1209-1930-test', 'bd-1209-1930-alt-test'): `test=True`, or `schedule=TEST_SCHEDULE`."""
    base = f"{abbr(instance)}-{start.strftime('%m%d-%H%M')}"
    key = base if schedule in (None, "", DEFAULT_SCHEDULE) else f"{base}-{schedule}"
    return f"{key}-{TEST_SCHEDULE}" if test and schedule != TEST_SCHEDULE else key


def event_key(instance: str, start: datetime, schedule: str | None = None, *, test: bool = False) -> str:
    """The key open_event gives the run's sheet: roster key + the start's date (`test`: a test run's '-test' key)."""
    return f"{run_key(instance, start, schedule, test=test)}-{start.date().isoformat()}"


def schedule_kw(schedule: str | None) -> dict:
    """`schedule=` for open_run_and_post, only when the run comes from a schedule other than the raid's own run times."""
    return {"schedule": schedule} if schedule not in (None, "", DEFAULT_SCHEDULE) else {}


def slot_starts(reg: Registry, instance: str, now: datetime, horizon_hours: float) -> list[tuple[str, datetime]]:
    """(slot, start) for every occurrence of the raid's own slots (the `default` schedule) within `horizon_hours` of
    now (and never before first_open)."""
    rd = reg.raid_def(instance)
    return _weekly_starts(reg, rd, rd.get("slots") or [], now, horizon_hours)


def first_open_of(reg: Registry, rd: dict) -> datetime | None:
    """An effective raid dict's first_open as an aware datetime (Registry.first_open, for a draft that isn't saved)."""
    raw = rd.get("first_open")
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=reg.tz)


def _weekly_starts(reg: Registry, rd: dict, slots, now: datetime, horizon_hours: float) -> list[tuple[str, datetime]]:
    fo = first_open_of(reg, rd)
    out = []
    for slot in slots:
        try:
            t = next_raid_time(slot, reg.config.timezone, after=now - timedelta(minutes=1))
        except ValueError:
            continue
        while t <= now + timedelta(hours=horizon_hours):
            if fo is None or t >= fo:
                out.append((slot, t))
            t += timedelta(days=7)
    return sorted(out, key=lambda x: x[1])


def _lockout_starts(reg: Registry, rd: dict, sched: dict, now: datetime, horizon_hours: float) -> list[datetime]:
    """Day N (1 = the reset's calendar day) of every lockout window at the schedule's time, windows counted from
    first_open every lockout_days calendar days — so a 3- or 5-day reset is followed exactly. Wall-clock times are
    resolved DST-safely (a time the clocks skip moves past the gap; one that happens twice takes the first)."""
    from datetime import time as _time

    from .wizard import resolve_local

    fo = first_open_of(reg, rd)
    if fo is None or not sched.get("days") or not sched.get("time"):
        return []
    z = reg.tz
    lockout = int(rd.get("lockout_days") or 7)
    h, m = (int(x) for x in str(sched["time"]).split(":"))
    d0 = fo.astimezone(z).date()
    lo, hi = now - timedelta(minutes=1), now + timedelta(hours=horizon_hours)
    k = max(0, (lo.astimezone(z).date() - d0).days // lockout - 1)
    out = []
    while True:
        wstart = d0 + timedelta(days=k * lockout)
        if datetime.combine(wstart, _time(0, 0), tzinfo=z) > hi:
            break
        for n in sched["days"]:
            t, _note = resolve_local(datetime.combine(wstart + timedelta(days=int(n) - 1), _time(h, m)), z)
            if lo < t <= hi and t >= fo:
                out.append(t)
        k += 1
    return sorted(out)


def schedule_starts(reg: Registry, instance: str, sched: dict, now: datetime, horizon_hours: float, rd: dict | None = None) -> list[datetime]:
    """One schedule's run starts within `horizon_hours` of now. A pickup template never has any: it opens on demand.
    `rd`: an effective raid dict to read first_open / lockout_days from (a wizard's unsaved draft); default the stored raid."""
    rd = rd if rd is not None else reg.raid_def(instance)
    if sched.get("kind") == "lockout":
        return _lockout_starts(reg, rd, sched, now, horizon_hours)
    if sched.get("kind") == "pickup":
        return []
    return [t for _s, t in _weekly_starts(reg, rd, sched.get("slots") or [], now, horizon_hours)]


def upcoming(reg: Registry, instance: str, now: datetime, horizon_hours: float | None = None, *, lead: bool = False) -> list[tuple[dict, datetime]]:
    """(schedule, start) for every ACTIVE schedule's runs, soonest first. `lead=True`: each schedule looks only as far
    ahead as its own signup lead (what the scheduler opens now); else `horizon_hours` for all."""
    out = []
    for s in reg.schedules(instance):
        if not s.get("active", True) or s["kind"] == "pickup":
            continue
        h = float(reg.schedule_def(instance, s["id"])["signup_lead_hours"]) if lead else float(horizon_hours or 0)
        out += [(s, t) for t in schedule_starts(reg, instance, s, now, h)]
    return sorted(out, key=lambda x: (x[1], x[0]["id"] != DEFAULT_SCHEDULE))


def next_runs_of(reg: Registry, instance: str, sched: dict, n: int = 3, now: datetime | None = None, rd: dict | None = None,
                 min_days: float = 0) -> list[datetime]:
    """A schedule's next `n` starts (active or not), for "the next three runs" on every surface; never before the raid
    first opens (before that: its first runs, not an empty list). `min_days`: look at least this far ahead (the
    forecast's four weeks must not be cut by a short lockout's horizon)."""
    rd = rd if rd is not None else reg.raid_def(instance)
    now = now or reg.now_local()
    fo = first_open_of(reg, rd)
    base = max(now, fo) if fo else now
    horizon = 24 * max(OPEN_HORIZON_DAYS, 8 * int(rd.get("lockout_days") or 7), min_days)
    return schedule_starts(reg, instance, sched, base, horizon, rd)[:n]


FORECAST_DAYS = 28  # the regulars forecast looks this far ahead (from today, or from the raid's first opening before then)
FORECAST_MAX = 8  # and never lists more starts than this


def series_forecast(reg: Registry, key: str, now: datetime | None = None) -> list[dict]:
    """A recurring series' 4-week forecast: each upcoming start in the next FORECAST_DAYS (counted from the raid's
    first opening while it hasn't opened) with what its regulars minus recorded absences leave short of the tank /
    healer minimums. [{start (ISO), day ('Tue 15 Dec'), when (12-hour), expected, seats, short {role: n}, away
    [names]}]. Empty while the series has no regulars or is paused. Computed in code; officers see a hole a week
    ahead instead of at reminder time."""
    rid, _, sid = key.partition(":")
    regs = reg.regulars(key)
    if not regs or rid not in reg.profile.raids:
        return []
    sched = next((x for x in reg.schedules(rid) if x["id"] == sid), None)
    if sched is None or not sched.get("active", True) or schedule_class(sched["kind"]) != "recurring":
        return []
    rd = reg.raid_def(rid)
    starts = next_runs_of(reg, rid, sched, n=FORECAST_MAX, now=now, rd=rd, min_days=2 * FORECAST_DAYS)  # the first start, then 4 weeks from it
    if not starts:
        return []
    until = starts[0] + timedelta(days=FORECAST_DAYS)
    size = int(reg.schedule_def(rid, sid).get("size") or DEFAULT_RAID_SIZE)
    n = int(sched.get("rosters") or 1)
    bounds = reg.role_bounds(rid, size)
    need = {r: int(bounds[r]["min"] or 0) * n for r in ("tank", "healer")}
    out = []
    for t in starts:
        if t >= until:
            break
        day = t.astimezone(reg.tz).date().isoformat()
        here = [c for m, c in regs if not m.absent_on(day)]
        counts = {r: sum(1 for c in here if reg.profile.spec(c.cls, c.spec).role == r) for r in need}
        lt = t.astimezone(reg.tz)
        out.append({"start": t.isoformat(), "day": f"{lt:%a} {lt.day} {lt:%b}", "when": reg.local12(t), "expected": len(here), "seats": size * n,
                    "short": {r: k - counts[r] for r, k in need.items() if counts[r] < k},
                    "away": sorted(m.display_name for m, _c in regs if m.absent_on(day))})
    return out


def series_outlook(reg: Registry, rid: str | None = None) -> list[dict]:
    """Registry.series_coverage (regulars against the minimums, green / amber / red) with each series' forecast
    attached — what the Rosters page, the analytics card, /roster overview, readiness (MCP, /ask) all show."""
    return [{**cv, "forecast": series_forecast(reg, cv["key"])} for cv in reg.series_coverage(rid)]


# a run opened closer than its cadence assumes keeps this much of the remaining time for each step
FIT_NUDGE, FIT_LOCK, FIT_CONFIRM = 0.6, 0.3, 0.1


def fit_cutoffs(available_h: float, nudge_h: float, lock_h: float, confirm_h: float) -> tuple[float, float, float]:
    """Cadence hours-before-start, squeezed into the time a run actually has. A pickup opened two hours out would
    otherwise inherit a 24 h lock, putting nudge and lock in the past so the scheduler locks it on the next tick with
    nobody on the sheet. When the cadence fits (the usual case) nothing changes, because each min() keeps the cadence."""
    if available_h <= 0:
        return 0.0, 0.0, 0.0
    return (min(nudge_h, available_h * FIT_NUDGE), min(lock_h, available_h * FIT_LOCK), min(confirm_h, available_h * FIT_CONFIRM))


def ensure_run(reg: Registry, instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None, schedule: str | None = None) -> dict:
    """The ephemeral roster that carries one run's settings (size, cutoffs from its schedule's cadence, how many rosters
    it expects); created once. `schedule` = the schedule id it comes from (None: the raid's own cadence).
    `cutoffs` = {soft, hard, confirm} in hours to override the cadence (test runs compress it to minutes) — and makes
    it a TEST run: its key is in the '-test' key space (TEST_SCHEDULE), and an existing roster whose test flag differs
    from what is asked is refused (a rehearsal never takes over a real run's roster, nor a real run a rehearsal's).
    `cutoffs["test_channel"]`: where the command was used — the test run's cards post there (D20)."""
    test = bool(cutoffs)
    key = run_key(instance, start, schedule, test=test)
    t = reg.config.roster(key)
    if t:
        if bool(t.get("test")) != test:
            raise RunKeyClash(f"{t.get('name') or key} is {'a test run' if t.get('test') else 'a real run'}: "
                                f"{'a real run' if not test else 'a test run'} can't share it — nothing was opened")
        mine, theirs = (cutoffs or {}).get("test_by"), t.get("test_by")
        if test and mine is not None and theirs is not None and str(mine) != str(theirs):
            # a rehearsal belongs to its tester (its pool, its buttons, its DMs): a second tester never inherits it
            who = reg.members.get(int(theirs)) if str(theirs).isdigit() else None
            raise RunKeyClash(f"{who.display_name if who else 'Another officer'} opened a test run at {reg.local12(start)} — "
                                "join theirs or clear it (nothing was opened)")
        return t
    rd = reg.schedule_def(instance, schedule)
    sched = rd["schedule"]
    lead, lock, confirm = float(rd["signup_lead_hours"]), float(rd["lock_hours_before"]), float(rd["confirm_hours_before"])
    c = cutoffs or {}
    if not c:  # test runs pass their own compressed cutoffs and mean them
        available = (start - reg.now_local()).total_seconds() / 3600
        nudge_h, lock, confirm = fit_cutoffs(available, float(rd["nudge_hours_before"]), lock, confirm)
        rd = {**rd, "nudge_hours_before": nudge_h}
    t = {"key": key, "name": f"{rd.get('name', instance)} {reg.local12(start)}", "size": int(rd.get("size") or DEFAULT_RAID_SIZE), "schedule": start.strftime("%a %H:%M"), "instance": instance,
         "cutoff_soft_hours": c.get("soft", float(rd["nudge_hours_before"])), "cutoff_hard_hours": c.get("hard", lock), "confirm_hours": c.get("confirm", confirm), "open_days_before": lead / 24,
         "reminders": "dm" if c.get("nudge", rd["nudge"]) else "none", "open_dm": bool(c.get("open_dm", rd["open_dm"])), "autofill": bool(rd["autofill"]), "ephemeral": True}
    # every run carries its class and series (roster redesign Phase 1): a one-off (ADHOC_SCHEDULE) and a pickup template
    # are ad hoc; anything else is a run of a recurring series whose regulars it expects
    kind = rd.get("class") or schedule_class(sched.get("kind"))
    t.update({"schedule_id": sched["id"], "kind": kind, "series": series_key(instance, sched["id"]) if kind == "recurring" else None,
              "confirm": True if cutoffs else bool(rd.get("confirm", True))})  # a test run rehearses the confirmations
    if sched["id"] != DEFAULT_SCHEDULE or sched.get("own") or int(sched.get("rosters") or 1) > 1:
        # the run carries its schedule's settings (the raid's are read live, as before, for a plain run)
        t.update({"schedule_id": sched["id"], "rosters": int(sched.get("rosters") or 1), "fill_ask_hours": float(rd["fill_ask_hours"]), "split_policy": rd.get("split_policy") or "balanced"})
        if sched["id"] != DEFAULT_SCHEDULE:
            t["name"] = f"{rd.get('name', instance)} · {sched.get('name') or sched['id']} {reg.local12(start)}"
    if cutoffs:
        t["test"] = True
        t["test_by"] = c.get("test_by")
        if c.get("test_channel"):
            t["test_channel"] = int(c["test_channel"])  # the asking channel: the run's officer cards post there (D20)
    reg.config.rosters.append(t)
    reg.save_config(f"run {key} ({t['name']}) created by {by}", notify=False)
    return t


def closed_run_refusal(rs: "RaidStore", key: str) -> str | None:
    """Why a run can't be opened at `key` (an event key), or None: that time's run already exists and is CLOSED. A
    cancelled run is never brought back by an open (D19: open a new run at another time), and an ended one only by
    Reopen within the hour — open_run refuses rather than hand the closed run back as if it had opened."""
    ev = rs.events.get(key)
    if ev is None or ev.state not in ("done", "cancelled"):
        return None
    if ev.state == "cancelled":
        return f"That time's run was cancelled ({ev.key}) — a cancelled run isn't opened again; pick another time."
    return f"That time's run already ended ({ev.key}) — Reopen it within the hour if that was a mistake, or pick another time."


def open_run(reg: Registry, rs: "RaidStore", instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None, schedule: str | None = None) -> "RaidEvent":
    """Open (or find the live) run for `instance` at `start`. RegistryError when that time's run is closed
    (closed_run_refusal) — checked before ensure_run, so a refused open re-creates no roster entry."""
    why = closed_run_refusal(rs, event_key(instance, start, schedule, test=bool(cutoffs)))
    if why:
        raise RegistryError(why)
    return open_event(reg, rs, ensure_run(reg, instance, start, by, cutoffs, schedule), start)


def forget_test_members(reg: Registry, rs: "RaidStore", uids: list[int], by: str) -> list["RaidEvent"]:
    """Remove chosen puppets and take them off every open sheet (answer, pin, board place). Refused, with nothing
    changed, when one is rostered on a locked run: its seat would need the fill engine, so cancel the run or clear the
    bench instead. Returns the sheets that changed (the caller refreshes their messages)."""
    by_uid = {u: reg.members[u].display_name for u in uids if u in reg.members}
    names = set(by_uid.values())
    held = [(by_uid[u], e.key) for e in rs.live() for u in sorted(by_uid, key=by_uid.get) if e.seat_of(u)]  # seats by id
    if held:
        raise RegistryError(f"{', '.join(n for n, _ in held)} {'is' if len(held) == 1 else 'are'} rostered on a locked run "
                            f"({', '.join(sorted({k for _, k in held}))}): cancel it or clear the whole bench")
    reg.remove_test_members(uids, by)
    ids, changed = {str(u) for u in uids}, []
    for ev in rs.live():
        if ids & set(ev.signups) or (ev.layout and any(n in names for g in ev.layout for n in g)):
            ev.signups = {k: sg for k, sg in ev.signups.items() if k not in ids}
            ev.pins = {k: p for k, p in ev.pins.items() if k not in ids}
            if ev.layout:
                ev.layout = [[n for n in g if n not in names] for g in ev.layout]
            rs.save(ev, f"test members removed by {by}")
            changed.append(ev)
    return changed


def open_comp_sandbox(reg: Registry, rs: "RaidStore", instance: str, by: str, test_by) -> "RaidEvent":
    """A test run two weeks out with every puppet joined, never posted: the board's Propose roster then builds the best
    comp from whatever mix the bench holds. Cleared with the rest of the test bench."""
    if instance not in reg.profile.raids:
        raise RegistryError(f"unknown raid {instance or '?'} (one of {', '.join(reg.profile.raids)})")
    puppets = reg.test_members()
    if not puppets:
        raise RegistryError("add test members first")
    start = (reg.now_local() + timedelta(days=14)).replace(minute=0, second=0, microsecond=0)
    ev = open_run(reg, rs, instance, start, by=by, cutoffs={"soft": 2, "hard": 1, "confirm": 0.5, "open_dm": False, "test_by": test_by})
    for m in puppets:
        set_signup(reg, rs, ev, m, None, "in", source="test")
    return ev


def test_answer_mix(reg: Registry, rs: "RaidStore", ev: "RaidEvent", join: int = 0, bench: int = 0, out: int = 0, rng=None) -> list[str]:
    """The test bench's random answers (/gm test answer, the web's test bench, MCP test_bench, plain text): puppets
    who haven't answered this sheet yet, shuffled; `join` of them Join, `bench` Bench, `out` No thanks — fewer when
    the bench runs out. Returns '<name> <label>' lines; the caller refreshes the sheet."""
    import random

    counts = (("in", int(join or 0)), ("sub", int(bench or 0)), ("out", int(out or 0)))
    if any(n < 0 for _, n in counts):
        raise RegistryError("answer counts can't be negative")
    pool = [m for m in reg.test_members() if str(m.discord_id) not in ev.signups]
    (rng or random).shuffle(pool)
    done = []
    for st, n in counts:
        for _ in range(n):
            if not pool:
                break
            m = pool.pop()
            set_signup(reg, rs, ev, m, None, st, source="test")
            done.append(f"{m.display_name} {LABELS[st]}")
    return done


test_answer_mix.__test__ = False  # a helper, not a pytest test (its name starts with test_)

PAST_GRACE_MINUTES = 5  # an on-demand open may start this close to now, not earlier


def refuse_past_start(reg: Registry, start: datetime, now_: datetime | None = None) -> None:
    """Refuse a run start more than PAST_GRACE_MINUTES ago: a mistyped year or a stale pick would open a run the
    scheduler closes at once. Every on-demand open (web, plain text, MCP, /raid open) passes through
    RaidMixin.open_run_and_post, which calls this; the scheduler only opens starts from upcoming() (never past) and
    the comp sandbox opens two weeks out."""
    ref = now_ or reg.now_local()
    if start < ref - timedelta(minutes=PAST_GRACE_MINUTES):
        raise RegistryError(f"{reg.local12(start)} is in the past — pick a time from now on")


def next_runs_preview(reg: Registry, instance: str, *, fields: dict | None = None, schedule: str | None = None, schedule_fields: dict | None = None,
                      timezone: str | None = None, n: int = 3, now_: datetime | None = None) -> list[dict]:
    """What a raid's next runs WOULD be after a change, without saving it (a Registry.shadow): `fields` = raid
    settings as set_raid_overrides takes them (slots, first_open, lockout_days, cadence hours…), `schedule` +
    `schedule_fields` = one schedule's settings as set_schedule takes them, `timezone` = the guild's new zone.
    Returns one row per active schedule except pickup templates: {id, name, starts: [aware datetimes], labels:
    ['Tue 6 Oct 7:30 PM', …]} — labels in the (new) guild time, 12-hour. A refused change raises RegistryError with
    the message the save would give. Used by describe() previews and TimezoneWizard-style confirmations."""
    if instance not in reg.profile.raids:
        raise RegistryError(f"unknown raid {instance or '?'} (one of {', '.join(reg.profile.raids)})")
    sh = reg.shadow()
    if timezone:
        try:
            ZoneInfo(timezone)
        except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError / ValueError on odd input
            raise RegistryError(f"unknown timezone {timezone} (an IANA name like America/New_York)") from None
        sh.config.timezone = timezone
    if fields:
        sh.set_raid_overrides(instance, dict(fields), "preview")
    if schedule and schedule_fields:
        sh.set_schedule(instance, schedule, dict(schedule_fields), "preview")
    ref = (now_ or sh.now_local()).astimezone(sh.tz)
    out = []
    for s in sh.schedules(instance):
        if not s.get("active", True) or s["kind"] == "pickup":
            continue
        starts = next_runs_of(sh, instance, s, n, ref)
        out.append({"id": s["id"], "name": s.get("name") or s["id"], "starts": starts, "labels": [sh.local12(t) for t in starts]})
    return out


def next_runs_lines(reg: Registry, instance: str, **kw) -> list[str]:
    """next_runs_preview as one line per schedule: 'Barrow Deeps · Default: next Tue 6 Oct 7:30 PM, Thu 8 Oct 7:30 PM'."""
    name = reg.raid_def(instance).get("name", instance)
    rows = next_runs_preview(reg, instance, **kw)
    return [f"{name}" + (f" · {r['name']}" if len(rows) > 1 or r["id"] != DEFAULT_SCHEDULE else "") + ": next " + (", ".join(r["labels"]) or "none (no run times yet)")
            for r in rows]


def timezone_preview_rows(reg: Registry, zone: str, now_: datetime | None = None) -> list[dict]:
    """timezone_preview with the moments behind each line: {line, start (aware, the next run under the new zone),
    was (aware, the next run under the current zone, or None)} — Discord adds <t:unix:f> stamps from them (the
    TimezoneWizard), so every reader sees the before and after in their own locale."""
    rows = []
    for rid in reg.profile.raids:
        new = next_runs_preview(reg, rid, timezone=zone, n=1, now_=now_)
        firsts = sorted((r["starts"][0], r["labels"][0]) for r in new if r["starts"])
        if not firsts:
            continue
        t, label = firsts[0]
        cur = sorted(r["starts"][0] for r in next_runs_preview(reg, rid, n=1, now_=now_) if r["starts"])
        rows.append({"line": f"{reg.raid_def(rid).get('name', rid)}: next {label} {zone} — {reg.local12(t)} on today's clock ({reg.config.timezone})",
                     "start": t, "was": cur[0] if cur else None})
    return rows


def timezone_preview(reg: Registry, zone: str, now_: datetime | None = None) -> list[str]:
    """What a timezone change does to each raid's next run, in words (run times keep their clock time, so the real
    moment moves): 'Barrow Deeps: next Tue 6 Oct 7:30 PM America/New_York — Tue 6 Oct 4:30 PM on today's clock'.
    One line per raid with run times; a bad zone raises RegistryError."""
    return [r["line"] for r in timezone_preview_rows(reg, zone, now_)]


def run_rosters(reg: Registry, ev: "RaidEvent") -> int:
    """How many rosters the run expects up front (its schedule's `rosters`, 1 for a plain run)."""
    return max(1, int((reg.config.team(ev.team) or {}).get("rosters") or 1))


# ---------------------------------------------------------------- events on the store

class RaidStore:
    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key
        self.events: dict[str, RaidEvent] = {}
        d = store.root / guild_key / "raids"
        if d.exists():
            for f in d.glob("*.json"):
                ev = RaidEvent.model_validate_json(f.read_text())  # legacy states / roster / tentative normalised by the model
                if ev.log_at is None:  # a file from before stamps: its lines have none (never stamped "now" on the next save)
                    ev.log_at = [""] * len(ev.log)
                self.events[ev.key] = ev

    def save(self, ev: RaidEvent, message: str) -> None:
        stamp_log(ev)
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


def stamp_log(ev: RaidEvent) -> None:
    """Every log line gets a stamp the first time the run is saved after it was written (lines are appended right
    before a save, so the save time is the line's time). `ev.log_at` follows `ev.log` index for index; a line removed
    (lock_run's ev.log.pop()) trims it."""
    n = len(ev.log)
    stamps = list(ev.log_at or [])
    if len(stamps) < n:
        stamps += [now()] * (n - len(stamps))
    ev.log_at = stamps[:n]


# ---------------------------------------------------------------- building an event

def open_event(reg: Registry, rs: RaidStore, team: dict, starts_at: datetime) -> RaidEvent:
    key = f"{team['key']}-{starts_at.date().isoformat()}"
    if key in rs.events:
        ev = rs.events[key]
        if ev.test and not team.get("test"):  # a rehearsal's sheet from before the '-test' key space: never a real run's
            raise RunKeyClash(f"{key} is a test run's sheet: a real run can't reuse it — nothing was opened")
        why = closed_run_refusal(rs, key)
        if why:  # never hand a cancelled / ended run back as the one just opened
            raise RegistryError(why)
        return ev
    sid = team.get("schedule_id")
    ev = RaidEvent(key=key, team=team["key"], instance=team.get("instance"), schedule=None if sid in (None, DEFAULT_SCHEDULE) else sid,
                   series=team.get("series"), kind=team.get("kind"), test=bool(team.get("test")), starts_at=starts_at.isoformat())
    prefill(reg, ev, team)
    ev.log.append(f"opened ({run_kind(reg, ev)}); {len(ev.signups)} marked away from absences")
    rs.save(ev, "opened")
    return ev


def prefill(reg: Registry, ev: RaidEvent, team: dict) -> None:
    """Absences seed the sheet: anyone the sheet could expect (every main, a regular with their regular character; a
    test run: the puppets and the tester) with a recorded absence covering the run's day is No thanks (source
    "absence", the sheet's Away row). Nobody is pre-filled Join (owner decision D1: a regular starts unanswered like
    everyone else; officers see who hasn't answered). The series' regulars are snapshotted on the run for history."""
    day = ev.start.astimezone(reg.tz).date().isoformat()
    for m, c in absence_pool(reg, ev, team):
        if m.absent_on(day):
            ev.signups[str(m.discord_id)] = _signup(reg, m, c, "out", "absence")
    ev.regulars = [m.discord_id for m, _ in reg.regulars(ev.series)] if ev.series and not ev.test else []


# ---------------------------------------------------------------- who a run is for (roster redesign Phase 1)

def run_kind(reg: Registry, ev: RaidEvent) -> str:
    """'recurring' | 'adhoc'. Stamped at open; a file from before classes reads as its schedule says (a pickup or a
    one-off → ad hoc; the raid's own run times or another weekly / lockout schedule → recurring)."""
    if ev.kind:
        return ev.kind
    team = reg.config.team(ev.team) or {}
    if team.get("kind"):
        return team["kind"]
    sid = team.get("schedule_id") or ev.schedule
    if sid == ADHOC_SCHEDULE:
        return "adhoc"
    s = next((x for x in reg.schedules(ev.instance) if x["id"] == (sid or DEFAULT_SCHEDULE)), None) if ev.instance in reg.profile.raids else None
    return schedule_class(s["kind"]) if s else "recurring"


def run_series(reg: Registry, ev: RaidEvent) -> str | None:
    """The recurring series a run belongs to ('<raid>:<schedule>'), None for an ad hoc run."""
    if run_kind(reg, ev) != "recurring" or not ev.instance:
        return None
    if ev.series:
        return ev.series
    team = reg.config.team(ev.team) or {}
    return team.get("series") or series_key(ev.instance, team.get("schedule_id") or ev.schedule or DEFAULT_SCHEDULE)


def is_test_run(reg: Registry, ev: RaidEvent) -> bool:
    return ev.test or bool((reg.config.team(ev.team) or {}).get("test"))


def _test_pool(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    tester = (reg.config.team(ev.team) or {}).get("test_by")
    return [(m, m.main) for m in reg.members.values() if m.main and (m.test or m.discord_id == tester)]


def run_regulars(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    """The regulars a run expects, with their regular character: its series' regulars (recurring); a test run treats
    every puppet (and the tester) as a virtual regular — no real membership is read or written; ad hoc: nobody."""
    if is_test_run(reg, ev):
        return _test_pool(reg, ev)
    series = run_series(reg, ev)
    return [(m, c) for m, c in reg.regulars(series) if not m.test] if series else []


def run_pool(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    """Who a run is FOR, with the character each would bring: a test run → the puppets + the tester; a recurring run
    whose series has regulars → those regulars; otherwise (ad hoc, or a series nobody curated yet) → every main —
    never a test puppet on a real run (puppets belong to test runs only, so they are never suggested, asked, nudged
    or listed as "No answer" there). Keyed on the series, never the run key, so it doesn't change at lock. The fill
    engine's first tier after the bench, health's "not on sheet", another_run_hint and the open DMs read it."""
    if is_test_run(reg, ev):
        return _test_pool(reg, ev)
    regs = run_regulars(reg, ev)
    return regs if regs else [(m, m.main) for m in reg.members.values() if m.main and not m.test]


def expected(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    """Who the bot expects an answer from (officers' "No answer" list, the nudge): recurring → run_pool (the regulars,
    or every main while the series has none); ad hoc → nobody (the sheet is posted and people come; no reminder)."""
    return run_pool(reg, ev) if run_kind(reg, ev) == "recurring" else []


def expected_from(reg: Registry, ev: RaidEvent) -> str:
    """Where `expected` comes from, for the officer surfaces: regulars | everyone (a recurring series with no regulars
    yet) | test | none (ad hoc)."""
    if is_test_run(reg, ev):
        return "test"
    if run_kind(reg, ev) != "recurring":
        return "none"
    return "regulars" if run_regulars(reg, ev) else "everyone"


def not_answered(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    """The expected people with no answer on the sheet (an absence-marked No thanks counts as an answer), by name."""
    return sorted([(m, c) for m, c in expected(reg, ev) if c and str(m.discord_id) not in ev.signups], key=lambda mc: mc[0].display_name.lower())


def regular_answers(reg: Registry, ev: RaidEvent) -> list[dict]:
    """Every regular of the run with what they answered: {uid, display_name, character, cls, spec, role, status|None,
    source}. Officer-facing (the web sheet card, MCP get_run, help)."""
    out = []
    for m, c in run_regulars(reg, ev) if not is_test_run(reg, ev) else []:
        sg = ev.signups.get(str(m.discord_id))
        out.append({"uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec,
                    "role": reg.profile.spec(c.cls, c.spec).role, "status": sg.status if sg else None, "source": sg.source if sg else None,
                    "signed_as": sg.character if sg else None})
    return out


def on_bench_side(reg: Registry, ev: RaidEvent, m: Member) -> bool:
    """Does `m` belong on this run's side of the test bench? A test run takes its puppets and its tester; a real run
    takes real members only (a puppet's absence never marks it away on a real sheet, a real member's never on a test one)."""
    if is_test_run(reg, ev):
        return bool(m.test) or m.discord_id == (reg.config.team(ev.team) or {}).get("test_by")
    return not m.test


def absence_pool(reg: Registry, ev: RaidEvent, team: dict | None = None) -> list[tuple[Member, RegisteredCharacter]]:
    """Everyone a recorded absence marks away on this run: a test run's puppets + tester; otherwise every member with a
    main (never a test puppet), a regular with their regular character."""
    if is_test_run(reg, ev):
        return _test_pool(reg, ev)
    regs = {m.discord_id: c for m, c in run_regulars(reg, ev)}
    return [(m, regs.get(m.discord_id) or m.main) for m in reg.members.values() if not m.test and (m.main or m.discord_id in regs)]


def expected_character(reg: Registry, ev: RaidEvent, m: Member) -> RegisteredCharacter | None:
    """The character `m` would bring to this run: their regular character on its series, else their main."""
    return next((c for mm, c in run_regulars(reg, ev) if mm.discord_id == m.discord_id), None) or m.main


def run_confirms(reg: Registry, ev: RaidEvent) -> bool:
    """Does the lock ask the rostered to Confirm / Can't make it? Recurring default yes; ad hoc default no (D5: Join is
    the commitment). Frozen on the run dict at open; files from before read yes."""
    return bool((reg.config.team(ev.team) or {}).get("confirm", True))


def open_schedule(reg: Registry, instance: str, start: datetime, sid: str | None) -> str | None:
    """Which schedule an on-demand open at `start` belongs to (web, plain text, MCP): `sid` when named; else one of the
    raid's upcoming runs at exactly that start (its schedule id, None for the raid's own run times); else a one-off →
    ADHOC_SCHEDULE. The /raid open wizard asks instead (Next run · Another upcoming run · Ad hoc run…)."""
    if sid:
        return None if sid == DEFAULT_SCHEDULE else sid
    hit = next((s["id"] for s, t in upcoming(reg, instance, reg.now_local(), 24 * OPEN_HORIZON_DAYS) if t == start), None)
    if hit is not None:
        return None if hit == DEFAULT_SCHEDULE else hit
    return ADHOC_SCHEDULE


def _signup(reg: Registry, m: Member, c: RegisteredCharacter, status: str, source: str, note: str | None = None) -> Signup:
    return Signup(discord_id=m.discord_id, display_name=m.display_name, character=c.label, cls=c.cls, spec=c.spec, offspec=c.offspec, role=reg.profile.spec(c.cls, c.spec).role, status=status, source=source, note=note)


def carry_joined(old: Signup | None, s: Signup) -> Signup:
    """Keep the queue place of a Join that stays a Join (a re-press, another character, a yes to an ask); a fresh Join
    (after Bench / No thanks, or a first answer) starts now; anything but Join holds none."""
    if s.status != "in":
        s.joined_at = None
    elif old is not None and old.status == "in":
        s.joined_at = old.order_at
    else:
        s.joined_at = s.updated_at
    return s


def set_signup(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, character: str | None, status: str, source: str = "member", note: str | None = None) -> Signup:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    want = (character or "").strip().lower()
    c = (next((c for c in m.active() if c.label.lower() == want), None) or next((c for c in m.active() if (c.name or "").lower() == want), None)) if want else m.main
    if not c:
        raise ValueError("no such active character")
    # no curated-roster demotion here (roster redesign Phase 0): it keyed on the run key, which lock used to fill with
    # the seated, so a member's own Join after lock was stored as Bench and then seated anyway
    s = carry_joined(ev.signups.get(str(m.discord_id)), _signup(reg, m, c, status, source, note))
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"{m.display_name} {status} as {c.label} ({source})")
    rs.save(ev, f"{m.display_name} {status}")
    return s


def signup_by_name(ev: RaidEvent, display_name: str) -> Optional[Signup]:
    n = display_name.strip().lower()
    return next((s for s in ev.signups.values() if s.display_name.lower() == n), None)


def set_pin(ev: RaidEvent, who: int | str, pin: str | None) -> None:
    """Officer decision the lock solve must honour: "in" | "out" | None (clear). Pins are keyed by discord id (as
    the web API keys them); the person must be on the sheet. `who` = the member's discord id — THE write path
    (inbox.pin_write): an id decides, so two members sharing a name never pin the wrong one; a str is a display name
    (tests and offline tools only). Appends the log line; the caller saves the event."""
    if pin not in ("in", "out", None):
        raise ValueError("pin must be in, out or nothing (clear)")
    sg = ev.signups.get(str(who)) if isinstance(who, int) else signup_by_name(ev, who)
    if not sg:
        raise ValueError(f"{who} isn't on this sheet")
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


def runs_in_span(reg: Registry, rs: RaidStore, start: str, end: str) -> list[RaidEvent]:
    """The live runs whose day (guild time) falls inside an absence's ISO days: the sheets it touches."""
    return [ev for ev in rs.live() if start <= ev.start.astimezone(reg.tz).date().isoformat() <= end]


def absence_effects(reg: Registry, rs: RaidStore, m: Member | None, start: str, end: str) -> list[tuple[RaidEvent, str]]:
    """What recording an absence would do to each live sheet in the span, without doing it (RaidMixin.after_absence
    does it): 'seat freed' (locked and rostered: the fill engine looks for cover) | 'No thanks' | 'already No thanks'."""
    out = []
    for ev in runs_in_span(reg, rs, start, end):
        if m is not None and not on_bench_side(reg, ev, m):
            continue  # a puppet's absence never touches a real sheet (nor a real member's a test run)
        sg = ev.signups.get(str(m.discord_id)) if m is not None else None
        if m is not None and ev.state != "open" and ev.seat_of(m):
            out.append((ev, "seat freed"))
        elif sg is not None and sg.status == "out":
            out.append((ev, "already No thanks"))
        else:
            out.append((ev, "No thanks"))
    return out


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


def gaps(reg: Registry, ev: RaidEvent) -> dict:
    """The ONE role-need calculation — the health card, health_data, needs() (so the fill engine) and would_short all
    read it: seats and the tank/healer minimums times the rosters in play. Before lock that is the rosters the run
    expects up front (its schedule's `rosters`) and the players are the Join answers (their signed role); after lock
    it is the locked rosters and the seated players (their seat's role — the solver may have switched an offspec).
    {size, rosters, seats, have{role}, need{role}, roles{tank|healer: short}, headcount (free seats), locked}"""
    size = run_size(reg, ev)
    locked = ev.state != "open" and bool(ev.all_rosters)
    n = len(ev.all_rosters) if locked else run_rosters(reg, ev)
    bounds = reg.role_bounds(ev.instance, size)
    players = ev.seated() if locked else ev.by_status("in")
    have = {r: sum(1 for p in players if p.role == r) for r in ROLES}
    need = {r: int(bounds[r]["min"]) * n if r in ("tank", "healer") else 0 for r in ROLES}
    short = {r: need[r] - have[r] for r in ("tank", "healer") if need[r] > have[r]}
    return {"size": size, "rosters": n, "seats": size * n, "have": have, "need": need, "roles": short,
            "headcount": max(0, size * n - len(players)), "locked": locked}


def needs(reg: Registry, ev: RaidEvent, team: dict | None = None) -> dict:
    """What the sheet is short (rc.gaps): headcount and per-role minimums. Before lock: Join signups vs the raid size
    times the rosters the run expects. After lock: freed seats across the locked roster(s)."""
    g = gaps(reg, ev)
    return {"headcount": g["headcount"], "roles": g["roles"], "size": g["size"]}


def _rank_key(reg: Registry, m: Member) -> tuple:
    c = m.main
    return (RANK_ORDER.get(c.rank if c else "trial", 9), m.created_at)


def ask_deadline(reg: Registry, ev: RaidEvent, at: datetime | None = None) -> datetime:
    """When an unanswered fill ask counts as no: `fill_ask_hours` after it was sent, never past the run start."""
    at = at or datetime.now(ev.start.tzinfo)
    hours = float((reg.config.team(ev.team) or {}).get("fill_ask_hours") or reg.raid_def(ev.instance).get("fill_ask_hours") or FILL_ASK_HOURS_DEFAULT)
    return min(at + timedelta(hours=hours), ev.start)


def would_short(reg: Registry, ev: RaidEvent, team: dict, role: str) -> bool:
    """Would losing one `role` player (a Join before lock, a seated player after) drop that role under its minimum?"""
    g = gaps(reg, ev)
    n = g["rosters"]
    bounds = reg.role_bounds(ev.instance, g["size"])
    players = ev.seated() if g["locked"] else ev.by_status("in")
    key = "dps" if role in ("melee", "ranged") else role
    have = sum(1 for p in players if (p.role in ("melee", "ranged") if key == "dps" else p.role == role))
    return have - 1 < int(bounds.get(key, {}).get("min") or 0) * n


# ---------------------------------------------------------------- the planner (roster redesign Phase 2)
# ONE engine for "who could cover this": the fill engine after lock and the officers' suggestions before it. No second
# solver: a greedy pass in cost order, a matching check so nobody is counted for two seats, paired backfills.

MOVE_COST = {"sub": 1, "flex": 1, "offspec": 2, "regular": 2, "unanswered": 2, "alt": 3, "other": 4}  # design §4: cheapest first
PLAN_ALTERNATES = 2  # alternates listed per short seat, so an officer can pick someone else without typing a name


class PlanSeat(BaseModel):
    """One seat the plan tries to cover: `role` short of its minimum ("" = a plain seat after lock). `options[0]` is the
    suggestion, then up to PLAN_ALTERNATES alternates; an option is one ask, or a swap tied to the backfill that covers
    the seat it vacates. No options = nobody can cover it (the still-short line)."""

    role: str
    options: list[list[FillAsk]] = Field(default_factory=list)


class Plan(BaseModel):
    """role_plan's answer — computed on view, never stored (only sent asks are saved)."""

    phase: Literal["prelock", "fill"]
    short: dict  # {"headcount": n, "roles": {role: n}}: what the plan works on (before lock: the expected roster)
    have: dict[str, int]  # role counts the plan starts from (before lock: joined + the regulars expected; "dps" summed)
    need: dict[str, int]  # the minimums × rosters (tank, healer, dps)
    full: bool  # every seat taken (before lock: by the expected roster) — swaps are tried before new people
    expected: int = 0  # before lock: regulars who haven't answered (and aren't away), counted as coming
    waiting: list[str] = Field(default_factory=list)  # asks still out (before lock they count as covering their seat)
    seats: list[PlanSeat] = Field(default_factory=list)
    short_after: dict  # {"headcount": n, "roles": {role: n}}: still short if every suggestion is a yes
    rev: str = ""  # plan_rev: a send carrying an older one is refused
    out: int = 0  # after lock: asks out now (pending) — an officer's send fills the room up to FILL_MAX_OPEN (send_batch)
    held_back: int = 0  # set by an officer's send after lock: asks not sent now (at most FILL_MAX_OPEN out at once)

    @property
    def room(self) -> int:
        """After lock: how many more asks may go out now (FILL_MAX_OPEN minus those out); before lock: no cap."""
        return max(0, FILL_MAX_OPEN - self.out)

    @property
    def asks(self) -> list[FillAsk]:
        """The suggestions in send order (a swap right before its backfill)."""
        return [a for st in self.seats if st.options for a in st.options[0]]


class PlanChanged(RegistryError):
    """An officer's Ask / Send carried a plan fingerprint that is no longer current: nothing was sent; `plan` is the
    plan as it is now (the card, modal or wizard shows it instead)."""

    def __init__(self, plan: "Plan", msg: str = "The sheet changed since you looked — the suggestions have been refreshed; check them and send again."):
        super().__init__(msg)
        self.plan = plan


def plan_rev(plan: "Plan") -> str:
    """A fingerprint of what the plan would send (each seat's options: who, how, as what). An Ask carrying an older one
    is refused, so nobody acts on a stale plan; an answer elsewhere that leaves the suggestions as they were does not
    make a card stale."""
    import hashlib
    import json

    seats = [[st.role, [[(a.discord_id, a.kind, a.role, a.character, a.spec, a.pair) for a in opt] for opt in st.options]] for st in plan.seats]
    parts = [plan.phase, seats]
    if plan.phase == "fill":  # after lock the cap decides how much of it goes out now (send_batch): a change there is a new plan
        parts.append(min(plan.room, sum(max(len(o) for o in st.options) for st in plan.seats if st.options)))
    blob = json.dumps(parts)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def saved_this_lockout(reg: Registry, rs: "RaidStore | None", ev: RaidEvent) -> set[str]:
    """Characters already rostered on another run of this raid in the same lockout window (locked or done): an alt
    saved there can't come, so the planner never suggests it (the alt-lockout filter — not a general lockout guard).
    Test runs only count for test runs."""
    if rs is None or not ev.instance:
        return set()
    days = int(reg.raid_def(ev.instance).get("lockout_days", 7))
    anchored = reg.first_open(ev.instance) is not None
    win = reg.lockout_window(ev.instance, ev.start) if anchored else None
    out: set[str] = set()
    for e in rs.events.values():
        if e.key == ev.key or e.instance != ev.instance or e.state not in ("locked", "done") or bool(e.test) != bool(is_test_run(reg, ev)):
            continue
        same = reg.lockout_window(e.instance, e.start) == win if anchored else abs((e.start - ev.start).total_seconds()) < days * 86400
        if same:
            out |= {p.character for p in e.seated() if p.character}
    return out


def spec_for_role(reg: Registry, cls: str, offspec: str | None, role: str) -> str | None:
    """The spec a declared flex role means on this character: its offspec when that plays the role, else the class's
    only spec of that role; None when it is ambiguous (two specs of the role and neither is the offspec)."""
    if offspec:
        try:
            if reg.profile.spec(cls, offspec).role == role:
                return offspec
        except KeyError:
            pass
    specs = [sp for sp in reg.profile.classes.get(cls, {}) if reg.profile.spec(cls, sp).role == role]
    return specs[0] if len(specs) == 1 else None


def _dps(role: str) -> str:
    return "dps" if role in ("melee", "ranged") else role


def _role_word(role: str) -> str:
    return "DPS" if _dps(role) == "dps" else role


def role_plan(reg: Registry, rs: "RaidStore | None", ev: RaidEvent, team: dict | None = None, *, swaps: bool = True, count_waiting: bool = True) -> Plan:
    """What would cover the run's shortfall, best first — pure; nothing is sent. The fill engine after lock (role gaps,
    then free seats) and the officers' suggestions before it (tank/healer minimums only) read this one function.

    Who counts: after lock the seated (their seat's role — the solver may have switched an offspec); before lock the
    Join answers plus, on a recurring run with regulars, the regulars who haven't answered and aren't away (nothing is
    pre-filled, D1: the plan treats them as coming, so it suggests what goes beyond them), and the officer's asks still
    out as if they said yes (a No frees the seat again and the next alternate shows). After lock the asks still out
    count the same way for the officers' view (the lock card, an officer's Send), so nobody is asked twice for one
    seat; the fill engine's own batches (fill_batch) pass count_waiting=False and keep their over-ask (shortfall + 1,
    at most FILL_MAX_OPEN outstanding).
    Moves per short seat, cheapest first (MOVE_COST): someone on Bench who plays the role · a declared flex role ·
    an offspec · an unanswered regular · an alt (skipped when that alt is already saved to this raid this lockout) · a
    non-regular main. When every seat is taken (before lock: by the expected roster), switches by people already coming
    go before new people — a new healer would only bench a DPS at lock. A switch that would drop the role it vacates
    under its minimum only goes with a tied backfill. A matching check keeps one person from covering two seats and
    from blocking a seat only they can cover. Each short seat gets up to PLAN_ALTERNATES alternates. `swaps=False`
    leaves switches out (D3: after lock on a recurring run they wait for an officer's Ask; `withheld` says so)."""
    locked = ev.state != "open" and bool(ev.all_rosters)
    g = gaps(reg, ev)
    n, size = g["rosters"], g["size"]
    bounds = reg.role_bounds(ev.instance, size)
    need = {r: int(bounds.get(r, {}).get("min") or 0) * n for r in ("tank", "healer", "dps")}
    day = ev.start.astimezone(reg.tz).date().isoformat()
    busy = conflicts(rs, ev) if rs is not None else {}
    # one PRE-LOCK ask per member per run, and one fill ask after lock: once the run locked, the pre-lock asks (answered,
    # or closed at lock) no longer keep anyone out — a benched joiner who said yes (or no) before lock can be asked for
    # a seat freed later; asks still in play always do
    asked = {a.discord_id for a in ev.fill_asks if a.pending or ev.state == "open" or a.phase == "fill"}
    saved = saved_this_lockout(reg, rs, ev)
    exp_from = expected_from(reg, ev)

    def role_of(cls: str, spec: str) -> str:
        return reg.profile.spec(cls, spec).role

    def ok(m: Member | None) -> bool:
        # DMs off is no reason to skip someone (D14): they are asked in the same order, the ask waits on their Me page
        # (channel "web", same deadline); fill_batch adds a reachable twin when the hours before start run short
        return bool(m) and m.discord_id not in asked and not m.absent_on(day) and m.discord_id not in busy

    # ---- who counts now
    cur: list[dict] = []
    seated_names = {p.signup_name for p in ev.seated()} if locked else set()
    if locked:
        for p in ev.seated():
            uid = player_uid(ev, p)
            sg = ev.signups.get(str(uid)) if uid is not None else None
            cur.append({"uid": uid, "m": reg.members.get(uid) if uid is not None else None, "name": p.signup_name, "character": p.character or (sg.character if sg else p.signup_name),
                        "cls": p.cls, "spec": p.spec, "offspec": sg.offspec if sg else p.offspec, "role": p.role, "signed": True})
    else:
        for sg in ev.by_status("in"):
            cur.append({"uid": sg.discord_id, "m": reg.members.get(sg.discord_id), "name": sg.display_name, "character": sg.character,
                        "cls": sg.cls, "spec": sg.spec, "offspec": sg.offspec, "role": sg.role, "signed": True})
    projecting = not locked and exp_from == "regulars"
    if projecting:
        for m, c in not_answered(reg, ev):
            if not m.absent_on(day):
                cur.append({"uid": m.discord_id, "m": m, "name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec, "offspec": c.offspec,
                            "role": role_of(c.cls, c.spec), "signed": False})
    have = {r: sum(1 for p in cur if p["role"] == r) for r in ROLES}
    # asks still out — minus anyone who can't come any more (away that day, or No thanks since): theirs covers nothing
    waiting = [a for a in ev.fill_asks if a.pending and ask_live(reg, ev, a) and not parallel_shadowed(ev, a)]  # a parallel twin covers its primary's seat, not a second one
    counted = waiting if (count_waiting or not locked) else []
    for a in counted:  # asks still out count as covering their seat (no second ask for it meanwhile)
        have[a.role] = have.get(a.role, 0) + 1
        if a.swap and a.vacates:
            have[a.vacates] = have.get(a.vacates, 0) - 1
    have["dps"] = have.get("melee", 0) + have.get("ranged", 0)
    short_roles = {r: need[r] - have[r] for r in ("tank", "healer") if need[r] > have[r]}
    # after lock a new body needs a free seat (seat_player) — a body ask still out holds one; before lock the lock decides
    seats_free = max(0, g["headcount"] - sum(1 for a in counted if not a.swap)) if locked else None
    free = [seats_free]
    full = (seats_free == 0) if locked else len(cur) >= g["seats"]
    headcount = seats_free if locked else 0  # before lock suggestions are for the minimums only; the reminder chases the rest
    plan = Plan(phase="fill" if locked else "prelock", short={"headcount": headcount, "roles": dict(short_roles)}, have=dict(have), need=need, full=full,
                expected=sum(1 for p in cur if not p["signed"]), short_after={"headcount": headcount, "roles": dict(short_roles)},
                waiting=[a.display_name for a in waiting], out=sum(1 for a in ev.fill_asks if a.pending and not parallel_shadowed(ev, a)) if locked else 0)
    if not short_roles and not headcount:
        plan.rev = plan_rev(plan)
        return plan

    # ---- bodies: the bench, the regulars (every main while the series has none) who haven't answered, everyone else
    pool = run_pool(reg, ev)
    pool_ids = {m.discord_id for m, _ in pool}
    benched_joiners = [s for s in ev.by_status("in") if locked and s.display_name not in seated_names]
    bodies: list[tuple[str, Member, RegisteredCharacter, str]] = []  # (kind, member, character, words)
    for s in benched_joiners + ev.by_status("sub"):
        m = reg.members.get(s.discord_id)
        if s.display_name not in seated_names and m:
            c = next((c for c in m.active() if c.label == s.character), m.main)
            if c:
                bodies.append(("sub", m, c, "joined, not rostered" if s.status == "in" else "on the Bench"))
    if not projecting:  # projecting: the unanswered regulars are already counted as coming
        word = {"regulars": "a regular, hasn't answered", "test": "test bench, hasn't answered"}.get(exp_from, "hasn't answered")
        # "regular" only where the run has regulars (a test run's puppets stand in for them); an ad hoc run or a series
        # nobody curated yet has none — there the pool is every main, and the ask says only that they haven't answered
        tier = "regular" if exp_from in ("regulars", "test") else "unanswered"
        bodies += [(tier, m, c, word) for m, c in pool if c and str(m.discord_id) not in ev.signups]
    if not is_test_run(reg, ev):
        # everyone else with a main — never a test puppet on a real run (a test run's pool is the puppets + the tester)
        others = sorted([(m, m.main) for m in reg.members.values() if m.main and not m.test and m.discord_id not in pool_ids and str(m.discord_id) not in ev.signups], key=lambda mc: _rank_key(reg, mc[0]))
        bodies += [("other", m, c, "not a regular, no answer") for m, c in others]

    def char_of(p: dict) -> RegisteredCharacter | None:
        m = p["m"]
        return next((c for c in m.active() if c.label == p["character"]), None) if m else None

    def switch(p: dict, role: str) -> tuple[str, str, str, str] | None:
        """(kind, character, spec, words) — the cheapest way `p` could play `role`; None when they can't."""
        if p["role"] == role or not p["m"]:
            return None
        c = char_of(p)
        first = p["name"]
        if c and role in (c.flex or []):
            sp = spec_for_role(reg, p["cls"], p["offspec"] if p["offspec"] != p["spec"] else None, role)
            if sp and sp != p["spec"]:
                return "flex", p["character"], sp, f"{first}'s declared flex {sp}"
        if p["offspec"] and p["offspec"] != p["spec"]:
            try:
                if role_of(p["cls"], p["offspec"]) == role:
                    return "offspec", p["character"], p["offspec"], f"{first}'s offspec {p['offspec']}"
            except KeyError:
                pass
        alt = next((a for a in p["m"].active() if a.label != p["character"] and role_of(a.cls, a.spec) == role and a.label not in saved), None)
        if alt:
            return "alt", alt.label, alt.spec, f"{first} brings {alt.label} ({alt.spec} alt, not saved this lockout)"
        return None

    def moves(role: str) -> list[tuple]:
        """Every way to cover one `role` seat, in the order they are tried. ("body", kind, m, c, words) |
        ("swap", kind, p, character, spec, words). Bodies of the role only while a seat is left (after lock)."""
        out = []
        if role:
            for p in cur:
                if swaps and ok(p["m"]):
                    sw = switch(p, role)
                    if sw:
                        out.append(("swap", sw[0], p, sw[1], sw[2], sw[3]))
        if free[0] is None or free[0] > 0:
            out += [("body", k, m, c, w) for k, m, c, w in bodies if ok(m) and (not role or role_of(c.cls, c.spec) == role)]
        if full:  # every seat taken: switches by people already coming before new people
            return sorted(out, key=lambda mv: (0 if mv[0] == "swap" else 1, MOVE_COST[mv[1]]))
        return sorted(out, key=lambda mv: (MOVE_COST[mv[1]], 0 if mv[0] == "body" else 1))

    def person(mv) -> int:
        return mv[2]["uid"] if mv[0] == "swap" else mv[2].discord_id

    def count(role: str) -> int:
        return have.get(_dps(role), 0) if _dps(role) == "dps" else have.get(role, 0)

    def reason_for(role: str, k: int) -> str:
        return f"short {k} {role}" if role else f"short {headcount}"

    def build(mv, role: str, k: int, used: set[int]) -> tuple[list[FillAsk], set[int]] | None:
        """The asks for one move (a swap with its backfill when the vacated role would go short), or None."""
        head = f"short {k} {_role_word(role)} · " if role else ""
        tail = " · the sheet is full: the lock would bench someone" if (mv[0] == "body" and full and not locked) else ""
        if mv[0] == "body":
            _, kind, m, c, words = mv
            a = FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind=kind, role=role_of(c.cls, c.spec), character=c.label, spec=c.spec,
                        reason=reason_for(role, k), why=f"{head}{c.label} ({c.spec}) · {words}{tail}")
            return [a], {m.discord_id}
        _, kind, p, character, spec, words = mv
        vac = p["role"]
        left, floor = count(vac) - 1, need.get(_dps(vac), 0)
        unsigned = "" if p["signed"] else " · a regular, hasn't answered yet"
        a = FillAsk(discord_id=p["uid"], display_name=p["name"], kind=kind, role=role, character=character, spec=spec, reason=reason_for(role, k), vacates=vac)
        if left >= floor:
            a.why = f"{head}{words}{unsigned} · {_role_word(vac)} stays {left}" + (f" of {floor} minimum" if floor else "")
            return [a], {p["uid"]}
        if free[0] is not None and free[0] <= 0:
            return None  # after lock the backfill would need a seat nobody has
        back = next((b for b in bodies if ok(b[1]) and b[1].discord_id not in used and b[1].discord_id != p["uid"] and _dps(role_of(b[2].cls, b[2].spec)) == _dps(vac)), None)
        if not back:
            return None
        bk, bm, bc, bw = back
        a.pair = bm.discord_id
        a.why = f"{head}{words}{unsigned} · {_role_word(vac)} would drop under its minimum, so {bm.display_name} covers it"
        b = FillAsk(discord_id=bm.discord_id, display_name=bm.display_name, kind=bk, role=role_of(bc.cls, bc.spec), character=bc.label, spec=bc.spec,
                    reason=f"covering {p['name']}'s {vac} seat", why=f"covering {p['name']}'s {_role_word(vac)} seat · {bc.label} ({bc.spec}) · {bw}", pair=p["uid"])
        return [a, b], {p["uid"], bm.discord_id}

    def cover_sets(exclude: set[int], roles_needed: set[str]) -> list[set[str]]:
        """Per person still free, the short roles they have some move for (the matching check's input)."""
        by: dict[int, set[str]] = {}
        for r in roles_needed:
            for mv in moves(r):
                uid = person(mv)
                if uid not in exclude:
                    by.setdefault(uid, set()).add(r)
        return list(by.values())

    def apply_counts(opt: list[FillAsk]) -> None:
        for a in opt:
            if a.swap:
                have[a.vacates] = have.get(a.vacates, 0) - 1
                if _dps(a.vacates) == "dps":
                    have["dps"] = have.get("dps", 0) - 1
            have[a.role] = have.get(a.role, 0) + 1
            if _dps(a.role) == "dps":
                have["dps"] = have.get("dps", 0) + 1
            if not a.swap and free[0] is not None:
                free[0] -= 1

    # ---- 1. the role seats: suggestions first (so alternates never take someone a later seat's suggestion needs)
    used: set[int] = set()
    need_list = [r for r, k in short_roles.items() for _ in range(k)]
    seats: list[PlanSeat] = []
    for i, role in enumerate(need_list):
        rest = need_list[i + 1:]
        chosen = None
        for mv in moves(role):
            if person(mv) in used:
                continue
            got = build(mv, role, short_roles[role], used)
            if not got:
                continue
            opt, people = got
            # the matching check: never take someone a later seat can't do without
            if rest and _can_cover(cover_sets(used, set([role] + rest)), [role] + rest) and not _can_cover(cover_sets(used | people, set(rest)), rest):
                continue
            chosen = (opt, people)
            break
        st = PlanSeat(role=role)
        if chosen:
            st.options.append(chosen[0])
            used |= chosen[1]
            apply_counts(chosen[0])
        seats.append(st)
    # ---- 2. after lock, the seats still free (plain headcount)
    if locked:
        for _ in range(max(0, free[0] or 0)):
            mv = next((mv for mv in moves("") if person(mv) not in used), None)
            st = PlanSeat(role="")
            if mv:
                opt, people = build(mv, "", 0, used)
                st.options.append(opt)
                used |= people
                apply_counts(opt)
            seats.append(st)
    # ---- 3. alternates per seat: people no suggestion uses, distinct per role
    shown: dict[str, set[int]] = {}
    for st in seats:
        if not st.options:
            continue
        taken = shown.setdefault(st.role, set())
        # an alternate REPLACES this seat's suggestion: the free seats its bodies took are the alternate's to take (after
        # lock the suggestions may have used up every free seat, which would leave body seats with no alternates)
        back = sum(1 for a in st.options[0] if not a.swap) if free[0] is not None else 0
        if free[0] is not None:
            free[0] += back
        for mv in moves(st.role):
            if len(st.options) > PLAN_ALTERNATES:
                break
            uid = person(mv)
            if uid in used or uid in taken:
                continue
            got = build(mv, st.role, short_roles.get(st.role, 0), used | taken)
            if got and not got[1] & (used | taken):
                st.options.append(got[0])
                taken |= got[1]
        if free[0] is not None:
            free[0] -= back
    plan.seats = seats
    plan.short_after = {"headcount": sum(1 for st in seats if not st.role and not st.options),
                        "roles": {r: k for r in short_roles if (k := sum(1 for st in seats if st.role == r and not st.options))}}
    plan.rev = plan_rev(plan)
    return plan


def early_warning(reg: Registry, rs: "RaidStore | None", ev: RaidEvent, plan: "Plan | None" = None) -> bool:
    """Post the officer health card as soon as a recurring sheet opens? Yes when the series has regulars and, counting
    the ones who haven't answered (minus anyone away) as coming, a tank or healer minimum is still short — officers
    see the hole (and the Suggested block) days before the reminder. Never for ad hoc or test runs, or a series
    nobody curated (nothing to base it on). `plan`: role_plan already computed for this run (ev_json passes its own)."""
    if ev.state != "open" or run_kind(reg, ev) != "recurring" or is_test_run(reg, ev) or expected_from(reg, ev) != "regulars":
        return False
    return bool((plan or role_plan(reg, rs, ev)).short["roles"])


def withheld_swaps(reg: Registry, rs: "RaidStore | None", ev: RaidEvent) -> bool:
    """After lock (any run kind), would a role swap cover a gap the fill engine can't cover on its own? (D3 / D22: such
    swaps wait for an officer's Ask.)"""
    if ev.state == "open" or not ev.all_rosters:
        return False
    with_swaps = role_plan(reg, rs, ev)
    return any(a.swap for a in with_swaps.asks)


def auto_swaps(reg: Registry, ev: RaidEvent) -> bool:
    """May the fill engine send role swaps (flex / offspec / alt) on its own? Never — owner decisions D3 and D22: on
    EVERY run kind (recurring and ad hoc alike) same-role fill asks stay automatic and a role swap waits for an
    officer's Ask (fill_state "suggested", the lock card lists it)."""
    return False


def fill_candidates(reg: Registry, rs: RaidStore, ev: RaidEvent, team: dict | None = None, *, swaps: bool = True) -> list[FillAsk]:
    """The planner's suggestions in send order (role_plan(...).asks). Pure; nothing is sent."""
    return role_plan(reg, rs, ev, team, swaps=swaps).asks


def fill_batch(reg: Registry, rs: RaidStore, ev: RaidEvent, team: dict | None = None, *, swaps: bool = True) -> list[FillAsk]:
    """The next asks the fill engine sends now: shortfall + FILL_OVERASK, capped so at most FILL_MAX_OPEN are
    outstanding — never cutting a tied swap from its backfill."""
    open_asks = [a for a in ev.fill_asks if a.pending and not parallel_shadowed(ev, a)]  # a parallel twin is not a second ask out
    nd = needs(reg, ev, team)
    want = nd["headcount"] + sum(nd["roles"].values())
    if want <= 0:
        return []
    room = max(0, min(want + FILL_OVERASK, FILL_MAX_OPEN) - len(open_asks))
    plan = role_plan(reg, rs, ev, team, swaps=swaps, count_waiting=False)
    batch = plan.asks[:room]  # the engine's over-ask: its own asks out aren't counted
    ids = {a.discord_id for a in batch}
    batch = [a for a in batch if a.pair is None or a.pair in ids]
    return add_parallel_twins(reg, ev, plan, batch)


def pick_asks(plan: Plan, picks: dict | None = None, limit: int | None = None) -> list[FillAsk]:
    """The asks an officer sends: each seat's suggestion, or the alternate they picked (`picks` = {seat index: option
    index}, 0 = the suggestion). `limit`: only the first that many seats someone can cover (a card's Ask sends the
    seats the card draws, raid_views.SUGGEST_LINES). Refuses a pick that asks one person for two seats."""
    out: list[FillAsk] = []
    seen: set[int] = set()
    shown = 0
    for i, st in enumerate(plan.seats):
        if not st.options:
            continue
        if limit is not None and shown >= limit:
            break
        shown += 1
        k = int((picks or {}).get(i, (picks or {}).get(str(i), 0)) or 0)
        if not 0 <= k < len(st.options):
            raise RegistryError(f"seat {i + 1} has no option {k + 1}")
        for a in st.options[k]:
            if a.discord_id in seen:
                raise RegistryError(f"{a.display_name} is picked for two seats — pick someone else for one of them")
            seen.add(a.discord_id)
            out.append(a.model_copy())
    return out


def send_batch(plan: Plan, picks: dict | None = None, limit: int | None = None) -> tuple[list[FillAsk], int]:
    """Exactly what an officer's send of this plan (with `picks`, pick_asks) DMs now, and how many of the picked asks
    wait: before lock all of them; after lock at most `plan.room` (FILL_MAX_OPEN out at once), never a swap without
    its tied backfill. The preview screens (the wizard, the plain-text diff, the site and MCP) show this list and
    RaidMixin.send_plan sends it — the cap is part of the plan's rev, so what was shown is what goes. RegistryError
    when nothing can go out now. `limit`: only the first that many seats (pick_asks — a card's Ask)."""
    asks = pick_asks(plan, picks, limit)
    if plan.phase == "prelock" or not asks:
        return asks, 0
    if plan.room == 0:
        raise RegistryError(f"{plan.out} asks are still out — at most {FILL_MAX_OPEN} at a time; ask again once some are answered.")
    keep = asks[:plan.room]
    ids = {a.discord_id for a in keep}
    keep = [a for a in keep if a.pair is None or a.pair in ids]  # never a swap without its tied backfill
    if not keep:
        raise RegistryError(f"That swap and its backfill are two asks and only {plan.room} more can go out now (at most {FILL_MAX_OPEN}); ask again once some are answered.")
    return keep, len(asks) - len(keep)


ASK_HOW = {"sub": "from the bench", "regular": "a regular, no answer yet", "unanswered": "no answer yet", "other": "not a regular",
           "flex": "declared flex", "offspec": "offspec", "alt": "brings an alt"}


def batch_lines(asks: list[FillAsk]) -> list[str]:
    """One line per ask an officer is about to send (plain text: the wizard, the plain-text diff, MCP): who, as which
    character and spec, for which short role, SWAP marked with the role it leaves, the tied backfill named."""
    names = {a.discord_id: a.display_name for a in asks}
    out = []
    for a in asks:
        line = f"{a.display_name} as {a.character} ({a.spec}) for {a.role} — {a.reason}; {ASK_HOW.get(a.kind, a.kind)}"
        if a.swap:
            line += " · SWAP" + (f" (leaves {a.vacates})" if a.vacates else "")
        if a.pair is not None:
            other = names.get(a.pair, "?")
            line += f" · backfill: {other}" if a.vacates else f" · backfills {other}'s swap"
        out.append(line)
    return out


def batch_preview(plan: Plan, picks: dict | None = None) -> list[str]:
    """The batch a send would DM (send_batch), one ask per line, then what waits and what stays short — or why
    nothing can go out now. What every officer-triggered send shows before its Send / Apply."""
    try:
        asks, held = send_batch(plan, picks)
    except RegistryError as e:
        return [f"Nothing can go out now: {e}"]
    if not asks:
        return ["Nobody to ask."] + [f"Still short if everyone says yes: {still_short_words(plan)}"]
    return ([f"{n + 1}. {ln}" for n, ln in enumerate(batch_lines(asks))]
            + ([f"{held} more wait — at most {FILL_MAX_OPEN} asks out at once"] if held else [])
            + [f"Still short if everyone says yes: {still_short_words(plan)}"])


def plan_lines(reg: Registry, plan: Plan, limit: int = 8) -> list[str]:
    """The plan in words (plain text, MCP, help, the wizard): one line per seat with its alternates, then what stays
    short. Code-written — Claude may rephrase it, never invent one."""
    out = []
    for i, st in enumerate(plan.seats[:limit]):
        if not st.options:
            continue
        main = st.options[0]
        out.append(f"{i + 1}. " + " + ".join(_ask_words(a) for a in main) + (f" — {main[0].why}" if main[0].why else ""))
        if len(st.options) > 1:
            out.append("   alternates: " + " · ".join(" + ".join(_ask_words(a) for a in opt) for opt in st.options[1:]))
    left = still_short_words(plan)
    out.append(f"Still short if everyone says yes: {left}")
    return out


def _ask_words(a: FillAsk) -> str:
    if a.kind in ("flex", "offspec"):
        return f"{a.display_name} → {a.spec} ({a.kind})"
    if a.kind == "alt":
        return f"{a.display_name} brings {a.character} ({a.spec})"
    return f"{a.display_name} as {a.character} ({a.spec}, {a.kind})"


def held_back_words(plan: Plan) -> str:
    """What an officer's send after lock left for later (the FILL_MAX_OPEN cap), or ""."""
    return f" · {plan.held_back} more wait — at most {FILL_MAX_OPEN} asks out at once; ask again once some are answered" if plan.held_back else ""


def still_short_words(plan: Plan) -> str:
    sa = plan.short_after
    bits = ([f"{sa['headcount']} seat{'s' if sa['headcount'] != 1 else ''}"] if sa.get("headcount") else []) + [f"{k} {r}" for r, k in sa.get("roles", {}).items()]
    return ", ".join(bits) + " → recruit" if bits else "nothing"


def _reseat(ev: RaidEvent, sg: Signup, guild_key: str | None = None) -> bool:
    """A swap by someone already seated: update their seat in place (character, spec, role) instead of seating twice.
    The seat and its group entry take the signup's CURRENT display name (a re-answer after a nick change), so the
    roster never carries two names for one person — a stale `signup_name` left a ghost in the groups once `free_seat`
    filtered them by the old name while the board had written the new one."""
    hit = ev.seat_of(sg.discord_id)
    if not hit:
        return False
    i, p = hit
    before = (p.cls, p.spec, p.character)
    p.cls, p.spec, p.role, p.offspec, p.character = sg.cls, sg.spec, sg.role, sg.offspec, sg.character
    relabel_seat(ev.all_rosters[i], p, sg.display_name)
    if before != (p.cls, p.spec, p.character):
        on_roster_change(guild_key, ev, changed=[sg.discord_id])
    return True


def relabel_seat(r: RosterResult, p: Player, name: str) -> None:
    """One name per seat: `p` is now known as `name` in this roster (its Player and the group entry that carries it)."""
    old = p.signup_name
    if not name or name == old:
        return
    p.signup_name = name
    r.groups = [[name if n == old else n for n in g] for g in r.groups]


def tied_ask(ev: RaidEvent, ask: FillAsk) -> Optional[FillAsk]:
    """The ask tied to this one (swap ↔ backfill) in any state: sent together, so the same phase (one ask per member per
    phase — a member asked before lock may be asked again after it), the latest such."""
    if ask.pair is None:
        return None
    return next((a for a in reversed(ev.fill_asks) if a is not ask and a.discord_id == ask.pair and a.pair == ask.discord_id and a.phase == ask.phase), None)


def partner_of(ev: RaidEvent, ask: FillAsk) -> Optional[FillAsk]:
    """The tied ask still in play (unanswered, or a yes held until this one answers), if any."""
    other = tied_ask(ev, ask)
    return other if other and other.pending else None


def release_partner(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk, because: str | None = None) -> Optional[FillAsk]:
    """A no (or silence) on one half of a tied pair withdraws the other half — unanswered, or a yes that was held
    waiting for this one (nothing had changed for it). Returns the released ask."""
    other = partner_of(ev, ask)
    if other:
        other.answer, other.answered_at, other.held = "released", now(), False
        ev.log.append(f"fill: {other.display_name}'s ask withdrawn ({because or ask.display_name + ' said no'})")
        rs.save(ev, f"fill {other.display_name} released")
    return other


def run_day(reg: Registry, ev: RaidEvent) -> str:
    return ev.start.astimezone(reg.tz).date().isoformat()


def ask_live(reg: Registry, ev: RaidEvent, a: FillAsk) -> bool:
    """Can the person asked still come? Not when they're away that day or answered No thanks since: their ask then
    covers no seat (role_plan) and a late yes on it is refused (apply_fill_answer)."""
    m = reg.members.get(a.discord_id)
    if m is not None and m.absent_on(run_day(reg, ev)):
        return False
    sg = ev.signups.get(str(a.discord_id))
    return not (sg is not None and sg.status == "out")


def release_member_asks(reg: Registry, rs: RaidStore, ev: RaidEvent, uid: int, why: str) -> list[FillAsk]:
    """Someone can't come any more (an absence, No thanks, a callout): their asks still in play close (`released`),
    and the tied half of each pair is withdrawn too (release_partner). Returns the partners released (the caller
    tells them); saves when anything closed."""
    mine = [a for a in ev.fill_asks if a.discord_id == uid and a.pending]
    if not mine:
        return []
    partners = []
    for a in mine:
        a.answer, a.answered_at, a.held = "released", now(), False
        ev.log.append(f"{'suggestion' if a.phase == 'prelock' else 'fill'}: {a.display_name}'s ask closed ({why})")
        other = release_partner(reg, rs, ev, a, because=f"{a.display_name}: {why}")
        if other:
            partners.append(other)
    rs.save(ev, f"asks closed for {mine[0].display_name}")
    return partners


def expire_fill_asks(reg: Registry, rs: RaidStore, ev: RaidEvent) -> tuple[list[FillAsk], list[FillAsk]]:
    """Open asks past their deadline count as no. Returns (expired, released partners). An ask that waited on the site
    (channel "web", DMs off) gets a notice saying it closed (design §2.5 — the outcome reaches them somewhere); a DM
    ask's card is the scheduler's to edit (RaidMixin.close_ask_card)."""
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
            if a.channel == "web":
                reg.notify(a.discord_id, "ask_closed", f"The seat question for {run_words(reg, ev)} closed at {reg.local12(t)} without your answer. "
                           "Nothing to do.", run=ev.key)
        rs.save(ev, f"fill expired {len(expired)}")
    return expired, released


def apply_fill_answer(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk, yes: bool) -> str:
    """Record the answer and, on yes, put the person on the sheet the way they were asked. Before lock (an officer's
    pre-lock ask) a yes changes the SIGNUP only — Join, with the spec or character they agreed to — and seats nobody:
    the lock still decides, and now sees the agreed role. After lock (or a pre-lock ask answered once the run locked)
    a yes takes the seat, or switches the seat in place. A tied pair (a swap and the backfill covering the seat it
    vacates) needs BOTH yeses: the first yes is held (`held`, nothing changes) until the partner answers; the second
    yes applies both; a no or silence from the partner releases the held yes (release_partner)."""
    m = reg.members.get(ask.discord_id)
    if yes and not ask_live(reg, ev, ask):  # away that day (or No thanks since): the absence stands, the yes counts as no
        ask.answer, ask.answered_at, ask.held = "no", now(), False
        away = m is not None and m.absent_on(run_day(reg, ev))
        ev.log.append(f"{'suggestion' if ev.state == 'open' else 'fill'}: {ask.display_name}'s yes not taken ({'away that day' if away else 'answered No thanks'})")
        rs.save(ev, f"fill {ask.display_name} yes refused")
        return (f"{ask.display_name} is marked away that day, so the yes wasn't taken — clear the absence first, then answer the sheet" if away
                else f"{ask.display_name} answered No thanks for this run, so the yes wasn't taken — change the answer on the sheet first")
    ask.answer, ask.answered_at = ("yes" if yes else "no"), now()
    if not yes or not m:
        ev.log.append(f"{'suggestion' if ev.state == 'open' else 'fill'}: {ask.display_name} declined ({ask.kind})")
        rs.save(ev, f"fill {ask.display_name} no")
        return f"{ask.display_name} can't ({'swap' if ask.swap else ask.kind})"
    other = partner_of(ev, ask)
    if other is not None and other.answer is None:  # the partner hasn't answered: hold this yes, change nothing
        ask.held = True
        ev.log.append(f"{'suggestion' if ev.state == 'open' else 'fill'}: {ask.display_name} said yes — waiting for {other.display_name} (a tied pair needs both)")
        rs.save(ev, f"fill {ask.display_name} yes, held")
        return f"{ask.display_name} said yes — waiting for {other.display_name}; nothing changes until both say yes"
    close_parallel(reg, rs, ev, ask)  # D14: the first yes of a parallel pair takes the seat; the other ask closes
    line = _apply_yes(reg, rs, ev, ask, m)
    if other is not None and other.held:  # the partner said yes first: now both happen
        other.held = False
        om = reg.members.get(other.discord_id)
        if om:
            line += " · " + _apply_yes(reg, rs, ev, other, om)
    return line


def _apply_yes(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk, m: Member) -> str:
    """One yes taking effect (apply_fill_answer): the signup before lock, the seat after."""
    if ev.state == "open":
        return _prelock_yes(reg, rs, ev, ask, m)
    if ask.kind in ("offspec", "flex") and str(ask.discord_id) in ev.signups and ev.signups[str(ask.discord_id)].character == ask.character:
        s = ev.signups[str(ask.discord_id)]
        s.spec, s.offspec, s.role, s.note, s.source, s.updated_at = ask.spec, s.spec, ask.role, f"switched to {ask.spec} at request", "fill", now()
        _reseat(ev, s, reg.key)
        ev.log.append(f"fill: {ask.display_name} switches to {ask.spec}")
        rs.save(ev, f"fill {ask.display_name} offspec")
        return f"{ask.display_name} swaps to {ask.spec} ({ask.role})"
    c = next((c for c in m.active() if c.label == ask.character), m.main)
    s = carry_joined(ev.signups.get(str(m.discord_id)), _signup(reg, m, c, "in", "fill", "filled in at request" if ask.kind != "alt" else f"swapped to {c.label} at request"))
    if ask.kind in ("offspec", "flex") and ask.spec != c.spec:  # a switch by someone who had no signup on that character
        s.spec, s.offspec, s.role = ask.spec, c.spec, ask.role
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"fill: {ask.display_name} in as {c.label} ({ask.kind})")
    if ev.state != "open" and ev.all_rosters and not _reseat(ev, s, reg.key) and seat_player(reg, ev, s) is None:
        # every seat was taken meanwhile: a Join, not rostered (the board can move them in); never reported as seated
        ev.log.append(f"fill: no free seat for {ask.display_name}")
        rs.save(ev, f"fill {ask.display_name} in, no seat")
        return f"{ask.display_name} said yes as {c.label} ({ask.role}), but there is no free seat — not rostered (move them in on the board)"
    rs.save(ev, f"fill {ask.display_name} in")
    return f"{ask.display_name} {'swaps to' if ask.kind == 'alt' else 'is in as'} {c.label} ({ask.role})"


def _prelock_yes(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk, m: Member) -> str:
    """A yes to a pre-lock ask: the signup becomes Join with what they agreed to (an offspec / flex spec on the same
    character, an alt, or just Join); nobody is seated — the lock builds the roster from the signups."""
    c = next((c for c in m.active() if c.label == ask.character), None) or m.main
    if not c:
        rs.save(ev, f"fill {ask.display_name} yes (no character)")
        return f"{ask.display_name} said yes, but has no active character — set their answer on the sheet"
    old = ev.signups.get(str(m.discord_id))
    s = carry_joined(old, _signup(reg, m, c, "in", "fill"))
    if ask.kind in ("offspec", "flex") and ask.spec != c.spec:
        s.spec, s.offspec, s.role = ask.spec, c.spec, ask.role
    what = f"{c.label} as {s.spec}" if s.spec != c.spec or ask.kind in ("offspec", "flex") else c.label
    s.note = f"agreed before lock to come as {what}"
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"suggestion: {ask.display_name} said yes — Join as {what} ({ask.role})" + (f", was {LABELS.get(old.status, old.status)} as {old.character} {old.spec}" if old else ""))
    rs.save(ev, f"suggestion {ask.display_name} yes")
    return f"{ask.display_name} said yes: Join as {what} ({ask.role}) — the lock builds the roster"


def release_prelock_asks(ev: RaidEvent) -> list[FillAsk]:
    """At lock: pre-lock asks nobody answered close (`released`) — they expire at lock time; so does a yes held for a
    tied partner who never answered (nothing had changed for it). The caller saves."""
    gone = [a for a in ev.fill_asks if a.pending and a.phase == "prelock"]
    for a in gone:
        a.answer, a.answered_at, a.held = "released", now(), False
    if gone:
        ev.log.append(f"pre-lock asks closed at lock: {', '.join(a.display_name for a in gone)}")
    return gone


# ---------------------------------------------------------------- health

def health_data(reg: Registry, ev: RaidEvent, team: dict) -> dict:
    """Structured health check: headcount, per-role tiles, per-buff providers, non-responders. A run that expects N
    rosters (its schedule's `rosters`) is measured against N × the seats and N × each role minimum."""
    profile = reg.profile
    ins, subs = ev.by_status("in"), ev.by_status("sub")
    g = gaps(reg, ev)  # the one role-need calculation (seats and minimums × the rosters in play)
    n_rosters, size, need = g["rosters"], g["seats"], g["need"]
    counts = {r: sum(1 for s in ins if s.role == r) for r in ROLES}
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
            off = [m.display_name for m, _c in run_pool(reg, ev) if str(m.discord_id) not in ev.signups and any(profile.spec(c.cls, c.spec).role == r for c in m.active())]
            hint = ("not on sheet: " + ", ".join(off[:3])) if off else "nobody can cover → recruit"
        roles.append({"role": r, "have": have, "need": n, "level": level, "hint": hint, "cover": cover})
    buffs = []
    for b in profile.party_buffs():
        providers = [s.display_name for s in ins if b.provided_by(profile.spec(s.cls, s.spec))]
        buffs.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "name": b.short, "providers": providers})
    unresp = [m.display_name for m, _c in not_answered(reg, ev)]  # the expected (regulars) who haven't answered; ad hoc: nobody
    hc_level = "green" if len(ins) >= size else ("amber" if len(ins) + len(subs) >= size else "red")
    # headcount is a 4-tuple (in, size, 0 [was tentative], sub): the cards and the pool data share the shape
    return {"headcount": (len(ins), size, 0, len(subs)), "headcount_level": hc_level, "roles": roles, "buffs": buffs, "unresponsive": unresp, "rosters": n_rosters,
            "expected_from": expected_from(reg, ev), "kind": run_kind(reg, ev)}


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
    for i, s in enumerate(sorted(ev.signups.values(), key=lambda s: s.order_at)):  # first come = when the Join began
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
        players.append(Player(signup_name=s.display_name, discord_id=s.discord_id, pos=i + 1, status="signed" if s.status == "in" else "bench", cls=s.cls, spec=s.spec, role=s.role, offspec=s.offspec, character=s.character, map_confidence="high", unmapped=False, rank=rank, tank_capable_main=tank_alt))
    return players


def player_uid(ev: RaidEvent, p: Player) -> int | None:
    """Who a seat belongs to: the Player's discord_id, else (files from before it was stored) the run's own signup
    under the name it was seated with — display names change, ids don't."""
    if p.discord_id is not None:
        return p.discord_id
    sg = signup_by_name(ev, p.signup_name)
    return sg.discord_id if sg else None


def uid_of_name(ev: RaidEvent, name: str) -> int | None:
    """A board name as a member id: the run's signup under that name, else a seat still carrying it (the name the
    person had when seated, before a nick change). None when the run knows nobody by it."""
    sg = signup_by_name(ev, name)
    if sg is not None:
        return sg.discord_id
    p = next((p for p in ev.seated() if p.signup_name == name), None)
    return player_uid(ev, p) if p is not None else None


def seat_uid(ev: RaidEvent, who) -> int | None:
    """`who` (a Member, a Discord id, a display name, a Signup or a Player) as the member id seats are keyed by."""
    if who is None:
        return None
    if isinstance(who, bool):
        return None
    if isinstance(who, int):
        return who
    if isinstance(who, str):
        return int(who) if who.isdigit() else uid_of_name(ev, who)
    uid = getattr(who, "discord_id", None)
    if uid is None and isinstance(who, Player):
        return player_uid(ev, who)
    return int(uid) if uid is not None else None


def last_window(reg: Registry, rs: RaidStore, ev: RaidEvent) -> list[RaidEvent]:
    """Done runs of the same raid within one lockout before this one: what sat-out credit and rotation look back on."""
    lockout = int(reg.raid_def(ev.instance)["lockout_days"])
    return [e for e in rs.events.values() if e.instance == ev.instance and e.key != ev.key and e.state == "done" and ev.start - timedelta(days=lockout) <= e.start < ev.start]


def sat_out_last_window(reg: Registry, rs: RaidStore, ev: RaidEvent, first_only: bool = False) -> set[int]:
    """discord ids who Joined a run of the last window but were not seated (in roster 1 only, with `first_only`)."""
    prev = last_window(reg, rs, ev)
    seated = {player_uid(e, p) for e in prev for p in ((e.all_rosters[0].selected if e.all_rosters else []) if first_only else e.seated())}
    signed = {sg.discord_id for e in prev for sg in e.signups.values() if sg.status == "in"}
    return signed - seated


def run_weights(reg: Registry, ev: RaidEvent) -> dict[str, int]:
    """The seat weights a run's lock uses: its schedule's (own weights over the raid's — ad hoc: over ADHOC_WEIGHTS),
    read live like the raid's always were. A schedule removed since falls back by class."""
    team = reg.config.team(ev.team) or {}
    sid = team.get("schedule_id") or ev.schedule
    try:
        return dict(reg.schedule_def(ev.instance, sid)["weights"])
    except RegistryError:
        base = ADHOC_WEIGHTS if run_kind(reg, ev) == "adhoc" else reg.raid_def(ev.instance)["weights"]
        return {**RAID_WEIGHT_DEFAULTS, **base}


def seat_bonus(reg: Registry, rs: RaidStore, ev: RaidEvent, players: list[Player]) -> dict[str, int]:
    """Per-player selection bonus from the run's weights: rank, main over alt, sat out last window, signup order, and
    being a REGULAR of the run's series signed with their regular character (D9: a weight above one rank step, so a
    strong outsider can still win a role seat). A signup an officer's ask changed (source "fill": "Wren brings Fern")
    keeps the main and regular weights the member had — doing the officers a favour never costs the seat. Ad hoc runs are first come: signup order counts per position (each
    earlier joiner `signup_order` points ahead of the next), rank and sat-out don't count by default.
    Keyed by this run's signup names (the solver's handle); sat-out and regulars match discord ids, so a rename keeps them."""
    w = run_weights(reg, ev)
    adhoc = run_kind(reg, ev) == "adhoc"
    sat = sat_out_last_window(reg, rs, ev) if int(w.get("sat_out", 0)) else set()
    regs = {} if adhoc or is_test_run(reg, ev) else {m.discord_id: c.label for m, c in run_regulars(reg, ev)}
    n = max(1, len(players))
    out = {}
    for p in players:
        b = int(w.get("rank", 0)) * (4 - RANK_ORDER.get(p.rank, 3))
        uid = player_uid(ev, p)
        sg = ev.signups.get(str(uid)) if uid is not None else None
        asked = sg is not None and sg.source == "fill"  # agreed to an officer's ask: what they were asked to bring counts as theirs
        found = reg.find(p.character) if p.character else None
        if (found and found[1].is_main) or asked:
            b += int(w.get("main", 0))
        if uid in sat:
            b += int(w.get("sat_out", 0))
        if uid in regs and (regs[uid] == p.character or asked):
            b += int(w.get("regular", 0))
        b += int(w.get("signup_order", 0)) * (n - p.pos) if adhoc else round(int(w.get("signup_order", 0)) * (n - p.pos) / n)
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


def how_many_rosters(reg: Registry, players: list[Player], size: int, rb: dict, want: int = 1) -> int:
    """How many runs to build: as many full runs as the Join answers support (bodies), or the `want` rosters the
    schedule declares up front when that is more — either way never more than the tank/healer minimums per run allow."""
    signed = [p for p in players if p.status == "signed"]
    n = min(MAX_ROSTERS_PER_SLOT, max(len(signed) // size, want))
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
    """Distinct people for every role seat in `need` (registry.can_cover: the one bipartite matching)."""
    return can_cover(people, need)


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
    for m, c in run_pool(reg, ev):
        if c and str(m.discord_id) not in ev.signups and not m.absent_on(day):
            add(m, role_of(c.cls, c.spec))
    need = [r for r in ("tank", "healer") for _ in range(int(rb[r]["min"] or 0))]
    if len(any_role) < size or not _can_cover(any_role, need):
        return None
    how = "" if _can_cover(main_only, need) else " with offspecs or alts"
    return f"Leftovers, bench and pool ({len(any_role)}) could make another run{how}."


def first_come_out(players: list[Player], seats: int, role_min: dict[str, int], role_max: dict[str, int], fixed: set[str]) -> set[str]:
    """Ad hoc seating is first come, first rostered (D4): the joiners in signup order take the seats, except that a seat
    still needed for a tank/healer minimum is kept for the first later joiner who plays that role (and a role at its
    maximum takes no more). `fixed` (pinned in / placed on the board) are seated first. Returns the joiners who come
    too late — the lock benches them (hard), the solver only arranges the groups of the rest."""
    signed = sorted((p for p in players if p.status == "signed"), key=lambda p: p.pos)
    have = {r: 0 for r in set(role_min) | set(role_max)}
    taken: list[str] = []
    for p in signed:
        if p.signup_name in fixed:
            taken.append(p.signup_name)
            if p.role in have:
                have[p.role] += 1
    out: set[str] = set()
    rest = [p for p in signed if p.signup_name not in fixed]
    for i, p in enumerate(rest):
        free = seats - len(taken)
        if free <= 0:
            out.add(p.signup_name)
            continue
        if p.role in role_max and have.get(p.role, 0) >= role_max[p.role]:
            out.add(p.signup_name)
            continue
        fills = p.role in role_min and have.get(p.role, 0) < role_min[p.role]
        later = rest[i + 1:]
        reserved = sum(min(max(0, n - have.get(r, 0)), sum(1 for q in later if q.role == r)) for r, n in role_min.items())
        if not fills and free <= reserved:
            out.add(p.signup_name)
            continue
        taken.append(p.signup_name)
        if p.role in have:
            have[p.role] += 1
    return out


def merged_role_min(rb: dict, policy: dict[str, int] | None) -> dict[str, int]:
    """The lock's role minimums: the raid's tank/healer minimums (role_bounds) with the comp policy's role_min laid
    over them PER ROLE — a policy line about healers never drops the tank minimum."""
    return {**{r: int(rb[r]["min"]) for r in ("tank", "healer") if rb.get(r, {}).get("min")}, **{r: int(n) for r, n in (policy or {}).items()}}


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
    n_rosters = max(how_many_rosters(reg, players, size, rb, want=run_rosters(reg, ev)), -(-len(layout) // k) if any(layout) else 1)
    strategy = strategy or ev.split_strategy or (reg.config.team(ev.team) or {}).get("split_policy") or rd.get("split_policy") or "balanced"
    bonus = seat_bonus(reg, rs, ev, players)
    roster_bonus = None
    if strategy == "rotation":
        w = int(rd["weights"].get("sat_out", 2))
        missed_first = sat_out_last_window(reg, rs, ev, first_only=True)
        roster_bonus = {0: {p.signup_name: 3 * w for p in players if player_uid(ev, p) in missed_first}}
    pool = {p.signup_name for p in players}
    role_min = merged_role_min(rb, cc["role_min"])
    late: set[str] = set()
    benched = {x for x in pin_out + [resolve(n) for n in cc["always_bench"]] if x and x in pool and x not in placed}
    kept = {x for x in pin_in + list(placed) + [resolve(n) for n in cc["never_bench"]] if x and x in pool and x not in pin_out}
    if run_kind(reg, ev) == "adhoc":  # first come, first rostered: the late joiners are benched before the solve
        # a joiner benched by a pin / always_bench takes no first-come seat, and a never_bench joiner keeps one —
        # otherwise the same name lands in force_in and force_out, or a seat goes unfilled (INFEASIBLE)
        late = first_come_out([p for p in players if p.signup_name not in benched], size * n_rosters,
                              {r: n * n_rosters for r, n in role_min.items()},
                              {r: int(rb[r]["max"]) * n_rosters for r in ("tank", "healer") if rb[r].get("max")}, kept)
    opts = solver.SolveOptions(
        time_limit_s=time_limit, raid_size=size,
        keep_together=tuple((resolve(a), resolve(b)) for a, b in cc["keep_together"] if resolve(a) and resolve(b)),
        keep_apart=tuple((resolve(a), resolve(b)) for a, b in cc["keep_apart"] if resolve(a) and resolve(b)),
        force_in=tuple(x for x in pin_in + list(placed) + [resolve(n) for n in cc["never_bench"]] if x and x in pool and x not in pin_out),
        force_out=tuple(x for x in pin_out + [resolve(n) for n in cc["always_bench"]] + sorted(late - kept) if x and x in pool and x not in placed),
        pins={n: gi for n, gi in placed.items() if gi < k * n_rosters} or None,
        prefer_group={resolve(n): g for n, g in cc["prefer_group"].items() if resolve(n)},
        role_min=role_min or None,
        role_max={r: rb[r]["max"] for r in ("tank", "healer") if rb[r].get("max")},
        bonus=bonus,
    )
    # "another one": for a split the rosters must differ, for a single run the groups must
    rosters = solver.solve_rosters(reg.profile, players, raid_id, opts, rosters=n_rosters, strategy=strategy, roster_bonus=roster_bonus, avoid=avoid if n_rosters > 1 else None, avoid_groups=avoid_groups if n_rosters == 1 else None)
    # bench what-ifs re-solve the model per benched player (the bench hangs off rosters[0]): capped by explain.WHATIF_*,
    # only at lock (never for previews), skipped for big splits / sheets
    do_whatif = whatif and explain.whatif_allowed(n_rosters, len(players))
    tank_min = (opts.role_min or {}).get("tank", 0)
    rosters = [explain.annotate(reg.profile, players, raid_id, r, whatif=do_whatif and i == 0, tank_min=tank_min) for i, r in enumerate(rosters)]
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


class BoardChanged(RegistryError):
    """A whole-board write made on a board that changed since its author looked (its `rev` is stale)."""

    def __init__(self, msg: str = "The board changed since you looked (someone else moved people) — check it and try again."):
        super().__init__(msg)


class SolverBusy(RegistryError):
    """RaidMixin.solving: the solver is already working on this run (a lock, an auto-fill, a split preview)."""

    def __init__(self, msg: str = "the solver is already working on this run — try again in a moment"):
        super().__init__(msg)


def board_rev(reg: Registry, ev: RaidEvent) -> str:
    """A fingerprint of the board (groups + pins + state). A whole-board write carrying an older one is stale."""
    import hashlib
    import json

    blob = json.dumps([ev.state, board_layout(reg, ev), sorted(ev.pins.items())], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


LOCK_SOLVES = 3  # lock_run solves again when the answers or pins moved under it; after this many it gives up (sheet stays open)


def solve_inputs(ev: RaidEvent) -> str:
    """What the lock solve reads from the run (every answer's status, character, class, spec and role; the pins; the
    board; the split strategy). lock_run compares it before and after the solve: an answer or pin written meanwhile
    (a sheet press, set_answer, the site's pin) takes no lock while the run is open, so the roster is solved again."""
    import json

    sgs = sorted((k, s.display_name, s.character, s.cls, s.spec, s.offspec, s.role, s.status) for k, s in ev.signups.items())
    return json.dumps([sgs, sorted(ev.pins.items()), ev.layout, ev.split_strategy])


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
    # the board is names (what people drag); seats are keyed by member id, so a name is resolved once (uid_of_name: the
    # signup, else the seat still carrying the name from before a nick change) and the groups are rewritten with the
    # current names
    n = groups_per_roster(reg, run_size(reg, ev))
    before = {player_uid(ev, p): p for p in ev.seated()}
    players = {player_uid(ev, p): p for p in players_for(reg, ev)}
    layout_uids = [[u for u in (uid_of_name(ev, m) for m in g) if u is not None and u in players] for g in layout]
    after = {u for g in layout_uids for u in g}
    removed_uids = [u for u in before if u not in after]
    removed = [before[u].signup_name for u in removed_uids]
    for u in removed_uids:
        free_seat(reg, rs, ev, u, "moved to bench")
    players = {player_uid(ev, p): p for p in players_for(reg, ev)}  # after the frees: a freed signup is out, not on the bench
    added: list[Signup] = []
    for i in range(max(len(ev.all_rosters), -(-len(layout_uids) // n))):
        guids = layout_uids[i * n:(i + 1) * n] + [[] for _ in range(n - len(layout_uids[i * n:(i + 1) * n]))]
        groups = [[players[u].signup_name for u in g] for g in guids]
        if i >= len(ev.rosters):
            ev.rosters.append(RosterResult(selected=[], benched=[], groups=groups, group_reports=[], objective=0, synergy_value=0, role_counts={}, advisories=[]))
        r = ev.rosters[i]
        r.groups = groups
        want = [u for g in guids for u in g]
        keep = {player_uid(ev, p): p for p in r.selected if player_uid(ev, p) in want}
        for u in want:
            if u not in keep:
                sg = ev.signups.get(str(u))
                if sg and u not in before:
                    added.append(sg)
                keep[u] = players[u]
            elif keep[u].signup_name != players[u].signup_name:  # the groups were just written with the signup's name: the kept seat follows
                keep[u].signup_name = players[u].signup_name
        r.selected = [keep[u] for u in want]
        r.role_counts = {role: sum(1 for p in r.selected if p.role == role) for role in ROLES}
    ev.rosters = [r for r in ev.rosters if r.selected or r is ev.rosters[0]]
    seated = {player_uid(ev, p) for p in ev.seated()}
    ev.rosters[0].benched = [p for u, p in players.items() if u not in seated and p.status == "signed"]
    for sg in added:
        sg.joined_at = sg.order_at if sg.status == "in" else now()
        sg.status, sg.source, sg.updated_at = "in", "officer", now()
    ev.log.append("board: " + (f"+{', '.join(s.display_name for s in added)} " if added else "") + (f"-{', '.join(removed)}" if removed else "") or "board: groups moved")
    rs.save(ev, "board updated")
    on_roster_change(rs.key, ev, added=[s.discord_id for s in added])  # the removed were told by free_seat
    return added, removed


# ---------------------------------------------------------------- after lock: confirmations and freed seats

def confirmations(reg: Registry, ev: RaidEvent) -> list[dict]:
    """One row per seated player: their confirmation state for this run (placement ask on the run's roster key)."""
    out = []
    for i, r in enumerate(ev.all_rosters):
        for p in r.selected:
            m = reg.members.get(player_uid(ev, p)) if player_uid(ev, p) is not None else None
            if m is None:
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


def free_seat(reg: Registry, rs: RaidStore, ev: RaidEvent, who, why: str) -> Player | None:
    """Take someone off the locked roster (decline, callout, absence, missed confirmation): seat opens, signup → out.
    `who`: a Member, a Discord id or a display name (ev.seat_of) — the seat is found by id, so a nick change mid-run
    can't leave it standing."""
    hit = ev.seat_of(who)
    if not hit:
        return None
    i, p = hit
    uid = player_uid(ev, p)
    r = ev.all_rosters[i]
    r.selected = [x for x in r.selected if x is not p]
    # the group entry goes by id as well as by name: a name the person took since (the signup's) is theirs too
    r.groups = [[n for n in g if n != p.signup_name and (uid is None or uid_of_name(ev, n) != uid)] for g in r.groups]
    p.status = "bench"
    ev.all_rosters[0].benched.append(p)
    sg = ev.signups.get(str(uid)) if uid is not None else None
    if sg is None:
        sg = next((s for s in ev.signups.values() if s.display_name == p.signup_name), None)
    if sg:
        sg.status, sg.source, sg.updated_at = "out", why, now()
    name = sg.display_name if sg else p.signup_name
    ev.log.append(f"seat freed: {name} ({why})")
    rs.save(ev, f"seat freed {name}")
    if uid is not None:
        seat_gone(reg, ev, uid, why)  # their open Confirm ask closes (a later Confirm is refused) and their inbox hears why
    on_roster_change(rs.key, ev, removed=[uid if uid is not None else name], roster_of={uid if uid is not None else name: i})
    return p


def seat_player(reg: Registry, ev: RaidEvent, sg: Signup) -> int | None:
    """Put a signup into the first roster with a free seat (after a fill yes or an officer add). Returns the roster index."""
    size = run_size(reg, ev)
    found = reg.find(sg.character)
    p = Player(signup_name=sg.display_name, discord_id=sg.discord_id, pos=len(ev.signups), status="signed", cls=sg.cls, spec=sg.spec, role=sg.role, offspec=sg.offspec, character=sg.character, rank=found[1].rank if found else "unknown")
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
            on_roster_change(reg.key, ev, added=[sg.discord_id])
            return i
    return None


def on_roster_change(guild_key: str | None, ev: RaidEvent, added=(), removed=(), changed=(), roster_of: dict | None = None) -> None:
    """Tell ROSTER_LISTENERS that a locked run's seats moved (seat_player, free_seat, an alt / offspec reseat, the board
    after lock — set_answer and drop_seated reach these). `roster_of`: removed uid → the roster index the seat was freed
    from (a split run's councils each hear only of their own roster). A listener never breaks the seat change."""
    if not guild_key or not (added or removed or changed):
        return
    change = {"added": list(added), "removed": list(removed), "changed": list(changed)}
    if roster_of:
        change["roster_of"] = {str(k): v for k, v in roster_of.items()}
    for fn in list(ROSTER_LISTENERS):
        try:
            fn(guild_key, ev.key, change)
        except Exception:  # noqa: BLE001
            log.exception("roster listener failed")


def end_run(reg: Registry, rs: RaidStore, ev: RaidEvent, by: str) -> bool:
    """A run is over: `done` (LootMixin.end_run and the scheduler's close come here). Idempotent; a cancelled run stays
    cancelled. Returns True when it changed."""
    if ev.state in ("done", "cancelled"):
        return False
    ev.prev_state, ev.state, ev.closed_at = ev.state, "done", now()
    ev.log.append(f"done ({by})")
    rs.save(ev, f"done ({by})")
    return True


REOPEN_GRACE_HOURS = 1.0  # a run ended / closed by mistake can be reopened this long after (never a cancelled one, D19)


def closed_recently(ev: RaidEvent, hours: float = REOPEN_GRACE_HOURS, at: datetime | None = None) -> bool:
    """Done / cancelled less than `hours` ago (by `closed_at`; a file from before the stamp counts as long closed)."""
    if ev.state not in ("done", "cancelled") or not ev.closed_at:
        return False
    t = at or datetime.now(ev.start.tzinfo)
    return t - datetime.fromisoformat(ev.closed_at) < timedelta(hours=hours)


def reopen_refusal(reg: Registry, ev: RaidEvent, at: datetime | None = None) -> str | None:
    """Why `ev` can't be reopened now, or None (design B6, owner decision D19): only a run ENDED (done) by mistake,
    within REOPEN_GRACE_HOURS of the end, before its scheduled close, and only back to the state it ended from. A
    cancelled run is never reopened — open a new one."""
    if ev.state == "cancelled":
        return "A cancelled run can't be reopened — open a new run instead."
    if ev.state != "done":
        return f"{ev.key} is {ev.state}, not ended — nothing to reopen."
    t = at or datetime.now(ev.start.tzinfo)
    if not closed_recently(ev, at=t):
        return f"It ended more than {REOPEN_GRACE_HOURS:g} hour ago — reopening is for a run ended by mistake, within the hour."
    if t >= run_close_at(reg, ev):
        return "Its scheduled close has passed — the run is over."
    if not reg.config.team(ev.team):
        return "Its roster settings were already cleaned up — open a new run instead."
    return None


def reopen_run(reg: Registry, rs: RaidStore, ev: RaidEvent, by: str) -> str:
    """Undo an end by mistake (RaidMixin.reopen_run is the surfaces' verb): the run goes back to the state it ended
    from (locked; open for a run ended before lock) with its rosters, asks and loot archive untouched. RegistryError
    with the reason (reopen_refusal) otherwise. Returns the state it is back in."""
    why = reopen_refusal(reg, ev)
    if why:
        raise RegistryError(why)
    back = ev.prev_state if ev.prev_state in ("open", "locked") else ("locked" if ev.all_rosters else "open")
    ev.state, ev.closed_at, ev.prev_state = back, None, None
    ev.log.append(f"reopened by {by} ({back})")
    rs.save(ev, f"reopened by {by}")
    return back


def rostered_runs(reg: Registry, rs: RaidStore, m: Member) -> list[tuple[RaidEvent, str]]:
    """The live locked runs (not past their close) where `m` holds a seat, with the character seated — read from the run files (the member
    file keeps no per-run membership: RegisteredCharacter.rosters is for regular series, roster redesign Phase 0).
    Shown by /me view, the registration card's status, help and the site's member JSON."""
    out = []
    for ev in rs.live():
        if ev.state == "open":
            continue
        if datetime.now(ev.start.tzinfo) >= run_close_at(reg, ev):
            continue  # the raid is over; a loot council with a pick still waiting may keep it locked — nobody is "rostered" on it
        p = next((p for p in ev.seated() if player_uid(ev, p) == m.discord_id), None)
        if p is not None:
            out.append((ev, p.character or p.signup_name))
    return out


def expire_confirmations(reg: Registry, rs: RaidStore, ev: RaidEvent) -> list[str]:
    """expire_confirmation_asks, as the display names on the run (its lines)."""
    return [name for _uid, name in expire_confirmation_asks(reg, rs, ev)]


def expire_confirmation_asks(reg: Registry, rs: RaidStore, ev: RaidEvent) -> list[tuple[int, str]]:
    """Past confirm_by: unanswered confirmations count as out and their seats open. One commit for the lot. Returns
    (member id, display name on the run) per seat freed — callers find the member BY ID (a rename since the Join, or
    two members sharing a name, never edits the wrong card).
    Only a seat whose member HOLDS an open ask expires (design B2 "expiry correctness"): a seated member nobody asked
    — a lock whose Confirm / Can't fan-out was interrupted, the scheduler's recovery step still to come — keeps the
    seat; "no answer" needs a question first."""
    if not ev.confirm_by or datetime.fromisoformat(ev.confirm_by) > datetime.now(ev.start.tzinfo):
        return []
    gone = []
    with rs.store.batch(f"{rs.key}: raid {ev.key}: confirmations expired"):
        for row in confirmations(reg, ev):
            if row["answer"] is None and row["uid"]:
                m = reg.members.get(int(row["uid"]))
                ask = next((a for a in reversed(m.placement_asks) if a["roster"] == ev.team and a.get("answer") is None), None) if m else None
                if ask is None:
                    continue  # never asked: not an unanswered ask
                ask["answer"], ask["answered_at"] = "expired", now()
                reg.save(m, f"{m.display_name} didn't confirm {ev.team} in time")
                free_seat(reg, rs, ev, int(row["uid"]), "no-confirm")
                gone.append((int(row["uid"]), row["display_name"]))
    return gone


def seated_by_fill(ev: RaidEvent, uid: int) -> bool:
    """The member's seat (or their switch on it) came from a fill ask they answered yes after lock: that yes was the
    commitment — the lock's Confirm / Can't is not theirs to answer."""
    sg = ev.signups.get(str(uid))
    return (sg is not None and sg.source == "fill") or any(
        a.discord_id == uid and a.answer == "yes" and a.phase == "fill" for a in ev.fill_asks)


def legacy_lock_sent(reg: Registry, ev: RaidEvent) -> bool:
    """A run locked before `lock_seats` / `confirmations_sent_at` were recorded whose fan-out evidently ran: a
    placement or fill ask on it already exists, or its confirmation deadline has passed. The recovery step stamps it and
    asks nobody (a deploy must never re-ask every locked run)."""
    if ev.lock_seats is not None or ev.confirmations_sent_at:
        return False
    if ev.confirm_by and datetime.fromisoformat(ev.confirm_by) <= datetime.now(ev.start.tzinfo):
        return True
    if any(a.phase == "fill" for a in ev.fill_asks):
        return True
    return any(a["roster"] == ev.team for m in reg.members.values() for a in m.placement_asks)


def unasked_seats(reg: Registry, ev: RaidEvent) -> list[tuple[int, int, Signup]]:
    """(roster index, group index, signup) of every member rostered AT THE LOCK FLIP (ev.lock_seats) who is still
    seated and has NO placement ask on this run since it locked — the Confirm / Can't fan-out never reached them (a
    crash or a Discord error after the lock flipped). Never a seat that came later (a fill yes, an officer's add after
    lock: they carry their own ask or none) nor a fill-seated member. A legacy run (no lock_seats) whose fan-out
    evidently ran (legacy_lock_sent) has none. The scheduler's recovery step asks exactly these
    (RaidMixin.send_confirmations(resume=True)). Empty when confirmations are off."""
    if ev.state != "locked" or not ev.all_rosters or not run_confirms(reg, ev) or legacy_lock_sent(reg, ev):
        return []
    since = ev.locked_at or ""
    at_lock = set(ev.lock_seats) if ev.lock_seats is not None else None
    out = []
    for i, r in enumerate(ev.all_rosters):
        for gi, g in enumerate(r.groups):
            for n in g:
                uid = uid_of_name(ev, n)
                sg = ev.signups.get(str(uid)) if uid is not None else None
                m = reg.members.get(uid) if uid is not None else None
                if not sg or not m:
                    continue
                if (at_lock is not None and uid not in at_lock) or seated_by_fill(ev, uid):
                    continue
                if any(a["roster"] == ev.team and (a.get("asked_at") or "") >= since for a in m.placement_asks):
                    continue
                out.append((i, gi, sg))
    return out


def open_placement_rows(reg: Registry, ev: RaidEvent) -> list[tuple[Member, dict]]:
    """(member, ask) for every UNANSWERED placement ask on this run's roster key — Remind's targets, cancel's cards."""
    return [(m, a) for m in reg.members.values() for a in m.placement_asks if a["roster"] == ev.team and a.get("answer") is None]


REMIND_EVERY = timedelta(hours=1)  # "Remind the unconfirmed": at most once per hour per run (D15)


def remind_refusal(reg: Registry, ev: RaidEvent, at: datetime | None = None) -> str | None:
    """Why Remind can't go now, or None: a locked run with confirmations, someone still unconfirmed whom a DM can reach,
    and not within the hour of the last reminder."""
    if ev.state != "locked" or not ev.all_rosters:
        return "Remind is for a locked roster — the sheet is still open." if ev.state == "open" else f"That run is {ev.state}."
    if not run_confirms(reg, ev):
        return "Nobody is asked to confirm on this run (Join is the commitment) — nothing to remind."
    t = at or datetime.now(ev.start.tzinfo)
    if ev.reminded_at and t - datetime.fromisoformat(ev.reminded_at) < REMIND_EVERY:
        nxt = datetime.fromisoformat(ev.reminded_at) + REMIND_EVERY
        return (f"Reminded at {reg.local12(ev.reminded_at, '%I:%M %p')}" + (f" by {ev.reminded_by}" if ev.reminded_by else "")
                + f" — once an hour; again after {reg.local12(nxt, '%I:%M %p')}.")
    rows = open_placement_rows(reg, ev)
    if not rows:
        return "Everyone rostered has answered — nobody to remind."
    if not any(a.get("channel", "dm") == "dm" and not m.dm_opt_out for m, a in rows):
        return "The unconfirmed all wait on the site (DMs off) — a reminder can't reach them."
    return None


def cancel_effects(reg: Registry, ev: RaidEvent) -> dict:
    """Who a cancel touches, from the run as it stands (consequences.consequence("cancel"), the plain diff, MCP):
    `rostered` names of the seated (after lock), `open_asks` names with an unanswered Confirm / Can't, `fill_asks` names
    with a fill or pre-lock ask in play, `answered` how many answered the sheet. Nobody is DMed by a cancel (the DM
    rule): the sheet, the open ask cards, the signup channel line and the run's thread carry it."""
    locked = ev.state != "open" and bool(ev.all_rosters)
    return {"state": ev.state, "locked": locked,
            "rostered": [p.signup_name for p in ev.seated()] if locked else [],
            "open_asks": [m.display_name for m, _a in open_placement_rows(reg, ev)] if locked else [],
            "fill_asks": [a.display_name for a in ev.fill_asks if a.pending],
            "answered": len([s for s in ev.signups.values() if s.status in STATUSES])}


# ---------------------------------------------------------------- opening: which run a picked time is (design B4)

def which_run(reg: Registry, instance: str, start: datetime, sid: str | None = None) -> dict:
    """What an open at `start` would be — the one answer the /raid open wizard, the site's preview route and plain text
    share: {kind: scheduled | adhoc, schedule (id; None = the raid's own run times; ADHOC_SCHEDULE = a one-off),
    schedule_name, key}. `sid` named → that schedule's run (a pickup template is ad hoc); none → one of the raid's
    upcoming runs at exactly `start` (scheduled), else a one-off (ad hoc)."""
    if instance not in reg.profile.raids:
        raise RegistryError(f"unknown raid {instance or '?'} (one of {', '.join(reg.profile.raids)})")
    if sid and sid not in (DEFAULT_SCHEDULE, ADHOC_SCHEDULE) and not any(s["id"] == sid for s in reg.schedules(instance)):
        raise RegistryError(f"{reg.raid_def(instance).get('name', instance)} has no schedule {sid}")
    resolved = open_schedule(reg, instance, start, sid)
    if resolved == ADHOC_SCHEDULE:
        kind, name = "adhoc", "One-off"
    else:
        s = next((x for x in reg.schedules(instance) if x["id"] == (resolved or DEFAULT_SCHEDULE)), None) or {}
        kind = "adhoc" if schedule_class(s.get("kind") or "weekly") == "adhoc" else "scheduled"
        name = s.get("name") or (resolved or DEFAULT_SCHEDULE)
    return {"kind": kind, "schedule": resolved, "schedule_name": name, "key": event_key(instance, start, resolved)}


def open_when(reg: Registry, instance: str, value: str | None, schedule: str | None = None) -> tuple[datetime, str | None, str]:
    """A picked open time → (start, schedule id or None, DST note): the raid's next run (of `schedule` when named) when
    `value` is empty; a bare 'HH:MM' = the next upcoming run at that clock time; 'YYYY-MM-DD HH:MM' (guild wall time,
    from a picker — never typed by a person) resolved DST-safely by wizard.resolve_local (on the spring-forward night
    2:30 AM becomes 3:30 — note "gap"; on the fall-back night the first 1:30 is taken — note "ambiguous"). An aware ISO
    instant (the preview's `start`, carried back by Open) is taken as is. configops._open_time and the site's open and
    preview routes share it, so a DST night opens the same instant everywhere."""
    import re

    from .wizard import resolve_local

    if instance not in reg.profile.raids:
        raise RegistryError(f"unknown raid {instance or '?'} (one of {', '.join(reg.profile.raids)})")
    sid = (schedule or "").strip().lower() or None
    sched = next((s for s in reg.schedules(instance) if s["id"] == sid), None) if sid and sid != ADHOC_SCHEDULE else None
    if sid and sid != ADHOC_SCHEDULE and sched is None:
        raise RegistryError(f"{instance} has no schedule {sid} (it has {', '.join(s['id'] for s in reg.schedules(instance)) or 'none'})")
    when = (value or "").strip()
    nxt = [(s, t) for s, t in upcoming(reg, instance, reg.now_local(), 24 * OPEN_HORIZON_DAYS) if sid in (None, ADHOC_SCHEDULE) or s["id"] == sid]
    if when:
        if re.fullmatch(r"\d{1,2}:\d{2}", when):  # a bare time: the next upcoming run at that clock time
            hit = next(((s, t) for s, t in nxt if t.astimezone(reg.tz).strftime("%H:%M") == when.zfill(5)), None)
            if hit is None:
                raise RegistryError(f"{instance} has no upcoming run at {reg.time_label(when.zfill(5))} — give the date too")
            return hit[1], hit[0]["id"], ""
        try:
            parsed = datetime.fromisoformat(when.replace(" ", "T", 1))
        except ValueError:
            raise RegistryError("I couldn't tell which time you meant — pick a day and time with the picker, or name them, e.g. Thu 10 Dec 7:30 PM (guild time)")
        if parsed.tzinfo is not None:
            start, note = parsed.astimezone(reg.tz), ""
        else:
            start, note = resolve_local(parsed, reg.tz)
        if sid is None:  # one of the raid's own upcoming runs: open it as that schedule's run; any other time is a one-off (ad hoc)
            sid = open_schedule(reg, instance, start, None)
        return start, sid, note
    if sid == ADHOC_SCHEDULE:
        raise RegistryError("an ad hoc run starts at the day and time you pick — pick one")
    if sched is not None and sched["kind"] == "pickup":
        raise RegistryError(f"{sched['name']} is a pickup template — pick the date and time it starts")
    if not nxt:
        fo = reg.first_open(instance)
        has = [s for s in reg.schedules(instance) if s["kind"] != "pickup" and (sched is None or s["id"] == sid)]
        why = (f"its slots all fall before it opens on {reg.local12(fo)}" if has and fo and fo > reg.now_local() else "it has no recurring slots yet")
        raise RegistryError(f"No next slot for this raid: {why}. Pick a date and time instead, or set it up on the Raids page.")
    return nxt[0][1], nxt[0][0]["id"], ""


def dst_words(reg: Registry, note: str, start: datetime) -> str:
    """The DST note in words for a person (the wizard's when_note, the site's preview): "" on an ordinary night."""
    if note == "gap":
        return f"That time doesn't exist that night (the clocks go forward) — using {reg.local12(start, '%I:%M %p')}."
    if note == "ambiguous":
        return f"{reg.local12(start, '%I:%M %p')} happens twice that night (the clocks go back) — using the first."
    return ""


def open_preview(reg: Registry, rs: "RaidStore", instance: str, value: str | None = None, schedule: str | None = None) -> dict:
    """What Open would do, before anything is written (POST /api/raid/{rid}/open/preview; the wizard's confirm shows
    the same facts): {kind, schedule, schedule_name, key, start (ISO — the exact instant Open must carry back), label
    (12-hour guild time), when_note (the DST line or ""), exists (the live run's state at that key, or None), channel
    (the signup channel id the sheet posts in, or None), rules (what kind of run it is, in one line), past, closed
    (done | cancelled when that time's run is closed — Open is refused) and refusal (the sentence why, or None)}."""
    start, sid, note = open_when(reg, instance, value, schedule)
    w = which_run(reg, instance, start, sid if sid is not None else DEFAULT_SCHEDULE)
    ev = rs.events.get(w["key"])
    live = ev if ev is not None and ev.state not in ("done", "cancelled") else None
    refusal = closed_run_refusal(rs, w["key"])
    sd = reg.schedule_def(instance, w["schedule"])
    lock_h = float(sd["lock_hours_before"])
    if w["kind"] == "adhoc":
        rules = ("Ad hoc: first come, first rostered; no regulars, no reminder" + ("" if sd.get("confirm") else ", no confirmations (Join is the commitment)")
                 + f"; locks {lock_h:g} h before; a role swap waits for an officer.")
    else:
        rules = ("Scheduled: its regulars are expected" + ("; the rostered are asked to confirm" if sd.get("confirm", True) else "")
                 + f"; locks {lock_h:g} h before.")
    return {**w, "start": start.isoformat(), "label": reg.local12(start), "when_note": dst_words(reg, note, start), "exists": live.state if live else None,
            "closed": ev.state if refusal else None, "refusal": refusal, "channel": reg.config.signup_channel_id or None, "rules": rules, "past": start < reg.now_local() - timedelta(minutes=PAST_GRACE_MINUTES)}


# ---------------------------------------------------------------- what the lock decided, as data (the site's locked SheetCard)

def spec_switches(reg: Registry, ev: RaidEvent) -> list[dict]:
    """'Playing an offspec': every seat the lock put on a spec other than the one they signed up with, one row each
    {roster, uid, display_name, character, cls, spec, signed_spec} — the lock card's lines as data."""
    out = []
    for i, r in enumerate(ev.all_rosters):
        by = {p.signup_name: p for p in r.selected}
        for n in r.spec_switches or []:
            p = by.get(n)
            if p is None:
                continue
            uid = player_uid(ev, p)
            sg = ev.signups.get(str(uid)) if uid is not None else None
            out.append({"roster": i + 1, "uid": str(uid) if uid is not None else None, "display_name": p.signup_name, "character": p.character or p.signup_name,
                        "cls": p.cls, "spec": p.spec, "signed_spec": sg.spec if sg else None})
    return out


def regulars_not_rostered(reg: Registry, ev: RaidEvent) -> list[dict]:
    """A recurring run's regulars who answered Join / Bench but hold no seat (who gets priority next time) — the lock
    card's 'Regulars not rostered' as data. Empty for ad hoc and test runs and before lock."""
    if ev.state == "open" or not ev.all_rosters or run_kind(reg, ev) != "recurring" or is_test_run(reg, ev):
        return []
    seated = {player_uid(ev, p) for p in ev.seated()}
    out = []
    for m, c in run_regulars(reg, ev):
        sg = ev.signups.get(str(m.discord_id))
        if sg and sg.status in ("in", "sub") and m.discord_id not in seated:
            out.append({"uid": str(m.discord_id), "display_name": m.display_name, "character": c.label, "cls": c.cls, "spec": c.spec, "status": sg.status})
    return out


FILL_STATE_WORDS = {"idle": "nothing to fill yet", "asking": "asking — waiting for answers", "filled": "every seat filled",
                    "exhausted": "nobody left to ask — recruit, or move someone in on the board",
                    "suggested": "waiting for an officer: a role swap would cover it"}


def fill_state_text(reg: Registry, ev: RaidEvent) -> str:
    """The fill engine's state in words (the lock card, the SheetCard, MCP get_run) — never the bare code:
    'waiting for an officer: a role swap would cover it', 'asking — waiting for answers (2 on the site)',
    'autofill paused by Jon · …'."""
    if ev.state == "open":
        return "the fill engine starts after lock"
    if ev.state in ("done", "cancelled"):
        return f"the run is {'over' if ev.state == 'done' else 'cancelled'}"
    words = FILL_STATE_WORDS.get(ev.fill_state, ev.fill_state)
    site = sum(1 for a in ev.fill_asks if a.pending and a.channel == "web")
    if site:
        words += f" ({site} on the site)"
    if ev.autofill_paused:
        words = "autofill paused" + (f" by {ev.autofill_paused_by}" if ev.autofill_paused_by else "") + " · " + words
    return words


def asks_on_site(reg: Registry, ev: RaidEvent) -> int:
    """How many asks in play wait on the members' Me pages (DMs off, or the DM failed): fill / pre-lock asks with
    channel "web" plus unanswered Confirm / Can't asks with channel "web" — the "N wait on the site" count."""
    fills = sum(1 for a in ev.fill_asks if a.pending and a.channel == "web")
    places = sum(1 for _m, a in open_placement_rows(reg, ev) if a.get("channel") == "web") if ev.state == "locked" else 0
    return fills + places


# a log line's officer-only marks: free text people wrote (a reason, a note) and planning a member never reads
_OFFICER_LOG = ("cancelled by", "lock (", "fill", "suggestion", "pre-lock", "pin ", "pinned", "board", "proposed", "reminded",
                "autofill", "callout", "early warning", "confirmation dms")


def log_rows(reg: Registry, ev: RaidEvent) -> list[dict]:
    """ev.log for people (the site's Detail › log): {at (ISO or ""), when (12-hour guild time; "" for a line from before
    stamps were kept), text, officer (planning or free text a member never sees)}. The raw lines stay `ev.log` (the
    site's "machine form")."""
    stamps = list(ev.log_at or [])
    out = []
    for i, line in enumerate(ev.log):
        at = stamps[i] if i < len(stamps) else ""
        low = line.lower()
        out.append({"at": at, "when": reg.local12(at) if at else "", "text": line, "officer": any(k in low for k in _OFFICER_LOG)})
    return out


def end_refusal(ev: RaidEvent, at: datetime | None = None) -> str | None:
    """Why "End the run now" can't go (the lock card, the SheetCard): only a locked run, and only after its scheduled
    start — before then, Cancel is the verb. None = it may."""
    if ev.state != "locked":
        return f"That run is {ev.state} — End is for a locked run." if ev.state in ("done", "cancelled") else "End is for a locked run — the sheet is still open."
    if (at or datetime.now(ev.start.tzinfo)) < ev.start:
        return "The run hasn't started yet — End is for after the start; before then, Cancel it."
    return None


# ---------------------------------------------------------------- character changes ripple into runs (design §3.3, D24)
# A character table save (Me page, Members page, /me char, /roster char, plain text, MCP — PeopleMixin.save_characters)
# can retire, delete, respec, name or promote a character that live sheets and locked rosters already hold. Sheets
# snapshot cls/spec/role at the press and the lock solves from the snapshot, so the run files are rewritten here (data
# only); seats to free go back to RaidMixin.drop_seated (free_seat + the card edit + the fill engine).

def _run_words(reg: Registry, ev: RaidEvent) -> str:
    return f"{reg.raid_def(ev.instance).get('name', ev.instance or ev.team)} {reg.local12(ev.start)}"


def character_changes(reg: Registry, m: Member | None, rows) -> list[dict]:
    """What a table save would change for runs, read from its rows against the member AS STORED NOW (call it before the
    save): [{kind: retire|delete|spec|name|main, label, …}]. `spec` carries cls/spec/offspec and the old and new role;
    `name` the new label (and so does `spec` on an unnamed, planned character: its label 'Class (Spec)' changes). A row
    the save will refuse is still listed — the save refuses before any ripple runs."""
    out: list[dict] = []
    if m is None:
        return out
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        cur = next((c for c in m.active() if c.label == label), None) if label else None
        if cur is None:
            continue
        if row.get("delete") or row.get("retire"):
            out.append({"kind": "delete" if row.get("delete") else "retire", "label": cur.label})
            continue
        spec, off = (row.get("spec") or "").strip(), (row.get("offspec") or "").strip() or None
        if spec and (spec, off) != (cur.spec, cur.offspec):
            try:
                new = reg.profile.spec(cur.cls, spec)
            except KeyError:
                new = None
            if new is not None and new.spec != cur.spec:
                out.append({"kind": "spec", "label": cur.label, "cls": cur.cls, "spec": new.spec, "offspec": off,
                            "role": new.role, "old_role": reg.profile.spec(cur.cls, cur.spec).role, "old_spec": cur.spec,
                            # a PLANNED character's label is 'Class (Spec)': the respec renames it, so the ripple
                            # re-finds it under the new label after the save (a named one keeps its label)
                            **({} if cur.name else {"new_label": f"{cur.cls} ({new.spec})"})})
        name = (row.get("name") or "").strip()
        if not cur.name and name:
            try:
                first = reg.normalise_name(name)
                last = reg.normalise_name(row["surname"]) if (row.get("surname") or "").strip() else None
            except RegistryError:
                first, last = None, None
            if first:
                out.append({"kind": "name", "label": cur.label, "new_label": f"{first} {last}" if last else first})
        if (row.get("main") or row.get("slot") == "main") and not cur.is_main:
            out.append({"kind": "main", "label": cur.label})
    return out


def character_effects(reg: Registry, rs: RaidStore, m: Member, change: dict) -> list[dict]:
    """What one character change does to the live runs, without doing it: [{ev, kind: seat|answer|regular|ask|rank,
    action: free|keep|rewrite|clear|repoint|none, text}] in the words every surface prints (the Discord confirm screens,
    the web's {preview: true}, plain text's diff). Rules (D24): retire / delete → an open sheet's Join or Bench with it is
    cleared (a No thanks moves to another character), a locked seat on it is handed back and the bench asked, its
    regular series drop it; a respec → the sheet answer is rewritten (sheets snapshot the spec), a locked seat in the
    same role follows it, a role flip on a locked RECURRING run keeps the seat as it is and tells the officers (an ad hoc
    run hands it back: first come); a name → answers and seats take the new name; a new main → the rank moves with it."""
    kind, label = change["kind"], change["label"]
    out: list[dict] = []
    char = next((c for c in m.characters if c.label == label), None)
    if kind == "main":
        if m.main is not None and m.main.label != label:
            out.append({"ev": None, "kind": "rank", "action": "none", "text": f"Rank {m.main.rank} moves to {label} with the main; {m.main.label} reads alt"})
        return out
    if kind in ("retire", "delete") and char is not None:
        for k in [k for k in char.rosters if ":" in k]:
            out.append({"ev": None, "kind": "regular", "action": "none",
                        "text": f"Regular of {reg.series_label(k)} — comes off it ({'retired' if kind == 'retire' else 'deleted'} characters aren't regulars)"})
    for ev in rs.live():
        if not on_bench_side(reg, ev, m):
            continue
        words = _run_words(reg, ev)
        sg = ev.signups.get(str(m.discord_id))
        seat = ev.seat_of(m) if ev.state != "open" else None
        if seat is not None and (seat[1].character or seat[1].signup_name) != label:
            seat = None
        if kind in ("retire", "delete", "spec") and any(a.discord_id == m.discord_id and a.pending and a.character == label for a in ev.fill_asks):
            out.append({"ev": ev, "kind": "ask", "action": "clear", "text": f"Has an open ask on {words} as {label} — it closes"})
        if seat is not None:
            role = seat[1].role
            if kind in ("retire", "delete"):
                out.append({"ev": ev, "kind": "seat", "action": "free", "text": f"Holds a {role} seat on {words} — the seat is handed back and the bench is asked"})
            elif kind == "spec" and change["role"] == role:
                out.append({"ev": ev, "kind": "seat", "action": "rewrite", "text": f"Rostered on {words} — the seat now reads {change['spec']}"})
            elif kind == "spec" and run_kind(reg, ev) == "recurring":
                out.append({"ev": ev, "kind": "seat", "action": "keep",
                            "text": f"Holds a {role} seat on {words} — {change['spec']} plays {change['role']}: the seat is kept as it is and the officers are told (they decide)"})
            elif kind == "spec":
                out.append({"ev": ev, "kind": "seat", "action": "free",
                            "text": f"Holds a {role} seat on {words} — {change['spec']} plays {change['role']}, so the seat is handed back (ad hoc: Join again to queue)"})
            elif kind == "name":
                out.append({"ev": ev, "kind": "seat", "action": "rewrite", "text": f"Rostered on {words} — the seat now reads {change['new_label']}"})
            continue
        if sg is None or sg.character != label:
            continue
        what = LABELS.get(sg.status, sg.status)
        if kind in ("retire", "delete"):
            other = next((c for c in m.active() if c.label != label), None)
            if sg.status == "out" and other is not None:
                out.append({"ev": ev, "kind": "answer", "action": "repoint", "text": f"Answered No thanks on {words} — it stays No thanks, as {other.label}"})
            else:
                out.append({"ev": ev, "kind": "answer", "action": "clear", "text": f"Answered {what} on {words} with {label} — that answer is cleared (answer again with another character)"})
        elif kind == "spec":
            out.append({"ev": ev, "kind": "answer", "action": "rewrite", "text": f"Answered {what} on {words} — the answer now reads {change['spec']}"})
        elif kind == "name":
            out.append({"ev": ev, "kind": "answer", "action": "rewrite", "text": f"Answered {what} on {words} — the answer now names {change['new_label']}"})
    return out


def character_preview(reg: Registry, rs: RaidStore, m: Member | None, changes: list[dict]) -> list[str]:
    """The ripple of a table save as sentences (empty = nothing outside the member's own record changes): the confirm
    screens and {preview: true} answers print exactly these."""
    if m is None:
        return []
    lines: list[str] = []
    for ch in changes:
        for e in character_effects(reg, rs, m, ch):
            if e["text"] not in lines:
                lines.append(e["text"])
    return lines


def after_character_change(reg: Registry, rs: RaidStore, m: Member, changes: list[dict]) -> dict:
    """Apply the data side of a saved table's ripple (call it AFTER the registry save, on the loop): rewrite or clear
    the sheet answers, re-label locked seats. Returns {"free": [(ev, why)], "keep": [(ev, text)], "release": [ev],
    "touched": [ev], "lines": [text]} — the seats to hand back, the role flips to tell officers about, the runs whose
    asks by this member close, every run touched (sheet + cards to redraw) and the sentences. Nothing is sent here."""
    renamed = {ch["label"]: ch["new_label"] for ch in changes if ch["kind"] == "name"}
    respecced = {ch["label"]: ch["new_label"] for ch in changes if ch["kind"] == "spec" and ch.get("new_label")}
    res: dict = {"free": [], "keep": [], "release": [], "touched": [], "lines": []}

    def touch(ev):
        if ev not in res["touched"]:
            res["touched"].append(ev)

    for ch in changes:
        # the character's label AFTER the save: a name given wins, else a planned character's respec ('Priest (Holy)' →
        # 'Priest (Shadow)'), else unchanged — looked up by the old label it would silently skip the rewrite
        now_label = renamed.get(ch["label"]) or respecced.get(ch["label"]) or ch["label"]
        for e in character_effects(reg, rs, m, ch):
            ev, act = e["ev"], e["action"]
            if e["text"] not in res["lines"]:
                res["lines"].append(e["text"])
            if ev is None:
                continue
            sg = ev.signups.get(str(m.discord_id))
            if e["kind"] == "ask":
                if ev not in res["release"]:
                    res["release"].append(ev)
                continue
            if act == "free":
                res["free"].append((ev, f"character {'respecced' if ch['kind'] == 'spec' else ch['kind'] + 'd'}"))
            elif act == "keep":
                res["keep"].append((ev, e["text"]))
                ev.log.append(f"{m.display_name}: {ch['old_spec']} → {ch['spec']} ({ch['old_role']} → {ch['role']}), seat kept for an officer")
                rs.save(ev, f"{m.display_name} respecced (seat kept)")
            elif act == "clear" and sg is not None:
                del ev.signups[str(m.discord_id)]
                ev.log.append(f"{m.display_name}'s answer cleared ({ch['label']} {ch['kind']}d)")
                rs.save(ev, f"{m.display_name} answer cleared")
            elif act == "repoint" and sg is not None:
                other = next((c for c in m.active() if c.label != ch["label"]), None)
                if other is not None:
                    set_signup(reg, rs, ev, m, other.label, "out", source=sg.source)
            elif act == "rewrite" and sg is not None:
                c = next((x for x in m.active() if x.label == now_label), None)
                if c is not None:
                    s = set_signup(reg, rs, ev, m, c.label, sg.status, source=sg.source if sg.status == "out" else "respec", note=sg.note)
                    if ev.seat_of(m) is not None:
                        _reseat(ev, s, reg.key)
                        rs.save(ev, f"{m.display_name} seat follows {c.label}")
            touch(ev)
    return res


def join_while_away(reg: Registry, ev: RaidEvent, m: Member, mine: bool = True) -> str | None:
    """A Join on a day the member is recorded away (design §3.2 B1): Join stays pure — it answers this sheet and the
    absence stands for the others. The receipt says so (and Discord offers I'm back). None when they aren't away."""
    day = run_day(reg, ev)
    a = m.absent_on(day)
    if a is None:
        return None
    who = "You're" if mine else f"{m.display_name} is"
    their = "your" if mine else "their"
    return (f"{who} marked away {reg.day_label(day)} — joined for this run; {their} absence {reg.span_label(a.start, a.end)} "
            "still stands for the other sheets (I'm back clears it).")

# ---------------------------------------------------------------- the member inbox (dual interfaces phase 2 B, design §5.40)
# D14 (DMs-off asks by phase), the receipt that renders state (§2.7), the on-behalf union (B5) and the notices the run's
# verbs write (D26: an outcome is a notice on the Me page and the card edited — never a courtesy DM).

RELEASE_NOTICE = {"no-confirm": "no confirmation came in time", "absence": "you're marked away that day", "moved to bench": "an officer moved you to the bench on the board",
                  "board": "the board was changed", "officer": "an officer took you off"}


def run_words(reg: Registry, ev: RaidEvent) -> str:
    """A run as a notice reads it: raid name + 12-hour guild time ('Barrow Deeps Tue 9 Dec 7:30 PM'), never the key."""
    return f"{reg.raid_def(ev.instance).get('name', ev.instance or ev.team)} {reg.local12(ev.start)}"


def reachable(m: Member | None) -> bool:
    """Can a DM reach them? Not with DMs off (their asks wait on the Me page)."""
    return m is not None and not m.dm_opt_out


def parallel_shadowed(ev: RaidEvent, a: FillAsk) -> bool:
    """A parallel twin while its primary is still in play: it covers the primary's seat, never a second one."""
    if a.parallel_of is None:
        return False
    return any(p is not a and p.discord_id == a.parallel_of and p.phase == a.phase and p.pending for p in ev.fill_asks)


def parallel_window(reg: Registry, ev: RaidEvent, at: datetime | None = None) -> bool:
    """D14: the last hours before start — when an ask can no longer run its whole fill_ask_hours (ask_deadline caps it at
    the start), a DMs-off candidate (who must find the Me page in time) gets a reachable twin asked in parallel."""
    at = at or datetime.now(ev.start.tzinfo)
    return ask_deadline(reg, ev, at) >= ev.start


def add_parallel_twins(reg: Registry, ev: RaidEvent, plan: "Plan", batch: list[FillAsk], at: datetime | None = None) -> list[FillAsk]:
    """The fill engine's batch with D14's parallel asks: after lock, in the last hours before start, every plain seat ask
    (not a swap, no tied partner) to someone a DM can't reach is followed by its seat's first alternate a DM CAN reach,
    marked `parallel_of`. Both go out; the first yes takes the seat (close_parallel). Before lock, outside the window,
    or with no reachable alternate: the batch as it was (same order — D14: the DMs-off person is not skipped)."""
    if plan.phase != "fill" or not batch or not parallel_window(reg, ev, at):
        return batch
    ids = {a.discord_id for a in batch}
    out: list[FillAsk] = []
    for a in batch:
        out.append(a)
        if a.swap or a.pair is not None or reachable(reg.members.get(a.discord_id)):
            continue
        seat = next((st for st in plan.seats if st.options and any(x.discord_id == a.discord_id for x in st.options[0])), None)
        for opt in (seat.options[1:] if seat else []):
            if len(opt) != 1 or opt[0].swap or opt[0].pair is not None or opt[0].discord_id in ids or not reachable(reg.members.get(opt[0].discord_id)):
                continue
            twin = opt[0].model_copy()
            twin.parallel_of, twin.reason = a.discord_id, a.reason
            twin.why = (twin.why or "") + f" · asked in parallel: {a.display_name}'s DMs are off and the raid is close — the first yes takes the seat"
            out.append(twin)
            ids.add(twin.discord_id)
            break
    return out


def parallel_linked(ev: RaidEvent, ask: FillAsk) -> list[FillAsk]:
    """The asks tied to `ask` by D14's parallel pairing (its primary, or its twins), any state, same phase."""
    return [a for a in ev.fill_asks if a is not ask and a.phase == ask.phase
            and ((ask.parallel_of is not None and a.discord_id == ask.parallel_of and a.parallel_of is None)
                 or (a.parallel_of is not None and a.parallel_of == ask.discord_id))]


def close_parallel(reg: Registry, rs: "RaidStore", ev: RaidEvent, ask: FillAsk) -> list[FillAsk]:
    """A yes on one ask of a parallel pair: the others still open close (`released`) — the seat is taken. Their DM card is
    the caller's to edit (RaidMixin.after_fill_answer); an ask that waited on the site gets a notice. Saves when any closed."""
    gone = [a for a in parallel_linked(ev, ask) if a.pending and not a.held]
    for a in gone:
        a.answer, a.answered_at, a.answered_by = "released", now(), None
        ev.log.append(f"fill: {a.display_name}'s ask withdrawn ({ask.display_name} said yes first)")
        if a.channel == "web":
            reg.notify(a.discord_id, "ask_closed", f"Never mind the seat question for {run_words(reg, ev)} — someone else took the seat first. Nothing to do.", run=ev.key)
    if gone:
        rs.save(ev, f"fill: parallel ask(s) closed for {ask.display_name}'s yes")
    return gone


def seat_gone(reg: Registry, ev: RaidEvent, uid: int, why: str) -> None:
    """A seat was freed (free_seat — every path: an officer, the board, an absence, expiry, a Can't make it): the member's
    OPEN Confirm ask on this run closes (`released`; a Confirm pressed later on the Me page or an old card is then
    refused from one place) and — unless it was their own doing, or an officer's on-behalf write that tells them
    itself — their inbox says why. Never a DM (the card is RaidMixin.seat_released's to edit)."""
    reg.close_placement_ask(uid, ev.team, "released")
    if why in ("declined", "callout", "callout (test)", "officer"):
        return
    reason = RELEASE_NOTICE.get(why, why)
    reg.notify(uid, "released", f"Your seat on {run_words(reg, ev)} was released ({reason}). Nothing to do — if that's wrong, tell an officer.", run=ev.key)


def leftover_notices(reg: Registry, ev: RaidEvent) -> int:
    """At lock: everyone who joined and was not rostered gets ONE notice (D26 — never a DM unless a seat is offered, the
    DM rule): how many seats, how many joined, and that a freed seat would reach them. Returns how many were told."""
    left = [s for s in ev.by_status("in") if not ev.seat_of(s.discord_id)]
    if not left or not ev.all_rosters:
        return 0
    seats = len(ev.all_rosters) * run_size(reg, ev)
    joined = len(ev.by_status("in"))
    with reg.store.batch(f"{reg.key}: notices — not rostered on {ev.key}"):
        for s in left:
            m = reg.members.get(s.discord_id)
            how = "you'd get a DM" if reachable(m) else "it would wait on your Me page"
            reg.notify(s.discord_id, "not_rostered", f"You weren't rostered for {run_words(reg, ev)} ({seats} seats, {joined} joined). You're in line for a freed seat — {how}.", run=ev.key)
    return len(left)


def cancel_notices(reg: Registry, ev: RaidEvent, by: str, by_id: int | None = None) -> int:
    """A cancelled run: one notice to everyone who joined or benched it, held a seat or had an ask open — never the
    reason (that is for officers) and never a DM. The canceller is not told. Returns how many were told."""
    who: dict[int, bool] = {}
    for s in ev.signups.values():
        if s.status in ("in", "sub"):
            who[s.discord_id] = False
    for p in ev.seated():
        uid = player_uid(ev, p)
        if uid is not None:
            who[uid] = True
    for a in ev.fill_asks:
        if a.answer == "cancelled":
            who.setdefault(a.discord_id, False)
    who.pop(by_id, None)
    with reg.store.batch(f"{reg.key}: notices — {ev.key} cancelled"):
        for uid, seated in who.items():
            reg.notify(uid, "cancelled", f"{run_words(reg, ev)} was cancelled by {by}." + (" Your seat is released — nothing to do." if seated else " Nothing to do."), run=ev.key)
    return len(who)


def on_behalf_notice(reg: Registry, ev: RaidEvent, uid: int, what: str, by: str) -> dict | None:
    """An officer changed a member's standing for them (B5: set_answer / call_out / a Confirm or fill answer recorded for
    them): one notice line — 'Jon set you to No thanks for Barrow Deeps Tue 7:30 PM'. Their open DM card, when there is
    one, is the verb's to edit; nothing else is sent."""
    return reg.notify(uid, "answered_for_you", f"{by} {what} for {run_words(reg, ev)}.", run=ev.key)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def answer_receipt(reg: Registry, ev: RaidEvent, m: Member) -> str:
    """THE receipt for an answer (design §2.7: it renders STATE, not the action) — the sheet button's ephemeral reply, the
    Me page's toast: what they are now on this run, as which character, and what happens next. On an ad hoc run the
    queue position ('14th of 25 seats — in if nothing changes'); a recurring run with several rosters says which roster
    is decided at lock; after lock rostered / confirmed / in line. Markdown bold; the web strips it. A Join on a day
    they are recorded away carries join_while_away's sentence (the absence stands for the other sheets; I'm back)."""
    text = _answer_state(reg, ev, m)
    s = ev.signups.get(str(m.discord_id))
    if s is not None and s.status == "in" and ev.state not in ("done", "cancelled"):
        note = join_while_away(reg, ev, m)
        if note:
            text += "\n" + note
    return text


def _answer_state(reg: Registry, ev: RaidEvent, m: Member) -> str:
    """answer_receipt without the away note: the state line alone."""
    s = ev.signups.get(str(m.discord_id))
    run = run_words(reg, ev)
    dm = "you'd get a DM" if reachable(m) else "it would wait on your Me page"
    if ev.state in ("done", "cancelled"):
        return f"{run} is {ev.state}."
    if s is None:
        return f"You haven't answered {run} yet."
    as_ = f"{s.character} ({s.spec})"
    if ev.state == "open":
        if s.status == "in":
            ins = ev.by_status("in")
            n = run_rosters(reg, ev)
            seats = n * run_size(reg, ev)
            if run_kind(reg, ev) == "adhoc":
                pos = next((i for i, x in enumerate(ins, 1) if x.discord_id == s.discord_id), len(ins))
                if pos <= seats:
                    return f"You're **joined** as {as_} for {run} — {_ordinal(pos)} of {seats} seats, in if nothing changes (first come, first rostered)."
                return f"You're **joined** as {as_} for {run} — {_ordinal(pos)} for {seats} seats: a seat opens only if someone drops; {dm}."
            return (f"You're **joined** as {as_} for {run} · {len(ins)}/{seats} so far."
                    + (f" Join counts for 1 of {n} rosters — which one is decided at lock." if n > 1 else " The roster is decided at lock."))
        if s.status == "sub":
            return f"You're on the **Bench** as {as_} for {run} — not coming unless needed; the officers ask you if a seat opens."
        away = " (you're marked away that day)" if s.source == "absence" else ""
        return f"You're **No thanks** for {run}{away}."
    seat = ev.seat_of(m)
    if seat:
        i, p = seat
        where = f" · roster {i + 1} of {len(ev.all_rosters)}" if len(ev.all_rosters) > 1 else ""
        ask = next((a for a in reversed(m.placement_asks) if a["roster"] == ev.team), None)
        if not run_confirms(reg, ev):
            then = " — you're in; press Can't make it on the sheet if that changes."
        elif ask and ask.get("answer") == "yes":
            then = " — confirmed."
        elif ask and ask.get("answer") is None:
            by = f" by {reg.local12(ev.confirm_by)}" if ev.confirm_by else ""
            then = f" — confirm{by} ({'your DM or ' if reachable(m) and ask.get('channel', 'dm') == 'dm' else ''}your Me page)."
        else:
            then = "."
        return f"You're **rostered** as {p.character or s.character} ({p.spec}) for {run}{where}{then}"
    if s.status == "in":
        return f"You joined {run} but weren't rostered — you're in line for a freed seat; {dm}."
    if s.status == "sub":
        return f"You're on the **Bench** for {run} — not rostered; the officers ask you if a seat opens."
    return f"You're **No thanks** for {run}."


# the on-behalf union (B5): every verb an officer may record for a member on one run, in the members' own words, raid-night
# first. `key` is what the wizard / the web send back; `needs` = "character" (a pick) | "note" (optional prose) | None.
ACTION_LABELS = {"callout": "Record a callout…", "confirm_yes": "Confirm for them", "confirm_no": "Can't make it for them",
                 "fill_yes": "Confirm their fill", "fill_no": "No to their fill", "join": "Join", "bench": "Bench", "no_thanks": "No thanks",
                 "pin_in": "Pin to roster", "pin_out": "Keep on bench", "unpin": "Unpin"}


def member_actions(reg: Registry, ev: RaidEvent, m: Member) -> list[dict]:
    """What an officer can record for `m` on `ev` right now — ONLY the verbs that apply (the SetWizard's buttons and the
    site's row ⋮ menu read this one list): Confirm / Can't make it for their open Confirm ask · Confirm / No to their
    open fill ask · Record a callout (with an optional note; before lock it is No thanks + the callout) · Join (as
    <alt>) · Bench · No thanks (an answer change: no callout record) · Pin to roster / Keep on bench / Unpin (open
    sheet, on it). Each: {key, label, needs, characters?: [labels]}."""
    if ev.state not in ("open", "locked"):
        return []
    s = ev.signups.get(str(m.discord_id))
    seat = ev.seat_of(m) if ev.state != "open" else None
    chars = [c.label for c in m.active()]
    out: list[dict] = []

    def add(key: str, needs: str | None = None, label: str | None = None, **extra) -> None:
        out.append({"key": key, "label": label or ACTION_LABELS[key], "needs": needs, **extra})

    if ev.state == "locked" and any(a["roster"] == ev.team and a.get("answer") is None for a in m.placement_asks):
        add("confirm_yes")
        add("confirm_no")
    ask = next((a for a in ev.fill_asks if a.discord_id == m.discord_id and a.open), None)
    if ask is not None:
        add("fill_yes", label=f"Confirm their {'swap' if ask.swap else 'fill'} ({ask.character}, {ask.spec})")
        add("fill_no", label=f"No to their {'swap' if ask.swap else 'fill'}")
    if seat or s is None or s.status != "out":  # a callout for someone who told an officer they can't come (answered or not)
        add("callout", "note")
    if ev.state == "open" or not seat:
        if s is None or s.status != "in":
            add("join", "character" if len(chars) > 1 else None, characters=chars)
        elif len(chars) > 1:
            add("join", "character", label="Join as another character", characters=[c for c in chars if c != s.character])
    elif len(chars) > 1:  # rostered: a character swap keeps the seat (set_answer reseats)
        add("join", "character", label="Play another character", characters=[c for c in chars if c != (s.character if s else None)])
    if s is None or s.status != "sub":
        add("bench", "character" if len(chars) > 1 and s is None else None, characters=chars)
    if s is None or s.status != "out":
        add("no_thanks")
    if ev.state == "open" and s is not None:
        pin = ev.pins.get(str(m.discord_id))
        if pin != "in" and s.status == "in":
            add("pin_in")
        if pin != "out":
            add("pin_out")
        if pin:
            add("unpin")
    return out


def member_state_rev(ev: RaidEvent, m: Member) -> str:
    """What the SetWizard re-reads at Confirm: the member's answer, seat, pin and open asks on this run. A change since the
    screen was drawn is shown ('Kestrel is now Bench … — keep going?') before anything is written."""
    s = ev.signups.get(str(m.discord_id))
    open_place = any(a["roster"] == ev.team and a.get("answer") is None for a in m.placement_asks)
    open_fill = any(a.discord_id == m.discord_id and a.open for a in ev.fill_asks)
    return "|".join(str(x) for x in (ev.state, s.status if s else "", s.character if s else "", s.updated_at if s else "", bool(ev.seat_of(m)),
                                       ev.pins.get(str(m.discord_id), ""), open_place, open_fill))


def member_change_words(reg: Registry, ev: RaidEvent, m: Member) -> str:
    """'Kestrel is now Bench as Kestrel (since 5:12 PM)' — the member's answer as it is now, for the re-read screen."""
    s = ev.signups.get(str(m.discord_id))
    if s is None:
        return f"{m.display_name} has no answer on this run now"
    seat = " and rostered" if ev.state != "open" and ev.seat_of(m) else ""
    return f"{m.display_name} is now {LABELS.get(s.status, s.status)} as {s.character}{seat} (since {reg.local12(s.updated_at, '%I:%M %p')})"


def pin_counts(ev: RaidEvent) -> str:
    """The sheet footer's pin line (B5, numbers only — who is officer data): 'Pinned: 2 · Bench-pinned: 1', or ''."""
    on = {str(sg.discord_id) for sg in ev.signups.values()}
    ins = sum(1 for u, v in ev.pins.items() if v == "in" and u in on)
    outs = sum(1 for u, v in ev.pins.items() if v == "out" and u in on)
    return " · ".join(p for p in ((f"Pinned: {ins}" if ins else ""), (f"Bench-pinned: {outs}" if outs else "")) if p)
