"""Short-horizon momentum in a resolving window, and what to do about it.

Measured on the websocket-recorded 15-minute crypto windows: conditional on the
mid moving at least a cent over thirty seconds, the next thirty seconds

* continue in the same direction **74%** of the time, and
* travel a further **+2.9c on average** (median +1.85c).

That single fact points in two directions at once, which is why the signal lives
here rather than inside either strategy.

**Defensively**, it explains our worst fills. A resting quote is a free option
written to whoever crosses it, and the fills that hurt - the -1c to -7.5c tail
of the live markout distribution - are the ones taken by someone trading into a
move that keeps going. Standing aside for a minute after a trigger removes the
fills most likely to be adversely selected, which is worth more than it costs
in missed spread precisely because the distribution is skewed.

**Offensively**, +2.9c of expected continuation is larger than the 1.75c taker
fee at the midpoint, and far larger than the 0.04-0.6c fee in the tails where
these windows spend their final minutes. A taker acting on the same trigger is
not competing for queue position at all, which matters: queue capacity caps the
market maker at two markets, and a taker has no such limit.

## What this is not

It is not a claim that prediction-market prices trend. It is a measurement of a
specific structure: a binary that resolves in minutes, whose underlying is a
continuously traded asset, whose book is thin enough that information arrives as
a visible price move rather than being already priced. Nothing here should be
carried to a market that does not have all three properties.

## Sample

n=42 triggers across 13 window paths in one afternoon. The direction is
credible and the magnitude is soft, so `MomentumConfig` defaults are set to be
useful if the effect is real and cheap if it is not - a one-minute stand-aside
costs a fraction of a session's fills, and the taker threshold demands more edge
than the measurement suggests is available.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from kalshi_mm_bot.market.types import MarketTicker

# One cent over thirty seconds: the trigger the measurement was conditioned on.
DEFAULT_TRIGGER_TICKS = 100
DEFAULT_LOOKBACK_SECONDS = 30.0
# Continuation was measured over the following thirty seconds. Standing aside
# for twice that is deliberate: the cost of being out is linear in time while
# the cost of a bad fill is not.
DEFAULT_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class MomentumConfig:
    trigger_ticks: int = DEFAULT_TRIGGER_TICKS
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        if self.trigger_ticks <= 0:
            raise ValueError("trigger_ticks must be positive")
        if self.lookback_seconds <= 0:
            raise ValueError("lookback_seconds must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class MomentumSignal:
    """A detected move, and how long its shadow lasts."""

    market_ticker: MarketTicker
    direction: int
    move_ticks: int
    at_offset: float
    expires_at: float

    def active_at(self, offset_seconds: float) -> bool:
        return offset_seconds < self.expires_at

    @property
    def is_up(self) -> bool:
        return self.direction > 0


@dataclass
class MomentumTracker:
    """Detects trigger-sized moves per market from a stream of mids.

    Keeps only the mids inside the lookback window, so memory is bounded by
    update rate rather than by session length. A market that stops updating
    simply stops producing signals rather than firing on a stale comparison.
    """

    config: MomentumConfig = field(default_factory=MomentumConfig)
    _history: dict[MarketTicker, deque[tuple[float, int]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _latest: dict[MarketTicker, MomentumSignal] = field(
        default_factory=dict, init=False, repr=False
    )

    def observe(
        self, market_ticker: MarketTicker, mid: int, *, offset_seconds: float
    ) -> MomentumSignal | None:
        """Record a mid and return a signal when the move clears the trigger."""

        history = self._history.setdefault(market_ticker, deque())
        history.append((offset_seconds, mid))
        cutoff = offset_seconds - self.config.lookback_seconds

        # Keep one sample older than the cutoff: it is the reference the move is
        # measured from. Dropping it would shorten the effective lookback to
        # whatever happens to remain, and silently weaken the trigger.
        while len(history) > 1 and history[1][0] < cutoff:
            history.popleft()

        if len(history) < 2:
            return None

        # One move, one signal. Without this the trigger re-fires on every
        # update for as long as the move stays inside the lookback window - a
        # single cent step emitting dozens of signals, which a taker would pay
        # the fee on each time. A move already in force does not need
        # re-detecting; callers ask `active_signal` for that.
        if self.active_signal(market_ticker, offset_seconds=offset_seconds):
            return None

        reference_offset, reference_mid = history[0]

        if offset_seconds - reference_offset < self.config.lookback_seconds:
            return None

        move = mid - reference_mid

        if abs(move) < self.config.trigger_ticks:
            return None

        signal = MomentumSignal(
            market_ticker=market_ticker,
            direction=1 if move > 0 else -1,
            move_ticks=abs(move),
            at_offset=offset_seconds,
            expires_at=offset_seconds + self.config.cooldown_seconds,
        )
        self._latest[market_ticker] = signal
        return signal

    def active_signal(
        self, market_ticker: MarketTicker, *, offset_seconds: float
    ) -> MomentumSignal | None:
        """The signal still in force for this market, if any."""

        signal = self._latest.get(market_ticker)

        if signal is None or not signal.active_at(offset_seconds):
            return None

        return signal

    def suppresses(
        self, market_ticker: MarketTicker, *, action: str, offset_seconds: float
    ) -> bool:
        """Should we stand aside from quoting this side right now?

        Only the side the move runs *into* is suppressed, and getting this the
        right way round matters more than the feature does.

        After a move up that continues, the quote that fills is our **offer** -
        a buyer lifts it, and we are left short while the price keeps rising.
        Our bid is not the exposure: the market is walking away from it, and if
        it does fill the price came back to us, which is the good case. So an
        up-move suppresses selling, and a down-move suppresses buying.

        Suppressing both sides would give up half the fills to avoid half the
        risk, which is a worse trade than it sounds given the markout
        distribution is skewed rather than symmetric.
        """

        signal = self.active_signal(market_ticker, offset_seconds=offset_seconds)

        if signal is None:
            return False

        # Buy is dangerous while the market is falling; sell while it is rising.
        return not signal.is_up if action == "buy" else signal.is_up
