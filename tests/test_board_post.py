"""Post board (design 2026-11-27-post-board): an officer posts the event's board to a Discord channel — one message per
raid, in lineup order, each raid's groups with the spec each person plays in THAT raid, nobody unplaced listed — and the
bot keeps those SAME messages current as the event changes (a move, Auto-fill groups, Clear, a per-raid switch, a lock,
an unlock, an officer's edit after the lock, a lineup edit), edits them one last time when the event ends or is
cancelled, moves them when the event's Board posts channel changes (new posted first, a failure keeps the old, a restart
heals), posts again a message deleted by hand, and takes them down on Remove board post. Board posts is a fourth place of
the event channels: event → schedule → guild → the event's signup sheet channel. Nobody is DMed, told or mentioned; the
first post, a move and a removal write one log and ops line, a live update none."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from conftest import first_slot
from test_event_channels import ALT_SHEET, ChanBot, _failing, live, sched
from test_event_threads import SIGNUP, Msg, Room
from test_per_raid_switch import BD as BD_RAID, HYJAL, evening
from test_raid_night import run

from oibot_gm import consequences as cq
from oibot_gm import event_board as eb
from oibot_gm import raidcycle as rc
from oibot_gm.board_post import BOARD_NONE, BOARD_OVER
from oibot_gm.raid_views import BOARD_NOBODY
from oibot_gm.registry import RegistryError
from oibot_gm.roster import solver

BOARD, ALT_BOARD = 63001, 63002


def http_error(cls, status: int, text: str = "x"):
    """A discord.py HTTP error as Discord raises it (NotFound 404, Forbidden 403, HTTPException 5xx)."""
    return cls(SimpleNamespace(status=status, reason=text), text)


class Partial:
    """discord.PartialMessage: edit / delete by id, no fetch. A message gone → NotFound; `room.edit_error` → that error
    (a refused or failing edit); `room.gate` (an asyncio.Event) holds every edit until it is set."""

    def __init__(self, room, mid: int):
        self.room, self.id = room, int(mid)

    async def edit(self, **kw):
        if self.room.gate is not None:
            await self.room.gate.wait()
        if self.room.edit_error is not None:
            raise self.room.edit_error
        if self.id not in self.room.msgs:
            raise http_error(discord.NotFound, 404, "Unknown Message")
        await self.room.msgs[self.id].edit(**kw)

    async def delete(self):
        if self.id not in self.room.msgs:
            raise http_error(discord.NotFound, 404, "Unknown Message")
        await self.room.msgs[self.id].delete()


class BRoom(Room):
    """A channel whose messages keep the embed they show (posted, then each edit). Like the "board" permission kind
    (View, Send, Embed), the bot can't read its history: fetch_message is Forbidden; edits and deletes go through
    get_partial_message."""

    edit_error = None
    gate = None
    history = False

    async def send(self, content=None, **kw):
        msg = await super().send(content, **kw)
        msg.embed, msg.edits = kw.get("embed"), []
        self.bot.seq.append(("send", self.id))
        if "view" not in kw:  # a board post (a sheet in a sheet channel carries its buttons)
            assert kw.get("allowed_mentions") is not None and not kw["allowed_mentions"].everyone, "never a mention"
        return msg

    async def fetch_message(self, mid):
        if not self.history:
            raise http_error(discord.Forbidden, 403, "Missing Access")
        return await super().fetch_message(mid)

    def get_partial_message(self, mid):
        return Partial(self, mid)


def live_msgs(bot, cid=BOARD) -> int:
    return len(bot.rooms[cid].msgs) if cid in bot.rooms else 0


@pytest.fixture(autouse=True)
def recorded(monkeypatch):
    """Msg.edit keeps the embed it was given; Msg.delete is put in the bot's sequence; the board listener goes after."""
    async def edit(self, **kw):
        self.__dict__.setdefault("edits", []).append(kw)
        if "embed" in kw:
            self.embed = kw["embed"]

    real_delete = Msg.delete

    async def delete(self):
        self.channel.bot.__dict__.setdefault("seq", []).append(("delete", self.channel.id))
        await real_delete(self)

    monkeypatch.setattr(Msg, "edit", edit)
    monkeypatch.setattr(Msg, "delete", delete)
    yield
    rc.RUN_LISTENERS[:] = [f for f in rc.RUN_LISTENERS if getattr(f, "__name__", "") != "board_run_saved"]


@pytest.fixture(autouse=True)
def _opened(reg):
    for rid in reg.profile.raids:
        reg.set_raid_overrides(rid, {"first_open": "2026-01-01T00:00"}, "t")


class BoardBot(ChanBot):
    BOARD_DEBOUNCE_S = 0.0

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.seq: list = []
        for cid, name in ((BOARD, "board"), (ALT_BOARD, "alt-board")):
            self.rooms[cid] = BRoom(self, cid, name)
        self.ico = lambda kind, key="", *a, **k: f"[{key}]" if kind == "spec" else ""
        reg.config.board_channel_id = BOARD


def shown(msg) -> str:
    e = msg.embed
    return "\n".join([e.title or "", e.description or ""] + [f"{f.name}: {f.value}" for f in e.fields])


def ids(rs, ev) -> dict:
    return {p.key: list(p.board_posts) for p in rc.event_parts(rs, rc.first_part(rs, ev))}


def texts(bot, rs, ev, cid=BOARD) -> list[str]:
    return [shown(bot.rooms[cid].msgs[m]) for ms in ids(rs, ev).values() for m in ms]


def edits(bot, cid=BOARD) -> int:
    return sum(len(getattr(m, "edits", [])) for m in bot.rooms[cid].msgs.values())


def char(ev, m) -> str:
    return ev.signups[str(m.discord_id)].character


def one_raid(reg, rs, bot, **chans):
    """A Barrow Deeps event (two groups of five), ten joined, eight placed: five in group 1, three in group 2."""
    ev, ms = live(reg, rs, bot, **chans)
    ev.layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:8]]]
    rs.save(ev, "board (test)")
    return rs.events[ev.key], ms


def scenario(bot, work):
    """`work()` with the board listener installed on this loop, then every redraw it scheduled."""
    async def go():
        bot.install_board_posts()
        out = await work()
        await bot.board_settle()
        return out
    return run(go())


# ---------------------------------------------------------------- where it goes: event → schedule → guild → signup sheet

def test_board_posts_inherit_event_then_schedule_then_guild_then_the_signup_sheets_channel(reg, rs):
    reg.config.signup_channel_id, reg.config.board_channel_id = SIGNUP, None
    sid = sched(reg)
    start = first_slot(reg, BD_RAID)
    ev = rc.open_run(reg, rs, BD_RAID, start, by="t", schedule=sid)
    assert rc.channel_of(reg, ev, "board_channel") == SIGNUP, "no guild setting: the event's signup sheet channel"
    rc.reschedule(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara")
    assert rc.channel_of(reg, ev, "board_channel") == ALT_SHEET, "the sheet's own channel, not the guild's"
    reg.config.board_channel_id = BOARD
    assert rc.channel_of(reg, ev, "board_channel") == BOARD, "the guild's"
    reg.set_schedule(sid, {"board_channel": str(ALT_BOARD)}, "t")
    later = rc.open_run(reg, rs, BD_RAID, start + timedelta(days=7), by="t", schedule=sid)
    assert rc.event_channel(reg, later, "board_channel") == ALT_BOARD, "the schedule's, stamped at open"
    rc.reschedule(reg, rs, ev, timing={"board_channel": str(ALT_BOARD)}, by="Kara")
    assert rc.channel_of(reg, ev, "board_channel") == ALT_BOARD, "the event's own"


# ---------------------------------------------------------------- the first post

def test_the_first_post_is_one_message_per_raid_in_lineup_order_with_each_raids_own_spec(reg, rs):
    bot = BoardBot(reg, rs)
    lead, fev, ms = evening(reg, rs)
    nyx, mages = ms[0], ms[9:]
    placed = [m.display_name for m in ms[:19]]  # everyone but the last mage
    lead.layout = [placed[i:i + 5] for i in range(0, 20, 5)]
    rs.save(lead, "board (test)")

    async def work():
        return await bot.post_board(reg, rs, lead, by="Kara")
    said = scenario(bot, work)
    assert "posted in #board" in said
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert [len(v) for v in ids(rs, lead).values()] == [1, 1] and lead.board_channel_id == BOARD
    order = [m for v in ids(rs, lead).values() for m in v]
    assert order == sorted(order), "in lineup order: the first raid's message first"
    first_text, bd_text = texts(bot, rs, lead)
    assert "Hyjal" in first_text.splitlines()[0] and "<t:" in first_text and "Group 1" in first_text
    assert "[Priest:Shadow] Nyx" in first_text and mages[-1].display_name not in first_text and char(lead, mages[-1]) not in first_text
    assert BOARD_NOBODY in bd_text and "Group" not in bd_text, "a raid with nobody placed: its header and one line"
    assert "Bench" not in first_text and "Not placed" not in first_text

    async def switch():  # the 10-man: Nyx placed and switched to Holy for it alone
        fev2 = rs.events[fev.key]
        fev2.layout = [[nyx.display_name], []]
        rs.save(fev2, "board (test)")
        rc.set_raid_switch(reg, rs, rs.events[fev.key], nyx, "Nyx", "Holy", by="Kara")
    before = ids(rs, lead)
    scenario(bot, switch)
    assert ids(rs, lead) == before, "the same messages, edited"
    first_text, bd_text = texts(bot, rs, lead)
    assert "[Priest:Holy] Nyx" in bd_text and "[Priest:Shadow] Nyx" in first_text


# ---------------------------------------------------------------- live: every change edits the same messages

def test_every_change_edits_the_same_messages(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    first_ids = ids(rs, ev)
    (mid,), = first_ids.values()
    msg = bot.rooms[BOARD].msgs[mid]
    assert char(ev, ms[0]) in shown(msg) and char(ev, ms[9]) not in shown(msg)

    def changed(work, check):
        n = edits(bot)
        scenario(bot, work)
        assert ids(rs, ev) == first_ids, "never a new message"
        assert edits(bot) > n, "edited in place"
        assert check(shown(msg)), shown(msg)

    changed(lambda: bot.board_move(reg, rs, rs.events[ev.key], ms[9].display_name, (1, 3), by="Kara"),
            lambda t: char(ev, ms[9]) in t)

    async def autofill():
        p = rs.events[ev.key]
        layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:10]]]
        return await eb.autofill_of(bot).write(reg, rs, p.key, eb.inputs(reg, rs, p), layout, [ms[8].display_name], solver.Stop(), "Kara", None, "web")
    changed(autofill, lambda t: char(ev, ms[8]) in t)
    changed(lambda: bot.save_board(reg, rs, rs.events[ev.key], [[], []], by="Kara"), lambda t: BOARD_NOBODY in t)
    changed(lambda: bot.save_board(reg, rs, rs.events[ev.key], [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:10]]], by="Kara"),
            lambda t: char(ev, ms[7]) in t)
    changed(lambda: bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False), lambda t: "🔒" in t)
    changed(lambda: bot.board_move(reg, rs, rs.events[ev.key], ms[9].display_name, None, by="Kara", by_id=7),
            lambda t: char(ev, ms[9]) not in t)
    changed(lambda: bot.unlock_run(reg, rs, rs.events[ev.key], by="Jon"), lambda t: "🔒" not in t)


def test_a_message_deleted_by_hand_is_posted_again_at_the_next_change(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()

    async def work():
        await bot.rooms[BOARD].msgs[old].delete()
        await bot.board_move(reg, rs, rs.events[ev.key], ms[9].display_name, (1, 3), by="Kara")
    scenario(bot, work)
    (new,), = ids(rs, ev).values()
    assert new != old and new in bot.rooms[BOARD].msgs and char(ev, ms[9]) in shown(bot.rooms[BOARD].msgs[new])


# ---------------------------------------------------------------- a lineup edit adds and deletes messages

def test_a_raid_added_to_the_lineup_gains_a_message_and_a_removed_one_loses_it(reg, rs):
    from conftest import OWNER

    bot = BoardBot(reg, rs)
    lead, fev, ms = evening(reg, rs)
    scenario(bot, lambda: bot.post_board(reg, rs, lead, by="Kara"))
    assert [len(v) for v in ids(rs, lead).values()] == [1, 1]
    ony = "onyxias_lair"

    async def add():
        return await bot.edit_lineup(reg, rs, rs.events[lead.key], [{"raid": HYJAL, "rosters": 1}, {"raid": BD_RAID, "rosters": 2},
                                                                    {"raid": ony, "rosters": 1}], by="Jon", by_id=OWNER)
    scenario(bot, add)
    got = ids(rs, lead)
    assert len(got) == 3 and all(len(v) == 1 for v in got.values()) and "Onyxia" in texts(bot, rs, lead)[2]
    bd_mid = got[fev.key][0]

    async def remove():
        return await bot.edit_lineup(reg, rs, rs.events[lead.key], [{"raid": HYJAL, "rosters": 1}, {"raid": ony, "rosters": 1}], by="Jon", by_id=OWNER)
    scenario(bot, remove)
    assert bd_mid in bot.rooms[BOARD].deleted and fev.key not in ids(rs, lead) and not rs.events[fev.key].board_posts


# ---------------------------------------------------------------- end and cancel: one last edit, then nothing

@pytest.mark.parametrize("how", ["end", "cancel"])
def test_the_event_ending_or_cancelled_edits_the_post_once_and_then_stops(reg, rs, how):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (mid,), = ids(rs, ev).values()
    msg = bot.rooms[BOARD].msgs[mid]

    async def close():
        if how == "cancel":
            await bot.cancel_run(reg, rs, rs.events[ev.key], by="Jon", reason=None, notify=False)
        else:
            await bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False)
            p = rs.events[ev.key]
            p.state, p.closed_at = "done", rc.now()
            rs.save(p, "done (test)")
    scenario(bot, close)
    assert ("Cancelled" if how == "cancel" else "Finished") in shown(msg) and rs.events[ev.key].board_done
    assert mid in bot.rooms[BOARD].msgs, "not deleted"
    n, sent = edits(bot), len(bot.rooms[BOARD].sent)

    async def later():
        p = rs.events[ev.key]
        p.log.append("a later line (test)")
        rs.save(p, "later (test)")
    scenario(bot, later)
    assert (edits(bot), len(bot.rooms[BOARD].sent)) == (n, sent), "no more edits"
    with pytest.raises(RegistryError, match="over"):
        run(bot.post_board(reg, rs, rs.events[ev.key], by="Kara"))


# ---------------------------------------------------------------- Remove board post

def test_remove_board_post_deletes_the_messages_and_stops_the_updates(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (mid,), = ids(rs, ev).values()
    said = scenario(bot, lambda: bot.remove_board_post(reg, rs, ev, by="Kara"))
    assert "removed from #board" in said and mid in bot.rooms[BOARD].deleted
    ev = rs.events[ev.key]
    assert ev.board_channel_id is None and not ev.board_posts and any("board post removed" in x for x in ev.log)
    sent = len(bot.rooms[BOARD].sent)
    scenario(bot, lambda: bot.board_move(reg, rs, rs.events[ev.key], ms[9].display_name, (1, 3), by="Kara"))
    assert len(bot.rooms[BOARD].sent) == sent and edits(bot) == 0
    with pytest.raises(RegistryError, match=BOARD_NONE):
        run(bot.remove_board_post(reg, rs, ev, by="Kara"))


# ---------------------------------------------------------------- a move: the box and Edit event, a failure, a restart

def test_picking_another_channel_in_the_box_moves_the_post_new_first_then_old_deleted(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()
    bot.seq.clear()
    said = scenario(bot, lambda: bot.post_board(reg, rs, ev, channel=ALT_BOARD, by="Kara"))
    ev = rs.events[ev.key]
    (new,), = ids(rs, ev).values()
    assert ev.board_channel_id == ALT_BOARD and new in bot.rooms[ALT_BOARD].msgs and old in bot.rooms[BOARD].deleted
    assert bot.seq.index(("send", ALT_BOARD)) < bot.seq.index(("delete", BOARD)), "posted in the new channel before the old goes"
    assert rc.event_channel(reg, ev, "board_channel") == ALT_BOARD and not rc.pending_moves(reg, ev), "it becomes the event's own"
    assert "has moved" in said and not bot.dms
    lines = [x[-1] for x in bot.ops.lines]
    assert sum("posted the board" in t for t in lines) == 1 and sum("set the channels" in t for t in lines) == 1


def test_a_first_post_to_a_picked_channel_makes_it_the_events_own(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, channel=ALT_BOARD, by="Kara"))
    ev = rs.events[ev.key]
    assert ev.board_channel_id == ALT_BOARD and rc.event_channel(reg, ev, "board_channel") == ALT_BOARD
    assert not bot.rooms[BOARD].sent
    lines = [x[-1] for x in bot.ops.lines]
    assert sum("board" in t for t in lines) == 1, "one ops line for the first post, the channel write included"


def test_edit_event_moves_the_post_a_failure_keeps_the_old_and_a_restart_heals(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()
    # a failure: nothing old touched, the setting goes back
    real_send = bot.rooms[ALT_BOARD].send
    _failing(bot.rooms[ALT_BOARD])
    said = scenario(bot, lambda: bot.reschedule_event(reg, rs, rs.events[ev.key], timing={"board_channel": str(ALT_BOARD)}, by="Kara"))
    ev = rs.events[ev.key]
    assert "did not move" in said[0] and ids(rs, ev) == {ev.key: [old]} and old in bot.rooms[BOARD].msgs
    assert rc.event_channel(reg, ev, "board_channel") is None and not rc.pending_moves(reg, ev)
    bot.rooms[ALT_BOARD].send = real_send
    # a restart cut the move off after the write: the next ready finishes it, once
    rc.reschedule(reg, rs, ev, timing={"board_channel": str(ALT_BOARD)}, by="Kara")
    assert rc.pending_moves(reg, ev) == ["board_channel"]
    scenario(bot, lambda: bot.finish_channel_moves(reg))
    ev = rs.events[ev.key]
    assert ev.board_channel_id == ALT_BOARD and old in bot.rooms[BOARD].deleted and not rc.pending_moves(reg, ev)
    n = len(bot.rooms[ALT_BOARD].sent)
    scenario(bot, lambda: bot.finish_channel_moves(reg))
    assert len(bot.rooms[ALT_BOARD].sent) == n
    # Edit event's box says what moves
    box = cq.reschedule_box(reg, rs, ev, None, {"board_channel": str(BOARD)}, edit=True)
    assert "The board post moves to" in box["sentences"][0] and any(x.startswith("Board posts: ") for x in box["diff"])


# ---------------------------------------------------------------- who is told

def test_nobody_is_dmed_or_told_and_only_post_move_and_remove_write_a_line(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    notices = {m.discord_id: len(m.notices) for m in ms}
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    log_n, ops_n = len(rs.events[ev.key].log), len(bot.ops.lines)
    for i in range(3):  # live updates: no log line, no ops line
        scenario(bot, lambda: bot.board_move(reg, rs, rs.events[ev.key], ms[9].display_name, (1, i), by="Kara"))
    assert len(bot.ops.lines) == ops_n
    assert not [x for x in rs.events[ev.key].log[log_n:] if "board" in x.lower()]
    scenario(bot, lambda: bot.remove_board_post(reg, rs, ev, by="Kara"))
    assert len(bot.ops.lines) == ops_n + 1 and "removed the board post" in bot.ops.lines[-1][-1]
    assert not bot.dms and {m.discord_id: len(m.notices) for m in ms} == notices


# ---------------------------------------------------------------- the site: the box and its route

@pytest.fixture
def site(reg, rs, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from test_mcp import TOKEN

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    bot = BoardBot(reg, rs)
    bot.get_guild = lambda gid: SimpleNamespace(unavailable=False)
    bot.guild_channels = lambda reg_: [{"id": str(c), "name": r.name, "category": None} for c, r in bot.rooms.items() if isinstance(r, BRoom)]
    return SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)))


def test_the_box_opens_on_the_resolved_channel_posts_and_a_stale_box_is_409(reg, rs, site):
    from test_raid_night import H

    ev, ms = one_raid(reg, rs, site.bot)
    url = f"/api/event/{ev.key}/board"
    j = site.c.get(f"/api/event/{ev.key}", headers=H()).json()
    assert j["board_post"] == {"button": "Post board", "remove": None}
    b = site.c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert b["title"] == "Post board" and b["verb"] == "Post" and b["sentences"] == [] and b["diff"] == []
    assert b["channel"]["label"] == "Post to" and b["channel"]["value"] == str(BOARD)
    assert {"value": str(ALT_BOARD), "label": "#alt-board"} in b["channel"]["options"]
    stale = site.c.post(url, json={"seen": "0" * 12, "channel": str(BOARD)}, headers=H())
    assert stale.status_code == 409 and stale.json()["box"]["fp"] == b["fp"] and not site.bot.rooms[BOARD].sent
    ok = site.c.post(url, json={"seen": b["fp"], "channel": str(BOARD)}, headers=H())
    assert ok.status_code == 200 and "posted in #board" in ok.json()["message"], ok.text
    assert rs.events[ev.key].board_posts and len(site.bot.rooms[BOARD].sent) == 1
    j = site.c.get(f"/api/event/{ev.key}", headers=H()).json()
    assert j["board_post"] == {"button": "Update board post", "remove": "Remove board post"}
    b2 = site.c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert b2["title"] == "Update board post" and b2["fp"] != b["fp"]
    gone = site.c.post(f"/api/event/{ev.key}/board/remove", json={}, headers=H())
    assert gone.status_code == 200 and not rs.events[ev.key].board_posts
    again = site.c.post(f"/api/event/{ev.key}/board/remove", json={}, headers=H())
    assert again.status_code == 400 and again.json()["box"]["sentences"] == [BOARD_NONE]


def test_the_box_is_refused_on_an_ended_event(reg, rs, site):
    from test_raid_night import H

    ev, ms = one_raid(reg, rs, site.bot)
    run(site.bot.cancel_run(reg, rs, ev, by="Jon", reason=None, notify=False))
    r = site.c.post(f"/api/event/{ev.key}/board", json={"preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["sentences"] == [BOARD_OVER] and r.json()["box"]["verb"] is None
    assert site.c.get(f"/api/event/{ev.key}", headers=H()).json()["board_post"] is None


def test_a_member_who_is_not_an_officer_is_refused(reg, rs, monkeypatch):
    from conftest import open_test_run
    from test_member_inbox_web import as_user, w as w_fixture  # noqa: F401

    from oibot_gm.web import app as web
    from fastapi.testclient import TestClient
    from test_member_inbox_web import SECRET, TOKEN, WebNightBot

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    c = TestClient(web.create_app(WebNightBot(reg, rs)))
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    for path in ("board", "board/remove"):
        r = c.post(f"/api/event/{ev.key}/{path}", json={"preview": True}, headers=as_user(c, m.discord_id))
        assert r.status_code == 403, path


# ---------------------------------------------------------------- fix round 1 (review of 2026-11-27)

def _move(bot, reg, rs, ev, ms, i=0):
    return bot.board_move(reg, rs, rs.events[ev.key], ms[9].display_name, (1, i), by="Kara")


@pytest.mark.parametrize("err", [http_error(discord.Forbidden, 403), http_error(discord.HTTPException, 503)], ids=["forbidden", "5xx"])
def test_an_edit_discord_refuses_or_fails_keeps_the_id_and_posts_nothing_new(reg, rs, err):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (mid,), = ids(rs, ev).values()
    bot.rooms[BOARD].edit_error = err
    for i in range(3):
        scenario(bot, lambda i=i: _move(bot, reg, rs, ev, ms, i))
    assert ids(rs, ev) == {ev.key: [mid]} and len(bot.rooms[BOARD].sent) == 1 and live_msgs(bot) == 1, "never a second post"
    bot.rooms[BOARD].edit_error = None
    scenario(bot, lambda: _move(bot, reg, rs, ev, ms, 4))
    assert ids(rs, ev) == {ev.key: [mid]} and char(ev, ms[9]) in shown(bot.rooms[BOARD].msgs[mid]), "tried again at the next change"


def test_only_a_message_discord_says_is_gone_is_posted_again_once(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()

    async def work():
        await bot.rooms[BOARD].msgs[old].delete()
        await _move(bot, reg, rs, ev, ms, 3)
    scenario(bot, work)
    (new,), = ids(rs, ev).values()
    assert new != old and live_msgs(bot) == 1 and len(bot.rooms[BOARD].sent) == 2


def test_update_board_post_checks_every_message_and_posts_a_missing_one_again(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()
    said = scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    assert "up to date" in said and ids(rs, ev) == {ev.key: [old]}

    async def gone():
        await bot.rooms[BOARD].msgs[old].delete()
    scenario(bot, gone)
    said = scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (new,), = ids(rs, ev).values()
    assert new != old and live_msgs(bot) == 1 and "posted again" in said and "up to date" not in said, said
    bot.rooms[BOARD].edit_error = http_error(discord.HTTPException, 503)
    said = scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    assert "couldn't" in said and ids(rs, ev) == {ev.key: [new]} and live_msgs(bot) == 1, said


def test_two_first_presses_at_once_post_once_and_the_second_is_told_the_box_changed(reg, rs, site):
    from test_raid_night import H

    ev, ms = one_raid(reg, rs, site.bot)
    url = f"/api/event/{ev.key}/board"
    assert site.c.post(url, json={"channel": str(BOARD)}, headers=H()).status_code == 409, "seen is required"
    fp = site.c.post(url, json={"preview": True}, headers=H()).json()["box"]["fp"]

    async def both():
        from oibot_gm import consequences as cq

        def fresh(f):
            return cq.board_post_box(reg, rs, f, [], site.bot.board_where(reg, f))["fp"]
        return await asyncio.gather(site.bot.post_board(reg, rs, ev, channel=BOARD, by="A", seen=fp, fp=fresh),
                                    site.bot.post_board(reg, rs, ev, channel=ALT_BOARD, by="B", seen=fp, fp=fresh), return_exceptions=True)
    got = run(both())
    assert sum(isinstance(x, str) for x in got) == 1 and sum(isinstance(x, rc.BoardChanged) for x in got) == 1, got
    assert live_msgs(site.bot, BOARD) + live_msgs(site.bot, ALT_BOARD) == 1
    r = site.c.post(url, json={"seen": fp, "channel": str(ALT_BOARD)}, headers=H())
    assert r.status_code == 409 and r.json()["box"]["title"] == "Update board post"


def test_a_post_on_the_sheets_channel_moves_with_the_sheet(reg, rs):
    bot = BoardBot(reg, rs)
    reg.config.board_channel_id = None
    for cid, name in ((SIGNUP, "signups"), (ALT_SHEET, "alt-signups")):
        bot.rooms[cid] = BRoom(bot, cid, name)
        bot.rooms[cid].history = True
    bot.signup = bot.rooms[SIGNUP]
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    (old,), = ids(rs, ev).values()
    assert rs.events[ev.key].board_channel_id == SIGNUP and old in bot.rooms[SIGNUP].msgs
    bot.seq.clear()
    scenario(bot, lambda: bot.reschedule_event(reg, rs, rs.events[ev.key], timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    ev = rs.events[ev.key]
    (new,), = ids(rs, ev).values()
    assert ev.board_channel_id == ALT_SHEET and new in bot.rooms[ALT_SHEET].msgs and old not in bot.rooms[SIGNUP].msgs
    sends = [i for i, x in enumerate(bot.seq) if x == ("send", ALT_SHEET)]
    assert sends and max(sends) < max(i for i, x in enumerate(bot.seq) if x == ("delete", SIGNUP)), "new first, then old deleted"


def test_a_post_whose_channel_is_gone_is_posted_again_where_board_posts_go_with_one_true_ops_line(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, channel=ALT_BOARD, by="Kara"))
    del bot.rooms[ALT_BOARD]  # the channel was deleted
    for i in range(2):
        scenario(bot, lambda i=i: _move(bot, reg, rs, ev, ms, i))
    ev = rs.events[ev.key]
    assert ev.board_channel_id == BOARD and live_msgs(bot, BOARD) == 1
    lines = [x[-1] for x in bot.ops.lines if "board post" in x[-1] and "gone" in x[-1]]
    assert len(lines) == 1 and "#board" in lines[0] and "guild's channel" not in lines[0], lines


def test_an_ended_post_whose_channel_is_gone_is_never_posted_again(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, channel=ALT_BOARD, by="Kara"))
    del bot.rooms[ALT_BOARD]
    scenario(bot, lambda: bot.cancel_run(reg, rs, rs.events[ev.key], by="Jon", reason=None, notify=False))
    run(bot.boards_restore(reg))
    assert live_msgs(bot, BOARD) == 0 and not bot.rooms[BOARD].sent and rs.events[ev.key].board_done


def test_remove_board_post_is_refused_and_hidden_once_the_event_is_over(reg, rs, site):
    from test_raid_night import H

    ev, ms = one_raid(reg, rs, site.bot)
    run(site.bot.post_board(reg, rs, ev, by="Kara"))
    run(site.bot.cancel_run(reg, rs, rs.events[ev.key], by="Jon", reason=None, notify=False))
    assert site.c.get(f"/api/event/{ev.key}", headers=H()).json()["board_post"] is None
    r = site.c.post(f"/api/event/{ev.key}/board/remove", json={}, headers=H())
    assert r.status_code == 400 and rs.events[ev.key].board_posts and live_msgs(site.bot) == 1


def test_a_storm_of_saves_edits_the_post_once(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))
    n = edits(bot)

    async def storm():
        p = rs.events[ev.key]
        for i in range(8):
            p.layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:6 + i % 4]]]
            rs.save(p, f"storm {i} (test)")
    scenario(bot, storm)
    assert edits(bot) == n + 1


def test_a_listener_error_never_breaks_a_save(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)

    class Broken:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *a):
            raise ValueError("broken loop")
    bot.install_board_posts(Broken())
    p = rs.events[ev.key]
    p.log.append("saved anyway (test)")
    rs.save(p, "a save (test)")
    assert "saved anyway (test)" in rc.RaidStore(reg.store, reg.key).events[ev.key].log


def test_a_redraw_racing_a_move_leaves_exactly_one_post(reg, rs):
    bot = BoardBot(reg, rs)
    ev, ms = one_raid(reg, rs, bot)
    scenario(bot, lambda: bot.post_board(reg, rs, ev, by="Kara"))

    async def race():
        gate = asyncio.Event()
        bot.rooms[BOARD].gate = gate
        await _move(bot, reg, rs, ev, ms, 2)  # schedules a redraw that stops inside its edit
        for _ in range(5):
            await asyncio.sleep(0)
        await bot.reschedule_event(reg, rs, rs.events[ev.key], timing={"board_channel": str(ALT_BOARD)}, by="Kara")
        gate.set()
        bot.rooms[BOARD].gate = None
    scenario(bot, race)
    assert live_msgs(bot, BOARD) + live_msgs(bot, ALT_BOARD) == 1 and live_msgs(bot, ALT_BOARD) == 1
    assert rs.events[rs.events[ev.key].key].board_channel_id == ALT_BOARD
