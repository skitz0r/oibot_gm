"""Weekly raid cycle: real signup sheets from the registry, cutoffs, health
check, lock → propose. No LLM anywhere in this module."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .models import Player, RosterResult
from .profiles import GameProfile
from .registry import Member, Registry, RegisteredCharacter, now
from .roster import explain, solver
from .store import GitStore

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
STATUSES = ("in", "tentative", "out", "sub")
TEAM_DEFAULTS = {"cutoff_soft_hours": 48, "cutoff_hard_hours": 24, "open_days_before": 6, "reminders": "dm"}


class Signup(BaseModel):
    discord_id: int
    display_name: str
    character: str
    cls: str
    spec: str
    role: str
    status: str  # in | tentative | out | sub
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


class RaidEvent(BaseModel):
    key: str  # <team>-<YYYY-MM-DD>
    team: str
    instance: Optional[str] = None
    starts_at: str  # ISO with offset
    state: str = "open"  # open | locked | proposed | accepted | done | cancelled
    signups: dict[str, Signup] = Field(default_factory=dict)  # discord_id -> signup
    callouts: list[Callout] = Field(default_factory=list)
    channel_id: Optional[int] = None
    message_id: Optional[int] = None
    thread_id: Optional[int] = None
    roster: Optional[RosterResult] = None
    health_posted: bool = False
    nudged: list[int] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.starts_at)

    def by_status(self, status: str) -> list[Signup]:
        return sorted([s for s in self.signups.values() if s.status == status], key=lambda s: s.updated_at)

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


# ---------------------------------------------------------------- events on the store

class RaidStore:
    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key
        self.events: dict[str, RaidEvent] = {}
        d = store.root / guild_key / "raids"
        if d.exists():
            for f in d.glob("*.json"):
                ev = RaidEvent.model_validate_json(f.read_text())
                self.events[ev.key] = ev

    def save(self, ev: RaidEvent, message: str) -> None:
        self.store.write_text(ev.rel_path(self.key), ev.model_dump_json(indent=1))
        self.events[ev.key] = ev
        self.store.commit(f"{self.key}: raid {ev.key}: {message}")

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
    ev.log.append(f"opened; prefilled {len(ev.signups)} from availability/absences")
    rs.save(ev, "opened")
    return ev


def prefill(reg: Registry, ev: RaidEvent, team: dict) -> None:
    """Standing availability + absences seed the sheet so people act on exceptions."""
    day = ev.start.date().isoformat()
    for m in reg.members.values():
        main = m.main
        if not main:
            continue
        absence = m.absent_on(day)
        avail = m.availability.get(team["key"])
        if absence:
            status, source = "out", "absence"
        elif avail == "in":
            status, source = "in", "prefill"
        elif avail == "sub":
            status, source = "sub", "prefill"
        elif avail == "out":
            status, source = "out", "prefill"
        else:
            continue  # unset: they must respond
        ev.signups[str(m.discord_id)] = _signup(reg, m, main, status, source)


def _signup(reg: Registry, m: Member, c: RegisteredCharacter, status: str, source: str, note: str | None = None) -> Signup:
    return Signup(discord_id=m.discord_id, display_name=m.display_name, character=c.name, cls=c.cls, spec=c.spec, role=reg.profile.spec(c.cls, c.spec).role, status=status, source=source, note=note)


def set_signup(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, character: str | None, status: str, source: str = "member", note: str | None = None) -> Signup:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    c = next((c for c in m.active() if c.name.lower() == (character or "").lower()), None) if character else m.main
    if not c:
        raise ValueError("no such active character")
    s = _signup(reg, m, c, status, source, note)
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"{m.display_name} {status} as {c.name} ({source})")
    rs.save(ev, f"{m.display_name} {status}")
    return s


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


# ---------------------------------------------------------------- health

def health(reg: Registry, ev: RaidEvent, team: dict) -> list[tuple[str, str]]:
    """[(level, line)] with level in green|amber|red."""
    profile = reg.profile
    ins = ev.by_status("in")
    tent = ev.by_status("tentative")
    subs = ev.by_status("sub")
    size = int(team.get("size", 20))
    rules = profile.comp_rules
    out: list[tuple[str, str]] = []
    # headcount
    n = len(ins)
    lvl = "green" if n >= size else ("amber" if n + len(tent) >= size else "red")
    out.append((lvl, f"Headcount {n}/{size} in, {len(tent)} tentative, {len(subs)} sub"))
    # roles
    counts = {r: sum(1 for s in ins if s.role == r) for r in ("tank", "healer", "melee", "ranged")}
    bounds = solver.scaled_role_bounds(rules, size)
    need_t = (profile.raids.get(ev.instance or "", {}).get("tank_needs", {}) or {}).get("count") or bounds["tank"]["min"]
    need_h = bounds["healer"]["min"]
    for role, need in (("tank", need_t), ("healer", need_h)):
        have = counts[role]
        if have >= need:
            out.append(("green", f"{role.title()}s {have}/{need}"))
        else:
            fix = [s.display_name for s in tent + subs if s.role == role]
            alt = []
            for m in reg.members.values():
                for c in m.active():
                    if reg.profile.spec(c.cls, c.spec).role == role and str(m.discord_id) not in ev.signups:
                        alt.append(f"{m.display_name} ({c.name})")
            hint = (f"; tentative/sub: {', '.join(fix)}" if fix else "") + (f"; not on sheet: {', '.join(alt[:4])}" if alt else "")
            out.append(("red" if have < need - 1 else "amber", f"{role.title()}s {have}/{need}{hint}"))
    # buffs nobody brings
    missing = []
    for b in profile.party_buffs():
        if not any(b.provided_by(profile.spec(s.cls, s.spec)) for s in ins):
            missing.append(b.name.split(" (")[0])
    if missing:
        out.append(("amber", "No provider for: " + ", ".join(missing)))
    # reliability: late callouts recently (from this store's other events) is future work
    unresp = [m.display_name for m in reg.members.values() if m.main and str(m.discord_id) not in ev.signups]
    if unresp:
        out.append(("amber" if len(unresp) > 3 else "green", f"No response from {len(unresp)}: " + ", ".join(unresp[:8]) + ("…" if len(unresp) > 8 else "")))
    return out


# ---------------------------------------------------------------- lock → propose

def players_for(reg: Registry, ev: RaidEvent) -> list[Player]:
    players = []
    for i, s in enumerate(sorted(ev.signups.values(), key=lambda s: s.updated_at)):
        if s.status == "out":
            continue
        c = reg.find(s.character)
        rank = c[1].rank if c else "unknown"
        players.append(Player(signup_name=s.display_name, pos=i + 1, status="signed" if s.status in ("in", "tentative") else "bench", cls=s.cls, spec=s.spec, role=s.role, character=s.character, map_confidence="high", unmapped=False, note="tentative" if s.status == "tentative" else None, rank=rank))
    return players


def propose(reg: Registry, rs: RaidStore, ev: RaidEvent) -> tuple[list[Player], RosterResult]:
    players = players_for(reg, ev)
    raid_id = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
    team = reg.config.team(ev.team) or {}
    size = int(team.get("size") or reg.profile.raids.get(raid_id, {}).get("size") or reg.profile.comp_rules["raid_size"])
    result = solver.solve(reg.profile, players, raid_id, solver.SolveOptions(time_limit_s=12, raid_size=size))
    result = explain.annotate(reg.profile, players, raid_id, result, whatif=len(players) <= 30)
    ev.roster = result
    ev.state = "proposed"
    ev.log.append(f"proposed {len(result.selected)} in / {len(result.benched)} bench")
    rs.save(ev, "proposed")
    return players, result
