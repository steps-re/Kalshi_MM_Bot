"""Walk-forward optimisation over every recording collected so far.

    python scripts/optimize_loop.py --sync --bucket steps-nate-backtest-data

Pulls recordings from GCS, fits parameters on earlier sessions, scores them on
later ones the fit never saw, and reports both. Designed to be re-run as data
accumulates, so the interesting output is the trend: does out-of-sample
retention improve as the dataset grows, or does it stay at noise?

The result this is built to deliver is a *negative* one as readily as a positive
one. If out-of-sample net is around zero after a few days of data, that is the
answer, and it is worth more than another week of tuning. The report says so
explicitly rather than presenting the best in-sample row and letting the reader
draw the flattering conclusion.

Every run also prints the baseline. A strategy that has not beaten `dumb` has
not been shown to do anything, however good its own number looks.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.market.fees import KalshiFeeModel  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE  # noqa: E402
from kalshi_mm_bot.sim.backtest import run_replay_backtest  # noqa: E402
from kalshi_mm_bot.sim.fills import fill_model_from_name  # noqa: E402
from kalshi_mm_bot.sim.validation import walk_forward  # noqa: E402
from kalshi_mm_bot.strategy.factory import strategy_from_name  # noqa: E402
from kalshi_mm_bot.strategy.requote import RequotePolicy  # noqa: E402

TAKER = KalshiFeeModel()
MAKER_FREE = KalshiFeeModel(charge_makers_taker_rate=False, maker_fee_per_contract_micros=0)

# Deliberately coarse. With a few hundred fills per session, a fine grid finds
# noise; these are the parameters with a mechanism behind them.
SEARCH_SPACE = {
    "min_profit_edge": (0, 25, 50, 100),
    "adverse_selection_bps": (0, 10_000, 20_000),
    "inventory_skew": (200, 500, 1_000),
    "max_quote_away": (100, 300),
}


def sync_recordings(bucket: str, local: Path) -> None:
    local.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gcloud", "storage", "rsync", "-r", f"gs://{bucket}/recordings", str(local)],
        check=True,
    )


def find_recordings(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.rglob("manifest.json"))


async def baseline(recordings: list[Path], args: argparse.Namespace) -> None:
    """Every strategy over every recording, under both fee assumptions."""

    print(f"\n{'strategy':<10}{'fee':<20}{'fills':>7}{'gross $':>10}{'fees $':>9}{'NET $':>10}")

    for name in ("horizon", "adaptive", "dumb"):
        for label, fee in (("taker both sides", TAKER), ("maker pays nothing", MAKER_FREE)):
            fills = gross = fees = net = 0.0

            for recording in recordings:
                try:
                    result = await run_replay_backtest(
                        recording,
                        strategy=strategy_from_name(
                            name,
                            count=args.order_size * COUNT_SCALE,
                            max_position=args.max_position * COUNT_SCALE,
                        ),
                        fill_model=fill_model_from_name(args.fill_model),
                        latency_seconds=args.latency_sec,
                        requote_policy=RequotePolicy(min_requote_seconds=args.min_requote_sec),
                        fee_model=fee,
                    )
                except Exception as error:
                    print(f"  {recording.name}: {type(error).__name__}")
                    continue

                summary = result.summary
                fills += summary.fill_count
                gross += summary.gross_mark_to_market_value / MONEY_SCALE
                fees += summary.fees_paid / MONEY_SCALE
                net += summary.net_liquidation_value / MONEY_SCALE

            print(f"{name:<10}{label:<20}{fills:>7.0f}{gross:>10.2f}{fees:>9.2f}{net:>10.2f}")


async def _run(args: argparse.Namespace) -> None:
    local = Path(args.local)

    if args.sync:
        print(f"Syncing gs://{args.bucket}/recordings -> {local}")
        sync_recordings(args.bucket, local)

    recordings = find_recordings(local)
    print(f"{len(recordings)} recording(s) available")

    if not recordings:
        raise SystemExit("nothing to optimise yet")

    await baseline(recordings, args)

    if len(recordings) < 3:
        print(
            "\nWalk-forward needs at least 3 recordings to have anything to hold "
            f"out; have {len(recordings)}. Collect more before believing any "
            "parameter set."
        )
        return

    print("\nWalk-forward (fit on earlier sessions, score on later ones):")
    result = await walk_forward(
        recordings,
        count=args.order_size * COUNT_SCALE,
        max_position=args.max_position * COUNT_SCALE,
        fill_model_factory=lambda: fill_model_from_name(args.fill_model),
        search_space=SEARCH_SPACE,
        strategy_name=args.strategy,
        latency_seconds=args.latency_sec,
        requote_policy=RequotePolicy(min_requote_seconds=args.min_requote_sec),
        fee_model=MAKER_FREE if args.assume_maker_free else TAKER,
        on_progress=print,
    )

    print()
    print(result.describe())
    print()

    ratio = result.overfit_ratio
    out = result.total_out_of_sample / MONEY_SCALE

    if ratio is None:
        print(
            "In-sample total was not positive. There is no edge to retain yet - "
            "this is a result, not a setback."
        )
    elif ratio < 0.3:
        print(
            f"Out-of-sample retention {ratio:.0%}: the in-sample gain is mostly "
            "fitted noise. More tuning will not fix that; more data or a "
            "different mechanism might."
        )
    else:
        print(f"Out-of-sample retention {ratio:.0%} on ${out:,.2f} - worth continuing.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="steps-nate-backtest-data")
    parser.add_argument("--local", default="data/recordings")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--strategy", default="horizon")
    parser.add_argument("--fill-model", default="queue")
    parser.add_argument("--order-size", type=int, default=10)
    parser.add_argument("--max-position", type=int, default=50)
    parser.add_argument("--latency-sec", type=float, default=0.15)
    parser.add_argument("--min-requote-sec", type=float, default=0.5)
    parser.add_argument(
        "--assume-maker-free",
        action="store_true",
        help="Optimise under a zero maker fee. Until calibrate_from_fills has "
        "run against real executions, this is a hypothesis, not a setting.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
