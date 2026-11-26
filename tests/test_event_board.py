"""The Events board before the lock is the OFFICERS' board (the owner's tweaks C, 2026-11-24): who they placed in which
group, everyone else who joined in Not placed. Nothing is solved on a page load, an answer or a drag; **Auto-fill
groups** runs the lock's own solve once (event_board.AutoFill → rc.autofill → rc._solve_run, the lock's strategy and
budget, `earlier` for a later raid) and writes its answer into the board as placements (ev.auto_placed), keeping every
hand placement and kept-off pin. A drop on Not placed only un-places a name; keeping someone off is the ⋮ pin. The
solver is a deterministic stub here (joiners in signup order) wherever the test is not about the solve itself; the real
solve is tests/test_event_board_solver.py (slow)."""
from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest
from conftest import OWNER, join
from test_confirm_boxes import people
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import event_board as eb
from oibot_gm import raidcycle as rc
from oibot_gm.constants import PAGE_WORDS
from oibot_gm.roster import solver

HYJAL, BD = "hyjal_summit_forever", "barrow_deeps"
EVENING = [{"raid": BD, "rosters": 2}]
B_ID = 300_000_000_000_000_003  # a second officer


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:  # the profile's raids first open 9 Dec: every event here is after it
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


def start_in(reg, days: float = 3):
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def one_off(reg, rs, lineup=EVENING, raid=HYJAL):
    return rc.open_run(reg, rs, raid, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=lineup)


def in_order(calls: list | None = None):
    """_solve_run without the solver: the joiners in signup order, each raid's pins and board places first, groups of
    group_size, as many rosters as run_rosters (records key, budget, whatif, `earlier` and the board it read per call)."""
    def solve(reg, rs, ev, strategy=None, avoid=None, time_limit=rc.SOLVE_TIME_LIMIT_S, whatif=True, avoid_groups=None, earlier=None, stop=None):
        if calls is not None:
            calls.append((ev.key, time_limit, whatif, dict(earlier) if earlier is not None else None, [list(g) for g in ev.layout or []]))
        players = rc.players_for(reg, ev)
        size, g = rc.run_size(reg, ev), int(reg.profile.comp_rules["group_size"])
        k, n = rc.groups_per_roster(reg, size), rc.run_rosters(reg, ev)
        groups = [list(x) for x in (ev.layout or [])] + [[] for _ in range(k * n)]
        groups = groups[: max(k * n, len(ev.layout or []))]
        placed = {x for gr in groups for x in gr}
        out = {sg.display_name for u, v in ev.pins.items() if v == "out" and (sg := ev.signups.get(u))}
        for p in players:
            if p.status != "signed" or p.signup_name in placed or p.signup_name in out:
                continue
            gi = next((i for i, gr in enumerate(groups) if len(gr) < g), None)
            if gi is None:
                break
            groups[gi].append(p.signup_name)
        rosters = rc.board_rosters(reg, ev, groups, size)
        return players, rosters, len(rosters)
    return solve


def fill(bot, reg, rs, ev, by="Jon", by_id=OWNER):
    """Press Auto-fill groups and wait for it: → the outcome sentence (None when the board was filled)."""
    async def go():
        task = eb.autofill_of(bot).start(reg, rs, ev, by=by, by_id=by_id, via="web")
        return await task
    return asyncio.run(go())


def placed(ev) -> set[str]:
    return {x for g in ev.layout or [] for x in g}


# ---------------------------------------------------------------- the board is the officers' board: nothing solves

def test_the_page_never_solves_and_draws_the_officers_board(reg, rs, monkeypatch, web_bot):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: calls.append(1))
    ev = one_off(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    assert calls == [], "a page load never solves"
    for part in j["parts"]:
        assert part["board_state"] == "open" and part["placed_words"] == f"0 of {len(ms)} joined"
        board = part["sheet"]["board"]
        assert all(not g for r in board["rosters"] for g in r["groups"]), "nobody placed: every group empty"
        assert len(board["bank"]) == len(ms), "everyone who joined is in Not placed"
        assert all(r["open"] is None for r in board["rosters"]), "no open seat drawn on an officers' board"
        assert part["autofill"] == {"working": False, "words": None, "failed": False}
    asyncio.run(web_bot.bot.board_move(reg, rs, ev, ms[0].display_name, (0, 0), "Jon", by_id=OWNER))
    rc.set_signup(reg, rs, ev, people(reg, 1)[0], None, "in", source="test")
    web_bot.c.get(f"/api/event/{ev.key}", headers=H())
    assert calls == [], "a drag and an answer never solve"
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["placed_words"] == f"1 of {len(ms) + 1} joined", "Placed counts the officers' board"
    assert part["sheet"]["board"]["rosters"][0]["groups"][0][0]["display_name"] == ms[0].display_name


def test_short_reads_the_needs_never_a_solve(reg, rs, monkeypatch, web_bot):
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: pytest.fail("no solve"))
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, people(reg, 3))
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["short"] == dict(rc.needs(reg, ev)["roles"])


def test_the_footer_and_the_bank_say_the_new_truth():
    assert "lock" in PAGE_WORDS["board_footer"] and PAGE_WORDS["autofill_button"] in PAGE_WORDS["board_footer"]
    assert "not_placed_note" not in PAGE_WORDS and "not_placed_off" not in PAGE_WORDS, "Not placed carries no how-to sentence (issue #12)"
    for gone in ("board_working", "board_failed"):
        assert gone not in PAGE_WORDS


# ---------------------------------------------------------------- a drop on Not placed only un-places

def test_a_drop_on_not_placed_only_unplaces(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, n[3], (0, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.board_move(reg, rs, ev, n[3], None, "Jon", by_id=OWNER))
    assert not ev.pins and placed(ev) == set() and ev.placed_by == {} and ev.drag_pins == {}, "un-placed, never kept off"
    assert rc.lock_forces(reg, rs, ev).kept_off == [], "the lock still places them"


def test_a_swap_from_not_placed_unplaces_the_partner(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, n[0], (0, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.board_move(reg, rs, ev, n[5], (0, 0), "Jon", by_id=OWNER))  # from Not placed onto n0
    assert ev.layout[0] == [n[5]] and n[0] not in placed(ev) and not ev.pins, "the partner goes back to Not placed, no pin"
    with pytest.raises(ValueError, match="full"):
        for x in n[6:12]:
            asyncio.run(bot.board_move(reg, rs, ev, x, (0, 9), "Jon", by_id=OWNER))


def test_a_drag_into_a_group_lifts_a_kept_off_pin_and_undo_puts_it_back(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    u = {x: str(m.discord_id) for x, m in zip(n, ms)}
    bot = NightBot(reg, rs)
    ev.pins[u[n[1]]] = "out"  # kept off through the ⋮ menu
    rs.save(ev, "a pin from the menu")
    asyncio.run(bot.board_move(reg, rs, ev, n[1], (0, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.board_move(reg, rs, ev, n[2], (1, 0), "Jon", by_id=OWNER))
    ev.pins[u[n[2]]] = "out"  # then plain text "keep n2 off": a placed name kept off leaves the board (prune_board)
    rs.save(ev, "a pin from elsewhere")
    assert ev.pins.get(u[n[1]]) is None and rc.own_moves(ev, OWNER) == [n[1]] and n[2] not in placed(ev)
    asyncio.run(bot.undo_moves(reg, rs, ev, OWNER, "Jon"))
    assert ev.pins[u[n[1]]] == "out", "the kept-off pin my drag lifted is back"
    assert ev.pins[u[n[2]]] == "out", "a pin set elsewhere is never touched"
    assert placed(ev) == set() and ev.placed_by == {} and ev.drag_pins == {}


def test_moves_from_elsewhere_clear_whose_move_it_was(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, n[0], (0, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.board_move(reg, rs, ev, n[0], (1, 0), "plain text"))  # from Discord / plain text / MCP: nobody's
    assert ev.placed_by == {} and rc.own_moves(ev, OWNER) == [], "my Undo never takes back someone else's later move"


def test_the_move_route_carries_the_officer(reg, rs, web_bot):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    r = web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": n[12], "to": {"r": 0, "g": 1, "i": 0}, "state": "open"}, headers=H())
    assert r.status_code == 200, r.text
    assert ev.layout[1] == [n[12]] and ev.placed_by == {n[12]: OWNER} and not ev.pins


def test_a_refused_move_answers_the_board_as_it_stands_and_its_sentence(reg, rs, web_bot):
    """Issue #12: the page draws the move's own answer, so a refusal must carry the board as it stands (the name back
    where it was) and the sentence the page shows — a full group, and a drag dropped after the event locked."""
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    g = int(reg.profile.comp_rules["group_size"])
    for i in range(g):
        assert web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": n[i], "to": {"r": 0, "g": 0, "i": i}, "state": "open"}, headers=H()).status_code == 200
    before = [list(x) for x in ev.layout]
    r = web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": n[g], "to": {"r": 0, "g": 0, "i": 99}, "state": "open"}, headers=H())
    assert r.status_code == 409 and r.json()["error"][:1].isupper(), r.text
    j = r.json()
    assert [s["display_name"] for s in j["board"]["rosters"][0]["groups"][0]] == n[:g], "the full group as it stands"
    assert n[g] in [s["display_name"] for s in j["board"]["bank"]], "the name is back in Not placed"
    assert j["rev"] == rc.board_rev(reg, ev) and ev.layout == before, "nothing written"
    ev.state = "locked"
    r = web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": n[g], "to": {"r": 0, "g": 1, "i": 0}, "state": "open"}, headers=H())
    assert r.status_code == 409 and "locked" in r.json()["error"] and "board" in r.json() and r.json()["state"] == "locked"


# ---------------------------------------------------------------- Undo my moves

def test_undo_drops_only_my_moves(reg, rs, web_bot):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = web_bot.bot
    run = asyncio.run
    run(bot.board_move(reg, rs, ev, n[0], (0, 0), "Jon", by_id=OWNER))
    run(bot.board_move(reg, rs, ev, n[1], (0, 1), "Jon", by_id=OWNER))
    run(bot.board_move(reg, rs, ev, n[2], (1, 0), "Bea", by_id=B_ID))
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["mine"] == sorted([n[0], n[1]])
    assert part["undo_box"]["sentences"] == ["Your 2 moves on this board are dropped.", "Those people go back to Not placed."]
    box = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "preview": True}, headers=H()).json()["box"]
    assert box == part["undo_box"], "the route's box is the page's"
    run(bot.board_move(reg, rs, ev, n[0], (2, 0), "Bea", by_id=B_ID))  # B moves one of mine meanwhile
    r = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "seen": box["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["box"]["sentences"][0] == "Your one move on this board is dropped."
    r = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "seen": r.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    assert ev.placed_by == {n[0]: B_ID, n[2]: B_ID} and placed(ev) == {n[0], n[2]}


def test_placed_by_and_auto_placed_are_pruned_with_the_board_and_cleared_at_the_lock(reg, rs, monkeypatch):
    monkeypatch.setattr(rc, "propose", lambda reg_, rs_, ev_, save=True: setattr(ev_, "rosters", rc.board_rosters(
        reg_, ev_, [[s.display_name for s in ev_.by_status("in")][:5]], rc.run_size(reg_, ev_))))
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:8])
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, n[0], (0, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.board_move(reg, rs, ev, n[1], (1, 0), "Jon", by_id=OWNER))
    ev.auto_placed = [n[2]]
    asyncio.run(bot.save_board(reg, rs, ev, [[n[1]], [n[2]]], by="Jon"))  # a whole-board write without n[0]
    assert ev.placed_by == {n[1]: OWNER} and ev.auto_placed == [], "a whole board written by a person is theirs"
    asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked" and ev.placed_by == {} and ev.drag_pins == {} and ev.auto_placed == []


# ---------------------------------------------------------------- Auto-fill groups

def test_autofill_writes_the_locks_solve_as_placements_and_keeps_hand_ones(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    ev.pins[str(ms[4].discord_id)] = "out"  # kept off through ⋮
    rs.save(ev, "kept off")
    asyncio.run(bot.board_move(reg, rs, ev, n[9], (2, 0), "Jon", by_id=OWNER))  # a hand placement in group 3
    assert fill(bot, reg, rs, ev) is None
    assert calls[0][1:3] == (rc.SOLVE_TIME_LIMIT_S, False), "the lock's budget"
    assert calls[0][4][2] == [n[9]], "the hand placement is fixed in the solve"
    assert ev.layout[2][0] == n[9] and ev.placed_by == {n[9]: OWNER}, "the hand placement stays where it was, still mine"
    assert n[4] not in placed(ev) and ev.pins == {str(ms[4].discord_id): "out"}, "the kept-off pin honoured, untouched"
    assert set(ev.auto_placed) == placed(ev) - {n[9]}, "every other placement is the Auto-fill's"
    assert any("auto-filled the board" in line for line in ev.log)
    assert rc.own_moves(ev, OWNER) == [n[9]], "Undo my moves never takes back an Auto-fill"


def test_autofill_again_replaces_its_own_placements_and_keeps_hand_ones(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    fill(bot, reg, rs, ev)
    first = set(ev.auto_placed)
    asyncio.run(bot.board_move(reg, rs, ev, n[0], None, "Jon", by_id=OWNER))  # one back to Not placed: room in group 1
    asyncio.run(bot.board_move(reg, rs, ev, n[7], (0, 9), "Jon", by_id=OWNER))  # an auto-placed name moved by hand
    assert n[7] not in ev.auto_placed and n[0] not in ev.auto_placed, "a name moved by hand is a hand placement"
    fill(bot, reg, rs, ev)
    assert calls[-1][4] == [[n[7]], [], [], []], "the second solve reads only the hand placement: the old fill is replaced"
    assert ev.layout[0][0] == n[7] and n[7] not in ev.auto_placed
    assert n[0] in ev.auto_placed, "a name dragged to Not placed is placed again by the next Auto-fill"
    assert set(ev.auto_placed) == placed(ev) - {n[7]} and first


def test_autofill_on_a_later_raid_reads_the_earlier_raids_as_the_lock_would(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    ev = one_off(reg, rs)
    join(reg, rs, ev, reg.test_members())
    first, bd = rc.event_parts(rs, ev)
    bot = NightBot(reg, rs)
    assert fill(bot, reg, rs, bd) is None
    assert [c[0] for c in calls] == [first.key, bd.key], "the earlier raid is solved first (its lock's answer), then this one"
    on_first = {p.discord_id for r in rc.event_board_preview(reg, rs, first, parts=[first]).get(first.key) for p in r.selected}
    assert set(calls[1][3]) == on_first, "`earlier` = who the first raid's lock rosters"
    assert first.layout is None and bd.layout, "only the pressed raid's board is written"


def test_a_board_changed_while_it_solved_is_never_overwritten(reg, rs, monkeypatch):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    bot = NightBot(reg, rs)
    real = in_order()

    def solve(reg_, rs_, p, *a, **k):  # another officer drags while the solve runs
        out = real(reg_, rs_, p, *a, **k)
        live = rs.events[ev.key]
        live.layout = [[ms[0].display_name]]
        rs.save(live, "a drag meanwhile")
        return out

    monkeypatch.setattr(rc, "_solve_run", solve)
    said = fill(bot, reg, rs, ev)
    assert said == PAGE_WORDS["autofill_changed"] and ev.layout == [[ms[0].display_name]] and ev.auto_placed == []
    st = eb.autofill_of(bot).state(reg, ev)
    assert st == {"working": False, "words": PAGE_WORDS["autofill_changed"], "failed": True}


def test_a_failure_says_the_solvers_sentence_and_writes_nothing(reg, rs, monkeypatch, web_bot):
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no roster found: INFEASIBLE")))
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    assert fill(web_bot.bot, reg, rs, ev) == rc.SOLVER_WORDS["infeasible"] and ev.layout is None
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["autofill"] == {"working": False, "words": rc.SOLVER_WORDS["infeasible"], "failed": True}
    asyncio.run(web_bot.bot.board_move(reg, rs, ev, ev.by_status("in")[0].display_name, (0, 0), "Jon", by_id=OWNER))
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["autofill"]["words"] is None, "the sentence was about the board before the drag"


def test_a_lock_stops_an_autofill_and_it_writes_nothing(reg, rs, monkeypatch):
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    bot = NightBot(reg, rs)
    started, proceed = threading.Event(), threading.Event()

    def slow(reg_, rs_, p, *a, stop=None, **k):
        started.set()
        proceed.wait(10)
        if stop is not None and stop.stopped:
            raise solver.Stopped()
        return in_order()(reg_, rs_, p, *a, stop=stop, **k)

    monkeypatch.setattr(rc, "_solve_run", slow)

    async def go():
        af = eb.autofill_of(bot)
        task = af.start(reg, rs, ev, by="Jon", by_id=OWNER, via="web")
        await asyncio.to_thread(started.wait, 10)
        assert af.state(reg, ev)["working"] is True and af.state(reg, ev)["words"] == PAGE_WORDS["autofill_working"]
        async with bot.solving(reg, ev.key, lock=True):  # a lock begins: the auto-fill gives way at once
            proceed.set()
        return await task

    assert asyncio.run(go()) == PAGE_WORDS["autofill_stopped"] and ev.layout is None


def test_no_autofill_starts_while_a_lock_solves(reg, rs):
    from oibot_gm.registry import RegistryError

    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    bot = NightBot(reg, rs)

    async def go():
        async with bot.solving(reg, ev.key):
            with pytest.raises(RegistryError, match=PAGE_WORDS["autofill_busy"]):
                eb.autofill_of(bot).start(reg, rs, ev, by="Jon", by_id=OWNER, via="web")

    asyncio.run(go())


def test_the_route_answers_at_once_and_refuses_a_locked_raid(reg, rs, monkeypatch, web_bot):
    from conftest import lock_with_board

    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={"wait": True}, headers=H())
    assert r.status_code == 200 and r.json()["message"] == PAGE_WORDS["autofill_done"] and ev.auto_placed, r.text
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={}, headers=H())
    assert r.status_code == 200 and r.json()["message"] == PAGE_WORDS["autofill_working"], "answered at once: the page shows the working words"
    lock_with_board(reg, rs, ev, [s.display_name for s in ev.by_status("in")][:10])
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={}, headers=H())
    assert r.status_code == 400 and r.json()["error"] == PAGE_WORDS["autofill_locked"]


def test_autofill_with_nobody_joined_is_refused(reg, rs, web_bot):
    ev = one_off(reg, rs, lineup=[])
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={}, headers=H())
    assert r.status_code == 400 and r.json()["error"] == PAGE_WORDS["autofill_nobody"]


def test_auto_placed_survives_a_restart_and_older_files_read_without_it(reg, rs, monkeypatch, store):
    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    fill(NightBot(reg, rs), reg, rs, ev)
    again = rc.RaidStore(store, reg.key)
    assert again.events[ev.key].auto_placed == ev.auto_placed and ev.auto_placed
    old = ev.model_dump()
    old.pop("auto_placed")
    assert rc.RaidEvent.model_validate(old).auto_placed == [], "a file from before reads as no auto placements"


# ---------------------------------------------------------------- Clear the board (More)

def test_clear_the_board_through_the_servers_box(reg, rs, monkeypatch, web_bot):
    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    first, bd = rc.event_parts(rs, ev)
    bot = web_bot.bot
    asyncio.run(bot.board_move(reg, rs, first, ms[0].display_name, (0, 0), "Jon", by_id=OWNER))
    fill(bot, reg, rs, bd)
    first.pins[str(ms[3].discord_id)] = "out"
    rs.save(first, "kept off")
    n = len(placed(first) | placed(rs.events[bd.key]))  # people, not places: one placed on both raids counts once
    url = f"/api/event/{ev.key}/clear"
    box = web_bot.c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert box["title"].startswith("Clear the board") and box["verb"] == "Clear the board" and not box["danger"]
    assert box["sentences"] == [f"The {n} people on the board go back to Not placed.", "Nobody is told.", "Auto-fill groups can place everyone again."]
    asyncio.run(bot.board_move(reg, rs, first, ms[1].display_name, (0, 1), "Jon", by_id=OWNER))  # a drag while the box is open
    n2 = len(placed(first) | placed(rs.events[bd.key]))
    r = web_bot.c.post(url, json={"seen": box["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["box"]["sentences"][0] == f"The {n2} people on the board go back to Not placed."
    r = web_bot.c.post(url, json={"seen": r.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    for p in (first, bd):
        assert placed(p) == set() and p.auto_placed == [] and p.placed_by == {}
    assert first.pins == {str(ms[3].discord_id): "out"}, "pins untouched"
    r = web_bot.c.post(url, json={"preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["verb"] is None, "nothing placed: a refusal, only Back"


def test_clear_is_refused_after_the_lock(reg, rs, web_bot):
    from conftest import lock_with_board

    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members())
    lock_with_board(reg, rs, ev, [s.display_name for s in ev.by_status("in")][:10])
    r = web_bot.c.post(f"/api/event/{ev.key}/clear", json={"preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["verb"] is None


# ---------------------------------------------------------------- what stays: a locked event, open seats, the preview helper

def test_a_locked_event_draws_its_roster_and_never_solves(reg, rs, monkeypatch, web_bot):
    from conftest import lock_with_board

    calls = []
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: calls.append(1))
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members())
    names = [s.display_name for s in ev.by_status("in")][:10]
    lock_with_board(reg, rs, ev, names)
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["board_state"] == "locked" and calls == [] and part["mine"] == [] and part["undo_box"] is None
    assert part["autofill"] is None
    assert sorted(n["display_name"] for r in part["sheet"]["board"]["rosters"] for g in r["groups"] for n in g) == sorted(names)


def test_a_locked_raid_counts_seats_and_undo_says_why_it_is_greyed(reg, rs, web_bot):
    from conftest import lock_with_board

    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["undo_box"] is None and part["undo_why"] == PAGE_WORDS["no_moves"]
    assert part["rostered_words"] is None and part["placed_words"] == f"0 of {len(ms)} joined"
    bd = rc.open_run(reg, rs, BD, start_in(reg, 4), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, bd, ms[:9])
    rc.set_signup(reg, rs, bd, ms[9], None, "sub", source="test")  # the lock seats someone from the Bench
    lock_with_board(reg, rs, bd, [s.display_name for s in bd.by_status("in")] + [bd.signups[str(ms[9].discord_id)].display_name])
    locked = web_bot.c.get(f"/api/event/{bd.key}", headers=H()).json()["parts"][0]
    assert locked["placed_words"] is None and locked["undo_why"] is None
    assert locked["rostered_words"] == PAGE_WORDS["seats_of"].format(n=10, size=10)


def test_open_seats_count_each_minimum_once_and_never_past_the_empty_seats(reg, rs):
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    reg.set_minimums(BD, {"Paladin:Holy": {"min": 1}, "Mage": {"min": 1}}, "t")
    size = rc.run_size(reg, ev)
    empty = rc.board_rosters(reg, ev, [[] for _ in range(2)], size)
    mins = reg.minimums(BD, size)
    seats = rc.open_seat_roles(reg, ev, empty)[0]
    assert seats[: mins["tank"]] == ["tank"] * mins["tank"]
    assert seats.count("healer") == mins["healer"], "the Holy Paladin seat is one of the healer seats, not one more"
    assert seats[-1] == "ranged", "a Mage seat drawn with its role, after the role minimums"
    joined = reg.test_members()[:size]
    join(reg, rs, ev, joined)
    full = rc.board_rosters(reg, ev, [[m.display_name for m in joined[:5]], [m.display_name for m in joined[5:10]]], size)
    assert rc.open_seat_roles(reg, ev, full) == [[]], "a full roster has no open seat whatever it is short"


def test_the_preview_uses_the_lock_budget_and_hands_later_raids_the_earlier_preview(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    ev = one_off(reg, rs)
    join(reg, rs, ev, reg.test_members())
    pv = rc.event_board_preview(reg, rs, ev)
    first, bd = rc.event_parts(rs, ev)
    assert [c[0] for c in calls] == [first.key, bd.key]
    assert all(c[1] == rc.SOLVE_TIME_LIMIT_S and c[2] is False for c in calls), "the lock's budget: the lock's answer"
    on_first = {p.discord_id for r in pv[first.key] for p in r.selected}
    assert calls[0][3] is None and set(calls[1][3]) == on_first and set(calls[1][3].values()) == {1}
    assert rs.events[first.key].layout is None and not rs.events[first.key].rosters, "nothing written"


def test_a_failed_part_keeps_the_others(reg, rs, monkeypatch):
    LINE = [{"raid": BD, "rosters": 1}, {"raid": "onyxias_lair", "rosters": 1}]
    ev = rc.open_run(reg, rs, HYJAL, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=LINE)
    join(reg, rs, ev, reg.test_members())
    parts = rc.event_parts(rs, ev)
    real, seen = in_order(), []

    def solve(reg_, rs_, p, *a, **k):
        seen.append(k.get("earlier"))
        if p.key == parts[1].key:
            raise RuntimeError("INFEASIBLE")
        return real(reg_, rs_, p, *a, **k)

    monkeypatch.setattr(rc, "_solve_run", solve)
    pv = rc.event_board_preview(reg, rs, ev)
    assert isinstance(pv[parts[0].key], list) and isinstance(pv[parts[2].key], list)
    assert pv[parts[1].key] == rc.SOLVER_WORDS["infeasible"]
    assert set(seen[2]) == {p.discord_id for r in pv[parts[0].key] for p in r.selected}, "the third raid prefers the first raid's preview"


def test_a_later_raids_board_places_reach_the_solve_past_its_first_group(reg, rs, monkeypatch):
    """_solve_run used to reuse `k` (the groups per roster) for the part number on a later raid, cutting its board pins
    to `gi < part × rosters`: a place in group 2 of a later raid was dropped by the lock."""
    ev = one_off(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    bd = rc.event_parts(rs, ev)[1]
    bd.layout = [[], [], [ms[0].display_name]]  # roster 2, group 1
    rs.save(bd, "a place")
    seen = {}

    class Seen(Exception):
        pass

    def capture(profile, players, raid_id, opts, **kw):
        seen["pins"] = opts.pins
        raise Seen

    monkeypatch.setattr(solver, "solve_rosters", capture)
    with pytest.raises(Seen):
        rc._solve_run(reg, rs, bd, earlier={})
    assert seen["pins"] == {ms[0].display_name: 2}
    with pytest.raises(Seen):
        rc._solve_run(reg, rs, bd)  # the lock's own path (no earlier given) keeps it too
    assert seen["pins"] == {ms[0].display_name: 2}


def test_the_autofill_reads_the_events_own_first_come_priority_and_split(reg, rs, monkeypatch):
    """Part B's values are the run's own (Edit event): Auto-fill reads them through _solve_run (run_first_come,
    seat_bonus, run_strategy) on the run itself — a change to any of them reaches the next press."""
    seen = []

    def capture(reg_, rs_, p, *a, **k):
        seen.append((rc.run_first_come(reg_, p), rc.run_strategy(reg_, p), rc.run_team(reg_, p).get("weights")))
        return in_order()(reg_, rs_, p, *a, **k)

    monkeypatch.setattr(rc, "_solve_run", capture)
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    bot = NightBot(reg, rs)
    fill(bot, reg, rs, ev)
    team = rc.run_team(reg, ev)
    team["first_come"] = not rc.run_first_come(reg, ev)
    team["weights"] = {**(team.get("weights") or {}), "rank": 6}
    reg.save_config("t: the event's own", notify=False)
    ev.split_strategy = "rotation"
    rs.save(ev, "split")
    fill(bot, reg, rs, ev)
    assert seen[0] != seen[1] and seen[1][0] == team["first_come"] and seen[1][1] == "rotation" and seen[1][2]["rank"] == 6


# ---------------------------------------------------------------- the live two-raid event as it is stored today

def test_the_live_two_raid_event_shows_an_empty_board_and_writes_nothing(data_root, old_store_with, monkeypatch):
    """big-guys-for's open evening (a one-off Hyjal lead with a Barrow Deeps follow-on, phase 2's file shape, no board
    placements), with 18 joined: after the start's stamps its event page shows each raid's empty board with all 18 in
    Not placed, solves nothing and writes nothing — the run files are byte for byte what they were, so the lock reads
    exactly what it read before (tests/test_event_board_solver.py and the model-hash probe cover the lock itself)."""
    from fastapi.testclient import TestClient
    from test_event_identity import _big_guys_pair, _old_guild, _soon
    from test_mcp import TOKEN

    from oibot_gm.web import app as web

    classes = [("Warrior", "Protection", "tank"), ("Priest", "Holy", "healer"), ("Mage", "Fire", "ranged"), ("Rogue", "Combat", "melee"),
               ("Druid", "Restoration", "healer"), ("Hunter", "Marksmanship", "ranged")]
    t = _soon(48).replace(hour=18, minute=30)
    cfg, _dicts, files = _big_guys_pair(t)
    signups = {str(900 + i): rc.Signup(discord_id=900 + i, display_name=f"P{i}", character=f"P{i}", cls=classes[i % 6][0], spec=classes[i % 6][1],
                                       role=classes[i % 6][2], status="in", updated_at="2026-11-20T10:00:00+00:00",
                                       joined_at=f"2026-11-20T10:{i:02d}:00+00:00").model_dump(mode="json") for i in range(18)}
    for f in files:
        f["signups"] = signups
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    rc.migrate_at_start(reg, rs)
    store.flush()
    lk, fk = files[0]["key"], files[1]["key"]
    raids = data_root / reg.key / "raids"
    before = {k: (raids / f"{k}.json").read_bytes() for k in (lk, fk)}
    inputs = {k: rc.solve_inputs(rs.events[k]) for k in (lk, fk)}
    calls = []
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: calls.append(1))
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    c = TestClient(web.create_app(NightBot(reg, rs)))
    j = c.get(f"/api/event/{lk}", headers=H()).json()
    assert [p["key"] for p in j["parts"]] == [lk, fk]
    for part in j["parts"]:
        board = part["sheet"]["board"]
        assert part["board_state"] == "open" and part["placed_words"] == "0 of 18 joined"
        assert all(not g for r in board["rosters"] for g in r["groups"]) and len(board["bank"]) == 18
        assert part["autofill"] == {"working": False, "words": None, "failed": False}
    store.flush()
    assert calls == [], "nothing solved"
    assert {k: (raids / f"{k}.json").read_bytes() for k in (lk, fk)} == before, "nothing written: the lock reads what it read"
    assert {k: rc.solve_inputs(rs.events[k]) for k in (lk, fk)} == inputs
    assert all(rs.events[k].layout is None and not rs.events[k].pins and rs.events[k].auto_placed == [] for k in (lk, fk))


# ---------------------------------------------------------------- fix round: the board follows the answers and the pins

def filled(reg, rs, monkeypatch, lineup=(), n=10):
    """A real-member event (people()) with `n` joined, auto-filled through the in-order stub: everyone placed."""
    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs, lineup=list(lineup))
    ms = people(reg, n)
    join(reg, rs, ev, ms)
    bot = NightBot(reg, rs)
    assert fill(bot, reg, rs, ev) is None and placed(ev) == {m.display_name for m in ms}
    return ev, ms, bot


def gone(ev, m) -> bool:
    n = m.display_name
    return n not in placed(ev) and n not in ev.auto_placed and n not in ev.placed_by and n not in ev.placed_as


def test_keep_off_takes_a_placed_name_off_the_board(reg, rs, monkeypatch):
    from oibot_gm import inbox

    ev, ms, bot = filled(reg, rs, monkeypatch)
    sentences = inbox.action_consequence(reg, ev, ms[2], "pin_out") if hasattr(inbox, "action_consequence") else None
    inbox.pin_write(reg, rs, ev, ms[2], "out", "Jon")
    assert gone(ev, ms[2]) and ms[2].display_name in rc.lock_forces(reg, rs, ev).kept_off, "kept off wins: the lock leaves them out"
    if sentences is not None:
        assert "They come off the board now." in sentences


def test_leaving_join_and_bench_takes_a_placed_name_off_every_raid_of_the_event(reg, rs, monkeypatch):
    ev, ms, bot = filled(reg, rs, monkeypatch, lineup=[{"raid": BD, "rosters": 1}])
    bd = rc.event_parts(rs, ev)[1]
    assert fill(bot, reg, rs, bd) is None and ms[1].display_name in placed(bd)
    asyncio.run(bot.set_answer(reg, rs, ev, ms[1], "sub", None, "Jon", by_id=OWNER))  # an officer answers Bench for them
    assert not gone(ev, ms[1]) and not gone(rs.events[bd.key], ms[1]), "a Bench answer keeps its place on both boards (task 5)"
    assert ms[1].display_name in placed(ev)
    # Auto-fill placed them: as a Bench answer that place is decided again at the lock (task 5 fix), never kept on
    assert ms[1].display_name in ev.auto_placed and ms[1].display_name not in rc.lock_forces(reg, rs, ev).placed
    asyncio.run(bot.set_answer(reg, rs, ev, ms[1], "out", None, "Jon", by_id=OWNER))  # then No thanks
    assert gone(ev, ms[1]) and gone(rs.events[bd.key], ms[1]), "the answer is the evening's: both boards"
    assert ms[1].display_name not in rc.lock_forces(reg, rs, ev).placed
    asyncio.run(bot.set_answer(reg, rs, ev, ms[3], "out", None, ms[3].display_name, by_id=ms[3].discord_id))  # their own No thanks
    assert gone(ev, ms[3])
    asyncio.run(bot.call_out(reg, rs, ev, ms[4], "sick", "Jon", by_id=OWNER))  # Can't make it
    assert gone(ev, ms[4])


def test_an_absence_and_a_retired_character_take_a_placed_name_off(reg, rs, monkeypatch):
    ev, ms, bot = filled(reg, rs, monkeypatch)
    day = ev.start.astimezone(reg.tz).date().isoformat()
    asyncio.run(bot.record_absence(reg, ms[5].discord_id, day, day, None, "Jon", by_id=OWNER))
    assert gone(rs.events[ev.key], ms[5])
    asyncio.run(bot.save_characters(reg, ms[6].discord_id, [{"label": ms[6].main.label, "retire": True}], ms[6].display_name, by_id=ms[6].discord_id))
    assert gone(rs.events[ev.key], ms[6])


def test_a_changed_character_or_spec_takes_a_placed_name_off(reg, rs, monkeypatch):
    ev, ms, bot = filled(reg, rs, monkeypatch)
    nyx = ms[0]  # Warrior Protection; add an alt and answer with it
    reg.add_character(nyx.discord_id, nyx.display_name, "Nyxalt", "Mage", "Fire", None, False)
    rc.set_signup(reg, rs, ev, reg.members[nyx.discord_id], "Nyxalt", "in", source="test")
    assert gone(ev, nyx), "another character for the night: back to Not placed"
    asyncio.run(bot.board_move(reg, rs, ev, nyx.display_name, (3, 0), "Jon", by_id=OWNER))
    assert nyx.display_name in placed(ev) and ev.placed_as[nyx.display_name] == "Nyxalt|Fire"
    rc.set_signup(reg, rs, ev, reg.members[nyx.discord_id], "Nyxalt", "in", source="test")  # the same answer again
    assert nyx.display_name in placed(ev), "the same character and spec pressed again stays placed"


def test_leaving_the_server_takes_a_placed_name_off(reg, rs, monkeypatch):
    ev, ms, bot = filled(reg, rs, monkeypatch)
    from oibot_gm.discord_pool import SetupMixin

    SetupMixin.note_membership(bot, reg.config.discord_guild_id, ms[7].discord_id, False)  # Discord's own 404: they left
    assert reg.members[ms[7].discord_id].left_at and gone(rs.events[ev.key], ms[7])
    assert ev.signups[str(ms[7].discord_id)].status == "in", "their answer stays as they gave it"


def test_after_the_lock_the_board_never_prunes(reg, rs, monkeypatch):
    from conftest import lock_with_board

    ev, ms, bot = filled(reg, rs, monkeypatch)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    ev.layout = [[ms[0].display_name]]  # (a locked run's layout is never read; prune leaves a locked run alone)
    rc.set_signup(reg, rs, ev, ms[0], None, "sub", source="test")
    assert ev.layout == [[ms[0].display_name]]


def test_a_second_press_with_nothing_changed_writes_nothing(reg, rs, monkeypatch):
    ev, ms, bot = filled(reg, rs, monkeypatch)
    log = len(ev.log)
    assert fill(bot, reg, rs, ev) == PAGE_WORDS["autofill_same"] and len(ev.log) == log
    st = eb.autofill_of(bot).state(reg, ev)
    assert st == {"working": False, "words": PAGE_WORDS["autofill_same"], "failed": False}


def test_an_answer_changed_while_it_solves_is_never_written_over(reg, rs, monkeypatch):
    ev = one_off(reg, rs, lineup=[])
    ms = people(reg, 10)
    join(reg, rs, ev, ms)
    bot = NightBot(reg, rs)
    real = in_order()

    def solve(reg_, rs_, p, *a, **k):  # a spec switch lands while the solve runs: the board is unchanged, the answers aren't
        out = real(reg_, rs_, p, *a, **k)
        rc.set_signup(reg, rs, rs.events[ev.key], ms[1], None, "sub", source="test")
        return out

    monkeypatch.setattr(rc, "_solve_run", solve)
    assert fill(bot, reg, rs, ev) == PAGE_WORDS["autofill_changed"] and ev.layout is None


def test_a_preview_stops_only_its_own_events_autofill(reg, rs):
    bot = NightBot(reg, rs)
    a, b = one_off(reg, rs, lineup=[]), rc.open_run(reg, rs, BD, start_in(reg, 5), by="t", schedule=rc.ADHOC_SCHEDULE)
    af = eb.autofill_of(bot)
    sa, sb = solver.Stop(), solver.Stop()
    af.stops = {(reg.key, a.key): sa, (reg.key, b.key): sb}
    af.events = {(reg.key, a.key): rc.event_id(a), (reg.key, b.key): rc.event_id(b)}

    async def preview_then_lock():
        async with bot.solving(reg, a.key):  # a Preview lock on event a
            pass
        assert sa.stopped and sa.why == PAGE_WORDS["autofill_stopped_preview"] and not sb.stopped, "only its own event's"
        async with bot.solving(reg, a.key, lock=True):  # a real lock: every auto-fill gives way
            pass
        assert sb.stopped and sb.why == PAGE_WORDS["autofill_stopped"]

    asyncio.run(preview_then_lock())


def test_a_later_raid_counts_a_complete_earlier_board_instead_of_solving_it(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    ev = one_off(reg, rs, lineup=[{"raid": BD, "rosters": 1}])
    ms = people(reg, 8)
    join(reg, rs, ev, ms)
    first, bd = rc.event_parts(rs, ev)
    bot = NightBot(reg, rs)
    fill(bot, reg, rs, first)  # everyone who joined is placed on the first raid: its lock has nothing to decide
    calls.clear()
    fill(bot, reg, rs, bd)
    assert [c[0] for c in calls] == [bd.key], "the complete first board is counted, never solved"
    assert set(calls[0][3]) == {m.discord_id for m in ms}
    asyncio.run(bot.board_move(reg, rs, first, ms[0].display_name, None, "Jon", by_id=OWNER))  # incomplete again
    calls.clear()
    fill(bot, reg, rs, rs.events[bd.key])
    assert [c[0] for c in calls] == [first.key, bd.key], "an incomplete board is solved, as its lock would"


def test_clear_counts_people_once_across_raids(reg, rs, monkeypatch):
    from oibot_gm import consequences as cq

    ev, ms, bot = filled(reg, rs, monkeypatch, lineup=[{"raid": BD, "rosters": 1}])
    bd = rc.event_parts(rs, ev)[1]
    fill(bot, reg, rs, bd)
    n = len({m.display_name for m in ms} & (placed(ev) | placed(rs.events[bd.key])))
    assert cq.clear_board_box(reg, rs, ev)["sentences"][0] == f"The {n} people on the board go back to Not placed."


def test_the_test_bench_place_the_rest_gets_its_board_and_its_rev_is_honoured(reg, rs, monkeypatch, web_bot):
    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={"rev": "stale", "wait": True}, headers=H())
    assert r.status_code == 409 and r.json()["board"] and ev.layout is None, "a board changed since: nothing solved"
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={"rev": rc.board_rev(reg, ev), "wait": True}, headers=H())
    j = r.json()
    assert r.status_code == 200 and j["board"]["rosters"] and j["rev"] == rc.board_rev(reg, ev) and j["needs"] is not None, r.text
    assert sum(len(g) for ro in j["board"]["rosters"] for g in ro["groups"]) == len(placed(ev)) > 0
    r = web_bot.c.post(f"/api/run/{ev.key}/autofill", json={}, headers=H())
    assert r.json()["board"], "the at-once answer carries the board as it is"


def test_a_bench_answer_the_solve_seats_is_written_to_the_board_once(reg, rs, monkeypatch):
    """The lock seats a Bench answer where no joiner can take the seat; Auto-fill shows what the lock would roster, so
    the board holds that Bench answer too (task 5: prune_board keeps it), and an unchanged press writes nothing — the
    loop found on the scratch server, where prune_board took it straight off again, cannot come back."""
    ev = one_off(reg, rs, lineup=[])
    ms = people(reg, 6)
    join(reg, rs, ev, ms[:5])
    rc.set_signup(reg, rs, ev, ms[5], None, "sub", source="test")

    def solve(reg_, rs_, p, *a, **k):  # seats everyone, the Bench answer too
        players = rc.players_for(reg_, p)
        rosters = rc.board_rosters(reg_, p, [[x.signup_name for x in players]], rc.run_size(reg_, p))
        return players, rosters, 1

    monkeypatch.setattr(rc, "_solve_run", solve)
    bot = NightBot(reg, rs)
    assert fill(bot, reg, rs, ev) is None and placed(ev) == {m.display_name for m in ms}
    assert ms[5].display_name in rs.events[ev.key].auto_placed
    assert fill(bot, reg, rs, ev) == PAGE_WORDS["autofill_same"], "an unchanged press writes nothing"
