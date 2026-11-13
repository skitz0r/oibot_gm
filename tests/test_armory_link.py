"""Linking a registered character to its armory character (armory link, spec §5.2): find_for_link, link_character,
unlink_character and the per-character "keep professions by hand" switch (discord_armory.py), the Registry verbs
under them, and the hand-edit refusal on a synced character (R2). A fake Armory stands in for Blizzard; the guild,
the characters and the members are invented ("Lantern Wardens", Bram Kettle registered to Tessa)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import OWNER

from oibot_gm import armory, battlenet, discord_armory
from oibot_gm import consequences as cq
from oibot_gm.armory import NOT_MODIFIED, Armory, ArmoryMissing, CharacterFacts, GuildFacts, Reply
from oibot_gm.registry import Registry, RegistryError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "armory" / "profile"
STAMP = "Thu, 12 Nov 2026 19:59:57 GMT"
LATER = "Thu, 12 Nov 2026 21:10:00 GMT"
TESSA, ORLA, WREN = 470_000_000_000_000_101, 470_000_000_000_000_102, 470_000_000_000_000_103
WARDENS = 418362


def run(coro):
    return asyncio.run(coro)


def _parsed(name: str, kind: str):
    """A fixture through the real client's validation (roster rows, professions) — the shapes the bot reads."""
    body = (FIXTURES / f"{name}.json").read_bytes()
    from oibot_gm.profiles import GameProfile

    prof = GameProfile.load(ROOT / "profiles" / "forever")
    cli = Armory("us", "p", "s", fetch=lambda url, headers: Reply(200, {"last-modified": STAMP}, body), classes=prof.armory["classes"],
                 rulesets=prof.armory["rulesets"], professions={p.blizzard_id: p.id for p in prof.professions.values() if p.blizzard_id},
                 limiter=battlenet.Limiter(1000))
    return cli.roster("x")[0] if kind == "roster" else cli.professions("x")


ROWS = _parsed("roster", "roster")
TAMSIN_PROFS = _parsed("professions", "professions")


def facts(name: str, surname: str | None, aid: int, cls: str = "Warrior", level: int = 60, guild_id: int | None = WARDENS,
          modified: str = STAMP) -> CharacterFacts:
    return CharacterFacts(id=aid, name=name, surname=surname, cls=cls, level=level, guild_id=guild_id, modified=modified)


class FakeArmory:
    """The armory as the bot reads it: profiles and professions by address, the guild and its roster. Every call is
    kept; `fail` makes the next calls raise."""

    def __init__(self):
        self.chars: dict[str, CharacterFacts] = {"bram-kettle": facts("Bram", "Kettle", 700101),
                                                 "tamsin-pell": facts("Tamsin", "Pell", 700103, cls="Druid")}
        self.profs: dict[str, list] = {"bram-kettle": [dict(p, recipes=list(p["recipes"])) for p in TAMSIN_PROFS[:1]],
                                       "tamsin-pell": [dict(p) for p in TAMSIN_PROFS]}
        self.rows = list(ROWS)
        self.roster_stamp = STAMP
        self.guild_facts = GuildFacts(id=WARDENS, slug="lantern-wardens", name="Lantern Wardens", faction="Horde", kind="Normal",
                                      members=8, created=datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.fail: Exception | None = None
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    def character(self, slug, since=None):
        slug = urllib.parse.unquote(slug).lower()
        self.calls.append(("character", slug, since))
        self._maybe_fail()
        f = self.chars.get(slug)
        if f is None:
            raise ArmoryMissing(armory.CHARACTER_MISSING_WORDS, "404")
        if since and since == f.modified:
            return NOT_MODIFIED
        return f

    def professions(self, slug):
        slug = urllib.parse.unquote(slug).lower()
        self.calls.append(("professions", slug))
        self._maybe_fail()
        if slug not in self.chars:
            raise ArmoryMissing(armory.CHARACTER_MISSING_WORDS, "404")
        return [dict(p, recipes=[dict(r) for r in p["recipes"]]) for p in self.profs.get(slug, [])]

    def guild(self, slug):
        self.calls.append(("guild", slug))
        self._maybe_fail()
        return self.guild_facts

    def roster(self, slug, since=None):
        self.calls.append(("roster", slug, since))
        self._maybe_fail()
        if since and since == self.roster_stamp:
            return NOT_MODIFIED, since
        return list(self.rows), self.roster_stamp

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)


class FakeOps:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def emit(self, cfg, level, text, exc=None, dm=True, dm_only=None):
        self.lines.append((level, text))


class FakeBot:
    """A bare client: an ops feed that records, and a DM path that must never be used."""

    def __init__(self):
        self.ops = FakeOps()
        self.dms: list = []
        self.redrawn: list[str] = []

    async def send_member_dm(self, *a, **k):  # the DM rule: a sync, a link, an unlink never DM
        self.dms.append((a, k))
        raise AssertionError("no DM from the armory")

    async def refresh_sheet(self, reg, ev):
        self.redrawn.append(ev.key)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeArmory()
    monkeypatch.setattr(armory, "client_for", lambda reg: f)
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    monkeypatch.setattr(discord_armory, "OUT", tmp_path / "armory-out")
    discord_armory.reset_state()
    yield f
    discord_armory.reset_state()


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(discord_armory, "_now", c)
    return c


@pytest.fixture
def bot():
    return FakeBot()


def link_guild(reg):
    reg.set_armory_guild(FakeArmory().guild_facts, "Owner", by_id=OWNER)


def people(reg):
    """Tessa registered "Bram" (a Warrior with a hand-typed Mining); Orla a Warrior "Brom"; Wren a Rogue "Pip"."""
    reg.add_character(TESSA, "Tessa", "Bram", "Warrior", "Protection", None, True)
    reg.set_professions(TESSA, "Bram", [{"id": "mining", "skill": 120}])
    reg.add_character(ORLA, "Orla", "Brom", "Warrior", "Arms", None, True)
    reg.add_character(WREN, "Wren", "Pip", "Rogue", "Combat", None, True)


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def last_commit(reg) -> str:
    return subprocess.run(["git", "log", "-1", "--format=%s"], cwd=reg.store.root, capture_output=True, text=True, check=True).stdout.strip()


def char(reg, uid, label):
    m = reg.members[uid]
    return reg.own_character(m.characters, label)


def link_bram(bot, reg, by_id=TESSA):
    found = run(discord_armory.find_for_link(bot, reg, "Bram", "Kettle"))
    return run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Tessa", by_id=by_id))


# ------------------------------------------------------------------ linking


def test_link_happy_path(reg, fake, bot, clock):
    link_guild(reg)
    people(reg)
    found = run(discord_armory.find_for_link(bot, reg, "bram", "kettle"))
    assert (found.id, found.name, found.surname, found.cls, found.level, found.source) == (700101, "Bram", "Kettle", "Warrior", 60, "profile")
    said = run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Tessa", by_id=TESSA, preview=True))
    assert said == cq.armory_link_consequence(reg, reg.members[TESSA], char(reg, TESSA, "Bram"), found, TESSA)
    assert "Bram Kettle" in said[0] and "Your Bram takes that name" in said[1]
    before, lines = commits(reg), len(bot.ops.lines)
    c = run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Tessa", by_id=TESSA))
    assert commits(reg) == before + 1  # the rename and the link land as one commit
    assert c.label == "Bram Kettle" and c.former_names == ["Bram"]
    a = c.armory
    assert (a.id, a.slug, a.level, a.cls, a.linked_by, a.missing_since) == (700101, "bram-kettle", 60, "Warrior", "Tessa", None)
    assert "in_guild" not in a.model_dump()  # derived from the roster, never stored (R9)
    assert discord_armory.in_guild(reg, c) is None  # the guild's roster hasn't been read yet
    run(discord_armory.roster_refresh(bot, reg))
    assert discord_armory.in_guild(reg, c) is True
    assert c.profession_sync is True
    assert [p.id for p in c.professions] == ["leatherworking"]  # the hand-typed Mining is replaced by the armory's list
    audit = [t for lv, t in bot.ops.lines[lines:] if lv == "info"]
    assert len(audit) == 1 and "Bram Kettle" in audit[0]  # one audit line
    assert bot.dms == []
    on_disk = json.loads((reg.store.root / reg.key / "members" / f"{TESSA}.json").read_text())
    assert on_disk["characters"][0]["armory"]["id"] == 700101


def test_link_needs_no_guild_membership(reg, fake, bot, clock):
    people(reg)  # no guild linked to the server, and the character is in a stranger's guild
    fake.chars["bram-kettle"] = facts("Bram", "Kettle", 700101, guild_id=555)
    fake.rows = [r for r in fake.rows if r.id != 700101]
    c = link_bram(bot, reg)
    assert c.armory.id == 700101 and discord_armory.in_guild(reg, c) is None  # not known: no guild is linked
    assert discord_armory.flags(reg, c) == []
    link_guild(reg)
    run(discord_armory.roster_refresh(bot, reg))
    assert discord_armory.in_guild(reg, c) is False and "outside" in discord_armory.flags(reg, c)


def link_bram_again(bot, reg):
    found = run(discord_armory.find_for_link(bot, reg, "Bram", "Kettle"))
    return run(discord_armory.link_character(bot, reg, TESSA, "Bram Kettle", found, "Tessa", by_id=TESSA))


def test_a_second_claim_is_refused_naming_the_holder_to_an_officer_only(reg, fake, bot, clock):
    link_guild(reg)
    people(reg)
    link_bram(bot, reg)
    found = run(discord_armory.find_for_link(bot, reg, "Bram", "Kettle"))
    with pytest.raises(RegistryError) as member:
        run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Orla", by_id=ORLA, preview=True))
    assert str(member.value) == "That armory character is already linked here. Ask an officer."
    before = commits(reg)
    with pytest.raises(RegistryError) as officer:
        run(discord_armory.link_character(bot, reg, ORLA, "Brom", found, "Owner", by_id=OWNER))
    assert str(officer.value) == "Bram Kettle in the armory is linked to Tessa's Bram Kettle. Unlink it there first."
    assert commits(reg) == before and char(reg, ORLA, "Brom").armory is None


def test_someone_elses_character_needs_an_officer(reg, fake, bot, clock):
    people(reg)
    found = run(discord_armory.find_for_link(bot, reg, "Bram", "Kettle"))
    with pytest.raises(RegistryError) as e:
        run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Orla", by_id=ORLA))
    assert str(e.value) == discord_armory.OFFICER_ONLY["link"]
    c = run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Owner", by_id=OWNER))  # an officer (the owner) may
    assert c.armory.linked_by == "Owner"


def test_a_low_level_character_is_found_on_the_roster(reg, fake, bot, clock):
    link_guild(reg)
    people(reg)
    found = run(discord_armory.find_for_link(bot, reg, "Pip", "Wobble"))  # level 1: the profile 404s, the roster has him
    assert (found.id, found.level, found.cls, found.source) == (700105, 1, "Rogue", "roster")
    said = cq.armory_link_consequence(reg, reg.members[WREN], char(reg, WREN, "Pip"), found, WREN)
    assert "roster" in said[0] and "level 10" in said[1]
    c = run(discord_armory.link_character(bot, reg, WREN, "Pip", found, "Wren", by_id=WREN))
    assert c.label == "Pip Wobble" and (c.armory.level, discord_armory.in_guild(reg, c)) == (1, True)
    assert fake.count("professions") == 0  # nothing to read below level 10


def test_not_found_is_the_refusal_sentence(reg, fake, bot, clock):
    people(reg)
    with pytest.raises(RegistryError) as e:  # no guild linked: only the profile is asked
        run(discord_armory.find_for_link(bot, reg, "Pip", "Wobble"))
    assert str(e.value) == discord_armory.NOT_FOUND == "The armory has no character by that name. Reach level 10 and log out once, then try again."
    link_guild(reg)
    with pytest.raises(RegistryError) as e:  # nor on the guild's roster
        run(discord_armory.find_for_link(bot, reg, "Nobody", "Here"))
    assert str(e.value) == discord_armory.NOT_FOUND


def test_a_name_another_record_holds_is_refused_before_anything_is_written(reg, fake, bot, clock):
    people(reg)
    reg.add_character(ORLA, "Orla", "Bram", "Warrior", "Arms", None, False, surname="Kettle")
    found = run(discord_armory.find_for_link(bot, reg, "Bram", "Kettle"))
    before = commits(reg)
    with pytest.raises(RegistryError) as e:
        run(discord_armory.link_character(bot, reg, TESSA, "Bram", found, "Tessa", by_id=TESSA))
    assert str(e.value) == "That name is already registered here. Ask an officer."
    assert commits(reg) == before and char(reg, TESSA, "Bram").armory is None


# ------------------------------------------------------------------ unlink and the switch


def test_unlink_keeps_the_professions_and_makes_them_hand_editable(reg, fake, bot, clock):
    people(reg)
    link_bram(bot, reg)
    kept = [p.model_dump() for p in char(reg, TESSA, "Bram Kettle").professions]
    said = run(discord_armory.unlink_character(bot, reg, TESSA, "Bram Kettle", "Tessa", by_id=TESSA, preview=True))
    assert said == cq.armory_unlink_consequence(reg, reg.members[TESSA], char(reg, TESSA, "Bram Kettle"), TESSA)
    c = run(discord_armory.unlink_character(bot, reg, TESSA, "Bram Kettle", "Tessa", by_id=TESSA))
    assert c.armory is None and [p.model_dump() for p in c.professions] == kept and c.label == "Bram Kettle"
    reg.save_character_table(TESSA, [{"label": "Bram Kettle", "professions": [{"id": "leatherworking", "skill": 250}]}], "Tessa", by_id=TESSA)
    assert char(reg, TESSA, "Bram Kettle").professions[0].skill == 250
    with pytest.raises(RegistryError) as e:
        run(discord_armory.unlink_character(bot, reg, TESSA, "Bram Kettle", "Tessa", by_id=TESSA))
    assert str(e.value) == "Bram Kettle isn't linked to the armory."


def test_the_switch_off_makes_them_editable_and_on_replaces_the_hand_edits(reg, fake, bot, clock):
    people(reg)
    link_bram(bot, reg)
    off = run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA, preview=True))
    assert off == cq.profession_sync_consequence(False, "Bram Kettle") and "kept by hand" in off[0]
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    assert char(reg, TESSA, "Bram Kettle").profession_sync is False
    reg.save_character_table(TESSA, [{"label": "Bram Kettle", "professions": [{"id": "mining", "skill": 200}]}], "Tessa", by_id=TESSA)
    assert [p.id for p in char(reg, TESSA, "Bram Kettle").professions] == ["mining"]
    on = cq.profession_sync_consequence(True, "Bram Kettle")
    assert "replace the hand edits" in on[0]
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", True, "Tessa", by_id=TESSA))
    c = char(reg, TESSA, "Bram Kettle")
    assert c.profession_sync is True and [p.id for p in c.professions] == ["leatherworking"]  # the armory's list is back now


def test_a_synced_characters_professions_refuse_a_hand_edit(reg, fake, bot, clock):
    people(reg)
    link_bram(bot, reg)
    c = char(reg, TESSA, "Bram Kettle")
    assert any(r.id == 999001 for r in c.professions[0].recipes)  # the recipe the game's list doesn't have yet
    same = reg.professions_entries(c)
    reg.save_character_table(TESSA, [{"label": "Bram Kettle", "spec": "Arms", "cls": "Warrior", "professions": same}], "Tessa", by_id=TESSA)
    assert char(reg, TESSA, "Bram Kettle").spec == "Arms"  # an unchanged list rides along with another edit (R2)
    before = commits(reg)
    with pytest.raises(RegistryError) as e:
        reg.save_character_table(TESSA, [{"label": "Bram Kettle", "professions": [{"id": "mining", "skill": 200}]}], "Tessa", by_id=TESSA)
    assert str(e.value) == "Bram Kettle's professions come from the armory, so turn on Keep professions by hand to edit them."
    assert commits(reg) == before


def test_the_switch_and_unlink_need_a_link(reg, fake, bot, clock):
    people(reg)
    with pytest.raises(RegistryError) as e:
        run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram", False, "Tessa", by_id=TESSA))
    assert str(e.value) == "Bram isn't linked to the armory."
    with pytest.raises(RegistryError) as e:
        run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram", False, "Orla", by_id=ORLA))
    assert str(e.value) == discord_armory.OFFICER_ONLY["switch"]


# ------------------------------------------------------------------ final review B1 / B2 / A5 / A9: what a character holds


def a_listed_leatherworking_recipe(reg, c):
    held = {r.id for p in c.professions for r in p.recipes}
    return next(d for d in sorted(reg.profile.recipes_of("leatherworking"), key=lambda d: d.id) if d.id not in held)


@pytest.mark.parametrize("how", ["by_hand", "unlinked"])
def test_a_held_recipe_the_list_lacks_never_blocks_a_hand_edit(reg, fake, bot, clock, how):
    """B1: the armory kept Gleaming Spindle Wrap (an id the game's list lacks). Kept by hand or unlinked, every edit of the
    character's professions still works and keeps it; a NEW unlisted recipe is still refused."""
    people(reg)
    link_bram(bot, reg)
    if how == "by_hand":
        run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    else:
        run(discord_armory.unlink_character(bot, reg, TESSA, "Bram Kettle", "Tessa", by_id=TESSA))
    c = char(reg, TESSA, "Bram Kettle")
    assert any(r.id == 999001 and r.id not in reg.profile.recipes for p in c.professions for r in p.recipes)
    # add a profession
    reg.set_professions(TESSA, "Bram Kettle", reg.professions_entries(c) + [{"id": "cooking", "skill": 100, "recipes": []}])
    c = char(reg, TESSA, "Bram Kettle")
    assert [p.id for p in c.professions] == ["leatherworking", "cooking"] and any(r.id == 999001 for r in c.professions[0].recipes)
    # add a listed recipe
    d = a_listed_leatherworking_recipe(reg, c)
    e = reg.professions_entries(c)
    next(p for p in e if p["id"] == "leatherworking")["recipes"].append({"id": d.id, "name": d.name})
    assert cq.profession_consequence(reg, reg.members[TESSA], "Bram Kettle", e, by_id=TESSA)
    reg.set_professions(TESSA, "Bram Kettle", e)
    c = char(reg, TESSA, "Bram Kettle")
    assert {d.id, 999001} <= {r.id for r in c.professions[0].recipes}
    # change a skill
    e = reg.professions_entries(c)
    e[0]["skill"] = 250
    reg.set_professions(TESSA, "Bram Kettle", e)
    assert char(reg, TESSA, "Bram Kettle").professions[0].skill == 250
    # a held recipe named without its id is the held one
    e = reg.professions_entries(char(reg, TESSA, "Bram Kettle"))
    e[0]["recipes"] = [{"name": "gleaming spindle wrap"} if r.get("id") == 999001 else r for r in e[0]["recipes"]]
    reg.set_professions(TESSA, "Bram Kettle", e)
    assert any(r.id == 999001 and r.name == "Gleaming Spindle Wrap" for r in char(reg, TESSA, "Bram Kettle").professions[0].recipes)
    # a NEW unlisted recipe, or the held one moved to another profession, is refused: the keep path adds nothing
    for bad in ({"id": 999777, "name": "Made Up Wrap"},):
        e = reg.professions_entries(char(reg, TESSA, "Bram Kettle"))
        e[0]["recipes"].append(bad)
        with pytest.raises(RegistryError) as err:
            reg.set_professions(TESSA, "Bram Kettle", e)
        assert str(err.value) == "Made Up Wrap isn't a recipe in the game's list."
    e = reg.professions_entries(char(reg, TESSA, "Bram Kettle"))
    e[1]["recipes"] = [{"id": 999001, "name": "Gleaming Spindle Wrap"}]  # onto Cooking
    with pytest.raises(RegistryError):
        reg.set_professions(TESSA, "Bram Kettle", e)
    # remove the unknown recipe
    e = reg.professions_entries(char(reg, TESSA, "Bram Kettle"))
    e[0]["recipes"] = [r for r in e[0]["recipes"] if r.get("id") != 999001]
    reg.set_professions(TESSA, "Bram Kettle", e)
    assert not any(r.id == 999001 for r in char(reg, TESSA, "Bram Kettle").professions[0].recipes)


def test_a_held_state_the_hand_rules_refuse_is_kept_until_the_edit_adds_to_it(reg, fake, bot, clock):
    """B1: the armory's skill above the cap and three trade professions round-trip; a changed skill, or a new trade, is
    checked by the hand rules."""
    fake.profs["bram-kettle"][0]["skill"] = 310
    fake.profs["bram-kettle"] += [{"id": "mining", "skill": 120, "recipes": []}, {"id": "skinning", "skill": 90, "recipes": []}]
    people(reg)
    link_bram(bot, reg)
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    c = char(reg, TESSA, "Bram Kettle")
    assert [(p.id, p.skill) for p in c.professions][:1] == [("leatherworking", 310)] and len(c.professions) == 3
    reg.set_professions(TESSA, "Bram Kettle", reg.professions_entries(c) + [{"id": "cooking", "skill": 100, "recipes": []}])
    c = char(reg, TESSA, "Bram Kettle")
    assert len(c.professions) == 4 and c.professions[0].skill == 310
    e = reg.professions_entries(c)
    e[0]["skill"] = 305
    with pytest.raises(RegistryError, match="Skill is a number from 1 to 300."):
        reg.set_professions(TESSA, "Bram Kettle", e)
    e = [p for p in reg.professions_entries(c) if p["id"] != "skinning"] + [{"id": "tailoring", "skill": 10, "recipes": []}]
    with pytest.raises(RegistryError, match="two trade professions"):
        reg.set_professions(TESSA, "Bram Kettle", e)
    e = [p for p in reg.professions_entries(c) if p["id"] != "skinning"]  # dropping one of three is fine
    reg.set_professions(TESSA, "Bram Kettle", e)
    assert [p.id for p in char(reg, TESSA, "Bram Kettle").professions] == ["leatherworking", "mining", "cooking"]


def test_every_preview_of_a_synced_character_refuses_in_the_writes_words(reg, fake, bot, clock):
    """B2: no preview offers what Apply then refuses."""
    from oibot_gm import configops

    said = "Bram Kettle's professions come from the armory, so turn on Keep professions by hand to edit them."
    fake.profs["bram-kettle"] = [dict(p, recipes=[r for r in p["recipes"] if r["id"] != 999001]) for p in fake.profs["bram-kettle"]]
    people(reg)
    link_bram(bot, reg)
    c = char(reg, TESSA, "Bram Kettle")
    entries = reg.professions_entries(c) + [{"id": "cooking", "skill": 100, "recipes": []}]
    with pytest.raises(RegistryError) as e:
        reg.set_professions(TESSA, "Bram Kettle", entries)
    assert str(e.value) == said
    with pytest.raises(RegistryError) as e:
        cq.profession_consequence(reg, reg.members[TESSA], "Bram Kettle", entries, by_id=TESSA)
    assert str(e.value) == said
    for field, value in (("profession", "cooking 100"), ("recipe_add", a_listed_leatherworking_recipe(reg, c).name),
                         ("recipe_remove", c.professions[0].recipes[0].name), ("profession_remove", "leatherworking")):
        op = configops.ConfigOp(op="character", member="Tessa", character="Bram Kettle", field=field, value=value)
        out = configops.describe(reg, op, by_id=TESSA)
        assert said in str(out) and "recorded" not in str(out), (field, out)


def test_the_officers_fix_list_shows_held_off_list_recipes_and_never_a_synced_character(reg, fake, bot, clock):
    """A5 / A9: a synced character's recipes are the armory's (never on the fix list); kept by hand, a recipe the list
    lacks is listed, and Remove takes it off."""
    from oibot_gm import professions

    people(reg)
    link_bram(bot, reg)
    assert not [r for r in professions.unmatched(reg) if r["label"] == "Bram Kettle"]
    run(discord_armory.set_profession_sync(bot, reg, TESSA, "Bram Kettle", False, "Tessa", by_id=TESSA))
    rows = [r for r in professions.unmatched(reg) if r["label"] == "Bram Kettle"]
    assert rows == [{"uid": TESSA, "member": "Tessa", "label": "Bram Kettle", "profession": "leatherworking", "name": "Gleaming Spindle Wrap"}]
    said = professions.fix_unmatched(reg, TESSA, "Bram Kettle", "leatherworking", "Gleaming Spindle Wrap", None, "Owner", by_id=OWNER)
    assert said == ["Gleaming Spindle Wrap comes off Bram Kettle's Leatherworking."]
    assert not any(r.id == 999001 for p in char(reg, TESSA, "Bram Kettle").professions for r in p.recipes)


def test_tbc_has_no_armory_and_behaves_as_today(tmp_path, monkeypatch):
    from oibot_gm.profiles import GameProfile

    tbc = GameProfile.load(ROOT / "profiles" / "tbc")
    assert not tbc.armory
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))

    class Reg:
        key, profile = "t", tbc
        config = type("C", (), {"armory": None})()
        members: dict = {}

    assert run(discord_armory.armory_tick(FakeBot(), Reg())) is None

