"""The OBI gate blocks exactly the adverse fill and nothing else.

The failure modes this guards against are both measured history:
- blocking too much strands inventory (the momentum defence, -$14.78), so a
  risk-REDUCING quote must always pass, even mid-episode;
- blocking nothing at all is the control arm, and the A/B is only valid if
  obi_gate=0 makes the wrapper provably transparent.
"""

from __future__ import annotations

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.types import PriceRange
from kalshi_mm_bot.market.price import parse_count_fp, parse_price_fp
from kalshi_mm_bot.strategy import strategy_from_name
from kalshi_mm_bot.strategy.obi_gate import OBIGatedStrategy
from kalshi_mm_bot.strategy.types import QuoteIntent, StrategyContext

ONE = parse_count_fp("1.00")
PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


def book(bid_size: str, ask_size: str) -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=(("0.4900", bid_size),),
        asks_raw=(("0.5000", ask_size),),
        price_ranges=PRICE_RANGES,
    )


class BothSides:
    name = "both"

    def on_orderbook(self, context, market_ticker, orderbook, portfolio):
        return (
            QuoteIntent("b", market_ticker, "buy", "yes", parse_price_fp("0.4900"), ONE),
            QuoteIntent("s", market_ticker, "sell", "yes", parse_price_fp("0.5000"), ONE),
        )


class Held:
    def __init__(self, position: int) -> None:
        self._position = position

    def position(self, market_ticker):
        return self._position


def run(threshold: int, position: int, orderbook: Orderbook):
    strategy = OBIGatedStrategy(inner=BothSides(), threshold_hundredths=threshold)
    intents = strategy.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=1.0),
        market_ticker="M1",
        orderbook=orderbook,
        portfolio=Held(position),
    )
    return strategy, [i.action for i in intents]


BID_HEAVY = book(bid_size="1900.00", ask_size="100.00")   # OBI +0.90: rise coming
ASK_HEAVY = book(bid_size="100.00", ask_size="1900.00")   # OBI -0.90: fall coming
BALANCED = book(bid_size="500.00", ask_size="500.00")


def test_balanced_book_passes_everything() -> None:
    _, actions = run(90, 0, BALANCED)

    assert actions == ["buy", "sell"]


def test_bid_heavy_blocks_the_sell_that_would_open_a_short() -> None:
    """Flat + price about to rise: a sell fill creates the losing short."""

    strategy, actions = run(90, 0, BID_HEAVY)

    assert actions == ["buy"]
    assert strategy.blocked_sells == 1


def test_bid_heavy_keeps_the_sell_that_reduces_a_long() -> None:
    """Long + price rising: selling merely banks the spread early. Blocking it
    is the stranded-inventory bug, so it must pass."""

    _, actions = run(90, ONE, BID_HEAVY)

    assert actions == ["buy", "sell"]


def test_ask_heavy_blocks_the_buy_that_would_open_a_long() -> None:
    strategy, actions = run(90, 0, ASK_HEAVY)

    assert actions == ["sell"]
    assert strategy.blocked_buys == 1


def test_ask_heavy_keeps_the_buy_that_reduces_a_short() -> None:
    _, actions = run(90, -ONE, ASK_HEAVY)

    assert actions == ["buy", "sell"]


def test_threshold_zero_is_the_control_arm() -> None:
    """obi_gate=0 must make the wrapper invisible, or the A/B compares nothing."""

    strategy, actions = run(0, 0, BID_HEAVY)

    assert actions == ["buy", "sell"]
    assert strategy.blocked_sells == 0


def test_below_threshold_imbalance_does_not_gate() -> None:
    mild = book(bid_size="800.00", ask_size="200.00")   # OBI +0.60

    _, actions = run(90, 0, mild)

    assert actions == ["buy", "sell"]


def test_factory_pops_the_gate_param_before_the_inner_strategy() -> None:
    """obi_gate must never reach the adaptive constructor, which does not own
    it and would refuse to start - discovered the expensive way with obi_skew."""

    strategy = strategy_from_name(
        "obigate:phased:adaptive",
        count=ONE,
        max_position=5 * ONE,
        adaptive_params={"obi_gate": 90, "obi_skew": 42},
    )

    assert strategy.threshold_hundredths == 90
    assert strategy.inner.inner.obi_skew == 42


def test_factory_threshold_zero_builds_a_transparent_wrapper() -> None:
    strategy = strategy_from_name(
        "obigate:adaptive",
        count=ONE,
        max_position=5 * ONE,
        adaptive_params={"obi_gate": 0},
    )

    assert strategy.threshold_hundredths == 0
