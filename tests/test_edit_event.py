"""The owner's tweak B (2026-11-24): one event, every field. An officer changes, for ONE event before its roster locks,
everything its schedule sets for a night — the sheet timing, whether the rostered confirm, who asks for open seats, the
priority (the five weights, and first-to-join versus priority) and each raid's split — through the reschedule verb
(RaidMixin.reschedule_event over rc.reschedule, rc.EVENT_TIMING). After the lock only the start, the automatic asks and
how long an ask waits still mean something; the rest is refused in one sentence each. The run carries its own priority
from its open (it was read from the schedule live), and a run already open is given the value it reads today at the
start's migration, so nothing about an open event changes at the upgrade."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from conftest import join, lock_with_board, weekly
from test_event_identity import HYJAL, _big_guys_pair, _old_guild, _soon
from test_guild_schedules import commit_count
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture
from test_reschedule import OFFICER_ID, moved_notices, open_one_off, real_people

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.constants import PAGE_WORDS
from oibot_gm.registry import RAID_WEIGHT_DEFAULTS

RID = "barrow_deeps"
OPENED = {"first_open": "2026-01-01T00:00"}


def run(coro):
    return asyncio.run(coro)


def scheduled(reg, rs, days: float = 4):
    reg.set_raid_overrides(RID, OPENED, "t")
    now = reg.now_local()
    t = (now + timedelta(days=days)).replace(hour=19, minute=30, second=0, microsecond=0)
    sid = weekly(reg, RID, t.strftime("%a %H:%M"))
    reg.set_schedule(sid, {"weights": {"rank": 5, "regular": 6}}, "t")
    return sid, rc.open_run(reg, rs, RID, t, schedule=sid)


# ---------------------------------------------------------------- the run carries its own priority

def test_a_new_run_carries_its_schedules_priority_and_a_later_schedule_edit_leaves_it(reg, rs):
    sid, ev = scheduled(reg, rs)
    want = dict(reg.schedule_def(RID, sid)["weights"])
    assert reg.config.roster(ev.team)["weights"] == want, "stamped at open"
    reg.set_schedule(sid, {"weights": {"rank": 0, "regular": 1}}, "t")
    assert rc.run_weights(reg, ev) == want, "a schedule edit never silently changes an open event's priority"
    assert rc.priority_words(reg, ev) == "Regulars first, then rank"


def test_a_later_raid_carries_the_weights_it_read_before(reg, rs):
    for r in (RID, HYJAL):
        reg.set_raid_overrides(r, OPENED, "t")
    first = open_one_off(reg, rs, days=4, raid=HYJAL, follow={"raid": RID, "rosters": 1})
    later = rc.later_parts(rs, first)[0]
    assert reg.config.roster(later.team)["weights"] == {**RAID_WEIGHT_DEFAULTS, **rc.ADHOC_WEIGHTS}, "a one-off's later raid: the ad hoc weights, as before"


def test_the_start_gives_an_open_event_the_values_it_reads_today(data_root, old_store_with):
    """big-guys-for's live two-raid first-come event, open now: after the start's migration its sheet, lock and roster
    inputs read exactly as before (weights, first come, confirmation, split, timing, the seat bonus, the fingerprint),
    and from then on a raid or schedule edit no longer reaches it."""
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    lead, later = rs.events[files[0]["key"]], rs.events[files[1]["key"]]

    def reads():
        out = {}
        for e in (lead, later):
            out[e.key] = (rc.run_weights(reg, e), rc.run_kind(reg, e), rc.run_first_come(reg, e), rc.run_confirms(reg, e),
                          rc.run_strategy(reg, e), rc.priority_words(reg, e),
                          rc.seat_bonus(reg, rs, e, rc.players_for(reg, e)))
        return out, rc.run_timing(reg, lead)
    before = reads()
    assert "weights" not in reg.config.roster(lead.team), "the premise: an old run dict without its own priority"
    assert rc.priority_words(reg, lead) == PAGE_WORDS["first_to_join"]
    n0 = commit_count(store)
    rc.migrate_at_start(reg, rs)
    store.flush()
    assert commit_count(store) == n0 + 1, "one commit with the stamps"
    assert reads() == before, "nothing about the open event changes at the upgrade"
    assert reg.config.roster(lead.team)["weights"] == before[0][lead.key][0]
    assert reg.config.roster(later.team)["weights"] == before[0][later.key][0]
    rev = rc.event_rev(reg, rs, lead)
    reg.set_raid_overrides(RID, {"weight_rank": 6}, "t")
    assert reads() == before and rc.event_rev(reg, rs, lead) == rev, "a raid edit no longer reaches the open event"
    assert rc.migrate_at_start(reg, rs) == []
    store.flush()
    assert commit_count(store) == n0 + 2, "a second start stamps nothing (the raid edit is the one commit)"


# ---------------------------------------------------------------- one verb, every field

def test_every_field_changes_on_one_event_before_the_lock(reg, rs):
    sid, ev = scheduled(reg, rs)
    bot = NightBot(reg, rs)
    stored = json.dumps(reg.config.schedules, sort_keys=True)
    timing = {"confirm": False, "weights": {"rank": 1, "sat_out": 4}, "split_policy": "rotation", "nudge": False,
              "fill_ask_hours": 3, "autofill": False}
    lines = run(bot.reschedule_event(reg, rs, ev, timing=timing, by="o", by_id=OFFICER_ID, preview=True))
    assert 1 <= len(lines) <= 3
    run(bot.reschedule_event(reg, rs, ev, timing=timing, by="o", by_id=OFFICER_ID))
    team = reg.config.roster(ev.team)
    assert team["confirm"] is False and rc.run_confirms(reg, ev) is False
    assert rc.run_weights(reg, ev)["rank"] == 1 and rc.run_weights(reg, ev)["sat_out"] == 4 and rc.run_weights(reg, ev)["regular"] == 6
    assert rc.run_strategy(reg, ev) == "rotation" and (team["autofill"], team["fill_ask_hours"], team["reminders"]) == (False, 3, rc.OFF)
    assert json.dumps(reg.config.schedules, sort_keys=True) == stored, "the schedule keeps its own"
    assert rc.reschedule_refusal(reg, rs, ev, None, {"confirm": False}) == rc.TIMING_SAME


def test_a_one_off_switches_to_priority_and_back(reg, rs):
    ev = open_one_off(reg, rs)
    assert rc.run_first_come(reg, ev) is True and rc.priority_words(reg, ev) == PAGE_WORDS["first_to_join"]
    rc.reschedule(reg, rs, ev, timing={"first_come": False, "weights": {"rank": 5}}, by="o")
    assert rc.run_first_come(reg, ev) is False and rc.priority_words(reg, ev).startswith("Signed up early first") is False
    assert rc.priority_words(reg, ev) != PAGE_WORDS["first_to_join"]
    rc.reschedule(reg, rs, ev, timing={"weights": {"signup_order": 6, "rank": 5}}, by="o")
    assert rc.priority_words(reg, ev) == "Early signups first, then rank", "never 'who signed up first first'"
    rc.reschedule(reg, rs, ev, timing={"first_come": True}, by="o")
    assert rc.run_first_come(reg, ev) is True and rc.priority_words(reg, ev) == PAGE_WORDS["first_to_join"]


def test_after_the_lock_only_what_still_means_something(reg, rs):
    sid, ev = scheduled(reg, rs)
    lock_with_board(reg, rs, ev, [])
    assert rc.reschedule_refusal(reg, rs, ev, None, {"lock_hours_before": 8}) == rc.TIMING_LOCKED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"confirm": False}) == rc.CONFIRM_LOCKED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"weights": {"rank": 1}}) == rc.PRIORITY_LOCKED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"first_come": True}) == rc.PRIORITY_LOCKED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"split_policy": "rotation"}) == cq.STRATEGY_LOCKED
    assert rc.reschedule_refusal(reg, rs, ev, None, {"autofill": False, "fill_ask_hours": 5}) is None
    rc.reschedule(reg, rs, ev, timing={"autofill": False, "fill_ask_hours": 5}, by="o")
    team = reg.config.roster(ev.team)
    assert (team["autofill"], team["fill_ask_hours"]) == (False, 5)
    said = cq.reschedule_consequence(reg, rs, ev, None, {"autofill": True})
    assert "until the roster locks" not in " ".join(said), "after the lock the undo line is not the lock's"


def test_a_lock_time_change_tells_who_answered_and_a_priority_change_tells_nobody(reg, rs):
    sid, ev = scheduled(reg, rs)
    ms = real_people(reg, 2)
    join(reg, rs, ev, ms)
    bot = NightBot(reg, rs)
    run(bot.reschedule_event(reg, rs, ev, timing={"confirm": False, "weights": {"rank": 2}}, by="o", by_id=OFFICER_ID))
    assert not moved_notices(reg, ms[0].discord_id), "confirmation and priority tell nobody"
    lines = run(bot.reschedule_event(reg, rs, ev, timing={"lock_hours_before": 6}, by="o", by_id=OFFICER_ID, preview=True))
    assert "notice" in lines[1]
    run(bot.reschedule_event(reg, rs, ev, timing={"lock_hours_before": 6}, by="o", by_id=OFFICER_ID))
    said = moved_notices(reg, ms[0].discord_id)
    assert len(said) == 1 and reg.local12(ev.start - timedelta(hours=6)) in said[0]["text"]


# ---------------------------------------------------------------- the box, the route, plain text

def test_the_edit_box_prints_the_schedule_editors_words_and_a_stale_one_is_409(reg, rs, web_bot):  # noqa: F811
    sid, ev = scheduled(reg, rs)
    c = web_bot.c
    url = f"/api/run/{ev.key}/reschedule"
    body = {"edit": True, "timing": {"confirm": False, "weights": {"rank": 2}, "lock_hours_before": 12}}
    b = c.post(url, json={**body, "preview": True}, headers=H()).json()["box"]
    F = PAGE_WORDS["schedule_fields"]
    assert b["verb"] == "Save" and b["title"].startswith("Change ")
    assert f"{F['confirm']}: {PAGE_WORDS['on']} → {PAGE_WORDS['off']}" in b["diff"]
    assert any(x.startswith(f"{F['lock_hours_before']}: ") and x.endswith("→ 12 hours before") for x in b["diff"])
    assert f"{F['weights']}: {PAGE_WORDS['priority']['rank']} 5 → {PAGE_WORDS['priority']['rank']} 2" in b["diff"]
    assert ev.key not in json.dumps(b)
    rc.reschedule(reg, rs, ev, timing={"nudge": False}, by="other officer")  # changed while the box is open
    r = c.post(url, json={**body, "seen": b["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["box"]["fp"] != b["fp"] and rc.run_confirms(reg, ev) is True
    ok = c.post(url, json={**body, "seen": r.json()["box"]["fp"]}, headers=H())
    assert ok.status_code == 200, ok.text
    assert rc.run_confirms(reg, ev) is False and rc.run_weights(reg, ev)["rank"] == 2


def test_the_event_view_carries_its_own_values_and_the_editor(reg, rs, web_bot):  # noqa: F811
    sid, ev = scheduled(reg, rs)
    rc.reschedule(reg, rs, ev, timing={"confirm": False, "weights": {"rank": 6, "regular": 0}, "autofill": False}, by="o")
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    facts = {f["label"]: f["value"] for f in j["details"]}
    assert facts["Confirmation"] == PAGE_WORDS["not_asked"] and "Confirm by" not in facts
    assert facts["Priority"].startswith("Rank first")
    assert facts["Open seats after the lock"] == PAGE_WORDS["officer_asks"]
    e = j["edit"]
    assert e["values"]["confirm"] is False and e["values"]["weights"]["rank"] == 6 and e["values"]["autofill"] is False
    assert set(e["fields"]) >= {"lock_hours_before", "confirm", "weights", "first_come", "split_policy", "autofill", "fill_ask_hours"}
    assert e["splits"] == [{"key": ev.key, "raid": "Barrow Deeps", "value": rc.run_strategy(reg, ev)}]
    assert j["actions"]["edit"] is True
    lock_with_board(reg, rs, ev, [])
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    assert sorted(j["edit"]["fields"]) == ["autofill", "fill_ask_hours"], "after the lock only what still means something"


def test_plain_text_reaches_the_new_fields(reg, rs):
    sid, ev = scheduled(reg, rs)
    ops = [ConfigOp(op="run_reschedule", target=ev.key, field="confirm", value="off"),
           ConfigOp(op="run_reschedule", target=ev.key, field="weight_rank", value="1"),
           ConfigOp(op="run_reschedule", target=ev.key, field="split_policy", value="rotation"),
           ConfigOp(op="run_reschedule", target=ev.key, field="first_come", value="on")]
    for op in ops:
        start, timing = configops._reschedule_args(reg, op)
        assert start is None and rc.reschedule_refusal(reg, rs, ev, None, rc.named_split(rs, ev, timing)) is None, op
    bot = NightBot(reg, rs)
    for op in ops:
        run(configops.apply_async(reg, op, "Jon", True, bot=bot))
    assert rc.run_confirms(reg, ev) is False and rc.run_weights(reg, ev)["rank"] == 1
    assert rc.run_strategy(reg, ev) == "rotation" and rc.run_first_come(reg, ev) is True


# ---------------------------------------------------------------- the fix round (review of 2026-11-24)

def _footer(reg, ev):
    from oibot_gm import raid_views

    e, _v = raid_views.sheet_message(reg, ev, rc.run_team(reg, ev), lambda *a, **k: "")
    return e.footer.text, e.to_dict()


def test_the_first_come_words_follow_the_events_own_seating(reg, rs):
    """The sheet footer, the member's status line, the Me page's queue note and the help snapshot read the event's own
    seating (rc.first_come_shown), so switching it either way changes what every surface says."""
    from oibot_gm.help import run_lines
    from oibot_gm.web.api_me import sheet_row_extras

    one = open_one_off(reg, rs)
    m = real_people(reg, 1)[0]
    join(reg, rs, one, [m])
    first_words = rc.FIRST_COME.capitalize()
    assert first_words in _footer(reg, one)[0] and rc.FIRST_COME in rc.answer_receipt(reg, one, m)
    assert sheet_row_extras(reg, one, m, rs)["queue_note"] and "first come" in " ".join(run_lines(reg, rs, one, True, True))
    rc.reschedule(reg, rs, one, timing={"first_come": False}, by="o")
    assert first_words not in _footer(reg, one)[0] and rc.FIRST_COME not in rc.answer_receipt(reg, one, m)
    assert sheet_row_extras(reg, one, m, rs)["queue_note"] is None
    assert "by priority, no regulars" in " ".join(run_lines(reg, rs, one, True, True))
    sid, ev = scheduled(reg, rs)
    join(reg, rs, ev, [m])
    assert first_words not in _footer(reg, ev)[0]
    rc.reschedule(reg, rs, ev, timing={"first_come": True}, by="o")
    assert first_words in _footer(reg, ev)[0] and rc.FIRST_COME in rc.answer_receipt(reg, ev, m)
    assert sheet_row_extras(reg, ev, m, rs)["queue_note"] and "· first come" in " ".join(run_lines(reg, rs, ev, True, True))


def test_the_live_two_raid_events_sheet_is_byte_identical_across_the_upgrade(data_root, old_store_with):
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    reg, rs, _store = _old_guild(data_root, old_store_with, cfg, files)
    lead = rs.events[files[0]["key"]]
    before = json.dumps(_footer(reg, lead)[1], sort_keys=True)
    rotation = {e.key: rc.rotation_weight(reg, e) for e in (lead, rs.events[files[1]["key"]])}
    rc.migrate_at_start(reg, rs)
    assert json.dumps(_footer(reg, lead)[1], sort_keys=True) == before, "nobody edited it: the sheet renders as before"
    for e in (lead, rs.events[files[1]["key"]]):
        team = reg.config.roster(e.team)
        assert team["rotation_sat_out"] == rotation[e.key] == int(reg.raid_def(e.instance)["weights"]["sat_out"]), \
            "the stamped rotation weight equals the raid's at stamping time: the rotation's model is unchanged"


def test_a_new_run_stamps_the_raids_rotation_weight_and_an_edit_of_sat_out_sets_it(reg, rs):
    sid, ev = scheduled(reg, rs)
    assert reg.config.roster(ev.team)["rotation_sat_out"] == int(reg.raid_def(RID)["weights"]["sat_out"]) == rc.rotation_weight(reg, ev)
    rc.reschedule(reg, rs, ev, timing={"weights": {"sat_out": 5}}, by="o")
    assert rc.rotation_weight(reg, ev) == 5


def test_a_weight_edit_changes_only_the_picked_weights_on_each_raid(reg, rs):
    for r in (RID, HYJAL):
        reg.set_raid_overrides(r, OPENED, "t")
    first = open_one_off(reg, rs, days=4, raid=HYJAL, follow={"raid": RID, "rosters": 1})
    later = rc.later_parts(rs, first)[0]
    reg.config.roster(later.team)["weights"] = {**rc.run_weights(reg, later), "main": 4}  # the later raid's own
    b = cq.reschedule_box(reg, rs, first, None, {"weights": {"rank": 5}}, edit=True)
    P, F = PAGE_WORDS["priority"], PAGE_WORDS["schedule_fields"]
    assert f"Hyjal Summit · {F['weights']}: {P['rank']} 0 → {P['rank']} 5" in b["diff"], b["diff"]
    assert f"Barrow Deeps · {F['weights']}: {P['rank']} 0 → {P['rank']} 5" in b["diff"], b["diff"]
    rc.reschedule(reg, rs, first, timing={"weights": {"rank": 5}}, by="o")
    assert rc.run_weights(reg, later)["rank"] == 5 and rc.run_weights(reg, later)["main"] == 4, "its own other weights stay"


def test_a_lock_moved_before_a_reminder_that_is_off_keeps_the_health_card_posting(reg, rs, monkeypatch):
    """The officers' health card goes out at the reminder time whatever the reminder switch: a lock moved past a reminder
    that is off takes the reminder time with it (never later than the lock), so the card still posts."""
    from test_event_identity import _scheduler

    sid, ev = scheduled(reg, rs, days=4)
    rc.reschedule(reg, rs, ev, timing={"nudge": False}, by="o")
    team = rc.run_team(reg, ev)
    assert float(team["cutoff_soft_hours"]) < 72
    assert rc.reschedule_refusal(reg, rs, ev, None, {"lock_hours_before": 72}) is None
    said = cq.reschedule_consequence(reg, rs, ev, None, {"lock_hours_before": 72})
    assert "reminder" not in " ".join(said), "nothing about a reminder that is off"
    rc.reschedule(reg, rs, ev, timing={"lock_hours_before": 72}, by="o")
    assert float(team["cutoff_soft_hours"]) == float(team["cutoff_hard_hours"]) == 72
    assert rc.reschedule_refusal(reg, rs, ev, None, {"nudge": True, "nudge_hours_before": 24}) == rc.TIMING_ORDER, "an ON reminder after the lock is refused"
    bot, _opened = _scheduler(reg, rs)
    posted = []

    async def health(*a, **k):
        posted.append(a[2].key)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(bot, "post_health", health)
    monkeypatch.setattr(bot, "lock_run", nothing)
    monkeypatch.setattr(reg, "now_local", lambda: ev.start - timedelta(hours=72) + timedelta(minutes=1))
    run(bot.scheduler_tick_guild(reg))
    assert posted == [ev.key], "the health card posts at the (moved) reminder time"


def test_an_empty_switch_is_refused_and_the_new_refusals_read_aloud(reg, rs):
    sid, ev = scheduled(reg, rs)
    for k in ("confirm", "first_come", "nudge", "autofill"):
        assert rc.reschedule_refusal(reg, rs, ev, None, {k: ""}) == rc.SWITCH_EMPTY
    assert rc.CONFIRM_LOCKED == "Asking the rostered to confirm is fixed once the roster locks."
    said = cq.reschedule_consequence(reg, rs, ev, None, {"first_come": True})
    assert said[0].startswith("For this event,") and said[2] == cq.SEATING_WORDS[True]
    one = open_one_off(reg, rs, days=6)
    assert cq.reschedule_consequence(reg, rs, one, None, {"first_come": False})[2] == "Join order no longer decides who gets a seat."
