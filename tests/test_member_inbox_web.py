"""The member inbox and the officers' on-behalf routes over HTTP (web/api_me.py, design §5.40): GET /api/me's asks and
notices, a member's own answers (POST /api/me/run/{key}/answer|out, /api/me/fill/{key}, /api/me/ask/{id}) through the
SAME RaidMixin verbs as the sheet and the DM cards, member vs officer, an ask answered after its seat was freed (refused
from one place), two devices (last press wins, each reply the receipt of the state then), the on-behalf list and its rev
(GET /api/admin/actions, POST /api/admin/action), the MCP tool shapes and the run_answer re-check at apply."""
from __future__ import annotations

import asyncio
import json
import time
import types

import httpx
import pytest
from conftest import OFFICER_ROLE, OWNER, MemberDirectory, by_role, join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_raid_night import NightBot

from oibot_gm import configops
from oibot_gm import mcp_server as ms
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError
from oibot_gm.web import app as web

TOKEN = "a" * 64
SECRET = "s" * 40
OFFICER = 300_000_000_000_000_005
OUTSIDER = 300_000_000_000_000_009


def person(uid, *roles):
    return types.SimpleNamespace(id=uid, display_name=f"u{str(uid)[-2:]}", roles=[types.SimpleNamespace(id=r) for r in roles])


class WebNightBot(NightBot):
    """The real RaidMixin verbs (NightBot: DM cards that can be edited) behind the real web app, with a member directory:
    the puppets and an officer are in the server; OUTSIDER is not."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.admit()

    def admit(self) -> None:
        reg = self.reg if hasattr(self, "reg") else next(iter(self.registries.ordered()))
        people = {(1, OFFICER): person(OFFICER, OFFICER_ROLE), (1, OWNER): person(OWNER)}
        for m in reg.test_members():
            people[(1, m.discord_id)] = person(m.discord_id)
        MemberDirectory(people).install(self, servers=(1,))

    def officiates(self, member, guild):
        return any(r.id == OFFICER_ROLE for r in getattr(member, "roles", []))


@pytest.fixture
def w(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    bot = WebNightBot(reg, rs)
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, rs=rs)


def as_user(c, uid) -> dict:
    c.cookies.clear()
    c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


def locked_with_asks(reg, rs, n=10):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()[:n]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool])
    for m in pool:
        reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    return ev, pool


# ---------------------------------------------------------------- GET /api/me: asks and notices

def test_me_lists_my_asks_and_notices_and_marks_them_read(w):
    ev, pool = locked_with_asks(w.reg, w.rs)
    m = pool[0]
    w.reg.notify(m.discord_id, "released", "Something happened.", run=ev.key)
    h = as_user(w.c, m.discord_id)
    d = w.c.get("/api/me", headers=h).json()
    (a,) = d["asks"]
    assert a["kind"] == "confirm" and a["run"] == ev.key and a["group"]["members"] and a["deadline"]
    assert [n["text"] for n in d["notices"]] == ["Something happened."] and not d["notices"][0]["read"]
    assert d["dm_sentence"].startswith("With DMs on")
    (row,) = [s for s in d["sheets"] if s["key"] == ev.key]
    assert row["may_cant"] and not row["may_answer"] and "rostered" in row["receipt"]
    r = w.c.post("/api/me/notices/read", json={}, headers=h)
    assert r.status_code == 200 and all(n["read"] for n in r.json()["notices"])


# ---------------------------------------------------------------- my own answers, through the sheet's verbs

def test_member_answers_their_own_sheet_and_gets_the_receipt(w):
    ev = open_test_run(w.reg, w.rs)
    m = w.reg.test_members()[0]
    h = as_user(w.c, m.discord_id)
    r = w.c.post(f"/api/me/run/{ev.key}/answer", json={"status": "in"}, headers=h)
    assert r.status_code == 200, r.text
    assert "joined" in r.json()["message"] and ev.signups[str(m.discord_id)].status == "in"
    assert not w.reg.notices(m.discord_id), "my own answer is no notice"
    assert w.c.post(f"/api/me/run/{ev.key}/answer", json={"status": "maybe"}, headers=h).status_code == 400


def test_two_devices_last_press_wins_and_each_reply_is_the_state_then(w):
    ev = open_test_run(w.reg, w.rs)
    m = w.reg.test_members()[0]
    h = as_user(w.c, m.discord_id)
    a = w.c.post(f"/api/me/run/{ev.key}/answer", json={"status": "in"}, headers=h).json()["message"]
    b = w.c.post(f"/api/me/run/{ev.key}/answer", json={"status": "sub"}, headers=h).json()["message"]
    assert "joined" in a and "Bench" in b and ev.signups[str(m.discord_id)].status == "sub"


def test_my_join_after_lock_is_refused_and_cant_make_it_frees_my_seat(w):
    ev, pool = locked_with_asks(w.reg, w.rs)
    m = pool[1]
    h = as_user(w.c, m.discord_id)
    r = w.c.post(f"/api/me/run/{ev.key}/answer", json={"status": "in"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == rc.LOCKED_JOIN
    r = w.c.post(f"/api/me/run/{ev.key}/out", json={"note": "flight delayed"}, headers=h)
    assert r.status_code == 200, r.text
    assert not ev.seat_of(m) and ev.callouts[-1].note == "flight delayed" and ev.callouts[-1].display_name == m.display_name
    assert not w.reg.notices(m.discord_id) or all("recorded that you" not in n["text"] for n in w.reg.notices(m.discord_id)), "their own callout is not 'for them'"


def test_confirm_on_the_site_and_a_second_answer_is_refused_from_one_place(w):
    ev, pool = locked_with_asks(w.reg, w.rs)
    m = pool[2]
    h = as_user(w.c, m.discord_id)
    r = w.c.post(f"/api/me/ask/{ev.key}", json={"answer": "yes"}, headers=h)
    assert r.status_code == 200 and "confirmed" in r.json()["message"]
    r = w.c.post(f"/api/me/ask/{ev.key}", json={"answer": "no"}, headers=h)
    assert r.status_code == 400 and r.json()["error"].startswith("Already answered (yes")


def test_a_web_ask_answered_after_the_seat_was_freed_is_refused(w):
    ev, pool = locked_with_asks(w.reg, w.rs)
    m = pool[3]
    rc.free_seat(w.reg, w.rs, ev, m, "board")  # an officer's board edit took the seat meanwhile
    h = as_user(w.c, m.discord_id)
    r = w.c.post(f"/api/me/ask/{ev.key}", json={"answer": "yes"}, headers=h)
    assert r.status_code == 400 and "the seat was released" in r.json()["error"]
    assert not ev.seat_of(m), "a late yes never seats them again"
    assert not [a for a in w.c.get("/api/me", headers=h).json()["asks"] if a["kind"] == "confirm"]


def test_my_fill_ask_on_the_site(w):
    w.reg.seed_test_members(24, "t")
    w.bot.admit()  # the new puppets are in the server too
    ev = open_test_run(w.reg, w.rs)
    roles = by_role(w.reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]
    join(w.reg, w.rs, ev, seated)
    lock_with_board(w.reg, w.rs, ev, [m.display_name for m in seated])
    rc.free_seat(w.reg, w.rs, ev, seated[0], "declined")
    team = rc.run_team(w.reg, ev)
    first = rc.fill_batch(w.reg, w.rs, ev, team)[0]
    w.reg.members[first.discord_id].dm_opt_out = True
    asyncio.run(w.bot.run_fill(w.reg, w.rs, ev, team, by="t"))
    h = as_user(w.c, first.discord_id)
    (a,) = [x for x in w.c.get("/api/me", headers=h).json()["asks"] if x["kind"] in ("fill", "swap")]
    assert a["channel"] == "web"
    r = w.c.post(f"/api/me/fill/{ev.key}", json={"answer": "yes"}, headers=h)
    assert r.status_code == 200, r.text
    assert ev.seat_of(w.reg.members[first.discord_id])
    r = w.c.post(f"/api/me/fill/{ev.key}", json={"answer": "yes"}, headers=h)
    assert r.status_code == 400, "nothing open any more"


def test_member_routes_need_a_registered_member_and_officer_routes_an_officer(w):
    ev = open_test_run(w.reg, w.rs)
    m = w.reg.test_members()[0]
    h = as_user(w.c, OFFICER)
    assert w.c.post(f"/api/me/run/{ev.key}/answer", json={"status": "in"}, headers=h).status_code == 400, "not registered"
    h = as_user(w.c, m.discord_id)
    assert w.c.get(f"/api/admin/actions?run={ev.key}&member={m.discord_id}", headers=h).status_code == 403
    assert w.c.post("/api/admin/action", json={"run": ev.key, "member": str(m.discord_id), "action": "join"}, headers=h).status_code == 403
    assert w.c.get(f"/api/admin/inbox?member={m.discord_id}", headers=h).status_code == 403


# ---------------------------------------------------------------- officers on behalf

def test_officer_actions_list_and_record_with_rev(w):
    ev = open_test_run(w.reg, w.rs)
    m = w.reg.test_members()[0]
    h = as_user(w.c, OFFICER)
    d = w.c.get(f"/api/admin/actions?run={ev.key}&member={m.discord_id}", headers=h).json()
    assert [a["key"] for a in d["actions"]] == ["callout", "join", "bench", "no_thanks"] and d["now"] == "no answer yet"
    assert all(a["consequence"] for a in d["actions"])
    join(w.reg, w.rs, ev, [m], "sub")  # the member answers while the officer's menu is open
    r = w.c.post("/api/admin/action", json={"run": ev.key, "member": str(m.discord_id), "action": "join", "rev": d["rev"]}, headers=h)
    assert r.status_code == 409 and "is now Bench" in r.json()["error"] and ev.signups[str(m.discord_id)].status == "sub"
    d = w.c.get(f"/api/admin/actions?run={ev.key}&member={m.discord_id}", headers=h).json()
    r = w.c.post("/api/admin/action", json={"run": ev.key, "member": str(m.discord_id), "action": "callout", "note": "told me on vent", "rev": d["rev"]}, headers=h)
    assert r.status_code == 200, r.text
    assert ev.signups[str(m.discord_id)].status == "out" and ev.callouts[-1].note == "told me on vent"
    assert any("recorded that you can't make it" in n["text"] for n in w.reg.notices(m.discord_id))
    inbox_d = w.c.get(f"/api/admin/inbox?member={m.discord_id}", headers=h).json()
    assert inbox_d["notices"] and inbox_d["asks"] == []


def test_officer_confirms_for_a_member_and_their_dm_card_is_edited(w):
    ev, pool = locked_with_asks(w.reg, w.rs)
    m = pool[4]
    h = as_user(w.c, OFFICER)
    r = w.c.post("/api/admin/action", json={"run": ev.key, "member": str(m.discord_id), "action": "confirm_yes"}, headers=h)
    assert r.status_code == 200, r.text
    ask = next(a for a in w.reg.members[m.discord_id].placement_asks if a["roster"] == ev.team)
    assert ask["answer"] == "yes"
    assert any("confirmed your seat" in n["text"] for n in w.reg.notices(m.discord_id))


# ---------------------------------------------------------------- MCP: set_answer's callout flag, member_inbox

@pytest.fixture
def mocked():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read() or b"{}") if request.method != "GET" else dict(request.url.params)
        calls.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"message": "ok", "asks": [], "notices": []})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    yield calls
    ms.use(None)


def test_mcp_set_answer_callout_flag_and_member_inbox(mocked):
    ms.set_answer("r1", "Kestrel", "out")
    ms.set_answer("r1", "Kestrel", "out", callout=False)
    ms.set_answer("r1", "Kestrel", "out", note="flight")
    bodies = [b for _m, p, b in mocked if p == "/api/run/r1/set"]
    assert bodies[0].get("callout") is True, "out defaults to Can't make it (a callout)"
    assert "callout" not in bodies[1], "callout=False is a plain No thanks"
    assert bodies[2].get("note") == "flight" and bodies[2].get("callout") is True
    with pytest.raises(Exception, match="callout"):
        ms.set_answer("r1", "Kestrel", "out", callout=False, note="x")
    ms.member_inbox("Kestrel")
    assert mocked[-1] == ("GET", "/api/admin/inbox", {"member": "Kestrel"})


# ---------------------------------------------------------------- run_answer re-checks the lock at apply (B1)

def test_plain_run_answer_rechecks_the_lock_at_apply(w):
    ev = open_test_run(w.reg, w.rs)
    m = w.reg.test_members()[0]
    op = ConfigOp(op="run_answer", target=ev.key, member=m.display_name, value="in")
    lock_with_board(w.reg, w.rs, ev, [])  # previewed while open, Applied after the lock
    with pytest.raises(RegistryError, match="locked"):
        asyncio.run(configops.apply_async(w.reg, op, m.display_name, is_owner=False, bot=w.bot, by_id=m.discord_id))
    assert str(m.discord_id) not in ev.signups or ev.signups[str(m.discord_id)].status != "in"
