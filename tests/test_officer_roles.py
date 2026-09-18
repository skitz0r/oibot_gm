"""Officer roles are Discord role IDS (audit S6): a rename keeps the officers, a same-named role someone else creates
grants nothing. The legacy name list migrates to ids once, against a guild's roles; until then names still match so
nobody is locked out on the first start after the upgrade."""
import asyncio
from dataclasses import dataclass, field

import pytest
import yaml

from conftest import GUILD, OFFICER_ROLE

from oibot_gm import configops
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import GuildConfig, Registry, RegistryError


@dataclass
class Role:
    id: int
    name: str
    position: int = 0


@dataclass
class Guild:
    roles: list[Role] = field(default_factory=list)


OFFICER = Role(OFFICER_ROLE, "Officer", 5)
RAIDER = Role(300, "Raider", 3)
GUILD_ROLES = Guild([Role(1, "@everyone"), RAIDER, OFFICER, Role(400, "Council", 4)])


def cfg(**over) -> GuildConfig:
    return GuildConfig(key="g", name="G", game_profile="forever", discord_guild_id=1, **over)


# ---- GuildConfig.officer_by_roles: ids decide

def test_ids_match_regardless_of_name():
    c = cfg(officer_role_ids=[OFFICER_ROLE])
    assert c.officer_by_roles([RAIDER, OFFICER])
    assert c.officer_by_roles([Role(OFFICER_ROLE, "Renamed Officers")]), "a rename keeps the officer"
    assert not c.officer_by_roles([Role(999, "Officer")]), "a same-named role with another id grants nothing"
    assert not c.officer_by_roles([RAIDER]) and not c.officer_by_roles([])


def test_legacy_names_only_while_no_id_is_configured():
    c = cfg(officer_roles=["Officer"])
    assert c.officer_by_roles([Role(999, "Officer")]), "unresolved names still match (first start before resolution)"
    assert not c.officer_by_roles([RAIDER])
    c = cfg(officer_roles=["Officer"], officer_role_ids=[400])
    assert not c.officer_by_roles([Role(999, "Officer")]), "once an id exists, names grant nothing"
    assert c.officer_by_roles([Role(400, "Council")])
    assert not cfg().officer_by_roles([OFFICER]), "nothing configured: Manage Server only (checked by the caller)"


# ---- Registry.resolve_officer_roles: names → ids, once

def legacy_registry(reg: Registry, names: list[str], ids: list[int] | None = None) -> Registry:
    p = reg.store.root / GUILD / "guild.yaml"
    d = yaml.safe_load(p.read_text())
    d["officer_roles"], d["officer_role_ids"] = names, ids or []
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return Registry(reg.store, GUILD, reg.profile)


def test_migration_names_to_ids(reg):
    r = legacy_registry(reg, ["Officer", "Council"])
    assert r.config.officer_role_ids == [] and r.config.officer_roles == ["Officer", "Council"]
    assert r.config.officer_by_roles([Role(999, "Officer")]), "before resolution: by name"
    head = r.store.head()
    names = r.resolve_officer_roles(GUILD_ROLES)
    assert names == ["Officer", "Council"]
    assert r.config.officer_role_ids == [OFFICER_ROLE, 400] and r.config.officer_roles == []
    assert r.store.head() != head, "one commit"
    assert yaml.safe_load((r.store.root / GUILD / "guild.yaml").read_text())["officer_role_ids"] == [OFFICER_ROLE, 400]
    assert not r.config.officer_by_roles([Role(999, "Officer")]), "after resolution: by id"
    assert r.config.officer_by_roles([Role(OFFICER_ROLE, "Anything")])
    head = r.store.head()
    assert r.resolve_officer_roles(GUILD_ROLES) == ["Officer", "Council"]
    assert r.store.head() == head, "idempotent: nothing left to migrate, no commit"
    assert Registry(r.store, GUILD, r.profile).config.officer_role_ids == [OFFICER_ROLE, 400]


def test_migration_keeps_unresolved_names(reg):
    r = legacy_registry(reg, ["Officer", "Ghost"])
    assert r.resolve_officer_roles(GUILD_ROLES) == ["Officer", "Ghost"]
    assert r.config.officer_role_ids == [OFFICER_ROLE] and r.config.officer_roles == ["Ghost"], "the unknown name is kept, visibly, not dropped"
    assert r.config.officer_roles_pending() == ["Ghost"]
    assert not r.config.officer_by_roles([Role(999, "Ghost")]), "but grants nothing once an id is configured"
    r.resolve_officer_roles(Guild([*GUILD_ROLES.roles, Role(500, "Ghost")]))  # the role appears later (on_guild_role_create)
    assert r.config.officer_role_ids == [OFFICER_ROLE, 500] and r.config.officer_roles == []


def test_nothing_resolves_without_matching_roles(reg):
    r = legacy_registry(reg, ["Officer"])
    head = r.store.head()
    assert r.resolve_officer_roles(Guild([RAIDER])) == ["Officer"]
    assert r.config.officer_roles == ["Officer"] and r.config.officer_role_ids == [] and r.store.head() == head
    assert r.config.officer_by_roles([Role(999, "Officer")]), "still by name: nobody is locked out"


def test_officer_role_names_display(reg):
    assert reg.officer_role_names() == [f"role:{OFFICER_ROLE}"], "no guild seen yet: the id is shown"
    reg.cache_roles(GUILD_ROLES)
    assert reg.officer_role_names() == ["Officer"]


# ---- set_officer_role / configops role_add, role_remove

def test_set_officer_role_by_id_mention_and_name(reg):
    reg.cache_roles(GUILD_ROLES)
    assert reg.set_officer_role("<@&400>", True, "t") == "officer roles = Officer, Council"
    assert reg.config.officer_role_ids == [OFFICER_ROLE, 400]
    assert reg.set_officer_role("Council", False, "t") == "officer roles = Officer"
    assert reg.set_officer_role("council", True, "t").endswith("Officer, Council"), "names match case-insensitively"
    assert reg.set_officer_role(str(OFFICER_ROLE), False, "t") == "officer roles = Council"
    assert reg.config.officer_role_ids == [400]
    with pytest.raises(RegistryError, match="role mention, id or name"):
        reg.set_officer_role("", True, "t")


def test_configops_role_ops(reg):
    reg.cache_roles(GUILD_ROLES)
    add = ConfigOp(op="role_add", value="<@&400>")
    assert configops.describe(reg, add) == "officer roles ['Officer'] + Council"
    with pytest.raises(RegistryError, match="needs the owner"):
        configops.apply(reg, add, "officer", is_owner=False)
    assert configops.apply(reg, add, "owner", is_owner=True) == "officer roles = Officer, Council"
    assert configops.apply(reg, ConfigOp(op="role_remove", value="Officer"), "owner", is_owner=True) == "officer roles = Council"
    assert reg.config.officer_role_ids == [400] and reg.config.officer_roles == []


def test_configops_unknown_name_is_parked_until_the_bot_resolves_it(reg):
    op = ConfigOp(op="role_add", value="Loot Council")
    assert "by name" in configops.describe(reg, op) and "resolves" in configops.describe(reg, op)
    assert configops.apply(reg, op, "owner", is_owner=True) == f"officer roles = role:{OFFICER_ROLE}, Loot Council"
    assert reg.config.officer_roles == ["Loot Council"] and reg.config.officer_role_ids == [OFFICER_ROLE]
    assert not reg.config.officer_by_roles([Role(600, "Loot Council")]), "a parked name grants nothing yet"
    reg.resolve_officer_roles(Guild([*GUILD_ROLES.roles, Role(600, "Loot Council")]))  # the bot's next ready / role event
    assert reg.config.officer_role_ids == [OFFICER_ROLE, 600] and reg.config.officer_roles == []
    assert reg.config.officer_by_roles([Role(600, "Loot Council")])
    assert configops.apply(reg, ConfigOp(op="role_remove", value="Loot Council"), "owner", is_owner=True) == "officer roles = Officer"


def test_apply_async_resolves_against_the_bot_guild(reg):
    class Bot:
        raids = None

        def get_guild(self, gid):
            return GUILD_ROLES if gid == reg.config.discord_guild_id else None

    out = asyncio.run(configops.apply_async(reg, ConfigOp(op="role_add", value="Council"), "owner", True, bot=Bot()))
    assert out == "officer roles = Officer, Council"
    assert reg.config.officer_role_ids == [OFFICER_ROLE, 400] and reg.config.officer_roles == []
