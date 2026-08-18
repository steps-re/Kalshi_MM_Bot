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
    r"""**Crossing the spread to flatten was a measured, provable loss; removing it
stopped the bleed but has not yet proven a profit.** Over 9 cycles the
flatten-by-crossing bot lost \$0.31 a cycle (t = -2.4, real, not noise).
Switching to free passive exits moved that to +\$0.09 a cycle over the next
4 cycles - but that is statistically indistinguishable from zero (t = +0.2), and
a single lucky +\$1.32 cycle carries it: the other three corrected cycles sum
-\$0.95 on still-positive markout. What remains after fees is adverse selection,
and we have not shown it nets positive. The honest status is: we removed a proven
loser, we have not yet proven a winner."""
)

st.header("How Nate made ~$1.50 (and why our bot didn't, at first)")
st.markdown(
    r"""
The puzzle: Nate ran an **automated** bot on BTC 15-minute markets, tuned its
settings, and ended up **+\$1.50 on a few dollars**. Our automated version, with
more infrastructure, bled money on the same book. There was no secret setting.

The ledger reconciles it in one line:

| fills | count | fees paid |
|---|---:|---:|
| **maker** (resting) | 1,621 | **$0.01** |
| **taker** (crossing) | 107 | **$1.02** |

Every taker fill is a cross - a fee **plus** the half-spread paid to get out.
The profitable behaviour is to **not cross**: rest a buy, rest a sell, let both
fill for free, capture the spread. His tuned bot did that; ours forced a cross
every cycle to flatten. He hit "fees ate everything" too - in the episodes where
his settings did cross. His net is the free round-trips minus those.

**He did this on BTC 15M only, and he AUTOMATED it** - which removes the last
hand-wave. It was never human-discretion vs bot. Ours lost on the same book for
one concrete reason: it **flattened every cycle by crossing**, paying the taker
fee plus half-spread to exit what Nate's bot let fill for free. Nate simply never
wrote that line.

Removing it was necessary but not sufficient. Passive exits stopped the *proven*
bleed (the crossing bot lost at t = -2.4, real), but the account is still not
demonstrably positive - see the live scoreboard. The residual leak after fees is
**adverse selection**: we fill at roughly a 10% rate with 90% of quotes
cancelled, so the fills we get are disproportionately the ones an informed trader
crossed into, and a short-horizon markout only partly sees the move that follows.
Nate's ~$1.50 is consistent with this - a small, real, variance-dominated edge a
patient automated bot on one book can bank over enough cycles, and that a bot
paying to cross cannot. We removed the reason we lost. Proving we win is a
question of many more cycles, not one lucky one: distinguishing a \$0.15/cycle
edge from zero takes ~250 cycles, because the per-cycle swing (~\$0.84) dwarfs it.
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

We had been anchored to BTC/ETH. BTC is fine - middling but positive, the book
Nate used. ETH is the one genuine loser. Small and positive is the goal, not the
top of the list.
"""
)

st.header("The paradigm, applied to every market we considered")
st.markdown(
    """
The passive-exit insight is a **screen**, not a tweak. A resting-quote MM keeps
its edge only where **both** hold: (1) you can rest an exit and it fills before
you must be flat - needs continuous two-sided flow and time; (2) when a cross is
unavoidable it is cheap - the taker fee `0.07 x P(1-P)` peaks at mid-price 0.50
and vanishes in the tails.

Measured cross-rate x cost per book (ledger):

| book | forced-cross rate | fee per fill |
|---|---:|---:|
| BTC15M | 9% | 0.107c |
| ETH15M | 5% | 0.036c |
| commodities | 2-5% | 0.004-0.031c |
| WTI15M | 2% | 0.004c |

The crypto majors cross **most often and most expensively** - they live near
0.50. This is the same fact as their worse markout, seen through cost.

**Full circle:** day one said "quote near an end, where the fee is cheap." The
maker-free discovery seemed to retire that. The paradigm brings it back for a
subtler reason - price decides the cost of the *unavoidable* crosses, not the
entries. Re-scoring the shelved markets:

- **Hourly strike ladders** - the two conditions are anti-correlated across a
  ladder (deep strikes are cheap to cross but never trade; the ATM strike trades
  but is near 0.50). A single 15M window that trades AND drifts to the tail as
  it resolves beats a ladder structurally.
- **In-play sports / esports** - fail both at once: lumpy directional flow (no
  resting exit) and near-0.50 during a close game (expensive crosses).
- **News / political** - the book gaps on news; you can't rest an exit through a
  jump.
- **The 15M commodity family** - the sweet spot: continuous flow + tail-drift =
  free exits, confirmed twice.
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
