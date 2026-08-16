"""What the whole Kalshi universe looks like, from a complete scan."""

from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from theme import SERIES_BLUE, SERIES_ORANGE, style

DATA = json.loads((Path(__file__).resolve().parents[1] / "data" / "scan.json").read_text())
CAP = DATA["capacity"]

st.title("What the exchange actually offers")
st.caption(f"Complete scan of every open Kalshi market, {DATA['scanned_at_utc'][:10]}")

a, b, c, d = st.columns(4)
a.metric("Real markets", f"{DATA['real_markets']:,}")
b.metric("Liquid & two-sided", f"{DATA['liquid_markets']:,}")
c.metric("Clear their own fee", f"{DATA['viable_markets']:,}")
d.metric("Contracts/day", f"{CAP['all_liquid_volume'] / 1e6:,.0f}M")

viable_share = DATA["viable_markets"] / DATA["liquid_markets"]
vol_share = CAP["viable_volume"] / CAP["all_liquid_volume"]

st.markdown(
    f"""
**{DATA['real_markets']:,} open markets.** {DATA['liquid_markets']:,} of them have
two-sided quotes and 24h volume of 50+ contracts, carrying
**{CAP['all_liquid_volume']:,} contracts a day** between them.

Under the *pessimistic* fee assumption — the taker rate charged on both sides —
**{viable_share:.0%} of liquid markets clear their own fee**, and those carry
**{vol_share:.0%} of the volume**.
"""
)

st.error(
    "**Correction.** An earlier version of this page reported 2,504 markets and "
    "$64/day. That scan paged the `/markets` endpoint and stopped early, so it "
    "covered about 3% of the exchange and contained zero crypto markets — and "
    "then reported their absence as a finding. This page is a complete scan via "
    "`/events`, which returns 84,181 real markets. The fee arithmetic was never "
    "wrong; the population it was applied to was."
)

st.divider()
st.subheader("Where the viable volume sits on the price range")

bands = DATA["price_bands"]
labels = [row["band"].split(" (")[0] for row in bands]

fig = go.Figure()
fig.add_trace(
    go.Bar(
        name="clears the fee",
        x=labels,
        y=[row["viable_volume"] for row in bands],
        marker=dict(color=SERIES_BLUE, line=dict(width=0)),
        hovertemplate="%{x}<br>%{y:,} contracts clear the fee<extra></extra>",
    )
)
fig.add_trace(
    go.Bar(
        name="blocked by fees",
        x=labels,
        y=[row["volume"] - row["viable_volume"] for row in bands],
        marker=dict(color=SERIES_ORANGE, line=dict(width=0)),
        hovertemplate="%{x}<br>%{y:,} contracts blocked<extra></extra>",
    )
)
style(fig, height=360, y_title="24h volume (contracts)")
fig.update_layout(barmode="stack", showlegend=True, bargap=0.35)
st.plotly_chart(fig, use_container_width=True)

band_rows = [
    {
        "Distance from an end": row["band"],
        "Viable": f"{row['viable']:,} of {row['markets']:,}",
        "Hit rate": f"{row['viable'] / row['markets']:.0%}" if row["markets"] else "-",
        "24h volume": f"{row['volume']:,}",
        "Volume that clears": f"{row['viable_volume']:,}",
    }
    for row in bands
]
st.dataframe(band_rows, hide_index=True)

st.markdown(
    """
The deep tails have much the highest hit rate, exactly as the fee curve
predicts. But they are not the *only* place that works: a market near the money
clears the fee whenever its spread is wide enough, and plenty of thinly-quoted
markets are. The rule is **spread versus fee at that price**, not "only trade
the tails".
"""
)

st.divider()
st.subheader("By category")

cats = [row for row in DATA["categories"] if row["total"] >= 20][:10]
cat_rows = [
    {
        "Category": row["category"],
        "Viable / liquid": f"{row['viable']:,} / {row['total']:,}",
        "Hit rate": f"{row['viable'] / row['total']:.0%}",
        "24h volume": f"{row['volume']:,}",
    }
    for row in cats
]
st.dataframe(cat_rows, hide_index=True)

st.markdown(
    """
Sports dominates Kalshi by volume and therefore dominates this table. Crypto —
the family the early live testing was pointed at — is present and partly
viable, but the viable strikes are the **wings of each ladder**, not the
at-the-money strikes where the volume concentrates.
"""
)

st.divider()
st.subheader("How much is it worth?")

st.warning(
    "The dollar figures below are an **upper bound on the fee-permitted edge**, "
    "not a forecast. They assume we capture the quoted spread on every round "
    "trip with no adverse selection — precisely the assumption that fails in "
    "practice. They also concentrate in wide-spread, thinly traded markets, "
    "where 'we intermediate 10% of volume' is weakest. Treat them as a ceiling "
    "on the opportunity, and read the markout page for what erodes it."
)

share_rows = [
    {
        "Share of volume intermediated": f"{row['share']:.0%}",
        "Markets": f"{row['viable']:,}",
        "Fee-permitted edge": f"${row['daily']:,.0f}/day",
    }
    for row in CAP["by_share"]
]
st.dataframe(share_rows, hide_index=True)

st.markdown(
    f"""
Volume-weighted net edge across viable markets is
**{CAP['weighted_net_edge_cents']:.2f}c per contract**. Order size does not enter
this at all — the estimate scales with the *market's* volume and our share of
it, not with our capital. Trading bigger only wins a larger share of a fixed
pool.
"""
)

st.divider()
st.subheader("The fee schedule matters more than anything we control")

sched_rows = [
    {
        "Fee schedule": row["label"],
        "Viable markets": f"{row['viable']:,}",
        "Addressable volume": f"{row['volume']:,}",
        "Fee-permitted edge": f"${row['daily']:,.0f}/day",
    }
    for row in CAP["by_schedule"]
]
st.dataframe(sched_rows, hide_index=True)

st.info(
    "Kalshi's published schedule charges the taker rate on takers, and public "
    "summaries say **most standard markets carry no maker fee at all**. If that "
    "applies to this account, the pessimistic row is far too harsh and most of "
    "the exchange is quotable.\n\n"
    "`market/fees.py::calibrate_from_fills` settles it against the `fees_paid` "
    "Kalshi reports on real executions. One session of data decides which row "
    "of this table is the real one."
)

st.divider()
st.subheader("The markets that clear their fee, ranked")

table = [
    {
        "Ticker": row["ticker"],
        "Mid": f"${row['mid']:.2f}",
        "Band": row["band"].split(" (")[0],
        "Spread": f"{row['spread_cents']:.1f}c",
        "Fee": f"{row['fee_cents']:.2f}c",
        "Net": f"{row['net_cents']:.2f}c",
        "24h vol": f"{row['volume_24h']:,}",
    }
    for row in DATA["top_markets"]
]
st.dataframe(table, hide_index=True)

st.caption(
    "Reproduce: `python scripts/screen_markets.py --prod --min-volume 50`, then "
    "`python analysis_app/build_data.py <payload> <timestamp>`. The public "
    "market-data endpoint needs no auth."
)
