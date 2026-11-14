"""Auction prices in Discord (auction prices, spec §4.1, §4.4, §4.5): the owner's auction house inside `/gm config
armory`'s hub (look → pick → decide → set, Turn prices off…), `/price` for every member (autocomplete from memory only)
and the cost to make on `/professions who`. The same verbs as the site's routes. No network: a fake armory and a fake
auction client stand in for Blizzard; names are invented or the game's own."""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone

import pytest
from conftest import OWNER
from test_armory_guild import LinkBot
from test_prices import HORDE_AH, facts, priced, recipe
from test_prices_web import BAR, BOLT, MOONCLOTH, FakeArmory, FakeAuctions
from test_professions_flows import _cmd, _ix, _offline_tree
from wizard_harness import Harness

from oibot_gm import armory, discord_auctions, prices
from oibot_gm import consequences as cq
from oibot_gm import wizard_flows  # noqa: F401 — registers every flow
from oibot_gm.auctions import House, Price
from oibot_gm.wizard import FLOWS


def run(coro):
    return asyncio.run(coro)


def now() -> datetime:
    return datetime.now(timezone.utc)


class TwoHouses(FakeAuctions):
    """The Horde house of the fake, plus a second Horde house with listings (two houses of a ruleset share a name)."""

    HOUSES = [*FakeAuctions.HOUSES, House(id=20, name="Horde Auction House")]

    def table(self, ruleset_id, house_id, since=None):
        if house_id == 20:
            self.reads.append((ruleset_id, house_id))
            return {BAR: Price(low=510000, qty=4)}, None
        return super().table(ruleset_id, house_id, since)


@pytest.fixture
def market_on(monkeypatch, tmp_path):
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "test-id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(discord_auctions, "OUT", tmp_path / "auctions-out")
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    fa = TwoHouses()
    monkeypatch.setattr(armory, "client_for", lambda reg: FakeArmory())
    monkeypatch.setattr(discord_auctions, "_client", lambda reg: fa)
    discord_auctions.reset_state()
    prices.reset_names()
    yield fa
    discord_auctions.reset_state()
    prices.reset_names()


def harness(reg, rs, uid=OWNER, name="Owner"):
    return Harness(reg, rs, user=types.SimpleNamespace(id=uid, display_name=name, mention=f"@{name}"), client=LinkBot())


def audit(h) -> list[str]:
    return [t for _k, _lvl, t in h.client.ops.lines]


# ---------------------------------------------------------------- the owner's picker inside /gm config armory


def test_the_hub_offers_the_auction_house_only_with_a_linked_guild(reg, rs, market_on, monkeypatch):
    h = harness(reg, rs)
    run(FLOWS["armory_guild"](h.command(), reg))
    assert h.modal is not None, "no guild linked: the name box, never an auction house button"
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    h = harness(reg, rs)
    run(FLOWS["armory_guild"](h.command(), reg))
    assert h.buttons() == ["Change…", "Unlink…", "Auction house…", "Done"], "no house picked: nothing to turn off"
    assert "No auction house is picked" in h.text
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    h = harness(reg, rs)
    run(FLOWS["armory_guild"](h.command(), reg))
    assert h.buttons() == ["Change…", "Unlink…", "Auction house…", "Turn prices off…", "Done"]
    assert "Horde Auction House · picked by Owner" in h.text and discord_auctions.WAITING_WORDS in h.text and ".." not in h.text
    # without the Blizzard keys the armory command refuses before the hub, as it always did
    monkeypatch.setattr(armory, "available", lambda reg: False)
    h = harness(reg, rs)
    run(FLOWS["armory_guild"](h.command(), reg))
    assert h.screen["view"] is None and "Auction house" not in h.text


def test_pick_decide_set_and_the_rev_is_checked(reg, rs, market_on):
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    h = harness(reg, rs)

    async def go():
        await FLOWS["armory_guild"](h.command(), reg)
        await h.press("Auction house…")
        assert h.selects() == ["Which auction house"]
        sel = h._items()[0]
        labels = [o.label for o in sel.options]
        assert labels == ["Auction house 1 of 2", "Auction house 2 of 2"], "the empty and other factions' houses are left out"
        d1 = sel.options[0].description
        assert d1 == "230 listed in all · 48g 99s 98c · 40s 99c · none listed" and all(len(o.description) <= 100 for o in sel.options)
        for word in ("Arcanite Bar", "Dreamfoil", "Runecloth"):
            assert word in h.text, "the screen names the three items the prices are for"
        for word in ("house id", "ruleset", "commodities"):
            assert word not in h.text.lower() and word not in d1
        await h.pick("Which auction house", "1")
        assert h.text.endswith(" ".join(cq.market_consequence(reg, HORDE_AH))) and reg.config.market is None, "nothing before the press"
        assert h.buttons() == ["Pick this auction house", "Back"]
        await h.press("Pick this auction house")
        assert "✅" in h.text and "Turn prices off…" in h.buttons(), "back on the hub with the house picked"
        await asyncio.sleep(0.05)  # the first read of its prices runs in the background
    run(go())
    m = reg.config.market
    assert (m.house_id, m.ruleset_id, m.picked_by) == (6, 2, "Owner")
    assert sum("picked the Horde Auction House" in t for t in audit(h)) == 1
    assert discord_auctions.state(reg) == "fresh", "the picker's read became the guild's first table"

    # the same house again: said so, nothing written
    before = reg.config_rev("settings")
    h2 = harness(reg, rs)

    async def same():
        await FLOWS["armory_guild"](h2.command(), reg)
        await h2.press("Auction house…")
        await h2.pick("Which auction house", "1")
        assert "already the guild's auction house" in h2.text and "Auction house…" in h2.buttons()
    run(same())
    assert reg.config_rev("settings") == before and len(audit(h2)) == 0

    # the settings changed under the open wizard: the pick is refused, nothing written
    h3 = harness(reg, rs)

    async def stale():
        await FLOWS["armory_guild"](h3.command(), reg)
        await h3.press("Auction house…")
        await h3.pick("Which auction house", "2")
        reg.set_market(House(id=20, name="Horde Auction House"), 2, "Owner", by_id=OWNER)  # another surface, meanwhile
        reg.clear_market("Owner", by_id=OWNER)
        await h3.press("Pick this auction house")
        assert "❌" in h3.text
    run(stale())
    assert reg.config.market is None and len(audit(h3)) == 0


def test_the_picker_says_why_it_has_nothing_to_offer(reg, rs, market_on, monkeypatch):
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    monkeypatch.setattr(market_on, "HOUSES", [House(id=2, name="Alliance Auction House")])
    h = harness(reg, rs)

    async def empty():
        await FLOWS["armory_guild"](h.command(), reg)
        await h.press("Auction house…")
        assert discord_auctions.NO_CHOICES in h.text and "Try again" in h.buttons()
    run(empty())

    def down(ruleset_id):
        raise armory.ArmoryDown(retry_after=60)
    monkeypatch.setattr(market_on, "houses", down)
    h = harness(reg, rs)

    async def outage():
        await FLOWS["armory_guild"](h.command(), reg)
        await h.press("Auction house…")
        assert armory.DOWN_WORDS in h.text and "Try again" in h.buttons()
    run(outage())
    assert reg.config.market is None


def test_turn_prices_off(reg, rs, market_on):
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    h = harness(reg, rs)

    async def go():
        await FLOWS["armory_guild"](h.command(), reg)
        await h.press("Turn prices off…")
        assert h.text.endswith(" ".join(cq.market_clear_consequence(reg))) and reg.config.market is not None
        assert h.buttons() == ["Turn prices off", "Back"]
        await h.press("Turn prices off")
        assert "✅" in h.text and "Turn prices off…" not in h.buttons()
    run(go())
    assert reg.config.market is None
    assert sum("turned prices off" in t for t in audit(h)) == 1


def test_an_officer_never_reaches_the_picker(reg, rs, market_on):
    from test_loot_web import OFFICER

    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    h = harness(reg, rs, uid=OFFICER, name="Officer")
    run(FLOWS["armory_guild"](h.command(), reg))
    assert "Owner only" in h.text and h.screen["view"] is None


# ---------------------------------------------------------------- /price


def test_price_answers_one_item_several_and_the_off_sentence(reg, market_on):
    tree = _offline_tree(reg)
    cmd = tree.get_command("price")
    assert cmd is not None and len(cmd.description) <= 100

    def ask(item, uid=None):
        ix = _ix(reg)
        if uid is not None:
            ix.user = types.SimpleNamespace(id=uid, display_name="o")
        run(cmd.callback(ix, item=item))
        text, kw = ix.response.sent[0]
        assert kw["ephemeral"] and kw["allowed_mentions"].to_dict()["parse"] == []
        return text

    assert ask("arcanite bar") == discord_auctions.OFF_WORDS, "prices off: that sentence alone"
    assert ask("arcanite bar", OWNER) == prices.OWNER_OFF_WORDS, "the owner is told how to turn them on"
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    assert ask("arcanite bar") == discord_auctions.WAITING_WORDS
    priced(reg, {BAR: Price(low=489998, qty=30)}, at=now())
    one = ask(f"#{BAR}").splitlines()  # the autocomplete's value: that item
    assert one[0].startswith("Prices as of <t:") and one[0].endswith(":R>"), "a native stamp: the reader's own clock"
    assert one[1:] == ["Arcanite Bar: lowest price 48g 99s 98c, 30 listed."]
    several = ask("a").splitlines()
    assert len(several) == 12 and several[-1].startswith("And ") and several[-1].endswith(" more items match."), "ten lines and how many more"
    assert ask("dreamfoil").splitlines()[1] == "Dreamfoil: none listed."
    assert ask("zzqx").splitlines()[1] == prices.NO_MATCH
    assert ask("#999999").splitlines()[1] == prices.NO_MATCH
    priced(reg, {BAR: Price(low=489998, qty=30)}, at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert ask("arcanite bar") == discord_auctions.OLD_WORDS, "more than a day old: that sentence alone"


def test_autocomplete_never_waits_on_blizzard(reg, market_on, monkeypatch):
    tree = _offline_tree(reg)
    auto = tree.get_command("price")._params["item"].autocomplete

    def no_network(*a, **k):
        raise AssertionError("autocomplete asked Blizzard")
    monkeypatch.setattr(discord_auctions, "_client", no_network)
    monkeypatch.setattr(armory, "client_for", no_network)
    assert run(auto(_ix(reg), "arcanite")) == [], "prices off: no choices, never an error"
    priced(reg, {BAR: Price(low=489998, qty=30)}, at=now())
    got = run(auto(_ix(reg), "arcanite bar"))
    assert got[0].name == "Arcanite Bar" and got[0].value == f"#{BAR}"
    assert len(run(auto(_ix(reg), "a"))) == 25 and run(auto(_ix(reg), "")) == []


# ---------------------------------------------------------------- /professions who


def test_who_adds_the_cost_only_when_prices_are_on(reg, market_on):
    tree = _offline_tree(reg)
    reg.add_character(9100, "Thornwick", "Thornwick", "Mage", "Frost", None, True)
    reg.save_character_table(9100, [{"label": "Thornwick", "professions": [{"id": "tailoring", "skill": 300, "recipes": [{"name": "Mooncloth Bag"}]}]}], "t", officer=True)
    who = _cmd(tree, "professions who")

    def ask(recipe):
        ix = _ix(reg)
        run(who.callback(ix, recipe=recipe))
        return ix.response.sent[0][0]

    off = ask("mooncloth bag")
    assert "Thornwick" in off and "Cost to make" not in off and "Prices as of" not in off
    priced(reg, {BOLT: Price(low=5000, qty=40), MOONCLOTH: Price(low=12000, qty=3)}, at=now())
    on = ask("mooncloth bag")
    assert "Cost to make: at least 3g 20s — 4 reagents have none listed." in on and "Prices as of <t:" in on
    assert on.index("Mooncloth Bag") < on.index("Cost to make") < on.index("Thornwick")
    # a recipe nobody makes still gains its cost
    nobody = ask("#" + str(reg.profile.recipes_named("mooncloth robe")[0].id))
    assert nobody.startswith("Nobody in the guild makes Mooncloth Robe yet.") and "Cost to make" in nobody
    # a recipe with no reagents (the game's list has none today): no cost line, never 0c
    assert prices.cost_line(reg, recipe(makes={"id": BAR, "name": "Arcanite Bar", "quantity": 1})) == ""
    assert prices.cost_line(reg, None) == ""


# ---------------------------------------------------------------- a game version without auction houses (tbc)


def test_a_version_without_auction_houses_has_no_price_anywhere_in_discord(reg, rs, market_on, monkeypatch):
    from test_armory_guild import ROOT as _ROOT

    from oibot_gm.profiles import GameProfile

    p = GameProfile.load(_ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    assert not p.auctions
    tree = _offline_tree(reg)
    ix = _ix(reg)
    ix.user = types.SimpleNamespace(id=OWNER, display_name="Owner")
    run(tree.get_command("price").callback(ix, item="arcanite bar"))
    assert ix.response.sent[0][0] == prices.NO_AUCTIONS_WORDS == "This game version has no auction houses."
    # /professions who reads exactly as it did before prices (tbc has no professions: its one sentence, no cost line)
    ix = _ix(reg)
    run(_cmd(tree, "professions who").callback(ix, recipe="mooncloth bag"))
    assert ix.response.sent[0][0] == "Nobody has registered that recipe yet — crafters add theirs with /me professions."
    # /gm config armory: the version has no armory, so no hub and no auction house button
    h = harness(reg, rs)
    run(FLOWS["armory_guild"](h.command(), reg))
    assert h.text.endswith(armory.NO_ARMORY_WORDS) and h.modal is None
    assert "Auction house…" not in (h.buttons() if h.screen.get("view") else []) and "Auction house" not in h.text
