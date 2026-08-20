"""Calibration at a FIXED time before close - the clean test.

    python scripts/calibration_at_t.py ~/kalshi-audit/candles.jsonl

For each settled market, read the book from the 1-minute candle ending at or
before T-minus-X, and ask: given the mid then, how often did YES settle? These
are prices a trader could actually have acted on, so the last-print convergence
bias of the zeroth pass is gone.

Economics are priced from the ACTIONABLE side, fees included, both ways of
harvesting an overpriced longshot:

    taker: sell YES at the bid now, pay 0.07*p*(1-p), hold to settlement.
    maker: rest an offer at the ask, pay nothing, hold to settlement.
           (Conditional on being filled - and fills are adversely selected,
           so this column is an upper bound. The taker column is not.)

Settlement pays no fee. Clustering is on the event ticker: fifty strikes of
one hourly ladder ride one outcome and are one draw, not fifty. Being wrong
about that once turned t=2.5 into a published t=44 in this project.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_curves import BUCKETS, family_of  # noqa: E402
from cluster_stats import clustered  # noqa: E402

# A RANGE, deliberately. The single most useful diagnostic in this whole study
# is how a tail trade's return decays with time-to-close. A real mispricing
# should persist as the horizon lengthens; a convergence artifact dies, because
# it was only ever measuring "the outcome is already known". Tennis ran
# +4.06c at T-2min on ZERO observed losses and -0.57c at T-60min on 50 of
# them, with the loss rate climbing monotonically 0.00% -> 9.38% in between.
# Report the curve, never a single horizon.
LOOKBACKS = (2 * 60, 5 * 60, 10 * 60, 30 * 60, 60 * 60)
MAX_SPREAD = 0.10          # book wider than 10c at T-minus = not actionable
MIN_BUCKET = 40


def book_at(candles: list[dict], when: float):
    """Bid/ask closes of the last candle ending at or before `when`."""

    best = None

    for candle in candles:
        ts = candle.get("end_period_ts")

        if ts is None or ts > when:
            continue

        if best is None or ts > best.get("end_period_ts", 0):
            best = candle

    if best is None:
        return None

    try:
        bid = float(best["yes_bid"]["close_dollars"])
        ask = float(best["yes_ask"]["close_dollars"])
    except (KeyError, TypeError, ValueError):
        return None

    # A one-sided or unopened book shows 0.001/1.00 placeholders. Not a price.
    if bid <= 0.001 or ask >= 0.9999 or ask <= bid:
        return None

    if ask - bid > MAX_SPREAD:
        return None

    return bid, ask


def fee(price: float) -> float:
    return 0.07 * price * (1.0 - price)


def main() -> None:
    path = Path(sys.argv[1])
    # (lookback, family, bucket) -> event -> [yes_count, n, sum_bid, sum_ask]
    cells: dict[tuple, dict] = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0.0, 0.0]))
    rows = usable = 0

    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue

        rows += 1
        result = d.get("result")

        if result not in ("yes", "no"):
            continue

        candles = d.get("candles") or []

        if not candles:
            continue

        family = family_of(d.get("series", ""))
        # Cluster on series x UTC day, not the event. Consecutive 15M windows
        # ride one underlying price path all day, so events are not independent
        # draws - the same correlation that inflated this project's t-stats
        # tenfold before the audit. Day-level clusters are the conservative
        # unit this data supports.
        from datetime import datetime, timezone
        day = datetime.fromtimestamp(d["close_ts"], tz=timezone.utc).date()
        event = f"{d.get('series', '')}|{day}"
        won = 1 if result == "yes" else 0
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
                        slot = cells[(lookback, fam, (lo, hi))][event]
                        slot[0] += won
                        slot[1] += 1
                        slot[2] += bid
                        slot[3] += ask

                    used = True
                    break

        usable += 1 if used else 0

    print(f"{rows} markets, {usable} with an actionable book at >=1 lookback\n")

    for lookback in LOOKBACKS:
        minutes = lookback // 60
        families = sorted({fam for lb, fam, _ in cells if lb == lookback})

        for fam in families:
            table = [(bucket, cells[(lookback, fam, bucket)])
                     for bucket in BUCKETS
                     if (lookback, fam, bucket) in cells]
            table = [(b, events) for b, events in table
                     if sum(s[1] for s in events.values()) >= MIN_BUCKET]

            if not table:
                continue

            print(f"\n== T-minus {minutes} min | {fam} ==")
            print(f"{'mid bucket':<12}{'n':>7}{'evts':>6}{'P(yes)':>10}"
                  f"{'+/-':>7}{'gap':>8}{'SELL@bid net':>14}{'REST@ask net':>14}")

            for (lo, hi), events in table:
                n = sum(s[1] for s in events.values())
                values = [s[0] / s[1] for s in events.values()]
                weights = [s[1] for s in events.values()]
                win = sum(v * w for v, w in zip(values, weights)) / sum(weights)
                _, groups, _, se = clustered(values, list(events.keys()))
                # A bucket of straight zeros has a clustered SE of zero, which
                # overstates certainty badly. Jeffreys upper bound instead:
                # with 0 successes in n, the 95% upper limit is ~1.92/n before
                # clustering, so report at least that.
                wins_total = sum(s[0] for s in events.values())

                if wins_total == 0 or wins_total == n:
                    se = max(se if not math.isnan(se) else 0.0, 1.92 / n)
                avg_bid = sum(s[2] for s in events.values()) / n
                avg_ask = sum(s[3] for s in events.values()) / n
                mid = (lo + hi) / 2
                # Sell YES at the bid, pay taker fee, pay out `win` on average.
                taker_net = (avg_bid - win - fee(avg_bid)) * 100
                # Rest at the ask for free; conditional on a fill.
                maker_net = (avg_ask - win) * 100
                se_str = (f"{se * 100:5.2f}" if not math.isnan(se) else "  n/a")
                print(f"{lo * 100:>4.0f}-{hi * 100:<7.0f}{n:>7}{groups:>6}"
                      f"{win * 100:>9.2f}%{se_str:>7}"
                      f"{(win - mid) * 100:>+8.2f}"
                      f"{taker_net:>+13.2f}c{maker_net:>+13.2f}c")

    # ---- the pooled, pre-specified trades ----
    print(f"\n{'=' * 78}")
    print("""POOLED TRADES - one number each, not sixteen buckets to pick from.
  tail SELL: every market with mid <= 5c  -> sell YES at bid, hold to settle
  fave BUY:  every market with mid >= 80c -> buy YES at ask, hold to settle""")
    print(f"\n{'lookback':>9} {'family':<15}{'trade':<11}{'n':>6}{'clusters':>9}"
          f"{'losses':>8}{'net c/contract':>16}{'t':>7}")

    for lookback in LOOKBACKS:
        for fam in sorted({f for lb, f, _ in cells if lb == lookback}):
            for label, wanted, side in (
                ("tail SELL", lambda lo, hi: hi <= 0.05 + 1e-9, "sell"),
                ("fave BUY", lambda lo, hi: lo >= 0.80 - 1e-9, "buy"),
            ):
                merged: dict[str, list] = defaultdict(
                    lambda: [0.0, 0, 0, 0.0])

                for (lo, hi) in BUCKETS:
                    if not wanted(lo, hi):
                        continue

                    events = cells.get((lookback, fam, (lo, hi)))

                    if not events:
                        continue

                    for event, slot in events.items():
                        wins, count, sum_bid, sum_ask = slot

                        # Per-contract realised P&L of the pooled trade, in
                        # dollars, accumulated per cluster so the clustered SE
                        # is on the money metric itself.
                        if side == "sell":
                            pnl = (sum_bid - wins
                                   - fee(sum_bid / count) * count)
                            # A "loss" is the rare event that decides this
                            # trade: the tail actually came in.
                            losses = wins
                            loss_size = 1.0 - sum_bid / count
                        else:
                            pnl = (wins - sum_ask
                                   - fee(sum_ask / count) * count)
                            losses = count - wins
                            loss_size = sum_ask / count

                        merged[event][0] += pnl
                        merged[event][1] += count
                        merged[event][2] += losses
                        # POTENTIAL loss magnitude, accrued for every
                        # contract - not just the ones that lost. With
                        # zero observed losses the average of observed
                        # ones is zero, which silently disabled the floor.
                        merged[event][3] += count * loss_size

                total = sum(v[1] for v in merged.values())

                if total < 60:
                    continue

                per = [v[0] / v[1] for v in merged.values()]
                weights = [v[1] for v in merged.values()]
                mean = sum(p * w for p, w in zip(per, weights)) / sum(weights)
                _, groups, _, se = clustered(per, list(merged.keys()))
                losses = sum(v[2] for v in merged.values())
                loss_cost = (sum(v[3] for v in merged.values())
                             / max(total, 1))

                # RULE OF THREE, in money. With few or no losses observed, the
                # cluster means are near-identical, the clustered SE collapses
                # and t explodes - tennis printed t=24.6 off ZERO losses in 344
                # contracts. The real uncertainty is in the loss RATE: the
                # Poisson sd on a count of k is sqrt(k), and for k=0 the 95%
                # upper bound is 3, i.e. an sd of about 1.5. Convert that count
                # uncertainty into per-contract dollars and never report a
                # tighter error bar than it allows.
                count_sd = math.sqrt(losses) if losses else 1.53
                floor = count_sd * loss_cost / max(total, 1)
                se = max(se if not math.isnan(se) else 0.0, floor)
                t = mean / se if se else float("nan")
                print(f"{lookback // 60:>7}m  {fam:<15}{label:<11}{total:>6}"
                      f"{groups:>9}{losses:>8}{mean * 100:>+13.2f}c "
                      f"+/-{se * 100:.2f}{t:>7.1f}")

    print("""
SELL@bid net = cents per contract from selling YES at the bid as a taker and
holding to settlement, fee included. Positive and significant at low prices =
the longshot bias is real and harvestable even paying the fee.
REST@ask net = the maker version, fee-free but conditional on being filled;
adverse selection makes it an upper bound.""")


if __name__ == "__main__":
    main()
