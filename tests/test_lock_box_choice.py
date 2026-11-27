"""Issue 14, Task 3: the lock box and every surface's choice. A board that is not complete offers one choice,
PAGE_WORDS["lock_fill"] ("Auto-fill empty roster slots"), unticked: a person's lock rosters the board AS IT STANDS unless
they tick it. The box leads with the open slots per roster and says what the lock it describes does; an empty board
refuses the unticked lock in one sentence (the choice stays to tick). A complete board's box is today's, with no choice.
The site's route, the Discord screen (Lock as it stands · Lock and auto-fill), plain text ("lock and fill …") and MCP
(`autofill`) all default to as it stands. Pausing or resuming Automated Fill Requests acts on every raid of the event."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from conftest import join
from test_bench_last import DPS, base_eight, people, schedule_event
from test_lock_as_it_stands import _pair_with_lead_board
from test_raid_night import H, NightBot, run, web_bot  # noqa: F401 — the routes' fixture

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.constants import CONFIRM_WORDS, PAGE_WORDS

ROOT = Path(__file__).resolve().parents[1]


def ten(reg, rs, placed: int = 6):
    """A schedule's event (Barrow Deeps, one roster of ten): ten joined, `placed` of them on the board."""
    ev = schedule_event(reg, rs)
    ms = base_eight(reg) + people(reg, 2, DPS)
    join(reg, rs, ev, ms)
    ns = [m.display_name for m in ms]
    ev.layout = [ns[:min(placed, 5)], ns[5:placed]] if placed else None
    rs.save(ev, "board (test)")
    return rs.events[ev.key], ms


# ---------------------------------------------------------------- the box

def test_a_part_filled_board_offers_the_choice_unticked(reg, rs):
    ev, ms = ten(reg, rs, placed=6)
    b = cq.lock_box(reg, ev, rs)
    assert b["choice"] == {"label": "Auto-fill empty roster slots", "value": False} and b["verb"] == "Lock"
    assert PAGE_WORDS["lock_fill"] == "Auto-fill empty roster slots"
    assert b["sentences"] == ["The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq.LOCK_PAUSE]
    assert cq.LOCK_PAUSE == "Automated Fill Requests pause until an officer resumes them from the event."
    f = cq.lock_box(reg, ev, rs, fill=True)
    assert f["choice"] == {"label": "Auto-fill empty roster slots", "value": True}
    assert f["sentences"] == ["The roster has 4 open slots.",
                              "Everyone on the board keeps their place, and the lock fills the open slots from Not placed.",
                              f"Everyone rostered gets {cq.CONFIRM_DM}."]
    assert f["fp"] != b["fp"], "the fingerprint covers the choice"
    assert f["title"] == b["title"]


def test_as_it_stands_with_asks_off_or_paused_says_who_is_asked(reg, rs):
    ev, ms = ten(reg, rs, placed=6)
    ev.autofill_paused = True
    b = cq.lock_box(reg, ev, rs)
    assert b["sentences"] == ["The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq._lock_deadline(reg, ev)], "paused already: the deadline"
    ev.autofill_paused = False
    ms[0].dm_opt_out = True
    assert cq.lock_box(reg, ev, rs)["sentences"][1:] == [
        "Only the 6 people on the board are rostered, and 1 of them sees the ask on the site.", cq.LOCK_PAUSE]


def test_a_name_kept_on_off_the_board_is_named(reg, rs):
    ev, ms = ten(reg, rs, placed=6)
    ev.pins[str(ms[8].discord_id)] = "in"
    assert rc.lock_left_out(reg, rs, ev) == [ms[8].display_name]
    b = cq.lock_box(reg, ev, rs)
    assert b["sentences"][0] == f"The roster has 4 open slots, and {ms[8].display_name} is not rostered."


def test_an_empty_board_warns_on_the_unticked_lock_and_keeps_the_choice(reg, rs):
    ev, ms = ten(reg, rs, placed=0)
    b = cq.lock_box(reg, ev, rs)
    assert b["verb"] == "Lock" and b["sentences"][1] == rc.LOCK_EMPTY_WARN_ONE and b["choice"]["value"] is False
    assert b["title"] == cq.lock_box(reg, ev, rs, fill=True)["title"]
    f = cq.lock_box(reg, ev, rs, fill=True)
    assert f["verb"] == "Lock" and f["sentences"][0] == "The lock builds the roster for Barrow Deeps from those who joined."
    assert len(f["sentences"]) == 3, "an empty board's fill box is today's box"


def test_a_board_with_no_open_slot_is_todays_box_with_no_choice(reg, rs):
    ev, ms = ten(reg, rs, placed=10)
    b = cq.lock_box(reg, ev, rs)
    assert "choice" not in b and b["sentences"][0] == "The board becomes the roster for Barrow Deeps."
    assert cq.lock_box(reg, ev, rs, fill=True) == b, "no choice: the fill asked for changes nothing"
    assert cq.lock_fill(reg, ev, rs, False) is True, "no open slot: the lock is today's"
    ev.layout = [ev.layout[0], ev.layout[1][:3]]
    assert cq.lock_fill(reg, ev, rs, False) is False and cq.lock_fill(reg, ev, rs, True) is True


def test_everyone_placed_with_spare_slots_still_asks_first(reg, rs):
    """The owner's ruling (fix round 1): open slots on a board where everyone who answered is placed still get the
    choice; ticked, the lock places nobody, so the box says what ticking does — Automated Fill Requests ask."""
    ev = schedule_event(reg, rs)
    ms = base_eight(reg)[:6]
    join(reg, rs, ev, ms)
    ev.layout = [[m.display_name for m in ms[:5]], [ms[5].display_name]]
    b = cq.lock_box(reg, ev, rs)
    assert b["choice"]["value"] is False and b["sentences"] == [
        "The roster has 4 open slots.", "Only the 6 people on the board are rostered and get the Confirm or Can't make it DM.", cq.LOCK_PAUSE]
    f = cq.lock_box(reg, ev, rs, fill=True)
    assert f["sentences"] == ["The roster has 4 open slots.", f"The 6 rostered get {cq.CONFIRM_DM}.", cq.LOCK_ASKS]
    assert cq.LOCK_ASKS == "Automated Fill Requests then ask others to take the open slots."
    assert cq.lock_fill(reg, ev, rs, False) is False


def test_an_event_nobody_joined_locks_empty(reg, rs):
    ev = schedule_event(reg, rs)
    b = cq.lock_box(reg, ev, rs)
    assert b["verb"] == "Lock" and b["sentences"][:2] == ["The roster has 10 open slots.",
                                                          "Nobody is on the board, so the roster locks empty."]


def test_two_raids_lead_with_the_raid_that_has_open_slots(reg, rs):
    lead, fev, ms = _pair_with_lead_board(reg, rs)  # Hyjal Summit (20) with 5 placed, then Barrow Deeps × 2 (10 each)
    name = cq._raid(reg, fev.instance)
    b = cq.lock_box(reg, lead, rs)
    assert b["verb"] == "Lock" and b["sentences"][0] == rc.LOCK_EMPTY_WARN.format(raid=name), "the empty raid is said first"
    fev.layout = [[m.display_name for m in ms[5:10]], [m.display_name for m in ms[10:15]]]
    b = cq.lock_box(reg, lead, rs)
    assert b["choice"]["value"] is False
    assert b["sentences"] == [f"The {cq._raid(reg, lead.instance)} roster has 15 open slots.",
                              "Only the people on each raid's board are rostered, and nobody is asked to confirm.", cq.LOCK_PAUSE], b
    fev.layout = [[m.display_name for m in ms[5:8]]]
    assert cq.lock_box(reg, lead, rs)["sentences"][0] == "The rosters have 22 open slots across 2 raids."
    # ticked, the open slots lead only where they are the board's for certain: the second Barrow Deeps roster is the solve's
    assert cq.lock_box(reg, lead, rs, fill=True)["sentences"][0] == "Everyone on the board keeps their place, and the lock fills the open slots from Not placed."


def test_the_slot_sentences():
    assert cq._slots_sentence([("X", [3])]) == "The roster has 3 open slots."
    assert cq._slots_sentence([("X", [1])]) == "The roster has 1 open slot."
    assert cq._slots_sentence([("X", [0, 3])]) == "Roster 2 has 3 open slots."
    assert cq._slots_sentence([("X", [3, 1])]) == "Roster 1 has 3 open slots and roster 2 has 1."
    assert cq._slots_sentence([("X", [3, 1, 2])]) == "Rosters 1, 2 and 3 have 6 open slots in all."
    assert cq._slots_sentence([("X", [0]), ("Y", [2, 1])]) == "The Y rosters have 3 open slots."
    assert cq._slots_sentence([("X", [1]), ("Y", [2])]) == "The rosters have 3 open slots across 2 raids."
    assert cq._slots_sentence([("X", [0]), ("Y", [0])]) is None


# ---------------------------------------------------------------- the site's route

def test_the_route_locks_as_it_stands_by_default(reg, rs, web_bot):  # noqa: F811
    ev, ms = ten(reg, rs, placed=6)
    layout = [list(g) for g in ev.layout]
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"preview": True}, headers=H())
    assert r.status_code == 200 and r.json()["box"] == cq.lock_box(reg, ev, rs)
    a = r.json()["box"]
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"preview": True, "fill": True}, headers=H())
    assert r.json()["box"] == cq.lock_box(reg, ev, rs, fill=True) and r.json()["box"]["fp"] != a["fp"]
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"seen": a["fp"], "fill": True}, headers=H())
    assert r.status_code == 409 and r.json()["error"] == CONFIRM_WORDS["changed"] and ev.state == "open", "the box pressed was the other choice"
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"seen": a["fp"]}, headers=H())
    assert r.status_code == 200, r.text
    ev = rs.events[ev.key]
    assert ev.state == "locked" and ev.lock_fill is False and not ev.lock_error
    assert [sorted(g) for r_ in ev.all_rosters for g in r_.groups][:2] == [sorted(g) for g in layout]
    assert ev.autofill_paused, "slots left open: the asks pause"


def test_the_route_locks_an_empty_board_as_it_stands(reg, rs, web_bot):  # noqa: F811
    """F3: an empty board is the officer's choice — the box warns, Lock stays pressable, the roster locks empty."""
    ev, ms = ten(reg, rs, placed=0)
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={"preview": True}, headers=H())
    b = r.json()["box"]
    assert r.status_code == 200 and b["verb"] == "Lock" and b["choice"]["value"] is False and rc.LOCK_EMPTY_WARN_ONE in b["sentences"]
    r = web_bot.c.post(f"/api/run/{ev.key}/lock", json={}, headers=H())
    assert r.status_code == 200, r.text
    ev = rs.events[ev.key]
    assert ev.state == "locked" and not ev.seated() and not ev.lock_error


def test_preview_lock_shows_the_roster_for_the_choice(reg, rs, web_bot):  # noqa: F811
    ev, ms = ten(reg, rs, placed=6)
    r = web_bot.c.post(f"/api/run/{ev.key}/lock/preview", json={}, headers=H())
    assert r.status_code == 200, r.text
    j = r.json()
    assert sum(len(g) for ro in j["board"]["rosters"] for g in ro["groups"]) == 6
    assert j["box"] == cq.lock_box(reg, ev, rs)
    ev.layout = None  # an empty board previews as the empty roster it locks (F3)
    r = web_bot.c.post(f"/api/run/{ev.key}/lock/preview", json={}, headers=H())
    assert r.status_code == 200 and not any(g for ro in r.json()["board"]["rosters"] for g in ro["groups"]), r.text


# ---------------------------------------------------------------- Discord: two verbs in place of the checkbox

def _card(reg, rs, ev):
    from test_raid_night_flows import night, officer_user

    from oibot_gm.raid_buttons import RunButton

    h, bot = night(reg, rs, user=officer_user())
    run(RunButton(reg.key, ev.key, "lock").callback(h.card_press()))
    return h, bot


def test_discord_offers_both_locks_when_the_board_has_open_slots(reg, rs):
    ev, ms = ten(reg, rs, placed=6)
    h, bot = _card(reg, rs, ev)
    assert h.buttons() == ["Lock as it stands", "Lock and auto-fill", "Preview lock", "Back"]
    assert all(x in h.text for x in cq.lock_box(reg, ev, rs)["sentences"]), "the as-it-stands screen shows its own box"
    run(h.press("Lock and auto-fill"))
    assert h.buttons() == ["Lock and auto-fill", "Preview lock", "Back"]
    assert all(x in h.text for x in cq.lock_box(reg, ev, rs, fill=True)["sentences"]), "the fill screen shows its own box"
    run(h.press("Preview lock"))
    assert [c for c in bot.calls if c[0] == "lock_preview"][-1][3:] == (), "the fill screen previews the fill"
    run(h.press("Back"))
    run(h.press("Back"))
    assert h.buttons()[0] == "Lock as it stands"
    run(h.press("Preview lock"))
    assert [c for c in bot.calls if c[0] == "lock_preview"][-1][3:] == ("as it stands",)
    run(h.press("Lock as it stands"))
    assert [c[3:] for c in bot.calls if c[0] == "lock_run"] == [("as it stands",)]


def test_discord_offers_both_locks_on_an_empty_board_and_warns(reg, rs):
    ev, ms = ten(reg, rs, placed=0)
    h, bot = _card(reg, rs, ev)
    assert h.buttons() == ["Lock as it stands", "Lock and auto-fill", "Preview lock", "Back"] and rc.LOCK_EMPTY_WARN_ONE in h.text
    run(h.press("Lock as it stands"))
    assert [c[3:] for c in bot.calls if c[0] == "lock_run"] == [("as it stands",)], "the officer's choice: it locks empty"


def test_discord_a_complete_board_has_one_lock(reg, rs):
    ev, ms = ten(reg, rs, placed=10)
    h, bot = _card(reg, rs, ev)
    assert h.buttons() == ["Lock", "Preview lock", "Back"]
    run(h.press("Lock"))
    assert [c[3:] for c in bot.calls if c[0] == "lock_run"] == [()], "a complete board locks as it always did"


# ---------------------------------------------------------------- plain text and MCP

class LockBot:
    def __init__(self, rs):
        self.raids, self.calls = type("S", (), {"store": lambda _s, _r: rs})(), []

    async def lock_run(self, reg, rs, ev, by="scheduler", fill=True, by_person=False):
        self.calls.append(("lock_run", ev.key, by, fill))
        ev.state = "locked"
        return f"{ev.key}: locked by {by}"


def test_plain_text_locks_as_it_stands_unless_asked_to_fill(reg, rs):
    ev, ms = ten(reg, rs, placed=6)
    d = configops.describe(reg, ConfigOp(op="run_lock", target=ev.key))
    assert "as the board stands" in d, d
    d = configops.describe(reg, ConfigOp(op="run_lock", target=ev.key, value="fill"))
    assert "auto-fill" in d, d
    bot = LockBot(rs)
    run(configops.apply_async(reg, ConfigOp(op="run_lock", target=ev.key), "Officer", False, bot=bot))
    assert bot.calls == [("lock_run", ev.key, "Officer", False)]
    ev.state = "open"
    run(configops.apply_async(reg, ConfigOp(op="run_lock", target=ev.key, value="fill"), "Officer", False, bot=bot))
    assert bot.calls[-1] == ("lock_run", ev.key, "Officer", True)
    assert "lock and fill" in configops.SCHEMA_TEXT


def test_plain_text_locks_an_empty_board_as_it_stands_and_its_review_says_so(reg, rs):
    ev, ms = ten(reg, rs, placed=0)
    assert rc.LOCK_EMPTY_WARN_ONE in configops.describe(reg, ConfigOp(op="run_lock", target=ev.key)), "the review warns"
    bot = LockBot(rs)
    run(configops.apply_async(reg, ConfigOp(op="run_lock", target=ev.key), "Officer", False, bot=bot))
    assert bot.calls == [("lock_run", ev.key, "Officer", False)]


def test_mcp_lock_run_takes_autofill(monkeypatch):
    from oibot_gm import mcp_server

    sent = []

    class Api:
        def post(self, path, body=None):
            sent.append((path, body))
            return {"message": "ok"}

    monkeypatch.setattr(mcp_server, "api", lambda guild=None: Api())
    assert inspect.signature(mcp_server.lock_run).parameters["autofill"].default is False
    mcp_server.lock_run("bd-1")
    mcp_server.lock_run("bd-1", autofill=True)
    assert sent == [("/api/run/bd-1/lock", {"fill": False}), ("/api/run/bd-1/lock", {"fill": True})]


# ---------------------------------------------------------------- pause and resume: every raid of the event

def test_pause_and_resume_act_on_every_raid_of_the_event(reg, rs):
    bot = NightBot(reg, rs)
    first, later, _ms = _pair_with_lead_board(reg, rs)
    line = run(bot.set_autofill_paused(reg, rs, first, True, by="Jon"))
    assert rs.events[first.key].autofill_paused and rs.events[later.key].autofill_paused, line
    assert rs.events[later.key].autofill_paused_by == "Jon" and "every raid" in line
    line = run(bot.set_autofill_paused(reg, rs, later, False, by="Jon"))
    assert not rs.events[first.key].autofill_paused and not rs.events[later.key].autofill_paused, line
    first.autofill_paused = True  # one raid paused alone (stored before this change): one press resumes both
    run(bot.set_autofill_paused(reg, rs, rs.events[later.key], False, by="Jon"))
    assert not rs.events[first.key].autofill_paused and not rs.events[later.key].autofill_paused


# ---------------------------------------------------------------- the frontend

def test_one_lock_box_with_the_choice_on_both_pages():
    src = ROOT / "frontend" / "src"
    lb = (src / "components" / "events" / "LockBox.tsx").read_text()
    assert ".choice.label" in lb and "<Checkbox" in lb and "replace(b)" in lb and "/preview`" in lb
    assert "seen: fp, fill: fillOf()" in lb, "the press carries the choice of the box on screen"
    assert "if (n !== seq) return null" in lb and "useSyncExternalStore(store.sub, store.get)" in lb, "only the latest answer lands; no stale copy"
    more = (src / "components" / "events" / "EventMore.tsx").read_text()
    assert more.count("/pause`") == 2 and 'key="pause"' in more, "Pause / Resume once, for the whole event"
    sheet = (src / "components" / "board" / "SheetCard.tsx").read_text()
    events = (src / "pages" / "Events.tsx").read_text()
    assert "function LockModal" not in sheet and "useLockBox" in sheet and "useLockBox" in events


# ---------------------------------------------------------------- fix round 1: the parser and the review lines

@pytest.mark.parametrize("value,fill", [
    ("fill", True), ("and auto-fill", True), ("fill the empty slots", True), ("auto fill empty roster slots", True),
    ("Auto-Fill", True), ("lock and fill", True), ("autofill", True), ("true", True),
    (None, False), ("", False), ("as it stands", False), ("no fill", False), ("don't fill", False), ("without auto-fill", False),
    ("whatever this means", False), ("board", False)])
def test_the_fill_wording_never_refuses(value, fill):
    assert configops._lock_fill_value(ConfigOp(op="run_lock", target="x", value=value)) is fill


def test_plain_text_lock_review_reads_the_box(reg, rs):
    ev, ms = ten(reg, rs, placed=6)
    ev.pins[str(ms[8].discord_id)] = "in"
    rs.save(ev, "pin (test)")
    d = configops.describe(reg, ConfigOp(op="run_lock", target=ev.key))
    want = " ".join(cq.lock_box(reg, rs.events[ev.key], rs)["sentences"])
    assert d.endswith("now as the board stands. " + want), d
    assert "is not rostered" in d and cq.LOCK_PAUSE in d


def test_plain_text_pause_review_names_the_whole_event(reg, rs):
    lead, fev, _ms = _pair_with_lead_board(reg, rs)
    lead.autofill_paused = True
    rs.save(lead, "paused (test)")
    d = configops.describe(reg, ConfigOp(op="run_pause", target=lead.key, value="pause"))
    assert d.startswith("every raid of ") and "already" not in d, "the later raid would still flip"
    for p in (lead, fev):
        p.autofill_paused = True
        rs.save(p, "paused (test)")
    assert "already paused" in configops.describe(reg, ConfigOp(op="run_pause", target=lead.key, value="pause"))
    r = configops.describe(reg, ConfigOp(op="run_pause", target=fev.key, value="resume"))
    assert r.startswith("every raid of ") and "fill asks may go out" in r and "Hyjal Summit and Barrow Deeps" in r, r
