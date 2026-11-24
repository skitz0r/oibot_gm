"""Phase 4, Task 4: the Events page's Role switches — one list per event, composed from each raid's planner
(rc.role_plan); Switch records the answer before the lock (a notice, the whole evening) and sends that one ask after it
(RaidMixin.send_plan with `seats`)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import lock_with_board
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import event_board, event_view, inbox
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS
from oibot_gm.registry import RegistryError

HYJAL, RID = "hyjal_summit_forever", "barrow_deeps"
EVENING = [{"raid": RID, "rosters": 2}]
REAL = 473_000_000_000_000_000


@pytest.fixture(autouse=True)
def _no_board_solves(monkeypatch):
    monkeypatch.setattr(event_board.BoardCache, "start", lambda self, *a, **k: None)


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def add(reg, i: int, name: str, cls: str, spec: str, offspec: str | None = None, flex: list[str] | None = None, alt: tuple | None = None):
    reg.add_character(REAL + i, name, name, cls, spec, offspec, True)
    if flex:
        reg.set_flex(REAL + i, name, flex)
    if alt:
        reg.add_character(REAL + i, name, alt[0], alt[1], alt[2], None, False)
    return reg.members[REAL + i]


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def evening(reg, rs):
    return rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", lineup=EVENING)


def join(reg, rs, ev, *ms):
    for m in ms:
        rc.set_signup(reg, rs, ev, m, None, "in", source="test")


def post(web_bot, key, body):
    return web_bot.c.post(f"/api/run/{key}/switch", json=body, headers=H())


# ---------------------------------------------------------------- the list

def test_switches_list_one_line_per_person_across_raids(reg, rs):
    """Hyjal → BD ×2, joiners short of healers on both, Ilse (Balance, offspec Restoration) joined: one row for Ilse with
    raids == ['Hyjal Summit', 'Barrow Deeps'], from 'Balance', to 'Restoration', role 'healer'."""
    ev = evening(reg, rs)
    ilse = add(reg, 0, "Ilse", "Druid", "Balance", "Restoration", flex=["healer"])
    rook = add(reg, 1, "Rook", "Warrior", "Fury", "Protection")
    join(reg, rs, ev, ilse, rook, add(reg, 2, "Mage", "Mage", "Fire"))
    sw = rc.event_switches(reg, rs, ev)
    assert [s["raid"] for s in sw["short"]] == ["Hyjal Summit", "Barrow Deeps"]
    assert sw["short"][0]["roles"]["healer"] == 5 and sw["short"][1]["roles"]["healer"] == 4
    ilse_rows = [r for r in sw["rows"] if r["uid"] == str(ilse.discord_id)]
    assert len(ilse_rows) == 1, "one line per person, whatever raids it helps"
    row = ilse_rows[0]
    assert row["raids"] == ["Hyjal Summit", "Barrow Deeps"] and row["role"] == "healer"
    assert (row["from"], row["to"], row["answered"], row["spec"], row["character"]) == ("Balance", "Restoration", True, "Restoration", "Ilse")
    assert row["part"] == ev.key and set(sw["revs"]) == {p.key for p in rc.event_parts(rs, ev)}
    # the box says what the code does: one answer for the whole evening, so every raid of it
    assert row["box"]["verb"] == "Switch" and row["box"]["title"] == "Switch Ilse to Restoration?"
    assert row["box"]["sentences"][0] == "Ilse plays Restoration for Hyjal Summit and Barrow Deeps, not Balance."
    roles = [r["role"] for r in sw["rows"]]
    assert roles == sorted(roles, key=["tank", "healer", "melee", "ranged"].index), "ordered by role"
    rook_row = next(r for r in sw["rows"] if r["uid"] == str(rook.discord_id))
    assert (rook_row["from"], rook_row["to"], rook_row["role"]) == ("Fury", "Protection", "tank")
    for r in sw["rows"]:
        assert all(e.key not in w for e in rs.events.values() for w in (r["from"], r["to"], *r["raids"], *r["box"]["sentences"]))


def test_another_character_names_both_characters(reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", alt=("Nyxbear", "Druid", "Guardian"))
    join(reg, rs, ev, nyx)
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id) and r["role"] == "tank")
    assert (row["from"], row["to"]) == ("Nyx, Shadow", "Nyxbear, Guardian")


def test_switches_with_nobody_joined_are_answers_for_them(reg, rs):
    """No joiner: every row has answered False and from == 'no answer yet', and its box verb is 'Record answer'."""
    ev = evening(reg, rs)
    add(reg, 0, "Tavi", "Priest", "Holy")
    add(reg, 1, "Pal", "Paladin", "Protection")
    add(reg, 2, "Mage", "Mage", "Fire")
    sw = rc.event_switches(reg, rs, ev)
    assert sw["rows"]
    for r in sw["rows"]:
        assert r["answered"] is False and r["from"] == PAGE_WORDS["no_answer_yet"] and r["box"]["verb"] == "Record answer"
    tavi = next(r for r in sw["rows"] if r["display_name"] == "Tavi")
    assert tavi["to"] == "Holy" and tavi["raids"] == ["Hyjal Summit", "Barrow Deeps"]
    assert tavi["box"]["sentences"][0] == "Tavi is marked as joined, playing Holy."


def test_the_event_view_carries_the_switches_and_none_when_nothing_is_short(reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    add(reg, 0, "Tavi", "Priest", "Holy")
    j = event_view.event_json(reg, rs, ev, 1)
    assert j["switches"]["short"] == [{"raid": "Barrow Deeps", "roles": {"tank": 2, "healer": 2}}]
    assert j["switches"]["rows"]
    t1, t2, h1, h2 = (add(reg, 1, "Tanka", "Warrior", "Protection"), add(reg, 2, "Tankb", "Warrior", "Protection"),
                      add(reg, 3, "Heala", "Priest", "Holy"), add(reg, 4, "Healb", "Priest", "Holy"))
    join(reg, rs, ev, t1, t2, h1, h2)
    assert event_view.event_json(reg, rs, ev, 1)["switches"] == {"short": [], "rows": [], "revs": {ev.key: rc.role_plan(reg, rs, ev).rev}}


# ---------------------------------------------------------------- the verbs

def test_a_spec_the_character_cannot_play_is_refused(web_bot, reg, rs):
    bot = web_bot.bot
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    warrior = add(reg, 0, "Brick", "Warrior", "Arms", "Fury", flex=["tank"])
    with pytest.raises(RegistryError, match="doesn't play Holy"):
        asyncio.run(bot.set_answer(reg, rs, ev, warrior, "in", None, "o", by_id=1, spec="Holy"))
    assert str(warrior.discord_id) not in ev.signups
    asyncio.run(bot.set_answer(reg, rs, ev, warrior, "in", None, "o", by_id=1, spec="Protection"))  # a flex role's spec
    s = ev.signups[str(warrior.discord_id)]
    assert (s.spec, s.role, s.offspec) == ("Protection", "tank", "Arms")


def test_switch_before_the_lock_changes_the_answer_and_nothing_is_sent(web_bot, reg, rs):
    """POST /switch for Nyx Shadow → Holy: part 0's signup spec Holy, role healer; nothing DMed; Nyx has a notice. The
    later raid's answer follows (one answer for the whole evening), and the board picture made before it is stale."""
    ev = evening(reg, rs)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    join(reg, rs, ev, nyx)
    before = event_board.fingerprint(reg, rs, event_board.snapshot(rs, ev))
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id) and r["spec"] == "Holy")
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": rc.event_switches(reg, rs, ev)["revs"][row["part"]]}
    pv = post(web_bot, ev.key, body | {"preview": True})
    assert pv.status_code == 200 and pv.json()["box"]["title"] == "Switch Nyx to Holy?"
    r = post(web_bot, ev.key, body | {"seen": pv.json()["box"]["fp"]})
    assert r.status_code == 200, r.text
    s = ev.signups[str(nyx.discord_id)]
    assert (s.spec, s.role, s.status) == ("Holy", "healer", "in")
    later = rc.later_parts(rs, ev)[0]
    assert later.signups[str(nyx.discord_id)].spec == "Holy", "the later raid's answer is the first raid's"
    assert not any(web_bot.bot.dms.values()) and not ev.fill_asks
    assert any("Holy" in n["text"] for n in inbox.notices_of(reg, nyx.discord_id))
    assert event_board.fingerprint(reg, rs, event_board.snapshot(rs, ev)) != before, "a switch changes what the lock would place"
    assert "switches" in r.json()


def test_answer_for_them_before_the_lock_records_a_join(web_bot, reg, rs):
    ev = evening(reg, rs)
    tavi = add(reg, 0, "Tavi", "Priest", "Holy")
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(tavi.discord_id))
    r = post(web_bot, ev.key, {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"seen": row["box"]["fp"]})
    assert r.status_code == 200, r.text
    assert ev.signups[str(tavi.discord_id)].status == "in" and not any(web_bot.bot.dms.values())


def test_a_stale_box_is_refused_with_the_fresh_one(web_bot, reg, rs):
    ev = evening(reg, rs)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    join(reg, rs, ev, nyx)
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id))
    rc.set_signup(reg, rs, ev, nyx, None, "sub", source="test")  # they went to Bench meanwhile
    r = post(web_bot, ev.key, {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"seen": row["box"]["fp"]})
    assert r.status_code == 409 and r.json()["error"] == CONFIRM_WORDS["changed"] and r.json()["box"]["fp"] != row["box"]["fp"]
    assert ev.signups[str(nyx.discord_id)].status == "sub", "nothing written"


def locked_short_two_healers(reg, rs):
    """A locked Barrow Deeps: 2 tanks + 8 DPS seated (full), no healer; two DPS could heal (offspec Restoration)."""
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    ms = [add(reg, 0, "Sham", "Shaman", "Enhancement", "Restoration"), add(reg, 1, "Bear", "Druid", "Balance", "Restoration"),
          add(reg, 2, "Tanka", "Warrior", "Protection"), add(reg, 3, "Tankb", "Warrior", "Protection")]
    ms += [add(reg, 4 + i, f"Dps{chr(97 + i)}", "Mage", "Fire") for i in range(6)]
    join(reg, rs, ev, *ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    return ev, ms


def test_switch_after_the_lock_sends_only_that_ask(web_bot, reg, rs):
    """Locked with two short seats: POST /switch for seat 1 option 0 → exactly one FillAsk sent, for that person."""
    ev, ms = locked_short_two_healers(reg, rs)
    sw = rc.event_switches(reg, rs, ev)
    assert sw["short"] == [{"raid": "Barrow Deeps", "roles": {"healer": 2}}]
    row = next(r for r in sw["rows"] if r["seat"] == 1 and r["option"] == 0)
    assert row["box"]["verb"] == "Ask" and row["box"]["title"] == f"Ask {row['display_name']} to play Restoration?"
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": sw["revs"][ev.key], "seen": row["box"]["fp"]}
    r = post(web_bot, ev.key, body)
    assert r.status_code == 200, r.text
    sent = [a for a in ev.fill_asks if a.pending]
    assert len(sent) == 1 and str(sent[0].discord_id) == row["uid"] and sent[0].spec == "Restoration"
    assert ev.signups[row["uid"]].spec != "Restoration", "the seat changes only on a yes"


def test_a_changed_plan_is_refused_with_the_fresh_switches(web_bot, reg, rs):
    ev, ms = locked_short_two_healers(reg, rs)
    sw = rc.event_switches(reg, rs, ev)
    row = next(r for r in sw["rows"] if r["seat"] == 0 and r["option"] == 0)
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": "stale"}
    r = post(web_bot, ev.key, body)
    assert r.status_code == 409 and "switches" in r.json() and not ev.fill_asks


def test_send_plan_seats_sends_only_those_seats(web_bot, reg, rs):
    ev, ms = locked_short_two_healers(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    asks, _ = rc.send_batch(plan, seats=[0])
    assert len(asks) == 1 and asks[0].discord_id == plan.seats[0].options[0][0].discord_id
    assert len(rc.send_batch(plan)[0]) == 2, "no `seats`: every seat, as before"


def test_plain_text_and_mcp_carry_the_spec(web_bot, reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    op = ConfigOp(op="run_answer", target=ev.key, member="Nyx", value="join", field="Holy")
    assert "Nyx → Join, playing Holy (hasn't answered)" in configops.describe(reg, op)
    asyncio.run(configops.apply_async(reg, op, "Officer", True, bot=web_bot.bot, by_id=1))
    assert ev.signups[str(nyx.discord_id)].spec == "Holy"
    r = web_bot.c.post(f"/api/run/{ev.key}/set", json={"member": "Nyx", "status": "in", "spec": "Shadow"}, headers=H())
    assert r.status_code == 200, r.text
    assert ev.signups[str(nyx.discord_id)].spec == "Shadow"
    bad = web_bot.c.post(f"/api/run/{ev.key}/set", json={"member": "Nyx", "status": "in", "spec": "Fire"}, headers=H())
    assert bad.status_code == 400 and "doesn't play Fire" in bad.json()["error"]


def test_member_action_carries_the_spec(web_bot, reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    asyncio.run(inbox.do_member_action(web_bot.bot, reg, rs, ev, nyx, "join", spec="Holy", by="o", by_id=1))
    assert ev.signups[str(nyx.discord_id)].spec == "Holy"
