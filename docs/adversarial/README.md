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

## Upheld: the fills are adversely selected. Corrected: it does not kill it

The break-even framing multiplied the passive fill rate by the average P&L of
ALL trades. That assumes the trades that fill are a random sample of them. They
are not: **you fill precisely when the market comes to you**, so fills are
concentrated in the trades that went wrong.

`scripts/exit_fill_study.py` replays every qualifying trigger against the book
that followed it and lets each take the path it would really have taken.

**Corrected 2026-08-20, and the correction flips this section back.** The first
version's fill detector required the best BID to rise to our resting ASK, and
called that the optimistic bound. It is not a bound in either direction. The
ordinary way a resting sell fills is a marketable buy consuming the ask level,
after which the book re-quotes with the bid still below us - a real fill that
the detector scored as a forced cross, complete with an exit fee that would
never have been paid. Every missed fill was re-priced as the bad outcome, so the
realised expectancy was pushed down, toward the conclusion the script was
written to test. "So the truth is worse" had the sign of its own error
backwards.

The detector now brackets from both sides. `optimistic` fires if the level was
consumed, cleared, or traded through, which includes cancellations and is a
genuine upper bound. `conservative` requires the level to have emptied AND the
market to have traded through our price. Also fixed: every (hold, rest) cell now
runs on ONE shared trigger set instead of nine differently-censored ones, no
window is allowed to run past the market's close, the exit book is read with the
no-lookahead convention, and every mean is clustered on the market ticker.

    ORIGINAL ARCHIVE, 598 triggers across 84 markets, one shared set

    hold  rest   optimistic  conservative   realised if opt.   if conservative
     15s   20s      93%          37%       +0.559c +/-0.138   -0.375c +/-0.143
     15s   90s      97%          60%       +0.603c +/-0.140   -0.270c +/-0.151
     30s   45s      93%          47%       +0.738c +/-0.194   -0.237c +/-0.196
     60s   45s      92%          40%       +1.022c +/-0.296   -0.054c +/-0.310
     60s   90s      95%          49%       +1.054c +/-0.296   -0.034c +/-0.304

**The round trip is not dead. It is bracketed and undecided.** At the upper
bound it earns +0.6c to +1.1c per trade at t of roughly 4. At the conservative
bound it is between -0.4c and zero. The truth is in between, and nothing in
recorded book data can narrow it further, because snapshots cannot see trades.
That is what the script's own design note said it would find, before the
detector bug buried it.

**The direction split is the most interesting thing in the archive run.** Taking `sign = +1 if
obi > 0 else -1` puts roughly half the trades on the ask-heavy side, where the
audit measured the signal at +0.02c over a balanced book against +0.68c for
bid-heavy. Those halves do not look alike:

    hold 30s / rest 45s        n   realised if opt.     if it never fills
    bid-heavy                262   +1.122c +/-0.411     +0.473c +/-0.611
    ask-heavy                336   +0.439c +/-0.138     -0.550c +/-0.239

Bid-heavy triggers are roughly 2.5x better on the optimistic column, and the
never-fills column separates cleanly: forced to cross out, a bid-heavy entry is
about break-even while an ask-heavy one loses half a cent at t over 2.

**It does not replicate on the recovered corpus.** Same cell, 136 triggers:
bid-heavy +0.502c +/-0.628 against ask-heavy +0.464c +/-0.406, on 8 and 11
markets respectively. That is not a refutation, it is no power - 8 markets
cannot detect a half-cent difference. But it means the split is one period's
result, which is the exact situation the "three periods, no survivor" chapter
was in. Pre-register it before the next live run rather than acting on it.

    RECOVERED CORPUS, 136 triggers across 19 markets

    hold  rest   optimistic  conservative   realised if opt.   if conservative
     15s   20s      91%          32%       +0.337c +/-0.213   -0.540c +/-0.249
     30s   45s      90%          38%       +0.481c +/-0.350   -0.463c +/-0.377
     60s   90s      90%          35%       +0.590c +/-0.425   -0.718c +/-0.433

Same bracket straddling zero, weaker throughout, and too small to settle it.

Two things worth stating about the fixes rather than the result.

Requiring one shared trigger set with the whole window inside the market's life
drops 13,782 candidate triggers to keep 598 clean ones. Fewer, but comparable
across cells, which the old 586-to-726 spread was not.

And the post-close guard turned out to be inert here. It flags 4,693 triggers,
every one of which was already dropped for insufficient recording length,
because these recordings end near the market's close. It was a real hole in the
method and it was not costing anything on this data. Both counts are now in the
census so the next corpus does not have to re-derive that.

## Verdict

The signal is real: +0.86c of mid-to-mid directional forecast at extreme
imbalance against a control of +0.013c, monotone across 640 markets, with the
thick side carrying +0.78c of it - so it is not a thin-side widening artifact.
At the touch convention that a resting quote actually collects, the same table
reads -0.905c control to -0.014c at |OBI| > 0.9: a +0.891c lift off a baseline
that starts under water.

The round trip is **undecided, not refuted**. Priced conditionally, with a fill
detector that brackets rather than one that misses the common case, realised
expectancy runs +0.6c to +1.1c at the upper bound and -0.4c to 0.0c at the
conservative one. Live orders on the bid-heavy side are the test that decides
it.

Live so far, 13 real orders and 33 cents: entries filled at the displayed touch
every time, realised cash -1.09c per completed trade. That is 13 orders. It
does not distinguish between the two bounds above and should not be quoted as
if it did.

Full corrected output: `exit-fill-study.txt` and `gate-dose-study.txt`, verbatim.
