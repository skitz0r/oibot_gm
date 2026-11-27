"""Phase 4, Task 1: the confirmation box (design §7b, D18) — ONE shape every press that asks first prints: a title, at
most three sentences under the Confirm rule, the verb (None = a refusal, only Back), red only when it cannot be undone,
the "label: old → new" lines and a fingerprint the action route re-checks (`seen` → 409 with the fresh box)."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest
from conftest import OWNER, join, lock_with_board, weekly
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
    assert cq.undo_box(4)["sentences"] == ["Your 4 moves on this board are dropped.", "Those people go back to Not placed."]
    assert cq.undo_box(1)["sentences"] == ["Your one move on this board is dropped.", "That person goes back to Not placed."]
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
    assert (rc.confirmation_words(True), rc.confirmation_words(False)) == ("Yes", "No")
    assert cq.count_words(3) == "three" and cq.count_words(11) == "11"


# ---------------------------------------------------------------- the event boxes

def test_lock_box_names_the_lineup_and_says_one_off_rules(reg, rs):
    ev = one_off(reg, rs)
    join(reg, rs, ev, people(reg, 4))  # people to place: the ticked box states the fill rule (nobody joined: what ticking does)
    b = cq.lock_box(reg, ev, fill=True)
    assert b["title"] == f"Lock {reg.local12(ev.starts_at)}?" and b["verb"] == "Lock" and not b["danger"]
    assert b["sentences"] == ["The lock builds the roster for Hyjal Summit and both Barrow Deeps rosters from those who joined.",
                              "Nobody is asked to confirm.", "You can still move people after the lock."]
    assert cq.lock_box(reg, rc.event_parts(rs, ev)[1], fill=True)["title"] == b["title"], "a later raid's box names the event"


def test_lock_box_on_a_scheduled_event_says_who_confirms(reg, rs):
    sid = tuesday(reg)
    ev = rc.open_run(reg, rs, HYJAL, next_tuesday(reg, sid), schedule=sid)
    # nobody joined: ticking places nobody, so the ticked box says what it then does (fix round 1)
    assert cq.lock_box(reg, ev, fill=True)["sentences"][1:] == ["Nobody has joined yet, so nobody is asked to confirm.", cq.LOCK_ASKS]
    join(reg, rs, ev, people(reg, 20))
    s = cq.lock_box(reg, ev, fill=True)["sentences"]
    assert s[0] == "The lock builds the roster for Hyjal Summit and both Barrow Deeps rosters from those who joined."
    assert s[1] == "Everyone rostered gets the Confirm or Can't make it DM." and s[2].startswith("They have until ")


def test_lock_box_says_what_the_board_becomes(reg, rs):
    """The board is only what officers placed; the lock also places everyone still under Not placed (the last rehearsal):
    an empty board builds the roster from those who joined, a part-filled one keeps its places and fills the rest from
    Not placed, a full one (every raid's) IS the roster. With the run store every open raid of the event is read."""
    sid = tuesday(reg)
    ev = rc.open_run(reg, rs, HYJAL, next_tuesday(reg, sid), schedule=sid)
    join(reg, rs, ev, people(reg, 20))
    parts = [p for p in rc.event_parts(rs, ev) if p.state == "open"]
    assert len(parts) == 2
    empty = cq.lock_box(reg, ev, rs, fill=True)["sentences"][0]
    assert empty == "The lock builds the roster for Hyjal Summit and both Barrow Deeps rosters from those who joined."
    names = [x.display_name for x in ev.by_status("in")]
    ev.layout = [names[:2]]
    part = cq.lock_box(reg, ev, rs, fill=True)["sentences"]
    assert part[0] == "Everyone on the board keeps their place, and the lock fills the open slots from Not placed."
    assert cq.consequence("lock", reg, ev, rs=rs)[0] == part[0], "the Discord screen and the site say the same"
    for p in parts:  # every raid's board holds everyone who joined: nobody is left under Not placed
        here = [x.display_name for x in p.by_status("in")]
        p.layout = [here[i:i + 5] for i in range(0, len(here), 5)]
    full = cq.lock_box(reg, ev, rs, fill=True)["sentences"]
    assert full[0] == "The board becomes the roster for Hyjal Summit and both Barrow Deeps rosters."
    parts[1].layout = None  # one raid's board emptied again: the event is part-filled
    assert cq.lock_box(reg, ev, rs, fill=True)["sentences"][0] == part[0]
    for b in (empty, part[0], full[0]):
        assert len(b.split()) <= 20 and b.endswith(".")


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
    assert b["sentences"] == ["The sheet opens now, three days early, and stays up until the event.", "It posts in the signup channel.",
                              f"It locks {reg.local12(lock_at)}, and the schedule won't open that night again."]
    assert cq.open_now_box(reg, start, reg.now_local() + timedelta(hours=5), lock_at)["sentences"][0] == \
        "The sheet opens now, five hours early, and stays up until the event."
    far = start_in(reg, 42)  # no horizon (the owner's tweaks): six weeks out says the same, in days
    b = cq.open_now_box(reg, far, far - timedelta(days=5), far - timedelta(hours=24))
    # the sheet's opening is 6:30 PM 37 days from today: run before 6:30 AM that is more than 37 and a half days away,
    # and the sentence rounds to the nearer day (38) — the count depends on the clock when the suite runs
    assert re.fullmatch(r"The sheet opens now, 3[78] days early, and stays up until the event\.", b["sentences"][0]), b["sentences"][0]
    assert cq.open_now_box(reg, start, reg.now_local(), lock_at)["sentences"][2] == f"It still locks {reg.local12(lock_at)}."


# ---------------------------------------------------------------- a stale box (Review Focus 5)

def test_a_stale_box_is_refused_with_the_fresh_one(reg, rs, web_bot, monkeypatch):
    from test_lineups import seat_first

    monkeypatch.setattr(rc, "propose", seat_first())
    sid = weekly(reg, RID, "Tue 18:30")
    ev = rc.open_run(reg, rs, RID, next_tuesday(reg, sid), schedule=sid)
    ms = people(reg, 6)  # nobody on the board: the lock that fills (issue 14: Auto-fill empty roster slots ticked)
    join(reg, rs, ev, ms[:4])
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"preview": True, "fill": True}, headers=H())
    assert r.status_code == 200, r.text
    a = r.json()["box"]
    join(reg, rs, ev, ms[4:5])
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"seen": a["fp"], "fill": True}, headers=H())
    assert r.status_code == 409 and ev.state == "open", "nothing is locked on a box that changed"
    fresh = r.json()["box"]
    assert r.json()["error"] == CONFIRM_WORDS["changed"] and fresh["fp"] != a["fp"] and fresh == cq.lock_box(reg, ev, rs, fill=True)
    assert fresh["sentences"] == a["sentences"], "no count: the fingerprint alone covers the new answer"
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"seen": fresh["fp"], "fill": True}, headers=H())
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
    assert b["sentences"] == ["Barrow Deeps goes from two rosters to one.", "Moves made on the removed roster are dropped.", "All 6 answers stand."]
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
    assert said == ["Onyxia's Lair is taken out with its board place.", "Moves made on the removed roster are dropped.", "All 6 answers stand."], \
        "the losses (what changes) first, who is told last"


def test_an_empty_roster_that_goes_leaves_the_board(reg, rs):
    """The board saves PADDED layouts: an empty second roster survives a save. Going 2 → 1 cuts the layout to the roster
    that stays anyway (else the board draws it and one drag into it makes the lock build two), with no loss sentence."""
    from test_raid_night import NightBot

    ms = people(reg, 3)
    ev = one_off(reg, rs)
    join(reg, rs, ev, ms)
    bd = rc.event_parts(rs, ev)[1]
    k = rc.groups_per_roster(reg, rc.run_size(reg, bd))
    bd.layout = [[ms[0].display_name]] + [[] for _ in range(2 * k - 1)]
    rs.save(bd, "a padded board")
    want = [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1}]
    assert not any("Moves made" in x for x in cq.lineup_consequence(reg, rs, ev, want))
    asyncio.run(NightBot(reg, rs).edit_lineup(reg, rs, ev, want, by="Jon", by_id=None))
    bd = rs.events[bd.key]
    assert len(bd.layout) == k and bd.layout[0] == [ms[0].display_name]
    assert len(rc.board_layout(reg, bd)) == k and not any("board place" in x for x in bd.log)


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
    # undo: the server counts the viewer's own moves (Task 3: ev.placed_by); the fp covers the board and those moves
    for i, m in enumerate(ms[:2]):
        asyncio.run(web_bot.bot.board_move(reg, rs, ev, m.display_name, (0, i), "Jon", by_id=OWNER))
    a = c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "preview": True, "moves": 7}, headers=h).json()["box"]
    assert a["sentences"][0] == "Your 2 moves on this board are dropped.", "the count is the server's, never the body's"
    moved = [ms[0].display_name, ms[1].display_name, ms[2].display_name]
    ev.layout = [list(moved)]
    rs.save(ev, "a move meanwhile")
    r = c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "seen": a["fp"]}, headers=h)
    assert r.status_code == 409 and ev.layout == [moved]
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


@pytest.mark.parametrize("gap", ["missing", "unset"])
def test_open_refuses_when_its_times_cant_be_read(reg, rs, web_bot, monkeypatch, gap):
    """Open the sheet now's box needs the schedule's opening and lock: when either can't be read the box is never made
    with a made-up number (the sheet would 'lock at the start'): the preview has no box and a `seen` open is refused
    with one sentence and only Back, nothing opened."""
    from oibot_gm.registry import Registry

    sid = weekly(reg, RID, "Tue 18:30")
    t = next_tuesday(reg, sid)
    c, h = web_bot.c, H()
    p = c.post(f"/api/raid/{RID}/open/preview", json={"start": t.isoformat(), "schedule": sid}, headers=h).json()
    real, real_preview, armed = Registry.schedule_def, rc.open_preview, []

    def broken(self, instance, s=None):  # the schedule read right after open_preview (the box's) lacks a time
        d = dict(real(self, instance, s))
        if armed:
            armed.clear()
            if gap == "missing":
                d.pop("lock_hours_before")
            else:
                d["signup_lead_hours"] = None
        return d

    def preview(*a, **k):
        out = real_preview(*a, **k)
        armed.append(1)
        return out

    monkeypatch.setattr(Registry, "schedule_def", broken)
    monkeypatch.setattr(rc, "open_preview", preview)
    assert c.post(f"/api/raid/{RID}/open/preview", json={"start": t.isoformat(), "schedule": sid}, headers=h).json().get("box") is None
    r = c.post(f"/api/raid/{RID}/open", json={"start": t.isoformat(), "schedule": sid, "occurrence": p["occurrence"], "seen": p["box"]["fp"]}, headers=h)
    assert r.status_code == 400 and not rs.events, r.json()
    box = r.json()["box"]
    assert box["verb"] is None and box["sentences"] == [r.json()["error"]] and "nothing was opened" in r.json()["error"]


def test_the_lock_box_never_counts_a_joiner_kept_off(reg, rs):
    """The count is the board's, said only when the board is the whole roster (consequences._LockFacts.full): seven
    joined and all placed is "The 7 rostered"; one kept off and the others placed is "The 6 rostered"; with everyone
    who joined kept off the box is a refusal (the lock would roster nobody)."""
    sid = tuesday(reg)
    ev = rc.open_run(reg, rs, HYJAL, next_tuesday(reg, sid), schedule=sid)
    ms = people(reg, 7)
    join(reg, rs, ev, ms)
    assert cq.lock_box(reg, ev, fill=True)["sentences"][1] == "Everyone rostered gets the Confirm or Can't make it DM.", "nobody placed: no count"
    ev.layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:]]]
    assert cq.lock_box(reg, ev, fill=True)["sentences"][1].startswith("The 7 rostered get")
    ev.layout = [[m.display_name for m in ms[1:6]], [ms[6].display_name]]
    ev.pins[str(ms[0].discord_id)] = "out"
    assert cq.lock_box(reg, ev, fill=True)["sentences"][1].startswith("The 6 rostered get")
    ev.layout = None
    for m in ms:
        ev.pins[str(m.discord_id)] = "out"
    b = cq.lock_box(reg, ev, fill=True)
    assert b["verb"] is None and b["sentences"] == [rc.SOLVER_WORDS["all_off"]]


# ---------------------------------------------------------------- the final review: Remind and a locked board's moves

def test_remind_asks_with_the_servers_box_and_an_answer_meanwhile_is_a_fresh_box(reg, rs, web_bot):
    from test_raid_night import locked_ten

    c = web_bot.c
    ev, seated = locked_ten(reg, rs, asks=True)
    url = f"/api/run/{ev.key}/remind"
    b = c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert b == cq.remind_box(reg, ev, rs) and b["verb"] == "Send reminder" and not b["danger"]
    assert b["sentences"] == cq.consequence("remind", reg, ev) and b["title"] == f"Send the reminder for {cq.event_title(reg, ev)}?"
    ask = next(a for a in reg.members[seated[0].discord_id].placement_asks if a["roster"] == ev.team)
    ask["answer"] = "yes"  # someone answered while the box was open
    r = c.post(url, json={"seen": b["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["error"] == CONFIRM_WORDS["changed"] and not ev.reminded_at
    r = c.post(url, json={"seen": r.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200 and ev.reminded_at, r.text
    again = c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert again["verb"] is None and "once an hour" in again["sentences"][0], "within the hour: the refusal"


def test_a_locked_boards_moves_ask_with_the_servers_box(reg, rs, web_bot):
    """Move to a group, take off the roster, swap, substitute and put on the roster on a LOCKED event: preview →
    {box} with consequence()'s sentences for that move (rc.board_move_kind reads which one it is on the board as it
    stands); a full group is the refusal; `seen` → 409 with the fresh box when the board changed while it was open."""
    from conftest import open_test_run

    c = web_bot.c
    ev = open_test_run(reg, rs)
    pool = reg.test_members()[:10]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool[:8]])  # groups of 5 and 3: room in group 2
    url = f"/api/run/{ev.key}/move"
    lay = rc.board_layout(reg, ev)
    a, b0, bench = lay[0][0], lay[1][0], pool[9].display_name
    cases = {"move": (a, {"r": 0, "g": 1, "i": 99}), "swap": (a, {"r": 0, "g": 1, "i": 0}), "release": (a, None),
             "roster": (bench, {"r": 0, "g": 1, "i": 99}), "substitute": (bench, {"r": 0, "g": 1, "i": 0})}
    for kind, (name, to) in cases.items():
        box = c.post(url, json={"member": name, "to": to, "preview": True, "state": "locked"}, headers=H()).json()["box"]
        assert box["verb"] == cq.BOARD_MOVE_VERBS[kind], (kind, box)
        assert box["title"] == cq.BOARD_MOVE_TITLES[kind].format(name=name, other=b0), (kind, box)
        kind_, other, where = rc.board_move_kind(reg, ev, name, (to["g"], to["i"]) if to else None)
        want = (cq.consequence("release", reg, ev, name=name) if kind == "release"
                else cq.consequence(kind, reg, ev, name=name, other=other, to=where))
        assert kind_ == kind and box["sentences"] == want, (kind, box)
    full = c.post(url, json={"member": bench, "to": {"r": 0, "g": 0, "i": 99}, "preview": True, "state": "locked"}, headers=H()).json()["box"]
    assert full["verb"] is None and full["sentences"] == ["Group 1 is full."], full
    assert rc.board_layout(reg, ev) == lay, "a preview writes nothing"
    b = c.post(url, json={"member": a, "to": {"r": 0, "g": 1, "i": 0}, "preview": True, "state": "locked"}, headers=H()).json()["box"]
    asyncio.run(web_bot.bot.board_move(reg, rs, ev, b0, None, "Other officer", by_id=OWNER))  # the board changed while it was open
    r = c.post(url, json={"member": a, "to": {"r": 0, "g": 1, "i": 0}, "seen": b["fp"], "state": "locked"}, headers=H())
    assert r.status_code == 409 and r.json()["box"]["fp"] != b["fp"] and r.json()["error"] == CONFIRM_WORDS["changed"]
    fresh = r.json()["box"]
    r = c.post(url, json={"member": a, "to": {"r": 0, "g": 1, "i": 0}, "seen": fresh["fp"], "state": "locked"}, headers=H())
    assert r.status_code == 200 and a in rc.board_layout(reg, ev)[1], r.text
