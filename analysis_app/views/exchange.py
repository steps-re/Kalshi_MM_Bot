"""What the live exchange actually offers, from a full scan."""

from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from theme import SERIES_BLUE, style

DATA = json.loads((Path(__file__).resolve().parents[1] / "data" / "scan.json").read_text())

FAMILY_LABELS = {
    "KXMLBHIT": "MLB hits",
    "KXMLBHR": "MLB home runs",
    "KXMLBHRR": "MLB HR/runs",
    "KXMLBTB": "MLB total bases",
    "KXTEMPDCH": "Temp - DC",
    "KXTEMPMIAH": "Temp - Miami",
    "KXTEMPLAXH": "Temp - LA",
    "KXTEMPAUSH": "Temp - Austin",
    "KXTEMPNYCH": "Temp - NYC",
    "KXCLUBFTOTAL": "Club friendly totals",
    "KXCLUBFSPREAD": "Club friendly spread",
    "KXLALIGATOTAL": "La Liga totals",
    "KXLALIGATEAMTOTAL": "La Liga team totals",
    "KXELITESERIENTOTAL": "Eliteserien totals",
}

st.title("What the exchange actually offers")
st.caption(f"Full scan of open markets, {DATA['scanned_at_utc'][:10]}")

a, b, c, d = st.columns(4)
a.metric("Real markets", f"{DATA['real_markets']:,}")
b.metric("Liquid & two-sided", f"{DATA['liquid_markets']:,}")
c.metric("Clear their own fee", f"{DATA['viable_markets']:,}")
d.metric("Total net edge", f"${DATA['total_daily_dollars']:,.0f}/day")

unviable = DATA["liquid_markets"] - DATA["viable_markets"]
share = unviable / DATA["liquid_markets"]

st.markdown(
    f"""
Of {DATA['records_scanned']:,} market records, **{DATA['combo_markets']:,} were
auto-generated multivariate parlay combos** — essentially none of them quoted.
Filter those out (`mve_collection_ticker`) or they crowd out every real market
in a scan.

That leaves {DATA['real_markets']:,} real markets, {DATA['liquid_markets']}
of which had two-sided quotes and 24h volume of 50+ contracts.
**{share:.0%} of those are structurally unviable** — the fee exceeds the whole
spread. The {DATA['viable_markets']} that clear it are worth an estimated
**\\${DATA['total_daily_dollars']:,.2f}/day of net edge in total**, assuming we
intermediate 10% of their volume.
"""
)

st.divider()
st.subheader("Where the viable edge is")

families = [row for row in DATA["families"] if row["daily"] > 0][:10]
labels = [FAMILY_LABELS.get(row["family"], row["family"]) for row in families]

fig = go.Figure(
    go.Bar(
        x=[row["daily"] for row in families],
        y=labels,
        orientation="h",
        marker=dict(color=SERIES_BLUE, line=dict(width=0)),
        text=[f"${row['daily']:,.2f}" for row in families],
        textposition="outside",
        hovertemplate="%{y}<br>$%{x:.2f}/day<extra></extra>",
    )
)
style(fig, height=380, x_title="Estimated net edge ($/day)")
fig.update_yaxes(autorange="reversed")
fig.update_xaxes(range=[0, max(row["daily"] for row in families) * 1.25])
st.plotly_chart(fig, use_container_width=True)

st.markdown(
    """
**Crypto hourly strikes do not appear at any volume threshold.** Tick-wide
spreads at midpoint prices is the single worst cell in the fee table, and it is
where the early live testing was pointed.
"""
)

st.divider()
st.subheader("Why so few survive: almost everything is quoted one tick wide")

hist = {int(k): v for k, v in DATA["spread_histogram"].items()}
common = {k: v for k, v in sorted(hist.items()) if k <= 10}
other = sum(v for k, v in hist.items() if k > 10)

xs = [f"{k}c" for k in common] + (["> 10c"] if other else [])
ys = list(common.values()) + ([other] if other else [])

fig = go.Figure(
    go.Bar(
        x=xs,
        y=ys,
        marker=dict(color=SERIES_BLUE, line=dict(width=0)),
        text=ys,
        textposition="outside",
        hovertemplate="%{x} spread<br>%{y} markets<extra></extra>",
    )
)
style(fig, height=300, x_title="Quoted spread", y_title="Markets")
st.plotly_chart(fig, use_container_width=True)

st.markdown(
    """
A 1c spread only clears the fee at or below \\$0.077 or at or above \\$0.923.
So a tick-wide market is quotable *only* if it is also priced deep in a tail —
which is why the survivors are a small intersection of two conditions rather
than a broad category.
"""
)

st.divider()
st.subheader("The markets that clear their fee")

table = [
    {
        "Ticker": row["ticker"],
        "Mid": f"${row['mid']:.2f}",
        "Spread": f"{row['spread_cents']:.1f}c",
        "Fee (round trip)": f"{row['fee_cents']:.2f}c",
        "Net edge": f"{row['net_cents']:.2f}c",
        "24h volume": f"{row['volume_24h']:,}",
        "Est. $/day": f"${row['daily_dollars']:,.2f}",
    }
    for row in DATA["top_markets"]
]
st.dataframe(table, hide_index=True)

st.warning(
    f"Read \\${DATA['total_daily_dollars']:,.0f}/day as an **upper bound, not a "
    "forecast**. It assumes capturing the quoted spread on every round trip, "
    "which is exactly what adverse selection erodes — see the markout page. "
    "This is a low-capacity game. That is not a reason not to play it, but it "
    "is a reason to know what you are playing before sizing up."
)

st.caption(
    "Reproduce: `python scripts/screen_markets.py --prod --min-volume 50`. "
    "The public market-data endpoint needs no auth."
)

st.divider()
st.subheader("Is $64/day a size limit? No.")

CAP = DATA["capacity"]

st.markdown(
    f"""
Order size does not enter this calculation at all. The estimate is

```
daily = net edge per contract  ×  round trips
      = net edge per contract  ×  (market volume × our share) / 2
```

Both terms are set by the market, not by our capital:

- **Volume in fee-viable markets: {CAP['viable_volume']:,} contracts/day** — out of
  {CAP['all_liquid_volume']:,} across all liquid markets. That is the whole pool.
- **Volume-weighted net edge: {CAP['weighted_net_edge_cents']:.2f}c per contract** — what
  survives the fee.

Trading bigger only helps insofar as it wins a larger *share* of a fixed pool,
and the share cannot exceed 100%.
"""
)

share_rows = [
    {
        "Share of volume we intermediate": f"{row['share']:.0%}",
        "Contracts/day we trade": f"{int(row['volume'] * row['share']):,}",
        "Net edge": f"${row['daily']:,.2f}/day",
    }
    for row in CAP["by_share"]
]
st.dataframe(share_rows, hide_index=True)

st.caption(
    "100% means being the counterparty to every single trade in every "
    "fee-viable market on the exchange. It is a ceiling, not a plan."
)

st.markdown(
    """
Nor is there a hidden pool further down: dropping the volume threshold from 50
contracts to zero adds 579 more viable markets and about **$3.50/day**. The long
tail is viable but empty.
"""
)

st.divider()
st.subheader("The lever that actually moves it")

sched_rows = [
    {
        "Fee schedule": row["label"],
        "Viable markets": row["viable"],
        "Addressable volume": f"{row['volume']:,}",
        "Net edge @10%": f"${row['daily']:,.2f}/day",
    }
    for row in CAP["by_schedule"]
]
st.dataframe(sched_rows, hide_index=True)

st.markdown(
    """
This is why the maker-fee question is not a detail. Under the taker schedule we
can address 18% of the liquid volume on the exchange. Under a flat maker fee
that rises to 52%, because the midpoint markets — where nearly all the volume
is — come back into play.

**Capital is not the constraint. The fee schedule is.** Which is why calibrating
it against real fills is the first thing to do, not the last.
"""
)
