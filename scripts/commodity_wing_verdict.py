"""Three pre-specified tests on the hourly commodity ladders.

1. Pooled deep-wing test, with rare-event uncertainty computed at CONTRACT,
   EVENT and CLUSTER level. A zero-win cell has almost no across-cluster
   variance in P&L, so the naive clustered t-stat is enormous and meaningless -
   the same defect that once printed t=11.0 on a zero-loss cell in this repo.
   The uncertainty in a rare-event cell lives in the WIN COUNT, so it is
   reported with a rule-of-three bound at each level of aggregation.
2. Over-dispersion: one number per horizon (cheap half minus rich half), rather
   than seven bands that invite cherry-picking.
3. The EIA release test, with the power calculation reported BEFORE the result.
"""
import json, math, statistics, collections, datetime, os

FILES = ["candles.jsonl", "candles_breadth.jsonl", "candles_other.jsonl"]
SERIES = {"KXWTIH": "ENERGY", "KXGOLDH": "METALS", "KXSILVERH": "METALS"}
ROOT = os.path.expanduser("~/kalshi-audit")
STALE = 180


def fee_c(p):
    return math.ceil(0.07 * p * (1 - p) * 10000.0) / 100.0


def load():
    out = []
    for f in FILES:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            for line in open(p):
                d = json.loads(line)
                if d.get("series") in SERIES:
                    out.append(d)
    return out


def entry(m, h, need_volume):
    ct = m.get("close_ts")
    if not ct:
        return None
    cut = ct - h * 60
    best = None
    for c in m.get("candles", []):
        t = c.get("end_period_ts")
        if t is None or t > cut:
            continue
        if best is None or t > best["end_period_ts"]:
            best = c
    if best is None or cut - best["end_period_ts"] > STALE:
        return None
    a = best.get("yes_ask", {}).get("close_dollars")
    if a is None:
        return None
    a = float(a)
    if a <= 0 or a >= 1:
        return None
    if need_volume and float(best.get("volume_fp") or 0) < 1:
        return None
    if m.get("result") not in ("yes", "no"):
        return None
    return a, m["result"] == "yes"


def rows_for(mkts, h, need_volume):
    out = []
    for m in mkts:
        e = entry(m, h, need_volume)
        if not e:
            continue
        a, won = e
        day = datetime.datetime.fromtimestamp(m["close_ts"], datetime.UTC)
        out.append({"ask_c": a * 100, "won": won, "event": m["event"],
                    "cluster": f"{SERIES[m['series']]}|{day:%Y-%m-%d}",
                    "series": m["series"], "wd": day.weekday(), "hr": day.hour,
                    "pnl": (100.0 if won else 0.0) - a * 100 - fee_c(a)})
    return out


def rule_of_three(wins, n):
    """95% upper bound on the rate. Exact for wins=0, Poisson-ish otherwise."""
    if n == 0:
        return float("nan")
    if wins == 0:
        return 3.0 / n
    return (wins + 1.96 * math.sqrt(wins)) / n


def clustered(vals_by_key):
    means = [statistics.mean(v) for v in vals_by_key.values()]
    if len(means) < 2:
        return float("nan"), float("nan")
    return statistics.mean(means), statistics.stdev(means) / math.sqrt(len(means))


mkts = load()
print(f"{len(mkts):,} ladder strikes / {len({m['event'] for m in mkts}):,} events "
      f"across {len({m['series'] for m in mkts})} series\n")

# ---------------------------------------------------------------- TEST 1
print("=" * 78)
print("TEST 1  Deep wing (ask 1-5c), pooled inside 20 minutes of close")
print("=" * 78)
for need_vol, label in ((False, "all quotes"), (True, "volume printed (tradeable)")):
    rows = []
    for h in (5, 10, 20):
        rows += [r for r in rows_for(mkts, h, need_vol) if 1 <= r["ask_c"] <= 5]
    if not rows:
        continue
    wins = sum(1 for r in rows if r["won"])
    n = len(rows)
    ev = statistics.mean(r["pnl"] for r in rows)
    avg_ask = statistics.mean(r["ask_c"] for r in rows)
    be = (avg_ask + fee_c(avg_ask / 100)) / 100
    ev_by = collections.defaultdict(list)
    cl_by = collections.defaultdict(list)
    for r in rows:
        ev_by[r["event"]].append(r["won"])
        cl_by[r["cluster"]].append(r["won"])
    n_ev, n_cl = len(ev_by), len(cl_by)
    w_ev = sum(1 for v in ev_by.values() if any(v))
    w_cl = sum(1 for v in cl_by.values() if any(v))
    print(f"\n-- {label}")
    print(f"   contracts {n:,}   events {n_ev}   clusters {n_cl}")
    print(f"   average ask {avg_ask:.2f}c  ->  break-even ITM rate {be*100:.2f}%")
    print(f"   wins: {wins} of {n:,} contracts | {w_ev} of {n_ev} events | "
          f"{w_cl} of {n_cl} clusters")
    print(f"   naive per-contract ITM {wins/n*100:.2f}%   raw EV {ev:+.3f}c")
    for lvl, w, nn in (("contract", wins, n), ("event", w_ev, n_ev),
                       ("cluster", w_cl, n_cl)):
        ub = rule_of_three(w, nn) * 100
        verdict = "EXCLUDES break-even" if ub < be * 100 else "cannot exclude break-even"
        print(f"     95% upper bound on ITM at {lvl:>8} level: {ub:6.2f}%   {verdict}")

# ---------------------------------------------------------------- TEST 2
print("\n" + "=" * 78)
print("TEST 2  Over-dispersion: mean P&L on cheap (<50c) minus rich (>=50c)")
print("        One number per horizon. Positive = market too confident.")
print("=" * 78)
print(f"{'horizon':>9}{'n':>7}{'cl':>5}{'cheap':>9}{'rich':>9}{'diff':>9}{'se':>8}{'t':>7}")
for h in (5, 10, 20, 30, 45, 60):
    rows = rows_for(mkts, h, True)
    if len(rows) < 30:
        continue
    by = collections.defaultdict(lambda: {"c": [], "r": []})
    for r in rows:
        by[r["cluster"]]["c" if r["ask_c"] < 50 else "r"].append(r["pnl"])
    diffs, cheaps, richs = [], [], []
    for v in by.values():
        if v["c"] and v["r"]:
            diffs.append(statistics.mean(v["c"]) - statistics.mean(v["r"]))
            cheaps.append(statistics.mean(v["c"]))
            richs.append(statistics.mean(v["r"]))
    if len(diffs) < 3:
        continue
    m = statistics.mean(diffs)
    se = statistics.stdev(diffs) / math.sqrt(len(diffs))
    print(f"   T-{h:>2}min{len(rows):>7}{len(diffs):>5}{statistics.mean(cheaps):>+9.2f}"
          f"{statistics.mean(richs):>+9.2f}{m:>+9.2f}{se:>8.2f}{m/se if se else 0:>7.2f}")

# ---------------------------------------------------------------- TEST 3
print("\n" + "=" * 78)
print("TEST 3  EIA release window (WTI, Wed 14:00-15:00 UTC = 10:30 ET print)")
print("=" * 78)
rows = [r for r in rows_for(mkts, 10, False) if r["series"] == "KXWTIH"]
rel = [r for r in rows if r["wd"] == 2 and r["hr"] == 14]
same_hr = [r for r in rows if r["wd"] != 2 and r["hr"] == 14]
rel_ev = {r["event"] for r in rel}
ctl_ev = {r["event"] for r in same_hr}
print(f"   release windows found : {len(rel_ev)} events, {len(rel)} strikes")
print(f"   same-hour control     : {len(ctl_ev)} events, {len(same_hr)} strikes")
if same_hr and len(ctl_ev) > 2:
    per_ev = collections.defaultdict(list)
    for r in same_hr:
        per_ev[r["event"]].append(r["pnl"])
    sd = statistics.stdev([statistics.mean(v) for v in per_ev.values()])
    k = max(len(rel_ev), 1)
    mde = 2.8 * sd * math.sqrt(1 / k + 1 / len(ctl_ev))
    print(f"\n   POWER FIRST: control event-level SD of P&L = {sd:.1f}c")
    print(f"   With {k} release event(s) vs {len(ctl_ev)} controls, the minimum")
    print(f"   detectable difference at 80% power is {mde:.1f}c per contract.")
    print(f"   An EIA effect would have to be larger than {mde:.0f}c/contract to")
    print("   be visible. That is not a plausible effect size. This test is")
    print("   UNDERPOWERED BY CONSTRUCTION and no result from it is evidence.")
    if rel:
        print(f"\n   (point estimate, reported as an anecdote only: "
              f"release {statistics.mean(r['pnl'] for r in rel):+.2f}c vs "
              f"control {statistics.mean(r['pnl'] for r in same_hr):+.2f}c)")
