"""A run opened closer than its cadence assumes still gets a usable signup window."""
from oibot_gm import raidcycle as rc
from conftest import open_test_run


def test_cadence_is_untouched_when_it_fits():
    # 120 h out, cadence nudge 72 / lock 24 / confirm 6: every step already fits
    assert rc.fit_cutoffs(120, 72, 24, 6) == (72, 24, 6)


def test_a_pickup_two_hours_out_is_squeezed_in_order():
    nudge, lock, confirm = rc.fit_cutoffs(2, 72, 24, 6)
    assert nudge == 1.2 and lock == 0.6 and confirm == 0.2
    assert nudge > lock > confirm > 0  # the order the scheduler walks, all still ahead of the start


def test_a_run_starting_now_or_in_the_past_locks_at_once():
    assert rc.fit_cutoffs(0, 72, 24, 6) == (0.0, 0.0, 0.0)
    assert rc.fit_cutoffs(-5, 72, 24, 6) == (0.0, 0.0, 0.0)


def test_ensure_run_puts_every_cutoff_ahead_of_now_for_a_pickup(reg, rs):
    from datetime import timedelta

    from oibot_gm.raid_views import run_times

    now = reg.now_local()
    start = now + timedelta(hours=2)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    soft, hard, confirm = run_times(reg, ev, rc.run_team(reg, ev))
    assert now < soft < hard < confirm < start, (soft, hard, confirm)


def test_a_test_run_keeps_its_own_compressed_cutoffs(reg, rs):
    ev = open_test_run(reg, rs, days=0.05)  # ~72 minutes out, cutoffs given explicitly
    t = rc.run_team(reg, ev)
    assert t["cutoff_hard_hours"] == 1.5 and t["confirm_hours"] == 1
