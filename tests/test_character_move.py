"""Moving a character to another member (armory link, spec §5.4 "Move to another member…", the owner's decision
2026-11-12): when a claim was wrong an officer moves the character, and everything follows it — the record with its
wishlist, regular spots, armory link and former names, its past loot awards and its raid attendance, its signups and
seats on live runs. The ledger stays append-only and finished run files are never rewritten: the move is one row of
<guild>/character_moves.jsonl, and every "whose award / whose attendance" lookup resolves through it
(Registry.owner_of / owner_of_award). Both members get a notice, never a DM.

Every name here is invented (R8)."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import OWNER, join, lock_with_board
from test_loot_web import env  # noqa: F401 — a fixture

from oibot_gm import appeals, discord_armory, loot_ops
from oibot_gm import consequences as cq
from oibot_gm import raidcycle as rc
from oibot_gm.loot import scoring
from oibot_gm.lootctx import RegistryLootContext
from oibot_gm.models import Player
from oibot_gm.registry import ArmoryLink, Member, RegistryError

TESSA = 700_000_000_000_000_011
ORLA = 700_000_000_000_000_012
PELL = 700_000_000_000_000_013
RID = "barrow_deeps"
SERIES = f"{RID}:default"


def run(coro):
    return asyncio.run(coro)


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def moves_file(reg) -> Path:
    return reg.store.root / reg.key / "character_moves.jsonl"


def ledger_file(reg) -> Path:
    return reg.store.root / reg.key / "ledger.jsonl"


def people(reg):
    """Tessa: Bram Kettle (her main, a regular, linked, a wishlist, once called Bram Ashgrove) and Wren Hollow (an alt).
    Orla: Ivo Marsh (her main). Pell: a member with no character yet."""
    reg.set_raid_overrides(RID, {"slots": ["Tue 19:30", "Thu 19:30", "Sat 19:30"], "first_open": "2026-01-01T00:00"}, "t")
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Arms", None, True, surname="Kettle")
    reg.add_character(TESSA, "Tessa", "Wren", "Mage", "Fire", None, False, surname="Hollow")
    reg.add_character(ORLA, "Orla", "Ivo", "Priest", "Holy", None, True, surname="Marsh")
    reg.set_rank(TESSA, "core", "Owner", by_id=OWNER)
    m = reg.members[TESSA]
    c = reg.own_character(m.characters, "Bram Kettle")
    c.former_names = ["Bram Ashgrove"]
    c.armory = ArmoryLink(id=880011, slug="bram-kettle", linked_by="Tessa", linked_at="2026-11-01T10:00:00+00:00")
    reg.save(m, "t: a former name and a link")
    reg.regular_add(TESSA, SERIES, "t", "Bram Kettle")
    reg.set_wishlist(TESSA, "Bram Kettle", [{"name": "Robe of Tests"}], "Tessa")
    pell = Member(discord_id=PELL, display_name="Pell")
    reg.save(pell, "t: Pell, nothing registered")


def award_row(reg, raider, *, uid=None, item="Robe of Tests", received="2026-10-09", iid="mv-d1", weight=0.75, confirmed_at=None):
    row = {"raider": raider, "item_id": 0, "item_name": item, "tier": "S", "total_weight": weight, "offspec": False, "received": received,
           "instance": "x", "boss": "Gatekeeper", "import_id": iid, "source": "bot", "discord_id": uid}
    if confirmed_at:
        row["confirmed_at"] = confirmed_at
    reg.store.append_jsonl(Path(reg.key) / "ledger.jsonl", row)
    reg.store.commit("seed one award")


def live_run(reg, rs, days, *, instance=RID):
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, instance, start, by="t")


def done_run(reg, rs, joined, days_ago: float, *, label_of=None, n: int = 0):
    """A finished run `days_ago` back: `joined` signed up (each with `label_of[m]`, else their main), all seated, each
    confirmed yes."""
    start = (reg.now_local() + timedelta(days=20 + n, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, RID, start, by="t")
    for m in joined:
        rc.set_signup(reg, rs, ev, m, (label_of or {}).get(m.discord_id), "in", source="test")
    lock_with_board(reg, rs, ev, [m.display_name for m in joined])
    for m in joined:
        reg.add_placement_ask(m.discord_id, ev.team, ev.signups[str(m.discord_id)].character, by="t")
        reg.members[m.discord_id].placement_asks[-1]["answer"] = "yes"
        reg.save(reg.members[m.discord_id], "t: yes")
    ev.starts_at = (reg.now_local() - timedelta(days=days_ago)).replace(second=0, microsecond=0).isoformat()
    rc.end_run(reg, rs, ev, "t")
    return ev


def move(reg, rs, label="Bram Kettle", to=ORLA, frm=TESSA, by_id=OWNER, moved=None):
    return reg.move_character(frm, label, to, "Owner", rs=rs, by_id=by_id, moved=moved)


def char(reg, uid, label):
    return reg.own_character(reg.members[uid].characters, label)


# ---------------------------------------------------------------- the record

def test_the_record_and_its_wishlist_regulars_and_link_arrive(reg, rs):
    people(reg)
    t_rev, o_rev = reg.characters_rev(reg.members[TESSA]), reg.characters_rev(reg.members[ORLA])
    n = commits(reg)
    c = move(reg, rs)
    assert commits(reg) == n + 1, "both member files and the move's row in one commit"
    tessa, orla = reg.members[TESSA], reg.members[ORLA]
    assert char(reg, TESSA, "Bram Kettle") is None and char(reg, ORLA, "Bram Kettle") is c
    assert [w.name for w in c.wishlist] == ["Robe of Tests"] and c.rosters == [SERIES]
    assert c.armory.id == 880011 and c.former_names == ["Bram Ashgrove"]
    assert reg.armory_holder(880011)[0] is orla, "one claim per armory id still holds: it is the same record"
    assert [m.display_name for m, _c in reg.regulars(SERIES)] == ["Orla"]
    assert not c.is_main and c.rank == "alt", "Orla has a main: it arrives as an alt"
    assert orla.main.label == "Ivo Marsh"
    assert tessa.main.label == "Wren Hollow" and tessa.main.rank == "core", "R3: Tessa's next character is her main, her rank with it"
    assert reg.characters_rev(tessa) != t_rev and reg.characters_rev(orla) != o_rev
    (row,) = [json.loads(x) for x in moves_file(reg).read_text().splitlines()]
    assert {k: row[k] for k in ("label", "armory_id", "from", "to", "by", "by_id")} == {
        "label": "Bram Kettle", "armory_id": 880011, "from": TESSA, "to": ORLA, "by": "Owner", "by_id": OWNER}
    assert row["at"] and set(row["names"]) == {"Bram Kettle", "Bram Ashgrove"}
    reg.reload()
    assert char(reg, ORLA, "Bram Kettle").armory.id == 880011, "the files say so too"
    # to a member with no main: it becomes their main
    c2 = move(reg, rs, "Wren Hollow", to=PELL)
    assert c2.is_main and reg.members[PELL].main is c2 and reg.members[TESSA].main is None


def test_a_regular_spot_the_new_member_already_holds_stays_theirs(reg, rs):
    people(reg)
    reg.add_character(PELL, "Pell", "Tamsin", "Druid", "Restoration", None, True, surname="Reed")
    reg.regular_add(PELL, SERIES, "t", "Tamsin Reed")
    c = move(reg, rs, to=PELL)
    assert not c.is_main and c.rosters == [], "one character per member per series: Pell's own spot stands"
    assert [(m.display_name, x.label) for m, x in reg.regulars(SERIES)] == [("Pell", "Tamsin Reed")]


# ---------------------------------------------------------------- the refusals

def refused(reg, rs, **kw) -> str:
    n, files = commits(reg), {p: p.read_text() for p in (reg.store.root / reg.key / "members").glob("*.json")}
    rows = moves_file(reg).read_text() if moves_file(reg).exists() else None
    with pytest.raises(RegistryError) as e:
        move(reg, rs, **kw)
    assert commits(reg) == n and (moves_file(reg).read_text() if moves_file(reg).exists() else None) == rows
    assert {p: p.read_text() for p in files} == files, "nothing written"
    return str(e.value)


def test_an_open_ask_is_refused(reg, rs):
    people(reg)
    ev = live_run(reg, rs, 4)
    rc.set_signup(reg, rs, ev, reg.members[TESSA], "Bram Kettle", "in", source="test")
    lock_with_board(reg, rs, ev, ["Tessa"])
    reg.add_placement_ask(TESSA, ev.team, "Bram Kettle", by="t")
    assert refused(reg, rs) == "Bram Kettle has an open ask. Withdraw it first."
    reg.members[TESSA].placement_asks[-1]["answer"] = "yes"
    reg.save(reg.members[TESSA], "t: answered")
    ev.fill_asks.append(rc.FillAsk(discord_id=TESSA, display_name="Tessa", kind="sub", role="melee", character="Bram Kettle", spec="Arms", reason="short 1"))
    rs.save(ev, "t: a fill ask")
    assert refused(reg, rs) == "Bram Kettle has an open ask. Withdraw it first."
    ev.fill_asks[-1].answer = "no"
    rs.save(ev, "t: answered")
    move(reg, rs)  # an answered ask is no obstacle


def test_a_name_the_new_member_already_has_is_refused(reg, rs):
    people(reg)
    o = char(reg, ORLA, "Ivo Marsh")
    o.former_names = ["Bram Kettle"]  # Orla's character went by that name before a rename
    reg.save(reg.members[ORLA], "t: a former name")
    assert refused(reg, rs) == "Orla already has a character called Bram Kettle."


def test_a_member_not_registered_is_refused_and_never_created(reg, rs):
    people(reg)
    stranger = 700_000_000_000_000_099
    assert refused(reg, rs, to=stranger) == "The new member has to register here first."
    assert stranger not in reg.members
    reg.members[ORLA].left_at = "2026-11-01T00:00:00+00:00"
    reg.save(reg.members[ORLA], "t: left")
    assert refused(reg, rs) == "Orla has left the server."


def test_the_other_refusals(reg, rs):
    people(reg)
    assert refused(reg, rs, to=TESSA) == "Bram Kettle is already Tessa's."
    assert refused(reg, rs, label="Nobody Here") == "Tessa has no character named Nobody Here."
    assert refused(reg, rs, to=reg.test_members()[0].discord_id) == "A test character never moves to or from a real member."
    assert refused(reg, rs, by_id=ORLA) == "Ask another officer to move it to you."
    move(reg, rs, "Wren Hollow", by_id=TESSA)  # from yourself is how an officer fixes their own wrong claim
    # the new member already signed up for a live run the character is on: the officer sorts that signup out first
    ev = live_run(reg, rs, 3)
    rc.set_signup(reg, rs, ev, reg.members[TESSA], "Bram Kettle", "in", source="test")
    rc.set_signup(reg, rs, ev, reg.members[ORLA], "Ivo Marsh", "in", source="test")
    assert refused(reg, rs) == f"Orla is already on {rc.run_words(reg, ev)}, so sort that out first."


# ---------------------------------------------------------------- history follows the character

def test_past_awards_and_attendance_count_for_the_new_member(reg, rs, env):  # noqa: F811
    from conftest import OFFICER_ROLE, MemberDirectory
    from fastapi.testclient import TestClient
    from test_loot_verbs import tiered_item
    from test_loot_web import OFFICER, WebLootBot, as_user, person

    from oibot_gm.help import Ask, _loot
    from oibot_gm.web import app as web

    people(reg)
    tessa, orla = reg.members[TESSA], reg.members[ORLA]
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1")
    award_row(reg, "Bram Ashgrove", uid=TESSA, item="Shard of Doom", iid="mv-d2", received="2026-10-02")  # under its former name
    award_row(reg, "Wren Hollow", uid=TESSA, item="Wand of Tests", iid="mv-d3")  # Tessa's other character: stays hers
    award_row(reg, "Bram Kettle", uid=None, item="Cloak of Tests", iid="mv-d4")  # recorded without an id
    done_run(reg, rs, [tessa], 6, label_of={TESSA: "Bram Kettle"})
    done_run(reg, rs, [tessa], 5, label_of={TESSA: "Wren Hollow"}, n=1)
    assert reg.attendance(TESSA) == (2, 2) and reg.attendance(ORLA) == (0, 0)
    ctx = RegistryLootContext(reg, reg.store, None)
    item = tiered_item(reg)

    def power(uid, label):
        m = reg.members[uid]
        c = reg.own_character(m.characters, label)
        spec = reg.profile.spec(c.cls, c.spec)
        p = Player(signup_name=m.display_name, pos=1, discord_id=uid, status="signed", cls=c.cls, spec=c.spec, role=spec.role, character=label)
        (x,) = scoring.candidates(reg.profile, item, [p], ctx.ledger, {}, date(2026, 10, 12), reg=reg) or [None]
        return x.recent_power

    before = power(TESSA, "Bram Kettle")
    assert before > 0
    move(reg, rs)
    ctx.reload()
    assert reg.owner_of_award(next(a for a in ctx.ledger if a.import_id == "mv-d1")) == ORLA
    assert reg.owner_of_award(next(a for a in ctx.ledger if a.import_id == "mv-d3")) == TESSA
    assert power(ORLA, "Bram Kettle") == before, "the loot divisor counts what it received for Tessa"
    mine = lambda m: sorted(a.import_id for a in loot_ops.member_awards(reg, ctx, m) if (a.import_id or "").startswith("mv-"))  # noqa: E731
    assert mine(orla) == ["mv-d1", "mv-d2", "mv-d4"] and mine(tessa) == ["mv-d3"]
    got = loot_ops.received_map(reg, ctx, orla)
    assert ("bram kettle", "n:robe of tests") in got and ("bram kettle", "n:shard of doom") in got
    assert not any(k[0] == "bram kettle" for k in loot_ops.received_map(reg, ctx, tessa))
    assert reg.attendance(ORLA) == (1, 1), "the run Tessa played on Bram Kettle counts for Orla"
    assert reg.attendance_runs(ORLA)[0]["character"] == "Bram Kettle"
    assert reg.attendance(TESSA) == (1, 1), "only the run on Wren Hollow is still Tessa's"
    # web: My loot, the log's member filter and `mine`
    bot = WebLootBot(reg, rs)
    MemberDirectory({(1, OFFICER): person(OFFICER, OFFICER_ROLE), (1, TESSA): person(TESSA), (1, ORLA): person(ORLA)}).install(bot, servers=(1,))
    c = TestClient(web.create_app(bot))

    def ids(rows):
        return sorted(r["item"] for r in rows if r["item"] in {"Robe of Tests", "Shard of Doom", "Wand of Tests", "Cloak of Tests"})

    assert ids(c.get("/api/me/loot", headers=as_user(c, ORLA)).json()["rows"]) == ["Cloak of Tests", "Robe of Tests", "Shard of Doom"]
    assert ids(c.get("/api/me/loot", headers=as_user(c, TESSA)).json()["rows"]) == ["Wand of Tests"]
    assert ids(c.get("/api/loot/log", params={"member": "Orla"}, headers=as_user(c, OFFICER)).json()["rows"]) == ["Cloak of Tests", "Robe of Tests", "Shard of Doom"]
    assert ids(c.get("/api/loot/log", params={"member": "Tessa"}, headers=as_user(c, OFFICER)).json()["rows"]) == ["Wand of Tests"]
    log = {r["item"]: r for r in c.get("/api/loot/log", headers=as_user(c, ORLA)).json()["rows"]}
    assert log["Robe of Tests"]["mine"] and not log["Wand of Tests"]["mine"] and log["Shard of Doom"]["cls"] == "Warrior"
    lines = _loot(Ask(reg, rs, OFFICER, True, "what loot did Tessa get"))
    assert any("for the member(s) named: 1 award(s)" in x for x in lines), lines


def test_attendance_on_a_night_both_played_keeps_the_new_members_own_seat(reg, rs):
    people(reg)
    tessa, orla = reg.members[TESSA], reg.members[ORLA]
    done_run(reg, rs, [tessa, orla], 6, label_of={TESSA: "Bram Kettle"})
    move(reg, rs)
    rows = reg.attendance_runs(ORLA)
    assert len(rows) == 1 and rows[0]["character"] == "Ivo Marsh", "one row per run: Orla's own seat that night"
    assert reg.attendance(TESSA) == (0, 0)


def test_an_award_after_the_move_on_a_name_taken_again_stays_with_its_member(reg, rs):
    people(reg)
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1", received="2026-10-09")
    move(reg, rs)
    reg.rename_character(ORLA, "Bram Kettle", "Bram", "Fallow", "Owner", rs=rs, by_id=OWNER, officer=True)
    reg.add_character(TESSA, "Tessa", "Bram", "Rogue", "Combat", None, False, surname="Kettle")  # the freed name, a new record
    later = (reg.now_local() + timedelta(days=1)).date().isoformat()  # tomorrow's guild day: after the move
    award_row(reg, "Bram Kettle", uid=TESSA, item="Wand of Tests", iid="mv-d5", received=later)
    ctx = RegistryLootContext(reg, reg.store, None)
    by = {a.import_id: a for a in ctx.ledger}
    assert reg.owner_of_award(by["mv-d1"]) == ORLA and reg.award_holder(by["mv-d1"])[1].label == "Bram Fallow"
    assert reg.owner_of_award(by["mv-d5"]) == TESSA and reg.award_holder(by["mv-d5"])[1] is char(reg, TESSA, "Bram Kettle")


def test_two_moves_fold(reg, rs):
    people(reg)
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1")
    done_run(reg, rs, [reg.members[TESSA]], 6, label_of={TESSA: "Bram Kettle"})
    move(reg, rs)
    move(reg, rs, frm=ORLA, to=PELL)
    ctx = RegistryLootContext(reg, reg.store, None)
    (a,) = [a for a in ctx.ledger if a.import_id == "mv-d1"]
    assert reg.owner_of_award(a) == PELL and reg.award_holder(a)[0] is reg.members[PELL]
    assert reg.attendance(PELL) == (1, 1) and reg.attendance(TESSA) == (0, 0) and reg.attendance(ORLA) == (0, 0)
    assert len(moves_file(reg).read_text().splitlines()) == 2


def test_an_appeal_on_a_moved_award_belongs_to_the_new_member(reg, rs):
    people(reg)
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1")
    award_row(reg, "Bram Kettle", uid=TESSA, item="Shard of Doom", iid="mv-d2")
    ctx = RegistryLootContext(reg, reg.store, None)
    ids = appeals.ledger_ids(ctx.ledger)
    a1, a2 = (next(a for a in ctx.ledger if a.import_id == i) for i in ("mv-d1", "mv-d2"))
    ap = appeals.record_open(reg, reg.members[TESSA], a1, "I was told this was another raider's call.", via="web", aid=ids[id(a1)])
    move(reg, rs)
    ctx.reload()
    st = appeals.store_of(reg)
    assert [x.id for x in st.of_member(ORLA, reg)] == [ap.id] and st.of_member(TESSA, reg) == []
    assert st.get(ap.id).member_id == TESSA, "the appeal file is not rewritten: its owner is resolved"
    assert appeals.appeal_owner(reg, st.get(ap.id)) == ORLA
    assert appeals.find_award(reg, ctx, ORLA, ids[id(a2)]).import_id == "mv-d2", "Orla may ask about it now"
    with pytest.raises(RegistryError):
        appeals.find_award(reg, ctx, TESSA, ids[id(a2)])
    done = appeals.record_resolve(reg, ap.id, "stands", None, "Owner", OWNER)
    assert any(n["kind"] == "appeal" for n in reg.notices(ORLA)) and not any(n["kind"] == "appeal" for n in reg.notices(TESSA))
    assert appeals.resolve_consequence(reg, done, "stands").startswith("Orla's question")


# ---------------------------------------------------------------- live runs

def test_a_live_seat_moves_with_it(reg, rs):
    people(reg)
    tessa = reg.members[TESSA]
    puppets = reg.test_members()[:3]
    open_ev = live_run(reg, rs, 3)
    rc.set_signup(reg, rs, open_ev, tessa, "Bram Kettle", "in", source="test")
    open_ev.pins[str(TESSA)] = "in"
    open_ev.layout = [["Tessa"]]
    rs.save(open_ev, "t: a pin and a board")
    locked = live_run(reg, rs, 4, instance="hyjal_summit_forever")
    rc.set_signup(reg, rs, locked, tessa, "Bram Kettle", "in", source="test")
    join(reg, rs, locked, puppets)
    lock_with_board(reg, rs, locked, ["Tessa"] + [p.display_name for p in puppets])
    locked.lock_seats = [TESSA] + [p.discord_id for p in puppets]
    locked.series, locked.regulars = SERIES, [TESSA]
    rs.save(locked, "t: lock seats")
    reg.add_placement_ask(TESSA, locked.team, "Bram Kettle", by="t")
    reg.members[TESSA].placement_asks[-1]["answer"] = "yes"
    reg.save(reg.members[TESSA], "t: yes")
    other = live_run(reg, rs, 5)
    rc.set_signup(reg, rs, other, tessa, "Wren Hollow", "in", source="test")  # her other character: stays hers
    done = done_run(reg, rs, [tessa], 6, label_of={TESSA: "Bram Kettle"}, n=3)
    finished = (reg.store.root / done.rel_path(reg.key)).read_text()
    n = commits(reg)
    keys: list[str] = []
    move(reg, rs, moved=keys)
    assert commits(reg) == n + 1, "the members, the move's row and the live runs in one commit"
    assert {open_ev.key, locked.key} <= set(keys) <= {open_ev.key, locked.key, other.key}, "the other run only for its regulars"
    fresh = rc.RaidStore(reg.store, reg.key)
    assert TESSA not in fresh.events[other.key].regulars, "her regular spot in the series went with the character"
    for ev in (fresh.events[open_ev.key], fresh.events[locked.key], rs.events[locked.key]):
        sg = ev.signups[str(ORLA)]
        assert str(TESSA) not in ev.signups and sg.discord_id == ORLA and sg.display_name == "Orla" and sg.character == "Bram Kettle"
    o = fresh.events[open_ev.key]
    assert o.pins == {str(ORLA): "in"} and o.layout == [["Orla"]]
    lk = fresh.events[locked.key]
    assert lk.seat_of(TESSA) is None and lk.seat_of(ORLA)[1].character == "Bram Kettle" and lk.seat_of(ORLA)[1].signup_name == "Orla"
    assert ORLA in lk.lock_seats and TESSA not in lk.lock_seats and lk.regulars == [ORLA]
    assert fresh.events[other.key].signups[str(TESSA)].character == "Wren Hollow"
    assert (reg.store.root / done.rel_path(reg.key)).read_text() == finished, "a finished run is never rewritten"
    asks = reg.members[ORLA].placement_asks
    assert asks and asks[-1]["roster"] == locked.team and asks[-1]["answer"] == "yes", "the answered ask on the live run goes with the seat"
    assert not any(a["roster"] == locked.team for a in reg.members[TESSA].placement_asks)


# ---------------------------------------------------------------- the surface's verb (discord_armory)

class FakeOps:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def emit(self, cfg, level, text, exc=None, dm=True, dm_only=None):
        self.lines.append((level, text))


class FakeBot:
    def __init__(self, rs):
        self.ops = FakeOps()
        self.dms: list = []
        self.redrawn: list[str] = []
        self.raids = type("R", (), {"store": lambda _s, reg: rs})()

    async def send_member_dm(self, *a, **k):
        self.dms.append((a, k))
        raise AssertionError("a move never DMs")

    async def refresh_sheet(self, reg, ev):
        self.redrawn.append(ev.key)


def test_both_get_a_notice_and_nobody_a_dm(reg, rs):
    people(reg)
    bot = FakeBot(rs)
    ev = live_run(reg, rs, 3)
    rc.set_signup(reg, rs, ev, reg.members[TESSA], "Bram Kettle", "in", source="test")
    with pytest.raises(RegistryError) as e:  # an officer only, decided live
        run(discord_armory.move_character(bot, reg, TESSA, "Bram Kettle", ORLA, "Tessa", by_id=TESSA))
    assert str(e.value) == discord_armory.OFFICER_ONLY["move"]
    n = commits(reg)
    said = run(discord_armory.move_character(bot, reg, TESSA, "Bram Kettle", ORLA, "Owner", by_id=OWNER, preview=True))
    assert said == cq.move_consequence(reg, reg.members[TESSA], char(reg, TESSA, "Bram Kettle"), reg.members[ORLA]) and commits(reg) == n
    c = run(discord_armory.move_character(bot, reg, TESSA, "Bram Kettle", ORLA, "Owner", by_id=OWNER, via="web"))
    assert c is char(reg, ORLA, "Bram Kettle") and commits(reg) == n + 1
    assert bot.dms == [] and bot.redrawn == [ev.key]
    (t,) = [x for x in reg.notices(TESSA) if x["kind"] == "character"]
    (o,) = [x for x in reg.notices(ORLA) if x["kind"] == "character"]
    assert t["text"] == "Owner moved Bram Kettle from you to Orla. Its past loot and attendance went with it."
    assert o["text"] == "Owner moved Bram Kettle from Tessa to you. Its past loot and attendance came with it."
    assert [x for x in bot.ops.lines if "moved" in x[1]] == [("info", "[web] Owner: moved Tessa's Bram Kettle to Orla")]


def test_the_ledger_file_is_untouched(reg, rs):
    people(reg)
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1")
    before = ledger_file(reg).read_bytes()
    move(reg, rs)
    assert ledger_file(reg).read_bytes() == before


def test_a_guild_that_never_moved_a_character_reads_as_before(reg, rs):
    people(reg)
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1")
    done_run(reg, rs, [reg.members[TESSA]], 6, label_of={TESSA: "Bram Kettle"})
    ctx = RegistryLootContext(reg, reg.store, None)
    assert reg.character_moves() == [] and not moves_file(reg).exists()
    for a in ctx.ledger:
        assert reg.owner_of_award(a) == a.discord_id
    assert reg.owner_of(TESSA, "Bram Kettle", "2026-10-09") == TESSA and reg.owner_of(None, "Bram Kettle", None) is None
    assert reg.attendance(TESSA) == (1, 1)
    assert [a.import_id for a in loot_ops.member_awards(reg, ctx, reg.members[TESSA]) if a.import_id == "mv-d1"] == ["mv-d1"]
    assert not moves_file(reg).exists(), "reading writes nothing"


def test_the_confirm_reads_aloud(reg, rs):
    from test_confirm_readability import lint

    people(reg)
    tessa, orla = reg.members[TESSA], reg.members[ORLA]
    names = {"Bram", "Tessa", "Orla", "Wren", "Pell"}
    for c in (char(reg, TESSA, "Bram Kettle"), char(reg, TESSA, "Wren Hollow")):
        said = cq.move_consequence(reg, tessa, c, orla)
        assert lint(said, names) == [], said
        assert any("loot history" in s and "attendance" in s for s in said)


def test_import_dedupe_sees_through_a_move(reg, rs):
    from oibot_gm.importers import ledger_csv

    people(reg)
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-d1", received="2026-10-09")
    move(reg, rs)
    rows = ledger_csv.parse("character,item,received\nBram Kettle,Robe of Tests,2026-10-09\n").rows
    rep = reg.import_awards(rows, "Officer", preview=True)
    assert (rep["new"], rep["duplicates"]) == (0, 1), "the ledger's row is Orla's character now, as the file's is"


# ---------------------------------------------------------------- fix round 1

def test_a_regular_who_did_not_answer_gets_the_moved_characters_rostered_night(reg, rs):
    people(reg)
    reg.regular_add(ORLA, SERIES, "t", "Ivo Marsh")
    ev = done_run(reg, rs, [reg.members[TESSA]], 6, label_of={TESSA: "Bram Kettle"})
    ev.regulars = [TESSA, ORLA]  # both expected; Orla never answered
    rs.save(ev, "t: regulars")
    assert [r["outcome"] for r in reg.attendance_runs(ORLA)] == ["no_answer"] and reg.attendance(TESSA) == (1, 1)
    move(reg, rs)
    rows = reg.attendance_runs(ORLA)
    assert [(r["outcome"], r["character"]) for r in rows] == [("rostered", "Bram Kettle")], "the better row: one run, one row"
    assert reg.attendance(ORLA) == (1, 1) and reg.attendance(TESSA) == (0, 0)


def test_a_first_name_row_is_decided_at_the_move(reg, rs):
    people(reg)
    award_row(reg, "Bram", uid=TESSA, iid="mv-f1")  # an imported row carries the first name only
    award_row(reg, "Wren", uid=TESSA, item="Wand of Tests", iid="mv-f2")
    move(reg, rs)
    by = {a.import_id: a for a in RegistryLootContext(reg, reg.store, None).ledger}
    assert reg.owner_of_award(by["mv-f1"]) == ORLA and reg.owner_of_award(by["mv-f2"]) == TESSA
    reg.add_character(TESSA, "Tessa", "Bram", "Rogue", "Combat", None, False, surname="Stone")  # a later Bram changes nothing
    assert reg.owner_of_award(by["mv-f1"]) == ORLA and reg.award_holder(by["mv-f1"])[0] is reg.members[ORLA]
    (row,) = [json.loads(x) for x in moves_file(reg).read_text().splitlines()]
    assert row["firsts"] == ["Bram"]


def test_a_shared_first_name_is_not_frozen(reg, rs):
    people(reg)
    reg.add_character(TESSA, "Tessa", "Bram", "Rogue", "Combat", None, False, surname="Stone")
    award_row(reg, "Bram", uid=TESSA, iid="mv-f1")
    move(reg, rs)
    (row,) = [json.loads(x) for x in moves_file(reg).read_text().splitlines()]
    assert row["firsts"] == [], "two of Tessa's characters answered to Bram at the move: the row stays hers"
    (a,) = [a for a in RegistryLootContext(reg, reg.store, None).ledger if a.import_id == "mv-f1"]
    assert reg.owner_of_award(a) == TESSA


def test_an_award_after_the_move_under_the_old_id_follows_the_character(reg, rs):
    people(reg)
    move(reg, rs)
    later = (reg.now_local() + timedelta(days=2)).isoformat()
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-late", received=later[:10], confirmed_at=later)  # a council's stale seat
    (a,) = [a for a in RegistryLootContext(reg, reg.store, None).ledger if a.import_id == "mv-late"]
    assert reg.owner_of_award(a) == ORLA, "Tessa held no Bram Kettle then: it is the moved character's"


def test_the_award_and_its_appeal_resolve_at_the_same_time(reg, rs):
    people(reg)
    move(reg, rs)
    reg.rename_character(ORLA, "Bram Kettle", "Bram", "Fallow", "Owner", rs=rs, by_id=OWNER, officer=True)
    soon = reg.now_local() + timedelta(hours=1)
    reg.add_character(TESSA, "Tessa", "Bram", "Rogue", "Combat", None, False, surname="Kettle")
    char(reg, TESSA, "Bram Kettle").created_at = (soon + timedelta(hours=1)).isoformat()  # registered after the award
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-t1", received=soon.date().isoformat(), confirmed_at=soon.isoformat())
    ctx = RegistryLootContext(reg, reg.store, None)
    (a,) = [a for a in ctx.ledger if a.import_id == "mv-t1"]
    ap = appeals.Appeal(id=appeals.ledger_ids(ctx.ledger)[id(a)], award_ref="x", member_id=TESSA, member_name="Tessa", character="Bram Kettle",
                        item="Robe of Tests", received=a.received.isoformat(), text="I'd like to know more please.", created_at="2026-11-12T00:00:00+00:00")
    assert reg.owner_of_award(a) == appeals.appeal_owner(reg, ap) == ORLA, "both read the confirmation time, not the day"


def test_the_confirm_names_a_regular_spot_given_up_and_a_main_change(reg, rs):
    people(reg)
    reg.add_character(PELL, "Pell", "Tamsin", "Druid", "Restoration", None, True, surname="Reed")
    reg.regular_add(PELL, SERIES, "t", "Tamsin Reed")
    said = cq.move_consequence(reg, reg.members[TESSA], char(reg, TESSA, "Bram Kettle"), reg.members[PELL])
    assert said[0] == "Tessa's main Bram Kettle moves to Pell with its loot history and attendance."
    assert said[1].startswith("It gives up its regular spot on Barrow Deeps") and said[1].endswith("where the new member already has one.")
    assert said[2] == "Wren Hollow becomes the old member's main."
    move(reg, rs, to=PELL)
    assert char(reg, PELL, "Bram Kettle").rosters == [] and reg.members[TESSA].main.label == "Wren Hollow"


def test_a_name_another_character_of_the_old_member_holds_now_is_not_frozen(reg, rs):
    """A1: Bram Kettle is renamed Bram Fallow, then Tessa registers a new Bram Kettle and it receives loot. Moving Bram
    Fallow leaves the new character's awards with Tessa, before and after a restart: a name one of her other characters
    answers to at the move is not one the moved character takes along."""
    from oibot_gm.registry import Registry

    people(reg)
    old = (reg.now_local() - timedelta(days=30)).date().isoformat()
    award_row(reg, "Bram Kettle", uid=TESSA, iid="mv-pre", received=old)  # Bram Fallow's own, under its name then
    reg.rename_character(TESSA, "Bram Kettle", "Bram", "Fallow", "Owner", rs=rs, by_id=OWNER, officer=True)
    reg.add_character(TESSA, "Tessa", "Bram", "Rogue", "Combat", None, False, surname="Kettle")
    today = reg.now_local()
    award_row(reg, "Bram Kettle", uid=TESSA, item="Wand of Tests", iid="mv-alt", received=today.date().isoformat(),
              confirmed_at=(today - timedelta(minutes=5)).isoformat())
    ctx = RegistryLootContext(reg, reg.store, None)
    by = {a.import_id: a for a in ctx.ledger}
    rogue = char(reg, TESSA, "Bram Kettle")
    before_pre = reg.award_holder(by["mv-pre"])
    assert reg.award_holder(by["mv-alt"])[1] is rogue
    move(reg, rs, label="Bram Fallow")
    (row,) = [json.loads(x) for x in moves_file(reg).read_text().splitlines()]
    assert "Bram Kettle" not in row["names"] and row["names"][0] == "Bram Fallow"
    assert row["firsts"] == []
    for r in (reg, Registry(reg.store, reg.key, reg.profile)):  # a restart reads the same row
        by = {a.import_id: a for a in RegistryLootContext(r, r.store, None).ledger}
        assert r.owner_of_award(by["mv-alt"]) == TESSA
        assert r.award_holder(by["mv-alt"])[1].label == "Bram Kettle" and r.award_holder(by["mv-alt"])[1].cls == "Rogue"
        # the moved character's own award under the name the Rogue now holds: a current name beats a former one, the
        # same answer before and after the move — Tessa's Bram Kettle
        h = r.award_holder(by["mv-pre"])
        assert r.owner_of_award(by["mv-pre"]) == TESSA and h[0].discord_id == before_pre[0].discord_id == TESSA
        assert h[1].label == before_pre[1].label == "Bram Kettle" and h[1].cls == "Rogue"


@pytest.mark.parametrize("miss", ["called_out", "no_thanks", "no_answer", "benched", "unconfirmed"])
def test_away_is_the_better_row_than_any_counted_miss(reg, miss):
    """A8: away is never counted, so on a night both rows exist it beats every counted miss — the loot factor's order."""
    from oibot_gm.registry import Registry

    away = {"outcome": "away", "counted": False, "attended": False}
    missed = {"outcome": miss, "counted": True, "attended": False}
    played = {"outcome": "rostered", "counted": True, "attended": True}
    assert Registry._row_rank(away) > Registry._row_rank(missed)
    assert Registry._row_rank(played) > Registry._row_rank(away)
