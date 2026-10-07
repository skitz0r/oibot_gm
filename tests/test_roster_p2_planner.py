"""Roster redesign Phase 2 — the ONE planner (rc.role_plan, the fill engine extended to run before lock), as amended by
the owner (out/roster_decisions.md). The design's §4 worked example is the fixture: Barrow Deeps, Tuesdays, 10 seats,
minimum 2 tanks and 2 healers, 12 regulars; Thorn and Ona record absences.

D1 (nothing is pre-filled) changes one thing about the example: before lock the planner counts the regulars who haven't
answered (and aren't away) as coming, so the suggestions go beyond them — the hole shows at sheet open, before anyone
answered. The data model stores a declared flex role only when the offspec doesn't already give it, so Ana is
Retribution with a declared flex healer (→ Holy, the Paladin's only healer spec)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import first_slot, join

from oibot_gm import raidcycle as rc
from oibot_gm.models import Player, RosterResult
from oibot_gm.registry import Absence

RID = "barrow_deeps"
KEY = f"{RID}:default"
BASE = 600_000_000_000_000_000

# name, class, spec, offspec, declared flex, alt (name, class, spec)
REGULARS = [
    ("Brannoc", "Warrior", "Protection", None, [], None),
    ("Thorn", "Druid", "Guardian", None, [], None),
    ("Mira", "Priest", "Holy", None, [], None),
    ("Ona", "Shaman", "Restoration", None, [], None),
    ("Kael", "Warrior", "Fury", "Protection", [], None),
    ("Ana", "Paladin", "Retribution", None, ["healer"], None),
    ("Jex", "Rogue", "Combat", None, [], None),
    ("Bo", "Mage", "Fire", None, [], None),
    ("Vel", "Hunter", "Marksmanship", None, [], None),
    ("Rook", "Rogue", "Assassination", None, [], None),
    ("Sable", "Mage", "Frost", None, [], None),
    ("Wren", "Warlock", "Affliction", None, [], ("Fern", "Druid", "Restoration")),
]
OUTSIDERS = [("Gorm", "Paladin", "Protection"), ("Pip", "Priest", "Discipline")]


def add(reg, i: int, name: str, cls: str, spec: str, offspec=None, flex=(), alt=None):
    uid = BASE + i
    reg.add_character(uid, name, name, cls, spec, offspec, True)
    if flex:
        reg.set_flex(uid, name, list(flex))
    if alt:
        reg.add_character(uid, name, alt[0], alt[1], alt[2], None, False)
    return reg.members[uid]


def worked_example(reg, rs, absent=("Thorn", "Ona")):
    """The §4 fixture: returns (run, {name: member}). The run is the next Tuesday; `absent` recorded away that day."""
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    people = {}
    for i, (name, cls, spec, off, flex, alt) in enumerate(REGULARS):
        people[name] = add(reg, i, name, cls, spec, off, flex, alt)
        reg.regular_add(people[name].discord_id, KEY, "officer")
    for j, (name, cls, spec) in enumerate(OUTSIDERS):
        people[name] = add(reg, 50 + j, name, cls, spec)
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    day = t.astimezone(reg.tz).date().isoformat()
    for name in absent:
        m = people[name]
        m.absences = [Absence(start=day, end=day, reason="away", by="t")]
        reg.save(m, "away")
    ev = rc.open_run(reg, rs, RID, t, by="t")
    return ev, people


def seat(plan, role):
    return next(st for st in plan.seats if st.role == role)


def test_worked_example_at_sheet_open(reg, rs):
    ev, p = worked_example(reg, rs)
    assert ev.signups[str(p["Thorn"].discord_id)].source == "absence" and ev.signups[str(p["Ona"].discord_id)].source == "absence"
    plan = rc.role_plan(reg, rs, ev)
    assert plan.phase == "prelock" and plan.expected == 10 and plan.full, "10 regulars expected for 10 seats: every seat taken"
    assert plan.short["roles"] == {"tank": 1, "healer": 1}
    tank, healer = seat(plan, "tank"), seat(plan, "healer")
    k = tank.options[0]
    assert [(a.display_name, a.kind, a.spec, a.role) for a in k] == [("Kael", "offspec", "Protection", "tank")]
    assert k[0].why == "short 1 tank · Kael's offspec Protection · a regular, hasn't answered yet · DPS stays 7 of 5 minimum"
    assert [opt[0].display_name for opt in tank.options[1:]] == ["Gorm"], "the alternate: a non-regular main of the role"
    a = healer.options[0]
    assert [(x.display_name, x.kind, x.spec) for x in a] == [("Ana", "flex", "Holy")], "a declared flex role before an alt or a new body"
    assert "Ana's declared flex Holy" in a[0].why and "DPS stays 6 of 5 minimum" in a[0].why
    alts = [(opt[0].display_name, opt[0].kind, opt[0].character) for opt in healer.options[1:]]
    assert alts == [("Wren", "alt", "Fern"), ("Pip", "other", "Pip")], "two alternates: Wren brings Fern, then Pip"
    assert "not saved this lockout" in healer.options[1][0].why
    assert plan.short_after == {"headcount": 0, "roles": {}}
    assert rc.still_short_words(plan) == "nothing"
    assert [a.display_name for a in plan.asks] == ["Kael", "Ana"]
    assert all(not m.test for m in (p[a.display_name] for a in plan.asks)), "never a test puppet on a real run"


def test_ana_says_no_then_wren_brings_fern_and_the_signups_change_without_seating(reg, rs):
    ev, p = worked_example(reg, rs)
    plan = rc.role_plan(reg, rs, ev)
    for a in plan.asks:  # what an officer's Ask records (send_plan); here without DMs
        a.phase, a.approved_by = "prelock", "Officer"
        ev.fill_asks.append(a)
    kael, ana = (next(a for a in ev.fill_asks if a.display_name == n) for n in ("Kael", "Ana"))
    line = rc.apply_fill_answer(reg, rs, ev, kael, True)
    sg = ev.signups[str(p["Kael"].discord_id)]
    assert (sg.status, sg.spec, sg.offspec, sg.role, sg.source) == ("in", "Protection", "Fury", "tank", "fill")
    assert ev.state == "open" and not ev.rosters and "the lock builds the roster" in line, "a yes before lock seats nobody"
    rc.apply_fill_answer(reg, rs, ev, ana, False)
    assert str(p["Ana"].discord_id) not in ev.signups, "a no records nothing on the sheet"
    again = rc.role_plan(reg, rs, ev)
    assert list(again.short["roles"]) == ["healer"]
    nxt = again.seats[0].options[0][0]
    assert (nxt.display_name, nxt.kind, nxt.character, nxt.spec) == ("Wren", "alt", "Fern", "Restoration"), "Ana said no → Fern is suggested"
    assert "Ana" not in {a.display_name for st in again.seats for opt in st.options for a in opt}, "one ask per member per run"
    nxt.phase = "prelock"
    ev.fill_asks.append(nxt)
    rc.apply_fill_answer(reg, rs, ev, nxt, True)
    w = ev.signups[str(p["Wren"].discord_id)]
    assert (w.character, w.spec, w.role, w.status) == ("Fern", "Restoration", "healer", "in")
    final = rc.role_plan(reg, rs, ev)
    assert final.short["roles"] == {} and not final.seats, "tanks 2/2, healers 2/2"


@pytest.mark.slow
def test_lock_seats_two_tanks_and_two_healers(reg, rs):
    """The exit test's last step: after Kael (Protection) and Wren (Fern) said yes and the rest joined, the lock seats
    2 tanks + 2 healers."""
    ev, p = worked_example(reg, rs)
    for name, char, spec in (("Kael", "Kael", "Protection"), ("Wren", "Fern", "Restoration")):
        a = rc.FillAsk(discord_id=p[name].discord_id, display_name=name, kind="offspec" if name == "Kael" else "alt", role="tank" if name == "Kael" else "healer",
                       character=char, spec=spec, reason="t", phase="prelock")
        ev.fill_asks.append(a)
        rc.apply_fill_answer(reg, rs, ev, a, True)
    join(reg, rs, ev, [p[n] for n in ("Brannoc", "Mira", "Ana", "Jex", "Bo", "Vel", "Rook", "Sable")])
    rc.propose(reg, rs, ev, save=False)
    roles = [x.role for x in ev.roster.selected]
    assert len(roles) == 10 and roles.count("tank") == 2 and roles.count("healer") == 2


# ---------------------------------------------------------------- the planner's properties

def _joined_series(reg, rs, specs):
    """A recurring run whose regulars are exactly `specs` and have all joined (nobody projected)."""
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    ms = []
    for i, row in enumerate(specs):
        name, cls, spec = row[:3]
        off, flex, alt = (row[3] if len(row) > 3 else None), (row[4] if len(row) > 4 else ()), (row[5] if len(row) > 5 else None)
        m = add(reg, 100 + i, name, cls, spec, off, flex, alt)
        reg.regular_add(m.discord_id, KEY, "officer")
        ms.append(m)
    t = first_slot(reg, RID)
    ev = rc.open_run(reg, rs, RID, t, by="t")
    join(reg, rs, ev, ms)
    return ev, {m.display_name: m for m in ms}


DPS8 = [("Dax", "Rogue", "Combat"), ("Eko", "Mage", "Fire"), ("Fyn", "Hunter", "Survival"), ("Gil", "Rogue", "Subtlety"), ("Hal", "Mage", "Arcane"), ("Ivo", "Warlock", "Destruction")]


def test_cost_order_flex_then_offspec_then_alt_with_two_alternates(reg, rs):
    ev, _ = _joined_series(reg, rs, [("Tank", "Warrior", "Protection"), ("Tanky", "Paladin", "Protection"), ("Heal", "Priest", "Holy"),
                                     ("Alty", "Warlock", "Demonology", None, (), ("Alta", "Druid", "Restoration")),
                                     ("Offy", "Shaman", "Enhancement", "Restoration"),
                                     ("Flexy", "Paladin", "Retribution", None, ["healer"])] + DPS8[:4])
    plan = rc.role_plan(reg, rs, ev)
    st = seat(plan, "healer")
    assert [(o[0].display_name, o[0].kind) for o in st.options] == [("Flexy", "flex"), ("Offy", "offspec"), ("Alty", "alt")]


def test_swaps_first_only_when_every_seat_is_taken(reg, rs):
    nine = [("Tank", "Warrior", "Protection"), ("Tanky", "Paladin", "Protection"), ("Heal", "Priest", "Holy"), ("Offy", "Shaman", "Enhancement", "Restoration")] + DPS8[:6]
    ev, ms = _joined_series(reg, rs, nine)
    bench = add(reg, 199, "Benchy", "Druid", "Restoration")
    rc.set_signup(reg, rs, ev, bench, None, "sub")
    full = rc.role_plan(reg, rs, ev)
    assert full.full and seat(full, "healer").options[0][0].display_name == "Offy", "every seat taken: a switch before a new healer"
    assert [o[0].display_name for o in seat(full, "healer").options] == ["Offy", "Benchy"]
    for name in ("Dax", "Eko"):  # two joiners say No thanks: seats are open now
        rc.set_signup(reg, rs, ev, ms[name], None, "out")
    open_ = rc.role_plan(reg, rs, ev)
    assert not open_.full and seat(open_, "healer").options[0][0].display_name == "Benchy", "seats open: someone on Bench first (cheapest)"


def test_matching_check_never_takes_someone_a_later_seat_needs(reg, rs):
    """Short a tank and a healer. Feral can tank (declared flex, cheapest) or heal (offspec); only an alt warrior can
    otherwise tank, and nobody else heals. Greedy would give Feral the tank seat and leave the healer seat empty."""
    ev, _ = _joined_series(reg, rs, [("Tank", "Warrior", "Protection"), ("Heal", "Priest", "Holy"),
                                     ("Feral", "Druid", "Feral", "Restoration", ["tank"]),
                                     ("Locky", "Warlock", "Affliction", None, (), ("Shieldy", "Warrior", "Protection"))] + DPS8)
    plan = rc.role_plan(reg, rs, ev)
    assert (seat(plan, "tank").options[0][0].display_name, seat(plan, "tank").options[0][0].kind) == ("Locky", "alt")
    assert (seat(plan, "healer").options[0][0].display_name, seat(plan, "healer").options[0][0].kind) == ("Feral", "offspec")
    people = [a.discord_id for a in plan.asks]
    assert len(people) == len(set(people)), "nobody counted twice"
    assert plan.short_after["roles"] == {}


def test_alt_saved_this_lockout_is_not_suggested(reg, rs):
    ev, p = worked_example(reg, rs)
    win = reg.lockout_window(RID, ev.start)
    other_start = win[0] if win[0] != ev.start else win[0] + timedelta(hours=1)
    fern = Player(signup_name="Wren", discord_id=p["Wren"].discord_id, pos=1, status="signed", cls="Druid", spec="Restoration", role="healer", character="Fern")
    done = rc.RaidEvent(key="bd-saved-x", team="bd-saved", instance=RID, starts_at=other_start.isoformat(), state="done",
                        rosters=[RosterResult(selected=[fern], benched=[], groups=[["Wren"]], group_reports=[], objective=0, synergy_value=0, role_counts={}, advisories=[])])
    rs.save(done, "a run earlier this lockout")
    assert rc.saved_this_lockout(reg, rs, ev) == {"Fern"}
    healer = seat(rc.role_plan(reg, rs, ev), "healer")
    assert "Fern" not in {a.character for opt in healer.options for a in opt}, "Fern is saved to Barrow Deeps this lockout"


def test_paired_backfill_when_the_vacated_role_would_go_short(reg, rs):
    """The only possible healer is a joiner whose leaving would drop the tanks under 2: the switch goes out tied to a
    backfill tank from the Bench, both halves in one option."""
    ev, ms = _joined_series(reg, rs, [("Tank", "Warrior", "Protection"), ("Pally", "Paladin", "Protection", "Holy"), ("Heal", "Priest", "Holy")] + DPS8[:5])
    bench = add(reg, 198, "Spare", "Druid", "Guardian")
    rc.set_signup(reg, rs, ev, bench, None, "sub")
    st = seat(rc.role_plan(reg, rs, ev), "healer")
    first = st.options[0]  # the Bench tank is not a healer, so Pally's switch is the only healer move — tied to Spare
    assert [a.display_name for a in first] == ["Pally", "Spare"] and first[0].pair == bench.discord_id and first[1].pair == ms["Pally"].discord_id
    assert first[1].reason == "covering Pally's tank seat" and "tank would drop under its minimum, so Spare covers it" in first[0].why
    assert len(st.options) == 1, "no other healer move exists"


def test_plan_rev_tracks_what_would_be_sent(reg, rs):
    ev, p = worked_example(reg, rs)
    rev = rc.role_plan(reg, rs, ev).rev
    rc.set_signup(reg, rs, ev, p["Jex"], None, "in")  # a regular answers as expected: the suggestions stay the same
    assert rc.role_plan(reg, rs, ev).rev == rev
    rc.set_signup(reg, rs, ev, p["Gorm"], None, "in")  # the alternate joins: the tank seat's options change
    assert rc.role_plan(reg, rs, ev).rev != rev


def test_pick_asks_refuses_one_person_for_two_seats():
    """Alternates are distinct per role, so one druid can be an alternate for the tank seat AND the healer seat: picking
    them for both is refused; the default picks (each seat's suggestion) never collide."""
    def ask(uid, name, role):
        return rc.FillAsk(discord_id=uid, display_name=name, kind="alt", role=role, character=name, spec="x", reason="t")
    plan = rc.Plan(phase="prelock", short={"headcount": 0, "roles": {"tank": 1, "healer": 1}}, have={}, need={}, full=False,
                   short_after={"headcount": 0, "roles": {}},
                   seats=[rc.PlanSeat(role="tank", options=[[ask(1, "Tia", "tank")], [ask(9, "Druid", "tank")]]),
                          rc.PlanSeat(role="healer", options=[[ask(2, "Hux", "healer")], [ask(9, "Druid", "healer")]])])
    assert [a.display_name for a in rc.pick_asks(plan)] == ["Tia", "Hux"]
    assert [a.display_name for a in rc.pick_asks(plan, {"1": 1})] == ["Tia", "Druid"]
    with pytest.raises(rc.RegistryError, match="two seats"):
        rc.pick_asks(plan, {0: 1, 1: 1})
    with pytest.raises(rc.RegistryError, match="no option 3"):
        rc.pick_asks(plan, {0: 2})


def test_no_projection_without_regulars_and_no_suggestions_when_covered(reg, rs):
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    t = rc.upcoming(reg, RID, reg.now_local(), 24 * 21)[0][1]
    ev = rc.open_run(reg, rs, RID, t, by="t")
    plan = rc.role_plan(reg, rs, ev)
    assert plan.expected == 0, "a series nobody curated: the planner counts only who joined"
    assert plan.short["roles"] == {"tank": 2, "healer": 2}
    covered, _ = _joined_series(reg, rs, [("Tia", "Warrior", "Protection"), ("Tob", "Paladin", "Protection"), ("Hux", "Priest", "Holy"), ("Hob", "Shaman", "Restoration")] + DPS8)
    assert not rc.role_plan(reg, rs, covered).seats
