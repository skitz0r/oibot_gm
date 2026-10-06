"""Roster redesign D11 (Phase 0): the phantom-membership cleanup, on scratch data (a copy of the fixtures, push off).
Registry.migrate_rosters_to_series drops every RegisteredCharacter.rosters entry without a ':' (the leftover "main",
run keys), keeps regular-series keys, touches retired characters too, is one commit, and a second run is a no-op."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from conftest import GUILD

from oibot_gm.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


def _phantoms(reg):
    """The live shape: one member with rosters ['main'] on the main, one with ['main'] on a RETIRED character, one
    with a stale run key and a (future) series key side by side."""
    a, b, c = reg.test_members()[:3]
    a.main.rosters = ["main"]
    reg.save(a, "seed phantom main")
    reg.add_character(b.discord_id, b.display_name, "Boomalt", "Druid", "Balance", None, False)
    b = reg.members[b.discord_id]
    old = next(ch for ch in b.characters if ch.label == "Boomalt")
    old.rosters, old.status = ["main"], "retired"
    reg.save(b, "seed retired phantom")
    c.main.rosters = ["bd-0917-1937", "barrow_deeps:default"]
    reg.save(c, "seed run key + series")
    return a, b, c


def test_migration_drops_phantoms_keeps_series_and_is_idempotent(reg, store):
    a, b, c = _phantoms(reg)
    n = commits(store)
    preview = reg.migrate_rosters_to_series(dry_run=True)
    assert len(preview) == 3 and commits(store) == n, "a dry run changes nothing"
    assert reg.members[a.discord_id].main.rosters == ["main"]
    lines = reg.migrate_rosters_to_series(by="t")
    assert lines == preview
    assert commits(store) == n + 1, "one commit for the lot"
    head = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=store.root, capture_output=True, text=True).stdout
    assert "migrate: rosters = regular series only" in head
    fresh = Registry(store, GUILD, reg.profile)  # what is on disk
    assert fresh.members[a.discord_id].main.rosters == []
    assert all(ch.rosters == [] for ch in fresh.members[b.discord_id].characters), "retired characters too"
    assert fresh.members[c.discord_id].main.rosters == ["barrow_deeps:default"]
    assert fresh.migrate_rosters_to_series(by="t") == [] and commits(store) == n + 1, "idempotent: nothing left, no commit"


def test_migration_script_on_a_scratch_copy(reg, store, data_root):
    _phantoms(reg)
    spec = importlib.util.spec_from_file_location("migrate_rosters", ROOT / "scripts" / "migrate_rosters.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    n = commits(store)
    assert mod.main(["--data", str(data_root), "--dry-run"]) == 0 and commits(store) == n
    res = mod.migrate(data_root)
    assert len(res[GUILD]) == 3 and commits(store) == n + 1
    assert mod.migrate(data_root) == {GUILD: []} and commits(store) == n + 1
