"""The whole arc, measured: what we learned, and how Nate made his $1.50."""

from __future__ import annotations

import streamlit as st

st.title("Kalshi market-making: what we actually found")
st.caption(
    "Two days, one \\$50 account, ~\\$9 spent buying the map. Everything below is "
    "measured against the exchange's own ledger, not a backtest. For Nate."
)

st.warning(
    "**Re-audited 2026-08-19 against the full 62-hour archive.** The verdict below "
    "held and is better supported than when it was written. Several numbers on "
    "this page did not survive, and are corrected on **The audit**. The one "
    "condition that changes the answer is on **Where the money might be**.",
    icon="🔬",
)

st.header("The one-sentence version")
st.markdown(
    r"""**On this exchange's fee schedule, nobody small keeps money by quoting or
by taking across the board.** We proved each piece in turn. Crossing to flatten
was a provable loss (-\$0.31/cycle, t = -2.4) - removed. Passive exits took the
strategy to +\$0.09/cycle - indistinguishable from zero over any horizon we could
afford. The order book's imbalance genuinely predicts the next move, and the move
is smaller than the fee to act on it: gross runs +0.06c to +0.65c per trade
against a taker fee of 0.36c to 0.56c in the price bands originally tested.

The re-audit changed two things worth knowing up front. The predictive slope is
**+0.224c per unit imbalance** once standard errors are clustered on the market
rather than the book update, not the +0.85c first published, and it is one-sided.
And the "no edge anywhere" result is a consequence of *pooling*: entries below 5
cents in the final quarter hour of a market clear costs, and one such slice
survived a pre-registered holdout. That is a lead, not a business."""
)

st.header("How Nate made ~\\$1.50 (and why our bot didn't, at first)")
st.markdown(
    r"""
The puzzle: Nate ran an **automated** bot on BTC 15-minute markets, tuned its
settings, and ended up **+\$1.50 on a few dollars**. Our automated version, with
more infrastructure, bled money on the same book. There was no secret setting.

The ledger reconciles it in one line:

| fills | count | fees paid |
|---|---:|---:|
| **maker** (resting) | 1,621 | **\$0.01** |
| **taker** (crossing) | 107 | **\$1.02** |

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
Nate's ~\$1.50 is consistent with this - a small, real, variance-dominated edge a
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

st.header("The ending: the sniper, and what the re-audit did to it")
st.markdown(
    r"""
The last live hypothesis was the **sniper**: don't quote at all, just cross the
spread on extreme order-book imbalance and exit passively for free. The taker
entry is deterministic on recorded books, so the entry half of that backtest is
exact in a way no maker backtest can be.

This was originally reported as closed by replication failure: winning slices on
one day, different winning slices the next, no slice ever repeating. **That
reasoning does not hold.** Re-run with the holdout's statistical power computed
before the verdict, five of eight pre-registered slices were *absent* from the
holdout entirely, and most of the rest could not have detected their own effect.
A backward control - freeze the holdout's own winners, test them on the original
period - comes back underpowered on every one. The periods were never able to
judge each other.

**The verdict survived anyway, and for a better reason.** Across the full
archive, no slice clears costs and a calibrated placebo never beats the best
observed one. What the original scan had was the right answer supported by the
wrong argument, and one important thing it never looked at: it pooled the cheap
tail into the expensive middle, and the final minutes of a market into its whole
life. Split both ways, slices do clear costs, and one survived a pre-registered
holdout. That is on **Where the money might be**, with the reasons it might still
be nothing.

The Nate reconciliation is unchanged and is the most durable thing here: his
bot's real achievement was **zero expected cost** (maker-only, one book, never a
forced cross). From there, +\$1.50 over a limited run is about a one-in-three
draw, preserved by stopping. The skill was the zero-cost build and the good sense
to pocket the draw.
"""
)

st.header("Where the coffee fund stands")
st.markdown(
    r"""
\$50 -> \$36.66, trading stopped by choice with the account above its \$35
floor. About \$13 bought the complete map: the fee schedule read off the ledger,
the maker-free discovery, the passive-exit fix, the venue census, the biased-mid
proof, and the three-period taker verdict above. The coffee target is not
reachable on this exchange, and knowing that for sure - with the toolchain to
re-ask the question on any venue in an evening - is what the \$13 bought. The
account keeps the rest.
"""
)
