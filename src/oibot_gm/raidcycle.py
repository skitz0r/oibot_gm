"""Signup-driven raid cycle: one sheet per raid slot per lockout window (opened on cadence), Join / Bench / No thanks,
health check, lock → roster(s) from the signups (weights + officer pins), confirmations, seat freeing and filling.
No LLM anywhere in this module."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import RANK_ORDER, ROLES
from .models import Player, RosterResult
from .profiles import GameProfile
from .registry import (ADHOC_SCHEDULE, ADHOC_WEIGHTS, DEFAULT_RAID_SIZE, FOLLOW_SCHEDULE, LINEUP_MAX, RAID_DEFAULTS, RAID_WEIGHT_DEFAULTS, Member, Registry,
                       RegisteredCharacter, RegistryError, can_cover, join_words, now, schedule_class, schedule_def_of, slot_words)
from .roster import explain, solver
from .store import GitStore

log = logging.getLogger(__name__)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
STATUSES = ("in", "sub", "out")  # Join / Bench / No thanks (legacy "tentative" is read as "in")
LABELS = {"in": "Join", "sub": "Bench", "out": "No thanks"}
# ---- the run's vocabulary, defined ONCE (design §5.56 phase 2): the Discord cards and DMs read these, the site gets
# them through /api/meta (web/api.py meta_json) and never keeps a copy of its own
# a run's state in words — the card's badge, History, the fill state, the Me page, the closed Discord cards
RUN_STATE_WORDS = {"open": "open for signup", "locked": "locked — the roster stands", "done": "finished", "cancelled": "cancelled"}
# the one sentence for a run whose rostered aren't asked to confirm (ad hoc by default, D5) — every surface
NO_CONFIRMATIONS = "nobody is asked to confirm"
# how an ad hoc run seats people (D4)
FIRST_COME = "first come, first rostered"
# the board's list of joiners who hold no seat: before the lock nobody is placed yet, after it they aren't rostered
BANK_WORDS = {"open": "Not placed", "locked": "Not rostered"}
# who the bot waits on before the lock (rc.expected_from) — the officers' box on the site and the health card
NOT_ANSWERED_WORDS = {"regulars": "Regulars who haven't answered", "everyone": "Haven't answered — no regulars set, everyone counts",
                      "test": "Test bench, not answered"}
# an answer to a fill / pre-lock ask (FillAsk.answer; None = still out)
ASK_ANSWER_WORDS = {"yes": "said yes", "no": "said no", "expired": "no answer in time", "released": "closed", None: "waiting"}
# an officer's pin on a joiner (Signup.pin; ACTION_LABELS pin_in / pin_out): the board's pin marks' tooltips and legend
PIN_WORDS = {"in": "pinned: the lock keeps them where you put them", "out": "kept off: the lock leaves them off the roster"}
# the joiners the full roster(s) left out (rc.leftovers) — the board's and the lock card's heading
LEFTOVERS = "Leftovers"
# a machine code, never shown: a run that sends no reminders (`reminders`), an ad hoc run that expects nobody (`expected_from`)
OFF = "none"
# follow-on runs (design §5.62): a second raid the same night from the lead's signup
LEAVING_NOTE = "leaving after the first raid"  # the follow-on's out row for someone who stays for the lead only
FOLLOW_ANSWER_REFUSAL = "Answer on the {lead} sheet. It covers the whole night."
LEAD_SEAT_BONUS = 1000  # a follow-on's solve: the lead's rostered beat every seat weight combined, so the splits are the lead's roster split
# a later raid of a run (part ≥ 1) is never locked by itself while an earlier raid is open: the first raid's lock builds them all
LATER_PART_LOCK = "Lock {first} first. Its lock builds every raid of the run."
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
    why: Optional[str] = None  # the code-written reason officers see (role_plan): "short 1 healer · Ana can also play Holy · DPS stays 6 of 5 minimum"
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

    # an opaque id: a new run's is '<raid abbr>-<6 random chars>' (new_run_key), the same string as its run dict's key; an
    # older run keeps the key it was opened with ('bd-1209-1930-2026-12-09'), never read for a raid, a time or a schedule
    key: str
    team: str  # its run dict's key (config.rosters): a new run's own key; an older run's key without its date
    instance: Optional[str] = None
    schedule: Optional[str] = None  # the schedule id it was opened for (None: a one-off; an older file: its old stamp, see run_schedule_id)
    # the identity stamps (phase 3; None / 0 on a file from before until stamp_runs adds them at the start): the start the
    # schedule gave it in GUILD WALL TIME 'YYYY-MM-DDTHH:MM' (a timezone change never makes a second run of one occurrence),
    # the first part's key, which part of the event this run is, and its start in minutes after the first part's
    occurrence: Optional[str] = None
    # the occurrence of its schedule this run ALSO stands for, decided once when the schedule's run time was edited while
    # the run was live (plan_stands_for): Tue's run with the night moved to Wed stands for that week's Wed. None = only
    # its own occurrence. Older code ignores the key (extra="ignore")
    stands_for: Optional[str] = None
    event: Optional[str] = None
    part: int = 0
    starts: int = 0
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
    # before the lock, who placed a name by hand (a drag on the Events board): signup name → the officer's Discord id. A name
    # in the layout, or pinned out by a drag to Not placed; pruned at every board write, cleared at the lock. "Undo my
    # moves" drops exactly the viewer's names (RaidMixin.undo_moves)
    placed_by: dict[str, int] = Field(default_factory=dict)
    # before the lock, the pins an officer's DRAG changed (a drop on Not placed pins "out"; a place lifts an "out" pin):
    # member id → {by: the officer, was: the pin before (None = none), now: the pin the drag left}. Undo my moves puts
    # `was` back only while the pin is still `now` — a pin set anywhere else is never touched. Cleared at the lock
    drag_pins: dict[str, dict] = Field(default_factory=dict)
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
    # a later raid of a run (design §5.62; up to LINEUP_MAX raids from one signup): `follows` = the FIRST part's key (on
    # every later part), `followed_by` = the next part's key (on every part but the last). A later part's signups are
    # materialised from the first part's (rc.sync_parts) and never answered directly; `leaving` = the discord ids out of
    # that part alone
    follows: Optional[str] = None
    followed_by: Optional[str] = None
    leaving: list[int] = Field(default_factory=list)
    # a later raid taken out of its run's lineup before the lock (apply_lineup): cancelled, kept on file with its stamps
    # for the record, and no longer a part of the run (event_parts leaves it out, so the part numbers stay unique)
    dropped: bool = False
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
            "reminders": "dm" if rd.get("nudge", RAID_DEFAULTS["nudge"]) else OFF,
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


def after_minutes(start: datetime, minutes: float) -> datetime:
    """THE start of a later raid: `minutes` of real time after `start`, counted in UTC and given back in `start`'s own
    zone. Adding a timedelta to a zoneinfo datetime is wall-clock arithmetic (an hour off on a clocks-change night),
    while a run file's start is an absolute instant — every part start (ensure_run, complete_parts, the previews, the
    schedule's C8 check, the C9 sentence) goes through here, so they all agree."""
    from datetime import timezone

    return (start.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(start.tzinfo)


def follow_start(reg: Registry, instance: str, start: datetime) -> datetime:
    """When a follow-on starts: the lead's start plus the lead raid's length (design §5.62)."""
    return after_minutes(start, 60 * float(reg.raid_def(instance).get("duration_hours") or RAID_DEFAULTS["duration_hours"]))


def is_follow(ev: "RaidEvent") -> bool:
    """A later raid of a run (part ≥ 1): no sheet, its answers the first raid's."""
    return ev.part > 0 or bool(ev.follows)


def part_no(ev: "RaidEvent") -> int:
    """Which raid of its run `ev` is (0 = the first): its stamp; a follow-on of older code not stamped yet is part 1."""
    return ev.part if ev.event else (1 if ev.follows else 0)


def event_id(ev: "RaidEvent") -> str:
    """The run a part belongs to: its `event` stamp (the first part's key), else — a file from before the stamps — the
    first part's key it `follows`, else its own key."""
    return ev.event or ev.follows or ev.key


def same_event(a: "RaidEvent", b: "RaidEvent") -> bool:
    """Two parts of one run (a run is the same event as itself)."""
    return event_id(a) == event_id(b)


def event_parts(rs: "RaidStore", ev: "RaidEvent") -> list["RaidEvent"]:
    """Every part of `ev`'s run, any state, in order (part 0 first). A part has exactly one `event`, so it belongs to
    exactly one run; and every later part's dict is made by its first part's open (ensure_run), so one first part
    holds one chain. A raid taken out of the lineup (`dropped`, apply_lineup) is no longer a part."""
    if ev.dropped:  # taken out of its run: it answers as itself (cancelled), never as the live run
        return [ev]
    eid = event_id(ev)
    parts = {e.key: e for e in rs.events.values() if event_id(e) == eid and not e.dropped}
    parts.setdefault(ev.key, ev)
    return sorted(parts.values(), key=lambda e: (part_no(e), e.starts_at, e.key))


def first_part(rs: "RaidStore", ev: "RaidEvent") -> "RaidEvent":
    """The first raid of `ev`'s run (the one with the sheet, the answers and the timing); `ev` itself when it is, and
    when `ev` was taken out of its run's lineup (`dropped`: a cancel, a move or an edit called on it answers as that
    cancelled raid, never as the live run)."""
    if not is_follow(ev) or ev.dropped:
        return ev
    first = rs.events.get(ev.follows) if ev.follows else None
    if first is None:
        first = next((p for p in event_parts(rs, ev) if not is_follow(p)), None)
    return first or ev


def later_parts(rs: "RaidStore", ev: "RaidEvent") -> list["RaidEvent"]:
    """The raids after the first of `ev`'s run, in order, any state ([] for a run of one raid, and for a raid taken out
    of its lineup: it is a run of its own now)."""
    if ev.dropped:
        return []
    return [p for p in event_parts(rs, ev) if is_follow(p)]


def held_later(rs: "RaidStore", ev: "RaidEvent", who) -> list["RaidEvent"]:
    """The LOCKED later raids of a first part's run on which `who` (a member or discord id) holds a seat — what their
    Can't make it on the first raid frees too (free_seat's cascade); [] for a later part."""
    if is_follow(ev) or who is None:
        return []
    return [p for p in later_parts(rs, ev) if p.state == "locked" and p.seat_of(who)]


def lead_of(rs: "RaidStore", ev: "RaidEvent") -> "RaidEvent | None":
    """The first part of a later part's run (its answers are the first part's), else None."""
    if not is_follow(ev):
        return None
    first = first_part(rs, ev)
    return first if first is not ev else None


def follow_of(rs: "RaidStore", ev: "RaidEvent") -> "RaidEvent | None":
    """The next part of a run (the part after `ev`), else None."""
    return rs.events.get(ev.followed_by) if ev.followed_by else None


def lead_dict(reg: Registry, ev: "RaidEvent") -> dict | None:
    """The lead's run dict for a follow-on (no store needed: the sheet, the cards and the confirms read it). The
    events carry run (event) keys; the dicts are found through the run's own dict, whose `follows` is a roster key."""
    t = reg.config.team(ev.team) if ev.follows else None
    return reg.config.team(t["follows"]) if t and t.get("follows") else None


def follow_dict(reg: Registry, ev: "RaidEvent") -> dict | None:
    """The follow-on's run dict for a lead, else None."""
    t = reg.config.team(ev.team) if ev.followed_by else None
    return reg.config.team(t["followed_by"]) if t and t.get("followed_by") else None


def later_dicts(reg: Registry, ev: "RaidEvent") -> list[dict]:
    """The run dicts of the raids after the first of a FIRST part's run, in order (each dict's `followed_by` names the
    next); [] for a later part or a run of one raid. No store needed: the sheet, the cards and the confirms read it."""
    if is_follow(ev):
        return []
    out, seen = [], {ev.team}
    t = reg.config.team(ev.team)
    while t and t.get("followed_by") and t["followed_by"] not in seen and len(out) < LINEUP_MAX - 1:
        seen.add(t["followed_by"])
        t = reg.config.team(t["followed_by"])
        if t is not None:
            out.append(t)
    return out


def dict_start(reg: Registry, first: "RaidEvent", t: dict) -> datetime:
    """When a later part's dict starts: the first part's start plus its `starts` minutes (an older dict without them:
    the first raid's length after it)."""
    return after_minutes(first.start, int(t["starts"])) if t.get("starts") else follow_start(reg, first.instance, first.start)


def parts_short(reg: Registry, ev: "RaidEvent") -> list[tuple[str, dict[str, int]]]:
    """(raid name, {role: short}) for each later raid of a first part whose tank and healer minimums × its rosters the
    first part's Join answers can't cover — the health card's later-raid lines; [] when none or all covered."""
    ins = ev.by_status("in")
    have = {r: sum(1 for s in ins if s.role == r) for r in ("tank", "healer")}
    out = []
    for t in later_dicts(reg, ev):
        size, n = int(t.get("size") or DEFAULT_RAID_SIZE), int(t.get("rosters") or 1)
        rb = reg.role_bounds(t.get("instance"), size)
        short = {r: int(rb[r]["min"]) * n - have[r] for r in ("tank", "healer") if int(rb[r]["min"]) * n > have[r]}
        if short:
            out.append((reg.raid_def(t.get("instance")).get("name", t.get("instance")), short))
    return out


def follow_words_of(reg: Registry, ev: "RaidEvent") -> str:
    """'Barrow Deeps ×2, then Onyxia's Lair' — the raids after a first part, in order ('' when none or a later part)."""
    return reg.later_words([{"raid": t.get("instance"), "rosters": t.get("rosters") or 1} for t in later_dicts(reg, ev)])


def _raid_name(reg: Registry, raid: str | None) -> str:
    return reg.raid_def(raid).get("name", raid) if raid in reg.profile.raids else str(raid or "")


def lineup_words(reg: Registry, lineup: list[dict]) -> str:
    """'Hyjal Summit, then Barrow Deeps × 2' — a whole lineup ([{raid, rosters}], its first raid first) as the Events
    and Schedules pages print it."""
    def one(e: dict) -> str:
        n = int(e.get("rosters") or 1)
        return _raid_name(reg, e.get("raid")) + (f" × {n}" if n > 1 else "")
    return ", then ".join(one(e) for e in lineup or [] if e)


def lineup_roster_words(reg: Registry, lineup: list[dict]) -> str:
    """'Hyjal Summit and both Barrow Deeps rosters' — every roster of a lineup as a sentence names them (the lock box):
    one roster = the raid's name, two = 'both {Raid} rosters', three or four = 'all {n} {Raid} rosters'."""
    def one(e: dict) -> str:
        n, name = int(e.get("rosters") or 1), _raid_name(reg, e.get("raid"))
        return name if n == 1 else f"both {name} rosters" if n == 2 else f"all {n} {name} rosters"
    items = [one(e) for e in lineup or [] if e]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def event_lineup(reg: Registry, ev: "RaidEvent") -> list[dict]:
    """The lineup of `ev`'s event ([{raid, rosters}], its first raid first), from the run dicts alone: part 0's dict
    `lineup`, else its raid and rosters followed by its later dicts (any part stands for its event)."""
    t0 = reg.config.team(ev.team)
    seen = {ev.team}
    while t0 is not None and t0.get("follows") and t0["follows"] not in seen:  # a later part's dict: up to part 0's
        seen.add(t0["follows"])
        up = reg.config.team(t0["follows"])
        if up is None:
            break
        t0 = up
    if t0 is None:
        return [{"raid": ev.instance, "rosters": 1}]
    if t0.get("lineup"):
        return [{"raid": e["raid"], "rosters": int(e.get("rosters") or 1)} for e in t0["lineup"]]
    first = RaidEvent(key=t0["key"], team=t0["key"], instance=t0.get("instance") or ev.instance, starts_at=ev.starts_at)
    return [{"raid": first.instance, "rosters": int(t0.get("rosters") or 1)}] + [
        {"raid": t.get("instance"), "rosters": int(t.get("rosters") or 1)} for t in later_dicts(reg, first)]


def priority_words(reg: Registry, ev_or_schedule_def) -> str:
    """Who gets a seat first when more join than there are seats (D17), short: 'First to join' for a one-off, else the
    two heaviest weights — 'Regulars first, then rank' (constants.PRIORITY_SHORT)."""
    from .constants import PAGE_WORDS, PRIORITY_SHORT
    from .registry import schedule_class

    x = ev_or_schedule_def
    first = PAGE_WORDS["first_to_join"]
    if isinstance(x, RaidEvent):
        if run_kind(reg, x) == "adhoc":
            return first
        weights = run_weights(reg, x)
    else:
        if schedule_class((x or {}).get("kind")) == "adhoc":
            return first
        weights = dict((x or {}).get("weights") or {})
    order = list(PRIORITY_SHORT)
    top = sorted((k for k in order if float(weights.get(k) or 0) > 0), key=lambda k: (-float(weights.get(k) or 0), order.index(k)))[:2]
    if not top:
        return first
    a = PRIORITY_SHORT[top[0]]
    a = a[:1].upper() + a[1:]
    if len(top) == 1:
        return f"{a} first"
    b = PRIORITY_SHORT[top[1]]
    return f"{a} first, then {b[:1].lower() + b[1:]}"


def confirmation_words(confirm: bool) -> str:
    """Whether the rostered are asked to confirm, as the Details label's value reads (constants.PAGE_WORDS)."""
    from .constants import PAGE_WORDS

    return PAGE_WORDS["asked_at_lock"] if confirm else PAGE_WORDS["not_asked"]


def lead_name_of(reg: Registry, ev: "RaidEvent") -> str:
    """'Hyjal Summit' for a follow-on's lead ('' when the run is not a follow-on)."""
    lt = lead_dict(reg, ev)
    return reg.raid_def(lt.get("instance")).get("name", lt.get("instance")) if lt else ""


def abbr(instance: str) -> str:
    return "".join(w[0] for w in instance.split("_"))[:4]


# A rehearsal of a raid with no schedule named: its run dict's schedule_id (a virtual weekly schedule on the game's
# suggested timing, never in a key; the registry refuses it as a real schedule id).
_REHEARSAL = "test"
# a new run key: the raid's abbreviation and KEY_CHARS of these, drawn with secrets.choice (no 0, 1, i, l, o, r)
KEY_ALPHABET = "23456789abcdefghjkmnpqstuvwxyz"
KEY_CHARS = 6


def new_run_key(reg: Registry, rs: "RaidStore | None", instance: str) -> str:
    """A key no run dict (config.rosters) and no run file holds: f"{abbr(instance)}-{6 chars}". It carries no time and
    no schedule, and nothing ever reads one out of it. `rs` None: the run files on disk are checked instead."""
    import secrets

    taken = {t.get("key") for t in reg.config.rosters if isinstance(t, dict)}
    raids = reg.store.root / reg.key / "raids"
    while True:
        key = f"{abbr(instance)}-{''.join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_CHARS))}"
        if key in taken or (key in rs.events if rs is not None else (raids / f"{key}.json").exists()):
            continue
        return key


def occurrence_stamp(reg: Registry, start: datetime) -> str:
    """The occurrence a schedule's run is found by: its start in guild WALL time, 'YYYY-MM-DDTHH:MM' (naive)."""
    return start.astimezone(reg.tz).strftime("%Y-%m-%dT%H:%M")


def schedule_stamp(sid: str | None) -> str | None:
    """The schedule id a run carries: a real schedule's id; None for a one-off, a rehearsal or a follow-on (the reserved
    ids old run dicts carry: adhoc, test, after, default)."""
    from .registry import RESERVED_SCHEDULE_IDS

    return None if not sid or sid in RESERVED_SCHEDULE_IDS else sid


def _first_part(ev: "RaidEvent") -> bool:
    """A run's first raid (the one an event list lists and a schedule night is found by): not a later part."""
    return not is_follow(ev)


def _newest(evs: list) -> "RaidEvent | None":
    return max(evs, key=lambda e: e.created_at) if evs else None


def event_of_occurrence(rs: "RaidStore", sid: str, occurrence: str, *, reg: Registry | None = None) -> "RaidEvent | None":
    """THE finder of a schedule's run: the first part (never a test run) of schedule `sid` stamped with `occurrence`, or
    that stands for it (stands_for_of: decided when the schedule's run time was edited while the run was live —
    plan_stands_for), in any state (the newest created if several). With `reg`, also a run file from before the stamps
    (no `event`) whose start and schedule (run_schedule_id: its run dict, else its old stamp through the aliases) give
    the same pair: a finished or cancelled run the start-up stamping never touches, or a live one opened before it; and
    a LIVE run of the schedule moved to exactly that start (reschedule), so nothing else opens there. Nothing is
    guessed: a run stands for what its stamps say."""
    if reg is not None:
        return occurrence_index(reg, rs)(sid, occurrence)
    hits = [e for e in rs.events.values() if e.event and _first_part(e) and not e.test and e.schedule == sid
            and (e.occurrence == occurrence or stands_for_of(reg, e) == occurrence)]
    return _newest(hits)


def occurrence_index(reg: Registry, rs: "RaidStore"):
    """event_of_occurrence (with `reg`) for many nights at once: ONE pass over the run store, then find(sid, occurrence)
    → the run or None, by the same three tiers — a stamped first part (never a test run) by its occurrence or the night
    it stands for, plus a run file from before the stamps by its start and schedule; only when neither gives one, a LIVE
    stamped first part of the schedule moved onto that minute. The newest created when several. The Events list builds
    one per request; event_of_occurrence answers through it."""
    from collections import defaultdict

    found: dict[tuple, list] = defaultdict(list)
    moved: dict[tuple, list] = defaultdict(list)
    for e in rs.events.values():
        if not _first_part(e):
            continue
        if e.event:
            if e.test:
                continue
            found[(e.schedule, e.occurrence)].append(e)
            sf = stands_for_of(reg, e)
            if sf and sf != e.occurrence:
                found[(e.schedule, sf)].append(e)
            if e.state not in ("done", "cancelled"):
                moved[(e.schedule, occurrence_stamp(reg, e.start))].append(e)
        elif not is_test_run(reg, e):
            found[(run_schedule_id(reg, e), occurrence_stamp(reg, e.start))].append(e)

    def find(sid: str, occurrence: str) -> "RaidEvent | None":
        return _newest(found.get((sid, occurrence)) or moved.get((sid, occurrence)) or [])
    return find


def stands_for_of(reg: Registry | None, ev: "RaidEvent") -> str | None:
    """The occurrence `ev` also stands for: its run dict's `stands_for` while the dict exists (the schedule write sets it
    there, in the same commit), else the run file's (copied over by sync_stands_for before the dict goes). A CANCELLED
    run stands for nothing more, whenever it was cancelled (a done run keeps it: that night was played)."""
    if ev.state == "cancelled":  # a cancelled run holds its own night only: the night it stood for opens again
        return None
    t = reg.config.team(ev.team) if reg is not None else None
    if t is not None and "stands_for" in t:
        return t["stands_for"]
    return ev.stands_for


def sync_stands_for(reg: Registry, rs: "RaidStore", ev: "RaidEvent") -> bool:
    """Copy the run dict's `stands_for` onto the run file (cleanup_ephemeral, before a closed run's dict goes; the
    scheduler's minute for a live run). → whether it changed."""
    t = reg.config.team(ev.team)
    if t is None or "stands_for" not in t or t["stands_for"] == ev.stands_for:
        return False
    ev.stands_for = t["stands_for"]
    rs.save(ev, f"stands for {ev.stands_for}" if ev.stands_for else "stands for its own night only")
    return True


def _run_files(reg: Registry) -> list[dict]:
    """Every run file of the guild as stored (the few keys plan_stands_for reads): read from disk, so it needs no run store."""
    out = []
    for f in sorted((reg.store.root / reg.key / "raids").glob("*.json")):
        try:
            raw = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        out.append({k: raw.get(k) for k in ("key", "team", "state", "schedule", "occurrence", "stands_for", "event", "part",
                                            "follows", "test", "created_at")})
    return out


def _anchor_window(reg: Registry, instance: str, at: datetime) -> tuple[datetime, datetime]:
    """The period a weekly run time means one run in: the raid's lockout window containing `at` (one sheet per run time
    per lockout window) when the raid has a first opening, else the Monday-to-Sunday guild week."""
    if reg.first_open(instance) is not None:
        return reg.lockout_window(instance, at)
    start = datetime.combine(at.astimezone(reg.tz).date() - timedelta(days=at.astimezone(reg.tz).weekday()),
                             datetime.min.time(), tzinfo=reg.tz)
    return start, start + timedelta(days=7)


def _run_times(sched: dict) -> tuple[str, list]:
    """(kind, its run times): weekly 'Tue 19:30' slots, days of each lockout as (day, 'HH:MM'); a pickup has none."""
    kind = sched.get("kind") or "weekly"
    if kind == "lockout":
        return kind, [(int(d), sched.get("time")) for d in sched.get("days") or []]
    if kind == "weekly":
        return kind, list(sched.get("slots") or [])
    return kind, []


def run_times_changed(old: dict, new: dict | None) -> bool:
    """Did a schedule write change when the schedule runs (its kind or run times)? A timing-only edit didn't."""
    return new is not None and _run_times(old) != _run_times(new)


def _slot_pairs(old: dict, new: dict, window: tuple[datetime, datetime]) -> dict:
    """The run times an edit took away, each paired with one it added in its place: {old slot: new slot | None}. Both
    lists sorted by time — weekly run times by how far they fall after the start of `window` (the run's anchor window),
    days of each lockout by (day, time) — then paired in that order, whatever order they were typed in. A change of kind
    pairs nothing."""
    ka, a = _run_times(old)
    kb, b = _run_times(new)
    if ka != kb:
        return {}
    gone, added = [x for x in a if x not in b], [x for x in b if x not in a]
    if ka == "weekly":
        ws = window[0].astimezone(window[0].tzinfo)
        base = ws.weekday() * 1440 + ws.hour * 60 + ws.minute

        def key(slot):
            wd, h, m = parse_schedule(slot)
            return (wd * 1440 + h * 60 + m - base) % (7 * 1440)
    else:
        def key(slot):
            return slot
    gone, added = sorted(gone, key=key), sorted(added, key=key)
    return {x: (added[i] if i < len(added) else None) for i, x in enumerate(gone)}


def _lockout_day(reg: Registry, instance: str, at: datetime) -> tuple[int, object]:
    """(day N of its lockout window, the window's first calendar day) for a guild wall time, as _lockout_starts counts."""
    fo = reg.first_open(instance)
    days = int(reg.raid_def(instance).get("lockout_days") or 7)
    d0 = fo.astimezone(reg.tz).date() if fo else at.date()
    k = (at.date() - d0).days // days
    w0 = d0 + timedelta(days=k * days)
    return (at.date() - w0).days + 1, w0


def _given(reg: Registry, instance: str, sched: dict, stamp: str) -> bool:
    """Does `sched` give the occurrence `stamp` (its day's starts)?"""
    from datetime import time as _time

    day = datetime.combine(datetime.fromisoformat(stamp).date(), _time(0, 0), tzinfo=reg.tz)
    return stamp in {occurrence_stamp(reg, t) for t in schedule_starts(reg, instance, sched, day, 25)}


def _replacement(reg: Registry, instance: str, sched: dict, slot, original: str) -> str | None:
    """The occurrence `slot` of the edited schedule gives in the run's own period: weekly, the anchor window of its
    ORIGINAL occurrence (_anchor_window: the raid's lockout window, else the Monday-to-Sunday week); days of each
    lockout, that occurrence's lockout window. None when the schedule doesn't give one there."""
    from .wizard import resolve_local

    at = datetime.fromisoformat(original).replace(tzinfo=reg.tz)
    if isinstance(slot, tuple):
        n, hhmm = slot
        _day, w0 = _lockout_day(reg, instance, at)
        date = w0 + timedelta(days=int(n) - 1)
        h, m = (int(x) for x in str(hhmm).split(":"))
        t, _note = resolve_local(datetime(date.year, date.month, date.day, h, m), reg.tz)
        stamp = occurrence_stamp(reg, t)
        return stamp if _given(reg, instance, sched, stamp) else None
    ws, we = _anchor_window(reg, instance, at)
    hits = [t for t in schedule_starts(reg, instance, sched, ws, (we - ws).total_seconds() / 3600)
            if ws <= t < we and t.astimezone(reg.tz).strftime("%a %H:%M") == slot]
    return occurrence_stamp(reg, hits[0]) if hits else None


def plan_stands_for(reg: Registry, instance: str, before: dict[str, dict], after: dict[str, dict]) -> dict[str, str | None]:
    """THE decision of what a live run stands for after its schedule's run times were edited — made once, at the
    schedule write, BEFORE the new rows are installed (`before` / `after`: sid → row), when the old and the new run
    times are both known; never guessed at read time. Nothing is read unless a schedule's kind or run times changed.
    For each LIVE (open or locked; never cancelled, done or a test run) first raid of an edited schedule whose current
    night — its `stands_for` when it has one, else its own `occurrence` — the new run times no longer give: the run time
    that night came from is paired with the one the edit added in its place (_slot_pairs: both sorted by time from the
    run's anchor window), and the run stands for that run time's occurrence in its ORIGINAL occurrence's anchor window
    (weekly: the raid's lockout window, else the Monday-to-Sunday week) or lockout window (days of each lockout) —
    when the schedule gives it and no other run holds it (by occurrence, or by stands_for unless cancelled). No added
    run time, or a held one: it stands for nothing more. An edit that gives its own night again clears it. → {run dict key:
    stands_for | None} for the dicts that change; the caller writes them (Registry._write_schedules)."""
    edited = {sid: (old, after[sid]) for sid, old in before.items() if run_times_changed(old, after.get(sid))}
    if not edited:
        return {}
    files = _run_files(reg)
    out: dict[str, str | None] = {}
    for sid, (old, new) in edited.items():
        mine = [f for f in files if f.get("schedule") == sid and not f.get("follows") and not f.get("part") and not f.get("test")]
        dicts = [t for t in reg.config.rosters if isinstance(t, dict) and t.get("occurrence") and not t.get("part") and not t.get("follows")
                 and not t.get("test") and t.get("schedule_id") == sid]
        state = {}
        for f in mine:
            state.setdefault(f.get("team") or f.get("key"), f.get("state"))
            state.setdefault(f.get("key"), f.get("state"))
        held = {f.get("occurrence") for f in mine if f.get("occurrence")}
        held |= {f.get("stands_for") for f in mine if f.get("stands_for") and f.get("state") != "cancelled"}
        held |= {t["stands_for"] for t in dicts if t.get("stands_for") and state.get(t["key"]) != "cancelled"}
        for t in dicts:
            files_of = [f for f in mine if f.get("team") == t["key"] or f.get("key") == t["key"]]
            f = max(files_of, key=lambda x: x.get("created_at") or "") if files_of else None
            if f is None or f.get("state") not in ("open", "locked"):
                continue
            own, was = t["occurrence"], t.get("stands_for")
            if was and _given(reg, instance, new, own):  # its own night is given again: it answers for that one alone
                out[t["key"]] = None
                held.discard(was)
                continue
            cur = was or own
            if _given(reg, instance, new, cur):
                continue
            at = datetime.fromisoformat(cur).replace(tzinfo=reg.tz)
            lockout = (old.get("kind") or "weekly") == "lockout"
            slot = (_lockout_day(reg, instance, at)[0], at.strftime("%H:%M")) if lockout else at.strftime("%a %H:%M")
            window = _anchor_window(reg, instance, datetime.fromisoformat(own).replace(tzinfo=reg.tz))
            to = _slot_pairs(old, new, window).get(slot)
            rep = _replacement(reg, instance, new, to, own) if to is not None else None
            if rep == own or (rep is not None and rep in held - {own, was}):
                rep = None
            if rep != was:
                out[t["key"]] = rep
                held.discard(was)
                if rep:
                    held.add(rep)
    return out


def live_at(rs: "RaidStore", instance: str, start: datetime, *, test: bool = False) -> "RaidEvent | None":
    """A live first part whose raid is `instance` at exactly `start` and whose test flag is `test`: the finder of a
    one-off and of a rehearsal (an open at the same minute is the same run)."""
    return _newest([e for e in rs.live() if _first_part(e) and e.instance == instance and e.start == start and bool(e.test) == test])


def find_run(reg: Registry, rs: "RaidStore", instance: str, start: datetime, schedule: str | None = None, *,
             test: bool = False) -> "RaidEvent | None":
    """The run an open of `instance` at `start` meets: a schedule's run by its occurrence (in any state: open_run refuses
    a closed one), else a live one-off or rehearsal at that minute. `schedule` None: the schedule with a run at exactly
    `start`, else a one-off (open_schedule)."""
    if test:
        return live_at(rs, instance, start, test=True)
    sid = schedule_stamp(schedule or open_schedule(reg, instance, start, None))
    if sid:
        return event_of_occurrence(rs, sid, occurrence_stamp(reg, start), reg=reg)
    return live_at(rs, instance, start)


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
    if gone_raids(reg, instance, sched):  # a raid of its lineup left the game's profile: the schedule opens nothing
        return []
    rd = rd if rd is not None else reg.raid_def(instance)
    if sched.get("kind") == "lockout":
        starts = _lockout_starts(reg, rd, sched, now, horizon_hours)
    elif sched.get("kind") == "pickup":
        return []
    else:
        starts = [t for _s, t in _weekly_starts(reg, rd, sched.get("slots") or [], now, horizon_hours)]
    return _after_first_opens(reg, instance, sched, starts)


def schedule_lineup(instance: str, sched: dict) -> list[dict]:
    """A schedule's whole lineup: its first raid with its rosters, then its follow-on and the raids under `then`."""
    from .registry import row_lineup

    return row_lineup(instance, sched)


def gone_raids(reg: Registry, instance: str, sched: dict) -> list[str]:
    """The raids of a schedule's lineup that are no longer the game profile's (in lineup order). A schedule with any
    opens no run at all — never a chain with a raid the profile lacks (the scheduler warns the officers once an hour)."""
    try:
        lineup = schedule_lineup(instance, sched)
    except RegistryError:
        lineup = [{"raid": instance}]
    return [e["raid"] for e in lineup if e.get("raid") not in reg.profile.raids]


def gone_name(reg: Registry, rid: str) -> str:
    """A raid's name even after it left the game's profile: the base profile's or the guild override's, else its id in
    words."""
    return (getattr(reg, "base_profile", reg.profile).raids.get(rid) or reg.config.raids.get(rid) or {}).get("name") or str(rid).replace("_", " ").title()


def gone_sentence(reg: Registry, gone: list[str], tail: str = "") -> str:
    """'Barrow Deeps is no longer a raid of this game.' — the raids gone_raids found (gone_name), with `tail` before the
    period (', so this schedule opens no events'): the ops warning and the Schedules page say it."""
    names = [gone_name(reg, r) for r in gone]
    words = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{words} {'is' if len(names) == 1 else 'are'} no longer a raid of this game{tail}."


def _after_first_opens(reg: Registry, instance: str, sched: dict, starts: list[datetime]) -> list[datetime]:
    """C8: a start is dropped when a later raid of the schedule's lineup would begin before that raid first opens."""
    lineup = schedule_lineup(instance, sched)
    if len(lineup) < 2:
        return starts
    try:
        offsets = reg.lineup_offsets(lineup)
    except RegistryError:
        return starts
    opens = [(reg.first_open(e["raid"]), at) for e, at in zip(lineup[1:], offsets[1:])]
    return [t for t in starts if not any(fo is not None and fo > after_minutes(t, at) for fo, at in opens)]


def occurrences(reg: Registry, now: datetime, horizon_hours: float | None = None, *, lead: bool = False) -> list[tuple[dict, datetime]]:
    """(schedule, start) for every ACTIVE non-pickup schedule of the guild (an all_schedules row: its first raid is
    `raid`), soonest first. `lead=True`: each schedule looks only as far ahead as its own signup lead (what the scheduler
    opens now); else `horizon_hours` for all."""
    out = []
    for s in reg.all_schedules():
        if not s.get("active", True) or s["kind"] == "pickup" or gone_raids(reg, s["raid"], s):
            continue
        h = float(reg.schedule_def(s["raid"], s["id"])["signup_lead_hours"]) if lead else float(horizon_hours or 0)
        out += [(s, t) for t in schedule_starts(reg, s["raid"], s, now, h)]
    return sorted(out, key=lambda x: (x[1], x[0]["id"]))


def upcoming(reg: Registry, instance: str, now: datetime, horizon_hours: float | None = None, *, lead: bool = False) -> list[tuple[dict, datetime]]:
    """occurrences of the schedules whose first raid is `instance`."""
    return [(s, t) for s, t in occurrences(reg, now, horizon_hours, lead=lead) if s["raid"] == instance]


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


def sheet_times(start: datetime, sd: dict) -> tuple[datetime, datetime]:
    """(when the sheet opens, when it locks) for a night of a schedule starting at `start` — `sd` = its schedule_def
    (signup_lead_hours, lock_hours_before). The Schedules page's Next events and a coming-up event read it."""
    return start - timedelta(hours=float(sd["signup_lead_hours"])), start - timedelta(hours=float(sd["lock_hours_before"]))


FORECAST_DAYS = 28  # the regulars forecast looks this far ahead (from today, or from the raid's first opening before then)
FORECAST_MAX = 8  # and never lists more starts than this


def series_forecast(reg: Registry, key: str, now: datetime | None = None, *, if_resumed: bool = False) -> list[dict]:
    """A recurring series' 4-week forecast: each upcoming start in the next FORECAST_DAYS (counted from the raid's
    first opening while it hasn't opened) with what its regulars minus recorded absences leave short of the tank /
    healer minimums. [{start (ISO), day ('Tue 15 Dec'), when (12-hour), expected, seats, short {role: n}, away
    [names]}]. Empty while the series has no regulars or is paused. Computed in code; officers see a hole a week
    ahead instead of at reminder time. `if_resumed`: a paused series' forecast as if it ran again (the Schedules page's
    paused schedule, beside "Next event if resumed") — the same computation, the pause alone not counted."""
    regs = reg.regulars(key)
    try:
        entry = reg.schedule_entry(key)
    except RegistryError:
        return []
    rid, sid = entry["raid"], entry["id"]
    if not regs or rid not in reg.profile.raids:
        return []
    sched = next((x for x in reg.schedules(rid) if x["id"] == sid), None)
    if sched is None or (not sched.get("active", True) and not if_resumed) or schedule_class(sched["kind"]) != "recurring":
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
    attached — what the Schedules page, the analytics card, /roster overview, readiness (MCP, /ask) all show."""
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


def ensure_run(reg: Registry, instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None, schedule: str | None = None,
               follow: dict | None = None, *, occurrence: str | None = None, rs: "RaidStore | None" = None,
               lineup: list[dict] | None = None, lock_hours: float | None = None, first_rosters: int | None = None) -> dict:
    """CREATE the run dicts of one run — one per raid of its lineup — that carry its settings (size, cutoffs from its
    schedule's cadence, how many rosters it expects) under new keys (new_run_key; finding an existing run is the
    caller's: find_run). `schedule` = the schedule id it comes from (None: the schedule with a run at exactly `start`,
    else a one-off — open_schedule; a test run: a rehearsal on the game's suggested timing). `cutoffs` = {soft, hard,
    confirm} in hours to override the cadence (test runs compress it to minutes) — and makes it a TEST run (one raid).
    `cutoffs["test_channel"]`: where the command was used — the test run's cards post there (D20). Part 0's dict is
    stamped with its identity: `occurrence` (a schedule's run: the start the schedule gave it, default `start` in guild
    wall time; None for a one-off), `event` (its own key), `part` 0, `starts` 0.
    `lineup` = the LATER raids [{raid, rosters[, starts]}] (None = the schedule's own follow-on and `then`; `follow` = a
    one-entry alias): each gets its own dict (part k): part 0's settings with its raid, size and rosters, `follows` =
    part 0's key, `followed_by` = the next part's key, `part`, `starts` (minutes after part 0, Registry.lineup_offsets),
    the cutoffs part 0's shifted by `starts` (its lock, nudge and confirm fall at part 0's instants), no confirmations,
    no reminders, no DM at open, no series. Part 0's dict holds the whole `lineup` (with each entry's offset) and
    `followed_by`. Every dict is checked before a key is drawn and all are saved in ONE config write, so a refused
    lineup (LINEUP_MAX_WORDS, a raid twice, an unknown raid, LINEUP_ORDER) writes nothing. `rs`: the store whose run
    files a new key must not meet. `lock_hours` / `first_rosters` (a one-off from the New event page, never a test run):
    the lock this many hours before the start instead of the schedule's, and the first raid's rosters. → part 0's dict."""
    from .registry import parse_lineup

    test = bool(cutoffs)
    if not schedule and not test:  # none named: the schedule that runs at that start, else a one-off (open_schedule)
        schedule = open_schedule(reg, instance, start, None)
    if test and not schedule:  # a rehearsal of the raid: a recurring run on the game's suggested timing, its own cutoffs
        rd = schedule_def_of({**reg.raid_def(instance), "schedules": [{"id": _REHEARSAL, "kind": "weekly"}]}, _REHEARSAL)
    else:
        rd = reg.schedule_def(instance, schedule)
    sched = rd["schedule"]
    later = lineup if lineup is not None else ([follow] if follow else schedule_lineup(instance, sched)[1:])
    later = [] if test else [e for e in later if e]
    own = test or (first_rosters is None and lock_hours is None)  # the New event page's one-off choices, never a test run's
    first_n = int(sched.get("rosters") or 1) if own or first_rosters is None else int(first_rosters)
    full = parse_lineup([{"raid": instance, "rosters": first_n}, *later])  # LINEUP_MAX_WORDS, a raid twice, 1 to 4 rosters
    for e in full[1:]:
        if e["raid"] not in reg.profile.raids or e["raid"] == instance:
            raise RegistryError(f"a follow-on is another raid of the profile (one of {', '.join(reg.profile.raids)})")
    offsets = reg.lineup_offsets(full)  # LINEUP_ORDER
    key = new_run_key(reg, rs, instance)
    lead, lock, confirm = float(rd["signup_lead_hours"]), float(rd["lock_hours_before"]), float(rd["confirm_hours_before"])
    if not own and lock_hours is not None:  # the New event page's Locks choice: the reminder and confirm-by never after it
        lock = float(lock_hours)
        confirm = min(confirm, lock)
        rd = {**rd, "nudge_hours_before": max(float(rd["nudge_hours_before"]), lock)}
    c = cutoffs or {}
    if not c:  # test runs pass their own compressed cutoffs and mean them
        available = (start - reg.now_local()).total_seconds() / 3600
        nudge_h, lock, confirm = fit_cutoffs(available, float(rd["nudge_hours_before"]), lock, confirm)
        rd = {**rd, "nudge_hours_before": nudge_h}
    t = {"key": key, "name": f"{rd.get('name', instance)} {reg.local12(start)}", "size": int(rd.get("size") or DEFAULT_RAID_SIZE), "schedule": start.strftime("%a %H:%M"), "instance": instance,
         "cutoff_soft_hours": c.get("soft", float(rd["nudge_hours_before"])), "cutoff_hard_hours": c.get("hard", lock), "confirm_hours": c.get("confirm", confirm), "open_days_before": lead / 24,
         "reminders": "dm" if c.get("nudge", rd["nudge"]) else OFF, "open_dm": bool(c.get("open_dm", rd["open_dm"])), "autofill": bool(rd["autofill"]), "ephemeral": True,
         "created_at": now()}  # cleanup_ephemeral drops a dict whose open was cut off before its run file (DICT_WITHOUT_RUN_HOURS)
    # every run carries its class and series (roster redesign Phase 1): a one-off (ADHOC_SCHEDULE) and a pickup template
    # are ad hoc; anything else is a run of a recurring series whose regulars it expects
    kind = rd.get("class") or schedule_class(sched.get("kind"))
    t.update({"schedule_id": sched["id"], "kind": kind, "series": sched["id"] if kind == "recurring" and not test else None,
              "confirm": True if cutoffs else bool(rd.get("confirm", True))})  # a test run rehearses the confirmations
    # the run carries its schedule's settings
    t.update({"rosters": first_n, "fill_ask_hours": float(rd["fill_ask_hours"]), "split_policy": rd.get("split_policy") or "balanced"})
    occ = (occurrence or occurrence_stamp(reg, start)) if schedule_stamp(sched["id"]) else None
    t.update({"occurrence": occ, "event": key, "part": 0, "starts": 0})
    if not test and (sched.get("virtual") or len(reg.schedules(instance)) > 1):  # the raid's only schedule needs no name on the run
        t["name"] = f"{rd.get('name', instance)} · {sched.get('name') or sched['id']} {reg.local12(start)}"
    if cutoffs:
        t["test"] = True
        t["test_by"] = c.get("test_by")
        if c.get("test_channel"):
            t["test_channel"] = int(c["test_channel"])  # the asking channel: the run's officer cards post there (D20)
    parts: list[dict] = []
    if len(full) > 1:  # the later raids (design §5.62): one dict each, parts 1.. of the same event, from part 0's signup
        keys = {key}
        for k, (e, at) in enumerate(zip(full[1:], offsets[1:]), start=1):
            pkey = key
            while pkey in keys:
                pkey = new_run_key(reg, rs, e["raid"])
            keys.add(pkey)
            pt = _part_dict(reg, t, e, k, at, key=pkey, start=start)
            if parts:
                parts[-1]["followed_by"] = pkey
            parts.append(pt)
        t["lineup"] = [{**e, "starts": at} for e, at in zip(full, offsets)]
        t["followed_by"] = parts[0]["key"]
    reg.config.rosters.extend(parts)
    reg.config.rosters.append(t)
    reg.save_config(f"run {key} ({t['name']}) created by {by}" + (f" with {len(parts)} later raid{'s' if len(parts) > 1 else ''}" if parts else ""), notify=False)
    return t


def _part_dict(reg: Registry, first: dict, entry: dict, k: int, offset: int, *, key: str, start: datetime) -> dict:
    """The run dict of part `k` of a run (design §5.62): part 0's dict `first` with the entry's raid, size and rosters,
    `follows` = part 0's key, `part` k, `starts` = `offset` minutes after part 0's `start`, the cut-offs part 0's shifted
    by the offset (its lock, nudge and confirm fall at part 0's instants), no confirmations, no reminders, no DM at open,
    no series, never part 0's sheet nor its chain (`followed_by` is the caller's). ensure_run and apply_lineup build
    every later part through here."""
    frd, hours = reg.raid_def(entry["raid"]), offset / 60
    lead = reg.raid_def(first.get("instance")).get("name", first.get("instance"))
    pt = {**first, "key": key, "instance": entry["raid"], "size": int(frd.get("size") or DEFAULT_RAID_SIZE),
          "name": f"{frd.get('name', entry['raid'])} · after {lead} {reg.local12(start)}",
          "schedule": after_minutes(start, offset).astimezone(reg.tz).strftime("%a %H:%M"), "schedule_id": FOLLOW_SCHEDULE, "series": None,
          "confirm": False, "rosters": int(entry.get("rosters") or 1), "follows": first["key"], "reminders": OFF, "open_dm": False,
          "cutoff_soft_hours": first["cutoff_soft_hours"] + hours, "cutoff_hard_hours": first["cutoff_hard_hours"] + hours,
          "confirm_hours": first.get("confirm_hours", first["cutoff_hard_hours"]) + hours, "part": k, "starts": offset}
    for f in ("lineup", "follow", "followed_by", "message_id", "channel_id", "thread_id"):  # never part 0's sheet nor its chain
        pt.pop(f, None)
    return pt


def closed_run_refusal(rs: "RaidStore", ev: "RaidEvent | None") -> str | None:
    """Why run `ev` (the one an open meets: find_run) can't be opened, or None: it is CLOSED. A cancelled run is never
    brought back by an open (D19: open a new run at another time), and an ended one only by Reopen within the hour —
    open_run refuses rather than hand the closed run back as if it had opened."""
    if ev is None or ev.state not in ("done", "cancelled"):
        return None
    if ev.state == "cancelled":
        return "That time's run was cancelled, and a cancelled run isn't opened again. Pick another time."
    return (f"That time's run already ended. Reopen works for {reopen_window()} after the end if that was a mistake. "
            "Otherwise pick another time.")


def open_run(reg: Registry, rs: "RaidStore", instance: str, start: datetime, by: str = "scheduler", cutoffs: dict | None = None,
             schedule: str | None = None, follow: dict | None = None, lineup: list[dict] | None = None,
             lock_hours: float | None = None, first_rosters: int | None = None) -> "RaidEvent":
    """Open (or find the live) run for `instance` at `start`: a schedule's run is found by its occurrence (live: handed
    back, its missing later raids completed — complete_parts; closed: RegistryError with closed_run_refusal's sentence,
    nothing written), a one-off or a test run by a live run at that minute (live_at); else a new run (ensure_run, then
    open_event for part 0 and complete_parts for the rest). A rehearsal at that minute that another tester opened is
    refused. `lineup` = the LATER raids [{raid, rosters[, starts]}] (None = the schedule's own; `follow` = a one-entry
    alias): each opens as a part of the same run (design §5.62) — no sheet, its answers the first part's. `lock_hours` /
    `first_rosters`: ensure_run's (a new one-off only; a run found keeps its own)."""
    test = bool(cutoffs)
    if not schedule and not test:  # none named: the schedule that runs at that start, else a one-off (open_schedule)
        schedule = open_schedule(reg, instance, start, None)
    have = find_run(reg, rs, instance, start, schedule, test=test)
    if have is not None:
        why = closed_run_refusal(rs, have)
        if why:
            raise RegistryError(why)
        mine, theirs = (cutoffs or {}).get("test_by"), (reg.config.roster(have.team) or {}).get("test_by")
        if test and mine is not None and theirs is not None and str(mine) != str(theirs):
            # a rehearsal belongs to its tester (its pool, its buttons, its DMs): a second tester never inherits it
            who = reg.members.get(int(theirs)) if str(theirs).isdigit() else None
            raise RegistryError(f"{who.display_name if who else 'Another officer'} opened a test run at {reg.local12(start)} — "
                                "join theirs or clear it (nothing was opened)")
        complete_parts(reg, rs, have)  # an open cut off between its parts (a restart) is finished, once
        return have
    with reg.config_lock:  # one step for a box re-described under the lock (configops.apply_ops `expect`): never half an open
        t = ensure_run(reg, instance, start, by, cutoffs, schedule, follow=follow, rs=rs, lineup=lineup, lock_hours=lock_hours, first_rosters=first_rosters)
        ev = open_event(reg, rs, t, start)
        complete_parts(reg, rs, ev)
    return ev


def complete_parts(reg: Registry, rs: "RaidStore", first: "RaidEvent") -> list["RaidEvent"]:
    """Open the later raids of a live FIRST part whose run dict exists and whose run file doesn't — an open cut off
    between its parts (a restart, a failed write) — and link each part to the next (`followed_by`). Called by open_run
    (new and found runs) and by the scheduler for every live first part each minute. Never posts a sheet (a later part
    has none) and never makes a dict: the chain is the one ensure_run wrote. A part already on file in any state is
    never opened again. Saves part 0 once when something changed (its save copies the answers onto the new parts).
    → the parts opened now."""
    if is_follow(first) or first.state in ("done", "cancelled"):
        return []
    dicts = later_dicts(reg, first)
    if not dicts:
        return []
    opened: list[RaidEvent] = []
    prev, changed = first, False
    for t in dicts:
        ev = next((e for e in rs.events.values() if e.team == t["key"]), None)
        if ev is None:
            at = dict_start(reg, first, t)
            ev = open_event(reg, rs, t, at)
            opened.append(ev)
            first.log.append(f"then {reg.raid_def(ev.instance).get('name', ev.instance)} ×{int(t.get('rosters') or 1)} at {reg.local12(at)} from this signup")
            changed = True
        if prev.followed_by != ev.key:
            prev.followed_by = ev.key
            if prev is first:
                changed = True
            else:
                rs.save(prev, "next raid linked")
        prev = ev
    if changed:
        rs.save(first, "later raids opened" if opened else "later raids linked")  # the save syncs their answers
    return opened


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
    """A test run two weeks out with every puppet joined, never posted: its event page draws the board as the lock
    would place them — the best comp from whatever mix the bench holds. Cleared with the rest of the test bench."""
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
        new = schedule not in {s["id"] for s in sh.all_schedules()}  # a named raid means create: an existing one names none
        sh.set_schedule(schedule, dict(schedule_fields), "preview", raid=instance if new else None)
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
    return [f"{name}" + (f" · {r['name']}" if len(rows) > 1 else "") + ": next " + (", ".join(r["labels"]) or "none (no run times yet)")
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


def move_runs_with_clock(reg: Registry, rs: "RaidStore", rows: list[dict], zone: str, by: str) -> list["RaidEvent"]:
    """"Move open runs with the clock" (dual interfaces §3.5, a timezone change): every movable row of
    Registry.timezone_runs (computed BEFORE the zone changed) keeps its wall-clock time on the new zone. The run's key,
    its roster entry and its name were all made from that wall clock, so they stay; only the instant (`starts_at`)
    moves — the sheet, its signups, pins and board are the same run, nobody re-answers, nobody is DMed. Its nudge, lock
    and confirm marks are hours before the start, so they move with it. Locked runs, test runs and a run whose lock would
    already be past are left at their real moment (the confirm said so). On the event loop with the bot's live store
    (rc.open_run's rule L3); the caller refreshes the sheets. Returns the moved runs."""
    moved = []
    for r in rows:
        ev = rs.events.get(r["ev"].key)
        if ev is None or not r.get("movable") or ev.state != "open":
            continue
        place_run(reg, ev, r["moved"])
        ev.log.append(f"moved with the clock to {zone} by {by}: still {reg.local12(r['moved'])}")
        rs.save(ev, f"moved with the clock ({zone})")
        moved.append(ev)
    return moved


def place_run(reg: Registry, ev: "RaidEvent", start: datetime, team: dict | None = None) -> None:
    """THE start of a run moved (a timezone change's "move with the clock", a reschedule): its instant (`starts_at`) and,
    when given, its run dict's weekday-and-time line (`schedule`, guild wall time). Its key, identity stamps, sheet,
    signups, pins and seats are never touched here."""
    ev.starts_at = start.isoformat()
    if team is not None:
        team["schedule"] = start.astimezone(reg.tz).strftime("%a %H:%M")


# ---------------------------------------------------------------- reschedule and per-run timing (phase 3, Task 5)

# a run's own sheet timing: the schedule setting (SCHEDULE_TIMING's words, what a schedule and the surfaces name) → the
# run dict key it lives under. `nudge` stores "dm" / OFF there; the rest are hours, or a switch for autofill
EVENT_TIMING = {"nudge_hours_before": "cutoff_soft_hours", "lock_hours_before": "cutoff_hard_hours",
                "confirm_hours_before": "confirm_hours", "fill_ask_hours": "fill_ask_hours", "nudge": "reminders",
                "autofill": "autofill"}
class Refusal(str):
    """A refusal sentence that knows which kind it is (consequences.REFUSAL_TITLES: past · clash · …) — for the
    sentences built from parts (a past start, a clash), whose words can't be compared with a constant."""

    kind: str = "other"

    def __new__(cls, text: str, kind: str = "other"):
        obj = super().__new__(cls, text)
        obj.kind = kind
        return obj


MOVE_CLOSED = "This run is finished or cancelled, so it can't move."
MOVE_STARTED = "This run has started, so it can't move."
TIMING_SAME = "The run already has that timing."
MOVE_CLASH = "{raid} already has a run at {when}. Pick another time."
MOVE_SAME = "The run already starts then."
TIMING_LOCKED = "The sheet timing is fixed once the roster locks. Move the start instead."
TIMING_ORDER = "The reminder has to come before the lock, and confirm-by after it."
TIMING_UNKNOWN = "A run's timing is its reminder, lock, confirm-by, fill asks and automatic asks."
MOVE_OFFICER_ONLY = "Only an officer moves a run or changes its timing."
MOVE_CONFIRM_GRACE = timedelta(hours=1)  # a confirm-by the move leaves in the past: within the hour instead (never past the start)


MOVE_GAP = "That time doesn't exist that night because the clocks go forward. Pick another time."
MOVE_TWICE = "That time happens twice that night because the clocks go back. Pick another time."
MOVE_WHEN = "Pick the new day and time with the picker."
RUN_CHANGED = "Someone changed this run a moment ago, so nothing was saved. Check it and try again."


def move_start(reg: Registry, value) -> datetime:
    """A reschedule's new start from a surface: an aware ISO instant (MCP, a preview carried back) as it is, or the guild
    WALL time 'YYYY-MM-DD HH:MM' (the site's datetime-local, the plain-text parser's machine field) resolved DST-safely
    by wizard.resolve_local — a wall time the clocks skip (MOVE_GAP) or pass twice (MOVE_TWICE) is refused, never guessed
    (open_when's opening takes the shifted time; a move names one exact minute). Anything else: MOVE_WHEN."""
    from .wizard import resolve_local

    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        raise RegistryError(MOVE_WHEN) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(reg.tz)
    start, note = resolve_local(parsed.replace(second=0, microsecond=0), reg.tz)
    if note == "gap":
        raise RegistryError(MOVE_GAP)
    if note == "ambiguous":
        raise RegistryError(MOVE_TWICE)
    return start


def event_rev(reg: Registry, rs: "RaidStore", ev: "RaidEvent") -> str:
    """The fingerprint of what a reschedule or a lineup edit acts on (any raid of a run stands for its first): the first
    raid's start and state, the lineup it plays now and its own sheet timing. A preview hands it out; a write carrying an
    older one is refused (RUN_CHANGED) — a change made meanwhile is never overwritten unseen."""
    import hashlib

    first = first_part(rs, ev)
    team = run_team(reg, first)
    timing = {k: (team.get(k) if k in team else team_setting(team, k)) for k in sorted(set(EVENT_TIMING.values()))}
    raw = json.dumps([first.starts_at, first.state, run_lineup(reg, rs, first), timing], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def cancel_tells_channel(rs: "RaidStore", ev: "RaidEvent") -> bool:
    """Whether a cancel pressed on `ev` can post its fixed line in the signup channel: the run's first raid owns the
    sheet and its channel, so a later raid whose first raid has already finished has neither (its cancel takes the raids
    still to play, and nothing posts)."""
    if not is_follow(ev):
        return True
    first = first_part(rs, ev)
    return first is not ev and first.state not in ("done", "cancelled")


def answered_ids(first: "RaidEvent") -> list[int]:
    """Who a change to a run's start times tells (the `moved` notice: reschedule and a lineup edit alike): everyone with
    a row on the first raid — Join, Bench or No thanks, a test puppet's included — but an away row, which nobody
    answered."""
    return [sg.discord_id for sg in first.signups.values() if sg.source != "absence"]


class Moved(BaseModel):
    """What a reschedule did (or, as a preview, would do): the run's raids, the old and new start (ISO), the run's own
    timing set now (run dict keys), who gets a `moved` notice, the away rows taken off and written, whether the first
    raid locks at the scheduler's next minute, and the people also Join on another run that night (uid → its key)."""

    parts: list[str]
    old: str
    new: str
    timing: dict = Field(default_factory=dict)
    told: list[int] = Field(default_factory=list)
    absence_cleared: list[int] = Field(default_factory=list)
    absence_written: list[int] = Field(default_factory=list)
    locks_now: bool = False
    conflicts: dict[int, str] = Field(default_factory=dict)


def run_timing(reg: Registry, first: "RaidEvent") -> dict:
    """A run's own sheet timing as {setting: value} (EVENT_TIMING's keys, _timing_values' inverse): hours as numbers,
    `nudge` and `autofill` as switches — what a surface shows and sends back to the reschedule verb unchanged."""
    team = run_team(reg, first)
    out = {k: team_setting(team, v) for k, v in EVENT_TIMING.items()}
    out["nudge"] = out["nudge"] not in (None, OFF, False)
    out["autofill"] = bool(out["autofill"])
    return out


def _timing_values(timing: dict | None) -> dict:
    """`timing` {setting: value} (EVENT_TIMING's keys) → {run dict key: value}; RegistryError(TIMING_UNKNOWN) for any other
    setting or a value that isn't one."""
    out: dict = {}
    for k, v in (timing or {}).items():
        key = EVENT_TIMING.get(k)
        if key is None:
            raise RegistryError(TIMING_UNKNOWN)
        if k == "nudge":
            out[key] = OFF if v in (False, None, OFF, "off", "false", 0) else "dm"
        elif k == "autofill":
            out[key] = v not in (False, None, "off", "false", 0)
        else:
            try:
                h = float(v)
            except (TypeError, ValueError):
                raise RegistryError(TIMING_UNKNOWN) from None
            if h < 0:
                raise RegistryError(TIMING_UNKNOWN)
            out[key] = int(h) if h == int(h) else h
    return out


def timing_changes(reg: Registry, first: "RaidEvent", timing: dict | None) -> dict:
    """The part of `timing` that changes the run (run dict keys → new values): a value equal to the run's own now is
    dropped. RegistryError(TIMING_UNKNOWN) as _timing_values."""
    team = run_team(reg, first)
    return {k: v for k, v in _timing_values(timing).items() if (team.get(k) if k in team else team_setting(team, k)) != v}


def reschedule_refusal(reg: Registry, rs: "RaidStore", ev: "RaidEvent", start: datetime | None, timing: dict | None,
                       now: datetime | None = None) -> str | None:
    """Why `ev`'s run can't move to `start` / take `timing`, in one sentence, else None — in this order: finished or
    cancelled (MOVE_CLOSED), already started (MOVE_STARTED: neither start nor timing), a start more than
    PAST_GRACE_MINUTES ago (consequences.move_past_sentence), nothing that changes (MOVE_SAME; TIMING_SAME for a timing-only no-op), its
    first raid already has another live run at that minute (MOVE_CLASH), timing on a locked run (TIMING_LOCKED), a
    reminder after the lock or a confirm-by before it (TIMING_ORDER). Any raid of a run answers for its first raid."""
    first = first_part(rs, ev)
    if first.state in ("done", "cancelled"):
        return MOVE_CLOSED
    now = now or reg.now_local()
    if first.start <= now:  # under way (a loot council may be live): neither its start nor its timing moves
        return MOVE_STARTED
    try:
        values = timing_changes(reg, first, timing)
    except RegistryError as e:
        return str(e)
    if start is not None and start < now - timedelta(minutes=PAST_GRACE_MINUTES):
        from .consequences import move_past_sentence

        return move_past_sentence(reg, start, first.start)
    if (start is None or start == first.start) and not values:
        return TIMING_SAME if timing else MOVE_SAME
    if start is not None and start != first.start:
        other = live_at(rs, first.instance, start, test=bool(first.test))
        if other is not None and not same_event(other, first):
            return Refusal(MOVE_CLASH.format(raid=reg.raid_def(first.instance).get("name", first.instance), when=reg.local12(start)), "clash")
    if values:
        if first.state != "open":
            return TIMING_LOCKED
        team = run_team(reg, first)
        soft = float(values.get("cutoff_soft_hours", team_setting(team, "cutoff_soft_hours")))
        hard = float(values.get("cutoff_hard_hours", team_setting(team, "cutoff_hard_hours")))
        confirm = float(values.get("confirm_hours", team.get("confirm_hours", hard)))
        if soft < hard or confirm > hard:
            return TIMING_ORDER
    return None


def absorbed_schedule_run(reg: Registry, rs: "RaidStore", ev: "RaidEvent", start: datetime | None) -> str | None:
    """A run of a schedule moved exactly onto another start that schedule gives and hasn't opened: from then on this run
    stands for it (event_of_occurrence's moved-run case), so that start never opens a sheet of its own. → the
    schedule's name to say so, else None."""
    from .registry import schedule_name

    first = first_part(rs, ev)
    sid = first.schedule
    if not sid or start is None or start == first.start:
        return None
    occ = occurrence_stamp(reg, start)
    if occ == first.occurrence:
        return None
    try:
        sched = reg.schedule_entry(sid)
    except RegistryError:
        return None
    if start not in schedule_starts(reg, sched["raid"], sched, start, 1):
        return None
    if event_of_occurrence(rs, sid, occ, reg=reg) is not None:
        return None
    return schedule_name(sched)


def recompute_absences(reg: Registry, ev: "RaidEvent") -> tuple[list[int], list[int]]:
    """A run on a new day (C18): an away row (`source` "absence") comes off when the member's absences no longer cover
    the new day; everyone the sheet could expect (absence_pool) who is away that day and has no row gets one. Every
    other row stands — an answer someone gave is theirs, whatever the day. → (uids cleared, uids written)."""
    day = ev.start.astimezone(reg.tz).date().isoformat()
    cleared = []
    for k, sg in list(ev.signups.items()):
        m = reg.members.get(sg.discord_id)
        if sg.source == "absence" and not (m is not None and m.absent_on(day)):
            del ev.signups[k]
            cleared.append(sg.discord_id)
    written = []
    for m, c in absence_pool(reg, ev):
        if c is not None and str(m.discord_id) not in ev.signups and m.absent_on(day):
            ev.signups[str(m.discord_id)] = _signup(reg, m, c, "out", "absence")
            written.append(m.discord_id)
    return cleared, written


def _run_name(reg: Registry, t: dict, old: datetime, new: datetime) -> str:
    """A run dict's name with its time re-rendered: the old start's words become the new start's (a later raid's name
    carries the FIRST raid's start, so the first raid's old and new starts are what is replaced on every dict)."""
    name = str(t.get("name") or "")
    was, now_ = reg.local12(old), reg.local12(new)
    return name.replace(was, now_) if was in name else name


def reschedule(reg: Registry, rs: "RaidStore", ev: "RaidEvent", *, start: datetime | None = None, timing: dict | None = None,
               by: str, save: bool = True) -> Moved:
    """THE move of a run (any raid of it answers for its first): every raid's `starts_at` moves with the first's — each
    keeps its offset, in real time (after_minutes) —, each run dict's weekday-and-time line and its name follow; `timing`
    ({setting: value}, EVENT_TIMING) is the run's own on the first raid's dict, later raids' cut-offs the first's plus
    their offset. The `occurrence` and `schedule` stamps NEVER change (C11): a moved run still answers for the occurrence
    it was opened for. A confirm-by moves by the same amount, or to within the hour when that is already past (never
    past the start); fill asks keep their expiry; away rows are worked out again for the new day (recompute_absences);
    one log line. Every raid and the dicts are saved in ONE store batch. Never touches config.schedules. The caller
    checks reschedule_refusal first. `save=False` = the preview: a copy, nothing written."""
    import copy

    first = first_part(rs, ev)
    parts = [p for p in event_parts(rs, first) if p.state not in ("done", "cancelled") or p is first]
    teams = {p.key: reg.config.team(p.team) for p in parts}
    if not save:
        parts = [p.model_copy(deep=True) for p in parts]
        teams = {k: copy.deepcopy(t) for k, t in teams.items()}
        first = next(p for p in parts if p.key == first.key)
    old = first.start
    new = start if start is not None else old
    delta = new - old
    now_ = reg.now_local()
    values = timing_changes(reg, first, timing)
    t0 = teams.get(first.key)
    if t0 is not None and values:
        t0.update(values)
    for p in parts:
        at = new if p is first else after_minutes(new, p.starts)
        was = p.start
        t = teams.get(p.key)
        place_run(reg, p, at, t)
        if t is not None:
            t["name"] = _run_name(reg, t, old, new)
            if p is not first and t0 is not None:
                hours = p.starts / 60
                for k in ("cutoff_soft_hours", "cutoff_hard_hours", "confirm_hours"):
                    if k in t0:
                        t[k] = t0[k] + hours
                for k in ("fill_ask_hours", "autofill"):
                    if k in values:
                        t[k] = values[k]
        if p.confirm_by and at != was:
            cb = datetime.fromisoformat(p.confirm_by) + (at - was)
            if cb <= now_:
                cb = min(at, now_ + MOVE_CONFIRM_GRACE)
            p.confirm_by = cb.isoformat()
    cleared, written = recompute_absences(reg, first) if delta else ([], [])
    told = answered_ids(first) if delta else []
    team = t0 or run_team(reg, first)
    hard = first.start - timedelta(hours=float(team_setting(team, "cutoff_hard_hours")))
    ins = {sg.discord_id for sg in first.signups.values() if sg.status == "in"}
    clash = {uid: key for uid, key in conflicts(rs, first).items() if uid in ins}
    moved = Moved(parts=[p.key for p in parts], old=old.isoformat(), new=new.isoformat(), timing=values, told=told,
                  absence_cleared=cleared, absence_written=written, locks_now=first.state == "open" and hard <= now_, conflicts=clash)
    if not save:
        return moved
    what = [f"moved from {reg.local12(old)} to {reg.local12(new)} by {by}"] if delta else []
    if values:
        what.append(f"own timing set by {by}: " + ", ".join(f"{k} {v}" for k, v in values.items()))
    first.log.extend(what)
    line = "; ".join(what) or f"saved by {by}"
    with reg.store.batch(f"{reg.key}: run {first.key} {line}"):
        reg.save_config(f"run {first.key} {line}", notify=False)
        for p in sorted(parts, key=lambda e: e is first):  # the first raid last: its save copies its answers onto the later raids
            rs.save(p, line)
    return moved


# ---------------------------------------------------------------- the lineup of an open run (phase 3, Task 6)

LINEUP_LOCKED = "The lineup is fixed once the roster locks."
LINEUP_CLOSED = "This run is finished or cancelled, so its raids can't change."
LINEUP_FIRST = "The first raid of a run can't change. Start a new run instead."
LINEUP_GONE = "This game has no raid called {raid}. Pick another raid."
LINEUP_SAME = "The run already has that lineup."
LINEUP_OFFICER_ONLY = "Only an officer changes the raids of a run."


class LineupChange(BaseModel):
    """What an edit of a run's lineup does (plan_lineup; apply_lineup writes it): the new raids' entries ({raid, rosters,
    starts}), the later parts taken out (their run keys), the new order of the run keys (index 0 the first part; None
    = a new part, in `added`'s order), the parts whose roster count changes (run key → count), what each removed part
    held — its board places, its pins, its answers to leave before it and its open fill asks —, the KEPT later parts
    whose start changes (run key → its new start in minutes after the first raid's), who gets a `moved` notice for that
    (everyone with a row on the first raid but an away row; [] when no kept start changes), and the whole new lineup
    with every entry's start in minutes after the first raid's (entry 0 at 0)."""

    added: list[dict] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    order: list[Optional[str]] = Field(default_factory=list)
    rosters: dict[str, int] = Field(default_factory=dict)
    dropped_places: dict[str, int] = Field(default_factory=dict)
    dropped_pins: dict[str, int] = Field(default_factory=dict)
    dropped_leaving: dict[str, int] = Field(default_factory=dict)
    dropped_asks: dict[str, int] = Field(default_factory=dict)
    trimmed_places: dict[str, int] = Field(default_factory=dict)  # a kept part whose roster count goes down: its board
    trimmed_pins: dict[str, int] = Field(default_factory=dict)  # places (and their pins) on the rosters that go
    moved: dict[str, int] = Field(default_factory=dict)
    told: list[int] = Field(default_factory=list)
    lineup: list[dict] = Field(default_factory=list)


def _live_later(rs: "RaidStore", first: "RaidEvent") -> list["RaidEvent"]:
    return [p for p in later_parts(rs, first) if p.state not in ("done", "cancelled")]


def run_lineup(reg: Registry, rs: "RaidStore", ev: "RaidEvent") -> list[dict]:
    """The lineup a run plays now — [{raid, rosters, starts}], the first raid at 0, then each live later raid with its
    minutes after the first — read from its run files (any raid of it stands for its first)."""
    first = first_part(rs, ev)
    out = [{"raid": first.instance, "rosters": int((reg.config.team(first.team) or {}).get("rosters") or 1), "starts": 0}]
    for p in _live_later(rs, first):
        at = p.starts or round((p.start - first.start).total_seconds() / 60)
        out.append({"raid": p.instance, "rosters": int((reg.config.team(p.team) or {}).get("rosters") or 1), "starts": at})
    return out


def parse_run_lineup(lineup) -> list[dict]:
    """An edited lineup validated as a run's (parse_lineup's words said of a run, not a schedule): entry 0's own start
    (0) dropped, then parse_lineup."""
    from .registry import parse_lineup

    rows = [dict(e) if isinstance(e, dict) else e for e in (lineup or [])]
    if rows and isinstance(rows[0], dict) and rows[0].get("starts") in (0, "0"):
        rows[0].pop("starts")
    try:
        full = parse_lineup(rows)
    except RegistryError as e:
        raise RegistryError(str(e).replace("A schedule can't run the same raid twice in one night.", "A run can't hold the same raid twice.")
                            .replace("A schedule", "A run").replace("in a schedule", "in a run").replace("of a schedule", "of a run")
                            .replace("the schedule's own time", "the run's own time")) from None
    return full


def lineup_fixed(rs: "RaidStore", ev: "RaidEvent") -> str | None:
    """Why `ev`'s run can't change its lineup at all, whatever the lineup: finished or cancelled (LINEUP_CLOSED), or its
    roster locked — the first raid or any later one (LINEUP_LOCKED); None while it can. THE rule lineup_refusal and
    the Edit lineup page (event_view.lineup_edit_json) share."""
    first = first_part(rs, ev)
    if first.state in ("done", "cancelled"):
        return LINEUP_CLOSED
    if first.state != "open" or any(p.state == "locked" for p in later_parts(rs, first)):
        return LINEUP_LOCKED
    return None


def lineup_refusal(reg: Registry, rs: "RaidStore", ev: "RaidEvent", lineup: list[dict]) -> str | None:
    """Why `ev`'s run can't take `lineup` ([{raid, rosters[, starts]}], its first raid first), in one sentence, else
    None — in this order: finished or cancelled (LINEUP_CLOSED), locked (LINEUP_LOCKED), parse_lineup's refusals
    (LINEUP_MAX_WORDS, a raid twice, a bad roster count), another first raid (LINEUP_FIRST), a raid the game profile
    lacks (LINEUP_GONE), the starts out of order (LINEUP_ORDER), nothing that changes (LINEUP_SAME). Any raid of a run
    answers for its first."""
    first = first_part(rs, ev)
    if (why := lineup_fixed(rs, first)):
        return why
    try:
        full = parse_run_lineup(lineup)
    except RegistryError as e:
        return str(e)
    if full[0]["raid"] != first.instance:
        return LINEUP_FIRST
    for e in full:
        if e["raid"] not in reg.profile.raids:
            return LINEUP_GONE.format(raid=e["raid"])
    try:
        offsets = reg.lineup_offsets(full)
    except RegistryError as e:
        return str(e)
    now_ = [(e["raid"], e["rosters"], at) for e, at in zip(full, offsets)]
    if now_ == [(e["raid"], e["rosters"], e["starts"]) for e in run_lineup(reg, rs, first)]:
        return LINEUP_SAME
    return None


def _board_places(p: "RaidEvent") -> int:
    """How many board places run `p`'s board holds (a place is a signup's display name in a group)."""
    return len({n for g in (p.layout or []) for n in g})


def roster_trim(reg: Registry, p: "RaidEvent", rosters: int) -> tuple[list[list[str]], dict[str, str], int, int]:
    """What part `p`'s board keeps when it goes down to `rosters` rosters: (the layout of the rosters that stay, the
    pins left, how many board places go, how many pins go). A place on a roster that goes is dropped with its pin —
    the person stays joined and the lock places them again (else _solve_run would build the old count from the board)."""
    k = groups_per_roster(reg, run_size(reg, p))
    layout = [list(g) for g in (p.layout or [])]
    keep, gone = layout[: rosters * k], layout[rosters * k:]
    names = {n for g in gone for n in g} - {n for g in keep for n in g}
    uids = {str(sg.discord_id) for sg in p.signups.values() if sg.display_name in names}
    pins = {u: v for u, v in p.pins.items() if u not in uids}
    return keep, pins, len(names), len(p.pins) - len(pins)


def plan_lineup(reg: Registry, rs: "RaidStore", ev: "RaidEvent", lineup: list[dict]) -> LineupChange:
    """What `lineup` does to `ev`'s run (any raid of it stands for its first), worked out and nothing written: a live
    later part whose raid is still in the lineup is KEPT (its run key, signups, pins, board and leaving answers),
    moved to its new place; a raid new to the run is a new part; a live later part whose raid is gone is removed. Each
    entry's start is its own `starts`, else the C12 default (Registry.lineup_offsets). The caller checks lineup_refusal
    first."""
    first = first_part(rs, ev)
    full = parse_run_lineup(lineup)
    offsets = reg.lineup_offsets(full)
    live = _live_later(rs, first)
    by_raid = {p.instance: p for p in live}
    raids = {e["raid"] for e in full}
    ch = LineupChange(order=[first.key], lineup=[{**e, "starts": at} for e, at in zip(full, offsets)])
    if full[0]["rosters"] != int((reg.config.team(first.team) or {}).get("rosters") or 1):
        ch.rosters[first.key] = full[0]["rosters"]
        _note_trim(reg, ch, first, full[0]["rosters"])
    for e in ch.lineup[1:]:
        p = by_raid.get(e["raid"])
        if p is None:
            ch.order.append(None)
            ch.added.append(dict(e))
            continue
        ch.order.append(p.key)
        if e["rosters"] != int((reg.config.team(p.team) or {}).get("rosters") or 1):
            ch.rosters[p.key] = e["rosters"]
            _note_trim(reg, ch, p, e["rosters"])
        was = p.starts or round((p.start - first.start).total_seconds() / 60)
        if int(e["starts"]) != was:  # a kept raid starts at another time: everyone who answered is told (reschedule's notice)
            ch.moved[p.key] = int(e["starts"])
    for p in live:
        if p.instance not in raids:
            ch.removed.append(p.key)
            ch.dropped_places[p.key] = _board_places(p)
            ch.dropped_pins[p.key] = len(p.pins)
            ch.dropped_leaving[p.key] = len([u for u in p.leaving if str(u) in first.signups])
            ch.dropped_asks[p.key] = len([a for a in p.fill_asks if a.pending])
    if ch.moved:
        ch.told = answered_ids(first)
    return ch


def _note_trim(reg: Registry, ch: LineupChange, p: "RaidEvent", rosters: int) -> None:
    _keep, _pins, places, pins = roster_trim(reg, p, rosters)
    if places:
        ch.trimmed_places[p.key] = places
    if pins:
        ch.trimmed_pins[p.key] = pins


def apply_lineup(reg: Registry, rs: "RaidStore", ev: "RaidEvent", change: LineupChange, by: str) -> list[str]:
    """THE write of an edited lineup (RaidMixin.edit_lineup holds every part's board lock and re-checks lineup_refusal).
    Run keys never change: a kept part keeps its key, its run file and its own pins, board and leaving answers, and
    takes its new `part`, `starts`, `starts_at` and `followed_by`; its run dict its new `part`, `starts`, weekday line,
    cut-offs (part 0's shifted by its start), rosters and `followed_by`. A new raid is a new part: a dict from part 0's
    (_part_dict, a fresh new_run_key) and its run file (open_event; part 0's save copies the answers onto it). A
    removed part's run file is cancelled (`closed_at`, `dropped`, the log line "removed from the lineup by {by}") —
    saved BEFORE its dict is dropped, so a crash between leaves no open part without a dict — no notice, no card. Its
    open fill asks are the caller's to close, under that part's plan_lock (every writer of ev.fill_asks holds it):
    RaidMixin.edit_lineup does, and edits their cards. Part 0's dict takes the whole `lineup` and its rosters;
    part 0's signups are never touched, and every remaining later part still mirrors them (sync_parts, on part 0's
    save, last). One save_config and one store batch. → the run keys of the run in their new order."""
    first = first_part(rs, ev)
    t0 = reg.config.team(first.team)
    if t0 is None:
        raise RegistryError(LINEUP_CLOSED)
    start = first.start
    later = {p.key: p for p in later_parts(rs, first)}
    words = reg.later_words(change.lineup[1:])
    line = f"lineup changed by {by}: {reg.raid_def(first.instance).get('name', first.instance)}" + (f", then {words}" if words else " alone")
    trims = {}  # a part going down in rosters: its board places (and pins) on the rosters that go, worked out before any dict changes
    for key, n in change.rosters.items():
        p = first if key == first.key else later.get(key)
        if p is not None and n < int((reg.config.team(p.team) or {}).get("rosters") or 1):
            trims[key] = roster_trim(reg, p, int(n))
    with reg.store.batch(f"{reg.key}: run {first.key} {line}"):
        for key, (keep, pins, places, npins) in trims.items():
            p = first if key == first.key else later[key]
            # always cut to the rosters that stay: the board saves PADDED layouts, so an empty roster that goes would
            # survive, be drawn and (one drag into it) make the lock build the old count; the log line counts losses only
            p.layout, p.pins = (keep if any(keep) else None), pins
            if places or npins:
                p.log.append(f"{places} board place(s) and {npins} pin(s) on the removed roster dropped by {by}")
        for key in change.removed:  # first: a removed raid is a closed, dropped file before its dict goes
            p = later[key]
            p.prev_state, p.state, p.closed_at = p.state, "cancelled", now()
            p.dropped, p.followed_by = True, None
            p.log.append(f"removed from the lineup by {by}")
            rs.save(p, f"removed from the lineup by {by}")
        chain: list[tuple[dict, "RaidEvent | None"]] = []
        taken = {t["key"] for t in reg.config.rosters if isinstance(t, dict)}
        for k, (key, e) in enumerate(zip(change.order[1:], change.lineup[1:]), start=1):
            at, hours = int(e["starts"]), int(e["starts"]) / 60
            if key is None:
                pkey = new_run_key(reg, rs, e["raid"])
                while pkey in taken:
                    pkey = new_run_key(reg, rs, e["raid"])
                taken.add(pkey)
                t = _part_dict(reg, t0, e, k, at, key=pkey, start=start)
                reg.config.rosters.append(t)
                chain.append((t, None))
                continue
            p = later[key]
            t = reg.config.team(p.team)
            if t is None:  # its dict cleaned up under it: rebuilt from part 0's, under the part's own dict key
                t = _part_dict(reg, t0, e, k, at, key=p.team, start=start)
                reg.config.rosters.append(t)
            t.update({"part": k, "starts": at, "rosters": int(e["rosters"]),
                      "schedule": after_minutes(start, at).astimezone(reg.tz).strftime("%a %H:%M"),
                      "cutoff_soft_hours": t0["cutoff_soft_hours"] + hours, "cutoff_hard_hours": t0["cutoff_hard_hours"] + hours,
                      "confirm_hours": t0.get("confirm_hours", t0["cutoff_hard_hours"]) + hours})
            chain.append((t, p))
        for i, (t, _p) in enumerate(chain):
            if i + 1 < len(chain):
                t["followed_by"] = chain[i + 1][0]["key"]
            else:
                t.pop("followed_by", None)
        if chain:
            t0["followed_by"] = chain[0][0]["key"]
            t0["lineup"] = [dict(e) for e in change.lineup]
        else:
            t0.pop("followed_by", None)
            t0.pop("lineup", None)
        t0.pop("follow", None)  # older code's one follow-on: the chain above is the run's lineup now
        t0["rosters"] = int(change.lineup[0]["rosters"])
        gone = {later[k].team for k in change.removed if k in later}
        reg.config.rosters = [t for t in reg.config.rosters if not (isinstance(t, dict) and t.get("key") in gone)]
        reg.save_config(f"run {first.key} {line}", notify=False)
        evs: list[RaidEvent] = []
        for t, p in chain:
            at = after_minutes(start, int(t["starts"]))
            if p is None:
                p = open_event(reg, rs, t, at)
            else:
                p.part, p.starts = int(t["part"]), int(t["starts"])
                place_run(reg, p, at)
            evs.append(p)
        for i, p in enumerate(evs):
            p.followed_by = evs[i + 1].key if i + 1 < len(evs) else None
            rs.save(p, f"part {p.part} of {first.key} after a lineup change")
        first.followed_by = evs[0].key if evs else None
        first.log.append(line)
        rs.save(first, line)  # last: its save copies its answers onto every later part, the new ones included
    return [first.key] + [p.key for p in evs]


def rosters_copy(reg: Registry, instance: str, sid: str, n: int, rs=None) -> str:
    """What changing a schedule's rosters per run to `n` does, in ONE sentence for the schedule wizard, the Raids page's
    editor (schedule_json `rosters_copy`) and describe_ops (dual interfaces §3.4 RD-10): ensure_run copies the schedule's
    rosters onto a run when it opens, so the change reaches the NEXT run and a run already open keeps what it opened with.
    'From the next run: the sheet advertises 2 × 25 seats. The run already open keeps its 1 × 25.'"""
    try:
        size = int(reg.schedule_def(instance, sid).get("size") or DEFAULT_RAID_SIZE)
    except RegistryError:  # a schedule that doesn't exist yet: the raid's size
        size = int(reg.raid_def(instance).get("size") or DEFAULT_RAID_SIZE)
    n = max(1, int(n or 1))
    text = f"From the next run: the sheet advertises {n} × {size} seats."
    try:
        live = reg.schedule_runs_open(sid, rs)
    except Exception:  # noqa: BLE001 — the first sentence is still true without the run store
        live = []
    have = sorted({int(t.get("rosters") or 1) for t in live})
    if len(live) == 1:
        text += f" The run already open keeps its {have[0]} × {int(live[0].get('size') or size)}."
    elif live:
        text += f" The {len(live)} runs already open keep theirs ({', '.join(f'{m} × {size}' for m in have)})."
    return text


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
        if ev.followed_by and not is_follow(ev):  # a first part: every later part's answers follow (never the other way round)
            sync_parts(self, ev)

    def live(self) -> list[RaidEvent]:
        return sorted([e for e in self.events.values() if e.state not in ("done", "cancelled")], key=lambda e: e.starts_at)

    def for_team(self, team: str) -> Optional[RaidEvent]:
        return next((e for e in self.live() if e.team == team), None)

    def by_message(self, message_id: int) -> Optional[RaidEvent]:
        return next((e for e in self.events.values() if e.message_id == message_id), None)


def relabel_runs(rs: RaidStore, uid: int, old: str, new: str, by: str = "") -> list[str]:
    """A character renamed (Registry.rename_character, design §5.2): every LIVE run (open or locked) that names `old`
    for member `uid` — their signup, seats (selected and benched), fill asks, callouts — now names `new`; each changed
    run saved once (the caller's store.batch makes it one commit with the member file). Finished and cancelled runs keep
    the name they were played under. Returns the run keys changed."""
    low = old.strip().lower()
    out = []
    for ev in rs.live():
        hits = []
        sg = ev.signups.get(str(uid))
        if sg is not None and sg.character.lower() == low:
            hits.append(sg)
        hits += [p for r in ev.rosters for p in r.selected + r.benched if (p.character or "").lower() == low and player_uid(ev, p) == uid]
        hits += [a for a in ev.fill_asks if a.discord_id == uid and a.character.lower() == low]
        hits += [co for co in ev.callouts if co.discord_id == uid and co.character.lower() == low]
        if not hits:
            continue
        for x in hits:
            x.character = new
        ev.log.append(f"{old} is now {new}" + (f" (by {by})" if by else ""))
        rs.save(ev, f"{old} is now {new}")
        out.append(ev.key)
    return out


def character_entries(ev: RaidEvent, uid: int, names: set[str]) -> list:
    """Member `uid`'s entries on run `ev` that carry a character answering to one of `names` (lower case): their
    signup, their seats (selected and benched) and their callouts. A character move carries them over (move_on_runs)."""
    out: list = []
    sg = ev.signups.get(str(uid))
    if sg is not None and sg.character.strip().lower() in names:
        out.append(sg)
    out += [p for r in ev.rosters for p in r.selected + r.benched if (p.character or "").strip().lower() in names and player_uid(ev, p) == uid]
    out += [co for co in ev.callouts if co.discord_id == uid and co.character.strip().lower() in names]
    return out


def on_run(ev: RaidEvent, uid: int) -> bool:
    """Whether member `uid` is on run `ev` at all: an answer, a seat, a callout or an open ask."""
    return (str(uid) in ev.signups or ev.seat_of(uid) is not None or any(co.discord_id == uid for co in ev.callouts)
            or any(a.discord_id == uid and a.pending for a in ev.fill_asks))


def move_on_runs(rs: RaidStore, uid: int, to, names: set[str], label: str, *, series_add=frozenset(), series_drop=frozenset(),
                 by: str = "") -> list[str]:
    """A character moved from member `uid` to member `to` (Registry.move_character, design §5.4): on every LIVE run
    (open or locked) its signup, seats and callouts change owner (the id and the display name), and with the signup
    the member's pins, board place, lock seat, follow-on leave and nudge; a run of a series in `series_add` lists
    `to` among its regulars, one in `series_drop` no longer lists `uid`. The caller refused a run where `to` is
    already on it (Registry.move_refusal) and any open ask. Each changed run saved once (the caller's store.batch makes
    it one commit); finished and cancelled runs keep who played. Returns the run keys changed."""
    out = []
    a, b = str(uid), str(to.discord_id)
    for ev in rs.live():
        hits = character_entries(ev, uid, names)
        changed = bool(hits)
        sg = ev.signups.get(a)
        if sg is not None and any(x is sg for x in hits):
            old_name = sg.display_name
            ev.signups = {(b if k == a else k): v for k, v in ev.signups.items()}
            if a in ev.pins:
                ev.pins = {(b if k == a else k): v for k, v in ev.pins.items()}
            if ev.layout:
                ev.layout = [[to.display_name if n == old_name else n for n in g] for g in ev.layout]
            for field in ("lock_seats", "leaving", "nudged"):
                ids = getattr(ev, field)
                if ids and uid in ids:
                    setattr(ev, field, [to.discord_id if i == uid else i for i in ids])
        for x in hits:
            if isinstance(x, Player):
                x.signup_name = to.display_name
            else:
                x.display_name = to.display_name
            x.discord_id = to.discord_id
        if ev.series and ev.series in series_add and to.discord_id not in ev.regulars:
            ev.regulars = list(ev.regulars) + [to.discord_id]
            changed = True
        if ev.series and ev.series in series_drop and uid in ev.regulars:
            ev.regulars = [i for i in ev.regulars if i != uid]
            changed = True
        if not changed:
            continue
        ev.log.append(f"{label} moved to {to.display_name}" + (f" (by {by})" if by else ""))
        rs.save(ev, f"{label} moved to {to.display_name}")
        out.append(ev.key)
    return out


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
    """The run file of run dict `team`, keyed with the dict's own key; its identity (`event`, `part`, `starts`,
    `occurrence`, `follows`) comes from the dict as ensure_run stamped it — nothing is read from or built out of a time.
    A file already there under that key is handed back (a closed one refused)."""
    key = team["key"]
    if key in rs.events:
        ev = rs.events[key]
        why = closed_run_refusal(rs, ev)
        if why:  # never hand a cancelled / ended run back as the one just opened
            raise RegistryError(why)
        return ev
    follows = None
    if team.get("follows"):  # the dict names the first part's dict key; the run carries the first part's RUN key
        lead = rs.for_team(team["follows"])
        follows = lead.key if lead is not None else team["follows"]
    ev = RaidEvent(key=key, team=key, instance=team.get("instance"), schedule=schedule_stamp(team.get("schedule_id")),
                   series=team.get("series"), kind=team.get("kind"), test=bool(team.get("test")), starts_at=starts_at.isoformat(),
                   follows=follows, occurrence=team.get("occurrence"), event=follows or team.get("event") or key,
                   part=int(team.get("part") or 0), starts=int(team.get("starts") or 0))
    if not ev.follows:  # a follow-on's answers come from its lead (sync_follow), absences included
        prefill(reg, ev, team)
    ev.log.append(f"opened ({run_kind(reg, ev)}); {len(ev.signups)} marked away from absences" if not ev.follows
                  else "opened as a follow-on: its answers are the lead's")
    rs.save(ev, "opened")
    return ev


STAMP_FIELDS = ("schedule", "occurrence", "event", "part", "starts")


def stamp_runs(reg: Registry, rs: "RaidStore", commit: bool = True) -> list[str]:
    """At the start (phase 3): every LIVE run file without an `event` gets its identity stamps, read from what it already
    is — `event` = its own key (a follow-on: its `follows`), `part` 1 for a follow-on else 0, `starts` = minutes after its
    lead's start, `schedule` = its run dict's schedule_id when that is a real schedule id (else None: adhoc, after, test),
    `occurrence` = its start in guild wall time when it has a schedule. Its run dict gets the same `occurrence` / `event`
    / `part` / `starts`. Only those keys are added to a run file (written from its own JSON, every other value as it
    was); finished and cancelled runs are never touched. A live run whose FILE carries the stamps but whose run dict
    lacks `event` (a start whose config save failed) heals: the dict takes the file's stamps, the file is not rewritten.
    Idempotent: nothing to stamp = nothing written. The writes are
    the caller's batch; `commit=False` (migrate_at_start) leaves the run dicts' save and the commit message to the caller,
    so the schedules lift and the stamps land in one commit saying each once. → the run keys stamped."""
    if getattr(reg, "_shadow", False):
        return []
    todo = [e for e in rs.live() if not e.event]
    # a run file stamped whose run dict is not (the start's config save failed): the dict takes the file's stamps
    heal = [(e, t) for e in rs.live() if e.event and (t := reg.config.roster(e.team)) is not None and not t.get("event")]
    if not todo and not heal:
        return []
    done, dicts = [], False
    for ev, team in heal:
        team.update({k: getattr(ev, k) for k in STAMP_FIELDS if k != "schedule"})
        dicts = True
        done.append(ev.key)
    for ev in todo:
        stamps = _run_stamps(reg, rs, ev)
        path = reg.store.root / ev.rel_path(rs.key)
        raw = json.loads(path.read_text()) if path.exists() else json.loads(ev.model_dump_json())
        raw.update(stamps)
        reg.store.write_text(ev.rel_path(rs.key), json.dumps(raw, indent=1, ensure_ascii=False))
        for k, v in stamps.items():
            setattr(ev, k, v)
        team = reg.config.roster(ev.team)
        if team is not None and not team.get("event"):
            team.update({k: stamps[k] for k in STAMP_FIELDS if k != "schedule"})
            dicts = True
        done.append(ev.key)
    if not commit:
        return done
    if dicts:
        reg.save_config(f"live runs stamped with their schedule and occurrence ({len(done)})", notify=False)
    reg.store.commit(f"{rs.key}: live runs stamped with their schedule and occurrence: {', '.join(done)}")
    return done


def _run_stamps(reg: Registry, rs: "RaidStore", ev: "RaidEvent") -> dict:
    """The identity stamps stamp_runs gives a run file from before them, read from what it already is (its start now)."""
    lead = rs.events.get(ev.follows) if ev.follows else None
    sid = schedule_stamp(run_schedule_id(reg, ev))
    return {"schedule": sid, "occurrence": occurrence_stamp(reg, ev.start) if sid else None,
            "event": ev.follows or ev.key, "part": 1 if ev.follows else 0,
            "starts": round((ev.start - lead.start).total_seconds() / 60) if lead is not None else 0}


def stamp_before_change(reg: Registry, rs: "RaidStore", parts: list["RaidEvent"]) -> list[str]:
    """A live run the start's stamping missed (stamp_runs failed) gets the same stamps IN MEMORY — the run and its run
    dict — just before a move or a lineup edit, so the caller's own batch writes them with the change. Without them the
    unstamped fallback would find the run by its NEW start and the scheduler would open its original night again.
    Computed from the starts as they are now, so call it before anything moves. → the run keys stamped."""
    if getattr(reg, "_shadow", False):
        return []
    todo = [p for p in parts if not p.event and p.state not in ("done", "cancelled")]
    stamps = {p.key: _run_stamps(reg, rs, p) for p in todo}  # all read before any is set: a follow-on reads its lead's start
    for p in todo:
        for k, v in stamps[p.key].items():
            setattr(p, k, v)
        team = reg.config.roster(p.team)
        if team is not None and not team.get("event"):
            team.update({k: stamps[p.key][k] for k in STAMP_FIELDS if k != "schedule"})
    return [p.key for p in todo]


def migrate_at_start(reg: Registry, rs: "RaidStore", by: str = "migration") -> list[str]:
    """The start's one save per guild (discord_bot._schedules_save, else _schedules_migrate): Registry.migrate_schedules
    and stamp_runs in ONE store.batch, so the schedule lift and the live runs' stamps land in one commit; a second start
    writes nothing. The live runs are stamped first (in memory and on their files, from the lifted config already in
    memory), then migrate_schedules saves guild.yaml once with ONE message naming both. → the schedules lift's lines."""
    with reg.store.batch():
        stamped = stamp_runs(reg, rs, commit=False)
        lines = reg.migrate_schedules(by, stamped=stamped)
    return lines


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


def sync_parts(rs: "RaidStore", first: RaidEvent) -> bool:
    """Every later part's signups ARE the first part's (design §5.62): copied from the first part every time it is
    saved, onto each LIVE later part on its own — the members leaving that part as out (source callout, LEAVING_NOTE):
    every out on that part alone (their own Leave, an officer's removal, a freed seat) is in its `leaving` — and a row
    of someone with no row on the first part (a pool member's fill yes on that part) kept. Pins, the board and the
    rosters are each part's own. Saves a part only when something changed; returns whether any did."""
    changed = False
    for fev in later_parts(rs, first):
        if fev.state in ("done", "cancelled"):
            continue
        want: dict[str, Signup] = {}
        for uid, s in first.signups.items():
            c = s.model_copy()
            if s.discord_id in fev.leaving and c.status != "out":
                c.status, c.source, c.note, c.joined_at = "out", "callout", LEAVING_NOTE, None
            want[uid] = c
        for uid, s in fev.signups.items():  # a row of the part's own (a pool member's yes to a fill on it) stays
            want.setdefault(uid, s)
        # the same answer pressed again only moves the first part's updated_at (the last touch): not a change to the part
        if {k: v.model_dump(exclude={"updated_at"}) for k, v in want.items()} == {k: v.model_dump(exclude={"updated_at"}) for k, v in fev.signups.items()}:
            continue
        fev.signups = want
        rs.save(fev, "answers follow the first raid")
        changed = True
    return changed


sync_follow = sync_parts  # the two-raid name (design §5.62)


# ---------------------------------------------------------------- who a run is for (roster redesign Phase 1)

def run_schedule_id(reg: Registry, ev: RaidEvent) -> str | None:
    """The id of the schedule a run came from today: its run dict's (the lift translated those), else its own stamp read
    through config.schedule_aliases — a run file the old `default` opened has none, a renamed duplicate the old id."""
    team = reg.config.team(ev.team) or {}
    if team.get("schedule_id"):
        return team["schedule_id"]  # already today's id: never read through the aliases again
    if ev.event:  # stamped (phase 3): its schedule id as it is, None for a one-off — never an alias
        return ev.schedule
    return reg.config.schedule_aliases.get(f"{ev.instance}:{ev.schedule or 'default'}", ev.schedule)


def run_kind(reg: Registry, ev: RaidEvent) -> str:
    """'recurring' | 'adhoc'. Stamped at open; a file from before classes reads as its schedule says (a pickup or a
    one-off → ad hoc; the raid's own run times or another weekly / lockout schedule → recurring)."""
    if ev.kind:
        return ev.kind
    team = reg.config.team(ev.team) or {}
    if team.get("kind"):
        return team["kind"]
    sid = run_schedule_id(reg, ev)
    if sid == ADHOC_SCHEDULE or (ev.event and not sid):
        return "adhoc"
    try:
        s = reg.schedule_entry(sid) if sid else None
    except RegistryError:
        s = None
    return schedule_class(s["kind"]) if s else "recurring"


def run_series(reg: Registry, ev: RaidEvent) -> str | None:
    """The recurring series a run belongs to (its schedule's id), None for an ad hoc run or a rehearsal. An older
    "<raid>:<schedule>" stamp is read through config.schedule_aliases; else the run dict's series, else its schedule id."""
    if run_kind(reg, ev) != "recurring" or not ev.instance or is_test_run(reg, ev):
        return None
    team = reg.config.team(ev.team) or {}
    key = ev.series or team.get("series") or team.get("schedule_id") or ev.schedule
    return reg.config.schedule_aliases.get(key, key) if key else None


def is_test_run(reg: Registry, ev: RaidEvent) -> bool:
    return ev.test or bool((reg.config.team(ev.team) or {}).get("test"))


def rehearsal_run(reg: Registry, ev: RaidEvent) -> bool:
    """is_test_run, and also a CLOSED rehearsal from before the `test` flag whose run dict is gone: its own schedule
    stamp is the rehearsal's (_REHEARSAL) or its roster key carries it ('…-test'). Lists of past runs read this."""
    return (is_test_run(reg, ev) or ev.schedule == _REHEARSAL or run_schedule_id(reg, ev) == _REHEARSAL
            or str(ev.team or "").endswith(f"-{_REHEARSAL}"))


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
        return OFF
    return "regulars" if run_regulars(reg, ev) else "everyone"


def not_answered(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    """The expected people with no answer on the sheet (an absence-marked No thanks counts as an answer), by name."""
    return sorted([(m, c) for m, c in expected(reg, ev) if c and str(m.discord_id) not in ev.signups], key=lambda mc: mc[0].display_name.lower())


def pool_unanswered(reg: Registry, ev: RaidEvent) -> list[tuple[Member, RegisteredCharacter]]:
    """Who the run is FOR (run_pool — never `expected`, the nudge's gate, which lists nobody on an ad hoc run) with no
    row on its sheet, by name, each with the character run_pool gives them (a regular's series character, else their
    main): the Events page's "No answer yet" and Answer-for-them rows, health's "no answer on the sheet" hint. A one-off
    lists every member's main without an answer; on a recurring run `expected` IS run_pool, so it equals not_answered.
    A member run_pool gives no character (no active main, and not a regular) is never listed — health's hint never names
    them, even when an alt of theirs plays the role."""
    return sorted([(m, c) for m, c in run_pool(reg, ev) if c and str(m.discord_id) not in ev.signups], key=lambda mc: mc[0].display_name.lower())


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
        return sid
    now = reg.now_local()  # whatever the distance (the final fix wave of §5.67 phase 4): a schedule's night beyond the
    # open horizon is still that schedule's night — open_refusal refuses opening it early, it never becomes a one-off
    hours = max(24 * OPEN_HORIZON_DAYS, (start - now).total_seconds() / 3600 + 1)
    hit = next((s["id"] for s, t in upcoming(reg, instance, now, hours) if t == start), None)
    return hit if hit is not None else ADHOC_SCHEDULE


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


def spec_role(reg: Registry, c: RegisteredCharacter, spec: str) -> str:
    """The role `c` plays as `spec` — its spec, its offspec, or the spec one of its declared flex roles means
    (spec_for_role) — else RegistryError("Nyx doesn't play Holy.")."""
    plays = {c.spec, c.offspec} | {spec_for_role(reg, c.cls, c.offspec, r) for r in (c.flex or [])}
    if spec not in plays - {None}:
        raise RegistryError(f"{c.label} doesn't play {spec}.")
    return reg.profile.spec(c.cls, spec).role


# a role switch for someone rostered on a locked run is an ask they answer (Switch on the event's Role switches,
# RaidMixin.send_plan), never a write over their seat: set_signup refuses `spec` there on every surface
SPEC_SEATED = "{name} holds a seat on the locked roster, so use Switch under the event's Role switches to ask them to play {spec}."


def set_signup(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, character: str | None, status: str, source: str = "member", note: str | None = None,
               *, spec: str | None = None, by: str | None = None) -> Signup:
    """THE answer write on a first part. `spec` (an officer's role switch): the spec the character plays tonight — its
    own, its offspec or a flex role's (spec_role, refused otherwise); the signup's spec and role follow, its own spec
    becomes the offspec (as a yes to a switch ask leaves it). Refused (SPEC_SEATED) once the run is locked and they hold
    a seat whose spec it would change: that switch is an ask. `by`: who switched them, for the run's log."""
    if ev.follows:  # D1: one Join for the night — the follow-on is never answered on its own
        lead = rs.events.get(ev.follows)
        raise RegistryError(FOLLOW_ANSWER_REFUSAL.format(lead=reg.raid_def(lead.instance if lead else None).get("name", "first raid")))
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    want = (character or "").strip().lower()
    c = (next((c for c in m.active() if c.label.lower() == want), None) or next((c for c in m.active() if (c.name or "").lower() == want), None)) if want else m.main
    if not c:
        raise ValueError("no such active character")
    # no curated-roster demotion here (roster redesign Phase 0): it keyed on the run key, which lock used to fill with
    # the seated, so a member's own Join after lock was stored as Bench and then seated anyway
    role = spec_role(reg, c, spec) if spec else None
    seat = ev.seat_of(m) if spec and ev.state != "open" else None
    if seat is not None and seat[1].spec != spec:
        raise RegistryError(SPEC_SEATED.format(name=m.display_name, spec=spec))
    s = carry_joined(ev.signups.get(str(m.discord_id)), _signup(reg, m, c, status, source, note))
    if spec and spec != c.spec:
        s.spec, s.offspec, s.role = spec, c.spec, role
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"{m.display_name} {status} as {c.label}" + (f", {s.spec}" if spec else "") + f" ({source}" + (f", switched by {by})" if by else ")"))
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
    # a follow-on is never touched directly: the absence marks its lead and the follow-on's answers follow it
    return [ev for ev in rs.live() if not ev.follows and start <= ev.start.astimezone(reg.tz).date().isoformat() <= end]


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
    if ev.follows:  # a follow-on: leaving after the lead (design §5.62); the lead's answer stands
        return leave_follow(reg, rs, ev, m, note)
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


def leave_follow(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, note: str | None = None) -> Callout:
    """Leaving after the lead: out of the follow-on alone (design §5.62). Records the callout on the
    follow-on, adds them to `leaving`, re-materialises their row as out; the caller frees a held seat
    (RaidMixin.call_out → drop_seated)."""
    s = ev.signups.get(str(m.discord_id))
    hours = (ev.start - datetime.now(ev.start.tzinfo)).total_seconds() / 3600
    co = Callout(discord_id=m.discord_id, display_name=m.display_name, character=s.character if s else (m.main.name if m.main else "?"),
                 at=now(), hours_before=round(hours, 1), note=note, late=ev.state != "open")
    ev.callouts.append(co)
    if m.discord_id not in ev.leaving:
        ev.leaving.append(m.discord_id)
    ev.log.append(f"{m.display_name} is {LEAVING_NOTE}")
    rs.save(ev, f"leaving: {m.display_name}")
    lead = lead_of(rs, ev)
    if lead is not None:
        sync_follow(rs, lead)
    return co


def stay_for_follow(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member) -> bool:
    """Undo leaving early: their lead answer stands on the follow-on again (not re-seated: fill may have taken the
    seat, and the fill engine offers one when short). False when they weren't leaving (a No thanks on the lead is the night)."""
    if m.discord_id not in ev.leaving:
        return False
    ev.leaving = [u for u in ev.leaving if u != m.discord_id]
    ev.log.append(f"{m.display_name} is staying for the follow-on")
    rs.save(ev, f"staying: {m.display_name}")
    lead = lead_of(rs, ev)
    if lead is not None:
        sync_follow(rs, lead)
    return True


# ---------------------------------------------------------------- filling gaps

def conflicts(rs: RaidStore, ev: RaidEvent, window_hours: float = CONFLICT_WINDOW_HOURS) -> dict[int, str]:
    """discord_id -> other raid key, for people In on another live raid within the window."""
    out: dict[int, str] = {}
    for other in rs.live():
        if other.key == ev.key:
            continue
        if same_event(other, ev):  # the raids of one run share their people (design §5.62)
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
    `classes` = what the class, spec, melee and ranged minimums × the rosters are short: shown on the run, never asked
    for (the fill engine reads `roles` only).
    {size, rosters, seats, have{role}, need{role}, roles{tank|healer: short}, classes{slot: short}, headcount (free seats), locked}"""
    size = run_size(reg, ev)
    locked = ev.state != "open" and bool(ev.all_rosters)
    n = len(ev.all_rosters) if locked else run_rosters(reg, ev)
    bounds = reg.role_bounds(ev.instance, size)
    players = ev.seated() if locked else ev.by_status("in")
    have = {r: sum(1 for p in players if p.role == r) for r in ROLES}
    need = {r: int(bounds[r]["min"]) * n if r in ("tank", "healer") else 0 for r in ROLES}
    short = {r: need[r] - have[r] for r in ("tank", "healer") if need[r] > have[r]}
    classes = {}
    for slot, k in reg.minimums(ev.instance, size).items():
        if slot in ("tank", "healer"):
            continue
        got = sum(1 for p in players if slot_holds(slot, p))
        if k * n > got:
            classes[slot] = k * n - got
    return {"size": size, "rosters": n, "seats": size * n, "have": have, "need": need, "roles": short, "classes": classes,
            "headcount": max(0, size * n - len(players)), "locked": locked}


def slot_holds(slot: str, p) -> bool:
    """Does a player (a seat or a signup: role, cls, spec) count toward a minimum's slot — a role, a class or Class:Spec?"""
    if slot in ROLES:
        return p.role == slot
    cls, _, spec = slot.partition(":")
    return p.cls == cls and (not spec or p.spec == spec)


def class_short_words(classes: dict[str, int]) -> str:
    """'1 Paladin, 2 Holy Priests' — a run's class shorts (gaps()['classes']) in words, '' when none."""
    return ", ".join(slot_words(slot, n) for slot, n in classes.items())


def needs(reg: Registry, ev: RaidEvent, team: dict | None = None) -> dict:
    """What the sheet is short (rc.gaps): headcount and per-role minimums. Before lock: Join signups vs the raid size
    times the rosters the run expects. After lock: freed seats across the locked roster(s)."""
    g = gaps(reg, ev)
    return {"headcount": g["headcount"], "roles": g["roles"], "size": g["size"], "classes": g["classes"]}


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

    off_ids = kept_off_ids(reg, rs, ev)  # an "out" pin, a drag onto Not placed, always_bench: never asked, never counted

    def ok(m: Member | None) -> bool:
        # DMs off is no reason to skip someone (D14): they are asked in the same order, the ask waits on their Me page
        # (channel "web", same deadline); fill_batch adds a reachable twin when the hours before start run short
        return bool(m) and m.discord_id not in asked and not m.absent_on(day) and m.discord_id not in busy and m.discord_id not in off_ids

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
            if sg.discord_id in off_ids:  # kept off: not coming as far as the lock is concerned
                continue
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
                return "flex", p["character"], sp, f"{first} can also play {sp}"
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


def pick_asks(plan: Plan, picks: dict | None = None, limit: int | None = None, seats: list[int] | None = None) -> list[FillAsk]:
    """The asks an officer sends: each seat's suggestion, or the alternate they picked (`picks` = {seat index: option
    index}, 0 = the suggestion). `limit`: only the first that many seats someone can cover (a card's Ask sends the
    seats the card draws, raid_views.SUGGEST_LINES). `seats`: only those seat indexes (the Events page's Switch on one
    row). Refuses a pick that asks one person for two seats."""
    out: list[FillAsk] = []
    seen: set[int] = set()
    shown = 0
    only = {int(i) for i in seats} if seats is not None else None
    for i, st in enumerate(plan.seats):
        if not st.options or (only is not None and i not in only):
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


def send_batch(plan: Plan, picks: dict | None = None, limit: int | None = None, seats: list[int] | None = None) -> tuple[list[FillAsk], int]:
    """Exactly what an officer's send of this plan (with `picks`, pick_asks) DMs now, and how many of the picked asks
    wait: before lock all of them; after lock at most `plan.room` (FILL_MAX_OPEN out at once), never a swap without
    its tied backfill. The preview screens (the wizard, the plain-text diff, the site and MCP) show this list and
    RaidMixin.send_plan sends it — the cap is part of the plan's rev, so what was shown is what goes. RegistryError
    when nothing can go out now. `limit`: only the first that many seats (pick_asks — a card's Ask); `seats`: only those
    seat indexes (pick_asks — the Events page's Switch)."""
    asks = pick_asks(plan, picks, limit, seats)
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


# ---------------------------------------------------------------- role switches for the whole event (phase 4, §7a)

def switch_raids(rs: "RaidStore", first: RaidEvent, m: Member) -> list[RaidEvent]:
    """The raids a role switch before the lock changes for `m`: every open raid of the event (the answer is one for the
    evening, sync_parts copies it on) but the later ones they leave before (`leaving`: out there whatever they answer)."""
    return [p for p in event_parts(rs, first) if p.state == "open" and m.discord_id not in p.leaving]


def switch_box_for(reg: Registry, rs: "RaidStore", part: RaidEvent, m: Member, character: str, spec: str, *, plan_rev: str = "",
                   viewer: int | None = None, swap: bool = True) -> tuple[dict, str]:
    """(box, button word) for one Role switches row — the /switch route re-builds the box for `seen`. Before the lock
    the answer is one for the evening, so the box names the raids the write changes for THAT member (switch_raids):
    someone who joined → Switch (consequences.switch_box, a notice); the officer's own row → Switch said to them
    (switch_box own: nobody told); anyone else → Answer for them (answer_box: Join as that spec). After the lock it is
    an ask on `part` alone → Ask (switch_box locked, its rev the part's plan rev). Button words: PAGE_WORDS."""
    from . import consequences as cq
    from .constants import PAGE_WORDS

    first = first_part(rs, part)
    if first.state == "open":
        rev = event_rev(reg, rs, first) + member_state_rev(first, m)
        sg = first.signups.get(str(m.discord_id))
        raids = [p.instance for p in switch_raids(rs, first, m)]
        if viewer is not None and int(viewer) == m.discord_id:
            return cq.switch_box(reg, first, m, character, spec, raids, own=True, rev=rev), PAGE_WORDS["switch_button"]
        if sg is None or sg.status != "in":
            return cq.answer_box(reg, first, m, "join", character, spec, by_id=viewer, rev=rev), PAGE_WORDS["answer_button"]
        return cq.switch_box(reg, first, m, character, spec, raids, rev=rev), PAGE_WORDS["switch_button"]
    box = cq.switch_box(reg, part, m, character, spec, [part.instance], locked=True, swap=swap, rev=plan_rev + member_state_rev(part, m))
    return box, PAGE_WORDS["ask_button"]


def _plays_words(character: str, spec: str, base: str | None) -> str:
    """'Holy', or 'Nyxbear, Guardian' when the character is not the one they answered with (or their main)."""
    return spec if character == base else f"{character}, {spec}"


def _short_why(plan: "Plan", short: dict, prelock: bool) -> str:
    """Why a short raid has no switch to show, and what to do (PAGE_WORDS): its only moves need a cover (the Suggest… /
    Fill seats… screen has them), the asks already out cover it, the regulars who haven't answered are counted as
    coming (before the lock), or nobody free can play the role."""
    from .constants import PAGE_WORDS
    from .raid_buttons import RUN_WORDS

    if any(st.role and st.options and all(len(o) != 1 for o in st.options) for st in plan.seats):
        return PAGE_WORDS["short_cover"].format(screen=RUN_WORDS["suggest" if prelock else "fill"])
    covered = not plan.short["roles"]  # every short seat is covered by the asks out or the regulars counted as coming
    if covered and plan.waiting:
        return PAGE_WORDS["short_asked"]
    if covered and prelock and plan.expected:
        return PAGE_WORDS["short_counted"]
    return PAGE_WORDS["short_nobody"]


def event_switches(reg: Registry, rs: "RaidStore", first: RaidEvent, *, viewer: int | None = None) -> dict:
    """The Events page's Role switches: {short [{raid, roles {role: n}, why}] (each live raid short of a tank / healer
    minimum, rc.needs — the Board's own numbers; `why` = the sentence when that raid has no row, else None), rows
    [Switch], revs {part key: plan rev}}. Composed from each live raid's planner (role_plan: suggestions before the
    lock, the fill plan after it) — no second ranking. A row is one option of a role seat (a plain seat after the lock
    is the fill engine's, not a role switch; a swap tied to a backfill is two asks and is left to Suggest… / Fill
    seats…): {uid, display_name, role, from, to, answered, button, raids, part, seat, option, character, spec, box}.
    Before the lock one answer serves the whole evening, so a person's same move on several raids is ONE row (`raids` =
    every raid it helps; part/seat/option = the first); after the lock each ask is a raid's own (sent on that raid
    alone), so a row is per raid, and the officer's own row is left out (it would ask themselves). Ordered by role, then
    suggestions before alternates. `from` / `to`: 'Shadow', 'Nyx, Shadow' when the character changes, PAGE_WORDS
    no_answer_yet (or Bench) without a Join. `answered` = they joined; `button` = the press's word from the server:
    Switch (a joined member's answer, or the officer's own, before the lock) · Answer for them (anyone else before the
    lock) · Ask (after the lock)."""
    from .constants import PAGE_WORDS

    first = first_part(rs, first)
    out: dict = {"short": [], "rows": [], "revs": {}}
    if first.state not in ("open", "locked"):
        return out
    prelock = first.state == "open"
    parts = [p for p in event_parts(rs, first) if p.state in ("open", "locked")]
    rows: dict[tuple, dict] = {}
    for pi, p in enumerate(parts):
        name = _raid_name(reg, p.instance)
        short = dict(needs(reg, p)["roles"])
        plan = role_plan(reg, rs, p)
        out["revs"][p.key] = plan.rev
        has_row = False
        for si, st in enumerate(plan.seats):
            if not st.role:
                continue
            for oi, opt in enumerate(st.options):
                if len(opt) != 1:  # a swap tied to its backfill: two people asked together — Suggest… / Fill seats…
                    continue
                a = opt[0]
                if not prelock and viewer is not None and a.discord_id == int(viewer):
                    continue  # after the lock a switch is an ask: never one to the officer pressing it
                k = (a.discord_id, a.character, a.spec) + (() if prelock else (p.key,))
                if k in rows:
                    has_row = True
                    if name not in rows[k]["raids"]:
                        rows[k]["raids"].append(name)
                    continue
                m = reg.members.get(a.discord_id)
                if m is None:
                    continue
                sg = first.signups.get(str(m.discord_id))
                seat = next((x for x in p.seated() if player_uid(p, x) == m.discord_id), None) if not prelock else None
                joined = sg is not None and sg.status == "in"
                if seat is not None:
                    base = seat.character or a.character
                    frm = _plays_words(base, seat.spec, a.character)
                elif joined:
                    base = sg.character
                    frm = _plays_words(sg.character, sg.spec, a.character)
                else:
                    base = m.main.label if m.main else a.character
                    frm = LABELS["sub"] if sg is not None and sg.status == "sub" else PAGE_WORDS["no_answer_yet"]
                box, button = switch_box_for(reg, rs, p, m, a.character, a.spec, plan_rev=plan.rev, viewer=viewer, swap=a.swap)
                has_row = True
                rows[k] = {"uid": str(m.discord_id), "display_name": m.display_name, "role": a.role, "from": frm,
                           "to": _plays_words(a.character, a.spec, base), "answered": joined, "button": button, "raids": [name],
                           "part": p.key, "seat": si, "option": oi, "character": a.character, "spec": a.spec, "box": box,
                           "_rank": (ROLES.index(a.role) if a.role in ROLES else len(ROLES), oi, pi, si)}
        if short:
            out["short"].append({"raid": name, "roles": short, "why": None if has_row else _short_why(plan, short, prelock)})
    out["rows"] = sorted(rows.values(), key=lambda r: r["_rank"])
    for r in out["rows"]:
        del r["_rank"]
    return out


# THE words for how an ask (or a would-be cover) reaches someone (FillAsk.kind): the fill screens on both surfaces, the
# thread lines, the health hints and the site's fill modal and Detail (meta.ask_kinds) — design §5.56 phase 2
ASK_HOW = {"sub": "from the bench", "regular": "a regular, no answer yet", "unanswered": "no answer yet", "other": "not a regular",
           "flex": "extra role", "offspec": "offspec", "alt": "on an alt"}
# what a role's cover DOES, for "Could tank: …" (the owner's review, 2026-11-22: "declared flex: Rhoz" never said what for)
COULD = {"tank": "tank", "healer": "heal", "melee": "play melee", "ranged": "play ranged"}


def could_line(roles: list[str], groups: list[tuple[list[str], str]]) -> str:
    """'Could tank: Rhoz (extra role) · Trun (on an alt)' — who could cover the short role(s), each group with how
    (ASK_HOW's words). One sentence-shaped line that names the role itself: no icon stands in for it."""
    return f"Could {' or '.join(COULD.get(r, r) for r in roles)}: " + " · ".join(f"{', '.join(names)} ({how})" for names, how in groups)


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
                reg.notify(a.discord_id, "ask_closed", f"The seat question for {run_words(reg, ev)} closed at {reg.local12(t)} without your answer.", run=ev.key)
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
        # who could cover, in the fill screens' words (could_line + ASK_HOW: "Could tank: A, B (from the bench) · C (offspec)")
        # — never a code key; `off` = members with no answer on the sheet who play the role, only when nobody on it could
        hint, off = "", []
        if n and have < n:
            if coverable:
                hint = could_line([r], [([x.split(" (")[0] for x in v[:3]], ASK_HOW.get(k, k)) for k, v in cover.items() if v])
            else:
                off = [m.display_name for m, _c in pool_unanswered(reg, ev) if any(profile.spec(c.cls, c.spec).role == r for c in m.active())][:3]
                hint = could_line([r], [(off, ASK_HOW["unanswered"])]) if off else f"Nobody registered could {COULD.get(r, r)}. Recruit one."
        roles.append({"role": r, "have": have, "need": n, "level": level, "hint": hint, "off": off, "cover": cover})
    buffs = []
    for b in profile.party_buffs():
        providers = [s.display_name for s in ins if b.provided_by(profile.spec(s.cls, s.spec))]
        buffs.append({"id": b.id, "abbr": b.abbr, "colour": b.colour, "name": b.short, "providers": providers})
    unresp = [m.display_name for m, _c in not_answered(reg, ev)]  # the expected (regulars) who haven't answered; ad hoc: nobody
    hc_level = "green" if len(ins) >= size else ("amber" if len(ins) + len(subs) >= size else "red")
    # headcount is a 4-tuple (in, size, 0 [was tentative], sub): the cards and the pool data share the shape
    return {"headcount": (len(ins), size, 0, len(subs)), "headcount_level": hc_level, "roles": roles, "buffs": buffs, "unresponsive": unresp, "rosters": n_rosters,
            "expected_from": expected_from(reg, ev), "kind": run_kind(reg, ev)}


def cover_hints(roles: list[dict]) -> list[dict]:
    """The short roles' "who could cover" lines (health_data's per-role `hint`), with the roles that only members with NO
    answer on the sheet could cover said once: [{roles: [role, …], hint}] — "Could tank or heal: A, B, C (no answer
    yet)"; the same names under each short role read as one line twice on the health card and the site. Code builds
    it; the surfaces print it."""
    out, off_roles, off_names = [], [], []
    for r in roles:
        if not (r.get("need") and r["have"] < r["need"] and r.get("hint")):
            continue
        if r.get("off"):
            off_roles.append(r["role"])
            off_names += [n for n in r["off"] if n not in off_names]
        else:
            out.append({"roles": [r["role"]], "hint": r["hint"]})
    if off_roles:
        out.append({"roles": off_roles, "hint": could_line(off_roles, [(off_names[:5], ASK_HOW["unanswered"])])})
    return out


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
    """The run's signups as solver Players (first come = when the Join began), each carrying its member's attendance
    from run history (Registry.attendance over the loot policy's window, every character rolled up by Discord id;
    design §3.1 decision 10, D49) — left 0/0 = unknown (the solver weighs it neutral) while the window holds fewer than
    ATTENDANCE_MIN_RUNS real runs, or for a member with none counted."""
    players = []
    tracked = getattr(reg, "attendance_tracked", None)
    known = bool(tracked and tracked()["known"])
    seen: dict = {}

    def attendance_of(uid) -> tuple[int, int]:
        if not known or uid is None:
            return 0, 0
        if uid not in seen:
            seen[uid] = reg.attendance(uid)
        return seen[uid]
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
        attended, total = attendance_of(s.discord_id)
        players.append(Player(signup_name=s.display_name, discord_id=s.discord_id, pos=i + 1, status="signed" if s.status == "in" else "bench", cls=s.cls, spec=s.spec, role=s.role, offspec=s.offspec, character=s.character, map_confidence="high", unmapped=False, rank=rank, tank_capable_main=tank_alt,
                              attended=attended, attendance_total=total))
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
    schedule declares up front when that is more — never more than the tank/healer minimums per run allow, EXCEPT
    (the owner, 2026-11-22) that a declared roster is built as soon as the joiners no longer fit in fewer: 18 joiners
    of a 10-player ×2 run make two rosters even with tanks for one, and the short seats stay open to fill (the
    solver caps each roster's minimum at what the joiners supply)."""
    signed = [p for p in players if p.status == "signed"]
    n = min(MAX_ROSTERS_PER_SLOT, max(len(signed) // size, want))
    for role in ("tank", "healer"):
        if rb[role]["min"]:
            n = min(n, _capable(signed, role, reg) // rb[role]["min"])
    declared = min(MAX_ROSTERS_PER_SLOT, want, -(-len(signed) // size))
    return max(1, n, declared)


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
    return f"Leftovers, Bench and everyone else who could play ({len(any_role)}) are enough for another run{how}."


EXTRA_ROLES = ("tank", "healer")  # a role past its minimum waits for everyone else (no role has an upper limit — D16)


def first_come_out(players: list[Player], seats: int, role_min: dict[str, int], fixed: set[str]) -> set[str]:
    """Ad hoc seating is first come, first rostered (D4): the joiners in signup order take the seats, except that a seat
    still needed for a tank/healer minimum is kept for the first later joiner who plays that role, and a tank or healer
    joiner whose role already meets its minimum waits until every other joiner has a seat, then takes what is left in
    signup order. `fixed` (pinned in / placed on the board) are seated first. Returns the joiners who come too late —
    the lock benches them (hard), the solver only arranges the groups of the rest."""
    signed = sorted((p for p in players if p.status == "signed"), key=lambda p: p.pos)
    have = {r: 0 for r in set(role_min) | set(EXTRA_ROLES)}
    taken: list[str] = []
    for p in signed:
        if p.signup_name in fixed:
            taken.append(p.signup_name)
            if p.role in have:
                have[p.role] += 1
    out: set[str] = set()
    waiting: list[Player] = []
    rest = [p for p in signed if p.signup_name not in fixed]
    for i, p in enumerate(rest):
        free = seats - len(taken)
        if free <= 0:
            out.add(p.signup_name)
            continue
        fills = p.role in role_min and have.get(p.role, 0) < role_min[p.role]
        if p.role in EXTRA_ROLES and not fills:
            waiting.append(p)  # past its role's minimum: seated only with a seat nobody else takes
            continue
        later = rest[i + 1:]
        reserved = sum(min(max(0, n - have.get(r, 0)), sum(1 for q in later if q.role == r)) for r, n in role_min.items())
        if not fills and free <= reserved:
            out.add(p.signup_name)
            continue
        taken.append(p.signup_name)
        if p.role in have:
            have[p.role] += 1
    for p in waiting:
        if len(taken) < seats:
            taken.append(p.signup_name)
        else:
            out.add(p.signup_name)
    return out


def merged_role_min(rb: dict, policy: dict[str, int] | None) -> dict[str, int]:
    """The lock's role minimums: the raid's tank/healer minimums (role_bounds — a 0 the raid set is kept, so the game's
    own doesn't come back in the solver) with the comp policy's role_min laid over them PER ROLE — a policy line about
    healers never drops the tank minimum."""
    return {**{r: int(rb[r]["min"]) for r in ("tank", "healer") if r in rb}, **{r: int(n) for r, n in (policy or {}).items()}}


def run_strategy(reg: Registry, ev: RaidEvent) -> str:
    """THE split strategy a run's lock uses: the run's own, else its roster's split_policy, else the raid's, else
    balanced. The lock (_solve_run) and its preview (RaidMixin.lock_preview) both read it, so they solve alike."""
    raid_id = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
    return (ev.split_strategy or (reg.config.team(ev.team) or {}).get("split_policy") or reg.raid_def(raid_id).get("split_policy")
            or "balanced")


class LockForces(BaseModel):
    """Who the lock keeps on and off the roster, as _solve_run hands them to the solver (THE reader: the lock box's
    count, the planner's candidates and the all-kept-off refusal read the same lists, so they never drift).
    `kept_off` = an "out" pin or the comp policy's always_bench, never a name placed on the board (the board place
    wins); `kept_on` = an "in" pin, a board place or the policy's never_bench, never an "out" pin. Signup names, in the
    order the solve adds them (pins first, then policy lines), duplicates as the solve sees them."""

    pool: list[str]
    placed: dict[str, int]
    kept_off: list[str]
    kept_on: list[str]
    never_bench: list[str]
    always_bench: list[str]


def lock_forces(reg: Registry, rs, ev: RaidEvent, players: list[Player] | None = None) -> LockForces:
    """See LockForces. `rs` = the run store (or the board's frozen view; None: the guild's own files)."""
    from .policy import PolicyStore

    players = players if players is not None else players_for(reg, ev)
    cc = PolicyStore(rs.store if rs is not None else reg.store, rs.key if rs is not None else reg.key).comp_constraints()
    names = {p.character: p.signup_name for p in players} | {p.signup_name: p.signup_name for p in players}
    resolve = lambda n: names.get(n) or next((v for k, v in names.items() if k.lower() == n.lower()), None)  # noqa: E731
    by_uid = {str(sg.discord_id): sg.display_name for sg in ev.signups.values()}
    pin_in = [by_uid[u] for u, v in ev.pins.items() if v == "in" and u in by_uid]
    pin_out = [by_uid[u] for u, v in ev.pins.items() if v == "out" and u in by_uid]
    layout = [[n for n in g if n in names] for g in (ev.layout or [])]
    placed = {n: gi for gi, g in enumerate(layout) for n in g}
    pool = [p.signup_name for p in players]
    inpool = set(pool)
    always = [x for x in (resolve(n) for n in cc["always_bench"]) if x and x in inpool]
    never = [x for x in (resolve(n) for n in cc["never_bench"]) if x and x in inpool]
    return LockForces(pool=pool, placed=placed,
                      kept_off=[x for x in pin_out + always if x in inpool and x not in placed],
                      kept_on=[x for x in pin_in + list(placed) + never if x in inpool and x not in pin_out],
                      never_bench=never, always_bench=always)


ALL_KEPT_OFF = "everyone who joined is kept off"  # the solver-side reason (no roster: nobody who joined may play)


def kept_off_ids(reg: Registry, rs, ev: RaidEvent) -> set[int]:
    """The members the lock keeps off this run (lock_forces' kept_off, as ids) plus anyone the comp policy's
    always_bench names by a character or display name: the planner never asks them (a pin "out" or a drag onto Not
    placed means "not this run")."""
    from .policy import PolicyStore

    f = lock_forces(reg, rs, ev)
    out = {sg.discord_id for sg in ev.signups.values() if sg.display_name in set(f.kept_off)}
    always = {str(n).lower() for n in PolicyStore(rs.store if rs is not None else reg.store, rs.key if rs is not None else reg.key).comp_constraints()["always_bench"]}
    if always:
        placed = set(f.placed)
        out |= {m.discord_id for m in reg.members.values()
                if (m.display_name.lower() in always or any(c.label.lower() in always for c in m.characters))
                and not (str(m.discord_id) in ev.signups and ev.signups[str(m.discord_id)].display_name in placed)}
    return out


def _solve_run(reg: Registry, rs: RaidStore, ev: RaidEvent, strategy: str | None = None, avoid: list[dict[str, int]] | None = None, time_limit: float = SOLVE_TIME_LIMIT_S, whatif: bool = True, avoid_groups: list[dict[str, int]] | None = None,
               earlier: dict[int, int] | None = None, stop=None) -> tuple[list[Player], list[RosterResult], int]:
    """The joint solve behind propose(), autofill() and split previews: as many rosters as the signups support,
    the officers' layout as hard pins, weights as seat bonuses, the strategy shaping the objective.
    `earlier` (a later raid only): Discord id → how many raids before it rostered them, given instead of read from the
    earlier raids' locked rosters — the board preview (event_board_preview) hands it the earlier PREVIEWS, which are
    what those raids' locks will roster, so a later raid's preview is what its lock builds."""
    players = players_for(reg, ev)
    raid_id = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
    rd = reg.raid_def(raid_id)
    size = run_size(reg, ev)
    rb = reg.role_bounds(raid_id, size)
    from .policy import PolicyStore

    cc = PolicyStore(rs.store, rs.key).comp_constraints()
    names = {p.character: p.signup_name for p in players} | {p.signup_name: p.signup_name for p in players}
    resolve = lambda n: names.get(n) or next((v for k, v in names.items() if k.lower() == n.lower()), None)  # noqa: E731
    forces = lock_forces(reg, rs, ev, players)  # THE kept on / kept off lists (the lock box and the planner read them too)
    joined = {p.signup_name for p in players if p.status == "signed"}
    if joined and joined <= set(forces.kept_off):  # nobody who joined may play: never an empty roster
        raise RuntimeError(f"no roster found: {ALL_KEPT_OFF}")
    k = groups_per_roster(reg, size)
    layout = [[n for n in g if n in names] for g in (ev.layout or [])]
    placed = forces.placed
    used = max((gi for gi, g in enumerate(layout) if g), default=-1) + 1  # groups up to the last one holding someone: empty rosters drawn on the board force nothing
    n_rosters = max(how_many_rosters(reg, players, size, rb, want=run_rosters(reg, ev)), -(-used // k) if used else 1)
    strategy = strategy or run_strategy(reg, ev)
    bonus = seat_bonus(reg, rs, ev, players)
    lead_rostered: tuple[str, ...] = ()
    if is_follow(ev):  # a later raid: the people rostered on the raids before it come first — its splits are theirs (design §5.62)
        times: dict[int, int] = {}  # discord id → how many earlier raids rostered them
        if earlier is not None:
            times = dict(earlier)
        else:  # (never `k`: that is the groups per roster the board pins below are cut with)
            no = part_no(ev)
            for p in [p for p in event_parts(rs, ev) if part_no(p) < no and p.all_rosters]:
                for uid in {player_uid(p, x) for x in p.seated()} - {None}:
                    times[uid] = times.get(uid, 0) + 1
        # every one of them gets LEAD_SEAT_BONUS (and the solver never charges them as an extra tank or healer); someone
        # rostered on more of the earlier raids gets it once per raid, so a later raid keeps the night's longest players
        on_earlier = {p.signup_name: times[u] for p in players if (u := player_uid(ev, p)) in times}
        lead_rostered = tuple(on_earlier)
        bonus = {**bonus, **{n: int(bonus.get(n, 0)) + LEAD_SEAT_BONUS * c for n, c in on_earlier.items()}}
    roster_bonus = None
    if strategy == "rotation":
        w = int(rd["weights"].get("sat_out", 2))
        missed_first = sat_out_last_window(reg, rs, ev, first_only=True)
        roster_bonus = {0: {p.signup_name: 3 * w for p in players if player_uid(ev, p) in missed_first}}
    pool = {p.signup_name for p in players}
    role_min = merged_role_min(rb, cc["role_min"])
    minimums = {**reg.minimums(raid_id, size), **role_min}  # every class, spec, melee or ranged minimum too (D16)
    late: set[str] = set()
    benched = set(forces.kept_off)
    kept = set(forces.kept_on)
    if run_kind(reg, ev) == "adhoc" and not is_follow(ev):  # first come, first rostered (a follow-on: the lead's roster comes first instead): the late joiners are benched before the solve
        # a joiner benched by a pin / always_bench takes no first-come seat, and a never_bench joiner keeps one —
        # otherwise the same name lands in force_in and force_out, or a seat goes unfilled (INFEASIBLE)
        late = first_come_out([p for p in players if p.signup_name not in benched], size * n_rosters,
                              {r: n * n_rosters for r, n in role_min.items() if r in EXTRA_ROLES}, kept)
    opts = solver.SolveOptions(
        time_limit_s=time_limit, raid_size=size,
        keep_together=tuple((resolve(a), resolve(b)) for a, b in cc["keep_together"] if resolve(a) and resolve(b)),
        keep_apart=tuple((resolve(a), resolve(b)) for a, b in cc["keep_apart"] if resolve(a) and resolve(b)),
        force_in=tuple(forces.kept_on),
        force_out=tuple(forces.kept_off + [x for x in sorted(late - kept) if x in pool and x not in placed]),
        pins={n: gi for n, gi in placed.items() if gi < k * n_rosters} or None,
        prefer_group={resolve(n): g for n, g in cc["prefer_group"].items() if resolve(n)},
        minimums=minimums or None,
        lead_rostered=lead_rostered,
        bonus=bonus,
        kept_off=tuple(forces.kept_off),
        stop=stop,  # the board's picture only (solver.Stop): a lock that starts abandons it; never set by the lock itself
    )
    # "another one": for a split the rosters must differ, for a single run the groups must
    rosters = solver.solve_rosters(reg.profile, players, raid_id, opts, rosters=n_rosters, strategy=strategy, roster_bonus=roster_bonus, avoid=avoid if n_rosters > 1 else None, avoid_groups=avoid_groups if n_rosters == 1 else None)
    # bench what-ifs re-solve the model per benched player (the bench hangs off rosters[0]): capped by explain.WHATIF_*,
    # only at lock (never for previews), skipped for big splits / sheets
    do_whatif = whatif and explain.whatif_allowed(n_rosters, len(players))
    tank_min = (opts.minimums or {}).get("tank", 0)
    rosters = [explain.annotate(reg.profile, players, raid_id, r, whatif=do_whatif and i == 0, tank_min=tank_min, opts=opts) for i, r in enumerate(rosters)]
    return players, rosters, n_rosters


def propose(reg: Registry, rs: RaidStore, ev: RaidEvent, save: bool = True) -> tuple[list[Player], RosterResult]:
    """Roster(s) for a sheet from its signups: one joint solve over as many runs as the Join answers support,
    weights + officer pins + the officers' board, the raid's minimums, the comp policy, the run's split strategy."""
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


SOLVER_WORDS = {  # what blocks a roster, in one or two sentences an officer can act on (minimums are preferences: never one)
    "infeasible": "No roster fits the board, the pins and the comp rules together. Move names off the board, clear a pin or "
                  "loosen a comp rule, then lock again.",
    "time": "The bot couldn't build the roster in time. Lock again, or place fewer people by hand.",
    "other": "The bot couldn't build the roster. Lock again in a moment.",
    "changing": "The answers or pins kept changing while the roster was being built. Lock again in a moment.",
    "all_off": "Everyone who joined is kept off the roster, so move someone back from Not placed before you lock.",
    "no_other": "There is no other split that fits the rules. Use one already shown.",
}
LOCK_AGAIN = ", then lock again."  # every conflict_sentence ends so (how a stored one is known when it is read again)
# lock_error sentences stored before SOLVER_WORDS (still on old run files): shown as they are
_OLD_LOCK_WORDS = ("no roster satisfies the rules", "the bot couldn't find a roster in time", "the answers or pins kept changing",
                   "building the roster failed")


_OLD_LOCK_KINDS = dict(zip(_OLD_LOCK_WORDS, ("infeasible", "time", "changing", "other")))


def lock_failure_kind(text: str | None) -> str | None:
    """What kind of failure a stored lock_error is: a SOLVER_WORDS key, the same key for a sentence stored before
    SOLVER_WORDS (_OLD_LOCK_WORDS), else the sentence itself (a conflict names its people: another conflict is another
    failure). The lock's "first time this failed" test reads it, so the same failure in new words after an upgrade posts
    no second run-update line. None for no failure."""
    if not text:
        return None
    for k, words in SOLVER_WORDS.items():
        if text == words:
            return k
    return next((kind for old, kind in _OLD_LOCK_KINDS.items() if text.startswith(old)), text)


def conflict_sentence(e: "solver.Conflict") -> str:
    """A conflict the solver refused before searching, naming who and where (member names are data: the Discord lines
    that carry it escape it)."""
    who = join_words(list(e.names))
    where = f"Group {e.group}" + (f" of roster {e.roster}" if e.roster else "")
    if e.reason == solver.KEPT_ON:
        return (f"{e.have} people are kept on the roster, and it has {e.seats} seats. Clear a pin, a board place or a comp "
                "policy line that keeps someone on, then lock again.")
    if e.reason == solver.KEPT_BOTH:
        one = len(e.names) == 1
        return (f"{who} {'is' if one else 'are'} kept on the roster by a pin or the comp policy, and kept off it by the comp "
                f"policy. Clear one of the two, then lock again.")
    if e.reason == solver.GROUP_FULL:
        return f"{where} on the board holds {e.have} people and has room for {e.seats}. Move one of {who} to another group, then lock again."
    if e.reason == solver.GROUP_ROLES:
        role = {"healer": "healers", "tank": "tanks"}.get(e.role or "", "people of one role")
        return f"{where} on the board holds more {role} than one group may have: {e.seats}. Move one of {who} to another group, then lock again."
    return SOLVER_WORDS["infeasible"]


def solver_error_text(e: Exception) -> str:
    """What blocked a roster, in sentences an officer can act on (the lock's run-update line, its ops warn, the
    lock_error the site and plain text show, Preview lock, the board's failed state): a conflict the solver named
    before searching (conflict_sentence, with the people), everyone who joined kept off, no other split, a TIMEOUT (the
    solver's "no roster found: UNKNOWN" — matched before the infeasible case), an infeasible model, anything else.
    A sentence already made here (a stored lock_error passed through again, also one stored before SOLVER_WORDS)
    reads as itself; an exception's own words never reach a person."""
    if isinstance(e, solver.Conflict):
        return conflict_sentence(e)
    msg = str(e)
    if msg in SOLVER_WORDS.values() or msg.startswith(_OLD_LOCK_WORDS):
        return msg
    if type(e) is RuntimeError and msg.endswith(LOCK_AGAIN) and not msg.startswith("no roster found"):
        return msg  # a sentence of ours (a stored conflict_sentence, which names people) read again
    if ALL_KEPT_OFF in msg:
        return SOLVER_WORDS["all_off"]
    if NO_OTHER in msg:
        return SOLVER_WORDS["no_other"]
    if "UNKNOWN" in msg or "time limit" in msg.lower() or "timed out" in msg.lower():
        return SOLVER_WORDS["time"]
    if "INFEASIBLE" in msg or "no roster found" in msg:
        return SOLVER_WORDS["infeasible"]
    return SOLVER_WORDS["other"]


NO_OTHER = "no other split"  # "another one" asked for a roster the earlier ones exclude, and none is left


def split_preview(reg: Registry, rs: RaidStore, ev: RaidEvent, strategy: str, avoid: list[list[list[str]]] | None = None,
                  time_limit: float = PREVIEW_TIME_LIMIT_S, earlier: dict[int, int] | None = None) -> tuple[list[list[str]], list[RosterResult]]:
    """A split under `strategy` for the modal: the layout (flat groups) and the rosters, nothing saved.
    `avoid` = earlier previews (flat groups) the answer must differ from. `time_limit`: the split modal's budget by
    default; the lock preview passes the lock's (SOLVE_TIME_LIMIT_S) so it shows the roster the lock will build.
    `earlier` (a later raid whose earlier raid is still open: earlier_counts) = who the raids before it will roster."""
    k = groups_per_roster(reg, run_size(reg, ev))
    prev = [{n: gi // k for gi, g in enumerate(lay) for n in g} for lay in (avoid or [])]
    prev_g = [{n: gi for gi, g in enumerate(lay) for n in g} for lay in (avoid or [])]
    trial = ev.model_copy(deep=True)
    try:
        players, rosters, _ = _solve_run(reg, rs, trial, strategy=strategy, avoid=prev, time_limit=time_limit, whatif=False, avoid_groups=prev_g,
                                         earlier=earlier)
    except RuntimeError as e:
        if not (prev and "INFEASIBLE" in str(e)):
            raise
        # "another one" found none: is it the earlier answers that leave nothing, or the board and the rules? (a model
        # without the avoid lines is proven quickly either way)
        _solve_run(reg, rs, ev.model_copy(deep=True), strategy=strategy, time_limit=time_limit, whatif=False, earlier=earlier)
        raise RuntimeError(f"no roster found: {NO_OTHER}") from e
    seated = {p.signup_name for r in rosters for p in r.selected}
    rosters[0].benched = [p for p in players if p.signup_name not in seated]
    return [list(g) for r in rosters for g in r.groups], rosters


# ---------------------------------------------------------------- the officers' board (bank + groups) before and after lock

def groups_per_roster(reg: Registry, size: int) -> int:
    return max(1, -(-size // int(reg.profile.comp_rules["group_size"])))


def board_layout(reg: Registry, ev: RaidEvent) -> list[list[str]]:
    """The board as it stands, flat groups across rosters: the locked roster(s), else the officers' layout.
    Always padded to whole rosters so (roster, group) addresses are stable — and, before the lock, to every roster the
    run expects (the owner, 2026-11-22: a 10-player ×2 run shows 20 places, not 10)."""
    n = groups_per_roster(reg, run_size(reg, ev))
    if ev.state != "open" and ev.all_rosters:
        flat = [list(g) for r in ev.all_rosters for g in (list(r.groups) + [[] for _ in range(n - len(r.groups))])[:max(n, len(r.groups))]]
        expected = 1
    else:
        flat = [list(g) for g in (ev.layout or [])]
        expected = run_rosters(reg, ev) if ev.state == "open" else 1
    want = max(n * expected, -(-len(flat) // n) * n)
    return flat + [[] for _ in range(want - len(flat))]


class BoardChanged(RegistryError):
    """A whole-board write made on a board that changed since its author looked (its `rev` is stale)."""

    def __init__(self, msg: str = "The board changed since you looked (someone else moved people) — check it and try again."):
        super().__init__(msg)


class SolverBusy(RegistryError):
    """RaidMixin.solving: the solver is already working on this run (a lock, an auto-fill, a split preview)."""

    def __init__(self, msg: str = "the roster is already being built — try again in a moment"):
        super().__init__(msg)


def board_rev(reg: Registry, ev: RaidEvent) -> str:
    """A fingerprint of the board (groups + pins + state). A whole-board write carrying an older one is stale."""
    import hashlib
    import json

    blob = json.dumps([ev.state, board_layout(reg, ev), sorted(ev.pins.items())], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def record_moves(ev: RaidEvent, name: str, at_before: dict, groups: list[list[str]], pins_before: dict, by_id: int | None) -> None:
    """Whose moves the board holds after one drag (Undo my moves reads it): every name whose group changed and every
    pin the drag changed. With `by_id` the mover's name is theirs; a swapped partner keeps whoever placed them (a
    partner nobody placed becomes the mover's); a pin the drag changed records what it was. Without `by_id` (a move
    from Discord, plain text or MCP) the moved names and changed pins belong to nobody. A name moved by someone
    supersedes another officer's pin record on it."""
    at_after = {n: gi for gi, g in enumerate(groups) for n in g}
    changed = {n for n in set(at_before) | set(at_after) if at_before.get(n) != at_after.get(n)} | {name}
    uid_of = {n: str(sg.discord_id) for n in changed if (sg := signup_by_name(ev, n)) is not None}
    owners, records = dict(ev.placed_by), dict(ev.drag_pins)
    for n in changed:
        u = uid_of.get(n)
        if by_id is None:
            owners.pop(n, None)
            records.pop(u, None)
            continue
        if n == name:
            owners[n] = int(by_id)
        else:
            owners.setdefault(n, int(by_id))
        if u in records and int(records[u].get("by") or 0) != int(by_id):
            records.pop(u)
    for u in set(pins_before) | set(ev.pins):
        if pins_before.get(u) == ev.pins.get(u):
            continue
        if by_id is None:
            records.pop(u, None)
            continue
        rec = records.get(u)
        was = rec["was"] if rec and int(rec.get("by") or 0) == int(by_id) and rec.get("now") == pins_before.get(u) else pins_before.get(u)
        if was == ev.pins.get(u):
            records.pop(u, None)  # back as it was: nothing of this officer's left to undo
        else:
            records[u] = {"by": int(by_id), "was": was, "now": ev.pins.get(u)}
    ev.placed_by, ev.drag_pins = owners, records


def own_pins(ev: RaidEvent, by_id: int | None) -> dict[str, dict]:
    """The pin changes officer `by_id`'s drags made that still stand (ev.drag_pins whose pin is still what the drag
    left): member id → the record."""
    if by_id is None:
        return {}
    return {u: d for u, d in ev.drag_pins.items() if int(d.get("by") or 0) == int(by_id) and ev.pins.get(u) == d.get("now")}


def own_moves(ev: RaidEvent, by_id: int | None) -> list[str]:
    """The names officer `by_id` moved by hand on the board before the lock and that still stand: placed in a group
    (ev.placed_by) or pinned / unpinned by their drag (own_pins), sorted."""
    if by_id is None:
        return []
    names = {n for n, u in ev.placed_by.items() if int(u) == int(by_id)}
    names |= {sg.display_name for u in own_pins(ev, by_id) if (sg := ev.signups.get(u)) is not None}
    return sorted(names)


def undo_rev(reg: Registry, ev: RaidEvent, by_id: int | None) -> str:
    """What Undo my moves acts on: the board (board_rev) and the officer's own moves. The undo box's fingerprint and
    RaidMixin.undo_moves' check: a board, or a set of moves, that changed since the box was shown is refused."""
    import hashlib

    return board_rev(reg, ev) + hashlib.sha1("\n".join(own_moves(ev, by_id)).encode()).hexdigest()[:8]


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


def group_words(reg: Registry, ev: RaidEvent, gi: int, layout: list[list[str]] | None = None) -> str:
    """A flat group index in words: 'Group 2', or 'Group 2 of roster 1' when the board holds several rosters."""
    k = groups_per_roster(reg, run_size(reg, ev))
    n = len(layout if layout is not None else board_layout(reg, ev))
    return f"Group {gi % k + 1}" + (f" of roster {gi // k + 1}" if n > k else "")


def board_move_kind(reg: Registry, ev: RaidEvent, name: str, to: tuple[int, int] | None) -> tuple[str | None, str | None, str | None]:
    """What one board move is (consequences.board_move_box's words), read on the board as it stands with apply_move's
    own rules: ('move', None, where) to another group while rostered, ('swap', other, where) dropped on someone while
    rostered, ('roster', None, where) from Not rostered into a free place, ('substitute', other, where) from Not rostered
    onto someone, ('release', None, None) back to Not rostered; (None, None, None) when nothing would change. ValueError
    as apply_move raises it (not joined, a full group). PURE."""
    apply_move(reg, ev, name, to)  # its refusals, nothing kept
    layout = board_layout(reg, ev)
    origin = next((gi for gi, g in enumerate(layout) if name in g), None)
    if to is None:
        return ("release", None, None) if origin is not None else (None, None, None)
    gi, pos = to
    g = layout[gi] if gi < len(layout) else []
    other = g[pos] if 0 <= pos < len(g) and origin != gi else None
    where = group_words(reg, ev, gi, layout)
    if origin is not None:
        if other:
            return "swap", other, where
        return ("move", None, where) if origin != gi else (None, None, None)
    return ("substitute", other, where) if other else ("roster", None, where)


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


# ---------------------------------------------------------------- the board as the lock would place it (Events page)

def event_board_preview(reg: Registry, rs: RaidStore, first: RaidEvent, parts: list[RaidEvent] | None = None, stop=None,
                        memo=None, raid_key=None, counts: dict[int, int] | None = None) -> dict[str, "list[RosterResult] | str"]:
    """Every OPEN raid of an event drawn as its lock would place people now — PURE: deep copies, nothing saved. Each
    raid solves through the lock's own entry (_solve_run, the run's strategy, the lock's budget SOLVE_TIME_LIMIT_S, the
    raid's minimums, pins and the officers' board as hard placements), so the solver's repeatable answer is the
    roster lock_run then builds. A later raid gets `earlier` from the raids before it: their locked rosters, or the
    previews of the ones still open (what their locks will roster). A raid nobody joined is never solved: its board is
    every expected roster's groups, empty. A raid whose solve fails maps to rc.solver_error_text's sentence, and the
    raids after it still solve with what the others placed. `parts` = the event's parts already copied (the board
    cache's snapshot), else read from `rs`. `stop` (solver.Stop): the board cache abandons the solve when a lock
    starts — solver.Stopped is raised, never turned into a raid's sentence. `memo` + `raid_key(p, earlier)` (the
    board cache's, per event): a raid whose own inputs — its key covers them and the `earlier` it is handed — were
    solved before takes that answer instead of solving again (the solver is repeatable: the same answer); the picture
    only, never the lock. `counts` (a dict): filled with how many of the raids solved rostered (or will roster) each
    Discord id — the `earlier` a raid after them gets (earlier_counts). → {run key: [RosterResult]
    (rosters[0].benched = the joiners not placed) | the sentence}; a locked or closed raid has no entry (its board is
    its roster)."""
    parts = [p.model_copy(deep=True) for p in (parts if parts is not None else event_parts(rs, first))]
    out: dict[str, list[RosterResult] | str] = {}
    times: dict[int, int] = counts if counts is not None else {}  # discord id → how many raids before this one rostered (or will roster) them

    def count(p: RaidEvent, rosters: list[RosterResult]) -> None:
        for uid in {player_uid(p, x) for r in rosters for x in r.selected} - {None}:
            times[uid] = times.get(uid, 0) + 1

    for p in parts:
        if p.state != "open":
            if p.all_rosters:
                count(p, p.all_rosters)
            continue
        if not p.by_status("in"):  # nobody joined: nothing to place, no solve
            out[p.key] = board_rosters(reg, p, board_layout(reg, p), run_size(reg, p))
            continue
        earlier = dict(times) if is_follow(p) else None
        mk = raid_key(p, earlier) if memo is not None and raid_key is not None else None
        if mk is not None and mk in memo:
            out[p.key] = memo[mk]
            if isinstance(out[p.key], list):
                count(p, out[p.key])
            continue
        try:
            players, rosters, _ = _solve_run(reg, rs, p, time_limit=SOLVE_TIME_LIMIT_S, whatif=False, earlier=earlier, stop=stop)
        except solver.Stopped:
            raise
        except Exception as e:  # noqa: BLE001 — infeasible / timed out: this raid says why, the others still solve
            out[p.key] = solver_error_text(e)
        else:
            placed = {x.signup_name for r in rosters for x in r.selected}
            rosters[0].benched = [x for x in players if x.signup_name not in placed and x.status == "signed"]
            out[p.key] = rosters
            count(p, rosters)
        if mk is not None:
            memo[mk] = out[p.key]
    return out


def earlier_counts(reg: Registry, rs: RaidStore, ev: RaidEvent) -> dict[int, int] | None:
    """A later raid's `earlier` for a solve made while a raid before it is still OPEN (Preview lock on a later raid,
    the final fix wave of §5.67 phase 4): who the raids before it will roster, as the board picture counts them — a
    locked raid's roster, an open raid's own lock solve (event_board_preview over the raids before it). Without it the
    preview solved as if nobody were rostered before it, while the lock (made only after those raids lock) reads their
    rosters. BLOCKING (it may solve): a thread. None for a first raid, or when no raid before it is open (the solve
    reads their rosters itself, exactly as the lock does)."""
    if not is_follow(ev):
        return None
    before = [p for p in event_parts(rs, ev) if part_no(p) < part_no(ev)]
    if not any(p.state == "open" for p in before):
        return None
    times: dict[int, int] = {}
    event_board_preview(reg, rs, first_part(rs, ev), parts=before, counts=times)
    return times


def overlay_board(reg: Registry, ev: RaidEvent, rosters: list[RosterResult]) -> list[RosterResult]:
    """The last picture of raid `ev` (an older solve's rosters) with the officers' CURRENT moves on top, for the while
    a new picture is worked out — so a drag never snaps back. PURE. Each name in ev.layout is drawn at its group; a
    name pinned out, or no longer joined, leaves the board; whoever a placed name displaces goes to the first group with
    room, else to Not placed; anyone else keeps the old picture's place. → board_rosters of that layout."""
    size = run_size(reg, ev)
    k = groups_per_roster(reg, size)
    gsize = int(reg.profile.comp_rules["group_size"])
    joined = {s.display_name for s in ev.signups.values() if s.status == "in"}
    out = {sg.display_name for u, v in ev.pins.items() if v == "out" and (sg := ev.signups.get(u)) is not None}
    lay = [[x for x in g if x in joined and x not in out] for g in (ev.layout or [])]
    hand = {x for g in lay for x in g}
    pic = [list(g) for r in rosters for g in (list(r.groups) + [[] for _ in range(k - len(r.groups))])]
    n = -(-max(len(pic), len(lay), 1) // k) * k
    pic += [[] for _ in range(n - len(pic))]
    lay += [[] for _ in range(n - len(lay))]
    pic = [[x for x in g if x in joined and x not in out and x not in hand] for g in pic]
    displaced: list[str] = []
    for gi in range(n):
        room = max(0, gsize - len(lay[gi]))
        displaced += pic[gi][room:]
        pic[gi] = lay[gi] + pic[gi][:room]
    for x in displaced:
        gi = next((i for i, g in enumerate(pic) if len(g) < gsize), None)
        if gi is not None:
            pic[gi].append(x)
    return board_rosters(reg, ev, pic, size)


def _slot_role(reg: Registry, slot: str) -> str:
    """The role a minimum's slot is drawn with: a role is itself, a spec its role, a class the role most of its specs
    play (ties in ROLES order)."""
    if slot in ROLES:
        return slot
    cls, _, spec = slot.partition(":")
    try:
        if spec:
            return reg.profile.spec(cls, spec).role
        roles = [reg.profile.spec(cls, sp).role for sp in reg.profile.classes.get(cls, {})]
    except KeyError:
        return "melee"
    return max(ROLES, key=lambda r: (roles.count(r), -ROLES.index(r))) if roles else "melee"


def open_seat_roles(reg: Registry, ev: RaidEvent, rosters: list[RosterResult]) -> list[list[str]]:
    """Per roster, its empty seats a minimum still wants, as roles (the page draws the role icon + "open seat"): the
    tank shortfall first, then the healer's, then the other slots' (melee, ranged, a class, a spec) by their role — a
    class or spec slot covers its role's shortfall as far as it goes (one Holy Paladin fills both), and never more
    seats than the roster has empty."""
    size = run_size(reg, ev)
    mins = reg.minimums(ev.instance, size)
    out = []
    for r in rosters:
        free = max(0, size - len(r.selected))
        short = {slot: max(0, int(n) - sum(1 for p in r.selected if slot_holds(slot, p))) for slot, n in mins.items()}
        by_role = {role: short.get(role, 0) for role in ROLES}
        rest: list[str] = []
        for slot, n in short.items():
            if slot in ROLES or not n:
                continue
            role = _slot_role(reg, slot)
            by_role[role] = max(0, by_role[role] - n)
            rest += [role] * n
        seats = ["tank"] * by_role["tank"] + ["healer"] * by_role["healer"] + ["melee"] * by_role["melee"] + ["ranged"] * by_role["ranged"]
        out.append((seats + rest)[:free])
    return out


def apply_move_shown(reg: Registry, ev: RaidEvent, shown: list[list[str]], name: str, to: tuple[int, int] | None) -> tuple[list[list[str]], dict[str, str], list[str]]:
    """One drag on the board AS THE PAGE SHOWS IT before the lock (the lock's placement, `shown` = its flat groups as
    the server holds them now), written as the officers' own placements: the same swap and full-group rules as
    apply_move, read on `shown`, but only the mover (and a swapped partner) changes in the officers' layout — pinned
    to the group they land in — and every other name stays where ev.layout had it, or unplaced (the lock places them).
    A drop on Not placed keeps them out of the lock (removed from the layout, pin "out"); a place in a group lifts an
    "out" pin. ValueError when the group is full (on the picture, or with the officers' placements) or `name` hasn't
    joined. → (the new layout, the new pins, the names moved)."""
    sg = signup_by_name(ev, name)
    if sg is None or sg.status != "in":
        raise ValueError(f"{name} hasn't joined this run")
    k = groups_per_roster(reg, run_size(reg, ev))
    gsize = int(reg.profile.comp_rules["group_size"])
    pic = [list(g) for g in shown]
    lay = board_layout(reg, ev)
    pins = dict(ev.pins)
    joined = {s.display_name for s in ev.signups.values() if s.status == "in"}

    def fit(n: int) -> None:
        for board in (pic, lay):
            while len(board) < n:
                board.append([])

    def uid(x: str) -> str | None:
        s = signup_by_name(ev, x)
        return str(s.discord_id) if s is not None else None

    def place(x: str, gi: int, pos: int | None = None) -> None:
        for g in lay:
            if x in g:
                g.remove(x)
        fit(gi + 1)
        lay[gi].insert(len(lay[gi]) if pos is None else max(0, min(pos, len(lay[gi]))), x)
        if pins.get(uid(x)) == "out":
            pins.pop(uid(x))

    def keep_out(x: str) -> None:
        for g in lay:
            if x in g:
                g.remove(x)
        if uid(x) is not None:
            pins[uid(x)] = "out"

    origin = next((gi for gi, g in enumerate(pic) if name in g), None)
    if to is None:
        keep_out(name)
        return lay, pins, [name]
    gi, pos = to
    fit(max(gi + 1, -(-(gi + 1) // k) * k))
    g = pic[gi]
    other = g[pos] if 0 <= pos < len(g) else None
    if other is not None and other != name and other in joined and origin != gi:  # dropped on someone in another group: swap
        place(name, gi, pos)
        if origin is not None:
            place(other, origin)
        else:
            keep_out(other)
        moved = [name, other]
    else:
        if len([x for x in g if x != name]) >= gsize:
            raise ValueError(f"group {gi % k + 1} is full")
        place(name, gi, pos)
        moved = [name]
    if any(len(x) > gsize for x in lay):
        raise ValueError(f"group {gi % k + 1} is full")
    return lay, pins, moved


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


def free_seat(reg: Registry, rs: RaidStore, ev: RaidEvent, who, why: str, *, notice: bool = True) -> Player | None:
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
    if ev.follows and uid is not None and uid not in ev.leaving:  # out of the follow-on while still on the lead is leaving
        lead = lead_of(rs, ev)  # after the lead (design §5.62): kept out when the lead is saved again (sync_follow)
        lead_row = lead.signups.get(str(uid)) if lead is not None else None
        if lead_row is not None and lead_row.status != "out":  # a lead seat freed first (the cascade) is the whole night
            ev.leaving.append(uid)
    ev.log.append(f"seat freed: {name} ({why})")
    rs.save(ev, f"seat freed {name}")
    if uid is not None:
        seat_gone(reg, ev, uid, why, notice=notice)  # their open Confirm ask closes (a later Confirm is refused) and their inbox hears why
    on_roster_change(rs.key, ev, removed=[uid if uid is not None else name], roster_of={uid if uid is not None else name: i})
    if ev.followed_by and not is_follow(ev) and uid is not None:  # out of the first raid is out of the night: every later
        for fev in later_parts(rs, ev):  # raid's seat goes too (idempotent; the scheduler's next fill tick covers it,
            if fev.state == "locked" and fev.seat_of(uid):  # RaidMixin.drop_seated asks at once)
                if fev.fill_state == "exhausted":  # a freed seat is a new gap: the engine looks again (as the scheduler's expiry does)
                    fev.fill_state = "idle"
                free_seat(reg, rs, fev, uid, why, notice=False)
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
            ev.log.append(f"rostered {sg.display_name} in roster {i + 1}")
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


# a run dict with no run file this long after ensure_run made it is an open cut off before its file was written (a
# restart, a failed save): cleanup_ephemeral drops it — a new key holds no time, so nothing else ever matches it again
DICT_WITHOUT_RUN_HOURS = 1.0
REOPEN_GRACE_HOURS = 1.0  # a run ended / closed by mistake can be reopened this long after (never a cancelled one, D19)


def reopen_window() -> str:
    """REOPEN_GRACE_HOURS in words ('an hour', '2 hours'): End's confirm (consequences.consequence("end")) and its done
    line say how long Reopen works from this one number."""
    h = REOPEN_GRACE_HOURS
    return "an hour" if h == 1 else f"{h:g} hours"


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
        return "A cancelled run can't be reopened. Open a new run instead."
    if ev.state != "done":
        return "This run hasn't ended, so there is nothing to reopen."
    t = at or datetime.now(ev.start.tzinfo)
    if not closed_recently(ev, at=t):
        return f"It ended more than {reopen_window()} ago. Reopen is only for a run ended by mistake."
    if t >= run_close_at(reg, ev):
        return "Its scheduled close has passed, so the run is over."
    if not reg.config.team(ev.team):
        return "Its roster settings are already cleaned up. Open a new run instead."
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
    seat; "no answer" needs a question first. Only a LOCKED run's asks expire (a run cancelled mid-tick frees nobody)."""
    if ev.state != "locked" or not ev.confirm_by or datetime.fromisoformat(ev.confirm_by) > datetime.now(ev.start.tzinfo):
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
        return ("Remind is for a locked roster, and this sheet is still open." if ev.state == "open" else
                f"This run is already {RUN_STATE_WORDS.get(ev.state, ev.state)}.")
    if not run_confirms(reg, ev):
        return no_confirm_sentence(reg, ev, "there is nothing to remind")
    t = at or datetime.now(ev.start.tzinfo)
    if ev.reminded_at and t - datetime.fromisoformat(ev.reminded_at) < REMIND_EVERY:
        nxt = datetime.fromisoformat(ev.reminded_at) + REMIND_EVERY
        return (f"{ev.reminded_by or 'An officer'} reminded them at {reg.local12(ev.reminded_at, '%I:%M %p')}. "
                f"Remind works once an hour, so try again after {reg.local12(nxt, '%I:%M %p')}.")
    rows = open_placement_rows(reg, ev)
    if not rows:
        return "Everyone rostered has answered, so there is nobody to remind."
    if not any(a.get("channel", "dm") == "dm" and not m.dm_opt_out for m, a in rows):
        return "Everyone who hasn't answered has DMs off, so a reminder can't reach them. Their asks wait on the site."
    return None


def cancel_effects(reg: Registry, ev: RaidEvent, rs: "RaidStore | None" = None) -> dict:
    """Who a cancel touches, from the run as it stands (consequences.consequence("cancel"), the plain diff, MCP):
    `rostered` names of the seated (after lock), `open_asks` names with an unanswered Confirm / Can't, `fill_asks` names
    with a fill or pre-lock ask in play, `answered` how many answered the sheet. Nobody is DMed by a cancel (the DM
    rule): the sheet, the open ask cards, the signup channel line and the run's thread carry it. A cancel on any raid of
    a run cancels every LIVE raid of it (RaidMixin.cancel_run): see _cancel_lineup for `later` / `follow` / `lead` /
    `first_closed` — `rs` (the run store) tells which raids are still live; without it every raid of the chain counts."""
    locked = ev.state != "open" and bool(ev.all_rosters)
    return {"state": ev.state, "locked": locked,
            "rostered": [p.signup_name for p in ev.seated()] if locked else [],
            "open_asks": [m.display_name for m, _a in open_placement_rows(reg, ev)] if locked else [],
            "fill_asks": [a.display_name for a in ev.fill_asks if a.pending],
            "answered": len([s for s in ev.signups.values() if s.status in STATUSES]),
            **_cancel_lineup(reg, ev, rs)}


def _cancel_lineup(reg: Registry, ev: RaidEvent, rs: "RaidStore | None" = None) -> dict:
    """`later` = the names of the later raids the cancel takes (a later `ev` itself included), in order; `follow` = them
    in words when `ev` is the first raid; `lead` = the first raid's name when `ev` is a later raid and the first is still
    live (the whole run goes); `first_closed` = `ev` is a later raid whose first raid has already finished or gone."""
    if rs is not None:
        parts = event_parts(rs, ev)
        live = [p for p in parts if p.state not in ("done", "cancelled") or p is ev]
        first = parts[0] if parts and not is_follow(parts[0]) else None
        later = [reg.raid_def(p.instance).get("name", p.instance) for p in live if is_follow(p)]
        first_live = first is not None and first.state not in ("done", "cancelled")
        lead = reg.raid_def(first.instance).get("name", first.instance) if is_follow(ev) and first_live else None
        return {"later": later, "follow": join_words(later) if later and not is_follow(ev) else None, "lead": lead,
                "first_closed": is_follow(ev) and not first_live}
    lt = lead_dict(reg, ev) if is_follow(ev) else reg.config.team(ev.team)
    name = lambda t: reg.raid_def(t.get("instance")).get("name", t.get("instance"))  # noqa: E731
    later: list[str] = []
    if lt is not None:
        first = RaidEvent(key=lt["key"], team=lt["key"], instance=lt.get("instance"), starts_at=ev.starts_at)
        later = [name(t) for t in later_dicts(reg, first)]
    if not later and ev.followed_by and (ft := follow_dict(reg, ev)):  # a dict chain gone (cleaned up): the next raid at least
        later = [name(ft)]
    return {"later": later, "follow": join_words(later) if later and not is_follow(ev) else None,
            "lead": name(lt) if is_follow(ev) and lt is not None else None, "first_closed": False}


# ---------------------------------------------------------------- opening: which run a picked time is (design B4)

def which_run(reg: Registry, instance: str, start: datetime, sid: str | None = None) -> dict:
    """What an open at `start` would be — the one answer the /raid open wizard, the site's preview route and plain text
    share: {kind: scheduled | adhoc, schedule (id; ADHOC_SCHEDULE = a one-off), schedule_name, occurrence (the stamp a
    schedule's run is found by, None for a one-off)}. `sid` named → that schedule's run (a pickup template is ad hoc); none → one of the raid's
    upcoming runs at exactly `start` (scheduled), else a one-off (ad hoc)."""
    if instance not in reg.profile.raids:
        raise RegistryError(f"unknown raid {instance or '?'} (one of {', '.join(reg.profile.raids)})")
    if sid and sid != ADHOC_SCHEDULE and not any(s["id"] == sid for s in reg.schedules(instance)):
        raise RegistryError(f"{reg.raid_def(instance).get('name', instance)} has no schedule {sid}")
    resolved = open_schedule(reg, instance, start, sid)
    if resolved == ADHOC_SCHEDULE:
        kind, name = "adhoc", "One-off"
    else:
        s = next((x for x in reg.schedules(instance) if x["id"] == resolved), None) or {}
        kind = "adhoc" if schedule_class(s.get("kind") or "weekly") == "adhoc" else "scheduled"
        name = s.get("name") or resolved
    return {"kind": kind, "schedule": resolved, "schedule_name": name,
            "occurrence": occurrence_stamp(reg, start) if schedule_stamp(resolved) else None}


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
        later_fo = [t for s in has for e in schedule_lineup(instance, s)[1:] if (t := reg.first_open(e["raid"])) and t > reg.now_local()]
        why = (f"its slots all fall before it opens on {reg.local12(fo)}" if has and fo and fo > reg.now_local()
               else f"a later raid of its run first opens {reg.local12(min(later_fo))}" if later_fo  # C8: those nights are skipped
               else "it has no recurring slots yet")
        raise RegistryError(f"No next slot for this raid: {why}. Pick a date and time instead, or set it up on the Raids page.")
    return nxt[0][1], nxt[0][0]["id"], ""


def dst_words(reg: Registry, note: str, start: datetime) -> str:
    """The DST note in words for a person (the wizard's when_note, the site's preview): "" on an ordinary night."""
    if note == "gap":
        return f"That time doesn't exist that night because the clocks go forward. The run uses {reg.local12(start, '%I:%M %p')} instead."
    if note == "ambiguous":
        return f"{reg.local12(start, '%I:%M %p')} happens twice that night because the clocks go back. The run uses the first one."
    return ""


OPEN_FOLLOW_REFUSAL = "A follow-on can't be added to a run that is already open."


def open_preview(reg: Registry, rs: "RaidStore", instance: str, value: str | None = None, schedule: str | None = None, follow: dict | None = None,
                 lineup: list[dict] | None = None, first_rosters: int | None = None) -> dict:
    """What Open would do, before anything is written (POST /api/raid/{rid}/open/preview; the wizard's confirm shows
    the same facts): {kind, schedule, schedule_name, occurrence, start (ISO — the exact instant Open must carry back), label
    (12-hour guild time), when_note (the DST line or ""), exists (the live run's state at that key, or None), channel
    (the signup channel id the sheet posts in, or None), rules (what kind of run it is, in sentences), past, closed
    (done | cancelled when that time's run is closed — Open is refused), refusal (the sentence why, or None), later
    ([{raid, name, rosters, when}] — the later raids: `lineup` given (or `follow`, one entry), else the schedule's own;
    a run already open at that time: ITS later raids), follow (later[0] or None), follow_line (the sentence the confirm
    prints: "Then Barrow Deeps ×2 at 9:30 PM, from the same signup.", OPEN_FOLLOW_REFUSAL when later raids were asked of
    a run already open, or "") and before_open (consequences.before_first_open_sentence: a raid of the lineup that
    first opens after its start, or None — C9)}. Each `later` entry also carries `starts` (minutes after the first raid)
    and `time` (its clock alone, '9:30 PM'). `first_rosters`: the first raid's rosters a one-off asks for."""
    from .consequences import before_first_open_sentence

    start, sid, note = open_when(reg, instance, value, schedule)
    w = which_run(reg, instance, start, sid)
    ev = find_run(reg, rs, instance, start, w["schedule"])
    live = ev if ev is not None and ev.state not in ("done", "cancelled") else None
    refusal = closed_run_refusal(rs, ev) or (None if live is not None else open_refusal(reg, rs, instance, start, w["schedule"]))
    rules = " ".join(open_rules(reg, instance, w["schedule"], w["kind"]))
    asked = lineup if lineup is not None else ([follow] if follow else None)
    if asked is None:
        asked = schedule_lineup(instance, reg.schedule_def(instance, w["schedule"])["schedule"])[1:] if w["schedule"] != ADHOC_SCHEDULE else []
    later: list[tuple[dict, datetime]] = []
    follow_line = ""
    if live is not None:  # Open points at the run already there: its own later raids, never the ones asked for (design §5.62)
        later = [({"raid": t.get("instance"), "rosters": int(t.get("rosters") or 1), "starts": int(t.get("starts") or 0)}, dict_start(reg, live, t))
                 for t in later_dicts(reg, live)]
        if not later and asked:
            follow_line = OPEN_FOLLOW_REFUSAL
    elif asked:
        full = [{"raid": instance, "rosters": int(first_rosters or 1)}, *asked]
        offsets = reg.lineup_offsets(full)
        later = [({**e, "starts": off}, after_minutes(start, off)) for e, off in zip(full[1:], offsets[1:])]
    ljson = [{"raid": e["raid"], "name": reg.raid_def(e["raid"]).get("name", e["raid"]), "rosters": int(e.get("rosters") or 1), "when": reg.local12(at),
              "time": reg.local12(at, "%I:%M %p"), "starts": int(e.get("starts") or 0)}
             for e, at in later]
    if later:
        follow_line = "Then " + ", then ".join(f"{reg.follow_words(e)} at {reg.local12(at, '%I:%M %p')}" for e, at in later) + ", from the same signup."
    before = None if live is not None else before_first_open_sentence(reg, [{"raid": instance, "rosters": 1}, *asked], start)
    return {**w, "later": ljson, "follow": ljson[0] if ljson else None, "follow_line": follow_line, "before_open": before,
            "start": start.isoformat(), "label": reg.local12(start), "when_note": dst_words(reg, note, start), "exists": live.state if live else None,
            "closed": ev.state if ev is not None and closed_run_refusal(rs, ev) else None, "refusal": refusal, "channel": reg.config.signup_channel_id or None, "rules": rules, "past": start < reg.now_local() - timedelta(minutes=PAST_GRACE_MINUTES)}


UPCOMING_PER_SCHEDULE = 3  # the Events page's Coming up lists each active schedule's next three nights, whatever the horizon


def open_row_refusal(reg: Registry, rs: "RaidStore", instance: str, start: datetime, sid: str, opens: datetime, now: datetime | None = None) -> str | None:
    """Why an upcoming run's row can't be opened by hand yet, in words (the Events page's Coming up prints it under Open the sheet now), or None.
    A closed run at that time is never opened again (closed_run_refusal); a run more than OPEN_HORIZON_DAYS out isn't
    "the next run" the open paths reach (open_when), so the row says when Open works and when the sheet opens on its own."""
    why = closed_run_refusal(rs, event_of_occurrence(rs, sid, occurrence_stamp(reg, start), reg=reg))
    if why:
        return why
    now = now or reg.now_local()
    if start > now + timedelta(days=OPEN_HORIZON_DAYS):
        early = start - timedelta(days=OPEN_HORIZON_DAYS)
        return (f"This run is too far ahead to open now. Open works from {reg.local12(early)}, three weeks before the run."
                + (f" Its sheet opens on its own {reg.local12(opens)}." if opens > early else ""))
    return None


SCHEDULE_NIGHT = ("{when} is a night of {name}, so a one-off can't start then. Open that night from its schedule instead, "
                  "or pick another time.")
NIGHT_TAKEN = ("{title} already starts then, so {name} can't open that night beside it. Keep that event for the night, "
               "or cancel it first.")


def open_refusal(reg: Registry, rs: "RaidStore", instance: str, start: datetime, schedule: str | None = None,
                 now: datetime | None = None) -> str | None:
    """Why an on-demand open of `instance` at `start` is refused before anything is written (every open surface goes
    through RaidMixin.open_run_and_post; open_preview's `refusal` says it first), or None. A live run at that minute is
    never refused: Open points at it. A night of a recurring schedule, at any distance (open_schedule), opens only as
    that schedule's run: asked as a one-off or from a pickup template it is refused (SCHEDULE_NIGHT); as the schedule's
    run it is refused while it is too far ahead to open (open_row_refusal, the Events page's Coming up sentence) — never
    silently a one-off beside the run the schedule then opens — and while a live ONE-OFF already holds that minute (one
    made there before this check; NIGHT_TAKEN — the scheduler skips that night the same way). Its sheet already due
    and nothing in the way (the scheduler's case): None."""
    now = now or reg.now_local()
    have = find_run(reg, rs, instance, start, schedule)
    if have is not None and have.state not in ("done", "cancelled"):
        return None
    night = open_schedule(reg, instance, start, None)
    if night == ADHOC_SCHEDULE:
        return None
    asked = schedule or night
    tpl = next((s for s in reg.schedules(instance) if s["id"] == asked), None)
    if asked == ADHOC_SCHEDULE or (tpl is not None and schedule_class(tpl.get("kind") or "weekly") == "adhoc"):
        s = next((x for x in reg.schedules(instance) if x["id"] == night), None) or {}
        return SCHEDULE_NIGHT.format(when=reg.local12(start), name=s.get("name") or night)
    if asked != night:
        return None  # another recurring schedule named: open_run's own rules
    other = live_at(rs, instance, start)
    if other is not None and schedule_stamp(other.schedule) is None:  # a one-off on the night: never a second run beside it
        from .raid_views import run_title

        return NIGHT_TAKEN.format(title=run_title(reg, other), name=(tpl or {}).get("name") or night)
    opens = start - timedelta(hours=float(reg.schedule_def(instance, night)["signup_lead_hours"]))
    return None if opens <= now else open_row_refusal(reg, rs, instance, start, night, opens, now)


def upcoming_rows(reg: Registry, rs: "RaidStore", instance: str, now: datetime | None = None, n: int = UPCOMING_PER_SCHEDULE) -> list[dict]:
    """Each ACTIVE recurring schedule's next `n` runs that aren't live yet, soonest first, whatever the horizon (the
    Events page's Coming up and its Open the sheet now; a pickup template opens nothing on its own): {schedule,
    schedule_name, start (words), start_iso (the exact instant Open carries back), occurrence, open_key (the live run's
    key: always None here, a live run has its own card), closed (that occurrence's run is finished or cancelled), opens
    (words), opens_iso,
    needs_channel (the sheet is due and no signup channel can take it), can_open, why (open_row_refusal's sentence),
    follow (the schedule's follow-on in words, "Barrow Deeps ×2", or "")}."""
    now = now or reg.now_local()
    no_channel = not reg.config.signup_channel_id
    out = []
    for s in reg.schedules(instance):
        if s.get("kind") == "pickup" or not s.get("active", True):
            continue
        lead = float(reg.schedule_def(instance, s["id"])["signup_lead_hours"])
        rows = []
        for t in next_runs_of(reg, instance, s, n + 6, now):
            occ = occurrence_stamp(reg, t)
            ev = event_of_occurrence(rs, s["id"], occ, reg=reg)
            if ev is not None and ev.state not in ("done", "cancelled"):
                continue  # live: it has its own card
            opens = t - timedelta(hours=lead)
            why = open_row_refusal(reg, rs, instance, t, s["id"], opens, now)
            rows.append({"schedule": s["id"], "schedule_name": s.get("name") or s["id"], "start": reg.local12(t), "start_iso": t.isoformat(),
                         "occurrence": occ, "open_key": None, "closed": ev is not None,
                         "opens": reg.local12(opens), "opens_iso": opens.isoformat(), "needs_channel": no_channel and opens <= now,
                         "can_open": why is None, "why": why, "follow": reg.later_words(schedule_lineup(instance, s)[1:])})
            if len(rows) >= n:
                break
        out += rows
    return sorted(out, key=lambda r: (r["start_iso"], r["schedule"]))


def short_why(reg: Registry, ev: RaidEvent) -> str | None:
    """Why a short run is short, as the clause after the role icons on a run card (the Test bench's) ("short ⛨1 — 2 asks out, 1 on
    the bench could fill it"): after the lock the fill asks out and who could take a seat; before it the expected people
    who haven't answered. None when there is nothing to add (or the run is over)."""
    if ev.state in ("done", "cancelled"):
        return None
    cls_short = class_short_words(gaps(reg, ev)["classes"])
    head = [f"short {cls_short}"] if cls_short else []  # a class minimum is shown here, never asked for
    if ev.state != "open" and ev.all_rosters:
        asks = [a for a in ev.fill_asks if a.pending]
        asked = {a.discord_id for a in asks}
        seated = {player_uid(ev, p) for p in ev.seated()}
        bench = [s for s in ev.signups.values() if s.status in ("in", "sub") and s.discord_id not in seated and s.discord_id not in asked]
        parts = head + ([f"{len(asks)} ask{'' if len(asks) == 1 else 's'} out"] if asks else []) + ([f"{len(bench)} on the bench could fill it"] if bench else [])
        return ", ".join(parts) or None
    waiting = not_answered(reg, ev)
    if not waiting:
        return ", ".join(head) or None
    names = [c.label for _m, c in waiting[:5]] + ([f"+{len(waiting) - 5} more"] if len(waiting) > 5 else [])
    k = len(waiting)
    noun = (f"{k} regular{'' if k == 1 else 's'}" if expected_from(reg, ev) == "regulars" else str(k))
    return ", ".join(head + [f"{noun} {'hasn' if k == 1 else 'haven'}'t answered: {', '.join(names)}"])


def answer_word(ev: RaidEvent, status: str | None, roster: int | None) -> str:
    """A member's own standing on a run in one word or two — the Me page's badge (design §5.56 phase 2): 'rostered'
    ('rostered · roster 2' on a split), 'not rostered' for a joiner the lock left out (never 'Join' on a locked run),
    else their answer's label, else 'not answered'. `roster` = their roster number (1-based) when they hold a seat."""
    if roster:
        return "rostered" + (f" · roster {roster}" if roster > 1 else "")
    if status == "in" and ev.state != "open" and ev.all_rosters:
        return "not rostered"
    return LABELS.get(status, status) if status else "not answered"


def balance_words(values: list[int | float]) -> str | None:
    """How a split's rosters compare, in words instead of the group-building numbers (design §5.56 phase 2): 'the two
    rosters are close' when the weakest is within a tenth of the strongest, else 'roster 2 is the weaker' ('… the
    weakest' for three or more). None for one roster or when there is nothing to compare."""
    vals = [float(v or 0) for v in values]
    if len(vals) < 2 or max(vals) <= 0:
        return None
    hi, lo = max(vals), min(vals)
    if hi - lo <= max(1.0, hi * 0.1):
        return "the two rosters are close" if len(vals) == 2 else "the rosters are close"
    return f"roster {vals.index(lo) + 1} is the {'weaker' if len(vals) == 2 else 'weakest'}"


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


FILL_STATE_WORDS = {"idle": "No seat to fill yet.", "asking": "Asking for open seats — waiting for answers.", "filled": "Every seat is filled.",
                    "exhausted": "Nobody left to ask — recruit, or move someone in on the board.",
                    "suggested": "Waiting for an officer: a role swap would cover a seat."}
# Pause automatic asks (D18): the paused state's words, before the fill state
AUTO_ASKS_PAUSED = "Automatic asks paused"


def open_seat_count(reg: Registry, ev: RaidEvent) -> int:
    """Empty seats on a locked run's roster(s): each roster's size minus who holds a seat."""
    if ev.state != "locked" or not ev.all_rosters:
        return 0
    size = run_size(reg, ev)
    return sum(max(0, size - len(r.selected)) for r in ev.all_rosters)


def open_seats_words(reg: Registry, ev: RaidEvent, n: int) -> str:
    """A locked run with `n` open seats and no ask out yet, in the glossary's words: how many, and whether automatic
    asks will ask for them (on), wait while paused (fill_state_text says paused first), or an officer fills them (off)."""
    head = f"{n} seat is open." if n == 1 else f"{n} seats are open."
    if ev.autofill_paused:
        return head
    if team_setting(run_team(reg, ev), "autofill"):
        return head + (" Automatic asks will ask for it." if n == 1 else " Automatic asks will ask for them.")
    return head + f" Automatic asks are off, so an officer fills {'it' if n == 1 else 'them'} with {fill_button_words()}."


def fill_state_text(reg: Registry, ev: RaidEvent) -> str:
    """How open seats are being filled, in sentences (the lock card, the SheetCard, MCP get_run) — never the bare code
    and never "engine": 'Waiting for an officer: a role swap would cover a seat.', 'Asking for open seats — waiting for
    answers (2 on the site).', 'Automatic asks paused by Jon. …'. A closed run says its state (RUN_STATE_WORDS)."""
    if ev.state == "open":
        return "Open seats are asked for after the lock; before it, Suggest… shows who could cover a minimum."
    if ev.state in ("done", "cancelled"):
        return f"The run is {RUN_STATE_WORDS[ev.state]}."
    words = FILL_STATE_WORDS.get(ev.fill_state, ev.fill_state)
    if ev.fill_state == "idle" and (n := open_seat_count(reg, ev)):
        words = open_seats_words(reg, ev, n)  # "No seat to fill yet." is false while seats are open
    site = sum(1 for a in ev.fill_asks if a.pending and a.channel == "web")
    if site:
        words = words[:-1] + f" ({site} on the site)."
    if ev.autofill_paused:
        words = AUTO_ASKS_PAUSED + (f" by {ev.autofill_paused_by}" if ev.autofill_paused_by else "") + ". " + words
    return words


def asks_on_site(reg: Registry, ev: RaidEvent) -> int:
    """How many asks in play wait on the members' Me pages (DMs off, or the DM failed): fill / pre-lock asks with
    channel "web" plus unanswered Confirm / Can't asks with channel "web" — the "N wait on the site" count."""
    fills = sum(1 for a in ev.fill_asks if a.pending and a.channel == "web")
    places = sum(1 for _m, a in open_placement_rows(reg, ev) if a.get("channel") == "web") if ev.state == "locked" else 0
    return fills + places


# a log line's officer-only marks: free text people wrote (a reason, a note) and planning a member never reads
_OFFICER_LOG = ("cancelled by", "lock (", "fill", "suggestion", "pre-lock", "pin ", "pinned", "board", "proposed", "reminded",
                "autofill", "automatic asks", "callout", "early warning", "confirmation dms")


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
        return (f"This run is already {RUN_STATE_WORDS[ev.state]}." if ev.state in ("done", "cancelled") else
                "End is for a locked run, and this sheet is still open.")
    if (at or datetime.now(ev.start.tzinfo)) < ev.start:
        return "The run hasn't started yet. Before the start, use Cancel instead."
    return None


# ---------------------------------------------------------------- character changes ripple into runs (design §3.3, D24)
# A character table save (Me page, Members page, /me char, /roster char, plain text, MCP — PeopleMixin.save_characters)
# can retire, delete, respec, name or promote a character that live sheets and locked rosters already hold. Sheets
# snapshot cls/spec/role at the press and the lock solves from the snapshot, so the run files are rewritten here (data
# only); seats to free go back to RaidMixin.drop_seated (free_seat + the card edit + the fill engine).

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
            out.append({"ev": None, "kind": "rank", "action": "none", "text": f"Rank {m.main.rank} moves to {label} with the main, and {m.main.label} becomes an alt."})
        return out
    if kind in ("retire", "delete") and char is not None:
        for k in list(char.rosters):
            from .consequences import series_words

            out.append({"ev": None, "kind": "regular", "action": "none",
                        "text": f"{label} stops being a regular of {series_words(reg, k)}, because {'retired' if kind == 'retire' else 'deleted'} characters aren't regulars."})
    for ev in rs.live():
        if not on_bench_side(reg, ev, m):
            continue
        words = run_on(reg, ev)
        sg = ev.signups.get(str(m.discord_id))
        seat = ev.seat_of(m) if ev.state != "open" else None
        if seat is not None and (seat[1].character or seat[1].signup_name) != label:
            seat = None
        if kind in ("retire", "delete", "spec") and any(a.discord_id == m.discord_id and a.pending and a.character == label for a in ev.fill_asks):
            out.append({"ev": ev, "kind": "ask", "action": "clear", "text": f"The open ask for {label} on {words} closes."})
        if seat is not None:
            role = seat[1].role
            if kind in ("retire", "delete"):
                out.append({"ev": ev, "kind": "seat", "action": "free",
                            "text": f"{label}'s {role} seat for {words} is released. {refill_sentence(reg, ev)}"})
            elif kind == "spec" and change["role"] == role:
                out.append({"ev": ev, "kind": "seat", "action": "rewrite", "text": f"{label}'s seat for {words} now reads {change['spec']}."})
            elif kind == "spec" and run_kind(reg, ev) == "recurring":
                out.append({"ev": ev, "kind": "seat", "action": "keep",
                            "text": f"{label} keeps the {role} seat for {words}, though {change['spec']} plays {change['role']}. The officers are told and decide."})
            elif kind == "spec":
                out.append({"ev": ev, "kind": "seat", "action": "free",
                            "text": f"{label}'s {role} seat for {words} is released, because {change['spec']} plays {change['role']}. "
                                    "On an ad hoc run, Join again to queue."})
            elif kind == "name":
                out.append({"ev": ev, "kind": "seat", "action": "rewrite", "text": f"The seat for {words} now reads {change['new_label']}."})
            continue
        if sg is None or sg.character != label:
            continue
        what = LABELS.get(sg.status, sg.status)
        if kind in ("retire", "delete"):
            other = next((c for c in m.active() if c.label != label), None)
            if sg.status == "out" and other is not None:
                out.append({"ev": ev, "kind": "answer", "action": "repoint", "text": f"The No thanks for {words} stays, now as {other.label}."})
            else:
                out.append({"ev": ev, "kind": "answer", "action": "clear",
                            "text": f"The {what} answer for {words} with {label} is cleared. Answer again with another character."})
        elif kind == "spec":
            out.append({"ev": ev, "kind": "answer", "action": "rewrite", "text": f"The {what} answer for {words} now reads {change['spec']}."})
        elif kind == "name":
            out.append({"ev": ev, "kind": "answer", "action": "rewrite", "text": f"The {what} answer for {words} now names {change['new_label']}."})
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


def site_words(reg: Registry, text: str) -> str:
    """A verb's Discord-flavoured line as the site prints it (the Clock rule): every native time stamp `<t:unix:X>`
    (raid_views.run_label's — Discord renders it in the reader's locale, the site can't) becomes the guild's 12-hour
    words (Registry.local12), the bold marks go and a leading ✅ goes. RaidMixin.set_answer / call_out's line on
    POST /api/run/{key}/set (Answer for someone) and the Events page's switch."""
    import re

    def stamp(mt: "re.Match") -> str:
        return reg.local12(datetime.fromtimestamp(int(mt.group(1)), tz=reg.tz))

    return re.sub(r"<t:(\d+)(?::[a-zA-Z])?>", stamp, str(text)).replace("**", "").removeprefix("✅ ")


def run_on(reg: Registry, ev: RaidEvent) -> str:
    """A run as a confirm reads it (the Confirm rule, design §5.57): '<raid> on <day>' ('Barrow Deeps on Tue 9 Dec') —
    the clock only where the sentence is about the time (run_words)."""
    return f"{reg.raid_def(ev.instance).get('name', ev.instance or ev.team)} on {reg.day_label(run_day(reg, ev))}"


def fill_button_words() -> str:
    """The officers' Fill seats button as a sentence names it (raid_buttons.RUN_WORDS, without its ellipsis)."""
    from .raid_buttons import RUN_WORDS

    return RUN_WORDS["fill"].rstrip("…").rstrip(".")


def refill_sentence(reg: Registry, ev: RaidEvent, it: str = "it") -> str:
    """What happens to a seat a release leaves open, as one sentence: the bench is asked only when the run's automatic
    asks are on and not paused (RaidMixin.drop_seated → run_fill); otherwise it waits for an officer's Fill seats.
    `it`: "it" for one seat, "them" for several."""
    if not team_setting(run_team(reg, ev), "autofill"):
        return f"The seat{'s wait' if it == 'them' else ' waits'} until an officer presses {fill_button_words()}."
    if ev.autofill_paused:
        return f"Automatic asks are paused, so the seat{'s wait' if it == 'them' else ' waits'} for {fill_button_words()}."
    return f"The bench is asked to fill {it}."


def no_confirm_sentence(reg: Registry, ev: RaidEvent, tail: str = "nobody is asked to confirm") -> str:
    """Why nobody confirms on this run (D5): 'On an ad hoc run Join is the commitment, so nobody is asked to confirm.'"""
    where = "On an ad hoc run" if run_kind(reg, ev) == "adhoc" else "On this run"
    return f"{where} Join is the commitment, so {tail}."


def hours_words(h: float) -> str:
    """24 → '24 hours', 1 → '1 hour', 1.5 → '1.5 hours'."""
    return f"{h:g} hour" + ("" if float(h) == 1 else "s")


def open_rules(reg: Registry, instance: str, sid: str | None, kind: str) -> list[str]:
    """What kind of run an open makes, in sentences (open_preview's `rules`, the /raid open wizard's confirm)."""
    sd = reg.schedule_def(instance, sid)
    lock = hours_words(float(sd["lock_hours_before"]))
    if kind == "adhoc":
        return [f"It's an ad hoc run, so seats go {FIRST_COME}.",
                "There are no regulars or reminders" + (", and the rostered are asked to confirm." if sd.get("confirm") else ", and Join is the commitment."),
                f"It locks {lock} before the start, and a role swap waits for an officer."]
    return ["It's a scheduled run, so its regulars are expected." + (" The rostered are asked to confirm." if sd.get("confirm", True) else ""),
            f"It locks {lock} before the start."]


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
            reg.notify(a.discord_id, "ask_closed", f"Never mind the seat question for {run_words(reg, ev)} — someone else took the seat first.", run=ev.key)
    if gone:
        rs.save(ev, f"fill: parallel ask(s) closed for {ask.display_name}'s yes")
    return gone


def seat_gone(reg: Registry, ev: RaidEvent, uid: int, why: str, *, notice: bool = True) -> None:
    """A seat was freed (free_seat — every path: an officer, the board, an absence, expiry, a Can't make it): the member's
    OPEN Confirm ask on this run closes (`released`; a Confirm pressed later on the Me page or an old card is then
    refused from one place) and — unless it was their own doing, or an officer's on-behalf write that tells them
    itself — their inbox says why. `notice=False`: the member's own act by another road (retiring / deleting their own
    character) — no notice about what they just did. Never a DM (the card is RaidMixin.seat_released's to edit)."""
    reg.close_placement_ask(uid, ev.team, "released")
    if not notice or why in ("declined", "callout", "callout (test)", "officer"):
        return
    reason = RELEASE_NOTICE.get(why, why)
    reg.notify(uid, "released", f"Your seat on {run_words(reg, ev)} was released ({reason}). If that's wrong, tell an officer.", run=ev.key)


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
            reg.notify(uid, "cancelled", f"{run_words(reg, ev)} was cancelled by {by}." + (" Your seat is released." if seated else ""), run=ev.key)
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
    if ev.follows and m.discord_id in ev.leaving:  # leaving early (design §5.62): not a No thanks
        name = reg.raid_def(ev.instance).get("name", ev.instance)
        return f"You're leaving after {lead_name_of(reg, ev) or 'the first raid'}, so you're out of {name}."
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
                    return f"You're **joined** as {as_} for {run} — {_ordinal(pos)} of {seats} seats, in if nothing changes ({FIRST_COME})."
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
                 "pin_in": "Pin to roster", "pin_out": "Keep off the roster", "unpin": "Unpin"}


def member_actions(reg: Registry, ev: RaidEvent, m: Member) -> list[dict]:
    """What an officer can record for `m` on `ev` right now — ONLY the verbs that apply (the SetWizard's buttons and the
    site's row ⋮ menu read this one list): Confirm / Can't make it for their open Confirm ask · Confirm / No to their
    open fill ask · Record a callout (with an optional note; before lock it is No thanks + the callout) · Join (as
    <alt>) · Bench · No thanks (an answer change: no callout record) · Pin to roster / Keep off the roster / Unpin (open
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
    """The sheet footer's pin line (B5, numbers only — who is officer data): 'Pinned: 2 · Kept off: 1', or ''."""
    on = {str(sg.discord_id) for sg in ev.signups.values()}
    ins = sum(1 for u, v in ev.pins.items() if v == "in" and u in on)
    outs = sum(1 for u, v in ev.pins.items() if v == "out" and u in on)
    return " · ".join(p for p in ((f"Pinned: {ins}" if ins else ""), (f"Kept off: {outs}" if outs else "")) if p)
