"""The follow-on's solve (slow: CP-SAT): the two 10s are the lead's 20 split in two whenever the roles allow, bench
joiners cover the rest; the roles cap the rosters."""
import pytest
from conftest import by_role, join, lock_with_board
from test_follow_on import FOLLOW, LEAD, open_pair  # noqa: F401

from oibot_gm import raidcycle as rc

pytestmark = pytest.mark.slow


def test_the_splits_are_the_leads_roster(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    rc.propose(reg, rs, lead)
    lead.state = "locked"
    rs.save(lead, "locked (test)")
    on_lead = {p.signup_name for r in lead.all_rosters for p in r.selected}
    assert len(on_lead) == 20
    fev = rs.events[fev.key]
    rc.propose(reg, rs, fev)
    on_follow = {p.signup_name for r in fev.all_rosters for p in r.selected}
    assert len(fev.all_rosters) == 2
    # the roles decide how much of the 20 fits: two 10s need each role's minimum twice; a role the 20 hold too few of is
    # covered by bench joiners (in this seed the lead holds two tanks, two 10s need four) — nobody else displaces the 20
    role = {p.signup_name: p.role for p in rc.players_for(reg, fev)}
    rb = reg.role_bounds(FOLLOW, rc.run_size(reg, fev))
    have = {r: sum(1 for n in on_lead if (role[n] in ("melee", "ranged") and r == "dps") or role[n] == r) for r in rb}
    short = {r: 2 * int(rb[r].get("min") or 0) - have[r] for r in rb if 2 * int(rb[r].get("min") or 0) > have[r]}
    extra = on_follow - on_lead
    assert len(extra) == sum(short.values()) == len(on_lead - on_follow), "bench joiners only cover gaps"
    assert all((("dps" if role[n] in ("melee", "ranged") else role[n]) in short) for n in extra)


def test_follow_on_caps_rosters_by_roles(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    tanks = by_role(reg)["tank"]
    join(reg, rs, lead, [m for m in ms if m not in tanks[3:]])  # three tanks: two 10s need four
    rc.propose(reg, rs, lead)
    lead.state = "locked"
    rs.save(lead, "locked (test)")
    fev = rs.events[fev.key]
    rc.propose(reg, rs, fev)
    assert len(fev.all_rosters) == 1


def test_the_leads_board_beats_first_come_on_the_follow_on(reg, rs):
    """The lead's officers placed the LAST 20 joiners: first come alone would put the first ones on the follow-on."""
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    lock_with_board(reg, rs, lead, [m.display_name for m in ms[4:]])
    on_lead = {p.signup_name for p in lead.seated()}
    fev = rs.events[fev.key]
    rc.propose(reg, rs, fev)
    on_follow = {p.signup_name for p in fev.seated()}
    first = {m.display_name for m in ms[:4]}
    role = {p.signup_name: p.role for p in rc.players_for(reg, fev)}
    rb = reg.role_bounds(FOLLOW, rc.run_size(reg, fev))
    slot = lambda n: "dps" if role[n] in ("melee", "ranged") else role[n]  # noqa: E731
    short = {r: 2 * int(rb[r].get("min") or 0) - sum(1 for n in on_lead if slot(n) == r) for r in rb}
    extra = on_follow - on_lead
    assert extra <= first and len(extra) == sum(v for v in short.values() if v > 0), "only a role the 20 can't fill twice brings an early joiner in"
