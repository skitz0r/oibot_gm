"""`/price` with Watch and the watch list (price watch spec §4.4, C7, C13, C15, C16, C18): the flow `price` driven end to
end through the wizard harness (every screen linted), and the command reached through the offline tree. No network: the
history file is a temporary one, Blizzard and the model are never reached. Names are invented or the game's own."""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone

import discord
import pytest
from conftest import OWNER
from test_prices import HORDE_AH, facts, priced
from test_prices_discord import market_on  # noqa: F401 — the fixture: a fake auction client, prices out/ in tmp
from test_prices_web import BAR
from test_professions_flows import _offline_tree
from wizard_harness import Harness

from oibot_gm import armory, discord_auctions, pending_views, price_history, prices
from oibot_gm import wizard_flows  # noqa: F401 — registers every flow
from oibot_gm.auctions import Price
from oibot_gm.registry import WATCH_FULL, WATCH_MAX, WATCH_REGISTER_FIRST
from oibot_gm.wizard import FLOWS

UID = 300_000_000_000_000_202
PLAGUEBLOOM = 13466
UNNAMED_ID = 900_001  # an id the bot could name once; its name is gone since
DAY = timedelta(days=1)


def run(coro):
    return asyncio.run(coro)


def now() -> datetime:
    return datetime.now(timezone.utc)


def register(reg, uid=UID, name="Brindlewick"):
    m = reg.member(uid, name, create=True)
    reg.save(m, f"{name} registered")
    return m


def harness(reg, uid=UID, name="Brindlewick"):
    return Harness(reg, user=types.SimpleNamespace(id=uid, display_name=name, mention=f"@{name}"))


def keep(reg, at, table):
    assert price_history.record(discord_auctions.history_key(reg), at, table) >= 0


def lines(h) -> list[str]:
    return h.text.splitlines()


def on(reg):
    """Prices show: the Horde house picked and read just now."""
    return priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4), BAR: Price(low=489998, qty=30)}, at=now())


# ---------------------------------------------------------------- /price with an item


def test_price_with_one_item_offers_watch_and_flips(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        today = prices.price_answer(reg, f"#{PLAGUEBLOOM}", False)
        assert lines(h) == today, "the answer reads exactly as /price did"
        assert h.buttons() == ["Watch"]
        await h.press("Watch")
        assert lines(h)[0] == "You're watching Plaguebloom." and lines(h)[1:] == today
        assert h.buttons() == ["Stop watching"] and reg.watching(UID) == [PLAGUEBLOOM]
        await h.press("Stop watching")
        assert lines(h)[0] == "You've stopped watching Plaguebloom." and h.buttons() == ["Watch"]
        assert reg.watching(UID) == []
    run(go())
    # the typed name that matches one item has the button too; already on the list → Stop watching
    reg.watch(UID, PLAGUEBLOOM)
    assert prices.search(reg, "plaguebloom", 25)[1] == 1
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg, item="plaguebloom"))
    assert h.buttons() == ["Stop watching"] and lines(h)[1] == "Plaguebloom: lowest price 15s, 4 listed."


def test_typed_text_matching_several_has_no_button(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg, item="a"))
    assert h.screen["view"] is None and lines(h) == prices.price_answer(reg, "a", False)
    for text in ("zzqx", "#999999"):  # no item: no button
        h = harness(reg)
        run(FLOWS["price"](h.command(), reg, item=text))
        assert h.screen["view"] is None and lines(h)[1] == prices.NO_MATCH


# ---------------------------------------------------------------- /price alone: the watch list


def test_price_alone_lists_the_watch_list_and_opens_one(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    t = now()
    for days, low in ((40, 1800), (20, 1100), (10, 1500), (5, 1240), (3, 1902)):
        keep(reg, t - days * DAY, {PLAGUEBLOOM: Price(low=low, qty=3)})
    reg.watch(UID, PLAGUEBLOOM)
    reg.watch(UID, BAR)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg)
        got = lines(h)
        assert got[0] == "**Your watch list**"
        assert got[1].startswith("Prices as of <t:") and got[1].endswith(":R>"), "the status once, Discord's own stamp"
        assert got[2:] == ["Arcanite Bar: lowest price 48g 99s 98c, 30 listed.", "Plaguebloom: lowest price 15s, 4 listed."], "newest first"
        assert h.selects() == ["Pick an item"] and h.buttons() == []
        sel = h._items()[0]
        assert [(o.label, o.value) for o in sel.options] == [("Arcanite Bar", str(BAR)), ("Plaguebloom", str(PLAGUEBLOOM))]
        await h.pick("Pick an item", str(PLAGUEBLOOM))
        item = lines(h)
        history = prices.history_sentences(reg, PLAGUEBLOOM)
        assert len(history) == 2 and history[0].startswith("Lowest in the last 7 days: 12s 40c · highest: 19s 2c")
        assert item[0] == "Plaguebloom: lowest price 15s, 4 listed." and item[1:3] == history
        assert item[3].startswith("Prices as of <t:")
        assert h.buttons() == ["Stop watching", "Back"]
        await h.press("Stop watching")
        assert lines(h)[0] == "You've stopped watching Plaguebloom." and h.buttons() == ["Watch", "Back"]
        await h.press("Back")
        assert lines(h)[0] == "**Your watch list**" and lines(h)[2:] == ["Arcanite Bar: lowest price 48g 99s 98c, 30 listed."]
    run(go())
    assert reg.watching(UID) == [BAR]


def test_an_unnamed_watched_item_shows_and_can_be_stopped(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    reg.watch(UID, UNNAMED_ID)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg)
        assert lines(h)[2] == f"{prices.UNNAMED}: none listed."
        assert [o.label for o in h._items()[0].options] == [prices.UNNAMED]
        await h.pick("Pick an item", str(UNNAMED_ID))
        await h.press("Stop watching")
        assert lines(h)[0] == "You've stopped watching that item." and h.buttons() == ["Back"], "nothing to watch again by"
    run(go())
    assert reg.watching(UID) == []


def test_an_empty_list(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg))
    assert h.text == prices.EMPTY_LIST_DISCORD and h.screen["view"] is None


def test_an_empty_list_while_prices_are_hidden(reg, market_on):  # noqa: F811
    register(reg)
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)}, at=now() - timedelta(hours=30))  # a day old: hidden (C16)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg))
    assert h.text == prices.EMPTY_LIST_HIDDEN and h.screen["view"] is None, "no /price item to point at while nothing can be watched"


def test_the_full_list_and_the_unregistered_refusals(reg, market_on):  # noqa: F811
    on(reg)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg))
    assert h.text == WATCH_REGISTER_FIRST and h.screen["view"] is None, "no record, no item: register first"

    async def refused(expect):
        h = harness(reg)
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        await h.press("Watch")
        assert lines(h)[0] == expect and h.buttons() == ["Watch"], "the refusal, the button unchanged"
        assert lines(h)[1:] == prices.price_answer(reg, f"#{PLAGUEBLOOM}", False)
    run(refused(WATCH_REGISTER_FIRST))
    assert UID not in reg.members, "a watch never creates a record"
    register(reg)
    for i in range(WATCH_MAX):
        reg.watch(UID, 800_000 + i)
    run(refused(WATCH_FULL))
    assert len(reg.watching(UID)) == WATCH_MAX and PLAGUEBLOOM not in reg.watching(UID)


def test_a_long_list_stays_whole_lines_under_discords_limit(reg, market_on, monkeypatch):  # noqa: F811
    register(reg)
    long = {700_000 + i: f"A Remarkably Long Item Name For Testing Number {i:02d} Of The Watch List" for i in range(WATCH_MAX)}
    real = prices.names
    monkeypatch.setattr(prices, "names", lambda reg: {**real(reg), **long})
    priced(reg, {i: Price(low=99_999_999, qty=999) for i in long}, at=now())
    for i in long:
        reg.watch(UID, i)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg))
    got = lines(h)
    assert len(h.text) <= 2000 and got[-1].startswith("and ") and got[-1].endswith(" more items")
    assert all(x.endswith("listed.") for x in got[2:-1]), "never a cut line"
    assert len(h._items()[0].options) == WATCH_MAX


def test_prices_off_shows_names_and_no_history(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    keep(reg, now() - 3 * DAY, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    reg.watch(UID, PLAGUEBLOOM)
    priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)}, at=now() - timedelta(hours=30))  # a day old: hidden (C16)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg)
        assert lines(h) == ["**Your watch list**", discord_auctions.OLD_WORDS, "Plaguebloom"]
        await h.pick("Pick an item", str(PLAGUEBLOOM))
        assert lines(h) == ["Plaguebloom", discord_auctions.OLD_WORDS], "no price, no history"
        assert h.buttons() == ["Stop watching", "Back"]
    run(go())
    # with an item: today's one sentence, no Watch button (C15)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}"))
    assert h.text == discord_auctions.OLD_WORDS and h.screen["view"] is None
    # prices off: a member reads the names alone
    reg.clear_market("Owner", by_id=OWNER)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg))
    assert lines(h) == ["**Your watch list**", "Plaguebloom"]


def test_prices_hidden_between_the_answer_and_the_press(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)}, at=now() - timedelta(hours=30))
        await h.press("Watch")
        assert h.text == discord_auctions.OLD_WORDS and h.screen["view"] is None
    run(go())
    assert reg.watching(UID) == [], "items are added only while prices show (C15)"


def test_stop_watching_after_prices_went_hidden_still_says_so(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    reg.watch(UID, PLAGUEBLOOM)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        assert h.buttons() == ["Stop watching"]
        priced(reg, {PLAGUEBLOOM: Price(low=1500, qty=4)}, at=now() - timedelta(hours=30))  # a day old: hidden (C16)
        await h.press("Stop watching")
        assert lines(h) == ["You've stopped watching Plaguebloom.", discord_auctions.OLD_WORDS], "the write's sentence stays first"
        assert h.screen["view"] is None
    run(go())
    assert reg.watching(UID) == []


def test_every_answer_is_ephemeral_and_mentions_nobody(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    reg.watch(UID, PLAGUEBLOOM)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        await h.press("Stop watching")
        await FLOWS["price"](h.command(), reg, item="a")
        await FLOWS["price"](h.command(), reg)
        await FLOWS["price"](h.command(), reg, item=f"#{BAR}")
        await h.press("Watch")
        await FLOWS["price"](h.command(), reg)
        await h.pick("Pick an item", str(BAR))
        await h.press("Back")
    run(go())
    assert len(h.screens) >= 9
    for s in h.screens:
        assert s["kw"]["allowed_mentions"].to_dict()["parse"] == [], s["text"]
        assert s["kw"].get("ephemeral", True), "a new message is the asker's only; an edit stays so"
    assert sum(1 for s in h.screens if "ephemeral" in s["kw"]) >= 4


def test_names_are_escaped(reg, market_on, monkeypatch):  # noqa: F811
    register(reg)
    real = prices.names
    monkeypatch.setattr(prices, "names", lambda reg: {**real(reg), 700_001: "Star*Shard_Dust"})
    priced(reg, {700_001: Price(low=100, qty=1)}, at=now())
    reg.watch(UID, 700_001)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg))
    assert lines(h)[2] == "Star\\*Shard\\_Dust: lowest price 1s, 1 listed."
    assert h._items()[0].options[0].label == "Star*Shard_Dust", "a select shows no markdown"


def test_only_the_asker_may_press(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        view = h.screen["view"]
        other = harness(reg, uid=300_000_000_000_000_303, name="Quillfeather")
        itx = other._on_screen() if other.screens else other.command()
        itx.message = types.SimpleNamespace(id=h.screen["message_id"])
        assert await view.interaction_check(itx) is False
        assert other.text == "This belongs to someone else's wizard."
    run(go())
    assert reg.watching(UID) == []


def test_a_restart_says_nothing_changed_until_a_press_writes(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)

    async def go():
        ix = h.command()
        ix.application_id, ix.token = 1, "interaction-token"  # an ephemeral reply is tracked by its webhook
        await FLOWS["price"](ix, reg, item=f"#{PLAGUEBLOOM}")
        key = f"wiz:{id(h.screen['view'])}"
        assert key in pending_views._live and not pending_views._live[key].get("saved"), "a restart: nothing was changed"
        assert not any(isinstance(it, discord.ui.DynamicItem) for it in h.screen["view"].children), "nothing survives a restart"
        await h.press("Watch")
        assert pending_views._live[key].get("saved"), "after a watch: what you saved stays"
    run(go())


def test_no_model_and_no_blizzard_call(reg, market_on, monkeypatch):  # noqa: F811
    from oibot_gm.llm import provider

    def boom(*a, **k):
        raise AssertionError("reached the model or Blizzard")
    register(reg)
    on(reg)
    keep(reg, now() - 3 * DAY, {PLAGUEBLOOM: Price(low=1500, qty=4)})
    monkeypatch.setattr(discord_auctions, "_client", boom)
    monkeypatch.setattr(armory, "client_for", boom)
    monkeypatch.setattr(provider, "get_provider", boom)
    monkeypatch.setattr(provider.AnthropicProvider, "complete", boom)
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg, item=f"#{PLAGUEBLOOM}")
        await h.press("Watch")
        await FLOWS["price"](h.command(), reg)
        await h.pick("Pick an item", str(PLAGUEBLOOM))
        await h.press("Stop watching")
        await h.press("Back")
    run(go())
    assert h.text == prices.EMPTY_LIST_DISCORD, "the list emptied by the press: Back reads the empty list"
    assert reg.watching(UID) == []


def test_tbc(reg, rs, market_on, monkeypatch):  # noqa: F811
    from test_armory_guild import ROOT as _ROOT

    from oibot_gm.profiles import GameProfile

    p = GameProfile.load(_ROOT / "profiles" / "tbc")
    monkeypatch.setattr(reg, "profile", p)
    monkeypatch.setattr(reg, "base_profile", p)
    register(reg)
    for item in (None, "arcanite bar", f"#{BAR}"):
        h = harness(reg)
        run(FLOWS["price"](h.command(), reg, item=item))
        assert h.text == prices.NO_AUCTIONS_WORDS == "This game version has no auction houses." and h.screen["view"] is None


# ---------------------------------------------------------------- the command


def test_the_command_tree_still_builds_with_item_optional(reg, market_on):  # noqa: F811
    from discord import app_commands

    tree = _offline_tree(reg)
    leaves = [c for c in tree.walk_commands() if isinstance(c, app_commands.Command)]
    assert len(leaves) == 91
    cmd = tree.get_command("price")
    assert cmd._params["item"].required is False and len(cmd.description) <= 100
    # the command opens the flow: with no item, the list
    register(reg)
    on(reg)
    h = harness(reg)
    ix = h.command()
    ix.extras, ix.guild_id = {"guild": reg.key}, None
    run(cmd.callback(ix))
    assert h.text == prices.EMPTY_LIST_DISCORD
    ix = h.command()
    ix.extras, ix.guild_id = {"guild": reg.key}, None
    run(cmd.callback(ix, item=f"#{PLAGUEBLOOM}"))
    assert h.buttons() == ["Watch"]


def test_house_switch_reads_the_new_houses_history(reg, market_on):  # noqa: F811
    register(reg)
    on(reg)
    reg.watch(UID, PLAGUEBLOOM)
    assert reg.config.market.house_id == HORDE_AH.id and facts().faction == "Horde"
    h = harness(reg)

    async def go():
        await FLOWS["price"](h.command(), reg)
        await h.pick("Pick an item", str(PLAGUEBLOOM))
        assert lines(h)[1] == prices.NO_HISTORY_YET, "nothing kept for this house yet"
    run(go())


@pytest.mark.parametrize("item", ["", "   "])
def test_a_blank_item_is_the_list(reg, market_on, item):  # noqa: F811
    register(reg)
    on(reg)
    h = harness(reg)
    run(FLOWS["price"](h.command(), reg, item=item))
    assert h.text == prices.EMPTY_LIST_DISCORD
