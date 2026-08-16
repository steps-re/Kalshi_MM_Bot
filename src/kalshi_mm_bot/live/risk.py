"""Pre-trade and in-flight risk limits with a kill switch.

A strategy decides what it *wants* to do. Risk decides what it is *allowed* to
do, and the two must be separate code: every way a trading system loses more
money than intended runs through the strategy being wrong in a way the strategy
cannot detect. The checks here are deliberately dumb and absolute.

The one that matters most and is least obvious is `max_feed_silence_seconds`.
Resting orders do not cancel themselves when the websocket goes quiet. If the
feed stalls while quotes are live, the bot is showing prices based on a book
that may be seconds stale, and it will be picked off by everyone whose feed
still works. Silence is not calm - it is the most dangerous state the system
can be in, and the correct response is to pull everything.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE


class BreachKind(str, Enum):
    POSITION = "position"
    SESSION_LOSS = "session_loss"
    DRAWDOWN = "drawdown"
    ORDER_RATE = "order_rate"
    REJECTIONS = "rejections"
    FEED_SILENCE = "feed_silence"


@dataclass(frozen=True, slots=True)
class RiskBreach:
    kind: BreachKind
    detail: str
    halt: bool = True

    def describe(self) -> str:
        action = "HALT" if self.halt else "BLOCK"
        return f"[{action}] {self.kind.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Hard bounds on a live session.

    Every limit is optional so a limit can be disabled explicitly rather than
    by setting a number so large it never triggers - "None" is greppable and
    "999999999" is a bug waiting to happen.
    """

    max_abs_position: int | None = None
    max_session_loss_micros: int | None = None
    max_drawdown_micros: int | None = None
    max_orders_per_minute: int | None = None
    max_consecutive_rejections: int | None = None
    max_feed_silence_seconds: float | None = 15.0

    def __post_init__(self) -> None:
        # A negative limit reads as "off" but behaves as "inverted": a
        # max_session_loss of -$5 would only trip once the account was $5 UP.
        # Reject it rather than fail open on a sign slip.
        for name in (
            "max_abs_position",
            "max_session_loss_micros",
            "max_drawdown_micros",
            "max_orders_per_minute",
            "max_consecutive_rejections",
            "max_feed_silence_seconds",
        ):
            value = getattr(self, name)

            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative or None, got {value!r}")

    @classmethod
    def conservative(cls, *, contracts: int = 10, loss_dollars: float = 5.0) -> "RiskLimits":
        """Sane starting limits for a first live run."""

        return cls(
            max_abs_position=contracts * COUNT_SCALE,
            max_session_loss_micros=int(loss_dollars * MONEY_SCALE),
            max_drawdown_micros=int(loss_dollars * MONEY_SCALE),
            max_orders_per_minute=120,
            max_consecutive_rejections=5,
            max_feed_silence_seconds=15.0,
        )


@dataclass(slots=True)
class RiskMonitor:
    """Tracks session state and reports the first limit that trips.

    Once halted it stays halted. A risk system that can un-halt itself will,
    at exactly the wrong moment.
    """

    limits: RiskLimits = field(default_factory=RiskLimits)
    peak_equity_micros: int = 0
    _halted: RiskBreach | None = field(default=None, init=False)
    _order_times: deque[float] = field(default_factory=deque, init=False)
    _consecutive_rejections: int = field(default=0, init=False)
    _last_event_monotonic: float | None = field(default=None, init=False)
    _started_monotonic: float = field(default_factory=time.monotonic, init=False)

    @property
    def halted(self) -> bool:
        return self._halted is not None

    @property
    def halt_reason(self) -> RiskBreach | None:
        return self._halted

    def record_event(self, *, now: float | None = None) -> None:
        self._last_event_monotonic = time.monotonic() if now is None else now

    def record_order(self, *, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        self._order_times.append(moment)
        self._trim_order_times(moment)

    def record_rejection(self) -> None:
        self._consecutive_rejections += 1

    def record_acceptance(self) -> None:
        self._consecutive_rejections = 0

    def check(
        self,
        *,
        position: int,
        equity_micros: int,
        now: float | None = None,
    ) -> RiskBreach | None:
        """Evaluate every limit. Returns the first breach, or None."""

        if self._halted is not None:
            return self._halted

        moment = time.monotonic() if now is None else now
        self.peak_equity_micros = max(self.peak_equity_micros, equity_micros)

        breach = (
            self._check_position(position)
            or self._check_session_loss(equity_micros)
            or self._check_drawdown(equity_micros)
            or self._check_order_rate(moment)
            or self._check_rejections()
            or self._check_feed_silence(moment)
        )

        if breach is not None and breach.halt:
            self._halted = breach

        return breach

    def _check_position(self, position: int) -> RiskBreach | None:
        limit = self.limits.max_abs_position

        if limit is None or abs(position) <= limit:
            return None

        return RiskBreach(
            kind=BreachKind.POSITION,
            detail=f"position {_contracts(position)} exceeds limit {_contracts(limit)}",
        )

    def _check_session_loss(self, equity_micros: int) -> RiskBreach | None:
        limit = self.limits.max_session_loss_micros

        if limit is None or equity_micros >= -limit:
            return None

        return RiskBreach(
            kind=BreachKind.SESSION_LOSS,
            detail=f"session P&L {_money(equity_micros)} beyond limit -{_money(limit)}",
        )

    def _check_drawdown(self, equity_micros: int) -> RiskBreach | None:
        limit = self.limits.max_drawdown_micros

        if limit is None:
            return None

        drawdown = self.peak_equity_micros - equity_micros

        if drawdown <= limit:
            return None

        return RiskBreach(
            kind=BreachKind.DRAWDOWN,
            detail=f"drawdown {_money(drawdown)} from peak exceeds {_money(limit)}",
        )

    def _check_order_rate(self, now: float) -> RiskBreach | None:
        limit = self.limits.max_orders_per_minute

        if limit is None:
            return None

        self._trim_order_times(now)

        if len(self._order_times) <= limit:
            return None

        return RiskBreach(
            kind=BreachKind.ORDER_RATE,
            detail=f"{len(self._order_times)} orders in the last minute exceeds {limit}",
        )

    def _check_rejections(self) -> RiskBreach | None:
        limit = self.limits.max_consecutive_rejections

        if limit is None or self._consecutive_rejections <= limit:
            return None

        return RiskBreach(
            kind=BreachKind.REJECTIONS,
            detail=f"{self._consecutive_rejections} consecutive rejections exceeds {limit}",
        )

    def _check_feed_silence(self, now: float) -> RiskBreach | None:
        limit = self.limits.max_feed_silence_seconds

        if limit is None:
            return None

        # Before the first event, measure from session start so a feed that
        # never delivers anything still trips the limit.
        reference = (
            self._last_event_monotonic
            if self._last_event_monotonic is not None
            else self._started_monotonic
        )
        silence = now - reference

        if silence <= limit:
            return None

        return RiskBreach(
            kind=BreachKind.FEED_SILENCE,
            detail=(
                f"no market data for {silence:.1f}s (limit {limit:.1f}s); "
                "resting quotes are stale"
            ),
        )

    def _trim_order_times(self, now: float) -> None:
        cutoff = now - 60.0

        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()


def _money(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    micros = abs(micros)
    return f"{sign}${micros // MONEY_SCALE}.{(micros % MONEY_SCALE) // 10_000:02d}"


def _contracts(count: int) -> str:
    return f"{count / COUNT_SCALE:.2f}"
