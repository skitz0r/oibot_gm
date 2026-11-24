"""Phase 4, Task 2 on the widened TEST profile (tests/test_lineups.py: two more raids, so a lineup can hold four): an
event of four raids reads each raid at its real-time start with its own rosters, and a raid taken out of the lineup is
no row, no part and no event of its own."""
from __future__ import annotations

import asyncio

from conftest import OWNER, join
from test_lineups import BD, H, LINEUP, bot, open_four, reg, start_in  # noqa: F401 — the wide profile's fixtures
from test_raid_night import H as auth, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import raidcycle as rc
from oibot_gm.web.api_events import NO_EVENT


def get(web_bot, path):
    return web_bot.c.get(path, headers=auth())


def test_a_four_raid_event_reads_each_raid_at_its_own_start(web_bot, reg, rs):
    ev = open_four(reg, rs)
    j = get(web_bot, f"/api/event/{ev.key}").json()
    parts = rc.event_parts(rs, ev)
    assert len(parts) == 4 and [p["key"] for p in j["parts"]] == [p.key for p in parts]
    lineup = rc.run_lineup(reg, rs, ev)
    assert j["lineup"] == lineup and [e["raid"] for e in lineup] == [H, BD, "extra_a", "extra_b"]
    for p, e in zip(j["parts"], lineup):
        at = rc.after_minutes(ev.start, e["starts"])
        assert p["label"] == f"{reg.local12(at, '%I:%M %p')} · {reg.raid_def(e['raid'])['name']}"
        assert p["rosters_words"] == f"{e['rosters']} of {reg.raid_def(e['raid']).get('size')}"
    assert [p["rosters_words"].split(" of ")[0] for p in j["parts"]] == ["1", "2", "1", "1"]
    facts = {f["label"]: f["value"] for f in j["details"]}
    assert facts["Lineup"] == "Hyjal Summit, then Barrow Deeps × 2, then Extra A, then Extra B"
    row = get(web_bot, "/api/event-list").json()["open"]
    assert [r["key"] for r in row] == [ev.key] and row[0]["subtitle"] == "Hyjal Summit, then Barrow Deeps, then Extra A, then Extra B"


def test_a_dropped_raid_is_no_row_no_part_and_no_event(web_bot, reg, rs):
    ev = open_four(reg, rs)
    join(reg, rs, ev, reg.test_members()[:4])
    gone = rc.event_parts(rs, ev)[3]
    asyncio.run(web_bot.bot.edit_lineup(reg, rs, ev, [{"raid": H, "rosters": 1}, *LINEUP[:2]], by="Jon", by_id=OWNER))
    assert gone.dropped and gone.state == "cancelled"
    r = get(web_bot, f"/api/event/{gone.key}")
    assert r.status_code == 404 and r.json()["error"] == NO_EVENT and gone.key not in r.text
    j = get(web_bot, f"/api/event/{ev.key}").json()
    assert gone.key not in [p["key"] for p in j["parts"]] and len(j["parts"]) == 3
    lst = get(web_bot, "/api/event-list").json()
    assert [r["key"] for r in lst["open"]] == [ev.key] and gone.key not in [r["key"] for r in lst["history"]]
    for p in rc.event_parts(rs, ev):  # the event ends: history shows it once, without the raid that went
        rc.end_run(reg, rs, p, "t")
    lst = get(web_bot, "/api/event-list").json()
    assert [r["key"] for r in lst["history"]] == [ev.key]
    assert lst["history"][0]["subtitle"] == "Hyjal Summit, then Barrow Deeps, then Extra A · finished"
