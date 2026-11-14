"""The kept price history per auction house (price watch, spec §4.1, C2, C10): a row only when an item's lowest price or
quantity listed changed, once per house per feed time; a span reads the price carried in from before it; the daily fold
keeps every change for 90 days, then one lowest and one highest per item per day, and nothing past a year. A tmp root
stands in for out/auctions; no network, no Blizzard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oibot_gm import auctions, discord_auctions
from oibot_gm import price_history as ph

K = ("dynamic-forever-us", 1, 6)
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)
P = auctions.Price


@pytest.fixture(autouse=True)
def root(monkeypatch, tmp_path):
    monkeypatch.setattr(discord_auctions, "OUT", tmp_path)
    return tmp_path


def test_first_record_keeps_every_item_then_only_changes():
    assert ph.record(K, T0, {1: P(100, 5), 2: P(50, 1)}) == 2
    assert ph.record(K, T0 + H, {1: P(100, 5), 2: P(40, 1)}) == 1          # item 1 unchanged
    assert ph.record(K, T0 + 2 * H, {1: P(100, 7), 2: P(40, 1)}) == 1      # quantity alone is a change


def test_the_same_or_an_earlier_feed_time_writes_nothing():
    ph.record(K, T0 + H, {1: P(100, 5)})
    assert ph.record(K, T0 + H, {1: P(90, 5)}) == 0
    assert ph.record(K, T0, {1: P(80, 5)}) == 0
    assert [p.low for p in ph.points(K, 1, T0, T0 + 3 * H)] == [100]


def test_delisted_then_relisted_is_a_gap():
    ph.record(K, T0, {1: P(100, 5)}); ph.record(K, T0 + H, {}); ph.record(K, T0 + 2 * H, {1: P(120, 2)})
    assert [(p.low, p.qty) for p in ph.points(K, 1, T0, T0 + 3 * H)] == [(100, 5), (None, 0), (120, 2)]
    assert ph.span(K, 1, 1, T0 + 3 * H) == ph.Span(low=100, high=120)


def test_a_price_unchanged_before_the_span_is_carried_forward():
    ph.record(K, T0, {1: P(100, 5)})
    now = T0 + timedelta(days=10)
    pts = ph.points(K, 1, now - timedelta(days=7), now)
    assert [(p.at, p.low) for p in pts] == [(now - timedelta(days=7), 100)]
    assert ph.span(K, 1, 7, now) == ph.Span(low=100, high=100)


def test_nothing_listed_all_span_and_an_unknown_item():
    ph.record(K, T0, {1: P(100, 5)}); ph.record(K, T0 + H, {})
    assert ph.span(K, 1, 7, T0 + timedelta(days=9)) == ph.Span(None, None)
    assert ph.points(K, 99, T0, T0 + H) == [] and ph.span(K, 99, 7, T0 + H) == ph.Span(None, None)


def test_houses_are_kept_apart_and_kept_since():
    other = ("dynamic-forever-us", 1, 7)
    ph.record(K, T0, {1: P(100, 5)}); ph.record(other, T0 + H, {1: P(300, 1)})
    assert ph.kept_since(K) == T0 and ph.kept_since(other) == T0 + H and ph.kept_since(("x", 9, 9)) is None
    assert ph.path(K) != ph.path(other) and ph.path(K).parent == ph.root()


def test_the_file_name_is_letters_digits_and_dashes():
    name = ph.path(("dynamic forever/../us", 1, 6)).stem
    assert name and all(c.isalnum() or c == "-" for c in name)
    assert ph.path(("dynamic forever/../us", 1, 6)).parent == ph.root()


def test_fold_keeps_a_low_and_high_per_day_then_drops_a_year():
    old = T0 - timedelta(days=100)
    ph.record(K, old, {1: P(100, 5)}); ph.record(K, old + H, {1: P(140, 5)}); ph.record(K, old + 2 * H, {1: P(90, 5)})
    ph.record(K, T0, {1: P(110, 5)})
    ph.fold(T0 + timedelta(days=1))
    day = [p for p in ph.points(K, 1, old, old + timedelta(days=1)) if p.qty is None and not p.unread]  # the 100 unread days aside
    assert [(p.low, p.high) for p in day] == [(90, 140)]
    assert [p.low for p in ph.points(K, 1, T0, T0 + H)][-1] == 110            # recent changes untouched
    ph.fold(T0 + timedelta(days=400))
    assert not ph.path(K).exists()                                            # nothing left: the file goes


def test_fold_carries_the_last_value_when_a_day_had_no_change():
    """A price set 100 days ago and never changed still reads after the fold (the fold never loses the standing price)."""
    ph.record(K, T0 - timedelta(days=100), {1: P(100, 5)})
    ph.fold(T0)
    assert ph.span(K, 1, 7, T0) == ph.Span(100, 100)


def test_fold_twice_is_the_same_and_a_standing_value_outlives_a_year_while_the_house_is_read():
    ph.record(K, T0 - timedelta(days=500), {1: P(100, 5), 2: P(7, 1), 4: P(5, 1)})
    ph.record(K, T0 - timedelta(days=450), {1: P(100, 5), 2: P(7, 1), 4: P(6, 1)})
    ph.record(K, T0 - timedelta(days=200), {2: P(9, 1)})                     # items 1 and 4 delisted, item 2 changed
    ph.record(K, T0, {2: P(9, 1), 3: P(1, 1)})                               # the house still read
    ph.fold(T0); ph.fold(T0)
    assert ph.span(K, 2, 7, T0) == ph.Span(9, 9)                              # P2: the standing value stays
    assert ph.span(K, 1, 7, T0) == ph.Span(None, None)
    # past a year only each item's last value before the year stays (the carried start); the older day goes
    assert [p.low for p in ph.points(K, 4, T0 - timedelta(days=600), T0 - timedelta(days=400)) if not p.unread] == [6]
    assert ph.kept_since(K) >= T0 - timedelta(days=365)


# ------------------------------------------------------------------ time the bot never read (final review F-5)

D = timedelta(days=1)


def test_a_long_unread_stretch_is_kept_as_a_gap_and_reads_as_one():
    """20 days without a read (the house switched away and back, prices off, the bot down): never a flat price."""
    assert ph.GAP_AFTER == timedelta(hours=3)
    start, back = T0, T0 + 20 * D
    ph.record(K, start, {1: P(100, 5), 2: P(50, 1)})
    ph.record(K, back, {1: P(100, 5), 2: P(70, 2), 3: P(9, 1)})               # item 1 resumes unchanged, item 2 changed, item 3 new
    now = back + D
    pts = ph.points(K, 1, start - D, now)
    assert [(p.at, p.low, p.high, p.qty, p.unread) for p in pts] == [
        (start, 100, 100, 5, False), (start, None, None, None, True), (back, 100, 100, 5, False)], "a break, then the value it resumed with"
    pts = ph.points(K, 2, start - D, now)
    assert [(p.at, p.low, p.unread) for p in pts] == [(start, 50, False), (start, None, True), (back, 70, False)], \
        "a change at the resume is its own point (no second one)"
    assert [(p.at, p.low, p.unread) for p in ph.points(K, 3, start - D, now)] == [(back, 9, False)], "new at the resume: no gap of its own"
    # a window starting inside the gap: the unread point at its start, never the carried price
    pts = ph.points(K, 1, start + 10 * D, now)
    assert [(p.at, p.low, p.unread) for p in pts] == [(start + 10 * D, None, True), (back, 100, False)]
    # a window ending inside the gap: no resume point
    assert [(p.at, p.unread) for p in ph.points(K, 1, start - D, start + 5 * D)] == [(start, False), (start, True)]
    # the span: unread time counts for neither
    assert ph.span(K, 2, 7, back - D) == ph.Span(None, None), "7 days wholly unread"
    assert ph.span(K, 2, 30, now) == ph.Span(50, 70)
    assert ph.span(K, 2, 1, now + H) == ph.Span(70, 70)


def test_short_stretches_and_an_unlisted_item_get_no_gap_points():
    ph.record(K, T0, {1: P(100, 5), 2: P(50, 1)})
    ph.record(K, T0 + 3 * H, {1: P(100, 5)})                                   # exactly three hours: still read; item 2 delisted
    assert [p.unread for p in ph.points(K, 1, T0, T0 + 4 * H)] == [False]
    ph.record(K, T0 + 3 * H + 20 * D, {1: P(100, 5)})                          # a long stretch: item 2 was unlisted going in
    pts = ph.points(K, 2, T0, T0 + 21 * D)
    assert [(p.low, p.unread) for p in pts] == [(50, False), (None, False)], "nothing listed going in: no extra points"
    assert ph.span(K, 2, 7, T0 + 21 * D) == ph.Span(None, None)


def test_the_fold_keeps_gaps_and_drops_those_older_than_a_year():
    old = T0 - 400 * D
    ph.record(K, old, {1: P(100, 5)})
    ph.record(K, old + 10 * D, {1: P(100, 5)})                                 # a gap wholly older than a year
    ph.record(K, T0 - 30 * D, {1: P(120, 5)})                                  # a gap from 390 to 30 days ago: partly inside the year
    ph.record(K, T0, {1: P(120, 5)})                                           # and one from 30 days ago to now
    import sqlite3

    def gaps():
        with sqlite3.connect(ph.path(K)) as con:
            return con.execute("SELECT start, stop FROM unread ORDER BY start").fetchall()
    assert len(gaps()) == 3
    ph.fold(T0)
    s = lambda t: int(t.timestamp())  # noqa: E731
    assert gaps() == [(s(old + 10 * D), s(T0 - 30 * D)), (s(T0 - 30 * D), s(T0))], "the year-old gap goes, the rest stay as they are"
    pts = ph.points(K, 1, T0 - 7 * D, T0)
    assert [(p.low, p.unread) for p in pts] == [(None, True), (120, False)]
    ph.fold(T0 + 400 * D)
    assert not ph.path(K).exists(), "a file with nothing left is still deleted"


def test_a_file_made_before_gaps_were_kept_still_opens_and_reads():
    import sqlite3

    ph.path(K).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ph.path(K)) as con:  # the schema as Task 1 built it: no unread table
        con.executescript("""
        CREATE TABLE meta (one INTEGER PRIMARY KEY CHECK (one = 1), first_at INTEGER NOT NULL, last_at INTEGER NOT NULL);
        CREATE TABLE changes (item INTEGER NOT NULL, at INTEGER NOT NULL, low INTEGER, high INTEGER, qty INTEGER,
                              PRIMARY KEY (item, at)) WITHOUT ROWID;
        CREATE TABLE last (item INTEGER PRIMARY KEY, low INTEGER, qty INTEGER NOT NULL);
        """)
        t = int(T0.timestamp())
        con.execute("INSERT INTO meta VALUES (1, ?, ?)", (t, t))
        con.execute("INSERT INTO changes VALUES (1, ?, 100, 100, 5)", (t,))
        con.execute("INSERT INTO last VALUES (1, 100, 5)")
    assert [(p.low, p.unread) for p in ph.points(K, 1, T0, T0 + H)] == [(100, False)]
    assert ph.span(K, 1, 1, T0 + H) == ph.Span(100, 100) and ph.kept_since(K) == T0
    assert ph.record(K, T0 + 10 * D, {1: P(90, 5)}) == 1
    assert [(p.low, p.unread) for p in ph.points(K, 1, T0, T0 + 11 * D)] == [(100, False), (None, True), (90, False)]
    ph.fold(T0 + 11 * D)


def test_a_broken_file_raises_history_error():
    ph.path(K).parent.mkdir(parents=True, exist_ok=True); ph.path(K).write_bytes(b"not a database")
    for call in (lambda: ph.record(K, T0, {1: P(1, 1)}), lambda: ph.points(K, 1, T0, T0 + H), lambda: ph.kept_since(K)):
        with pytest.raises(ph.HistoryError):
            call()
    ph.fold(T0)                                                               # the fold skips a file it can't read
