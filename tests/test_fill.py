"""Fill engine after lock: candidate tiers, the tied swap + backfill pair, release on a no and on timeout, reseat in place."""
from datetime import datetime, timedelta

from conftest import by_role, join, lock_with_board, open_test_run

from oibot_gm import raidcycle as rc
from oibot_gm.registry import Absence


def _absent(reg, m, ev):
    day = ev.start.date().isoformat()
    m.absences = [Absence(start=day, end=day, reason="t", by="t")]
    reg.save(m, "absent")


def _present(reg, m):
    m.absences = []
    reg.save(m, "back")


def _setup(reg, rs):
    """24 puppets (4 tanks, 5 healers). Seated: 2 tanks + healers at the minimum + dps; Bench: a healer and a dps;
    one tank and one healer never answer. One seated healer gets a tank alt so a swap is possible."""
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    team = reg.config.roster(ev.team)
    roles = by_role(reg)
    tanks, healers, dps = roles["tank"], roles["healer"], roles["melee"] + roles["ranged"]
    assert len(tanks) >= 3 and len(healers) >= 5
    hmin = reg.role_bounds("barrow_deeps", 10)["healer"]["min"]
    seated = tanks[:2] + healers[:hmin] + dps[:10 - 2 - hmin]
    join(reg, rs, ev, seated)
    bench_healer, silent_healer = healers[hmin], healers[hmin + 1]
    join(reg, rs, ev, [bench_healer, dps[-1]], "sub")
    swapper = healers[0]
    reg.add_character(swapper.discord_id, swapper.display_name, "Swaptank", "Warrior", "Protection", None, False)
    swapper = reg.members[swapper.discord_id]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    assert len(ev.roster.selected) == 10
    return ev, team, dict(tanks=tanks, healers=healers, dps=dps, swapper=swapper, bench_healer=bench_healer, silent_healer=silent_healer, silent_tank=tanks[2])


def test_fill_pool_tank_before_swap(reg, rs):
    ev, team, s = _setup(reg, rs)
    seated_tank = next(p for p in ev.roster.selected if p.role == "tank")
    rc.free_seat(reg, rs, ev, seated_tank.signup_name, "declined")
    nd = rc.needs(reg, ev, team)
    assert nd["roles"] == {"tank": 1} and nd["headcount"] == 1
    assert ev.signups[str(next(m.discord_id for m in s["tanks"] if m.display_name == seated_tank.signup_name))].status == "out"
    cands = rc.fill_candidates(reg, rs, ev, team)
    assert cands[0].kind == "pool" and cands[0].role == "tank" and cands[0].discord_id == s["silent_tank"].discord_id and cands[0].pair is None
    assert all(not a.swap for a in cands), "a plain pool tank exists, so no swap is proposed"


def test_fill_tied_swap_and_backfill(reg, rs):
    ev, team, s = _setup(reg, rs)
    seated_tank = next(p for p in ev.roster.selected if p.role == "tank")
    rc.free_seat(reg, rs, ev, seated_tank.signup_name, "declined")
    for m in s["tanks"][2:]:  # every spare tank away → a swap is the only way to a second tank
        _absent(reg, m, ev)
    assert rc.would_short(reg, ev, team, "healer"), "healers sit at the minimum: losing one needs a backfill"
    cands = rc.fill_candidates(reg, rs, ev, team)
    swap = next((a for a in cands if a.swap), None)
    assert swap and swap.discord_id == s["swapper"].discord_id and swap.vacates == "healer" and swap.kind == "alt" and swap.character == "Swaptank"
    back = next(a for a in cands if a.pair == swap.discord_id and not a.swap)
    assert swap.pair == back.discord_id and back.role == "healer" and back.reason.startswith("covering")
    assert back.kind == "sub" and back.discord_id == s["bench_healer"].discord_id, "the Bench healer covers first"
    # nobody can cover the healer seat → the swap is withheld
    spare = [m for m in s["healers"] if not ev.seat_of(m.display_name)]
    for m in spare:
        _absent(reg, m, ev)
    assert not any(a.swap for a in rc.fill_candidates(reg, rs, ev, team))
    for m in spare:
        _present(reg, m)
    # the batch: deadlines never past the start; a no on the backfill releases the swap
    batch = rc.fill_batch(reg, rs, ev, team)
    deadline = rc.ask_deadline(reg, ev).isoformat()
    assert datetime.fromisoformat(deadline) <= ev.start
    for a in batch:
        a.expires_at = deadline
        ev.fill_asks.append(a)
    swap = next(a for a in ev.fill_asks if a.swap and a.open)
    back = rc.partner_of(ev, swap)
    assert back is not None
    rc.apply_fill_answer(reg, rs, ev, back, False)
    rel = rc.release_partner(reg, rs, ev, back)
    assert rel is swap and swap.answer == "released"
    # timeout on the swap releases the backfill
    ev.fill_asks.clear()
    for a in rc.fill_batch(reg, rs, ev, team):
        a.expires_at = (datetime.now(ev.start.tzinfo) + timedelta(hours=1)).isoformat()
        ev.fill_asks.append(a)
    swap = next(a for a in ev.fill_asks if a.swap)
    back = rc.partner_of(ev, swap)
    swap.expires_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()
    expired, released = rc.expire_fill_asks(reg, rs, ev)
    assert expired == [swap] and released == [back]
    assert rc.expire_fill_asks(reg, rs, ev) == ([], []), "nothing open is past its deadline now"
    # yes + yes: the swapper is re-seated in place as the tank alt, the backfill takes the healer seat
    ev.fill_asks.clear()
    for a in rc.fill_batch(reg, rs, ev, team):
        a.expires_at = deadline
        ev.fill_asks.append(a)
    swap = next(a for a in ev.fill_asks if a.swap)
    back = rc.partner_of(ev, swap)
    rc.apply_fill_answer(reg, rs, ev, swap, True)
    rc.apply_fill_answer(reg, rs, ev, back, True)
    names = [p.signup_name for p in ev.roster.selected]
    assert names.count(s["swapper"].display_name) == 1
    seat = ev.seat_of(s["swapper"].display_name)[1]
    assert seat.role == "tank" and seat.character == "Swaptank"
    assert len(ev.roster.selected) == 10 and not rc.needs(reg, ev, team)["roles"]
