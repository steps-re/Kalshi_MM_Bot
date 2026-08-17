"""Stop opening new risk once a window's edge has decayed.

Measured live across **784 fills**, markout by time remaining in a 15-minute
window:

    12m+     +0.354c   68% favourable   n=41
     9-12m   +0.316c   66%              n=125
      6-9m   +0.222c   63%              n=206
      4-6m   +0.085c   53%              n=173
      2-4m   +0.094c   55%              n=150
      1-2m   -0.112c   38%              n=66
       <1m   +0.104c   48%              n=23

Edge decays through the window - roughly +0.27c with six minutes or more left
against +0.06c inside that - which is what you would expect mechanically: as
expiry approaches the traders still active increasingly know where it settles,
and depth collapses about elevenfold, so a resting quote sits in a thinner and
better-informed book.

**But late is still positive**, and this threshold was set at 360 seconds on a
sample a fifth this size that showed the last six minutes going negative. It did
not. Only the 1-2 minute band is genuinely bad, so the default is 120 seconds:
gating at 360 would refuse trades that make ~0.09c, which is small but not a
loss, and every fill refused is also a fill unavailable for working inventory
off. The larger sample moved this the same way it moved overall markout, from
+0.446c to +0.264c - small samples in this project have consistently flattered
whatever they were measuring.

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

Note that horizon's own reduce-only threshold is 120 seconds, which the 784-fill
measurement says is right. An earlier draft of this file called it "far too
late" on the strength of a 539-fill sample; that was wrong, and horizon had it
correct all along.
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

# Two minutes: the only band measured to be negative (-0.112c, 38% favourable
# over 66 fills). Everything above it still earns, thinly.
DEFAULT_REDUCE_ONLY_SECONDS = 120.0


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
