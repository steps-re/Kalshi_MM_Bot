"""Capacity and fragility of the two pooled calibration trades.

    python scripts/calibration_diagnostics.py ~/kalshi-audit/candles.jsonl

Three diagnostics, none of which changes the pre-specified trades:

1. **Capacity.** A +1.5c edge on tails is only money if contracts trade at
   tail prices. From the candle minutes where the book was actionable and the
   mid was in a trade's zone, accumulate traded volume. This counts BOTH
   sides of every print and assumes we could have been party to all of it, so
   it is a hard upper bound on harvestable size.

2. **Leave-one-day-out.** Recompute each pooled trade's mean dropping one UTC
   day at a time. A result that flips sign when one day leaves is one day's
   result. This is fragility reporting, not selection - the trades stay as
   pre-specified.

3. **What's inside "other".** The family with the most markets is a grab bag;
   this lists which series actually populate its tail and favorite zones, as
   exploration for a LATER pre-registered split, not as a result.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_at_t import LOOKBACKS, book_at, fee  # noqa: E402
from calibration_curves import family_of  # noqa: E402

TAIL = 0.05
FAVE = 0.80
LOOKBACK = 5 * 60          # diagnostics at the 5-minute lookback only


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def main() -> None:
    path = Path(sys.argv[1])
    # capacity: family -> zone -> [tail_volume_contracts, markets, days set]
    volume: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0, set()]))
    # fragility: (family, zone) -> day -> [pnl_dollars, contracts]
    pnl_by_day: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    # exploration: series -> [n, wins] inside each zone of "other"
    other_series: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    markets_per_day: dict = defaultdict(set)

    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue

        if d.get("result") not in ("yes", "no"):
            continue

        candles = d.get("candles") or []

        if not candles:
            continue

        family = family_of(d.get("series", ""))
        day = day_of(d["close_ts"])
        won = 1 if d["result"] == "yes" else 0
        markets_per_day[family].add((day, d["ticker"]))
        book = book_at(candles, d["close_ts"] - LOOKBACK)

        if book is None:
            continue

        bid, ask = book
        mid = (bid + ask) / 2
        zone = ("tail" if mid <= TAIL else "fave" if mid >= FAVE else None)

        if zone is None:
            continue

        # Capacity: volume printed in the final LOOKBACK window while the mid
        # stayed in the zone (per-minute mean price as the zone test).
        zone_volume = 0.0

        for candle in candles:
            ts = candle.get("end_period_ts")

            if ts is None or not d["close_ts"] - LOOKBACK <= ts <= d["close_ts"]:
                continue

            try:
                mean_price = float(candle["price"]["mean_dollars"])
                traded = float(candle.get("volume_fp") or 0)
            except (KeyError, TypeError, ValueError):
                continue

            in_zone = (mean_price <= TAIL if zone == "tail"
                       else mean_price >= FAVE)

            if in_zone:
                zone_volume += traded

        slot = volume[family][zone]
        slot[0] += zone_volume
        slot[1] += 1
        slot[2].add(day)

        # Fragility: the same per-contract P&L as the pooled test.
        if zone == "tail":
            pnl = bid - won - fee(bid)
        else:
            pnl = won - ask - fee(ask)

        day_slot = pnl_by_day[(family, zone)][day]
        day_slot[0] += pnl
        day_slot[1] += 1
        all_slot = pnl_by_day[("ALL", zone)][day]
        all_slot[0] += pnl
        all_slot[1] += 1

        if family == "other":
            series_slot = other_series[zone][d.get("series", "?")]
            series_slot[0] += 1
            series_slot[1] += won

    # ---- capacity ----
    print("CAPACITY (upper bound): contracts printed in-zone in the final "
          f"{LOOKBACK // 60} minutes, sampled markets only\n")
    print(f"{'family':<15}{'zone':<6}{'mkts':>6}{'days':>6}"
          f"{'contracts':>12}{'per mkt':>9}{'per day':>10}")

    for family in sorted(volume):
        for zone in ("tail", "fave"):
            contracts, markets, days = volume[family].get(zone, (0.0, 0, set()))

            if not markets:
                continue

            per_day = contracts / max(len(days), 1)
            print(f"{family:<15}{zone:<6}{markets:>6}{len(days):>6}"
                  f"{contracts:>12.0f}{contracts / markets:>9.1f}"
                  f"{per_day:>10.0f}")

    print("""
NOTE: sampled markets only (the candle fetch capped each family), both sides
of every print counted, and zone-minutes judged by mean trade price. Scale
per-day numbers up by the sampling fraction for the family before quoting.""")

    # ---- fragility ----
    print("\nLEAVE-ONE-DAY-OUT on the pooled trades (5m lookback)\n")
    print(f"{'trade':<24}{'days':>5}{'full mean':>11}{'worst-day-out':>15}"
          f"{'best-day-out':>14}{'sign flips?':>12}")

    for (family, zone), days in sorted(pnl_by_day.items()):
        total_pnl = sum(v[0] for v in days.values())
        total_n = sum(v[1] for v in days.values())

        if total_n < 60 or len(days) < 4:
            continue

        full = total_pnl / total_n * 100
        outs = []

        for skip in days:
            pnl = total_pnl - days[skip][0]
            count = total_n - days[skip][1]

            if count:
                outs.append(pnl / count * 100)

        flips = any((o < 0) != (full < 0) for o in outs)
        print(f"{family + ' ' + zone:<24}{len(days):>5}{full:>+10.2f}c"
              f"{min(outs):>+14.2f}c{max(outs):>+13.2f}c"
              f"{'YES' if flips else 'no':>12}")

    # ---- what's inside "other" ----
    print("\nEXPLORATORY: series populating the 'other' family's zones "
          "(for a future pre-registered split, NOT a result)\n")

    for zone in ("tail", "fave"):
        rows = sorted(other_series[zone].items(), key=lambda kv: -kv[1][0])[:10]
        print(f"  {zone}:")

        for series, (n, wins) in rows:
            print(f"    {series:<24}{n:>5} markets   P(yes) {wins / n:>6.1%}")


if __name__ == "__main__":
    main()
