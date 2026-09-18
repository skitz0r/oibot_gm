"""Shared fixtures: a Registry + RaidStore on a throw-away copy of the public demo fixtures (a git repo of its own,
push off), the `forever` profile and a seeded test bench. Nothing here touches the real data repo.

Solver-backed tests carry `@pytest.mark.slow`; `scripts/check_commands.py` runs `pytest -m "not slow"`."""
from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from oibot_gm import raidcycle as rc
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import Registry
from oibot_gm.store import GitStore

ROOT = Path(__file__).resolve().parents[1]
GUILD = "demo"  # the fixture guild's key: data lives under <data>/demo (the demo fixtures + a guild.yaml written here)
OWNER = 100_000_000_000_000_001
TEST_MEMBERS = 16  # 2 tanks, 5 healers, 9 dps from Registry.TEST_MIX


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
        "timezone": "America/Los_Angeles", "officer_roles": ["officer"],
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
