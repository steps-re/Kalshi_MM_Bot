"""Regression tests for defects found by adversarial review.

Each test names the failure it prevents. They are grouped here rather than
scattered so the next review can see what has already been checked.
"""

import pytest

from kalshi_mm_bot.analytics.performance import attribute_pnl, risk_metrics
from kalshi_mm_bot.market.dynamics import MarketDynamicsTracker
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.sim.accounting import SimPortfolio
from kalshi_mm_bot.market.fees import KalshiFeeModel, ZERO_FEE_MODEL
from kalshi_mm_bot.sim.fills import SimulatedFill
from kalshi_mm_bot.live.risk import RiskLimits
from kalshi_mm_bot.strategy.horizon import HorizonAwareMarketMaker
from kalshi_mm_bot.strategy.types import StrategyContext

PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)
ONE = COUNT_SCALE


def book(bids=(("0.4000", "50.00"),), asks=(("0.6000", "50.00"),)) -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=bids,
        asks_raw=asks,
        price_ranges=PRICE_RANGES,
    )


def fill(*, order_id="o1", action="buy", price=5000, count=ONE) -> SimulatedFill:
    return SimulatedFill(
        fill_id=f"{order_id}-{count}",
        order_id=order_id,
        market_ticker="M1",
        action=action,
        side="yes",
        yes_price=price,
        count=count,
        offset_seconds=0.0,
        observed_at_utc=None,
        fill_model="queue",
        reason="same_level_reduction",
        is_taker=False,
    )


# 1. The per-order fee ceiling was charged once per partial fill.


def test_partial_fills_are_not_charged_the_ceiling_repeatedly() -> None:
    piecemeal = SimPortfolio()

    for _ in range(10):
        piecemeal.apply_fill(fill(order_id="o1", count=ONE))

    single = SimPortfolio()
    single.apply_fill(fill(order_id="o2", count=10 * ONE))

    assert piecemeal.total_fees() == single.total_fees()


def test_separate_orders_each_pay_their_own_ceiling() -> None:
    # At a penny the exact fee is a fraction of a cent, so the ceiling is the
    # whole cost and splitting one order into two doubles it. (At fifty cents
    # both cases round to the same figure, which is why this uses a tail price.)
    two_orders = SimPortfolio()
    two_orders.apply_fill(fill(order_id="a", price=100, count=ONE))
    two_orders.apply_fill(fill(order_id="b", price=100, count=ONE))

    one_order = SimPortfolio()
    one_order.apply_fill(fill(order_id="c", price=100, count=2 * ONE))

    assert two_orders.total_fees() == 2 * one_order.total_fees()


# 2. Reduce-only sized off position capacity, so it could reverse the position.


def test_reduce_only_never_exceeds_the_position_it_is_reducing() -> None:
    strategy = HorizonAwareMarketMaker(
        count=1000 * ONE,
        max_position=1000 * ONE,
        min_count=ONE,
        fee_model=ZERO_FEE_MODEL,
        max_quote_away=5_000,
        max_spread=5_000,
        reduce_only_seconds=120.0,
        stop_quoting_seconds=30.0,
        size_ramp_enabled=False,
    )
    position = 5 * ONE
    portfolio = SimPortfolio(positions={"M1": position})

    intents = strategy.on_orderbook(
        StrategyContext(event_count=1, offset_seconds=0.0, seconds_to_close=60.0),
        "M1",
        book(),
        portfolio,
    )

    assert [intent.action for intent in intents] == ["sell"]
    assert intents[0].count <= position


# 3. An unpriceable short was silently dropped, overstating equity.


def test_short_with_no_ask_is_marked_at_the_worst_case_not_ignored() -> None:
    portfolio = SimPortfolio(fee_model=ZERO_FEE_MODEL, positions={"M1": -2 * ONE})
    one_sided = book(bids=(("0.4000", "50.00"),), asks=())

    value = portfolio.liquidation_value({"M1": one_sided})

    # Two contracts short that cannot be bought back settle at a dollar each.
    assert value == -2 * ONE * ONE_DOLLAR
    assert not portfolio.unwindable_at_touch({"M1": one_sided})


# 4. Liquidation marked the whole position at the touch, ignoring depth.


def test_liquidation_walks_the_book_instead_of_using_only_the_touch() -> None:
    portfolio = SimPortfolio(fee_model=ZERO_FEE_MODEL, positions={"M1": 10 * ONE})
    thin = book(
        bids=(("0.5000", "1.00"), ("0.4000", "9.00")),
        asks=(("0.6000", "50.00"),),
    )

    value = portfolio.liquidation_value({"M1": thin})

    # One contract at 50c and nine at 40c, not ten at 50c.
    assert value == 1 * ONE * 5000 + 9 * ONE * 4000
    assert value < 10 * ONE * 5000


def test_book_deep_enough_to_absorb_the_position_is_unwindable() -> None:
    portfolio = SimPortfolio(fee_model=ZERO_FEE_MODEL, positions={"M1": 2 * ONE})

    assert portfolio.unwindable_at_touch({"M1": book()})


# 5. Retaining stale samples made volatility collapse after a feed gap.


def test_volatility_estimate_is_withdrawn_after_a_feed_gap() -> None:
    tracker = MarketDynamicsTracker(vol_window_seconds=10.0, min_samples=4)

    for step in range(10):
        bid = "0.4000" if step % 2 else "0.4200"
        tracker.observe(
            "M1",
            book(bids=((bid, "50.00"),), asks=(("0.6000", "50.00"),)),
            offset_seconds=float(step),
        )

    # Feed stalls for five minutes, then one update arrives.
    after_gap = tracker.observe("M1", book(), offset_seconds=310.0)

    assert after_gap is not None
    assert not after_gap.has_volatility_estimate


def test_strategy_widens_rather_than_tightens_when_volatility_is_unknown() -> None:
    known = HorizonAwareMarketMaker(
        count=20 * ONE,
        max_position=100 * ONE,
        fee_model=ZERO_FEE_MODEL,
        max_quote_away=5_000,
        max_spread=5_000,
        unknown_volatility_ticks=500,
        adverse_selection_bps=10_000,
        size_ramp_enabled=False,
    )
    context = StrategyContext(event_count=1, offset_seconds=0.0)

    # First event: no samples yet, so the fallback applies.
    cold = known.on_orderbook(context, "M1", book(), SimPortfolio())

    for step in range(1, 15):
        warm_intents = known.on_orderbook(
            StrategyContext(event_count=step, offset_seconds=float(step)),
            "M1",
            book(),
            SimPortfolio(),
        )

    cold_bid = next(i.yes_price for i in cold if i.action == "buy")
    warm_bid = next(i.yes_price for i in warm_intents if i.action == "buy")

    # A quiet book measured over many samples permits a tighter bid than the
    # pessimistic fallback used before any data has arrived.
    assert cold_bid < warm_bid


# 6. attribute_pnl silently mis-valued multi-market sessions.


def test_attribution_refuses_a_single_mark_for_multiple_markets() -> None:
    other = SimulatedFill(
        fill_id="x",
        order_id="o2",
        market_ticker="M2",
        action="buy",
        side="yes",
        yes_price=5000,
        count=ONE,
        offset_seconds=0.0,
        observed_at_utc=None,
        fill_model="queue",
        reason="same_level_reduction",
    )

    with pytest.raises(ValueError, match="multiple markets"):
        attribute_pnl([fill(), other], fees_paid=0, final_position=ONE, final_mid=5000)


def test_attribution_accepts_per_market_marks() -> None:
    other = SimulatedFill(
        fill_id="x",
        order_id="o2",
        market_ticker="M2",
        action="buy",
        side="yes",
        yes_price=4000,
        count=ONE,
        offset_seconds=0.0,
        observed_at_utc=None,
        fill_model="queue",
        reason="same_level_reduction",
        mid_at_fill=4200,
    )
    attribution = attribute_pnl(
        [fill(price=5000, count=ONE), other],
        fees_paid=0,
        final_marks={"M1": (ONE, 5000), "M2": (ONE, 4000)},
    )

    # Each market's residual position is marked at its own price. Both were
    # bought at their marks, so gross is flat, and the only identified edge is
    # the 2c the M2 fill captured against the mid at the time.
    assert attribution.spread_capture == 200 * ONE
    assert attribution.gross == 0


# 7. time_in_drawdown counted samples, not time.


def test_time_in_drawdown_is_weighted_by_elapsed_time() -> None:
    # Underwater for one second, then above water for ninety-nine, but sampled
    # densely during the dip. Sample counting would report ~50%.
    curve = [(0.0, 100), (0.5, 50), (1.0, 100), (100.0, 100)]

    metrics = risk_metrics(curve)

    assert metrics.time_in_drawdown < 0.02


# 8. Negative risk limits inverted the comparison and failed open.


def test_negative_risk_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_session_loss_micros"):
        RiskLimits(max_session_loss_micros=-5_000_000)

    with pytest.raises(ValueError, match="max_abs_position"):
        RiskLimits(max_abs_position=-1)


def test_fee_model_rejects_a_negative_rate() -> None:
    with pytest.raises(ValueError):
        KalshiFeeModel(trading_fee_bps=-1)
