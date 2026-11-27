"""Fix round 1 of officer edits after the lock (review C1, I1, I2): a seat an officer opens on a locked roster is HELD
from the automatic asks — role-aware (an open tank seat held = no automatic ask at all, for the headcount and for the
tank minimum), never more holds than seats actually open (a held seat filled by any path stops being held), a seat a
member gives up afterwards is asked for exactly as before, held seats never read as "nobody left to ask", and the
person an officer took off is never asked automatically for it. Fill seats still asks for held seats. Puppets only."""
from __future__ import annotations

import asyncio

import pytest
from conftest import by_role
from test_raid_night import NightBot, locked_ten

from oibot_gm import raidcycle as rc

OFFICER = 990_000_000_000_000_001


def run(coro):
    return asyncio.run(coro)


def night(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    team = rc.run_team(reg, ev)
    team["autofill"] = True
    return bot, ev, seated, team


def of_role(reg, seated, role):
    return next(m for m in seated if by_role(reg, [m])[role])


def take_off(bot, reg, rs, ev, m):
    run(bot.board_move(reg, rs, ev, m.display_name, None, by="Officer", by_id=OFFICER))


def pending(ev):
    return [a for a in ev.fill_asks if a.pending]


def test_a_held_tank_seat_sends_no_automatic_ask_and_is_never_nobody_left(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    tank = of_role(reg, seated, "tank")
    take_off(bot, reg, rs, ev, tank)
    assert rc.needs(reg, ev, team)["roles"].get("tank"), "the tank minimum is short now"
    bot.dms.clear()
    sent, _ = run(bot.run_fill(reg, rs, ev, team))
    assert sent == [] and not pending(ev) and bot.dms == {}
    assert ev.fill_state != "exhausted" and not any("nobody left" in u for u in bot.updates)
    text = rc.fill_state_text(reg, ev)
    assert "Nobody left" not in text and rc.held_words(1) in text


def test_a_held_healer_and_a_held_dps_send_nothing(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    take_off(bot, reg, rs, ev, of_role(reg, seated, "healer"))
    dps = next(m for m in seated if by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"])
    take_off(bot, reg, rs, ev, dps)
    assert len(rc.held_seats(reg, ev)) == 2
    sent, _ = run(bot.run_fill(reg, rs, ev, team))
    assert sent == [] and ev.fill_state != "exhausted"
    assert rc.held_words(2) in rc.fill_state_text(reg, ev)


def test_one_held_seat_and_one_a_member_gives_up_asks_for_the_members_only(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    dps = next(m for m in seated if by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"])
    take_off(bot, reg, rs, ev, dps)
    tank = of_role(reg, seated, "tank")
    run(bot.set_answer(reg, rs, ev, tank, "out", None, by=tank.display_name, by_id=tank.discord_id))  # their own Can't make it
    asked = pending(ev)
    assert asked, "the member's seat is asked for as before"
    assert dps.discord_id not in {a.discord_id for a in asked}, "the person taken off is never asked automatically"
    assert len(rc.held_seats(reg, ev)) == 1


def test_the_person_taken_off_is_never_asked_for_their_seat(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    tank = of_role(reg, seated, "tank")
    take_off(bot, reg, rs, ev, tank)
    run(bot.run_fill(reg, rs, ev, team))
    assert tank.discord_id not in {a.discord_id for a in ev.fill_asks}


def test_a_held_seat_filled_by_fill_seats_stops_being_held_and_a_later_cant_is_asked_for(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    dps = next(m for m in seated if by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"])
    take_off(bot, reg, rs, ev, dps)
    sent, _ = run(bot.run_fill(reg, rs, ev, team, by="Officer", approved_by="Officer"))
    assert sent, "Fill seats still asks for a held seat"
    run(bot.answer_fill_for(reg, rs, ev, reg.members[sent[0].discord_id], True, "Officer", by_id=OFFICER))
    for a in pending(ev):
        a.answer = "no"
    assert rc.open_seat_count(reg, ev) == 0 and rc.held_seats(reg, ev) == []
    who = next(m for m in seated if ev.seat_of(m) and m is not dps and not (by_role(reg, [m])["tank"] or by_role(reg, [m])["healer"]))
    run(bot.set_answer(reg, rs, ev, who, "out", None, by=who.display_name, by_id=who.discord_id))
    sent, _ = run(bot.run_fill(reg, rs, ev, team))
    assert pending(ev), "a member's Can't make it is asked for after the held seat was filled"


def test_an_officers_no_thanks_holds_the_seat_too(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    tank = of_role(reg, seated, "tank")
    run(bot.set_answer(reg, rs, ev, tank, "out", None, by="Officer", by_id=OFFICER))
    assert len(rc.held_seats(reg, ev)) == 1
    sent, _ = run(bot.run_fill(reg, rs, ev, team))
    assert sent == [] and ev.fill_state != "exhausted"


def test_a_quiet_seat_shrinks_the_holds(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    dps = next(m for m in seated if by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"])
    take_off(bot, reg, rs, ev, dps)
    run(bot.board_move(reg, rs, ev, dps.display_name, (1, 4), by="Officer", by_id=OFFICER))
    assert rc.held_seats(reg, ev) == [] and ev.held_seats == []


# ---------------------------------------------------------------- I3: plain text's callout preview says what apply does

def test_plain_texts_callout_preview_says_the_notice_and_the_fill(reg, rs):
    from oibot_gm import configops, inbox
    from oibot_gm.configops import ConfigOp

    bot, ev, seated, team = night(reg, rs)
    who = seated[-1]
    d = configops.describe(reg, ConfigOp(op="run_answer", target=ev.key, member=who.display_name, value="out", reason="sick"))
    assert "nobody gets a DM" not in d, d
    assert inbox.action_consequence(reg, ev, who, "callout") in d and rc.refill_sentence(reg, ev) in d, d
    assert "sick" in d


# ---------------------------------------------------------------- I4: officer surfaces vs member surfaces after the lock

def officer_night(reg, rs):
    """A locked test event whose tester is the owner (an officer with a character who never answered)."""
    from conftest import OWNER, join, lock_with_board, open_test_run

    reg.add_character(OWNER, "Boss", "Bossa", "Warrior", "Arms", None, True)
    ev = open_test_run(reg, rs, test_by=OWNER)
    ms = reg.test_members()
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:4]  # one seat open
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    return ev, reg.members[OWNER], seated


def test_an_officer_records_their_own_answer_on_an_officer_surface_after_the_lock(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN
    from test_raid_night import H

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = NightBot(reg, rs)
    c = TestClient(web.create_app(bot))
    ev, me, seated = officer_night(reg, rs)
    assert "join" in {a["key"] for a in rc.member_actions(reg, ev, me)}, "offered, so it must work"
    r = c.post(f"/api/run/{ev.key}/set", json={"member": str(me.discord_id), "status": "in"}, headers=H())  # MCP set_answer
    assert r.status_code == 200, r.text
    assert ev.signups[str(me.discord_id)].status == "in" and not ev.seat_of(me), "Join waits in Not rostered to be placed"
    assert me.display_name in rc.board_names(ev)
    run(bot.board_move(reg, rs, ev, ev.signups[str(me.discord_id)].display_name, (1, 4), by="Boss", by_id=me.discord_id))
    assert ev.seat_of(me), "a never-answered officer reaches the locked roster without Unlock"
    assert bot.dms == {}


def test_the_on_behalf_verbs_work_for_the_officers_own_row_and_someone_who_never_answered(reg, rs):
    from oibot_gm import inbox

    bot = NightBot(reg, rs)
    ev, me, seated = officer_night(reg, rs)
    run(inbox.do_member_action(bot, reg, rs, ev, me, "bench", by="Boss", by_id=me.discord_id))
    assert ev.signups[str(me.discord_id)].status == "sub"
    run(inbox.do_member_action(bot, reg, rs, ev, me, "join", by="Boss", by_id=me.discord_id))
    assert ev.signups[str(me.discord_id)].status == "in" and not ev.seat_of(me)


def test_member_surfaces_refuse_every_answer_after_the_lock_officers_included(reg, rs):
    bot = NightBot(reg, rs)
    ev, me, seated = officer_night(reg, rs)
    for status in ("in", "sub"):  # the sheet's buttons: set_answer as themself, no officer surface
        with pytest.raises(ValueError, match="roster is locked"):
            run(bot.set_answer(reg, rs, ev, me, status, None, by="Boss", by_id=me.discord_id))
    assert str(me.discord_id) not in ev.signups or ev.signups[str(me.discord_id)].status not in ("in", "sub")


def test_the_me_page_refuses_join_bench_and_no_thanks_after_the_lock(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_member_inbox_web import SECRET, TOKEN, WebNightBot, as_user

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    c = TestClient(web.create_app(WebNightBot(reg, rs)))
    ev, me, seated = officer_night(reg, rs)
    for uid in (me.discord_id, seated[0].discord_id):
        h = as_user(c, uid)
        for status in ("in", "sub", "out"):
            r = c.post(f"/api/me/run/{ev.key}/answer", json={"status": status}, headers=h)
            assert r.status_code == 400 and r.json()["error"] == rc.LOCKED_JOIN, (uid, status, r.text)


# ---------------------------------------------------------------- the minor findings

def test_join_for_someone_says_what_can_be_undone(reg, rs):
    from oibot_gm import inbox

    bot, ev, seated, team = night(reg, rs)
    late = next(m for m in reg.test_members() if not ev.seat_of(m))
    rc.set_signup(reg, rs, ev, late, None, "out", source="test")
    lines = inbox.action_lines(reg, ev, late, "join", by_id=OFFICER)
    assert lines[-1] == "You can change their answer again until the event starts."


def test_reseat_parts_saves_nothing_when_nothing_changed(reg, rs, monkeypatch):
    bot, ev, seated, team = night(reg, rs)
    saves = []
    monkeypatch.setattr(rs, "save", lambda e, why: saves.append(why))
    assert rc.reseat_parts(reg, rs, ev, seated[0]) == [] and saves == []


def test_undo_brings_them_back_rostered_without_reopening_their_confirm_ask(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    who = seated[-1]
    take_off(bot, reg, rs, ev, who)
    assert reg.members[who.discord_id].placement_asks[-1]["answer"] == "released"
    run(bot.undo_moves(reg, rs, ev, OFFICER, "Officer"))
    assert ev.seat_of(who), "back on the roster"
    asks = reg.members[who.discord_id].placement_asks
    assert asks[-1]["answer"] == "released" and not [a for a in asks if a.get("answer") is None], "no ask reopened: nothing expires"


def test_plain_text_moves_and_boards_after_the_lock_record_the_officers_undo(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot, ev, seated, team = night(reg, rs)
    who = seated[-1]
    run(configops.apply_async(reg, ConfigOp(op="run_move", target=ev.key, member=who.display_name, value="bench"), "Officer", True,
                              bot=bot, by_id=OFFICER))
    assert not ev.seat_of(who) and rc.undo_record(reg, ev, OFFICER) is not None
    board = ", ".join(n for g in rc.board_layout(reg, ev) for n in g)
    run(configops.apply_async(reg, ConfigOp(op="run_board", target=ev.key, value=board + ", " + who.display_name), "Officer", True,
                              bot=bot, by_id=OFFICER + 1))
    assert ev.seat_of(who) and rc.undo_record(reg, ev, OFFICER + 1) is not None and rc.undo_record(reg, ev, OFFICER) is None


# ---------------------------------------------------------------- fix round 2: a hold is a seat IN A ROSTER

def split_night(reg, rs, ev=None):
    """Every puppet rostered on two rosters of a 10-man (10 + 6) — on `ev` (a later raid of a lineup), else a fresh test event."""
    from conftest import join, lock_with_board, open_test_run

    pool = reg.test_members()
    if ev is None:
        ev = open_test_run(reg, rs)
        join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool])
    assert [len(r.selected) for r in ev.all_rosters] == [10, 6]
    return ev


def take_off_roster_one_then_a_member_leaves_roster_two(bot, reg, rs, ev):
    a = ev.all_rosters[0].selected[-1].signup_name
    rc.apply_layout_locked(reg, rs, ev, [[n for n in g if n != a] for g in rc.board_layout(reg, ev)], by="Officer", by_id=OFFICER)
    assert [h["roster"] for h in rc.held_seats(reg, ev)] == [0]
    b = ev.all_rosters[1].selected[0]
    rc.free_seat(reg, rs, ev, b.discord_id, "callout")
    return b


def test_a_fill_yes_for_a_members_seat_lands_in_the_roster_they_left_never_the_held_seat(reg, rs):
    bot = NightBot(reg, rs)
    ev = split_night(reg, rs)
    b = take_off_roster_one_then_a_member_leaves_roster_two(bot, reg, rs, ev)
    sg = ev.signups[str(b.discord_id)]
    sg.status = "in"
    assert rc.seat_player(reg, ev, sg) == 1, "the yes takes the member's roster-2 seat"
    assert [len(r.selected) for r in ev.all_rosters] == [9, 6] and [h["roster"] for h in rc.held_seats(reg, ev)] == [0]


def test_an_officers_fill_seats_yes_may_take_a_held_seat_and_ends_that_hold(reg, rs):
    bot, ev, seated, team = night(reg, rs)  # one roster, full: the officer's take-off is the only open seat
    a = seated[-1].display_name
    take_off(bot, reg, rs, ev, seated[-1])
    sg = rc.signup_by_name(ev, a)
    assert rc.seat_player(reg, ev, sg) is None, "an automatic yes never takes a held seat"
    assert rc.seat_player(reg, ev, sg, held_ok=True) == 0 and rc.held_seats(reg, ev) == []


def test_the_same_on_a_later_raid_of_a_lineup(reg, rs):
    from test_per_raid_switch import evening

    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")
    lead, fev, _ms = evening(reg, rs)
    for m in reg.test_members():
        rc.set_signup(reg, rs, rs.events[lead.key], m, None, "in", source="test")
    fev = rs.events[fev.key]
    lock_names = [m.display_name for m in reg.test_members()]
    from conftest import lock_with_board

    lock_with_board(reg, rs, fev, lock_names)
    bot = NightBot(reg, rs)
    b = take_off_roster_one_then_a_member_leaves_roster_two(bot, reg, rs, fev)
    sg = fev.signups[str(b.discord_id)]
    sg.status = "in"
    assert rc.seat_player(reg, fev, sg) == 1 and [h["roster"] for h in rc.held_seats(reg, fev)] == [0]


def test_a_dps_hold_never_evaporates_when_tanks_and_healers_are_short(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    tank = of_role(reg, seated, "tank")
    dps = next(m for m in seated if by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"])
    take_off(bot, reg, rs, ev, dps)  # the officer's dps seat: held
    rc.free_seat(reg, rs, ev, tank.discord_id, "callout")  # a tank short elsewhere, and its seat free
    rc.seat_player(reg, ev, rc.signup_by_name(ev, bench_dps(reg, ev)))  # the tank's seat taken by a dps: tank short, one seat open
    assert rc.open_seat_count(reg, ev) == 1 and rc.needs(reg, ev, team)["roles"].get("tank")
    assert len(rc.held_seats(reg, ev)) == 1, "the held seat stays held though the tank minimum is short"
    sent, _ = run(bot.run_fill(reg, rs, ev, team))
    assert sent == [] and rc.held_words(1) in rc.fill_state_text(reg, ev)


def bench_dps(reg, ev):
    return next(s.display_name for s in ev.signups.values() if s.status == "in" and not ev.seat_of(s.discord_id)
                and s.role in ("melee", "ranged"))


def test_the_person_taken_off_is_eligible_for_a_seat_a_member_gives_up(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    dps = next(m for m in seated if by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"])
    take_off(bot, reg, rs, ev, dps)
    for s in ev.signups.values():  # nobody else off the roster: only the person taken off could come
        if s.status in ("in", "sub") and not ev.seat_of(s.discord_id) and s.discord_id != dps.discord_id:
            s.status = "out"
    rs.save(ev, "t")
    other = next(m for m in seated if ev.seat_of(m) and m is not dps and (by_role(reg, [m])["melee"] or by_role(reg, [m])["ranged"]))
    run(bot.set_answer(reg, rs, ev, other, "out", None, by=other.display_name, by_id=other.discord_id))
    assert dps.discord_id in {a.discord_id for a in pending(ev)} and ev.fill_state != "exhausted"


def test_all_held_leaves_no_stale_asking_state(reg, rs):
    bot, ev, seated, team = night(reg, rs)
    take_off(bot, reg, rs, ev, of_role(reg, seated, "tank"))
    ev.fill_state = "asking"  # an earlier ask, closed since
    run(bot.run_fill(reg, rs, ev, team))
    assert ev.fill_state == "idle" and "waiting for answers" not in rc.fill_state_text(reg, ev)
    assert rc.held_words(1) in rc.fill_state_text(reg, ev)
