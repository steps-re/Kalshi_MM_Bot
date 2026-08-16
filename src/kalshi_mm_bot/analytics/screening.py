"""Rank markets by whether market making in them can pay for itself.

The question "which markets should we quote?" has a mostly arithmetic answer on
Kalshi, because the fee is a known function of price:

    round-trip fee per contract = 2 * ceil_to_cent(0.07 * C * P * (1 - P)) / C

At $0.50 that is 3.5 cents against a 1 cent tick. A market maker capturing an
entire 2 cent spread at the midpoint still loses 1.5 cents per contract per
round trip. No amount of parameter tuning fixes it, and no amount of small-size
live testing reveals it quickly - it just shows up as "fees ate everything".

The fee falls with P*(1-P), so the same 2 cent spread is worth +1.3 cents at
$0.05 and -1.5 cents at $0.50. Screening on `net_edge_ticks` therefore does
most of the work of finding alpha before a single order is sent, and the
`structurally_unviable` count says how much of the exchange is off the table.

Everything here is a pure function of market metadata so it can be tested and
so the ranking can be re-derived from a saved snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE, ONE_DOLLAR
from kalshi_mm_bot.sim.fees import DEFAULT_FEE_MODEL, KalshiFeeModel

# A quote that joins the touch captures the whole spread but rarely fills. One
# tick of improvement per side is the realistic assumption for a maker who
# wants to trade, and it is the default the screen prices in.
DEFAULT_IMPROVEMENT_TICKS = 100
DEFAULT_PARTICIPATION_SHARE = 0.10
DEFAULT_ASSUMED_SIZE = 50 * COUNT_SCALE


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """The subset of Kalshi market metadata the screen needs."""

    ticker: str
    yes_bid: int
    yes_ask: int
    volume_24h: int
    open_interest: int = 0
    seconds_to_close: float | None = None
    series: str = ""
    title: str = ""

    @property
    def mid(self) -> int:
        return (self.yes_bid + self.yes_ask) // 2

    @property
    def spread_ticks(self) -> int:
        return self.yes_ask - self.yes_bid

    @property
    def is_quotable(self) -> bool:
        return 0 < self.yes_bid < self.yes_ask < ONE_DOLLAR


@dataclass(frozen=True, slots=True)
class MarketScore:
    market: MarketQuote
    fee_round_trip_ticks: int
    capturable_ticks: int
    net_edge_ticks: int
    expected_daily_micros: int

    @property
    def ticker(self) -> str:
        return self.market.ticker

    @property
    def structurally_unviable(self) -> bool:
        """True when the fee exceeds the whole spread at this price.

        Not "unprofitable given our parameters" - impossible, for anyone
        quoting this market at this price with this fee schedule.
        """

        return self.net_edge_ticks <= 0

    def describe(self) -> str:
        return (
            f"{self.ticker:<28} mid={self.market.mid / ONE_DOLLAR:>5.2f} "
            f"spread={self.market.spread_ticks / 100:>5.1f}c "
            f"fee={self.fee_round_trip_ticks / 100:>5.2f}c "
            f"net={self.net_edge_ticks / 100:>6.2f}c "
            f"vol24h={self.market.volume_24h:>8} "
            f"est=${self.expected_daily_micros / MONEY_SCALE:>8.2f}/day"
        )


@dataclass(frozen=True, slots=True)
class ScreenReport:
    scores: tuple[MarketScore, ...]
    considered: int
    skipped: int

    @property
    def viable(self) -> tuple[MarketScore, ...]:
        return tuple(score for score in self.scores if not score.structurally_unviable)

    @property
    def unviable_count(self) -> int:
        return sum(1 for score in self.scores if score.structurally_unviable)

    @property
    def unviable_share(self) -> float:
        return self.unviable_count / len(self.scores) if self.scores else 0.0

    def top(self, limit: int = 20) -> tuple[MarketScore, ...]:
        return self.viable[:limit]

    def by_series(self) -> dict[str, tuple[int, int]]:
        """Series -> (viable count, total count), for a diversity read."""

        totals: dict[str, list[int]] = {}

        for score in self.scores:
            entry = totals.setdefault(score.market.series or "(unknown)", [0, 0])
            entry[1] += 1

            if not score.structurally_unviable:
                entry[0] += 1

        return {series: (counts[0], counts[1]) for series, counts in totals.items()}

    def describe(self, limit: int = 20) -> str:
        lines = [
            f"screened {self.considered} market(s), skipped {self.skipped} unquotable",
            f"structurally unviable at current prices: {self.unviable_count} "
            f"({self.unviable_share:.0%})",
            "",
        ]

        top = self.top(limit)

        if not top:
            lines.append("no market clears its own round-trip fee right now")
            return "\n".join(lines)

        lines.append(f"top {len(top)} by expected daily net edge:")
        lines.extend(f"  {score.describe()}" for score in top)
        return "\n".join(lines)


def score_market(
    market: MarketQuote,
    *,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    improvement_ticks: int = DEFAULT_IMPROVEMENT_TICKS,
    participation_share: float = DEFAULT_PARTICIPATION_SHARE,
    assumed_size: int = DEFAULT_ASSUMED_SIZE,
) -> MarketScore:
    """Net capturable edge per round trip, and a rough daily value."""

    fee_round_trip = fee_model.breakeven_edge_ticks(
        yes_price=market.mid,
        count=assumed_size,
    )
    capturable = max(0, market.spread_ticks - 2 * improvement_ticks)
    net_edge = capturable - fee_round_trip

    # Each round trip consumes two contracts of exchange volume - our buy and
    # our sell - so the share of 24h volume we could plausibly intermediate
    # translates to half as many round trips.
    round_trips = market.volume_24h * participation_share / 2.0
    expected = int(max(0, net_edge) * round_trips * COUNT_SCALE)

    return MarketScore(
        market=market,
        fee_round_trip_ticks=fee_round_trip,
        capturable_ticks=capturable,
        net_edge_ticks=net_edge,
        expected_daily_micros=expected,
    )


def screen_markets(
    markets: Iterable[MarketQuote],
    *,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    improvement_ticks: int = DEFAULT_IMPROVEMENT_TICKS,
    participation_share: float = DEFAULT_PARTICIPATION_SHARE,
    assumed_size: int = DEFAULT_ASSUMED_SIZE,
    min_volume_24h: int = 0,
) -> ScreenReport:
    """Score and rank every quotable market."""

    scores: list[MarketScore] = []
    considered = 0
    skipped = 0

    for market in markets:
        considered += 1

        if not market.is_quotable or market.volume_24h < min_volume_24h:
            skipped += 1
            continue

        scores.append(
            score_market(
                market,
                fee_model=fee_model,
                improvement_ticks=improvement_ticks,
                participation_share=participation_share,
                assumed_size=assumed_size,
            )
        )

    scores.sort(key=lambda score: (score.expected_daily_micros, score.net_edge_ticks), reverse=True)

    return ScreenReport(scores=tuple(scores), considered=considered, skipped=skipped)


def fee_curve(
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    *,
    count: int = DEFAULT_ASSUMED_SIZE,
    step: int = 500,
) -> tuple[tuple[int, int], ...]:
    """(price, round-trip fee in ticks) across the price range.

    Useful for showing why the tails are the only place the arithmetic works.
    """

    return tuple(
        (price, fee_model.breakeven_edge_ticks(yes_price=price, count=count))
        for price in range(step, ONE_DOLLAR, step)
    )


def viable_price_band(
    spread_ticks: int,
    *,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    count: int = DEFAULT_ASSUMED_SIZE,
    improvement_ticks: int = DEFAULT_IMPROVEMENT_TICKS,
) -> tuple[int, int] | None:
    """Prices where `spread_ticks` covers the round-trip fee.

    Returns the inclusive (low, high) YES-price bounds of the lower tail; the
    upper tail is its mirror about $1.00. None when no price works.
    """

    capturable = spread_ticks - 2 * improvement_ticks

    if capturable <= 0:
        return None

    viable = [
        price
        for price in range(1, ONE_DOLLAR // 2)
        if fee_model.breakeven_edge_ticks(yes_price=price, count=count) <= capturable
    ]

    return (viable[0], viable[-1]) if viable else None


def describe_series(report: ScreenReport, limit: int = 15) -> str:
    """Which product families are worth quoting at all."""

    rows = sorted(
        report.by_series().items(),
        key=lambda item: (item[1][0], item[1][1]),
        reverse=True,
    )[:limit]

    if not rows:
        return "no series to report"

    lines = ["viable markets by series:"]
    lines.extend(
        f"  {series:<26} {viable:>4} viable of {total:>4}"
        for series, (viable, total) in rows
    )
    return "\n".join(lines)


def parse_market(raw: dict) -> MarketQuote | None:
    """Build a `MarketQuote` from a Kalshi `/markets` entry.

    Kalshi reports `yes_bid` and `yes_ask` in whole cents. Returns None when
    the entry lacks a two-sided market, which is not an error - most of the
    exchange is untraded at any moment.
    """

    ticker = raw.get("ticker")
    yes_bid = raw.get("yes_bid")
    yes_ask = raw.get("yes_ask")

    if not ticker or yes_bid is None or yes_ask is None:
        return None

    return MarketQuote(
        ticker=str(ticker),
        yes_bid=int(yes_bid) * 100,
        yes_ask=int(yes_ask) * 100,
        volume_24h=int(raw.get("volume_24h") or 0),
        open_interest=int(raw.get("open_interest") or 0),
        series=str(raw.get("series_ticker") or raw.get("event_ticker") or ""),
        title=str(raw.get("title") or ""),
    )


def parse_markets(raw_markets: Sequence[dict]) -> tuple[MarketQuote, ...]:
    parsed = (parse_market(raw) for raw in raw_markets)
    return tuple(market for market in parsed if market is not None)
