"""Do the wings of the hourly commodity ladders pay?

Pre-specified test. Buy YES at the displayed ask on KXWTIH / KXGOLDH /
KXSILVERH strikes, hold to settlement, at a range of horizons before close.

Design decisions that are NOT free parameters, all forced by prior errors in
this project:

* Entry reads the last candle at or BEFORE the horizon, never the first at or
  after it (that peeks at the move being predicted).
* A staleness limit, because a quarter of book reads in this corpus were >3min
  old and weather was 52%.
* Clustering is family-by-UTC-day. Six strikes of one ladder ride one oil
  print and are one draw, not six. Being wrong about this once turned t=2.5
  into a published t=44.
* Bands are cut on the OBSERVED ask, and EV is reported against the average
  observed ask, not the bucket centre.
* Every band is reported, both wings and the middle, so no single cell can be
  quoted alone. The curve is run over a range of horizons, with the overlap
  between horizons reported, because a curve whose horizons see different
  markets is a cast change and says nothing.
* Fees round UP to $0.0001, not to the next cent. Measured against 48 real
  taker fills: the cent ceiling overstates by 48% at one contract.
* Settlement pays no fee.
"""
import json, math, statistics, collections, datetime, argparse, os, sys

FILES = ["candles.jsonl", "candles_breadth.jsonl", "candles_other.jsonl"]
SERIES = {"KXWTIH": "ENERGY", "KXGOLDH": "METALS", "KXSILVERH": "METALS"}
FEE_RATE = 0.07
STALE_LIMIT_S = 180
HORIZONS = [5, 10, 20, 30, 45, 60]
BANDS = [(1, 5), (6, 15), (16, 40), (41, 59), (60, 84), (85, 94), (95, 99)]


def fee_cents(price_dollars: float) -> float:
    """Kalshi trading fee for one contract, in cents. Rounds up to $0.0001."""
    raw = FEE_RATE * price_dollars * (1.0 - price_dollars)
    return math.ceil(raw * 10000.0) / 100.0


def load(root):
    out = []
    for f in FILES:
        p = os.path.join(root, f)
        if not os.path.exists(p):
            continue
        for line in open(p):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("series") in SERIES:
                out.append(d)
    return out


def entries_at(mkt, horizon_min):
    """Last actionable candle at or before close - horizon. None if stale/absent."""
    close_ts = mkt.get("close_ts")
    if not close_ts:
        return None
    cutoff = close_ts - horizon_min * 60
    best = None
    for c in mkt.get("candles", []):
        ts = c.get("end_period_ts")
        if ts is None or ts > cutoff:
            continue
        if best is None or ts > best.get("end_period_ts", -1):
            best = c
    if best is None:
        return None
    if cutoff - best["end_period_ts"] > STALE_LIMIT_S:
        return None
    ask = best.get("yes_ask", {}).get("close_dollars")
    if ask is None:
        return None
    ask = float(ask)
    if ask <= 0.0 or ask >= 1.0:
        return None
    res = mkt.get("result")
    if res not in ("yes", "no"):
        return None
    return {
        "ask": ask,
        "won": res == "yes",
        "ts": best["end_period_ts"],
        "vol": float(best.get("volume_fp") or 0.0),
        "oi": float(best.get("open_interest_fp") or 0.0),
    }


def cluster_stats(rows):
    """Mean and cluster-robust SE. Cluster = family x UTC day."""
    if not rows:
        return None
    by = collections.defaultdict(list)
    for r in rows:
        by[r["cluster"]].append(r["pnl"])
    means = [statistics.mean(v) for v in by.values()]
    n_cl = len(means)
    mean = statistics.mean(means)
    if n_cl < 2:
        return {"n": len(rows), "clusters": n_cl, "mean": mean, "se": float("nan"),
                "t": float("nan")}
    se = statistics.stdev(means) / math.sqrt(n_cl)
    return {"n": len(rows), "clusters": n_cl, "mean": mean, "se": se,
            "t": mean / se if se > 0 else float("nan")}


def loss_events(rows):
    """Count losing CLUSTERS, not losing contracts (rule of three, in money)."""
    by = collections.defaultdict(list)
    for r in rows:
        by[r["cluster"]].append(r["won"])
    return sum(1 for v in by.values() if not all(v)), len(by)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/kalshi-audit"))
    ap.add_argument("--min-volume", type=float, default=0.0)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    mkts = load(args.root)
    print(f"loaded {len(mkts):,} ladder strikes across "
          f"{len({m['event'] for m in mkts}):,} events\n")

    report = {"horizons": {}, "overlap": {}, "meta": {
        "stale_limit_s": STALE_LIMIT_S, "min_volume": args.min_volume}}
    seen_by_h = {}

    for h in HORIZONS:
        rows = []
        for m in mkts:
            e = entries_at(m, h)
            if e is None or e["vol"] < args.min_volume:
                continue
            day = datetime.datetime.fromtimestamp(
                m["close_ts"], datetime.UTC).strftime("%Y-%m-%d")
            ask_c = e["ask"] * 100.0
            pnl = (100.0 if e["won"] else 0.0) - ask_c - fee_cents(e["ask"])
            rows.append({
                "series": m["series"], "event": m["event"], "ticker": m["ticker"],
                "cluster": f"{SERIES[m['series']]}|{day}", "ask_c": ask_c,
                "won": e["won"], "pnl": pnl,
            })
        seen_by_h[h] = {r["ticker"] for r in rows}
        band_out = []
        for lo, hi in BANDS:
            sub = [r for r in rows if lo <= r["ask_c"] <= hi]
            st = cluster_stats(sub)
            if not st:
                band_out.append({"band": f"{lo}-{hi}c", "n": 0})
                continue
            wins = sum(1 for r in sub if r["won"])
            avg_ask = statistics.mean(r["ask_c"] for r in sub)
            lcl, ncl = loss_events(sub)
            breakeven = avg_ask + fee_cents(avg_ask / 100.0)
            band_out.append({
                "band": f"{lo}-{hi}c", "n": st["n"], "clusters": st["clusters"],
                "avg_ask_c": round(avg_ask, 2),
                "itm_rate": round(wins / st["n"], 4),
                "breakeven_itm": round(breakeven / 100.0, 4),
                "ev_c": round(st["mean"], 3),
                "se_c": round(st["se"], 3) if st["se"] == st["se"] else None,
                "t": round(st["t"], 2) if st["t"] == st["t"] else None,
                "loss_clusters": f"{lcl}/{ncl}",
            })
        allst = cluster_stats(rows)
        report["horizons"][h] = {"bands": band_out, "all": {
            "n": allst["n"], "clusters": allst["clusters"],
            "ev_c": round(allst["mean"], 3), "se_c": round(allst["se"], 3),
            "t": round(allst["t"], 2)}}

        print(f"===== horizon T-{h}min   ({allst['n']:,} entries, "
              f"{allst['clusters']} clusters)")
        print(f"{'ask band':>10}{'n':>7}{'cl':>5}{'avg ask':>9}{'ITM':>8}"
              f"{'b/e ITM':>9}{'EV(c)':>9}{'se':>7}{'t':>7}  loss-clusters")
        for b in band_out:
            if not b.get("n"):
                print(f"{b['band']:>10}{0:>7}")
                continue
            print(f"{b['band']:>10}{b['n']:>7}{b['clusters']:>5}"
                  f"{b['avg_ask_c']:>8.1f}c{b['itm_rate']*100:>7.2f}%"
                  f"{b['breakeven_itm']*100:>8.2f}%{b['ev_c']:>+9.3f}"
                  f"{b['se_c'] if b['se_c'] is not None else float('nan'):>7.3f}"
                  f"{b['t'] if b['t'] is not None else float('nan'):>7.2f}"
                  f"  {b['loss_clusters']}")
        print()

    longest = max(HORIZONS)
    print("===== horizon overlap (Jaccard of tickers vs T-%dmin)" % longest)
    for h in HORIZONS:
        a, b = seen_by_h[h], seen_by_h[longest]
        j = len(a & b) / len(a | b) if (a | b) else 0.0
        report["overlap"][h] = round(j, 3)
        print(f"  T-{h:>2}min  overlap {j:5.1%}   ({len(a):,} tickers)")

    if args.json_out:
        json.dump(report, open(args.json_out, "w"), indent=1)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
