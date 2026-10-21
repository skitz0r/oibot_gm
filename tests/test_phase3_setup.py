"""Dual interfaces phase 3 — config edit modes, schedules, comp targets and the officers' Tell the bot box (design §5.44):
Registry.set_comp_targets (the one writer of officer targets; bounds stay the owner's) behind the comp_target op, the
structured `comp` kind, /roster comp and MCP set_comp_target; Registry.schedule_removal_effects and timezone_change_effects;
rc.rosters_copy; schedule_json's next_if_active and sentences; configops.config_summary(audience) → /gm config show, GET
/api/config/summary and MCP config_summary; POST /api/ask (member → member snapshot, an officer's request to act → act);
/api/meta's timezones and about_max; /api/readiness's targets; the frontend lints (D40: no box on Me; nothing typed for
a timezone, a date or a time)."""
from __future__ import annotations

import asyncio
import re
import subprocess
import types
from datetime import timedelta
from pathlib import Path

import discord
import pytest
from conftest import OFFICER_ROLE, OWNER
from fastapi.testclient import TestClient
from test_mcp import TOKEN, FakeBot, bearer
from test_people import real
from wizard_harness import Harness

from oibot_gm import configops, raidcycle as rc
from oibot_gm import flows_config  # noqa: F401 — registers the flows
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import ConfigChanged, RegistryError
from oibot_gm.web import app as web
from oibot_gm.wizard import FLOWS

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend" / "src"
CHANGE = "/api/ops/change"
MILITARY = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b(?!\s?[AP]M)")


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


def run(coro):
    return asyncio.run(coro)


def alt_schedule(reg, rid="barrow_deeps"):
    reg.set_schedule(rid, "alt", {"kind": "weekly", "slots": "Sat 20:00", "name": "Alt run"}, "t")


def open_real(reg, rs, rid="barrow_deeps", schedule="alt", days=3):
    """A REAL (non-test) run from a schedule: what schedule_removal_effects / rosters_copy / timezone_change_effects name."""
    start = (reg.now_local() + timedelta(days=days)).replace(hour=20, minute=0, second=0, microsecond=0)
    return rc.open_run(reg, rs, rid, start, by="t", schedule=schedule)


# ---------------------------------------------------------------- Registry.set_comp_targets (RD-12)

def test_set_comp_targets_is_one_write_and_refuses_outside_the_bounds(reg, store):
    n0 = commits(store)
    lines = reg.set_comp_targets("barrow_deeps", {"healer": {"min": 2, "max": 3, "note": "two raid healers"}, "Shaman": {"min": 1}}, "Jon")
    assert len(lines) == 2 and commits(store) == n0 + 1
    t = reg.config.raids["barrow_deeps"]["comp_targets"]
    assert t["healer"] == {"min": 2, "max": 3, "note": "two raid healers"} and t["Shaman"] == {"min": 1}
    # the owner's bounds: barrow_deeps seats at most 3 healers
    with pytest.raises(RegistryError, match=r"at least 4 healers but the raid allows at most 3 — change the bounds first \(owner\)"):
        reg.set_comp_targets("barrow_deeps", {"Mage": {"min": 1}, "healer": {"min": 4}}, "Jon")
    with pytest.raises(RegistryError, match="at most 1 tank but the raid seats at least 2"):
        reg.set_comp_targets("barrow_deeps", {"tank": {"min": 0, "max": 1}}, "Jon")
    with pytest.raises(RegistryError, match="below at least"):
        reg.set_comp_targets("barrow_deeps", {"Mage": {"min": 3, "max": 2}}, "Jon")
    with pytest.raises(RegistryError, match="unknown comp slot"):
        reg.set_comp_targets("barrow_deeps", {"Bard": {"min": 1}}, "Jon")
    assert "Mage" not in reg.config.raids["barrow_deeps"]["comp_targets"] and commits(store) == n0 + 1  # nothing of a refused call stuck
    reg.set_comp_targets("barrow_deeps", {"healer": None, "Shaman": None}, "Jon")
    assert "comp_targets" not in reg.config.raids.get("barrow_deeps", {})
    with pytest.raises(ConfigChanged):
        reg.set_comp_targets("barrow_deeps", {"Mage": {"min": 1}}, "Jon", rev="stale")


def test_comp_target_op_goes_through_the_verb_and_coalesces_into_one_write(reg, store):
    # the plain-text grammar ('2-3', a reason) keeps working; the parts it leaves out keep the stored target's
    line = configops.apply(reg, ConfigOp(op="comp_target", target="barrow_deeps", field="healer", value="2-3", reason="raid heals"), "Jon", True)
    assert "Healers: at least 2, at most 3 (raid heals)" in line
    configops.apply(reg, ConfigOp(op="comp_target", target="barrow_deeps", field="healer", value="2"), "Jon", True)
    assert reg.config.raids["barrow_deeps"]["comp_targets"]["healer"] == {"min": 2, "max": 3, "note": "raid heals"}
    with pytest.raises(RegistryError, match="change the bounds first"):
        configops.apply(reg, ConfigOp(op="comp_target", target="barrow_deeps", field="healer", value="4-4"), "Jon", True)
    # the structured kind: whole entries as JSON, only what differs, null clears
    ops = configops.ops_from_fields(reg, "comp", "barrow_deeps", {"healer": {"min": 2, "max": 3, "note": "raid heals"}, "Mage": {"min": 1, "max": 2}, "Rogue": None})
    assert [(o.op, o.field) for o in ops] == [("comp_target", "Mage")]
    ops += configops.ops_from_fields(reg, "comp", "barrow_deeps", {"healer": None, "Warlock": {"min": 1}})
    merged = configops.coalesce(ops)
    assert len(merged) == 1 and isinstance(merged[0], configops.MergedOp)
    n0 = commits(store)
    res = run(configops.apply_ops(reg, ops, by="Jon", by_id=OWNER))
    assert res["applied"] and not res["refused"] and commits(store) == n0 + 1
    assert reg.config.raids["barrow_deeps"]["comp_targets"] == {"Mage": {"min": 1, "max": 2}, "Warlock": {"min": 1}}
    d = configops.describe_ops(reg, configops.ops_from_fields(reg, "comp", "barrow_deeps", {"Mage": {"min": 2, "max": 2, "note": "Intellect"}}))
    ln = d.lines[0]
    assert ln.label == "Barrow Deeps · Desired comp" and ln.parts[0].label == "Mages" and ln.parts[0].before == "at least 1, at most 2"
    assert ln.parts[0].after == "at least 2, at most 2" and "Why: Intellect" in ln.notes and not d.review  # routine: saves directly


def test_comp_targets_are_officers_by_privilege(reg):
    ops = configops.ops_from_fields(reg, "comp", "barrow_deeps", {"Mage": {"min": 1}})
    assert configops.authorize_ops(reg, ops, 5, role="officer")[0] == ops
    allowed, refused = configops.authorize_ops(reg, ops, 5, role="member")
    assert not allowed and refused[0].startswith("Only officers")


# ---------------------------------------------------------------- schedules: removal effects, pause, rosters copy (RD-10 / RD-11)

def test_schedule_removal_effects_names_regulars_and_open_runs(reg, rs):
    alt_schedule(reg)
    m = real(reg, 0)
    reg.regular_add(m.discord_id, "barrow_deeps:alt", "t")
    ev = open_real(reg, rs)
    eff = reg.schedule_removal_effects("barrow_deeps", "alt", rs)
    assert eff.regulars == [m.display_name] and eff.open_runs == [reg.config.roster(ev.team)["name"]]
    s = eff.sentence()
    assert s.startswith("Removes Alt run (Saturdays 8:00 PM).") and "1 regular comes off it" in s and "nobody is DMed" in s and "keeps its time and sheet" in s
    cons = configops.op_consequences(reg, ConfigOp(op="schedule_remove", target="barrow_deeps", field="alt"))
    assert cons == eff.lines()
    with pytest.raises(RegistryError, match="has no schedule"):
        reg.schedule_removal_effects("barrow_deeps", "nope", rs)
    reg.set_schedule("barrow_deeps", "sun", {"kind": "weekly", "slots": "Sun 20:00", "name": "Sundays"}, "t")
    assert reg.schedule_removal_effects("barrow_deeps", "sun", rs).lines() == ["No run from it is open right now."]


def test_rosters_copy_reaches_the_next_run_only(reg, rs):
    alt_schedule(reg)
    assert rc.rosters_copy(reg, "barrow_deeps", "alt", 2, rs) == "From the next run: the sheet advertises 2 × 10 seats."
    open_real(reg, rs)
    assert rc.rosters_copy(reg, "barrow_deeps", "alt", 2, rs) == "From the next run: the sheet advertises 2 × 10 seats. The run already open keeps its 1 × 10."
    text = configops.describe(reg, ConfigOp(op="schedule_set", target="barrow_deeps", field="alt.rosters", value="2"))
    assert "From the next run: the sheet advertises 2 × 10 seats." in text
    d = configops.describe_ops(reg, [ConfigOp(op="schedule_set", target="barrow_deeps", field="alt.rosters", value="2")])
    assert any("The run already open keeps its 1 × 10." in n for n in d.lines[0].notes)


def test_pause_sentence_names_the_open_run(reg, rs):
    alt_schedule(reg)
    ev = open_real(reg, rs)
    s = configops.schedule_pause_sentence(reg, "barrow_deeps", "alt", True, rs)
    assert s.startswith("Nothing new opens from Alt run; ") and reg.config.roster(ev.team)["name"] in s and s.endswith("Regulars stay.")
    assert configops.schedule_pause_sentence(reg, "barrow_deeps", "alt", False).startswith("Alt run opens its sheets again on its cadence — next run ")


# ---------------------------------------------------------------- timezone change with live runs

def test_timezone_change_effects_lists_live_runs_whose_clock_moves(reg, rs):
    alt_schedule(reg)
    ev = open_real(reg, rs)
    effects = reg.timezone_change_effects("America/New_York", rs)
    assert len(effects) == 1 and "stays at its real moment" in effects[0] and "11:00 PM in America/New_York" in effects[0]
    assert "cancel and re-open it to move it" in effects[0] and not MILITARY.search(effects[0])
    assert reg.timezone_change_effects(reg.config.timezone, rs) == []
    with pytest.raises(RegistryError, match="isn't a timezone"):
        reg.timezone_change_effects("Mars/Olympus", rs)
    # the review of a timezone Save carries them (CONSEQUENCE_HOOKS), and it is warn-class
    d = configops.describe_ops(reg, configops.ops_from_fields(reg, "settings", None, {"timezone": "America/New_York"}))
    assert d.warn and d.review and effects[0] in d.consequences
    assert any("on today's clock" in n for n in d.lines[0].notes)  # rc.timezone_preview rows under the setting
    del ev


# ---------------------------------------------------------------- config_summary(audience) → /gm config show

def test_config_summary_sections_and_the_owners_host_facts(reg, monkeypatch, tmp_path):
    monkeypatch.setenv("OIBOT_SUPERUSERS", str(OWNER))
    monkeypatch.setenv("OIBOT_MCP_TOKEN", "x")
    monkeypatch.delenv("OIBOT_MCP_LOCAL_ONLY", raising=False)
    env = tmp_path / ".env"
    env.write_text("X=1")
    monkeypatch.setattr(configops, "ENV_FILE", env)
    reg.config.invite_url = "https://example.com/join"
    officer = configops.config_summary(reg)
    keys = [s.key for s in officer]
    assert keys == ["guild", "channels", "raids", "recruitment", "plain", "test_bench", "host"]
    assert officer[-1].lines == ["Host facts: owner only."]
    text = configops.summary_text(officer)
    assert "not a Discord invite link" in text and "analytics cards are paused" in text and not MILITARY.search(text)
    bot = types.SimpleNamespace(started_at=1.0, ctx=types.SimpleNamespace(provider=types.SimpleNamespace(summary=lambda: "LLM spend this month $1.00 of $5")),
                                guild_channels=lambda r: [])
    host = configops.config_summary(reg, bot, "owner")[-1].lines
    assert any("Host superusers" in x for x in host) and any("$1.00 of $5" in x for x in host)
    assert any("works only on the host (local only)" in x for x in host) and any(".env) changed since the bot started" in x for x in host)


def test_gm_config_show_is_one_field_per_section_with_a_more_line(reg):
    from oibot_gm.discord_registry import config_show_embed

    sections = configops.config_summary(reg)
    sections[2].lines = [f"Raid line {n} " + "x" * 60 for n in range(40)]  # a section past a field's 1024 characters
    e = config_show_embed(reg, sections)
    assert [f.name for f in e.fields] == [s.title for s in sections]
    assert all(len(f.value) <= 1024 for f in e.fields)
    assert re.search(r"\+\d+ more on the site", e.fields[2].value)
    for sec in sections:  # every section long: the embed still fits Discord's 6000-character total
        sec.lines = sec.lines + ["y" * 300 for _ in range(10)]
    assert len(config_show_embed(reg, sections)) <= 6000


# ---------------------------------------------------------------- the routes

@pytest.fixture
def owner_client(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FakeBot(reg, rs)))


def test_meta_carries_grouped_timezones_and_the_about_cap(owner_client):
    m = owner_client.get("/api/meta", headers=bearer()).json()
    groups = {g["group"]: g["items"] for g in m["timezones"]}
    assert "America" in groups and "Europe" in groups and groups["UTC"][0]["value"] == "UTC"
    ny = next(i for i in groups["America"] if i["value"] == "America/New_York")
    assert ny["label"] == "New York"
    assert m["about_max"] == flows_config.ABOUT_MAX


def test_config_summary_route_by_audience(owner_client, reg, rs, monkeypatch):
    d = owner_client.get("/api/config/summary", headers=bearer()).json()
    assert d["audience"] == "owner" and d["sections"][-1]["key"] == "host" and d["sections"][-1]["lines"] != ["Host facts: owner only."]
    assert not any("<#" in x or "<@" in x for s in d["sections"] for x in s["lines"])  # names on the site, never a raw mention


def test_readiness_route_carries_targets_bounds_and_slots(owner_client, reg):
    reg.set_comp_targets("barrow_deeps", {"Mage": {"min": 1, "note": "Intellect"}}, "t")
    d = owner_client.get("/api/readiness", params={"raid": "barrow_deeps"}, headers=bearer()).json()
    r = d["raids"][0]
    assert r["targets"] == {"Mage": {"min": 1, "note": "Intellect"}} and r["bounds"]["healer"] == {"min": 2, "max": 3} and r["rev"] == reg.config_rev("raids:barrow_deeps")
    assert {"value": "Paladin:Holy", "label": "Holy Paladins", "kind": "spec"} in d["slots"] and d["note_max"] == 200
    # the Readiness page's Save: typed comp changes on the structured path, the raid rev as shown
    body = {"changes": [{"kind": "comp", "target": "barrow_deeps", "fields": {"Mage": None, "healer": {"min": 3, "max": 3}}}], "apply": True,
            "revs": {"config:raids:barrow_deeps": r["rev"]}}
    res = owner_client.post(CHANGE, json=body, headers=bearer()).json()
    assert res["applied"] and reg.config.raids["barrow_deeps"]["comp_targets"] == {"healer": {"min": 3, "max": 3}}
    body["changes"][0]["fields"] = {"healer": {"min": 2}}
    stale = owner_client.post(CHANGE, json=body, headers=bearer())  # the rev the page loaded is old now
    assert stale.status_code == 409 and stale.json()["changed"] == ["raids:barrow_deeps"]


def test_raids_route_schedule_words_paused_and_sentences(owner_client, reg, rs):
    alt_schedule(reg)
    reg.set_schedule("barrow_deeps", "alt", {"active": False}, "t")
    raids = owner_client.get("/api/raids", headers=bearer()).json()["raids"]
    s = next(x for r in raids if r["id"] == "barrow_deeps" for x in r["schedules"] if x["id"] == "alt")
    assert s["next"] == [] and len(s["next_if_active"]) == 3 and not any(MILITARY.search(x) for x in s["next_if_active"])
    assert s["pause_sentence"].startswith("Nothing new opens from Alt run") and s["resume_sentence"].startswith("Alt run opens its sheets again")
    assert s["removal_sentence"].startswith("Removes Alt run") and s["rosters_copy"]["2"] == "From the next run: the sheet advertises 2 × 10 seats."


# ---- /api/ask (the officers' Tell the bot box; members get the member view)

@pytest.fixture
def ask(reg, rs, monkeypatch):
    from test_people_web import SECRET, WebBot

    from oibot_gm import help as help_mod
    from oibot_gm.discord_help import HelpMixin

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    seen: list = []

    def fake_answer(provider, tree, reg_, rs_, user_id, is_officer, question, extras=None, public=False):
        seen.append({"uid": user_id, "officer": is_officer, "public": public, "state": help_mod.guild_state(reg_, rs_, user_id, is_officer, question, None, public=public)})
        return help_mod.HelpAnswer(answer="Lock is Tue 6:30 PM.", acts="bench" in question)
    monkeypatch.setattr(help_mod, "answer", fake_answer)

    class Bot(WebBot):
        help_answer = HelpMixin.help_answer
        help_extras = HelpMixin.help_extras
        help_privilege = HelpMixin.help_privilege

    bot = Bot(reg, rs)
    bot.ctx = types.SimpleNamespace(provider=types.SimpleNamespace(summary=lambda: "LLM spend"))
    bot.tree = None
    return types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, rs=rs, seen=seen)


def test_ask_member_gets_the_member_snapshot_and_an_officer_a_request_to_act(ask):
    from test_people_web import OFFICER, as_user, member

    reg = ask.reg
    reg.config.ask_audience = "everyone"
    m = member(ask)
    h = as_user(ask, m.discord_id)
    d = ask.c.post("/api/ask", json={"question": "when does tonight lock? also bench Jonny"}, headers=h).json()
    assert d["answer"].startswith("Lock is") and d["officer_view"] is False and not d.get("act")  # a member's request to act is answered, never acted on
    assert ask.seen[-1]["officer"] is False and ask.seen[-1]["uid"] == m.discord_id
    assert "officer index" not in ask.seen[-1]["state"].lower()
    h = as_user(ask, OFFICER)
    assert ask.c.post("/api/ask", json={"question": "when does tonight lock?"}, headers=h).json()["officer_view"] is True
    assert ask.seen[-1]["officer"] is True
    d = ask.c.post("/api/ask", json={"question": "bench Jonny tonight"}, headers=h).json()
    assert d == {"act": True, "answer": None}  # the box then previews it on POST /api/ops/change {text}
    assert ask.c.post("/api/ask", json={"question": ""}, headers=h).status_code == 400
    assert ask.c.post("/api/ask", json={"question": "x" * 1600}, headers=h).status_code == 400
    ask.bot.ctx = types.SimpleNamespace(provider=None)
    r = ask.c.post("/api/ask", json={"question": "anything"}, headers=h)
    assert r.status_code == 503 and "can't read sentences right now" in r.json()["error"]


def test_ask_outside_the_audience_gets_the_guide(ask):
    from test_people_web import as_user, member

    ask.reg.config.ask_audience = "officers"
    m = member(ask)
    d = ask.c.post("/api/ask", json={"question": "hello?"}, headers=as_user(ask, m.discord_id)).json()
    assert d["answer"] is None and d["guide"] and "officers" in d["message"]


# ---------------------------------------------------------------- Discord: /roster comp, the raid wizard's targets block, the paused line

@pytest.fixture
def officer_only(monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: False)


def test_roster_comp_sets_a_target_through_configops(reg, rs, store, officer_only):
    h = Harness(reg, rs)

    async def go():
        await FLOWS["roster_comp"](h.command(), reg)
        await h.pick("Pick a raid", "barrow_deeps")
        assert "Desired comp" in h.text and "Bounds — what the solver seats" in h.text and "Save targets" in h.buttons()
        await h.pick("Set a target for…", "healer")
        await h.submit(h.modal, min="4")  # past the owner's bounds: refused at once, Fix this reopens the form
        assert "change the bounds first (owner)" in h.text and "Fix this" in h.buttons()
        await h.press("Fix this")
        await h.submit(h.modal, min="2", max="3", note="raid heals")
        assert "not saved yet" in h.text and "1 unsaved change" in h.text
        await h.pick("Set a target for…", "__spec")
        await h.pick("Pick a class", "Shaman")
        await h.pick("Pick a spec", "Enhancement")
        await h.submit(h.modal, min="1", max="none")
        n0 = commits(store)
        await h.press("Save targets")
        assert "✅" in h.text and commits(store) == n0 + 1
    run(go())
    assert reg.config.raids["barrow_deeps"]["comp_targets"] == {"healer": {"min": 2, "max": 3, "note": "raid heals"}, "Shaman:Enhancement": {"min": 1}}


def test_roster_comp_refuses_a_member(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: False)
    h = Harness(reg, rs)
    run(FLOWS["roster_comp"](h.command(), reg))
    assert "Officers only" in h.text


def test_the_raid_wizards_group_make_up_opens_the_targets_block(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    reg.set_comp_targets("barrow_deeps", {"Mage": {"min": 1}}, "t")
    h = Harness(reg, rs)

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        await h.pick("Pick a raid", "barrow_deeps")
        await h.pick("Pick a section", "comp")
        assert "**Bounds**" in h.text and "**Targets**" in h.text and "Mages at least 1" in h.text
        await h.press("Desired comp (targets)…")
        assert "Desired comp" in h.text and "Back" in h.buttons()
        await h.pick("Remove a target", "Mage")
        await h.press("Save targets")
        assert "✅" in h.text and "Mages: back to the derived target" in h.text and "Set a target for…" in h.selects()
        await h.press("Back")
        assert "Pick a role" in h.selects()
    run(go())
    assert "comp_targets" not in reg.config.raids.get("barrow_deeps", {})


def test_the_schedule_screen_says_paused_never_next_runs(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    alt_schedule(reg)
    reg.set_schedule("barrow_deeps", "alt", {"active": False}, "t")
    h = Harness(reg, rs)

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        await h.pick("Pick a raid", "barrow_deeps")
        await h.pick("Pick a section", "times")
        await h.pick("Pick a schedule", "alt")
        assert "⏸ Paused — would next run " in h.text and "· nothing opens" in h.text and "Next runs:" not in h.text
        await h.pick("Change this schedule", "rosters")
        await h.pick("Rosters per run", "2")
        assert "From the next run: the sheet advertises 2 × 10 seats." in h.text
    run(go())


def test_timezone_wizard_confirm_lists_live_runs(reg, rs, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    alt_schedule(reg)
    open_real(reg, rs)
    h = Harness(reg, rs)

    async def go():
        await FLOWS["timezone"](h.command(), reg)
        await h.press("Eastern · New York")
        assert "⚠ Barrow Deeps" in h.text and "stays at its real moment" in h.text
    run(go())


# ---------------------------------------------------------------- MCP

def test_mcp_comp_and_summary_tools(owner_client, reg, monkeypatch):
    from oibot_gm import mcp_server as ms

    class Api:
        def __init__(self, c):
            self.c = c

        def get(self, path, **params):
            return self.c.get(path, params={k: v for k, v in params.items() if v is not None}, headers=bearer()).json()

        def post(self, path, body):
            r = self.c.post(path, json=body, headers=bearer())
            return r.json()

    a = Api(owner_client)
    monkeypatch.setattr(ms, "api", lambda guild=None: a)
    out = ms.set_comp_target("Barrow Deeps", "Mage", min=1, max=2, note="Intellect")
    assert "Mages: at least 1, at most 2 (Intellect)" in out and reg.config.raids["barrow_deeps"]["comp_targets"]["Mage"]["max"] == 2
    pv = ms.set_comp_target("barrow_deeps", "Mage", clear=True, preview=True)
    assert pv["preview"][0]["parts"][0]["after"] == "derived" and "Mage" in reg.config.raids["barrow_deeps"]["comp_targets"]
    with pytest.raises(Exception, match="change the bounds first"):
        ms.set_comp_target("barrow_deeps", "healer", min=5)
    alt_schedule(reg)
    m = real(reg, 0)
    reg.regular_add(m.discord_id, "barrow_deeps:alt", "t")
    regs = ms.list_regulars("barrow_deeps", "alt")
    assert regs["series"][0]["regulars"][0]["display_name"] == m.display_name
    summ = ms.config_summary()
    assert summ["audience"] == "owner" and [s["key"] for s in summ["sections"]][0] == "guild"


# ---------------------------------------------------------------- frontend lints

def test_d40_no_tell_the_bot_box_on_the_me_page():
    me = (FRONT / "pages" / "Me.tsx").read_text()
    assert "TellTheBot" not in me, "D40: no 'tell the bot' box on the Me page (officer pages only)"
    app = (FRONT / "App.tsx").read_text()
    assert "TellTheBot" in app and re.search(r"officerPage|isOfficerPage|OFFICER\.some", app), "the rail's box shows on officer pages only"


TYPED_WORDS = re.compile(r"\b(time ?zone|iana|date|time|when|datetime-local)\b", re.I)


def typed_time_fields(src: str) -> list[str]:
    """Every free-text field (a Mantine TextInput / Textarea or a raw <input>) whose strings — label, description,
    placeholder, type — name a timezone, a date or a time: a value a parser would read."""
    bad = []
    for m in re.finditer(r"<(TextInput|Textarea|input)\b(?:[^<>]|=>)*?/?>", src, re.S):
        tag = m.group(0)
        strings = [a or b for a, b in re.findall(r'"([^"]*)"|`([^`]*)`', tag)]
        if any(TYPED_WORDS.search(x) for x in strings):
            bad.append(tag[:140])
    return bad


def test_no_free_typed_timezone_date_or_time_field_in_the_frontend():
    """The clock rule's second half: a person PICKS a timezone, a date or a time from a widget (Select, TimePicker,
    DateTimePicker, DatePickerInput); no free-text field asks for one."""
    bad = [f"{f.relative_to(FRONT)}: {t}" for f in sorted(FRONT.rglob("*.tsx")) for t in typed_time_fields(f.read_text())]
    assert not bad, "typed timestamps:\n" + "\n".join(bad)


def test_the_typed_field_lint_catches_the_old_timezone_box():
    """Regression: the Config page used to take the timezone as a typed IANA name (a TextInput saved on blur)."""
    old = '<TextInput label={lab("timezone", "Timezone")} description="IANA name; every schedule and clock on the site" defaultValue={data.settings.timezone} disabled={ro} w={260} onBlur={(e) => e.currentTarget.value !== data.settings.timezone && set("timezone", e.currentTarget.value)} />'
    assert typed_time_fields(old)
    assert not typed_time_fields('<Select label="Timezone" data={zones} searchable />')
