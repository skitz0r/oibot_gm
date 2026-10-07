"""Shared fixtures: a Registry + RaidStore on a throw-away copy of the public demo fixtures (a git repo of its own,
push off), the `forever` profile and a seeded test bench. Nothing here touches the real data repo.

Solver-backed tests carry `@pytest.mark.slow`; `scripts/check_commands.py` runs `pytest -m "not slow"`.
Web tests call as if from this machine: TestClient's default client is the made-up host "testclient", and the MCP
owner token is local-only by default (OIBOT_MCP_LOCAL_ONLY), so the default client is loopback here; the tunnel tests
pass their own client and headers."""
from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

import starlette.testclient as _testclient

from oibot_gm import raidcycle as rc
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import Registry

_tc_init = _testclient.TestClient.__init__


def _loopback_init(self, app, *args, client=("127.0.0.1", 50000), **kwargs):
    _tc_init(self, app, *args, client=client, **kwargs)


_testclient.TestClient.__init__ = _loopback_init  # fastapi.testclient.TestClient is the same class
from oibot_gm.store import GitStore

ROOT = Path(__file__).resolve().parents[1]
GUILD = "demo"  # the fixture guild's key: data lives under <data>/demo (the demo fixtures + a guild.yaml written here)
OWNER = 100_000_000_000_000_001
OFFICER_ROLE = 200_000_000_000_000_001  # the fixture guild's officer role id (officer roles are stored by id, not name)
TEST_MEMBERS = 16  # 2 tanks, 5 healers, 9 dps from Registry.TEST_MIX


@pytest.fixture(autouse=True)
def _no_pending_policy_edits():
    """policy.PENDING is process-wide (one pending edit per guild and doc): every test starts and ends without one."""
    from oibot_gm import policy

    policy.PENDING.clear()
    yield
    policy.PENDING.clear()


@pytest.fixture(autouse=True)
def _no_real_agents_out_or_mcp_default(tmp_path, monkeypatch):
    """The agents' status files and transcripts (agents.OUT, read at call time) live in this test's tmp dir, never the
    real out/agents the Agents page reads; and an OIBOT_MCP_GUILD from the real .env (mcp_server loads it) never
    picks a guild for a test."""
    from oibot_gm import agents

    monkeypatch.setattr(agents, "OUT", tmp_path / "agents-out")
    monkeypatch.delenv("OIBOT_MCP_GUILD", raising=False)


@pytest.fixture(scope="session")
def profile() -> GameProfile:
    return GameProfile.load(ROOT / "profiles" / "forever")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """<tmp>/data/demo = fixtures/demo + a minimal guild.yaml, committed once so every change is a commit on top."""
    root = tmp_path / "data"
    shutil.copytree(ROOT / "fixtures" / "demo", root / GUILD)
    (root / GUILD / "guild.yaml").write_text(yaml.safe_dump({
        "key": GUILD, "name": "Demo Guild", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": OWNER,
        "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE],
    }, sort_keys=False))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"], cwd=root, check=True)
    return root


@pytest.fixture
def store(data_root: Path) -> GitStore:
    return GitStore(data_root, push=False)


@pytest.fixture
def reg(store: GitStore, profile: GameProfile) -> Registry:
    r = Registry(store, GUILD, profile)
    r.seed_test_members(TEST_MEMBERS, "t")
    return r


@pytest.fixture
def rs(store: GitStore) -> rc.RaidStore:
    return rc.RaidStore(store, GUILD)


# ---- helpers (imported by the test modules)

def role_of(reg: Registry, m) -> str:
    return reg.profile.spec(m.main.cls, m.main.spec).role


def by_role(reg: Registry, members=None) -> dict[str, list]:
    pool = members if members is not None else reg.test_members()
    return {r: [m for m in pool if role_of(reg, m) == r] for r in ("tank", "healer", "melee", "ranged")}


def open_test_run(reg: Registry, rs: rc.RaidStore, instance: str = "barrow_deeps", days: float = 3, **cutoffs) -> rc.RaidEvent:
    """A run a few days out, opened with test cutoffs (so its pool is the test bench only)."""
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, instance, start, by="t", cutoffs={"soft": 2, "hard": 1.5, "confirm": 1, **cutoffs})


def first_slot(reg: Registry, instance: str, horizon_days: float = 21, min_lead_hours: float = 24):
    """The soonest scheduled start at least `min_lead_hours` away: a test that locks, confirms and fills a run must
    not depend on the hour it runs at (the next Tuesday 19:30 is half an hour away on a Tuesday evening, its confirm
    and fill deadlines already past)."""
    now = reg.now_local()
    return next(t for _, t in rc.upcoming(reg, instance, now, 24 * horizon_days) if t - now >= timedelta(hours=min_lead_hours))


def join(reg: Registry, rs: rc.RaidStore, ev: rc.RaidEvent, members, status: str = "in") -> None:
    for m in members:
        rc.set_signup(reg, rs, ev, m, None, status, source="test")


def lock_with_board(reg: Registry, rs: rc.RaidStore, ev: rc.RaidEvent, names: list[str], hours_to_confirm: float = 1) -> rc.RaidEvent:
    """Lock the sheet around a hand-picked roster (no solver): groups of `group_size` in the given order."""
    size = rc.run_size(reg, ev)
    g = int(reg.profile.comp_rules["group_size"])
    layout = [names[i:i + g] for i in range(0, len(names), g)]
    ev.rosters = rc.board_rosters(reg, ev, layout, size)
    ev.state, ev.locked_at = "locked", rc.now()
    ev.confirm_by = (ev.start - timedelta(hours=hours_to_confirm)).isoformat()
    rs.save(ev, "locked (test board)")
    return ev


# ---- two guilds in one process (design §5.32): `demo` (Discord server 1, the fixtures above) and `duo` (server 2, its
# own owner, officer role and members). Every isolation test is built on a run key that exists in BOTH guilds.
GUILD2 = "duo"
DUO_SERVER = 2
OWNER2 = 100_000_000_000_000_002
OFFICER_ROLE2 = 200_000_000_000_000_002
DUO_MEMBERS = 12


def add_duo(data_root: Path) -> None:
    """<data>/duo/guild.yaml committed on top of the demo guild (no fixture files: a fresh guild)."""
    (data_root / GUILD2).mkdir()
    (data_root / GUILD2 / "guild.yaml").write_text(yaml.safe_dump({
        "key": GUILD2, "name": "Duo Guild", "game_profile": "forever", "discord_guild_id": DUO_SERVER, "owner_discord_id": OWNER2,
        "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE2],
    }, sort_keys=False))
    subprocess.run(["git", "add", "-A"], cwd=data_root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "duo"], cwd=data_root, check=True)


def two_guilds(data_root: Path, store: GitStore):
    """(guilds, reg_demo, reg_duo) through the real loader (discord_registry.Guilds). The duo guild gets its own test
    members — the same puppet names as demo's, so a name shared by two guilds is part of every isolation test."""
    from oibot_gm.discord_registry import Guilds

    if not (data_root / GUILD2).exists():
        add_duo(data_root)
    guilds = Guilds(store, ROOT)
    a, b = guilds.by_key[GUILD], guilds.by_key[GUILD2]
    if not b.test_members():
        b.seed_test_members(DUO_MEMBERS, "t")
    return guilds, a, b


@pytest.fixture
def duo(data_root: Path, store: GitStore, reg: Registry):
    """Two configured guilds: SimpleNamespace(guilds, a=demo registry, b=duo registry, rs_a, rs_b). `reg` is requested
    first so demo's test bench is seeded on disk before the loader reads it."""
    from types import SimpleNamespace

    guilds, a, b = two_guilds(data_root, store)
    return SimpleNamespace(guilds=guilds, a=a, b=b, rs_a=rc.RaidStore(store, GUILD), rs_b=rc.RaidStore(store, GUILD2))


def same_run(reg_a: Registry, rs_a: rc.RaidStore, reg_b: Registry, rs_b: rc.RaidStore, instance: str = "barrow_deeps",
             days: float = 3, schedule: str | None = None) -> tuple[rc.RaidEvent, rc.RaidEvent]:
    """The same instance opened at the same local start in both guilds (same timezone), so the run keys COLLIDE.
    `schedule`: both guilds hold a weekly schedule with that id (the collision most likely in practice, K20)."""
    start = (reg_a.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    if schedule:
        slot = start.strftime("%a %H:%M")
        for r in (reg_a, reg_b):
            r.set_schedule(instance, schedule, {"kind": "weekly", "slots": slot, "name": "Fridays"}, "t")
    cut = {"soft": 2, "hard": 1.5, "confirm": 1}
    ev_a = rc.open_run(reg_a, rs_a, instance, start, by="t", cutoffs=cut, schedule=schedule)
    ev_b = rc.open_run(reg_b, rs_b, instance, start, by="t", cutoffs=cut, schedule=schedule)
    assert ev_a.key == ev_b.key and ev_a.team == ev_b.team, "the fixture's premise: one run key in both guilds"
    return ev_a, ev_b


class MemberDirectory:
    """A `cached_member` double for routing tests: {(discord server id, uid): member}; `flaky` = (server, uid) pairs
    whose lookup fails transiently (unknown), everything else absent is Discord's own 404 (recorded in _member_gone)."""

    def __init__(self, members: dict | None = None, flaky: set | None = None):
        self.members, self.flaky = dict(members or {}), set(flaky or ())

    def install(self, bot, servers=(1, 2)) -> None:
        guilds = {sid: __import__("types").SimpleNamespace(id=sid, name=f"Server {sid}", owner_id=None) for sid in servers}
        bot.get_guild = lambda gid: guilds.get(gid)
        bot.__dict__.setdefault("_member_gone", set())
        directory = self

        async def cached_member(guild, uid, max_age=300):
            key = (guild.id, uid)
            if key in directory.flaky:
                bot._member_gone.discard(key)
                return None
            m = directory.members.get(key)
            if m is None:
                bot._member_gone.add(key)
            return m
        bot.cached_member = cached_member
