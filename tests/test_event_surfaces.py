"""Phase 3, Task 7: the run verbs of Tasks 5 and 6 on every surface — plain text (run_reschedule / run_lineup and
run_open's later raids), the site's routes (POST /api/run/{key}/reschedule | lineup: preview → sentences + rev, then the
write; a member refused; a run changed since refused), MCP (reschedule_run / edit_lineup), and the parity matrix. Plus
the carried copy fixes: a person reads a run's name, never its random key; a schedule edit that names its own raid says
how to edit it; the Can't make it form never cuts a sentence; a cancel on a later raid after the first finished posts
nothing and promises no channel line."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import OWNER, join, weekly
from test_member_inbox_web import OFFICER, as_user, w  # noqa: F401 — the web fixture (the real RaidMixin verbs behind the app)
from test_raid_night import NightBot
from test_reschedule import open_one_off, real_people

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm import registry as rg
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError

RID, HYJAL, ONY = "barrow_deeps", "hyjal_summit_forever", "onyxias_lair"
OPENED = {"first_open": "2026-01-01T00:00"}


def run(coro):
    return asyncio.run(coro)


def wall(reg, t) -> str:
    """A start as the plain-text parser writes it: guild wall time 'YYYY-MM-DD HH:MM'."""
    return reg.local(t, "%Y-%m-%d %H:%M")


def opened_all(reg):
    for r in (RID, HYJAL, ONY):
        reg.set_raid_overrides(r, OPENED, "t")


# ---------------------------------------------------------------- plain text

def test_plain_text_reschedules(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    join(reg, rs, ev, real_people(reg, 3))
    new = ev.start + timedelta(days=1, hours=1)
    op = ConfigOp(op="run_reschedule", target=ev.key, field="start", value=wall(reg, new))
    line = configops.describe(reg, op)
    sentences = cq.reschedule_consequence(reg, rs, ev, new, None)
    assert line == f"Barrow Deeps {reg.local12(ev.start)}: " + " ".join(sentences), "the diff prints the confirm's own sentences"
    assert ev.key not in line, "a person reads the run's name, never its key"
    revs = configops.board_revs(reg, [op], rs)
    out = run(configops.apply_async(reg, op, "Officer", True, bot=bot, by_id=OWNER, rev=configops.op_rev(op, ev.key, revs), via="chat"))
    assert ev.start == new and out == f"Barrow Deeps now starts {reg.local12(new)}."
    assert bot.dms == {}, "nobody is DMed"


def test_a_start_and_timing_on_one_run_are_one_write(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=4)
    new = ev.start + timedelta(hours=2)
    ops = configops.coalesce([ConfigOp(op="run_reschedule", target=ev.key, field="start", value=wall(reg, new)),
                              ConfigOp(op="run_reschedule", target=ev.key, field="lock_hours_before", value="1")])
    assert len(ops) == 1, "several on one run coalesce"
    assert "with its own lock time" in configops.describe(reg, ops[0])
    run(configops.apply_async(reg, ops[0], "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    assert ev.start == new and reg.config.roster(ev.team)["cutoff_hard_hours"] == 1


def test_a_stale_diff_is_refused_and_nothing_moves(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    op = ConfigOp(op="run_reschedule", target=ev.key, field="start", value=wall(reg, ev.start + timedelta(hours=1)))
    revs = configops.board_revs(reg, [op], rs)
    run(bot.reschedule_event(reg, rs, ev, start=ev.start + timedelta(hours=3), by="Other", by_id=OWNER))  # someone else, meanwhile
    moved_to = ev.start
    with pytest.raises(RegistryError, match="Someone changed this run"):
        run(configops.apply_async(reg, op, "Officer", True, bot=bot, by_id=OWNER, rev=configops.op_rev(op, ev.key, revs), via="chat"))
    assert ev.start == moved_to


def test_a_wall_time_the_clocks_skip_or_repeat_is_refused(reg):
    reg.config.timezone = "America/New_York"
    with pytest.raises(RegistryError) as e:
        rc.move_start(reg, "2027-03-14 02:30")
    assert str(e.value) == rc.MOVE_GAP
    with pytest.raises(RegistryError) as e:
        rc.move_start(reg, "2027-11-07 01:30")
    assert str(e.value) == rc.MOVE_TWICE
    assert reg.local12(rc.move_start(reg, "2027-03-14 20:30")) == "Sun 14 Mar 8:30 PM"


def test_plain_text_edits_a_lineup_and_opens_one(reg, rs):
    assert rg.parse_lineup_text("barrow_deeps x2, onyxias_lair") == [{"raid": "barrow_deeps", "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}]
    assert rg.parse_lineup_text("none") == []
    opened_all(reg)
    bot = NightBot(reg, rs)
    start = (reg.now_local() + timedelta(days=4)).replace(hour=18, minute=30, second=0, microsecond=0)
    op = ConfigOp(op="run_open", target=HYJAL, value=wall(reg, start), text="barrow_deeps x2, onyxias_lair")
    assert "then Barrow Deeps ×2 at" in configops.describe(reg, op) and "then Onyxia" in configops.describe(reg, op)
    out = run(configops.apply_async(reg, op, "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    first = next(e for e in rs.live() if e.instance == HYJAL)
    assert out.startswith(f"opened Hyjal Summit {reg.local12(start)}") and first.key not in out
    assert [(e["raid"], e["rosters"]) for e in rc.run_lineup(reg, rs, first)] == [(HYJAL, 1), (RID, 2), (ONY, 1)]
    edit = ConfigOp(op="run_lineup", target=first.key, value="onyxias_lair")
    line = configops.describe(reg, edit)
    assert line.startswith("Hyjal Summit ") and " ".join(cq.lineup_consequence(reg, rs, first, [{"raid": HYJAL, "rosters": 1}, {"raid": ONY, "rosters": 1}])) in line
    run(configops.apply_async(reg, edit, "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [HYJAL, ONY]
    assert "A run can't hold the same raid twice." in configops.describe(reg, ConfigOp(op="run_lineup", target=first.key, value="onyxias_lair, onyxias_lair"))
    with pytest.raises(RegistryError) as e:
        run(configops.apply_async(reg, ConfigOp(op="run_lineup", target=first.key, value="onyxias_lair, onyxias_lair"), "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    assert str(e.value) == "A run can't hold the same raid twice."


def wide_reg(store, profile):
    """conftest's guild on a profile copy with three more raids (test_lineups' widened profile, one raid more): five
    raids, so a run of more than four can be asked for."""
    import copy

    from conftest import GUILD, TEST_MEMBERS

    from oibot_gm.registry import Registry

    p = copy.deepcopy(profile)
    for rid, name in (("extra_a", "Extra A"), ("extra_b", "Extra B"), ("extra_c", "Extra C")):
        p.raids[rid] = {**copy.deepcopy(p.raids[RID]), "id": rid, "name": name, "duration_hours": 2}
    r = Registry(store, GUILD, p)
    r.seed_test_members(TEST_MEMBERS, "t")
    for rid in r.profile.raids:
        r.set_raid_overrides(rid, OPENED, "t")
    return r


def test_plain_text_open_with_more_raids_than_a_run_holds_is_refused(store, profile, rs):
    reg = wide_reg(store, profile)
    start = (reg.now_local() + timedelta(days=4)).replace(hour=18, minute=30, second=0, microsecond=0)
    op = ConfigOp(op="run_open", target=HYJAL, value=wall(reg, start), text="barrow_deeps, extra_a, extra_b, extra_c")
    line = configops.describe(reg, op)
    assert line.endswith(": " + rg.LINEUP_MAX_WORDS), line
    with pytest.raises(RegistryError) as e:
        run(configops.apply_async(reg, op, "Officer", True, bot=NightBot(reg, rs), by_id=OWNER, via="chat"))
    assert str(e.value) == rg.LINEUP_MAX_WORDS and not rs.live()
    with pytest.raises(RegistryError) as e:
        configops.lineup_of_op(reg, HYJAL, "nope_raid")
    assert str(e.value) == rc.LINEUP_GONE.format(raid="nope_raid")
    with pytest.raises(RegistryError) as e:
        configops.lineup_of_op(reg, HYJAL, "hyjal_summit_forever")
    assert str(e.value) == "Hyjal Summit is the run's first raid. Pick another raid to follow it."


def test_a_plain_text_lineup_edit_keeps_a_kept_raids_own_start(reg, rs):
    """Barrow Deeps at its own start (5 h after Hyjal): adding Onyxia after it moves nobody; nobody gets a moved notice."""
    opened_all(reg)
    bot = NightBot(reg, rs)
    first = open_one_off(reg, rs, days=4, raid=HYJAL, lineup=[{"raid": RID, "rosters": 1, "starts": 300}])
    join(reg, rs, first, real_people(reg, 2))
    bd = rc.later_parts(rs, first)[0]
    was = bd.start
    run(configops.apply_async(reg, ConfigOp(op="run_lineup", target=first.key, value="barrow_deeps, onyxias_lair"), "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    assert bd.start == was and [e["starts"] for e in rc.run_lineup(reg, rs, first)][:2] == [0, 300]
    assert not any(n["kind"] == "moved" for m in reg.members.values() for n in m.notices), "nobody's night moved"
    # taken out of the middle: the raid after it now follows another raid, so it starts when that one ends (C12)
    run(configops.apply_async(reg, ConfigOp(op="run_lineup", target=first.key, value="onyxias_lair"), "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    hyjal_ends = round(float(reg.raid_def(HYJAL)["duration_hours"]) * 60)
    assert [e["starts"] for e in rc.run_lineup(reg, rs, first)] == [0, hyjal_ends]


def test_a_coalesced_start_and_timing_tell_each_person_once(reg, rs):
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=4)
    people = real_people(reg, 3)
    join(reg, rs, ev, people)
    new = ev.start + timedelta(hours=2)
    (op,) = configops.coalesce([ConfigOp(op="run_reschedule", target=ev.key, field="start", value=wall(reg, new)),
                                ConfigOp(op="run_reschedule", target=ev.key, field="autofill", value="off")])
    run(configops.apply_async(reg, op, "Officer", True, bot=bot, by_id=OWNER, via="chat"))
    assert [len([n for n in reg.members[m.discord_id].notices if n["kind"] == "moved"]) for m in people] == [1, 1, 1]


# ---------------------------------------------------------------- the site's routes

def test_the_routes_preview_then_write(w):
    reg, rs = w.reg, w.rs
    ev = open_one_off(reg, rs, days=3)
    new = ev.start + timedelta(days=1)
    h = as_user(w.c, OFFICER)
    r = w.c.post(f"/api/run/{ev.key}/reschedule", json={"start": reg.local(new, "%Y-%m-%dT%H:%M"), "preview": True}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sentences"] == cq.reschedule_consequence(reg, rs, ev, new, None) and body["rev"] and ev.start != new, "a preview writes nothing"
    r = w.c.post(f"/api/run/{ev.key}/reschedule", json={"start": reg.local(new, "%Y-%m-%dT%H:%M"), "rev": body["rev"]}, headers=h)
    assert r.status_code == 200 and ev.start == new, r.text
    r = w.c.post(f"/api/run/{ev.key}/reschedule", json={"start": reg.local(new + timedelta(hours=1), "%Y-%m-%dT%H:%M"), "rev": body["rev"]}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == rc.RUN_CHANGED and ev.start == new, "the rev the preview showed is stale now"
    past = reg.now_local() - timedelta(days=1)
    r = w.c.post(f"/api/run/{ev.key}/reschedule", json={"start": reg.local(past, "%Y-%m-%dT%H:%M")}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == rc.MOVE_PAST
    m = reg.test_members()[0]
    r = w.c.post(f"/api/run/{ev.key}/reschedule", json={"start": reg.local(new, "%Y-%m-%dT%H:%M"), "preview": True}, headers=as_user(w.c, m.discord_id))
    assert r.status_code == 403


def test_the_lineup_route_previews_then_writes(w):
    reg, rs = w.reg, w.rs
    opened_all(reg)
    first = open_one_off(reg, rs, days=4, raid=HYJAL, lineup=[{"raid": RID, "rosters": 1}])
    want = [{"raid": HYJAL, "rosters": 1}, {"raid": ONY, "rosters": 1}]
    h = as_user(w.c, OFFICER)
    r = w.c.post(f"/api/run/{first.key}/lineup", json={"lineup": want, "preview": True}, headers=h)
    assert r.status_code == 200 and r.json()["sentences"] == cq.lineup_consequence(reg, rs, first, want), r.text
    r = w.c.post(f"/api/run/{first.key}/lineup", json={"lineup": want, "rev": r.json()["rev"]}, headers=h)
    assert r.status_code == 200, r.text
    assert [e["raid"] for e in rc.run_lineup(reg, rs, first)] == [HYJAL, ONY]
    got = w.c.get(f"/api/run/{first.key}", headers=h).json()
    assert got["part"] == 0 and got["event"] == first.key and [e["raid"] for e in got["lineup"]] == [HYJAL, ONY]
    assert got["start_local"] == reg.local(first.start, "%Y-%m-%dT%H:%M") and got["cancel_tells_channel"] is True
    r = w.c.post(f"/api/run/{first.key}/lineup", json={"lineup": [{"raid": RID, "rosters": 1}]}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == rc.LINEUP_FIRST
    r = w.c.post(f"/api/run/{first.key}/lineup", json={"lineup": ["hyjal", 3]}, headers=h)
    assert r.status_code == 400, "a malformed lineup is a refusal, never a 500"
    second = open_one_off(reg, rs, days=6, raid=HYJAL)
    r = w.c.get("/api/run/Hyjal Summit", headers=h)  # two live Hyjal runs: named, never listed by key
    assert r.status_code == 400 and first.key not in r.text and second.key not in r.text and "Say which one." in r.text


def test_the_open_route_takes_later_raids(w):
    reg, rs = w.reg, w.rs
    opened_all(reg)
    start = (reg.now_local() + timedelta(days=4)).replace(hour=18, minute=30, second=0, microsecond=0)
    h = as_user(w.c, OFFICER)
    later = [{"raid": RID, "rosters": 2}, {"raid": ONY, "rosters": 1}]
    p = w.c.post(f"/api/raid/{HYJAL}/open/preview", json={"when": wall(reg, start), "lineup": later}, headers=h).json()
    assert [x["raid"] for x in p["later"]] == [RID, ONY]
    r = w.c.post(f"/api/raid/{HYJAL}/open", json={"when": wall(reg, start), "lineup": later}, headers=h)
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert key not in r.json()["message"] and r.json()["message"].startswith(f"Opened Hyjal Summit {reg.local12(start)}")
    assert [(e["raid"], e["rosters"]) for e in rc.run_lineup(reg, rs, rs.events[key])] == [(HYJAL, 1), (RID, 2), (ONY, 1)]


# ---------------------------------------------------------------- MCP and the matrix

def test_mcp_tools_exist():
    from oibot_gm import mcp_server as ms

    assert {"reschedule_run", "edit_lineup"} <= {t.name for t in asyncio.run(ms.mcp.list_tools())}


def test_mcp_tools_preview_then_send_the_rev():
    import httpx
    from test_mcp import TOKEN

    from oibot_gm import mcp_server as ms

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append((request.url.path, json.loads(request.content or b"{}")))
        body = seen[-1][1]
        return httpx.Response(200, json={"sentences": ["Barrow Deeps moves."], "rev": "r1"} if body.get("preview") else {"message": "moved"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        assert ms.reschedule_run("bd-abc234", when="2026-12-02 20:00", preview=True)["rev"] == "r1"
        assert ms.reschedule_run("bd-abc234", when="2026-12-02 20:00", rev="r1") == {"message": "moved"}
        assert seen[-1] == ("/api/run/bd-abc234/reschedule", {"preview": False, "start": "2026-12-02 20:00", "rev": "r1"})
        ms.edit_lineup("bd-abc234", [{"raid": RID, "rosters": 1}], rev="r1")
        assert seen[-1] == ("/api/run/bd-abc234/lineup", {"lineup": [{"raid": RID, "rosters": 1}], "rev": "r1"})
    finally:
        ms.use(None)


def test_the_matrix_lists_both_ops():
    assert set(configops.PARITY_PENDING) == {"run_reschedule", "run_lineup"}
    assert {"run_reschedule", "run_lineup"} <= configops.RUN_OPS and configops.OP_GROUP["run_reschedule"] == "runs"


# ---------------------------------------------------------------- carried copy fixes

def test_lines_a_person_reads_name_the_run_never_its_key(reg, rs):
    """(a) plain-text refusals and results, the lock's returned line, the pin diff, the non-strict refusal fallback,
    a confirmation's line and the timezone result: the run's name and time, never the random key."""
    bot = NightBot(reg, rs)
    ev = open_one_off(reg, rs, days=3)
    title = f"Barrow Deeps {reg.local12(ev.start)}"
    m = real_people(reg, 1)[0]
    pin = configops.describe(reg, ConfigOp(op="pin", target=ev.key, member=m.display_name, value="in"))
    assert pin == f"{title}: pin {m.display_name} to the roster (now not pinned)", pin
    refused = configops.describe(reg, ConfigOp(op="run_lineup", target=ev.key, value="barrow_deeps"))  # its own first raid named again
    assert refused.startswith(f"The raids of {title}: ") and ev.key not in refused and "run_lineup" not in refused, refused
    reg.add_placement_ask(m.discord_id, ev.team, m.main.label, by="t")
    _m, _a, line = reg.claim_placement(m.discord_id, ev.team, True, "t")  # the Confirm card, the Me page, confirm_for, the bench
    assert line == f"{m.display_name} confirmed {m.main.label} on {reg.config.roster(ev.team)['name']}" and ev.key not in line, line
    # the timezone moved with its runs: the result names them
    out = configops.apply(reg, ConfigOp(op="set", path="timezone", value="America/New_York", field="move"), "Owner", True, raids=rs)
    assert ev.key not in out and "Barrow Deeps" in out, out
    for op in (ConfigOp(op="run_lock", target=ev.key), ConfigOp(op="run_cancel", target=ev.key),
               ConfigOp(op="run_open", target=RID, value=wall(reg, ev.start))):
        line = configops.describe(reg, op)
        assert ev.key not in line, line
    assert title in configops.describe(reg, ConfigOp(op="run_lock", target=ev.key))
    ev.state = "locked"
    with pytest.raises(RegistryError) as e:
        run(bot.lock_run(reg, rs, ev, by="Officer"))
    assert ev.key not in str(e.value) and title in str(e.value)


def test_a_schedule_edit_naming_its_own_raid_says_how(reg):
    """(b) naming the schedule's own raid while editing it: refused, in words that say how to edit it; another raid's
    clash keeps ID_TAKEN."""
    weekly(reg, RID, "Tue 19:30", "alt")
    own = configops.coalesce([ConfigOp(op="schedule_set", target="alt", field="raid", value=RID),
                              ConfigOp(op="schedule_set", target="alt", field="slots", value="Wed 20:00")])[0]
    want = rg.SCHEDULE_EXISTS.format(sid="alt")
    assert want in configops.describe(reg, own) and "Another schedule" not in configops.describe(reg, own)
    with pytest.raises(RegistryError) as e:
        configops.apply(reg, own, "Ana", False)
    assert str(e.value) == want
    other = configops.coalesce([ConfigOp(op="schedule_set", target="alt", field="raid", value=HYJAL),
                                ConfigOp(op="schedule_set", target="alt", field="slots", value="Wed 20:00")])[0]
    assert rg.ID_TAKEN.format(sid="alt") in configops.describe(reg, other)


def test_a_create_shaped_lineup_on_an_existing_id_is_refused_unless_the_edit_carries_its_rev(reg):
    """A structured lineup with run times (create-shaped) on an id in use is never a rewrite: with the schedule's own
    first raid and no rev (MCP's schedule_set) it is told to save the two in turn; with another first raid it is a create
    clash, ID_TAKEN; with the schedule's own rev (the site's edit Save) it is the edit it looks like."""
    opened_all(reg)
    weekly(reg, RID, "Tue 19:30", "alt")
    draft = {"lineup": [{"raid": RID, "rosters": 1}, {"raid": ONY, "rosters": 1}], "slots": ["Fri 20:00"]}
    with pytest.raises(RegistryError) as e:
        configops.ops_from_fields(reg, "schedule", "alt", dict(draft))
    assert str(e.value) == "Raid nights already exists. Change its raids and its run times in two saves, one after the other."
    assert reg.schedule_entry("alt")["slots"] == ["Tue 19:30"]
    with pytest.raises(RegistryError) as e:
        configops.ops_from_fields(reg, "schedule", "alt", {"lineup": [{"raid": ONY, "rosters": 1}], "slots": ["Fri 20:00"]})
    assert str(e.value) == rg.ID_TAKEN.format(sid="alt") and reg.schedule_entry("alt")["slots"] == ["Tue 19:30"]
    revs = {"config:schedule:alt": reg.config_rev("schedule:alt")}
    (op,) = configops.coalesce(configops.ops_from_fields(reg, "schedule", "alt", dict(draft), revs=revs))
    configops.apply(reg, op, "Ana", False)
    assert reg.schedule_entry("alt")["slots"] == ["Fri 20:00"] and [e["raid"] for e in reg.schedule_entry("alt")["lineup"]] == [RID, ONY]


def test_a_new_schedule_save_on_a_taken_id_is_id_taken(reg):
    """The race Task 2 protects: officer B's new-schedule Save (it names its kind) a second after officer A's, same raid,
    is told the id is taken — never to edit (and so overwrite) A's schedule."""
    weekly(reg, RID, "Tue 19:30", "alt")
    with pytest.raises(RegistryError) as e:
        configops.ops_from_fields(reg, "schedule", "alt", {"lineup": [{"raid": RID, "rosters": 1}], "kind": "weekly", "slots": ["Sun 21:00"]})
    assert str(e.value) == rg.ID_TAKEN.format(sid="alt")
    (op,) = configops.coalesce(configops.ops_from_fields(reg, "schedule", "alt", {"raid": RID, "kind": "weekly", "slots": ["Sun 21:00"]}))
    assert rg.ID_TAKEN.format(sid="alt") in configops.describe(reg, op)
    with pytest.raises(RegistryError) as e:
        configops.apply(reg, op, "Bo", False)
    assert str(e.value) == rg.ID_TAKEN.format(sid="alt")
    with pytest.raises(RegistryError) as e:
        reg.set_schedule("alt", {"kind": "weekly", "slots": "Sun 21:00"}, "Bo", raid=RID)
    assert str(e.value) == rg.ID_TAKEN.format(sid="alt") and reg.schedule_entry("alt")["slots"] == ["Tue 19:30"]


def test_the_site_edits_an_existing_schedules_lineup(reg):
    """The schedule editor sends `lineup` for an existing schedule (no raid, no kind): an edit, never ID_TAKEN."""
    opened_all(reg)
    weekly(reg, HYJAL, "Tue 18:30", "tue")
    ops = configops.ops_from_fields(reg, "schedule", "tue", {"lineup": [{"raid": HYJAL, "rosters": 2}, {"raid": RID, "rosters": 1}, {"raid": ONY, "rosters": 1, "starts": 420}]})
    (op,) = configops.coalesce(ops)
    assert "then" in configops.describe(reg, op)
    configops.apply(reg, op, "Ana", False)
    assert [(e["raid"], e["rosters"]) for e in reg.schedule_entry("tue")["lineup"]] == [(HYJAL, 2), (RID, 1), (ONY, 1)]


def test_the_cant_form_hint_never_cuts_a_sentence(reg, rs):
    """(c) /raid out's hint fits Discord's 100 characters in whole sentences, three later raids included."""
    from oibot_gm.flows_raid import FORM_HINT_MAX, out_form_hint

    long = [type("P", (), {"instance": r})() for r in (RID, ONY, HYJAL)]
    for held in ([], long[:1], long):
        text = out_form_hint(reg, held)
        assert len(text) <= FORM_HINT_MAX and text.endswith("."), text


def test_a_cancel_on_a_later_raid_after_the_first_finished_posts_nothing(reg, rs):
    """(d) the first raid done: the cancel takes the raids still to play, offers no channel line and promises none."""
    opened_all(reg)
    bot = NightBot(reg, rs)
    first = open_one_off(reg, rs, days=3, raid=HYJAL, lineup=[{"raid": RID, "rosters": 1}, {"raid": ONY, "rosters": 1}])
    later = rc.later_parts(rs, first)
    first.state = "done"
    rs.save(first, "done")
    assert not rc.cancel_tells_channel(rs, later[1]) and rc.cancel_tells_channel(rs, first)
    line = configops.describe(reg, ConfigOp(op="run_cancel", target=later[1].key))
    assert "signup channel" not in line and "sheet" not in line.split(". ", 1)[1], line
    out = run(bot.cancel_run(reg, rs, later[1], "Officer", None, by_id=OWNER))
    assert "sheet" not in out and "signup channel" not in out and first.state == "done"
    assert all(p.state == "cancelled" for p in later)
