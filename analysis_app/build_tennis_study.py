"""Bake the tennis favourite-BUY study into site data.

    python analysis_app/build_tennis_study.py \\
        --candles ~/kalshi-audit/candles*.jsonl \\
        --settled ~/kalshi-audit/settled_compact.jsonl.gz \\
        --book ~/kalshi-audit/tennis_book.jsonl

Same rule as everywhere else in this app: figures are derived by a script that
can be re-run, never typed into a page.

The study asks whether the one surviving lead in this project - buying tennis
favourites near the end of a match - is a trade or a measurement. The answer
turns on a distinction the rest of the project never had to make, between what
is TRUE of the data and what is KNOWABLE at the moment of the trade.

`close_time` on a live Kalshi market is a scheduled placeholder, often weeks
out; the real match-end is stamped at settlement. Every "T-minus-X" number in
the calibration work is therefore indexed to a timestamp that did not exist
while the market was tradeable. The `lookahead` block proves this from the live
recorder's own rows rather than asserting it, and the `implementable` block
re-measures the trade using only state a live system can see.
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from calibration_core import (MAX_SPREAD, book_at, cluster_key, fee,  # noqa: E402
                              load_records, zone_of)
from calibration_curves import family_of, opener  # noqa: E402
from cluster_stats import clustered_pooled, loss_count_floor  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "tennis_study.json"

PRICE_BUCKETS = ((0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 0.999))
HORIZONS = (("0-10", 0, 10), ("10-20", 10, 20), ("20-40", 20, 40),
            ("40-90", 40, 90))
TIERS = {"KXITFMATCH": "ITF Futures", "KXITFWMATCH": "ITF Futures",
         "KXITFDOUBLES": "ITF Futures", "KXITFWDOUBLES": "ITF Futures",
         "KXATPCHALLENGERMATCH": "Challenger"}


def stat(clusters: dict) -> dict:
    """Pooled mean with a cluster-robust SE floored by loss-count uncertainty."""

    sums = [v[0] for v in clusters.values()]
    counts = [v[1] for v in clusters.values()]
    losses = [v[2] for v in clusters.values()]
    n, groups, mean, se = clustered_pooled(sums, counts)

    if not n:
        return {}

    se = max(se if se == se else 0.0, loss_count_floor(losses, n))
    return {"n": n, "clusters": groups, "losses": int(sum(losses)),
            "loss_rate": round(sum(losses) / n, 5),
            "ev_cents": round(mean * 100, 3),
            "se_cents": round(se * 100, 3),
            "t": round(mean / se, 2) if se else None}


def tennis_rows(records):
    """Every actionable fave-priced candle minute in the final 90 minutes."""

    for d in records:
        if d.get("result") not in ("yes", "no"):
            continue

        if family_of(d.get("series", "")) != "tennis":
            continue

        won = 1 if d["result"] == "yes" else 0
        day = datetime.fromtimestamp(
            d["close_ts"], tz=timezone.utc).date().isoformat()
        cluster = cluster_key("tennis", d.get("series", ""), day)
        candles = sorted((k for k in (d.get("candles") or [])
                          if k.get("end_period_ts")),
                         key=lambda k: k["end_period_ts"])

        for i, k in enumerate(candles):
            age = (d["close_ts"] - k["end_period_ts"]) / 60

            if not 0 <= age <= 90:
                continue

            try:
                bid = float(k["yes_bid"]["close_dollars"])
                ask = float(k["yes_ask"]["close_dollars"])
            except (KeyError, TypeError, ValueError):
                continue

            if bid <= 0.001 or ask >= 0.9999 or ask <= bid:
                continue

            if ask - bid > MAX_SPREAD:
                continue

            if zone_of((bid + ask) / 2) != "fave BUY":
                continue

            trailing = []

            for j in range(i, max(i - 5, -1), -1):
                try:
                    trailing.append(float(candles[j].get("volume_fp") or 0))
                except (TypeError, ValueError):
                    trailing.append(0.0)

            growth = None

            if i >= 5:
                try:
                    before = float(candles[i - 5].get("open_interest_fp") or 0)
                    now = float(k.get("open_interest_fp") or 0)
                    growth = (now - before) / before if before > 0 else None
                except (TypeError, ValueError):
                    growth = None

            yield {"cluster": cluster, "series": d.get("series", ""),
                   "age": age, "bid": bid, "ask": ask, "won": won,
                   "pnl": won - ask - fee(ask), "lost": 1 - won,
                   "trailing": trailing, "oi_growth": growth,
                   "ticker": d["ticker"], "day": day}


def entry_study(records) -> dict:
    """The T-10 headline, the horizon curve, and the price x horizon grid."""

    rows = list(tennis_rows(records))
    at_ten = defaultdict(lambda: [0.0, 0, 0])
    seen = set()
    per_day = defaultdict(set)

    for d in records:
        if d.get("result") not in ("yes", "no"):
            continue

        if family_of(d.get("series", "")) != "tennis":
            continue

        book = book_at(d.get("candles") or [], d["close_ts"] - 600)

        if not book or zone_of((book[0] + book[1]) / 2) != "fave BUY":
            continue

        won = 1 if d["result"] == "yes" else 0
        day = datetime.fromtimestamp(
            d["close_ts"], tz=timezone.utc).date().isoformat()
        slot = at_ten[cluster_key("tennis", d.get("series", ""), day)]
        slot[0] += won - book[1] - fee(book[1])
        slot[1] += 1
        slot[2] += 1 - won
        seen.add((d["ticker"], book[1]))
        per_day[day].add(d["ticker"])

    headline = stat(at_ten)
    avg_ask = sum(a for _, a in seen) / len(seen) if seen else 0.0
    headline.update({
        "avg_ask_cents": round(avg_ask * 100, 2),
        "breakeven_loss_rate": round(1 - (avg_ask + fee(avg_ask)), 5),
        "return_on_capital": round(headline["ev_cents"] / 100 / avg_ask, 5)
        if avg_ask else None,
        "days": len(per_day),
        "markets_per_day": round(len(seen) / max(len(per_day), 1), 1),
    })

    curve, grid = [], []

    for label, lo, hi in HORIZONS:
        cells = defaultdict(lambda: [0.0, 0, 0])

        for r in rows:
            if lo <= r["age"] < hi:
                s = cells[r["cluster"]]
                s[0] += r["pnl"]
                s[1] += 1
                s[2] += r["lost"]

        row = stat(cells)

        if row:
            row["horizon"] = label
            curve.append(row)

    for plo, phi in PRICE_BUCKETS:
        entry = {"price": f"{plo * 100:.0f}-{phi * 100:.0f}c", "cells": []}

        for label, lo, hi in HORIZONS:
            cells = defaultdict(lambda: [0.0, 0, 0])

            for r in rows:
                mid = (r["bid"] + r["ask"]) / 2

                if plo <= mid < phi and lo <= r["age"] < hi:
                    s = cells[r["cluster"]]
                    s[0] += r["pnl"]
                    s[1] += 1
                    s[2] += r["lost"]

            cell = stat(cells)
            cell["horizon"] = label

            if cell.get("n", 0) < 100:
                cell = {"horizon": label, "n": cell.get("n", 0), "thin": True}

            entry["cells"].append(cell)

        grid.append(entry)

    return {"headline_t10": headline, "horizon_curve": curve,
            "price_horizon_grid": grid, "rows": rows}


def implementable(rows) -> list[dict]:
    """The same trade, restricted to state a LIVE system can actually see.

    Pre-specified and mechanically motivated. No searching: each rule is a
    plain observable, and the ones that fail are reported alongside the ones
    that do not, because "we tried four proxies and none recovered the horizon"
    is the finding.
    """

    rules = {
        "any actionable minute": lambda r: True,
        "volume printed this minute": lambda r: r["trailing"][0] > 0,
        "volume in the last 5 min": lambda r: sum(r["trailing"]) > 0,
        "heavy tape (>=2000 in 5 min)": lambda r: sum(r["trailing"]) >= 2000,
        "open interest flat (<1%/5min)":
            lambda r: r["oi_growth"] is not None and r["oi_growth"] < 0.01,
    }
    out = []

    for name, test in rules.items():
        cells = defaultdict(lambda: [0.0, 0, 0])
        late = total = 0

        for r in rows:
            if not test(r):
                continue

            s = cells[r["cluster"]]
            s[0] += r["pnl"]
            s[1] += 1
            s[2] += r["lost"]
            total += 1
            late += 1 if r["age"] <= 40 else 0

        row = stat(cells)

        if row.get("n", 0) < 200:
            continue

        row["rule"] = name
        row["share_last_40min"] = round(late / max(total, 1), 4)
        out.append(row)

    return out


def tiers_and_stops(rows, records) -> tuple[list[dict], list[dict]]:
    by_tier = defaultdict(lambda: [0.0, 0, 0])
    entries = []

    for d in records:
        if d.get("result") not in ("yes", "no"):
            continue

        if family_of(d.get("series", "")) != "tennis":
            continue

        candles = d.get("candles") or []
        book = book_at(candles, d["close_ts"] - 600)

        if not book or zone_of((book[0] + book[1]) / 2) != "fave BUY":
            continue

        won = 1 if d["result"] == "yes" else 0
        tier = TIERS.get(d.get("series", ""), "Tour / other")
        slot = by_tier[tier]
        slot[0] += won - book[1] - fee(book[1])
        slot[1] += 1
        slot[2] += 1 - won
        entries.append((d, book[1], won, candles))

    tier_rows = []

    for name, v in sorted(by_tier.items()):
        tier_rows.append({"tier": name, "markets": v[1], "losses": int(v[2]),
                          "loss_rate": round(v[2] / v[1], 5),
                          "ev_cents": round(v[0] / v[1] * 100, 3)})

    stops = []

    for level in (None, 0.80, 0.70, 0.60, 0.50):
        pnl, stopped, regret = [], 0, 0

        for d, ask, won, candles in entries:
            entry = -ask - fee(ask)
            exited = False

            if level is not None:
                for off in range(540, -1, -60):
                    book = book_at(candles, d["close_ts"] - off,
                                   max_staleness=120)

                    if book and book[0] < level:
                        pnl.append(entry + book[0] - fee(book[0]))
                        stopped += 1
                        regret += won
                        exited = True
                        break

            if not exited:
                pnl.append(entry + won)

        losing = [x for x in pnl if x < 0]
        stops.append({
            "stop": "none" if level is None else f"{level * 100:.0f}c",
            "ev_cents": round(sum(pnl) / len(pnl) * 100, 3),
            "stopped_out": stopped,
            "stopped_that_would_have_won": regret,
            "worst_cents": round(min(pnl) * 100, 1),
            "mean_loss_cents": round(sum(losing) / len(losing) * 100, 1)
            if losing else None,
        })

    return tier_rows, stops


def lookahead(book_path: Path | None, settled: Path | None) -> dict:
    """Prove the entry clock is unknowable, from recorded rows.

    `close_time_live` is what the API showed while the market was TRADEABLE.
    The settled corpus shows what the same field becomes afterwards.
    """

    out: dict = {}

    if book_path and book_path.exists():
        leads, examples = [], []

        for line in book_path.open():
            try:
                r = json.loads(line)
            except ValueError:
                continue

            if r.get("kind") != "touch" or not r.get("close_time_live"):
                continue

            try:
                close = datetime.fromisoformat(
                    r["close_time_live"].replace("Z", "+00:00")).timestamp()
            except (AttributeError, ValueError):
                continue

            lead = (close - r["ts"]) / 86400
            leads.append(lead)

            if len(examples) < 3 and lead > 1:
                examples.append({"ticker": r["ticker"],
                                 "close_time_live": r["close_time_live"],
                                 "observed_utc": datetime.fromtimestamp(
                                     r["ts"], tz=timezone.utc).isoformat(
                                         timespec="seconds")})

        if leads:
            leads.sort()
            out["live"] = {
                "observations": len(leads),
                "median_days_ahead": round(statistics.median(leads), 2),
                "min_days_ahead": round(leads[0], 2),
                "max_days_ahead": round(leads[-1], 2),
                "examples": examples,
            }

    if settled and settled.exists():
        spans = []

        with opener(settled) as handle:
            for line in handle:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue

                if not d.get("ticker", "").startswith(
                        ("KXITFMATCH", "KXITFWMATCH", "KXATPCHALLENGERMATCH")):
                    continue

                try:
                    a = datetime.fromisoformat(
                        d["open_time"].replace("Z", "+00:00"))
                    b = datetime.fromisoformat(
                        d["close_time"].replace("Z", "+00:00"))
                except (KeyError, AttributeError, ValueError):
                    continue

                spans.append((b - a).total_seconds() / 3600)

                if len(spans) >= 20000:
                    break

        if spans:
            spans.sort()
            out["settled"] = {
                "markets": len(spans),
                "median_hours_open_to_close": round(
                    statistics.median(spans), 1),
            }

    return out


def depth(book_path: Path | None) -> dict:
    """What actually rests at the touch, from the live recorder."""

    if not book_path or not book_path.exists():
        return {}

    per_series = defaultdict(list)
    touch = []
    markets = set()

    for line in book_path.open():
        try:
            r = json.loads(line)
        except ValueError:
            continue

        if r.get("kind") != "book":
            continue

        markets.add(r["ticker"])
        per_series[r["series"]].append((r["ask_size"], r["ask_depth_1c"]))

        if r.get("ask", 0) >= 0.80:
            touch.append(r["ask_size"])

    rows = []

    for series, vals in sorted(per_series.items(),
                               key=lambda kv: -len(kv[1])):
        sizes = sorted(v[0] for v in vals)
        within = sorted(v[1] for v in vals)
        rows.append({
            "series": series,
            "snapshots": len(vals),
            "median_touch": round(statistics.median(sizes)),
            "median_within_1c": round(statistics.median(within)),
            "p10_touch": round(sizes[int(len(sizes) * 0.1)]),
        })

    out = {"snapshots": sum(len(v) for v in per_series.values()),
           "markets": len(markets), "by_series": rows}

    if touch:
        touch.sort()
        out["fave_zone_touch"] = {
            "snapshots": len(touch),
            "p10": round(touch[int(len(touch) * 0.1)]),
            "median": round(statistics.median(touch)),
            "p90": round(touch[int(len(touch) * 0.9)]),
        }

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candles", nargs="+", required=True)
    parser.add_argument("--settled", type=Path, default=None)
    parser.add_argument("--book", type=Path, default=None)
    args = parser.parse_args()

    paths: list[Path] = []

    for pattern in args.candles:
        paths.extend(Path(p) for p in sorted(glob.glob(
            str(Path(pattern).expanduser()))))

    if not paths:
        raise SystemExit("no candle files matched")

    records, stats = load_records(paths)
    print(f"{stats['rows']:,} rows, {stats['duplicates']:,} duplicates dropped, "
          f"{len(records):,} unique markets", flush=True)

    study = entry_study(records)
    rows = study.pop("rows")
    tier_rows, stops = tiers_and_stops(rows, records)
    book = args.book.expanduser() if args.book else None
    settled = args.settled.expanduser() if args.settled else None

    payload = {
        "headline_t10": study["headline_t10"],
        "horizon_curve": study["horizon_curve"],
        "price_horizon_grid": study["price_horizon_grid"],
        "implementable": implementable(rows),
        "tiers": tier_rows,
        "stops": stops,
        "lookahead": lookahead(book, settled),
        "depth": depth(book),
        "provenance": {
            "candle_files": [str(p) for p in paths],
            "unique_markets": len(records),
            "duplicate_tickers_dropped": stats["duplicates"],
            "book_file": str(book) if book else None,
            "settled_file": str(settled) if settled else None,
        },
        "generated_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
