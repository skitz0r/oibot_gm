"""The same signups give the same roster (slow: CP-SAT). Preview lock and Lock solve the same input; when the solve
stops at its limit before proving the best roster, the roster it stops on must not depend on the machine's speed or
on which search thread got there first (the final review of §5.67 phase 2: a follow-on split 8/10, 9/9, 10/8)."""
import pytest
from conftest import join
from test_follow_on import open_pair  # noqa: F401

from oibot_gm import raidcycle as rc
from oibot_gm.roster import solver

pytestmark = pytest.mark.slow


def placement(rosters) -> list[list[list[str]]]:
    return [[sorted(g) for g in r.groups] for r in rosters]


def test_the_same_signups_give_the_same_rosters_every_time(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    join(reg, rs, lead, reg.test_members())
    rc.propose(reg, rs, lead)
    lead.state = "locked"
    rs.save(lead, "locked (test)")
    fev = rs.events[fev.key]
    for ev in (lead, fev):  # a 20 with five groups, and its follow-on split into two 10s
        seen = []
        for _ in range(3):  # the search itself, each time (nothing kept: solver.forget_answers)
            solver.forget_answers()
            seen.append(placement(rc._solve_run(reg, rs, ev, time_limit=2.0, whatif=False)[1]))
        assert seen[0] == seen[1] == seen[2], f"{ev.key}: {[[len(sum(r, [])) for r in s] for s in seen]}"


def test_the_lock_preview_shows_the_roster_the_lock_builds(reg, rs):
    """Preview lock (RaidMixin.lock_preview, the Discord card's and the site's) solves with the lock's strategy and the
    lock's budget, so on the same signups it shows exactly what the lock then rosters: who, in which roster and group,
    in which role (an offspec switch included)."""
    import asyncio
    from contextlib import asynccontextmanager

    from oibot_gm.discord_raid import RaidMixin

    class Bare:
        lock_preview = RaidMixin.lock_preview

        @asynccontextmanager
        async def solving(self, reg, key):
            yield

    def shape(rosters):
        return [([sorted(g) for g in r.groups], sorted((p.signup_name, p.role) for p in r.selected)) for r in rosters]

    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    join(reg, rs, lead, reg.test_members())
    rc.propose(reg, rs, lead)
    lead.state = "locked"
    rs.save(lead, "locked (test)")
    fev = rs.events[fev.key]
    reg.config.team(fev.team)["split_policy"] = "first"  # the roster's own strategy, which the lock reads before the raid's
    shown = asyncio.run(Bare().lock_preview(reg, rs, fev, by="t"))
    assert fev.last_proposal["strategy"] == "first"
    rc.propose(reg, rs, fev, save=False)
    assert shape(shown) == shape(fev.all_rosters)
