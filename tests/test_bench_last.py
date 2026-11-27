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


def fill_box(reg, ev, rs):
    """The lock box with Auto-fill empty roster slots ticked (issue 14): the lock these cases test is the one that fills
    (rc.propose), so its rule sentences are read here — the leading open-slots sentence, a count of the board as it
    stands, is set aside so the head is sentences[0] and who is told sentences[1], as before the choice existed."""
    from oibot_gm import consequences as cq

    b = cq.lock_box(reg, ev, rs, fill=True)
    import re

    return {**b, "sentences": [x for x in b["sentences"] if not (re.search(r"\d+ open slots?\b", x) and x is b["sentences"][0])]}


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
    ev.layout = [[], [placed.display_name]]  # in memory (the saved board: test_a_saved_hand_placed_bench_answer_keeps_its_group_at_the_lock)
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


# ---------------------------------------------------------------- the board keeps Bench names (task 5)

def test_board_names_are_join_and_bench_while_open_and_join_plus_the_roster_once_locked(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    out = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [out], "out")
    assert rc.board_names(ev) == names(joiners) | names([core, raider, trial])
    rc.propose(reg, rs, ev, save=False)
    ev.state = "locked"
    assert rc.board_names(ev) == names(joiners) | {core.display_name, raider.display_name}


def test_a_bench_name_can_be_placed_and_survives_a_save(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    layout = rc.apply_move(reg, ev, trial.display_name, (1, 0))
    assert trial.display_name in layout[1]
    ev.layout = layout
    rs.save(ev, "board (test)")
    assert trial.display_name in rs.events[ev.key].layout[1]
    assert "off the board" not in "\n".join(ev.log)


def test_a_saved_hand_placed_bench_answer_keeps_its_group_at_the_lock(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, joiners)
    placed = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [placed], "sub")
    ev.layout = [[], [placed.display_name]]
    rs.save(ev, "board (test)")
    assert ev.layout == [[], [placed.display_name]]
    rc.propose(reg, rs, ev, save=False)
    r = ev.all_rosters[0]
    assert placed.display_name in r.groups[1]
    assert len(r.selected) == 10


def test_a_bench_answer_placed_by_hand_in_a_second_rosters_group_makes_that_roster_count(reg, rs):
    """The hand placement is the officer's decision: eight joiners make one roster, a Bench answer placed in the
    second roster's first group makes two (rc._roster_count reads the board's highest used group)."""
    ev, joiners, core, raider, trial = case_one(reg, rs)
    k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    ev.layout = [[] for _ in range(k)] + [[trial.display_name]]
    rs.save(ev, "board (test)")
    assert trial.display_name in ev.layout[k]
    rc.propose(reg, rs, ev, save=False)
    assert len(ev.all_rosters) == 2
    assert trial.display_name in ev.all_rosters[1].groups[0]


def test_a_placed_bench_name_whose_answer_becomes_no_thanks_leaves_the_board(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    ev.layout = rc.apply_move(reg, ev, trial.display_name, (0, 0))
    rs.save(ev, "board (test)")
    assert trial.display_name in ev.layout[0]
    rc.set_signup(reg, rs, ev, trial, None, "out", source="test")
    ev = rs.events[ev.key]
    assert trial.display_name not in {n for g in ev.layout or [] for n in g}
    assert f"off the board: {trial.display_name}" in ev.log
    rc.propose(reg, rs, ev, save=False)
    assert trial.display_name not in {p.signup_name for p in ev.seated()}


def test_a_placed_bench_name_whose_answer_becomes_join_stays(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    ev.layout = rc.apply_move(reg, ev, trial.display_name, (0, 0))
    rs.save(ev, "board (test)")
    rc.set_signup(reg, rs, ev, trial, None, "in", source="test")
    assert trial.display_name in rs.events[ev.key].layout[0]


def test_a_placed_join_name_whose_answer_becomes_bench_stays(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    who = joiners[-1]
    ev.layout = rc.apply_move(reg, ev, who.display_name, (1, 0))
    rs.save(ev, "board (test)")
    rc.set_signup(reg, rs, ev, who, None, "sub", source="test")
    assert who.display_name in rs.events[ev.key].layout[1]


def test_a_name_with_no_join_or_bench_is_refused_on_the_board(reg, rs):
    ev, *_ = case_one(reg, rs)
    out = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [out], "out")
    with pytest.raises(ValueError) as e:
        rc.apply_move(reg, ev, out.display_name, (0, 0))
    assert str(e.value) == rc.NOT_ON_SHEET.format(name=out.display_name)
    with pytest.raises(ValueError):
        rc.apply_move(reg, ev, "Nobody here", (0, 0))


def test_a_bench_answer_off_a_locked_roster_is_refused_on_the_board_in_its_own_words(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    rc.propose(reg, rs, ev, save=False)
    ev.state = "locked"
    with pytest.raises(ValueError) as e:
        rc.apply_move(reg, ev, trial.display_name, (0, 0))
    assert str(e.value) == rc.BENCH_LOCKED.format(name=trial.display_name)
    rc.apply_move(reg, ev, core.display_name, None)  # a rostered Bench answer moves like anyone rostered


def test_auto_fill_places_the_bench_answers_the_lock_would_seat(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    layout, auto = rc.autofill(reg, rs, ev)
    placed = {n for g in layout for n in g}
    assert placed == names(joiners) | {core.display_name, raider.display_name}
    assert set(auto) == placed
    # a second press replaces the auto placements and keeps a hand-placed Bench name
    ev.layout = [[trial.display_name]] + [list(g) for g in layout[1:]]
    ev.auto_placed = sorted(placed - {trial.display_name} - set(layout[0]))
    rs.save(ev, "board (test)")
    layout2, auto2 = rc.autofill(reg, rs, ev)
    assert trial.display_name in layout2[0] and trial.display_name not in auto2
    assert len({n for g in layout2 for n in g}) == 10


def test_preview_lock_lock_and_auto_fill_land_on_one_roster_with_bench(reg, rs):
    import asyncio
    from contextlib import asynccontextmanager

    from oibot_gm.discord_raid import RaidMixin

    class Bare:
        lock_preview = RaidMixin.lock_preview

        @asynccontextmanager
        async def solving(self, reg, key):
            yield

    ev, *_ = case_one(reg, rs)
    layout, _auto = rc.autofill(reg, rs, ev)
    shown = asyncio.run(Bare().lock_preview(reg, rs, ev, by="t"))
    rc.propose(reg, rs, ev, save=False)
    filled = {n for g in layout for n in g}
    assert filled == {p.signup_name for r in shown for p in r.selected} == {p.signup_name for p in ev.seated()}


def test_a_board_with_an_open_seat_and_a_bench_answer_is_not_complete(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg)
    join(reg, rs, ev, joiners)
    bench = people(reg, 2, DPS)
    join(reg, rs, ev, bench, "sub")
    ns = sorted(names(joiners))
    ev.layout = [ns[:5], ns[5:]]
    rs.save(ev, "board (test)")
    assert not rc.board_complete(reg, rs, ev)
    ev.layout = [ns[:5], ns[5:] + sorted(names(bench))]
    rs.save(ev, "board (test)")
    assert rc.board_complete(reg, rs, ev)


def test_a_board_with_every_seat_placed_is_complete_with_bench_answers_left(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, joiners)
    join(reg, rs, ev, people(reg, 1, DPS), "sub")
    ns = sorted(names(joiners))
    ev.layout = [ns[:5], ns[5:]]
    rs.save(ev, "board (test)")
    assert rc.board_complete(reg, rs, ev)


def test_a_locked_board_edit_keeps_a_rostered_bench_answer(reg, rs):
    import asyncio

    from test_raid_night import NightBot

    bot = NightBot(reg, rs)
    ev, joiners, core, raider, trial = case_one(reg, rs)
    asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    ev = rs.events[ev.key]
    assert ev.state == "locked"
    before = {p.signup_name for p in ev.seated()}
    assert {core.display_name, raider.display_name} <= before
    groups = [list(g) for g in reversed(rc.board_layout(reg, ev))]  # the roster's groups swapped
    asyncio.run(bot._write_board(reg, rs, ev, groups, "Jon"))
    ev = rs.events[ev.key]
    assert {p.signup_name for p in ev.seated()} == before
    assert core.display_name in {n for g in ev.all_rosters[0].groups for n in g}


def test_plain_texts_whole_board_takes_a_bench_name_and_refuses_no_thanks(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    ev, joiners, core, raider, trial = case_one(reg, rs)
    out = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [out], "out")
    ok = configops.describe(reg, ConfigOp(op="run_board", target=ev.key, value=trial.display_name))
    assert "haven't answered" not in ok and trial.display_name in ok
    assert rc.NOT_ON_SHEET_MANY.format(names=out.display_name) in configops.describe(
        reg, ConfigOp(op="run_board", target=ev.key, value=out.display_name))


# ---------------------------------------------------------------- task 5 fix: Auto-fill's Bench places, Clear, words

def _autofilled_case_one(reg, rs):
    """Case 1 with Auto-fill's board written as the page writes it (auto placements): the two Bench answers fill the
    two empty seats."""
    ev, joiners, core, raider, trial = case_one(reg, rs)
    layout, auto = rc.autofill(reg, rs, ev)
    ev.layout, ev.auto_placed = layout, sorted(auto)
    rs.save(ev, "auto-fill (test)")
    assert {core.display_name, raider.display_name} <= {n for g in rs.events[ev.key].layout for n in g}
    return rs.events[ev.key], joiners, core, raider, trial


def test_a_join_after_auto_fill_comes_before_the_bench_answers_it_placed(reg, rs):
    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    late = people(reg, 2, DPS)
    join(reg, rs, ev, late)
    ev = rs.events[ev.key]
    assert names(late) | names(joiners) == set(seated(reg, rs, ev)), "both later joiners play, no Bench answer does"
    lf = rc.lock_forces(reg, rs, ev)
    assert core.display_name not in lf.kept_on and core.display_name not in lf.placed


def test_a_join_after_auto_fill_leaves_the_bench_answer_a_seat_still_empty(reg, rs):
    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    late = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [late])
    ev = rs.events[ev.key]
    assert set(seated(reg, rs, ev)) == names(joiners) | {late.display_name, core.display_name}


def test_preview_lock_after_auto_fill_puts_a_new_joiner_before_the_bench(reg, rs):
    import asyncio
    from contextlib import asynccontextmanager

    from oibot_gm.discord_raid import RaidMixin

    class Bare:
        lock_preview = RaidMixin.lock_preview

        @asynccontextmanager
        async def solving(self, reg, key):
            yield

    ev, joiners, *_ = _autofilled_case_one(reg, rs)
    late = people(reg, 2, DPS)
    join(reg, rs, ev, late)
    ev = rs.events[ev.key]
    shown = asyncio.run(Bare().lock_preview(reg, rs, ev, by="t"))
    assert {p.signup_name for r in shown for p in r.selected} == names(joiners) | names(late)


def test_a_bench_answer_an_officer_placed_is_kept_on_beside_auto_fills(reg, rs):
    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    ev.layout = [list(ev.layout[0]) + [trial.display_name]] + [list(g) for g in ev.layout[1:]]
    rs.save(ev, "hand place (test)")  # trial is NOT in auto_placed: the officer's decision
    join(reg, rs, ev, people(reg, 2, DPS))
    ev = rs.events[ev.key]
    lf = rc.lock_forces(reg, rs, ev)
    assert trial.display_name in lf.kept_on and core.display_name not in lf.kept_on
    assert trial.display_name in seated(reg, rs, ev)


def test_a_board_holding_an_auto_fill_bench_place_is_not_complete(reg, rs):
    ev, *_ = _autofilled_case_one(reg, rs)
    assert not rc.board_complete(reg, rs, ev), "the lock decides the Bench place again, so the board is not the roster"


def test_clear_the_board_counts_placed_bench_names(reg, rs):
    from oibot_gm import consequences as cq

    ev, joiners, core, raider, trial = case_one(reg, rs)
    ev.layout = [[core.display_name, raider.display_name]]
    rs.save(ev, "bench only (test)")
    b = cq.clear_board_box(reg, rs, ev)
    assert b["verb"] == "Clear the board" and b["sentences"][0] == "The 2 Bench answers on the board go back to Bench.", b
    ev.layout = [[core.display_name, joiners[0].display_name, joiners[1].display_name]]
    rs.save(ev, "mixed (test)")
    assert cq.clear_board_box(reg, rs, ev)["sentences"][0] == "The 3 people on the board go back to Not placed or Bench, by their answer."
    ev.layout = [[core.display_name]]
    rs.save(ev, "one (test)")
    assert cq.clear_board_box(reg, rs, ev)["sentences"][0] == "The one Bench answer on the board goes back to Bench."


def test_the_board_refusals_are_whole_sentences(reg, rs):
    from oibot_gm import configops
    from oibot_gm import consequences as cq
    from oibot_gm.configops import ConfigOp

    for s in (rc.NOT_ON_SHEET.format(name="Bqzz"), rc.NOT_ON_SHEET_MANY.format(names="Bqzz"), rc.BENCH_LOCKED.format(name="Bqzz")):
        assert s.endswith(".") and len(s.split()) <= 20, s
    ev, joiners, core, raider, trial = case_one(reg, rs)
    out = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [out], "out")
    b = cq.board_move_box(reg, ev, out.display_name, (0, 0))
    assert b["sentences"] == [rc.NOT_ON_SHEET.format(name=out.display_name)], b
    d = configops.describe(reg, ConfigOp(op="run_move", target=ev.key, member=out.display_name, value="g1"))
    assert rc.NOT_ON_SHEET.format(name=out.display_name) in d and ".." not in d, d


def test_the_parsers_board_lists_unplaced_bench_answers(reg, rs):
    from oibot_gm import configops

    ev, joiners, core, raider, trial = case_one(reg, rs)
    ev.layout = [[joiners[0].display_name, core.display_name]]
    text = configops.board_text(reg, ev)
    head, bench = text.split(" · Bench: ")
    assert set(bench.split(", ")) == {raider.display_name, trial.display_name}, text
    assert set(head.split(" · not placed: ")[1].split(", ")) == names(joiners[1:]), text


# ---------------------------------------------------------------- the lock's words (task 6)

def _case_full(reg, rs):
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg) + people(reg, 4, DPS))
    join(reg, rs, ev, people(reg, 1, HEAL, "core") + people(reg, 1, DPS, "core"), "sub")
    return ev


def _case_healer(reg, rs):
    reg.set_minimums(RID, {"healer": {"min": 2}}, "t")
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, people(reg, 12, DPS))
    join(reg, rs, ev, people(reg, 1, HEAL), "sub")
    return ev


def _case_strong(reg, rs):
    sid = weekly(reg)
    reg.set_minimums(RID, {"Paladin": {"min": 1}}, "t")
    ev = rc.open_run(reg, rs, RID, first_slot(reg, RID), by="t", schedule=sid)
    join(reg, rs, ev, people(reg, 2, TANK) + people(reg, 2, HEAL) + people(reg, 7, DPS))
    join(reg, rs, ev, people(reg, 1, ("Paladin", "Retribution"), "core"), "sub")
    return ev


def _case_no_roster_added(reg, rs):
    ev = one_off(reg, rs)
    join(reg, rs, ev, base_eight(reg) + people(reg, 2, DPS))
    join(reg, rs, ev, people(reg, 4, TANK) + people(reg, 4, HEAL) + people(reg, 2, DPS), "sub")
    return ev


def _case_first_to_join(reg, rs):
    ev = one_off(reg, rs)
    join(reg, rs, ev, people(reg, 2, TANK) + people(reg, 2, HEAL) + people(reg, 3, DPS))
    join(reg, rs, ev, people(reg, 5, DPS), "sub")
    return ev


def _case_kept_off(reg, rs):
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg))
    off, other = people(reg, 2, DPS)
    join(reg, rs, ev, [off, other], "sub")
    ev.pins[str(off.discord_id)] = "out"
    return ev


def _case_placed_bench(reg, rs):
    """Ten joiners and a Bench answer placed by hand: the board's Bench answer is rostered, a joiner sits out."""
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg) + people(reg, 2, DPS))
    placed = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [placed] + people(reg, 1, DPS), "sub")
    ev.layout = [[], [placed.display_name]]
    return ev


def _case_spare_tank_heals(reg, rs):
    """The joiners together meet both minimums (a Protection Paladin who can heal): the benched Priest stays off, the
    benched Mage takes the one empty seat."""
    reg.set_minimums(RID, {"tank": {"min": 1}, "healer": {"min": 3}}, "t")
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, people(reg, 1, TANK) + people(reg, 1, ("Paladin", "Protection"), off="Holy") + people(reg, 2, HEAL)
         + people(reg, 5, DPS))
    join(reg, rs, ev, people(reg, 1, DPS) + people(reg, 1, HEAL), "sub")
    return ev


def _case_only_tank_heals(reg, rs):
    """The only tank is a Paladin who can heal and ten joiners fill the seats: the joiners together are a healer short,
    so a benched Priest takes a joiner's seat (the tank / healer exception)."""
    reg.set_minimums(RID, {"tank": {"min": 1}, "healer": {"min": 3}}, "t")
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, people(reg, 1, ("Paladin", "Protection"), off="Holy") + people(reg, 2, HEAL) + people(reg, 7, DPS))
    join(reg, rs, ev, people(reg, 1, HEAL), "sub")
    return ev


BOX_CASES = {"case 1": lambda reg, rs: case_one(reg, rs)[0], "case 2": _case_full, "case 3": _case_healer,
             "case 5": _case_strong, "case 7": _case_no_roster_added, "first to join": _case_first_to_join,
             "kept off": _case_kept_off, "placed by hand": _case_placed_bench, "spare tank heals": _case_spare_tank_heals,
             "only tank heals": _case_only_tank_heals}


# the review's cases (Task 6 fix): each one a box the old prediction contradicted
def _case_b1_full_hand_board_healer_short(reg, rs):
    """12 damage dealers joined, a healer minimum of 2, a healer on the Bench, and every seat placed by hand with
    joiners: the board is the roster, the benched healer stays off (the old box said 'one from the bench')."""
    ev = _case_healer(reg, rs)
    ns = sorted(s.display_name for s in ev.by_status("in"))
    ev.layout = [ns[:5], ns[5:10]]
    return ev


def _case_b6_pinned_bench_on_a_full_event(reg, rs):
    """Ten joined and a Bench answer pinned on: the lock rosters the pinned one and a joiner is not rostered."""
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg) + people(reg, 2, DPS))
    pinned = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [pinned] + people(reg, 1, DPS), "sub")
    ev.pins[str(pinned.discord_id)] = "in"
    return ev


def _case_b6b_pinned_bench_with_seats_left(reg, rs):
    """Case 1 with the trial Bench answer pinned on: the lock rosters it and one more Bench answer (two seats empty)."""
    ev, joiners, core, raider, trial = case_one(reg, rs)
    ev.pins[str(trial.discord_id)] = "in"
    return ev


def _case_b11_second_rosters_group(reg, rs):
    """Ten joiners placed in the first roster's groups and a Bench answer placed in a second roster's group: the lock
    builds two rosters, so the board is not the whole roster."""
    ev, joiners, core, raider, trial = case_one(reg, rs)
    extra = people(reg, 4, DPS)
    join(reg, rs, ev, extra)
    ns = sorted(names(joiners) | names(extra))
    k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    ev.layout = [ns[:5], ns[5:10]] + [[] for _ in range(k - 2)] + [[trial.display_name]]
    return ev


def _case_b12_auto_fill_then_two_joins(reg, rs):
    ev, *_ = _autofilled_case_one(reg, rs)
    join(reg, rs, ev, people(reg, 2, DPS))
    return rs.events[ev.key]


def _case_b13_auto_fill_then_one_join(reg, rs):
    ev, *_ = _autofilled_case_one(reg, rs)
    join(reg, rs, ev, people(reg, 1, DPS))
    return rs.events[ev.key]


def _night_bot(reg, rs):
    from test_raid_night import NightBot

    return NightBot(reg, rs)


def _case_a1_proposal_used_then_two_joins(reg, rs):
    """Use this on a proposal (a whole-board write) that put two Bench answers in empty seats, then two joins."""
    import asyncio

    ev, *_ = case_one(reg, rs)
    layout, _auto = rc.autofill(reg, rs, ev)
    asyncio.run(_night_bot(reg, rs).save_board(reg, rs, ev, layout, by="Jon"))
    join(reg, rs, ev, people(reg, 2, DPS))
    return rs.events[ev.key]


def _case_a1b_plain_text_board_then_two_joins(reg, rs):
    """Plain text's run_board with a Bench name, then two joins."""
    import asyncio

    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    ev, joiners, core, raider, trial = case_one(reg, rs)
    value = ", ".join(sorted(names(joiners))[:4] + [core.display_name])
    asyncio.run(configops.apply_async(reg, ConfigOp(op="run_board", target=ev.key, value=value), "Officer", True,
                                      bot=_night_bot(reg, rs), by_id=1, rev=rc.board_rev(reg, ev)))
    assert core.display_name in {n for g in rs.events[ev.key].layout for n in g}
    join(reg, rs, ev, people(reg, 2, DPS))
    return rs.events[ev.key]


# the second review's cases (fix round 2): a kept-on name the box's earlier rules could not see
def _case_p1_full_board_and_a_pinned_bench_answer_off_it(reg, rs):
    """8 joined, all placed on the board, plus a Bench answer pinned "in" who is NOT on the board: the lock rosters 9,
    so the board is not the whole roster."""
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg)
    join(reg, rs, ev, joiners)
    pinned = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [pinned], "sub")
    ev.pins[str(pinned.discord_id)] = "in"
    ns = sorted(names(joiners))
    ev.layout = [ns[:5], ns[5:]]
    return ev


def _case_b12_pinned_joiners_hold_every_seat(reg, rs):
    """12 damage dealers joined, 10 of them pinned "in", a healer minimum of 2, a healer on the Bench, an empty board:
    the pinned joiners hold every seat, so the benched healer cannot come in."""
    ev = _case_healer(reg, rs)
    for s in sorted(ev.by_status("in"), key=lambda s: s.display_name)[:10]:
        ev.pins[str(s.discord_id)] = "in"
    return ev


def _case_b13_one_pinned_bench_answer_empty_board(reg, rs):
    """8 joined, one Bench answer pinned "in", an empty board, no other Bench answer: the lock rosters the Bench answer."""
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg))
    pinned = people(reg, 1, DPS)[0]
    join(reg, rs, ev, [pinned], "sub")
    ev.pins[str(pinned.discord_id)] = "in"
    return ev


# the final review's cases
def _case_kept_on_hold_all_but_one_healer_short(reg, rs):
    """12 damage dealers joined, 9 of them pinned "in", a healer minimum of 2, a healer on the Bench: the kept-on names
    hold every seat but one, those who joined cannot meet the healer minimum, so the lock rosters the benched healer."""
    ev = _case_healer(reg, rs)
    for s in sorted(ev.by_status("in"), key=lambda s: s.display_name)[:9]:
        ev.pins[str(s.discord_id)] = "in"
    return ev


def _case_auto_fill_bench_then_pinned_two_joins(reg, rs):
    """Case 1 auto-filled (two Bench answers placed by Auto-fill), an officer then pins one of them in, two more join."""
    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    ev.pins[str(core.discord_id)] = "in"
    rs.save(ev, "pin (test)")
    join(reg, rs, ev, people(reg, 2, DPS))
    return rs.events[ev.key]


REVIEW_CASES = {"kept-on names hold every seat but one, a healer short": _case_kept_on_hold_all_but_one_healer_short,
                "a Bench answer Auto-fill placed and an officer then pinned in, two later joins": _case_auto_fill_bench_then_pinned_two_joins,
                "B1": _case_b1_full_hand_board_healer_short, "B6": _case_b6_pinned_bench_on_a_full_event,
                "B6b": _case_b6b_pinned_bench_with_seats_left, "B11": _case_b11_second_rosters_group,
                "B12": _case_b12_auto_fill_then_two_joins, "B13": _case_b13_auto_fill_then_one_join,
                "A1": _case_a1_proposal_used_then_two_joins, "A1b": _case_a1b_plain_text_board_then_two_joins,
                "P1": _case_p1_full_board_and_a_pinned_bench_answer_off_it,
                "B12 pinned joiners": _case_b12_pinned_joiners_hold_every_seat,
                "B13 pinned bench": _case_b13_one_pinned_bench_answer_empty_board}
ALL_CASES = {**BOX_CASES, **REVIEW_CASES}


def contradictions(reg, rs, ev) -> tuple[dict, list[str]]:
    """The lock box against the real lock (rc.propose): the head holds no number; the DM sentence counts only when the
    board is the whole roster, and then the count is the lock's; each rule sentence is true of the result."""
    import re

    from oibot_gm import consequences as cq

    b = fill_box(reg, ev, rs)
    head, told = b["sentences"][0], b["sentences"][1]
    lf = rc.lock_forces(reg, rs, ev)
    kept_bench = {n for n in lf.kept_on if (sg := rc.signup_by_name(ev, n)) is not None and sg.status == "sub"}
    joined = {s.display_name for s in ev.by_status("in")} - set(lf.kept_off)
    on_board = {n for g in (ev.layout or []) for n in g}
    rc.propose(reg, rs, ev, save=False)
    sel = [p for r in ev.all_rosters for p in r.selected]
    got = {p.signup_name for p in sel}
    bench_all = {p.signup_name for p in sel if p.status == "bench"}
    bench_in = bench_all - kept_bench
    # a Bench answer the head must name: every one rostered that the board does not already show (a kept-on one off the
    # board counts — a pin "in" or the policy's never_bench rosters them as surely as the solve does)
    unseen = bench_all - set(lf.placed)
    left = joined - got
    exception = head in {cq._short_first(r) for r in (("tank",), ("healer",), ("tank", "healer"))}
    bad = []
    if re.search(r"\d", head):
        bad.append(f"a number in the head: {head}")
    if (nums := [int(x) for x in re.findall(r"\b(\d+) rostered", told)]) and nums != [len(got)]:
        bad.append(f"the DM sentence counts {nums}, the lock rosters {len(got)}")
    if nums and not head.startswith("The board becomes the roster"):
        bad.append("a count while the board is not the whole roster")
    if head.startswith("The board becomes the roster") and got != set(lf.placed):
        bad.append(f"the board is not the roster: {sorted(got ^ set(lf.placed))}")
    if "keeps the board" in head and not set(lf.placed) <= got:
        bad.append("a board place was not kept")
    if head.startswith("Everyone on the board keeps their place") and not on_board <= got:
        bad.append("someone on the board lost their place")
    if "ahead of people who joined" in head and (not kept_bench <= got or not left):
        bad.append("no hand-placed Bench answer is rostered ahead of a joiner")
    if bench_in and left and not exception:
        bad.append(f"a Bench answer {sorted(bench_in)} is rostered while {sorted(left)} who joined is not")
    if exception and bench_in and left:
        roles = {p.role for p in sel if p.signup_name in bench_in}
        if not roles <= set(rc.EXTRA_ROLES):
            bad.append("a benched damage dealer came before a joiner")
    if exception and not any(p.role in rc.EXTRA_ROLES for p in sel if p.signup_name in bench_in):
        bad.append("the exception sentence, but the lock rosters no benched tank or healer")
    if "bench" not in head.lower() and unseen:
        bad.append(f"the head never names the bench, the lock rosters {sorted(unseen)}")
    return b, bad


@pytest.mark.parametrize("case", sorted(ALL_CASES))
def test_no_sentence_of_the_lock_box_is_contradicted_by_the_lock(reg, rs, case):
    ev = ALL_CASES[case](reg, rs)
    b, bad = contradictions(reg, rs, ev)
    assert not bad, (case, b["sentences"], bad)
    for s in b["sentences"]:
        assert len(s.split()) <= 20 and s.endswith("."), s


def test_the_box_says_the_board_becomes_the_roster_only_when_it_is(reg, rs):
    from oibot_gm import consequences as cq

    ev = _case_b1_full_hand_board_healer_short(reg, rs)
    b = fill_box(reg, ev, rs)
    assert b["sentences"][0] == "The board becomes the roster for Barrow Deeps.", b
    assert b["sentences"][1] == "The 10 rostered get the Confirm or Can't make it DM.", b
    ev = _case_b11_second_rosters_group(reg, rs)
    head = fill_box(reg, ev, rs)["sentences"][0]
    assert head == "The lock keeps the board and fills open slots from those who joined, then from the bench.", head


def test_the_box_names_auto_fills_bench_places_as_open(reg, rs):
    from oibot_gm import consequences as cq

    for make in (_case_b12_auto_fill_then_two_joins, _case_a1_proposal_used_then_two_joins):
        b = fill_box(reg, make(reg, rs), rs)
        assert b["sentences"][0] == cq.AUTO_AGAIN, b
        assert cq.AUTO_AGAIN == ("The lock decides again where each Bench answer goes, unless an officer dragged them there "
                                 "as a Bench answer.")
        assert b["sentences"][1] == "Everyone rostered gets the Confirm or Can't make it DM.", b


def test_the_box_says_a_hand_placed_or_pinned_bench_answer_comes_ahead(reg, rs):
    from oibot_gm import consequences as cq

    assert fill_box(reg, _case_b6_pinned_bench_on_a_full_event(reg, rs), rs)["sentences"][0] == \
        "A Bench answer an officer placed or pinned is rostered ahead of people who joined."
    assert fill_box(reg, _case_placed_bench(reg, rs), rs)["sentences"][0] == \
        "A Bench answer an officer placed or pinned is rostered ahead of people who joined."


def test_the_box_says_a_benched_healer_comes_ahead_for_the_healer_minimum(reg, rs):
    from oibot_gm import consequences as cq

    for make in (_case_healer, _case_only_tank_heals):
        head = fill_box(reg, make(reg, rs), rs)["sentences"][0]
        assert head == "If those who joined cannot meet the healer minimum, a healer on the bench is rostered ahead of them.", head
    assert rc.bench_short_roles(reg, rs, _case_spare_tank_heals(reg, rs)) == [], "the joiners together cover the minimums"


def test_the_box_names_the_bench_for_a_kept_on_bench_answer_off_the_board(reg, rs):
    """Fix round 2: P1 (a full board of joiners and a pinned Bench answer off it) is not 'the board becomes the roster';
    B13 (one pinned Bench answer, an empty board) names the bench; B12 (pinned joiners hold every seat) says no exception."""
    from oibot_gm import consequences as cq

    b = fill_box(reg, _case_p1_full_board_and_a_pinned_bench_answer_off_it(reg, rs), rs)
    assert b["sentences"][:2] == ["The lock keeps the board and fills open slots from those who joined, then from the bench.",
                                  "Everyone rostered gets the Confirm or Can't make it DM."], b
    for make in (_case_b13_one_pinned_bench_answer_empty_board, _case_b12_pinned_joiners_hold_every_seat):
        ev = make(reg, rs)
        assert fill_box(reg, ev, rs)["sentences"][0] == \
            "The lock builds the roster for Barrow Deeps from those who joined, then from the bench.", make.__name__
    assert rc.bench_short_roles(reg, rs, ev) == [], "the pinned joiners hold every seat"


def test_the_box_with_bench_answers_and_nobody_placed(reg, rs):
    from oibot_gm import consequences as cq

    ev, joiners, *_ = case_one(reg, rs)
    b = fill_box(reg, ev, rs)
    assert b["sentences"][:2] == ["The lock builds the roster for Barrow Deeps from those who joined, then from the bench.",
                                  "Everyone rostered gets the Confirm or Can't make it DM."], b
    ev.layout = [[joiners[0].display_name]]
    assert fill_box(reg, ev, rs)["sentences"][0] == \
        "The lock keeps the board and fills open slots from those who joined, then from the bench."


def test_a_full_roster_of_joiners_keeps_todays_lock_box(reg, rs):
    from oibot_gm import consequences as cq

    ev = _case_full(reg, rs)
    head = fill_box(reg, ev, rs)["sentences"][0]
    assert "bench" not in head.lower() or "then from the bench" in head, head


def test_the_box_fingerprint_changes_with_a_join_and_a_board_move(reg, rs):
    from oibot_gm import consequences as cq

    ev, joiners, *_ = case_one(reg, rs)
    a = fill_box(reg, ev, rs)
    join(reg, rs, ev, people(reg, 1, DPS))
    b = fill_box(reg, ev, rs)
    assert a["sentences"] == b["sentences"] and a["fp"] != b["fp"], "the answers changed, the sentences did not"
    ev.layout = [[joiners[0].display_name], [joiners[1].display_name]]
    c = fill_box(reg, ev, rs)
    ev.layout = [[joiners[1].display_name], [joiners[0].display_name]]
    assert fill_box(reg, ev, rs)["fp"] != c["fp"], "the board changed"


# ---------------------------------------------------------------- only an officer's own drag fixes a Bench place

def test_a_swap_leaves_the_displaced_bench_name_auto_placed(reg, rs):
    import asyncio

    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    bot = _night_bot(reg, rs)
    layout = rc.board_layout(reg, ev)
    gi = next(i for i, g in enumerate(layout) if core.display_name in g)
    pos = layout[gi].index(core.display_name)
    other = next(n for i, g in enumerate(layout) if i != gi for n in g if n in names(joiners))
    asyncio.run(bot.board_move(reg, rs, ev, other, (gi, pos), "Jon", by_id=1))
    ev = rs.events[ev.key]
    assert core.display_name in {n for g in ev.layout for n in g}, "the swap moved the Bench answer to another group"
    assert core.display_name in ev.auto_placed, "the officer dragged the joiner, not the Bench answer"
    assert other not in ev.auto_placed


def test_a_single_drag_of_a_bench_name_is_kept_at_the_lock(reg, rs):
    import asyncio

    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    bot = _night_bot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, core.display_name, (1, 0), "Jon", by_id=1))
    ev = rs.events[ev.key]
    assert core.display_name not in ev.auto_placed
    join(reg, rs, ev, people(reg, 2, DPS))
    ev = rs.events[ev.key]
    assert core.display_name in seated(reg, rs, ev), "the officer's own drag fixes the place"


def test_use_this_then_two_joins_then_the_lock_rosters_the_joiners(reg, rs):
    import asyncio

    ev, joiners, core, raider, trial = case_one(reg, rs)
    bot = _night_bot(reg, rs)
    layout, _auto = rc.autofill(reg, rs, ev)
    asyncio.run(bot.save_board(reg, rs, ev, layout, by="Jon"))
    ev = rs.events[ev.key]
    assert set(ev.auto_placed) == {core.display_name, raider.display_name}, "the whole-board write's Bench names"
    late = people(reg, 2, DPS)
    join(reg, rs, ev, late)
    asyncio.run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon"))
    ev = rs.events[ev.key]
    got = {p.signup_name for p in ev.seated()}
    assert names(late) | names(joiners) == got, sorted(got)


def test_plain_text_run_board_leaves_its_bench_name_decided_again(reg, rs):
    ev = _case_a1b_plain_text_board_then_two_joins(reg, rs)
    core = next(s.display_name for s in ev.by_status("sub") if s.display_name in {n for g in ev.layout for n in g})
    assert ev.auto_placed == [core]
    assert core not in seated(reg, rs, ev), "both later joiners come before the Bench answer"


def _placed_joiner_answers_bench(reg, rs, joined: int):
    """`joined` joined (base eight + damage dealers); an officer places the last one, who then answers Bench."""
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg) + people(reg, joined - 8, DPS)
    join(reg, rs, ev, joiners)
    who = joiners[-1]
    ev.layout = rc.apply_move(reg, ev, who.display_name, (1, 0))
    rs.save(ev, "board (test)")
    rc.set_signup(reg, rs, ev, who, None, "sub", source="test")
    ev = rs.events[ev.key]
    assert who.display_name in ev.layout[1], "the place stays on the board"
    assert who.display_name in ev.auto_placed, "their own answer says only if you need me: decided again at the lock"
    return ev, who


def test_a_placed_joiner_who_answers_bench_is_left_out_of_an_event_full_of_joiners(reg, rs):
    ev, who = _placed_joiner_answers_bench(reg, rs, 11)
    assert who.display_name not in seated(reg, rs, ev), "ten joiners fill the ten seats"


def test_a_placed_joiner_who_answers_bench_is_rostered_for_a_seat_left_empty(reg, rs):
    ev, who = _placed_joiner_answers_bench(reg, rs, 9)
    assert who.display_name in seated(reg, rs, ev)


def test_a_bench_answer_placed_by_hand_who_answers_join_stays_a_hand_place(reg, rs):
    import asyncio

    ev, joiners, core, raider, trial = case_one(reg, rs)
    asyncio.run(_night_bot(reg, rs).board_move(reg, rs, ev, core.display_name, (0, 0), "Jon", by_id=1))
    rc.set_signup(reg, rs, rs.events[ev.key], core, None, "in", source="test")
    ev = rs.events[ev.key]
    assert core.display_name in ev.layout[0] and core.display_name not in ev.auto_placed


def test_a_whole_board_write_keeps_a_dragged_bench_name_in_the_same_group_a_hand_place(reg, rs):
    import asyncio

    ev, joiners, core, raider, trial = case_one(reg, rs)
    bot = _night_bot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, core.display_name, (0, 0), "Jon", by_id=1))
    ev = rs.events[ev.key]
    assert core.display_name not in ev.auto_placed
    asyncio.run(bot.save_board(reg, rs, ev, [[core.display_name], [raider.display_name]], by="Jon"))
    ev = rs.events[ev.key]
    assert core.display_name not in ev.auto_placed, "the officer's drag stands: same group"
    assert raider.display_name in ev.auto_placed, "a Bench name new in the write is decided again"
    join(reg, rs, ev, people(reg, 2, DPS))
    assert core.display_name in seated(reg, rs, rs.events[ev.key]), "the hand place is kept at the lock"


def test_a_whole_board_write_that_moves_a_dragged_bench_name_makes_it_decided_again(reg, rs):
    import asyncio

    ev, joiners, core, raider, trial = case_one(reg, rs)
    bot = _night_bot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, core.display_name, (0, 0), "Jon", by_id=1))
    ev = rs.events[ev.key]
    asyncio.run(bot.save_board(reg, rs, ev, [[], [core.display_name]], by="Jon"))
    assert core.display_name in rs.events[ev.key].auto_placed, "moved by the write, not dragged: decided again"


def test_the_bench_receipt_says_when_the_lock_rosters_them(reg, rs):
    ev, joiners, core, raider, trial = case_one(reg, rs)
    text = rc.answer_receipt(reg, ev, core)
    assert "you may be rostered if a seat would otherwise stay empty" in text and "first asked when a seat opens later" in text
    assert "only if" not in text  # an officer may place a Bench answer by hand: never promise "only"
    assert "short a" not in text  # a damage dealer: the tank / healer exception isn't theirs
    healer = people(reg, 1, HEAL)[0]
    join(reg, rs, ev, [healer], "sub")
    assert "short a healer nobody who joined can cover" in rc.answer_receipt(reg, ev, healer)


def test_closest_to_a_seat_lists_a_joiner_before_a_bench_answer(reg, rs):
    ev = schedule_event(reg, rs)
    joiners = base_eight(reg) + people(reg, 3, DPS, "trial")  # 11 joined: one left out
    join(reg, rs, ev, joiners)
    star = people(reg, 1, DPS, "core")[0]
    join(reg, rs, ev, [star], "sub")
    rc.propose(reg, rs, ev, save=False)
    line = next(a for a in ev.all_rosters[0].advisories if "Closest to a seat" in a)
    out = [n.strip() for n in line.split("·", 1)[1].split(",")]
    assert out[-1] == star.display_name and len(out) == 2, line
    assert not any(ch.isdigit() for ch in line), line




# ---------------------------------------------------------------- a Bench name taken off the groups goes back to Bench

def test_clear_and_substitute_and_undo_say_a_bench_name_goes_back_to_bench(reg, rs):
    from oibot_gm import consequences as cq

    ev, joiners, core, raider, trial = case_one(reg, rs)
    ev.layout = [[core.display_name]]
    assert cq.consequence("clear", reg, ev)[0] == "The one Bench answer on the board goes back to Bench."
    ev.layout = [[core.display_name, raider.display_name]]
    assert cq.consequence("clear", reg, ev)[0] == "The 2 Bench answers on the board go back to Bench."
    ev.layout = [[core.display_name, joiners[0].display_name]]
    assert cq.consequence("clear", reg, ev)[0] == "The 2 people on the board go back to Not placed or Bench, by their answer."
    ev.layout = [[joiners[0].display_name]]
    assert cq.consequence("clear", reg, ev)[0] == "The one person on the board goes back to Not placed."
    sub = cq.consequence("substitute", reg, ev, name=joiners[1].display_name, other=core.display_name, to="Group 1")
    assert sub[1] == f"{core.display_name} goes back to Bench.", sub
    sub = cq.consequence("substitute", reg, ev, name=core.display_name, other=joiners[0].display_name, to="Group 1")
    assert sub[1] == f"{joiners[0].display_name} goes back to Not placed.", sub
    ev.placed_by = {core.display_name: 1}
    assert cq.undo_box_for(reg, ev, 1)["sentences"][1] == "That person goes back to Bench."
    ev.placed_by = {core.display_name: 1, joiners[0].display_name: 1}
    assert cq.undo_box_for(reg, ev, 1)["sentences"][1] == "Those people go back to Not placed or Bench, by their answer."
    ev.placed_by = {joiners[0].display_name: 1}
    assert cq.undo_box_for(reg, ev, 1)["sentences"][1] == "That person goes back to Not placed."


def test_raids_of_one_event_that_differ_get_the_rule_true_of_both(reg, rs):
    """Hyjal (20) then Barrow Deeps (10), 8 joined and 2 on the Bench: Hyjal's board empty, Barrow Deeps' an Auto-fill
    Bench place — the box says the rule true of both (the Auto-fill sentence), never a number."""
    from oibot_gm import consequences as cq

    unlock_hyjal(reg)
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": RID, "rosters": 1}])
    joiners, bench = base_eight(reg), people(reg, 2, DPS)
    join(reg, rs, lead, joiners)
    join(reg, rs, lead, bench, "sub")
    b = fill_box(reg, lead, rs)
    assert b["sentences"][0] == "The lock builds the roster for Hyjal Summit and Barrow Deeps from those who joined, then from the bench.", b
    fev = rs.events[rc.follow_of(rs, lead).key]
    fev.layout, fev.auto_placed = [[bench[0].display_name]], [bench[0].display_name]
    b = fill_box(reg, lead, rs)
    assert b["sentences"][0] == cq.AUTO_AGAIN, b
    fev.layout, fev.auto_placed = [[joiners[0].display_name]], []
    b = fill_box(reg, lead, rs)
    assert b["sentences"][0] == "The lock fills open slots from those who joined first, then from the bench.", b
    for s in b["sentences"]:
        assert len(s.split()) <= 20 and not any(ch.isdigit() for ch in s.split(" until ")[0]), s


# ---------------------------------------------------------------- the final review: Join → Bench → Join keeps a drag

def test_a_dragged_joiner_who_answers_bench_then_join_keeps_the_officers_place(reg, rs):
    """An officer drags a joiner into a group on an event with more joiners than seats; they answer Bench, then Join
    again: the drag is theirs again (not in auto_placed), so the next Auto-fill keeps it and the lock rosters them."""
    import asyncio

    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg) + people(reg, 2, DPS))
    late = people(reg, 1, DPS, "trial")[0]
    join(reg, rs, ev, [late])  # 11 joined, 10 seats
    bot = _night_bot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, late.display_name, (1, 0), "Jon", by_id=1))
    assert late.display_name in rs.events[ev.key].placed_by
    rc.set_signup(reg, rs, rs.events[ev.key], late, None, "sub", source="test")
    assert late.display_name in rs.events[ev.key].auto_placed, "their Bench answer: decided again at the lock"
    rc.set_signup(reg, rs, rs.events[ev.key], late, None, "in", source="test")
    ev = rs.events[ev.key]
    assert late.display_name not in ev.auto_placed, "joined again: the officer's drag stands"
    layout, auto = rc.autofill(reg, rs, ev)
    assert late.display_name in layout[1] and late.display_name not in auto
    ev.layout, ev.auto_placed = layout, sorted(auto)
    rs.save(ev, "auto-fill (test)")
    asyncio.run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon"))
    assert late.display_name in {p.signup_name for p in rs.events[ev.key].seated()}


def test_a_later_raids_dragged_joiner_who_answers_bench_then_join_keeps_the_place(reg, rs):
    """The same on a later raid of the evening: the answers reach it through sync_parts."""
    import asyncio

    unlock_hyjal(reg)
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": RID, "rosters": 1}])
    join(reg, rs, lead, base_eight(reg) + people(reg, 2, DPS))
    late = people(reg, 1, DPS, "trial")[0]
    join(reg, rs, lead, [late])
    fev = rs.events[rc.follow_of(rs, lead).key]
    asyncio.run(_night_bot(reg, rs).board_move(reg, rs, fev, late.display_name, (1, 0), "Jon", by_id=1))
    assert late.display_name in rs.events[fev.key].placed_by
    rc.set_signup(reg, rs, rs.events[lead.key], late, None, "sub", source="test")
    assert late.display_name in rs.events[fev.key].auto_placed
    rc.set_signup(reg, rs, rs.events[lead.key], late, None, "in", source="test")
    fev = rs.events[fev.key]
    assert late.display_name not in fev.auto_placed
    layout, auto = rc.autofill(reg, rs, fev, earlier=rc.earlier_counts(reg, rs, fev))
    assert late.display_name in layout[1] and late.display_name not in auto


def test_plain_text_run_board_without_the_bot_marks_its_bench_names_as_the_bot_does(reg, rs):
    """configops.apply's run_board (no bot running) records the written board's Bench names as auto-placed exactly as
    discord_raid.save_board does: a Bench name an officer dragged keeps its place when the write leaves it in that group."""
    import asyncio

    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    ev, joiners, core, raider, trial = case_one(reg, rs)
    asyncio.run(_night_bot(reg, rs).board_move(reg, rs, ev, core.display_name, (0, 0), "Jon", by_id=1))
    value = ", ".join([core.display_name, raider.display_name] + sorted(names(joiners))[:3])
    configops.apply(reg, ConfigOp(op="run_board", target=ev.key, value=value), "Officer", True, raids=rs, by_id=1)
    ev = rs.events[ev.key]
    assert core.display_name in ev.layout[0] and raider.display_name in ev.layout[0]
    assert ev.auto_placed == [raider.display_name], ev.auto_placed


def test_the_final_reviews_two_boxes_say_what_the_lock_does(reg, rs):
    from oibot_gm import consequences as cq

    ev = _case_kept_on_hold_all_but_one_healer_short(reg, rs)
    assert fill_box(reg, ev, rs)["sentences"][0] == cq._short_first(("healer",))
    healer = next(s.display_name for s in ev.by_status("sub"))
    assert healer in seated(reg, rs, ev), "the lock rosters the benched healer"
    ev = _case_auto_fill_bench_then_pinned_two_joins(reg, rs)
    assert fill_box(reg, ev, rs)["sentences"][0] == \
        "A Bench answer an officer placed or pinned is rostered ahead of people who joined."
    pinned = next(s.display_name for s in ev.by_status("sub") if ev.pins.get(str(s.discord_id)) == "in")
    assert pinned in seated(reg, rs, ev), "the lock agrees"
