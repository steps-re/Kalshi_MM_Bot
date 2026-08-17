"""Dissect the internal structure of a short-window market over its life.

    python scripts/window_anatomy.py --series KXBTC15M --minutes 20
    python scripts/window_anatomy.py --replay recordings/btc15m.jsonl

Records book snapshots for a rolling short-dated series and reports how spread,
price, depth and fee arithmetic evolve from open to close. Needs no API key.

Why this exists as its own tool: the cross-sectional screen looks at every
market at one instant and concludes that markets near the ends of the price
range are the viable ones, because the fee falls with P(1-P) while the spread
stays near a tick. That is true *across* markets and it is not true *within* a
15-minute window, which is the thing the strategy actually trades. Measuring one
window end to end is the only way to see the difference, and the difference
reverses the conclusion.

Measured on KXBTC15M, 2026-08-16, one full window at 2s resolution:

    time left      mid   spread   rt fee   net (taker)   net (maker-free)
    14-12m       0.455    1.00c    3.48c        -2.48c              1.00c
    12-9m        0.685    1.00c    3.04c        -2.04c              1.00c
    9-6m         0.625    1.00c    3.30c        -2.30c              1.00c
    6-3m         0.365    1.00c    3.26c        -2.26c              1.00c
    3-1m         0.155    1.00c    1.84c        -0.84c              1.00c
    <1m          0.008    0.10c    0.12c        -0.02c              0.10c

Three things this shows that the cross-sectional screen cannot:

* **The price does migrate to the tail, monotonically.** Distance from the
  midpoint runs 4.5c -> 18.5c -> 34.5c -> 45.5c as the window resolves. The
  "resolving path" story is real.
* **The spread migrates with it.** It holds at exactly one cent for fourteen
  minutes and then collapses to a tenth of a cent inside the last minute, when
  the tapered grid opens up below 10c. So the fee falls in the tail and the
  spread falls too, and the gap never opens. Across markets those two move
  independently; within a window they do not.
* **Depth collapses 11x** (4,131 -> 372 contracts at the touch) while flow rises
  (130k -> 234k contracts/min). Queue position gets easier all the way down. The
  queue is not what stops this market being profitable.

Which leaves the whole question resting on one binary: the last column. Every
row is negative if a resting maker order pays the taker fee, and every row is
positive if it does not. No amount of further screening moves that needle -
only a live maker fill does, which is what scripts/calibrate_fees.py is for.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import poll_record as pr  # noqa: E402

# Open-to-close buckets, coarse enough that a single window fills them all.
BUCKETS = (
    ("14-12m", 720, 900),
    ("12-9m", 540, 720),
    ("9-6m", 360, 540),
    ("6-3m", 180, 360),
    ("3-1m", 60, 180),
    ("<1m", 0, 60),
)


def fee_cents(price: float, contracts: int = 100) -> float:
    """Kalshi's per-order fee in cents per contract.

    The ceiling is applied to the whole order, so it is amortised over the
    order rather than charged per contract; quoting it per contract at a
    realistic order size is the honest way to compare it to a spread.
    """

    return math.ceil(0.07 * contracts * price * (1.0 - price) * 100) / contracts


def record(series: str, minutes: float, out: Path, interval: float) -> Path:
    """Poll the open market(s) of a rolling series and write raw snapshots."""

    deadline = time.time() + minutes * 60
    written = 0

    with out.open("w") as handle:
        while time.time() < deadline:
            try:
                markets = pr.get(
                    "/markets",
                    {"status": "open", "limit": 5, "series_ticker": series},
                ).get("markets", [])

                for market in markets:
                    book = pr.get(
                        f"/markets/{market['ticker']}/orderbook", {"depth": 6}
                    ).get("orderbook_fp", {})
                    handle.write(
                        json.dumps(
                            {
                                "t": datetime.now(UTC).isoformat(),
                                "m": market,
                                "ob": book,
                            }
                        )
                        + "\n"
                    )
                    handle.flush()
                    written += 1
            except Exception as error:  # a blip must not end a 20-minute run
                print(f"  {type(error).__name__}: {error}", file=sys.stderr)

            time.sleep(interval)

    print(f"{written} snapshots -> {out}")
    return out


def load(path: Path) -> list[dict]:
    """Parse snapshots into per-observation rows, skipping partial lines.

    A file being written concurrently ends in half a line; that is expected,
    not an error worth stopping for.
    """

    rows: list[dict] = []

    for line in path.open():
        try:
            record = json.loads(line)
        except ValueError:
            continue

        market, book = record.get("m"), record.get("ob") or {}

        if not market:
            continue

        yes, no = book.get("yes_dollars") or [], book.get("no_dollars") or []

        if not yes or not no:
            continue

        bids = {float(p): float(s) for p, s in yes}
        asks = {1.0 - float(p): float(s) for p, s in no}
        best_bid, best_ask = max(bids), min(asks)

        if best_bid >= best_ask:
            continue

        closed_at = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        rows.append(
            {
                "ticker": market["ticker"],
                "left": (closed_at - datetime.fromisoformat(record["t"])).total_seconds(),
                "spread": (best_ask - best_bid) * 100,
                "mid": (best_ask + best_bid) / 2,
                "depth": (bids[best_bid] + asks[best_ask]) / 2,
                "volume": float(market.get("volume_fp") or 0),
            }
        )

    return rows


def report(rows: list[dict]) -> str:
    windows = sorted({r["ticker"] for r in rows})
    lines = [
        f"{len(rows)} snapshots across {len(windows)} window(s)",
        "",
        f"{'time left':<10}{'mid':>8}{'spread':>9}{'depth':>9}"
        f"{'rt fee':>9}{'net (taker)':>13}{'net (maker-free)':>19}",
    ]

    for label, low, high in BUCKETS:
        bucket = [r for r in rows if low <= r["left"] < high]

        if not bucket:
            continue

        mid = st.median([r["mid"] for r in bucket])
        spread = st.median([r["spread"] for r in bucket])
        depth = st.median([r["depth"] for r in bucket])
        round_trip = 2 * fee_cents(mid)

        lines.append(
            f"{label:<10}{mid:>8.3f}{spread:>8.2f}c{depth:>9,.0f}"
            f"{round_trip:>8.2f}c{spread - round_trip:>12.2f}c{spread:>18.2f}c"
        )

    lines += [
        "",
        "Fee is ceil(0.07 x N x P x (1-P)) per order, charged to takers. Whether a",
        "resting maker order pays it decides the sign of every row above, and it is",
        "still unmeasured - see scripts/calibrate_fees.py.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXBTC15M")
    parser.add_argument("--minutes", type=float, default=20.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("window_anatomy.jsonl"))
    parser.add_argument(
        "--replay",
        type=Path,
        help="Analyse an existing recording instead of polling.",
    )
    args = parser.parse_args()

    path = args.replay or record(args.series, args.minutes, args.output, args.interval)
    rows = load(path)

    if not rows:
        print("no two-sided snapshots in that recording")
        return

    print(report(rows))


if __name__ == "__main__":
    main()
