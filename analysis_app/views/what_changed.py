"""What changed in the fork, and what to do next."""

from __future__ import annotations

import streamlit as st

st.title("What changed, and what to do next")

st.markdown(
    """
The engineering in the original was good: disciplined fixed-point money, careful
websocket sequencing, self-orders removed from the book before the strategy sees
it, an order manager that namespaces its own orders and sweeps them on shutdown,
78 passing tests on a clean checkout.

The changes below are not corrections of sloppy work. They are the things that
are invisible from inside a backtest — which is where a good backtest is most
dangerous.
"""
)

st.subheader("New")

st.markdown(
    """
| Module | What it does |
| --- | --- |
| `market/fees.py` | The real fee schedule, with the per-order ceiling, breakeven-edge and minimum-viable-size helpers, and `calibrate_from_fills` |
| `market/dynamics.py` | Realized volatility, microprice, book imbalance, and the blend from diffusion risk to binary-payoff risk near expiry |
| `market/clock.py` | Time to close, plumbed through live and replay; unknown is `None` everywhere and never guessed |
| `strategy/horizon.py` | Strategy that prices adverse selection off measured volatility, ramps size with edge, and refuses orders whose edge cannot cover their own fee |
| `sim/validation.py` | Expanding-window walk-forward |
| `analytics/markout.py` | Markout by horizon and by time to close |
| `analytics/performance.py` | P&L attribution, drawdown, time underwater |
| `analytics/screening.py` | Which markets can pay their own fees |
| `live/risk.py` | Position, loss, drawdown, order-rate, rejection and feed-silence limits, with a latching kill switch |
"""
)

st.subheader("Changed")

st.markdown(
    """
- **Fees are charged.** Cash is net; `gross_*` fields show the difference.
- **Inventory is marked at what it could actually be sold into**, walking the book.
- **The optimizer maximises net liquidation** and breaks ties toward ending flat.
- **Quotes price off the reservation price** instead of pinning to the touch.
  The old `min(best_bid, target)` form silently disabled inventory skew and
  volatility widening whenever the book was wider than the required edge — which
  is most of the time. In a 40/60 book the strategy joined at 40 whether it was
  flat or at its position limit.
- **Tests: 78 → 193.**
"""
)

st.divider()
st.subheader("Habits worth stealing")

st.markdown(
    """
**Compute published numbers, never type them.** Every figure in this app comes
out of `market/fees.py` at render time. Typed numbers drift from the code the
moment either changes.

**Make the pessimistic assumption the default.** The fee model charges the taker
rate on every fill because that is the direction of error we want in a system
that decides whether to risk money. Optimism belongs behind a flag.

**Unknown must not mean zero.** The volatility window used to keep a minimum
number of samples regardless of age. After a feed gap those stale samples
spanned the whole outage, so σ collapsed toward zero — telling the strategy the
market was *calm* at the exact moment it had no idea what was happening, and
quoting the tightest spread of the session.

**Silence is the dangerous state.** Resting orders do not cancel themselves when
the websocket stalls. `max_feed_silence_seconds` is the most important limit in
`live/risk.py` and the one nobody thinks to add.

**Test the invariant, not the implementation.** Several of my first tests
asserted "the strategy refuses to quote here" and failed — because it quoted
*wider* instead, which is better. The right assertion was economic: the two
quotes together must be at least the round-trip fee apart. Tests that encode
behaviour break when behaviour improves; tests that encode invariants do not.

**Get someone else to attack it.** Two other models reviewed this code cold.
Eight findings held up, including the reduce-only reversal. I also found two
they missed by checking my own claims against the live exchange — one of which
had me stating the wrong conclusion for an hour. Reviewing your own code has a
ceiling.
"""
)

st.divider()
st.subheader("What I would do next, in order")

st.markdown(
    """
1. **Calibrate the fee model** against real fills. One session. It decides
   whether any of the rest matters.
2. **Point the recorder at the viable markets** — MLB props and temperature, not
   crypto strikes — and collect several sessions.
3. **Read the markout before the P&L.** If markout is negative at every horizon,
   fix that before touching anything else.
4. **Walk-forward** across the sessions. Near-zero out-of-sample retention means
   there is no edge yet, and more tuning will not create one.
5. Only then go live, small, with `RiskLimits.conservative()`.
"""
)

st.info(
    "And always compare against `dumb`. A strategy that has not beaten a "
    "baseline has not been shown to do anything."
)

st.code(
    """# which markets can pay their fees
python scripts/screen_markets.py --prod --min-volume 50

# record the promising ones (captures close times)
python scripts/record_markets.py --prod TICKER --duration-sec 3600

# what happened, and was it edge or luck
python scripts/analyze_session.py recordings/<session>

# does it survive on data it was not fitted to
python scripts/analyze_session.py recordings/* --walk-forward

# dry run against live data, no orders sent
python scripts/live_trade.py TICKER --strategy horizon""",
    language="bash",
)
