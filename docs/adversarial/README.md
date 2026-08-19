# Adversarial review, 2026-08-19

Four hostile reviews of the corrected analysis, run on Gemini via Vertex credits
rather than premium tokens. `claims.md` is the briefing they were given; each
`out-*.txt` is one reviewer told to destroy the analysis and paid nothing for
agreeing.

They converged on two attacks, and both were then tested against the data.

## Attack 1: the exit assumes a guaranteed passive fill - UPHELD

All four flagged it. The audited `touch` convention marks the exit at the touch,
which assumes a resting order fills. It may not.

Tested three exit conventions side by side on both corpora. Pooled across
everything, at the strongest imbalance:

    touch  (rested exit fills)   -0.014c   about break-even
    cross  (never fills, pay out) -2.193c   deeply negative

So the reviewers are right that everything hinges on the passive fill. Live, the
first completed trade rested for 40 seconds, did not fill, and had to cross,
costing 2c - the CROSS branch. **The passive-exit fill rate is now the single
number that decides whether this is tradeable**, and the live test measures it.

## Attack 2: the dose-response is a mechanical artifact - REFUTED

Three reviewers argued the gradient comes from book geometry: at |OBI|>0.9 the
crossing side is thin, so buying the ask and marking the exit at the ask 30s
later profits when that thin level is consumed, with no forecast involved. The
proposed test was to re-run on pure mid-to-mid returns, where the prediction was
that the gradient would "flatten into noise around negative taker fees".

It does not flatten. Mid-to-mid has no spread capture at all:

    control  +0.013c        obi>.9  +0.861c     (recovered, 641 markets)
    control  -0.025c        obi>.9  +0.587c     (archive, 436 markets)

Two further checks, both proposed by the reviewers:

* **Volatility regime.** Stratified by spread, the gradient is the same at 1c
  (-0.005c to +1.067c) and 2c (-0.035c to +0.887c). Not a regime effect.
* **Fragility.** Share of individual markets with a positive forecast rises
  51%, 63%, 72%, 78%, **88%** across the bands, and the median tracks the mean.
  The control at 51% is a coin flip. Not carried by a few trending markets.

A signed mean cannot be produced by volatility, which is symmetric, and 88% of
641 independent markets is not book geometry.

## Where that leaves it

The signal is real, large, broad-based and robust to every attack made on it.
Monetising it is a separate question, and on current evidence the round trip
costs more than the forecast is worth except where fees are near zero. That is
the 2-5c near-expiry cell, and whether even that pays depends on the passive
exit filling.


---

# Round 2, 2026-08-19

Two more attacks on the surviving claims. One refuted, one was a real error in
my own arithmetic and it flipped the verdict.

## Refuted: "mid-to-mid is the thin side widening away"

The argument: at extreme imbalance you consume the thin side, market makers pull
back, the spread widens, the mid rises - and none of that is sellable because
the bid never moved. Test: decompose the signed 30s move into the side we cross
(thin) and the side that must come to us (thick).

    band       thin side   THICK side
    control      +0.046c      -0.020c
    obi>.9       +0.943c      +0.779c

The thick side lifts +0.800c from control to extreme. Spread widening accounts
for about 0.16c of the 0.86c. The bid genuinely rises after a bid-heavy signal,
which is exactly what has to happen for the trade to be sellable.

## Upheld, and it flipped the answer: the fills are adversely selected

The break-even framing multiplied the passive fill rate by the average P&L of
ALL trades. That assumes the trades that fill are a random sample of them. They
are not: **you fill precisely when the market comes to you**, so fills are
concentrated in the trades that went wrong.

`scripts/exit_fill_study.py` replays every qualifying trigger against the book
that followed it and lets each take the path it would really have taken:

    ORIGINAL ARCHIVE, 1,022 triggers
    hold  rest   exits   filled passively   realised
     15s   90s     726          57%          -0.412c
     30s   90s     678          51%          -0.405c
     60s   45s     722          40%          -0.247c
     60s   90s     586          49%          -0.017c

    RECOVERED CORPUS, 212 triggers: -0.52c to -0.77c throughout

Fill rates do rise with a longer rest window, and several configurations clear
the 42% threshold. **The realised expectancy is negative anyway.** A 57% fill
rate still loses 0.412c, because the 57% that fill are the wrong 57%.

These are also the optimistic bound - a fill is counted whenever a counterparty
reached our price, ignoring the queue ahead of us - so the truth is worse.

## Verdict

The signal is real: +0.86c of directional forecast at extreme imbalance against
a control of +0.013c, monotone across 640 markets, with the thick side carrying
+0.78c of it. The round trip is not tradeable: every hold and rest combination
realises a loss once fills are priced conditionally.

Live confirmation, 13 real orders and 33 cents: entries filled at the displayed
touch every time, and realised cash ran -1.09c per completed trade, in the same
negative territory the study predicts.
