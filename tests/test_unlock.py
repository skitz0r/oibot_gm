"""Issue 14, Task 4: Unlock in the engine. `rc.unlock` puts a locked event back into open signup WITH ITS ROSTERS ON THE
BOARD — every locked raid of it, group for group, every name a hand placement — and switches Auto Lock off so the clock
leaves it alone. Confirmation asks are kept (answers stay, open asks stay open, nothing expires while open); fill asks
are withdrawn; the lock's Not rostered notices are taken back. A seat lost after the lock to silence or an officer's
board move comes back to Join; a Can't make it or a callout stays out. The next lock reconciles the asks: a yes is not
asked again, an open ask is kept, a newcomer is asked, someone left off has their ask closed and one Not rostered
notice. `RaidMixin.unlock_run` is the surfaces' verb; a person's lock refuses an empty board instead of failing."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import join, open_test_run
from test_auto_lock import _later
from test_bench_last import DPS, TANK, base_eight, people, schedule_event
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import NightBot, card_texts, run

from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError


def lock(bot, reg, rs, ev, **kw):
    return run(bot.lock_run(reg, rs, ev, by="Jon", **kw))


def groups_of(ev) -> list[list[str]]:
    return [list(g) for r in ev.all_rosters for g in r.groups]


def locked_event(reg, rs, *, placed: int = 8, joined_extra: int = 2, fill: bool = False):
    """A schedule's event: 8 + `joined_extra` joined, `placed` of them on the board, locked by Jon (as it stands, or
    filled with `fill`; the confirmations go out to everyone rostered)."""
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg) + people(reg, joined_extra, DPS)
    join(reg, rs, ev, ms)
    ns = [m.display_name for m in ms]
    ev.layout = [ns[:min(placed, 5)], ns[5:placed]]
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=fill)
    ev = rs.events[ev.key]
    assert ev.state == "locked" and rc.run_confirms(reg, ev)
    return bot, ev, ms


def ask_of(reg, ev, m) -> dict | None:
    return next((a for a in reversed(reg.members[m.discord_id].placement_asks) if a["roster"] == ev.team), None)


def not_rostered(reg, ev, m) -> list[dict]:
    return [n for n in reg.members[m.discord_id].notices if n.get("kind") == "not_rostered" and n.get("run") == ev.key]


# ---------------------------------------------------------------- the board after an unlock is the roster

def test_the_board_after_an_unlock_is_the_locked_roster_every_name_by_hand(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    roster = groups_of(ev)
    done = rc.unlock(reg, rs, ev, "Jon")
    ev = rs.events[ev.key]
    assert ev.state == "open" and not ev.rosters and ev.locked_at is None and ev.confirm_by is None and ev.lock_error is None
    assert [g for g in rc.board_layout(reg, ev) if g] == [g for g in roster if g], "group for group"
    placed = {n for g in roster for n in g}
    assert set(ev.placed_by) == placed and set(ev.placed_by.values()) == {0} and ev.auto_placed == []
    assert not rc.auto_lock(reg, ev), "Auto Lock goes off: the clock leaves it alone"
    assert done.raids == [ev.key] and done.asks_kept == len(placed) and done.fills_withdrawn == 0
    assert {s.display_name for s in ev.by_status("in")} == {m.display_name for m in ms}, "the unrostered are joiners in Not placed"


def test_a_relock_as_it_stands_reproduces_the_roster_exactly(reg, rs):
    bot, ev, _ms = locked_event(reg, rs)
    roster = groups_of(ev)
    rc.unlock(reg, rs, ev, "Jon")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    ev = rs.events[ev.key]
    assert ev.state == "locked" and groups_of(ev) == roster, "name for name, group for group"


def test_a_relock_with_auto_fill_keeps_every_rostered_name_where_it_was(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    roster = groups_of(ev)
    rc.unlock(reg, rs, ev, "Jon")
    lock(bot, reg, rs, rs.events[ev.key], fill=True)
    ev = rs.events[ev.key]
    after = groups_of(ev)
    for gi, g in enumerate(roster):
        assert set(g) <= set(after[gi]), "a hand placement stays in its group"


def test_a_lineup_unlocks_whole(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    lock(bot, reg, rs, rs.events[lead.key], fill=False)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == fev.state == "locked"
    lead_roster, fev_roster = groups_of(lead), groups_of(fev)
    done = rc.unlock(reg, rs, fev, "Jon")  # any raid of it stands for the first
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == fev.state == "open" and sorted(done.raids) == sorted([lead.key, fev.key])
    assert [g for g in rc.board_layout(reg, lead) if g] == [g for g in lead_roster if g]
    assert [g for g in rc.board_layout(reg, fev) if g] == [g for g in fev_roster if g]
    assert not rc.auto_lock(reg, lead) and not rc.auto_lock(reg, fev)
    lock(bot, reg, rs, lead, fill=False)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert groups_of(lead) == lead_roster and groups_of(fev) == fev_roster, "the relock rebuilds both"


# ---------------------------------------------------------------- the clock leaves it alone; nothing expires

def test_the_scheduler_does_not_lock_an_unlocked_event_again(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs, days=0, soft=6, hard=4, confirm=1)  # starts in 3 h: its lock time has passed
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    ev = rs.events[ev.key]
    ev.layout = [[m.display_name for m in ms[:5]]]
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    rc.unlock(reg, rs, rs.events[ev.key], "Jon")
    ev = rs.events[ev.key]
    asks = {m.discord_id: dict(ask_of(reg, ev, m)) for m in ms[:5]}
    later = reg.now_local() + timedelta(hours=2)  # past the old confirm deadline, before the start
    monkeypatch.setattr(reg, "now_local", lambda: later)
    _later(monkeypatch, later)  # the clock expiry reads (rc's datetime.now), not only the scheduler's
    run(bot.scheduler_tick_guild(reg))
    ev = rs.events[ev.key]
    assert ev.state == "open" and not ev.rosters, "no lock by the clock a minute later"
    for m in ms[:5]:
        a = ask_of(reg, ev, m)
        assert a["answer"] is None and a["asked_at"] == asks[m.discord_id]["asked_at"], "the ask stays open"


def test_no_confirmation_expires_while_the_event_is_open(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    rc.unlock(reg, rs, ev, "Jon")
    ev = rs.events[ev.key]
    ev.confirm_by = (datetime.now(ev.start.tzinfo) - timedelta(minutes=5)).isoformat()  # even a stale deadline
    assert rc.expire_confirmation_asks(reg, rs, ev) == []
    assert rc.unasked_seats(reg, ev) == [] and rc.remind_refusal(reg, ev) is not None, "no reminder on an open event"
    assert all(ask_of(reg, ev, m)["answer"] is None for m in ms[:8])
    assert run(bot.recover_lock(reg, rs, ev)) == 0


# ---------------------------------------------------------------- who comes back

def test_silence_and_a_board_move_come_back_a_cant_and_a_callout_stay_out(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    silent, moved, cant, called = ms[0], ms[1], ms[2], ms[3]
    rc.free_seat(reg, rs, ev, silent.discord_id, "no-confirm")
    rc.free_seat(reg, rs, ev, moved.discord_id, "moved to bench")
    run(bot.answer_placement_for(reg, cant.discord_id, ev.team, False, cant.display_name, by_id=cant.discord_id))
    run(bot.call_out(reg, rs, rs.events[ev.key], called, "away", by=called.display_name, by_id=called.discord_id))
    ev = rs.events[ev.key]
    assert all(ev.signups[str(m.discord_id)].status == "out" for m in (silent, moved, cant, called))
    done = rc.unlock(reg, rs, ev, "Jon")
    ev = rs.events[ev.key]
    st = {m.display_name: ev.signups[str(m.discord_id)].status for m in (silent, moved, cant, called)}
    assert st == {silent.display_name: "in", moved.display_name: "in", cant.display_name: "out", called.display_name: "out"}
    assert sorted(done.returned) == sorted([silent.display_name, moved.display_name])
    placed = {n for g in rc.board_layout(reg, ev) for n in g}
    assert silent.display_name not in placed and moved.display_name not in placed, "back as joiners in Not placed"


def test_the_locks_not_rostered_notices_are_taken_back(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    left = ms[8:]
    assert all(len(not_rostered(reg, ev, m)) == 1 for m in left)
    done = rc.unlock(reg, rs, ev, "Jon")
    assert done.notices == len(left) and all(not not_rostered(reg, ev, m) for m in left)


# ---------------------------------------------------------------- a confirmation answered while open

def test_a_cant_while_open_takes_the_name_off_the_board_and_a_yes_is_kept(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    rc.unlock(reg, rs, ev, "Jon")
    yes, no = ms[0], ms[1]
    run(bot.answer_placement_for(reg, yes.discord_id, ev.team, True, yes.display_name, by_id=yes.discord_id))
    run(bot.answer_placement_for(reg, no.discord_id, ev.team, False, no.display_name, by_id=no.discord_id))
    ev = rs.events[ev.key]
    assert ask_of(reg, ev, yes)["answer"] == "yes" and ask_of(reg, ev, no)["answer"] == "no"
    placed = {n for g in rc.board_layout(reg, ev) for n in g}
    assert yes.display_name in placed and no.display_name not in placed
    assert ev.signups[str(no.discord_id)].status == "out" and ev.signups[str(no.discord_id)].source == "declined"
    assert ev.state == "open"


# ---------------------------------------------------------------- the next lock reconciles

def test_the_next_lock_reconciles_the_asks(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    confirmed, kept, gone_yes, gone_open = ms[0], ms[1], ms[2], ms[3]
    newcomer = ms[8]
    for m in (confirmed, gone_yes):
        run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, m.display_name, by_id=m.discord_id))
    kept_ask = dict(ask_of(reg, ev, kept))
    dms_before = {m.discord_id: len(bot.dms.get(m.discord_id, [])) for m in ms}
    rc.unlock(reg, rs, rs.events[ev.key], "Jon")
    ev = rs.events[ev.key]
    lay = [[n for n in g if n not in (gone_yes.display_name, gone_open.display_name)] for g in ev.layout]
    lay[-1].append(newcomer.display_name)
    ev.layout = lay  # the officer's moves while open
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    ev = rs.events[ev.key]
    seated = {p.signup_name for p in ev.seated()}
    assert newcomer.display_name in seated and gone_yes.display_name not in seated and gone_open.display_name not in seated
    # confirmed: not asked again
    assert ask_of(reg, ev, confirmed)["answer"] == "yes" and len(bot.dms.get(confirmed.discord_id, [])) == dms_before[confirmed.discord_id]
    # an open ask is kept, its card redrawn (never a new DM)
    a = ask_of(reg, ev, kept)
    assert a["answer"] is None and a["asked_at"] == kept_ask["asked_at"]
    assert len(bot.dms.get(kept.discord_id, [])) == dms_before[kept.discord_id]
    assert bot.cards[kept_ask["dm"][1]].edits, "the kept card shows the new roster"
    # a newcomer is asked
    assert ask_of(reg, ev, newcomer)["answer"] is None and len(bot.dms[newcomer.discord_id]) == dms_before[newcomer.discord_id] + 1
    # left off: the open ask closed, the yes lapsed; each card edited; one Not rostered notice each
    assert ask_of(reg, ev, gone_open)["answer"] == "unrostered"
    assert ask_of(reg, ev, gone_yes).get("lapsed_at") and rc.standing_yes(reg.members[gone_yes.discord_id], ev) is None
    for m in (gone_open, gone_yes):
        assert len(not_rostered(reg, ev, m)) == 1
        assert any("locked again without you" in t for t in card_texts(bot.cards[ask_of(reg, ev, m)["dm"][1]]))
    # a third lock after another unlock writes no second notice and asks nobody twice
    rc.unlock(reg, rs, ev, "Jon")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    ev = rs.events[ev.key]
    for m in (gone_open, gone_yes):
        assert len(not_rostered(reg, ev, m)) == 1
    assert len(bot.dms.get(confirmed.discord_id, [])) == dms_before[confirmed.discord_id]


# ---------------------------------------------------------------- refusals

def test_unlock_refusals_one_sentence_each(reg, rs):
    bot, ev, _ms = locked_event(reg, rs)
    assert rc.unlock_refusal(reg, rs, ev) is None
    assert rc.unlock_refusal(reg, rs, ev, at=ev.start) == rc.UNLOCK_STARTED
    d = rs.store.root / rs.key / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ev.key}.json").write_text("{}")
    assert rc.unlock_refusal(reg, rs, ev) == rc.UNLOCK_LOOT
    with pytest.raises(RegistryError, match="loot council"):
        rc.unlock(reg, rs, ev, "Jon")
    (d / f"{ev.key}.json").unlink()
    opened = open_test_run(reg, rs)
    assert rc.unlock_refusal(reg, rs, opened) == rc.UNLOCK_OPEN
    rc.end_run(reg, rs, ev, "t")
    assert rc.unlock_refusal(reg, rs, ev) == rc.UNLOCK_DONE
    opened.state = "cancelled"
    assert rc.unlock_refusal(reg, rs, opened) == rc.UNLOCK_CANCELLED
    for s in (rc.UNLOCK_OPEN, rc.UNLOCK_DONE, rc.UNLOCK_CANCELLED, rc.UNLOCK_STARTED, rc.UNLOCK_LOOT, rc.UNLOCK_SENDING):
        assert s.endswith(".") and s.count(".") == 1 and len(s.split()) <= 20


# ---------------------------------------------------------------- an event locked before this change

def test_an_event_locked_before_this_change_unlocks_the_same_way(reg, rs):
    """What an old file really lacks: the lock line's id, the lock's fill choice, the seats at the flip, the played-as
    specs (the keys are absent from the run file), and the stamped Auto Lock settings on its run dict."""
    bot, ev, _ms = locked_event(reg, rs, fill=True)
    roster = groups_of(ev)
    raw = ev.model_dump(mode="json")
    for k in ("lock_line", "lock_fill", "lock_seats", "played_as"):
        raw.pop(k, None)
    rs.events[ev.key] = old = rc.RaidEvent.model_validate(raw)
    team = reg.config.team(old.team)
    for k in ("auto_lock", "auto_lock_fill"):
        team.pop(k, None)
    assert old.lock_line is None and old.lock_fill is None and rc.auto_lock(reg, old), "read as an old file reads"
    run(bot.unlock_run(reg, rs, old, "Jon"))
    ev = rs.events[ev.key]
    assert ev.state == "open" and [g for g in rc.board_layout(reg, ev) if g] == [g for g in roster if g]
    assert not rc.auto_lock(reg, ev), "Auto Lock off, though the run dict never had the key"
    lock(bot, reg, rs, ev, fill=False)
    assert groups_of(rs.events[ev.key]) == roster


# ---------------------------------------------------------------- the surfaces' verb

class Gone:
    """A channel whose messages can be fetched and deleted (or are already gone)."""

    def __init__(self, cid, have=()):
        self.id, self.have, self.deleted = cid, set(have), []

    async def fetch_message(self, mid):
        if mid not in self.have:
            raise LookupError("gone")
        ch = self

        class M:
            async def delete(self_inner):
                ch.deleted.append(mid)
        return M()

    async def send(self, *a, **k):
        raise AssertionError("an unlock posts nothing new here")


def test_unlock_run_takes_down_the_lock_cards_and_line_withdraws_fill_asks_and_dms_nobody(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    officers, sig = Gone(4242, have={777}), Gone(5151, have={888})
    bot.channels[officers.id], bot.channels[sig.id] = officers, sig
    ev.cards.update({"lock:0": 777, "lock:1": 778})  # one card still there, one already deleted by hand
    ev.cards_channel_id, ev.lock_line = officers.id, [sig.id, 888]
    ask = rc.FillAsk(discord_id=ms[9].discord_id, display_name=ms[9].display_name, kind="sub", role="dps", character=ms[9].main.label,
                     spec=ms[9].main.spec, reason="short 1")
    ev.fill_asks.append(ask)
    rs.save(ev, "a fill ask (test)")
    dms = sum(len(v) for v in bot.dms.values())
    line = run(bot.unlock_run(reg, rs, rs.events[ev.key], "Jon", by_id=1))
    ev = rs.events[ev.key]
    assert ev.state == "open" and "Nobody was DMed" in line
    assert officers.deleted == [777] and sig.deleted == [888], "the cards and the line come down; a gone message fails nothing"
    assert not any(k.startswith("lock:") for k in ev.cards) and ev.lock_line is None
    assert ev.fill_asks[-1].answer == "withdrawn" and ev.fill_asks[-1].answered_by == "Jon"
    assert sum(len(v) for v in bot.dms.values()) == dms, "nobody gets a DM"
    assert sum(u.startswith("🔓 unlocked by Jon") for u in bot.updates) == 1, "one thread line"
    assert sum("unlocked" in text for _lvl, text in bot.ops.lines) == 1, "one audit line"
    with pytest.raises(RegistryError, match="nothing to unlock"):
        run(bot.unlock_run(reg, rs, ev, "Jon"))


# ---------------------------------------------------------------- a person's lock refuses instead of failing

def test_a_persons_lock_on_a_board_emptied_since_the_box_is_refused_not_failed(reg, rs):
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg)
    join(reg, rs, ev, ms)
    ev.layout = [[ms[0].display_name]]
    rs.save(ev, "board (test)")
    assert rc.lock_empty_refusal(reg, rs, rs.events[ev.key]) is None  # the box offered the lock as it stands
    ev = rs.events[ev.key]
    ev.layout = None  # the placement is removed before the press lands
    rs.save(ev, "board emptied (test)")
    with pytest.raises(RegistryError, match="Nobody is on the board"):
        lock(bot, reg, rs, rs.events[ev.key], fill=False, by_person=True)
    ev = rs.events[ev.key]
    assert ev.state == "open" and ev.lock_error is None and not bot.updates, "no failed lock, no footer, no thread line"
    lock(bot, reg, rs, ev, fill=False)  # the scheduler keeps the failure path
    assert rs.events[ev.key].lock_error == rc.LOCK_EMPTY_BOARD


# ---------------------------------------------------------------- fix round 1: specs, the box, the reminder, cards

def seats_of(ev) -> list[list[tuple]]:
    """Every roster's groups as (name, character, spec, role), group for group."""
    out = []
    for r in ev.all_rosters:
        by = {p.signup_name: p for p in r.selected}
        out += [[(n, by[n].character, by[n].spec, by[n].role) for n in g] for g in r.groups]
    return out


def test_an_offspec_seat_keeps_its_spec_through_an_unlock_and_a_relock_as_it_stands(reg, rs):
    """The reviewer's P5: two tanks and eight Retribution / Holy paladins, locked with auto-fill — the solve rosters two
    of them as Holy healers. After the unlock the board shows them as Holy; a relock as it stands rosters them so."""
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = people(reg, 2, TANK) + people(reg, 8, ("Paladin", "Retribution"), off="Holy")
    join(reg, rs, ev, ms)
    lock(bot, reg, rs, rs.events[ev.key], fill=True)
    ev = rs.events[ev.key]
    before = seats_of(ev)
    healers = [x for g in before for x in g if x[3] == "healer"]
    assert healers and all(x[2] == "Holy" for x in healers), before
    rc.unlock(reg, rs, ev, "Jon")
    ev = rs.events[ev.key]
    shown = {p.signup_name: p for r in rc.board_rosters(reg, ev, rc.board_layout(reg, ev), rc.run_size(reg, ev)) for p in r.selected}
    assert all(shown[n].spec == "Holy" and shown[n].role == "healer" for n, *_ in healers), "the board shows them as rostered"
    lock(bot, reg, rs, ev, fill=False)
    after = rs.events[ev.key]
    assert seats_of(after) == before, "names, groups, characters and specs"
    assert set(after.all_rosters[0].spec_switches) >= {n for n, *_ in healers}, "the lock card still says they play an offspec"
    rc.unlock(reg, rs, after, "Jon")  # and again: the second unlock reads the same specs back
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    assert seats_of(rs.events[ev.key]) == before


def test_two_rosters_filled_then_unlocked_relock_as_they_stood(reg, rs):
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg) + base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, ms)
    ev = rs.events[ev.key]
    k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
    ev.layout = [[ms[0].display_name]] + [[] for _ in range(k - 1)] + [[ms[1].display_name]]  # someone in roster 2: two built
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=True)
    ev = rs.events[ev.key]
    before = seats_of(ev)
    assert len(ev.all_rosters) == 2
    rc.unlock(reg, rs, ev, "Jon")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    assert seats_of(rs.events[ev.key]) == before


def test_a_lineup_filled_then_unlocked_relocks_as_it_stood(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    lock(bot, reg, rs, lead, fill=True)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == fev.state == "locked"
    a, b = seats_of(lead), seats_of(fev)
    rc.unlock(reg, rs, lead, "Jon")
    lock(bot, reg, rs, rs.events[lead.key], fill=False)
    assert seats_of(rs.events[lead.key]) == a and seats_of(rs.events[fev.key]) == b


def test_the_lock_box_after_an_unlock_counts_only_who_this_lock_asks(reg, rs):
    """The reviewer's P7: confirmed people are not asked again, so the box says nobody new is asked; a newcomer is
    counted; someone left off holding a yes has their ask close — said in the box."""
    from oibot_gm import consequences as cq

    bot, ev, ms = locked_event(reg, rs, placed=8, joined_extra=4)  # 8 rostered, 4 waiting: the board keeps open slots
    for m in ms[:8]:
        run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, m.display_name, by_id=m.discord_id))
    rc.unlock(reg, rs, rs.events[ev.key], "Jon")
    ev = rs.events[ev.key]
    b = cq.lock_box(reg, ev, rs)
    assert b["sentences"][1] == "Only the 8 on the board are rostered, and nobody new is asked to confirm.", b
    gone, new = ms[0], ms[9]
    ev.layout = [[n for n in g if n != gone.display_name] for g in ev.layout]
    ev.layout[-1].append(new.display_name)
    rs.save(ev, "board (test)")
    ev = rs.events[ev.key]
    b = cq.lock_box(reg, ev, rs)
    assert b["sentences"][1] == f"Only the 8 on the board are rostered, and 1 newly rostered gets {cq.CONFIRM_DM}.", b
    assert b["sentences"][2] == f"{gone.display_name} is left off the roster, so their Confirm ask closes.", b
    f = cq.lock_box(reg, ev, rs, fill=True)
    assert f"Everyone rostered who wasn't asked before gets {cq.CONFIRM_DM}." in f["sentences"], f
    dms = sum(len(v) for v in bot.dms.values())
    lock(bot, reg, rs, ev, fill=False)
    assert sum(len(v) for v in bot.dms.values()) == dms + 1, "the box's count is the lock's: one DM, to the newcomer"
    assert ask_of(reg, rs.events[ev.key], gone).get("lapsed_at")


def test_no_signup_reminder_dm_comes_from_an_unlock(reg, rs, monkeypatch):
    """Locked before its reminder time, unlocked after it: the scheduler's next minute nudges nobody."""
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs, days=1, soft=20, hard=10, confirm=5)  # starts in 27 h: the reminder is 7 h away
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms[:4])
    ev = rs.events[ev.key]
    ev.layout = [[m.display_name for m in ms[:4]]]
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
    assert not rs.events[ev.key].health_posted
    rc.unlock(reg, rs, rs.events[ev.key], "Jon")
    nudged = []
    real = bot.post_health

    async def spy(reg_, rs_, ev_, ch, nudge, early=False):
        nudged.append(nudge)
        return await real(reg_, rs_, ev_, ch, nudge, early)
    monkeypatch.setattr(bot, "post_health", spy)
    later = reg.now_local() + timedelta(hours=8)  # past the reminder time, before the old lock time
    monkeypatch.setattr(reg, "now_local", lambda: later)
    dms = sum(len(v) for v in bot.dms.values())
    run(bot.scheduler_tick_guild(reg))
    assert True not in nudged and sum(len(v) for v in bot.dms.values()) == dms, "nobody DMed"
    assert rs.events[ev.key].state == "open"


def test_unlock_run_redraws_the_open_confirm_cards_clears_remind_and_takes_back_released_notices(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    silent, waiting = ms[0], ms[1]
    rc.free_seat(reg, rs, ev, silent.discord_id, "no-confirm")
    assert any(n["kind"] == "released" and n["run"] == ev.key for n in reg.members[silent.discord_id].notices)
    ev = rs.events[ev.key]
    ev.reminded_at, ev.reminded_by = rc.now(), "Jon"
    rs.save(ev, "reminded (test)")
    card = bot.cards[ask_of(reg, ev, waiting)["dm"][1]]
    dms = sum(len(v) for v in bot.dms.values())
    run(bot.unlock_run(reg, rs, rs.events[ev.key], "Jon"))
    ev = rs.events[ev.key]
    assert any("open for signup again" in t and "still counts" in t for t in card_texts(card)), "edited in place"
    assert sum(len(v) for v in bot.dms.values()) == dms, "never a new DM"
    assert ev.reminded_at is None and ev.reminded_by is None
    assert not any(n["kind"] == "released" and n["run"] == ev.key for n in reg.members[silent.discord_id].notices)
    assert ev.signups[str(silent.discord_id)].status == "in"


def test_an_unlock_while_the_lock_is_still_sending_is_refused(reg, rs):
    bot, ev, _ms = locked_event(reg, rs)
    bot._lock_posting().add((reg.key, ev.key))
    with pytest.raises(RegistryError, match="still sending"):
        run(bot.unlock_run(reg, rs, ev, "Jon"))
    assert rs.events[ev.key].state == "locked"
    bot._lock_posting().discard((reg.key, ev.key))
    run(bot.unlock_run(reg, rs, rs.events[ev.key], "Jon"))
    assert rs.events[ev.key].state == "open"


# ---------------------------------------------------------------- fix round 2

def _paladins_unlocked(reg, rs):
    """P5 again: the fill lock rosters two Retribution / Holy paladins as Holy; then unlocked. → bot, event, a Holy-seated name."""
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = people(reg, 2, TANK) + people(reg, 8, ("Paladin", "Retribution"), off="Holy")
    join(reg, rs, ev, ms)
    lock(bot, reg, rs, rs.events[ev.key], fill=True)
    ev = rs.events[ev.key]
    holy = next(p.signup_name for r in ev.all_rosters for p in r.selected if p.spec == "Holy")
    rc.unlock(reg, rs, ev, "Jon")
    return bot, rs.events[ev.key], holy


def test_an_offspec_seat_whose_member_now_signs_up_for_that_spec_keeps_its_place(reg, rs):
    bot, ev, holy = _paladins_unlocked(reg, rs)
    sg = rc.signup_by_name(ev, holy)
    sg.spec, sg.offspec, sg.role = "Holy", "Retribution", "healer"  # their own spec for the night is now the seat's
    rs.save(ev, "respec (test)")
    ev = rs.events[ev.key]
    assert holy in {n for g in ev.layout for n in g}, "agrees with the seat: the place stands"
    assert holy not in ev.played_as, "their own spec plays it now"
    lock(bot, reg, rs, ev, fill=False)
    seat = next(p for r in rs.events[ev.key].all_rosters for p in r.selected if p.signup_name == holy)
    assert seat.spec == "Holy" and seat.role == "healer"


def test_an_offspec_seat_whose_member_changes_to_another_spec_leaves_the_board(reg, rs):
    bot, ev, holy = _paladins_unlocked(reg, rs)
    sg = rc.signup_by_name(ev, holy)
    sg.spec, sg.offspec, sg.role = "Protection", "Retribution", "tank"
    rs.save(ev, "respec (test)")
    ev = rs.events[ev.key]
    assert holy not in {n for g in ev.layout or [] for n in g} and holy not in ev.played_as, "a different spec: Not placed, as now"


def test_the_kept_confirm_card_shows_the_spec_they_were_rostered_on(reg, rs):
    from oibot_gm.raid_views import confirm_open_layout

    bot, ev, holy = _paladins_unlocked(reg, rs)
    sg = rc.signup_by_name(ev, holy)
    asked = []
    confirm_open_layout(reg, ev, lambda kind, key, *a, **k: asked.append(key) or "", sg, sg.discord_id)
    assert asked == ["Paladin:Holy"], asked


def test_an_unlock_during_a_lock_recovery_is_refused(reg, rs, monkeypatch):
    bot, ev, _ms = locked_event(reg, rs)
    ev.cards.pop("lock:0", None)  # a lock card the interrupted lock never posted: the recovery re-posts it
    rs.save(ev, "card missing (test)")
    refused = []

    async def post_lock_cards(reg_, rs_, ev_, team, ch, only_missing=False):
        try:
            await bot.unlock_run(reg_, rs_, ev_, "Jon")
        except RegistryError as e:
            refused.append(str(e))
        return 0
    monkeypatch.setattr(bot, "officer_channel", lambda reg_, ev_=None: object())
    monkeypatch.setattr(bot, "post_lock_cards", post_lock_cards)
    run(bot.recover_lock(reg, rs, rs.events[ev.key]))
    assert refused == [rc.UNLOCK_SENDING] and rs.events[ev.key].state == "locked"
    assert (reg.key, ev.key) not in bot._lock_posting(), "the guard ends with the recovery"


def test_a_stale_open_ask_from_before_the_event_opened_reads_the_same_in_the_box_and_the_sends(reg, rs):
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg)
    join(reg, rs, ev, ms)
    ev = rs.events[ev.key]
    ev.layout = [[m.display_name for m in ms[:5]]]
    rs.save(ev, "board (test)")
    stale = reg.members[ms[0].discord_id]
    stale.placement_asks.append({"roster": ev.team, "character": stale.main.label, "asked_at": "2000-01-01T00:00:00+00:00",
                                 "answer": None, "answered_at": None, "by": "lock", "channel": "dm"})
    plan = rc.confirm_plan(reg, ev, [m.discord_id for m in ms[:5]])
    assert stale.discord_id in plan.asked and not plan.earlier, "the box: asked, as on a first lock"
    dms = len(bot.dms.get(stale.discord_id, []))
    lock(bot, reg, rs, ev, fill=False)
    assert len(bot.dms.get(stale.discord_id, [])) == dms + 1, "the sends agree: asked"
    assert rc.open_ask(reg.members[stale.discord_id], rs.events[ev.key]) is not None
