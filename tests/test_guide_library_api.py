"""The guild guide library on the site and MCP (spec docs/superpowers/specs/2026-11-09-guide-library-design.md §5):
GET /api/guides/library, a member's upload and download (never MCP: the string and the BattleTag stay out of
transcripts), the officers' withdraw / restore with a preview (MCP needs the actor, D8), the `guides` topic on every
write, one ops line per write and none for a download. D7: no BattleTag and no import string in a log record, an ops
line, the data repo or any response but the download's body."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import types
from datetime import datetime, timezone

import httpx
import pytest
from conftest import OFFICER_ROLE, OWNER, MemberDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_mcp import TOKEN, FakeBot

from oibot_gm import mcp_server as ms
from oibot_gm.inbox import notices_of
from oibot_gm.guides import library as lib
from oibot_gm.guides.rxp_decode import decode, encode
from oibot_gm.web import app as web
from oibot_gm.web.api_guides import ACTOR_NEEDED, FROM_MCP, NO_TOOL, install_guides_api

SECRET = "s" * 40
AUTHOR, READER, OFFICER = 480_000_000_000_000_011, 480_000_000_000_000_012, 480_000_000_000_000_013
AUTHOR_TAG, READER_TAG = "Quillwright#48213", "Marrowfen#77104"
TEXTS = ["#name 1-6 Guildwood\n#group Our Route\nstep\n.goto 1429/0,-8949,-132\n.accept 783 >>Take it\n",
         "#name 6-12 Guildshore\n#group Our Route\nstep\n.hs Sentinel Hill\n"]
STRING = encode(list(TEXTS), AUTHOR_TAG, sections=2)
H = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}
SECRETS = (AUTHOR_TAG, READER_TAG, "Quillwright", "Marrowfen", STRING[:40])


def person(uid, name, *roles):
    return types.SimpleNamespace(id=uid, display_name=name, roles=[types.SimpleNamespace(id=r) for r in roles])


class LibraryBot(FakeBot):
    """FakeBot behind the real app with a server directory: two members, an officer (the role) and the owner."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        sid = reg.config.discord_guild_id
        self.directory = MemberDirectory({(sid, AUTHOR): person(AUTHOR, "Quill"), (sid, READER): person(READER, "Marrow"),
                                          (sid, OFFICER): person(OFFICER, "Offi", OFFICER_ROLE), (sid, OWNER): person(OWNER, "Owner")})
        self.directory.install(self, servers=(sid,))

    def officiates(self, member, guild):
        return any(r.id == OFFICER_ROLE for r in getattr(member, "roles", []))


@pytest.fixture
def clock(monkeypatch):
    t = [datetime(2026, 11, 9, 23, 0, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(lib, "_now", lambda: t[0])
    return t


@pytest.fixture
def w(reg, rs, monkeypatch, clock):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    monkeypatch.setattr(lib, "GUIDE_LISTENERS", [])
    reg.add_character(AUTHOR, "Quill", "Quillon", "Mage", "Fire", None, True)
    reg.add_character(READER, "Marrow", "Mossvale", "Priest", "Holy", None, True)
    reg.add_character(OFFICER, "Offi", "Ashgrove", "Warrior", "Protection", None, True)
    bot = LibraryBot(reg, rs)
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, clock=clock)


def as_user(c, uid) -> dict:
    c.cookies.clear()
    c.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


def upload(w, faction="horde", uid=AUTHOR, text=STRING, tag=AUTHOR_TAG):
    return w.c.post("/api/guides/library/upload", json={"text": text, "battletag": tag, "faction": faction}, headers=as_user(w.c, uid))


def no_secret(*texts):
    for t in texts:
        for s in SECRETS:
            assert s not in str(t), f"a BattleTag or the string leaked: {str(t)[:200]!r}"


# ---------------------------------------------------------------- reads and the member's upload / download

def test_the_library_reads_with_the_factions_and_the_upload_sentences(w):
    d = w.c.get("/api/guides/library", headers=as_user(w.c, READER)).json()
    assert d["available"] is True and d["rows"] == [] and d["can_withdraw"] is False
    assert d["factions"] == [{"id": "alliance", "words": "Alliance"}, {"id": "horde", "words": "Horde"}, {"id": "both", "words": "Both factions"}]
    assert d["upload_sentences"] == {f: lib.upload_consequence(w.reg, f) for f in lib.FACTIONS}
    assert w.c.get("/api/guides/library", headers=as_user(w.c, OFFICER)).json()["can_withdraw"] is True


def test_a_member_uploads_and_another_downloads_a_copy_locked_to_their_tag(w, caplog):
    caplog.set_level(logging.DEBUG)
    r = upload(w)
    assert r.status_code == 200, r.text
    d = r.json()
    when = w.reg.local12("2026-11-09T23:00:12+00:00")
    assert d["message"] == f"Uploaded: Horde · {when}." and d["available"]
    assert [(x["id"], x["faction"], x["by"], x["latest"], x["guides"]) for x in d["rows"]] == [("horde-20261109-230012", "horde", "Quill", True, 2)]
    assert [ln for _lvl, ln in w.bot.ops.lines] == [f"[web] Quill: guild guide uploaded: Horde · {when}"]

    r = w.c.post("/api/guides/library/horde-20261109-230012/download", json={"battletag": READER_TAG}, headers=as_user(w.c, READER))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"].startswith("attachment") and 'filename="GuildGuide-Horde-20261109-Marrowfen.txt"' in r.headers["content-disposition"]
    assert decode(r.text, READER_TAG) == TEXTS
    assert len(w.bot.ops.lines) == 1, "no ops line for a download"

    no_secret(*(rec.getMessage() for rec in caplog.records), *(ln for _l, ln in w.bot.ops.lines), json.dumps(d))
    root = w.reg.store.root
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            no_secret(p.read_text(errors="replace"))
    no_secret(subprocess.run(["git", "-C", str(root), "log", "-p", "--all"], capture_output=True, text=True).stdout)


def test_upload_and_download_refusals_are_words_and_never_echo_the_request(w, caplog):
    caplog.set_level(logging.DEBUG)
    for body, words in (({"text": STRING, "battletag": "Wrong#11111", "faction": "horde"}, "doesn't unlock"),
                        ({"text": "", "battletag": AUTHOR_TAG, "faction": "horde"}, "Paste the import string first"),
                        ({"text": STRING, "battletag": AUTHOR_TAG, "faction": "goblin"}, lib.BAD_FACTION),
                        ({"text": STRING, "battletag": "Quillwright", "faction": "horde"}, "name, a # and")):
        r = w.c.post("/api/guides/library/upload", json=body, headers=as_user(w.c, AUTHOR))
        assert r.status_code == 400 and words in r.json()["error"], (body["faction"], r.text)
        no_secret(r.text)
    assert upload(w).status_code == 200
    vid = "horde-20261109-230012"
    r = w.c.post(f"/api/guides/library/{vid}/download", json={"battletag": "Marrowfen"}, headers=as_user(w.c, READER))
    assert r.status_code == 400 and "name, a # and" in r.json()["error"]
    r = w.c.post("/api/guides/library/horde-20991231-000000/download", json={"battletag": READER_TAG}, headers=as_user(w.c, READER))
    assert r.status_code == 404 and r.json()["error"] == lib.NO_VERSION
    r = w.c.post("/api/guides/library/../index/download", json={"battletag": READER_TAG}, headers=as_user(w.c, READER))
    assert r.status_code in (404, 405)
    no_secret(r.text, *(rec.getMessage() for rec in caplog.records), *(ln for _l, ln in w.bot.ops.lines))


def test_an_unexpected_failure_reads_generic_and_logs_no_secret(w, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    def boom(*a, **k):
        raise RuntimeError(f"broke on {a[2]!r} with {a[1][:60]!r}")  # an exception whose words carry the inputs

    monkeypatch.setattr(lib, "upload", boom)
    r = upload(w)
    assert r.status_code == 500 and "broke" not in r.text
    no_secret(r.text, *(rec.getMessage() for rec in caplog.records), *(str(rec.exc_info) for rec in caplog.records if rec.exc_info),
              *(ln for _l, ln in w.bot.ops.lines))


def test_mcp_never_uploads_or_downloads(w):
    r = w.c.post("/api/guides/library/upload", json={"text": STRING, "battletag": AUTHOR_TAG, "faction": "horde"}, headers=H)
    assert r.status_code == 403 and r.json()["error"] == FROM_MCP and "transcripts" in FROM_MCP
    assert lib.versions(w.reg) == []
    assert upload(w).status_code == 200
    r = w.c.post("/api/guides/library/horde-20261109-230012/download", json={"battletag": READER_TAG}, headers=H)
    assert r.status_code == 403 and r.json()["error"] == FROM_MCP
    assert w.c.get("/api/guides/library", headers=H).json()["rows"], "MCP reads the list"


def test_no_tool_for_the_game_version_answers_the_tools_words(w, monkeypatch):
    monkeypatch.setattr(w.reg.profile, "guides", {})
    h = as_user(w.c, AUTHOR)
    d = w.c.get("/api/guides/library", headers=h).json()
    assert d["available"] is False and d["rows"] == []
    for path, body in (("/api/guides/library/upload", {"text": STRING, "battletag": AUTHOR_TAG, "faction": "horde"}),
                       ("/api/guides/library/horde-20261109-230012/download", {"battletag": READER_TAG})):
        r = w.c.post(path, json=body, headers=as_user(w.c, AUTHOR))
        assert r.status_code == 400 and r.json()["error"] == NO_TOOL
    r = w.c.post("/api/guides/library/horde-20261109-230012/withdraw", json={}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 400 and r.json()["error"] == NO_TOOL


# ---------------------------------------------------------------- the officers' withdraw / restore

def test_officers_withdraw_and_restore_with_a_preview_first(w):
    assert upload(w).status_code == 200
    w.clock[0] = w.clock[0].replace(hour=23, minute=30)
    assert upload(w).status_code == 200
    newer, older = "horde-20261109-233012", "horde-20261109-230012"
    r = w.c.post(f"/api/guides/library/{newer}/withdraw", json={"preview": True}, headers=as_user(w.c, READER))
    assert r.status_code == 403, "members never withdraw"

    r = w.c.post(f"/api/guides/library/{newer}/withdraw", json={"preview": True}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 200, r.text
    v = lib.get(w.reg, newer)
    assert r.json() == {"preview": True, "sentences": [s for s in lib.withdraw_consequence(w.reg, v, by_id=OFFICER).replace(". ", ".\n").split("\n")]}
    assert len(r.json()["sentences"]) == 3 and lib.get(w.reg, newer).withdrawn_at is None and w.bot.ops.lines[2:] == [], "a preview writes nothing"

    r = w.c.post(f"/api/guides/library/{newer}/withdraw", json={}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 200, r.text
    d = r.json()
    when = lib.when(w.reg, v)
    assert d["message"] == f"Withdrawn: Horde · {when}." and d["can_withdraw"]
    assert [(x["id"], x["withdrawn"], x["latest"], x["withdrawn_by"]) for x in d["rows"]] == [(newer, True, False, "Offi"), (older, False, True, None)]
    assert w.bot.ops.lines[-1][1] == f"[web] Offi: guild guide withdrawn: Horde · {when}"
    assert any("was withdrawn by Offi" in n["text"] for n in notices_of(w.reg, AUTHOR)), "the uploader's notice, never a DM"

    # a member: the withdrawn version is gone from the list and can't be fetched by its id
    rows = w.c.get("/api/guides/library", headers=as_user(w.c, READER)).json()["rows"]
    assert [x["id"] for x in rows] == [older] and "withdrawn_by" not in rows[0]
    r = w.c.post(f"/api/guides/library/{newer}/download", json={"battletag": READER_TAG}, headers=as_user(w.c, READER))
    assert r.status_code == 400 and r.json()["error"] == lib.WITHDRAWN
    # an officer still may (officer=True)
    r = w.c.post(f"/api/guides/library/{newer}/download", json={"battletag": READER_TAG}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 200 and decode(r.text, READER_TAG) == TEXTS

    r = w.c.post(f"/api/guides/library/{newer}/withdraw", json={"preview": True}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 400 and r.json()["error"].startswith("Already withdrawn by Offi on ")
    r = w.c.post(f"/api/guides/library/{newer}/withdraw", json={}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 400 and r.json()["error"].startswith("Already withdrawn by Offi on ")

    r = w.c.post(f"/api/guides/library/{newer}/restore", json={"preview": True}, headers=as_user(w.c, OFFICER))
    assert r.json() == {"preview": True, "sentences": [f"The Horde guide of {when} is back.", "It becomes the latest again."]}
    r = w.c.post(f"/api/guides/library/{newer}/restore", json={}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 200 and r.json()["message"] == f"Restored: Horde · {when}." and r.json()["rows"][0]["latest"]
    assert w.bot.ops.lines[-1][1] == f"[web] Offi: guild guide restored: Horde · {when}"
    r = w.c.post(f"/api/guides/library/{newer}/restore", json={}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 400 and r.json()["error"] == lib.NOT_WITHDRAWN
    r = w.c.post("/api/guides/library/horde-20991231-000000/restore", json={"preview": True}, headers=as_user(w.c, OFFICER))
    assert r.status_code == 404 and r.json()["error"] == lib.NO_VERSION


def test_a_stale_officer_reads_but_never_withdraws(w):
    """§2.9: Discord can't answer for the officer now — the last-known yes keeps reads going, the write waits (503)."""
    assert upload(w).status_code == 200
    sid = w.reg.config.discord_guild_id
    w.bot.__dict__["_member_last_yes"] = {(sid, OFFICER): (time.time(), person(OFFICER, "Offi", OFFICER_ROLE))}
    w.bot.directory.flaky.add((sid, OFFICER))
    h = as_user(w.c, OFFICER)
    assert w.c.get("/api/guides/library", headers=h).status_code == 200
    r = w.c.post("/api/guides/library/horde-20261109-230012/withdraw", json={}, headers=h)
    assert r.status_code == 503 and "can't confirm your officer role" in r.json()["error"]
    assert lib.get(w.reg, "horde-20261109-230012").withdrawn_at is None


def test_mcp_withdraw_needs_the_actor_and_is_theirs(w):
    assert upload(w).status_code == 200
    vid = "horde-20261109-230012"
    r = w.c.post(f"/api/guides/library/{vid}/withdraw", json={"preview": True}, headers=H)
    assert r.status_code == 200 and r.json()["sentences"], "a preview needs no actor"
    r = w.c.post(f"/api/guides/library/{vid}/withdraw", json={}, headers=H)
    assert r.status_code == 403 and r.json()["error"] == ACTOR_NEEDED and lib.get(w.reg, vid).withdrawn_at is None
    r = w.c.post(f"/api/guides/library/{vid}/withdraw", json={}, headers=H | {"X-Oibot-Actor": str(READER)})
    assert r.status_code == 403, "the actor must be an officer"
    r = w.c.post(f"/api/guides/library/{vid}/withdraw", json={}, headers=H | {"X-Oibot-Actor": str(OFFICER)})
    assert r.status_code == 200, r.text
    v = lib.get(w.reg, vid)
    assert v.withdrawn_by == "Offi" and v.withdrawn_by_id == OFFICER
    assert w.bot.ops.lines[-1][1].startswith("[mcp] Offi: guild guide withdrawn: Horde · ")
    r = w.c.post(f"/api/guides/library/{vid}/restore", json={}, headers=H)
    assert r.status_code == 403 and r.json()["error"] == ACTOR_NEEDED
    assert w.c.post(f"/api/guides/library/{vid}/restore", json={}, headers=H | {"X-Oibot-Actor": str(OFFICER)}).status_code == 200


# ---------------------------------------------------------------- the `guides` topic

def test_every_write_publishes_the_guides_topic_once_per_app(reg, monkeypatch, clock):
    monkeypatch.setattr(lib, "GUIDE_LISTENERS", [])
    sent = []
    for _ in range(2):
        install_guides_api(FastAPI(), types.SimpleNamespace(), who=None, body=None, publish=lambda topic, guild=None, **kw: sent.append((topic, guild)))
    assert len(lib.GUIDE_LISTENERS) == 1, "a second app replaces the listener, never stacks it"
    v = lib.upload(reg, STRING, AUTHOR_TAG, "both", AUTHOR, "Quill")
    lib.withdraw(reg, v.id, "Offi", OFFICER)
    lib.restore(reg, v.id, "Offi", OFFICER)
    assert sent == [("guides", reg.key)] * 3


# ---------------------------------------------------------------- MCP tools

def test_mcp_tools_read_withdraw_and_restore_through_the_routes(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("x-oibot-actor"), request.read()))
        if request.method == "GET":
            return httpx.Response(200, json={"available": True, "rows": [{"id": "horde-20261109-230012", "faction": "horde", "faction_words": "Horde", "when": "Mon 9 Nov 3:00 PM",
                                                                          "by": "Quill", "guides": 2, "latest": True, "withdrawn": False}],
                                             "factions": [], "can_withdraw": True, "upload_sentences": {}})
        if json.loads(request.read() or b"{}").get("preview"):
            return httpx.Response(200, json={"preview": True, "sentences": ["The Horde guide of Mon 9 Nov 3:00 PM is withdrawn.", "You can restore it."]})
        return httpx.Response(200, json={"message": "Withdrawn: Horde · Mon 9 Nov 3:00 PM.", "rows": []})

    monkeypatch.setenv(ms.ACTOR_ENV, str(OFFICER))
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        d = ms.list_guild_guides()
        assert d["rows"][0]["id"] == "horde-20261109-230012" and d["available"] and calls[0][:2] == ("GET", "/api/guides/library")
        assert ms.withdraw_guild_guide("horde-20261109-230012", preview=True) == "The Horde guide of Mon 9 Nov 3:00 PM is withdrawn. You can restore it."
        assert calls[1][1] == "/api/guides/library/horde-20261109-230012/withdraw" and calls[1][2] is None, "a preview sends no actor"
        assert ms.withdraw_guild_guide("horde-20261109-230012") == "Withdrawn: Horde · Mon 9 Nov 3:00 PM."
        assert calls[2][2] == str(OFFICER) and json.loads(calls[2][3]) == {}
        ms.restore_guild_guide("horde-20261109-230012")
        assert calls[3][1].endswith("/restore") and calls[3][2] == str(OFFICER)
        names = {t.name for t in asyncio.run(ms.mcp.list_tools())}
        assert {"list_guild_guides", "withdraw_guild_guide", "restore_guild_guide"} <= names
        assert not {n for n in names if "guide" in n and ("upload" in n or "issue" in n or "download" in n or "get_guild_guide" == n)}, "no upload, no issue tool"
    finally:
        ms.use(None)
