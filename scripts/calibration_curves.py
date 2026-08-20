"""Are Kalshi's prices calibrated? Zeroth pass, from the settled-market list.

    python scripts/calibration_curves.py ~/kalshi-audit/settled_compact.jsonl.gz

For each last-traded-price bucket: how often did YES actually happen? A 4c
contract in an efficient market settles YES about 4% of the time. If it
settles less, longshots are overpriced and a RESTING bid on the NO side
(equivalently, selling the longshot) harvests the gap with zero fees on both
legs - maker entry is free and settlement is free - and no exit-fill problem,
because there is no exit.

## The bias in this pass, stated up front

`last_price` is the final print, and prices converge toward the outcome as
information arrives. Doomed markets drift to 1-3c before settling NO; sure
things drift to 97-99c before settling YES. That stuffs the tail buckets with
already-decided markets and mechanically produces the favorite-longshot
pattern - longshots "overpriced", favorites "underpriced" - even on a
perfectly efficient exchange. **This pass therefore cannot confirm the bias,
only measure bucket populations and rule the idea out** (if tails look fair
even here, with the bias helping, the idea is dead). Confirmation needs prices
at a fixed time before close: `settlement_candles.py`, stage 2.

Standard errors cluster on `event_ticker`: fifty strikes of one BTC hourly
event share one outcome path and are one draw, not fifty. This project has
been burned by exactly that error at t=44.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cluster_stats import clustered  # noqa: E402

BUCKETS = ((0.0, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05),
           (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50),
           (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.95),
           (0.95, 0.97), (0.97, 0.98), (0.98, 0.99), (0.99, 1.0))
# Families rebuilt from what actually settles, repeatedly, with populated
# tails - see the 161-series census. The first version's "other" bucket was
# 94% MVE parlay products, which carry volume but no continuous two-sided
# book: of 4,000 sampled, 2,089 had a placeholder book five minutes out and
# only 39 were actionable. They are excluded by name rather than by accident.
PARLAY_PREFIXES = ("KXMVE",)
FAMILIES = {
    # One underlying, many strikes: these do NOT diversify against each other.
    "crypto-15M": lambda s: s.endswith("15M"),
    "crypto-hourly": lambda s: s in ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD",
                                     "KXBTC", "KXETH", "KXSOL", "KXXRP"),
    # Per-game and per-player props. Genuinely independent settlements, which
    # is the whole point of the diversification argument.
    "sports-props": lambda s: (s.startswith(("KXMLB", "KXNBA", "KXNFL",
                                             "KXNCAA", "KXCLUBF", "KXWNBA"))
                               or s in ("KXSB", "KXPGATOUR")),
    "tennis": lambda s: s.startswith(("KXITF", "KXATP", "KXWTA")),
    # Daily, per-city. Correlated within a weather system, independent across
    # continents - partial diversification, unlike a single ladder.
    "weather": lambda s: (s.startswith(("KXTEMP", "KXHIGH", "KXRAIN", "KXLOW"))
                          and "INFLATION" not in s and "YTVIEWS" not in s),
    "commodities": lambda s: s.startswith(("KXGOLD", "KXSILVER", "KXWTI",
                                           "KXNG", "KXCORN", "KXCOPPER")),
    "indices": lambda s: s.startswith(("KXDJI", "KXNASDAQ", "KXINX", "KXSPX")),
    "parlay-EXCLUDE": lambda s: s.startswith(PARLAY_PREFIXES),
}


def family_of(series: str) -> str:
    for name, test in FAMILIES.items():
        if test(series):
            return name

    return "other"


def opener(path: Path):
    return gzip.open(path, "rt") if path.suffix == ".gz" else open(path)


def main() -> None:
    path = Path(sys.argv[1])
    # (family, bucket) -> {event: [wins, n]}
    cells: dict[tuple, dict] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    skipped_no_price = skipped_no_result = skipped_no_volume = rows = 0

    with opener(path) as handle:
        for line in handle:
            try:
                d = json.loads(line)
            except ValueError:
                continue

            rows += 1
            result = d.get("result")

            if result not in ("yes", "no"):
                skipped_no_result += 1
                continue

            try:
                volume = float(d.get("volume_fp") or d.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0

            if volume <= 0:
                skipped_no_volume += 1
                continue

            try:
                price = float(d["last_price_dollars"])
            except (KeyError, TypeError, ValueError):
                skipped_no_price += 1
                continue

            if not 0.0 < price < 1.0:
                skipped_no_price += 1
                continue

            series = d.get("ticker", "").split("-", 1)[0]
            event = d.get("event_ticker") or d.get("ticker", "")
            fam = family_of(series)

            for lo, hi in BUCKETS:
                if lo <= price < hi:
                    for key in ((fam, (lo, hi)), ("ALL", (lo, hi))):
                        slot = cells[key][event]
                        slot[0] += 1 if result == "yes" else 0
                        slot[1] += 1

                    break

    print(f"{rows} rows: skipped {skipped_no_result} no-result, "
          f"{skipped_no_volume} zero-volume, {skipped_no_price} no-price\n")
    print("""BIAS WARNING: prices here are LAST PRINTS, which converge toward the
outcome. The favorite-longshot pattern below is expected mechanically even on
an efficient exchange. Read gaps as an UPPER BOUND on tail overpricing; only
stage 2 (prices at fixed T-minus-close) can confirm anything.\n""")

    families = sorted({fam for fam, _ in cells})

    for fam in families:
        print(f"\n== {fam} ==")
        print(f"{'last price':<14}{'markets':>9}{'events':>8}"
              f"{'implied':>9}{'settled YES':>18}{'gap (c)':>10}")

        for lo, hi in BUCKETS:
            events = cells.get((fam, (lo, hi)))

            if not events:
                continue

            n = sum(slot[1] for slot in events.values())

            if n < 50:
                continue

            # One value per event (its mean outcome), clustered SE across
            # events. Within-event correlation is ~1 for ladder strikes.
            values = [slot[0] / slot[1] for slot in events.values()]
            weights = [slot[1] for slot in events.values()]
            # Weighted mean by event size, SE from clustered() on per-event
            # means (conservative: treats events equally for dispersion).
            win = sum(v * w for v, w in zip(values, weights)) / sum(weights)
            _, groups, _, se = clustered(values, list(events.keys()))
            mid = (lo + hi) / 2
            gap = (win - mid) * 100
            se_c = (se * 100) if not math.isnan(se) else float("nan")
            se_str = f"+/-{se_c:.2f}" if not math.isnan(se_c) else "n/a"
            print(f"{lo * 100:>5.0f}-{hi * 100:<7.0f}{n:>9}{groups:>8}"
                  f"{mid * 100:>8.1f}c{win * 100:>11.2f}% {se_str:>9}"
                  f"{gap:>+9.2f}")

    print("""
gap < 0 at low prices  = longshots settle YES less often than priced
                         (selling them / resting NO bids would have paid)
gap > 0 at high prices = favorites settle YES more often than priced
Remember the bias warning before believing either.""")


if __name__ == "__main__":
    main()
