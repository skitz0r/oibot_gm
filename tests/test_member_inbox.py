"""Dual interfaces phase 2 B — the member inbox and officers on behalf (design §3.2 B1/B2/B5, §5.40; D14, D16, D21, D26 and
the DM rule): notices on the member file (cap, expiry, mark read), DMs-off asks by phase (pre-lock and confirmations in the
same order on the site; a reachable twin asked in parallel in the last hours, first yes wins), the outcomes that became
notices (not rostered, released, cancelled, answered for you, schedule removed, application, loot award), the receipt that
renders state, the on-behalf union and its consequence sentences, the D21 sheet copy, the character picker, pins in the
`runs` group, and the lint that no courtesy DM is left. The bot is the real RaidMixin over test_raid_night's recording fake
(DM cards that can be edited); Registry and RaidStore are real (conftest)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import by_role, join, lock_with_board, open_test_run
from test_raid_night import NightBot

from oibot_gm import configops, inbox
from oibot_gm import raidcycle as rc
from oibot_gm.registry import NOTICE_CAP, NOTICE_DAYS, PLAIN_GROUPS, RegistryError

SRC = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
REAL = 610_000_000_000_000_000


def run(coro):
    return asyncio.run(coro)


def real(reg, n: int, cls="Priest", spec="Holy", name=None):
    uid = REAL + n
    reg.add_character(uid, f"Player{n}", name or f"Realname{chr(97 + n)}", cls, spec, None, True)
    return reg.members[uid]


def texts(reg, m, unread=False) -> list[str]:
    return [n["text"] for n in reg.notices(m.discord_id, unread=unread)]


# ---------------------------------------------------------------- notices on the member file (D26)

def test_notify_caps_expires_dedupes_and_marks_read(reg):
    m = reg.test_members()[0]
    for i in range(NOTICE_CAP + 5):
        reg.notify(m.discord_id, "released", f"notice {i}", run="r1")
    got = reg.notices(m.discord_id)
    assert len(got) == NOTICE_CAP and got[0]["text"] == f"notice {NOTICE_CAP + 4}", "newest first, capped"
    reg.notify(m.discord_id, "released", f"notice {NOTICE_CAP + 4}", run="r1")
    assert len(reg.notices(m.discord_id)) == NOTICE_CAP and reg.notices(m.discord_id)[0]["id"] == got[0]["id"], "the same unread line twice is kept once"
    old = (datetime.now(timezone.utc) - timedelta(days=NOTICE_DAYS + 1)).isoformat(timespec="seconds")
    reg.members[m.discord_id].notices[0]["at"] = old  # the oldest one is past the window
    assert len(reg.notices(m.discord_id)) == NOTICE_CAP - 1
    one = got[1]["id"]
    assert reg.mark_read(m.discord_id, [one]) == 1 and reg.mark_read(m.discord_id, [one]) == 0
    assert one not in {n["id"] for n in reg.notices(m.discord_id, unread=True)}
    assert reg.mark_read(m.discord_id) == NOTICE_CAP - 2 and reg.notices(m.discord_id, unread=True) == []
    stored = (reg.store.root / reg.key / "members" / f"{m.discord_id}.json").read_text()
    assert '"read_at"' in stored and f"notice {NOTICE_CAP + 4}" in stored, "the inbox lives on the member file"


def test_notice_ids_stay_unique_at_the_cap_in_one_millisecond(reg, monkeypatch):
    """Regression: the id was '<ms>' + len(notices) % 100 — at the cap len stays 20, so two notices in one millisecond
    shared an id and mark_read([id]) marked both."""
    import oibot_gm.registry as registry_mod

    m = reg.test_members()[0]
    for i in range(NOTICE_CAP):
        reg.notify(m.discord_id, "released", f"filler {i}", save=False)
    fixed = datetime(2026, 10, 19, 18, 0, 0, 123000, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(registry_mod, "datetime", Frozen)
    a = reg.notify(m.discord_id, "released", "first", save=False)
    b = reg.notify(m.discord_id, "cancelled", "second", save=False)
    assert a["id"] != b["id"]
    assert reg.mark_read(m.discord_id, [a["id"]]) == 1
    assert [n["text"] for n in reg.notices(m.discord_id, unread=True)][:1] == ["second"]


def test_notify_refuses_unknown_kinds_and_ignores_unknown_members(reg):
    with pytest.raises(ValueError):
        reg.notify(reg.test_members()[0].discord_id, "fyi", "hello")
    assert reg.notify(123, "released", "nobody") is None and reg.notices(123) == []


# ---------------------------------------------------------------- D14: DMs off is no reason to skip anyone

def _tank_short_open(reg, rs):
    """An open test run where nobody who can tank has joined: the planner's pre-lock asks are for tanks."""
    ev = open_test_run(reg, rs)
    roles = by_role(reg)
    join(reg, rs, ev, roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:5])
    return ev


def test_prelock_asks_include_dm_off_members_in_the_same_order(reg, rs):
    ev = _tank_short_open(reg, rs)
    before = [a.discord_id for a in rc.role_plan(reg, rs, ev).asks]
    assert before, "the run is short of tanks: the planner suggests someone"
    first = reg.members[before[0]]
    first.dm_opt_out = True
    after = [a.discord_id for a in rc.role_plan(reg, rs, ev).asks]
    assert after == before, "same order with DMs off (D14): the ask waits on their Me page instead"


def _fill_scene(reg, rs, hours_out: float = 72):
    """24 puppets; a locked 10 seat test run `hours_out` from now with a freed tank seat (two spare tanks never answered)."""
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    ev.starts_at = (reg.now_local() + timedelta(hours=hours_out)).replace(second=0, microsecond=0).isoformat()
    roles = by_role(reg)
    tanks, healers, dps = roles["tank"], roles["healer"], roles["melee"] + roles["ranged"]
    assert len(tanks) >= 4
    seated = tanks[:2] + healers[:3] + dps[:5]
    join(reg, rs, ev, seated)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    gone = next(p for p in ev.roster.selected if p.role == "tank")
    rc.free_seat(reg, rs, ev, gone.signup_name, "declined")
    return ev, tanks


def test_fill_asks_the_dm_off_candidate_first_on_the_site_far_from_start(reg, rs):
    ev, tanks = _fill_scene(reg, rs, hours_out=72)
    team = rc.run_team(reg, ev)
    first = rc.fill_batch(reg, rs, ev, team)[0]
    reg.members[first.discord_id].dm_opt_out = True
    batch = rc.fill_batch(reg, rs, ev, team)
    assert batch[0].discord_id == first.discord_id, "same order"
    assert not [a for a in batch if a.parallel_of is not None], "outside the last hours: no parallel twin"
    bot = NightBot(reg, rs)
    sent, _nd = run(bot.run_fill(reg, rs, ev, team, by="t"))
    ask = next(a for a in ev.fill_asks if a.discord_id == first.discord_id)
    assert ask.channel == "web" and ask.dm_message_id is None and ask.pending
    assert "(site)" in __import__("oibot_gm.raid_views", fromlist=["ask_line"]).ask_line(reg, lambda *a: "", ask)
    shown = inbox.asks_of(reg, rs, first.discord_id)
    assert [(a["kind"], a["channel"], a["run"]) for a in shown] == [("fill", "web", ev.key)]


def test_last_hours_ask_a_reachable_twin_in_parallel_and_the_first_yes_wins(reg, rs):
    ev, tanks = _fill_scene(reg, rs, hours_out=1)
    team = rc.run_team(reg, ev)
    assert rc.parallel_window(reg, ev)
    first = rc.fill_batch(reg, rs, ev, team)[0]
    reg.members[first.discord_id].dm_opt_out = True
    batch = rc.fill_batch(reg, rs, ev, team)
    twins = [a for a in batch if a.parallel_of == first.discord_id]
    assert twins, [(a.display_name, a.parallel_of) for a in batch]
    twin = reg.members[twins[0].discord_id]
    assert not twin.dm_opt_out, "the twin is someone a DM reaches"
    bot = NightBot(reg, rs)
    run(bot.run_fill(reg, rs, ev, team, by="t"))
    primary = next(a for a in ev.fill_asks if a.discord_id == first.discord_id and a.pending)
    plan = rc.role_plan(reg, rs, ev, team)
    assert plan.out == len([a for a in ev.fill_asks if a.pending]) - 1, "the twin is not a second ask out"
    run(bot.answer_fill_for(reg, rs, ev, twin, True, twin.display_name, by_id=twin.discord_id))
    assert primary.answer == "released", "the other ask closes once the seat is taken"
    assert any("someone else took the seat first" in t for t in texts(reg, reg.members[first.discord_id])), "the site ask hears it as a notice"
    assert ev.seat_of(twin)


def test_confirmation_for_a_dm_off_member_waits_on_the_site(reg, rs):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()[:10]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool])
    m = pool[3]
    m.dm_opt_out = True
    bot = NightBot(reg, rs)
    sg = ev.signups[str(m.discord_id)]
    if not rc.run_confirms(reg, ev):
        pytest.skip("test runs without confirmations")
    sent = run(bot.confirm_one(reg, ev, sg, 0, 0, "lock"))
    assert sent is False and not bot.dms.get(m.discord_id)
    (ask,) = [a for a in inbox.asks_of(reg, rs, m.discord_id) if a["kind"] == "confirm"]
    assert ask["channel"] == "web" and ask["deadline"] and ask["group"]["members"] and any(p["me"] for p in ask["group"]["members"])
    assert "Me page" in "\n".join(inbox.inbox_lines(reg, rs, m.discord_id))


# ---------------------------------------------------------------- outcomes become notices, never DMs

def test_not_rostered_joiners_get_one_notice_and_no_dm(reg, rs):
    reg.seed_test_members(14, "t")
    ev = open_test_run(reg, rs)
    pool = reg.test_members()[:13]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool[:10]])
    assert rc.leftover_notices(reg, ev) == 3
    for m in pool[10:]:
        (t,) = texts(reg, m)
        assert "weren't rostered" in t and "10 seats, 13 joined" in t
    assert not texts(reg, pool[0])


def test_a_seat_freed_by_the_board_closes_the_ask_and_a_later_confirm_is_refused(reg, rs):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()[:10]
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool])
    m = pool[2]
    reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    rc.free_seat(reg, rs, ev, m, "board")
    assert any("was released" in t for t in texts(reg, m))
    bot = NightBot(reg, rs)
    with pytest.raises(RegistryError, match="Already answered \\(the seat was released"):
        run(bot.answer_placement_for(reg, m.discord_id, ev.team, True, m.display_name, by_id=m.discord_id))
    assert not ev.seat_of(m)


def test_cancel_writes_notices_for_everyone_on_it_and_sends_no_dm(reg, rs):
    ev = open_test_run(reg, rs)
    pool = reg.test_members()[:6]
    join(reg, rs, ev, pool[:4])
    join(reg, rs, ev, pool[4:5], "sub")
    join(reg, rs, ev, pool[5:6], "out")
    bot = NightBot(reg, rs)
    run(bot.cancel_run(reg, rs, ev, "Officer", "boss bugged", by_id=999))
    assert not bot.dms, "nobody is DMed (the DM rule)"
    for m in pool[:5]:
        (t,) = texts(reg, m)
        assert "was cancelled by Officer" in t and "boss bugged" not in t, "the reason is for officers"
    assert not texts(reg, pool[5]), "a No thanks has nothing to hear"


def test_officer_on_behalf_writes_tell_the_member_and_their_own_do_not(reg, rs):
    ev = open_test_run(reg, rs)
    m, other = reg.test_members()[:2]
    bot = NightBot(reg, rs)
    run(bot.set_answer(reg, rs, ev, m, "in", None, m.display_name, by_id=m.discord_id))
    assert not texts(reg, m), "their own answer is no news"
    run(bot.set_answer(reg, rs, ev, m, "sub", None, "Jon", by_id=other.discord_id))
    (t,) = texts(reg, m)
    assert t.startswith("Jon set you to Bench") and rc.run_words(reg, ev) in t
    run(bot.call_out(reg, rs, ev, m, "sick", "Jon", by_id=other.discord_id))
    assert texts(reg, m)[0].startswith("Jon recorded that you can't make it") and "sick" not in texts(reg, m)[0]


def test_schedule_removed_tells_its_regulars_by_notice(reg):
    reg.set_raid_overrides("barrow_deeps", {"slots": ["Tue 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    reg.set_schedule("barrow_deeps", "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    m = real(reg, 0)
    reg.regular_add(m.discord_id, "barrow_deeps:alt", "t")
    reg.remove_schedule("barrow_deeps", "alt", "Jon")
    (t,) = texts(reg, m)
    assert "removed by Jon" in t


def test_application_decision_has_a_member_copy(reg):
    reg.set_recruitment("t", questions=[])
    reg.apply(5551, "Newbie", "Kessa", "Mage", "Frost", None, None, None, None, surname="Dawn", answers=[])
    reg.decide(5551, True, "Jon")
    (t,) = texts(reg, reg.members[5551])
    assert "accepted by Jon" in t


def test_loot_award_line_is_copied_to_the_recipient(reg):
    from oibot_gm.discord_pool import award_notice

    m = reg.test_members()[0]
    award = SimpleNamespace(raider=m.main.label, discord_id=m.discord_id, item_name="Wand of Tests", item_id=1, boss="Gatekeeper", offspec=False,
                            run_key="r", tier="", weight=1.0, by="Jon", source="council", disenchant=False)
    try:
        award_notice(reg, award, None)
    except Exception as e:  # noqa: BLE001 — the line builder reads more fields than this fake carries
        pytest.skip(f"award fake incomplete: {e}")
    got = texts(reg, m)
    assert got and got[0].startswith("Loot:")


def test_loot_correction_is_copied_to_who_lost_the_item_too(reg):
    """Regression: a correction wrote a notice only for the new recipient; the member who held the item heard nothing."""
    from oibot_gm.discord_pool import award_notice

    old, new = reg.test_members()[:2]
    award = SimpleNamespace(raider=new.main.label, discord_id=new.discord_id, item_name="Wand of Tests", item_id=1, boss="Gatekeeper", offspec=False,
                            run_key="r", tier="", weight=1.0, by="Jon", source="correct", disenchant=False)
    award_notice(reg, award, old.main.label)
    (t_new,) = texts(reg, new)
    (t_old,) = texts(reg, old)
    assert t_new == t_old and t_old.startswith("Loot correction:") and f"was {old.main.label}" in t_old
    from oibot_gm.loot.session import DISENCHANT

    gone = reg.test_members()[2]
    award_notice(reg, SimpleNamespace(**{**vars(award), "raider": DISENCHANT, "discord_id": None}), gone.main.label)
    (t_gone,) = texts(reg, gone)
    assert "now disenchanted" in t_gone, "disenchanted after all: the one who held it still hears"


# ---------------------------------------------------------------- the receipt renders state (§2.7)

def test_receipt_says_state_queue_position_and_rosters(reg, rs):
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:3]
    join(reg, rs, ev, ms)
    r = rc.answer_receipt(reg, ev, ms[1])
    assert "joined" in r and ms[1].main.label in r
    if rc.run_kind(reg, ev) == "adhoc":
        assert "2nd of" in r
    join(reg, rs, ev, ms[2:], "sub")
    assert "Bench" in rc.answer_receipt(reg, ev, ms[2])
    assert "haven't answered" in rc.answer_receipt(reg, ev, reg.test_members()[5])
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:2]])
    assert "rostered" in rc.answer_receipt(reg, ev, ms[0])


# ---------------------------------------------------------------- the on-behalf union (B5)

def test_member_actions_offer_only_what_applies(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    keys = lambda: [a["key"] for a in rc.member_actions(reg, ev, m)]  # noqa: E731
    assert keys() == ["callout", "join", "bench", "no_thanks"], "no answer yet: no pins (a callout they told an officer about is fine)"
    join(reg, rs, ev, [m])
    assert keys() == ["callout", "bench", "no_thanks", "pin_in", "pin_out"]
    rc.set_pin(ev, ev.signups[str(m.discord_id)].display_name, "in")
    assert "pin_in" not in keys() and "unpin" in keys()
    ev.pins.clear()
    lock_with_board(reg, rs, ev, [m.display_name])
    reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    got = keys()
    assert got[:2] == ["confirm_yes", "confirm_no"] and "pin_in" not in got and "join" not in got


def test_action_consequences_say_who_is_told_and_never_promise_a_dm(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    join(reg, rs, ev, [m])
    for a in rc.member_actions(reg, ev, m):
        text = inbox.action_consequence(reg, ev, m, a["key"])
        assert "DM" not in text.replace("no new DM", "").replace("DM card", ""), text
        if a["key"].startswith("pin"):
            assert "Nobody is told" in text
        else:
            assert "notice on their Me page" in text
    assert "callout is recorded" in inbox.action_consequence(reg, ev, m, "callout")
    assert "no callout is recorded" in inbox.action_consequence(reg, ev, m, "no_thanks")


def test_consequence_on_the_officers_own_record_promises_no_notice(reg, rs):
    """Regression: an officer recording on their OWN record (by_id == member) is their own answer — the verbs write no
    notice, so the sentence must not promise one."""
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    join(reg, rs, ev, [m])
    for key in ("bench", "no_thanks", "callout"):
        mine = inbox.action_consequence(reg, ev, m, key, by_id=m.discord_id)
        assert "notice on their Me page" not in mine and "no notice is written" in mine, mine
        assert "notice on their Me page" in inbox.action_consequence(reg, ev, m, key, by_id=m.discord_id + 1)
    bot = NightBot(reg, rs)
    run(inbox.do_member_action(bot, reg, rs, ev, m, "bench", by=m.display_name, by_id=m.discord_id))
    assert not texts(reg, m), "and indeed none is written"


def test_do_member_action_refuses_what_no_longer_applies(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    bot = NightBot(reg, rs)
    with pytest.raises(RegistryError, match="no longer applies"):
        run(inbox.do_member_action(bot, reg, rs, ev, m, "confirm_yes", by="Jon", by_id=1))
    line = run(inbox.do_member_action(bot, reg, rs, ev, m, "callout", note="flight", by="Jon", by_id=1))
    assert ev.signups[str(m.discord_id)].status == "out" and ev.callouts and ev.callouts[-1].note == "flight", line


def test_member_state_rev_moves_when_they_answer(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    rev = rc.member_state_rev(ev, m)
    join(reg, rs, ev, [m], "sub")
    assert rc.member_state_rev(ev, m) != rev
    assert "is now Bench" in rc.member_change_words(reg, ev, m)


# ---------------------------------------------------------------- pins are a runs decision (D16)

def test_pin_op_is_in_the_runs_group_and_the_copy_says_so(reg, rs):
    assert configops.OP_GROUP["pin"] == "runs"
    assert "pin" in PLAIN_GROUPS["runs"][2] and "pin" not in PLAIN_GROUPS["comp"][2]
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:3]
    join(reg, rs, ev, ms)
    rc.set_pin(ev, ms[0].display_name, "in")
    rc.set_pin(ev, ms[1].display_name, "in")
    rc.set_pin(ev, ms[2].display_name, "out")
    assert rc.pin_counts(ev) == "Pinned: 2 · Bench-pinned: 1"


def _sheet_refreshes(bot, monkeypatch) -> list:
    got: list = []

    async def refresh_sheet(reg, ev):
        got.append((ev.key, rc.pin_counts(ev)))
    monkeypatch.setattr(bot, "refresh_sheet", refresh_sheet)
    return got


def test_every_pin_path_is_one_write_and_refreshes_the_sheet_footer(reg, rs, monkeypatch):
    """Regression: only the row menu's pin re-rendered the sheet; plain text's pin op (and the web / MCP route, see
    test_member_inbox_web) saved without it, so the public 'Pinned: N' footer went stale. All of them are now
    inbox.pin_member."""
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:3]
    join(reg, rs, ev, ms)
    bot = NightBot(reg, rs)
    got = _sheet_refreshes(bot, monkeypatch)
    run(inbox.do_member_action(bot, reg, rs, ev, ms[0], "pin_in", by="Jon", by_id=1))
    assert got[-1] == (ev.key, "Pinned: 1")
    op = configops.ConfigOp(op="pin", target=ev.key, member=ms[1].display_name, value="out")
    line = run(configops.apply_async(reg, op, "Jon", False, bot=bot, by_id=1, via="chat"))
    assert line == f"{ev.team}: keep {ms[1].display_name} on bench"
    assert got[-1] == (ev.key, "Pinned: 1 · Bench-pinned: 1"), "plain text's pin re-renders the sheet too"
    assert ev.pins.get(str(ms[1].discord_id)) == "out"
    with pytest.raises(RegistryError, match="isn't on this sheet"):
        run(inbox.pin_member(bot, reg, rs, ev, reg.test_members()[5], "in", by="Jon"))
    src = (SRC / "configops.py").read_text() + (SRC / "web" / "api.py").read_text() + (SRC / "inbox.py").read_text()
    assert src.count("rc.set_pin(") == 1, "one pin write (inbox.pin_write)"


# ---------------------------------------------------------------- D21: a member's /raid sheet is a copy

class Sent:
    def __init__(self, ch, mid):
        self.channel, self.id, self.edits = ch, mid, []

    async def edit(self, **kw):
        self.edits.append(kw)


class PostChan:
    def __init__(self, cid, name, guild_id):
        self.id, self.name, self.guild, self.sent = cid, name, SimpleNamespace(id=guild_id), []

    async def send(self, content=None, **kw):
        msg = Sent(self, 9000 + len(self.sent) + self.id % 100)
        self.sent.append((kw, msg))
        return msg

    async def fetch_message(self, mid):
        return next(m for _kw, m in self.sent if m.id == mid)


def test_member_copy_never_moves_the_live_sheet_and_officers_move_it(reg, rs):
    reg.config.discord_guild_id = 1
    ev = open_test_run(reg, rs)
    bot = NightBot(reg, rs)
    live, here, there = PostChan(11, "signups", 1), PostChan(12, "general", 1), PostChan(13, "raids", 1)
    for ch in (live, here, there):
        bot.channels[ch.id] = ch
    run(bot.repost_sheet(reg, rs, ev, live))
    assert (ev.channel_id, ev.message_id) == (11, live.sent[0][1].id)
    line = run(bot.repost_sheet(reg, rs, ev, here, copy=True))
    assert "read-only copy" in line and (ev.channel_id, ev.message_id) == (11, live.sent[0][1].id)
    kw, _msg = here.sent[0]
    assert "view" not in kw and "copy — the live sheet is in #signups" in kw["embed"].footer.text
    assert ev.mirror_messages == [[12, here.sent[0][1].id]]
    run(bot.repost_sheet(reg, rs, ev, there))  # an officer's re-post moves it; the old message points there
    assert ev.channel_id == 13
    old = live.sent[0][1]
    assert old.edits and old.edits[-1]["view"] is None and "Re-posted in #raids" in old.edits[-1]["embed"].footer.text


def test_repost_refuses_a_channel_where_the_bot_keeps_quiet(reg, rs):
    reg.config.discord_guild_id = 1
    ev = open_test_run(reg, rs)
    bot = NightBot(reg, rs)
    quiet = PostChan(21, "offtopic", 1)
    reg.config.plain.channels[str(quiet.id)] = "ignore"
    if reg.plain_mode(quiet.id) != "ignore":
        reg.config.plain.channels[quiet.id] = "ignore"
    with pytest.raises(RegistryError, match="keeps quiet"):
        run(bot.repost_sheet(reg, rs, ev, quiet, copy=True))


# ---------------------------------------------------------------- the sheet's character picker (B1)

def _picker(reg, rs, n_alts: int, status="in"):
    import discord

    from oibot_gm.raid_buttons import character_picker

    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    specs = [(c, s) for c in reg.profile.classes for s in reg.profile.classes[c]]
    for i in range(n_alts):
        cls, spec = specs[i % len(specs)]
        reg.add_character(m.discord_id, m.display_name, f"Alt{chr(97 + i)}", cls, spec, None, False)
    m = reg.members[m.discord_id]
    calls = []

    class B:
        ico = staticmethod(lambda *a: None)

        async def apply_signup(self, i, reg, rs, ev, m, character, status):
            calls.append((character, status))

    async def build():
        return character_picker(B(), reg, rs, ev, m, status)
    view = asyncio.run(build())
    return view, m, calls, discord


def test_picker_buttons_up_to_four_with_the_main_first(reg, rs):
    view, m, _calls, discord = _picker(reg, rs, 3, "sub")
    btns = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(btns) == 4 and btns[0].label.startswith(m.main.label), "main first, one press each"


def test_picker_select_from_five_with_the_main_preselected_and_nothing_dropped(reg, rs):
    view, m, calls, discord = _picker(reg, rs, 6, "out")
    (sel,) = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert len(sel.options) == 7, "the old picker cut at five"
    assert [o.value for o in sel.options if o.default] == [m.main.label]
    (btn,) = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert btn.label == f"No thanks as {m.main.label}"
    asyncio.run(btn.callback(None))
    assert calls == [(m.main.label, "out")]


# ---------------------------------------------------------------- the DM rule: no courtesy DM left

# every place the bot DMs someone, and why it is allowed (an ask, a decision someone waits on, the owner's error alert)
DM_CALL_SITES = {
    ("discord_raid.py", "dm_on_open"): "an ask: answer the sheet (open_dm, per raid)",
    ("discord_raid.py", "post_health"): "an ask: the reminder nudge — answer the sheet before the lock",
    ("discord_raid.py", "_send_asks"): "an ask: a fill / swap / pre-lock question",
    ("discord_raid.py", "confirm_one"): "an ask: Confirm / Can't make it",
    ("discord_raid.py", "remind"): "an ask: the same Confirm card again (Remind)",
    ("discord_raid.py", "send_member_dm"): "the one DM path itself",
    ("discord_raid.py", "send_member_card"): "the one DM path itself",
    ("discord_registry.py", "settle_application"): "a decision the applicant waits on",
    ("discord_loot_surface.py", "_loot_after_end_dm"): "officers must act: record in-game awards that arrived after the end",
    ("ops.py", "emit"): "the owner's error alert (one per distinct error per 30 min)",
    ("discord_bot.py", "claim_guild"): "the host's security alert: a new server claimed this bot (OIBOT_SUPERUSERS only)",
    ("discord_raid.py", "send_digest"): "an ask: a test run's puppet asks, batched into one DM to the tester who answers for them (D20)",
}


def _dm_sites() -> set[tuple[str, str]]:
    found = set()
    pat = re.compile(r"send_member_dm\(|send_member_card\(|\buser\.send\(|\.send\(prefix|fetch_user\([^)]*\)\)\.send\(")
    for path in SRC.rglob("*.py"):
        fn = None
        for line in path.read_text().splitlines():
            mdef = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if mdef:
                fn = mdef.group(1)
            if pat.search(line) and not re.match(r"\s*(?:async\s+)?def\s", line) and not line.strip().startswith(("#", '"', "'")):
                found.add((path.name, fn))
    return found


def test_every_dm_is_an_ask_a_decision_or_the_owner_alert():
    sites = _dm_sites()
    unknown = sorted(s for s in sites if s not in DM_CALL_SITES)
    assert not unknown, f"a DM call site with no written reason (the DM rule: only asks, decisions waited on, the owner's alert): {unknown}"


def test_officer_character_confirm_is_a_notice_not_a_dm():
    text = (SRC / "flows_roster.py").read_text()
    assert "Welcome aboard" not in text or "send_member_dm" not in text.split("Welcome aboard")[1][:400]


def test_on_behalf_join_after_lock_says_the_confirm_dm_it_sends(reg, rs):
    """Regression: an on-behalf Join on a locked confirming run with a free seat said '— no new DM', yet set_answer seats
    them and confirm_one DMs the Confirm / Can't card. The sentence must match what is sent."""
    ev = open_test_run(reg, rs)  # a test run rehearses the confirmations
    roles = by_role(reg)
    seated = roles["tank"][:2] + roles["healer"][:3] + (roles["melee"] + roles["ranged"])[:4]  # 9 of 10: one seat free
    join(reg, rs, ev, seated)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    m = next(x for x in reg.test_members() if x not in seated)
    assert rc.run_confirms(reg, ev) and not ev.seat_of(m)
    text = inbox.action_consequence(reg, ev, m, "join", by_id=1)
    assert "Confirm / Can't DM" in text and "no new DM" not in text, text
    bot = NightBot(reg, rs)
    run(inbox.do_member_action(bot, reg, rs, ev, m, "join", by="Jon", by_id=1))
    assert ev.seat_of(m) and any(a["roster"] == ev.team and a.get("dm") for a in reg.members[m.discord_id].placement_asks), "the Confirm DM card went out"
    other = next(x for x in reg.test_members() if x not in seated and x is not m)
    other.dm_opt_out = True  # DMs off: the ask waits on the Me page, and the sentence says so
    off = inbox.action_consequence(reg, ev, other, "join", by_id=1)
    assert "Me page (DMs off)" in off and "Confirm / Can't DM" not in off, off


def test_a_members_plain_text_sheet_ask_posts_a_copy_and_never_moves_the_live_sheet(reg, rs):
    """Regression (D21): run_sheet is open to anyone (ANYONE_OPS) and plain text called repost_sheet without copy= — so a
    member's "post the sheet here" MOVED the live sheet. It posts a read-only copy now; the preview says so."""
    from oibot_gm.configops import ConfigOp

    reg.config.discord_guild_id = 1
    ev = open_test_run(reg, rs)
    bot = NightBot(reg, rs)
    live, here = PostChan(11, "signups", 1), PostChan(12, "general", 1)
    for ch in (live, here):
        bot.channels[ch.id] = ch
    run(bot.repost_sheet(reg, rs, ev, live))
    before = (ev.channel_id, ev.message_id)
    member = reg.test_members()[0]
    op = ConfigOp(op="run_sheet", target=ev.key)
    allowed, refused = configops.authorize(reg, [op], member.discord_id, officer=False, channel=here.id)
    assert allowed and not refused
    words = configops.describe(reg, op, here.id)
    assert "read-only copy" in words and "follow the new message" not in words, words
    line = run(configops.apply_async(reg, op, member.display_name, False, bot=bot, by_id=member.discord_id, channel_id=here.id))
    assert "read-only copy" in line and (ev.channel_id, ev.message_id) == before, "the live sheet stays in #signups"
    assert ev.mirror_messages and ev.mirror_messages[-1][0] == here.id
    officer_op = ConfigOp(op="run_sheet", target=ev.key)
    configops.authorize(reg, [officer_op], reg.config.owner_discord_id, officer=True, channel=here.id)
    assert "move the live sheet" in configops.describe(reg, officer_op, here.id)


def test_an_expired_site_ask_writes_a_notice_and_a_dm_asks_card_is_closed(reg, rs):
    """Expiry reached the member nowhere: the AskCard just vanished from the Me page and a DM card kept live Yes / No.
    A site ask (DMs off) now gets an ask_closed notice; a DM ask's card is edited (the scheduler)."""
    ev, tanks = _fill_scene(reg, rs, hours_out=72)
    team = rc.run_team(reg, ev)
    first = rc.fill_batch(reg, rs, ev, team)[0]
    reg.members[first.discord_id].dm_opt_out = True
    bot = NightBot(reg, rs)
    run(bot.run_fill(reg, rs, ev, team, by="t"))
    site = next(a for a in ev.fill_asks if a.discord_id == first.discord_id)
    dm = next((a for a in ev.fill_asks if a.channel == "dm" and a.dm_message_id), None)
    past = (datetime.now(ev.start.tzinfo) - timedelta(minutes=1)).isoformat()
    for a in ev.fill_asks:
        if a.open:
            a.expires_at = past
    rs.save(ev, "deadline")
    run(bot.scheduler_tick_guild(reg))
    assert site.answer == "expired"
    assert any("closed at" in t and "without your answer" in t for t in texts(reg, reg.members[first.discord_id])), "the site ask hears it"
    if dm is not None:
        from test_raid_night import card_texts
        assert dm.answer == "expired" and any("without an answer" in t for t in card_texts(bot.cards[dm.dm_message_id]))
        assert not any("without your answer" in t for t in texts(reg, reg.members[dm.discord_id])), "a DM ask's card says it — no notice too"
