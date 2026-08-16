"""Replay a recording and print the full desk report.

    python scripts/analyze_session.py recordings/2026-08-16T18-00-00Z
    python scripts/analyze_session.py rec1 rec2 rec3 --walk-forward

Single recording: net P&L, where it came from, markout by horizon and by time
to close, and drawdown. Several recordings with --walk-forward: fit on the
earlier ones, score on the later ones, and report the gap.

Read the markout before the P&L. On a ten minute session the P&L is noise and
the markout is not.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.analytics.markout import (  # noqa: E402
    compute_markout,
    describe_close_buckets,
    markout_by_time_to_close,
)
from kalshi_mm_bot.analytics.performance import build_performance_report  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE  # noqa: E402
from kalshi_mm_bot.sim.backtest import BacktestResult, run_replay_backtest  # noqa: E402
from kalshi_mm_bot.sim.fees import KalshiFeeModel  # noqa: E402
from kalshi_mm_bot.sim.fills import fill_model_from_name  # noqa: E402
from kalshi_mm_bot.sim.validation import walk_forward  # noqa: E402
from kalshi_mm_bot.strategy.factory import parse_params_for, strategy_from_name  # noqa: E402
from kalshi_mm_bot.strategy.requote import RequotePolicy  # noqa: E402


def print_report(result: BacktestResult, fee_model: KalshiFeeModel) -> None:
    summary = result.summary
    final_mid = _final_mid(result)

    print(f"=== {result.recording.name} ===")
    print(f"strategy={summary.strategy_name} fills={summary.fill_count} "
          f"events={summary.event_count}")
    print()
    print(f"  gross (before fees) {_money(summary.gross_mark_to_market_value)}")
    print(f"  fees paid           {_money(summary.fees_paid)}")
    print(f"  mark to market      {_money(summary.mark_to_market_value)}")
    print(f"  net liquidation     {_money(summary.net_liquidation_value)}")

    if summary.inventory_mark_gap:
        print(f"  mid-vs-exit gap     {_money(summary.inventory_mark_gap)} "
              f"(ends holding {summary.position_count / COUNT_SCALE:+.2f} contracts)")

    if not summary.unwindable_at_touch:
        print("  WARNING: leftover inventory has no touch to exit into")

    print()

    report = build_performance_report(
        result.fills,
        result.equity_curve,
        final_position=summary.position_count,
        final_mid=final_mid,
        fees_paid=summary.fees_paid,
        fee_model=fee_model,
    )
    print(report.describe())
    print()
    print(compute_markout(result.fills, result.mid_series).describe())
    print()
    print(
        describe_close_buckets(
            markout_by_time_to_close(
                [(fill, fill.seconds_to_close) for fill in result.fills],
                result.mid_series,
            )
        )
    )
    print()


async def _single(args: argparse.Namespace, fee_model: KalshiFeeModel) -> None:
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
            fee_model=fee_model,
        )
        print_report(result, fee_model)


async def _walk_forward(args: argparse.Namespace, fee_model: KalshiFeeModel) -> None:
    result = await walk_forward(
        args.recordings,
        count=args.order_size * COUNT_SCALE,
        max_position=args.max_position * COUNT_SCALE,
        fill_model_factory=lambda: fill_model_from_name(args.fill_model),
        strategy_name=args.strategy,
        latency_seconds=args.latency_sec,
        requote_policy=RequotePolicy(min_requote_seconds=args.min_requote_sec),
        fee_model=fee_model,
        on_progress=print,
    )
    print()
    print(result.describe())
    print()

    ratio = result.overfit_ratio

    if ratio is None:
        print("in-sample total was not positive - nothing to retain out of sample")
    elif ratio < 0.3:
        print(
            "out-of-sample retention is low: the in-sample result is mostly "
            "fitted noise, not edge"
        )


async def _run(args: argparse.Namespace) -> None:
    fee_model = KalshiFeeModel(trading_fee_bps=args.fee_bps)

    if args.walk_forward:
        await _walk_forward(args, fee_model)
    else:
        await _single(args, fee_model)


def _final_mid(result: BacktestResult) -> int | None:
    """Last mid we actually observed, from the recorded series.

    `final_rows` is pre-formatted display text, not numbers.
    """

    for series in result.mid_series.values():
        if series.mids:
            return series.mids[-1]

    return None


def _money(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    micros = abs(micros)
    return f"{sign}${micros // MONEY_SCALE}.{micros % MONEY_SCALE:06d}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", nargs="+", help="Recording directories.")
    parser.add_argument("--strategy", default="horizon")
    parser.add_argument("--fill-model", default="queue", help="optimistic, pessimistic or queue.")
    parser.add_argument("--order-size", type=int, default=10, help="Contracts per quote.")
    parser.add_argument("--max-position", type=int, default=50, help="Contract position cap.")
    parser.add_argument("--latency-sec", type=float, default=0.15)
    parser.add_argument("--min-requote-sec", type=float, default=0.5)
    parser.add_argument("--fee-bps", type=int, default=700)
    parser.add_argument("--param", action="append", help="Strategy override key=value.")
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Fit on earlier recordings and score on later ones.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
