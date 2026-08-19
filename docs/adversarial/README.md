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
