"""Parity B1, batch 3: the plain-text `character` op grows a planned add (no name; set_plan, so a re-planned main keeps
rosters and rank), a named add that can be the main in one op, a hard delete, and flex roles — all through
Registry.save_character_table, the verb the Me and Members pages (and MCP save_characters) use. Member self-service
stays bounded by bind_self; a newcomer still gets APPLY_FIRST. No Discord, no LLM."""
from __future__ import annotations

import pytest
from conftest import OWNER

from oibot_gm import configops
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError

GENERAL_CH = 900_003


def apply(reg, **kw):
    return configops.apply(reg, ConfigOp(op="character", **kw), "Officer", is_owner=False)


def dps(reg):
    return next(m for m in reg.test_members() if reg.profile.spec(m.main.cls, m.main.spec).role in ("melee", "ranged"))


def test_planned_add_starts_a_record_and_replans_the_main(reg):
    uid = 5_550_000_001
    op = ConfigOp(op="character", member=f"<@{uid}>", field="add", value="priest holy", text="main")
    assert configops.describe(reg, op) == "you: plan Priest Holy (main)"
    assert configops.apply(reg, op, "Newbie", is_owner=False) == "Newbie: planned Priest Holy"
    m = reg.members[uid]
    c = m.main
    assert (c.name, c.cls, c.spec, c.status, c.is_main, c.rank) == (None, "Priest", "Holy", "planned", True, "trial")
    reg.set_rank(uid, "raider", "t")  # D25: the member's rank (stored on the main)
    # re-planning the main replaces it and keeps its rank
    op = ConfigOp(op="character", member=f"<@{uid}>", field="add", value="Priest Discipline Holy", text="main")
    assert configops.describe(reg, op) == "Newbie: plan Priest Discipline/Holy (main) — replaces the planned Priest Holy"
    configops.apply(reg, op, "Newbie", is_owner=False)
    m = reg.members[uid]
    assert [(c.spec, c.offspec, c.rank) for c in m.active()] == [("Discipline", "Holy", "raider")]
    # a planned alt goes next to it; with no text the slot is alt once there is a main
    apply(reg, member=f"<@{uid}>", field="add", value="Mage Frost")
    assert sorted((c.cls, c.is_main) for c in reg.members[uid].active()) == [("Mage", False), ("Priest", True)]


def test_named_add_can_be_the_main_in_one_op(reg):
    m = reg.test_members()[0]
    op = ConfigOp(op="character", member=m.display_name, character="Jonny Blaze", field="add", value="Mage Fire", text="main")
    assert configops.describe(reg, op) == f"{m.display_name}: add Jonny Blaze (Mage Fire, main)"
    out = configops.apply(reg, op, "Officer", is_owner=False)
    assert out == f"{m.display_name}: added Jonny Blaze"  # added as the main (add_character is_main)
    assert reg.members[m.discord_id].main.label == "Jonny Blaze"
    assert "text is main | alt" in configops.describe(reg, ConfigOp(op="character", member=m.display_name, character="X Y", field="add", value="Mage Fire", text="sometimes"))
    with pytest.raises(RegistryError, match="text is main"):
        apply(reg, member=m.display_name, character="X Y", field="add", value="Mage Fire", text="sometimes")


def test_delete_removes_the_character_and_describe_warns(reg):
    m = reg.test_members()[0]
    old_main = m.main.label
    apply(reg, member=m.display_name, character="Jonny Blaze", field="add", value="Mage Fire")
    line = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character="Jonny Blaze", field="delete"))
    assert "DELETE Jonny Blaze outright" in line and "retire keeps it" in line and "main" not in line.split("—")[-1]
    assert apply(reg, member=m.display_name, character="Jonny Blaze", field="delete") == f"{m.display_name}: deleted Jonny Blaze"
    assert all(c.label != "Jonny Blaze" for c in reg.members[m.discord_id].characters), "gone outright, not retired"
    # deleting the main: the describe says the next character becomes main, and it does
    apply(reg, member=m.display_name, character="Alt Two", field="add", value="Mage Frost")
    line = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=old_main, field="delete"))
    assert "the next character becomes main" in line
    apply(reg, member=m.display_name, character=old_main, field="delete")
    assert reg.members[m.discord_id].main.label == "Alt Two"


def test_flex_add_and_clear(reg):
    m = dps(reg)
    label = m.main.label
    line = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=label, field="flex", value="tank and heal"))
    assert line == f"{label}: flex none → healer, tank"
    assert apply(reg, member=m.display_name, character=label, field="flex", value="tank, heal") == f"{m.display_name}: {label}: flex healer, tank"
    assert reg.members[m.discord_id].main.flex == ["healer", "tank"]
    own = reg.profile.spec(m.main.cls, m.main.spec).role
    line = configops.describe(reg, ConfigOp(op="character", member=m.display_name, character=label, field="flex", value=f"tank, {own}"))
    assert f"({own} already comes from the spec)" in line
    assert apply(reg, member=m.display_name, character=label, field="flex", value="none") == f"{m.display_name}: {label}: flex none"
    assert reg.members[m.discord_id].main.flex == []
    with pytest.raises(RegistryError, match="flex roles are"):
        apply(reg, member=m.display_name, character=label, field="flex", value="dancer")


def test_flex_rides_the_character_table_for_the_web_and_mcp(reg):
    m = dps(reg)
    label = m.main.label
    assert reg.save_character_table(m.discord_id, [{"label": label, "flex": ["tank"]}], "t") == [f"{label}: flex tank"]
    assert reg.save_character_table(m.discord_id, [{"label": label, "cls": m.main.cls, "spec": m.main.spec}], "t") == [], "no flex key: unchanged"
    assert reg.members[m.discord_id].main.flex == ["tank"]
    assert reg.save_character_table(m.discord_id, [{"label": label, "flex": []}], "t") == [f"{label}: flex none"]
    with pytest.raises(RegistryError, match="list of roles"):
        reg.save_character_table(m.discord_id, [{"label": label, "flex": "tank"}], "t")


def test_member_self_service_stays_their_own(reg):
    me, other = reg.test_members()[:2]
    mine, theirs = me.main.label, other.main.label

    def check(op, author=me.discord_id, officer=False):
        allowed, refused = configops.authorize(reg, [op], author, officer=officer, channel=GENERAL_CH)
        return refused[0] if refused else None

    assert check(ConfigOp(op="character", field="delete", character=mine)) is None
    assert check(ConfigOp(op="character", field="flex", character=mine, value="tank")) is None
    assert check(ConfigOp(op="character", field="add", value="Priest Holy", text="alt")) is None, "a planned alt of my own"
    assert check(ConfigOp(op="character", field="delete", character=theirs)) == f"{theirs} isn't one of your characters"
    assert "someone else's characters" in check(ConfigOp(op="character", member=other.display_name, field="delete", character=theirs))
    assert check(ConfigOp(op="character", member=other.display_name, field="delete", character=theirs), author=OWNER, officer=True) is None


def test_a_newcomer_still_applies_first(reg):
    for op in (ConfigOp(op="character", field="add", value="Priest Holy", text="main"),
               ConfigOp(op="character", field="add", character="Nyx Starfall", value="Priest Holy", text="main")):
        allowed, refused = configops.authorize(reg, [op], 4_242_000_009, officer=False, channel=GENERAL_CH)
        assert not allowed and refused == [configops.APPLY_FIRST]
