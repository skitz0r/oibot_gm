"""The seed (armory link, spec §5.5): the registered characters set against the linked guild's roster — Sure / Likely /
No match, the guild's characters nobody claims — and the apply of the picks an officer confirms (armory_seed.py).
Every name is invented: the roster fixture's "Lantern Wardens" (Bram Kettle, Tamsin Pell, …) plus a few rows built
here; the members are Tessa, Orla, Wren, Pell and Quill."""
from __future__ import annotations

import asyncio
import dataclasses
import subprocess

import pytest
from conftest import OWNER
from test_armory_link import ROWS, FakeArmory, FakeBot

from oibot_gm import armory, armory_seed, discord_armory
from oibot_gm.armory import RosterRow
from oibot_gm.registry import ArmoryLink, RegistryError

TESSA, ORLA, WREN, PELL, QUILL = (470_000_000_000_000_101, 470_000_000_000_000_102, 470_000_000_000_000_103,
                                  470_000_000_000_000_104, 470_000_000_000_000_105)
THORN = RosterRow(id=700201, name="Thorn", surname="Wick", slug="thorn-wick", level=60, cls="Warrior")
KORRIN_TOO = RosterRow(id=700202, name="Korrin", surname="Vale", slug="korrin-vale", level=58, cls="Shaman")
MINE = {TESSA, ORLA, WREN, PELL, QUILL}


def run(coro):
    return asyncio.run(coro)


def roster(*extra) -> list[RosterRow]:
    return list(ROWS) + list(extra)


def mine(rows):
    return [r for r in rows if r.uid in MINE]


def by_label(rows):
    return {r.label: r for r in mine(rows)}


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def held(reg, uid, label, aid, slug):
    reg.link_armory(uid, label, ArmoryLink(id=aid, slug=slug, linked_by="Owner", linked_at="2026-11-12T20:00:00+00:00"), "Owner",
                    by_id=OWNER, officer=True)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeArmory()
    f.rows = roster(THORN)
    monkeypatch.setattr(armory, "client_for", lambda reg: f)
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    monkeypatch.setattr(discord_armory, "OUT", tmp_path / "armory-out")
    discord_armory.reset_state()
    yield f
    discord_armory.reset_state()


# ------------------------------------------------------------------ propose


def test_seed_sure_needs_uniqueness_on_both_sides(reg):
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)  # one Warrior Bram on each side
    reg.add_character(WREN, "Wren", "Tamsin", "Druid", "Balance", None, True)  # two registered Druid Tamsins
    reg.add_character(ORLA, "Orla", "Tamsin", "Druid", "Feral", None, True, surname="Ash")
    reg.add_character(PELL, "Pell", "Korrin", "Shaman", "Elemental", None, True)  # two Shaman Korrins on the roster
    reg.add_character(QUILL, "Quill", "Vell", "Warlock", "Affliction", None, True, surname="Moss")  # the surname differs
    seed = armory_seed.propose(reg, roster(KORRIN_TOO))
    sure = by_label(seed.sure)
    assert list(sure) == ["Bram"]
    assert (sure["Bram"].candidate.id, sure["Bram"].why, sure["Bram"].cls, sure["Bram"].member) == (700101, "same name", "Warrior", "Tessa")
    likely = mine(seed.likely)
    tamsins = [r for r in likely if r.label.startswith("Tamsin")]
    assert [(r.member, r.label, r.candidate.id, r.why) for r in tamsins] == [("Orla", "Tamsin Ash", 700103, "close name"),
                                                                             ("Wren", "Tamsin", 700103, "same name")]
    korrin = next(r for r in likely if r.label == "Korrin")
    assert [korrin.candidate.id, *[o.id for o in korrin.others]] == [700106, 700202]  # ties by name: Dask before Vale
    vell = next(r for r in likely if r.label == "Vell Moss")
    assert (vell.candidate.id, vell.why) == (700107, "close name")  # the first name alone is not the same name
    # a roster row is sure for at most one character, and a character for at most one row
    ids = [r.candidate.id for r in seed.sure]
    assert len(ids) == len(set(ids)) and len({(r.uid, r.label) for r in seed.sure}) == len(seed.sure)
    assert 700101 not in {r.id for r in seed.unclaimed}  # the sure match's row is spoken for
    assert {700102, 700105, 700108} <= {r.id for r in seed.unclaimed}


def test_a_held_row_is_never_a_candidate_and_never_unclaimed(reg):
    reg.add_character(TESSA, "Tessa", "Odo", "Hunter", "Survival", None, True)
    reg.add_character(ORLA, "Orla", "Odo", "Hunter", "Marksmanship", None, True, surname="Brack")
    held(reg, ORLA, "Odo Brack", 700108, "odo-brack")
    seed = armory_seed.propose(reg, roster())
    assert [(r.label, r.candidate) for r in mine(seed.none)] == [("Odo", None)]
    assert 700108 not in {r.id for r in seed.unclaimed}
    assert not mine(seed.sure) and not mine(seed.likely)


def test_a_joined_spelling_is_likely_for_the_split_name_never_sure(reg):
    reg.add_character(TESSA, "Tessa", "Thornwick", "Warrior", "Arms", None, True)
    reg.add_character(WREN, "Wren", "Ebba", "Mage", "Frost", None, True)  # the roster's is Ébba: an accent apart
    reg.add_character(ORLA, "Orla", "Xtamsin", "Druid", "Feral", None, True)  # a prefix on the roster's first name
    reg.add_character(PELL, "Pell", "Marow", "Warlock", "Affliction", None, True)  # a letter short of the surname
    seed = armory_seed.propose(reg, roster(THORN))
    assert not mine(seed.sure)
    likely = by_label(seed.likely)
    assert (likely["Thornwick"].candidate.id, likely["Thornwick"].why) == (700201, "close name")
    assert (likely["Ebba"].candidate.id, likely["Ebba"].why) == (700104, "same name")
    assert (likely["Xtamsin"].candidate.id, likely["Xtamsin"].why) == (700103, "close name")
    assert (likely["Marow"].candidate.id, likely["Marow"].why) == (700107, "close name")


def test_display_name_can_make_a_likely(reg):
    reg.add_character(TESSA, "Vell Marrow", "Shadowpact", "Warlock", "Affliction", None, True)
    seed = armory_seed.propose(reg, roster())
    row = by_label(seed.likely)["Shadowpact"]
    assert (row.candidate.id, row.why, row.member) == (700107, "display name", "Vell Marrow")


def test_a_class_mismatch_is_no_match(reg):
    reg.add_character(TESSA, "Tessa", "Pip", "Warrior", "Arms", None, True)  # the roster's Pip Wobble is a Rogue
    reg.add_character(WREN, "Wren", "Thornwick", "Rogue", "Combat", None, True)  # Thorn Wick is a Warrior
    seed = armory_seed.propose(reg, roster(THORN))
    assert [(r.label, r.candidate, r.why) for r in mine(seed.none)] == [("Pip", None, ""), ("Thornwick", None, "")]
    assert not mine(seed.sure) and not mine(seed.likely)


def test_at_most_three_candidates_best_first(reg):
    reg.add_character(TESSA, "Tessa", "Korrin", "Shaman", "Elemental", None, True)
    extra = [RosterRow(id=700300 + i, name="Korrin", surname=s, slug=f"korrin-{s.lower()}", level=60, cls="Shaman")
             for i, s in enumerate(["Abel", "Bose", "Cray"])]
    near = RosterRow(id=700310, name="Korrinth", surname="Zed", slug="korrinth-zed", level=60, cls="Shaman")
    row = by_label(armory_seed.propose(reg, roster(near, *extra)).likely)["Korrin"]
    assert [row.candidate.id, *[o.id for o in row.others]] == [700300, 700301, 700302]  # same name first, by name; then nothing else


def test_linked_characters_are_left_out(reg):
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True, surname="Kettle")
    held(reg, TESSA, "Bram Kettle", 700101, "bram-kettle")
    reg.add_character(WREN, "Wren", "Pip", "Rogue", "Combat", None, True)
    reg.retire(WREN, "Pip")  # a retired character is not proposed
    reg.add_character(ORLA, "Orla", "Tamsin", "Druid", "Feral", None, True)
    reg.member_left(ORLA)  # nor one of a member who left
    seed = armory_seed.propose(reg, roster())
    everyone = seed.sure + seed.likely + seed.none
    assert not mine(everyone)
    assert not any(r.uid in {m.discord_id for m in reg.members.values() if m.test} for r in everyone)  # puppets never
    assert 700101 not in {r.id for r in seed.unclaimed}


def test_propose_writes_nothing_and_its_rev_follows_the_content(reg, fake):
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)
    reg.add_character(WREN, "Wren", "Thornwick", "Warrior", "Arms", None, True)
    before = commits(reg)
    a = armory_seed.propose(reg, roster(THORN), taken_at="2026-11-12T20:00:00+00:00")
    b = armory_seed.propose(reg, roster(THORN), taken_at="2026-11-12T20:00:00+00:00")
    assert commits(reg) == before and fake.calls == []  # no write, no request to the armory
    assert a.rev == b.rev and len(a.rev) == 12
    assert armory_seed.propose(reg, roster(), taken_at="2026-11-12T20:00:00+00:00").rev != a.rev  # a row gone
    assert armory_seed.propose(reg, roster(THORN), taken_at="2026-11-12T23:00:00+00:00").rev != a.rev  # read again
    assert [r.member for r in mine(a.sure + a.likely)] == ["Tessa", "Wren"]


# ------------------------------------------------------------------ apply


def test_apply_links_each_on_its_own_and_reports_a_refusal(reg, fake):
    bot = FakeBot()
    reg.set_armory_guild(fake.guild_facts, "Owner", by_id=OWNER)
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)
    reg.add_character(ORLA, "Orla", "Brom", "Warrior", "Arms", None, True)
    reg.add_character(WREN, "Wren", "Pip", "Rogue", "Combat", None, True)
    run(discord_armory.roster_refresh(bot, reg, force=True))
    picks = [(TESSA, "Bram", 700101), (ORLA, "Brom", 700101), (WREN, "Pip", 700105), (ORLA, "Brom", 799999)]
    with pytest.raises(RegistryError) as e:  # an officer's call, decided live: a member can't apply the seed
        run(armory_seed.apply(bot, reg, picks, "Tessa", by_id=TESSA))
    assert str(e.value) == armory_seed.OFFICER_ONLY
    before = commits(reg)
    out = run(armory_seed.apply(bot, reg, picks, "Owner", by_id=OWNER))
    assert [(r["label"], r["ok"]) for r in out] == [("Bram", True), ("Brom", False), ("Pip", True), ("Brom", False)]
    assert out[0]["text"] == "Tessa's Bram is linked to Bram Kettle in the armory."
    assert out[1]["text"] == "Bram Kettle in the armory is linked to Tessa's Bram Kettle. Unlink it there first."
    assert out[2]["text"] == "Wren's Pip is linked to Pip Wobble in the armory."
    assert out[3]["text"] == armory_seed.ROW_GONE
    assert commits(reg) == before + 2  # one commit per link that went through
    tessa = reg.own_character(reg.members[TESSA].characters, "Bram Kettle")
    assert tessa.armory.id == 700101
    assert reg.own_character(reg.members[ORLA].characters, "Brom").armory is None
    assert reg.own_character(reg.members[WREN].characters, "Pip Wobble").armory.id == 700105
    assert bot.dms == []


def test_apply_refuses_a_row_the_armory_now_shows_as_another_character(reg, fake):
    bot = FakeBot()
    reg.set_armory_guild(fake.guild_facts, "Owner", by_id=OWNER)
    reg.add_character(TESSA, "Tessa", "Tamsin", "Druid", "Feral", None, True)
    run(discord_armory.roster_refresh(bot, reg, force=True))
    fake.chars["tamsin-pell"] = dataclasses.replace(fake.chars["tamsin-pell"], id=709999)
    out = run(armory_seed.apply(bot, reg, [(TESSA, "Tamsin", 700103)], "Owner", by_id=OWNER))
    assert out == [{"uid": TESSA, "label": "Tamsin", "id": 700103, "ok": False, "text": armory_seed.ANOTHER_CHARACTER}]
    assert reg.own_character(reg.members[TESSA].characters, "Tamsin").armory is None
