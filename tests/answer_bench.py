"""The question bench (answer tools, design §10): questions people ask the bot, mapped onto the demo guild, each with
what the answer must hold, what it must never show, the tools it should reach for and a script for the offline run
(tests/answer_fakes.ScriptedProvider). The three Ironfeather cases are the questions that failed on the old path:
"Ironfeather" names exactly three recipes in the forever profile's list (Breastplate 10790, Leggings 55073, Shoulders
10769, all Leatherworking) while "leather" is in about 200 names.

`question`, `candidates`, `must` and `never` are `str.format`-ed with the dict a case's `setup(reg, rs)` returns.
Every setup makes sure its asker's record exists. Members, characters and items here are invented; no real guild data."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Literal

import yaml

from conftest import GUILD, OFFICER_ROLE, OWNER, ROOT, TEST_MEMBERS, open_test_run, weekly

from answer_fakes import call, reply

from oibot_gm import raidcycle as rc
from oibot_gm.lootctx import RegistryLootContext
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import Registry
from oibot_gm.store import GitStore

WREN = 700_000_000_000_000_001     # the member: Wrenfield, main Thornwick (Hunter Survival)
MARROW = 700_000_000_000_000_002   # the officer: Marrowby
PELLOW = 700_000_000_000_000_003   # the second member: Pellow, main Briarwood (Priest Holy)
NEWCOMER = 700_000_000_000_000_004  # an applicant: Newbie

# asker → (uid, display name, main, class, spec, officer)
ASKERS: dict[str, tuple[int, str, str, str, str, bool]] = {
    "member": (WREN, "Wrenfield", "Thornwick", "Hunter", "Survival", False),
    "member2": (PELLOW, "Pellow", "Briarwood", "Priest", "Holy", False),
    "officer": (MARROW, "Marrowby", "Marrowby", "Warrior", "Protection", True),
    "owner": (OWNER, "Owner", "Oakhollow", "Mage", "Frost", True),
}

IRONFEATHER = ("Ironfeather Breastplate", "Ironfeather Leggings", "Ironfeather Shoulders")
LOOT_ITEM = "Robe of the Bench"
LOOT_REASON = "kept for the tank"


@dataclass(frozen=True)
class BenchCase:
    id: str
    asker: Literal["member", "member2", "officer", "owner"]
    where: Literal["private", "public"]
    question: str
    setup: Callable[[Registry, rc.RaidStore], dict[str, str]]
    candidates: tuple[str, ...] = ()
    must: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    expect_tools: tuple[str, ...] = ()
    asks_back: bool = False
    script: tuple = ()


def asker_of(case: BenchCase) -> tuple[int, bool]:
    """(uid, officer) of the case's asker."""
    uid, *_rest, officer = ASKERS[case.asker]
    return uid, officer


# ---------------------------------------------------------------- setups

def _ensure(reg: Registry, uid: int, display: str, name: str, cls: str, spec: str) -> None:
    m = reg.members.get(uid)
    if m is not None and reg.own_character(m.active(), name):
        return
    reg.add_character(uid, display, name, cls, spec, None, True)


def askers(reg: Registry, rs: rc.RaidStore | None = None) -> dict[str, str]:
    """Every asker's record (idempotent)."""
    for uid, display, name, cls, spec, _officer in ASKERS.values():
        _ensure(reg, uid, display, name, cls, spec)
    return {}


def crafters(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Thornwick knows Leatherworking 300 with Ironfeather Breastplate."""
    askers(reg)
    reg.set_professions(WREN, "Thornwick", [{"id": "leatherworking", "skill": 300, "recipes": [{"id": 10790}]}])
    return {"crafter": "Thornwick", "recipe": "Ironfeather Breastplate"}


def two_crafters(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Crafters, and Pellow's Briarwood with Leatherworking and Ironfeather Leggings."""
    out = crafters(reg, rs)
    reg.set_professions(PELLOW, "Briarwood", [{"id": "leatherworking", "skill": 300, "recipes": [{"id": 55073}]}])
    return out


def an_event(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    askers(reg)
    open_test_run(reg, rs, "barrow_deeps")
    return {"raid": reg.raid_def("barrow_deeps").get("name") or "Barrow Deeps"}


def a_schedule(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    askers(reg)
    if not reg.schedules("barrow_deeps"):
        weekly(reg, "barrow_deeps", "Tue 19:30")
    return {"raid": reg.raid_def("barrow_deeps").get("name") or "Barrow Deeps"}


def an_award(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """One award to Thornwick, read back as the newest award of the guild's ledger."""
    askers(reg)
    reg.store.append_jsonl(f"{reg.key}/ledger.jsonl", {
        "raider": "Thornwick", "discord_id": WREN, "item_id": 0, "item_name": LOOT_ITEM, "tier": "S", "total_weight": 1.0,
        "received": "2026-09-02", "instance": "bd", "boss": "Gatekeeper", "offspec": False, "import_id": "bench-d1",
        "source": "override", "bot_pick": "Briarwood"})
    newest = max(RegistryLootContext(reg, reg.store, None).ledger, key=lambda a: a.received)
    return {"item": newest.item_name or "", "character": newest.raider}


def an_award_with_reason(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    out = an_award(reg, rs)
    reg.store.append_jsonl(f"{reg.key}/precedents.jsonl", {"date": "2026-09-02", "item": out["item"], "bot": "Briarwood", "human": out["character"],
                                                          "reason": LOOT_REASON, "by": "Marrowby", "import_id": "bench-d1"})
    return out


def an_applicant(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    askers(reg)
    sid = reg.schedules("barrow_deeps")[0]["id"] if reg.schedules("barrow_deeps") else weekly(reg, "barrow_deeps", "Tue 19:30")
    reg.set_recruitment("t", note="looking for healers", questions=[{"label": "Why us?", "required": True}])
    if NEWCOMER not in reg.applicants:
        reg.apply(NEWCOMER, "Newbie", "Newname", "Mage", "Frost", None, None, None, None, schedules=[sid],
                  answers=[{"label": "Why us?", "answer": "Friends here"}])
    return {"applicant": "Newbie"}


def away_next_week(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Wrenfield is away next week, for the dentist."""
    out = crafters(reg, rs)
    day = (reg.now_local().date() + timedelta(days=7)).isoformat()
    if not any(a.reason == "dentist" for a in reg.members[WREN].absences):
        reg.add_absence(WREN, day, None, "dentist", "t")
    return out


# ---------------------------------------------------------------- the bench

BENCH: list[BenchCase] = [
    # the three questions that failed on the old path
    BenchCase("ironfeather_who_has", "member", "public", "bot who has ironfeather", crafters,
              candidates=IRONFEATHER, must=("Thornwick",), expect_tools=("recipe",),
              script=([call("recipe", name="Ironfeather Breastplate")], [reply(answer="Thornwick can make the Ironfeather Breastplate.")])),
    BenchCase("ironfeather_breastplate", "member", "public", "bot who can make ironfeather breastplate", crafters,
              candidates=("Ironfeather Breastplate",), must=("Thornwick",),
              script=([reply(answer="Thornwick can make the Ironfeather Breastplate.")],)),
    BenchCase("ironfeather_pieces", "member", "public", "Bot who can make ironfeather leather working pieces", crafters,
              candidates=IRONFEATHER, must=("Thornwick", "Ironfeather Breastplate"), expect_tools=("find",),
              script=([call("find", text="ironfeather", kinds=["recipe"])],
                      [reply(answer="Thornwick can make the Ironfeather Breastplate. Nobody has the leggings or shoulders yet.")])),
    BenchCase("ironfeather_choose", "member", "private", "who can make ironfeather?", two_crafters,
              candidates=IRONFEATHER, asks_back=True,
              script=([reply(answer="Which one?", choices=["Ironfeather Breastplate", "Ironfeather Leggings"])],)),

    # one per tool
    BenchCase("crafters", "member", "public", "who are our leatherworkers?", crafters,
              must=("Thornwick", "300"), expect_tools=("crafters",),
              script=([call("crafters", profession="leatherworking")], [reply(answer="Thornwick is our leatherworker, at 300.")])),
    BenchCase("price", "member", "public", "what does an arcanite bar go for?", askers,
              expect_tools=("price",),
              script=([call("price", name="Arcanite Bar")], [reply(answer="Here is what an Arcanite Bar goes for.")])),
    BenchCase("member", "member", "public", "what does Thornwick play?", crafters,
              must=("Hunter",), expect_tools=("member",),
              script=([call("member", name="Thornwick")], [reply(answer="Thornwick plays a Survival Hunter.")])),
    BenchCase("event", "member", "public", "when is the next event?", an_event,
              must=("{raid}",), expect_tools=("event",),
              script=([call("event", which="next")], [reply(answer="The next event is Barrow Deeps.")])),
    BenchCase("schedule", "member", "public", "when do we raid?", a_schedule,
              must=("Barrow Deeps",), expect_tools=("schedule",),
              script=([call("schedule")], [reply(answer="We raid Barrow Deeps on Tuesdays at 7:30 PM.")])),
    BenchCase("loot", "member", "public", "who got {item}?", an_award,
              must=("{character}",), expect_tools=("loot",),
              script=([call("loot", item=LOOT_ITEM)], [reply(answer=f"Thornwick got the {LOOT_ITEM}.")])),
    BenchCase("attendance", "member", "private", "what's my attendance?", askers,
              must=("not tracked yet",), expect_tools=("attendance",),
              script=([call("attendance")], [reply(answer="Your attendance is not tracked yet.")])),
    BenchCase("manual", "member", "private", "how do I mark myself away?", askers,
              expect_tools=("manual",),
              script=([call("manual", query="mark myself away")], [reply(answer="Press I'll be away on the absences card.")])),
    BenchCase("section", "officer", "private", "any applications waiting?", an_applicant,
              must=("Newbie",), expect_tools=("section",),
              script=([call("section", key="applications")], [reply(answer="Newbie has applied and waits for a decision.")])),

    # visibility: what an asker must never be shown
    BenchCase("away_member", "member2", "private", "who is away next week?", away_next_week,
              never=("dentist",),
              script=([call("section", key="absences")], [reply(answer="Only officers can see who is away.")])),
    BenchCase("away_officer_public", "officer", "public", "who is away next week?", away_next_week,
              never=("dentist",),
              script=([call("section", key="absences")], [reply(answer="Ask me in a private message for that.")])),
    BenchCase("loot_reason_member", "member", "public", "why did {character} get {item}?", an_award_with_reason,
              never=(LOOT_REASON,),
              script=([call("loot", item=LOOT_ITEM)], [reply(answer=f"Thornwick got the {LOOT_ITEM}; the council's reasons are for officers.")])),
    BenchCase("record_other_member", "member2", "public", "what's Thornwick's record?", away_next_week,
              never=("dentist",),
              script=([call("member", name="Thornwick")], [reply(answer="Thornwick plays a Survival Hunter.")])),
]


# ---------------------------------------------------------------- the probe's registry

def bench_registry(tmp: Path) -> tuple[Registry, rc.RaidStore]:
    """conftest's data_root + reg + rs, built by hand for the probe: a copy of fixtures/demo with the same minimal
    guild.yaml, committed once, on a GitStore that never pushes."""
    root = tmp / "data"
    shutil.copytree(ROOT / "fixtures" / "demo", root / GUILD)
    (root / GUILD / "guild.yaml").write_text(yaml.safe_dump({
        "key": GUILD, "name": "Demo Guild", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": OWNER,
        "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE],
    }, sort_keys=False))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"], cwd=root, check=True)
    store = GitStore(root, push=False)
    reg = Registry(store, GUILD, GameProfile.load(ROOT / "profiles" / "forever"))
    reg.seed_test_members(TEST_MEMBERS, "t")
    return reg, rc.RaidStore(store, GUILD)
