"""Which real fills lost money, and how much would each gate level have saved?

    python scripts/fetch_corpus.py --list   # corpus lives in GCS, not on disk
    python scripts/gate_dose_study.py ~/kalshi-audit/journals2 \\
        ~/kalshi-audit/recs2 ~/kalshi-audit/recs

The live A/B tests one gate threshold at ~4 cycles an hour. The journaled fills
let every threshold be scored at once: for each fill, look up the order-book
imbalance the collector recorded near it, ask "would a gate at level t have
blocked this fill?", and compare the 30s markout of blocked versus kept. Real
fills carry real adverse selection, so this sidesteps the fill-model problem -
nothing is simulated except the veto.

This is observational, not a randomised experiment, and the earlier version's
"treatment group" framing oversold it. Nothing was assigned. The same fills are
re-scored under different vetoes, and the fills that a veto would have blocked
differ from the rest in venue, price, and time of day as well as in imbalance.

That is the idea. Everything below is the reason the first version of this
script could not support it, and what it now does instead.

## The join, and why it is the whole problem

**markout** comes from the journal's mid timeline, and which timeline turns out
to be most of the story. `record_mid` writes a fixed-cadence snapshot, median
1.0s apart, precisely because `placed` mids are event-driven: a fill triggers a
re-quote, the re-quote writes a `placed` mid milliseconds later, so a
placed-derived timeline is densest right after the event being measured.

`record_mid` was added partway through the August run. Fifteen of the 21
journals - 1,273 of 2,507 fills - contain **zero** `mid` events, and in the six
that do, only 11-29% of the filled tickers are covered. So the earlier version's
"self-book markout" was, for the large majority of its sample, computed from the
contaminated timeline it believed it had stopped using.

Deleting those fills would leave nothing, so both timelines are built and every
fill is tagged with the one that produced its markout. Where both exist the two
are computed side by side and the paired difference is reported, which measures
the clustering bias in cents instead of arguing about it. Every table is
stratified by source. A gradient that appears only in placed-sourced fills is an
artifact of the timeline, not a property of the market.

A markout sample must also be no more than MARKOUT_STALE_TOLERANCE seconds
before the horizon AND strictly after the fill, so a sparse ticker cannot
silently return a pre-fill mid as a "30s markout".

**The self-book markout has no passing t=0 control, and never has.** An earlier
draft of this file claimed the opposite. The notes are unambiguous: the mid
moves about 1.6c across a single fill (pre-fill sample +1.34c, post-fill -0.24c,
the journal's own stamp +0.37c between them), which is wider than the entire
markout curve this study is trying to resolve. Nothing below can be read as
established at a resolution finer than that.

**OBI** comes from the collector recordings (the journal carries no sizes).
Separate connection, and - this is the part that decides whether any of this
means anything - **the journal's `at` field is when we WROTE the event, not when
the exchange executed it.** `mid_lag_seconds` exists to measure that gap and is
null on all 2,507 fills in the August corpus, because no caller ever passed
`executed_at` (now fixed in `trader.py`, which does not help data already
collected). The gap is therefore unknown, and the earlier version papered over
it with a hardcoded 0.25s lookback.

This matters because the artifact runs the same direction as the hypothesis.
When our resting sell is lifted, the ask side depletes or the level clears, so
the book is left bid-heavy - high "imbalance against the fill" is partly a
*consequence* of being filled, and the same aggression that caused it moves the
mid against us over the next 30 seconds. Sample the book a moment too late and
the study measures its own fills.

So the lookback is no longer a constant. `--offsets` sweeps the join across a
range that runs from clearly-before the fill to clearly-after it, and prints the
dose-response at each. Read it as a diagnostic, not a robustness check:

* clean-pre offsets (-30s, -10s) are stale but causally safe.
* the +5s offset is a deliberate placebo. It samples a book that certainly
  postdates the fill, so any "signal" there is pure contamination.
* if the association strengthens monotonically as the sample moves toward and
  past the fill, the effect is mechanical and the gate has nothing to gate.

## Confounders, which are the size of the claimed effect

Venue and price are not controls, they are rival explanations. One venue is a
third of the sample at -1.354c while three others are positive, and one price
decile runs -5.179c against a -0.433c overall mean. Bucketing on imbalance
without conditioning on either lets a venue effect or a price effect print as a
dose-response. The tables below are therefore reported three ways: raw,
venue-demeaned, and venue-and-price-demeaned. Only the demeaned gradient is
evidence about imbalance.

Every mean carries a standard error clustered on the market ticker. Fills inside
one market ride one price path and their 30s markout windows overlap; treating
them as independent draws is what inflated this project's earlier t-statistics
by 1.6x and flipped its venue conclusions.

## Sign convention

`obi_against` is imbalance pointing AGAINST the passive fill - for our resting
sell being lifted, a bid-heavy book (price about to rise); for our resting buy
being hit, ask-heavy. The gate blocks a fill when obi_against exceeds its
threshold AND the fill would have opened or extended a position.

The two directions are reported separately and never pooled by default. The
audit found the underlying signal is one-sided (bid-heavy +0.68c over baseline,
ask-heavy +0.02c), so pooling them hides the possibility that the whole result
lives in the sell half and the gate's buy half is blocking at random.

## What this design still cannot see

The sweep now walks fills in time order and carries a *counterfactual* position,
so blocking one fill correctly changes whether the next one counts as
position-increasing. It still scores every kept fill at its factual markout, in
a world where the earlier vetoes never happened. And a 30s markout is the drift
leg of a maker's P&L, not the P&L: it excludes the spread captured on entry, the
eventual exit, the queue position lost to the cancel, and the inventory a veto
strands - which is the cost that turned the last version of this idea from
+$1.33 into -$14.78. Nothing here is a profit estimate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics as st
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from cluster_stats import clustered, clustered_diff, fmt  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402
from taker_extract import parse_utc, walk  # noqa: E402

TPC = ONE_DOLLAR // 100
HORIZON = 30.0
THRESHOLDS = (50, 60, 70, 80, 90, 95)
# How stale the OBI sample may be relative to the point we are aiming at.
OBI_SKEW_TOLERANCE = 2.0
# How stale the markout mid may be relative to fill+HORIZON. The `mid` timeline
# runs at a 1.0s median cadence, so 5s is generous and still rules out reading a
# mid from a minute earlier as if it were the 30s mark.
MARKOUT_STALE_TOLERANCE = 5.0
# Where to sample OBI, in seconds relative to the journal's fill stamp. Negative
# is before. +5.0 is the contamination placebo - see the module docstring.
DEFAULT_OFFSETS = (-30.0, -10.0, -2.0, -0.25, 5.0)
REPORT_OFFSET = -0.25       # the offset the detailed tables are built at
MIN_BUCKET = 15


def parse_iso(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------- loading


def _markout_at(series, fill, buying: bool, census, tag: str):
    """30s markout off one timeline, or None with the reason counted."""

    if not series:
        census[f"drop_{tag}_no_timeline"] += 1
        return None

    when = fill["at"] + HORIZON

    if when > series[-1][0]:
        census[f"drop_{tag}_horizon_past_end"] += 1
        return None

    index = bisect_right(series, (when, float("inf"))) - 1

    if index < 0:
        census[f"drop_{tag}_no_sample_before_horizon"] += 1
        return None

    at_mid, mid_value = series[index]

    # The sample must actually be near the horizon, and it must be after the
    # fill. Neither was checked before, so on a thin ticker a "30s markout"
    # could be measured against a book that predates the fill entirely.
    if when - at_mid > MARKOUT_STALE_TOLERANCE:
        census[f"drop_{tag}_sample_stale"] += 1
        return None

    if at_mid <= fill["at"]:
        census[f"drop_{tag}_sample_predates_fill"] += 1
        return None

    sign = 1.0 if buying else -1.0
    return sign * (mid_value - fill["yes_price"]) / TPC, when - at_mid


def load_journals(journal_dir: Path) -> tuple[list[dict], dict[str, int]]:
    """Fills with a self-book markout, plus a census of what was dropped.

    Every drop is counted and reported. The earlier version printed only the
    survivors, so "2,208 journaled fills" was really "fills that happened to
    still have a mid 30 seconds later" - a censoring step that preferentially
    removes the end of a market's life, which is exactly where adverse selection
    and the phased strategy's reduce-only behaviour live.
    """

    fills: list[dict] = []
    census: dict[str, int] = defaultdict(int)

    for path in sorted(journal_dir.glob("*.jsonl")):
        clean: dict[str, list[tuple[float, int]]] = defaultdict(list)
        full: dict[str, list[tuple[float, int]]] = defaultdict(list)
        raw_fills = []

        for line in path.read_text().splitlines():
            line = line.strip()

            if not line:
                continue

            try:
                event = json.loads(line)
            except ValueError:
                census["unparseable_lines"] += 1
                continue

            at = parse_iso(event.get("at"))

            if at is None:
                continue

            kind = event.get("event")

            if kind in ("mid", "placed") and event.get("mid") is not None:
                full[event["market_ticker"]].append((at, event["mid"]))

                if kind == "mid":
                    clean[event["market_ticker"]].append((at, event["mid"]))
            elif kind == "filled" and event.get("yes_price") is not None:
                raw_fills.append(event | {"at": at})

        for table in (clean, full):
            for series in table.values():
                series.sort()

        census["journals"] += 1

        if not clean:
            census["journals_with_no_mid_events"] += 1

        position: dict[str, int] = defaultdict(int)
        raw_fills.sort(key=lambda f: f["at"])

        for fill in raw_fills:
            census["journaled_fills"] += 1
            ticker = fill["market_ticker"]

            # `side` is None in journals written before it was recorded. Signing
            # off `action` alone is only correct for YES quotes: a buy of NO is
            # economically a short of YES and would invert both the markout sign
            # and the position update.
            side = fill.get("side")

            if side is not None and str(side) != "yes":
                census["dropped_non_yes_side"] += 1
                continue

            buying = fill.get("action") == "buy"
            count = int(fill.get("count") or COUNT_SCALE)
            before = position[ticker]

            # The exchange's own post-fill position beats re-deriving one from
            # zero at each file boundary, which any inherited inventory or any
            # missed fill silently corrupts. Only the sign matters here.
            post = fill.get("post_position")

            if post is not None:
                position[ticker] = int(post)
                before = position[ticker] - (count if buying else -count)
                census["position_from_exchange"] += 1
            else:
                position[ticker] += count if buying else -count
                census["position_derived"] += 1

            if fill.get("mid_lag_seconds") is not None:
                census["fills_with_known_lag"] += 1

            from_clean = _markout_at(
                clean.get(ticker), fill, buying, census, "clean")
            from_full = _markout_at(
                full.get(ticker), fill, buying, census, "full")

            if from_clean is not None:
                markout, lag = from_clean
                source = "mid"
            elif from_full is not None:
                markout, lag = from_full
                source = "placed"
            else:
                census["dropped_no_markout_either_timeline"] += 1
                continue

            fills.append({
                "ticker": ticker,
                "at": fill["at"],
                "buying": buying,
                "price": fill["yes_price"],
                "contracts": count / COUNT_SCALE,
                "markout": markout,
                "markout_lag": lag,
                "markout_source": source,
                "markout_clean": from_clean[0] if from_clean else None,
                "markout_full": from_full[0] if from_full else None,
                # The FACTUAL position before this fill. The sweep computes
                # its own counterfactual one, and keeping a same-named
                # convenience flag next to it is how the first version ended up
                # testing every threshold against a history where nothing was
                # ever blocked. This is only the anchor the counterfactual
                # starts from.
                "position_before": before,
                "price_decile": fill["yes_price"] // 1000,
                "taker": bool(fill.get("is_taker")),
                "journal": path.stem,
                "venue": ticker.split("-", 1)[0],
            })
            census[f"kept_from_{source}"] += 1

    return fills, dict(census)


async def load_obi(rec_dirs, tickers_needed):
    """Per-ticker [(abs_utc, obi)] from every recording that covers a fill."""

    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    skipped = 0

    for rec_dir in rec_dirs:
        recordings = sorted(
            p for p in Path(rec_dir).iterdir() if (p / "manifest.json").exists())
        print(f"  scanning {rec_dir}: {len(recordings)} recordings", flush=True)

        for index, rec in enumerate(recordings, 1):
            # Read the manifest before replaying the session. `walk` decodes the
            # whole websocket log, so replaying a recording that subscribes to
            # none of our tickers costs minutes and yields nothing.
            try:
                listed = json.loads((rec / "manifest.json").read_text())
                subscribed = set(listed.get("tickers") or ())
            except (OSError, ValueError):
                subscribed = set()

            if subscribed and not (subscribed & tickers_needed):
                skipped += 1
                continue

            try:
                samples, _span, manifest = await walk(rec)
            except Exception:  # noqa: BLE001
                continue

            if not any(t in tickers_needed for t in samples):
                continue

            started = parse_utc(manifest.started_at_utc).timestamp()

            for ticker, rows in samples.items():
                if ticker not in tickers_needed:
                    continue

                for row in rows:
                    series[ticker].append((started + row[0], row[2]))

            if index % 40 == 0:
                print(f"    {rec_dir}: {index}/{len(recordings)}", flush=True)

    print(f"  {skipped} recordings skipped by manifest (no overlapping ticker)",
          flush=True)

    for rows in series.values():
        rows.sort()

    return series


def obi_at(series, when: float) -> float | None:
    if not series:
        return None

    index = bisect_right(series, (when, float("inf"))) - 1

    if index < 0:
        return None

    at, obi = series[index]
    return obi if when - at <= OBI_SKEW_TOLERANCE else None


def join_obi(fills, obi_series, offset: float) -> list[dict]:
    """Attach `obi_against` sampled `offset` seconds from each fill stamp."""

    matched = []

    for fill in fills:
        obi = obi_at(obi_series.get(fill["ticker"]), fill["at"] + offset)

        if obi is None:
            continue

        row = dict(fill)
        # Imbalance pointing against the passive fill: our sell is picked off
        # by a bid-heavy book, our buy by an ask-heavy one.
        row["obi_against"] = obi if not fill["buying"] else -obi
        matched.append(row)

    return matched


# -------------------------------------------------------------------- tables


def demean(rows, keys) -> list[dict]:
    """Subtract each group's own mean markout, so a group effect cannot print
    as a dose-response. `keys` is a tuple of row fields to group on."""

    groups: dict[tuple, list[float]] = defaultdict(list)

    for row in rows:
        groups[tuple(row[k] for k in keys)].append(row["markout"])

    means = {key: st.mean(values) for key, values in groups.items()}
    out = []

    for row in rows:
        copy = dict(row)
        copy["markout"] = row["markout"] - means[tuple(row[k] for k in keys)]
        out.append(copy)

    return out


def obi_bucket(row) -> str:
    """Ordered label. The previous version built its sort prefix with
    `f"{1 - lo:.0f}"`, which rounds to "0" for every band from .50 up, so the
    table did not print in intensity order and three rows were dropped when it
    was transcribed."""

    value = row["obi_against"]
    bands = (
        (0.95, "6 against >= .95"),
        (0.90, "5 against .90-.95"),
        (0.70, "4 against .70-.90"),
        (0.50, "3 against .50-.70"),
        (0.20, "2 against .20-.50"),
    )

    for low, name in bands:
        if value >= low:
            return name

    if value <= -0.5:
        return "0 WITH the fill >= .5"

    return "1 balanced"


def bucket_table(rows, key, label, *, note: str = "") -> None:
    groups: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        groups[key(row)].append(row)

    print(f"\n{label}")

    if note:
        print(f"  {note}")

    print(f"{'bucket':<24}{'fills':>7}{'mkts':>6}{'mean markout':>20}"
          f"{'median':>10}{'% losing':>10}")
    small = 0

    for name in sorted(groups):
        batch = groups[name]

        # Nothing is dropped silently. Thin buckets print with a marker so a
        # reader can see the whole partition and its counts.
        marker = "" if len(batch) >= MIN_BUCKET else "  (thin)"

        if len(batch) < MIN_BUCKET:
            small += 1

        values = [r["markout"] for r in batch]
        n, groups_n, mean, se = clustered(values, [r["ticker"] for r in batch])
        losing = sum(1 for v in values if v < 0) / len(values)
        print(f"{name:<24}{n:>7}{groups_n:>6}{fmt(mean, se):>20}"
              f"{st.median(values):>+9.3f}c{losing:>10.0%}{marker}")

    if small:
        print(f"  ({small} bucket(s) under {MIN_BUCKET} fills, marked thin, "
              f"shown anyway)")


def dose_line(rows, level: float) -> str:
    """One-line summary of the gradient: extreme bucket versus the rest."""

    high = [r for r in rows if r["obi_against"] >= level]
    rest = [r for r in rows if r["obi_against"] < level]

    if not high or not rest:
        return "insufficient data"

    diff, se = clustered_diff(
        [r["markout"] for r in high], [r["ticker"] for r in high],
        [r["markout"] for r in rest], [r["ticker"] for r in rest])
    ratio = abs(diff / se) if se and not math.isnan(se) else float("nan")
    return (f"n={len(high):>4} vs {len(rest):>4}   "
            f"gap {diff:+.3f}c +/-{se:.3f}  t={ratio:>5.1f}")


# --------------------------------------------------------------------- sweep


def sweep(passive, thresholds) -> None:
    """Simulate the veto with a counterfactual position path.

    Fills are walked in time order per ticker carrying a position that reflects
    the vetoes already applied, so blocking one fill correctly changes whether
    the next counts as position-increasing. The earlier version tested every
    threshold against the factual position path, in which nothing was blocked.
    """

    print(f"\n{'=' * 86}")
    print(f"GATE SWEEP over {len(passive)} passive fills")
    print("Counterfactual position path: a blocked fill does not move inventory.")
    print(f"\n{'thresh':>7}{'blocked':>9}{'%':>5}"
          f"{'blocked markout':>22}{'kept markout':>22}"
          f"{'blocked - kept':>22}")

    journals = len({f["journal"] for f in passive})
    per_ticker: dict[str, list[dict]] = defaultdict(list)

    for fill in passive:
        per_ticker[fill["ticker"]].append(fill)

    for rows in per_ticker.values():
        rows.sort(key=lambda f: f["at"])

    drift_leg: dict[int, float] = {}

    for threshold in thresholds:
        level = threshold / 100.0
        blocked: list[dict] = []
        kept: list[dict] = []

        for rows in per_ticker.values():
            # Seed from the real pre-fill position of this ticker's first
            # matched fill. Starting every ticker at zero would assume the
            # matched subset is the whole trading history, and it is 34% of it.
            position = float(rows[0]["position_before"]) / COUNT_SCALE

            for fill in rows:
                increases = (
                    position >= 0 if fill["buying"] else position <= 0
                )

                if fill["obi_against"] >= level and increases:
                    blocked.append(fill)
                    continue

                kept.append(fill)
                position += (
                    fill["contracts"] if fill["buying"] else -fill["contracts"]
                )

        if len(blocked) < 10:
            print(f"{threshold:>6}%{len(blocked):>9}{'-':>5}{'-':>22}"
                  f"{'-':>22}{'-':>22}")
            continue

        b_n, _, b_mean, b_se = clustered(
            [f["markout"] for f in blocked], [f["ticker"] for f in blocked])
        k_n, _, k_mean, k_se = clustered(
            [f["markout"] for f in kept], [f["ticker"] for f in kept])
        diff, diff_se = clustered_diff(
            [f["markout"] for f in blocked], [f["ticker"] for f in blocked],
            [f["markout"] for f in kept], [f["ticker"] for f in kept])
        drift_leg[threshold] = -sum(
            f["markout"] * f["contracts"] for f in blocked) / max(journals, 1)
        print(f"{threshold:>6}%{b_n:>9}{b_n / len(passive):>5.0%}"
              f"{fmt(b_mean, b_se):>22}{fmt(k_mean, k_se):>22}"
              f"{fmt(diff, diff_se):>22}")

    print(f"\n{'thresh':>7}{'drift-leg cents per journal':>30}")

    for threshold, value in drift_leg.items():
        print(f"{threshold:>6}%{value:>29.2f}c")

    print(f"""
The last column is the only thing here that speaks to the gate's premise: is
what it blocks worse than what it keeps, by more than its own error bar?

'drift-leg cents per journal' is the blocked fills' 30s markout, size-weighted
by contracts and negated, over {journals} journals. It is NOT a P&L estimate and
must not be compared against a per-cycle profit figure. It omits the spread
captured on those fills, the exit that still has to happen, the queue position
lost when the veto cancels a resting order, and the inventory a veto strands.
The last of those is what turned this design's predecessor from +$1.33 to
-$14.78, and no offline replay of un-vetoed fills can see it.""")


# ---------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("journal_dir", type=Path)
    parser.add_argument("rec_dirs", type=Path, nargs="+")
    parser.add_argument("--offsets", type=float, nargs="+",
                        default=list(DEFAULT_OFFSETS),
                        help="seconds relative to the fill stamp to sample OBI")
    args = parser.parse_args()

    fills, census = load_journals(args.journal_dir)
    print(f"{len(fills)} fills with a clean 30s self-book markout\n")
    print("journal census (every drop counted):")

    for name in sorted(census):
        print(f"  {name:<38}{census[name]:>7}")

    by_source = defaultdict(int)

    for fill in fills:
        by_source[fill["markout_source"]] += 1

    print(f"\n  markout source: {dict(by_source)}")

    # The decisive comparison. Where a fill has both timelines, the paired
    # difference is the placed-clustering bias in cents, measured rather than
    # argued about.
    paired = [f for f in fills
              if f["markout_clean"] is not None and f["markout_full"] is not None]

    if len(paired) >= 10:
        deltas = [f["markout_full"] - f["markout_clean"] for f in paired]
        n, groups, mean, se = clustered(deltas, [f["ticker"] for f in paired])
        print(f"  placed-timeline bias, paired on {n} fills across {groups} "
              f"markets: {fmt(mean, se)}")
        print("  (placed-derived markout minus mid-derived markout, same fill)")
    else:
        print(f"  only {len(paired)} fills carry both timelines, so the "
              "placed-clustering bias cannot be measured here")

    known_lag = census.get("fills_with_known_lag", 0)

    if known_lag == 0:
        print("""
  WARNING: mid_lag_seconds is null on every fill, so the gap between exchange
  execution and our own journal stamp is UNMEASURED in this corpus. The OBI
  join below is therefore reported across a range of offsets rather than at a
  single assumed lookback. Runs after the trader.py fix will carry the real
  lag and can pin this down.""")
    else:
        lags = [f["markout_lag"] for f in fills]
        print(f"  markout sample lag: median {st.median(lags):.2f}s "
              f"max {max(lags):.2f}s")

    takers = sum(1 for f in fills if f["taker"])
    print(f"\n  taker fills: {takers} of {len(fills)}")

    if takers == 0:
        print("""  Zero. The ledger's own per-venue cross rate is 2-9%, so this
  corpus contains no forced crosses at all - it cannot show what a blocked fill
  costs when the stranded inventory has to be crossed out later. The passive
  filter below excludes nothing.""")

    tickers = {f["ticker"] for f in fills}
    print(f"\njoining OBI from recordings for {len(tickers)} tickers...")
    obi_series = asyncio.run(load_obi(args.rec_dirs, tickers))

    # ---- the contamination diagnostic ----
    print(f"\n{'=' * 86}")
    print("OBI JOIN OFFSET SWEEP - is the dose-response a forecast or an echo?")
    print("""
Negative offsets sample the book before the fill stamp; +5s samples a book that
certainly postdates it and is a pure placebo. Being filled itself depletes the
touch we were resting on, so a book sampled late looks imbalanced against us
BECAUSE we were filled. If the gap widens monotonically as the sample moves
toward and past the fill, that is the artifact, not the signal.""")
    print(f"\n{'offset':>8}{'matched':>9}{'rate':>7}   "
          f"gap: markout(obi_against >= .9) - markout(rest)")

    joins = {}

    for offset in sorted(args.offsets):
        matched = join_obi(fills, obi_series, offset)
        joins[offset] = matched
        label = f"{offset:+.2f}s" + (" PLACEBO" if offset > 0 else "")
        rate = len(matched) / max(len(fills), 1)
        print(f"{label:>8}{len(matched):>9}{rate:>7.0%}   "
              f"{dose_line(matched, 0.9) if matched else 'no matches'}")

    matched = joins.get(REPORT_OFFSET) or join_obi(fills, obi_series, REPORT_OFFSET)

    if not matched:
        print("\nnothing matched - are the recording dirs right?")
        return

    print(f"\n{'=' * 86}")
    print(f"DETAIL AT OFFSET {REPORT_OFFSET:+.2f}s  ({len(matched)} fills, "
          f"{len(matched) / max(len(fills), 1):.0%} of the corpus)")
    print("""
The matched subset is selected on collector coverage, which is not random with
respect to venue or time of day. Read every number below as conditional on it.""")

    bucket_table(matched, obi_bucket,
                 "RAW MARKOUT BY IMBALANCE AGAINST THE FILL")

    print(f"\n{'-' * 86}")
    print("SAME GRADIENT, SPLIT BY WHICH TIMELINE PRODUCED THE MARKOUT")
    print("""
A gradient that lives only in the placed-sourced rows is a property of the
timeline, not of the market: those mids cluster milliseconds after the very
fill being measured.""")

    for source in ("mid", "placed"):
        subset = [r for r in matched if r["markout_source"] == source]

        if subset:
            print(f"\n  {source}-sourced ({len(subset)} fills): "
                  f"{dose_line(subset, 0.9)}")
    bucket_table(matched, lambda r: f"price {r['price'] // 1000}x",
                 "BY PRICE DECILE (ticks/1000)")
    bucket_table(matched, lambda r: r["venue"], "BY VENUE")
    bucket_table(matched, lambda r: "taker" if r["taker"] else "maker",
                 "BY FILL ROLE")

    # ---- the confounder controls ----
    print(f"\n{'=' * 86}")
    print("IS IT IMBALANCE, OR IS IT VENUE AND PRICE?")
    bucket_table(
        demean(matched, ("venue",)), obi_bucket,
        "VENUE-DEMEANED MARKOUT BY IMBALANCE",
        note="each fill minus its own venue's mean, so a toxic venue cannot "
             "print as a dose-response")
    bucket_table(
        demean(matched, ("venue", "price_decile")), obi_bucket,
        "VENUE-AND-PRICE-DEMEANED MARKOUT BY IMBALANCE",
        note="also minus the venue x price-decile cell mean")

    # ---- the direction split ----
    print(f"\n{'=' * 86}")
    print("BY DIRECTION - the gate's two halves, never pooled")
    print("""
The audit measured the underlying signal as one-sided: relative to a balanced
book, bid-heavy predicts +0.68c at 5s and ask-heavy +0.02c. A resting SELL is
endangered by a bid-heavy book (the supported half); a resting BUY by an
ask-heavy one (the half with no measured support). Pooling them would hide a
result that lives entirely in one.""")

    for label, subset in (
        ("SELL fills, endangered by a bid-heavy book (supported half)",
         [r for r in matched if not r["buying"]]),
        ("BUY fills, endangered by an ask-heavy book (unsupported half)",
         [r for r in matched if r["buying"]]),
    ):
        if subset:
            bucket_table(subset, obi_bucket, label)
            print(f"  gradient: {dose_line(subset, 0.9)}")

    # ---- the dose-response: sweep the gate over history ----
    passive = [f for f in matched if not f["taker"]]
    sweep(passive, THRESHOLDS)


if __name__ == "__main__":
    main()
