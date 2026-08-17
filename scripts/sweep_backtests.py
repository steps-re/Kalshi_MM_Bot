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

**Neither total capture nor capture-per-fill is a safe ranking on its own, and
this tool learned that the hard way.** Total capture rewards whichever strategy
traded most, and any strategy can trade more by quoting worse. Capture per fill
is the mirror image: any strategy can raise it by quoting wider and trading
less. Ranking horizon on per-fill picked a configuration earning a third as much
as the one next to it.

So the ranking is on total capture - the money actually earned by quoting - and
per-fill is reported beside it as the quality signal. A configuration with a
high per-fill and very few fills is flagged rather than promoted, because a
handful of fills does not distinguish a good quote from a lucky one.

Residual inventory is reported alongside. A run that ends flat earned its
number; a run that ends at its position limit has taken a directional bet that
the risk control merely bounded, and its P&L belongs to the price rather than
to the quoting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
from kalshi_mm_bot.strategy.factory import parse_params_for  # noqa: E402

STRATEGIES = ("adaptive", "horizon", "dumb")

# Below this many fills across the whole book, a per-fill figure says more about
# luck than about the quote.
MIN_FILLS_FOR_CONFIDENCE = 200

# Book deltas per second per ticker, below which a recording cannot support a
# conclusion about fill rates. Measured on one KXBTC15M window captured both
# ways at once: the websocket feed carried 300,562 deltas and 152.9M contracts
# of level shrinkage, while REST polling of the same market over the same window
# carried 14,122 deltas and 18.0M of shrinkage - **11.8% of the real thing**.
# The same strategy filled 942 times on the feed and 13 times on the polled copy.
#
# Polling reports the NET change per interval, so a level that trades and
# refills between two samples is invisible. The fill model consumes queue from
# observed reductions, so hiding 88% of them means a resting order essentially
# never reaches the front, and every strategy looks like it cannot trade.
# Polling faster helps a little; it does not fix the netting.
MIN_DELTAS_PER_SECOND = 50.0


@dataclass(frozen=True, slots=True)
class Resolution:
    """How finely a recording saw the book, which bounds what it can prove."""

    recording: str
    deltas: int
    seconds: float
    tickers: int

    @property
    def deltas_per_second_per_ticker(self) -> float:
        span = max(1.0, self.seconds) * max(1, self.tickers)
        return self.deltas / span

    @property
    def is_thin(self) -> bool:
        return self.deltas_per_second_per_ticker < MIN_DELTAS_PER_SECOND


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


def measure_resolution(recording: Path) -> Resolution | None:
    """Count book deltas and the span they cover, without replaying."""

    events = recording / "events.jsonl"

    if not events.exists():
        return None

    deltas = 0
    tickers: set[str] = set()
    first = last = None

    for line in events.open():
        try:
            row = json.loads(line)
        except ValueError:
            continue

        message = row.get("msg") or {}
        inner = message.get("msg") or {}
        ticker = inner.get("market_ticker")

        if ticker:
            tickers.add(str(ticker))

        if message.get("type") != "orderbook_delta":
            continue

        deltas += 1
        offset = row.get("offset_seconds")

        if isinstance(offset, (int, float)):
            first = offset if first is None else min(first, offset)
            last = offset if last is None else max(last, offset)

    return Resolution(
        recording=recording.name,
        deltas=deltas,
        seconds=(last - first) if (first is not None and last is not None) else 0.0,
        tickers=len(tickers),
    )


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
                adaptive_params=parse_params_for(strategy_name, args.param),
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


def resolution_report(resolutions: list[Resolution]) -> str:
    thin = [r for r in resolutions if r.is_thin]

    if not thin:
        return ""

    worst = min(thin, key=lambda r: r.deltas_per_second_per_ticker)
    lines = [
        f"!! {len(thin)} of {len(resolutions)} recording(s) are below "
        f"{MIN_DELTAS_PER_SECOND:.0f} book deltas/sec/ticker - the thinnest is "
        f"{worst.recording} at {worst.deltas_per_second_per_ticker:.1f}.",
        "   These are REST-polled books. Polling reports the net change per",
        "   interval, so a level that trades and refills between samples is",
        "   invisible: measured against a websocket capture of the same window,",
        "   polling carried 11.8% of the real shrinkage and the same strategy",
        "   filled 13 times instead of 942.",
        "   Fill counts and totals below are therefore floors, not estimates.",
        "   The RANKING may survive, since every strategy is starved equally, but",
        "   any parameter that controls how often we trade is being fitted to the",
        "   artifact rather than to the market.",
        "",
    ]
    return "\n".join(lines)


def report(runs: list[Run]) -> str:
    by_strategy: dict[str, list[Run]] = {}

    for run in runs:
        by_strategy.setdefault(run.strategy, []).append(run)

    lines = [
        f"{'strategy':<10}{'runs':>6}{'fills':>8}{'capture $':>12}"
        f"{'per fill':>11}{'inventory $':>12}{'resid/mkt':>11}{'flat runs':>11}"
    ]

    # Rank on money earned, not on the per-fill rate. See the module docstring:
    # per-fill is trivially gamed by quoting wider and trading less.
    ranked = sorted(
        by_strategy.items(),
        key=lambda kv: -sum(r.spread_capture for r in kv[1]),
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

    thin = [
        strategy
        for strategy, group in by_strategy.items()
        if sum(r.fills for r in group) < MIN_FILLS_FOR_CONFIDENCE
    ]

    lines.append("")

    if thin:
        lines.append(
            f"!! {', '.join(thin)}: under {MIN_FILLS_FOR_CONFIDENCE} fills across the "
            "whole book. A high per-fill figure on a sample this thin is not "
            "evidence of a better quote, only of a rarer one."
        )
        lines.append("")

    lines.append(
        "Ranked on total spread capture - the money the quoting earned. Per fill "
        "is shown beside it as a quality signal, NOT as the ranking: it is raised "
        "just as easily by quoting wider and trading less as by quoting better."
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

    tag = f" [{args.label}]" if args.label else ""
    overrides = f" {args.param}" if args.param else ""
    print(
        f"{len(recordings)} recording(s) x {len(args.strategies)} strategies"
        f"{tag}{overrides}"
    )
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

    resolutions = [r for r in (measure_resolution(d) for d in recordings) if r]

    print("")
    warning = resolution_report(resolutions)

    if warning:
        print(warning)

    print(report(runs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book", type=Path, help="Directory of recording directories.")
    parser.add_argument("--strategies", nargs="+", default=list(STRATEGIES))
    parser.add_argument("--fill-model", default="queue")
    parser.add_argument("--order-size", default="5")
    parser.add_argument("--max-position", default="50")
    parser.add_argument(
        "--param",
        action="append",
        help="Strategy parameter override, KEY=VALUE. Repeatable. Applies to "
        "whichever strategies accept it, so a sweep can compare one knob across "
        "the whole book instead of one lucky window.",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Tag for this configuration, printed with the summary so several "
        "sweeps can be compared by eye.",
    )
    args = parser.parse_args()

    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
