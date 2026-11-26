"""The solve takes Bench last (solver level, design §3 of 2026-11-25-member-events-and-bench): a Bench seat costs more
than anything one seat can earn, and with no seatable Bench answer the model is exactly what it was (same proto, same
kept answer). Small models (fast CP-SAT)."""
from pathlib import Path

from oibot_gm.models import Player
from oibot_gm.profiles import GameProfile
from oibot_gm.roster import solver
from oibot_gm.roster.solver import SolveOptions, solve_rosters

FOREVER = GameProfile.load(Path(__file__).resolve().parents[1] / "profiles" / "forever")
RID = "barrow_deeps"


def player(i: int, cls: str, spec: str, role: str, status: str = "signed") -> Player:
    return Player(signup_name=f"p{i}", pos=i, status=status, cls=cls, spec=spec, role=role)


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
    """The model solve_rosters builds, step for step, with or without the bench step."""
    s = solver._setup(FOREVER, ps, RID, opts, rosters, "balanced")
    solver._build_vars(s)
    solver._add_seat_and_group_constraints(s)
    solver._add_forces_pairs_and_pins(s)
    solver._add_avoid(s, None, None, 4)
    solver._selection_terms(s, None)
    solver._minimum_terms(s, None)
    solver._extra_role_terms(s, None)
    if with_bench:
        solver._bench_terms(s, None)
    solver._preference_terms(s)
    solver._synergy_terms(s)
    solver._raid_cover_terms(s)
    solver._add_symmetry_break(s)
    solver._balance_terms(s)
    s.m.Maximize(sum(s.terms) + sum(s.synergy_terms) + sum(s.balance_terms))
    return str(s.m.Proto())  # what the kept-answer key hashes


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
