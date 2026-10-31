"""/me professions · /roster professions (design §5.60): the wizard hub driven end to end through the harness — every
screen linted, every write ending on the decide screen with consequences.profession_consequence's sentences."""
import asyncio
import types

import discord

from oibot_gm import wizard_flows  # noqa: F401 — registers every flow
from oibot_gm.wizard import FLOWS
from test_professions import LABEL, OFFICER_UID, OTHER_LABEL, OTHER_UID, UID, _give
from wizard_harness import Harness


def _as(reg, uid):
    m = reg.members[uid]
    return Harness(reg, None, user=types.SimpleNamespace(id=uid, display_name=m.display_name, mention="@m"))


def _options(h, placeholder):
    sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == placeholder)
    return [o.value for o in sel.options]


def _char(reg, uid, label):
    return next(c for c in reg.members[uid].active() if c.label == label)


def test_me_professions_adds_a_profession_a_skill_and_a_recipe(reg):
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        assert LABEL in h.text and "no professions on record" in h.text
        await h.press("Add profession…")
        await h.pick("Pick the profession", "leatherworking")
        await h.pick("Pick the skill", "300")
        assert "Your Leatherworking is recorded at 300." in h.text and h.buttons() == ["Do it", "Back"]  # no note: the character verb keeps none
        await h.press("Do it")
        assert "✅" in h.text
        await FLOWS["me_professions"](h.command(), reg)
        assert "Leatherworking 300" in h.text
        await h.press("Add recipe…")
        await h.submit(h.modal, recipe="Devilsaur Gauntlets")
        assert "Your Leatherworking is recorded at 300 with 1 notable recipe." in h.text
        await h.press("Do it")
        assert "✅" in h.text
    asyncio.run(go())
    c = _char(reg, UID, LABEL)
    assert c.professions[0].skill == 300 and c.professions[0].recipes[0].name == "Devilsaur Gauntlets"


def test_a_full_trade_list_offers_only_secondary_professions(reg):
    _give(reg, UID, LABEL, [{"id": "alchemy", "skill": 1}, {"id": "mining", "skill": 1}])
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        assert "Two trades are taken. Cooking, First Aid and Fishing are open." in h.text
        await h.press("Add profession…")
        values = _options(h, "Pick the profession")
        assert "leatherworking" not in values and "herbalism" not in values
        assert set(values) == {"cooking", "first_aid", "fishing"}
    asyncio.run(go())


def test_change_skill_and_remove_end_on_the_consequence(reg):
    _give(reg, UID, LABEL, [{"id": "alchemy", "skill": 150, "recipes": [{"name": "Elixir of Fortitude"}]}, {"id": "fishing", "skill": 50}])
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Change skill…")
        await h.pick("Pick the profession", "alchemy")
        await h.pick("Pick the skill", "150")
        assert "already as given" in h.text and "Back" in h.buttons()  # unchanged: refused before the decide screen
        await h.press("Back")
        await h.press("Change skill…")
        await h.pick("Pick the profession", "alchemy")
        await h.pick("Pick the skill", "225")
        assert "Your Alchemy is recorded at 225 with 1 notable recipe." in h.text
        await h.press("Do it")
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Remove…")
        await h.pick("Pick what to remove", "p:fishing")
        assert "Your Fishing is removed from your record." in h.text
        await h.press("Do it")
    asyncio.run(go())
    c = _char(reg, UID, LABEL)
    assert [(p.id, p.skill) for p in c.professions] == [("alchemy", 225)] and c.professions[0].recipes[0].name == "Elixir of Fortitude"


def test_add_recipe_asks_which_profession_and_refuses_a_gatherer_only_character(reg):
    _give(reg, UID, LABEL, [{"id": "mining", "skill": 300}, {"id": "fishing", "skill": 10}])
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        assert "Add recipe…" not in h.buttons()  # nothing on the record makes things
    asyncio.run(go())
    _give(reg, UID, LABEL, [{"id": "alchemy", "skill": 300}, {"id": "cooking", "skill": 300}])

    async def go2():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add recipe…")
        assert set(_options(h, "Which profession makes it?")) == {"alchemy", "cooking"}
        await h.pick("Which profession makes it?", "cooking")
        await h.submit(h.modal, recipe="Smoked Desert Dumplings")
        assert "Your Cooking is recorded at 300 with 1 notable recipe." in h.text
    asyncio.run(go2())


def test_roster_professions_is_the_officers_form(reg, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    h = Harness(reg, None, user=types.SimpleNamespace(id=OFFICER_UID, display_name="Officer", mention="@o"))
    other = reg.members[OTHER_UID]
    target = types.SimpleNamespace(id=OTHER_UID, display_name=other.display_name)

    async def go():
        await FLOWS["roster_professions"](h.command(), reg, member=target)
        assert other.display_name in h.text and OTHER_LABEL in h.text
        await h.press("Add profession…")
        await h.pick("Pick the profession", "tailoring")
        await h.pick("Pick the skill", "275")
        assert f"{other.display_name}'s Tailoring is recorded at 275." in h.text
        await h.press("Do it")
        assert "✅" in h.text
    asyncio.run(go())
    assert [(p.id, p.skill) for p in _char(reg, OTHER_UID, OTHER_LABEL).professions] == [("tailoring", 275)]


def test_roster_professions_refuses_a_non_officer(reg, monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: False)
    h = _as(reg, UID)
    other = reg.members[OTHER_UID]

    async def go():
        await FLOWS["roster_professions"](h.command(), reg, member=types.SimpleNamespace(id=OTHER_UID, display_name=other.display_name))
        assert "Officers only" in h.text
    asyncio.run(go())
    assert not _char(reg, OTHER_UID, OTHER_LABEL).professions


# ---- /professions who · /professions list (Task 13): read-only, ephemeral, names only, no model call

def _offline_tree(reg):
    """The command tree built offline exactly as scripts/check_commands.py builds it (no Discord connection)."""
    from discord import app_commands

    from oibot_gm.discord_help import register_help_commands
    from oibot_gm.discord_policy import PolicyContext, register_policy_commands
    from oibot_gm.discord_raid import register_raid_commands
    from oibot_gm.discord_registry import Guilds, register_commands
    from oibot_gm.ops import Ops

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    guilds = Guilds.of(reg.store, reg)

    class Bot:
        raids = type("R", (), {"store": lambda self, r: None})()

    register_commands(tree, guilds, Ops(client), lambda k, v: "")
    register_raid_commands(tree, guilds, Ops(client), Bot())
    register_policy_commands(tree, guilds, Ops(client), Bot(), PolicyContext(guilds))
    register_help_commands(tree, guilds, Ops(client), Bot())
    return tree


class _Resp:
    def __init__(self):
        self.sent = []

    def is_done(self):
        return bool(self.sent)

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw))


def _ix(reg, **ns):
    return types.SimpleNamespace(extras={"guild": reg.key}, guild_id=None, namespace=types.SimpleNamespace(**ns),
                                 user=types.SimpleNamespace(id=UID, display_name="m"), response=_Resp(), client=None)


def _crafter(reg):
    reg.add_character(9100, "Thornwick", "Thornwick", "Hunter", "Survival", None, True)
    reg.save_character_table(9100, [{"label": "Thornwick", "professions": [{"id": "leatherworking", "skill": 300, "recipes": [{"name": "Devilsaur Gauntlets"}]}]}], "t", officer=True)


def _cmd(tree, path):
    group, name = path.split()
    return tree.get_command(group).get_command(name)


def test_professions_who_and_list_commands_exist_and_answer_from_the_catalog(reg):
    tree = _offline_tree(reg)
    paths = {c.qualified_name for c in tree.walk_commands()}
    assert {"professions who", "professions list"} <= paths
    choices = {c.value for c in _cmd(tree, "professions list")._params["profession"].choices}
    assert {"leatherworking", "alchemy", "cooking"} <= choices and len(choices) <= 25
    _crafter(reg)
    auto = _cmd(tree, "professions who")._params["recipe"].autocomplete
    got = asyncio.run(auto(_ix(reg), "devil"))
    assert [c.value for c in got] == ["devilsaur gauntlets"] and got[0].name.startswith("Devilsaur Gauntlets")
    assert asyncio.run(auto(_ix(reg), "xyz")) == []


def test_professions_who_names_the_crafters_and_refuses_an_unknown_recipe(reg):
    tree = _offline_tree(reg)
    _crafter(reg)
    who = _cmd(tree, "professions who")
    ix = _ix(reg)
    asyncio.run(who.callback(ix, recipe="devilsaur gauntlets"))
    text, kw = ix.response.sent[0]
    assert "Devilsaur Gauntlets" in text and "Thornwick" in text and "300" in text and "<@" not in text
    assert kw["ephemeral"] and kw["allowed_mentions"].to_dict()["parse"] == []
    ix = _ix(reg)
    asyncio.run(who.callback(ix, recipe="Thunderfury"))
    text, kw = ix.response.sent[0]
    assert text == "Nobody has registered that recipe yet — crafters add theirs with /me professions." and kw["ephemeral"]


def test_professions_list_shows_crafters_or_says_how_to_add_one(reg):
    tree = _offline_tree(reg)
    lst = _cmd(tree, "professions list")
    ix = _ix(reg)
    asyncio.run(lst.callback(ix, profession="leatherworking"))
    text, kw = ix.response.sent[0]
    assert text.startswith("Nobody has registered Leatherworking yet.") and kw["ephemeral"]
    _crafter(reg)
    ix = _ix(reg)
    asyncio.run(lst.callback(ix, profession="leatherworking"))
    text, kw = ix.response.sent[0]
    assert "**Leatherworking** · 1 crafter" in text and "Thornwick" in text and kw["ephemeral"]
    assert kw["allowed_mentions"].to_dict()["parse"] == []
