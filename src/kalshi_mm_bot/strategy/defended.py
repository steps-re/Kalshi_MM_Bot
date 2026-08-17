"""Momentum defence, as a wrapper around any market maker.

A resting quote is a free option written to whoever crosses it. Most of the time
that option is cheap - the crosser is trading for reasons unrelated to where the
price goes next, and we collect the spread. It is expensive in exactly one
situation: someone is trading into a move that keeps going, and we are on the
wrong side of it. Measured live, that is the -1c to -7.5c tail of our markout
distribution, against a median fill of +0.5c.

`strategy/momentum.py` measures when that situation is in force. This applies
it, by dropping quotes on the side a live move is running into and leaving the
other side alone.

## Why a wrapper and not a parameter

Three strategies exist and all of them write the same option. Implementing this
inside `adaptive` would leave `horizon` undefended and make the comparison
between them a comparison of two things at once. A wrapper defends any of them,
keeps the defence in one testable place, and - because it only ever *removes*
intents - can be evaluated by running the same strategy with and against it
without touching the strategy at all.

## Widen, do not withhold

The first version dropped the exposed quote entirely, and that was measurably
worse than no defence at all: fills fell 73%, capture fell 75%, per-fill edge
did not improve, and inventory went from +$1.33 to -$14.78 across seven
recordings while runs ending flat collapsed from 6/7 to 1/7.

The mechanism is obvious in hindsight. **Quoting one side only is a directional
bet.** Removing our offer during a rise leaves us buying and nothing else, so we
accumulate exactly the position the signal said was dangerous. The defence
recreated the risk it was built to avoid, in a larger size.

So it widens instead. The exposed side stays quoted and moves away from the
market by `widen_ticks`, which keeps the book two-sided - no accumulation - and
prices the risk rather than refusing it. A quote further out fills less often
and, when it does fill, fills at a better price; that is the trade a market
maker is supposed to make against adverse selection.

## What it deliberately does not do

It does not cancel resting orders. A quote already at the front of a queue has
paid for its position, and pulling it on a signal that is right 74% of the time
means paying that cost again on the 26%. Widening applies to new quotes; the
existing order lives out its own logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import ONE_DOLLAR
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.strategy.momentum import MomentumConfig, MomentumTracker
from kalshi_mm_bot.strategy.types import (
    PortfolioView,
    QuoteIntent,
    Strategy,
    StrategyContext,
)


# One cent. Enough to matter against a book quoted a cent wide, small enough
# that the quote stays in the market rather than being a withheld quote wearing
# a price.
DEFAULT_WIDEN_TICKS = 100


@dataclass
class MomentumDefendedStrategy:
    """Wraps a strategy, widening quotes into a live momentum move."""

    inner: Strategy
    config: MomentumConfig = field(default_factory=MomentumConfig)
    widen_ticks: int = DEFAULT_WIDEN_TICKS
    # Widen both sides rather than only the exposed one. Asymmetric widening
    # still biases which side fills, and any asymmetric response to a
    # directional signal accumulates a directional position - measured, it took
    # inventory from +$1.33 to -$10.38 and cut runs ending flat from 6/7 to 1/7.
    # Symmetric widening treats a momentum trigger as what it also is: evidence
    # the market just became more dangerous to quote at all.
    symmetric: bool = False
    _tracker: MomentumTracker = field(init=False, repr=False)
    # Counted so a backtest can report what the defence actually did, rather
    # than leaving its effect to be inferred from a P&L difference.
    widened_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._tracker = MomentumTracker(config=self.config)

    @property
    def name(self) -> str:
        return f"defended_{getattr(self.inner, 'name', 'strategy')}"

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        best_bid, best_ask = orderbook.best_bid, orderbook.best_ask

        if best_bid is not None and best_ask is not None and best_bid < best_ask:
            self._tracker.observe(
                market_ticker,
                (best_bid + best_ask) // 2,
                offset_seconds=context.offset_seconds,
            )

        intents = self.inner.on_orderbook(
            context=context,
            market_ticker=market_ticker,
            orderbook=orderbook,
            portfolio=portfolio,
        )

        adjusted: list[QuoteIntent] = []

        signal_active = (
            self._tracker.active_signal(
                market_ticker, offset_seconds=context.offset_seconds
            )
            is not None
        )

        for intent in intents:
            exposed = (
                signal_active
                if self.symmetric
                else self._tracker.suppresses(
                    market_ticker,
                    action=str(intent.action),
                    offset_seconds=context.offset_seconds,
                )
            )

            if not exposed:
                adjusted.append(intent)
                continue

            # Away from the market: a buy moves down, a sell moves up. Both
            # make the quote harder to hit and better priced if it is.
            shift = -self.widen_ticks if intent.action == "buy" else self.widen_ticks
            price = intent.yes_price + shift

            if not 0 < price < ONE_DOLLAR:
                # Widening off the end of the price range is a withheld quote by
                # another name, so drop it rather than clamp it back to a price
                # the defence just decided was dangerous.
                self.widened_count += 1
                continue

            self.widened_count += 1
            adjusted.append(replace(intent, yes_price=price))

        return tuple(adjusted)
