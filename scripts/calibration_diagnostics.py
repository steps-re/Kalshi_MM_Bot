"""Capacity and fragility of the two pooled calibration trades.

    python scripts/calibration_diagnostics.py ~/kalshi-audit/candles.jsonl \\
        [more.jsonl ...] --settled ~/kalshi-audit/settled_history.jsonl.gz

Three diagnostics, none of which changes the pre-specified trades:

1. **Capacity.** A +1.5c edge on tails is only money if contracts trade at tail
   prices. From the candle minutes where the book was actionable and the mid was
   in a trade's zone, accumulate traded volume. This is an upper bound: it
   assumes we could have been party to every print.

   The per-day figure is only meaningful scaled by the SAMPLING FRACTION, since
   the candle fetch capped each family. The previous version printed the
   unscaled number with a note telling the reader to scale it - and computed the
   market counts needed to do that, then threw them away without printing them.
   This version needs `--settled` and does the scaling itself, or refuses to
   print the column.

2. **Leave-one-out.** Recompute each pooled trade's mean dropping one cluster,
   and one series, at a time. A result that flips sign when one leaves is one
   cluster's result.

   A zero-loss cell CANNOT flip: every per-contract P&L in it is positive, so
   every leave-one-out mean is positive by construction and the test prints
   "robust" precisely where the data is weakest. Those cells now report `n/a -
   no losses` instead of `no`.

3. **What's inside "other".** The family with the most markets is a grab bag;
   this lists which series actually populate its tail and favorite zones, as
   exploration for a LATER pre-registered split, not as a result.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_core import (MIN_CONTRACTS, book_at, cluster_key,  # noqa: E402
                              load_records, trade_pnl, zone_of)
from calibration_curves import family_of  # noqa: E402

LOOKBACK = 5 * 60          # diagnostics at the 5-minute lookback only
MIN_GROUPS = 4


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def population(settled: Path) -> dict:
    """Settled, traded markets per family in the FULL corpus, for scaling."""

    counts: dict[str, int] = defaultdict(int)
    handle = (gzip.open(settled, "rt") if settled.suffix == ".gz"
              else settled.open())

    with handle:
        for line in handle:
            try:
                d = json.loads(line)
            except ValueError:
                continue

            if d.get("result") not in ("yes", "no"):
                continue

            try:
                volume = float(d.get("volume_fp") or d.get("volume") or 0)
            except (TypeError, ValueError):
                continue

            if volume <= 0:
                continue

            counts[family_of(d.get("ticker", "").split("-", 1)[0])] += 1

    return dict(counts)


def collect(records):
    """Per-market rows at the 5-minute lookback, plus in-zone traded volume."""

    rows = []
    sampled: dict[str, set] = defaultdict(set)
    other_series: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    for d in records:
        if d.get("result") not in ("yes", "no"):
            continue

        candles = d.get("candles") or []

        if not candles:
            continue

        series = d.get("series", "")
        family = family_of(series)
        day = day_of(d["close_ts"])
        won = 1 if d["result"] == "yes" else 0
        sampled[family].add(d["ticker"])
        book = book_at(candles, d["close_ts"] - LOOKBACK)

        if book is None:
            continue

        bid, ask = book
        zone = zone_of((bid + ask) / 2)

        if zone is None:
            continue

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

            if zone_of(mean_price) == zone:
                zone_volume += traded

        pnl, lost = trade_pnl(zone, bid, ask, won)
        rows.append({"family": family, "series": series, "zone": zone,
                     "day": day, "cluster": cluster_key(family, series, day),
                     "pnl": pnl, "lost": lost, "volume": zone_volume})

        if family == "other":
            slot = other_series[zone][series]
            slot[0] += 1
            slot[1] += won

    return rows, sampled, other_series


def leave_one_out(rows, key: str):
    """Mean, and the range of means dropping one `key` group at a time."""

    groups: dict = defaultdict(lambda: [0.0, 0])

    for row in rows:
        slot = groups[row[key]]
        slot[0] += row["pnl"]
        slot[1] += 1

    total_pnl = sum(v[0] for v in groups.values())
    total_n = sum(v[1] for v in groups.values())
    outs = []

    for skip in groups:
        pnl = total_pnl - groups[skip][0]
        count = total_n - groups[skip][1]

        if count:
            outs.append(pnl / count * 100)

    return total_pnl / total_n * 100, outs, len(groups)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candles", nargs="+", type=Path)
    parser.add_argument("--settled", type=Path, default=None,
                        help="full settled history, for the sampling fraction "
                             "the capacity column needs")
    args = parser.parse_args()

    records, stats = load_records(args.candles)
    print(f"{stats['rows']} rows read, {stats['duplicates']} duplicate tickers "
          f"dropped, {len(records)} unique markets\n")

    rows, sampled, other_series = collect(records)
    universe = population(args.settled) if args.settled else None

    # ---- capacity ----
    print("CAPACITY (upper bound): contracts printed in-zone in the final "
          f"{LOOKBACK // 60} minutes\n")

    if universe is None:
        print("  --settled not given, so the sampling fraction is unknown and\n"
              "  the per-day column is omitted. A per-market number without it\n"
              "  cannot be scaled to the exchange, and guessing is how a\n"
              "  capacity figure gets quoted 30x too small.\n")

    header = (f"{'family':<15}{'zone':<11}{'mkts':>6}{'days':>6}"
              f"{'contracts':>12}{'per mkt':>9}")
    print(header + (f"{'sampled':>9}{'per day':>12}" if universe else ""))

    by_zone: dict = defaultdict(
        lambda: {"contracts": 0.0, "markets": 0, "days": set()})

    for row in rows:
        slot = by_zone[(row["family"], row["zone"])]
        slot["contracts"] += row["volume"]
        slot["markets"] += 1
        slot["days"].add(row["day"])

    for (family, zone), slot in sorted(by_zone.items()):
        contracts = slot["contracts"]
        markets = slot["markets"]
        days = len(slot["days"])
        line = (f"{family:<15}{zone:<11}{markets:>6}{days:>6}"
                f"{contracts:>12.0f}{contracts / markets:>9.1f}")

        if universe:
            fraction = min(
                len(sampled[family]) / max(universe.get(family, 0), 1), 1.0)
            per_day = contracts / max(days, 1) / max(fraction, 1e-9)
            line += f"{fraction:>8.1%}{per_day:>12.0f}"

        print(line)

    print("\nNOTE: both sides of a print may or may not be counted here - "
          "`volume_fp`\nis taken at face value and its side convention has not "
          "been verified.\nZone-minutes are judged by mean trade price.")

    # ---- fragility ----
    print(f"\nLEAVE-ONE-OUT on the pooled trades ({LOOKBACK // 60}m lookback)\n")
    print(f"{'trade':<24}{'n':>6}{'loss':>6}{'full mean':>11}"
          f"{'worst-out':>11}{'best-out':>10}  {'flips?':<14}{'unit'}")

    grouped: dict = defaultdict(list)

    for row in rows:
        grouped[(row["family"], row["zone"])].append(row)
        grouped[("ALL", row["zone"])].append(row)

    for (family, zone), subset in sorted(grouped.items()):
        losses = sum(r["lost"] for r in subset)

        if len(subset) < MIN_CONTRACTS:
            continue

        for unit in ("cluster", "series"):
            full, outs, count = leave_one_out(subset, unit)

            if count < MIN_GROUPS or not outs:
                continue

            if losses == 0:
                # Every per-contract P&L is positive, so every leave-one-out
                # mean is positive. The test has one possible answer and is
                # not evidence of robustness.
                verdict = "n/a - no losses"
            else:
                verdict = ("YES" if any((o < 0) != (full < 0) for o in outs)
                           else "no")

            print(f"{family + ' ' + zone:<24}{len(subset):>6}{losses:>6}"
                  f"{full:>+10.2f}c{min(outs):>+10.2f}c{max(outs):>+9.2f}c  "
                  f"{verdict:<14}{unit} (n={count})")

    # ---- what's inside "other" ----
    print("\nEXPLORATORY: series populating the 'other' family's zones "
          "(for a future pre-registered split, NOT a result)\n")

    for zone in ("tail SELL", "fave BUY"):
        ranked = sorted(other_series[zone].items(), key=lambda kv: -kv[1][0])[:10]
        print(f"  {zone}:")

        for series, (n, wins) in ranked:
            print(f"    {series:<24}{n:>5} markets   P(yes) {wins / n:>6.1%}")


if __name__ == "__main__":
    main()
