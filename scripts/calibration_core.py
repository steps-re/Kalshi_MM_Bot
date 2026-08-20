"""One definition of the pre-specified calibration trades, shared by every caller.

This module exists because the definition used to live twice - once in
`calibration_at_t.py` (the script that prints the tables) and once in
`analysis_app/build_exchange_census.py` (the script that feeds the website) -
and the two copies disagreed. The printed version selected tail markets by
BUCKET edge (`hi <= 0.05`), which excludes a mid of exactly 5c; the website
version selected by `mid <= 0.05`, which includes it. Same "pre-specified"
trade, two populations, and the more inclusive one is what shipped.

Anything that reports these trades imports from here. If the definition needs
to change it changes in one place, for everybody, at once.

## What a "loss" costs, and why the SE floor is counted in clusters

Both trades win small and often and lose big and rarely. That shape breaks the
cluster sandwich: with no losses in sample the cluster means are near-identical,
the SE collapses and t explodes. The floor in `cluster_stats.loss_count_floor`
bounds the error bar by the uncertainty in the loss COUNT instead - and counts
that uncertainty in loss EVENTS at cluster level, because one tennis match or
one BTC path decides every contract in its cluster together.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from calibration_curves import family_of
from cluster_stats import (clustered_pooled, cluster_t_critical,
                           loss_count_floor)

# A RANGE, deliberately. The single most useful diagnostic in this study is how
# a tail trade's return decays with time-to-close: a real mispricing should
# persist as the horizon lengthens, a convergence artifact dies because it was
# only ever measuring "the outcome is already known". Report the curve, never a
# single horizon - and see `decay_panel`, because the horizons do not see the
# same markets unless you make them.
LOOKBACKS = (2 * 60, 5 * 60, 10 * 60, 30 * 60, 60 * 60)

TAIL_MAX = 0.05
FAVE_MIN = 0.80
MAX_SPREAD = 0.10          # book wider than 10c at T-minus = not actionable
# `book_at` takes the last candle at or before T-minus-X. Without an age limit
# a market that stopped printing an hour earlier hands back that hour-old book
# as if it were the price at T-2min. Three minutes on a one-minute candle.
MAX_STALENESS = 180
MIN_CONTRACTS = 60
# Cluster-robust inference is asymptotic in the NUMBER OF CLUSTERS. A cell
# resting on eighteen of them has no usable error bar regardless of how many
# contracts sit inside them.
MIN_CLUSTERS = 20

# Families whose members ride ONE underlying, so a same-day cluster must span
# the whole family rather than a single series. KXBTCD and KXETHD on one day
# are not two independent draws, and neither are KXDJI and KXSPX. Tennis and
# sports props are absent on purpose: separate matches settle separately, which
# is the entire diversification argument.
SHARED_UNDERLYING = {
    "crypto-15M": "crypto",
    "crypto-hourly": "crypto",
    "indices": "equity-index",
    "commodities": "commodities",
    "weather": "weather",
}


def fee(price: float) -> float:
    """Kalshi's taker fee. Makers pay nothing and settlement is free."""

    return 0.07 * price * (1.0 - price)


def book_at(candles: list[dict], when: float,
            max_staleness: float = MAX_STALENESS):
    """Bid/ask closes of the last candle ending at or before `when`.

    Returns None when there is no ACTIONABLE book: no candle inside the
    staleness window, a one-sided or unopened book (0.001/1.00 placeholders),
    a crossed book, or a spread too wide to trade.
    """

    best = None

    for candle in candles:
        ts = candle.get("end_period_ts")

        if ts is None or ts > when or when - ts > max_staleness:
            continue

        if best is None or ts > best.get("end_period_ts", 0):
            best = candle

    if best is None:
        return None

    try:
        bid = float(best["yes_bid"]["close_dollars"])
        ask = float(best["yes_ask"]["close_dollars"])
    except (KeyError, TypeError, ValueError):
        return None

    if bid <= 0.001 or ask >= 0.9999 or ask <= bid:
        return None

    if ask - bid > MAX_SPREAD:
        return None

    return bid, ask


def zone_of(mid: float) -> str | None:
    """Which pre-specified trade, if any, this mid qualifies for."""

    if mid <= TAIL_MAX:
        return "tail SELL"

    if mid >= FAVE_MIN:
        return "fave BUY"

    return None


def trade_pnl(zone: str, bid: float, ask: float, won: int) -> tuple[float, int]:
    """Per-contract P&L in dollars, and whether this contract lost.

    tail SELL: sell YES at the bid as a taker, pay the fee, hold to settlement.
    fave BUY:  buy YES at the ask as a taker, pay the fee, hold to settlement.

    Both price the ACTIONABLE side, so neither flatters itself with the mid.
    """

    if zone == "tail SELL":
        return bid - won - fee(bid), won

    return won - ask - fee(ask), 1 - won


def cluster_key(family: str, series: str, day: str) -> str:
    """The unit this data actually supports as an independent draw.

    Series-by-day for things that settle separately; family-by-day for the
    families that share one underlying, because a same-day BTC and ETH ladder
    are one bet on crypto.
    """

    return f"{SHARED_UNDERLYING.get(family, series)}|{day}"


def load_records(paths) -> tuple[list[dict], dict]:
    """Read candle files into one deduplicated list, and say what happened.

    Duplicates are real: the four candle files in this study share 1,238
    tickers, because separate targeted crawls (`--families tennis`,
    `--min-volume ...`) re-drew markets an earlier pass already had. Pooling
    them without deduplication counts those markets twice, inflates n and
    shrinks the error bar.

    A path that does not exist is an ERROR, not something to skip quietly: the
    silent version builds a smaller table and still reports the file count it
    was asked for.
    """

    seen: set[str] = set()
    records: list[dict] = []
    stats = {"files": [], "rows": 0, "duplicates": 0, "unparsed": 0}

    for path in paths:
        path = Path(path).expanduser()

        if not path.exists():
            raise FileNotFoundError(f"candle file not found: {path}")

        before = len(records)
        dupes = 0

        for line in path.open():
            try:
                d = json.loads(line)
            except ValueError:
                stats["unparsed"] += 1
                continue

            stats["rows"] += 1
            ticker = d.get("ticker")

            if ticker in seen:
                dupes += 1
                continue

            seen.add(ticker)
            records.append(d)

        stats["duplicates"] += dupes
        stats["files"].append({"path": str(path), "kept": len(records) - before,
                               "duplicates": dupes})

    return records, stats


def accumulate(records, lookbacks=LOOKBACKS) -> dict:
    """(lookback, family, zone) -> cluster -> [pnl_sum, contracts, losses].

    Also returns, per cell, the set of tickers that entered it, which is what
    makes the decay curve auditable: the horizons do not see the same markets.
    """

    cells: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0]))
    tickers: dict = defaultdict(set)

    for d in records:
        if d.get("result") not in ("yes", "no"):
            continue

        candles = d.get("candles") or []

        if not candles:
            continue

        series = d.get("series", "")
        family = family_of(series)
        day = datetime.fromtimestamp(
            d["close_ts"], tz=timezone.utc).date().isoformat()
        cluster = cluster_key(family, series, day)
        won = 1 if d["result"] == "yes" else 0

        for lookback in lookbacks:
            book = book_at(candles, d["close_ts"] - lookback)

            if book is None:
                continue

            bid, ask = book
            zone = zone_of((bid + ask) / 2)

            if zone is None:
                continue

            value, lost = trade_pnl(zone, bid, ask, won)

            for fam in (family, "ALL"):
                slot = cells[(lookback, fam, zone)][cluster]
                slot[0] += value
                slot[1] += 1
                slot[2] += lost
                tickers[(lookback, fam, zone)].add(d["ticker"])

    return {"cells": cells, "tickers": tickers}


def summarize(accumulated, min_contracts=MIN_CONTRACTS,
              min_clusters=MIN_CLUSTERS) -> list[dict]:
    """Cells that clear the size gates, with an error bar each has earned."""

    cells = accumulated["cells"]
    tickers = accumulated["tickers"]
    rows = []

    for (lookback, family, zone), clusters in cells.items():
        sums = [v[0] for v in clusters.values()]
        counts = [v[1] for v in clusters.values()]
        losses = [v[2] for v in clusters.values()]
        n, groups, mean, se = clustered_pooled(sums, counts)

        if n < min_contracts or groups < min_clusters:
            continue

        floor = loss_count_floor(losses, n)
        se_clustered = se
        se = max(se if se == se else 0.0, floor)
        critical = cluster_t_critical(groups)
        t = mean / se if se else float("nan")
        rows.append({
            "lookback_min": lookback // 60,
            "family": family,
            "trade": zone,
            "n": n,
            "markets": len(tickers[(lookback, family, zone)]),
            "clusters": groups,
            "losses": int(sum(losses)),
            "loss_clusters": sum(1 for k in losses if k > 0),
            "net_cents": round(mean * 100, 3),
            "se_cents": round(se * 100, 3),
            "se_source": "loss-rate floor" if se > se_clustered else "sandwich",
            "t": round(t, 2),
            "t_critical_95": round(critical, 2),
            "significant_95": abs(t) > critical,
        })

    rows.sort(key=lambda r: (r["lookback_min"], r["family"], r["trade"]))
    return rows


def _curve_rows(accumulated, family, zone, lookbacks, reference) -> list[dict]:
    """One row per horizon for a (family, trade), each with its own error bar."""

    cells = accumulated["cells"]
    tickers = accumulated["tickers"]
    rows = []

    for lookback in lookbacks:
        if (lookback, family, zone) not in cells:
            continue

        clusters = cells[(lookback, family, zone)]
        sums = [v[0] for v in clusters.values()]
        counts = [v[1] for v in clusters.values()]
        losses = [v[2] for v in clusters.values()]
        n, groups, mean, se = clustered_pooled(sums, counts)
        se = max(se if se == se else 0.0, loss_count_floor(losses, n))
        here = tickers[(lookback, family, zone)]
        rows.append({
            "lookback_min": lookback // 60,
            "n": n,
            "markets": len(here),
            "clusters": groups,
            "losses": int(sum(losses)),
            "loss_rate": round(sum(losses) / n, 5) if n else None,
            "net_cents": round(mean * 100, 3),
            "se_cents": round(se * 100, 3),
            "t": round(mean / se, 2) if se else None,
            "t_critical_95": round(cluster_t_critical(groups), 2),
            # Jaccard against the longest horizon, deliberately symmetric.
            # |here & ref| / |ref| alone reads as 100% for a short row that
            # merely CONTAINS the long sample inside a much bigger one, which
            # is exactly the case that needs flagging.
            "overlap_with_longest": (
                round(len(here & reference) / len(here | reference), 3)
                if (here or reference) else None),
        })

    return rows


def decay_panel(records, family: str, zone: str, lookbacks=LOOKBACKS) -> dict:
    """The decay curve, with the sample turnover it hides made explicit.

    "Run the same trade at every horizon" is not what a per-horizon table does.
    A market enters a horizon only if it had an actionable book that far out AND
    sat in the zone at that moment, so the rows are different populations. On
    this data the T-60 tennis tail-SELL row shares 6% of its markets with the
    T-10 row. Any monotone pattern across such rows is confounded with who is
    in them, and the monotone pattern is the finding.

    So report two things: the raw per-horizon rows WITH their overlap against
    the longest horizon, and a BALANCED panel restricted to the markets present
    at every horizon, which is the only version that isolates the horizon
    itself. A family whose markets do not exist an hour before they close -
    crypto-15M - cannot be tested this way at all, and says so in
    `missing_lookbacks` rather than silently reporting fewer rows.
    """

    accumulated = accumulate(records, lookbacks)
    tickers = accumulated["tickers"]
    available = [lb for lb in lookbacks if (lb, family, zone) in accumulated["cells"]]

    if not available:
        return {"family": family, "trade": zone, "rows": [], "balanced": [],
                "balanced_markets": 0,
                "missing_lookbacks": [lb // 60 for lb in lookbacks]}

    reference = tickers[(max(available), family, zone)]
    common = set.intersection(*(tickers[(lb, family, zone)] for lb in available))
    balanced = []

    if common:
        held = accumulate([d for d in records if d.get("ticker") in common],
                          lookbacks)
        balanced = _curve_rows(held, family, zone, available, common)

    return {
        "family": family,
        "trade": zone,
        "rows": _curve_rows(accumulated, family, zone, available, reference),
        "balanced": balanced,
        "balanced_markets": len(common),
        "missing_lookbacks": [lb // 60 for lb in lookbacks
                              if lb not in available],
    }


def accumulate_for(records, tickers: set[str], lookbacks=LOOKBACKS) -> dict:
    """`accumulate`, restricted to a fixed set of tickers."""

    return accumulate([d for d in records if d.get("ticker") in tickers],
                      lookbacks)
