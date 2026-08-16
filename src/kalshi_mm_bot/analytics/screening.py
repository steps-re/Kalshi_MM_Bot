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
from kalshi_mm_bot.market.fees import DEFAULT_FEE_MODEL, KalshiFeeModel

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
    # Minimum price increment. Whole cents on most markets, a tenth of a cent
    # on deci-cent markets, which can therefore hold a tighter spread.
    tick_ticks: int = 100

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


def capturable_ticks(market: MarketQuote, improvement_ticks: int) -> int:
    """Spread we could actually capture after stepping inside the touch.

    Improvement is capped by what the spread physically allows. A market
    already quoted one tick wide cannot be improved at all - there is nowhere
    to stand between the bid and the ask - so the only option is to join, and
    the whole spread is capturable. Applying a flat "give up a cent per side"
    to those markets subtracts liquidity that was never there and wrongly
    condemns every tick-wide market, which on Kalshi is most of them.

    At least one tick of spread must survive, or we would be crossing.
    """

    tick = max(1, market.tick_ticks)
    # Improvement happens in whole ticks and must leave at least one tick of
    # spread standing, so it is the total across both sides that is bounded.
    room = max(0, market.spread_ticks - tick)
    improvement = min(2 * improvement_ticks, room) // tick * tick

    return max(tick, market.spread_ticks - improvement)


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
    capturable = capturable_ticks(market, improvement_ticks)
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
    tick_ticks: int = 100,
) -> tuple[int, int] | None:
    """Prices where `spread_ticks` covers the round-trip fee.

    Returns the inclusive (low, high) YES-price bounds of the lower tail; the
    upper tail is its mirror about $1.00. None when no price works.
    """

    probe = MarketQuote(
        ticker="",
        yes_bid=0,
        yes_ask=spread_ticks,
        volume_24h=0,
        tick_ticks=tick_ticks,
    )
    capturable = capturable_ticks(probe, improvement_ticks)

    if capturable <= 0:
        return None

    viable = [
        price
        for price in range(1, ONE_DOLLAR // 2)
        if fee_model.breakeven_edge_ticks(yes_price=price, count=count) <= capturable
    ]

    return (viable[0], viable[-1]) if viable else None


PRICE_BANDS: tuple[tuple[str, int], ...] = (
    ("deep tail (<=5c from an end)", 500),
    ("tail (5-15c)", 1_500),
    ("shoulder (15-30c)", 3_000),
    ("near the money (30-50c)", 5_000),
)


def distance_from_end(yes_price: int) -> int:
    """How far a price sits from the nearest end of the range, in ticks.

    The fee depends on `P * (1 - P)`, which is symmetric, so this single number
    - not the price itself - determines whether a market is quotable.
    """

    return min(yes_price, ONE_DOLLAR - yes_price)


def price_band(yes_price: int) -> str:
    distance = distance_from_end(yes_price)

    for label, upper in PRICE_BANDS:
        if distance <= upper:
            return label

    return PRICE_BANDS[-1][0]


def by_price_band(report: ScreenReport) -> dict[str, dict[str, int]]:
    """Viability grouped by distance from the end of the range.

    This is the screen's most actionable cut. On a strike ladder the same
    underlying produces markets across the whole probability range, and only
    the ends of the ladder clear the fee - so the rule is about *which strikes*
    to quote, not which product.
    """

    bands: dict[str, dict[str, int]] = {
        label: {"markets": 0, "viable": 0, "volume": 0, "viable_volume": 0, "daily_micros": 0}
        for label, _ in PRICE_BANDS
    }

    for score in report.scores:
        entry = bands[price_band(score.market.mid)]
        entry["markets"] += 1
        entry["volume"] += score.market.volume_24h

        if not score.structurally_unviable:
            entry["viable"] += 1
            entry["viable_volume"] += score.market.volume_24h
            entry["daily_micros"] += score.expected_daily_micros

    return bands


def describe_price_bands(report: ScreenReport) -> str:
    lines = ["viability by distance from the end of the range:"]

    for label, _ in PRICE_BANDS:
        entry = by_price_band(report)[label]

        if not entry["markets"]:
            continue

        lines.append(
            f"  {label:<30} {entry['viable']:>4}/{entry['markets']:<4} viable  "
            f"{entry['volume']:>9,} contracts  "
            f"${entry['daily_micros'] / MONEY_SCALE:>8.2f}/day"
        )

    return "\n".join(lines)


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

    The live API reports prices as decimal-dollar strings (`yes_bid_dollars`)
    and sizes as fixed-point strings (`volume_24h_fp`). Older integer-cent
    fields are accepted too so saved payloads keep working.

    Returns None when the entry lacks a two-sided market, which is not an
    error - most of the exchange is untraded at any moment.
    """

    ticker = raw.get("ticker")

    if not ticker:
        return None

    yes_bid = _price_ticks(raw, "yes_bid_dollars", "yes_bid")
    yes_ask = _price_ticks(raw, "yes_ask_dollars", "yes_ask")

    if yes_bid is None or yes_ask is None:
        return None

    return MarketQuote(
        ticker=str(ticker),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume_24h=_count(raw, "volume_24h_fp", "volume_24h"),
        open_interest=_count(raw, "open_interest_fp", "open_interest"),
        seconds_to_close=None,
        series=str(raw.get("series_ticker") or raw.get("event_ticker") or ""),
        title=str(raw.get("title") or ""),
        tick_ticks=_tick_size(raw),
    )


def _price_ticks(raw: dict, dollars_key: str, cents_key: str) -> int | None:
    """Price in ticks, from either the decimal-dollar or integer-cent field."""

    dollars = raw.get(dollars_key)

    if dollars not in (None, ""):
        try:
            return int(round(float(dollars) * ONE_DOLLAR))
        except (TypeError, ValueError):
            return None

    cents = raw.get(cents_key)

    if cents in (None, ""):
        return None

    try:
        return int(cents) * 100
    except (TypeError, ValueError):
        return None


def _count(raw: dict, fp_key: str, plain_key: str) -> int:
    """Contract count, rounded down to whole contracts."""

    value = raw.get(fp_key)

    if value not in (None, ""):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    try:
        return int(raw.get(plain_key) or 0)
    except (TypeError, ValueError):
        return 0


def tick_at_price(price_ranges: Sequence[dict], yes_price: int) -> int:
    """Minimum price increment **at this price**, in ticks.

    Kalshi's common structure is `tapered_deci_cent`: 0.1c steps below $0.10
    and above $0.90, 1c steps in between. Treating tick size as a per-market
    constant is therefore wrong in both directions - it claims you can improve
    a midpoint quote by a tenth of a cent (you cannot) and that a tail quote is
    stuck on whole cents (it is not).

    This matters more than it sounds. The fee analysis already showed the tails
    are the only place the fee arithmetic works; the taper means the tails are
    also the only place you can step inside a one-cent spread to jump the
    queue. Cheap fees and a fine tick land in the same place.
    """

    for price_range in price_ranges or ():
        try:
            low = int(round(float(price_range["start"]) * ONE_DOLLAR))
            high = int(round(float(price_range["end"]) * ONE_DOLLAR))
            step = int(round(float(price_range["step"]) * ONE_DOLLAR))
        except (KeyError, TypeError, ValueError):
            continue

        if low <= yes_price <= high and step > 0:
            return step

    return 100


def _tick_size(raw: dict) -> int:
    """Tick at this market's current mid, not at the bottom of its range.

    An earlier version returned the first range's step, which on a tapered
    market is the 0.1c tail step - so every midpoint market was modelled as
    ten times finer than it is.
    """

    bid = _price_ticks(raw, "yes_bid_dollars", "yes_bid")
    ask = _price_ticks(raw, "yes_ask_dollars", "yes_ask")

    if bid is None or ask is None:
        return 100

    return tick_at_price(raw.get("price_ranges") or (), (bid + ask) // 2)


def is_combo_market(raw: dict) -> bool:
    """True for auto-generated multivariate parlay markets.

    Kalshi generates enormous numbers of these - tens of thousands, versus a
    few thousand real markets - and essentially none of them are quoted. They
    dominate a naive `/markets` scan and crowd out everything worth looking at,
    so the screen drops them by default.
    """

    return bool(raw.get("mve_collection_ticker") or raw.get("mve_selected_legs"))


def parse_markets(
    raw_markets: Sequence[dict],
    *,
    skip_combos: bool = True,
) -> tuple[MarketQuote, ...]:
    parsed = (
        parse_market(raw)
        for raw in raw_markets
        if not (skip_combos and is_combo_market(raw))
    )
    return tuple(market for market in parsed if market is not None)
