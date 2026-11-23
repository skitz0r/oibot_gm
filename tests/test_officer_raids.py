"""Officers edit raids and schedules everywhere (design D8 / D16 / C17): the site's one change route and the admin
translators, plain text's default policy, the raid wizard in Discord and MCP's set_minimum. Sheet timing is each
schedule's own (TIMING_ON_SCHEDULE on a raid); a raid's make-up is one minimum per role, class or spec (no upper limit)."""
from __future__ import annotations

import asyncio
import types

import pytest
from fastapi.testclient import TestClient
from test_flows_config import open_raid
from test_member_inbox_web import OFFICER, SECRET, TOKEN, WebNightBot, as_user
from wizard_harness import Harness

from oibot_gm import configops, help
from oibot_gm import flows_config  # noqa: F401 — registers the flows
from oibot_gm import mcp_server as ms
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import NO_UPPER_LIMIT, PLAIN_WEB, TIMING_ON_SCHEDULE, RegistryError
from oibot_gm.web import app as web

RID = "barrow_deeps"
CHANGE = "/api/ops/change"


@pytest.fixture
def w(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    bot = WebNightBot(reg, rs)
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg)


@pytest.fixture
def officer_client(w):
    w.c.headers.update(as_user(w.c, OFFICER))
    return w.c


@pytest.fixture
def member_client(w, reg):
    w.c.headers.update(as_user(w.c, reg.test_members()[0].discord_id))
    return w.c


def mcp_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(ms.mcp.list_tools())}


# ---------------------------------------------------------------- the site

@pytest.mark.parametrize("change", [
    {"kind": "raid", "target": RID, "fields": {"notes": "officers wrote this"}},
    {"kind": "schedule", "target": f"{RID}:tue", "fields": {"kind": "weekly", "slots": "Tue 19:30"}},
    {"kind": "comp", "target": RID, "fields": {"healer": {"min": 3}}},
])
def test_an_officer_saves_raid_and_schedule_changes_on_the_site(officer_client, change, reg):
    r = officer_client.post(CHANGE, json={"changes": [change], "confirmed": True, "apply": True})
    assert r.status_code == 200 and not r.json().get("refused"), r.text
    assert r.json().get("applied")


def test_the_admin_raid_routes_take_an_officer(officer_client, reg):
    r = officer_client.post("/api/admin/raid/schedule", json={"instance": RID, "id": "tue", "fields": {"kind": "weekly", "slots": "Tue 19:30"}, "confirmed": True})
    assert r.status_code == 200, r.text
    assert next(s for s in reg.schedules(RID) if s["id"] == "tue")["slots"] == ["Tue 19:30"]
    r = officer_client.post("/api/admin/raid", json={"instance": RID, "notes": "bring pots", "confirmed": True})
    assert r.status_code == 200 and reg.raid_def(RID)["notes"] == "bring pots"
    body = {"instance": RID, "id": "tue", "confirmed": True}
    r = officer_client.post("/api/admin/raid/schedule/remove", json=body)
    if r.json().get("review_required"):  # a removal carries consequences: confirmed with the revs its preview returned (D51)
        r = officer_client.post("/api/admin/raid/schedule/remove", json={**body, "revs": r.json()["revs"]})
    assert r.status_code == 200 and not any(s["id"] == "tue" for s in reg.schedules(RID)), r.text
    body = {"instance": RID, "confirmed": True}
    r = officer_client.post("/api/admin/raid/reset", json=body)
    if r.json().get("review_required"):
        r = officer_client.post("/api/admin/raid/reset", json={**body, "revs": r.json()["revs"]})
    assert r.status_code == 200 and RID not in reg.config.raids, r.text


def test_a_member_is_still_refused(member_client, reg):
    r = member_client.post(CHANGE, json={"changes": [{"kind": "raid", "target": RID, "fields": {"notes": "x"}}], "confirmed": True, "apply": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["refused"] and d["refused"][0].startswith("Only officers") and not d.get("applied")
    assert "notes" not in (reg.config.raids.get(RID) or {})


@pytest.mark.parametrize("fields,words", [
    ({"lock_hours_before": "same"}, TIMING_ON_SCHEDULE),  # the raid's current number: refused all the same, never "no changes"
    ({"lock_hours_before": "changed"}, TIMING_ON_SCHEDULE),
    ({"cadence": "short"}, TIMING_ON_SCHEDULE),
    ({"confirm": True}, TIMING_ON_SCHEDULE),
    ({"tank_min": 2}, "Set a role's minimum as a minimum: at least 2 tanks."),
    ({"comp": {"healer": {"min": 3}}}, "Set a role's minimum as a minimum: at least 3 healers."),
    ({"comp": "healer 3"}, "not a map"),  # a string or a list: the same sentence, never a 500
    ({"comp": [3]}, "not a map"),
])
def test_raid_timing_and_role_limits_are_refused_whenever_present(officer_client, reg, fields, words):
    """configops._raid_ops (the Raids form, ops_from_fields "raid", MCP raid_set): present = refused, without comparing."""
    lock = reg.raid_def(RID)["lock_hours_before"]
    fields = {k: (lock if v == "same" else lock + 2 if v == "changed" else v) for k, v in fields.items()}
    if words == "not a map":
        from oibot_gm.registry import role_limit_refusal
        words = role_limit_refusal("tank_min")
    with pytest.raises(RegistryError) as e:
        configops.ops_from_fields(reg, "raid", RID, dict(fields))
    assert str(e.value) == words
    r = officer_client.post("/api/admin/raid", json={"instance": RID, **fields, "confirmed": True})
    assert r.status_code == 400 and r.json()["error"] == words
    r = officer_client.post(CHANGE, json={"changes": [{"kind": "raid", "target": RID, "fields": fields}], "confirmed": True, "apply": True})
    assert words in (r.json().get("error") or ""), r.text


def test_the_raids_route_carries_minimums_and_no_raid_timing(officer_client, reg):
    reg.set_minimums(RID, {"healer": {"min": 3, "note": "two groups"}}, "t")
    row = next(x for x in officer_client.get("/api/raids").json()["raids"] if x["id"] == RID)
    assert not ({"signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours",
                 "nudge", "autofill", "open_dm", "confirm", "comp", "comp_targets"} & set(row))
    assert row["minimums"] == {"healer": {"min": 3, "note": "two groups"}} and row["minimum"]["healer"] == 3
    ready = next(x for x in officer_client.get("/api/readiness", params={"raid": RID}).json()["raids"])
    assert ready["minimums"] == row["minimums"] and ready["minimum"]["healer"] == 3 and "targets" not in ready and "bounds" not in ready


# ---------------------------------------------------------------- plain text and the registry

def test_plain_text_lets_officers_change_a_raid_by_default(reg):
    officer = reg.test_members()[0].discord_id
    assert reg.may_plain("comp", officer, officer=True, channel=PLAIN_WEB) is None
    allowed, refused = configops.authorize(reg, [ConfigOp(op="schedule_set", target=RID, field="alt.slots", value="Sat 20:00"),
                                                 ConfigOp(op="raid_set", target=RID, field="notes", value="x")], officer, officer=True, channel=PLAIN_WEB)
    assert len(allowed) == 2 and not refused
    allowed, refused = configops.authorize(reg, [ConfigOp(op="aura_set", target="blood_pact", field="note", value="x")], officer, officer=True, channel=PLAIN_WEB)
    assert not allowed and refused, "auras stay the owner's"


def test_raid_set_refuses_timing_and_limits_in_one_sentence(reg):
    with pytest.raises(RegistryError, match="Change it on the schedule"):
        configops.apply(reg, ConfigOp(op="raid_set", target=RID, field="lock_hours_before", value="12"), "t", is_owner=False)
    with pytest.raises(RegistryError, match="Change it on the schedule"):
        configops.apply(reg, ConfigOp(op="raid_set", target=RID, field="cadence", value="short"), "t", is_owner=False)
    with pytest.raises(RegistryError) as e:
        configops.apply(reg, ConfigOp(op="raid_set", target=RID, field="tank_min", value="3"), "t", is_owner=False)
    assert str(e.value) == "Set a role's minimum as a minimum: at least 3 tanks."
    with pytest.raises(RegistryError, match="as a minimum: at least 5 healers"):
        configops.apply(reg, ConfigOp(op="raid_set", target=RID, field="healer_max", value="5"), "t", is_owner=False)
    assert configops.describe(reg, ConfigOp(op="raid_set", target=RID, field="lock_hours_before", value="12")).endswith(TIMING_ON_SCHEDULE)


def test_a_minimum_takes_no_upper_limit_and_no_unknown_slot(reg):
    for entry in ({"min": 2, "max": None}, {"min": 2, "max": ""}, {"min": 2, "max": 4}):
        with pytest.raises(RegistryError, match=NO_UPPER_LIMIT):
            reg.set_minimums(RID, {"healer": entry}, "t")
    with pytest.raises(RegistryError, match=NO_UPPER_LIMIT):
        configops.apply(reg, ConfigOp(op="comp_target", target=RID, field="healer", value="2-4"), "t", is_owner=False)
    for slot in ("Tank", "paladin", "Paladin:Tank"):
        with pytest.raises(RegistryError, match="isn't a role, a class or a spec"):
            reg.set_minimums(RID, {slot: {"min": 1}}, "t")
    assert "minimums" not in (reg.config.raids.get(RID) or {})
    assert reg.set_minimums(RID, {"healer": {"min": 3}}, "t") == ["Barrow Deeps · Healers: at least 3"]


def test_a_schedule_takes_a_run_week_preset(reg):
    reg.set_schedule("alt", {"slots": "Sat 20:00"}, "t", raid=RID)
    configops.apply(reg, ConfigOp(op="schedule_set", target=RID, field="alt.cadence", value="short"), "t", is_owner=False)
    sd = reg.schedule_def(RID, "alt")
    from oibot_gm import presets

    assert {f: float(sd[f]) for f in presets.CADENCE_FIELDS} == {f: float(v) for f, v in presets.preset_fields("short").items()}


# ---------------------------------------------------------------- Discord

def test_the_raid_wizard_opens_for_an_officer(reg, store, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)  # an officer, not the owner
    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: False)
    h = Harness(reg)

    async def go():
        await open_raid(h, reg)
        assert "Owner only." not in h.text and "Officers only" not in h.text
        sections = [o.value for s in h._items() if getattr(s, "placeholder", None) == "Pick a section" for o in s.options]
        assert "cadence" not in sections and "comp" in sections
        await h.pick("Pick a section", "comp")
        assert "**Minimums**" in h.text and "Save minimums" in h.buttons()
        await h.pick("Set a minimum for…", "healer")
        assert set(h.modal._items) == {"min", "note"}, "one number and a reason: no at-most select"
        await h.submit(h.modal, min="4")
        await h.press("Save minimums")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.minimums(RID)["healer"] == 4


def test_a_member_cannot_open_the_raid_wizard(reg, store):
    from oibot_gm.wizard import FLOWS

    h = Harness(reg)  # not patched: the harness user is neither owner nor officer

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        assert h.text == flows_config.OFFICERS_ONLY and h.screen["view"] is None
    asyncio.run(go())


def test_gate_of_names_officers_for_the_raid_wizard():
    assert help.gate_of("/gm config raid") == "officer"
    assert help.gate_of("/gm config aura") == "owner"


# ---------------------------------------------------------------- MCP

def test_set_minimum_is_the_mcp_tool():
    names = mcp_tool_names()
    assert "set_minimum" in names and "set_comp_target" not in names


def test_set_minimum_on_a_run_says_minimums_belong_to_the_raid(w, reg, rs, monkeypatch):
    from conftest import open_test_run
    from mcp.server.fastmcp.exceptions import ToolError

    from oibot_gm.registry import MINIMUM_ON_RAID

    ev = open_test_run(reg, rs)

    class Api:
        def get(self, path, **params):
            r = w.c.get(path, params={k: v for k, v in params.items() if v is not None}, headers={"Authorization": f"Bearer {TOKEN}"})
            if r.status_code >= 400:
                raise ToolError(r.text)
            return r.json()

        def post(self, path, body=None, actor=False):
            r = w.c.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"})
            if r.status_code >= 400:
                raise ToolError(r.json().get("error") or r.text)
            return r.json()
    monkeypatch.setattr(ms, "api", lambda guild=None: Api())
    with pytest.raises(ToolError, match=MINIMUM_ON_RAID):
        ms.set_minimum(ev.key, "healer", min=3)
    with pytest.raises(ToolError, match="unknown raid"):
        ms.set_minimum("molten_core", "healer", min=3)


def test_a_minimum_aimed_at_the_raids_name_reaches_the_raid_while_one_run_is_live(reg, rs):
    """Only an exact run, event or roster key is MINIMUM_ON_RAID: a raid named by its name while exactly one of its
    runs is live is the raid, never that run."""
    from conftest import open_test_run

    from oibot_gm.registry import MINIMUM_ON_RAID

    ev = open_test_run(reg, rs)
    name = reg.raid_def(RID)["name"]
    assert configops._minimum_raid(reg, name) == RID
    assert configops._minimum_raid(reg, name.upper()) == RID
    for key in (ev.key, ev.team):
        with pytest.raises(RegistryError) as e:
            configops._minimum_raid(reg, key)
        assert str(e.value) == MINIMUM_ON_RAID
    ops = configops.ops_from_fields(reg, "comp", name, {"healer": {"min": 3}})
    asyncio.run(configops.apply_ops(reg, ops, by="t", by_id=None))
    assert reg.config.raids[RID]["minimums"]["healer"]["min"] == 3


def test_the_raid_wizards_reset_goes_through_the_one_op_with_its_effects(reg, store, monkeypatch):
    from oibot_gm import discord_registry
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: False)
    reg.set_schedule("alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t", raid=RID)
    from test_member_inbox import real

    m = real(reg, 0)
    reg.regular_add(m.discord_id, "alt", "t")
    d = configops.describe_ops(reg, [ConfigOp(op="raid_reset", target=RID)])
    want = [d.lines[0].before, *d.consequences]  # the site's and plain text's raid_reset preview: what goes, in words
    assert "Alt run" in want[0]
    h = Harness(reg)
    calls = []
    real = configops.apply_ops

    async def spy(*a, **kw):
        calls.append([op.op for op in a[1]])
        return await real(*a, **kw)
    monkeypatch.setattr(configops, "apply_ops", spy)

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        await h.pick("Pick a raid", RID)
        await h.pick("Pick a section", "reset")
        assert all(c in h.text for c in want)
        await h.press("Reset")
        assert "✅" in h.text
    asyncio.run(go())
    assert calls == [["raid_reset"]] and RID not in reg.config.raids and not any(s["id"] == "alt" for s in reg.schedules(RID))


def test_the_raid_wizard_writes_one_audit_line_per_save_and_reset(reg, store, monkeypatch):
    from oibot_gm import discord_registry
    from oibot_gm.wizard import FLOWS

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)

    class Feed:
        def __init__(self):
            self.lines = []

        async def emit(self, cfg, level, text, exc=None):
            self.lines.append(text)
    client = types.SimpleNamespace(ops=Feed())
    h = Harness(reg, client=client)

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        await h.pick("Pick a raid", RID)
        await h.pick("Pick a section", "notes")
        await h.submit(h.modal, notes="Bring fire resistance.")
        await h.press("Save")
        await h.press("Save")
        assert "✅" in h.text
        await FLOWS["raid_config"](h.command(), reg)
        await h.pick("Pick a raid", RID)
        await h.pick("Pick a section", "reset")
        await h.press("Reset")
        assert "✅" in h.text
    asyncio.run(go())
    assert len(client.ops.lines) == 2, client.ops.lines
    assert "Barrow Deeps raid rules: Notes" in client.ops.lines[0] and RID not in reg.config.raids
