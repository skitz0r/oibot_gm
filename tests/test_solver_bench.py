"""The solve takes Bench last (solver level, design §3 of 2026-11-25-member-events-and-bench): a Bench seat costs more
than anything one seat can earn, and with no seatable Bench answer the model is exactly what it was (same proto, same
kept answer). Small models (fast CP-SAT)."""
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


def test_a_restoration_druid_who_fills_a_feral_minimum_leaves_the_healer_seat_to_the_bench():
    ps = [player(0, "Warrior", "Protection", "tank"), player(1, "Priest", "Holy", "healer"),
          player(2, "Priest", "Holy", "healer"), player(3, "Druid", "Restoration", "healer", off="Feral")]
    ps += [player(4 + i, "Mage", "Fire", "ranged") for i in range(7)]
    ps.append(player(11, "Priest", "Holy", "healer", "bench"))
    rosters = solve(ps, minimums={"Druid:Feral": 1})
    got = seated(rosters)
    assert "p11" in got and "p3" in got and len(got) == 10
    assert switched(rosters) == {"p3": "Feral"}
    healers = sum(1 for r in rosters for p in r.selected if p.role == "healer" and p.signup_name not in r.spec_switches)
    assert healers == 3


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


def test_bench_refund_room_counts_only_joiners_held_to_the_role():
    """THE shared rule (the solver's cap and raidcycle.first_come_bench_out read it): per tank / healer, the seats a
    Bench answer may take = the minimum over every roster minus the joiners who can play ONLY that role."""
    ps = [player(0, "Paladin", "Protection", "tank", off="Holy"), player(1, "Priest", "Holy", "healer"),
          player(2, "Priest", "Holy", "healer"), player(3, "Druid", "Restoration", "healer", off="Feral"),
          player(4, "Mage", "Fire", "ranged"), player(5, "Priest", "Holy", "healer", "bench")]
    room = solver.bench_refund_room(FOREVER, ps, {"tank": 1, "healer": 3})
    assert room == {"tank": 1, "healer": 1}
    assert solver.bench_refund_room(FOREVER, ps, {"tank": 1, "healer": 3}, allow_offspec=False) == {"tank": 0, "healer": 0}
    assert solver.bench_refund_room(FOREVER, ps, {"healer": 2}, skip={"p1"}) == {"healer": 1}


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
