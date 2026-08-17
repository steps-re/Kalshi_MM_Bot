"""Measure the simulator's fill-model parameters instead of assuming them.

    python scripts/calibrate_fills.py --series KXBTC15M --minutes 15
    python scripts/calibrate_fills.py --replay data/fillcal.jsonl

`QueueAwareFillModel` has one parameter that decides almost everything it says:
`trade_fraction`, the share of a level's shrinkage that was a real trade rather
than someone cancelling. It ships at 0.5, which is a guess, and the model is
extremely sensitive to it - it controls how fast the queue ahead of a resting
order is eaten, and therefore whether the order ever fills.

That guess is why the simulator and the account have never agreed. The queue
model reported a 31% fill rate over twenty minutes while seven real resting
orders filled 0%; days later, live fills in a 15-minute crypto window came back
at 91%. Both cannot be right, and neither was measured.

**It does not have to be fitted.** A level shrinks for exactly two reasons, and
Kalshi publishes one of them. `/markets/trades` is public and reports every
execution with its price and the side the taker hit. So:

    trade_fraction = traded volume / total level shrinkage

is a direct measurement over any window we care to record, not a parameter
search. Fitting it against fill outcomes would have been circular anyway: the
thing we want the model to predict is the thing we would have tuned it on.

What this reports, and why each number matters:

* **trade_fraction** - feeds straight into the model. Below 0.5 means the book
  is mostly people cancelling, queue position decays slower than assumed, and
  the simulator is optimistic. Above 0.5 and it is pessimistic.
* **cancel share** - the same number from the other side, because "84% of the
  shrinkage at the touch was cancellations" is the sentence that explains why a
  queue looked impassable and was not.
* **per-side split** - bid and ask can differ, and a strategy quoting both sides
  inherits both.

The honest limit: polling at one-second intervals sees the net change over that
second, so a level that traded and was replenished inside the interval reads as
no change. That biases the measured shrinkage *down* and therefore the fraction
*up*. The websocket feed does not have this problem, and a run recorded through
it should be preferred whenever credentials are available. The gap is reported
rather than corrected for, because a correction would be another guess.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import poll_record as pr  # noqa: E402


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


def _levels(book: dict) -> tuple[dict[float, float], dict[float, float]]:
    """Bid and ask levels keyed by YES price in dollars."""

    bids = {float(p): float(s) for p, s in (book.get("yes_dollars") or [])}
    # NO prices mirror about a dollar; fold them onto the YES axis so trades and
    # book levels share one coordinate system.
    asks = {round(1.0 - float(p), 4): float(s) for p, s in (book.get("no_dollars") or [])}
    return bids, asks


def record(series: str, minutes: float, out: Path, interval: float) -> Path:
    """Snapshot books and trades together for one rolling series."""

    deadline = time.time() + minutes * 60
    written = 0
    seen_trades: set[str] = set()
    warmed: set[str] = set()

    with out.open("w") as handle:
        while time.time() < deadline:
            try:
                markets = pr.get(
                    "/markets", {"status": "open", "limit": 5, "series_ticker": series}
                ).get("markets", [])

                for market in markets:
                    ticker = market["ticker"]
                    book = pr.get(
                        f"/markets/{ticker}/orderbook", {"depth": 10}
                    ).get("orderbook_fp", {})
                    raw_trades = pr.get(
                        "/markets/trades", {"ticker": ticker, "limit": 200}
                    ).get("trades", [])

                    fresh = [t for t in raw_trades if t.get("trade_id") not in seen_trades]
                    seen_trades.update(str(t.get("trade_id")) for t in fresh)

                    # The first poll of a ticker returns its recent trade
                    # HISTORY, not trades since we started watching. Counting
                    # that backlog against shrinkage we never observed inflates
                    # the ratio without limit: a slow market measured 28,003
                    # contracts traded against 127 of observed shrinkage, which
                    # a min(1.0, ...) clamp then quietly reported as a perfect
                    # 1.000. Seed the dedup set on the first poll and count
                    # nothing from it.
                    if ticker not in warmed:
                        warmed.add(ticker)
                        fresh = []

                    handle.write(
                        json.dumps(
                            {
                                "t": datetime.now(UTC).isoformat(),
                                "ticker": ticker,
                                "ob": book,
                                "trades": fresh,
                            }
                        )
                        + "\n"
                    )
                    handle.flush()
                    written += 1
            except Exception as error:
                log(f"  {type(error).__name__}: {error}")

            time.sleep(interval)

    log(f"{written} snapshots -> {out}")
    return out


def calibrate_websocket(path: Path, trades_by_ticker: dict[str, float]) -> dict:
    """Shrinkage from the delta feed, which sees every change rather than a net.

    This is the measurement polling cannot make. A REST snapshot every second
    reports the *net* change over that second, so a level that traded 500
    contracts and was refilled by another 500 reads as unchanged - the trade is
    invisible and the shrinkage is never counted. The delta feed reports both
    events, so summing negative deltas gives true shrinkage rather than a lower
    bound on it.

    Since polling understates shrinkage and shrinkage is the denominator, the
    polled trade_fraction is biased *up*. This should therefore come out lower
    than the 0.286 polling reported, and if it does not, the polling result was
    not suffering the bias we assumed it was.
    """

    per_ticker: dict[str, dict] = defaultdict(
        lambda: {
            "bid_shrink": 0.0,
            "ask_shrink": 0.0,
            "bid_traded": 0.0,
            "ask_traded": 0.0,
            "unattributed_traded": 0.0,
            "snapshots": 0,
        }
    )

    for line in path.open():
        try:
            row = json.loads(line)
        except ValueError:
            continue

        message = (row.get("msg") or {}).get("msg") or {}
        kind = (row.get("msg") or {}).get("type")
        ticker = message.get("market_ticker")

        if not ticker:
            continue

        stats = per_ticker[ticker]

        if kind == "orderbook_snapshot":
            stats["snapshots"] += 1
            continue

        if kind != "orderbook_delta":
            continue

        try:
            delta = float(message.get("delta_fp") or message.get("delta") or 0)
        except (TypeError, ValueError):
            continue

        if delta >= 0:
            continue  # growth is new liquidity, not queue consumption

        side = message.get("side")

        if side == "yes":
            stats["bid_shrink"] += -delta
        elif side == "no":
            stats["ask_shrink"] += -delta

    # Trades come from REST; the delta channel does not carry them.
    for ticker, traded in trades_by_ticker.items():
        if ticker in per_ticker:
            per_ticker[ticker]["bid_traded"] = traded / 2.0
            per_ticker[ticker]["ask_traded"] = traded / 2.0

    return dict(per_ticker)


def fetch_trades_since(ticker: str, since_ts: float) -> float:
    """Total contracts traded in `ticker` at or after `since_ts`."""

    total = 0.0
    cursor = None

    for _ in range(40):
        params = {"ticker": ticker, "limit": 200}

        if cursor:
            params["cursor"] = cursor

        data = pr.get("/markets/trades", params)
        page = data.get("trades") or []

        for trade in page:
            stamp = trade.get("created_time")

            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                continue

            if when >= since_ts:
                try:
                    total += float(trade.get("count_fp") or 0)
                except (TypeError, ValueError):
                    continue

        cursor = data.get("cursor")

        if not cursor or not page:
            break

    return total


def calibrate(path: Path) -> dict:
    """Shrinkage attributable to trades, versus all shrinkage."""

    per_ticker: dict[str, dict] = defaultdict(
        lambda: {
            "bid_shrink": 0.0,
            "ask_shrink": 0.0,
            "bid_traded": 0.0,
            "ask_traded": 0.0,
            "unattributed_traded": 0.0,
            "snapshots": 0,
        }
    )
    previous: dict[str, tuple[dict, dict]] = {}

    for line in path.open():
        try:
            row = json.loads(line)
        except ValueError:
            continue

        ticker = row.get("ticker")
        book = row.get("ob") or {}

        if not ticker or not book:
            continue

        bids, asks = _levels(book)
        stats = per_ticker[ticker]
        stats["snapshots"] += 1

        if ticker in previous:
            old_bids, old_asks = previous[ticker]

            # Only shrinkage counts. Growth is new liquidity arriving, which
            # tells us nothing about how fast a queue is consumed.
            for price, size in old_bids.items():
                stats["bid_shrink"] += max(0.0, size - bids.get(price, 0.0))

            for price, size in old_asks.items():
                stats["ask_shrink"] += max(0.0, size - asks.get(price, 0.0))

        previous[ticker] = (bids, asks)

        for trade in row.get("trades") or []:
            try:
                count = float(trade.get("count_fp") or 0)
            except (TypeError, ValueError):
                continue

            # taker_book_side names the resting side that was consumed. An
            # absent or unexpected value used to fall through to the bid, which
            # silently attributed every unparseable trade to one side and
            # skewed the per-side split. Count it toward neither.
            side = trade.get("taker_book_side")

            if side == "ask":
                stats["ask_traded"] += count
            elif side == "bid":
                stats["bid_traded"] += count
            else:
                stats["unattributed_traded"] = (
                    stats.get("unattributed_traded", 0.0) + count
                )

    return dict(per_ticker)


def _ws_window(path: Path) -> tuple[float, float, list[str]]:
    """First and last observation times, and the tickers seen."""

    first = last = None
    tickers: set[str] = set()

    for line in path.open():
        try:
            row = json.loads(line)
        except ValueError:
            continue

        stamp = row.get("observed_at_utc")

        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue

        first = when if first is None else min(first, when)
        last = when if last is None else max(last, when)
        ticker = ((row.get("msg") or {}).get("msg") or {}).get("market_ticker")

        if ticker:
            tickers.add(str(ticker))

    return (first or 0.0), (last or 0.0), sorted(tickers)


def report(stats: dict, *, websocket: bool = False) -> str:
    lines = [
        f"{'ticker':<28}{'snaps':>7}{'shrink':>11}{'traded':>10}"
        f"{'trade_frac':>12}{'cancel':>9}"
    ]
    fractions: list[float] = []

    for ticker, s in sorted(stats.items()):
        shrink = s["bid_shrink"] + s["ask_shrink"]
        traded = s["bid_traded"] + s["ask_traded"]

        if shrink <= 0:
            continue

        fraction = traded / shrink

        if fraction > 1.0:
            # Physically impossible: more traded than the book gave up. Report
            # it and exclude it rather than clamping, which is what hid the
            # trade-backlog bug in the first place.
            lines.append(
                f"{ticker[-27:]:<28}{s['snapshots']:>7}{shrink:>11,.0f}{traded:>10,.0f}"
                f"{'IMPOSSIBLE':>12}{'excluded':>9}"
            )
            continue

        fractions.append(fraction)
        lines.append(
            f"{ticker[-27:]:<28}{s['snapshots']:>7}{shrink:>11,.0f}{traded:>10,.0f}"
            f"{fraction:>12.3f}{1 - fraction:>9.1%}"
        )

    unattributed = sum(s.get("unattributed_traded", 0.0) for s in stats.values())

    if unattributed:
        lines.append(
            f"\n!! {unattributed:,.0f} contracts had no readable taker side and "
            "were excluded from both sides rather than guessed."
        )

    if not fractions:
        return "no level shrinkage observed - nothing to calibrate"

    median = st.median(fractions)
    lines.append("")
    lines.append(f"measured trade_fraction: {median:.3f} (median over {len(fractions)} market(s))")
    lines.append(f"simulator default:       0.500")

    if median < 0.45:
        lines.append(
            f"\n-> The book shrinks mostly through CANCELLATION ({1 - median:.0%}). "
            "The queue ahead of a resting order decays slower than the default "
            "assumes, so the simulator is OPTIMISTIC about fills. Set "
            f"trade_fraction={median:.3f}."
        )
    elif median > 0.55:
        lines.append(
            f"\n-> The book shrinks mostly through TRADING ({median:.0%}). The "
            "queue is consumed faster than the default assumes and the simulator "
            f"is PESSIMISTIC. Set trade_fraction={median:.3f}."
        )
    else:
        lines.append("\n-> Close enough to the 0.5 default that it was a lucky guess.")

    if websocket:
        lines.append(
            "\nMeasured from the delta feed, which reports every book change "
            "rather than a net per interval, so shrinkage is exact rather than a "
            "lower bound. This is the number to use."
        )
    else:
        lines.append(
            "\nPolling sees net change per interval, so a level that traded and "
            "refilled inside one tick reads as unchanged. That understates "
            "shrinkage and overstates this fraction, so treat it as an UPPER "
            "bound - the websocket measurement came in at roughly half the "
            "polled figure."
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXBTC15M")
    parser.add_argument("--minutes", type=float, default=15.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=Path("fill_calibration.jsonl"))
    parser.add_argument("--replay", type=Path)
    parser.add_argument(
        "--ws-replay",
        type=Path,
        help="A websocket recording (events.jsonl) from record_markets.py. Sees "
        "every book change rather than a net-per-second, so shrinkage is exact.",
    )
    args = parser.parse_args()

    if args.ws_replay:
        first, last, tickers = _ws_window(args.ws_replay)
        log(f"websocket window {last - first:.0f}s over {len(tickers)} ticker(s)")
        trades = {t: fetch_trades_since(t, first) for t in tickers}

        for ticker, traded in trades.items():
            log(f"  {ticker}: {traded:,.0f} contracts traded in window")

        stats = calibrate_websocket(args.ws_replay, trades)
        print(report(stats, websocket=True))
        return

    path = args.replay or record(args.series, args.minutes, args.output, args.interval)
    stats = calibrate(path)

    if not stats:
        print("no usable snapshots")
        return

    print(report(stats))


if __name__ == "__main__":
    main()
