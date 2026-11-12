"""Renaming a character (armory link, design §5.2 "The name.", designer's call C9): the old name becomes an alias,
history is never rewritten. `Registry.rename_character` is the one verb; `Registry.same_character` the one predicate
every by-name lookup goes through — the registry's find, the loot context, the import dedupe, the companion's routing,
the received marks, My loot and appeals — so an award recorded under the old name still belongs to the character.
Run files of live runs (open or locked) take the new name in the same commit; finished and cancelled runs keep theirs.

Every name here is invented (R8)."""
from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import join, lock_with_board
from test_loot_web import env  # noqa: F401 — a fixture

from oibot_gm import appeals, loot_ops
from oibot_gm import raidcycle as rc
from oibot_gm.loot import scoring
from oibot_gm.loot import session as ls
from oibot_gm.lootctx import RegistryLootContext
from oibot_gm.models import LootAward, Player
from oibot_gm.registry import ArmoryLink, RegisteredCharacter, RegistryError

TESSA = 700_000_000_000_000_001
ORLA = 700_000_000_000_000_002


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def tessa(reg):
    """Tessa with Bram Kettle (her main, a Warrior); returns (member, character)."""
    return reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Arms", None, True, surname="Kettle")


def award_row(reg, raider, *, uid=None, item="Robe of Tests", received="2026-12-09", iid="r1-d1", weight=0.75):
    reg.store.append_jsonl(Path(reg.key) / "ledger.jsonl", {"raider": raider, "item_id": 0, "item_name": item, "tier": "S", "total_weight": weight,
                                                            "offspec": False, "received": received, "instance": "x", "boss": "Gatekeeper",
                                                            "import_id": iid, "source": "bot", "discord_id": uid})
    reg.store.commit("seed one award")


def live_run(reg, rs, days, *, instance="barrow_deeps"):
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, instance, start, by="t")


# ---------------------------------------------------------------- the model

def test_new_fields_default_and_an_old_member_file_loads_unchanged(reg):
    m, c = tessa(reg)
    assert c.armory is None and c.profession_sync is True and c.former_names == []
    old = json.loads((reg.store.root / reg.key / "members" / f"{TESSA}.json").read_text())
    for ch in old["characters"]:  # a file written before the fields existed
        for k in ("armory", "profession_sync", "former_names"):
            ch.pop(k, None)
    path = reg.store.root / reg.key / "members" / f"{TESSA}.json"
    path.write_text(json.dumps(old, indent=1))
    subprocess.run(["git", "add", "-A"], cwd=reg.store.root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "old file"], cwd=reg.store.root, check=True)
    before = path.read_text()
    reg.reload()
    c = reg.members[TESSA].characters[0]
    assert c.armory is None and c.profession_sync and c.former_names == []
    assert path.read_text() == before, "a member file is not rewritten because fields were added"
    link = ArmoryLink(id=42, slug="bram-kettle", linked_by="Tessa", linked_at="2026-11-12T10:00:00+00:00")
    assert link.checked_at is None and link.level is None and link.in_guild is None and link.missing_since is None
    assert RegisteredCharacter(name="Bram", cls="Warrior", spec="Arms", armory=link).armory.id == 42


# ---------------------------------------------------------------- the verb

def test_rename_keeps_the_old_name_as_an_alias(reg):
    m, c = tessa(reg)
    rev = reg.characters_rev(m)
    n = commits(reg)
    got = reg.rename_character(TESSA, "Bram Kettle", "Bram", "Ashby", "Tessa", by_id=TESSA)
    assert got is reg.members[TESSA].characters[0] and got.label == "Bram Ashby" and got.former_names == ["Bram Kettle"]
    assert got.matches("Bram Kettle") and got.matches("bram ashby") and got.matches("Bram")
    assert reg.characters_rev(reg.members[TESSA]) != rev and commits(reg) == n + 1
    stored = json.loads((reg.store.root / reg.key / "members" / f"{TESSA}.json").read_text())["characters"][0]
    assert (stored["name"], stored["surname"], stored["former_names"]) == ("Bram", "Ashby", ["Bram Kettle"])
    # the same name (any case) is no change and no write
    n = commits(reg)
    assert reg.rename_character(TESSA, "Bram Ashby", "bram", "ASHBY", "Tessa", by_id=TESSA).former_names == ["Bram Kettle"]
    assert commits(reg) == n
    # renamed by a former name; then back: the alias list never grows a duplicate and drops the name taken back
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Moss", "Tessa", by_id=TESSA)
    assert c.label == "Bram Moss" and c.former_names == ["Bram Kettle", "Bram Ashby"]
    reg.rename_character(TESSA, "Bram Moss", "Bram", "Kettle", "Tessa", by_id=TESSA)
    assert c.label == "Bram Kettle" and c.former_names == ["Bram Ashby", "Bram Moss"]
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Ashby", "Tessa", by_id=TESSA)
    assert c.former_names == ["Bram Moss", "Bram Kettle"]
    with pytest.raises(RegistryError):
        reg.rename_character(TESSA, "Nobody Here", "Bram", "Vale", "Tessa", by_id=TESSA)
    with pytest.raises(RegistryError):
        reg.rename_character(TESSA, "Bram Ashby", "B4d", None, "Tessa", by_id=TESSA)


def test_find_answers_to_a_former_name(reg):
    tessa(reg)
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Ashby", "Tessa", by_id=TESSA)
    m, c = reg.find("Bram Kettle")
    assert m.discord_id == TESSA and c.label == "Bram Ashby"
    assert reg.find("bram ashby")[1] is c and reg.find("Bram")[1] is c
    assert reg.same_character(c, " bram kettle ") and reg.same_character(c, "Bram Ashby") and not reg.same_character(c, "Bram")
    assert not reg.same_character(c, "Bram Kettle", former=False)
    assert reg.set_flex(TESSA, "Bram Kettle", ["tank"]) is c, "a member's own verbs answer to the old name too"


def test_a_current_name_beats_anothers_former_name(reg):
    tessa(reg)
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Ashby", "Tessa", by_id=TESSA)
    _, orlas = reg.add_character(ORLA, "Orla", "Bram", "Mage", "Fire", None, True, surname="Kettle")  # a former name never blocks a new one
    assert reg.find("Bram Kettle") == (reg.members[ORLA], orlas)
    # a member's own two characters: one's former name is the other's current name — the current one answers
    _, cinder = reg.add_character(TESSA, "Tessa", "Cinder", "Priest", "Holy", None, False, surname="Vale")
    reg.rename_character(TESSA, "Cinder Vale", "Cinder", "Moss", "Tessa", by_id=TESSA)
    _, again = reg.add_character(TESSA, "Tessa", "Cinder", "Mage", "Frost", None, False, surname="Vale")
    assert reg.find("Cinder Vale")[1] is again and reg.find("Cinder Moss")[1] is cinder
    m = reg.members[TESSA]
    assert reg.own_character(m.active(), "Cinder Vale") is again and reg.own_character(m.active(), "Cinder Moss") is cinder
    assert reg.wish_character(TESSA, "Cinder Vale")[1] is again
    aw = LootAward(raider="Cinder Vale", item_id=0, item_name="Robe", tier="S", total_weight=1, offspec=False, received=date(2026, 12, 1),
                   instance="x", boss="b", discord_id=TESSA)
    assert reg.award_holder(aw) == (m, again)
    sub = aw.model_copy(update={"discord_id": None, "raider": "Bram Kettle"})
    assert reg.award_holder(sub) == (reg.members[ORLA], orlas), "a row with no member: the current name wins"


def test_a_taken_name_is_refused_with_no_partial_write(reg, rs):
    m, c = tessa(reg)
    reg.add_character(ORLA, "Orla", "Wren", "Mage", "Fire", None, True, surname="Hollow")
    ev = live_run(reg, rs, 3)
    rc.set_signup(reg, rs, ev, m, "Bram Kettle", "in", source="test")
    run_file = (reg.store.root / ev.rel_path(reg.key)).read_text()
    member_file = (reg.store.root / reg.key / "members" / f"{TESSA}.json").read_text()
    n = commits(reg)
    with pytest.raises(RegistryError) as e:
        reg.rename_character(TESSA, "Bram Kettle", "wren", "hollow", "Officer", by_id=1, officer=True)
    assert str(e.value) == "Wren Hollow is registered to Orla. Move it or retire it first."
    with pytest.raises(RegistryError) as e:
        reg.rename_character(TESSA, "Bram Kettle", "Wren", "Hollow", "Tessa", by_id=TESSA)
    assert str(e.value) == "That name is already registered here. Ask an officer."
    _, own = reg.add_character(TESSA, "Tessa", "Sorrel", "Priest", "Holy", None, False)
    with pytest.raises(RegistryError, match="registered to Tessa"):
        reg.rename_character(TESSA, "Bram Kettle", "Sorrel", None, "Officer", by_id=1, officer=True)
    member_file = (reg.store.root / reg.key / "members" / f"{TESSA}.json").read_text()
    n = commits(reg)
    with pytest.raises(RegistryError):
        reg.rename_character(TESSA, "Bram Kettle", "Wren", "Hollow", "Tessa", by_id=TESSA)
    assert c.label == "Bram Kettle" and c.former_names == [] and commits(reg) == n
    assert (reg.store.root / ev.rel_path(reg.key)).read_text() == run_file
    assert (reg.store.root / reg.key / "members" / f"{TESSA}.json").read_text() == member_file


def test_live_run_files_take_the_new_label_finished_ones_do_not(reg, rs):
    m, c = tessa(reg)
    puppets = reg.test_members()[:4]
    open_ev = live_run(reg, rs, 3)
    rc.set_signup(reg, rs, open_ev, m, "Bram Kettle", "in", source="test")
    locked = live_run(reg, rs, 4, instance="hyjal_summit_forever")
    rc.set_signup(reg, rs, locked, m, "Bram Kettle", "in", source="test")
    join(reg, rs, locked, puppets)
    lock_with_board(reg, rs, locked, ["Tessa"] + [p.display_name for p in puppets])
    locked.fill_asks.append(rc.FillAsk(discord_id=TESSA, display_name="Tessa", kind="sub", role="melee", character="Bram Kettle", spec="Arms", reason="short 1"))
    locked.callouts.append(rc.Callout(discord_id=TESSA, display_name="Tessa", character="Bram Kettle", at=rc.now(), hours_before=2.0, late=False))
    rs.save(locked, "t: an ask and a callout")
    m.placement_asks.append({"roster": locked.team, "character": "Bram Kettle", "asked_at": rc.now(), "answer": None})
    reg.save(m, "t: a placement ask")
    done = live_run(reg, rs, 5)
    rc.set_signup(reg, rs, done, m, "Bram Kettle", "in", source="test")
    done.state = "done"
    rs.save(done, "t: done")
    gone = live_run(reg, rs, 6, instance="hyjal_summit_forever")
    rc.set_signup(reg, rs, gone, m, "Bram Kettle", "in", source="test")
    gone.state = "cancelled"
    rs.save(gone, "t: cancelled")
    finished = {e.key: (reg.store.root / e.rel_path(reg.key)).read_text() for e in (done, gone)}
    n = commits(reg)
    changed: list[str] = []
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Ashby", "Tessa", by_id=TESSA, rs=rs, relabelled=changed)
    assert commits(reg) == n + 1, "the member and every live run in one commit"
    assert sorted(changed) == sorted([open_ev.key, locked.key])
    fresh = rc.RaidStore(reg.store, reg.key)
    for ev in (fresh.events[open_ev.key], fresh.events[locked.key], rs.events[locked.key]):
        assert ev.signups[str(TESSA)].character == "Bram Ashby"
    lk = fresh.events[locked.key]
    assert lk.seat_of(TESSA)[1].character == "Bram Ashby"
    assert lk.fill_asks[-1].character == "Bram Ashby" and lk.callouts[-1].character == "Bram Ashby"
    assert all(p.character != "Bram Kettle" for r in lk.rosters for p in r.selected + r.benched)
    assert reg.members[TESSA].placement_asks[-1]["character"] == "Bram Ashby"
    assert {k: (reg.store.root / fresh.events[k].rel_path(reg.key)).read_text() for k in finished} == finished, "finished and cancelled runs keep the old name"
    assert reg.members[TESSA].characters[0].rosters == c.rosters, "regulars keep their series keys"


# ---------------------------------------------------------------- history keeps answering

def test_past_awards_still_belong_to_the_character(reg, env):  # noqa: F811
    from fastapi.testclient import TestClient
    from test_loot_verbs import tiered_item
    from test_loot_web import WebLootBot, as_user

    from oibot_gm.web import app as web

    m = reg.test_members()[0]
    old = m.main.label
    award_row(reg, old, uid=m.discord_id, iid="r1-d1", received="2026-12-09")
    award_row(reg, old, uid=None, item="Shard of Doom", iid="r1-d2", received="2026-12-10")
    rs = rc.RaidStore(reg.store, reg.key)
    att = reg.attendance(m.discord_id)
    ctx = RegistryLootContext(reg, reg.store, None)
    item = tiered_item(reg)
    spec = reg.profile.spec(m.main.cls, m.main.spec)

    def cand(label):
        p = Player(signup_name=m.display_name, pos=1, discord_id=m.discord_id, status="signed", cls=m.main.cls, spec=m.main.spec, role=spec.role, character=label)
        (x,) = scoring.candidates(reg.profile, item, [p], ctx.ledger, {}, date(2026, 12, 12), reg=reg) or [None]
        return x

    before = cand(old)
    reg.rename_character(m.discord_id, old, "Bramble", None, "Officer", by_id=None, officer=True, rs=rs)
    assert m.main.label == "Bramble"
    ctx.reload()
    after = cand("Bramble")
    assert before is not None and (after.recent_power, after.already_has) == (before.recent_power, before.already_has)
    assert after.recent_power > 0, "the loot divisor still counts what the old name received"
    assert [a.import_id for a in loot_ops.member_awards(reg, ctx, m)] == ["r1-d2", "r1-d1"]
    got = loot_ops.received_map(reg, ctx, m)
    assert ("bramble", "n:robe of tests") in got and ("bramble", "n:shard of doom") in got
    assert reg.attendance(m.discord_id) == att, "attendance is by Discord id: unchanged"
    bot = WebLootBot(reg, rs)
    c = TestClient(web.create_app(bot))
    rows = c.get("/api/me/loot", headers=as_user(c, m.discord_id)).json()["rows"]
    assert {r["item"] for r in rows} == {"Robe of Tests", "Shard of Doom"} and all(r["character"] == old for r in rows), "history keeps its name"
    assert all(r["cls"] == m.main.cls for r in rows), "the old name still finds the character's class"
    other = reg.test_members()[1]
    assert c.get("/api/me/loot", headers=as_user(c, other.discord_id)).json()["rows"] == []


def test_an_appeal_on_an_old_award_still_opens(reg):
    m = reg.test_members()[0]
    old = m.main.label
    award_row(reg, old, uid=m.discord_id, iid="r1-d7")
    ctx = RegistryLootContext(reg, reg.store, None)
    (aw,) = ctx.ledger
    aid = appeals.ledger_ids(ctx.ledger)[id(aw)]
    reg.rename_character(m.discord_id, old, "Bramble", None, "Officer", by_id=None, officer=True)
    ctx.reload()
    hit = appeals.find_award(reg, ctx, m.discord_id, aid)
    assert hit.import_id == "r1-d7" and appeals.ledger_ids(ctx.ledger)[id(hit)] == aid, "award ids stay as they are"
    ap = appeals.record_open(reg, m, hit, "I was told this was mine after the reset.", via="web", aid=aid)
    assert ap.id == aid and ap.character == "Bramble"


def test_import_dedupe_sees_through_a_rename(reg):
    from oibot_gm.importers import ledger_csv

    tessa(reg)
    award_row(reg, "Bram Kettle", uid=TESSA)
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Ashby", "Tessa", by_id=TESSA)
    for name in ("Bram Ashby", "Bram Kettle", "bram kettle"):
        rows = ledger_csv.parse(f"character,item,received\n{name},Robe of Tests,2026-12-09\n").rows
        rep = reg.import_awards(rows, "Officer", preview=True)
        assert (rep["new"], rep["duplicates"]) == (0, 1), name
    rows = ledger_csv.parse("character,item,received\nBram Kettle,Robe of Tests,2026-12-20\n").rows
    rep = reg.import_awards(rows, "Officer", preview=True)
    assert rep["new"] == 1 and rep["groups"]["new"][0]["character"] == "Bram Ashby" and rep["unknown_characters"] == []


# ---------------------------------------------------------------- the companion

def test_the_companion_routes_loot_by_the_new_and_the_old_name(reg, rs):
    from test_loot_verbs import LootBot, real_run, run

    from oibot_gm.feed import Companion

    ev1, ms = real_run(reg, rs, n=5, days=3)
    start = (reg.now_local() + timedelta(days=4, hours=3)).replace(second=0, microsecond=0)
    ev2 = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t")
    other = reg.test_members()[5:10]
    join(reg, rs, ev2, other)
    lock_with_board(reg, rs, ev2, [m.display_name for m in other])
    bot = LootBot(reg, rs)
    s1, _ = run(bot.open_loot(reg, rs, ev1, by="Officer"))
    s2, _ = run(bot.open_loot(reg, rs, ev2, by="Officer"))
    who = other[0]
    old = who.main.label
    # a pick the council made under the old name
    run(bot.tick(reg, s2, add=[{"name": "Shard of Doom", "boss": "Gatekeeper"}], by="Officer", by_id=1))
    run(bot.distribute(reg, s2, by="Officer"))
    (p,) = s2.proposals
    if p.to != old:
        run(bot.override(reg, s2, p.drop_id, old, "typed reason", by="Officer", rejudge=False))
    # a puppet's display name is its first character's name, so the name it is routed by is a rename further on
    reg.rename_character(who.discord_id, old, "Bramble", None, "Officer", by_id=None, officer=True, rs=rs)
    reg.rename_character(who.discord_id, "Bramble", "Brambleton", None, "Officer", by_id=None, officer=True, rs=rs)
    assert bot.active_raid(Companion("Brambleton", "c", "", "p")) is s2
    assert bot.active_raid(Companion("Bramble", "c", "", "p")) is s2, "a companion still started with an old name"
    assert bot.loot_route(Companion("Nobody", "c", "", "p"))[0] is None
    roster = bot.loot_roster(s2)
    assert ls.recipient_of(reg, roster, "Bramble")[0] == "Brambleton" and ls.recipient_of(reg, roster, "Bramble")[2] is not None
    # seen in game under the new name: confirmed, not a conflict
    out = run(bot.handle_feed_event({"type": "loot", "recipient": "Brambleton", "item_name": "Shard of Doom", "idem": "x1"},
                                    Companion("Brambleton", "c", "", "peer")))
    assert out["result"] == "confirmed", out
