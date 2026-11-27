"""Issue 14, Task 1: Auto Lock and Auto-fill on Auto Lock. Two switches on a schedule (for its events) and on a one-off's
open form, stamped on the event when it opens like the sheet timing, editable per event before the lock on every
surface (site, Discord, plain text, MCP) and refused after it in one sentence. Both default On, so a schedule or event
stored before them reads as it always did. With Auto Lock off the scheduler neither locks the event at its lock time
nor closes its pre-lock asks there (an ask past its own deadline still counts as no); the Details Locks row says an
officer locks it."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from conftest import open_test_run, weekly
from test_edit_event import scheduled
from test_flows_event import harness as event_harness
from test_flows_raid import officer  # noqa: F401 — the fixture
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture
from test_reschedule import OFFICER_ID, open_one_off, real_people

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.constants import PAGE_WORDS, field_help, switch_words
from oibot_gm.event_view import EDIT_AFTER_LOCK, EDIT_FIELDS
from oibot_gm.registry import ADHOC_DEFAULTS, RAID_DEFAULTS, SCHEDULE_TIMING
from oibot_gm.schedule_view import stored_entry
from oibot_gm.wizard import FLOWS

F = PAGE_WORDS["schedule_fields"]
BOTH = ("auto_lock", "auto_lock_fill")


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------- defaults, stamping

def test_both_default_on_for_a_schedule_and_an_event_stored_before_them(reg, rs):
    assert all(RAID_DEFAULTS[k] is True and ADHOC_DEFAULTS[k] is True and k in SCHEDULE_TIMING for k in BOTH)
    sid = weekly(reg)
    row = next(s for s in reg.config.schedules if s.get("id") == sid)
    for k in BOTH:  # a schedule as an older bot stored it
        row.pop(k, None)
    s = reg.schedule_entry(sid)
    sd = reg.schedule_def(s["raid"], sid)
    assert sd["auto_lock"] is True and sd["auto_lock_fill"] is True
    assert stored_entry(s)["auto_lock"] is True and stored_entry(s)["auto_lock_fill"] is True, "the editors start from On"
    ev = open_one_off(reg, rs)
    team = reg.config.roster(ev.team)
    for k in BOTH:  # an event opened by an older bot
        team.pop(k, None)
    assert rc.auto_lock(reg, ev) is True and rc.auto_lock_fill(reg, ev) is True
    t = rc.run_timing(reg, ev)
    assert t["auto_lock"] is True and t["auto_lock_fill"] is True


def test_a_schedules_switches_are_stamped_on_its_event_at_open_and_a_later_edit_leaves_it(reg, rs):
    sid, _ev = scheduled(reg, rs, days=4)
    reg.set_schedule(sid, {"auto_lock": False, "auto_lock_fill": False}, "t")
    s = reg.schedule_entry(sid)
    t = rc.next_runs_of(reg, s["raid"], s, 2)[-1]
    ev = rc.open_run(reg, rs, s["raid"], t, schedule=sid)
    team = reg.config.roster(ev.team)
    assert team["auto_lock"] is False and team["auto_lock_fill"] is False
    reg.set_schedule(sid, {"auto_lock": True}, "t")
    assert rc.auto_lock(reg, ev) is False, "an open event keeps what it was stamped with"


def test_a_one_offs_open_form_sets_both(reg, rs):
    ev = open_one_off(reg, rs, days=3, lock_hours=2, first_rosters=1, auto_lock=True, auto_lock_fill=False)
    assert rc.auto_lock(reg, ev) is True and rc.auto_lock_fill(reg, ev) is False
    ev2 = open_one_off(reg, rs, days=5, lock_hours=2, first_rosters=1, auto_lock=False)
    assert rc.auto_lock(reg, ev2) is False and rc.auto_lock_fill(reg, ev2) is True


def test_the_open_route_carries_the_one_offs_switches(reg, rs, web_bot):  # noqa: F811
    day = (reg.now_local() + timedelta(days=4)).date().isoformat()
    body = {"when": f"{day} 20:00", "schedule": "", "rosters": 1, "lineup": [],
            "timing": {"lock_hours_before": 2, "auto_lock": False, "auto_lock_fill": False}}
    r = web_bot.c.post("/api/raid/barrow_deeps/open", json=body, headers=H())
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    assert rc.auto_lock(reg, ev) is False and rc.auto_lock_fill(reg, ev) is False
    bad = web_bot.c.post("/api/raid/barrow_deeps/open", json={**body, "when": f"{day} 22:00", "timing": {"lock_hours_before": 2, "auto_lock": "maybe"}},
                         headers=H())
    assert bad.status_code == 400 and rc.SWITCH_EMPTY in bad.text


# ---------------------------------------------------------------- the scheduler obeys Auto Lock

def _past_its_lock(reg, rs):
    """A test event three hours out whose lock time was two hours ago, with a pre-lock ask out until its start."""
    ev = open_test_run(reg, rs, days=0, soft=6, hard=5, confirm=1)
    m = real_people(reg, 1)[0]
    ask = rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="tank", character=m.main.name,
                     spec=m.main.spec, reason="short a tank")
    return ev, ask


def _short_night(reg, rs):
    """The planner's worked example: a schedule's event short of roles, whose Ask (send_plan) sends real pre-lock asks."""
    from test_roster_p2_planner import worked_example

    ev, _people = worked_example(reg, rs)
    return ev


def _lock_passed(reg, ev):
    """Move `ev`'s lock time (and its reminder) to an hour ago, its start where it is."""
    team = reg.config.roster(ev.team)
    h = (ev.start - reg.now_local()).total_seconds() / 3600 + 1
    team["cutoff_hard_hours"], team["cutoff_soft_hours"] = h, h + 1


def _ask_all(bot, reg, rs, ev):
    sent, _ = run(bot.send_plan(reg, rs, ev, rev=rc.role_plan(reg, rs, ev).rev, by="Jon"))
    assert sent and all(a.phase == "prelock" for a in sent)
    return sent


def test_with_auto_lock_off_a_prelock_ask_runs_until_the_start(reg, rs):
    """The owner's ruling: with Auto Lock off a pre-lock ask's deadline is the start, and nothing happens at the old lock
    time — an ask sent before it and one sent after it are both still open a scheduler tick later."""
    from test_raid_night import _view_text

    bot = NightBot(reg, rs)
    ev = _short_night(reg, rs)
    rc.reschedule(reg, rs, ev, timing={"auto_lock": False}, by="o")
    before = _ask_all(bot, reg, rs, ev)
    assert all(a.expires_at == ev.start.isoformat() for a in before), "the start, never the lock time"
    card = _view_text(bot.cards[before[0].dm_message_id].payload)
    assert f"<t:{int(ev.start.timestamp())}:R>" in card and "No answer by the start" in card and "built at lock" not in card
    _lock_passed(reg, ev)
    rs.save(ev, "lock time passed")
    run(bot.scheduler_tick_guild(reg))
    assert ev.state == "open" and not ev.lock_tried_at and not ev.all_rosters, "no lock by the clock"
    assert all(a.answer is None for a in before), "nothing closes them at the old lock time"
    for a in before:  # asked again after the old lock time
        a.answer = "no"
    after = _ask_all(bot, reg, rs, ev)
    assert all(a.expires_at == ev.start.isoformat() for a in after)
    run(bot.scheduler_tick_guild(reg))
    assert all(a.answer is None for a in after), "an ask sent after the old lock time is still open a tick later"
    assert not any("locked" in t for _lvl, t in bot.ops.lines)
    after[0].expires_at = (reg.now_local() - timedelta(minutes=1)).isoformat()  # its deadline (the start) passes
    rs.save(ev, "deadline")
    run(bot.scheduler_tick_guild(reg))
    assert after[0].answer == "expired" and ev.state == "open"
    assert f"⌛ no answer from {after[0].display_name} — counts as no" in bot.updates, "the run thread says so, as a locked event's does"


def test_auto_lock_on_still_closes_the_asks_at_the_lock_time(reg, rs, monkeypatch):
    """The On path as today (the lock itself is test_raid_night's): a pre-lock ask's deadline is the lock time and the
    clock's lock closes it there."""
    from oibot_gm.raid_views import run_times

    bot = NightBot(reg, rs)
    ev = _short_night(reg, rs)
    _soft, hard, _c = run_times(reg, ev, rc.run_team(reg, ev))
    sent = _ask_all(bot, reg, rs, ev)
    assert all(a.expires_at == min(hard, ev.start).isoformat() for a in sent)
    _lock_passed(reg, ev)
    rs.save(ev, "lock time passed")
    tried = []

    async def lock_run(reg_, rs_, ev_, *a, **k):
        tried.append((ev_.key, k.get("fill")))
        return "locked"
    monkeypatch.setattr(bot, "lock_run", lock_run)
    run(bot.scheduler_tick_guild(reg))
    assert tried == [(ev.key, True)] and all(a.answer == "released" for a in sent)


def test_a_lock_after_the_start_leaves_every_confirmation_open_a_tick_later(reg, rs, monkeypatch):
    """The owner's ruling on a late lock: at or after the start the confirm deadline is an hour from now, never the start
    already past, so nobody's seat is freed at the next tick."""
    from conftest import by_role, join
    from test_raid_night import fast_propose

    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs, days=0)
    rc.reschedule(reg, rs, ev, timing={"auto_lock": False}, by="o")
    ev.starts_at = (reg.now_local() - timedelta(minutes=10)).isoformat()  # an officer locks it after its start
    rs.save(ev, "started")
    pool = reg.test_members()
    join(reg, rs, ev, pool)
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    monkeypatch.setattr(rc, "propose", fast_propose([m.display_name for m in seated]))
    run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked"
    now = reg.now_local()
    assert now < datetime.fromisoformat(ev.confirm_by) <= now + rc.MOVE_CONFIRM_GRACE
    asks = [a for m in seated for a in reg.members[m.discord_id].placement_asks if a.get("roster") == ev.team]
    assert asks and all(a.get("answer") is None for a in asks)
    run(bot.scheduler_tick_guild(reg))
    asks = [a for m in seated for a in reg.members[m.discord_id].placement_asks if a.get("roster") == ev.team]
    assert all(a.get("answer") is None for a in asks), "every confirmation ask still open"
    assert all(ev.seat_of(m) for m in seated), "nobody's seat freed"


def test_late_confirm_by_keeps_reschedules_rule(reg):
    """Reschedule passes no `lock`: a new start a few minutes in the past keeps that start as the deadline, as before."""
    now = reg.now_local()
    past, ahead = now - timedelta(minutes=4), now + timedelta(hours=3)
    assert rc.late_confirm_by(past, past, now) == past
    assert rc.late_confirm_by(past, past, now, lock=True) == now + rc.MOVE_CONFIRM_GRACE
    assert rc.late_confirm_by(past, ahead, now, lock=True) == now + rc.MOVE_CONFIRM_GRACE == rc.late_confirm_by(past, ahead, now)
    assert rc.late_confirm_by(ahead, ahead, now, lock=True) == ahead


def test_turning_auto_lock_on_after_the_lock_time_says_it_locks_within_a_minute(reg, rs):
    ev, _ask = _past_its_lock(reg, rs)
    rc.reschedule(reg, rs, ev, timing={"auto_lock": False}, by="o")
    said = cq.reschedule_consequence(reg, rs, ev, None, {"auto_lock": True})
    assert said[-1] == "It locks within a minute, because its lock time has passed.", said
    off = cq.reschedule_consequence(reg, rs, ev, None, {"lock_hours_before": 4})
    assert "within a minute" not in " ".join(off), "an Auto Lock off event never locks by the clock"


# ---------------------------------------------------------------- Edit event: before the lock on every surface, refused after

def test_edit_event_before_the_lock_and_refused_after(reg, rs):
    sid, ev = scheduled(reg, rs)
    assert {"auto_lock", "auto_lock_fill"} <= set(EDIT_FIELDS) and not set(BOTH) & set(EDIT_AFTER_LOCK)
    assert rc.reschedule_refusal(reg, rs, ev, None, {"auto_lock": False, "auto_lock_fill": False}) is None
    assert rc.reschedule_refusal(reg, rs, ev, None, {"auto_lock": ""}) == rc.SWITCH_EMPTY
    rc.reschedule(reg, rs, ev, timing={"auto_lock": False, "auto_lock_fill": False}, by="o")
    assert rc.auto_lock(reg, ev) is False and rc.auto_lock_fill(reg, ev) is False
    from conftest import lock_with_board

    lock_with_board(reg, rs, ev, [])
    assert rc.reschedule_refusal(reg, rs, ev, None, {"auto_lock": True}) == rc.AUTO_LOCK_LOCKED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"auto_lock_fill": True}) == rc.AUTO_LOCK_LOCKED
    assert rc.AUTO_LOCK_LOCKED == "Auto Lock and its auto-fill are fixed once the roster locks."


def test_the_site_edits_both_and_the_box_reads_the_editors_words(reg, rs, web_bot):  # noqa: F811
    sid, ev = scheduled(reg, rs)
    url = f"/api/run/{ev.key}/reschedule"
    body = {"edit": True, "timing": {"auto_lock": False, "auto_lock_fill": False}}
    b = web_bot.c.post(url, json={**body, "preview": True}, headers=H()).json()["box"]
    assert "Auto Lock: On → Off" in b["diff"] and "Auto-fill on Auto Lock: On → Off" in b["diff"], b["diff"]
    assert b["sentences"][0] == "For this event, an officer locks it.", "the clock's fill is not described when no clock locks it"
    fill_only = cq.reschedule_consequence(reg, rs, ev, None, {"auto_lock_fill": False})
    assert fill_only[0] == "For this event, Auto Lock rosters the board as it stands.", fill_only
    ok = web_bot.c.post(url, json={**body, "seen": b["fp"]}, headers=H())
    assert ok.status_code == 200, ok.text
    assert rc.auto_lock(reg, ev) is False and rc.auto_lock_fill(reg, ev) is False
    waits = cq.reschedule_consequence(reg, rs, ev, None, {"auto_lock_fill": True})
    assert waits[0] == "This event keeps its own Auto-fill on Auto Lock for when Auto Lock is on.", waits
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    assert j["edit"]["values"]["auto_lock"] is False and {"auto_lock", "auto_lock_fill"} <= set(j["edit"]["fields"])


def test_discord_edit_event_offers_auto_lock_and_hides_its_fill_while_off(reg, rs, officer):  # noqa: F811
    ev = open_one_off(reg, rs, days=4)
    h, _bot = event_harness(reg, rs)
    run(FLOWS["raid_edit"](h.command(), reg))
    assert f"{F['auto_lock']}: On" in h.text and f"{F['auto_lock_fill']}: On" in h.text

    def offered():
        sel = next(s for s in h.screen["view"].children if getattr(s, "placeholder", "") == "Change a setting")
        return {o.value for o in sel.options}
    assert {"auto_lock", "auto_lock_fill"} <= offered()
    run(h.pick("Change a setting", "auto_lock"))
    run(h.pick(F["auto_lock"], "off"))
    assert "auto_lock_fill" not in offered(), "the auto-fill under an Auto Lock that is off is not offered"
    run(h.press("Review changes"))
    run(h.press("Save"))
    assert rc.auto_lock(reg, ev) is False and "✅" in h.text


def test_plain_text_and_mcp_reach_both(reg, rs):
    import httpx
    from test_mcp import TOKEN

    from oibot_gm import mcp_server as ms

    sid, ev = scheduled(reg, rs)
    bot = NightBot(reg, rs)
    for f in BOTH:
        op = ConfigOp(op="run_reschedule", target=ev.key, field=f, value="off")
        assert configops._reschedule_args(reg, op) == (None, {f: "off"})
        run(configops.apply_async(reg, op, "Jon", True, bot=bot))
    assert rc.auto_lock(reg, ev) is False and rc.auto_lock_fill(reg, ev) is False
    assert "auto_lock" in ms.reschedule_run.__doc__ and "auto_lock_fill" in ms.schedule_set.__doc__
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"message": "changed"})
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        ms.reschedule_run(ev.key, timing={"auto_lock": "on"}, rev="r1")
        assert seen[-1]["timing"] == {"auto_lock": "on"}
    finally:
        ms.use(None)


def test_plain_text_sets_a_schedules_switches(reg):
    sid = weekly(reg)
    op = ConfigOp(op="schedule_set", target=sid, field="auto_lock", value="false")
    configops.apply(reg, op, "Jon", True)
    assert reg.schedule_entry(sid)["auto_lock"] is False
    ops = configops.ops_from_changes(reg, [{"kind": "schedule", "target": sid, "fields": {"auto_lock_fill": False}}],
                                     revs={f"config:schedule:{sid}": reg.config_rev(f"schedule:{sid}")})
    assert configops.describe_ops(reg, ops).box["diff"] == ["Auto-fill on Auto Lock: On → Off"], "the site's Save box: the editor's words"


# ---------------------------------------------------------------- the words

def test_details_name_both_with_their_hints_and_locks_says_an_officer_locks_it(reg, rs, web_bot):  # noqa: F811
    sid, ev = scheduled(reg, rs)
    facts = {f["label"]: f for f in web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["details"]}
    assert facts[F["auto_lock"]]["value"] == "On" and facts[F["auto_lock_fill"]]["value"] == "On"
    assert facts[F["auto_lock"]]["help"] == field_help("auto_lock", F["auto_lock"]) and facts[F["auto_lock_fill"]]["help"]
    assert facts["Locks"]["value"] != PAGE_WORDS["officer_locks"]
    rc.reschedule(reg, rs, ev, timing={"auto_lock": False}, by="o")
    facts = {f["label"]: f for f in web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["details"]}
    assert facts["Locks"]["value"] == PAGE_WORDS["officer_locks"] == "An officer locks it."
    assert facts[F["auto_lock"]]["value"] == "Off" and F["auto_lock_fill"] not in facts, "its auto-fill means nothing while it is off"
    # the schedule's Details and Sheet timing say the same
    reg.set_schedule(sid, {"auto_lock": False}, "t")
    sv = web_bot.c.get(f"/api/schedules/{sid}", headers=H()).json()
    rows = {f["label"]: f["value"] for f in sv["details"] + sv["timing"]}
    assert rows[F["auto_lock"]] == "Off" and rows["Locks"] == PAGE_WORDS["officer_locks"]
    assert all(n["locks"] == PAGE_WORDS["officer_locks"] for n in sv["next"])


def test_the_values_and_hints_are_the_editors(reg):
    assert (F["auto_lock"], F["auto_lock_fill"]) == ("Auto Lock", "Auto-fill on Auto Lock")
    assert [switch_words(k, v) for k in BOTH for v in (True, False)] == ["On", "Off", "On", "Off"]
    help_ = PAGE_WORDS["schedule_field_help"]
    assert all(help_[k] and len(help_[k]) < 300 for k in BOTH)
    assert cq.event_value_words("auto_lock", False) == "Off"


def test_the_one_offs_discord_screen_picks_auto_lock(reg, rs, officer):  # noqa: F811
    from oibot_gm.flows_raid import AUTO_LOCK_PICKS, auto_lock_pick

    opts = auto_lock_pick(True, True)
    assert [o.label for o in opts] == ["Auto Lock: On · Auto-fill on Auto Lock: On", "Auto Lock: On · Auto-fill on Auto Lock: Off",
                                       "Auto Lock: Off"]
    assert [o.default for o in opts] == [True, False, False] and AUTO_LOCK_PICKS["off"][0] is False
    assert [o.default for o in auto_lock_pick(False, False)] == [False, False, True]

