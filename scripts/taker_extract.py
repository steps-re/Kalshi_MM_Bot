"""One corrected pass over the recordings -> a compact trigger cache.

    python scripts/taker_extract.py ~/kalshi-audit/recs --out ~/kalshi-audit/triggers.jsonl

Replaces the book-walking half of the original `taker_expectancy.py`. That scan
folded extraction and analysis into a single pass, which meant every question
cost a full re-parse of 10GB and no question could be asked twice. It also
carried six measurement defects that this file fixes:

1. **Lookahead.** The original took the mid at the FIRST update at or AFTER
   `t + horizon`. A book is a step function: its state at `t+h` is the LAST
   update at or before `t+h`. Reading the next update instead peeks at the very
   move the signal is trying to predict, and it peeks furthest on the quietest
   books. Both conventions are recorded here (`at` and `after`) so the bias can
   be measured rather than argued about.
2. **Censoring.** An entry whose horizon ran past the end of the recording was
   silently dropped, and dropped for the 30s horizon while surviving for 5s, so
   the three horizons were computed on different trigger sets. Here an entry is
   eligible only if the recording covers `t + max(HORIZONS)`, all horizons share
   one trigger set, and the censored count is reported.
3. **Cooldown across bands.** One `last_trigger` per ticker was shared by three
   disjoint OBI bands, so a 0.5-0.7 reading blocked a >0.9 reading three seconds
   later. OBI ramps through mild before it gets extreme, so the strongest band
   was systematically stripped of its ramping episodes. Cooldown is now per
   (ticker, band).
4. **Exit price.** The original marked the exit at the mid while claiming a
   resting maker exit. A resting exit fills at the touch. All three exit
   conventions are recorded (touch / mid / forced cross) so the assumption is a
   reported bracket, not a hidden choice worth half a spread.
5. **Depth.** OBI was computed from top-of-book sizes with no floor, so a 1-lot
   versus 19-lot touch scored 0.9 mechanically. Touch sizes are recorded so a
   floor can be applied at analysis time, and so capacity is finally measurable.
6. **Window phase.** The project's strongest measured conditioner (markout decays
   monotonically from +0.41c to -0.07c across a 15-minute window) was absent from
   the scan entirely. `close_times_utc` is in every manifest; seconds-to-close is
   now on every row, as is the wall-clock UTC hour.

Output is one JSON object per trigger. Everything downstream reads this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.api.feed_controller import (  # noqa: E402
    FeedController,
    ORDERBOOK_CHANNEL,
)
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402
from kalshi_mm_bot.recording import (  # noqa: E402
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingSessionReader,
)

TICKS_PER_CENT = ONE_DOLLAR // 100
HORIZONS = (5.0, 15.0, 30.0)
MAX_HORIZON = max(HORIZONS)
OBI_BANDS = (
    # CONTROL: books with no meaningful imbalance. These carry no signal by
    # construction, so whatever they earn in a given price band and market phase
    # is what the STRUCTURE pays - decay, drift, spread dynamics - and not what
    # the imbalance predicts. Without this band a positive slice cannot be told
    # apart from a market that simply drifts one way near expiry.
    ("obi<.2 CTRL", 0.0, 0.2),
    ("obi.2-.5", 0.2, 0.5),
    ("obi.5-.7", 0.5, 0.7),
    ("obi.7-.9", 0.7, 0.9),
    ("obi>.9", 0.9, 1.01),
)
MAX_SPREAD_TICKS = 200
MIN_GAP = 5.0


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def band_of(bands, value: float) -> str | None:
    for label, lo, hi in bands:
        if lo <= value < hi:
            return label

    return None


def book_at(offsets: list[float], series: list, when: float):
    """Book state at `when`: the LAST sample at or before it. No lookahead.

    A book only changes when an update arrives, so between updates its state is
    the last one seen. This is the honest reading and it is also the only one a
    live system could have acted on.
    """

    j = bisect_right(offsets, when) - 1
    return series[j] if j >= 0 else None


def book_after(offsets: list[float], series: list, when: float, start: int):
    """Book state at the FIRST sample at or after `when` - the original, biased
    convention, kept only so the bias can be quantified."""

    j = bisect_left(offsets, when, start)
    return series[j] if j < len(series) else None


async def walk(recording: Path):
    """Per-ticker [(offset, mid, obi, bid, ask, bid_sz, ask_sz)] + covered span."""

    reader = RecordingSessionReader.open(recording)

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        return {}, 0.0, reader.manifest

    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=0.0)
    controller = FeedController(rest=RecordedRestClient(reader.manifest), ws=ws)
    samples: dict[str, list[tuple]] = defaultdict(list)
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
                bid_sz,
                ask_sz,
            )
        )

    return samples, last_offset, reader.manifest


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def coverage_seconds(intervals: list[tuple[float, float]]) -> float:
    """Union of intervals, not their sum.

    The original summed each ticker's own span into its venue's hour count. That
    is elapsed time only when a venue has exactly one live ticker; the KXBTCD
    ladder puts nine strikes in one recording, so its hours came out ~9x real and
    its triggers-per-hour ~9x too low. Frequency is a rejection criterion in this
    scan, so an inflated denominator manufactures rejections.
    """

    if not intervals:
        return 0.0

    ordered = sorted(intervals)
    merged_start, merged_end = ordered[0]
    total = 0.0

    for start, end in ordered[1:]:
        if start > merged_end:
            total += merged_end - merged_start
            merged_start, merged_end = start, end
        else:
            merged_end = max(merged_end, end)

    return total + merged_end - merged_start


async def extract(rec_dir: Path, out_path: Path) -> None:
    recordings = sorted(p for p in rec_dir.iterdir() if (p / "manifest.json").exists())
    # Interval union per venue, in absolute UTC epoch seconds, so concurrent
    # tickers and concurrent recordings both collapse to real elapsed time.
    venue_intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    all_intervals: list[tuple[float, float]] = []
    skipped: list[str] = []
    rows = 0
    censored = 0
    handle = out_path.open("w", encoding="utf-8")

    for index, rec in enumerate(recordings, 1):
        try:
            samples, _span, manifest = await walk(rec)
        except Exception as error:  # noqa: BLE001
            skipped.append(f"{rec.name}: {type(error).__name__} {error}")
            print(f"  SKIPPED {rec.name}: {type(error).__name__} {error}", flush=True)
            continue

        started = parse_utc(manifest.started_at_utc).timestamp()
        closes = {
            ticker: parse_utc(value).timestamp()
            for ticker, value in (manifest.metadata or {}).get("close_times_utc", {}).items()
        }

        for ticker, series in samples.items():
            series.sort(key=lambda row: row[0])
            offsets = [s[0] for s in series]
            venue = series_of(ticker)

            if len(offsets) > 1:
                interval = (started + offsets[0], started + offsets[-1])
                venue_intervals[venue].append(interval)
                all_intervals.append(interval)

            series_end = offsets[-1] if offsets else 0.0
            close_at = closes.get(ticker)
            # One cooldown per (ticker, band): a mild reading must no longer
            # censor the extreme reading that follows it three seconds later.
            last_trigger: dict[str, float] = defaultdict(lambda: -1e9)

            for i, row in enumerate(series):
                offset, _mid, obi, best_bid, best_ask, bid_sz, ask_sz = row

                if best_ask - best_bid > MAX_SPREAD_TICKS:
                    continue

                obi_band = band_of(OBI_BANDS, abs(obi))

                if obi_band is None or offset - last_trigger[obi_band] < MIN_GAP:
                    continue

                last_trigger[obi_band] = offset

                # Every horizon must be observable, or the horizons are computed
                # on different trigger sets and are not comparable to each other.
                if offset + MAX_HORIZON > series_end:
                    censored += 1
                    continue

                sign = 1 if obi > 0 else -1
                entry = best_ask if sign > 0 else best_bid
                # Size available on the side we must cross. At extreme OBI this
                # is by construction the thin side, which is why capacity and
                # signal strength are structurally anti-correlated.
                crossable = (ask_sz if sign > 0 else bid_sz) / COUNT_SCALE
                wall = started + offset
                record = {
                    "venue": venue,
                    "ticker": ticker,
                    "rec": rec.name,
                    "utc": wall,
                    "hour": datetime.fromtimestamp(wall, timezone.utc).hour,
                    "obi_band": obi_band,
                    "obi": round(obi, 4),
                    "side": "buy" if sign > 0 else "sell",
                    "entry": entry,
                    "bid": best_bid,
                    "ask": best_ask,
                    "spread": best_ask - best_bid,
                    "bid_sz": bid_sz / COUNT_SCALE,
                    "ask_sz": ask_sz / COUNT_SCALE,
                    "crossable": crossable,
                    "to_close": round(close_at - wall, 1) if close_at else None,
                    "h": {},
                }

                for horizon in HORIZONS:
                    target = offset + horizon
                    at = book_at(offsets, series, target)
                    after = book_after(offsets, series, target, i + 1)

                    if at is None:
                        continue

                    record["h"][f"{horizon:.0f}"] = {
                        # Correct: state of the book at the horizon.
                        "mid": at[1],
                        "bid": at[3],
                        "ask": at[4],
                        "lag": round(target - at[0], 2),
                        # Original convention, for the lookahead A/B only.
                        "mid_after": after[1] if after else None,
                        "lead": round(after[0] - target, 2) if after else None,
                    }

                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                rows += 1

        if index % 10 == 0:
            print(f"  {index}/{len(recordings)} recordings, {rows} triggers", flush=True)

    handle.close()
    meta = {
        "recordings": len(recordings),
        "skipped": skipped,
        "triggers": rows,
        "censored_by_horizon": censored,
        "elapsed_hours": coverage_seconds(all_intervals) / 3600.0,
        "venue_hours": {
            venue: coverage_seconds(intervals) / 3600.0
            for venue, intervals in venue_intervals.items()
        },
        # Raw absolute-time intervals so any sub-period's coverage can be
        # recomputed as a union. A per-period trigger rate divided by whole-corpus
        # hours is not a rate at all, and frequency is a rejection criterion here.
        "intervals": [
            [venue, round(start, 1), round(end, 1)]
            for venue, spans in venue_intervals.items()
            for start, end in spans
        ],
        "config": {
            "horizons": list(HORIZONS),
            "min_gap": MIN_GAP,
            "max_spread_ticks": MAX_SPREAD_TICKS,
            "obi_bands": [list(b) for b in OBI_BANDS],
        },
    }
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"\n{rows} triggers -> {out_path}")
    print(f"{censored} entries dropped for running past the end of their recording")
    print(f"real elapsed coverage: {meta['elapsed_hours']:.1f}h (interval union)")

    if skipped:
        # Loud on purpose. A recording that fails to parse leaves both the
        # numerator and the denominator, and a differing failure rate between
        # two periods silently changes what is being compared.
        print(f"\n!! {len(skipped)} recordings SKIPPED - coverage is not what it looks like:")

        for line in skipped:
            print(f"   {line}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rec_dir", type=Path)
    parser.add_argument("--out", type=Path, default=Path("triggers.jsonl"))
    args = parser.parse_args()
    asyncio.run(extract(args.rec_dir, args.out))


if __name__ == "__main__":
    main()
