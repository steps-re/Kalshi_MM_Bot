"""Cross the spread on measured continuation, when the fee is small enough.

Everything else in this project rests and waits. That caps it at two markets,
because resting is queue-constrained and the exchange has exactly two books
whose queues we can get through. A taker has no queue, so if it works at all it
works in every market at once - which makes it the only idea here that answers
"how do we grow beyond two markets" rather than "how do we earn more in them".

The trade is simple and its arithmetic is unforgiving:

    expected edge  =  continuation after a trigger   (+2.9c mean, measured)
    cost           =  half the spread + the taker fee at this price

The fee is the whole game, and it is why this is a *tail* strategy. Kalshi
charges 0.07 x N x P x (1-P), which is 1.75c at the midpoint and 0.04c at three
cents. A 15-minute window spends its final minutes in exactly that tail, so the
same signal that is marginal at the midpoint is cheap where these markets end
up. `max_fee_ticks` is the control, and it is set below the measured edge
rather than at it.

## Why this is not the market maker with different parameters

The market maker earns the spread and pays adverse selection. This pays the
spread and earns direction. They lose money in opposite conditions, which is
the argument for running both: the momentum that hurts a resting quote is the
momentum this trades. Defence and offence on one measurement.

## Honesty about the sample

n=42 triggers across 13 windows. `min_edge_ticks` demands more than the median
measured continuation, so a real-but-weaker effect produces no trades rather
than losing trades. The failure mode of this design is doing nothing, which is
the correct failure mode for a strategy that crosses spreads on a small sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_mm_bot.market.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.strategy.momentum import MomentumConfig, MomentumTracker
from kalshi_mm_bot.strategy.types import (
    PortfolioView,
    QuoteIntent,
    StrategyContext,
)

# Measured continuation is +2.9c mean, +1.85c median. Demanding 150 ticks of
# net edge is above the median on purpose: a weaker-than-measured effect should
# produce silence, not losses.
DEFAULT_MIN_EDGE_TICKS = 150
# Above this, the fee eats the trade. 60 ticks is 0.6c, which the fee curve
# reaches around 9c or 91c - the tail region these windows resolve into.
DEFAULT_MAX_FEE_TICKS = 60


@dataclass
class MomentumTakerStrategy:
    """Crosses the spread in the direction of a measured continuation.

    Emits marketable quotes: a buy at the current best ask, a sell at the
    current best bid. The simulator prices those as taker fills, which is the
    honest treatment - this strategy is paying for immediacy and must be
    charged for it.
    """

    count: int = COUNT_SCALE
    max_position: int = 5 * COUNT_SCALE
    min_edge_ticks: int = DEFAULT_MIN_EDGE_TICKS
    max_fee_ticks: int = DEFAULT_MAX_FEE_TICKS
    config: MomentumConfig = field(default_factory=MomentumConfig)
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL
    name: str = "momentum_taker"
    _tracker: MomentumTracker = field(init=False, repr=False)
    # One entry per signal: crossing repeatedly on a single move would pay the
    # fee several times for one idea.
    _traded_signals: set[tuple[MarketTicker, float]] = field(
        default_factory=set, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._tracker = MomentumTracker(config=self.config)

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        best_bid, best_ask = orderbook.best_bid, orderbook.best_ask

        if best_bid is None or best_ask is None or best_bid >= best_ask:
            return ()

        mid = (best_bid + best_ask) // 2
        signal = self._tracker.observe(
            market_ticker, mid, offset_seconds=context.offset_seconds
        )

        if signal is None:
            return ()

        key = (market_ticker, signal.at_offset)

        if key in self._traded_signals:
            return ()

        # Crossing costs half the spread plus the fee. Both are paid now; the
        # edge is expected. Requiring the expectation to beat the certainty is
        # the only discipline that makes a taker viable.
        half_spread = (best_ask - best_bid) // 2
        fee_ticks = self.fee_model.edge_ticks_per_contract(mid, is_taker=True)

        if fee_ticks > self.max_fee_ticks:
            return ()

        if signal.move_ticks - half_spread - fee_ticks < self.min_edge_ticks:
            return ()

        position = portfolio.position(market_ticker)
        action = "buy" if signal.is_up else "sell"
        capacity = (
            self.max_position - position if action == "buy" else self.max_position + position
        )

        if capacity < self.count:
            return ()

        self._traded_signals.add(key)
        # Marketable by construction: buy at the ask, sell at the bid.
        price = best_ask if action == "buy" else best_bid

        return (
            QuoteIntent(
                quote_id=f"{market_ticker}:momo:{action}",
                market_ticker=market_ticker,
                action=action,
                side="yes",
                yes_price=price,
                count=self.count,
            ),
        )
