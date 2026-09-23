"""The /raid wizards (flows_raid.py) driven end to end: /raid open's shortlist and date-time picker (DST included),
/raid set's character pick, and the run-scoped commands, which stay zero-argument on a one-run night and show a run
picker only when several are live. The bot's Discord side is a recording fake; the Registry and RaidStore are real."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import wizard_harness
from conftest import join, lock_with_board, open_test_run
from oibot_gm import discord_registry
from oibot_gm import flows_raid  # noqa: F401 — registers the flows
from oibot_gm import raidcycle as rc
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

PAST = "2026-01-01T00:00"


class FakeMessage:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid
        self.threads = []

    async def create_thread(self, name):
        t = SimpleNamespace(id=self.id + 1, name=name)
        self.threads.append(t)
        return t


class FakeChannel:
    def __init__(self):
        self.id, self.sent = 555, []

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})
        return FakeMessage(self, 7000 + len(self.sent) * 10)


class FakeBot:
    """The client methods the flows call, recording each call; the Registry/RaidStore underneath are real."""

    def __init__(self, rs):
        self.rs = rs
        self.raids = SimpleNamespace(store=lambda _reg: rs)
        self.calls: list[tuple] = []
        self.events: dict = {}
        self.ops = SimpleNamespace(emit=self._emit)
        self.ico = lambda kind, key: ""

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    async def _emit(self, cfg, level, text, *a):
        self.calls.append(("emit", level, text))

    async def open_run_and_post(self, reg, rs, instance, start, by):
        ev = rc.open_run(reg, rs, instance, start, by=by)
        self.calls.append(("open_run_and_post", instance, start, by))
        return ev

    async def post_sheet(self, reg, rs, ev, channel):
        ev.channel_id, ev.message_id = channel.id, 4242
        self.calls.append(("post_sheet", ev.key, channel.id))

    async def set_answer(self, reg, rs, ev, m, status, character, by):
        self.calls.append(("set_answer", ev.key, m.display_name, status, character, by))
        return f"✅ {m.display_name} {status}"

    async def lock_run(self, reg, rs, ev, by):
        self.calls.append(("lock_run", ev.key, by))
        return f"{ev.key}: locked"

    async def cancel_run(self, reg, rs, ev, by, reason):
        self.calls.append(("cancel_run", ev.key, by, reason))
        return f"Cancelled {ev.key}" + (f": {reason}" if reason else "")

    async def run_fill(self, reg, rs, ev, team, by):
        self.calls.append(("run_fill", ev.key, by))
        return [], rc.needs(reg, ev, team)

    async def drop_seated(self, reg, rs, ev, team, m, why, note):
        self.calls.append(("drop_seated", ev.key, m.display_name, why, note))

    async def refresh_sheet(self, reg, ev):
        self.calls.append(("refresh_sheet", ev.key))

    async def post_run_update(self, reg, ev, text):
        self.calls.append(("post_run_update", ev.key, text))

    async def post_boss_tables(self, session, thread):
        self.calls.append(("post_boss_tables", session.id, thread.id))


@pytest.fixture
def channel(monkeypatch):
    """Every fake interaction sits in one channel (the harness's default is none)."""
    ch = FakeChannel()
    orig = wizard_harness.FakeInteraction.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.channel = ch
    monkeypatch.setattr(wizard_harness.FakeInteraction, "__init__", init)
    return ch


@pytest.fixture
def officer(monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)


def harness(reg, rs, user=None):
    bot = FakeBot(rs)
    return Harness(reg, rs, client=bot, user=user), bot


def as_member(h, m):
    h.user.id, h.user.display_name = m.discord_id, m.display_name


def with_slots(reg, rid="barrow_deeps", slots=("Tue 19:30", "Thu 19:30"), first_open=PAST):
    reg.set_raid_overrides(rid, {"slots": list(slots), "first_open": first_open}, "t")


# ---------------------------------------------------------------- /raid open

def test_open_next_slot_is_one_press_after_the_raid(reg, rs, officer, channel):
    with_slots(reg)
    h, bot = harness(reg, rs)
    want = rc.slot_starts(reg, "barrow_deeps", reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)[0][1]

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        assert h.selects() == ["Pick the raid"]
        await h.pick("Pick the raid", "barrow_deeps")
        label = f"Open the next slot — {reg.local12(want)}"
        assert label in h.buttons() and "Or another upcoming run" in h.selects()
        assert "19:30" not in h.text and "19:30" not in label
        await h.press(label)
        assert f"<t:{int(want.timestamp())}:f>" in h.text and reg.local12(want) in h.text
        assert "signup channel" in h.text  # none is set: say where the sheet will go
        assert not bot.calls  # nothing before Confirm
        await h.press("Open")
    asyncio.run(go())
    assert bot.calls[0][:3] == ("open_run_and_post", "barrow_deeps", want)
    assert ("post_sheet", bot.calls[1][1], channel.id) == bot.calls[1]
    assert "✅ Opened" in h.text and "posted in this channel" in h.text


def test_open_another_upcoming_run_from_the_select(reg, rs, officer, channel):
    with_slots(reg)
    h, bot = harness(reg, rs)
    starts = [t for _s, t in rc.slot_starts(reg, "barrow_deeps", reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)]

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        sel = next(s for s in h._items() if getattr(s, "placeholder", None) == "Or another upcoming run")
        assert [o.value for o in sel.options] == [t.isoformat() for t in starts[1:23]]
        assert all(":" in o.label and ("AM" in o.label or "PM" in o.label) for o in sel.options)
        await h.pick("Or another upcoming run", starts[2].isoformat())
        await h.press("Open")
    asyncio.run(go())
    assert bot.calls[0][2] == starts[2]


def test_open_existing_run_is_named_on_confirm(reg, rs, officer, channel):
    with_slots(reg)
    h, bot = harness(reg, rs)
    first = rc.slot_starts(reg, "barrow_deeps", reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)[0][1]
    rc.open_run(reg, rs, "barrow_deeps", first, by="t")

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press(f"Open the next slot — {reg.local12(first)}")
        assert "already exists" in h.text
    asyncio.run(go())


def test_open_dst_gap_shows_the_note_and_opens_3_30(reg, rs, officer, channel, monkeypatch):
    fake_now = datetime(2026, 3, 1, 12, 0, tzinfo=reg.tz)
    monkeypatch.setattr(reg, "now_local", lambda: fake_now)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        assert "no run times yet" in h.text  # the profile has none: the one-off path
        await h.press("Another day and time…")
        form = h.modal
        assert [o.value for o in form._items["day"].options][7] == "2026-03-08"
        await h.submit(form, day="2026-03-08", hour="2", minute="30")
        assert "doesn't exist" in h.text and "3:30 AM" in h.text
        await h.press("Open")
    asyncio.run(go())
    start = bot.calls[0][2]
    assert (start.year, start.month, start.day, start.hour, start.minute) == (2026, 3, 8, 3, 30)
    assert start.utcoffset() == timedelta(hours=-7)


def test_open_no_slots_says_why_and_offers_run_times(reg, rs, officer, channel, monkeypatch):
    monkeypatch.delitem(FLOWS, "raid_config", raising=False)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        assert "no run times yet" in h.text
        assert h.buttons() == ["Set this raid's run times →", "Another day and time…", "Cancel"]
        await h.press("Set this raid's run times →")
        assert "Raids page" in h.text
    asyncio.run(go())
    assert not bot.calls


def test_open_slots_before_first_open_say_so_and_jump_to_run_times(reg, rs, officer, channel, monkeypatch):
    with_slots(reg, first_open=(reg.now_local() + timedelta(days=60)).replace(microsecond=0).isoformat())
    seen = {}

    async def raid_config(interaction, reg_, **seed):
        seen.update(seed)
    monkeypatch.setitem(FLOWS, "raid_config", raid_config)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        assert "before its first lockout opens" in h.text
        await h.press("Set this raid's run times →")
    asyncio.run(go())
    assert seen == {"raid": "barrow_deeps", "section": "times"} and not bot.calls


def test_open_refuses_the_past_and_cancel_writes_nothing(reg, rs, officer, channel):
    h, bot = harness(reg, rs)
    today = reg.now_local().date().isoformat()
    runs = len(rs.events)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press("Another day and time…")
        await h.submit(day=today, hour="0", minute="0")  # midnight today: already gone
        assert "already passed" in h.text and "Pick again" in h.buttons()
        await h.press("Pick again")
        assert h.modal is not None
        tomorrow = (reg.now_local().date() + timedelta(days=1)).isoformat()
        await h.submit(day=tomorrow, hour="20", minute="0")
        assert "8:00 PM" in h.text
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    asyncio.run(go())
    assert not bot.calls and len(rs.events) == runs


def test_open_is_officer_only(reg, rs, channel, monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: False)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_open"](h.command(), reg))
    assert h.text == "Officers only." and not bot.calls


def test_open_later_than_the_window_narrows_by_week(reg, rs, officer, channel):
    h, bot = harness(reg, rs)
    got = {}

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press("Another day and time…")
        await h.submit(day="later", hour="19", minute="30")
        week = got["week"] = h._items()[0].options[2].value
        await h.pick("Pick the week", week)
        assert h.modal._items["day"].options[0].value == week
        await h.submit(h.modal, day=week, hour="19", minute="30")
        await h.press("Open")
    asyncio.run(go())
    start = bot.calls[0][2]
    assert start.date().isoformat() == got["week"] and (start.hour, start.minute) == (19, 30)


# ---------------------------------------------------------------- /raid set

def test_set_picks_run_and_character_then_calls_set_answer(reg, rs, officer, channel):
    a = open_test_run(reg, rs, days=3)
    b = open_test_run(reg, rs, "hyjal_summit_forever", days=5)
    m = reg.test_members()[0]
    cls = next(iter(reg.profile.classes))
    spec = next(iter(reg.profile.classes[cls]))
    reg.add_character(m.discord_id, m.display_name, "Altonar", cls, spec, None, False)
    h, bot = harness(reg, rs)
    user = SimpleNamespace(id=m.discord_id, display_name=m.display_name)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=user, status="in")
        assert h.selects() == ["Pick the run"]
        await h.pick("Pick the run", b.team)
        assert h.selects() == ["Pick the character"]
        await h.pick("Pick the character", "Altonar")
        assert "Altonar" in h.text and "Join" in h.text
        assert not bot.calls
        await h.press("Confirm")
    asyncio.run(go())
    assert bot.calls == [("set_answer", b.key, m.display_name, "in", "Altonar", "Tester")]
    assert a.key != b.key


def test_set_one_run_one_character_goes_straight_to_confirm_and_cancel_writes_nothing(reg, rs, officer, channel):
    open_test_run(reg, rs)
    m = reg.test_members()[1]
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=SimpleNamespace(id=m.discord_id, display_name=m.display_name), status="out")
        assert h.buttons() == ["Confirm", "Cancel"] and "No thanks" in h.text
        await h.press("Cancel")
    asyncio.run(go())
    assert not bot.calls


def test_set_refuses_when_the_run_closed(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[2]
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=SimpleNamespace(id=m.discord_id, display_name=m.display_name), status="in")
        ev.state = "cancelled"
        rs.save(ev, "cancelled")
        await h.press("Confirm")
        assert "closed" in h.text
    asyncio.run(go())
    assert not bot.calls


def test_set_unregistered_member(reg, rs, officer, channel):
    open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_set"](h.command(), reg, member=SimpleNamespace(id=1, display_name="Nobody"), status="in"))
    assert "isn't registered" in h.text and not bot.calls


# ---------------------------------------------------------------- run-scoped commands

def test_lock_with_one_run_needs_no_picker(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_lock"](h.command(), reg))
    assert bot.calls[0] == ("lock_run", ev.key, "Tester") and bot.calls[1][0] == "emit"
    assert h.text.endswith(f"{ev.key}: locked") and len([s for s in h.screens if s["view"] is not None]) == 0


def test_lock_with_two_runs_shows_the_picker_and_cancel_does_nothing(reg, rs, officer, channel):
    open_test_run(reg, rs, days=3)
    b = open_test_run(reg, rs, "hyjal_summit_forever", days=5)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_lock"](h.command(), reg)
        assert h.selects() == ["Pick the run"] and len(h._items()[0].options) == 2
        await h.press("Cancel")
        assert not bot.calls
        h2, bot2 = harness(reg, rs)
        await FLOWS["raid_lock"](h2.command(), reg)
        await h2.pick("Pick the run", b.team)
        assert bot2.calls[0] == ("lock_run", b.key, "Tester")
    asyncio.run(go())


def test_lock_no_open_sheet(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    lock_with_board(reg, rs, ev, [m.display_name for m in reg.test_members()[:5]])
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_lock"](h.command(), reg))
    assert "No open sheet to lock." in h.text and not bot.calls


def test_run_commands_are_officer_only(reg, rs, channel, monkeypatch):
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: False)
    open_test_run(reg, rs)
    for name in ("raid_lock", "raid_loot", "raid_cancel", "raid_fill"):
        h, bot = harness(reg, rs)
        asyncio.run(FLOWS[name](h.command(), reg))
        assert h.text == "Officers only." and not bot.calls, name


def test_sheet_reposts_in_the_channel(reg, rs, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_sheet"](h.command(), reg))
    assert channel.sent and channel.sent[0]["embed"] is not None
    assert rs.events[ev.key].channel_id == channel.id and rs.events[ev.key].message_id == 7010
    assert "Re-posted" in h.text


def test_sheet_with_no_live_run(reg, rs, channel):
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_sheet"](h.command(), reg))
    assert h.text.endswith("No live sheet.") and not channel.sent


def test_health_from_the_command_and_from_the_picker(reg, rs, officer, channel):
    open_test_run(reg, rs, days=3)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_health"](h.command(), reg))
    assert h.screen["view"] is not None and h.text == ""  # the layout card itself
    b = open_test_run(reg, rs, "hyjal_summit_forever", days=5)

    async def go():
        h2, _ = harness(reg, rs)
        await FLOWS["raid_health"](h2.command(), reg)
        await h2.pick("Pick the run", b.team)
        assert "below" in h2.screens[-2]["text"] and h2.screen["view"] is not None
    asyncio.run(go())


def test_loot_opens_a_thread_on_a_locked_run(reg, rs, officer, channel, monkeypatch):
    from oibot_gm import discord_bot

    saved = []
    monkeypatch.setattr(discord_bot.MockEvent, "save", lambda self, msg=None: saved.append(msg))
    ev = open_test_run(reg, rs)
    names = [m.display_name for m in reg.test_members()[:10]]
    join(reg, rs, ev, reg.test_members()[:10])
    lock_with_board(reg, rs, ev, names)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_loot"](h.command(), reg))
    thread_id = 7011  # the loot embed is the first message in the channel (7010); its thread is +1
    assert channel.sent[0]["embed"] is not None
    assert rs.events[ev.key].thread_id == thread_id and thread_id in bot.events
    assert ("post_boss_tables", ev.key, thread_id) in bot.calls and saved
    assert f"<#{thread_id}>" in h.text


def test_loot_needs_a_locked_roster(reg, rs, officer, channel):
    open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_loot"](h.command(), reg))
    assert "Lock the roster first" in h.text and not channel.sent and not bot.calls


def test_cancel_takes_a_reason_in_a_modal(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_cancel"](h.command(), reg)
        assert h.modal is not None and not bot.calls  # one run: the box opens straight away
        await h.submit(reason="tank flu")
    asyncio.run(go())
    assert bot.calls == [("cancel_run", ev.key, "Tester", "tank flu")]
    assert channel.sent[-1]["content"] == f"Cancelled {ev.key}: tank flu" and "tank flu" in h.text


def test_cancel_dismissed_box_writes_nothing_and_a_closed_run_is_refused(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_cancel"](h.command(), reg)  # the box opens; closing it sends nothing back
        assert h.modal is not None and not bot.calls
        form = h.modal
        ev.state = "done"
        rs.save(ev, "done")
        await h.submit(form)
        assert "already closed" in h.text
    asyncio.run(go())
    assert not bot.calls


def test_cancel_with_two_runs_picks_then_opens_the_box(reg, rs, officer, channel):
    open_test_run(reg, rs, days=3)
    b = open_test_run(reg, rs, "hyjal_summit_forever", days=5)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_cancel"](h.command(), reg)
        await h.pick("Pick the run", b.team)
        assert h.modal is not None
        await h.submit(reason="")
    asyncio.run(go())
    assert bot.calls == [("cancel_run", b.key, "Tester", None)]


def test_out_is_one_step_with_one_run(reg, rs, channel):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    join(reg, rs, ev, [m])
    h, bot = harness(reg, rs)
    as_member(h, m)

    async def go():
        await FLOWS["raid_out"](h.command(), reg)
        assert h.modal is not None
        await h.submit(note="work")
    asyncio.run(go())
    assert rs.events[ev.key].signups[str(m.discord_id)].status == "out"
    assert "Noted: out for" in h.text
    assert bot.names() == ["refresh_sheet", "post_run_update", "emit"] and "work" in bot.calls[-1][2]


def test_out_after_lock_frees_the_seat(reg, rs, channel):
    ev = open_test_run(reg, rs)
    members = reg.test_members()[:5]
    join(reg, rs, ev, members)
    lock_with_board(reg, rs, ev, [m.display_name for m in members])
    h, bot = harness(reg, rs)
    as_member(h, members[0])

    async def go():
        await FLOWS["raid_out"](h.command(), reg)
        await h.submit()
    asyncio.run(go())
    assert bot.calls == [("drop_seated", ev.key, members[0].display_name, "callout", None)]


def test_out_prefers_the_run_you_joined_and_refuses_strangers(reg, rs, channel):
    open_test_run(reg, rs, days=3)
    b = open_test_run(reg, rs, "hyjal_summit_forever", days=5)
    m = reg.test_members()[3]
    join(reg, rs, b, [m])
    h, bot = harness(reg, rs)
    as_member(h, m)

    async def go():
        await FLOWS["raid_out"](h.command(), reg)
        assert h.modal is not None and "Hyjal" in h.modal.title  # the one they joined: no picker
    asyncio.run(go())
    h2, bot2 = harness(reg, rs)  # not registered
    asyncio.run(FLOWS["raid_out"](h2.command(), reg))
    assert "not registered" in h2.text and not bot2.calls


def test_fill_preview_and_send(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    members = reg.test_members()[:5]
    join(reg, rs, ev, members)
    lock_with_board(reg, rs, ev, [m.display_name for m in members])
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_fill"](h.command(), reg, preview=True))
    assert "Would ask next" in h.text and not bot.calls
    asyncio.run(FLOWS["raid_fill"](h.command(), reg))
    assert bot.names() == ["run_fill", "emit"] and bot.calls[0][1] == ev.key


def test_fill_before_lock(reg, rs, officer, channel):
    open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_fill"](h.command(), reg))
    assert "Fill works after lock" in h.text and not bot.calls
