# Research notes — measured signals beyond passive quoting

Working notes for strategy development. Everything here is measured, with the
instrument and sample stated, because this project has repeatedly discovered
that unstated methodology is where the errors live.

## Live markout by window phase (n=164 live fills, 2026-08-17)

| time left | n | mean | median | in favour |
|---|---|---|---|---|
| 12-15m | 29 | +0.414c | +0.500c | 72% |
| 9-12m | 79 | +0.416c | +0.500c | 75% |
| 6-9m | 56 | +0.185c | +0.150c | 66% |
| 0-6m | 0 | — | — | — |

Two things. The early window is our *best* regime live, which contradicts the
polled-era backtest bucket (12m+ mean −0.53c) — that number was built on
unknown-horizon markout from 1%-resolution data and loses to live evidence.
And the final six minutes are completely unexplored: every live run started at
window open with a ~7 minute duration. Depth collapses ~11x into the close, so
this is not a small gap.

## Mid-price momentum inside a window (13 window paths, canonical mid series)

Conditional on a ≥1c mid move over 30 seconds, the next 30 seconds:

- continues in the same direction **74%** of the time (n=42)
- moves a further **+2.91c mean / +1.85c median** in the trigger's direction

Measured from `BacktestResult.mid_series` over the 7 websocket-era feed
recordings — not from a hand-rolled book reconstruction (the first attempt at
one self-crossed within 30s and produced nothing; use the canonical machinery).

Implications, in order of confidence:

1. **Defence.** The worst markout tails (−1c to −7.5c) are consistent with
   resting through exactly these moves. Pulling or skewing quotes for ~60s
   after a 1c/30s trigger converts the fat left tail into avoided fills. This
   is a parameter-sized change to the market maker, not a new strategy.
2. **Offence.** +2.9c expected continuation against a taker fee of 1.75c at
   the midpoint — and 0.04–0.6c in the tails, which is where momentum
   converges as windows resolve. A small taker strategy triggered by the same
   signal is plausibly positive after fees, and unlike market making it is not
   queue-constrained, so it scales in size where the MM cannot.
3. n=42 across 13 windows in one afternoon. Direction is credible; magnitude
   is soft. Both firm up for free as the collector accumulates windows.

## Venue asymmetry (n=110 live fills)

ETH15M: +0.352c mean, 80% favourable, queue depth ~7 contracts.
BTC15M: +0.161c mean, 46% favourable, queue depth ~1,690 contracts.

The thin market is the better market: we sit at the front of a short queue and
the flow that reaches us is less informed. Sizing should follow — ETH first,
BTC smaller, not symmetric.

---

# Strategy evaluations, 2026-08-17

Six ideas built or tested against the 7 websocket-era feed recordings and the
live account. One shipped, one confirmed, four rejected. Rejections are recorded
in full because each cost real work and the reasons generalise.

## 1. Momentum defence on the market maker — REJECTED

Three designs, each fixing the last one's flaw, none beating no-defence.

| variant | fills | capture | per fill | inventory | flat runs |
|---|---|---|---|---|---|
| baseline adaptive | 3,582 | $15.34 | 0.43c | +$1.33 | 6/7 |
| v1 withhold exposed side | 977 | $3.85 | 0.39c | −$14.78 | 1/7 |
| v2 widen exposed side | 2,472 | $18.37 | 0.74c | −$10.38 | 1/7 |
| v3 widen both sides | 2,250 | $22.36 | 0.99c | −$10.32 | 2/7 |

v3 more than doubles per-fill edge — the defence *works* at what it targets.
It still loses on net, consistently rather than in one window:

    adaptive             +2.86 +3.83 +1.37 +0.84 +2.83 +3.24 +0.09  = +15.06
    symmetric:adaptive   +0.36 +3.79 +2.77 +2.16 +2.85 -0.33 +0.07  = +11.69

**The lesson:** fewer fills means a position opened before a signal is worked
off more slowly, and that inventory cost exceeds the fill-quality gain. Also,
v1's failure is worth remembering on its own — quoting one side only IS a
directional bet, so a defence built to avoid directional risk created it.

## 2. Asymmetric venue sizing — CONFIRMED, ship it

Two independent methods agree that ETH15M is the better book:

| method | KXETH15M | KXBTC15M |
|---|---|---|
| live markout (n=290) | +0.338c, 73% favourable | +0.148c, 55% favourable |
| backtest net per fill | 0.46c | 0.31c |

ETH is ~50% better per fill despite being 20x smaller by volume, because its
queue is ~7 contracts against BTC's ~1,690. We sit at the front of a short queue
and the flow reaching us is less informed. Size should follow the edge, not the
volume.

## 3. The final six minutes — UNTESTED, cheapest open question

Every live run so far started at window open and ran ~7 minutes. The closing
phase is unmeasured, and it is where depth collapses ~11x and the fee falls to
0.04–0.18c. One session of runs timed to span the close answers it for a few
dollars.

## 4. Momentum taker overlay — REJECTED on this sample

14 fills across seven recordings, net −$4.77. The fee gate worked as designed:
it refused the midpoint, where a 1.75c fee eats a 2.9c edge, and what survived
in the tails was too rare to matter. Built so a weaker-than-measured effect
produces silence rather than losses, and that is what it produced.

## 5. Spot-feed fair value — REJECTED in its exploitable form

Coinbase and Kraken spot are freely reachable. Sampled BTC spot against a live
KXBTC15M book at 1s for 186 samples:

- contemporaneous 1s correlation: **0.226**
- spot move → Kalshi move 5s later: −0.08
- → 10s later: −0.10 · → 20s: +0.03 · → 30s: +0.07

**Spot does not lead the Kalshi book at any horizon we can act on.** The market
already prices spot contemporaneously. There is no lag to trade.

The stronger form — computing fair value from spot and a volatility model, then
quoting around that rather than around the book's mid — is not refuted by this,
because it does not depend on a lead. But it needs a volatility model, carries
model risk, and at 1s sampling we cannot even see the timescale it would operate
on. Not worth building on this infrastructure.

## 6. Polymarket maker rebates — OPEN, best remaining growth option

CLOB API reachable (HTTP 200), market listing needs different pagination than
tried. Fee schedules there are taker-only with 15–25% maker rebates: paid to do
what we currently do for free. Same underlyings, and it sidesteps the binding
constraint, which is that Kalshi has exactly two books whose queues we can reach.

Unevaluated. It is the only idea here that raises the ceiling rather than the
rate.

---

# External data sources, 2026-08-17

## What is reachable

| source | status | use |
|---|---|---|
| Coinbase BTC-USD | 200 | spot, USD |
| Kraken BTC-USD | 200 | spot, USD |
| Deribit `btc_usd` index | 200 | index + options chain for implied vol |
| OKX BTC-USDT | 200 | spot, USDT |
| Polymarket gamma + CLOB | 200 | competing venue, same events |
| Binance spot/futures | **451** | geo-blocked from this location |

Binance being blocked matters more than it looks - see below.

## Spot feeds do not lead Kalshi

186 paired samples at 1s against a live KXBTC15M book: contemporaneous
correlation 0.226, and spot moves predict Kalshi moves 5/10/20/30s later at
−0.08 / −0.10 / +0.03 / +0.07. **There is no lag to trade.** A faster spot feed
buys nothing on its own.

## The finding: Kalshi and Polymarket settle different underlyings

Both venues run BTC strike markets expiring at the same instant (16:00 UTC),
with matching strikes. They are not the same contract.

- **Kalshi** `KXBTCD-...-T63999.99` settles on the 60-second average of **CF
  Benchmarks BRTI**, a BTC/**USD** index, above 63999.99.
- **Polymarket** "above $64,000 on August 17" settles on the **Binance 1-minute
  candle for BTC/USDT** close.

Observed at 15:35 UTC, 25 minutes to expiry, on the $64,000 strike:

    Kalshi mid   0.325   (bid 0.32 / ask 0.33, tight and liquid)
    Polymarket   0.590   (bid 0.58 / ask 0.599)
    gap          26.5 cents

Measured basis at that moment: BTC/USD 63,970.82 across three sources,
BTC/USDT 64,029.55 — a **+$58.74 (9 bps)** spread. The strike sat $29 *above*
USD spot and $30 *below* USDT spot, so the two contracts genuinely had opposite
moneyness at the same instant.

At a crude 23-minute sigma of ~$200 that basis explains roughly 12 points of
the 26.5-point gap. **The remainder is unexplained and worth pursuing.**

### Why this matters more than a faster feed

This is not an arbitrage - the payoffs differ - and it must not be traded as
one. It is a basis position: long Kalshi / short Polymarket on the same strike
is a bet on where BTC/USD sits relative to BTC/USDT at a known instant.

It is also the first thing found here that is *not* queue-constrained and does
not depend on a lead-lag edge, which is what killed the spot idea and caps the
market maker at two books.

### The instrument gap

Kalshi settles on BRTI, which we do not currently read. Polymarket settles on a
Binance candle, and **Binance is geo-blocked from this location**. So today we
can price neither venue's settlement source directly - we are approximating both
from Coinbase, Kraken, Deribit and OKX. Closing that gap (a BRTI feed, and a
Binance-equivalent price) is a prerequisite before any size goes near this.

## Cross-venue gap, resolved with real implied vol and live books

The 26.5-point gap does not survive proper measurement. Two errors made it:

1. **Guessed volatility.** Replacing a back-of-envelope sigma with Deribit's
   DVOL (34.2% annualised) changed the basis-explained share from 12 points to
   16.
2. **Stale prices.** Polymarket's gamma `outcomePrices` / `bestBid` were **14
   points stale** against the live CLOB book - 0.83 cached against a real
   midpoint of 0.97 at the same instant. The live figure agreed with fair value
   to within a cent. The entire apparent mispricing was the cache.

Priced against each venue's own underlying, with live books:

    USD 64,125  USDT 64,189  basis +$63.89  DVOL 34.2%  9 minutes left
    strike   K mid  K fair  K edge   P mid  P fair  P edge   basis
    64,000   0.945   0.918   +2.7c   0.971   0.982   -1.1c   +6.4pts

**Both venues are efficient within a few cents of their own fair value.** There
is no cross-venue edge here, only a basis that is exactly what it should be.

`scripts/cross_venue.py` reads live CLOB midpoints and refuses to fall back to
cached quotes - returning None instead - because a stale price that looks live
is how this nearly became a trade.

---

# Window phase: where the edge actually is (539 live fills)

Markout by time remaining, pooled across all live runs:

| time left | n | mean | in favour |
|---|---|---|---|
| 12-15m | 29 | **+0.414c** | 75% |
| 9-12m | 79 | **+0.416c** | 75% |
| 6-8m | 39 | +0.232c | 56% |
| 4-6m | 92 | +0.078c | 63% |
| 2-4m | 82 | +0.002c | 49% |
| 1-2m | 33 | −0.068c | 36% |
| <1m | 3 | −0.050c | 0% |

Monotonic decay. The edge is concentrated in the **first half** of a window and
is gone by roughly six minutes out - consistent with the mechanics: as expiry
approaches the remaining traders increasingly know where it settles, and depth
collapses about elevenfold, so a resting quote sits in a thinner, better
informed book.

I had this backwards. The closing minutes were on the list as unexplored
*opportunity* - low fees, thin queue. They are unexplored *risk*.

## The gate, and why the backtest cannot judge it

`strategy/phase.py` goes **reduce-only** past a threshold rather than stopping:
withholding quotes leaves inventory with no exit except crossing, which is how
the momentum defence turned +$1.33 of inventory into -$14.78.

Backtested it collapses: 3,582 fills -> 177, net $15.06 -> $1.57. That number
means nothing, because of where the recordings sit in the window:

    fills by time to close, across the 7 feed recordings
      12m+     177
      9-12m      0
      6-9m       0
      3-6m     539
      0-3m   2,866

**95% of backtest fills are inside six minutes of close, and the 6-12m band -
where the live edge lives - is empty.** The collector records fifteen-minute
cycles that land wherever they land, and they have landed late. So the backtest
is not a fair test of a rule about window phase; it is mostly measuring the
regime the rule exists to avoid.

Fixing this needs recordings aligned to window open, which is a collector change
(start a cycle when a window opens rather than on a fixed clock), not a strategy
change. Until then the gate should be judged live, where the 539-fill decay
curve came from.

---

# The backtest cannot measure adverse selection (2026-08-17)

With the collector finally recording whole windows, the simulator's markout can
be compared to live markout on the same markets, bucket by bucket. They do not
agree, and the disagreement is structural rather than a calibration offset.

| time left | SIMULATED | in favour | LIVE | in favour |
|---|---|---|---|---|
| 12m+ | +1.008c | 87% | +0.354c | 68% |
| 9-12m | +1.250c | 88% | +0.316c | 66% |
| 6-9m | +0.574c | 79% | +0.222c | 63% |
| 4-6m | +0.471c | 85% | +0.085c | 53% |
| 2-4m | +0.521c | 84% | +0.094c | 55% |
| 1-2m | +0.545c | 77% | **-0.112c** | 38% |
| <1m | +0.179c | 42% | +0.104c | 48% |

    simulated:  early +0.663c   late +0.505c    (n=3,634)
    live:       early +0.268c   late +0.058c    (n=784)

Two separate problems:

**Level.** The simulator overstates markout by 2.5x early and **8.7x** late.

**Shape.** Live markout decays hard through a window - +0.27c early against
+0.06c late, and negative in the 1-2 minute band. The simulator shows almost no
decay at all (0.66 -> 0.51) and stays strongly positive in the band where live
trading loses money. It is blind to the single effect that governs when we
should be quoting.

## Why

The queue model fills us on mechanical book events - a level shrinks past our
position, so we trade - and then marks the fill against the mid immediately
after that same event. Nothing in that loop knows *who* traded against us or
why. In reality the fills that hurt are the ones taken deliberately by someone
who knows where the market is going, and that selection is exactly what the
simulator has no representation of.

A comment in `sim/fills.py` claims marking against the post-event mid "slightly
understates captured edge - the conservative direction". Measured, it overstates
it by up to nine times.

## What this invalidates

Every strategy comparison in this project measures spread capture **in a world
without adverse selection**. On aligned data the ranking is:

    dumb              4,292 fills   $9.65 capture   0.22c/fill   resid 2.2
    adaptive          1,562 fills   $7.93           0.51c        resid 0.4
    phased:adaptive   1,428 fills   $7.29           0.51c        resid 0.7
    horizon             672 fills   $3.82           0.57c        resid 0.7

That ordering is a ranking of who captures the most spread when nobody picks
them off. It cannot answer which strategy survives being picked off, which is
the question that decides whether any of this makes money - and live markout
says the difference between regimes is the whole game.

## What to trust instead

Live measurement, which is why the 784-fill decay curve is the most valuable
artifact of the session. The simulator remains useful for mechanics - does a
strategy quote, does it manage inventory, does it stay flat - and for relative
fill rates. It should not be used to choose between strategies on edge, and no
parameter that trades off fill quality against fill quantity can be tuned on it.

---

# Adverse selection in the fill model: attempted, failed, informative

The simulator overstates markout 2.5x early and 8.7x late and shows none of the
decay live data shows. Two hypotheses, tested in order.

**H1: it is a marking artifact** - we measure at too short a horizon. Rejected.
Simulated markout against the forward mid series is flat at every horizon:
1s 0.558c, 5s 0.557c, 15s 0.487c, 30s 0.592c, 60s 0.499c, 120s 0.521c. Waiting
longer never reveals a loss.

**H2: it is a selection artifact** - the simulator gives us too many good fills
relative to bad ones. Built `sim/adverse.py` to keep adverse fills and thin
favourable ones, then calibrated the thinning rate against live:

    keep   fills    early     late      (target: early +0.268c, late +0.058c)
    1.00    1561   +1.093c  +0.579c
    0.50    1264   +1.143c  +0.562c
    0.30    1205   +1.049c  +0.537c
    0.15     956   +1.002c  +0.451c
    0.05     602   +1.006c  +0.222c

**Also rejected.** Discarding 95% of favourable fills leaves early markout at
+1.006c against a +0.268c target. The mix is not the problem.

**What is left: fill eligibility.** If removing fills does not lower the average
quality of what remains, the remaining fills are themselves too good - the
simulator is filling us at prices reality would not give us. The queue model
puts us at the touch whenever a level shrinks past our position; in a book with
1,690 contracts resting ahead, we would not be there at all. That is a question
of whether a fill happens, not which fills we keep.

The model is committed, defaulted off, because ruling out the obvious
explanation is worth keeping. The next attempt should condition fills on
modelled queue position.

One methodological note: the first version of this model judged fills on a
30-second forward drift while calibrating against immediate markout. Thinning
half the "favourable" fills moved measured markout from +1.093c to +1.131c -
no effect - because thinning on one quantity cannot calibrate another.

## Fixed: the simulator was filling orders that could not trade

The 2.4x optimism was mostly one bug. `QueueAwareFillModel` treated a size
reduction at **any** price level as containing trades, so a cancellation three
levels deep drained our queue and eventually filled us at a price the market
never reached.

Measured before the fix: **71% of queue fills happened behind the touch**,
median 0.30c back, p90 a full cent. Buying under the market always marks up,
which is why those fills looked so good, why simulated markout ran 2.4x live,
and why discarding fills never helped - they were not mis-selected, they were
impossible.

Requiring an order to be reachable at the touch before a reduction can fill it:

                    before    after     live
    early          +1.093c  +0.555c  +0.268c
    late           +0.579c  +0.285c  +0.058c
    fills            1,562      662        -

    by reason, after:  queue_exhausted 326 @ +0.507c
                       through         197 @ +0.454c
                       cross_or_touch  139 @ -0.295c

Also fixed on the way, though it turned out immaterial (1,562 -> 1,531 fills):
`_fractional_count` floored at one unit, so any reduction too small to round up
still consumed queue and nothing ever rounded down. Queue consumption now
carries the fractional remainder across events.

### The residual is adverse selection, and it is now quantified

Buying at the best bid of a one-cent market yields +0.5c of markout
mechanically, and the simulator now reports roughly that. Live reports about
half. The difference - **~0.25c per fill** - is the cost of being selected
against: the counterparty who lifts a resting quote is more often right about
the next few seconds than we are.

That is not a bug to fix. The simulator cannot represent it without knowing why
somebody traded, and the recordings carry no counterparty identity. It is a
haircut to apply when reading simulated edge, and it is now printed on every
sweep report.

---

# Alpha candidate tests, round 2 (offline, 7 aligned recordings)

## BTC -> ETH cross-window lead: DEAD

BTC15M's book is ~20x thicker than ETH15M's, and both windows record
simultaneously every cycle, so if information reached the thick book first the
thin one would lag. It does not:

    BTC(past 10s) -> ETH(next 10s):  n=867  corr -0.027
    control ETH -> BTC:              n=867  corr -0.074
    after a >=1c BTC move: ETH same-sign 290/594 (48.8%), signed mean -0.014c

Same conclusion as the spot test: everything watching the same underlying
prices it contemporaneously. There is no cross-asset lag at horizons we can act
on. That is now three lead-lag hypotheses dead (spot->Kalshi, Kalshi<->Poly,
BTC->ETH), which is itself a finding: these markets are informationally flat.

## Cancellation-flow signal: ALIVE, with the opposite sign to the hypothesis

Hypothesis was "makers pull the side about to be run over" - bids pulled means
down. Measured (trailing 10s of side-classified reductions, next 10s mid move,
n=1,951 windowed samples):

    bids-pulled dominant (>50% imbalance):  next move +0.288c  (n=403)
    asks-pulled dominant:                   next move -0.077c  (n=339)

Bids being pulled predicts UP. The story consistent with the sign: heavy
bid-side shrinkage is what upward REPRICING looks like - stale bids cancelled
and re-placed higher while the mid climbs. So this is largely the 74%
momentum-continuation effect read from order flow instead of price, but it is
observable in real time, costs nothing to compute from the feed we already
consume, and the conditional spread between states is ~0.37c over 10s with the
states active ~38% of the time. Rough scale: +0.288c on n=403 against ~2c move
noise is about three standard errors.

Monetisation is the open question, and the failures already on record apply:
as a taker it cannot beat half-spread plus fee; as a symmetric widen/withhold it
recreates the momentum defence, which lost. The untested shape is **asymmetric
joining** - when bid-pulls dominate, place only the join-bid (ride the
repricing) and let the ask rest wider. That is a one-parameter change testable
live for a few dollars, and it should be tested live, not in the simulator,
whose residual 2x optimism sits exactly on fill quality.

## Updated alpha map

Dead by measurement: spot lead, BTC->ETH lead, cross-venue basis (efficient),
momentum taker (fees), momentum defence (inventory turnover), late-window
making (informed flow).

Alive by measurement: early-window making (+0.27c, the base business);
repricing-flow signal (above, monetisation open).

Untested, ranked by cost of finding out:
1. **Smart flatten** - when |price - settlement certainty| is tiny at cycle end,
   settling beats paying half-spread+fee to cross. Pure cost reduction, sizeable
   from existing journals, zero new risk.
2. **Settlement-average lock-in** - Kalshi settles on a 60s AVERAGE of BRTI, so
   during the final minute the settlement value is progressively determined by
   already-observed seconds; variance collapses linearly while books may price
   it as still open. Needs a spot sidecar recorder next to the book feed.
3. **Window-open dislocation** - the first thin minute after open, priced
   against the prior window's continuous path.
4. **Polymarket maker-rebate MM** - the ceiling raiser; whole playbook applies.

---

# Polymarket venue recon (agent research, sourced from docs.polymarket.com)

The economics are precisely shaped for what we built, and the blocker is
jurisdiction, not technology.

**For a resting-quote MM:**
- Makers pay **zero fees and zero gas** (off-chain EIP-712 orders; operator
  settles on Polygon). Takers pay C x feeRate x p(1-p), crypto feeRate 0.07 -
  the same formula as Kalshi.
- **Liquidity Rewards: ~$1M/month**, concentrated exactly on the 5-min/15-min
  crypto up/down markets ($550k + $350k monthly), paid daily, scored per-minute
  by a quadratic closeness-to-mid function with a min-of-both-sides rule that
  requires two-sided quoting. This program pays people to do what our strategy
  already does for free on Kalshi.
- **Maker rebates on top**: 15-25% of taker fees redistributed pro-rata by
  filled maker volume (crypto 20%).
- Rate limits: 40 orders/s base (Kalshi 429'd us at ~4/s), scaling with volume.
- Their short crypto markets settle on a **60-second TWAP of Chainlink** since
  Aug 7, 2026 (changed after a manipulation incident) - meaning the settlement
  lock-in analysis we built for Kalshi applies to Polymarket identically.
- Volume: ~$153M/day in 5/15-min crypto markets (Apr 2026 reporting).

**The blocker:** the US is on the main CLOB's close-only geoblock list,
IP-enforced - a US person/entity cannot OPEN positions via the API. KYC does
not unlock it. The CFTC-regulated "Polymarket US" (via the QCEX DCM/DCO
acquisition) is a separate, intermediated, invite-only venue that does not
expose the rewarded crypto CLOB markets; Polymarket was still seeking CFTC
approval to open the main exchange to US users as of April 2026.

**Standing decision for Mike, not for the tooling:** the strategy is runnable
there today only through a non-US entity, or by waiting for the pending CFTC
approval. We do not route around geoblocks. Until that changes, Polymarket is
a monitored option, not an executable one - worth re-checking the CFTC status
periodically, because when it opens, the playbook and codebase port almost
unchanged (same fee formula, same TWAP settlement, richer rewards).

---

# Full-exchange lifecycle census: the 15-minute family is bigger than we knew

A one-pass census of every open market (31 series with median lifetime under
two hours; an agent-run version of the same census reported zero, a reminder
that agent output needs the same controls as any other instrument):

    15-minute windows (one open at a time, volume = current window):
      KXBTC15M 12,052   KXETH15M 480   KXGOLD15M 470   KXWTI15M 224
      KXSILVER15M 174   KXXRP15M 125   KXSOL15M 114    KXHYPE15M 78
      KXDOGE15M 59      KXBNB15M 36    KXNEAR15M 10    KXZEC15M 8

    hourly ladders: KXBTCD 22,595 contracts/hr across 318 strikes; KXETHD,
    KXSOLD, KXBTC, KXETH, KXSOLE similar structure
    ~1-2h: hourly city temperatures, GOLDH/SILVERH/WTIH, KXEARTHQUAKEM (893)

**Capacity re-measured immediately: 5 reachable queues, up from 2** - BTC15M,
ETH15M, XRP15M, SOL15M and a near-expiry BTCD hourly strike, all at once.
GOLD15M (ETH-scale volume on a Sunday night, a *non-crypto* underlying with
different session dynamics) will qualify at busier hours.

Collector now pins eight 15M series. Order of operations stands: record first,
measure markout per venue, trade only what measures positive. The whole
decay-curve/journal/session apparatus applies per-venue unchanged - which is
the payoff of having built it venue-agnostic.
