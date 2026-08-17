"""Continuously record the markets most likely to be worth quoting, to GCS.

**Read this before backtesting anything recorded here.** This collector polls
REST, and polling reports the NET change to a price level between two samples.
A level that trades and is refilled in between is invisible. Measured against a
websocket capture of the same KXBTC15M window taken at the same time:

    websocket   300,562 deltas   152.9M contracts of shrinkage   942 fills
    polled       14,122 deltas    18.0M contracts of shrinkage    13 fills

Polling carried **11.8% of the real shrinkage**, and because the simulator
consumes queue from observed reductions, a resting order almost never reaches
the front. The same strategy filled 72x less often.

So this data is good for what it was first built for - spreads, depth, price
paths, which markets are alive and when - and it cannot support a conclusion
about fill rates, queue position, or any parameter that governs how often we
trade. Polling more often helps at the margin and does not fix the netting; the
websocket feed does, and needs credentials on the host.

scripts/sweep_backtests.py measures each recording's delta rate and refuses to
present its numbers as anything but floors when they are this thin.

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

# Families worth sampling, ordered by what we have actually measured rather than
# by what looked promising on a fee screen.
#
# The rolling 15-minute crypto windows come first because they are the only
# markets measured to support this strategy end to end: 91% fill rate, fills in
# about two seconds, and a queue that recycles many times inside one window.
# They are also the only markets on the exchange that are reliably short-dated -
# at any instant the whole exchange has roughly two of them open.
#
# The rest are here because a dataset of one regime teaches nothing about which
# regime matters. In-play sports and esports are episodic: their books are
# tradable while a game is live and effectively frozen the rest of the time
# (KXCS2GAME measured a queue needing 3.8 days to clear between matches, and
# KXLOLGAME 18 days), so recording them is only useful when something is
# happening. The collector re-picks targets every cycle precisely so it catches
# those windows instead of assuming them.
DEFAULT_SERIES = (
    # Measured viable.
    "KXBTC15M",
    "KXETH15M",
    # Same underlying, longer horizon - the control for "is it crypto, or is it
    # the 15-minute structure?"
    "KXBTCD",
    "KXETHD",
    # Episodic: worth recording only while in play.
    "KXNBAGAME",
    "KXNFLGAME",
    "KXMLBGAME",
    "KXVALORANTGAME",
    "KXCS2GAME",
    # News-driven rather than price-driven. Different mechanism, useful contrast.
    "KXTRUMPSAY",
    # Daily settles with real flow near the close.
    "KXHIGHNY",
    "KXHIGHCHI",
)
FEE_MODEL = KalshiFeeModel()
SCORE_SIZE = 50 * COUNT_SCALE


# Short-dated crypto windows open and close on the quarter hour. Recording on a
# fixed clock instead means every cycle starts wherever it happens to start,
# which produced a book with 95% of its fills inside six minutes of close and
# NOTHING in the 6-12 minute band - the half of a window where live markout is
# +0.41c. Every backtest run on that book was measuring the regime we least want
# to trade.
WINDOW_SECONDS = 15 * 60


def seconds_to_next_window(now: float | None = None) -> float:
    """Seconds until the next quarter-hour boundary, where a window opens."""

    now = time.time() if now is None else now
    return WINDOW_SECONDS - (now % WINDOW_SECONDS)


def wait_for_window_open(max_wait: float = WINDOW_SECONDS) -> float:
    """Sleep until the next window opens. Returns how long it waited.

    Waiting up to fifteen minutes to start looks wasteful and is not: a cycle
    that begins mid-window records the tail of a market nobody should be quoting
    and misses the part that pays.
    """

    delay = min(seconds_to_next_window(), max_wait)

    if delay > 1.0:
        log(f"  aligning to window open in {delay:.0f}s")
        time.sleep(delay)

    return delay


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

            # volume_24h_fp reads 0.00 for any market younger than a day, which
            # is every 15-minute window, always. Screening on it silently
            # excluded exactly the markets this collector exists to record.
            # volume_fp is the market's own lifetime volume and is the right
            # measure for a short-dated market.
            volume = max(
                pr._num(market.get("volume_fp")),
                pr._num(market.get("volume_24h_fp")),
            )

            # Pinned series bypass the volume floor. Aligning cycles to window
            # open means we look at a market seconds after it opens, when it has
            # traded almost nothing - so a volume filter rejects precisely the
            # market the alignment exists to capture. Two cycles recorded no
            # 15-minute window at all for this reason, after the alignment and
            # expiry-race fixes had both landed.
            #
            # These are pinned by policy rather than by liquidity, so liquidity
            # is not the gate. The two-sided check still applies: a market with
            # no book is not recordable whatever we intend.
            if 0 < bid < ask < 1 and (
                volume >= min_volume or _is_short_window(market["ticker"])
            ):
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


# Series whose markets are always worth a recording slot: rolling short-dated
# windows, which are both the subject of the research and by far the most
# active books on the exchange.
PINNED_SERIES = ("KXBTC15M", "KXETH15M")


# A pinned window must have enough life left to be worth recording. Without
# this the alignment fix backfired precisely: waiting for the quarter-hour and
# then selecting grabbed the window that had just EXPIRED at that boundary,
# because Kalshi still lists it for a few seconds. One aligned cycle recorded
# two dead 15-minute markets for fourteen minutes - 2,389 events against the
# ~100,000 a live window produces.
MIN_PINNED_SECONDS = 300.0


def _is_short_window(ticker: str) -> bool:
    return any(ticker.startswith(series) for series in PINNED_SERIES)


def _pinnable(market: dict, now: datetime | None = None) -> bool:
    """A short window with real life left, not one expiring as we look at it."""

    if not _is_short_window(str(market.get("ticker", ""))):
        return False

    stamp = market.get("close_time")

    if not isinstance(stamp, str) or not stamp:
        return False

    try:
        close = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False

    remaining = (close - (now or datetime.now(UTC))).total_seconds()
    return remaining >= MIN_PINNED_SECONDS


def run_cycle(args: argparse.Namespace, cycle: int) -> Path | None:
    log(f"cycle {cycle}: selecting targets")
    markets = candidate_markets(tuple(args.series), args.min_volume)

    if not markets:
        log("  no candidates; sleeping before retry")
        return None

    shortlisted = rank_candidates(markets, args.shortlist)
    log(f"  {len(markets)} candidates -> {len(shortlisted)} fee-plausible; probing churn")

    # Always record the rolling short-dated windows, whatever churn says.
    #
    # Churn ranking is a reasonable way to spend spare slots and a bad way to
    # choose the subject of the study. Left to itself it fills every slot with
    # daily crypto strike ladders, which are busy in aggregate and nearly static
    # per book: a cycle recorded that way carried 4 book deltas/sec/ticker while
    # a KXBTC15M window measured 352. Recording the quiet markets in high
    # fidelity is not an improvement over recording them badly.
    #
    # A 15-minute window is also the market this project is actually about, and
    # it is systematically penalised by a churn probe - it can be seconds from
    # settling, or freshly opened with an empty book, at the instant we look.
    pinned = [m["ticker"] for m in markets if _pinnable(m)]
    remaining = max(0, args.markets - len(pinned))
    ranked = pr._rank_by_churn(
        [t for t in shortlisted if t not in pinned],
        remaining,
        gap_seconds=args.probe_gap,
    ) if remaining else []
    tickers = pinned + ranked

    if pinned:
        log(f"  pinned {len(pinned)} short-window market(s): {', '.join(pinned)}")

    if not tickers:
        log("  nothing moving; sleeping before retry")
        return None

    out = Path(args.workdir) / f"rec_{now_stamp()}"

    if args.websocket:
        # The authenticated feed delivers every book event rather than net
        # change per interval - measured at ~50x the events of polling on the
        # same markets - so queue position and volatility are real rather than
        # approximated. Use it whenever credentials are present.
        command = [
            sys.executable,
            str(ROOT / "scripts" / "record_markets.py"),
            "--prod",
            *tickers,
            "--duration-sec",
            str(args.cycle_min * 60),
            "--output",
            str(out),
        ]
    else:
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
    parser.add_argument(
        "--align-windows",
        action="store_true",
        help="Start each cycle at a quarter-hour boundary, where the 15-minute "
        "crypto windows open. Without this a cycle lands mid-window and the "
        "recording misses the half of a window that carries the edge.",
    )
    parser.add_argument("--probe-gap", type=float, default=8.0)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means run forever.")
    parser.add_argument(
        "--websocket",
        action="store_true",
        help="Record via the authenticated websocket feed instead of REST "
        "polling. Needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.",
    )
    parser.add_argument("--keep-local", action="store_true")
    args = parser.parse_args()

    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    cycle = 0

    while not args.max_cycles or cycle < args.max_cycles:
        cycle += 1

        try:
            if args.align_windows:
                wait_for_window_open()

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
