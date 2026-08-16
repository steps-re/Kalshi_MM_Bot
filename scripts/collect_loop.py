"""Continuously record the markets most likely to be worth quoting, to GCS.

    python scripts/collect_loop.py --bucket steps-nate-backtest-data --cycle-min 20

Designed to run unattended on a VM for days. Each cycle re-picks its targets by
measured churn, records for a fixed window, uploads, and starts again. Re-picking
every cycle is the point: activity on Kalshi moves between series through the
day - crypto ladders overnight, sports in the afternoon, the near-dated strikes
of whatever is about to expire - and a fixed ticker list would spend most of the
week recording frozen books. The first attempt at this recorded twelve political
markets for 25 minutes and produced one fill.

Selection deliberately spans several families rather than backing one. Two
things drive whether a market is worth quoting - how much its book moves, and
whether its spread covers the fee at its price - and they favour different
markets, so the collector samples across the space rather than assuming which
combination wins.

Failures never stop the loop. A cycle that dies from a network blip logs and the
next one starts; the run only ends when it is told to.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

sys.path.insert(0, str(ROOT / "scripts"))

import poll_record as pr  # noqa: E402

from kalshi_mm_bot.analytics.screening import parse_market, score_market  # noqa: E402
from kalshi_mm_bot.market.fees import KalshiFeeModel  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE  # noqa: E402

# Families worth sampling. Crypto ladders churn hardest; the others are here so
# the dataset is not a single regime, and because the fee screen likes wide
# spreads which crypto rarely has.
DEFAULT_SERIES = (
    "KXBTCD",
    "KXETHD",
    "KXNBAGAME",
    "KXNFLGAME",
    "KXMLBGAME",
    "KXHIGHNY",
    "KXHIGHCHI",
)
FEE_MODEL = KalshiFeeModel()
SCORE_SIZE = 50 * COUNT_SCALE


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def candidate_markets(series: tuple[str, ...], min_volume: float) -> list[dict]:
    """Two-sided markets from the named series, plus the busiest events."""

    seen: dict[str, dict] = {}

    for series_ticker in series:
        try:
            data = pr.get(
                "/markets",
                {"status": "open", "limit": 1000, "series_ticker": series_ticker},
            )
        except Exception as error:
            log(f"  series {series_ticker}: {type(error).__name__} {error}")
            continue

        for market in data.get("markets", []):
            bid = pr._num(market.get("yes_bid_dollars"))
            ask = pr._num(market.get("yes_ask_dollars"))

            if 0 < bid < ask < 1 and pr._num(market.get("volume_24h_fp")) >= min_volume:
                seen[market["ticker"]] = market

    return list(seen.values())


def rank_candidates(markets: list[dict], shortlist: int) -> list[str]:
    """Shortlist on fee-viability, then rank what survives by measured churn.

    Churn alone would pick the busiest markets whether or not their spread can
    ever cover the fee; the fee screen alone would pick wide, frozen markets
    that never trade. A market has to pass both to be worth a recording slot.
    """

    scored: list[tuple[int, str]] = []

    for raw in markets:
        quote = parse_market(raw)

        if quote is None or not quote.is_quotable:
            continue

        score = score_market(quote, fee_model=FEE_MODEL, assumed_size=SCORE_SIZE)
        # Keep near-misses: a market a cent from viable today may be viable in
        # an hour, and excluding them would bias the dataset toward easy cases.
        if score.net_edge_ticks > -200:
            scored.append((score.net_edge_ticks, quote.ticker))

    scored.sort(reverse=True)
    return [ticker for _, ticker in scored[:shortlist]]


def run_cycle(args: argparse.Namespace, cycle: int) -> Path | None:
    log(f"cycle {cycle}: selecting targets")
    markets = candidate_markets(tuple(args.series), args.min_volume)

    if not markets:
        log("  no candidates; sleeping before retry")
        return None

    shortlisted = rank_candidates(markets, args.shortlist)
    log(f"  {len(markets)} candidates -> {len(shortlisted)} fee-plausible; probing churn")

    tickers = pr._rank_by_churn(shortlisted, args.markets, gap_seconds=args.probe_gap)

    if not tickers:
        log("  nothing moving; sleeping before retry")
        return None

    out = Path(args.workdir) / f"rec_{now_stamp()}"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "poll_record.py"),
        *tickers,
        "--interval",
        str(args.interval),
        "--duration-sec",
        str(args.cycle_min * 60),
        "--output",
        str(out),
    ]
    log(f"  recording {len(tickers)} markets for {args.cycle_min}m -> {out.name}")
    subprocess.run(command, check=True, cwd=str(ROOT))
    return out


def upload(directory: Path, bucket: str) -> None:
    destination = f"gs://{bucket}/recordings/{directory.name}"
    subprocess.run(
        ["gcloud", "storage", "cp", "-r", str(directory), destination],
        check=True,
    )
    log(f"  uploaded -> {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="steps-nate-backtest-data")
    parser.add_argument("--workdir", default="/var/tmp/kalshi-recordings")
    parser.add_argument("--series", action="append", default=list(DEFAULT_SERIES))
    parser.add_argument("--markets", type=int, default=12, help="Markets per cycle.")
    parser.add_argument("--shortlist", type=int, default=60, help="Churn-probe pool size.")
    parser.add_argument("--min-volume", type=float, default=200.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--cycle-min", type=int, default=20)
    parser.add_argument("--probe-gap", type=float, default=8.0)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means run forever.")
    parser.add_argument("--keep-local", action="store_true")
    args = parser.parse_args()

    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    cycle = 0

    while not args.max_cycles or cycle < args.max_cycles:
        cycle += 1

        try:
            directory = run_cycle(args, cycle)

            if directory is None:
                time.sleep(120)
                continue

            events = directory / "events.jsonl"
            count = sum(1 for _ in events.open()) if events.exists() else 0
            log(f"  {count} events")

            if count < 50:
                log("  too thin to keep; discarding")
            elif args.bucket:
                upload(directory, args.bucket)

            if not args.keep_local:
                subprocess.run(["rm", "-rf", str(directory)], check=False)
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception:
            # A cycle dying must never end the run.
            log("cycle failed:\n" + traceback.format_exc())
            time.sleep(60)

    log(f"done after {cycle} cycle(s)")


if __name__ == "__main__":
    main()
