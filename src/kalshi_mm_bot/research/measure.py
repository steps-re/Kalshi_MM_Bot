"""Turn real fills into measurements the assumption ledger can judge.

Each function here answers exactly one assumption, from data we already
collect. Nothing estimates; if the data cannot answer the question the function
returns a measurement with a small sample size and lets the ledger call it
INSUFFICIENT, rather than returning a confident number built from nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from kalshi_mm_bot.market.fees import KalshiFeeModel, calibrate_from_fills
from kalshi_mm_bot.market.price import COUNT_SCALE
from kalshi_mm_bot.market.series import MidSeries
from kalshi_mm_bot.research.assumptions import Measurement
from kalshi_mm_bot.sim.fills import SimulatedFill

CAPTURE_HORIZON_SECONDS = 30.0

# Quoting both sides of one contract posts P on the buy and (1 - P) on the
# sell, so a contract-pair ties up one dollar whatever the price.
DOLLARS_PER_CONTRACT_PAIR = 1.0


def measure_maker_fee(
    fills: Sequence[tuple[int, int, bool, int]],
    *,
    measured_at_utc: str,
    model: KalshiFeeModel | None = None,
) -> Measurement:
    """Average fee actually charged per contract on resting fills.

    Each entry is `(yes_price, count, is_taker, actual_fee_micros)` - exactly
    what an `OrderFill` plus the account's reported `fees_paid` provide. Only
    maker fills count, because the assumption is about resting quotes.
    """

    maker = [f for f in fills if not f[2]]

    if not maker:
        return Measurement(
            key="maker_fee",
            observed=0.0,
            sample_size=0,
            measured_at_utc=measured_at_utc,
            note="no maker fills in the sample",
        )

    contracts = sum(count for _, count, _, _ in maker) / COUNT_SCALE
    charged = sum(fee for _, _, _, fee in maker)
    per_contract = charged / contracts if contracts else 0.0

    calibration = calibrate_from_fills(model or KalshiFeeModel(), maker)

    return Measurement(
        key="maker_fee",
        observed=per_contract,
        sample_size=len(maker),
        measured_at_utc=measured_at_utc,
        note=calibration.describe(),
    )


def measure_spread_capture(
    fills: Sequence[SimulatedFill],
    mid_series: dict[str, MidSeries],
    *,
    measured_at_utc: str,
    horizon_seconds: float = CAPTURE_HORIZON_SECONDS,
) -> Measurement:
    """Fraction of the quoted edge we still hold `horizon_seconds` after a fill.

    Quoted edge is how far our fill price sat from the mid at the time; realised
    edge is how far it sits from the mid once the market has had time to move
    against us. Their ratio is the capture rate the opportunity model assumes,
    and it is the number that adverse selection destroys.

    Clamped at zero: a fill that ends up underwater captured none of the spread,
    not a negative fraction of it, and letting those go negative would let a few
    disasters cancel out many small wins and report a flattering mean.
    """

    quoted = 0.0
    realised = 0.0
    counted = 0

    for fill in fills:
        if fill.mid_at_fill is None:
            continue

        series = mid_series.get(fill.market_ticker)

        if series is None:
            continue

        target = fill.offset_seconds + horizon_seconds

        if not series.covers(target):
            continue

        future_mid = series.mid_at(target)

        if future_mid is None:
            continue

        direction = 1 if fill.action == "buy" else -1
        edge_at_fill = direction * (fill.mid_at_fill - fill.yes_price)

        if edge_at_fill <= 0:
            # We crossed rather than captured; not evidence about spread capture.
            continue

        edge_after = direction * (future_mid - fill.yes_price)
        quoted += edge_at_fill * fill.count
        realised += max(0.0, edge_after) * fill.count
        counted += 1

    observed = (realised / quoted) if quoted > 0 else 0.0

    return Measurement(
        key="spread_capture",
        observed=observed,
        sample_size=counted,
        measured_at_utc=measured_at_utc,
        note=f"{horizon_seconds:g}s horizon over {counted} maker-side fills",
    )


def measure_participation(
    our_contracts_by_ticker: dict[str, int],
    market_volume_by_ticker: dict[str, int],
    *,
    measured_at_utc: str,
) -> Measurement:
    """Our share of traded volume in the markets we quoted.

    Reported as the median across markets rather than the aggregate, because
    one market where we were the only participant would otherwise carry the
    whole figure.
    """

    shares = [
        our / market_volume_by_ticker[ticker]
        for ticker, our in our_contracts_by_ticker.items()
        if market_volume_by_ticker.get(ticker, 0) > 0
    ]

    return Measurement(
        key="participation",
        observed=median(shares) if shares else 0.0,
        sample_size=len(shares),
        measured_at_utc=measured_at_utc,
        note=f"median across {len(shares)} market(s)",
    )


def measure_edge_cap(
    fills: Sequence[SimulatedFill],
    *,
    measured_at_utc: str,
) -> Measurement:
    """Realised edge on the best decile of round trips, in cents.

    Answers whether wide markets are more harvestable than the cap assumes. If
    the top decile is comfortably above the cap, the model is leaving money on
    the table; if it is below, the cap is already generous.
    """

    edges = sorted(
        (
            (1 if fill.action == "buy" else -1)
            * (fill.mid_at_fill - fill.yes_price)
            / 100
            for fill in fills
            if fill.mid_at_fill is not None
        ),
        reverse=True,
    )

    if not edges:
        return Measurement(
            key="edge_cap",
            observed=0.0,
            sample_size=0,
            measured_at_utc=measured_at_utc,
            note="no fills with a recorded mid",
        )

    decile = edges[: max(1, len(edges) // 10)]

    return Measurement(
        key="edge_cap",
        observed=sum(decile) / len(decile),
        sample_size=len(edges),
        measured_at_utc=measured_at_utc,
        note=f"mean of the top decile of {len(edges)} fills",
    )


def measure_fill_rate(
    *,
    orders_placed: int,
    orders_filled: int,
    measured_at_utc: str,
) -> Measurement:
    """How often a resting quote actually trades. Drives capital turnover."""

    observed = (orders_filled / orders_placed) if orders_placed else 0.0

    return Measurement(
        key="fill_rate",
        observed=observed,
        sample_size=orders_placed,
        measured_at_utc=measured_at_utc,
        note=f"{orders_filled} filled of {orders_placed} placed",
    )


def capital_required(
    *,
    contracts_per_day: float,
    turns_per_day: float,
) -> float:
    """Collateral needed to sustain a daily throughput, in dollars.

    Kalshi is fully collateralised: a resting buy at P posts P and a resting
    sell posts (1 - P), so quoting both sides of one contract ties up about a
    dollar regardless of where the market is priced. Capital is therefore set by
    how many contracts are outstanding at once - throughput divided by how many
    times a day the capital recycles - not by the notional traded.

    Args:
        contracts_per_day: Whole contracts traded per day (buys plus sells).
        turns_per_day: How many times the same dollar is redeployed in a day.
            Measure it as contracts traded divided by peak contracts held.
    """

    if turns_per_day <= 0:
        raise ValueError("turns_per_day must be positive")

    return contracts_per_day / turns_per_day * DOLLARS_PER_CONTRACT_PAIR
