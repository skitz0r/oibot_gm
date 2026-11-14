"""The auction boundary (auction prices, spec §4.1/§4.2/§8): auctions.Client over hand-written responses in the probed
shapes (tests/fixtures/auctions; no seller, no person), never the network."""
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from oibot_gm import auctions, battlenet
from oibot_gm.armory import Armory, ArmoryShape, GuildFacts, Reply
from oibot_gm.auctions import Client, House, Price, money
from oibot_gm.profiles import GameProfile

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "auctions"
FOREVER = GameProfile.load(ROOT / "profiles" / "forever")
TBC = GameProfile.load(ROOT / "profiles" / "tbc")
STAMP = "Fri, 13 Nov 2026 18:00:04 GMT"
NS = "dynamic-forever-us"
IDX = "/data/wow/ruleset/1/auctions/index"
HOUSE = "/data/wow/ruleset/1/auctions/2"
STACKS = f"{HOUSE}/commodities"


class Fake:
    """The injected transport: path → (status, body, headers); every call kept with its query and headers."""

    def __init__(self, routes: dict[str, tuple]):
        self.routes, self.calls = routes, []

    def __call__(self, url: str, headers: dict[str, str]) -> Reply:
        u = urllib.parse.urlsplit(url)
        self.calls.append((u.path, dict(urllib.parse.parse_qsl(u.query)), dict(headers)))
        route = self.routes.get(u.path)
        if route is None:
            raise AssertionError(f"not recorded: {u.path}")
        status, body, hdrs = (*route, {})[:3]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return Reply(status, {k.lower(): v for k, v in hdrs.items()}, body or b"")


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def client(routes: dict) -> tuple[Client, Fake]:
    fake = Fake(routes)
    arm = Armory("us", "profile-forever-us", "static-forever-us", fetch=fake, limiter=battlenet.Limiter(1000))
    return Client(arm, NS), fake


def test_houses_lists_the_ruleset_and_faction_houses_filter_by_name():
    c, fake = client({IDX: (200, fixture("index"))})
    houses = c.houses(1)
    assert len(houses) == 6 and all(isinstance(h, House) for h in houses)
    assert houses[0] == House(id=2, name="Alliance Auction House")
    assert fake.calls[0][1] == {"namespace": NS, "locale": "en_US"}
    reg = SimpleNamespace(profile=FOREVER)
    assert auctions.faction_houses(reg, houses, "Alliance") == [House(2, "Alliance Auction House"), House(12, "Alliance Auction House")]
    assert [h.id for h in auctions.faction_houses(reg, houses, "Horde")] == [6, 13]
    assert auctions.faction_houses(reg, houses, "Neutral") == []  # never the Blackwater houses


def test_table_merges_listings_and_stackables():
    c, fake = client({HOUSE: (200, fixture("house"), {"Last-Modified": STAMP}), STACKS: (200, fixture("commodities"))})
    table, modified = c.table(1, 2)
    assert modified == STAMP
    assert table[14047] == Price(low=4099, qty=1)
    assert table[2447] == Price(low=334, qty=3)  # 1000 for 3: rounded up
    assert table[3575] == Price(low=650, qty=30)  # stackables: unit price, quantities summed
    assert table[15211] == Price(low=25000, qty=1)  # a random-suffix item keyed by its plain id
    assert [p for p, _, _ in fake.calls] == [HOUSE, STACKS]
    assert all(q["namespace"] == NS for _, q, _ in fake.calls)


def test_an_item_in_both_feeds_takes_the_lowest_and_sums_the_quantity():
    c, _ = client({HOUSE: (200, fixture("house")), STACKS: (200, fixture("commodities"))})
    table, _ = c.table(1, 2)
    # listings: 520000 × 1, 979996 for 2 (489998 each); stackables: 489998 × 4
    assert table[12360] == Price(low=489998, qty=7)


def test_rows_without_a_usable_buyout_are_ignored():
    c, _ = client({HOUSE: (200, fixture("house")), STACKS: (200, fixture("commodities"))})
    table, _ = c.table(1, 2)
    assert 13463 not in table  # no buyout, buyout 0, quantity 0, no unit price: never a zero price
    assert set(table) == {14047, 2447, 12360, 15211, 3575}  # the rows without an item id are gone too
    assert all(p.low > 0 and p.qty > 0 for p in table.values())


def test_an_empty_house_is_an_empty_table_not_an_error():
    c, _ = client({HOUSE: (200, fixture("house_empty"), {"Last-Modified": STAMP}), STACKS: (200, fixture("house_empty"))})
    assert c.table(1, 2) == ({}, STAMP)


def test_a_304_on_the_listings_is_not_modified():
    c, fake = client({HOUSE: (304, b"", {"Last-Modified": STAMP})})
    assert c.table(1, 2, since=STAMP) == (None, STAMP)
    assert [p for p, _, _ in fake.calls] == [HOUSE]  # the stackables are not read
    assert fake.calls[0][2]["If-Modified-Since"] == STAMP


def test_since_goes_on_the_listings_only():
    c, fake = client({HOUSE: (200, fixture("house"), {"Last-Modified": STAMP}), STACKS: (200, fixture("commodities"))})
    c.table(1, 2, since="Fri, 13 Nov 2026 17:00:00 GMT")
    assert "If-Modified-Since" in fake.calls[0][2] and "If-Modified-Since" not in fake.calls[1][2]


@pytest.mark.parametrize("routes", [
    {HOUSE: (200, {"auctions": {"id": 1}})},
    {HOUSE: (200, {"auctions": ["a row"]})},
    {HOUSE: (200, fixture("house")), STACKS: (200, {"auctions": 7})},
    {HOUSE: (200, fixture("house")), STACKS: (200, {"auctions": [None]})},
])
def test_an_odd_shape_raises_armory_shape(routes):
    c, _ = client(routes)
    with pytest.raises(ArmoryShape):
        c.table(1, 2)


@pytest.mark.parametrize("body", [{"auctions": "x"}, {"auctions": [3]}, {"auctions": [{"id": "2", "name": "A"}]}, {"auctions": [{"id": 2}]}])
def test_an_odd_house_list_raises_armory_shape(body):
    c, _ = client({IDX: (200, body)})
    with pytest.raises(ArmoryShape):
        c.houses(1)


def test_money_words():
    assert money(489998) == "48g 99s 98c"
    assert money(4099) == "40s 99c"
    assert money(10000) == "1g"
    assert money(0) == "0c"
    assert money(7) == "7c"
    assert money(1000005) == "100g 5c"


def test_tbc_has_no_auctions(monkeypatch):
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "secret")
    assert TBC.auctions is None
    assert not auctions.available(SimpleNamespace(profile=TBC))
    assert auctions.available(SimpleNamespace(profile=FOREVER))
    monkeypatch.delenv("BLIZZARD_CLIENT_SECRET")
    assert not auctions.available(SimpleNamespace(profile=FOREVER))


def test_the_forever_block_and_client_for():
    a = FOREVER.auctions
    assert a["factions"] == {"Alliance": "Alliance Auction House", "Horde": "Horde Auction House"}
    named = {r["id"] for rec in FOREVER.recipes.values() for r in rec.reagents}
    assert len(a["reference_items"]) == 3 and set(a["reference_items"]) <= named
    c = auctions.client_for(SimpleNamespace(profile=FOREVER))
    assert c.dynamic_ns == NS and c.arm.region == "us"


def test_guild_facts_carry_the_ruleset_id():
    g = Fake({"/data/wow/guild/lantern-wardens": (200, json.loads((ROOT / "tests" / "fixtures" / "armory" / "profile" / "guild.json").read_text()))})
    arm = Armory("us", "profile-forever-us", "static-forever-us", fetch=g, rulesets=FOREVER.armory["rulesets"], limiter=battlenet.Limiter(1000))
    facts = arm.guild("lantern-wardens")
    assert isinstance(facts, GuildFacts) and facts.ruleset_id == 2 and facts.kind == "Normal"
    assert isinstance(facts.created, datetime) and facts.created.tzinfo == timezone.utc


def test_a_house_list_without_its_auctions_key_raises_armory_shape():
    """An empty HOUSE answers without `auctions` (an empty table); the house INDEX always lists its houses — a missing key
    there is a surprise, never "no houses"."""
    c, _ = client({IDX: (200, {"_links": {}})})
    with pytest.raises(ArmoryShape):
        c.houses(1)
