"""Roster redesign Phase 2 review fixes, each a regression test that fails without its fix:
- officer-only data stays with officers: /raid health is open to members, and their card leaves out the Suggested
  block, the regulars who haven't answered, who could cover, who is double-booked and the officer buttons;
- D3: a run whose role swap waits for an officer ("suggested") still gets automatic same-role asks from the
  scheduler for a seat freed later (the worked example's "Bo never confirmed → fill asks Quill");
- test puppets never reach a real run: not suggested, asked, nudged or listed on an ad hoc run or a series with no
  regulars (the pool there is every REAL main), before or after lock; a puppet's absence never marks a real sheet;
- the scheduler closes pre-lock asks at lock time as "released" (not "expired");
- a tied pair (a swap + the backfill covering the seat it vacates) needs BOTH yeses: the first yes is held and
  changes nothing; a No or silence from the partner releases it; the second yes applies both — before and after lock;
- an officer's send after lock keeps the FILL_MAX_OPEN cap, and the officers' plan after lock counts the asks still
  out (nobody asked twice for one seat) while the fill engine keeps its over-ask;
- the cards' Ask and Suggest… buttons driven through a fake interaction; the Suggest… screen refuses one person
  picked for two seats."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from conftest import join, lock_with_board
from test_analytics_cards import text_of
from test_flows_raid import FakeBot as FlowBot
from test_flows_raid import channel, officer  # noqa: F401 — the flows' fixtures
from test_parity_verbs import Bot, Chan
from test_roster_p2_asks import _locked_short_healer
from test_roster_p2_planner import DPS8, KEY, RID, _joined_series, add, worked_example
from wizard_harness import Harness

from oibot_gm import flows_raid  # noqa: F401 — registers the flows
from oibot_gm import raid_scheduler
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.raid_scheduler import RaidSchedulerMixin
from oibot_gm.registry import ADHOC_SCHEDULE, RegistryError
from oibot_gm.wizard import FLOWS

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def ids(view) -> list[str]:
    return [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]


class CardBot(Bot):
    """The real RaidMixin behind the cards' buttons: registries resolve the interaction, officer per flag."""

    def __init__(self, reg, rs, officer: bool = True):
        super().__init__(reg, rs)
        self.registries.for_interaction = lambda i: reg
        self._officer = officer

    async def is_officer_anywhere(self, reg, uid):
        return self._officer


class SchedBot(RaidSchedulerMixin, Bot):
    events: dict = {}

    def officer_channel(self, reg, ev=None):
        return None


# ---------------------------------------------------------------- officer-only data never reaches members

def test_member_health_card_leaves_out_the_officer_parts(reg, rs):
    ev, p = worked_example(reg, rs)
    team = rc.run_team(reg, ev)
    full = views.health_layout(reg, rs, ev, team, ico)
    assert "**Suggested**" in text_of(full) and "**No answer**" in text_of(full) and any(i.startswith("runact:") for i in ids(full))
    member = views.health_layout(reg, rs, ev, team, ico, officer=False)
    body = text_of(member)
    assert "**Short**" in body and "locks <t:" in body, "the counts and what is short stay"
    for hidden in ("**Suggested**", "**No answer**", "Kael", "Ana", "Brannoc", "a regular, hasn't answered", "declared flex"):
        assert hidden not in body, hidden
    assert not ids(member), "no officer buttons on a member's card"


def test_raid_health_for_a_member_is_the_redacted_card(reg, rs, channel):  # noqa: F811
    ev, p = worked_example(reg, rs)
    bot = FlowBot(rs)
    h = Harness(reg, rs, client=bot)  # not an officer (discord_registry.is_officer is real here)
    run(FLOWS["raid_health"](h.command(), reg))
    body = text_of(h.screen["view"])
    assert "**Short**" in body and "**Suggested**" not in body and "**No answer**" not in body and "Kael" not in body


def test_raid_health_for_an_officer_is_the_full_card(reg, rs, officer, channel):  # noqa: F811
    ev, p = worked_example(reg, rs)
    h = Harness(reg, rs, client=FlowBot(rs))
    run(FLOWS["raid_health"](h.command(), reg))
    body = text_of(h.screen["view"])
    assert "**Suggested**" in body and "**No answer**" in body


# ---------------------------------------------------------------- D3: "suggested" doesn't stop same-role fill

def test_scheduler_keeps_filling_same_role_seats_after_a_swap_waits_for_an_officer(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    bot = SchedBot(reg, rs)
    run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))
    assert ev.fill_state == "suggested" and not bot.dms, "the role swap waits for an officer"
    quill = add(reg, 398, "Quill", "Rogue", "Combat")  # not a regular, no answer: a same-role body for a DPS seat
    rc.free_seat(reg, rs, ev, "Dax", "no-confirm")  # a DPS seat freed later (an expired confirmation)
    run(bot.scheduler_tick())
    asked = [(a.display_name, a.kind) for a in ev.fill_asks]
    assert ("Quill", "other") in asked and quill.discord_id in bot.dms, "same-role fill asks stay automatic"
    assert not [a for a in ev.fill_asks if a.swap], "the swap still waits for an officer"
    assert ev.fill_state == "asking"


def test_scheduler_in_suggested_state_writes_nothing_while_nothing_changes(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    bot = SchedBot(reg, rs)
    run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))
    log = list(ev.log)
    run(bot.scheduler_tick())
    run(bot.scheduler_tick())
    assert ev.fill_state == "suggested" and ev.log == log and not bot.dms, "no churn: said once, stays suggested"


# ---------------------------------------------------------------- test puppets never reach a real run

def _adhoc_short_healer(reg, rs):
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    rows = [("Tanka", "Warrior", "Protection"), ("Tankb", "Paladin", "Protection"), ("Heala", "Priest", "Holy")] + DPS8[:5]
    ms = [add(reg, 820 + i, *row) for i, row in enumerate(rows)]
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    ev = rc.open_run(reg, rs, RID, t, by="t", schedule=ADHOC_SCHEDULE)
    join(reg, rs, ev, ms)
    return ev, ms


def test_puppets_are_never_suggested_or_asked_on_a_real_ad_hoc_run(reg, rs):
    puppets = {m.discord_id for m in reg.test_members()}
    assert puppets and any(reg.profile.spec(m.main.cls, m.main.spec).role == "healer" for m in reg.test_members()), "the bench has healers"
    ev, ms = _adhoc_short_healer(reg, rs)
    assert rc.run_kind(reg, ev) == "adhoc"
    assert not puppets & {m.discord_id for m, _ in rc.run_pool(reg, ev)}, "the pool is every REAL main"
    before = rc.role_plan(reg, rs, ev)
    assert not puppets & {a.discord_id for st in before.seats for opt in st.options for a in opt}
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    after = rc.role_plan(reg, rs, ev)
    assert not puppets & {a.discord_id for st in after.seats for opt in st.options for a in opt}
    bot = Bot(reg, rs)
    run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))  # ad hoc: the engine asks on its own
    assert not puppets & set(bot.dms) and not puppets & {a.discord_id for a in ev.fill_asks}


def test_a_series_with_no_regulars_expects_real_mains_only(reg, rs):
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    real = add(reg, 880, "Realone", "Priest", "Holy")
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    ev = rc.open_run(reg, rs, RID, t, by="t")
    assert rc.expected_from(reg, ev) == "everyone"
    waiting = {m.discord_id for m, _ in rc.not_answered(reg, ev)}
    assert real.discord_id in waiting and not waiting & {m.discord_id for m in reg.test_members()}, "no puppet in 'No answer' or the nudge"


def test_a_puppets_absence_never_marks_a_real_sheet(reg, rs):
    ev, ms = _adhoc_short_healer(reg, rs)
    puppet = reg.test_members()[0]
    day = ev.start.astimezone(reg.tz).date().isoformat()
    assert rc.absence_effects(reg, rs, puppet, day, day) == []
    real = ms[0]
    assert [e.key for e, _ in rc.absence_effects(reg, rs, real, day, day)] == [ev.key]


# ---------------------------------------------------------------- the scheduler closes pre-lock asks as "released"

def test_scheduler_closes_prelock_asks_as_released_at_lock_time(reg, rs, monkeypatch):
    ev, p = worked_example(reg, rs)
    bot = SchedBot(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Officer"))
    seen = []

    async def lock_run(reg_, rs_, ev_, by="scheduler"):
        seen.extend(a.answer for a in ev_.fill_asks)
        raise RegistryError("not locking in this test")
    bot.lock_run = lock_run
    past = reg.now_local() - timedelta(minutes=1)
    monkeypatch.setattr(raid_scheduler, "run_times", lambda reg_, ev_, team: (past, past, ev_.start))  # reminder and lock time have passed
    run(bot.scheduler_tick())
    assert seen == ["released", "released"], "closed at lock time as released (FillButton: 'closed when the roster locked')"


# ---------------------------------------------------------------- a tied pair needs both yeses

def _prelock_pair(reg, rs):
    ev, p = worked_example(reg, rs)
    quill = add(reg, 60, "Quill", "Rogue", "Combat")
    kael, q = p["Kael"], quill
    swap = rc.FillAsk(discord_id=kael.discord_id, display_name="Kael", kind="offspec", role="tank", character="Kael", spec="Protection",
                      reason="short 1 tank", vacates="melee", pair=q.discord_id, phase="prelock", approved_by="Officer")
    back = rc.FillAsk(discord_id=q.discord_id, display_name="Quill", kind="other", role="melee", character="Quill", spec="Combat",
                      reason="covering Kael's melee seat", pair=kael.discord_id, phase="prelock", approved_by="Officer")
    ev.fill_asks += [swap, back]
    return ev, p, quill, swap, back


def test_first_yes_of_a_pair_is_held_and_changes_nothing(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    line = rc.apply_fill_answer(reg, rs, ev, swap, True)
    assert swap.held and swap.answer == "yes" and "waiting for Quill" in line
    assert str(p["Kael"].discord_id) not in ev.signups, "nothing changes until both say yes"
    assert rc.partner_of(ev, back) is swap, "the held yes is still in play"
    rc.apply_fill_answer(reg, rs, ev, back, False)
    released = rc.release_partner(reg, rs, ev, back)  # what after_fill_answer does on a no
    assert released is swap and (swap.answer, swap.held) == ("released", False)
    assert str(p["Kael"].discord_id) not in ev.signups, "the partner said no: Kael's signup never changed"


def test_second_yes_applies_both_halves(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    rc.apply_fill_answer(reg, rs, ev, back, True)  # either half can answer first
    assert back.held and str(quill.discord_id) not in ev.signups
    line = rc.apply_fill_answer(reg, rs, ev, swap, True)
    k, q = ev.signups[str(p["Kael"].discord_id)], ev.signups[str(quill.discord_id)]
    assert (k.spec, k.role, k.status) == ("Protection", "tank", "in") and (q.status, q.role) == ("in", "melee")
    assert not back.held and back.answer == "yes" and "Kael" in line and "Quill" in line


def test_partner_silence_releases_a_held_yes(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    rc.apply_fill_answer(reg, rs, ev, swap, True)
    back.expires_at = (datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()
    expired, released = rc.expire_fill_asks(reg, rs, ev)
    assert expired == [back] and released == [swap] and swap.answer == "released"
    assert str(p["Kael"].discord_id) not in ev.signups


def test_lock_releases_a_held_yes(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    rc.apply_fill_answer(reg, rs, ev, swap, True)
    assert set(map(id, rc.release_prelock_asks(ev))) == {id(swap), id(back)}
    assert (swap.answer, swap.held, back.answer) == ("released", False, "released")


def test_held_yes_counts_as_an_ask_out(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    rc.apply_fill_answer(reg, rs, ev, swap, True)
    plan = rc.role_plan(reg, rs, ev)
    assert "Kael" in plan.waiting and "Quill" in plan.waiting
    assert "tank" not in plan.short["roles"], "the held swap still covers its seat while the partner decides"


def test_bot_tells_the_held_partner_when_both_said_yes(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    bot = Bot(reg, rs)
    line = rc.apply_fill_answer(reg, rs, ev, swap, True)
    run(bot.after_fill_answer(reg, rs, ev, swap, line))
    assert not bot.dms, "a held yes withdraws nobody and tells nobody yet"
    line = rc.apply_fill_answer(reg, rs, ev, back, True)
    run(bot.after_fill_answer(reg, rs, ev, back, line))
    dm = bot.dms[p["Kael"].discord_id][-1]
    assert "Quill said yes too" in dm and "**Kael** (Protection)" in dm


def test_bot_withdraws_a_held_yes_when_the_partner_says_no(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    bot = Bot(reg, rs)
    rc.apply_fill_answer(reg, rs, ev, swap, True)
    line = rc.apply_fill_answer(reg, rs, ev, back, False)
    run(bot.after_fill_answer(reg, rs, ev, back, line))
    assert swap.answer == "released" and "Never mind the swap question" in bot.dms[p["Kael"].discord_id][-1]


def test_post_lock_pair_needs_both_yeses(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    spare = add(reg, 397, "Spare", "Rogue", "Combat")
    offy = people["Offy"]
    swap = rc.FillAsk(discord_id=offy.discord_id, display_name="Offy", kind="offspec", role="healer", character="Offy", spec="Restoration",
                      reason="short 1 healer", vacates="melee", pair=spare.discord_id, approved_by="Officer")
    back = rc.FillAsk(discord_id=spare.discord_id, display_name="Spare", kind="other", role="melee", character="Spare", spec="Combat",
                      reason="covering Offy's melee seat", pair=offy.discord_id, approved_by="Officer")
    ev.fill_asks += [swap, back]
    rc.apply_fill_answer(reg, rs, ev, swap, True)
    assert swap.held and ev.seat_of("Offy")[1].spec == "Enhancement", "the seat doesn't switch on one yes"
    rc.apply_fill_answer(reg, rs, ev, back, True)
    assert ev.seat_of("Offy")[1].spec == "Restoration" and ev.seat_of("Spare"), "both happen on the second yes"


def test_fill_dm_for_a_pair_says_both_must_agree(reg, rs):
    ev, p, quill, swap, back = _prelock_pair(reg, rs)
    lock_with_board(reg, rs, ev, [p[n].display_name for n in ("Brannoc", "Mira", "Kael", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren")])
    swap.phase = back.phase = "fill"
    body = text_of(views.fill_layout(reg, ev, rc.run_team(reg, ev), ico, swap))
    assert "the swap only happens if you both say yes" in body and "Confirm counts once you both say yes." in body
    assert "makes the swap at once" not in body


# ---------------------------------------------------------------- after lock: the cap and the asks still out

def _locked_with_free_dps(reg, rs, free: int = 4, spares: int = 6):
    rows = [("Tanka", "Warrior", "Protection"), ("Tankb", "Paladin", "Protection"), ("Heala", "Priest", "Holy"), ("Healb", "Shaman", "Restoration")] + DPS8
    ev, people = _joined_series(reg, rs, rows)
    lock_with_board(reg, rs, ev, list(people))
    for name in [r[0] for r in DPS8[:free]]:
        rc.free_seat(reg, rs, ev, name, "declined")
    extra = [add(reg, 900 + i, f"Spare{'abcdefgh'[i]}", "Rogue", "Combat") for i in range(spares)]
    return ev, extra


def _ask_out(m) -> rc.FillAsk:
    return rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="melee", character=m.display_name, spec="Combat", reason="short")


def test_officer_plan_after_lock_counts_the_asks_still_out(reg, rs):
    ev, extra = _locked_with_free_dps(reg, rs)
    assert rc.needs(reg, ev)["headcount"] == 4
    ev.fill_asks += [_ask_out(extra[0]), _ask_out(extra[1])]
    plan = rc.role_plan(reg, rs, ev)
    assert plan.short["headcount"] == 2 and len(plan.asks) == 2, "two seats are being asked about: the plan suggests the other two"
    engine = rc.fill_batch(reg, rs, ev)
    assert len(engine) == 1, "the fill engine keeps its over-ask (shortfall + 1, at most 3 out)"


def test_officer_send_after_lock_keeps_the_cap(reg, rs):
    ev, extra = _locked_with_free_dps(reg, rs)
    ev.fill_asks += [_ask_out(extra[0]), _ask_out(extra[1])]
    bot = Bot(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    sent, after = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Officer"))
    assert len(sent) == 1 and after.held_back == 1, "at most FILL_MAX_OPEN asks out, an officer's send included"
    assert sum(1 for a in ev.fill_asks if a.pending) == rc.FILL_MAX_OPEN
    assert "1 more wait" in rc.held_back_words(after)
    with pytest.raises(RegistryError, match="at most 3"):
        run(bot.send_plan(reg, rs, ev, rev=rc.role_plan(reg, rs, ev).rev, by="Officer"))


# ---------------------------------------------------------------- the cards' Ask and Suggest… buttons

def test_card_ask_button_sends_through_send_plan(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    rev = rc.role_plan(reg, rs, ev).rev
    run(views.RunButton(ev.key, "ask", rev=rev).callback(h.card_press()))
    assert h.text.startswith("Asked Kael, Ana") and set(bot.dms) == {p["Kael"].discord_id, p["Ana"].discord_id}
    assert all(a.phase == "prelock" and a.approved_by == "Tester" for a in ev.fill_asks)


def test_card_ask_button_with_a_stale_rev_sends_nothing(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    old = rc.role_plan(reg, rs, ev).rev
    rc.set_signup(reg, rs, ev, p["Gorm"], None, "in")
    run(views.RunButton(ev.key, "ask", rev=old).callback(h.card_press()))
    assert "The sheet changed" in h.text and not ev.fill_asks and not bot.dms


def test_card_ask_button_refuses_a_non_officer(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = CardBot(reg, rs, officer=False)
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(ev.key, "ask", rev=rc.role_plan(reg, rs, ev).rev).callback(h.card_press()))
    assert h.text == "Officers only." and not ev.fill_asks


def test_card_suggest_button_opens_the_screen_and_sends(reg, rs, officer):  # noqa: F811
    ev, p = worked_example(reg, rs)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(ev.key, "suggest").callback(h.card_press()))
    assert "Kael → Protection (offspec)" in h.text and not bot.dms, "nothing goes out before Send"
    run(h.press("Send 2 asks"))
    assert set(bot.dms) == {p["Kael"].discord_id, p["Ana"].discord_id} and "Asked Kael, Ana" in h.text


def test_suggest_screen_refuses_one_person_for_two_seats(reg, rs, officer):  # noqa: F811
    rows = [("Tank", "Warrior", "Protection"), ("Heal", "Priest", "Holy"), ("Kael", "Warrior", "Fury", "Protection"),
            ("Ana", "Paladin", "Retribution", None, ["healer"]), ("Duo", "Druid", "Balance", "Guardian", ["healer"])] + DPS8[:5]
    ev, people = _joined_series(reg, rs, rows)
    plan = rc.role_plan(reg, rs, ev)
    t = next(i for i, st in enumerate(plan.seats) if st.role == "tank")
    hl = next(i for i, st in enumerate(plan.seats) if st.role == "healer")
    duo_t = next(k for k, opt in enumerate(plan.seats[t].options) if opt[0].display_name == "Duo")
    duo_h = next(k for k, opt in enumerate(plan.seats[hl].options) if opt[0].display_name == "Duo")
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(FLOWS["raid_suggest"](h.command(), reg, key=ev.key))
    run(h.pick(next(s for s in h.selects() if "(tank)" in s), str(duo_t)))
    run(h.pick(next(s for s in h.selects() if "(healer)" in s), str(duo_h)))
    assert "⚠️ Duo is picked for two seats" in h.text
    send = next(b for b in h._items() if getattr(b, "label", "").startswith("Send"))
    assert send.disabled and not bot.dms, "Send stays off until a pick changes"
