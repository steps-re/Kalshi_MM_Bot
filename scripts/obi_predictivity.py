"""Does order-book imbalance predict the next mid move? (Is our mid biased?)

    python scripts/obi_predictivity.py /var/tmp/kalshi-recordings

The whole adverse-selection story rests on one testable claim: our quote center
(the mid) is a biased fair value, so when the book is imbalanced our resting
quote on the heavy side sits below true value and gets picked off. If that's
true, top-of-book imbalance

    OBI = (size_bid - size_ask) / (size_bid + size_ask)

must PREDICT the next mid move. This walks the recorded books (no trading, no
real money), computes OBI at each update and the realised mid change a few
seconds later, and reports the relationship - binned, and as a slope in cents of
forward move per unit of OBI, per venue.

If the slope is materially positive, the mid is biased by exactly that much and
the microprice / OBI-skew fixes are justified with a calibrated size. If it's
flat, quoting around the mid is fine and the leak is elsewhere - a real negative
result, which is worth as much as a positive one. Memory says spot does not lead
the book here; this is a different, shorter-horizon question about the book's own
microstructure, and it has to be asked separately.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics as st
import sys
from bisect import bisect_left, bisect_right
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
HORIZONS = (2.0, 5.0, 10.0)
OBI_BUCKETS = (
    ("<-0.5 (ask-heavy)", -1.01, -0.5),
    ("-0.5..-0.2", -0.5, -0.2),
    ("-0.2..0.2 (balanced)", -0.2, 0.2),
    ("0.2..0.5", 0.2, 0.5),
    (">0.5 (bid-heavy)", 0.5, 1.01),
)


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


async def walk(recording: Path) -> dict[str, list[tuple[float, float, float]]]:
    """Return per-ticker [(offset, mid_ticks, obi)] from one recording."""

    reader = RecordingSessionReader.open(recording)

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        return {}

    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=0.0)
    controller = FeedController(rest=RecordedRestClient(reader.manifest), ws=ws)
    samples: dict[str, list[tuple[float, float, float]]] = defaultdict(list)

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

        book = controller.orderbooks.get(ticker)

        if book is None or book.best_bid is None or book.best_ask is None:
            continue

        # bids/asks are lists indexed by price tick, not dicts; best_bid/ask are
        # valid indices by construction.
        bid_sz = book.bids[book.best_bid]
        ask_sz = book.asks[book.best_ask]
        total = bid_sz + ask_sz

        if total <= 0:
            continue

        obi = (bid_sz - ask_sz) / total
        mid = (book.best_bid + book.best_ask) / 2
        samples[ticker].append((event.offset_seconds, mid, obi))

    return samples


def forward_returns(
    samples: list[tuple[float, float, float]], horizon: float
) -> list[tuple[float, float, float]]:
    """[(obi, forward_move_cents, forward_move_biased)] per sample.

    The book is a step function: its state at `offset + horizon` is the LAST
    update at or before that instant. The original version of this function took
    the FIRST update at or AFTER it, which on a quiet book skips forward to
    whenever the next update lands - and that next update tends to BE the move
    the signal is supposed to predict. Both are returned so the difference is
    reported rather than assumed away.
    """

    offsets = [s[0] for s in samples]
    end = offsets[-1] if offsets else 0.0
    out = []

    for i, (offset, mid, obi) in enumerate(samples):
        target = offset + horizon

        # Censor consistently: if the recording does not cover the horizon, the
        # sample is unusable, and dropping it later for one horizon but not
        # another would compare different sample sets.
        if target > end:
            break

        honest = samples[bisect_right(offsets, target) - 1][1]
        j = bisect_left(offsets, target, i + 1)
        biased = samples[j][1] if j < len(samples) else honest
        out.append((
            obi,
            (honest - mid) / TICKS_PER_CENT,
            (biased - mid) / TICKS_PER_CENT,
        ))

    return out


def slope(pairs, column: int = 1) -> float:
    """OLS slope of forward-return on OBI: cents of move per unit OBI."""

    if len(pairs) < 30:
        return 0.0

    xs = [p[0] for p in pairs]
    ys = [p[column] for p in pairs]
    mx, my = st.mean(xs), st.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)

    if denom == 0:
        return 0.0

    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def clustered_slope(by_ticker: dict, column: int = 1) -> tuple[float, float, int]:
    """Slope estimated per market, then averaged: (mean, SE, markets used).

    The headline "n=1.36M updates" counts book updates, not independent
    observations. Consecutive samples overlap at every horizon and every sample
    inside one market rides one price path, so 1.36M is a measure of how densely
    the book was sampled, not of how much was learned. Estimating the slope
    separately per market and taking the spread across markets gives a standard
    error whose independent unit is a market, which is the honest one.
    """

    per_market = [
        slope(pairs, column) for pairs in by_ticker.values() if len(pairs) >= 200
    ]

    if len(per_market) < 3:
        return (0.0, float("inf"), len(per_market))

    mean = st.mean(per_market)
    se = st.stdev(per_market) / math.sqrt(len(per_market))
    return (mean, se, len(per_market))


def report(pairs_by_venue: dict, by_ticker: dict, horizon: float) -> dict:
    print(f"\n=== horizon {horizon:.0f}s ===")
    allpairs = [p for v in pairs_by_venue.values() for p in v]

    if not allpairs:
        print("  no pairs")
        return {}

    print("  forward mid move (cents) by OBI bucket, pooled:")
    buckets = []

    for label, lo, hi in OBI_BUCKETS:
        vals = [row[1] for row in allpairs if lo <= row[0] < hi]

        if vals:
            print(f"    {label:<24}n={len(vals):>7}  mean {st.mean(vals):+.3f}c")
            buckets.append({"label": label, "n": len(vals), "mean": st.mean(vals)})

    mean, se, markets = clustered_slope(by_ticker)
    biased_mean, _, _ = clustered_slope(by_ticker, column=2)
    pooled = slope(allpairs)
    print(f"\n  pooled slope   : {pooled:+.3f}c per unit OBI  "
          f"(n={len(allpairs)} book updates)")
    print(f"  per-market     : {mean:+.3f}c +/- {se:.3f}  "
          f"(t={mean / se if se else 0:+.1f}, {markets} markets)")
    print(f"  same, with the old lookahead: {biased_mean:+.3f}c  "
          f"(difference {biased_mean - mean:+.3f}c)")
    print("  The pooled n counts book updates. Overlapping samples inside one market")
    print("  are one price path, so the market count is the real sample size.")
    print("  per-venue slope (per-market mean +/- SE):")
    venues = []

    for venue in sorted(pairs_by_venue):
        tickers = {t: p for t, p in by_ticker.items() if series_of(t) == venue}
        v_mean, v_se, v_markets = clustered_slope(tickers)
        real = bool(v_se) and abs(v_mean) > 1.96 * v_se
        flag = "" if real else "   (not distinguishable from 0)"
        print(f"    {venue:<14}{v_mean:+.3f}c +/- {v_se:.3f}  "
              f"({v_markets} markets){flag}")
        venues.append({
            "venue": venue, "slope": v_mean,
            "se": v_se if math.isfinite(v_se) else None,
            "markets": v_markets, "significant": real,
        })

    return {
        "horizon": horizon,
        "pooled_slope": pooled,
        "book_updates": len(allpairs),
        "per_market": mean,
        "se": se,
        "t": mean / se if se else 0.0,
        "markets": markets,
        "with_old_lookahead": biased_mean,
        "lookahead_delta": biased_mean - mean,
        "buckets": buckets,
        "venues": venues,
    }


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    json_out = None

    for i, a in enumerate(sys.argv):
        if a == "--json" and i + 1 < len(sys.argv):
            json_out = Path(sys.argv[i + 1])

    root = Path(args[0] if args else "/var/tmp/kalshi-recordings")
    recordings = sorted(p for p in root.iterdir() if (p / "manifest.json").exists())

    if not recordings:
        print(f"no recordings under {root}")
        return

    per_ticker: dict[str, list[tuple[float, float, float]]] = defaultdict(list)

    for rec in recordings:
        try:
            for ticker, samples in (await walk(rec)).items():
                per_ticker[ticker].extend(samples)
        except Exception as error:  # noqa: BLE001
            print(f"  skipped {rec.name}: {type(error).__name__} {error}")

    print(f"{len(recordings)} recording(s), {len(per_ticker)} ticker(s), "
          f"{sum(len(v) for v in per_ticker.values())} book updates")

    payload = {
        "recordings": len(recordings),
        "tickers": len(per_ticker),
        "book_updates": sum(len(v) for v in per_ticker.values()),
        "horizons": [],
    }

    for horizon in HORIZONS:
        by_venue: dict[str, list] = defaultdict(list)
        by_ticker: dict[str, list] = {}

        for ticker, samples in per_ticker.items():
            samples.sort()
            pairs = forward_returns(samples, horizon)
            by_ticker[ticker] = pairs
            by_venue[series_of(ticker)].extend(pairs)

        result = report(by_venue, by_ticker, horizon)

        if result:
            payload["horizons"].append(result)

    if json_out:
        json_out.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {json_out}")

    print("\nA materially positive slope => the mid is biased and OBI is the skew "
          "signal (center on microprice). Flat => the leak is not a biased center.")


if __name__ == "__main__":
    asyncio.run(main())
