"""The guild's auction house (auction prices, spec §4.1, C11): the owner picks which house the guild's prices come from —
`GuildConfig.market` — beside the server's guild link. Owner only, one commit each, in the `settings` rev; refused without
a linked guild or on a version with no auction houses. Re-linking the server to a guild of another ruleset or faction
clears the choice in the same commit; unlinking always does. No network: the facts are built by hand (invented names)."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import OWNER

from oibot_gm import consequences as cq
from oibot_gm.armory import GuildFacts
from oibot_gm.auctions import House
from oibot_gm.profiles import GameProfile
from oibot_gm.registry import ConfigChanged, Market, RegistryError

ROOT = Path(__file__).resolve().parents[1]
OFFICER = 100_000_000_000_000_777
BORN = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
HORDE_AH = House(id=6, name="Horde Auction House")
OTHER_HORDE_AH = House(id=18, name="Horde Auction House")


def facts(*, faction="Horde", ruleset=2, gid=418362, name="Lantern Wardens") -> GuildFacts:
    return GuildFacts(id=gid, slug=name.lower().replace(" ", "-"), name=name, faction=faction, kind="Normal", ruleset_id=ruleset,
                      members=8, created=BORN)


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def linked(reg, **kw):
    reg.set_armory_guild(facts(**kw), "Owner", by_id=OWNER)
    return reg


def test_only_the_owner_sets_and_clears_the_market(reg):
    linked(reg)
    assert reg.config.market is None
    with pytest.raises(RegistryError, match="owner"):
        reg.set_market(HORDE_AH, 2, "Officer", by_id=OFFICER)
    assert reg.config.market is None
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    m = reg.config.market
    assert isinstance(m, Market)
    assert (m.ruleset_id, m.house_id, m.name, m.faction, m.picked_by) == (2, 6, "Horde Auction House", "Horde", "Owner")
    assert m.picked_at
    reread = type(reg)(reg.store, reg.key, reg.base_profile)
    assert reread.config.market == m, "it is stored in guild.yaml"
    words = reg.market_words()
    assert "Horde Auction House" in words and "Owner" in words
    with pytest.raises(RegistryError, match="owner"):
        reg.clear_market("Officer", by_id=OFFICER)
    reg.clear_market("Owner", by_id=OWNER)
    assert reg.config.market is None
    with pytest.raises(RegistryError, match="No auction house"):
        reg.clear_market("Owner", by_id=OWNER)


def test_no_linked_guild_or_no_auctions_block_is_refused(reg, monkeypatch):
    with pytest.raises(RegistryError, match="guild"):
        reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert reg.config.market is None
    assert "auction house" in reg.market_words() and "link" in reg.market_words()
    linked(reg)
    with pytest.raises(RegistryError, match="faction"):
        reg.set_market(House(id=2, name="Alliance Auction House"), 2, "Owner", by_id=OWNER)
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    assert p.auctions is None
    with pytest.raises(RegistryError, match="no auction houses"):
        reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert reg.config.market is None
    assert reg.market_words() == "This game version has no auction houses."


def test_a_stale_settings_rev_raises_config_changed(reg):
    linked(reg)
    rev = reg.config_rev("settings")
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER, rev=rev)
    assert reg.config_rev("settings") != rev, "the choice is part of the settings rev"
    with pytest.raises(ConfigChanged):
        reg.set_market(OTHER_HORDE_AH, 2, "Owner", by_id=OWNER, rev=rev)
    with pytest.raises(ConfigChanged):
        reg.clear_market("Owner", by_id=OWNER, rev=rev)
    assert reg.config.market.house_id == 6


def test_relinking_the_same_ruleset_and_faction_keeps_the_market(reg):
    linked(reg).set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    m = reg.config.market
    assert reg.market_survives(facts(gid=700001, name="Ember Court"))
    reg.set_armory_guild(facts(gid=700001, name="Ember Court"), "Owner", by_id=OWNER)
    assert reg.config.market == m
    lines = cq.armory_guild_consequence(reg, facts())
    assert not any("Prices" in s for s in lines)


def test_relinking_another_faction_or_ruleset_clears_it_in_the_same_commit(reg):
    linked(reg).set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    for other in (facts(faction="Alliance", gid=700002, name="Ember Court"), facts(ruleset=15, gid=700003, name="Grey Lantern")):
        assert not reg.market_survives(other)
        lines = cq.armory_guild_consequence(reg, other)
        assert "Prices turn off until you pick the auction house again." in lines and len(lines) <= 3
        before = commits(reg)
        reg.set_armory_guild(other, "Owner", by_id=OWNER)
        assert commits(reg) == before + 1, "the link and the cleared choice are one commit"
        assert reg.config.market is None and reg.config.armory.guild_id == other.id
        reread = type(reg)(reg.store, reg.key, reg.base_profile)
        assert reread.config.market is None
        linked(reg).set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert not reg.market_survives(facts(ruleset=3)) and reg.market_survives(facts())


def test_unlinking_the_guild_clears_it(reg):
    linked(reg).set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert "Prices turn off until you pick the auction house again." in cq.armory_guild_clear_consequence(reg)
    before = commits(reg)
    reg.clear_armory_guild("Owner", by_id=OWNER)
    assert commits(reg) == before + 1
    assert reg.config.market is None and reg.config.armory is None
    assert not reg.market_survives(facts()), "no market: nothing survives"


def test_one_commit_per_write(reg):
    linked(reg)
    before = commits(reg)
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert commits(reg) == before + 1
    reg.set_market(OTHER_HORDE_AH, 2, "Owner", by_id=OWNER)
    assert commits(reg) == before + 2
    reg.clear_market("Owner", by_id=OWNER)
    assert commits(reg) == before + 3
    with pytest.raises(RegistryError):
        reg.clear_market("Owner", by_id=OWNER)
    assert commits(reg) == before + 3, "a refusal writes nothing"


def test_the_consequences_say_what_changes(reg):
    linked(reg)
    first = cq.market_consequence(reg, HORDE_AH)
    assert "Horde Auction House" in first[0] and len(first) <= 3
    assert cq.market_clear_consequence(reg) == ["No auction house is picked for this guild."]
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert "already" in cq.market_consequence(reg, HORDE_AH)[0]
    assert "instead" in cq.market_consequence(reg, OTHER_HORDE_AH)[0]
    assert any("Prices turn off" in s for s in cq.market_clear_consequence(reg))
