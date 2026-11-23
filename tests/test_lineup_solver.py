"""A run of three raids, solved for real (slow: CP-SAT): a 20 → 10 ×2 → 10 chain. Each later raid prefers the people
rostered on the raids before it (never charging them as an extra tank or healer), and its solve is repeatable — the
lock preview shows what the lock builds."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from conftest import join
from test_lineups import BD, H, reg, start_in  # noqa: F401 — the profile copy with extra_a / extra_b

from oibot_gm import raidcycle as rc

pytestmark = pytest.mark.slow

CHAIN = [{"raid": BD, "rosters": 2}, {"raid": "extra_a", "rosters": 1}]


def lock(reg, rs, ev) -> None:
    rc.propose(reg, rs, ev)
    ev.state = "locked"
    rs.save(ev, "locked (test)")


def chain(reg, rs):
    reg.seed_test_members(24, "t")
    first = rc.open_run(reg, rs, H, start_in(reg, 3), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=CHAIN)
    join(reg, rs, first, reg.test_members())
    lock(reg, rs, first)
    p1 = rc.event_parts(rs, first)[1]
    lock(reg, rs, p1)
    return first, p1, rc.event_parts(rs, first)[2]


def slot(role: str) -> str:
    return "dps" if role in ("melee", "ranged") else role


def test_a_later_raid_prefers_the_earlier_rostered(reg, rs):
    first, p1, p2 = chain(reg, rs)
    on_first = {p.signup_name for p in first.seated()}
    assert len(on_first) == 20
    rc.propose(reg, rs, p2)
    on_p2 = {p.signup_name for p in p2.seated()}
    n = len(p2.all_rosters)  # 24 joined a 10: the joiners don't fit one, so the declared roster gets a second (as any run)
    assert len(on_p2) == 10 * n
    role = {p.signup_name: p.role for p in rc.players_for(reg, p2)}
    rb = reg.role_bounds("extra_a", rc.run_size(reg, p2))
    have = {r: sum(1 for x in on_first if slot(role[x]) == r) for r in rb}
    short = {r: n * int(rb[r].get("min") or 0) - have[r] for r in rb if n * int(rb[r].get("min") or 0) > have[r]}
    extra = on_p2 - on_first
    assert all(slot(role[x]) in short for x in extra), "only a role the first raid's 20 can't cover brings anyone else in"
    assert len(extra) <= sum(short.values()), "and only as many as that role is short"


def test_a_later_raid_gives_its_bonus_to_everyone_rostered_before_it(reg, rs, monkeypatch):
    """What the third raid hands the solver: the people rostered on either earlier raid carry rc.LEAD_SEAT_BONUS (once per
    earlier raid they played) and are never charged as extra; nobody else gets it."""
    from oibot_gm.roster import solver

    first, p1, p2 = chain(reg, rs)
    seen = {}

    class Seen(Exception):
        pass

    def capture(profile, players, raid_id, opts, **kw):
        seen["opts"] = opts
        raise Seen

    monkeypatch.setattr(solver, "solve_rosters", capture)
    with pytest.raises(Seen):
        rc.propose(reg, rs, p2, save=False)
    o = seen["opts"]
    a, b = {p.signup_name for p in first.seated()}, {p.signup_name for p in p1.seated()}
    assert set(o.lead_rostered) == a | b
    for n in a | b:
        assert o.bonus.get(n, 0) >= rc.LEAD_SEAT_BONUS * ((n in a) + (n in b))
    assert all(o.bonus.get(m.display_name, 0) < rc.LEAD_SEAT_BONUS for m in reg.test_members() if m.display_name not in a | b)


def test_the_third_raid_solves_the_same_every_time_and_the_preview_shows_it(reg, rs):
    from oibot_gm.discord_raid import RaidMixin

    first, p1, p2 = chain(reg, rs)
    seen = [[[sorted(g) for g in r.groups] for r in rc._solve_run(reg, rs, p2, time_limit=2.0, whatif=False)[1]] for _ in range(3)]
    assert seen[0] == seen[1] == seen[2]

    class Bare:
        lock_preview = RaidMixin.lock_preview

        @asynccontextmanager
        async def solving(self, reg, key):
            yield

    def shape(rosters):
        return [([sorted(g) for g in r.groups], sorted((p.signup_name, p.role) for p in r.selected)) for r in rosters]

    shown = asyncio.run(Bare().lock_preview(reg, rs, p2, by="t"))
    rc.propose(reg, rs, p2, save=False)
    assert shape(shown) == shape(p2.all_rosters)
