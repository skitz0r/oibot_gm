"""Phase 4, Task 2: the Events page's read model (event_view.py, web/api_events.py) — the list (Open · Coming up ·
History), one event in sections, a coming-up event, and the page words on /api/meta. Every fact comes from the existing
readers; a run key travels only as an id, never in a word."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from conftest import join, lock_with_board, open_test_run, weekly
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import consequences as cq
from oibot_gm import event_view
from oibot_gm import raidcycle as rc
from oibot_gm import registry
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS

HYJAL, RID = "hyjal_summit_forever", "barrow_deeps"
EVENING = [{"raid": RID, "rosters": 2}]
REAL = 472_000_000_000_000_000
CLASSES = [("Warrior", "Protection"), ("Priest", "Holy"), ("Priest", "Holy"), ("Mage", "Fire"), ("Mage", "Fire"), ("Rogue", "Combat")]


@pytest.fixture(autouse=True)
def _no_board_solves(monkeypatch):
    """The board's lock placement is Task 3's (tests/test_event_board.py): here no background solve starts, so an
    open event with joiners reads "working" with the officers' layout drawn."""
    from oibot_gm import event_board

    monkeypatch.setattr(event_board.BoardCache, "start", lambda self, *a, **k: None)


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:  # the profile's raids first open 9 Dec: every event here is after it
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


@pytest.fixture
def officer_client(web_bot):
    class C:
        def get(self, path):
            return web_bot.c.get(path, headers=H())
    return C()


def people(reg, n: int = 24):
    out = []
    for i in range(n):
        cls, spec = CLASSES[i % len(CLASSES)]
        reg.add_character(REAL + i, f"Member{chr(97 + i)}", f"Char{chr(97 + i)}", cls, spec, None, True)
        out.append(reg.members[REAL + i])
    return out


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def tuesday(reg) -> str:
    sid = weekly(reg, HYJAL, "Tue 18:30", sid="tue")
    reg.set_schedule(sid, {"name": "Tuesday raid night", "lineup": [{"raid": HYJAL, "rosters": 1}, *EVENING]}, "t")
    return sid


def words_of(obj) -> list[str]:
    """Every string a person could read in a JSON answer (ids and machine fields left out)."""
    skip = {"key", "schedule", "occurrence", "start_iso", "href", "fp", "uid", "raid", "id", "sheet", "sheet_url", "timing", "lineup", "open", "part", "revs"}
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                out += words_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out += words_of(v)
    elif isinstance(obj, str):
        out.append(obj)
    return out


# ---------------------------------------------------------------- the list

def test_the_list_has_three_groups_and_no_test_runs(officer_client, reg, rs):
    one_off = rc.open_run(reg, rs, HYJAL, start_in(reg, 2), by="t", lineup=EVENING)
    locked = rc.open_run(reg, rs, RID, start_in(reg, 4), by="t")
    ms = people(reg, 10)
    join(reg, rs, locked, ms)
    lock_with_board(reg, rs, locked, [m.display_name for m in ms])
    sid = tuesday(reg)
    done = rc.open_run(reg, rs, RID, start_in(reg, 1), by="t")
    rc.end_run(reg, rs, done, "t")
    test = open_test_run(reg, rs)
    r = officer_client.get("/api/event-list")
    assert r.status_code == 200, r.text
    j = r.json()
    assert [x["key"] for x in j["open"]] == [one_off.key, locked.key], "live events, soonest first; later parts are never rows"
    assert j["open"][0] == {"key": one_off.key, "title": reg.local12(one_off.start), "subtitle": "Hyjal Summit, then Barrow Deeps",
                            "state": "open", "chip": rc.RUN_STATE_WORDS["open"]}
    assert j["open"][1]["chip"] == rc.RUN_STATE_WORDS["locked"]
    coming = j["coming"]
    assert len(coming) == 3 and {x["schedule"] for x in coming} == {sid}
    starts = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][:3]
    assert [x["occurrence"] for x in coming] == [rc.occurrence_stamp(reg, t) for t in starts]
    assert [x["title"] for x in coming] == [reg.local12(t) for t in starts] and {x["subtitle"] for x in coming} == {"Tuesday raid night"}
    assert [x["key"] for x in j["history"]] == [done.key] and j["history"][0]["subtitle"] == "Barrow Deeps · finished"
    assert j["history_more"] is False
    everything = [x["key"] for g in ("open", "history") for x in j[g]]
    assert test.key not in everything and all(p.key not in everything for p in rc.event_parts(rs, one_off)[1:])
    for w in words_of(j):
        assert all(e.key not in w for e in rs.events.values()), w


def test_a_schedules_opened_night_is_not_coming_up_and_names_its_schedule(officer_client, reg, rs):
    sid = tuesday(reg)
    first, second = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][:2]
    ev = rc.open_run(reg, rs, HYJAL, first, by="t", schedule=sid)
    j = officer_client.get("/api/event-list").json()
    assert j["open"][0]["key"] == ev.key and j["open"][0]["subtitle"] == "Tuesday raid night"
    assert rc.occurrence_stamp(reg, first) not in [x["occurrence"] for x in j["coming"]]
    assert j["coming"][0]["occurrence"] == rc.occurrence_stamp(reg, second) and len(j["coming"]) == 3


def test_a_night_a_live_run_stands_for_is_not_coming_up(reg, rs):
    sid = tuesday(reg)
    first, second = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][:2]
    ev = rc.open_run(reg, rs, HYJAL, first, by="t", schedule=sid)
    reg.config.team(ev.team)["stands_for"] = rc.occurrence_stamp(reg, second)  # what plan_stands_for writes on a schedule edit
    coming = event_view.event_list(reg, rs, reg.now_local())["coming"]
    assert not {rc.occurrence_stamp(reg, first), rc.occurrence_stamp(reg, second)} & {x["occurrence"] for x in coming}


def test_history_pages_by_twenty_newest_first(officer_client, reg, rs):
    evs = []
    for i in range(21):
        ev = rc.open_run(reg, rs, RID, start_in(reg, 1 + i), by="t")
        if i % 2:
            rc.end_run(reg, rs, ev, "t")
        else:  # cancelled as RaidMixin.cancel_run leaves a run file
            ev.state, ev.closed_at = "cancelled", rc.now()
            rs.save(ev, "t: cancelled")
        evs.append(ev)
    j = officer_client.get("/api/event-list").json()
    assert [x["key"] for x in j["history"]] == [e.key for e in reversed(evs)][:20] and j["history_more"] is True
    j = officer_client.get("/api/event-list?history_offset=20").json()
    assert [x["key"] for x in j["history"]] == [evs[0].key] and j["history_more"] is False


def test_the_list_never_solves_and_finds_nights_with_one_index(reg, rs, monkeypatch):
    tuesday(reg)
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 2), by="t", lineup=EVENING)
    join(reg, rs, ev, people(reg, 18))

    def boom(*a, **k):
        raise AssertionError("the list must not call this")

    for name in ("_solve_run", "propose", "role_plan", "event_of_occurrence"):
        monkeypatch.setattr(rc, name, boom)
    out = event_view.event_list(reg, rs, reg.now_local())
    assert len(out["open"]) == 1 and len(out["coming"]) == 3


def test_the_night_index_answers_every_tier(reg, rs):
    """rc.occurrence_index (what event_of_occurrence answers through): a night by its stamp, by the night a run stands
    for, a finished night, a run moved onto a night, and none."""
    sid = tuesday(reg)
    nights = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][:5]
    ev = rc.open_run(reg, rs, HYJAL, nights[0], by="t", schedule=sid)
    reg.config.team(ev.team)["stands_for"] = rc.occurrence_stamp(reg, nights[1])
    done = rc.open_run(reg, rs, HYJAL, nights[2], by="t", schedule=sid)
    rc.end_run(reg, rs, done, "t")
    moved = rc.open_run(reg, rs, HYJAL, nights[4] - timedelta(days=1), by="t", schedule=sid)
    moved.starts_at = nights[4].isoformat()  # rescheduled onto the next night (its stamp still says its own)
    find = rc.occurrence_index(reg, rs)
    got = [find(sid, rc.occurrence_stamp(reg, t)) for t in nights]
    assert got == [ev, ev, done, None, moved]
    assert [rc.event_of_occurrence(rs, sid, rc.occurrence_stamp(reg, t), reg=reg) for t in nights] == got


# ---------------------------------------------------------------- one event

def test_an_open_event_reads_by_its_sections(officer_client, reg, rs):
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    join(reg, rs, ev, people(reg)[:18])
    j = officer_client.get(f"/api/event/{rc.event_parts(rs, ev)[1].key}").json()
    assert j["key"] == ev.key and [f["label"] for f in j["details"]] == ["Lineup", "Comes from", "Priority", "Confirmation", "Sheet opened", "Locks", "Signup sheet"]
    facts = {f["label"]: f["value"] for f in j["details"]}
    assert facts["Comes from"] == "One-off, no schedule" and facts["Lineup"] == "Hyjal Summit, then Barrow Deeps × 2"
    assert (facts["Priority"], facts["Confirmation"]) == ("First to join", "Not asked")
    assert facts["Signup sheet"] == PAGE_WORDS["sheet_waits"], "no signup channel here: the fact says what it waits for"
    assert j["signups"]["joined"] == 18 and [p["label"] for p in j["parts"]][0].endswith("· Hyjal Summit")
    assert j["boxes"]["lock"]["verb"] == "Lock" and j["actions"]["edit_lineup"] is True
    assert j["boxes"]["lock"] == cq.lock_box(reg, ev, rs) and j["boxes"]["cancel"] == cq.cancel_box(reg, ev, rs)
    assert (j["kind"], j["schedule"], j["state"], j["chip"], j["title"]) == ("one_off", None, "open", rc.RUN_STATE_WORDS["open"], reg.local12(ev.start))
    assert j["actions"] == {"reschedule": True, "edit_lineup": True, "lock": True, "cancel": True, "open_now": False}
    assert [p["key"] for p in j["parts"]] == [p.key for p in rc.event_parts(rs, ev)]
    hy, bd = j["parts"]
    assert hy["label"] == f"6:30 PM · Hyjal Summit" and bd["raid_name"] == "Barrow Deeps"
    assert hy["rosters_words"] == f"1 of {rc.run_size(reg, ev)}" and bd["rosters_words"].startswith("2 of ")
    assert hy["placed_words"] == "0 of 18 joined" and hy["board_state"] == "working" and hy["sheet"]["key"] == ev.key, \
        "18 joined: the lock's placement is being worked out, the officers' (empty) layout drawn meanwhile"
    assert hy["short"] == rc.needs(reg, ev)["roles"]
    assert j["lineup"] == rc.run_lineup(reg, rs, ev) and j["start_iso"] == ev.start.isoformat()
    assert set(j["timing"]) == set(rc.EVENT_TIMING) and rc.timing_changes(reg, ev, j["timing"]) == {}, "sent back as it is, it changes nothing"
    assert len(j["signups"]["rows"]["in"]) == 18 and {"uid", "display_name", "character", "cls", "spec"} <= set(j["signups"]["rows"]["in"][0])
    assert {p["uid"] for p in j["people"]} == {str(REAL + i) for i in range(18, 24)}, "Answer for someone: the members with no answer"
    assert j["activity"] and all(set(a) == {"when", "line"} for a in j["activity"])
    for w in words_of({k: v for k, v in j.items() if k not in ("parts", "boxes")}):
        assert all(e.key not in w for e in rs.events.values()), w


def test_a_scheduled_event_comes_from_its_schedule(officer_client, reg, rs):
    sid = tuesday(reg)
    night = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    ev = rc.open_run(reg, rs, HYJAL, night, by="t", schedule=sid)
    j = officer_client.get(f"/api/event/{ev.key}").json()
    came = next(f for f in j["details"] if f["label"] == "Comes from")
    assert came == {"label": "Comes from", "value": "Tuesday raid night", "href": f"/schedules/{sid}"}
    assert j["kind"] == "scheduled" and j["schedule"] == {"id": sid, "name": "Tuesday raid night"}
    assert {f["label"]: f["value"] for f in j["details"]}["Confirmation"] == "Asked at the lock"


def test_a_locked_event_adds_its_confirmations_and_open_seats(officer_client, reg, rs):
    sid = tuesday(reg)
    night = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    ev = rc.open_run(reg, rs, HYJAL, night, by="t", schedule=sid)
    ms = people(reg, 10)
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    later = rc.event_parts(rs, ev)[1]
    lock_with_board(reg, rs, later, [])  # the evening's second raid locks too (lock_run locks every raid)
    j = officer_client.get(f"/api/event/{ev.key}").json()
    facts = {f["label"]: f["value"] for f in j["details"]}
    # the Tuesday night plays Hyjal Summit then Barrow Deeps: EVERY raid's open seats, each named (fix round 1)
    assert facts["Open seats · Hyjal Summit"] == rc.fill_state_text(reg, ev)
    assert facts["Open seats · Barrow Deeps"] == rc.fill_state_text(reg, later)
    assert j["state"] == "locked" and j["parts"][0]["board_state"] == "locked" and j["parts"][0]["placed_words"] is None and j["parts"][0]["rostered_words"] == "10 of 20 seats"
    assert j["actions"]["lock"] is False and j["actions"]["edit_lineup"] is False and j["boxes"]["lock"] is None
    labels = [f["label"] for f in j["details"]]
    assert facts["Confirmed"] == "10 waiting" and labels[-3:] == ["Open seats · Hyjal Summit", "Open seats · Barrow Deeps", "Signup sheet"]
    assert "Confirm by" in facts
    assert "Reminder" not in facts, "after the lock the reminder is past; Reminded says when one was sent"


def test_an_events_raid_night_facts_come_from_its_runs(officer_client, reg, rs, monkeypatch):
    """Fix round 1 (the Rosters card's raid-night lines, kept): automatic asks paused and by whom, who is double-booked,
    who is away with the reason the sheet shows, the reminder and confirm-by times, the last reminder and who sent it,
    confirmations not yet sent, the Discord links beside the Signup sheet, the time zone once on the list, and a test
    run's marker — each in the server's words, read from the runs (ev_json), nothing composed on the page."""
    from oibot_gm.raid_views import run_times

    sid = tuesday(reg)
    night = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    ev = rc.open_run(reg, rs, HYJAL, night, by="t", schedule=sid)
    ms = people(reg, 12)
    join(reg, rs, ev, ms[:10])
    day = ev.start.astimezone(reg.tz).date().isoformat()
    reg.add_absence(ms[11].discord_id, day, day, "work trip", "t")
    j = officer_client.get(f"/api/event/{ev.key}").json()
    facts = {f["label"]: f["value"] for f in j["details"]}
    soft, _hard, confirm = run_times(reg, ev, rc.run_team(reg, ev))
    assert facts["Reminder"] == reg.local12(soft) and facts["Confirm by"] == reg.local12(confirm)
    assert facts["Away that day"] == f"{ms[11].display_name} (work trip)"
    other = rc.open_run(reg, rs, RID, night, by="t")  # another event that same night, one of ours joined there too
    join(reg, rs, other, [ms[0]])
    facts = {f["label"]: f["value"] for f in officer_client.get(f"/api/event/{ev.key}").json()["details"]}
    assert ms[0].display_name in facts["Also on another event"]
    assert j["test_chip"] is None
    ev.autofill_paused, ev.autofill_paused_by = True, "Jon"  # before the lock there is no Open seats sentence to say it
    rs.save(ev, "t")
    facts = {f["label"]: f["value"] for f in officer_client.get(f"/api/event/{ev.key}").json()["details"]}
    assert facts["Automatic asks · Hyjal Summit"] == PAGE_WORDS["paused_by"].format(who="Jon")
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:10]])
    ev.autofill_paused, ev.autofill_paused_by = True, "Jon"
    ev.reminded_at, ev.reminded_by = reg.now_local().isoformat(), "Jon"
    ev.cards, ev.cards_channel_id, ev.updates_thread_id = {"lock:0": 42}, 77, 88
    ev.confirmations_sent_at = None  # the Confirm / Can't fan-out not finished yet
    rs.save(ev, "t")
    j = officer_client.get(f"/api/event/{ev.key}").json()
    facts = {f["label"]: f for f in j["details"]}
    # once locked, the Open seats sentence already says who paused the automatic asks: said once, not in a second fact
    assert facts["Open seats · Hyjal Summit"]["value"].startswith("Automatic asks paused by Jon. ")
    assert "Automatic asks · Hyjal Summit" not in facts
    assert sum("paused by Jon" in str(f["value"]) for f in j["details"]) == 1
    assert facts["Reminded"]["value"].endswith(", by Jon") and not re.search(r"\d{4}-\d\d", facts["Reminded"]["value"]), "words, never ISO"
    assert facts["Confirmations"]["value"] == PAGE_WORDS["confirm_pending"]
    links = facts["In Discord"]
    assert [x["name"] for x in links["links"]] == ["Officer card", "Updates thread"] and all(x["href"].startswith("https://discord.com/") for x in links["links"])
    assert [f["label"] for f in j["details"]].index("In Discord") == [f["label"] for f in j["details"]].index("Signup sheet") + 1
    lst = officer_client.get("/api/event-list").json()
    assert lst["tz_words"].startswith("Times in ") and lst["tz_words"].endswith(".")


def test_a_test_runs_event_page_says_it_is_one(officer_client, reg, rs):
    ev = open_test_run(reg, rs, RID)
    j = officer_client.get(f"/api/event/{ev.key}").json()
    assert j["test_chip"] == PAGE_WORDS["test_run"]


def test_a_finished_event_reads_as_history(officer_client, reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg, 1), by="t")
    ms = people(reg, 10)
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    rc.end_run(reg, rs, ev, "t")
    j = officer_client.get(f"/api/event/{ev.key}").json()
    assert [f["label"] for f in j["details"]] == ["Lineup", "Comes from", "Rostered", "Ended"]
    facts = {f["label"]: f["value"] for f in j["details"]}
    assert (facts["Lineup"], facts["Rostered"], facts["Ended"]) == ("Barrow Deeps", "10", reg.local12(ev.closed_at))
    assert j["chip"] == "finished" and j["boxes"] == {"lock": None, "cancel": None, "reschedule": None}
    assert not any(v for k, v in j["actions"].items())


def mains_unanswered(reg, ev) -> set[str]:
    return {str(m.discord_id) for m in reg.members.values() if m.main and not m.test and str(m.discord_id) not in ev.signups}


def test_a_one_offs_no_answer_is_every_main_who_hasnt_answered(officer_client, reg, rs):
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    join(reg, rs, ev, people(reg)[:18])
    assert rc.not_answered(reg, ev) == [], "the nudge's gate expects nobody on a one-off"
    j = officer_client.get(f"/api/event/{ev.key}").json()
    assert {x["uid"] for x in j["signups"]["rows"]["none"]} == mains_unanswered(reg, ev) and j["signups"]["no_answer"] == 6
    assert [x["display_name"] for x in j["signups"]["rows"]["none"]] == sorted((x["display_name"] for x in j["signups"]["rows"]["none"]), key=str.lower)


def test_a_scheduled_runs_no_answer_is_what_it_was(officer_client, reg, rs):
    sid = tuesday(reg)
    night = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    ev = rc.open_run(reg, rs, HYJAL, night, by="t", schedule=sid)
    join(reg, rs, ev, people(reg)[:10])
    assert rc.pool_unanswered(reg, ev) == rc.not_answered(reg, ev) != []
    j = officer_client.get(f"/api/event/{ev.key}").json()
    assert [x["uid"] for x in j["signups"]["rows"]["none"]] == [str(m.discord_id) for m, _c in rc.not_answered(reg, ev)]


def test_an_event_with_nobody_joined(officer_client, reg, rs):
    people(reg, 6)
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    j = officer_client.get(f"/api/event/{ev.key}").json()
    s = j["signups"]
    assert (s["joined"], s["bench"], s["no_thanks"]) == (0, 0, 0) and s["rows"]["in"] == s["rows"]["sub"] == s["rows"]["out"] == []
    assert s["no_answer"] == len(s["rows"]["none"]) == len(mains_unanswered(reg, ev)) == len(j["people"])
    for p in j["parts"]:
        assert p["placed_words"] == "0 of 0 joined" and p["board_state"] == "ready" and p["sheet"]["board"]["rosters"]
        assert p["short"] == rc.needs(reg, rs.events[p["key"]])["roles"]
    assert [f["label"] for f in j["details"]][0] == "Lineup" and j["boxes"]["lock"]["verb"] == "Lock"
    assert isinstance(j["activity"], list) and j["actions"]["lock"] is True


def test_history_counts_a_person_once_and_carries_each_raids_start(officer_client, reg, rs):
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 1), by="t", lineup=[{"raid": RID, "rosters": 1}])
    ms = people(reg, 10)
    join(reg, rs, ev, ms)
    first, later = rc.event_parts(rs, ev)
    lock_with_board(reg, rs, first, [m.display_name for m in ms])
    lock_with_board(reg, rs, later, [m.display_name for m in ms])
    later.all_rosters[0].selected[0].signup_name = "Someone Else"  # the same person on another character there
    for p in (first, later):
        rc.end_run(reg, rs, p, "t")
    j = officer_client.get(f"/api/event/{ev.key}").json()
    assert {f["label"]: f["value"] for f in j["details"]}["Rostered"] == "10"
    assert j["lineup"] == [{"raid": HYJAL, "rosters": 1, "starts": 0}, {"raid": RID, "rosters": 1, "starts": later.starts}] and later.starts > 0


def test_an_old_finished_rehearsal_stays_out_of_history(officer_client, reg, rs):
    ev = open_test_run(reg, rs)
    rc.end_run(reg, rs, ev, "t")
    reg.config.rosters = [t for t in reg.config.rosters if t.get("key") != ev.team]  # its run dict is gone
    ev.test, ev.event, ev.part, ev.schedule = False, None, 0, "test"  # a file from before the flag and the stamps
    rs.save(ev, "t: an old rehearsal")
    assert not rc.is_test_run(reg, ev) and rc.rehearsal_run(reg, ev)
    assert ev.key not in [x["key"] for x in officer_client.get("/api/event-list").json()["history"]]


def test_an_unknown_event_is_a_sentence_without_the_key(officer_client):
    r = officer_client.get("/api/event/bd-zz9k2m")
    assert r.status_code == 404 and "bd-zz9k2m" not in r.text


# ---------------------------------------------------------------- a coming-up event

def test_a_coming_up_event_says_when_it_opens_by_hand(officer_client, reg, rs):
    sid = tuesday(reg)
    near = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    j = officer_client.get(f"/api/event/coming/{sid}/{rc.occurrence_stamp(reg, near)}").json()
    assert j["can_open"] is True and j["why"] is None and j["box"]["verb"] == "Open the sheet"
    # Hyjal first opens ten weeks out: past any fixed horizon, its first nights are still coming up — and too far to open
    reg.set_raid_overrides(HYJAL, {"first_open": (reg.now_local() + timedelta(days=70)).strftime("%Y-%m-%dT%H:%M")}, "t")
    coming = officer_client.get("/api/event-list").json()["coming"]
    assert len(coming) == 3 and {x["schedule"] for x in coming} == {sid}
    nights = rc.next_runs_of(reg, HYJAL, reg.schedule_entry(sid), 3, reg.now_local())
    assert [x["occurrence"] for x in coming] == [rc.occurrence_stamp(reg, t) for t in nights]
    far = nights[0]
    assert far > reg.now_local() + timedelta(days=56), "more than eight weeks out"
    r = officer_client.get(f"/api/event/coming/{sid}/{rc.occurrence_stamp(reg, far)}")
    assert r.status_code == 200, r.text
    j = r.json()
    assert [f["label"] for f in j["details"]] == ["Lineup", "Comes from", "Priority", "Confirmation", "Sheet opens", "Opening by hand", "Locks"]
    facts = {f["label"]: f["value"] for f in j["details"]}
    assert facts["Opening by hand"] == f"From {reg.local12(far - timedelta(days=rc.OPEN_HORIZON_DAYS))}"
    assert facts["Lineup"] == "Hyjal Summit, then Barrow Deeps × 2" and facts["Priority"] == "Regulars first, then rank"
    sd = reg.schedule_def(HYJAL, sid)
    assert facts["Sheet opens"] == reg.local12(far - timedelta(hours=float(sd["signup_lead_hours"])))
    assert facts["Locks"] == reg.local12(far - timedelta(hours=float(sd["lock_hours_before"])))
    assert j["can_open"] is False and j["why"] == rc.open_row_refusal(reg, rs, HYJAL, far, sid, far - timedelta(hours=float(sd["signup_lead_hours"])))
    assert j["box"] is None and (j["title"], j["chip"]) == (reg.local12(far), PAGE_WORDS["no_sheet_yet"])
    assert j["open"] == {"start_iso": far.isoformat(), "schedule": sid, "occurrence": rc.occurrence_stamp(reg, far), "raid": HYJAL}
    assert j["schedule"] == {"id": sid, "name": "Tuesday raid night"}
    for x in coming:  # every listed night opens its coming-up view
        assert officer_client.get(f"/api/event/coming/{sid}/{x['occurrence']}").status_code == 200


def test_a_coming_up_night_no_longer_on_the_schedule_is_404(officer_client, reg, rs):
    sid = tuesday(reg)
    near = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    occ = rc.occurrence_stamp(reg, near)
    reg.set_schedule(sid, {"slots": "Wed 18:30"}, "t")
    r = officer_client.get(f"/api/event/coming/{sid}/{occ}")
    assert r.status_code == 404 and sid not in r.json()["error"]
    reg.set_schedule(sid, {"slots": "Tue 18:30"}, "t")
    ev = rc.open_run(reg, rs, HYJAL, near, by="t", schedule=sid)
    r = officer_client.get(f"/api/event/coming/{sid}/{occ}")
    assert r.status_code == 404 and r.json()["key"] == ev.key, "opened since: the page goes to the event"


# ---------------------------------------------------------------- the Events page's actions (Task 8)

def test_a_coming_up_nights_box_opens_on_the_first_press(web_bot, reg, rs):
    """Open the sheet now posts to its first raid (`open.raid`) with the box's fp as `seen`: the box the coming-up view
    printed is the one the open route builds, so the first press opens (no "changed" box) and answers the new key."""
    c, h = web_bot.c, H()
    sid = tuesday(reg)
    near = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    j = c.get(f"/api/event/coming/{sid}/{rc.occurrence_stamp(reg, near)}", headers=h).json()
    o = j["open"]
    r = c.post(f"/api/raid/{o['raid']}/open", json={"start": o["start_iso"], "schedule": o["schedule"], "occurrence": o["occurrence"],
                                                     "seen": j["box"]["fp"]}, headers=h)
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    assert ev.occurrence == o["occurrence"] and rc.schedule_stamp(rc.run_schedule_id(reg, ev)) == sid


def test_reschedule_starts_from_the_pick_box_with_the_guilds_date(officer_client, reg, rs):
    """Reschedule opens on the server's own box before anything is picked (its title names the event, one sentence,
    only Back); the Day picker's lower bound is the GUILD's date and the Locks choices carry the event's own lock."""
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    j = officer_client.get(f"/api/event/{ev.key}").json()
    assert j["boxes"]["reschedule"] == cq.reschedule_pick_box(reg, rs, ev)
    b = j["boxes"]["reschedule"]
    assert b["title"] == f"Move {reg.local12(ev.start)}?" and b["sentences"] == [rc.MOVE_WHEN] and b["verb"] is None
    assert j["today"] == reg.now_local().date().isoformat()
    own = j["timing"]["lock_hours_before"]
    assert any(x["hours"] == own for x in j["locks"])
    assert {x["hours"] for x in j["locks"]} >= set(event_view.NEW_EVENT_LOCKS)
    assert all(x["words"].endswith(" before") for x in j["locks"])


def test_answer_for_someone_previews_the_servers_box(web_bot, reg, rs):
    """Answer for someone prints consequences.answer_box: `preview` answers the box and writes nothing; a press whose box
    changed meanwhile (`seen` stale) is refused with the fresh box (409); the box's own fp records the answer."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    m = people(reg, 2)[1]
    ch = m.main
    body = {"run": ev.key, "member": str(m.discord_id), "action": "join", "character": ch.label, "spec": ch.spec}
    r = c.post("/api/admin/action", json={**body, "preview": True}, headers=h)
    assert r.status_code == 200, r.text
    box = r.json()["box"]
    assert (box["title"], box["verb"]) == ("Answer for someone", "Record answer")
    assert box["sentences"][0].startswith(f"{m.display_name} is marked as joined") and str(m.discord_id) not in ev.signups
    r = c.post("/api/admin/action", json={**body, "seen": "stale"}, headers=h)
    assert r.status_code == 409 and r.json()["box"] == box and str(m.discord_id) not in ev.signups
    r = c.post("/api/admin/action", json={**body, "seen": box["fp"]}, headers=h)
    assert r.status_code == 200, r.text
    assert ev.signups[str(m.discord_id)].status == "in"


def test_reschedule_starts_from_the_guilds_current_clock(officer_client, reg, rs):
    """Fix round 1 (2a): the picker starts at the event's day and clock in the guild's CURRENT zone — a timezone changed
    with `keep` leaves the stored ISO in the old offset, so its digits are not the guild's clock any more."""
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    old = reg.config.timezone
    reg.config.timezone = "Asia/Tokyo" if old != "Asia/Tokyo" else "America/New_York"
    j = officer_client.get(f"/api/event/{ev.key}").json()
    local = ev.start.astimezone(reg.tz)
    assert (j["start_day"], j["start_time"]) == (local.strftime("%Y-%m-%d"), local.strftime("%H:%M"))
    assert j["start_iso"][11:16] != j["start_time"], "the stored offset's clock is not the guild's now"


def test_the_row_menus_answer_box_offers_only_specs_the_server_takes(web_bot, reg, rs):
    """Fix round 1 (1, 2c): each answered member's characters carry `plays` (rc.spec_role's specs), so the row ⋮'s box
    never offers a spec the write refuses; a spec the character doesn't play is a refusal box (400), never a 500; the
    action that no longer applies says so once."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    ms = people(reg, 3)
    join(reg, rs, ev, ms[:1])
    j = c.get(f"/api/event/{ev.key}", headers=h).json()
    rows = j["characters"][str(ms[0].discord_id)]
    assert rows[0]["label"] == ms[0].main.label and rows[0]["plays"][0] == ms[0].main.spec
    for sp in rows[0]["plays"]:
        rc.spec_role(reg, ms[0].main, sp)
    body = {"run": ev.key, "member": str(ms[1].discord_id), "action": "join", "character": ms[1].main.label, "spec": "Nonsense", "preview": True}
    r = c.post("/api/admin/action", json=body, headers=h)
    assert r.status_code == 400 and r.json()["box"]["verb"] is None and "doesn't play Nonsense" in r.json()["box"]["sentences"][0]
    r = c.post("/api/admin/action", json={**body, "spec": ms[1].main.spec, "action": "join", "member": str(ms[0].discord_id),
                                         "character": ms[0].main.label}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == f"That no longer applies to {ms[0].display_name} on {rc.run_words(reg, ev)}.", \
        "Join isn't offered to someone already joined: said once, no 'any more'"


def test_a_lock_box_left_open_on_an_ended_event_gets_its_refusal(web_bot, reg, rs):
    """Fix round 1 (2d): the event is cancelled while its Lock box is open — the box's refresh (`preview`) and the press
    answer the refusal box (400, only Back), never a bare 404 that leaves Lock pressable."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    box = c.post(f"/api/run/{ev.key}/lock", json={"preview": True}, headers=h).json()["box"]
    ev.state = "cancelled"  # another officer cancelled it meanwhile
    rs.save(ev, "cancelled (test)")
    for body in ({"preview": True}, {"seen": box["fp"]}):
        r = c.post(f"/api/run/{ev.key}/lock", json=body, headers=h)
        assert r.status_code == 400 and r.json()["box"]["verb"] is None, r.text


# ---------------------------------------------------------------- the page words

def test_meta_carries_the_page_words(officer_client):
    m = officer_client.get("/api/meta").json()
    assert m["page_words"]["one_off"] == "One-off, no schedule" and m["confirm_words"]["discard"] == "Discard edits"
    assert m["page_words"] == PAGE_WORDS and m["leave"] == cq.LEAVE_TEMPLATE
    # + the title of a refusal the server sent no box for (the Save box's fallback never prints the question title)
    assert m["confirm_words"] == {**CONFIRM_WORDS, "refused": cq.REFUSAL_TITLES["other"]}
    assert rc.confirmation_words(False) == PAGE_WORDS["not_asked"] and rc.confirmation_words(True) == PAGE_WORDS["asked_at_lock"]


# ---------------------------------------------------------------- New event (Task 7): the lineup builder's open

def wall(reg, t) -> str:
    return t.astimezone(reg.tz).strftime("%Y-%m-%d %H:%M")


def test_open_with_a_lineup_and_a_lock_choice(web_bot, reg, rs):
    """The New event page's preview answers "What this makes" as facts in the server's words, every step's start as a
    12-hour time, and no refusal; Open with the lock choice makes a one-off that locks 24 hours before."""
    c, h = web_bot.c, H()
    start = start_in(reg, 4)
    body = {"when": wall(reg, start), "schedule": "", "lineup": EVENING, "timing": {"lock_hours_before": 24}}
    r = c.post(f"/api/raid/{HYJAL}/open/preview", json=body, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert [f["label"] for f in p["facts"]] == ["One signup sheet", "Seats", "Priority", "Confirmation"]
    facts = {f["label"]: f["value"] for f in p["facts"]}
    assert facts["Seats"] == "20, then 10 + 10" and facts["One signup sheet"] == "Hyjal Summit, then Barrow Deeps × 2"
    assert facts["Priority"] == PAGE_WORDS["first_to_join"] and facts["Confirmation"] == PAGE_WORDS["not_asked"]
    assert [(s["name"], s["rosters"], s["time_words"], s["seats"]) for s in p["lineup"]] == [("Hyjal Summit", 1, "6:30 PM", 20), ("Barrow Deeps", 2, "9:30 PM", 10)]
    assert p["refusal_box"] is None and p["kind"] == "adhoc"
    r = c.post(f"/api/raid/{HYJAL}/open", json={**body, "start": p["start"], "occurrence": p["occurrence"]}, headers=h)
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert reg.config.team(key)["cutoff_hard_hours"] == 24
    assert [(e["raid"], e["rosters"]) for e in rc.run_lineup(reg, rs, rs.events[key])] == [(HYJAL, 1), (RID, 2)]
    # the same night again: already open — a refusal box, the run named by its time, never its key
    again = c.post(f"/api/raid/{HYJAL}/open/preview", json=body, headers=h).json()
    assert again["refusal_box"]["verb"] is None and again["refusal_box"]["title"] == cq.REFUSAL_TITLES["clash"]
    assert key not in str(again["refusal_box"]) and reg.local12(start) in again["refusal_box"]["sentences"][0]


def test_a_new_events_first_step_takes_its_rosters_and_a_custom_start(web_bot, reg, rs):
    c, h = web_bot.c, H()
    start = start_in(reg, 5)
    later = [{"raid": RID, "rosters": 1, "starts": 240}]
    body = {"when": wall(reg, start), "schedule": "", "rosters": 2, "lineup": later}
    p = c.post(f"/api/raid/{HYJAL}/open/preview", json=body, headers=h).json()
    facts = {f["label"]: f["value"] for f in p["facts"]}
    assert facts["Seats"] == "20 + 20, then 10" and facts["One signup sheet"] == "Hyjal Summit × 2, then Barrow Deeps"
    assert [s["time_words"] for s in p["lineup"]] == ["6:30 PM", "10:30 PM"]
    r = c.post(f"/api/raid/{HYJAL}/open", json={**body, "start": p["start"], "occurrence": p["occurrence"]}, headers=h)
    assert r.status_code == 200, r.text
    assert [(e["raid"], e["rosters"]) for e in rc.run_lineup(reg, rs, rs.events[r.json()["key"]])] == [(HYJAL, 2), (RID, 1)]


@pytest.mark.parametrize("bad", [
    [{"raid": "molten_core", "rosters": 1}],
    [{"raid": RID, "rosters": 1}, {"raid": RID, "rosters": 1}],
    [{"raid": RID, "rosters": 1, "starts": 240}, {"raid": "onyxias_lair", "rosters": 1, "starts": 120}],
    [{"raid": RID, "rosters": 1}],  # with a lock choice the New event page never offers
])
def test_a_refused_lineup_is_a_refusal_box(web_bot, reg, rs, bad):
    c, h = web_bot.c, H()
    body = {"when": wall(reg, start_in(reg, 4)), "schedule": "", "lineup": bad}
    if len(bad) == 1 and bad[0]["raid"] == RID:
        body["timing"] = {"lock_hours_before": 5}
    for path in ("open/preview", "open"):
        r = c.post(f"/api/raid/{HYJAL}/{path}", json=body, headers=h)
        assert r.status_code == 400, r.text
        box = r.json()["box"]
        assert box["verb"] is None and box["sentences"] == [r.json()["error"]]
    assert not rs.events


def test_a_scheduled_night_opens_as_its_schedule_whatever_the_builder_held(web_bot, reg, rs):
    """A picked time that is a schedule's night of the same first raid: the preview shows that schedule's lineup and
    Comes from, and Open opens the schedule's event — the builder's lineup, rosters and lock choice are not used."""
    c, h = web_bot.c, H()
    sid = tuesday(reg)
    night = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][0]
    body = {"when": wall(reg, night), "schedule": "", "rosters": 3, "lineup": [{"raid": "onyxias_lair", "rosters": 1}], "timing": {"lock_hours_before": 2}}
    p = c.post(f"/api/raid/{HYJAL}/open/preview", json=body, headers=h).json()
    assert p["kind"] == "scheduled" and p["schedule"] == sid and p["box"]["verb"] == "Open the sheet"
    facts = {f["label"]: f for f in p["facts"]}
    assert list(facts) == ["One signup sheet", "Comes from", "Seats", "Priority", "Confirmation"]
    assert facts["One signup sheet"]["value"] == "Hyjal Summit, then Barrow Deeps × 2" and facts["Comes from"]["href"] == f"/schedules/{sid}"
    assert facts["Seats"]["value"] == "20, then 10 + 10" and any("Tuesday raid night" in n for n in p["notes"])
    # what the page sends for a schedule's night: no lineup (the schedule's own); the rosters and the lock choice are never applied to it
    sent = {"start": p["start"], "schedule": p["schedule"], "occurrence": p["occurrence"], "seen": p["box"]["fp"], "rosters": 3, "timing": {"lock_hours_before": 2}}
    r = c.post(f"/api/raid/{HYJAL}/open", json=sent, headers=h)
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    assert ev.occurrence == p["occurrence"] and [(e["raid"], e["rosters"]) for e in rc.run_lineup(reg, rs, ev)] == [(HYJAL, 1), (RID, 2)]
    assert reg.config.team(ev.team)["cutoff_hard_hours"] != 2


def test_the_new_event_page_reads_its_choices_from_the_server(officer_client, reg):
    sid = tuesday(reg)
    j = officer_client.get("/api/event-new").json()
    assert j["today"] == reg.now_local().date().isoformat()
    assert [(x["id"], x["name"], x["size"], x["hours"]) for x in j["raids"]] == [
        (rid, reg.raid_def(rid)["name"], int(reg.raid_def(rid)["size"]), float(reg.raid_def(rid)["duration_hours"])) for rid in reg.profile.raids]
    assert j["lineups"] == [{"id": sid, "name": "Tuesday raid night", "lineup": [{"raid": HYJAL, "rosters": 1, "starts": 0}, {"raid": RID, "rosters": 2, "starts": 180}]}]
    assert [x["words"] for x in j["locks"]] == ["2 hours before", "24 hours before"] and [x["hours"] for x in j["locks"]] == [2, 24]
    assert j["lineup_max"] == 4


def test_copy_a_lineup_leaves_pickup_templates_out(officer_client, reg):
    """A pickup template's lineup copied without its timing would open something else than the template (the New event
    review, P3): "Copy a lineup" offers schedules only."""
    sid = tuesday(reg)
    reg.set_schedule("pug", {"kind": "pickup", "name": "Pickup"}, "t", raid=HYJAL)
    j = officer_client.get("/api/event-new").json()
    assert [x["id"] for x in j["lineups"]] == [sid]
    assert j["lineup_max"] == 4 and j["lineup_full"] == registry.LINEUP_MAX_WORDS


def test_a_preview_without_a_lineup_never_builds_the_new_event_view(web_bot, reg, monkeypatch):
    """Only the New event page sends `lineup`: every other preview caller (the old Open wizard on Rosters) answers without
    event_view.new_event_view, so a refusal raised there never turns a working preview into a 500 (P3)."""
    def boom(*a, **k):
        raise registry.RegistryError("not for this caller")
    monkeypatch.setattr(event_view, "new_event_view", boom)
    c, h = web_bot.c, H()
    day = (reg.now_local() + timedelta(days=3)).strftime("%Y-%m-%d")
    r = c.post(f"/api/raid/{HYJAL}/open/preview", json={"when": f"{day} 19:00", "schedule": ""}, headers=h)
    assert r.status_code == 200, r.text
    assert "facts" not in r.json()


def test_a_four_step_lineup_opens_on_a_four_raid_profile(store, profile, monkeypatch):
    """profiles/forever has three raids: the four-step lineup runs on a profile COPY with one more (tests/test_lineups.py's
    way) — the preview reads all four steps and Open makes four parts from one signup; a fifth is a refusal box."""
    import copy

    from conftest import GUILD, TEST_MEMBERS
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN
    from test_raid_night import NightBot

    from oibot_gm.registry import Registry
    from oibot_gm.web import app as web

    p = copy.deepcopy(profile)
    for rid, name in (("extra_a", "Extra A"), ("extra_b", "Extra B")):
        p.raids[rid] = {**copy.deepcopy(p.raids[RID]), "id": rid, "name": name, "duration_hours": 2}
    reg = Registry(store, GUILD, p)
    reg.seed_test_members(TEST_MEMBERS, "t")
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")
    rs = rc.RaidStore(store, GUILD)
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    c = TestClient(web.create_app(NightBot(reg, rs)))
    later = [{"raid": RID, "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}, {"raid": "extra_a", "rosters": 1}]
    body = {"when": wall(reg, start_in(reg, 4)), "schedule": "", "lineup": later}
    p = c.post(f"/api/raid/{HYJAL}/open/preview", json=body, headers=H()).json()
    assert [s["time_words"] for s in p["lineup"]] == ["6:30 PM", "9:30 PM", "12:00 AM", "1:30 AM"]
    assert {f["label"]: f["value"] for f in p["facts"]}["Seats"] == "20, then 10 + 10, then 40, then 10"
    r = c.post(f"/api/raid/{HYJAL}/open", json={**body, "start": p["start"], "occurrence": p["occurrence"]}, headers=H())
    assert r.status_code == 200, r.text
    assert [pt.instance for pt in rc.event_parts(rs, rs.events[r.json()["key"]])] == [HYJAL, RID, "onyxias_lair", "extra_a"]
    five = body | {"when": wall(reg, start_in(reg, 6)), "lineup": later + [{"raid": "extra_b", "rosters": 1}]}
    r = c.post(f"/api/raid/{HYJAL}/open/preview", json=five, headers=H())
    assert r.status_code == 400 and r.json()["box"]["verb"] is None


# ---------------------------------------------------------------- Edit lineup (phase 4, Task 10)

def test_edit_lineup_reads_its_event_and_says_what_a_draft_makes(web_bot, reg, rs):
    """The Edit lineup page reads ONE route: the event's own lineup (any raid's key answers as its event), its start's
    clock for the builder, the palette; a draft POSTed there answers "What this makes" — the facts, each step's start in
    the server's 12-hour words, and the Save box itself (consequences.lineup_box) — and writes nothing."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    later = rc.event_parts(rs, ev)[1]
    g = c.get(f"/api/event/{later.key}/lineup", headers=h)
    assert g.status_code == 200, g.text
    g = g.json()
    assert (g["key"], g["title"], g["start_time"]) == (ev.key, reg.local12(ev.start), "18:30")
    assert g["lineup"] == rc.run_lineup(reg, rs, ev) and g["refusal_box"] is None
    assert g["raids"] == event_view.new_event_options(reg)["raids"] and g["lineup_max"] == registry.LINEUP_MAX
    assert g["lineup_full"] == registry.LINEUP_MAX_WORDS
    before = rc.run_lineup(reg, rs, ev)
    draft = [{"raid": HYJAL, "rosters": 1, "starts": 0}, {"raid": RID, "rosters": 1, "starts": 180}]
    p = c.post(f"/api/event/{ev.key}/lineup", json={"lineup": draft}, headers=h)
    assert p.status_code == 200, p.text
    p = p.json()
    facts = {f["label"]: f["value"] for f in p["facts"]}
    assert facts == {"One signup sheet": "Hyjal Summit, then Barrow Deeps", "Seats": "20, then 10"}
    assert [(s["raid"], s["starts"], s["time_words"]) for s in p["lineup"]] == [(HYJAL, 0, "6:30 PM"), (RID, 180, "9:30 PM")]
    assert p["box"] == cq.lineup_box(reg, rs, ev, draft) and p["box"]["verb"] == "Save lineup"
    assert any("Barrow Deeps" in d and "→" in d for d in p["box"]["diff"])
    assert rc.run_lineup(reg, rs, ev) == before, "a draft writes nothing"
    assert p["same"] is False
    # the event's own lineup sent back is the server's 'nothing changes' — `same`, never drawn as a refusal
    same = c.post(f"/api/event/{ev.key}/lineup", json={"lineup": before}, headers=h).json()
    assert same["same"] is True and same["box"]["verb"] is None
    bad = c.post(f"/api/event/{ev.key}/lineup", json={"lineup": "x"}, headers=h)
    assert bad.status_code == 400


def test_edit_lineup_on_a_locked_event_is_its_refusal(web_bot, reg, rs):
    """After the lock the route answers the server's LINEUP_LOCKED box (Back only) and no draft can be made."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, RID, start_in(reg, 1), by="t")
    ms = people(reg, 10)
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    g = c.get(f"/api/event/{ev.key}/lineup", headers=h).json()
    assert g["refusal_box"] == cq.refusal_box_for(rc.LINEUP_LOCKED) and g["refusal_box"]["sentences"] == [rc.LINEUP_LOCKED]
    assert c.get("/api/event/nope/lineup", headers=h).status_code == 404


def test_edit_lineups_save_box_goes_stale_and_the_fresh_one_goes_through(web_bot, reg, rs):
    """The Save press carries the fp of the box on screen: when the event changed while it was open, 409 with the fresh
    box and nothing written; the next press with THAT box's fp saves."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t", lineup=EVENING)
    draft = [{"raid": HYJAL, "rosters": 1, "starts": 0}, {"raid": RID, "rosters": 1, "starts": 180}]
    old = c.post(f"/api/run/{ev.key}/lineup", json={"lineup": draft, "preview": True}, headers=h).json()["box"]
    join(reg, rs, ev, people(reg, 3))  # someone joins while the box is open: the event's fingerprint moves
    stale = c.post(f"/api/run/{ev.key}/lineup", json={"lineup": draft, "seen": old["fp"]}, headers=h)
    assert stale.status_code == 409 and stale.json()["box"]["fp"] != old["fp"]
    assert [e["rosters"] for e in rc.run_lineup(reg, rs, ev)] == [1, 2], "nothing written"
    ok = c.post(f"/api/run/{ev.key}/lineup", json={"lineup": draft, "seen": stale.json()["box"]["fp"]}, headers=h)
    assert ok.status_code == 200, ok.text
    assert [e["rosters"] for e in rc.run_lineup(reg, rs, ev)] == [1, 1]


def test_the_lock_rule_of_edit_lineup_is_one_function(reg, rs):
    """Fix round 1: the Edit lineup page's refusal and lineup_refusal read ONE rule (rc.lineup_fixed)."""
    ev = rc.open_run(reg, rs, RID, start_in(reg, 1), by="t")
    assert rc.lineup_fixed(rs, ev) is None and event_view.lineup_edit_json(reg, rs, ev)["refusal_box"] is None
    lock_with_board(reg, rs, ev, [])
    assert rc.lineup_fixed(rs, ev) == rc.LINEUP_LOCKED == rc.lineup_refusal(reg, rs, ev, rc.run_lineup(reg, rs, ev))
    assert event_view.lineup_edit_json(reg, rs, ev)["refusal_box"] == cq.refusal_box_for(rc.LINEUP_LOCKED)


# ---------------------------------------------------------------- More › Split strategy… (fix round 1)

def test_split_strategy_asks_with_the_servers_box_and_the_picture_and_lock_follow_it(web_bot, reg, rs, monkeypatch):
    """One raid's split strategy through the existing route: the pick box (Back only), the strategy it already has
    (Back only), another one (its box, `seen`), stale → 409; the write moves the board's fingerprint (the picture is
    placed again) and the lock's own solve (_solve_run, no strategy passed) uses it; refused once locked."""
    from oibot_gm import event_board as eb

    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, RID, start_in(reg, 3), by="t")
    join(reg, rs, ev, people(reg, 12))
    now = rc.run_strategy(reg, ev)
    other = next(s for s in ("rotation", "first", "balanced") if s != now)
    url = f"/api/run/{ev.key}/strategy"
    pick = c.post(url, json={"preview": True}, headers=h).json()["box"]
    assert pick["verb"] is None and pick["sentences"] == [cq.STRATEGY_PICK.format(raid="Barrow Deeps")]
    same = c.post(url, json={"preview": True, "strategy": now}, headers=h).json()["box"]
    assert same["verb"] is None and same["sentences"] == [cq.STRATEGY_SAME.format(raid="Barrow Deeps")]
    b = c.post(url, json={"preview": True, "strategy": other}, headers=h).json()["box"]
    assert b["verb"] == "Use this split" and b == cq.strategy_box(reg, ev, other) and len(b["sentences"]) == 3
    assert [x["id"] for x in c.get(f"/api/event/{ev.key}", headers=h).json()["strategies"]] == list(registry.SPLIT_POLICIES)
    before = eb.fingerprint(reg, eb.frozen(rs), eb.snapshot(rs, ev))
    assert c.post(url, json={"strategy": other, "seen": "stale"}, headers=h).status_code == 409 and rs.events[ev.key].split_strategy != other
    r = c.post(url, json={"strategy": other, "seen": b["fp"]}, headers=h)
    assert r.status_code == 200, r.text
    ev = rs.events[ev.key]
    assert rc.run_strategy(reg, ev) == other and c.get(f"/api/event/{ev.key}", headers=h).json()["parts"][0]["strategy"] == other
    assert eb.fingerprint(reg, eb.frozen(rs), eb.snapshot(rs, ev)) != before, "the picture is placed again by the new strategy"
    seen: list = []
    real = rc.solver.solve_rosters

    def spy(*a, **k):
        seen.append(k.get("strategy"))
        return real(*a, **k)
    monkeypatch.setattr(rc.solver, "solve_rosters", spy)
    rc._solve_run(reg, rs, ev, time_limit=1, whatif=False)
    assert seen == [other], "the lock solves by the run's own strategy"
    lock_with_board(reg, rs, ev, [])
    r = c.post(url, json={"strategy": now}, headers=h)
    assert r.status_code == 400 and r.json()["box"]["sentences"] == [cq.STRATEGY_LOCKED]


def test_the_strategy_route_has_one_body_model():
    """Task 12: the strategy route reads its body itself; the unused StrategyBody model is gone."""
    from oibot_gm.web import api

    assert not hasattr(api, "StrategyBody")


def test_the_strategy_box_is_stale_when_another_officer_changed_the_split(web_bot, reg, rs):
    """The final review: the strategy box's fp held the board only, so another officer's split change while it was open
    was silently overwritten. Its fp now carries the split the raid has; the press answers 409 with the fresh box."""
    c, h = web_bot.c, H()
    ev = rc.open_run(reg, rs, RID, start_in(reg, 3), by="t")
    join(reg, rs, ev, people(reg, 12))
    now = rc.run_strategy(reg, ev)
    mine, theirs = [s for s in ("rotation", "first", "balanced") if s != now][:2]
    url = f"/api/run/{ev.key}/strategy"
    b = c.post(url, json={"preview": True, "strategy": mine}, headers=h).json()["box"]
    ev.split_strategy = theirs  # another officer's change while the box is open
    rs.save(ev, "another officer")
    r = c.post(url, json={"strategy": mine, "seen": b["fp"]}, headers=h)
    assert r.status_code == 409 and r.json()["box"] == cq.strategy_box(reg, rs.events[ev.key], mine)
    assert rs.events[ev.key].split_strategy == theirs, "nothing overwritten"


def test_no_answer_lists_who_the_run_is_for_with_the_character_it_gives_them(reg, rs):
    """rc.pool_unanswered as its docstring says (the final review: it claimed more): run_pool, never `expected` — a
    one-off lists every main without an answer, the main as the character; a member run_pool gives no character (no
    active main) is never listed, even with an alt that plays."""
    ms = people(reg, 6)
    m = ms[0]
    reg.add_character(m.discord_id, m.display_name, "Altchar", "Priest", "Holy", None, False)
    m = reg.members[m.discord_id]
    m.main.status = "retired"
    reg.save(m, "t: main retired")
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg, 3), by="t")
    join(reg, rs, ev, ms[1:3])
    got = rc.pool_unanswered(reg, ev)
    assert [x.discord_id for x, _c in got] == sorted((x.discord_id for x in ms[3:]), key=lambda u: reg.members[u].display_name.lower())
    assert all(c is x.main for x, c in got) and rc.not_answered(reg, ev) == [], "a one-off expects nobody; who it is for is listed"
    assert m.discord_id not in {x.discord_id for x, _c in got}, "no active main: not listed, though an alt plays"


def test_the_sentences_the_pages_wrote_are_the_servers_now(officer_client):
    """The final review: empty states and hints the pages composed come from PAGE_WORDS (carried by /api/meta) under the
    Copy rule — whole sentences that name what is missing and how to fill it; the schedule editor's reminder switch and
    its select no longer both read "Reminder"."""
    words = officer_client.get("/api/meta").json()["page_words"]
    for k in ("events_empty", "schedules_empty", "raids_empty", "raid_missing", "new_event_no_lineup", "new_event_no_time",
              "schedule_no_lineup", "activity_empty", "repost_sheet", "nothing_applies"):
        s = words[k]
        assert s == PAGE_WORDS[k] and s[0].isupper() and s.endswith(".") and ":" not in s and "—" not in s, (k, s)
        assert all(len(x.split()) <= 20 for x in s.split(". ")), (k, s)
    assert "event" in words["nothing_applies"] and "run" not in words["nothing_applies"]
    assert words["weight_same"].format(n=3) == "Same as the raid (3)"
    f = words["schedule_fields"]
    assert f["nudge"] != f["nudge_hours_before"], "the switch and the select are told apart"
