"""Roster redesign Phase 2 — suggestions reach people only through an officer, as amended by the owner:
- send_plan (the officer's Ask): pre-lock asks by DM, one per member per run, expiring at lock; a stale plan
  fingerprint is refused (nothing sent) and the current plan handed back; a yes changes the signup and seats nobody;
- a run that locks mid-ask: the answer takes the post-lock path; unanswered pre-lock asks close at lock;
- D3: after lock on a RECURRING run the fill engine sends same-role asks on its own, a ROLE SWAP waits for an officer
  (fill_state "suggested", the lock card's Suggested block); an officer's Send includes swaps; ad hoc stays automatic;
- the officer cards: the health card's Suggested block with Ask (the rev rides in its id) / Suggest…; the early
  warning at sheet open; the lock card's "Locked short", solver spec switches and benched regulars; the Confirm DM
  names the spec they're rostered as."""
from __future__ import annotations

import asyncio

import pytest
from conftest import first_slot, join, lock_with_board
from test_analytics_cards import text_of
from test_parity_verbs import Bot, Chan
from test_roster_p2_planner import DPS8, KEY, RID, _joined_series, add, worked_example

from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.registry import ADHOC_SCHEDULE

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def buttons(view) -> list:
    return [c for c in view.walk_children() if hasattr(c, "custom_id") and getattr(c, "custom_id", None)]


# ---------------------------------------------------------------- the officer's Ask before lock

def test_ask_sends_prelock_dms_that_expire_at_lock(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = Bot(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Officer"))
    assert [a.display_name for a in sent] == ["Kael", "Ana"]
    _soft, hard, _c = views.run_times(reg, ev, rc.run_team(reg, ev))
    for a in ev.fill_asks:
        assert a.phase == "prelock" and a.approved_by == "Officer" and a.expires_at == min(hard, ev.start).isoformat()
    dm = text_of(bot.dms[p["Kael"].discord_id][0])
    assert "is short a tank" in dm and "Could you come as" in dm and "**Protection** on **Kael**" in dm and "built at lock" in dm
    labels = [getattr(b, "item", b).label for b in buttons(bot.dms[p["Kael"].discord_id][0])]
    assert labels == ["Yes, as Protection", "No"]
    assert ev.state == "open" and not ev.rosters, "nothing seated"
    again = rc.role_plan(reg, rs, ev)
    assert not again.asks, "one ask per member per run: Kael and Ana are not suggested again while waiting"


def test_a_stale_ask_is_refused_and_nothing_is_sent(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = Bot(reg, rs)
    old = rc.role_plan(reg, rs, ev).rev
    rc.set_signup(reg, rs, ev, p["Gorm"], None, "in")  # the plan moved (an alternate joined)
    with pytest.raises(rc.PlanChanged) as e:
        run(bot.send_plan(reg, rs, ev, rev=old, by="Officer"))
    assert e.value.plan.rev == rc.role_plan(reg, rs, ev).rev, "the refusal carries the plan as it is now"
    assert not ev.fill_asks and not bot.dms
    with pytest.raises(rc.PlanChanged):
        run(bot.send_plan(reg, rs, ev, rev="", by="Officer"))


def test_an_officer_can_ask_an_alternate_instead(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = Bot(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    h = next(i for i, st in enumerate(plan.seats) if st.role == "healer")
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Officer", picks={h: 1}))
    assert [a.display_name for a in sent] == ["Kael", "Wren"] and sent[1].character == "Fern"


def test_a_prelock_yes_after_the_run_locked_takes_the_post_lock_path(reg, rs):
    ev, p = worked_example(reg, rs)
    ten = [p[n] for n in ("Brannoc", "Mira", "Kael", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren")]
    join(reg, rs, ev, ten)
    ask = rc.FillAsk(discord_id=p["Kael"].discord_id, display_name="Kael", kind="offspec", role="tank", character="Kael", spec="Protection", reason="short 1 tank", phase="prelock")
    ev.fill_asks.append(ask)
    lock_with_board(reg, rs, ev, [m.display_name for m in ten])  # locked while the ask was out
    line = rc.apply_fill_answer(reg, rs, ev, ask, True)
    seat = ev.seat_of("Kael")[1]
    assert (seat.spec, seat.role) == ("Protection", "tank") and "swaps to Protection" in line, "the seat switched in place"


def test_unanswered_prelock_asks_close_at_lock(reg, rs):
    ev, p = worked_example(reg, rs)
    open_ask = rc.FillAsk(discord_id=p["Kael"].discord_id, display_name="Kael", kind="offspec", role="tank", character="Kael", spec="Protection", reason="t", phase="prelock")
    answered = rc.FillAsk(discord_id=p["Ana"].discord_id, display_name="Ana", kind="flex", role="healer", character="Ana", spec="Holy", reason="t", phase="prelock", answer="no")
    after = rc.FillAsk(discord_id=p["Gorm"].discord_id, display_name="Gorm", kind="other", role="tank", character="Gorm", spec="Protection", reason="t")
    ev.fill_asks += [open_ask, answered, after]
    assert rc.release_prelock_asks(ev) == [open_ask]
    assert (open_ask.answer, answered.answer, after.answer) == ("released", "no", None)
    assert "pre-lock asks closed at lock: Kael" in ev.log[-1]


@pytest.mark.slow
def test_lock_after_suggestions_releases_open_asks_and_cards_say_what_happened(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = Bot(reg, rs)
    reg.config.roster_channel_id = 77
    bot.channels[77] = Chan(77, "roster")
    plan = rc.role_plan(reg, rs, ev)
    run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Officer"))
    kael = next(a for a in ev.fill_asks if a.display_name == "Kael")
    rc.apply_fill_answer(reg, rs, ev, kael, True)  # Kael agrees; Ana never answers
    join(reg, rs, ev, [p[n] for n in ("Brannoc", "Mira", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren")])
    run(bot.lock_run(reg, rs, ev, by="Officer"))
    assert ev.state == "locked"
    assert next(a for a in ev.fill_asks if a.display_name == "Ana").answer == "released"
    card = text_of(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs))
    if rc.needs(reg, ev)["roles"]:
        assert "**Locked short**" in card
    dms = bot.dms[p["Kael"].discord_id]
    confirm = text_of(dms[-1])
    assert "**Kael** · Group" in confirm and "You're rostered as **Protection**, your offspec — as you agreed." in confirm  # the bot's ico is a stub here


# ---------------------------------------------------------------- D3: after lock, role swaps need an officer

def _locked_short_healer(reg, rs, adhoc: bool = False):
    """A locked 10-seat run, one healer seat freed; the only cover is a seated DPS's healer offspec (a role swap)."""
    rows = [("Tanka", "Warrior", "Protection"), ("Tankb", "Paladin", "Protection"), ("Heala", "Priest", "Holy"), ("Healb", "Shaman", "Restoration"),
            ("Offy", "Shaman", "Enhancement", "Restoration")] + DPS8[:5]
    if adhoc:
        reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
        ms = [add(reg, 300 + i, *row) for i, row in enumerate(rows)]
        assert reg.test_members(), "the fixture has test puppets: an ad hoc run's pool (every main) must leave them out"
        t = first_slot(reg, RID)
        ev = rc.open_run(reg, rs, RID, t, by="t", schedule=ADHOC_SCHEDULE)
        join(reg, rs, ev, ms)
        people = {m.display_name: m for m in ms}
    else:
        ev, people = _joined_series(reg, rs, rows)
    lock_with_board(reg, rs, ev, list(people))
    rc.free_seat(reg, rs, ev, "Healb", "declined")
    assert rc.needs(reg, ev)["roles"] == {"healer": 1}
    return ev, people


def test_recurring_after_lock_a_role_swap_waits_for_an_officer(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    assert rc.run_kind(reg, ev) == "recurring" and not rc.auto_swaps(reg, ev)
    bot = Bot(reg, rs)
    sent, _ = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))  # the engine on its own (scheduler / freed seat)
    assert sent == [] and ev.fill_state == "suggested" and not bot.dms, "no swap DM without an officer"
    assert any("waiting for an officer's Ask" in ln for ln in ev.log)
    card = text_of(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs))
    assert "**Suggested**" in card and "**Offy** · offspec" in card and "**Short** <role:healer> 1" in card
    ask = next(b for b in buttons(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs)) if b.custom_id.startswith(f"runact.{reg.key}:{ev.key}:ask:"))
    assert ask.custom_id.endswith(rc.role_plan(reg, rs, ev).rev)
    sent, _ = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev), by="Officer", approved_by="Officer"))  # an officer's Send
    assert [(a.display_name, a.kind, a.approved_by) for a in sent] == [("Offy", "offspec", "Officer")]
    assert people["Offy"].discord_id in bot.dms


def test_ad_hoc_after_lock_a_role_swap_waits_for_an_officer_too(reg, rs):
    """D22: ad hoc runs also wait for an officer before a role swap (the engine used to send it on its own)."""
    ev, people = _locked_short_healer(reg, rs, adhoc=True)
    assert rc.run_kind(reg, ev) == "adhoc" and not rc.auto_swaps(reg, ev)
    bot = Bot(reg, rs)
    sent, _ = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))  # the scheduler / a freed seat: nobody pressed
    assert sent == [] and ev.fill_state == "suggested" and not bot.dms, "no swap DM without an officer"
    card = text_of(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs))
    assert "**Suggested**" in card and "**Offy** · offspec" in card, "the ad hoc lock card offers the Ask"
    sent, _ = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev), by="Officer", approved_by="Officer"))
    assert [(a.display_name, a.kind, a.approved_by) for a in sent] == [("Offy", "offspec", "Officer")]


def test_same_role_fill_stays_automatic_on_a_recurring_run(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    spare = add(reg, 399, "Spareheal", "Druid", "Restoration")
    reg.regular_add(spare.discord_id, KEY, "officer")  # an unanswered regular healer: a same-role body
    bot = Bot(reg, rs)
    sent, _ = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev)))
    assert [(a.display_name, a.kind) for a in sent] == [("Spareheal", "regular")]


# ---------------------------------------------------------------- the health card and the early warning

def test_health_card_has_the_suggested_block_with_ask_and_suggest(reg, rs):
    ev, p = worked_example(reg, rs)
    view = views.health_layout(reg, rs, ev, rc.run_team(reg, ev), ico)
    body = text_of(view)
    assert "**Suggested** · expected <role:tank> 1/2   <role:healer> 1/2 (joined + 10 regulars yet to answer) · every seat taken: swaps first" in body
    lines = body.split("\n")
    assert "<spec:Warrior:Protection> **Kael** · offspec" in lines and "alternates: Gorm" in body
    assert "<spec:Paladin:Holy> **Ana** · flex" in lines and "alternates: Wren · Pip" in body
    assert not any("<role:" in ln and "<spec:" in ln for ln in lines), "a spec icon alone on a member line — never a role icon beside it"
    assert "Still short if all say yes: nothing" in body
    ids = [b.custom_id for b in buttons(view)]
    rev = rc.role_plan(reg, rs, ev).rev
    assert f"runact.{reg.key}:{ev.key}:ask:{rev}" in ids and f"runact.{reg.key}:{ev.key}:suggest" in ids
    assert "tank 1" not in body and "1 healer" not in body.split("**Suggested**")[1].split("\n")[0], "role counts are icon + number"


def test_ask_button_round_trips_its_rev(reg, rs):
    ev, _ = worked_example(reg, rs)
    rev = rc.role_plan(reg, rs, ev).rev
    b = views.RunButton(reg.key, ev.key, "ask", rev=rev)
    m = views.RunButton.__discord_ui_compiled_template__.fullmatch(b.custom_id)
    assert m and m["key"] == ev.key and m["action"] == "ask" and m["rev"] == rev
    plain = views.RunButton.__discord_ui_compiled_template__.fullmatch(f"runact:{ev.key}:lock")
    assert plain and plain["rev"] is None, "the old buttons still match"


def test_early_warning_at_sheet_open_when_regulars_cant_cover(reg, rs):
    ev0, p = worked_example(reg, rs)
    rs.events.pop(ev0.key)  # reopen the same run through the bot's open path
    reg.config.roster_channel_id = 77
    bot = Bot(reg, rs)
    bot.channels[77] = Chan(77, "roster")
    ev = run(bot.open_run_and_post(reg, rs, RID, ev0.start, by="scheduler"))
    assert ev.health_early and "health" in ev.cards and not ev.health_posted
    assert any("**Suggested**" in text_of(m["view"]) for m in bot.channels[77].sent if m.get("view") is not None)
    assert not bot.dms, "the early card nudges nobody"
    first = ev.cards["health"]
    run(bot.post_health(reg, rs, ev, bot.channels[77], nudge=True))  # reminder time: a fresh card
    assert ev.health_posted and ev.cards["health"] != first


def test_no_early_warning_when_the_regulars_cover_the_minimums(reg, rs):
    ev0, p = worked_example(reg, rs, absent=())
    rs.events.pop(ev0.key)
    reg.config.roster_channel_id = 77
    bot = Bot(reg, rs)
    bot.channels[77] = Chan(77, "roster")
    ev = run(bot.open_run_and_post(reg, rs, RID, ev0.start, by="scheduler"))
    assert not ev.health_early and not bot.channels[77].sent


# ---------------------------------------------------------------- the lock card and the Confirm DM

def test_lock_card_lists_spec_switches_and_benched_regulars(reg, rs):
    rows = [("Tanka", "Warrior", "Protection"), ("Kael", "Warrior", "Fury", "Protection"), ("Heala", "Priest", "Holy"), ("Healb", "Shaman", "Restoration")] + DPS8[:6]
    ev, people = _joined_series(reg, rs, rows)
    extra = add(reg, 450, "Benched", "Rogue", "Combat")
    reg.regular_add(extra.discord_id, KEY, "officer")
    rc.set_signup(reg, rs, ev, extra, None, "in")
    lock_with_board(reg, rs, ev, list(people))
    k = ev.seat_of("Kael")[1]
    k.spec, k.role = "Protection", "tank"
    ev.rosters[0].spec_switches = {"Kael": "Protection"}
    card = text_of(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs))
    assert "**Playing an offspec**\n<spec:Warrior:Protection> Kael" in card
    assert "**Regulars not rostered**\n<spec:Rogue:Combat> Benched" in card
    sg = ev.signups[str(people["Kael"].discord_id)]
    confirm = text_of(views.confirm_layout(reg, ev, rc.run_team(reg, ev), ico, sg, 0, 0, sg.discord_id))
    assert "<spec:Warrior:Protection> **Kael**" in confirm and "You're rostered as **Protection**, your offspec — you signed up as Fury." in confirm


def test_lock_card_says_locked_short_when_the_lock_came_out_short(reg, rs):
    rows = [("Tanka", "Warrior", "Protection"), ("Tankb", "Paladin", "Protection"), ("Heala", "Priest", "Holy")] + DPS8[:6]
    ev, people = _joined_series(reg, rs, rows)
    lock_with_board(reg, rs, ev, list(people))
    ev.log.append("locked short 1 healer")  # what lock_run writes when the solver capped the minimum
    card = text_of(views.lock_layout(reg, ev, rc.run_team(reg, ev), ico, 0, rs))
    assert "**Locked short** <role:healer> 1" in card


def test_pressing_yes_on_a_prelock_ask_that_closed_at_lock_says_so(reg, rs):
    from types import SimpleNamespace

    ev, p = worked_example(reg, rs)
    ask = rc.FillAsk(discord_id=p["Kael"].discord_id, display_name="Kael", kind="offspec", role="tank", character="Kael", spec="Protection", reason="t", phase="prelock")
    ev.fill_asks.append(ask)
    rc.release_prelock_asks(ev)
    said = []

    async def send_message(text, **kw):
        said.append(text)

    async def may_answer_for(itx, reg_, uid):
        return True
    client = SimpleNamespace(registries=SimpleNamespace(for_interaction=lambda i: reg), raids=SimpleNamespace(store=lambda r: rs), may_answer_for=may_answer_for)
    itx = SimpleNamespace(client=client, user=SimpleNamespace(id=p["Kael"].discord_id), response=SimpleNamespace(send_message=send_message))
    run(views.FillButton(reg.key, ev.key, p["Kael"].discord_id, "yes").callback(itx))
    assert said == ["That question closed when the roster locked — nothing changed."]
    assert str(p["Kael"].discord_id) not in ev.signups
