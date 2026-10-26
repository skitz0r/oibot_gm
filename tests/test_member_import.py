"""Design §5.53: a roster from a signup list. Registry.import_members registers people by their Discord ids with the
character as their main, skips (and names) what it can't apply, and previews without writing; the Raid-Helper script
turns an export into the test bench's rows or the import's rows; the MCP tool posts the route and sends the actor only
when a rank is in the rows."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from oibot_gm import mcp_server as ms
from oibot_gm.registry import RegistryError

ROOT = Path(__file__).resolve().parents[1]
IMP = 310_000_000_000_000_000  # import ids: real snowflakes in range, apart from the other tests' members
TOKEN = "a" * 64


def row(n: int, name: str, character: str, cls: str, spec: str, **extra) -> dict:
    return {"discord_id": str(IMP + n), "name": name, "character": character, "cls": cls, "spec": spec, **extra}


def test_import_registers_new_people_and_skips_the_rest_with_a_reason(reg):
    seen = reg.member(IMP + 2, "Bramble", create=True)  # a record with no characters (a sighting, an absence): gets the character, keeps its name
    reg.save(seen, "seen")
    reg.add_character(IMP + 3, "Corvid", "Corvidane", "Mage", "Frost", None, True)  # already registered: never changed
    puppet = (reg.test_members() or reg.seed_test_members(1, "tester"))[0]
    rows = [
        row(1, "Ash", "Ashgrove", "Priest", "Holy", rank="raider"),
        row(2, "Bramble Nick", "Bramblewick", "Warrior", "Protection"),
        row(3, "Corvid", "Corvidtwo", "Mage", "Fire"),
        row(4, "Dun", "corvidane", "Rogue", "Combat"),  # a name someone else holds (case apart)
        {"discord_id": str(puppet.discord_id), "name": "Pup", "character": "Puppetone", "cls": "Rogue", "spec": "Combat"},
        row(1, "Ash again", "Ashtwo", "Priest", "Shadow"),
        {"discord_id": "", "name": "Nobody", "character": "Nobodyx", "cls": "Priest", "spec": "Shadow"},
        row(5, "Eld", "Eldric", "Bard", "Song"),
        row(6, "Fen", "Fenwild", "Druid", "Balance", rank="alt"),
        row(7, "Gil", "Gilroy", "Druid", "Fishing"),
        row(8, "Hal", "Ashgrove", "Hunter", "Survival"),  # the same new name twice in one list
        row(9, "Ivy", "Iv", "Hunter", "Survival", offspec="Marksmanship"),
    ]
    before = dict(reg.members)
    pre = reg.import_members(rows, "Jon", preview=True)
    assert pre["preview"] and pre["added"] == ["Ash: Ashgrove (Priest Holy, raider)", "Bramble: Bramblewick (Warrior Protection, trial)",
                                                "Ivy: Iv (Hunter Survival/Marksmanship, trial)"]
    assert reg.members == before and IMP + 1 not in reg.members, "a preview writes nothing"

    out = reg.import_members(rows, "Jon", by_id=1)
    assert out["added"] == pre["added"] and not out["preview"]
    assert [(s["name"], s["why"]) for s in out["skipped"]] == [
        ("Corvid", "already registered as Corvidane"),
        ("Dun", "Corvidane is already registered to Corvid"),
        ("Pup", "a test id"),
        ("Ash again", "a second row for the same person"),
        ("Nobody", "no Discord id"),
        ("Eld", "Bard isn't a class in this game version"),
        ("Fen", "a first character is a main, never an alt"),
        ("Gil", "Fishing is not a Druid spec. Valid: Balance, Feral, Guardian, Restoration."),
        ("Hal", "Ashgrove is already in this list"),
    ]
    a = reg.members[IMP + 1]
    assert a.display_name == "Ash" and a.main.name == "Ashgrove" and a.rank == "raider" and not a.test and not a.main.confirmed_by
    b = reg.members[IMP + 2]
    assert b.display_name == "Bramble" and b.main.name == "Bramblewick" and b.rank == "trial", "a record on file keeps its name; no rank → trial"
    assert reg.members[IMP + 3].main.name == "Corvidane" and len(reg.members[IMP + 3].characters) == 1
    assert reg.members[IMP + 9].main.offspec == "Marksmanship"
    assert IMP + 4 not in reg.members and IMP + 5 not in reg.members

    again = reg.import_members(rows[:2], "Jon")
    assert again["added"] == [] and [s["why"] for s in again["skipped"]] == ["already registered as Ashgrove", "already registered as Bramblewick"], "idempotent"


def test_import_refuses_an_empty_or_oversized_list(reg):
    with pytest.raises(RegistryError, match="empty"):
        reg.import_members([], "Jon")
    with pytest.raises(RegistryError, match="at most"):
        reg.import_members([row(i, f"P{i}", f"Person{i}", "Mage", "Fire") for i in range(reg.IMPORT_MAX + 1)], "Jon", preview=True)
    with pytest.raises(RegistryError, match="objects"):
        reg.import_members(["nope"], "Jon")


def load_script():
    spec = importlib.util.spec_from_file_location("raidhelper_roster", ROOT / "scripts" / "raidhelper_roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_turns_an_export_into_test_rows_or_import_rows(profile):
    rh = load_script()
    signups = [{"userId": "1001", "name": "Nick(Quillbrook)", "className": "Tank", "specName": "Protection1"},
               {"userId": "1002", "name": "Wren/Alt", "className": "Healer", "specName": "Restoration1"},
               {"userId": "1003", "name": "Gone", "className": "Absence", "specName": None},
               {"name": "Noid", "className": "Ranged", "specName": "Frost"}]
    rows, skipped = rh.rows_from(signups, profile)
    assert [(r["name"], r["character"], r["cls"], r["spec"], r["rank"]) for r in rows] == [
        ("Nick(Quillbrook)", "Quillbrook", "Paladin", "Protection", "raider"), ("Wren/Alt", "Wren", "Shaman", "Restoration", "raider"), ("Noid", "Noid", "Mage", "Frost", "raider")]
    assert skipped == ["Gone (Absence: no spec)"]
    mrows, mskipped = rh.member_rows(signups, profile)
    assert mrows == [{"discord_id": "1001", "name": "Nick", "character": "Quillbrook", "cls": "Paladin", "spec": "Protection"},
                     {"discord_id": "1002", "name": "Wren", "character": "Wren", "cls": "Shaman", "spec": "Restoration"}]
    assert mskipped == ["Gone (Absence: no spec)", "Noid (no Discord id in the export)"], "no rank unless asked: a rank is a person's decision"
    assert rh.member_rows(signups[:1], profile, "raider")[0][0]["rank"] == "raider"


def test_import_members_tool_posts_the_route_and_sends_the_actor_only_for_a_rank(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content), request.headers.get(ms.ACTOR_HEADER)))
        return httpx.Response(200, json={"message": "2 member(s) imported from a signup list", "added": ["a", "b"], "skipped": [], "preview": False})

    monkeypatch.setenv(ms.ACTOR_ENV, "123")
    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        plain = [{"discord_id": "1", "name": "A", "character": "Ab", "cls": "Mage", "spec": "Fire"}]
        ranked = [{**plain[0], "rank": "raider"}]
        assert "imported" in ms.import_members(plain, preview=True)
        ms.import_members(plain)
        ms.import_members(ranked, preview=True)
        ms.import_members(ranked)
    finally:
        ms.use(None)
    assert [p for p, _, _ in seen] == ["/api/members/import"] * 4
    assert [b["preview"] for _, b, _ in seen] == [True, False, True, False] and seen[1][1]["rows"] == plain
    assert [a for _, _, a in seen] == [None, None, None, "123"], "D8: the actor travels only with a rank that is written"
