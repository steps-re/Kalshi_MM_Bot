"""The overview: how big the market is, what is addressable, what it could pay."""

from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from kalshi_mm_bot.market.fees import KalshiFeeModel
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR

from theme import GRIDLINE, INK_MUTED, SERIES_BLUE, SERIES_ORANGE, annotate, style

DATA = json.loads((Path(__file__).resolve().parents[1] / "data" / "scan.json").read_text())
CAP = DATA["capacity"]
MARKETS = DATA["markets_compact"]  # [mid_ticks, capturable_ticks, contracts_24h]

# Two fee worlds. Which one is real is the open question, and it is worth more
# than every parameter in the strategy combined.
PESSIMISTIC = KalshiFeeModel()
MAKER_FREE = KalshiFeeModel(charge_makers_taker_rate=False, maker_fee_per_contract_micros=0)
SIZE = 100 * COUNT_SCALE

st.title("The opportunity, in numbers")
st.caption(
    f"Every open Kalshi market, scanned {DATA['scanned_at_utc'][:10]}. "
    "Every figure on this page is computed from the fee model at render time."
)


@st.cache_data
def model(
    fee_bps: int,
    charge_makers: bool,
    capture: float,
    share: float,
    cap_ticks: int,
) -> dict:
    """Fee-adjusted edge across the exchange under explicit assumptions.

    `capture` is the fraction of the quoted spread we actually keep once
    informed traders have picked off the rest. It is the single biggest unknown
    and the reason this is a model rather than a forecast.

    `cap_ticks` bounds how much edge a single round trip can realistically
    harvest. Without it the estimate is dominated by markets quoted 10-20c wide
    - and those are wide precisely because nobody is quoting them. A maker who
    steps in to get filled narrows the spread by doing so, and competitors
    arrive. The cap is a crude stand-in for that compression, and it changes the
    answer by roughly 5x, so it is a slider rather than a constant.
    """

    fee_model = (
        KalshiFeeModel(trading_fee_bps=fee_bps)
        if charge_makers
        else KalshiFeeModel(charge_makers_taker_rate=False, maker_fee_per_contract_micros=0)
    )

    per_market: list[float] = []
    viable = 0
    contracts = 0
    notional = 0.0

    for mid, capturable, volume in MARKETS:
        kept = min(capturable * capture, cap_ticks)
        fee = fee_model.breakeven_edge_ticks(yes_price=mid, count=SIZE)
        net = kept - fee

        if net <= 0:
            continue

        viable += 1
        contracts += volume
        notional += volume * mid / ONE_DOLLAR
        # net is in price ticks; a round trip consumes two contracts of volume.
        per_market.append(net / ONE_DOLLAR * (volume * share / 2))

    per_market.sort(reverse=True)
    daily = sum(per_market)

    return {
        "daily": daily,
        "viable": viable,
        "contracts": contracts,
        "notional": notional,
        "top50_share": (sum(per_market[:50]) / daily) if daily > 0 else 0.0,
    }


# ---- headline ------------------------------------------------------------

st.subheader("How big is the market?")

a, b, c = st.columns(3)
a.metric("Open markets", f"{DATA['real_markets']:,}")
b.metric("Liquid enough to quote", f"{DATA['liquid_markets']:,}")
c.metric("Traded per day", f"${DATA['notional_per_day'] / 1e6:,.1f}M")

st.markdown(
    f"""
Kalshi turns over **\\${DATA['notional_per_day'] / 1e6:,.1f}M of notional a day**
across the {DATA['liquid_markets']:,} markets that have two-sided quotes and real
volume — roughly **\\${DATA['notional_per_day'] * 365 / 1e9:,.1f}B a year**. That is
the whole pond. Polymarket, the only comparable venue with open data, turns over
about the same.

Market making does not earn the notional. It earns a slice of the *spread* on
the fraction of that flow it intermediates, minus fees. The rest of this page is
about how big that slice plausibly is.
"""
)

st.divider()

# ---- the model -----------------------------------------------------------

st.subheader("What could it pay?")

st.markdown(
    "Three assumptions drive the answer. None of them are known yet, so set them "
    "yourself rather than trusting a single headline number."
)

left, right = st.columns(2)

with left:
    share_pct = st.slider(
        "Share of market volume we intermediate (%)",
        min_value=0.5,
        max_value=25.0,
        value=5.0,
        step=0.5,
        help="A dominant maker in a niche might reach 20-30%. A new entrant is "
        "closer to 1%.",
    )
with right:
    capture_pct = st.slider(
        "Share of the quoted spread we actually keep (%)",
        min_value=5,
        max_value=100,
        value=30,
        step=5,
        help="100% assumes nobody ever picks you off. This is what markout "
        "measures, and it is the number to go find out first.",
    )

cap_cents = st.slider(
    "Most edge one round trip can realistically harvest (cents)",
    min_value=1,
    max_value=10,
    value=2,
    help="Without a cap the estimate is dominated by markets quoted 10-20c "
    "wide, which are wide because nobody quotes them. Stepping in to get "
    "filled narrows the spread and draws competitors. This is the crudest "
    "assumption here and it moves the answer about 5x.",
)

fee_world = st.radio(
    "Fee schedule",
    options=["Maker pays nothing (Kalshi's published standard)", "Maker pays the taker rate (worst case)"],
    horizontal=False,
    help="Kalshi charges takers, and public summaries of its schedule say most "
    "standard markets carry no maker fee. Which applies to this account is "
    "unresolved and decides most of this page.",
)
charge_makers = fee_world.startswith("Maker pays the taker")

result = model(700, charge_makers, capture_pct / 100, share_pct / 100, cap_cents * 100)

d, m, y = st.columns(3)
d.metric("Per day", f"${result['daily']:,.0f}")
m.metric("Per month", f"${result['daily'] * 30:,.0f}")
y.metric("Per year", f"${result['daily'] * 365:,.0f}")

st.caption(
    f"Across {result['viable']:,} markets that clear their fee under these "
    f"assumptions, intermediating {result['contracts'] * share_pct / 100:,.0f} "
    f"contracts a day "
    f"(${result['notional'] * share_pct / 100:,.0f} of notional)."
)

if result["top50_share"] > 0.4:
    st.warning(
        f"**Concentrated: {result['top50_share']:.0%} of that comes from just 50 "
        "markets.** Those are the widest-spread names, which is where the "
        "assumptions are least reliable and where a competitor arriving hurts "
        "most. A plan that depends on 50 specific markets is a fragile plan."
    )

if result["daily"] < 50:
    st.error(
        "At these assumptions the strategy does not clear the cost of running "
        "it, let alone the cost of the attention it takes."
    )
elif result["daily"] < 500:
    st.warning(
        "A real but small number. Viable as a research project and a skill "
        "builder; not a business on its own."
    )
else:
    st.success(
        "A meaningful number — but read the sensitivity below before believing "
        "it, because it depends hardest on the assumption we have not measured."
    )

st.divider()

# ---- sensitivity ---------------------------------------------------------

st.subheader("What the answer actually hinges on")

captures = [5, 10, 20, 30, 40, 50, 75, 100]
free_curve = [model(700, False, c / 100, share_pct / 100, cap_cents * 100)["daily"] for c in captures]
paid_curve = [model(700, True, c / 100, share_pct / 100, cap_cents * 100)["daily"] for c in captures]

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=captures,
        y=free_curve,
        mode="lines+markers",
        line=dict(color=SERIES_BLUE, width=2),
        marker=dict(size=8),
        hovertemplate="keep %{x}% of spread<br>$%{y:,.0f}/day<extra></extra>",
    )
)
fig.add_trace(
    go.Scatter(
        x=captures,
        y=paid_curve,
        mode="lines+markers",
        line=dict(color=SERIES_ORANGE, width=2, dash="dot"),
        marker=dict(size=8),
        hovertemplate="keep %{x}% of spread<br>$%{y:,.0f}/day<extra></extra>",
    )
)
annotate(fig, captures[3], free_curve[3], "  maker pays nothing", color=SERIES_BLUE, shift=16)
annotate(fig, captures[3], paid_curve[3], "  maker pays taker rate", color=SERIES_ORANGE, shift=-16)
fig.add_vline(x=capture_pct, line=dict(color=GRIDLINE, width=1, dash="dot"))
annotate(fig, capture_pct, max(free_curve), "your setting", color=INK_MUTED, shift=6)
style(fig, x_title="Share of quoted spread kept (%)", y_title=f"$/day at {share_pct:.1f}% participation")
st.plotly_chart(fig, use_container_width=True)

gap_free = model(700, False, capture_pct / 100, share_pct / 100, cap_cents * 100)["daily"]
gap_paid = model(700, True, capture_pct / 100, share_pct / 100, cap_cents * 100)["daily"]
ratio = (gap_free / gap_paid) if gap_paid > 0 else None

st.markdown(
    f"""
The gap between those two lines is **the fee question**, and at your current
settings it is worth
{"about " + format(ratio, ".1f") + "x" if ratio else "the entire opportunity"}.
It is not a modelling detail — it is the largest single lever on this page, and
it is settled by one session of real fills, not by argument.

The slope along the x-axis is **adverse selection**. Nothing in the strategy
config moves it much; it is a property of who trades against you and how fast
you can pull. Markout measures it directly.
"""
)

st.divider()

# ---- honesty -------------------------------------------------------------

st.subheader("What this model does not know")

st.markdown(
    """
| Assumption | Status | How to resolve it |
| --- | --- | --- |
| Maker fee schedule | **Unknown** — worth several times everything else | `calibrate_from_fills` against real executions. One session. |
| Spread actually captured | **Unknown** — the x-axis above | Record the viable markets, run markout by horizon. |
| Participation share | Guess | Falls out of live fills versus market volume. |
| Queue position | Not modelled | Joining the touch fills rarely; the backtest's queue-aware fill model brackets it. |
| Competition | Crudely — the edge cap | Watch whether the wide-spread names stay wide once we quote them. |

Two of the five are measurable this week with tooling that already exists in the
repo. Until then, treat every number on this page as a **bracket, not a
forecast** — which is exactly why the assumptions are sliders instead of prose.
"""
)

st.info(
    "**The honest summary for a review.** The fee arithmetic says a real "
    "addressable slice exists and identifies exactly which markets it is in. "
    "Whether it is a hobby-scale or business-scale number turns on two "
    "measurements nobody has taken yet. The next move is not more strategy "
    "work — it is the two measurements."
)
