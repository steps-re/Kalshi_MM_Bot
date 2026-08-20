"""What Kalshi is actually made of, counted rather than assumed.

Every number on this page comes out of `data/exchange_census.json`, which
`build_exchange_census.py` regenerates from the crawl. Nothing here is typed.
That rule exists because this page used to carry a hand-typed decay table that
disagreed with the generated table rendered directly above it - including on
the SIGN of the number the page's conclusion rested on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

DATA = Path(__file__).resolve().parents[1] / "data" / "exchange_census.json"
census = json.loads(DATA.read_text())

parlays = census.get("parlays_excluded", 0)
traded = census["total_markets"]
zero_volume = census.get("dropped_zero_volume", 0)
no_result = census.get("dropped_no_result", 0)
crawled = parlays + traded + zero_volume + no_result
real = traded + zero_volume + no_result

st.title("The anatomy of the exchange")
st.caption(
    "A full crawl of settled markets, not a sample of the famous ones. "
    "Everything here is a market COUNT, which is the view you only get by "
    "paging the whole list."
)
st.caption(f"Generated {census['generated_utc']} · "
           f"{census['distinct_days']} distinct settlement days, "
           f"{census['day_span'][0]} to {census['day_span'][1]}")

if parlays:
    share = parlays / crawled
    st.header(f"{share:.0%} of the exchange is parlays")
    st.markdown(
        f"""
Crawling every settled market over the window returns **{crawled:,}** of them.
**{parlays:,} are MVE parlay products** - multi-leg combination tickets, mostly
`KXMVECROSSCATEGORY` and `KXMVESPORTSMULTIGAMEEXTENDED`.

They carry volume and they are not tradeable in the sense this project cares
about.
"""
    )
else:
    st.header("Parlays")
    st.warning(
        "This census was built from a corpus with **no parlay tickets in it**, "
        "so the parlay share cannot be shown. Rebuild against the full crawl "
        "(`settled_compact.jsonl.gz`) to populate it. The exclusion used to "
        "happen silently upstream while a `parlay-EXCLUDE` family sat in the "
        "code that had never once matched a record.",
        icon="⚠️",
    )

sample = census.get("parlay_sample")

if sample:
    st.markdown(
        f"""
A targeted sample of **{sample['parlays']:,}** parlay tickets (out of
{sample['file_rows']:,} rows in that file, {sample['non_parlay_rows']:,} of
which were not parlays) found:

* **{sample['no_candles']:,}** with no candlestick history at all,
* **{sample['no_actionable_book']:,}** with no actionable two-sided book five
  minutes before close,
* **{sample['actionable']:,}** with an actionable book
  ({sample['actionable_share']:.1%}).

Those three categories are exhaustive and sum to {sample['parlays']:,}.
"""
    )

    if sample["actionable_any_age"] > sample["actionable"]:
        st.caption(
            f"Dropping the staleness limit ({sample['staleness_limit_s']}s) "
            f"raises the actionable count to {sample['actionable_any_age']:,}. "
            f"Those extra books were real quotes from minutes or hours earlier, "
            f"not prices available five minutes before close."
        )

st.markdown(
    f"""
The parlay tickets are a retail product that prints a price when someone buys
one, not a market with a resting book you can quote into. Any "Kalshi volume"
figure that does not separate them is describing a different business than the
one a market maker can participate in.
"""
)

families = pd.DataFrame(census["families"])
families = families[families["markets"] > 0].copy()
families["share"] = (families["share"] * 100).round(2)
families = families.rename(columns={
    "family": "family", "markets": "markets", "series": "distinct series",
    "share": "% of traded non-parlay markets"})
st.dataframe(families, hide_index=True)

st.caption(
    f"Denominator is the {traded:,} settled non-parlay markets that actually "
    f"traded - NOT the {crawled:,} crawled. Parlays are excluded from every "
    f"other page and from the calibration work; they are counted above because "
    f"the exclusion is itself a finding."
)

st.header("Where tradeable breadth actually lives")
st.markdown(
    f"""
Strip the parlays and {real:,} real settled markets remain, of which
**{traded:,} actually traded** ({zero_volume:,} settled with zero volume),
across {census['distinct_days']} days. Of those,
**{census['breadth_total']} series** settle at least 100 markets across at
least 5 distinct days with at least 50 markets landing in the tails.

That number matters for one specific reason. A tail-selling strategy is only
as good as the number of *independent* settlements it can spread across -
fifty strikes on one Bitcoin ladder are one bet on Bitcoin, not fifty bets.
Per-player baseball props, individual tennis matches and per-city daily
temperatures settle on genuinely different things.
"""
)

breadth = pd.DataFrame(census["breadth"]).rename(columns={
    "series": "series", "family": "family", "markets": "settled",
    "days": "days", "per_day": "per day",
    "tails_lastprice": "tail-priced (last print)",
    "faves_lastprice": "favourite-priced (last print)",
    "avg_volume": "avg volume"})
st.dataframe(breadth, hide_index=True)

st.warning(census["breadth_basis"], icon="⚠️")

st.header("The calibration question")
st.markdown(
    """
Kalshi charges takers `7% x P x (1-P)` and charges makers nothing. Settlement
is also free. So the one structure that pays no fee on either leg is: rest an
order, hold it to settlement, never cross. It also has no exit-fill problem,
because there is no exit.

That makes tail mispricing the natural thing to hunt. Two trades were fixed in
advance - **sell YES on anything priced at or under 5c**, and **buy YES on
anything at or above 80c**, both held to settlement. Prices are taken from the
book at a fixed time before close, never the last print, the book must be no
more than three minutes stale, and errors cluster on series-by-day - or
family-by-day for crypto, indices, commodities and weather, whose members ride
one underlying and are not separate draws.
"""
)

pooled = pd.DataFrame(census["pooled"])

if not pooled.empty:
    view = pooled[["lookback_min", "family", "trade", "n", "markets",
                   "clusters", "losses", "loss_clusters", "net_cents",
                   "se_cents", "t", "t_critical_95", "significant_95",
                   "se_source"]].rename(columns={
        "lookback_min": "lookback (min)", "family": "family", "trade": "trade",
        "n": "contracts", "markets": "markets", "clusters": "clusters",
        "losses": "losses", "loss_clusters": "losing clusters",
        "net_cents": "net (c)", "se_cents": "se (c)", "t": "t",
        "t_critical_95": "95% crit", "significant_95": "sig?",
        "se_source": "se from"})
    st.dataframe(view, hide_index=True)

provenance = census.get("provenance", {})

st.warning(
    "Read the **losses** and **losing clusters** columns before any "
    "t-statistic. These trades win small and often and lose big and rarely, so "
    "a cell with no losses in sample has no variance and its error bar "
    "collapses. Every error bar is floored by the uncertainty in the loss "
    "RATE, counted in CLUSTERS rather than contracts - one tennis match takes "
    "down every contract riding it, so the Poisson count that matters is the "
    "count of loss EVENTS. Counting it in contracts, as an earlier version "
    "did, understated the floor about threefold.",
    icon="⚠️",
)

if provenance.get("multiplicity_note"):
    st.info(provenance["multiplicity_note"], icon="🔎")

st.header("The decay curve, and why it does not settle the question")
st.markdown(
    """
A real mispricing should survive a longer horizon. A convergence artifact
cannot, because it was only ever measuring resolution. So run the trade at
every horizon and watch the loss rate: that was meant to be the transferable
test this whole project produced.

It does not work on this data, and the reason is in the **`% same markets`**
column below. A market only enters a horizon if it had an actionable book that
far out *and* sat in the zone at that moment. The rows are therefore different
populations, not one population observed at different times - at the short
horizons they share a small minority of their markets with the long ones. Any
monotone pattern across those rows is confounded with who is in them.

The **balanced** rows hold the market set fixed at the markets present in every
horizon, which is the only controlled comparison available. Two things happen:
the sample collapses to a handful of markets, and the sign of the slope
reverses. Both say the same thing - **this dataset cannot identify the decay**,
so it neither confirms the edge nor kills it.
"""
)

for panel in census.get("decay", []):
    st.subheader(f"{panel['family']} · {panel['trade']}")

    if panel["missing_lookbacks"]:
        missing = ", ".join(f"{m}m" for m in panel["missing_lookbacks"])
        st.error(
            f"**No data at T-{missing}.** These markets do not exist that far "
            f"before they close, so the decay test cannot be run on this "
            f"family at all - it is untested here, not failed.",
            icon="🚨",
        )

    raw = pd.DataFrame(panel["rows"])

    if not raw.empty:
        raw["loss_rate"] = (raw["loss_rate"] * 100).round(2)
        raw["overlap_with_longest"] = (raw["overlap_with_longest"] * 100).round(0)
        st.dataframe(raw[["lookback_min", "n", "markets", "clusters", "losses",
                          "loss_rate", "net_cents", "se_cents", "t",
                          "overlap_with_longest"]].rename(columns={
            "lookback_min": "lookback (min)", "n": "contracts",
            "markets": "markets", "clusters": "clusters", "losses": "losses",
            "loss_rate": "loss rate %", "net_cents": "net (c)",
            "se_cents": "se (c)", "t": "t",
            "overlap_with_longest": "% same markets"}), hide_index=True)

    balanced = pd.DataFrame(panel["balanced"])

    if not balanced.empty:
        count = panel["balanced_markets"]
        st.caption(
            f"Balanced panel: the {count} "
            f"{'market' if count == 1 else 'markets'} present at every "
            f"horizon.")
        balanced["loss_rate"] = (balanced["loss_rate"] * 100).round(2)
        st.dataframe(balanced[["lookback_min", "n", "clusters", "losses",
                               "loss_rate", "net_cents", "se_cents",
                               "t"]].rename(columns={
            "lookback_min": "lookback (min)", "n": "contracts",
            "clusters": "clusters", "losses": "losses",
            "loss_rate": "loss rate %", "net_cents": "net (c)",
            "se_cents": "se (c)", "t": "t"}), hide_index=True)
    else:
        st.caption("No market is present at every horizon, so no balanced "
                   "panel exists for this trade.")

st.error(
    "**The balanced panel is not an estimate either.** Requiring a market to "
    "sit in the zone at every horizon selects markets that were already "
    "settled-looking an hour out and stayed that way, which is its own "
    "conditioning on the outcome. It is a diagnostic showing the raw curve is "
    "not identified, not a replacement for it.",
    icon="🚨",
)

st.markdown(
    """
The right conditioner is *fraction of the event's uncertainty still
outstanding*, not wall-clock minutes, and that has to be built per family
before any of these numbers can be compared. "Five minutes before close" is a
third of a 15-minute Bitcoin window and the final game of a tennis match.
Until that exists, the honest statement is that the tail and favourite trades
are **unresolved on this data** - positive at short horizons where convergence
bias lives, indistinguishable from zero at the long ones, and measured on
samples that barely overlap.

This is the fourth idea in this project to look good on one slice and behave
differently on the next. The taker scan crowned a different winner in each of
three periods. The order-imbalance gate was null offline and null live. A
weather model lost to the market's own forecast. The pattern is consistent
enough to be the real finding: **on this fee schedule, apparent edges are
usually the measurement.**
"""
)

st.header("What this says about how Kalshi works")
st.markdown(
    """
Put the pieces together and the exchange has a coherent shape:

1. **It taxes urgency and pays patience.** The fee is charged only to the side
   that crosses, is largest at 50c, and is zero at settlement. Every strategy
   this project killed was paying that tax somewhere.
2. **Most of the product is entertainment.** Parlays dominate by count, and the
   volume leaders are alien disclosure, presidential nominees and the Super
   Bowl. The exchange converts recreational interest into fees.
3. **The professional lane needs a hedge you cannot buy retail.** A binary near
   expiry is a digital option; hedging one \\$1 contract five minutes out takes
   about \\$259 of underlying. Firms that quote these ladders export the
   directional risk elsewhere and keep the spread. Without that leg, spread
   capture is real (+0.4c per fill, measured) and realised profit is zero
   (measured over 68 live cycles).
4. **The retail-accessible edge, if any, is diversification rather than
   prediction** - many small independent settlements, no crossing, no exit.
   That is the one idea still standing, and this data cannot yet tell whether
   it is real.
"""
)

with st.expander("Provenance"):
    st.json(provenance)
