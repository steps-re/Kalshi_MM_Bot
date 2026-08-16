"""Measure the assumptions the opportunity model rests on, against real data.

    python scripts/test_assumptions.py recordings/<session> [more sessions...]

Replays each recording, measures every assumption it can from the resulting
fills, and prints a verdict per assumption plus a go / no-go on the blocking
ones. Prints what it could NOT measure just as loudly, because an assumption
nobody checked is the one that costs money.

Two of the five assumptions can only be measured from *live* fills, because
they depend on what Kalshi actually charged and on how our orders sat in a real
queue. Those show as UNMEASURED until a live session is fed in via --fills.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.analytics.competition import analyse_toxicity  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE  # noqa: E402
from kalshi_mm_bot.research.assumptions import default_ledger  # noqa: E402
from kalshi_mm_bot.research.measure import (  # noqa: E402
    capital_required,
    measure_edge_cap,
    measure_fill_rate,
    measure_maker_fee,
    measure_participation,
    measure_spread_capture,
)
from kalshi_mm_bot.sim.backtest import run_replay_backtest  # noqa: E402
from kalshi_mm_bot.sim.fills import fill_model_from_name  # noqa: E402
from kalshi_mm_bot.strategy.factory import parse_params_for, strategy_from_name  # noqa: E402
from kalshi_mm_bot.strategy.requote import RequotePolicy  # noqa: E402

HORIZON_SECONDS = 30.0


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def replay_all(args: argparse.Namespace):
    """Replay every recording and pool the fills and mid series."""

    fills = []
    mid_series: dict = {}
    orders_placed = 0
    orders_filled = 0
    our_contracts: dict[str, int] = {}

    for recording in args.recordings:
        result = await run_replay_backtest(
            recording,
            strategy=strategy_from_name(
                args.strategy,
                count=args.order_size * COUNT_SCALE,
                max_position=args.max_position * COUNT_SCALE,
                adaptive_params=parse_params_for(args.strategy, args.param),
            ),
            fill_model=fill_model_from_name(args.fill_model),
            latency_seconds=args.latency_sec,
            requote_policy=RequotePolicy(min_requote_seconds=args.min_requote_sec),
        )
        fills.extend(result.fills)
        # Later recordings would otherwise clobber earlier ones for the same
        # ticker; namespace by recording so markout looks up the right series.
        for ticker, series in result.mid_series.items():
            mid_series.setdefault(ticker, series)

        orders_placed += result.summary.order_count
        orders_filled += result.summary.fill_count

        for fill in result.fills:
            our_contracts[fill.market_ticker] = (
                our_contracts.get(fill.market_ticker, 0) + fill.count
            )

    return fills, mid_series, orders_placed, orders_filled, our_contracts


async def _run(args: argparse.Namespace) -> None:
    ledger = default_ledger()
    stamp = now_utc()

    fills, mid_series, placed, filled, our_contracts = await replay_all(args)

    print(f"Replayed {len(args.recordings)} recording(s): {len(fills)} fills\n")

    if fills:
        ledger.record(
            measure_spread_capture(
                fills, mid_series, measured_at_utc=stamp, horizon_seconds=HORIZON_SECONDS
            )
        )
        ledger.record(measure_edge_cap(fills, measured_at_utc=stamp))

    if placed:
        ledger.record(
            measure_fill_rate(
                orders_placed=placed, orders_filled=filled, measured_at_utc=stamp
            )
        )

    if args.market_volume:
        volumes = json.loads(Path(args.market_volume).read_text())
        ledger.record(
            measure_participation(our_contracts, volumes, measured_at_utc=stamp)
        )

    if args.fills:
        # Live fills: [[yes_price, count, is_taker, fees_paid_micros], ...]
        live = [tuple(row) for row in json.loads(Path(args.fills).read_text())]
        ledger.record(measure_maker_fee(live, measured_at_utc=stamp))

    print(ledger.describe())
    print()

    if fills:
        forward = _forward_mids(fills, mid_series)
        print(analyse_toxicity(fills, forward).describe())
        print()

    contracts_per_day = sum(our_contracts.values()) / COUNT_SCALE

    if contracts_per_day:
        print("capital, if this throughput ran all day:")
        for turns in (2, 4, 8, 24):
            need = capital_required(
                contracts_per_day=contracts_per_day, turns_per_day=turns
            )
            print(f"  recycling {turns:>2}x/day -> ${need:,.0f} of collateral")


def _forward_mids(fills, mid_series) -> dict[str, int]:
    forward: dict[str, int] = {}

    for fill in fills:
        series = mid_series.get(fill.market_ticker)

        if series is None:
            continue

        target = fill.offset_seconds + HORIZON_SECONDS

        if not series.covers(target):
            continue

        mid = series.mid_at(target)

        if mid is not None:
            forward[fill.fill_id] = mid

    return forward


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", nargs="+", help="Recording directories to replay.")
    parser.add_argument("--strategy", default="horizon")
    parser.add_argument("--fill-model", default="queue")
    parser.add_argument("--order-size", type=int, default=10)
    parser.add_argument("--max-position", type=int, default=50)
    parser.add_argument("--latency-sec", type=float, default=0.15)
    parser.add_argument("--min-requote-sec", type=float, default=0.5)
    parser.add_argument("--param", action="append")
    parser.add_argument(
        "--fills",
        help="JSON of live fills [[yes_price, count, is_taker, fees_paid_micros]] "
        "to measure the maker fee. This is the blocking one.",
    )
    parser.add_argument(
        "--market-volume",
        help="JSON {ticker: contracts} of market volume over the same window, "
        "to measure participation.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
