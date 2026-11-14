"""Auction prices on the site and MCP (auction prices, spec §4.1, §4.4–§4.6): /api/meta's `prices` and `item_url`, the
cost to make on the Professions page's recipe rows, the price search for every member, and the owner's auction house
(officers read; the owner looks at the choices, previews, picks and clears; a stale rev 409; one audit line per write).
Thin routes over prices / discord_auctions / Registry. No network: a fake armory and a fake auction client stand in for
Blizzard; names are invented or the game's own."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from conftest import OWNER
from test_people_web import OFFICER, TOKEN, as_user, member, w  # noqa: F401 — `w` is the fixture
from test_prices import HORDE_AH, facts, priced, recipe

from oibot_gm import armory, discord_auctions, prices
from oibot_gm import consequences as cq
from oibot_gm import mcp_server as ms
from oibot_gm.auctions import House, Price
from oibot_gm.profiles import GameProfile
from oibot_gm.web.api_prices import cost_json

ROOT = Path(__file__).resolve().parents[1]
MCP = {"Authorization": f"Bearer {TOKEN}", "X-Requested-With": "oibot"}
BAR, DREAMFOIL, RUNECLOTH = 12360, 13463, 14047
BOLT, MOONCLOTH, RUNE_THREAD = 14048, 14342, 14341  # three of Mooncloth Bag's reagents


def now() -> datetime:
    return datetime.now(timezone.utc)


class FakeArmory:
    def __init__(self, ruleset=2):
        self.ruleset = ruleset

    def guild(self, slug):
        return dataclasses.replace(facts(), ruleset_id=self.ruleset)


class FakeAuctions:
    """Two Horde houses (one empty), the Alliance's and the neutral one; every table read is recorded."""

    HOUSES = [House(id=2, name="Alliance Auction House"), HORDE_AH, House(id=7, name="Blackwater Auction House"), House(id=18, name="Horde Auction House")]

    def __init__(self):
        self.reads: list[tuple[int, int]] = []

    def houses(self, ruleset_id):
        return list(self.HOUSES)

    def table(self, ruleset_id, house_id, since=None):
        self.reads.append((ruleset_id, house_id))
        if house_id == 6:
            return {BAR: Price(low=489998, qty=30), DREAMFOIL: Price(low=4099, qty=200)}, format_datetime(now(), usegmt=True)  # a feed read just now (the routes run on the real clock)
        return {}, None


@pytest.fixture
def market_on(monkeypatch, tmp_path):
    monkeypatch.setattr(discord_auctions, "OUT", tmp_path / "auctions-out")
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    fa = FakeAuctions()
    monkeypatch.setattr(armory, "client_for", lambda reg: FakeArmory())
    monkeypatch.setattr(discord_auctions, "_client", lambda reg: fa)
    discord_auctions.reset_state()
    prices.reset_names()
    yield fa
    discord_auctions.reset_state()
    prices.reset_names()


@pytest.fixture
def tbc(w, monkeypatch, market_on):
    p = GameProfile.load(ROOT / "profiles" / "tbc")
    monkeypatch.setattr(w.reg, "profile", p)
    monkeypatch.setattr(w.reg, "base_profile", p)
    return w


def crafter(w, n=0, recipe_name="Mooncloth Bag"):
    m = member(w, n)
    w.reg.save_character_table(m.discord_id, [{"label": m.main.label, "professions": [{"id": "tailoring", "skill": 300, "recipes": [{"name": recipe_name}]}]}], "t", officer=True)
    return w.reg.members[m.discord_id]


def test_meta_carries_prices_and_item_url(w, market_on):
    m = member(w)
    d = w.c.get("/api/meta", headers=as_user(w, OWNER)).json()
    assert d["prices"] == {"on": False, "state": "off", "words": prices.OWNER_LINK_WORDS, "market": True}, "no guild linked: link it first"
    assert d["item_url"] == "https://www.wowhead.com/forever/item={id}", "Forever's items link to Wowhead's Forever section (C12)"
    w.reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    assert w.c.get("/api/meta", headers=as_user(w, OWNER)).json()["prices"]["words"] == prices.OWNER_OFF_WORDS
    assert w.c.get("/api/meta", headers=as_user(w, m.discord_id)).json()["prices"]["words"] == "", "a member reads nothing while prices are off"
    priced(w.reg, {BAR: Price(low=489998, qty=30)}, at=now())
    p = w.c.get("/api/meta", headers=as_user(w, m.discord_id)).json()["prices"]
    assert p["on"] is True and p["state"] == "fresh" and p["words"].startswith("Prices as of ")


def test_recipe_rows_carry_cost_when_prices_are_on_and_null_when_off(w, market_on):
    m = crafter(w)
    h = as_user(w, m.discord_id)
    [row] = w.c.get("/api/professions/find?q=mooncloth%20bag", headers=h).json()["recipes"]
    assert row["cost"] is None, "no prices: no cost"
    assert w.c.get("/api/professions", headers=h).json()["recipes"][0]["cost"] is None
    priced(w.reg, {BOLT: Price(low=5000, qty=40), MOONCLOTH: Price(low=12000, qty=3), 14155: Price(low=900000, qty=2)}, at=now())
    [row] = w.c.get("/api/professions/find?q=mooncloth%20bag", headers=h).json()["recipes"]
    c = row["cost"]
    bolt = next(x for x in c["lines"] if x["id"] == BOLT)
    assert bolt == {"id": BOLT, "name": "Bolt of Runecloth", "quantity": 4, "unit_words": "50s", "total_words": "2g"}
    thread = next(x for x in c["lines"] if x["id"] == RUNE_THREAD)
    assert thread["unit_words"] == prices.NONE_LISTED and thread["total_words"] is None
    assert c["sentence"] == "Cost to make: at least 3g 20s — 4 reagents have none listed."
    assert c["made_words"] == "Makes 1 · lowest price 90g each, 2 listed"
    assert w.c.get("/api/professions", headers=h).json()["recipes"][0]["cost"] == c, "the catalog's rows carry the same cost"


def test_a_recipe_with_no_reagents_has_no_cost_never_a_zero(w, market_on):
    priced(w.reg, {BAR: Price(low=489998, qty=30)}, at=now())
    assert cost_json(w.reg, recipe(makes={"id": BAR, "name": "Arcanite Bar", "quantity": 1})) is None
    assert cost_json(w.reg, None) is None
    got = cost_json(w.reg, recipe(reagents=[{"id": BAR, "name": "Arcanite Bar", "quantity": 2}]))
    assert got["sentence"] == "Cost to make: 97g 99s 96c." and got["made_words"] == ""


def test_find_returns_rows_total_and_words(w, market_on):
    m = member(w)
    h = as_user(w, m.discord_id)
    d = w.c.get("/api/prices/find?q=arcanite%20bar", headers=h).json()
    assert d["rows"] == [] and d["total"] == 0 and d["words"] == "", "prices off: nothing, and nothing to read for a member"
    priced(w.reg, {BAR: Price(low=489998, qty=30)}, at=now())
    d = w.c.get("/api/prices/find?q=arcanite%20bar", headers=h).json()
    assert d["rows"][0] == {"id": BAR, "name": "Arcanite Bar", "low_words": "48g 99s 98c", "qty": 30,
                            "words": "Arcanite Bar: lowest price 48g 99s 98c, 30 listed."}
    assert d["total"] >= 1 and d["words"].startswith("Prices as of ")
    assert d["message"].splitlines()[:2] == [d["words"], "Arcanite Bar: lowest price 48g 99s 98c, 30 listed."]
    d = w.c.get("/api/prices/find?q=dreamfoil", headers=h).json()
    assert d["rows"][0]["low_words"] == prices.NONE_LISTED and d["rows"][0]["qty"] == 0
    d = w.c.get("/api/prices/find?q=a", headers=h).json()
    assert len(d["rows"]) == 25 and d["total"] > 25 and d["message"].endswith(f"And {d['total'] - 25} more items match.")
    assert d["more"] == f"And {d['total'] - 25} more items match.", "the page prints the server's last sentence alone"
    d = w.c.get("/api/prices/find?q=zzqx", headers=h).json()
    assert d["rows"] == [] and d["message"].endswith(prices.NO_MATCH) and d["more"] == ""


def test_market_routes_owner_only_preview_set_clear_and_409(w, market_on):
    reg = w.reg
    m = member(w)
    assert w.c.get("/api/admin/market", headers=as_user(w, m.discord_id)).status_code == 403
    off = as_user(w, OFFICER)
    d = w.c.get("/api/admin/market", headers=off).json()
    assert d["selected"] is None and "link" in d["words"] and d["clear_sentences"] == [] and d["rev"] == reg.config_rev("settings")
    assert w.c.post("/api/admin/market", json={"choices": True}, headers=off).status_code == 403
    h = as_user(w, OWNER)
    r = w.c.post("/api/admin/market", json={"choices": True}, headers=h)
    assert r.status_code == 400 and "Link the server" in r.json()["error"], "no linked guild: one sentence"
    reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    d = w.c.post("/api/admin/market", json={"choices": True}, headers=h).json()
    assert d["ruleset_id"] == 2 and [c["house_id"] for c in d["choices"]] == [6], "the Horde houses with listings only"
    assert d["words"] == ""
    [c] = d["choices"]
    assert c["name"] == "Horde Auction House" and c["listings"] == 230 and c["listings_words"] == "230 listed in all"
    assert c["samples"] == [{"id": BAR, "name": "Arcanite Bar", "low_words": "48g 99s 98c"}, {"id": DREAMFOIL, "name": "Dreamfoil", "low_words": "40s 99c"},
                            {"id": RUNECLOTH, "name": "Runecloth", "low_words": prices.NONE_LISTED}]
    rev = reg.config_rev("settings")
    p = w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "preview": True}, headers=h).json()
    assert p["sentences"] == cq.market_consequence(reg, HORDE_AH) and p["rev"] == rev and reg.config.market is None
    for bad in (2, 7, 99):  # another faction's, the neutral one, one the index doesn't list
        r = w.c.post("/api/admin/market", json={"house_id": bad, "ruleset_id": 2, "rev": rev}, headers=h)
        assert r.status_code == 400 and r.json()["error"] == "That isn't one of the guild's auction houses. Look at the choices again.", bad
    r = w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 3, "rev": rev}, headers=h)
    assert r.status_code == 409 and reg.config.market is None, "a ruleset that isn't the guild's now"
    assert w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "rev": "stale"}, headers=h).status_code == 409
    assert w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "rev": rev}, headers=as_user(w, OFFICER)).status_code == 403
    assert reg.config.market is None
    h = as_user(w, OWNER)  # the session cookie is the client's one: back to the owner
    r = w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "rev": rev}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (reg.config.market.house_id, reg.config.market.ruleset_id) == (6, 2)
    assert out["selected"]["name"] == "Horde Auction House" and out["selected"]["house_id"] == 6 and out["clear_sentences"] == cq.market_clear_consequence(reg)
    assert sum("Horde Auction House" in t and "[web]" in t for *_x, t in w.bot.ops.lines) == 1
    assert discord_auctions.state(reg) == "fresh", "the picker's read became the guild's first table"
    again = w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "rev": out["rev"]}, headers=h)
    assert again.status_code == 200 and again.json()["message"] == "Prices already come from the Horde Auction House."
    assert again.json()["rev"] == out["rev"], "the house already picked: nothing written"
    assert sum("Horde Auction House" in t and "[web]" in t for *_x, t in w.bot.ops.lines) == 1, "and no audit line"
    s = w.c.post("/api/admin/market", json={"clear": True, "preview": True}, headers=h).json()
    assert s["sentences"] == cq.market_clear_consequence(reg) and reg.config.market is not None
    assert w.c.post("/api/admin/market", json={"clear": True, "rev": "stale"}, headers=h).status_code == 409
    r = w.c.post("/api/admin/market", json={"clear": True, "rev": out["rev"]}, headers=h)
    assert r.status_code == 200 and r.json()["selected"] is None and reg.config.market is None
    assert sum("[web]" in t and "auction house" in t for *_x, t in w.bot.ops.lines) == 2
    assert w.c.post("/api/admin/market", json={"clear": True, "preview": True}, headers=h).status_code == 400


def test_a_write_from_mcp_needs_the_owner_as_actor(w, market_on, monkeypatch):
    w.reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    rev = w.reg.config_rev("settings")
    p = w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "preview": True}, headers=MCP)
    assert p.status_code == 200, "a preview needs no actor"
    r = w.c.post("/api/admin/market", json={"house_id": 6, "ruleset_id": 2, "rev": rev}, headers=MCP)
    assert r.status_code == 403 and "owner" in r.json()["error"] and w.reg.config.market is None


def test_everything_404s_or_is_null_on_tbc(tbc):
    w = tbc
    h = as_user(w, OWNER)
    assert w.c.get("/api/admin/market", headers=h).status_code == 404
    assert w.c.post("/api/admin/market", json={"choices": True}, headers=h).status_code == 404
    assert w.c.get("/api/prices/find?q=arcanite", headers=h).status_code == 404
    d = w.c.get("/api/meta", headers=h).json()
    assert d["prices"] == {"on": False, "state": "off", "words": "", "market": False}, "no auction houses: no word about a picker"
    assert d["item_url"] == "https://www.wowhead.com/tbc/item={id}", "links ride wowhead_item_url alone"
    assert all(r["cost"] is None for r in w.c.get("/api/professions", headers=h).json()["recipes"])


def test_no_keys_means_no_word_about_a_picker(w, market_on, monkeypatch):
    """F-1: a host without the Blizzard keys has no auction house card, so the owner is not sent to one."""
    w.reg.set_armory_guild(facts(), "Owner", by_id=OWNER)
    monkeypatch.setattr(armory, "available", lambda reg: False)
    d = w.c.get("/api/meta", headers=as_user(w, OWNER)).json()
    assert d["prices"] == {"on": False, "state": "off", "words": "", "market": False}


def test_the_prices_topic_is_published_on_a_table_update(w, market_on):
    """discord_auctions.PRICE_LISTENERS → publish("prices", guild): one listener per process, the newest app's."""
    listeners = [f for f in discord_auctions.PRICE_LISTENERS if f.__name__ == "prices_updated"]
    assert len(listeners) == 1
    scheduled = []
    w.bot.loop = type("Loop", (), {"call_soon_threadsafe": lambda self, cb: scheduled.append(cb)})()
    listeners[0](w.reg.key)
    assert scheduled, "a table update wakes the guild's open pages"


def test_mcp_price_check_and_market_read_the_site_routes(w, market_on):
    def handler(request: httpx.Request) -> httpx.Response:
        r = w.c.request(request.method, request.url.path, params=dict(request.url.params), headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "application/json")})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        assert ms.price_check("arcanite bar") == prices.OWNER_LINK_WORDS, "prices off, no guild linked: the owner's token reads how to turn them on"
        assert "link" in ms.market()["words"] and ms.market()["selected"] is None
        priced(w.reg, {BAR: Price(low=489998, qty=30)}, at=now())
        out = ms.price_check("arcanite bar")
        assert out.startswith("Prices as of ") and "Arcanite Bar: lowest price 48g 99s 98c, 30 listed." in out
        assert ms.market()["selected"]["name"] == "Horde Auction House"
    finally:
        ms.use(None)
    tools = {t.name for t in asyncio.run(ms.mcp.list_tools())}
    assert {"price_check", "market"} <= tools and not {"set_market", "market_set"} & tools


def test_wowhead_links_forever_items():
    """C12: Forever's template is set, so the Discord loot item card links its item too; tbc unchanged; an item known only
    by name has no link."""
    from types import SimpleNamespace

    from oibot_gm.discord_bot import wowhead

    forever = GameProfile.load(ROOT / "profiles" / "forever")
    tbc_p = GameProfile.load(ROOT / "profiles" / "tbc")
    assert wowhead(SimpleNamespace(id=13452), forever) == "https://www.wowhead.com/forever/item=13452"
    assert forever.item_url == "https://www.wowhead.com/forever/item={id}"
    assert wowhead(SimpleNamespace(id=28830), tbc_p) == "https://www.wowhead.com/tbc/item=28830"
    assert wowhead(SimpleNamespace(id=0), forever) is None and wowhead(None, forever) is None
