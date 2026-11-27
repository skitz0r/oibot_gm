"""The lock closes the sheet to members, not to officers (design 2026-11-27-officer-edits-after-lock): every officer edit
of a locked roster goes through ONE quiet path (rc.apply_layout_locked / RaidMixin.set_answer's locked branch) — nobody
is asked to confirm, DMed or sent a notice, no ask is created, confirmed people keep their confirmation, no automatic
fill ask goes out — while the lock itself and a member's own Can't make it keep today's fan-out. Members can no longer
change their answer after the lock, except to say they can't come. The bot is the real RaidMixin over test_raid_night's
recording fake of Discord; Registry and RaidStore are real (conftest). Puppets only, no real names."""
from __future__ import annotations

import asyncio

import pytest
from conftest import by_role, join, lock_with_board, open_test_run
from test_raid_night import NightBot, locked_ten

from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError


def run(coro):
    return asyncio.run(coro)


OFFICER = 990_000_000_000_000_001  # the officer pressing (not a member of the run)


def snapshot(reg, ev):
    """Everything a member could be told by: DM-able asks, notices and confirmation asks, per member."""
    return {m.discord_id: (len(m.placement_asks), [a.get("answer") for a in m.placement_asks], len(reg.notices(m.discord_id)))
            for m in reg.test_members()}


def assert_told_nobody(bot, reg, ev, before, *, closed=()):
    """No DM to anyone, no new notice, no ask created; only the people in `closed` had an open ask closed."""
    assert bot.dms == {}, f"nobody gets a DM, got {bot.dms}"
    after = snapshot(reg, ev)
    for uid, (n, answers, notices) in before.items():
        n2, answers2, notices2 = after[uid]
        assert notices2 == notices, f"{uid}: no notice"
        assert n2 == n, f"{uid}: no ask created"
        if uid not in closed:
            assert answers2 == answers, f"{uid}: their confirmation stands"
    assert not [a for a in ev.fill_asks if a.pending], "no fill ask goes out"


def bench_name(ev, seated):
    on = {m.display_name for m in seated}
    return next(s.display_name for s in ev.signups.values() if s.status == "in" and s.display_name not in on)


def asked_and_confirmed(reg, ev, seated):
    """Every rostered person holds the lock's Confirm ask; the first two said yes."""
    for m in seated:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    for m in seated[:2]:
        reg.answer_placement(m.discord_id, ev.team, True, m.display_name)


# ---------------------------------------------------------------- §4: the board's moves

def test_a_move_between_groups_tells_nobody_and_keeps_the_confirmation(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    asked_and_confirmed(reg, ev, seated)
    before = snapshot(reg, ev)
    who = seated[0].display_name
    line = run(bot.board_move(reg, rs, ev, who, (1, 0), by="Officer", by_id=OFFICER))
    assert who in ev.all_rosters[0].groups[1]
    assert_told_nobody(bot, reg, ev, before)
    assert rc.standing_yes(reg.members[seated[0].discord_id], ev) is not None, "a confirmed person keeps their confirmation"
    assert "keeps their seat" in line


def test_a_swap_tells_nobody(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    asked_and_confirmed(reg, ev, seated)
    before = snapshot(reg, ev)
    a, b = ev.all_rosters[0].groups[0][0], ev.all_rosters[0].groups[1][0]
    run(bot.board_move(reg, rs, ev, a, (1, 0), by="Officer", by_id=OFFICER))
    assert a in ev.all_rosters[0].groups[1] and b in ev.all_rosters[0].groups[0]
    assert_told_nobody(bot, reg, ev, before)


def test_a_substitute_seats_the_newcomer_without_an_ask_and_the_other_keeps_their_answer(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    before = snapshot(reg, ev)
    new = bench_name(ev, seated)
    gone = ev.all_rosters[0].groups[1][-1]
    gone_uid = rc.uid_of_name(ev, gone)
    run(bot.board_move(reg, rs, ev, new, (1, len(ev.all_rosters[0].groups[1]) - 1), by="Officer", by_id=OFFICER))
    assert ev.seat_of(new) and not ev.seat_of(gone)
    assert ev.signups[str(gone_uid)].status == "in", "taken off: their answer stays Join"
    assert rc.signup_by_name(ev, new).status == "in"
    assert_told_nobody(bot, reg, ev, before, closed={gone_uid})
    assert reg.members[gone_uid].placement_asks[-1]["answer"] == "released", "their open Confirm ask closes"
    assert not [a for a in reg.members[rc.uid_of_name(ev, new)].placement_asks if a.get("answer") is None], "no ask for the newcomer"
    assert len(ev.all_rosters[0].selected) == 10


def test_take_off_the_roster_puts_them_in_not_rostered_with_their_answer(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    before = snapshot(reg, ev)
    who = seated[-1]
    run(bot.board_move(reg, rs, ev, who.display_name, None, by="Officer", by_id=OFFICER))
    assert not ev.seat_of(who)
    assert ev.signups[str(who.discord_id)].status == "in"
    assert who.display_name in rc.board_names(ev), "they can be dragged back"
    assert_told_nobody(bot, reg, ev, before, closed={who.discord_id})
    assert ev.officer_open == 1, "the seat an officer opened is held from the automatic asks"


def test_a_bench_answer_can_be_put_on_the_roster_and_keeps_its_answer(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    off = [s for s in ev.signups.values() if not ev.seat_of(s.discord_id)][0]
    m = reg.members[off.discord_id]
    rc.set_signup(reg, rs, ev, m, None, "sub", source="test")
    run(bot.board_move(reg, rs, ev, seated[-1].display_name, None, by="Officer"))  # an open seat
    bot.dms.clear()
    before = snapshot(reg, ev)
    sg = ev.signups[str(m.discord_id)]
    run(bot.board_move(reg, rs, ev, sg.display_name, (1, 4), by="Officer", by_id=OFFICER))
    assert ev.seat_of(m) and ev.signups[str(m.discord_id)].status == "sub"
    assert_told_nobody(bot, reg, ev, before)


def test_the_quiet_path_keeps_the_roster_count_when_a_roster_empties(reg, rs):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool])  # 16 names: two rosters (10 + 6)
    assert len(ev.all_rosters) == 2
    layout = rc.board_layout(reg, ev)
    k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    emptied = [list(g) for g in layout[:k]] + [[] for _ in layout[k:]]
    rc.apply_layout_locked(reg, rs, ev, emptied, by="Officer")
    assert len(ev.all_rosters) == 2, "a split council's roster index stays valid"
    assert ev.all_rosters[1].selected == [] and len(rc.board_layout(reg, ev)) == 2 * k


def test_the_loot_council_hears_of_the_seat_from_its_own_roster(reg, rs):
    heard = []
    rc.ROSTER_LISTENERS.append(lambda gk, key, change: heard.append(change))
    try:
        ev = open_test_run(reg, rs)
        pool = reg.test_members()
        join(reg, rs, ev, pool)
        lock_with_board(reg, rs, ev, [m.display_name for m in pool])
        who = ev.all_rosters[1].selected[0]
        layout = [[n for n in g if n != who.signup_name] for g in rc.board_layout(reg, ev)]
        rc.apply_layout_locked(reg, rs, ev, layout, by="Officer")
    finally:
        rc.ROSTER_LISTENERS.pop()
    gone = [c for c in heard if c["removed"]]
    assert gone and gone[0]["roster_of"] == {str(who.discord_id): 1}


# ---------------------------------------------------------------- §4: Auto-fill, Clear, Undo, split, pins

def test_auto_fill_on_a_locked_roster_fills_open_seats_only(reg, rs):
    ev, seated = locked_ten(reg, rs)
    for m in seated[-2:]:
        rc.apply_layout_locked(reg, rs, ev, [[n for n in g if n != m.display_name] for g in rc.board_layout(reg, ev)], by="Officer")
    where = {n: gi for gi, g in enumerate(rc.board_layout(reg, ev)) for n in g}
    layout, added = rc.autofill_locked(reg, rs, ev)
    assert len(added) == 2
    after = {n: gi for gi, g in enumerate(layout) for n in g}
    assert all(after[n] == gi for n, gi in where.items()), "nobody rostered moves"
    assert len(layout) == len(rc.board_layout(reg, ev)), "the roster count never changes"
    assert all(len(g) <= 5 for g in layout)


def test_auto_fill_route_on_a_locked_roster_writes_through_the_quiet_path(reg, rs):
    from oibot_gm import event_board

    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    run(bot.board_move(reg, rs, ev, seated[-1].display_name, None, by="Officer"))
    bot.dms.clear()
    before = snapshot(reg, ev)
    af = event_board.autofill_of(bot)

    async def go():
        return await af.start(reg, rs, ev, by="Officer", by_id=OFFICER, via="web")
    assert run(go()) is None
    assert len(ev.all_rosters[0].selected) == 10
    assert_told_nobody(bot, reg, ev, before)
    assert ev.officer_open == 0


def test_auto_fill_refuses_a_full_locked_roster_in_its_sentence(reg, rs):
    from oibot_gm import event_board
    from oibot_gm.constants import PAGE_WORDS

    ev, seated = locked_ten(reg, rs)
    assert event_board.AutoFill().refusal(reg, ev) == PAGE_WORDS["autofill_full"]


def test_clear_stays_refused_after_the_lock_and_names_unlock(reg, rs):
    ev, seated = locked_ten(reg, rs)
    b = cq.clear_board_box(reg, rs, ev)
    assert b["verb"] is None and b["sentences"] == [cq.CLEAR_LOCKED] and "Unlock" in cq.CLEAR_LOCKED


def test_split_strategy_and_propose_stay_refused_and_name_unlock(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    ev, seated = locked_ten(reg, rs)
    assert cq.strategy_refusal(reg, ev) == cq.STRATEGY_LOCKED and "Unlock" in cq.STRATEGY_LOCKED
    assert cq.PROPOSE_LOCKED in configops.describe(reg, ConfigOp(op="run_propose", target=ev.key))


def test_undo_puts_back_the_officers_last_change_and_only_that(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    board = rc.board_layout(reg, ev)
    who = seated[-1]
    run(bot.board_move(reg, rs, ev, who.display_name, None, by="Officer", by_id=OFFICER))
    box = cq.undo_box_for(reg, ev, OFFICER)
    assert box is not None and box["sentences"] == cq.UNDO_LOCKED
    assert cq.undo_box_for(reg, ev, OFFICER + 1) is None, "another officer has no change of theirs to undo"
    before = snapshot(reg, ev)
    run(bot.undo_moves(reg, rs, ev, OFFICER, "Officer", rev=rc.undo_rev(reg, ev, OFFICER)))
    assert rc.board_layout(reg, ev) == board and ev.seat_of(who)
    assert_told_nobody(bot, reg, ev, before)
    assert cq.undo_box_for(reg, ev, OFFICER) is None, "one step"


def test_undo_is_refused_once_the_board_changed_since(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    run(bot.board_move(reg, rs, ev, seated[-1].display_name, None, by="Officer", by_id=OFFICER))
    run(bot.board_move(reg, rs, ev, seated[-2].display_name, None, by="Another", by_id=OFFICER + 1))
    assert cq.undo_box_for(reg, ev, OFFICER) is None
    with pytest.raises(rc.BoardChanged):
        run(bot.undo_moves(reg, rs, ev, OFFICER, "Officer"))


def test_a_pin_is_refused_after_the_lock(reg, rs):
    from oibot_gm import inbox

    ev, seated = locked_ten(reg, rs)
    with pytest.raises(RegistryError) as e:
        inbox.pin_write(reg, rs, ev, seated[0], "in", "Officer")
    assert str(e.value) == inbox.PIN_LOCKED


# ---------------------------------------------------------------- §4: an answer recorded for someone

def test_join_for_someone_who_never_answered_puts_them_in_not_rostered(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:4]
    join(reg, rs, ev, seated)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    late = next(m for m in pool if m not in seated)
    before = snapshot(reg, ev)
    run(bot.set_answer(reg, rs, ev, late, "in", None, by="Officer", by_id=OFFICER))
    assert ev.signups[str(late.discord_id)].status == "in" and not ev.seat_of(late), "the officer places them"
    assert late.display_name in rc.board_names(ev)
    assert_told_nobody(bot, reg, ev, before)


def test_bench_for_someone_rostered_is_the_answer_only(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    before = snapshot(reg, ev)
    run(bot.set_answer(reg, rs, ev, seated[0], "sub", None, by="Officer", by_id=OFFICER))
    assert ev.seat_of(seated[0]) and ev.signups[str(seated[0].discord_id)].status == "sub"
    assert_told_nobody(bot, reg, ev, before)


def test_no_thanks_for_someone_rostered_frees_the_seat_and_asks_nobody(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    rc.run_team(reg, ev)["autofill"] = True
    before = snapshot(reg, ev)
    who = seated[-1]
    run(bot.set_answer(reg, rs, ev, who, "out", None, by="Officer", by_id=OFFICER))
    assert not ev.seat_of(who) and ev.signups[str(who.discord_id)].status == "out"
    assert_told_nobody(bot, reg, ev, before, closed={who.discord_id})
    assert ev.officer_open == 1
    sent, _nd = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))
    assert sent == [], "the scheduler's automatic asks leave the officer's open seat alone"


def test_a_members_cant_make_it_still_frees_the_seat_and_asks_the_bench(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    rc.run_team(reg, ev)["autofill"] = True
    who = seated[-1]
    run(bot.set_answer(reg, rs, ev, who, "out", None, by=who.display_name, by_id=who.discord_id))
    assert not ev.seat_of(who) and ev.callouts, "the callout is recorded"
    assert ev.officer_open == 0
    assert [a for a in ev.fill_asks if a.pending], "a seat a member gives up is asked for as before"


def test_the_on_behalf_box_after_the_lock_says_nobody_gets_a_dm(reg, rs):
    ev, seated = locked_ten(reg, rs)
    late = next(m for m in reg.test_members() if not ev.seat_of(m))
    rc.set_signup(reg, rs, ev, late, None, "out", source="test")
    for key, m in (("join", late), ("bench", seated[0]), ("no_thanks", seated[1])):
        lines = cq.member_action_box(reg, ev, m, key)["sentences"]
        assert cq.QUIET_TOLD in lines and not any("Confirm" in x or "notice" in x for x in lines), (key, lines)


# ---------------------------------------------------------------- §4: role switches

def _seated_flex(reg, rs, ev, seated):
    """A rostered member whose character plays another spec, and that spec (a damage dealer's main given an offspec of
    another spec of its class when the puppets have none)."""
    for m in seated:
        specs = rc.specs_of(reg, m.main)
        if len(specs) > 1:
            return m, next(s for s in specs if s != m.main.spec)
    m = seated[-1]
    other = next(s for s in reg.profile.classes[m.main.cls] if s != m.main.spec)
    reg.set_spec(m.discord_id, m.main.name, m.main.spec, other)
    m = reg.members[m.discord_id]
    rc.set_signup(reg, rs, ev, m, None, "in", source="test")
    return m, other


def test_a_whole_event_switch_of_someone_rostered_changes_the_seat_quietly(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    m, spec = _seated_flex(reg, rs, ev, seated)
    before = snapshot(reg, ev)
    run(bot.set_answer(reg, rs, ev, m, "in", None, by="Officer", by_id=OFFICER, spec=spec))
    assert ev.seat_of(m)[1].spec == spec
    assert_told_nobody(bot, reg, ev, before)


def test_the_switch_box_after_the_lock_reads_aloud_and_is_a_switch(reg, rs):
    ev, seated = locked_ten(reg, rs)
    m, spec = _seated_flex(reg, rs, ev, seated)
    b = cq.switch_box(reg, ev, m, m.main.label, spec, [ev.instance], locked=True, swap=True)
    assert b["verb"] == "Switch" and cq.QUIET_TOLD in b["sentences"]


def test_a_per_raid_switch_on_a_locked_raid_changes_that_seat_and_tells_nobody(reg, rs):
    from test_per_raid_switch import evening, place_all, seat

    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")
    bot = NightBot(reg, rs)
    lead, fev, ms = evening(reg, rs)
    names = [m.display_name for m in ms]
    place_all(rs, lead, names)
    place_all(rs, fev, names)
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Officer", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    nyx = ms[0]
    assert seat(fev, "Nyx").spec == "Shadow"
    assert rc.raid_switch_options(reg, rs, fev, nyx), "the board's ⋮ offers Play as after the lock too"
    bot.dms.clear()
    notices = len(reg.notices(nyx.discord_id))
    b = cq.raid_switch_box(reg, rs, fev, nyx, "Nyx", "Holy")
    assert cq.QUIET_TOLD in b["sentences"] and "until the event starts" in b["sentences"][-1]
    run(bot.set_raid_switch(reg, rs, fev, nyx, "Nyx", "Holy", "Officer", by_id=OFFICER, via="web"))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert (seat(fev, "Nyx").spec, seat(fev, "Nyx").role) == ("Holy", "healer")
    assert seat(lead, "Nyx").spec == "Shadow", "their answer stays for the other raid"
    assert bot.dms == {} and len(reg.notices(nyx.discord_id)) == notices, "nobody is told"


# ---------------------------------------------------------------- §5: members after the lock

def test_a_members_own_join_or_bench_is_refused_after_the_lock(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    late = next(m for m in reg.test_members() if not ev.seat_of(m))
    for status in ("in", "sub"):
        with pytest.raises(ValueError) as e:
            run(bot.set_answer(reg, rs, ev, late, status, None, by=late.display_name, by_id=late.discord_id))
        assert str(e.value) == rc.LOCKED_JOIN


# ---------------------------------------------------------------- Unlock after quiet edits; old files

def test_unlock_after_quiet_edits_restores_the_board(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    off = seated[-1]
    run(bot.board_move(reg, rs, ev, off.display_name, None, by="Officer", by_id=OFFICER))
    roster = {n for g in rc.board_layout(reg, ev) for n in g}
    rc.unlock(reg, rs, ev, "Officer")
    ev = rs.events[ev.key]
    assert ev.state == "open" and {n for g in ev.layout for n in g} == roster
    assert ev.signups[str(off.discord_id)].status == "in", "taken off by an officer: back in Not placed with their answer"
    assert off.display_name not in roster
    assert ev.officer_open == 0 and ev.board_undo == {}


def test_an_event_locked_before_this_change_loads(reg, rs):
    ev, seated = locked_ten(reg, rs)
    raw = ev.model_dump()
    raw.pop("officer_open")
    raw.pop("board_undo")
    old = rc.RaidEvent.model_validate(raw)
    assert old.officer_open == 0 and old.board_undo == {}
