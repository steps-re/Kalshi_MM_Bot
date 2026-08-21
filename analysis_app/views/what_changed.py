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

st.caption(
    "The original five-step list is done and has been retired: the fee model "
    "was calibrated against real fills, the recorder was pointed at the viable "
    "markets, markout was read before P&L, the walk-forward was run, and the "
    "strategy went live small. All of it is on the earlier pages. This is "
    "where the project actually stands."
)

st.markdown(
    """
1. **Measure the pre-match hours.** A recorder is capturing the whole life of
   every quoting tennis market. The one surviving lead pays +2.12c per entry
   over the final ninety minutes, but that window was defined by a close time
   nobody can see live, and the hours before it have never been priced. This
   decides the lead. See **The tennis lead**.
2. **Do not commit money until step 1 lands.** Depth is not the blocker -
   the books are deep enough - and the edge replicated out of sample. The
   blocker is that the entry condition is not observable in time to act on it.
3. **Pre-register the tier hypothesis before testing it.** ITF Futures runs
   about double the favourite-loss rate of the tours, and its women's book is
   an order of magnitude thinner. Picking the clean tiers off the same sample
   that suggested them is the selection error this project keeps making.
4. **If step 1 clears, test size live**: 25-contract orders against the
   displayed ask, with the kill rule fixed in advance - stop if the median
   fills worse than 1.5c from the touch.
5. **Build the uncertainty-fraction conditioner.** "Five minutes before close"
   is a third of a crypto window and the final game of a tennis match. Until
   horizons are expressed as fraction-of-event-remaining, no cross-family
   comparison on any of these pages means what it appears to mean.
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
python scripts/live_trade.py TICKER --strategy horizon

# the open question: record whole tennis market lives, public book, no key
python scripts/tennis_depth_recorder.py --out ~/kalshi-audit/tennis_book.jsonl

# rebuild the study pages from the corpus
python analysis_app/build_exchange_census.py --history ... --candles ...
python analysis_app/build_tennis_study.py --candles ... --book ...""",
    language="bash",
)
