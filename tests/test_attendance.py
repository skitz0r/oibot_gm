"""Attendance from run history (design §3.5 L35/M43, owner decision D49; dual interfaces phase 3, G4).

Registry.attendance(member, window_days) → (attended, total) from DONE runs: attended = rostered when the run ended and,
on a recurring run that asks for confirmations, confirmed; on ad hoc, rostered. total = the runs the member was in the
pool for (a regular of the run, or they joined / held a seat / called out) and not away. Test runs and runs nobody held a
seat on are left out; the bench counts for the attendance VIEW (the Me card, /me view), never for the loot factor. A
member's characters roll up by Discord id. Unknown (fewer than ATTENDANCE_MIN_RUNS real runs in the window, or none for
the member) → neutral (1.0) and `attendance_known: False` in loot scoring. Registry and RaidStore are real (scratch
GitStore)."""
from __future__ import annotations

from datetime import date, timedelta

from conftest import join, lock_with_board

from oibot_gm import raidcycle as rc
from oibot_gm.loot import scoring
from oibot_gm.registry import ADHOC_SCHEDULE, ATTENDANCE_MIN_RUNS, Registry


def done_run(reg: Registry, rs: rc.RaidStore, joined, seated, days_ago: float, *, adhoc: bool = False, regulars=(), confirm=None,
             test: bool = False, n: int = 0):
    """A run `days_ago` days back, joined by `joined`, locked around `seated`, then ended (state done). `confirm`:
    {member: answer} placement asks on the run's roster key (recurring); `regulars`: who the series expected at open."""
    start = (reg.now_local() + timedelta(days=20 + n, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t", schedule=ADHOC_SCHEDULE if adhoc else None)
    join(reg, rs, ev, joined)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    ev.regulars = [m.discord_id for m in regulars]
    ev.test = test
    for m, answer in (confirm or []):
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, by="t")
        ask = reg.members[m.discord_id].placement_asks[-1]
        ask["answer"] = answer
        reg.save(reg.members[m.discord_id], "t: answer")
    ev.starts_at = (reg.now_local() - timedelta(days=days_ago)).replace(second=0, microsecond=0).isoformat()
    rc.end_run(reg, rs, ev, "t")
    return ev


def three_runs(reg, rs, people, **kw):
    """ATTENDANCE_MIN_RUNS real runs in the window (so attendance is known), everyone seated and confirmed."""
    for i in range(ATTENDANCE_MIN_RUNS):
        done_run(reg, rs, people, people, 3 + i, confirm=[(m, "yes") for m in people], n=i, **kw)


def test_recurring_counts_rostered_and_confirmed_only(reg, rs):
    a, b, c, d = reg.test_members()[:4]
    done_run(reg, rs, [a, b, c, d], [a, b, c], 5, regulars=[a, b, c, d], confirm=[(a, "yes"), (b, None), (c, "yes")])
    assert reg.attendance(a) == (1, 1), "rostered + confirmed"
    assert reg.attendance(b) == (0, 1), "rostered but never confirmed: in the total, not attended (D49)"
    assert reg.attendance(d) == (0, 1), "a regular who joined and sat on the bench: counted, not attended for loot"
    assert reg.attendance(d, bench=True) == (1, 1), "…but the attendance view counts the bench"
    rows = {r["outcome"] for r in reg.attendance_runs(b)}
    assert rows == {"unconfirmed"}


def test_a_seat_that_came_after_the_lock_counts_without_a_placement_ask(reg, rs):
    a, b = reg.test_members()[:2]
    done_run(reg, rs, [a, b], [a, b], 4, confirm=[(a, "yes")])  # b's seat came later (a fill yes): no placement ask
    assert reg.attendance(b) == (1, 1)


def test_ad_hoc_counts_rostered_and_only_for_those_who_joined(reg, rs):
    a, b, c = reg.test_members()[:3]
    ev = done_run(reg, rs, [a, b], [a], 4, adhoc=True)
    assert rc.run_kind(reg, ev) == "adhoc"
    assert reg.attendance(a) == (1, 1), "ad hoc: rostered is attended, no confirmation needed"
    assert reg.attendance(b) == (0, 1) and reg.attendance(b, bench=True) == (1, 1)
    assert reg.attendance(c) == (0, 0), "never joined the pickup: it was never theirs to miss"


def test_away_runs_are_shown_and_never_counted(reg, rs):
    a, b = reg.test_members()[:2]
    ev = done_run(reg, rs, [a], [a], 6, regulars=[a, b], confirm=[(a, "yes")])
    reg.add_absence(b.discord_id, ev.start.date().isoformat(), None, "trip", by="t")
    assert reg.attendance(b) == (0, 0)
    assert [r["outcome"] for r in reg.attendance_runs(b)] == ["away"]
    other = done_run(reg, rs, [a], [a], 9, regulars=[a, b], confirm=[(a, "yes")], n=1)
    assert reg.attendance(b) == (0, 1), "a regular who didn't answer and wasn't away: counted, missed"
    assert {r["key"]: r["outcome"] for r in reg.attendance_runs(b)}[other.key] == "no_answer"


def test_test_runs_and_runs_without_seats_are_left_out(reg, rs):
    a = reg.test_members()[0]
    done_run(reg, rs, [a], [a], 3, test=True, confirm=[(a, "yes")])
    assert reg.attendance(a) == (0, 0) and reg.attendance_tracked()["runs"] == 0
    empty = done_run(reg, rs, [a], [], 4, n=1)
    assert not empty.seated() and reg.attendance(a) == (0, 0), "a run nobody held a seat on had no activity"


def test_the_window(reg, rs):
    a = reg.test_members()[0]
    done_run(reg, rs, [a], [a], 5, confirm=[(a, "yes")])
    done_run(reg, rs, [a], [a], 45, confirm=[(a, "yes")], n=1)
    assert reg.attendance_window() == 30, "the loot policy's window (loot_defaults attendance.window_days)"
    assert reg.attendance(a) == (1, 1), "the run 45 days back is outside 30 days"
    assert reg.attendance(a, 60) == (2, 2)


def test_rollup_across_a_members_characters(reg, rs):
    a, b = reg.test_members()[:2]
    alt = next(c for c in reg.profile.classes if c != a.main.cls)
    spec = next(iter(reg.profile.classes[alt]))
    reg.add_character(a.discord_id, a.display_name, "Altchar", alt, spec, None, False)
    ev = done_run(reg, rs, [a, b], [a, b], 4, confirm=[(a, "yes"), (b, "yes")])
    # a played an alt that night: the seat names the alt, the member is the same Discord id
    seat = ev.seat_of(a.discord_id)[1]
    seat.character = "Altchar"
    rs.save(ev, "t: alt")
    assert reg.attendance(a) == (1, 1), "attendance is the member's, whatever character held the seat"
    assert reg.attendance_runs(a)[0]["character"] == "Altchar"


def test_unknown_is_neutral_and_excluded_until_three_runs(reg, rs):
    people = reg.test_members()[:4]
    a = people[0]
    done_run(reg, rs, people, people, 3, confirm=[(m, "yes") for m in people])
    assert reg.attendance(a) == (1, 1) and reg.attendance_known(a) is None, "one run: not enough to judge anyone"
    look = scoring.attendance_lookup(reg)
    seat = done_run(reg, rs, people, people, 4, confirm=[(m, "yes") for m in people], n=1).seat_of(a.discord_id)[1]
    assert look(seat) is None, "two runs: still unknown → neutral"
    done_run(reg, rs, people, people[1:], 5, confirm=[(m, "yes") for m in people[1:]], n=2)
    assert reg.attendance_known(a) == (2, 3)
    assert scoring.attendance_lookup(reg)(seat) == (2, 3)


def test_scoring_uses_registry_attendance_and_keeps_unknown_neutral(reg, rs):
    from oibot_gm.profiles import Item

    people = reg.test_members()[:6]
    three_runs(reg, rs, people[:5])
    # people[5] joined but never sat (a regular benched three times): known, 0 of 3
    for i in range(3):
        done_run(reg, rs, people, people[:5], 10 + i, regulars=people, confirm=[(m, "yes") for m in people[:5]], n=10 + i)
    item = Item(id=0, name="Mystery Cloak", slot="back", type="cloth", boss="?", tiers={})
    ev = done_run(reg, rs, people, people, 1, confirm=[(m, "yes") for m in people], n=30)
    roster = list(ev.seated())
    cands = {c.character: c for c in scoring.candidates(reg.profile, item, roster, [], {}, date.today(), reg=reg)}
    full = cands[people[0].main.label]
    benched = cands[people[5].main.label]
    assert full.attendance_known and full.factors["attendance"] == 1.0 and full.attendance_str == "7/7"
    assert benched.attendance_known and benched.attendance_str == "1/4" and benched.factors["attendance"] < 1.0
    assert benched.base_score < full.base_score
    # an outsider with no history: unknown → neutral, excluded (never 0.5)
    stranger = roster[0].model_copy(update={"discord_id": 999, "character": "Nobody", "signup_name": "Nobody"})
    c = scoring.candidates(reg.profile, item, [stranger], [], {}, date.today(), reg=reg)[0]
    assert not c.attendance_known and c.factors["attendance"] == 1.0 and c.attendance_str == "n/a"


def test_the_sentence_is_computed(reg, rs):
    a = reg.test_members()[0]
    assert reg.attendance_text(a) == "not tracked yet — counts from the first run that ends"
    done_run(reg, rs, [a], [a], 5, confirm=[(a, "yes")])
    assert reg.attendance_text(a).startswith("1 of 1 since "), "the window reaches back past the guild's first run"
    done_run(reg, rs, [a], [], 40, n=1)  # an empty run 40 days back: no activity, never the 'first run'
    assert reg.attendance_text(a).startswith("1 of 1 since ")
    done_run(reg, rs, [a], [a], 40, confirm=[(a, "yes")], n=2)
    assert reg.attendance_text(a) == "1 of 1 in the last 30 days"
    b = reg.test_members()[1]
    assert reg.attendance_text(b) == "no runs for you in the last 30 days"
