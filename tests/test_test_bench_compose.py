"""The Members page's test bench: puppets by composition (class/spec × count), removing chosen ones, and a comp
sandbox — every puppet joined to a far-off test run that is never posted."""
from collections import Counter

import pytest
from conftest import join, lock_with_board, open_test_run
from test_mcp import bearer, client  # noqa: F401  (the web app on a fake bot, as the owner)

from oibot_gm.registry import RegistryError


def mains(reg):
    return Counter((m.main.cls, m.main.spec) for m in reg.test_members())


def test_add_by_composition_names_are_legal_and_unique(reg):
    before = len(reg.test_members())
    made = reg.add_test_members([{"cls": "Warrior", "spec": "Protection", "count": 3}, {"cls": "Priest", "spec": "Holy", "offspec": "Discipline", "count": 2}], "t")
    assert len(made) == 5 and len(reg.test_members()) == before + 5
    names = [m.main.name for m in made]
    assert len(set(n.lower() for n in names)) == 5 and all(n.isalpha() and 2 <= len(n) <= 12 for n in names)
    assert all(m.test and m.main.confirmed_by == "t" and m.main.is_main for m in made)
    assert made[-1].main.offspec == "Discipline"
    assert all(reg.find(n)[0] is m for n, m in zip(names, made))  # every name resolves to exactly its puppet


def test_add_refuses_bad_specs_and_the_cap_before_writing(reg):
    before = len(reg.test_members())
    for rows, why in (([{"cls": "Warrior", "spec": "Holy", "count": 1}], "no spec"), ([{"cls": "Bard", "spec": "Lute", "count": 1}], "isn't a class"),
                      ([{"cls": "Warrior", "spec": "Arms", "count": reg.TEST_MAX}], "holds")):
        with pytest.raises(RegistryError, match=why):
            reg.add_test_members(rows, "t")
    assert len(reg.test_members()) == before


def test_remove_only_test_members(reg):
    made = reg.add_test_members([{"cls": "Mage", "spec": "Frost", "count": 2}], "t")
    real, _ = reg.add_character(4242, "Realperson", "Realperson", "Mage", "Fire", None, True)
    with pytest.raises(RegistryError, match="Only test members"):
        reg.remove_test_members([made[0].discord_id, real.discord_id], "t")
    assert made[0].discord_id in reg.members  # nothing removed when any id is refused
    assert reg.remove_test_members([m.discord_id for m in made], "t") == 2 and not any(m.discord_id in reg.members for m in made)


def test_compose_endpoint_adds_removes_and_clears_open_sheets(client, reg, rs):  # noqa: F811
    made = reg.add_test_members([{"cls": "Rogue", "spec": "Combat", "count": 2}], "t")
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, made)
    ev.layout = [[made[0].display_name, "Someone"]]
    r = client.post("/api/admin/test", json={"action": "compose", "add": [{"cls": "Druid", "spec": "Restoration", "count": 2}], "remove": [str(made[0].discord_id)]}, headers=bearer())
    assert r.status_code == 200, r.text and "2 added, 1 removed" in r.json()["message"]
    ev = rs.events[ev.key]
    assert str(made[0].discord_id) not in ev.signups and made[0].display_name not in ev.layout[0]
    assert mains(reg)[("Druid", "Restoration")] >= 2


def test_compose_refuses_a_puppet_rostered_on_a_locked_run(client, reg, rs):  # noqa: F811
    ev = open_test_run(reg, rs)
    ms = reg.test_members()[:10]
    join(reg, rs, ev, ms)
    lock_with_board(reg, rs, ev, [m.display_name for m in ms])
    r = client.post("/api/admin/test", json={"action": "compose", "remove": [str(ms[0].discord_id)]}, headers=bearer())
    assert r.status_code == 400 and "locked run" in r.json()["error"] and ms[0].discord_id in reg.members


def test_comp_sandbox_joins_every_puppet_and_posts_nothing(client, reg, rs):  # noqa: F811
    rid = next(iter(reg.profile.raids))
    r = client.post("/api/admin/test", json={"action": "comp", "raid": rid}, headers=bearer())
    assert r.status_code == 200, r.text
    ev = rs.events[r.json()["key"]]
    assert ev.message_id is None and (reg.config.roster(ev.team) or {}).get("test")
    assert {int(k) for k, sg in ev.signups.items() if sg.status == "in"} == {m.discord_id for m in reg.test_members()}
    assert (ev.start - reg.now_local()).days >= 13


def test_plain_text_add_remove_and_comp(reg, rs):
    import asyncio
    from types import SimpleNamespace

    from oibot_gm import configops as co

    op = lambda v: co.ConfigOp(op="test", value=v)  # noqa: E731
    assert "add 3 Protection Warrior, 1 Holy Priest" in co.describe(reg, op("add 3 warrior protection, 1 Priest holy/discipline"))
    before = mains(reg)[("Warrior", "Protection")]
    assert "4 added" in co.apply(reg, op("add 3 Warrior Protection, 1 Priest Holy/Discipline"), "t", True)
    assert mains(reg)[("Warrior", "Protection")] == before + 3
    assert "Warrior specs are Arms, Fury, Protection" in co.describe(reg, op("add 2 Warrior Holy"))  # describe reads a refusal out
    assert "not 99" in co.describe(reg, op("remove 99 Warrior Protection"))

    async def refresh(reg, ev):
        pass

    bot = SimpleNamespace(raids=SimpleNamespace(store=lambda r: rs), refresh_sheet=refresh)
    line = asyncio.run(co._apply_with_bot(reg, op("remove 2 Warrior Protection"), "t", bot, 1))
    assert "2 removed" in line and mains(reg)[("Warrior", "Protection")] == before + 1
    line = asyncio.run(co._apply_with_bot(reg, op(f"comp {next(iter(reg.profile.raids))}"), "t", bot, 1))
    assert line.startswith("comp sandbox ")
