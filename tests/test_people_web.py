"""The people routes over HTTP (dual interfaces phase 2, part C): the Me and Members pages' character table (THE character
verb: rank refused for a member, preview, the rev), /api/members (characterless members, puppets only for their tester),
/api/members/set (rank per member), absences (From + For how long, preview, overlap, reason cap, I'm back), /api/absences,
/api/meta's durations; no owner transfer on the site (design §2.1). A real web session (signed cookie) and the MCP bearer."""
from __future__ import annotations

import time
import types
from datetime import timedelta

import pytest
from conftest import OFFICER_ROLE, OWNER, MemberDirectory, join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_people import PeopleBot, real, real_run

from oibot_gm import raidcycle as rc
from oibot_gm.registry import ABSENCE_REASON_MAX
from oibot_gm.web import app as web

TOKEN = "a" * 64
SECRET = "s" * 40
OFFICER = 300_000_000_000_000_015
H = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}


def person(uid, *roles, bot=False):
    return types.SimpleNamespace(id=uid, display_name=f"u{str(uid)[-3:]}", roles=[types.SimpleNamespace(id=r) for r in roles], bot=bot)


class WebBot(PeopleBot):
    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.people = {(1, OFFICER): person(OFFICER, OFFICER_ROLE), (1, OWNER): person(OWNER)}
        for m in reg.test_members():
            self.people[(1, m.discord_id)] = person(m.discord_id)
        self.directory = MemberDirectory(self.people)
        self.directory.install(self, servers=(1,))

    def admit(self, m, bot=False):
        self.directory.members[(1, m.discord_id)] = person(m.discord_id, bot=bot)

    def officiates(self, member, guild):
        return any(r.id == OFFICER_ROLE for r in getattr(member, "roles", []))


@pytest.fixture
def w(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    bot = WebBot(reg, rs)
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, rs=rs)


def as_user(w, uid) -> dict:
    w.c.cookies.clear()
    w.c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


def member(w, n=0, **kw):
    m = real(w.reg, n, **kw)
    w.bot.admit(m)
    return m


# ---------------------------------------------------------------- the Me page's table

def test_a_member_posting_a_rank_is_refused(w):
    m = member(w)
    h = as_user(w, m.discord_id)
    r = w.c.post("/api/me/characters", json={"rows": [{"label": m.main.label, "cls": "Priest", "spec": "Holy", "rank": "core"}]}, headers=h)
    assert r.status_code == 400 and "Only an officer sets a rank" in r.json()["error"]
    assert w.reg.members[m.discord_id].rank == "trial"


def test_me_characters_preview_rev_and_save(w):
    m = member(w)
    ev = real_run(w.reg, w.rs)
    join(w.reg, w.rs, ev, [m])
    h = as_user(w, m.discord_id)
    me = w.c.get("/api/me", headers=h).json()
    rev = me["characters_rev"]
    assert rev and me["rank"] == "trial"
    row = {"label": m.main.label, "cls": "Priest", "spec": "Shadow"}
    p = w.c.post("/api/me/characters", json={"rows": [row], "rev": rev, "preview": True}, headers=h).json()
    assert p["preview"] and any("now reads Shadow" in x for x in p["ripple"]) and w.reg.members[m.discord_id].main.spec == "Holy"
    r = w.c.post("/api/me/characters", json={"rows": [row], "rev": rev}, headers=h)
    assert r.status_code == 200, r.text
    assert ev.signups[str(m.discord_id)].spec == "Shadow"
    stale = w.c.post("/api/me/characters", json={"rows": [{"label": m.main.label, "cls": "Priest", "spec": "Discipline"}], "rev": rev}, headers=h)
    assert stale.status_code == 409 and stale.json()["characters"][0]["spec"] == "Shadow" and "changed since" in stale.json()["error"]


def test_me_delete_and_main_are_one_row_saves(w):
    m = member(w)
    w.reg.add_character(m.discord_id, m.display_name, "Secundus", "Mage", "Fire", None, False)
    h = as_user(w, m.discord_id)
    assert w.c.post("/api/me/main", json={"label": "Secundus"}, headers=h).status_code == 200
    assert w.reg.members[m.discord_id].main.label == "Secundus"
    r = w.c.post("/api/me/character/delete", json={"label": "Anabelle"}, headers=h)
    assert r.status_code == 200 and [c.label for c in w.reg.members[m.discord_id].active()] == ["Secundus"]


# ---------------------------------------------------------------- the Members page

def test_members_lists_characterless_members_and_hides_others_puppets(w):
    lone = 420_000_000_000_000_001
    w.reg.add_absence(lone, (w.reg.now_local().date() + timedelta(days=3)).isoformat(), None, None, "Officer", display_name="Lonely")
    h = as_user(w, OFFICER)
    rows = w.c.get("/api/members", headers=h).json()["rows"]
    lonely = next(r for r in rows if r["display_name"] == "Lonely")
    assert lonely["characters"] == [] and lonely["absences"] and "dm" in lonely and "characters_rev" in lonely
    assert any(r["privilege"] == "test" for r in rows)  # no rehearsal live: any officer sees the bench
    open_test_run(w.reg, w.rs, test_by=OWNER)  # someone else's rehearsal: its puppets are theirs
    rows = w.c.get("/api/members", headers=h).json()["rows"]
    assert not any(r["privilege"] == "test" for r in rows)
    assert not any(r.get("test") for r in w.c.get("/api/absences", headers=h).json()["rows"])


def test_members_asks_are_live_runs_only_in_words(w):
    """Finding 1 (design §5.58): an unanswered Confirm ask whose run had ended still showed as live ("confirm Barrow Deeps
    · Thu 17 Sep?"). Only an ask on a live run is listed, with the run's key (the link), '<raid> on <day>' and how it
    reaches them; an ended run's ask and a key with no run file are gone. Never the run key as words."""
    ms = [real(w.reg, i, cls="Mage", spec="Fire") for i in range(5)]
    for m in ms:
        w.bot.admit(m)
    done = real_run(w.reg, w.rs, days=2)
    done.state = "done"
    w.rs.save(done, "test: ended")
    ev = real_run(w.reg, w.rs, days=4)
    join(w.reg, w.rs, ev, ms)
    lock_with_board(w.reg, w.rs, ev, [m.display_name for m in ms])
    m = ms[2]
    w.reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock", channel="web")
    m.placement_asks = [{"roster": done.team, "character": m.main.label, "asked_at": "2026-09-17T19:19:00+00:00", "answer": None},
                        {"roster": "BD-0917-1919", "character": m.main.label, "asked_at": "2026-09-17T19:19:00+00:00", "answer": None},
                        *m.placement_asks]
    rows = w.c.get("/api/members", headers=as_user(w, OFFICER)).json()["rows"]
    (a,) = next(r for r in rows if r["uid"] == str(m.discord_id))["asks"]
    assert a["key"] == ev.key and a["words"] == rc.run_on(w.reg, ev) and a["line"] == f"{rc.run_on(w.reg, ev)} waits for their Confirm"
    assert a["how"] == "web" and a["how_words"] and ev.team not in a["line"] and "1919" not in a["line"]


def test_members_rows_say_left_test_attendance_and_the_rank_choices(w):
    """Design §5.58: `left` = when the bot noticed someone is not in the Discord server (in words, null while they are)
    with its line and Remove record's sentence; `test`; `attendance` = the attendance sentence; `rank_choices` never alt;
    `pending` = characters waiting for an officer's confirm."""
    from oibot_gm.discord_registry import remove_record_consequence

    m, gone = member(w, 0), member(w, 1, cls="Mage", spec="Fire")
    w.reg.member_left(gone.discord_id)
    d = w.c.get("/api/members", headers=as_user(w, OFFICER)).json()
    assert d["rank_choices"] == ["trial", "raider", "core", "social"]
    rows = {r["uid"]: r for r in d["rows"]}
    waiting = sum(1 for r in d["rows"] if not r["test"] and not r["left"] for c in r["characters"] if c["status"] == "active" and c["name"] and not c["confirmed"])
    assert d["pending"] == waiting >= 1, "people in the server only: never a puppet's or a left member's character"
    here, left = rows[str(m.discord_id)], rows[str(gone.discord_id)]
    assert here["left"] is None and here["left_line"] is None and here["remove_consequence"] is None and here["test"] is False
    assert left["left"] == w.reg.local12(w.reg.members[gone.discord_id].left_at, "%a %d %b") and left["left_line"] == f"not in the server · noticed {left['left']}"
    assert left["remove_consequence"] == remove_record_consequence(w.reg, w.reg.members[gone.discord_id])
    assert here["attendance"] is None, "nothing counted yet: no line (the sentence comes once a finished run counts)"
    assert any(r["test"] for r in d["rows"]) and all(r["attendance"] is None for r in d["rows"] if r["test"])
    assert d["own"] == {"rank": "Another officer sets your rank.", "confirm": "Another officer confirms your characters."}
    w.c.cookies.clear()
    assert w.c.get("/api/members", headers=H).json()["own"] == {}, "the owner (here the MCP bearer) may rank and confirm their own"


def test_members_rows_carry_the_away_line(w):
    """Design §5.58 phase 2: the standing column's absence line comes from the server — the current absence as "away
    until <day>" (away_now), else the next one as "away <day> to <day>"; nothing when none is upcoming."""
    m, later, here = member(w, 0), member(w, 1, cls="Mage", spec="Fire"), member(w, 2, cls="Priest", spec="Holy")
    today = w.reg.now_local().date()
    w.reg.add_absence(m.discord_id, today.isoformat(), (today + timedelta(days=3)).isoformat(), None, "x")
    w.reg.add_absence(later.discord_id, (today + timedelta(days=5)).isoformat(), (today + timedelta(days=7)).isoformat(), None, "x")
    rows = {r["uid"]: r for r in w.c.get("/api/members", headers=as_user(w, OFFICER)).json()["rows"]}
    day = lambda n: w.reg.day_label((today + timedelta(days=n)).isoformat())  # noqa: E731
    assert rows[str(m.discord_id)]["away"] == f"away until {day(3)}" and rows[str(m.discord_id)]["away_now"] is True
    assert rows[str(later.discord_id)]["away"] == f"away {day(5)} to {day(7)}" and rows[str(later.discord_id)]["away_now"] is False
    assert rows[str(here.discord_id)]["away"] is None and rows[str(here.discord_id)]["away_now"] is False


def test_members_set_previews_the_generators_sentences_and_takes_a_rev(w):
    """THE one-member route (design §5.58): `preview` answers {sentences, rev} — exactly consequences.people_consequence,
    the words Discord's /roster char decide screen prints — and nothing is written; a write with a stale `rev` is a 409;
    retire, delete, spec (+ offspec) and flex go through the same one-row save."""
    from oibot_gm import consequences as cq

    ms = [member(w, i, cls="Mage", spec="Fire") for i in range(5)]
    ev = real_run(w.reg, w.rs)
    join(w.reg, w.rs, ev, ms)
    lock_with_board(w.reg, w.rs, ev, [m.display_name for m in ms])
    m = ms[1]
    w.reg.add_character(m.discord_id, m.display_name, "Reserva", "Rogue", "Combat", None, False)
    label = w.reg.members[m.discord_id].main.label
    h = as_user(w, OFFICER)
    p = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "retire": label, "preview": True}, headers=h)
    assert p.status_code == 200, p.text
    p = p.json()
    assert p["sentences"] == cq.retire_consequence(w.reg, w.rs, w.reg.members[m.discord_id], label, by_id=OFFICER)
    assert p["rev"] == w.reg.characters_rev(w.reg.members[m.discord_id]) and ev.seat_of(m)
    assert any("is released" in x for x in p["sentences"]) and p["sentences"][-1] == "It stays on the record as retired, so its history is kept."
    w.reg.save_character_table(m.discord_id, [{"label": "Reserva", "flex": ["tank"]}], "someone")  # changed meanwhile
    stale = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "retire": label, "rev": p["rev"]}, headers=h)
    assert stale.status_code == 409 and ev.seat_of(m)
    rev = w.reg.characters_rev(w.reg.members[m.discord_id])
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "retire": label, "rev": rev}, headers=h)
    assert r.status_code == 200, r.text
    assert ev.seat_of(m) is None and w.reg.members[m.discord_id].main.label == "Reserva"
    # spec + offspec, flex and delete, each previewed then written
    n = ms[2]
    lbl = w.reg.members[n.discord_id].main.label
    p = w.c.post("/api/members/set", json={"uid": str(n.discord_id), "spec": "Frost", "offspec": "Arcane", "preview": True}, headers=h).json()
    assert p["sentences"][0] == f"{n.display_name}'s character {lbl} switches to Frost, with Arcane as offspec."
    assert w.c.post("/api/members/set", json={"uid": str(n.discord_id), "spec": "Frost", "offspec": "Arcane"}, headers=h).status_code == 200
    assert (w.reg.members[n.discord_id].main.spec, w.reg.members[n.discord_id].main.offspec) == ("Frost", "Arcane")
    p = w.c.post("/api/members/set", json={"uid": str(n.discord_id), "flex": ["tank"], "preview": True}, headers=h).json()
    assert p["sentences"] == cq.flex_consequence(w.reg, w.rs, w.reg.members[n.discord_id], lbl, ["tank"], by_id=OFFICER)
    assert w.c.post("/api/members/set", json={"uid": str(n.discord_id), "flex": ["tank"]}, headers=h).status_code == 200
    assert w.reg.members[n.discord_id].main.flex == ["tank"]
    w.reg.add_character(n.discord_id, n.display_name, "Spareling", "Mage", "Fire", None, False)
    p = w.c.post("/api/members/set", json={"uid": str(n.discord_id), "delete": "Spareling", "preview": True}, headers=h).json()
    assert p["sentences"][0] == f"{n.display_name}'s character Spareling is deleted."
    assert w.c.post("/api/members/set", json={"uid": str(n.discord_id), "delete": "Spareling"}, headers=h).status_code == 200
    assert "Spareling" not in [c.label for c in w.reg.members[n.discord_id].characters]
    # a rank preview, the same rank refused in one sentence, never alt
    p = w.c.post("/api/members/set", json={"uid": str(n.discord_id), "rank": "core", "preview": True}, headers=h).json()
    assert p["sentences"] == [f"{n.display_name}'s rank goes from trial to core.", "It lives on their main and counts for who gets a seat from the next lock."]
    r = w.c.post("/api/members/set", json={"uid": str(n.discord_id), "rank": "trial", "preview": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == f"{n.display_name}'s rank is already trial."
    r = w.c.post("/api/members/set", json={"uid": str(n.discord_id), "rank": "alt"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "A rank is trial, raider, core or social." and w.reg.members[n.discord_id].rank == "trial"


def test_members_set_your_own_rank_and_confirm_say_another_officer_does_it(w):
    me = OFFICER
    w.reg.add_character(me, "Officer", "Kingsley", "Mage", "Fire", None, True)
    h = as_user(w, me)
    r = w.c.post("/api/members/set", json={"uid": str(me), "rank": "core", "preview": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "Another officer sets your rank."
    r = w.c.post("/api/members/set", json={"uid": str(me), "confirm": True, "preview": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "Another officer confirms your characters."
    p = w.c.post("/api/members/set", json={"uid": str(me), "spec": "Frost", "preview": True}, headers=h).json()
    assert p["sentences"][0] == "Your character Kingsley switches to Frost, with no offspec."  # your own record reads "your"


def test_members_dm_and_im_back_preview_the_sentences(w):
    from oibot_gm import consequences as cq

    m = member(w)
    h = as_user(w, OFFICER)
    p = w.c.post("/api/members/dm", json={"uid": str(m.discord_id), "on": False, "preview": True}, headers=h).json()
    assert p["sentences"] == cq.dm_consequence(w.reg, m, False, by_id=OFFICER) and not w.reg.members[m.discord_id].dm_opt_out
    assert p["sentences"][0] == f"{m.display_name}'s Confirm and fill asks wait on their Me page and the sheet."
    r = w.c.post("/api/members/dm", json={"uid": str(m.discord_id), "on": True, "preview": True}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == f"{m.display_name}'s DMs are already on."
    day = (w.reg.now_local().date() + timedelta(days=40)).isoformat()
    w.reg.add_absence(m.discord_id, day, None, None, "Officer")
    p = w.c.post("/api/members/absence/clear", json={"uid": str(m.discord_id), "start": day, "preview": True}, headers=h).json()
    assert p["sentences"] == [f"{m.display_name}'s absence on {w.reg.day_label(day)} is cleared.", cq.NO_SHEET_CHANGES, "Record it again if they're still away."]
    p = w.c.post("/api/me/absence/clear", json={"start": day, "preview": True}, headers=as_user(w, m.discord_id)).json()
    assert p["sentences"][0] == f"Your absence on {w.reg.day_label(day)} is cleared." and p["lines"] == []


def test_save_previews_answer_the_sentences(w):
    """POST /api/members/save and /api/me/characters previews carry `sentences` — the ripple as the confirm prints it
    (consequences.save_consequence) — per member on Members, flat on Me; [] when nothing beyond the record changes."""
    m = member(w)
    ev = real_run(w.reg, w.rs)
    join(w.reg, w.rs, ev, [m])
    label = m.main.label
    rows = [{"label": label, "cls": "Priest", "spec": "Shadow"}]
    p = w.c.post("/api/members/save", json={"preview": True, "rows": [{"uid": str(m.discord_id), "characters": rows}]}, headers=as_user(w, OFFICER)).json()
    (one,) = p["sentences"]
    assert one["uid"] == str(m.discord_id) and any("now reads Shadow" in x for x in one["sentences"])
    p = w.c.post("/api/me/characters", json={"rows": rows, "preview": True}, headers=as_user(w, m.discord_id)).json()
    assert p["sentences"] == one["sentences"]
    p = w.c.post("/api/me/characters", json={"rows": [{"label": label, "flex": ["tank"]}], "preview": True}, headers=as_user(w, m.discord_id)).json()
    assert p["sentences"] == []


def test_test_bench_tempos_are_the_runs_week_and_the_feed_names_its_run(w):
    """Findings 23 and 31: each tempo carries the run's week in the Rosters strip's shape (opens now, the reminder, the
    lock, confirm by, the raid — in words), and the feed's events carry the run's title, never its key."""
    from oibot_gm import testfeed

    h = as_user(w, OFFICER)
    d = w.c.get("/api/test", headers=h).json()
    std = next(t for t in d["tempos"] if t["id"] == "standard")
    assert [s["step"] for s in std["week"]] == ["signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "raid"]
    assert [s["when"] for s in std["week"]] == ["now", "in 8 min", "in 15 min", "in 25 min", "in 40 min"] and std["week"][0]["done"]
    ev = open_test_run(w.reg, w.rs)
    testfeed.FEED.record(w.reg.key, ev.key, "step", "Opened")
    feed = w.c.get("/api/test/feed", headers=h).json()
    e = next(x for x in feed["events"] if x["run"] == ev.key)
    assert e["title"] == f"{w.reg.raid_def(ev.instance).get('name', ev.instance)} · {w.reg.local12(ev.starts_at)}" and ev.key not in e["title"]
    assert feed["titles"][ev.key] == e["title"]


def test_members_save_refuses_only_the_stale_member(w):
    a, b = member(w, 0), member(w, 1, cls="Mage", spec="Fire")
    h = as_user(w, OFFICER)
    rows = {r["uid"]: r for r in w.c.get("/api/members", headers=h).json()["rows"]}
    w.reg.save_character_table(a.discord_id, [{"label": a.main.label, "cls": "Priest", "spec": "Discipline"}], a.display_name)  # a changes meanwhile
    r = w.c.post("/api/members/save", json={"rows": [
        {"uid": str(a.discord_id), "rev": rows[str(a.discord_id)]["characters_rev"], "characters": [{"label": a.main.label, "cls": "Priest", "spec": "Shadow"}]},
        {"uid": str(b.discord_id), "rev": rows[str(b.discord_id)]["characters_rev"], "characters": [{"label": b.main.label, "cls": "Mage", "spec": "Frost", "main": True, "rank": "raider"}]},
    ]}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["uid"] for x in d["refused"]] == [str(a.discord_id)] and d["refused"][0]["stale"]
    assert w.reg.members[a.discord_id].main.spec == "Discipline" and w.reg.members[b.discord_id].main.spec == "Frost"
    assert w.reg.members[b.discord_id].rank == "raider"


def test_members_set_rank_is_the_members_and_refuses_an_alt(w):
    m = member(w)
    w.reg.add_character(m.discord_id, m.display_name, "Altname", "Mage", "Fire", None, False)
    h = as_user(w, OFFICER)
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "rank": "core"}, headers=h)
    assert r.status_code == 200 and w.reg.members[m.discord_id].rank == "core"
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "character": "Altname", "rank": "raider"}, headers=h)
    assert r.status_code == 400 and "rank lives on the main" in r.json()["error"]
    r = w.c.post("/api/members/set", json={"member": m.display_name, "rank": "emperor"}, headers=H | {"X-Oibot-Actor": str(OFFICER)})  # MCP: the same refusal
    assert r.status_code == 400


def test_members_dm_and_me_dm_use_the_one_verb(w):
    m = member(w)
    h = as_user(w, OFFICER)
    assert w.c.post("/api/members/dm", json={"uid": str(m.discord_id), "on": False}, headers=h).json()["message"].endswith("DMs off")
    assert w.reg.members[m.discord_id].dm_opt_out
    h = as_user(w, m.discord_id)
    assert w.c.post("/api/me/dm", json={"on": True}, headers=h).status_code == 200 and not w.reg.members[m.discord_id].dm_opt_out


def test_seated_delete_from_the_members_page_frees_the_seat(w):
    ms = [member(w, i, cls="Mage", spec="Fire") for i in range(5)]
    ev = real_run(w.reg, w.rs)
    join(w.reg, w.rs, ev, ms)
    lock_with_board(w.reg, w.rs, ev, [m.display_name for m in ms])
    rc.run_team(w.reg, ev)["autofill"] = True
    m = ms[3]
    w.reg.add_character(m.discord_id, m.display_name, "Reserva", "Rogue", "Combat", None, False)
    h = as_user(w, OFFICER)
    body = {"rows": [{"uid": str(m.discord_id), "deletes": [m.main.label]}]}
    p = w.c.post("/api/members/save", json={**body, "preview": True}, headers=h).json()
    assert any("is released" in x for x in p["ripple"]) and ev.seat_of(m)
    assert w.c.post("/api/members/save", json=body, headers=h).status_code == 200
    assert ev.seat_of(m) is None and w.bot.fills


# ---------------------------------------------------------------- absences

def test_absence_from_and_for_how_long_with_preview(w):
    m = member(w)
    h = as_user(w, m.discord_id)
    meta = w.c.get("/api/meta", headers=h).json()
    assert meta["durations"][0] == {"days": 1, "label": "1 day"} and meta["absence_reason_max"] == ABSENCE_REASON_MAX
    start = (w.reg.now_local().date() + timedelta(days=5)).isoformat()
    p = w.c.post("/api/me/absence", json={"start": start, "days": 3, "preview": True}, headers=h).json()
    assert p["preview"] and p["end"] == (w.reg.now_local().date() + timedelta(days=7)).isoformat() and p["span"] and not w.reg.members[m.discord_id].absences
    assert w.c.post("/api/me/absence", json={"start": start, "days": 3, "reason": "trip"}, headers=h).status_code == 200
    r = w.c.post("/api/me/absence", json={"start": start, "days": 1, "preview": True}, headers=h)
    assert r.status_code == 400 and "already away" in r.json()["error"]
    r = w.c.post("/api/me/absence", json={"start": (w.reg.now_local().date() + timedelta(days=30)).isoformat(), "days": 1, "reason": "x" * 300}, headers=h)
    assert r.status_code == 400 and f"at most {ABSENCE_REASON_MAX}" in r.json()["error"]


def test_im_back_preview_then_clear_on_the_web(w):
    m = member(w)
    ev = real_run(w.reg, w.rs)
    day = rc.run_day(w.reg, ev)
    h = as_user(w, m.discord_id)
    assert w.c.post("/api/me/absence", json={"start": day, "days": 1}, headers=h).status_code == 200
    assert ev.signups[str(m.discord_id)].source == "absence"
    p = w.c.post("/api/me/absence/clear", json={"start": day, "preview": True}, headers=h).json()
    assert p["preview"] and "opens again for an answer" in p["lines"][0] and w.reg.members[m.discord_id].absences
    r = w.c.post("/api/me/absence/clear", json={"start": day}, headers=h)
    assert r.status_code == 200 and str(m.discord_id) not in ev.signups and not w.reg.members[m.discord_id].absences
    h = as_user(w, OFFICER)
    w.reg.add_absence(m.discord_id, day, None, "x", "Officer")
    r = w.c.post("/api/members/absence/clear", json={"uid": str(m.discord_id), "start": day}, headers=h)
    assert r.status_code == 200 and not w.reg.members[m.discord_id].absences


def test_absences_table_carries_the_span(w):
    m = member(w)
    start = (w.reg.now_local().date() + timedelta(days=2)).isoformat()
    w.reg.add_absence(m.discord_id, start, None, "why", "Officer")
    row = next(r for r in w.c.get("/api/absences", headers=as_user(w, OFFICER)).json()["rows"] if r["uid"] == str(m.discord_id))
    assert row["span"] == w.reg.span_label(start, start) and row["reason"] == "why" and row["by"] == "Officer"


# ---------------------------------------------------------------- the owner (design §2.1: Discord only)

def test_the_site_has_no_owner_transfer(w):
    """Owner transfer is /gm config owner in the server (BY_DESIGN_ONLY): the site has no route for it, for anyone."""
    m = member(w)
    for h in (as_user(w, OWNER), H):
        assert w.c.post("/api/owner", json={"uid": str(m.discord_id)}, headers=h).status_code in (404, 405)
    assert w.reg.config.owner_discord_id == OWNER


# ---------------------------------------------------------------- rank / confirm through THE character verb (review fixes)

def test_members_set_refuses_a_character_that_isnt_theirs(w):
    """A `character` that doesn't exist used to fall back to the main and set its rank without saying so."""
    m = member(w)
    h = as_user(w, OFFICER)
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "character": "Nobodyhere", "rank": "core"}, headers=h)
    assert r.status_code == 400 and "no active character named Nobodyhere" in r.json()["error"]
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "character": "Nobodyhere", "confirm": True}, headers=h)
    assert r.status_code == 400 and "no active character named Nobodyhere" in r.json()["error"]
    mm = w.reg.members[m.discord_id]
    assert mm.rank == "trial" and not mm.main.confirmed_by


def test_members_set_and_admin_confirm_are_one_table_save(w, monkeypatch):
    from oibot_gm import discord_people

    calls = []
    verb = discord_people.save_characters

    async def spy(*a, **kw):
        calls.append(a[3])
        return await verb(*a, **kw)
    monkeypatch.setattr(discord_people, "save_characters", spy)
    m = member(w)
    w.reg.add_character(m.discord_id, m.display_name, "Newmainy", "Mage", "Fire", None, False)
    h = as_user(w, OFFICER)
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "main": "Newmainy", "rank": "core", "confirm": True}, headers=h)
    assert r.status_code == 200, r.text
    assert calls == [[{"label": "Newmainy", "main": True}, {"label": "Newmainy", "rank": "core"}, {"label": "Newmainy", "confirm": True}]]
    mm = w.reg.members[m.discord_id]
    assert mm.main.label == "Newmainy" and mm.rank == "core" and mm.main.confirmed_by
    other = mm.characters[0] if mm.characters[0].label != "Newmainy" else mm.characters[1]
    r = w.c.post("/api/admin/confirm", json={"uid": str(m.discord_id), "label": other.label}, headers=h)
    assert r.status_code == 200 and r.json()["message"] == f"confirmed {other.label}"
    assert calls[-1] == [{"label": other.label, "confirm": True}]
    r = w.c.post("/api/admin/confirm", json={"uid": str(m.discord_id), "label": other.label}, headers=h)
    assert r.status_code == 400 and "already confirmed" in r.json()["error"]
    audits = [t for _lvl, t in w.bot.ops.lines if "Newmainy" in t]
    assert len(audits) == 1, f"one audit line for the one save: {w.bot.ops.lines}"


def test_admin_confirm_refuses_a_shared_planned_label(w):
    w.reg.set_plan(9_100_000_000_000_001, "Plannerone", "Priest", "Holy", None, "main")
    w.reg.set_plan(9_100_000_000_000_002, "Plannertwo", "Priest", "Holy", None, "main")
    h = as_user(w, OFFICER)
    r = w.c.post("/api/admin/confirm", json={"label": "Priest (Holy)"}, headers=h)
    assert r.status_code == 400 and "2 members plan" in r.json()["error"]


# ---------------------------------------------------------------- review fixes (the C fixer)

def test_member_get_hides_another_officers_puppet(w):
    """GET /api/member applies the same puppet filter as /api/members and /api/absences."""
    p = w.reg.test_members()[0]
    h = as_user(w, OFFICER)
    assert w.c.get(f"/api/member?name={p.discord_id}", headers=h).status_code == 200  # no rehearsal live: any officer
    open_test_run(w.reg, w.rs, test_by=OWNER)
    r = w.c.get(f"/api/member?name={p.discord_id}", headers=h)
    assert r.status_code == 400 and r.json()["error"] == "unknown member"
    assert w.c.get(f"/api/member?name={p.discord_id}", headers=H).status_code == 200  # the owner's token: the owner may change it


def test_mcp_rank_and_confirm_need_the_actor_header(w):
    """D8: from MCP a rank or a confirmation needs X-Oibot-Actor naming an officer — /api/members/set, /api/members/save
    (checked before any member saves) and /api/admin/confirm; previews and the rest of a table don't."""
    m, n = member(w, 0), member(w, 1, cls="Mage", spec="Fire")
    n_label = n.main.label
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "rank": "core"}, headers=H)
    assert r.status_code == 403 and "X-Oibot-Actor" in r.json()["error"] and w.reg.members[m.discord_id].rank == "trial"
    r = w.c.post("/api/admin/confirm", json={"uid": str(m.discord_id), "label": m.main.label}, headers=H)
    assert r.status_code == 403 and not w.reg.members[m.discord_id].main.confirmed_by
    r = w.c.post("/api/members/save", json={"rows": [{"uid": str(n.discord_id), "characters": [{"label": n_label, "cls": "Mage", "spec": "Frost"}]},
                                                     {"uid": str(m.discord_id), "characters": [{"label": m.main.label, "cls": "Priest", "spec": "Holy", "rank": "core"}]}]}, headers=H)
    assert r.status_code == 403 and w.reg.members[n.discord_id].main.spec == "Fire", "refused before the first member saved"
    r = w.c.post("/api/members/save", json={"preview": True, "rows": [{"uid": str(m.discord_id), "characters": [{"label": m.main.label, "cls": "Priest", "spec": "Holy", "rank": "core"}]}]}, headers=H)
    assert r.status_code == 200 and r.json()["preview"]
    r = w.c.post("/api/members/save", json={"rows": [{"uid": str(n.discord_id), "characters": [{"label": n_label, "cls": "Mage", "spec": "Frost"}]}]}, headers=H)
    assert r.status_code == 200 and w.reg.members[n.discord_id].main.spec == "Frost"  # a respec isn't a decision about a person
    actor = H | {"X-Oibot-Actor": str(OFFICER)}
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "rank": "core", "confirm": True}, headers=actor)
    assert r.status_code == 200, r.text
    mm = w.reg.members[m.discord_id]
    assert mm.rank == "core" and mm.main.confirmed_by
    assert any("[mcp]" in t and "rank core" in t for _lvl, t in w.bot.ops.lines)
    r = w.c.post("/api/members/set", json={"uid": str(m.discord_id), "rank": "raider"}, headers=H | {"X-Oibot-Actor": str(m.discord_id)})
    assert r.status_code == 403 and "not an officer" in r.json()["error"]  # the header never widens what the token may do


def test_mcp_plain_change_refuses_a_rank_without_the_actor(w, monkeypatch):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp, ConfigRequest

    m = member(w)
    w.bot.ctx = types.SimpleNamespace(provider=object())
    monkeypatch.setattr(configops, "parse", lambda *a, **k: ConfigRequest(kind="change", reply="", ops=[
        ConfigOp(op="rank", member=m.display_name, value="core"), ConfigOp(op="dm", member=m.display_name, value="off")]))
    d = w.c.post("/api/ops/change", json={"text": "make them core and DMs off"}, headers=H).json()
    assert "X-Oibot-Actor" in str(d) and "rank" not in " ".join(d.get("describe") or d.get("lines") or []).lower()
    d2 = w.c.post("/api/ops/change", json={"token": d["token"], "apply": True}, headers=H).json()
    assert w.reg.members[m.discord_id].rank == "trial" and w.reg.members[m.discord_id].dm_opt_out, d2


def test_mcp_member_tools_send_the_actor_only_for_a_rank_or_a_confirm(monkeypatch):
    import httpx

    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("x-oibot-actor")))
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        monkeypatch.setenv(ms.ACTOR_ENV, str(OFFICER))
        ms.set_member("Kessa", main="Kessalt")
        ms.set_member("Kessa", rank="core")
        ms.set_member("Kessa", confirm=True)
        ms.save_characters("Kessa", [{"label": "Kessa", "cls": "Priest", "spec": "Shadow"}])
        ms.save_characters("Kessa", [{"label": "Kessa", "cls": "Priest", "spec": "Holy", "rank": "core"}])
    finally:
        ms.use(None)
    assert [a for _p, a in calls] == [None, str(OFFICER), str(OFFICER), None, str(OFFICER)]


def test_no_free_typed_date_or_time_field_on_the_site():
    """The clock rule: a person picks a date or time from a widget, never types one for a parser — Mantine's DateInput
    and TimeInput are free-typed, so the site uses DatePickerInput / DateTimePicker (12-hour) instead."""
    import re as _re
    from pathlib import Path as _P
    src = _P(__file__).resolve().parents[1] / "frontend" / "src"
    typed = [f"{p.relative_to(src)}" for p in src.rglob("*.tsx")
             if _re.search(r"\b(DateInput|TimeInput)\b", p.read_text()) or 'type="date' in p.read_text() or 'type="time' in p.read_text()]
    assert not typed, f"free-typed date / time fields: {typed}"
    absences = (src / "components" / "AbsencesModal.tsx").read_text()
    assert "DatePickerInput" in absences and "withinPortal: false" in absences, "picked in the modal, its popover kept inside it"


def test_mcp_application_decisions_need_the_actor_header(w):
    """Regression (final review, D8): the bare MCP token accepted or declined an application — the applicant's DM and
    the audit line read as the owner, who never decided. Now POST /api/admin/application/decide and the `application`
    op through /api/ops/change refuse it without X-Oibot-Actor; MCP decide_application sends the actor."""
    import inspect

    from oibot_gm import mcp_server as ms

    decided = []

    async def decide_application(reg, uid, accept, by, note=None, by_id=None, via="discord"):
        decided.append((uid, accept, by, via))
        reg.decide(uid, accept, by, note)
        return f"application {'accepted' if accept else 'declined'}"
    w.bot.decide_application = decide_application
    spec = next(iter(w.reg.profile.classes["Priest"]))
    w.reg.apply(500_000_000_000_000_011, "discAna", "Ana", "Priest", spec, None, None, None, None)
    r = w.c.post("/api/admin/application/decide", json={"uid": "500000000000000011", "accept": False}, headers=H)
    assert r.status_code == 403 and "X-Oibot-Actor" in r.json()["error"] and not decided
    r = w.c.post("/api/ops/change", json={"ops": [{"op": "application", "target": "500000000000000011", "value": "decline"}], "apply": True,
                                          "confirmed": True}, headers=H)
    assert not r.json().get("applied") and any("application decision" in x for x in r.json().get("refused", [])), r.text
    assert not decided and w.reg.applicants[500_000_000_000_000_011].status == "open"
    r = w.c.post("/api/admin/application/decide", json={"uid": "500000000000000011", "accept": False}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200, r.text
    assert decided and decided[0][3] == "mcp"
    assert "actor=True" in inspect.getsource(ms.decide_application)


# ---------------------------------------------------------------- a roster from a signup list (design §5.53)

def test_members_import_is_the_officers_previews_and_needs_the_actor_for_a_rank(w):
    imp = 310_000_000_000_000_001
    rows = [{"discord_id": str(imp), "name": "Ash", "character": "Ashgrove", "cls": "Priest", "spec": "Holy"}]
    assert w.c.post("/api/members/import", json={"rows": rows}, headers=as_user(w, member(w, 0).discord_id)).status_code == 403, "officers only"
    h = as_user(w, OFFICER)
    r = w.c.post("/api/members/import", json={"rows": rows, "preview": True}, headers=h)
    assert r.status_code == 200 and r.json()["preview"] and imp not in w.reg.members and r.json()["message"].startswith("preview")
    r = w.c.post("/api/members/import", json={"rows": rows}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == ["Ash: Ashgrove (Priest Holy, trial)"] and w.reg.members[imp].main.name == "Ashgrove" and w.reg.members[imp].rank == "trial"
    assert r.json()["message"].startswith("1 member(s) imported from a signup list")
    r = w.c.post("/api/members/import", json={"rows": rows}, headers=h)
    assert r.status_code == 200 and r.json()["added"] == [] and r.json()["skipped"][0]["why"] == "already registered as Ashgrove"
    assert w.c.post("/api/members/import", json={"rows": []}, headers=h).status_code == 400
    assert w.c.post("/api/members/import", json={"rows": "x"}, headers=h).status_code == 400
    ranked = [{"discord_id": str(imp + 1), "name": "Bram", "character": "Bramblewick", "cls": "Warrior", "spec": "Protection", "rank": "raider"}]
    r = w.c.post("/api/members/import", json={"rows": ranked}, headers=H)  # MCP's bearer alone: a rank is a person's decision (D8)
    assert r.status_code == 403 and imp + 1 not in w.reg.members
    assert w.c.post("/api/members/import", json={"rows": ranked, "preview": True}, headers=H).status_code == 200, "a preview needs no actor"
    r = w.c.post("/api/members/import", json={"rows": ranked}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200 and w.reg.members[imp + 1].rank == "raider"
