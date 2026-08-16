"""The fee arithmetic, with a calculator that runs the real model."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from kalshi_mm_bot.analytics.screening import viable_price_band
from kalshi_mm_bot.market.fees import KalshiFeeModel
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR

from theme import GRIDLINE, INK_MUTED, SERIES_BLUE, annotate, style

MODEL = KalshiFeeModel()

st.title("The fee wall")

st.markdown(
    r"""
Kalshi's trading fee is

$$\text{fee} = \lceil\, 0.07 \times C \times P \times (1-P) \,\rceil_{\text{cent}}$$

applied **per order**. At \$0.50 that is 1.75c per side, **3.5c for a round
trip** — against a tick of 1c.

A market maker who captures the *entire* 2c spread of a market at \$0.50 still
loses 1.5c per contract per round trip. No parameter fixes this. It is
arithmetic, and it is why the early live runs saw fees eat everything.
"""
)

st.subheader("Try it")

left, right = st.columns(2)

with left:
    price_cents = st.slider("YES price (cents)", 1, 99, 50)
with right:
    contracts = st.select_slider(
        "Order size (contracts)",
        options=[1, 2, 5, 10, 25, 50, 100, 250, 500, 1000],
        value=10,
    )

yes_price = price_cents * 100
count = contracts * COUNT_SCALE

one_side = MODEL.fee_micros(yes_price=yes_price, count=count) / 1e6
round_trip = MODEL.round_trip_micros(yes_price=yes_price, count=count) / 1e6
breakeven = MODEL.breakeven_edge_ticks(yes_price=yes_price, count=count) / 100
surcharge = MODEL.ceiling_surcharge_micros(yes_price=yes_price, count=count) / 1e6

a, b, c = st.columns(3)
a.metric("Fee, one side", f"${one_side:,.4f}")
b.metric("Fee, round trip", f"${round_trip:,.4f}")
c.metric("Breakeven edge", f"{breakeven:.2f}c")

TICK_CENTS = 1.0

if breakeven <= TICK_CENTS:
    st.success(
        f"A {breakeven:.2f}c hurdle fits inside the 1c tick, so even a "
        "tick-wide market at this price clears its own fee."
    )
else:
    st.error(
        f"A {breakeven:.2f}c round-trip hurdle needs a spread at least that "
        f"wide. The tick is 1c, so this price only works where the market is "
        f"quoted at least {breakeven:.2f}c wide — and most are quoted 1c wide."
    )

if surcharge > 0:
    st.caption(
        f"Of that, \\${surcharge:,.4f} is the per-order rounding to the next cent. "
        "It does not scale with size, so it behaves like a fixed cost and hits "
        "small test orders hardest."
    )

st.divider()
st.subheader("Why the tails are the only place it works")

prices = list(range(100, ONE_DOLLAR, 100))
big = 100 * COUNT_SCALE
curve = [MODEL.breakeven_edge_ticks(yes_price=p, count=big) / 100 for p in prices]

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=[p / ONE_DOLLAR for p in prices],
        y=curve,
        mode="lines",
        line=dict(color=SERIES_BLUE, width=2),
        name="Round-trip fee",
        hovertemplate="P=$%{x:.2f}<br>round-trip fee %{y:.2f}c<extra></extra>",
    )
)

for cents, label in ((1, "1c spread"), (2, "2c spread"), (3, "3c spread")):
    fig.add_hline(y=cents, line=dict(color=GRIDLINE, width=1, dash="dot"))
    annotate(fig, 0.02, cents, label, color=INK_MUTED, shift=8)

style(fig, x_title="YES price", y_title="Round-trip fee (cents per contract)")
fig.update_xaxes(tickformat="$.2f")
st.plotly_chart(fig, use_container_width=True)

st.markdown(
    r"""
The curve is $P(1-P)$, so it peaks at \$0.50 and collapses toward both ends.
Anywhere the blue line sits **below** your spread, market making pays for
itself. Anywhere it sits above, it cannot — for anyone, at any size.
"""
)

st.divider()
st.subheader("Viable price bands, by quoted spread")

def band_label(spread_cents: int, improvement_ticks: int) -> str:
    """Viable band, via the same function the screener uses."""

    band = viable_price_band(
        spread_cents * 100,
        fee_model=MODEL,
        count=big,
        improvement_ticks=improvement_ticks,
    )
    if band is None:
        return "nowhere"

    high = band[1]
    if high >= ONE_DOLLAR // 2 - 100:
        return "the whole range"

    return (
        f"at/below ${high / ONE_DOLLAR:.3f}  or  "
        f"at/above ${(ONE_DOLLAR - high) / ONE_DOLLAR:.3f}"
    )


rows = [
    {
        "Quoted spread": f"{cents}c",
        "If you join the touch": band_label(cents, 0),
        "If you step inside to get filled": band_label(cents, 100),
    }
    for cents in (1, 2, 3, 4, 5, 6, 8)
]

st.dataframe(rows, hide_index=True)

st.info(
    "Joining the touch captures the whole spread but fills rarely; stepping "
    "inside fills far more often and gives up a tick per side. The right column "
    "is the one to plan with.\n\n"
    "**A one-tick market cannot be improved** — there is nowhere to stand "
    "between the bid and the ask — so both columns agree there. My first "
    "version of this screen subtracted a cent per side everywhere, which "
    "removed liquidity that was never there and wrongly declared every "
    "tick-wide market dead."
)

st.divider()
st.subheader("The caveat that could invert all of this")

st.warning(
    "Everything above assumes the 0.07 formula applies to **maker** fills. "
    "Kalshi has had a separate flat per-contract maker fee on some markets. If "
    "the account is billed that way, midpoint market making becomes viable and "
    "this whole page is wrong.\n\n"
    "`market/fees.py::calibrate_from_fills` reconciles the model against the "
    "`fees_paid` Kalshi already reports on real executions. Existing fills "
    "contain the answer. **Run it before trusting any backtest.**"
)
