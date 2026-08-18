"""Where does adverse selection bite? Slice per-fill markout by toxicity axes.

    # pull the preserved journals down first
    gcloud storage rsync gs://steps-kalshi-book/journals /tmp/journals
    python scripts/markout_slices.py /tmp/journals

We have positive *aggregate* markout and flat account P&L, which means the
average hides a toxic tail: some fills mark strongly negative and cost us the
edge the benign fills earn. This finds that tail by slicing per-fill markout on
the axes the market-making literature flags as adverse-selection predictors:

* **queue depth at placement** (depth_ahead) - the journal's own comment calls it
  "the single best predictor of adverse selection". Back-of-queue fills happen
  only after everyone ahead pulled, i.e. right before the move.
* **price level (P)** - near 0.5 the binary's delta is largest, so a one-tick
  index wiggle swings fair value hardest; the tails resolve on a discrete jump.
* **time into the cycle** - informed flow concentrates near expiry as the
  settlement average locks in.
* **A/B arm** (from the filename) - does a wider quote actually move the toxic
  slice, or just fill less?

Markout is the LEADING signal, not P&L (a positive-markout fill can still lose on
the follow-through past the horizon). The point here is comparative: the slice
where markout is most negative is where to widen, skew, or pull - the calibration
the OSS microprice / OBI-skew / settlement-pull techniques all need as input.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ONE_DOLLAR = 10_000
TICKS_PER_CENT = ONE_DOLLAR // 100  # 100 ticks = 1 cent


def arm_of(path: Path) -> str:
    # jrnl_<YYYYmmddTHHMMSS>_<arm>.jsonl -> <arm>; anything else -> "?"
    parts = path.stem.split("_")
    return parts[2] if len(parts) >= 3 and parts[0] == "jrnl" else "?"


def depth_bucket(depth: float | None) -> str:
    if depth is None:
        return "unknown"
    if depth <= 0:
        return "0 (front)"
    if depth <= 100:
        return "1-100"
    if depth <= 1_000:
        return "100-1k"
    if depth <= 10_000:
        return "1k-10k"
    return "10k+ (deep)"


def price_bucket(mid: int | None) -> str:
    if mid is None:
        return "unknown"
    p = mid / ONE_DOLLAR
    if p < 0.15:
        return "0.00-0.15"
    if p < 0.35:
        return "0.15-0.35"
    if p < 0.65:
        return "0.35-0.65 (mid)"
    if p < 0.85:
        return "0.65-0.85"
    return "0.85-1.00"


def time_bucket(elapsed: float) -> str:
    if elapsed < 60:
        return "0-60s"
    if elapsed < 180:
        return "60-180s"
    if elapsed < 300:
        return "180-300s"
    if elapsed < 420:
        return "300-420s"
    return "420s+"


def parse_iso(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def fills_from(path: Path) -> list[dict]:
    """Return each fill with markout and its toxicity slice keys."""

    placed: dict[str, dict] = {}
    rows = []
    start: float | None = None

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            event = json.loads(line)
        except ValueError:
            continue

        at = parse_iso(event.get("at", ""))

        if at is not None and (start is None or at < start):
            start = at

        kind = event.get("event")

        if kind == "placed":
            oid = event.get("order_id")

            if oid:
                placed[oid] = event
        elif kind == "filled":
            rows.append((event, at))

    out = []

    for event, at in rows:
        mid = event.get("mid_at_fill")
        price = event.get("yes_price")

        if mid is None or price is None:
            continue

        drift = (mid - price) / TICKS_PER_CENT
        markout = drift if event.get("action") == "buy" else -drift
        origin = placed.get(event.get("order_id"), {})
        out.append(
            {
                "markout": markout,
                "arm": arm_of(path),
                "depth": depth_bucket(origin.get("depth_ahead")),
                "price": price_bucket(mid),
                "time": time_bucket((at - start) if at and start else 0.0),
            }
        )

    return out


def report_slice(fills: list[dict], key: str, order: list[str] | None = None) -> None:
    groups: dict[str, list[float]] = defaultdict(list)

    for f in fills:
        groups[f[key]].append(f["markout"])

    keys = order or sorted(groups)
    print(f"\nmarkout by {key}:")
    print(f"  {'slice':<18}{'n':>7}{'mean':>10}{'contribution':>14}")

    for k in keys:
        vals = groups.get(k)

        if not vals:
            continue

        mean = st.mean(vals)
        # contribution = mean * n, in "cents of markout" this slice adds or
        # removes overall - the toxic tail is the most-negative contribution.
        print(f"  {k:<18}{len(vals):>7}{mean:>+9.3f}c{mean * len(vals):>+13.1f}c")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/tmp/session")
    paths = sorted(root.glob("*.jsonl"))

    if not paths:
        print(f"no journals under {root}")
        return

    fills: list[dict] = []

    for path in paths:
        fills.extend(fills_from(path))

    if not fills:
        print(f"{len(paths)} journal(s) but no scorable fills")
        return

    overall = st.mean(f["markout"] for f in fills)
    print(f"{len(fills)} fills across {len(paths)} journal(s); "
          f"overall markout {overall:+.3f}c")

    report_slice(fills, "depth",
                 ["0 (front)", "1-100", "100-1k", "1k-10k", "10k+ (deep)", "unknown"])
    report_slice(fills, "price",
                 ["0.00-0.15", "0.15-0.35", "0.35-0.65 (mid)", "0.65-0.85", "0.85-1.00", "unknown"])
    report_slice(fills, "time",
                 ["0-60s", "60-180s", "180-300s", "300-420s", "420s+"])
    report_slice(fills, "arm")

    print("\nThe most-negative contribution is where adverse selection bites; "
          "set the microprice / OBI-skew / settlement-pull thresholds off it. "
          "Markout is leading, not P&L - confirm any change against ab_report.py.")


if __name__ == "__main__":
    main()
