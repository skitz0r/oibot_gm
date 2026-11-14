"""Item names, the price search, a recipe's cost to make and the words (auction prices, spec §3, §4.3–§4.5, C6, C9, C10).
A hand-built price table stands in for the auction house, a fake client for Blizzard's item names, a fake clock for time;
item and guild names are invented or the game's own."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import OWNER

from oibot_gm import armory, auctions, battlenet, discord_auctions, prices
from oibot_gm.armory import Armory, ArmoryDown, GuildFacts, Reply
from oibot_gm.auctions import House, Price
from oibot_gm.profiles import GameProfile, RecipeDef

ROOT = Path(__file__).resolve().parents[1]
TBC = GameProfile.load(ROOT / "profiles" / "tbc")
HORDE_AH = House(id=6, name="Horde Auction House")
NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
BAR, DREAMFOIL, VIAL, LAB = 12360, 13463, 8925, 279990  # Arcanite Bar, Dreamfoil, Crystal Vial, Alchemy Laboratory


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class FakeNames:
    """The auction client's item_name, recorded: `names` answers, a missing id is a 404 (None), `fail_after` raises."""

    def __init__(self, names=None, fail_after=None):
        self.names = dict(names or {})
        self.asked: list[int] = []
        self.fail_after = fail_after

    def item_name(self, item_id):
        if self.fail_after is not None and len(self.asked) >= self.fail_after:
            raise ArmoryDown(detail="503")
        self.asked.append(item_id)
        return self.names.get(item_id)


class FakeOps:
    def __init__(self):
        self.lines = []

    async def emit(self, cfg, level, text, exc=None, dm=True, dm_only=None):
        self.lines.append((level, text))


@pytest.fixture
def clock(monkeypatch, tmp_path):
    c = Clock()
    monkeypatch.setattr(discord_auctions, "_now", c)
    monkeypatch.setattr(discord_auctions, "OUT", tmp_path / "auctions-out")
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    discord_auctions.reset_state()
    prices.reset_names()
    yield c
    discord_auctions.reset_state()
    prices.reset_names()


def facts() -> GuildFacts:
    return GuildFacts(id=418362, slug="lantern-wardens", name="Lantern Wardens", faction="Horde", kind="Normal", ruleset_id=2,
                      members=8, created=NOW)


def priced(reg, table: dict[int, Price], *, at=NOW):
    """The guild picks the Horde house and its table is `table`, read at `at`."""
    if reg.config.armory is None:
        reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    if reg.config.market is None:
        reg.set_market(HORDE_AH, 2, "Owner", by_id=OWNER)
    discord_auctions._st(reg)["table"] = discord_auctions.Table(at=at, house_id=6, ruleset_id=2, prices=dict(table), modified=None)
    return reg


def recipe(makes=None, reagents=()) -> RecipeDef:
    return RecipeDef(id=1, name="A Test Recipe", profession="alchemy", category="Tests", makes=makes, reagents=list(reagents), key="a test recipe")


def items_file() -> Path:
    return discord_auctions.OUT / "items.json"


# ------------------------------------------------------------------ names


def test_names_come_from_recipes_items_then_the_cache(reg, clock):
    names = prices.names(reg)
    assert names[DREAMFOIL] == "Dreamfoil" and names[LAB] == "Alchemy Laboratory", "what a recipe makes and its reagents"
    # the cache fills only what nothing local names, and is kept per game version
    items_file().parent.mkdir(parents=True)
    items_file().write_text(json.dumps({reg.profile.name: {"900001": "Kettle Lid", str(DREAMFOIL): "Wrong Name"},
                                        "tbc": {"900002": "Another Version's Thing"}}), encoding="utf-8")
    prices.reset_names()
    names = prices.names(reg)
    assert names[900001] == "Kettle Lid"
    assert names[DREAMFOIL] == "Dreamfoil", "the recipe list wins over the cache"
    assert 900002 not in names, "two versions never share names"
    # a version with loot items (tbc): the item data names them; a recipe list names before it
    item_id, item = next(iter(TBC.items.items()))
    tbc = SimpleNamespace(profile=TBC)
    assert prices.names(tbc)[item_id] == item.name
    assert prices.names(tbc)[900002] == "Another Version's Thing"
    fake = SimpleNamespace(profile=SimpleNamespace(name="madeup", items={7: SimpleNamespace(name="Item Name")}, known_items={8: "Known"},
                                                   recipes={1: recipe(makes={"id": 7, "name": "Recipe Name", "quantity": 1})}))
    assert prices.names(fake) == {7: "Recipe Name", 8: "Known"}


def test_item_name_reads_the_static_namespace_and_a_404_is_none():
    calls = []

    def fetch(url, headers):
        calls.append(url)
        if "/item/7" in url:
            return Reply(200, {}, json.dumps({"id": 7, "name": "Arcanite Bar"}).encode())
        return Reply(404, {}, b"")

    arm = Armory("us", "profile-forever-us", "static-forever-us", fetch=fetch, limiter=battlenet.Limiter(1000))
    cli = auctions.Client(arm, "dynamic-forever-us")
    assert cli.item_name(7) == "Arcanite Bar"
    assert "/data/wow/item/7?" in calls[0] and "namespace=static-forever-us" in calls[0]
    assert cli.item_name(8) is None


# ------------------------------------------------------------------ the search


def test_search_matches_every_word_listed_first_and_counts_the_total(reg, clock):
    priced(reg, {BAR: Price(low=489998, qty=30), DREAMFOIL: Price(low=1500, qty=40)})
    assert prices.search(reg, "") == ([], 0)
    assert prices.search(reg, "   ") == ([], 0)
    hits, total = prices.search(reg, "ARCANITE bar")
    assert hits[0] == prices.ItemPrice(id=BAR, name="Arcanite Bar", low=489998, qty=30)
    assert all("arcanite" in h.name.casefold() and "bar" in h.name.casefold() for h in hits)
    assert total == len(hits)
    hits, total = prices.search(reg, "dream")
    assert hits[0].id == DREAMFOIL, "a listed item comes first"
    unlisted = [h for h in hits if h.low is None]
    assert unlisted and all(h.qty == 0 for h in unlisted)
    assert [h.name for h in unlisted] == sorted(h.name for h in unlisted), "then by name"
    hits, total = prices.search(reg, "a", limit=5)
    assert len(hits) == 5 and total > 5, "the total counts every match, past the limit"


def test_search_is_empty_when_prices_are_off_or_too_old(reg, clock):
    assert prices.search(reg, "arcanite") == ([], 0), "no market picked"
    priced(reg, {BAR: Price(low=489998, qty=30)}, at=NOW - timedelta(hours=25))
    assert discord_auctions.state(reg) == "old"
    assert prices.search(reg, "arcanite") == ([], 0)
    assert prices.recipe_cost(reg, recipe(reagents=[{"id": BAR, "name": "Arcanite Bar", "quantity": 1}])) is None
    priced(reg, {BAR: Price(low=489998, qty=30)}, at=NOW - timedelta(hours=7))
    assert discord_auctions.state(reg) == "stale"
    assert prices.search(reg, "arcanite bar")[1] >= 1, "a stale table still shows"


# ------------------------------------------------------------------ cost to make


def test_cost_of_an_enchant_a_range_and_a_missing_reagent(reg, clock):
    priced(reg, {BAR: Price(low=100000, qty=3), DREAMFOIL: Price(low=1200, qty=40), LAB: Price(low=32000, qty=14)})
    enchant = prices.recipe_cost(reg, recipe(reagents=[{"id": BAR, "name": "Arcanite Bar", "quantity": 1},
                                                       {"id": DREAMFOIL, "name": "Dreamfoil", "quantity": 2}]))
    assert enchant.made is None and (enchant.makes_min, enchant.makes_max) == (0, 0)
    assert enchant.total == 102400 and enchant.missing == 0
    assert enchant.lines[1] == prices.CostLine(id=DREAMFOIL, name="Dreamfoil", quantity=2, unit=1200, total=2400)
    assert prices.made_words(enchant) == ""
    ranged = prices.recipe_cost(reg, recipe(makes={"id": LAB, "name": "Alchemy Laboratory", "quantity": 2, "quantity_max": 4},
                                            reagents=[{"id": DREAMFOIL, "name": "Dreamfoil", "quantity": 1}]))
    assert (ranged.makes_min, ranged.makes_max) == (2, 4)
    assert ranged.made == prices.ItemPrice(id=LAB, name="Alchemy Laboratory", low=32000, qty=14)
    assert ranged.total == 1200, "per craft: what it makes is never added or subtracted"
    short = prices.recipe_cost(reg, recipe(makes={"id": LAB, "name": "Alchemy Laboratory", "quantity": 1},
                                           reagents=[{"id": DREAMFOIL, "name": "Dreamfoil", "quantity": 3},
                                                     {"id": VIAL, "name": "Crystal Vial", "quantity": 1}]))
    assert short.missing == 1 and short.total == 3600
    assert short.lines[1] == prices.CostLine(id=VIAL, name="Crystal Vial", quantity=1, unit=None, total=None)
    assert prices.cost_sentence(short) == "Cost to make: at least 36s — 1 reagent has none listed."


def test_a_reagent_listed_twice_is_one_line(reg, clock):
    priced(reg, {DREAMFOIL: Price(low=1000, qty=40)})
    cost = prices.recipe_cost(reg, recipe(reagents=[{"id": DREAMFOIL, "name": "Dreamfoil", "quantity": 2},
                                                    {"id": VIAL, "name": "Crystal Vial", "quantity": 1},
                                                    {"id": DREAMFOIL, "name": "Dreamfoil", "quantity": 3}]))
    assert [(c.id, c.quantity) for c in cost.lines] == [(DREAMFOIL, 5), (VIAL, 1)]
    assert cost.lines[0].total == 5000 and cost.total == 5000 and cost.missing == 1


# ------------------------------------------------------------------ the words


def test_the_sentences(reg, clock):
    assert prices.NONE_LISTED == "none listed"
    bar = prices.ItemPrice(id=BAR, name="Arcanite Bar", low=489998, qty=30)
    assert prices.price_words(bar) == "Arcanite Bar: lowest price 48g 99s 98c, 30 listed."
    assert prices.price_words(prices.ItemPrice(id=BAR, name="Arcanite Bar", low=None, qty=0)) == "Arcanite Bar: none listed."

    def cost(lines, made=None, lo=0, hi=0):
        return prices.Cost(lines=lines, total=sum(c.total or 0 for c in lines), missing=sum(c.unit is None for c in lines),
                           made=made, makes_min=lo, makes_max=hi)

    known = prices.CostLine(id=1, name="A", quantity=1, unit=124000, total=124000)
    gone = prices.CostLine(id=2, name="B", quantity=1, unit=None, total=None)
    gone2 = prices.CostLine(id=3, name="C", quantity=2, unit=None, total=None)
    assert prices.cost_sentence(cost([known])) == "Cost to make: 12g 40s."
    assert prices.cost_sentence(cost([known, gone])) == "Cost to make: at least 12g 40s — 1 reagent has none listed."
    assert prices.cost_sentence(cost([known, gone, gone2])) == "Cost to make: at least 12g 40s — 2 reagents have none listed."
    assert prices.cost_sentence(cost([gone, gone2])) == "Cost to make: not known — none of the reagents are listed."
    lab = prices.ItemPrice(id=LAB, name="Alchemy Laboratory", low=32000, qty=14)
    assert prices.made_words(cost([known], lab, 1, 1)) == "Makes 1 · lowest price 3g 20s each, 14 listed"
    assert prices.made_words(cost([known], lab, 2, 4)) == "Makes 2 to 4 · lowest price 3g 20s each, 14 listed"
    none = prices.ItemPrice(id=LAB, name="Alchemy Laboratory", low=None, qty=0)
    assert prices.made_words(cost([known], none, 1, 1)) == "Makes 1 · none listed"
    assert prices.made_words(cost([known])) == ""
    # how recent, or why there are none
    assert prices.status_sentence(reg, owner=True) == prices.OWNER_LINK_WORDS, "no guild linked: link it first"
    assert prices.status_sentence(reg, owner=False) == ""
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    assert prices.status_sentence(reg, owner=True) == "Pick the guild's auction house on the Config page to see prices."
    assert prices.status_sentence(reg, owner=False) == ""
    priced(reg, {})
    assert prices.status_sentence(reg, owner=False) == discord_auctions.as_of_words(reg)
    assert prices.status_sentence(reg, owner=True).startswith("Prices as of ")
    discord_auctions._st(reg)["table"] = None
    assert discord_auctions.state(reg) == "waiting"
    assert prices.status_sentence(reg, owner=False) == discord_auctions.WAITING_WORDS
    priced(reg, {}, at=NOW - timedelta(hours=30))
    assert prices.status_sentence(reg, owner=False) == discord_auctions.OLD_WORDS


def test_the_owner_is_never_told_to_pick_a_house_that_cannot_be_picked(reg, clock, monkeypatch):
    """F-1: the owner's 'pick the auction house' only where the picker exists (auction houses, the keys, a linked guild);
    without a linked guild, link it first; with no auction houses or no keys, nothing — and /price says there are none."""
    assert prices.OWNER_LINK_WORDS == "Link the server to its guild in the game on the Config page first, then pick its auction house to see prices."
    assert prices.status_sentence(reg, owner=True) == prices.OWNER_LINK_WORDS
    assert prices.price_answer(reg, "arcanite bar", owner=True) == [prices.OWNER_LINK_WORDS]
    assert prices.price_answer(reg, "arcanite bar", owner=False) == [discord_auctions.OFF_WORDS]
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    assert prices.status_sentence(reg, owner=True) == prices.OWNER_OFF_WORDS
    assert prices.price_answer(reg, "arcanite bar", owner=True) == [prices.OWNER_OFF_WORDS]
    # a host without the Blizzard keys: no picker, so nothing to tell the owner
    monkeypatch.setattr(armory, "available", lambda reg: False)
    assert prices.status_sentence(reg, owner=True) == "" and prices.status_sentence(reg, owner=False) == ""
    assert prices.price_answer(reg, "arcanite bar", owner=True) == [auctions.NO_AUCTIONS_WORDS]
    assert prices.price_answer(reg, "arcanite bar", owner=False) == [auctions.NO_AUCTIONS_WORDS]
    # a version without auction houses
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    monkeypatch.setattr(reg, "profile", TBC)
    assert prices.status_sentence(reg, owner=True) == ""
    assert prices.price_answer(reg, "arcanite bar", owner=True) == [auctions.NO_AUCTIONS_WORDS]


# ------------------------------------------------------------------ the naming step


def test_naming_stops_quietly_on_a_failure_and_caps_per_fetch(reg, clock, monkeypatch):
    unnamed = {900000 + i: Price(low=100, qty=i) for i in range(1, 251)}  # 250 ids nothing names, qty 1..250
    priced(reg, {BAR: Price(low=489998, qty=999), **unnamed})
    fake = FakeNames({i: f"Kettle Part {i}" for i in unnamed})
    monkeypatch.setattr(discord_auctions, "_client", lambda r: fake)
    bot = SimpleNamespace(ops=FakeOps())
    assert run(discord_auctions.name_items(bot, reg)) == discord_auctions.NAME_PER_FETCH == 200
    assert BAR not in fake.asked, "an id the recipe list names is never asked"
    assert fake.asked[:3] == [900250, 900249, 900248], "most listed first"
    assert len(fake.asked) == 200
    assert prices.names(reg)[900250] == "Kettle Part 900250"
    saved = json.loads(items_file().read_text(encoding="utf-8"))
    assert saved[reg.profile.name]["900250"] == "Kettle Part 900250"
    assert not [p for p in items_file().parent.iterdir() if p.name != "items.json" and p.is_file()], "written whole"
    prices.reset_names()  # a restart: the file names them again
    assert prices.names(reg)[900250] == "Kettle Part 900250"
    # the next fetch names the rest; a 404 is not asked again in this process
    fake.asked.clear()
    del fake.names[900001]
    assert run(discord_auctions.name_items(bot, reg)) == 49
    assert len(fake.asked) == 50
    fake.asked.clear()
    assert run(discord_auctions.name_items(bot, reg)) == 0 and fake.asked == []
    # a failure stops it quietly: what was named before it is kept, no ops line
    more = {800000 + i: Price(low=100, qty=i) for i in range(1, 11)}
    priced(reg, more)
    failing = FakeNames({i: f"Lid {i}" for i in more}, fail_after=3)
    monkeypatch.setattr(discord_auctions, "_client", lambda r: failing)
    assert run(discord_auctions.name_items(bot, reg)) == 3
    assert prices.names(reg)[800010] == "Lid 800010" and 800007 not in prices.names(reg)
    assert bot.ops.lines == []
    # the hourly check names after it reads the table, in the same background task
    discord_auctions.reset_state()
    prices.reset_names()
    reader = SimpleNamespace(
        table=lambda ruleset, house, since=None: ({700001: Price(low=5, qty=2)}, None),
        item_name=lambda i: "Spare Cog")
    monkeypatch.setattr(discord_auctions, "_client", lambda r: reader)

    async def tick():
        await (await discord_auctions.auctions_tick(bot, reg))
    run(tick())
    assert prices.names(reg)[700001] == "Spare Cog"


def test_a_malformed_item_is_skipped_and_naming_goes_on(reg, clock, monkeypatch):
    listed = {600000 + i: Price(low=100, qty=i) for i in range(1, 6)}  # 600005 is the most listed
    priced(reg, listed)
    asked = []

    def fetch(url, headers):
        item = int(url.split("/data/wow/item/")[1].split("?")[0])
        asked.append(item)
        body = {"id": item} if item == 600005 else {"id": item, "name": f"Gear {item}"}  # one answer lacks its name
        return Reply(200, {}, json.dumps(body).encode())

    arm = Armory("us", "profile-forever-us", "static-forever-us", fetch=fetch, limiter=battlenet.Limiter(1000))
    cli = auctions.Client(arm, "dynamic-forever-us")
    monkeypatch.setattr(discord_auctions, "_client", lambda r: cli)
    bot = SimpleNamespace(ops=FakeOps())
    assert run(discord_auctions.name_items(bot, reg)) == 4, "the malformed one is skipped, the rest are named"
    assert asked[0] == 600005 and prices.names(reg)[600001] == "Gear 600001"
    assert 600005 not in prices.names(reg) and bot.ops.lines == []
    asked.clear()
    assert run(discord_auctions.name_items(bot, reg)) == 0 and asked == [], "it is not asked first again every hour"


# ------------------------------------------------------------------ the help answer's section (C5)

THORIUM, CRYSTAL, ENCH_THORIUM = 12359, 12363, 12655  # Thorium Bar, Arcane Crystal, Enchanted Thorium Bar


def ask(reg, question):
    from oibot_gm import help as help_mod

    return help_mod.Ask(reg=reg, rs=None, user_id=9100, officer=False, question=question)


def test_how_much_is_an_item_quotes_the_lowest_price(reg, clock):
    priced(reg, {BAR: Price(low=489998, qty=30), THORIUM: Price(low=900, qty=200), ENCH_THORIUM: Price(low=25000, qty=4)})
    q = "how much is an Arcanite Bar right now?"
    assert prices.help_hit(reg, q)
    lines = prices.help_section(ask(reg, q))
    assert lines[0].startswith("### Prices:") and "lowest price listed" in lines[0] and "worth" in lines[0], "the header says what the number is"
    assert lines[1] == discord_auctions.as_of_words(reg) and lines[1].startswith("Prices as of ")
    assert "Arcanite Bar: lowest price 48g 99s 98c, 30 listed." in lines
    assert "Arcanite Bar: lowest price 48g 99s 98c, 30 listed." in prices.help_section(ask(reg, "what do arcanite bars cost?")), "a plural"
    assert "Crystal Vial: none listed." in prices.help_section(ask(reg, "what's the price of a crystal vial")), "named, nothing up"
    longest = prices.help_section(ask(reg, "cheapest enchanted thorium bar?"))
    assert "Enchanted Thorium Bar: lowest price 2g 50s, 4 listed." in longest
    assert not any(l.startswith("Thorium Bar:") for l in longest), "a name inside a longer named one is not named again"
    from oibot_gm import help as help_mod
    s = help_mod.guild_state(reg, None, 9100, False, q, None)
    assert "### Prices:" in s and "Arcanite Bar: lowest price 48g 99s 98c, 30 listed." in s


def test_cost_to_make_a_named_recipe_lists_each_reagent(reg, clock):
    priced(reg, {BAR: Price(low=489998, qty=30), THORIUM: Price(low=900, qty=200)})
    lines = prices.help_section(ask(reg, "how much does it cost to make Transmute: Arcanite?"))
    text = "\n".join(lines)
    assert "Transmute: Arcanite (one craft):" in text
    assert "  - Thorium Bar × 1: lowest price 9s each, 9s in all" in lines
    assert "  - Arcane Crystal × 1: none listed" in lines
    assert "  Makes 1 · lowest price 48g 99s 98c each, 30 listed" in lines
    assert "  Cost to make: at least 9s — 1 reagent has none listed." in lines
    assert "0c" not in text, "never a zero cost"


def test_no_section_when_prices_are_off_or_no_item_is_named(reg, clock):
    from oibot_gm import help as help_mod

    q = "how much is an arcanite bar"
    assert not prices.help_hit(reg, q), "no auction house picked"
    assert "### Prices" not in help_mod.guild_state(reg, None, 9100, False, q, None)
    assert not prices.help_hit(SimpleNamespace(profile=TBC), q), "a version without auction houses"
    priced(reg, {BAR: Price(low=489998, qty=30)})
    assert not prices.help_hit(reg, "how much gold is in the guild bank?"), "no item named"
    assert not prices.help_hit(reg, "who has an arcanite bar?"), "no price asked"
    assert not prices.help_hit(reg, "is the price right on the next raid?"), "a price word, no item"
    # none read yet, or too old: only the line saying why
    discord_auctions._st(reg)["table"] = None
    assert prices.help_hit(reg, q)
    waiting = prices.help_section(ask(reg, q))
    assert discord_auctions.WAITING_WORDS in waiting and not any("lowest price" in l and ":" in l and "Arcanite" in l for l in waiting)
    assert len(waiting) == 2
    priced(reg, {BAR: Price(low=489998, qty=30)}, at=NOW - timedelta(hours=30))
    old = prices.help_section(ask(reg, q))
    assert old[1:] == [discord_auctions.OLD_WORDS], "a day-old table: why, never a price"
    assert "48g" not in help_mod.guild_state(reg, None, 9100, False, q, None)


def test_the_section_stays_under_its_cap(reg, clock):
    from oibot_gm import help as help_mod

    assert help_mod.SECTIONS["prices"][2] == 2_000
    named = sorted({n for n in prices.names(reg).values() if len(n.split()) == 2}, key=len, reverse=True)[:30]
    priced(reg, {i: Price(low=123456789, qty=9999) for i in prices.names(reg)})
    q = "how much are " + ", ".join(named) + " and what does it cost to make Transmute: Arcanite, Arcanite Rod, Heartseeker and Mooncloth Bag?"
    lines = prices.help_section(ask(reg, q))
    items = [l for l in lines if not l.startswith((" ", "###")) and ": lowest price" in l]
    assert len(items) <= prices.HELP_ITEMS == 10
    assert sum(1 for l in lines if l.endswith("(one craft):")) <= prices.HELP_RECIPES
    assert len("\n".join(lines)) <= 2_000, "the section fits its cap without a cut"


# ------------------------------------------------------------------ the watch list and the price history (price watch §3, §4.2)

UID = 300_000_000_000_000_101
PLAGUEBLOOM = 13466
DAY = timedelta(days=1)


@pytest.fixture
def member(reg):
    m = reg.member(UID, "Thornwick", create=True)
    reg.save(m, "Thornwick registered")
    return m


def keep(reg, at, table: dict[int, Price]):
    """The selected house's history keeps `table` at feed time `at`."""
    from oibot_gm import price_history

    assert price_history.record(discord_auctions.history_key(reg), at, table) >= 0


def test_watch_and_unwatch_sentences(reg, clock, member):
    from oibot_gm.registry import RegistryError

    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    assert prices.watch(reg, UID, PLAGUEBLOOM) == "You're watching Plaguebloom."
    assert prices.watch(reg, UID, PLAGUEBLOOM) == "You're watching Plaguebloom.", "harmless to repeat"
    assert prices.is_watching(reg, UID, PLAGUEBLOOM)
    assert prices.unwatch(reg, UID, PLAGUEBLOOM) == "You've stopped watching Plaguebloom."
    assert not prices.is_watching(reg, UID, PLAGUEBLOOM)
    with pytest.raises(RegistryError, match="No item the bot knows has that name."):
        prices.watch(reg, UID, 999_999)
    assert reg.watching(UID) == []


def test_watch_passes_on_the_registry_refusals(reg, clock):
    from oibot_gm.registry import WATCH_REGISTER_FIRST, RegistryError

    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    with pytest.raises(RegistryError, match=WATCH_REGISTER_FIRST):
        prices.watch(reg, UID, PLAGUEBLOOM)


def test_nothing_is_added_while_prices_are_hidden(reg, clock, member):
    """C15: watch refuses with the status sentence that says why while prices don't show; unwatch still trims."""
    from oibot_gm.registry import RegistryError

    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4), VIAL: Price(low=640, qty=200)})
    prices.watch(reg, UID, VIAL)
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)}, at=NOW - timedelta(hours=30))
    assert discord_auctions.state(reg) == "old"
    with pytest.raises(RegistryError) as e:
        prices.watch(reg, UID, PLAGUEBLOOM)
    assert str(e.value) == discord_auctions.as_of_words(reg) == discord_auctions.OLD_WORDS
    with pytest.raises(RegistryError, match=discord_auctions.OLD_WORDS):
        prices.watch(reg, UID, 999_999)  # not shown comes before NO_MATCH
    discord_auctions._st(reg)["table"] = None
    assert discord_auctions.state(reg) == "waiting"
    with pytest.raises(RegistryError) as e:
        prices.watch(reg, UID, PLAGUEBLOOM)
    assert str(e.value) == discord_auctions.WAITING_WORDS
    reg.clear_market("Owner", by_id=OWNER)
    assert discord_auctions.state(reg) == "off"
    with pytest.raises(RegistryError) as e:
        prices.watch(reg, UID, PLAGUEBLOOM)
    assert str(e.value) == discord_auctions.OFF_WORDS
    assert reg.watching(UID) == [VIAL], "nothing written"
    assert prices.unwatch(reg, UID, VIAL) == "You've stopped watching Crystal Vial.", "a list can always be trimmed"
    assert reg.watching(UID) == []


def test_watch_list_rows_in_order_with_prices(reg, clock, member):
    priced(reg, {VIAL: Price(low=640, qty=200), PLAGUEBLOOM: Price(low=1500, qty=4)})
    prices.watch(reg, UID, PLAGUEBLOOM)
    prices.watch(reg, UID, VIAL)
    rows = prices.watch_list(reg, UID)
    assert [(p.id, p.name, p.low, p.qty) for p in rows] == [(VIAL, "Crystal Vial", 640, 200), (PLAGUEBLOOM, "Plaguebloom", 1500, 4)]
    priced(reg, {VIAL: Price(low=640, qty=200)})
    assert [(p.id, p.low, p.qty) for p in prices.watch_list(reg, UID)][1] == (PLAGUEBLOOM, None, 0), "none listed now"
    assert prices.watch_list(reg, 300_000_000_000_000_999) == [], "someone with no record"


def test_watch_list_without_prices_shows_names_only(reg, clock, member):
    priced(reg, {VIAL: Price(low=640, qty=200)})
    prices.watch(reg, UID, VIAL)
    priced(reg, {VIAL: Price(low=640, qty=200)}, at=NOW - timedelta(hours=30))
    assert discord_auctions.state(reg) == "old"
    assert [(p.id, p.name, p.low, p.qty) for p in prices.watch_list(reg, UID)] == [(VIAL, "Crystal Vial", None, 0)]
    reg.clear_market("Owner", by_id=OWNER)
    assert discord_auctions.state(reg) == "off"
    assert [(p.name, p.low, p.qty) for p in prices.watch_list(reg, UID)] == [("Crystal Vial", None, 0)]


def test_a_watched_id_the_bot_cannot_name(reg, clock, member):
    priced(reg, {900_001: Price(low=99, qty=1)})
    reg.watch(UID, 900_001)  # named when it was watched; the names file is gone since
    [row] = prices.watch_list(reg, UID)
    assert (row.id, row.name, row.low) == (900_001, prices.UNNAMED, 99)
    assert prices.UNNAMED == "An item the bot can't name yet"
    assert prices.unwatch(reg, UID, 900_001) == "You've stopped watching that item."
    assert prices.watch_list(reg, UID) == []


def test_the_empty_list_sentences():
    assert prices.EMPTY_LIST_SITE == "You aren't watching anything yet. Search for an item above and press Watch."
    assert prices.EMPTY_LIST_DISCORD == "You aren't watching anything yet. Use /price with an item and press Watch."
    assert prices.SPANS == {"7": (7, "7 days"), "30": (30, "30 days"), "90": (90, "90 days"), "365": (365, "1 year")}


def test_history_sentences(reg, clock):
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    for days, low in ((40, 1800), (20, 1100), (15, 2500), (10, 1500), (5, 1240), (3, 1902)):
        keep(reg, NOW - days * DAY, {PLAGUEBLOOM: Price(low=low, qty=3)})
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == [
        "Lowest in the last 7 days: 12s 40c · highest: 19s 2c", "Lowest in the last 30 days: 11s · highest: 25s"]
    clock.t = NOW
    assert prices.history_sentences(reg, PLAGUEBLOOM) == prices.history_sentences(reg, PLAGUEBLOOM, now=NOW), "now = the bot's clock"


def test_a_short_history_says_since_when(reg, clock):
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    for days, low in ((2, 1500), (1, 1400), (0, 1600)):
        keep(reg, NOW - days * DAY, {PLAGUEBLOOM: Price(low=low, qty=3)})
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == [
        "Lowest in the last 7 days: 14s · highest: 16s", "Lowest in the last 30 days: 14s · highest: 16s",
        "The bot has kept prices since Wed 11 Nov."]


def test_nothing_listed_in_a_span(reg, clock):
    priced(reg, {VIAL: Price(low=640, qty=200)})
    keep(reg, NOW - 40 * DAY, {VIAL: Price(low=640, qty=200)})
    keep(reg, NOW - 20 * DAY, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    keep(reg, NOW - 10 * DAY, {})  # delisted ten days ago, nothing since
    keep(reg, NOW - DAY, {VIAL: Price(low=640, qty=200)})
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == [
        "None listed in the last 7 days.", "Lowest in the last 30 days: 15s · highest: 15s"]
    assert prices.history_sentences(reg, 900_002, now=NOW)[:2] == [
        "None listed in the last 7 days.", "None listed in the last 30 days."], "an item the house never listed"


def test_a_new_house_has_no_history_yet(reg, clock):
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    keep(reg, NOW - 3 * DAY, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW)[0].startswith("Lowest in the last 7 days")
    reg.set_market(House(id=7, name="Horde Auction House"), 2, "Owner", by_id=OWNER)  # the owner picks another house
    discord_auctions._st(reg)["table"] = discord_auctions.Table(at=NOW, house_id=7, ruleset_id=2, prices={}, modified=None)
    assert discord_auctions.state(reg) == "fresh"
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == [prices.NO_HISTORY_YET]
    assert prices.NO_HISTORY_YET == "The bot hasn't kept any prices for this auction house yet."
    assert prices.chart(reg, PLAGUEBLOOM, "7", now=NOW)["points"] == []


def test_history_is_hidden_when_prices_are(reg, clock):
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    keep(reg, NOW - 3 * DAY, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)}, at=NOW - timedelta(hours=30))
    assert discord_auctions.state(reg) == "old"
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == []
    assert prices.chart(reg, PLAGUEBLOOM, "7", now=NOW)["points"] == []
    reg.clear_market("Owner", by_id=OWNER)
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == []
    c = prices.chart(reg, PLAGUEBLOOM, "30", now=NOW)
    assert c == {"span": "30", "points": [], "low": None, "high": None, "low_words": "none listed", "high_words": "none listed",
                 "since": int((NOW - 30 * DAY).timestamp()), "until": int(NOW.timestamp())}, "the span's start and end even with no points"


def test_a_broken_history_file_reads_as_none(reg, clock):
    from oibot_gm import price_history

    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    p = price_history.path(discord_auctions.history_key(reg))
    p.parent.mkdir(parents=True)
    p.write_bytes(b"this is not a database at all, not even close" * 40)
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == [prices.NO_HISTORY_YET]
    assert prices.chart(reg, PLAGUEBLOOM, "7", now=NOW)["points"] == []


def test_chart_points(reg, clock):
    from oibot_gm.registry import RegistryError

    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    keep(reg, NOW - 9 * DAY, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    keep(reg, NOW - 4 * DAY, {})  # delisted: a gap
    keep(reg, NOW - 2 * DAY, {PLAGUEBLOOM: Price(low=1240, qty=2)})
    c = prices.chart(reg, PLAGUEBLOOM, "7", now=NOW)
    assert c["span"] == "7" and [p["low"] for p in c["points"]][:2] == [1500, None] and c["points"][1]["words"] == "none listed"
    assert all(" AM" in p["when"] or " PM" in p["when"] for p in c["points"])  # the Clock rule
    assert c["points"][0]["t"] == int((NOW - 7 * DAY).timestamp()), "the price carried to the span's start"
    assert c["points"][0]["when"] == "Fri 6 Nov 12:00 PM" and c["points"][0]["words"] == "15s"
    assert (c["low"], c["high"], c["low_words"], c["high_words"]) == (1240, 1500, "12s 40c", "15s")
    assert (c["since"], c["until"]) == (int((NOW - 7 * DAY).timestamp()), int(NOW.timestamp())), "the x axis: the span's start to the server's now"
    clock.t = NOW + timedelta(minutes=5)
    assert prices.chart(reg, PLAGUEBLOOM, "7")["until"] == int(clock.t.timestamp()), "now = the bot's clock"
    assert prices.chart(reg, PLAGUEBLOOM, "365", now=NOW)["points"][0]["low"] == 1500, "the year starts at the first kept"
    with pytest.raises(RegistryError, match="That isn't one of the spans."):
        prices.chart(reg, PLAGUEBLOOM, "14", now=NOW)


def test_a_folded_day_reads_as_the_day_alone(reg, clock):
    from oibot_gm import price_history

    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    old = datetime(2026, 7, 1, 18, tzinfo=timezone.utc)
    keep(reg, old, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    keep(reg, old + timedelta(hours=1), {PLAGUEBLOOM: Price(low=1700, qty=4)})
    keep(reg, old + timedelta(hours=2), {PLAGUEBLOOM: Price(low=1600, qty=4)})
    keep(reg, NOW - DAY, {PLAGUEBLOOM: Price(low=1400, qty=4)})
    price_history.fold(NOW)
    pts = prices.chart(reg, PLAGUEBLOOM, "365", now=NOW)["points"]
    assert pts[0]["when"] == "Wed 1 Jul" and (pts[0]["low"], pts[0]["high"]) == (1500, 1700)
    assert pts[-1]["when"].endswith((" AM", " PM"))


def test_a_version_without_auction_houses_has_no_watch_or_history(reg, clock, member, monkeypatch):
    from oibot_gm.registry import RegistryError

    monkeypatch.setattr(reg, "profile", TBC)
    monkeypatch.setattr(reg, "base_profile", TBC)
    with pytest.raises(RegistryError, match=auctions.NO_AUCTIONS_WORDS):
        prices.watch(reg, UID, next(iter(TBC.items)))
    assert prices.history_sentences(reg, PLAGUEBLOOM, now=NOW) == []
    assert prices.chart(reg, PLAGUEBLOOM, "7", now=NOW)["points"] == []
    assert prices.watch_list(reg, UID) == []
