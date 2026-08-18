"""Horizon markout from the BOT'S OWN book - the trustworthy version.

    gcloud storage rsync gs://steps-kalshi-book/journals /tmp/pj
    python scripts/markout_selfbook.py /tmp/pj

The recording-join version (markout_horizon.py) failed its own control: the
collector is a separate websocket connection, so its book does not align with the
bot's at the fill instant (recorded t=0 markout +0.08c vs the journal's +0.44c),
and the "adverse-selection curve" it produced was an artifact - it even flipped
sign between sample sizes.

This avoids the join entirely. Every `placed` event in the journal carries the
bot's own book mid and a timestamp (~4 per second), so the journal already
contains a dense mid timeline from the SAME connection the fills came from. For
each fill we read that timeline at fill_time + 5/30/60s. No second connection, no
alignment gap.

The built-in control: the self-book markout at t=0 must equal the journal's own
mid_at_fill markout (they are the same book), so if t=0 reconciles, the
longer-horizon curve is real. A curve that starts at the fill-instant value and
decays toward negative IS adverse selection, measured honestly this time.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ONE_DOLLAR = 10_000
TICKS_PER_CENT = ONE_DOLLAR // 100
HORIZONS = (0.0, 1.0, 5.0, 10.0, 30.0, 60.0)


def parse_iso(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def arm_of(path: Path) -> str:
    parts = path.stem.split("_")
    return parts[2] if len(parts) >= 3 and parts[0] == "jrnl" else "?"


def mid_at(series: list[tuple[float, int]], when: float) -> int | None:
    i = bisect_left(series, (when, -1))
    return series[i][1] if i < len(series) else None


def process(path: Path, markouts, by_arm, journal_markouts):
    """One journal = one cycle = one self-consistent book timeline."""

    arm = arm_of(path)
    timeline: dict[str, list[tuple[float, int]]] = defaultdict(list)
    fills: list[dict] = []

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            event = json.loads(line)
        except ValueError:
            continue

        at = parse_iso(event.get("at"))

        if at is None:
            continue

        kind = event.get("event")

        # "mid" = the fixed-cadence snapshot (dense, unbiased); "placed" is the
        # older event-driven mid, kept as a fallback for journals recorded before
        # the snapshot existed. New journals with "mid" events give a clean curve.
        if kind in ("mid", "placed") and event.get("mid") is not None:
            timeline[event["market_ticker"]].append((at, event["mid"]))
        elif kind == "filled" and event.get("yes_price") is not None:
            fills.append(event | {"at": at})

    for series in timeline.values():
        series.sort()

    for fill in fills:
        series = timeline.get(fill["market_ticker"])

        if not series:
            continue

        sign = 1.0 if fill.get("action") == "buy" else -1.0
        price = fill["yes_price"]

        for horizon in HORIZONS:
            mid = mid_at(series, fill["at"] + horizon)

            if mid is None:
                continue

            move = sign * (mid - price) / TICKS_PER_CENT
            markouts[horizon].append(move)
            by_arm[arm][horizon].append(move)

        if fill.get("mid_at_fill") is not None:
            journal_markouts.append(sign * (fill["mid_at_fill"] - price) / TICKS_PER_CENT)


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pj")
    paths = sorted(root.glob("*.jsonl"))

    if not paths:
        print(f"no journals under {root}")
        return

    markouts: dict[float, list[float]] = defaultdict(list)
    by_arm: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    journal_markouts: list[float] = []

    for path in paths:
        process(path, markouts, by_arm, journal_markouts)

    n = len(markouts.get(0.0, []))
    print(f"{len(paths)} journals, {n} fills with a self-book timeline\n")

    if journal_markouts and markouts.get(0.0):
        jm, rec0 = st.mean(journal_markouts), st.mean(markouts[0.0])
        print("CONTROL (same book, must agree):")
        print(f"  journal mid_at_fill : {jm:+.3f}c")
        print(f"  self-book t=0       : {rec0:+.3f}c")
        ok = abs(rec0 - jm) < 0.10
        print(f"  -> {'AGREE, curve is trustworthy' if ok else 'DISAGREE, investigate before trusting'}\n")

    print("markout vs horizon (all fills, bot's own book):")
    print(f"  {'horizon':>8}{'n':>8}{'mean':>10}")

    for horizon in HORIZONS:
        vals = markouts.get(horizon)

        if vals:
            print(f"  {horizon:>6.0f}s{len(vals):>8}{st.mean(vals):>+9.3f}c")

    print("\nby A/B arm:")

    for arm in sorted(by_arm):
        cells = []

        for horizon in HORIZONS:
            vals = by_arm[arm].get(horizon)
            cells.append(f"{horizon:.0f}s {st.mean(vals):+.2f}" if vals else f"{horizon:.0f}s -")
        print(f"  {arm:<12} " + "  ".join(cells))

    print("\nStart-positive, decay-to-negative = adverse selection, and where it "
          "crosses says what to fix. Flat/positive = the fills are not the leak.")


if __name__ == "__main__":
    main()
