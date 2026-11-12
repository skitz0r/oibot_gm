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
    assert (c.armory.in_guild, c.armory.modified, c.professions[0].skill) == (False, LATER, 301)
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
    kept = c.model_dump(exclude={"armory"})
    level = c.armory.level
    del fake.chars["bram-kettle"]
    fake.rows = [r for r in fake.rows if r.id != 700101]
    run(discord_armory.roster_refresh(bot, reg, force=True))
    before = commits(reg)
    assert run(discord_armory.sync_character(bot, reg, TESSA, "Bram Kettle")) == "missing"
    assert commits(reg) == before + 1
    c = char(reg, TESSA, "Bram Kettle")
    assert c.armory is not None and c.armory.missing_since == clock().isoformat(timespec="seconds") and c.armory.level == level
    assert c.model_dump(exclude={"armory"}) == kept
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
    assert (c.armory.level, c.armory.in_guild, c.armory.missing_since) == (7, True, None)


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
    fake.guild_facts = GuildFacts(id=999, slug="lantern-wardens", name="Lantern Wardens", faction="Alliance", kind="PvP", members=2,
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
    fake.chars["tamsin-pell"] = replace(fake.chars["tamsin-pell"], level=60, modified=LATER, guild_id=None)
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


@pytest.mark.parametrize("err", [ArmoryDown(detail="503"), ArmoryShape("/x", "members"), ArmoryAccess("keys", "401")])
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
