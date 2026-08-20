"""Would the resting exit have filled? The fill-rate question, on recorded books.

    python scripts/exit_fill_study.py ~/kalshi-audit/recs2 --hold 30 --rest 45

The candidate cell pays about +0.69c when the exit rests and fills and about
-0.51c when it has to cross. Live, that arrives a handful of exits at a time.
The recorded books already contain thousands of them.

## What a fill actually requires, and what a snapshot can see

Posting a sell at price P puts you at the back of the queue at P. You fill only
if buyers take everything ahead of you and then reach you. **Book snapshots
cannot see trades**, so any fill criterion built from them is a proxy, and the
proxy has to bracket the truth from both sides or it is just an opinion.

Three observable events, for a resting SELL at P (mirrored for a resting buy):

    traded_through   the best BID rose to >= P. The market traded up through
                     our level, so anything resting at P is gone.
    level_cleared    the best ASK moved above P, or nothing is left at P. The
                     level emptied - by trades, or by cancellations, which look
                     identical in book data.
    level_depleted   the ASK is still at P but smaller than when we posted.
                     Someone traded there. A back-of-queue order may not have
                     been reached.

    optimistic   = traded_through OR level_cleared OR level_depleted
    conservative = traded_through AND level_cleared

`optimistic` is a genuine upper bound: it fires on every way the level could
have been consumed, including cancellations that never traded. `conservative`
requires the level to have emptied AND the market to have traded up through it,
which is the strongest evidence a snapshot can offer that a back-of-queue order
was reached. The truth is between them.

The previous version of this script used `traded_through` alone and called it
the optimistic bound. It is not a bound at all, in either direction. A
marketable buy that consumes the whole ask level and leaves best_bid below P
fills a resting sell while `traded_through` stays false, so real fills were
scored as forced crosses and the realised expectancy was pushed down, toward the
conclusion the script was written to test. The README's "so the truth is worse"
had the sign of its own error backwards.

## One trigger set, one clean window

Every (hold, rest) cell now runs on the SAME triggers. A trigger qualifies only
if the recording covers `max(hold) + max(rest)` past it AND the whole window
fits before the market's close time. Two defects that fixes:

* The old censoring dropped triggers per-cell, so the grid compared 726 exits
  against 586 different ones, and the cell that looked closest to break-even was
  the most heavily censored. This is defect #2 from `taker_extract`, which
  exists to keep horizons on one trigger set, reintroduced.
* Nothing stopped a rest window from running past expiry. A trigger 30s from
  close with a 60s hold and a 90s rest was watched for two minutes after the
  market shut, against a book that cannot move, so it was scored as a never-fill
  and crossed at a post-close touch. Measured, this guard is inert on the
  current corpus: it flags 4,693 triggers on the archive and every one of them
  is already dropped for insufficient recording length, because recordings end
  near the market's close. It stays because that is a property of these
  recordings, not of the method.

The exit book is now read with the no-lookahead convention: the LAST sample at
or before `t + hold`, not the first one after it. Reading forward peeks at the
move the entry signal is trying to predict, and here it set both the price we
rest at and the P&L.

## Direction, and error bars

Triggers are reported separately for bid-heavy and ask-heavy books. The audit
measured the signal as one-sided (+0.68c versus +0.02c over a balanced book), so
`sign = +1 if obi > 0 else -1` puts roughly half the trades on the side where
there is nothing to predict. Pooling them reports the average of a bet and a
coin flip.

Every mean is clustered on the market ticker. Triggers are 5 seconds apart and
carry windows up to 150 seconds, so they overlap heavily and are nowhere near
independent draws.

## What this does not do

It does not print a break-even fill rate. The 42% figure came from blending
E[touch over ALL trades] with E[cross over ALL trades] at the fill rate, and
that blend is invalid: you fill precisely when the market comes to you, so the
filled trades are not a random sample. The realised columns below replace it.
Comparing a measured fill rate against 42% and calling it a pass would be using
the arithmetic this script exists to retire.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics as st
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from cluster_stats import clustered, fmt  # noqa: E402
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


def book_at(offsets, series, when: float):
    """Book state at `when`: the LAST sample at or before it. No lookahead.

    A book is a step function. Its state at `when` is the last update it
    received, not the next one it is about to receive.
    """

    index = bisect_right(offsets, when) - 1
    return series[index] if index >= 0 else None


def simulate(series, offsets, i, hold: float, rest: float, buying: bool,
             entry: int):
    """Post the exit at the touch after `hold`, watch `rest` seconds of book.

    Returns both fill criteria and the P&L of the path actually taken, which is
    the correction that matters. Each trigger contributes the one outcome it
    would really have had, rather than a blend across all triggers.
    """

    exit_index = bisect_right(offsets, offsets[i] + hold) - 1

    if exit_index <= i:
        return None

    # The price we would rest at: a long exits by selling at the ask, a short
    # by buying at the bid.
    _, _, _, bid_at_exit, ask_at_exit, bid_sz_at_exit, ask_sz_at_exit = series[
        exit_index]
    price = ask_at_exit if buying else bid_at_exit
    size_at_exit = ask_sz_at_exit if buying else bid_sz_at_exit
    deadline = offsets[exit_index] + rest

    traded_through = False
    level_cleared = False
    level_depleted = False
    when = None
    start = exit_index + 1

    for j in range(start, len(series)):
        offset, _mid, _obi, bid, ask, bid_sz, ask_sz = series[j]

        if offset > deadline:
            break

        if buying:
            # Resting a SELL at `price`.
            if bid >= price:
                traded_through = True
                when = when if when is not None else offset - offsets[exit_index]

            if ask > price or (ask == price and ask_sz == 0):
                level_cleared = True
            elif ask == price and ask_sz < size_at_exit:
                level_depleted = True
        else:
            if ask <= price:
                traded_through = True
                when = when if when is not None else offset - offsets[exit_index]

            if bid < price or (bid == price and bid_sz == 0):
                level_cleared = True
            elif bid == price and bid_sz < size_at_exit:
                level_depleted = True

    optimistic = traded_through or level_cleared or level_depleted
    conservative = traded_through and level_cleared

    # The book we would actually cross into when the rest window expires, read
    # with the same no-lookahead convention as the exit book.
    at_deadline = book_at(offsets, series, deadline) or series[exit_index]
    _, _, _, last_bid, last_ask, _, _ = at_deadline

    sign = 1.0 if buying else -1.0
    entry_fee = fee_cents(entry)
    filled_pnl = sign * (price - entry) / TICKS_PER_CENT - entry_fee
    crossed_at = last_bid if buying else last_ask
    crossed_pnl = (sign * (crossed_at - entry) / TICKS_PER_CENT
                   - entry_fee - fee_cents(crossed_at))
    return (optimistic, conservative, when, filled_pnl, crossed_pnl)


async def study(rec_dir: Path, holds, rests) -> None:
    recordings = sorted(p for p in rec_dir.iterdir() if (p / "manifest.json").exists())
    max_window = max(holds) + max(rests)
    # (hold, rest) -> [optimistic, conservative, total]
    tally: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
    times: dict[tuple, list] = defaultdict(list)
    realised: dict[tuple, dict] = defaultdict(
        lambda: {"optimistic": [], "conservative": [], "never": [],
                 "ticker": [], "buying": []})
    triggers = 0
    census: dict[str, int] = defaultdict(int)

    for index, rec in enumerate(recordings, 1):
        try:
            samples, _span, manifest = await walk(rec)
        except Exception:  # noqa: BLE001
            census["recordings_unreadable"] += 1
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
                census["tickers_without_close_time"] += 1
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

                # ONE trigger set for the whole grid. A trigger qualifies only
                # if the recording covers the longest window AND that window
                # ends before the market closes, so no cell is scored on a
                # different or a post-expiry sample than any other.
                # Both reasons are counted, not just the first to fire, so the
                # census says whether the close clamp actually binds on this
                # corpus or is dominated by recording length.
                too_short = offsets[-1] < offset + max_window
                past_close = to_close < max_window

                if too_short:
                    census["dropped_recording_too_short"] += 1

                if past_close:
                    census["dropped_window_runs_past_close"] += 1

                if too_short or past_close:
                    census["dropped_total"] += 1
                    continue

                last = offset
                triggers += 1

                for hold in holds:
                    for rest in rests:
                        result = simulate(series, offsets, i, hold, rest,
                                          buying, entry)

                        if result is None:
                            census["dropped_no_exit_sample"] += 1
                            continue

                        optimistic, conservative, when, filled, crossed = result
                        slot = tally[(hold, rest)]
                        slot[0] += 1 if optimistic else 0
                        slot[1] += 1 if conservative else 0
                        slot[2] += 1
                        bucket = realised[(hold, rest)]
                        bucket["optimistic"].append(filled if optimistic else crossed)
                        bucket["conservative"].append(
                            filled if conservative else crossed)
                        bucket["never"].append(crossed)
                        bucket["ticker"].append(ticker)
                        bucket["buying"].append(buying)

                        if when is not None:
                            times[(hold, rest)].append(when)

        if index % 40 == 0:
            print(f"  {index}/{len(recordings)} recordings, {triggers} triggers",
                  flush=True)

    print(f"\n{triggers} qualifying triggers in {SERIES}, one shared set "
          f"across every (hold, rest) cell\n")
    print("trigger census (every drop counted):")

    for name in sorted(census):
        print(f"  {name:<34}{census[name]:>7}")

    if not tally:
        print("\nno cells produced results")
        return

    print("\nWould a resting exit have filled?\n")
    print(f"{'hold':>6}{'rest':>7}{'exits':>8}{'optimistic':>13}"
          f"{'conservative':>14}{'median fill':>13}")

    for key in sorted(tally):
        optimistic, conservative, total = tally[key]

        if total < 30:
            continue

        median = st.median(times[key]) if times[key] else None
        print(f"{key[0]:>5.0f}s{key[1]:>6.0f}s{total:>8}"
              f"{optimistic / total:>12.0%}{conservative / total:>13.0%}"
              f"{(f'{median:.0f}s' if median else '-'):>13}")

    print("""
optimistic   = the level was consumed, cleared, or the market traded through
               it. A true upper bound: it counts cancellations too.
conservative = the level emptied AND the market traded up through our price.
The truth is between them. Neither is a break-even test on its own.""")

    print("\nREALISED expectancy per trade, each trigger taking the path it would")
    print("actually have taken, clustered on the market ticker.\n")
    print(f"{'hold':>6}{'rest':>7}{'n':>7}{'mkts':>6}{'if optimistic':>22}"
          f"{'if conservative':>22}{'never fills':>22}")

    for key in sorted(realised):
        rows = realised[key]

        if len(rows["never"]) < 30:
            continue

        cells = []
        markets = 0

        for column in ("optimistic", "conservative", "never"):
            n, markets, mean, se = clustered(rows[column], rows["ticker"])
            cells.append(fmt(mean, se))

        print(f"{key[0]:>5.0f}s{key[1]:>6.0f}s{len(rows['never']):>7}"
              f"{markets:>6}" + "".join(f"{c:>22}" for c in cells))

    # ---- the direction split ----
    print(f"\n{'=' * 100}")
    print("SAME CELLS, SPLIT BY DIRECTION")
    print("""
`sign = +1 if obi > 0 else -1` takes half the trades on the ask-heavy side,
where the audit measured the signal at +0.02c over a balanced book against
+0.68c for bid-heavy. If the pooled number is the average of an edge and a coin
flip, these two columns will not look alike.""")
    print(f"\n{'hold':>6}{'rest':>7}{'side':>12}{'n':>7}{'mkts':>6}"
          f"{'if optimistic':>22}{'never fills':>22}")

    for key in sorted(realised):
        rows = realised[key]

        for label, want in (("bid-heavy", True), ("ask-heavy", False)):
            picked = [i for i, b in enumerate(rows["buying"]) if b is want]

            if len(picked) < 30:
                continue

            tickers = [rows["ticker"][i] for i in picked]
            n, groups, opt_mean, opt_se = clustered(
                [rows["optimistic"][i] for i in picked], tickers)
            _, _, never_mean, never_se = clustered(
                [rows["never"][i] for i in picked], tickers)
            print(f"{key[0]:>5.0f}s{key[1]:>6.0f}s{label:>12}{n:>7}{groups:>6}"
                  f"{fmt(opt_mean, opt_se):>22}{fmt(never_mean, never_se):>22}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rec_dir", type=Path)
    parser.add_argument("--hold", type=float, nargs="+", default=[15.0, 30.0, 60.0])
    parser.add_argument("--rest", type=float, nargs="+", default=[20.0, 45.0, 90.0])
    args = parser.parse_args()
    asyncio.run(study(args.rec_dir, args.hold, args.rest))


if __name__ == "__main__":
    main()
