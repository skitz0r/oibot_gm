"""Roster Phase 2 follow-ups from the review's verify pass, each a regression test that fails without its fix:
- the health / lock card's Ask sends only the seats the card draws (raid_views.SUGGEST_LINES), never one it left
  under "+N more" — its label counts exactly those;
- the suggestions screen keeps the batch Send DMs whole when the plan is long (the reasons go first, then seat lines
  from the end); a batch too long to show turns Send off instead of DMing people the screen cut;
- a member help snapshot's "cover for short roles" gives counts only — who could cover is the officers' business."""
from __future__ import annotations

import asyncio

from test_flows_raid import channel, officer  # noqa: F401 — the flows' fixtures
from test_roster_p2_fixes import CardBot
from test_roster_p2_planner import worked_example
from wizard_harness import Harness

from oibot_gm import flows_raid
from oibot_gm import help as help_mod
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1. the card's Ask sends only what the card shows

def _four_seats(reg, rs, monkeypatch):
    """The worked example (tank: Kael, healer: Ana), the plan given two more seats (Gorm, Bo): four seats, the card
    draws three."""
    ev, p = worked_example(reg, rs)
    real = rc.role_plan

    def four(reg_, rs_, ev_, *a, **kw):
        plan = real(reg_, rs_, ev_, *a, **kw)
        if plan.phase == "prelock" and [st.role for st in plan.seats] == ["tank", "healer"]:
            extra = [(p["Gorm"], "tank", "Protection"), (p["Bo"], "dps", "Fire")]
            plan.seats += [rc.PlanSeat(role=role, options=[[rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role=role,
                                                                       character=m.display_name, spec=spec, reason=f"short 1 {role}", why="test")]])
                           for m, role, spec in extra]
            plan.rev = rc.plan_rev(plan)
        return plan
    monkeypatch.setattr(rc, "role_plan", four)
    plan = rc.role_plan(reg, rs, ev)
    assert [a.display_name for a in plan.asks] == ["Kael", "Ana", "Gorm", "Bo"]
    return ev, p, plan


def test_card_shows_three_seats_and_its_ask_counts_three(reg, rs, monkeypatch):
    ev, p, plan = _four_seats(reg, rs, monkeypatch)
    body = views.suggested_block(reg, ico, plan)
    assert "**Bo**" not in body and "+1 more — Ask sends only the 3 above" in body
    row = views.suggest_row(reg, ev, plan)
    assert row.children[0].item.label == "Ask these 3"


def test_card_ask_button_never_dms_a_seat_the_card_left_out(reg, rs, monkeypatch):
    ev, p, plan = _four_seats(reg, rs, monkeypatch)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(reg.key, ev.key, "ask", rev=plan.rev).callback(h.card_press()))
    assert h.text.startswith("Asked Kael, Ana, Gorm")
    assert set(bot.dms) == {p["Kael"].discord_id, p["Ana"].discord_id, p["Gorm"].discord_id}
    assert p["Bo"].discord_id not in bot.dms and all(a.display_name != "Bo" for a in ev.fill_asks)


def test_the_suggest_screen_still_sends_every_seat(reg, rs, monkeypatch, officer):  # noqa: F811 — Suggest… lists them all
    ev, p, plan = _four_seats(reg, rs, monkeypatch)
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(reg.key, ev.key, "suggest").callback(h.card_press()))
    assert "Bo as Bo (Fire" in h.text
    run(h.press("Send 4 asks"))
    assert p["Bo"].discord_id in bot.dms


# ---------------------------------------------------------------- 2. a long suggestions screen keeps the batch whole

def test_fit_suggestions_drops_reasons_then_seat_lines_never_the_batch():
    lines = ["Run · before lock"]
    seats = [(f"{n + 1}. dps: Person{n} as Char{n} (Spec, other)" + " pad" * 10, "why " * 20) for n in range(40)]
    batch = ["", "**Send DMs exactly these:**", "1. Person0 as Char0 (Spec) for dps — short 1", "Still short if everyone says yes: nothing"]
    text, whole = flows_raid.fit_suggestions(lines, seats, batch)
    assert whole and len(text) <= flows_raid.SUGGEST_TEXT_MAX
    assert text.endswith("\n".join(batch)), "the batch Send DMs is on the screen in full"
    assert "why why" not in text and "more seats — the selects below" in text
    small, whole2 = flows_raid.fit_suggestions(lines, seats[:2], batch)
    assert whole2 and "why why" in small and "more seats" not in small, "a short plan keeps its reasons"


def test_fit_suggestions_turns_send_off_when_the_batch_cannot_show():
    batch = ["", "**Send DMs exactly these:**"] + [f"{n + 1}. Person{n} as Char{n} (Spec) for dps — short 30; not a regular" + " pad" * 5 for n in range(60)]
    text, whole = flows_raid.fit_suggestions(["Run"], [], batch)
    assert not whole and len(text) <= flows_raid.SUGGEST_TEXT_MAX and "Send stays off" in text


def test_suggest_screen_sends_what_fits_when_the_batch_cannot_show(reg, rs, monkeypatch, officer):  # noqa: F811
    """Phase 2 (design B7): a batch too long for one screen no longer disables Send — the screen lists the asks of the
    first seats that fit and Send DMs exactly those ("Send these N (the rest on the site)"); with room for none, off."""
    ev, p, plan = _four_seats(reg, rs, monkeypatch)
    monkeypatch.setattr(flows_raid, "fit_suggestions", lambda lines, seats, batch: ("cut", False))
    bot = CardBot(reg, rs)
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(reg.key, ev.key, "suggest").callback(h.card_press()))
    send = next(b for b in h.screen["view"].children if getattr(b, "label", "").startswith("Send"))
    assert not send.disabled and send.label.startswith("Send these ") and send.label.endswith("(the rest on the site)")
    monkeypatch.setattr(flows_raid, "fit_partial", lambda lines, plan, picks: (None, "too long"))
    run(views.RunButton(reg.key, ev.key, "suggest").callback(h.card_press()))
    send = next(b for b in h.screen["view"].children if getattr(b, "label", "").startswith("Send"))
    assert send.disabled and send.label == "Send 0 asks"


# ---------------------------------------------------------------- 3. member help: cover counts, no names

def test_member_help_cover_line_counts_without_names(reg, rs):
    ev, p = worked_example(reg, rs)
    for name in ("Kael", "Ana", "Jex"):  # Kael (Fury, offspec Protection) could tank; Ana (declared flex) could heal
        rc.set_signup(reg, rs, ev, p[name], None, "in")
    officer_view = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=True, detail=True))
    cover = next(ln for ln in officer_view.splitlines() if "cover for short roles" in ln)
    assert "Kael" in cover and "Ana" in cover
    member_view = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=False, detail=True, viewer_id=p["Jex"].discord_id))
    mcover = next(ln for ln in member_view.splitlines() if "cover for short roles" in ln)
    assert "Kael" not in mcover and "Ana" not in mcover
    assert "1 offspec" in mcover and "1 declared flex" in mcover, "the counts stay, in the fill screens' words"

