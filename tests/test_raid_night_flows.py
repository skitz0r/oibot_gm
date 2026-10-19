"""Dual interfaces phase 2 — the raid-night wizard screens driven end to end (tests/wizard_harness.py lints every screen):
/raid lock's Preview lock, /raid open's template select / week paging / a one-off that is a scheduled run, the
suggestions screen's Withdraw, the lock card's Remind / Pause / End / the closed card's Reopen, /raid health on a locked
run. The bot is test_flows_raid's recording fake (plus the verbs these screens call); Registry and RaidStore are real."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import discord
from conftest import by_role, join, lock_with_board, open_test_run
from test_flows_raid import FakeBot, harness, with_slots  # noqa: F401
from test_flows_raid import channel, officer  # noqa: F401 — the fixtures

from oibot_gm import raidcycle as rc
from oibot_gm.raid_buttons import RunButton
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness


def run(coro):
    return asyncio.run(coro)


class NightFlowBot(FakeBot):
    """test_flows_raid's fake plus the raid-night verbs, recorded."""

    def __init__(self, rs, reg):
        super().__init__(rs)
        self.reg = reg
        self.registries.for_interaction = lambda i: reg
        self.registries.by_key = {reg.key: reg}

    async def is_officer_anywhere(self, reg, uid):
        return True

    async def lock_preview(self, reg, rs, ev, *, by):
        names = [s.display_name for s in ev.by_status("in")]
        self.calls.append(("lock_preview", ev.key, by))
        rosters = rc.board_rosters(reg, ev, [names[:5], names[5:10]], rc.run_size(reg, ev))
        seated = {p.signup_name for r in rosters for p in r.selected}
        rosters[0].benched = [p for p in rc.players_for(reg, ev) if p.signup_name not in seated]
        return rosters

    async def withdraw_sent_ask(self, reg, rs, ev, uid, *, by, by_id=None, via="discord"):
        a = next(a for a in ev.fill_asks if a.discord_id == uid and a.pending)
        a.answer = "withdrawn"
        self.calls.append(("withdraw_sent_ask", ev.key, uid, by))
        return f"Withdrew the ask to {a.display_name}."

    async def remind(self, reg, rs, ev, *, by, by_id=None, via="discord"):
        self.calls.append(("remind", ev.key, by))
        return "Reminded 3 by DM."

    async def set_autofill_paused(self, reg, rs, ev, paused, *, by, by_id=None, via="discord"):
        self.calls.append(("pause", ev.key, paused, by))
        ev.autofill_paused = paused
        return "paused" if paused else "on"

    def end_consequence(self, reg, rs, ev):
        return f"End {ev.key}: no loot council is open — the run is marked done."

    async def end_run(self, reg, rs, ev, *, by, by_id=None, via="discord", refresh=True):
        self.calls.append(("end_run", ev.key, by))
        rc.end_run(reg, rs, ev, by)
        return "ended"

    async def reopen_run(self, reg, rs, ev, *, by, by_id=None, via="discord"):
        self.calls.append(("reopen_run", ev.key, by))
        rc.reopen_run(reg, rs, ev, by)
        return f"Reopened {ev.key}."


def night(reg, rs, user=None):
    bot = NightFlowBot(rs, reg)
    return Harness(reg, rs, client=bot, user=user), bot


def officer_user():
    from types import SimpleNamespace

    return SimpleNamespace(id=424242, display_name="Tester", mention="@Tester")


def locked_ten(reg, rs):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    return ev, seated


# ---------------------------------------------------------------- /raid lock: the confirm screen with Preview lock

def test_raid_lock_preview_draws_the_roster_one_member_per_line_then_locks(reg, rs, officer, channel):  # noqa: F811
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:12])
    h, bot = night(reg, rs)

    async def go():
        await FLOWS["raid_lock"](h.command(), reg)
        assert h.buttons() == ["Lock now", "Preview lock", "Back"]
        await h.press("Preview lock")
        emb = h.screen["embed"]
        assert emb is not None and emb.title.startswith("Preview")
        groups = [f for f in emb.fields if f.name.startswith("Group")]
        assert len(groups) == 2 and all(len(f.value.splitlines()) == 5 for f in groups), "the full roster, one member per line"
        left = next(f for f in emb.fields if f.name.startswith("Leftovers"))
        assert left.name == "Leftovers (2)"
        assert h.buttons() == ["Lock now", "Back"] and ("lock_preview", ev.key, "Tester") in bot.calls
        await h.press("Back")
        assert h.buttons() == ["Lock now", "Preview lock", "Back"]
        await h.press("Lock now")
    run(go())
    assert ("lock_run", ev.key, "Tester") in bot.calls


def test_card_lock_preview_refusal_offers_back(reg, rs):
    ev = open_test_run(reg, rs)
    h, bot = night(reg, rs, user=officer_user())

    async def busy(reg_, rs_, ev_, *, by):
        raise rc.SolverBusy()
    bot.lock_preview = busy

    async def go():
        await RunButton(reg.key, ev.key, "lock").callback(h.card_press())
        await h.press("Preview lock")
        assert "already working" in h.text and h.buttons() == ["Back", "Cancel"]
        await h.press("Back")
        assert h.buttons() == ["Lock now", "Preview lock", "Back"]
    run(go())


# ---------------------------------------------------------------- /raid open: templates, week paging, a one-off that IS a scheduled run

def test_more_than_three_templates_become_one_select(reg, rs, officer, channel):  # noqa: F811
    for i in range(4):
        reg.set_schedule("barrow_deeps", f"pug{i}", {"kind": "pickup", "name": f"Pug {i}"}, "t")
    h, bot = night(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press("Ad hoc run…")
        assert "Pick a template" in h.selects() and not any(b.endswith("pick a time") for b in h.buttons())
        await h.pick("Pick a template", "pug2")
        assert h.modal is not None and "Pug 2" in h.modal.title
    run(go())


def test_another_upcoming_run_pages_by_week(reg, rs, officer, channel):  # noqa: F811
    with_slots(reg, slots=[f"{d} {t}" for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") for t in ("19:30", "21:30")])
    h, bot = night(reg, rs)
    seen = {}

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press("Another upcoming run…")
        assert "Week of" in h.text and "← Earlier week" in h.buttons() and "Later week →" in h.buttons()
        first = [o.value for o in next(s for s in h._items() if getattr(s, "placeholder", None) == "Pick the run").options]
        assert len(first) <= 14
        await h.press("Later week →")
        second = [o.value for o in next(s for s in h._items() if getattr(s, "placeholder", None) == "Pick the run").options]
        assert second and not set(first) & set(second)
        seen["pick"] = second[0]
        await h.pick("Pick the run", second[0])
        await h.press("Open")
    run(go())
    assert bot.calls[0][0] == "open_run_and_post" and bot.calls[0][2].isoformat() == seen["pick"].partition("|")[0]


def test_a_one_off_at_a_scheduled_minute_opens_as_that_run(reg, rs, officer, channel):  # noqa: F811
    with_slots(reg, slots=("Wed 20:00",))
    nxt = next(t for _s, t in rc.upcoming(reg, "barrow_deeps", reg.now_local(), 24 * 21) if t - reg.now_local() > timedelta(days=1))
    h, bot = night(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press("Ad hoc run…")
        local = nxt.astimezone(reg.tz)
        await h.submit(h.modal, day=local.date().isoformat(), hour=str(local.hour), minute=str(local.minute))
        assert "opens as scheduled" in h.text and "Ad hoc:" not in h.text
        await h.press("Open")
    run(go())
    assert bot.calls[0][:3] == ("open_run_and_post", "barrow_deeps", nxt) and len(bot.calls[0]) == 4, "the raid's own run, not a one-off"


# ---------------------------------------------------------------- the suggestions screen: Withdraw

def test_suggestions_screen_withdraws_a_sent_ask(reg, rs, officer, channel):  # noqa: F811
    ev, seated = locked_ten(reg, rs)
    rc.free_seat(reg, rs, ev, seated[-1].discord_id, "no-confirm")
    plan = rc.role_plan(reg, rs, ev)
    a = rc.send_batch(plan)[0][0]
    a.expires_at = rc.ask_deadline(reg, ev).isoformat()
    ev.fill_asks.append(a)
    rs.save(ev, "asked")
    h, bot = night(reg, rs)

    async def go():
        await FLOWS["raid_suggest"](h.command(), reg, key=ev.key)
        assert "Withdraw an ask…" in h.selects()
        await h.pick("Withdraw an ask…", str(a.discord_id))
        assert f"Withdrew the ask to {a.display_name}" in h.text
    run(go())
    assert ("withdraw_sent_ask", ev.key, a.discord_id, "Tester") in bot.calls


# ---------------------------------------------------------------- the lock card's raid-night row and the closed card

def test_lock_card_remind_pause_end_and_reopen(reg, rs, monkeypatch):
    ev, _seated = locked_ten(reg, rs)
    h, bot = night(reg, rs, user=officer_user())

    async def go():
        await RunButton(reg.key, ev.key, "remind").callback(h.card_press())
        assert "Reminded 3 by DM." in h.text
        await RunButton(reg.key, ev.key, "pause").callback(h.card_press())
        assert ev.autofill_paused and ("pause", ev.key, True, "Tester") in bot.calls
        await RunButton(reg.key, ev.key, "end").callback(h.card_press())
        assert "hasn't started yet" in h.text, "End is for after the start"
        monkeypatch.setattr(rc, "end_refusal", lambda ev_, at=None: None)
        await RunButton(reg.key, ev.key, "end").callback(h.card_press())
        assert "no loot council is open" in h.text and h.buttons() == ["End the run now", "Back"]
        assert h.screen["view"].children[0].style == discord.ButtonStyle.danger
        await h.press("End the run now")
        assert ev.state == "done" and "Reopen works for an hour" in h.text
        await RunButton(reg.key, ev.key, "reopen").callback(h.card_press())
        assert ev.state == "locked" and "Reopened" in h.text
    run(go())


def test_the_closed_card_offers_reopen_only_for_a_recent_end(reg, rs):
    from oibot_gm.raid_views import closed_layout

    ev, _seated = locked_ten(reg, rs)
    ico = lambda k, v: ""  # noqa: E731
    rc.end_run(reg, rs, ev, "Jon")
    ids = [c.custom_id for c in closed_layout(reg, ev, ico).walk_children() if getattr(c, "custom_id", None)]
    assert ids == [f"runact.{reg.key}:{ev.key}:reopen"]
    ev.closed_at = "2020-01-01T00:00:00+00:00"
    assert not [c for c in closed_layout(reg, ev, ico).walk_children() if getattr(c, "custom_id", None)]
    ev2 = open_test_run(reg, rs, days=5)
    ev2.state, ev2.closed_at = "cancelled", rc.now()
    assert not [c for c in closed_layout(reg, ev2, ico).walk_children() if getattr(c, "custom_id", None)], "never a cancelled run (D19)"


# ---------------------------------------------------------------- /raid health at any time

def test_raid_health_on_a_locked_run_is_the_brief(reg, rs, officer, channel):  # noqa: F811
    ev, seated = locked_ten(reg, rs)
    for m in seated:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    h, bot = night(reg, rs)
    run(FLOWS["raid_health"](h.command(), reg))
    view = h.screen["view"]
    text = "\n".join(str(c.content) for c in view.walk_children() if hasattr(c, "content"))
    assert "Roster health" in text and "rostered" in text and "**Waiting on**" in text and "fill:" in text
