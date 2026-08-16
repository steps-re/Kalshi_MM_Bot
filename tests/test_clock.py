from datetime import UTC, datetime

from kalshi_mm_bot.market.clock import MarketClock, parse_utc


def test_unknown_ticker_returns_none_not_zero() -> None:
    clock = MarketClock.from_iso_map({"M1": "2026-08-16T12:00:00Z"})

    # None means "no deadline"; zero would mean "closing now" and would make an
    # expiry-aware strategy flatten a market it has all day to work.
    assert clock.seconds_to_close("M2") is None
    assert not clock.knows("M2")


def test_seconds_to_close_counts_down() -> None:
    clock = MarketClock.from_iso_map({"M1": "2026-08-16T12:00:00Z"})

    remaining = clock.seconds_to_close("M1", now_utc="2026-08-16T11:55:00Z")

    assert remaining == 300.0


def test_past_close_clamps_to_zero() -> None:
    clock = MarketClock.from_iso_map({"M1": "2026-08-16T12:00:00Z"})

    assert clock.seconds_to_close("M1", now_utc="2026-08-16T12:05:00Z") == 0.0


def test_naive_timestamps_are_treated_as_utc() -> None:
    clock = MarketClock.from_iso_map({"M1": "2026-08-16T12:00:00"})

    assert clock.seconds_to_close(
        "M1",
        now_utc=datetime(2026, 8, 16, 11, 59, 0),
    ) == 60.0


def test_unparseable_close_times_are_dropped_not_guessed() -> None:
    clock = MarketClock.from_iso_map({"M1": "not a timestamp", "M2": "2026-08-16T12:00:00Z"})

    assert not clock.knows("M1")
    assert clock.knows("M2")


def test_empty_map_produces_an_empty_clock() -> None:
    assert MarketClock.from_iso_map(None).close_times_utc == {}
    assert MarketClock.from_iso_map({}).close_times_utc == {}


def test_round_trips_through_iso() -> None:
    clock = MarketClock.from_iso_map({"M1": "2026-08-16T12:00:00Z"})

    assert MarketClock.from_iso_map(clock.to_iso_map()).close_times_utc == clock.close_times_utc


def test_parse_utc_handles_offsets_and_z() -> None:
    assert parse_utc("2026-08-16T12:00:00Z") == datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert parse_utc("2026-08-16T08:00:00-04:00") == datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert parse_utc("") is None
    assert parse_utc(None) is None
