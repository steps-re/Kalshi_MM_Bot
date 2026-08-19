"""Would the resting exit have filled? The 42% question, on 100 hours of books.

    python scripts/exit_fill_study.py ~/kalshi-audit/recs2 --hold 30 --rest 45

The candidate cell pays +0.69c when the exit rests and fills, and -0.51c when it
has to cross, so it clears costs only if a resting exit fills **42%** of the
time. Live that number arrives a handful of exits at a time. The recorded books
already contain the answer for tens of thousands of exits, and nobody has asked
them.

## What a fill actually requires

Posting a sell at price P puts you at the back of the queue at P. You fill only
if buyers take everything ahead of you and then reach you. Book snapshots cannot
see trades, so this reports a **bracket**:

    optimistic   the opposite touch reached P at some point, so a counterparty
                 was willing to trade there. Ignores the queue, so it is an
                 upper bound on fill probability.
    queued       the same, AND the resting size at P fell to zero at some point,
                 meaning the whole level cleared and a back-of-queue order would
                 have traded. Cancellations look identical to trades in book
                 data, so this is not a lower bound, but it is much closer.

If even the optimistic number is under 42%, the strategy is dead and no amount
of live sampling will rescue it. If the queued number is over 42%, it is alive.
Between the two, the live test decides.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics as st
import sys
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402

TICKS_PER_CENT = ONE_DOLLAR // 100
from taker_extract import (  # noqa: E402
    MAX_SPREAD_TICKS,
    parse_utc,
    walk,
)

MIN_OBI = 0.9
ENTRY_MIN = int(0.02 * ONE_DOLLAR)
ENTRY_MAX = int(0.05 * ONE_DOLLAR)
PHASE_MAX = 900.0
PHASE_MIN = 0.0
MIN_CROSSABLE = 10
COOLDOWN = 5.0
SERIES = "KXBTCD"


def fee_cents(price_ticks: float) -> float:
    p = price_ticks / ONE_DOLLAR
    return 7.0 * p * (1.0 - p)


def simulate(series, offsets, i, hold: float, rest: float, buying: bool,
             entry: int):
    """Post the exit at the touch after `hold`, watch `rest` seconds of book.

    Returns fills, timing, AND the P&L of the path actually taken - which is the
    correction that matters. The break-even arithmetic elsewhere blends
    E[touch over ALL trades] with E[cross over ALL trades] at the fill rate, and
    that is wrong: you fill precisely when the market comes to you, so the
    filled trades are not a random sample. Here each trigger contributes the one
    outcome it would really have had.
    """

    start = bisect_left(offsets, offsets[i] + hold, i + 1)

    if start >= len(series):
        return None

    # The price we would rest at: a long exits by selling at the ask, a short
    # by buying at the bid.
    _, _, _, bid_at_exit, ask_at_exit, _, _ = series[start]
    price = ask_at_exit if buying else bid_at_exit
    deadline = offsets[start] + rest

    if offsets[-1] < deadline:
        return None                      # recording ends before the rest window

    reached = False
    cleared = False
    when = None
    last_bid, last_ask = bid_at_exit, ask_at_exit

    for j in range(start + 1, len(series)):
        offset, _mid, _obi, bid, ask, bid_sz, ask_sz = series[j]

        if offset > deadline:
            break

        last_bid, last_ask = bid, ask

        if buying:
            # Resting a SELL at `price`. A buyer reaching it means best_bid >= price.
            if bid >= price:
                reached = True
                when = when if when is not None else offset - offsets[start]
            # The level clears when the ask side at our price is gone: either
            # the best ask has moved above us, or nothing is left there.
            if ask > price or (ask == price and ask_sz == 0):
                cleared = True
        else:
            if ask <= price:
                reached = True
                when = when if when is not None else offset - offsets[start]

            if bid < price or (bid == price and bid_sz == 0):
                cleared = True

    sign = 1.0 if buying else -1.0
    entry_fee = fee_cents(entry)
    # If it fills, we exit at the price we rested at. If not, we cross at
    # whatever the far touch is when the rest window expires.
    filled_pnl = sign * (price - entry) / TICKS_PER_CENT - entry_fee
    crossed_at = last_bid if buying else last_ask
    crossed_pnl = (sign * (crossed_at - entry) / TICKS_PER_CENT
                   - entry_fee - fee_cents(crossed_at))
    return (reached, reached and cleared, when, filled_pnl, crossed_pnl)


async def study(rec_dir: Path, holds, rests) -> None:
    recordings = sorted(p for p in rec_dir.iterdir() if (p / "manifest.json").exists())
    # (hold, rest) -> [optimistic, queued, total]
    tally: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
    times: dict[tuple, list] = defaultdict(list)
    realised: dict[tuple, dict] = defaultdict(
        lambda: {"optimistic": [], "queued": [], "never": []})
    triggers = 0

    for index, rec in enumerate(recordings, 1):
        try:
            samples, _span, manifest = await walk(rec)
        except Exception:  # noqa: BLE001
            continue

        started = parse_utc(manifest.started_at_utc).timestamp()
        closes = {
            ticker: parse_utc(value).timestamp()
            for ticker, value in (manifest.metadata or {}).get(
                "close_times_utc", {}).items()
        }

        for ticker, series in samples.items():
            if ticker.split("-", 1)[0] != SERIES:
                continue

            series.sort(key=lambda row: row[0])
            offsets = [s[0] for s in series]
            close_at = closes.get(ticker)

            if close_at is None:
                continue

            last = -1e9

            for i, row in enumerate(series):
                offset, _mid, obi, bid, ask, bid_sz, ask_sz = row

                if ask - bid > MAX_SPREAD_TICKS or abs(obi) < MIN_OBI:
                    continue

                if offset - last < COOLDOWN:
                    continue

                to_close = close_at - (started + offset)

                if not PHASE_MIN <= to_close <= PHASE_MAX:
                    continue

                buying = obi > 0
                entry = ask if buying else bid
                equiv = entry if buying else ONE_DOLLAR - entry

                if not ENTRY_MIN <= equiv <= ENTRY_MAX:
                    continue

                if (ask_sz if buying else bid_sz) / COUNT_SCALE < MIN_CROSSABLE:
                    continue

                last = offset
                triggers += 1

                for hold in holds:
                    for rest in rests:
                        result = simulate(series, offsets, i, hold, rest,
                                          buying, entry)

                        if result is None:
                            continue

                        optimistic, queued, when, filled_pnl, crossed_pnl = result
                        slot = tally[(hold, rest)]
                        slot[0] += 1 if optimistic else 0
                        slot[1] += 1 if queued else 0
                        slot[2] += 1
                        # The realised path under each fill assumption.
                        realised[(hold, rest)]["optimistic"].append(
                            filled_pnl if optimistic else crossed_pnl)
                        realised[(hold, rest)]["queued"].append(
                            filled_pnl if queued else crossed_pnl)
                        realised[(hold, rest)]["never"].append(crossed_pnl)

                        if when is not None:
                            times[(hold, rest)].append(when)

        if index % 40 == 0:
            print(f"  {index}/{len(recordings)} recordings, {triggers} triggers",
                  flush=True)

    print(f"\n{triggers} qualifying triggers in {SERIES}\n")
    print("Would a resting exit have filled? Break-even needs 42%.\n")
    print(f"{'hold':>6}{'rest':>7}{'exits':>8}{'optimistic':>13}{'queued':>11}"
          f"{'median fill':>13}")

    for key in sorted(tally):
        optimistic, queued, total = tally[key]

        if total < 30:
            continue

        median = st.median(times[key]) if times[key] else None
        print(f"{key[0]:>5.0f}s{key[1]:>6.0f}s{total:>8}"
              f"{optimistic / total:>12.0%}{queued / total:>11.0%}"
              f"{(f'{median:.0f}s' if median else '-'):>13}")

    print("\nREALISED expectancy per trade, each trigger taking the path it would")
    print("actually have taken. This is NOT fill_rate*touch + (1-rate)*cross: the")
    print("filled trades are not a random sample, because you fill when the market")
    print("comes to you.\n")
    print(f"{'hold':>6}{'rest':>7}{'n':>8}{'if optimistic':>15}{'if queued':>12}"
          f"{'never fills':>13}")

    for key in sorted(realised):
        rows = realised[key]

        if len(rows["never"]) < 30:
            continue

        print(f"{key[0]:>5.0f}s{key[1]:>6.0f}s{len(rows['never']):>8}"
              f"{st.mean(rows['optimistic']):>+14.3f}c"
              f"{st.mean(rows['queued']):>+11.3f}c"
              f"{st.mean(rows['never']):>+12.3f}c")

    print("\noptimistic = a counterparty reached our price (upper bound, ignores queue)")
    print("queued     = that, and the level cleared, so a back-of-queue order traded")
    print("Under 42% on the optimistic column means the cell cannot pay at all.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rec_dir", type=Path)
    parser.add_argument("--hold", type=float, nargs="+", default=[15.0, 30.0, 60.0])
    parser.add_argument("--rest", type=float, nargs="+", default=[20.0, 45.0, 90.0])
    args = parser.parse_args()
    asyncio.run(study(args.rec_dir, args.hold, args.rest))


if __name__ == "__main__":
    main()
