"""Bench is strictly last at the lock (design §3 of 2026-11-25-member-events-and-bench): a Bench answer takes a seat
only when no joiner can — a seat left empty once every joiner is placed, or a tank / healer minimum the joiners cannot
meet. Among Bench answers the event's own order applies (priority on a schedule's event, answer order on a
first-to-join one); Bench never adds a roster; kept-off pins and hand placements hold. rc level, real solves."""
from datetime import timedelta

import pytest
from conftest import first_slot, join, weekly
from test_follow_on import LEAD, unlock_hyjal

from oibot_gm import raidcycle as rc
from oibot_gm.registry import Registry
from oibot_gm.roster import solver

RID = "barrow_deeps"
TANK, HEAL, DPS = ("Warrior", "Protection"), ("Priest", "Holy"), ("Mage", "Fire")
_made = [0]


def _name(i: int) -> str:
    return "Bq" + chr(97 + i // 26 % 26) + chr(97 + i % 26)


def people(reg: Registry, n: int, cls_spec: tuple[str, str], rank: str = "raider", off: str | None = None) -> list:
    """`n` new members playing `cls_spec` as their main (`off` its offspec), of `rank` (not puppets: a regular spot needs
    a real member)."""
    out = []
    for _ in range(n):
        i = _made[0] = _made[0] + 1
        uid = Registry.TEST_BASE + 7000 + i
        name = _name(i)
        m, c = reg.add_character(uid, name, name, cls_spec[0], cls_spec[1], off, True)
        c.rank, c.confirmed_by = rank, "t"
        reg.save(m, f"bench test member {name}")
        out.append(reg.members[uid])
    return out


def schedule_event(reg, rs):
    sid = weekly(reg)
    return rc.open_run(reg, rs, RID, first_slot(reg, RID), by="t", schedule=sid)


def one_off(reg, rs, **kw):
    start = (reg.now_local() + timedelta(days=4)).replace(hour=13, minute=15, second=0, microsecond=0)
    return rc.open_run(reg, rs, RID, start, by="t", **kw)


def seated(reg, rs, ev) -> set[str]:
    rc.propose(reg, rs, ev, save=False)
    return {p.signup_name for r in ev.all_rosters for p in r.selected}


def names(ms) -> set[str]:
    return {m.display_name for m in ms}


def base_eight(reg):
    return people(reg, 2, TANK) + people(reg, 2, HEAL) + people(reg, 4, DPS)


def case_one(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg)
    core, raider, trial = (people(reg, 1, DPS, r)[0] for r in ("core", "raider", "trial"))
    join(reg, rs, ev, joiners)
    join(reg, rs, ev, [trial, raider, core], "sub")  # answered in the reverse of their priority
    return ev, joiners, core, raider, trial


def test_empty_seats_go_to_the_bench_by_the_events_order(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    assert seated(reg, rs, ev) == names(joiners) | {core.display_name, raider.display_name}


def test_a_full_roster_of_joiners_seats_no_bench(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg) + people(reg, 4, DPS)
    join(reg, rs, ev, joiners)
    join(reg, rs, ev, people(reg, 1, HEAL, "core") + people(reg, 1, DPS, "core"), "sub")
    rc.propose(reg, rs, ev, save=False)
    assert [p for r in ev.all_rosters for p in r.selected if p.status == "bench"] == []
    assert len(seated(reg, rs, ev)) == 10


def test_a_benched_healer_fills_a_healer_minimum_the_joiners_cannot(reg, rs):
    reg.set_minimums(RID, {"healer": {"min": 2}}, "t")
    ev = schedule_event(reg, rs)
    joiners = people(reg, 12, DPS)
    join(reg, rs, ev, joiners)
    healer = people(reg, 1, HEAL)[0]
    join(reg, rs, ev, [healer], "sub")
    got = seated(reg, rs, ev)
    assert healer.display_name in got
    assert len(got & names(joiners)) == 9, "exactly one more joiner left out than the 10 seated without the bench answer"


def test_a_benched_damage_dealer_never_displaces_a_joiner(reg, rs):
    reg.set_minimums(RID, {"healer": {"min": 2}}, "t")
    ev = schedule_event(reg, rs)
    joiners = people(reg, 12, DPS)
    join(reg, rs, ev, joiners)
    dps = people(reg, 1, DPS, "core")[0]
    join(reg, rs, ev, [dps], "sub")
    got = seated(reg, rs, ev)
    assert dps.display_name not in got and len(got & names(joiners)) == 10


def test_a_strong_bench_answer_never_displaces_a_weak_joiner(reg, rs, monkeypatch):
    """The benched player has every bonus at its highest (rank core, main, regular, sat out last time, every weight at
    the Priority page's top choice) AND alone fills a Paladin minimum; the joiner they could displace is an alt (rank
    "alt": no rank points), no regular spot. Before Bench was last, the 45-point gap lost to that."""
    sid = weekly(reg)
    reg.set_minimums(RID, {"Paladin": {"min": 1}}, "t")
    ev = rc.open_run(reg, rs, RID, first_slot(reg, RID), by="t", schedule=sid)
    reg.config.team(ev.team)["weights"] = {"rank": 6, "main": 6, "sat_out": 6, "signup_order": 6, "regular": 6}
    joiners = people(reg, 2, TANK) + people(reg, 2, HEAL) + people(reg, 6, DPS)
    owner = people(reg, 1, DPS, "trial")[0]
    m, alt = reg.add_character(owner.discord_id, owner.display_name, "Bqaltone", "Mage", "Frost", None, False)
    join(reg, rs, ev, joiners)
    rc.set_signup(reg, rs, ev, reg.members[owner.discord_id], alt.label, "in", source="test")
    star = people(reg, 1, ("Paladin", "Retribution"), "core")[0]
    reg.regular_add(star.discord_id, sid, "t")
    monkeypatch.setattr(rc, "sat_out_last_window", lambda reg, rs, ev, first_only=False: {star.discord_id})
    join(reg, rs, ev, [star], "sub")
    bonus = rc.seat_bonus(reg, rs, ev, rc.players_for(reg, ev))
    assert bonus[star.display_name] >= 6 * 4 + 6 + 6 + 6, bonus  # every weight counted for the benched player
    got = seated(reg, rs, ev)
    assert star.display_name not in got
    assert len(got) == 10 and got <= names(joiners) | {owner.display_name}


def test_a_first_to_join_event_seats_the_bench_in_answer_order_after_every_joiner(reg, rs):
    ev = one_off(reg, rs)
    assert rc.run_first_come(reg, ev)
    joiners = people(reg, 2, TANK) + people(reg, 2, HEAL) + people(reg, 3, DPS)
    join(reg, rs, ev, joiners)
    bench = people(reg, 5, DPS)
    join(reg, rs, ev, bench, "sub")
    order = [bench[3], bench[0], bench[4], bench[1], bench[2]]  # t1 < … < t5, not the order they were added in
    for i, m in enumerate(order):
        sg = ev.signups[str(m.discord_id)]
        sg.updated_at = sg.benched_at = f"2026-11-25T10:0{i + 1}:00+00:00"
    assert seated(reg, rs, ev) == names(joiners) | names(order[:3])


def test_the_bench_never_adds_a_roster(reg, rs):
    ev = one_off(reg, rs)
    assert rc.run_rosters(reg, ev) == 1
    join(reg, rs, ev, base_eight(reg) + people(reg, 2, DPS))
    join(reg, rs, ev, people(reg, 4, TANK) + people(reg, 4, HEAL) + people(reg, 2, DPS), "sub")
    rc.propose(reg, rs, ev, save=False)
    assert len(ev.all_rosters) == 1
    assert [p for p in ev.all_rosters[0].selected if p.status == "bench"] == []


def test_a_kept_off_bench_answer_is_never_seated(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg)
    join(reg, rs, ev, joiners)
    off, other = people(reg, 2, DPS)
    join(reg, rs, ev, [off, other], "sub")
    ev.pins[str(off.discord_id)] = "out"
    got = seated(reg, rs, ev)
    assert got == names(joiners) | {other.display_name} and len(got) == 9


def test_a_hand_placed_bench_answer_keeps_its_group(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, joiners)
    placed = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [placed], "sub")
    ev.layout = [[], [placed.display_name]]  # in memory only: prune_board keeps a Bench name on a saved board from Task 5
    rc.propose(reg, rs, ev, save=False)
    r = ev.all_rosters[0]
    assert placed.display_name in r.groups[1]
    assert len(r.selected) == 10


def test_hand_placed_joiners_fill_every_seat_and_the_bench_stays_off(reg, rs):
    ev = schedule_event(reg, rs)
    t, h, d = people(reg, 2, TANK), people(reg, 2, HEAL), people(reg, 6, DPS)
    join(reg, rs, ev, t + h + d)
    join(reg, rs, ev, people(reg, 1, HEAL, "core") + people(reg, 1, DPS, "core"), "sub")
    board = [[t[0], h[0], *d[:3]], [t[1], h[1], *d[3:]]]
    ev.layout = [[m.display_name for m in g] for g in board]
    rc.propose(reg, rs, ev, save=False)
    r = ev.all_rosters[0]
    assert [sorted(g) for g in r.groups] == [sorted(g) for g in ev.layout]
    assert {p.signup_name for p in r.selected} == names(t + h + d)


def _shape(rosters):
    return [([sorted(g) for g in r.groups], sorted((p.signup_name, p.role) for p in r.selected)) for r in rosters]


@pytest.mark.slow
def test_the_same_inputs_give_the_same_roster_with_bench_answers(reg, rs):
    ev, *_ = case_one(reg, rs)
    seen = []
    for _ in range(2):
        solver.forget_answers()
        seen.append(_shape(rc._solve_run(reg, rs, ev, whatif=False)[1]))
    assert seen[0] == seen[1]


@pytest.mark.slow
def test_preview_lock_and_lock_agree_with_bench_answers(reg, rs):
    import asyncio
    from contextlib import asynccontextmanager

    from oibot_gm.discord_raid import RaidMixin

    class Bare:
        lock_preview = RaidMixin.lock_preview

        @asynccontextmanager
        async def solving(self, reg, key):
            yield

    ev, *_ = case_one(reg, rs)
    shown = asyncio.run(Bare().lock_preview(reg, rs, ev, by="t"))
    rc.propose(reg, rs, ev, save=False)
    assert _shape(shown) == _shape(ev.all_rosters)


def test_a_later_raid_fills_from_the_bench_too(reg, rs):
    """A Hyjal (20) night with Barrow Deeps after it (a raid can't follow itself): 8 joined and 2 benched fill both."""
    unlock_hyjal(reg)
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": RID, "rosters": 1}])
    fev = rc.follow_of(rs, lead)
    assert fev is not None
    joiners = base_eight(reg)
    join(reg, rs, lead, joiners)
    bench = people(reg, 2, DPS)
    join(reg, rs, lead, bench, "sub")
    want = names(joiners) | names(bench)
    for ev in rc.event_parts(rs, lead):  # lock_parts' order: the first raid, then the later one
        ev = rs.events[ev.key]
        rc.propose(reg, rs, ev)
        ev.state = "locked"
        rs.save(ev, "locked (test)")
        assert {p.signup_name for p in ev.all_rosters[0].selected} == want, ev.key
        assert len(ev.all_rosters[0].selected) == 10
    assert len(rc.event_parts(rs, lead)) == 2


def test_a_first_to_join_event_benches_the_healer_when_a_joined_druid_can_heal(reg, rs):
    """The shared rule (solver.bench_refund_room): a Restoration Druid joined, so the joiners together can meet the
    healer minimum; a Druid:Feral minimum never brings the benched healer in. Every joiner plays."""
    reg.set_minimums(RID, {"healer": {"min": 3}, "Druid:Feral": {"min": 1}}, "t")
    ev = one_off(reg, rs)
    assert rc.run_first_come(reg, ev)
    druid = people(reg, 1, ("Druid", "Restoration"), off="Feral")[0]
    joiners = people(reg, 1, TANK) + people(reg, 2, HEAL) + [druid] + people(reg, 6, DPS)
    join(reg, rs, ev, joiners)
    healer = people(reg, 1, HEAL)[0]
    join(reg, rs, ev, [healer], "sub")
    rc.propose(reg, rs, ev, save=False)
    assert {p.signup_name for p in ev.all_rosters[0].selected} == names(joiners)


def test_a_first_to_join_event_seats_the_earlier_bench_answer_when_a_spare_tank_can_heal(reg, rs):
    """The first-to-join twin of H1: a Warrior and a Protection Paladin who can heal, two healers, five damage dealers
    (one seat empty); a benched Mage answered before a benched Priest. The joiners together meet both minimums, so the
    paladin heals and the earlier answer takes the empty seat."""
    reg.set_minimums(RID, {"tank": {"min": 1}, "healer": {"min": 3}}, "t")
    ev = one_off(reg, rs)
    assert rc.run_first_come(reg, ev)
    paladin = people(reg, 1, ("Paladin", "Protection"), off="Holy")[0]
    joiners = people(reg, 1, TANK) + [paladin] + people(reg, 2, HEAL) + people(reg, 5, DPS)
    join(reg, rs, ev, joiners)
    mage, priest = people(reg, 1, DPS)[0], people(reg, 1, HEAL)[0]
    join(reg, rs, ev, [mage, priest], "sub")
    for i, m in enumerate((mage, priest)):
        sg = ev.signups[str(m.discord_id)]
        sg.updated_at = sg.benched_at = f"2026-11-25T10:0{i + 1}:00+00:00"
    rc.propose(reg, rs, ev, save=False)
    r = ev.all_rosters[0]
    assert {p.signup_name for p in r.selected} == names(joiners) | {mage.display_name}
    assert r.spec_switches == {paladin.display_name: "Holy"}


def _p(i: int, cls: str, spec: str, role: str, status: str = "signed", off=None):
    from oibot_gm.models import Player
    return Player(signup_name=f"q{i}", pos=i, status=status, cls=cls, spec=spec, role=role, offspec=off)


def test_first_come_bench_out_keeps_a_benched_healer_only_while_the_joiners_together_are_short(profile):
    """first_come_bench_out reads the joint cap (solver.bench_refund_room): a full roster of joiners whose spare tank
    can heal forces the benched healer out; with the paladin the only tank, the healer is kept for the solve."""
    need = {"tank": 1, "healer": 3}
    paladin = _p(1, "Paladin", "Protection", "tank", off="Holy")
    priests = [_p(2, "Priest", "Holy", "healer"), _p(3, "Priest", "Holy", "healer")]
    mages = [_p(10 + i, "Mage", "Fire", "ranged") for i in range(6)]
    healer = _p(30, "Priest", "Holy", "healer", "bench")
    full = [_p(0, "Warrior", "Protection", "tank"), paladin, *priests, *mages, healer]
    assert rc.first_come_bench_out(profile, full, 10, need, set(), set(), set()) == {"q30"}
    only = [paladin, *priests, *mages, _p(16, "Mage", "Fire", "ranged"), healer]
    assert rc.first_come_bench_out(profile, only, 10, need, set(), set(), set()) == set()


def _bench_queue(reg, rs):
    """A first-to-join event, 8 joined (2 free seats), three benched damage dealers who answered at t1 < t2 < t3 in a
    file from before a Bench answer's time was kept (updated_at only)."""
    ev = one_off(reg, rs)
    assert rc.run_first_come(reg, ev)
    join(reg, rs, ev, base_eight(reg))
    bench = people(reg, 3, DPS)
    join(reg, rs, ev, bench, "sub")
    for i, m in enumerate(bench):
        sg = ev.signups[str(m.discord_id)]
        sg.updated_at, sg.benched_at = f"2000-01-01T10:0{i + 1}:00+00:00", None
    return ev, bench


def test_pressing_bench_again_keeps_a_bench_answers_place(reg, rs):
    ev, (a, b, c) = _bench_queue(reg, rs)
    rc.set_signup(reg, rs, ev, a, None, "sub", source="test")  # the earliest presses Bench again: still first
    assert ev.signups[str(a.discord_id)].order_at == "2000-01-01T10:01:00+00:00"
    got = seated(reg, rs, ev)
    assert a.display_name in got and b.display_name in got and c.display_name not in got


def test_benching_another_character_keeps_a_bench_answers_place(reg, rs):
    ev, (a, b, c) = _bench_queue(reg, rs)
    _m, alt = reg.add_character(a.discord_id, a.display_name, "Bqaltbench", "Mage", "Frost", None, False)
    rc.set_signup(reg, rs, ev, reg.members[a.discord_id], alt.label, "sub", source="test")
    assert ev.signups[str(a.discord_id)].character == alt.label
    got = seated(reg, rs, ev)
    assert a.display_name in got and b.display_name in got and c.display_name not in got


def _sg(status: str, at: str, **kw) -> rc.Signup:
    return rc.Signup(discord_id=1, display_name="a", character="a", cls="Mage", spec="Fire", role="ranged", status=status,
                     updated_at=at, **kw)


def test_join_bench_join_lands_at_the_back_of_the_joiners():
    s = rc.carry_joined(None, _sg("in", "t1"))
    s = rc.carry_joined(s, _sg("sub", "t2"))
    assert (s.joined_at, s.benched_at, s.order_at) == (None, "t2", "t2")
    s = rc.carry_joined(s, _sg("in", "t3"))
    assert (s.joined_at, s.benched_at, s.order_at) == ("t3", None, "t3")


def test_bench_join_bench_lands_at_the_back_of_the_bench():
    s = rc.carry_joined(None, _sg("sub", "t1"))
    assert s.order_at == "t1"
    s = rc.carry_joined(s, _sg("sub", "t2"))  # Bench again: the first Bench's time stays
    assert (s.benched_at, s.order_at) == ("t1", "t1")
    s = rc.carry_joined(s, _sg("in", "t3"))
    s = rc.carry_joined(s, _sg("sub", "t4"))
    assert (s.joined_at, s.benched_at, s.order_at) == (None, "t4", "t4")
    s = rc.carry_joined(s, _sg("out", "t5"))
    assert (s.joined_at, s.benched_at, s.order_at) == (None, None, "t5")


def test_an_older_file_without_a_bench_time_reads_its_last_touch():
    old = rc.Signup.model_validate({"discord_id": 1, "display_name": "a", "character": "a", "cls": "Mage", "spec": "Fire",
                                    "role": "ranged", "status": "sub", "updated_at": "t1"})
    assert old.benched_at is None and old.order_at == "t1"
    assert rc.carry_joined(old, _sg("sub", "t2")).order_at == "t1"


def test_a_benched_player_with_the_lead_seat_bonus_never_takes_a_joiners_seat_on_a_later_raid(reg, rs):
    """A later raid: the benched player was rostered on both raids before it (LEAD_SEAT_BONUS twice, never charged as
    extra), the joiners on none; ten joiners for ten seats → the joiners play (_solve_run's own `earlier` path)."""
    unlock_hyjal(reg)
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": RID, "rosters": 1}])
    fev = rc.follow_of(rs, lead)
    joiners = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, lead, joiners)
    star = people(reg, 1, DPS, "core")[0]
    join(reg, rs, lead, [star], "sub")
    fev = rs.events[fev.key]
    assert str(star.discord_id) in fev.signups
    _players, rosters, _n = rc._solve_run(reg, rs, fev, whatif=False, earlier={star.discord_id: 2})
    got = {p.signup_name for r in rosters for p in r.selected}
    assert got == names(joiners)
