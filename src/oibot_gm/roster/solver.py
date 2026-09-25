"""Roster selection + party layout as one CP-SAT model.

Selection: which N of the signups raid (role bounds, prefer signed over bench,
prefer attendance). Layout: assign the selected to groups of 5 to maximise
party-buff synergy from the GameProfile's buff matrix. Deterministic; the LLM
only explains the result.

`solve_rosters` builds the model in named steps that share one `_Model` state:
setup → variables → seat/group constraints → role bounds → forces, pairs, pins →
avoid → objective terms (seat quality, preferences, synergy, symmetry, balance) →
solve → extract. The order of the steps is part of the model (variable and
constraint indices), so keep it when editing."""
from __future__ import annotations

from dataclasses import dataclass, field

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


DEFAULT_OPTIONS = SolveOptions()  # the shared default for solve()/solve_rosters(); never mutate it


def scaled_role_bounds(rules: dict, raid_size: int) -> dict[str, dict[str, int]]:
    """Role min/max from comp_rules, scaled to a different raid size (ceil for mins)."""
    import math

    f = raid_size / rules["raid_size"]
    return {
        role: {"min": math.ceil(b["min"] * f) if b["min"] else 0, "max": max(1, round(b["max"] * f))}
        for role, b in rules["roles"].items()
    }


def solve(profile: GameProfile, players: list[Player], raid_id: str,
          opts: SolveOptions = DEFAULT_OPTIONS) -> RosterResult:
    """One roster (the common case): select who raids and lay them out in groups."""
    return solve_rosters(profile, players, raid_id, opts, rosters=1)[0]


STRATEGIES = ("balanced", "first", "rotation")


def solve_rosters(profile: GameProfile, players: list[Player], raid_id: str, opts: SolveOptions = DEFAULT_OPTIONS,
                  rosters: int = 1, strategy: str = "balanced", roster_bonus: dict[int, dict[str, int]] | None = None,
                  avoid: list[dict[str, int]] | None = None, min_changes: int = 4,
                  avoid_groups: list[dict[str, int]] | None = None) -> list[RosterResult]:
    """`rosters` runs of `raid_size` from one pool of signups, solved jointly. Groups are flat across rosters
    (roster r owns groups r*k … r*k+k-1; pins/prefer_group use that numbering). Strategy shapes the objective:
    balanced — total synergy minus the gap between rosters (and a smaller gap on seat quality: rank/main/etc.);
    first — roster 1's synergy and seat weights count double; rotation — balanced plus `roster_bonus[0]`
    (e.g. sat-out points) for landing in roster 1. `avoid` = earlier roster assignments (name → roster) the answer
    must differ from by at least `min_changes` people; `avoid_groups` the same at group level (name → group index) for
    a single-roster "another layout". Pinned players can't move, so they never count towards the difference."""
    s = _setup(profile, players, raid_id, opts, rosters, strategy)
    _build_vars(s)
    _add_seat_and_group_constraints(s)
    _add_role_bounds(s)
    _add_forces_pairs_and_pins(s)
    _add_avoid(s, avoid, avoid_groups, min_changes)
    _selection_terms(s, roster_bonus)
    _preference_terms(s)
    _synergy_terms(s)
    _raid_cover_terms(s)
    _add_symmetry_break(s)
    _balance_terms(s)
    solver, status = _run(s)
    return _extract(s, solver, status)


# ---------------------------------------------------------------- model state


@dataclass
class _Model:
    """What the build steps of one `solve_rosters` call share: the sizes, the CP-SAT model, its variables and
    the objective term lists. `_setup` fills the sizes, `_build_vars` the variables, the other steps add
    constraints and append to the term lists."""

    profile: GameProfile
    players: list[Player]
    opts: SolveOptions
    rosters: int
    strategy: str
    rules: dict  # profile.comp_rules
    gsize: int  # players per group
    target: int  # seats per roster
    k: int  # groups per roster
    n_groups: int  # groups over all rosters
    seats_total: int  # seats the pool can fill over all rosters
    role_bounds: dict[str, dict[str, int]]  # role -> {min, max}, per roster
    specs: dict[str, SpecInfo]  # signup_name -> signed spec (offspec switches are applied after the solve)
    m: cp_model.CpModel = field(default_factory=cp_model.CpModel)
    x: dict[str, cp_model.IntVar] = field(default_factory=dict)  # name -> seated at all
    y: dict[tuple[str, int], cp_model.IntVar] = field(default_factory=dict)  # (name, group) -> seated there
    xr: dict[tuple[str, int], cp_model.LinearExpr] = field(default_factory=dict)  # (name, roster) -> seated
    o: dict[str, cp_model.IntVar] = field(default_factory=dict)  # name -> raids as their offspec
    off_specs: dict[str, SpecInfo] = field(default_factory=dict)  # name -> the other-role offspec they can switch to
    ym: dict[tuple[str, int], cp_model.IntVar] = field(default_factory=dict)  # (name, group) -> there as main spec
    yo: dict[tuple[str, int], cp_model.IntVar] = field(default_factory=dict)  # (name, group) -> there as offspec
    terms: list = field(default_factory=list)  # objective: seat quality + preferences
    quality: dict[int, list] = field(default_factory=dict)  # roster -> positive seat-quality terms (balance step)
    synergy_terms: list = field(default_factory=list)  # objective: party-buff synergy
    roster_syn: dict[int, list] = field(default_factory=dict)  # roster -> its synergy terms (balance step)
    balance_terms: list = field(default_factory=list)  # objective: minus the gaps between rosters

    def groups_of(self, r: int) -> range:
        """The flat group indices roster r owns."""
        return range(r * self.k, (r + 1) * self.k)


def _setup(profile: GameProfile, players: list[Player], raid_id: str, opts: SolveOptions, rosters: int,
           strategy: str) -> _Model:
    """Sizes and bounds: groups per roster from the comp rules, role min/max scaled to `raid_size` then overridden
    by `role_min`/`role_max` (a max never drops below the min), the signed spec of every player."""
    rules = profile.comp_rules
    profile.raids[raid_id]  # the raid must exist (KeyError otherwise)
    gsize = rules["group_size"]
    target = opts.raid_size or rules["raid_size"]
    k = max(1, -(-target // gsize))  # groups per roster
    bounds_src = scaled_role_bounds(rules, target) if opts.raid_size else rules["roles"]
    role_bounds = {kk: dict(v) for kk, v in bounds_src.items()}
    for role, n in (opts.role_min or {}).items():
        role_bounds.setdefault(role, {"min": 0, "max": target})["min"] = n
        role_bounds[role]["max"] = max(role_bounds[role]["max"], n)
    for role, n in (opts.role_max or {}).items():
        role_bounds.setdefault(role, {"min": 0, "max": target})["max"] = max(n, role_bounds[role]["min"])
    s = _Model(
        profile=profile, players=players, opts=opts, rosters=rosters, strategy=strategy, rules=rules,
        gsize=gsize, target=target, k=k, n_groups=k * rosters, seats_total=min(target * rosters, len(players)),
        role_bounds=role_bounds, specs={p.signup_name: profile.spec(p.cls, p.spec) for p in players},
    )
    s.quality = {r: [] for r in range(rosters)}
    s.roster_syn = {r: [] for r in range(rosters)}
    return s


# ---------------------------------------------------------------- variables


def _build_vars(s: _Model) -> None:
    """x[name] = seated, y[name, g] = seated in group g, xr[name, r] = seated in roster r (sum over its groups).
    Offspec switch: o[name] = 1 means the player raids as their offspec (a different role); only players whose
    offspec is a valid spec of another role get one, and only when `allow_offspec`. A switch implies a seat."""
    m = s.m
    s.x = {p.signup_name: m.NewBoolVar(f"x_{p.signup_name}") for p in s.players}
    s.y = {(p.signup_name, g): m.NewBoolVar(f"y_{p.signup_name}_{g}") for p in s.players for g in range(s.n_groups)}
    s.xr = {(p.signup_name, r): sum(s.y[p.signup_name, g] for g in s.groups_of(r))
            for p in s.players for r in range(s.rosters)}
    if not s.opts.allow_offspec:
        return
    for p in s.players:
        if p.offspec and p.offspec != p.spec:
            try:
                os_ = s.profile.spec(p.cls, p.offspec)
            except KeyError:
                continue
            if os_.role != s.specs[p.signup_name].role:
                s.off_specs[p.signup_name] = os_
                s.o[p.signup_name] = m.NewBoolVar(f"o_{p.signup_name}")
                m.Add(s.o[p.signup_name] <= s.x[p.signup_name])


def _role_expr(s: _Model, role: str, r: int | None = None) -> list:
    """Players raiding as `role` (offspec switches counted), over the whole pool or one roster."""
    terms = []
    for p in s.players:
        n = p.signup_name
        seat = s.x[n] if r is None else s.xr[n, r]
        if n in s.o:
            if p.role == role:
                switched = s.o[n] if r is None else m_and(s.m, s.o[n], s.xr[n, r], f"osw_{n}_{r}_{role}")
                terms.append(seat - switched)
            if s.off_specs[n].role == role:
                terms.append(s.o[n] if r is None else m_and(s.m, s.o[n], s.xr[n, r], f"osw2_{n}_{r}_{role}"))
        elif p.role == role:
            terms.append(seat)
    return terms


def _group_role_expr(s: _Model, role: str, g: int) -> list:
    """Players of `role` in group g, for the healer/tank caps: a switch-capable player counts as their signed role
    (an upper bound: switching away only lowers it) and not as their offspec role, keeping the caps conservative."""
    terms = []
    for p in s.players:
        n = p.signup_name
        if n in s.off_specs:
            if p.role == role:
                terms.append(s.y[n, g])
        elif p.role == role:
            terms.append(s.y[n, g])
    return terms


# ---------------------------------------------------------------- constraints


def _add_seat_and_group_constraints(s: _Model) -> None:
    """Exactly `seats_total` players raid; a seated player sits in exactly one group; a roster holds at most `target`
    (exactly `target` when the pool can fill every roster); a group holds at most `gsize` and respects the per-group
    healer/tank caps from the comp rules."""
    m = s.m
    m.Add(sum(s.x.values()) == s.seats_total)
    for p in s.players:
        m.Add(sum(s.y[p.signup_name, g] for g in range(s.n_groups)) == s.x[p.signup_name])
    for r in range(s.rosters):
        m.Add(sum(s.xr[p.signup_name, r] for p in s.players) <= s.target)
        if len(s.players) >= s.target * s.rosters:
            m.Add(sum(s.xr[p.signup_name, r] for p in s.players) == s.target)
    for g in range(s.n_groups):
        m.Add(sum(s.y[p.signup_name, g] for p in s.players) <= s.gsize)
        m.Add(sum(_group_role_expr(s, "healer", g)) <= s.rules["grouping"]["healer_max_per_group"])
        m.Add(sum(_group_role_expr(s, "tank", g)) <= s.rules["grouping"]["tank_max_per_group"])


def _add_role_bounds(s: _Model) -> None:
    """Each role stays within its min/max per roster (over the whole pool for a single roster). The minimum is
    capped at what the pool can supply per roster (`capable // rosters`) so a thin sheet still solves."""
    for role, bounds in s.role_bounds.items():
        capable = sum(1 for p in s.players
                      if p.role == role or (p.signup_name in s.off_specs and s.off_specs[p.signup_name].role == role))
        for r in range(s.rosters):
            have = _role_expr(s, role, r if s.rosters > 1 else None)
            if have:
                s.m.Add(sum(have) >= min(bounds["min"], capable // s.rosters if s.rosters > 1 else capable))
                s.m.Add(sum(have) <= bounds["max"])


def _add_forces_pairs_and_pins(s: _Model) -> None:
    """force_in / force_out fix a seat either way; keep_together pairs (comp rules + options) share a group whenever
    both are seated and keep_apart pairs never share one; a pin seats a player in one specific group."""
    m, x, y, opts = s.m, s.x, s.y, s.opts
    for name in opts.force_in:
        m.Add(x[name] == 1)
    for name in opts.force_out:
        m.Add(x[name] == 0)
    for a, b in list(s.rules["grouping"].get("keep_together", [])) + list(opts.keep_together):
        if a in x and b in x:
            for g in range(s.n_groups):
                m.Add(y[a, g] == y[b, g]).OnlyEnforceIf([x[a], x[b]])
    for a, b in list(s.rules["grouping"].get("keep_apart", [])) + list(opts.keep_apart):
        if a in x and b in x:
            for g in range(s.n_groups):
                m.Add(y[a, g] + y[b, g] <= 1)
    for name, g in (opts.pins or {}).items():
        if name in x and 0 <= g < s.n_groups:
            m.Add(x[name] == 1)
            m.Add(y[name, g] == 1)


def _add_avoid(s: _Model, avoid: list[dict[str, int]] | None, avoid_groups: list[dict[str, int]] | None,
               min_changes: int) -> None:
    """"Another one": against each earlier answer at least `min_changes` unpinned players must land in a different
    roster (`avoid`: name → roster) or a different group (`avoid_groups`: name → group index)."""
    pinned = set((s.opts.pins or {}).keys())
    for prev in avoid or []:
        same = [s.xr[n, r] for n, r in prev.items() if n in s.x and n not in pinned and 0 <= r < s.rosters]
        if same:
            s.m.Add(sum(same) <= max(0, len(same) - min_changes))
    for prev in avoid_groups or []:
        same = [s.y[n, g] for n, g in prev.items() if n in s.x and n not in pinned and 0 <= g < s.n_groups]
        if same:
            s.m.Add(sum(same) <= max(0, len(same) - min_changes))


# ---------------------------------------------------------------- objective


def _selection_terms(s: _Model, roster_bonus: dict[int, dict[str, int]] | None) -> None:
    """Seat quality: signed beats bench, attendance, an unknown-character penalty, the per-player `bonus`. With
    several rosters the value is earned per roster — doubled for roster 1 under `first`, plus `roster_bonus[r]` —
    and its positive part feeds the per-roster quality totals the balance step compares."""
    sel, opts = s.rules["selection"], s.opts
    for p in s.players:
        v = sel["signed_bonus"] if p.status == "signed" else -sel["bench_penalty"]
        v += int(round(sel["attendance_weight"] * p.attendance))
        if p.unmapped:
            v -= sel["unknown_character_penalty"]
        v += int((opts.bonus or {}).get(p.signup_name, 0))
        if s.rosters == 1:
            s.terms.append(v * SCALE * s.x[p.signup_name])
        else:
            for r in range(s.rosters):
                w = 2 if (s.strategy == "first" and r == 0) else 1
                extra = int((roster_bonus or {}).get(r, {}).get(p.signup_name, 0))
                s.terms.append((v * w + extra) * SCALE * s.xr[p.signup_name, r])
                s.quality[r].append(max(0, v) * s.xr[p.signup_name, r])


def _preference_terms(s: _Model) -> None:
    """Soft group preferences from standing instructions (a bonus when honoured) and the cost of each offspec switch."""
    opts = s.opts
    for name, g1 in (opts.prefer_group or {}).items():
        if name in s.x and 1 <= g1 <= s.n_groups:
            s.terms.append(opts.prefer_weight * SCALE * s.y[name, g1 - 1])
    for n in s.o:
        s.terms.append(-opts.offspec_penalty * SCALE * s.o[n])


def _raid_cover_terms(s: _Model) -> None:
    """Raid-wide cast buffs (Fortitude, Mark, Intellect, Blessings…): a roster earns each one's value for up to `wanted`
    providers it seats. Party synergy alone can't see them — they reach everyone wherever the caster stands — so
    without this a class with strong party buffs could push every priest to the bench and leave the raid without
    Fortitude. Value per provider = the buff's mean benefit over the pool × the roster's seats ÷ providers wanted."""
    for b in s.profile.raid_buffs():
        provs = [p.signup_name for p in s.players if b.provided_by(s.specs[p.signup_name])
                 or (p.signup_name in s.off_specs and b.provided_by(s.off_specs[p.signup_name]))]
        if not provs:
            continue
        wanted = b.wanted or (len(b.choices) if b.choices else 1)
        mean = sum(b.benefit(s.specs[p.signup_name]) for p in s.players) / len(s.players)
        unit = int(round(mean * s.target / wanted * SCALE))
        if unit <= 0:
            continue
        for r in range(s.rosters):
            n = s.m.NewIntVar(0, wanted, f"cover_{b.id}_{r}")
            s.m.Add(n <= sum(s.xr[name, r] for name in provs))
            s.synergy_terms.append(unit * n)
            s.roster_syn[r].append(unit * n)


def _synergy_terms(s: _Model) -> None:
    """Party-buff synergy per group. Presence splits into ym (as main spec) / yo (as offspec) so a switched player
    provides and benefits as the spec they raid as. A `unique` buff has one `prov` var per group and a `z` var per
    beneficiary that pays only when provided and present; a `stack` buff pays per (provider, beneficiary) pair.
    Raid-wide buffs anyone signed can cast are taken as present, so a party buff of the same family only adds what
    it beats. Then: one buff per stacking family counts per player, and a group with k shamans gets at most k
    totems of the same element (slot exclusivity)."""
    m, players, specs, off_specs, o = s.m, s.players, s.specs, s.off_specs, s.o
    for p in players:
        n = p.signup_name
        for g in range(s.n_groups):
            if n in o:
                a = m.NewBoolVar(f"ym_{n}_{g}")
                bvar = m.NewBoolVar(f"yo_{n}_{g}")
                m.Add(a + bvar == s.y[n, g])
                m.Add(bvar <= o[n])
                m.Add(a <= 1 - o[n])
                s.ym[n, g], s.yo[n, g] = a, bvar
            else:
                s.ym[n, g] = s.y[n, g]

    def presence_terms(p: Player, g: int) -> list[tuple[cp_model.IntVar, SpecInfo]]:
        """[(var, spec)] ways player p can be in group g, with the spec that applies."""
        out = [(s.ym[p.signup_name, g], specs[p.signup_name])]
        if (p.signup_name, g) in s.yo:
            out.append((s.yo[p.signup_name, g], off_specs[p.signup_name]))
        return out

    def anyone_provides(b: Buff) -> bool:
        return any(
            b.provided_by(specs[p.signup_name])
            or (p.signup_name in off_specs and b.provided_by(off_specs[p.signup_name]))
            for p in players
        )

    raid_cover = [rb for rb in s.profile.raid_buffs() if anyone_provides(rb)]

    def net_benefit(b: Buff, sp: SpecInfo) -> float:
        base = b.benefit(sp)
        return max(0.0, base - max([rb.benefit(sp) for rb in raid_cover if rb.family_id == b.family_id] or [0.0]))

    def add_syn(g: int, val: int, var) -> None:
        r = g // s.k
        w = 2 if (s.rosters > 1 and s.strategy == "first" and r == 0) else 1
        s.synergy_terms.append(val * w * var)
        s.roster_syn[r].append(val * var)

    slot_provs: dict[tuple[str, int], list] = {}  # (slot, g) -> prov vars of the buffs sharing that slot
    fam_terms: dict[tuple[int, str, str, str], list] = {}  # (g, player, spec, family) -> z vars
    for b in s.profile.party_buffs():
        if not anyone_provides(b):
            continue
        for g in range(s.n_groups):
            prov_vars = [v for p in players for v, sp in presence_terms(p, g) if b.provided_by(sp)]
            if b.stacking == "unique":
                prov = m.NewBoolVar(f"prov_{b.id}_{g}")
                m.Add(prov <= sum(prov_vars))
                if b.slot:
                    slot_provs.setdefault((b.slot, g), []).append((prov, prov_vars))
                for q in players:
                    for v, sp in presence_terms(q, g):
                        val = int(round(net_benefit(b, sp) * SCALE))
                        if val <= 0:
                            continue
                        z = m.NewBoolVar(f"z_{b.id}_{g}_{q.signup_name}_{sp.spec}")
                        m.Add(z <= prov)
                        m.Add(z <= v)
                        add_syn(g, val, z)
                        fam_terms.setdefault((g, q.signup_name, sp.spec, b.family_id), []).append(z)
            else:  # stack: every provider adds for every other member
                for p in players:
                    for pv, ps in presence_terms(p, g):
                        if not b.provided_by(ps):
                            continue
                        for q in players:
                            if q is p:
                                continue
                            for qv, qs in presence_terms(q, g):
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
    for entries in slot_provs.values():
        provider_vars = {id(v): v for _, pv in entries for v in pv}
        m.Add(sum(prov for prov, _ in entries) <= sum(provider_vars.values()))


def _add_symmetry_break(s: _Model) -> None:
    """Group numbering is arbitrary for a single roster: the first signed player anchors group 0 (unless pins or
    seeded preferences already fix the numbering)."""
    opts = s.opts
    if not opts.pins and not opts.prefer_group and s.rosters == 1:
        first = next((p for p in s.players if p.status == "signed" and p.signup_name not in opts.force_out), None)
        if first:
            s.m.Add(s.y[first.signup_name, 0] == s.x[first.signup_name])


def _balance_terms(s: _Model) -> None:
    """balanced / rotation with several rosters: pay for the largest synergy gap between any two rosters and,
    at half weight, for the largest seat-quality gap."""
    if not (s.rosters > 1 and s.strategy in ("balanced", "rotation")):
        return
    m = s.m
    big = SCALE * 100 * len(s.players) * 20
    syn_r = [m.NewIntVar(0, big, f"syn_r{r}") for r in range(s.rosters)]
    qual_r = [m.NewIntVar(0, big, f"qual_r{r}") for r in range(s.rosters)]
    for r in range(s.rosters):
        m.Add(syn_r[r] == sum(s.roster_syn[r]))
        m.Add(qual_r[r] == sum(s.quality[r]))
    gap, qgap = m.NewIntVar(0, big, "syn_gap"), m.NewIntVar(0, big, "qual_gap")
    for a in range(s.rosters):
        for b in range(s.rosters):
            if a != b:
                m.Add(gap >= syn_r[a] - syn_r[b])
                m.Add(qgap >= qual_r[a] - qual_r[b])
    s.balance_terms = [-gap, -(SCALE // 2) * qgap]


# ---------------------------------------------------------------- solve + read back


def _run(s: _Model) -> tuple[cp_model.CpSolver, int]:
    """Maximise seat quality + synergy + balance within the time limit; raise when nothing feasible was found."""
    s.m.Maximize(sum(s.terms) + sum(s.synergy_terms) + sum(s.balance_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = s.opts.time_limit_s
    solver.parameters.num_workers = s.opts.workers
    status = solver.Solve(s.m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"no roster found: {solver.StatusName(status)}")
    return solver, status


def _extract(s: _Model, solver: cp_model.CpSolver, status: int) -> list[RosterResult]:
    """Read the solution back: apply offspec switches to the Player objects (reports/coverage use the chosen spec),
    then one RosterResult per roster; everyone unseated is the bench of roster 0."""
    players, n_groups = s.players, s.n_groups
    switches: dict[str, str] = {}
    for n, var in s.o.items():
        if solver.Value(var):
            p = next(pp for pp in players if pp.signup_name == n)
            switches[n] = s.off_specs[n].spec
            p.offspec, p.spec, p.role = p.spec, s.off_specs[n].spec, s.off_specs[n].role
            s.specs[n] = s.off_specs[n]
    all_groups: list[list[str]] = [[] for _ in range(n_groups)]
    for p in players:
        if solver.Value(s.x[p.signup_name]):
            for g in range(n_groups):
                if solver.Value(s.y[p.signup_name, g]):
                    all_groups[g].append(p.signup_name)
    seated_all = {n for g in all_groups for n in g}
    results = []
    for r in range(s.rosters):
        groups = all_groups[r * s.k:(r + 1) * s.k]
        names = {n for g in groups for n in g}
        selected = [p for p in players if p.signup_name in names]
        reports, syn_total = group_reports(s.profile, players, groups)
        counts: dict[str, int] = {}
        for p in selected:
            counts[p.role] = counts.get(p.role, 0) + 1
        results.append(RosterResult(
            selected=selected,
            benched=[p for p in players if p.signup_name not in seated_all] if r == 0 else [],
            groups=groups, group_reports=reports,
            objective=int(solver.ObjectiveValue()) // SCALE, synergy_value=syn_total, role_counts=counts,
            advisories=[], spec_switches=switches, solver_status=solver.StatusName(status),
        ))
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


def group_reports(profile: GameProfile, players: list[Player],
                  groups: list[list[str]]) -> tuple[list[GroupReport], int]:
    by_name = {p.signup_name: p for p in players}
    total = 0
    reports = []
    for gi, names in enumerate(groups):
        members = [by_name[n] for n in names]
        lines = []
        gval = 0.0
        from . import coverage as _cov

        present, _picks, _wanted = _cov.group_buffs(
            profile, [(p.signup_name, profile.spec(p.cls, p.spec)) for p in members])
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
