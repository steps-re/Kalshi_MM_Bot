"""Kalshi against the other venue with open data, and what the comparison shows."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from kalshi_mm_bot.market.fees import KalshiFeeModel
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR

from theme import GRIDLINE, INK_MUTED, SERIES_BLUE, SERIES_ORANGE, annotate, style

MODEL = KalshiFeeModel()
BIG = 100 * COUNT_SCALE

st.title("Kalshi versus the rest of the universe")

st.markdown(
    """
Two prediction-market venues publish enough open data to screen: **Kalshi** and
**Polymarket**. Manifold is play money. PredictIt is small and closing.
Betfair needs credentials. So the comparison is those two — and it turns out to
be the most useful thing in this whole analysis, because it shows that the
constraint we spent all this effort measuring is *not a law of prediction
markets*. It is a property of one fee schedule.
"""
)

st.divider()
st.subheader("The structural difference, in one line")

st.markdown(
    """
| | Kalshi | Polymarket |
| --- | --- | --- |
| Fee formula | `0.07 x C x P x (1-P)`, rounded up per order | `rate x min(P, 1-P)`, rate 0.04-0.07 |
| Who pays | takers; **maker fee is 0 on most standard markets** | **takers only** — every schedule sets `takerOnly: true` |
| Maker rebate | none | 15-25% of the taker fee, rebated to makers |
| Minimum tick | 1c | 0.1c on over half of active markets |
"""
)

st.success(
    "**Every Polymarket fee schedule observed carries `takerOnly: true`.** A "
    "market maker resting quotes there pays nothing and is paid a rebate. The "
    "fee wall that dominates the Kalshi analysis simply does not exist for a "
    "maker on Polymarket."
)

st.markdown(
    """
Sample: the 2,100 highest-volume open Polymarket markets (the API caps the
listing there, which for this question is the right slice). Of those, 1,518 had
two-sided order books, and among markets priced between 2c and 98c with at
least \\$1k of 24h volume — 1,120 markets, \\$16.2M of daily volume — the fee
schedules break down as:

- `sports_fees_v2`, `politics_fees`, `weather_fees`, `crypto_fees_v2`,
  `culture_fees`: all taker-only, rates 0.04-0.07, rebates 0.15-0.25
- 223 markets with no fee schedule at all
"""
)

st.divider()
st.subheader("What that does to the screen")

st.markdown(
    """
Run the same viability screen against Polymarket and **every liquid market
passes**. Not "most" — all of them. With no maker fee, any spread of at least
one tick is capturable, and the tick is 0.1c.

That result is not an opportunity estimate. It is the screen telling you it has
stopped being the binding analysis.
"""
)

st.warning(
    "**Do not read this as 'Polymarket is $50k/day of free money'.** The fee "
    "screen answers one question: *does the fee structure permit market making "
    "at all?* On Kalshi that question is live and discriminating. On Polymarket "
    "the answer is trivially yes, which means the binding constraints there are "
    "the ones this screen does not model — **adverse selection, and competing "
    "against market makers who are already doing it well.** Those can only be "
    "measured with markout on real fills."
)

st.divider()
st.subheader("Why the fee curve shape still matters on both")

prices = list(range(100, ONE_DOLLAR, 100))
kalshi = [MODEL.breakeven_edge_ticks(yes_price=p, count=BIG) / 100 for p in prices]
# Polymarket taker fee ~ rate * min(P, 1-P); shown at 5% for one side.
poly = [0.05 * min(p, ONE_DOLLAR - p) / ONE_DOLLAR * 100 for p in prices]

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=[p / ONE_DOLLAR for p in prices],
        y=kalshi,
        mode="lines",
        line=dict(color=SERIES_BLUE, width=2),
        hovertemplate="P=$%{x:.2f}<br>Kalshi round trip %{y:.2f}c<extra></extra>",
    )
)
fig.add_trace(
    go.Scatter(
        x=[p / ONE_DOLLAR for p in prices],
        y=poly,
        mode="lines",
        line=dict(color=SERIES_ORANGE, width=2),
        hovertemplate="P=$%{x:.2f}<br>Polymarket taker %{y:.2f}c<extra></extra>",
    )
)
fig.add_hline(y=1, line=dict(color=GRIDLINE, width=1, dash="dot"))
annotate(fig, 0.02, 1, "1c spread", color=INK_MUTED, shift=8)
annotate(fig, 0.30, kalshi[29], "Kalshi, round trip, both sides", color=SERIES_BLUE, shift=-18)
annotate(fig, 0.30, poly[29], "Polymarket, taker side only", color=SERIES_ORANGE, shift=14)
style(fig, x_title="YES price", y_title="Fee (cents per contract)")
fig.update_xaxes(tickformat="$.2f")
st.plotly_chart(fig, use_container_width=True)

st.markdown(
    r"""
Both curves are humped in the middle, because both fees scale with how
uncertain the contract is. The difference is *who pays* and *how many times*.
Kalshi's blue line is a round trip charged to a maker under the pessimistic
assumption; Polymarket's orange line is charged only when you cross.
"""
)

st.divider()
st.subheader("What this means for the project")

st.markdown(
    """
1. **The fee-wall screen is a Kalshi-specific tool.** That is a feature — it is
   exactly the discriminating question on the venue we can trade. It is just
   not a general theory of prediction markets.
2. **The Kalshi maker-fee question is now the whole ballgame.** If Kalshi
   charges this account no maker fee on standard markets, Kalshi looks much more
   like Polymarket than like the pessimistic table, and most of the exchange
   opens up. Calibrating against real fills settles it in one session.
3. **Where the fee wall does not bind, adverse selection is the constraint** —
   which is what the markout tooling was built for, and why it is the number to
   read first.
"""
)

st.info(
    "**Compliance, not a technical point.** Kalshi is the CFTC-regulated venue. "
    "Polymarket's US availability changed with its 2025 acquisition of a "
    "CFTC-licensed exchange, and the position for a given US person is a "
    "question for counsel, not for this app. Everything here is read-only "
    "market-structure research on public data."
)
