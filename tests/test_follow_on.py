"""Follow-on runs (design §5.62): a second raid the same night from one signup. Config, the pair of runs, the lead's
answers on the follow-on, the lock, leaving early, cancel, the words, plain text and the routes."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import join

from oibot_gm import raidcycle as rc
from oibot_gm import registry as rg
from oibot_gm.registry import RegistryError

LEAD, FOLLOW = "hyjal_summit_forever", "barrow_deeps"


def unlock_hyjal(reg) -> None:
    reg.set_raid_overrides(LEAD, {"first_open": "2026-01-01T00:00"}, "t")


# ---------------------------------------------------------------- Task 1: config

def test_parse_follow_grammar():
    assert rg.parse_follow("barrow_deeps x2") == {"raid": "barrow_deeps", "rosters": 2}
    assert rg.parse_follow("Barrow_Deeps ×2") == {"raid": "barrow_deeps", "rosters": 2}
    assert rg.parse_follow("barrow_deeps") == {"raid": "barrow_deeps", "rosters": 1}
    assert rg.parse_follow({"raid": "barrow_deeps", "rosters": "3"}) == {"raid": "barrow_deeps", "rosters": 3}
    assert rg.parse_follow(None) is None and rg.parse_follow("") is None and rg.parse_follow("none") is None
    with pytest.raises(RegistryError, match="rosters"):
        rg.parse_follow("barrow_deeps x0")
    with pytest.raises(RegistryError, match="rosters"):
        rg.parse_follow("barrow_deeps x5")
    with pytest.raises(RegistryError, match="whole number"):
        rg.parse_follow("barrow_deeps xtwo")


def test_schedule_follow_is_validated_and_read_back(reg):
    unlock_hyjal(reg)
    reg.set_schedule(LEAD, "tue", {"slots": "Tue 18:30", "name": "Tuesday", "follow": "barrow_deeps x2"}, "t")
    s = next(x for x in reg.schedules(LEAD) if x["id"] == "tue")
    assert s["follow"] == {"raid": "barrow_deeps", "rosters": 2}
    assert reg.schedule_label(LEAD, s).endswith("· then Barrow Deeps ×2")
    assert reg.follow_words(s["follow"]) == "Barrow Deeps ×2" and reg.follow_words(None) == ""
    reg.set_schedule(FOLLOW, "wed", {"slots": "Wed 18:30"}, "t")  # Barrow Deeps has no run times of its own in the profile
    assert next(x for x in reg.schedules(FOLLOW) if x["id"] == "wed")["follow"] is None  # every schedule carries the key
    with pytest.raises(RegistryError, match="unknown raid"):
        reg.set_schedule(LEAD, "tue", {"follow": "nope x2"}, "t")
    with pytest.raises(RegistryError, match="itself"):
        reg.set_schedule(LEAD, "tue", {"follow": "hyjal_summit_forever x2"}, "t")
    reg.set_schedule(LEAD, "tue", {"follow": "none"}, "t")
    assert next(x for x in reg.schedules(LEAD) if x["id"] == "tue")["follow"] is None
    with pytest.raises(RegistryError, match="kept for follow-on"):
        reg.set_schedule(LEAD, "after", {"slots": "Tue 18:30"}, "t")


# ---------------------------------------------------------------- Task 2: the pair of runs

def open_pair(reg, rs, days: float = 3):
    unlock_hyjal(reg)
    start = (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": FOLLOW, "rosters": 2})
    return lead, rc.follow_of(rs, lead)


def test_open_pair_opens_two_runs_with_one_signup(reg, rs):
    lead, fev = open_pair(reg, rs)
    assert fev is not None and fev.follows == lead.key and lead.followed_by == fev.key
    assert fev.instance == FOLLOW and "-after-" in fev.key  # 'bd-MMDD-HHMM-after-YYYY-MM-DD'
    assert fev.start == lead.start + timedelta(hours=3)  # Hyjal lasts 3 h
    t, ft = reg.config.roster(lead.team), reg.config.roster(fev.team)
    assert t["follow"] == {"raid": FOLLOW, "rosters": 2} and t["followed_by"] == ft["key"] and ft["follows"] == t["key"]
    assert ft["rosters"] == 2 and ft["size"] == 10 and ft["confirm"] is False and ft["kind"] == t["kind"] and ft["series"] is None
    assert ft["cutoff_hard_hours"] == t["cutoff_hard_hours"] + 3  # the same instant as the lead's lock
    assert fev.message_id is None and fev.signups == {}  # no sheet, no answers of its own yet
    assert rc.run_rosters(reg, fev) == 2 and rc.run_confirms(reg, fev) is False


def test_open_pair_is_idempotent(reg, rs):
    lead, fev = open_pair(reg, rs)
    again = rc.open_run(reg, rs, LEAD, lead.start, by="t", schedule=rc.ADHOC_SCHEDULE, follow={"raid": FOLLOW, "rosters": 2})
    assert again is lead and rc.follow_of(rs, again) is fev
    assert sum(1 for t in reg.config.rosters if t.get("follows") or t.get("followed_by")) == 2


def test_a_schedule_with_a_follow_on_opens_the_pair_on_its_own(reg, rs):
    unlock_hyjal(reg)
    reg.set_schedule(LEAD, "tue", {"slots": "Tue 18:30", "follow": "barrow_deeps x2"}, "t")
    now = reg.now_local()
    t = next(t for s, t in rc.upcoming(reg, LEAD, now, 24 * 21) if s["id"] == "tue" and t - now >= timedelta(hours=30))
    lead = rc.open_run(reg, rs, LEAD, t, by="scheduler", schedule="tue")  # no explicit follow: the schedule's
    fev = rc.follow_of(rs, lead)
    assert fev is not None and fev.instance == FOLLOW and rc.run_rosters(reg, fev) == 2


def test_answers_materialise_on_the_follow_on(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms[:3])
    join(reg, rs, lead, ms[3:4], "sub")
    join(reg, rs, lead, ms[4:5], "out")
    fev = rs.events[fev.key]
    assert {k: v.status for k, v in fev.signups.items()} == {str(m.discord_id): st for m, st in zip(ms[:5], ["in"] * 3 + ["sub", "out"])}
    assert fev.signups[str(ms[0].discord_id)].character == lead.signups[str(ms[0].discord_id)].character
    rc.set_signup(reg, rs, lead, ms[0], None, "out", source="test")
    assert rs.events[fev.key].signups[str(ms[0].discord_id)].status == "out"
    with pytest.raises(RegistryError, match="Hyjal Summit sheet"):
        rc.set_signup(reg, rs, fev, ms[1], None, "sub", source="test")


def test_sync_runs_once_and_never_back(reg, rs, monkeypatch):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    saves = []
    orig = rc.RaidStore.save

    def spy(self, ev, message):
        saves.append((ev.key, message))
        return orig(self, ev, message)

    monkeypatch.setattr(rc.RaidStore, "save", spy)
    join(reg, rs, lead, reg.test_members()[:1])
    assert [k for k, _m in saves] == [lead.key, fev.key]  # the lead, then its follow-on once — nothing back
    saves.clear()
    join(reg, rs, lead, reg.test_members()[:1])  # the same answer again: the follow-on is unchanged, no save
    assert [k for k, _m in saves] == [lead.key]


def test_conflicts_exempt_the_pair_and_absences_skip_the_follow_on(reg, rs):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms[:2])
    assert rc.conflicts(rs, fev) == {} and rc.conflicts(rs, lead) == {}
    day = lead.start.astimezone(reg.tz).date().isoformat()
    assert [ev.key for ev in rc.runs_in_span(reg, rs, day, day)] == [lead.key]  # an absence touches the lead; the follow-on follows it


def test_the_pair_finds_each_others_dicts(reg, rs):
    lead, fev = open_pair(reg, rs)
    assert rc.is_follow(fev) and not rc.is_follow(lead)
    assert rc.lead_of(rs, fev) is lead and rc.lead_of(rs, lead) is None and rc.follow_of(rs, fev) is None
    assert rc.lead_dict(reg, fev)["key"] == lead.team and rc.follow_dict(reg, lead)["key"] == fev.team
    assert rc.lead_dict(reg, lead) is None and rc.follow_dict(reg, fev) is None


def test_a_second_lead_on_the_same_follow_on_is_refused(reg, rs):
    lead, fev = open_pair(reg, rs)
    n_dicts, n_events = len(reg.config.rosters), len(rs.events)
    with pytest.raises(RegistryError, match=r"^That night already has a Barrow Deeps follow-on after .+\. Pick another time\.$"):
        rc.open_run(reg, rs, LEAD, lead.start, by="t", follow={"raid": FOLLOW, "rosters": 1})  # the raid's own times, same minute
    assert len(reg.config.rosters) == n_dicts and len(rs.events) == n_events
    assert reg.config.roster(fev.team)["follows"] == lead.team


def test_a_cancelled_follow_on_refuses_the_night_and_opens_no_lead(reg, rs):
    lead, fev = open_pair(reg, rs)
    fev.state = "cancelled"
    rs.save(fev, "cancelled")
    n_dicts, n_events = len(reg.config.rosters), len(rs.events)
    with pytest.raises(RegistryError, match="cancelled"):
        rc.open_run(reg, rs, LEAD, lead.start, by="t", follow={"raid": FOLLOW, "rosters": 1})
    assert len(reg.config.rosters) == n_dicts and len(rs.events) == n_events


# ---------------------------------------------------------------- Task 3: the lock builds both

from test_raid_night import NightBot, fast_propose, run  # noqa: E402,F401


def seat_by_name(names_by_key: dict[str, list[str]]):
    """rc.propose without the solver, per run key prefix: the lead's names and the follow-on's names."""
    def propose(reg, rs, ev, save=True):
        names = names_by_key[ev.instance]
        size = rc.run_size(reg, ev)
        g = int(reg.profile.comp_rules["group_size"])
        layout = [names[i:i + g] for i in range(0, len(names), g)]
        ev.rosters = rc.board_rosters(reg, ev, layout, size)
        return [], ev.rosters[0]
    return propose


def test_the_leads_lock_locks_the_follow_on_too(reg, rs, monkeypatch):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    names = [m.display_name for m in ms]
    monkeypatch.setattr(rc, "propose", seat_by_name({LEAD: names[:20], FOLLOW: names[:20]}))
    line = run(bot.lock_run(reg, rs, lead, by="Jon"))
    fev = rs.events[fev.key]
    assert lead.state == "locked" and fev.state == "locked" and fev.key in line
    assert len(fev.all_rosters) == 2 and sorted(p.signup_name for r in fev.all_rosters for p in r.selected) == sorted(names[:20])
    assert fev.confirm_by is None and not bot.dms  # a follow-on never confirms (its lead is ad hoc here: no DMs either)
    assert "lock:0" in fev.cards or bot.officer_channel(reg, fev) is None  # cards post when there is a channel


def test_a_follow_on_cannot_lock_before_its_lead(reg, rs):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    with pytest.raises(RegistryError, match="its lock builds the follow-on too"):
        run(bot.lock_run(reg, rs, fev, by="Jon"))
    assert fev.state == "open" and lead.state == "open"


def test_the_scheduler_skips_a_follow_on_while_its_lead_is_open(reg, rs, monkeypatch):
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs, days=0.5)  # inside the ad hoc lock window would be days < 2/24; keep both open
    locked = []

    async def lock_run(reg, rs, ev, by="scheduler"):
        locked.append(ev.key)
        return "ok"

    monkeypatch.setattr(bot, "lock_run", lock_run)
    monkeypatch.setattr(bot, "post_health", lambda *a, **k: None)
    run(bot.scheduler_tick_guild(reg))
    assert fev.key not in locked and not fev.health_posted


def test_the_follow_ons_solve_prefers_the_leads_rostered_and_skips_first_come(reg, rs, monkeypatch):
    """What the follow-on hands the solver: every lead seat carries rc.LEAD_SEAT_BONUS, nobody else does, and no late
    joiner is benched by first come (the lead's roster comes first instead)."""
    from conftest import lock_with_board
    from oibot_gm.roster import solver

    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    lock_with_board(reg, rs, lead, [m.display_name for m in ms[4:]])
    seen = {}

    class Seen(Exception):
        pass

    def capture(profile, players, raid_id, opts, **kw):
        seen["opts"] = opts
        raise Seen

    monkeypatch.setattr(solver, "solve_rosters", capture)
    with pytest.raises(Seen):
        rc.propose(reg, rs, rs.events[fev.key], save=False)
    bonus, out = seen["opts"].bonus, set(seen["opts"].force_out)
    assert all(bonus.get(m.display_name, 0) >= rc.LEAD_SEAT_BONUS for m in ms[4:])
    assert all(bonus.get(m.display_name, 0) < rc.LEAD_SEAT_BONUS for m in ms[:4])
    assert not out, "a follow-on benches nobody by first come"


# ---------------------------------------------------------------- Task 4: leaving early, cancel, the sentences

from oibot_gm import consequences as cq  # noqa: E402


def locked_pair(reg, rs, monkeypatch, bot):
    reg.seed_test_members(24, "t")
    lead, fev = open_pair(reg, rs)
    ms = reg.test_members()
    join(reg, rs, lead, ms)
    names = [m.display_name for m in ms]
    monkeypatch.setattr(rc, "propose", seat_by_name({LEAD: names[:20], FOLLOW: names[:20]}))
    run(bot.lock_run(reg, rs, lead, by="Jon"))
    return lead, rs.events[fev.key], ms


def test_leaving_early_frees_only_the_follow_on_seat(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m = ms[0]
    line = run(bot.call_out(reg, rs, fev, m, "early night", by=m.display_name, by_id=m.discord_id))
    fev = rs.events[fev.key]
    assert m.discord_id in fev.leaving and fev.signups[str(m.discord_id)].status == "out" and fev.signups[str(m.discord_id)].note == rc.LEAVING_NOTE
    assert fev.seat_of(m) is None and lead.seat_of(m) is not None and lead.signups[str(m.discord_id)].status == "in"
    assert "seat freed" in line and fev.callouts[-1].display_name == m.display_name


def test_leave_then_stay_keeps_the_lead_answer(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m, gone = ms[0], ms[4]
    rc.set_signup(reg, rs, lead, gone, None, "out", source="test")  # No thanks on the lead: out on both, for good
    run(bot.call_out(reg, rs, fev, m, None, by=m.display_name, by_id=m.discord_id))
    assert run(bot.stay_for_follow(reg, rs, rs.events[fev.key], m, by=m.display_name, by_id=m.discord_id)).startswith("Noted")
    fev = rs.events[fev.key]
    assert m.discord_id not in fev.leaving and fev.signups[str(m.discord_id)].status == "in" and fev.seat_of(m) is None  # back on the sheet, not re-seated
    assert fev.signups[str(gone.discord_id)].status == "out"
    assert rc.stay_for_follow(reg, rs, fev, gone) is False  # nothing to undo: they left the night, not the follow-on


def test_a_cant_on_the_lead_frees_both_seats(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m = ms[1]
    run(bot.call_out(reg, rs, lead, m, None, by=m.display_name, by_id=m.discord_id))
    fev = rs.events[fev.key]
    assert lead.seat_of(m) is None and fev.seat_of(m) is None and fev.signups[str(m.discord_id)].status == "out"
    m2 = ms[2]
    run(bot.set_answer(reg, rs, lead, m2, "out", None, by=m2.display_name, by_id=m2.discord_id))
    assert rs.events[fev.key].seat_of(m2) is None


def test_cancel_lead_cancels_follow_on(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    run(bot.cancel_run(reg, rs, lead, "Jon", "storm"))
    fev = rs.events[fev.key]
    assert lead.state == "cancelled" and fev.state == "cancelled"
    audits = [t for _lvl, t in bot.ops.lines if "cancelled" in t]
    assert len(audits) == 1 and fev.key in audits[0] and lead.key in audits[0]


def test_cancel_follow_on_alone_leaves_the_lead_standing(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    run(bot.cancel_run(reg, rs, fev, "Jon", None))
    assert fev.state == "cancelled" and lead.state == "locked" and lead.followed_by is None
    assert reg.config.roster(lead.team).get("followed_by") is None
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(lead.team) is not None and reg.config.roster(fev.team) is None


def test_cleanup_drops_the_pair_together(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    run(bot.cancel_run(reg, rs, lead, "Jon", None))
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(lead.team) is None and reg.config.roster(fev.team) is None


def test_cleanup_drops_an_orphaned_follow_on(reg, rs):
    """A follow-on dict whose lead dict is gone would block its night for good (ensure_run refuses a new lead on it)."""
    reg.seed_test_members(24, "t")
    bot = NightBot(reg, rs)
    lead, fev = open_pair(reg, rs)
    reg.config.rosters = [t for t in reg.config.rosters if t["key"] != lead.team]
    reg.save_config("harness: the lead's dict is gone", notify=False)
    run(bot.cleanup_ephemeral(reg, rs))
    assert reg.config.roster(fev.team) is None


def test_the_sentences(reg, rs, monkeypatch):
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    assert "Barrow Deeps follow-on is cancelled with it" in " ".join(cq.consequence("cancel", reg, lead))
    follow_cancel = cq.consequence("cancel", reg, fev)
    assert follow_cancel[0] == "The Barrow Deeps follow-on is called off for the 20 people rostered on it."
    assert follow_cancel[1] == "The Hyjal Summit run goes ahead, and nobody gets a DM."
    refill = rc.refill_sentence(reg, fev)
    assert cq.leave_follow_consequence(reg, fev, True, True) == ["You stay on the Hyjal Summit roster.", "Your Barrow Deeps place is freed.", refill]
    assert cq.leave_follow_consequence(reg, fev, True, False) == ["Your Hyjal Summit answer stands.", "Your Barrow Deeps place is freed.", refill]
    assert cq.leave_follow_consequence(reg, fev, False, False) == ["Your Hyjal Summit answer stands.", "You are out of the Barrow Deeps follow-on."]
    rc.run_team(reg, fev)["autofill"] = True
    assert cq.leave_follow_consequence(reg, fev, True, True)[2] == "The bench is asked to fill it."  # never "an officer may" when the bot asks
    fx = rc.cancel_effects(reg, lead)
    assert fx["follow"] == "Barrow Deeps" and rc.cancel_effects(reg, fev)["lead"] == "Hyjal Summit"


def test_a_confirm_dm_cant_on_the_lead_frees_the_follow_on_seat(reg, rs, monkeypatch):
    """Every path that frees a lead seat frees the follow-on's: here the Confirm / Can't card's Can't make it."""
    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m = ms[3]
    assert lead.seat_of(m) and fev.seat_of(m)
    run(bot.after_placement_answer(reg, m.discord_id, lead.team, False, f"{m.display_name} can't make it"))
    fev = rs.events[fev.key]
    assert lead.seat_of(m) is None and fev.seat_of(m) is None and fev.signups[str(m.discord_id)].status == "out"


def test_an_expired_confirmation_on_the_lead_frees_the_follow_on_seat(reg, rs, monkeypatch):
    from test_raid_night import past_confirm

    bot = NightBot(reg, rs)
    lead, fev, ms = locked_pair(reg, rs, monkeypatch, bot)
    m = ms[5]
    reg.add_placement_ask(m.discord_id, lead.team, m.main.label, "lock")
    past_confirm(lead)
    rs.save(lead, "deadline")
    assert [u for u, _n in rc.expire_confirmation_asks(reg, rs, lead)] == [m.discord_id]
    fev = rs.events[fev.key]
    assert lead.seat_of(m) is None and fev.seat_of(m) is None and fev.signups[str(m.discord_id)].status == "out"
    assert fev.seat_of(ms[6]) is not None  # nobody else's follow-on seat moved
