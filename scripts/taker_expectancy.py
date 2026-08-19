"""Does any slice of the OBI signal clear TAKER costs? The go/no-go scan.

    python scripts/taker_expectancy.py /tmp/recs_pm

obi_predictivity.py proved the book's imbalance predicts the next mid move
(~0.85c per unit OBI at 5s, n=1.36M). A resting maker is on the wrong side of
that signal - it is the flow that picks the maker off. The only way to be on the
right side is to TAKE: cross the spread when OBI is extreme, exit passively later.

A taker entry is deterministic on recorded data (you pay the displayed touch), so
unlike every maker backtest this scan is trustworthy. The question is purely
whether the conditional move beats the known, exact costs:

    net(cents) = signed mid move at horizon
               - half-spread paid at entry
               - 7 * P * (1-P) taker fee (P = entry price)

assuming the exit rests as a maker (measured free). The scan reports mean net by
OBI extremity x spread x price band, per venue, with trigger counts per hour -
because a slice that fires twice a day cannot pay for anything. Slices are
disjoint on OBI (0.5-0.7, 0.7-0.9, >0.9), so a stronger band's edge is not
diluted into a weaker one's.

No positive slice with real frequency => no deployable taker program on this fee
schedule, and that negative is final for this venue class.
"""

from __future__ import annotations

import asyncio
import statistics as st
import sys
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.api.feed_controller import (  # noqa: E402
    FeedController,
    ORDERBOOK_CHANNEL,
)
from kalshi_mm_bot.market.price import ONE_DOLLAR  # noqa: E402
from kalshi_mm_bot.recording import (  # noqa: E402
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingSessionReader,
)

TICKS_PER_CENT = ONE_DOLLAR // 100
HORIZONS = (5.0, 15.0, 30.0)
OBI_BANDS = (("obi.5-.7", 0.5, 0.7), ("obi.7-.9", 0.7, 0.9), ("obi>.9", 0.9, 1.01))
# Entry price bands: the fee is 7*P*(1-P) cents, so the tails are where taking
# is cheap. Bands are on the ENTRY price (the touch we cross).
PRICE_BANDS = (("tail<.15", 0.0, 0.15), (".15-.35", 0.15, 0.35),
               ("mid.35-.65", 0.35, 0.65), (".65-.85", 0.65, 0.85),
               ("tail>.85", 0.85, 1.01))
MAX_SPREAD_TICKS = 200  # only consider crossing books <= 2c wide


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


async def walk(recording: Path):
    """Per-ticker [(offset, mid, obi, best_bid, best_ask)] plus covered seconds."""

    reader = RecordingSessionReader.open(recording)

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        return {}, 0.0

    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=0.0)
    controller = FeedController(rest=RecordedRestClient(reader.manifest), ws=ws)
    samples: dict[str, list[tuple[float, float, float, int, int]]] = defaultdict(list)
    last_offset = 0.0

    await controller.connect()
    await controller.subscribe(reader.manifest.tickers, channels=(ORDERBOOK_CHANNEL,))

    while True:
        try:
            ticker = await controller.recv()
        except EOFError:
            break

        event = ws.last_event

        if event is None or ticker is None:
            continue

        last_offset = max(last_offset, event.offset_seconds)
        book = controller.orderbooks.get(ticker)

        if book is None or book.best_bid is None or book.best_ask is None:
            continue

        bid_sz = book.bids[book.best_bid]
        ask_sz = book.asks[book.best_ask]
        total = bid_sz + ask_sz

        if total <= 0:
            continue

        samples[ticker].append(
            (
                event.offset_seconds,
                (book.best_bid + book.best_ask) / 2,
                (bid_sz - ask_sz) / total,
                book.best_bid,
                book.best_ask,
            )
        )

    return samples, last_offset


def fee_cents(price_ticks: int) -> float:
    p = price_ticks / ONE_DOLLAR
    return 7.0 * p * (1.0 - p)


def mid_after(series, offsets, i: int, horizon: float) -> float | None:
    j = bisect_left(offsets, series[i][0] + horizon, i + 1)
    return series[j][1] if j < len(series) else None


def band_of(bands, value: float) -> str | None:
    for label, lo, hi in bands:
        if lo <= value < hi:
            return label

    return None


async def main() -> None:
    rec_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/recs_pm")
    recordings = sorted(p for p in rec_dir.iterdir() if (p / "manifest.json").exists())
    # nets[(venue, obi_band, price_band, horizon)] = list of net cents
    nets: dict[tuple, list[float]] = defaultdict(list)
    hours = 0.0
    venue_hours: dict[str, float] = defaultdict(float)
    MIN_GAP = 5.0  # per-ticker cooldown so one episode isn't counted 50 times

    for rec in recordings:
        try:
            samples, span = await walk(rec)
        except Exception as error:  # noqa: BLE001
            print(f"  skipped {rec.name}: {type(error).__name__} {error}")
            continue

        hours += span / 3600.0

        for ticker, series in samples.items():
            series.sort()
            offsets = [s[0] for s in series]
            venue = series_of(ticker)

            if len(offsets) > 1:
                venue_hours[venue] += (offsets[-1] - offsets[0]) / 3600.0

            last_trigger = -1e9

            for i, (offset, mid, obi, best_bid, best_ask) in enumerate(series):
                if best_ask - best_bid > MAX_SPREAD_TICKS:
                    continue

                obi_band = band_of(OBI_BANDS, abs(obi))

                if obi_band is None or offset - last_trigger < MIN_GAP:
                    continue

                last_trigger = offset
                # Positive OBI: mid about to rise -> BUY at the ask.
                # Negative OBI: SELL at the bid. Entry price = the touch we cross.
                entry = best_ask if obi > 0 else best_bid
                sign = 1.0 if obi > 0 else -1.0
                price_band = band_of(PRICE_BANDS, entry / ONE_DOLLAR)

                if price_band is None:
                    continue

                cost = fee_cents(entry)

                for horizon in HORIZONS:
                    future = mid_after(series, offsets, i, horizon)

                    if future is None:
                        continue

                    gross = sign * (future - entry) / TICKS_PER_CENT
                    nets[(venue, obi_band, price_band, horizon)].append(gross - cost)

    print(f"{len(recordings)} recordings, ~{hours:.1f} recorded book-hours\n")
    print("NET expectancy per taker entry (after half-spread-implicit entry at the "
          "touch AND exact taker fee), assuming a free passive exit at future mid:\n")
    rows = []

    for (venue, ob, pb, hz), vals in nets.items():
        if len(vals) < 30:
            continue

        se = st.stdev(vals) / len(vals) ** 0.5
        # t is OPTIMISTIC: overlapping horizons within an episode are correlated,
        # so effective n is smaller than n. Treat t < ~3.5 as noise given the
        # 482-slice search, and validate out-of-sample regardless.
        rows.append((st.mean(vals), venue, ob, pb, hz, len(vals), se))

    rows.sort(reverse=True)
    print(f"{'net/trade':>10}{'SE':>7}{'t':>6}{'n':>7}{'per-hr':>8}  venue / obi / price / horizon")

    for mean, venue, ob, pb, hz, n, se in rows[:25]:
        t = mean / se if se else 0.0
        print(f"{mean:>+9.3f}c{se:>6.2f}c{t:>+6.1f}{n:>7}{n / max(hours, 0.1):>8.1f}  "
              f"{venue} / {ob} / {pb} / {hz:.0f}s")

    positive = [r for r in rows if r[0] > 0]
    print(f"\n{len(positive)} of {len(rows)} slices positive net of costs.")

    if positive:
        best = positive[0]
        print(f"Best: {best[1]} {best[2]} {best[3]} @{best[4]:.0f}s -> "
              f"{best[0]:+.3f}c/trade, {best[5]} triggers (~{best[5]/max(hours,0.1):.1f}/hr).")
        print("A candidate exists - validate per-day (not pooled) before believing it.")
    else:
        print("No slice clears taker costs: the fee schedule eats the signal. "
              "That is a final negative for taker strategies on these books.")

    # ---- per-venue sniper map: can a sniper live on each venue, and how well ----
    print("\nSNIPER MAP (per venue: its own recorded hours, its positive slices, its best):")
    print(f"{'venue':<14}{'bk-hrs':>7}{'+slices':>8}{'best net':>10}{'t':>6}{'trig/hr':>9}  best slice")
    venues = sorted(venue_hours, key=lambda v: -venue_hours[v])

    for venue in venues:
        vh = venue_hours[venue]
        mine = [r for r in rows if r[1] == venue]
        pos = [r for r in mine if r[0] > 0]

        if not mine:
            print(f"{venue:<14}{vh:>7.1f}{'0':>8}{'-':>10}{'-':>6}{'-':>9}  (no slice reached n>=30)")
            continue

        best = max(mine, key=lambda r: r[0])
        mean, _, ob, pb, hz, n, se = best
        t = mean / se if se else 0.0
        rate = n / vh if vh else 0.0
        print(f"{venue:<14}{vh:>7.1f}{len(pos):>8}{mean:>+9.3f}c{t:>+6.1f}{rate:>9.1f}  "
              f"{ob} / {pb} / {hz:.0f}s")

    print("\nA venue with 0 positive slices offers a sniper nothing at these triggers; "
          "a venue whose best slice is positive but t<2 is a candidate only until "
          "out-of-sample data votes. trig/hr uses the venue's own recorded hours.")


if __name__ == "__main__":
    asyncio.run(main())
