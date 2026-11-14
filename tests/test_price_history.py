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
    day = [p for p in ph.points(K, 1, old, old + timedelta(days=1)) if p.qty is None]
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
    assert [p.low for p in ph.points(K, 4, T0 - timedelta(days=600), T0 - timedelta(days=400))] == [6]
    assert ph.kept_since(K) >= T0 - timedelta(days=365)


def test_a_broken_file_raises_history_error():
    ph.path(K).parent.mkdir(parents=True, exist_ok=True); ph.path(K).write_bytes(b"not a database")
    for call in (lambda: ph.record(K, T0, {1: P(1, 1)}), lambda: ph.points(K, 1, T0, T0 + H), lambda: ph.kept_since(K)):
        with pytest.raises(ph.HistoryError):
            call()
    ph.fold(T0)                                                               # the fold skips a file it can't read
