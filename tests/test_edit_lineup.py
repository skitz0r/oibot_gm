"""Phase 3, Task 6: a run's lineup is edited before the lock — a raid added, removed or moved, a roster count changed. Run
keys never change; a removed raid's run is cancelled and its dict dropped; the first raid's answers are untouched and
every later raid still mirrors them; nobody is DMed or told. Runs on test_lineups' wide profile (extra_a, extra_b)."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import OWNER, join, weekly
from test_event_identity import _big_guys_pair, _old_guild, _soon
from test_guild_schedules import commit_count
from test_lineups import BD, H, bot, reg, start_in  # noqa: F401 — the wide profile's fixtures
from test_raid_night import NightBot
from test_reschedule import posted

from oibot_gm import consequences as cq
from oibot_gm import raid_views as rv
from oibot_gm import raidcycle as rc
from oibot_gm.registry import RegistryError

ONY = "onyxias_lair"
ico = lambda *a, **k: ""  # noqa: E731


def run(coro):
    return asyncio.run(coro)


def L(*raids) -> list[dict]:
    """A lineup: the first raid, then (raid, rosters) or a raid id."""
    return [{"raid": r, "rosters": 1} if isinstance(r, str) else {"raid": r[0], "rosters": r[1]} for r in raids]


def open_run(reg, rs, later, days: float = 3):
    return rc.open_run(reg, rs, H, start_in(reg, days), by="t", schedule=rc.ADHOC_SCHEDULE, lineup=later)


def edit(bot, reg, rs, ev, lineup):
    return run(bot.edit_lineup(reg, rs, ev, lineup, by="Jon", by_id=OWNER))


def assert_one_chain(reg, rs, first):
    """One chain per first part, one run per part: the live parts numbered 0..n-1 in start order, each stamped with the
    first part's event and following it, the file chain (followed_by) = the dict chain = event_parts' order, and no
    other live run claims a part."""
    parts = rc.event_parts(rs, first)
    assert [p.part for p in parts] == list(range(len(parts))) and parts[0] is first
    assert all(p.event == first.key for p in parts) and all(p.follows == first.key for p in parts[1:])
    assert [p.followed_by for p in parts] == [p.key for p in parts[1:]] + [None]
    assert [p.start for p in parts] == sorted(p.start for p in parts)
    assert [p.start for p in parts] == [rc.after_minutes(first.start, p.starts) for p in parts]
    assert [t["key"] for t in rc.later_dicts(reg, first)] == [p.team for p in parts[1:]]
    assert all(p.state not in ("done", "cancelled") for p in parts)
    for other in rs.live():
        if other.key not in {p.key for p in parts}:
            assert rc.event_id(other) != first.key
    for p in parts[1:]:  # every later part mirrors the first part's answers
        assert {k: s.status for k, s in p.signups.items()} == {k: s.status for k, s in first.signups.items()}


# ---------------------------------------------------------------- add, reorder, remove

def test_add_reorder_and_remove_keep_part_keys(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 2}, {"raid": "extra_a", "rosters": 1}])
    join(reg, rs, first, reg.test_members()[:5])
    bd, xa = rc.event_parts(rs, first)[1:]
    assert edit(bot, reg, rs, first, L(H, "extra_a", BD, "extra_b")) == [
        "The run is now Hyjal Summit, then Extra A at 9:30 PM, then Barrow Deeps at 11:30 PM, then Extra B."], "the kept raids' new times"
    parts = rc.event_parts(rs, first)
    assert [p.key for p in parts[:3]] == [first.key, xa.key, bd.key], "the kept parts keep their keys, in their new places"
    assert (xa.part, bd.part, parts[3].part, parts[3].instance) == (1, 2, 3, "extra_b")
    # C12: Hyjal 3 h, then Extra A 2 h, then Barrow Deeps 2.5 h
    assert [p.starts for p in parts] == [0, 180, 300, 450] and parts[3].start == first.start + timedelta(minutes=450)
    assert reg.config.roster(bd.team)["rosters"] == 1 and reg.config.roster(bd.team)["starts"] == 300
    t0 = reg.config.roster(first.team)
    assert [e["raid"] for e in t0["lineup"]] == [H, "extra_a", BD, "extra_b"]
    assert reg.config.roster(bd.team)["cutoff_hard_hours"] == t0["cutoff_hard_hours"] + 5, "it still locks at the first raid's instant"
    assert len(parts[3].signups) == 5, "the new raid mirrors the first raid's answers"
    assert_one_chain(reg, rs, first)
    xb = parts[3]
    edit(bot, reg, rs, first, L(H, "extra_a", BD))
    assert rs.events[xb.key].state == "cancelled" and rs.events[xb.key].dropped and rs.events[xb.key].closed_at
    assert rs.events[xb.key].log[-1] == "removed from the lineup by Jon"
    assert reg.config.roster(xb.team) is None, "its run dict is dropped"
    assert [p.key for p in rc.event_parts(rs, first)] == [first.key, xa.key, bd.key]
    assert_one_chain(reg, rs, first)
    assert bot.dms == {}, "nobody is DMed"


def test_an_edit_is_one_commit(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    reg.store.flush()
    n0 = commit_count(reg.store)
    edit(bot, reg, rs, first, L(H, "extra_b", BD))
    reg.store.flush()
    assert commit_count(reg.store) == n0 + 1


def test_removing_a_raid_drops_only_its_board_places(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    ms = reg.test_members()[:6]
    join(reg, rs, first, ms)
    first.layout, first.pins = [[ms[0].display_name, ms[1].display_name]], {str(ms[2].discord_id): "in"}
    rs.save(first, "board")
    bd = rc.event_parts(rs, first)[1]
    bd.layout, bd.pins = [[ms[0].display_name, ms[3].display_name]], {str(ms[4].discord_id): "out"}
    rs.save(bd, "board")
    rc.leave_follow(reg, rs, bd, ms[5])
    before = ({k: s.model_dump() for k, s in first.signups.items()}, first.layout, dict(first.pins))
    said = run(bot.edit_lineup(reg, rs, first, L(H, "extra_a"), by="Jon", by_id=OWNER, preview=True))
    assert said == ["The lineup becomes Hyjal Summit, then Extra A at 9:30 PM.",
                    "Everyone's answer covers the new lineup, and the 6 who answered see a notice.",
                    "Barrow Deeps is taken out with its 2 board places, 1 pin and 1 plan to leave early."]
    edit(bot, reg, rs, first, L(H, "extra_a"))
    assert ({k: s.model_dump() for k, s in first.signups.items()}, first.layout, dict(first.pins)) == before
    assert rs.events[bd.key].state == "cancelled" and rs.events[bd.key].pins, "its pins stay in its cancelled file, for the record"
    xa = rc.event_parts(rs, first)[1]
    assert xa.signups[str(ms[5].discord_id)].status == "in", "leaving before Barrow Deeps was about Barrow Deeps alone"
    assert bot.dms == {} and bot.updates[-1] == "🗂 lineup changed by Jon: Hyjal Summit, then Extra A at 9:30 PM · 6 who answered see a notice"
    audits = [t for _lvl, t in bot.ops.lines if "lineup" in t]
    assert len(audits) == 1 and bd.key in audits[0]
    assert_one_chain(reg, rs, first)


def test_a_new_raid_and_a_roster_count_on_the_first_raid(bot, reg, rs):
    first = open_run(reg, rs, [])
    join(reg, rs, first, reg.test_members()[:4])
    assert rc.later_parts(rs, first) == []
    edit(bot, reg, rs, first, L((H, 2), (BD, 2)))
    parts = rc.event_parts(rs, first)
    assert [p.instance for p in parts] == [H, BD] and reg.config.roster(first.team)["rosters"] == 2
    assert reg.config.roster(parts[1].team)["rosters"] == 2 and parts[1].starts == 180
    assert_one_chain(reg, rs, first)
    edit(bot, reg, rs, first, L((H, 2)))
    assert rc.event_parts(rs, first) == [first] and first.followed_by is None
    assert "followed_by" not in reg.config.roster(first.team) and "lineup" not in reg.config.roster(first.team)


def test_an_edit_from_a_later_raid_and_the_lock_after_it(bot, reg, rs, monkeypatch):
    from test_lineups import seat_first

    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    join(reg, rs, first, reg.test_members())
    edit(bot, reg, rs, rc.event_parts(rs, first)[2], L(H, "extra_b", "extra_a"))  # any raid of the run stands for its first
    assert [p.instance for p in rc.event_parts(rs, first)] == [H, "extra_b", "extra_a"]
    monkeypatch.setattr(rc, "propose", seat_first())
    run(bot.lock_run(reg, rs, first, by="Jon"))
    assert [p.state for p in rc.event_parts(rs, first)] == ["locked"] * 3, "the lock builds the new lineup, in order"


# ---------------------------------------------------------------- refusals

def test_the_first_raid_cannot_change_and_a_locked_run_refuses(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}])
    assert rc.lineup_refusal(reg, rs, first, L(BD, H)) == rc.LINEUP_FIRST
    assert rc.lineup_refusal(reg, rs, first, L(H, "nowhere")) == "This game has no raid called nowhere. Pick another raid."
    assert rc.lineup_refusal(reg, rs, first, L(H, BD, "extra_a", "extra_b", ONY)) == "A run holds at most 4 raids."
    assert rc.lineup_refusal(reg, rs, first, L(H, BD, BD)) == "A run can't hold the same raid twice."
    assert rc.lineup_refusal(reg, rs, first, L(H, BD)) == rc.LINEUP_SAME
    assert rc.lineup_refusal(reg, rs, first, [{"raid": H}, {"raid": BD, "starts": 200}, {"raid": "extra_a", "starts": 100}]) == "Each raid starts after the one before it."
    assert rc.lineup_refusal(reg, rs, first, [{"raid": H, "starts": 0}, {"raid": "extra_a"}]) is None, "the first raid's own 0 is fine"
    member = next(m for m in reg.members.values() if m.discord_id != OWNER)
    with pytest.raises(RegistryError, match=rc.LINEUP_OFFICER_ONLY):
        run(bot.edit_lineup(reg, rs, first, L(H, "extra_a"), by="x", by_id=member.discord_id))
    first.state = "locked"
    rs.save(first, "locked")
    assert rc.lineup_refusal(reg, rs, rc.event_parts(rs, first)[1], L(H, "extra_a")) == rc.LINEUP_LOCKED
    with pytest.raises(RegistryError, match="fixed once the roster locks"):
        edit(bot, reg, rs, first, L(H, "extra_a"))
    first.state = "cancelled"
    rs.save(first, "cancelled")
    assert rc.lineup_refusal(reg, rs, first, L(H, "extra_a")) == rc.LINEUP_CLOSED


def test_a_schedules_run_changes_alone(bot, reg, rs):
    sid = weekly(reg, H, "Tue 18:30")
    entry = dict(reg.schedule_entry(sid))
    now = reg.now_local()
    t = next(t for s, t in rc.upcoming(reg, H, now, 24 * 21) if s["id"] == sid and t - now >= timedelta(hours=30))
    first = rc.open_run(reg, rs, H, t, by="scheduler", schedule=sid)
    said = cq.lineup_consequence(reg, rs, first, L(H, (BD, 2)))
    assert said == ["The lineup becomes Hyjal Summit, then Barrow Deeps ×2.",
                    "Everyone's answer now covers the new lineup, and the schedule keeps its own lineup."]
    edit(bot, reg, rs, first, L(H, (BD, 2)))
    assert reg.schedule_entry(sid) == entry, "the schedule is never touched"
    assert rc.event_of_occurrence(rs, sid, first.occurrence) is first


def test_the_sheet_title_follows_the_new_lineup(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    sheet = posted(bot, first)
    edit(bot, reg, rs, first, L(H, "extra_a", BD))
    e, _view = rv.sheet_message(reg, first, rc.run_team(reg, first), ico)
    assert e.title == "Hyjal Summit, then Extra A, then Barrow Deeps"
    assert sheet.sent == [] and sheet.msg.edits and sheet.msg.edits[-1]["embed"].title == e.title, "edited in place, never reposted"


# ---------------------------------------------------------------- the live shape

def test_the_old_one_off_pair_drops_its_second_raid_and_takes_another(data_root, old_store_with):
    """big-guys-for's live run (older `-adhoc` / `-after` keys, no created_at, a posted sheet): Barrow Deeps out,
    Onyxia's Lair in; the keys, the sheet message and the answers stand."""
    t = _soon(48).replace(hour=18, minute=30)
    cfg, dicts, files = _big_guys_pair(t)
    reg, rs, store = _old_guild(data_root, old_store_with, cfg, files)
    reg.set_raid_overrides(ONY, {"first_open": "2026-01-01T00:00"}, "t")
    rc.migrate_at_start(reg, rs)
    lk, fk = files[0]["key"], files[1]["key"]
    lt, ft = dicts[0]["key"], dicts[1]["key"]
    bot = NightBot(reg, rs)
    lead = rs.events[lk]
    sheet = posted(bot, lead)
    lead.message_id = 777
    answers = {k: v.model_dump() for k, v in lead.signups.items()}
    said = cq.lineup_consequence(reg, rs, rs.events[fk], L(H, ONY))
    assert said == ["The lineup becomes Hyjal Summit, then Onyxia's Lair.", "Everyone's answer now covers the new lineup."]
    run(bot.edit_lineup(reg, rs, rs.events[fk], L(H, ONY), by="o", by_id=OWNER))
    lead = rs.events[lk]
    assert lead.message_id == 777 and sheet.sent == [] and sheet.msg.edits, "the sheet is edited in place"
    assert {k: v.model_dump() for k, v in lead.signups.items()} == answers
    assert rs.events[fk].state == "cancelled" and rs.events[fk].dropped and reg.config.roster(ft) is None
    ony = rc.follow_of(rs, lead)
    assert ony.instance == ONY and ony.key not in (lk, fk) and ony.follows == lk and ony.event == lk and ony.part == 1
    assert ony.start == t + timedelta(hours=3) and set(ony.signups) == set(answers)
    t0 = reg.config.roster(lt)
    assert "follow" not in t0 and t0["followed_by"] == ony.team and [e["raid"] for e in t0["lineup"]] == [H, ONY]
    e, _view = rv.sheet_message(reg, lead, rc.run_team(reg, lead), ico)
    assert e.title == "Hyjal Summit, then Onyxia's Lair"
    assert_one_chain(reg, rs, lead)
    again = rc.RaidStore(store, rs.key)  # a restart reads the same chain back
    assert [p.key for p in rc.event_parts(again, again.events[lk])] == [lk, ony.key]


# ---------------------------------------------------------------- fix round 1

def test_a_kept_raid_that_starts_at_another_time_tells_everyone_who_answered(bot, reg, rs):
    """Taking out raid 2 pulls raid 3 earlier by raid 2's length: its new time is in the confirm, and everyone who
    answered gets reschedule's `moved` notice (an away row is not an answer). Nobody is DMed."""
    from test_reschedule import moved_notices, real_people

    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    ms = real_people(reg, 4)
    join(reg, rs, first, ms[:3])
    xa = rc.event_parts(rs, first)[2]
    said = cq.lineup_consequence(reg, rs, first, L(H, "extra_a"))
    assert said == ["The lineup becomes Hyjal Summit, then Extra A at 9:30 PM.",
                    "Everyone's answer covers the new lineup, and the 3 who answered see a notice."]
    edit(bot, reg, rs, first, L(H, "extra_a"))
    assert xa.start == first.start + timedelta(hours=3) and xa.part == 1
    for m in ms[:3]:
        assert [n["text"] for n in moved_notices(reg, m.discord_id)] == [f"Extra A now starts {reg.local12(xa.start)}."]
    assert moved_notices(reg, ms[3].discord_id) == [] and bot.dms == {}
    # a raid added at the end moves nobody's kept raid: no notice, no time
    assert cq.lineup_consequence(reg, rs, first, L(H, "extra_a", "extra_b")) == [
        "The lineup becomes Hyjal Summit, then Extra A, then Extra B.", "Everyone's answer now covers the new lineup."]


def test_an_ask_on_a_removed_raid_closes_under_its_plan_lock_and_its_card_says_so(bot, reg, rs, monkeypatch):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    ms = reg.test_members()[:3]
    join(reg, rs, first, ms[:2])
    bd = rc.event_parts(rs, first)[1]

    def ask(m, mid):
        return rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="other", role="healer", character="X",
                          spec="Holy", reason="short 1 healer", phase="prelock", dm_channel_id=50, dm_message_id=mid)

    bd.fill_asks.append(ask(ms[2], 501))  # an officer's pre-lock Ask on the later raid
    rs.save(bd, "asked")
    assert cq.lineup_consequence(reg, rs, first, L(H, "extra_a"))[-1] == "Barrow Deeps is taken out with its 1 open ask."
    real = rc.apply_lineup

    def apply_then_late_ask(*a, **k):  # a fan-out in flight appends one more after the write
        out = real(*a, **k)
        bd.fill_asks.append(ask(ms[1], 502))
        return out

    monkeypatch.setattr(rc, "apply_lineup", apply_then_late_ask)
    edited = []

    async def edit_card(reg_, cid, mid, text):
        edited.append((mid, text))
        return True

    monkeypatch.setattr(bot, "edit_ask_card", edit_card)
    edit(bot, reg, rs, first, L(H, "extra_a"))
    assert [a.answer for a in rs.events[bd.key].fill_asks] == ["cancelled", "cancelled"]
    assert sorted(edited) == [(501, "Barrow Deeps was taken out of the Hyjal Summit run, so this ask is closed."),
                              (502, "Barrow Deeps was taken out of the Hyjal Summit run, so this ask is closed.")]


def test_adding_a_raid_before_it_first_opens_is_allowed_and_said(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}])
    later = first.start + timedelta(days=30)
    reg.set_raid_overrides("extra_b", {"first_open": later.isoformat()}, "t")
    said = cq.lineup_consequence(reg, rs, first, L(H, BD, "extra_b"))
    assert said[-1] == f"Extra B first opens {reg.local12(reg.first_open('extra_b'))}, after this run starts."
    assert len(said) <= 3
    edit(bot, reg, rs, first, L(H, BD, "extra_b"))
    assert [p.instance for p in rc.event_parts(rs, first)] == [H, BD, "extra_b"]


def test_a_dropped_raid_answers_as_itself(bot, reg, rs):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}, {"raid": "extra_a", "rosters": 1}])
    bd = rc.event_parts(rs, first)[1]
    edit(bot, reg, rs, first, L(H, "extra_a"))
    bd = rs.events[bd.key]
    assert rc.first_part(rs, bd) is bd and rc.lead_of(rs, bd) is None and rc.event_parts(rs, bd) == [bd]
    with pytest.raises(RegistryError, match="already cancelled"):
        run(bot.cancel_run(reg, rs, bd, "Jon", None))
    assert first.state == "open" and all(p.state == "open" for p in rc.event_parts(rs, first)), "the live run stands"
    assert rc.reschedule_refusal(reg, rs, bd, first.start + timedelta(hours=1), None) == rc.MOVE_CLOSED
    assert rc.lineup_refusal(reg, rs, bd, L(H)) == rc.LINEUP_CLOSED


def test_a_removed_raid_is_closed_on_file_before_its_dict_goes(bot, reg, rs, monkeypatch):
    first = open_run(reg, rs, [{"raid": BD, "rosters": 1}])
    bd = rc.event_parts(rs, first)[1]
    change = rc.plan_lineup(reg, rs, first, L(H))

    def crash(*a, **k):
        raise RuntimeError("power cut")

    monkeypatch.setattr(reg, "save_config", crash)
    with pytest.raises(RuntimeError):
        rc.apply_lineup(reg, rs, first, change, "Jon")
    again = rc.RaidStore(reg.store, rs.key)
    assert again.events[bd.key].state == "cancelled" and again.events[bd.key].dropped


def test_the_first_raids_roster_count_alone_reads_aloud(reg, rs):
    first = open_run(reg, rs, [])
    assert cq.lineup_consequence(reg, rs, first, L((H, 2))) == ["The run becomes Hyjal Summit alone, with 2 rosters.",
                                                                "Everyone's answer now covers the new lineup."]
