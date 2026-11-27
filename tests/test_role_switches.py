"""Phase 4, Task 4: the Events page's Role switches — one list per event, composed from each raid's planner
(rc.role_plan); Switch records the answer before the lock (a notice, the whole evening) and sends that one ask after it
(RaidMixin.send_plan with `seats`)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import OWNER, lock_with_board, weekly
from test_raid_night import H, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import event_view, inbox
from oibot_gm import raidcycle as rc
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS
from oibot_gm.registry import RegistryError

HYJAL, RID = "hyjal_summit_forever", "barrow_deeps"
EVENING = [{"raid": RID, "rosters": 2}]
REAL = 473_000_000_000_000_000


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
    assert j["switches"]["short"] == [{"raid": "Barrow Deeps", "roles": {"tank": 2, "healer": 2}, "why": None}]
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
    later raid's answer follows (one answer for the whole evening), and the lock's inputs change."""
    ev = evening(reg, rs)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    join(reg, rs, ev, nyx)
    before = rc.solve_inputs(ev)
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
    assert rc.solve_inputs(ev) != before, "a switch changes what the lock (and Auto-fill groups) reads"
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
    rc.set_pin(ev, nyx.discord_id, "in")  # another officer pinned them meanwhile
    rs.save(ev, "t: pin")
    r = post(web_bot, ev.key, {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"seen": row["box"]["fp"]})
    assert r.status_code == 409 and r.json()["error"] == CONFIRM_WORDS["changed"] and r.json()["box"]["fp"] != row["box"]["fp"]
    assert ev.signups[str(nyx.discord_id)].spec == "Shadow", "nothing written"


def locked_short_two_healers(reg, rs):
    """A locked Barrow Deeps: 2 tanks + 8 DPS seated (full), no healer; two DPS could heal (offspec Restoration)."""
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    ms = [add(reg, 0, "Sham", "Shaman", "Enhancement", "Restoration"), add(reg, 1, "Bear", "Druid", "Balance", "Restoration"),
          add(reg, 2, "Tanka", "Warrior", "Protection"), add(reg, 3, "Tankb", "Warrior", "Protection")]
    ms += [add(reg, 4 + i, f"Dps{chr(97 + i)}", "Mage", "Fire") for i in range(6)]
    join(reg, rs, ev, *ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    return ev, ms


def test_switch_after_the_lock_switches_a_rostered_seat_and_tells_nobody(web_bot, reg, rs):
    """Locked with two short seats (2026-11-27): a rostered person's row is Switch — POST /switch changes their seat at
    once; no ask, no DM, no notice."""
    ev, ms = locked_short_two_healers(reg, rs)
    sw = rc.event_switches(reg, rs, ev)
    assert sw["short"] == [{"raid": "Barrow Deeps", "roles": {"healer": 2}, "why": None}]
    row = next(r for r in sw["rows"] if r["seat"] == 1 and r["option"] == 0)
    assert row["button"] == PAGE_WORDS["switch_button"]
    assert row["box"]["verb"] == "Switch" and row["box"]["title"] == f"Switch {row['display_name']} to Restoration?"
    assert "Nobody gets a DM." in row["box"]["sentences"]
    notices = len(reg.notices(int(row["uid"])))
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": sw["revs"][ev.key], "seen": row["box"]["fp"]}
    r = post(web_bot, ev.key, body)
    assert r.status_code == 200, r.text
    assert not [a for a in ev.fill_asks if a.pending] and not any(web_bot.bot.dms.values())
    seat = ev.seat_of(int(row["uid"]))[1]
    assert (seat.spec, seat.role) == ("Restoration", "healer") and len(reg.notices(int(row["uid"]))) == notices


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


# ---------------------------------------------------------------- fix round 1

def test_a_spec_switch_of_a_rostered_seat_after_the_lock_is_applied_on_every_surface(web_bot, reg, rs):
    """After the lock an officer's switch of someone rostered is applied to their seat at once (2026-11-27): set_answer
    (spec=), the site's /set (MCP set_answer) and plain text all write it; nobody is asked."""
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    ev, ms = locked_short_two_healers(reg, rs)
    sham, bear = ms[0], ms[1]
    asyncio.run(web_bot.bot.set_answer(reg, rs, ev, sham, "in", None, "o", by_id=1, spec="Restoration"))
    assert (ev.seat_of(sham)[1].spec, ev.signups[str(sham.discord_id)].spec) == ("Restoration", "Restoration")
    r = web_bot.c.post(f"/api/run/{ev.key}/set", json={"member": "Sham", "status": "in", "spec": "Enhancement"}, headers=H())  # MCP set_answer
    assert r.status_code == 200, r.text
    assert ev.seat_of(sham)[1].spec == "Enhancement"
    asyncio.run(configops.apply_async(reg, ConfigOp(op="run_answer", target=ev.key, member="Bear", value="join", field="Restoration"),
                                      "Officer", True, bot=web_bot.bot, by_id=1))
    assert ev.seat_of(bear)[1].spec == "Restoration"
    assert not ev.fill_asks and not any(web_bot.bot.dms.values())


def test_a_short_raid_without_rows_says_why(reg, rs):
    """Regulars who haven't answered count as coming before the lock; a raid whose only move is a swap needing a cover
    points at Suggest…; nobody free → its own sentence. Never 'Show 0 possible switches' with no words."""
    from oibot_gm.raid_buttons import RUN_WORDS

    # regulars counted as coming: a recurring run whose two healer regulars haven't answered
    sid = weekly(reg, RID, "Tue 19:30", sid="tue")
    healers = [add(reg, i, n, "Priest", "Holy") for i, n in enumerate(("Heala", "Healb"))]
    healers += [add(reg, 2 + i, n, "Warrior", "Protection") for i, n in enumerate(("Tanka", "Tankb"))]
    for h in healers:
        reg.regular_add(h.discord_id, sid, "t")
    start = next(t for s_, t in rc.occurrences(reg, reg.now_local(), 24 * 21) if s_["id"] == sid)
    ev = rc.open_run(reg, rs, RID, start, by="t", schedule=sid)
    sw = rc.event_switches(reg, rs, ev)
    bd = next(x for x in sw["short"] if x["raid"] == "Barrow Deeps")
    assert "healer" in bd["roles"] and not [r for r in sw["rows"] if r["role"] == "healer"]
    assert bd["why"] == PAGE_WORDS["short_counted"]


def test_short_why_for_paired_swaps_and_nobody(reg, rs):
    from oibot_gm.raid_buttons import RUN_WORDS

    plan = rc.Plan(phase="prelock", short={"headcount": 0, "roles": {"tank": 1}}, have={}, need={}, full=False,
                   short_after={"headcount": 0, "roles": {"tank": 1}},
                   seats=[rc.PlanSeat(role="tank", options=[[rc.FillAsk(discord_id=1, display_name="A", kind="offspec", role="tank", character="A", spec="Protection", reason="short 1 tank"),
                                                              rc.FillAsk(discord_id=2, display_name="B", kind="sub", role="healer", character="B", spec="Holy", reason="covering")]])])
    assert rc._short_why(plan, {"tank": 1}, True) == PAGE_WORDS["short_cover"].format(screen=RUN_WORDS["suggest"])
    assert rc._short_why(plan, {"tank": 1}, False) == PAGE_WORDS["short_cover"].format(screen=RUN_WORDS["fill"])
    plan.seats = [rc.PlanSeat(role="tank")]
    assert rc._short_why(plan, {"tank": 1}, True) == PAGE_WORDS["short_nobody"]
    plan.short, plan.expected = {"headcount": 0, "roles": {}}, 1
    assert rc._short_why(plan, {"tank": 1}, True) == PAGE_WORDS["short_counted"]
    plan.waiting = ["A"]
    assert rc._short_why(plan, {"tank": 1}, True) == PAGE_WORDS["short_asked"]


def test_nobody_free_reads_its_sentence_on_the_event(reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    join(reg, rs, ev, add(reg, 0, "Mage", "Mage", "Fire"))
    sw = rc.event_switches(reg, rs, ev)
    assert sw["rows"] == [] and {x["why"] for x in sw["short"]} == {PAGE_WORDS["short_nobody"]}


def test_every_row_carries_its_button_word(web_bot, reg, rs):
    """Switch (a joined member), Answer for them (no answer yet), and after the lock Ask — for a Bench / unanswered
    person too; the officer's own joined row before the lock is Switch with a box said to them."""
    ev = evening(reg, rs)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    add(reg, 1, "Tavi", "Priest", "Holy")
    reg.add_character(OWNER, "Boss", "Boss", "Druid", "Balance", "Restoration", True)
    me = reg.members[OWNER]
    join(reg, rs, ev, nyx, me)
    rows = rc.event_switches(reg, rs, ev, viewer=OWNER)["rows"]
    by = {r["display_name"]: r for r in rows if r["role"] == "healer"}
    assert by["Nyx"]["button"] == PAGE_WORDS["switch_button"] and by["Nyx"]["box"]["verb"] == "Switch"
    assert by["Tavi"]["button"] == PAGE_WORDS["answer_button"] and by["Tavi"]["box"]["verb"] == "Record answer"
    mine = by["Boss"]
    assert mine["button"] == PAGE_WORDS["switch_button"] and mine["box"]["title"] == "Switch to Restoration?"
    assert mine["box"]["sentences"] == ["You play Restoration for Hyjal Summit and Barrow Deeps, not Balance.", "You can switch back until the event starts."]
    ev2, ms = locked_short_two_healers(reg, rs)
    after = rc.event_switches(reg, rs, ev2, viewer=OWNER)["rows"]  # rostered people: their seat switches at once
    assert after and {r["button"] for r in after} == {PAGE_WORDS["switch_button"]}


def test_the_officers_own_row_is_left_out_after_the_lock(reg, rs):
    ev, ms = locked_short_two_healers(reg, rs)
    sham = ms[0]
    assert any(r["uid"] == str(sham.discord_id) for r in rc.event_switches(reg, rs, ev)["rows"])
    assert not any(r["uid"] == str(sham.discord_id) for r in rc.event_switches(reg, rs, ev, viewer=sham.discord_id)["rows"])


def test_the_box_names_only_the_raids_the_member_stays_for(reg, rs):
    ev = evening(reg, rs)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    join(reg, rs, ev, nyx)
    later = rc.later_parts(rs, ev)[0]
    later.leaving.append(nyx.discord_id)
    rs.save(later, "t: Nyx leaves before Barrow Deeps")
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id))
    assert row["box"]["sentences"][0] == "Nyx plays Holy for Hyjal Summit, not Shadow."


def test_a_row_no_longer_on_the_list_is_refused_before_the_lock(web_bot, reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    join(reg, rs, ev, nyx)
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id))
    join(reg, rs, ev, add(reg, 1, "Heala", "Priest", "Holy"), add(reg, 2, "Healb", "Priest", "Holy"))  # no longer short of healers
    r = post(web_bot, ev.key, {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"seen": row["box"]["fp"]})
    assert r.status_code == 409 and "switches" in r.json() and r.json()["error"]
    assert ev.signups[str(nyx.discord_id)].spec == "Shadow"


def test_a_bad_row_is_a_refusal_box(web_bot, reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    r = post(web_bot, ev.key, {"uid": "12", "character": "Nobody", "spec": "Holy"})
    assert r.status_code == 400 and r.json()["box"]["verb"] is None and r.json()["box"]["sentences"] == [r.json()["error"]]


def test_a_switch_is_logged_and_audited_with_the_officer_and_the_spec(web_bot, reg, rs):
    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    join(reg, rs, ev, nyx)
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id))
    assert post(web_bot, ev.key, {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")}).status_code == 200
    assert any("Holy" in ln and "switched by" in ln for ln in ev.log)
    lines = [str(x) for x in web_bot.bot.ops.lines] if hasattr(web_bot.bot.ops, "lines") else []
    audited = [ln for ln in lines if "switched Nyx to Holy" in ln]
    assert audited and "[mcp]" in audited[-1], lines[-5:]


def test_after_the_lock_a_second_press_of_the_same_row_switches_nothing_more(web_bot, reg, rs):
    ev, ms = locked_short_two_healers(reg, rs)
    for m in ms:
        m.dm_opt_out = True
    sw = rc.event_switches(reg, rs, ev)
    row = next(r for r in sw["rows"] if r["seat"] == 0 and r["option"] == 0)
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": sw["revs"][ev.key]}
    assert post(web_bot, ev.key, body).status_code == 200
    specs = {p.signup_name: p.spec for p in ev.seated()}
    again = post(web_bot, ev.key, body)
    assert again.status_code == 409 and "switches" in again.json()
    assert {p.signup_name: p.spec for p in ev.seated()} == specs and not ev.fill_asks and not any(web_bot.bot.dms.values())


def test_a_raid_whose_only_switch_needs_a_cover_points_at_suggest(reg, rs):
    """Two tanks joined (the minimum), one could heal; leaving tank would drop under its minimum, so the only move is a
    swap tied to a backfill: no row, and the raid says Suggest… has it."""
    from oibot_gm.raid_buttons import RUN_WORDS

    ev = rc.open_run(reg, rs, RID, start_in(reg), by="t")
    join(reg, rs, ev, add(reg, 0, "Pally", "Paladin", "Protection", "Holy"), add(reg, 1, "Tanka", "Warrior", "Protection"))
    add(reg, 2, "Tankb", "Warrior", "Protection")  # hasn't answered: the cover for the tank seat
    plan = rc.role_plan(reg, rs, ev)
    assert any(len(o) == 2 for st in plan.seats if st.role == "healer" for o in st.options)
    sw = rc.event_switches(reg, rs, ev)
    assert sw["rows"] == []
    assert sw["short"][0]["why"] == PAGE_WORDS["short_cover"].format(screen=RUN_WORDS["suggest"])


@pytest.fixture
def wide(store, profile):
    """A profile copy with two more raids (as tests/test_lineups.py): a four-raid lineup, no profile file changed."""
    import copy

    from conftest import GUILD, TEST_MEMBERS

    from oibot_gm.registry import Registry

    p = copy.deepcopy(profile)
    for rid, name in (("extra_a", "Extra A"), ("extra_b", "Extra B")):
        p.raids[rid] = {**copy.deepcopy(p.raids[RID]), "id": rid, "name": name, "duration_hours": 2}
    r = Registry(store, GUILD, p)
    r.seed_test_members(TEST_MEMBERS, "t")
    for rid in r.profile.raids:
        r.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")
    return r


def test_a_four_raid_event_lists_one_row_for_every_raid_it_helps(wide, rs):
    lineup = [{"raid": RID, "rosters": 2}, {"raid": "extra_a", "rosters": 1}, {"raid": "extra_b", "rosters": 1}]
    ev = rc.open_run(wide, rs, HYJAL, start_in(wide), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=lineup)
    assert len(rc.event_parts(rs, ev)) == 4
    ilse = add(wide, 0, "Ilse", "Druid", "Balance", "Restoration", flex=["healer"])
    join(wide, rs, ev, ilse)
    sw = rc.event_switches(wide, rs, ev)
    assert [x["raid"] for x in sw["short"]] == ["Hyjal Summit", "Barrow Deeps", "Extra A", "Extra B"]
    rows = [r for r in sw["rows"] if r["uid"] == str(ilse.discord_id)]
    assert len(rows) == 1 and rows[0]["raids"] == ["Hyjal Summit", "Barrow Deeps", "Extra A", "Extra B"]
    assert rows[0]["box"]["sentences"][0] == "Ilse plays Restoration for Hyjal Summit, Barrow Deeps, Extra A and Extra B, not Balance."


# ---------------------------------------------------------------- task 7 fix round 1

def test_a_row_its_box_refuses_is_left_out_not_the_whole_list(reg, rs, web_bot, monkeypatch):
    """A row whose box now refuses (a spec they no longer play, a retired character) is skipped; the list and
    GET /api/event/{key} still answer with every other row."""
    ev = evening(reg, rs)
    ilse = add(reg, 0, "Ilse", "Druid", "Balance", "Restoration", flex=["healer"])
    rook = add(reg, 1, "Rook", "Warrior", "Fury", "Protection")
    join(reg, rs, ev, ilse, rook, add(reg, 2, "Mage", "Mage", "Fire"))
    real = rc.switch_box_for

    def refusing(reg_, rs_, part, m, *a, **kw):
        if m.discord_id == ilse.discord_id:
            raise RegistryError("Ilse doesn't play Restoration.")
        return real(reg_, rs_, part, m, *a, **kw)

    monkeypatch.setattr(rc, "switch_box_for", refusing)
    uids = {r["uid"] for r in rc.event_switches(reg, rs, ev)["rows"]}
    assert str(ilse.discord_id) not in uids and str(rook.discord_id) in uids
    r = web_bot.c.get(f"/api/event/{ev.key}", headers=H())
    assert r.status_code == 200, r.text
