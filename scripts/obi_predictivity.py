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

        bid_sz = book.bids.get(book.best_bid, 0)
        ask_sz = book.asks.get(book.best_ask, 0)
        total = bid_sz + ask_sz

        if total <= 0:
            continue

        obi = (bid_sz - ask_sz) / total
        mid = (book.best_bid + book.best_ask) / 2
        samples[ticker].append((event.offset_seconds, mid, obi))

    return samples


def forward_returns(
    samples: list[tuple[float, float, float]], horizon: float
) -> list[tuple[float, float]]:
    """[(obi, forward_mid_move_cents)] pairing each sample with mid ~horizon later."""

    offsets = [s[0] for s in samples]
    out = []

    for i, (offset, mid, obi) in enumerate(samples):
        j = bisect_left(offsets, offset + horizon, i + 1)

        if j >= len(samples):
            break

        future_mid = samples[j][1]
        out.append((obi, (future_mid - mid) / TICKS_PER_CENT))

    return out


def slope(pairs: list[tuple[float, float]]) -> float:
    """OLS slope of forward-return on OBI: cents of move per unit OBI."""

    if len(pairs) < 30:
        return 0.0

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = st.mean(xs), st.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)

    if denom == 0:
        return 0.0

    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def report(pairs_by_venue: dict[str, list[tuple[float, float]]], horizon: float) -> None:
    print(f"\n=== horizon {horizon:.0f}s ===")
    allpairs = [p for v in pairs_by_venue.values() for p in v]

    if not allpairs:
        print("  no pairs")
        return

    print("  forward mid move (cents) by OBI bucket, pooled:")

    for label, lo, hi in OBI_BUCKETS:
        vals = [ret for obi, ret in allpairs if lo <= obi < hi]

        if vals:
            print(f"    {label:<24}n={len(vals):>6}  mean {st.mean(vals):+.3f}c")

    print(f"  pooled slope: {slope(allpairs):+.3f}c per unit OBI  (n={len(allpairs)})")
    print("  per-venue slope:")

    for venue in sorted(pairs_by_venue):
        pairs = pairs_by_venue[venue]
        print(f"    {venue:<14}{slope(pairs):+.3f}c  (n={len(pairs)})")


async def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/tmp/kalshi-recordings")
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

    for horizon in HORIZONS:
        by_venue: dict[str, list[tuple[float, float]]] = defaultdict(list)

        for ticker, samples in per_ticker.items():
            samples.sort()
            by_venue[series_of(ticker)].extend(forward_returns(samples, horizon))

        report(by_venue, horizon)

    print("\nA materially positive slope => the mid is biased and OBI is the skew "
          "signal (center on microprice). Flat => the leak is not a biased center.")


if __name__ == "__main__":
    asyncio.run(main())
