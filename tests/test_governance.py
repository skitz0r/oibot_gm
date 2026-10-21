"""Dual interfaces phase 3 — governance (design §5.44; out/dual_interface_design.md §3.4, owner decisions D33–D41, D45):
the proposal verb (owner-only game-data approval, the job header, a repeat names the first decider, the annotate verb),
D33's raid guard per guild, the apply job driven with a fake Api (the gate, the re-read before restart, the guard, the
pending restart, recovery), policy drafts on disk (a restart loses nothing; the persona's two steps; an append never
rides on another officer's draft), the manual on the site and the help links, the plain-text read view, the leftover
screens after a restart and the per-guild LLM usage. No Discord, no LLM, no launchctl."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import GUILD, GUILD2, OWNER
from fastapi.testclient import TestClient
from test_agents import clients, load_script
from test_agents_multiguild import PROFILE_EDIT
from test_news import add_news, fake_client, proposal_data
from test_parity_ops_owner import FakeProvider
from wizard_harness import Harness

from oibot_gm import agents, help as help_mod, news, pending_views, policy as pol
from oibot_gm import raidcycle as rc
from oibot_gm.news import AlreadyDecided, ProposalStore
from oibot_gm.web import app as web

TOKEN = "t" * 64
BEARER = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}
JOB = {**BEARER, "X-Oibot-Job": "apply"}
SITE = {"X-Requested-With": "oibot"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "OUT", tmp_path / "agents")
    monkeypatch.setattr(agents, "launchd_status", lambda label: {"loaded": False, "state": None, "pid": None, "last_exit": None})
    return tmp_path / "agents"


# ---------------------------------------------------------------- the proposal verb

def test_a_game_data_approval_is_the_owners_even_in_a_lone_guild(reg):
    """Design §3.4: a profile approval edits shared files and restarts the whole process — an officer of a lone guild
    used to approve it because shared_profile_refusal returned None with nobody to share with."""
    add_news(reg)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data(kind="profile", edit=PROFILE_EDIT))
    assert news.decision_refusal(reg.store, reg, p.id, "approved", 5) == news.PROFILE_OWNER_ONLY
    with pytest.raises(PermissionError, match="only the owner approves a game-data change"):
        ps.move(p.id, "approved", "an officer", officer=True, job=False, by_id=5, owner=False)
    assert news.decision_refusal(reg.store, reg, p.id, "approved", OWNER) is None
    assert ps.move(p.id, "approved", "the owner", officer=True, job=False, by_id=OWNER, owner=True).state == "approved"
    g = ps.create(reg, proposal_data())  # a guild setting stays the officers'
    assert ps.move(g.id, "approved", "an officer", officer=True, job=False, by_id=5, owner=False).state == "approved"


def test_a_repeated_decision_names_who_decided_first(reg):
    add_news(reg)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data())
    ps.move(p.id, "approved", "Kessa", officer=True, job=False, by_id=5)
    with pytest.raises(AlreadyDecided) as e:
        ps.move(p.id, "approved", "Jonny", officer=True, job=False, by_id=6)
    assert str(e.value).startswith("already approved by Kessa <t:") and str(e.value).endswith("— nothing to do"), "Discord's stamp by default"
    site = news.already_text(e.value.p, reg.local12)
    assert "<t:" not in site and (" AM " in site or " PM " in site), "the site's words are 12-hour guild time"
    assert ps.get(p.id).decided_by == "Kessa", "the first decider stays on record"


def test_the_job_header_carries_job_moves_only_and_never_an_officers_decision(reg, rs, monkeypatch, out_dir):
    from test_agents import PROP, seed

    seed(reg)
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    pid = c.post("/api/proposals", json=PROP, headers=job).json()["id"]
    assert ProposalStore(reg.store, reg.key).get(pid).created_by == "apply", "the job's own name, never a person's"
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": "approved"}, headers=job).status_code == 403, "a job never decides"
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": "approved"}, headers=site).status_code == 200
    r = c.post(f"/api/proposals/{pid}/resolve", json={"state": "approved"}, headers=site)
    assert r.status_code == 409 and r.json()["error"].startswith("Already approved by Viewer"), r.text
    bearer = {k: v for k, v in job.items() if k != "X-Oibot-Job"}
    r = c.post(f"/api/proposals/{pid}/resolve", json={"state": "applying"}, headers=bearer)
    assert r.status_code == 403 and "apply job" in r.json()["error"], "the owner's token alone reports no job step"
    # the annotate verb: the job's "waiting: …" line, no move, written once per change
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": None, "note": "waiting: x"}, headers=site).status_code == 403
    r = c.post(f"/api/proposals/{pid}/resolve", json={"state": None, "note": "waiting: Demo Guild raids until 10 PM"}, headers=job)
    assert r.status_code == 200 and r.json()["proposal"]["status_note"] == "waiting: Demo Guild raids until 10 PM"
    n = len(ProposalStore(reg.store, reg.key).get(pid).history)
    c.post(f"/api/proposals/{pid}/resolve", json={"state": None, "note": "waiting: Demo Guild raids until 10 PM"}, headers=job)
    p = ProposalStore(reg.store, reg.key).get(pid)
    assert len(p.history) == n and p.state == "approved" and p.history[-1]["state"] is None, "the same note again writes nothing"
    assert c.post(f"/api/proposals/{pid}/resolve", json={"state": "applying"}, headers=job).status_code == 200
    assert ProposalStore(reg.store, reg.key).get(pid).status_note is None, "a move clears the annotation"


def test_get_proposals_carries_the_consequence_and_the_card_state(reg, rs, monkeypatch, out_dir):
    from test_agents import PROP, seed

    seed(reg)
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    pid = c.post("/api/proposals", json=PROP, headers=job).json()["id"]
    p = next(x for x in c.get("/api/proposals").json()["proposals"] if x["id"] == pid)
    assert p["consequence"]["approved"][0].startswith("Approved: the apply job carries it out at its next run") and "no restart" in p["consequence"]["approved"][0]
    assert p["consequence"]["dismissed"][0].startswith("Dismissed: nothing changes")

    async def card_down(r, prop):
        return False  # Discord unreachable
    bot.proposal_card_update = card_down
    r = c.post(f"/api/proposals/{pid}/resolve", json={"state": "approved"}, headers=site)
    assert r.json()["card_updated"] is False and "the Discord card updates when the bot is back" in r.json()["message"]
    assert "approved" not in next(x for x in c.get("/api/proposals").json()["proposals"] if x["id"] == pid)["consequence"]


def test_a_profile_approval_names_every_served_server_and_each_busy_one(duo):
    add_news(duo.a)
    p = ProposalStore(duo.a.store, GUILD).create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT))
    busy = {GUILD: {"raiding": False}, GUILD2: {"raiding": True, "why": "Barrow Deeps Tue 7:30 PM is locked"}}
    lines = news.consequence(p, duo.a, "approved", guilds=duo.guilds, busy=busy, next_run="Tue 7:45 PM")
    assert "restarts for every server it serves (Demo Guild, Duo Guild)" in lines[0] and "at its next run (Tue 7:45 PM)" in lines[0]
    assert lines[1] == "⚠ Duo Guild has a run right now (Barrow Deeps Tue 7:30 PM is locked) — the job will wait until it is over."


# ---------------------------------------------------------------- D33: busy per guild

def ev(reg, *, state, hours, key="bd-x", test=False):
    start = datetime.now(timezone.utc) + timedelta(hours=hours)
    return SimpleNamespace(state=state, start=start, key=key, instance="barrow_deeps", team="bd", test=test)


def test_busy_follows_the_d33_windows(reg):
    assert not news.guild_busy(reg, [])["raiding"]
    assert news.guild_busy(reg, [ev(reg, state="locked", hours=3)])["raiding"], "locked, 3 h before its start: within 4 h"
    assert not news.guild_busy(reg, [ev(reg, state="locked", hours=5)])["raiding"], "locked, 5 h out: not yet"
    assert news.guild_busy(reg, [ev(reg, state="locked", hours=-1)])["raiding"], "under way until it closes"
    assert news.guild_busy(reg, [ev(reg, state="open", hours=0.5)])["raiding"], "open, starting within the hour"
    assert not news.guild_busy(reg, [ev(reg, state="open", hours=2)])["raiding"]
    assert not news.guild_busy(reg, [ev(reg, state="locked", hours=1, test=True)])["raiding"], "a rehearsal never holds a restart"
    b = news.guild_busy(reg, [ev(reg, state="open", hours=2, key="bd-loot")], loot_runs=["bd-loot"])
    assert b["raiding"] and "loot council" in b["why"] and b["until_label"]
    late = ev(reg, state="locked", hours=2)
    b = news.guild_busy(reg, [late])
    assert b["until"] == rc.run_close_at(reg, late).astimezone(timezone.utc).isoformat(timespec="seconds")


def test_api_guilds_carries_busy_per_guild(duo, monkeypatch):
    from test_agents_multiguild import site

    bot, c = site(duo, monkeypatch)
    start = (duo.a.now_local() + timedelta(hours=2)).replace(second=0, microsecond=0)
    e = rc.open_run(duo.a, duo.rs_a, "barrow_deeps", start, by="t")
    e.state = "locked"
    duo.rs_a.save(e, "locked (test)")
    rows = {g["key"]: g for g in c.get("/api/guilds", headers={"Authorization": f"Bearer {TOKEN}"}).json()["guilds"]}
    assert rows[GUILD]["busy"]["raiding"] and "is locked" in rows[GUILD]["busy"]["why"]
    assert rows[GUILD2]["busy"] == {"raiding": False, "until": None, "until_label": "", "why": ""}


# ---------------------------------------------------------------- the apply job, with a fake Api

class JobApi:
    """mcp_server.Api for the apply job: guilds (with busy), proposals per (guild, state), each proposal's state, the
    bot's quiet answer; every POST recorded; `refuse` = {(path, state): message} answered as a ToolError."""

    def __init__(self, world, guild=None):
        self.world, self.guild = world, guild

    def guilds(self):
        g = self.world["guilds"]
        return g() if callable(g) else g

    def get(self, path, **params):
        if path == "/api/proposals":
            return {"proposals": self.world["lists"].get((self.guild, params.get("state")), [])}
        if path == "/api/agents/quiet":
            return self.world.get("quiet", {"quiet": True, "why": []})
        if path.startswith("/api/proposals/"):
            return {"state": self.world["states"].get(path.rsplit("/", 1)[1], "applying")}
        raise AssertionError(path)

    def post(self, path, body=None):
        from oibot_gm.mcp_server import ToolError

        body = body or {}
        self.world["posts"].append((self.guild, path, body.get("state"), body.get("note")))
        why = self.world.get("refuse", {}).get((path, body.get("state")))
        if why:
            raise ToolError(why)
        return {"message": "ok"}


GUILDS = [{"key": GUILD, "name": "Demo Guild", "busy": {"raiding": False}}, {"key": GUILD2, "name": "Duo Guild", "busy": {"raiding": False}}]
BUSY = [GUILDS[0], {"key": GUILD2, "name": "Duo Guild", "busy": {"raiding": True, "until_label": "Tue 11:30 PM", "why": "Barrow Deeps Tue 7:30 PM is locked"}}]
PROFILE = {"id": "260101-bbbb", "kind": "profile", "created_at": "2026-01-01T00:00:00+00:00", "title": "Barrow Deeps is 20-player", "change": "size 10 → 20"}


@pytest.fixture
def job(out_dir, monkeypatch):
    """The apply script with git, the checks, Claude and launchctl faked: `state.edited` flips when Claude 'edits'."""
    apply = load_script("apply")
    state = SimpleNamespace(edited=False, claude=0, restarts=0)
    git = {("rev-parse", "--abbrev-ref", "HEAD"): "main", ("rev-parse", "HEAD"): "abc", ("rev-parse", "--short", "HEAD"): "c0ffee1"}
    monkeypatch.setattr(apply, "sh", lambda *a, **k: SimpleNamespace(stdout=git.get(a[1:], ""), stderr="", returncode=0))
    monkeypatch.setattr(apply, "dirty_paths", lambda: ["profiles/forever/raids.yaml"] if state.edited else [])
    monkeypatch.setattr(apply, "revert_worktree", lambda: None)
    monkeypatch.setattr(apply, "restart_bot", lambda: setattr(state, "restarts", state.restarts + 1) or 1)
    monkeypatch.setattr(apply, "wait_healthy", lambda old, timeout_s=0: True)

    def claude(cmd, prompt, transcript, on_step=None, timeout_s=0, env=None):
        state.claude += 1
        state.edited = True
        return 0
    return apply, state, claude


def world(**kw):
    return {"guilds": GUILDS, "lists": {}, "states": {}, "posts": [], **kw}


def test_apply_a_proposal_dismissed_before_the_job_starts_does_nothing(job):
    apply, state, claude = job
    w = world(lists={(GUILD2, "approved"): [PROFILE]}, states={PROFILE["id"]: "dismissed"},
              refuse={(f"/api/proposals/{PROFILE['id']}/resolve", "applying"): "260101-bbbb is dismissed: it can't become applying"})
    assert apply.main([], api_factory=lambda guild=None: JobApi(w, guild), run_claude=claude) == 0
    assert state.claude == 0 and state.restarts == 0, "the gate refused: Claude never started, nothing committed, no restart"
    assert "260101-bbbb was dismissed before the job started — nothing done" in agents.read_status("apply")["result"]


def test_apply_re_reads_the_proposal_before_restarting(job):
    apply, state, claude = job
    w = world(lists={(GUILD2, "approved"): [PROFILE]}, states={PROFILE["id"]: "failed"})  # moved while Claude and the checks ran
    assert apply.main([], api_factory=lambda guild=None: JobApi(w, guild), run_claude=claude) == 0
    st = agents.read_status("apply")
    assert state.claude == 1 and state.restarts == 0, "the commit stays, the bot is not restarted"
    assert st["restart_pending"]["id"] == PROFILE["id"] and st["restart_pending"]["auto"] is False and st["restart_pending"]["commit"] == "c0ffee1"
    assert "became failed meanwhile" in st["result"]


def test_apply_waits_while_any_guild_raids_and_says_so_on_the_proposal(job):
    apply, state, claude = job
    w = world(guilds=BUSY, lists={(GUILD, "approved"): [{**PROFILE, "id": "260101-aaaa"}]})
    assert apply.main([], api_factory=lambda guild=None: JobApi(w, guild), run_claude=claude) == 0
    assert state.claude == 0, "another server raids: the whole process restarts, so the job waits"
    notes = [p for p in w["posts"] if p[2] is None]
    assert notes == [(GUILD, "/api/proposals/260101-aaaa/resolve", None, "waiting: Duo Guild raids until Tue 11:30 PM (Barrow Deeps Tue 7:30 PM is locked)")]
    st = agents.read_status("apply")
    assert st["state"] == "idle" and "waiting (raid time" in st["result"] and "demo/260101-aaaa" in st["result"], "listed with its guild key"


def test_a_raid_starting_during_the_edit_defers_the_restart_to_a_later_poll(job):
    apply, state, claude = job
    calls = {"n": 0}

    def guilds():
        calls["n"] += 1
        return GUILDS if calls["n"] == 1 else BUSY  # quiet at the start; a run locked while Claude worked
    w = world(guilds=guilds, lists={(GUILD2, "approved"): [PROFILE]}, states={PROFILE["id"]: "applying"})
    assert apply.main([], api_factory=lambda guild=None: JobApi(w, guild), run_claude=claude) == 0
    st = agents.read_status("apply")
    assert state.restarts == 0 and st["restart_pending"]["auto"] is True and "the restart waits" in st["result"]
    # the next poll: quiet again → the restart happens, the proposal is applied, nothing re-edited
    w2 = world(lists={(GUILD2, "applying"): [PROFILE]}, states={PROFILE["id"]: "applying"})
    assert apply.main([], api_factory=lambda guild=None: JobApi(w2, guild), run_claude=claude) == 0
    assert state.restarts == 1 and state.claude == 1 and not agents.read_status("apply").get("restart_pending")
    assert (GUILD2, f"/api/proposals/{PROFILE['id']}/resolve", "applied", "checks passed, bot restarted healthy") in w2["posts"]
    assert not any(p[2] == "failed" for p in w2["posts"]), "the pending one is never 'recovered' as interrupted"


def test_apply_recovers_a_proposal_left_applying_and_does_every_guild_setting(job):
    apply, state, claude = job
    stuck = {"id": "260101-cccc", "kind": "profile", "created_at": "2026-01-01T00:00:00+00:00", "title": "t", "change": "c"}
    settings = [{"id": f"26010{i}-dddd", "kind": "guild_setting", "created_at": f"2026-01-0{i}T00:00:00+00:00", "title": "s", "change": "c"} for i in (2, 3)]
    w = world(lists={(GUILD, "applying"): [stuck], (GUILD, "approved"): settings[:1], (GUILD2, "approved"): settings[1:]})
    assert apply.main([], api_factory=lambda guild=None: JobApi(w, guild), run_claude=claude) == 0
    assert (GUILD, "/api/proposals/260101-cccc/resolve", "failed", apply.INTERRUPTED) in w["posts"]
    applied = [p[:2] for p in w["posts"] if p[1].endswith("/apply")]
    assert applied == [(GUILD, "/api/proposals/260102-dddd/apply"), (GUILD2, "/api/proposals/260103-dddd/apply")], "every guild setting this poll, oldest first"


def test_the_job_scripts_send_the_job_header():
    import httpx

    from oibot_gm import mcp_server as ms

    seen = []

    def handler(request):
        seen.append(request.headers.get("x-oibot-job"))
        return httpx.Response(200, json={"guilds": []})
    ms.Api(url="http://bot", token="x", transport=httpx.MockTransport(handler), job="apply").guilds()
    ms.Api(url="http://bot", token="x", transport=httpx.MockTransport(handler), job=None).guilds()
    assert seen == ["apply", None], "the job's own calls only; an MCP session never carries it"


# ---------------------------------------------------------------- policy: drafts on disk

def test_a_pending_policy_edit_survives_a_restart(reg):
    from oibot_gm.store import GitStore

    ps = pol.PolicyStore(reg.store, reg.key)
    pol.begin_edit(ps, "loot", "# Loot\n\n1. Mainspec first.\n2. Tanks first on shields.", "Kessa", by_id=5)
    pol.compile_pending(ps, "loot", FakeProvider())
    pol.PENDING.clear()  # the process is gone
    ps2 = pol.PolicyStore(GitStore(reg.store.root, push=False), reg.key)
    held = ps2.load_pending("loot")
    assert held["by"] == "Kessa" and isinstance(held["compiled"], pol.LootCompiled) and held["previous"] == pol.DEFAULTS["loot"]
    assert (reg.store.root / reg.key / "policy" / "loot.pending.json").exists()
    assert pol.confirm_pending(ps2, "loot", "Kessa").startswith("loot policy is live")
    c = ps2.compiled("loot")
    assert c["text_sha"] == pol.text_sha(ps2.read("loot")) and c["compiled_at"] and c["confirmed_by"] == "Kessa"
    assert not (reg.store.root / reg.key / "policy" / "loot.pending.json").exists(), "the draft file goes in the confirm's commit"
    st = pol.policy_state(ps2, "loot", reg.local12)
    assert not st["differs"] and st["items"] == ["Mainspec before offspec.", "Tanks first on shields."] and st["compiled_label"]
    reg.store.write_text(ps2.path("loot"), "# Loot\n\nA PR edit.\n")  # the text moves on without a reading
    assert pol.policy_state(ps2, "loot")["differs"], "Written vs Applied: computed from the sha, not inferred"


def test_an_appended_rule_never_rides_on_another_officers_draft(reg):
    ps = pol.PolicyStore(reg.store, reg.key)
    pol.begin_edit(ps, "loot", "# Loot\n\n1. Mainspec first.", "Kessa", by_id=5)
    with pytest.raises(pol.PolicyPendingError, match=r"^A draft by Kessa is waiting for confirmation \(not compiled yet\) — confirm or discard it first"):
        pol.append_rule(ps, "loot", "tanks first", "Jonny", by_id=6)
    new, line = pol.append_rule(ps, "loot", "tanks first", "Kessa", by_id=5)
    assert new.endswith("- tanks first\n") and "draft" in line, "the draft's own author continues it"
    with pytest.raises(pol.PolicyPendingError):
        pol.append_rule(ps, "persona", "x", "Kessa")


def test_policy_routes(reg, rs, monkeypatch, out_dir):
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    docs = c.get("/api/policy").json()["docs"]
    assert [d["doc"] for d in docs] == ["loot", "comp", "persona"] and all("differs" in d and "pending_by" in d for d in docs)
    r = c.post("/api/policy/loot/append", json={"text": "tanks first on shields"}, headers=site)
    assert r.status_code == 200 and r.json()["pending"] and r.json()["pending_by"] == "Viewer" and r.json()["text"].endswith("- tanks first on shields\n")
    assert c.post("/api/policy/loot/append", json={"text": "  "}, headers=site).status_code == 400
    bot.ctx = SimpleNamespace(provider=FakeProvider())
    r = c.post("/api/policy/loot/compile", headers=site)
    assert r.status_code == 200 and "R1" in r.json()["reading"] and r.json()["state"]["pending_compiled"]
    assert c.get("/api/policy/loot").json()["pending_reading"], "the reading waits on disk for the page"
    r = c.post("/api/policy/loot/confirm", headers=site)
    assert r.status_code == 200 and r.json()["compiled"] and not r.json()["pending"] and not r.json()["differs"]
    # the persona: draft → its own text as the reading → confirm (no model call)
    bot.ctx = SimpleNamespace(provider=None)
    assert c.put("/api/policy/persona", json={"text": "Be brief."}, headers=site).json()["pending"]
    r = c.post("/api/policy/persona/compile", headers=site)
    assert r.status_code == 200 and r.json()["reading"].startswith("Be brief.")
    assert c.post("/api/policy/persona/confirm", headers=site).json()["applied_text"] == "Be brief."
    member = clients(reg, rs, monkeypatch, officer=False)[1]
    assert member.get("/api/policy").status_code == 403


def test_the_persona_the_bot_speaks_with_is_the_confirmed_one(reg):
    ps = pol.PolicyStore(reg.store, reg.key)
    pol.begin_edit(ps, "persona", "Speak like a pirate.", "Kessa")
    assert ps.live_text("persona") == pol.DEFAULTS["persona"], "a draft is not the system prompt"
    pol.compile_pending(ps, "persona", None)
    pol.confirm_pending(ps, "persona", "Kessa")
    assert ps.live_text("persona") == "Speak like a pirate."


# ---------------------------------------------------------------- the manual on the site, the help links

def test_the_manual_splits_by_its_own_headings_and_tags_officer_sections(tmp_path):
    secs = help_mod.manual_sections()
    ids = [s["id"] for s in secs]
    assert {"3a", "7a", "8b", "8c", "10a"} <= set(ids), "every section, not /help's TOPICS list"
    tier = {s["id"]: s["tier"] for s in secs}
    assert tier["1"] is None and tier["6"] == "officers" and tier["8"] == "owner" and tier["8c"] == "owner"
    assert help_mod.manual_sections() is secs, "cached until the file changes"
    f = tmp_path / "m.md"
    f.write_text("# M\n\n## 1. One\nBody one.\n\n## 2a. Owner things\nBody two.\n")
    assert [(s["id"], s["tier"], s["body"]) for s in help_mod.manual_sections(f)] == [("1", None, "Body one."), ("2a", "owner", "Body two.")]
    time.sleep(0.01)
    f.write_text("# M\n\n## 9. Changed\nNew.\n")
    assert help_mod.manual_sections(f)[0]["title"] == "Changed", "re-read when the mtime moves"


def test_help_links_carry_the_guild_before_the_anchor(reg, monkeypatch):
    from oibot_gm import discord_help

    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    assert help_mod.site_url(reg, "/app/help", "4") == "https://gm.example/app/help?g=demo#4"
    long = discord_help.topic_parts(reg, "## 4", discord_help.manual_section("## 4"))
    assert len(long) == discord_help.HELP_PARTS and long[-1].endswith("continued on the site: https://gm.example/app/help?g=demo#4")
    short = discord_help.topic_parts(reg, "## 6", discord_help.manual_section("## 6"))
    assert short[-1].endswith("(https://gm.example/app/help?g=demo#6)") and all(len(p) <= 2000 for p in long + short)
    monkeypatch.setenv("OIBOT_WEB_URL", "")
    assert help_mod.site_url(reg, "/app/help") is None


def test_api_manual_shows_every_section_to_members_and_commands_by_tier(reg, rs, monkeypatch):
    import discord
    from discord import app_commands

    bot, member, _, _ = clients(reg, rs, monkeypatch, officer=False)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))

    @tree.command(name="help", description="what the bot does")
    async def _h(i: discord.Interaction):
        pass

    roster = app_commands.Group(name="roster", description="r")

    @roster.command(name="add", description="officers add")
    async def _a(i: discord.Interaction):
        pass

    tree.add_command(roster)
    bot.tree = tree
    d = member.get("/api/manual").json()
    assert d["tier"] == "member" and any(s["tier"] == "officers" for s in d["sections"]), "D38: tagged, never hidden"
    assert [c["path"] for c in d["commands"]] == ["/help"], "the commands this person may run"
    assert [c["path"] for c in help_mod.command_rows(tree, "officer")] == ["/help", "/roster add"]


# ---------------------------------------------------------------- the decide flow (harness)

def test_the_decide_flow_refuses_an_officers_game_data_approval_at_once(reg):
    from oibot_gm.discord_news import ProposalButton

    add_news(reg)
    p = ProposalStore(reg.store, reg.key).create(reg, proposal_data(kind="profile", edit=PROFILE_EDIT))
    client, _ = fake_client(reg, officer=True)
    h = Harness(reg, user=SimpleNamespace(id=5, display_name="Kessa"), client=client)
    itx = h.card_press()
    sent = []

    async def send_message(content=None, **kw):
        sent.append(content)
    itx.response.send_message = send_message
    run(ProposalButton(reg.key, p.id, "approve").callback(itx))
    assert sent == [news.PROFILE_OWNER_ONLY] and ProposalStore(reg.store, reg.key).get(p.id).state == "proposed"
    # the owner: the restart sentence names every served server, then Approve writes
    owner = Harness(reg, user=SimpleNamespace(id=OWNER, display_name="Owner"), client=client)
    run(ProposalButton(reg.key, p.id, "approve").callback(owner.card_press()))
    assert "the bot restarts for every server it serves (Demo Guild)" in owner.text and owner.buttons() == ["Approve", "Approve with a note…", "Back"]
    run(owner.press("Approve"))
    assert ProposalStore(reg.store, reg.key).get(p.id).state == "approved" and "the card is updated" in owner.text
    assert client.card_edits[-1] == (p.id, "approved")


def test_the_decide_flow_on_a_dismiss_and_back_changes_nothing(reg):
    from oibot_gm.discord_news import ProposalButton

    add_news(reg)
    p = ProposalStore(reg.store, reg.key).create(reg, proposal_data())
    client, lines = fake_client(reg, officer=True)
    h = Harness(reg, user=SimpleNamespace(id=5, display_name="Kessa"), client=client)
    run(ProposalButton(reg.key, p.id, "dismiss").callback(h.card_press()))
    assert "the review is told not to propose it again" in h.text and h.buttons() == ["Dismiss", "Dismiss with a note…", "Back"]
    run(h.press("Back"))
    assert ProposalStore(reg.store, reg.key).get(p.id).state == "proposed" and not lines
    run(ProposalButton(reg.key, p.id, "dismiss").callback(h.card_press()))
    run(h.press("Dismiss with a note…"))
    run(h.submit(note="already handled"))
    q = ProposalStore(reg.store, reg.key).get(p.id)
    assert (q.state, q.note) == ("dismissed", "already handled") and any("→ dismissed" in x and "already handled" in x for x in lines)


# ---------------------------------------------------------------- /gm policy show, /gm config plain

def test_policy_show_chunks_the_text_and_caps_the_reading(reg, monkeypatch):
    from oibot_gm.discord_policy import POLICY_SHOW_PARTS, RULES_SHOWN, pending_reading, policy_show_messages

    monkeypatch.setenv("OIBOT_WEB_URL", "https://gm.example")
    ps = pol.PolicyStore(reg.store, reg.key)
    long = "# Loot\n\n" + "\n\n".join(f"{i}. " + "A rule about who gets what and why it matters. " * 8 for i in range(1, 40))
    rules = pol.LootCompiled(rules=[pol.LootRule(id=f"R{i}", text=f"rule {i}", kind="ordering") for i in range(1, 21)], summary="Twenty rules.")
    pol.begin_edit(ps, "loot", long, "Kessa")
    ps.confirm("loot", rules, "Kessa", long)
    pol.begin_edit(ps, "loot", long + "\n\n- one more", "Jonny")
    texts, e = policy_show_messages(reg, ps, "loot")
    assert len(texts) == POLICY_SHOW_PARTS and all(len(t) <= 2000 for t in texts)
    assert texts[-1].endswith("continued on the site: https://gm.example/app/policy?g=demo#loot")
    assert e.description.count("\n") >= RULES_SHOWN and "+5 more on the site" in e.description
    assert "a draft by Jonny waits" in e.footer.text and "no longer reads this way" in e.footer.text
    sent = []

    async def send(**kw):
        sent.append(kw)
    assert not run(pending_reading(send, reg, ps, "loot", None)), "not compiled yet: no Confirm to show"
    pol.compile_pending(ps, "loot", FakeProvider())
    assert run(pending_reading(send, reg, ps, "loot", None)) and sent[-1]["view"].ACTIONS == ("confirm", "discard")


def test_the_edit_modal_is_append_only_past_discords_box(reg):
    from oibot_gm.discord_policy import PolicyEditModal

    ps = pol.PolicyStore(reg.store, reg.key)
    short = PolicyEditModal(reg, ps, "loot", None, None)
    assert not short.append_only and short.text.default == ps.read("loot")
    reg.store.write_text(ps.path("loot"), "x" * (pol.MODAL_MAX + 10))
    big = PolicyEditModal(reg, ps, "loot", None, None)
    assert big.append_only and not big.text.default, "never a truncated document in the box"
    assert "full text on the site" in big.text.label and len(big.text.label) <= 45


def test_gm_config_plain_reads_the_policy_from_here(reg):
    from oibot_gm.discord_pool import SetupMixin, plain_read_text

    reg.config.ops_channel_id, reg.config.plain.channels = 11, {"22": "answer"}

    class C(SetupMixin):
        def get_guild(self, gid):
            return None

        def guild_channels(self, r):
            return [{"id": "11", "name": "ops"}, {"id": "22", "name": "raid-chat"}, {"id": "33", "name": "general"}]
    table = C().plain_table(reg)
    assert {c["id"]: c["why"] for c in table["channels"]} == {"22": "listed", "11": "the ops channel acts unless listed"}
    here = plain_read_text(reg, table, 33)
    assert here.startswith("**Here (#general): Own record only** — the default for other channels")
    assert "• Runs: officers" in here and "• #raid-chat: Answers only — listed" in here and "The site and MCP: Acts (not configurable)" in here
    assert plain_read_text(reg, table, None).startswith("**Here (DMs): Own record only** — the DMs setting")
    assert "#general" not in plain_read_text(reg, table, 11).split("\n", 1)[1], "an unlisted channel shows only as 'here'"


def test_the_plain_command_exists_for_officers(reg):
    import discord
    from discord import app_commands

    from oibot_gm.discord_policy import PolicyContext, register_policy_commands
    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guilds = Guilds.of(reg.store, reg)
    register_commands(tree, guilds, Ops(client), lambda k, v: "")
    register_policy_commands(tree, guilds, Ops(client), SimpleNamespace(), PolicyContext(guilds))
    config = next(c for c in next(c for c in tree.get_commands() if c.name == "gm").commands if c.name == "config")
    assert any(c.name == "plain" for c in config.commands) and len(config.commands) <= 25
    assert help_mod.gate_of("/gm config plain") == "officer"


def test_plain_footer_is_one_helper_and_idempotent():
    from oibot_gm import configops

    once = configops.plain_footer("⛔ Only the owner can change raids & auras.")
    assert once.endswith(configops.PLAIN_POLICY_FOOTER) and configops.plain_footer(once) == once


# ---------------------------------------------------------------- leftovers after a restart, usage per guild, quiet

def test_leftover_screens_are_edited_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_views, "PATH", tmp_path / "pending_views.jsonl")
    monkeypatch.setattr(pending_views, "_live", {})
    monkeypatch.setattr(pending_views, "ENABLED", True)
    pending_views.track("pv:abc", channel_id=7, message_id=8)
    pending_views.track_interaction("wiz:1", SimpleNamespace(application_id=None, token=None))  # nothing to edit later: not kept
    assert pending_views.live_count() == 1 and (tmp_path / "pending_views.jsonl").read_text().count("\n") == 1
    pending_views._live.clear()  # a new process
    edits = []

    class Msg:
        async def edit(self, **kw):
            edits.append(kw)
    bot = SimpleNamespace(get_channel=lambda cid: SimpleNamespace(get_partial_message=lambda mid: Msg()) if cid == 7 else None)
    assert run(pending_views.restore_pending_views(bot)) == 1
    assert edits == [{"content": pending_views.RESTARTED, "embed": None, "view": None}]
    assert (tmp_path / "pending_views.jsonl").read_text() == "", "the file mirrors this process again"


def test_quiet_counts_runs_locks_views_and_previews(reg, rs, monkeypatch):
    from test_mcp import FakeBot

    from oibot_gm.web.api_agents import quiet_state

    bot = FakeBot(reg, rs)
    assert quiet_state(bot) == {"quiet": True, "why": []}
    start = (reg.now_local() + timedelta(minutes=90)).replace(second=0, microsecond=0)
    rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    q = quiet_state(bot, views=2, previews=1)
    assert not q["quiet"] and any("locks or starts within 2 h" in w for w in q["why"]) and "2 wizard or confirm screen(s) open in Discord" in q["why"]
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    c = TestClient(web.create_app(bot))
    r = c.get("/api/agents/quiet", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert r["quiet"] is False and r["why"]


def test_agents_overview_host_facts_are_the_owners(reg, rs, monkeypatch, out_dir):
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    officer = c.get("/api/agents").json()
    assert officer["apply_mode"]["mode"] in ("main", "pr") and "mcp" not in officer and "llm" not in officer and officer["ops_channel"] is False
    apply = next(j for j in officer["jobs"] if j["job"] == "apply")
    assert apply["install"] == "sh scripts/agents/install.sh apply" and apply["elsewhere"] is False
    owner = c.get("/api/agents", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert owner["mcp"]["configured"] and owner["mcp"]["local_only"] and "llm" in owner


def test_llm_usage_is_shown_per_guild_under_one_cap(tmp_path, monkeypatch):
    from test_budget_and_find import make

    from oibot_gm.llm import provider as pv

    p = make(tmp_path, monkeypatch)
    month = p._month()
    p.log = [{"month": month, "guild": "demo", "workload": "help", "cost": 0.5}, {"month": month, "guild": "duo", "workload": "help", "cost": 0.25},
             {"month": month, "workload": "help", "cost": 0.1}, {"month": "2000-01", "guild": "demo", "workload": "help", "cost": 9}]
    assert p.usage_by_guild() == {"demo": 0.5, "duo": 0.25, "": 0.1}
    with pv.for_guild("demo"):
        assert pv.CURRENT_GUILD.get() == "demo"
        assert run(asyncio.to_thread(pv.CURRENT_GUILD.get)) == "demo", "a worker thread keeps the guild"
    assert pv.CURRENT_GUILD.get() == ""
    p.budget_usd = 0.1
    with pytest.raises(pv.BudgetExceeded, match="the host raises it"):
        p.complete("help", "s", "u", SimpleNamespace)
