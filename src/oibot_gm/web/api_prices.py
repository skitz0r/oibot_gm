"""Auction prices on the site (auction prices, spec §4.1, §4.4, §4.5): thin callers of prices / discord_auctions /
Registry — no rule lives here.

- `GET /api/prices/find?q=` (every member): the price search (prices.search) → {rows [{id, name, low_words, qty, words}],
  total, words (how recent the prices are, prices.status_sentence for this reader), message (the answer in lines:
  prices.answer_lines — MCP's price_check reads it)}. 404 on a version with no auction houses.
- `GET /api/admin/market` (officers read) + `POST /api/admin/market` (owner): the guild's auction house.
  `{choices: true}` → the houses the owner may pick (discord_auctions.house_choices: a Blizzard read in a thread;
  `words` = why the list is empty, else "");
  `{house_id, ruleset_id, preview: true}` → the confirm sentences; `{house_id, ruleset_id, rev}` → picked (the ids are
  re-checked against the armory: discord_auctions.pick_house), then the first read of its prices in the background;
  `{clear: true, preview?, rev?}` → prices off. A stale rev 409; one audit line per write; from MCP a write needs the
  owner as actor (D8). 404 where auctions.available is false.
- `cost_json` = a recipe's cost to make for the Professions page's rows (api_professions.entry_json): null without prices
  to show and for a recipe with no reagents (never a zero cost).
- discord_auctions.PRICE_LISTENERS → publish("prices", guild): an open Professions page reloads after an hourly check.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auctions, discord_auctions, prices
from .. import ops as ops_mod
from ..registry import ConfigChanged, RegistryError

log = logging.getLogger(__name__)

FIND_LIMIT = 25  # items the price search returns; `total` counts every match
MARKET_ACTOR = ("Picking the guild's auction house is the owner's decision: from MCP it needs OIBOT_MCP_ACTOR set to the "
                "owner's Discord id.")


def _low(low: int | None) -> str:
    return auctions.money(low) if low is not None else prices.NONE_LISTED


def cost_json(reg, recipe) -> dict | None:
    """A recipe's cost to make (prices.recipe_cost) for a page: {lines [{id, name, quantity, unit_words, total_words (None
    when none listed)}], sentence, made_words}; None for no recipe, no reagents or no prices to show."""
    if recipe is None or not recipe.reagents:
        return None
    cost = prices.recipe_cost(reg, recipe)
    if cost is None:
        return None
    return {"lines": [{"id": c.id, "name": c.name, "quantity": c.quantity, "unit_words": _low(c.unit),
                       "total_words": auctions.money(c.total) if c.total is not None else None} for c in cost.lines],
            "sentence": prices.cost_sentence(cost), "made_words": prices.made_words(cost)}


def prices_meta(reg, owner: bool) -> dict:
    """/api/meta's `prices`: on (a house is picked on a version with auction houses), state, words for this reader, and
    `market` = the owner's picker is available here (the Config page shows the auction house card)."""
    state = discord_auctions.state(reg)
    return {"on": state != "off", "state": state, "words": prices.status_sentence(reg, owner), "market": auctions.available(reg)}


def install_prices_api(app: FastAPI, bot, *, who, body, need_owner, publish=None) -> None:
    def prices_updated(guild_key: str) -> None:
        if publish is not None:
            publish("prices", guild_key)  # a new price table: an open Professions page reloads

    hooks = discord_auctions.PRICE_LISTENERS
    hooks[:] = [f for f in hooks if getattr(f, "__name__", "") != prices_updated.__name__] + [prices_updated]  # one per process, the newest app wins

    @app.get("/api/prices/find")
    async def prices_find(request: Request, q: str = ""):
        """Every member: the items whose name holds every typed word, listed ones first; at most FIND_LIMIT rows."""
        v = await who(request)
        reg = v.reg
        if not reg.profile.auctions:
            raise HTTPException(404, auctions.NO_AUCTIONS_WORDS)
        shown, total = prices.search(reg, q, FIND_LIMIT)
        owner = bool(v.owner)
        return {"q": q, "rows": [{"id": p.id, "name": p.name, "low_words": _low(p.low), "qty": p.qty, "words": prices.price_words(p)} for p in shown],
                "total": total, "words": prices.status_sentence(reg, owner),
                "message": "\n".join(prices.answer_lines(reg, shown, total, owner)) if q.strip() else ""}

    # ---- the guild's auction house: officers read, the owner picks / clears
    def market_state(v) -> dict:
        from ..consequences import market_clear_consequence

        reg = v.reg
        m = reg.config.market
        selected = None if m is None else {"house_id": m.house_id, "ruleset_id": m.ruleset_id, "name": m.name, "faction": m.faction,
                                           "picked_by": m.picked_by, "picked_label": reg.local12(m.picked_at, "%a %d %b %Y")}
        return {"selected": selected, "words": reg.market_words(), "clear_sentences": market_clear_consequence(reg) if m is not None else [],
                "prices": prices.status_sentence(reg, bool(v.owner)), "rev": reg.config_rev("settings"), "owner": bool(v.owner)}

    def market_or_404(reg) -> None:
        if not auctions.available(reg):
            raise HTTPException(404, auctions.NO_AUCTIONS_WORDS)

    def choice_json(reg, c: discord_auctions.Choice) -> dict:
        named = prices.names(reg)
        return {"house_id": c.house.id, "name": c.house.name, "listings": c.listings,
                "listings_words": "1 item listed" if c.listings == 1 else f"{c.listings} items listed",
                "samples": [{"id": i, "name": named.get(i) or f"Item {i}", "low_words": _low(low)} for i, low in c.samples]}

    @app.get("/api/admin/market")
    async def market_get(request: Request):
        """Officers read, the owner edits: the auction house the guild's prices come from (or what is missing)."""
        v = await who(request)
        if not (v.officer or v.owner):
            raise HTTPException(403, "Officers only.")
        market_or_404(v.reg)
        return market_state(v)

    @app.post("/api/admin/market")
    async def market_set(request: Request):
        """Owner: `{choices: true}` → the houses to pick from; `{house_id, ruleset_id, preview: true}` → the sentences;
        `{house_id, ruleset_id, rev}` → picked; `{clear: true, preview?, rev?}` → prices off. One audit line per write."""
        from ..armory import ArmoryError
        from ..consequences import market_clear_consequence, market_consequence
        from .api_armory import armory_error_response

        v, d = await body(request)
        need_owner(v)  # the owner (or a host superuser): owner rights come from the id, not the officer roles
        reg = v.reg
        market_or_404(reg)
        preview = bool(d.get("preview"))
        writes = not preview and not d.get("choices")
        if writes and v.via == "mcp" and not getattr(v, "actor", None):
            raise HTTPException(403, MARKET_ACTOR)
        rev = d.get("rev") or None
        if d.get("choices"):
            try:
                got = await discord_auctions.house_choices(bot, reg)
            except ArmoryError as e:
                return armory_error_response(e)  # down / keys refused → 503, as the guild link answers
            except RegistryError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            ruleset = got[0].ruleset_id if got else None
            return {"choices": [choice_json(reg, c) for c in got], "ruleset_id": ruleset, "words": "" if got else discord_auctions.NO_CHOICES}
        if d.get("clear"):
            if preview:
                if reg.config.market is None:
                    return JSONResponse({"error": "No auction house is picked for this guild."}, status_code=400)
                return {"preview": True, "sentences": market_clear_consequence(reg), "rev": reg.config_rev("settings")}
            old = reg.config.market
            try:
                await asyncio.to_thread(lambda: reg.clear_market(v.name, by_id=v.uid, rev=rev))
            except ConfigChanged as e:
                return JSONResponse({"error": str(e)}, status_code=409)
            except RegistryError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            await ops_mod.audited(bot.ops, reg, v.name, f"turned prices off (the auction house was the {old.name})", via=v.via, by_id=v.uid)
            return {"message": "Prices are off.", **market_state(v)}
        try:
            house_id, ruleset_id = int(d.get("house_id")), int(d.get("ruleset_id"))
        except (TypeError, ValueError):
            return JSONResponse({"error": discord_auctions.NOT_A_CHOICE}, status_code=400)
        try:
            house = await discord_auctions.pick_house(bot, reg, house_id, ruleset_id)
        except ArmoryError as e:
            return armory_error_response(e)
        except discord_auctions.HousesChanged as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if preview:
            return {"preview": True, "sentences": market_consequence(reg, house), "rev": reg.config_rev("settings")}
        try:
            said = await asyncio.to_thread(lambda: reg.set_market(house, ruleset_id, v.name, by_id=v.uid, rev=rev))
        except ConfigChanged as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except RegistryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        await ops_mod.audited(bot.ops, reg, v.name, f"picked the {house.name} as the guild's auction house", via=v.via, by_id=v.uid)
        await refresh_soon(reg)
        return {"message": said[:1].upper() + said[1:] + ".", **market_state(v)}

    running: set[asyncio.Task] = set()

    async def refresh_soon(reg) -> None:
        """The new house's first read, off the request: the picker's own read is usually taken (no request)."""
        async def go():
            try:
                await discord_auctions.refresh(bot, reg)
            except Exception:  # noqa: BLE001 — the hourly check tries again
                log.exception("first price read for %s failed", reg.key)

        t = asyncio.get_running_loop().create_task(go())
        running.add(t)
        t.add_done_callback(running.discard)
        await asyncio.sleep(0)  # let it take a shared read now when there is one
