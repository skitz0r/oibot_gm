"""Roster redesign Phase 0 ("Untangle", out/roster_redesign.md §7): the confirmed bugs, each reproduced first.

- a member's own Join after lock was stored as Bench (set_signup's curated-roster demotion keyed on the run key);
- after lock the fill engine's "pool" tier was empty (confirm_one wrote the seated people onto the run key, so
  roster_pool flipped to "the seated") and unanswered members were asked as "other_roster" ("you're free that night");
- seat fairness (sat out last time) matched people by display name, so a rename lost the credit;
- "Can't make it" on the sheet after lock freed the seat but wrote no Callout record;
- the health card measured role need against how_many_rosters while health_data / needs / fill used the rosters the
  run expects (and split the card's reason over size × rosters);
- a policy role_min naming only healers replaced the whole minimum dict, dropping the raid's tank minimum;
- the explain tank advisory read profile.raids[...]["tank_needs"] instead of the guild's role bounds;
- a post-lock fill ask for a body with no free seat was marked rostered (map bug 1), and a seated player the solver
  already moved to their offspec could be asked to switch to it again (map bug 3).
The Bot is the real RaidMixin over a recording fake (test_parity_verbs.Bot); Registry and RaidStore are real."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import by_role, join, lock_with_board, open_test_run
from test_analytics_cards import text_of
from test_parity_verbs import Bot

from oibot_gm import raidcycle as rc
from oibot_gm import raid_views as views

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def open_plain_run(reg, rs, days: float = 3, instance: str = "barrow_deeps") -> rc.RaidEvent:
    """A real (not test) run: its pool is every main, as on the live guild."""
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, instance, start, by="t")


def _locked_with_confirmations(reg, rs, ev, seated):
    """lock_with_board + what lock_run does next: the confirmation DMs (confirm_one) for every seat."""
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    bot = Bot(reg, rs)
    run(bot.send_confirmations(reg, rs, ev, rc.run_team(reg, ev), "t"))
    return bot


def _ten(reg):
    roles = by_role(reg)
    return roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5]


# ---------------------------------------------------------------- the saved repro (test_repro_sub.py)

def test_own_join_after_lock_stays_join_and_seats(reg, rs):
    """The repro: someone not rostered sets their OWN answer to Join after lock (the board's Join for themselves, MCP
    as the owner). It used to be stored as Bench ("not on this roster") and then seated anyway."""
    ev = open_plain_run(reg, rs)
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    bot = _locked_with_confirmations(reg, rs, ev, seated)
    late = next(m for m in reg.test_members() if m not in seated)
    line = run(bot.set_answer(reg, rs, ev, late, "in", None, by=late.display_name))
    sg = ev.signups[str(late.discord_id)]
    assert sg.status == "in", f"own Join after lock became {sg.status} ({sg.note})"
    assert "not on this roster" not in (sg.note or "")
    assert "Join" in line or "rostered" in line


def test_own_join_on_an_open_sheet_is_never_demoted(reg, rs):
    """No curated-roster demotion before lock either, whatever the character's leftover memberships say."""
    ev = open_plain_run(reg, rs)
    a, b = reg.test_members()[:2]
    a.main.rosters = [ev.team]  # a leftover run-key membership (what confirm_one used to write)
    reg.save(a, "leftover")
    s = rc.set_signup(reg, rs, ev, b, None, "in", source="member")
    assert s.status == "in" and s.note is None


def test_fill_pool_after_lock_holds_the_unanswered(reg, rs):
    """After lock (with confirmations sent, as lock_run does) the unanswered mains are the 'pool' tier — not
    'other_roster' — and health still lists them as not answered. The bench's members stand in for a live guild here
    (made real members: a real run never takes test puppets)."""
    seated = _ten(reg)
    for m in reg.test_members():
        m.test = False
        reg.save(m, "a real member for this test")
    ev = open_plain_run(reg, rs)
    join(reg, rs, ev, seated)
    _locked_with_confirmations(reg, rs, ev, seated)
    silent = [m for m in reg.members.values() if m.main and str(m.discord_id) not in ev.signups]
    assert silent
    assert all(not c.rosters for m in seated for c in m.characters), "lock writes no run-key membership"
    pool = {m.discord_id for m, _ in rc.run_pool(reg, ev)}
    assert {m.discord_id for m in silent} <= pool, "the pool after lock is still everyone, not the seated"
    dps = next(p for p in ev.roster.selected if p.role in ("melee", "ranged"))
    rc.free_seat(reg, rs, ev, dps.signup_name, "declined")
    cands = rc.fill_candidates(reg, rs, ev, rc.run_team(reg, ev))
    assert cands and cands[0].kind == "regular", [(a.display_name, a.kind) for a in cands]
    assert not [a for a in cands if a.kind == "other"]
    h = rc.health_data(reg, ev, rc.run_team(reg, ev))
    assert set(h["unresponsive"]) >= {m.display_name for m in silent}


def test_seated_membership_is_not_written_or_removed_on_the_member(reg, rs):
    """Confirm, decline, expiry, fill yes and cleanup leave RegisteredCharacter.rosters alone (it is the regular-series
    list from Phase 1); who holds a seat is read from the run file."""
    ev = open_plain_run(reg, rs)
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    bot = _locked_with_confirmations(reg, rs, ev, seated)
    m = seated[-1]
    assert [e.key for e, _ in rc.rostered_runs(reg, rs, m)] == [ev.key]
    run(bot.answer_placement_for(reg, m.discord_id, ev.team, False, m.display_name))
    assert not ev.seat_of(m.display_name) and rc.rostered_runs(reg, rs, m) == []
    assert all(not c.rosters for x in reg.members.values() for c in x.characters)
    ev.confirm_by = (rc.datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()
    assert rc.expire_confirmations(reg, rs, ev)
    ev.state = "done"
    rs.save(ev, "done")
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(ev.team) is None
    assert all(not c.rosters for x in reg.members.values() for c in x.characters)


# ---------------------------------------------------------------- seat fairness survives a rename

def test_sat_out_credit_survives_a_rename(reg, rs):
    first = open_test_run(reg, rs, days=1)
    seated = _ten(reg)
    sat = next(m for m in reg.test_members() if m not in seated)
    join(reg, rs, first, seated + [sat])
    lock_with_board(reg, rs, first, [m.display_name for m in seated])
    first.state = "done"
    rs.save(first, "done")
    reg.member(sat.discord_id, "Renamedperson")  # Discord rename: the member file follows the new name
    reg.save(sat, "rename")
    second = open_test_run(reg, rs, days=2)
    join(reg, rs, second, seated + [sat])
    players = rc.players_for(reg, second)
    p = next(p for p in players if p.signup_name == "Renamedperson")
    assert p.discord_id == sat.discord_id
    reg.set_raid_overrides("barrow_deeps", {"weight_rank": 0, "weight_main": 0, "weight_signup_order": 0, "weight_sat_out": 5}, "t")  # only sat_out counts
    bonus = rc.seat_bonus(reg, rs, second, players)
    assert bonus["Renamedperson"] == 5, bonus
    assert bonus[seated[-1].display_name] == 0, "seated last time: no sat-out credit"


def test_seat_player_carries_the_discord_id(reg, rs):
    ev = open_test_run(reg, rs)
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated[:-1]])
    sg = ev.signups[str(seated[-1].discord_id)]
    rc.seat_player(reg, ev, sg)
    assert ev.seat_of(sg.display_name)[1].discord_id == seated[-1].discord_id
    assert all(p.discord_id for p in ev.seated())


# ---------------------------------------------------------------- Can't make it after lock is a callout

def test_cant_make_it_after_lock_records_a_callout(reg, rs):
    ev = open_test_run(reg, rs, soft=90, hard=80)  # the lock cutoff is behind us: a callout now is late
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    bot = _locked_with_confirmations(reg, rs, ev, seated)
    m = seated[-1]
    run(bot.set_answer(reg, rs, ev, m, "out", None, by=m.display_name))  # the sheet's Can't make it
    assert not ev.seat_of(m.display_name)
    co = [c for c in ev.callouts if c.discord_id == m.discord_id]
    assert len(co) == 1 and co[0].late, "after lock the callout is late"
    assert ev.signups[str(m.discord_id)].status == "out"


def test_officer_setting_someone_out_after_lock_is_not_their_callout(reg, rs):
    ev = open_test_run(reg, rs)
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    bot = _locked_with_confirmations(reg, rs, ev, seated)
    m = seated[-1]
    run(bot.set_answer(reg, rs, ev, m, "out", None, by="Officer"))
    assert not ev.seat_of(m.display_name) and not ev.callouts


# ---------------------------------------------------------------- one role-need calculation

def _two_roster_run(reg, rs):
    """A run whose schedule expects 2 rosters, with enough joiners for 2 runs but healers for one."""
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    reg.config.roster(ev.team)["rosters"] = 2
    roles = by_role(reg)
    joiners = roles["tank"][:4] + roles["healer"][:2] + (roles["melee"] + roles["ranged"])[:14]
    join(reg, rs, ev, joiners)
    return ev


def test_gaps_is_the_one_role_need(reg, rs):
    ev = _two_roster_run(reg, rs)
    team = rc.run_team(reg, ev)
    g = rc.gaps(reg, ev)
    hmin = reg.role_bounds("barrow_deeps", 10)["healer"]["min"]
    assert g["rosters"] == 2 and g["need"]["healer"] == 2 * hmin and g["have"]["healer"] == 2
    nd = rc.needs(reg, ev, team)
    h = rc.health_data(reg, ev, team)
    assert nd["roles"] == g["roles"] and nd["headcount"] == g["headcount"]
    assert {r["role"]: r["need"] for r in h["roles"]} == g["need"]


def test_health_card_counts_need_like_the_fill(reg, rs):
    """The card's role have/need and its Short line are rc.gaps (what needs() and the fill read), and its split
    reason is the run's own (per-run size), not one computed over size × rosters."""
    ev = _two_roster_run(reg, rs)
    team = rc.run_team(reg, ev)
    g = rc.gaps(reg, ev)
    body = text_of(views.health_layout(reg, rs, ev, team, ico))
    assert f"<role:healer> **{g['have']['healer']}**/{g['need']['healer']}" in body, body
    assert f"**Short** " in body and f"<role:healer> {g['roles']['healer']}" in body
    reason = rc.run_split_reason(reg, ev)
    if reason:
        assert rc.split_reason_text(reason, lambda role, k: f"<role:{role}> {k}") in body
    else:
        assert "Enough people" not in body


def test_health_card_single_roster_with_bodies_for_two(reg, rs):
    """20 joiners for a 10-seat run: the card used to scale need by the 2 runs the bodies allow while needs() used 1."""
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    roles = by_role(reg)
    joiners = roles["tank"][:4] + roles["healer"][:4] + (roles["melee"] + roles["ranged"])[:12]
    join(reg, rs, ev, joiners)
    team = rc.run_team(reg, ev)
    nd = rc.needs(reg, ev, team)
    body = text_of(views.health_layout(reg, rs, ev, team, ico))
    assert nd["roles"] == {} and "**Short**" not in body, body
    tmin = reg.role_bounds("barrow_deeps", 10)["tank"]["min"]
    assert f"<role:tank> 4/{tmin}" in body


# ---------------------------------------------------------------- policy role_min merges per role

def _capture_solve(monkeypatch):
    seen = {}
    real = rc.solver.solve_rosters

    def spy(profile, players, raid_id, opts, **kw):
        seen.setdefault("opts", opts)  # the lock's own solve (explain's bench what-ifs solve again afterwards)
        return real(profile, players, raid_id, opts, **kw)

    monkeypatch.setattr(rc.solver, "solve_rosters", spy)
    return seen


def _write_comp_policy(rs, constraints):
    from oibot_gm.policy import PolicyStore

    ps = PolicyStore(rs.store, rs.key)
    rs.store.write_text(ps.compiled_path("comp"), json.dumps({"constraints": constraints}))
    rs.store.commit("comp policy (test)")


@pytest.mark.slow
def test_healer_policy_keeps_the_tank_minimum(reg, rs, monkeypatch):
    _write_comp_policy(rs, [{"type": "role_min", "role": "healer", "count": 3, "hard": True}])
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members())
    seen = _capture_solve(monkeypatch)
    rc.propose(reg, rs, ev, save=False)
    rb = reg.role_bounds("barrow_deeps", rc.run_size(reg, ev))
    assert seen["opts"].role_min["healer"] == 3
    assert seen["opts"].role_min.get("tank") == rb["tank"]["min"], seen["opts"].role_min


def test_role_min_merge_is_per_role(reg):
    rb = reg.role_bounds("barrow_deeps", 10)
    assert rc.merged_role_min(rb, {"healer": 3}) == {"tank": rb["tank"]["min"], "healer": 3}
    assert rc.merged_role_min(rb, {}) == {r: rb[r]["min"] for r in ("tank", "healer") if rb[r]["min"]}
    assert rc.merged_role_min(rb, {"tank": 0})["tank"] == 0, "an explicit policy number still wins for its role"


# ---------------------------------------------------------------- explain's tank advisory reads the guild's bounds

def test_tank_advisory_reads_the_guild_minimum(reg):
    from oibot_gm.models import Player, RosterResult
    from oibot_gm.roster import explain

    players = [Player(signup_name="A", pos=1, status="signed", cls="Warrior", spec="Protection", role="tank")]
    res = RosterResult(selected=players, benched=[], groups=[["A"]], group_reports=[], objective=0, synergy_value=0, role_counts={"tank": 1}, advisories=[])
    out = explain.annotate(reg.profile, players, "barrow_deeps", res, whatif=False, tank_min=3)
    assert any("Tanks 1/3" in a for a in out.advisories), out.advisories
    res2 = RosterResult(selected=players, benched=[], groups=[["A"]], group_reports=[], objective=0, synergy_value=0, role_counts={"tank": 1}, advisories=[])
    out2 = explain.annotate(reg.profile, players, "barrow_deeps", res2, whatif=False, tank_min=1)
    assert not any(a.startswith(("🔴 Tanks", "🟡 Tanks")) for a in out2.advisories), out2.advisories


# ---------------------------------------------------------------- fill after lock: no seat, no body; no switch to the spec they already play

def test_role_gap_with_no_free_seat_asks_no_new_body(reg, rs):
    """A locked roster that is full but short a healer (the solver capped the minimum): a new healer can't be seated,
    so the fill only proposes swaps by seated players — never a body ask that would end 'in' but unseated."""
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    roles = by_role(reg)
    hmin = reg.role_bounds("barrow_deeps", 10)["healer"]["min"]
    seated = roles["tank"][:2] + roles["healer"][:hmin - 1] + (roles["melee"] + roles["ranged"])[:10 - 2 - (hmin - 1)]
    join(reg, rs, ev, seated)
    join(reg, rs, ev, roles["healer"][hmin:hmin + 1], "sub")  # a healer on Bench who can't be seated
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    team = rc.run_team(reg, ev)
    nd = rc.needs(reg, ev, team)
    assert nd["headcount"] == 0 and nd["roles"] == {"healer": 1}
    cands = rc.fill_candidates(reg, rs, ev, team)
    assert all(a.swap or a.pair for a in cands), [(a.display_name, a.kind) for a in cands]


def test_a_yes_that_finds_no_seat_is_not_marked_rostered(reg, rs):
    ev = open_test_run(reg, rs)
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    bot = _locked_with_confirmations(reg, rs, ev, seated)
    extra = next(m for m in reg.test_members() if m not in seated)
    ask = rc.FillAsk(discord_id=extra.discord_id, display_name=extra.display_name, kind="sub", role="melee", character=extra.main.label, spec=extra.main.spec, reason="short 1")
    ev.fill_asks.append(ask)
    line = rc.apply_fill_answer(reg, rs, ev, ask, True)
    assert not ev.seat_of(extra.display_name)
    assert "no free seat" in line
    run(bot.after_fill_answer(reg, rs, ev, ask, line))
    assert not [a for a in reg.members[extra.discord_id].placement_asks if a["roster"] == ev.team], "no 'rostered via fill' record without a seat"


def test_fill_never_asks_a_seated_player_to_switch_to_the_spec_they_play(reg, rs):
    """The solver switched a seated player to their tank offspec (Player.role tank, Signup still dps): when a tank seat
    opens, they are not asked to 'swap to' the offspec they already play."""
    reg.seed_test_members(24, "t")  # spare dps who could backfill a swap
    ev = open_test_run(reg, rs)
    seated = _ten(reg)
    join(reg, rs, ev, seated)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    dps = next(p for p in ev.roster.selected if p.role in ("melee", "ranged") and p.cls == "Warrior") if any(p.cls == "Warrior" and p.role in ("melee", "ranged") for p in ev.roster.selected) else None
    if dps is None:
        pytest.skip("no seated dps Warrior in the bench mix")
    sg = rc.signup_by_name(ev, dps.signup_name)
    sg.offspec = "Protection"
    dps.offspec, dps.spec, dps.role = sg.spec, "Protection", "tank"  # what the solver's _extract does
    for tank in [p for p in ev.roster.selected if p.role == "tank" and p.signup_name != dps.signup_name]:
        rc.free_seat(reg, rs, ev, tank.signup_name, "declined")  # both real tanks out: the switched one is the only tank
    assert rc.needs(reg, ev, rc.run_team(reg, ev))["roles"].get("tank")
    for m in by_role(reg)["tank"]:
        if str(m.discord_id) not in ev.signups:
            m.dm_opt_out = True  # no spare tank to ask instead: only a switch could cover it
    cands = rc.fill_candidates(reg, rs, ev, rc.run_team(reg, ev))
    assert not [a for a in cands if a.discord_id == sg.discord_id and a.role == "tank"], [(a.display_name, a.kind, a.role) for a in cands]
