"""Phase 4, Task 3: the Events board before the lock is drawn the way the lock would place people NOW — the lock's own
solve (rc.event_board_preview → rc._solve_run), computed once per fingerprint in the background (event_board), a drag
applied to the picture as the server holds it, and Undo my moves counted and done by the server. The solver is a
deterministic stub here (joiners in signup order) wherever the test is not about the solve itself; the real solve is
tests/test_event_board_solver.py (slow)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from conftest import OWNER, join
from test_confirm_boxes import people
from test_raid_night import H, NightBot, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import event_board as eb
from oibot_gm import raidcycle as rc

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
    group_size, as many rosters as run_rosters (records `earlier` per call)."""
    def solve(reg, rs, ev, strategy=None, avoid=None, time_limit=rc.SOLVE_TIME_LIMIT_S, whatif=True, avoid_groups=None, earlier=None):
        if calls is not None:
            calls.append((ev.key, time_limit, whatif, dict(earlier) if earlier is not None else None))
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


def flat(rosters) -> list[list[str]]:
    return [list(g) for r in rosters for g in r.groups]


def get(cache, reg, rs, ev):
    return asyncio.run(cache.get(reg, rs, ev))


def settle(cache, reg, rs, ev):
    """Ask, let the background solve finish, ask again: what a page sees after the `run:` reload."""
    async def go():
        first = await cache.get(reg, rs, ev)
        while cache.tasks:
            await asyncio.gather(*list(cache.tasks.values()))
        return first, await cache.get(reg, rs, ev)
    return asyncio.run(go())


# ---------------------------------------------------------------- Review Focus 1: nobody joined

def test_an_event_with_nobody_joined_draws_open_seats_and_never_solves(reg, rs, monkeypatch, web_bot):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: calls.append(1))
    ev = one_off(reg, rs)
    pv = rc.event_board_preview(reg, rs, ev)
    assert calls == [] and set(pv) == {p.key for p in rc.event_parts(rs, ev)}
    assert all(sum(len(g) for r in v for g in r.groups) == 0 for v in pv.values())
    assert len(pv[rc.event_parts(rs, ev)[1].key]) == 2, "every expected roster is drawn, empty"
    assert rc.open_seat_roles(reg, ev, pv[ev.key])[0][:2] == ["tank", "tank"]
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    assert calls == [] and eb.cache_of(web_bot.bot).solves == 0, "the route never starts a solve for nobody"
    for part in j["parts"]:
        assert part["board_state"] == "ready" and part["placed_words"] == "0 of 0 joined"
        rosters = part["sheet"]["board"]["rosters"]
        assert all(not g for r in rosters for g in r["groups"])
        assert all(r["open"][:2] == ["tank", "tank"] for r in rosters)
        size = part["sheet"]["size"]
        assert all(len(r["open"]) <= size for r in rosters)


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


# ---------------------------------------------------------------- the preview: the lock's own solve

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
    bench = pv[first.key][0].benched
    assert {p.signup_name for p in bench} == {s.display_name for s in first.by_status("in")} - {p.signup_name for r in pv[first.key] for p in r.selected}


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
    assert pv[parts[1].key].startswith("No roster satisfies the rules") and pv[parts[1].key].endswith(".")
    assert set(seen[2]) == {p.discord_id for r in pv[parts[0].key] for p in r.selected}, "the third raid prefers the first raid's preview"


def test_the_route_draws_the_lock_placement_once_it_is_ready(reg, rs, monkeypatch, web_bot):
    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs)
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    _, (st, previews) = settle(eb.cache_of(web_bot.bot), reg, rs, ev)  # the background solve, finished
    assert st == "ready"
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    hy, bd = j["parts"]
    assert hy["board_state"] == bd["board_state"] == "ready" and hy["board_error"] is None
    assert hy["placed_words"] == f"{len(ms)} of {len(ms)} joined", "Placed counts the lock's placement"
    drawn = [[s["display_name"] for s in g] for r in hy["sheet"]["board"]["rosters"] for g in r["groups"]]
    assert drawn == flat(previews[ev.key]) and len(bd["sheet"]["board"]["rosters"]) == 2
    assert hy["layout"] == rc.board_layout(reg, ev) and hy["mine"] == [] and hy["undo_box"] is None
    assert eb.cache_of(web_bot.bot).solves == 1, "the page view found the picture: no second solve"
    rc.set_signup(reg, rs, ev, ms[0], None, "out", source="test")
    monkeypatch.setattr(eb.BoardCache, "start", lambda self, *a, **k: None)
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["board_state"] == "working", "an answer changed: the last picture, marked as being placed again"
    assert ms[0].display_name in [s["display_name"] for r in part["sheet"]["board"]["rosters"] for g in r["groups"] for s in g]


def test_a_failed_raid_draws_the_layout_and_says_why(reg, rs, monkeypatch, web_bot):
    def fail(*a, **k):
        raise RuntimeError("INFEASIBLE")

    monkeypatch.setattr(rc, "_solve_run", fail)
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    settle(eb.cache_of(web_bot.bot), reg, rs, ev)
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["board_state"] == "failed" and part["board_error"].startswith("No roster satisfies the rules")
    assert part["placed_words"] == f"0 of {len(reg.test_members())} joined"


def test_a_locked_event_draws_its_roster_and_never_solves(reg, rs, monkeypatch, web_bot):
    from conftest import lock_with_board

    calls = []
    monkeypatch.setattr(rc, "_solve_run", lambda *a, **k: calls.append(1))
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    join(reg, rs, ev, reg.test_members())
    names = [s.display_name for s in ev.by_status("in")][:10]
    lock_with_board(reg, rs, ev, names)
    assert get(eb.cache_of(web_bot.bot), reg, rs, ev) == ("locked", None)
    part = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()["parts"][0]
    assert part["board_state"] == "locked" and calls == [] and part["mine"] == [] and part["undo_box"] is None
    assert sorted(n["display_name"] for r in part["sheet"]["board"]["rosters"] for g in r["groups"] for n in g) == sorted(names)


# ---------------------------------------------------------------- the cache

def test_the_cache_computes_once_per_fingerprint(reg, rs, monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_solve_run", in_order(calls))
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms[:6])
    cache = eb.BoardCache()

    async def two_at_once():
        a, b = await asyncio.gather(cache.get(reg, rs, ev), cache.get(reg, rs, ev))
        assert cache.solves == 1, "two pages at once: one solve"
        while cache.tasks:
            await asyncio.gather(*list(cache.tasks.values()))
        return a, b, await cache.get(reg, rs, ev)

    a, b, c = asyncio.run(two_at_once())
    assert a == ("working", None) and b == ("working", None)
    assert c[0] == "ready" and len(calls) == 1
    assert get(cache, reg, rs, ev)[0] == "ready" and cache.solves == 1, "the same fingerprint: no second solve"
    join(reg, rs, ev, ms[6:7])
    st, previews = get(cache, reg, rs, ev)
    assert st == "working" and previews == c[1], "a join: being placed again, the last picture meanwhile"


def test_a_ready_picture_wakes_the_page_and_a_stale_one_is_solved_again(reg, rs, monkeypatch):
    monkeypatch.setattr(rc, "_solve_run", in_order())
    heard = []
    monkeypatch.setattr(eb, "BOARD_LISTENERS", [lambda gk, key: heard.append((gk, key))])
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms[:6])
    cache = eb.BoardCache()
    _, (st, _pv) = settle(cache, reg, rs, ev)
    assert st == "ready" and heard == [(reg.key, ev.key)]
    import threading

    real, started, proceed = rc.event_board_preview, threading.Event(), threading.Event()

    def slow(*a, **k):  # the solve holds until the test has changed an answer under it
        started.set()
        proceed.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(rc, "event_board_preview", slow)
    join(reg, rs, ev, ms[7:8])

    async def go():
        assert (await cache.get(reg, rs, ev))[0] == "working"
        await asyncio.to_thread(started.wait, 5)
        rc.set_signup(reg, rs, ev, ms[6], None, "in", source="test")  # an answer changes while the board is being placed
        assert (await cache.get(reg, rs, ev))[0] == "working", "a page asks for the new answers while it solves"
        assert cache.solves == 2, "never two solves of one event at once"
        proceed.set()
        while cache.tasks:
            await asyncio.gather(*list(cache.tasks.values()))
        return await cache.get(reg, rs, ev)

    st, pv = asyncio.run(go())
    assert st == "ready" and cache.solves == 3, "the stale picture was solved again, once"
    assert ms[6].display_name in {p.signup_name for r in pv[ev.key] for p in r.selected}
    assert heard == [(reg.key, ev.key)] * 2, "only a current picture wakes the page"


def test_an_entry_goes_when_its_event_locks(reg, rs, monkeypatch):
    from conftest import lock_with_board

    monkeypatch.setattr(rc, "_solve_run", in_order())
    ev = one_off(reg, rs, lineup=[])
    join(reg, rs, ev, reg.test_members())
    cache = eb.BoardCache()
    settle(cache, reg, rs, ev)
    assert (reg.key, ev.key) in cache.entries
    lock_with_board(reg, rs, ev, [s.display_name for s in ev.by_status("in")][:20])
    other = one_off(reg, rs, lineup=[])
    get(cache, reg, rs, other)
    assert (reg.key, ev.key) not in cache.entries


# ---------------------------------------------------------------- the fingerprint covers every input of the solve

def _fp(reg, rs, ev):
    return eb.fingerprint(reg, rs, eb.snapshot(rs, ev))


def test_the_fingerprint_moves_with_every_input_of_the_solve(reg, rs):
    from conftest import weekly

    sid = weekly(reg, HYJAL, "Tue 18:30", sid="tue")
    reg.set_schedule(sid, {"lineup": [{"raid": HYJAL, "rosters": 1}, {"raid": BD, "rosters": 2}]}, "t")
    now = reg.now_local()
    t = next(t for s, t in rc.occurrences(reg, now, 24 * 21) if s["id"] == sid and t - now >= timedelta(hours=30))
    ev = rc.open_run(reg, rs, HYJAL, t, by="t", schedule=sid)
    ms = people(reg, 14)
    join(reg, rs, ev, ms[:12])
    bd = rc.event_parts(rs, ev)[1]
    seen = [_fp(reg, rs, ev)]
    assert _fp(reg, rs, ev) == seen[0], "nothing changed: the same fingerprint"

    def changed(what: str) -> None:
        fp = _fp(reg, rs, ev)
        assert fp not in seen, f"{what} must change the fingerprint"
        seen.append(fp)

    rc.set_signup(reg, rs, ev, ms[12], None, "in", source="test")
    changed("a join")
    rc.set_signup(reg, rs, ev, ms[12], None, "out", source="test")
    changed("a No thanks")
    s3 = ev.signups[str(ms[3].discord_id)]
    spec = next(x for x in reg.profile.classes[s3.cls] if x != s3.spec)
    s3.spec, s3.role = spec, reg.profile.spec(s3.cls, spec).role
    rs.save(ev, "a respec on the sheet")
    changed("an answer's spec")
    s0 = ev.signups[str(ms[0].discord_id)]
    s0.joined_at = (datetime.fromisoformat(max(s.order_at for s in ev.signups.values())) + timedelta(minutes=1)).isoformat()  # now joined last
    rs.save(ev, "who joined first")
    changed("the order people joined in")
    ev.pins[str(ms[2].discord_id)] = "out"
    rs.save(ev, "a pin")
    changed("a pin")
    ev.layout = [[ms[4].display_name]]
    rs.save(ev, "a board place")
    changed("the officers' board")
    bd = rs.events[bd.key]
    bd.layout = [[], [], [ms[5].display_name]]
    rs.save(bd, "a later raid's board place")
    changed("a later raid's board")
    reg.config.team(bd.team)["rosters"] = 1
    reg.save_config("a roster count", notify=False)
    changed("a raid's roster count")
    reg.set_minimums(BD, {"healer": {"min": 3}}, "t")
    changed("a raid's minimums")
    reg.set_schedule(sid, {"weights": {"rank": 9}}, "t")
    changed("the schedule's priority")
    reg.set_raid_overrides(HYJAL, {"split_policy": "first"}, "t")
    changed("the raid's split")
    reg.set_rank(ms[6].discord_id, "core", "t", by_id=OWNER)
    changed("a member's rank")
    day = ev.start.astimezone(reg.tz).date().isoformat()
    reg.add_absence(ms[7].discord_id, day, day, None, "t")
    asyncio.run(NightBot(reg, rs).after_absence(reg, ms[7], day, day, "t"))
    assert ev.signups[str(ms[7].discord_id)].status == "out"
    changed("an absence that turns an answer into away")


def test_the_later_raids_fingerprint_follows_the_earlier_raids_roster(reg, rs):
    from conftest import lock_with_board

    ev = one_off(reg, rs)
    ms = people(reg, 22)
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]
    lock_with_board(reg, rs, ev, names[:20])
    a = _fp(reg, rs, ev)
    lock_with_board(reg, rs, ev, names[2:22])
    assert _fp(reg, rs, ev) != a, "another roster on the first raid: the later raid prefers other people"


# ---------------------------------------------------------------- the later raid's board pins (a fix found on the way)

def test_a_later_raids_board_places_reach_the_solve_past_its_first_group(reg, rs, monkeypatch):
    """_solve_run used to reuse `k` (the groups per roster) for the part number on a later raid, cutting its board pins
    to `gi < part × rosters`: a place in group 2 of a later raid was dropped by the lock."""
    from oibot_gm.roster import solver

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


# ---------------------------------------------------------------- Review Focus 3: a drag on the board as it is NOW

def _shown(reg, rs, cache, ev, layout):
    """Put a picture in the cache for `ev`'s event (as a finished solve would)."""
    size = rc.run_size(reg, ev)
    first = rc.first_part(rs, ev)
    e = cache.entries.get((reg.key, first.key))
    previews = dict(e.previews) if e else {}
    previews[ev.key] = rc.board_rosters(reg, ev, layout, size)
    cache._keep((reg.key, first.key), "fp", previews)


def test_a_move_lands_on_the_board_as_it_is_now_when_the_preview_changed(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    cache = eb.cache_of(bot)
    ava = n[0]
    # the page drew Ava in group 2 …
    _shown(reg, rs, cache, ev, [n[1:6], [ava] + n[6:10], n[10:15], []])
    # … another officer's move re-placed everyone: Ava is in group 3 now and group 1 starts with someone else
    ev.layout = [[n[9]]]
    ev.placed_by = {n[9]: B_ID}
    rs.save(ev, "officer B")
    _shown(reg, rs, cache, ev, [[n[9], n[2], n[3], n[4], n[5]], n[6:9] + [n[1]], [ava] + n[10:14], [n[14]]])
    # the drop of Ava on group 1, position 0, sent against the OLD picture
    asyncio.run(bot.board_move(reg, rs, ev, ava, (0, 0), "Jon", by_id=OWNER, shown=True))
    assert ev.layout[0] == [ava] and ev.layout[2] == [n[9]], "Ava pinned to group 1; who group 1 held at 0 NOW takes her group 3"
    assert {x for g in ev.layout for x in g} == {ava, n[9]}, "nobody else enters the officers' layout"
    assert ev.placed_by == {ava: OWNER, n[9]: OWNER}, "the move and its swapped partner are the mover's now"
    assert not ev.pins


def test_moves_on_the_picture_to_not_placed_and_from_it(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    cache = eb.cache_of(bot)
    _shown(reg, rs, cache, ev, [n[0:5], n[5:10], n[10:15], []])
    asyncio.run(bot.board_move(reg, rs, ev, n[3], None, "Jon", by_id=OWNER, shown=True))
    assert ev.pins == {str(ms[3].discord_id): "out"} and not ev.layout and ev.placed_by == {n[3]: OWNER}, "Not placed = kept out of the lock"
    _shown(reg, rs, cache, ev, [n[0:3] + [n[4], n[15]], n[5:10], n[10:15], []])
    asyncio.run(bot.board_move(reg, rs, ev, n[3], (1, 0), "Jon", by_id=OWNER, shown=True))
    assert ev.pins == {str(ms[5].discord_id): "out"} and ev.layout[1] == [n[3]], "from Not placed onto someone: they go to Not placed, kept out"
    assert ev.placed_by == {n[3]: OWNER, n[5]: OWNER}
    with pytest.raises(ValueError, match="full"):
        asyncio.run(bot.board_move(reg, rs, ev, n[15], (2, 9), "Jon", by_id=OWNER, shown=True))


def test_without_shown_the_move_is_the_officers_layout_as_before(reg, rs):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    _shown(reg, rs, eb.cache_of(bot), ev, [n[0:5], n[5:10], n[10:15], []])
    asyncio.run(bot.board_move(reg, rs, ev, n[0], (1, 0), "Jon"))  # the Rosters page, MCP, plain text: no picture
    assert ev.layout[1] == [n[0]] and not ev.pins and ev.placed_by == {}


def test_the_move_route_carries_the_officer_and_the_picture(reg, rs, web_bot):
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    _shown(reg, rs, eb.cache_of(web_bot.bot), ev, [n[0:5], n[5:10], n[10:15], []])
    r = web_bot.c.post(f"/api/run/{ev.key}/move", json={"member": n[12], "to": {"r": 0, "g": 0, "i": 1}, "state": "open", "shown": True}, headers=H())
    assert r.status_code == 200, r.text
    assert ev.layout[0] == [n[12]] and ev.layout[2] == [n[1]] and ev.placed_by == {n[12]: OWNER, n[1]: OWNER}


# ---------------------------------------------------------------- Undo my moves: the server knows whose moves they are

def test_undo_drops_only_my_moves(reg, rs, web_bot, monkeypatch):
    monkeypatch.setattr(eb.BoardCache, "start", lambda self, *a, **k: None)  # the layout board: the moves below are on it
    ev = one_off(reg, rs, lineup=[])
    ms = reg.test_members()
    join(reg, rs, ev, ms)
    n = [m.display_name for m in ms]
    bot = web_bot.bot
    run = asyncio.run
    run(bot.board_move(reg, rs, ev, n[0], (0, 0), "Jon", by_id=OWNER, shown=True))
    run(bot.board_move(reg, rs, ev, n[1], None, "Jon", by_id=OWNER, shown=True))  # kept out
    run(bot.board_move(reg, rs, ev, n[2], (1, 0), "Bea", by_id=B_ID, shown=True))
    j = web_bot.c.get(f"/api/event/{ev.key}", headers=H()).json()
    part = j["parts"][0]
    assert part["mine"] == sorted([n[0], n[1]])
    assert part["undo_box"]["sentences"][0] == "Your 2 moves on this board are dropped." and part["undo_box"]["verb"] == "Undo moves"
    assert part["layout"][0] == [n[0]] and part["layout"][1] == [n[2]]
    box = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "preview": True}, headers=H()).json()["box"]
    assert box == part["undo_box"], "the route's box is the page's"
    run(bot.board_move(reg, rs, ev, n[0], (2, 0), "Bea", by_id=B_ID))  # B moves one of mine meanwhile
    r = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "seen": box["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["box"]["sentences"][0] == "Your one move on this board is dropped.", "the fresh box: my count now"
    r = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "seen": r.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    assert ev.placed_by == {n[0]: B_ID, n[2]: B_ID} and ev.pins == {}, "my kept-out pin goes, B's two placements stay"
    assert {x for g in ev.layout for x in g} == {n[0], n[2]}
    r = web_bot.c.post(f"/api/run/{ev.key}/layout", json={"undo": True, "preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["verb"] is None, "no moves of mine: a refusal, only Back"


def test_placed_by_is_pruned_with_the_board_and_cleared_at_the_lock(reg, rs, monkeypatch):
    monkeypatch.setattr(rc, "propose", lambda reg_, rs_, ev_, save=True: setattr(ev_, "rosters", rc.board_rosters(
        reg_, ev_, [[s.display_name for s in ev_.by_status("in")][:5]], rc.run_size(reg_, ev_))))
    ev = rc.open_run(reg, rs, BD, start_in(reg), by="t", schedule=rc.ADHOC_SCHEDULE)
    ms = reg.test_members()
    join(reg, rs, ev, ms[:8])
    n = [m.display_name for m in ms]
    bot = NightBot(reg, rs)
    asyncio.run(bot.board_move(reg, rs, ev, n[0], (0, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.board_move(reg, rs, ev, n[1], (1, 0), "Jon", by_id=OWNER))
    asyncio.run(bot.save_board(reg, rs, ev, [[n[1]]], by="Jon"))  # a whole-board write without n[0]
    assert ev.placed_by == {n[1]: OWNER}
    asyncio.run(bot.lock_run(reg, rs, ev, by="Jon"))
    assert ev.state == "locked" and ev.placed_by == {}
