"""Chat/MCP parity, stage A1: the shared verbs every surface calls (RaidMixin.save_board / board_move / call_out /
answer_fill_for / repost_sheet, open_run_and_post's past-start refusal, the test-run guard in set_answer, per-run DM
routing), the registry and raidcycle helpers (merge_plain_policy, shadow previews, test_answer_mix, the change ring,
may_plain for the owner) and presets. The bot here is RaidMixin + AbsencesMixin on a recording fake of the Discord
side; the Registry and RaidStore underneath are real (conftest)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import OWNER, join, lock_with_board, open_test_run
from test_mcp import FakeBot

from oibot_gm import presets
from oibot_gm import raidcycle as rc
from oibot_gm import registry as registry_mod
from oibot_gm.discord_pool import AbsencesMixin
from oibot_gm.discord_raid import RaidMixin
from oibot_gm.registry import RegistryError

REAL_UID = 500_000_000_000_000_001
TESTER_A, TESTER_B = 500_000_000_000_000_010, 500_000_000_000_000_011


class Chan:
    def __init__(self, cid: int, name: str = "raids"):
        self.id, self.name, self.sent = cid, name, []

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})
        return SimpleNamespace(id=9000 + len(self.sent), channel=self)

    async def fetch_message(self, mid):
        raise LookupError("not in this fake")


class User:
    def __init__(self, bot, uid):
        self.bot, self.id = bot, uid

    async def send(self, content=None, **kw):
        self.bot.dms.setdefault(self.id, []).append(content if content is not None else kw.get("view"))


class Bot(RaidMixin, AbsencesMixin, FakeBot):
    """The real RaidMixin / AbsencesMixin verbs over FakeBot's registries/ops; channels and DMs are recorded."""

    def __init__(self, reg, rs):
        FakeBot.__init__(self, reg, rs)
        self.channels: dict[int, Chan] = {}
        self.dms: dict[int, list] = {}
        self.ico = lambda *a, **k: ""
        self.card_refreshes = 0

    def get_channel(self, cid):
        return self.channels.get(int(cid)) if cid else None

    async def fetch_user(self, uid):
        return User(self, int(uid))

    async def refresh_registration_card(self, reg):
        self.card_refreshes += 1


def run(coro):
    return asyncio.run(coro)


def real_member(reg, uid=REAL_UID, name="Realperson"):
    reg.add_character(uid, name, name, "Warrior", next(iter(reg.profile.classes["Warrior"])), None, True)
    return reg.members[uid]


# ---------------------------------------------------------------- set_answer: a test run takes puppets + its tester only

def test_set_answer_refuses_a_real_member_on_a_test_run(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs, test_by=REAL_UID + 1)
    m = real_member(reg)
    with pytest.raises(RegistryError, match="test run"):
        run(bot.set_answer(reg, rs, ev, m, "in", None, by="Officer"))
    assert str(m.discord_id) not in ev.signups
    with pytest.raises(RegistryError, match="test run"):  # the callout path has the same guard
        run(bot.call_out(reg, rs, ev, m, "busy", by="Officer"))
    puppet = reg.test_members()[0]
    assert "Join" in run(bot.set_answer(reg, rs, ev, puppet, "in", None, by="Officer"))
    tester = real_member(reg, REAL_UID + 1, "Testerperson")
    run(bot.set_answer(reg, rs, ev, tester, "in", None, by=tester.display_name))
    assert ev.signups[str(tester.discord_id)].status == "in"


def test_a_real_run_still_takes_real_members(reg, rs):
    bot = Bot(reg, rs)
    reg.set_raid_overrides("barrow_deeps", {"slots": "Tue 19:30"}, "t")
    start = (reg.now_local() + timedelta(days=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    m = real_member(reg)
    run(bot.set_answer(reg, rs, ev, m, "in", None, by=m.display_name))
    assert ev.signups[str(m.discord_id)].status == "in"


# ---------------------------------------------------------------- open_run_and_post refuses the past

def test_open_refuses_a_start_in_the_past(reg, rs):
    bot = Bot(reg, rs)
    past = reg.now_local() - timedelta(hours=2)
    with pytest.raises(RegistryError, match="in the past") as e:
        run(bot.open_run_and_post(reg, rs, "barrow_deeps", past, "Officer"))
    assert "PM" in str(e.value) or "AM" in str(e.value)  # 12-hour, never 24-hour
    assert not rs.events
    rc.refuse_past_start(reg, reg.now_local() - timedelta(minutes=3))  # inside the grace: fine
    soon = (reg.now_local() + timedelta(hours=5)).replace(second=0, microsecond=0)
    ev = run(bot.open_run_and_post(reg, rs, "barrow_deeps", soon, "Officer"))
    assert ev.key in rs.events and any("opened" in t for _, t in bot.ops.lines)


def test_the_comp_sandbox_still_opens(reg, rs):
    ev = rc.open_comp_sandbox(reg, rs, "barrow_deeps", "t", OWNER)
    assert ev.start > reg.now_local() and len(ev.signups) == len(reg.test_members())


# ---------------------------------------------------------------- DMs about a run go to THAT run's tester

def test_puppet_dms_follow_the_runs_own_tester(reg, rs):
    """A puppet's DM about a run reaches THAT run's tester — as one digest per batch (phase 2, B9), never one DM each."""
    from oibot_gm.raid_views import run_title

    bot = Bot(reg, rs)
    a = open_test_run(reg, rs, days=2, test_by=TESTER_A)
    b = open_test_run(reg, rs, days=4, test_by=TESTER_B)
    puppet = reg.test_members()[0]

    async def go():
        assert await bot.send_member_dm(reg, puppet, "about b", ev=b)
        assert await bot.send_member_dm(reg, puppet, "about a", ev=a)
        assert await bot.send_member_dm(reg, puppet, "no run")  # no run: the first test run's tester, as before
        assert not bot.dms, "nothing sent per puppet: the digest waits for the batch to end"
        assert await bot.flush_puppet_digests() == 3
    run(go())
    assert run_title(reg, b) in str(bot.dms[TESTER_B][0]) and len(bot.dms[TESTER_B]) == 1
    assert run_title(reg, a) in str(bot.dms[TESTER_A][0]) and all(run_title(reg, b) not in str(x) for x in bot.dms[TESTER_A])
    assert puppet.display_name in str(bot.dms[TESTER_A][-1]) and len(bot.dms[TESTER_A]) == 2


# ---------------------------------------------------------------- the board verbs

def test_save_board_and_move_share_the_lock_and_the_rev(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]
    seen = rc.board_rev(reg, ev)
    line = run(bot.save_board(reg, rs, ev, [names[:3], names[3:]], rev=seen, by="Officer"))
    assert "board saved" in line and ev.layout == [names[:3], names[3:]]
    with pytest.raises(rc.BoardChanged, match="changed since you looked"):
        run(bot.save_board(reg, rs, ev, [names], rev=seen, by="Other"))
    assert ev.layout == [names[:3], names[3:]], "a stale whole-board write changes nothing"
    run(bot.board_move(reg, rs, ev, names[0], None, by="Officer"))  # to the bank
    assert names[0] not in [n for g in ev.layout for n in g]
    run(bot.board_move(reg, rs, ev, names[0], (1, 0), by="Officer"))
    assert ev.layout[1][0] == names[0]
    with pytest.raises(ValueError, match="hasn't joined"):
        run(bot.board_move(reg, rs, ev, "Nobody", None, by="Officer"))
    assert "cleared" in run(bot.save_board(reg, rs, ev, [], by="Officer")) and ev.layout is None


def test_after_lock_the_board_is_the_roster(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]
    lock_with_board(reg, rs, ev, names[:5])
    line = run(bot.save_board(reg, rs, ev, [names[:4] + [names[5]]], by="Officer"))
    assert f"asked {names[5]} to confirm" in line and f"freed {names[4]}" in line
    assert ev.seat_of(names[5]) and not ev.seat_of(names[4])


# ---------------------------------------------------------------- call_out

def test_call_out_before_lock_keeps_the_note_for_officers(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    m = reg.test_members()[2]
    join(reg, rs, ev, [m])
    line = run(bot.call_out(reg, rs, ev, m, "work trip", by="Officer"))
    assert m.display_name in line and "noted for the officers" in line and "work trip" not in line
    assert ev.signups[str(m.discord_id)].status == "out" and ev.callouts[-1].note == "work trip"
    assert any("work trip" in t for _, t in bot.ops.lines)  # the ops feed (officers) carries the note
    mine = run(bot.call_out(reg, rs, ev, m, None, by=m.display_name, by_id=m.discord_id))
    assert mine.startswith("Noted: out for")


def test_call_out_after_lock_frees_the_seat(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:5]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    line = run(bot.call_out(reg, rs, ev, ms[0], "sick", by="Officer"))
    assert line.endswith("seat freed") and not ev.seat_of(ms[0].display_name)
    assert ev.callouts[-1].note == "sick"


# ---------------------------------------------------------------- answer_fill_for

def fill_ask(reg, ev, m, kind="sub"):
    a = rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind=kind, role=reg.profile.spec(m.main.cls, m.main.spec).role,
                   character=m.main.label, spec=m.main.spec, reason="short 1")
    ev.fill_asks.append(a)
    return a


def test_answer_fill_for_is_the_buttons_verb(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:6]
    join(reg, rs, ev, ms[:5])
    lock_with_board(reg, rs, ev, [m.display_name for m in ms[:4]])
    ask = fill_ask(reg, ev, ms[5])
    with pytest.raises(RegistryError, match="no open fill ask"):
        run(bot.answer_fill_for(reg, rs, ev, ms[0], True, by="Officer"))
    line = run(bot.answer_fill_for(reg, rs, ev, ms[5], True, by="Officer"))
    assert ms[5].display_name in line and ask.answer == "yes" and ev.seat_of(ms[5].display_name)
    with pytest.raises(RegistryError):  # answered: nothing waiting any more
        run(bot.answer_fill_for(reg, rs, ev, ms[5], False, by="Officer"))


# ---------------------------------------------------------------- repost_sheet

def test_repost_sheet_moves_the_sheet(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs)
    ch = bot.channels[77] = Chan(77, "raid-signups")
    line = run(bot.repost_sheet(reg, rs, ev, ch))
    assert "#raid-signups" in line and ev.channel_id == 77 and ev.message_id and ch.sent[0]["embed"] is not None


# ---------------------------------------------------------------- announce_absence says which sheets it touched

def test_announce_absence_returns_the_sheets(reg, rs):
    bot = Bot(reg, rs)
    ev = open_test_run(reg, rs, days=3)
    m = reg.test_members()[1]
    day = ev.start.astimezone(reg.tz).date().isoformat()
    m, a = reg.add_absence(m.discord_id, day, day, "wedding", "t")
    assert run(bot.announce_absence(reg, m, a, "t")) == [ev.key]
    assert ev.signups[str(m.discord_id)].status == "out"
    line = next(t for _, t in bot.ops.lines if "absent" in t)
    assert f"absent {reg.day_label(day)} — wedding" in line, "people read days, never ISO"


# ---------------------------------------------------------------- registry helpers

def test_may_plain_lets_the_owner_act_in_ignore_and_answer_channels(reg):
    reg.config.plain.channels = {"42": "ignore", "43": "answer"}
    for ch in (42, 43):
        assert reg.may_plain("runs", OWNER, officer=True, channel=ch) is None
        assert "only answers questions" in reg.may_plain("runs", REAL_UID, officer=True, channel=ch)


def test_merge_plain_policy(reg):
    reg.config.plain.channels = {"1": "act", "2": "answer"}
    m = reg.merge_plain_policy({"set_channels": {"<#2>": "ignore", "1": None, "3": "self"}, "groups": {"runs": ["everyone"]}})
    assert m["channels"] == {"2": "ignore", "3": "self"} and m["groups"]["runs"] == ["everyone"] and m["dm"] == reg.config.plain.dm
    whole = reg.merge_plain_policy({"channels": {"9": "act"}, "dm": "answer"})
    assert whole["channels"] == {"9": "act"} and whole["dm"] == "answer"
    line = reg.set_plain_policy(m, "Owner")
    assert "runs" not in line or "everyone" in line
    assert reg.config.plain.channels == {"2": "ignore", "3": "self"}


def test_registry_changes_are_kept_in_a_ring(reg):
    registry_mod.RECENT_CHANGES.clear()
    reg.save_config("about text changed (test)")
    rows = registry_mod.recent_changes(reg.key)
    assert rows[0]["text"] == "about text changed (test)" and rows[0]["kind"] == "config"
    assert registry_mod.recent_changes("other-guild") == []


def test_shadow_previews_never_touch_the_live_config(reg):
    head = reg.store.head()
    before = reg.raid_def("barrow_deeps")["slots"]
    rows = rc.next_runs_preview(reg, "barrow_deeps", fields={"slots": "Sat 20:00", "first_open": "2020-01-01T00:00"})
    assert rows and all("Sat" in label and "8:00 PM" in label for label in rows[0]["labels"])
    assert reg.raid_def("barrow_deeps")["slots"] == before and reg.store.head() == head
    with pytest.raises(RegistryError):  # the save's own validation: signups must open before they lock
        rc.next_runs_preview(reg, "barrow_deeps", fields={"signup_lead_hours": "1", "lock_hours_before": "5"})
    lines = rc.next_runs_lines(reg, "barrow_deeps", fields={"slots": "Sat 20:00", "first_open": "2020-01-01T00:00"})
    assert lines[0].startswith(reg.raid_def("barrow_deeps")["name"]) and "20:00" not in lines[0]


def test_timezone_preview(reg):
    reg.set_raid_overrides("barrow_deeps", {"slots": "Tue 19:30", "first_open": "2020-01-01T00:00"}, "t")
    lines = rc.timezone_preview(reg, "America/New_York")
    line = next(x for x in lines if x.startswith(reg.raid_def("barrow_deeps")["name"]))
    assert "7:30 PM America/New_York" in line and "4:30 PM" in line and reg.config.timezone == "America/Los_Angeles"
    with pytest.raises(RegistryError, match="unknown timezone"):
        rc.timezone_preview(reg, "Mars/Olympus")


def test_test_answer_mix(reg, rs):
    ev = open_test_run(reg, rs)
    done = rc.test_answer_mix(reg, rs, ev, join=3, bench=2, out=1)
    assert len(done) == 6 and len(ev.by_status("in")) == 3 and len(ev.by_status("sub")) == 2 and len(ev.by_status("out")) == 1
    rest = rc.test_answer_mix(reg, rs, ev, join=99)
    assert len(rest) == len(reg.test_members()) - 6, "capped by the puppets who haven't answered"
    with pytest.raises(RegistryError):
        rc.test_answer_mix(reg, rs, ev, join=-1)


def test_presets_are_shared(reg):
    from oibot_gm import flows_config, flows_misc

    assert flows_config.PRESETS is presets.PRESETS and flows_misc.TEMPOS is presets.TEMPOS
    f = presets.preset_fields("short")
    assert f == {"signup_lead_hours": "48", "nudge_hours_before": "36", "lock_hours_before": "24", "confirm_hours_before": "12"}
    reg.set_raid_overrides("barrow_deeps", f, "t")  # legal by construction
    start_in, cut = presets.tempo_cutoffs("quick", test_by=7)
    assert start_in == 20 and cut["hard"] == 12 / 60 and cut["test_by"] == 7
    with pytest.raises(ValueError):
        presets.preset_fields("weekly")
