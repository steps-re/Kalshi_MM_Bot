"""The whole arc, measured: what we learned, and how Nate made his $1.50."""

from __future__ import annotations

import streamlit as st

st.title("Kalshi market-making: what we actually found")
st.caption(
    "Two days, one $50 account, ~$9 spent buying the map. Everything below is "
    "measured against the exchange's own ledger, not a backtest. For Nate."
)

st.header("The one-sentence version")
st.markdown(
    "**The edge is real and small; the way you exit decides whether you keep it.** "
    "Resting maker fills mark up ~0.3-0.5c and cost zero fees. Crossing the spread "
    "to get flat costs a cent-plus every time. Collect the first, avoid the second, "
    "and you make money. Do the reverse - which a naive always-on bot does by "
    "flattening every cycle - and positive markout turns into a negative account."
)

st.header("How Nate made ~$1.50 (and why our bot didn't, at first)")
st.markdown(
    """
The puzzle: Nate hand-tuned settings on BTC 15-minute markets, managed his queue
position carefully, and ended up **+$1.50 on a few dollars**. Our automated
version, with more infrastructure, bled money. There was no secret setting.

The ledger reconciles it in one line:

| fills | count | fees paid |
|---|---:|---:|
| **maker** (resting) | 1,621 | **$0.01** |
| **taker** (crossing) | 107 | **$1.02** |

Every taker fill is a cross - a fee **plus** the half-spread paid to get out.
Nate's "thoughtful queue management" was exactly the discipline of **not
crossing**: rest a buy, rest a sell, let both fill for free, capture the spread.
He also traded selectively and tiny, so he could decline the bad fills an
always-on bot takes automatically. He hit "fees ate everything" too - in the
episodes where he *did* cross. His net is the free round-trips minus those.

**He did this on BTC 15M only - and that confirms the mechanism rather than
contradicting it.** BTC15M is a middling book by our ranking (+0.36c live markout,
71% favourable) - not the best, but genuinely positive. A tiny, selective,
maker-only trader captures that. The commodity finding below means Nate left
better books on the table, not that his book was unprofitable: the same
discipline on WTI (+0.75c) or silver (+0.53c) would have paid roughly double for
the identical behaviour.

**We over-automated.** Our bot quoted continuously and then flattened every
cycle **by crossing** - mechanising the one action Nate's discretion avoided.
The fix, now live, exits passively (rest the flatten, wait, cross only the stub
that won't fill), turning his hand-judgment into a rule.
"""
)

st.header("What's true (measured, survived every correction)")
st.markdown(
    """
- **Makers pay nothing; takers pay `0.07 x N x P(1-P)`, rounded to $0.0001** (not
  the whole cent the docs claim). Model reproduces all 85 real charges exactly.
- **Live markout decays through a window:** +0.41c with >6 min left, ~0 late,
  negative in the last 1-2 min. The informed traders arrive near expiry.
- **The commodity 15-minute windows out-mark crypto 2-4x.** BTC/ETH are the
  thickest, most-competed books, so resting quotes there sit behind the most
  informed flow. Oil, silver, gold, doge are less picked-over.

| venue (live fills) | markout | in favour |
|---|---:|---:|
| WTI 15M (oil) | +0.75c | 75% |
| SILVER 15M | +0.53c | 79% |
| SOL 15M | +0.55c | 79% |
| GOLD 15M | +0.39c | 67% |
| BTC 15M | +0.36c | 71% |
| **ETH 15M** | **-0.52c** | 56% |

We had been anchored to BTC/ETH - the two *worst* books on the list.
"""
)

st.header("What's false (killed by measurement)")
st.markdown(
    """
- **No lead-lag anywhere.** Spot doesn't lead the book; BTC doesn't lead ETH;
  Kalshi and Polymarket don't lead each other. Three dead hypotheses = these
  markets price their underlying contemporaneously.
- **Settlement lock-in is already crowded.** The final minute is priced
  correctly to within measurement noise - the informed late takers *are* the
  lock-in arithmetic, and their profit is the resting maker's loss.
- **The simulator can't see adverse selection.** It filled orders resting behind
  the touch (71% of them, at prices the market never reached), so its markout
  ran 2.4x live. Fixed to require reachability; residual ~0.25c/fill is the
  adverse-selection haircut, stated on every report.
"""
)

st.header("The bugs that taught the most")
st.markdown(
    """
Five significant corrections were defects in *our own instruments*, every one
found by checking output against an independent source rather than reading code,
and every one flattered the result until caught:

1. A fee reader defaulting a renamed field to `0` - made 48 charged taker fills
   read as free.
2. Polled data carried 11.8% of true book activity - 942 simulated fills became
   13 on the same window.
3. A resolution metric that judged mixed recordings on their average, hiding the
   good data.
4. A four-bug chain in the collector that recorded the wrong half of every window.
5. The simulator filling orders that couldn't trade.

The transferable lesson: **enumerate what must fail closed; check output against
an independent source; a small sample always flatters.**
"""
)

st.header("Where the coffee fund stands")
st.markdown(
    """
$50 -> ~$41. Most of the drop bought the map (deliberate worst-regime
experiments, the venue census, the flatten diagnosis). The strategy in its
*corrected* form - commodity books, passive exits, phase-gated - has only just
started its first clean run. The honest answer to "$50 -> $85" is: **unknown,
and now measurable.** If passive exits let the positive commodity markout reach
the account, it's a question of patience at ~$1-4/day. If not, the real money is
Polymarket's maker-rewards pool - which pays makers to do exactly this - and
that's gated on jurisdiction, not on anything we can code.
"""
)
