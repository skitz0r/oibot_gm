"""Roster selection + party layout as one CP-SAT model.

Selection: which N of the signups raid (role bounds, prefer signed over bench,
prefer attendance). Layout: assign the selected to groups of 5 to maximise
party-buff synergy from the GameProfile's buff matrix. Deterministic; the LLM
only explains the result."""
from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

from ..models import GroupReport, Player, RosterResult, SpecInfo
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
    role_max: dict[str, int] | None = None  # role -> maximum (e.g. the raid's desired comp)
    bonus: dict[str, int] | None = None  # signup_name -> extra selection value (rank, main, sat-out, signup order)
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
    """One roster (the common case): select who raids and lay them out in groups."""
    return solve_rosters(profile, players, raid_id, opts, rosters=1)[0]


STRATEGIES = ("balanced", "first", "rotation")


def solve_rosters(profile: GameProfile, players: list[Player], raid_id: str, opts: SolveOptions = SolveOptions(), rosters: int = 1,
                  strategy: str = "balanced", roster_bonus: dict[int, dict[str, int]] | None = None, avoid: list[dict[str, int]] | None = None, min_changes: int = 4, avoid_groups: list[dict[str, int]] | None = None) -> list[RosterResult]:
    """`rosters` runs of `raid_size` from one pool of signups, solved jointly. Groups are flat across rosters
    (roster r owns groups r*k … r*k+k-1; pins/prefer_group use that numbering). Strategy shapes the objective:
    balanced — total synergy minus the gap between rosters (and a smaller gap on seat quality: rank/main/etc.);
    first — roster 1's synergy and seat weights count double; rotation — balanced plus `roster_bonus[0]`
    (e.g. sat-out points) for landing in roster 1. `avoid` = earlier roster assignments (name → roster) the answer
    must differ from by at least `min_changes` people; `avoid_groups` the same at group level (name → group index) for
    a single-roster "another layout". Pinned players can't move, so they never count towards the difference."""
    rules = profile.comp_rules
    raid = profile.raids[raid_id]
    gsize = rules["group_size"]
    target = opts.raid_size or rules["raid_size"]
    k = max(1, -(-target // gsize))  # groups per roster
    n_groups = k * rosters
    role_bounds = {kk: dict(v) for kk, v in (scaled_role_bounds(rules, target) if opts.raid_size else rules["roles"]).items()}
    for role, n in (opts.role_min or {}).items():
        role_bounds.setdefault(role, {"min": 0, "max": target})["min"] = n
        role_bounds[role]["max"] = max(role_bounds[role]["max"], n)
    for role, n in (opts.role_max or {}).items():
        role_bounds.setdefault(role, {"min": 0, "max": target})["max"] = max(n, role_bounds[role]["min"])
    specs = {p.signup_name: profile.spec(p.cls, p.spec) for p in players}
    sel = rules["selection"]
    seats_total = min(target * rosters, len(players))

    m = cp_model.CpModel()
    x = {p.signup_name: m.NewBoolVar(f"x_{p.signup_name}") for p in players}
    y = {(p.signup_name, g): m.NewBoolVar(f"y_{p.signup_name}_{g}") for p in players for g in range(n_groups)}
    groups_of = lambda r: range(r * k, (r + 1) * k)  # noqa: E731
    xr = {(p.signup_name, r): sum(y[p.signup_name, g] for g in groups_of(r)) for p in players for r in range(rosters)}

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

    def role_expr(role: str, r: int | None = None):
        """Players raiding as `role` (offspec switches counted), over the whole pool or one roster."""
        terms = []
        for p in players:
            n = p.signup_name
            seat = x[n] if r is None else xr[n, r]
            if n in o:
                if p.role == role:
                    terms.append(seat - (o[n] if r is None else m_and(m, o[n], xr[n, r], f"osw_{n}_{r}_{role}")))
                if off_specs[n].role == role:
                    terms.append(o[n] if r is None else m_and(m, o[n], xr[n, r], f"osw2_{n}_{r}_{role}"))
            elif p.role == role:
                terms.append(seat)
        return terms

    # per-group role expressions (for the healer/tank caps)
    def group_role_expr(role: str, g: int):
        terms = []
        for p in players:
            n = p.signup_name
            if n in off_specs:
                if p.role == role:
                    terms.append(y[n, g])  # upper bound; switching away only lowers it
                elif off_specs[n].role == role:
                    pass  # counted via o below; keep caps conservative
            elif p.role == role:
                terms.append(y[n, g])
        return terms

    m.Add(sum(x.values()) == seats_total)
    for p in players:
        m.Add(sum(y[p.signup_name, g] for g in range(n_groups)) == x[p.signup_name])
    for r in range(rosters):
        m.Add(sum(xr[p.signup_name, r] for p in players) <= target)
        if len(players) >= target * rosters:
            m.Add(sum(xr[p.signup_name, r] for p in players) == target)
    for g in range(n_groups):
        m.Add(sum(y[p.signup_name, g] for p in players) <= gsize)
        m.Add(sum(group_role_expr("healer", g)) <= rules["grouping"]["healer_max_per_group"])
        m.Add(sum(group_role_expr("tank", g)) <= rules["grouping"]["tank_max_per_group"])
    for role, bounds in role_bounds.items():
        capable = sum(1 for p in players if p.role == role or (p.signup_name in off_specs and off_specs[p.signup_name].role == role))
        for r in range(rosters):
            have = role_expr(role, r if rosters > 1 else None)
            if have:
                m.Add(sum(have) >= min(bounds["min"], capable // rosters if rosters > 1 else capable))
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
        if name in x and 0 <= g < n_groups:
            m.Add(x[name] == 1)
            m.Add(y[name, g] == 1)
    pinned = set((opts.pins or {}).keys())
    for prev in avoid or []:
        same = [xr[n, r] for n, r in prev.items() if n in x and n not in pinned and 0 <= r < rosters]
        if same:
            m.Add(sum(same) <= max(0, len(same) - min_changes))
    for prev in avoid_groups or []:
        same = [y[n, g] for n, g in prev.items() if n in x and n not in pinned and 0 <= g < n_groups]
        if same:
            m.Add(sum(same) <= max(0, len(same) - min_changes))

    # --- objective: selection terms (seat quality: signed, attendance, per-player bonus) ---
    terms = []
    quality: dict[int, list] = {r: [] for r in range(rosters)}  # per roster, for the balanced quality gap
    for p in players:
        v = sel["signed_bonus"] if p.status == "signed" else -sel["bench_penalty"]
        v += int(round(sel["attendance_weight"] * p.attendance))
        if p.unmapped:
            v -= sel["unknown_character_penalty"]
        v += int((opts.bonus or {}).get(p.signup_name, 0))
        if rosters == 1:
            terms.append(v * SCALE * x[p.signup_name])
        else:
            for r in range(rosters):
                w = 2 if (strategy == "first" and r == 0) else 1
                extra = int((roster_bonus or {}).get(r, {}).get(p.signup_name, 0))
                terms.append((v * w + extra) * SCALE * xr[p.signup_name, r])
                quality[r].append(max(0, v) * xr[p.signup_name, r])

    # --- objective: soft group preferences from standing instructions ---
    for name, g1 in (opts.prefer_group or {}).items():
        if name in x and 1 <= g1 <= n_groups:
            terms.append(opts.prefer_weight * SCALE * y[name, g1 - 1])
    for n in o:
        terms.append(-opts.offspec_penalty * SCALE * o[n])

    # --- objective: party buff synergy (a switched player provides/benefits as their offspec) ---
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
    roster_syn: dict[int, list] = {r: [] for r in range(rosters)}
    slot_provs: dict[tuple[str, int], list] = {}  # (slot, g) -> prov vars of the buffs sharing that slot
    # raid-wide buffs anyone signed can cast are assumed present: a party buff of the same family only adds what it beats
    raid_cover = [rb for rb in profile.raid_buffs() if any(rb.provided_by(specs[p.signup_name]) or (p.signup_name in off_specs and rb.provided_by(off_specs[p.signup_name])) for p in players)]

    def net_benefit(b, s) -> float:
        base = b.benefit(s)
        return max(0.0, base - max([rb.benefit(s) for rb in raid_cover if rb.family_id == b.family_id] or [0.0]))

    def add_syn(g: int, val: int, var) -> None:
        r = g // k
        w = 2 if (rosters > 1 and strategy == "first" and r == 0) else 1
        synergy_terms.append(val * w * var)
        roster_syn[r].append(val * var)

    fam_terms: dict[tuple[int, str, str, str], list] = {}  # (g, player, spec, family) -> z vars: one buff per family counts
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
                        val = int(round(net_benefit(b, s) * SCALE))
                        if val <= 0:
                            continue
                        z = m.NewBoolVar(f"z_{b.id}_{g}_{q.signup_name}_{s.spec}")
                        m.Add(z <= prov)
                        m.Add(z <= v)
                        add_syn(g, val, z)
                        fam_terms.setdefault((g, q.signup_name, s.spec, b.family_id), []).append(z)
            else:  # stack: every provider adds for every other member
                for p in players:
                    for pv, ps in presence_terms(p, g, b):
                        if not b.provided_by(ps):
                            continue
                        for q in players:
                            if q is p:
                                continue
                            for qv, qs in presence_terms(q, g, b):
                                val = int(round(net_benefit(b, qs) * SCALE))
                                if val <= 0:
                                    continue
                                w = m.NewBoolVar(f"w_{b.id}_{g}_{p.signup_name}_{ps.spec}_{q.signup_name}_{qs.spec}")
                                m.Add(w <= pv)
                                m.Add(w <= qv)
                                add_syn(g, val, w)

    # stacking families: a player counts at most one buff per family (the solver keeps the strongest)
    for zs in fam_terms.values():
        if len(zs) > 1:
            m.Add(sum(zs) <= 1)

    # slot exclusivity: a group with k shamans gets at most k totems of the same element
    for (_slot, g), entries in slot_provs.items():
        provider_vars = {id(v): v for _, pv in entries for v in pv}
        m.Add(sum(prov for prov, _ in entries) <= sum(provider_vars.values()))

    # symmetry breaking: the first signed player anchors group 0 (unless pins or seeds fix the numbering)
    if not opts.pins and not opts.prefer_group and rosters == 1:
        first = next((p for p in players if p.status == "signed" and p.signup_name not in opts.force_out), None)
        if first:
            m.Add(y[first.signup_name, 0] == x[first.signup_name])

    # --- balance between rosters (balanced / rotation): pay for the synergy gap and, less, the seat-quality gap ---
    balance_terms = []
    if rosters > 1 and strategy in ("balanced", "rotation"):
        big = SCALE * 100 * len(players) * 20
        syn_r = [m.NewIntVar(0, big, f"syn_r{r}") for r in range(rosters)]
        qual_r = [m.NewIntVar(0, big, f"qual_r{r}") for r in range(rosters)]
        for r in range(rosters):
            m.Add(syn_r[r] == sum(roster_syn[r]))
            m.Add(qual_r[r] == sum(quality[r]))
        gap, qgap = m.NewIntVar(0, big, "syn_gap"), m.NewIntVar(0, big, "qual_gap")
        for a in range(rosters):
            for b in range(rosters):
                if a != b:
                    m.Add(gap >= syn_r[a] - syn_r[b])
                    m.Add(qgap >= qual_r[a] - qual_r[b])
        balance_terms = [-gap, -(SCALE // 2) * qgap]

    m.Maximize(sum(terms) + sum(synergy_terms) + sum(balance_terms))
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
    all_groups: list[list[str]] = [[] for _ in range(n_groups)]
    for p in players:
        if solver.Value(x[p.signup_name]):
            for g in range(n_groups):
                if solver.Value(y[p.signup_name, g]):
                    all_groups[g].append(p.signup_name)
    seated_all = {n for g in all_groups for n in g}
    results = []
    for r in range(rosters):
        groups = all_groups[r * k:(r + 1) * k]
        names = {n for g in groups for n in g}
        selected = [p for p in players if p.signup_name in names]
        reports, syn_total = group_reports(profile, players, groups)
        counts: dict[str, int] = {}
        for p in selected:
            counts[p.role] = counts.get(p.role, 0) + 1
        results.append(RosterResult(selected=selected, benched=[p for p in players if p.signup_name not in seated_all] if r == 0 else [], groups=groups, group_reports=reports,
                                    objective=int(solver.ObjectiveValue()) // SCALE, synergy_value=syn_total, role_counts=counts, advisories=[], spec_switches=switches, solver_status=solver.StatusName(status)))
    return results


def m_and(m: cp_model.CpModel, a, b, name: str):
    """Bool var = a AND b (b may be a linear sum of bools that is at most 1)."""
    v = m.NewBoolVar(name)
    m.Add(v <= a)
    m.Add(v <= b)
    m.Add(v >= a + b - 1)
    return v


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
