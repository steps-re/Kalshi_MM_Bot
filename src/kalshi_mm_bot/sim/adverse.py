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

## It does not work, and that is the finding

Calibrated against the live target of +0.268c early / +0.058c late:

    keep   fills    early     late
    1.00    1561   +1.093c  +0.579c
    0.50    1264   +1.143c  +0.562c
    0.30    1205   +1.049c  +0.537c
    0.15     956   +1.002c  +0.451c
    0.05     602   +1.006c  +0.222c

Discarding **95%** of favourable fills leaves early markout at +1.006c against
a target of +0.268c. It barely moves. Selection thinning cannot get there, so
the hypothesis this model embodies - that the simulator's optimism is a mix
problem, too many good fills relative to bad ones - **is wrong**.

What the numbers imply instead is that the surviving fills are themselves too
good: the simulator is filling us at prices reality would not give us at all.
The queue model puts us at the touch whenever a level shrinks past our
position, and in a real book with 1,690 contracts resting ahead we would simply
not be there. That is a fill-eligibility problem, not a fill-selection one, and
no amount of discarding fixes it.

Kept, unshipped, defaulted off (`favourable_keep_rate=1.0` is the inner model),
because the negative result is worth more than the code: it rules out the
obvious explanation and points at queue position as the real culprit. The next
attempt should make fills conditional on modelled queue position rather than on
what happened afterwards.

## Limits worth stating

The lookahead models the *counterparty's* information and is never shown to a
strategy. It does not claim to model why any individual trader crossed, and it
cannot: the recordings carry no counterparty identity.
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

# Defaults to 1.0, which is the inner model unchanged. Calibration showed
# thinning cannot reach the live markout target at any rate, so shipping a
# non-neutral default would silently apply a correction known not to work.
DEFAULT_FAVOURABLE_KEEP_RATE = 1.0


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
        """Signed edge on this fill, positive when it favoured us.

        Measured at the **same horizon the live comparison uses** - the book
        immediately after the fill - not at a forward lookahead. The first
        version of this model judged fills on a 30-second forward drift while
        the metric it was calibrating against was the immediate markout, and
        the two are only weakly related: dropping half the "favourable" fills
        moved measured markout from +1.093c to +1.131c, which is to say not at
        all. Thinning on one quantity cannot calibrate another.

        Falls back to the forward series only when the fill carries no mid,
        because a fill with no mark cannot be judged either way.
        """

        direction = 1 if fill.action == "buy" else -1

        if fill.mid_at_fill is not None:
            return direction * (fill.mid_at_fill - fill.yes_price)

        series = self.mid_series.get(fill.market_ticker)

        if series is None:
            return None

        later = context.offset_seconds + self.lookahead_seconds

        if not series.covers(later):
            return None

        future_mid = series.mid_at(later)

        if future_mid is None:
            return None

        return direction * (future_mid - fill.yes_price)
