"""The guild's price table and its hourly check (auction prices, spec §4.2, C3, C7, C10): one fetch per guild per hour of
the selected auction house, kept whole at out/auctions/<guild key>/prices.json, stale after six hours, hidden after a
day (a table loaded after a restart too). A failure keeps the last table and warns ops once an hour; a success says
nothing to anyone. A fake auction client stands in for Blizzard and a fake clock for time; names are invented."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import OWNER, OWNER2

from oibot_gm import armory, auctions, discord_auctions
from oibot_gm.armory import ArmoryDown, GuildFacts
from oibot_gm.auctions import House, Price
from oibot_gm.profiles import GameProfile

ROOT = Path(__file__).resolve().parents[1]
BORN = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
HORDE_AH = House(id=6, name="Horde Auction House")
OTHER_HORDE_AH = House(id=18, name="Horde Auction House")
ALLIANCE_AH = House(id=5, name="Alliance Auction House")
NEUTRAL_AH = House(id=7, name="Blackwater Auction House")
EMPTY_HORDE_AH = House(id=30, name="Horde Auction House")
STAMP = "Fri, 13 Nov 2026 20:00:00 GMT"
LATER = "Fri, 13 Nov 2026 21:00:00 GMT"
BAR, DREAMFOIL, RUNECLOTH = 12360, 13463, 14047


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeClient:
    """auctions.Client's two calls, recorded; `tables` per house id, `unchanged` = answer 304 to a matching `since`."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.tables: dict[int, dict[int, Price]] = {6: {BAR: Price(low=489998, qty=12), DREAMFOIL: Price(low=1500, qty=40)},
                                                    18: {BAR: Price(low=250000, qty=3)}, 30: {}}
        self.modified = STAMP
        self.fail: Exception | None = None
        self.houses_list = [ALLIANCE_AH, HORDE_AH, NEUTRAL_AH, OTHER_HORDE_AH, EMPTY_HORDE_AH]
        self.during = None  # called inside a table fetch (the market changing under it)

    def houses(self, ruleset_id):
        self.calls.append(("houses", ruleset_id))
        return list(self.houses_list)

    def table(self, ruleset_id, house_id, since=None):
        self.calls.append(("table", ruleset_id, house_id, since))
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        if since is not None and since == self.modified:
            return None, self.modified
        return dict(self.tables[house_id]), self.modified

    def fetches(self):
        return [c for c in self.calls if c[0] == "table"]


class FakeArmory:
    def __init__(self, ruleset=2):
        self.ruleset = ruleset
        self.calls: list = []

    def guild(self, slug):
        self.calls.append(("guild", slug))
        return facts(ruleset=self.ruleset)


class FakeOps:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def emit(self, cfg, level, text, exc=None, dm=True, dm_only=None):
        self.lines.append((level, text))


class FakeBot:
    def __init__(self):
        self.ops = FakeOps()

    async def send_member_dm(self, *a, **k):  # the DM rule: a fetch never DMs
        raise AssertionError("no DM from a price fetch")


def facts(*, faction="Horde", ruleset=2, gid=418362, name="Lantern Wardens") -> GuildFacts:
    return GuildFacts(id=gid, slug=name.lower().replace(" ", "-"), name=name, faction=faction, kind="Normal", ruleset_id=ruleset,
                      members=8, created=BORN)


def commits(reg) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=reg.store.root, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeClient()
    monkeypatch.setattr(discord_auctions, "_client", lambda reg: f)
    monkeypatch.setattr(armory, "available", lambda reg: bool(reg.profile.armory))
    monkeypatch.setattr(discord_auctions, "OUT", tmp_path / "auctions-out")
    discord_auctions.reset_state()
    yield f
    discord_auctions.reset_state()


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(discord_auctions, "_now", c)
    return c


@pytest.fixture
def bot():
    return FakeBot()


def market(reg, house=HORDE_AH, ruleset=2):
    if reg.config.armory is None:
        reg.set_armory_guild(facts(ruleset=ruleset), "Owner", by_id=OWNER)
    reg.set_market(house, ruleset, "Owner", by_id=OWNER)
    return reg


def prices_file(reg) -> Path:
    return discord_auctions.OUT / reg.key / "prices.json"


# ------------------------------------------------------------------ the fetch


def test_first_fetch_writes_the_table_and_a_second_within_the_hour_does_nothing(reg, fake, bot, clock):
    market(reg)
    assert discord_auctions.state(reg) == "waiting"
    assert run(discord_auctions.refresh(bot, reg)) == "updated"
    assert fake.fetches() == [("table", 2, 6, None)]
    t = discord_auctions.table(reg)
    assert (t.at, t.house_id, t.ruleset_id, t.modified) == (clock(), 6, 2, STAMP)
    assert t.prices[BAR] == Price(low=489998, qty=12)
    d = json.loads(prices_file(reg).read_text(encoding="utf-8"))
    assert d["house_id"] == 6 and d["ruleset_id"] == 2
    assert not list(prices_file(reg).parent.glob("*.tmp")), "written whole: no temp file left behind"
    assert discord_auctions.state(reg) == "fresh"
    assert discord_auctions.price_of(reg, BAR) == Price(low=489998, qty=12)
    assert discord_auctions.price_of(reg, RUNECLOTH) is None
    clock.advance(minutes=59)
    assert run(discord_auctions.refresh(bot, reg)) == "unchanged"
    assert len(fake.fetches()) == 1


def test_not_modified_keeps_the_table_and_its_time_only_the_checked_time_moves(reg, fake, bot, clock):
    """F-2: `at` is the feed's time — a 304 leaves it; the checked time moves and drives the hourly schedule."""
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    first = discord_auctions.table(reg)
    clock.advance(hours=5)
    assert run(discord_auctions.refresh(bot, reg)) == "unchanged"  # due: asked again on the stored Last-Modified
    assert fake.fetches()[-1] == ("table", 2, 6, STAMP)
    t = discord_auctions.table(reg)
    assert t.prices == first.prices and t.at == first.at and t.checked_at == clock()
    clock.advance(minutes=30)
    assert run(discord_auctions.refresh(bot, reg)) == "unchanged"
    assert len(fake.fetches()) == 2, "checked within the hour: no second fetch"
    reread = json.loads(prices_file(reg).read_text(encoding="utf-8"))
    assert datetime.fromisoformat(reread["at"]) == first.at
    assert datetime.fromisoformat(reread["checked_at"]) == clock() - timedelta(minutes=30)


def test_a_stalled_feed_reads_stale_then_hidden(reg, fake, bot, clock):
    """F-2: Blizzard's feed stops moving (every hourly answer a 304): the prices age from the feed's time."""
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    for _ in range(7):
        clock.advance(hours=1)
        assert run(discord_auctions.refresh(bot, reg)) == "unchanged"
    assert len(fake.fetches()) == 8
    assert discord_auctions.state(reg) == "stale"
    assert discord_auctions.as_of_words(reg) == f"Prices as of {reg.local12(clock() - timedelta(hours=7))} — more than six hours old"
    for _ in range(18):
        clock.advance(hours=1)
        run(discord_auctions.refresh(bot, reg))
    assert discord_auctions.state(reg) == "old" and discord_auctions.price_of(reg, BAR) is None


def test_a_day_old_table_after_a_restart_stays_hidden_through_a_not_modified(reg, fake, bot, clock):
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    discord_auctions.reset_state()  # a restart: only the file is left
    clock.advance(hours=30)
    assert run(discord_auctions.refresh(bot, reg)) == "unchanged"
    assert fake.fetches()[-1] == ("table", 2, 6, STAMP), "a 304"
    assert discord_auctions.state(reg) == "old" and discord_auctions.price_of(reg, BAR) is None


def test_prices_as_of_is_the_feeds_time(reg, fake, bot, clock):
    """F-2: an update's Last-Modified two hours ago reads 'as of' that time; never later than now; none or unreadable = now."""
    market(reg)
    fake.modified = "Fri, 13 Nov 2026 18:00:00 GMT"
    assert run(discord_auctions.refresh(bot, reg)) == "updated"
    t = discord_auctions.table(reg)
    assert t.at == clock() - timedelta(hours=2) and t.checked_at == clock()
    assert discord_auctions.as_of_words(reg) == f"Prices as of {reg.local12(clock() - timedelta(hours=2))}"
    clock.advance(hours=1)
    fake.modified = "Sat, 14 Nov 2026 03:00:00 GMT"  # a clock ahead of ours
    run(discord_auctions.refresh(bot, reg))
    assert discord_auctions.table(reg).at == clock()
    clock.advance(hours=1)
    fake.modified = None
    run(discord_auctions.refresh(bot, reg))
    assert discord_auctions.table(reg).at == clock()
    clock.advance(hours=1)
    fake.modified = "not a date"
    run(discord_auctions.refresh(bot, reg))
    assert discord_auctions.table(reg).at == clock()


def test_a_file_written_before_the_checked_time_loads_with_checked_at_its_time(reg, fake, bot, clock):
    market(reg)
    prices_file(reg).parent.mkdir(parents=True)
    at = clock() - timedelta(minutes=20)
    prices_file(reg).write_text(json.dumps({"at": at.isoformat(), "house_id": 6, "ruleset_id": 2, "modified": STAMP,
                                            "prices": {str(BAR): [489998, 12]}}), encoding="utf-8")
    t = discord_auctions.table(reg)
    assert t.at == at and t.checked_at == at
    assert run(discord_auctions.refresh(bot, reg)) == "unchanged" and fake.fetches() == [], "checked 20 minutes ago"


def test_stale_after_six_hours_and_hidden_after_a_day(reg, fake, bot, clock):
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    words = discord_auctions.as_of_words(reg)
    assert words == f"Prices as of {reg.local12(clock())}"
    clock.advance(hours=6)
    assert discord_auctions.state(reg) == "stale"
    assert discord_auctions.price_of(reg, BAR) == Price(low=489998, qty=12)
    assert discord_auctions.as_of_words(reg).endswith(" — more than six hours old")
    clock.advance(hours=18)
    assert discord_auctions.state(reg) == "old"
    assert discord_auctions.price_of(reg, BAR) is None
    old = discord_auctions.as_of_words(reg)
    assert "out of date" in old and "Prices as of" not in old


def test_a_table_older_than_a_day_is_hidden_after_a_restart(reg, fake, bot, clock):
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    discord_auctions.reset_state()  # a restart: only the file is left
    clock.advance(hours=30)
    assert discord_auctions.table(reg) is not None
    assert discord_auctions.state(reg) == "old"
    assert discord_auctions.price_of(reg, BAR) is None
    assert "out of date" in discord_auctions.as_of_words(reg)


def test_a_corrupt_or_missing_file_is_no_table(reg, fake, bot, clock):
    market(reg)
    assert discord_auctions.table(reg) is None
    assert discord_auctions.state(reg) == "waiting"
    assert "yet" in discord_auctions.as_of_words(reg)
    prices_file(reg).parent.mkdir(parents=True)
    prices_file(reg).write_text("{not json", encoding="utf-8")
    discord_auctions.reset_state()
    assert discord_auctions.table(reg) is None
    prices_file(reg).write_text(json.dumps({"at": "yesterday", "house_id": 6}), encoding="utf-8")
    discord_auctions.reset_state()
    assert discord_auctions.table(reg) is None
    assert run(discord_auctions.refresh(bot, reg)) == "updated", "a damaged file is read again from the auction house"


def test_a_failed_fetch_keeps_the_table_and_warns_once_an_hour(reg, fake, bot, clock):
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    kept = discord_auctions.table(reg)
    fake.fail = ArmoryDown(detail="503 secret-free detail")
    clock.advance(hours=1)
    assert run(discord_auctions.refresh(bot, reg)) == "failed"
    assert discord_auctions.table(reg) == kept
    assert len(bot.ops.lines) == 1
    level, text = bot.ops.lines[0]
    assert level == "warn" and "Horde Auction House" in text and armory.DOWN_WORDS in text
    clock.advance(minutes=20)
    assert run(discord_auctions.refresh(bot, reg, force=True)) == "failed"
    assert len(bot.ops.lines) == 1, "one warning per guild per hour"
    clock.advance(hours=1)
    assert run(discord_auctions.refresh(bot, reg)) == "failed"
    assert len(bot.ops.lines) == 2


def test_a_fetch_for_a_house_no_longer_selected_is_discarded(reg, fake, bot, clock):
    market(reg)
    fake.during = lambda: reg.set_market(OTHER_HORDE_AH, 2, "Owner", by_id=OWNER)  # the owner switches mid-fetch
    run(discord_auctions.refresh(bot, reg))
    assert discord_auctions.table(reg) is None
    assert not prices_file(reg).exists()
    fake.during = None
    assert run(discord_auctions.refresh(bot, reg)) == "updated"
    assert discord_auctions.table(reg).house_id == 18
    # the owner clears it while the next one is in flight
    fake.during = lambda: reg.clear_market("Owner", by_id=OWNER)
    clock.advance(hours=2)
    fake.modified = LATER
    assert run(discord_auctions.refresh(bot, reg)) == "off"
    assert json.loads(prices_file(reg).read_text(encoding="utf-8"))["modified"] == STAMP, "nothing stored from it"
    assert discord_auctions.state(reg) == "off"


def test_a_stored_table_of_another_house_is_no_table(reg, fake, bot, clock):
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    reg.set_market(OTHER_HORDE_AH, 2, "Owner", by_id=OWNER)
    assert discord_auctions.table(reg) is None and discord_auctions.state(reg) == "waiting"


def test_two_guilds_on_one_house_share_one_fetch(duo, fake, bot, clock):
    a, b = duo.a, duo.b
    market(a)
    b.set_armory_guild(facts(gid=518362, name="Ember Quill"), "Owner", by_id=OWNER2)
    b.set_market(HORDE_AH, 2, "Owner", by_id=OWNER2)
    assert run(discord_auctions.refresh(bot, a)) == "updated"
    clock.advance(minutes=10)
    assert run(discord_auctions.refresh(bot, b)) == "updated"
    assert len(fake.fetches()) == 1
    assert discord_auctions.table(b).prices == discord_auctions.table(a).prices
    assert (discord_auctions.OUT / b.key / "prices.json").exists()


def test_a_second_guild_taking_an_old_shared_read_on_a_not_modified_answer_is_updated(duo, fake, bot, clock):
    a, b = duo.a, duo.b
    market(a)
    b.set_armory_guild(facts(gid=518362, name="Ember Quill"), "Owner", by_id=OWNER2)
    b.set_market(HORDE_AH, 2, "Owner", by_id=OWNER2)
    assert run(discord_auctions.refresh(bot, a)) == "updated"
    clock.advance(hours=2)
    heard: list[str] = []
    discord_auctions.PRICE_LISTENERS.append(heard.append)
    try:
        assert run(discord_auctions.refresh(bot, b)) == "updated"
    finally:
        discord_auctions.PRICE_LISTENERS.remove(heard.append)
    assert fake.fetches()[-1] == ("table", 2, 6, STAMP), "asked on the shared read's Last-Modified: a 304"
    assert discord_auctions.state(b) == "fresh" and discord_auctions.table(b).prices == discord_auctions.table(a).prices
    assert heard == [b.key]


def test_tick_starts_one_background_task_when_due(reg, fake, bot, clock):
    market(reg)

    async def go():
        task = await discord_auctions.auctions_tick(bot, reg)
        assert task is not None
        assert await discord_auctions.auctions_tick(bot, reg) is None, "one task per guild at a time"
        await task
        assert await discord_auctions.auctions_tick(bot, reg) is None, "not due within the hour"

    run(go())
    assert len(fake.fetches()) == 1 and discord_auctions.state(reg) == "fresh"


def test_a_failed_tick_waits_an_hour_before_trying_again(reg, fake, bot, clock):
    market(reg)
    fake.fail = ArmoryDown()

    async def go():
        await (await discord_auctions.auctions_tick(bot, reg))
        clock.advance(minutes=5)
        assert await discord_auctions.auctions_tick(bot, reg) is None
        clock.advance(hours=1)
        assert await discord_auctions.auctions_tick(bot, reg) is not None

    run(go())


def test_tick_is_off_without_a_market_and_on_tbc(reg, fake, bot, clock, monkeypatch):
    assert run(discord_auctions.auctions_tick(bot, reg)) is None
    assert run(discord_auctions.refresh(bot, reg)) == "off"
    assert discord_auctions.state(reg) == "off"
    assert "auction house" in discord_auctions.as_of_words(reg)
    assert fake.calls == []

    tbc = GameProfile.load(ROOT / "profiles" / "tbc")
    assert not tbc.auctions

    class Reg:
        key, profile = "t", tbc
        config = type("C", (), {"armory": None, "market": None})()

    assert not auctions.available(Reg())
    assert run(discord_auctions.auctions_tick(bot, Reg())) is None
    assert run(discord_auctions.refresh(bot, Reg())) == "off"
    assert discord_auctions.state(Reg()) == "off"
    assert fake.calls == []


def test_no_dm_no_notice_no_ops_line_on_success(reg, fake, bot, clock):
    market(reg)
    heard: list[str] = []
    discord_auctions.PRICE_LISTENERS.append(heard.append)
    try:
        before = commits(reg)
        assert run(discord_auctions.refresh(bot, reg)) == "updated"
        clock.advance(hours=2)
        assert run(discord_auctions.refresh(bot, reg)) == "unchanged"
    finally:
        discord_auctions.PRICE_LISTENERS.remove(heard.append)
    assert bot.ops.lines == []
    assert commits(reg) == before, "never the data repo"
    assert all(not m.notices for m in reg.members.values() if hasattr(m, "notices"))
    assert heard == [reg.key], "listeners hear an update only"


# ------------------------------------------------------------------ the owner's picker


def test_house_choices_offer_only_the_faction_houses_with_listings(reg, fake, bot, clock, monkeypatch):
    reg.set_armory_guild(facts(ruleset=2), "Owner", by_id=OWNER)
    arm = FakeArmory(ruleset=2)
    monkeypatch.setattr(armory, "client_for", lambda reg: arm)
    choices = run(discord_auctions.house_choices(bot, reg))
    assert arm.calls == [("guild", "lantern-wardens")]
    assert ("houses", 2) in fake.calls
    assert [c.house for c in choices] == [HORDE_AH, OTHER_HORDE_AH], "the guild's faction only, an empty one skipped"
    first = choices[0]
    assert first.ruleset_id == 2 and first.listings == 52
    assert first.samples == [(BAR, 489998), (DREAMFOIL, 1500), (RUNECLOTH, None)]
    assert [c[2] for c in fake.fetches()] == [6, 18, 30], "never another faction's or the neutral house"


def test_a_stale_or_hidden_table_is_heard_only_when_the_feed_moves(reg, fake, bot, clock):
    """A 304 leaves the feed's time, so it can no longer bring a stale or hidden table back to fresh: nothing to hear. A new
    answer holding the same prices moves the feed's time: an open page showing "more than six hours old" (or no prices,
    past a day) hears the table come back to fresh."""
    market(reg)
    run(discord_auctions.refresh(bot, reg))
    heard: list[str] = []
    discord_auctions.PRICE_LISTENERS.append(heard.append)
    try:
        clock.advance(hours=7)
        assert discord_auctions.state(reg) == "stale"
        assert run(discord_auctions.refresh(bot, reg)) == "unchanged"  # a 304 on the stored Last-Modified
        assert fake.fetches()[-1] == ("table", 2, 6, STAMP)
        assert discord_auctions.state(reg) == "stale" and heard == []
        clock.advance(hours=1)
        fake.modified = "Sat, 14 Nov 2026 03:30:00 GMT"  # a new answer holding the same prices, half an hour old
        assert run(discord_auctions.refresh(bot, reg)) == "updated"
        assert discord_auctions.state(reg) == "fresh" and heard == [reg.key]
        clock.advance(hours=25)
        assert discord_auctions.state(reg) == "old"
        fake.modified = "Sun, 15 Nov 2026 05:00:00 GMT"
        assert run(discord_auctions.refresh(bot, reg)) == "updated"
        assert heard == [reg.key, reg.key]
        clock.advance(hours=1)
        fake.modified = "Sun, 15 Nov 2026 06:00:00 GMT"
        assert run(discord_auctions.refresh(bot, reg)) == "unchanged", "fresh to fresh with the same prices: nothing to hear"
        assert heard == [reg.key, reg.key]
    finally:
        discord_auctions.PRICE_LISTENERS.remove(heard.append)


def test_an_open_page_hears_the_table_turn_stale_then_hidden_after_failed_checks(reg, fake, bot, clock):
    """F-4: the tick remembers the state it last saw per guild and tells the listeners once when time alone changes it
    (fresh → stale at six hours, stale → old at a day), with every check failing in between."""
    market(reg)
    heard: list[str] = []

    async def go():
        await (await discord_auctions.auctions_tick(bot, reg))  # the first read
        fake.fail = ArmoryDown()
        discord_auctions.PRICE_LISTENERS.append(heard.append)
        try:
            for _ in range(26 * 12):  # every five minutes for 26 hours
                clock.advance(minutes=5)
                task = await discord_auctions.auctions_tick(bot, reg)
                if task is not None:
                    await task
                if clock() - discord_auctions.table(reg).at == timedelta(hours=6, minutes=5):
                    assert heard == [reg.key], "stale: heard once"
        finally:
            discord_auctions.PRICE_LISTENERS.remove(heard.append)

    run(go())
    assert discord_auctions.state(reg) == "old"
    assert heard == [reg.key, reg.key], "stale once, then hidden once; nothing in between"
