"""Issue 14, Task 5: Unlock on every surface. One box, `consequences.unlock_box`: what changes (the event goes back to open
signup with its rosters on the board — the whole event when it has several raids), who is told (nobody gets a DM; the
fill asks it withdraws, else the confirmation asks that stay open), what follows (Auto Lock is turned off, so an officer
locks it again). The site's POST /api/run/{key}/unlock (preview → box, `seen` → 409 when it changed, a refusal with its
box), Events › More and the sheet card, the Discord lock card's Unlock and /raid unlock (the same decide screen), plain
text's run_unlock (always reviewed, never confused with run_reopen) and MCP's unlock_run."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from conftest import join, open_test_run
from test_flows_raid import channel, officer  # noqa: F401 — the fixtures
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import H, NightBot, run, web_bot  # noqa: F401 — the routes' fixture
from test_unlock import locked_event

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.registry import RegistryError

ROOT = Path(__file__).resolve().parents[1]
HEAD = "The event goes back to open signup with its rosters on the board."


def fill_ask(ev, m):
    a = rc.FillAsk(discord_id=m.discord_id, display_name=m.display_name, kind="sub", role="dps", character=m.main.label,
                   spec=m.main.spec, reason="short 1")
    ev.fill_asks.append(a)
    return a


def answer_all(reg, ev):
    for _m, a in rc.open_placement_rows(reg, ev):
        a["answer"] = "yes"


def loot_file(rs, ev):
    d = rs.store.root / rs.key / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ev.key}.json").write_text("{}")
    return d / f"{ev.key}.json"


# ---------------------------------------------------------------- the box

def test_the_box_says_what_changes_who_is_told_and_what_follows(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    asks = len(rc.open_placement_rows(reg, ev))
    assert asks == 8, "the lock asked everyone rostered"
    b = cq.unlock_box(reg, ev, rs)
    assert b["title"] == f"Unlock {cq.event_title(reg, ev)}?" and b["verb"] == "Unlock" and not b["danger"]
    assert b["sentences"] == [HEAD, "Nobody gets a DM, and 8 confirmation asks stay open.", cq.UNLOCK_AGAIN]
    for _m, a in rc.open_placement_rows(reg, ev)[1:]:
        a["answer"] = "yes"
    assert cq.unlock_box(reg, ev, rs)["sentences"][1] == "Nobody gets a DM, and 1 confirmation ask stays open."
    answer_all(reg, ev)
    assert cq.unlock_box(reg, ev, rs)["sentences"][1] == "Nobody gets a DM."
    fill_ask(ev, ms[9])
    assert cq.unlock_box(reg, ev, rs)["sentences"][1] == "Nobody gets a DM, and the one person asked to fill a seat sees their ask withdrawn."
    fill_ask(ev, ms[8])
    ev.fill_asks[0].answer = None
    assert cq.unlock_box(reg, ev, rs)["sentences"][1] == "Nobody gets a DM, and the 2 people asked to fill a seat see their ask withdrawn."


def test_the_people_asked_to_fill_are_counted_once_each(reg, rs):
    """One member holding fill asks on both raids of a lineup is one person in the box."""
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    answer_all(reg, lead)
    fill_ask(lead, ms[20])
    fill_ask(fev, ms[20])
    told = cq.unlock_box(reg, lead, rs)["sentences"][1]
    assert told == "Nobody gets a DM, and the one person asked to fill a seat sees their ask withdrawn.", told
    b = cq.unlock_box(reg, lead, rs)
    fill_ask(fev, ms[21])
    assert cq.unlock_box(reg, lead, rs)["fp"] != b["fp"]


def test_withdrawn_fill_asks_win_over_confirmation_asks_kept(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    fill_ask(ev, ms[9])
    b = cq.unlock_box(reg, ev, rs)
    assert b["sentences"][1].endswith("sees their ask withdrawn.") and "confirmation" not in " ".join(b["sentences"])
    done = rc.unlock(reg, rs, ev, "Jon")
    assert done.asks_kept == 8 and done.fills_withdrawn == 1, "the box read the counts the unlock then had"


def test_a_lineup_unlocks_whole_and_the_box_says_so(reg, rs):
    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]
    answer_all(reg, lead)
    answer_all(reg, fev)
    b = cq.unlock_box(reg, fev, rs)  # any raid of it stands for the event
    assert b == cq.unlock_box(reg, lead, rs)
    assert b["sentences"][0] == "The whole event goes back to open signup, with every raid's rosters on the board."


def test_the_box_refuses_in_one_sentence(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    f = loot_file(rs, ev)
    b = cq.unlock_box(reg, ev, rs)
    assert b["verb"] is None and b["sentences"] == [rc.UNLOCK_LOOT]
    f.unlink()
    opened = open_test_run(reg, rs)
    assert cq.unlock_box(reg, opened, rs)["sentences"] == [rc.UNLOCK_OPEN]
    rc.end_run(reg, rs, ev, "t")
    assert cq.unlock_box(reg, ev, rs)["sentences"] == [rc.UNLOCK_DONE]


def test_every_sentence_reads_aloud(reg, rs):
    """Every case of the box: nothing out, confirmation asks open, fill asks out (one person, two), a lineup, refusals."""
    bot, ev, ms = locked_event(reg, rs)
    boxes = [cq.unlock_box(reg, ev, rs)]
    answer_all(reg, ev)
    boxes.append(cq.unlock_box(reg, ev, rs))
    fill_ask(ev, ms[9])
    boxes.append(cq.unlock_box(reg, ev, rs))
    fill_ask(ev, ms[8])
    boxes.append(cq.unlock_box(reg, ev, rs))
    loot = loot_file(rs, ev)
    boxes.append(cq.unlock_box(reg, ev, rs))
    loot.unlink()
    boxes.append(cq.unlock_box(reg, open_test_run(reg, rs, days=6), rs))
    lead, fev, pool = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in pool[5:8]]]
    rs.save(fev, "board (test)")
    run(NightBot(reg, rs).lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    boxes.append(cq.unlock_box(reg, rs.events[fev.key], rs))
    rc.end_run(reg, rs, ev, "t")
    boxes.append(cq.unlock_box(reg, ev, rs))
    assert len({b["fp"] for b in boxes}) == len(boxes), "eight cases, eight boxes"
    for b in boxes:
        for s in b["sentences"]:
            assert s[0].isupper() and s.endswith(".") and s.count(".") == 1 and len(s.split()) <= 20, s
            assert not any(ch in s for ch in "();:—"), s


# ---------------------------------------------------------------- the site

def test_the_route_asks_first_refuses_a_stale_box_and_unlocks(reg, rs, web_bot):
    bot, ev, ms = locked_event(reg, rs)
    c, url = web_bot.c, f"/api/run/{ev.key}/unlock"
    b = c.post(url, json={"preview": True}, headers=H()).json()["box"]
    assert b == cq.unlock_box(reg, ev, rs) and rs.events[ev.key].state == "locked", "the preview writes nothing"
    fill_ask(rs.events[ev.key], ms[9])  # an ask went out while the box was open
    r = c.post(url, json={"seen": b["fp"]}, headers=H())
    assert r.status_code == 409 and r.json()["error"] == CONFIRM_WORDS["changed"] and rs.events[ev.key].state == "locked"
    r = c.post(url, json={"seen": r.json()["box"]["fp"]}, headers=H())
    assert r.status_code == 200 and rs.events[ev.key].state == "open", r.text
    assert "back to open signup" in r.json()["message"]
    r = c.post(url, json={"preview": True}, headers=H())
    assert r.status_code == 400 and r.json()["box"]["sentences"] == [rc.UNLOCK_OPEN]


def test_the_run_json_offers_unlock_or_says_why_not(reg, rs, web_bot):
    bot, ev, ms = locked_event(reg, rs)
    j = web_bot.c.get(f"/api/run/{ev.key}", headers=H()).json()
    assert j["may_unlock"] is True and j["unlock_note"] is None
    f = loot_file(rs, ev)
    j = web_bot.c.get(f"/api/run/{ev.key}", headers=H()).json()
    assert j["may_unlock"] is False and j["unlock_note"] == rc.UNLOCK_LOOT
    f.unlink()
    opened = open_test_run(reg, rs, days=5)
    j = web_bot.c.get(f"/api/run/{opened.key}", headers=H()).json()
    assert j["may_unlock"] is False and j["unlock_note"] is None, "an open event offers no Unlock at all"


def test_both_pages_print_the_servers_box():
    src = ROOT / "frontend" / "src"
    more = (src / "components" / "events" / "EventMore.tsx").read_text()
    assert "/unlock`" in more and 'key="unlock"' in more and "L.unlock" in more and "unlock_note" in more
    assert more.count('key="unlock"') == 1, "once, for the whole event"
    sheet = (src / "components" / "board" / "SheetCard.tsx").read_text()
    assert "/unlock`" in sheet and "L.unlock" in sheet and "useConfirmBox" in sheet and "seen: fp" in sheet
    assert "e.unlock_note" in sheet and "!e.may_unlock" in sheet


# ---------------------------------------------------------------- Discord

def _night(reg, rs):
    from test_raid_night_flows import night, officer_user

    h, bot = night(reg, rs, user=officer_user())

    async def unlock_run(reg_, rs_, ev_, by, by_id=None, *, via="discord"):
        bot.calls.append(("unlock_run", ev_.key, by, via))
        rc.unlock(reg_, rs_, ev_, by)
        return "Unlocked it."
    bot.unlock_run = unlock_run
    return h, bot


def test_the_lock_card_unlock_is_the_box_then_unlocks(reg, rs):
    from oibot_gm.raid_buttons import RunButton

    _bot, ev, ms = locked_event(reg, rs)
    h, bot = _night(reg, rs)
    run(RunButton(reg.key, ev.key, "unlock").callback(h.card_press()))
    b = cq.unlock_box(reg, ev, rs)
    assert h.buttons() == ["Unlock", "Back"] and b["title"] in h.text and all(s in h.text for s in b["sentences"])
    assert not [c for c in bot.calls if c[0] == "unlock_run"], "nothing before the press"
    run(h.press("Unlock"))
    assert ("unlock_run", ev.key, "Tester", "discord") in bot.calls and rs.events[ev.key].state == "open"


def test_a_refused_unlock_is_the_box_with_back_alone(reg, rs):
    from oibot_gm.raid_buttons import RunButton

    _bot, ev, ms = locked_event(reg, rs)
    loot_file(rs, ev)
    h, bot = _night(reg, rs)
    run(RunButton(reg.key, ev.key, "unlock").callback(h.card_press()))
    assert h.buttons() == ["Back"] and rc.UNLOCK_LOOT in h.text


def test_raid_unlock_opens_the_same_screen(reg, rs, officer, channel):  # noqa: F811
    from oibot_gm.wizard import FLOWS

    _bot, ev, ms = locked_event(reg, rs)
    h, bot = _night(reg, rs)
    run(FLOWS["raid_unlock"](h.command(), reg))
    assert h.buttons() == ["Unlock", "Back"] and all(s in h.text for s in cq.unlock_box(reg, ev, rs)["sentences"])
    run(h.press("Unlock"))
    assert rs.events[ev.key].state == "open"


def test_raid_unlock_with_nothing_locked_says_so(reg, rs, officer, channel):  # noqa: F811
    from oibot_gm.wizard import FLOWS

    open_test_run(reg, rs)
    h, bot = _night(reg, rs)
    run(FLOWS["raid_unlock"](h.command(), reg))
    assert "nothing to unlock" in h.text


def test_the_lock_card_carries_unlock_beside_end_with_the_guild_key(reg, rs):
    from oibot_gm.raid_views import night_actions_row

    _bot, ev, ms = locked_event(reg, rs)
    ids = [b.custom_id for b in night_actions_row(reg, ev).children]
    assert ids[-2:] == [f"runact.{reg.key}:{ev.key}:unlock", f"runact.{reg.key}:{ev.key}:end"]
    assert not night_actions_row(reg, ev).children[-2].item.disabled, "before the start it can be pressed"


def test_the_lock_card_greys_unlock_while_a_loot_council_is_open(reg, rs, monkeypatch):
    from oibot_gm import raid_views

    _bot, ev, ms = locked_event(reg, rs)
    monkeypatch.setattr(raid_views, "boss_progress", lambda reg_, ev_: {"done": 0, "total": 0, "rosters": {}, "all_down": False})
    assert raid_views.night_actions_row(reg, ev).children[-2].item.disabled


def test_a_later_raids_lock_card_greys_unlock_by_the_first_raids_start(reg, rs):
    from datetime import datetime

    from oibot_gm.raid_views import night_actions_row

    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    fev = rs.events[fev.key]
    assert rc.is_follow(fev) and not night_actions_row(reg, fev).children[-2].item.disabled
    now = datetime.now(fev.start.tzinfo)
    fev.starts = int((fev.start - now).total_seconds() // 60) + 30  # its first raid began half an hour ago; it hasn't
    assert fev.start > now
    assert night_actions_row(reg, fev).children[-2].item.disabled, "greyed by the FIRST raid's start"


# ---------------------------------------------------------------- plain text and MCP

class UnlockBot:
    def __init__(self, rs):
        self.raids, self.calls = type("S", (), {"store": lambda _s, _r: rs})(), []

    async def unlock_run(self, reg, rs, ev, by, by_id=None, *, via="discord", seen=None):
        self.calls.append(("unlock_run", ev.key, by, via))
        return "Unlocked."


def test_plain_text_unlock_is_reviewed_and_reads_the_box(reg, rs):
    _bot, ev, ms = locked_event(reg, rs)
    op = ConfigOp(op="run_unlock", target=ev.key)
    assert configops.is_warn(reg, op), "always reviewed first"
    d = configops.describe(reg, op)
    assert d.endswith(" ".join(cq.unlock_box(reg, ev, rs)["sentences"])) and d.startswith("unlock "), d
    bot = UnlockBot(rs)
    run(configops.apply_async(reg, op, "Officer", False, bot=bot, via="chat"))
    assert bot.calls == [("unlock_run", ev.key, "Officer", "chat")]
    assert configops.OP_GROUP["run_unlock"] == "runs" and "run_unlock" in configops.RUN_VERBS_AUDITED


def test_unlock_and_reopen_are_never_confused(reg, rs):
    """Both ways: an unlock of an ended event is refused (Reopen is for that), a reopen of a locked one is refused, and
    the parser's prompt tells them apart by the words people use."""
    _bot, ev, ms = locked_event(reg, rs)
    with pytest.raises(RegistryError) as e:
        configops.describe(reg, ConfigOp(op="run_reopen", target=ev.key), strict=True)
    assert str(e.value).endswith("is locked, not ended, so unlock it to change its roster."), e.value
    rc.end_run(reg, rs, ev, "t")
    with pytest.raises(RegistryError) as e:
        configops.describe(reg, ConfigOp(op="run_unlock", target=ev.key), strict=True)
    assert str(e.value) == configops.UNLOCK_ENDED
    for x in (configops.UNLOCK_ENDED, configops.REOPEN_LOCKED.format(event="Barrow Deeps Wed 9 Dec 10 PM")):
        assert x.endswith(".") and x.count(".") == 1 and len(x.split()) <= 20 and not any(c in x for c in "();:—"), x
    assert "Unlock" in configops.describe(reg, ConfigOp(op="run_unlock", target=ev.key)), "listed as refused, by its name"
    text = configops.SCHEMA_TEXT
    unlock = text[text.index("  run_unlock:"):].split("\n  run_")[0]
    reopen = text[text.index("  run_reopen:"):].split("\n  run_")[0]
    for phrase in ('"unlock tonight\'s raid"', '"reopen the roster for Tuesday"', '"undo the lock"'):
        assert phrase in unlock and phrase not in reopen, phrase
    assert '"undo the end"' in reopen and "run_unlock" in reopen and "run_reopen" in unlock


def test_a_reviewed_unlock_is_refused_when_the_event_changed_before_apply(reg, rs):
    """Plain text carries the box the review showed (configops.board_revs → op_rev, the site's `seen`): a fill ask sent
    between the review and Apply makes the unlock refuse (RUN_CHANGED), nothing written; the same box applies."""
    bot, ev, ms = locked_event(reg, rs)
    op = ConfigOp(op="run_unlock", target=ev.key)
    revs = configops.board_revs(reg, [op], rs)
    key = configops.op_run_key(reg, op, rs)
    assert configops.op_rev(op, key, revs) == cq.unlock_box(reg, ev, rs)["fp"]
    fill_ask(rs.events[ev.key], ms[9])
    with pytest.raises(RegistryError, match="changed this event"):
        run(configops.apply_async(reg, op, "Officer", True, bot=bot, rev=configops.op_rev(op, key, revs)))
    assert rs.events[ev.key].state == "locked"
    revs = configops.board_revs(reg, [op], rs)
    run(configops.apply_async(reg, op, "Officer", True, bot=bot, rev=configops.op_rev(op, key, revs)))
    assert rs.events[ev.key].state == "open"


def test_mcp_unlock_run_posts_the_route(monkeypatch):
    from oibot_gm import mcp_server

    sent = []

    class Api:
        def post(self, path, body=None):
            sent.append((path, body))
            return {"message": "Unlocked."}

    monkeypatch.setattr(mcp_server, "api", lambda guild=None: Api())
    assert list(inspect.signature(mcp_server.unlock_run).parameters) == ["run", "guild"]
    assert mcp_server.unlock_run("bd-1") == "Unlocked." and sent == [("/api/run/bd-1/unlock", None)]


def test_mcp_unlock_reaches_the_engine_through_the_site(reg, rs, web_bot):
    _bot, ev, ms = locked_event(reg, rs)
    r = web_bot.c.post(f"/api/run/{ev.key}/unlock", json={}, headers=H())  # MCP sends no `seen`
    assert r.status_code == 200 and rs.events[ev.key].state == "open", r.text
    r = web_bot.c.post(f"/api/run/{ev.key}/unlock", json={}, headers=H())
    assert r.status_code == 400 and r.json()["error"] == rc.UNLOCK_OPEN



def test_unlock_and_start_now_decide_the_officer_live():
    """As End's route (api_loot.writer): the Unlock and Start now routes decide the officer NOW (live_officer, PRIV_LIVE)
    through live_body, so an officer demoted since the login's lookup can't press them. (The live check itself is
    web/app.py's, pinned by the guide library's demoted-officer test.)"""
    src = (ROOT / "src" / "oibot_gm" / "web" / "api.py").read_text()
    gate = src[src.index("    async def live_body(request: Request):"):]
    gate = gate[:gate.index("\n\n")]
    assert "await live_officer(v)" in gate and 'HTTPException(403, "Officers only.")' in gate
    for route in ('"/api/run/{key}/unlock")', '"/api/run/{key}/start")'):
        body_ = src[src.index(route):]
        body_ = body_[:body_.index("@app.")]
        assert "v, d = await live_body(request)" in body_ and "await body(request" not in body_, route


def test_a_reviewed_start_now_carries_its_box_too(reg, rs):
    bot, ev, ms = locked_event(reg, rs)
    op = ConfigOp(op="run_start", target=ev.key)
    revs = configops.board_revs(reg, [op], rs)
    key = configops.op_run_key(reg, op, rs)
    seen = configops.op_rev(op, key, revs)
    assert seen == cq.start_now_box(reg, ev, rs)["fp"]
    with pytest.raises(RegistryError, match="changed this event"):
        run(bot.start_now(reg, rs, ev, "Officer", seen="0" * 12))
    assert rs.events[ev.key].start == ev.start


# ---------------------------------------------------------------- final round: a later raid's card, the clock's warnings

def test_a_later_raids_card_greys_start_now_and_unlock_when_the_first_raid_has_ended(reg, rs):
    from oibot_gm.raid_views import night_actions_row

    bot = NightBot(reg, rs)
    lead, fev, ms = _pair_with_lead_board(reg, rs)
    fev.layout = [[m.display_name for m in ms[5:8]]]
    rs.save(fev, "board (test)")
    run(bot.lock_run(reg, rs, rs.events[lead.key], by="Jon", fill=False))
    lead, fev = rs.events[lead.key], rs.events[fev.key]

    def greyed(row):
        return {b.item.custom_id.rsplit(":", 1)[-1]: b.item.disabled for b in row.children}

    g = greyed(night_actions_row(reg, fev, rs))
    assert g["start"] is False and g["unlock"] is False, "before anything starts both can be pressed"
    rc.end_run(reg, rs, lead, "t")
    assert rc.start_now_refusal(reg, rs, fev) is not None and rc.unlock_refusal(reg, rs, fev) is not None
    g = greyed(night_actions_row(reg, fev, rs))
    assert g["start"] is True and g["unlock"] is True, "the press would be refused: greyed, read for the event"


def test_the_clocks_failing_lock_warns_the_ops_feed_once_per_failure(reg, rs):
    from test_lock_box_choice import ten

    bot = NightBot(reg, rs)
    ev, _ms = ten(reg, rs, placed=0)  # people joined, nobody on the board: the lock as it stands fails

    def warns():
        return [t for lvl, t in bot.ops.lines if "lock failed" in t]
    for _ in range(3):  # the clock retries every 15 minutes (an officer may still place someone)
        run(bot.lock_run(reg, rs, rs.events[ev.key], fill=False))
    ev = rs.events[ev.key]
    assert ev.state == "open" and ev.lock_error == rc.LOCK_EMPTY_BOARD
    assert len(warns()) == 1, warns()
    assert sum("failed" in u for u in bot.updates) == 1, "and one thread line"
    ev.lock_error = rc.SOLVER_WORDS["infeasible"]  # the last failure was another one: this one is new again
    rs.save(ev, "another failure before (test)")
    run(bot.lock_run(reg, rs, rs.events[ev.key], fill=False))
    assert len(warns()) == 2, warns()
