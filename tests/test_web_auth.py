"""Session and redirect rules of the web login (pure helpers of web/app.py)."""
import time

from oibot_gm.web import app as web


def test_safe_next_only_inside_the_app():
    assert web.safe_next("/app/rosters") == "/app/rosters"
    assert web.safe_next("/app") == "/app"
    for bad in (None, "", "/", "/api/me", "https://evil.example/app", "//evil.example", "/app//evil", "/app\\evil"):
        assert web.safe_next(bad) == "/app/me", bad


def test_session_valid_age_and_epoch(monkeypatch):
    now = time.time()
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    assert web.session_valid({"uid": "1", "iat": now - 60}, now)
    assert not web.session_valid({"uid": "1"}, now), "pre-hardening cookies have no iat"
    assert not web.session_valid({"uid": "1", "iat": now - web.SESSION_MAX_AGE_S - 1}, now)
    assert not web.session_valid({"uid": "1", "iat": now + 3600}, now), "issued in the future"
    assert not web.session_valid(None, now) and not web.session_valid({"iat": now}, now)
    monkeypatch.setenv("OIBOT_WEB_SESSION_EPOCH", str(int(now - 30)))
    assert not web.session_valid({"uid": "1", "iat": now - 60}, now), "issued before the revocation epoch"
    assert web.session_valid({"uid": "1", "iat": now - 10}, now)
    monkeypatch.setenv("OIBOT_WEB_SESSION_EPOCH", "2099-01-01T00:00:00+00:00")
    assert not web.session_valid({"uid": "1", "iat": now}, now)
    monkeypatch.setenv("OIBOT_WEB_SESSION_EPOCH", "not a date")
    assert web.session_epoch() == 0.0



def test_login_finished_in_another_browser_asks_to_continue(reg, rs, monkeypatch):
    """Phones: the OAuth callback can land in a browser without the nonce cookie. The callback then offers
    'Continue as <name>' and POST /auth/confirm sets the session; an old, forged or wrong-kind ticket is refused."""
    import time

    from fastapi.testclient import TestClient
    from itsdangerous import URLSafeSerializer
    from test_mcp import FakeBot

    monkeypatch.setenv("OIBOT_WEB_SECRET", "s" * 40)
    sign = URLSafeSerializer("s" * 40, salt="session")
    c = TestClient(web.create_app(FakeBot(reg, rs)), follow_redirects=False)
    good = sign.dumps({"uid": "42", "name": "Tester", "next": "/app/rosters", "t": time.time(), "k": "confirm"})
    r = c.post("/auth/confirm", data={"ticket": good})
    assert r.status_code == 303 and r.headers["location"] == "/app/rosters" and web.COOKIE in r.headers.get("set-cookie", "")
    assert c.post("/auth/confirm", data={"ticket": "forged"}).status_code == 400
    old = sign.dumps({"uid": "42", "name": "Tester", "next": "/app", "t": time.time() - web.CONFIRM_TTL_S - 5, "k": "confirm"})
    assert c.post("/auth/confirm", data={"ticket": old}).status_code == 400
    session_like = sign.dumps({"uid": "42", "name": "Tester", "iat": time.time()})  # a session cookie is not a ticket
    assert c.post("/auth/confirm", data={"ticket": session_like}).status_code == 400
    assert "Continue as" in web.CONFIRM_PAGE and "{name}" in web.CONFIRM_PAGE


def test_mobile_login_is_a_tappable_link_desktop_is_a_redirect(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_mcp import FakeBot

    monkeypatch.setenv("DISCORD_CLIENT_ID", "123"); monkeypatch.setenv("DISCORD_CLIENT_SECRET", "x")
    c = TestClient(web.create_app(FakeBot(reg, rs)), follow_redirects=False)
    d = c.get("/auth/login", headers={"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"})
    assert d.status_code in (302, 307) and "discord.com" in d.headers["location"] and web.NONCE_COOKIE in d.headers.get("set-cookie", "")
    m = c.get("/auth/login", headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148"})
    assert m.status_code == 200 and "Continue with Discord" in m.text and "discord.com" in m.text and web.NONCE_COOKIE in m.headers.get("set-cookie", "")


def test_opening_a_run_refuses_the_past_and_explains_a_missing_slot(reg, rs, monkeypatch):
    """The web opens a run from a picked time; a mistyped past date and a raid with no slots both get a sentence
    that names the fix, not a parser complaint (CLAUDE.md: no typed timestamps, no military time)."""
    from fastapi.testclient import TestClient
    from test_mcp import FakeBot, TOKEN, bearer

    monkeypatch.setenv("OIBOT_MCP_TOKEN", TOKEN)
    c = TestClient(web.create_app(FakeBot(reg, rs)), follow_redirects=False)
    rid = next(iter(reg.profile.raids))
    h = {**bearer(), "X-Requested-With": "oibot"}
    past = c.post(f"/api/raid/{rid}/open", json={"when": "2020-01-02 19:30"}, headers=h)
    assert past.status_code == 400 and "in the past" in past.json()["error"]
    assert "PM" in past.json()["error"] and ":30" in past.json()["error"]  # 12-hour, never "19:30"
    junk = c.post(f"/api/raid/{rid}/open", json={"when": "next tuesday"}, headers=h)
    assert junk.status_code == 400 and "picker" in junk.json()["error"]
    reg.set_raid_override(rid, "slots", "", "t")
    none = c.post(f"/api/raid/{rid}/open", json={"when": ""}, headers=h)
    assert none.status_code == 400 and "no recurring slots yet" in none.json()["error"] and "Pick a date and time" in none.json()["error"]
    # slots exist but every occurrence is before the raid opens: say THAT, not "no slots"
    reg.set_raid_override(rid, "slots", "Tue 19:30", "t")
    reg.set_raid_override(rid, "first_open", "2030-01-01T15:00", "t")
    early = c.post(f"/api/raid/{rid}/open", json={"when": ""}, headers=h)
    assert early.status_code == 400 and "before it opens" in early.json()["error"] and "PM" in early.json()["error"]
