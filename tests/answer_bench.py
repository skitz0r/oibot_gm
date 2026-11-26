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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

import yaml

from conftest import GUILD, OFFICER_ROLE, OWNER, ROOT, TEST_MEMBERS, at_schedule, weekly

from answer_fakes import call, reply

from oibot_gm import auctions, discord_auctions
from oibot_gm import raidcycle as rc
from oibot_gm.armory import GuildFacts
from oibot_gm.auctions import House, Price
from oibot_gm.constants import CLOCK12
from oibot_gm.lootctx import RegistryLootContext
from oibot_gm.profiles import GameProfile
from oibot_gm.raid_views import run_times
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
LOOT_TO = "Thornwick"   # who an_award gives LOOT_ITEM to: the loot cases' scripts and their `must` both come from it
LOOT_REASON = "kept for the tank"
LOOT_BOSS = "Gatekeeper"   # the boss an_award's row names: only the loot lookup gives it (the question names item and no boss)
ARCANITE_BAR = 12360
BAR_PRICE = Price(low=489998, qty=12)  # what the bench's auction house lists Arcanite Bars at: 48g 99s 98c each


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
    tags: tuple[str, ...] = ()  # a group the probe reports together: `shorthand` = the person wrote the game's short names


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


def alchemist(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Pellow's Briarwood knows Alchemy 300 with two flasks (Flask of the Titans 9978, Flask of Supreme Power 9980)."""
    askers(reg)
    reg.set_professions(PELLOW, "Briarwood", [{"id": "alchemy", "skill": 300, "recipes": [{"id": 9978}, {"id": 9980}]}])
    return {"crafter": "Briarwood", "flask": "Flask of the Titans"}


def real_event(reg: Registry, rs: rc.RaidStore, raid: str = "barrow_deeps", days: int = 3):
    """A schedule's event a few days out (conftest.open_test_run's events are rehearsals, which the `event` lookup
    leaves out)."""
    start = (reg.now_local() + timedelta(days=days, hours=3)).replace(second=0, microsecond=0)
    return rc.open_run(reg, rs, raid, start, by="t", schedule=at_schedule(reg, start, raid))


def an_event(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """A real Barrow Deeps event (idempotent: one already open stays the one) → its raid's name and when it locks,
    in the words the `event` lookup prints (raid_views.run_times, 12-hour): the start's event line has no lock time."""
    askers(reg)
    ev = next((e for e in rs.live() if e.instance == "barrow_deeps" and not rc.rehearsal_run(reg, e)), None)
    ev = ev or real_event(reg, rs)
    _soft, lock, _confirm = run_times(reg, ev, rc.run_team(reg, ev))
    return {"raid": reg.raid_def("barrow_deeps").get("name") or "Barrow Deeps", "lock": reg.local12(lock, CLOCK12)}


def a_schedule(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    askers(reg)
    if not reg.schedules("barrow_deeps"):
        weekly(reg, "barrow_deeps", "Tue 19:30")
    nxt = rc.next_runs_of(reg, "barrow_deeps", reg.schedules("barrow_deeps")[0])[0]
    return {"raid": reg.raid_def("barrow_deeps").get("name") or "Barrow Deeps", "next": reg.local12(nxt)}


def an_award(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """One award of LOOT_ITEM to LOOT_TO, read back as the newest award of the guild's ledger."""
    askers(reg)
    reg.store.append_jsonl(f"{reg.key}/ledger.jsonl", {
        "raider": LOOT_TO, "discord_id": WREN, "item_id": 0, "item_name": LOOT_ITEM, "tier": "S", "total_weight": 1.0,
        "received": "2026-09-02", "instance": "bd", "boss": LOOT_BOSS, "offspec": False, "import_id": "bench-d1",
        "source": "override", "bot_pick": "Briarwood"})
    newest = max(RegistryLootContext(reg, reg.store, None).ledger, key=lambda a: a.received)
    return {"item": newest.item_name or "", "character": newest.raider}


def an_award_with_reason(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    out = an_award(reg, rs)
    reg.store.append_jsonl(f"{reg.key}/precedents.jsonl", {"date": "2026-09-02", "item": out["item"], "bot": "Briarwood", "human": out["character"],
                                                          "reason": LOOT_REASON, "by": "Marrowby", "import_id": "bench-d1"})
    return out


def priced_bar(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """The guild's Horde auction house lists Arcanite Bars at BAR_PRICE, read just now. Only where the bot can read
    auction houses (tests/test_answer_bench's `auction_house` fixture stands in for the host's armory keys); elsewhere
    the asker's record alone, and the price lookup says why it shows none."""
    askers(reg)
    if not auctions.available(reg):
        return {"price": auctions.money(BAR_PRICE.low)}
    if reg.config.armory is None:
        reg.set_armory_guild(GuildFacts(id=418362, slug="bench-wardens", name="Bench Wardens", faction="Horde", kind="Normal",
                                        ruleset_id=2, members=8, created=datetime(2026, 1, 5, tzinfo=timezone.utc)),
                             "Owner", by_id=OWNER)
    if reg.config.market is None:
        reg.set_market(House(id=6, name="Horde Auction House"), 2, "Owner", by_id=OWNER)
    discord_auctions._st(reg)["table"] = discord_auctions.Table(at=datetime.now(timezone.utc), house_id=6, ruleset_id=2,
                                                                prices={ARCANITE_BAR: BAR_PRICE}, modified=None)
    return {"price": auctions.money(BAR_PRICE.low)}


def an_applicant(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    askers(reg)
    sid = reg.schedules("barrow_deeps")[0]["id"] if reg.schedules("barrow_deeps") else weekly(reg, "barrow_deeps", "Tue 19:30")
    reg.set_recruitment("t", note="looking for healers", questions=[{"label": "Why us?", "required": True}])
    if NEWCOMER not in reg.applicants:
        reg.apply(NEWCOMER, "Newbie", "Newname", "Mage", "Frost", None, None, None, None, schedules=[sid],
                  answers=[{"label": "Why us?", "answer": "Friends here"}])
    return {"applicant": "Newbie"}


def blacksmith(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Pellow's Briarwood knows Blacksmithing 285."""
    askers(reg)
    reg.set_professions(PELLOW, "Briarwood", [{"id": "blacksmithing", "skill": 285, "recipes": []}])
    return {"crafter": "Briarwood (Pellow, 285)"}


def paladin_healer(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Pellow has a Holy Paladin alt, Dawnmere."""
    askers(reg)
    _ensure(reg, PELLOW, "Pellow", "Dawnmere", "Paladin", "Holy")
    return {"healer": "Dawnmere Holy Paladin healer"}


def leatherworkers(reg: Registry, rs: rc.RaidStore) -> dict[str, str]:
    """Two leatherworkers: Thornwick at 300 and Briarwood at 240, so the best one leads the lookup's list."""
    out = crafters(reg, rs)
    reg.set_professions(PELLOW, "Briarwood", [{"id": "leatherworking", "skill": 240, "recipes": []}])
    return {**out, "order": "Thornwick (Wrenfield, 300)\nBriarwood (Pellow, 240)"}


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
              # the reagent item stays a candidate beside its three recipes: after the one somebody makes, then by name
              candidates=(IRONFEATHER[0], "Ironfeather", *IRONFEATHER[1:]), expect_tools=("recipe",),
              # the start names Thornwick already (the asker's character, the matched recipe's maker): the lookup alone
              # gives the crafter's member and the reagents
              must=("Thornwick (Wrenfield, 300)", "Rugged Leather"),
              script=([call("recipe", name="Ironfeather Breastplate")], [reply(answer="Thornwick can make the Ironfeather Breastplate.")])),
    BenchCase("ironfeather_breastplate", "member", "public", "bot who can make ironfeather breastplate", crafters,
              candidates=("Ironfeather Breastplate",), must=("made by Thornwick (300)",),  # from the start's matched names
              script=([reply(answer="Thornwick can make the Ironfeather Breastplate.")],)),
    BenchCase("ironfeather_pieces", "member", "public", "Bot who can make ironfeather leather working pieces", crafters,
              # the answer is in the start's matched names (a `find` would give back the same three recipe lines), so
              # the case expects no lookup and reads its facts off the start; find_flasks is the case `find` must prove
              candidates=IRONFEATHER, must=("Ironfeather Breastplate · Leatherworking", "made by Thornwick (300)"),
              script=([reply(answer="Thornwick can make the Ironfeather Breastplate. Nobody has the leggings or shoulders yet.")],)),
    # a question naming nothing in the guild's names ("brew" matches none): the start holds no recipe, so only the
    # model's own search of one kind finds the guild's flasks and their maker
    BenchCase("find_flasks", "member", "public", "what can the guild brew?", alchemist,
              must=("Flask of the Titans", "made by Briarwood (300)"), expect_tools=("find",),
              script=([call("find", text="flask", kinds=["recipe"])],
                      [reply(answer="Briarwood can make the Flask of the Titans and the Flask of Supreme Power.")])),
    BenchCase("ironfeather_choose", "member", "private", "who can make ironfeather?", two_crafters,
              candidates=IRONFEATHER, asks_back=True,
              script=([reply(answer="Which one?", choices=["Ironfeather Breastplate", "Ironfeather Leggings"])],)),

    # one per tool
    BenchCase("crafters", "member", "public", "who are our leatherworkers?", crafters,
              must=("Thornwick (Wrenfield, 300)",), expect_tools=("crafters",),
              script=([call("crafters", profession="Leatherworking")], [reply(answer="Thornwick is our leatherworker, at 300.")])),
    BenchCase("price", "member", "public", "what does an arcanite bar go for?", priced_bar,
              must=("{price}",), expect_tools=("price",),
              script=([call("price", name="Arcanite Bar")], [reply(answer="An Arcanite Bar goes for 48g 99s 98c.")])),
    # asked by another member: the start's matched names say "Hunter Survival", only the record adds the role
    BenchCase("member", "member2", "public", "what does Thornwick play?", crafters,
              must=("Thornwick Survival Hunter ranged",), expect_tools=("member",),
              script=([call("member", name="Thornwick")], [reply(answer="Thornwick plays a Survival Hunter.")])),
    BenchCase("event", "member", "public", "when is the next event?", an_event,
              must=("lock {lock}",), expect_tools=("event",),  # the start's line has the raid and state, not the lock
              script=([call("event", which="next")], [reply(answer="The next event is Barrow Deeps.")])),
    BenchCase("schedule", "member", "public", "when do we raid?", a_schedule,
              must=("next: {next}",), expect_tools=("schedule",),  # the start names the schedule, never its nights
              script=([call("schedule")], [reply(answer="We raid Barrow Deeps on Tuesdays at 7:30 PM.")])),
    BenchCase("loot", "member", "public", "who got {item}?", an_award,
              must=(LOOT_BOSS,), expect_tools=("loot",),  # the question names the item; the award's boss is the lookup's
              script=([call("loot", item=LOOT_ITEM)], [reply(answer=f"{LOOT_TO} got the {LOOT_ITEM} from the {LOOT_BOSS}.")])),
    BenchCase("attendance", "member", "private", "what's my attendance?", askers,
              must=("not tracked yet",), expect_tools=("attendance",),
              script=([call("attendance")], [reply(answer="Your attendance is not tracked yet.")])),
    BenchCase("manual", "member", "private", "how do I mark myself away?", askers,
              must=("I'll be away",), expect_tools=("manual",),  # the button's name, from the manual part's text
              script=([call("manual", query="mark myself away")], [reply(answer="Press I'll be away on the absences card.")])),
    BenchCase("section", "officer", "private", "any applications waiting?", an_applicant,
              must=("Newbie",), expect_tools=("section",),
              script=([call("section", key="applications")], [reply(answer="Newbie has applied and waits for a decision.")])),

    # the game's shorthand (the owner, 2026-11-25: no alias table): the resolver finds nothing in "bs", "pally" or "lw";
    # the model maps them to the profile's own names the lookups take as an enum. With the scripted model these prove
    # the lookups deliver; with the real model (the probe's `shorthand` group) that no alias table is needed
    BenchCase("bs_in_the_guild", "member", "public", "bot who are bs in the guild", blacksmith,
              must=("{crafter}",), expect_tools=("crafters",), tags=("shorthand",),
              script=([call("crafters", profession="Blacksmithing")], [reply(answer="Briarwood is our blacksmith, at 285.")])),
    BenchCase("pally_healers", "member", "public", "any pally healers?", paladin_healer,
              must=("{healer}",), expect_tools=("member",), tags=("shorthand",),
              script=([call("member", cls="Paladin", role="healer")], [reply(answer="Pellow plays Dawnmere, a Holy Paladin.")])),
    BenchCase("lw_who", "member", "public", "who's our best lw", leatherworkers,
              must=("{order}",), expect_tools=("crafters",), tags=("shorthand",),
              script=([call("crafters", profession="Leatherworking")], [reply(answer="Thornwick is our best leatherworker, at 300.")])),

    # visibility: what an asker must never be shown
    BenchCase("away_member", "member2", "private", "who is away next week?", away_next_week,
              never=("dentist",),
              script=([call("section", key="absences")], [reply(answer="Only officers can see who is away.")])),
    BenchCase("away_officer_public", "officer", "public", "who is away next week?", away_next_week,
              never=("dentist",),
              script=([call("section", key="absences")], [reply(answer="Ask me in a private message for that.")])),
    BenchCase("loot_reason_member", "member", "public", "why did {character} get {item}?", an_award_with_reason,
              never=(LOOT_REASON,),
              script=([call("loot", item=LOOT_ITEM)], [reply(answer=f"{LOOT_TO} got the {LOOT_ITEM}; the council's reasons are for officers.")])),
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
