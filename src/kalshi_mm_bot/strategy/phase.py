"""Stop opening new risk once a window's edge has decayed.

Measured live across 539 fills, markout by time remaining in a 15-minute window:

    12-15m   +0.414c   75% favourable
     9-12m   +0.416c   75%
      6-8m   +0.232c   56%
      4-6m   +0.078c   63%
      2-4m   +0.002c   49%
      1-2m   -0.068c   36%
       <1m   -0.050c    0%

The edge is concentrated in the first half of a window and is gone by about six
minutes out. That is what you would expect mechanically: as expiry approaches
the traders still active are increasingly the ones who know where it settles,
and depth collapses about elevenfold, so a resting quote sits in a thinner and
better-informed book.

## Reduce-only, not stop

The obvious response - stop quoting late - is wrong, and the momentum defence
already taught this lesson the expensive way. Withholding quotes leaves whatever
inventory we are holding with no way out except crossing the spread, which costs
the taker fee, or carrying a binary to settlement. A gate that reduces fills
without reducing risk makes things worse; that is exactly how the defence turned
+$1.33 of inventory into -$14.78.

So this goes **reduce-only** instead. Past the threshold we keep quoting the
side that flattens us and drop the side that would add. A flat book keeps
quoting nothing, which is correct: there is no position to work off and no edge
left to earn.

## Why a wrapper

Same reason as the momentum defence. `horizon` already has this machinery and
`adaptive` - the strategy that actually earns - does not. A wrapper gives it to
whichever strategy is in front, keeps the rule in one testable place, and lets
the same strategy be run with and against it for comparison.

Note that horizon's own thresholds are far too late: it goes reduce-only at 120
seconds and stops at 30, while the measurement says the edge is gone by 360.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.strategy.types import (
    PortfolioView,
    QuoteIntent,
    Strategy,
    StrategyContext,
)

# Six minutes. The 6-8m bucket still earns +0.23c and 4-6m is +0.08c, so this
# sits at the point where the edge is small rather than where it turns negative
# - opening risk for two hundredths of a cent is not worth the inventory it
# leaves behind.
DEFAULT_REDUCE_ONLY_SECONDS = 360.0


@dataclass
class WindowPhaseStrategy:
    """Wraps a strategy, going reduce-only late in a market's life."""

    inner: Strategy
    reduce_only_seconds: float = DEFAULT_REDUCE_ONLY_SECONDS
    # Counted so a backtest reports what the gate did rather than leaving its
    # effect to be inferred from a P&L difference.
    blocked_count: int = field(default=0, init=False)

    @property
    def name(self) -> str:
        return f"phased_{getattr(self.inner, 'name', 'strategy')}"

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        intents = self.inner.on_orderbook(
            context=context,
            market_ticker=market_ticker,
            orderbook=orderbook,
            portfolio=portfolio,
        )
        seconds_left = context.seconds_to_close

        # Unknown time to close means we cannot tell which phase we are in.
        # Quoting normally is the right default: the alternative is silently
        # gating every market whose close time we failed to read.
        if seconds_left is None or seconds_left > self.reduce_only_seconds:
            return intents

        position = portfolio.position(market_ticker)

        if position == 0:
            # Nothing to unwind and no edge left to earn.
            self.blocked_count += len(intents)
            return ()

        # Keep only the side that moves us toward flat.
        wanted = "sell" if position > 0 else "buy"
        kept = tuple(i for i in intents if i.action == wanted)
        self.blocked_count += len(intents) - len(kept)
        return kept
