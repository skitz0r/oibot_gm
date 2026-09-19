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
