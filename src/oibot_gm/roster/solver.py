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
    allow_offspec: bool = True  # let the solver switch players to their offspec to meet role minimums
    offspec_penalty: int = 8  # objective cost per switch
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

    # offspec switch: o[p] = 1 means p raids as their offspec (different role); only when the roster needs it
    off_specs: dict[str, SpecInfo] = {}
    o: dict[str, cp_model.IntVar] = {}
    if opts.allow_offspec:
        for p in players:
            if p.offspec and p.offspec != p.spec:
                try:
                    os_ = profile.spec(p.cls, p.offspec)
                except KeyError:
                    continue
                if os_.role != specs[p.signup_name].role:
                    off_specs[p.signup_name] = os_
                    o[p.signup_name] = m.NewBoolVar(f"o_{p.signup_name}")
                    m.Add(o[p.signup_name] <= x[p.signup_name])

    def role_expr(role: str):
        terms = []
        for p in players:
            n = p.signup_name
            if n in o:
                if p.role == role:
                    terms.append(x[n] - o[n])
                if off_specs[n].role == role:
                    terms.append(o[n])
            elif p.role == role:
                terms.append(x[n])
        return terms

    # per-group role expressions (for the healer/tank caps)
    def group_role_expr(role: str, g: int):
        terms = []
        for p in players:
            n = p.signup_name
            if n in o:
                if p.role == role:
                    terms.append(y[n, g])  # upper bound; switching away only lowers it
                elif off_specs[n].role == role:
                    pass  # counted via o below; keep caps conservative
            elif p.role == role:
                terms.append(y[n, g])
        return terms

    m.Add(sum(x.values()) == raid_size)
    for p in players:
        m.Add(sum(y[p.signup_name, g] for g in range(n_groups)) == x[p.signup_name])
    for g in range(n_groups):
        m.Add(sum(y[p.signup_name, g] for p in players) <= gsize)
        m.Add(sum(group_role_expr("healer", g)) <= rules["grouping"]["healer_max_per_group"])
        m.Add(sum(group_role_expr("tank", g)) <= rules["grouping"]["tank_max_per_group"])
    for role, bounds in role_bounds.items():
        have = role_expr(role)
        capable = sum(1 for p in players if p.role == role or (p.signup_name in off_specs and off_specs[p.signup_name].role == role))
        if have:
            m.Add(sum(have) >= min(bounds["min"], capable))
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
    for n in o:
        terms.append(-opts.offspec_penalty * SCALE * o[n])

    # --- objective: party buff synergy (a switched player provides/benefits as their offspec) ---
    # presence-in-spec variables: ym[p,g] = in group g as main spec, yo[p,g] = in group g as offspec
    ym: dict[tuple[str, int], cp_model.IntVar] = {}
    yo: dict[tuple[str, int], cp_model.IntVar] = {}
    for p in players:
        n = p.signup_name
        for g in range(n_groups):
            if n in o:
                a = m.NewBoolVar(f"ym_{n}_{g}")
                bvar = m.NewBoolVar(f"yo_{n}_{g}")
                m.Add(a + bvar == y[n, g])
                m.Add(bvar <= o[n])
                m.Add(a <= 1 - o[n])
                ym[n, g], yo[n, g] = a, bvar
            else:
                ym[n, g] = y[n, g]

    def presence_terms(p, g, b):
        """[(var, spec)] ways player p can be in group g, with the spec that applies."""
        out = [(ym[p.signup_name, g], specs[p.signup_name])]
        if (p.signup_name, g) in yo:
            out.append((yo[p.signup_name, g], off_specs[p.signup_name]))
        return out

    synergy_terms = []
    slot_provs: dict[tuple[str, int], list] = {}  # (slot, g) -> prov vars of the buffs sharing that slot
    for b in profile.party_buffs():
        any_provider = any(b.provided_by(specs[p.signup_name]) or (p.signup_name in off_specs and b.provided_by(off_specs[p.signup_name])) for p in players)
        if not any_provider:
            continue
        for g in range(n_groups):
            prov_vars = [v for p in players for v, s in presence_terms(p, g, b) if b.provided_by(s)]
            if b.stacking == "unique":
                prov = m.NewBoolVar(f"prov_{b.id}_{g}")
                m.Add(prov <= sum(prov_vars))
                if b.slot:
                    slot_provs.setdefault((b.slot, g), []).append((prov, prov_vars))
                for q in players:
                    for v, s in presence_terms(q, g, b):
                        val = int(round(b.benefit(s) * SCALE))
                        if val <= 0:
                            continue
                        z = m.NewBoolVar(f"z_{b.id}_{g}_{q.signup_name}_{s.spec}")
                        m.Add(z <= prov)
                        m.Add(z <= v)
                        synergy_terms.append(val * z)
            else:  # stack: every provider adds for every other member
                for p in players:
                    for pv, ps in presence_terms(p, g, b):
                        if not b.provided_by(ps):
                            continue
                        for q in players:
                            if q is p:
                                continue
                            for qv, qs in presence_terms(q, g, b):
                                val = int(round(b.benefit(qs) * SCALE))
                                if val <= 0:
                                    continue
                                w = m.NewBoolVar(f"w_{b.id}_{g}_{p.signup_name}_{ps.spec}_{q.signup_name}_{qs.spec}")
                                m.Add(w <= pv)
                                m.Add(w <= qv)
                                synergy_terms.append(val * w)

    # slot exclusivity: a group with k shamans gets at most k totems of the same element
    for (_slot, g), entries in slot_provs.items():
        provider_vars = {id(v): v for _, pv in entries for v in pv}
        m.Add(sum(prov for prov, _ in entries) <= sum(provider_vars.values()))

    # symmetry breaking: the first signed player anchors group 0 (unless pins or seeds fix the numbering)
    if not opts.pins and not opts.prefer_group:
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

    # apply offspec switches to the Player objects so reports/coverage use the chosen spec
    switches: dict[str, str] = {}
    for n, var in o.items():
        if solver.Value(var):
            p = next(pp for pp in players if pp.signup_name == n)
            switches[n] = off_specs[n].spec
            p.offspec, p.spec, p.role = p.spec, off_specs[n].spec, off_specs[n].role
            specs[n] = off_specs[n]
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
        spec_switches=switches,
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
        from . import coverage as _cov

        present, _picks, _wanted = _cov.group_buffs(profile, [(p.signup_name, profile.spec(p.cls, p.spec)) for p in members])
        for b in profile.party_buffs():
            provs = [p for p in members if p.signup_name in present.get(b.id, [])]
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
