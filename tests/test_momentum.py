"""Momentum signal, defence and taker.

The sign convention is the part worth guarding. It was written backwards first
time - suppressing the bid after an up-move - and a defence that removes the
safe side while leaving the exposed one is worse than no defence at all, because
it costs fills and buys nothing.
"""

import pytest

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.strategy.defended import MomentumDefendedStrategy
from kalshi_mm_bot.strategy.momentum import MomentumConfig, MomentumTracker
from kalshi_mm_bot.strategy.momentum_taker import MomentumTakerStrategy
from kalshi_mm_bot.strategy.types import QuoteIntent, StrategyContext

ONE = COUNT_SCALE
PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


def book(bid="0.4900", ask="0.5000", bid_size="500.00", ask_size="500.00"):
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=((bid, bid_size),),
        asks_raw=((ask, ask_size),),
        price_ranges=PRICE_RANGES,
    )


class Flat:
    def position(self, market_ticker):
        return 0


def test_no_signal_until_the_move_clears_the_trigger() -> None:
    tracker = MomentumTracker()

    assert tracker.observe("M1", 5000, offset_seconds=0.0) is None
    # Half a cent over the full lookback is not a trigger.
    assert tracker.observe("M1", 5050, offset_seconds=30.0) is None


def test_no_signal_until_the_lookback_has_elapsed() -> None:
    """A two-cent jump in one second is not a thirty-second move."""

    tracker = MomentumTracker()
    tracker.observe("M1", 5000, offset_seconds=0.0)

    assert tracker.observe("M1", 5200, offset_seconds=1.0) is None


def test_a_cent_over_the_lookback_fires() -> None:
    tracker = MomentumTracker()
    tracker.observe("M1", 5000, offset_seconds=0.0)
    signal = tracker.observe("M1", 5100, offset_seconds=30.0)

    assert signal is not None
    assert signal.is_up
    assert signal.move_ticks == 100


def test_an_up_move_suppresses_selling_not_buying() -> None:
    """The exposed quote after an up-move is the offer.

    A continuing rise lifts our offer and leaves us short into it. Our bid is
    the safe side - the market is walking away from it, and a fill there means
    the price came back, which is the good case.
    """

    tracker = MomentumTracker()
    tracker.observe("M1", 5000, offset_seconds=0.0)
    tracker.observe("M1", 5100, offset_seconds=30.0)

    assert tracker.suppresses("M1", action="sell", offset_seconds=31.0)
    assert not tracker.suppresses("M1", action="buy", offset_seconds=31.0)


def test_a_down_move_suppresses_buying_not_selling() -> None:
    tracker = MomentumTracker()
    tracker.observe("M1", 5000, offset_seconds=0.0)
    tracker.observe("M1", 4900, offset_seconds=30.0)

    assert tracker.suppresses("M1", action="buy", offset_seconds=31.0)
    assert not tracker.suppresses("M1", action="sell", offset_seconds=31.0)


def test_suppression_expires() -> None:
    tracker = MomentumTracker(config=MomentumConfig(cooldown_seconds=60.0))
    tracker.observe("M1", 5000, offset_seconds=0.0)
    tracker.observe("M1", 5100, offset_seconds=30.0)

    assert tracker.suppresses("M1", action="sell", offset_seconds=89.0)
    assert not tracker.suppresses("M1", action="sell", offset_seconds=91.0)


def test_signals_do_not_leak_between_markets() -> None:
    tracker = MomentumTracker()
    tracker.observe("M1", 5000, offset_seconds=0.0)
    tracker.observe("M1", 5100, offset_seconds=30.0)

    assert not tracker.suppresses("M2", action="sell", offset_seconds=31.0)


def test_defence_removes_only_the_exposed_side() -> None:
    class BothSides:
        name = "both"

        def on_orderbook(self, context, market_ticker, orderbook, portfolio):
            return (
                QuoteIntent("b", market_ticker, "buy", "yes", parse_price_fp("0.4900"), ONE),
                QuoteIntent("s", market_ticker, "sell", "yes", parse_price_fp("0.5000"), ONE),
            )

    defended = MomentumDefendedStrategy(inner=BothSides())

    # Drive a rising mid across the lookback.
    defended.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=0.0),
        market_ticker="M1",
        orderbook=book("0.4900", "0.5000"),
        portfolio=Flat(),
    )
    kept = defended.on_orderbook(
        context=StrategyContext(event_count=2, offset_seconds=31.0),
        market_ticker="M1",
        orderbook=book("0.5000", "0.5100"),
        portfolio=Flat(),
    )

    actions = [i.action for i in kept]
    # Both sides stay quoted: withholding one is a directional bet, which is the
    # risk the defence exists to avoid.
    assert sorted(actions) == ["buy", "sell"]
    assert defended.widened_count == 1

    sell = next(i for i in kept if i.action == "sell")
    # The exposed side moved away from the market.
    assert sell.yes_price > parse_price_fp("0.5000")


def test_defence_is_transparent_when_nothing_is_moving() -> None:
    class BothSides:
        name = "both"

        def on_orderbook(self, context, market_ticker, orderbook, portfolio):
            return (
                QuoteIntent("b", market_ticker, "buy", "yes", parse_price_fp("0.4900"), ONE),
                QuoteIntent("s", market_ticker, "sell", "yes", parse_price_fp("0.5000"), ONE),
            )

    defended = MomentumDefendedStrategy(inner=BothSides())

    for offset in (0.0, 30.0, 60.0):
        kept = defended.on_orderbook(
            context=StrategyContext(event_count=1, offset_seconds=offset),
            market_ticker="M1",
            orderbook=book(),
            portfolio=Flat(),
        )

    assert len(kept) == 2
    assert defended.widened_count == 0


def test_taker_refuses_when_the_fee_is_too_large() -> None:
    """The midpoint fee is 1.75c against a measured 2.9c edge - too thin."""

    taker = MomentumTakerStrategy(count=ONE)
    taker.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=0.0),
        market_ticker="M1",
        orderbook=book("0.4900", "0.5000"),
        portfolio=Flat(),
    )
    intents = taker.on_orderbook(
        context=StrategyContext(event_count=2, offset_seconds=30.0),
        market_ticker="M1",
        orderbook=book("0.5000", "0.5100"),
        portfolio=Flat(),
    )

    assert intents == ()


def test_taker_crosses_in_the_tail_where_the_fee_is_small() -> None:
    """Same signal, priced at three cents instead of fifty."""

    taker = MomentumTakerStrategy(count=ONE, min_edge_ticks=50)
    taker.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=0.0),
        market_ticker="M1",
        orderbook=book("0.0200", "0.0300"),
        portfolio=Flat(),
    )
    intents = taker.on_orderbook(
        context=StrategyContext(event_count=2, offset_seconds=30.0),
        market_ticker="M1",
        orderbook=book("0.0400", "0.0500"),
        portfolio=Flat(),
    )

    assert len(intents) == 1
    assert intents[0].action == "buy"
    # Marketable: a buy must be at the ask to cross.
    assert intents[0].yes_price == parse_price_fp("0.0500")


def test_taker_trades_a_signal_once() -> None:
    taker = MomentumTakerStrategy(count=ONE, min_edge_ticks=50)
    taker.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=0.0),
        market_ticker="M1",
        orderbook=book("0.0200", "0.0300"),
        portfolio=Flat(),
    )
    first = taker.on_orderbook(
        context=StrategyContext(event_count=2, offset_seconds=30.0),
        market_ticker="M1",
        orderbook=book("0.0400", "0.0500"),
        portfolio=Flat(),
    )
    second = taker.on_orderbook(
        context=StrategyContext(event_count=3, offset_seconds=30.5),
        market_ticker="M1",
        orderbook=book("0.0400", "0.0500"),
        portfolio=Flat(),
    )

    assert len(first) == 1
    assert second == (), "one idea, one fee"


def test_momentum_config_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        MomentumConfig(trigger_ticks=0)

    with pytest.raises(ValueError):
        MomentumConfig(lookback_seconds=0)


def test_one_move_emits_one_signal() -> None:
    """The trigger must not re-fire while the same move sits in the lookback.

    Left unguarded it emits a signal on every update for the whole window - and
    a taker pays the fee on each one.
    """

    tracker = MomentumTracker()
    tracker.observe("M1", 5000, offset_seconds=0.0)

    assert tracker.observe("M1", 5100, offset_seconds=30.0) is not None
    assert tracker.observe("M1", 5100, offset_seconds=30.5) is None
    assert tracker.observe("M1", 5110, offset_seconds=31.0) is None

    # Once the cooldown lapses a genuinely new move can fire again.
    tracker.observe("M1", 5110, offset_seconds=95.0)
    assert tracker.observe("M1", 5220, offset_seconds=125.0) is not None


# --- window phase gate -------------------------------------------------------


def _phased(position=0):
    from kalshi_mm_bot.strategy.phase import WindowPhaseStrategy

    class BothSides:
        name = "both"

        def on_orderbook(self, context, market_ticker, orderbook, portfolio):
            return (
                QuoteIntent("b", market_ticker, "buy", "yes", parse_price_fp("0.4900"), ONE),
                QuoteIntent("s", market_ticker, "sell", "yes", parse_price_fp("0.5000"), ONE),
            )

    class Held:
        def position(self, market_ticker):
            return position

    return WindowPhaseStrategy(inner=BothSides()), Held()


def _run(strategy, portfolio, seconds_left):
    return strategy.on_orderbook(
        context=StrategyContext(
            event_count=1, offset_seconds=1.0, seconds_to_close=seconds_left
        ),
        market_ticker="M1",
        orderbook=book(),
        portfolio=portfolio,
    )


def test_phase_gate_is_transparent_early_in_the_window() -> None:
    strategy, portfolio = _phased()

    assert len(_run(strategy, portfolio, 700.0)) == 2
    assert strategy.blocked_count == 0


def test_phase_gate_keeps_only_the_flattening_side_when_long() -> None:
    """Reduce-only, not stop: a withheld quote leaves inventory with no exit."""

    strategy, portfolio = _phased(position=ONE)
    kept = _run(strategy, portfolio, 120.0)

    assert [i.action for i in kept] == ["sell"]


def test_phase_gate_keeps_only_the_flattening_side_when_short() -> None:
    strategy, portfolio = _phased(position=-ONE)
    kept = _run(strategy, portfolio, 120.0)

    assert [i.action for i in kept] == ["buy"]


def test_phase_gate_stops_entirely_when_flat() -> None:
    """No position to unwind and no edge left to earn."""

    strategy, portfolio = _phased(position=0)

    assert _run(strategy, portfolio, 120.0) == ()


def test_unknown_time_to_close_does_not_gate() -> None:
    """Otherwise a market whose close time failed to parse is silently muted."""

    strategy, portfolio = _phased(position=0)

    assert len(_run(strategy, portfolio, None)) == 2
