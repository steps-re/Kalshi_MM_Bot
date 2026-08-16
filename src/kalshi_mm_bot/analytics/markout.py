"""Markout: did the market move against us right after we traded?

This is the diagnostic a market-making desk looks at before P&L, because P&L
over a short session is mostly noise while markout is measurable on a few
hundred fills.

For a fill at price `p` and signed direction `d` (+1 bought, -1 sold), the
markout at horizon `h` is `d * (mid(t + h) - p) * count`. A market maker
expects this to be *positive* on average: you buy below fair value and the mid
does not run away from you. Persistently negative markout means you are being
adversely selected - the people trading against you know something about the
next tick, and no amount of spread will save you.

The horizon curve matters as much as the level. Markout that is positive at one
second and negative at thirty says quotes are being picked off slowly by
informed flow. Markout that is negative immediately says the quotes are simply
stale relative to how fast the book moves.

Bucketing markout by time-to-close is how the "the last minutes of a 15-minute
BTC market behave differently" observation becomes a number you can act on
rather than an impression.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from kalshi_mm_bot.market.price import MONEY_SCALE
from kalshi_mm_bot.market.series import MidSeries
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.sim.fills import SimulatedFill

DEFAULT_HORIZONS_SECONDS = (1.0, 5.0, 15.0, 30.0, 60.0)


@dataclass(frozen=True, slots=True)
class HorizonMarkout:
    horizon_seconds: float
    fill_count: int
    total_micros: int
    per_contract_ticks: float

    @property
    def mean_micros(self) -> float:
        return self.total_micros / self.fill_count if self.fill_count else 0.0

    @property
    def is_adverse(self) -> bool:
        return self.fill_count > 0 and self.total_micros < 0


@dataclass(frozen=True, slots=True)
class MarkoutReport:
    horizons: tuple[HorizonMarkout, ...]
    skipped_fills: int

    def at(self, horizon_seconds: float) -> HorizonMarkout | None:
        for horizon in self.horizons:
            if horizon.horizon_seconds == horizon_seconds:
                return horizon

        return None

    @property
    def worst(self) -> HorizonMarkout | None:
        scored = [h for h in self.horizons if h.fill_count]
        return min(scored, key=lambda h: h.per_contract_ticks) if scored else None

    def describe(self) -> str:
        if not any(horizon.fill_count for horizon in self.horizons):
            return "markout: no fills with enough forward data to measure"

        lines = ["markout (positive means the market moved our way after we traded):"]

        for horizon in self.horizons:
            if not horizon.fill_count:
                lines.append(f"  {horizon.horizon_seconds:>5.0f}s   no data")
                continue

            verdict = "ADVERSE" if horizon.is_adverse else "ok"
            lines.append(
                f"  {horizon.horizon_seconds:>5.0f}s  "
                f"{horizon.per_contract_ticks:>8.2f} ticks/contract  "
                f"{horizon.total_micros / MONEY_SCALE:>9.4f}$  "
                f"n={horizon.fill_count:<5} {verdict}"
            )

        if self.skipped_fills:
            lines.append(
                f"  ({self.skipped_fills} fill(s) too close to the end of the "
                "recording to measure)"
            )

        return "\n".join(lines)


def compute_markout(
    fills: Iterable[SimulatedFill],
    mid_series: dict[MarketTicker, MidSeries],
    *,
    horizons_seconds: Sequence[float] = DEFAULT_HORIZONS_SECONDS,
) -> MarkoutReport:
    """Markout across horizons for every fill with enough forward data."""

    totals = {horizon: 0 for horizon in horizons_seconds}
    counts = {horizon: 0 for horizon in horizons_seconds}
    contracts = {horizon: 0 for horizon in horizons_seconds}
    skipped = 0

    for fill in fills:
        series = mid_series.get(fill.market_ticker)

        if series is None:
            skipped += 1
            continue

        direction = 1 if fill.action == "buy" else -1
        measured_any = False

        for horizon in horizons_seconds:
            target = fill.offset_seconds + horizon

            if not series.covers(target):
                continue

            future_mid = series.mid_at(target)

            if future_mid is None:
                continue

            totals[horizon] += direction * (future_mid - fill.yes_price) * fill.count
            counts[horizon] += 1
            contracts[horizon] += fill.count
            measured_any = True

        if not measured_any:
            skipped += 1

    return MarkoutReport(
        horizons=tuple(
            HorizonMarkout(
                horizon_seconds=horizon,
                fill_count=counts[horizon],
                total_micros=totals[horizon],
                # totals are price_ticks * count_fp, so dividing by the summed
                # count_fp gives a size-weighted mean move in ticks.
                per_contract_ticks=(
                    totals[horizon] / contracts[horizon] if contracts[horizon] else 0.0
                ),
            )
            for horizon in horizons_seconds
        ),
        skipped_fills=skipped,
    )


@dataclass(frozen=True, slots=True)
class TimeBucketMarkout:
    """Markout for fills grouped by how long the market had left to run."""

    label: str
    lower_seconds: float
    upper_seconds: float | None
    fill_count: int
    markout_ticks_per_contract: float

    def describe(self) -> str:
        return (
            f"  {self.label:>14}  {self.markout_ticks_per_contract:>8.2f} ticks/contract  "
            f"n={self.fill_count}"
        )


DEFAULT_CLOSE_BUCKETS = (
    ("final 30s", 0.0, 30.0),
    ("30s-2m", 30.0, 120.0),
    ("2m-5m", 120.0, 300.0),
    ("5m-15m", 300.0, 900.0),
    ("over 15m", 900.0, None),
)


def markout_by_time_to_close(
    fills: Iterable[tuple[SimulatedFill, float | None]],
    mid_series: dict[MarketTicker, MidSeries],
    *,
    horizon_seconds: float = 15.0,
    buckets: Sequence[tuple[str, float, float | None]] = DEFAULT_CLOSE_BUCKETS,
) -> tuple[TimeBucketMarkout, ...]:
    """Split markout by time remaining, to see where the danger actually is.

    Each fill is paired with the seconds-to-close observed when it happened.
    Fills with no close time are dropped rather than lumped into a bucket.
    """

    grouped: dict[str, list[SimulatedFill]] = {label: [] for label, _, _ in buckets}

    for fill, seconds_to_close in fills:
        if seconds_to_close is None:
            continue

        for label, lower, upper in buckets:
            if seconds_to_close >= lower and (upper is None or seconds_to_close < upper):
                grouped[label].append(fill)
                break

    results: list[TimeBucketMarkout] = []

    for label, lower, upper in buckets:
        bucket_fills = grouped[label]
        report = compute_markout(
            bucket_fills,
            mid_series,
            horizons_seconds=(horizon_seconds,),
        )
        horizon = report.at(horizon_seconds)
        results.append(
            TimeBucketMarkout(
                label=label,
                lower_seconds=lower,
                upper_seconds=upper,
                fill_count=horizon.fill_count if horizon else 0,
                markout_ticks_per_contract=horizon.per_contract_ticks if horizon else 0.0,
            )
        )

    return tuple(results)


def describe_close_buckets(buckets: Sequence[TimeBucketMarkout]) -> str:
    measured = [bucket for bucket in buckets if bucket.fill_count]

    if not measured:
        return "markout by time to close: no fills with a known close time"

    lines = ["markout by time to close:"]
    lines.extend(bucket.describe() for bucket in measured)

    worst = min(measured, key=lambda bucket: bucket.markout_ticks_per_contract)

    if worst.markout_ticks_per_contract < 0:
        lines.append(
            f"  worst window is {worst.label} - consider stopping quoting there"
        )

    return "\n".join(lines)
