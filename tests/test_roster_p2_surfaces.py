"""Roster redesign Phase 2 on every surface — one code path (the planner, RaidMixin.send_plan) behind:
- the web: /api/run/{key}/fill/preview before lock returns the plan; /fill needs its rev (stale → 409 + the current
  plan, nothing sent), takes picks; the run JSON carries the plan; /api/rosters carries coverage level + forecast;
- plain text: run_fill before lock previews the suggestions, a send carries the plan fingerprint the diff showed;
- MCP fill_seats: previews before lock, sends only with the rev (and picks);
- Discord: /raid fill on an open run = the suggestions screen (alternates picked from a select) → Send; a stale screen
  is redrawn, not sent;
- regulars coverage (green / amber / red, recruit candidates) and the 4-week forecast."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_flows_raid import channel, harness, officer  # noqa: F401 — the flows' fixtures
from test_mcp import TOKEN, bearer
from test_parity_verbs import Bot
from test_roster_p2_planner import KEY, RID, add, worked_example

from oibot_gm import configops
from oibot_gm import flows_raid  # noqa: F401 — registers the flows
from oibot_gm import mcp_server as ms
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import Absence, RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client_bot(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    bot = Bot(reg, rs)
    return TestClient(web.create_app(bot)), bot


# ---------------------------------------------------------------- the web

def test_web_preview_and_send_before_lock(reg, rs, client_bot):
    c, bot = client_bot
    ev, p = worked_example(reg, rs)
    pv = c.post(f"/api/run/{ev.key}/fill/preview", json={}, headers=bearer())
    assert pv.status_code == 200, pv.text
    plan = pv.json()["plan"]
    assert plan["phase"] == "prelock" and plan["full"] and plan["expected"] == 10 and plan["still_short"] == "nothing"
    assert [[o[0]["display_name"] for o in st["options"]] for st in plan["seats"]] == [["Kael", "Gorm"], ["Ana", "Wren", "Pip"]]
    assert plan["seats"][0]["options"][0][0]["why"].startswith("short 1 tank · Kael's offspec Protection")
    assert any(ln.startswith("1. Kael → Protection (offspec)") for ln in plan["lines"])
    assert c.post(f"/api/run/{ev.key}/fill", json={}, headers=bearer()).status_code == 400, "a send before lock needs the rev"
    stale = c.post(f"/api/run/{ev.key}/fill", json={"rev": "0123456789ab"}, headers=bearer())
    assert stale.status_code == 409 and stale.json()["plan"]["rev"] == plan["rev"] and not ev.fill_asks
    ok = c.post(f"/api/run/{ev.key}/fill", json={"rev": plan["rev"], "picks": {"1": 1}}, headers=bearer())
    assert ok.status_code == 200 and ok.json()["message"] == "asked Kael (swap), Wren (swap)"
    assert [(a.display_name, a.phase, a.approved_by) for a in ev.fill_asks] == [("Kael", "prelock", "mcp"), ("Wren", "prelock", "mcp")]
    run_json = c.get(f"/api/run/{ev.key}", headers=bearer()).json()
    assert run_json["plan"]["waiting"] == ["Kael", "Wren"] and run_json["fill_asks"][0]["phase"] == "prelock"


def test_rosters_carry_coverage_level_and_forecast(reg, rs, client_bot):
    c, _ = client_bot
    worked_example(reg, rs)
    raid = next(r for r in c.get("/api/rosters", headers=bearer()).json()["raids"] if r["id"] == RID)
    s = next(x for x in raid["series"] if x["key"] == KEY)
    assert s["level"] == "green" and len(s["forecast"]) >= 4
    first = s["forecast"][0]
    assert first["short"] == {"tank": 1, "healer": 1} and first["away"] == ["Ona", "Thorn"] and ":" not in first["day"]
    assert all(f["short"] == {} for f in s["forecast"][1:]), "the absences cover one day only"


# ---------------------------------------------------------------- plain text

def test_plain_text_previews_and_sends_with_the_fingerprint(reg, rs):
    ev, p = worked_example(reg, rs)
    preview = configops.apply(reg, ConfigOp(op="run_fill", target=ev.key, value="preview"), "Officer", is_owner=False, raids=rs)
    assert preview.startswith(f"{ev.key}: suggestions before lock (nothing sent): 1. Kael → Protection (offspec)") and "alternates: Wren brings Fern" in preview
    send = ConfigOp(op="run_fill", target=ev.key, value="send")
    said = configops.describe(reg, send)
    assert "ask these by DM" in said and "Ana → Holy (flex)" in said
    revs = configops.board_revs(reg, [send], rs)
    assert revs == {configops.PLAN_REV + ev.key: rc.role_plan(reg, rs, ev).rev}
    assert configops.op_rev(send, ev.key, revs) == revs[configops.PLAN_REV + ev.key]
    bot = Bot(reg, rs)
    with pytest.raises(RegistryError, match="preview the suggestions first"):
        run(configops.apply_async(reg, send, "Officer", True, bot=bot))
    line = run(configops.apply_async(reg, send, "Officer", True, bot=bot, rev=configops.op_rev(send, ev.key, revs)))
    assert line.startswith(f"{ev.key}: asked Kael (offspec), Ana (flex)")
    with pytest.raises(rc.PlanChanged):  # the same diff applied twice: the plan moved (they are asked now)
        run(configops.apply_async(reg, send, "Officer", True, bot=bot, rev=revs[configops.PLAN_REV + ev.key]))


# ---------------------------------------------------------------- MCP

def test_mcp_fill_seats_previews_and_sends_with_rev():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content or b"{}")))
        if request.url.path.endswith("/fill/preview"):
            return httpx.Response(200, json={"plan": {"phase": "prelock", "rev": "abc123abc123", "lines": ["1. Kael → Protection (offspec)"]}, "batch": []})
        return httpx.Response(200, json={"message": "asked Kael (swap)"})

    ms.use(ms.Api(url="http://bot", token="t" * 64, transport=httpx.MockTransport(handler)))
    try:
        out = ms.fill_seats("bd-1215-1930")
        assert out["plan"]["rev"] == "abc123abc123" and seen[-1][0] == "/api/run/bd-1215-1930/fill/preview"
        assert ms.fill_seats("bd-1215-1930", send=True, rev="abc123abc123", picks={1: 2}) == {"message": "asked Kael (swap)"}
        assert seen[-1] == ("/api/run/bd-1215-1930/fill", {"rev": "abc123abc123", "picks": {"1": 2}})
    finally:
        ms.use(None)


# ---------------------------------------------------------------- Discord: /raid fill before lock, the Suggest… screen

def test_raid_fill_on_an_open_run_is_suggest_then_send(reg, rs, officer, channel):
    ev, p = worked_example(reg, rs)
    h, bot = harness(reg, rs)
    run(FLOWS["raid_fill"](h.command(), reg))
    assert "before lock" in h.text and "Kael → Protection (offspec)" in h.text and "Still short if everyone says yes: nothing" in h.text
    assert not bot.calls, "nothing goes out before Send"
    healer = next(s for s in h.selects() if "(healer)" in s)
    run(h.pick(healer, "1"))  # the first alternate: Wren brings Fern
    assert "Wren brings Fern (Restoration) (alternate)" in h.text
    run(h.press("Send 2 asks"))
    assert bot.calls[-1] == ("send_plan", ev.key, "Tester", ["Kael", "Wren"])
    assert "Asked Kael, Wren" in h.text


def test_a_stale_suggestions_screen_is_redrawn_not_sent(reg, rs, officer, channel):
    ev, p = worked_example(reg, rs)
    h, bot = harness(reg, rs)
    run(FLOWS["raid_suggest"](h.command(), reg, key=ev.key))
    rc.set_signup(reg, rs, ev, p["Gorm"], None, "in")  # meanwhile an alternate joined: the plan changed
    run(h.press("Send 2 asks"))
    assert not [c for c in bot.calls if c[0] == "send_plan"] and "⚠️ The sheet changed" in h.text


# ---------------------------------------------------------------- regulars coverage and the forecast

def _series(reg, rows):
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    ms_ = [add(reg, 700 + i, *row) for i, row in enumerate(rows)]
    for m in ms_:
        reg.regular_add(m.discord_id, KEY, "officer")
    return ms_


def test_coverage_green_amber_red_with_recruits(reg):
    ms_ = _series(reg, [("Tia", "Warrior", "Protection"), ("Tob", "Paladin", "Protection"), ("Hux", "Priest", "Holy"), ("Hob", "Shaman", "Restoration")])
    assert reg.series_coverage(RID)[0]["level"] == "green", "regular mains cover the minimums"
    reg.regular_remove(ms_[1].discord_id, KEY, "t")
    offy = add(reg, 750, "Offy", "Warrior", "Fury", "Protection")
    reg.regular_add(offy.discord_id, KEY, "t")
    cov = reg.series_coverage(RID)[0]
    assert cov["level"] == "amber" and cov["cover"] == ["Offy (tank)"], "covered only with an offspec"
    reg.regular_remove(offy.discord_id, KEY, "t")
    add(reg, 760, "Gorm", "Paladin", "Protection")  # not a regular: a recruit candidate for the missing tank
    cov = reg.series_coverage(RID)[0]
    assert cov["level"] == "red" and "Gorm" in cov["recruit"] and "Tia" not in cov["recruit"]


def test_forecast_counts_regulars_minus_absences(reg):
    ms_ = _series(reg, [("Tia", "Warrior", "Protection"), ("Tob", "Paladin", "Protection"), ("Hux", "Priest", "Holy"), ("Hob", "Shaman", "Restoration")])
    rows = rc.series_forecast(reg, KEY)
    assert rows and all(r["short"] == {} for r in rows)
    second = rows[1]
    day = second["start"][:10]
    ms_[0].absences = [Absence(start=day, end=day, reason="trip", by="t")]
    reg.save(ms_[0], "away")
    rows = rc.series_forecast(reg, KEY)
    assert rows[1]["short"] == {"tank": 1} and rows[1]["away"] == [ms_[0].display_name] and rows[0]["short"] == {}
    assert all("AM" in r["when"] or "PM" in r["when"] for r in rows), "12-hour words for people"
    reg.regular_remove(ms_[0].discord_id, KEY, "t")
    reg.set_schedule(RID, "default", {"active": False}, "t")
    assert rc.series_forecast(reg, KEY) == [], "a paused series has no forecast"
