"""Phase 5, Task 4: /raid lineup (flows_event.EditLineupWizard on LineupBlock) driven end to end. A hub of steps, one
screen per step (rosters, start in 15-minute steps, move, remove), saved through the server's box 4
(consequences.lineup_box). Runs on test_lineups' widened TEST profile (five raids), so a four-raid lineup fits."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
from conftest import join, lock_with_board
from test_flows_raid import officer  # noqa: F401 — the fixture
from test_lineups import reg  # noqa: F401 — the widened profile
from test_raid_night import NightBot
from test_reschedule import OFFICER_ID, open_one_off, real_people

from oibot_gm import consequences as cq
from oibot_gm import flows_event
from oibot_gm import raidcycle as rc
from oibot_gm.registry import LINEUP_MAX_WORDS, MAX_SCHEDULE_ROSTERS
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

RAID_B, RAID_C, RAID_D = "extra_a", "extra_b", "onyxias_lair"


def run(c):
    return asyncio.run(c)


def harness(reg, rs):
    user = SimpleNamespace(id=OFFICER_ID, display_name="Officer", mention="@Officer")
    return Harness(reg, rs, user=user, client=NightBot(reg, rs))


def child(h, placeholder):
    return next(s for s in h.screen["view"].children if getattr(s, "placeholder", "") == placeholder)


def test_add_a_raid_and_save_through_the_lineup_box(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    assert h.selects() == ["Pick a step", "Add a raid"] and h.buttons() == ["Review changes", "Back"]
    assert first.key not in h.text, "no run key shown"
    run(h.pick("Add a raid", RAID_B))
    assert "2. Extra A · 1 roster · " in h.text
    run(h.press("Review changes"))
    box = cq.lineup_box(reg, rs, first, [{"raid": first.instance, "rosters": 1, "starts": 0}, {"raid": RAID_B, "rosters": 1}])
    assert box["verb"] and box["diff"] and all(d in h.text for d in box["diff"])
    assert all(s in h.text for s in box["sentences"]) and h.buttons() == [box["verb"], "Back"]
    run(h.press(box["verb"]))
    assert "✅" in h.text
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [first.instance, RAID_B]


def test_a_four_raid_lineup_fits_one_screen(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h = harness(reg, rs)  # the harness lints every screen: ≤ 5 rows, ≤ 25 options, ≤ 2000 characters
    run(FLOWS["raid_lineup"](h.command(), reg))
    for rid in (RAID_B, RAID_C, RAID_D):
        run(h.pick("Add a raid", rid))
    for n in ("1", "2", "3", "4"):
        run(h.pick("Pick a step", n))
        run(h.pick("Rosters", str(MAX_SCHEDULE_ROSTERS)))
        run(h.press("Back"))
    assert h.text.count(f"{MAX_SCHEDULE_ROSTERS} rosters") == 4
    add = child(h, "Add a raid")
    assert add.disabled and LINEUP_MAX_WORDS in h.text
    run(h.pick("Pick a step", "3"))
    starts = child(h, "Starts")
    assert len(starts.options) <= 25 and all(o.label[-2:] in ("AM", "PM") for o in starts.options[1:])
    assert starts.options[0].label.startswith("When Extra A ends · ")
    assert h.buttons() == ["Move earlier", "Move later", "Remove", "Back"]
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Save lineup"))
    assert [(e["raid"], e["rosters"]) for e in rc.run_lineup(reg, rs, first)] == [
        (first.instance, 4), (RAID_B, 4), (RAID_C, 4), (RAID_D, 4)]


def test_a_later_start_is_picked_in_15_minute_steps(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Add a raid", RAID_B))
    run(h.pick("Pick a step", "2"))
    starts = child(h, "Starts")
    vals = [int(o.value) for o in starts.options[1:]]
    assert vals == [15 * k for k in range(1, 25)], "from the first raid's start plus 15 minutes"
    run(h.pick("Starts", "45"))
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Save lineup"))
    later = rc.event_parts(rs, first)[1]
    assert later.start == rc.after_minutes(first.start, 45)


def test_removing_a_middle_raid_says_the_next_moves_earlier(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3, lineup=[{"raid": RAID_B, "rosters": 1}, {"raid": RAID_C, "rosters": 1}])
    join(reg, rs, first, real_people(reg, 2))
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Pick a step", "2"))
    run(h.press("Remove"))
    assert "Extra A" not in h.text and "2. Extra B" in h.text
    run(h.press("Review changes"))
    new = [{"raid": first.instance, "rosters": 1, "starts": 0}, {"raid": RAID_C, "rosters": 1}]
    box = cq.lineup_box(reg, rs, first, new)
    assert all(s in h.text for s in box["sentences"])
    at = reg.local12(rc.after_minutes(first.start, reg.lineup_offsets(new)[1]), "%I:%M %p")
    assert at in h.text, "Extra B's new start is said"
    run(h.press("Save lineup"))
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [first.instance, RAID_C]


def test_move_earlier_swaps_and_resets_the_later_starts(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3, lineup=[{"raid": RAID_B, "rosters": 1}, {"raid": RAID_C, "rosters": 1}])
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Pick a step", "3"))
    run(h.press("Move earlier"))
    assert h.text.index("2. Extra B") < h.text.index("3. Extra A")
    run(h.press("Review changes"))
    run(h.press("Save lineup"))
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [first.instance, RAID_C, RAID_B]


def test_a_lineup_edit_after_the_lock_is_refused_at_the_press(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    people = real_people(reg, 3)
    join(reg, rs, first, people)
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Add a raid", RAID_B))
    run(h.press("Review changes"))
    assert h.buttons() == ["Save lineup", "Back"]
    lock_with_board(reg, rs, first, [p.main.label for p in people])  # the scheduler locks meanwhile
    run(h.press("Save lineup"))
    assert h.buttons() == ["Back"] and rc.LINEUP_LOCKED in h.text
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [first.instance], "nothing written"


def test_a_locked_event_goes_straight_to_its_refusal(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    people = real_people(reg, 3)
    join(reg, rs, first, people)
    lock_with_board(reg, rs, first, [p.main.label for p in people])
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    assert rc.LINEUP_LOCKED in h.text and h.buttons() == ["Back"]


def test_the_first_raid_never_changes(reg, rs, officer):  # noqa: F811
    open_one_off(reg, rs, days=3, lineup=[{"raid": RAID_B, "rosters": 1}])
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Pick a step", "1"))
    assert h.selects() == ["Rosters"] and h.buttons() == ["Back"]


def test_the_command_takes_no_typed_value(reg):  # noqa: F811
    from discord import app_commands

    from oibot_gm.discord_raid import register_raid_commands
    from oibot_gm.discord_registry import Guilds
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    class Bot:
        raids = type("R", (), {"store": lambda self, r: None})()

    register_raid_commands(tree, Guilds.of(reg.store, reg), Ops(client), Bot())
    raid = next(c for c in tree.get_commands() if c.name == "raid")
    cmd = next(c for c in raid.commands if c.name == "lineup")
    assert cmd.parameters == [] and cmd.description == "Officer: change the raids of an open event before it locks"
    assert flows_event.EditLineupWizard.__mro__[1] is flows_event.LineupBlock


# ---------------------------------------------------------------- fix round 1

def test_a_new_start_shifts_the_later_raids_and_keeps_their_gaps(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3, lineup=[{"raid": RAID_B, "rosters": 1, "starts": 120},
                                                  {"raid": RAID_C, "rosters": 1, "starts": 300}])
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Pick a step", "2"))
    run(h.pick("Starts", "150"))
    run(h.press("Back"))
    run(h.press("Review changes"))
    run(h.press("Save lineup"))
    parts = rc.event_parts(rs, first)
    assert parts[1].start == rc.after_minutes(first.start, 150)
    assert parts[2].start == rc.after_minutes(first.start, 330), "raid 3 keeps its gap of 3 hours"


def test_tidy_drops_a_later_start_no_longer_after_the_one_before(reg, rs):  # noqa: F811
    class Block(flows_event.LineupBlock):
        pass

    b = Block()
    b.reg = reg
    b.lineup = [{"raid": "x", "rosters": 1, "starts": 30}, {"raid": RAID_B, "rosters": 1, "starts": 120},
                {"raid": RAID_C, "rosters": 1, "starts": 120}, {"raid": RAID_D, "rosters": 1, "starts": 400}]
    b.lineup[0]["raid"] = next(iter(reg.profile.raids))
    b._tidy()
    assert "starts" not in b.lineup[0], "the first raid starts the event"
    assert b.lineup[1]["starts"] == 120 and "starts" not in b.lineup[2], "not after the one before: back to its default"
    assert b.lineup[3]["starts"] == 400


def test_back_on_the_hub_goes_through_the_hosts_hook(reg, rs, officer):  # noqa: F811
    open_one_off(reg, rs, days=3)
    h = harness(reg, rs)
    wiz = flows_event.EditLineupWizard
    called = []

    async def back(self, i):
        called.append(True)
        await self.lineup_hub(i, "Back to where it came from.")

    wiz.lineup_back = back  # a host's own Back (a one-off's /raid open, a schedule's lineup step)
    try:
        run(FLOWS["raid_lineup"](h.command(), reg))
        run(h.press("Back"))
    finally:
        del wiz.lineup_back
    assert called and "Back to where it came from." in h.text


def test_add_a_raid_says_why_when_every_raid_is_in(reg, rs, officer):  # noqa: F811
    first = open_one_off(reg, rs, days=3)
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    others = [r for r in reg.profile.raids if r != first.instance]
    for rid in others[:2]:
        run(h.pick("Add a raid", rid))
    saved = dict(reg.profile.raids)
    try:  # a game of three raids: every one is in, under LINEUP_MAX
        for rid in list(reg.profile.raids):
            if rid not in {first.instance, *others[:2]}:
                del reg.profile.raids[rid]
        run(h.pick("Pick a step", "1"))
        run(h.press("Back"))
        assert child(h, "Add a raid").disabled
        assert flows_event.EVERY_RAID_IN in h.text and LINEUP_MAX_WORDS not in h.text
    finally:
        reg.profile.raids.clear()
        reg.profile.raids.update(saved)


def test_a_start_off_the_list_is_kept_as_its_own_option(reg, rs, officer):  # noqa: F811
    open_one_off(reg, rs, days=3, lineup=[{"raid": RAID_B, "rosters": 1, "starts": 500}])
    h = harness(reg, rs)
    run(FLOWS["raid_lineup"](h.command(), reg))
    run(h.pick("Pick a step", "2"))
    starts = child(h, "Starts")
    assert len(starts.options) <= 25
    picked = [o for o in starts.options if o.default]
    assert [o.value for o in picked] == ["500"]
