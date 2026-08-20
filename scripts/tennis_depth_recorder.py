"""Record the tennis order book, continuously, to answer three open questions.

    python scripts/tennis_depth_recorder.py --out ~/kalshi-audit/tennis_book.jsonl

Uses the PUBLIC REST orderbook endpoint - no API key, no shared rate limit with
a trading account, nothing at risk.

## Why this exists

The settlement study measured a tennis favourite-BUY edge of +4.63c/contract at
ten minutes before the match ends. Two things block trading it, and this
recorder is the only way to unblock either.

**1. The entry rule is not implementable as measured.** `close_time` on a LIVE
Kalshi market is a placeholder weeks out; the real match-end is only stamped at
settlement. So "buy ten minutes before the end" is indexed to a timestamp
nobody can know in advance. And price does not substitute for it - an 80-90c
favourite is +10.32c at 0-10 minutes out and -0.41c at 40-90 minutes out, the
same observable price on both sides of the sign.

Buying blind across the final 90 minutes still measured +2.12c (t=4.2), but
that 90-minute window is itself defined by the unknowable close. A live bot
would also be sitting in the ~17 hours of pre-match quote before the match
starts, and NOTHING in the candle data says what happens there. This recorder
captures a market's whole life, so the pre-match period can finally be priced
and the "is the match actually in progress" filter can be tested against
something rather than assumed.

**2. Depth at the touch was unmeasured.** Candlesticks carry no book. A first
live probe says depth is NOT the binding constraint - a main-tour ATP match
showed 510,936 contracts resting at the best ask against 98,079 traded and
83,954 open interest. That makes the horizon problem above the whole game, and
it makes a 5-point mispricing sitting in front of a half-million-contract book
something to explain rather than celebrate. Depth is recorded anyway, per
market per poll, because the tours are liquid and ITF Futures may not be.

## What it writes

One JSON line per market per poll: full depth both sides in YES terms, the
touch, the spread, traded volume and open interest. Joining these to settlement
later gives the true match-end, so every observation can be labelled with its
real horizon after the fact - which is exactly the label the live system will
not have, and the point is to learn how to live without it.

Polite by construction: one list request per series every few minutes, and book
requests only for markets that are actually quoting, capped to a fixed budget.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Men's and women's ITF Futures, ATP Challenger, and the main tours. The tier
# split is a HYPOTHESIS to test out of sample, not a filter to apply: Futures
# showed roughly double the favourite-loss rate of the tours in-sample, and
# picking the winners off that same sample is the error this project keeps
# making. Record everything; decide later, on fresh data.
SERIES = ("KXITFMATCH", "KXITFWMATCH", "KXITFDOUBLES", "KXITFWDOUBLES",
          "KXATPCHALLENGERMATCH", "KXATPMATCH", "KXWTAMATCH",
          "KXATPSETWINNER", "KXWTASETWINNER")

# Record well below the 80c trade zone so a market is already being recorded
# when it walks into it - entries are what we are trying to price, and a market
# that first appears at 94c has lost the context that would explain it.
MIN_MID = 0.50
MAX_BOOK_REQUESTS = 45      # per cycle, keeps the request rate near 2/s
LIST_REFRESH_SEC = 240
POLL_SEC = 20
REQUEST_GAP = 0.4


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def get(path: str, params: dict | None = None, *, timeout: float = 20.0):
    url = f"{BASE}{path}"

    if params:
        url += "?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=timeout) as handle:
        return json.load(handle)


def num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def list_candidates() -> list[dict]:
    """Quoting tennis markets, with their TOUCH, from the market list.

    The list endpoint already carries `yes_bid_size_fp` / `yes_ask_size_fp`, so
    the touch costs one request per series rather than one per market. Only the
    busiest markets then need a full-book request.

    The field names are `yes_bid_dollars` / `yes_ask_dollars` / `volume_fp` and
    they are already in DOLLARS. A first version of this function read
    `yes_bid` / `yes_ask` / `volume`, which do not exist on this endpoint, so
    every market silently looked unquoted and the recorder wrote nothing while
    reporting healthy cycles. Guessing Kalshi field names has now cost this
    project two runs; verify against a real payload before trusting one.
    """

    out = []

    for series in SERIES:
        cursor = None

        for _page in range(6):
            params = {"status": "open", "series_ticker": series, "limit": 1000}

            if cursor:
                params["cursor"] = cursor

            try:
                data = get("/markets", params)
            except Exception as error:  # noqa: BLE001
                log(f"  list {series}: {type(error).__name__}")
                break

            for m in data.get("markets", []):
                bid = num(m.get("yes_bid_dollars"))
                ask = num(m.get("yes_ask_dollars"))

                if bid <= 0.001 or ask >= 0.9999 or ask <= bid:
                    continue

                if (bid + ask) / 2 < MIN_MID:
                    continue

                out.append({
                    "ticker": m["ticker"], "series": series,
                    "bid": bid, "ask": ask,
                    "bid_size": num(m.get("yes_bid_size_fp")),
                    "ask_size": num(m.get("yes_ask_size_fp")),
                    "volume": num(m.get("volume_fp")),
                    "volume_24h": num(m.get("volume_24h_fp")),
                    "open_interest": num(m.get("open_interest_fp")),
                    "last": num(m.get("last_price_dollars")),
                    "mid": (bid + ask) / 2,
                })

            cursor = data.get("cursor")

            if not cursor:
                break

            time.sleep(REQUEST_GAP)

        time.sleep(REQUEST_GAP)

    # Busiest first: if more markets quote than the book budget allows, the ones
    # carrying volume are the ones a live system could have traded.
    out.sort(key=lambda m: -m["volume"])
    return out


def fetch_book(ticker: str) -> dict | None:
    """Full depth, both sides expressed in YES prices.

    Kalshi returns the ask side in NO prices: a NO bid at 0.06 is a YES offer
    at 0.94. Converting here keeps one convention in the file.
    """

    try:
        data = get(f"/markets/{ticker}/orderbook", {"depth": 100})
    except Exception:  # noqa: BLE001
        return None

    book = data.get("orderbook_fp") or {}
    bids = sorted(((num(p), num(s)) for p, s in (book.get("yes_dollars") or [])),
                  key=lambda x: -x[0])
    asks = sorted(((1.0 - num(p), num(s)) for p, s in (book.get("no_dollars") or [])),
                  key=lambda x: x[0])

    if not bids or not asks:
        return None

    return {"bids": bids, "asks": asks}


def depth_within(levels: list, touch: float, cents: float, side: str) -> float:
    """Contracts resting within `cents` of the touch - what you could sweep."""

    limit = touch + cents / 100.0 if side == "ask" else touch - cents / 100.0
    return sum(size for price, size in levels
               if (price <= limit if side == "ask" else price >= limit))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=None,
                        help="stop after this many hours (default: run forever)")
    parser.add_argument("--series", nargs="+", default=None,
                        help="override the series list, e.g. for a smoke test")
    args = parser.parse_args()

    if args.series:
        globals()["SERIES"] = tuple(args.series)

    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.hours * 3600 if args.hours else None
    candidates: list[dict] = []
    refreshed = 0.0
    polls = wrote = 0

    log(f"recording {len(SERIES)} series -> {out}")

    with out.open("a", encoding="utf-8") as handle:
        while deadline is None or time.time() < deadline:
            cycle = time.time()

            if cycle - refreshed > LIST_REFRESH_SEC:
                candidates = list_candidates()
                refreshed = cycle
                live = sum(1 for m in candidates if m["volume"] > 0)
                log(f"{len(candidates)} quoting markets, {live} with volume "
                    f"(full book for top {min(live, MAX_BOOK_REQUESTS)})")

            if not candidates:
                time.sleep(POLL_SEC)
                polls += 1
                continue

            # Touch, volume and open interest for EVERY quoting market, from the
            # list refresh. Cheap, and it covers the pre-match hours that the
            # candle data never saw.
            for market in candidates:
                row = dict(market)
                row["ts"] = cycle
                row["kind"] = "touch"
                handle.write(json.dumps(row) + "\n")
                wrote += 1

            # Full depth only for the markets actually trading.
            busy = [m for m in candidates if m["volume"] > 0][:MAX_BOOK_REQUESTS]

            for market in busy:
                book = fetch_book(market["ticker"])
                time.sleep(REQUEST_GAP)

                if book is None:
                    continue

                best_bid, bid_size = book["bids"][0]
                best_ask, ask_size = book["asks"][0]

                if best_ask <= best_bid:
                    continue

                handle.write(json.dumps({
                    "ts": time.time(),
                    "kind": "book",
                    "ticker": market["ticker"],
                    "series": market["series"],
                    "bid": round(best_bid, 4),
                    "ask": round(best_ask, 4),
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    # the numbers the capacity question turns on
                    "ask_depth_1c": depth_within(book["asks"], best_ask, 1, "ask"),
                    "ask_depth_2c": depth_within(book["asks"], best_ask, 2, "ask"),
                    "ask_depth_5c": depth_within(book["asks"], best_ask, 5, "ask"),
                    "bid_depth_1c": depth_within(book["bids"], best_bid, 1, "bid"),
                    "bid_depth_5c": depth_within(book["bids"], best_bid, 5, "bid"),
                    "volume": market["volume"],
                    "open_interest": market["open_interest"],
                    "asks": book["asks"][:12],
                    "bids": book["bids"][:12],
                }) + "\n")
                wrote += 1

            handle.flush()
            polls += 1

            if polls % 10 == 0:
                log(f"{polls} cycles, {wrote:,} rows written")

            elapsed = time.time() - cycle

            if elapsed < POLL_SEC:
                time.sleep(POLL_SEC - elapsed)

    log(f"done: {polls} cycles, {wrote:,} snapshots -> {out}")


if __name__ == "__main__":
    main()
