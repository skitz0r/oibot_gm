"""Lineups of up to four raids (phase 3, Task 4): one signup, one lock that builds every raid in order, one cancel.
profiles/forever has three raids, so this file's `reg` runs on a profile COPY with two more (extra_a, extra_b) — no
profile file changes."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta

import pytest
from conftest import GUILD, TEST_MEMBERS, by_role, join

from oibot_gm import raidcycle as rc
from oibot_gm.registry import Registry, RegistryError

H, BD = "hyjal_summit_forever", "barrow_deeps"
LINEUP = [{"raid": "barrow_deeps", "rosters": 2}, {"raid": "extra_a", "rosters": 1}, {"raid": "extra_b", "rosters": 1}]


@pytest.fixture
def reg(store, profile):
    """conftest's guild on a profile copy with two more raids (Barrow Deeps' rules, 2 hours each); every raid open."""
    p = copy.deepcopy(profile)  # a dataclass: a deep copy (the brief says model_copy(deep=True), a pydantic name)
    for rid, name in (("extra_a", "Extra A"), ("extra_b", "Extra B")):
        p.raids[rid] = {**copy.deepcopy(p.raids[BD]), "id": rid, "name": name, "duration_hours": 2}
    r = Registry(store, GUILD, p)
    r.seed_test_members(TEST_MEMBERS, "t")
    for rid in r.profile.raids:
        r.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")
    return r


@pytest.fixture
def bot(reg, rs):
    from test_raid_night import NightBot

    return NightBot(reg, rs)


def run(coro):
    return asyncio.run(coro)


def start_in(reg, days: float) -> datetime:
    return (reg.now_local() + timedelta(days=days)).replace(hour=18, minute=30, second=0, microsecond=0)


def open_four(reg, rs, days: float = 3) -> rc.RaidEvent:
    return rc.open_run(reg, rs, H, start_in(reg, days), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=LINEUP)


def seat_first(n_by_raid: dict[str, int] | None = None):
    """rc.propose without the solver: each raid's rosters filled from the joiners in order (a short role stays short)."""
    def propose(reg, rs, ev, save=True):
        names = [s.display_name for s in ev.by_status("in")]
        size = rc.run_size(reg, ev)
        take = names[: size * rc.run_rosters(reg, ev)]
        g = int(reg.profile.comp_rules["group_size"])
        ev.rosters = rc.board_rosters(reg, ev, [take[i:i + g] for i in range(0, len(take), g)], size)
        return [], ev.rosters[0]
    return propose


# ---------------------------------------------------------------- the four parts

def test_a_four_raid_lineup_opens_four_parts_from_one_signup(reg, rs):
    first = open_four(reg, rs)
    parts = rc.event_parts(rs, first)
    assert [p.part for p in parts] == [0, 1, 2, 3] and {p.event for p in parts} == {first.key}
    assert [p.starts for p in parts] == reg.lineup_offsets([{"raid": H, "rosters": 1}, *LINEUP]) == [0, 180, 330, 450]
    assert [p.instance for p in parts] == [H, BD, "extra_a", "extra_b"]
    assert all(p.follows == first.key for p in parts[1:]), "every later part follows the FIRST part"
    assert [p.followed_by for p in parts] == [parts[1].key, parts[2].key, parts[3].key, None], "each part names the next"
    assert [p.start for p in parts] == [first.start + timedelta(minutes=m) for m in (0, 180, 330, 450)]
    t0 = reg.config.roster(first.team)
    assert t0["lineup"] == [{"raid": H, "rosters": 1, "starts": 0}, {"raid": BD, "rosters": 2, "starts": 180},
                            {"raid": "extra_a", "rosters": 1, "starts": 330}, {"raid": "extra_b", "rosters": 1, "starts": 450}]
    assert "follow" not in t0
    for p in parts[1:]:
        d = reg.config.roster(p.team)
        assert d["cutoff_hard_hours"] == t0["cutoff_hard_hours"] + p.starts / 60, "every raid locks at the first raid's instant"
        assert (d["confirm"], d["reminders"], d["open_dm"], d["series"]) == (False, rc.OFF, False, None)
        assert d["name"] == f"{reg.raid_def(p.instance)['name']} · after Hyjal Summit {reg.local12(first.start)}"
    join(reg, rs, first, reg.test_members()[:5])
    assert all(len(p.signups) == 5 for p in rc.event_parts(rs, first))
    assert rc.follow_words_of(reg, first) == "Barrow Deeps ×2, then Extra A, then Extra B"
    assert rc.follow_words_of(reg, parts[2]) == ""


def test_a_fifth_raid_is_refused(reg, rs):
    n = len(reg.config.rosters)
    with pytest.raises(RegistryError, match="at most 4 raids"):
        rc.open_run(reg, rs, H, start_in(reg, 3), schedule=rc.ADHOC_SCHEDULE, lineup=LINEUP + [{"raid": "onyxias_lair", "rosters": 1}])
    assert len(reg.config.rosters) == n and not rs.events, "a refused lineup writes nothing"


def test_a_raid_twice_or_out_of_order_is_refused(reg, rs):
    with pytest.raises(RegistryError, match="same raid twice"):
        rc.open_run(reg, rs, H, start_in(reg, 3), schedule=rc.ADHOC_SCHEDULE, lineup=[{"raid": BD}, {"raid": BD}])
    with pytest.raises(RegistryError, match="Each raid starts after the one before it"):
        rc.open_run(reg, rs, H, start_in(reg, 3), schedule=rc.ADHOC_SCHEDULE,
                    lineup=[{"raid": BD, "starts": 200}, {"raid": "extra_a", "starts": 190}])
    assert not rs.events


def test_one_first_part_holds_one_chain_and_a_part_one_run(reg, rs):
    """Task 3 dropped the 'already has a follow-on' refusal: two runs at the same minute keep their own chains."""
    a = open_four(reg, rs)
    b = rc.open_run(reg, rs, H, a.start + timedelta(minutes=1), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=LINEUP[:2])
    pa, pb = rc.event_parts(rs, a), rc.event_parts(rs, b)
    assert len(pa) == 4 and len(pb) == 3 and not {p.key for p in pa} & {p.key for p in pb}
    assert all(rc.first_part(rs, p) is a for p in pa) and all(rc.first_part(rs, p) is b for p in pb)
    assert rc.open_run(reg, rs, H, a.start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=LINEUP) is a  # found, never a second chain
    assert len(rc.event_parts(rs, a)) == 4


def test_a_schedules_four_raid_lineup_round_trips_and_opens(reg, rs):
    reg.set_schedule("tue", {"slots": "Tue 18:30", "lineup": [{"raid": H, "rosters": 1}, *LINEUP]}, "t", raid=H)
    entry = next(e for e in reg.config.schedules if e["id"] == "tue")
    assert [e["raid"] for e in entry["lineup"]] == [H, BD, "extra_a", "extra_b"] and "then" not in entry
    row = next(r for r in reg.raid_rows(H) if r["id"] == "tue")
    assert row["follow"] == {"raid": BD, "rosters": 2} and row["then"] == [{"raid": "extra_a", "rosters": 1}, {"raid": "extra_b", "rosters": 1}]
    assert reg.schedule_label(H, reg.schedule_entry("tue")).endswith("· then Barrow Deeps ×2, then Extra A, then Extra B")
    with pytest.raises(RegistryError, match="The schedule called tue already exists"):
        reg.set_schedule("tue", {"lineup": [{"raid": H}, {"raid": BD}]}, "t", raid=H)  # naming a raid means create (its own: how to edit)
    reg.set_schedule("tue", {"follow": "none"}, "t")  # the second raid cleared: the third moves up, the order kept
    assert [e["raid"] for e in reg.schedule_entry("tue")["lineup"]] == [H, "extra_a", "extra_b"]
    now = reg.now_local()
    t = next(t for s, t in rc.upcoming(reg, H, now, 24 * 21) if s["id"] == "tue" and t - now >= timedelta(hours=30))
    first = rc.open_run(reg, rs, H, t, by="scheduler", schedule="tue")
    assert [p.instance for p in rc.event_parts(rs, first)] == [H, "extra_a", "extra_b"]


# ---------------------------------------------------------------- the lock

def test_a_later_part_never_locks_first(bot, reg, rs):
    first = open_four(reg, rs)
    p2 = rc.event_parts(rs, first)[2]
    with pytest.raises(RegistryError) as e:
        run(bot.lock_run(reg, rs, p2, by="Jon"))
    assert str(e.value) == f"Lock Hyjal Summit {reg.local12(first.start)} first. Its lock builds every raid of the run."
    assert all(p.state == "open" for p in rc.event_parts(rs, first))


def test_four_raids_lock_in_order_and_a_short_role_never_stops_one(bot, reg, rs, monkeypatch):
    first = open_four(reg, rs)
    tanks = by_role(reg)["tank"]
    join(reg, rs, first, [m for m in reg.test_members() if m not in tanks[1:]])  # one tank: every raid is a tank short
    monkeypatch.setattr(rc, "propose", seat_first())
    line = run(bot.lock_run(reg, rs, first, by="Jon"))
    parts = rc.event_parts(rs, first)
    assert [p.state for p in parts] == ["locked"] * 4 and all(p.all_rosters for p in parts)
    assert all(any(x.startswith("locked short") for x in p.log) for p in parts), "short of a tank, every raid still locks"
    assert all(p.key not in line for p in parts) and line.startswith(f"Hyjal Summit {reg.local12(first.start)}: locked")  # a name, never a key
    # a fresh run whose third raid's solve fails: the first two lock, the third stays open with its error, the fourth waits
    first = open_four(reg, rs, days=4)
    join(reg, rs, first, reg.test_members())
    good = seat_first()

    def failing(reg_, rs_, ev, save=True):
        if ev.instance == "extra_a":
            raise RuntimeError("no feasible roster")
        return good(reg_, rs_, ev, save)

    monkeypatch.setattr(rc, "propose", failing)
    run(bot.lock_run(reg, rs, first, by="Jon"))
    parts = rc.event_parts(rs, first)
    assert [p.state for p in parts] == ["locked", "locked", "open", "open"]
    assert parts[2].lock_error and not parts[3].lock_error and parts[3].lock_tried_at is None
    # the scheduler's minute: the third raid is retried first, the fourth waits for it
    parts[2].lock_tried_at = (datetime.fromisoformat(rc.now()) - timedelta(minutes=20)).isoformat()
    later = first.start - timedelta(minutes=1)
    monkeypatch.setattr(reg, "now_local", lambda: later)
    tried = []

    async def lock_run(reg_, rs_, ev, by="scheduler"):
        tried.append(ev.key)
        return "ok"

    monkeypatch.setattr(bot, "lock_run", lock_run)
    monkeypatch.setattr(bot, "post_health", lambda *a, **k: asyncio.sleep(0))
    run(bot.scheduler_tick_guild(reg))
    assert parts[2].key in tried and parts[3].key not in tried


def test_a_later_part_locked_on_retry_carries_on_down_the_chain(bot, reg, rs, monkeypatch):
    first = open_four(reg, rs)
    join(reg, rs, first, reg.test_members())
    good, fail = seat_first(), {"on": True}

    def flaky(reg_, rs_, ev, save=True):
        if ev.instance == "extra_a" and fail["on"]:
            raise RuntimeError("no feasible roster")
        return good(reg_, rs_, ev, save)

    monkeypatch.setattr(rc, "propose", flaky)
    run(bot.lock_run(reg, rs, first, by="Jon"))
    fail["on"] = False
    p2 = rc.event_parts(rs, first)[2]
    run(bot.lock_run(reg, rs, p2, by="scheduler"))
    assert [p.state for p in rc.event_parts(rs, first)] == ["locked"] * 4


# ---------------------------------------------------------------- one cancel, the people, the night

def test_cancel_on_any_part_cancels_the_whole_run(bot, reg, rs):
    first = open_four(reg, rs)
    parts = rc.event_parts(rs, first)
    from oibot_gm import consequences as cq

    said = cq.consequence("cancel", reg, parts[2])
    assert said[0] == "The whole Hyjal Summit run is cancelled, with Barrow Deeps, Extra A and Extra B."
    assert cq.consequence("cancel", reg, first)[1] == "The Barrow Deeps, Extra A and Extra B follow-ons are cancelled with it, and nobody gets a DM."
    run(bot.cancel_run(reg, rs, parts[2], "Jon", "storm"))
    assert [p.state for p in rc.event_parts(rs, first)] == ["cancelled"] * 4
    audits = [t for _lvl, t in bot.ops.lines if "cancelled" in t]
    assert len(audits) == 1 and all(rc.run_words(reg, p) in audits[0] and p.key not in audits[0] for p in parts)


def test_conflicts_never_count_another_part_of_the_same_run(reg, rs):
    first = open_four(reg, rs)
    join(reg, rs, first, reg.test_members()[:5])
    assert rc.conflicts(rs, rc.event_parts(rs, first)[3]) == {}
    assert rc.conflicts(rs, first) == {}


def test_a_seat_freed_on_the_first_raid_goes_on_every_later_one(bot, reg, rs, monkeypatch):
    first = open_four(reg, rs)
    join(reg, rs, first, reg.test_members())
    monkeypatch.setattr(rc, "propose", seat_first())
    run(bot.lock_run(reg, rs, first, by="Jon"))
    m = next(m for m in reg.test_members() if all(p.seat_of(m) for p in rc.event_parts(rs, first)))
    run(bot.call_out(reg, rs, first, m, None, by=m.display_name, by_id=m.discord_id))
    assert all(p.seat_of(m) is None for p in rc.event_parts(rs, first))
    m2 = next(x for x in reg.test_members() if x is not m and all(p.seat_of(x) for p in rc.event_parts(rs, first)))
    p2 = rc.event_parts(rs, first)[2]
    run(bot.call_out(reg, rs, p2, m2, None, by=m2.display_name, by_id=m2.discord_id))  # leaving after part 1: part 2 alone
    parts = rc.event_parts(rs, first)
    assert parts[2].seat_of(m2) is None and m2.discord_id in parts[2].leaving
    assert all(parts[k].seat_of(m2) for k in (0, 1, 3)), "leaving acts on the raid named"


def test_the_sheet_and_the_health_card_name_every_later_raid(reg, rs):
    from oibot_gm import raid_views as rv

    import discord

    first = open_four(reg, rs)
    ico = lambda *a, **k: ""  # noqa: E731
    e, _view = rv.sheet_message(reg, first, rc.run_team(reg, first), ico)
    assert e.title.endswith("Hyjal Summit, then Barrow Deeps ×2, then Extra A, then Extra B")
    assert e.description.count("\nthen ") == 3
    shorts = rc.parts_short(reg, first)
    assert [n for n, _ in shorts] == ["Barrow Deeps", "Extra A", "Extra B"]  # nobody joined: every later raid is short
    card = "\n".join(str(ch.content) for ch in rv.health_layout(reg, rs, first, rc.run_team(reg, first), ico).walk_children()
                     if isinstance(ch, discord.ui.TextDisplay))
    assert all(f"**{n} follow-on** short" in card for n in ("Barrow Deeps", "Extra A", "Extra B"))
    assert rc.parts_short(reg, rc.event_parts(rs, first)[1]) == []  # a later raid has no later raids of its own


# ---------------------------------------------------------------- when a run may start (C8, C9)

def test_a_schedule_skips_a_night_a_later_raid_is_not_open_yet(reg):
    reg.set_schedule("tue", {"slots": "Tue 18:30"}, "t", raid=H)
    t1, t2 = rc.next_runs_of(reg, H, reg.schedule_entry("tue"), n=2)
    reg.set_schedule("tue", {"follow": BD}, "t")
    opens = (t1 + timedelta(days=2)).astimezone(reg.tz)  # Barrow Deeps first opens between the two Tuesdays' 9:30 PM
    reg.set_raid_overrides(BD, {"first_open": opens.strftime("%Y-%m-%dT%H:%M")}, "t")
    runs = rc.next_runs_of(reg, H, reg.schedule_entry("tue"), n=3)
    assert t1 not in runs and t2 in runs


def test_a_one_off_before_first_open_says_so(reg, rs):
    start = start_in(reg, 3)
    fo = (start + timedelta(days=5)).astimezone(reg.tz)
    reg.set_raid_overrides("extra_a", {"first_open": fo.strftime("%Y-%m-%dT%H:%M")}, "t")
    when = start.astimezone(reg.tz).strftime("%Y-%m-%d %H:%M")
    p = rc.open_preview(reg, rs, H, when, rc.ADHOC_SCHEDULE, lineup=LINEUP)
    assert p["before_open"] == f"Extra A first opens {reg.local12(reg.first_open('extra_a'))}, after this run starts."
    assert [x["name"] for x in p["later"]] == ["Barrow Deeps", "Extra A", "Extra B"]
    assert p["follow_line"].startswith("Then Barrow Deeps ×2 at ") and p["follow_line"].endswith(", from the same signup.")
    first = rc.open_run(reg, rs, H, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=LINEUP)  # a one-off still opens
    assert len(rc.event_parts(rs, first)) == 4
    assert rc.open_preview(reg, rs, H, when, rc.ADHOC_SCHEDULE)["before_open"] is None  # the run already there: no warning


# ---------------------------------------------------------------- a restart between parts

def test_an_open_cut_off_between_parts_is_completed_once(bot, reg, rs, monkeypatch):
    real = rc.open_event

    def cut(reg_, rs_, team, at):
        if team.get("part") == 2:
            raise OSError("the bot restarted")
        return real(reg_, rs_, team, at)

    monkeypatch.setattr(rc, "open_event", cut)
    with pytest.raises(OSError):
        open_four(reg, rs)
    first = next(e for e in rs.events.values() if e.instance == H)
    first.message_id = 4242
    rs.save(first, "harness: the sheet is posted")
    assert sum(1 for t in reg.config.rosters if t["key"] == first.team or t.get("follows") == first.team) == 4
    assert sorted(p.part for p in rc.event_parts(rs, first)) == [0, 1]
    monkeypatch.setattr(rc, "open_event", real)
    opened = rc.complete_parts(reg, rs, first)
    assert [p.part for p in opened] == [2, 3] and rc.complete_parts(reg, rs, first) == []
    parts = rc.event_parts(rs, first)
    assert [p.part for p in parts] == [0, 1, 2, 3] and [p.followed_by for p in parts] == [parts[1].key, parts[2].key, parts[3].key, None]
    assert rs.events[first.key].message_id == 4242


def test_the_scheduler_minute_completes_a_cut_off_open(bot, reg, rs, monkeypatch):
    real = rc.open_event
    monkeypatch.setattr(rc, "open_event", lambda reg_, rs_, team, at: (_ for _ in ()).throw(OSError("cut")) if team.get("part") == 3 else real(reg_, rs_, team, at))
    with pytest.raises(OSError):
        open_four(reg, rs)
    monkeypatch.setattr(rc, "open_event", real)
    first = next(e for e in rs.events.values() if e.instance == H)
    monkeypatch.setattr(bot, "post_health", lambda *a, **k: asyncio.sleep(0))
    run(bot.scheduler_tick_guild(reg))
    run(bot.scheduler_tick_guild(reg))
    assert [p.part for p in rc.event_parts(rs, first)] == [0, 1, 2, 3]
    assert sum(1 for e in rs.events.values() if e.instance == "extra_b") == 1


# ---------------------------------------------------------------- the raid wizard

def test_the_raid_wizard_keeps_a_third_raid_it_does_not_show(reg, monkeypatch):
    from wizard_harness import Harness

    from oibot_gm import discord_registry
    from oibot_gm import flows_config as _fc  # noqa: F401 — registers the flows
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    reg.set_schedule("tue", {"slots": "Tue 18:30", "lineup": [{"raid": H, "rosters": 1}, *LINEUP[:2]]}, "t", raid=H)
    h = Harness(reg)

    async def go():
        await FLOWS["raid_config"](h.command(), reg, raid=H, section="times")
        if "Pick a schedule" in h.selects():
            await h.pick("Pick a schedule", "tue")
        await h.pick("Change this schedule", "name")
        await h.submit(h.modal, name="Main night")
        await h.press("Back to the sections")
        await h.press("Save")
        await h.press("Save")
    run(go())
    s = reg.schedule_entry("tue")
    assert s["name"] == "Main night" and [e["raid"] for e in s["lineup"]] == [H, BD, "extra_a"]


def test_a_structured_four_raid_lineup_creates_the_whole_schedule(reg):
    """The site and MCP name a new schedule's lineup: three or four raids go through in one field, none dropped."""
    from oibot_gm import configops

    ops = configops.ops_from_fields(reg, "schedule", "late", {"lineup": [{"raid": H, "rosters": 1}, *LINEUP], "kind": "weekly", "slots": ["Thu 18:30"]})
    (op,) = configops.coalesce(ops)
    assert "then Barrow Deeps ×2, then Extra A, then Extra B" in configops.describe_ops(reg, ops).as_text(reg)
    configops.apply(reg, op, "Officer", False)
    assert [e["raid"] for e in reg.schedule_entry("late")["lineup"]] == [H, BD, "extra_a", "extra_b"]
    with pytest.raises(RegistryError, match="Another schedule is already called late"):  # a create (its kind named) on an id in use
        configops.ops_from_fields(reg, "schedule", "late", {"lineup": [{"raid": H, "rosters": 1}, *LINEUP], "kind": "weekly"})
    # the site's editor on that schedule (no raid, no kind): the lineup is an EDIT (phase 3, Task 7)
    edit = configops.coalesce(configops.ops_from_fields(reg, "schedule", "late", {"lineup": [{"raid": H, "rosters": 2}, *LINEUP[:1]]}))[0]
    configops.apply(reg, edit, "Officer", False)
    assert [(e["raid"], e["rosters"]) for e in reg.schedule_entry("late")["lineup"]] == [(H, 2), (BD, 2)]


# ---------------------------------------------------------------- fix round 1

def test_a_cancel_after_the_first_raid_finished_takes_every_raid_still_to_play(bot, reg, rs):
    """The first raid ends (done) while raids 3–4 of a long lineup still play: a cancel on any later raid cancels every
    LIVE raid, and the confirm names only those, without 'whole'."""
    from oibot_gm import consequences as cq

    first = open_four(reg, rs)
    first.state = "done"
    rs.save(first, "harness: the first raid finished")
    parts = rc.event_parts(rs, first)
    said = cq.consequence("cancel", reg, parts[2], rs=rs)
    assert said == ["The Barrow Deeps, Extra A and Extra B raids are cancelled.",
                    "Nobody gets a DM, but every ask card says Cancelled.", "A cancelled run can't be reopened."]
    run(bot.cancel_run(reg, rs, parts[2], "Jon", None))
    assert [p.state for p in rc.event_parts(rs, first)] == ["done", "cancelled", "cancelled", "cancelled"]
    audits = [t for _lvl, t in bot.ops.lines if "cancelled" in t]
    assert len(audits) == 1 and all(rc.run_words(reg, p) in audits[0] for p in parts[1:]) and rc.run_words(reg, first) not in audits[0]


def test_the_confirm_names_only_the_raids_still_live(reg, rs):
    from oibot_gm import consequences as cq

    first = open_four(reg, rs)
    parts = rc.event_parts(rs, first)
    parts[3].state = "cancelled"  # an older run's raid cancelled on its own
    rs.save(parts[3], "harness")
    assert cq.consequence("cancel", reg, parts[1], rs=rs)[0] == "The whole Hyjal Summit run is cancelled, with Barrow Deeps and Extra A."
    assert cq.consequence("cancel", reg, first, rs=rs)[1] == "The Barrow Deeps and Extra A follow-ons are cancelled with it, and nobody gets a DM."


@pytest.mark.parametrize("keep, words", [((1, 3), "Your Barrow Deeps and Extra B places go too."), ((2,), "Your Extra A place goes too.")],
                         ids=["seated on raids 2 and 4", "seated only on raid 3"])
def test_the_me_pages_cant_names_every_later_place_it_frees(bot, reg, rs, monkeypatch, keep, words):
    from oibot_gm.web.api_me import sheet_row_extras

    first = open_four(reg, rs)
    join(reg, rs, first, reg.test_members())
    monkeypatch.setattr(rc, "propose", seat_first())
    run(bot.lock_run(reg, rs, first, by="Jon"))
    m = next(m for m in reg.test_members() if all(p.seat_of(m) for p in rc.event_parts(rs, first)))
    for k, p in enumerate(rc.event_parts(rs, first)):
        if k and k not in keep:
            rc.free_seat(reg, rs, p, m.discord_id, "test", notice=False)
    assert [k for k, p in enumerate(rc.event_parts(rs, first)) if k and p.seat_of(m)] == list(keep)
    said = sheet_row_extras(reg, rs.events[first.key], m, rs)["cant_consequence"]
    assert words in said and said.count("goes too") + said.count("go too") == 1


def test_a_lineup_across_a_clocks_change_night_starts_each_raid_in_real_time(reg, rs):
    """Spring forward (Sun 14 Mar 2027, 2:00 AM): a 12:30 AM Hyjal of 3 hours is followed at 4:30 AM, never 3:30 AM —
    the preview, the part's stored start and its dict's run time agree."""
    p = rc.open_preview(reg, rs, H, "2027-03-14 00:30", rc.ADHOC_SCHEDULE, lineup=LINEUP)
    start = datetime.fromisoformat(p["start"])
    first = rc.open_run(reg, rs, H, start, by="t", schedule=rc.ADHOC_SCHEDULE, lineup=LINEUP)
    parts = rc.event_parts(rs, first)
    assert [x["when"] for x in p["later"]] == [reg.local12(x.start) for x in parts[1:]]
    assert [round((x.start - first.start).total_seconds() / 60) for x in parts] == [0, 180, 330, 450]
    assert parts[1].start.astimezone(reg.tz).strftime("%H:%M") == "04:30"
    assert reg.config.roster(parts[1].team)["schedule"] == "Sun 04:30"
    assert p["follow_line"].startswith("Then Barrow Deeps ×2 at 4:30 AM")
    lineup = [{"raid": H, "rosters": 1}, *LINEUP]
    reg.set_raid_overrides(BD, {"first_open": "2027-03-14T04:00"}, "t")  # opens between 3:30 (wall) and 4:30 (real)
    assert cq_before(reg, lineup, reg.tz) is None


def cq_before(reg, lineup, tz):
    from oibot_gm import consequences as cq

    return cq.before_first_open_sentence(reg, lineup, datetime(2027, 3, 14, 0, 30, tzinfo=tz))
