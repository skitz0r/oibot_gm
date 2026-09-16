"""Auto-planner: for a raid, find the best windows in its next lockout period from members' availability
grids, make as many runs as the character bank supports, seat them with the builder, and keep only runs
that are viable (≥ MIN_FILL of the size, no tank/healer shortfall). Proposals are persisted under
<guild>/proposals/ and go to officers by DM for Accept / Reject; acceptance turns each run into a dated
roster with an open sheet (the sheet is the members' verification). Deterministic."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..registry import Registry, now
from . import builder

MIN_FILL = 0.8
MAX_RUNS = 4
LEAD_HOURS = 20  # never propose a run sooner than this
STEP_MIN = 30


class Run(BaseModel):
    key: str  # roster key created on accept, e.g. bd-0924-2000
    name: str
    instance: str
    size: int
    starts_at: str  # ISO with offset (guild tz)
    slot: str  # "Thu 20:00"
    seats: list[dict]  # {discord_id, display_name, character, cls, spec, role, reasons}
    shortfalls: dict[str, int] = Field(default_factory=dict)


class Proposal(BaseModel):
    id: str
    instance: str
    window_start: str
    window_end: str
    runs: list[Run]
    unplaced: list[tuple[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    state: str = "proposed"  # draft (best effort, not viable) | proposed | accepted | rejected | expired
    viable: bool = True
    problems: list[str] = Field(default_factory=list)
    asked: list[int] = Field(default_factory=list)  # officer ids DMed
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    created_at: str = Field(default_factory=now)

    def rel_path(self, guild_key: str) -> Path:
        return Path(guild_key) / "proposals" / f"{self.id}.json"


class ProposalStore:
    def __init__(self, store, guild_key: str):
        self.store, self.key = store, guild_key
        self.items: dict[str, Proposal] = {}
        d = store.root / guild_key / "proposals"
        if d.exists():
            for f in d.glob("*.json"):
                p = Proposal.model_validate_json(f.read_text())
                self.items[p.id] = p

    def save(self, p: Proposal, message: str) -> None:
        self.store.write_text(p.rel_path(self.key), p.model_dump_json(indent=1))
        self.items[p.id] = p
        self.store.commit(f"{self.key}: proposal {p.id}: {message}")

    def open_for(self, instance: str) -> list[Proposal]:
        return [p for p in self.items.values() if p.instance == instance and p.state == "proposed"]

    def drafts_for(self, instance: str) -> list[Proposal]:
        return [p for p in self.items.values() if p.instance == instance and p.state == "draft"]

    def drop(self, p: Proposal, message: str) -> None:
        try:
            (self.store.root / p.rel_path(self.key)).unlink()
        except FileNotFoundError:
            pass
        self.items.pop(p.id, None)
        self.store.commit(f"{self.key}: proposal {p.id}: {message}")


ABBR = {"barrow_deeps": "bd", "hyjal_summit_forever": "hs", "onyxias_lair": "ony"}


def abbr(instance: str) -> str:
    return ABBR.get(instance) or "".join(w[0] for w in instance.split("_"))[:3]


def eligible_pool(reg: Registry, rs, instance: str, window_start: datetime, window_end: datetime) -> list:
    """Members with a main who could run this instance in the window (not locked out for it, have a grid)."""
    shells = [builder.Shell(key="probe", name="probe", instance=instance, size=1, start=window_start, slot="")]
    locked = builder.recent_runs(reg, rs, shells)
    out = []
    for m in reg.members.values():
        if not m.main:
            continue
        if any("probe" in locked.get((c.label, instance), set()) for c in m.active()):
            if all("probe" in locked.get((c.label, instance), set()) for c in m.active()):
                continue
        out.append(m)
    return out


def fallback_windows(reg: Registry, instance: str, window_start: datetime, window_end: datetime, k: int) -> list[tuple[datetime, int, int]]:
    """No grids to score: use the guild's candidate slots inside the window, else 19:00 on evenly spaced days."""
    from ..raidcycle import next_raid_time

    z = ZoneInfo(reg.config.timezone)
    out = []
    for slot in reg.config.slots:
        try:
            t = next_raid_time(slot, reg.config.timezone, after=window_start - timedelta(minutes=1))
        except ValueError:
            continue
        if window_start <= t <= window_end:
            out.append((t, 0, 0))
    if not out:
        days = max(1, int((window_end - window_start).total_seconds() // 86400))
        for i in range(min(k, days)):
            day = (window_start.astimezone(z) + timedelta(days=i * max(1, days // max(1, k)))).date()
            t = datetime(day.year, day.month, day.day, 19, 0, tzinfo=z)
            if window_start <= t <= window_end:
                out.append((t, 0, 0))
    return out[:k]


def candidate_windows(reg: Registry, members: list, instance: str, window_start: datetime, window_end: datetime, k: int) -> list[tuple[datetime, int, int]]:
    """Top-k non-overlapping (start, preferred count, available count) windows for the raid's duration."""
    rd = reg.raid_def(instance)
    hours = float(rd.get("duration_hours", 3))
    z = ZoneInfo(reg.config.timezone)
    t = window_start.astimezone(z).replace(second=0, microsecond=0)
    t = t + timedelta(minutes=(STEP_MIN - t.minute % STEP_MIN) % STEP_MIN)
    scored = []
    while t + timedelta(hours=hours) <= window_end:
        day = t.date().isoformat()
        p = a = 0
        for m in members:
            if m.absent_on(day):
                continue
            lvl = reg.week_level(m, t, hours)
            if lvl == "preferred":
                p += 1
            elif lvl == "available":
                a += 1
        if p + a:
            scored.append((t, p, a))
        t += timedelta(minutes=STEP_MIN)
    scored.sort(key=lambda x: (-(2 * x[1] + x[2]), x[0]))
    chosen: list[tuple[datetime, int, int]] = []
    for cand in scored:
        if len(chosen) >= k:
            break
        if all(abs((cand[0] - c[0]).total_seconds()) >= (hours + 1) * 3600 for c in chosen):
            chosen.append(cand)
    chosen.sort(key=lambda x: x[0])
    return chosen


def plan(reg: Registry, rs, instance: str, start: datetime | None = None) -> Proposal | None:
    """Propose runs of `instance` for its next lockout window. None when nothing viable."""
    rd = reg.raid_def(instance)
    size = int(rd.get("size") or 20)
    z = ZoneInfo(reg.config.timezone)
    earliest = (start or datetime.now(z)) + timedelta(hours=LEAD_HOURS)
    ws, we = reg.lockout_window(instance, earliest)
    if we - earliest < timedelta(hours=float(rd.get("duration_hours", 3)) + 12):
        ws, we = reg.lockout_window(instance, we + timedelta(minutes=1))  # too little of this window left: plan the next one
    window_start, window_end = max(ws, earliest), we
    members = eligible_pool(reg, rs, instance, window_start, window_end)
    problems: list[str] = []
    if not members:
        return None
    gridded = [m for m in members if m.week]
    if len(members) < size * MIN_FILL:
        problems.append(f"only {len(members)} eligible main(s) for a {size}-player raid")
    if not gridded:
        problems.append("nobody has filled the availability grid — windows are guesses")
    elif len(gridded) < len(members):
        problems.append(f"{len(members) - len(gridded)} member(s) have no availability grid and are assumed free")
    k = min(MAX_RUNS, max(1, len(members) // size))
    wins = candidate_windows(reg, gridded, instance, window_start, window_end, k) if gridded else []
    if not wins:
        wins = fallback_windows(reg, instance, window_start, window_end, k)
    if not wins:
        return None
    shells = []
    for t, p, a in wins:
        key = f"{abbr(instance)}-{t.strftime('%m%d-%H%M')}"
        shells.append(builder.Shell(key=key, name=f"{rd.get('name', instance)} {t.strftime('%a %d %b %H:%M')}", instance=instance, size=size, start=t, slot=t.strftime("%a %H:%M")))
    res = builder.build(reg, rs, shells)
    if res.status in ("no shells", "INFEASIBLE", "UNKNOWN") and not any(res.rosters.values()):
        return None
    viable_shells = [sh for sh in shells if len(res.rosters.get(sh.key, [])) >= size * MIN_FILL and not res.shortfalls.get(sh.key)]
    if viable_shells and len(viable_shells) < len(shells):
        shells = viable_shells
        res = builder.build(reg, rs, shells)  # free the seats the dropped runs held
    elif not viable_shells:
        # best effort: keep the fullest run(s) only, so the draft shows what the bank can actually field
        shells = sorted(shells, key=lambda sh: -len(res.rosters.get(sh.key, [])))[:1]
        res = builder.build(reg, rs, shells)
    runs = []
    for sh in shells:
        seats = res.rosters.get(sh.key, [])
        short = res.shortfalls.get(sh.key, {})
        runs.append(Run(key=sh.key, name=sh.name, instance=instance, size=size, starts_at=sh.start.isoformat(), slot=sh.slot, seats=[s.__dict__ for s in seats], shortfalls=short))
        if len(seats) < size * MIN_FILL:
            problems.append(f"{sh.name}: {len(seats)}/{size} seated")
        for role, n in short.items():
            problems.append(f"{sh.name}: short {n} {role}")
    viable = not problems or all(("no availability grid" in x or "assumed free" in x) for x in problems)
    pid = f"{abbr(instance)}-{window_start.strftime('%Y%m%d')}-{datetime.now(z).strftime('%H%M%S')}"
    return Proposal(id=pid, instance=instance, window_start=window_start.isoformat(), window_end=window_end.isoformat(), runs=runs, unplaced=res.unplaced, notes=res.notes,
                    state="proposed" if viable else "draft", viable=viable, problems=problems)
