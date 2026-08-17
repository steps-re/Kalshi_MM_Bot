"""Run every strategy over every recording and rank them on what they earned.

    python scripts/sweep_backtests.py data/book
    python scripts/sweep_backtests.py data/book --strategies adaptive horizon

One backtest is an anecdote. A strategy that captures 3.7c a fill in one
fifteen-minute window has told you about that window, and the ranking from a
single run inverted the moment P&L was attributed - so it can invert again.

This runs the matrix and aggregates on **spread capture**, not on mark to
market. That choice is the entire point:

* Mark to market answers "did this run make money", which for a strategy
  finishing at its position limit is a question about the price path.
* Spread capture answers "did the quoting earn anything", which is the only
  part a market maker controls and the only part that should generalise from
  one window to the next.

A strategy is ranked by capture per fill rather than total capture, because
total capture rewards whichever strategy happened to trade the most, and a
strategy can always trade more by quoting worse.

Residual inventory is reported alongside. A run that ends flat earned its
number; a run that ends at its position limit has taken a directional bet that
the risk control merely bounded, and its P&L belongs to the price rather than
to the quoting.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics as st
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.analytics.performance import attribute_pnl  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE, parse_count_fp  # noqa: E402
from kalshi_mm_bot.sim import fill_model_from_name, run_replay_backtest  # noqa: E402
from kalshi_mm_bot.strategy import strategy_from_name  # noqa: E402

STRATEGIES = ("adaptive", "horizon", "dumb")


@dataclass(frozen=True, slots=True)
class Run:
    recording: str
    strategy: str
    fills: int
    spread_capture: int
    inventory: int
    net: int
    residual_contracts: float
    markets_held: int

    @property
    def capture_per_fill(self) -> float:
        return self.spread_capture / self.fills if self.fills else 0.0

    @property
    def residual_per_market(self) -> float:
        """Contracts left per market that was actually traded.

        Summing across tickers made a strategy holding eight contracts in each
        of ten markets read as eighty - indistinguishable from one sitting at a
        single market's limit, which is the thing this metric exists to catch.
        """

        return self.residual_contracts / max(1, self.markets_held)


async def one_run(
    recording: Path, strategy_name: str, args: argparse.Namespace
) -> Run | None:
    try:
        result = await run_replay_backtest(
            recording,
            strategy=strategy_from_name(
                strategy_name,
                count=parse_count_fp(args.order_size),
                max_position=parse_count_fp(args.max_position),
            ),
            fill_model=fill_model_from_name(args.fill_model),
            speed_multiplier=0,
        )
    except Exception as error:
        print(f"  {recording.name}/{strategy_name}: {type(error).__name__}: {error}")
        return None

    marks = {
        ticker: (position, result.final_mids_by_ticker.get(ticker))
        for ticker, position in result.positions_by_ticker.items()
    }
    attribution = attribute_pnl(
        result.fills, fees_paid=result.summary.fees_paid, final_marks=marks
    )

    return Run(
        recording=recording.name,
        strategy=strategy_name,
        fills=len(result.fills),
        spread_capture=attribution.spread_capture,
        inventory=attribution.inventory_pnl,
        net=attribution.net,
        residual_contracts=sum(abs(p) for p in result.positions_by_ticker.values())
        / COUNT_SCALE,
        markets_held=sum(1 for p in result.positions_by_ticker.values() if p),
    )


def report(runs: list[Run]) -> str:
    by_strategy: dict[str, list[Run]] = {}

    for run in runs:
        by_strategy.setdefault(run.strategy, []).append(run)

    lines = [
        f"{'strategy':<10}{'runs':>6}{'fills':>8}{'capture $':>12}"
        f"{'per fill':>11}{'inventory $':>12}{'resid/mkt':>11}{'flat runs':>11}"
    ]

    ranked = sorted(
        by_strategy.items(),
        key=lambda kv: -(
            sum(r.spread_capture for r in kv[1]) / max(1, sum(r.fills for r in kv[1]))
        ),
    )

    for strategy, group in ranked:
        fills = sum(r.fills for r in group)
        capture = sum(r.spread_capture for r in group)
        # A run is "flat" if it ends holding under one contract - that is the
        # difference between a market maker and a directional bet the risk
        # limit happened to cap.
        flat = sum(1 for r in group if r.residual_per_market < 1.0)
        lines.append(
            f"{strategy:<10}{len(group):>6}{fills:>8}"
            f"{capture / MONEY_SCALE:>11.2f}$"
            f"{(capture / fills / MONEY_SCALE * 100 if fills else 0):>10.2f}c"
            f"{sum(r.inventory for r in group) / MONEY_SCALE:>11.2f}$"
            f"{st.median([r.residual_per_market for r in group]):>11.1f}"
            f"{flat:>7}/{len(group):<3}"
        )

    lines.append("")
    lines.append(
        "Ranked on capture per fill, which is the part the quoting earns. Total "
        "capture would reward whichever strategy traded most, and any strategy "
        "can trade more by quoting worse."
    )
    lines.append(
        "Residual is contracts still held at the end. A run that ends at its "
        "position limit did not make a market, it took a position - its P&L "
        "belongs to the price path, not to the strategy."
    )
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> None:
    recordings = sorted(
        d for d in args.book.iterdir() if (d / "manifest.json").exists()
    )

    if not recordings:
        raise SystemExit(f"no recordings with a manifest under {args.book}")

    print(f"{len(recordings)} recording(s) x {len(args.strategies)} strategies")
    runs: list[Run] = []

    for recording in recordings:
        for strategy in args.strategies:
            run = await one_run(recording, strategy, args)

            if run is not None:
                runs.append(run)
                print(
                    f"  {recording.name[-16:]:<17}{strategy:<10}"
                    f"fills={run.fills:<6} capture=${run.spread_capture / MONEY_SCALE:>8.4f} "
                    f"resid/mkt={run.residual_per_market:>6.2f}"
                )

    if not runs:
        raise SystemExit("every run failed")

    print("")
    print(report(runs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book", type=Path, help="Directory of recording directories.")
    parser.add_argument("--strategies", nargs="+", default=list(STRATEGIES))
    parser.add_argument("--fill-model", default="queue")
    parser.add_argument("--order-size", default="5")
    parser.add_argument("--max-position", default="50")
    args = parser.parse_args()

    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
