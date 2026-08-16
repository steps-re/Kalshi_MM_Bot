"""How a backtest reports profit that live trading cannot produce."""

from __future__ import annotations

import streamlit as st

st.title("Why the backtest could not have caught this")

st.markdown(
    """
The simulator computed each fill as

```python
signed_cash = -direction * fill.yes_price * fill.count
```

and stopped. **No fee, anywhere.** `fees_paid` was parsed off the wire in
`api/parser.py` and never read.

So every backtest reported gross P&L while every live fill paid. That alone
would be a bad day. The optimizer made it worse.
"""
)

st.error(
    "**The optimizer was selecting for the most expensive configuration.** "
    "`_trial_sort_key` ranked trials by objective, then broke ties on "
    "`volume_count`. Among parameter sets with equal fee-free P&L it preferred "
    "the one that traded most — and every extra round trip is another unmodelled "
    "fee plus another chance to be adversely selected."
)

st.markdown(
    """
This is the most portable lesson here:

> A backtest optimises whatever you measure, so anything you do not measure gets
> spent freely. **An unmodelled cost is not neutral** — the search will find it
> and pour your money into it.

Fixed: cash is net of fees, the default objective is net liquidation, and ties
break toward ending flat rather than toward trading more.
"""
)

st.divider()
st.subheader("Two more ways the P&L flattered itself")

left, right = st.columns(2)

with left:
    st.markdown(
        """
**Inventory was marked at mid.**

If a session ends holding 100 contracts, mid is the price at which nobody has
agreed to trade with you.

`liquidation_value` now walks the actual book — 100 contracts against 1 contract
on the bid do not all get the bid — and charges the exit fee. The difference is
reported as `inventory_mark_gap`.
"""
    )

with right:
    st.markdown(
        """
**Unfillable positions vanished.**

A position the book could not absorb was silently dropped from the valuation.
For a short that erases the liability entirely and reports the account as richer
than it is.

Unfillable inventory is now marked at the worst case: zero for a long, a dollar
for a short.
"""
    )

st.divider()
st.subheader("Optimising on one recording finds noise")

st.markdown(
    """
`optimize_adaptive_backtest` searched up to 250 parameter combinations against a
**single** recording and returned the best one.

On one ten-minute session, the best of 250 is mostly a description of that
recording's noise. You would get an impressive-looking winner out of a shuffled
P&L column too.

The fix is boring and non-negotiable: **fit on some data, score on data the fit
never saw.** `sim/validation.py` runs expanding-window walk-forward and reports
both numbers side by side. The gap between them is the honest estimate of how
much was real.

It also defaults to picking the parameter set with the best **worst** recording
rather than the best total — a set that made everything back on one lucky replay
is precisely what we are trying not to ship.
"""
)

st.code(
    "python scripts/analyze_session.py recordings/* --walk-forward",
    language="bash",
)

st.info(
    "If out-of-sample retention is near zero, there is no edge yet and more "
    "tuning will not create one."
)
