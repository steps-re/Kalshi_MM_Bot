"""What Kalshi is actually made of, counted rather than assumed."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

DATA = Path(__file__).resolve().parents[1] / "data" / "exchange_census.json"
census = json.loads(DATA.read_text())

st.title("The anatomy of the exchange")
st.caption(
    "A full crawl of settled markets, not a sample of the famous ones. "
    "Everything here is a market COUNT, which is the view you only get by "
    "paging the whole list."
)
st.caption(f"Generated {census['generated_utc']} · "
           f"{census['distinct_days']} distinct settlement days, "
           f"{census['day_span'][0]} to {census['day_span'][1]}")

st.header("94% of the exchange is parlays")
st.markdown(
    """
Crawling every settled market with volume over a month returns **19.76 million**
of them. **18.48 million are MVE parlay products** - multi-leg combination
tickets, mostly `KXMVECROSSCATEGORY` and `KXMVESPORTSMULTIGAMEEXTENDED`.

They carry volume and they are not tradeable in the sense this project cares
about. A targeted sample of 4,000 of the highest-volume ones found:

* 1,658 with no candlestick history at all,
* 2,089 showing a placeholder book five minutes before close,
* **39 with an actionable two-sided book.**

One percent. The parlay tickets are a retail product that prints a price when
someone buys one, not a market with a resting book you can quote into. Any
"Kalshi volume" figure that does not separate them is describing a different
business than the one a market maker can participate in.
"""
)

families = pd.DataFrame(census["families"])
families = families[families["markets"] > 0].copy()
families["share"] = (families["share"] * 100).round(2)
families = families.rename(columns={
    "family": "family", "markets": "markets", "series": "distinct series",
    "share": "% of markets"})
st.dataframe(families, hide_index=True)

st.caption(
    "Parlays are excluded from every other page and from the calibration work. "
    "They are counted here because the exclusion is itself a finding."
)

st.header("Where tradeable breadth actually lives")
st.markdown(
    f"""
Strip the parlays and 1.28 million real settled markets remain, of which
**{census['total_markets']:,} actually traded**, across
{census['distinct_days']} days. Of those,
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
    "days": "days", "per_day": "per day", "tails": "tail-priced",
    "faves": "favourite-priced", "avg_volume": "avg volume"})
st.dataframe(breadth, hide_index=True)

st.header("The calibration question, and its replication failure")
st.markdown(
    """
Kalshi charges takers `7% x P x (1-P)` and charges makers nothing. Settlement
is also free. So the one structure that pays no fee on either leg is: rest an
order, hold it to settlement, never cross. It also has no exit-fill problem,
because there is no exit.

That makes tail mispricing the natural thing to hunt. Two trades were fixed in
advance - **sell YES on anything priced at or under 5c**, and **buy YES on
anything at or above 80c**, both held to settlement - so there was no choosing
a winner from a grid of buckets afterwards. Prices are taken from the book at a
fixed time before close, never the last print, and errors cluster on
series-by-day because consecutive windows ride one underlying path.
"""
)

pooled = pd.DataFrame(census["pooled"])

if not pooled.empty:
    pooled["t"] = (pooled["net_cents"] / pooled["se_cents"]).round(1)
    view = pooled[["lookback_min", "family", "trade", "n", "clusters",
                   "losses", "net_cents", "se_cents", "t"]].rename(columns={
        "lookback_min": "lookback (min)", "family": "family", "trade": "trade",
        "n": "contracts", "clusters": "clusters", "losses": "losses",
        "net_cents": "net (c)", "se_cents": "se (c)"})
    st.dataframe(view, hide_index=True)

st.warning(
    "Read the **losses** column before any t-statistic. These trades win small "
    "and often and lose big and rarely, so a cell with no losses in sample has "
    "no variance and its error bar collapses. Tennis first printed t=24.6 off "
    "ZERO losses in 344 contracts. Every error bar here is now floored by the "
    "uncertainty in the loss RATE (rule of three, in money), which is what "
    "brought that to t=3.8.",
    icon="⚠️",
)

st.error(
    "**The cross-family comparison does not mean what it looks like.** "
    "'Five minutes before close' is not a constant amount of remaining "
    "uncertainty. For a 15-minute Bitcoin window it is a third of the window "
    "with real price risk left. For a tennis match it is the final game: only "
    "2% of tennis markets sit within a cent of their T-60 price, and typical "
    "paths run 0.53 -> 0.26 -> 0.04. Selling a 4c tennis tail at T-5 is "
    "selling a player who has already lost, which is a different trade from "
    "selling a 4c Bitcoin strike that can still be reached. These rows are "
    "not replications of each other.",
    icon="🚨",
)

st.markdown(
    """
The right conditioner is *fraction of the event's uncertainty still
outstanding*, not wall-clock minutes, and that has to be built per family
before any of these numbers can be compared. Until it is, the only honest
statement is that crypto ladders show a modest tail edge that commodities and
indices do not reproduce.

This is the fourth idea in this project to look good on one slice and vanish on
the next. The taker scan crowned a different winner in each of three periods.
The order-imbalance gate was null offline and null live. A weather model lost
to the market's own forecast. The pattern is consistent enough to be the real
finding: **on this fee schedule, apparent edges are usually the measurement.**
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
   expiry is a digital option; hedging one \$1 contract five minutes out takes
   about \$259 of underlying. Firms that quote these ladders export the
   directional risk elsewhere and keep the spread. Without that leg, spread
   capture is real (+0.4c per fill, measured) and realised profit is zero
   (measured over 68 live cycles).
4. **The retail-accessible edge, if any, is diversification rather than
   prediction** - many small independent settlements, no crossing, no exit.
   That is the one idea still standing, and it has not replicated yet.
"""
)
