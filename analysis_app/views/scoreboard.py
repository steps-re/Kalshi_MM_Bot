"""Live coffee scoreboard: is Nate's project paying for coffee yet?

Reads the small JSON the VM writes from the session log every few minutes. No
account access here - only the numbers the running session already reported.
"""

from __future__ import annotations

import json

import plotly.graph_objects as go
import streamlit as st

BUCKET = "steps-kalshi-book"
BLOB = "session/scoreboard.json"


@st.cache_data(ttl=60)
def load_board() -> dict | None:
    try:
        from google.cloud import storage

        client = storage.Client()
        data = client.bucket(BUCKET).blob(BLOB).download_as_text()
        return json.loads(data)
    except Exception as error:  # noqa: BLE001 - surface, never crash the page
        st.session_state["_board_error"] = f"{type(error).__name__}: {error}"
        return None


st.title("Coffee scoreboard")
st.caption(
    "Nate's market maker, running live. The goal is small and specific: earn back "
    "this summer's coffees, then a few weeks of Nate's future coffees. Updated "
    "every few minutes from the session's own ledger."
)

board = load_board()

if board is None:
    st.info(
        "No scoreboard yet. The session writes it from its log every few minutes - "
        "check back shortly."
    )
    err = st.session_state.get("_board_error")

    if err:
        st.caption(f"(reader said: {err})")
    st.stop()

balance = board.get("balance")
stake = board.get("original_stake", 50.0)
target = board.get("target_balance", 85.0)
earned = board.get("earned_since_fix", 0.0)

col1, col2, col3 = st.columns(3)
col1.metric("Account balance", f"\${balance:,.2f}" if balance is not None else "-",
            f"{balance - stake:+.2f} vs \${stake:.0f} stake" if balance is not None else None)
col2.metric("Earned since the fix", f"\${earned:+,.2f}",
            f"{board.get('cycles_won', 0)}/{board.get('cycles_total', 0)} cycles up")
col3.metric("Total fills", f"{board.get('fills_total', 0):,}")

# Progress from the $50 stake to the $85 coffee target. Below the stake we are
# still recovering; the bar reflects the whole journey.
if balance is not None:
    span = target - stake
    frac = max(0.0, min(1.0, (balance - stake) / span)) if span else 0.0
    st.progress(frac, text=f"\${balance:,.2f} of the \${target:,.0f} coffee target "
                           f"(\${stake:.0f} stake + \${board.get('coffee_goal', 35):.0f} of coffees)")

cycles = board.get("cycles", [])

if cycles:
    st.subheader("Balance, cycle by cycle")
    xs = list(range(1, len(cycles) + 1))
    ys = [c["balance"] for c in cycles]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", name="balance",
                             line={"width": 2}))
    fig.add_hline(y=stake, line_dash="dot", annotation_text="$50 stake")
    fig.add_hline(y=target, line_dash="dot", annotation_text="$85 target")
    fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      yaxis_title="balance ($)", xaxis_title="cycle (recent)")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Recent cycles")
    rows = [
        {
            "time": c["time"],
            "fills": c["fills"],
            "markout (c)": c["markout_cents"],
            "P&L ($)": c["pnl"],
            "balance ($)": c["balance"],
        }
        for c in reversed(cycles[-20:])
    ]
    st.dataframe(rows, hide_index=True)

st.caption(f"Updated {board.get('updated_at_utc', 'unknown')} UTC.")
