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
    PUPPET_DIGEST = False  # the puppets stand in for members here: their DM cards must exist to be edited (B9's digest: test_testbench)

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
    ev.lock_seats = [m.discord_id for m in seated]  # recorded at the flip (lock_run)
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
    ev.lock_seats = [m.discord_id for m in _seated]
    bot._confirming().add((reg.key, ev.key))
    assert run(bot.recover_lock(reg, rs, ev)) == 0 and rc.unasked_seats(reg, ev)


def test_recover_lock_never_reasks_a_seat_that_came_after_the_lock(reg, rs):
    """A fill yes and an officer's add after the lock are not the lock's seats: the recovery step asks only the seats
    rostered at the flip that nobody asked — never a fill-seated member, never one an officer seated later."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, confirm_hours=1)
    at_lock, filler, added = seated[:8], seated[8], seated[9]
    ev.lock_seats = [m.discord_id for m in at_lock]
    ev.confirmations_sent_at = None  # the fan-out was interrupted after the flip
    ev.locked_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=5)).astimezone().isoformat()
    reg.add_placement_ask(at_lock[0].discord_id, ev.team, at_lock[0].main.label, "lock")
    # the filler's seat came through a fill ask answered yes (their signup is the fill's)
    ev.fill_asks.append(rc.FillAsk(discord_id=filler.discord_id, display_name=filler.display_name, kind="sub", role="melee",
                                   character=filler.main.label, spec=filler.main.spec, reason="short 1", answer="yes",
                                   answered_at=rc.now()))
    ev.signups[str(filler.discord_id)].source = "fill"
    rs.save(ev, "after-lock seats")
    missing = {sg.discord_id for _i, _g, sg in rc.unasked_seats(reg, ev)}
    assert missing == {m.discord_id for m in at_lock[1:]}
    n = run(bot.recover_lock(reg, rs, ev))
    assert n == len(at_lock) - 1 and ev.confirmations_sent_at
    for m in (filler, added):
        assert m.discord_id not in bot.dms, f"{m.display_name}'s seat came after the lock: no Confirm / Can't"
        assert not [a for a in reg.members[m.discord_id].placement_asks if a["roster"] == ev.team]


def test_recover_lock_stamps_a_legacy_locked_run_and_asks_nobody(reg, rs):
    """On deploy, a run locked before `lock_seats` / `confirmations_sent_at` existed whose asks went out (or whose
    deadline passed) is treated as sent: stamped, nobody asked again."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True, confirm_hours=1)
    for m in seated[:3]:  # some answered, some still hold the card
        a = reg.members[m.discord_id].placement_asks[-1]
        a["answer"], a["answered_at"] = "yes", rc.now()
    ev.confirmations_sent_at = ev.lock_seats = None
    assert rc.unasked_seats(reg, ev) == []
    assert run(bot.recover_lock(reg, rs, ev)) == 0
    assert ev.confirmations_sent_at and not bot.dms and not bot.updates
    # a legacy run with no asks at all whose deadline has passed: stamped too, nobody asked
    ev2, seated2 = locked_ten(reg, rs, confirm_hours=1, days=4)
    ev2.confirmations_sent_at = ev2.lock_seats = None
    past_confirm(ev2)
    assert run(bot.recover_lock(reg, rs, ev2)) == 0
    assert ev2.confirmations_sent_at and not bot.dms
    assert all(ev2.seat_of(m) for m in seated2)


def test_lock_run_records_the_seats_at_the_flip(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members())
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    monkeypatch.setattr(rc, "propose", fast_propose([m.display_name for m in seated]))
    run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert sorted(ev.lock_seats) == sorted(m.discord_id for m in seated)


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


def test_a_confirm_card_in_flight_at_a_cancel_is_closed_when_it_lands(reg, rs):
    """The Confirm DM is still being sent when the run is cancelled: the cancel can't edit a card it has no id for, so
    confirm_one edits it the moment it lands — no live Confirm on a cancelled run."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    m = seated[1]

    async def go():
        bot.dm_gate = asyncio.Event()  # the DM hangs: in flight
        ask = asyncio.create_task(bot.confirm_one(reg, ev, ev.signups[str(m.discord_id)], ev.seat_of(m)[0], 0, "lock"))
        await asyncio.sleep(0.05)
        await bot.cancel_run(reg, rs, ev, "Kara", None)
        bot.dm_gate.set()
        await ask
    run(go())
    a = reg.members[m.discord_id].placement_asks[-1]
    card = bot.cards[a["dm"][1]]
    assert ev.state == "cancelled" and a["answer"] == "cancelled"
    assert _live_buttons(card) == 0 and "Cancelled" in card_texts(card)[-1]


def test_the_scheduler_expires_nothing_on_a_run_cancelled_mid_tick(reg, rs, monkeypatch):
    """A cancel lands during the tick's awaits (the recovery step), before its asks are withdrawn: the expiry step
    re-checks the state under the run's lock and frees nobody."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs, asks=True)
    past_confirm(ev)
    rs.save(ev, "deadline passed")

    async def cancelled_meanwhile(reg_, rs_, ev_):
        ev_.prev_state, ev_.state = ev_.state, "cancelled"  # cancel_run's flip; its withdraw step hasn't run yet
        return 0
    monkeypatch.setattr(bot, "recover_lock", cancelled_meanwhile)
    run(bot.scheduler_tick_guild(reg))
    assert all(ev.seat_of(m) for m in seated), "nobody freed on a cancelled run"
    assert all(a.get("answer") != "expired" for m in seated for a in reg.members[m.discord_id].placement_asks)


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


def _live_buttons(card: Msg) -> int:
    """Buttons still pressable on a DM card: the last edit's view, else what was sent."""
    import discord as _d
    view = card.edits[-1]["view"] if card.edits else card.payload
    return sum(1 for c in view.walk_children() if isinstance(c, (_d.ui.Button, _d.ui.DynamicItem))) if hasattr(view, "walk_children") else 0


def test_a_yes_after_a_remind_leaves_no_live_button_on_either_card(reg, rs):
    """Remind sends a newer Confirm card: the older one goes dead at once ('Reminded — see the newer message'), and a
    yes then closes the newer one — no Confirm stays pressable on either."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    m = seated[1]
    seat = ev.seat_of(m)
    run(bot.confirm_one(reg, ev, ev.signups[str(m.discord_id)], seat[0], 0, "lock"))
    first = bot.cards[reg.members[m.discord_id].placement_asks[-1]["dm"][1]]
    assert _live_buttons(first) >= 2
    run(bot.remind(reg, rs, ev, by="Jon"))
    second = bot.cards[reg.members[m.discord_id].placement_asks[-1]["dm"][1]]
    assert second is not first and _live_buttons(second) >= 2
    assert _live_buttons(first) == 0 and "Reminded" in card_texts(first)[-1]
    run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, m.display_name, by_id=m.discord_id))
    assert _live_buttons(first) == 0 and _live_buttons(second) == 0


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


# ---------------------------------------------------------------- fix pass: cancel vs a fan-out, two Reminds, surfaces

def short_three(reg, rs):
    """A locked 10-man with three DPS seats freed: the fill engine's batch holds several asks."""
    ev, seated = locked_ten(reg, rs, confirm_hours=1)
    for m in seated[-3:]:
        rc.free_seat(reg, rs, ev, m.discord_id, "no-confirm")
    return ev, seated


async def _until(cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _cancel_mid_fanout(bot, reg, rs, ev, start_fanout):
    """Hold the first DM, start the fan-out, cancel while it hangs, then let the DM through. Returns (fan-out result,
    the cancel's line, how many asks were on the run when the cancel came)."""
    async def go():
        bot.dm_gate = asyncio.Event()
        fan = asyncio.create_task(start_fanout())
        assert await _until(lambda: len(ev.fill_asks) > 0), "the fan-out put its asks on the run"
        on_run = len(ev.fill_asks)
        cancel = asyncio.create_task(bot.cancel_run(reg, rs, ev, "Kara", "server down"))
        assert await _until(lambda: ev.state == "cancelled"), "the cancel flips the state at once (board lock, not plan lock)"
        assert not cancel.done(), "the asks close under plan_lock: the cancel waits for the DM in flight"
        bot.dm_gate.set()
        return await fan, await cancel, on_run
    return run(go())


def test_cancel_during_a_fill_fanout_sends_nothing_more_and_edits_the_card_in_flight(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_three(reg, rs)
    team = rc.run_team(reg, ev)
    (sent, _nd), line, on_run = _cancel_mid_fanout(bot, reg, rs, ev, lambda: bot.run_fill(reg, rs, ev, team))
    assert on_run >= 2, "a batch of several asks, so 'nothing after the cancel' means something"
    carded = [a for a in ev.fill_asks if a.dm_message_id]
    assert len(carded) == 1, "only the DM already in flight went out"
    assert any("Cancelled" in t for t in card_texts(bot.cards[carded[0].dm_message_id])), "its card was edited at once"
    assert all(a.answer == "cancelled" and not a.pending for a in ev.fill_asks), "no ask left open on a cancelled run"
    assert not sent, "nothing counts as out on a cancelled run"
    assert ev.fill_state != "asking" and ev.state == "cancelled"
    assert "Nobody was DMed" in line


def test_cancel_during_an_officers_send_stops_the_fanout(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_three(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    (sent, _plan), _line, on_run = _cancel_mid_fanout(bot, reg, rs, ev, lambda: bot.send_plan(reg, rs, ev, rev=plan.rev, by="Jon"))
    assert on_run >= 2
    assert len([a for a in ev.fill_asks if a.dm_message_id]) == 1
    assert all(not a.pending for a in ev.fill_asks) and not sent
    assert ev.fill_state != "asking"


def test_a_prelock_fanout_stops_when_the_run_locks(reg, rs):
    """The same re-check for pre-lock suggestions: a lock during the fan-out closes the rest as released."""
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    asks = [rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="tank", character=m.main.name, spec=m.main.spec, reason="short a tank")
            for m in reg.test_members()[:3]]

    async def go():
        bot.dm_gate = asyncio.Event()
        task = asyncio.create_task(bot._send_asks(reg, rs, ev, rc.run_team(reg, ev), asks, phase="prelock", approved_by="Jon", deadline=ev.start.isoformat()))
        assert await _until(lambda: len(ev.fill_asks) == 3)
        ev.state = "locked"
        bot.dm_gate.set()
        return await task
    out = run(go())
    assert out == [] and all(a.answer == "released" for a in asks)
    assert len([a for a in asks if a.dm_message_id]) == 1 and any("Never mind" in t for t in card_texts(bot.cards[asks[0].dm_message_id]))


def _confirm_all(bot, reg, ev):
    for i, r in enumerate(ev.all_rosters):
        for gi, g in enumerate(r.groups):
            for n in g:
                run(bot.confirm_one(reg, ev, rc.signup_by_name(ev, n), i, gi, "lock"))


def test_two_officers_pressing_remind_at_once_send_one_round(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    _confirm_all(bot, reg, ev)
    before = len(bot.cards)

    async def go():
        bot.dm_gate = asyncio.Event()
        a = asyncio.create_task(bot.remind(reg, rs, ev, by="Jon"))
        await asyncio.sleep(0.02)
        b = asyncio.create_task(bot.remind(reg, rs, ev, by="Kara"))
        await asyncio.sleep(0.02)
        bot.dm_gate.set()
        return await asyncio.gather(a, b, return_exceptions=True)
    ra, rb = run(go())
    assert isinstance(ra, str) and "Reminded 10 by DM" in ra
    assert isinstance(rb, RegistryError) and "by Jon" in str(rb), "the second press is told who reminded"
    one_round = len(bot.cards) - before
    assert one_round <= 2 * len(seated), "one card (plus the test bench's prefix) per unconfirmed member — never two rounds"
    assert ev.reminded_by == "Jon"


def test_cancel_during_a_remind_stops_it_and_closes_the_card_in_flight(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    _confirm_all(bot, reg, ev)
    before = set(bot.cards)

    async def go():
        bot.dm_gate = asyncio.Event()
        rem = asyncio.create_task(bot.remind(reg, rs, ev, by="Jon"))
        await asyncio.sleep(0.02)
        await bot.cancel_run(reg, rs, ev, "Kara", None)
        bot.dm_gate.set()
        return await rem
    line = run(go())
    new = [bot.cards[k] for k in bot.cards if k not in before]
    assert len(new) <= 2, "the reminder in flight only (and its test-bench prefix)"
    assert any("Cancelled" in t for m in new for t in card_texts(m)), "the reminder card that landed after the cancel is closed"
    assert "stopped" in line and "cancelled" in line
    assert all(a.get("answer") == "cancelled" for m in seated for a in reg.members[m.discord_id].placement_asks if a["roster"] == ev.team)


def test_clearing_the_test_bench_posts_no_cancel_line(reg, rs):
    from oibot_gm.discord_pool import SetupMixin

    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    ev.channel_id = bot.signup.id
    run(SetupMixin.test_bench_clear(bot, reg, "Jon"))
    assert ev.state == "cancelled"
    assert not bot.signup.sent, "real members read the signup channel: a bench clear says nothing there"


def test_plain_text_cancel_keeps_its_surface_and_can_be_quiet(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    ev.channel_id = bot.signup.id
    owner = reg.config.owner_discord_id
    preview = configops.describe(reg, ConfigOp(op="run_cancel", target=ev.key, value="quiet"))
    assert "Nobody is DMed" in preview and "no line in the signup channel" in preview
    line = run(configops.apply_async(reg, ConfigOp(op="run_cancel", target=ev.key, value="quiet"), "Jon", True, bot=bot, by_id=owner, via="chat"))
    assert "no line in the signup channel" in line and not bot.signup.sent
    audits = [t for _lvl, t in bot.ops.lines if "cancelled" in t and ev.key in t]
    assert len(audits) == 1 and audits[0].startswith("[chat]"), audits


def test_answer_placement_for_decides_their_own_by_id_not_by_name(reg, rs):
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    _confirm_all(bot, reg, ev)
    m, other = seated[0], seated[1]
    # an officer whose display name happens to be the member's: still "answered for you"
    run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, m.display_name, by_id=reg.config.owner_discord_id))
    card = bot.cards[reg.members[m.discord_id].placement_asks[-1]["dm"][1]]
    assert any("answered for you by" in t for t in card_texts(card))
    run(bot.answer_placement_for(reg, other.discord_id, ev.team, True, "Someone Else", by_id=other.discord_id))
    card = bot.cards[reg.members[other.discord_id].placement_asks[-1]["dm"][1]]
    assert card_texts(card) and not any("answered for you" in t for t in card_texts(card)), "their own answer, whatever the name"


def test_plain_text_use_finds_the_last_proposal_after_a_restart_in_one_write(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = NightBot(reg, rs)  # a fresh process: nothing in split_seen
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:6])
    names = [s.display_name for s in ev.signups.values() if s.status == "in"]
    # Jon (id 1) saw this split on the site before the bot restarted
    ev.last_proposal = {"layout": [names[:3], names[3:6]], "strategy": "rotation", "rev": rc.board_rev(reg, ev), "by": "Jon", "by_id": 1, "at": rc.now(), "kind": "split"}
    use = ConfigOp(op="run_propose", target=ev.key, field="use")
    with pytest.raises(RegistryError, match="no proposal to use yet"):  # another officer never applies a preview unseen
        run(configops._propose(reg, rs, ev, use, "Kara", bot, 2, None))
    assert not ev.layout
    seen = {}
    real = bot.save_board

    async def save_board(*a, **kw):
        seen.update(kw)
        return await real(*a, **kw)
    bot.save_board = save_board
    line = run(configops._propose(reg, rs, ev, use, "Jon", bot, 1, None))
    assert seen.get("strategy") == "rotation", "the strategy rides in the same save_board write"
    assert ev.layout == [names[:3], names[3:6]] and ev.split_strategy == "rotation" and "rotation proposal used" in line


def test_plain_text_preview_keeps_the_proposal_on_the_run(reg, rs, monkeypatch):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:6])
    names = [s.display_name for s in ev.signups.values() if s.status == "in"]
    monkeypatch.setattr(rc, "split_preview", lambda reg_, rs_, ev_, strategy, avoid=None: ([names[:5], names[5:]], []))
    monkeypatch.setattr(configops, "propose_text", lambda *a, **k: "shown")
    assert run(configops._propose(reg, rs, ev, ConfigOp(op="run_propose", target=ev.key), "Jon", bot, 1, None)) == "shown"
    assert ev.last_proposal["layout"] == [names[:5], names[5:]] and ev.last_proposal["by"] == "Jon" and ev.last_proposal["by_id"] == 1


def test_raid_night_levers_from_plain_text_call_the_same_verbs(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = NightBot(reg, rs)
    owner = reg.config.owner_discord_id
    ev, seated = locked_ten(reg, rs)
    _confirm_all(bot, reg, ev)

    def apply(op):
        return run(configops.apply_async(reg, op, "Jon", True, bot=bot, by_id=owner, via="chat"))
    assert "remind the unconfirmed" in configops.describe(reg, ConfigOp(op="run_remind", target=ev.key))
    assert "Reminded 10 by DM" in apply(ConfigOp(op="run_remind", target=ev.key)) and ev.reminded_by == "Jon"
    assert "pause autofill" in configops.describe(reg, ConfigOp(op="run_pause", target=ev.key, value="pause"))
    apply(ConfigOp(op="run_pause", target=ev.key, value="pause"))
    assert ev.autofill_paused
    apply(ConfigOp(op="run_pause", target=ev.key, value="resume"))
    assert not ev.autofill_paused
    with pytest.raises(RegistryError, match="pause \\| resume"):
        configops._pause_value(ConfigOp(op="run_pause", target=ev.key, value="maybe"))
    # withdraw: an officer's sent ask, closed from chat
    rc.free_seat(reg, rs, ev, seated[-1].discord_id, "no-confirm")
    plan = rc.role_plan(reg, rs, ev)
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Kara"))
    who = reg.members[sent[0].discord_id].display_name
    assert "withdraw the" in configops.describe(reg, ConfigOp(op="run_withdraw", target=ev.key, member=who))
    assert "Withdrew the ask" in apply(ConfigOp(op="run_withdraw", target=ev.key, member=who)) and sent[0].answer == "withdrawn"
    audits = [t for _l, t in bot.ops.lines if t.startswith("[chat]")]
    assert len([t for t in audits if "reminded" in t]) == 1, "the verb writes the one audit line"


def test_reopen_from_plain_text_and_a_cancelled_run_is_refused(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = NightBot(reg, rs)
    ev, _seated = locked_ten(reg, rs)
    ev.prev_state, ev.state, ev.closed_at = "locked", "done", rc.now()
    rs.save(ev, "ended by mistake")
    assert "reopen" in configops.describe(reg, ConfigOp(op="run_reopen", target=ev.key))
    line = run(configops.apply_async(reg, ConfigOp(op="run_reopen", target=ev.key), "Jon", True, bot=bot, by_id=reg.config.owner_discord_id, via="chat"))
    assert ev.state == "locked" and "Reopened" in line
    run(bot.cancel_run(reg, rs, ev, "Kara", None, notify=False))
    with pytest.raises(RegistryError, match="never reopened"):
        run(configops.apply_async(reg, ConfigOp(op="run_reopen", target=ev.key), "Jon", True, bot=bot, via="chat"))


def test_mcp_tools_for_the_levers_post_to_the_run_routes():
    import httpx

    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        calls.append((request.method, request.url.path, _json.loads(request.read() or b"{}")))
        return httpx.Response(200, json={"message": "ok"})
    ms.use(ms.Api(url="http://bot", token="t", transport=httpx.MockTransport(handler)))
    try:
        ms.remind_unconfirmed("bd-1")
        ms.pause_autofill("bd-1", paused=False)
        ms.withdraw_ask("bd-1", "Kessa")
        ms.reopen_run("bd-1")
        ms.cancel_run("bd-1", "server down", notify=False)
    finally:
        ms.use(None)
    assert [(m, p) for m, p, _b in calls] == [("POST", "/api/run/bd-1/remind"), ("POST", "/api/run/bd-1/pause"), ("POST", "/api/run/bd-1/withdraw"),
                                              ("POST", "/api/run/bd-1/reopen"), ("POST", "/api/run/bd-1/cancel")]
    assert calls[1][2] == {"paused": False} and calls[2][2] == {"member": "Kessa"} and calls[4][2] == {"reason": "server down", "notify": False}


# ---------------------------------------------------------------- final fixer: regressions

def test_lock_fanout_never_asks_a_member_moved_off_the_board_meanwhile(reg, rs, monkeypatch):
    """The lock's Confirm / Can't fan-out lists the seats once and runs outside board_lock: a seat freed while the DMs
    go out (an officer's drag to the bank) must get no ask — confirm_one re-reads the seat."""
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    monkeypatch.setattr(rc, "propose", fast_propose([m.display_name for m in seated]))
    gone = seated[-1]

    async def go():
        bot.dm_gate = asyncio.Event()  # the first confirmation DM hangs: the lock is mid fan-out
        lock = asyncio.create_task(bot.lock_run(reg, rs, ev, by="Jon"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ev.state == "locked" and any(a["roster"] == ev.team for m in seated for a in reg.members[m.discord_id].placement_asks):
                break
        assert ev.state == "locked"
        move = asyncio.create_task(bot.board_move(reg, rs, ev, gone.display_name, None, by="Kara"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not ev.seat_of(gone):
                break
        assert not ev.seat_of(gone), "the drag to the bank freed the seat"
        bot.dm_gate.set()
        await lock
        await move

    run(go())
    open_asks = [a for a in reg.members[gone.discord_id].placement_asks if a["roster"] == ev.team and a.get("answer") is None]
    assert not ev.seat_of(gone) and not open_asks, "no seat, so no Confirm / Can't ask"
    assert all(any(a["roster"] == ev.team for a in reg.members[m.discord_id].placement_asks) for m in seated[:-1]), "everyone still seated was asked"


def test_opening_at_a_cancelled_runs_time_is_refused_and_the_preview_says_so(reg, rs, web_bot):
    """open_event used to hand the cancelled run back ('opened <key>') — nothing opened, the run stayed cancelled."""
    bot, c = web_bot.bot, web_bot.c
    wed = reg.now_local().date() + timedelta(days=(2 - reg.now_local().weekday()) % 7 + 7)
    reg.set_raid_overrides("barrow_deeps", {"slots": "Wed 20:00", "first_open": "2026-01-01T00:00"}, "t")
    p = c.post("/api/raid/barrow_deeps/open/preview", json={"when": f"{wed.isoformat()} 20:00"}, headers=H()).json()
    assert c.post("/api/raid/barrow_deeps/open", json={"start": p["start"], "key": p["key"]}, headers=H()).status_code == 200
    ev = rs.events[p["key"]]
    run(bot.cancel_run(reg, rs, ev, "Jon", None, notify=False))
    run(bot.cleanup_ephemeral(reg, rs))
    assert not reg.config.team(ev.team)
    opened_lines = sum("opened" in t for _, t in bot.ops.lines)
    p2 = c.post("/api/raid/barrow_deeps/open/preview", json={"when": f"{wed.isoformat()} 20:00"}, headers=H()).json()
    assert p2["closed"] == "cancelled" and "was cancelled" in p2["refusal"] and p2["exists"] is None
    r = c.post("/api/raid/barrow_deeps/open", json={"start": p2["start"], "key": p2["key"]}, headers=H())
    assert r.status_code == 400 and "was cancelled" in r.json()["error"], r.text
    assert ev.state == "cancelled" and not reg.config.team(ev.team), "a refused open re-creates no roster entry"
    assert sum("opened" in t for _, t in bot.ops.lines) == opened_lines, "no 'opened' audit line for a refused open"
    with pytest.raises(RegistryError, match="was cancelled"):
        run(bot.open_run_and_post(reg, rs, "barrow_deeps", ev.start, by="Kara"))


def test_a_test_run_opened_after_clearing_one_the_same_minute_is_a_live_run(reg, rs):
    bot = NightBot(reg, rs)
    reg.seed_test_members(10, "t")
    ev, _ = run(bot.open_test_run(reg, rs, "barrow_deeps", start_in=10, nudge_in=8, lock_in=6, confirm_in=3, dm=False, by="Jon", by_id=1, channel=bot.signup))
    run(bot.cancel_run(reg, rs, ev, "Jon", None, notify=False))  # what clearing the bench does to its runs
    ev2, _ = run(bot.open_test_run(reg, rs, "barrow_deeps", start_in=10, nudge_in=8, lock_in=6, confirm_in=3, dm=False, by="Jon", by_id=1, channel=bot.signup))
    assert ev2.key != ev.key and ev2.state == "open" and ev.state == "cancelled", "a fresh rehearsal, never the cleared one handed back"
    assert ev2.key.endswith(f"-test-{ev2.start.date().isoformat()}"), "still in the '-test' key space"


def test_rosters_page_open_of_the_raids_own_scheduled_run_from_the_adhoc_picker(reg, rs, web_bot):
    """Rosters.tsx StartAdhoc: a one-off time that IS the raid's own (default) run previews as 'scheduled' with schedule
    null; the page then sends schedule 'default' (it used to send 'adhoc' → 409, the key never matched)."""
    c = web_bot.c
    wed = reg.now_local().date() + timedelta(days=(2 - reg.now_local().weekday()) % 7 + 7)
    reg.set_raid_overrides("barrow_deeps", {"slots": "Wed 20:00", "first_open": "2026-01-01T00:00"}, "t")
    p = c.post("/api/raid/barrow_deeps/open/preview", json={"when": f"{wed.isoformat()} 20:00", "schedule": ""}, headers=H()).json()
    assert p["kind"] == "scheduled" and p["schedule"] is None
    tpl = "adhoc"  # the page's template select: One-off
    sent = {"start": p["start"], "key": p["key"],
            "schedule": (p["schedule"] or "default") if p["kind"] == "scheduled" else (p["schedule"] or ("adhoc" if tpl == "adhoc" else tpl))}
    r = c.post("/api/raid/barrow_deeps/open", json=sent, headers=H())
    assert r.status_code == 200 and r.json()["key"] == p["key"], r.text
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "frontend/src/pages/Rosters.tsx").read_text()
    assert 'p.kind === "scheduled" ? (p.schedule || "default")' in page, "the page sends what this test sends"
    old = {**sent, "schedule": "adhoc"}  # what the page sent before the fix
    assert c.post("/api/raid/barrow_deeps/open", json=old, headers=H()).status_code == 409


def test_plain_text_use_refuses_a_proposal_solved_on_an_older_board(reg, rs):
    """'use it' compares the board rev stored with the proposal: a split shown hours ago never overwrites the moves
    other officers made since (the restart path through ev.last_proposal and the in-memory path alike)."""
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:6])
    names = [s.display_name for s in ev.signups.values() if s.status == "in"]
    ev.last_proposal = {"layout": [names[:3], names[3:6]], "strategy": "rotation", "rev": rc.board_rev(reg, ev), "by": "Jon", "by_id": 1, "at": rc.now(), "kind": "split"}
    run(bot.board_move(reg, rs, ev, names[0], (0, 0), by="Kara"))  # another officer's move after the proposal
    moved = rc.board_layout(reg, ev)
    use = ConfigOp(op="run_propose", target=ev.key, field="use")
    with pytest.raises(rc.BoardChanged, match="ask for a fresh one"):
        run(configops._propose(reg, rs, ev, use, "Jon", bot, 1, None))
    assert rc.board_layout(reg, ev) == moved and ev.split_strategy != "rotation", "nothing written"
    configops.split_seen(bot)[(reg.key, ev.key, 1)] = {"strategy": "rotation", "layouts": [[names[:3], names[3:6]]], "rev": "stale0000000"}
    with pytest.raises(rc.BoardChanged):
        run(configops._propose(reg, rs, ev, use, "Jon", bot, 1, None))


def test_a_members_own_fill_card_is_closed_when_their_ask_is_released(reg, rs):
    """release_asks_of edited only the tied partners' cards: the member's own fill DM kept live Yes / No after their
    callout / No thanks closed the ask."""
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Jon"))
    a = sent[0]
    m = reg.members[a.discord_id]
    run(bot.call_out(reg, rs, ev, m, None, by="Jon", by_id=1))
    assert a.answer == "released"
    texts = card_texts(bot.cards[a.dm_message_id])
    assert any("Never mind" in t and "called out" in t for t in texts), texts


def test_an_expired_fill_ask_closes_its_own_card(reg, rs):
    bot = NightBot(reg, rs)
    ev, _seated = short_one(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Jon"))
    a = sent[0]
    a.expires_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()
    rs.save(ev, "deadline")
    run(bot.scheduler_tick_guild(reg))
    assert a.answer == "expired"
    assert any("without an answer" in t for t in card_texts(bot.cards[a.dm_message_id])), "the card says it closed — no live Yes / No"


def test_prelock_asks_closed_by_the_lock_close_their_cards(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    monkeypatch.setattr(rc, "propose", fast_propose([m.display_name for m in seated]))
    asks = [rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="tank", character=m.main.name, spec=m.main.spec, reason="short a tank")
            for m in pool[-2:]]
    run(bot._send_asks(reg, rs, ev, rc.run_team(reg, ev), asks, phase="prelock", approved_by="Jon", deadline=ev.start.isoformat()))
    assert all(a.dm_message_id for a in asks)
    run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert all(a.answer == "released" for a in asks)
    for a in asks:
        assert any("locked before you answered" in t for t in card_texts(bot.cards[a.dm_message_id])), a.display_name
    # the scheduler's lock-time close (close_prelock_asks) does the same
    ev2 = open_test_run(reg, rs, days=5)
    more = [rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="tank", character=m.main.name, spec=m.main.spec, reason="short a tank")
            for m in pool[:1]]
    run(bot._send_asks(reg, rs, ev2, rc.run_team(reg, ev2), more, phase="prelock", approved_by="Jon", deadline=ev2.start.isoformat()))
    run(bot.close_prelock_asks(reg, rs, ev2))
    assert any("locked before you answered" in t for t in card_texts(bot.cards[more[0].dm_message_id]))


def test_expired_confirmation_card_is_found_by_id_after_a_rename(reg, rs):
    """The scheduler found the member by display name: a rename since the Join (the signup keeps the old name) left the
    Confirm card with live buttons."""
    bot = NightBot(reg, rs)
    ev, seated = locked_ten(reg, rs)
    m = seated[2]
    run(bot.confirm_one(reg, ev, ev.signups[str(m.discord_id)], 0, 0, "lock"))
    reg.rename(m.discord_id, "Renamed Since", "t")
    past_confirm(ev)
    rs.save(ev, "deadline")
    run(bot.scheduler_tick_guild(reg))
    assert not ev.seat_of(m)
    card = bot.cards[reg.members[m.discord_id].placement_asks[-1]["dm"][1]]
    assert any("was released" in t for t in card_texts(card))
    assert rc.expire_confirmation_asks(reg, rs, ev) == []
