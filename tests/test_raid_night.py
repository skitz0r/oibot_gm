"""Dual interfaces phase 2 — raid-night correctness (design §3.2 B2/B4/B6/B7/B8, §5.39): the races (lock vs cancel, fill vs
an officer's send, a placement answer vs expiry, a drag on a board that locked), the lock's recovery step, expiry of
asks only, DM failure as a channel change, Remind once an hour, Withdraw, pausing autofill, reopen within the hour
(never a cancelled run), the open preview across DST, cancel with no DM and its ask cards edited. The bot is the real
RaidMixin (+ the scheduler mixin) over a recording fake of Discord; Registry and RaidStore are real (conftest)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import by_role, join, lock_with_board, open_test_run
from test_parity_verbs import Bot, Chan

from oibot_gm import raidcycle as rc
from oibot_gm.raid_scheduler import RaidSchedulerMixin
from oibot_gm.registry import RegistryError


def run(coro):
    return asyncio.run(coro)


class Msg:
    """A sent DM card: records the edits made to it (RaidMixin.edit_ask_card)."""

    def __init__(self, channel, mid, payload):
        self.channel, self.id, self.payload, self.edits = channel, mid, payload, []

    async def edit(self, **kw):
        self.edits.append(kw)


class DMChannel:
    def __init__(self, bot, uid):
        self.bot, self.id, self.uid = bot, 70_000 + uid % 10_000, uid

    async def fetch_message(self, mid):
        return self.bot.cards[mid]


class CardUser:
    """fetch_user's result: send() returns a message (with its DM channel), so asks store their card ids."""

    def __init__(self, bot, uid):
        self.bot, self.id = bot, uid

    async def send(self, content=None, **kw):
        if self.id in self.bot.dm_fail:
            raise RuntimeError("Cannot send messages to this user")
        gate = self.bot.dm_gate
        if gate is not None:
            await gate.wait()
        payload = content if content is not None else kw.get("view")
        self.bot.dms.setdefault(self.id, []).append(payload)
        ch = self.bot.dm_channels.setdefault(self.id, DMChannel(self.bot, self.id))
        self.bot.channels[ch.id] = ch
        msg = Msg(ch, 500_000 + len(self.bot.cards), payload)
        self.bot.cards[msg.id] = msg
        return msg


class NightBot(RaidSchedulerMixin, Bot):
    """RaidMixin + RaidSchedulerMixin on test_parity_verbs' recording fake; DMs return cards that can be edited."""

    events: dict = {}

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.cards: dict[int, Msg] = {}
        self.dm_channels: dict[int, DMChannel] = {}
        self.dm_fail: set[int] = set()
        self.dm_gate: asyncio.Event | None = None
        self.updates: list[str] = []
        self.signup = Chan(1111, "signups")
        self.channels[self.signup.id] = self.signup

    async def fetch_user(self, uid):
        return CardUser(self, int(uid))

    async def post_run_update(self, reg, ev, text):
        self.updates.append(text)

    def officer_channel(self, reg, ev=None):
        return None


def _view_text(view) -> str:
    out = []
    for c in view.walk_children():
        if hasattr(c, "content"):
            out.append(str(c.content))
    return "\n".join(out)


def card_texts(m: Msg) -> list[str]:
    return [_view_text(e["view"]) for e in m.edits if e.get("view") is not None]


def locked_ten(reg, rs, *, asks: bool = False, confirm_hours: float = 1, **cut):
    """Everyone joins a 10-man test run; 2 tanks, 3 healers, 5 dps are seated by the board, the rest wait on the bench."""
    ev = open_test_run(reg, rs, **cut)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated], hours_to_confirm=confirm_hours)
    if asks:
        for m in seated:
            reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    return ev, seated


def past_confirm(ev):
    ev.confirm_by = (datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()


# ---------------------------------------------------------------- expiry: only an ask that exists expires

def test_expiry_frees_only_seats_whose_member_holds_an_ask(reg, rs):
    ev, seated = locked_ten(reg, rs)
    asked = seated[:4]
    for m in asked:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    past_confirm(ev)
    gone = rc.expire_confirmations(reg, rs, ev)
    assert sorted(gone) == sorted(m.display_name for m in asked), "a seat nobody asked is never 'no answer'"
    for m in seated[4:]:
        assert ev.seat_of(m), f"{m.display_name} was never asked: the seat stays"
    assert rc.expire_confirmations(reg, rs, ev) == []


# ---------------------------------------------------------------- the lock's recovery step

def test_recover_lock_asks_exactly_the_unasked_and_stamps_the_run(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, confirm_hours=1)
    reg.add_placement_ask(seated[0].discord_id, ev.team, seated[0].main.label, "lock")  # one reached before the "crash"
    ev.confirmations_sent_at = None  # the fan-out never finished
    ev.locked_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=5)).astimezone().isoformat()
    assert len(rc.unasked_seats(reg, ev)) == len(seated) - 1 and not ev.confirmations_sent_at
    n = run(bot.recover_lock(reg, rs, ev))
    assert n == len(seated) - 1
    assert not rc.unasked_seats(reg, ev) and ev.confirmations_sent_at
    assert sum(1 for m in seated for a in reg.members[m.discord_id].placement_asks if a["roster"] == ev.team) == len(seated), "nobody asked twice"
    assert seated[0].discord_id not in bot.dms, "the one already asked keeps their card"
    assert run(bot.recover_lock(reg, rs, ev)) == 0, "idempotent"
    assert any("interrupted" in u for u in bot.updates)


def test_recover_lock_leaves_a_fanout_in_progress_alone(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = locked_ten(reg, rs)
    ev.confirmations_sent_at = None
    bot._confirming().add((reg.key, ev.key))
    assert run(bot.recover_lock(reg, rs, ev)) == 0 and rc.unasked_seats(reg, ev)


def test_the_scheduler_never_frees_unasked_seats_at_the_deadline(reg, rs):
    """A lock interrupted before any ask went out and found only after confirm_by: no recovery ask (it would expire at
    once) and no expiry — nobody loses a seat to a question never asked."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    past_confirm(ev)
    rs.save(ev, "deadline passed")
    run(bot.scheduler_tick_guild(reg))
    assert all(ev.seat_of(m) for m in seated)
    assert not bot.dms


# ---------------------------------------------------------------- lock vs cancel

def fast_propose(seat_names):
    """rc.propose without the solver: the hand-picked board (lock_run solves on a copy in a thread)."""
    def propose(reg, rs, ev, save=True):
        size = rc.run_size(reg, ev)
        g = int(reg.profile.comp_rules["group_size"])
        layout = [seat_names[i:i + g] for i in range(0, len(seat_names), g)]
        ev.rosters = rc.board_rosters(reg, ev, layout, size)
        return [], ev.rosters[0]
    return propose


def test_cancel_during_the_lock_fanout_leaves_no_orphan_asks(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    monkeypatch.setattr(rc, "propose", fast_propose([m.display_name for m in seated]))
    ev.channel_id = bot.signup.id

    async def go():
        bot.dm_gate = asyncio.Event()  # the first confirmation DM hangs: the lock is mid fan-out
        lock = asyncio.create_task(bot.lock_run(reg, rs, ev, by="Jon"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ev.state == "locked" and any(a["roster"] == ev.team for m in seated for a in reg.members[m.discord_id].placement_asks):
                break
        assert ev.state == "locked"
        cancel = asyncio.create_task(bot.cancel_run(reg, rs, ev, "Kara", "boss bugged"))
        await asyncio.sleep(0.05)
        bot.dm_gate.set()
        line = await lock
        cline = await cancel
        return line, cline

    line, cline = run(go())
    assert ev.state == "cancelled" and "cancelled before the confirmations finished" in line
    asked = [m for m in seated if any(a["roster"] == ev.team for a in reg.members[m.discord_id].placement_asks)]
    assert len(asked) <= 1, "the fan-out stopped at the cancel: at most the ask in flight"
    assert all(a.get("answer") is not None for m in asked for a in reg.members[m.discord_id].placement_asks if a["roster"] == ev.team), "no open ask on a cancelled run"
    assert not any("is locked" in str(c["content"]) for c in bot.signup.sent), "no 'is locked' line for a cancelled run"
    assert "Nobody was DMed" in cline


def test_cancel_waits_for_the_board_lock_and_refuses_a_closed_run(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)

    async def go():
        lock = bot.board_lock(reg, ev.key)
        await lock.acquire()
        task = asyncio.create_task(bot.cancel_run(reg, rs, ev, "Kara", None))
        await asyncio.sleep(0.05)
        assert ev.state == "open", "cancel waits while a lock holds the board"
        lock.release()
        await task
        assert ev.state == "cancelled" and ev.closed_at and ev.prev_state == "open"
        with pytest.raises(RegistryError, match="already cancelled"):
            await bot.cancel_run(reg, rs, ev, "Kara", None)
    run(go())


def test_cancel_sends_no_dm_edits_the_ask_cards_and_keeps_the_reason_off_the_channel(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, confirm_hours=1)
    ev.channel_id = bot.signup.id
    for i, r in enumerate(ev.all_rosters):  # the lock's fan-out, through the real confirm_one: cards stored on the asks
        for gi, g in enumerate(r.groups):
            for n in g:
                sg = rc.signup_by_name(ev, n)
                run(bot.confirm_one(reg, ev, sg, i, gi, "lock"))
    assert all(a.get("dm") for m in seated for a in reg.members[m.discord_id].placement_asks if a["roster"] == ev.team)
    dms_before = sum(len(v) for v in bot.dms.values())
    fx = rc.cancel_effects(reg, ev)
    assert len(fx["rostered"]) == 10 and len(fx["open_asks"]) == 10
    line = run(bot.cancel_run(reg, rs, ev, "Kara", "tank flu"))
    assert sum(len(v) for v in bot.dms.values()) == dms_before, "the DM rule: a cancel DMs nobody"
    edited = [m for m in bot.cards.values() if any("Cancelled" in t for t in card_texts(m))]
    assert len(edited) == 10, "every open ask card says Cancelled"
    assert bot.signup.sent and "cancelled" in bot.signup.sent[-1]["content"] and "tank flu" not in bot.signup.sent[-1]["content"]
    assert any("tank flu" in u for u in bot.updates), "the reason is the officers' (the run's thread)"
    assert all(a["answer"] == "cancelled" for m in seated for a in reg.members[m.discord_id].placement_asks if a["roster"] == ev.team)
    assert "10 open ask card(s) now say Cancelled" in line


def test_cancel_without_a_notice_posts_nothing_in_the_channel(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    ev.channel_id = bot.signup.id
    line = run(bot.cancel_run(reg, rs, ev, "Kara", None, notify=False))
    assert not bot.signup.sent and "no line in the signup channel" in line


# ---------------------------------------------------------------- fill vs an officer's send; pause; DM failure

def short_one(reg, rs, *, kind="adhoc"):
    """A locked 10-man with one DPS seat freed and the bench (joined, not seated) as candidates."""
    ev, seated = locked_ten(reg, rs, confirm_hours=1)
    rc.free_seat(reg, rs, ev, seated[-1].discord_id, "no-confirm")
    return ev, seated


def test_run_fill_waits_for_the_plan_lock_and_never_double_asks(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    team = rc.run_team(reg, ev)

    async def go():
        lock = bot.plan_lock(reg, ev.key)
        await lock.acquire()  # an officer's send in progress
        task = asyncio.create_task(bot.run_fill(reg, rs, ev, team))
        await asyncio.sleep(0.05)
        assert not ev.fill_asks, "the engine waits for the officer's send"
        plan = rc.role_plan(reg, rs, ev)
        officer = rc.send_batch(plan)[0][:1]
        for a in officer:  # what the officer's send put on the run meanwhile
            a.approved_by, a.expires_at = "Jon", rc.ask_deadline(reg, ev).isoformat()
            ev.fill_asks.append(a)
        lock.release()
        sent, _nd = await task
        return officer, sent
    officer, sent = run(go())
    pending = [a.discord_id for a in ev.fill_asks if a.pending]
    assert len(pending) == len(set(pending)), "nobody holds two open asks for one run"
    assert all(a.discord_id != officer[0].discord_id for a in sent), "the engine counted the officer's ask"
    assert len(pending) <= rc.FILL_MAX_OPEN


def test_paused_autofill_sends_nothing_on_its_own_but_an_officers_send_goes(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    team = rc.run_team(reg, ev)
    run(bot.set_autofill_paused(reg, rs, ev, True, by="Jon"))
    assert ev.autofill_paused and ev.autofill_paused_by == "Jon"
    assert run(bot.run_fill(reg, rs, ev, team))[0] == [], "the scheduler / a freed seat / an answer: nothing"
    sent, _ = run(bot.run_fill(reg, rs, ev, team, by="Jon", approved_by="Jon"))
    assert sent, "an officer's explicit send still works"
    assert "paused" in rc.fill_state_text(reg, ev)
    assert "already paused" in run(bot.set_autofill_paused(reg, rs, ev, True, by="Jon"))


def test_a_failed_dm_is_a_channel_change_never_an_answer(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    team = rc.run_team(reg, ev)
    bot.dm_fail = {m.discord_id for m in reg.test_members()} | {reg.config.owner_discord_id}  # puppets' DMs reach the owner (the tester)
    sent, _ = run(bot.run_fill(reg, rs, ev, team))
    assert sent and all(a.channel == "web" and a.answer is None and a.pending for a in sent)
    assert not any(a.answer == "expired" for a in ev.fill_asks)
    assert any("DM failed — waits on the site" in t for _, t in bot.ops.lines)
    plan = rc.role_plan(reg, rs, ev)
    assert set(a.display_name for a in sent) <= set(plan.waiting), "the planner still counts them as asked"
    assert rc.asks_on_site(reg, ev) == len(sent) and "on the site" in rc.fill_state_text(reg, ev)


def test_a_failed_confirmation_dm_waits_on_the_site(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    bot.dm_fail = {reg.config.owner_discord_id}  # puppets' DMs go to the tester (the owner here)
    sg = ev.signups[str(seated[0].discord_id)]
    assert run(bot.confirm_one(reg, ev, sg, 0, 0, "lock")) is False
    ask = reg.members[seated[0].discord_id].placement_asks[-1]
    assert ask["answer"] is None and ask["channel"] == "web"


def test_two_officers_pressing_ask_the_second_is_told_who_sent_it(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Jon"))
    with pytest.raises(rc.PlanChanged, match="Already sent by Jon"):
        run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Kara"))


# ---------------------------------------------------------------- a placement answer vs expiry (check-and-set)

def test_placement_answer_after_expiry_is_refused_from_one_place(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    m = seated[0]
    past_confirm(ev)
    rc.expire_confirmations(reg, rs, ev)
    with pytest.raises(RegistryError, match=r"Already answered \(no answer in time"):
        run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, m.display_name))
    assert not ev.seat_of(m), "the member file and the run file agree: no seat"


def test_a_second_placement_answer_names_the_first(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    m = seated[1]
    run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, "Kestrel"))
    with pytest.raises(RegistryError, match=r"Already answered \(yes, by Kestrel at \d{1,2}:\d\d [AP]M\)"):
        run(bot.answer_placement_for(reg, m.discord_id, ev.team, False, m.display_name))
    past_confirm(ev)
    assert m.display_name not in rc.expire_confirmations(reg, rs, ev), "a yes before the deadline keeps the seat"
    assert ev.seat_of(m)


def test_expiry_edits_the_card_and_sends_no_dm(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    sg = ev.signups[str(seated[2].discord_id)]
    run(bot.confirm_one(reg, ev, sg, 0, 0, "lock"))
    past_confirm(ev)
    rs.save(ev, "deadline")
    run(bot.scheduler_tick_guild(reg))
    assert not ev.seat_of(seated[2])
    card = bot.cards[reg.members[seated[2].discord_id].placement_asks[-1]["dm"][1]]
    assert any("was released" in t for t in card_texts(card))
    assert not any("released" in str(x) for v in bot.dms.values() for x in v), "no 'your seat was released' DM (the fill asks that follow are asks)"


# ---------------------------------------------------------------- a drag on a board that locked meanwhile

def test_board_move_refuses_a_drag_rendered_on_the_open_board(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    bench = [s.display_name for s in ev.signups.values() if s.status == "in" and not ev.seat_of(s.discord_id)]
    with pytest.raises(rc.BoardChanged, match="locked while you were dragging"):
        run(bot.board_move(reg, rs, ev, bench[0], (0, 0), by="Jon", expect_state="open"))
    assert not ev.seat_of(bench[0]), "never applied as a post-lock roster edit"


# ---------------------------------------------------------------- Remind and Withdraw

def test_remind_resends_against_the_existing_ask_once_an_hour(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    for i, r in enumerate(ev.all_rosters):
        for gi, g in enumerate(r.groups):
            for n in g:
                run(bot.confirm_one(reg, ev, rc.signup_by_name(ev, n), i, gi, "lock"))
    run(bot.answer_placement_for(reg, seated[0].discord_id, ev.team, True, seated[0].display_name))
    before = {m.discord_id: reg.members[m.discord_id].placement_asks[-1]["asked_at"] for m in seated}
    owner = reg.config.owner_discord_id  # puppets' DMs reach the tester
    n_before = len(bot.dms.get(owner, []))
    line = run(bot.remind(reg, rs, ev, by="Jon"))
    assert "Reminded 9 by DM" in line and ev.reminded_by == "Jon" and ev.reminded_at
    assert {m.discord_id: reg.members[m.discord_id].placement_asks[-1]["asked_at"] for m in seated} == before, "the same asks: never re-added"
    assert all(reg.members[m.discord_id].placement_asks[-1].get("reminded_at") for m in seated[1:])
    assert len(bot.dms[owner]) > n_before
    with pytest.raises(RegistryError, match="once an hour"):
        run(bot.remind(reg, rs, ev, by="Kara"))
    ev.reminded_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=61)).isoformat()
    assert "Reminded" in run(bot.remind(reg, rs, ev, by="Kara"))


def test_withdraw_closes_a_sent_ask_and_edits_its_card(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Jon"))
    a = sent[0]
    line = run(bot.withdraw_sent_ask(reg, rs, ev, a.discord_id, by="Kara"))
    assert a.answer == "withdrawn" and a.answered_by == "Kara" and a.display_name in line
    assert any("Never mind" in t for t in card_texts(bot.cards[a.dm_message_id]))
    with pytest.raises(RegistryError, match="No ask is out"):
        run(bot.withdraw_sent_ask(reg, rs, ev, a.discord_id, by="Kara"))


# ---------------------------------------------------------------- reopen within the hour; never a cancelled run

def test_reopen_an_end_by_mistake_within_the_hour_and_the_roster_entry_survives(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = locked_ten(reg, rs)
    assert rc.end_run(reg, rs, ev, "Jon") and ev.closed_at and ev.prev_state == "locked"
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.team(ev.team), "the grace hour keeps the run's roster entry (cutoffs, test flags)"
    line = run(bot.reopen_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked" and "Reopened" in line and ev.closed_at is None
    rc.end_run(reg, rs, ev, "Jon")
    ev.closed_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=61)).isoformat()
    with pytest.raises(RegistryError, match="more than 1 hour"):
        run(bot.reopen_run(reg, rs, ev, by="Jon"))
    run(bot.cleanup_ephemeral(reg, rs))
    assert not reg.config.team(ev.team), "after the grace hour the entry goes"


def test_a_cancelled_run_is_never_reopened_and_its_entry_goes_at_once(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    run(bot.cancel_run(reg, rs, ev, "Jon", None, notify=False))
    with pytest.raises(RegistryError, match="cancelled run can't be reopened"):
        run(bot.reopen_run(reg, rs, ev, by="Jon"))
    run(bot.cleanup_ephemeral(reg, rs))
    assert not reg.config.team(ev.team)


# ---------------------------------------------------------------- opening: the preview across DST

def test_open_preview_resolves_dst_nights_and_matches_a_scheduled_run(reg, rs):
    gap = rc.open_preview(reg, rs, "barrow_deeps", "2027-03-14 02:30", "adhoc")
    assert gap["kind"] == "adhoc" and "clocks go forward" in gap["when_note"] and "3:30 AM" in gap["label"]
    assert datetime.fromisoformat(gap["start"]).utcoffset() == timedelta(hours=-7)
    amb = rc.open_preview(reg, rs, "barrow_deeps", "2026-11-01 01:30", "adhoc")
    assert "clocks go back" in amb["when_note"] and datetime.fromisoformat(amb["start"]).utcoffset() == timedelta(hours=-7), "the first 1:30 (still PDT)"
    wed = reg.now_local().date() + timedelta(days=(2 - reg.now_local().weekday()) % 7 + 7)  # a Wednesday 1–2 weeks out
    plain = rc.open_preview(reg, rs, "barrow_deeps", f"{wed.isoformat()} 20:00", "adhoc")
    assert plain["when_note"] == "" and "8:00 PM" in plain["label"] and "Ad hoc" in plain["rules"]
    reg.set_raid_overrides("barrow_deeps", {"slots": "Wed 20:00", "first_open": "2026-01-01T00:00"}, "t")
    hit = rc.open_preview(reg, rs, "barrow_deeps", f"{wed.isoformat()} 20:00", None)
    assert hit["kind"] == "scheduled" and hit["key"] == rc.event_key("barrow_deeps", datetime.fromisoformat(hit["start"]), None)
    again = rc.open_preview(reg, rs, "barrow_deeps", hit["start"], None)  # the exact instant carried back by Open
    assert again["key"] == hit["key"] and again["start"] == hit["start"]
    with pytest.raises(RegistryError, match="pick one"):
        rc.open_when(reg, "barrow_deeps", None, "adhoc")


def test_local12_is_the_one_formatter(reg):
    assert reg.local12("2026-12-02T04:05:00+00:00") == "Tue 01 Dec 8:05 PM"


# ---------------------------------------------------------------- the scheduler's missing signup channel

def test_no_signup_channel_warns_once_per_run(reg, rs):
    bot = NightBot(reg, rs)
    reg.set_raid_overrides("barrow_deeps", {"slots": "Wed 20:00", "first_open": "2026-01-01T00:00", "signup_lead_hours": 24 * 14}, "t")
    run(bot.scheduler_tick_guild(reg))
    first = [t for lvl, t in bot.ops.lines if "did not open: no signup channel" in t]
    assert first and all("PM" in t or "AM" in t for t in first)
    run(bot.scheduler_tick_guild(reg))
    assert len([t for lvl, t in bot.ops.lines if "did not open" in t]) == len(first), "once per run, never every minute"
    assert not rs.events


# ---------------------------------------------------------------- the routes (TestClient over the real verbs)

@pytest.fixture
def web_bot(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = NightBot(reg, rs)
    return SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)))


def H():
    from test_mcp import bearer

    return bearer()


def test_run_json_carries_the_raid_night_fields(reg, rs, web_bot):
    ev, seated = locked_ten(reg, rs, asks=True)
    ev.cards, ev.cards_channel_id, ev.updates_thread_id = {"lock:0": 42}, 77, 88
    rs.save(ev, "cards")
    r = web_bot.c.get(f"/api/run/{ev.key}", headers=H())
    assert r.status_code == 200, r.text
    j = r.json()
    for k in ("lock_error", "fill_state_text", "early_warning", "spec_switches", "regulars_not_rostered", "confirm_by", "confirmations_sent_at",
              "confirmations_pending", "asks_on_site", "autofill_paused", "may_remind", "remind_note", "may_end", "end_note", "may_reopen", "log_rows"):
        assert k in j, k
    assert j["links"]["card"].endswith("/77/42") and j["links"]["thread"].endswith("/88")
    assert j["may_remind"] is True and j["may_end"] is False and "hasn't started" in j["end_note"]
    assert j["log_rows"] and all("when" in row and "officer" in row for row in j["log_rows"])


def test_move_with_a_stale_state_is_409_with_the_board(reg, rs, web_bot):
    ev, seated = locked_ten(reg, rs)
    bench = next(s.display_name for s in ev.signups.values() if s.status == "in" and not ev.seat_of(s.discord_id))
    r = web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": bench, "to": {"r": 0, "g": 0, "i": 0}, "state": "open"}, headers=H())
    assert r.status_code == 409 and r.json()["state"] == "locked" and "board" in r.json() and "locked while you were dragging" in r.json()["error"]
    assert not ev.seat_of(bench)
    bad = web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": bench, "to": None, "state": "sideways"}, headers=H())
    assert bad.status_code == 400


def test_layout_and_strategy_are_one_write(reg, rs, web_bot):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]
    stale = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"groups": [names[:3]], "rev": "000000000000", "strategy": "first"}, headers=H())
    assert stale.status_code == 409 and ev.split_strategy is None, "a refused layout never saves its strategy"
    ok = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"groups": [names[:3]], "rev": rc.board_rev(reg, ev), "strategy": "first"}, headers=H())
    assert ok.status_code == 200, ok.text
    assert ev.split_strategy == "first" and ev.layout == [names[:3]]
    assert web_bot.c.post(f"/api/run/{ev.key}/layout", json={"groups": [], "strategy": "nonsense"}, headers=H()).status_code == 400


def test_lock_preview_route_shows_the_roster_and_saves_nothing_but_the_proposal(reg, rs, web_bot, monkeypatch):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]

    def preview(reg_, rs_, ev_, strategy, avoid=None):
        trial = ev_.model_copy(deep=True)
        rosters = rc.board_rosters(reg_, trial, [names[:5], names[5:]], rc.run_size(reg_, trial))
        seated = {p.signup_name for r in rosters for p in r.selected}
        rosters[0].benched = [p for p in rc.players_for(reg_, trial) if p.signup_name not in seated]
        return [list(g) for r in rosters for g in r.groups], rosters
    monkeypatch.setattr(rc, "split_preview", preview)
    r = web_bot.c.post(f"/api/run/{ev.key}/lock/preview", json={}, headers=H())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["board"]["rosters"][0]["rostered"] == 10 and "The board becomes the roster." in j["consequence"]
    assert ev.state == "open" and not ev.rosters and ev.last_proposal["kind"] == "lock preview"


def test_cancel_reopen_remind_pause_withdraw_routes(reg, rs, web_bot):
    c = web_bot.c
    ev, seated = short_one(reg, rs)
    r = c.post(f"/api/run/{ev.key}/remind", json={}, headers=H())
    assert r.status_code == 409 and "remind" in r.json()["error"].lower()
    r = c.post(f"/api/run/{ev.key}/pause", json={"paused": True}, headers=H())
    assert r.status_code == 200 and ev.autofill_paused
    assert c.post(f"/api/run/{ev.key}/pause", json={}, headers=H()).status_code == 400
    plan = rc.role_plan(reg, rs, ev)
    run(web_bot.bot.send_plan(reg, rs, ev, rev=plan.rev, by="Jon"))  # an officer's send still goes while paused
    out = next(a for a in ev.fill_asks if a.pending)
    r = c.post(f"/api/run/{ev.key}/withdraw", json={"uid": str(out.discord_id)}, headers=H())
    assert r.status_code == 200 and out.answer == "withdrawn", r.text
    assert c.post(f"/api/run/{ev.key}/withdraw", json={"uid": str(out.discord_id)}, headers=H()).status_code == 400
    rc.end_run(reg, rs, ev, "Jon")
    r = c.post(f"/api/run/{ev.key}/reopen", json={}, headers=H())
    assert r.status_code == 200 and ev.state == "locked", r.text
    r = c.post(f"/api/run/{ev.key}/cancel", json={"reason": "wipe night", "notify": False}, headers=H())
    assert r.status_code == 200 and ev.state == "cancelled" and "Nobody was DMed" in r.json()["message"]
    assert c.post(f"/api/run/{ev.key}/cancel", json={}, headers=H()).status_code == 400, "already cancelled"
    r = c.post(f"/api/run/{ev.key}/reopen", json={}, headers=H())
    assert r.status_code == 400 and "cancelled run can't be reopened" in r.json()["error"]
    assert any(t.startswith("[mcp]") and "cancelled" in t for _, t in web_bot.bot.ops.lines), "the audit line names the surface"


def test_open_preview_and_open_routes_carry_the_exact_instant(reg, rs, web_bot):
    c = web_bot.c
    p = c.post("/api/raid/barrow_deeps/open/preview", json={"when": "2027-03-14 02:30", "schedule": "adhoc"}, headers=H())
    assert p.status_code == 200, p.text
    j = p.json()
    assert j["kind"] == "adhoc" and "clocks go forward" in j["when_note"] and j["channel"] is None
    wed = reg.now_local().date() + timedelta(days=(2 - reg.now_local().weekday()) % 7 + 7)
    reg.set_raid_overrides("barrow_deeps", {"slots": "Wed 20:00", "first_open": "2026-01-01T00:00"}, "t")
    p = c.post("/api/raid/barrow_deeps/open/preview", json={"when": f"{wed.isoformat()} 20:00"}, headers=H()).json()
    assert p["kind"] == "scheduled"
    bad = c.post("/api/raid/barrow_deeps/open", json={"start": p["start"], "key": "bd-0000-0000-2000-01-01"}, headers=H())
    assert bad.status_code == 409 and bad.json()["preview"]["key"] == p["key"] and not rs.events
    ok = c.post("/api/raid/barrow_deeps/open", json={"start": p["start"], "key": p["key"]}, headers=H())
    assert ok.status_code == 200 and ok.json()["key"] == p["key"] and p["key"] in rs.events, ok.text


def test_runs_history_pages_beyond_the_first_eight(reg, rs, web_bot):
    for d in range(12):
        ev = open_test_run(reg, rs, days=2 + d)
        rc.end_run(reg, rs, ev, "t")
    j = web_bot.c.get("/api/runs?raid=barrow_deeps&offset=8&limit=20", headers=H()).json()
    assert j["total"] == 12 and len(j["runs"]) == 4 and not j["more"]
    ros = web_bot.c.get("/api/rosters", headers=H()).json()
    bd = next(x for x in ros["raids"] if x["id"] == "barrow_deeps")
    assert len(bd["past"]) == 8 and bd["past_total"] == 12
    assert web_bot.c.get("/api/runs?raid=nope", headers=H()).status_code == 404


def test_upcoming_rows_carry_the_instant_and_say_when_no_channel_can_take_the_sheet(reg, rs, web_bot):
    reg.set_raid_overrides("barrow_deeps", {"slots": "Wed 20:00", "first_open": "2026-01-01T00:00", "signup_lead_hours": 24 * 14}, "t")
    bd = next(x for x in web_bot.c.get("/api/rosters", headers=H()).json()["raids"] if x["id"] == "barrow_deeps")
    assert bd["upcoming"] and all(u["start_iso"] and u["key"] for u in bd["upcoming"])
    assert bd["upcoming"][0]["needs_channel"] is True and bd["signup_channel"] is False
