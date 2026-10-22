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
from oibot_gm.discord_loot import LootMixin
from oibot_gm.discord_raid import RaidMixin
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


class FakeBot(LootMixin):
    """The client methods the flows call, recording each call; the Registry/RaidStore underneath are real. call_out is
    RaidMixin's own (the shared verb /raid out, the site and MCP use), so its ripple shows up as recorded calls; the loot
    verbs are LootMixin's own (open_loot keys the session (guild, run) and puts its thread in the officer channel)."""

    call_out = RaidMixin.call_out
    repost_sheet = RaidMixin.repost_sheet
    _live_sheet_name = RaidMixin._live_sheet_name
    MIRRORS_KEPT = RaidMixin.MIRRORS_KEPT
    release_asks_of = RaidMixin.release_asks_of  # call_out's own steps: a pending ask closes, the cards follow
    refill_after_release = RaidMixin.refill_after_release
    refresh_cards = RaidMixin.refresh_cards

    def __init__(self, rs):
        self.rs = rs
        self.raids = SimpleNamespace(store=lambda _reg: rs)
        self.calls: list[tuple] = []
        self.events: dict = {}
        self.ops = SimpleNamespace(emit=self._emit)
        self.ico = lambda kind, key: ""
        self.registries = SimpleNamespace(by_key={})

    def get_channel(self, cid):
        return SimpleNamespace(id=cid)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    async def _emit(self, cfg, level, text, *a):
        self.calls.append(("emit", level, text))

    async def open_run_and_post(self, reg, rs, instance, start, by, schedule=None):
        ev = rc.open_run(reg, rs, instance, start, by=by, schedule=schedule)
        self.calls.append(("open_run_and_post", instance, start, by) + ((schedule,) if schedule else ()))
        return ev

    async def post_sheet(self, reg, rs, ev, channel):
        ev.channel_id, ev.message_id = channel.id, 4242
        self.calls.append(("post_sheet", ev.key, channel.id))

    async def set_answer(self, reg, rs, ev, m, status, character, by, by_id=None):
        self.calls.append(("set_answer", ev.key, m.display_name, status, character, by))
        return f"✅ {m.display_name} {status}"

    async def lock_run(self, reg, rs, ev, by):
        self.calls.append(("lock_run", ev.key, by))
        return f"{ev.key}: locked"

    async def cancel_run(self, reg, rs, ev, by, reason, notify=True, by_id=None, via="discord"):
        self.calls.append(("cancel_run", ev.key, by, reason) + (() if notify else ("quiet",)))
        return f"Cancelled {ev.key}" + (f": {reason}" if reason else "")

    async def run_fill(self, reg, rs, ev, team, by, approved_by=None):
        self.calls.append(("run_fill", ev.key, by))
        return [], rc.needs(reg, ev, team)

    async def send_plan(self, reg, rs, ev, *, rev, by, picks=None):
        """RaidMixin.send_plan's contract (refused on a stale plan fingerprint), recorded instead of DMing."""
        plan = rc.role_plan(reg, rs, ev)
        if rev != plan.rev:
            raise rc.PlanChanged(plan)
        asks, plan.held_back = rc.send_batch(plan, picks)
        self.calls.append(("send_plan", ev.key, by, [a.display_name for a in asks]))
        return asks, plan

    async def drop_seated(self, reg, rs, ev, team, m, why, note):
        self.calls.append(("drop_seated", ev.key, m.display_name, why, note))

    async def refresh_sheet(self, reg, ev):
        self.calls.append(("refresh_sheet", ev.key))

    async def post_run_update(self, reg, ev, text):
        self.calls.append(("post_run_update", ev.key, text))

    def officer_channel(self, reg, ev=None):
        return self.__dict__.get("officer_ch")


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
        label = f"Next run — {reg.local12(want)}"
        assert h.buttons() == [label, "Another upcoming run…", "Ad hoc run…", "Cancel"]
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
        await h.press("Another upcoming run…")
        sel = next(s for s in h._items() if getattr(s, "placeholder", None) == "Pick the run")
        assert [o.value for o in sel.options] == [t.isoformat() for t in starts[1:23]]
        assert all(":" in o.label and ("AM" in o.label or "PM" in o.label) for o in sel.options)
        await h.pick("Pick the run", starts[2].isoformat())
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
        await h.press(f"Next run — {reg.local12(first)}")
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
        await h.press("Ad hoc run…")  # no pickup template: straight to the day · hour · minute form
        form = h.modal
        assert [o.value for o in form._items["day"].options][7] == "2026-03-08"
        await h.submit(form, day="2026-03-08", hour="2", minute="30")
        assert "doesn't exist" in h.text and "3:30 AM" in h.text
        await h.press("Open")
    asyncio.run(go())
    start = bot.calls[0][2]
    assert (start.year, start.month, start.day, start.hour, start.minute) == (2026, 3, 8, 3, 30)
    assert start.utcoffset() == timedelta(hours=-7)
    assert bot.calls[0][4] == "adhoc", "a one-off is ad hoc, never a run of the raid's own run times"


def test_open_no_slots_says_why_and_offers_run_times(reg, rs, officer, channel, monkeypatch):
    monkeypatch.delitem(FLOWS, "raid_config", raising=False)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", "barrow_deeps")
        assert "no run times yet" in h.text
        assert h.buttons() == ["Set this raid's run times →", "Ad hoc run…", "Cancel"]
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
        await h.press("Ad hoc run…")
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
        await h.press("Ad hoc run…")
        await h.submit(day="later", hour="19", minute="30")
        week = got["week"] = h._items()[0].options[2].value
        await h.pick("Pick the week", week)
        assert h.modal._items["day"].options[0].value == week
        await h.submit(h.modal, day=week, hour="19", minute="30")
        await h.press("Open")
    asyncio.run(go())
    start = bot.calls[0][2]
    assert start.date().isoformat() == got["week"] and (start.hour, start.minute) == (19, 30)


# ---------------------------------------------------------------- /raid set: anything a member can do, for them (B5)

def _user(m):
    return SimpleNamespace(id=m.discord_id, display_name=m.display_name)


REAL = 620_000_000_000_000_000  # real (non-puppet) members: `/raid set` leaves test runs to `/gm test answer` (design B5)


def open_real_run(reg, rs, instance="barrow_deeps", days=3):
    """A real run (no test cutoffs): its side of the bench is the real members."""
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, instance, start, by="t")


def reals(reg, n, start=0):
    out = []
    for k in range(start, start + n):
        uid = REAL + k
        if uid not in reg.members:
            reg.add_character(uid, f"Real{k}", "Real" + "".join(chr(97 + int(d)) for d in f"{k:02d}"), "Priest", "Holy", None, True)
        out.append(reg.members[uid])
    return out

def test_set_picks_run_then_only_what_applies_then_the_character(reg, rs, officer, channel):
    a = open_real_run(reg, rs, days=3)
    b = open_real_run(reg, rs, "hyjal_summit_forever", days=5)
    m = reals(reg, 1)[0]
    cls = next(iter(reg.profile.classes))
    spec = next(iter(reg.profile.classes[cls]))
    reg.add_character(m.discord_id, m.display_name, "Altonar", cls, spec, None, False)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=_user(m))
        assert h.selects() == ["Pick the run"]
        await h.pick("Pick the run", b.key)
        assert h.buttons() == ["Record a callout…", "Join", "Bench", "No thanks", "Cancel"], "no answer yet: no Confirm, no pins"
        assert "no answer yet" in h.text
        await h.press("Join")
        assert h.selects() == ["Pick the character"]
        await h.pick("Pick the character", "Altonar")
        assert "Altonar" in h.text and "notice on their Me page" in h.text and "no new DM" in h.text
        assert h.buttons() == ["Join", "Back"]
        assert not bot.calls
        await h.press("Join")
    asyncio.run(go())
    assert bot.calls == [("set_answer", b.key, m.display_name, "in", "Altonar", "Tester")]
    assert a.key != b.key


def test_set_with_what_goes_straight_to_the_confirm_screen_and_back_writes_nothing(reg, rs, officer, channel):
    open_real_run(reg, rs)
    m = reals(reg, 1, 1)[0]
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=_user(m), what="no_thanks")
        assert h.buttons() == ["No thanks", "Back"] and "No thanks" in h.text and "no callout is recorded" in h.text
        await h.press("Back")
        assert "Nothing was changed" in h.text
    asyncio.run(go())
    assert not bot.calls


def test_set_refuses_when_the_run_closed(reg, rs, officer, channel):
    ev = open_real_run(reg, rs)
    m = reals(reg, 1, 2)[0]
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join")
        ev.state = "cancelled"
        rs.save(ev, "cancelled")
        await h.press("Join")
        assert "closed" in h.text
    asyncio.run(go())
    assert not bot.calls


def test_set_unregistered_member(reg, rs, officer, channel):
    open_real_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_set"](h.command(), reg, member=SimpleNamespace(id=1, display_name="Nobody")))
    assert "aren't registered" in h.text and not bot.calls


def test_set_rereads_the_member_at_confirm(reg, rs, officer, channel):
    ev = open_real_run(reg, rs)
    m = reals(reg, 1, 3)[0]
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=_user(m), what="join")
        join(reg, rs, ev, [m], "sub")  # the member presses Bench while the officer's wizard is open
        await h.press("Join")
        assert "is now Bench" in h.text and "keep going?" in h.text
        assert not bot.calls
        await h.press("Keep going")
    asyncio.run(go())
    assert bot.calls and bot.calls[0][:4] == ("set_answer", ev.key, m.display_name, "in")


def test_set_records_a_callout_with_a_note_before_lock(reg, rs, officer, channel):
    ev = open_real_run(reg, rs)
    m = reals(reg, 1, 4)[0]
    join(reg, rs, ev, [m])
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=_user(m), what="callout")
        assert h.buttons() == ["Record the callout", "Record the callout with a note…", "Back"]
        assert "No thanks" in h.text and "callout is recorded" in h.text
        await h.press("Record the callout with a note…")
        await h.submit(note="flight delayed")
    asyncio.run(go())
    assert ev.signups[str(m.discord_id)].status == "out"
    assert ev.callouts and ev.callouts[-1].note == "flight delayed"


def test_set_on_a_locked_run_offers_their_confirm_ask_first(reg, rs, officer, channel):
    ev = open_real_run(reg, rs)
    pool = reals(reg, 10)
    join(reg, rs, ev, pool)
    lock_with_board(reg, rs, ev, [m.display_name for m in pool])
    m = pool[0]
    reg.add_placement_ask(m.discord_id, ev.team, m.main.label, "lock")
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_set"](h.command(), reg, member=_user(m))
        assert h.buttons()[:3] == ["Confirm for them", "Can't make it for them", "Record a callout…"]
        assert "Pin to roster" not in h.buttons(), "pins are for an open sheet"
    asyncio.run(go())


def test_set_with_a_what_that_does_not_apply_says_so(reg, rs, officer, channel):
    open_real_run(reg, rs)
    m = reals(reg, 1, 5)[0]
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_set"](h.command(), reg, member=_user(m), what="confirm_yes"))
    assert "Nothing to set" in h.text and "Confirm for them" in h.text and not bot.calls


def test_set_leaves_test_runs_to_gm_test_answer(reg, rs, officer, channel):
    """Design B5: `/raid set` refuses a test run — a puppet on one is answered with `/gm test answer` (the Test bench)."""
    open_test_run(reg, rs)
    m = reg.test_members()[0]
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_set"](h.command(), reg, member=_user(m)))
    assert "test run" in h.text and "/gm test answer" in h.text and not bot.calls
    real = open_real_run(reg, rs, days=5)
    r = reals(reg, 1, 7)[0]
    h2, bot2 = harness(reg, rs)

    async def go():  # a real run alongside: only it is offered (one run → straight to the verbs)
        await FLOWS["raid_set"](h2.command(), reg, member=_user(r))
        assert "Pick the run" not in h2.selects() and "Join" in h2.buttons()
    asyncio.run(go())
    assert real.key in [e.key for e in rs.live()]


# ---------------------------------------------------------------- run-scoped commands

def test_lock_with_one_run_needs_no_picker(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)

    async def go():
        # THE lock confirm screen (phase 2): the consequence from code, Lock now · Preview lock · Back — nothing before the press
        await FLOWS["raid_lock"](h.command(), reg)
        assert h.buttons() == ["Lock now", "Preview lock", "Back"] and "The board becomes the roster." in h.text and not bot.calls
        await h.press("Lock now")
        assert bot.calls[0] == ("lock_run", ev.key, "Tester") and bot.calls[1][0] == "emit"
        assert h.text.endswith(f"{ev.key}: locked")
    asyncio.run(go())


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
        assert not bot2.calls and "Lock now" in h2.buttons()
        await h2.press("Lock now")
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


def test_sheet_officer_moves_the_live_sheet_here(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_sheet"](h.command(), reg))
    assert channel.sent and channel.sent[0]["embed"] is not None
    assert rs.events[ev.key].channel_id == channel.id and rs.events[ev.key].message_id == 7010
    assert "Re-posted" in h.text


def test_sheet_member_posts_a_read_only_copy_that_never_moves_the_live_one(reg, rs, channel):
    """D21: a member's /raid sheet is a labelled copy — no buttons, the live sheet stays where it is."""
    ev = open_test_run(reg, rs)
    ev.channel_id, ev.message_id = 4444, 4545
    rs.save(ev, "t: posted")
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_sheet"](h.command(), reg))
    (sent,) = channel.sent
    assert "view" not in sent and "copy — the live sheet is in" in sent["embed"].footer.text
    assert (rs.events[ev.key].channel_id, rs.events[ev.key].message_id) == (4444, 4545)
    assert rs.events[ev.key].mirror_messages == [[channel.id, 7010]] and "read-only copy" in h.text


def test_sheet_member_channel_choice_is_not_followed_and_the_reply_says_so(reg, rs, channel):
    """A member's `channel:` is never followed (only officers choose where a sheet goes) — and never silently: the reply
    names the channel they asked for."""
    ev = open_test_run(reg, rs)
    ev.channel_id, ev.message_id = 4444, 4545
    rs.save(ev, "t: posted")
    other = FakeChannel()
    other.id, other.name = 556, "general"
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_sheet"](h.command(), reg, channel=other))
    assert len(channel.sent) == 1 and not other.sent, "the copy is posted here"
    assert "Only officers choose where a sheet goes" in h.text and "#general" in h.text
    h2, _ = harness(reg, rs)
    asyncio.run(FLOWS["raid_sheet"](h2.command(), reg))
    assert "Only officers" not in h2.text, "no channel asked for: nothing to explain"


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
    from oibot_gm.loot.session import LootSession

    saved = []
    monkeypatch.setattr(LootSession, "save", lambda self, msg=None: saved.append(msg))
    ev = open_test_run(reg, rs)
    names = [m.display_name for m in reg.test_members()[:10]]
    join(reg, rs, ev, reg.test_members()[:10])
    lock_with_board(reg, rs, ev, names)
    h, bot = harness(reg, rs)
    reg.config.roster_channel_id = channel.id
    bot.officer_ch = channel  # the council's thread goes to the officer channel (D1), never "where the command ran"
    asyncio.run(FLOWS["raid_loot"](h.command(), reg))
    thread_id = 7011  # the loot embed is the first message in the channel (7010); its thread is +1
    assert channel.sent[0]["embed"] is not None
    session = bot.events[(reg.key, ev.key)]  # keyed (guild, run), the file events/<run key>.json
    assert rs.events[ev.key].thread_id == thread_id and session.raid_thread_id == thread_id and bot.event_for(thread_id) is session
    assert saved
    assert f"<#{thread_id}>" in h.text


def test_loot_on_a_split_run_opens_one_council_per_roster_and_reposts_when_open(reg, rs, officer, channel):
    """§3.1: a split run gets one session per roster ("loot · Roster 2"), each judged on its own seats; /raid loot on
    an open council re-posts what still waits (never just "already open")."""
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    names = [m.display_name for m in ms]
    size = rc.run_size(reg, ev)
    ev.rosters = rc.board_rosters(reg, ev, [names[:5]], size) + rc.board_rosters(reg, ev, [names[5:]], size)
    ev.state, ev.locked_at = "locked", rc.now()
    rs.save(ev, "split")
    h, bot = harness(reg, rs)
    reg.config.roster_channel_id = channel.id
    bot.officer_ch = channel
    asyncio.run(FLOWS["raid_loot"](h.command(), reg))
    s1, s2 = bot.events[(reg.key, f"{ev.key}-r1")], bot.events[(reg.key, f"{ev.key}-r2")]
    assert "Roster 1: Loot thread open" in h.text and "Roster 2: Loot thread open" in h.text
    assert s1.raid_thread_id and s2.raid_thread_id and s1.raid_thread_id != s2.raid_thread_id
    reposted = []

    async def loot_repost(reg_, session):
        reposted.append(session.id)
        return 0
    bot.loot_repost = loot_repost
    h2 = Harness(reg, rs, client=bot)
    asyncio.run(FLOWS["raid_loot"](h2.command(), reg))
    assert reposted == [s1.id, s2.id] and "re-posted" in h2.text


def test_loot_refuses_without_an_officer_channel(reg, rs, officer, channel):
    """D1: no roster or ops channel → refused, nothing opened (never a public thread wherever the command ran)."""
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[:10])
    lock_with_board(reg, rs, ev, [m.display_name for m in reg.test_members()[:10]])
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_loot"](h.command(), reg))
    assert "Set a roster or ops channel first" in h.text and not channel.sent and not bot.events


def test_loot_needs_a_locked_roster(reg, rs, officer, channel):
    open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_loot"](h.command(), reg))
    assert "Lock the roster first" in h.text and not channel.sent and not bot.calls


def test_cancel_shows_the_consequence_then_one_form_with_reason_and_notify(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_cancel"](h.command(), reg)
        # THE cancel screen (phase 2): the consequence from code, then one Form — nothing written before Submit
        assert h.buttons() == ["Cancel the run…", "Back"] and "Nobody is DMed" in h.text and not bot.calls
        await h.press("Cancel the run…")
        assert h.modal is not None and set(h.modal._items) == {"reason", "notify"}
        await h.submit(reason="tank flu", notify="post")
    asyncio.run(go())
    assert bot.calls == [("cancel_run", ev.key, "Tester", "tank flu")]
    assert not channel.sent, "the reason is never echoed in the channel the command ran in"
    assert h.text.endswith(f"Cancelled {ev.key}: tank flu")


def test_cancel_dismissed_box_writes_nothing_and_a_closed_run_is_refused(reg, rs, officer, channel):
    ev = open_test_run(reg, rs)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_cancel"](h.command(), reg)
        await h.press("Cancel the run…")  # the box opens; closing it sends nothing back
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
        await h.press("Cancel the run…")
        assert h.modal is not None
        await h.submit(reason="", notify="quiet")
    asyncio.run(go())
    assert bot.calls == [("cancel_run", b.key, "Tester", None, "quiet")]


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
    assert "A Send would DM:" in h.text and not bot.calls
    with pytest.raises(AssertionError, match="no buttons"):  # preview: the screen is final, no Send button
        h._items()
    asyncio.run(FLOWS["raid_fill"](h.command(), reg))
    assert "Send DMs exactly these:" in h.text and not bot.calls, "after lock /raid fill shows the batch first: nothing sent"
    shown, _held = rc.send_batch(rc.role_plan(reg, rs, ev))
    asyncio.run(h.press(next(b.label for b in h._items() if getattr(b, "label", "").startswith("Send"))))
    assert bot.calls[0] == ("send_plan", ev.key, "Tester", [a.display_name for a in shown]), "Send sends the batch shown"


def test_fill_before_lock_shows_suggestions(reg, rs, officer, channel):
    """Phase 2: /raid fill on an open run is suggest → Send; nothing goes out before Send."""
    open_test_run(reg, rs)
    h, bot = harness(reg, rs)
    asyncio.run(FLOWS["raid_fill"](h.command(), reg))
    assert "before lock" in h.text and not bot.calls


def test_handing_off_to_run_times_leaves_no_open_screen(reg, rs, officer, channel, monkeypatch):
    """Regression (G2 review): to_run_times stopped the wizard without finish(), so its pending_views entry stayed for
    the life of the process and GET /api/agents/quiet never read quiet again — every profile restart waited forever."""
    from oibot_gm import pending_views

    monkeypatch.setattr(pending_views, "_live", {})
    monkeypatch.setattr(pending_views, "track_interaction",
                        lambda key, interaction, title="": pending_views.track(key, app_id=1, token="t", title=title))
    with_slots(reg, first_open=(reg.now_local() + timedelta(days=60)).replace(microsecond=0).isoformat())

    async def raid_config(interaction, reg_, **seed):
        pass
    monkeypatch.setitem(FLOWS, "raid_config", raid_config)
    h, bot = harness(reg, rs)

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        assert pending_views.live_count() == 1
        await h.pick("Pick the raid", "barrow_deeps")
        await h.press("Set this raid's run times →")
    asyncio.run(go())
    assert pending_views.live_count() == 0
