"""Phase 4, Task 1: the confirmation box (design §7b, D18) — ONE shape every press that asks first prints: a title, at
most three sentences under the Confirm rule, the verb (None = a refusal, only Back), red only when it cannot be undone,
the "label: old → new" lines and a fingerprint the action route re-checks (`seen` → 409 with the fresh box)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import join, weekly
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.constants import CONFIRM_WORDS

HYJAL, RID, ONY = "hyjal_summit_forever", "barrow_deeps", "onyxias_lair"
EVENING = [{"raid": RID, "rosters": 2}]
REAL = 471_000_000_000_000_000
CLASSES = [("Warrior", "Protection"), ("Priest", "Holy"), ("Priest", "Holy"), ("Mage", "Fire"), ("Mage", "Fire"), ("Rogue", "Combat")]


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:  # the profile's raids first open 9 Dec: every event here is after it
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def people(reg, n: int = 24):
    out = []
    for i in range(n):
        cls, spec = CLASSES[i % len(CLASSES)]
        reg.add_character(REAL + i, f"Member{chr(97 + i)}", f"Char{chr(97 + i)}", cls, spec, None, True)
        out.append(reg.members[REAL + i])
    return out


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def one_off(reg, rs, days: float = 3, lineup=EVENING):
    return rc.open_run(reg, rs, HYJAL, start_in(reg, days), by="t", lineup=lineup)


def tuesday(reg, rs=None, *, lineup=True) -> str:
    sid = weekly(reg, HYJAL, "Tue 18:30", sid="tue")
    reg.set_schedule(sid, {"name": "Tuesday raid night", **({"lineup": [{"raid": HYJAL, "rosters": 1}, *EVENING]} if lineup else {})}, "t")
    return sid


def next_tuesday(reg, sid):
    now = reg.now_local()
    return next(t for s, t in rc.occurrences(reg, now, 24 * 21) if s["id"] == sid and t - now >= timedelta(hours=30))


# ---------------------------------------------------------------- the shape

def test_the_box_shape_and_its_fingerprint():
    b = cq.box("Lock it?", ["One.", "Two."], "Lock", diff=["A: 1 → 2"])
    assert set(b) == {"title", "sentences", "verb", "danger", "diff", "fp"} and len(b["fp"]) == 12 and not b["danger"]
    import hashlib
    assert b["fp"] == hashlib.sha1("Lock it?\nA: 1 → 2\nOne.\nTwo.".encode()).hexdigest()[:12]
    assert cq.box("Lock it?", ["One.", "Two."], "Lock", diff=["A: 1 → 2"], rev="x")["fp"] != b["fp"], "the write's fingerprint counts"
    r = cq.refusal_box("That time has passed", "No.")
    assert r["verb"] is None and r["sentences"] == ["No."]
    assert CONFIRM_WORDS["changed"] == "This changed while the box was open. Read it again before you press."


def test_undo_and_leave_words():
    assert cq.undo_box(4)["sentences"] == ["Your 4 moves on this board are dropped.", "Everyone goes back to where the lock would place them."]
    assert cq.undo_box(1)["sentences"][0] == "Your one move on this board is dropped."
    assert (cq.undo_box(4)["title"], cq.undo_box(4)["verb"]) == ("Undo your moves?", "Undo moves")
    assert cq.leave_unsaved_sentence(2, "Hyjal Summit") == "Your 2 edits to Hyjal Summit are not saved yet."
    assert cq.leave_unsaved_sentence(1, "Hyjal Summit") == "Your one edit to Hyjal Summit is not saved yet."
    b = cq.leave_box(2, "Hyjal Summit")
    assert (b["title"], b["verb"], b["danger"]) == ("Leave without saving?", "Discard edits", True)


def test_the_word_helpers(reg, rs):
    assert rc.lineup_words(reg, [{"raid": HYJAL, "rosters": 1}, *EVENING]) == "Hyjal Summit, then Barrow Deeps × 2"
    assert rc.lineup_roster_words(reg, [{"raid": HYJAL, "rosters": 1}, *EVENING]) == "Hyjal Summit and both Barrow Deeps rosters"
    assert rc.lineup_roster_words(reg, [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 3}, {"raid": ONY, "rosters": 1}]) == \
        "Hyjal Summit, all 3 Barrow Deeps rosters and Onyxia's Lair"
    ev = one_off(reg, rs)
    assert rc.event_lineup(reg, ev) == [{"raid": HYJAL, "rosters": 1}, *EVENING]
    assert rc.event_lineup(reg, rc.event_parts(rs, ev)[1]) == rc.event_lineup(reg, ev), "any part stands for its event"
    assert rc.priority_words(reg, ev) == "First to join"
    sid = tuesday(reg)
    assert rc.priority_words(reg, reg.schedule_def(HYJAL, sid)) == "Regulars first, then rank"
    assert (rc.confirmation_words(True), rc.confirmation_words(False)) == ("Asked at the lock", "Not asked")
    assert cq.count_words(3) == "three" and cq.count_words(11) == "11"


# ---------------------------------------------------------------- the event boxes

def test_lock_box_names_the_lineup_and_says_one_off_rules(reg, rs):
    ev = one_off(reg, rs)
    b = cq.lock_box(reg, ev)
    assert b["title"] == f"Lock {reg.local12(ev.starts_at)}?" and b["verb"] == "Lock" and not b["danger"]
    assert b["sentences"] == ["The board becomes the roster for Hyjal Summit and both Barrow Deeps rosters.",
                              "Nobody is asked to confirm.", "You can still move people after the lock."]
    assert cq.lock_box(reg, rc.event_parts(rs, ev)[1])["title"] == b["title"], "a later raid's box names the event"


def test_lock_box_on_a_scheduled_event_says_who_confirms(reg, rs):
    sid = tuesday(reg)
    ev = rc.open_run(reg, rs, HYJAL, next_tuesday(reg, sid), schedule=sid)
    assert cq.lock_box(reg, ev)["sentences"][1:] == ["Nobody has joined yet, so nobody is asked to confirm."]
    join(reg, rs, ev, people(reg, 20))
    s = cq.lock_box(reg, ev)["sentences"]
    assert s[0] == "The board becomes the roster for Hyjal Summit and both Barrow Deeps rosters."
    assert s[1] == "The 20 rostered get the Confirm or Can't make it DM." and s[2].startswith("They have until ")


def test_cancel_box_is_red_and_says_the_event(reg, rs):
    ev = one_off(reg, rs)
    join(reg, rs, ev, people(reg)[:18])
    b = cq.cancel_box(reg, ev)
    assert b["verb"] == "Cancel event" and b["danger"] and b["title"] == f"Cancel {reg.local12(ev.starts_at)}?"
    assert b["sentences"] == ["Both raids of this event are cancelled.", "The 18 who joined see it on the sheet, and nobody gets a DM.",
                              "A cancelled event cannot be reopened."]
    alone = rc.open_run(reg, rs, RID, start_in(reg, 4), by="t")
    assert cq.cancel_box(reg, alone)["sentences"][:2] == ["This event is cancelled.", "Nobody has joined, and nobody gets a DM."]


def test_the_reschedule_box_moves_the_event(reg, rs):
    ev = one_off(reg, rs)
    join(reg, rs, ev, people(reg)[:18])
    new = ev.start + timedelta(days=1, minutes=30)
    b = cq.reschedule_box(reg, rs, ev, new, None)
    assert (b["title"], b["verb"]) == (f"Move {reg.local12(ev.start)}?", "Move")
    assert b["sentences"] == [f"The event moves to {reg.local12(new)}.", "All 18 answers stand, and each person gets a notice.",
                              "The sheet in Discord shows the new time."]


def test_the_past_refusal_is_one_sentence_with_a_title(reg, rs, web_bot):
    ev = one_off(reg, rs)
    past = reg.now_local().replace(second=0, microsecond=0) - timedelta(days=3)
    said = rc.reschedule_refusal(reg, rs, ev, past, None)
    assert said == cq.move_past_sentence(reg, past, ev.start) == f"{reg.local12(past)} is in the past, so the event stays at {reg.local12(ev.start)}."
    r = web_bot.c.post(f"/api/run/{ev.key}/reschedule", json={"start": past.isoformat(), "preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["error"] == said
    assert {k: r.json()["box"][k] for k in ("title", "sentences", "verb", "danger")} == {
        "title": "That time has passed", "sentences": [said], "verb": None, "danger": False}


def test_the_lineup_box_has_its_diff(reg, rs):
    ev = one_off(reg, rs)
    join(reg, rs, ev, people(reg)[:18])
    want = [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1}]
    b = cq.lineup_box(reg, rs, ev, want)
    assert (b["title"], b["verb"], b["diff"]) == ("Save this lineup?", "Save lineup", ["Barrow Deeps: 2 rosters → 1 roster"])
    assert b["sentences"] == ["Barrow Deeps goes from two rosters to one.", "All 18 answers stand."]
    gone = cq.lineup_box(reg, rs, ev, [{"raid": HYJAL, "rosters": 1}, {"raid": ONY, "rosters": 1}])
    assert gone["sentences"][0] == "Barrow Deeps leaves the lineup."
    ev.state = "locked"
    assert cq.lineup_box(reg, rs, ev, want) == cq.refusal_box("The lineup is fixed", rc.LINEUP_LOCKED)


def test_switch_and_answer_boxes(reg, rs):
    ev = one_off(reg, rs)
    ms = people(reg, 4)
    join(reg, rs, ev, ms[:3])
    nyx = ms[2]  # a Holy priest who joined
    b = cq.switch_box(reg, ev, nyx, None, "Shadow", [HYJAL])
    assert (b["title"], b["verb"]) == (f"Switch {nyx.display_name} to Shadow?", "Switch")
    assert b["sentences"] == [f"{nyx.display_name} plays Shadow for Hyjal Summit, not Holy.", "They get a notice.",
                              "You can switch them back until the lock."]
    a = cq.switch_box(reg, ev, nyx, None, "Shadow", [HYJAL], locked=True)
    assert (a["title"], a["verb"]) == (f"Ask {nyx.display_name} to play Shadow?", "Ask")
    assert a["sentences"][1:] == ["They get a DM with Yes and No.", "Their seat changes only when they say yes."]
    nyx.dm_opt_out = True
    assert cq.switch_box(reg, ev, nyx, None, "Shadow", [HYJAL], locked=True)["sentences"][1] == "It waits on their Me page."
    tavi = ms[3]  # one character: the mockup's words
    ab = cq.answer_box(reg, ev, tavi, "join", tavi.main.label, "Fire", by_id=1)
    assert (ab["title"], ab["verb"]) == ("Answer for someone", "Record answer")
    assert ab["sentences"] == [f"{tavi.display_name} is marked as joined, playing Fire.", "They get a notice.",
                               "They can change the answer themselves."]
    reg.add_character(tavi.discord_id, tavi.display_name, "Altonar", "Mage", "Frost", None, False)  # several: which one is said
    tavi = reg.members[tavi.discord_id]
    assert cq.answer_box(reg, ev, tavi, "join", "Altonar", "Frost", by_id=1)["sentences"][0] == \
        f"{tavi.display_name} is marked as joined, playing Altonar, Frost."
    assert cq.answer_box(reg, ev, tavi, "join", tavi.main.label, "Fire", by_id=1)["sentences"][0] == \
        f"{tavi.display_name} is marked as joined, playing {tavi.main.label}, Fire."


def test_the_swap_after_the_lock_reads_as_the_mockup(reg, rs):
    ev = one_off(reg, rs)
    ev.state = "locked"
    assert cq.consequence("swap", reg, ev, name="Anathema", other="Borin") == [
        "Anathema and Borin swap seats.", "Both see the change on the roster card.", "You can swap them back."]


def test_open_the_sheet_now(reg, rs):
    start = start_in(reg, 6)
    opens = reg.now_local() + timedelta(days=3, minutes=5)
    lock_at = start - timedelta(hours=24)
    b = cq.open_now_box(reg, start, opens, lock_at)
    assert (b["title"], b["verb"]) == (f"Open the sheet for {reg.local12(start, '%a %d %b')}?", "Open the sheet")
    assert b["sentences"] == ["The sheet opens now, three days early.", "It posts in the signup channel.", f"It still locks {reg.local12(lock_at)}."]
    assert cq.open_now_box(reg, start, reg.now_local() + timedelta(hours=5), lock_at)["sentences"][0] == "The sheet opens now, five hours early."


# ---------------------------------------------------------------- a stale box (Review Focus 5)

def test_a_stale_box_is_refused_with_the_fresh_one(reg, rs, web_bot, monkeypatch):
    from test_lineups import seat_first

    monkeypatch.setattr(rc, "propose", seat_first())
    sid = weekly(reg, RID, "Tue 18:30")
    ev = rc.open_run(reg, rs, RID, next_tuesday(reg, sid), schedule=sid)
    ms = people(reg, 6)
    join(reg, rs, ev, ms[:4])
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"preview": True}, headers=H())
    assert r.status_code == 200, r.text
    a = r.json()["box"]
    join(reg, rs, ev, ms[4:5])
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"seen": a["fp"]}, headers=H())
    assert r.status_code == 409 and ev.state == "open", "nothing is locked on a box that changed"
    fresh = r.json()["box"]
    assert r.json()["error"] == CONFIRM_WORDS["changed"] and fresh["fp"] != a["fp"] and fresh == cq.lock_box(reg, ev, rs)
    assert "The 5 rostered" in fresh["sentences"][1] and "The 4 rostered" in a["sentences"][1], "the fresh box names the new count"
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"seen": fresh["fp"]}, headers=H())
    assert r.status_code == 200 and ev.state == "locked", r.text


def test_cancel_with_a_stale_box_writes_nothing(reg, rs, web_bot):
    ev = one_off(reg, rs)
    ms = people(reg, 3)
    join(reg, rs, ev, ms[:2])
    a = web_bot.c.post(f"/api/run/{ev.key}/cancel", json={"preview": True}, headers=H()).json()["box"]
    join(reg, rs, ev, ms[2:])
    r = web_bot.c.post(f"/api/run/{ev.key}/cancel", json={"seen": a["fp"]}, headers=H())
    assert r.status_code == 409 and ev.state == "open" and "The 3 who joined" in r.json()["box"]["sentences"][1]


# ---------------------------------------------------------------- the schedule boxes

def test_schedule_boxes_read_as_the_mockups(reg, rs):
    sid = tuesday(reg)
    entry = reg.schedule_entry(sid)
    first = rc.next_runs_of(reg, HYJAL, entry, n=1)[0]
    c = cq.schedule_create_box(reg, entry)
    assert (c["title"], c["verb"]) == ("Create Tuesday raid night?", "Create schedule")
    assert c["sentences"][0] == f"An event opens every Tuesday at 6:30 PM, starting {reg.local12(first, '%a %d %b')}."
    assert c["sentences"][2] == "You can pause or edit the schedule at any time."
    # one open event: Save (14), Pause (15)
    t = next_tuesday(reg, sid)
    ev = rc.open_run(reg, rs, HYJAL, t, schedule=sid)
    after = next(x for x in rc.next_runs_of(reg, HYJAL, entry, n=4) if x != t)
    s = cq.schedule_save_box(reg, rs, sid, {"lock_hours_before": 12}, ["Locks: 24 hours before → 12 hours before"])
    assert (s["title"], s["verb"], s["diff"]) == ("Save Tuesday raid night?", "Save", ["Locks: 24 hours before → 12 hours before"])
    assert s["sentences"] == [f"The change starts with the {reg.local12(after, '%a %d %b')} event.",
                              f"The {reg.local12(ev.start, '%a %d %b')} event is already open and keeps its timing."]
    p = cq.schedule_pause_box(reg, rs, sid)
    assert (p["title"], p["verb"]) == ("Pause Tuesday raid night?", "Pause")
    assert p["sentences"] == ["No new sheet opens from this schedule.", "The event already open stays open.", "Regulars keep their spots."]
    # Remove with 6 regulars (17), one open event
    for m in people(reg, 6):
        reg.regular_add(m.discord_id, sid, "t")
    rm = cq.schedule_remove_box(reg, rs, sid)
    assert (rm["title"], rm["verb"], rm["danger"]) == ("Remove Tuesday raid night?", "Remove schedule", True)
    assert rm["sentences"][0] == "The schedule and its 6 regular spots are removed, and each regular sees a notice."
    assert rm["sentences"][1].startswith("The open event keeps its sheet") and rm["sentences"][2] == "A removed schedule cannot be brought back."
    # a regular's removal (18): their answer is kept only when they have one
    m = reg.members[REAL]
    rr = cq.regular_remove_box(reg, m, sid, rs=rs)
    assert (rr["title"], rr["verb"]) == (f"Take {m.display_name} off the regulars?", "Remove")
    assert rr["sentences"] == [f"{m.display_name} is no longer a regular of Tuesday raid night.", "You can add them again."]
    join(reg, rs, ev, [m])
    assert cq.regular_remove_box(reg, m, sid, rs=rs)["sentences"] == [f"{m.display_name} is no longer a regular of Tuesday raid night.",
                                                                      "They keep their answer on the open event.", "You can add them again."]


def test_resume_says_when_the_next_sheet_opens(reg, rs):
    sid = tuesday(reg, lineup=False)
    reg.set_schedule(sid, {"active": "false", "signup_lead_hours": 24 * 20}, "t")
    entry = reg.schedule_entry(sid)
    nxt = rc.next_runs_of(reg, HYJAL, entry, n=1)[0]
    b = cq.schedule_resume_box(reg, rs, sid)
    assert (b["title"], b["verb"]) == ("Resume Tuesday raid night?", "Resume")
    assert b["sentences"] == ["Sheets open from this schedule again.", f"The next one opens now, for {reg.local12(nxt)}."]


def test_raid_save_box_counts_the_open_events(reg, rs):
    reg.set_minimums(HYJAL, {"healer": {"min": 5}}, "t")
    rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t")
    d = configops.describe_ops(reg, [ConfigOp(op="comp_target", target=HYJAL, field="healer", value="4")])
    assert d.box is not None and (d.box["title"], d.box["verb"]) == ("Save Hyjal Summit?", "Save")
    assert d.box["sentences"] == ["The new minimum applies at the next lock.", "That includes the event open now."]
    assert d.box["diff"] == ["Healers, minimum: 5 → 4"]
    assert cq.raid_save_box(reg, rs, HYJAL, {"weight:rank"})["sentences"][0] == "The new priority applies at the next lock."
    assert cq.raid_save_box(reg, rs, HYJAL, {"lockout_days"})["sentences"][0] == "Lockout checks use the new values from now on."
    assert cq.raid_save_box(reg, rs, HYJAL, {"minimum:healer", "weight:rank"})["sentences"][0] == "The changes apply at the next lock."


def test_describe_ops_carries_the_box(reg):
    sid = tuesday(reg)
    d = configops.describe_ops(reg, [ConfigOp(op="schedule_set", target=sid, field="active", value="false")])
    assert d.box is not None and d.box["title"] == "Pause Tuesday raid night?" and d.box["sentences"] == d.consequences
    assert d.json_for(reg)["box"] == d.box
    rm = configops.describe_ops(reg, [ConfigOp(op="schedule_remove", target=sid)])
    assert rm.box["verb"] == "Remove schedule" and rm.box["danger"]


def test_a_raid_with_only_minimums_resets_and_names_them(reg):
    reg.set_minimums(RID, {"healer": {"min": 3}}, "t")
    b = cq.raid_reset_box(reg, RID)
    assert b["verb"] == "Reset" and b["danger"] and "minimums for Healers" in b["sentences"][1]
    assert b["fp"] == cq.box(b["title"], b["sentences"], "Reset", danger=True, rev=reg.config_rev(f"raid_all:{RID}"))["fp"]


# ---------------------------------------------------------------- fix round 1

def test_a_rename_is_not_reviewed_and_a_time_change_is(reg, rs):
    sid = tuesday(reg)
    ev = rc.open_run(reg, rs, HYJAL, next_tuesday(reg, sid), schedule=sid)
    d = configops.describe_ops(reg, [ConfigOp(op="schedule_set", target=sid, field="name", value="Tuesdays")])
    assert d.box["sentences"] == ["The change shows from now on."] and not d.review, "a rename says nothing of timing and isn't reviewed"
    t = configops.describe_ops(reg, [ConfigOp(op="schedule_set", target=sid, field="lock_hours_before", value="30")])
    assert f"The {reg.local12(ev.start, '%a %d %b')} event is already open and keeps its timing." in t.consequences and t.review
    r = configops.describe_ops(reg, [ConfigOp(op="schedule_set", target=sid, field="rosters", value="2")])
    assert r.box["sentences"][1] == f"The {reg.local12(ev.start, '%a %d %b')} event is already open and keeps its lineup." and not r.review


def _roster_two(reg, rs, ms):
    ev = one_off(reg, rs)
    join(reg, rs, ev, ms)
    bd = rc.event_parts(rs, ev)[1]
    k = rc.groups_per_roster(reg, rc.run_size(reg, bd))
    bd.layout = [[ms[0].display_name]] + [[] for _ in range(k - 1)] + [[ms[1].display_name, ms[2].display_name]]
    bd.pins = {str(ms[2].discord_id): "in"}
    rs.save(bd, "board on roster 2")
    return ev, bd, k


def test_a_roster_that_goes_drops_its_board_places(reg, rs):
    from test_raid_night import NightBot

    ms = people(reg, 6)
    ev, bd, k = _roster_two(reg, rs, ms)
    want = [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1}]
    ch = rc.plan_lineup(reg, rs, ev, want)
    assert ch.trimmed_places == {bd.key: 2} and ch.trimmed_pins == {bd.key: 1}
    b = cq.lineup_box(reg, rs, ev, want)
    assert b["sentences"] == ["Barrow Deeps goes from two rosters to one.", "All 6 answers stand.", "Moves made on the removed roster are dropped."]
    bot = NightBot(reg, rs)
    asyncio.run(bot.edit_lineup(reg, rs, ev, want, by="Jon", by_id=None))
    bd = rs.events[bd.key]
    assert bd.layout == [[ms[0].display_name]] + [[] for _ in range(k - 1)] and bd.pins == {}, "the places and pin on roster 2 go"
    assert all(str(m.discord_id) in bd.signups for m in ms), "the people stay joined"
    assert rc.run_rosters(reg, bd) == 1 and len(rc.board_layout(reg, bd)) == k, "the lock builds one roster from this board"


def test_no_places_on_the_roster_that_goes_no_sentence(reg, rs):
    ms = people(reg, 3)
    ev = one_off(reg, rs)
    join(reg, rs, ev, ms)
    b = cq.lineup_box(reg, rs, ev, [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1}])
    assert not any("Moves made" in x for x in b["sentences"])


def test_a_loss_is_never_cut_with_a_raid_and_a_roster_going(reg, rs):
    ms = people(reg, 6)
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=[{"raid": RID, "rosters": 2}, {"raid": ONY, "rosters": 1}])
    join(reg, rs, ev, ms)
    bd, ony = rc.event_parts(rs, ev)[1:]
    k = rc.groups_per_roster(reg, rc.run_size(reg, bd))
    bd.layout = [[] for _ in range(k)] + [[ms[1].display_name]]
    ony.layout = [[ms[3].display_name]]
    rs.save(bd, "b")
    rs.save(ony, "o")
    said = cq.lineup_consequence(reg, rs, ev, [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1}])
    assert len(said) <= 3 and "Onyxia's Lair is taken out with its board place." in said and "Moves made on the removed roster are dropped." in said


def test_a_cancel_after_the_lock_counts_fill_asks_to_people_not_rostered(reg, rs):
    from conftest import lock_with_board

    ms = people(reg, 6)
    ev = rc.open_run(reg, rs, RID, start_in(reg, 3), by="t")
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:5]])
    ev.fill_asks.append(rc.FillAsk(discord_id=ms[5].discord_id, display_name=ms[5].display_name, kind="other", role="healer",
                                   character="X", spec="Holy", reason="short 1 healer"))
    rs.save(ev, "an ask")
    assert cq.cancel_box(reg, ev, rs)["sentences"][1].endswith(", and 1 open ask card says Cancelled.")


def test_refusal_kinds_come_from_the_refusal_not_its_words(reg, rs):
    ev = one_off(reg, rs)
    past = reg.now_local() - timedelta(days=2)
    assert cq.refusal_kind(rc.reschedule_refusal(reg, rs, ev, past, None)) == "past"
    assert cq.refusal_kind(str(cq.move_past_sentence(reg, past, ev.start))) == "other", "a plain string is matched only by constant"
    other = rc.open_run(reg, rs, HYJAL, ev.start + timedelta(days=1), by="t")
    assert cq.refusal_kind(rc.reschedule_refusal(reg, rs, ev, other.start, None)) == "clash"
    assert (cq.refusal_kind(rc.LINEUP_LOCKED), cq.refusal_kind(rc.MOVE_CLOSED)) == ("locked", "closed")


def test_seen_on_reschedule_lineup_and_undo(reg, rs, web_bot):
    ms = people(reg, 3)
    ev = one_off(reg, rs)
    join(reg, rs, ev, ms[:1])
    new = (ev.start + timedelta(days=1)).isoformat()
    c, h = web_bot.c, H()
    a = c.post(f"/api/run/{ev.key}/reschedule", json={"start": new, "preview": True}, headers=h).json()["box"]
    join(reg, rs, ev, ms[1:2])
    r = c.post(f"/api/run/{ev.key}/reschedule", json={"start": new, "seen": a["fp"]}, headers=h)
    assert r.status_code == 409 and r.json()["box"]["fp"] != a["fp"] and ev.start.isoformat() != new
    want = [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1}]
    a = c.post(f"/api/run/{ev.key}/lineup", json={"lineup": want, "preview": True}, headers=h).json()["box"]
    join(reg, rs, ev, ms[2:])
    r = c.post(f"/api/run/{ev.key}/lineup", json={"lineup": want, "seen": a["fp"]}, headers=h)
    assert r.status_code == 409 and rc.run_lineup(reg, rs, ev)[1]["rosters"] == 2, "nothing written"
    r = c.post(f"/api/run/{ev.key}/lineup", json={"lineup": want}, headers=h)
    assert r.status_code == 200 and rc.run_lineup(reg, rs, ev)[1]["rosters"] == 1, "no seen: as before"
    r = c.post(f"/api/run/{ev.key}/reschedule", json={"start": new}, headers=h)
    assert r.status_code == 200 and rc.first_part(rs, ev).start.isoformat() == new, "no seen: as before"
    # undo: the board's fingerprint
    a = c.post(f"/api/run/{ev.key}/layout", json={"groups": [], "preview": True, "moves": 2}, headers=h).json()["box"]
    assert a["sentences"][0] == "Your 2 moves on this board are dropped."
    ev.layout = [[ms[0].display_name]]
    rs.save(ev, "a move meanwhile")
    r = c.post(f"/api/run/{ev.key}/layout", json={"groups": [], "seen": a["fp"], "moves": 2}, headers=h)
    assert r.status_code == 409 and ev.layout == [[ms[0].display_name]]
    assert c.post(f"/api/run/{ev.key}/layout", json={"groups": []}, headers=h).status_code == 200, "no seen: as before"


def test_seen_on_open_the_sheet_now(reg, rs, web_bot):
    sid = weekly(reg, RID, "Tue 18:30")
    t = next_tuesday(reg, sid)
    c, h = web_bot.c, H()
    p = c.post(f"/api/raid/{RID}/open/preview", json={"start": t.isoformat(), "schedule": sid}, headers=h).json()
    assert p["box"]["verb"] == "Open the sheet", p
    reg.set_schedule(sid, {"lock_hours_before": 6}, "t")  # its lock time changed while the box was open
    body = {"start": t.isoformat(), "schedule": sid, "occurrence": p["occurrence"]}
    r = c.post(f"/api/raid/{RID}/open", json={**body, "seen": p["box"]["fp"]}, headers=h)
    assert r.status_code == 409 and r.json()["box"]["fp"] != p["box"]["fp"] and not rs.events
    r = c.post(f"/api/raid/{RID}/open", json=body, headers=h)
    assert r.status_code == 200 and rs.events, "no seen: as before"
