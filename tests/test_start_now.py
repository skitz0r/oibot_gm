"""Issue 14, Task 6: Start now, and the loot End gap. `rc.start_now` moves a locked event's start to this minute (later
raids by the same amount) through reschedule's writes, EXCEPT every confirm-by stays: an unanswered confirmation keeps
its deadline and no seat is freed (the owner's ruling). After it End is allowed, Reschedule and Unlock are refused.
`RaidMixin.start_now` tells as Reschedule does (the `moved` notice, cards edited, no DM). One box,
`consequences.start_now_box`, on every surface: the site's POST /api/run/{key}/start, Events › More and the sheet card,
the Discord lock card's Start now, End's "hasn't started" refusal and /raid start, plain text's run_start (always
reviewed) and MCP's start_now. `LootMixin.end_run`, POST /api/loot/{run}/end and MCP end_loot refuse End before the
start like every other End, unless a loot council is open."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import join, open_test_run
from test_flows_raid import channel, officer  # noqa: F401 — the fixtures
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import H, NightBot, run, web_bot  # noqa: F401 — the routes' fixture
from test_loot_web import H as LH, env, w  # noqa: F401 — the loot routes' fixture
from test_unlock import locked_event

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.registry import RegistryError

ROOT = Path(__file__).resolve().parents[1]


def this_minute(reg) -> datetime:
    return reg.now_local().replace(second=0, microsecond=0)


def open_asks(reg, ev) -> list:
    return [a for _m, a in rc.open_placement_rows(reg, ev)]


def moved_notices(reg, ev) -> list[int]:
    return [m.discord_id for m in reg.members.values() for n in m.notices if n.get("kind") == "moved" and n.get("run") == ev.key]


# ---------------------------------------------------------------- the engine

def test_start_now_moves_the_start_to_this_minute_and_keeps_every_confirm_by(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    confirm_by, asks, seats = ev.confirm_by, len(open_asks(reg, ev)), sorted(p.signup_name for r in ev.all_rosters for p in r.selected)
    assert asks == 8 and rc.end_refusal(ev) == rc.END_NOT_STARTED
    before = this_minute(reg)
    mv = rc.start_now(reg, rs, ev, "Jon")
    ev = rs.events[ev.key]
    assert before <= ev.start <= this_minute(reg) and ev.start.second == 0, "the start is this minute"
    assert ev.confirm_by == confirm_by, "the confirm deadline is not moved"
    assert len(open_asks(reg, ev)) == asks and sorted(p.signup_name for r in ev.all_rosters for p in r.selected) == seats
    assert sorted(mv.told) == sorted(rc.answered_ids(ev)) and mv.told
    assert rc.end_refusal(ev) is None, "End is allowed now"
    assert rc.reschedule_refusal(reg, rs, ev, ev.start + timedelta(days=1), None) == rc.MOVE_STARTED
    assert rc.unlock_refusal(reg, rs, ev) == rc.UNLOCK_STARTED
    assert rc.start_now_refusal(reg, rs, ev) == rc.START_NOW_STARTED
    assert any("moved from" in line for line in ev.log), "one log line, as Reschedule writes"


def test_no_confirmation_expires_and_no_seat_is_freed_at_the_next_tick(reg, rs):
    """The trap: Reschedule would make confirm_by now (late_confirm_by) and the next tick would expire every unanswered
    ask. Start now keeps the deadline, so the scheduler's expiry step frees nobody."""
    bot, ev, ms = locked_event(reg, rs)
    seats = sorted(p.signup_name for r in ev.all_rosters for p in r.selected)
    run(bot.start_now(reg, rs, ev, by="Jon"))
    run(bot.scheduler_tick_guild(reg))
    ev = rs.events[ev.key]
    assert ev.state == "locked" and len(open_asks(reg, ev)) == 8, "every ask stays open"
    assert sorted(p.signup_name for r in ev.all_rosters for p in r.selected) == seats, "no seat freed"
    assert rc.expire_confirmation_asks(reg, rs, ev) == []


def test_a_lineup_moves_every_raid_by_the_same_amount(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    gap, by = fev.start - lead.start, {p.key: p.confirm_by for p in (lead, fev)}
    assert rc.start_now_refusal(reg, rs, fev) is None, "any raid of it stands for the event"
    rc.start_now(reg, rs, fev, "Jon")
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    assert lead.start == this_minute(reg) or lead.start == this_minute(reg) - timedelta(minutes=1)
    assert fev.start - lead.start == gap, "the later raid keeps its offset"
    assert {p.key: p.confirm_by for p in (lead, fev)} == by


def test_the_refusals(reg, rs):
    opened = open_test_run(reg, rs, days=5)
    assert rc.start_now_refusal(reg, rs, opened) == rc.START_NOW_OPEN
    bot, ev, ms = locked_event(reg, rs)
    assert rc.start_now_refusal(reg, rs, ev) is None
    assert rc.start_now_refusal(reg, rs, ev, at=ev.start) == rc.START_NOW_STARTED
    rc.end_run(reg, rs, ev, "t")
    assert rc.start_now_refusal(reg, rs, ev) == rc.START_NOW_DONE
    opened.state = "cancelled"
    assert rc.start_now_refusal(reg, rs, opened) == rc.START_NOW_CANCELLED
    with pytest.raises(RegistryError, match="still open"):
        run(bot.start_now(reg, rs, rs.events[open_test_run(reg, rs, days=6).key], by="Jon"))


def test_a_test_event_starts_now_too(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_test_run(reg, rs, days=0, soft=0.4, hard=0.3, confirm=0.1)  # a compressed rehearsal: starts in 3 h
    ev.test = True
    pool = reg.test_members()[:10]
    join(reg, rs, ev, pool)
    ev = rs.events[ev.key]
    ev.layout = [[m.display_name for m in pool[:5]]]
    rs.save(ev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[ev.key], by="Jon", fill=False))
    ev = rs.events[ev.key]
    assert rc.is_test_run(reg, ev) and rc.start_now_refusal(reg, rs, ev) is None
    run(bot.start_now(reg, rs, ev, by="Jon"))
    assert rc.end_refusal(rs.events[ev.key]) is None


# ---------------------------------------------------------------- the verb: who is told

def test_the_verb_tells_as_reschedule_does_and_dms_nobody(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    dms = len(bot.cards)
    line = run(bot.start_now(reg, rs, ev, by="Jon", via="web"))
    ev = rs.events[ev.key]
    assert line.startswith(f"{reg.raid_def(ev.instance)['name']} started now, at {reg.local12(ev.start)}.") and "Nobody was DMed." in line
    assert sorted(moved_notices(reg, ev)) == sorted(rc.answered_ids(ev)), "the moved notice, to everyone who answered"
    assert len(bot.cards) == dms, "no new DM"
    assert len([u for u in bot.updates if "started now by Jon" in u]) == 1, "one thread line"


# ---------------------------------------------------------------- the box

def test_the_box_says_what_changes_who_is_told_and_what_cant_be_undone(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    b = cq.start_now_box(reg, ev, rs)
    n = len(rc.answered_ids(ev))
    assert b["title"] == f"Start the {cq.event_title(reg, ev)} event now?" and b["verb"] == "Start now" and not b["danger"]
    assert b["sentences"] == ["The start moves to this minute.", f"The {n} who answered each get a notice, and 8 confirmation asks keep their deadline.",
                              "A started event can't be moved back or edited."]
    for a in open_asks(reg, ev):
        a["answer"] = "yes"
    assert cq.start_now_box(reg, ev, rs)["sentences"][1] == f"The {n} who answered each get a notice, and nobody gets a DM."
    for s in b["sentences"]:
        assert s[0].isupper() and s.endswith(".") and s.count(".") == 1 and len(s.split()) <= 20, s
        assert not any(ch in s for ch in "();:—"), s
    rc.start_now(reg, rs, ev, "t")
    b = cq.start_now_box(reg, ev, rs)
    assert b["verb"] is None and b["sentences"] == [rc.START_NOW_STARTED]


# ---------------------------------------------------------------- the site

def test_the_route_asks_first_refuses_a_stale_box_and_starts(reg, rs, web_bot):
    bot, ev, ms = locked_event(reg, rs)
    c, url = web_bot.c, f"/api/run/{ev.key}/start"
    b = c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert b == cq.start_now_box(reg, ev, rs) and rs.events[ev.key].start == ev.start, "the preview writes nothing"
    open_asks(reg, rs.events[ev.key])[0]["answer"] = "yes"  # someone confirmed while the box was open
    r = c.post(url, json={"seen": b["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["error"] == CONFIRM_WORDS["changed"]
    r = c.post(url, json={"seen": r.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200 and "started now" in r.json()["message"], r.text
    assert rc.end_refusal(rs.events[ev.key]) is None
    r = c.post(url, json={"preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["sentences"] == [rc.START_NOW_STARTED]


def test_the_run_json_offers_start_now_until_the_start(reg, rs, web_bot):
    bot, ev, ms = locked_event(reg, rs)
    j = web_bot.c.get(f"/api/run/{ev.key}", headers=H()).json()
    assert j["may_start"] is True and j["may_end"] is False and j["end_note"] == rc.END_NOT_STARTED
    rc.start_now(reg, rs, ev, "t")
    j = web_bot.c.get(f"/api/run/{ev.key}", headers=H()).json()
    assert j["may_start"] is False and j["may_end"] is True
    opened = open_test_run(reg, rs, days=5)
    assert web_bot.c.get(f"/api/run/{opened.key}", headers=H()).json()["may_start"] is False


def test_the_pages_print_the_servers_box():
    src = ROOT / "frontend" / "src"
    more = (src / "components" / "events" / "EventMore.tsx").read_text()
    assert "/start`" in more and 'key="start"' in more and "L.start" in more and "may_start" in more
    sheet = (src / "components" / "board" / "SheetCard.tsx").read_text()
    assert "/start`" in sheet and "L.start" in sheet and "e.may_start" in sheet
    tonight = (src / "components" / "loot" / "Tonight.tsx").read_text()
    assert "run.end_note" in tonight and "run.may_start" in tonight and "/start`" in tonight


# ---------------------------------------------------------------- the loot End gap

def test_loot_end_before_the_start_is_refused_on_the_route_the_verb_and_plain_text(w):
    ev = w.ev
    r = w.c.post(f"/api/loot/{ev.key}/end", json={"preview": True}, headers=LH)
    assert r.status_code == 400 and r.json()["error"] == rc.END_NOT_STARTED and r.json()["box"]["verb"] is None
    r = w.c.post(f"/api/loot/{ev.key}/end", json={}, headers=LH)  # MCP's end_loot posts exactly this
    assert r.status_code == 400 and r.json()["error"] == rc.END_NOT_STARTED and w.rs.events[ev.key].state == "locked"
    with pytest.raises(RegistryError, match="hasn't started"):
        run(w.bot.end_run(w.reg, w.rs, ev, by="Jon"))
    assert w.bot.end_box(w.reg, w.rs, ev)["sentences"] == [rc.END_NOT_STARTED]
    from oibot_gm import loot_ops

    op = ConfigOp(op="loot", field="end", target=ev.key)
    with pytest.raises(RegistryError, match="hasn't started"):
        loot_ops.describe(w.reg, op)
    assert rc.END_NOT_STARTED in configops.describe(w.reg, op), "the plain-text diff says so before Apply"
    row = next(x for x in w.c.get("/api/loot", headers=LH).json()["runs"] if x["key"] == ev.key)
    assert row["end_note"] == rc.END_NOT_STARTED and row["may_start"] is True, "Loot › Tonight: the refusal, Start now beside it"
    rc.start_now(w.reg, w.rs, ev, "t")
    row = next(x for x in w.c.get("/api/loot", headers=LH).json()["runs"] if x["key"] == ev.key)
    assert row["end_note"] is None and row["may_start"] is False
    r = w.c.post(f"/api/loot/{ev.key}/end", json={}, headers=LH)
    assert r.status_code == 200 and w.rs.events[ev.key].state == "done", r.text


def test_loot_end_with_a_council_open_still_ends_before_the_start(w):
    assert w.c.post(f"/api/loot/{w.ev.key}/open", json={}, headers=LH).status_code == 200
    assert rc.end_refusal(w.ev) == rc.END_NOT_STARTED, "before the start"
    row = next(x for x in w.c.get("/api/loot", headers=LH).json()["runs"] if x["key"] == w.ev.key)
    assert row["end_note"] is None, "a council open: End decides, as end_decide has it"
    r = w.c.post(f"/api/loot/{w.ev.key}/end", json={}, headers=LH)
    assert r.status_code == 200 and w.rs.events[w.ev.key].state == "done", r.text


def test_mcp_start_now_posts_the_route(monkeypatch):
    from oibot_gm import mcp_server

    sent = []

    class Api:
        def post(self, path, body=None):
            sent.append((path, body))
            return {"message": "Started."}

    monkeypatch.setattr(mcp_server, "api", lambda guild=None: Api())
    assert list(inspect.signature(mcp_server.start_now).parameters) == ["run", "guild"]
    assert mcp_server.start_now("bd-1") == "Started." and sent == [("/api/run/bd-1/start", None)]


def test_mcp_start_now_reaches_the_engine_through_the_site(reg, rs, web_bot):
    _bot, ev, ms = locked_event(reg, rs)
    r = web_bot.c.post(f"/api/run/{ev.key}/start", json={}, headers=H())  # MCP sends no `seen`
    assert r.status_code == 200 and rc.end_refusal(rs.events[ev.key]) is None, r.text
    r = web_bot.c.post(f"/api/run/{ev.key}/start", json={}, headers=H())
    assert r.status_code == 400 and r.json()["error"] == rc.START_NOW_STARTED


# ---------------------------------------------------------------- Discord

def _night(reg, rs):
    from test_raid_night_flows import night, officer_user

    h, bot = night(reg, rs, user=officer_user())

    async def start_now(reg_, rs_, ev_, by, by_id=None, *, via="discord"):
        bot.calls.append(("start_now", ev_.key, by, via))
        rc.start_now(reg_, rs_, ev_, by)
        return "Started it."
    bot.start_now = start_now
    return h, bot


def test_the_lock_card_start_now_is_the_box_then_starts(reg, rs):
    from oibot_gm.raid_buttons import RunButton

    _bot, ev, ms = locked_event(reg, rs)
    h, bot = _night(reg, rs)
    run(RunButton(reg.key, ev.key, "start").callback(h.card_press()))
    b = cq.start_now_box(reg, ev, rs)
    assert h.buttons() == ["Start now", "Back"] and b["title"] in h.text and all(s in h.text for s in b["sentences"])
    assert not [c for c in bot.calls if c[0] == "start_now"], "nothing before the press"
    run(h.press("Start now"))
    assert ("start_now", ev.key, "Tester", "discord") in bot.calls and rc.end_refusal(rs.events[ev.key]) is None


def test_ends_hasnt_started_refusal_offers_start_now(reg, rs):
    from oibot_gm.raid_buttons import RunButton

    _bot, ev, ms = locked_event(reg, rs)
    h, bot = _night(reg, rs)
    run(RunButton(reg.key, ev.key, "end").callback(h.card_press()))
    assert h.buttons() == ["Start now", "Back"] and rc.END_NOT_STARTED in h.text
    run(h.press("Start now"))
    assert h.buttons() == ["Start now", "Back"] and all(s in h.text for s in cq.start_now_box(reg, ev, rs)["sentences"])
    run(h.press("Start now"))
    assert ("start_now", ev.key, "Tester", "discord") in bot.calls


def test_raid_start_opens_the_same_screen(reg, rs, officer, channel):  # noqa: F811
    from oibot_gm.wizard import FLOWS

    _bot, ev, ms = locked_event(reg, rs)
    h, bot = _night(reg, rs)
    run(FLOWS["raid_start"](h.command(), reg))
    assert h.buttons() == ["Start now", "Back"] and all(s in h.text for s in cq.start_now_box(reg, ev, rs)["sentences"])
    run(h.press("Start now"))
    assert rc.end_refusal(rs.events[ev.key]) is None


def test_raid_start_with_nothing_locked_says_so(reg, rs, officer, channel):  # noqa: F811
    from oibot_gm.wizard import FLOWS

    open_test_run(reg, rs)
    h, bot = _night(reg, rs)
    run(FLOWS["raid_start"](h.command(), reg))
    assert "nothing to start now" in h.text


def test_the_lock_card_carries_start_now_with_the_guild_key(reg, rs):
    from oibot_gm.raid_views import night_actions_row

    _bot, ev, ms = locked_event(reg, rs)
    row = night_actions_row(reg, ev).children
    assert len(row) == 5 and row[2].custom_id == f"runact.{reg.key}:{ev.key}:start" and not row[2].item.disabled
    rc.start_now(reg, rs, ev, "t")
    assert night_actions_row(reg, rs.events[ev.key]).children[2].item.disabled, "greyed once started"


def test_an_old_card_id_still_resolves():
    from oibot_gm.raid_buttons import RunButton

    assert RunButton.__discord_ui_compiled_template__.fullmatch("runact:bd-1:start")
    assert RunButton.__discord_ui_compiled_template__.fullmatch("runact.gk:bd-1:end")


# ---------------------------------------------------------------- plain text

class StartBot:
    def __init__(self, rs):
        self.raids, self.calls = type("S", (), {"store": lambda _s, _r: rs})(), []

    async def start_now(self, reg, rs, ev, by, by_id=None, *, via="discord", seen=None):
        self.calls.append(("start_now", ev.key, by, via))
        return "Started."


def test_plain_text_start_is_reviewed_and_reads_the_box(reg, rs):
    _bot, ev, ms = locked_event(reg, rs)
    op = ConfigOp(op="run_start", target=ev.key)
    assert configops.is_warn(reg, op), "always reviewed first"
    d = configops.describe(reg, op)
    assert d.endswith(" ".join(cq.start_now_box(reg, ev, rs)["sentences"])) and d.startswith("start ") and " now. " in d, d
    bot = StartBot(rs)
    run(configops.apply_async(reg, op, "Officer", False, bot=bot, via="chat"))
    assert bot.calls == [("start_now", ev.key, "Officer", "chat")]
    assert configops.OP_GROUP["run_start"] == "runs" and "run_start" in configops.RUN_VERBS_AUDITED
    rc.end_run(reg, rs, ev, "t")
    with pytest.raises(RegistryError, match="ended"):
        configops.describe(reg, op, strict=True)


def test_start_now_never_swallows_opening_a_sheet_or_a_loot_council():
    """Both ways in the parser's prompt: run_start names its own phrases and points the others at run_open and the
    loot council's open; run_open and the loot council never name a Start now phrase."""
    text = configops.SCHEMA_TEXT
    start = text[text.index("  run_start:"):].split("\n  run_")[0]
    opened = text[text.index("  run_open:"):].split("\n  run_")[0]
    loot = text[text.index("Loot council (officer"):].split("\n\n")[0]
    for phrase in ('"start tonight\'s raid now"', '"we\'re starting"', '"start the raid"'):
        assert phrase in start and phrase not in opened and phrase not in loot, phrase
    assert "run_open" in start and '"start a sheet"' in start and "loot field=open" in start and '"start the loot council"' in start
