"""Record real orderbooks by polling the public REST endpoint. No API key needed.

    python scripts/poll_record.py --auto 8 --duration-sec 1800

Kalshi's websocket feed needs credentials, which is why nothing has ever been
recorded here. The REST orderbook endpoint does not, so this polls it and
synthesises the same `orderbook_snapshot` / `orderbook_delta` messages the
websocket would have produced. The output drops straight into the existing
replay, backtest, markout and walk-forward machinery.

What polling costs you, stated plainly:

* **Resolution.** At a 1-2s interval you see net change, not every event. Two
  trades that cancel out inside one interval look like nothing happened, so
  measured volatility is understated and fills that would have happened between
  polls are invisible.
* **Queue position.** The queue-aware fill model assumes it knows how much size
  sat ahead of us. Polled deltas cannot distinguish a cancel from a fill, so
  queue estimates are rougher than from the real feed.

That makes this good enough for markout, spread capture, and fee calibration -
the measurements that are currently blocking - and not a substitute for the
websocket feed when tuning execution. Recordings are marked `source: rest_poll`
in the manifest so nothing downstream can mistake one for the other.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.market.types import PriceRange  # noqa: E402
from kalshi_mm_bot.recording import RecordingManifest, RecordingSessionWriter  # noqa: E402
from kalshi_mm_bot.recording.paths import default_recording_dir  # noqa: E402

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ORDERBOOK_CHANNEL = "orderbook_delta"


def get(path: str, params: dict | None = None, *, timeout: float = 30.0) -> dict:
    url = f"{BASE}{path}"

    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(url, headers={"User-Agent": "kalshi-mm-poll/1.0"})

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pick_liquid_tickers(count: int, *, min_volume: float) -> list[str]:
    """The most active two-sided markets right now, via /events."""

    candidates: list[tuple[float, str]] = []
    cursor = None

    for _ in range(6):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}

        if cursor:
            params["cursor"] = cursor

        data = get("/events", params)
        cursor = data.get("cursor")

        for event in data.get("events", []):
            for market in event.get("markets", []) or []:
                if market.get("mve_collection_ticker"):
                    continue

                bid = _num(market.get("yes_bid_dollars"))
                ask = _num(market.get("yes_ask_dollars"))
                volume = _num(market.get("volume_24h_fp"))

                if 0 < bid < ask < 1 and volume >= min_volume:
                    candidates.append((volume, market["ticker"]))

        if not cursor:
            break

    candidates.sort(reverse=True)
    return [ticker for _, ticker in candidates[:count]]


def fetch_book(ticker: str) -> tuple[dict[str, float], dict[str, float]]:
    """Yes-side levels as {price_string: size}, both sides in YES prices.

    Kalshi returns the ask side in NO prices; a NO bid at 0.73 is a YES offer at
    0.27. The replayer expects everything in YES terms because the live feed
    subscribes with use_yes_price, so convert here rather than teaching the
    replayer about a second convention.
    """

    data = get(f"/markets/{ticker}/orderbook", {"depth": 100})
    book = data.get("orderbook_fp") or {}

    bids = {f"{_num(p):.4f}": _num(s) for p, s in (book.get("yes_dollars") or [])}
    asks = {
        f"{1.0 - _num(p):.4f}": _num(s) for p, s in (book.get("no_dollars") or [])
    }

    return bids, asks


def snapshot_message(ticker: str, seq: int, bids: dict, asks: dict) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[p, f"{s:.2f}"] for p, s in sorted(bids.items())],
            "no_dollars_fp": [[p, f"{s:.2f}"] for p, s in sorted(asks.items())],
        },
    }


def delta_messages(ticker: str, seq: int, side: str, before: dict, after: dict) -> list[dict]:
    """One delta per changed price level, in the websocket's shape."""

    messages = []

    for price in sorted(set(before) | set(after)):
        change = after.get(price, 0.0) - before.get(price, 0.0)

        if abs(change) < 0.005:
            continue

        seq += 1
        messages.append(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": seq,
                "msg": {
                    "market_ticker": ticker,
                    "side": side,
                    "price_dollars": price,
                    "delta_fp": f"{change:.2f}",
                },
            }
        )

    return messages


async def _run(args: argparse.Namespace) -> None:
    tickers = list(args.tickers)

    if args.auto:
        print(f"Selecting the {args.auto} most active two-sided markets...")
        tickers = pick_liquid_tickers(args.auto, min_volume=args.min_volume)

    if not tickers:
        raise SystemExit("no tickers to record; pass some or raise --auto / lower --min-volume")

    print(f"Recording {len(tickers)}: {', '.join(tickers)}")

    writer = RecordingSessionWriter.create(
        args.output or default_recording_dir(ROOT),
        flush_every=args.flush_every,
    )
    # Whole-cent grid; the replayer needs the level set to index the book.
    price_ranges = {t: (PriceRange(start=0, end=10_000, step=100),) for t in tickers}
    close_times: dict[str, str] = {}

    try:
        for ticker in tickers:
            try:
                market = get("/markets", {"tickers": ticker})["markets"][0]
                close = market.get("close_time")

                if close:
                    close_times[ticker] = str(close)
            except Exception as error:
                print(f"  close time unavailable for {ticker}: {error}")

        writer.write_manifest(
            RecordingManifest.create(
                environment="prod",
                tickers=tuple(tickers),
                channels=(ORDERBOOK_CHANNEL,),
                price_ranges_by_ticker=price_ranges,
                event_file=writer.event_path.name,
                started_at_utc=writer.started_at_utc,
                metadata={
                    "source": "rest_poll",
                    "poll_interval_seconds": args.interval,
                    "close_times_utc": close_times,
                    "caveat": (
                        "Polled snapshots, not the websocket feed. Net change per "
                        "interval only; volatility understated and queue position "
                        "approximate."
                    ),
                },
            )
        )
        # The replayer drives a real FeedController, which opens by sending a
        # subscribe command and waiting for Kalshi's acknowledgement. A recorded
        # websocket session contains that reply; a synthesised one has to as
        # well, with the id the controller will use for its first command.
        writer.write_event(
            {
                "type": "subscribed",
                "id": 1,
                "msg": {"channel": ORDERBOOK_CHANNEL, "sid": 1},
            }
        )

        print(f"Recording to {writer.directory}")

        state: dict[str, tuple[dict, dict]] = {}
        seq = 0
        started = time.monotonic()
        polls = 0

        while time.monotonic() - started < args.duration_sec:
            for ticker in tickers:
                try:
                    bids, asks = fetch_book(ticker)
                except Exception as error:
                    print(f"  poll failed for {ticker}: {type(error).__name__}")
                    continue

                if ticker not in state:
                    seq += 1
                    writer.write_event(snapshot_message(ticker, seq, bids, asks))
                else:
                    old_bids, old_asks = state[ticker]
                    for side, before, after in (
                        ("yes", old_bids, bids),
                        ("no", old_asks, asks),
                    ):
                        for message in delta_messages(ticker, seq, side, before, after):
                            seq += 1
                            message["seq"] = seq
                            writer.write_event(message)

                state[ticker] = (bids, asks)

            polls += 1

            if polls % 20 == 0:
                elapsed = time.monotonic() - started
                print(
                    f"  {elapsed:6.0f}s  {polls:5d} polls  {writer.event_count:7d} events",
                    flush=True,
                )

            await asyncio.sleep(args.interval)
    finally:
        writer.close()
        print(f"Done: {writer.event_count} events in {writer.directory}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="Explicit tickers to record.")
    parser.add_argument("--auto", type=int, default=0, help="Auto-pick the N busiest markets.")
    parser.add_argument("--min-volume", type=float, default=1000.0)
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between polls.")
    parser.add_argument("--duration-sec", type=float, default=900.0)
    parser.add_argument("--output", help="Recording directory.")
    parser.add_argument("--flush-every", type=int, default=50)
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
