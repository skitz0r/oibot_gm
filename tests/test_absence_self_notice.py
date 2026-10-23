"""Carry-over from phase 2 (design §5.49): a member who records their OWN absence on a run they're rostered on gets no
"seat released (you're marked away)" notice about what they just did; when an OFFICER records it, the member is told
(a notice — never a DM). Decided by the actor's Discord id (`by_id`), never by comparing display names."""
from __future__ import annotations

import asyncio

from conftest import join, lock_with_board, open_test_run
from test_parity_verbs import Bot

from oibot_gm import discord_people

OFFICER_ID = 300_000_000_000_000_777


def rostered(reg, rs):
    ev = open_test_run(reg, rs, days=3)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    return ev, ms


def released(reg, m):
    return [n for n in reg.notices(m.discord_id) if n["kind"] == "released"]


def away(reg, rs, m, ev, *, by, by_id):
    bot = Bot(reg, rs)
    day = ev.start.astimezone(reg.tz).date().isoformat()
    _m, _a, touched = asyncio.run(discord_people.record_absence(bot, reg, m.discord_id, day, day, None, by, by_id=by_id))
    return touched


def test_my_own_absence_frees_my_seat_without_a_notice_about_it(reg, rs):
    ev, ms = rostered(reg, rs)
    m = ms[2]
    assert ev.seat_of(m)
    touched = away(reg, rs, m, ev, by=m.display_name, by_id=m.discord_id)
    assert touched == [ev.key] and not ev.seat_of(m), "the seat is still freed"
    assert released(reg, m) == [], "no notice about your own act"


def test_an_officer_recording_my_absence_tells_me_with_a_notice(reg, rs):
    ev, ms = rostered(reg, rs)
    m = ms[3]
    touched = away(reg, rs, m, ev, by="Officer", by_id=OFFICER_ID)
    assert touched == [ev.key] and not ev.seat_of(m)
    (n,) = released(reg, m)
    assert "marked away" in n["text"] and n["run"] == ev.key


def test_a_name_that_matches_is_not_who_did_it(reg, rs):
    """An officer who happens to share the member's display name is still someone else: the id decides."""
    ev, ms = rostered(reg, rs)
    m = ms[4]
    away(reg, rs, m, ev, by=m.display_name, by_id=OFFICER_ID)
    assert len(released(reg, m)) == 1
