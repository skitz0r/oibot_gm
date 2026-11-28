"""A schedule's and an event's own Discord channels (design 2026-11-27-event-channels): where its signup sheet, its
officers' cards and its loot log lines post. An event with none uses its schedule's (stamped when it opens, later raids
of a lineup too), then the guild's; an empty value means the guild's, and an event or schedule stored before this has
none. Changing one on a live event moves what is posted (the sheet with every answer, the cards of every raid, a new
updates thread); a deleted channel falls back to the guild's with one ops line; nobody is ever DMed by any of it."""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import first_slot, join, weekly
from test_bench_last import base_eight, people, DPS
from test_event_threads import OPS, ROSTER, SIGNUP, Msg, Room, ThreadBot, Thread
from test_follow_on import FOLLOW, LEAD, unlock_hyjal
from test_raid_night import run, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import raidcycle as rc
from oibot_gm.registry import OFFICERS_ON_SHEET, RegistryError

RID = "barrow_deeps"
ALT_SHEET, ALT_CARDS, LOOT, ALT_LOOT, GONE = 62001, 62002, 62003, 62004, 62999


class ChanBot(ThreadBot):
    """ThreadBot with three more channels: another sheet channel, another officers' channel and two loot channels."""

    def __init__(self, reg, rs, **kw):
        super().__init__(reg, rs, roster_is_signup=False, **kw)
        for cid, name in ((ALT_SHEET, "alt-signups"), (ALT_CARDS, "alt-officers"), (LOOT, "loot"), (ALT_LOOT, "alt-loot")):
            self.rooms[cid] = Room(self, cid, name)

    def dms(self) -> int:
        return len(getattr(self, "dm_log", []) or []) + sum(len(c.sent) for c in getattr(self, "dm_channels", {}).values())


@pytest.fixture(autouse=True)
def threads_archive(monkeypatch):
    """The fake thread can be archived and unarchived, as a Discord thread is (Thread.edit)."""
    async def edit(self, **kw):
        self.archived = kw.get("archived", self.archived)
        self.name = kw.get("name", self.name)

    monkeypatch.setattr(Thread, "edit", edit, raising=False)


def sched(reg, sid="tue", **chans):
    weekly(reg, RID, sid=sid)
    if chans:
        reg.set_schedule(sid, chans, "t")
    return sid


def open_from(reg, rs, sid="tue"):
    return rc.open_run(reg, rs, RID, first_slot(reg, RID), by="t", schedule=sid)


# ---------------------------------------------------------------- inheritance: event → schedule → guild

def test_a_schedules_channels_are_stamped_on_the_event_it_opens_and_the_guilds_fill_the_rest(reg, rs):
    reg.config.signup_channel_id, reg.config.roster_channel_id, reg.config.loot_channel_id = SIGNUP, ROSTER, LOOT
    sid = sched(reg, sheet_channel=str(ALT_SHEET), loot_log_channel=f"<#{ALT_LOOT}>")
    ev = open_from(reg, rs, sid)
    assert rc.event_channel(reg, ev, "sheet_channel") == ALT_SHEET
    assert rc.event_channel(reg, ev, "officers_channel") is None, "unset: never stamped"
    assert (rc.channel_of(reg, ev, "sheet_channel"), rc.channel_of(reg, ev, "officers_channel"), rc.channel_of(reg, ev, "loot_log_channel")) \
        == (ALT_SHEET, ROSTER, ALT_LOOT)
    reg.set_schedule(sid, {"sheet_channel": None}, "t")  # a schedule edit never moves a live event
    assert rc.channel_of(reg, ev, "sheet_channel") == ALT_SHEET
    assert "sheet_channel" not in reg.schedule_entry(sid), "empty means the guild's: nothing stored"


def test_every_raid_of_a_lineup_carries_the_first_raids_channels(reg, rs):
    unlock_hyjal(reg)
    reg.set_schedule("pair", {"kind": "weekly", "slots": "Tue 18:30", "follow": {"raid": FOLLOW, "rosters": 1}, "officers_channel": str(ALT_CARDS)}, "t", raid=LEAD)
    start = first_slot(reg, LEAD)
    lead = rc.open_run(reg, rs, LEAD, start, by="t", schedule="pair")
    later = rc.later_parts(rs, lead)
    assert later and all(rc.event_channel(reg, p, "officers_channel") == ALT_CARDS for p in [lead, *later])


def test_an_event_and_a_schedule_from_before_have_no_channels_of_their_own(reg, rs):
    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    sid = sched(reg)
    ev = open_from(reg, rs, sid)
    t = reg.config.team(ev.team)
    assert not {"sheet_channel", "officers_channel", "loot_log_channel"} & set(t)
    assert not {"sheet_channel", "officers_channel", "loot_log_channel"} & set(reg.schedule_entry(sid))
    assert rc.channel_of(reg, ev, "sheet_channel") == SIGNUP and rc.own_channels(reg, ev) == dict.fromkeys(rc.SCHEDULE_CHANNELS)


def test_a_one_offs_open_form_names_its_own_channels(reg, rs):
    start = (reg.now_local() + timedelta(days=4)).replace(hour=13, minute=15, second=0, microsecond=0)
    ev = rc.open_run(reg, rs, RID, start, by="t", channels={"sheet_channel": str(ALT_SHEET), "officers_channel": ""})
    assert rc.own_channels(reg, ev) == {"sheet_channel": str(ALT_SHEET), "officers_channel": None, "loot_log_channel": None, "board_channel": None}


# ---------------------------------------------------------------- the save-time rules

def test_a_schedule_refuses_officers_cards_in_its_sheet_channel(reg):
    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    sid = sched(reg)
    with pytest.raises(RegistryError, match="officers' cards can't go"):
        reg.set_schedule(sid, {"sheet_channel": str(ALT_SHEET), "officers_channel": str(ALT_SHEET)}, "t")
    with pytest.raises(RegistryError, match="officers' cards can't go"):
        reg.set_schedule(sid, {"officers_channel": str(SIGNUP)}, "t")  # the guild's sheet channel is public
    with pytest.raises(RegistryError, match="officers' cards can't go"):
        reg.set_schedule(sid, {"sheet_channel": str(ROSTER)}, "t")  # its sheet in the guild's officers' channel
    assert "sheet_channel" not in reg.schedule_entry(sid), "a refusal writes nothing"
    with pytest.raises(RegistryError, match="Pick the channel"):
        reg.set_schedule(sid, {"loot_log_channel": "raid chat"}, "t")


def test_an_event_edit_refuses_officers_cards_in_its_sheet_channel(reg, rs):
    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    ev = open_from(reg, rs, sched(reg, sheet_channel=str(ALT_SHEET)))
    assert rc.reschedule_refusal(reg, rs, ev, None, {"officers_channel": str(ALT_SHEET)}) == OFFICERS_ON_SHEET
    assert rc.reschedule_refusal(reg, rs, ev, None, {"sheet_channel": str(ALT_SHEET)}) == rc.CHANNELS_SAME
    assert rc.reschedule_refusal(reg, rs, ev, None, {"officers_channel": str(ALT_CARDS)}) is None


# ---------------------------------------------------------------- editable after the lock and the start, not once ended

def test_channels_change_after_the_lock_and_the_start_but_not_once_ended_or_cancelled(reg, rs):
    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    ev = open_from(reg, rs, sched(reg))
    ev.state = "locked"
    rs.save(ev, "locked (test)")
    assert rc.reschedule_refusal(reg, rs, ev, None, {"loot_log_channel": str(ALT_LOOT)}) is None
    assert rc.reschedule_refusal(reg, rs, ev, None, {"lock_hours_before": 3}) == rc.TIMING_LOCKED, "timing keeps its gate"
    assert rc.reschedule_refusal(reg, rs, ev, None, {"loot_log_channel": str(ALT_LOOT)}, now=ev.start + timedelta(minutes=5)) is None
    assert rc.reschedule_refusal(reg, rs, ev, None, {"loot_log_channel": str(ALT_LOOT), "fill_ask_hours": 3}, now=ev.start + timedelta(minutes=5)) == rc.MOVE_STARTED
    for state in ("done", "cancelled"):
        ev.state = state
        assert rc.reschedule_refusal(reg, rs, ev, None, {"loot_log_channel": str(ALT_LOOT)}) == rc.CHANNELS_CLOSED


def test_an_edit_writes_every_raids_dict_and_the_rev_sees_it(reg, rs):
    unlock_hyjal(reg)
    reg.set_schedule("pair", {"kind": "weekly", "slots": "Tue 18:30", "follow": {"raid": FOLLOW, "rosters": 1}}, "t", raid=LEAD)
    lead = rc.open_run(reg, rs, LEAD, first_slot(reg, LEAD), by="t", schedule="pair")
    before = rc.event_rev(reg, rs, lead)
    mv = rc.reschedule(reg, rs, lead, timing={"loot_log_channel": str(ALT_LOOT)}, by="Kara")
    assert mv.channels == {"loot_log_channel": [None, str(ALT_LOOT)]} and not mv.timing and not mv.told
    assert all(rc.event_channel(reg, p, "loot_log_channel") == ALT_LOOT for p in rc.event_parts(rs, lead))
    assert rc.event_rev(reg, rs, lead) != before
    rc.reschedule(reg, rs, lead, timing={"loot_log_channel": ""}, by="Kara")
    assert all(rc.event_channel(reg, p, "loot_log_channel") is None for p in rc.event_parts(rs, lead))


# ---------------------------------------------------------------- opening: the resolver, not the guild's setting

def test_a_schedule_with_its_own_sheet_channel_opens_when_the_guild_has_none(reg, rs):
    bot = ChanBot(reg, rs)
    reg.config.signup_channel_id = None
    reg.set_raid_overrides(RID, {"first_open": "2026-01-01T00:00"}, "t")
    weekly(reg, RID, "Wed 20:00", sid="wed")
    reg.set_schedule("wed", {"signup_lead_hours": 24 * 14, "sheet_channel": str(ALT_SHEET)}, "t")
    run(bot.scheduler_tick_guild(reg))
    assert rs.events, "it opened"
    assert not [t for _l, t in bot.ops.lines if "did not open" in t]
    first = next(iter(rs.events.values()))
    assert first.channel_id == ALT_SHEET and first.message_id in bot.rooms[ALT_SHEET].msgs
    rows = rc.upcoming_rows(reg, rs, RID)
    assert all(not r["needs_channel"] for r in rows)
    assert rc.open_preview(reg, rs, RID, schedule="wed")["channel"] == ALT_SHEET


def test_a_deleted_own_channel_falls_back_to_the_guilds_with_one_ops_line(reg, rs):
    bot = ChanBot(reg, rs)
    ev = open_from(reg, rs, sched(reg, sheet_channel=str(GONE), officers_channel=str(GONE + 1)))
    async def resolve_twice():
        import asyncio

        for _ in range(2):
            assert bot.sheet_channel(reg, ev).id == SIGNUP
            assert bot.officer_channel(reg, ev).id == ROSTER
        await asyncio.sleep(0)
    run(resolve_twice())
    gone = [t for _l, t in bot.ops.lines if "is gone or closed to the bot" in t]
    assert len(gone) == 2 and any("signup sheet" in t for t in gone) and any("officers' cards" in t for t in gone), "one line per place, never repeated"


# ---------------------------------------------------------------- a move on a live event

def live(reg, rs, bot, **chans):
    sid = sched(reg, **chans)
    ev = run(bot.open_run_and_post(reg, rs, RID, first_slot(reg, RID), by="t", schedule=sid))
    ms = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, ms)
    return rs.events[ev.key], ms


def test_moving_the_sheet_on_an_open_event_keeps_every_answer_and_deletes_the_old_one(reg, rs):
    bot = ChanBot(reg, rs)
    ev, ms = live(reg, rs, bot)
    old_mid, answers = ev.message_id, dict(ev.signups)
    assert ev.channel_id == SIGNUP
    ev.sheet_thread_id = old_mid  # a thread was started on the old sheet
    rs.save(ev, "thread (test)")
    said = run(bot.reschedule_event(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    ev = rs.events[ev.key]
    assert ev.channel_id == ALT_SHEET and ev.message_id in bot.rooms[ALT_SHEET].msgs
    assert old_mid in bot.rooms[SIGNUP].deleted and old_mid not in bot.rooms[SIGNUP].msgs, "never left behind"
    assert ev.sheet_thread_id is None and ev.lock_line is None
    assert ev.signups.keys() == answers.keys()
    assert "Nobody was DMed" in said[0] and not bot.dms
    assert any("channels set by Kara" in t for _l, t in bot.ops.lines)


def test_moving_the_sheet_on_a_locked_event_writes_the_lock_line_again_in_the_new_thread(reg, rs):
    bot = ChanBot(reg, rs)
    ev, ms = live(reg, rs, bot)
    ev.layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:10]]]
    rs.save(ev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False))
    ev = rs.events[ev.key]
    old_line = list(ev.lock_line)
    old_thread = bot.rooms[old_line[0]]
    assert old_thread.parent_id == SIGNUP
    dms_before = {k: len(v) for k, v in bot.dms.items()}
    run(bot.reschedule_event(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    ev = rs.events[ev.key]
    assert old_line[1] in old_thread.deleted and old_thread.archived
    new_thread = bot.rooms[ev.lock_line[0]]
    assert new_thread.parent_id == ALT_SHEET and "is locked" in new_thread.lines()[-1]
    assert ev.sheet_thread_id == ev.message_id == new_thread.id
    assert {k: len(v) for k, v in bot.dms.items()} == dms_before, "nobody DMed by the move"


def test_moving_the_officers_cards_moves_every_raids_cards_and_starts_a_new_updates_thread(reg, rs):
    from test_event_threads import card_in

    unlock_hyjal(reg)
    bot = ChanBot(reg, rs)
    reg.set_schedule("pair", {"kind": "weekly", "slots": "Tue 18:30", "follow": {"raid": FOLLOW, "rosters": 1}}, "t", raid=LEAD)
    lead = rc.open_run(reg, rs, LEAD, first_slot(reg, LEAD), by="t", schedule="pair")
    parts = rc.event_parts(rs, lead)
    old = {p.key: card_in(bot, rs, p, ROSTER, "health").id for p in parts}
    run(bot.post_run_update(reg, lead, "a line before the move"))
    lead = rs.events[lead.key]
    old_thread = bot.rooms[lead.updates_thread_id]
    run(bot.reschedule_event(reg, rs, lead, timing={"officers_channel": str(ALT_CARDS)}, by="Kara"))
    for p in rc.event_parts(rs, lead):
        p = rs.events[p.key]
        assert p.cards_channel_id == ALT_CARDS and p.cards["health"] in bot.rooms[ALT_CARDS].msgs
        assert old[p.key] in bot.rooms[ROSTER].deleted
        view = next(c["view"] for c in bot.rooms[ALT_CARDS].sent if c.get("view") is not None)
        assert any(p.key in str(getattr(i, "custom_id", "")) or lead.key in str(getattr(i, "custom_id", "")) for i in view.walk_children()), \
            "the persistent buttons are keyed by the event"
    assert old_thread.lines()[-1] == "Updates for this event continue in #alt-officers." and old_thread.archived
    lead = rs.events[lead.key]
    new_thread = bot.rooms[lead.updates_thread_id]
    assert new_thread.parent_id == ALT_CARDS and any("channels set by Kara" in x for x in new_thread.lines())
    assert not bot.dms


def test_loot_log_lines_go_to_the_events_own_channel(reg, rs):
    from oibot_gm.discord_pool import LootLogMixin

    bot = ChanBot(reg, rs)
    reg.config.loot_channel_id, reg.config.loot_log = LOOT, True
    ev = open_from(reg, rs, sched(reg))
    run(bot.reschedule_event(reg, rs, ev, timing={"loot_log_channel": str(ALT_LOOT)}, by="Kara"))
    target = LootLogMixin.loot_log_target
    assert target(bot, reg, ev.key) == (bot.rooms[ALT_LOOT], False)
    run(bot.reschedule_event(reg, rs, ev, timing={"loot_log_channel": ""}, by="Kara"))
    assert target(bot, reg, ev.key) == (bot.rooms[LOOT], False), "cleared: the guild's"
    assert not bot.rooms[ALT_LOOT].sent, "lines already posted stay; nothing is moved"


def test_a_schedule_edit_moves_no_live_event(reg, rs):
    bot = ChanBot(reg, rs)
    ev, _ms = live(reg, rs, bot)
    reg.set_schedule("tue", {"sheet_channel": str(ALT_SHEET)}, "t")
    assert rs.events[ev.key].channel_id == SIGNUP and not bot.rooms[ALT_SHEET].sent


def test_a_lock_still_posting_refuses_a_channel_move(reg, rs):
    bot = ChanBot(reg, rs)
    ev, _ms = live(reg, rs, bot)
    bot._lock_posting().add((reg.key, ev.key))
    with pytest.raises(RegistryError, match="still posting"):
        run(bot.reschedule_event(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    assert rs.events[ev.key].channel_id == SIGNUP and rc.event_channel(reg, ev, "sheet_channel") is None


def test_a_cleared_channel_on_an_event_from_a_schedule_goes_back_to_the_schedules(reg, rs):
    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    ev = open_from(reg, rs, sched(reg, loot_log_channel=str(ALT_LOOT)))
    rc.reschedule(reg, rs, ev, timing={"loot_log_channel": str(LOOT)}, by="Kara")
    assert rc.event_channel(reg, ev, "loot_log_channel") == LOOT
    rc.reschedule(reg, rs, ev, timing={"loot_log_channel": ""}, by="Kara")
    assert rc.event_channel(reg, ev, "loot_log_channel") == ALT_LOOT


# ---------------------------------------------------------------- every surface: the site, Discord, plain text, MCP

def test_the_site_edits_an_events_channels_with_a_box_and_a_stale_one_is_409(reg, rs, web_bot):
    from test_raid_night import H

    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    ev = open_from(reg, rs, sched(reg))
    c, url = web_bot.c, f"/api/run/{ev.key}/reschedule"
    body = {"edit": True, "timing": {"loot_log_channel": str(ALT_LOOT)}}
    b = c.post(url, json={**body, "preview": True}, headers=H()).json()["box"]
    assert b["verb"] == "Save" and "Nobody gets a DM." in b["sentences"] and any(x.startswith("Loot log: ") for x in b["diff"])
    rc.reschedule(reg, rs, ev, timing={"officers_channel": str(ALT_CARDS)}, by="other officer")  # changed while the box is open
    r = c.post(url, json={**body, "seen": b["fp"]}, headers=H())
    assert r.status_code == 409 and rc.event_channel(reg, ev, "loot_log_channel") is None
    ok = c.post(url, json={**body, "seen": r.json()["box"]["fp"]}, headers=H())
    assert ok.status_code == 200, ok.text
    assert rc.event_channel(reg, ev, "loot_log_channel") == ALT_LOOT
    bad = c.post(url, json={"edit": True, "timing": {"officers_channel": str(SIGNUP)}, "preview": True}, headers=H())
    assert bad.status_code == 400 and bad.json()["error"] == OFFICERS_ON_SHEET
    j = c.get(f"/api/event/{ev.key}", headers=H()).json()
    sect = j["edit"]["channels"]
    assert [f["key"] for f in sect["fields"]] == list(rc.SCHEDULE_CHANNELS) and sect["title"] == "Channels"
    assert next(f for f in sect["fields"] if f["key"] == "loot_log_channel")["value"] == str(ALT_LOOT)
    labels = {f["label"] for f in j["details"]}
    assert "Loot log" in labels and "Signup sheet" in labels  # the loot log differs from the guild's; the sheet row is the link's
    new = c.get("/api/event-new", headers=H()).json()["channels"]
    assert all(f["value"] is None and "(the guild's)" in f["empty"] for f in new["fields"] if f["key"] not in ("loot_log_channel", "board_channel"))
    assert next(f for f in new["fields"] if f["key"] == "board_channel")["empty"] == "The signup sheet's channel"


def test_the_site_opens_a_one_off_with_its_own_channels(reg, rs, web_bot):
    from test_raid_night import H

    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    when = (reg.now_local() + timedelta(days=4)).replace(hour=13, minute=15).strftime("%Y-%m-%d %H:%M")
    r = web_bot.c.post(f"/api/raid/{RID}/open", json={"when": when, "channels": {"sheet_channel": str(ALT_SHEET), "officers_channel": str(ALT_SHEET)}},
                       headers=H())
    assert r.status_code == 400 and OFFICERS_ON_SHEET in r.text and not rs.events
    r = web_bot.c.post(f"/api/raid/{RID}/open", json={"when": when, "channels": {"sheet_channel": str(ALT_SHEET)}}, headers=H())
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    assert rc.event_channel(reg, ev, "sheet_channel") == ALT_SHEET


def test_the_site_saves_a_schedules_channels_through_the_ops_path(reg, rs, web_bot):
    from test_raid_night import H

    sid = sched(reg)
    change = {"kind": "schedule", "target": sid, "fields": {"officers_channel": str(ALT_CARDS)}}
    pv = web_bot.c.post("/api/ops/change", json={"changes": [change]}, headers=H()).json()
    box = pv["described"]["box"]
    assert box["verb"] == "Save" and any(x.startswith("Officers' cards: ") for x in box["diff"]), pv
    r = web_bot.c.post("/api/ops/change", json={"token": pv["token"], "apply": True, "changes": [change], "revs": pv["revs"]}, headers=H())
    assert r.status_code == 200, r.text
    assert reg.schedule_entry(sid).get("officers_channel") == str(ALT_CARDS), r.text
    j = web_bot.c.get(f"/api/schedules/{sid}", headers=H()).json()
    assert j["stored"]["officers_channel"] == str(ALT_CARDS) and any(f["label"] == "Officers' cards" for f in j["details"])
    assert web_bot.c.get("/api/schedules", headers=H()).json()["editor"]["channels"]["title"] == "Channels"


def test_plain_text_reaches_the_channels_on_both_ops(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    sid = sched(reg)
    bot = ChanBot(reg, rs)
    run(configops.apply_async(reg, ConfigOp(op="schedule_set", target=sid, field="sheet_channel", value=f"<#{ALT_SHEET}>"), "Jon", True, bot=bot))
    assert reg.schedule_entry(sid)["sheet_channel"] == str(ALT_SHEET)
    ev = open_from(reg, rs, sid)
    op = ConfigOp(op="run_reschedule", target=ev.key, field="loot_log_channel", value=f"<#{ALT_LOOT}>")
    assert configops._reschedule_args(reg, op) == (None, {"loot_log_channel": ALT_LOOT})
    run(configops.apply_async(reg, op, "Jon", True, bot=bot))
    assert rc.event_channel(reg, ev, "loot_log_channel") == ALT_LOOT
    run(configops.apply_async(reg, ConfigOp(op="run_reschedule", target=ev.key, field="loot_log_channel", value="none"), "Jon", True, bot=bot))
    assert rc.event_channel(reg, ev, "loot_log_channel") is None


def test_the_schedule_and_event_wizards_have_a_skippable_channels_step(reg, rs, monkeypatch):
    import discord
    from test_flows_raid import officer  # noqa: F401
    from test_reschedule import OFFICER_ID
    from test_setup import pick_widget
    from types import SimpleNamespace
    from wizard_harness import Harness

    from oibot_gm import discord_registry, flows_config, flows_event, flows_schedule  # noqa: F401 — registers the flows
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_officer", lambda *a, **k: True)
    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    sid = sched(reg)
    bot = ChanBot(reg, rs)
    user = SimpleNamespace(id=OFFICER_ID, display_name="Officer", mention="@Officer")
    h = Harness(reg, rs, user=user, client=bot)
    run(FLOWS["schedule"](h.command(), reg, raid=RID))
    run(h.pick("Pick a schedule", sid))
    run(h.pick("Change", "channels"))
    run(h.pick("Channels", "officers_channel"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Pick the channel", bot.rooms[ALT_CARDS]))
    run(h.press("Back"))
    assert f"Officers' cards: <#{ROSTER}> (the guild's) → <#{ALT_CARDS}>" in h.text
    run(h.press("Review changes"))
    run(h.press("Save"))
    assert reg.schedule_entry(sid)["officers_channel"] == str(ALT_CARDS), h.text

    ev = run(bot.open_run_and_post(reg, rs, RID, first_slot(reg, RID), by="t", schedule=sid))
    h2 = Harness(reg, rs, user=user, client=bot)
    run(FLOWS["raid_edit"](h2.command(), reg))
    run(h2.pick("Change a setting", "sheet_channel"))
    run(pick_widget(h2, discord.ui.ChannelSelect, "Pick the channel", bot.rooms[ALT_SHEET]))
    assert f"→ <#{ALT_SHEET}>" in h2.text
    run(h2.press("Review changes"))
    assert "moves to" in h2.text and "Nobody gets a DM." in h2.text
    run(h2.press("Save"))
    assert rs.events[ev.key].channel_id == ALT_SHEET and "✅" in h2.text


def test_the_channels_reach_every_surface():
    """The three channels ride on schedule_set (a schedule's), run_reschedule (one event's: Edit event) and the open
    route (a one-off's) — the site's editors, Discord's /gm schedule and /raid edit, plain text and MCP each name them."""
    from pathlib import Path

    from oibot_gm import configops, flows_event, flows_raid, flows_schedule
    from oibot_gm import mcp_server as ms
    from oibot_gm.registry import SCHEDULE_FIELDS
    from oibot_gm.schedule_view import STORED_FIELDS

    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    for k in rc.SCHEDULE_CHANNELS:
        assert k in SCHEDULE_FIELDS and k in STORED_FIELDS, k  # schedule_set's settings; the editor sends them back
        assert k in flows_event.CHANNELS and k in flows_schedule.CHANNELS, k  # Discord's two wizards
        assert k in configops.SCHEMA_TEXT, k  # plain text: schedule_set names them; run_reschedule says "sheet_channel | …"
        assert k in ms.schedule_set.__doc__ and k in ms.reschedule_run.__doc__ and k in ms.open_run.__doc__, k
        assert k in flows_raid.OpenWizard.one_off_channels.__code__.co_names or k in flows_event.CHANNELS, k  # /raid open's one-off step
        assert configops._open_channels(f"{k} <#1>") == {k: "1"}, k  # plain text's run_open
    tsx = (src / "components" / "ChannelFields.tsx").read_text()
    assert "channels" in (src / "pages" / "LineupPage.tsx").read_text() and "ChannelFields" in (src / "components" / "events" / "EditEvent.tsx").read_text()
    assert "fields" in tsx and "empty" in tsx, "labels and empty options come from the server"


# ---------------------------------------------------------------- fix round 1 (review of 2026-11-27)

def test_a_schedules_channel_is_checked_on_the_path_every_surface_uses(reg, rs):
    """The site, the Discord wizard, plain text and MCP all write through configops.apply_ops: the permission check must
    run there, not only in apply_async called directly."""
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    sid = sched(reg)
    bot = ChanBot(reg, rs)
    asked = []

    async def refuse(reg_, chans):
        asked.append(chans)
        return "I can't post in #alt-officers without Send Messages."
    bot.channel_setting_refusal = refuse
    res = run(configops.apply_ops(reg, [ConfigOp(op="schedule_set", target=sid, field="officers_channel", value=str(ALT_CARDS))],
                                  by="Jon", by_id=None, bot=bot))
    assert asked and not res["applied"] and "Send Messages" in res["results"][0]["line"]
    assert "officers_channel" not in reg.schedule_entry(sid)


def _failing(room):
    async def send(*a, **k):
        raise RuntimeError("Missing Permissions")
    room.send = send


def test_a_sheet_that_cant_post_in_the_new_channel_stays_and_the_setting_goes_back(reg, rs):
    bot = ChanBot(reg, rs)
    ev, _ms = live(reg, rs, bot)
    old = ev.message_id
    _failing(bot.rooms[ALT_SHEET])
    said = run(bot.reschedule_event(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    ev = rs.events[ev.key]
    assert ev.channel_id == SIGNUP and ev.message_id == old and old in bot.rooms[SIGNUP].msgs, "the old sheet stays"
    assert rc.event_channel(reg, ev, "sheet_channel") is None, "the setting goes back"
    assert "did not move" in said[0] and "has moved" not in said[0], said
    assert any("did not move" in t for _l, t in bot.ops.lines)
    assert not rc.pending_moves(reg, ev)


def test_cards_that_cant_post_in_the_new_channel_stay_with_their_buttons(reg, rs):
    from test_event_threads import card_in

    bot = ChanBot(reg, rs)
    ev, _ms = live(reg, rs, bot)
    card = card_in(bot, rs, ev, ROSTER, "health")
    _failing(bot.rooms[ALT_CARDS])
    said = run(bot.reschedule_event(reg, rs, ev, timing={"officers_channel": str(ALT_CARDS)}, by="Kara"))
    ev = rs.events[ev.key]
    assert ev.cards == {"health": card.id} and ev.cards_channel_id == ROSTER and card.id not in bot.rooms[ROSTER].deleted
    assert rc.event_channel(reg, ev, "officers_channel") is None and "did not move" in said[0]


def test_a_move_cut_off_by_a_restart_heals_and_a_re_save_retries_it(reg, rs):
    bot = ChanBot(reg, rs)
    ev, _ms = live(reg, rs, bot)
    rc.reschedule(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara")  # written; the process stopped before the move
    assert rc.pending_moves(reg, ev) == ["sheet_channel"] and rs.events[ev.key].channel_id == SIGNUP
    assert rc.reschedule_refusal(reg, rs, ev, None, {"sheet_channel": str(ALT_SHEET)}) is None, "an unfinished move is never 'already'"
    run(bot.finish_channel_moves(reg))
    ev = rs.events[ev.key]
    assert ev.channel_id == ALT_SHEET and not rc.pending_moves(reg, ev)
    n = len(bot.rooms[ALT_SHEET].sent)
    run(bot.finish_channel_moves(reg))
    assert len(bot.rooms[ALT_SHEET].sent) == n, "idempotent"
    # a re-save of the same channel finishes an unfinished move too
    ev2_bot = ChanBot(reg, rs)
    rc.reschedule(reg, rs, ev, timing={"loot_log_channel": str(ALT_LOOT), "officers_channel": str(ALT_CARDS)}, by="Kara")
    assert set(rc.pending_moves(reg, ev)) == {"loot_log_channel", "officers_channel"}
    run(ev2_bot.reschedule_event(reg, rs, ev, timing={"officers_channel": str(ALT_CARDS)}, by="Kara"))
    assert not rc.pending_moves(reg, ev)


def test_a_mixed_edit_keeps_the_warning_that_it_locks_at_once(reg, rs):
    from oibot_gm import consequences as cq

    reg.config.signup_channel_id, reg.config.roster_channel_id = SIGNUP, ROSTER
    ev = open_from(reg, rs, sched(reg))
    hours = (ev.start - reg.now_local()).total_seconds() / 3600 + 24  # the lock time raised past now
    timing = {"lock_hours_before": round(hours), "nudge": False, "confirm": False, "loot_log_channel": str(ALT_LOOT)}
    assert rc.reschedule_refusal(reg, rs, ev, None, timing) is None
    lines = cq.reschedule_consequence(reg, rs, ev, None, timing)
    assert len(lines) <= 3 and any("locks within a minute" in x for x in lines), lines
    box = cq.reschedule_box(reg, rs, ev, None, timing, edit=True)
    assert any(x.startswith("Loot log: ") for x in box["diff"]), "what moves is on its own diff line"


def test_a_lock_waits_for_a_move_still_running(reg, rs):
    import asyncio

    bot = ChanBot(reg, rs)
    ev, _ms = live(reg, rs, bot)

    async def go():
        gate = asyncio.Event()
        task = asyncio.get_running_loop().create_task(gate.wait())
        bot.__dict__.setdefault("_move_finishing", {})[(reg.key, ev.key)] = task
        try:
            with pytest.raises(RegistryError, match="still moving"):
                await bot.lock_run(reg, rs, ev, by="scheduler")
        finally:
            gate.set()
            await task
    run(go())
    assert rs.events[ev.key].state == "open"


def test_the_edge_cases_a_legacy_channel_the_lock_lines_words_and_a_part_ended_lineup(reg, rs):
    from datetime import datetime

    bot = ChanBot(reg, rs)
    ev, ms = live(reg, rs, bot)
    ev.layout = [[m.display_name for m in ms[:5]], [m.display_name for m in ms[5:10]]]
    rs.save(ev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False))
    ev = rs.events[ev.key]
    ev.confirm_by = (datetime.now(ev.start.tzinfo) - timedelta(minutes=5)).isoformat()  # the confirm-by has passed
    rs.save(ev, "confirm-by past (test)")
    run(bot.reschedule_event(reg, rs, ev, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    line = bot.rooms[rs.events[ev.key].lock_line[0]].lines()[-1]
    assert "confirm in your DMs" not in line, line
    # a lineup whose first raid has ended while a later raid plays still changes its channels
    unlock_hyjal(reg)
    reg.set_schedule("pair", {"kind": "weekly", "slots": "Tue 18:30", "follow": {"raid": FOLLOW, "rosters": 1}}, "t", raid=LEAD)
    lead = rc.open_run(reg, rs, LEAD, first_slot(reg, LEAD), by="t", schedule="pair")
    later = rc.later_parts(rs, lead)[0]
    later.channel_id = SIGNUP  # an older file's later raid carrying the sheet's channel
    lead.state, later.state = "done", "locked"
    rs.save(later, "legacy (test)")
    rs.save(lead, "ended (test)")
    assert rc.reschedule_refusal(reg, rs, later, None, {"loot_log_channel": str(ALT_LOOT)}) is None


def test_a_later_raids_legacy_channel_follows_the_sheet(reg, rs):
    unlock_hyjal(reg)
    bot = ChanBot(reg, rs)
    reg.set_schedule("pair", {"kind": "weekly", "slots": "Tue 18:30", "follow": {"raid": FOLLOW, "rosters": 1}}, "t", raid=LEAD)
    lead = run(bot.open_run_and_post(reg, rs, LEAD, first_slot(reg, LEAD), by="t", schedule="pair"))
    later = rc.later_parts(rs, lead)[0]
    later.channel_id = lead.channel_id
    rs.save(later, "legacy (test)")
    run(bot.reschedule_event(reg, rs, lead, timing={"sheet_channel": str(ALT_SHEET)}, by="Kara"))
    assert rs.events[later.key].channel_id == ALT_SHEET


def test_the_clash_reads_where_the_cards_really_go(reg):
    reg.config.signup_channel_id, reg.config.roster_channel_id, reg.config.ops_channel_id = SIGNUP, None, OPS
    sid = sched(reg)
    with pytest.raises(RegistryError, match="officers' cards can't go"):
        reg.set_schedule(sid, {"sheet_channel": str(OPS)}, "t")  # no roster channel: the cards go to ops


def test_a_signup_channel_without_thread_permissions_is_a_warning_not_an_error(reg):
    from test_setup import SetupBot

    bot = SetupBot(reg)
    bot.add(50, "raids", view_channel=True, send_messages=True, embed_links=True)  # no thread permissions
    reg.config.signup_channel_id = 50
    run(bot.check_channel_perms(reg))
    (lvl, text), = [(lvl, t) for _k, lvl, t in bot.ops.lines]
    assert lvl == "warn" and "lock and cancel lines" in text, text


def test_the_new_event_preview_reads_the_forms_own_sheet_channel(reg, rs, web_bot):
    from test_raid_night import H

    reg.config.signup_channel_id = None
    when = (reg.now_local() + timedelta(days=4)).replace(hour=13, minute=15).strftime("%Y-%m-%d %H:%M")
    p = web_bot.c.post(f"/api/raid/{RID}/open/preview", json={"when": when, "channels": {"sheet_channel": str(ALT_SHEET)}}, headers=H()).json()
    assert p["channel"] == str(ALT_SHEET) or p["channel"] == ALT_SHEET
    assert "No signup channel" not in p["posts"]


def test_plain_text_run_open_takes_the_channels(reg, rs):
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    bot = ChanBot(reg, rs)
    when = (reg.now_local() + timedelta(days=4)).replace(hour=13, minute=15).strftime("%Y-%m-%d %H:%M")
    op = ConfigOp(op="run_open", target=RID, value=when, path=f"sheet_channel <#{ALT_SHEET}>, loot_log_channel <#{ALT_LOOT}>")
    run(configops.apply_async(reg, op, "Jon", True, bot=bot))
    ev = next(iter(rs.events.values()))
    assert rc.event_channel(reg, ev, "sheet_channel") == ALT_SHEET and rc.event_channel(reg, ev, "loot_log_channel") == ALT_LOOT
    assert ev.channel_id == ALT_SHEET


def test_a_discord_picker_warns_at_pick_time_when_the_bot_cant_post(reg, rs, monkeypatch):
    import discord
    from test_reschedule import OFFICER_ID
    from test_setup import Chan, FakeGuild, pick_widget
    from types import SimpleNamespace
    from wizard_harness import Harness

    from oibot_gm import discord_registry, flows_config, flows_schedule  # noqa: F401
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_officer", lambda *a, **k: True)
    sid = sched(reg)
    bot = ChanBot(reg, rs)
    mute = Chan(ALT_CARDS, "alt-officers", FakeGuild(reg.config.discord_guild_id), view_channel=True, send_messages=True)
    bot.rooms[ALT_CARDS] = mute
    h = Harness(reg, rs, user=SimpleNamespace(id=OFFICER_ID, display_name="Officer", mention="@Officer"), client=bot)
    run(FLOWS["schedule"](h.command(), reg, raid=RID))
    run(h.pick("Pick a schedule", sid))
    run(h.pick("Change", "channels"))
    run(h.pick("Channels", "officers_channel"))
    run(pick_widget(h, discord.ui.ChannelSelect, "Pick the channel", mute))
    assert "I can't post in #alt-officers" in h.text


def test_a_gone_channel_is_not_reported_before_the_client_is_ready(reg, rs):
    import asyncio

    bot = ChanBot(reg, rs)
    ev = open_from(reg, rs, sched(reg, sheet_channel=str(GONE)))
    bot.is_ready = lambda: False

    async def twice():
        bot.sheet_channel(reg, ev)
        await asyncio.sleep(0)
        assert not [t for _l, t in bot.ops.lines if "gone or closed" in t], "a cache not ready yet is no evidence"
        bot.is_ready = lambda: True
        bot.sheet_channel(reg, ev)
        await asyncio.sleep(0)
    run(twice())
    assert len([t for _l, t in bot.ops.lines if "gone or closed" in t]) == 1
