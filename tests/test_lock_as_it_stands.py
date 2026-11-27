"""Issue 14, Task 2: the lock's fill choice in the engine. `RaidMixin.lock_run(..., fill=False)` locks the board AS IT
STANDS — exactly the placed names in their groups (a Bench answer Auto-fill put there too), nobody added (a name pinned
in or never benched that is not on the board stays off) — and every later raid of the event locks with the same
choice. `fill=True` (the default) is today's lock, roster for roster. A lock as it stands with people who answered and
nobody placed fails with rc.LOCK_EMPTY_BOARD (an event nobody answered still locks); one that leaves seats open pauses
Automated Fill Requests for the event, and the Not rostered notice then promises no DM. A confirm deadline already
past at any lock becomes the earlier of the start and an hour from now. The scheduler passes Auto-fill on Auto Lock;
its failed lock as it stands waits 15 minutes like any failed lock. With Auto Lock off the sheet, the officers' card
and the sheet's DMs say an officer locks it instead of a time."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import join, open_test_run
from test_bench_last import DPS, HEAL, _autofilled_case_one, base_eight, case_one, names, people, schedule_event
from test_follow_on import open_pair
from test_raid_night import NightBot, run

from oibot_gm import raidcycle as rc
from oibot_gm.constants import PAGE_WORDS
from oibot_gm.raid_views import health_layout, sheet_message


def groups_of(ev) -> list[list[str]]:
    return [sorted(g) for r in ev.all_rosters for g in r.groups]


def board_of(layout, k: int) -> list[list[str]]:
    """The layout's groups padded to whole rosters of `k` groups, trimmed after its last roster holding someone (as
    the lock builds them)."""
    flat = [sorted(g) for g in layout]
    flat += [[] for _ in range(-len(flat) % k)]
    while len(flat) > k and not any(flat[-k:]):
        flat = flat[:-k]
    return flat


def lock(bot, reg, rs, ev, **kw):
    return run(bot.lock_run(reg, rs, ev, by="Jon", **kw))


def ten_with_board(reg, rs, placed: int = 6):
    """A schedule's event: ten joined, `placed` of them on the board in two groups."""
    ev = schedule_event(reg, rs)
    ms = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, ms)
    ns = [m.display_name for m in ms]
    ev.layout = [ns[:min(placed, 5)], ns[5:placed]]
    rs.save(ev, "board (test)")
    return rs.events[ev.key], ms


# ---------------------------------------------------------------- as it stands = the board

def test_a_lock_as_it_stands_rosters_exactly_the_board(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = ten_with_board(reg, rs, placed=6)
    layout = [list(g) for g in ev.layout]
    assert rc.lock_open_slots(reg, rs, ev) == [4]
    line = lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert ev.state == "locked" and "locked by Jon" in line
    assert groups_of(ev) == board_of(layout, 2), "name for name, group for group"
    assert {p.signup_name for p in ev.all_rosters[0].benched} == names(ms[6:]), "the unplaced are not rostered"
    assert ev.lock_fill is False and any("as the board stood" in x for x in ev.log)
    assert len(ev.all_rosters[0].group_reports) == 2, "a report per group"


def test_a_board_as_it_stands_carries_the_usual_advisories(reg, rs):
    """Only damage dealers placed: the roster is short both tanks, and the lock card says so as the full lock's does."""
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg)
    join(reg, rs, ev, ms)
    ev.layout = [[m.display_name for m in ms[4:8]]]  # base_eight: 2 tanks, 2 healers, then 4 damage dealers
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    adv = rs.events[ev.key].all_rosters[0].advisories
    assert any(a.startswith("🔴 Tanks 0/2") for a in adv), adv


def test_a_name_placed_then_answering_no_thanks_is_not_rostered(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = ten_with_board(reg, rs, placed=6)
    gone = ms[0]
    join(reg, rs, rs.events[ev.key], [gone], "out")
    ev = rs.events[ev.key]
    lock(bot, reg, rs, ev, fill=False)
    seated = {p.signup_name for p in rs.events[ev.key].seated()}
    assert gone.display_name not in seated and seated == names(ms[1:6])


def test_preview_lock_as_it_stands_is_the_board_and_refuses_an_empty_one(reg, rs):
    from contextlib import asynccontextmanager

    import pytest as _pt

    from oibot_gm.discord_raid import RaidMixin
    from oibot_gm.registry import RegistryError

    class Bare:
        lock_preview = RaidMixin.lock_preview

        @asynccontextmanager
        async def solving(self, reg, key):
            raise AssertionError("a preview as it stands never solves")
            yield

    ev, _ms = ten_with_board(reg, rs, placed=6)
    layout = [list(g) for g in ev.layout]
    shown = run(Bare().lock_preview(reg, rs, ev, by="t", fill=False))
    assert [sorted(g) for r in shown for g in r.groups] == board_of(layout, 2)
    empty = schedule_event(reg, rs)
    join(reg, rs, empty, people(reg, 2, DPS))
    with _pt.raises(RegistryError, match="Nobody is on the board"):
        run(Bare().lock_preview(reg, rs, rs.events[empty.key], by="t", fill=False))


def test_two_rosters_as_they_stand(reg, rs):
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg) + people(reg, 4, DPS)
    join(reg, rs, ev, ms)
    ns = [m.display_name for m in ms]
    ev.layout = [[ns[0], ns[2]], [ns[4]], [ns[1], ns[3]], [ns[5], ns[6]]]  # roster 1: 3 placed, roster 2: 4 placed
    rs.save(ev, "board (test)")
    ev = rs.events[ev.key]
    assert rc.lock_open_slots(reg, rs, ev) == [7, 6]
    lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert len(ev.all_rosters) == 2 and groups_of(ev) == [sorted(g) for g in [[ns[0], ns[2]], [ns[4]], [ns[1], ns[3]], [ns[5], ns[6]]]]


def test_a_lineups_later_raid_locks_as_its_board_stands(reg, rs):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    ns = [m.display_name for m in ms]
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    lead.layout = [ns[:5], ns[5:8]]
    rs.save(lead, "board (test)")
    fev.layout = [ns[10:13], [], [ns[13]]]
    rs.save(fev, "board (test)")
    fev_layout = [list(g) for g in fev.layout]
    lock(bot, reg, rs, rs.events[lead.key], fill=False)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == "locked" and fev.state == "locked"
    assert {p.signup_name for p in lead.seated()} == set(ns[:8])
    assert groups_of(fev) == board_of(fev_layout, rc.groups_per_roster(reg, rc.run_size(reg, fev)))
    assert fev.lock_fill is False, "the later raid took the first raid's choice"


def test_a_bench_answer_auto_fill_placed_is_rostered_as_it_stands(reg, rs):
    bot = NightBot(reg, rs)
    ev, joiners, core, raider, trial = _autofilled_case_one(reg, rs)
    late = people(reg, 1, DPS)
    join(reg, rs, ev, late)  # the full lock would put this joiner before a Bench answer Auto-fill placed
    ev = rs.events[ev.key]
    layout = [list(g) for g in ev.layout]
    lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert {core.display_name, raider.display_name} <= {p.signup_name for p in ev.seated()}
    assert late[0].display_name not in {p.signup_name for p in ev.seated()}
    assert groups_of(ev) == board_of(layout, 2)


def test_a_name_pinned_in_off_the_board_is_left_out(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = ten_with_board(reg, rs, placed=6)
    pinned = ms[8]
    ev.pins[str(pinned.discord_id)] = "in"
    rs.save(ev, "pin (test)")
    ev = rs.events[ev.key]
    assert rc.lock_left_out(reg, rs, ev) == [pinned.display_name]
    lock(bot, reg, rs, ev, fill=False)
    assert pinned.display_name not in {p.signup_name for p in rs.events[ev.key].seated()}


# ---------------------------------------------------------------- fill=True is today's lock

def _fixture_hand_placed(reg, rs):
    ev, ms = ten_with_board(reg, rs, placed=4)
    join(reg, rs, ev, people(reg, 1, HEAL, "core"), "sub")
    return rs.events[ev.key]


@pytest.mark.parametrize("make", ["joiners_and_bench", "auto_filled", "hand_placed"])
def test_the_default_lock_rosters_what_the_full_solve_does(reg, rs, make):
    bot = NightBot(reg, rs)
    ev = {"joiners_and_bench": lambda: case_one(reg, rs)[0], "auto_filled": lambda: _autofilled_case_one(reg, rs)[0],
          "hand_placed": lambda: _fixture_hand_placed(reg, rs)}[make]()
    ev = rs.events[ev.key]
    want = ev.model_copy(deep=True)
    rc.propose(reg, rs, want, save=False)  # the solve as it was before the fill choice
    lock(bot, reg, rs, ev)
    got = rs.events[ev.key]
    assert got.state == "locked" and got.lock_fill is True and not got.autofill_paused
    assert [r.model_dump() for r in got.all_rosters] == [r.model_dump() for r in want.all_rosters]


# ---------------------------------------------------------------- the empty board

def test_people_answered_and_nobody_placed_is_refused(reg, rs):
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    join(reg, rs, ev, base_eight(reg))
    assert rc.lock_empty_refusal(reg, rs, ev) == rc.LOCK_EMPTY_BOARD
    line = lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert ev.state == "open" and ev.lock_error == rc.LOCK_EMPTY_BOARD and "lock failed" in line
    assert bot.updates == [f"⚠️ The lock by Jon failed. {rc.LOCK_EMPTY_BOARD} The sheet stays open."]


def test_an_event_nobody_answered_still_locks_as_it_stands(reg, rs):
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    assert rc.lock_empty_refusal(reg, rs, ev) is None
    lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert ev.state == "locked" and not ev.seated() and len(ev.all_rosters) == 1


def test_the_refusal_is_one_sentence_naming_the_way_out():
    w = rc.LOCK_EMPTY_BOARD
    assert w.count(".") == 1 and w.endswith(".") and len(w.split()) <= 20 and "place someone" in w and "auto-fill" in w


# ---------------------------------------------------------------- a late lock

def test_a_late_lock_never_asks_with_a_deadline_already_past(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs, days=0, soft=6, hard=5, confirm=4)  # starts in 3 h; confirm-by was an hour ago
    join(reg, rs, ev, reg.test_members()[:10])
    before = reg.now_local()
    lock(bot, reg, rs, ev)
    ev = rs.events[ev.key]
    cb = datetime.fromisoformat(ev.confirm_by)
    assert before < cb <= ev.start and cb <= reg.now_local() + rc.MOVE_CONFIRM_GRACE


def test_late_confirm_by_is_the_reschedule_rule():
    now = datetime(2026, 12, 1, 18, 0).astimezone()
    start = now + timedelta(hours=3)
    assert rc.late_confirm_by(now - timedelta(minutes=5), start, now) == now + rc.MOVE_CONFIRM_GRACE
    assert rc.late_confirm_by(now - timedelta(minutes=5), now + timedelta(minutes=20), now) == now + timedelta(minutes=20)
    assert rc.late_confirm_by(now + timedelta(hours=2), start, now) == now + timedelta(hours=2), "a deadline ahead is kept"


# ---------------------------------------------------------------- open seats: asks paused, the notice promises no DM

def test_open_seats_after_a_lock_as_it_stands_pause_the_asks_and_the_notice_says_an_officer_decides(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = ten_with_board(reg, rs, placed=6)
    assert rc.team_setting(rc.run_team(reg, ev), "autofill"), "Automated Fill Requests are on for a schedule's event"
    lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert ev.autofill_paused and ev.autofill_paused_by == "Jon"
    assert sum("automatic asks paused by Jon" in x for x in ev.log) == 1
    left = reg.members[ms[7].discord_id]
    said = [n["text"] for n in left.notices if n.get("kind") == "not_rostered"]
    assert said and said[-1].endswith(rc.LEFTOVER_PAUSED) and "DM" not in said[-1]


def test_a_full_board_as_it_stands_leaves_the_asks_on(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = ten_with_board(reg, rs, placed=10)
    lock(bot, reg, rs, ev, fill=False)
    ev = rs.events[ev.key]
    assert ev.state == "locked" and len(ev.seated()) == 10 and not ev.autofill_paused


def test_the_lock_keeps_its_signup_line(reg, rs):
    bot = NightBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev.channel_id = bot.signup.id
    rs.save(ev, "sheet (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    ev = rs.events[ev.key]
    assert ev.lock_line and ev.lock_line[0] == bot.signup.id and "is locked" in bot.signup.sent[-1]["content"]


# ---------------------------------------------------------------- a lineup is checked whole

def _pair_with_lead_board(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    lead = rs.events[lead.key]
    lead.layout = [[m.display_name for m in ms[:5]]]
    rs.save(lead, "board (test)")
    return rs.events[lead.key], rs.events[fev.key], ms


def test_a_lineup_with_an_empty_later_board_is_refused_whole(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    want = rc.LOCK_EMPTY_RAID.format(raid=reg.raid_def(fev.instance)["name"])
    assert rc.lock_empty_refusal(reg, rs, lead) == want and len(want.split()) <= 20
    line = lock(bot, reg, rs, lead, fill=False)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == "open" and fev.state == "open", "nothing locks: never a half-locked event"
    assert lead.lock_error == want and not fev.lock_error and "lock failed" in line
    assert bot.updates == [f"⚠️ The lock by Jon failed. {want} The sheet stays open."]


def test_the_clock_refuses_a_lineup_with_an_empty_later_board_whole(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    rc.reschedule(reg, rs, lead, timing={"auto_lock_fill": False}, by="o")
    at = rs.events[lead.key].start - timedelta(minutes=1)
    monkeypatch.setattr(reg, "now_local", lambda: at)
    run(bot.scheduler_tick_guild(reg))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == "open" and fev.state == "open" and lead.lock_error and lead.lock_error.startswith("Nobody is on the")
    assert len(bot.updates) == 1 and not fev.lock_error


def test_a_lineup_whose_every_raid_has_a_board_locks_whole_and_pauses_every_raid(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    rc.reschedule(reg, rs, lead, timing={"autofill": True}, by="o")
    fev = rs.events[fev.key]
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    assert rc.lock_empty_refusal(reg, rs, rs.events[lead.key]) is None
    lock(bot, reg, rs, rs.events[lead.key], fill=False)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == "locked" and fev.state == "locked"
    assert {p.signup_name for p in fev.seated()} == {m.display_name for m in ms[5:8]}
    assert lead.autofill_paused and fev.autofill_paused and lead.autofill_paused_by == fev.autofill_paused_by == "Jon"


# ---------------------------------------------------------------- the clock's lock

def _clock_event(reg, rs, *, fill: bool, placed: bool = True):
    ev = open_test_run(reg, rs, days=0, soft=6, hard=4, confirm=1)  # starts in 3 h; its lock time was an hour ago
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    rc.reschedule(reg, rs, ev, timing={"auto_lock_fill": fill}, by="o")
    ev = rs.events[ev.key]
    if placed:
        ev.layout = [[m.display_name for m in ms[:4]]]
        rs.save(ev, "board (test)")
    return rs.events[ev.key], ms


def test_the_clocks_lock_with_auto_fill_off_rosters_the_board(reg, rs):
    bot = NightBot(reg, rs)
    ev, ms = _clock_event(reg, rs, fill=False)
    run(bot.scheduler_tick_guild(reg))
    ev = rs.events[ev.key]
    assert ev.state == "locked" and ev.lock_fill is False
    assert {p.signup_name for p in ev.seated()} == {m.display_name for m in ms[:4]}


def test_a_failed_clock_lock_as_it_stands_waits_fifteen_minutes(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev, _ms = _clock_event(reg, rs, fill=False, placed=False)
    run(bot.scheduler_tick_guild(reg))
    ev = rs.events[ev.key]
    tried = ev.lock_tried_at
    assert ev.state == "open" and ev.lock_error == rc.LOCK_EMPTY_BOARD and len(bot.updates) == 1
    later = reg.now_local() + timedelta(minutes=10)
    monkeypatch.setattr(reg, "now_local", lambda: later)
    run(bot.scheduler_tick_guild(reg))
    ev = rs.events[ev.key]
    assert ev.lock_tried_at == tried and len(bot.updates) == 1, "not tried again within 15 minutes"
    ev.layout = [[m.display_name for m in _ms[:4]]]  # an officer places people meanwhile
    rs.save(ev, "board (test)")
    later = reg.now_local() + timedelta(minutes=6)  # 16 minutes after the failure
    monkeypatch.setattr(reg, "now_local", lambda: later)
    run(bot.scheduler_tick_guild(reg))
    ev = rs.events[ev.key]
    assert ev.state == "locked" and {p.signup_name for p in ev.seated()} == {m.display_name for m in _ms[:4]}, "tried again after 15"


def test_the_clocks_lock_with_auto_fill_on_passes_fill(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev, _ms = _clock_event(reg, rs, fill=True)
    seen = []

    async def lock_run(reg_, rs_, ev_, by="scheduler", fill=True):
        seen.append(fill)
        return "locked"
    monkeypatch.setattr(bot, "lock_run", lock_run)
    run(bot.scheduler_tick_guild(reg))
    assert seen == [True]


# ---------------------------------------------------------------- Auto Lock off: no lock time on the sheet or its DMs

def test_auto_lock_off_the_sheet_card_and_dms_say_an_officer_locks_it(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs, days=2)
    rc.reschedule(reg, rs, ev, timing={"auto_lock": False}, by="o")
    ev = rs.events[ev.key]
    team = rc.run_team(reg, ev)
    said = PAGE_WORDS["officer_locks"]
    embed, _view = sheet_message(reg, ev, team, bot.ico)
    assert said in embed.description and "locks <t:" not in embed.description
    card = "\n".join(str(c.content) for c in health_layout(reg, rs, ev, team, bot.ico).walk_children() if hasattr(c, "content"))
    assert said in card and "locks <t:" not in card
    team["open_dm"] = True
    run(bot.dm_on_open(reg, rs, ev, team))
    texts = [t for msgs in bot.dms.values() for t in msgs if isinstance(t, str)]
    assert texts and all(said in t and "The roster locks" not in t for t in texts)
    rc.reschedule(reg, rs, ev, timing={"auto_lock": True}, by="o")
    ev = rs.events[ev.key]
    embed, _view = sheet_message(reg, ev, rc.run_team(reg, ev), bot.ico)
    assert "locks <t:" in embed.description and said not in embed.description
