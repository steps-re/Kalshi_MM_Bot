"""Crawl the exchange's settled history and its open-market census.

    python scripts/settlement_crawl.py --days 90 --out ~/kalshi-audit/settled.jsonl
    python scripts/settlement_crawl.py --open-census --out ~/kalshi-audit/open_census.jsonl

Two questions, one crawler:

1. **Calibration** (settled markets). Every settled market carries its result.
   Joined with prices at T-minus-X (candlesticks, stage 2), that answers the
   favorite-longshot question directly: do 3-5c contracts settle YES at 3-5%,
   or less? The structural prize: a resting entry pays no fee and settlement
   pays no fee, so any measured miscalibration at the tails is harvestable
   without ever paying the taker tax, and hold-to-settlement has no exit-fill
   problem - the failure mode that stranded a fifth of the live run's round
   trips simply does not exist for it.

2. **Venue census** (open markets). One page-through of everything open, with
   volume and open interest, grouped by series. This is where the weather
   markets either show enough liquidity to bother with or don't.

Scope note: the settled crawl is bounded by --days because the exchange's full
history is enormous and the question does not need it. The market LIST is cheap
(1,000 per page); per-market candlesticks are not, so stage 2
(`settlement_candles.py`) restricts to target series.

Politeness: ~5 requests/second with exponential backoff on 429. This runs for
minutes, not hours, and shares the account's rate limit with any live process,
so it stays well under the cap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.rest import KalshiRestClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402

REQUEST_GAP = 0.2          # seconds between requests, ~5/s
# The 90-day first crawl wrote 27GB of raw JSON onto a disk at 99% and had to
# be compacted after the fact. Filtered fields, gz, and traded-only are now
# applied AT WRITE TIME.
KEEP = ("ticker", "event_ticker", "result", "close_time", "open_time",
        "last_price_dollars", "previous_yes_bid_dollars",
        "previous_yes_ask_dollars", "volume", "volume_fp",
        "open_interest_fp", "liquidity_dollars", "market_type", "status")
# Bulky text fields nobody's calibration needs. Everything else is kept raw so
# the analysis layer, not the crawl, decides what matters.
DROP_FIELDS = ("rules_primary", "rules_secondary", "settlement_sources",
               "custom_strike")


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


async def throttled(request, *args, **kwargs):
    """One request with backoff. A 429 means slow down, not fail."""

    delay = REQUEST_GAP

    for attempt in range(6):
        try:
            result = await request(*args, **kwargs)
            await asyncio.sleep(REQUEST_GAP)
            return result
        except Exception as error:  # noqa: BLE001
            text = str(error)

            if "429" in text or "rate" in text.lower():
                delay = min(delay * 2, 10.0)
                log(f"  rate limited, backing off {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            raise

    raise RuntimeError("still rate limited after 6 backoffs")


def slim(market: dict) -> dict:
    for field in DROP_FIELDS:
        market.pop(field, None)

    return market


async def crawl(status: str, out_path: Path, days: int | None,
                before: str | None = None, traded_only: bool = False) -> None:
    settings = load_settings()
    environment = settings.environment(prod=True)
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
    rest = KalshiRestClient(environment.rest_base_url, auth)

    params_extra: dict = {}
    end = (datetime.fromisoformat(before).replace(tzinfo=timezone.utc)
           if before else datetime.now(timezone.utc))

    if days is not None and status == "settled":
        params_extra["min_close_ts"] = int((end - timedelta(days=days)).timestamp())

    if before is not None:
        params_extra["max_close_ts"] = int(end.timestamp())

    cursor = None
    total = 0
    pages = 0
    started = time.time()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import gzip as _gzip
    open_out = ((lambda: _gzip.open(out_path, "wt", compresslevel=6))
                if out_path.suffix == ".gz" else
                (lambda: out_path.open("w", encoding="utf-8")))

    with open_out() as handle:
        while True:
            # list_markets does not pass min_close_ts, so call the endpoint
            # directly for the settled crawl.
            params = {"status": status, "limit": 1000, **params_extra}

            if cursor:
                params["cursor"] = cursor

            try:
                data = await throttled(rest._request, "GET", "/markets",
                                       params=params)
            except Exception as error:  # noqa: BLE001
                if params_extra and "min_close_ts" in str(error):
                    # Older API spellings vary; fall back to unfiltered and
                    # let the analyzer window on close_time client-side.
                    log("min_close_ts rejected, falling back to unfiltered")
                    params_extra.clear()
                    continue

                raise

            markets = list(data.get("markets") or ())
            cursor = data.get("cursor") or None

            for market in markets:
                if traded_only:
                    try:
                        volume = float(market.get("volume_fp")
                                       or market.get("volume") or 0)
                    except (TypeError, ValueError):
                        volume = 0.0

                    if volume <= 0:
                        continue

                    market = {k: market.get(k) for k in KEEP
                              if market.get(k) is not None}

                handle.write(json.dumps(slim(market)) + "\n")

            total += len(markets)
            pages += 1

            if pages % 20 == 0:
                rate = total / max(time.time() - started, 1)
                log(f"{total} markets, page {pages}, {rate:.0f}/s")

            if not cursor or not markets:
                break

    await rest.close()
    log(f"done: {total} {status} markets -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="settled history window (list crawl only)")
    parser.add_argument("--open-census", action="store_true",
                        help="crawl open markets instead of settled")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--before", default=None,
                        help="ISO date; crawl the window ending here")
    parser.add_argument("--traded-only", action="store_true",
                        help="write only traded markets, compact fields")
    args = parser.parse_args()
    status = "open" if args.open_census else "settled"
    asyncio.run(crawl(status, args.out, None if args.open_census else args.days,
                      before=args.before, traded_only=args.traded_only))


if __name__ == "__main__":
    main()
