"""GitHub issue 15: before the lock an officer switches a person's role / spec for ONE raid of a multi-raid event (the
owner: "a Priest plays Shadow in 20 man, but in 10 mans switches to Holy to heal"). The switch is stored on that raid
(RaidEvent.switched, keyed by the board's name) and read through ONE function, rc.plays_in — the board, needs, the
planner, Auto-fill groups, Preview lock, both locks, Unlock and the cards all see the same spec."""
from __future__ import annotations

from datetime import timedelta

import pytest
from test_raid_night import NightBot, run, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError

HYJAL, BD = "hyjal_summit_forever", "barrow_deeps"
EVENING = [{"raid": BD, "rosters": 2}]  # Hyjal Summit (20) then Barrow Deeps ×2 (10 each)
BASE = 474_000_000_000_000_000


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def add(reg, i: int, name: str, cls: str, spec: str, offspec: str | None = None, alt: tuple | None = None):
    reg.add_character(BASE + i, name, name, cls, spec, offspec, True)
    if alt:
        reg.add_character(BASE + i, name, alt[0], alt[1], alt[2], None, False)
    return reg.members[BASE + i]


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def evening(reg, rs, extra_dps: int = 0):
    """The owner's case: 3 tanks, 5 Holy priests, Nyx (Shadow, offspec Holy) and 11 + `extra_dps` mages joined (20 for
    the 20-man and the two 10-mans, the 20-man's healer minimum met without Nyx)."""
    lead = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", lineup=EVENING)
    ms = [add(reg, 0, "Nyx", "Priest", "Shadow", "Holy", alt=("Nyxpal", "Paladin", "Holy"))]
    ms += [add(reg, 1 + i, f"Tank{chr(97 + i)}", "Warrior", "Protection") for i in range(3)]
    ms += [add(reg, 4 + i, f"Heal{chr(97 + i)}", "Priest", "Holy") for i in range(5)]
    ms += [add(reg, 9 + i, f"Mage{chr(97 + i)}", "Mage", "Fire") for i in range(11 + extra_dps)]
    for m in ms:
        rc.set_signup(reg, rs, lead, m, None, "in", source="test")
    lead = rs.events[lead.key]
    return lead, rc.later_parts(rs, lead)[0], ms


def seat(ev, name):
    return next((p for p in ev.seated() if p.signup_name == name), None)


def place_all(rs, ev, names):
    ev.layout = [names[i:i + 5] for i in range(0, len(names), 5)]
    rs.save(ev, "board (test)")


def holy_for_bd(reg, rs, fev, nyx):
    return rc.set_raid_switch(reg, rs, fev, nyx, "Nyx", "Holy", by="Jon")


# ---------------------------------------------------------------- the owner's case, end to end

def test_the_owners_case_shadow_for_the_20_man_and_holy_for_the_10_man(reg, rs):
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    healers_before = (rc.gaps(reg, lead)["have"]["healer"], rc.gaps(reg, fev)["have"]["healer"])
    holy_for_bd(reg, rs, fev, nyx)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    sg_lead, sg_fev = lead.signups[str(nyx.discord_id)], fev.signups[str(nyx.discord_id)]
    assert sg_lead.spec == sg_fev.spec == "Shadow", "their answer stays as it is"
    assert (rc.plays_in(reg, lead, sg_lead).spec, rc.plays_in(reg, lead, sg_lead).role) == ("Shadow", "ranged")
    assert (rc.plays_in(reg, fev, sg_fev).spec, rc.plays_in(reg, fev, sg_fev).role) == ("Holy", "healer")
    # needs / the Short facts: one more healer on the 10-man only
    assert rc.gaps(reg, lead)["have"]["healer"] == healers_before[0]
    assert rc.gaps(reg, fev)["have"]["healer"] == healers_before[1] + 1
    # the board's member line (players_for → board_rosters)
    names = [m.display_name for m in ms]
    place_all(rs, lead, names)
    place_all(rs, fev, names)
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    on = lambda ev: next(p for r in rc.board_rosters(reg, ev, rc.board_layout(reg, ev), rc.run_size(reg, ev)) for p in r.selected if p.signup_name == "Nyx")  # noqa: E731
    assert (on(lead).spec, on(fev).spec) == ("Shadow", "Holy")
    assert "Nyx" in fev.layout[0], "a switch keeps their board place"
    # the lock as it stands
    bot = NightBot(reg, rs)
    run(bot.lock_run(reg, rs, lead, by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == fev.state == "locked"
    assert (seat(lead, "Nyx").spec, seat(lead, "Nyx").role) == ("Shadow", "ranged")
    assert (seat(fev, "Nyx").spec, seat(fev, "Nyx").role) == ("Holy", "healer")
    roster = [[list(g) for g in r.groups] for r in fev.rosters]
    specs = {p.signup_name: p.spec for p in fev.seated()}
    # Unlock: the switch stands (one truth: no played_as mark beside it), the board shows Holy, the relock is identical
    rc.unlock(reg, rs, lead, "Jon")
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert fev.state == "open" and fev.switched["Nyx"].spec == "Holy" and "Nyx" not in fev.played_as
    assert rc.plays_in(reg, fev, fev.signups[str(nyx.discord_id)]).spec == "Holy"
    run(bot.lock_run(reg, rs, lead, by="Jon", fill=False))
    fev = rs.events[fev.key]
    assert [[list(g) for g in r.groups] for r in fev.rosters] == roster and {p.signup_name: p.spec for p in fev.seated()} == specs


def test_the_fill_lock_preview_and_auto_fill_place_them_holy_on_the_10_man(reg, rs):
    lead, fev, ms = evening(reg, rs)
    holy_for_bd(reg, rs, fev, ms[0])
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    pv = rc.event_board_preview(reg, rs, lead)
    pick = lambda rosters: next((p for r in rosters for p in r.selected if p.signup_name == "Nyx"), None)  # noqa: E731
    assert pick(pv[lead.key]).spec == "Shadow" and pick(pv[fev.key]).spec == "Holy", "Preview lock"
    layout, _auto = rc.autofill(reg, rs, fev, earlier=rc.earlier_counts(reg, rs, fev))
    assert any("Nyx" in g for g in layout), "Auto-fill groups place them (20 joined, 20 seats)"
    fev.layout, fev.auto_placed = layout, [n for g in layout for n in g]
    rs.save(fev, "auto-fill (test)")
    fev = rs.events[fev.key]
    assert next(p for r in rc.board_rosters(reg, fev, rc.board_layout(reg, fev), 10) for p in r.selected if p.signup_name == "Nyx").spec == "Holy"
    fev.layout, fev.auto_placed = None, []
    rs.save(fev, "clear (test)")
    run(NightBot(reg, rs).lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=True))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.state == fev.state == "locked"
    assert seat(lead, "Nyx").spec == "Shadow" and seat(fev, "Nyx").spec == "Holy"


def test_the_fill_never_seats_them_on_the_10_man_in_another_spec(reg, rs):
    """A fixed role: the switched Player has no offspec, so the solver can leave them out but never switch them back."""
    lead, fev, ms = evening(reg, rs, extra_dps=4)
    holy_for_bd(reg, rs, fev, ms[0])
    fev = rs.events[fev.key]
    p = next(p for p in rc.players_for(reg, fev) if p.signup_name == "Nyx")
    assert (p.spec, p.role, p.offspec) == ("Holy", "healer", None)
    run(NightBot(reg, rs).lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=True))
    fev = rs.events[fev.key]
    s = seat(fev, "Nyx")
    assert s is None or (s.spec, s.role) == ("Holy", "healer")


# ---------------------------------------------------------------- back, replaced, dropped

def test_switch_back_removes_the_switch(reg, rs):
    lead, fev, ms = evening(reg, rs)
    holy_for_bd(reg, rs, fev, ms[0])
    assert rc.set_raid_switch(reg, rs, rs.events[fev.key], ms[0], "Nyx", "Shadow", by="Jon") == "cleared"
    fev = rs.events[fev.key]
    assert "Nyx" not in fev.switched and rc.plays_in(reg, fev, fev.signups[str(ms[0].discord_id)]).spec == "Shadow"
    assert any("Shadow again" in line for line in fev.log)


def test_a_whole_event_switch_replaces_the_per_raid_one(reg, rs):
    lead, fev, ms = evening(reg, rs)
    holy_for_bd(reg, rs, fev, ms[0])
    rc.set_signup(reg, rs, rs.events[lead.key], ms[0], "Nyx", "in", source="officer", spec="Holy", by="Jon")
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert not fev.switched and lead.signups[str(ms[0].discord_id)].spec == "Holy"


def test_their_own_change_drops_a_switch_that_no_longer_makes_sense(reg, rs):
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    holy_for_bd(reg, rs, fev, nyx)
    rc.set_signup(reg, rs, rs.events[lead.key], nyx, "Nyxpal", "in", source="member")  # another character: the switch is gone
    fev = rs.events[fev.key]
    assert not fev.switched and any("switch dropped" in line for line in fev.log)


def test_their_own_bench_keeps_a_switch_and_no_thanks_drops_it(reg, rs):
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    holy_for_bd(reg, rs, fev, nyx)
    rc.set_signup(reg, rs, rs.events[lead.key], nyx, None, "sub", source="member")
    assert "Nyx" in rs.events[fev.key].switched, "Join ↔ Bench keeps it, as a board place"
    rc.set_signup(reg, rs, rs.events[lead.key], nyx, None, "out", source="member")
    assert not rs.events[fev.key].switched


def test_another_character_for_one_raid(reg, rs):
    lead, fev, ms = evening(reg, rs)
    rc.set_raid_switch(reg, rs, fev, ms[0], "Nyxpal", "Holy", by="Jon")
    fev = rs.events[fev.key]
    p = rc.plays_in(reg, fev, fev.signups[str(ms[0].discord_id)])
    assert (p.character, p.cls, p.spec, p.role) == ("Nyxpal", "Paladin", "Holy", "healer")
    assert next(x for x in rc.players_for(reg, fev) if x.signup_name == "Nyx").character == "Nyxpal"


def test_a_spec_the_character_does_not_play_is_refused(reg, rs):
    lead, fev, ms = evening(reg, rs)
    with pytest.raises(RegistryError, match="Nyx doesn't play Fire."):
        rc.set_raid_switch(reg, rs, fev, ms[0], "Nyx", "Fire", by="Jon")


def test_a_single_raid_event_offers_no_scope(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, ev, nyx, None, "in", source="test")
    assert rc.raid_switch_options(reg, rs, ev, nyx) == []
    with pytest.raises(RegistryError):
        rc.set_raid_switch(reg, rs, ev, nyx, "Nyx", "Holy", by="Jon")


def test_an_old_event_without_the_field_loads_as_today(reg, rs):
    lead, fev, ms = evening(reg, rs)
    raw = fev.model_dump(exclude={"switched"})
    old = rc.RaidEvent.model_validate(raw)
    assert old.switched == {}
    assert [p.spec for p in rc.players_for(reg, old)] == [p.spec for p in rc.players_for(reg, fev)]


# ---------------------------------------------------------------- the cards

def test_the_lock_card_and_the_confirm_card_draw_the_raids_own_spec(reg, rs):
    from test_analytics_cards import text_of

    from oibot_gm import raid_views as views

    ico = lambda kind, key, *a, **k: f"<{kind}:{key}>"  # noqa: E731
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    holy_for_bd(reg, rs, fev, nyx)
    names = [m.display_name for m in ms]
    place_all(rs, rs.events[lead.key], names)
    place_all(rs, rs.events[fev.key], names)
    run(NightBot(reg, rs).lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    i = next(k for k, r in enumerate(fev.rosters) if any(p.signup_name == "Nyx" for p in r.selected))
    assert "<spec:Priest:Holy> Nyx" in text_of(views.lock_layout(reg, fev, rc.run_team(reg, fev), ico, i, rs))
    assert "<spec:Priest:Shadow> Nyx" in text_of(views.lock_layout(reg, lead, rc.run_team(reg, lead), ico, 0, rs))
    sg = fev.signups[str(nyx.discord_id)]
    gi = next(k for k, g in enumerate(fev.rosters[i].groups) if "Nyx" in g)
    assert "<spec:Priest:Holy> **Nyx**" in text_of(views.confirm_layout(reg, fev, rc.run_team(reg, fev), ico, sg, i, gi, nyx.discord_id))
    rc.unlock(reg, rs, lead, "Jon")
    fev = rs.events[fev.key]
    assert "<spec:Priest:Holy> **Nyx**" in text_of(views.confirm_open_layout(reg, fev, ico, fev.signups[str(nyx.discord_id)], nyx.discord_id))


# ---------------------------------------------------------------- who is told; the planner; the Me page

def test_the_member_gets_one_notice_and_the_me_row_says_what_they_play(reg, rs):
    from oibot_gm import inbox
    from oibot_gm.web.api_me import me_sheet_row

    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    bot = NightBot(reg, rs)
    line = run(bot.set_raid_switch(reg, rs, fev, nyx, "Nyx", "Holy", "Jon", by_id=1, via="web"))
    assert line == "✅ Nyx plays Holy for Barrow Deeps"
    notes = [n["text"] for n in inbox.notices_of(reg, nyx.discord_id)]
    assert len(notes) == 1 and "Jon switched you to Holy for Barrow Deeps" in notes[0]
    assert not any(bot.dms.values()), "the DM rule: a notice, never a DM"
    assert me_sheet_row(reg, rs, rs.events[fev.key], nyx, nyx.discord_id)["plays"] == "You play Holy for Barrow Deeps."
    assert me_sheet_row(reg, rs, rs.events[lead.key], nyx, nyx.discord_id)["plays"] is None
    assert any("switched by Jon" in ln for ln in rs.events[fev.key].log)


def test_the_planner_stops_listing_a_raid_no_longer_short_and_never_suggests_them_again(reg, rs):
    lead = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", lineup=EVENING)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, lead, nyx, None, "in", source="test")
    fev = rc.later_parts(rs, lead)[0]
    before = rc.event_switches(reg, rs, lead)
    row = next(r for r in before["rows"] if r["uid"] == str(nyx.discord_id) and r["spec"] == "Holy")
    assert set(row["raids"]) == {"Hyjal Summit", "Barrow Deeps"} and row["scope"] == "event"
    rc.set_raid_switch(reg, rs, fev, nyx, "Nyx", "Holy", by="Jon")
    after = rc.event_switches(reg, rs, rs.events[lead.key])
    nyx_rows = [r for r in after["rows"] if r["uid"] == str(nyx.discord_id)]
    assert all("Barrow Deeps" not in r["raids"] for r in nyx_rows), "switched for that raid: not suggested again there"
    bd = next(s for s in after["short"] if s["raid"] == "Barrow Deeps")
    assert bd["roles"].get("healer", 0) == next(s for s in before["short"] if s["raid"] == "Barrow Deeps")["roles"]["healer"] - 1


# ---------------------------------------------------------------- the boxes (the Confirm rule's sentences)

def test_the_box_sentences(reg, rs):
    from oibot_gm import consequences as cq

    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    b = cq.raid_switch_box(reg, rs, fev, nyx, "Nyx", "Holy")
    assert b["title"] == "Switch Nyx to Holy for Barrow Deeps?" and b["verb"] == "Switch"
    assert b["sentences"] == ["Nyx plays Holy for Barrow Deeps and stays Shadow for Hyjal Summit.",
                              "They get a notice, and nobody gets a DM.", "You can switch them back until the event starts."]
    own = cq.raid_switch_box(reg, rs, fev, nyx, "Nyx", "Holy", own=True)
    assert own["sentences"] == ["You play Holy for Barrow Deeps and stay Shadow for Hyjal Summit.", "You can switch back until the event starts."]
    alt = cq.raid_switch_box(reg, rs, fev, nyx, "Nyxpal", "Holy")
    assert alt["sentences"][0] == "Nyx plays Nyxpal, Holy for Barrow Deeps and stays Nyx, Shadow for Hyjal Summit."
    rc.set_raid_switch(reg, rs, fev, nyx, "Nyx", "Holy", by="Jon")
    back = cq.raid_switch_box(reg, rs, rs.events[fev.key], nyx, "Nyx", "Shadow")
    assert back["title"] == "Switch Nyx back to Shadow for Barrow Deeps?"
    assert back["sentences"] == ["Nyx plays Shadow again for Barrow Deeps, as they answered.", "They get a notice, and nobody gets a DM.",
                                 "You can switch them again until the event starts."]
    with pytest.raises(RegistryError, match="already plays Holy for Barrow Deeps"):
        cq.raid_switch_box(reg, rs, rs.events[fev.key], nyx, "Nyx", "Holy")
    # the whole-event box says it replaces the switch for that raid
    whole, _b = rc.switch_box_for(reg, rs, rs.events[lead.key], nyx, "Nyx", "Holy")
    assert whole["sentences"][-1] == "This replaces their switch for Barrow Deeps."


# ---------------------------------------------------------------- the surfaces

def test_site_board_menu_lists_the_raid_switches_and_play_as_writes(web_bot, reg, rs):
    from test_raid_night import H

    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    c = web_bot.c
    got = c.get(f"/api/admin/actions?run={fev.key}&member={nyx.discord_id}", headers=H()).json()
    assert got["raid_switches"] == [{"character": "Nyx", "spec": "Holy", "cls": "Priest", "label": "Play as Holy in this raid"}]
    body = {"uid": str(nyx.discord_id), "character": "Nyx", "spec": "Holy"}
    pv = c.post(f"/api/run/{fev.key}/play-as", json=body | {"preview": True}, headers=H())
    assert pv.status_code == 200 and pv.json()["box"]["title"] == "Switch Nyx to Holy for Barrow Deeps?"
    stale = c.post(f"/api/run/{fev.key}/play-as", json=body | {"seen": "0" * 12}, headers=H())
    assert stale.status_code == 409 and stale.json()["box"]["fp"] == pv.json()["box"]["fp"] and not rs.events[fev.key].switched
    r = c.post(f"/api/run/{fev.key}/play-as", json=body | {"seen": pv.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    assert rs.events[fev.key].switched["Nyx"].spec == "Holy" and rs.events[lead.key].signups[str(nyx.discord_id)].spec == "Shadow"
    again = c.get(f"/api/admin/actions?run={fev.key}&member={nyx.discord_id}", headers=H()).json()["raid_switches"]
    assert [o["label"] for o in again] == ["Play as Shadow again in this raid"]
    bad = c.post(f"/api/run/{fev.key}/play-as", json={"uid": str(nyx.discord_id), "character": "Nyx", "spec": "Fire", "preview": True}, headers=H())
    assert bad.status_code == 400 and bad.json()["box"]["sentences"] == ["Nyx doesn't play Fire."]


def test_site_role_switches_box_offers_the_scope_and_writes_one_raid(web_bot, reg, rs):
    from test_raid_night import H

    lead = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", lineup=EVENING)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, lead, nyx, None, "in", source="test")
    for i in range(4):  # the two 10-mans' healers met: only Hyjal Summit (5) is short of one
        h = add(reg, 1 + i, f"Heal{chr(97 + i)}", "Priest", "Holy")
        rc.set_signup(reg, rs, rs.events[lead.key], h, None, "in", source="test")
    lead = rs.events[lead.key]
    fev = rc.later_parts(rs, lead)[0]
    sw = rc.event_switches(reg, rs, lead)
    row = next(r for r in sw["rows"] if r["uid"] == str(nyx.discord_id))
    assert row["raids"] == ["Hyjal Summit"] and row["scope"] == lead.key and row["part"] == lead.key
    scope = row["box"]["scope"]
    assert scope["value"] == lead.key and scope["label"] == rc.SCOPE_LABEL
    assert scope["options"] == [{"value": lead.key, "label": "Hyjal Summit"}, {"value": "event", "label": "The whole event"}]
    assert row["box"]["sentences"][0] == "Nyx plays Holy for Hyjal Summit and stays Shadow for Barrow Deeps."
    body = {k: row[k] for k in ("uid", "character", "spec", "part", "seat", "option")} | {"rev": sw["revs"][row["part"]]}
    url = f"/api/run/{lead.key}/switch"
    whole = web_bot.c.post(url, json=body | {"preview": True, "scope": "event"}, headers=H()).json()["box"]
    assert whole["scope"]["value"] == "event" and whole["sentences"][0] == "Nyx plays Holy for Hyjal Summit and Barrow Deeps, not Shadow."
    stale = web_bot.c.post(url, json=body | {"scope": lead.key, "seen": whole["fp"]}, headers=H())
    assert stale.status_code == 409 and stale.json()["box"]["fp"] == row["box"]["fp"]
    r = web_bot.c.post(url, json=body | {"scope": lead.key, "seen": row["box"]["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.switched["Nyx"].spec == "Holy" and not fev.switched and lead.signups[str(nyx.discord_id)].spec == "Shadow"
    assert rc.plays_in(reg, fev, fev.signups[str(nyx.discord_id)]).spec == "Shadow"


def test_a_single_raid_events_switch_box_has_no_scope(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, ev, nyx, None, "in", source="test")
    row = next(r for r in rc.event_switches(reg, rs, ev)["rows"] if r["uid"] == str(nyx.discord_id))
    assert row["scope"] is None and "scope" not in row["box"]


def test_mcp_set_answer_with_a_raid(web_bot, reg, rs):
    import json

    import httpx
    from test_mcp import TOKEN
    from test_raid_night import H

    from oibot_gm import mcp_server as ms

    lead, fev, mems = evening(reg, rs)
    sent = []

    def handler(request):
        sent.append(json.loads(request.read()))
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        ms.set_answer(lead.key, "Nyx", "in", spec="Holy", raid="Barrow Deeps")
        with pytest.raises(Exception, match="a raid goes with a spec"):
            ms.set_answer(lead.key, "Nyx", "in", raid="Barrow Deeps")
    finally:
        ms.use(None)
    assert sent == [{"member": "Nyx", "status": "in", "character": None, "spec": "Holy", "raid": "Barrow Deeps"}]
    r = web_bot.c.post(f"/api/run/{lead.key}/set", json=sent[0], headers=H())
    assert r.status_code == 200 and "Nyx plays Holy for Barrow Deeps" in r.json()["message"], r.text
    assert rs.events[fev.key].switched["Nyx"].spec == "Holy" and rs.events[lead.key].signups[str(mems[0].discord_id)].spec == "Shadow"
    r = web_bot.c.post(f"/api/run/{lead.key}/set", json={"member": "Nyx", "status": "in", "spec": "Holy"}, headers=H())  # no raid: the whole event
    assert r.status_code == 200, r.text
    assert not rs.events[fev.key].switched and rs.events[lead.key].signups[str(mems[0].discord_id)].spec == "Holy"


def test_plain_text_both_ways(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    lead, fev, ms = evening(reg, rs)
    bot = NightBot(reg, rs)
    one = ConfigOp(op="run_answer", target=lead.key, member="Nyx", value="join", field="Holy", text="Barrow Deeps")
    assert configops.is_warn(reg, one), "a role switch is always reviewed"
    assert "Nyx plays Holy for Barrow Deeps alone" in configops.describe(reg, one)
    run(configops.apply_async(reg, one, "Jon", True, bot=bot, by_id=1))
    assert rs.events[fev.key].switched["Nyx"].spec == "Holy" and rs.events[lead.key].signups[str(ms[0].discord_id)].spec == "Shadow"
    whole = ConfigOp(op="run_answer", target=lead.key, member="Nyx", value="join", field="Holy")
    assert configops.is_warn(reg, whole) and "alone" not in configops.describe(reg, whole)
    run(configops.apply_async(reg, whole, "Jon", True, bot=bot, by_id=1))
    assert not rs.events[fev.key].switched and rs.events[lead.key].signups[str(ms[0].discord_id)].spec == "Holy"
    with pytest.raises(RegistryError, match="isn't one of this event's raids"):
        run(configops.apply_async(reg, ConfigOp(op="run_answer", target=lead.key, member="Nyx", value="join", field="Holy", text="Onyxia's Lair"),
                                  "Jon", True, bot=bot, by_id=1))


def test_discord_raid_set_asks_for_which_raid(reg, rs, monkeypatch):
    """/raid set → Play another spec → Plays which spec? → For which raid? (a select: the whole event or each raid by
    name, nothing typed) → the raid's own box → set_raid_switch; the whole event stays today's switch."""
    import asyncio

    import test_flows_raid as tf

    from oibot_gm import discord_registry
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    ch = tf.FakeChannel()
    orig = tf.wizard_harness.FakeInteraction.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.channel = ch
    monkeypatch.setattr(tf.wizard_harness.FakeInteraction, "__init__", init)

    class Bot(tf.FakeBot):
        async def set_raid_switch(self, reg, rs, part, m, character, spec, by, by_id=None, via="discord", status=None):
            self.calls.append(("set_raid_switch", part.key, m.display_name, character, spec, by, status))
            return f"✅ {m.display_name} plays {spec}"

    lead = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", lineup=EVENING)
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, lead, nyx, None, "in", source="test")
    fev = rc.later_parts(rs, lead)[0]
    bot = Bot(rs)
    h = tf.Harness(reg, rs, client=bot)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=tf._user(nyx))
        await h.pick("Pick the event", lead.key)
        assert "Play another spec" in h.buttons()
        await h.press("Play another spec")
        assert h.selects() == ["Plays which spec?"]
        await h.pick("Plays which spec?", "Holy")
        assert h.selects() == [rc.SCOPE_LABEL]
        await h.pick(rc.SCOPE_LABEL, fev.key)
        assert "Nyx plays Holy for Barrow Deeps and stays Shadow for Hyjal Summit." in h.text
        assert "They get a notice, and nobody gets a DM." in h.text and not bot.calls
        await h.press("Switch")
    asyncio.run(go())
    assert bot.calls == [("set_raid_switch", fev.key, "Nyx", "Nyx", "Holy", "Tester", "in")]

    h2 = tf.Harness(reg, rs, client=bot)

    async def whole():
        await FLOWS["raid_set"](h2.command(), reg, member=tf._user(nyx))
        await h2.pick("Pick the event", lead.key)
        await h2.press("Play another spec")
        await h2.pick("Plays which spec?", "Holy")
        await h2.pick(rc.SCOPE_LABEL, "event")
        await h2.press("Record answer")
    asyncio.run(whole())
    assert bot.calls[-1] == ("set_answer", lead.key, "Nyx", "in", "Nyx", "Tester", "Holy")


def test_discord_raid_set_on_a_single_raid_event_has_no_raid_select(reg, rs, monkeypatch):
    import asyncio

    import test_flows_raid as tf

    from oibot_gm import discord_registry
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t")
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, ev, nyx, None, "in", source="test")
    h = tf.Harness(reg, rs, client=tf.FakeBot(rs))

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=tf._user(nyx))
        await h.press("Play another spec")
        await h.pick("Plays which spec?", "Holy")
        assert h.selects() == [] and "Record answer" in h.buttons()
    asyncio.run(go())


def test_a_joined_members_spec_switch_is_marked_for_menus_without_a_spec_field(reg, rs):
    lead, fev, ms = evening(reg, rs)
    join = next(a for a in rc.member_actions(reg, lead, ms[0]) if a["key"] == "join")
    assert join["label"] == "Play another spec or character" and join["characters"] == ["Nyx", "Nyxpal"] and join["respec"] is True


# ---------------------------------------------------------------- fix round (review of 2026-11-27)

def _flow_bot(monkeypatch):
    import test_flows_raid as tf

    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)

    class Bot(tf.FakeBot):
        async def set_raid_switch(self, reg, rs, part, m, character, spec, by, by_id=None, via="discord", status=None):
            self.calls.append(("set_raid_switch", part.key, m.display_name, character, spec, by, status))
            return f"✅ {m.display_name} plays {spec}"
    return tf, Bot


def test_discord_the_whole_event_with_their_own_spec_replaces_a_raids_switch(reg, rs, monkeypatch):
    """Fix 1: Nyx answered Shadow and is Holy for Barrow Deeps; /raid set → Shadow → The whole event sends the spec, so
    the switch for Barrow Deeps goes, and the box says so."""
    import asyncio

    from oibot_gm import inbox
    from oibot_gm.wizard import FLOWS

    tf, _Bot = _flow_bot(monkeypatch)
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    holy_for_bd(reg, rs, fev, nyx)
    bot = NightBot(reg, rs)

    class Bot(tf.FakeBot):
        async def set_answer(self, reg, rs, ev, m, status, character, by, by_id=None, spec=None, via=None):
            self.calls.append(("set_answer", spec))
            return await inbox.do_member_action(bot, reg, rs, ev, m, "join", character=character, spec=spec, by=by, by_id=by_id)
    fb = Bot(rs)
    h = tf.Harness(reg, rs, client=fb)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=tf._user(nyx))
        await h.pick("Pick the event", lead.key)
        await h.press("Play another spec or character")
        await h.pick("Pick the character", "Nyx")
        await h.pick("Plays which spec?", "Shadow")
        assert h.selects() == [rc.SCOPE_LABEL]
        await h.pick(rc.SCOPE_LABEL, "event")
        assert "This replaces their switch for Barrow Deeps." in h.text
        await h.press("Record answer")
    asyncio.run(go())
    assert fb.calls[-1] == ("set_answer", "Shadow")
    assert not rs.events[fev.key].switched and rs.events[lead.key].signups[str(nyx.discord_id)].spec == "Shadow"


def test_a_bench_member_joins_and_is_switched_for_one_raid_on_every_surface(reg, rs, web_bot):
    """Fix 2: the status is one per event, the spec per raid — a Bench answer + Join + one raid records both."""
    from test_raid_night import H

    from oibot_gm import configops
    from oibot_gm import consequences as cq
    from oibot_gm.configops import ConfigOp

    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    rc.set_signup(reg, rs, rs.events[lead.key], nyx, None, "sub", source="test")
    b = cq.raid_switch_box(reg, rs, rs.events[fev.key], nyx, "Nyx", "Holy", status="in")
    assert b["sentences"] == ["Nyx joins as Shadow and plays Holy for Barrow Deeps.", "They get a notice, and nobody gets a DM.",
                              "You can change both until the event starts."]
    assert b["title"] == "Answer for Nyx and switch them to Holy for Barrow Deeps?"
    # the site / MCP route
    r = web_bot.c.post(f"/api/run/{lead.key}/set", json={"member": "Nyx", "status": "in", "spec": "Holy", "raid": "Barrow Deeps"}, headers=H())
    assert r.status_code == 200 and r.json()["message"] == "Nyx joins as Nyx and plays Holy for Barrow Deeps", r.text
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    sg = lead.signups[str(nyx.discord_id)]
    assert (sg.status, sg.spec) == ("in", "Shadow") and fev.switched["Nyx"].spec == "Holy"
    # plain text: Bench again, then "join Nyx as Holy for Barrow Deeps"
    rc.set_raid_switch(reg, rs, fev, nyx, "Nyx", "Shadow", by="Jon")
    rc.set_signup(reg, rs, rs.events[lead.key], nyx, None, "sub", source="test")
    op = ConfigOp(op="run_answer", target=lead.key, member="Nyx", value="join", field="Holy", text="Barrow Deeps")
    assert "Nyx → Join for the whole event, and plays Holy for Barrow Deeps alone" in configops.describe(reg, op)
    run(configops.apply_async(reg, op, "Jon", True, bot=web_bot.bot, by_id=1))
    assert rs.events[lead.key].signups[str(nyx.discord_id)].status == "in" and rs.events[fev.key].switched["Nyx"].spec == "Holy"


def test_no_answer_yet_joins_and_is_switched_and_a_pure_switch_changes_no_answer(reg, rs):
    lead, fev, ms = evening(reg, rs)
    tavi = add(reg, 40, "Tavi", "Priest", "Shadow", "Holy")
    bot = NightBot(reg, rs)
    with pytest.raises(RegistryError, match="hasn't joined"):
        run(bot.set_raid_switch(reg, rs, fev, tavi, "Tavi", "Holy", "Jon", by_id=1))  # a pure switch: nothing changes
    assert str(tavi.discord_id) not in rs.events[lead.key].signups
    line = run(bot.set_raid_switch(reg, rs, fev, tavi, "Tavi", "Holy", "Jon", by_id=1, status="in"))
    assert line == "✅ Tavi joins as Tavi and plays Holy for Barrow Deeps"
    assert rs.events[lead.key].signups[str(tavi.discord_id)].status == "in" and rs.events[rs.events[fev.key].key].switched["Tavi"].spec == "Holy"
    from oibot_gm import inbox
    notes = [n["text"] for n in inbox.notices_of(reg, tavi.discord_id)]
    assert len(notes) == 1 and "set you to Join as Tavi and switched you to Holy" in notes[0]


def test_discord_join_from_bench_with_one_raid_says_both(reg, rs, monkeypatch):
    import asyncio

    from oibot_gm.wizard import FLOWS

    tf, Bot = _flow_bot(monkeypatch)
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    rc.set_signup(reg, rs, rs.events[lead.key], nyx, None, "sub", source="test")
    bot = Bot(rs)
    h = tf.Harness(reg, rs, client=bot)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=tf._user(nyx))
        await h.pick("Pick the event", lead.key)
        await h.press("Join")
        await h.pick("Pick the character", "Nyx")
        await h.pick("Plays which spec?", "Holy")
        await h.pick(rc.SCOPE_LABEL, fev.key)
        assert "Nyx joins as Shadow and plays Holy for Barrow Deeps." in h.text
        await h.press("Switch")
    asyncio.run(go())
    assert bot.calls[-1] == ("set_raid_switch", fev.key, "Nyx", "Nyx", "Holy", "Tester", "in")


def test_the_bots_context_reads_what_people_play_in_each_raid(reg, rs):
    """Fix 3: help.run_lines' joined-by-role and joined list agree with needs for the switched raid."""
    from oibot_gm import help as hp

    lead, fev, ms = evening(reg, rs)
    holy_for_bd(reg, rs, fev, ms[0])
    fev = rs.events[fev.key]
    lines = hp.run_lines(reg, rs, fev, is_officer=True)
    role = next(ln for ln in lines if ln.strip().startswith("joined by role"))
    assert f"healer {rc.gaps(reg, fev)['have']['healer']}" in role
    assert "Nyx (Nyx Holy Priest)" in next(ln for ln in lines if ln.strip().startswith("joined:"))
    assert "Nyx (Nyx Shadow Priest)" in next(ln for ln in hp.run_lines(reg, rs, rs.events[lead.key], is_officer=True) if ln.strip().startswith("joined:"))


def test_the_box_lists_each_raid_the_row_helps(reg, rs):
    """Fix 4: a row that helps two raids of three: each by name, then the whole event; any one can be picked."""
    lead = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", lineup=EVENING + [{"raid": "onyxias_lair", "rosters": 1}])
    nyx = add(reg, 0, "Nyx", "Priest", "Shadow", "Holy")
    rc.set_signup(reg, rs, lead, nyx, None, "in", source="test")
    hy, bd, ony = rc.event_parts(rs, rs.events[lead.key])
    b, _ = rc.switch_box_for(reg, rs, bd, nyx, "Nyx", "Holy", scope=bd.key, helps=[bd, ony])
    assert b["scope"]["options"] == [{"value": bd.key, "label": "Barrow Deeps"}, {"value": ony.key, "label": "Onyxia's Lair"},
                                     {"value": "event", "label": "The whole event"}]
    assert b["scope"]["value"] == bd.key and b["sentences"][0].startswith("Nyx plays Holy for Barrow Deeps")
    b2, _ = rc.switch_box_for(reg, rs, bd, nyx, "Nyx", "Holy", scope=ony.key, helps=[bd, ony])
    assert b2["scope"]["value"] == ony.key and b2["sentences"][0].startswith("Nyx plays Holy for Onyxia's Lair")


def test_a_switch_to_a_retired_or_respecced_character_is_dropped_with_a_log_line(reg, rs):
    """Fix 5."""
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    rc.set_raid_switch(reg, rs, fev, nyx, "Nyxpal", "Holy", by="Jon")
    pal = next(c for c in nyx.characters if c.label == "Nyxpal")
    pal.spec = "Retribution"  # its spec edited: it no longer plays Holy
    assert [e.key for e in rc.drop_stale_switches(reg, rs, nyx)] == [fev.key]
    fev = rs.events[fev.key]
    assert not fev.switched and "switch dropped: Nyx (Nyxpal no longer plays Holy)" in fev.log
    pal.spec = "Holy"
    rc.set_raid_switch(reg, rs, fev, nyx, "Nyxpal", "Holy", by="Jon")
    pal.status = "retired"
    res = rc.after_character_change(reg, rs, nyx, [])
    assert fev.key in [e.key for e in res["touched"]] and not rs.events[fev.key].switched
    assert "switch dropped: Nyx (Nyxpal is no longer theirs to play)" in rs.events[fev.key].log


def test_naming_an_ended_raid_and_a_size_phrase(reg, rs):
    """Fixes 6 and 7."""
    lead, fev, ms = evening(reg, rs)
    assert rc.event_raid(reg, rs, lead, "the 10-mans").key == fev.key
    assert rc.event_raid(reg, rs, lead, "20-man").key == lead.key
    with pytest.raises(RegistryError, match="isn't one of this event's raids, which are Hyjal Summit and Barrow Deeps."):
        rc.event_raid(reg, rs, lead, "the 25-man")
    fev.state = "done"
    assert rc.raid_switch_refusal(reg, rs, fev, ms[0], "Nyx", "Holy") == "Barrow Deeps has ended or was cancelled, so nobody can switch roles on it."


def test_a_raid_without_a_spec_is_refused_in_the_review(reg, rs):
    """Fix 8."""
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    lead, fev, ms = evening(reg, rs)
    with pytest.raises(RegistryError, match=configops.RAID_SWITCH_NEEDS_SPEC):
        configops.describe(reg, ConfigOp(op="run_answer", target=lead.key, member="Nyx", value="join", text="Barrow Deeps"), strict=True)


def test_the_way_back_is_audited_as_back_to_their_answer(reg, rs):
    """Fix 9 (the audit line)."""
    lead, fev, ms = evening(reg, rs)
    bot = NightBot(reg, rs)
    seen = []

    async def audited(ops, reg_, by, line, **kw):
        seen.append(line)
    import oibot_gm.discord_raid as dr
    orig = dr.ops_mod.audited
    dr.ops_mod.audited = audited
    try:
        run(bot.set_raid_switch(reg, rs, fev, ms[0], "Nyx", "Holy", "Jon", by_id=1))
        run(bot.set_raid_switch(reg, rs, rs.events[fev.key], ms[0], "Nyx", "Shadow", "Jon", by_id=1))
    finally:
        dr.ops_mod.audited = orig
    assert seen[0].startswith("switched Nyx to Holy for Barrow Deeps alone on ")
    assert seen[1].startswith("switched Nyx back to their answer, Shadow, for Barrow Deeps on ")


def test_the_confirm_card_says_an_officer_switched_them(reg, rs):
    """Fix 10."""
    from test_analytics_cards import text_of

    from oibot_gm import raid_views as views

    ico = lambda kind, key, *a, **k: f"<{kind}:{key}>"  # noqa: E731
    lead, fev, ms = evening(reg, rs)
    holy_for_bd(reg, rs, fev, ms[0])
    names = [m.display_name for m in ms]
    place_all(rs, rs.events[lead.key], names)
    place_all(rs, rs.events[fev.key], names)
    run(NightBot(reg, rs).lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    fev = rs.events[fev.key]
    i = next(k for k, r in enumerate(fev.rosters) if any(p.signup_name == "Nyx" for p in r.selected))
    gi = next(k for k, g in enumerate(fev.rosters[i].groups) if "Nyx" in g)
    card = text_of(views.confirm_layout(reg, fev, rc.run_team(reg, fev), ico, fev.signups[str(ms[0].discord_id)], i, gi, ms[0].discord_id))
    assert "An officer switched you to Holy for Barrow Deeps." in card and "you signed up as" not in card


def test_a_nick_change_keeps_the_switch_on_the_person(reg, rs):
    """Fix 12: the switch carries the member's id, so a re-answer under a new display name keeps it."""
    lead, fev, ms = evening(reg, rs)
    nyx = ms[0]
    holy_for_bd(reg, rs, fev, nyx)
    reg.rename(nyx.discord_id, "Nyxie", "t")
    rc.set_signup(reg, rs, rs.events[lead.key], reg.members[nyx.discord_id], None, "in", source="member")
    fev = rs.events[fev.key]
    assert fev.signups[str(nyx.discord_id)].display_name == "Nyxie"
    assert fev.switched["Nyxie"].spec == "Holy" and rc.plays_in(reg, fev, fev.signups[str(nyx.discord_id)]).spec == "Holy"
    assert not any("switch dropped" in ln for ln in fev.log)
