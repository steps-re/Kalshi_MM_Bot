"""Bake the settled-history census and calibration results into site data.

    python analysis_app/build_exchange_census.py \\
        ~/kalshi-audit/settled_history.jsonl.gz \\
        ~/kalshi-audit/candles.jsonl ~/kalshi-audit/candles_breadth.jsonl

The raw corpus is ~10M markets and lives outside the repo. The site needs the
summary, not the corpus, so this reduces it once and writes
`data/exchange_census.json`. Same rule as the rest of the app: figures are
derived by a script that can be re-run, never typed into a page.

Three things end up in the file:

* the structural census - what Kalshi is made of by market count, which is the
  part almost nobody measures because it needs a full crawl;
* the tradeable-breadth table - which series settle repeatedly with populated
  tails, i.e. where diversification could actually come from;
* the pooled calibration trades by family, so the crypto result and its
  failure to replicate sit in the same table.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_at_t import LOOKBACKS, book_at, fee  # noqa: E402
from calibration_curves import family_of, opener  # noqa: E402
from cluster_stats import clustered  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "exchange_census.json"
TAIL_MAX = 0.05
FAVE_MIN = 0.80


def census(history: Path) -> dict:
    families: dict[str, dict] = defaultdict(
        lambda: {"markets": 0, "volume": 0.0, "series": set()})
    series_stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "volume": 0.0, "days": set(), "tails": 0, "faves": 0})
    days: set[str] = set()
    total = 0

    with opener(history) as handle:
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

            if volume <= 0:
                continue

            total += 1
            ticker = d.get("ticker", "")
            series = ticker.split("-", 1)[0]
            family = family_of(series)
            close = (d.get("close_time") or "")[:10]

            if close:
                days.add(close)

            fam = families[family]
            fam["markets"] += 1
            fam["volume"] += volume
            fam["series"].add(series)

            stat = series_stats[series]
            stat["n"] += 1
            stat["volume"] += volume
            stat["family"] = family

            if close:
                stat["days"].add(close)

            try:
                price = float(d["last_price_dollars"])
            except (KeyError, TypeError, ValueError):
                price = None

            if price is not None:
                if price <= TAIL_MAX:
                    stat["tails"] += 1
                elif price >= FAVE_MIN:
                    stat["faves"] += 1

    breadth = []

    for series, stat in series_stats.items():
        if stat["n"] < 100 or len(stat["days"]) < 5:
            continue

        if stat["tails"] + stat["faves"] < 50:
            continue

        breadth.append({
            "series": series,
            "family": stat.get("family", "other"),
            "markets": stat["n"],
            "days": len(stat["days"]),
            "per_day": round(stat["n"] / len(stat["days"]), 1),
            "tails": stat["tails"],
            "faves": stat["faves"],
            "avg_volume": round(stat["volume"] / stat["n"]),
        })

    breadth.sort(key=lambda r: -(r["tails"] + r["faves"]))
    return {
        "total_markets": total,
        "distinct_days": len(days),
        "day_span": [min(days), max(days)] if days else [],
        "families": sorted(
            ({"family": name,
              "markets": v["markets"],
              "series": len(v["series"]),
              "share": v["markets"] / max(total, 1)}
             for name, v in families.items()),
            key=lambda r: -r["markets"]),
        "breadth": breadth[:40],
        "breadth_total": len(breadth),
    }


def pooled(candle_paths: list[Path]) -> list[dict]:
    """The two pre-specified trades, by family, at each lookback."""

    cells: dict[tuple, dict] = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))

    for path in candle_paths:
        if not path.exists():
            continue

        for line in path.open():
            try:
                d = json.loads(line)
            except ValueError:
                continue

            if d.get("result") not in ("yes", "no"):
                continue

            candles = d.get("candles") or []

            if not candles:
                continue

            family = family_of(d.get("series", ""))
            day = datetime.fromtimestamp(
                d["close_ts"], tz=timezone.utc).date().isoformat()
            cluster = f"{d.get('series', '')}|{day}"
            won = 1 if d["result"] == "yes" else 0

            for lookback in LOOKBACKS:
                book = book_at(candles, d["close_ts"] - lookback)

                if book is None:
                    continue

                bid, ask = book
                mid = (bid + ask) / 2

                if mid <= TAIL_MAX:
                    zone, value = "tail SELL", bid - won - fee(bid)
                elif mid >= FAVE_MIN:
                    zone, value = "fave BUY", won - ask - fee(ask)
                else:
                    continue

                for fam in (family, "ALL"):
                    slot = cells[(lookback, fam, zone)][cluster]
                    slot[0] += value
                    slot[1] += 1

    rows = []

    for (lookback, family, zone), clusters in cells.items():
        n = sum(c for _, c in clusters.values())

        if n < 60:
            continue

        per = [total / count for total, count in clusters.values()]
        weights = [count for _, count in clusters.values()]
        mean = sum(p * w for p, w in zip(per, weights)) / sum(weights)
        _, groups, _, se = clustered(per, list(clusters.keys()))
        rows.append({
            "lookback_min": lookback // 60,
            "family": family,
            "trade": zone,
            "n": n,
            "clusters": groups,
            "net_cents": round(mean * 100, 3),
            "se_cents": round(se * 100, 3) if se == se else None,
        })

    rows.sort(key=lambda r: (r["lookback_min"], r["family"], r["trade"]))
    return rows


def main() -> None:
    history = Path(sys.argv[1]).expanduser()
    candles = [Path(p).expanduser() for p in sys.argv[2:]]
    print(f"census from {history} ...", flush=True)
    payload = census(history)
    print(f"  {payload['total_markets']:,} markets, "
          f"{payload['distinct_days']} days, "
          f"{payload['breadth_total']} qualifying series", flush=True)
    print(f"pooled trades from {len(candles)} candle file(s) ...", flush=True)
    payload["pooled"] = pooled(candles)
    payload["generated_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(payload['pooled'])} pooled rows)")


if __name__ == "__main__":
    main()
