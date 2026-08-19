"""Does any slice of the OBI signal clear TAKER costs? The corrected scan.

    python scripts/taker_expectancy.py triggers.jsonl --period in
    python scripts/taker_expectancy.py triggers.jsonl --period in --freeze winners.json
    python scripts/taker_expectancy.py triggers.jsonl --period virgin --replicate winners.json

Reads the trigger cache built by `taker_extract.py`. The original version of this
file walked the books and reported a mean and a naive t per slice, and wrote its
negative conclusion into its own docstring before the data was seen. What it
could support was narrower than what it concluded. The changes:

**The exit is a bracket, not a hidden assumption.** The old scan marked the exit
at the MID while claiming a resting maker exit. A resting exit fills at the
TOUCH. On books filtered to <=2c that gap is 0.5-1.0c per trade, larger than
every effect the scan reported. Three conventions are priced side by side:
`touch` (the resting exit fills), `cross` (it never fills, pay a second fee), and
`mid` (the old number). The truth is between touch and cross at the venue's
measured cross rate, which is the `blended` column.

**The standard error is clustered on the window.** Every trigger inside one
15-minute market is the same price path. Treating 473 triggers from four windows
as 473 independent draws understates the SE by roughly the square root of the
triggers per window. Clustering on the ticker absorbs that and the overlap
between horizons together, so no separate overlap fudge is needed. The naive SE
is printed beside it as a ratio, because that ratio is most of the old result.

**Failure to replicate is evidence only if the test had power.** Replication mode
reports the minimum detectable effect at the holdout's own sample size and
refuses to call a slice refuted when the holdout could not have seen the
in-sample effect. It then runs the positive control: inject the in-sample effect
into the holdout and confirm the test fires.

**The search is calibrated by placebo, not by counting slices.** Flipping the
direction of whole windows at random preserves every trade, every cost and all
the volatility, and destroys only the signal. The distribution of the best slice
under that null is the bar a winner must clear. Counting "482 slices" overstated
the family, because the three horizons share one trigger set.

Gross, fee and net are reported separately. The old scan printed only net, so it
could not distinguish "the signal is absent" from "the signal is real and the fee
eats it" - which is the distinction its conclusion rested on.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ONE_DOLLAR = 10_000
TICKS_PER_CENT = ONE_DOLLAR // 100
PRICE_BANDS = (("tail<.15", 0.0, 0.15), (".15-.35", 0.15, 0.35),
               ("mid.35-.65", 0.35, 0.65), (".65-.85", 0.65, 0.85),
               ("tail>.85", 0.85, 1.01))
EXITS = ("touch", "mid", "cross")
MIN_TRIGGERS = 30
MIN_WINDOWS = 3          # a slice living in one or two windows is one price path
PLACEBOS = 400
RESERVOIR = 2000
# Fraction of exits the live ledger could not rest and had to cross, per venue.
CROSS_RATE = {"KXBTC15M": 0.09, "KXETH15M": 0.05}
DEFAULT_CROSS_RATE = 0.04

PERIODS = {
    "pre": ("2026-08-16T00:00:00Z", "2026-08-18T00:00:00Z"),
    "in": ("2026-08-18T00:00:00Z", "2026-08-19T00:00:00Z"),
    "oos": ("2026-08-19T00:00:00Z", "2026-08-19T07:44:00Z"),
    "virgin": ("2026-08-19T07:44:00Z", "2026-08-20T00:00:00Z"),
}


def parse_utc(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def fee_cents(price_ticks: float) -> float:
    p = price_ticks / ONE_DOLLAR
    return 7.0 * p * (1.0 - p)


def band_of(bands, value: float) -> str | None:
    for label, lo, hi in bands:
        if lo <= value < hi:
            return label

    return None


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


class Slice:
    """Running statistics for one (venue, obi, price, horizon) cell.

    Everything is kept per cluster, where a cluster is one market ticker - one
    15-minute window, one price path. That is the independent unit for both the
    standard error and the placebo, and it is what the old scan did not have.
    """

    def __init__(self) -> None:
        self.n = 0
        self.sums: dict[str, float] = dict.fromkeys(EXITS, 0.0)
        self.sqs: dict[str, float] = dict.fromkeys(EXITS, 0.0)
        self.cl_n: dict[str, int] = defaultdict(int)
        self.cl_net: dict[str, float] = defaultdict(float)
        self.cl_gross: dict[str, float] = defaultdict(float)
        self.cl_fee: dict[str, float] = defaultdict(float)
        self.gross = 0.0
        self.fees = 0.0
        self.lookahead = 0.0
        self.buys = 0
        self.sizes: list[float] = []
        self.phases: list[float] = []

    def add(self, nets, gross, fee, lookahead, ticker, size, phase, buy) -> None:
        self.n += 1

        for name, value in nets.items():
            self.sums[name] += value
            self.sqs[name] += value * value

        self.cl_n[ticker] += 1
        self.cl_net[ticker] += nets["touch"]
        self.cl_gross[ticker] += gross
        self.cl_fee[ticker] += fee
        self.gross += gross
        self.fees += fee
        self.lookahead += lookahead
        self.buys += 1 if buy else 0

        if len(self.sizes) < RESERVOIR:
            self.sizes.append(size)

        if phase is not None and len(self.phases) < RESERVOIR:
            self.phases.append(phase)

    def mean(self, name: str = "touch") -> float:
        return self.sums[name] / self.n if self.n else 0.0

    def naive_se(self, name: str = "touch") -> float:
        if self.n < 2:
            return float("inf")

        mean = self.mean(name)
        var = max(self.sqs[name] / self.n - mean * mean, 0.0) * self.n / (self.n - 1)
        return math.sqrt(var / self.n)

    def clustered_se(self, name: str = "touch") -> float:
        """SE of the mean with observations clustered on the market ticker.

        Every trigger inside one window rides one price path, so the independent
        unit is the window, not the trigger. This single correction decides
        whether any of the old t-statistics survive.
        """

        groups = len(self.cl_n)

        if groups < 2 or self.n < 2 or name != "touch":
            if name != "touch":
                return self.naive_se(name)

            return float("inf")

        mean = self.mean(name)
        total = sum(
            (self.cl_net[ticker] - count * mean) ** 2
            for ticker, count in self.cl_n.items()
        )
        return math.sqrt(groups / (groups - 1) * total) / self.n

    def placebo_means(self, draws: list[dict[str, int]]) -> list[float]:
        """Slice mean under each whole-window sign flip.

        Flipping at the window level rather than the trigger level preserves the
        within-window correlation that the clustered SE exists to respect, and it
        is the standard randomisation test for clustered data.
        """

        fee_total = sum(self.cl_fee.values())
        out = []

        for signs in draws:
            total = 0.0

            for ticker, gross in self.cl_gross.items():
                total += signs.get(ticker, 1) * gross

            out.append((total - fee_total) / self.n)

        return out


def load(path: Path, start: float | None, end: float | None,
         min_size: float = 0.0, phase: tuple[float, float] | None = None):
    """Stream the cache into per-slice accumulators. Flat memory in the corpus.

    `min_size` is a floor on the contracts resting on the side being crossed. The
    old scan had none, so a 1-lot versus 19-lot touch scored OBI 0.9 on pure
    microstructure noise, and the extreme band filled up with books nobody could
    trade. `phase` restricts to seconds-to-close, because live markout decays
    monotonically across a window and pooling the whole window averages the good
    regime into the bad one.
    """

    slices: dict[tuple, Slice] = defaultdict(Slice)
    venue_windows: dict[str, set[str]] = defaultdict(set)
    hours: dict[int, int] = defaultdict(int)
    tickers: set[str] = set()
    # Lookahead bias should grow with how far past the horizon the next update
    # sat, because on a quiet book that next update tends to BE the move.
    lead_buckets = ((0.5, "<0.5s"), (2.0, "0.5-2s"), (10.0, "2-10s"),
                    (math.inf, ">10s"))
    by_lead: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    kept = 0
    total = 0

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            total += 1
            row = json.loads(line)
            utc = row["utc"]

            if start is not None and utc < start:
                continue

            if end is not None and utc >= end:
                continue

            if min_size and row["crossable"] < min_size:
                continue

            if phase is not None:
                to_close = row.get("to_close")

                if to_close is None or not phase[0] <= to_close < phase[1]:
                    continue

            kept += 1
            hours[row["hour"]] += 1
            ticker = row["ticker"]
            tickers.add(ticker)
            venue_windows[row["venue"]].add(ticker)
            buy = row["side"] == "buy"
            sign = 1.0 if buy else -1.0
            entry = row["entry"]
            # Long-equivalent price: a short of YES at 0.90 is a long of NO at
            # 0.10 and carries the identical fee. Banding on the raw entry put
            # those two opposite positions in one bucket, where any
            # price-conditional structure cancels in the mean.
            long_equiv = entry if buy else ONE_DOLLAR - entry
            price_band = band_of(PRICE_BANDS, long_equiv / ONE_DOLLAR)

            if price_band is None:
                continue

            entry_fee = fee_cents(entry)

            for horizon, book in row["h"].items():
                exit_touch = book["ask"] if buy else book["bid"]
                exit_cross = book["bid"] if buy else book["ask"]
                gross_touch = sign * (exit_touch - entry) / TICKS_PER_CENT
                nets = {
                    "touch": gross_touch - entry_fee,
                    "mid": sign * (book["mid"] - entry) / TICKS_PER_CENT - entry_fee,
                    "cross": (sign * (exit_cross - entry) / TICKS_PER_CENT
                              - entry_fee - fee_cents(exit_cross)),
                }
                # What the old first-update-after-the-horizon convention added to
                # gross. Positive means the lookahead flattered the signal.
                lookahead = 0.0

                if book.get("mid_after") is not None:
                    lookahead = sign * (book["mid_after"] - book["mid"]) / TICKS_PER_CENT
                    lead = book.get("lead") or 0.0

                    for edge, name in lead_buckets:
                        if lead < edge:
                            by_lead[name][0] += 1
                            by_lead[name][1] += lookahead
                            break

                slices[(row["venue"], row["obi_band"], price_band, horizon)].add(
                    nets=nets,
                    gross=gross_touch,
                    fee=entry_fee,
                    lookahead=lookahead,
                    ticker=ticker,
                    size=row["crossable"],
                    phase=row.get("to_close"),
                    buy=buy,
                )

    return slices, venue_windows, hours, sorted(tickers), by_lead, kept, total


def eligible(cell: Slice) -> bool:
    return cell.n >= MIN_TRIGGERS and len(cell.cl_n) >= MIN_WINDOWS


def summarise(key, cell: Slice, hours_by_venue: dict[str, float]) -> dict:
    venue = key[0]
    mean = cell.mean("touch")
    se = cell.clustered_se("touch")
    naive = cell.naive_se("touch")
    t = mean / se if se and math.isfinite(se) else 0.0
    rate = CROSS_RATE.get(venue, DEFAULT_CROSS_RATE)
    venue_hours = hours_by_venue.get(venue, 0.0)
    per_hour = cell.n / venue_hours if venue_hours else 0.0
    median_size = st.median(cell.sizes) if cell.sizes else 0.0

    return {
        "key": key,
        "venue": venue,
        "obi": key[1],
        "price": key[2],
        "horizon": key[3],
        "n": cell.n,
        "windows": len(cell.cl_n),
        "mean": mean,
        "mean_mid": cell.mean("mid"),
        "mean_cross": cell.mean("cross"),
        "blended": (1 - rate) * mean + rate * cell.mean("cross"),
        "gross": cell.gross / cell.n,
        "fee": cell.fees / cell.n,
        "lookahead": cell.lookahead / cell.n,
        "se": se,
        "naive_se": naive,
        "se_ratio": se / naive if naive and math.isfinite(se) else float("inf"),
        "t": t,
        "p": 2 * norm_sf(abs(t)) if math.isfinite(t) else 1.0,
        "per_hour": per_hour,
        "median_size": median_size,
        # Cents per trade -> dollars per hour at the size actually on the touch.
        "dollars_hr": mean / 100.0 * min(median_size, 50.0) * per_hour,
        "buy_share": cell.buys / cell.n,
        "median_phase": st.median(cell.phases) if cell.phases else None,
    }


def census(path: Path) -> None:
    """What each period actually contains, side by side.

    The chapter's load-bearing sentence is "three independent periods, three
    disjoint winner lists". Three periods are only three tests of one hypothesis
    if they sample the same thing. This prints what they sample: which venues,
    how many distinct markets, which hours of the day, and how far into a window.
    """

    stats: dict[str, dict] = {
        name: {"n": 0, "venues": set(), "markets": set(), "hours": set(),
               "phases": [], "short": 0}
        for name in PERIODS
    }
    bounds = {name: tuple(parse_utc(t) for t in span) for name, span in PERIODS.items()}

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)

            for name, (start, end) in bounds.items():
                if not start <= row["utc"] < end:
                    continue

                bucket = stats[name]
                bucket["n"] += 1
                bucket["venues"].add(row["venue"])
                bucket["markets"].add(row["ticker"])
                bucket["hours"].add(row["hour"])
                phase = row.get("to_close")

                if phase is not None:
                    if phase <= 900:
                        bucket["short"] += 1

                    if len(bucket["phases"]) < 20000:
                        bucket["phases"].append(phase)

    print(f"\n{'=' * 80}\nPERIOD CENSUS - are these periods testing the same thing?\n")
    # "near expiry" = triggers inside the last 15 minutes of their OWN market's
    # life, whatever its total length. Not the KX*15M series.
    print(f"{'period':<9}{'triggers':>10}{'venues':>8}{'markets':>9}{'UTC hours':>26}"
          f"{'<15m left':>11}{'med phase':>11}")

    for name in PERIODS:
        bucket = stats[name]

        if not bucket["n"]:
            print(f"{name:<9}{'(empty)':>10}")
            continue

        hours = sorted(bucket["hours"])
        span = f"{hours[0]:02d}-{hours[-1]:02d} ({len(hours)}h)"
        short = bucket["short"] / bucket["n"]
        phase = st.median(bucket["phases"]) if bucket["phases"] else float("nan")
        print(f"{name:<9}{bucket['n']:>10}{len(bucket['venues']):>8}"
              f"{len(bucket['markets']):>9}{span:>26}{short:>10.0%}{phase:>10.0f}s")

    print("\nvenue overlap between periods (a slice can only be replicated where its "
          "venue exists):")
    names = [n for n in PERIODS if stats[n]["n"]]

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = stats[a]["venues"] & stats[b]["venues"]
            print(f"  {a:>7} n {b:<7} {len(shared):>3} shared venue(s)"
                  f"{'  <- no common ground' if not shared else ''}")


def coverage_seconds(intervals) -> float:
    if not intervals:
        return 0.0

    ordered = sorted(intervals)
    merged_start, merged_end = ordered[0]
    total = 0.0

    for start, end in ordered[1:]:
        if start > merged_end:
            total += merged_end - merged_start
            merged_start, merged_end = start, end
        else:
            merged_end = max(merged_end, end)

    return total + merged_end - merged_start


def venue_hours_in(meta: dict, start: float | None, end: float | None) -> dict[str, float]:
    """Real elapsed coverage per venue INSIDE the period, as an interval union.

    Dividing a period's triggers by the whole corpus's hours is not a rate. The
    old scan divided by a sum of per-ticker spans, which for a laddered series
    like KXBTCD counted the same wall-clock minute once per strike.
    """

    lo = start if start is not None else -math.inf
    hi = end if end is not None else math.inf
    clipped: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for venue, span_start, span_end in meta.get("intervals", []):
        a, b = max(span_start, lo), min(span_end, hi)

        if b > a:
            clipped[venue].append((a, b))

    return {venue: coverage_seconds(spans) / 3600.0 for venue, spans in clipped.items()}


def benjamini_hochberg(rows: list[dict], q: float = 0.10) -> float:
    """Largest p-value surviving BH at level q. 0.0 if none do."""

    ordered = sorted(r["p"] for r in rows)
    threshold = 0.0

    for rank, p in enumerate(ordered, 1):
        if p <= q * rank / len(ordered):
            threshold = p

    return threshold


def placebo_draws(tickers: list[str], count: int) -> list[dict[str, int]]:
    rng = random.Random(20260819)
    return [
        {ticker: rng.choice((-1, 1)) for ticker in tickers}
        for _ in range(count)
    ]


def report(slices, hours_by_venue, hours, tickers, by_lead, kept, total, label) -> list[dict]:
    keys = [k for k, cell in slices.items() if eligible(cell)]
    rows = [summarise(k, slices[k], hours_by_venue) for k in keys]
    rows.sort(key=lambda r: -r["mean"])
    suppressed = len(slices) - len(keys)

    print(f"\n{'=' * 80}")
    print(f"{label.upper()}: {kept} of {total} cached triggers, {len(slices)} cells, "
          f"{len(keys)} testable (n>={MIN_TRIGGERS}, >={MIN_WINDOWS} windows)")
    print(f"{suppressed} cells suppressed as underpowered. The old scan dropped these "
          f"silently,\nwhich is one way a slice 'fails to replicate': by vanishing "
          f"rather than by being refuted.")

    if not rows:
        print("nothing testable in this period.")
        return rows

    span = sorted(hours)
    covered = ", ".join(f"{h:02d}" for h in span)
    print(f"UTC hours covered: {covered}")

    print(f"\n{'net/trade':>10}{'gross':>8}{'fee':>7}{'SE':>7}{'t':>6}"
          f"{'n':>7}{'win':>5}{'/hr':>7}{'$/hr':>8}  venue / obi / price / horizon")

    for r in rows[:20]:
        print(f"{r['mean']:>+9.3f}c{r['gross']:>+7.3f}{r['fee']:>7.3f}"
              f"{r['se']:>6.3f}{r['t']:>+6.1f}{r['n']:>7}{r['windows']:>5}"
              f"{r['per_hour']:>7.1f}{r['dollars_hr']:>+8.2f}  "
              f"{r['venue']} / {r['obi']} / {r['price']} / {r['horizon']}s")

    positive = [r for r in rows if r["mean"] > 0]
    significant = [r for r in rows if r["mean"] - 1.96 * r["se"] > 0]
    print(f"\n{len(positive)} of {len(rows)} testable slices positive net of costs; "
          f"{len(significant)} significant before any multiplicity correction.")

    mean_touch = st.mean([r["mean"] for r in rows])
    mean_mid = st.mean([r["mean_mid"] for r in rows])
    mean_cross = st.mean([r["mean_cross"] for r in rows])
    print(f"\nEXIT BRACKET   the old scan marked the exit at the mid while assuming a "
          f"rested maker exit.")
    print(f"  rested at touch : {len(positive):>3} positive   mean {mean_touch:+.3f}c")
    print(f"  marked at mid   : {len([r for r in rows if r['mean_mid'] > 0]):>3} positive"
          f"   mean {mean_mid:+.3f}c   <- the old number")
    print(f"  forced to cross : {len([r for r in rows if r['mean_cross'] > 0]):>3} positive"
          f"   mean {mean_cross:+.3f}c")
    print(f"  the mid convention cost {mean_touch - mean_mid:+.3f}c per trade against a "
          f"rested exit.")
    # The honest central estimate: mostly rested, occasionally forced to cross,
    # at the rate the live ledger actually measured per venue.
    blended = st.mean([r["blended"] for r in rows])
    print(f"  blended at the ledger's measured cross rates: {blended:+.3f}c "
          f"({len([r for r in rows if r['blended'] > 0])} positive)")

    look = st.mean([r["lookahead"] for r in rows])
    print(f"\nLOOKAHEAD      the old scan read the book at the FIRST update at or AFTER "
          f"the horizon\n  instead of the last one at or before it, peeking at the very "
          f"move being predicted.\n  That added {look:+.3f}c per trade to gross "
          f"({'flattering' if look > 0 else 'penalising'} the signal), by how far past "
          f"the\n  horizon that next update sat:")

    for name in ("<0.5s", "0.5-2s", "2-10s", ">10s"):
        count, total_bias = by_lead.get(name, (0.0, 0.0))

        if count:
            print(f"    next update {name:>7}: {count:>8.0f} obs   "
                  f"{total_bias / count:+.3f}c")

    finite = [r["se_ratio"] for r in rows if math.isfinite(r["se_ratio"])]
    ratio = st.median(finite) if finite else float("nan")
    trig_per_window = st.mean([r["n"] / r["windows"] for r in rows])
    print(f"\nCLUSTERING     median clustered SE is {ratio:.1f}x the naive SE the old "
          f"scan printed.\n  Slices average {trig_per_window:.0f} triggers from each "
          f"15-minute window, and a window is\n  one price path. Read every old "
          f"t-statistic as roughly t/{ratio:.1f}.")

    draws = placebo_draws(tickers, PLACEBOS)
    # One matrix pass: placebo_means is O(clusters) per slice, so compute each
    # slice's row once and take the max down each column.
    matrix = [slices[k].placebo_means(draws) for k in keys]
    null = sorted(max(row[i] for row in matrix) for i in range(PLACEBOS)) if matrix else []

    if null:
        bar95 = null[int(0.95 * (len(null) - 1))]
        best = rows[0]["mean"]
        beaten = sum(1 for value in null if value >= best) / len(null)
        bh = benjamini_hochberg(rows)
        print(f"\nSEARCH         {len(keys)} testable slices. Under {PLACEBOS} "
              f"whole-window sign flips (same trades,\n  same costs, direction "
              f"randomised) the best slice averages {st.mean(null):+.3f}c and reaches "
              f"{bar95:+.3f}c\n  at the 95th percentile. Observed best {best:+.3f}c is "
              f"beaten by placebo {beaten * 100:.1f}% of the time.")
        survivors = [r for r in rows if bh and r["p"] <= bh]
        # Direction matters. A two-sided p-value is just as small for a slice
        # that loses money reliably, and a reliable loser is not a finding about
        # the signal - it is the fee showing through a gross of zero.
        print(f"  BH-FDR q=0.10: {len([r for r in survivors if r['mean'] > 0])} "
              f"positive survivor(s), {len([r for r in survivors if r['mean'] < 0])} "
              f"reliably negative")

    # --- what the old scan would have printed, beside what is true ---
    print(f"\nLEGACY vs CORRECTED   best slice per venue under each set of "
          f"conventions.\n  'old' = exit marked at the mid, naive SE, no clustering - "
          f"the numbers in the chapter.\n  'new' = exit rested at the touch, SE "
          f"clustered on the window.")
    print(f"\n{'venue':<14}{'old net':>9}{'old t':>7}{'new net':>9}{'new t':>7}"
          f"{'win':>5}  best slice (corrected)")
    by_venue: dict[str, list[dict]] = defaultdict(list)

    for r in rows:
        by_venue[r["venue"]].append(r)

    for venue in sorted(by_venue, key=lambda v: -max(x["mean"] for x in by_venue[v])):
        mine = by_venue[venue]
        old_best = max(mine, key=lambda r: r["mean_mid"])
        new_best = max(mine, key=lambda r: r["mean"])
        old_t = old_best["mean_mid"] / old_best["naive_se"] if old_best["naive_se"] else 0.0
        print(f"{venue:<14}{old_best['mean_mid']:>+8.3f}c{old_t:>+7.1f}"
              f"{new_best['mean']:>+8.3f}c{new_best['t']:>+7.1f}"
              f"{new_best['windows']:>5}  {new_best['obi']} / {new_best['price']} / "
              f"{new_best['horizon']}s")

    short = [r for r in rows if r["median_phase"] is not None
             and r["median_phase"] <= 900]
    print(f"\nWINDOW PHASE   {len(short)} of {len(rows)} slices sit inside a "
          f"15-minute window;")

    if short:
        print(f"  their median seconds-to-close is {st.median([r['median_phase'] for r in short]):.0f}s.")
        late = len([r for r in short if r["median_phase"] < 360])
        print(f"  {late} of those sample mostly the last six minutes, where live "
              f"markout is already gone\n  (+0.41c at 12-15m left decaying to -0.07c "
              f"at 1-2m). Depth collapses ~11x late, which\n  mechanically inflates OBI, "
              f"so the extreme band over-samples the worst regime.")
    else:
        print("  the rest are longer-dated ladders, where 'window phase' does not apply "
              "and the\n  15-minute markout decay curve cannot be carried over.")

    return rows


def freeze(rows: list[dict], path: Path, top: int) -> None:
    """Pre-register the best slices for a holdout to judge.

    Rows are already sorted by mean. Taking the top N regardless of sign keeps
    the replication test informative when the corrected scan yields almost no
    positive slices: the chapter's claim was that the WINNER LIST does not
    repeat, and testing rank stability needs a list longer than one.
    """

    winners = rows[:top]
    payload = [
        {
            "key": list(r["key"]), "venue": r["venue"], "obi": r["obi"],
            "price": r["price"], "horizon": r["horizon"], "mean": r["mean"],
            "se": r["se"], "n": r["n"], "windows": r["windows"],
            "median_phase": r["median_phase"],
        }
        for r in winners
    ]
    path.write_text(json.dumps(payload, indent=2))
    print(f"\nfroze {len(payload)} candidate slices -> {path}")


def replicate(frozen_path: Path, slices, label: str) -> None:
    """Evaluate pre-registered slices on a holdout, with power stated first."""

    frozen = json.loads(frozen_path.read_text())
    print(f"\n{'=' * 80}")
    print(f"REPLICATION of {len(frozen)} frozen slices on period '{label}'")
    print("A slice is REFUTED only if this holdout could have detected the in-sample")
    print("effect. MDE is the smallest effect the holdout has 80% power to see.\n")
    print(f"{'in-sample':>10}{'holdout':>10}{'SE':>7}{'MDE':>8}{'conf?':>6}{'n':>7}"
          f"{'win':>5}{'z-diff':>8}  verdict     slice")
    verdicts: dict[str, int] = defaultdict(int)

    for spec in frozen:
        key = tuple(spec["key"])
        cell = slices.get(key)
        name = f"{spec['venue']} / {spec['obi']} / {spec['price']} / {spec['horizon']}s"

        if cell is None or not eligible(cell):
            n = cell.n if cell else 0
            windows = len(cell.cl_n) if cell else 0
            print(f"{spec['mean']:>+9.3f}c{'-':>10}{'-':>7}{'-':>8}{'-':>6}{n:>7}"
                  f"{windows:>5}{'-':>8}  ABSENT      {name}")
            verdicts["absent"] += 1
            continue

        mean = cell.mean("touch")
        se = cell.clustered_se("touch")
        mde = 2.80 * se           # 80% power, two-sided 5%: 1.96 + 0.84 SEs
        pooled = math.sqrt(spec["se"] ** 2 + se ** 2)
        z_diff = (spec["mean"] - mean) / pooled if pooled else 0.0
        # A slice label pins venue, OBI band, price band and horizon. It does not
        # pin where in the window the triggers sat, and markout decays across a
        # window, so a large phase shift means the label is sampling a different
        # regime under the same name.
        here = st.median(cell.phases) if cell.phases else None
        there = spec.get("median_phase")
        drift = (f"  phase {there:.0f}s->{here:.0f}s"
                 if here is not None and there is not None
                 and abs(here - there) > 120 else "")

        # Two questions, deliberately not collapsed into one label. Can this
        # holdout CONFIRM the effect (is the effect above its detection floor)?
        # And does it actually REJECT the in-sample value? A holdout can fail the
        # first and pass the second, and reporting only one is how "no slice
        # replicated" got written down as "no edge".
        can_confirm = "Y" if mde <= spec["mean"] else "n"

        if mean - 1.96 * se > 0:
            verdict = "HELD"
        elif abs(z_diff) > 1.96:
            verdict = "SMALLER"      # significantly below the in-sample value
        elif mde > spec["mean"]:
            verdict = "NO POWER"     # cannot confirm, cannot reject: silent
        else:
            verdict = "CONSISTENT"

        verdicts[verdict] += 1
        print(f"{spec['mean']:>+9.3f}c{mean:>+9.3f}c{se:>7.3f}{mde:>8.3f}"
              f"{can_confirm:>6}{cell.n:>7}{len(cell.cl_n):>5}{z_diff:>+8.2f}  "
              f"{verdict:<11} {name}{drift}")

    print(f"\n{dict(verdicts)}")

    if verdicts["NO POWER"]:
        print(f"\n{verdicts['NO POWER']} slice(s) could not have detected their own "
              f"in-sample effect here.\nCalling those 'failed to replicate' reads "
              f"absence of power as evidence of absence.")

    print("\nPOWER   if the in-sample effect were exactly true, how often would this")
    print("holdout detect it at 5% two-sided? Below ~80% a null result is weak")
    print("evidence; near 50% it is a coin toss dressed as a refutation.\n")
    weak = 0
    tested = 0

    for spec in frozen:
        cell = slices.get(tuple(spec["key"]))
        name = f"{spec['venue']} / {spec['obi']} / {spec['price']} / {spec['horizon']}s"

        if cell is None or not eligible(cell):
            print(f"  {'n/a':>6}   (slice absent from this holdout)   {name}")
            continue

        tested += 1
        se = cell.clustered_se("touch")

        if not math.isfinite(se) or se == 0:
            continue

        # P(reject H0 | true effect = spec["mean"]), two-sided at 5%.
        shift = spec["mean"] / se
        power = norm_sf(1.96 - shift) + norm_sf(1.96 + shift)

        if power < 0.80:
            weak += 1

        print(f"  {power:>5.0%}   detection floor {2.80 * se:+.3f}c vs effect "
              f"{spec['mean']:+.3f}c   {name}")

    if weak:
        print(f"\n  {weak} of {tested} slices are underpowered here. For those, a null "
              f"holdout result is\n  not evidence against the hypothesis, and the "
              f"original chapter counted it as such.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache", type=Path)
    parser.add_argument("--period", default="all",
                        help=f"one of {list(PERIODS)} or 'all'")
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--replicate", type=Path)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--census", action="store_true",
                        help="compare what each period actually contains")
    parser.add_argument("--min-size", type=float, default=0.0,
                        help="contracts required on the side being crossed")
    parser.add_argument("--phase", nargs=2, type=float, metavar=("LO", "HI"),
                        help="seconds-to-close window, e.g. --phase 360 900 for the "
                             "first half of a 15-minute market")
    args = parser.parse_args()

    if args.census:
        census(args.cache)
        return

    meta_path = args.cache.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    if args.period == "all":
        start = end = None
    elif args.period in PERIODS:
        start, end = (parse_utc(t) for t in PERIODS[args.period])
    else:
        sys.exit(f"unknown period {args.period}")

    slices, _venue_windows, hours, tickers, by_lead, kept, total = load(
        args.cache, start, end, args.min_size,
        tuple(args.phase) if args.phase else None,
    )

    if not kept:
        sys.exit(f"no triggers in period {args.period}")

    if args.replicate:
        replicate(args.replicate, slices, args.period)
        return

    hours_by_venue = venue_hours_in(meta, start, end)
    rows = report(slices, hours_by_venue, hours, tickers, by_lead, kept, total,
                  args.period)

    if args.freeze and rows:
        freeze(rows, args.freeze, args.top)


if __name__ == "__main__":
    main()
