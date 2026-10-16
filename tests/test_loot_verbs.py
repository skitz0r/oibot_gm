"""The loot council's verbs (dual interfaces phase 1, design §3.1) over a scratch GitStore: LootMixin.open_loot · tick ·
distribute · override · confirm · discard · set_reason · correct · end_run, the companion feed through them, routing,
idempotency, revs, the snapshot distribute against a concurrent confirm, restart resume, the scheduler's auto-end, the
migration of old thread-keyed session files, and the data pieces (items, tiers, loot_cfg, neutral attendance).

The runs here are REAL (not test-bench) runs unless a test says otherwise, so awards reach the ledger. WoW: Forever
has no item tables: every drop is unknown — the Dec 9 case — unless a test writes a guild item file."""
from __future__ import annotations

import asyncio
import json
import threading
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from conftest import GUILD, join, lock_with_board
from oibot_gm import raidcycle as rc
from oibot_gm.discord_feed import FeedMixin
from oibot_gm.discord_loot import LootMixin
from oibot_gm.feed import Companion
from oibot_gm.loot import scoring, session as ls
from oibot_gm.loot.session import LootBusy, LootChanged, LootSession
from oibot_gm.lootctx import RegistryLootContext
from oibot_gm.models import LootRecommendation, Player
from oibot_gm.registry import RegistryError, item_key


def run(coro):
    return asyncio.run(coro)


class RecOps:
    def __init__(self):
        self.lines: list[tuple[str, str, str]] = []

    async def emit(self, cfg, level, text, exc=None, dm=True):
        self.lines.append((cfg.key, level, text))


class LootBot(FeedMixin, LootMixin):
    """The verbs as the client has them; the Discord side records instead of posting."""

    def __init__(self, reg, rs, provider=None):
        self.registries = types.SimpleNamespace(by_key={reg.key: reg}, many=False, only=lambda: reg, get=lambda k: reg if k in (reg.key, reg.config.name) else None,
                                                cfg_for=lambda k: reg.config)
        self.raids = types.SimpleNamespace(store=lambda r: rs)
        self.events: dict = {}
        self.ctx = None
        self.loot_provider = provider
        self.ops = RecOps()
        self.posts: list[tuple[str, str]] = []
        self.drop_posts: list[tuple[str, str]] = []
        self.feed = None
        self.thread_seq = 9000

    async def loot_open_thread(self, reg, rs, ev, session):
        self.thread_seq += 1
        return self.thread_seq

    async def loot_notify(self, reg, session, line=None, *, kind="", drop_id=None):
        self.posts.append((kind, line or ""))
        if drop_id:
            self.drop_posts.append((kind, drop_id))

    async def loot_ended(self, reg, session, text):
        self.posts.append(("ended", text))

    async def loot_closing(self, reg, session):
        self.posts.append(("closing", ""))

    async def loot_held(self, reg, session):
        self.posts.append(("held", session.id))


class SlowProvider:
    """A model stand-in that blocks in the worker thread until released, then picks the top score."""

    name = "slow"

    def __init__(self):
        self.go = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def complete(self, workload, system, user, schema):
        self.calls += 1
        self.entered.set()
        self.go.wait(5)
        first = user.split("| 1 | ", 1)[1].split(" |", 1)[0] if "| 1 | " in user else "(nobody)"
        return LootRecommendation(primary=first, justification="top score [R1]", close_call=False, deviates_from_score=False)

    def summary(self):
        return "slow"


def real_run(reg, rs, n=10, days=3):
    """A non-test run, locked around `n` puppets (its council writes the ledger)."""
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    ms = reg.test_members()[:n]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    assert not rc.is_test_run(reg, ev)
    return ev, ms


def char(m) -> str:
    return m.main.label


def ledger(reg) -> list[dict]:
    return reg.store.read_jsonl(Path(reg.key) / "ledger.jsonl")


def precedents(reg) -> list[dict]:
    return reg.store.read_jsonl(Path(reg.key) / "precedents.jsonl")


@pytest.fixture
def council(reg, rs):
    """A locked real run with an open council: SimpleNamespace(bot, reg, rs, ev, s, members)."""
    ev, ms = real_run(reg, rs)
    bot = LootBot(reg, rs)
    s, created = run(bot.open_loot(reg, rs, ev, by="Officer", by_id=1))
    assert created
    return types.SimpleNamespace(bot=bot, reg=reg, rs=rs, ev=ev, s=s, members=ms)


def tick_one(c, name="Shard of Doom", boss="Gatekeeper", copies=1, by="Officer"):
    return run(c.bot.tick(c.reg, c.s, add=[{"name": name, "boss": boss, "copies": copies}], by=by, by_id=1))


def tiered_item(reg, iid=90001, name="Robe of Tests", tiers=None):
    """A guild item file with tiers (what Loot › Items writes): every role at S unless told otherwise."""
    d = reg.store.root / reg.key / "items"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{iid}.yaml").write_text(yaml.safe_dump({"id": iid, "name": name, "slot": "chest", "type": "misc", "boss": "Gatekeeper",
                                                   "tiers": tiers or {"role:tank": "S", "role:healer": "S", "role:melee": "S", "role:ranged": "S"}}))
    reg.__dict__.pop("_items_cache", None)
    return reg.item(iid)


# ---------------------------------------------------------------- open, keys, files

def test_open_is_idempotent_keyed_by_guild_and_run(council):
    c = council
    again, created = run(c.bot.open_loot(c.reg, c.rs, c.ev, by="Mira", by_id=2))
    assert again is c.s and not created, "a second officer gets the same council"
    assert c.s.key == (GUILD, c.ev.key) and c.s.id == c.ev.key and c.s.run_key == c.ev.key
    assert (c.reg.store.root / GUILD / "events" / f"{c.ev.key}.json").exists(), "the file is events/<run key>.json"
    assert c.bot.event_for(c.s.raid_thread_id) is c.s and c.rs.events[c.ev.key].thread_id == c.s.raid_thread_id
    assert c.bot.loot_live(c.s) and any("opened the loot council" in t for _, _, t in c.bot.ops.lines)


def test_open_refuses_an_open_run_and_an_unknown_instance(reg, rs):
    start = (reg.now_local() + timedelta(days=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    bot = LootBot(reg, rs)
    with pytest.raises(RegistryError, match="Lock the roster first"):
        run(bot.open_loot(reg, rs, ev, by="Officer"))
    ev2, _ = real_run(reg, rs, days=5)
    ev2.instance = "atlantis"
    with pytest.raises(RegistryError, match="No raid definition for atlantis"):
        run(bot.open_loot(reg, rs, ev2, by="Officer"))
    assert not bot.events


def test_a_council_is_live_iff_its_run_is_locked(council):
    c = council
    assert c.bot.loot_live(c.s)
    c.ev.state = "done"
    assert not c.bot.loot_live(c.s)


# ---------------------------------------------------------------- unknown items, drops

def test_an_unknown_item_is_ticked_by_name_learned_and_scored_untiered(council):
    c = council
    res = tick_one(c)
    (d,) = res["added"]
    assert d.drop_id == "d1" and d.item_id == 0 and d.item_name == "Shard of Doom" and not d.tiered
    f = c.reg.store.root / GUILD / "items" / f"{item_key(0, 'Shard of Doom')}.yaml"
    doc = yaml.safe_load(f.read_text())
    assert doc["learned"]["times_dropped"] == 1 and doc["boss"] == "Gatekeeper" and doc["tiers"] == {}
    out = run(c.bot.distribute(c.reg, c.s, by="Officer", by_id=1))
    (p,) = out["proposals"]
    cands = p.result.candidates
    assert len(cands) == len(c.members), "no tier data: everyone rostered is a candidate"
    assert all(x.tier == "?" and x.upgrade_value == 0.5 and not x.tiered for x in cands)
    assert all(x.factors["attendance"] == 1.0 and not x.attendance_known for x in cands), "unknown attendance is neutral"
    assert "No tier data" in p.result.recommendation.warnings[0]
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, expect_to=p.to, by="Officer", by_id=1))
    (row,) = ledger(c.reg)
    assert row["item_name"] == "Shard of Doom" and row["item_id"] == 0 and row["tiered"] is False and row["total_weight"] == 0.5
    assert row["import_id"] == f"{c.ev.key}-d1" and row["by"] == "Officer" and row["discord_id"] is not None and row["candidates_snapshot"]


def test_two_copies_are_two_drops(council):
    c = council
    res = tick_one(c, copies=2)
    assert [d.drop_id for d in res["added"]] == ["d1", "d2"]
    out = run(c.bot.distribute(c.reg, c.s, by="Officer"))
    assert {p.drop_id for p in out["proposals"]} == {"d1", "d2"}
    run(c.bot.confirm(c.reg, c.s, batch_rev=out["batch_rev"], by="Officer"))
    assert sorted(r["import_id"] for r in ledger(c.reg)) == [f"{c.ev.key}-d1", f"{c.ev.key}-d2"], "two unknown copies never collide on id 0"
    assert len({r["raider"] for r in ledger(c.reg)}) == 2, "the second copy went to someone else (the first counted as an award)"


def test_untick_a_proposed_drop_drops_its_proposal_and_an_awarded_one_is_refused(council):
    c = council
    tick_one(c, copies=2)
    run(c.bot.distribute(c.reg, c.s, by="Officer"))
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Officer"))
    with pytest.raises(RegistryError, match="already awarded to .* use Correct"):
        run(c.bot.tick(c.reg, c.s, remove=["d1"], by="Officer"))
    run(c.bot.tick(c.reg, c.s, remove=["d2"], by="Officer"))
    assert [d.drop_id for d in c.s.drop_list] == ["d1"] and not c.s.proposals


def test_a_known_tiered_item_is_scored_by_tier_and_not_learned(council):
    c = council
    it = tiered_item(c.reg)
    assert c.reg.item_tier(it, f"{c.members[0].main.cls}:{c.members[0].main.spec}") == "S"
    res = run(c.bot.tick(c.reg, c.s, add=[{"item_id": it.id}], by="Officer"))
    (d,) = res["added"]
    assert d.tiered and d.item_name == "Robe of Tests" and d.boss == "Gatekeeper"
    assert c.reg.item_meta(str(it.id)).get("times_dropped") is None, "a tiered item is not 'learned' again"
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    assert all(x.tier == "S" and x.upgrade_value == 0.75 for x in p.result.candidates)


# ---------------------------------------------------------------- confirm, override, discard, revs

def test_confirm_writes_the_ledger_once(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Mira", by_id=2))
    assert len(ledger(c.reg)) == 1
    ctx = c.bot.loot_ctx(c.s)
    assert ls.sync_ledger(ctx, c.s) == 0, "derived again: nothing appended (import_id present)"
    with pytest.raises(RegistryError, match=r"Already recorded \(Mira"):
        run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Officer"))
    assert any(f"recorded Shard of Doom → {p.to}" in t for _, _, t in c.bot.ops.lines), "one ops line per award"


def test_an_override_lives_on_the_proposal_and_the_precedent_is_written_at_confirm(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.award_to)
    rev = ls.batch_rev(c.s)
    run(c.bot.override(c.reg, c.s, "d1", other, "tank progression", by="Jonny", by_id=3, batch_rev=rev))
    assert c.s.pending("d1").to == other and c.s.pending("d1").award_to == p.award_to
    assert not precedents(c.reg) and not c.s.overrides, "game truth first: nothing written while pending"
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, expect_to=other, by="Mira", by_id=2))
    (prec,) = precedents(c.reg)
    assert prec["human"] == other and prec["bot"] == p.award_to and prec["reason"] == "tank progression" and prec["by"] == "Jonny"
    (row,) = ledger(c.reg)
    assert row["source"] == "override" and row["bot_pick"] == p.award_to and row["by"] == "Mira"


def test_override_rules(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    a, b = [char(m) for m in c.members if char(m) != p.award_to][:2]
    with pytest.raises(RegistryError, match="Give a reason"):
        run(c.bot.override(c.reg, c.s, "d1", a, " ", by="Jonny"))
    run(c.bot.override(c.reg, c.s, "d1", a, "needs it", by="Jonny"))
    with pytest.raises(LootChanged, match=f"Jonny already set this to {a}"):
        run(c.bot.override(c.reg, c.s, "d1", b, "no, this one", by="Mira"))
    run(c.bot.override(c.reg, c.s, "d1", b, "no, this one", by="Mira", replace=True))
    assert c.s.pending("d1").to == b
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Mira"))
    with pytest.raises(RegistryError, match="use Correct"):
        run(c.bot.override(c.reg, c.s, "d1", a, "again", by="Jonny"))


def test_override_on_an_undistributed_drop_and_to_someone_off_the_roster(council):
    c = council
    tick_one(c)
    run(c.bot.override(c.reg, c.s, "d1", "Randompug", "pug tank carried us", by="Jonny"))
    p = c.s.pending("d1")
    assert p.to == "Randompug" and p.result.source.startswith("deterministic")
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Jonny"))
    (row,) = ledger(c.reg)
    assert row["raider"] == "Randompug" and row["discord_id"] is None and row["total_weight"] == 0.5, "a sub by name, untiered weight"


def test_discard_drops_pending_overrides_and_keeps_the_ticks(council):
    c = council
    tick_one(c, copies=2)
    out = run(c.bot.distribute(c.reg, c.s, by="Officer"))
    other = next(char(m) for m in c.members if char(m) != out["proposals"][0].award_to)
    run(c.bot.override(c.reg, c.s, "d1", other, "because", by="Jonny", rejudge=False))
    with pytest.raises(LootChanged, match="overrode"):
        run(c.bot.discard(c.reg, c.s, batch_rev=out["batch_rev"], by="Mira"))
    line = run(c.bot.discard(c.reg, c.s, batch_rev=ls.batch_rev(c.s), by="Mira"))
    assert "1 pending override(s) dropped" in line and "nothing was recorded" in line
    assert not c.s.proposals and len(c.s.drop_list) == 2 and len(c.s.undistributed()) == 2
    assert not precedents(c.reg) and not ledger(c.reg), "no orphan precedents"


def test_batch_rev_survives_a_confirm_of_the_same_pick_but_not_an_override(council):
    c = council
    tick_one(c, copies=3)
    out = run(c.bot.distribute(c.reg, c.s, by="Officer"))
    rev = out["batch_rev"]
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Mira"))  # another officer, per item, as shown
    assert ls.batch_rev(c.s) == rev, "confirming the pick as shown doesn't move the rev"
    p2 = c.s.pending("d2")
    other = next(char(m) for m in c.members if char(m) != p2.to)
    run(c.bot.override(c.reg, c.s, "d2", other, "reasons", by="Jonny", rejudge=False))
    with pytest.raises(LootChanged, match=r"The batch changed \(Jonny overrode the Shard of Doom"):
        run(c.bot.confirm(c.reg, c.s, batch_rev=rev, by="Mira"))
    run(c.bot.confirm(c.reg, c.s, batch_rev=ls.batch_rev(c.s), by="Mira"))
    assert len(ledger(c.reg)) == 3


def test_confirm_carries_expect_to(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    with pytest.raises(LootChanged, match=f"now goes to {p.to}"):
        run(c.bot.confirm(c.reg, c.s, only={"d1"}, expect_to="Somebody Else", by="Mira"))
    assert not ledger(c.reg)


def test_two_officers_confirm_one_drop_once(council):
    c = council
    tick_one(c)
    run(c.bot.distribute(c.reg, c.s, by="Officer"))

    async def both():
        return await asyncio.gather(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Mira"), c.bot.confirm(c.reg, c.s, only={"d1"}, by="Jonny"), return_exceptions=True)
    a, b = run(both())
    assert isinstance(b, RegistryError) and "Already recorded (Mira" in str(b) and not isinstance(a, Exception)
    assert len(ledger(c.reg)) == 1 and len([x for x in c.s.awards if not x.void]) == 1


def test_tick_and_distribute_carry_drops_rev(council):
    c = council
    tick_one(c)
    rev = ls.drops_rev(c.s)
    run(c.bot.tick(c.reg, c.s, add=[{"name": "Second Thing"}], by="companion:Scribe", source="companion"))
    with pytest.raises(LootChanged, match="The drops changed"):
        run(c.bot.distribute(c.reg, c.s, by="Officer", drops_rev=rev))
    out = run(c.bot.distribute(c.reg, c.s, by="Officer", drops_rev=ls.drops_rev(c.s)))
    assert len(out["proposals"]) == 2


def test_distribute_appends_a_batch_and_counts_pending_picks_as_provisional(council):
    c = council
    tick_one(c, boss="Gatekeeper")
    first = run(c.bot.distribute(c.reg, c.s, by="Officer"))
    assert first["batch"] == 1
    tick_one(c, name="Other Thing", boss="Second Boss")
    second = run(c.bot.distribute(c.reg, c.s, by="Officer"))
    assert second["batch"] == 2 and [p.drop_id for p in second["proposals"]] == ["d2"]
    assert [b["n"] for b in c.s.batches] == [1, 2] and c.s.pending("d1").batch == 1
    assert c.s.pending("d2").to != c.s.pending("d1").to, "the earlier pending pick counted as a provisional award"
    with pytest.raises(RegistryError, match="Nothing to distribute"):
        run(c.bot.distribute(c.reg, c.s, by="Officer"))


# ---------------------------------------------------------------- snapshot distribute vs concurrent writes

def test_distribute_judges_a_snapshot_while_a_confirm_lands(reg, rs):
    ev, ms = real_run(reg, rs)
    prov = SlowProvider()
    bot = LootBot(reg, rs, provider=prov)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    c = types.SimpleNamespace(bot=bot, reg=reg, rs=rs, ev=ev, s=s, members=ms)

    async def go():
        await bot.tick(reg, s, add=[{"name": "First"}], by="Officer")
        await bot.set_scores_only(reg, s, True, by="Officer")
        await bot.distribute(reg, s, by="Officer")  # d1: by score (no model)
        await bot.set_scores_only(reg, s, False, by="Officer")
        await bot.tick(reg, s, add=[{"name": "Second"}], by="Officer")
        task = asyncio.create_task(bot.distribute(reg, s, by="Officer"))  # d2 through the slow model
        while not prov.entered.is_set():
            await asyncio.sleep(0.01)
        with pytest.raises(LootBusy, match=r"Distributing already \(Officer"):
            await bot.distribute(reg, s, by="Mira")
        assert s.distributing and s.distributing["drops"] == ["d2"]
        await bot.confirm(reg, s, only={"d1"}, by="Mira")  # the lock is free while the model thinks
        prov.go.set()
        return await task
    out = run(go())
    assert [p.drop_id for p in out["proposals"]] == ["d2"] and not out["stale"], "the confirmed pick was already provisional: same table"
    assert s.awarded("d1") and s.pending("d2") and s.distributing is None
    assert len(ledger(reg)) == 1 and c.s is s


def test_a_write_that_moves_the_table_makes_the_judged_drop_stale(reg, rs):
    ev, ms = real_run(reg, rs)
    prov = SlowProvider()
    bot = LootBot(reg, rs, provider=prov)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))

    async def go():
        await bot.tick(reg, s, add=[{"name": "First"}], by="Officer")
        await bot.set_scores_only(reg, s, True, by="Officer")
        await bot.distribute(reg, s, by="Officer")
        await bot.set_scores_only(reg, s, False, by="Officer")
        await bot.tick(reg, s, add=[{"name": "Second"}], by="Officer")
        task = asyncio.create_task(bot.distribute(reg, s, by="Officer"))
        while not prov.entered.is_set():
            await asyncio.sleep(0.01)
        other = next(m.main.label for m in ms if m.main.label != s.pending("d1").to)
        await bot.override(reg, s, "d1", other, "moves the table", by="Jonny", rejudge=False)
        prov.go.set()
        return await task
    out = run(go())
    assert out["stale"] == ["d2"] and not out["proposals"] and s.pending("d2") is None, "never merged into a table that moved"
    assert s.undistributed()[0].drop_id == "d2" and "distribute again" in out["line"]


# ---------------------------------------------------------------- restart resume

def test_an_interrupted_distribute_is_reported_after_a_restart_never_rerun(council):
    c = council
    tick_one(c)
    c.s.distributing = {"by": "Officer", "by_id": 1, "since": ls.now_iso(), "drops": ["d1"]}
    c.s.save("crash mid-distribute")
    loaded = ls.load_sessions(c.reg.store)
    bot = LootBot(c.reg, c.rs)
    bot.events = loaded
    out = run(bot.loot_restore())
    s = bot.events[(GUILD, c.ev.key)]
    assert s.distributing is None and s.interrupted["drops"] == ["d1"] and s.failures == {"d1": 1}
    assert any("Distribute was interrupted at" in ln for ln in out) and not s.proposals, "nothing judged on its own"
    assert bot.event_for(s.raid_thread_id) is s
    s.distributing = {"by": "Officer", "since": ls.now_iso(), "drops": ["d1"]}
    run(bot.loot_restore())
    assert run(bot.distribute(c.reg, s, by="Officer"))["scores_only"], "the same drops interrupted twice: scores only"


def test_a_session_survives_a_restart_with_its_pending_state(council):
    c = council
    tick_one(c, copies=2)
    run(c.bot.distribute(c.reg, c.s, by="Officer"))
    other = next(char(m) for m in c.members if char(m) != c.s.pending("d1").to)
    run(c.bot.override(c.reg, c.s, "d1", other, "pending across restarts", by="Jonny", rejudge=False))
    loaded = ls.load_sessions(c.reg.store)[(GUILD, c.ev.key)]
    assert loaded.pending("d1").override.reason == "pending across restarts" and loaded.pending("d2") is not None
    assert ls.batch_rev(loaded) == ls.batch_rev(c.s) and ls.drops_rev(loaded) == ls.drops_rev(c.s)


def test_a_crash_between_session_and_ledger_is_repaired_on_ready(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    c.s.awards.append(ls.make_award(c.bot.loot_ctx(c.s), c.s, c.s.drop("d1"), p.to, c.bot.loot_roster(c.s), source="bot", by="Officer"))
    c.s.proposals = []
    c.s.save("session written, ledger not")
    assert not ledger(c.reg)
    bot = LootBot(c.reg, c.rs)
    bot.events = ls.load_sessions(c.reg.store)
    run(bot.loot_restore())
    assert [r["import_id"] for r in ledger(c.reg)] == [f"{c.ev.key}-d1"]


# ---------------------------------------------------------------- reasons and corrections

def test_set_reason_is_idempotent_and_a_replacement_supersedes(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.to)
    run(c.bot.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "x1"}, Companion("Scribe", "c", "", "peer")))
    assert c.s.pending_reasons and c.s.pending_reasons[0]["recipient"] == other and not precedents(c.reg)
    run(c.bot.set_reason(c.reg, c.s, "d1", "he needed it more", by="Mira", by_id=2))
    with pytest.raises(LootChanged, match="Mira already gave a reason at"):
        run(c.bot.set_reason(c.reg, c.s, "d1", "second thoughts", by="Jonny"))
    run(c.bot.set_reason(c.reg, c.s, "d1", "second thoughts", by="Jonny", replace=True))
    rows = precedents(c.reg)
    assert [r["reason"] for r in rows] == ["he needed it more", "second thoughts"]
    ctx = RegistryLootContext(c.reg, c.reg.store, None)
    active = [r for r in ctx.precedents if r.get("status", "active") == "active"]
    assert [r["reason"] for r in active] == ["second thoughts"], "the later reason supersedes (append-only)"
    assert not c.s.pending_reasons


def test_a_reason_can_be_given_days_later_after_the_council_ended(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.to)
    run(c.bot.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "x2"}, scribe()))
    text = run(c.bot.end_run(c.reg, c.rs, c.ev, by="Mira"))
    assert "1 award(s) still waiting for a reason" in text
    (need,) = c.bot.loot_reasons_pending(c.reg)
    assert need["drop_id"] == "d1" and need["recipient"] == other and need["run"] == c.ev.key
    run(c.bot.set_reason(c.reg, c.ev.key, "d1", "decided after the raid", by="Mira"))
    assert precedents(c.reg)[-1]["reason"] == "decided after the raid" and not c.bot.loot_reasons_pending(c.reg)
    with pytest.raises(LootChanged, match="already gave a reason"):
        run(c.bot.set_reason(c.reg, c.ev.key, "d1", "again", by="Jonny"))


def test_correct_voids_the_row_and_records_anew_without_a_precedent(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Mira"))
    other = next(char(m) for m in c.members if char(m) != p.to)
    with pytest.raises(RegistryError, match="Give a reason"):
        run(c.bot.correct(c.reg, c.s, "d1", other, "", by="Mira"))
    line = run(c.bot.correct(c.reg, c.s, "d1", other, "the master looter misclicked", by="Mira"))
    assert f"was {p.to}, now **{other}**" in line
    rows = ledger(c.reg)
    assert [r.get("kind") for r in rows] == [None, "void", None] and rows[1]["import_id"] == f"{c.ev.key}-d1" and rows[2]["import_id"] == f"{c.ev.key}-d1-c1"
    ctx = RegistryLootContext(c.reg, c.reg.store, None)
    assert [a.raider for a in ctx.ledger] == [other], "the voided award no longer counts"
    assert not precedents(c.reg) and c.s.awarded("d1").raider == other


# ---------------------------------------------------------------- the companion through the verbs

def scribe():
    return Companion("Scribe", "oibot-companion/0.1", "", "peer")


def test_companion_match_confirms_the_effective_recipient(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.to)
    run(c.bot.override(c.reg, c.s, "d1", other, "typed reason", by="Jonny", rejudge=False))
    out = run(c.bot.handle_feed_event({"type": "loot", "recipient": other.upper(), "item_name": "shard of doom", "idem": "m1"}, scribe()))
    assert out["result"] == "confirmed" and out["session"] == f"{GUILD}:{c.ev.key}"
    (row,) = ledger(c.reg)
    assert row["raider"] == other and row["by"] == "companion:Scribe" and row["source"] == "override"
    assert precedents(c.reg)[0]["reason"] == "typed reason", "a pending override confirmed in game keeps its typed reason"


def test_companion_mismatch_records_at_once_with_the_reason_pending(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.to)
    out = run(c.bot.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "m2"}, scribe()))
    assert out["result"] == "recorded_reason_pending"
    (row,) = ledger(c.reg)
    assert row["raider"] == other and row["source"] == "companion" and row["bot_pick"] == p.to
    assert not precedents(c.reg) and not c.s.proposals
    assert c.s.pending_reasons == [{**c.s.pending_reasons[0], "drop_id": "d1", "recipient": other, "bot": p.to}]
    out = run(c.bot.handle_feed_event({"type": "loot", "recipient": p.to, "item_name": "Shard of Doom", "idem": "m3"}, scribe()))
    assert out["result"] == "conflict" and c.s.conflicts[0]["in_game"] == p.to and len(ledger(c.reg)) == 1, "recorded earlier to someone else: Correct?, no write"


def test_companion_unknown_item_drop_then_award_without_proposal(council):
    c = council
    out = run(c.bot.handle_feed_event({"type": "drop", "boss": "Gatekeeper", "items": [{"id": 123456, "name": "Mystery Blade"}], "idem": "d1"}, scribe()))
    assert out["ticked"] == 1 and out["learned"] == 1
    assert (c.reg.store.root / GUILD / "items" / "123456.yaml").exists()
    who = char(c.members[3])
    out = run(c.bot.handle_feed_event({"type": "loot", "recipient": who, "item_id": 123456, "item_name": "Mystery Blade", "idem": "l1"}, scribe()))
    assert out["result"] == "manual"
    (row,) = ledger(c.reg)
    assert row["source"] == "manual" and row["item_id"] == 123456 and row["item_name"] == "Mystery Blade" and row["tiered"] is False
    assert row["discord_id"] == c.members[3].discord_id


def test_companion_award_of_an_untracked_item_to_a_sub(council):
    c = council
    out = run(c.bot.handle_feed_event({"type": "loot", "recipient": "Randompug", "item_name": "Never Ticked", "idem": "l2"}, scribe()))
    assert out["result"] == "manual" and c.s.drop_list[0].item_name == "Never Ticked"
    (row,) = ledger(c.reg)
    assert row["raider"] == "Randompug" and row["discord_id"] is None, "a sub stored by name, no member"


def test_companion_resends_are_acked_not_applied_twice(council):
    c = council
    ev = {"type": "drop", "boss": "Gatekeeper", "items": [{"name": "Twice"}], "idem": "same"}
    run(c.bot.handle_feed_event(ev, scribe()))
    out = run(c.bot.handle_feed_event(ev, Companion("Scribe", "c", "", "another-peer")))  # a reconnect: a new connection
    assert out == {"dup": True, "session": f"{GUILD}:{c.ev.key}"} and len(c.s.drop_list) == 1
    assert "same" in ls.load_sessions(c.reg.store)[(GUILD, c.ev.key)].seen_idem, "kept on the session (survives a restart)"


def test_companion_with_no_live_run_is_held_then_replayed(reg, rs):
    ev, ms = real_run(reg, rs)
    bot = LootBot(reg, rs)
    out = run(bot.handle_feed_event({"type": "drop", "boss": "Gatekeeper", "items": [{"name": "Early"}], "idem": "e1"}, scribe()))
    assert out["held"] and bot.feed_waiting(reg)["count"] == 1
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    assert [d.item_name for d in s.drop_list] == ["Early"] and bot.feed_waiting(reg) is None


def test_a_test_council_takes_drops_only_from_a_puppets_companion_and_writes_nothing(reg, rs):
    start = (reg.now_local() + timedelta(days=3, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t", cutoffs={"soft": 2, "hard": 1.5, "confirm": 1})
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    bot = LootBot(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    assert s.test
    out = run(bot.handle_feed_event({"type": "drop", "items": [{"name": "Rehearsal Item"}], "idem": "t1"}, scribe()))
    assert "rehearsal takes drops from a puppet's or the tester's companion only" in out["error"]
    puppet = Companion(ms[0].main.name, "c", "", "p2")
    assert run(bot.handle_feed_event({"type": "drop", "items": [{"name": "Rehearsal Item"}], "idem": "t2"}, puppet))["ticked"] == 1
    run(bot.distribute(reg, s, by="Officer"))
    run(bot.confirm(reg, s, by="Officer"))
    assert s.awards and not ledger(reg) and not precedents(reg)
    assert not (reg.store.root / GUILD / "items").exists(), "a rehearsal learns nothing either"


def test_routing_between_two_live_councils_by_seated_character(reg, rs):
    ev1, ms = real_run(reg, rs, n=5, days=3)
    start = (reg.now_local() + timedelta(days=4, hours=3)).replace(second=0, microsecond=0)
    ev2 = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t")
    other = reg.test_members()[5:10]
    join(reg, rs, ev2, other)
    lock_with_board(reg, rs, ev2, [m.display_name for m in other])
    bot = LootBot(reg, rs)
    s1, _ = run(bot.open_loot(reg, rs, ev1, by="Officer"))
    s2, _ = run(bot.open_loot(reg, rs, ev2, by="Officer"))
    assert bot.active_raid(Companion(other[0].main.name, "c", "", "p")) is s2
    session, why = bot.loot_route(Companion("Nobody", "c", "", "p"))
    assert session is None and "rostered on none of them" in why
    out = run(bot.handle_feed_event({"type": "drop", "items": [{"name": "X"}], "idem": "r1"}, Companion("Nobody", "c", "", "p")))
    assert "error" in out and any(lvl == "warn" for _, lvl, _ in bot.ops.lines)


def test_auto_distribute_after_the_last_drop_of_a_boss(council, monkeypatch):
    c = council
    c.bot.AUTO_DISTRIBUTE_S = 0.05

    async def go():
        await c.bot.handle_feed_event({"type": "drop", "boss": "Gatekeeper", "items": [{"name": "A"}], "idem": "a1"}, scribe())
        await c.bot.handle_feed_event({"type": "drop", "boss": "Gatekeeper", "items": [{"name": "B"}], "idem": "a2"}, scribe())
        await asyncio.sleep(0.2)
        await c.bot.loot_settle()
    run(go())
    assert {p.drop_id for p in c.s.proposals} == {"d1", "d2"} and len(c.s.batches) == 1, "one batch for the boss (debounced)"
    c.reg.set_raid_overrides("barrow_deeps", {"loot_auto_distribute": False}, "t")

    async def off():
        await c.bot.handle_feed_event({"type": "drop", "boss": "Second", "items": [{"name": "C"}], "idem": "a3"}, scribe())
        await asyncio.sleep(0.15)
        await c.bot.loot_settle()
    run(off())
    assert c.s.pending("d3") is None, "the per-raid switch off: no auto-distribute"


# ---------------------------------------------------------------- roster changes, end, scheduler

def test_roster_changes_reach_the_council_live(council):
    c = council

    async def go():
        c.bot.loop = asyncio.get_running_loop()
        c.bot.attach_loot_listeners()
        gone = c.members[0]
        rc.free_seat(c.reg, c.rs, c.ev, gone.discord_id, "callout")
        for _ in range(5):
            await asyncio.sleep(0)
        await c.bot.loot_settle()
        return gone
    gone = run(go())
    try:
        assert [p.discord_id for p in c.s.departed] == [gone.discord_id], "left the roster: still a candidate until removed"
        assert gone.main.label in [p.character for p in c.bot.loot_roster(c.s)]
        assert any("left the roster — still a candidate?" in ln for _, ln in c.bot.posts)
        run(c.bot.remove_candidate(c.reg, c.s, gone.discord_id, by="Officer"))
        assert gone.main.label not in [p.character for p in c.bot.loot_roster(c.s)]
    finally:
        rc.ROSTER_LISTENERS[:] = [f for f in rc.ROSTER_LISTENERS if getattr(f, "__name__", "") != "loot_on_roster_change"]


def test_end_run_archives_the_council_and_keeps_what_was_pending(council):
    c = council
    tick_one(c, copies=3)
    run(c.bot.distribute(c.reg, c.s, by="Officer", only={"d1", "d2"}))
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Mira"))
    assert "1 award(s), 0 override(s), 1 proposal(s) never confirmed, 1 drop(s) never distributed" in c.bot.end_consequence(c.reg, c.rs, c.ev)
    text = run(c.bot.end_run(c.reg, c.rs, c.ev, by="Mira", by_id=2))
    assert "1 proposal(s) never confirmed" in text and "1 drop(s) never distributed" in text
    assert c.rs.events[c.ev.key].state == "done" and not c.bot.events and c.bot.event_for(c.s.raid_thread_id) is None
    assert not (c.reg.store.root / GUILD / "events" / f"{c.ev.key}.json").exists()
    (arch,) = (c.reg.store.root / GUILD / "events" / "archive").glob(f"{c.ev.key}-*.json")
    body = json.loads(arch.read_text())
    assert body["summary"]["never_confirmed"][0]["drop_id"] == "d2" and body["summary"]["undistributed"][0]["drop_id"] == "d3"
    assert ("ended", text) in c.bot.posts and any("ended" in t for _, _, t in c.bot.ops.lines)


def test_the_scheduler_ends_a_council_only_past_close_idle_and_with_nothing_pending(council):
    c = council
    tick_one(c)
    after = rc.run_close_at(c.reg, c.ev) + timedelta(minutes=1)
    assert not run(c.bot.loot_may_close(c.reg, c.rs, c.ev, after)), "pending (a ticked drop): never auto-ended"
    run(c.bot.distribute(c.reg, c.s, by="Officer"))
    run(c.bot.confirm(c.reg, c.s, by="Officer"))
    assert not run(c.bot.loot_may_close(c.reg, c.rs, c.ev, after)), "used in the last 30 minutes"
    idle = datetime.now(timezone.utc) + timedelta(minutes=31)
    late = max(after, idle)
    assert not run(c.bot.loot_may_close(c.reg, c.rs, c.ev, late)) and ("closing", "") in c.bot.posts, "the one notice first"
    assert c.bot.loot_closing_state(c.reg, c.rs, c.ev, late)["state"] == "closing"
    assert run(c.bot.loot_may_close(c.reg, c.rs, c.ev, late + timedelta(minutes=16)))
    assert c.rs.events[c.ev.key].state == "done" and not c.bot.events


# ---------------------------------------------------------------- listeners, migration, data pieces

def test_every_save_tells_loot_listeners(council):
    heard = []
    ls.LOOT_LISTENERS.append(lambda g, sid, rk: heard.append((g, rk)))
    try:
        tick_one(council)
    finally:
        ls.LOOT_LISTENERS.pop()
    assert heard and heard[-1] == (GUILD, council.ev.key)


def test_old_thread_keyed_session_files_migrate_once(reg, rs):
    ev, _ = real_run(reg, rs)
    old = {"id": ev.key, "channel_id": 7011, "instance": "barrow_deeps", "date": "2026-12-09", "guild": GUILD, "origin": "raid", "state": "raid",
           "signups": [], "raid_thread_id": 7011, "drops": {"Gatekeeper": [32262, 32263]}, "distributed": [32262],
           "awards": [{"raider": "Ash", "item_id": 32262, "tier": "S", "total_weight": 1.0, "offspec": False, "received": "2026-12-09", "instance": "Barrow Deeps", "boss": "Gatekeeper"}],
           "proposals": [{"item_id": 32263, "item_name": "Glaive", "boss": "Gatekeeper", "award_to": "Fen", "source": "override", "reason": "tank",
                          "result": {"item_id": 32263, "item_name": "Glaive", "boss": "Gatekeeper", "candidates": [],
                                     "recommendation": {"primary": "Kes", "justification": "x", "close_call": False, "deviates_from_score": False}, "source": "deterministic"}}]}
    mock = {"id": "BLA-0915", "channel_id": 4242, "instance": "black_temple", "date": "2026-09-15", "guild": GUILD, "origin": "mock", "state": "raid", "signups": []}
    d = reg.store.root / GUILD / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "7011.json").write_text(json.dumps(old))
    (d / "4242.json").write_text(json.dumps(mock))
    lines = ls.migrate_sessions(reg.store)
    assert len(lines) == 2 and not (d / "7011.json").exists() and (d / f"{ev.key}.json").exists() and (d / "mock-4242.json").exists()
    assert ls.migrate_sessions(reg.store) == [], "idempotent"
    s = ls.load_sessions(reg.store)[(GUILD, ev.key)]
    assert [(x.drop_id, x.item_id) for x in s.drop_list] == [("d1", 32262), ("d2", 32263)]
    assert s.awards[0].drop_id == "d1" and s.pending("d2").override.to == "Fen" and s.pending("d2").award_to == "Kes"
    m = ls.load_sessions(reg.store)[(GUILD, "mock-4242")]
    assert m.label == "BLA-0915" and m.origin == "mock"


def test_registry_items_tiers_and_loot_cfg(reg):
    assert reg.items() == {} or all(k.isdigit() for k in reg.items()), "the forever profile has no tables"
    assert reg.loot_cfg()["untiered_weight"] == 0.5
    reg.config.loot = {"untiered_weight": 0.3, "rank_factor": {"trial": 0.9}}
    cfg = reg.loot_cfg()
    assert cfg["untiered_weight"] == 0.3 and cfg["rank_factor"]["trial"] == 0.9 and cfg["rank_factor"]["core"] == 1.0, "merged one level deep"
    it = reg.learn_item(None, "Blade of Names", boss="Gatekeeper", run="r1")
    assert it.id == 0 and not it.tiers and reg.item(name="blade of names") is it or reg.item(name="blade of names").name == "Blade of Names"
    assert reg.item_meta(item_key(0, "Blade of Names"))["times_dropped"] == 1
    reg.learn_item(None, "Blade of Names")
    assert reg.item_meta(item_key(0, "Blade of Names"))["times_dropped"] == 2
    tiered = tiered_item(reg, 777, "Tiered", {"Priest:*": "S+"})
    assert reg.item_tier(tiered, "Priest:Holy") == "S+" and reg.item_tier(tiered, "Warrior:Arms") is None


def test_unknown_attendance_is_neutral_and_known_attendance_counts(profile):
    from oibot_gm.profiles import Item

    item = Item(id=0, name="Thing", slot="unknown", type="unknown", boss="?", tiers={})
    cls, spec = next((c, s) for c, specs in profile.classes.items() for s in specs)
    new = Player(signup_name="New", pos=1, status="signed", cls=cls, spec=spec, role="melee", character="New")
    vet = Player(signup_name="Vet", pos=2, status="signed", cls=cls, spec=spec, role="melee", character="Vet", attended=3, attendance_total=4)
    cands = {c.character: c for c in scoring.candidates(profile, item, [new, vet], [], {}, date(2026, 12, 9))}
    assert cands["New"].factors["attendance"] == 1.0 and cands["New"].attendance_str == "n/a" and not cands["New"].attendance_known
    assert cands["Vet"].factors["attendance"] == 0.875 and cands["Vet"].attendance_known


def test_feed_presence_has_three_states():
    from oibot_gm.feed import QUIET_AFTER_S, FeedServer

    fs = FeedServer(lambda ev, c: None, "t")
    a, b, gone = Companion("A", "c", "demo", "p1"), Companion("B", "c", "demo", "p2"), Companion("C", "c", "demo", "p3")
    b.last_seen = datetime.now(timezone.utc) - timedelta(seconds=QUIET_AFTER_S + 5)
    gone.disconnected_at = datetime.now(timezone.utc)
    fs.companions = {"p1": a, "p2": b}
    fs.gone = {("C", "demo"): gone}
    assert {r["character"]: r["state"] for r in fs.presence("demo")} == {"A": "live", "B": "quiet", "C": "disconnected"}


def test_mock_sessions_still_load_and_run_on_the_fixtures(reg, rs, store):
    """/mock is kept until D11's retirement: a mock session (id mock-<channel>) runs the same verbs on the shadow
    fixtures' context and never reads a real guild's."""
    from oibot_gm import discord_bot
    from oibot_gm.lootctx import FixtureLootContext

    ROOT = Path(__file__).resolve().parents[1]
    gctx = discord_bot.GuildContext(ROOT / "fixtures" / "demo", "signup_2026-09-15.yaml")
    gctx.provider = None
    bot = LootBot(reg, rs)
    bot.ctx = gctx
    s = LootSession(id="mock-5", channel_id=5, instance="black_temple", date="2026-09-15", guild=gctx.guild_key, origin="mock", state="raid",
                    signups=gctx.seed_players).attach(store)
    from oibot_gm.roster import solver

    s.roster = solver.solve(gctx.profile, s.active_signups(), "black_temple", solver.SolveOptions(time_limit_s=5))
    bot.loot_index(s)
    assert isinstance(bot.loot_ctx(s), FixtureLootContext)
    first = next(iter(gctx.profile.items.values()))
    run(bot.tick(None, s, add=[{"item_id": first.id, "boss": first.boss}], by="Officer"))
    (p,) = run(bot.distribute(None, s, by="Officer"))["proposals"]
    run(bot.confirm(None, s, by="Officer"))
    assert s.awards[0].item_id == first.id and s.awards[0].import_id == "mock-5-d1"


def test_a_drop_nobody_can_take_is_never_recorded_to_nobody(council):
    """Everyone rostered already has the item: the pick is '(nobody)' — a per-item Confirm is refused (override or
    disenchant it), Confirm-all leaves it waiting."""
    from oibot_gm.models import LootAward

    c = council
    tick_one(c, copies=2)
    ctx = c.bot.loot_ctx(c.s)
    for p in c.bot.loot_roster(c.s):  # every candidate already has a Shard of Doom
        ctx.ledger.append(LootAward(raider=p.character, item_id=0, item_name="Shard of Doom", tier="?", total_weight=0.5, offspec=False,
                                    received=date.fromisoformat(c.s.date), instance="Barrow Deeps", boss="Gatekeeper"))
    out = run(c.bot.distribute(c.reg, c.s, by="Officer"))
    assert {p.to for p in out["proposals"]} == {"(nobody)"}
    with pytest.raises(RegistryError, match="Nobody rostered is a candidate"):
        run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Officer"))
    with pytest.raises(RegistryError, match="every pick waiting has no candidate"):
        run(c.bot.confirm(c.reg, c.s, by="Officer"))
    run(c.bot.override(c.reg, c.s, "d1", "disenchant", "nobody needs it", by="Officer", rejudge=False))
    run(c.bot.confirm(c.reg, c.s, only={"d1"}, by="Officer"))
    assert c.s.awarded("d1").raider == ls.DISENCHANT and ledger(c.reg)[0]["total_weight"] == 0


# ---------------------------------------------------------------- L1 review fixes (each fails without its fix)

def split_run(reg, rs, days=3):
    """A locked real run with TWO rosters (a split): members 0–4 on roster 1, 5–9 on roster 2."""
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    ev = rc.open_run(reg, rs, "barrow_deeps", start, by="t")
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    size = rc.run_size(reg, ev)
    names = [m.display_name for m in ms]
    ev.rosters = rc.board_rosters(reg, ev, [names[:5]], size) + rc.board_rosters(reg, ev, [names[5:]], size)
    ev.state, ev.locked_at = "locked", rc.now()
    rs.save(ev, "locked (split test board)")
    assert len(ev.all_rosters) == 2
    return ev, ms


def test_a_split_run_opens_one_council_per_roster_judging_only_its_seats(reg, rs):
    ev, ms = split_run(reg, rs)
    bot = LootBot(reg, rs)
    opened = run(bot.open_council(reg, rs, ev, by="Officer", by_id=1))
    (s1, c1), (s2, c2) = opened
    assert c1 and c2 and (s1.id, s2.id) == (f"{ev.key}-r1", f"{ev.key}-r2") and (s1.label, s2.label) == ("Roster 1", "Roster 2")
    assert {p.character for p in bot.loot_roster(s1)} == {char(m) for m in ms[:5]}
    assert {p.character for p in bot.loot_roster(s2)} == {char(m) for m in ms[5:]}, "roster 2 is judged for roster 2's drops only"
    assert [s for s, created in run(bot.open_council(reg, rs, ev, by="Mira")) if not created] == [s1, s2], "idempotent per roster"
    for s in (s1, s2):
        run(bot.tick(reg, s, add=[{"name": "Split Shard", "boss": "Gatekeeper"}], by="Officer"))
        (p,) = run(bot.distribute(reg, s, by="Officer"))["proposals"]
        assert p.to in {p.character for p in bot.loot_roster(s)}
        run(bot.confirm(reg, s, by="Officer"))
    assert sorted(r["import_id"] for r in ledger(reg)) == [f"{ev.key}-r1-d1", f"{ev.key}-r2-d1"], "two rosters' d1 never share an import id"
    text = run(bot.end_run(reg, rs, ev, by="Mira"))
    assert "Roster 1" in text and "Roster 2" in text and not bot.events and rs.events[ev.key].state == "done"


def test_a_roster_councils_thread_is_named_for_its_roster(reg, rs):
    ev, ms = split_run(reg, rs)

    class Thread:
        def __init__(self, name):
            self.name, self.id = name, 4242

    class Msg:
        async def create_thread(self, name):
            made.append(name)
            return Thread(name)

    class Chan:
        async def send(self, embed=None, **kw):
            sent.append(embed)
            return Msg()
    made, sent = [], []
    bot = LootBot(reg, rs)
    bot.officer_channel = lambda reg_, ev_: Chan()
    reg.config.roster_channel_id = 55
    s = LootSession(id=f"{ev.key}-r2", run_key=ev.key, roster_index=1, instance=ev.instance, date="2026-12-09", guild=reg.key, origin="raid")
    assert run(LootMixin.loot_open_thread(bot, reg, rs, ev, s)) == 4242
    assert made[0].startswith("loot · Roster 2 · ") and sent[0].title.startswith("⚔ Roster 2 · ")
    assert "5 rostered" in sent[0].description, "roster 2's seats only"


def test_a_reason_owed_or_a_conflict_never_holds_the_council_open(council):
    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.to)
    run(c.bot.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "h1"}, scribe()))
    run(c.bot.handle_feed_event({"type": "loot", "recipient": p.to, "item_name": "Shard of Doom", "idem": "h2"}, scribe()))
    assert c.s.pending_reasons and c.s.conflicts and not c.s.blocks_close()
    late = max(rc.run_close_at(c.reg, c.ev), datetime.now(timezone.utc)) + timedelta(minutes=31)
    assert not run(c.bot.loot_may_close(c.reg, c.rs, c.ev, late)) and ("closing", "") in c.bot.posts
    assert run(c.bot.loot_may_close(c.reg, c.rs, c.ev, late + timedelta(minutes=16))), "ended: both outlive the council"
    assert c.rs.events[c.ev.key].state == "done" and not c.bot.events
    assert [x["drop_id"] for x in c.bot.loot_reasons_pending(c.reg)] == ["d1"]
    (cf,) = c.bot.loot_conflicts_pending(c.reg)
    assert cf["session"] == c.ev.key and cf["ended"] and cf["in_game"] == p.to
    line = run(c.bot.correct(c.reg, c.ev.key, "d1", p.to, "the log was right", by="Mira"))
    assert f"was {other}, now **{p.to}**" in line and not c.bot.loot_conflicts_pending(c.reg)
    rows = ledger(c.reg)
    assert rows[-2]["kind"] == "void" and rows[-1]["import_id"] == f"{c.ev.key}-d1-c1" and rows[-1]["raider"] == p.to and rows[-1]["source"] == "correct"


def _age(session, minutes=40):
    session.last_activity = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_a_waiting_pick_holds_the_council_with_one_line_and_never_blocks_tonights_run(reg, rs):
    ev1, ms = real_run(reg, rs)
    bot = LootBot(reg, rs)
    s1, _ = run(bot.open_loot(reg, rs, ev1, by="Officer"))
    run(bot.tick(reg, s1, add=[{"name": "Forgotten", "boss": "Gatekeeper"}], by="Officer"))
    run(bot.distribute(reg, s1, by="Officer"))
    ev1.starts_at = (ev1.start - timedelta(days=10)).isoformat()  # last week's raid, its council still holding a pick
    rs.save(ev1, "t")
    _age(s1)
    assert bot.loot_closing_state(reg, rs, ev1)["state"] == "held"
    assert not run(bot.loot_may_close(reg, rs, ev1)) and not run(bot.loot_may_close(reg, rs, ev1))
    assert [k for k, _ in bot.posts].count("held") == 1, "one 'still pending' line, then it waits for an officer"
    assert rs.events[ev1.key].state == "locked", "never auto-ended with a pick pending"
    assert rc.rostered_runs(reg, rs, ms[0]) == [], "nobody is 'rostered' on a raid that is over"
    start = (reg.now_local() + timedelta(days=1, hours=3)).replace(second=0, microsecond=0)
    ev2 = rc.open_run(reg, rs, "hyjal_summit_forever", start, by="t")
    join(reg, rs, ev2, ms)
    lock_with_board(reg, rs, ev2, [m.display_name for m in ms])
    s2, _ = run(bot.open_loot(reg, rs, ev2, by="Officer"))
    assert bot.active_raid(Companion(char(ms[0]), "c", "", "p")) is s2, "tonight's council takes the companion (run window first)"
    assert [e.key for e, _ in rc.rostered_runs(reg, rs, ms[0])] == [ev2.key]


def test_the_scheduler_sends_no_fill_or_expiry_past_the_close(reg, rs, monkeypatch):
    from test_roster_p2_fixes import SchedBot

    ev, ms = real_run(reg, rs)
    ev.starts_at = (ev.start - timedelta(days=10)).isoformat()
    rs.save(ev, "t")
    bot = SchedBot(reg, rs)
    calls = []

    async def run_fill(*a, **kw):
        calls.append("fill")
        return [], {"headcount": 1, "roles": {}}

    async def held(*a, **kw):
        return False  # a council holds the run open (a pick waits)
    bot.run_fill, bot.loot_may_close = run_fill, held
    monkeypatch.setattr(rc, "expire_confirmations", lambda *a, **kw: calls.append("expire") or [])
    monkeypatch.setattr(rc, "expire_fill_asks", lambda *a, **kw: calls.append("asks") or ([], []))
    run(bot.scheduler_tick_guild(reg))
    assert calls == [] and rs.events[ev.key].state == "locked", "past the close: only the close step runs"


def test_run_fill_refuses_past_the_close(reg, rs):
    from oibot_gm.discord_raid import RaidMixin

    ev, ms = real_run(reg, rs)
    join(reg, rs, ev, reg.test_members()[10:14], status="sub")  # benched joiners: the engine's first asks
    rc.free_seat(reg, rs, ev, ms[0].discord_id, "callout")  # a gap the engine would ask for
    assert rc.fill_batch(reg, rs, ev, rc.run_team(reg, ev)), "before the close somebody would be asked"
    ev.starts_at = (ev.start - timedelta(days=10)).isoformat()
    sent = []

    async def dm(*a, **kw):
        sent.append(a)
        return True
    fake = types.SimpleNamespace(send_member_dm=dm)
    asks, _ = run(RaidMixin.run_fill(fake, reg, rs, ev, rc.run_team(reg, ev)))
    assert asks == [] and not sent and not ev.fill_asks, "never a fill DM after the raid"


def test_held_companion_events_survive_a_restart_and_expired_ones_are_announced(reg, rs):
    ev, ms = real_run(reg, rs)
    bot = LootBot(reg, rs)
    out = run(bot.handle_feed_event({"type": "drop", "boss": "Gatekeeper", "items": [{"name": "Early"}], "idem": "e1"}, scribe()))
    assert out["held"] and "open the loot council" in out["note"]
    after = LootBot(reg, rs)  # a restart: a new process, nothing in memory
    assert after.feed_waiting(reg)["count"] == 1
    s, _ = run(after.open_loot(reg, rs, ev, by="Officer"))
    assert [d.item_name for d in s.drop_list] == ["Early"] and after.feed_waiting(reg) is None
    run(after.end_run(reg, rs, ev, by="Officer"))
    run(after.handle_feed_event({"type": "loot", "recipient": char(ms[1]), "item_name": "Late Ring", "idem": "e2"}, scribe()))
    f = reg.store.root / reg.key / "loot" / "held.json"
    rows = json.loads(f.read_text())
    rows[0]["at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(timespec="seconds")
    f.write_text(json.dumps(rows))
    gone = run(after.feed_expire(reg.key))
    assert len(gone) == 1 and json.loads(f.read_text()) == []
    (warn,) = [t for _, lvl, t in after.ops.lines if lvl == "warn"]
    assert "NOT recorded" in warn and f"Late Ring → {char(ms[1])}" in warn, "never silent: what was not recorded is named"


def test_a_thread_pending_at_open_is_created_on_the_next_open_or_a_resume(reg, rs):
    ev, ms = real_run(reg, rs)
    bot = LootBot(reg, rs)
    up = {"ok": False}

    async def thread(reg_, rs_, ev_, session):
        return 7777 if up["ok"] else None
    bot.loot_open_thread = thread
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    assert s.raid_thread_id is None and any("thread pending" in t for _, _, t in bot.ops.lines)
    assert run(bot.on_resumed()) is None and s.raid_thread_id is None, "still down: nothing made"
    up["ok"] = True
    run(bot.on_resumed())
    assert s.raid_thread_id == 7777 and rs.events[ev.key].thread_id == 7777 and bot.event_for(7777) is s
    assert ls.load_sessions(reg.store)[s.key].raid_thread_id == 7777
    s.raid_thread_id = None  # and the next /raid loot does the same
    again, created = run(bot.open_loot(reg, rs, ev, by="Mira"))
    assert again is s and not created and s.raid_thread_id == 7777


def test_items_cache_reads_a_tier_written_by_another_path(reg):
    tiered_item(reg, 90002, "Cached Cloak", tiers={"role:tank": "C"})
    assert reg.item(90002).tiers == {"role:tank": "C"}
    f = reg.store.root / reg.key / "items" / "90002.yaml"
    doc = yaml.safe_load(f.read_text())
    doc["tiers"] = {"role:tank": "S", "role:healer": "A"}
    f.write_text(yaml.safe_dump(doc))  # Loot › Items / a data-repo pull: not learn_item, no cache reset
    assert reg.item(90002).tiers == {"role:tank": "S", "role:healer": "A"}
    before = reg.items()
    reg.refresh_profile()  # a guild override reload builds a new profile
    assert reg.items() is not before


class ChatBot(LootBot):
    """The /mock thread chat (discord_bot.OibotGM.mock_loot_chat) on the verbs, with a fake message."""

    async def post_proposals(self, ev, dest, prefix=None):
        self.posts.append(("proposals", prefix or ""))


class FakeMsg:
    def __init__(self, text="ok", who="Mira", uid=2):
        self.content = text
        self.author = types.SimpleNamespace(display_name=who, id=uid)
        self.guild = None
        self.replies: list[str] = []
        outer = self

        class Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False

        self.channel = types.SimpleNamespace(typing=lambda: Typing())
        self.outer = outer

    async def reply(self, text=None, **kw):
        self.replies.append(text or "")


def test_loot_chat_never_records_a_pick_added_while_the_model_reads(reg, rs, monkeypatch):
    from oibot_gm import nl
    from oibot_gm.discord_bot import OibotGM

    ev, ms = real_run(reg, rs)
    bot = ChatBot(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    run(bot.tick(reg, s, add=[{"name": "One"}], by="Officer"))
    run(bot.distribute(reg, s, by="Officer"))
    added = {}

    def parse(provider, policy, digest, text, who):  # meanwhile, auto-distribute adds a pick nobody saw
        p = s.proposals[0].model_copy(update={"drop_id": "d9", "item_name": "Unseen"})
        s.drop_list.append(s.drop_list[0].model_copy(update={"drop_id": "d9", "item_name": "Unseen"}))
        s.proposals.append(p)
        added["p"] = p
        return nl.LootFeedback(kind="confirm", reply="ok")
    monkeypatch.setattr(nl, "parse_loot_feedback", parse)
    msg = FakeMsg("confirm them")
    run(OibotGM.mock_loot_chat(bot, msg, s))  # /mock's chat; the real thread's is tests/test_loot_surface.py
    assert "The batch changed" in msg.replies[-1] and not s.awards and not ledger(reg), "nothing recorded unseen"


def test_loot_chat_never_replaces_another_officers_override(reg, rs, monkeypatch):
    from oibot_gm import nl
    from oibot_gm.discord_bot import OibotGM

    ev, ms = real_run(reg, rs)
    bot = ChatBot(reg, rs)
    s, _ = run(bot.open_loot(reg, rs, ev, by="Officer"))
    it = tiered_item(reg, 90003, "Contested Helm")
    run(bot.tick(reg, s, add=[{"item_id": it.id}], by="Officer"))
    (p,) = run(bot.distribute(reg, s, by="Officer"))["proposals"]
    others = [c.character for c in p.result.candidates if c.character != p.to]
    run(bot.override(reg, s, "d1", others[0], "Jonny's call", by="Jonny", rejudge=False))
    monkeypatch.setattr(nl, "parse_loot_feedback", lambda *a: nl.LootFeedback(kind="override", reply="", changes=[nl.LootChange(item_id=it.id, award_to=others[1], reason="Mira's call")]))
    run(OibotGM.mock_loot_chat(bot, FakeMsg("give it to them"), s))
    assert s.pending("d1").override.by == "Jonny" and s.pending("d1").override.to == others[0]
    assert any("already set this" in t for _, t in bot.posts if t), "the replace? question comes back"


def test_the_threads_buttons_give_the_reason_and_correct(council):
    from oibot_gm.discord_loot import LootCorrectModal, LootDropView, LootReasonModal

    c = council
    tick_one(c)
    (p,) = run(c.bot.distribute(c.reg, c.s, by="Officer"))["proposals"]
    other = next(char(m) for m in c.members if char(m) != p.to)
    run(c.bot.handle_feed_event({"type": "loot", "recipient": other, "item_name": "Shard of Doom", "idem": "b1"}, scribe()))
    assert ("mismatch", "d1") in c.bot.drop_posts, "the mismatch line names its drop (its buttons bind to it)"

    async def role_of(reg, user, max_age=0):
        return "officer"
    c.bot.role_of = role_of
    said = []

    class Resp:
        async def send_message(self, text=None, **kw):
            said.append(text)

        async def send_modal(self, modal):
            said.append(modal)

    itx = types.SimpleNamespace(user=types.SimpleNamespace(display_name="Mira", id=2), response=Resp())

    async def go():
        v = c.bot.loot_view(c.reg, c.s, "mismatch", "d1")
        assert isinstance(v, LootDropView) and [b.label for b in v.children] == ["Give the reason", "Mistake — correct…"]
        await v.children[0].callback(itx)
        modal = said[-1]
        assert isinstance(modal, LootReasonModal)
        modal.reason._value = "raid leader's call for the tank"
        await modal.on_submit(itx)
        assert [b.label for b in c.bot.loot_view(c.reg, c.s, "conflict", "d1").children] == ["Correct…"]
        fix = LootCorrectModal(v.t)
        fix.to._value, fix.reason._value = p.to, "misclicked"
        await fix.on_submit(itx)
    run(go())
    assert precedents(c.reg)[-1]["reason"] == "raid leader's call for the tank" and not c.s.pending_reasons
    assert c.s.awarded("d1").raider == p.to and c.s.awarded("d1").source == "correct"
