"""Every Discord link to an event opens that event's page (phase 5, Task 10; the owner, 2026-11-25: "ensure deep
linking from discord (ex signup sheet title) correctly leads to the event page"). ONE builder, raid_views.event_url;
the sheet's title and every officer card link through it; the login round trip keeps the address."""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import discord
from conftest import open_test_run
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_mcp import FakeBot
from test_raid_night import locked_ten

from oibot_gm import raid_views
from oibot_gm import raidcycle as rc
from oibot_gm.web import app as web

SRC = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
WEB = "https://gm.example"
RID = "barrow_deeps"


def ico(*_a, **_k) -> str:
    return ""


def links(view) -> list[str]:
    return [b.url for b in view.walk_children() if isinstance(b, discord.ui.Button) and b.url]


def lineup_run(reg, rs):
    reg.set_raid_overrides("hyjal_summit_forever", {"first_open": "2026-01-01T00:00"}, "t")
    reg.set_raid_overrides(RID, {"first_open": "2026-01-01T00:00"}, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(hour=18, minute=30, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t", lineup=[{"raid": RID, "rosters": 1}])
    return ev, rc.event_parts(rs, ev)[1]


def test_event_url_is_the_first_raids_event_page_in_this_guild(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_WEB_URL", WEB)
    ev, later = lineup_run(reg, rs)
    want = f"{WEB}/app/events/{ev.key}?g={reg.key}"
    assert raid_views.event_url(reg, ev) == want
    assert raid_views.event_url(reg, later) == want  # a later raid's link opens the evening it belongs to
    monkeypatch.setenv("OIBOT_WEB_URL", "")
    assert raid_views.event_url(reg, ev) is None  # no public site address: no link at all


def test_the_sheet_title_opens_the_event_in_every_state(reg, rs, monkeypatch):
    """The title was the member's Me row before; now the event page for everyone (a member is sent on to their row by
    the page). A finished or cancelled sheet links too: the officer lands on its History view."""
    monkeypatch.setenv("OIBOT_WEB_URL", WEB)
    ev = open_test_run(reg, rs)
    team = rc.run_team(reg, ev)
    for state in ("open", "locked", "done", "cancelled"):
        ev.state = state
        embed, _view = raid_views.sheet_message(reg, ev, team, ico)
        assert embed.url == raid_views.event_url(reg, ev), state


def test_every_officer_card_links_the_event(reg, rs, monkeypatch):
    monkeypatch.setenv("OIBOT_WEB_URL", WEB)
    ev = open_test_run(reg, rs)
    team = rc.run_team(reg, ev)
    want = raid_views.event_url(reg, ev)
    assert links(raid_views.health_layout(reg, rs, ev, team, ico)) == [want]
    locked, _seated = locked_ten(reg, rs, days=6)
    team = rc.run_team(reg, locked)
    want = raid_views.event_url(reg, locked)
    assert links(raid_views.lock_layout(reg, locked, team, ico, 0, rs=rs)) == [want]
    assert links(raid_views.health_locked_brief(reg, locked, ico, officer=True)) == [want]  # /raid health on a locked run
    for state in ("done", "cancelled"):
        locked.state = state
        assert links(raid_views.closed_layout(reg, locked, ico)) == [want], state


def test_no_discord_module_builds_an_event_address_by_hand():
    """One builder: an event's address (/app/events/<key>, or the older /app/me?g=…#run-<key>) appears only inside
    raid_views.event_url. The site's own modules (web/) are not Discord surfaces."""
    found = []
    for f in sorted(SRC.rglob("*.py")):
        if "web" in f.relative_to(SRC).parts:
            continue
        text = f.read_text()
        if f.relative_to(SRC).as_posix() == "raid_views.py":
            start = text.index("def event_url(")
            end = text.index("\ndef ", start + 1)
            text = text[:start] + text[end:]
        for n, line in enumerate(text.splitlines(), 1):
            if "/app/events/" in line or ("/app/me?g=" in line and "#run-" in line):
                found.append(f"{f.relative_to(SRC)}:{n}")
    assert not found, found
    assert not hasattr(raid_views, "me_url") and not hasattr(raid_views, "board_url")


def test_the_login_round_trip_keeps_the_event_address(reg, rs, monkeypatch):
    """Not logged in: /app/events/<key>?g=<guild> goes to the Discord login with that address as `next`, the signed
    state carries it whole, and the login's last step lands back on it, key and ?g= included."""
    monkeypatch.setenv("OIBOT_WEB_SECRET", "s" * 40)
    monkeypatch.setenv("DISCORD_CLIENT_ID", "123")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "x")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    c = TestClient(web.create_app(FakeBot(reg, rs)), follow_redirects=False)
    here = f"/app/events/bd-abc234?g={reg.key}"
    r = c.get(here)
    assert r.status_code in (302, 307), r.status_code
    login = urlparse(r.headers["location"])
    assert login.path == "/auth/login" and unquote(parse_qs(login.query)["next"][0]) == here
    d = c.get(r.headers["location"], headers={"user-agent": "Mozilla/5.0 (Macintosh)"})
    state = parse_qs(urlparse(d.headers["location"]).query)["state"][0]
    sign = URLSafeSerializer("s" * 40, salt="session")
    assert sign.loads(state)["next"] == here
    import time

    ticket = sign.dumps({"uid": "42", "name": "Tester", "next": sign.loads(state)["next"], "t": time.time(), "k": "confirm"})
    done = c.post("/auth/confirm", data={"ticket": ticket})
    assert done.status_code == 303 and done.headers["location"] == here


def test_old_me_links_still_scroll_to_the_row():
    """Sheets posted before this change link /app/me?g=…#run-<key>: the Me page still scrolls to that row."""
    me = (SRC.parents[1] / "frontend" / "src" / "pages" / "Me.tsx").read_text()
    row = (SRC.parents[1] / "frontend" / "src" / "components" / "MyAnswer.tsx").read_text()  # the row the page draws
    assert 'id={`run-${s.key}`}' in row and re.search(r'hash\.startsWith\("#run-"\)', me)
