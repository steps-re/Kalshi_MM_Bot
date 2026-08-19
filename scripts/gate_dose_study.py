"""Which real fills lost money, and how much would each gate level have saved?

    python scripts/gate_dose_study.py ~/kalshi-audit/journals2 \\
        ~/kalshi-audit/recs2 ~/kalshi-audit/recs

The live A/B tests one gate threshold at ~4 cycles an hour. The 2,500 real
maker fills already journaled ARE the treatment group of every threshold at
once: for each fill, look up the order-book imbalance the collector recorded
just before it, ask "would a gate at level t have blocked this fill?", and
compare the 30s markout of blocked versus kept. Real fills carry real adverse
selection, so this sidesteps the fill-model problem entirely - nothing is
simulated except the veto.

Two joins, each from the source that is trustworthy for it:

* **markout** comes from the journal's own book timeline (same websocket that
  produced the fills - the recording join failed its t=0 control, the self-book
  one passes it).
* **OBI** comes from the collector recordings (the journal carries no sizes).
  Separate connection, so up to ~1s of skew; OBI episodes last seconds, and
  misclassification from skew dilutes a real effect rather than creating one.

Sign convention: `obi_against` is imbalance pointing AGAINST the passive fill -
for our resting sell being lifted, a bid-heavy book (price about to rise); for
our resting buy being hit, ask-heavy. The gate blocks a fill when obi_against
exceeds its threshold AND the fill would have opened or extended a position
(the increases-only rule that keeps inventory management alive).
"""

from __future__ import annotations

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

from taker_extract import parse_utc, walk  # noqa: E402

TPC = 100
HORIZON = 30.0
THRESHOLDS = (50, 60, 70, 80, 90, 95)
OBI_SKEW_TOLERANCE = 2.0     # max seconds between fill and the OBI sample


def parse_iso(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def load_journals(journal_dir: Path):
    """Fills with self-book markout, plus a running position per ticker."""

    fills = []

    for path in sorted(journal_dir.glob("*.jsonl")):
        timeline: dict[str, list[tuple[float, int]]] = defaultdict(list)
        raw_fills = []

        for line in path.read_text().splitlines():
            line = line.strip()

            if not line:
                continue

            try:
                event = json.loads(line)
            except ValueError:
                continue

            at = parse_iso(event.get("at"))

            if at is None:
                continue

            kind = event.get("event")

            if kind in ("mid", "placed") and event.get("mid") is not None:
                timeline[event["market_ticker"]].append((at, event["mid"]))
            elif kind == "filled" and event.get("yes_price") is not None:
                raw_fills.append(event | {"at": at})

        for series in timeline.values():
            series.sort()

        position: dict[str, int] = defaultdict(int)
        raw_fills.sort(key=lambda f: f["at"])

        for fill in raw_fills:
            ticker = fill["market_ticker"]
            buying = fill.get("action") == "buy"
            count = int(float(fill.get("count", 1)) * 100) if isinstance(
                fill.get("count"), str) else int(fill.get("count", 100))
            before = position[ticker]
            position[ticker] += count if buying else -count
            series = timeline.get(ticker)

            if not series:
                continue

            when = fill["at"] + HORIZON

            if when > series[-1][0]:
                continue

            index = bisect_right(series, (when, float("inf"))) - 1

            if index < 0:
                continue

            sign = 1.0 if buying else -1.0
            markout = sign * (series[index][1] - fill["yes_price"]) / TPC
            # Did this fill increase our exposure? A fill that reduced the
            # position would never have been blocked, at any threshold.
            increases = before >= 0 if buying else before <= 0
            fills.append({
                "ticker": ticker,
                "at": fill["at"],
                "buying": buying,
                "price": fill["yes_price"],
                "markout": markout,
                "increases": increases,
                "taker": bool(fill.get("is_taker")),
                "journal": path.stem,
            })

    return fills


async def load_obi(rec_dirs, tickers_needed):
    """Per-ticker [(abs_utc, obi)] from every recording that covers a fill."""

    series: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for rec_dir in rec_dirs:
        recordings = sorted(
            p for p in Path(rec_dir).iterdir() if (p / "manifest.json").exists())

        for index, rec in enumerate(recordings, 1):
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
                print(f"  {rec_dir}: {index}/{len(recordings)}", flush=True)

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


def bucket_table(rows, key, label) -> None:
    groups: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        groups[key(row)].append(row["markout"])

    print(f"\n{label}")
    print(f"{'bucket':<22}{'fills':>7}{'mean markout':>15}{'% losing':>10}")

    for name in sorted(groups):
        vals = groups[name]

        if len(vals) < 15:
            continue

        losing = sum(1 for v in vals if v < 0) / len(vals)
        print(f"{name:<22}{len(vals):>7}{st.mean(vals):>+13.3f}c{losing:>10.0%}")


def main() -> None:
    journal_dir = Path(sys.argv[1])
    rec_dirs = sys.argv[2:]
    fills = load_journals(journal_dir)
    print(f"{len(fills)} real fills with a 30s self-book markout")

    tickers = {f["ticker"] for f in fills}
    print(f"joining OBI from recordings for {len(tickers)} tickers...")
    obi_series = asyncio.run(load_obi(rec_dirs, tickers))
    matched = []

    for fill in fills:
        obi = obi_at(obi_series.get(fill["ticker"]), fill["at"] - 0.25)

        if obi is None:
            continue

        # Imbalance pointing against the passive fill: our sell is picked off
        # by a bid-heavy book, our buy by an ask-heavy one.
        fill["obi_against"] = obi if not fill["buying"] else -obi
        matched.append(fill)

    print(f"{len(matched)} fills matched to a fresh OBI sample "
          f"({len(matched) / max(len(fills), 1):.0%})\n")

    if not matched:
        print("nothing matched - are the recording dirs right?")
        return

    # ---- the pattern hunt: what do losing fills look like? ----
    def obi_bucket(row):
        v = row["obi_against"]

        for lo, name in ((0.95, "against >= .95"), (0.9, "against .90-.95"),
                         (0.7, "against .70-.90"), (0.5, "against .50-.70"),
                         (0.2, "against .20-.50")):
            if v >= lo:
                return f"{1 - lo:.0f}{name}"

        if v <= -0.5:
            return "5 WITH the fill >= .5"

        return "4 balanced"

    bucket_table(matched, obi_bucket,
                 "REAL-FILL MARKOUT BY IMBALANCE AGAINST THE FILL "
                 "(the direct evidence)")
    bucket_table(matched, lambda r: f"price {r['price'] // 1000}x",
                 "BY PRICE DECILE (ticks/1000)")
    bucket_table(matched, lambda r: r["ticker"].split("-", 1)[0], "BY VENUE")
    bucket_table(matched, lambda r: "taker" if r["taker"] else "maker", "BY FILL ROLE")

    # ---- the dose-response: sweep the gate over history ----
    passive = [f for f in matched if not f["taker"]]
    print(f"\n{'=' * 74}\nGATE SWEEP over {len(passive)} passive fills "
          f"(takers are our own crosses - the gate never sees them)")
    print(f"\n{'thresh':>7}{'blocked':>9}{'% fills':>9}"
          f"{'blocked markout':>17}{'kept markout':>14}{'saved/cycle*':>14}")
    baseline = st.mean([f["markout"] for f in passive])
    journals = len({f["journal"] for f in passive})

    for threshold in THRESHOLDS:
        level = threshold / 100.0
        blocked = [f for f in passive
                   if f["obi_against"] >= level and f["increases"]]
        blocked_ids = {id(f) for f in blocked}
        kept = [f for f in passive if id(f) not in blocked_ids]

        if len(blocked) < 10:
            print(f"{threshold:>6}%{len(blocked):>9}{'-':>9}{'-':>17}{'-':>14}{'-':>14}")
            continue

        saved = -sum(f["markout"] for f in blocked) / journals
        print(f"{threshold:>6}%{len(blocked):>9}"
              f"{len(blocked) / len(passive):>9.0%}"
              f"{st.mean([f['markout'] for f in blocked]):>+15.3f}c"
              f"{st.mean([f['markout'] for f in kept]):>+12.3f}c"
              f"{saved:>+13.2f}c")

    print(f"\nbaseline mean markout, all passive fills: {baseline:+.3f}c")
    print("* 'saved/cycle' = the blocked fills' total markout, negated, per journal")
    print("  (a journal is one cycle). Positive means the gate removes net losers.")
    print("  A good threshold blocks strongly-negative fills and few of the rest.")


if __name__ == "__main__":
    main()
