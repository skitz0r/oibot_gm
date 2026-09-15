"""Roster selection + party layout as one CP-SAT model.

Selection: which N of the signups raid (role bounds, prefer signed over bench,
prefer attendance). Layout: assign the selected to groups of 5 to maximise
party-buff synergy from the GameProfile's buff matrix. Deterministic; the LLM
only explains the result."""
from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

from ..models import GroupReport, Player, RosterResult
from ..profiles import Buff, GameProfile

SCALE = 10  # buff values are floats; CP-SAT wants ints


@dataclass
class SolveOptions:
    force_in: tuple[str, ...] = ()
    force_out: tuple[str, ...] = ()
    pins: dict[str, int] | None = None  # signup_name -> group index (0-based)
    keep_together: tuple[tuple[str, str], ...] = ()
    keep_apart: tuple[tuple[str, str], ...] = ()
    raid_size: int | None = None  # override the profile's raid_size (e.g. a 10-man team); role bounds scale with it
    prefer_group: dict[str, int] | None = None  # signup_name -> 1-based group, soft (bonus if honoured)
    prefer_weight: int = 15
    role_min: dict[str, int] | None = None  # role -> minimum, overrides the profile/scaled bounds
    time_limit_s: float = 20.0
    workers: int = 8


def scaled_role_bounds(rules: dict, raid_size: int) -> dict[str, dict[str, int]]:
    """Role min/max from comp_rules, scaled to a different raid size (ceil for mins)."""
    import math

    f = raid_size / rules["raid_size"]
    return {role: {"min": math.ceil(b["min"] * f) if b["min"] else 0, "max": max(1, round(b["max"] * f))} for role, b in rules["roles"].items()}


def solve(profile: GameProfile, players: list[Player], raid_id: str, opts: SolveOptions = SolveOptions()) -> RosterResult:
    rules = profile.comp_rules
    raid = profile.raids[raid_id]
    gsize = rules["group_size"]
    target = opts.raid_size or rules["raid_size"]
    n_groups = max(1, -(-target // gsize))  # ceil
    raid_size = min(target, len(players))
    role_bounds = {k: dict(v) for k, v in (scaled_role_bounds(rules, target) if opts.raid_size else rules["roles"]).items()}
    for role, n in (opts.role_min or {}).items():
        role_bounds.setdefault(role, {"min": 0, "max": target})["min"] = n
        role_bounds[role]["max"] = max(role_bounds[role]["max"], n)
    specs = {p.signup_name: profile.spec(p.cls, p.spec) for p in players}
    sel = rules["selection"]

    m = cp_model.CpModel()
    x = {p.signup_name: m.NewBoolVar(f"x_{p.signup_name}") for p in players}
    y = {(p.signup_name, g): m.NewBoolVar(f"y_{p.signup_name}_{g}") for p in players for g in range(n_groups)}

    m.Add(sum(x.values()) == raid_size)
    for p in players:
        m.Add(sum(y[p.signup_name, g] for g in range(n_groups)) == x[p.signup_name])
    for g in range(n_groups):
        m.Add(sum(y[p.signup_name, g] for p in players) <= gsize)
        m.Add(sum(y[p.signup_name, g] for p in players if p.role == "healer") <= rules["grouping"]["healer_max_per_group"])
        m.Add(sum(y[p.signup_name, g] for p in players if p.role == "tank") <= rules["grouping"]["tank_max_per_group"])
    for role, bounds in role_bounds.items():
        have = [x[p.signup_name] for p in players if p.role == role]
        if have:
            m.Add(sum(have) >= min(bounds["min"], len(have)))
            m.Add(sum(have) <= bounds["max"])
    for name in opts.force_in:
        m.Add(x[name] == 1)
    for name in opts.force_out:
        m.Add(x[name] == 0)
    for a, b in list(rules["grouping"].get("keep_together", [])) + list(opts.keep_together):
        if a in x and b in x:
            for g in range(n_groups):
                m.Add(y[a, g] == y[b, g]).OnlyEnforceIf([x[a], x[b]])
    for a, b in list(rules["grouping"].get("keep_apart", [])) + list(opts.keep_apart):
        if a in x and b in x:
            for g in range(n_groups):
                m.Add(y[a, g] + y[b, g] <= 1)
    for name, g in (opts.pins or {}).items():
        if name in x:
            m.Add(x[name] == 1)
            m.Add(y[name, g] == 1)

    # --- objective: selection terms ---
    terms = []
    for p in players:
        v = sel["signed_bonus"] if p.status == "signed" else -sel["bench_penalty"]
        v += int(round(sel["attendance_weight"] * p.attendance))
        if p.unmapped:
            v -= sel["unknown_character_penalty"]
        terms.append(v * SCALE * x[p.signup_name])

    # --- objective: soft group preferences from standing instructions ---
    for name, g1 in (opts.prefer_group or {}).items():
        if name in x and 1 <= g1 <= n_groups:
            terms.append(opts.prefer_weight * SCALE * y[name, g1 - 1])

    # --- objective: party buff synergy ---
    synergy_terms = []
    for b in profile.party_buffs():
        providers = [p for p in players if b.provided_by(specs[p.signup_name])]
        if not providers:
            continue
        for g in range(n_groups):
            if b.stacking == "unique":
                prov = m.NewBoolVar(f"prov_{b.id}_{g}")
                m.Add(prov <= sum(y[p.signup_name, g] for p in providers))
                for q in players:
                    val = int(round(b.benefit(specs[q.signup_name]) * SCALE))
                    if val <= 0:
                        continue
                    z = m.NewBoolVar(f"z_{b.id}_{g}_{q.signup_name}")
                    m.Add(z <= prov)
                    m.Add(z <= y[q.signup_name, g])
                    synergy_terms.append(val * z)
            else:  # stack: every provider adds for every other member
                for p in providers:
                    for q in players:
                        if q is p:
                            continue
                        val = int(round(b.benefit(specs[q.signup_name]) * SCALE))
                        if val <= 0:
                            continue
                        w = m.NewBoolVar(f"w_{b.id}_{g}_{p.signup_name}_{q.signup_name}")
                        m.Add(w <= y[p.signup_name, g])
                        m.Add(w <= y[q.signup_name, g])
                        synergy_terms.append(val * w)

    # symmetry breaking: the first signed player anchors group 0 (unless pins fix the numbering)
    if not opts.pins:
        first = next((p for p in players if p.status == "signed" and p.signup_name not in opts.force_out), None)
        if first:
            m.Add(y[first.signup_name, 0] == x[first.signup_name])

    m.Maximize(sum(terms) + sum(synergy_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = opts.time_limit_s
    solver.parameters.num_workers = opts.workers
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"no roster found: {solver.StatusName(status)}")

    selected = [p for p in players if solver.Value(x[p.signup_name])]
    benched = [p for p in players if not solver.Value(x[p.signup_name])]
    groups: list[list[str]] = [[] for _ in range(n_groups)]
    for p in selected:
        for g in range(n_groups):
            if solver.Value(y[p.signup_name, g]):
                groups[g].append(p.signup_name)
    reports, syn_total = group_reports(profile, players, groups)
    counts: dict[str, int] = {}
    for p in selected:
        counts[p.role] = counts.get(p.role, 0) + 1
    return RosterResult(
        selected=selected,
        benched=benched,
        groups=groups,
        group_reports=reports,
        objective=int(solver.ObjectiveValue()) // SCALE,
        synergy_value=syn_total,
        role_counts=counts,
        advisories=[],
        solver_status=solver.StatusName(status),
    )


def rebuild(profile: GameProfile, players: list[Player], groups: list[list[str]], base: RosterResult) -> RosterResult:
    """Recompute a RosterResult after groups were edited by hand (swap/move)."""
    by = {p.signup_name: p for p in players}
    selected = [by[n] for g in groups for n in g]
    sel_names = {p.signup_name for p in selected}
    reports, syn = group_reports(profile, players, groups)
    counts: dict[str, int] = {}
    for p in selected:
        counts[p.role] = counts.get(p.role, 0) + 1
    return RosterResult(
        selected=selected,
        benched=[p for p in players if p.signup_name not in sel_names],
        groups=groups,
        group_reports=reports,
        objective=base.objective - base.synergy_value + syn,
        synergy_value=syn,
        role_counts=counts,
        advisories=base.advisories,
        bench_whatif=base.bench_whatif,
        solver_status="edited",
    )


def group_reports(profile: GameProfile, players: list[Player], groups: list[list[str]]) -> tuple[list[GroupReport], int]:
    by_name = {p.signup_name: p for p in players}
    total = 0
    reports = []
    for gi, names in enumerate(groups):
        members = [by_name[n] for n in names]
        lines = []
        gval = 0.0
        for b in profile.party_buffs():
            provs = [p for p in members if b.provided_by(profile.spec(p.cls, p.spec))]
            if not provs:
                continue
            if b.stacking == "unique":
                bens = [(q, b.benefit(profile.spec(q.cls, q.spec))) for q in members]
                v = sum(val for _, val in bens)
                who = ",".join((p.character or p.signup_name) for p in provs[:1])
            else:
                v = 0.0
                for p in provs:
                    v += sum(b.benefit(profile.spec(q.cls, q.spec)) for q in members if q is not p)
                who = "+".join((p.character or p.signup_name) for p in provs)
            if v > 0:
                gval += v
                lines.append(f"{b.name} [{who}] +{v:.0f}")
        total += int(gval)
        reports.append(GroupReport(index=gi + 1, members=[p.label for p in members], buffs=lines, value=int(gval)))
    return reports, total
