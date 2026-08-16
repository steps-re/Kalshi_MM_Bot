"""Adverse selection, inventory risk, and the end-of-market effect generalised."""

from __future__ import annotations

from math import sqrt

import plotly.graph_objects as go
import streamlit as st

from theme import SERIES_BLUE, SERIES_ORANGE, annotate, style

st.title("What a market maker is actually risking")

st.markdown(
    """
The strategy modelled inventory risk with a constant. Reasonable first pass, but
it misses the two things that actually hurt.
"""
)

st.subheader("1. Adverse selection")

st.markdown(
    r"""
Your quote rests. Someone trades against it. The only question that matters is:
**did they know something you did not?**

If the mid moves against you right after every fill, you are being picked off,
and no amount of spread saves you — the informed trader simply waits for a
bigger edge. The right size for a quote's edge is roughly

$$\sigma \times \sqrt{\text{how long the quote will rest}}$$

where $\sigma$ is the *instantaneous* volatility of the mid. That is measurable
from the book (`market/dynamics.py`), so the same parameters behave sensibly in
a quiet market and a violent one without special-casing either.
"""
)

st.info(
    "**Markout** is how you measure it: for a fill at price `p` and direction "
    "`d`, compute `d × (mid(t+h) − p)` at several horizons `h`. A market maker "
    "expects this to be **positive**.\n\n"
    "Read the markout **before** the P&L. Over a ten-minute session the P&L is "
    "noise and the markout is not."
)

st.divider()
st.subheader("2. Inventory risk is not one thing")

st.markdown(
    r"""
Far from expiry, a position is a diffusion you can flatten out of — risk is
$\sigma\sqrt{t_{\text{flatten}}}$.

Near expiry, flattening stops being a choice. The position becomes a coin flip
you are stuck holding, worth $\sqrt{P(1-P)}$ in risk terms — which is *maximal
at \$0.50*, exactly where the fee is worst.
"""
)

taus = [1, 2, 5, 10, 20, 40, 80, 160, 320, 640]
sigma_per_sqrt_sec = 12.0
flatten_seconds = 30.0
terminal = sqrt(0.25) * 10000


def forced_hold(tau: float) -> float:
    if tau <= 0:
        return 1.0
    return max(0.0, 1.0 - min(1.0, tau / flatten_seconds))


diffusion = [sigma_per_sqrt_sec * sqrt(min(t, flatten_seconds)) for t in taus]
blended = [
    (1 - forced_hold(t)) * d + forced_hold(t) * terminal for t, d in zip(taus, diffusion)
]

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=taus,
        y=blended,
        mode="lines+markers",
        line=dict(color=SERIES_ORANGE, width=2),
        marker=dict(size=8),
        hovertemplate="%{x}s to close<br>%{y:.0f} ticks of risk<extra></extra>",
    )
)
fig.add_trace(
    go.Scatter(
        x=taus,
        y=diffusion,
        mode="lines",
        line=dict(color=SERIES_BLUE, width=2, dash="dot"),
        hovertemplate="%{x}s<br>diffusion only %{y:.0f} ticks<extra></extra>",
    )
)
annotate(fig, taus[0], blended[0], "  blended (what we use)", color=SERIES_ORANGE, shift=14)
annotate(fig, taus[-4], diffusion[-4], "diffusion only", color=SERIES_BLUE, shift=-16)
style(fig, x_title="Seconds to close (log scale)", y_title="Per-contract risk (ticks)")
fig.update_xaxes(type="log")
st.plotly_chart(fig, use_container_width=True)

st.caption(
    "Illustrative, at a \\$0.50 mid. The dotted line is the naive view: risk "
    "shrinks as the horizon shortens. The solid line blends in the probability "
    "that you cannot flatten at all, which is what actually happens near the "
    "bell. `MarketSnapshot.inventory_sigma_ticks` computes this."
)

st.divider()
st.subheader("Your end-of-market observation, generalised")

st.markdown(
    """
You noticed the last minutes of a 15-minute BTC market behave differently. You
were right, and it is worth being precise about *why* — because "minutes
remaining" does not port to a market that settles next Tuesday.

The clock is not the cause. Two things change together:

1. **σ spikes.** As expiry approaches the payoff steepens, so each tick of the
   underlying moves the probability further. The same BTC move that shifted the
   price 1c an hour ago shifts it 10c now.
2. **Time to flatten grows** while your ability to get out shrinks.

Both are measurable rather than calendar-based, which is what makes the control
portable: estimate σ from the book, blend inventory risk toward the binary
payoff as the close approaches, and the same parameters work on a 15-minute
crypto strike and a month-long election market.
"""
)

st.markdown(
    """
The strategy now moves through phases as the close approaches:

| phase | behaviour |
| --- | --- |
| normal | quote both sides, size ramped by available edge |
| taper | size shrinks as the chance of being stuck rises |
| reduce-only | only the side that reduces the position |
| flatten | cross the spread to get flat |

`seconds_to_close` is `None` whenever the close time is unknown — treated as
"no deadline", never as "closing now". An old recording must not make the bot
panic.
"""
)

st.error(
    "**Reduce-only had a nasty bug**, found by adversarial review. It restricted "
    "*direction* but sized off position *capacity*. With a long of 1 contract "
    "and a max position of 1000, 'reduce only' could sell 1000 and leave you 900 "
    "short — the largest position of the session, created by the risk control, "
    "in the final minutes.\n\n"
    "Risk controls need tests that try to break them, not tests that confirm "
    "them."
)

st.divider()
st.subheader("Checking the theory instead of assuming it")

st.markdown(
    """
`markout_by_time_to_close` buckets markout by time remaining. That turns the
impression into a number, and it will tell you exactly which window to stop
quoting in.
"""
)

st.code(
    """markout by time to close:
       final 30s     -12.40 ticks/contract  n=18
          30s-2m      -3.10 ticks/contract  n=52
           2m-5m       0.90 ticks/contract  n=61
          5m-15m       1.40 ticks/contract  n=44
  worst window is final 30s - consider stopping quoting there""",
    language="text",
)

st.caption(
    "Shape of the output, not real numbers — run "
    "`python scripts/analyze_session.py <recording>` against your own sessions."
)
