"""Price Kalshi's BTC strikes against implied vol, and against Polymarket.

    python scripts/cross_venue.py
    python scripts/cross_venue.py --samples 20 --interval 30

Kalshi and Polymarket both run BTC strike markets expiring at the same instant,
on matching strikes, and they are **not the same contract**:

* Kalshi `KXBTCD-...-T63999.99` settles on the 60-second average of CF
  Benchmarks BRTI, a BTC/**USD** index.
* Polymarket "above $64,000" settles on a Binance 1-minute candle, BTC/**USDT**.

Those two underlyings differ by a live basis - measured at +$59 (9bps) on one
occasion - which is enough to give the same strike opposite moneyness on the
two venues at the same moment. So a raw price difference between them is not an
arbitrage and must never be traded as one.

This tool separates the part of any gap that the basis explains from the part
it does not, by pricing each venue against **its own** underlying:

    P(above K) = N(d2),  d2 = (ln(S/K) - sigma^2 T / 2) / (sigma sqrt(T))

with sigma from Deribit's DVOL index rather than from a guess. An earlier
back-of-envelope sigma attributed about 12 of a 26.5-point gap to the basis;
the same calculation with real implied vol attributed 16, and revealed that
Polymarket was trading within a point of its own fair value while Kalshi was
not. The difference between those two conclusions is the whole finding, and it
came entirely from replacing an estimate with a measurement.

## What it cannot do yet

Neither settlement source is directly readable from here. Kalshi settles on
BRTI, which we do not have a feed for; Polymarket settles on Binance, which is
geo-blocked from this location. Both are approximated - USD from Coinbase,
Kraken and Deribit's index, USDT from OKX - so the basis is an estimate of an
estimate. Sizing anything on this before those two feeds exist would be
trading a proxy and calling it a price.

Also unmodelled: Kalshi averages BRTI over sixty seconds while Polymarket takes
a single candle close. Averaging reduces terminal variance, which pushes a
below-strike contract slightly *further* below fair value than this prices. Some
of any residual gap is that, and this tool does not currently say how much.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from math import erf, log, sqrt
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import poll_record as pr  # noqa: E402

YEAR_SECONDS = 365.0 * 24 * 3600


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def probability_above(spot: float, strike: float, sigma: float, seconds: float) -> float:
    """Risk-neutral P(S_T > K) under zero drift.

    Zero drift on purpose: over minutes, any plausible drift is orders of
    magnitude below the diffusion term, and inventing one would be a free
    parameter with no evidence behind it.
    """

    if seconds <= 0 or sigma <= 0 or spot <= 0:
        return float("nan")

    t = seconds / YEAR_SECONDS
    vol = sigma * sqrt(t)
    d2 = (log(spot / strike) - 0.5 * vol * vol) / vol
    return normal_cdf(d2)


@dataclass(frozen=True, slots=True)
class Marks:
    usd: float
    usdt: float
    dvol: float

    @property
    def basis(self) -> float:
        return self.usdt - self.usd


def read_marks(client: httpx.Client) -> Marks | None:
    """Spot in both currencies, plus implied vol. None if a leg is missing.

    A missing leg is not something to substitute around: pricing USD exposure
    off a USDT quote is exactly the error this tool exists to expose.
    """

    usd: list[float] = []
    usdt: list[float] = []
    dvol: float | None = None

    try:
        r = client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker").json()
        usd.append((float(r["bid"]) + float(r["ask"])) / 2)
    except Exception:
        pass

    try:
        r = client.get(
            "https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}
        ).json()["result"]
        k = list(r.values())[0]
        usd.append((float(k["a"][0]) + float(k["b"][0])) / 2)
    except Exception:
        pass

    try:
        r = client.get(
            "https://www.deribit.com/api/v2/public/get_index_price",
            params={"index_name": "btc_usd"},
        ).json()
        usd.append(float(r["result"]["index_price"]))
    except Exception:
        pass

    try:
        r = client.get(
            "https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT"}
        ).json()["data"][0]
        usdt.append((float(r["bidPx"]) + float(r["askPx"])) / 2)
    except Exception:
        pass

    try:
        now = int(time.time() * 1000)
        r = client.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={
                "currency": "BTC",
                "start_timestamp": now - 86_400_000,
                "end_timestamp": now,
                "resolution": "3600",
            },
        ).json()
        rows = r.get("result", {}).get("data") or []
        dvol = float(rows[-1][4]) / 100.0 if rows else None
    except Exception:
        dvol = None

    if not usd or not usdt or dvol is None:
        return None

    return Marks(usd=st.median(usd), usdt=st.median(usdt), dvol=dvol)


def kalshi_strikes() -> dict[float, dict]:
    """Open KXBTCD markets keyed by strike in dollars."""

    out: dict[float, dict] = {}

    for market in pr.get(
        "/markets", {"status": "open", "limit": 200, "series_ticker": "KXBTCD"}
    ).get("markets", []):
        match = re.search(r"-T(\d+)\.(\d+)", market["ticker"])

        if not match:
            continue

        bid = pr._num(market.get("yes_bid_dollars"))
        ask = pr._num(market.get("yes_ask_dollars"))

        if not 0 < bid < ask < 1:
            continue

        # T63999.99 is "above $64,000".
        out[float(match.group(1)) + 1.0] = {
            "ticker": market["ticker"],
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2,
            "close": market.get("close_time"),
        }

    return out


def polymarket_strikes(client: httpx.Client) -> dict[float, dict]:
    """Strike -> live CLOB midpoint. One extra request per strike, deliberately."""

    try:
        rows = client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 500, "closed": "false", "order": "volume24hr", "ascending": "false"},
        ).json()
    except Exception:
        return {}

    out: dict[float, dict] = {}

    for market in rows:
        question = market.get("question") or ""
        match = re.search(r"price of Bitcoin be above \$([\d,]+) on (\w+ \d+)", question)

        if not match:
            continue

        try:
            tokens = json.loads(market.get("clobTokenIds") or "[]")
        except (TypeError, ValueError):
            tokens = []

        out[float(match.group(1).replace(",", ""))] = {
            # Deliberately NOT gamma's outcomePrices/bestBid/bestAsk. Those are
            # cached and were measured 14 points stale against the live book -
            # 0.83 against a real midpoint of 0.97 - which is more than large
            # enough to invent an edge that does not exist. The live CLOB
            # midpoint agreed with fair value to within a cent at the same
            # instant, so the "mispricing" was entirely the cache.
            "yes": clob_midpoint(client, tokens[0]) if tokens else None,
            "stale_gamma": market.get("bestBid"),
            "end": str(market.get("endDate")),
        }

    return out


def clob_midpoint(client: httpx.Client, token_id: str) -> float | None:
    """Live midpoint from Polymarket's order book.

    Returns None rather than falling back to a cached price: a stale quote that
    looks like a live one is the failure this function exists to prevent.
    """

    try:
        response = client.get(
            "https://clob.polymarket.com/midpoint", params={"token_id": token_id}
        ).json()
        return float(response["mid"])
    except Exception:
        return None


def seconds_to(close_time: str | None) -> float | None:
    if not close_time:
        return None

    try:
        close = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
    except ValueError:
        return None

    remaining = (close - datetime.now(UTC)).total_seconds()
    return remaining if remaining > 0 else None


def report_once(client: httpx.Client) -> None:
    marks = read_marks(client)

    if marks is None:
        print("could not read spot/vol on every leg - refusing to price a partial set")
        return

    kalshi = kalshi_strikes()
    poly = polymarket_strikes(client)

    print(
        f"USD {marks.usd:,.2f}   USDT {marks.usdt:,.2f}   basis {marks.basis:+.2f}"
        f"   DVOL {marks.dvol * 100:.1f}%"
    )
    print(
        f"{'strike':>9}{'left':>7}{'K mid':>8}{'K fair':>8}{'K edge':>8}"
        f"{'P mid':>8}{'P fair':>8}{'P edge':>8}{'basis pts':>10}"
    )

    for strike in sorted(set(kalshi) & set(poly)):
        k = kalshi[strike]
        p = poly[strike]
        left = seconds_to(k["close"])

        if left is None or p["yes"] is None:
            continue

        k_fair = probability_above(marks.usd, strike, marks.dvol, left)
        p_fair = probability_above(marks.usdt, strike, marks.dvol, left)

        print(
            f"{strike:>9,.0f}{left / 60:>6.0f}m{k['mid']:>8.3f}{k_fair:>8.3f}"
            f"{(k['mid'] - k_fair) * 100:>+7.1f}c{p['yes']:>8.3f}{p_fair:>8.3f}"
            f"{(p['yes'] - p_fair) * 100:>+7.1f}c{(p_fair - k_fair) * 100:>+9.1f}"
        )

    if not (set(kalshi) & set(poly)):
        print("  no strike open on both venues right now")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()

    client = httpx.Client(timeout=25, follow_redirects=True)

    for index in range(args.samples):
        if index:
            time.sleep(args.interval)

        print(f"--- {datetime.now(UTC).strftime('%H:%M:%S')}")
        report_once(client)


if __name__ == "__main__":
    main()
