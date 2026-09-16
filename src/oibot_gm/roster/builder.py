"""Build every roster for the coming lockout window from the pool in one solve.

Shells (from the guild's rosters: instance, size, schedule) get (member, character) seats under the guild's
rules — one character per member per roster, one raid per member per time slot, one run per character per
instance per lockout, no seat on a slot the member marked "no", none on an absent day or with availability
"out" — while maximising: seats filled, slot preference (yes > maybe), rank, main over alt, rotation fairness
(sat out last window), a `need` hook (wishlist/ledger later; everyone needs by default), and buff-provider
coverage per roster (one of each class buff). Tank/healer minimums are soft with a heavy penalty so a thin
pool still gets a proposal. Groups inside each roster come from the existing group solver afterwards.
Deterministic; the model only explains the result."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ortools.sat.python import cp_model

from ..profiles import GameProfile
from ..registry import Member, Registry, RegisteredCharacter
from . import solver

RANK_VALUE = {"core": 3, "raider": 2, "trial": 1, "social": 0, "alt": 0}
SLOT_VALUE = {"yes": 4, "maybe": 1}
SEAT_VALUE, MAIN_BONUS, SAT_OUT_BONUS, NEED_WEIGHT, BUFF_VALUE, ROLE_SHORT_PENALTY, KEEP_BONUS = 10, 2, 2, 3, 3, 25, 3
OVERLAP_HOURS = 4.0


@dataclass
class Shell:
    key: str
    name: str
    instance: str | None
    size: int
    start: datetime
    slot: str  # schedule text, matches config.slots entries when rated

    @property
    def lockout_days(self) -> int:
        return 7


@dataclass
class Seat:
    discord_id: int
    display_name: str
    character: str
    cls: str
    spec: str
    role: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    rosters: dict[str, list[Seat]]
    unplaced: list[tuple[str, str]]  # (member, why)
    shortfalls: dict[str, dict[str, int]]  # roster -> role -> missing
    objective: int
    status: str
    notes: list[str] = field(default_factory=list)

    def placements(self) -> set[tuple[int, str, str]]:
        return {(s.discord_id, s.character, key) for key, seats in self.rosters.items() for s in seats}


def shells_from_config(reg: Registry) -> list[Shell]:
    from ..raidcycle import next_raid_time

    out = []
    for t in reg.config.rosters:
        if not t.get("schedule"):
            continue
        try:
            start = next_raid_time(t["schedule"], reg.config.timezone)
        except ValueError:
            continue
        sh = Shell(key=t["key"], name=t.get("name", t["key"]), instance=t.get("instance"), size=int(t.get("size") or reg.raid_def(t.get("instance")).get("size") or 20), start=start, slot=t["schedule"])
        out.append(sh)
    return out


def _lockout_days(reg: Registry, instance: str | None) -> int:
    return int(reg.raid_def(instance).get("lockout_days", 7))


def recent_runs(reg: Registry, rs, shells: list[Shell]) -> dict[tuple[str, str], set[str]]:
    """(character label, instance) -> roster keys whose upcoming window the character is already locked for,
    from accepted/done raids of that instance within lockout_days before the shell's start."""
    locked: dict[tuple[str, str], set[str]] = {}
    if rs is None:
        return locked
    for ev in rs.events.values():
        if ev.state not in ("accepted", "done") or not ev.roster or not ev.instance:
            continue
        for sh in shells:
            if sh.instance != ev.instance or ev.team == sh.key and ev.start == sh.start:
                continue
            ws, we = reg.lockout_window(ev.instance, sh.start)
            if ws <= ev.start < we and ev.start <= sh.start:  # same lockout window as this shell
                for p in ev.roster.selected:
                    locked.setdefault((p.character or p.signup_name, ev.instance), set()).add(sh.key)
    return locked


def sat_out_last_window(reg: Registry, rs, shells: list[Shell]) -> set[int]:
    """Members who signed In for a raid in the previous window but were benched — they get priority now."""
    out: set[int] = set()
    if rs is None:
        return out
    earliest = min((sh.start for sh in shells), default=None)
    if earliest is None:
        return out
    for ev in rs.events.values():
        if ev.state not in ("accepted", "done") or not ev.roster:
            continue
        if not (earliest - timedelta(days=14) <= ev.start < earliest):
            continue
        selected = {p.signup_name for p in ev.roster.selected}
        for s in ev.signups.values():
            if s.status == "in" and s.display_name not in selected:
                out.add(s.discord_id)
    return out


def build(reg: Registry, rs, shells: list[Shell] | None = None, need_fn=None, time_limit_s: float = 10.0) -> BuildResult:
    profile = reg.profile
    shells = shells if shells is not None else shells_from_config(reg)
    if not shells:
        return BuildResult({}, [], {}, 0, "no shells", ["no roster has a schedule"])
    need_fn = need_fn or (lambda m, c, instance: 1.0)
    locked = recent_runs(reg, rs, shells)
    priority = sat_out_last_window(reg, rs, shells)
    # slot groups: shells that overlap in time
    groups: list[list[Shell]] = []
    for sh in sorted(shells, key=lambda s: s.start):
        for g in groups:
            if abs((g[0].start - sh.start).total_seconds()) <= OVERLAP_HOURS * 3600:
                g.append(sh)
                break
        else:
            groups.append([sh])

    m = cp_model.CpModel()
    x: dict[tuple[int, str, str], cp_model.IntVar] = {}
    meta: dict[tuple[int, str, str], tuple[Member, RegisteredCharacter, str, list[str], int]] = {}
    unplaced: dict[int, str] = {}
    members = [mm for mm in reg.members.values() if mm.active()]
    for mm in members:
        possible = 0
        why = []
        for sh in shells:
            day = sh.start.date().isoformat()
            hours = float(reg.raid_def(sh.instance).get("duration_hours", 3))
            pref = reg.slot_pref(mm, sh.start, hours, sh.slot)
            if pref == "no":
                why.append(f"{sh.key}: not available then")
                continue
            if mm.absent_on(day):
                why.append(f"{sh.key}: absent")
                continue
            if mm.availability.get(sh.key) == "out":
                why.append(f"{sh.key}: availability out")
                continue
            for c in mm.active():
                if sh.key in locked.get((c.label, sh.instance or ""), set()):
                    why.append(f"{sh.key}: {c.label} locked")
                    continue
                role = reg.roles_of(mm)[0] if c.is_main else profile.spec(c.cls, c.spec).role
                role = role or profile.spec(c.cls, c.spec).role
                reasons = []
                val = SEAT_VALUE
                if pref in SLOT_VALUE:
                    val += SLOT_VALUE[pref]
                    reasons.append(f"slot {pref}")
                val += RANK_VALUE.get(c.rank, 0)
                if c.is_main:
                    val += MAIN_BONUS
                    reasons.append("main")
                else:
                    reasons.append("alt")
                if mm.discord_id in priority:
                    val += SAT_OUT_BONUS
                    reasons.append("sat out last window")
                if sh.key in c.rosters:
                    val += KEEP_BONUS  # stability: a current seat is only given up for a clearly better arrangement
                    reasons.append("already placed")
                need = float(need_fn(mm, c, sh.instance))
                val += int(round(NEED_WEIGHT * need))
                if need > 1:
                    reasons.append("needs loot here")
                key = (mm.discord_id, c.label, sh.key)
                x[key] = m.NewBoolVar(f"x_{mm.discord_id}_{c.label}_{sh.key}".replace(" ", "_"))
                meta[key] = (mm, c, role, reasons, val)
                possible += 1
        if not possible:
            unplaced[mm.discord_id] = "; ".join(why[:3]) or "no eligible character"

    # one character per member per roster; one raid per member per slot group; one run per character per instance
    for mm in members:
        for sh in shells:
            vs = [x[k] for k in x if k[0] == mm.discord_id and k[2] == sh.key]
            if len(vs) > 1:
                m.Add(sum(vs) <= 1)
        for g in groups:
            vs = [x[k] for k in x if k[0] == mm.discord_id and k[2] in {sh.key for sh in g}]
            if len(vs) > 1:
                m.Add(sum(vs) <= 1)
        for c in mm.active():
            for inst in {sh.instance for sh in shells}:
                vs = [x[k] for k in x if k[0] == mm.discord_id and k[1] == c.label and next(s for s in shells if s.key == k[2]).instance == inst]
                if len(vs) > 1:
                    m.Add(sum(vs) <= 1)
    # sizes, role minimums (soft), buff coverage
    terms = [meta[k][4] * v for k, v in x.items()]
    short_vars: dict[tuple[str, str], cp_model.IntVar] = {}
    for sh in shells:
        seats = [x[k] for k in x if k[2] == sh.key]
        m.Add(sum(seats) <= sh.size)
        bounds = reg.role_bounds(sh.instance, sh.size)
        for role in ("tank", "healer"):
            have = [x[k] for k in x if k[2] == sh.key and meta[k][2] == role]
            short = m.NewIntVar(0, sh.size, f"short_{sh.key}_{role}")
            m.Add(sum(have) + short >= bounds[role]["min"])
            short_vars[sh.key, role] = short
            terms.append(-ROLE_SHORT_PENALTY * short)
            if bounds[role]["max"] and have:
                m.Add(sum(have) <= bounds[role]["max"])
        seen_slots: set[str] = set()
        for b in profile.party_buffs() + profile.raid_buffs():
            if b.max_benefit < 3:
                continue
            if b.slot:
                if b.slot in seen_slots:
                    continue
                seen_slots.add(b.slot)
            provs = [x[k] for k in x if k[2] == sh.key and b.provided_by(profile.spec(meta[k][1].cls, meta[k][1].spec))]
            if not provs:
                continue
            y = m.NewBoolVar(f"buff_{sh.key}_{b.id}")
            m.Add(y <= sum(provs))
            terms.append(BUFF_VALUE * y)
    m.Maximize(sum(terms))
    cps = cp_model.CpSolver()
    cps.parameters.max_time_in_seconds = time_limit_s
    cps.parameters.num_workers = 4
    status = cps.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return BuildResult({}, [(mm.display_name, w) for mm, w in ((reg.members[i], w) for i, w in unplaced.items())], {}, 0, cps.StatusName(status), ["no assignment found"])
    rosters: dict[str, list[Seat]] = {sh.key: [] for sh in shells}
    placed: set[int] = set()
    for k, v in x.items():
        if cps.Value(v):
            mm, c, role, reasons, _ = meta[k]
            rosters[k[2]].append(Seat(mm.discord_id, mm.display_name, c.label, c.cls, c.spec, role, list(reasons)))
            placed.add(mm.discord_id)
    for key in rosters:
        rosters[key].sort(key=lambda s: ({"tank": 0, "healer": 1, "melee": 2, "ranged": 3}.get(s.role, 9), s.cls, s.display_name.lower()))
    for mm in members:
        if mm.discord_id not in placed and mm.discord_id not in unplaced:
            unplaced[mm.discord_id] = "no seat left (rosters full or slot/lockout clash)"
    shortfalls = {sh.key: {role: cps.Value(short_vars[sh.key, role]) for role in ("tank", "healer") if cps.Value(short_vars[sh.key, role])} for sh in shells}
    notes = [f"{len(placed)} of {len(members)} members seated across {len(shells)} roster(s)"]
    for sh in shells:
        n = len(rosters[sh.key])
        notes.append(f"{sh.key}: {n}/{sh.size}" + ("".join(f", short {v} {r}" for r, v in shortfalls[sh.key].items())))
    return BuildResult(rosters, [(reg.members[i].display_name, w) for i, w in unplaced.items()], {k: v for k, v in shortfalls.items() if v}, int(cps.ObjectiveValue()), cps.StatusName(status), notes)


def diff_placements(reg: Registry, result: BuildResult) -> tuple[list[tuple[int, str, str]], list[tuple[int, str]]]:
    """(adds: (discord_id, character, roster), removes: (discord_id, roster)) to turn current placements into the proposal."""
    current = {(mm.discord_id, c.label, key) for mm in reg.members.values() for c in mm.active() for key in c.rosters if key in result.rosters}
    proposed = result.placements()
    adds = sorted(proposed - current)
    removes = sorted({(d, key) for d, _c, key in current - proposed if not any(p[0] == d and p[2] == key for p in proposed)} | {(d, key) for d, _c, key in current - proposed if any(p[0] == d and p[2] == key for p in proposed)})
    return adds, removes


def apply(reg: Registry, result: BuildResult, by: str) -> list[str]:
    """Commit the proposal through the registry (one commit per change, like /roster add|remove)."""
    adds, removes = diff_placements(reg, result)
    done = []
    for d, key in removes:
        mm = reg.roster_remove(d, key, by)
        done.append(f"− {mm.display_name} from {key}")
    for d, c, key in adds:
        mm, ch = reg.roster_add(d, key, by, c)
        done.append(f"+ {mm.display_name} ({ch.label}) → {key}")
    return done
