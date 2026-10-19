"""Roster redesign Phase 0, the readers' follow-ups (verifier findings on 92e844c):

- the plain-text character-delete diff read RegisteredCharacter.rosters, which no longer holds run keys, so a seated
  character's delete never mentioned its seat; it reads rc.rostered_runs (the run files) like the site does;
- every reader of a held run names it as a person reads it (raid name + 12-hour start, or Discord's <t:…> stamp),
  never the run key — its tail is the 24-hour start ('bd-1209-1930', the clock rule): /me view and the Status button,
  help's member record, the site's member JSON (the delete confirmations), the plain-text delete diff;
- the analytics card (pool_layout) passes the bot's RaidStore to comp.readiness, so "on roster" is the same number
  GET /api/readiness, MCP and /ask read;
- the load-time legacy 'teams' migration copied bare team keys ("main") into main.rosters — the phantom D11 drops —
  it copies regular-series keys (with ':') only.
Registry and RaidStore are real (scratch copy of the fixtures, push off)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import GUILD, join, lock_with_board, open_test_run
from fastapi.testclient import TestClient
from test_mcp import TOKEN, bearer
from test_parity_verbs import Bot

from oibot_gm import comp as comp_mod
from oibot_gm import configops, help as help_mod
from oibot_gm import discord_pool as dp
from oibot_gm import discord_registry as dreg
from oibot_gm import raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import Registry
from oibot_gm.web import app as web

ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731


def _seated_run(reg, rs):
    """A locked run with the first five puppets seated; returns (run, a seated member)."""
    ev = open_test_run(reg, rs)
    seated = reg.test_members()[:5]
    join(reg, rs, ev, seated)
    lock_with_board(reg, rs, ev, [m.display_name for m in seated])
    m = seated[0]
    assert rc.rostered_runs(reg, rs, m), "the board seats the member"
    return ev, m


def _person_words(reg, ev) -> str:
    return f"{reg.raid_def(ev.instance)['name']} {reg.local12(ev.start)}"


def _held_char(reg, rs, m) -> str:
    return rc.rostered_runs(reg, rs, m)[0][1]


# ---------------------------------------------------------------- the plain-text delete diff

def test_plain_text_delete_names_the_seat_from_the_run_file(reg, rs):
    """Without the fix the diff said nothing about the seat (cur.rosters is empty now that the lock writes no run key)."""
    ev, m = _seated_run(reg, rs)
    char = _held_char(reg, rs, m)
    assert ev.team not in (reg.members[m.discord_id].main.rosters or [])  # nothing on the member file
    line = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=char, field="delete"))
    assert "seat is handed back" in line and _person_words(reg, ev) in line, line  # phase 2 C: the delete frees it (D24)
    assert ev.key not in line and ev.team not in line, line  # never the run key (24-hour tail)
    assert "placement" not in line


def test_plain_text_delete_of_an_unseated_character_mentions_no_seat(reg, rs):
    _seated_run(reg, rs)
    free = reg.test_members()[8]
    line = configops.describe(reg, ConfigOp(op="character", member=free.display_name, character=free.main.label, field="delete"))
    assert "seat" not in line and "DELETE" in line


def test_delete_does_not_free_the_seat(reg, rs):
    """What the diff (and the manual) promise: the delete touches the member file only; the seat stays in the run file."""
    ev, m = _seated_run(reg, rs)
    char = _held_char(reg, rs, m)
    reg.delete_character(m.discord_id, char)
    fresh = rc.RaidStore(reg.store, reg.key)
    assert any(e.key == ev.key for e, _ in rc.rostered_runs(reg, fresh, reg.members[m.discord_id]))


# ---------------------------------------------------------------- clock rule: no run key where a person reads

def test_discord_rostered_text_is_raid_name_and_native_stamp(reg, rs):
    ev, m = _seated_run(reg, rs)
    held = rc.rostered_runs(reg, rs, m)
    text = dreg.rostered_text(reg, held)
    assert reg.raid_def(ev.instance)["name"] in text and f"<t:{int(ev.start.timestamp())}:" in text, text
    assert ev.key not in text
    with_char = dreg.rostered_text(reg, held, with_char=True)
    assert f"({held[0][1]})" in with_char


def test_me_view_and_status_button_use_rostered_text():
    """Both Discord readers go through rostered_text (the old lines joined ev.team, the run key)."""
    src = Path(dreg.__file__).read_text()
    assert "ev.team for ev, _ in held" not in src and "{ev.team} ({char})" not in src
    assert src.count("rostered_text(reg, held") == 2


def test_help_member_record_names_runs_in_12_hour_words(reg, rs):
    ev, m = _seated_run(reg, rs)
    head = help_mod.member_lines(reg, rs, m, "You", True)[0]
    assert "rostered on " + _person_words(reg, ev) in head, head
    assert ev.key not in head and ev.team not in head


@pytest.fixture
def client(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(Bot(reg, rs)))


def test_member_json_rosters_are_person_words_not_run_keys(client, reg, rs):
    """char_json.rosters feeds the Me/Members delete confirmations ('It comes off the … roster')."""
    ev, m = _seated_run(reg, rs)
    char = _held_char(reg, rs, m)
    d = client.get("/api/member", params={"name": m.display_name}, headers=bearer()).json()
    row = next(c for c in d["characters"] if c["label"] == char)
    assert row["rosters"] == [_person_words(reg, ev)], row["rosters"]
    rows = client.get("/api/members", headers=bearer()).json()["rows"]
    mine = next(r for r in rows if r["uid"] == str(m.discord_id))
    assert next(c for c in mine["characters"] if c["label"] == char)["rosters"] == [_person_words(reg, ev)]


# ---------------------------------------------------------------- the analytics card reads the run files

def test_pool_layout_passes_the_raid_store_to_readiness(reg, rs, monkeypatch):
    ev, m = _seated_run(reg, rs)
    seen = {}
    real = comp_mod.readiness

    def spy(reg_, rid, rs_=None):
        d = real(reg_, rid, rs_)
        seen["on_roster"] = d["headcount"]["on_roster"]
        return d

    monkeypatch.setattr(dp.comp_mod, "readiness", spy)
    dp.pool_layout(reg, reg.raid_shell(ev.instance), ico, rs)
    assert seen["on_roster"] == real(reg, ev.instance, rs)["headcount"]["on_roster"] >= 5


def test_analytics_refresh_hands_the_bots_raid_store_to_the_card():
    src = Path(dp.__file__).read_text()
    assert "asyncio.to_thread(pool_layout, reg, roster, self.ico, raids.store(reg)" in src


# ---------------------------------------------------------------- the legacy 'teams' load migration

def test_legacy_teams_migration_copies_series_keys_only(reg, store, profile):
    """A member file still carrying the pre-roster 'teams' list: a bare team key ('main') used to be copied onto the
    main — the phantom D11 drops; a series key is kept, and the 'teams' key is gone after the load rewrite."""
    m = reg.test_members()[0]
    p = store.root / GUILD / "members" / f"{m.discord_id}.json"
    raw = json.loads(p.read_text())
    raw["teams"] = ["main", "barrow_deeps:default"]
    p.write_text(json.dumps(raw))
    again = Registry(store, GUILD, profile)
    assert again.members[m.discord_id].main.rosters == ["barrow_deeps:default"]
    assert "teams" not in json.loads(p.read_text())
    assert again.migrate_rosters_to_series(dry_run=True) == []  # nothing phantom left behind
