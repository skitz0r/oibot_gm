"""Phase 5, Task 7: /raid set (flows_roster.SetWizard) prints the server's box — consequences.member_action_box, the one
POST /api/admin/action's preview / seen answers for the site's row ⋮ menu — and Join / Bench pick the spec the
character plays (the role switch). After the lock a switch on someone rostered switches their seat at once, nobody told
(2026-11-27). The client is test_raid_night's NightBot: the real RaidMixin verbs, nothing on Discord."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import join, lock_with_board
from test_flows_raid import _user, officer, open_real_run  # noqa: F401 — the fixture
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture
from test_reschedule import OFFICER_ID

from oibot_gm import consequences as cq
from oibot_gm import flows_roster  # noqa: F401 — registers raid_set
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

UID = 630_000_000_000_000_000
HEAL, DPS = "Holy", "Shadow"  # a Priest: Holy heals, Shadow is ranged damage


def run(c):
    return asyncio.run(c)


def harness(reg, rs):
    bot = NightBot(reg, rs)
    return Harness(reg, rs, user=SimpleNamespace(id=OFFICER_ID, display_name="Officer", mention="@Officer"), client=bot), bot


def healer(reg, k: int = 0):
    """A member whose main heals as Holy and plays Shadow as their offspec."""
    uid = UID + k
    reg.add_character(uid, f"Healer{k}", f"Heal{chr(97 + k)}", "Priest", HEAL, DPS, True)
    return reg.members[uid]


def test_join_as_another_spec_switches_the_role_before_the_lock(reg, rs, officer):  # noqa: F811
    ev = open_real_run(reg, rs)
    m = healer(reg)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    assert h.selects() == ["Plays which spec?"]
    sel = next(s for s in h._items() if getattr(s, "placeholder", None) == "Plays which spec?")
    assert next(o for o in sel.options if o.default).value == HEAL, "their main's own spec is picked already"
    run(h.pick("Plays which spec?", DPS))
    box = cq.member_action_box(reg, ev, m, "join", m.main.label, DPS, by_id=OFFICER_ID)
    assert h.buttons() == ["Record answer", "Back"] and all(s in h.text for s in box["sentences"])
    assert str(m.discord_id) not in ev.signups, "nothing before the press"
    run(h.press("Record answer"))
    s = ev.signups[str(m.discord_id)]
    assert (s.status, s.spec) == ("in", DPS)


def test_a_spec_switch_on_a_seated_member_is_applied_quietly(reg, rs, officer):  # noqa: F811
    """After the lock (2026-11-27) Join as a spec of another role, for someone rostered, switches their seat at once:
    the box says nobody gets a DM, the press writes the seat, nobody is told."""
    ev = open_real_run(reg, rs)
    pool = [healer(reg, k) for k in range(3)]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [p.display_name for p in pool])
    m = pool[0]
    assert ev.seat_of(m)
    reg.add_character(m.discord_id, m.display_name, "Altpriest", "Priest", HEAL, DPS, False)
    m = reg.members[m.discord_id]
    h, bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    run(h.pick("Pick the character", m.main.label))
    run(h.pick("Plays which spec?", DPS))
    assert h.buttons() == ["Record answer", "Back"] and cq.QUIET_TOLD in h.text, h.text
    notices = len(reg.notices(m.discord_id))
    run(h.press("Record answer"))
    assert (ev.seat_of(m)[1].spec, ev.seat_of(m)[1].role) == (DPS, "ranged")
    assert bot.dms == {} and len(reg.notices(m.discord_id)) == notices


def test_a_callout_is_its_own_box_with_a_note(reg, rs, officer):  # noqa: F811
    ev = open_real_run(reg, rs)
    m = healer(reg)
    join(reg, rs, ev, [m])
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="callout"))
    assert h.selects() == [], "a callout plays no spec"
    assert h.buttons() == ["Record the callout", "Record the callout with a note", "Back"]
    assert h.text.startswith(f"**Record a callout for {m.display_name}?**")
    run(h.press("Record the callout with a note"))
    run(h.submit(note="flight delayed"))
    assert ev.signups[str(m.discord_id)].status == "out"
    assert ev.callouts and ev.callouts[-1].note == "flight delayed"


def test_an_answer_changed_meanwhile_is_a_fresh_box(reg, rs, officer):  # noqa: F811
    ev = open_real_run(reg, rs)
    m = healer(reg)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    run(h.pick("Plays which spec?", HEAL))
    join(reg, rs, ev, [m], "sub")  # the member presses Bench while the officer's box is open
    run(h.press("Record answer"))
    assert CONFIRM_WORDS["changed"] in h.text and ev.signups[str(m.discord_id)].status == "sub", "nothing written"
    run(h.press("Record answer"))
    assert ev.signups[str(m.discord_id)].status == "in"


def test_the_route_prints_the_same_box(reg, rs, web_bot):  # noqa: F811
    ev = open_real_run(reg, rs)
    m = healer(reg)
    join(reg, rs, ev, [m])
    body = {"run": ev.key, "member": str(m.discord_id), "action": "pin_out"}
    r = web_bot.c.post("/api/admin/action", json={**body, "preview": True}, headers=H())
    assert r.status_code == 200, r.text
    want = cq.member_action_box(reg, ev, m, "pin_out", by_id=OFFICER_ID, rev=rc.member_state_rev(ev, m))  # the bearer = the owner
    assert r.json()["box"] == want
    assert (want["title"], want["verb"], want["danger"]) == (f"Keep {m.display_name} off the roster?", "Keep off", False)
    r = web_bot.c.post("/api/admin/action", json={**body, "seen": want["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    assert ev.pins.get(str(m.discord_id)) == "out"


def test_each_action_names_its_person_and_verb(reg, rs):
    ev = open_real_run(reg, rs)
    m = healer(reg)
    join(reg, rs, ev, [m])
    for key, title, verb in (("pin_in", f"Pin {m.display_name} to the roster?", "Pin"), ("callout", f"Record a callout for {m.display_name}?",
                             "Record the callout"), ("bench", "Answer for someone", "Record answer")):
        b = cq.member_action_box(reg, ev, m, key, by_id=OFFICER_ID)
        assert (b["title"], b["verb"], b["danger"]) == (title, verb, False)
    assert cq.answer_box(reg, ev, m, "bench", by_id=OFFICER_ID) == cq.member_action_box(reg, ev, m, "bench", by_id=OFFICER_ID)


# ---------------------------------------------------------------- fix round 1: the seated switch is by ROLE

def seated_priest_with_druid(reg, rs):
    """A Holy priest rostered on a locked run, with a druid alt that plays Feral (its own spec, melee) and Restoration
    (its offspec, healer)."""
    ev = open_real_run(reg, rs)
    pool = [healer(reg, k) for k in range(3)]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [p.display_name for p in pool])
    m = pool[0]
    reg.add_character(m.discord_id, m.display_name, "Druidalt", "Druid", "Feral", "Restoration", False)
    return ev, reg.members[m.discord_id]


def test_a_same_role_spec_on_another_character_keeps_the_healer_seat(reg, rs, officer):  # noqa: F811
    ev, m = seated_priest_with_druid(reg, rs)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    run(h.pick("Pick the character", "Druidalt"))
    run(h.pick("Plays which spec?", "Restoration"))
    assert h.buttons() == ["Record answer", "Back"], h.text
    assert "moves from" not in h.text
    run(h.press("Record answer"))
    seat = ev.seat_of(m)[1]
    assert (seat.character, seat.spec, seat.role) == ("Druidalt", "Restoration", "healer")


def test_an_explicit_spec_of_another_role_switches_the_seat(reg, rs, officer):  # noqa: F811
    """Bench with a spec of another role, for someone rostered after the lock: their answer and their seat follow it."""
    ev, m = seated_priest_with_druid(reg, rs)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="bench"))
    run(h.pick("Plays which spec?", DPS))
    assert h.buttons() == ["Record answer", "Back"], h.text
    run(h.press("Record answer"))
    assert ev.signups[str(m.discord_id)].status == "sub" and (ev.seat_of(m)[1].spec, ev.seat_of(m)[1].role) == (DPS, "ranged")


def test_the_alts_own_spec_of_another_role_says_the_move_and_reseats(reg, rs, officer):  # noqa: F811
    ev, m = seated_priest_with_druid(reg, rs)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    run(h.pick("Pick the character", "Druidalt"))
    run(h.pick("Plays which spec?", "Feral"))
    box = cq.member_action_box(reg, ev, m, "join", "Druidalt", None, by_id=OFFICER_ID)
    assert box["sentences"][0] == f"{m.display_name} plays Druidalt and moves from healer to melee on the roster."
    assert box["sentences"][0] in h.text
    run(h.press("Record answer"))
    seat = ev.seat_of(m)[1]
    assert (seat.character, seat.spec, seat.role) == ("Druidalt", "Feral", "melee")


def test_bench_without_a_spec_keeps_a_seated_switch(reg, rs):
    """Fix 5: a seated member whose answer carries a switched spec goes to Bench with no spec picked — the signup keeps
    the switched spec, as the seat does."""
    ev = open_real_run(reg, rs)
    pool = [healer(reg, k) for k in range(3)]
    join(reg, rs, ev, pool)
    m = pool[0]
    rc.set_signup(reg, rs, ev, m, None, "in", spec=DPS)
    lock_with_board(reg, rs, ev, [p.display_name for p in pool])
    assert ev.seat_of(m)[1].spec == DPS
    s = rc.set_signup(reg, rs, ev, m, None, "sub")
    assert (s.status, s.spec, ev.seat_of(m)[1].spec) == ("sub", DPS, DPS)


def test_the_spec_select_offers_only_what_the_character_plays(reg, rs, officer):  # noqa: F811
    ev = open_real_run(reg, rs)
    m = healer(reg)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join"))
    sel = next(s for s in h._items() if getattr(s, "placeholder", None) == "Plays which spec?")
    assert sorted(o.value for o in sel.options) == sorted([HEAL, DPS]), "never Discipline: they don't play it"
    reg.add_character(UID + 9, "Solo", "Solo", "Priest", HEAL, None, True)
    solo = reg.members[UID + 9]
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(solo), what="join"))
    assert h.selects() == [] and h.buttons() == ["Record answer", "Back"], "one spec: no select"


def test_the_confirm_no_box_names_the_ask(reg, rs):
    title, verb = cq.MEMBER_ACTION_WORDS["confirm_no"]
    assert "(" not in verb and "Confirm ask" in title
    assert "fill ask" in cq.MEMBER_ACTION_WORDS["fill_no"][0]


# ---------------------------------------------------------------- final fix: re-picking a switch keeps it

def test_bench_leaving_the_switched_spec_keeps_the_switch_on_an_open_run(reg, rs, officer):  # noqa: F811
    """Final fix 1: an officer switched a Holy priest to Shadow on the open sheet; /raid set → Bench with Shadow left
    picked records Bench as Shadow, never silently back to Holy."""
    ev = open_real_run(reg, rs)
    m = healer(reg)
    rc.set_signup(reg, rs, ev, m, None, "in", spec=DPS)
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="bench"))
    sel = next(s for s in h._items() if getattr(s, "placeholder", None) == "Plays which spec?")
    assert next(o for o in sel.options if o.default).value == DPS, "the switched spec is picked already"
    run(h.pick("Plays which spec?", DPS))
    run(h.press("Record answer"))
    s = ev.signups[str(m.discord_id)]
    assert (s.status, s.spec, s.role) == ("sub", DPS, "ranged")


def test_on_a_locked_run_a_switch_back_to_their_own_spec_follows_the_seat(reg, rs, officer):  # noqa: F811
    ev = open_real_run(reg, rs)
    pool = [healer(reg, k) for k in range(3)]
    join(reg, rs, ev, pool)
    m = pool[0]
    rc.set_signup(reg, rs, ev, m, None, "in", spec=DPS)
    lock_with_board(reg, rs, ev, [p.display_name for p in pool])
    assert ev.seat_of(m)[1].spec == DPS
    h, _bot = harness(reg, rs)
    run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="bench"))
    run(h.pick("Plays which spec?", HEAL))
    assert h.buttons() == ["Record answer", "Back"], h.text
    run(h.press("Record answer"))
    s = ev.signups[str(m.discord_id)]
    assert (s.status, s.spec, ev.seat_of(m)[1].spec) == ("sub", HEAL, HEAL)
