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


def _no_list(reg):
    """A version without the game's list: typed recipes, as before the armory link."""
    import dataclasses
    reg.profile = dataclasses.replace(reg.profile, recipes={})


def test_me_professions_adds_a_profession_a_skill_and_a_recipe(reg):
    """Without the game's list the recipe is typed, as before."""
    _no_list(reg)
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
        assert h.modal.title == "Leatherworking: add a recipe", "never 'A Alchemy recipe'"
        assert [f.label for f in h.modal._fields] == ["The recipe's name"]
        await h.submit(h.modal, recipe="Devilsaur Gauntlets")
        assert "Your Leatherworking is recorded at 300 with 1 notable recipe." in h.text
        await h.press("Do it")
        assert "✅" in h.text
    asyncio.run(go())
    c = _char(reg, UID, LABEL)
    assert c.professions[0].skill == 300 and c.professions[0].recipes[0].name == "Devilsaur Gauntlets"


def test_add_recipe_searches_the_list_within_the_profession(reg):
    """armory link §4.4: Add recipe… = part of the name (prose) → a select of the list's recipes of THAT profession
    (value = the recipe id, the category tells twins apart) → the decide screen; no hit → NOT_A_RECIPE on the same
    screen with Search again…."""
    _give(reg, UID, LABEL, [{"id": "leatherworking", "skill": 300}, {"id": "alchemy", "skill": 300}])
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add recipe…")
        await h.pick("Which profession makes it?", "leatherworking")
        assert h.modal.title == "Leatherworking: add a recipe" and [f.label for f in h.modal._fields] == ["Part of the recipe's name"]
        await h.submit(h.modal, recipe="mooncloth")  # a Tailoring recipe: not in this profession
        assert "That isn't a recipe in the game's list." in h.text and "Search again…" in h.buttons() and "Back" in h.buttons()
        await h.press("Search again…")
        await h.submit(h.modal, recipe="devilsaur")
        sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == "Pick the recipe")
        assert [(o.value, o.label, o.description) for o in sel.options] == [("10789", "Devilsaur Gauntlets", "Leather Gauntlets"),
                                                                             ("10798", "Devilsaur Leggings", "Leather Legguards")]
        await h.pick("Pick the recipe", "10789")
        assert "Your Leatherworking is recorded at 300 with 1 notable recipe." in h.text and h.buttons() == ["Do it", "Back"]
        await h.press("Do it")
        assert "✅" in h.text
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add recipe…")
        await h.pick("Which profession makes it?", "alchemy")
        await h.submit(h.modal, recipe="lesser arcane elixir")
        sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == "Pick the recipe")
        assert len(sel.options) == 2 and len({o.label for o in sel.options}) == 1 and len({o.description for o in sel.options}) == 2, "twins told apart by category"
    asyncio.run(go())
    c = _char(reg, UID, LABEL)
    assert [(r.id, r.name) for r in c.professions[0].recipes] == [(10789, "Devilsaur Gauntlets")]


def test_add_recipe_says_how_many_match_past_a_select(reg):
    _give(reg, UID, LABEL, [{"id": "tailoring", "skill": 300}])
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add recipe…")
        await h.submit(h.modal, recipe="robe")
        sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == "Pick the recipe")
        assert len(sel.options) == 25
        n = sum(1 for r in reg.profile.recipes_of("tailoring") if any(w.startswith("robe") for w in r.key.split()))
        assert n > 25 and f"{n} recipes match. The first 25 are listed. Search again to narrow it down." in h.text
    asyncio.run(go())


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
        sel = next(x for x in h._items() if isinstance(x, discord.ui.Select) and x.placeholder == "Pick what to remove")
        assert {o.description for o in sel.options if o.value.startswith("r:")} == {"Alchemy recipe"}
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
        assert "Add recipe…" in h.buttons(), "R7: the game's list gives Mining its smelting"
        await h.press("Add recipe…")
        assert set(_options(h, "Which profession makes it?")) == {"mining", "fishing"}
    asyncio.run(go())
    _give(reg, UID, LABEL, [{"id": "alchemy", "skill": 300}, {"id": "cooking", "skill": 300}])

    async def go2():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add recipe…")
        assert set(_options(h, "Which profession makes it?")) == {"alchemy", "cooking"}
        await h.pick("Which profession makes it?", "cooking")
        await h.submit(h.modal, recipe="Smoked Desert Dumplings")
        await h.pick("Pick the recipe", "13056")
        assert "Your Cooking is recorded at 300 with 1 notable recipe." in h.text
    asyncio.run(go2())
    _no_list(reg)
    _give(reg, UID, LABEL, [{"id": "mining", "skill": 300}, {"id": "fishing", "skill": 10}])

    async def go3():
        await FLOWS["me_professions"](h.command(), reg)
        assert "Add recipe…" not in h.buttons()  # without a list: nothing on the record makes things
    asyncio.run(go3())


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
    got = asyncio.run(auto(_ix(reg), "devil"))  # the game's whole list, what somebody makes first; a listed recipe's value is its id
    assert got[0].value == "#10789" and got[0].name == "Devilsaur Gauntlets · Leather Gauntlets · 1 crafter"
    assert len(got) > 1 and all(c.name.endswith("nobody makes it yet") for c in got[1:]) and all(len(c.name) <= 100 for c in got)
    twins = asyncio.run(auto(_ix(reg), "lesser arcane elixir"))
    assert len(twins) == 2 and len({c.name for c in twins}) == 2, "same name, told apart by category"
    assert [c.value for c in asyncio.run(auto(_ix(reg), ""))] == ["#10789"], "an empty box offers what somebody makes"
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
    asyncio.run(who.callback(ix, recipe="#10789"))  # the autocomplete's value: that recipe
    assert "Thornwick" in ix.response.sent[0][0]
    ix = _ix(reg)
    asyncio.run(who.callback(ix, recipe="Thunderfury"))
    text, kw = ix.response.sent[0]
    assert text == "That isn't a recipe in the game's list." and kw["ephemeral"]


def test_professions_who_for_a_listed_recipe_nobody_makes(reg):
    tree = _offline_tree(reg)
    _crafter(reg)
    ix = _ix(reg)
    asyncio.run(_cmd(tree, "professions who").callback(ix, recipe="#" + str(reg.profile.recipes_named("ironfeather shoulders")[0].id)))
    text, kw = ix.response.sent[0]
    assert text == "Nobody in the guild makes Ironfeather Shoulders yet. A crafter adds it with /me professions." and kw["ephemeral"]
    ix = _ix(reg)
    asyncio.run(_cmd(tree, "professions who").callback(ix, recipe="ironfeather shoulders"))
    assert ix.response.sent[0][0] == text, "typed, not picked: the same answer"


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


def test_professions_who_stays_under_discords_limit_and_says_how_many_more(reg):
    tree = _offline_tree(reg)
    recipes = [{"name": "Devilsaur Gauntlets"}, {"name": "Devilsaur Leggings"}, {"name": "Ironfeather Shoulders"}]
    for i in range(10):
        uid = 9200 + i
        label = "Crafterxxx" + "abcdefghij"[i]
        reg.add_character(uid, f"A rather long display name for crafter number {i:02d}", label, "Hunter", "Survival", None, True)
        reg.save_character_table(uid, [{"label": label, "professions": [{"id": "leatherworking", "skill": 300 - i, "recipes": recipes}]}], "t", officer=True)
    ix = _ix(reg)
    asyncio.run(_cmd(tree, "professions who").callback(ix, recipe="devilsaur gauntlets leggings ironfeather shoulders"))
    text, kw = ix.response.sent[0]
    assert len(text) < 2000
    assert text.splitlines()[-1].startswith("…and ") and text.endswith("more — the Professions page has them all.")
    shown = sum(1 for line in text.splitlines() if line.startswith("• "))
    from oibot_gm import professions as pr
    hits = pr.who_can_make(reg, "devilsaur gauntlets leggings ironfeather shoulders")
    nobody = sum(1 for e in hits if not e.crafters)  # recipes of the game's list the text names that nobody makes: one line each, after the crafted
    assert [bool(e.crafters) for e in hits[:3]] == [True] * 3
    assert int(text.splitlines()[-1].split()[1]) == 30 + nobody - shown


def test_professions_who_counts_every_match_past_the_first_25(reg):
    """A part of a name that matches hundreds of recipes: the closing line counts all of them, not only the 25 the
    finder used to stop at."""
    tree = _offline_tree(reg)
    _crafter(reg)
    ix = _ix(reg)
    asyncio.run(_cmd(tree, "professions who").callback(ix, recipe="a"))
    text, _ = ix.response.sent[0]
    lines = text.splitlines()
    from oibot_gm import professions as pr
    hits = pr.who_can_make(reg, "a", None)
    assert len(hits) > 100
    units = sum(max(1, len(e.crafters)) for e in hits)
    shown = sum(1 for line in lines[:-2] if not line.startswith("**"))
    assert lines[-1].startswith("…and ") and int(lines[-1].split()[1]) == units - shown


def test_autocomplete_label_without_a_list_is_unchanged(reg):
    _no_list(reg)
    tree = _offline_tree(reg)
    _crafter(reg)
    auto = _cmd(tree, "professions who")._params["recipe"].autocomplete
    got = asyncio.run(auto(_ix(reg), "devil"))
    assert [(c.name, c.value) for c in got] == [("Devilsaur Gauntlets · 1 crafter", "devilsaur gauntlets")]


def test_professions_who_escapes_member_text_and_survives_a_profession_left_out_of_the_profile(reg):
    tree = _offline_tree(reg)
    reg.add_character(9300, "Star*Man_", "Starman", "Hunter", "Survival", None, True)
    from oibot_gm.registry import Profession, Recipe

    m = reg.members[9300]  # a recipe typed before the game's list (kept unmatched): member text the reply escapes
    m.main.professions = [Profession(id="leatherworking", skill=300, recipes=[Recipe(name="Bold *Gloves*")])]
    reg.save(m, "typed recipe")
    ix = _ix(reg)
    asyncio.run(_cmd(tree, "professions who").callback(ix, recipe="bold gloves"))
    text, _ = ix.response.sent[0]
    assert "Star\\*Man\\_" in text and "Bold \\*Gloves\\*" in text
    saved = dict(reg.profile.professions)
    reg.profile.professions.pop("leatherworking")
    try:
        ix = _ix(reg)
        asyncio.run(_cmd(tree, "professions who").callback(ix, recipe="bold gloves"))
        text, _ = ix.response.sent[0]
        assert text.splitlines()[0] == "**Bold \\*Gloves\\***"
    finally:
        reg.profile.professions.clear()  # the profile may be shared by the session's fixtures: restored in its order
        reg.profile.professions.update(saved)


def test_a_skill_may_be_left_not_said_and_the_hub_says_so(reg):
    h = _as(reg, UID)

    async def go():
        await FLOWS["me_professions"](h.command(), reg)
        await h.press("Add profession…")
        await h.pick("Pick the profession", "leatherworking")
        assert _options(h, "Pick the skill")[0] == "unsaid", "Not said yet is the first choice"
        await h.pick("Pick the skill", "unsaid")
        assert "Your Leatherworking is recorded." in h.text
        await h.press("Do it")
        await FLOWS["me_professions"](h.command(), reg)
        assert "Leatherworking** · skill not said yet" in h.text
    asyncio.run(go())
    assert _char(reg, UID, LABEL).professions[0].skill is None
