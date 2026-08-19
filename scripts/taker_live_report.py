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
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

# Measured on both recorded corpora, independently and in agreement, for the
# KXBTCD 2-5c near-expiry cell: a resting exit that fills is worth +0.69c and
# one forced to cross is worth -0.50c. So the strategy lives or dies on how
# often the resting exit trades.
TOUCH_CENTS = 0.694
CROSS_CENTS = -0.509
BREAK_EVEN_FILL_RATE = -CROSS_CENTS / (TOUCH_CENTS - CROSS_CENTS)


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

    # The number that decides the strategy. Both recorded corpora agree that a
    # resting exit filling at the touch is worth about +0.69c and being forced
    # to cross is worth about -0.50c, so the break-even passive fill rate is
    # 0.50 / (0.69 + 0.50) = 42%. Above that the cell pays, below it does not.
    rested = exits.get("rested", 0)
    attempted = rested + exits.get("partial", 0)

    if attempted:
        rate = rested / attempted
        print(f"\n  PASSIVE EXIT FILL RATE   {rested}/{attempted} = {rate:.0%}")
        print(f"    break-even needs         {BREAK_EVEN_FILL_RATE:.0%}")
        blended = (rate * TOUCH_CENTS + (1 - rate) * CROSS_CENTS)
        print(f"    implied edge             {blended:+.3f}c per trade")

        if attempted < 20:
            print(f"    {attempted} exits is far too few to call. The interval on this "
                  f"rate is wide.")
        elif rate >= BREAK_EVEN_FILL_RATE:
            print("    -> above break-even. The cell pays.")
        else:
            print("    -> below break-even. The round trip costs more than the "
                  "forecast is worth.")


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
