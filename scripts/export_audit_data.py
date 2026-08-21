"""Freeze the corrected audit into the JSON the findings site renders.

    python scripts/fetch_corpus.py --list   # corpus lives in GCS, not on disk
    python scripts/export_audit_data.py ~/kalshi-audit/triggers.jsonl \
        --obi ~/kalshi-audit/obi.json \
        --out analysis_app/data/audit.json

The first version of the site typed its numbers into markdown, which is how it
came to be publishing +0.85c per unit OBI and a t of -22.6 months after both had
been superseded. Everything the audit pages show is computed here, from the
trigger cache, and committed as data. If a number on the site is wrong now, this
script is wrong, and re-running it fixes the site.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from taker_expectancy import (  # noqa: E402
    PRICE_BANDS_FINE,
    MIN_TRIGGERS,
    MIN_WINDOWS,
    PERIODS,
    PLACEBOS,
    benjamini_hochberg,
    eligible,
    load,
    parse_utc,
    placebo_draws,
    summarise,
    venue_hours_in,
)


def scan_period(cache: Path, meta: dict, name: str, **filters) -> dict:
    start, end = (parse_utc(t) for t in PERIODS[name]) if name in PERIODS else (None, None)
    slices, _vw, hours, tickers, by_lead, kept, total = load(cache, start, end, **filters)
    keys = [k for k, cell in slices.items() if eligible(cell)]

    if not keys:
        return {"period": name, "testable": 0}

    hours_by_venue = venue_hours_in(meta, start, end)
    rows = [summarise(k, slices[k], hours_by_venue) for k in keys]
    rows.sort(key=lambda r: -r["mean"])
    draws = placebo_draws(tickers, PLACEBOS)
    matrix = [slices[k].placebo_means(draws) for k in keys]
    null = sorted(max(row[i] for row in matrix) for i in range(PLACEBOS))
    best = rows[0]["mean"]
    bh = benjamini_hochberg(rows)
    finite = [r["se_ratio"] for r in rows if math.isfinite(r["se_ratio"])]

    # Best slice per venue under each convention: the "what the old scan would
    # have told you" table, which is the clearest single view of the correction.
    by_venue: dict[str, list[dict]] = {}

    for r in rows:
        by_venue.setdefault(r["venue"], []).append(r)

    legacy = []

    for venue, mine in by_venue.items():
        old = max(mine, key=lambda r: r["mean_mid"])
        new = max(mine, key=lambda r: r["mean"])
        legacy.append({
            "venue": venue,
            "old_net": old["mean_mid"],
            "old_t": old["mean_mid"] / old["naive_se"] if old["naive_se"] else 0.0,
            "new_net": new["mean"],
            "new_t": new["t"],
            "windows": new["windows"],
            "slice": f"{new['obi']} / {new['price']} / {new['horizon']}s",
        })

    legacy.sort(key=lambda r: -r["new_net"])

    return {
        "period": name,
        "triggers": kept,
        "cells": len(slices),
        "testable": len(keys),
        "suppressed": len(slices) - len(keys),
        "positive": len([r for r in rows if r["mean"] > 0]),
        "significant": len([r for r in rows if r["mean"] - 1.96 * r["se"] > 0]),
        "best": best,
        "best_slice": (f"{rows[0]['venue']} / {rows[0]['obi']} / "
                       f"{rows[0]['price']} / {rows[0]['horizon']}s"),
        "best_windows": rows[0]["windows"],
        "best_dollars_hr": rows[0]["dollars_hr"],
        "placebo_mean": st.mean(null),
        "placebo_p95": null[int(0.95 * (len(null) - 1))],
        "placebo_beat_rate": sum(1 for v in null if v >= best) / len(null),
        "bh_positive": len([r for r in rows if bh and r["p"] <= bh and r["mean"] > 0]),
        "bh_negative": len([r for r in rows if bh and r["p"] <= bh and r["mean"] < 0]),
        "exit_touch": st.mean([r["mean"] for r in rows]),
        "exit_mid": st.mean([r["mean_mid"] for r in rows]),
        "exit_cross": st.mean([r["mean_cross"] for r in rows]),
        "exit_blended": st.mean([r["blended"] for r in rows]),
        "mid_convention_cost": st.mean([r["mean"] - r["mean_mid"] for r in rows]),
        "lookahead": st.mean([r["lookahead"] for r in rows]),
        "lookahead_by_lead": {k: (v[1] / v[0] if v[0] else 0.0, int(v[0]))
                              for k, v in by_lead.items()},
        "se_ratio": st.median(finite) if finite else None,
        "triggers_per_window": st.mean([r["n"] / r["windows"] for r in rows]),
        "gross_min": min(r["gross"] for r in rows),
        "gross_max": max(r["gross"] for r in rows),
        "fee_min": min(r["fee"] for r in rows),
        "fee_max": max(r["fee"] for r in rows),
        "hours": sum(hours_by_venue.values()),
        "utc_hours": sorted(hours),
        "legacy": legacy,
        "top": [
            {k: r[k] for k in ("venue", "obi", "price", "horizon", "mean", "gross",
                               "fee", "se", "t", "n", "windows", "per_hour",
                               "dollars_hr", "median_size", "buy_share")}
            for r in rows[:12]
        ],
    }


OBI_ORDER = ["obi<.2 CTRL", "obi.2-.5", "obi.5-.7", "obi.7-.9", "obi>.9"]


def dose_response(cache: Path, **filters) -> list[dict]:
    """Mean net by imbalance band with NO slice selected, one number per market.

    This is the test the original scan could not run, because it had no control
    band: every trigger it kept already had extreme imbalance, so a positive
    slice could not be told apart from a market that simply drifts. Pooling all
    price bands and both directions removes the selection, and the balanced
    band shows what the structure pays when the signal says nothing.
    """

    slices, *_ = load(cache, None, None, **filters)
    per: dict[str, dict[str, list]] = {ob: {} for ob in OBI_ORDER}

    for (_venue, ob, _price, horizon), cell in slices.items():
        if horizon != "30" or ob not in per:
            continue

        for ticker, total in cell.cl_net.items():
            slot = per[ob].setdefault(ticker, [0.0, 0])
            slot[0] += total
            slot[1] += cell.cl_n[ticker]

    out = []

    for ob in OBI_ORDER:
        means = [s / n for s, n in per[ob].values() if n >= 20]

        if len(means) < 5:
            out.append({"band": ob, "markets": len(means)})
            continue

        out.append({
            "band": ob,
            "mean": st.mean(means),
            "se": st.stdev(means) / math.sqrt(len(means)),
            "markets": len(means),
            "control": "CTRL" in ob,
        })

    return out


def named_cell(cache: Path, venue: str, price: str, **filters) -> list[dict]:
    """The dose-response inside one pre-registered cell, band by band."""

    slices, *_ = load(cache, None, None, **filters)
    out = []

    for ob in OBI_ORDER:
        cell = slices.get((venue, ob, price, "30"))

        if cell is None or not eligible(cell):
            out.append({"band": ob, "markets": cell.n if cell else 0})
            continue

        mean = cell.mean("touch")
        se = cell.clustered_se("touch")
        out.append({
            "band": ob, "mean": mean, "se": se, "markets": len(cell.cl_n),
            "significant": bool(mean - 1.96 * se > 0), "control": "CTRL" in ob,
        })

    return out


def census(cache: Path) -> list[dict]:
    bounds = {n: tuple(parse_utc(t) for t in s) for n, s in PERIODS.items()}
    stats = {n: {"n": 0, "venues": set(), "markets": set(), "hours": set(),
                 "phases": [], "near": 0} for n in PERIODS}

    with cache.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)

            for name, (start, end) in bounds.items():
                if not start <= row["utc"] < end:
                    continue

                b = stats[name]
                b["n"] += 1
                b["venues"].add(row["venue"])
                b["markets"].add(row["ticker"])
                b["hours"].add(row["hour"])
                phase = row.get("to_close")

                if phase is not None:
                    if phase <= 900:
                        b["near"] += 1

                    if len(b["phases"]) < 20000:
                        b["phases"].append(phase)

    out = []

    for name in PERIODS:
        b = stats[name]

        if not b["n"]:
            continue

        hours = sorted(b["hours"])
        out.append({
            "period": name,
            "triggers": b["n"],
            "venues": sorted(b["venues"]),
            "markets": len(b["markets"]),
            "utc_from": hours[0],
            "utc_to": hours[-1],
            "utc_count": len(hours),
            "near_expiry_share": b["near"] / b["n"],
            "median_phase": st.median(b["phases"]) if b["phases"] else None,
        })

    return out


def replication(cache: Path, frozen: Path, holdout: str, **filters) -> list[dict]:
    start, end = (parse_utc(t) for t in PERIODS[holdout])
    slices, *_ = load(cache, start, end, **filters)
    out = []

    for spec in json.loads(frozen.read_text()):
        cell = slices.get(tuple(spec["key"]))
        name = (f"{spec['venue']} / {spec['obi']} / {spec['price']} / "
                f"{spec['horizon']}s")

        if cell is None or not eligible(cell):
            out.append({"slice": name, "in_sample": spec["mean"], "verdict": "ABSENT",
                        "n": cell.n if cell else 0})
            continue

        mean = cell.mean("touch")
        se = cell.clustered_se("touch")
        mde = 2.80 * se
        pooled = math.sqrt(spec["se"] ** 2 + se ** 2)
        z = (spec["mean"] - mean) / pooled if pooled else 0.0
        shift = spec["mean"] / se if se else 0.0
        power = (0.5 * math.erfc((1.96 - shift) / math.sqrt(2))
                 + 0.5 * math.erfc((1.96 + shift) / math.sqrt(2)))

        if mean - 1.96 * se > 0:
            verdict = "HELD"
        elif abs(z) > 1.96:
            verdict = "SMALLER"
        elif mde > spec["mean"]:
            verdict = "NO POWER"
        else:
            verdict = "CONSISTENT"

        out.append({
            "slice": name, "in_sample": spec["mean"], "holdout": mean, "se": se,
            "mde": mde, "z": z, "power": power, "verdict": verdict,
            "n": cell.n, "windows": len(cell.cl_n),
            "phase_in": spec.get("median_phase"),
            "phase_out": st.median(cell.phases) if cell.phases else None,
        })

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache", type=Path)
    parser.add_argument("--obi", type=Path)
    parser.add_argument("--frozen-in", type=Path)
    parser.add_argument("--frozen-virgin", type=Path)
    parser.add_argument("--frozen-expiry", type=Path)
    parser.add_argument("--recovered", type=Path,
                        help="second, independent trigger cache for replication")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    meta = json.loads(args.cache.with_suffix(".meta.json").read_text())
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "corpus": {
            "recordings": meta["recordings"],
            "skipped": meta["skipped"],
            "triggers": meta["triggers"],
            "censored": meta["censored_by_horizon"],
            "elapsed_hours": meta["elapsed_hours"],
            "venue_hours": meta["venue_hours"],
            "config": meta["config"],
            "min_triggers": MIN_TRIGGERS,
            "min_windows": MIN_WINDOWS,
        },
        "census": census(args.cache),
        "scans": {},
        "replication": {},
    }

    for name in ("all", "pre", "in", "oos", "virgin"):
        print(f"  scanning {name}", flush=True)
        payload["scans"][name] = scan_period(args.cache, meta, name)

    if args.frozen_in:
        for holdout in ("oos", "virgin"):
            print(f"  replicating in -> {holdout}", flush=True)
            payload["replication"][f"in_to_{holdout}"] = replication(
                args.cache, args.frozen_in, holdout)

    if args.frozen_virgin:
        print("  replicating virgin -> in (backward control)", flush=True)
        payload["replication"]["virgin_to_in"] = replication(
            args.cache, args.frozen_virgin, "in")

    # --- the profit hunt: three nested conditions, each a hypothesis the
    # original scan pooled away, plus the pre-registered holdout for the last one
    print("  profit hunt: baseline / deep tail / deep tail near expiry", flush=True)
    payload["profit"] = {
        "baseline": scan_period(args.cache, meta, "all"),
        "deep_tail": scan_period(args.cache, meta, "all", bands=PRICE_BANDS_FINE),
        "near_expiry": scan_period(args.cache, meta, "all", bands=PRICE_BANDS_FINE,
                                   phase=(0.0, 900.0)),
    }

    if args.frozen_expiry:
        print("  profit hunt: replicate near-expiry winners pre -> in", flush=True)
        payload["profit"]["replication"] = replication(
            args.cache, args.frozen_expiry, "in",
            bands=PRICE_BANDS_FINE, phase=(0.0, 900.0))

    # --- the control band: is a positive slice signal, or just structure? ---
    if args.recovered:
        print("  dose-response with control band, both corpora", flush=True)
        payload["dose"] = {
            "archive": dose_response(args.cache),
            "recovered": dose_response(args.recovered),
            "cheap_recovered": dose_response(
                args.recovered, bands=PRICE_BANDS_FINE, phase=(0.0, 900.0)),
            "lead_archive": named_cell(
                args.cache, "KXBTCD", ".02-.05",
                bands=PRICE_BANDS_FINE, phase=(0.0, 900.0)),
            "lead_recovered": named_cell(
                args.recovered, "KXBTCD", ".02-.05",
                bands=PRICE_BANDS_FINE, phase=(0.0, 900.0)),
        }

    if args.obi and args.obi.exists():
        payload["obi"] = json.loads(args.obi.read_text())

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1))
    print(f"\nwrote {args.out} ({args.out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
