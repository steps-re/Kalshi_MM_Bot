import pytest

from kalshi_mm_bot.api.parser import parse_market_position, parse_price_ranges
from kalshi_mm_bot.market.types import MarketPosition, PriceRange


def test_parse_price_ranges() -> None:
    price_ranges = parse_price_ranges(
        {
            "ticker": "TEST",
            "price_level_structure": "tapered_deci_cent",
            "price_ranges": [
                {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
            ],
        }
    )

    assert price_ranges == (
        PriceRange(start=0, end=1000, step=10),
        PriceRange(start=1000, end=9000, step=100),
        PriceRange(start=9000, end=10000, step=10),
    )


def test_parse_market_position_accepts_six_decimal_money_fields() -> None:
    position = parse_market_position(
        {
            "type": "market_position",
            "msg": {
                "market_ticker": "TEST",
                "position_fp": "0.25",
                "position_cost_dollars": "0.017450",
                "realized_pnl_dollars": "-0.017450",
                "fees_paid_dollars": "0.017450",
                "volume_fp": "1.25",
            },
        }
    )

    assert position == MarketPosition(
        market_ticker="TEST",
        position=25,
        position_cost=17450,
        realized_pnl=-17450,
        fees_paid=17450,
        volume=125,
    )


def test_fee_reader_finds_the_current_field_name() -> None:
    """Kalshi returns `fee_cost` on REST /portfolio/fills."""

    from kalshi_mm_bot.api.parser import parse_fill_fee_micros

    assert parse_fill_fee_micros({"fee_cost": "0.011700"}) == 11_700


def test_fee_reader_still_understands_legacy_names() -> None:
    from kalshi_mm_bot.api.parser import parse_fill_fee_micros

    assert parse_fill_fee_micros({"fees_paid_dollars": "0.020000"}) == 20_000
    assert parse_fill_fee_micros({"fee_paid_dollars": "0.020000"}) == 20_000


def test_a_missing_fee_field_is_unknown_and_never_zero() -> None:
    """The regression that made every fill on the ledger look free.

    The old reader asked for a field Kalshi had renamed and defaulted the miss
    to "0", so 38 taker fills that were really charged $0.48 in total reported
    as costless - and so did the makers, which is the answer the project wanted.
    A fee we cannot read must never be summable.
    """

    from kalshi_mm_bot.api.parser import parse_fill_fee_micros

    assert parse_fill_fee_micros({"yes_price_dollars": "0.53"}) is None
    assert parse_fill_fee_micros({"fee_cost": ""}) is None
    assert parse_fill_fee_micros({"fee_cost": None}) is None
    assert parse_fill_fee_micros({}) is None


def test_zero_is_reported_as_zero_not_as_unknown() -> None:
    """A genuine zero is a measurement; it must survive as one."""

    from kalshi_mm_bot.api.parser import parse_fill_fee_micros

    assert parse_fill_fee_micros({"fee_cost": "0.000000"}) == 0


def test_an_unparseable_fee_raises_rather_than_reading_as_free() -> None:
    from kalshi_mm_bot.api.parser import parse_fill_fee_micros

    with pytest.raises(ValueError):
        parse_fill_fee_micros({"fee_cost": "not-a-number"})


def test_fill_carries_the_exchange_execution_stamp():
    """Our journal stamps events when we WRITE them. Without the venue's own
    stamp the offline joins cannot tell "the book before the fill" from "the
    book before we heard about the fill", and the difference is the whole
    result of a gate study."""

    from kalshi_mm_bot.api.parser import parse_order_fill

    fill = parse_order_fill({"msg": {
        "trade_id": "t", "order_id": "o", "market_ticker": "M", "action": "buy",
        "side": "yes", "yes_price_dollars": "0.40", "count_fp": "1.00",
        "post_position_fp": "3.00", "is_taker": False, "ts": 1_700_000_000,
    }})

    assert fill.exchange_ts == 1_700_000_000.0


def test_a_millisecond_stamp_is_normalised_to_seconds():
    from kalshi_mm_bot.api.parser import parse_exchange_ts

    assert parse_exchange_ts({"ts": 1_700_000_000_000}) == 1_700_000_000.0


def test_an_iso_stamp_parses():
    from kalshi_mm_bot.api.parser import parse_exchange_ts

    assert parse_exchange_ts(
        {"created_time": "2026-08-19T12:00:00Z"}) == 1787140800.0


def test_a_missing_stamp_is_none_not_now():
    """Guessing here would silently claim zero lag on every fill."""

    from kalshi_mm_bot.api.parser import parse_exchange_ts

    assert parse_exchange_ts({"yes_price_dollars": "0.40"}) is None
