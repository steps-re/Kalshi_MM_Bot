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


# --------------------------------------------------------- audit corrections


def test_counter_counts_the_episode_not_every_book_event():
    """A suppressed quote riding one OBI episode is ONE block, not forty.

    The counters used to increment per dropped intent per orderbook event, so a
    3-second episode across 40 book updates reported 40 blocks. Anything reading
    these as "fills avoided" was inflated by the event rate.
    """

    strategy = OBIGatedStrategy(inner=BothSides(), threshold_hundredths=90)
    imbalanced = book("100.00", "1.00")

    for _ in range(40):
        strategy.on_orderbook(
            context=StrategyContext(event_count=1, offset_seconds=1.0),
            market_ticker="M1",
            orderbook=imbalanced,
            portfolio=Held(0),
        )

    assert strategy.blocked_sells == 1


def test_a_new_episode_counts_again():
    """Two separate episodes are two blocks. The counter must not latch."""

    strategy = OBIGatedStrategy(inner=BothSides(), threshold_hundredths=90)
    imbalanced = book("100.00", "1.00")
    balanced = book("10.00", "10.00")

    for orderbook in (imbalanced, balanced, imbalanced):
        strategy.on_orderbook(
            context=StrategyContext(event_count=1, offset_seconds=1.0),
            market_ticker="M1",
            orderbook=orderbook,
            portfolio=Held(0),
        )

    assert strategy.blocked_sells == 2


def test_flipping_to_the_other_side_is_a_new_episode():
    strategy = OBIGatedStrategy(inner=BothSides(), threshold_hundredths=90)

    for orderbook in (book("100.00", "1.00"), book("1.00", "100.00")):
        strategy.on_orderbook(
            context=StrategyContext(event_count=1, offset_seconds=1.0),
            market_ticker="M1",
            orderbook=orderbook,
            portfolio=Held(0),
        )

    assert (strategy.blocked_sells, strategy.blocked_buys) == (1, 1)


def test_depth_floor_refuses_to_believe_a_one_lot_touch():
    """taker_extract defect #5: a 1-lot ask against a 19-lot bid scores 0.90
    mechanically. With a floor the ratio is not believed at all."""

    thin = book("19.00", "1.00")
    strategy = OBIGatedStrategy(
        inner=BothSides(), threshold_hundredths=90, min_touch_contracts=50)
    intents = strategy.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=1.0),
        market_ticker="M1",
        orderbook=thin,
        portfolio=Held(0),
    )

    assert sorted(i.action for i in intents) == ["buy", "sell"]
    assert strategy.blocked_sells == 0

    # The same book with the floor off is the live arm's behaviour, unchanged.
    unfloored = OBIGatedStrategy(inner=BothSides(), threshold_hundredths=90)
    kept = unfloored.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=1.0),
        market_ticker="M1",
        orderbook=thin,
        portfolio=Held(0),
    )
    assert [i.action for i in kept] == ["buy"]


def test_gate_buys_zero_runs_only_the_supported_half():
    """The audit measured the signal as one-sided. gate_buys=0 blocks SELLs on a
    bid-heavy book and leaves the unsupported half alone."""

    sell_only = OBIGatedStrategy(
        inner=BothSides(), threshold_hundredths=90, gate_buys=0)
    ask_heavy = sell_only.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=1.0),
        market_ticker="M1",
        orderbook=book("1.00", "100.00"),
        portfolio=Held(0),
    )
    assert sorted(i.action for i in ask_heavy) == ["buy", "sell"]

    bid_heavy = sell_only.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=1.0),
        market_ticker="M1",
        orderbook=book("100.00", "1.00"),
        portfolio=Held(0),
    )
    assert [i.action for i in bid_heavy] == ["buy"]


def test_a_no_side_quote_is_never_gated():
    """A `sell` of NO is economically a BUY of YES, so a bid-heavy book does not
    endanger it. Signing off `action` alone would block the wrong quote."""

    class NoSideSell:
        name = "no-side"

        def on_orderbook(self, context, market_ticker, orderbook, portfolio):
            return (
                QuoteIntent("s", market_ticker, "sell", "no",
                            parse_price_fp("0.5000"), ONE),
            )

    strategy = OBIGatedStrategy(inner=NoSideSell(), threshold_hundredths=90)
    intents = strategy.on_orderbook(
        context=StrategyContext(event_count=1, offset_seconds=1.0),
        market_ticker="M1",
        orderbook=book("100.00", "1.00"),
        portfolio=Held(0),
    )

    assert len(intents) == 1
    assert strategy.blocked_sells == 0


def test_factory_defaults_leave_the_running_ab_arm_untouched():
    """The A/B has been live since 2026-08-19. An arm string that does not
    mention the new params must build exactly what it built yesterday."""

    from kalshi_mm_bot.strategy.factory import strategy_from_name

    gate = strategy_from_name(
        "obigate:dumb", count=ONE, max_position=ONE,
        adaptive_params={"obi_gate": 90})

    assert gate.threshold_hundredths == 90
    assert gate.min_touch_contracts == 0
    assert gate.gate_buys == 1


def test_factory_passes_the_new_params_through_and_hides_them_from_the_inner():
    from kalshi_mm_bot.strategy.factory import strategy_from_name

    gate = strategy_from_name(
        "obigate:dumb", count=ONE, max_position=ONE, adaptive_params={
            "obi_gate": 95, "obi_gate_floor": 40, "obi_gate_buys": 0})

    assert (gate.threshold_hundredths, gate.min_touch_contracts,
            gate.gate_buys) == (95, 40, 0)
