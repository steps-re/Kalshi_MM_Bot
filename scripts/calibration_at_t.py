"""Calibration at a FIXED time before close - the clean test.

    python scripts/fetch_corpus.py --bundle candles   # corpus lives in GCS, not on disk
    python scripts/calibration_at_t.py ~/kalshi-audit/candles.jsonl [more.jsonl]

For each settled market, read the book from the 1-minute candle ending at or
before T-minus-X, and ask: given the mid then, how often did YES settle? These
are prices a trader could actually have acted on, so the last-print convergence
bias of the zeroth pass is gone.

Economics are priced from the ACTIONABLE side, fees included, both ways of
harvesting an overpriced longshot:

    taker: sell YES at the bid now, pay 0.07*p*(1-p), hold to settlement.
    maker: rest an offer at the ask, pay nothing, hold to settlement.

BOTH columns are upper bounds. The maker column because fills are adversely
selected. The taker column because candlesticks carry no DEPTH: a one-lot
phantom bid at 4c is weighted here exactly like a real book, and nothing in
this pipeline checks that the quoted price would have absorbed any size. An
earlier version of this docstring claimed the taker column was not an upper
bound. It has no basis for that claim and neither does the data.

Settlement pays no fee. The trade definitions, the clustering unit and the
error bars all live in `calibration_core` so that this script and the website
builder cannot drift apart - they already did once, disagreeing on whether a
mid of exactly 5c is a tail.

Clustering: series-by-day, except for families riding one underlying (crypto,
indices, commodities, weather), which cluster family-by-day. Fifty strikes of
one hourly ladder ride one outcome and are one draw, not fifty. Being wrong
about that once turned t=2.5 into a published t=44 in this project.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_core import (LOOKBACKS, MIN_CLUSTERS, MIN_CONTRACTS,  # noqa: E402
                              accumulate, book_at, cluster_key, decay_panel,
                              fee, load_records, summarize)
from calibration_curves import BUCKETS, family_of  # noqa: E402
from cluster_stats import clustered_pooled, loss_count_floor  # noqa: E402

MIN_BUCKET = 40


def bucket_table(records):
    """(lookback, family, bucket) -> cluster -> [yes, n, sum_bid, sum_ask]."""

    from datetime import datetime, timezone

    cells: dict = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0.0, 0.0]))
    usable = 0

    for d in records:
        if d.get("result") not in ("yes", "no"):
            continue

        candles = d.get("candles") or []

        if not candles:
            continue

        series = d.get("series", "")
        family = family_of(series)
        day = datetime.fromtimestamp(
            d["close_ts"], tz=timezone.utc).date().isoformat()
        cluster = cluster_key(family, series, day)
        won = 1 if d["result"] == "yes" else 0
        used = False

        for lookback in LOOKBACKS:
            book = book_at(candles, d["close_ts"] - lookback)

            if book is None:
                continue

            bid, ask = book
            mid = (bid + ask) / 2

            for lo, hi in BUCKETS:
                if lo <= mid < hi:
                    for fam in (family, "ALL"):
                        slot = cells[(lookback, fam, (lo, hi))][cluster]
                        slot[0] += won
                        slot[1] += 1
                        slot[2] += bid
                        slot[3] += ask

                    used = True
                    break

        usable += 1 if used else 0

    return cells, usable


def main() -> None:
    records, stats = load_records(sys.argv[1:])
    print(f"{stats['rows']} rows read, {stats['duplicates']} duplicate tickers "
          f"dropped, {len(records)} unique markets")

    cells, usable = bucket_table(records)
    print(f"{len(records)} markets, {usable} with an actionable book at "
          f">=1 lookback\n")

    for lookback in LOOKBACKS:
        minutes = lookback // 60
        families = sorted({fam for lb, fam, _ in cells if lb == lookback})

        for fam in families:
            table = [(bucket, cells[(lookback, fam, bucket)])
                     for bucket in BUCKETS
                     if (lookback, fam, bucket) in cells]
            table = [(b, groups) for b, groups in table
                     if sum(s[1] for s in groups.values()) >= MIN_BUCKET]

            if not table:
                continue

            print(f"\n== T-minus {minutes} min | {fam} ==")
            print(f"{'mid bucket':<12}{'n':>7}{'clus':>6}{'P(yes)':>10}"
                  f"{'+/-':>7}{'avg mid':>9}{'gap':>8}"
                  f"{'SELL@bid net':>14}{'REST@ask net':>14}")

            for (lo, hi), groups in table:
                sums = [s[0] for s in groups.values()]
                counts = [s[1] for s in groups.values()]
                n, clusters, win, se = clustered_pooled(sums, counts)
                # A bucket of straight zeros (or straight ones) has a clustered
                # SE of zero, which overstates certainty badly. Floor it by the
                # uncertainty in the COUNT, at cluster level.
                misses = [c - y for y, c in zip(sums, counts)]
                rare = sums if sum(sums) <= sum(misses) else misses
                se = max(se if not math.isnan(se) else 0.0,
                         loss_count_floor(rare, n))
                avg_bid = sum(s[2] for s in groups.values()) / n
                avg_ask = sum(s[3] for s in groups.values()) / n
                # The reference price is the AVERAGE MID ACTUALLY OBSERVED, not
                # the bucket's centre. Prices pile toward the low end of every
                # tail bucket, so a centre-based gap overstates tail
                # overpricing by up to 0.44c on this data - in the flattering
                # direction, in every tail bucket.
                avg_mid = (avg_bid + avg_ask) / 2
                taker_net = (avg_bid - win - fee(avg_bid)) * 100
                maker_net = (avg_ask - win) * 100
                se_str = (f"{se * 100:5.2f}" if not math.isnan(se) else "  n/a")
                print(f"{lo * 100:>4.0f}-{hi * 100:<7.0f}{n:>7}{clusters:>6}"
                      f"{win * 100:>9.2f}%{se_str:>7}{avg_mid * 100:>8.2f}c"
                      f"{(win - avg_mid) * 100:>+8.2f}"
                      f"{taker_net:>+13.2f}c{maker_net:>+13.2f}c")

    # ---- the pooled, pre-specified trades ----
    accumulated = accumulate(records)
    rows = summarize(accumulated)
    tested = len(accumulated["cells"])

    print(f"\n{'=' * 96}")
    print(f"""POOLED TRADES - two trades, fixed in advance:
  tail SELL: every market with mid <= 5c  -> sell YES at bid, hold to settle
  fave BUY:  every market with mid >= 80c -> buy YES at ask, hold to settle

The TRADES were pre-specified. The horizon and the family were not, and this
table is {tested} cells wide. `sig` uses the 95% critical value on G-1 degrees of
freedom, NOT 1.96, and makes no multiplicity adjustment: at {tested} cells you would
expect roughly {tested * 0.05:.0f} to clear it by chance alone. Read it as a screen.""")
    print(f"\n{'lookback':>9} {'family':<15}{'trade':<11}{'n':>6}{'mkts':>6}"
          f"{'clus':>6}{'loss':>6}{'lossG':>6}{'net c/contract':>16}"
          f"{'t':>7}{'crit':>6}{'sig':>5}  se from")

    for r in rows:
        print(f"{r['lookback_min']:>7}m  {r['family']:<15}{r['trade']:<11}"
              f"{r['n']:>6}{r['markets']:>6}{r['clusters']:>6}{r['losses']:>6}"
              f"{r['loss_clusters']:>6}{r['net_cents']:>+13.2f}c "
              f"+/-{r['se_cents']:.2f}{r['t']:>7.1f}{r['t_critical_95']:>6.2f}"
              f"{'  *' if r['significant_95'] else '   ':>5}  {r['se_source']}")

    dropped = tested - len(rows)
    print(f"\n{dropped} cells suppressed for n < {MIN_CONTRACTS} contracts or "
          f"G < {MIN_CLUSTERS} clusters.")

    # ---- the decay curve, with its sample turnover shown ----
    print(f"\n{'=' * 96}")
    print("""DECAY CURVES. A real mispricing should survive a longer horizon; a
convergence artifact cannot, because it was only ever measuring resolution.

But the horizons DO NOT SEE THE SAME MARKETS. A market enters a horizon only if
it had an actionable book that far out and sat in the zone at that moment, so
`%same` - the overlap between that row's markets and the longest horizon's -
is the number that says whether the curve is a curve or a cast change. The
BALANCED rows hold the market set fixed and are the only controlled comparison.""")

    for family, trade in sorted({(r["family"], r["trade"]) for r in rows}):
        panel = decay_panel(records, family, trade)

        if len(panel["rows"]) < 2:
            continue

        print(f"\n-- {family} | {trade} --")

        if panel["missing_lookbacks"]:
            missing = ", ".join(f"{m}m" for m in panel["missing_lookbacks"])
            print(f"   NO DATA at {missing}: these markets do not exist that "
                  f"far before they close. The decay test cannot be run here.")

        print(f"   {'T-':>5}{'n':>7}{'mkts':>6}{'clus':>6}{'loss':>6}"
              f"{'rate':>8}{'net':>9}{'se':>7}{'t':>7}{'%same':>7}")

        for label, series in (("raw", panel["rows"]),
                              (f"balanced (n={panel['balanced_markets']} mkts)",
                               panel["balanced"])):
            if not series:
                continue

            print(f"   {label}")

            for row in series:
                share = (f"{row['overlap_with_longest']:.0%}"
                         if row["overlap_with_longest"] is not None else "   -")
                print(f"   {row['lookback_min']:>4}m{row['n']:>7}"
                      f"{row['markets']:>6}{row['clusters']:>6}"
                      f"{row['losses']:>6}{row['loss_rate']:>8.2%}"
                      f"{row['net_cents']:>+8.2f}c{row['se_cents']:>7.2f}"
                      f"{row['t']:>7.1f}{share:>7}")

    print("""
SELL@bid net = cents per contract from selling YES at the bid as a taker and
holding to settlement, fee included.
REST@ask net = the maker version, fee-free but conditional on being filled.
Both are UPPER BOUNDS: the maker column for adverse selection, the taker column
because no depth data exists in candlesticks and no size check was made.""")


if __name__ == "__main__":
    main()
