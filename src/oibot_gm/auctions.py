"""THE boundary to Blizzard's auction houses (auction prices, spec §4.1/§4.2/§8): the one place the bot reads which houses
a ruleset has and what one house lists. It rides the armory's request path (armory.Armory.get: the shared token and
limiter, retries, the token never in an error) and the armory's failures (ArmoryDown / ArmoryAccess / ArmoryShape /
ArmoryMissing). The API is undocumented: every answer is validated here. Every call BLOCKS: callers use
`asyncio.to_thread`.

The version's facts are data (profiles/<version>/integrations.yaml `auctions`): the dynamic namespace and which house name
is each faction's. A version without the block has no prices.

A price is the lowest buyout per unit (D2), in copper: a listing's buyout ÷ quantity rounded up, a stackable's unit price.
A row with no buyout, a buyout ≤ 0, a quantity ≤ 0 or no item id is not a price and is skipped (C2). Random-suffix
variants are keyed by the plain item id."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oibot_gm import armory
from oibot_gm.armory import _int, _need, _str

NO_AUCTIONS_WORDS = "This game version has no auction houses."


@dataclass(frozen=True)
class House:
    id: int
    name: str


@dataclass(frozen=True)
class Price:
    low: int  # the lowest unit price listed, copper
    qty: int  # how many are listed in all (rows with a usable price only)


def _count(v: Any) -> int | None:
    """A positive whole number from the feed, else None (missing, zero, negative, a bool, not a number)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _rows(data: dict, path: str) -> list[dict]:
    """The `auctions` rows of an answer: none at all = an empty house; anything but a list of objects is a surprise."""
    rows = data.get("auctions")
    if rows is None:
        return []
    _need(isinstance(rows, list), path, "auctions")
    for r in rows:
        _need(isinstance(r, dict), path, "auction")
    return rows


def _item_id(row: dict) -> int | None:
    item = row.get("item")
    return _count(item.get("id")) if isinstance(item, dict) else None


def _add(table: dict[int, Price], item: int, unit: int, qty: int) -> None:
    old = table.get(item)
    table[item] = Price(low=unit, qty=qty) if old is None else Price(low=min(old.low, unit), qty=old.qty + qty)


class Client:
    """One region's auction houses in `dynamic_ns`, read through `arm` (an armory.Armory: its fetch, token and limiter)."""

    def __init__(self, arm: armory.Armory, dynamic_ns: str):
        self.arm, self.dynamic_ns = arm, dynamic_ns

    def houses(self, ruleset_id: int) -> list[House]:
        """Every house of the ruleset (every faction's and the neutral ones), in the armory's order."""
        path = f"/data/wow/ruleset/{int(ruleset_id)}/auctions/index"
        d = self.arm.get(path, self.dynamic_ns).data
        return [House(id=_int(h, "id", path), name=_str(h, "name", path)) for h in _rows(d, path)]

    def table(self, ruleset_id: int, house_id: int, since: str | None = None) -> tuple[dict[int, Price] | None, str | None]:
        """One house's prices → ({item id: Price}, the listings' Last-Modified). `since` goes on the listings request only;
        a 304 there → (None, the 304's Last-Modified, else `since`) and the stackables are not read. An empty house = {}."""
        path = f"/data/wow/ruleset/{int(ruleset_id)}/auctions/{int(house_id)}"
        got = self.arm.get(path, self.dynamic_ns, since)
        if got.unchanged:
            return None, got.modified
        table: dict[int, Price] = {}
        for r in _rows(got.data, path):
            item, buyout, qty = _item_id(r), _count(r.get("buyout")), _count(r.get("quantity"))
            if item and buyout and qty:
                _add(table, item, -(-buyout // qty), qty)
        stacks = f"{path}/commodities"
        for r in _rows(self.arm.get(stacks, self.dynamic_ns).data, stacks):
            item, unit, qty = _item_id(r), _count(r.get("unit_price")), _count(r.get("quantity"))
            if item and unit and qty:
                _add(table, item, unit, qty)
        return table, got.modified

    def item_name(self, item_id: int) -> str | None:
        """An item's name from the static namespace (the naming step behind the price search); None when Blizzard has no
        item of that id (a 404)."""
        path = f"/data/wow/item/{int(item_id)}"
        try:
            d = self.arm.get(path, self.arm.static_ns).data
        except armory.ArmoryMissing:
            return None
        return _str(d, "name", path)


# ------------------------------------------------------------------ per guild


def available(reg) -> bool:
    """The guild's game version has auction houses (integrations.yaml `auctions`) and an armory the bot can sign in to."""
    return bool(reg.profile.auctions) and armory.available(reg)


def client_for(reg) -> Client:
    """The guild's auction client: the process's Armory for its version and region, in the version's dynamic namespace."""
    a = reg.profile.auctions
    if not a:
        raise armory.ArmoryError(NO_AUCTIONS_WORDS, f"profile {reg.profile.name} has no auctions")
    arm = armory.client_for(reg)
    return Client(arm, str(a["dynamic_namespace"]).format(region=arm.region))


def faction_houses(reg, houses: list[House], faction: str) -> list[House]:
    """The houses named for `faction` ("Alliance" | "Horde", the armory's word) in the profile; never the neutral ones."""
    name = ((reg.profile.auctions or {}).get("factions") or {}).get(faction)
    return [h for h in houses if name and h.name == name]


def money(copper: int) -> str:
    """Copper in gold, silver and copper words: 489998 → '48g 99s 98c'; zero parts dropped; 0 → '0c'."""
    copper = max(0, int(copper))
    g, rest = divmod(copper, 10000)
    s, c = divmod(rest, 100)
    return " ".join(f"{n}{u}" for n, u in ((g, "g"), (s, "s"), (c, "c")) if n) or "0c"
