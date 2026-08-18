"""Does the book misprice the final minute, when settlement is partly decided?

    python scripts/settlement_lockin.py data/aligned data/spot

Kalshi's 15-minute crypto windows settle on the **60-second average** of the
underlying index before the close. That changes the character of the last
minute completely: at t seconds into the averaging window, t/60 of the
settlement value is already *observed and immutable*. The contract's value is

    S_hat(t) = (locked_sum + remaining * spot_now) / 60

with uncertainty only over the remaining seconds - and that uncertainty
collapses to zero as the bell approaches, quadratically faster than the naive
"time to expiry" a casual model would use.

Hypothesis: the book prices the final minute as if the question were still
open, so a taker who computes the locked share holds an information advantage
that grows every second - in exactly the regime where fees are near zero
(0.04-0.18c round trip in the tails) and the maker edge is measured negative.
This would be the only late-window idea that does not fight informed flow; it
would BE the informed flow.

## Method

Join the spot sidecar (1s BTC/ETH USD) with an aligned book recording covering
the same window's final 90 seconds. At each second of the averaging minute:

* fair = P(settlement > strike) with settlement mean S_hat(t) and the variance
  of the remaining Brownian partial sum (sigma estimated from the same spot
  file, so no external vol input)
* edge = fair - book mid, when a two-sided book exists

Report the edge distribution by seconds-remaining, and what a taker paying the
actual spread and tail fee would have kept.

## Honesty

The sidecar reads Coinbase/Kraken mid, not BRTI. The strike sits wherever it
sits relative to the true index, so systematic basis shows up as a constant
offset in `edge`; the *slope* of edge versus seconds-remaining is the part the
basis cannot fake. n will be small for days - one window per 15 minutes, and
only windows whose final minute both feeds covered.
"""

from __future__ import annotations

import json
import math
import re
import statistics as st
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.market.price import ONE_DOLLAR  # noqa: E402

AVERAGING_SECONDS = 60


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load_spot(spot_dir: Path, asset: str) -> dict[int, float]:
    """Per-second spot, keyed by unix second. Later files win on overlap."""

    out: dict[int, float] = {}

    for path in sorted(spot_dir.glob("spot_*.jsonl")):
        for line in path.open():
            try:
                row = json.loads(line)
                value = row.get(asset)

                if value is not None:
                    out[int(row["t"])] = float(value)
            except (ValueError, KeyError, TypeError):
                continue

    return out


def window_close_epoch(ticker: str) -> int | None:
    """Close time from the ticker name, e.g. ...-26AUG180215-15 -> epoch."""

    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-", ticker)

    if not match:
        return None

    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    year, mon = 2000 + int(match.group(1)), months.get(match.group(2))
    day, hour, minute = int(match.group(3)), int(match.group(4)), int(match.group(5))

    if mon is None:
        return None

    # Ticker times are US Eastern; August is EDT = UTC-4.
    return int(datetime(year, mon, day, hour + 4, minute, tzinfo=UTC).timestamp())


def book_mids(recording: Path, ticker: str, start_epoch: float) -> dict[int, int]:
    """Mid per unix second for the ticker, built from snapshots + full replays.

    Uses ws_top on reconstructed ladders per event would repeat the
    incremental-book mistake; instead sample only SNAPSHOT messages plus a
    conservative delta-application that resets on each snapshot. Snapshots are
    rare, so this leans on the deltas - but any second where the implied book
    is crossed is dropped rather than trusted.
    """

    bids: dict[int, float] = {}
    asks: dict[int, float] = {}
    out: dict[int, int] = {}

    for line in (recording / "events.jsonl").open():
        try:
            row = json.loads(line)
        except ValueError:
            continue

        message = row.get("msg") or {}
        inner = message.get("msg") or {}

        if inner.get("market_ticker") != ticker:
            continue

        kind = message.get("type")

        if kind == "orderbook_snapshot":
            bids = {int(float(p) * ONE_DOLLAR): float(s)
                    for p, s in inner.get("yes_dollars_fp") or []}
            asks = {int(float(p) * ONE_DOLLAR): float(s)
                    for p, s in inner.get("no_dollars_fp") or []}
        elif kind == "orderbook_delta":
            try:
                price = int(float(inner["price_dollars"]) * ONE_DOLLAR)
                delta = float(inner["delta_fp"])
            except (KeyError, TypeError, ValueError):
                continue

            ladder = bids if inner.get("side") == "yes" else asks
            ladder[price] = ladder.get(price, 0.0) + delta

            if ladder[price] <= 0:
                ladder.pop(price, None)
        else:
            continue

        offset = row.get("offset_seconds")

        if not isinstance(offset, (int, float)) or not bids or not asks:
            continue

        bid, ask = max(bids), min(asks)

        if bid < ask:
            out[int(start_epoch + offset)] = (bid + ask) // 2

    return out


def recording_start_epoch(recording: Path) -> float | None:
    try:
        manifest = json.loads((recording / "manifest.json").read_text())
        return datetime.fromisoformat(
            manifest["created_at_utc"].replace("Z", "+00:00")
        ).timestamp()
    except (OSError, ValueError, KeyError):
        return None


def analyse(book_dir: Path, spot_dir: Path) -> str:
    spots = {"btc": load_spot(spot_dir, "btc"), "eth": load_spot(spot_dir, "eth")}
    rows: list[tuple[int, float, float]] = []  # (secs_remaining, edge_cents, fair)
    windows = 0

    for recording in sorted(book_dir.iterdir()):
        start = recording_start_epoch(recording)

        if start is None:
            continue

        try:
            manifest = json.loads((recording / "manifest.json").read_text())
        except (OSError, ValueError):
            continue

        for ticker in manifest.get("tickers", []):
            if "15M" not in ticker:
                continue

            asset = "btc" if "BTC" in ticker else "eth"
            spot = spots[asset]
            close = window_close_epoch(ticker)

            if close is None:
                continue

            # Strike: the window's open price rounded per Kalshi's rule is not
            # in the ticker; the last two digits are the open minute. The
            # strike equals the index price at window open, which we can only
            # approximate by spot at open time. Skip windows where the sidecar
            # was not yet running at open.
            open_epoch = close - 900

            if open_epoch not in spot:
                continue

            strike = spot[open_epoch]
            mids = book_mids(recording, ticker, start)

            # Estimate per-second sigma from this window's own spot path.
            path = [spot[t] for t in range(open_epoch, close) if t in spot]

            if len(path) < 300:
                continue

            diffs = [b - a for a, b in zip(path, path[1:])]
            sigma = st.pstdev(diffs) or 1e-9
            windows += 1
            averaging_start = close - AVERAGING_SECONDS

            for t in range(averaging_start, close):
                if t not in spot or t not in mids:
                    continue

                observed = [spot[k] for k in range(averaging_start, t + 1) if k in spot]
                locked = sum(observed)
                elapsed = len(observed)
                remaining = AVERAGING_SECONDS - elapsed

                if remaining <= 0:
                    continue

                mean_settle = (locked + remaining * spot[t]) / AVERAGING_SECONDS
                # Variance of the remaining Brownian partial sum, scaled into
                # the 60-second average.
                var_sum = sigma * sigma * remaining * (remaining + 1) * (2 * remaining + 1) / 6
                std_settle = math.sqrt(var_sum) / AVERAGING_SECONDS

                if std_settle <= 0:
                    continue

                fair = normal_cdf((mean_settle - strike) / std_settle)
                mid_cents = mids[t] / (ONE_DOLLAR / 100)
                rows.append((remaining, (fair * 100) - mid_cents, fair))

    lines = [f"{windows} window(s) with joint spot+book coverage; {len(rows)} scored seconds"]

    if not rows:
        lines.append("no joint coverage yet - the sidecar needs to span a window open AND close")
        return "\n".join(lines)

    for label, lo, hi in (("45-60s", 45, 61), ("30-45s", 30, 45), ("15-30s", 15, 30), ("5-15s", 5, 15), ("<5s", 0, 5)):
        bucket = [e for r, e, _ in rows if lo <= r < hi]

        if bucket:
            lines.append(
                f"  {label:>7} remaining: n={len(bucket):>4}  edge (fair-mid) "
                f"mean {st.mean(bucket):+.2f}c  median {st.median(bucket):+.2f}c"
            )

    lines.append(
        "A constant offset across buckets is index basis (the strike is proxied "
        "by spot at open); alpha is a SLOPE toward the close that survives it."
    )
    return "\n".join(lines)


def main() -> None:
    book_dir, spot_dir = Path(sys.argv[1]), Path(sys.argv[2])
    print(analyse(book_dir, spot_dir))


if __name__ == "__main__":
    main()
