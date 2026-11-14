"""The watch list on the member's record (price watch, spec §4.2, C7, C9, C13, C14): `Member.watching` holds item ids,
newest first, at most 25. Registered members only — a watch never creates a record, and a test puppet is refused like
nobody. Written quietly: one commit "<display name>: watch list changed" that never names the item, no change-log line.
A version without auction houses has no watch list. No network; invented names."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from oibot_gm.profiles import GameProfile
from oibot_gm.registry import WATCH_FULL, WATCH_MAX, WATCH_REGISTER_FIRST, Member, RegistryError

ROOT = Path(__file__).resolve().parents[1]
UID = 300_000_000_000_000_101
STRANGER = 300_000_000_000_000_999


@pytest.fixture
def member(reg):
    m = reg.member(UID, "Thornwick", create=True)
    reg.save(m, "Thornwick registered")
    return m


@pytest.fixture
def tbc(reg, monkeypatch):
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    assert p.auctions is None
    return reg


def last_commit(reg) -> str:
    return subprocess.run(["git", "log", "-1", "--format=%s"], cwd=reg.store.root, capture_output=True, text=True, check=True).stdout.strip()


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def try_watch(reg, uid, item) -> str:
    try:
        reg.watch(uid, item)
        return "ok"
    except RegistryError as e:
        return "full" if str(e) == WATCH_FULL else str(e)


def run_in_threads(*fns) -> list:
    out: list = [None] * len(fns)
    start = threading.Barrier(len(fns))

    def go(i, fn):
        start.wait()
        out[i] = fn()

    threads = [threading.Thread(target=go, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_the_words():
    assert WATCH_MAX == 25
    assert WATCH_FULL == "Your watch list holds 25 items. Stop watching one first."
    assert WATCH_REGISTER_FIRST == "Register first with /register to keep a watch list."


def test_watch_adds_newest_first_and_is_harmless_to_repeat(reg, member):
    assert reg.watch(UID, 13466) is True and reg.watch(UID, 8925) is True
    assert reg.watching(UID) == [8925, 13466]
    before = commits(reg)
    assert reg.watch(UID, 13466) is False and reg.watching(UID) == [8925, 13466]
    assert commits(reg) == before, "already watched: no write"


def test_it_is_stored_on_the_member_file(reg, member):
    reg.watch(UID, 13466)
    reg.watch(UID, 8925)
    reread = type(reg)(reg.store, reg.key, reg.base_profile)
    assert reread.watching(UID) == [8925, 13466]


def test_unwatch(reg, member):
    reg.watch(UID, 13466)
    before = commits(reg)
    assert reg.unwatch(UID, 13466) is True and commits(reg) == before + 1
    assert reg.unwatch(UID, 13466) is False and reg.watching(UID) == []
    assert commits(reg) == before + 1, "not there: no write"


def test_a_watch_never_creates_a_record(reg):
    before = commits(reg)
    with pytest.raises(RegistryError, match="Register first with /register to keep a watch list."):
        reg.watch(STRANGER, 13466)
    assert STRANGER not in reg.members and reg.watching(STRANGER) == [] and reg.unwatch(STRANGER, 1) is False
    assert not (reg.store.root / reg.key / "members" / f"{STRANGER}.json").exists()
    assert commits(reg) == before


def test_a_test_puppet_is_refused_like_nobody(reg):
    puppet = next(m for m in reg.members.values() if m.test)
    with pytest.raises(RegistryError, match="Register first with /register to keep a watch list."):
        reg.watch(puppet.discord_id, 13466)
    assert reg.watching(puppet.discord_id) == [] and reg.unwatch(puppet.discord_id, 13466) is False


def test_the_26th_is_refused(reg, member):
    for i in range(25):
        reg.watch(UID, 1000 + i)
    with pytest.raises(RegistryError, match="Your watch list holds 25 items. Stop watching one first."):
        reg.watch(UID, 2000)
    assert len(reg.watching(UID)) == 25
    assert reg.watch(UID, 1003) is False, "one already on a full list is not refused"


def test_two_presses_at_24_keep_at_most_25(reg, member):
    for i in range(24):
        reg.watch(UID, 1000 + i)
    results = run_in_threads(lambda: try_watch(reg, UID, 3001), lambda: try_watch(reg, UID, 3002))
    assert len(reg.watching(UID)) == 25 and sorted(results) == ["full", "ok"]


def test_the_write_is_quiet(reg, member, monkeypatch):
    heard = []
    monkeypatch.setattr(reg, "listeners", [lambda *a, **k: heard.append(a)])  # the change-log listeners
    reg.watch(UID, 13466)
    assert heard == []
    assert last_commit(reg) == f"{reg.key}: {reg.member(UID).display_name}: watch list changed"
    assert "13466" not in last_commit(reg)
    reg.unwatch(UID, 13466)
    assert heard == []
    assert last_commit(reg) == f"{reg.key}: Thornwick: watch list changed"


def test_tbc_has_no_watch_list(tbc, member):
    with pytest.raises(RegistryError, match="This game version has no auction houses."):
        tbc.watch(UID, 1)
    assert tbc.watching(UID) == []


def test_an_old_member_file_loads_with_an_empty_list(reg, member):
    path = reg.store.root / reg.key / "members" / f"{UID}.json"
    raw = json.loads(path.read_text())
    raw.pop("watching", None)
    path.write_text(json.dumps(raw))
    reread = type(reg)(reg.store, reg.key, reg.base_profile)
    assert reread.members[UID].watching == [] and reread.watching(UID) == []
    assert Member(discord_id=1, display_name="Fen").watching == []
