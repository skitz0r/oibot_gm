"""Roster redesign Phase 2, final review fixes — each a regression test that fails without its fix:
- a pre-lock ask (answered yes or no, or closed at lock) no longer keeps that member out of the fill engine after lock:
  a benched joiner who agreed before lock is the cheapest move for a seat freed later;
- someone who goes away (an absence) or says No thanks while an ask is out: the ask closes (its tied half too), stops
  covering its seat, and a late yes on it never overrides the absence;
- plain text's "send them" on a run that locked after the diff goes through send_plan (refused: the plan's phase
  changed) — never a different post-lock batch the officer never saw;
- ad hoc first come is the order people JOINED (Signup.joined_at), not the last touch: pressing Join again, another
  character or a yes to an ask keeps the place;
- a regular who agreed to bring an alt keeps the regular (and main) seat weight;
- the open sheet's health card closes at lock (no live Ask), and is refreshed when an absence is recorded or cleared;
- MCP raid_set takes the raid-level `confirm` switch;
- fill DM wording for a non-regular never claims "you're free that night"; ask kind "regular" only where the run has
  regulars ("unanswered" elsewhere);
- alternates after lock for plain seats survive the suggestions using up the free seats;
- a board edit on a run with confirmations off doesn't claim "(asked to confirm)"."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import join, lock_with_board
from test_analytics_cards import text_of
from test_parity_verbs import Bot, Chan
from test_roster_p2_asks import _locked_short_healer
from test_roster_p2_fixes import CardBot, _adhoc_short_healer
from test_roster_p2_planner import DPS8, _joined_series, add, worked_example
from wizard_harness import Harness

from oibot_gm import configops
from oibot_gm import mcp_server as ms
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import ADHOC_SCHEDULE, Absence

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def ids(view) -> list[str]:
    return [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]


def _send_prelock(reg, rs, ev, picks=None):
    """What an officer's Ask records (send_plan), without DMs."""
    plan = rc.role_plan(reg, rs, ev)
    asks = rc.pick_asks(plan, picks)
    for a in asks:
        a.phase, a.approved_by = "prelock", "Officer"
        ev.fill_asks.append(a)
    return asks


def _away(reg, m, ev):
    day = ev.start.astimezone(reg.tz).date().isoformat()
    m.absences = [Absence(start=day, end=day, reason="away", by="t")]
    reg.save(m, "away")
    return day


# ---------------------------------------------------------------- a pre-lock ask doesn't bar a member after lock

def test_a_prelock_yes_benched_at_lock_is_the_sub_move_when_a_healer_seat_frees(reg, rs):
    ev, p = worked_example(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    h = next(i for i, st in enumerate(plan.seats) if st.role == "healer")
    assert plan.seats[h].options[2][0].display_name == "Pip"
    asks = _send_prelock(reg, rs, ev, picks={h: 2})  # the officer picks Pip (an outsider Disc priest) for the healer seat
    pip = next(a for a in asks if a.display_name == "Pip")
    rc.apply_fill_answer(reg, rs, ev, pip, True)
    assert ev.signups[str(p["Pip"].discord_id)].status == "in"
    ten = ["Brannoc", "Mira", "Kael", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren"]
    join(reg, rs, ev, [p[n] for n in ten])
    lock_with_board(reg, rs, ev, ten)  # the lock benches Pip
    rc.release_prelock_asks(ev)
    rc.free_seat(reg, rs, ev, "Mira", "declined")
    after = rc.role_plan(reg, rs, ev)
    healer = next(st for st in after.seats if st.role == "healer")
    assert healer.options, "somebody can cover the freed healer seat"
    first = healer.options[0][0]
    assert (first.display_name, first.kind) == ("Pip", "sub"), "Pip already agreed and was benched: the cheapest move"
    assert "Pip" in [a.display_name for a in rc.fill_batch(reg, rs, ev)]


def test_a_prelock_no_benched_joiner_is_asked_for_a_dps_seat_after_lock(reg, rs):
    ev, p = worked_example(reg, rs)
    asks = _send_prelock(reg, rs, ev)
    ana = next(a for a in asks if a.display_name == "Ana")
    rc.set_signup(reg, rs, ev, p["Ana"], None, "in")  # Ana joins as Ret …
    rc.apply_fill_answer(reg, rs, ev, ana, False)  # … and says No to flexing Holy
    ten = ["Brannoc", "Gorm", "Mira", "Pip", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren"]
    join(reg, rs, ev, [p[n] for n in ten])
    lock_with_board(reg, rs, ev, ten)  # Ana left off: the sheet is full
    rc.release_prelock_asks(ev)
    rc.free_seat(reg, rs, ev, "Jex", "declined")
    batch = rc.fill_batch(reg, rs, ev)
    assert ("Ana", "sub") in [(a.display_name, a.kind) for a in batch], "a joiner left off is the first asked, whatever she said before lock"


def test_before_lock_a_prelock_ask_still_counts_once(reg, rs):
    ev, p = worked_example(reg, rs)
    asks = _send_prelock(reg, rs, ev)
    rc.apply_fill_answer(reg, rs, ev, next(a for a in asks if a.display_name == "Ana"), False)
    again = rc.role_plan(reg, rs, ev)
    assert "Ana" not in {a.display_name for st in again.seats for opt in st.options for a in opt}, "one PRE-LOCK ask per member per run"


# ---------------------------------------------------------------- away / No thanks while an ask is out

def test_an_absence_closes_a_pending_prelock_ask_and_the_seat_shows_short_again(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = Bot(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    run(bot.send_plan(reg, rs, ev, rev=plan.rev, by="Officer"))
    assert not rc.role_plan(reg, rs, ev).seats, "both asks out cover their seats"
    day = _away(reg, p["Kael"], ev)
    run(bot.after_absence(reg, p["Kael"], day, day, "Kael"))
    kael = next(a for a in ev.fill_asks if a.display_name == "Kael")
    assert kael.answer == "released" and not kael.pending
    now = rc.role_plan(reg, rs, ev)
    assert now.short["roles"].get("tank") == 1 and "Kael" not in now.waiting
    assert seat_names(now, "tank") and seat_names(now, "tank")[0] == "Gorm", "the next tank is suggested"


def seat_names(plan, role):
    st = next((s for s in plan.seats if s.role == role), None)
    return [opt[0].display_name for opt in st.options] if st else []


def test_an_ask_to_someone_away_covers_nothing_even_if_still_open(reg, rs):
    ev, p = worked_example(reg, rs)
    _send_prelock(reg, rs, ev)
    _away(reg, p["Kael"], ev)  # recorded somewhere the ripple didn't reach: the planner still won't count the ask
    plan = rc.role_plan(reg, rs, ev)
    assert plan.short["roles"].get("tank") == 1 and plan.waiting == ["Ana"]


def test_a_late_yes_never_overrides_an_absence(reg, rs):
    ev, p = worked_example(reg, rs)
    asks = _send_prelock(reg, rs, ev)
    kael = next(a for a in asks if a.display_name == "Kael")
    day = _away(reg, p["Kael"], ev)
    rc.set_signup(reg, rs, ev, p["Kael"], None, "out", source="absence")
    line = rc.apply_fill_answer(reg, rs, ev, kael, True)
    sg = ev.signups[str(p["Kael"].discord_id)]
    assert (sg.status, sg.source) == ("out", "absence") and "away" in line and kael.answer == "no"
    assert day


def test_no_thanks_closes_the_pending_ask_and_withdraws_its_tied_half(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    bot = Bot(reg, rs)
    pip = add(reg, 77, "Pippa", "Mage", "Fire")  # a DPS body to back Offy's DPS seat if needed
    sent, _ = run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev), approved_by="Officer"))
    offy = next(a for a in ev.fill_asks if a.display_name == "Offy")
    assert offy.swap and offy.pending
    run(bot.set_answer(reg, rs, ev, people["Offy"], "out", None, by="Offy"))
    assert offy.answer == "released" and not offy.pending
    partner = rc.tied_ask(ev, offy)
    assert partner is None or partner.answer == "released"
    assert pip


def test_after_lock_an_absence_closes_the_fill_ask_and_a_late_yes_is_refused(reg, rs):
    ev, people = _locked_short_healer(reg, rs, adhoc=True)
    bot = Bot(reg, rs)
    run(bot.run_fill(reg, rs, ev, rc.run_team(reg, ev), by="Officer", approved_by="Officer"))  # a swap needs an officer (D3/D22)
    offy = next(a for a in ev.fill_asks if a.display_name == "Offy")
    assert offy.pending
    day = _away(reg, people["Offy"], ev)
    run(bot.after_absence(reg, people["Offy"], day, day, "Offy"))
    assert offy.answer == "released"
    offy.answer = None  # a stale button the ripple missed: the yes is still refused
    rc.apply_fill_answer(reg, rs, ev, offy, True)
    assert offy.answer == "no" and not ev.seat_of("Offy")


# ---------------------------------------------------------------- plain text: send after the run locked

def test_plain_text_send_after_the_run_locked_is_refused_and_sends_nothing(reg, rs):
    ev, p = worked_example(reg, rs)
    send = ConfigOp(op="run_fill", target=ev.key, value="send")
    revs = configops.board_revs(reg, [send], rs)
    rev = configops.op_rev(send, ev.key, revs)
    assert rev
    ten = ["Brannoc", "Mira", "Kael", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren"]
    join(reg, rs, ev, [p[n] for n in ten])
    lock_with_board(reg, rs, ev, ten)  # the scheduler locked before Apply
    rc.free_seat(reg, rs, ev, "Mira", "declined")
    bot = Bot(reg, rs)
    with pytest.raises(rc.PlanChanged):
        run(configops.apply_async(reg, send, "Officer", True, bot=bot, rev=rev))
    assert not ev.fill_asks and not bot.dms, "the approved pre-lock asks never turn into a different post-lock batch"


# ---------------------------------------------------------------- ad hoc first come: when they joined

def _adhoc(reg, rs, rows):
    reg.set_raid_overrides("barrow_deeps", {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    ms_ = [add(reg, 700 + i, *row) for i, row in enumerate(rows)]
    t = rc.upcoming(reg, "barrow_deeps", reg.now_local(), 24 * 21)[0][1]
    ev = rc.open_run(reg, rs, "barrow_deeps", t, by="t", schedule=ADHOC_SCHEDULE)
    return ev, ms_


def _stamp(ev, members):
    """Distinct, earlier answer times in this order (now() has one-second resolution: a test can't wait them apart)."""
    for i, m in enumerate(members):
        sg = ev.signups[str(m.discord_id)]
        sg.updated_at = f"2026-01-01T00:00:{i:02d}+00:00"
        if sg.status == "in" and "joined_at" in type(sg).model_fields:
            sg.joined_at = sg.updated_at


def test_pressing_join_again_keeps_the_first_come_place(reg, rs):
    rows = [("Tanka", "Warrior", "Protection"), ("Tankb", "Paladin", "Protection"), ("Heala", "Priest", "Holy"), ("Healb", "Shaman", "Restoration")] + DPS8 + [("Jot", "Rogue", "Combat")]
    ev, ms_ = _adhoc(reg, rs, rows)
    for m in ms_:
        rc.set_signup(reg, rs, ev, m, None, "in")
    _stamp(ev, ms_)
    first_dps = ms_[4]
    rc.set_signup(reg, rs, ev, first_dps, None, "in")  # pressed Join again a moment later
    players = rc.players_for(reg, ev)
    assert [p.signup_name for p in players][:5] == [m.display_name for m in ms_[:5]], "still fifth in line"
    late = rc.first_come_out(players, 10, {"tank": 2, "healer": 2}, {}, set())
    assert first_dps.display_name not in late and late == {m.display_name for m in ms_[10:]}, "the last joiners sit out, not the re-presser"


def test_bench_then_join_starts_a_new_place(reg, rs):
    ev, ms_ = _adhoc(reg, rs, [("Aa", "Rogue", "Combat"), ("Bb", "Mage", "Fire")])
    rc.set_signup(reg, rs, ev, ms_[0], None, "in")
    rc.set_signup(reg, rs, ev, ms_[1], None, "in")
    _stamp(ev, ms_)
    rc.set_signup(reg, rs, ev, ms_[0], None, "sub")
    assert ev.signups[str(ms_[0].discord_id)].joined_at is None
    rc.set_signup(reg, rs, ev, ms_[0], None, "in")
    assert [p.signup_name for p in rc.players_for(reg, ev)] == ["Bb", "Aa"]


def test_a_prelock_yes_keeps_the_join_place(reg, rs):
    ev, p = worked_example(reg, rs)
    rc.set_signup(reg, rs, ev, p["Ana"], None, "in")
    _stamp(ev, [p["Ana"]])
    first = ev.signups[str(p["Ana"].discord_id)].joined_at
    ana = next(a for a in _send_prelock(reg, rs, ev) if a.display_name == "Ana")
    rc.apply_fill_answer(reg, rs, ev, ana, True)
    sg = ev.signups[str(p["Ana"].discord_id)]
    assert sg.spec == "Holy" and sg.joined_at == first


def test_old_files_without_joined_at_order_by_updated_at(reg, rs):
    s = rc.Signup(discord_id=1, display_name="X", character="X", cls="Rogue", spec="Combat", role="melee", status="in", updated_at="2026-01-01T00:00:00")
    assert s.joined_at is None and s.order_at == "2026-01-01T00:00:00"


# ---------------------------------------------------------------- a regular bringing an alt at request keeps the weights

def test_a_regular_who_brings_an_alt_at_request_keeps_the_regular_weight(reg, rs):
    ev, p = worked_example(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    h = next(i for i, st in enumerate(plan.seats) if st.role == "healer")
    wren = next(a for a in _send_prelock(reg, rs, ev, picks={h: 1}) if a.display_name == "Wren")
    rc.apply_fill_answer(reg, rs, ev, wren, True)
    assert ev.signups[str(p["Wren"].discord_id)].character == "Fern"
    w = rc.run_weights(reg, ev)
    players = rc.players_for(reg, ev)
    asked = rc.seat_bonus(reg, rs, ev, players)["Wren"]
    ev.signups[str(p["Wren"].discord_id)].source = "member"  # the same alt, signed by herself: no favour done
    plain = rc.seat_bonus(reg, rs, ev, players)["Wren"]
    assert asked - plain == int(w["regular"]) + int(w["main"]), "Fern at the officers' request keeps the regular and main weights"


# ---------------------------------------------------------------- the health card: closed at lock, refreshed on absences

class Msg:
    def __init__(self, mid):
        self.id, self.views = mid, []

    async def edit(self, **kw):
        self.views.append(kw.get("view"))
        return self

    async def create_thread(self, **kw):
        raise LookupError("no threads here")


class CardChan(Chan):
    def __init__(self, cid):
        super().__init__(cid, "roster")
        self.msgs: dict[int, Msg] = {}

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})
        m = Msg(9000 + len(self.sent))
        self.msgs[m.id] = m
        return SimpleNamespace(id=m.id, channel=self)

    async def fetch_message(self, mid):
        return self.msgs[mid]


def _with_health_card(reg, rs, bot, ev):
    ch = CardChan(4242)
    bot.channels[ch.id] = ch
    run(bot.post_health(reg, rs, ev, ch, nudge=False, early=True))
    assert "health" in ev.cards
    return ch, ch.msgs[ev.cards["health"]]


def test_the_health_card_closes_at_lock_with_no_ask_button(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = Bot(reg, rs)
    ch, card = _with_health_card(reg, rs, bot, ev)
    ten = ["Brannoc", "Mira", "Kael", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren"]
    join(reg, rs, ev, [p[n] for n in ten])
    lock_with_board(reg, rs, ev, ten)
    run(bot.refresh_cards(reg, ev))
    assert card.views, "the card was redrawn after lock"
    body = text_of(card.views[-1])
    assert "Locked" in body and "**Suggested**" not in body and not ids(card.views[-1]), "no Suggested block, no live Ask"
    assert "health" not in ev.cards, "redrawn once, then left alone"


def test_an_ask_pressed_on_the_health_card_after_lock_says_the_run_locked(reg, rs):
    ev, p = worked_example(reg, rs)
    bot = CardBot(reg, rs)
    rev = rc.role_plan(reg, rs, ev).rev
    ten = ["Brannoc", "Mira", "Kael", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable", "Wren"]
    join(reg, rs, ev, [p[n] for n in ten])
    lock_with_board(reg, rs, ev, ten)
    rc.free_seat(reg, rs, ev, "Mira", "declined")
    h = Harness(reg, rs, client=bot)
    run(views.RunButton(reg.key, ev.key, "ask", rev=rev).callback(h.card_press()))
    assert "locked" in h.text and "The card shows them as they are now" not in h.text and not bot.dms


def test_an_absence_recorded_or_cleared_refreshes_the_health_card(reg, rs):
    ev, p = worked_example(reg, rs, absent=("Ona",))
    bot = Bot(reg, rs)
    ch, card = _with_health_card(reg, rs, bot, ev)
    assert "Thorn" in text_of(views.health_layout(reg, rs, ev, rc.run_team(reg, ev), ico))
    day = _away(reg, p["Thorn"], ev)
    run(bot.after_absence(reg, p["Thorn"], day, day, "Thorn"))
    assert len(card.views) == 1 and "Thorn" not in text_of(card.views[-1]).split("**Haven't answered**")[-1], "Thorn leaves the No answer list at once"
    a = p["Thorn"].absences[0]
    p["Thorn"].absences = []
    reg.save(p["Thorn"], "back")
    run(bot.after_absence_cleared(reg, p["Thorn"], a))
    assert len(card.views) == 2 and "Thorn" in text_of(card.views[-1])


# ---------------------------------------------------------------- MCP raid_set: confirm

def test_mcp_raid_set_takes_confirm():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps"}]})
        seen.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token="t" * 64, transport=httpx.MockTransport(handler)))
    try:
        ms.raid_set("barrow", "confirm", "off")  # sent as given: the bot reads 'off' (configops._raid_ops), no client-side grammar
        assert seen[-1] == ("/api/ops/change", {"changes": [{"kind": "raid", "target": "barrow_deeps", "fields": {"confirm": "off"}}], "apply": True, "confirmed": True})
        ms.raid_set("barrow", fields={"confirm": True})
        assert seen[-1][1]["changes"][0]["fields"]["confirm"] is True
    finally:
        ms.use(None)
    assert "confirm" in (ms.raid_set.__doc__ or "") and "weights" in (ms.schedule_set.__doc__ or "")


# ---------------------------------------------------------------- truthful words

def test_fill_dm_for_a_non_regular_never_says_free_that_night(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    m = add(reg, 88, "Stranger", "Priest", "Holy")
    ask = rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="healer", character="Stranger", spec="Holy", reason="short 1 healer")
    body = text_of(views.fill_layout(reg, ev, rc.run_team(reg, ev), ico, ask))
    assert "free that night" not in body and "haven't answered" in body


def test_ask_kind_is_not_regular_on_an_ad_hoc_run(reg, rs):
    ev, ms_ = _adhoc_short_healer(reg, rs)
    add(reg, 870, "Medic", "Priest", "Holy")  # a real healer main who hasn't answered
    plan = rc.role_plan(reg, rs, ev)
    kinds = {a.kind for st in plan.seats for opt in st.options for a in opt}
    assert "regular" not in kinds and "unanswered" in kinds, kinds
    block = views.suggested_block(reg, ico, plan)
    assert "regular" not in block


def test_ask_kind_is_regular_where_the_series_has_regulars(reg, rs):
    ev, people = _locked_short_healer(reg, rs)
    extra = add(reg, 871, "Cleric", "Priest", "Holy")
    reg.regular_add(extra.discord_id, "barrow_deeps:default", "officer")
    kinds = {a.kind for st in rc.role_plan(reg, rs, ev).seats for opt in st.options for a in opt}
    assert "regular" in kinds and "unanswered" not in kinds


# ---------------------------------------------------------------- alternates after lock

def test_a_freed_seat_after_lock_keeps_its_alternates(reg, rs):
    rows = [("Tanka", "Warrior", "Protection"), ("Tankb", "Paladin", "Protection"), ("Heala", "Priest", "Holy"), ("Healb", "Shaman", "Restoration")] + DPS8
    ev, people = _joined_series(reg, rs, rows)
    lock_with_board(reg, rs, ev, list(people))
    rc.free_seat(reg, rs, ev, "Dax", "declined")
    for i in range(3):
        add(reg, 930 + i, f"Spare{'abc'[i]}", "Rogue", "Combat")
    plan = rc.role_plan(reg, rs, ev)
    st = next(s for s in plan.seats if not s.role)
    assert len(st.options) == 1 + rc.PLAN_ALTERNATES, [[a.display_name for a in opt] for opt in st.options]


# ---------------------------------------------------------------- board edits with confirmations off

def test_board_edit_on_a_run_without_confirmations_says_nothing_about_confirming(reg, rs):
    ev, people = _locked_short_healer(reg, rs, adhoc=True)
    assert not rc.run_confirms(reg, ev)
    ch = CardChan(5151)

    class B(Bot):
        def officer_channel(self, reg, ev=None):
            return ch

    bot = B(reg, rs)
    sg = ev.signups[str(people["Healb"].discord_id)]
    run(bot.after_board_change(reg, rs, ev, rc.run_team(reg, ev), [sg], [], by="Officer"))
    line = next(s["content"] for s in ch.sent if s["content"] and "board by" in s["content"])
    assert "Healb" in line and "asked to confirm" not in line
