"""No event line is posted loose in a channel. The officers' lines (RaidMixin.post_run_update: a pause, a cancel with
its reason, a failed lock …) go to the event's updates thread under its officer card, fetched from the channel the
cards are really in (`ev.cards_channel_id`); one thread for every raid of the event; an archived thread is fetched and
used, never made twice; no card or no thread → the ops feed (officers only), never the signup channel. The members'
lines (the "is locked" line, the cancel line) go to the event's thread on its signup sheet (`sheet_thread_id` on the
first raid), and Unlock still deletes the lock line there. RaidMixin.officer_channel is never the public sheet channel
(a guild whose roster channel IS the signup channel posts its officer cards in the ops channel)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import open_test_run
from test_follow_on import open_pair
from test_lock_as_it_stands import _pair_with_lead_board, ten_with_board
from test_raid_night import NightBot, run

from oibot_gm import raidcycle as rc
from oibot_gm.discord_raid import RaidMixin

SIGNUP, OPS, ROSTER = 61001, 61002, 61003


class Msg:
    def __init__(self, channel, mid: int, content=None):
        self.channel, self.id, self.content = channel, mid, content
        self.thread = None
        self.flags = SimpleNamespace(components_v2=False)

    async def create_thread(self, name: str):
        if self.thread is not None:
            raise RuntimeError("a thread has already been created for this message")
        self.thread = Thread(self.channel.bot, self.id, name, self.channel)
        self.channel.bot.rooms[self.id] = self.thread
        return self.thread

    async def edit(self, **kw):
        pass

    async def delete(self):
        self.channel.msgs.pop(self.id, None)
        self.channel.deleted.append(self.id)


class Room:
    """A text channel: `sent` is everything posted loose in it; messages can be fetched, threaded and deleted."""

    def __init__(self, bot, cid: int, name: str):
        self.bot, self.id, self.name = bot, cid, name
        self.sent: list[dict] = []
        self.msgs: dict[int, Msg] = {}
        self.deleted: list[int] = []

    async def send(self, content=None, **kw):
        self.bot.next_id += 1
        msg = Msg(self, self.bot.next_id, content)
        self.msgs[msg.id] = msg
        self.sent.append({"content": content, **kw})
        return msg

    async def fetch_message(self, mid):
        if int(mid) not in self.msgs:
            raise LookupError("Unknown Message")
        return self.msgs[int(mid)]


class Thread(Room):
    """A thread started from a message: its id is the message's (as on Discord). An archived one is only fetched."""

    def __init__(self, bot, tid: int, name: str, parent: Room):
        super().__init__(bot, tid, name)
        self.parent, self.parent_id, self.archived = parent, parent.id, False

    def lines(self) -> list[str]:
        return [str(c["content"]) for c in self.sent]


class ThreadBot(NightBot):
    """NightBot with the real post_run_update and officer_channel, over channels that hold messages and threads."""

    post_run_update = RaidMixin.post_run_update
    officer_channel = RaidMixin.officer_channel

    def __init__(self, reg, rs, *, roster_is_signup: bool = True):
        super().__init__(reg, rs)
        self.next_id = 70_000
        self.rooms: dict[int, Room] = {}
        for cid, name in ((SIGNUP, "signups"), (OPS, "officers"), (ROSTER, "roster")):
            self.rooms[cid] = Room(self, cid, name)
        self.signup = self.rooms[SIGNUP]
        self.updates = []  # unused here: the real post_run_update runs
        reg.config.signup_channel_id, reg.config.ops_channel_id = SIGNUP, OPS
        reg.config.roster_channel_id = SIGNUP if roster_is_signup else ROSTER

    def get_channel(self, cid):
        ch = self.rooms.get(int(cid)) if cid else None
        return None if getattr(ch, "archived", False) else ch  # an archived thread is not in the cache

    async def fetch_channel(self, cid):
        if int(cid) not in self.rooms:
            raise LookupError("Unknown Channel")
        return self.rooms[int(cid)]

    def threads(self) -> list[Thread]:
        return [r for r in self.rooms.values() if isinstance(r, Thread)]

    def loose(self, cid: int) -> list[str]:
        return [str(c.get("content")) for c in self.rooms[cid].sent if c.get("content") is not None]


def post_sheet(bot, reg, rs, ev):
    run(bot.post_sheet(reg, rs, ev, bot.signup))
    return rs.events[ev.key]


def card_in(bot, rs, ev, cid: int, key: str = "lock:0"):
    """An officer card already up in channel `cid` (the live guild's: posted while the officers' channel was ops)."""
    room = bot.rooms[cid]
    bot.next_id += 1
    msg = Msg(room, bot.next_id)
    room.msgs[msg.id] = msg
    ev.cards[key], ev.cards_channel_id = msg.id, cid
    rs.save(ev, "card (test)")
    return msg


# ---------------------------------------------------------------- the officers' channel is never the sheet's

def test_a_roster_channel_that_is_the_signup_channel_is_not_the_officers_channel(reg, rs):
    bot = ThreadBot(reg, rs)
    assert bot.officer_channel(reg, None).id == OPS
    reg.config.ops_channel_id = None
    assert bot.officer_channel(reg, None) is None, "nowhere for officers beats the public channel"
    bot2 = ThreadBot(reg, rs, roster_is_signup=False)
    assert bot2.officer_channel(reg, None).id == ROSTER


def test_the_lock_posts_its_cards_in_ops_when_the_roster_channel_is_the_signup_channel(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    run(bot.lock_run(reg, rs, ev, by="Jon", fill=False))
    ev = rs.events[ev.key]
    assert ev.cards_channel_id == OPS and "lock:0" in ev.cards


# ---------------------------------------------------------------- the owner's guild: officer lines go to the card's thread

@pytest.mark.parametrize("roster_is_signup", [True, False])
def test_pause_cancel_and_a_failed_lock_land_in_the_cards_thread_never_in_the_signup_channel(reg, rs, roster_is_signup):
    bot = ThreadBot(reg, rs, roster_is_signup=roster_is_signup)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    card = card_in(bot, rs, ev, OPS, "health")
    ev.layout = [[], []]  # nobody on the board, people answered: a lock as it stands fails
    rs.save(ev, "board emptied (test)")
    run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False))
    run(bot.set_autofill_paused(reg, rs, rs.events[ev.key], True, by="Kara"))
    run(bot.cancel_run(reg, rs, rs.events[ev.key], "Kara", "a private reason"))
    ev = rs.events[ev.key]
    assert card.thread is not None and ev.updates_thread_id == card.thread.id
    said = card.thread.lines()
    assert any("lock by Jon failed" in x for x in said), said
    assert any(x.startswith("⏸️ Automated Fill Requests paused by Kara") for x in said), said
    assert any("cancelled by Kara: a private reason" in x for x in said), said
    loose = bot.loose(SIGNUP)
    assert not loose, f"nothing loose in the signup channel: {loose}"
    assert not any("a private reason" in x for t in bot.threads() if t.parent_id == SIGNUP for x in t.lines()), "the reason never reaches members"


def test_no_card_sends_the_line_to_the_ops_feed_only(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    run(bot.set_autofill_paused(reg, rs, ev, True, by="Kara"))
    assert not bot.loose(SIGNUP) and not bot.loose(OPS) and not bot.threads()
    assert any("Automated Fill Requests paused by Kara" in t for _lvl, t in bot.ops.lines)


def test_a_deleted_card_sends_the_line_to_the_ops_feed(reg, rs, caplog):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev.cards, ev.cards_channel_id = {"lock:0": 123}, OPS  # the card was deleted by hand
    rs.save(ev, "old card (test)")
    run(bot.post_run_update(reg, ev, "🔔 a line"))
    assert not bot.threads() and not bot.loose(OPS) and not bot.loose(SIGNUP)
    assert any("🔔 a line" in t for _lvl, t in bot.ops.lines)
    assert ev.key in caplog.text, "the failure is logged with the event"


def test_an_archived_thread_is_fetched_and_used_not_made_again(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    card = card_in(bot, rs, ev, OPS)
    run(bot.post_run_update(reg, ev, "one"))
    thread = card.thread
    thread.archived = True
    run(bot.post_run_update(reg, rs.events[ev.key], "two"))
    assert thread.lines() == ["one", "two"] and len(bot.threads()) == 1


def test_an_old_event_whose_card_has_a_thread_but_no_stored_id_reuses_it(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    card = card_in(bot, rs, ev, OPS)
    run(card.create_thread(name="old"))
    run(bot.post_run_update(reg, ev, "line"))
    assert card.thread.lines() == ["line"] and rs.events[ev.key].updates_thread_id == card.thread.id


def test_a_lineup_uses_one_thread(reg, rs):
    bot = ThreadBot(reg, rs)
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    lcard, fcard = card_in(bot, rs, lead, OPS), card_in(bot, rs, fev, OPS)
    run(bot.post_run_update(reg, rs.events[fev.key], "later raid first"))
    run(bot.post_run_update(reg, rs.events[lead.key], "first raid"))
    lname, fname = reg.raid_def(lead.instance)["name"], reg.raid_def(fev.instance)["name"]  # one thread: each line names its raid
    assert fcard.thread is None and lcard.thread.lines() == [f"{fname}: later raid first", f"{lname}: first raid"]
    assert rs.events[lead.key].updates_thread_id == lcard.thread.id


def test_a_test_event_keeps_its_cards_in_its_channel_and_threads_there(reg, rs):
    bot = ThreadBot(reg, rs)
    ev = open_test_run(reg, rs)
    reg.config.team(ev.team)["test_channel"] = SIGNUP  # started from the signup channel itself
    assert bot.officer_channel(reg, ev).id == SIGNUP, "D20: a rehearsal stays where it was asked for"
    run(bot.post_run_update(reg, ev, "no card yet"))
    assert not bot.ops.lines and not bot.loose(SIGNUP), "no thread: the test feed only, never the ops feed"
    card = card_in(bot, rs, ev, SIGNUP)
    run(bot.post_run_update(reg, ev, "step"))
    assert card.thread.lines() == ["step"] and not bot.loose(SIGNUP)


# ---------------------------------------------------------------- the members' lines: the sheet's thread

def test_the_lock_line_goes_to_the_sheets_thread_and_unlock_deletes_it(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    sheet = bot.signup.msgs[ev.message_id]
    run(bot.lock_run(reg, rs, ev, by="Jon", fill=False))
    ev = rs.events[ev.key]
    assert len(bot.loose(SIGNUP)) == 0, "only the sheet (an embed) is in the channel"
    assert sheet.thread is not None and ev.sheet_thread_id == sheet.thread.id
    assert any("is locked: 10 rostered" in x for x in sheet.thread.lines())
    tid, mid = ev.lock_line
    assert tid == sheet.thread.id and mid in sheet.thread.msgs
    run(bot.unlock_run(reg, rs, ev, "Jon", by_id=1))
    assert sheet.thread.deleted == [mid] and rs.events[ev.key].lock_line is None
    run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False))
    assert len(bot.threads()) == 2 and sum("is locked" in x for x in sheet.thread.lines()) == 2, "a relock reuses the sheet's thread"


def test_the_cancel_line_goes_to_the_sheets_thread_without_the_reason(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    sheet = bot.signup.msgs[ev.message_id]
    line = run(bot.cancel_run(reg, rs, ev, "Kara", "a private reason"))
    assert not bot.loose(SIGNUP)
    assert sheet.thread is not None and len(sheet.thread.lines()) == 1
    assert sheet.thread.lines()[0].startswith("🛑 ") and "is cancelled." in sheet.thread.lines()[0] and "private" not in sheet.thread.lines()[0]
    assert "signup channel" not in line


def test_a_sheet_gone_posts_no_line_anywhere(reg, rs):
    bot = ThreadBot(reg, rs)
    ev, _ms = ten_with_board(reg, rs, placed=10)
    ev = post_sheet(bot, reg, rs, ev)
    bot.signup.msgs.clear()  # the sheet was deleted by hand
    run(bot.lock_run(reg, rs, ev, by="Jon", fill=False))
    ev = rs.events[ev.key]
    assert ev.state == "locked" and ev.lock_line is None and not bot.loose(SIGNUP)
    assert not any(t.parent_id == SIGNUP for t in bot.threads())


def test_a_lineups_lock_lines_share_the_sheets_thread(reg, rs):
    bot = ThreadBot(reg, rs)
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    lead = post_sheet(bot, reg, rs, lead)
    fev = rs.events[fev.key]
    fev.channel_id = SIGNUP  # a later raid carrying a channel posts its own line
    fev.layout = [[m for m in lead.layout[0][:3]]]
    rs.save(fev, "test")
    run(bot.lock_run(reg, rs, lead, by="Jon", fill=False))
    sheet = bot.signup.msgs[lead.message_id]
    said = [x for x in sheet.thread.lines() if "is locked" in x]
    assert len(said) == 2 and not bot.loose(SIGNUP) and len([t for t in bot.threads() if t.parent_id == SIGNUP]) == 1
    assert rs.events[fev.key].lock_line[0] == sheet.thread.id
