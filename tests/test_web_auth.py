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
