"""Does the book misprice the final minute, when settlement is partly decided?

    python scripts/settlement_lockin.py data/aligned data/spot

Kalshi's 15-minute crypto windows settle on the **60-second average** of the
underlying index before the close. At t seconds into that averaging window,
t/60 of the settlement is already observed and immutable:

    S_hat(t) = (locked_sum + remaining * spot_now) / 60
    Var      = sigma^2 * m(m+1)(2m+1) / 6 / 60^2      (m seconds remaining)

(The variance is the remaining Brownian partial sum - independently verified,
Monte Carlo ratio 1.0018; the covariances carry ~95% of it, so the naive
sum-of-marginals would understate the sd ~4.5x and manufacture fake edge.)

Hypothesis: the book prices the final minute as if the question were still
open, so the locked share is an information advantage that grows every second,
in the one regime where fees are near zero and the maker edge is measured
negative. Alpha here is a **slope of (fair - mid) toward the close**; any
constant offset is index basis (the strike is proxied by spot at window open)
and proves nothing.

## Alignment is the whole game (adversarial review, round 2)

The first version of this script had three errors that compounded on the same
axis - the seconds-remaining alignment - each the same order as the effect
being measured:

* mids were epoch-aligned via `created_at_utc`, which is stamped seconds
  *after* the offset clock starts (connect + subscribe + close-time fetches),
  shearing every mid a constant few seconds late;
* `elapsed` counted spot *samples*, not wall seconds, so any gap in the spot
  file shifted rows into the wrong bucket and overstated the remaining
  variance (cubic in m);
* it hand-rolled incremental book reconstruction - the exact bug
  `market/bookio.py` exists to prevent - whose drift is monotone and
  indistinguishable from the slope under test.

This version reuses the canonical replay (`run_replay_backtest` mid_series),
aligns epochs with the manifest's `started_at_utc` (the same clock the offsets
are measured from), computes elapsed from wall time, and **drops any window**
with more than `MAX_MISSING_SECONDS` spot gaps in its averaging minute rather
than absorbing gaps into the variance term. Dropped windows are reported as
dropped; a window silently absorbed is a result silently wrong.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import statistics as st
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.market.price import ONE_DOLLAR, parse_count_fp  # noqa: E402
from kalshi_mm_bot.sim import fill_model_from_name, run_replay_backtest  # noqa: E402
from kalshi_mm_bot.strategy import strategy_from_name  # noqa: E402

AVERAGING_SECONDS = 60
# More than this many missing spot seconds in the averaging minute and the
# window is dropped, not patched.
MAX_MISSING_SECONDS = 5


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load_spot(spot_dir: Path, asset: str) -> dict[int, float]:
    """Per-second spot keyed by unix second; later files win on overlap."""

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
    """Close time from the ticker, e.g. KXBTC15M-26AUG172245-45.

    The HHMM is the CLOSE time in US Eastern (verified against live
    close_time fields: 26AUG171245-45 closes 16:45Z), and the trailing two
    digits repeat the close minute - they are not the open. Eastern-to-UTC via
    timedelta, because `hour + 4` raises ValueError for every evening window
    (ET hours 20-23) and a fixed offset is also an hour wrong in winter; the
    +4 here is correct only because these are August recordings, and the
    constant is named so a later reader questions it.
    """

    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-", ticker)

    if not match:
        return None

    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    mon = months.get(match.group(2))

    if mon is None:
        return None

    edt_to_utc = timedelta(hours=4)
    local = datetime(
        2000 + int(match.group(1)), mon, int(match.group(3)),
        int(match.group(4)), int(match.group(5)), tzinfo=UTC,
    )
    return int((local + edt_to_utc).timestamp())


def recording_offset_epoch(recording: Path) -> float | None:
    """Epoch of offset zero: the writer's start, NOT manifest creation.

    `created_at_utc` is stamped after connect/subscribe/close-time fetches, a
    run-dependent few seconds after the offset clock starts - a constant shear
    of exactly the axis this study measures.
    """

    try:
        manifest = json.loads((recording / "manifest.json").read_text())
        stamp = manifest.get("started_at_utc") or manifest.get("created_at_utc")
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, KeyError):
        return None


async def mid_series_for(recording: Path):
    """Canonical replay mid series - the tested reconstruction path."""

    result = await run_replay_backtest(
        recording,
        strategy=strategy_from_name(
            "dumb", count=parse_count_fp("1"), max_position=parse_count_fp("1")
        ),
        fill_model=fill_model_from_name("pessimistic"),
        speed_multiplier=0,
    )
    return result.mid_series


async def analyse(book_dir: Path, spot_dir: Path) -> str:
    spots = {"btc": load_spot(spot_dir, "btc"), "eth": load_spot(spot_dir, "eth")}
    rows: list[tuple[int, float]] = []  # (secs_remaining, edge_cents)
    windows = dropped_gaps = dropped_uncovered = 0

    for recording in sorted(book_dir.iterdir()):
        if not (recording / "manifest.json").exists():
            continue

        epoch0 = recording_offset_epoch(recording)

        if epoch0 is None:
            continue

        manifest = json.loads((recording / "manifest.json").read_text())
        series_by_ticker = None  # lazy: replay only when a window qualifies

        for ticker in manifest.get("tickers", []):
            if "15M" not in ticker:
                continue

            asset = "btc" if "BTC" in ticker else "eth"
            spot = spots[asset]
            close = window_close_epoch(ticker)

            if close is None:
                continue

            open_epoch = close - 900

            if open_epoch not in spot:
                dropped_uncovered += 1
                continue

            averaging_start = close - AVERAGING_SECONDS
            missing = [t for t in range(averaging_start, close) if t not in spot]

            if len(missing) > MAX_MISSING_SECONDS:
                dropped_gaps += 1
                continue

            if series_by_ticker is None:
                series_by_ticker = await mid_series_for(recording)

            series = series_by_ticker.get(ticker)

            if series is None or len(series.offsets) < 200:
                dropped_uncovered += 1
                continue

            strike = spot[open_epoch]
            # Sigma from contiguous 1-second diffs only: a diff across a hole
            # is not a 1-second diff, and treating it as one inflates vol.
            path = [(t, spot[t]) for t in range(open_epoch, close) if t in spot]
            diffs = [b - a for (t1, a), (t2, b) in zip(path, path[1:]) if t2 - t1 == 1]

            if len(diffs) < 300:
                dropped_gaps += 1
                continue

            sigma = st.pstdev(diffs) or 1e-9
            windows += 1

            for t in range(averaging_start, close):
                if t in missing:
                    continue

                offset = t - epoch0

                if not series.covers(offset):
                    continue

                mid = series.mid_at(offset)

                if mid is None:
                    continue

                # Elapsed is WALL time inside the averaging minute; a missing
                # second reduces the locked sum's sample but not the clock.
                elapsed_seconds = t - averaging_start + 1
                remaining = AVERAGING_SECONDS - elapsed_seconds

                if remaining <= 0:
                    continue

                observed = [spot[k] for k in range(averaging_start, t + 1) if k in spot]
                # Impute the few permitted missing seconds at the mean of what
                # was seen, never at the current spot.
                locked = sum(observed) + (elapsed_seconds - len(observed)) * st.mean(observed)
                mean_settle = (locked + remaining * spot[t]) / AVERAGING_SECONDS
                var_sum = sigma * sigma * remaining * (remaining + 1) * (2 * remaining + 1) / 6
                std_settle = math.sqrt(var_sum) / AVERAGING_SECONDS

                if std_settle <= 0:
                    continue

                fair = normal_cdf((mean_settle - strike) / std_settle)
                rows.append((remaining, fair * 100 - mid / (ONE_DOLLAR / 100)))

    lines = [
        f"{windows} window(s) scored; dropped {dropped_gaps} for spot gaps, "
        f"{dropped_uncovered} uncovered; {len(rows)} scored seconds"
    ]

    if not rows:
        lines.append("no joint coverage yet - sidecar must span a window's open AND close")
        return "\n".join(lines)

    for label, lo, hi in (("45-60s", 45, 61), ("30-45s", 30, 45),
                          ("15-30s", 15, 30), ("5-15s", 5, 15), ("<5s", 0, 5)):
        bucket = [e for r, e in rows if lo <= r < hi]

        if bucket:
            lines.append(
                f"  {label:>7} remaining: n={len(bucket):>4}  edge (fair-mid) "
                f"mean {st.mean(bucket):+.2f}c  median {st.median(bucket):+.2f}c"
            )

    lines.append(
        "A constant offset across buckets is index basis (strike proxied by spot "
        "at open); alpha is a SLOPE toward the close that survives it. Alignment "
        "error compounds on this exact axis - treat sub-2c slopes as noise."
    )
    return "\n".join(lines)


def main() -> None:
    book_dir, spot_dir = Path(sys.argv[1]), Path(sys.argv[2])
    print(asyncio.run(analyse(book_dir, spot_dir)))


if __name__ == "__main__":
    main()
