"""The tennis lead: a real edge indexed to a clock that does not exist live.

Every number comes from `data/tennis_study.json`, rebuilt by
`build_tennis_study.py`. Nothing here is typed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

DATA = Path(__file__).resolve().parents[1] / "data" / "tennis_study.json"
study = json.loads(DATA.read_text())
head = study["headline_t10"]
look = study.get("lookahead", {})
depth = study.get("depth", {})

st.title("The tennis lead, and the clock problem")
st.caption(
    "The one result in this project that replicated out of sample - and the "
    "reason it still cannot be traded as measured."
)
st.caption(f"Generated {study['generated_utc']} · "
           f"{study['provenance']['unique_markets']:,} unique markets")

st.header("What the measurement says")
st.markdown(
    f"""
Buy YES at the ask on any tennis market priced at or above 80c, ten minutes
before the match ends, hold to settlement:

* **{head['n']:,} entries** across {head['clusters']} series-day clusters,
  {head['days']} days, about {head['markets_per_day']} markets a day
* average ask **{head['avg_ask_cents']:.2f}c**, break-even loss rate
  **{head['breakeven_loss_rate'] * 100:.2f}%**, observed loss rate
  **{head['loss_rate'] * 100:.2f}%** ({head['losses']} losses)
* **{head['ev_cents']:+.2f}c per contract** ± {head['se_cents']:.2f}
  (t = {head['t']}), which is **{head['return_on_capital'] * 100:.1f}% return
  on capital** per ten-minute cycle

It also replicated on a clean split of the window, which nothing else in this
project has done. That is the strongest result here, and the rest of this page
is why it is not yet money.
"""
)

st.header("The clock problem")
st.markdown(
    """
"Ten minutes before the match ends" requires knowing when the match ends.
Kalshi's `close_time` on a **live** market is a scheduled placeholder. The real
match-end is only stamped at settlement, which is where the backtest read it.
"""
)

if look.get("live"):
    live = look["live"]
    st.error(
        f"**Measured from the live recorder's own rows.** Across "
        f"{live['observations']:,} observations of tradeable tennis markets, "
        f"the `close_time` the API was showing sat a median "
        f"**{live['median_days_ahead']:.1f} days** in the future "
        f"(range {live['min_days_ahead']:.1f}-{live['max_days_ahead']:.1f}). "
        f"Every T-minus-X figure in this study is indexed to a timestamp that "
        f"did not exist at the moment of the trade.",
        icon="🚨",
    )
    st.dataframe(pd.DataFrame(live["examples"]).rename(columns={
        "ticker": "market", "close_time_live": "close_time shown while trading",
        "observed_utc": "observed at"}), hide_index=True)

st.subheader("And price does not tell you where you are in the match")
st.markdown(
    """
The obvious hope is that the price level substitutes for the clock - that a 94c
favourite *is* a nearly-finished match. It does not. The same observable price
sits on both sides of zero depending on a horizon you cannot see. Rows are
observable live; **columns are not**.
"""
)

grid = study["price_horizon_grid"]
table = []

for row in grid:
    entry = {"mid at entry": row["price"]}

    for cell in row["cells"]:
        entry[f"T-{cell['horizon']}m"] = (
            "thin" if cell.get("thin")
            else f"{cell['ev_cents']:+.2f}c  (t {cell['t']:.0f})")

    table.append(entry)

st.dataframe(pd.DataFrame(table), hide_index=True)
st.caption("Cents per contract, buying at the ask and holding to settlement.")

st.header("What you could actually trade")
st.markdown(
    """
Drop the clock entirely and buy every actionable favourite while the market is
trading. That rule needs no knowledge of match-end, and it still pays - about
half the headline, and still several standard errors from zero.

The other rows are pre-specified proxies for "late in the match", tested to see
whether any observable substitutes for the clock. **None does**: the share of
entries falling in the profitable last-40-minute window barely moves. They are
reported because the failure is the finding.
"""
)

impl = pd.DataFrame(study["implementable"])
impl["share_last_40min"] = (impl["share_last_40min"] * 100).round(0)
impl["loss_rate"] = (impl["loss_rate"] * 100).round(2)
st.dataframe(impl[["rule", "n", "clusters", "loss_rate", "ev_cents",
                   "se_cents", "t", "share_last_40min"]].rename(columns={
    "rule": "live-observable rule", "n": "entries", "clusters": "clusters",
    "loss_rate": "loss %", "ev_cents": "EV (c)", "se_cents": "se (c)",
    "t": "t", "share_last_40min": "% in last 40 min"}), hide_index=True)

st.subheader("The horizon curve underneath it")
curve = pd.DataFrame(study["horizon_curve"])
curve["loss_rate"] = (curve["loss_rate"] * 100).round(2)
st.dataframe(curve[["horizon", "n", "clusters", "losses", "loss_rate",
                    "ev_cents", "se_cents", "t"]].rename(columns={
    "horizon": "minutes before end", "n": "entries", "clusters": "clusters",
    "losses": "losses", "loss_rate": "loss %", "ev_cents": "EV (c)",
    "se_cents": "se (c)", "t": "t"}), hide_index=True)
st.caption(
    "The blind rule is a blend of these. Its EV is the profitable early rows "
    "diluted by the last one, which is where most entries land.")

st.header("Depth is not the constraint - but it is not uniform")
st.markdown(
    """
Capacity was the expected blocker and it is not. Recorded live from the public
order book: contracts actually resting at the best ask, by series.
"""
)

if depth.get("by_series"):
    st.dataframe(pd.DataFrame(depth["by_series"]).rename(columns={
        "series": "series", "snapshots": "snapshots",
        "median_touch": "median at touch", "median_within_1c": "within 1c",
        "p10_touch": "p10 at touch"}), hide_index=True)
    st.caption(
        f"{depth['snapshots']:,} book snapshots across {depth['markets']} "
        f"markets, from the live recorder.")

st.warning(
    "**Read the p10 column, not the median.** The main tours rest tens of "
    "thousands of contracts at the touch, but ITF Futures - which is where "
    "most of the opportunities are - is thin in the tail, and the women's "
    "Futures book is an order of magnitude lighter than the men's. That is "
    "also the series with the highest favourite-loss rate below. Size the "
    "trade off the tenth percentile of the series you would actually be in, "
    "not off the exchange-wide median.",
    icon="⚠️",
)

st.header("Where the losses live")
tiers = pd.DataFrame(study["tiers"])
tiers["loss_rate"] = (tiers["loss_rate"] * 100).round(2)
st.dataframe(tiers.rename(columns={
    "tier": "tier", "markets": "markets", "losses": "losses",
    "loss_rate": "loss %", "ev_cents": "EV (c)"}), hide_index=True)
st.caption(
    "ITF Futures runs roughly double the favourite-loss rate of the tours. "
    "That is consistent with the integrity problems documented at that level, "
    "and it is NOT a licence to filter: picking the clean tiers off this same "
    "sample is the selection error this project keeps making. It is a "
    "hypothesis to pre-register and test on fresh data.")

st.header("The losses walk, they do not gap")
st.markdown(
    """
The favourites that lose slide down through tradeable prices rather than
jumping, so a stop is executable. Simulated on the recorded books, exiting at
the bid and paying the taker fee:
"""
)

stops = pd.DataFrame(study["stops"])
st.dataframe(stops.rename(columns={
    "stop": "stop at", "ev_cents": "EV (c)", "stopped_out": "stopped out",
    "stopped_that_would_have_won": "of which would have won",
    "worst_cents": "worst single (c)",
    "mean_loss_cents": "mean loss when losing (c)"}), hide_index=True)

st.caption(
    "A stop around 50-60c roughly halves the average loss at no cost to EV. "
    "Two caveats: the gain rests on a handful of events, and adding an exit "
    "reintroduces the exit-fill problem this project spent months proving is "
    "what kills strategies. The worst single outcome barely improves, because "
    "a few markets do gap with no tradeable price in between.")

st.header("What happens next")
st.markdown(
    """
A recorder is running against the public order book, capturing the **whole
life** of every quoting tennis market rather than the final ninety minutes the
candle data covered. It answers the one question that now decides this:

**What does the blind rule earn including the pre-match hours?** Markets open
roughly a day before the match. The candle data never saw that period, so the
+2c above is measured only over a window that itself required knowing the
close. If the pre-match quote is fairly priced, the blind rule is diluted; if
it is worse than fair, the rule goes negative.

Until that is measured, no money should be committed. The honest status is a
real, replicated, adequately deep edge whose entry condition nobody can yet
observe in time to act on it.
"""
)

with st.expander("Provenance"):
    st.json(study["provenance"])
