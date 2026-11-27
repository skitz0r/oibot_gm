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
from test_bench_last import DPS, base_eight, people, schedule_event
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import NightBot, card_texts, run

from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError


def lock(bot, reg, rs, ev, **kw):
    return run(bot.lock_run(reg, rs, ev, by="Jon", **kw))


def groups_of(ev) -> list[list[str]]:
    return [list(g) for r in ev.all_rosters for g in r.groups]


def locked_event(reg, rs, *, placed: int = 8, joined_extra: int = 2):
    """A schedule's event: 8 + `joined_extra` joined, `placed` of them on the board, locked as it stands by Jon (the
    confirmations go out to everyone rostered)."""
    bot = NightBot(reg, rs)
    ev = schedule_event(reg, rs)
    ms = base_eight(reg) + people(reg, joined_extra, DPS)
    join(reg, rs, ev, ms)
    ns = [m.display_name for m in ms]
    ev.layout = [ns[:min(placed, 5)], ns[5:placed]]
    rs.save(ev, "board (test)")
    lock(bot, reg, rs, rs.events[ev.key], fill=False)
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
    for s in (rc.UNLOCK_OPEN, rc.UNLOCK_DONE, rc.UNLOCK_CANCELLED, rc.UNLOCK_STARTED, rc.UNLOCK_LOOT):
        assert s.endswith(".") and s.count(".") == 1 and len(s.split()) <= 20


# ---------------------------------------------------------------- an event locked before this change

def test_an_event_locked_before_this_change_unlocks_the_same_way(reg, rs):
    bot, ev, _ms = locked_event(reg, rs)
    roster = groups_of(ev)
    ev.lock_line, ev.lock_fill, ev.placed_by, ev.lock_seats = None, None, {}, None
    rs.save(ev, "as an old file (test)")
    run(bot.unlock_run(reg, rs, rs.events[ev.key], "Jon"))
    ev = rs.events[ev.key]
    assert ev.state == "open" and [g for g in rc.board_layout(reg, ev) if g] == [g for g in roster if g]
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
