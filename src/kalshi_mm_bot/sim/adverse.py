"""A fill model that knows the counterparty is sometimes better informed.

The queue model fills us whenever the book mechanically permits: a level shrinks
past our position, so we trade. Every fill is equally likely regardless of what
the price does next. That is the flaw, and it is not small.

Measured on aligned recordings against 784 live fills in the same markets:

    simulated markout   +0.663c early   +0.505c late
    live markout        +0.268c early   +0.058c late

The simulator overstates by 2.5x early and 8.7x late, and - worse - shows almost
no decay through a window where live markout decays hard and turns negative
inside two minutes.

It is not a marking problem. Simulated markout measured against the forward mid
series is flat across every horizon tested:

    1s 0.558c | 5s 0.557c | 15s 0.487c | 30s 0.592c | 60s 0.499c | 120s 0.521c

Waiting longer never reveals a loss, because the losses were never in the
sample. The simulator hands us the good fills and not the bad ones.

## What this model adds

Real resting orders are not filled at random. They are filled disproportionately
by someone who wants the trade *now*, and that person is more often right about
the next few seconds than we are. The effect is selection on the counterparty's
information, and it can be represented directly: when the inner model produces a
fill, look at what the price actually did next, and **keep adverse fills while
discarding a fraction of the favourable ones**.

That is deliberately using hindsight, which is legitimate here and would not be
in a strategy. The lookahead models the *counterparty's* information, not ours -
the strategy never sees it, and the fill it produces is one that a better
informed trader would have taken. Using the future to decide our own quotes
would be cheating; using it to decide who trades against us is the point.

`favourable_keep_rate` is the one parameter, and it is calibrated so that
simulated markout matches the live measurement rather than chosen for
plausibility. At 1.0 this model is exactly the inner model.

## Limits worth stating

This reproduces the *level* and *shape* of markout. It does not claim to model
why any individual trader crossed, and it cannot: the recordings carry no
counterparty identity. A strategy that finds a way to exploit the specific
lookahead window would be exploiting an artifact, so the horizon is kept short
and the strategy is never shown the signal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.series import MidSeries
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.sim.fills import FillModel, SimulatedFill
from kalshi_mm_bot.sim.orders import SimulatedOrder
from kalshi_mm_bot.strategy.types import StrategyContext

# Seconds ahead used to judge whether a fill was adverse. Short on purpose: it
# represents what the counterparty knew when they crossed, not a forecast.
DEFAULT_LOOKAHEAD_SECONDS = 30.0

# Share of favourable fills kept. Calibrated against live markout - see
# scripts/calibrate_adverse.py - rather than picked. 1.0 disables the model.
DEFAULT_FAVOURABLE_KEEP_RATE = 0.35


@dataclass
class AdverseSelectionFillModel:
    """Wraps a fill model, keeping adverse fills and thinning favourable ones."""

    inner: FillModel
    mid_series: Mapping[MarketTicker, MidSeries]
    lookahead_seconds: float = DEFAULT_LOOKAHEAD_SECONDS
    favourable_keep_rate: float = DEFAULT_FAVOURABLE_KEEP_RATE
    name: str = "adverse"
    # Deterministic thinning: a counter rather than a random draw, so two runs
    # of the same recording produce identical results. A backtest that changes
    # answer between runs cannot be used to compare parameters.
    _favourable_seen: int = field(default=0, init=False, repr=False)
    kept_favourable: int = field(default=0, init=False)
    dropped_favourable: int = field(default=0, init=False)
    kept_adverse: int = field(default=0, init=False)

    def on_order_opened(self, order: SimulatedOrder, book: Orderbook) -> None:
        self.inner.on_order_opened(order, book)

    def on_order_closed(self, order: SimulatedOrder) -> None:
        self.inner.on_order_closed(order)

    def process_event(
        self,
        raw_msg: dict,
        orderbooks: Mapping[str, Orderbook],
        orders: Iterable[SimulatedOrder],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        fills = self.inner.process_event(raw_msg, orderbooks, orders, context)

        if not fills:
            return fills

        return tuple(f for f in fills if self._survives(f, context))

    def _survives(self, fill: SimulatedFill, context: StrategyContext) -> bool:
        drift = self._forward_drift(fill, context)

        if drift is None:
            # No forward data - the end of a recording. Keeping the fill is the
            # conservative choice here only in the sense of not inventing an
            # outcome; it is counted as favourable so the ratio stays honest.
            self.kept_favourable += 1
            return True

        # Adverse means the market moved against the side we took.
        if drift < 0:
            self.kept_adverse += 1
            return True

        self._favourable_seen += 1
        # Deterministic thinning: keep every 1/rate-th favourable fill.
        keep = (
            self.favourable_keep_rate >= 1.0
            or (self._favourable_seen * self.favourable_keep_rate) % 1.0
            < self.favourable_keep_rate
        )

        if keep:
            self.kept_favourable += 1
        else:
            self.dropped_favourable += 1

        return keep

    def _forward_drift(
        self, fill: SimulatedFill, context: StrategyContext
    ) -> float | None:
        """Signed price move after the fill, positive when it favoured us."""

        series = self.mid_series.get(fill.market_ticker)

        if series is None:
            return None

        later = context.offset_seconds + self.lookahead_seconds

        if not series.covers(later):
            return None

        future_mid = series.mid_at(later)

        if future_mid is None:
            return None

        direction = 1 if fill.action == "buy" else -1
        return direction * (future_mid - fill.yes_price)
