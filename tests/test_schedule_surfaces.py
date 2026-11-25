"""Phase 3, Task 2: every surface names a schedule by its guild-unique id — plain-text and structured ops, the admin routes,
MCP, the site's schedule rows and their revs. A target with ':' (the older '<raid>:<schedule>') is refused in one sentence;
an edit of a schedule with open runs says those runs keep their old times (C10)."""
from __future__ import annotations

import inspect
import re
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import weekly
from test_officer_raids import officer_client, w  # noqa: F401 — fixtures

from oibot_gm import configops
from oibot_gm import consequences as cq
from oibot_gm import mcp_server as ms
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError

ROOT = Path(__file__).resolve().parents[1]
RID = "barrow_deeps"
CHANGE = "/api/ops/change"


def test_plain_text_sets_a_schedule_by_id(reg):
    weekly(reg, RID, "Tue 19:30", "tue")
    ops = configops.ops_from_fields(reg, "schedule", "tue", {"slots": "Wed 19:30"})
    assert [(o.op, o.target, o.field) for o in ops] == [("schedule_set", "tue", "slots")]
    configops.apply(reg, ops[0], "Officer", False)
    assert reg.schedule_entry("tue")["slots"] == ["Wed 19:30"]


def test_an_old_raid_colon_target_is_refused_in_one_sentence(reg):
    with pytest.raises(RegistryError, match="by its id alone"):
        configops.ops_from_fields(reg, "schedule", "barrow_deeps:tue", {"active": False})
    with pytest.raises(RegistryError, match="by its id alone"):
        configops.ops_from_fields(reg, "schedule_remove", "barrow_deeps:tue", {})
    weekly(reg, RID, "Tue 19:30", "tue")
    for op in (ConfigOp(op="schedule_set", target="barrow_deeps:tue", field="active", value="false"),
               ConfigOp(op="schedule_remove", target="barrow_deeps:tue")):
        with pytest.raises(RegistryError, match="by its id alone"):
            configops.apply(reg, op, "Officer", False)


def test_an_old_id_dot_setting_field_is_refused(reg):
    weekly(reg, RID, "Tue 19:30", "tue")
    with pytest.raises(RegistryError, match="setting alone"):
        configops.apply(reg, ConfigOp(op="schedule_set", target="tue", field="tue.active", value="false"), "Officer", False)
    assert reg.schedule_entry("tue")["active"] is True


def test_a_new_schedule_names_its_raid_and_merges_into_one_write(reg, store):
    ops = configops.ops_from_fields(reg, "schedule", "alt", {"raid": RID, "kind": "weekly", "slots": ["Sat 20:00"], "name": "Alt run"})
    assert ops[0].field == "raid" and {o.target for o in ops} == {"alt"}
    merged = configops.coalesce(ops)
    assert len(merged) == 1 and configops._merge_key(merged[0]) == ("schedule_set", "alt")
    d = configops.describe_ops(reg, ops)
    assert len(d.lines) == 1 and d.lines[0].error is None and d.lines[0].label == "Barrow Deeps · Alt run"
    configops.apply(reg, merged[0], "Officer", False)
    s = reg.schedule_entry("alt")
    assert s["raid"] == RID and s["slots"] == ["Sat 20:00"] and s["name"] == "Alt run"


def test_a_new_schedule_without_its_raid_is_refused(reg):
    (op,) = configops.coalesce(configops.ops_from_fields(reg, "schedule", "alt", {"kind": "weekly", "slots": "Sat 20:00"}))
    with pytest.raises(RegistryError, match="There is no schedule called alt"):
        configops.apply(reg, op, "Officer", False)


def test_a_structured_lineup_sets_the_raid_and_its_follow_on(reg):
    ops = configops.ops_from_fields(reg, "schedule", "late", {"lineup": [{"raid": RID, "rosters": 2}, {"raid": "onyxias_lair", "rosters": 1}],
                                                              "kind": "weekly", "slots": ["Thu 20:00"]})
    (op,) = configops.coalesce(ops)
    configops.apply(reg, op, "Officer", False)
    s = reg.schedule_entry("late")
    assert s["raid"] == RID and s["rosters"] == 2 and [e["raid"] for e in s["lineup"]] == [RID, "onyxias_lair"]


def test_the_admin_route_takes_an_id_alone(officer_client, reg):  # noqa: F811
    weekly(reg, RID, "Tue 19:30", "tue")
    r = officer_client.post("/api/admin/raid/schedule", json={"id": "tue", "fields": {"active": False}, "confirmed": True})
    if r.json().get("review_required"):  # a pause is reviewed first (D51): confirmed with the revs its preview returned
        r = officer_client.post("/api/admin/raid/schedule", json={"id": "tue", "fields": {"active": False}, "confirmed": True, "revs": r.json()["revs"]})
    assert r.status_code == 200 and reg.schedule_entry("tue")["active"] is False, r.text
    # an `instance` sent by a page not reloaded is ignored; a new schedule names its raid in `fields`
    r = officer_client.post("/api/admin/raid/schedule", json={"instance": "onyxias_lair", "id": "alt", "fields": {"raid": RID, "slots": ["Sat 20:00"]}, "confirmed": True})
    assert r.status_code == 200 and reg.schedule_entry("alt")["raid"] == RID, r.text
    body = {"id": "alt", "confirmed": True}
    r = officer_client.post("/api/admin/raid/schedule/remove", json=body)
    if r.json().get("review_required"):
        r = officer_client.post("/api/admin/raid/schedule/remove", json={**body, "revs": r.json()["revs"]})
    assert r.status_code == 200 and not any(s["id"] == "alt" for s in reg.all_schedules()), r.text


def test_a_schedule_rev_from_the_page_is_checked(officer_client, reg):  # noqa: F811
    weekly(reg, RID, "Tue 19:30", "tue")
    rev = next(s for r in officer_client.get("/api/raids").json()["raids"] for s in r["schedules"] if s["id"] == "tue")["rev"]
    assert rev == reg.config_rev("schedule:tue")
    reg.set_schedule("tue", {"name": "Someone else's"}, "Ana")
    r = officer_client.post(CHANGE, json={"changes": [{"kind": "schedule", "target": "tue", "fields": {"slots": ["Wed 20:00"]}}], "apply": True,
                                          "revs": {"config:schedule:tue": rev}})
    assert r.status_code == 409 and r.json()["changed"] == ["schedule:tue"], r.text
    assert reg.schedule_entry("tue")["slots"] == ["Tue 19:30"]


def test_schedule_json_carries_its_id_raids_and_rev(officer_client, reg):  # noqa: F811
    reg.set_schedule("late", {"kind": "weekly", "slots": "Thu 20:00", "follow": {"raid": "onyxias_lair", "rosters": 1}}, "t", raid=RID)
    rows = [s for r in officer_client.get("/api/raids").json()["raids"] for s in r["schedules"] if s["id"] == "late"]
    assert len(rows) == 1 and rows[0]["raids"] == [RID, "onyxias_lair"] and rows[0]["rev"] == reg.config_rev("schedule:late")


def test_a_schedule_edit_says_open_runs_keep_their_times(reg, rs):
    sid = weekly(reg, RID, "Tue 19:30", "tue")
    now = reg.now_local()
    start = next(t for t in rc.next_runs_of(reg, RID, reg.schedule_entry(sid), n=3) if t - now >= timedelta(hours=24))
    ev = rc.open_run(reg, rs, RID, start, by="t", schedule=sid)
    before = dict(reg.config.roster(ev.team))
    op = ConfigOp(op="schedule_set", target="tue", field="lock_hours_before", value="30")
    d = configops.describe_ops(reg, [op])
    assert f"The {reg.local12(start, '%a %d %b')} event is already open and keeps its timing." in d.consequences and d.review
    configops.apply(reg, op, "Officer", False)
    assert reg.schedule_def(RID, "tue")["lock_hours_before"] == 30
    assert reg.config.roster(ev.team)["cutoff_hard_hours"] == before["cutoff_hard_hours"]
    # a pause says what it says already; a name changes no time
    assert "The open event keeps its old times." not in configops.op_consequences(reg, ConfigOp(op="schedule_set", target="tue", field="name", value="Tue"))


def test_schedule_keeps_sentence():
    assert cq.schedule_keeps_sentence(0) is None
    assert cq.schedule_keeps_sentence(1) == "The open event keeps its old times."
    assert cq.schedule_keeps_sentence(3) == "The 3 open events keep their old times."


def test_mcp_schedule_tools_take_no_raid():
    sig = inspect.signature(ms.schedule_set)
    assert "raid" in sig.parameters and list(sig.parameters)[0] == "schedule"
    assert list(inspect.signature(ms.schedule_remove).parameters)[0] == "schedule"


def test_mcp_sends_the_id_and_a_new_schedules_raid(monkeypatch):
    sent = []
    monkeypatch.setattr(ms, "ops_change", lambda guild, changes, preview: sent.append(changes) or "ok")
    monkeypatch.setattr(ms, "get_raid", lambda raid, guild=None: {"id": RID})
    ms.schedule_set("alt", fields={"kind": "weekly", "slots": "Sat 20:00"}, raid="Barrow Deeps")
    ms.schedule_set("alt", "active", False)
    ms.schedule_remove("alt")
    assert sent == [[{"kind": "schedule", "target": "alt", "fields": {"kind": "weekly", "slots": "Sat 20:00", "raid": RID}}],
                    [{"kind": "schedule", "target": "alt", "fields": {"active": False}}],
                    [{"kind": "schedule_remove", "target": "alt"}]]


SPLITS_ALLOWED = {"old_key_raid", "lowered_id"}  # the migration's reader of an older key's raid, and the way back's id
CLASS_SPEC = re.compile(r"\s*cls,\s*(?:_,\s*)?spec\s*=")  # a 'Class:Spec' key read into its class and spec: no series key
SERIES_SPLIT = re.compile(r"\b(key|k|target|sid|old)\b[^\n]{0,12}?\)?\.(?:r?partition|split)\(\":\"")


def test_no_series_key_is_split_any_more():
    """No code splits a schedule key on ':' (partition or split) — a key reads through config.schedule_aliases. The two
    named migration helpers are the only readers of an older '<raid>:<schedule>' key. \b: a 'Class:Spec' key (sk) is
    no series key."""
    hits = []
    for f in sorted((ROOT / "src/oibot_gm").rglob("*.py")):
        fn = None
        for n, line in enumerate(f.read_text().splitlines(), 1):
            d = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
            if d:
                fn = d.group(1)
            if SERIES_SPLIT.search(line) and fn not in SPLITS_ALLOWED and not CLASS_SPEC.match(line):
                hits.append(f"{f.relative_to(ROOT)}:{n} ({fn}): {line.strip()}")
    assert not hits, "\n".join(hits)


def test_a_removed_schedules_name_never_shows_its_key(reg):
    from oibot_gm import wizard_opts

    assert wizard_opts.schedule_names(reg, ["gone"]) == ["A removed schedule"]


def test_a_reset_never_removes_a_schedule_added_after_its_preview(officer_client, reg):  # noqa: F811
    """Fix round 1: a raid reset checks `raid_all:<rid>` — the raid's changes AND the schedules it removes — so a schedule
    added after the preview is a 409 and stays; the raid's own rev still ignores schedules (a rules Save never 409s on one)."""
    from oibot_gm.registry import ConfigChanged

    weekly(reg, RID, "Tue 19:30", "tue")
    pv = officer_client.post(CHANGE, json={"changes": [{"kind": "raid_reset", "target": RID}]}).json()
    revs = pv["revs"]
    assert f"config:raid_all:{RID}" in revs, revs
    reg.set_schedule("late", {"kind": "weekly", "slots": "Thu 20:00"}, "Ana", raid=RID)  # someone adds one meanwhile
    r = officer_client.post(CHANGE, json={"changes": [{"kind": "raid_reset", "target": RID}], "apply": True, "confirmed": True, "revs": revs})
    assert r.status_code == 409 and f"raid_all:{RID}" in r.json()["changed"], r.text
    assert {s["id"] for s in reg.schedules(RID)} == {"tue", "late"}
    r = officer_client.post(CHANGE, json={"token": pv["token"], "apply": True})  # the preview's token carries the same rev
    assert not r.json().get("applied") and {s["id"] for s in reg.schedules(RID)} == {"tue", "late"}, r.text
    # the admin translator checks the rev it is sent (a reset is routine there: it applies when nothing changed)
    body = {"instance": RID, "confirmed": True}
    want = {f"config:raid_all:{RID}": reg.config_rev(f"raid_all:{RID}")}
    reg.set_schedule("sun", {"kind": "weekly", "slots": "Sun 20:00"}, "Ana", raid=RID)
    r = officer_client.post("/api/admin/raid/reset", json={**body, "revs": want})
    assert r.status_code == 409 and "sun" in {s["id"] for s in reg.schedules(RID)}, r.text
    with pytest.raises(ConfigChanged):  # the verb itself (Discord's reset screen sends the same rev)
        reg.clear_raid_override(RID, "x", rev=want[f"config:raid_all:{RID}"])
    # a schedule edit between a raid-rules preview and its Save never 409s the rules
    rules = reg.config_rev(f"raids:{RID}")
    reg.set_schedule("tue", {"name": "Tuesday"}, "Ana")
    r = officer_client.post(CHANGE, json={"changes": [{"kind": "raid", "target": RID, "fields": {"notes": "bring pots"}}], "apply": True,
                                          "confirmed": True, "revs": {f"config:raids:{RID}": rules}})
    assert r.status_code == 200 and r.json().get("applied"), r.text


def test_naming_a_raid_creates_and_never_rewrites_an_existing_schedule(reg):
    first = configops.coalesce(configops.ops_from_fields(reg, "schedule", "alt", {"raid": RID, "kind": "weekly", "slots": "Sat 20:00"}))[0]
    configops.apply(reg, first, "Ana", False)
    again = configops.coalesce(configops.ops_from_fields(reg, "schedule", "alt", {"raid": RID, "kind": "weekly", "slots": "Sun 21:00"}))[0]
    taken = "Another schedule is already called alt"  # a create (its kind named), its own raid too: never told to edit it
    assert taken in configops.describe(reg, again)
    with pytest.raises(RegistryError, match=taken):
        configops.apply(reg, again, "Bo", False)
    with pytest.raises(RegistryError, match=taken):  # a create-shaped lineup on an id in use: refused as it is translated
        configops.ops_from_fields(reg, "schedule", "alt", {"lineup": [{"raid": RID, "rosters": 1}], "kind": "weekly"})
    with pytest.raises(RegistryError, match="The schedule called alt already exists. To change it, name the schedule without a raid."):
        reg.set_schedule("alt", {"slots": "Sun 21:00"}, "Bo", raid=RID)  # plain text naming its own raid on an edit: how to edit it
    other = configops.coalesce(configops.ops_from_fields(reg, "schedule", "alt", {"raid": "hyjal_summit_forever", "kind": "weekly", "slots": "Sun 21:00"}))[0]
    with pytest.raises(RegistryError, match="Another schedule is already called alt"):  # another raid: a real clash
        configops.apply(reg, other, "Bo", False)
    assert reg.schedule_entry("alt")["slots"] == ["Sat 20:00"]
    configops.apply(reg, ConfigOp(op="schedule_set", target="alt", field="slots", value="Sun 21:00"), "Bo", False)  # an edit names no raid
    assert reg.schedule_entry("alt")["slots"] == ["Sun 21:00"]


def test_mcp_raid_reset_hands_its_preview_revs_back(monkeypatch):
    sent = []

    class Api:
        def post(self, path, body):
            sent.append(body)
            return {"described": {"lines": []}, "revs": {"config:raid_all:barrow_deeps": "abc"}, "applied": ["ok"]}

    monkeypatch.setattr(ms, "api", lambda guild=None: Api())
    monkeypatch.setattr(ms, "get_raid", lambda raid, guild=None: {"id": RID})
    pv = ms.raid_reset("Barrow Deeps", preview=True)
    assert pv["revs"] == {"config:raid_all:barrow_deeps": "abc"}
    ms.raid_reset("Barrow Deeps", revs=pv["revs"])
    assert sent[-1]["revs"] == {"config:raid_all:barrow_deeps": "abc"} and sent[-1]["confirmed"] is True
