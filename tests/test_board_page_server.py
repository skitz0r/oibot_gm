"""Server fixes after the Events board page review: the site's answer lines in the guild's 12-hour words (never a
Discord time stamp) and a locked run's open seats said truly while nothing is asked yet. (The board picture solved
per raid in the background went with the owner's tweaks C: nothing solves until Auto-fill groups is pressed —
tests/test_event_board.py.)"""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join, lock_with_board
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import raidcycle as rc

HYJAL, BD = "hyjal_summit_forever", "barrow_deeps"
REAL = 474_000_000_000_000_000


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def member(reg, i: int, name: str, cls: str = "Priest", spec: str = "Shadow", off: str | None = "Holy"):
    reg.add_character(REAL + i, name, name, cls, spec, off, True)
    return reg.members[REAL + i]


# ---------------------------------------------------------------- 2. no Discord stamp on the site

def test_the_answer_route_speaks_the_guilds_time_never_a_discord_stamp(reg, rs, web_bot):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = [member(reg, i, name) for i, name in enumerate(("Anwen", "Brisa", "Corvin"))]
    for body in ({"member": str(ms[0].discord_id), "status": "in"}, {"member": str(ms[1].discord_id), "status": "sub"},
                 {"member": str(ms[2].discord_id), "status": "out", "note": "away at a wedding"},
                 {"member": str(ms[0].discord_id), "status": "in", "spec": "Holy"}):
        r = web_bot.c.post(f"/api/run/{ev.key}/set", json=body, headers=H())  # the officers' Answer for someone (the coordinator's "answer" route)
        assert r.status_code == 200, r.text
        msg = r.json()["message"]
        assert "<t:" not in msg and "**" not in msg, msg
        assert reg.local12(ev.start) in msg or "out of" in msg, msg


def test_the_switch_route_speaks_the_guilds_time(reg, rs, web_bot, monkeypatch):
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD, "rosters": 1}])
    nyx = member(reg, 0, "Nyx")
    rc.set_signup(reg, rs, ev, nyx, None, "in", source="test")
    sw = rc.event_switches(reg, rs, ev)
    row = next(r for r in sw["rows"] if r["uid"] == str(nyx.discord_id) and r["spec"] == "Holy")
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": sw["revs"][row["part"]]}
    r = web_bot.c.post(f"/api/run/{ev.key}/switch", json=body, headers=H())
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert "<t:" not in msg and reg.local12(ev.start) in msg, msg


def test_site_words_turns_every_stamp_into_local12(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    from oibot_gm.raid_views import run_label

    line = f"✅ Nyx Join for {run_label(reg, ev)} and <t:{int(ev.start.timestamp())}:R>"
    assert rc.site_words(reg, line) == f"Nyx Join for Barrow Deeps {reg.local12(ev.start)} and {reg.local12(ev.start)}"


# ---------------------------------------------------------------- 3. a locked run's open seats, while nothing is asked

def test_a_locked_run_with_open_seats_says_how_many_and_who_asks(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members()[:7])
    lock_with_board(reg, rs, ev, [s.display_name for s in ev.by_status("in")])
    assert ev.fill_state == "idle" and rc.open_seat_count(reg, ev) == 3
    team = rc.run_team(reg, ev)
    team["autofill"] = True
    assert rc.fill_state_text(reg, ev) == "3 seats are open. Automatic asks will ask for them."
    team["autofill"] = False
    assert rc.fill_state_text(reg, ev) == f"3 seats are open. Automatic asks are off, so an officer fills them with {rc.fill_button_words()}."
    team["autofill"], ev.autofill_paused, ev.autofill_paused_by = True, True, "Jon"
    assert rc.fill_state_text(reg, ev) == "Automatic asks paused by Jon. 3 seats are open."
    full = rc.open_run(reg, rs, BD, start_in(reg, 4), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, full, reg.test_members()[:10])
    lock_with_board(reg, rs, full, [s.display_name for s in full.by_status("in")])
    assert rc.fill_state_text(reg, full) == rc.FILL_STATE_WORDS["idle"], "no seat open: as before"
