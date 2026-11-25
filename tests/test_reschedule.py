"""Phase 3, Task 5: a run moves to another start (every raid of it, keeping their offsets) or takes its own sheet timing,
on its own: answers stand, nobody is DMed, everyone who answered gets a notice, the sheet and the open cards are edited
in place. A moved run still answers for the occurrence it was opened for — never opened again, never blocking the
schedule's next one — and a schedule's run time edited while its run is open never opens a second sheet that night."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import OWNER, by_role, join, lock_with_board, open_test_run, weekly
from test_event_identity import _big_guys_pair, _old_guild, _scheduler, _soon
from test_guild_schedules import commit_count
from test_raid_night import NightBot

from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError

RID, HYJAL, ONY = "barrow_deeps", "hyjal_summit_forever", "onyxias_lair"
OPENED = {"first_open": "2026-01-01T00:00"}
WEEKLY_LOCKOUT = {"first_open": "2026-01-01T00:00", "lockout_days": 7}  # a Thursday: Thursday-to-Wednesday lockouts
WED_LOCKOUT = {"first_open": "2026-01-07T15:00", "lockout_days": 7}  # Forever's shape: Wednesday 3 PM to Wednesday 3 PM
OFFICER_ID = OWNER  # an officer decided live: the owner needs no Discord lookup in the fake


def run(coro):
    return asyncio.run(coro)


def open_one_off(reg, rs, days: float = 3, raid: str = RID, **kw):
    start = (reg.now_local() + timedelta(days=days)).replace(hour=19, minute=30, second=0, microsecond=0)
    return rc.open_run(reg, rs, raid, start, by="t", schedule=rc.ADHOC_SCHEDULE, **kw)


def real_people(reg, n: int):
    """`n` real members with a main (a real run's sheet takes real members only)."""
    out = []
    for i in range(n):
        uid = 610_000_000_000_000_000 + i
        reg.add_character(uid, f"Member{i}", f"Char{chr(97 + i)}", "Mage", "Fire", None, True)
        out.append(reg.members[uid])
    return out


class Sheet:
    """The posted sheet: fetch_message hands back one message that records its edits (never a new post)."""

    def __init__(self, cid):
        self.id, self.sent, self.msg = cid, [], _SheetMsg()

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})

    async def fetch_message(self, mid):
        return self.msg


class _SheetMsg:
    flags = type("F", (), {"components_v2": False})()

    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


def posted(bot, ev) -> Sheet:
    sheet = Sheet(4242)
    bot.channels[sheet.id] = sheet
    ev.channel_id, ev.message_id = sheet.id, 9191
    return sheet


def moved_notices(reg, uid) -> list[dict]:
    return [n for n in reg.members[int(uid)].notices if n["kind"] == "moved"]


# ---------------------------------------------------------------- the move

def test_answers_stand_and_everyone_who_answered_gets_a_notice(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    people = real_people(reg, 4)
    join(reg, rs, ev, people)
    sheet = posted(bot, ev)
    before = {k: v.status for k, v in ev.signups.items()}
    new = ev.start + timedelta(days=1)
    lines = run(bot.reschedule_event(reg, rs, ev, start=new, by="o", by_id=OFFICER_ID, preview=True))
    assert lines[0] == f"The event moves to {reg.local12(new)}." and ev.start != new, "a preview writes nothing"
    assert lines[1] == "All 4 answers stand, and each person gets a notice."
    run(bot.reschedule_event(reg, rs, ev, start=new, by="o", by_id=OFFICER_ID))
    assert {k: v.status for k, v in ev.signups.items()} == before and ev.start == new
    assert all(moved_notices(reg, k) for k in before)
    assert moved_notices(reg, people[0].discord_id)[0]["text"] == f"Barrow Deeps now starts {reg.local12(new)}."
    assert bot.dms == {}, "nobody is DMed"
    assert sheet.sent == [] and len(sheet.msg.edits) == 1 and ev.message_id == 9191, "the sheet is edited in place"
    assert rc.RaidStore(reg.store, reg.key).events[ev.key].starts_at == new.isoformat(), "saved"
    assert any("moved from" in x for x in ev.log)
    assert [x for lvl, x in bot.ops.lines if "moved" in x], "one audit line"


def test_every_part_moves_with_its_offset(reg, rs):
    """A 3-raid run moved 2 hours: each raid's start +2 h, `starts` unchanged, later raids' cut-offs = the first's + offset."""
    for r in (RID, HYJAL, ONY):
        reg.set_raid_overrides(r, OPENED, "t")
    bot = NightBot(reg, rs)
    first = open_one_off(reg, rs, days=4, raid=HYJAL, lineup=[{"raid": RID, "rosters": 1}, {"raid": ONY, "rosters": 1}])
    parts = rc.event_parts(rs, first)
    was = [(p.start, p.starts) for p in parts]
    new = first.start + timedelta(hours=2)
    n0 = commit_count(reg.store)
    run(bot.reschedule_event(reg, rs, parts[2], start=new, by="o", by_id=OFFICER_ID))  # any raid resolves to the first
    reg.store.flush()
    assert commit_count(reg.store) == n0 + 1, "every raid and its dict saved in one commit"
    assert [(p.start, p.starts) for p in parts] == [(t + timedelta(hours=2), s) for t, s in was]
    assert [p.start for p in parts] == [rc.after_minutes(new, p.starts) for p in parts]
    t0 = reg.config.roster(first.team)
    for p in parts[1:]:
        t = reg.config.roster(p.team)
        for k in ("cutoff_soft_hours", "cutoff_hard_hours", "confirm_hours"):
            assert t[k] == pytest.approx(t0[k] + p.starts / 60)
        assert t["schedule"] == p.start.astimezone(reg.tz).strftime("%a %H:%M")
    assert [(p.occurrence, p.schedule) for p in parts] == [(None, None)] * 3, "the identity never changes"


def test_the_old_one_off_pair_moves_like_any_run(data_root, old_store_with):
    """big-guys-for's live shape (old `-adhoc` / `-after` keys, a posted sheet): both raids move, the keys and the sheet
    message stay, the answers stand."""
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, files)
    rc.migrate_at_start(reg, rs)
    lk, fk = files[0]["key"], files[1]["key"]
    bot = NightBot(reg, rs)
    lead = rs.events[lk]
    sheet = posted(bot, lead)
    lead.message_id = 777
    answers = {k: v.status for k, v in lead.signups.items()}
    new = t + timedelta(days=1, hours=1)
    run(bot.reschedule_event(reg, rs, rs.events[fk], start=new, by="o", by_id=OFFICER_ID))
    assert (rs.events[lk].start, rs.events[fk].start) == (new, new + timedelta(hours=3))
    assert sorted(rs.events) == sorted([lk, fk]) and rs.events[lk].message_id == 777 and sheet.sent == []
    assert {k: v.status for k, v in rs.events[lk].signups.items()} == answers
    assert set(rs.events[fk].signups) == set(answers), "the follow-on's answers are still the lead's"


@pytest.mark.parametrize("verb", ["move", "lineup"])
def test_a_run_the_start_never_stamped_is_stamped_before_it_changes(data_root, old_store_with, verb):
    """A live schedule run without stamps (the start's stamping failed) is stamped from its own night at the move or the
    lineup edit, in the same write — else the unstamped fallback finds it by its new start and its night opens again."""
    from test_event_identity import LEAD, OPENED as OPEN, _old_dict, _old_run, due

    t = _soon()
    key = f"bd-{t:%m%d-%H%M}-{t.date().isoformat()}"
    cfg = {"raids": {RID: OPEN}, "schedules": [{"id": "default", "lineup": [{"raid": RID, "rosters": 1}], "slots": [t.strftime("%a %H:%M")], **LEAD}],
           "rosters": [_old_dict(key[:-11], RID, t, "default", series="barrow_deeps:default")]}
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, [_old_run(key, key[:-11], RID, t, schedule="default", series="barrow_deeps:default")])
    due(reg, RID, t)
    bot, opened = _scheduler(reg, rs)
    ev = rs.events[key]
    assert ev.event is None, "the premise: the start's stamping never ran"
    if verb == "move":
        run(bot.reschedule_event(reg, rs, ev, start=t + timedelta(days=1), by="o", by_id=OFFICER_ID))
    else:
        run(bot.edit_lineup(reg, rs, ev, [{"raid": RID, "rosters": 1}, {"raid": ONY, "rosters": 1}], by="o", by_id=OFFICER_ID))
    store.flush()
    stored = json.loads((data_root / ev.rel_path(rs.key)).read_text())
    assert (stored["event"], stored["occurrence"]) == (key, rc.occurrence_stamp(reg, t)), "written with the change"
    assert reg.config.roster(ev.team)["occurrence"] == rc.occurrence_stamp(reg, t)
    run(bot.scheduler_tick_guild(reg))
    assert opened == [], "its own night never opens a second sheet"


# ---------------------------------------------------------------- occurrences: never repeated, never blocked

def _tuesdays(reg, sid_fields=None):
    reg.set_raid_overrides(RID, WEEKLY_LOCKOUT, "t")  # Thursday-to-Wednesday lockouts
    sid = weekly(reg, RID, "Tue 19:30")
    reg.set_schedule(sid, {"signup_lead_hours": 48, "nudge_hours_before": 36, "lock_hours_before": 24, "confirm_hours_before": 12}, "t")
    now = reg.now_local()
    t1 = next(t for _s, t in rc.upcoming(reg, RID, now, 24 * 21) if t - now >= timedelta(days=3))
    return sid, t1


def _at(monkeypatch, reg, clock, when):
    clock["now"] = when
    monkeypatch.setattr(reg, "now_local", lambda: clock["now"])


def test_a_moved_run_never_blocks_or_repeats_an_occurrence(reg, rs, monkeypatch):
    """Weekly Tue 7:30 PM. Next Tuesday's run (T1) moved to the Monday after: T1's slot never opens again and T2 opens
    on time. Then the T2 run moved past T3: T3 still opens on time."""
    sid, t1 = _tuesdays(reg)
    t2, t3 = t1 + timedelta(days=7), t1 + timedelta(days=14)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _at(monkeypatch, reg, clock, t1 - timedelta(hours=47))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1 and rs.events[opened[0]].start == t1
    r1 = rs.events[opened[0]]
    run(bot.reschedule_event(reg, rs, r1, start=t1 + timedelta(days=6), by="o", by_id=OFFICER_ID))
    _at(monkeypatch, reg, clock, t1 - timedelta(hours=46))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1, "the slot it left never opens again"
    assert rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, t1), reg=reg) is r1, "it still answers for T1"
    _at(monkeypatch, reg, clock, t2 - timedelta(hours=47))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 2 and rs.events[opened[1]].start == t2, "the next Tuesday opens on time"
    r2 = rs.events[opened[1]]
    r1.state = "cancelled"  # out of the way: the clock jumps past its night below
    rs.save(r1, "harness")
    run(bot.reschedule_event(reg, rs, r2, start=t3 + timedelta(days=1), by="o", by_id=OFFICER_ID))
    _at(monkeypatch, reg, clock, t3 - timedelta(hours=47))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 3 and rs.events[opened[2]].start == t3, "T3 still opens on time"
    assert len(bot.signup.sent) == 3, "one sheet per run, none twice"


def test_a_schedules_time_edited_with_its_run_open_opens_no_second_sheet(reg, rs, monkeypatch):
    """The confirm promised "The open event keeps its old times.": that night's 8:00 PM is the 7:30 PM run, and the next
    week opens at 8:00 PM."""
    sid, t1 = _tuesdays(reg)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _at(monkeypatch, reg, clock, t1 - timedelta(hours=47))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Tue 20:00"}, "t")
    t1b = t1 + timedelta(minutes=30)
    _at(monkeypatch, reg, clock, t1 - timedelta(hours=46))
    assert any(t == t1b for _s, t in rc.occurrences(reg, reg.now_local(), lead=True)), "the premise: 8:00 PM is due"
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1 and r1.start == t1, "no second sheet that night; the open run keeps its old time"
    assert rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, t1b), reg=reg) is r1
    assert all(r["start_iso"] != t1b.isoformat() for r in rc.upcoming_rows(reg, rs, RID)), "no Open row for that night"
    r1.state = "cancelled"
    rs.save(r1, "harness")
    t2b = t1b + timedelta(days=7)
    _at(monkeypatch, reg, clock, t2b - timedelta(hours=47))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 2 and rs.events[opened[1]].start == t2b, "the next week opens at the new time"


def test_two_schedule_slots_on_one_day_keep_their_own_runs(reg, rs):
    """The night's-run rule only stands in for a slot the schedule no longer has: two slots on one day are two runs."""
    reg.set_raid_overrides(RID, OPENED, "t")
    sid = weekly(reg, RID, ["Tue 18:00", "Tue 21:00"])
    now = reg.now_local()
    a, b = [t for _s, t in rc.upcoming(reg, RID, now, 24 * 21) if t - now >= timedelta(days=2)][:2]
    assert a.date() == b.date()
    ra = rc.open_run(reg, rs, RID, a, schedule=sid)
    assert rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, b), reg=reg) is None


def test_lockout_and_conflicts_read_the_moved_start(reg, rs):
    """A locked run moved into the next lockout window counts there for saved_this_lockout; conflicts read the new start."""
    reg.set_raid_overrides(RID, OPENED, "t")
    reg.set_raid_overrides(HYJAL, OPENED, "t")
    bot = NightBot(reg, rs)
    people = real_people(reg, 10)
    # placed by the lockout's own calendar, never by today's date: A starts a day into a window, 19:30, so A + 5 hours
    # stays in A's window and A + one lockout (+ 5 hours) lies in the next one, whatever day the suite runs
    days = int(reg.raid_def(RID)["lockout_days"])
    assert days >= 2
    win_start, _ = reg.lockout_window(RID, reg.now_local() + timedelta(days=3))
    a_start = (win_start + timedelta(days=1)).replace(hour=19, minute=30, second=0, microsecond=0)
    a = rc.open_run(reg, rs, RID, a_start, by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, a, people)
    lock_with_board(reg, rs, a, [m.display_name for m in people[:5]], hours_to_confirm=2)
    old_win = reg.lockout_window(RID, a.start)
    same = rc.open_run(reg, rs, RID, a.start + timedelta(hours=5), schedule=rc.ADHOC_SCHEDULE)  # same window as A
    assert reg.lockout_window(RID, same.start) == old_win
    seated = {p.character for p in a.seated()}
    assert rc.saved_this_lockout(reg, rs, same) == seated
    new = a.start + timedelta(days=days)  # the same moment of the next window
    nxt = rc.open_run(reg, rs, RID, new + timedelta(hours=5), schedule=rc.ADHOC_SCHEDULE)
    assert reg.lockout_window(RID, nxt.start) != old_win and rc.saved_this_lockout(reg, rs, nxt) == set()
    hy_old = rc.open_run(reg, rs, HYJAL, a.start + timedelta(hours=1), schedule=rc.ADHOC_SCHEDULE)
    hy_new = rc.open_run(reg, rs, HYJAL, new + timedelta(hours=1), schedule=rc.ADHOC_SCHEDULE)
    assert set(rc.conflicts(rs, hy_old)) == {m.discord_id for m in people}
    run(bot.reschedule_event(reg, rs, a, start=new, by="o", by_id=OFFICER_ID))
    assert reg.lockout_window(RID, a.start) == reg.lockout_window(RID, nxt.start)
    assert rc.saved_this_lockout(reg, rs, same) == set() and rc.saved_this_lockout(reg, rs, nxt) == seated
    assert rc.conflicts(rs, hy_old) == {} and set(rc.conflicts(rs, hy_new)) == {m.discord_id for m in people}
    assert a.state == "locked" and {p.character for p in a.seated()} == seated, "the roster stands"


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("case, sentence", [("past", "in the past"), ("closed", "finished or cancelled"),
                                            ("clash", "already has an event at"), ("same", "already starts then")])
def test_refusals_are_one_sentence(reg, rs, case, sentence):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    start = ev.start + timedelta(days=1)
    if case == "past":
        start = reg.now_local() - timedelta(hours=1)
    elif case == "closed":
        ev.state = "cancelled"
        rs.save(ev, "harness")
    elif case == "clash":
        rc.open_run(reg, rs, RID, start, schedule=rc.ADHOC_SCHEDULE)
    else:
        start = ev.start
    why = rc.reschedule_refusal(reg, rs, ev, start, None)
    assert why in (cq.move_past_sentence(reg, start, ev.start), rc.MOVE_CLOSED, rc.MOVE_SAME, rc.MOVE_CLASH.format(raid="Barrow Deeps", when=reg.local12(start)))
    assert sentence in why
    raw = (reg.store.root / ev.rel_path(reg.key)).read_text()
    n0 = commit_count(reg.store)
    with pytest.raises(RegistryError, match=sentence):
        run(bot.reschedule_event(reg, rs, ev, start=start, by="o", by_id=OFFICER_ID))
    reg.store.flush()
    assert (reg.store.root / ev.rel_path(reg.key)).read_text() == raw and commit_count(reg.store) == n0, "nothing written"


def test_a_member_is_refused(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    m = real_people(reg, 1)[0]
    with pytest.raises(RegistryError, match="Only an officer"):
        run(bot.reschedule_event(reg, rs, ev, start=ev.start + timedelta(days=1), by=m.display_name, by_id=m.discord_id))


# ---------------------------------------------------------------- per-run timing

def test_timing_lives_on_the_run_alone(reg, rs):
    from oibot_gm.raid_views import run_times

    reg.set_raid_overrides(RID, OPENED, "t")
    sid = weekly(reg, RID, "Tue 19:30")
    now = reg.now_local()
    t = next(x for _s, x in rc.upcoming(reg, RID, now, 24 * 21) if x - now >= timedelta(days=3))
    ev = rc.open_run(reg, rs, RID, t, schedule=sid)
    bot = NightBot(reg, rs)
    stored = json.dumps(reg.config.schedules, sort_keys=True)
    lines = run(bot.reschedule_event(reg, rs, ev, timing={"lock_hours_before": 6}, by="o", by_id=OFFICER_ID, preview=True))
    assert lines[0] == f"For this event, the sheet locks {reg.local12(t - timedelta(hours=6))}." and lines[1] == "Answers stand."
    only = cq.reschedule_consequence(reg, rs, ev, None, {"autofill": False})
    assert only[0] == "For this event, automatic asks are off.", "the lock time didn't change, so it isn't named"
    both = cq.reschedule_consequence(reg, rs, ev, t + timedelta(days=1), {"autofill": False, "lock_hours_before": 6})
    assert both[0] == f"The event moves to {reg.local12(t + timedelta(days=1))} with its own lock time and automatic asks."
    assert rc.reschedule_refusal(reg, rs, ev, None, {"lock_hours_before": rc.run_team(reg, ev)["cutoff_hard_hours"]}) == rc.TIMING_SAME
    run(bot.reschedule_event(reg, rs, ev, timing={"lock_hours_before": 6, "nudge": False, "autofill": False, "fill_ask_hours": 2},
                             by="o", by_id=OFFICER_ID))
    team = reg.config.roster(ev.team)
    assert (team["cutoff_hard_hours"], team["reminders"], team["autofill"], team["fill_ask_hours"]) == (6, rc.OFF, False, 2)
    assert run_times(reg, ev, rc.run_team(reg, ev))[1] == t - timedelta(hours=6), "the sheet, the scheduler and the Me page read the run's own"
    assert json.dumps(reg.config.schedules, sort_keys=True) == stored, "the schedule is untouched"
    assert ev.start == t and not [n for m in reg.members.values() for n in m.notices if n["kind"] == "moved"], "a timing change tells nobody"
    assert rc.reschedule_refusal(reg, rs, ev, None, {"lock_hours_before": 2, "confirm_hours_before": 4}) == rc.TIMING_ORDER
    assert rc.reschedule_refusal(reg, rs, ev, None, {"nudge_hours_before": 1, "nudge": True}) == rc.TIMING_ORDER
    assert rc.reschedule_refusal(reg, rs, ev, None, {"nudge_hours_before": 1}) is None, "a reminder that is off sets no order"
    lock_with_board(reg, rs, ev, [])
    assert rc.reschedule_refusal(reg, rs, ev, None, {"lock_hours_before": 8}) == rc.TIMING_LOCKED


def test_a_later_raids_timing_follows_the_first(reg, rs):
    for r in (RID, HYJAL):
        reg.set_raid_overrides(r, OPENED, "t")
    first = open_one_off(reg, rs, days=4, raid=HYJAL, follow={"raid": RID, "rosters": 1})
    later = rc.later_parts(rs, first)[0]
    rc.reschedule(reg, rs, later, timing={"lock_hours_before": 10}, by="o")
    assert reg.config.roster(first.team)["cutoff_hard_hours"] == 10
    assert reg.config.roster(later.team)["cutoff_hard_hours"] == pytest.approx(10 + later.starts / 60)


# ---------------------------------------------------------------- after the lock

def test_after_the_lock_the_roster_stands_and_open_cards_are_edited(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated], hours_to_confirm=1)
    for m in seated[:2]:
        sg = ev.signups[str(m.discord_id)]
        assert run(bot.confirm_one(reg, ev, sg, 0, 0, "lock"))
    rosters = [list(map(list, r.groups)) for r in ev.rosters]
    cb = ev.confirm_by
    dms = sum(len(v) for v in bot.dms.values())
    new = ev.start + timedelta(days=1)
    run(bot.reschedule_event(reg, rs, ev, start=new, by="o", by_id=OFFICER_ID))
    assert [list(map(list, r.groups)) for r in ev.rosters] == rosters and ev.state == "locked"
    from datetime import datetime
    assert datetime.fromisoformat(ev.confirm_by) == datetime.fromisoformat(cb) + timedelta(days=1)
    edited = [c for c in bot.cards.values() if c.edits]
    assert len(edited) == 2 and all(len(c.edits) == 1 for c in edited)
    from test_raid_night import card_texts
    for c in edited:
        text = card_texts(c)[0]
        assert f"<t:{int(new.timestamp())}:F>" in text and f"<t:{int(datetime.fromisoformat(ev.confirm_by).timestamp())}:f>" in text
    assert all(c.edits[0]["view"] is not None for c in edited) and sum(len(v) for v in bot.dms.values()) == dms, "no new DM"


def test_a_confirm_by_already_past_moves_to_within_the_hour(reg, rs):
    ev = open_one_off(reg, rs, days=3)
    lock_with_board(reg, rs, ev, [], hours_to_confirm=1)
    ev.confirm_by = (reg.now_local() + timedelta(minutes=10)).isoformat()
    new = reg.now_local() + timedelta(hours=3)
    m = rc.reschedule(reg, rs, ev, start=new, by="o")
    from datetime import datetime
    got = datetime.fromisoformat(ev.confirm_by)
    assert reg.now_local() + timedelta(minutes=59) <= got <= reg.now_local() + timedelta(hours=1, minutes=1)
    assert m.locks_now is False


def test_away_answers_are_worked_out_again(reg, rs):
    """A away on day 1 only (an away row), B away on day 2 with no answer, C away on day 2 who answered Join: moved to
    day 2 → A's row gone, B out from absence, C still Join."""
    a, b, c = real_people(reg, 3)
    day1 = (reg.now_local() + timedelta(days=3)).date()
    day2 = day1 + timedelta(days=1)
    reg.add_absence(a.discord_id, day1.isoformat(), day1.isoformat(), None, "t")
    ev = open_one_off(reg, rs, days=3)
    assert ev.signups[str(a.discord_id)].source == "absence"
    reg.add_absence(b.discord_id, day2.isoformat(), day2.isoformat(), None, "t")
    reg.add_absence(c.discord_id, day2.isoformat(), day2.isoformat(), None, "t")
    join(reg, rs, ev, [c])
    preview = rc.reschedule(reg, rs, ev, start=ev.start + timedelta(days=1), by="o", save=False)
    assert str(a.discord_id) in ev.signups, "a preview writes nothing"
    assert (preview.absence_cleared, preview.absence_written) == ([a.discord_id], [b.discord_id])
    assert "Away answers are worked out again for the new day." in cq.reschedule_consequence(reg, rs, ev, ev.start + timedelta(days=1), None)
    rc.reschedule(reg, rs, ev, start=ev.start + timedelta(days=1), by="o")
    assert str(a.discord_id) not in ev.signups
    assert (ev.signups[str(b.discord_id)].status, ev.signups[str(b.discord_id)].source) == ("out", "absence")
    assert ev.signups[str(c.discord_id)].status == "in"


def test_the_confirm_says_it_locks_now_and_names_a_clash_of_people(reg, rs):
    reg.set_raid_overrides(HYJAL, OPENED, "t")
    ev = open_one_off(reg, rs, days=3)
    people = real_people(reg, 3)
    join(reg, rs, ev, people)
    soon = reg.now_local() + timedelta(hours=2)
    lines = cq.reschedule_consequence(reg, rs, ev, soon, None)
    assert lines[-1] == "It locks within a minute, because its lock time has passed."
    later = ev.start + timedelta(days=2)
    other = rc.open_run(reg, rs, HYJAL, later + timedelta(hours=1), schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, other, people)
    lines = cq.reschedule_consequence(reg, rs, ev, later, None)
    assert lines[-1] == f"{people[0].display_name} and 2 others are also on Hyjal Summit {reg.local12(other.start)} that night."
    assert cq.reschedule_consequence(reg, rs, ev, ev.start + timedelta(days=1), None)[-1] == "The sheet in Discord shows the new time."


# ---------------------------------------------------------------- fix round 1

def _schedule(reg, slots, anchor=None):
    """A weekly schedule whose sheet opens 72 h before and locks 2 h before: a clock jump of a few days never locks. The
    raid's lockouts (the anchor a weekly run time means one run in) run Thursday to Wednesday unless `anchor` says."""
    reg.set_raid_overrides(RID, anchor or WEEKLY_LOCKOUT, "t")
    sid = weekly(reg, RID, slots)
    reg.set_schedule(sid, {"signup_lead_hours": 72, "nudge_hours_before": 3, "lock_hours_before": 2, "confirm_hours_before": 1}, "t")
    return sid


def _first_of(reg, slot: str):
    now = reg.now_local()
    return next(t for _s, t in rc.upcoming(reg, RID, now, 24 * 21) if t - now >= timedelta(days=4) and t.strftime("%a %H:%M") == slot)


@pytest.mark.parametrize("was, now_", [("Tue 23:30", "Wed 00:30"), ("Tue 19:30", "Wed 19:30")],
                         ids=["across midnight", "another weekday"])
def test_an_edited_run_time_in_another_day_opens_no_second_run(reg, rs, monkeypatch, was, now_):
    sid = _schedule(reg, was)
    t1 = _first_of(reg, was)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _at(monkeypatch, reg, clock, t1 - timedelta(hours=71))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": now_}, "t")
    new = next(t for _s, t in rc.occurrences(reg, t1 - timedelta(hours=70), 24 * 5) if _s["id"] == sid)
    assert new.strftime("%a %H:%M") == now_ and timedelta(0) < new - t1 < timedelta(days=2)
    _at(monkeypatch, reg, clock, new - timedelta(hours=69))
    assert t1 > reg.now_local(), "the premise: Tuesday's run hasn't started"
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 1 and r1.start == t1, "one run that week, at its old time"
    r1.state = "cancelled"
    rs.save(r1, "harness")
    _at(monkeypatch, reg, clock, new + timedelta(days=7) - timedelta(hours=71))
    run(bot.scheduler_tick_guild(reg))
    assert len(opened) == 2 and rs.events[opened[1]].start == new + timedelta(days=7), "the next week opens at the new time"


def test_editing_one_of_two_run_times_still_opens_the_other(reg, rs, monkeypatch):
    sid = _schedule(reg, ["Tue 19:30", "Thu 19:30"])
    tue = _first_of(reg, "Tue 19:30")
    thu = tue + timedelta(days=2)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _at(monkeypatch, reg, clock, tue - timedelta(hours=71))
    run(bot.scheduler_tick_guild(reg))
    assert [rs.events[k].start for k in opened] == [tue]
    reg.set_schedule(sid, {"slots": ["Wed 19:30", "Thu 19:30"]}, "t")
    _at(monkeypatch, reg, clock, thu - timedelta(hours=71))
    run(bot.scheduler_tick_guild(reg))
    assert [rs.events[k].start for k in opened] == [tue, thu], "Wednesday is Tuesday's run; Thursday opens its own"


def test_a_run_that_has_started_cant_move_or_take_timing(reg, rs):
    ev = open_one_off(reg, rs, days=3)
    ev.starts_at = (reg.now_local() - timedelta(minutes=10)).isoformat()
    rs.save(ev, "harness: under way")
    assert rc.reschedule_refusal(reg, rs, ev, reg.now_local() + timedelta(days=1), None) == rc.MOVE_STARTED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"autofill": False}) == rc.MOVE_STARTED


def test_a_run_moved_onto_its_schedules_unopened_night_says_it_stands_for_it(reg, rs):
    sid = _schedule(reg, "Tue 19:30")
    t1 = _first_of(reg, "Tue 19:30")
    ev = rc.open_run(reg, rs, RID, t1, schedule=sid)
    t2 = t1 + timedelta(days=7)
    lines = cq.reschedule_consequence(reg, rs, ev, t2, None)
    assert lines[-1] == "That night's Raid nights event is this one." and len(lines) == 3
    rc.reschedule(reg, rs, ev, start=t2, by="o")
    assert rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, t2), reg=reg) is ev
    assert cq.reschedule_consequence(reg, rs, ev, t2 + timedelta(hours=1), None)[-1] != "That night's Raid nights event is this one."


# ---------------------------------------------------------------- fix round 2: decided at the schedule write

def _tick(bot, reg, monkeypatch, clock, when):
    _at(monkeypatch, reg, clock, when)
    run(bot.scheduler_tick_guild(reg))


def _starts(rs, opened):
    return [rs.events[k].start for k in opened]


def _done(rs, ev):
    ev.state, ev.closed_at = "done", rc.now()
    rs.save(ev, "harness: over")


def test_p1_tue_moved_to_fri_still_opens_thursday(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Tue + Thu, Tue's run open, Tue edited to Fri: the Tue run stands for the
    Friday of ITS lockout (four days before it, already past); Thursday opens; the Friday after is in the next lockout
    and opens."""
    sid = _schedule(reg, ["Tue 19:30", "Thu 19:30"])
    tue = _first_of(reg, "Tue 19:30")
    thu, fri = tue + timedelta(days=2), tue + timedelta(days=3)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": ["Fri 19:30", "Thu 19:30"]}, "t")
    own_fri = rc.occurrence_stamp(reg, tue - timedelta(days=4))
    assert reg.config.roster(r1.team)["stands_for"] == own_fri
    _tick(bot, reg, monkeypatch, clock, thu - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, thu] and r1.stands_for == own_fri, "Thursday opens; the run file knows"
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, fri - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, thu, fri], "the Friday after Tuesday is in the next lockout: it opens"


def test_p2_a_finished_run_stands_for_nothing_new(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Weekly Sat, Sat's run done, then edited to Tue: the done run is never
    stamped; the Tuesday after the Saturday (same lockout, nothing holds it) opens."""
    sid = _schedule(reg, "Sat 19:30")
    sat = _first_of(reg, "Sat 19:30")
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, sat - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    _done(rs, r1)
    reg.set_schedule(sid, {"slots": "Tue 19:30"}, "t")
    assert reg.config.roster(r1.team).get("stands_for") is None
    tue = sat + timedelta(days=3)
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    assert _starts(rs, opened) == [sat, tue], "one run in Saturday's week, and the next Tuesday on time"


def test_p3_a_cancelled_run_stands_for_nothing(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Tue's run cancelled, then edited to Wed: Wednesday opens."""
    sid = _schedule(reg, "Tue 19:30")
    tue = _first_of(reg, "Tue 19:30")
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    r1.state = "cancelled"
    rs.save(r1, "harness")
    reg.set_schedule(sid, {"slots": "Wed 19:30"}, "t")
    _tick(bot, reg, monkeypatch, clock, tue + timedelta(days=1) - timedelta(hours=70))
    assert _starts(rs, opened) == [tue, tue + timedelta(days=1)], "Wednesday opens: the cancelled run holds only its own night"


def test_p4_tue_moved_to_sat_with_its_run_open_opens_no_second_run(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Tue edited to Sat with Tue's run open: the run stands for its own
    lockout's Saturday (three days before it); the Saturday after is in the next lockout and opens."""
    sid = _schedule(reg, "Tue 19:30")
    tue = _first_of(reg, "Tue 19:30")
    sat = tue + timedelta(days=4)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Sat 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, tue - timedelta(days=3))
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, sat - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, sat], "one run per lockout: Saturday is the next one's"


def test_p4_on_a_monday_week_stands_for_that_saturday(reg, rs, monkeypatch):
    """Anchor: a raid with no first opening reads Monday-to-Sunday weeks. Tue edited to Sat with Tue's run open: that
    week's Saturday is the Tuesday run's and doesn't open; the next Saturday does."""
    sid = _schedule(reg, "Tue 19:30", anchor=WEEKLY_LOCKOUT)
    monkeypatch.setattr(reg, "first_open", lambda instance: None)  # no anchor: Monday weeks (its runs still open from 1 Jan)
    tue = _first_of(reg, "Tue 19:30")
    sat = tue + timedelta(days=4)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Sat 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, sat)
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, sat - timedelta(hours=71))
    assert _starts(rs, opened) == [tue], "that week's Saturday is the Tuesday run's"
    _tick(bot, reg, monkeypatch, clock, sat + timedelta(days=7) - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, sat + timedelta(days=7)]


def test_two_run_times_edited_at_once(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Tue + Thu → Wed + Fri with Tue's run open: by time from the lockout's
    start, Thu ↔ Fri and Tue ↔ Wed, so Tue stands for the Wednesday after it (same lockout); Fri opens."""
    sid = _schedule(reg, ["Tue 19:30", "Thu 19:30"])
    tue = _first_of(reg, "Tue 19:30")
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": ["Wed 19:30", "Fri 19:30"]}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, tue + timedelta(days=1))
    _tick(bot, reg, monkeypatch, clock, tue + timedelta(days=1) - timedelta(hours=71))
    assert _starts(rs, opened) == [tue]
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, tue + timedelta(days=3) - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, tue + timedelta(days=3)]


def test_two_edits_in_a_row(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Tue → Wed → Thu with Tue's run open: each edit re-decides from the night
    the run stands for now (its stands_for, else its own occurrence), always inside the lockout of its ORIGINAL
    occurrence: Wed (the day after), then that lockout's Thursday (its first day, already past) — so the Thursday after
    Tuesday, in the next lockout, opens. Back to Tue clears it."""
    sid = _schedule(reg, "Tue 19:30")
    tue = _first_of(reg, "Tue 19:30")
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Wed 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, tue + timedelta(days=1))
    reg.set_schedule(sid, {"slots": "Thu 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, tue - timedelta(days=5))
    _tick(bot, reg, monkeypatch, clock, tue + timedelta(days=1) - timedelta(hours=71))
    assert _starts(rs, opened) == [tue], "Wednesday is not a run time any more"
    _tick(bot, reg, monkeypatch, clock, tue + timedelta(days=2) - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, tue + timedelta(days=2)], "the Thursday after is the next lockout's"
    reg.set_schedule(sid, {"slots": "Tue 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] is None, "back to its own night"


def test_its_own_night_given_back_beside_the_new_one_clears_the_stand_in(reg, rs, monkeypatch):
    """Tue → Wed with Tue's run open (it stands for that Wednesday), then 'Tue, Wed': the edit gives the run's own night
    back, so it stands for Tuesday only and Wednesday opens a run of its own."""
    sid = _schedule(reg, "Tue 19:30")
    tue = _first_of(reg, "Tue 19:30")
    wed = tue + timedelta(days=1)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Wed 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, wed)
    reg.set_schedule(sid, {"slots": ["Tue 19:30", "Wed 19:30"]}, "t")
    assert reg.config.roster(r1.team)["stands_for"] is None, "its own night is given again"
    _tick(bot, reg, monkeypatch, clock, wed - timedelta(hours=71))
    assert _starts(rs, opened) == [tue, wed], "Wednesday opens"
    assert rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, tue), reg=reg) is r1
    assert rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, wed), reg=reg) is rs.events[opened[1]]


def _lockout(reg, days):
    reg.set_raid_overrides(RID, OPENED, "t")  # 3-day lockouts from 1 Jan
    reg.set_schedule("bd-lock", {"kind": "lockout", "days": days, "time": "19:30", "name": "Lockout nights"}, "t", raid=RID)
    reg.set_schedule("bd-lock", {"signup_lead_hours": 48, "nudge_hours_before": 3, "lock_hours_before": 2, "confirm_hours_before": 1}, "t")
    now = reg.now_local()
    return "bd-lock", next(t for s, t in rc.occurrences(reg, now, 24 * 21) if s["id"] == "bd-lock" and t - now >= timedelta(days=4))


def test_lockout_day_3_to_day_1_never_takes_the_next_window(reg, rs, monkeypatch):
    sid, d3 = _lockout(reg, [3])
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, d3 - timedelta(hours=47))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"days": [1]}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, d3 - timedelta(days=2)), "its own window's day 1"
    nxt = d3 + timedelta(days=1)  # the next window's day 1
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, nxt - timedelta(hours=47))
    assert _starts(rs, opened) == [d3, nxt], "the next window's day 1 opens"


def test_lockout_day_1_to_day_3_opens_no_second_run_in_the_lockout(reg, rs, monkeypatch):
    sid, d1 = _lockout(reg, [1])
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, d1 - timedelta(hours=47))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"days": [3]}, "t")
    d3 = d1 + timedelta(days=2)
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, d3)
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, d3 - timedelta(hours=47))
    assert _starts(rs, opened) == [d1], "one run in that lockout"
    _tick(bot, reg, monkeypatch, clock, d3 + timedelta(days=3) - timedelta(hours=47))
    assert _starts(rs, opened) == [d1, d3 + timedelta(days=3)], "the next lockout's day 3 opens"


def test_the_run_thread_line_names_the_run_and_counts_cards(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    new = ev.start + timedelta(days=1)
    run(bot.reschedule_event(reg, rs, ev, start=new, by="Officer", by_id=OFFICER_ID))
    assert bot.updates[-1] == f"🕒 Barrow Deeps moved by Officer to {reg.local12(new)}" and ev.key not in bot.updates[-1]
    run(bot.reschedule_event(reg, rs, ev, timing={"autofill": False}, by="Officer", by_id=OFFICER_ID))
    assert bot.updates[-1] == "🕒 Barrow Deeps: its own sheet timing set by Officer"
    assert rc.reschedule_refusal(reg, rs, ev, ev.start, {"autofill": False}) == rc.TIMING_SAME


# ---------------------------------------------------------------- fix round 3

def test_the_order_run_times_are_typed_in_never_pairs_them(reg, rs, monkeypatch):
    """Anchor: Thursday-to-Wednesday lockouts. Tue + Thu → typed [Fri, Wed]: by time from the lockout's start, Thu ↔ Fri
    and Tue ↔ Wed, so Tuesday's run stands for the Wednesday after it and Wednesday doesn't open."""
    sid = _schedule(reg, ["Tue 19:30", "Thu 19:30"])
    tue = _first_of(reg, "Tue 19:30")
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": ["Fri 19:30", "Wed 19:30"]}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, tue + timedelta(days=1))
    _tick(bot, reg, monkeypatch, clock, tue + timedelta(days=1) - timedelta(hours=71))
    assert _starts(rs, opened) == [tue], "Wednesday is Tuesday's run"


@pytest.mark.parametrize("order", ["edit then cancel", "cancel then edit"])
def test_a_cancelled_run_never_holds_the_night_it_stood_for(reg, rs, monkeypatch, order):
    """Anchor: Thursday-to-Wednesday lockouts. Tue → Wed and Tue's run cancelled, in either order: Wednesday opens."""
    sid = _schedule(reg, "Tue 19:30")
    tue = _first_of(reg, "Tue 19:30")
    wed = tue + timedelta(days=1)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]

    def cancel():
        r1.state, r1.closed_at = "cancelled", rc.now()
        rs.save(r1, "harness: cancelled")

    if order == "edit then cancel":
        reg.set_schedule(sid, {"slots": "Wed 19:30"}, "t")
        assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, wed)
        cancel()
    else:
        cancel()
        reg.set_schedule(sid, {"slots": "Wed 19:30"}, "t")
    _tick(bot, reg, monkeypatch, clock, wed - timedelta(hours=70))
    assert _starts(rs, opened) == [tue, wed], "Wednesday opens"


def test_sunday_to_monday_on_a_wednesday_lockout(reg, rs, monkeypatch):
    """Anchor: Wednesday 3 PM lockouts (Forever's). Sun → Mon with Sunday's run open: the Monday after is in the same
    lockout, so the Sunday run stands for it and it doesn't open; the next Monday does."""
    sid = _schedule(reg, "Sun 19:30", anchor=WED_LOCKOUT)
    sun = _first_of(reg, "Sun 19:30")
    mon = sun + timedelta(days=1)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, sun - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Mon 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, mon)
    _done(rs, r1)
    _tick(bot, reg, monkeypatch, clock, mon - timedelta(hours=70))
    assert _starts(rs, opened) == [sun], "one run in that lockout"
    _tick(bot, reg, monkeypatch, clock, mon + timedelta(days=7) - timedelta(hours=71))
    assert _starts(rs, opened) == [sun, mon + timedelta(days=7)]


def test_monday_to_sunday_on_a_wednesday_lockout(reg, rs, monkeypatch):
    """Anchor: Wednesday 3 PM lockouts. Mon → Sun with Monday's run open: the run stands for its own lockout's Sunday
    (the day before it, past) — never the next lockout's — so the Sunday after opens."""
    sid = _schedule(reg, "Mon 19:30", anchor=WED_LOCKOUT)
    mon = _first_of(reg, "Mon 19:30")
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, mon - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Sun 19:30"}, "t")
    assert reg.config.roster(r1.team)["stands_for"] == rc.occurrence_stamp(reg, mon - timedelta(days=1))
    _done(rs, r1)
    nxt = mon + timedelta(days=6)
    _tick(bot, reg, monkeypatch, clock, nxt - timedelta(hours=71))
    assert _starts(rs, opened) == [mon, nxt], "the next lockout's Sunday opens"


def test_a_finished_runs_night_outlives_its_run_dict(reg, rs, monkeypatch):
    """The cleanup path: a stamped run finishes, cleanup_ephemeral drops its dict after the grace hour; the run file
    still holds the night it stood for, and that night never opens."""
    sid = _schedule(reg, "Tue 19:30")
    tue = _first_of(reg, "Tue 19:30")
    wed = tue + timedelta(days=1)
    bot, opened = _scheduler(reg, rs)
    clock: dict = {}
    _tick(bot, reg, monkeypatch, clock, tue - timedelta(hours=71))
    r1 = rs.events[opened[0]]
    reg.set_schedule(sid, {"slots": "Wed 19:30"}, "t")
    from datetime import datetime, timezone
    r1.state, r1.closed_at = "done", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    rs.save(r1, "harness: finished two hours ago")
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(r1.team) is None, "the dict is gone"
    stored = rc.RaidStore(reg.store, reg.key).events[r1.key]
    assert stored.stands_for == rc.occurrence_stamp(reg, wed), "the file holds the night"
    _tick(bot, reg, monkeypatch, clock, wed - timedelta(hours=70))
    assert _starts(rs, opened) == [tue], "Wednesday never opens"


def test_a_timing_edit_reads_no_run_file(reg, rs, monkeypatch):
    sid = _schedule(reg, "Tue 19:30")
    rc.open_run(reg, rs, RID, _first_of(reg, "Tue 19:30"), schedule=sid)

    def boom(_reg):
        raise AssertionError("a timing-only edit read the run files")

    monkeypatch.setattr(rc, "_run_files", boom)
    reg.set_schedule(sid, {"lock_hours_before": 3}, "t")


def test_a_refused_raid_write_puts_the_stamps_back(reg, rs, monkeypatch):
    """set_raid_overrides with schedules and a bad field: nothing it planned stays on a run dict."""
    sid = _schedule(reg, "Tue 19:30")
    ev = rc.open_run(reg, rs, RID, _first_of(reg, "Tue 19:30"), schedule=sid)
    rows = [{**s, "slots": ["Wed 19:30"]} for s in reg.raid_rows(RID)]
    real, calls, seen = reg._check_raid, [], {}

    def check(instance):  # the schedules write passes (its stamp is on the dict), the raid's final check refuses
        calls.append(instance)
        if len(calls) == 2:
            seen["stand"] = reg.config.roster(ev.team).get("stands_for")
            raise RegistryError("refused at the end")
        return real(instance)

    monkeypatch.setattr(reg, "_check_raid", check)
    with pytest.raises(RegistryError, match="refused at the end"):
        reg.set_raid_overrides(RID, {"schedules": rows}, "t")
    assert seen["stand"] == rc.occurrence_stamp(reg, ev.start + timedelta(days=1)), "the premise: it was stamped"
    assert "stands_for" not in reg.config.roster(ev.team) and reg.schedule_entry(sid)["slots"] == ["Tue 19:30"]
