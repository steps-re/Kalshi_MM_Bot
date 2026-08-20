"""Stage 2: prices at a FIXED time before close, for clean calibration.

    python scripts/settlement_candles.py ~/kalshi-audit/settled_compact.jsonl.gz \\
        --out ~/kalshi-audit/candles.jsonl --per-family 2500

The zeroth pass calibrated last prints, which converge toward the outcome and
mechanically manufacture the favorite-longshot pattern. This stage fetches
1-minute candlesticks for the final 90 minutes of a stratified sample of
settled markets, so the analyzer can read the price at T-minus-5/15/60 minutes
- prices a trader could actually have acted on, before convergence.

Resumable on purpose: tickers already in the output file are skipped, so a
killed run continues where it stopped. Runs are polite (~4 requests/second,
backoff on 429) and share the account's rate limit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.rest import KalshiRestClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402
from calibration_curves import family_of, opener  # noqa: E402

REQUEST_GAP = 0.25
LOOKBACK_MINUTES = 90
# Deterministic sample so a resumed run draws the same markets.
SEED = 20260820


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def parse_close(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def choose(settled: Path, per_family: int, families: set[str] | None = None,
           min_volume: float = 0.0) -> list[dict]:
    """Stratified deterministic sample of traded, settled markets.

    `families` restricts to named families; `min_volume` targets markets with
    real books. The 'other' family taught the need for both: a uniform draw
    from it is dominated by sparse oddities, and only 9 of 2,500 had an
    actionable book five minutes before close.
    """

    pools: dict[str, list[dict]] = defaultdict(list)

    with opener(settled) as handle:
        for line in handle:
            try:
                d = json.loads(line)
            except ValueError:
                continue

            if d.get("result") not in ("yes", "no"):
                continue

            try:
                volume = float(d.get("volume_fp") or d.get("volume") or 0)
            except (TypeError, ValueError):
                continue

            if volume <= 0 or volume < min_volume:
                continue

            close = parse_close(d.get("close_time", ""))

            if close is None:
                continue

            series = d.get("ticker", "").split("-", 1)[0]
            family = family_of(series)

            if families is not None and family not in families:
                continue

            pools[family].append({
                "ticker": d["ticker"],
                "series": series,
                "event": d.get("event_ticker"),
                "result": d["result"],
                "close_ts": close,
            })

    rng = random.Random(SEED)
    sample: list[dict] = []

    for family, pool in sorted(pools.items()):
        take = pool if len(pool) <= per_family else rng.sample(pool, per_family)
        sample.extend(take)
        log(f"  {family}: {len(take)} of {len(pool)}")

    return sample


async def fetch(sample: list[dict], out_path: Path) -> None:
    done = set()

    if out_path.exists():
        for line in out_path.open():
            try:
                done.add(json.loads(line)["ticker"])
            except (ValueError, KeyError):
                continue

        log(f"resuming: {len(done)} already fetched")

    settings = load_settings()
    environment = settings.environment(prod=True)
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
    rest = KalshiRestClient(environment.rest_base_url, auth)
    fetched = failures = 0
    delay = REQUEST_GAP

    with out_path.open("a", encoding="utf-8") as handle:
        for market in sample:
            if market["ticker"] in done:
                continue

            end_ts = int(market["close_ts"])
            start_ts = end_ts - LOOKBACK_MINUTES * 60
            path = (f"/series/{market['series']}/markets/"
                    f"{market['ticker']}/candlesticks")
            params = {"start_ts": start_ts, "end_ts": end_ts,
                      "period_interval": 1}

            data = None

            for _attempt in range(6):
                try:
                    data = await rest._request("GET", path, params=params)
                    delay = REQUEST_GAP
                    break
                except Exception as error:  # noqa: BLE001
                    if "429" in str(error):
                        # Same market, longer wait. Skipping on a 429 would
                        # silently thin the sample by exactly the rate limit.
                        delay = min(delay * 2, 15.0)
                        await asyncio.sleep(delay)
                        continue

                    failures += 1

                    if failures <= 5 or failures % 100 == 0:
                        log(f"  {market['ticker']}: {type(error).__name__} "
                            f"(failure {failures})")

                    break

            if data is None:
                await asyncio.sleep(REQUEST_GAP)
                continue

            candles = data.get("candlesticks") or []
            handle.write(json.dumps({
                "ticker": market["ticker"],
                "series": market["series"],
                "event": market["event"],
                "result": market["result"],
                "close_ts": end_ts,
                "candles": candles,
            }) + "\n")
            handle.flush()
            fetched += 1

            if fetched % 200 == 0:
                log(f"{fetched} fetched, {failures} failures, "
                    f"{len(sample) - len(done) - fetched} to go")

            await asyncio.sleep(REQUEST_GAP)

    await rest.close()
    log(f"done: {fetched} fetched this run, {failures} failures -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("settled", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-family", type=int, default=2500)
    parser.add_argument("--families", nargs="+", default=None)
    parser.add_argument("--min-volume", type=float, default=0.0)
    parser.add_argument("--gap", type=float, default=None,
                        help="request gap override, e.g. 0.4 when sharing the "
                             "rate limit with a running crawl")
    args = parser.parse_args()

    if args.gap:
        globals()["REQUEST_GAP"] = args.gap

    log("sampling...")
    sample = choose(args.settled, args.per_family,
                    set(args.families) if args.families else None,
                    args.min_volume)
    log(f"{len(sample)} markets in the sample")
    asyncio.run(fetch(sample, args.out))


if __name__ == "__main__":
    main()
