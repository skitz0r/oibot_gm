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


def test_a_bare_mcp_bearer_never_decides_a_proposal(reg, rs, monkeypatch, out_dir):
    """Regression (G2 review): the owner's token alone (no X-Oibot-Job, no X-Oibot-Actor) approved a game-data proposal
    — mcp_viewer is the owner, so the owner-only rule was met by the token. D8: a decision from MCP is a NAMED
    officer's (X-Oibot-Actor, validated now); a game-data approval, the owner's. The ops path refuses it the same way."""
    add_news(reg)
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    bearer = {k: v for k, v in job.items() if k != "X-Oibot-Job"}
    ps = ProposalStore(reg.store, reg.key)
    prof = ps.create(reg, proposal_data(kind="profile", edit=PROFILE_EDIT))
    setting = ps.create(reg, proposal_data(title="Barrow Deeps resets weekly (again)"))
    for pid, state in ((prof.id, "approved"), (prof.id, "dismissed"), (setting.id, "approved"), (setting.id, "dismissed")):
        r = c.post(f"/api/proposals/{pid}/resolve", json={"state": state}, headers=bearer)
        assert r.status_code == 403 and "X-Oibot-Actor" in r.json()["error"] and "Demo Guild" in r.json()["error"], (pid, state, r.text)
    r = c.post("/api/ops/change", json={"ops": [{"op": "proposal", "target": prof.id, "value": "approve"}], "apply": True, "confirmed": True}, headers=bearer)
    assert not r.json().get("applied") and any("X-Oibot-Actor" in str(x) for x in r.json().get("refused", [])), r.text
    assert ps.get(prof.id).state == "proposed" and ps.get(setting.id).state == "proposed", "nothing moved"
    officer = {**bearer, "X-Oibot-Actor": "42"}
    r = c.post(f"/api/proposals/{prof.id}/resolve", json={"state": "approved"}, headers=officer)
    assert r.status_code == 403 and "Only the owner approves a game-data change" in r.json()["error"], r.text
    assert c.post(f"/api/proposals/{setting.id}/resolve", json={"state": "approved"}, headers=officer).status_code == 200
    owner = {**bearer, "X-Oibot-Actor": str(OWNER)}
    assert c.post(f"/api/proposals/{prof.id}/resolve", json={"state": "approved"}, headers=owner).status_code == 200
    assert ps.get(prof.id).state == "approved"


def test_mcp_resolve_proposal_sends_the_actor_only_on_a_decision(monkeypatch):
    import httpx
    from oibot_gm import mcp_server as ms

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("x-oibot-actor"))
        return httpx.Response(200, json={"message": "ok"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        monkeypatch.setenv(ms.ACTOR_ENV, "42")
        ms.resolve_proposal("260101-aaaa", "approved")
        ms.resolve_proposal("260101-aaaa", "dismissed")
        ms.resolve_proposal("260101-aaaa", "applying")
    finally:
        ms.use(None)
    assert calls == ["42", "42", None]


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


def test_a_live_loot_council_holds_a_restart_however_late_it_runs(reg):
    """Regression (G2 review): the council counted only while its run was open/locked and before its scheduled close —
    a council still going 8 h after the start (D10 ends one only past close + 30 min idle) read raiding: False, so the
    apply job could restart in the middle of it. Every live council counts on its own; a test run's never does."""
    late = ev(reg, state="locked", hours=-8, key="bd-late")
    assert datetime.now(timezone.utc) > rc.run_close_at(reg, late), "the run is past its scheduled close"
    assert not news.guild_busy(reg, [late])["raiding"], "no council: past close is not raid time"
    council = SimpleNamespace(run_key="bd-late", test=False, instance="barrow_deeps")
    for loot in (["bd-late"], [council]):
        b = news.guild_busy(reg, [late], loot_runs=loot)
        assert b["raiding"] and "the loot council is open" in b["why"], loot
        assert datetime.fromisoformat(b["until"]) >= datetime.now(timezone.utc) + timedelta(minutes=news.BUSY_LOOT_MIN - 1)
    gone = SimpleNamespace(run_key="bd-done", test=False, instance="barrow_deeps")  # the scheduler marked the run done
    b = news.guild_busy(reg, [ev(reg, state="done", hours=-8, key="bd-done")], loot_runs=[gone])
    assert b["raiding"] and b["why"].startswith(reg.raid_def("barrow_deeps").get("name", "barrow_deeps"))
    assert news.guild_busy(reg, [], loot_runs=[gone])["raiding"], "a council whose run isn't among the live ones still counts"
    early = ev(reg, state="locked", hours=72, key="bd-early")
    assert not news.guild_busy(reg, [early], loot_runs=["bd-early"])["raiding"], "a council opened days early is not raid time yet"
    rehearsal = SimpleNamespace(run_key="bd-t", test=True, instance="barrow_deeps")
    assert not news.guild_busy(reg, [], loot_runs=[rehearsal])["raiding"]
    assert not news.guild_busy(reg, [ev(reg, state="locked", hours=-8, key="bd-t", test=True)], loot_runs=["bd-t"])["raiding"]


def test_the_bot_counts_its_live_councils_and_the_quiet_check_too(reg):
    from oibot_gm.discord_news import NewsMixin
    from oibot_gm.web import api_agents

    council = SimpleNamespace(run_key="bd-late", test=False, instance="barrow_deeps")
    bot = SimpleNamespace(raids=None, loot_sessions=lambda r: [council], registries=SimpleNamespace(ordered=lambda: [reg]))
    assert NewsMixin.guild_busy(bot, reg)["raiding"], "the bot's busy map passes the sessions themselves"
    q = api_agents.quiet_state(bot)
    assert not q["quiet"] and any("the loot council is open" in w for w in q["why"])
    bot.loot_sessions = lambda r: [SimpleNamespace(run_key="bd-t", test=True, instance="barrow_deeps")]
    assert not NewsMixin.guild_busy(bot, reg)["raiding"] and api_agents.quiet_state(bot)["quiet"]


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


# ---------------------------------------------------------------- review fixes (G2): status lines, recovery, refusals, SSE, attribution

def test_gm_status_carries_the_news_review_and_apply_lines(reg, out_dir):
    """Design §3.4 OFF-13/OFF-15: 'News review: last <t:f> · <result> · next <t:R> · N proposals waiting' and
    'Apply: idle | running | busy with another server's proposal | failed: <result>' — Discord stamps, never a clock."""
    from oibot_gm.discord_news import agents_status_lines

    add_news(reg)
    review, apply = agents_status_lines(reg, out=out_dir)
    assert review == "News review: never ran here · next " + review.split("next ", 1)[1] and review.endswith("0 proposals waiting")
    assert apply == "Apply: idle"
    ProposalStore(reg.store, reg.key).create(reg, proposal_data())
    agents.write_status("review", out_dir, state="idle", started="2026-10-20T16:00:00+00:00", finished="2026-10-20T16:04:00+00:00", result="1 proposal posted")
    review, _ = agents_status_lines(reg, out=out_dir)
    assert review.startswith("News review: last <t:") and ":f> · 1 proposal posted · next <t:" in review and review.endswith(":R> · 1 proposal waiting")
    assert " AM" not in review and " PM" not in review, "Discord renders the stamps in the reader's own clock"
    agents.write_status("apply", out_dir, state="failed", result="checks failed: tests/test_x.py")
    assert agents_status_lines(reg, out=out_dir)[1] == "Apply: failed: checks failed: tests/test_x.py"
    import os

    agents.write_status("apply", out_dir, state="running", pid=os.getpid(), guild="duo", step="running the tests")
    assert agents_status_lines(reg, out=out_dir, many=True)[1] == "Apply: busy with another server's proposal"
    assert agents_status_lines(reg, out=out_dir, many=False)[1] == "Apply: running: running the tests"


def test_gm_status_shows_the_agents_field(reg, monkeypatch, out_dir):
    import discord
    from discord import app_commands

    from oibot_gm import discord_news
    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guilds = Guilds.of(reg.store, reg)
    register_commands(tree, guilds, Ops(client), lambda k, v: "")
    bot = SimpleNamespace(registries=guilds, ctx=None, governance_status=lambda r: discord_news.agents_status_lines(r, out=out_dir), get_guild=lambda gid: None)
    h = Harness(reg, user=SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"), client=bot)
    run(tree.get_command("gm").get_command("status").callback(h.command()))
    agents_field = next(f for f in h.screen["embed"].fields if f.name == "Agents")
    assert agents_field.value.startswith("News review: never ran here") and agents_field.value.endswith("Apply: idle")


def owner_cookie(c, uid=OWNER):
    from itsdangerous import URLSafeSerializer

    c.cookies.set(web.COOKIE, URLSafeSerializer("s" * 40, salt="session").dumps({"uid": str(uid), "name": "Owner", "iat": time.time()}))


def test_the_owner_marks_an_interrupted_apply_run_while_the_job_is_not_running(reg, rs, monkeypatch, out_dir):
    """Design §3.4 apply job (5): a proposal an apply run left `applying` is the owner's to mark failed (the job's own
    recovery words) from the Agents page — never while the job runs, never an officer, never the MCP token."""
    import os

    add_news(reg)
    bot, c, job, site = clients(reg, rs, monkeypatch, officer=True)
    ps = ProposalStore(reg.store, reg.key)
    p = ps.create(reg, proposal_data())
    ps.move(p.id, "approved", "Kessa", officer=True, job=False, by_id=5)
    ps.move(p.id, "applying", "the apply job", officer=False, job=True)
    shown = next(x for x in c.get("/api/proposals").json()["proposals"] if x["id"] == p.id)
    assert shown["consequence"]["interrupted"][0].startswith("Marks it failed") and "approve it again" in shown["consequence"]["interrupted"][0]
    url = f"/api/proposals/{p.id}/interrupted"
    assert c.post(url, json={}, headers=site).status_code == 403, "an officer who isn't the owner"
    bearer = {k: v for k, v in job.items() if k != "X-Oibot-Job"}
    assert c.post(url, json={}, headers=bearer).status_code == 403, "a person's recovery press, not the token's"
    owner_cookie(c)
    agents.write_status("apply", out_dir, state="running", pid=os.getpid())
    r = c.post(url, json={}, headers=site)
    assert r.status_code == 409 and "apply job is running" in r.json()["error"]
    agents.write_status("apply", out_dir, state="idle", pid=None)
    r = c.post(url, json={}, headers=site)
    assert r.status_code == 200, r.text
    q = ps.get(p.id)
    assert (q.state, q.note, q.history[-1]["by"]) == ("failed", news.INTERRUPTED, "Viewer")
    assert c.post(url, json={}, headers=site).status_code == 400, "only an applying proposal"
    p2 = ps.create(reg, proposal_data(title="Another one"))
    ps.move(p2.id, "approved", "Kessa", officer=True, job=False, by_id=5)
    ps.move(p2.id, "applying", "the apply job", officer=False, job=True)
    with pytest.raises(PermissionError, match="only the owner marks an apply run as interrupted"):
        ps.move(p2.id, "failed", "Kessa", officer=True, job=False, by_id=5, owner=False, recover=True)
    with pytest.raises(PermissionError, match="only the apply job reports that"):
        ps.move(p2.id, "applied", "the owner", officer=True, job=False, owner=True, recover=True)
    apply = load_script("apply")
    assert apply.INTERRUPTED == news.INTERRUPTED, "the job's recovery and the owner's press say the same"


def test_a_game_data_proposal_citing_only_dismissed_news_is_refused(duo):
    """Design §3.4 OFF-14: was only the review prompt's rule — now code: a profile proposal citing nothing but news a
    dismissed game-data proposal (here or in a guild on the same game version) cited is refused; newer news is fine."""
    from test_news import URL

    add_news(duo.a)
    ps = ProposalStore(duo.a.store, GUILD)
    first = ps.create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT))
    ps.move(first.id, "dismissed", "Kessa", officer=True, job=False, by_id=5)
    with pytest.raises(news.ProposalError, match=f"cited by {first.id}, which an officer dismissed"):
        ps.create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT))
    assert ps.create(duo.a, proposal_data()).kind == "guild_setting", "a guild setting is the officers' to judge again"
    newer = "https://example.com/news/newer"
    add_news(duo.a, url=newer, title="Barrow Deeps hotfix: 10-player lockout clarified on WoW: Forever")
    assert ps.create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT, news=[URL, newer])).state == "proposed"
    add_news(duo.b)
    with pytest.raises(news.ProposalError, match=f"Demo Guild's {first.id}"):
        ProposalStore(duo.b.store, GUILD2).create(duo.b, proposal_data(kind="profile", edit=PROFILE_EDIT))
    assert news.consequence(first, duo.a, "dismissed")[0].startswith("Dismissed: nothing changes, and a game-data proposal citing only this news is refused")


def test_every_policy_write_publishes_to_the_officers_policy_pages(reg, monkeypatch):
    """S4: a second officer's Policy page reloads on another officer's draft, reading, confirm or discard — from any
    surface (policy.POLICY_LISTENERS; web/api_policy.py publishes the officers-only `policy` topic)."""
    from fastapi import FastAPI

    from oibot_gm.web.api_policy import install_policy_api

    monkeypatch.setattr(pol, "POLICY_LISTENERS", [])
    sent = []
    install_policy_api(FastAPI(), SimpleNamespace(registries=SimpleNamespace(store=reg.store)), who=None, body=None,
                       publish=lambda topic, guild, **kw: sent.append((topic, guild, kw)))
    install_policy_api(FastAPI(), SimpleNamespace(registries=SimpleNamespace(store=reg.store)), who=None, body=None,
                       publish=lambda topic, guild, **kw: sent.append((topic, guild, kw)))
    assert len(pol.POLICY_LISTENERS) == 1, "a second app replaces the listener, never stacks it"
    ps = pol.PolicyStore(reg.store, reg.key)
    pol.append_rule(ps, "loot", "Tanks first on tank trinkets", "Kessa", by_id=5)
    pol.compile_pending(ps, "loot", FakeProvider())
    pol.confirm_pending(ps, "loot", "Kessa")
    pol.begin_edit(ps, "comp", "# Comp\n\n- keep A with B", "Kessa")
    pol.discard_pending(ps, "comp", "Kessa")
    assert sent == [("policy", reg.key, {"officers": True})] * 5


def test_every_append_writes_policy_appended(reg):
    """THE policy_append verb: the site / MCP (policy.append_rule), the `policy_append` op (plain text), /gm rule and
    the long-document edit box all write policy.appended()'s text, and all are refused on another officer's draft."""
    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp
    from oibot_gm.registry import RegistryError

    ps = pol.PolicyStore(reg.store, reg.key)
    base = ps.read("loot")
    op = ConfigOp(op="policy_append", doc="loot", text="- Tanks first on tank trinkets")
    assert configops._policy_edit(reg, op, ps)[1] == pol.appended(base, "Tanks first on tank trinkets") == base.rstrip() + "\n- Tanks first on tank trinkets\n"
    configops.apply(reg, op, "Kessa", True, ps, None, None, 5)
    assert ps.read("loot") == base.rstrip() + "\n- Tanks first on tank trinkets\n" and ps.load_pending("loot")["by"] == "Kessa"
    with pytest.raises(RegistryError, match="A draft by Kessa is waiting"):
        configops.apply(reg, ConfigOp(op="policy_append", doc="loot", text="Healers second"), "Jonny", True, ps, None, None, 6)
    assert pol.rule_bullet("Two lines\nof one rule") == "- Two lines\n  of one rule\n" and pol.rule_bullet(" - ") == ""
    assert pol.appended("# P\n", "a paragraph", bullet=False) == "# P\n\na paragraph\n"
    import inspect

    from oibot_gm import discord_policy, flows_misc

    assert "append_rule(" in inspect.getsource(flows_misc.RuleWizard) and "append_rule(" in inspect.getsource(discord_policy.PolicyEditModal.on_submit)


def test_loot_judging_and_the_sites_parses_are_attributed_to_their_guild(reg, rs, monkeypatch):
    """D45: the loot council's judge (worker threads), the plain-text parse (Discord, the site's text box, MCP) and the
    help answer record their guild — no longer '' (unattributed)."""
    from oibot_gm import configops
    from oibot_gm.llm import provider as pv
    from oibot_gm.loot import recommend as rec_mod, session as ls

    seen = []

    class P:
        def complete(self, workload, system, user, schema):
            seen.append((workload, pv.CURRENT_GUILD.get()))
            raise RuntimeError("stop here")

    with pytest.raises(RuntimeError):
        configops.parse(P(), reg, "set the timezone to Europe/Paris")
    monkeypatch.setattr(help_mod, "system_prompt", lambda tree: "s")
    monkeypatch.setattr(help_mod, "guild_state", lambda *a, **kw: "state")
    with pytest.raises(RuntimeError):
        help_mod.answer(P(), None, reg, rs, OWNER, True, "when is the raid?")
    real = rec_mod.recommend

    def recommend(profile, item, cands, policy, provider, cfg=None):
        if provider is not None:
            seen.append(("loot_recommend", pv.CURRENT_GUILD.get()))
        return real(profile, item, cands, policy, None, cfg=cfg)
    monkeypatch.setattr(rec_mod, "recommend", recommend)
    from test_loot_verbs import LootBot, real_run

    ev, _ = real_run(reg, rs)
    bot = LootBot(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=1))
    run(bot.tick(reg, s, add=[{"name": "Shard of Doom", "boss": "Gatekeeper", "copies": 1}], by="Officer", by_id=1))
    ls.run_distribution(bot.loot_ctx(s), ls.snapshot(s), bot.loot_roster(s), [s.drop_list[0].drop_id], provider=object())
    assert seen == [("config_change", GUILD), ("help", GUILD), ("loot_recommend", GUILD)]
    assert pv.CURRENT_GUILD.get() == ""


def test_gm_policy_show_runs_through_the_guild_resolver(reg):
    """The slash callback itself (not only policy_show_messages): guild_routing.need picks the guild, officers only,
    the document then its reading; an unconfigured server is told once and never reaches another guild."""
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
    show = tree.get_command("gm").get_command("policy").get_command("show")
    loot = app_commands.Choice(name="loot", value="loot")
    bot = SimpleNamespace(registries=guilds, get_guild=lambda gid: None)
    h = Harness(reg, user=SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"), client=bot)
    run(show.callback(h.command(), loot))
    assert h.screens[0]["text"].startswith("**The loot policy — written**") and "Mainspec before offspec" in h.screens[0]["text"]
    assert any(s["embed"] is not None for s in h.screens[1:]), "the compiled reading follows the text"
    member = Harness(reg, user=SimpleNamespace(id=777, display_name="Member", mention="@Member"), client=bot)
    run(show.callback(member.command(), loot))
    assert [s["text"] for s in member.screens] == ["Officers only."]
    stranger = Harness(reg, user=SimpleNamespace(id=OWNER, display_name="Owner", mention="@Owner"), client=bot)
    stranger.guild_id = 999
    run(show.callback(stranger.command(), loot))
    assert len(stranger.screens) == 1 and "set up" in stranger.screens[0]["text"]
