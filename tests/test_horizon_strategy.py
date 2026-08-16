from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.sim import SimPortfolio
from kalshi_mm_bot.market.fees import ZERO_FEE_MODEL
from kalshi_mm_bot.strategy import strategy_from_name
from kalshi_mm_bot.strategy.horizon import HorizonAwareMarketMaker, parse_horizon_params
from kalshi_mm_bot.strategy.types import StrategyContext

PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


def make_book(
    bid: str = "0.4000",
    ask: str = "0.6000",
    *,
    bid_size: str = "50.00",
    ask_size: str = "50.00",
) -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=((bid, bid_size),),
        asks_raw=((ask, ask_size),),
        price_ranges=PRICE_RANGES,
    )


def make_strategy(**overrides) -> HorizonAwareMarketMaker:
    # A wide book and no fees by default so each test isolates one behaviour.
    defaults = dict(
        count=20 * COUNT_SCALE,
        max_position=100 * COUNT_SCALE,
        min_count=COUNT_SCALE,
        fee_model=ZERO_FEE_MODEL,
        max_quote_away=2_000,
        max_spread=5_000,
        size_ramp_enabled=False,
    )
    defaults.update(overrides)
    return HorizonAwareMarketMaker(**defaults)


def context(offset: float = 0.0, *, seconds_to_close: float | None = None) -> StrategyContext:
    return StrategyContext(
        event_count=1,
        offset_seconds=offset,
        seconds_to_close=seconds_to_close,
    )


def warm_up(strategy, *, seconds_to_close=None, steps=10, book=None) -> None:
    """Feed a stable book so the volatility estimator has samples."""

    for step in range(steps):
        strategy.on_orderbook(
            context(float(step), seconds_to_close=seconds_to_close),
            "M1",
            book or make_book(),
            SimPortfolio(),
        )


def test_quotes_both_sides_of_a_wide_book() -> None:
    strategy = make_strategy()

    intents = strategy.on_orderbook(context(), "M1", make_book(), SimPortfolio())

    assert {intent.action for intent in intents} == {"buy", "sell"}


def test_registered_in_the_strategy_factory() -> None:
    strategy = strategy_from_name("horizon", count=COUNT_SCALE, max_position=10 * COUNT_SCALE)

    assert isinstance(strategy, HorizonAwareMarketMaker)


def test_quoted_spread_always_covers_the_round_trip_fee() -> None:
    # The real schedule at fifty cents needs 3.5 cents of round-trip edge. The
    # strategy is free to quote, but the two quotes together must be at least
    # that far apart or the pair is a guaranteed loss.
    strategy = HorizonAwareMarketMaker(
        count=20 * COUNT_SCALE,
        max_position=100 * COUNT_SCALE,
        max_quote_away=5_000,
        max_spread=5_000,
    )
    fee_model = strategy.fee_model

    intents = strategy.on_orderbook(
        context(),
        "M1",
        make_book("0.4000", "0.6000"),
        SimPortfolio(),
    )

    buy = next(i.yes_price for i in intents if i.action == "buy")
    sell = next(i.yes_price for i in intents if i.action == "sell")
    required = fee_model.breakeven_edge_ticks(yes_price=5000, count=20 * COUNT_SCALE)

    assert sell - buy >= required


def test_quotes_tighter_in_the_tails_than_at_the_midpoint() -> None:
    strategy = HorizonAwareMarketMaker(
        count=20 * COUNT_SCALE,
        max_position=100 * COUNT_SCALE,
        max_quote_away=5_000,
        max_spread=5_000,
    )

    middle = strategy.on_orderbook(context(), "M1", make_book("0.4000", "0.6000"), SimPortfolio())
    tail = strategy.on_orderbook(context(), "M2", make_book("0.0100", "0.2100"), SimPortfolio())

    middle_width = _quoted_width(middle)
    tail_width = _quoted_width(tail)

    # Identical twenty cent books. The tail can be quoted far tighter because
    # the fee is proportional to P*(1-P), which is where the edge actually is.
    assert tail_width < middle_width


def _quoted_width(intents) -> int:
    buy = next(i.yes_price for i in intents if i.action == "buy")
    sell = next(i.yes_price for i in intents if i.action == "sell")
    return sell - buy


def test_fee_band_filter_rejects_expensive_prices() -> None:
    strategy = HorizonAwareMarketMaker(
        count=20 * COUNT_SCALE,
        max_position=100 * COUNT_SCALE,
        max_quote_away=5_000,
        max_spread=5_000,
        max_fee_round_trip_ticks=100,
    )

    middle = strategy.on_orderbook(context(), "M1", make_book("0.4000", "0.6000"), SimPortfolio())
    tail = strategy.on_orderbook(context(), "M2", make_book("0.0100", "0.0900"), SimPortfolio())

    # A one cent round-trip budget rules out the midpoint, where the fee is
    # 3.5 cents, and permits a nickel market, where it is 0.7 cents.
    assert middle == ()
    assert tail != ()


def test_widens_when_volatility_rises() -> None:
    calm = make_strategy(adverse_selection_bps=10_000)
    warm_up(calm)
    calm_intents = calm.on_orderbook(context(10.0), "M1", make_book(), SimPortfolio())

    choppy = make_strategy(adverse_selection_bps=10_000)
    for step in range(10):
        bid = "0.3800" if step % 2 else "0.4000"
        choppy.on_orderbook(
            context(float(step)),
            "M1",
            make_book(bid, f"{float(bid) + 0.2:.4f}"),
            SimPortfolio(),
        )
    choppy_intents = choppy.on_orderbook(context(10.0), "M1", make_book(), SimPortfolio())

    calm_buy = next(i.yes_price for i in calm_intents if i.action == "buy")
    choppy_buy = next(i.yes_price for i in choppy_intents if i.action == "buy")

    # More expected movement over the quote's life means a lower bid.
    assert choppy_buy < calm_buy


def test_long_inventory_skews_quotes_down() -> None:
    strategy = make_strategy()
    flat = SimPortfolio()
    long_book = SimPortfolio(positions={"M1": 50 * COUNT_SCALE})

    flat_intents = strategy.on_orderbook(context(), "M1", make_book(), flat)
    long_intents = strategy.on_orderbook(context(1.0), "M1", make_book(), long_book)

    flat_sell = next(i.yes_price for i in flat_intents if i.action == "sell")
    long_sell = next(i.yes_price for i in long_intents if i.action == "sell")

    # Holding a long position, we want to sell more eagerly.
    assert long_sell < flat_sell


def test_goes_reduce_only_approaching_the_close() -> None:
    strategy = make_strategy(reduce_only_seconds=120.0, stop_quoting_seconds=30.0)
    portfolio = SimPortfolio(positions={"M1": 20 * COUNT_SCALE})

    intents = strategy.on_orderbook(
        context(seconds_to_close=60.0),
        "M1",
        make_book(),
        portfolio,
    )

    # Long into the close: only the side that reduces the position survives.
    assert [intent.action for intent in intents] == ["sell"]


def test_flattens_across_the_spread_in_the_final_seconds() -> None:
    strategy = make_strategy(stop_quoting_seconds=30.0)
    portfolio = SimPortfolio(positions={"M1": 7 * COUNT_SCALE})

    intents = strategy.on_orderbook(
        context(seconds_to_close=5.0),
        "M1",
        make_book(),
        portfolio,
    )

    assert len(intents) == 1
    flatten = intents[0]
    assert flatten.action == "sell"
    assert flatten.count == 7 * COUNT_SCALE
    # Marketable: hitting the bid, not resting above it.
    assert flatten.yes_price == parse_price_fp("0.4000")


def test_quotes_nothing_at_the_close_when_already_flat() -> None:
    strategy = make_strategy(stop_quoting_seconds=30.0)

    assert (
        strategy.on_orderbook(context(seconds_to_close=5.0), "M1", make_book(), SimPortfolio())
        == ()
    )


def test_expiry_controls_are_inert_without_a_close_time() -> None:
    strategy = make_strategy(reduce_only_seconds=120.0, stop_quoting_seconds=30.0)
    portfolio = SimPortfolio(positions={"M1": 20 * COUNT_SCALE})

    intents = strategy.on_orderbook(context(seconds_to_close=None), "M1", make_book(), portfolio)

    # Unknown close time must not be read as "closing now".
    assert {intent.action for intent in intents} == {"buy", "sell"}


def test_size_ramps_with_available_edge() -> None:
    strategy = make_strategy(size_ramp_enabled=True, edge_ramp_ticks=200, min_count=COUNT_SCALE)

    thin_edge = strategy.on_orderbook(
        context(),
        "M1",
        make_book("0.4800", "0.5200"),
        SimPortfolio(),
    )
    fat_edge = strategy.on_orderbook(
        context(1.0),
        "M2",
        make_book("0.3000", "0.7000"),
        SimPortfolio(),
    )

    thin_buy = next(i.count for i in thin_edge if i.action == "buy")
    fat_buy = next(i.count for i in fat_edge if i.action == "buy")

    assert fat_buy > thin_buy


def test_size_shrinks_into_the_close() -> None:
    early = make_strategy(flatten_seconds=60.0, stop_quoting_seconds=None, reduce_only_seconds=None)
    late = make_strategy(flatten_seconds=60.0, stop_quoting_seconds=None, reduce_only_seconds=None)

    early_intents = early.on_orderbook(
        context(seconds_to_close=600.0),
        "M1",
        make_book(),
        SimPortfolio(),
    )
    late_intents = late.on_orderbook(
        context(seconds_to_close=15.0),
        "M1",
        make_book(),
        SimPortfolio(),
    )

    early_buy = next(i.count for i in early_intents if i.action == "buy")
    late_buy = next((i.count for i in late_intents if i.action == "buy"), 0)

    assert late_buy < early_buy


def test_respects_position_capacity() -> None:
    strategy = make_strategy(max_position=10 * COUNT_SCALE)
    portfolio = SimPortfolio(positions={"M1": 10 * COUNT_SCALE})

    intents = strategy.on_orderbook(context(), "M1", make_book(), portfolio)

    # Already at the cap, so no further buying.
    assert "buy" not in {intent.action for intent in intents}


def test_skips_a_book_with_no_size_at_the_touch() -> None:
    strategy = make_strategy(min_top_size=10 * COUNT_SCALE)

    intents = strategy.on_orderbook(
        context(),
        "M1",
        make_book(bid_size="0.10", ask_size="0.10"),
        SimPortfolio(),
    )

    assert intents == ()


def test_skips_a_book_wider_than_max_spread() -> None:
    strategy = make_strategy(max_spread=100)

    assert strategy.on_orderbook(context(), "M1", make_book(), SimPortfolio()) == ()


def test_quotes_land_on_tradable_price_levels() -> None:
    strategy = make_strategy()

    intents = strategy.on_orderbook(context(), "M1", make_book(), SimPortfolio())

    for intent in intents:
        assert intent.yes_price % 100 == 0


def test_never_quotes_across_the_touch() -> None:
    # Quoting inside the spread is intended; quoting through it is not, because
    # a resting order at or beyond the other side trades immediately as a taker.
    strategy = make_strategy()
    book = make_book()

    intents = strategy.on_orderbook(context(), "M1", book, SimPortfolio())

    assert intents != ()

    for intent in intents:
        if intent.action == "buy":
            assert intent.yes_price < book.best_ask
        else:
            assert intent.yes_price > book.best_bid


def test_join_only_mode_never_improves_the_touch() -> None:
    strategy = make_strategy(quote_inside_spread=False)
    book = make_book()

    intents = strategy.on_orderbook(context(), "M1", book, SimPortfolio())

    for intent in intents:
        if intent.action == "buy":
            assert intent.yes_price <= book.best_bid
        else:
            assert intent.yes_price >= book.best_ask


def test_parse_horizon_params_reads_seconds_and_prices() -> None:
    params = parse_horizon_params("stop_quoting_seconds=45, min_profit_edge=0.0100")

    assert params == {"stop_quoting_seconds": 45.0, "min_profit_edge": 100}


def test_parse_horizon_params_rejects_unknown_names() -> None:
    try:
        parse_horizon_params("not_a_param=1")
    except ValueError as error:
        assert "not_a_param" in str(error)
    else:
        raise AssertionError("expected ValueError")
