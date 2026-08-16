"""Rolling estimates of what a market is doing, so quotes can react to it.

Nate's observation was that the last few minutes of a 15-minute BTC market
behave nothing like the middle. That is true, but the clock is not the cause
and "minutes remaining" does not port to a market that settles next Tuesday.
What actually changes is the size of the move a resting quote is exposed to
before it can be pulled, and how much of the position's value is still
undecided. Both are measurable from the book alone:

* **Adverse selection** scales with `sigma * sqrt(quote_lifetime)` where sigma
  is the *instantaneous* volatility of the mid. In a 15-minute crypto market
  sigma spikes near the close because each tick of the underlying moves the
  probability further as the payoff steepens. Quote lifetime is a property of
  our own requote policy, not the market.
* **Inventory risk** scales with `sigma * sqrt(time_to_flatten)` in the normal
  case, but degrades toward `sqrt(P * (1 - P))` - the variance of the binary
  payoff - as the close approaches and getting out stops being a choice.

Expressed that way the same controls work on a 15-minute BTC strike and on a
month-long election market, because both terms are estimated from observed
price behaviour rather than assumed from the product. `seconds_to_close` is
used only where it genuinely belongs: deciding how likely we are to be stuck
with what we are holding.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import sqrt

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import ONE_DOLLAR
from kalshi_mm_bot.market.types import MarketTicker

BPS_SCALE = 10_000

DEFAULT_VOL_WINDOW_SECONDS = 45.0
DEFAULT_MIN_VOL_SAMPLES = 8


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything a quote decision needs, derived from the book.

    Prices and sizes are in the project's fixed point. `sigma_ticks_per_sqrt_sec`
    is a float because it is a rate, not a price.
    """

    market_ticker: MarketTicker
    best_bid: int
    best_ask: int
    mid: int
    microprice: int
    spread: int
    bid_size: int
    ask_size: int
    imbalance_bps: int
    sigma_ticks_per_sqrt_sec: float
    update_rate_hz: float
    sample_count: int
    price_levels: tuple[int, ...] = ()
    seconds_to_close: float | None = None

    min_samples_required: int = DEFAULT_MIN_VOL_SAMPLES

    @property
    def has_volatility_estimate(self) -> bool:
        """False when too few recent observations to trust sigma.

        A caller must widen, not tighten, when this is False. Zero samples
        produce a sigma of zero, and treating that as "calm" is exactly wrong
        after a feed gap or at the start of a session.
        """

        return self.sample_count >= self.min_samples_required

    def expected_move_ticks(self, horizon_seconds: float) -> float:
        """One standard deviation of mid movement over `horizon_seconds`."""

        if horizon_seconds <= 0:
            return 0.0

        return self.sigma_ticks_per_sqrt_sec * sqrt(horizon_seconds)

    def terminal_sigma_ticks(self) -> float:
        """Std dev of the binary payoff if we are forced to hold to resolution.

        A contract marked at P settles at 0 or 1, so its payoff has standard
        deviation sqrt(P * (1 - P)) - maximal at $0.50 and vanishing at the
        extremes. This is the risk that diffusion-style estimates miss.
        """

        p = self.mid / ONE_DOLLAR
        return sqrt(max(0.0, p * (1.0 - p))) * ONE_DOLLAR

    def inventory_sigma_ticks(self, *, flatten_seconds: float) -> float:
        """Blended per-contract inventory risk.

        Interpolates between "we can flatten in `flatten_seconds`" and "we are
        stuck to resolution" using how much time is left relative to the time
        flattening takes. With no close time known we assume flattening works.
        """

        diffusion = self.expected_move_ticks(flatten_seconds)

        if self.seconds_to_close is None:
            return diffusion

        stuck = _forced_hold_probability(self.seconds_to_close, flatten_seconds)

        return (1.0 - stuck) * diffusion + stuck * self.terminal_sigma_ticks()


@dataclass(slots=True)
class MarketDynamicsTracker:
    """Maintains a `MarketSnapshot` per ticker from a stream of book updates."""

    vol_window_seconds: float = DEFAULT_VOL_WINDOW_SECONDS
    min_samples: int = DEFAULT_MIN_VOL_SAMPLES
    _history: dict[MarketTicker, deque[tuple[float, int]]] = field(
        default_factory=dict,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.vol_window_seconds <= 0:
            raise ValueError("vol_window_seconds must be greater than zero")
        if self.min_samples < 2:
            raise ValueError("min_samples must be at least two")

    def observe(
        self,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        *,
        offset_seconds: float,
        seconds_to_close: float | None = None,
    ) -> MarketSnapshot | None:
        """Record a book update and return the current snapshot.

        Returns None when the book is one-sided or crossed, which is also when
        no sane quote can be formed.
        """

        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_bid is None or best_ask is None or best_bid >= best_ask:
            return None

        mid = (best_bid + best_ask) // 2
        history = self._history.setdefault(market_ticker, deque())
        history.append((offset_seconds, mid))
        self._trim(history, offset_seconds)

        bid_size = orderbook.bids[best_bid]
        ask_size = orderbook.asks[best_ask]

        return MarketSnapshot(
            market_ticker=market_ticker,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            microprice=_microprice(best_bid, best_ask, bid_size, ask_size),
            spread=best_ask - best_bid,
            bid_size=bid_size,
            ask_size=ask_size,
            imbalance_bps=_imbalance_bps(bid_size, ask_size),
            sigma_ticks_per_sqrt_sec=_realized_sigma(history),
            update_rate_hz=_update_rate_hz(history),
            sample_count=len(history),
            min_samples_required=self.min_samples,
            price_levels=orderbook.price_levels,
            seconds_to_close=seconds_to_close,
        )

    def reset(self, market_ticker: MarketTicker | None = None) -> None:
        if market_ticker is None:
            self._history.clear()
        else:
            self._history.pop(market_ticker, None)

    def _trim(self, history: deque[tuple[float, int]], now: float) -> None:
        """Drop observations older than the window, strictly.

        Keeping `min_samples` regardless of age looks like a kindness to the
        estimator and is the opposite. After a feed gap the retained samples
        span the whole outage, so the realized-variance denominator becomes
        huge and sigma collapses toward zero - telling the strategy the market
        is calm at the exact moment it has no idea what the market is doing.
        Better to have too few samples and say so via `has_volatility_estimate`.
        """

        cutoff = now - self.vol_window_seconds

        while history and history[0][0] < cutoff:
            history.popleft()


def _microprice(best_bid: int, best_ask: int, bid_size: int, ask_size: int) -> int:
    """Size-weighted fair value.

    Weighted so that a heavy bid pulls the estimate toward the ask: resting
    size on your side of the book is the side that is *not* about to trade.
    """

    total = bid_size + ask_size

    if total <= 0:
        return (best_bid + best_ask) // 2

    return (bid_size * best_ask + ask_size * best_bid) // total


def _imbalance_bps(bid_size: int, ask_size: int) -> int:
    """(bid - ask) / (bid + ask) in basis points. Positive means bid-heavy."""

    total = bid_size + ask_size

    if total <= 0:
        return 0

    return (bid_size - ask_size) * BPS_SCALE // total


def _realized_sigma(history: deque[tuple[float, int]]) -> float:
    """Realized volatility of the mid, in price ticks per sqrt(second).

    Sum of squared successive mid changes divided by elapsed time. Irregular
    sampling is fine - that is exactly what this estimator is for - but a
    window where nothing moved correctly returns zero.
    """

    if len(history) < 2:
        return 0.0

    elapsed = history[-1][0] - history[0][0]

    if elapsed <= 0:
        return 0.0

    sum_squared_changes = 0.0
    previous_mid = history[0][1]

    for _, mid in history:
        change = mid - previous_mid
        sum_squared_changes += float(change * change)
        previous_mid = mid

    return sqrt(sum_squared_changes / elapsed)


def _update_rate_hz(history: deque[tuple[float, int]]) -> float:
    if len(history) < 2:
        return 0.0

    elapsed = history[-1][0] - history[0][0]

    if elapsed <= 0:
        return 0.0

    return (len(history) - 1) / elapsed


def _forced_hold_probability(seconds_to_close: float, flatten_seconds: float) -> float:
    """How likely we are to be stuck with inventory at resolution.

    Zero while there is comfortably more time than a flatten takes, rising to
    one at the bell. The shape is a judgement call, not a measurement; what
    matters is that it is continuous and hits 1.0 at zero so the strategy stops
    treating a soon-to-resolve position as cheaply exitable.
    """

    if flatten_seconds <= 0:
        return 0.0 if seconds_to_close > 0 else 1.0

    if seconds_to_close <= 0:
        return 1.0

    ratio = seconds_to_close / flatten_seconds

    if ratio >= 1.0:
        return 0.0

    return 1.0 - ratio
