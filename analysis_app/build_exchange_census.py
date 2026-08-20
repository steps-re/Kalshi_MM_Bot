"""Bake the settled-history census and calibration results into site data.

    python analysis_app/build_exchange_census.py \\
        ~/kalshi-audit/settled_history.jsonl.gz \\
        ~/kalshi-audit/candles.jsonl ~/kalshi-audit/candles_breadth.jsonl \\
        ~/kalshi-audit/candles_tennis.jsonl ~/kalshi-audit/candles_other.jsonl

The raw corpus is ~10M markets and lives outside the repo. The site needs the
summary, not the corpus, so this reduces it once and writes
`data/exchange_census.json`. Same rule as the rest of the app: figures are
derived by a script that can be re-run, never typed into a page - a rule this
file used to state while the page beside it carried a hand-typed decay table
that disagreed with this file's own output about the SIGN of the headline
number. Everything the page needs is emitted here now.

Five things end up in the file:

* the structural census - what Kalshi is made of by market count, which is the
  part almost nobody measures because it needs a full crawl;
* the tradeable-breadth table - which series settle repeatedly with populated
  tails. NOTE the tail/favourite counts are LAST PRINTS, which converge toward
  the outcome, so they are an upper bound on how populated those zones are.
  They are labelled as such in the payload and must be on the page;
* the pooled calibration trades by family and horizon;
* the DECAY PANELS, with the sample turnover between horizons made explicit and
  a balanced-panel version that holds the market set fixed;
* a `provenance` block: what was read, what was dropped, and how wide the
  search was, so nobody has to reverse-engineer it from the numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_core import (LOOKBACKS, MIN_CLUSTERS, MIN_CONTRACTS,  # noqa: E402
                              accumulate, decay_panel, load_records, summarize)
from calibration_curves import PARLAY_PREFIXES, family_of, opener  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "exchange_census.json"
TAIL_MAX = 0.05
FAVE_MIN = 0.80


def census(histories) -> dict:
    families: dict[str, dict] = defaultdict(
        lambda: {"markets": 0, "volume": 0.0, "series": set()})
    series_stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "volume": 0.0, "days": set(), "tails": 0, "faves": 0})
    days: set[str] = set()
    total = 0
    parlays = 0
    no_result = 0
    no_volume = 0
    duplicates = 0
    lines = 0
    # The crawl ships as one compact file plus dated slices, and the slices
    # SHARE their boundary days: `settled_to_2026-07-14` and
    # `settled_to_2026-07-19` both carry 2026-07-14. Summing raw line counts
    # across them - which is where the hand-typed "19.76 million / 18.48
    # million" on the page came from - double-counts those days.
    seen: set[str] = set()

    for history in histories:
        with opener(Path(history)) as handle:
            for line in handle:
                lines += 1

                try:
                    d = json.loads(line)
                except ValueError:
                    continue

                ticker = d.get("ticker", "")

                if ticker in seen:
                    duplicates += 1
                    continue

                seen.add(ticker)
                series = ticker.split("-", 1)[0]

                # Parlays are counted, then excluded. The previous version had a
                # `parlay-EXCLUDE` FAMILY and no filter, so parlays would have been
                # folded into `total_markets` and every `share` on the page if the
                # upstream crawl had left any in. It hadn't, so the guard had never
                # once fired and nothing said so.
                if series.startswith(PARLAY_PREFIXES):
                    parlays += 1
                    continue

                if d.get("result") not in ("yes", "no"):
                    no_result += 1
                    continue

                try:
                    volume = float(d.get("volume_fp") or d.get("volume") or 0)
                except (TypeError, ValueError):
                    continue

                if volume <= 0:
                    no_volume += 1
                    continue

                total += 1
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
            "tails_lastprice": stat["tails"],
            "faves_lastprice": stat["faves"],
            "avg_volume": round(stat["volume"] / stat["n"]),
        })

    breadth.sort(key=lambda r: -(r["tails_lastprice"] + r["faves_lastprice"]))
    return {
        "corpus_lines": lines,
        "corpus_unique_tickers": len(seen),
        "duplicate_tickers_across_files": duplicates,
        "total_markets": total,
        "distinct_days": len(days),
        "day_span": [min(days), max(days)] if days else [],
        "parlays_excluded": parlays,
        "dropped_no_result": no_result,
        "dropped_zero_volume": no_volume,
        "families": sorted(
            ({"family": name,
              "markets": v["markets"],
              "series": len(v["series"]),
              "share": v["markets"] / max(total, 1)}
             for name, v in families.items()),
            key=lambda r: -r["markets"]),
        "breadth": breadth[:40],
        "breadth_total": len(breadth),
        "breadth_basis": (
            "tails_lastprice/faves_lastprice count markets by LAST TRADED "
            "PRICE, which converges toward the outcome before settlement. "
            "Every market that ends up losing drifts into the tail on its way "
            "there, so these are an UPPER BOUND on how populated those zones "
            "were at a tradeable moment, not a measurement of it."),
    }


def parlay_actionability(path: Path) -> dict | None:
    """How many sampled parlay tickets had a book anyone could trade.

    Hand-typed on the page as 1,658 / 2,089 / 39 of 4,000 - three categories
    presented as a partition that sum to 3,786. Two reasons they did not
    reconcile: the file holds some non-parlay markets too, and "placeholder
    book" counted only the 0.001/1.00 case while other rejects (wide spread,
    crossed book) fell out of the list entirely. Derived here, exhaustively.

    `actionable` applies the staleness limit; `actionable_any_age` does not, and
    is what the old figure measured. The gap between them is how many of those
    "actionable" books were in fact minutes or hours old.
    """

    from calibration_core import MAX_STALENESS, book_at

    if not path.exists():
        return None

    rows = parlays = no_candles = no_book = actionable = any_age = 0

    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue

        rows += 1

        if not d.get("series", "").startswith(PARLAY_PREFIXES):
            continue

        parlays += 1
        candles = d.get("candles") or []

        if not candles:
            no_candles += 1
            continue

        if book_at(candles, d["close_ts"] - 300) is not None:
            actionable += 1
        else:
            no_book += 1

        if book_at(candles, d["close_ts"] - 300,
                   max_staleness=float("inf")) is not None:
            any_age += 1

    if not parlays:
        return None

    return {"file_rows": rows, "parlays": parlays,
            "non_parlay_rows": rows - parlays,
            "no_candles": no_candles, "no_actionable_book": no_book,
            "actionable": actionable, "actionable_any_age": any_age,
            "actionable_share": round(actionable / parlays, 5),
            "staleness_limit_s": MAX_STALENESS}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", nargs="+", required=True, type=Path,
                        help="every settled-history file in the crawl, "
                             "including the dated slices - the parlay share is "
                             "only right if the whole corpus is read")
    parser.add_argument("--candles", nargs="+", required=True, type=Path)
    args = parser.parse_args()

    histories = [p.expanduser() for p in args.history]
    candle_paths = [p.expanduser() for p in args.candles]

    for path in histories + candle_paths:
        if not path.exists():
            raise SystemExit(f"file not found: {path}")

    print(f"census from {len(histories)} history file(s) ...", flush=True)
    payload = census(histories)
    print(f"  {payload['corpus_lines']:,} lines, "
          f"{payload['duplicate_tickers_across_files']:,} duplicate tickers "
          f"across files", flush=True)
    print(f"  {payload['total_markets']:,} traded markets, "
          f"{payload['parlays_excluded']:,} parlays excluded, "
          f"{payload['dropped_zero_volume']:,} zero-volume dropped, "
          f"{payload['distinct_days']} days, "
          f"{payload['breadth_total']} qualifying series", flush=True)

    # `load_records` raises on a missing path rather than skipping it quietly,
    # and deduplicates: the four candle files share ~1,240 tickers because
    # separate targeted crawls re-drew markets an earlier pass already had.
    records, stats = load_records(candle_paths)
    print(f"pooled trades from {len(stats['files'])} candle file(s): "
          f"{stats['rows']:,} rows, {stats['duplicates']:,} duplicates dropped, "
          f"{len(records):,} unique markets", flush=True)

    accumulated = accumulate(records)
    payload["pooled"] = summarize(accumulated)
    payload["cells_tested"] = len(accumulated["cells"])
    payload["decay"] = [
        decay_panel(records, family, trade)
        for family, trade in sorted({(r["family"], r["trade"])
                                     for r in payload["pooled"]})
    ]

    parlay = parlay_actionability(
        next((p for p in candle_paths if "other" in p.name), candle_paths[0]))

    if parlay:
        payload["parlay_sample"] = parlay

    payload["provenance"] = {
        "history_files": [str(p) for p in histories],
        "corpus_lines": payload["corpus_lines"],
        "corpus_unique_tickers": payload["corpus_unique_tickers"],
        "duplicate_tickers_across_history_files":
            payload["duplicate_tickers_across_files"],
        "candle_files": stats["files"],
        "candle_rows_read": stats["rows"],
        "duplicate_tickers_dropped": stats["duplicates"],
        "unique_markets": len(records),
        "lookbacks_min": [lb // 60 for lb in LOOKBACKS],
        "min_contracts": MIN_CONTRACTS,
        "min_clusters": MIN_CLUSTERS,
        "cells_tested": payload["cells_tested"],
        "cells_reported": len(payload["pooled"]),
        "multiplicity_note": (
            f"The two TRADES were pre-specified. The horizon and the family "
            f"were not: {payload['cells_tested']} cells were computed and "
            f"{len(payload['pooled'])} clear the size gates. No multiplicity "
            f"adjustment is applied, so roughly "
            f"{payload['cells_tested'] * 0.05:.0f} would clear a 95% threshold "
            f"by chance. Read the table as a screen, not as a result."),
    }
    payload["generated_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(payload['pooled'])} pooled rows, "
          f"{len(payload['decay'])} decay panels)")


if __name__ == "__main__":
    main()
