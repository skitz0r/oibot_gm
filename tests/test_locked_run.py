"""After lock: confirmations expiring and the officers' board (substitution vs removal)."""
from datetime import timedelta

from conftest import by_role, join, lock_with_board, open_test_run

from oibot_gm import raidcycle as rc


def _locked_ten(reg, rs):
    """Everyone joins a 10-man; the first 2 tanks, 3 healers and 5 dps are seated by the board, the rest are bench."""
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    assert len(ev.roster.selected) == 10 and len(ev.roster.benched) == len(pool) - 10
    return ev, seated


def test_expire_confirmations_frees_unanswered_seats(reg, rs):
    ev, seated = _locked_ten(reg, rs)
    for m in seated:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    for m in seated[:2]:
        reg.answer_placement(m.discord_id, ev.team, True, m.display_name)
    assert rc.expire_confirmations(reg, rs, ev) == [], "before the deadline nothing expires"
    ev.confirm_by = (rc.datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()
    gone = rc.expire_confirmations(reg, rs, ev)
    assert sorted(gone) == sorted(m.display_name for m in seated[2:])
    assert [p.signup_name for p in ev.roster.selected] == [m.display_name for m in seated[:2]]
    for m in seated[2:]:
        assert ev.signups[str(m.discord_id)].status == "out" and ev.signups[str(m.discord_id)].source == "no-confirm"
        assert reg.members[m.discord_id].placement_asks[-1]["answer"] == "expired"
    for m in seated[:2]:
        assert reg.members[m.discord_id].placement_asks[-1]["answer"] == "yes"
    assert all(m.display_name not in g for g in ev.roster.groups for m in seated[2:])
    assert rc.expire_confirmations(reg, rs, ev) == []


def test_apply_layout_locked_substitution_and_removal(reg, rs):
    ev, seated = _locked_ten(reg, rs)
    bench = [p.signup_name for p in ev.roster.benched]
    dps_out = seated[-1].display_name
    sub_in = next(n for n in bench if ev.signups[str(next(m.discord_id for m in reg.test_members() if m.display_name == n))].role in ("melee", "ranged"))
    layout = [[sub_in if n == dps_out else n for n in g] for g in ev.roster.groups]
    added, removed = rc.apply_layout_locked(reg, rs, ev, layout)
    assert [s.display_name for s in added] == [sub_in] and removed == [dps_out]
    assert added[0].status == "in" and added[0].source == "officer"
    out_sg = next(s for s in ev.signups.values() if s.display_name == dps_out)
    assert out_sg.status == "out" and out_sg.source == "moved to bench"
    assert ev.seat_of(sub_in) and not ev.seat_of(dps_out) and len(ev.roster.selected) == 10
    assert dps_out not in [p.signup_name for p in ev.roster.benched], "out of the run, not waiting on the bench"
    # a plain removal frees the seat and adds nobody
    layout2 = [[n for n in g if n != sub_in] for g in ev.roster.groups]
    added2, removed2 = rc.apply_layout_locked(reg, rs, ev, layout2)
    assert added2 == [] and removed2 == [sub_in] and len(ev.roster.selected) == 9
    # moving people between groups is free
    g = [list(x) for x in ev.roster.groups]
    g[0], g[1] = g[1], g[0]
    assert rc.apply_layout_locked(reg, rs, ev, g) == ([], [])
    assert ev.roster.groups == g
