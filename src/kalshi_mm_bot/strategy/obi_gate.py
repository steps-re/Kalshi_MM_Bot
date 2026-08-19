"""Pull the endangered quote when the book says it is about to be picked off.

The one validated signal in this project is top-of-book imbalance: at
|OBI| > 0.9 the mid moves ~0.86c in the imbalance's direction over the next 30s
(control band +0.01c, monotone across 640 markets, thick side carries +0.78c of
it). Every attempt to BUY that move failed - the round trip costs more than the
forecast is worth, measured to death in `exit_fill_study.py`.

This wrapper is the remaining use: don't pay for the move, stop being on the
wrong side of it. A maker's only loss is adverse selection - the resting quote
that fills seconds before the price moves through it. Those fills are exactly
what the signal predicts. Gating them converts the forecast into avoided losses,
which pay no spread and no fee.

## What is blocked, precisely

When OBI > +threshold (price about to rise), a fill on our resting SELL that
*creates or extends a short* is the adverse case. If we are long, that same fill
merely banks the spread a moment early - forgone upside, not a loss - and
blocking it would strand inventory, which is how the momentum defence turned
+$1.33 into -$14.78. So:

    OBI > +t: block SELL intents only while position <= 0
    OBI < -t: block BUY  intents only while position >= 0

Risk-reducing quotes always pass. The gate holds for seconds (an OBI episode),
not minutes, and composes under `phased:` which owns the end-of-life behaviour.

`threshold_hundredths <= 0` disables the gate entirely - the control arm. Run
`obigate:phased:adaptive` with `--ab-arms 'obi_gate=0;obi_gate=90'` and the two
arms differ by nothing but this file's if-statement.
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


@dataclass
class OBIGatedStrategy:
    """Wraps a strategy, dropping risk-adding quotes on the endangered side."""

    inner: Strategy
    # Hundredths, so it survives the int-only adaptive-param pipeline: 90 means
    # gate at |OBI| >= 0.90. Zero or negative disables the gate (control arm).
    threshold_hundredths: int = 90
    blocked_sells: int = field(default=0, init=False)
    blocked_buys: int = field(default=0, init=False)

    @property
    def name(self) -> str:
        return f"obigate{self.threshold_hundredths}_{getattr(self.inner, 'name', 'strategy')}"

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

        if self.threshold_hundredths <= 0 or not intents:
            return intents

        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_bid is None or best_ask is None:
            return intents

        bid_size = orderbook.bids[best_bid]
        ask_size = orderbook.asks[best_ask]
        total = bid_size + ask_size

        if total <= 0:
            return intents

        obi = (bid_size - ask_size) / total
        threshold = self.threshold_hundredths / 100.0

        if obi >= threshold:
            endangered = "sell"
        elif obi <= -threshold:
            endangered = "buy"
        else:
            return intents

        position = portfolio.position(market_ticker)
        kept: list[QuoteIntent] = []

        for intent in intents:
            if intent.action != endangered:
                kept.append(intent)
                continue

            # A fill that reduces the position is always allowed: stranding
            # inventory is the measured worse failure. Only fills that would
            # open or extend a position against the predicted move are blocked.
            increases = (
                position <= 0 if endangered == "sell" else position >= 0
            )

            if not increases:
                kept.append(intent)
                continue

            if endangered == "sell":
                self.blocked_sells += 1
            else:
                self.blocked_buys += 1

        return tuple(kept)
