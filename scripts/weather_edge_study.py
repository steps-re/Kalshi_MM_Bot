"""Does the public NBM forecast beat the Kalshi NYC-high-temp market?

    python scripts/weather_edge_study.py --days 90

The one front-facing prediction idea rated above 20%: KXHIGHNY settles on the
Central Park climate report, the counterparty pool is thin, and the strongest
public forecasting stack (NOAA's National Blend of Models) is free. This scores
that idea offline, no money involved:

  per settled bracket-market, compare
    market probability   = candlestick mid at a fixed snapshot hour
    forecast probability = Normal(NBM point forecast, sigma of its own recent
                           day-ahead errors) mass on the bracket
  against the settled outcome, by Brier score.

Conventions are CALIBRATED, not assumed: the NBM extended product's max-temp
field (`txn`) is matched to actual highs under both plausible ftime mappings
and the one with sane errors wins (the other is reported). The market snapshot
is taken AFTER the forecast run becomes available, giving the market an
information advantage - a conservative tilt, stated rather than hidden.

Outputs Brier per side with a clustered (by day) difference, the days each side
"won", and the calibration table. If the market wins or ties, the idea is dead
and no live experiment follows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics as st
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.rest import KalshiRestClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402

STATION = "KNYC"
SERIES = "KXHIGHNY"
SNAPSHOT_UTC_HOUR = 12          # 8am ET: NBM 00Z run is ~10h old, market awake
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def event_date(ticker: str):
    """KXHIGHNY-26AUG19-T92 -> date(2026, 8, 19)."""

    part = ticker.split("-")[1]
    return datetime(2000 + int(part[:2]), MONTHS[part[2:5]], int(part[5:7])).date()


def actual_highs(year: int) -> dict:
    rows = fetch_json(
        f"https://mesonet.agron.iastate.edu/json/cli.py?station={STATION}&year={year}"
    )["results"]
    return {
        datetime.fromisoformat(r["valid"]).date(): r["high"]
        for r in rows
        if isinstance(r.get("high"), (int, float))
    }


def nbm_forecasts(dates) -> dict:
    """date -> {'d1': txn from previous day's 00Z run, 'd0': same-day 00Z run}.

    Both candidate conventions are pulled; calibration decides which ftime
    mapping is real. `txn` at an 00Z ftime is a daytime max, at 12Z a min.
    """

    out: dict = defaultdict(dict)
    runtimes = {d - timedelta(days=1) for d in dates} | set(dates)

    for run_day in sorted(runtimes):
        url = (f"https://mesonet.agron.iastate.edu/api/1/mos.json?station={STATION}"
               f"&model=NBE&runtime={run_day:%Y-%m-%d}T00:00:00Z")

        try:
            rows = fetch_json(url).get("data", [])
        except Exception:  # noqa: BLE001
            continue

        for row in rows:
            if row.get("txn") is None:
                continue

            ftime = datetime.fromisoformat(row["ftime_utc"].replace(".000", ""))

            if ftime.hour != 0:
                continue          # 12Z rows are overnight minima

            # Two plausible conventions for which calendar day this max covers.
            for label, target in (("A", ftime.date() - timedelta(days=1)),
                                  ("B", ftime.date())):
                offset = (target - run_day).days
                key = f"{label}{offset}"

                if 0 <= offset <= 2:
                    out[target].setdefault(key, row["txn"])

    return out


def pick_convention(forecasts: dict, actuals: dict):
    """Choose the (label, offset) whose day-ahead errors are sane; report all."""

    scores = defaultdict(list)

    for day, byconv in forecasts.items():
        actual = actuals.get(day)

        if actual is None:
            continue

        for key, value in byconv.items():
            scores[key].append(value - actual)

    table = []

    for key, errs in sorted(scores.items()):
        if len(errs) < 10:
            continue

        table.append((key, len(errs), st.mean(errs), st.pstdev(errs),
                      st.mean([abs(e) for e in errs])))

    print("\nNBM txn convention calibration (key = mapping + days ahead):")
    print(f"{'key':>5}{'n':>6}{'bias':>8}{'sigma':>8}{'MAE':>7}")

    for key, n, bias, sigma, mae in table:
        print(f"{key:>5}{n:>6}{bias:>+8.2f}{sigma:>8.2f}{mae:>7.2f}")

    # Day-ahead forecast: offset 1, whichever mapping has the lower MAE.
    day_ahead = [t for t in table if t[0] in ("A1", "B1")]

    if not day_ahead:
        sys.exit("no day-ahead convention calibrates - abort")

    best = min(day_ahead, key=lambda t: t[4])
    print(f"chosen: {best[0]} (bias {best[2]:+.2f}, sigma {best[3]:.2f})")
    return best[0], best[2], best[3]


def bracket_prob(forecast: float, bias: float, sigma: float,
                 floor, cap) -> float:
    """P(actual in bracket) under Normal(forecast - bias, sigma).

    Kalshi temperature brackets settle on integer-reported highs; the strike
    conventions: T-type is (floor, inf), B-type is [floor, cap] inclusive of
    the integers between. Half-integer continuity correction on both edges.
    """

    mu = forecast - bias

    def cdf(x):
        return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

    lo = -math.inf if floor is None else floor + 0.5
    hi = math.inf if cap is None else cap + 0.5
    lo_p = 0.0 if lo == -math.inf else cdf(lo)
    hi_p = 1.0 if hi == math.inf else cdf(hi)
    return max(1e-4, min(1 - 1e-4, hi_p - lo_p))


async def market_snapshots(rest, markets):
    """ticker -> (market prob at the snapshot, close_ts). One call per market."""

    out = {}

    for market in markets:
        ticker = market["ticker"]
        close = int(datetime.fromisoformat(
            market["close_time"].replace("Z", "+00:00")).timestamp())
        day = event_date(ticker)
        snap = int(datetime(day.year, day.month, day.day, SNAPSHOT_UTC_HOUR,
                            tzinfo=timezone.utc).timestamp())

        try:
            data = await rest._request(
                "GET", f"/series/{SERIES}/markets/{ticker}/candlesticks",
                params={"start_ts": snap - 6 * 3600, "end_ts": snap,
                        "period_interval": 60})
        except Exception:  # noqa: BLE001
            continue

        candles = data.get("candlesticks", [])

        if not candles:
            continue

        last = candles[-1]
        bid = float(last["yes_bid"]["close_dollars"])
        ask = float(last["yes_ask"]["close_dollars"])

        if ask <= 0 or ask >= 1 and bid <= 0:
            continue

        out[ticker] = (bid + ask) / 2

    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    settings = load_settings()
    env = settings.environment(prod=True)
    rest = KalshiRestClient(env.rest_base_url,
                            KalshiAuth(settings.api_key_id,
                                       settings.private_key_path))
    markets = []
    cursor = None

    while True:
        params = {"series_ticker": SERIES, "status": "settled", "limit": 200}

        if cursor:
            params["cursor"] = cursor

        data = await rest._request("GET", "/markets", params=params)
        markets += data.get("markets", [])
        cursor = data.get("cursor")

        if not cursor or len(markets) > args.days * 10:
            break

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=args.days)
    markets = [m for m in markets
               if m.get("result") in ("yes", "no")
               and event_date(m["ticker"]) >= cutoff]
    days = sorted({event_date(m["ticker"]) for m in markets})
    print(f"{len(markets)} settled {SERIES} bracket-markets over {len(days)} days")

    years = {d.year for d in days}
    actuals = {}

    for year in years:
        actuals.update(actual_highs(year))

    print(f"actual highs loaded for {len(actuals)} days")
    forecasts = nbm_forecasts(days)
    conv, bias, sigma = pick_convention(forecasts, actuals)
    print("\npulling market snapshots (one candlestick call per market)...")
    snaps = await market_snapshots(rest, markets)
    print(f"{len(snaps)} markets with a {SNAPSHOT_UTC_HOUR:02d}Z quote")

    rows = []

    for market in markets:
        ticker = market["ticker"]
        day = event_date(ticker)
        fc = forecasts.get(day, {}).get(conv)
        prob_mkt = snaps.get(ticker)

        if fc is None or prob_mkt is None:
            continue

        outcome = 1.0 if market["result"] == "yes" else 0.0
        prob_fc = bracket_prob(fc, bias, sigma,
                               market.get("floor_strike"),
                               market.get("cap_strike"))
        rows.append({"day": day, "ticker": ticker, "y": outcome,
                     "mkt": prob_mkt, "fc": prob_fc})

    if len(rows) < 50:
        sys.exit(f"only {len(rows)} scoreable markets - not enough")

    briers_by_day = defaultdict(lambda: [0.0, 0.0, 0])

    for row in rows:
        slot = briers_by_day[row["day"]]
        slot[0] += (row["mkt"] - row["y"]) ** 2
        slot[1] += (row["fc"] - row["y"]) ** 2
        slot[2] += 1

    diffs = [(b[1] - b[0]) / b[2] for b in briers_by_day.values()]
    mkt_brier = st.mean([(r["mkt"] - r["y"]) ** 2 for r in rows])
    fc_brier = st.mean([(r["fc"] - r["y"]) ** 2 for r in rows])
    se = st.stdev(diffs) / math.sqrt(len(diffs))
    wins = sum(1 for d in diffs if d < 0)

    print(f"\n{'=' * 60}")
    print(f"{len(rows)} markets, {len(diffs)} days. Brier (lower is better):")
    print(f"  market  {mkt_brier:.4f}")
    print(f"  NBM     {fc_brier:.4f}")
    print(f"  per-day difference (NBM - market): {st.mean(diffs):+.4f} "
          f"+/- {se:.4f}  ({'NBM better' if st.mean(diffs) < 0 else 'market better'})")
    print(f"  NBM wins {wins}/{len(diffs)} days")
    print("\nThe market quote is from AFTER the forecast run (its info advantage,")
    print("stated). NBM materially better -> a real lead worth a live design.")
    print("Tie or market better -> the idea is dead, no live test follows.")
    await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
