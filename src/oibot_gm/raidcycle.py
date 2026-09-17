"""Signup-driven raid cycle: one sheet per raid slot per lockout window (opened on cadence), Join / Bench / No thanks,
health check, lock → roster(s) from the signups (weights + officer pins), confirmations, seat freeing and filling.
No LLM anywhere in this module."""
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
STATUSES = ("in", "sub", "out")  # Join / Bench / No thanks (legacy "tentative" is read as "in")
LABELS = {"in": "Join", "sub": "Bench", "out": "No thanks"}
MAX_ROSTERS_PER_SLOT = 4
TEAM_DEFAULTS = {"cutoff_soft_hours": 48, "cutoff_hard_hours": 24, "open_days_before": 6, "reminders": "dm", "open_dm": False, "autofill": True}
RANK_ORDER = {"core": 0, "raider": 1, "trial": 2, "social": 3, "alt": 4}
FILL_OVERASK = 1  # ask one more person than the shortfall per batch
FILL_MAX_OPEN = 3  # never more than this many unanswered asks per event


class Signup(BaseModel):
    discord_id: int
    display_name: str
    character: str
    cls: str
    spec: str
    offspec: Optional[str] = None
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
    answer: Optional[str] = None  # yes | no | expired
    answered_at: Optional[str] = None

    @property
    def open(self) -> bool:
        return self.answer is None


class RaidEvent(BaseModel):
    key: str  # <run roster key>-<YYYY-MM-DD>
    team: str  # roster key; for cadence runs an ephemeral roster named like the run (bd-1209-1930)
    instance: Optional[str] = None
    starts_at: str  # ISO with offset
    state: str = "open"  # open | locked | proposed | accepted | done | cancelled
    signups: dict[str, Signup] = Field(default_factory=dict)  # discord_id -> signup
    pins: dict[str, str] = Field(default_factory=dict)  # discord_id -> "in" | "out": officer decisions the solver must honour
    rosters: list[RosterResult] = Field(default_factory=list)  # after lock: one or more rosters for this slot (roster = rosters[0])
    locked_at: Optional[str] = None
    confirm_by: Optional[str] = None  # ISO: unanswered confirmations expire here
    callouts: list[Callout] = Field(default_factory=list)
    channel_id: Optional[int] = None
    message_id: Optional[int] = None
    thread_id: Optional[int] = None
    roster: Optional[RosterResult] = None
    health_posted: bool = False
    nudged: list[int] = Field(default_factory=list)
    fill_asks: list[FillAsk] = Field(default_factory=list)
    fill_state: str = "idle"  # idle | asking | filled | exhausted
    log: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.starts_at)

    def by_status(self, status: str) -> list[Signup]:
        return sorted([s for s in self.signups.values() if s.status == status], key=lambda s: s.updated_at)

    @property
    def all_rosters(self) -> list[RosterResult]:
        return self.rosters or ([self.roster] if self.roster else [])

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


def ensure_run(reg: Registry, instance: str, start: datetime, by: str = "scheduler") -> dict:
    """The ephemeral roster that carries one run's settings (size, cutoffs from the raid's cadence); created once."""
    key = run_key(instance, start)
    t = reg.config.roster(key)
    if t:
        return t
    rd = reg.raid_def(instance)
    lead, lock, confirm = float(rd["signup_lead_hours"]), float(rd["lock_hours_before"]), float(rd["confirm_hours_before"])
    t = {"key": key, "name": f"{rd.get('name', instance)} {start.strftime('%a %d %b %H:%M')}", "size": int(rd.get("size") or 20), "schedule": start.strftime("%a %H:%M"), "instance": instance,
         "cutoff_soft_hours": max(lock, min(48, lead / 2)), "cutoff_hard_hours": lock, "confirm_hours": confirm, "open_days_before": lead / 24, "reminders": "dm", "open_dm": False, "autofill": True, "ephemeral": True}
    reg.config.rosters.append(t)
    reg.save_config(f"run {key} ({t['name']}) created by {by}", notify=False)
    return t


def open_run(reg: Registry, rs: "RaidStore", instance: str, start: datetime, by: str = "scheduler") -> "RaidEvent":
    return open_event(reg, rs, ensure_run(reg, instance, start, by), start)


# ---------------------------------------------------------------- events on the store

class RaidStore:
    def __init__(self, store: GitStore, guild_key: str):
        self.store, self.key = store, guild_key
        self.events: dict[str, RaidEvent] = {}
        d = store.root / guild_key / "raids"
        if d.exists():
            for f in d.glob("*.json"):
                ev = RaidEvent.model_validate_json(f.read_text())
                for sg in ev.signups.values():
                    if sg.status == "tentative":  # pre-Join/Bench sheets
                        sg.status = "in"
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
    ev.log.append(f"opened; {len(ev.signups)} pre-filled from absences")
    rs.save(ev, "opened")
    return ev


def prefill(reg: Registry, ev: RaidEvent, team: dict) -> None:
    """Standing availability + absences seed the sheet so people act on exceptions."""
    day = ev.start.date().isoformat()
    explicit = bool(reg.roster_members(team["key"]))
    for m, main in reg.roster_pool(team["key"]):
        if not main:
            continue
        absence = m.absent_on(day)
        avail = m.availability.get(team["key"])
        if absence:
            status, source = "out", "absence"
        elif avail == "in" or (explicit and avail is None):
            status, source = "in", "prefill"  # team members default in
        elif avail == "sub":
            status, source = "sub", "prefill"
        elif avail == "out":
            status, source = "out", "prefill"
        else:
            continue  # unset, no explicit team: they must respond
        ev.signups[str(m.discord_id)] = _signup(reg, m, main, status, source)


def _signup(reg: Registry, m: Member, c: RegisteredCharacter, status: str, source: str, note: str | None = None) -> Signup:
    return Signup(discord_id=m.discord_id, display_name=m.display_name, character=c.label, cls=c.cls, spec=c.spec, offspec=c.offspec, role=reg.profile.spec(c.cls, c.spec).role, status=status, source=source, note=note)


def set_signup(reg: Registry, rs: RaidStore, ev: RaidEvent, m: Member, character: str | None, status: str, source: str = "member", note: str | None = None) -> Signup:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    c = next((c for c in m.active() if (c.name or c.label).lower() == (character or "").lower()), None) if character else m.main
    if not c:
        raise ValueError("no such active character")
    team = reg.config.team(ev.team) or {"key": ev.team}
    if status == "in" and reg.roster_members(team["key"]) and not reg.on_roster(m, team["key"]) and source == "member":
        status, note = "sub", "not on this roster; subs are picked when needed"
    s = _signup(reg, m, c, status, source, note)
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"{m.display_name} {status} as {c.label} ({source})")
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


# ---------------------------------------------------------------- filling gaps

def conflicts(rs: RaidStore, ev: RaidEvent, window_hours: float = 4.0) -> dict[int, str]:
    """discord_id -> other raid key, for people In/Tentative on another live raid within the window."""
    out: dict[int, str] = {}
    for other in rs.live():
        if other.key == ev.key:
            continue
        if abs((other.start - ev.start).total_seconds()) > window_hours * 3600:
            continue
        for s in other.signups.values():
            if s.status in ("in", "tentative"):
                out[s.discord_id] = other.key
    return out


def needs(reg: Registry, ev: RaidEvent, team: dict) -> dict:
    """What the sheet is short: headcount and per-role minimums. Before lock: Join signups vs the raid size.
    After lock: freed seats across the locked roster(s)."""
    size = int(team.get("size") or reg.raid_def(ev.instance).get("size") or 20)
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
    n_in, size, _tent, _subs = h["headcount"]
    role_short = {r["role"]: r["need"] - r["have"] for r in h["roles"] if r["need"] and r["have"] < r["need"]}
    return {"headcount": max(0, size - n_in), "roles": role_short, "size": size}


def _rank_key(reg: Registry, m: Member) -> tuple:
    c = m.main
    return (RANK_ORDER.get(c.rank if c else "trial", 9), m.created_at)


def fill_candidates(reg: Registry, rs: RaidStore, ev: RaidEvent, team: dict) -> list[FillAsk]:
    """Ordered list of people to ask, best first, for the sheet's current needs. Pure; nothing is sent.
    Order: role gaps first (a sub/pool member of that role, else an In player's offspec or alt),
    then plain headcount (subs → roster pool not on the sheet → other rosters' members free that night)."""
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
    subs = [(m, c) for s in benched_joiners + ev.by_status("sub") if (m := reg.members.get(s.discord_id)) and (c := next((c for c in m.active() if c.label == s.character), m.main))]
    unresponsive = [(m, c) for m, c in reg.roster_pool(team["key"]) if str(m.discord_id) not in ev.signups and c]
    others = sorted([(m, m.main) for m in reg.members.values() if m.main and m.discord_id not in pool_ids and str(m.discord_id) not in ev.signups and m.availability.get(team["key"]) != "out"], key=lambda mc: _rank_key(reg, mc[0]))
    tiers = [("sub", subs), ("pool", unresponsive), ("other_roster", others)]

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
            if s.offspec and reg.profile.spec(s.cls, s.offspec).role == role:
                out.append(FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="offspec", role=role, character=s.character, spec=s.offspec, reason=f"short {short} {role}"))
                used.add(sid)
                got += 1
                continue
            alt = next((a for a in m.active() if a.label != s.character and role_of(a) == role), None)
            if alt:
                out.append(FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="alt", role=role, character=alt.label, spec=alt.spec, reason=f"short {short} {role}"))
                used.add(sid)
                got += 1
    # 2. headcount (role-gap asks that bring a new body count towards it)
    remaining = nd["headcount"] - sum(1 for a in out if a.kind in ("sub", "pool", "other_roster"))
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


def apply_fill_answer(reg: Registry, rs: RaidStore, ev: RaidEvent, ask: FillAsk, yes: bool) -> str:
    """Record the answer and, on yes, put the person on the sheet the way they were asked."""
    ask.answer, ask.answered_at = ("yes" if yes else "no"), now()
    m = reg.members.get(ask.discord_id)
    if not yes or not m:
        ev.log.append(f"fill: {ask.display_name} declined ({ask.kind})")
        rs.save(ev, f"fill {ask.display_name} no")
        return f"{ask.display_name} can't ({ask.kind})"
    if ask.kind == "offspec":
        s = ev.signups[str(ask.discord_id)]
        s.spec, s.offspec, s.role, s.note, s.source, s.updated_at = ask.spec, s.spec, ask.role, f"switched to {ask.spec} at request", "fill", now()
        ev.log.append(f"fill: {ask.display_name} switches to {ask.spec}")
        rs.save(ev, f"fill {ask.display_name} offspec")
        return f"{ask.display_name} switches to {ask.spec} ({ask.role})"
    character = ask.character if ask.kind == "alt" else ask.character
    c = next((c for c in m.active() if c.label == character), m.main)
    s = _signup(reg, m, c, "in", "fill", "filled in at request" if ask.kind != "alt" else f"on alt {c.label} at request")
    ev.signups[str(m.discord_id)] = s
    ev.log.append(f"fill: {ask.display_name} in as {c.label} ({ask.kind})")
    if ev.state != "open" and ev.all_rosters:
        seat_player(reg, ev, s)
    rs.save(ev, f"fill {ask.display_name} in")
    return f"{ask.display_name} is in as {c.label} ({ask.role})"


# ---------------------------------------------------------------- health

def health_data(reg: Registry, ev: RaidEvent, team: dict) -> dict:
    """Structured health check: headcount, per-role tiles, per-buff providers, non-responders."""
    profile = reg.profile
    ins, tent, subs = ev.by_status("in"), ev.by_status("tentative"), ev.by_status("sub")
    size = int(team.get("size") or reg.raid_def(ev.instance).get("size") or 20)
    bounds = reg.role_bounds(ev.instance, size)
    counts = {r: sum(1 for s in ins if s.role == r) for r in ("tank", "healer", "melee", "ranged")}
    flex = {r: sum(1 for s in tent + subs if s.role == r) for r in counts}
    need = {
        "tank": bounds["tank"]["min"],
        "healer": bounds["healer"]["min"],
        "melee": 0,
        "ranged": 0,
    }
    roles = []
    for r in ("tank", "healer", "melee", "ranged"):
        have, n = counts[r], need[r]
        cover = {"offspec": [], "flex": [], "alt": [], "tent/sub": []}
        if n and have < n:
            for sg in ins:
                m = reg.members.get(sg.discord_id)
                if sg.offspec and profile.spec(sg.cls, sg.offspec).role == r and sg.role != r:
                    cover["offspec"].append(f"{sg.display_name} ({sg.offspec})")
                elif m and r in reg.roles_of(m)[1] and sg.role != r:
                    cover["flex"].append(sg.display_name)
                elif m and any(c.status in ("active", "planned") and not c.is_main and profile.spec(c.cls, c.spec).role == r for c in m.characters) and sg.role != r:
                    cover["alt"].append(sg.display_name)
            cover["tent/sub"] = [sg.display_name for sg in tent + subs if sg.role == r]
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
    hc_level = "green" if len(ins) >= size else ("amber" if len(ins) + len(tent) >= size else "red")
    return {"headcount": (len(ins), size, len(tent), len(subs)), "headcount_level": hc_level, "roles": roles, "buffs": buffs, "unresponsive": unresp}


def health(reg: Registry, ev: RaidEvent, team: dict) -> list[tuple[str, str]]:
    """[(level, line)] text form of health_data, for logs and ops lines."""
    h = health_data(reg, ev, team)
    n, size, tent, subs = h["headcount"]
    out = [(h["headcount_level"], f"Headcount {n}/{size} in, {tent} tentative, {subs} sub")]
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
        players.append(Player(signup_name=s.display_name, pos=i + 1, status="signed" if s.status in ("in", "tentative") else "bench", cls=s.cls, spec=s.spec, role=s.role, offspec=s.offspec, character=s.character, map_confidence="high", unmapped=False, note="tentative" if s.status == "tentative" else None, rank=rank, tank_capable_main=tank_alt))
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


def propose(reg: Registry, rs: RaidStore, ev: RaidEvent, save: bool = True) -> tuple[list[Player], RosterResult]:
    """Roster(s) for a sheet from its signups: weights + officer pins, the raid's role bounds, the comp policy.
    When more Join signups remain than a full run needs (with its tank/healer minimums), the solver runs again
    on the remainder — up to MAX_ROSTERS_PER_SLOT rosters for one slot."""
    players = players_for(reg, ev)
    raid_id = ev.instance if ev.instance in reg.profile.raids else next(iter(reg.profile.raids))
    team = reg.config.team(ev.team) or {}
    size = int(team.get("size") or reg.raid_def(raid_id).get("size") or reg.profile.comp_rules["raid_size"])
    rb = reg.role_bounds(raid_id, size)
    from .policy import PolicyStore

    cc = PolicyStore(rs.store, rs.key).comp_constraints()
    names = {p.character: p.signup_name for p in players} | {p.signup_name: p.signup_name for p in players}
    resolve = lambda n: names.get(n) or next((v for k, v in names.items() if k.lower() == n.lower()), None)  # noqa: E731
    by_uid = {str(sg.discord_id): sg.display_name for sg in ev.signups.values()}
    pin_in = tuple(by_uid[u] for u, v in ev.pins.items() if v == "in" and u in by_uid)
    pin_out = tuple(by_uid[u] for u, v in ev.pins.items() if v == "out" and u in by_uid)
    bonus = seat_bonus(reg, rs, ev, players)
    rosters: list[RosterResult] = []
    remaining = players
    while remaining:
        pool_names = {p.signup_name for p in remaining}
        opts = solver.SolveOptions(
            time_limit_s=12,
            raid_size=size,
            keep_together=tuple((resolve(a), resolve(b)) for a, b in cc["keep_together"] if resolve(a) and resolve(b)),
            keep_apart=tuple((resolve(a), resolve(b)) for a, b in cc["keep_apart"] if resolve(a) and resolve(b)),
            force_in=tuple(x for x in list(pin_in) + [resolve(n) for n in cc["never_bench"]] if x and x in pool_names and x not in pin_out),
            force_out=tuple(x for x in list(pin_out) + [resolve(n) for n in cc["always_bench"]] if x and x in pool_names),
            prefer_group={resolve(n): g for n, g in cc["prefer_group"].items() if resolve(n)},
            role_min=cc["role_min"] or {r: rb[r]["min"] for r in ("tank", "healer") if rb[r]["min"]} or None,
            role_max={r: rb[r]["max"] for r in ("tank", "healer") if rb[r].get("max")},
            bonus=bonus,
        )
        result = solver.solve(reg.profile, remaining, raid_id, opts)
        result = explain.annotate(reg.profile, remaining, raid_id, result, whatif=len(remaining) <= 30)
        rosters.append(result)
        chosen = {p.signup_name for p in result.selected}
        remaining = [p for p in remaining if p.signup_name not in chosen and p.status == "signed" and p.signup_name not in pin_out]
        if len(rosters) >= MAX_ROSTERS_PER_SLOT or len(remaining) < size:
            break
        if any(_capable(remaining, role, reg) < rb[role]["min"] for role in ("tank", "healer")):
            break
    seated = {p.signup_name for r in rosters for p in r.selected}
    rosters[0].benched = [p for p in players if p.signup_name not in seated]
    ev.rosters, ev.roster = rosters, rosters[0]
    if save:
        ev.state = "proposed" if ev.state in ("open", "locked", "proposed") else ev.state
        ev.log.append("proposed " + " + ".join(str(len(r.selected)) for r in rosters) + f" in / {len(rosters[0].benched)} bench")
        rs.save(ev, "proposed")
    return players, rosters[0]


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
                        "roster": i + 1, "answer": (ask or {}).get("answer"), "asked_at": (ask or {}).get("asked_at")})
    return out


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
    team = reg.config.team(ev.team) or {}
    size = int(team.get("size") or reg.raid_def(ev.instance).get("size") or 20)
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
    """Past confirm_by: unanswered confirmations count as out and their seats open."""
    if not ev.confirm_by or datetime.fromisoformat(ev.confirm_by) > datetime.now(ev.start.tzinfo):
        return []
    gone = []
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
