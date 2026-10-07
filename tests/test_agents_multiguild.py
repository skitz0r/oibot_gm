"""Several guilds and the news jobs (design §5.32, §5.26): the review runs one session per guild WITH news, pinned to
that guild (OIBOT_MCP_GUILD in the child's environment), with a `since` per guild (an older single value migrates);
runs are tagged and each guild sees only its own (untagged legacy runs belong to the first guild, K7); apply takes the
oldest approval across guilds and resolves it in its guild; a profile edit is shared by every guild on that game
version — one proposal is enough, the owner of every guild on it approves (K3) and every one of them hears it landed."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from conftest import GUILD, GUILD2, OWNER, OWNER2
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_agents import load_script
from test_news import add_news, proposal_data

from oibot_gm import agents, news
from oibot_gm.news import ProposalStore
from oibot_gm.web import app as web

TOKEN = "t" * 64
PROFILE_EDIT = {"file": "profiles/forever/raids.yaml", "key": "barrow_deeps.size", "value": 20}


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Every agents function reads OUT at call time, so the scripts' own calls land here too — never in the real out/."""
    monkeypatch.setattr(agents, "OUT", tmp_path / "agents")
    monkeypatch.setattr(agents, "launchd_status", lambda label: {"loaded": False, "state": None, "pid": None, "last_exit": None})
    return tmp_path / "agents"


# ---------------------------------------------------------------- the review, per guild

class FakeApi:
    """A guild-scoped stand-in for mcp_server.Api: news per guild, records what each guild was asked."""

    def __init__(self, world, guild=None):
        self.world, self.guild = world, guild

    def guilds(self):
        return [{"key": GUILD, "name": "Demo Guild"}, {"key": GUILD2, "name": "Duo Guild"}]

    def post(self, path, body=None):
        self.world["calls"].append((self.guild, "POST", path))
        return {"added": 0}

    def get(self, path, **params):
        self.world["calls"].append((self.guild, "GET", path, params.get("since"), params.get("state")))
        if path == "/api/news":
            return {"items": self.world["news"].get(self.guild, [])}
        if path == "/api/proposals":
            return {"proposals": self.world["approved"].get(self.guild, [])}
        raise AssertionError(path)


def test_review_runs_one_session_per_guild_with_news_pinned_to_it(out_dir, monkeypatch):
    review = load_script("review")
    world = {"calls": [], "news": {GUILD2: [{"url": "u"}]}, "approved": {}}
    sessions = []

    def fake_claude(cmd, prompt, transcript, on_step=None, timeout_s=0, env=None):
        sessions.append((env["OIBOT_MCP_GUILD"], prompt.rsplit("## This run", 1)[1]))
        return 0
    agents.write_status("review", state="idle", last_ok_started="2026-09-01T00:00:00+00:00")  # an old single value
    assert review.main([], api_factory=lambda guild=None: FakeApi(world, guild), run_claude=fake_claude) == 0
    assert [s[0] for s in sessions] == [GUILD2], "demo had no news: no session; duo's session is pinned to duo"
    assert "- guild: Duo Guild (duo)" in sessions[0][1]
    sinces = [c[3] for c in world["calls"] if c[2] == "/api/news"]
    assert sinces == ["2026-09-01T00:00:00+00:00"] * 2, "the old single value is every guild's since until it has its own"
    st = agents.read_status("review")
    assert set(st["last_ok_started"]) == {review.LEGACY, GUILD, GUILD2} and st["state"] == "idle"
    assert "2 guild(s)" in st["result"] and "Duo Guild: 1 item(s) reviewed" in st["result"]
    runs = agents.list_runs("review")
    assert sorted(r["guild"] for r in runs) == [GUILD, GUILD2], "one run per guild, tagged"
    world["calls"].clear()
    review.main([], api_factory=lambda guild=None: FakeApi(world, guild), run_claude=fake_claude)
    assert {c[0]: c[3] for c in world["calls"] if c[2] == "/api/news"} == {GUILD: st["last_ok_started"][GUILD], GUILD2: st["last_ok_started"][GUILD2]}


def test_the_jobs_still_work_against_a_bot_from_before_several_guilds(out_dir):
    """The jobs run from this checkout and can meet the running (older) bot: no /api/guilds = one guild, no header."""
    import httpx

    from oibot_gm import mcp_server as ms

    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("x-oibot-guild")))
        if request.url.path == "/api/guilds":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json={"items": [], "added": 0, "proposals": []})
    review = load_script("review")
    make = lambda guild=None: ms.Api(url="http://bot", token="x", transport=httpx.MockTransport(handler), guild=guild)  # noqa: E731
    agents.write_status("review", state="idle", last_ok_started="2026-09-01T00:00:00+00:00")
    assert review.main([], api_factory=make) == 0
    assert all(h is None for _, h in seen) and ("/api/news", None) in seen
    st = agents.read_status("review")
    assert st["result"].startswith("no new news since") and review.since_for(st, "oi") == st["last_ok_started"][""], "the next (new) bot picks up from here"
    assert not agents.list_runs("review")[0]["guild"]
    apply = load_script("apply")
    assert apply.main([], api_factory=make) == 0 and agents.read_status("apply")["result"] == "nothing approved"


def test_apply_takes_the_oldest_approval_across_guilds_and_resolves_it_there(out_dir):
    apply = load_script("apply")
    world = {"calls": [], "news": {}, "approved": {
        GUILD: [{"id": "260102-aaaa", "kind": "needs_developer", "created_at": "2026-01-02T00:00:00+00:00", "title": "later", "change": "c"}],
        GUILD2: [{"id": "260101-bbbb", "kind": "needs_developer", "created_at": "2026-01-01T00:00:00+00:00", "title": "first", "change": "c"}]}}
    resolved = []

    class Api(FakeApi):
        def post(self, path, body=None):
            resolved.append((self.guild, path, (body or {}).get("state")))
            return {}
    assert apply.main([], api_factory=lambda guild=None: Api(world, guild)) == 0
    assert resolved == [(GUILD2, "/api/proposals/260101-bbbb/resolve", "failed")], "duo's is older: resolved in duo"
    run = agents.list_runs("apply")[0]
    assert run["guild"] == GUILD2
    assert "from Duo Guild (duo)" in apply.commit_message({"id": "x", "title": "t", "change": "c"}, {"key": GUILD2, "name": "Duo Guild"})


# ---------------------------------------------------------------- runs per guild on the site

class Bot:
    def __init__(self, duo, officer=True):
        from test_mcp import FakeBot

        self.__dict__.update(FakeBot(duo.a, duo.rs_a).__dict__)
        self.registries = duo.guilds
        self._officer = officer
        self.lines = []
        stores = {GUILD: duo.rs_a, GUILD2: duo.rs_b}
        self.raids = type("S", (), {"store": lambda _s, r: stores[r.key]})()

        async def emit(cfg, level, text, exc=None, dm=True):
            self.lines.append((cfg.key, text))
        self.ops = SimpleNamespace(emit=emit, recent_for=lambda k: [])

    def get_guild(self, gid):
        return SimpleNamespace(id=gid)

    async def cached_member(self, guild, uid, max_age=300):
        return SimpleNamespace(id=uid, display_name="Viewer")

    def officiates(self, member, guild):
        return self._officer

    def guild_channels(self, reg):
        return []


def site(duo, monkeypatch, uid=42):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", "s" * 40)
    bot = Bot(duo)
    c = TestClient(web.create_app(bot))
    c.cookies.set(web.COOKIE, URLSafeSerializer("s" * 40, salt="session").dumps({"uid": str(uid), "name": "Viewer", "iat": time.time()}))
    return bot, c


def test_each_guild_sees_only_its_own_runs_and_legacy_runs_go_to_the_first(duo, monkeypatch, out_dir):
    legacy, _ = agents.new_run("review")  # written before runs carried a guild
    mine, _ = agents.new_run("review", guild=GUILD)
    theirs, _ = agents.new_run("review", guild=GUILD2)
    bot, c = site(duo, monkeypatch)
    sel = {"X-Oibot-Guild": GUILD2}
    ids = [r["id"] for r in c.get("/api/agents", headers=sel).json()["runs"]]
    assert ids == [theirs], "no legacy-guild file yet with two guilds: untagged runs are hidden"
    assert c.get(f"/api/agents/run/{mine}", headers=sel).status_code == 404 and c.get(f"/api/agents/run/{legacy}", headers=sel).status_code == 404
    assert agents.tag_legacy_runs(GUILD) and not agents.tag_legacy_runs(GUILD2), "written once, never rewritten"
    ids = sorted(r["id"] for r in c.get("/api/agents", headers={"X-Oibot-Guild": GUILD}).json()["runs"])
    assert ids == sorted([legacy, mine])
    assert c.get(f"/api/agents/run/{theirs}", headers={"X-Oibot-Guild": GUILD}).status_code == 404
    everything = c.get("/api/agents", headers={"Authorization": f"Bearer {TOKEN}", "X-Oibot-Guild": GUILD2}).json()["runs"]
    assert len(everything) == 3, "the MCP bearer (the owner's jobs) sees every run"


def test_one_guild_sees_every_run_as_before(reg, rs, monkeypatch, out_dir):
    from test_agents import clients

    rid, _ = agents.new_run("review")
    _, c, _, _ = clients(reg, rs, monkeypatch)
    assert [r["id"] for r in c.get("/api/agents").json()["runs"]] == [rid]


# ---------------------------------------------------------------- shared profile proposals (K3)

def test_a_profile_proposal_is_proposed_once_per_game_version(duo, monkeypatch):
    bot, c = site(duo, monkeypatch)
    for r in (duo.a, duo.b):
        add_news(r)
    job = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}
    first = c.post("/api/proposals", json=proposal_data(kind="profile", edit=PROFILE_EDIT), headers={**job, "X-Oibot-Guild": GUILD}).json()
    assert first["id"]
    dup = c.post("/api/proposals", json=proposal_data(kind="profile", edit=PROFILE_EDIT), headers={**job, "X-Oibot-Guild": GUILD2}).json()
    assert dup["duplicate_of"] == f"{GUILD}/{first['id']}" and not ProposalStore(duo.b.store, GUILD2).all()
    ok = c.post("/api/proposals", json=proposal_data(), headers={**job, "X-Oibot-Guild": GUILD2}).json()
    assert ok["id"], "a guild setting is per guild: never a duplicate"


def test_approving_a_shared_profile_edit_needs_every_owner(duo):
    add_news(duo.a)
    ps = ProposalStore(duo.a.store, GUILD)
    p = ps.create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT))
    owners = news.shared_profile_owners(duo.guilds, duo.a)
    assert owners == [("Demo Guild", OWNER), ("Duo Guild", OWNER2)]
    with pytest.raises(PermissionError, match="Duo Guild also uses"):
        ps.move(p.id, "approved", "Demo owner", officer=True, job=False, by_id=OWNER, profile_owners=owners)
    with pytest.raises(PermissionError, match="Demo Guild, Duo Guild also use"):
        ps.move(p.id, "approved", "an officer", officer=True, job=False, by_id=5, profile_owners=owners)
    assert ps.move(p.id, "dismissed", "an officer", officer=True, job=False, by_id=5, profile_owners=owners).state == "dismissed", "dismissing stays open"
    q = ps.create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT))
    duo.b.config.owner_discord_id = OWNER  # one person owns both
    assert ps.move(q.id, "approved", "owner", officer=True, job=False, by_id=OWNER, profile_owners=news.shared_profile_owners(duo.guilds, duo.a)).state == "approved"
    duo.b.config.game_profile = "tbc"
    assert news.shared_profile_owners(duo.guilds, duo.a) == [], "another game version: nothing shared"


def test_the_site_refuses_a_shared_profile_approval_and_tells_every_guild_when_it_lands(duo, monkeypatch):
    bot, c = site(duo, monkeypatch)
    add_news(duo.a)
    ps = ProposalStore(duo.a.store, GUILD)
    p = ps.create(duo.a, proposal_data(kind="profile", edit=PROFILE_EDIT))
    officer = {"X-Requested-With": "oibot", "X-Oibot-Guild": GUILD}
    r = c.post(f"/api/proposals/{p.id}/resolve", json={"state": "approved"}, headers=officer)
    assert r.status_code == 403 and "Demo Guild, Duo Guild also use" in r.json()["error"]
    duo.b.config.owner_discord_id = OWNER
    ps.move(p.id, "approved", "owner", officer=True, job=False, by_id=OWNER, profile_owners=news.shared_profile_owners(duo.guilds, duo.a))
    job = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot", "X-Oibot-Guild": GUILD}
    for state in ("applying", "applied"):
        assert c.post(f"/api/proposals/{p.id}/resolve", json={"state": state, "commit": "abc1234"}, headers=job).status_code == 200
    assert any(k == GUILD2 and "profile change from Demo Guild's news applied" in t for k, t in bot.lines), bot.lines
