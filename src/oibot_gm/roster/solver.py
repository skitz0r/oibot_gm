"""Roster selection + party layout as one CP-SAT model.

Selection: which N of the signups raid (minimums filled first, prefer signed over
bench, prefer attendance). Layout: assign the selected to groups of 5 to maximise
party-buff synergy from the GameProfile's buff matrix. Deterministic; the LLM
only explains the result.

`solve_rosters` builds the model in named steps that share one `_Model` state:
setup → variables → seat/group constraints → forces, pairs, pins → avoid →
objective terms (seat quality, minimums, extra tanks/healers, the bench last, preferences, synergy, symmetry,
balance) →
solve → extract. The order of the steps is part of the model (variable and
constraint indices), so keep it when editing."""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..constants import ROLES
from ..models import GroupReport, Player, RosterResult, SpecInfo
from ..profiles import Buff, GameProfile

log = logging.getLogger(__name__)

SCALE = 10  # buff values are floats; CP-SAT wants ints


@dataclass
class SolveOptions:
    force_in: tuple[str, ...] = ()
    force_out: tuple[str, ...] = ()
    pins: dict[str, int] | None = None  # signup_name -> group index (0-based)
    keep_together: tuple[tuple[str, str], ...] = ()
    keep_apart: tuple[tuple[str, str], ...] = ()
    raid_size: int | None = None  # override the profile's raid_size (e.g. a 10-man team); the game's minimums scale with it
    prefer_group: dict[str, int] | None = None  # signup_name -> 1-based group, soft (bonus if honoured)
    prefer_weight: int = 15
    # slot -> per-roster minimum, laid over the game's scaled tank/healer minimums; a slot is a role (tank, healer,
    # melee, ranged), a class (Paladin) or Class:Spec. A preference filled first, never a constraint (design D16)
    minimums: dict[str, int] | None = None
    # a follow-on: the players its lead rostered (design §5.62); a tank or healer among them is never charged as extra
    lead_rostered: tuple[str, ...] = ()
    bonus: dict[str, int] | None = None  # signup_name -> extra selection value (rank, main, sat-out, signup order)
    allow_offspec: bool = True  # let the solver switch players to their offspec to meet role minimums
    offspec_penalty: int = 8  # objective cost per switch
    time_limit_s: float = 20.0  # the solve's budget, spent as deterministic time (DTIME_PER_SECOND): the same input, the same roster
    workers: int = 8
    kept_off: tuple[str, ...] = ()  # who officers or the comp policy keep off (inside force_out; never in the model): explain never ranks them
    stop: "Stop | None" = None  # a solve that may be abandoned (the Events board's picture): Stop.stop() ends it, raising Stopped


# Repeatable solves (the final review of §5.67 phase 2): Preview lock and Lock solve the same input and must land on the
# same roster even when the search stops before proving the best one. CP-SAT's default portfolio races its workers, so
# what it holds at a wall-clock limit varies run to run; interleaved search is deterministic whatever the number of
# workers (OR-tools 9.15 sat_parameters: interleave_search), and a deterministic-time limit stops it at the same work
# on any machine under any load. DTIME_PER_SECOND turns a budget in seconds into deterministic time (measured on the
# lock's 20- and 10-player models: about 6 units per wall second here). The wall-clock limit is a HANG GUARD only,
# WALL_CEILING × the budget (120 s at the lock's 12 s): far past any solve that ends on its work budget, even with the
# host busy (a parallel test run; two or three solves at one lock minute reached 19 s and more of the old 24 s), so it
# changes no answer; a solve it does cut is no longer repeatable, is logged, and is never kept (below).
# interleave_search is marked "Experimental" in OR-tools' sat_parameters: re-check its determinism (tests/
# test_solver_repeatable.py) and DTIME_PER_SECOND's wall time on every OR-tools upgrade.
RANDOM_SEED = 0
DTIME_PER_SECOND = 6.0
WALL_CEILING = 10.0

# One answer per model (the final fix wave of §5.67 phase 4, 2026-11-24). The deterministic limit makes a solve
# repeatable only while it finishes inside WALL_CEILING: on a loaded machine (a full test run, another solve, the
# host busy) the wall clock stops it first, at whatever work it had done, and the Events board's picture and the lock
# solved a minute apart came out different — the same model, two rosters (measured: the later raid's two rosters
# numbered the other way round). So the process keeps the answer each model got (keyed by the model itself, every
# variable, constraint and objective term, and the search parameters) and a solve of a model it has answered before
# takes that answer: the picture, Preview lock and the lock give the same roster for the same input whatever the load.
# A model never solved before searches exactly as before; nothing is kept for a stopped or infeasible solve, nor for
# one the wall-clock ceiling cut short of its work budget (a later quiet solve gives the repeatable answer).
ANSWERS_MAX = 64  # models whose answer is kept (least recently used goes first)
_answers: "OrderedDict[str, _Answer]" = OrderedDict()
_answers_lock = threading.Lock()
ceiling_hits = 0  # solves the wall clock stopped before their deterministic budget (a log line each; tests read it)


def forget_answers() -> None:
    """Drop every kept answer (tests that measure the search itself)."""
    with _answers_lock:
        _answers.clear()

DEFAULT_OPTIONS = SolveOptions()  # the shared default for solve()/solve_rosters(); never mutate it


def scaled_role_minimums(rules: dict, raid_size: int) -> dict[str, int]:
    """Role minimums from comp_rules, scaled to a different raid size (ceil)."""
    import math

    f = raid_size / rules["raid_size"]
    return {role: math.ceil(b["min"] * f) if b.get("min") else 0 for role, b in rules["roles"].items()}


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
    s = _build_model(profile, players, raid_id, opts, rosters, strategy, roster_bonus, avoid, min_changes, avoid_groups)
    solver, status = _run(s)
    return _extract(s, solver, status)


def _build_model(profile: GameProfile, players: list[Player], raid_id: str, opts: SolveOptions, rosters: int,
                 strategy: str, roster_bonus: dict[int, dict[str, int]] | None, avoid: list[dict[str, int]] | None,
                 min_changes: int, avoid_groups: list[dict[str, int]] | None) -> "_Model":
    """THE model of one `solve_rosters` call, every step in order, the objective set (the kept-answer key hashes it;
    tests build it here, so a new step can't drift past them)."""
    s = _setup(profile, players, raid_id, opts, rosters, strategy)
    _refuse_conflicts(s)
    _build_vars(s)
    _add_seat_and_group_constraints(s)
    _add_forces_pairs_and_pins(s)
    _add_avoid(s, avoid, avoid_groups, min_changes)
    _selection_terms(s, roster_bonus)
    _minimum_terms(s, roster_bonus)
    _extra_role_terms(s, roster_bonus)
    _bench_terms(s, roster_bonus)
    _preference_terms(s)
    _synergy_terms(s)
    _raid_cover_terms(s)
    _add_symmetry_break(s)
    _balance_terms(s)
    s.m.Maximize(sum(s.terms) + sum(s.synergy_terms) + sum(s.balance_terms))
    return s


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
    seatable: int  # players the solve may seat: the pool minus force_out (a kept-off name never fills a seat)
    minimums: dict[str, int]  # slot (role, class or Class:Spec) -> minimum, per roster
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
    """Sizes and minimums: groups per roster from the comp rules, the game's tank/healer minimums scaled to
    `raid_size` with `opts.minimums` laid over them per slot, the signed spec of every player."""
    rules = profile.comp_rules
    profile.raids[raid_id]  # the raid must exist (KeyError otherwise)
    gsize = rules["group_size"]
    target = opts.raid_size or rules["raid_size"]
    k = max(1, -(-target // gsize))  # groups per roster
    game = scaled_role_minimums(rules, target)
    minimums = {r: game[r] for r in ("tank", "healer") if game.get(r)}
    minimums.update({slot: int(n) for slot, n in (opts.minimums or {}).items()})
    # the seats are filled from the players the solve MAY seat: counting a kept-off name (force_out — an "out" pin, an
    # always_bench line, a drag onto Not placed) asked for one seat more than anyone could fill, so a raid that wasn't
    # over-full was infeasible with one name kept off
    out = set(opts.force_out)
    seatable = sum(1 for p in players if p.signup_name not in out)
    s = _Model(
        profile=profile, players=players, opts=opts, rosters=rosters, strategy=strategy, rules=rules,
        gsize=gsize, target=target, k=k, n_groups=k * rosters, seats_total=min(target * rosters, seatable), seatable=seatable,
        minimums=minimums, specs={p.signup_name: profile.spec(p.cls, p.spec) for p in players},
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
    for p in s.players:
        os_ = switch_spec(s.profile, p, s.opts.allow_offspec)
        if os_ is not None:
            s.off_specs[p.signup_name] = os_
            s.o[p.signup_name] = m.NewBoolVar(f"o_{p.signup_name}")
            m.Add(s.o[p.signup_name] <= s.x[p.signup_name])


def switch_spec(profile: GameProfile, p: Player, allow_offspec: bool = True) -> SpecInfo | None:
    """The offspec player p may be switched to at the solve: a valid spec of ANOTHER role (None without one, or
    without `allow_offspec`). _build_vars and bench_refund_room both read it."""
    if not allow_offspec or not p.offspec or p.offspec == p.spec:
        return None
    try:
        os_ = profile.spec(p.cls, p.offspec)
    except KeyError:
        return None
    return os_ if os_.role != profile.spec(p.cls, p.spec).role else None


def _switched(s: _Model, n: str, r: int | None, tag: str):
    """Player n raids as their offspec (in roster r when given)."""
    return s.o[n] if r is None else m_and(s.m, s.o[n], s.xr[n, r], f"{tag}_{n}_{r}")


def _role_expr(s: _Model, role: str, r: int | None = None, only: set[str] | None = None,
               skip: frozenset[str] = frozenset()) -> list:
    """Players raiding as `role` (offspec switches counted), over the whole pool or one roster; `only` limits it to
    those players, `skip` leaves those out."""
    terms = []
    for p in s.players:
        n = p.signup_name
        if (only is not None and n not in only) or n in skip:
            continue
        seat = s.x[n] if r is None else s.xr[n, r]
        if n in s.o:
            if p.role == role:
                terms.append(seat - _switched(s, n, r, f"osw_{role}"))
            if s.off_specs[n].role == role:
                terms.append(_switched(s, n, r, f"osw2_{role}"))
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


def _slot_expr(s: _Model, slot: str, r: int | None = None, skip: frozenset[str] = frozenset()) -> list:
    """Seats that count toward a minimum's slot, over the whole pool or one roster: a role (offspec switches
    counted, as `_role_expr`), a class (every seat of a player of that class) or `Class:Spec` (the seats of players
    who play that spec: their signed spec unless switched, their offspec when switched — so a hybrid counts toward
    one spec's minimum, never two). `skip` leaves those players out."""
    if slot in ROLES:
        return _role_expr(s, slot, r, skip=skip)
    cls, _, spec = slot.partition(":")
    terms = []
    for p in s.players:
        n = p.signup_name
        if p.cls != cls or n in skip:
            continue
        seat = s.x[n] if r is None else s.xr[n, r]
        if not spec:
            terms.append(seat)
        elif n in s.o:
            if p.spec == spec:
                terms.append(seat - _switched(s, n, r, "ssw"))
            elif p.offspec == spec:
                terms.append(_switched(s, n, r, "ssw2"))
        elif p.spec == spec:
            terms.append(seat)
    return terms


# ---------------------------------------------------------------- constraints


def _add_seat_and_group_constraints(s: _Model) -> None:
    """Exactly `seats_total` players raid (the seats, or every player the solve may seat when they are fewer); a seated
    player sits in exactly one group; a roster holds at most `target` (exactly `target` when the players it may seat
    can fill every roster); a group holds at most `gsize` and respects the per-group healer/tank caps from the comp
    rules."""
    m = s.m
    m.Add(sum(s.x.values()) == s.seats_total)
    for p in s.players:
        m.Add(sum(s.y[p.signup_name, g] for g in range(s.n_groups)) == s.x[p.signup_name])
    for r in range(s.rosters):
        m.Add(sum(s.xr[p.signup_name, r] for p in s.players) <= s.target)
        if s.seatable >= s.target * s.rosters:
            m.Add(sum(s.xr[p.signup_name, r] for p in s.players) == s.target)
    for g in range(s.n_groups):
        m.Add(sum(s.y[p.signup_name, g] for p in s.players) <= s.gsize)
        m.Add(sum(_group_role_expr(s, "healer", g)) <= s.rules["grouping"]["healer_max_per_group"])
        m.Add(sum(_group_role_expr(s, "tank", g)) <= s.rules["grouping"]["tank_max_per_group"])


# what blocks a roster before any search, named so the officers' sentence can say it (raidcycle.solver_error_text)
KEPT_ON, GROUP_FULL, GROUP_ROLES, KEPT_BOTH = "more kept on than seats", "a group over full", "a group over its role cap", "kept on and off"


class Conflict(RuntimeError):
    """A roster refused before searching: `reason` (one of the names above) and the facts its sentence names —
    `names` (the people), `group` (1-based, within its roster), `have` / `seats` (counts). str() keeps the solver's
    "no roster found: <reason>" form."""

    def __init__(self, reason: str, *, names: tuple[str, ...] = (), group: int | None = None, roster: int | None = None,
                 role: str | None = None, have: int = 0, seats: int = 0):
        super().__init__(f"no roster found: {reason}")
        self.reason, self.names, self.group, self.roster, self.role = reason, tuple(names), group, roster, role
        self.have, self.seats = have, seats


def _refuse_conflicts(s: _Model) -> None:
    """RuntimeError("no roster found: <reason>") for the conflicts the board and the pins can make on their own,
    before searching: more players kept on (force_in, board places) than every roster's seats; a name both kept on and
    kept off; a group given more board places than it holds, or more healers / tanks than one group may have."""
    opts, names = s.opts, {p.signup_name: p for p in s.players}
    pins = {n: g for n, g in (opts.pins or {}).items() if n in names and 0 <= g < s.n_groups}
    on = {n for n in opts.force_in if n in names} | set(pins)
    both = sorted(on & set(opts.force_out))
    if both:
        raise Conflict(KEPT_BOTH, names=tuple(both))
    if len(on) > s.target * s.rosters:
        raise Conflict(KEPT_ON, have=len(on), seats=s.target * s.rosters)
    for g in sorted(set(pins.values())):
        held = [names[n] for n, gg in pins.items() if gg == g]
        if len(held) > s.gsize:
            raise Conflict(GROUP_FULL, names=tuple(p.signup_name for p in held), group=g % s.k + 1,
                           roster=g // s.k + 1 if s.rosters > 1 else None, have=len(held), seats=s.gsize)
        for role in ("healer", "tank"):
            cap = s.rules["grouping"][f"{role}_max_per_group"]
            if sum(1 for p in held if p.role == role) > cap:
                raise Conflict(GROUP_ROLES, names=tuple(p.signup_name for p in held if p.role == role), group=g % s.k + 1,
                               roster=g // s.k + 1 if s.rosters > 1 else None, role=role, seats=cap)


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


def _seat_value(s: _Model, p: Player) -> int:
    """One player's seat value before SCALE: signed / bench, attendance, an unknown character, the per-player bonus."""
    sel = s.rules["selection"]
    v = sel["signed_bonus"] if p.status == "signed" else -sel["bench_penalty"]
    v += int(round(sel["attendance_weight"] * (p.attendance if p.attendance_total else 1.0)))  # unknown: neutral, never a penalty
    if p.unmapped:
        v -= sel["unknown_character_penalty"]
    return v + int((s.opts.bonus or {}).get(p.signup_name, 0))


def _selection_terms(s: _Model, roster_bonus: dict[int, dict[str, int]] | None) -> None:
    """Seat quality: signed beats bench, attendance (unknown — no counted run — weighs as full, the loot factor's
    neutral 1.0: never a penalty; design §3.1 decision 10), an unknown-character penalty, the per-player `bonus`. With
    several rosters the value is earned per roster — doubled for roster 1 under `first`, plus `roster_bonus[r]` —
    and its positive part feeds the per-roster quality totals the balance step compares."""
    for p in s.players:
        v = _seat_value(s, p)
        if s.rosters == 1:
            s.terms.append(v * SCALE * s.x[p.signup_name])
        else:
            for r in range(s.rosters):
                w = 2 if (s.strategy == "first" and r == 0) else 1
                extra = int((roster_bonus or {}).get(r, {}).get(p.signup_name, 0))
                s.terms.append((v * w + extra) * SCALE * s.xr[p.signup_name, r])
                s.quality[r].append(max(0, v) * s.xr[p.signup_name, r])


SHORT_ROLE_MARGIN = 10_000  # buff points: more than the synergy, raid cover and group preference one seat can add


def _extra_role_cost(s: _Model, roster_bonus: dict[int, dict[str, int]] | None) -> int:
    """EXTRA_ROLE_COST: more than anything one seat earns otherwise — twice the largest seat value (the `first`
    strategy's double) plus the largest roster bonus, its half-weight balance gap on top, and SHORT_ROLE_MARGIN for
    synergy. MINIMUM_REWARD is twice it, so a seat that fills a minimum is taken even when it is also a tank or healer
    past theirs (a Holy Paladin for a Paladin minimum)."""
    top = 2 * max((abs(_seat_value(s, p)) for p in s.players), default=0)
    top += max((abs(int(v)) for b in (roster_bonus or {}).values() for v in b.values()), default=0)
    return (top + top // 2 + SHORT_ROLE_MARGIN) * SCALE


def _per_roster(s: _Model) -> tuple:
    """The rosters a per-roster term is laid over: each roster, or the whole pool (None) for one roster."""
    return tuple(range(s.rosters)) if s.rosters > 1 else (None,)


def _minimum_terms(s: _Model, roster_bonus: dict[int, dict[str, int]] | None) -> None:
    """Every minimum is filled first (design D16): per slot and per roster, each seat that counts toward it, up to
    the minimum, earns MINIMUM_REWARD — so everyone who joined for a short slot is rostered before one of its seats
    goes to anyone else (the rule set in review, 2026-11-22), the odd player beating even a lead's rostered player
    (LEAD_SEAT_BONUS rides in `bonus`). A preference, never a bound: a hybrid who could fill two minimums fills one,
    a player forced out earns nothing, a minimum the joiners can't meet leaves the rest of the roster as it would be,
    and a board that fills every seat still solves. A seatable Bench answer's seat counts toward a tank or healer
    minimum only — never a class, spec, melee or ranged one —, so no such minimum ever brings a Bench answer in (Bench
    last, 2026-11-25: with no seatable Bench answer every expression is what it was)."""
    reward = 2 * _extra_role_cost(s, roster_bonus)
    out = set(s.opts.force_out)
    bench = frozenset(p.signup_name for p in s.players if p.status == "bench" and p.signup_name not in out)
    for slot, n in s.minimums.items():
        if n <= 0:
            continue
        for r in _per_roster(s):
            have = _slot_expr(s, slot, r, skip=frozenset() if slot in ("tank", "healer") else bench)
            if not have:
                continue
            filled = s.m.NewIntVar(0, n, f"min_{slot}_{r}")
            s.m.Add(filled <= sum(have))
            s.terms.append(reward * filled)


def _extra_role_terms(s: _Model, roster_bonus: dict[int, dict[str, int]] | None) -> None:
    """No role has an upper limit, but the seats past the minimums go to damage dealers: each tank or healer above
    their minimum in a roster costs EXTRA_ROLE_COST, more than any seat earns otherwise, so one sits only when nobody
    else joined (every seat is filled) or officers placed them (forced in, pinned, on the board). On a follow-on a
    player the lead rostered (`opts.lead_rostered`) is never charged: the allowance is the minimum or the lead's own
    players in the role, whichever is more, so the two 10s stay the lead's 20 split in two (design §5.62)."""
    cost = _extra_role_cost(s, roster_bonus)
    lead = set(s.opts.lead_rostered) & set(s.x)
    for role in ("tank", "healer"):
        n = int(s.minimums.get(role, 0))
        for r in _per_roster(s):
            have = _role_expr(s, role, r)
            if not have:
                continue
            allowed: object = n
            on_lead = _role_expr(s, role, r, only=lead) if lead else []
            if on_lead:
                allowed = s.m.NewIntVar(0, len(s.players), f"allow_{role}_{r}")
                lead_n = s.m.NewIntVar(0, len(s.players), f"lead_{role}_{r}")
                s.m.Add(lead_n == sum(on_lead))
                s.m.AddMaxEquality(allowed, [lead_n, n])
            extra = s.m.NewIntVar(0, len(s.players), f"extra_{role}_{r}")
            s.m.Add(extra >= sum(have) - allowed)
            s.terms.append(-cost * extra)


# Bench is strictly last (design §3 of 2026-11-25, the owner's rule): a player who answered Bench takes a seat only
# when no joiner can — a seat left empty once every joiner is placed, or a tank / healer minimum the joiners cannot
# meet. BENCH_SEAT_COST = BENCH_SEAT_FACTOR × EXTRA_ROLE_COST per Bench seat, raised by 2 × EXTRA_ROLE_COST for each
# further minimum one Bench seat can count toward (a role, a class and a Class:Spec slot: at most three), so it is
# sized from the model's own bounds: one seat earns at most MINIMUM_REWARD (2×) per minimum it counts toward, the
# joiner it would displace can cost at most one EXTRA_ROLE_COST (1×), and seat values, synergy, raid cover, group
# preference and the balance gap together stay under one EXTRA_ROLE_COST (1×, by its own sizing) — so a Bench seat
# never out-bids a joiner, whatever their weights.
BENCH_SEAT_FACTOR = 4


def _bench_minimum_slots(s: _Model, p: Player) -> int:
    """How many of the minimums (n > 0) one seat of player p can count toward: one role slot, one class slot and one
    Class:Spec slot at most, each counted in any spec the player may raid as (an offspec switch included)."""
    n = p.signup_name
    roles = {p.role} | ({s.off_specs[n].role} if n in s.o else set())
    specs = {p.spec} | ({p.offspec} if n in s.o and p.offspec else set())
    slots = [slot for slot, k in s.minimums.items() if k > 0]
    return (any(slot in roles for slot in slots if slot in ROLES)
            + any(slot == p.cls for slot in slots)
            + any(slot == f"{p.cls}:{sp}" for slot in slots for sp in specs))


def bench_refund_room(profile: GameProfile, players: list[Player], need: dict[str, int], skip=(),
                      allow_offspec: bool = True) -> dict[str, int]:
    """THE shared rule of the Bench exception (the solver's cap and raidcycle.first_come_bench_out read it): for each of
    tank and healer with a minimum over every roster `need[role]` > 0, how many seats of that role a Bench answer may
    take at most = the minimum minus the joiners HELD to it — status not "bench", not in `skip` (kept off, late),
    playing the role and with no offspec of another role to switch to (switch_spec). A joiner who can play two needed
    roles, or is wanted in another spec for another minimum, is not counted: the solve itself decides where they play,
    and a refunded Bench seat is taken only when it fills more minimum seats than the joiners alone can."""
    out = set(skip)
    room = {}
    for role in ("tank", "healer"):
        n = int(need.get(role, 0))
        if n <= 0:
            continue
        held = sum(1 for p in players if p.status != "bench" and p.signup_name not in out and p.role == role
                   and switch_spec(profile, p, allow_offspec) is None)
        room[role] = max(0, n - held)
    return room


def _bench_terms(s: _Model, roster_bonus: dict[int, dict[str, int]] | None) -> None:
    """Bench last: each seatable Bench answer (status "bench", not in force_out) costs BENCH_SEAT_COST when seated
    (the block above says why it can't be out-bid). The one exception (the designer's, the owner told): a tank or
    healer minimum the joiners cannot meet. Per roster `refund[role, r]` Bench seats of the role, no more than the
    seats the joiners rostered there leave short of the minimum, and over every roster no more than
    bench_refund_room (the joiners held to the role); each refunded seat gets back all of BENCH_SEAT_COST but one
    EXTRA_ROLE_COST. So a refunded Bench seat still costs more than any joiner's seat, and the solve takes it only
    where it fills a minimum seat the joiners can't fill however they are arranged (a hybrid counts once: the only tank
    who could heal tanks, a healer wanted for a spec minimum plays it): its minimum's reward (2×) less that one
    EXTRA_ROLE_COST is more than the displaced joiner's seat earns, and a joiner able to fill the same seat earns the
    same reward without the cost. With no seatable Bench answer it adds nothing, so the model (and its kept answer) is
    exactly what it was."""
    out = set(s.opts.force_out)
    bench = [p for p in s.players if p.status == "bench" and p.signup_name not in out]
    if not bench:
        return
    factor = BENCH_SEAT_FACTOR + 2 * max(0, max(_bench_minimum_slots(s, p) for p in bench) - 1)
    extra = _extra_role_cost(s, roster_bonus)
    cost = factor * extra
    for p in bench:
        s.terms.append(-cost * s.x[p.signup_name])
    names = {p.signup_name for p in bench}
    joiners = {p.signup_name for p in s.players if p.status != "bench" and p.signup_name not in out}
    need = {role: int(s.minimums.get(role, 0)) * s.rosters for role in ("tank", "healer")}
    room = bench_refund_room(s.profile, s.players, need, skip=out, allow_offspec=s.opts.allow_offspec)
    for role, short in room.items():
        if not short:
            continue
        n = need[role] // s.rosters
        refunds = []
        for r in _per_roster(s):
            on_bench = _role_expr(s, role, r, only=names)
            if not on_bench:
                continue
            refund = s.m.NewIntVar(0, n, f"bench_refund_{role}_{r}")
            s.m.Add(refund <= sum(on_bench))
            s.m.Add(refund + sum(_role_expr(s, role, r, only=joiners)) <= n)
            s.terms.append((cost - extra) * refund)
            refunds.append(refund)
        if refunds:
            s.m.Add(sum(refunds) <= short)


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

class Stopped(Exception):
    """The solve was abandoned through its Stop (never a roster: the caller drops the answer and solves again later)."""


class Stop:
    """A handle that abandons a running solve from another thread: CpSolver.stop_search (OR-tools 9.15: "Stops the
    current search asynchronously") on every solver attached to it, and every solve that starts after stop() raises
    Stopped without searching. A solve that ends while stop() races it raises Stopped too, so no answer from a stopped
    handle is ever used. Only the Events board's pictures pass one; the lock never does."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._solvers: set = set()
        self.stopped = False

    def attach(self, solver) -> None:
        with self._lock:
            if self.stopped:
                raise Stopped()
            self._solvers.add(solver)

    def detach(self, solver) -> None:
        with self._lock:
            self._solvers.discard(solver)
            if self.stopped:
                raise Stopped()

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            for solver in list(self._solvers):
                solver.stop_search()



@dataclass(frozen=True)
class _Answer:
    """A model's kept answer: what _extract reads (the seat, group and offspec variables by index) and the summary."""
    values: dict
    objective: float
    status: int
    status_name: str

    def Value(self, var) -> int:  # noqa: N802 — CpSolver's name, so _extract reads either
        return self.values[var.Index()]

    def ObjectiveValue(self) -> float:  # noqa: N802
        return self.objective

    def StatusName(self, status=None) -> str:  # noqa: N802
        return self.status_name


def _model_key(s: _Model) -> str:
    """The model as CP-SAT gets it (the proto: variables, constraints, objective) and the search parameters."""
    h = hashlib.sha1(str(s.m.Proto()).encode())
    h.update(repr((s.opts.time_limit_s, s.opts.workers, RANDOM_SEED, DTIME_PER_SECOND, WALL_CEILING)).encode())
    return h.hexdigest()


def _keep(key: str, s: _Model, solver: cp_model.CpSolver, status: int) -> _Answer:
    read = list(s.x.values()) + list(s.y.values()) + list(s.o.values())
    a = _Answer({v.Index(): int(solver.Value(v)) for v in read}, solver.ObjectiveValue(), status, solver.StatusName(status))
    with _answers_lock:
        _answers[key] = a
        _answers.move_to_end(key)
        while len(_answers) > ANSWERS_MAX:
            _answers.popitem(last=False)
    return a


def _run(s: _Model) -> tuple["cp_model.CpSolver | _Answer", int]:
    """Maximise seat quality + synergy + balance (the objective _build_model set) within the deterministic budget (the same input, the same answer);
    raise when nothing feasible was found. A model answered before takes its kept answer without searching (see
    ANSWERS_MAX); a stopped handle raises Stopped either way."""
    global ceiling_hits
    key = _model_key(s)
    with _answers_lock:
        kept = _answers.get(key)
        if kept is not None:
            _answers.move_to_end(key)
    if kept is not None:
        if s.opts.stop is not None and s.opts.stop.stopped:
            raise Stopped()
        return kept, kept.status
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = RANDOM_SEED
    solver.parameters.interleave_search = True
    solver.parameters.max_deterministic_time = s.opts.time_limit_s * DTIME_PER_SECOND
    solver.parameters.max_time_in_seconds = s.opts.time_limit_s * WALL_CEILING
    solver.parameters.num_workers = s.opts.workers
    if s.opts.stop is None:
        status = solver.Solve(s.m)
    else:
        s.opts.stop.attach(solver)  # Stopped before searching when already stopped
        try:
            status = solver.Solve(s.m)
        finally:
            s.opts.stop.detach(solver)  # Stopped when stopped meanwhile: the answer is never used
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"no roster found: {solver.StatusName(status)}")
    budget = s.opts.time_limit_s * DTIME_PER_SECOND
    if status != cp_model.OPTIMAL and solver.deterministic_time < budget * 0.99:
        ceiling_hits += 1  # the wall clock cut it short of its work: used this once, never kept as the model's answer
        log.warning("a roster solve reached its wall-clock ceiling after %.1f s at %.0f of %.0f work units", solver.wall_time,
                    solver.deterministic_time, budget)
        return solver, status
    return _keep(key, s, solver, status), status


def _extract(s: _Model, solver: "cp_model.CpSolver | _Answer", status: int) -> list[RosterResult]:
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
