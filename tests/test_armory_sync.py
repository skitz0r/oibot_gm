"""What a check against the armory does (armory link, spec §5.3): the hourly conditional check of each linked
character, the three-hourly roster read, Sync now, and the scheduler's hook. A fake Armory stands in for Blizzard
(test_armory_link.FakeArmory); names are invented."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from conftest import OWNER
from test_armory_link import (LATER, ORLA, STAMP, TESSA, WARDENS, WREN, Clock, FakeArmory, FakeBot, char, commits, facts,  # noqa: F401
                              last_commit, link_bram, link_guild, people, run)
from test_armory_link import bot, clock, fake  # noqa: F401 — the fixtures

from oibot_gm import discord_armory
from oibot_gm.armory import ArmoryAccess, ArmoryDown, ArmoryShape, GuildFacts
from oibot_gm.registry import Registry


def linked(bot, reg):
    people(reg)
    link_guild(reg)
    return link_bram(bot, reg)


# ------------------------------------------------------------------ one character


def test_unchanged_writes_no_commit(reg, fake, bot, clock):
    linked(bot, reg)
    before, rev = commits(reg), reg.characters_rev(reg.members[TESSA])
    clock.advance(hours=1)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "unchanged"
    assert ("character", "bram-kettle", STAMP) in fake.calls  # a conditional request on the stored Last-Modified
    assert commits(reg) == before and reg.characters_rev(reg.members[TESSA]) == rev
    c = char(reg, TESSA, "Bram Kettle")
    assert discord_armory.last_checked(reg, c) == clock().isoformat(timespec="seconds")  # in memory only
    assert c.armory.checked_at != clock().isoformat(timespec="seconds")
    assert discord_armory.updated_words(reg, c).startswith("Updated from the armory ")


def test_changed_is_one_commit(reg, fake, bot, clock):
    linked(bot, reg)
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, level=60, guild_id=None, modified=LATER)  # left the guild
    fake.profs["bram-kettle"][0]["skill"] = 301
    before = commits(reg)
    clock.advance(hours=1)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"
    assert commits(reg) == before + 1
    assert last_commit(reg) == f"{reg.key}: Tessa: Bram Kettle updated from the armory"
    c = char(reg, TESSA, "Bram Kettle")
    assert (c.armory.modified, c.professions[0].skill) == (LATER, 301)
    assert c.armory.checked_at == clock().isoformat(timespec="seconds")
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "unchanged"  # the new stamp is asked next
    assert commits(reg) == before + 1


def test_a_new_stamp_with_nothing_else_new_writes_nothing(reg, fake, bot, clock):
    linked(bot, reg)
    fake.chars["bram-kettle"] = replace(fake.chars["bram-kettle"], modified=LATER)  # logged out, nothing changed
    before = commits(reg)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "unchanged"
    assert commits(reg) == before
    run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle"))
    assert fake.calls[-1] == ("character", "bram-kettle", LATER)  # the stamp read is remembered in memory


def test_sync_bumps_characters_rev(reg, fake, bot, clock):
    linked(bot, reg)
    rev = reg.characters_rev(reg.members[TESSA])
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, level=60, modified=LATER, cls="Warrior")
    fake.profs["bram-kettle"][0]["recipes"] = fake.profs["bram-kettle"][0]["recipes"][:2]
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"
    assert reg.characters_rev(reg.members[TESSA]) != rev


def test_class_differs_is_flagged_not_written(reg, fake, bot, clock):
    linked(bot, reg)
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, cls="Paladin", modified=LATER)
    run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle"))
    c = char(reg, TESSA, "Bram Kettle")
    assert c.cls == "Warrior" and c.armory.cls == "Paladin"
    assert discord_armory.flags(reg, c) == ["class"]


def test_unknown_recipe_id_is_kept_and_reported_once(reg, fake, bot, clock):
    linked(bot, reg)
    c = char(reg, TESSA, "Bram Kettle")
    kept = [r for p in c.professions for r in p.recipes if r.id == 999001]
    assert [r.name for r in kept] == ["Gleaming Spindle Wrap"]
    behind = [t for _l, t in bot.ops.lines if "behind the game" in t]
    assert len(behind) == 1 and "999001" in behind[0]
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, level=60, modified=LATER, guild_id=None)
    clock.advance(minutes=10)
    run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True))
    assert len([t for _l, t in bot.ops.lines if "behind the game" in t]) == 1


def test_404_sets_missing_and_keeps_everything(reg, fake, bot, clock):
    c = linked(bot, reg)
    kept = c.model_dump(exclude={"armory", "updated_at"})  # the write stamps updated_at: a second may tick
    level = c.armory.level
    del fake.chars["bram-kettle"]
    fake.rows = [r for r in fake.rows if r.id != 700101]
    run(discord_armory.roster_refresh(bot, reg, force=True))
    before = commits(reg)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "missing"
    assert commits(reg) == before + 1
    c = char(reg, TESSA, "Bram Kettle")
    assert c.armory is not None and c.armory.missing_since == clock().isoformat(timespec="seconds") and c.armory.level == level
    assert c.model_dump(exclude={"armory", "updated_at"}) == kept
    assert "missing" not in discord_armory.flags(reg, c)  # shown to officers after a day
    clock.advance(hours=25)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "missing"
    assert commits(reg) == before + 1  # set once
    assert "missing" in discord_armory.flags(reg, char(reg, TESSA, "Bram Kettle"))
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, modified=LATER)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"
    assert char(reg, TESSA, "Bram Kettle").armory.missing_since is None  # found again


def test_a_renamed_character_is_followed_by_id(reg, fake, bot, clock):
    linked(bot, reg)
    del fake.chars["bram-kettle"]
    fake.chars["bram-ashby"] = facts("Bram", "Ashby", 700101, modified=LATER)
    fake.rows = [replace(r, surname="Ashby", slug="bram-ashby") if r.id == 700101 else r for r in fake.rows]
    run(discord_armory.roster_refresh(bot, reg, force=True))
    before = commits(reg)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"
    assert commits(reg) == before + 1
    c = char(reg, TESSA, "Bram Ashby")
    assert c is not None and c.armory.slug == "bram-ashby" and c.former_names == ["Bram", "Bram Kettle"]
    assert c.armory.missing_since is None
    assert reg.find("Bram Kettle")[1] is c


def test_a_low_level_linked_character_reads_the_roster(reg, fake, bot, clock):
    people(reg)
    link_guild(reg)
    found = run(discord_armory.find_for_link(bot, reg, "Pip", "Wobble"))
    run(discord_armory.link_character(bot, reg, WREN, "Pip", found, "Wren", by_id=WREN))
    fake.rows = [replace(r, level=7) if r.id == 700105 else r for r in fake.rows]
    fake.roster_stamp = LATER
    clock.advance(hours=3)
    run(discord_armory.roster_refresh(bot, reg))
    assert run(discord_armory.sync_character(bot, reg, WREN, "Pip Wobble")) == "updated"
    c = char(reg, WREN, "Pip Wobble")
    assert (c.armory.level, discord_armory.in_guild(reg, c), c.armory.missing_since) == (7, True, None)


# ------------------------------------------------------------------ the roster


def test_roster_is_read_every_three_hours_and_survives_a_restart(reg, fake, bot, clock):
    link_guild(reg)
    people(reg)
    rows = run(discord_armory.roster_refresh(bot, reg))
    assert len(rows) == 8 and fake.count("roster") == 1
    assert run(discord_armory.roster_refresh(bot, reg)) == rows and fake.count("roster") == 1  # inside the window
    discord_armory.reset_state()  # a restart: memory gone, the file under out/ stays
    snap, taken = discord_armory.roster_snapshot(reg)
    assert snap == rows and taken == clock().isoformat(timespec="seconds")
    run(discord_armory.roster_refresh(bot, reg))
    assert fake.count("roster") == 1
    clock.advance(hours=3, minutes=1)
    run(discord_armory.roster_refresh(bot, reg))
    assert fake.calls[-1] == ("roster", "lantern-wardens", STAMP)  # conditional on the roster's own stamp
    assert len(discord_armory.unclaimed(reg)) == 8
    link_bram(bot, reg)
    assert 700101 not in {r.id for r in discord_armory.unclaimed(reg)}


def test_a_strangers_guild_under_our_slug_stops_the_roster(reg, fake, bot, clock):
    link_guild(reg)
    fake.guild_facts = GuildFacts(id=999, slug="lantern-wardens", name="Lantern Wardens", faction="Alliance", kind="PvP", ruleset_id=1, members=2,
                                  created=fake.guild_facts.created)
    assert run(discord_armory.roster_refresh(bot, reg)) is None
    assert fake.count("roster") == 0
    warned = [t for lv, t in bot.ops.lines if lv == "warn"]
    assert len(warned) == 1 and "another guild" in warned[0]
    clock.advance(hours=2)
    assert run(discord_armory.roster_refresh(bot, reg)) is None
    assert len([t for lv, t in bot.ops.lines if lv == "warn"]) == 1  # warned once
    assert discord_armory.roster_snapshot(reg) == ([], None)


def test_no_guild_no_roster(reg, fake, bot, clock):
    assert run(discord_armory.roster_refresh(bot, reg)) is None
    assert fake.calls == [] and discord_armory.unclaimed(reg) == []


# ------------------------------------------------------------------ the guild's run


def test_sync_guild_counts_and_skips_puppets_leavers_and_retired(reg, fake, bot, clock):
    linked(bot, reg)
    found = run(discord_armory.find_for_link(bot, reg, "Tamsin", "Pell"))
    run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Owner", by_id=OWNER))
    fake.chars["tamsin-pell"] = replace(fake.chars["tamsin-pell"], level=59, modified=LATER)
    res = run(discord_armory.sync_guild(bot, reg))
    assert {k: res[k] for k in ("checked", "updated", "missing")} == {"checked": 2, "updated": 1, "missing": 0}
    reg.members[ORLA].left_at = "2026-11-12T00:00:00+00:00"
    fake.calls.clear()
    run(discord_armory.sync_guild(bot, reg))
    assert {c[1] for c in fake.calls if c[0] == "character"} == {"bram-kettle"}
    reg.retire(TESSA, "Bram Kettle")
    fake.calls.clear()
    assert run(discord_armory.sync_guild(bot, reg))["checked"] == 0 and fake.calls == []
    puppet = next(m for m in reg.members.values() if m.test)
    assert all(c.armory is None for c in puppet.characters)


@pytest.mark.parametrize("err", [ArmoryDown(detail="503"), ArmoryAccess("keys", "401")])
def test_down_warns_once_an_hour(reg, fake, bot, clock, err):
    linked(bot, reg)
    found = run(discord_armory.find_for_link(bot, reg, "Tamsin", "Pell"))
    run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Owner", by_id=OWNER))
    before, lines = commits(reg), len(bot.ops.lines)
    fake.fail, fake.calls = err, []
    res = run(discord_armory.sync_guild(bot, reg))
    assert res["checked"] == 0 and res["stopped"] and fake.count("character") == 1  # the run stops at the first
    assert commits(reg) == before
    assert len(bot.ops.lines) == lines + 1 and bot.ops.lines[-1][0] == "warn" and err.words in bot.ops.lines[-1][1]
    clock.advance(minutes=30)
    run(discord_armory.sync_guild(bot, reg))
    assert len(bot.ops.lines) == lines + 1
    clock.advance(minutes=31)
    run(discord_armory.sync_guild(bot, reg))
    assert len(bot.ops.lines) == lines + 2
    assert all(c.armory.level == 60 for c in (char(reg, TESSA, "Bram Kettle"),))  # the last data stays


def test_sync_now_is_limited(reg, fake, bot, clock):
    linked(bot, reg)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=TESSA)) == "unchanged"
    assert fake.calls[-2] == ("character", "bram-kettle", None)  # unconditional
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=TESSA)) == "limited"
    assert discord_armory.SYNC_LIMITED == "Updated a moment ago. Try again in a few minutes."
    clock.advance(minutes=5)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=TESSA)) == "unchanged"
    with pytest.raises(Exception) as e:
        run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=ORLA))
    assert str(e.value) == discord_armory.OFFICER_ONLY["sync"]


def test_no_dm_and_no_notice_from_a_sync(reg, fake, bot, clock, monkeypatch):
    linked(bot, reg)
    found = run(discord_armory.find_for_link(bot, reg, "Tamsin", "Pell"))
    run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Owner", by_id=OWNER))

    def no_notice(*a, **k):
        raise AssertionError("no notice from the armory")

    monkeypatch.setattr(Registry, "notify", no_notice)
    del fake.chars["tamsin-pell"]  # missing
    fake.rows = [r for r in fake.rows if r.id != 700103]
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, cls="Paladin", level=60, guild_id=None, modified=LATER)  # changed, flagged
    run(discord_armory.roster_refresh(bot, reg, force=True))
    res = run(discord_armory.sync_guild(bot, reg))
    assert (res["updated"], res["missing"]) == (1, 1)
    assert bot.dms == []


# ------------------------------------------------------------------ the scheduler's hook


def test_armory_tick_hourly_and_the_roster_every_three_hours(reg, fake, bot, clock):
    linked(bot, reg)  # found by its profile: no roster read yet
    fake.calls.clear()
    assert run_tick(bot, reg) is not None
    assert fake.count("character") == 1 and fake.count("roster") == 1
    assert run_tick(bot, reg) is None  # within the hour: nothing
    clock.advance(hours=1)
    run_tick(bot, reg)
    assert fake.count("character") == 2 and fake.count("roster") == 1  # the roster waits three hours
    clock.advance(hours=2, minutes=1)
    run_tick(bot, reg)
    assert fake.count("character") == 3 and fake.count("roster") == 2


def test_armory_tick_needs_the_armory(reg, fake, bot, clock, monkeypatch):
    linked(bot, reg)
    from oibot_gm import armory

    monkeypatch.setattr(armory, "available", lambda reg: False)
    fake.calls.clear()
    assert run_tick(bot, reg) is None and fake.calls == []


def test_the_scheduler_calls_the_tick():
    import inspect

    from oibot_gm import raid_scheduler

    assert "discord_armory.armory_tick(self, reg)" in inspect.getsource(raid_scheduler.RaidSchedulerMixin.scheduler_tick_guild)


def run_tick(bot, reg):
    async def go():
        t = await discord_armory.armory_tick(bot, reg)
        if t is not None:
            await t
        return t

    return asyncio.run(go())


# ------------------------------------------------------------------ fix round 1: "in the guild" is derived (R9)


def other_guild(fake):
    return GuildFacts(id=777, slug="ember-court", name="Ember Court", faction="Horde", kind="Normal", ruleset_id=2, members=3,
                      created=fake.guild_facts.created)


def test_a_guild_linked_after_the_characters_answers_from_its_first_roster_with_no_commit(reg, fake, bot, clock):
    people(reg)
    c = link_bram(bot, reg)  # no guild linked yet
    assert discord_armory.in_guild(reg, c) is None
    link_guild(reg)
    before = commits(reg)
    assert discord_armory.in_guild(reg, c) is None  # linked, but its roster not read yet
    task = run_tick(bot, reg)  # the tick sees no roster for this guild and reads it
    assert task is not None and fake.count("roster") == 1
    assert discord_armory.in_guild(reg, char(reg, TESSA, "Bram Kettle")) is True
    assert commits(reg) == before  # nothing per character was written


def test_a_switched_guild_never_answers_from_the_old_roster(reg, fake, bot, clock):
    linked(bot, reg)
    run(discord_armory.roster_refresh(bot, reg))
    c = char(reg, TESSA, "Bram Kettle")
    assert discord_armory.in_guild(reg, c) is True
    reg.set_armory_guild(other_guild(fake), "Owner", by_id=OWNER)
    before = commits(reg)
    assert discord_armory.in_guild(reg, c) is None and discord_armory.roster_snapshot(reg) == ([], None)
    assert discord_armory.unclaimed(reg) == []
    discord_armory.reset_state()  # a restart: the file on disk is the old guild's and still never answers
    assert discord_armory.in_guild(reg, c) is None
    fake.guild_facts = other_guild(fake)
    fake.rows = [r for r in fake.rows if r.id != 700101]
    run_tick(bot, reg)
    assert discord_armory.in_guild(reg, c) is False and "outside" in discord_armory.flags(reg, c)
    assert commits(reg) == before


def test_unlinking_the_guild_makes_it_not_known_with_no_commit(reg, fake, bot, clock):
    linked(bot, reg)
    run(discord_armory.roster_refresh(bot, reg))
    reg.clear_armory_guild("Owner", by_id=OWNER)
    before = commits(reg)
    c = char(reg, TESSA, "Bram Kettle")
    assert discord_armory.in_guild(reg, c) is None and discord_armory.flags(reg, c) == []
    clock.advance(hours=1)
    run(discord_armory.sync_guild(bot, reg))  # the 304 writes nothing
    assert commits(reg) == before


def test_an_old_file_with_in_guild_still_loads(reg, fake, bot, clock):
    import json

    linked(bot, reg)
    path = reg.store.root / reg.key / "members" / f"{TESSA}.json"
    d = json.loads(path.read_text())
    d["characters"][0]["armory"]["in_guild"] = True
    path.write_text(json.dumps(d))
    reg.reload()
    assert char(reg, TESSA, "Bram Kettle").armory.id == 700101


# ------------------------------------------------------------------ fix round 1: the minors


def test_warnings_are_throttled_per_kind(reg, fake, bot, clock):
    linked(bot, reg)
    found = run(discord_armory.find_for_link(bot, reg, "Tamsin", "Pell"))
    run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Owner", by_id=OWNER))
    twice = lambda p: [p, p]  # noqa: E731 — a profession listed twice never fits a record
    fake.profs["bram-kettle"] = twice(fake.profs["bram-kettle"][0])
    fake.profs["tamsin-pell"] = twice(fake.profs["tamsin-pell"][0])
    fake.chars = {k: replace(v, modified=LATER) for k, v in fake.chars.items()}
    lines = len(bot.ops.lines)
    run(discord_armory.sync_guild(bot, reg))
    kept = [t for _l, t in bot.ops.lines[lines:] if "were not kept" in t]
    assert len(kept) == 2  # one per character: Bram's never hides Tamsin's
    fake.fail = ArmoryDown(detail="503")
    clock.advance(minutes=5)
    run(discord_armory.sync_guild(bot, reg))
    assert any("stopped" in t for _l, t in bot.ops.lines[lines:])  # an outage still warns in the same hour


def test_a_failed_sync_now_can_be_pressed_again(reg, fake, bot, clock):
    linked(bot, reg)
    fake.fail = ArmoryDown(detail="503")
    with pytest.raises(ArmoryDown):
        run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=TESSA))
    fake.fail = None
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=TESSA)) == "unchanged"
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True, by_id=TESSA)) == "limited"


def test_a_puppet_is_never_linked_and_never_holds_a_link(reg, fake, bot, clock):
    from oibot_gm.registry import ArmoryLink

    people(reg)
    puppet = next(m for m in reg.members.values() if m.test and m.main and m.main.name)
    pc = puppet.main
    assert reg.link_refusal(puppet.discord_id, pc.label, 700101, "Bram", "Kettle", officer=True) == "A test character can't be linked to the armory."
    pc.armory = ArmoryLink(id=700101, slug="bram-kettle", linked_by="t", linked_at="2026-11-12T00:00:00+00:00")
    assert reg.armory_holder(700101) is None
    assert reg.link_refusal(TESSA, "Bram", 700101, "Bram", "Kettle", officer=True) is None
    pc.armory = None


def test_a_puppets_name_is_still_unique_and_refused_in_words(reg, fake, bot, clock, rs):
    """R10: a full name stays unique across every record, puppets included; the officer is told to clear the test bench."""
    from types import SimpleNamespace

    people(reg)
    puppet = next(m for m in reg.members.values() if m.test and m.main and m.main.name)
    pc = puppet.main
    before, rev = commits(reg), reg.characters_rev(reg.members[TESSA])
    with pytest.raises(Exception) as officer:
        reg.rename_character(TESSA, "Bram", pc.name, pc.surname, "t", rs=rs, officer=True)
    assert str(officer.value) == f"{pc.label} is held by a test character. Clear the test bench first."
    with pytest.raises(Exception) as member:
        reg.rename_character(TESSA, "Bram", pc.name, pc.surname, "t", rs=rs, officer=False)
    assert str(member.value) == "That name is already registered here. Ask an officer."
    assert reg.link_refusal(TESSA, "Bram", 700101, pc.name, pc.surname, officer=True) == str(officer.value)
    assert reg.link_refusal(TESSA, "Bram", 700101, pc.name, pc.surname, officer=False) == str(member.value)
    fake.chars[pc.label.lower().replace(" ", "-")] = facts(pc.name, pc.surname, 700150, cls=pc.cls)
    found = run(discord_armory.find_for_link(bot, reg, pc.name, pc.surname))
    with pytest.raises(Exception) as linked_:
        run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Owner", by_id=OWNER))
    assert str(linked_.value) == str(officer.value)
    assert commits(reg) == before and reg.characters_rev(reg.members[TESSA]) == rev  # nothing written
    assert char(reg, TESSA, "Bram").armory is None
    # both records are found as before
    assert reg.find(pc.label)[0] is puppet and reg.find("Bram")[0].discord_id == TESSA
    assert reg.award_is_for(SimpleNamespace(discord_id=TESSA, raider="Bram"), "Bram")
    assert not reg.award_is_for(SimpleNamespace(discord_id=TESSA, raider="Bram"), pc.label)
    assert reg.award_is_for(SimpleNamespace(discord_id=puppet.discord_id, raider=pc.label), pc.label)


def test_two_unlinks_at_once_answer_in_words(reg, fake, bot, clock):
    linked(bot, reg)

    async def both():
        return await asyncio.gather(*(discord_armory.unlink_character(bot, reg, TESSA, "Bram Kettle", "Tessa", by_id=TESSA) for _ in range(2)),
                                    return_exceptions=True)

    a, b = run(both())
    assert getattr(a, "armory", "x") is None
    assert isinstance(b, Exception) and str(b) == "Bram Kettle isn't linked to the armory."


def test_no_behind_report_without_the_games_list(reg, fake, bot, clock, monkeypatch):
    from oibot_gm.profiles import GameProfile

    monkeypatch.setattr(GameProfile, "has_recipe_list", property(lambda self: False))
    linked(bot, reg)
    assert not [t for _l, t in bot.ops.lines if "behind the game" in t]


def test_an_empty_professions_answer_empties_the_list(reg, fake, bot, clock):
    linked(bot, reg)
    fake.profs["bram-kettle"] = []
    fake.chars["bram-kettle"] = replace(fake.chars["bram-kettle"], modified=LATER)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"
    assert char(reg, TESSA, "Bram Kettle").professions == []


def test_a_failed_professions_read_writes_nothing_and_keeps_the_stamp(reg, fake, bot, clock, monkeypatch):
    linked(bot, reg)
    fake.chars["bram-kettle"] = replace(fake.chars["bram-kettle"], level=59, modified=LATER)

    def down(slug):
        raise ArmoryDown(detail="503")

    monkeypatch.setattr(fake, "professions", down)
    before = commits(reg)
    with pytest.raises(ArmoryDown):
        run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle"))
    assert commits(reg) == before
    c = char(reg, TESSA, "Bram Kettle")
    assert c.armory.level == 60 and c.armory.modified == STAMP
    assert discord_armory._st(reg)["modified"].get(700101) in (None, STAMP)  # the next check asks again from the old stamp


def test_the_switch_already_in_that_state_says_so(reg, fake, bot, clock):
    linked(bot, reg)
    said = run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", True, "Tessa", by_id=TESSA, preview=True))
    assert said == ["Bram Kettle's professions already follow the armory."]
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    said = run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA, preview=True))
    assert said == ["Bram Kettle's professions are already kept by hand."]


# ------------------------------------------------------------------ final review A2: one character's odd answer


def three_linked(bot, reg, fake):
    """Bram Kettle (Tessa), Tamsin Pell (Orla's Brom), Pip Rook (Wren's Pip): checked in that order (by Discord id)."""
    linked(bot, reg)
    found = run(discord_armory.find_for_link(bot, reg, "Tamsin", "Pell"))
    run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Owner", by_id=OWNER))
    fake.chars["pip-rook"] = facts("Pip", "Rook", 700105, cls="Rogue")
    fake.profs["pip-rook"] = []
    found = run(discord_armory.find_for_link(bot, reg, "Pip", "Rook"))
    run(discord_armory.link_character(bot, reg, WREN, "Pip", found, "Owner", by_id=OWNER))
    fake.chars = {k: replace(v, level=59, modified=LATER) for k, v in fake.chars.items()}  # every one has news


@pytest.mark.parametrize("err", [ArmoryShape("/profile/wow/character/tamsin-pell/professions", "tier"),
                                 ArmoryAccess("refused", "GET x: 403", token=False)])
def test_one_characters_odd_answer_never_stops_the_run(reg, fake, bot, clock, monkeypatch, err):
    three_linked(bot, reg, fake)
    real = fake.professions

    def odd(slug):
        if "tamsin" in slug:
            raise err
        return real(slug)

    monkeypatch.setattr(fake, "professions", odd)
    lines = len(bot.ops.lines)
    res = run(discord_armory.sync_guild(bot, reg))
    assert not res["stopped"] and res["updated"] == 2
    assert char(reg, TESSA, "Bram Kettle").armory.level == 59 and char(reg, WREN, "Pip Rook").armory.level == 59
    assert char(reg, ORLA, "Tamsin Pell").armory.level == 60, "the odd one keeps its last data"
    warned = [t for _l, t in bot.ops.lines[lines:]]
    assert len(warned) == 1 and "Orla's Tamsin Pell" in warned[0] and "stopped" not in warned[0]
    clock.advance(minutes=30)
    run(discord_armory.sync_guild(bot, reg))
    assert len(bot.ops.lines) == lines + 1, "once an hour for that character"


def test_a_token_refusal_still_stops_the_run(reg, fake, bot, clock):
    three_linked(bot, reg, fake)
    fake.fail, fake.calls = ArmoryAccess("keys", "token refused"), []
    res = run(discord_armory.sync_guild(bot, reg))
    assert res["stopped"] and fake.count("character") == 1


def test_a_profession_without_tiers_is_a_profession_with_no_recipes():
    from test_armory_client import C, client

    a, _ = client({f"{C}/professions": (200, {"primaries": [{"profession": {"id": 165, "name": "Leatherworking"}}]})})
    assert a.professions("tamsin-pell") == [{"id": "leatherworking", "skill": 0, "recipes": []}]


def test_a_403_is_not_a_token_refusal_and_a_401_is():
    from test_armory_client import C, client

    a, _ = client({C: (403, b"")})
    with pytest.raises(ArmoryAccess) as e:
        a.character("tamsin-pell")
    assert e.value.token is False
    a, _ = client({C: (401, b"")})
    with pytest.raises(ArmoryAccess) as e:
        a.character("tamsin-pell")
    assert e.value.token is True


# ------------------------------------------------------------------ final review A3 / A4: a check that must read again


def test_follow_the_armory_again_after_a_failed_check_reads_again_next_hour(reg, fake, bot, clock):
    linked(bot, reg)
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    reg.save_character_table(TESSA, [{"label": "Bram Kettle", "professions": [{"id": "mining", "skill": 200}]}], "Tessa", by_id=TESSA)
    fake.fail = ArmoryDown(detail="503")
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", True, "Tessa", by_id=TESSA))  # the forced check fails
    assert [p.id for p in char(reg, TESSA, "Bram Kettle").professions] == ["mining"]
    fake.fail, fake.calls = None, []
    clock.advance(hours=1)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"  # nothing changed in the game
    assert ("character", "bram-kettle", None) in fake.calls, "read unconditionally, never a 304 on the old stamp"
    assert [p.id for p in char(reg, TESSA, "Bram Kettle").professions] == ["leatherworking"]
    fake.calls = []
    run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle"))
    assert fake.calls == [("character", "bram-kettle", STAMP)], "then conditional again"


def test_a_link_whose_professions_read_404s_reads_them_on_the_next_check(reg, fake, bot, clock, monkeypatch):
    from oibot_gm.armory import ArmoryMissing

    people(reg)
    link_guild(reg)
    real = fake.professions

    def missing(slug):
        raise ArmoryMissing("gone", "404")

    monkeypatch.setattr(fake, "professions", missing)
    c = link_bram(bot, reg)
    assert [p.id for p in c.professions] == ["mining"] and c.armory.modified is None
    monkeypatch.setattr(fake, "professions", real)
    fake.calls = []
    clock.advance(hours=1)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "updated"
    assert ("character", "bram-kettle", None) in fake.calls
    c = char(reg, TESSA, "Bram Kettle")
    assert [p.id for p in c.professions] == ["leatherworking"]


# ------------------------------------------------------------------ final review A7: no no-op commits


def test_a_case_only_name_difference_and_a_reordered_answer_write_nothing(reg, fake, bot, clock):
    linked(bot, reg)
    fake.chars["bram-kettle"] = replace(fake.chars["bram-kettle"], surname="KETTLE", modified=LATER)
    fake.profs["bram-kettle"][0]["recipes"] = list(reversed(fake.profs["bram-kettle"][0]["recipes"]))
    before = commits(reg)
    for _ in range(2):
        clock.advance(hours=1)
        assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle", force=True)) == "unchanged"
    assert commits(reg) == before and char(reg, TESSA, "Bram Kettle").label == "Bram Kettle"


def test_many_unknown_recipes_are_one_ops_line(reg, fake, bot, clock):
    """A6: a list a patch behind tells ops once per check, counting, naming the first few."""
    fake.profs["bram-kettle"][0]["recipes"] += [{"id": 999100 + i, "name": f"Patch Wrap {chr(65 + i)}"} for i in range(5)]
    linked(bot, reg)
    behind = [t for _l, t in bot.ops.lines if "behind the game" in t]
    assert len(behind) == 1
    assert "6 recipes on Tessa's Bram Kettle" in behind[0] and "and 3 more" in behind[0] and "999001" in behind[0]
