"""The Rosters page reads by its shape (design §5.56): every live run a card in time order, the Raids card's "When it
runs" block from the same server fields (`live_runs`, `next_run`), each active schedule's next three runs whatever the
horizon with `can_open` + `why` from the server, the run's week as steps for the strip (`week`) and the short line's
clause (`short_why`). The page itself is checked in tests/test_frontend_source.py; here, the JSON it reads."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, FakeBot, bearer
from test_raid_night import locked_ten

from oibot_gm import raidcycle as rc
from oibot_gm.web import app as web

RID = "barrow_deeps"
REAL = 410_000_000_000_000_000


@pytest.fixture
def c(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FakeBot(reg, rs)))


def raid(c) -> dict:
    return next(x for x in c.get("/api/rosters", headers=bearer()).json()["raids"] if x["id"] == RID)


def weekly(reg, first_open: str = "2026-01-01T00:00") -> None:
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": first_open}, "t")


def test_upcoming_lists_three_runs_per_active_schedule_whatever_the_horizon(reg, c):
    weekly(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")  # a template opens nothing on its own
    rows = raid(c)["upcoming"]
    assert sorted(u["schedule"] for u in rows) == ["alt"] * 3 + ["default"] * 3
    assert [u["start_iso"] for u in rows] == sorted(u["start_iso"] for u in rows)  # soonest first across schedules
    assert all("PM" in u["start"] and "19:30" not in u["start"] and u["opens"] for u in rows)  # words, 12-hour
    assert all(u["can_open"] and u["why"] is None for u in rows)  # all within three weeks


def test_a_raid_that_opens_later_lists_its_first_runs_and_says_why_they_cant_open_yet(reg, c):
    weekly(reg, (reg.now_local() + timedelta(days=40)).strftime("%Y-%m-%dT15:00"))
    r = raid(c)
    rows = r["upcoming"]
    assert len(rows) == 3, "no 'No upcoming runs before the raid opens': the first runs are listed"
    assert not any(u["can_open"] for u in rows)
    assert all(u["why"].startswith("Too far ahead to open now: Open works from ") and "three weeks before the run" in u["why"] for u in rows)
    assert r["next_run"] == {"when": rows[0]["start"], "schedule": rows[0]["schedule_name"], "opens": rows[0]["opens"]}
    assert r["live_runs"] == [] and r["opened"] is False and r["first_open_words"]
    # the row's reason matches what Open next run says on every surface
    nope = c.post(f"/api/raid/{RID}/open", json={"when": ""}, headers={**bearer(), "X-Requested-With": "oibot"})
    assert nope.status_code == 400 and "before it opens" in nope.json()["error"]


def test_live_runs_soonest_first_never_a_test_run_and_next_run_skips_them(reg, rs, c):
    weekly(reg)
    starts = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)]
    a = rc.open_run(reg, rs, RID, starts[1], by="t")
    b = rc.open_run(reg, rs, RID, starts[0], by="t")
    open_test_run(reg, rs)  # a rehearsal: its own tab, never a live line
    r = raid(c)
    assert [x["key"] for x in r["live_runs"]] == [b.key, a.key]
    assert all(x["state"] == "open" and "PM" in x["when"] for x in r["live_runs"])
    keys = {u["key"] for u in r["upcoming"]}
    assert a.key not in keys and b.key not in keys  # a live run has its own card, not an upcoming row
    assert r["next_run"]["when"] == reg.local12(starts[2])


def test_a_closed_run_at_an_upcoming_time_says_why_it_cant_open(reg, rs, c):
    weekly(reg)
    first = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)][0]
    ev = rc.open_run(reg, rs, RID, first, by="t")
    rc.end_run(reg, rs, ev, "t")
    r = raid(c)
    row = next(u for u in r["upcoming"] if u["key"] == ev.key)
    assert row["can_open"] is False and "already ended" in row["why"]
    assert r["next_run"]["when"] != row["start"]  # the next run is one that can still have a sheet


def test_the_run_week_steps_in_time_order_with_what_is_done(reg, rs, c):
    ev, _seated = locked_ten(reg, rs)
    j = c.get(f"/api/run/{ev.key}", headers=bearer()).json()
    steps = [w["step"] for w in j["week"]]
    assert steps[0] == "signup_lead_hours" and steps[-1] == "raid" and "lock_hours_before" in steps
    assert [w["at"] for w in j["week"]] == sorted(w["at"] for w in j["week"])
    done = {w["step"]: w["done"] for w in j["week"]}
    assert done["signup_lead_hours"] and done["lock_hours_before"] and not done["raid"]
    assert all("PM" in w["when"] or "AM" in w["when"] for w in j["week"])


def test_the_short_line_says_why_after_the_lock(reg, rs, c):
    ev, seated = locked_ten(reg, rs)
    rc.free_seat(reg, rs, ev, seated[-1].discord_id, "no-confirm")
    j = c.get(f"/api/run/{ev.key}", headers=bearer()).json()
    assert j["needs"]["headcount"] >= 1
    assert "on the bench could step in" in (j["short_why"] or "")


def test_the_short_line_names_the_regulars_who_havent_answered(reg, rs, c):
    weekly(reg)
    names = []
    for n, (cls, spec) in enumerate([("Priest", "Holy"), ("Shaman", "Restoration")]):
        uid = REAL + n
        names.append(f"Regular{'ab'[n]}")
        reg.add_character(uid, f"P{n}", names[-1], cls, spec, None, True)
        reg.regular_add(uid, f"{RID}:default", "t")
    first = [t for _s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * 21)][0]
    ev = rc.open_run(reg, rs, RID, first, by="t")
    assert rc.short_why(reg, ev) == f"2 regulars haven't answered: {names[0]}, {names[1]}"
    join(reg, rs, ev, [reg.members[REAL]])
    assert rc.short_why(reg, ev) == f"1 regular hasn't answered: {names[1]}"
    r = raid(c)
    card = next(e for e in r["open"] if e["key"] == ev.key)
    assert card["short_why"] == rc.short_why(reg, ev) and card["week"][0]["step"] == "signup_lead_hours"

