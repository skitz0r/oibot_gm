"""Phase 4, Task 2: the Events page's read model (event_view.py, web/api_events.py) — the list (Open · Coming up ·
History), one event in sections, a coming-up event, and the page words on /api/meta. Every fact comes from the existing
readers; a run key travels only as an id, never in a word."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join, lock_with_board, open_test_run, weekly
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import consequences as cq
from oibot_gm import event_view
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS

HYJAL, RID = "hyjal_summit_forever", "barrow_deeps"
EVENING = [{"raid": RID, "rosters": 2}]
REAL = 472_000_000_000_000_000
CLASSES = [("Warrior", "Protection"), ("Priest", "Holy"), ("Priest", "Holy"), ("Mage", "Fire"), ("Mage", "Fire"), ("Rogue", "Combat")]


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
    skip = {"key", "schedule", "occurrence", "start_iso", "href", "fp", "uid", "raid", "id", "sheet", "sheet_url", "timing", "lineup", "open"}
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


def test_the_index_agrees_with_the_finder(reg, rs):
    """event_view's one index per request answers as rc.event_of_occurrence for every schedule night."""
    sid = tuesday(reg)
    nights = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid][:4]
    ev = rc.open_run(reg, rs, HYJAL, nights[0], by="t", schedule=sid)
    reg.config.team(ev.team)["stands_for"] = rc.occurrence_stamp(reg, nights[1])
    done = rc.open_run(reg, rs, HYJAL, nights[2], by="t", schedule=sid)
    rc.end_run(reg, rs, done, "t")
    find = event_view.night_index(reg, rs)
    for t in nights:
        occ = rc.occurrence_stamp(reg, t)
        assert find(sid, occ) is rc.event_of_occurrence(rs, sid, occ, reg=reg), occ
    assert find(sid, rc.occurrence_stamp(reg, nights[3])) is None and find(sid, rc.occurrence_stamp(reg, nights[2])) is done


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
    assert hy["placed_words"] == "0 of 18 joined" and hy["board_state"] == "ready" and hy["sheet"]["key"] == ev.key
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
    j = officer_client.get(f"/api/event/{ev.key}").json()
    facts = {f["label"]: f["value"] for f in j["details"]}
    assert facts["Open seats"] == rc.fill_state_text(reg, ev)
    assert j["state"] == "locked" and j["parts"][0]["board_state"] == "locked" and j["parts"][0]["placed_words"] == "10 of 10 joined"
    assert j["actions"]["lock"] is False and j["actions"]["edit_lineup"] is False and j["boxes"]["lock"] is None
    assert facts["Confirmed"] == "10 waiting" and [f["label"] for f in j["details"]][-3:] == ["Confirmed", "Open seats", "Signup sheet"]


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
    assert j["chip"] == "finished" and j["boxes"] == {"lock": None, "cancel": None}
    assert not any(v for k, v in j["actions"].items())


def test_an_unknown_event_is_a_sentence_without_the_key(officer_client):
    r = officer_client.get("/api/event/bd-zz9k2m")
    assert r.status_code == 404 and "bd-zz9k2m" not in r.text


# ---------------------------------------------------------------- a coming-up event

def test_a_coming_up_event_says_when_it_opens_by_hand(officer_client, reg, rs):
    sid = tuesday(reg)
    nights = [t for s, t in rc.occurrences(reg, reg.now_local(), 24 * 7 * 8) if s["id"] == sid]
    far = next(t for t in nights if t > reg.now_local() + timedelta(days=rc.OPEN_HORIZON_DAYS))
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
    assert j["open"] == {"start_iso": far.isoformat(), "schedule": sid, "occurrence": rc.occurrence_stamp(reg, far)}
    assert j["schedule"] == {"id": sid, "name": "Tuesday raid night"}
    near = nights[0]
    j = officer_client.get(f"/api/event/coming/{sid}/{rc.occurrence_stamp(reg, near)}").json()
    assert j["can_open"] is True and j["why"] is None and j["box"]["verb"] == "Open the sheet"


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


# ---------------------------------------------------------------- the page words

def test_meta_carries_the_page_words(officer_client):
    m = officer_client.get("/api/meta").json()
    assert m["page_words"]["one_off"] == "One-off, no schedule" and m["confirm_words"]["discard"] == "Discard edits"
    assert m["page_words"] == PAGE_WORDS and m["confirm_words"] == CONFIRM_WORDS and m["leave"] == cq.LEAVE_TEMPLATE
    assert rc.confirmation_words(False) == PAGE_WORDS["not_asked"] and rc.confirmation_words(True) == PAGE_WORDS["asked_at_lock"]
