"""The members' event view, task 1 (design 2026-11-25 §2): member_event_view.py and its two routes. A member sees what
the Discord signup sheet already shows everyone, plus their own answer rows and asks — and nothing an officer-only
surface shows. Each no-leak test builds a state where the officer-only fact EXISTS and searches the WHOLE serialised
answer for it (its text and its key), never only a missing field."""
from __future__ import annotations

import json
import types
from datetime import timedelta

import pytest
from conftest import GUILD2, OWNER, join, lock_with_board, open_test_run, weekly
from fastapi.testclient import TestClient
from test_member_inbox_web import OFFICER, SECRET, TOKEN, WebNightBot, as_user, person
from test_web_multiguild import DEMO_ONLY, bot, client_for, env  # noqa: F401 — the two-guild fixtures
from test_web_multiguild import as_user as as_user2

from oibot_gm import member_event_view as mev
from oibot_gm import raidcycle as rc
from oibot_gm import raid_views as rv
from oibot_gm.constants import PAGE_WORDS
from oibot_gm.web import app as web
from oibot_gm.web.api_me import me_sheet_row

HYJAL, RID = "hyjal_summit_forever", "barrow_deeps"
REAL = 473_000_000_000_000_000
BEARER = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:  # the profile's raids first open 9 Dec: every event here is after it
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def keys_of(obj) -> set[str]:
    """Every dict key in a JSON answer, recursively."""
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= keys_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_of(v)
    return out


def dump(j) -> str:
    return json.dumps(j, ensure_ascii=False)


def start_in(reg, days: float):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def add(reg, i: int, name: str, cls: str, spec: str, offspec: str | None = None):
    m, _c = reg.add_character(REAL + i, name, name, cls, spec, offspec, True)
    return reg.members[REAL + i]


class MemberBot(WebNightBot):
    """WebNightBot whose server holds every member on file (real members too), so any of them may open the site."""

    def admit(self) -> None:
        from conftest import OFFICER_ROLE, MemberDirectory

        reg = self.reg if hasattr(self, "reg") else next(iter(self.registries.ordered()))
        people = {(1, OFFICER): person(OFFICER, OFFICER_ROLE), (1, OWNER): person(OWNER)}
        for m in reg.members.values():
            people[(1, m.discord_id)] = person(m.discord_id)
        MemberDirectory(people).install(self, servers=(1,))


def member_client(reg, rs, monkeypatch) -> TestClient:
    """The real web app over NightBot's verbs, every member admitted (build the state first: admission is at creation)."""
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    return TestClient(web.create_app(MemberBot(reg, rs)))


def leak_fixture(reg, rs) -> types.SimpleNamespace:
    """One locked event (Barrow Deeps, confirmations on) and one open event (Hyjal Summit, then Barrow Deeps). Between
    them every officer-only fact the tests look for: a member with no answer, an away member with a reason, a callout
    with a note, an "in" and an "out" pin, a pending fill ask to someone else, the officers' board (layout, placed_by,
    auto_placed, a drag pin), a log line, and a short tank / healer with a role-switch row."""
    viewer = add(reg, 0, "Viewer", "Priest", "Holy")
    plain = add(reg, 1, "Plainjoiner", "Mage", "Fire")
    silent = add(reg, 2, "Silentxyz", "Rogue", "Combat")  # never answers anything
    away = add(reg, 3, "Awayxyz", "Mage", "Frost")
    caller = add(reg, 4, "Callerxyz", "Warlock", "Destruction")
    target = add(reg, 5, "Askedxyz", "Druid", "Restoration")  # the fill ask's target
    fury = add(reg, 6, "Furyxyz", "Warrior", "Fury", "Protection")  # short tank: a role-switch row on the open event
    pinned_in, pinned_out = add(reg, 7, "Keepin", "Hunter", "Marksmanship"), add(reg, 8, "Keepout", "Rogue", "Combat")
    benchy = add(reg, 9, "Benchy", "Paladin", "Holy")
    fillonly = add(reg, 20, "Fillonlyxyz", "Shaman", "Restoration")  # on neither event: only a fill ask names them
    fillers = [add(reg, 10 + i, f"Seat{chr(97 + i)}", *cs) for i, cs in enumerate(
        [("Warrior", "Protection"), ("Warrior", "Protection"), ("Priest", "Holy"), ("Mage", "Fire"), ("Rogue", "Combat"),
         ("Hunter", "Marksmanship"), ("Warlock", "Affliction")])]

    # the locked event: 10 seats; viewer + plain + 7 fillers + target seated, caller joined but left off, then called out
    locked = rc.open_run(reg, rs, RID, start_in(reg, 4), by="t")
    reg.config.team(locked.team)["confirm"] = True  # a one-off asks for no confirmations by default: this one does
    seated = [viewer, plain, target, *fillers]
    join(reg, rs, locked, [*seated, caller])
    join(reg, rs, locked, [benchy], "sub")
    rc.set_signup(reg, rs, locked, away, None, "out", source="absence")
    day = locked.start.astimezone(reg.tz).date().isoformat()
    reg.add_absence(away.discord_id, day, day, "dentist-XYZ", "t")
    lock_with_board(reg, rs, locked, [m.display_name for m in seated])
    locked = rs.events[locked.key]
    rc.callout(reg, rs, locked, caller, rc.run_team(reg, locked), "note-XYZ")
    reg.add_placement_ask(viewer.discord_id, locked.team, viewer.main.label, "t")
    reg.add_placement_ask(plain.discord_id, locked.team, plain.main.label, "t")
    reg.close_placement_ask(plain.discord_id, locked.team, "yes", "t")
    locked = rs.events[locked.key]
    locked.fill_asks.append(rc.FillAsk(discord_id=target.discord_id, display_name=target.display_name, kind="sub", role="healer",
                                       character=target.main.label, spec="Restoration", reason="short 1 healer", why="why-XYZ"))
    locked.fill_asks.append(rc.FillAsk(discord_id=fillonly.discord_id, display_name=fillonly.display_name, kind="sub", role="healer",
                                       character=fillonly.main.label, spec="Restoration", reason="short 1 healer", why="why-XYZ"))
    locked.log.append("log-XYZ")
    rs.save(locked, "t: a fill ask and a log line")

    # the open event: Hyjal Summit, then Barrow Deeps; the officers' board, pins and a log line
    opened = rc.open_run(reg, rs, HYJAL, start_in(reg, 2), by="t", lineup=[{"raid": RID, "rosters": 1}])
    join(reg, rs, opened, [viewer, plain, fury, pinned_in, pinned_out])
    opened = rs.events[opened.key]
    opened.pins = {str(pinned_in.discord_id): "in", str(pinned_out.discord_id): "out"}
    opened.layout = [[plain.display_name, fury.display_name]]
    opened.placed_by = {plain.display_name: OFFICER}
    opened.auto_placed = [fury.display_name]
    opened.drag_pins = {str(pinned_in.discord_id): {"by": OFFICER, "was": None, "now": "in"}}
    opened.log.append("log-XYZ")
    rs.save(opened, "t: the officers' board")
    opened = rs.events[opened.key]
    later = rs.events[opened.followed_by]
    # every fact exists
    assert rc.pool_unanswered(reg, opened) and any(m.discord_id == silent.discord_id for m, _c in rc.pool_unanswered(reg, opened))
    assert locked.callouts[-1].note == "note-XYZ" and locked.signups[str(caller.discord_id)].source == "callout"
    assert sum(a.pending for a in locked.fill_asks) == 2
    assert str(fillonly.discord_id) not in locked.signups and str(fillonly.discord_id) not in opened.signups
    assert opened.layout and opened.placed_by and opened.auto_placed and opened.pins
    assert rc.event_switches(reg, rs, opened)["rows"]
    assert reg.members[away.discord_id].absent_on(day)
    return types.SimpleNamespace(locked=locked, opened=opened, later=later, viewer=viewer, plain=plain, silent=silent, away=away,
                                 caller=caller, target=target, fury=fury, benchy=benchy, seated=seated, fillonly=fillonly)


@pytest.fixture
def lf(reg, rs, monkeypatch):
    f = leak_fixture(reg, rs)
    f.c = member_client(reg, rs, monkeypatch)
    return f


def get(f, path, uid=None):
    h = as_user(f.c, uid or f.viewer.discord_id)
    return f.c.get(path, headers=h)


def both(f, uid=None) -> list:
    """The two events and the list, as the member routes answer them."""
    out = []
    for path in (f"/api/me/event/{f.locked.key}", f"/api/me/event/{f.opened.key}", "/api/me/events"):
        r = get(f, path, uid)
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


# ---------------------------------------------------------------- no leaks

def test_member_routes_leave_out_who_has_not_answered(lf):
    for j in both(lf):
        assert lf.silent.display_name not in dump(j)
        assert not {"none", "no_answer", "people"} & keys_of(j)


def test_member_routes_leave_out_absence_reasons(lf):
    j = get(lf, f"/api/me/event/{lf.locked.key}").json()
    assert "dentist-XYZ" not in dump(j) and "reason" not in keys_of(j["signups"])
    assert [p["display_name"] for p in j["signups"]["rows"]["away"]] == [lf.away.display_name]
    assert lf.away.display_name not in [p["display_name"] for p in j["signups"]["rows"]["out"]]
    for j in both(lf):
        assert "dentist-XYZ" not in dump(j)


def test_member_routes_leave_out_callout_notes(lf):
    j = get(lf, f"/api/me/event/{lf.locked.key}").json()
    assert "note-XYZ" not in dump(j) and "note" not in keys_of({k: v for k, v in j.items() if k != "answers"})
    row = next(p for p in j["signups"]["rows"]["out"] if p["display_name"] == lf.caller.display_name)
    assert row["callout"] is True
    for j in both(lf):
        assert "note-XYZ" not in dump(j)


def test_member_routes_leave_out_pins(lf):
    for j in both(lf):
        assert not {"pin", "pins", "kept_off", "drag_pins"} & keys_of(j)
        assert not any(w in dump(j) for w in ("Pinned", "Kept off", "kept off"))


def test_member_routes_leave_out_fill_asks(lf):
    for j in both(lf, lf.plain.discord_id):
        assert "fill_asks" not in keys_of(j) and "why-XYZ" not in dump(j)
    j = get(lf, f"/api/me/event/{lf.locked.key}", lf.plain.discord_id).json()
    assert j["asks"] == [], "the ask is the target's alone"
    mine = get(lf, f"/api/me/event/{lf.locked.key}", lf.target.discord_id).json()["asks"]
    assert [a["kind"] for a in mine] == ["fill"], "the target sees their own ask"
    # a target on neither event: their NAME is nowhere in anyone else's answer
    for uid in (lf.viewer.discord_id, lf.plain.discord_id, lf.target.discord_id, lf.silent.discord_id, OFFICER):
        for j in both(lf, uid):
            assert lf.fillonly.display_name not in dump(j) and lf.fillonly.main.label not in dump(j)
    theirs = get(lf, f"/api/me/event/{lf.locked.key}", lf.fillonly.discord_id).json()
    assert [a["kind"] for a in theirs["asks"]] == ["fill"], "the target on neither event still sees their own ask"


def test_member_routes_leave_out_the_officers_board(lf):
    for j in both(lf):
        assert not {"layout", "placed_by", "auto_placed", "mine", "undo_box", "autofill", "placed_as"} & keys_of(j)
    j = get(lf, f"/api/me/event/{lf.opened.key}").json()
    assert j["rosters"] == [], "before the lock there is no board for members"
    officer_name = person(OFFICER).display_name  # the placing officer as the server knows them
    for uid in (lf.viewer.discord_id, lf.plain.discord_id, lf.silent.discord_id):
        for j in both(lf, uid):
            assert str(OFFICER) not in dump(j) and officer_name not in dump(j), "who placed whom stays the officers'"


def test_member_routes_leave_out_the_activity_log(lf):
    for j in both(lf):
        assert "activity" not in keys_of(j) and "log" not in keys_of(j) and "log-XYZ" not in dump(j)


def test_member_routes_leave_out_role_switches(lf, reg, rs):
    sw = rc.event_switches(reg, rs, rs.events[lf.opened.key])
    fury = next(r for r in sw["rows"] if r["display_name"] == lf.fury.display_name)
    assert fury["spec"] == "Protection" and fury["from"] == "Fury"
    words = [fury["box"]["title"], *fury["box"]["sentences"]]
    unanswered = [r["display_name"] for r in sw["rows"] if not r["answered"]]
    assert unanswered
    for j in both(lf):
        assert not {"switches", "short", "revs"} & keys_of(j)
        assert not any(w in dump(j) for w in words), "the switch row's words"
    o = get(lf, f"/api/me/event/{lf.opened.key}").json()
    people = [p for rows in o["signups"]["rows"].values() for p in rows]
    assert [(p["character"], p["spec"]) for p in people if p["display_name"] == lf.fury.display_name] == [(lf.fury.main.label, "Fury")]
    assert not set(unanswered) & {p["display_name"] for p in people}, "a switch row's unanswered names stay off the event"


def test_member_routes_leave_out_priority_scores(lf):
    for j in both(lf):
        assert not {"bonus", "bench_whatif", "advisories", "synergy", "synergy_value", "objective", "weights", "details_long",
                    "attended", "rank"} & keys_of(j)
        assert "Closest to a seat" not in dump(j)


def test_member_details_are_the_public_facts_only(lf):
    j = get(lf, f"/api/me/event/{lf.locked.key}").json()
    assert [f["label"] for f in j["details"]] == ["Lineup", "Priority", "Locks", "Confirm by", "Signup sheet"]
    assert j["details"][-1]["value"] == PAGE_WORDS["sheet_not_posted"]
    o = get(lf, f"/api/me/event/{lf.opened.key}").json()
    assert o["details"][0]["value"] == "Hyjal Summit, then Barrow Deeps"


# ---------------------------------------------------------------- what is listed

def test_the_member_list_has_live_events_only(lf, reg, rs):
    done = rc.open_run(reg, rs, RID, start_in(reg, 6), by="t")
    done.state = "done"
    rs.save(done, "t: finished")
    j = get(lf, "/api/me/events").json()
    assert [r["key"] for r in j["open"]] == [lf.opened.key, lf.locked.key], "soonest first, first raids only"
    assert "history" not in j and "history_more" not in j and done.key not in dump(j)
    assert j["empty"] == PAGE_WORDS["member_events_empty"] and j["tz_words"]
    assert all(set(r) == {"title", "subtitle"} for r in j["coming"])
    rows = {r["key"]: r for r in j["open"]}
    assert rows[lf.locked.key]["answer"] == "rostered" and rows[lf.opened.key]["answer"] == rc.LABELS["in"]
    assert rows[lf.opened.key]["subtitle"] == "Hyjal Summit, then Barrow Deeps"
    assert rows[lf.locked.key]["chip"] == rc.RUN_STATE_WORDS["locked"]


def test_a_rehearsal_is_hidden_from_everyone_but_its_tester(lf, reg, rs):
    reg.seed_test_members(4, "t")
    test = open_test_run(reg, rs, days=5, test_by=lf.plain.discord_id)
    assert rc.rehearsal_run(reg, test)
    j = get(lf, "/api/me/events").json()
    assert test.key not in [r["key"] for r in j["open"]]
    r = get(lf, f"/api/me/event/{test.key}")
    assert r.status_code == 404 and r.json() == {"error": PAGE_WORDS["event_over_member"]}
    mine = get(lf, "/api/me/events", lf.plain.discord_id).json()
    assert test.key in [r["key"] for r in mine["open"]]
    r = get(lf, f"/api/me/event/{test.key}", lf.plain.discord_id)
    assert r.status_code == 200 and r.json()["test_chip"] == PAGE_WORDS["test_run"]


def test_a_dropped_raid_is_hidden(lf, reg, rs):
    first = lf.opened
    change = rc.plan_lineup(reg, rs, first, rc.run_lineup(reg, rs, first)[:1])
    rc.apply_lineup(reg, rs, first, change, "t")
    gone = rs.events[lf.later.key]
    assert gone.dropped
    r = get(lf, f"/api/me/event/{gone.key}")
    assert r.status_code == 404 and r.json() == {"error": PAGE_WORDS["event_over_member"]}
    j = get(lf, f"/api/me/event/{first.key}").json()
    assert gone.key not in j["part_keys"] and gone.key not in dump(j["rosters"]) and gone.key not in [a["key"] for a in j["answers"]]


def test_a_later_raids_key_answers_as_its_event(lf):
    r = get(lf, f"/api/me/event/{lf.later.key}")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["key"] == lf.opened.key and j["part_keys"] == [lf.opened.key, lf.later.key]
    assert [a["key"] for a in j["answers"]] == [lf.opened.key, lf.later.key], "the first raid first"


def test_a_finished_event_is_404_in_the_one_sentence(lf, reg, rs):
    ev = rs.events[lf.locked.key]
    ev.state = "done"
    rs.save(ev, "t: finished")
    for key in (lf.locked.key, "no-such-key"):
        r = get(lf, f"/api/me/event/{key}")
        assert r.status_code == 404 and r.json() == {"error": PAGE_WORDS["event_over_member"]}


def test_a_non_member_of_the_guild_gets_403(bot):  # noqa: F811 — test_web_multiguild's two-guild bot
    c = client_for(bot)
    h = as_user2(c, DEMO_ONLY)
    for path in ("/api/me/events", "/api/me/event/anything"):
        r = c.get(path, headers={**h, "X-Oibot-Guild": GUILD2})
        assert r.status_code == 403, r.text
        assert r.json()["not_member"] is True


def test_an_officer_gets_the_member_shape_from_the_member_routes(lf):
    """The owner (the bearer) and the officer get exactly a member's shape: no boxes, actions or board."""
    for key in (lf.locked.key, lf.opened.key):
        member = get(lf, f"/api/me/event/{key}", lf.silent.discord_id).json()  # a member with no answer and no asks
        officer = get(lf, f"/api/me/event/{key}", OFFICER).json()
        lf.c.cookies.clear()
        owner = lf.c.get(f"/api/me/event/{key}", headers=BEARER).json()
        shape = lambda j: keys_of({k: v for k, v in j.items() if k != "characters"})  # noqa: E731 — the officers hold no character here
        own = lambda j: {k: v for k, v in j.items() if k not in ("answers", "characters", "asks")}  # noqa: E731 — the viewer's own
        for j in (officer, owner):
            assert shape(j) == shape(member) and isinstance(j["characters"], list)
            assert own(j) == own(member), "an officer sees the same VALUES as a member"
            assert not {"boxes", "actions", "parts", "people", "edit", "timing"} & keys_of(j)
            assert lf.silent.display_name not in dump({k: v for k, v in j.items() if k not in ("answers", "characters")})
    lists = [get(lf, "/api/me/events", uid).json() for uid in (lf.silent.discord_id, OFFICER)]
    lf.c.cookies.clear()
    lists.append(lf.c.get("/api/me/events", headers=BEARER).json())
    strip = lambda j: {**j, "open": [{k: v for k, v in r.items() if k != "answer"} for r in j["open"]]}  # noqa: E731 — the viewer's own answer
    assert strip(lists[1]) == strip(lists[0]) and strip(lists[2]) == strip(lists[0])


# ---------------------------------------------------------------- the sheet's own facts

def test_the_locked_roster_matches_the_sheet(lf, reg, rs):
    ev = rs.events[lf.locked.key]
    j = get(lf, f"/api/me/event/{ev.key}").json()
    [part] = j["rosters"]
    assert part["key"] == ev.key and part["marks"] is True and part["raid_name"] == "Barrow Deeps"
    for r, mine in zip(ev.all_rosters, part["rosters"]):
        by = {p.signup_name: p for p in r.selected}
        want = [[by[n].character or n for n in g if n in by] for g in r.groups]
        assert [[s["character"] for s in g] for g in mine["groups"]] == want
    conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, ev)}
    seats = [s for r in part["rosters"] for g in r["groups"] for s in g]
    assert {s["display_name"]: s["answer"] for s in seats} == conf
    assert conf[lf.plain.display_name] == "yes"
    joined = {s.display_name for s in ev.by_status("in")}
    assert part["not_rostered"] == [p.signup_name for p in ev.all_rosters[0].benched if p.signup_name in joined]
    # the signups follow the sheet's rows: Bench, Away, No thanks with the callout mark
    away, declined = rv.split_away(reg, ev, ev.by_status("out"))
    s = j["signups"]
    assert [p["display_name"] for p in s["rows"]["in"]] == [x.display_name for x in ev.by_status("in")]
    assert [p["display_name"] for p in s["rows"]["sub"]] == [lf.benchy.display_name]
    assert [p["display_name"] for p in s["rows"]["out"]] == [x.display_name for x in declined]
    assert s["counts"] == {"in": len(ev.by_status("in")), "sub": 1, "out": len(declined), "away": len(away)}


def test_marks_are_left_out_without_confirmations(lf, reg, rs):
    ev = rs.events[lf.locked.key]
    reg.config.team(ev.team)["confirm"] = False
    j = get(lf, f"/api/me/event/{ev.key}").json()
    part = j["rosters"][0]
    assert part["marks"] is False and all("answer" not in s for r in part["rosters"] for g in r["groups"] for s in g)
    assert "Confirm by" not in [f["label"] for f in j["details"]]


def test_answers_and_asks_are_the_viewers_own(lf, reg, rs):
    j = get(lf, f"/api/me/event/{lf.locked.key}").json()
    me = get(lf, "/api/me").json()
    row = next(s for s in me["sheets"] if s["key"] == lf.locked.key)
    assert j["answers"] == [row]
    assert [(a["kind"], a["run"]) for a in j["asks"]] == [("confirm", lf.locked.key)]
    assert j["characters"] == [{"label": lf.viewer.main.label, "is_main": True}]
    o = get(lf, f"/api/me/event/{lf.opened.key}").json()
    assert o["asks"] == [], "only this event's raids"


def test_api_me_rows_are_unchanged_by_the_move(lf, reg, rs):
    me = get(lf, "/api/me").json()
    m = reg.members[lf.viewer.discord_id]
    built = json.loads(json.dumps([me_sheet_row(reg, rs, ev, m, m.discord_id) for ev in rs.live()]))
    assert me["sheets"] == built and len(built) == 3



# ---------------------------------------------------------------- the fix round: later raids, coming-up names, regulars

def test_a_later_raid_shows_its_roster_without_confirmation_marks(lf, reg, rs):
    """Only the first raid has a public Discord sheet, so only its seats carry a confirmation mark. A later raid's roster
    (groups and Not rostered) is shown; a Can't make it and a waiting ask planted on its seats are not."""
    first, later = rs.events[lf.opened.key], rs.events[lf.later.key]
    names = [s.display_name for s in first.by_status("in")]
    for p in (first, later):
        reg.config.team(p.team)["confirm"] = True
        lock_with_board(reg, rs, rs.events[p.key], names)
    later = rs.events[later.key]
    # the later raid: a "no" (Can't make it) and a waiting ask; the first raid: a "no" from someone else
    reg.add_placement_ask(lf.plain.discord_id, later.team, lf.plain.main.label, "t")
    reg.close_placement_ask(lf.plain.discord_id, later.team, "no", "t")
    keepin = next(m for m in reg.members.values() if m.display_name == "Keepin")
    reg.add_placement_ask(keepin.discord_id, later.team, keepin.main.label, "t")
    reg.add_placement_ask(lf.fury.discord_id, first.team, lf.fury.main.label, "t")
    reg.close_placement_ask(lf.fury.discord_id, first.team, "no", "t")
    later_conf = {c["display_name"]: c["answer"] for c in rc.confirmations(reg, later)}
    assert later_conf[lf.plain.display_name] == "no" and keepin.display_name in later_conf and later_conf[keepin.display_name] is None
    for uid in (lf.viewer.discord_id, lf.plain.discord_id, keepin.discord_id, lf.silent.discord_id, OFFICER):
        j = get(lf, f"/api/me/event/{lf.opened.key}", uid).json()
        head, tail = j["rosters"]
        assert head["key"] == first.key and head["marks"] is True
        seats = {s["display_name"]: s for r in head["rosters"] for g in r["groups"] for s in g}
        assert seats[lf.fury.display_name]["answer"] == "no", "the first raid's marks stay"
        assert tail["key"] == later.key and tail["marks"] is False
        tseats = [s for r in tail["rosters"] for g in r["groups"] for s in g]
        assert sorted(s["display_name"] for s in tseats) == sorted(names), "the later raid's roster is shown"
        assert all("answer" not in s for s in tseats), "no answer on a later raid's seat"
        assert "not_rostered" in tail


def scheduled_fixture(reg, rs) -> types.SimpleNamespace:
    """The leak fixture plus a schedule named by default (conftest.weekly: "Raid nights") with a regular who never answers,
    its event opened, and a two-raid schedule — built before the app so every member is admitted."""
    f = leak_fixture(reg, rs)
    start = start_in(reg, 5)
    sid = weekly(reg, RID, start.strftime("%a %H:%M"), sid="nights")
    regular = add(reg, 30, "Regularxyz", "Paladin", "Protection")
    reg.regular_add(regular.discord_id, sid, "t")
    ev = rc.open_run(reg, rs, RID, start, by="t", schedule=sid)
    later_day = start_in(reg, 6).strftime("%a 18:30")
    reg.set_schedule("pair", {"kind": "weekly", "slots": later_day, "name": "Double night"}, "t", raid=HYJAL)
    reg.set_schedule("pair", {"lineup": [{"raid": HYJAL, "rosters": 1}, {"raid": RID, "rosters": 1, "starts": 180}]}, "t")
    assert any(m.discord_id == regular.discord_id for m, _c in rc.pool_unanswered(reg, ev)), "the regular is unanswered"
    f.sched, f.sid, f.regular = ev, sid, regular
    return f


def test_a_coming_up_row_names_its_raids(reg, rs, monkeypatch):
    f = scheduled_fixture(reg, rs)
    c = member_client(reg, rs, monkeypatch)
    j = c.get("/api/me/events", headers=as_user(c, f.viewer.discord_id)).json()
    subtitles = {r["subtitle"] for r in j["coming"]}
    assert "Barrow Deeps · Raid nights" in subtitles, subtitles
    assert "Hyjal Summit, then Barrow Deeps · Double night" in subtitles, subtitles
    assert "Raid nights" not in subtitles


def test_an_unanswered_regular_is_absent_by_name(reg, rs, monkeypatch):
    f = scheduled_fixture(reg, rs)
    c = member_client(reg, rs, monkeypatch)
    for uid in (f.viewer.discord_id, f.plain.discord_id, OFFICER):
        h = as_user(c, uid)
        for path in (f"/api/me/event/{f.sched.key}", "/api/me/events"):
            r = c.get(path, headers=h)
            assert r.status_code == 200, r.text
            assert f.regular.display_name not in dump(r.json()), path
