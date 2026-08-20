"""What the live test actually measured.

    python scripts/taker_live_report.py /var/tmp/taker_live_test.jsonl

The question the recorded-book audit could not answer was whether a live order
gets the price that was showing when the signal fired. This reads the live
journal and answers it, plus the two things that decide whether the cell is
worth trading at size: how often we fill at all, and how often we fill as a
maker (which is free) rather than a taker.

Reports by order size, because one contract proves the price and says nothing
about depth. A clean result at 1 and a degraded one at 25 is the expected shape
if the edge is real but thin, and that is the distinction that matters.
"""

from __future__ import annotations

import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

# Measured on both recorded corpora for the KXBTCD 2-5c near-expiry cell: a
# resting exit that fills is worth +0.69c and one forced to cross is worth
# -0.50c, which gives an apparent break-even fill rate of 42%.
#
# That 42% is NOT a valid pass mark and this script no longer treats it as one.
# It blends E[touch over ALL trades] with E[cross over ALL trades] at the fill
# rate, which assumes the trades that fill are a random sample. They are not:
# you fill precisely when the market comes to you, so the filled trades are
# systematically the ones that went against you. `exit_fill_study.py` prices
# each trigger on the path it would really have taken, and that is the number
# to compare a live run against.
#
# Kept only to show the reference point and how far the live rate sits from it.
TOUCH_CENTS = 0.694
CROSS_CENTS = -0.509
NAIVE_BREAK_EVEN = -CROSS_CENTS / (TOUCH_CENTS - CROSS_CENTS)
# From the corrected exit_fill_study on the original archive, 598 triggers over
# 84 markets, clustered on the ticker. The truth is inside this bracket and
# recorded books cannot narrow it further, because snapshots cannot see trades.
OFFLINE_BRACKET = (-0.4, +1.1)
# Below this the pre-registration says print the count and no mean.
MIN_TRIPS_TO_REPORT = 30


def load(path: Path) -> list[dict]:
    rows = []

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            rows.append(json.loads(line))
        except ValueError:
            continue

    return rows


def summarise(label: str, rows: list[dict]) -> None:
    sent = [r for r in rows if r.get("executed") or r.get("filled") is not None]
    filled = [r for r in rows if (r.get("filled") or 0) > 0]
    slips = [r["slippage_cents"] for r in filled if "slippage_cents" in r]
    makers = [r for r in filled if r.get("was_maker")]
    fees = [r.get("fees_dollars", 0.0) for r in filled]
    pnl = [r["pnl_cents"] for r in rows if "pnl_cents" in r]

    print(f"\n{label}")
    print(f"  signals            {len(rows)}")
    print(f"  orders sent        {len(sent)}")

    if not sent:
        print("  (nothing was sent - dry run, or no window)")
        return

    print(f"  filled             {len(filled)}  ({len(filled) / len(sent):.0%} of sent)")

    if not filled:
        return

    print(f"  filled as MAKER    {len(makers)}  ({len(makers) / len(filled):.0%}, "
          f"zero fee)")
    print(f"  fees paid          ${sum(fees):.4f} total, "
          f"${sum(fees) / len(filled):.4f} per fill")

    if slips:
        # Sign convention: slippage is what the fill cost US relative to the
        # displayed touch. POSITIVE is against us, NEGATIVE is price
        # improvement. Treating a negative mean as bad would report a favourable
        # fill as a problem, which is how a good result gets thrown away.
        against = [s for s in slips if s > 0]
        better = [s for s in slips if s < 0]
        print(f"  SLIPPAGE           mean {st.mean(slips):+.3f}c   "
              f"{sum(1 for s in slips if s == 0)}/{len(slips)} exact, "
              f"{len(better)} improved, {len(against)} worse")

        if against:
            print(f"    worst against us   {max(against):+.3f}c")

        mean_slip = st.mean(slips)

        if mean_slip <= 0.05:
            note = ("fills land on the displayed touch or better. The audit's "
                    "entry assumption holds.")
        else:
            note = (f"fills run {mean_slip:.3f}c against us on average. Subtract "
                    f"that from every measured edge.")

        print(f"    -> {note}")

    if pnl:
        print(f"  balance change     {sum(pnl):+d}c over {len(pnl)} completed trades "
              f"({st.mean(pnl):+.2f}c each)")
        print("    Note: this is realised cash including settlement, not the "
              "30-second markout the audit measured.")

    exits = defaultdict(int)

    for row in filled:
        exits[row.get("exit", "none")] += 1

    if exits:
        print(f"  exits              {dict(exits)}")

    rested = exits.get("rested", 0)
    attempted = rested + exits.get("partial", 0)

    if attempted:
        rate = rested / attempted
        print(f"\n  PASSIVE EXIT FILL RATE   {rested}/{attempted} = {rate:.0%}")
        print(f"    naive break-even         {NAIVE_BREAK_EVEN:.0%}  "
              f"(NOT a pass mark - see below)")

        if attempted < 20:
            print(f"    {attempted} exits is far too few to call anything. The "
                  f"interval on this rate is wide.")

        print(f"""
    The 42% figure blends the touch and cross outcomes at the fill rate, which
    assumes the trades that fill are a random sample of all trades. They are
    not - you fill when the market comes to you - so comparing this rate
    against 42% overstates the strategy whenever the fill rate is high.

    The number to beat is realised cash per completed trade, above. Offline,
    priced conditionally on 598 triggers across 84 markets, this cell brackets
    {OFFLINE_BRACKET[0]:+.1f}c to {OFFLINE_BRACKET[1]:+.1f}c per trade depending on how
    many of the resting exits truly filled. A live run's job is to land inside
    that bracket and say where.""")


def shadow_report(rows: list[dict]) -> None:
    """Signal quality from the untraded population.

    These cost nothing and there are many more of them than traded rows, so
    they carry most of the statistical weight for the question "does the signal
    forecast", as opposed to "can we monetise it".
    """

    shadows = [r for r in rows if r.get("kind") == "shadow" and r.get("entry_book")]

    if not shadows:
        return

    print(f"\n{'=' * 62}\nSHADOW SIGNALS (followed, never traded): {len(shadows)}")
    print(f"{'horizon':>9}{'n':>7}{'signed mid move':>18}{'% favourable':>14}")

    for horizon in ("5", "15", "30", "60", "120"):
        moves = []

        for row in shadows:
            book = (row.get("forward") or {}).get(horizon)

            if not book:
                continue

            sign = 1.0 if row["buying"] else -1.0
            moves.append(sign * (book["mid"] - row["entry_book"]["mid"]) / 100.0)

        if len(moves) >= 5:
            good = sum(1 for m in moves if m > 0) / len(moves)
            print(f"{horizon + 's':>9}{len(moves):>7}{st.mean(moves):>+16.3f}c"
                  f"{good:>13.0%}")

    print("  These are forecasts, not P&L: no spread paid, no fee, no exit.")


def arm_report(rows: list[dict]) -> None:
    """Did the randomised parameters matter? This is the causal part."""

    trades = [r for r in rows if r.get("kind") == "trade" and (r.get("filled") or 0) > 0]

    if not trades or "arm_rest" not in trades[0]:
        return

    print(f"\n{'=' * 62}\nRANDOMISED ARMS")
    by_rest: dict[float, list[dict]] = defaultdict(list)

    for row in trades:
        by_rest[row.get("arm_rest", 0.0)].append(row)

    print(f"\n{'rest window':>13}{'exits':>8}{'filled passively':>19}{'implied edge':>15}")

    for arm in sorted(by_rest):
        group = by_rest[arm]
        attempts = [r for r in group if r.get("exit") in ("rested", "partial")]

        if not attempts:
            continue

        rested = sum(1 for r in attempts if r.get("exit") == "rested")
        rate = rested / len(attempts)
        edge = rate * TOUCH_CENTS + (1 - rate) * CROSS_CENTS
        print(f"{arm:>11.0f}s{len(attempts):>8}{rested}/{len(attempts)} = {rate:>6.0%}"
              f"{edge:>+14.3f}c")

    print("  A rising fill rate with a longer rest is the result that would make")
    print("  this tradeable. Flat means resting longer only adds exposure.")
    times = [r["fill_seconds"] for r in trades if r.get("fill_seconds") is not None]

    if times:
        print(f"\n  time-to-fill when it filled: median {st.median(times):.0f}s, "
              f"max {max(times):.0f}s  (n={len(times)})")

    by_hold: dict[float, list[int]] = defaultdict(list)

    for row in trades:
        if "pnl_cents" in row:
            by_hold[row.get("arm_hold", 0.0)].append(row["pnl_cents"])

    if len(by_hold) > 1:
        print(f"\n{'hold':>8}{'trades':>9}{'mean cash':>12}")

        for arm in sorted(by_hold):
            vals = by_hold[arm]
            print(f"{arm:>6.0f}s{len(vals):>9}{st.mean(vals):>+11.2f}c")


def bracket_verdict(traded: list[dict]) -> None:
    """The pre-registered primary endpoint, and nothing else.

    `docs/pre-registration-exit-fill.md` fixes the decision rule before the run:
    where in the offline bracket does the live mean land, and does its interval
    exclude zero? The rule is applied here mechanically so it cannot be
    reinterpreted once the numbers are in, which is how this project's taker
    scan and gate study both went wrong.
    """

    executed = [r for r in traded if "pnl_cents" in r]
    # Amendment 1: a trade abandoned to settlement ('no book') carries the mark
    # at abandonment, not its P&L - the position resolves at 0 or 100 after the
    # journal row is written. Excluded from the mean, counted against a 10% gate.
    stranded = [r for r in executed if r.get("exit") == "no book"]
    clean = [r for r in executed if r.get("exit") != "no book"]
    pnl = [r["pnl_cents"] for r in clean]
    n = len(pnl)

    print("\n" + "=" * 72)
    print("PRIMARY ENDPOINT (pre-registered)")

    if stranded:
        at_risk = sum(r.get("equiv", 0) for r in stranded) / 100.0
        share = len(stranded) / max(len(executed), 1)
        print(f"  settle-outs       {len(stranded)} of {len(executed)} executed "
              f"({share:.0%}), {at_risk:.0f}c at risk - EXCLUDED from the mean "
              f"(Amendment 1)")

        if len(executed) >= MIN_TRIPS_TO_REPORT and share > 0.10:
            print("""
  -> INSTRUMENT-LIMITED: over 10% of executed trades could not complete the
     round trip the offline study prices. Per Amendment 1 this run cannot be
     compared to the bracket. No verdict follows.""")
            return
    print(f"  offline bracket   {OFFLINE_BRACKET[0]:+.1f}c to {OFFLINE_BRACKET[1]:+.1f}c "
          f"per round trip")
    print(f"  completed trips   {n}")

    if n < MIN_TRIPS_TO_REPORT:
        print(f"\n  Under {MIN_TRIPS_TO_REPORT} completed round trips. The "
              f"pre-registration says report the count and stop, so no mean is "
              f"printed here. Keep running.")
        return

    mean = st.mean(pnl)
    sd = st.stdev(pnl) if n > 1 else float("nan")
    half = 1.96 * sd / math.sqrt(n)
    low, high = mean - half, mean + half
    print(f"  realised mean     {mean:+.3f}c  95% CI [{low:+.3f}, {high:+.3f}]  "
          f"(sd {sd:.2f}c)")
    print(f"  precision needed for +/-0.50c: ~{(1.96 * sd / 0.5) ** 2:.0f} trips; "
          f"for +/-0.25c: ~{(1.96 * sd / 0.25) ** 2:.0f}")

    if low > 0:
        print("""
  -> UPPER HALF OF THE BRACKET. The interval excludes zero and the mean is
     positive: the resting exit fills often enough that the cell pays at one
     contract. Pre-registered action: escalate to 10 contracts and re-run.
     Sign is settled; size is now the open question.""")
    elif high < 0:
        print("""
  -> CONSERVATIVE END. The interval excludes zero and the mean is negative. The
     round trip does not pay at one contract, and size cannot rescue it because
     depth only makes the crossing worse. Pre-registered action: the cell is
     dead. Stop.""")
    else:
        print("""
  -> UNDECIDED at this sample size. The interval contains zero. Per the
     pre-registration, do NOT read the sign of the mean: a positive-looking
     number with an interval spanning zero is exactly the reading that produced
     three disjoint winner lists in the taker scan. Either keep running to the
     trip count above, or report it as undecided.""")


def direction_split(traded: list[dict]) -> None:
    """The secondary hypothesis, reported with the power it does not have."""

    buys = [r["pnl_cents"] for r in traded
            if r.get("buying") and "pnl_cents" in r]
    sells = [r["pnl_cents"] for r in traded
             if r.get("buying") is False and "pnl_cents" in r]

    print("\n" + "=" * 72)
    print("SECONDARY: direction split (pre-registered as UNDERPOWERED)")

    if len(buys) < 2 or len(sells) < 2:
        print(f"  bid-heavy {len(buys)}, ask-heavy {len(sells)}. Too few to "
              f"report either way.")
        return

    pooled = st.stdev(buys + sells)
    needed = 2 * (1.96 + 0.84) ** 2 * pooled ** 2 / 0.68 ** 2
    print(f"  bid-heavy (buy)   n={len(buys):>4}  mean {st.mean(buys):+.3f}c")
    print(f"  ask-heavy (sell)  n={len(sells):>4}  mean {st.mean(sells):+.3f}c")
    print(f"""
  Detecting the 0.68c split the archive suggests needs ~{needed:.0f} trades PER
  ARM at this dispersion. This run has {min(len(buys), len(sells))} in the
  smaller arm. Whatever these two numbers do, they do not test it. A null here
  is not evidence of absence, and the archive's 2.5x split did not replicate on
  the recovered corpus either. Do not restrict the strategy to one side on the
  strength of this table.""")


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/tmp/taker_live_test.jsonl")

    if not path.exists():
        sys.exit(f"no journal at {path}")

    rows = load(path)

    if not rows:
        sys.exit("journal is empty")

    print(f"{len(rows)} journal rows from {path}")
    traded = [r for r in rows if r.get("kind") != "shadow"]
    summarise("TRADED", traded)
    bracket_verdict(traded)
    direction_split(traded)
    arm_report(rows)
    shadow_report(rows)
    by_size: dict[int, list[dict]] = defaultdict(list)

    for row in traded:
        by_size[int(row.get("size", 1))].append(row)

    if len(by_size) > 1:
        for size in sorted(by_size):
            summarise(f"AT {size} CONTRACT(S)", by_size[size])

    buys = [r for r in traded if r.get("buying")]
    sells = [r for r in traded if r.get("buying") is False]

    if buys and sells:
        summarise("BUY side", buys)
        summarise("SELL side", sells)


if __name__ == "__main__":
    main()
