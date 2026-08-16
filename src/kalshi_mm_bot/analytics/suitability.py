"""Which market families actually suit the strategy we built.

The fee screen answers "can market making pay here at all?". That is necessary
and not sufficient: a market can clear its fee and still be a bad fit, because
the strategy's specific machinery does nothing there.

What `horizon` actually brings, beyond quoting a spread:

* **Expiry handling.** It tapers size, goes reduce-only, then flattens as the
  close approaches, and blends inventory risk from a diffusion toward the binary
  payoff. All of that is inert in a market that settles in six months.
* **Volatility-aware widening.** It sizes the adverse-selection premium off
  measured sigma. In a market whose book never moves, that machinery costs
  nothing and earns nothing.
* **Tail-aware fee sizing.** It refuses orders whose edge cannot cover their own
  fee, which matters most where the fee is close to the spread.

So the fit score rewards markets that are short-dated, actually move, and sit
where the fee arithmetic is live rather than hopeless. A market that scores high
on the fee screen but expires next year is a market where we are just another
spread quoter with no advantage.

**Spread you cannot reach is not edge.** An earlier version of this score ranked
purely on net edge and log volume, and its top families were things like
KXWNBATEAMTOTAL: a 48c spread over a book that trades 56 contracts a day, where
the resting queue needs 38 days to clear. The arithmetic was right and the
conclusion was worthless, because a maker joining that queue never reaches the
front. Wide spreads in dead markets are wide *because* they are dead.

So the score now multiplies through `queue_clearance` - how many times the queue
ahead of us can turn over before the market closes. That single term is what
separates the 15-minute crypto window, where the whole book recycles many times
an hour, from a wide prop market that has not traded since yesterday. Depth is
not in the `/markets` payload and has to be probed per market, so it is optional
here; when it is missing the market is reported UNMEASURED rather than scored,
because an unmeasured queue is the assumption most likely to be flattering.

Deliberately kept separate from `screening.py`: that module answers a question
about the exchange, this one answers a question about *us*, and conflating them
would hide the assumption that our machinery is worth anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from kalshi_mm_bot.analytics.screening import MarketQuote, capturable_ticks, price_band
from kalshi_mm_bot.market.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR

DEFAULT_SIZE = 50 * COUNT_SCALE

# Below this, expiry machinery has something to act on within a session.
SHORT_DATED_SECONDS = 6 * 3600
# Above this, the market resolves so far out that time-to-close never binds.
LONG_DATED_SECONDS = 14 * 24 * 3600

# For a market with no close in sight, "before it closes" is not a useful
# horizon, so clearance is measured against a session instead.
SESSION_SECONDS = 6 * 3600

# The queue has to turn over several times over, not just once: joining at the
# back and being reached exactly at the bell is not a business.
MIN_QUEUE_CLEARANCE = 3.0


@dataclass(frozen=True, slots=True)
class Suitability:
    """How well one market fits what the strategy actually does."""

    ticker: str
    series: str
    net_edge_ticks: int
    spread_ticks: int
    volume_24h: int
    seconds_to_close: float | None
    band: str
    depth_at_touch: float | None = None

    @property
    def fee_viable(self) -> bool:
        return self.net_edge_ticks > 0

    @property
    def expected_wait_seconds(self) -> float | None:
        """Seconds for the queue ahead of us at the touch to trade through.

        Volume is two-sided and our order sits on one side, so only half the
        flow can reach us. This is an optimistic floor either way: it assumes
        every contract traded hits the touch and that nobody joins ahead of us
        or cancels behind. A market that fails this test fails a generous one.
        """

        if self.depth_at_touch is None or self.volume_24h <= 0:
            return None

        per_side_per_second = (self.volume_24h / 86_400.0) / 2.0
        return self.depth_at_touch / per_side_per_second

    @property
    def queue_clearance(self) -> float | None:
        """How many times the queue can turn over before we run out of market.

        None when depth was never probed - which is a missing measurement, not
        a passing grade, and callers must treat it as such.
        """

        wait = self.expected_wait_seconds

        if wait is None:
            return None

        if wait <= 0:
            return float("inf")

        horizon = self.seconds_to_close or SESSION_SECONDS
        return min(horizon, SESSION_SECONDS) / wait

    @property
    def queue_viable(self) -> bool | None:
        clearance = self.queue_clearance
        return None if clearance is None else clearance >= MIN_QUEUE_CLEARANCE

    @property
    def expiry_fit(self) -> float:
        """1.0 when expiry machinery has something to do, 0.0 when it is inert."""

        if self.seconds_to_close is None:
            return 0.0

        if self.seconds_to_close <= SHORT_DATED_SECONDS:
            return 1.0

        if self.seconds_to_close >= LONG_DATED_SECONDS:
            return 0.0

        span = LONG_DATED_SECONDS - SHORT_DATED_SECONDS
        return 1.0 - (self.seconds_to_close - SHORT_DATED_SECONDS) / span

    def score(self) -> float:
        """Composite fit. Zero whenever the fee arithmetic cannot work.

        Multiplicative rather than additive on purpose: a market that fails the
        fee test is not partially suitable, it is unsuitable, and no amount of
        volume or volatility redeems it. The same logic applies to the queue -
        an edge behind a queue we never reach is worth exactly nothing, so
        clearance multiplies rather than adjusts.

        An unprobed queue scores zero too. That is deliberately inconvenient:
        the alternative is a ranking whose top entries are all markets nobody
        bothered to measure, which is how the dead-wide-spread families topped
        this list in the first place.
        """

        if not self.fee_viable or self.volume_24h <= 0:
            return 0.0

        clearance = self.queue_clearance

        if clearance is None or clearance < MIN_QUEUE_CLEARANCE:
            return 0.0

        edge = self.net_edge_ticks / 100.0
        # Volume enters through a log so one enormous market cannot dominate a
        # family ranking on its own.
        from math import log10

        liquidity = log10(1.0 + self.volume_24h)
        # Clearance is logged for the same reason, and floored at 1.0 so a
        # queue that merely passes the bar neither helps nor hurts.
        turnover = log10(clearance / MIN_QUEUE_CLEARANCE * 10.0)
        return edge * liquidity * turnover * (0.25 + 0.75 * self.expiry_fit)


@dataclass(frozen=True, slots=True)
class FamilyFit:
    family: str
    markets: int
    viable: int
    volume_24h: int
    median_seconds_to_close: float | None
    short_dated_share: float
    mean_net_edge_ticks: float
    total_score: float

    @property
    def viable_share(self) -> float:
        return self.viable / self.markets if self.markets else 0.0

    def describe(self) -> str:
        close = (
            f"{self.median_seconds_to_close / 3600:>7.1f}h"
            if self.median_seconds_to_close is not None
            else "      ?"
        )
        return (
            f"{self.family[:26]:<27}{self.viable:>5}/{self.markets:<6}"
            f"{self.volume_24h:>11,}{close}"
            f"{self.short_dated_share:>8.0%}{self.mean_net_edge_ticks / 100:>9.2f}c"
            f"{self.total_score:>10.1f}"
        )


def assess(
    quote: MarketQuote,
    *,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    improvement_ticks: int = 100,
    assumed_size: int = DEFAULT_SIZE,
    depth_at_touch: float | None = None,
) -> Suitability:
    """Assess one market. `depth_at_touch` is contracts resting at the touch,
    averaged across the two sides; without it the queue cannot be judged and
    the market scores zero rather than being assumed clear."""

    capturable = capturable_ticks(quote, improvement_ticks)
    fee = fee_model.breakeven_edge_ticks(yes_price=quote.mid, count=assumed_size)

    return Suitability(
        ticker=quote.ticker,
        series=quote.series,
        net_edge_ticks=capturable - fee,
        spread_ticks=quote.spread_ticks,
        volume_24h=quote.volume_24h,
        seconds_to_close=quote.seconds_to_close,
        band=price_band(quote.mid),
        depth_at_touch=depth_at_touch,
    )


def by_family(
    assessments: Iterable[Suitability],
    *,
    family_of=None,
) -> tuple[FamilyFit, ...]:
    """Roll suitability up to the product-family level, ranked by total fit."""

    from statistics import median

    key = family_of or (lambda s: s.ticker.split("-")[0])
    grouped: dict[str, list[Suitability]] = {}

    for assessment in assessments:
        grouped.setdefault(key(assessment), []).append(assessment)

    fits: list[FamilyFit] = []

    for family, items in grouped.items():
        closes = [i.seconds_to_close for i in items if i.seconds_to_close is not None]
        viable = [i for i in items if i.fee_viable]

        fits.append(
            FamilyFit(
                family=family,
                markets=len(items),
                viable=len(viable),
                volume_24h=sum(i.volume_24h for i in items),
                median_seconds_to_close=median(closes) if closes else None,
                short_dated_share=(
                    sum(1 for i in items if i.expiry_fit >= 0.99) / len(items)
                ),
                mean_net_edge_ticks=(
                    sum(i.net_edge_ticks for i in viable) / len(viable) if viable else 0.0
                ),
                total_score=sum(i.score() for i in items),
            )
        )

    fits.sort(key=lambda f: -f.total_score)
    return tuple(fits)


def describe_families(fits: Sequence[FamilyFit], limit: int = 15) -> str:
    header = (
        f"{'family':<27}{'viable':>11}{'24h volume':>11}{'med close':>8}"
        f"{'short':>8}{'edge':>10}{'fit':>10}"
    )
    lines = ["market families by fit with the strategy we built:", header]
    lines.extend(fit.describe() for fit in fits[:limit] if fit.total_score > 0)

    if not any(fit.total_score > 0 for fit in fits):
        lines.append("  nothing scores above zero - no family suits this strategy today")

    return "\n".join(lines)
