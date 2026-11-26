"""The solve takes Bench last (solver level, design §3 of 2026-11-25-member-events-and-bench): a Bench seat costs more
than anything one seat can earn, and with no seatable Bench answer the model is exactly what it was (same proto, same
kept answer). Small models (fast CP-SAT)."""
import itertools
import random
from pathlib import Path

import pytest

from oibot_gm.models import Player
from oibot_gm.profiles import GameProfile
from oibot_gm.roster import solver
from oibot_gm.roster.solver import SolveOptions, solve_rosters

FOREVER = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "forever")
RID = "barrow_deeps"


def player(i: int, cls: str, spec: str, role: str, status: str = "signed", off: str | None = None) -> Player:
    return Player(signup_name=f"p{i}", pos=i, status=status, cls=cls, spec=spec, role=role, offspec=off)


def pool(n_dps: int, n_heal: int = 2, n_tank: int = 2, bench: tuple = ()) -> list[Player]:
    out = []
    for cls, spec, role, n in (("Warrior", "Protection", "tank", n_tank), ("Priest", "Holy", "healer", n_heal),
                               ("Mage", "Fire", "ranged", n_dps)):
        out += [player(len(out) + j, cls, spec, role) for j in range(n)]
    out += [player(len(out) + j, *b, status="bench") for j, b in enumerate(bench)]
    return out


def seated(rosters) -> set[str]:
    return {p.signup_name for r in rosters for p in r.selected}


def solve(ps, rosters=1, **kw):
    return solve_rosters(FOREVER, ps, RID, SolveOptions(raid_size=10, time_limit_s=2.0, **kw), rosters=rosters)


def built(ps, opts: SolveOptions, rosters: int = 1, with_bench: bool = True):
    """The model solve_rosters builds (solver._build_model, the one entry point it calls), with or without the bench
    step, as the text the kept-answer key hashes."""
    with pytest.MonkeyPatch.context() as mp:
        if not with_bench:
            mp.setattr(solver, "_bench_terms", lambda s, roster_bonus: None)
        s = solver._build_model(FOREVER, ps, RID, opts, rosters, "balanced", None, None, 4, None)
    return str(s.m.Proto())


def test_no_seatable_bench_answer_adds_nothing_to_the_model():
    ps = pool(8)
    for rosters in (1, 2):
        a = built(ps, SolveOptions(raid_size=10, minimums={"healer": 2}), rosters, with_bench=True)
        b = built(ps, SolveOptions(raid_size=10, minimums={"healer": 2}), rosters, with_bench=False)
        assert a == b
    benched = pool(6, bench=(("Mage", "Fire", "ranged"), ("Priest", "Holy", "healer")))
    out = tuple(p.signup_name for p in benched if p.status == "bench")
    opts = SolveOptions(raid_size=10, force_out=out)
    assert built(benched, opts, with_bench=True) == built(benched, opts, with_bench=False)
    # and a seatable Bench answer does add its terms (the comparison above is not vacuous)
    opts = SolveOptions(raid_size=10)
    assert built(benched, opts, with_bench=True) != built(benched, opts, with_bench=False)


def test_bench_seats_cost_more_than_a_class_minimum_earns():
    ps = pool(6) + [player(20, "Paladin", "Retribution", "melee", "bench")]
    got = seated(solve(ps, minimums={"Paladin": 1}))
    assert "p20" not in got and len(got) == 10


def test_bench_seats_cost_more_than_every_bonus_together():
    """A benched Holy Paladin who alone could fill a healer, a Paladin AND a Paladin:Holy minimum, with a seat bonus
    far past any weight, against a damage dealer joiner with none: the joiners meet the healer minimum, so the bench
    answer stays off."""
    ps = pool(6) + [player(20, "Paladin", "Holy", "healer", "bench")]
    got = seated(solve(ps, minimums={"healer": 3, "Paladin": 1, "Paladin:Holy": 1}, bonus={"p20": 5000}))
    # healer 3: the two joined healers can't meet it — the exception refunds one Bench healer seat
    assert "p20" in got and len(got) == 10
    got = seated(solve(ps, minimums={"healer": 2, "Paladin": 1, "Paladin:Holy": 1}, bonus={"p20": 5000}))
    assert "p20" not in got and len(got) == 10


def test_a_benched_healer_refund_is_only_for_the_seats_short():
    """Healer minimum 2, one joined healer, two benched healers: one benched healer is rostered for the minimum, the
    other is not (one joiner left out, not two)."""
    ps = pool(8, n_heal=1, bench=(("Priest", "Holy", "healer"), ("Priest", "Holy", "healer")))
    got = seated(solve(ps, minimums={"healer": 2}))
    assert len(got & {"p11", "p12"}) == 1 and len(got) == 10


def test_an_empty_seat_goes_to_the_bench():
    ps = pool(4, bench=(("Mage", "Fire", "ranged"), ("Mage", "Fire", "ranged"), ("Mage", "Fire", "ranged")))
    got = seated(solve(ps, bonus={"p8": 5, "p9": 3, "p10": 1}))
    assert got == {f"p{i}" for i in range(8)} | {"p8", "p9"}


def switched(rosters) -> dict[str, str]:
    return {n: sp for r in rosters for n, sp in r.spec_switches.items()}


# The tank / healer exception counts a hybrid once (the fix round of task 4): the forever game's 10-player minimums
# are 1 tank and 3 healers. A joiner who can play two needed roles fills only one of them.

def test_the_only_tank_with_a_healing_offspec_leaves_the_short_healer_seat_to_the_bench():
    ps = [player(0, "Paladin", "Protection", "tank", off="Holy"), player(1, "Priest", "Holy", "healer"),
          player(2, "Priest", "Holy", "healer")] + [player(3 + i, "Mage", "Fire", "ranged") for i in range(8)]
    ps.append(player(11, "Priest", "Holy", "healer", "bench"))
    rosters = solve(ps)
    got = seated(rosters)
    assert "p11" in got and "p0" in got and len(got) == 10
    assert switched(rosters) == {}  # the paladin tanks
    assert len(got & {f"p{3 + i}" for i in range(8)}) == 6


def test_a_guardian_druid_who_can_heal_is_still_the_only_tank():
    ps = [player(0, "Druid", "Guardian", "tank", off="Restoration"), player(1, "Priest", "Holy", "healer"),
          player(2, "Priest", "Holy", "healer")] + [player(3 + i, "Mage", "Fire", "ranged") for i in range(8)]
    ps.append(player(11, "Priest", "Holy", "healer", "bench"))
    rosters = solve(ps)
    assert "p11" in seated(rosters) and switched(rosters) == {}


def test_a_feral_minimum_never_brings_the_bench_in_over_a_joiner():
    """A class or spec minimum never brings a benched player in over a joiner (the owner's rule, ruling of fix round 2):
    a Restoration Druid joined, so the joiners together can meet the healer minimum and nothing is refunded. The
    benched healer stays off, every joined Mage plays, and the roster is exactly the joiners' own (where the druid
    plays between two minimums of equal weight is the joiners' solve, untouched by the Bench)."""
    def joiners():
        ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Priest", "Holy", "healer"),
              player(2, "Priest", "Holy", "healer"), player(3, "Druid", "Restoration", "healer", off="Feral")]
        return ps + [player(4 + i, "Mage", "Fire", "ranged") for i in range(6)]

    alone = solve(joiners(), minimums={"Druid:Feral": 1})
    rosters = solve(joiners() + [player(11, "Priest", "Holy", "healer", "bench")], minimums={"Druid:Feral": 1},
                    bonus={"p11": 5000})
    assert seated(rosters) == {f"p{i}" for i in range(10)}
    assert switched(rosters) == switched(alone)
    assert rosters[0].groups == alone[0].groups


def test_two_benched_healers_for_one_short_seat_in_a_split_roster_one_plays():
    """Two rosters of 10 (a tank and three healers each): a Warrior and a Protection Paladin who can heal (needed as the
    second tank), five healers for six healer seats, thirteen damage dealers, two benched healers → one plays."""
    ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Paladin", "Protection", "tank", off="Holy")]
    ps += [player(2 + i, "Priest", "Holy", "healer") for i in range(5)]
    ps += [player(7 + i, "Mage", "Fire", "ranged") for i in range(13)]
    ps += [player(20, "Priest", "Holy", "healer", "bench"), player(21, "Priest", "Holy", "healer", "bench")]
    rosters = solve(ps, rosters=2)
    got = seated(rosters)
    assert len(got) == 20 and len(got & {"p20", "p21"}) == 1
    assert {f"p{i}" for i in range(7)} <= got and switched(rosters) == {}
    assert all(sum(1 for p in r.selected if p.role == "healer") == 3 for r in rosters)


def test_a_spare_tank_who_can_heal_heals_and_the_bench_stays_off():
    """Two tanks joined, one a Protection Paladin with a Holy offspec; two healers; the third healer seat. The
    joiners CAN meet both minimums (the spare tank heals; the lock may switch a joiner to their offspec), so the benched
    healer stays off, however strong."""
    ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Paladin", "Protection", "tank", off="Holy"),
          player(2, "Priest", "Holy", "healer"), player(3, "Priest", "Holy", "healer")]
    ps += [player(4 + i, "Mage", "Fire", "ranged") for i in range(8)]
    ps.append(player(12, "Priest", "Holy", "healer", "bench"))
    rosters = solve(ps, bonus={"p12": 5000})
    got = seated(rosters)
    assert "p12" not in got and {"p0", "p1", "p2", "p3"} <= got and len(got) == 10
    assert switched(rosters) == {"p1": "Holy"}


def _brute_cover(caps: list[set[str]], t: int, h: int) -> tuple[int, int, int]:
    """Every way the joiners (each a set of the roles they can fill) can take the tank / healer minimum seats, one seat
    each at most: the most seats filled together, and among those ways the most tank / healer seats left short."""
    best, short_t, short_h = -1, 0, 0
    for pick in itertools.product(*[[None, *sorted(c)] for c in caps]):
        tanks, heals = pick.count("tank"), pick.count("healer")
        if tanks > t or heals > h:
            continue
        n = tanks + heals
        if n > best:
            best, short_t, short_h = n, t - tanks, h - heals
        elif n == best:
            short_t, short_h = max(short_t, t - tanks), max(short_h, h - heals)
    return best, short_t, short_h


def test_role_cover_is_the_largest_matching_by_brute_force():
    """THE joint capacity check: joiners who can only tank (a), only heal (b) or either (c) against t tank and h healer
    minimum seats. Hall's theorem over the two seat kinds gives the largest matching min(t + h, h + a + c, t + b + c,
    a + b + c); the most tank seats any largest matching leaves short is t - (M - min(h, b + c)), healers alike."""
    rng = random.Random(20261125)
    kinds = [{"tank"}, {"healer"}, {"tank", "healer"}]
    for _ in range(400):
        caps = [rng.choice(kinds) for _ in range(rng.randint(0, 6))]
        t, h = rng.randint(0, 3), rng.randint(0, 4)
        a, b, c = (sum(1 for x in caps if x == k) for k in kinds)
        assert solver.role_cover(a, b, c, t, h) == _brute_cover(caps, t, h), (a, b, c, t, h)


def test_bench_refund_room_is_what_the_joiners_together_leave_short():
    """THE shared rule (the solver's cap and raidcycle.first_come_bench_out read it): the joiners (not bench, not
    skipped) matched against the tank and healer minimum seats; a hybrid fills one seat; class and spec minimums
    never enter it."""
    tank_heal = player(0, "Paladin", "Protection", "tank", off="Holy")
    priests = [player(1, "Priest", "Holy", "healer"), player(2, "Priest", "Holy", "healer")]
    druid = player(3, "Druid", "Restoration", "healer", off="Feral")
    bench = player(5, "Priest", "Holy", "healer", "bench")
    need = {"tank": 1, "healer": 3}
    # the only tank can heal: one seat short, either kind
    room = solver.bench_refund_room(FOREVER, [tank_heal, *priests, bench], need)
    assert (room.total, room.roles) == (1, {"tank": 1, "healer": 1})
    # a Restoration druid heals the third seat whatever a Feral minimum wants
    room = solver.bench_refund_room(FOREVER, [tank_heal, *priests, druid, bench], need)
    assert (room.total, room.roles) == (0, {"tank": 0, "healer": 0})
    # a spare tank who can heal: nothing short (H1)
    warrior = player(6, "Warrior", "Protection", "tank")
    room = solver.bench_refund_room(FOREVER, [warrior, tank_heal, *priests, bench], need)
    assert room.total == 0
    # a Retribution paladin who can heal counts as a healer (H2)
    ret = player(7, "Paladin", "Retribution", "melee", off="Holy")
    assert solver.bench_refund_room(FOREVER, [warrior, ret, *priests, bench], need).total == 0
    assert solver.bench_refund_room(FOREVER, [warrior, ret, *priests], need, allow_offspec=False).roles == {"tank": 0, "healer": 1}
    # a skipped joiner (kept off, late) counts for nothing
    assert solver.bench_refund_room(FOREVER, [warrior, *priests], {"healer": 2}, skip={"p1"}).roles == {"healer": 1}


def test_a_spare_tank_heals_and_the_events_order_picks_the_bench_answer_for_the_empty_seat():
    """H1: two tanks joined (one a Protection Paladin who can heal), two healers, one seat empty; a benched Mage with a
    big bonus and a benched Priest. The joiners together meet both minimums, so the paladin heals and the stronger Mage
    takes the empty seat."""
    ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Paladin", "Protection", "tank", off="Holy"),
          player(2, "Priest", "Holy", "healer"), player(3, "Priest", "Holy", "healer")]
    ps += [player(4 + i, "Mage", "Fire", "ranged") for i in range(5)]
    ps += [player(20, "Mage", "Fire", "ranged", "bench"), player(21, "Priest", "Holy", "healer", "bench")]
    rosters = solve(ps, bonus={"p20": 5000})
    got = seated(rosters)
    assert got == {f"p{i}" for i in range(9)} | {"p20"}
    assert switched(rosters) == {"p1": "Holy"}


def test_a_retribution_paladin_who_can_heal_heals_and_the_mage_takes_the_empty_seat():
    """H2: a Retribution / Holy joiner, healers short by one, one seat empty: the joiner heals, the benched Mage (the
    event's order) takes the empty seat, the benched Priest stays off."""
    ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Paladin", "Retribution", "melee", off="Holy"),
          player(2, "Priest", "Holy", "healer"), player(3, "Priest", "Holy", "healer")]
    ps += [player(4 + i, "Mage", "Fire", "ranged") for i in range(5)]
    ps += [player(20, "Mage", "Fire", "ranged", "bench"), player(21, "Priest", "Holy", "healer", "bench")]
    rosters = solve(ps, bonus={"p20": 5000})
    assert seated(rosters) == {f"p{i}" for i in range(9)} | {"p20"}
    assert switched(rosters) == {"p1": "Holy"}


def test_a_class_minimum_chooses_among_bench_answers_for_an_empty_seat():
    """One seat empty, a Paladin minimum the joiners miss, a benched Paladin and a benched Mage: the Paladin takes the
    seat whatever the Mage's bonus (a Bench seat counts toward every minimum as a joiner's does)."""
    for bonus in (50, 5000):
        ps = pool(5, n_heal=3, n_tank=1) + [player(20, "Paladin", "Retribution", "melee", "bench"),
                                            player(21, "Mage", "Fire", "ranged", "bench")]
        got = seated(solve(ps, minimums={"Paladin": 1}, bonus={"p21": bonus}))
        assert got == {f"p{i}" for i in range(9)} | {"p20"}, bonus


def test_a_refunded_healer_seat_never_costs_a_joined_healer_their_seat():
    """Healers short by one (cap 1): a benched Holy Paladin who also fills a Paladin minimum takes the refunded seat,
    and both joined healers play although the joiners outnumber the seats."""
    ps = pool(8, n_heal=2, n_tank=1) + [player(20, "Paladin", "Holy", "healer", "bench"),
                                        player(21, "Priest", "Holy", "healer", "bench")]
    got = seated(solve(ps, minimums={"Paladin": 1}, bonus={"p20": 5000, "p21": 5000}))
    assert {"p0", "p1", "p2", "p20"} <= got and "p21" not in got and len(got) == 10


def test_a_class_minimum_the_bench_brings_never_takes_a_healer_seat_a_joiner_can_fill():
    """A Restoration Druid joined who can heal the third healer seat; a benched Holy Paladin would ALSO fill a Paladin
    (and a Paladin:Holy) minimum. A Bench seat never counts toward a class, spec, melee or ranged minimum, so the druid
    heals and the paladin stays off."""
    for mins in ({"Paladin": 1}, {"Paladin": 1, "Paladin:Holy": 1}):
        ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Priest", "Holy", "healer"),
              player(2, "Priest", "Holy", "healer"), player(3, "Druid", "Restoration", "healer", off="Feral")]
        ps += [player(4 + i, "Mage", "Fire", "ranged") for i in range(6)]
        ps.append(player(11, "Paladin", "Holy", "healer", "bench"))
        rosters = solve(ps, minimums=mins)
        got = seated(rosters)
        assert "p11" not in got and len(got) == 10, mins
        assert switched(rosters) == {}, mins


def test_a_class_and_spec_minimum_and_a_buff_never_bring_the_bench_in():
    """A benched Retribution Paladin (an aura for a group, a Paladin and a Paladin:Retribution minimum only they could
    fill, a big bonus) against a full roster of joiners: they stay off."""
    ps = pool(6) + [player(20, "Paladin", "Retribution", "melee", "bench")]
    got = seated(solve(ps, minimums={"Paladin": 1, "Paladin:Retribution": 1}, bonus={"p20": 5000}))
    assert got == {f"p{i}" for i in range(10)}


def test_a_surplus_healer_who_joined_plays_before_a_benched_damage_dealer():
    """Ten joiners, one a healer past the minimum (an extra healer costs one EXTRA_ROLE_COST): a benched Mage with a
    big bonus still stays off."""
    ps = pool(5, n_heal=4, n_tank=1) + [player(20, "Mage", "Fire", "ranged", "bench")]
    got = seated(solve(ps, bonus={"p20": 5000}))
    assert got == {f"p{i}" for i in range(10)}


@pytest.mark.slow
def test_forty_seats_at_the_locks_budget_take_the_bench_last():
    """A 40-player raid at the lock's own budget: 38 joiners (the minimums met), six benched damage dealers with
    bonuses → every joiner plays and the two strongest Bench answers take the two empty seats."""
    ps = pool(26, n_heal=8, n_tank=4)
    ps += [player(40 + i, "Mage", "Fire", "ranged", "bench") for i in range(6)]
    bonus = {f"p{40 + i}": 10 * i for i in range(6)}
    got = seated(solve_rosters(FOREVER, ps, "onyxias_lair",
                               SolveOptions(raid_size=40, time_limit_s=12.0, bonus=bonus), rosters=1))
    assert got == {f"p{i}" for i in range(38)} | {"p44", "p45"}
