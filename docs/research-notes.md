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

---

# Settlement lock-in: tested, not exploitable at our measurement precision

First harvest with the hardened pipeline (13 windows, 366 scored final-minute
seconds, zero dropped for spot gaps):

    45-60s remaining  n=178   mean -6.42c   median -0.15c
    30-45s            n=104   mean -6.12c   median -0.22c
    15-30s            n= 58   mean -2.81c   median -1.12c
     5-15s            n= 24   mean -0.26c   median -0.23c
      <5s             n=  2   mean -0.43c   median -0.43c

The medians are the story: within ~1c of zero at every horizon, inside the
stated noise floor. The large negative *means* early are a fat left tail in a
few windows - the signature of strike-proxy error (our strike is spot at open,
and near the money a $20 proxy error swings the model fair violently), not of
market error: fair is the fragile quantity in this join, the mid is not.

**Conclusion: the book prices the final minute correctly to within our ~1-2c
precision.** No slope toward the close survives the noise floor.

The unifying read, and it is worth keeping: this result and the late-window
maker markout are the same fact seen from two ends. Late-window makers measure
-0.11c/fill because someone informed is picking them off; the lock-in taker
finds no free edge because *that someone is already doing this exact
arithmetic*. The lock-in trade exists - it is just already crowded, and its
profits are the losses our maker measured. Beating the incumbents would need
the true settlement feed (BRTI) and the true strike, not proxies of both.

That closes the late-window question from both directions, and closes the last
untested item on the alpha queue that could be tested from here. Remaining:
per-venue markout on the newly recorded 15M family (data accumulating), and
Polymarket (gated on jurisdiction, not on research).

---

# Per-venue markout: the commodity 15M windows beat crypto

11 aligned recordings (2026-08-18), adaptive at 1 contract, simulated markout
(apply the ~0.25c adverse-selection haircut; rank relatively):

    series        recs  fills  sim markout  in fav   capture
    KXDOGE15M        7    826   +1.028c      77%      5.63$
    KXWTI15M         7   1460   +0.961c      86%     11.72$   <- oil, thickest
    KXSILVER15M      8   1197   +0.737c      81%      8.04$
    KXBTCD           7    484   +0.732c      94%      3.40$   <- hourly strike
    KXGOLD15M        8   1037   +0.607c      79%      4.81$
    KXETHD           3     98   +0.587c      77%
    KXXRP15M         7   1408   +0.569c      77%      5.31$
    KXSOL15M         6    947   +0.554c      73%
    KXBTC15M         9   1325   +0.286c      82%      3.14$   <- what we trade
    KXETH15M         7   2921   +0.266c      71%      5.51$   <- what we trade

**The two venues we have been trading are the two WORST on the list.** BTC and
ETH are the thickest, most-competed books, so our resting quotes there sit
behind the most informed flow. The commodity 15M windows - oil, silver, gold,
doge - show 2-4x the simulated markout, because they are less picked-over: WTI
at +0.96c/86% favourable is a different animal from BTC at +0.29c/82%.

Caveats held firmly: these are simulator numbers, ~2x optimistic and blind to
adverse selection by ~0.25c/fill, so the absolute values are not money. But the
RANKING is the trustworthy part - every series ran through the identical model,
so relative order survives the haircut. And thinner books mean lower capacity:
WTI/silver fill a few hundred contracts a window against ETH's thousands, so
this is a per-book edge, not a volume story.

**Actionable:** the live session should widen from BTC/ETH to the whole family
and let live markout re-rank them with real fills. If the sim ranking holds even
partially, WTI/silver/gold at 80-86% favourable are where the coffee fund gets
earned - not the crypto majors we started on. Capacity was 2, then 5; this says
the useful number is closer to 8-10 books, each thin but each an independent
draw, which is exactly the breadth that beats sizing up.

---

# Reconciling Nate's ~$1.50: he did by hand exactly what our bot failed to automate

The open puzzle: Nate made ~$1.50 on $10, using a few dollars, hand-tuning
settings on BTC15M and "thoughtfully managing queue issues" - while our
automated version bled. Nothing we measured contradicts his result; it explains
it, and the explanation is the fix we just deployed.

**The single number that reconciles it: 1,621 maker fills cost $0.01 in fees,
107 taker fills cost $1.02.** Every profitable thing on Kalshi is a resting
maker round-trip (buy rests, sell rests, both fill, zero fees, spread captured).
Every loss is a cross - the taker fee plus the half-spread paid to get out.

What Nate did, translated:
- **He round-tripped as a maker and did not cross to exit.** "Thoughtfully
  managing queue issues" is precisely this - rest, hold your place, wait for the
  other side to fill, do NOT pay to get out. His profitable trades were free
  maker fills on both legs.
- **He was selective and tiny.** A human watching a few dollars trades when the
  setup looks good and sits out otherwise. At a few contracts he was negligible
  size with no adverse impact, and he could decline the bad fills our always-on
  bot takes automatically.
- Mike's own earlier note: Nate ALSO hit "fees ate everything" - in exactly the
  episodes where he crossed. His net +$1.50 is the maker round-trips minus the
  cross episodes he learned to avoid.

**Where we diverged from him: automation re-introduced the exact cost he learned
to avoid by hand.** Our bot quoted continuously (taking every fill, good and
bad), and then FLATTENED EVERY CYCLE BY CROSSING. We mechanised the one action
Nate's discretion was avoiding. Positive markout, negative account - because we
paid to exit what he waited to exit for free.

This is not a story about a secret setting we failed to replicate. It is the
opposite: he had no edge we lack. He had discretion where we had a mechanical
flatten, and discretion happened to do the profitable thing. The deployed fix -
exit passively, cross only the stub - is our attempt to encode his hand-judgment
as a rule.

Two honest caveats. $1.50 is a handful of round-trips and partly luck at that
sample size; it is consistent with the mechanism, not proof of a repeatable
rate. And a human's selectivity - trading only good setups - is itself an edge a
naive always-on bot does not have, which is a separate lever (quote only when
the book/flow looks favourable) we have not yet built.

**Refinement (Mike: Nate traded BTC15M only).** This confirms rather than
complicates the reconciliation. BTC15M is middling by our per-venue ranking -
live markout +0.36c, 71% favourable - positive but not the best. So Nate made
money on a genuinely positive-markout book by capturing the maker edge without
crossing to exit, tiny and selective. The commodity finding (WTI +0.75c, silver
+0.53c) means the *same discipline* on a better book would have paid ~2x for
identical behaviour - he left upside on the table, he did not get lucky on a
losing book. It also means our bot's losses on BTC15M were NOT the book being
bad; they were the flatten-by-crossing cost, which is the whole point.


---

# The fix worked: first corrected cycle went positive, P&L > markout

The passive-exit flatten, first cycle after deploy (4 commodity books + carryover):

    exited WTI / SILVER / DOGE passively (free)
    141 fills, markout +0.371c implies +$0.52, ACCOUNT +$1.32, balance $41.13

Account P&L exceeded the markout-implied edge for the first time - resting exits
filled at favourable prices instead of crossing, so the $1.02/session taker drag
became zero and the paper edge finally reached the account. One cycle, not proof,
but it moved exactly as the taker-drag diagnosis predicted.

This closes the Nate question completely. He automated his bot on BTC15M and
won; automation was never the difference. The difference was one line - flatten
by crossing - that our bot had and his did not. Removing it reproduced the shape
of his result. No secret setting existed.

Mike's steer: "fine with small and middling but positive." So the target is not
the best book, it is a clean positive run - BTC15M qualifies, the commodities are
upside. The engineering goal is keeping exits free and inventory low, not chasing
the top of the venue ranking.

---

# The passive-exit paradigm, applied to every market we considered

The insight that turned the account positive - never cross to flatten - is not a
tweak, it is a new screening criterion, and it re-ranks every venue.

## The two conditions a resting-quote MM actually needs

A maker keeps its edge only where BOTH hold:

1. **You can rest an exit and it fills before you must be flat.** Needs
   *continuous two-sided flow* (someone to trade your resting exit) and *time*
   (the market does not resolve or gap before it fills).
2. **When a cross is unavoidable, it is cheap.** The taker fee is
   `0.07 x P(1-P)` - maximal at mid-price 0.50, near zero in the tails. A book
   that lives near 0.50 makes every forced cross expensive.

Measured cross-rate x cross-cost per venue (ledger):

    BTC15M   9% crossed, 0.107c/fill  <- worst on BOTH: crosses most, near 0.50
    ETH15M   5%,         0.036c/fill
    commods  2-5%,       0.004-0.031c/fill  <- WTI 0.004c: near-free
    WTI15M   2% crossed, 0.004c/fill  <- best: rarely crosses, tail-priced

This is why the commodity windows win under the paradigm, and it is the SAME
fact as their better markout, seen through cost instead of drift.

## Full circle: the tail-price preference was right all along

Day one concluded "quote near an end, where the fee is cheap." The maker-free
discovery seemed to retire that - price does not matter for free resting fills.
The passive-exit paradigm brings it back for a subtler reason: **price decides
the cost of the unavoidable crosses.** Tail-priced books are good again, not
because entries are cheap (free anywhere) but because forced exits are.

## Re-scoring the markets we shelved

- **Hourly strike ladders (KXBTCD etc.)** - RECONSIDER, with a catch. Deep
  strikes are tail-priced (cheap forced crosses) but never trade (can't rest an
  exit - condition 1 fails). The ATM strike trades but sits near 0.50 (expensive
  crosses - condition 2 fails). The two conditions are anti-correlated across a
  ladder, so a ladder is structurally worse than a single at-the-money window
  that stays tradeable AND drifts to the tail as it resolves - which is exactly
  what a 15M window does. This explains why 15M windows beat ladders.
- **In-play sports / esports** - STILL OUT, now for a sharper reason. Flow is
  directional and lumpy (a resting exit may never fill - condition 1), and a
  close game sits near 0.50 (expensive forced crosses - condition 2). They fail
  both conditions precisely when active.
- **News / political (KXTRUMPSAY)** - OUT. News gaps the book; you cannot rest
  an exit through a jump (condition 1 fails discontinuously).
- **Temperature / weather hourly** - MARGINAL. Slow books give time to rest
  exits (condition 1 ok) but thin flow means few round trips; tail-priced when
  the outcome is near-decided (condition 2 ok late). A low-volume positive, if
  positive at all.
- **The 15M commodity family** - the sweet spot, confirmed twice over:
  continuous flow (rest exits fill) + tail-drift as they resolve (cheap forced
  crosses).

## The new screen

capacity_scan now reports mid-price so the fee-at-mid (forced-cross cost) is
visible per candidate. The paradigm ranks a book by: continuous two-sided flow
(condition 1, proxied by depth-relative-to-life reachability, already there) AND
distance of the mid from 0.50 (condition 2, the cheap-cross screen). A book that
is reachable AND tail-priced is the target; near-0.50 books are penalised even
when reachable, because their unavoidable crosses are dear.

---

## The final chapter: the sniper, and three periods with no survivor (2026-08-19)

The maker verdict (edge indistinguishable from zero after passive exits) left one
hypothesis: stop quoting entirely and TAKE, selectively. The case for it was
strong. The book's own imbalance provably predicts the next mid move (+0.85c per
unit OBI at 5s, n=1.36M updates, monotonic on every major venue), and a taker
backtest on recorded books is trustworthy in a way no maker backtest can be: the
fill is deterministic (you pay the displayed touch) and the fee is ledger-exact.
`scripts/taker_expectancy.py` scans every (OBI extremity x entry price x horizon)
slice for net expectancy after the half-spread and the exact fee, with a per-venue
sniper map.

In-sample (8/18, ~11 book-hours, 482 slices searched): 4 of 16 recorded venues
had cost-clearing slices. GOLD15M +1.39c/trade (t=2.0, 43 triggers/hr), BTC15M
+1.00c, SOL15M +0.99c, DOGE15M +0.28c. Twelve venues offered nothing, several
decisively (ETH-daily -0.79c t=-22.6, in-play sports and esports all negative -
replicated rejections worth having).

Then the only test that matters: the same frozen slices on data they had never
seen.

* Pre-8/18 data (BTC/ETH/BTCD coverage only): BTC's winners shrank to noise or
  flipped sign (tail>.85 went -0.25 to -0.28c at t~-2.5). Different slices
  "won" than in-sample.
* 8/19 data (the commodities' true out-of-sample): GOLD fell to ZERO positive
  slices (best -0.07c). SOL's winner vanished. DOGE +0.16c at t=0.2. And the
  scan crowned brand-new winners (WTI +1.05c, XRP +1.24c) that had shown
  nothing the day before.

Three independent periods, three disjoint winner lists, no slice ever repeating.
That is the signature of searching hundreds of noisy slices, not of an edge.

**Final verdict, measured:** on Kalshi's fee schedule, neither a resting maker
nor a selective taker keeps money on any venue we can record. The imbalance
signal is real, and it is worth less than the cost of acting on it. The market
gives the signal away because harvesting it does not pay the fee.

This also completes the Nate reconciliation. His bot's real achievement was zero
expected cost: maker-only, one book, never a forced cross. From there, +$1.50
over a short run is roughly a one-in-three draw, preserved by stopping. The
skill was the zero-cost build and pocketing the draw.

Project closed 2026-08-19. Account $36.66 of the original $50; ~$13 bought the
complete map. All services stopped, VM stopped. The toolchain - ledger-truth
fees, the biased-mid test, deterministic taker scanning, frozen-slice
replication - re-asks this question of any venue with a public book in an
evening.

---

## Audit of the final chapter (2026-08-19, after close)

The chapter above was re-run against the full archived corpus - 187 recordings,
9.8GB, 62.3 hours of real elapsed coverage, 398,736 triggers - with the scan's
measurement defects fixed. **The verdict survives. Almost none of the evidence
given for it does.**

Corrected headline: **0 of 141 testable slices positive net of costs** across the
whole corpus, and a whole-window sign-flip placebo never beats the observed best
in 400 draws. That is a stronger negative than the chapter had, on six times the
data. What follows is what was wrong with how it got there.

### The exit was marked at the mid while the model claimed a rested exit

`net = signed mid move - fee` assumed the exit "rests as a maker (measured
free)". A resting exit fills at the **touch**, not the mid. On books filtered to
<=2c that is half a spread given away on every trade, and it is not small:

    mid convention cost, vs an exit rested at the touch
      whole corpus  +0.671c per trade
      8/18          +0.665c
      8/19 holdout  +0.699c

The largest corrected effect anywhere in the corpus is +0.111c. The exit
assumption alone was worth six times that, in the direction that manufactures
negatives. Three conventions are now priced side by side (touch / mid / forced
cross) plus a blend at the ledger's own per-venue cross rates.

### The t-statistics counted triggers, not price paths

Every trigger inside one market rides one price path, and slices average ~57
triggers per market. Clustering the standard error on the market ticker moves
the median SE by 1.6x, and it moves the venue conclusions much further than that,
because the old numbers combined an inflated effect with an understated error:

    venue          old net   old t     new net   new t   markets
    KXETHD        -0.980c   -44.4     -0.231c    -2.5      127
    KXBTCD        -0.754c    -5.7     -0.199c    -1.2       82
    KXMLBGAME     -0.634c   -11.9     -0.087c    -1.2       56
    KXNFLGAME     -2.160c  -199.6     -1.270c   -15.1        3

The chapter's "ETH-daily -0.79c t=-22.6", cited as a decisively replicated
rejection, reproduces here under the old conventions as -0.98c t=-21.7 on 8/18,
which confirms the reimplementation is faithful. Corrected, that same venue is
-0.231c at t=-2.5. On 8/18 alone it is t=-1.6, indistinguishable from zero. The
"decisive" rejections were mostly the exit convention and the missing clustering.
In-play sports, called "all negative - replicated rejections worth having", give
the corpus's single best corrected slice: KXMLBGAME obi>.9 / tail<.15 / 30s at
**+0.111c**, which is a sign flip from the number the chapter rejected it on.

### "Three periods, no survivor" was mostly absence and low power

Eight slices were pre-registered from 8/18 and put to the holdouts, with the
minimum detectable effect computed before the verdict:

    to the 8/19 holdout : 4 SMALLER, 4 NO POWER. 5 of 8 have under 50% power.
                          The best slice has 32% power against its own effect.
    to the untouched
    8/19 afternoon      : 5 of 8 ABSENT (those markets do not trade 07-14Z),
                          3 NO POWER, 0 testable with adequate power.
    backward control    : freeze the holdout's own winners, test them on 8/18.
                          8 of 8 NO POWER. Neither direction can confirm or
                          refute the other, which is the symmetry you would
                          expect if the periods were never comparable.

A holdout at 32% power that returns a null has not refuted anything. The periods
also sample different regimes under the same slice labels: median seconds-to-
close runs 76,884s (pre), 251,325s (8/18), 251,305s (8/19 early), 47,718s (8/19
afternoon), and the per-slice phase drift reaches 18x. A frozen slice label pins
venue, OBI band, price band and horizon. It does not pin the regime.

### The premise was overstated by 4x and is not present on every venue

"+0.85c per unit OBI at 5s, n=1.36M updates, monotonic on every major venue"
becomes, with the same estimator clustered on the market:

    pooled slope    +0.378c per unit OBI   (n=27.6M book updates)
    per-market      +0.224c +/- 0.058      (t=+3.9, 484 markets)
      KXBTCD        +0.280c +/- 0.073
      KXNFLGAME     +0.610c +/- 0.274
      KXMLBGAME     +0.448c +/- 0.236      not distinguishable from zero
      KXETHD        +0.010c +/- 0.096      not distinguishable from zero

The signal is real and it survives clustering. It is about a quarter of the
advertised size, and it is flatly absent on ETH-daily. "Monotonic on every major
venue" is not supported.

**And it is one-sided.** Forward move by OBI bucket at 5s: ask-heavy -0.294c,
mildly ask-heavy -0.335c, balanced -0.310c, mildly bid-heavy +0.155c, bid-heavy
+0.373c. Relative to the balanced baseline, bid-heavy predicts +0.68c and
ask-heavy predicts +0.02c. These are strike ladders that mostly decay toward
zero, so the common negative drift is expected, but the asymmetry is not: the
scan's rule takes `sign = +1 if obi > 0 else -1`, so roughly half its trades were
taken on the side where the signal does not exist.

### What the scan could not distinguish, and now can

The old scan printed net only. Its verdict sentence - the signal is real and
worth less than the cost of acting on it - was not identifiable from anything it
reported. With gross and fee separated, it is now measured, and it is true:
gross is positive in essentially every slice (+0.06c to +0.65c) and the fee runs
0.36c to 0.56c. The signal clears zero and does not clear the fee.

### Reproducibility gap

**The archived corpus contains no 15-minute-window recordings at all.** All 187
manifests hold KXBTCD and KXETHD strike ladders, MLB, NFL, and two weather
markets. Every venue in the chapter's in-sample table (GOLD15M +1.39c, BTC15M
+1.00c, SOL15M +0.99c, DOGE15M +0.28c) and both 8/19 "new winners" (WTI, XRP)
are 15M series. Those markets were certainly traded live, since the cross-rate
table comes from the ledger, but the recorded books the scan consumed were never
uploaded. `/tmp/recs_pm` was local to the collector VM, which is stopped.

So the chapter's headline in-sample table cannot be reproduced, checked, or
corrected from surviving data. Only its ladder and sports claims can be, and
those are the ones that did not hold up.

### One thing the audit got wrong

The lookahead defect is real - the scan read the book at the first update at or
*after* each horizon rather than the last one at or before it, which peeks at the
move being predicted - but it is worth **+0.001c per trade**, including on the
quietest books where the next update landed more than 10s past the horizon
(+0.004c). It was predicted to matter and it does not. Fixed anyway, in the scan
and in `obi_predictivity`, `markout_horizon` and `markout_selfbook`.

### Addendum, same day: the data was never lost, and there is a control band now

**The 15-minute recordings were recovered.** They had been uploading to
`gs://steps-kalshi-book/recordings/`, which returns 403 for
mike@stepsventures.com and resolves for mike@airloom.energy. The 403 was read as
absence. 159 recordings, 16.6GB, plus 21 fill journals. A further 10 recordings
survived on the stopped VM at `/var/tmp/kalshi-recordings` (`/tmp/recs_pm` was
cleared, as expected). All of it is now mirrored to
`gs://steps-nate-backtest-data/vm-rescue-15m/`.

**With that data the headline claim resolves, and the explanation is simpler than
the one the chapter gave itself.** `KXGOLD15M` had exactly **two windows** in the
data the scan read: 11:30 and 16:15 on 18 August. Thirty minutes of one market's
life. The published "+1.39c, t=2.0" came from treating 931 ticks inside two price
paths as 931 independent draws. SOL, DOGE, WTI, XRP and SILVER were all two
markets as well. That is not hundreds of slices mined for a winner. It is a
standard error computed across two markets, and it explains the replication
failure without any appeal to search noise.

Recovered, those venues have 46 to 54 markets each.

**The control band.** `taker_extract` now keeps books with `|OBI| < 0.2`. They
carry no signal by construction, so whatever they earn in the same price band and
the same part of a market's life is what the structure pays. This is the test the
original scan could never run, because every trigger it kept already had extreme
imbalance and there was nothing to compare against.

Nothing selected, one number per market, 30s horizon, both corpora independently:

    recovered corpus (642 markets)   control -0.905c -> obi>.9 -0.014c   lift +0.891c
    original archive (441 markets)   control -0.724c -> obi>.9 -0.142c   lift +0.581c

Perfectly monotone in both, with standard errors around 0.04c. **The imbalance
signal is real and far better established than the +0.85c originally published.**
It is also not sufficient on its own: it lifts about 0.6c to 0.9c from a baseline
that taking starts roughly 0.9c under water.

**One cell clears it, and it replicates on independent data.** `KXBTCD`, entry
2-5c, 30s, inside the last quarter hour before expiry:

    archive    control +0.025c -> obi>.9 +0.927c  (95 markets, clears zero)
    recovered  control -0.184c -> obi>.9 +0.551c  (24 markets, clears zero)

Monotone in both, control flat or negative in both, and the two estimates are not
statistically different (z = 1.15). That answers the decay confound directly:
drift does not care how imbalanced the book is, so a monotone response to
imbalance with a negative control cannot be decay.

**It does not generalise.** The same cheap-entry condition on the 15-minute
family lifts about 0.2c and stays negative throughout. This is a KXBTCD result,
not a Kalshi result, and anyone sizing it off the exchange as a whole is
overreading it by an order of magnitude.

Also fixed: `markout_selfbook`'s t=0 control cannot pass under any lookup
convention. The mid moves about 1.6c across a single fill (pre-fill sample
+1.34c, post-fill -0.24c, the journal's own stamp +0.37c between them), which is
wider than the entire markout curve. The script now says so rather than failing
silently.

### Live: the displayed touch is real

The audit's one untested assumption was that an order gets the price that was
showing when the signal fired. `scripts/taker_live_test.py` trades exactly the
audited cell at one contract, with a hard \$25 balance floor checked against the
exchange before every entry. It was validated against the recorded corpus first:
the live selector agrees with the audited cell on **629 of 630** recorded rows.

First five live orders, real money, 2026-08-19:

    sell 96c -> filled 96c   0.00c slippage   MAKER, $0.0000 fee
    buy   4c -> filled  4c   0.00c slippage   MAKER, $0.0000 fee
    sell 95c -> filled 95c   0.00c slippage   taker, $0.0034
    buy   4c -> filled  4c   0.00c slippage   taker, $0.0027
    sell 97c -> filled 97c   0.00c slippage   taker, $0.0021

**Five sent, five filled, zero slippage on every one.** The three taker fees
match `0.07 * P * (1-P)` to the cent, which re-validates the fee model on fresh
fills.

Two of the five filled as **makers at zero fees**. Pricing at the touch sometimes
rests and gets hit rather than crossing, and Kalshi charges makers nothing. That
is upside the model never assumed, and it is the same fact that made Nate's bot
work: the cheapest fill on this exchange is the one you did not have to cross for.

What this settles: a one-contract order gets the displayed price. What it does
not settle: **size**. Median depth on the crossing side is 74 contracts, so one
contract was never going to walk the book. Ten and twenty-five contracts are the
tests that decide whether this is worth running.

Three bugs surfaced on the first live run and are worth recording because each
would have produced a confident wrong answer:

1. The order fields were guessed (`filled_count`, `average_fill_price`). The real
   ones are `fill_count_fp`, `yes_price_dollars`, `maker_fill_cost_dollars`. The
   guessed version fell back to our own limit price, which would have reported
   **zero slippage on every trade by construction** - the exact answer we were
   trying to measure, arrived at without measuring anything.
2. The exit only ran when the measurement succeeded, so bug 1 stranded five
   contracts across three markets. Exiting must never depend on the analytics.
3. The 30-second hold ran in the same loop that pumped the websocket, so the
   book used to price the exit was 30 seconds stale.

### Where this leaves the verdict

Split three ways, the verdict is now specific rather than sweeping.

**Right:** there is no general taker edge on this fee schedule. Pooled across
everything, even the strongest imbalance averages -0.014c to -0.142c per trade.
A strategy that takes on imbalance without conditioning on price and phase loses.

**Wrong:** that the imbalance signal is worth less than the fee everywhere. It is
worth +0.6c to +0.9c per trade over a balanced book, monotone across 441 and 642
markets, and on `KXBTCD` at 2-5c entries near expiry that lift clears costs and
replicates on independent data with a negative control.

**Unresolved and now the only thing that matters:** whether a live order gets the
displayed touch. Extreme imbalance means the side you must cross is thin by
construction, so signal strength and available size are anti-correlated. No
amount of recorded book data settles it. Real orders at minimum size do, and
that test costs a few dollars.

The honest closing line is not "the market gives the signal away because it is
not worth the fee to harvest." It is: the signal is real, it is worth about
0.9c, taking costs about 0.9c, and whether you clear that depends entirely on
picking an entry price where the fee is a fifth of a cent instead of one and a
half.


---

# The gate, dosed on real fills - and the dose does not hold up (2026-08-19/20)

The OBI-gated maker went live as an A/B (obi_gate=0 vs 90, round-robin per
cycle, $25 floor). Rather than wait a night to learn whether 90 was the right
level, the journaled fills were re-scored under all six thresholds at once: each
real fill joined to the collector book for the imbalance near it, markout from
the journal's own timeline, veto simulated at every level. The first pass called
that "the treatment group of every threshold", which oversells it - nothing was
assigned, and the fills a veto would block differ from the rest in venue, price
and time of day as well as in imbalance.

The first pass said 90 sat on an efficient frontier and the A/B should stand.
**Re-run with the joins fixed and errors clustered on the market, none of that
survives.** The corrected output is `docs/adversarial/gate-dose-study.txt`, in
full, as the script printed it.

## What the corpus actually contains

    2,507  journaled fills
    1,957  with a 30s markout that is not stale and does not predate the fill
      661  also matched to a collector OBI sample (34%)
        0  taker fills, in the entire corpus

Two of those lines are load-bearing.

**Zero takers.** The ledger's own per-venue cross rate is 2-9%, so a corpus with
no forced crosses cannot show what a veto costs when the stranded inventory has
to be crossed out later. The "takers are our own crosses, the gate never sees
them" filter excluded nothing, because there was nothing to exclude.

**The markout timeline is almost entirely the wrong one.** `record_mid` was
added partway through the August run. Fifteen of the 21 journals contain *zero*
`mid` events, and in the six that do, only 11-29% of the filled tickers are
covered. So 1,651 of 1,957 markouts come from `placed` mids - the event-driven
timeline that `record_mid` exists to replace, because a fill triggers a re-quote
which writes a mid milliseconds after the event being measured. Of the 661 fills
that matched an OBI sample, **4** have a clean-timeline markout.

The bias is now measured rather than argued about. On the 306 fills carrying
both timelines, placed-derived minus mid-derived markout is **-0.000c +/-0.017**.
So the contamination mechanism is real and its magnitude is nil - on the fills
where it can be checked, which are not the fills the study rests on. That is the
honest statement, and it is the one good piece of news here.

## The join was never anchored to a real time

The journal's `at` field is when we *wrote* the event. `mid_lag_seconds` exists
to measure the gap to actual execution and is **null on all 2,507 fills**,
because no caller ever passed `executed_at`. The first pass papered over that
with a hardcoded 0.25s lookback.

That is not a detail, because the artifact runs the same direction as the
hypothesis: being filled depletes the touch we were resting on, so a book
sampled a moment late looks imbalanced against us *because* we were filled, and
the aggression that lifted us moves the mid over the next 30 seconds. So the
lookback became a sweep, with a deliberate placebo at +5s - a book that
certainly postdates the fill, where any "signal" is pure contamination:

    offset      matched   gap: markout(against >= .9) - markout(rest)
    -30.00s         640   +0.209c +/-0.876   t=0.2
    -10.00s         651   +1.477c +/-1.166   t=1.3
     -2.00s         656   -0.244c +/-1.038   t=0.2
     -0.25s         661   -0.724c +/-0.851   t=0.9
     +5.00s         663   -2.543c +/-1.304   t=1.9   <- PLACEBO

**The placebo is the strongest result in the table.** The association is three
times larger when the book is sampled after the fill than at the lookback the
first pass chose, and it is the only row anywhere near significance. That is the
contamination signature, not a forecast. `trader.py` now passes `executed_at`,
so future runs can pin the lag instead of sweeping around it.

## Nothing clears its own error bar

Clustered on the market ticker - which this project already established as
mandatory, having watched it turn -0.231c at t=-2.5 into a published -0.980c at
t=-44.4 - the dose-response is not monotone and not significant:

    bucket                 fills  mkts       mean markout   % losing
    WITH the fill >= .5      164    37    -0.213c +/-0.880       47%
    balanced                 124    27    +0.213c +/-0.796       43%
    against .20-.50           71    20    +0.309c +/-0.892       45%
    against .50-.70           56    20    -1.345c +/-2.643       39%
    against .70-.90           87    20    -0.675c +/-1.112       54%
    against .90-.95           38    12    -0.237c +/-1.058       74%
    against >= .95           121    24    -1.193c +/-0.702       60%

The first pass published four of these seven rows. The three it dropped are the
three that break the story: `.50-.70` at -1.345c is nearly as negative as the
top bucket while sitting below every level the write-up called "no worse than
average", and `WITH the fill` shows that books pointing our *way* lost money
too, which removes the directional reading entirely.

Demeaning by venue leaves the top bucket at -0.998c +/-0.551 and by venue and
price decile at -0.960c +/-0.564, both non-monotone below. So there is a
residual something at extreme imbalance, around -1c at t under 2, and no
gradient leading up to it.

## The association lives in the half with no signal

The audit established the underlying signal is one-sided: relative to a balanced
book, bid-heavy predicts +0.68c at 5s and ask-heavy +0.02c. A resting SELL is
endangered by a bid-heavy book (the supported half); a resting BUY by an
ask-heavy one. Split:

    SELL fills, bid-heavy books (the supported half)   gap +0.065c +/-1.392  t=0.0
    BUY  fills, ask-heavy books (no measured support)  gap -1.489c +/-1.029  t=1.4

**The half the signal supports shows nothing at all.** Whatever the pooled
number was measuring, it is not the mechanism the gate is built on.

## And there is no frontier at 90

    thresh  blocked    %      blocked markout        kept markout      blocked - kept
       50%      170  26%    -1.759c +/-1.119    +0.051c +/-0.432   -1.811c +/-1.069
       60%      160  24%    -1.513c +/-1.042    -0.063c +/-0.547   -1.449c +/-1.146
       70%      135  20%    -1.569c +/-1.041    -0.118c +/-0.577   -1.451c +/-1.216
       80%      113  17%    -1.758c +/-0.963    -0.137c +/-0.558   -1.621c +/-1.114
       90%       81  12%    -2.133c +/-0.774    -0.174c +/-0.542   -1.959c +/-0.969
       95%       61   9%    -2.398c +/-0.989    -0.212c +/-0.522   -2.186c +/-1.067

Selectivity is flat: every threshold from 50 to 95 blocks fills about 1.4c to
2.2c worse than what it keeps, with error bars that overlap all of them. The
first pass reported 60-70 as blocking "fills no worse than average" and 90-95 as
the selective levels. That contrast was an artifact of two things now fixed: the
sweep scored every threshold against the *factual* position path, in which
nothing was ever blocked, and it had no error bars.

The 80% row was in the first pass's output and not in its write-up. Restored, it
sits mid-range and makes the sequence read as noise, which it is.

**Also corrected:** the first pass compared its savings against "the maker's ~9c
per cycle historical edge." That figure appears nowhere else in this document
and nothing in the repo derives it. The nearest real numbers are 0.43c per fill
over 3,582 fills and +$15.06 across 7 recordings, which is roughly 215c per
recording. The comparison was meaningless and is withdrawn. The replacement
metric is deliberately not a P&L: the blocked fills' size-weighted 30s markout
runs 5.5c to 10c per journal across thresholds, and it excludes the spread
captured on those fills, the exit that still has to happen, the queue position
lost when a veto cancels a resting order, and the inventory a veto strands.

## The second lever was not one either

DOGE15M was flagged as the toxic venue at -1.354c and "a bigger effect than the
gate." Corrected, it is **-1.661c +/-1.394 across 5 markets** - t=1.19. Every
venue in that table rests on 5 to 8 markets. Only GOLD15M (+0.517c +/-0.214) and
SOL15M (+0.389c +/-0.163) clear t=2, and in the favourable direction. Dropping a
venue on 5 markets is the same mistake as publishing +1.39c from two windows.

## Where this leaves the gate

The offline study can neither support nor refute the live arm. It cannot
distinguish 90 from 50, its strongest reading is its own placebo, and the half
of the mechanism with measured backing is the half showing nothing.

The A/B was the only remaining evidence. **It has since finished, and it agrees
with the corrected offline study: nothing.**

    arm        cycles   $/cycle      SE  t vs 0    sum $   markout   fills
    obigate0        9    +0.007   0.101   +0.07    +0.06   +0.567c     401
    obigate90       9    -0.051   0.144   -0.36    -0.46   +0.434c     471

18 cycles, 2026-08-19 21:10 to 2026-08-20 01:25, balance $35.44 -> $35.95.
Welch t = 0.33 between arms. Cycles alternate, so they also pair cleanly by
time: paired difference (gated minus control) is **-$0.058 per cycle, sd 0.490,
t = -0.35**.

Two details worth more than the headline. The point estimate is *negative* on
both measures - the gated arm made less money and marked out worse (+0.434c
against +0.567c) - which is the opposite direction to the mechanism, not a
smaller version of it. And the gated arm took *more* fills than the control,
471 against 401, which is not what a filter is supposed to do.

None of that is significant. The honest reading is that 9 cycles per arm cannot
see an effect this size: pooled per-cycle sd is $0.36, so detecting a
$0.15/cycle difference at 80% power needs about **46 cycles per arm**. At
roughly 4 cycles an hour per arm that is a day and a half of continuous running,
on a $36 account, to resolve an effect that neither the offline study nor the
first 18 cycles can find any sign of.

`obi_gate.py` is unchanged in behaviour on purpose, so this result still applies
to the current code: `obi_gate_floor` (a touch-depth floor, defect #5 from
`taker_extract`) and `obi_gate_buys=0` (run only the supported half) both exist
now and both default to what the live arm ran, because changing a default
mid-flight would have silently redefined the treatment.

Worth keeping in view: this design is "v1 withhold exposed side" plus an
increases-only carve-out. v1 is in the table above at 977 fills against a
3,582-fill baseline, $3.85 captured against $15.34, and -$14.78 of inventory.
The carve-out is the entire hypothesis and no backtest of it exists. A replay of
un-vetoed fills cannot produce one, because the cost that sank v1 is the
inventory a veto strands, and there are no vetoes in the history being replayed.

## Same pass: the taker round trip is undecided again, not dead

The gate was framed as "the remaining use" of the imbalance signal, on the
grounds that buying the move had been measured to death and lost. That framing
came from `exit_fill_study.py`, and the script was wrong in the direction of its
own conclusion.

Its fill detector required the best BID to rise to our resting ASK. The ordinary
way a resting sell fills is a marketable buy consuming the ask level, after
which the book re-quotes with the bid still below us. Those are real fills, and
every one of them was scored as a forced cross carrying an exit fee that would
never have been paid. The write-up then said "these are the optimistic bound, so
the truth is worse", which had the sign of the error backwards.

Rebuilt with a two-sided bracket, one shared trigger set per grid, no window
running past expiry, no lookahead on the exit book, and errors clustered on the
market:

    ORIGINAL ARCHIVE, 598 triggers, 84 markets

    hold  rest   optimistic  conservative   realised if opt.   if conservative
     15s   20s      93%          37%       +0.559c +/-0.138   -0.375c +/-0.143
     30s   45s      93%          47%       +0.738c +/-0.194   -0.237c +/-0.196
     60s   90s      95%          49%       +1.054c +/-0.296   -0.034c +/-0.304

Upper bound clearly positive at t around 4, conservative bound between -0.4c and
zero. Recorded books cannot narrow that further, because snapshots cannot see
trades. The candidate is bracketed and undecided, which is exactly what the
script's design note said it would be before the detector buried it.

The recovered corpus agrees on the shape and not the size: 136 triggers across
19 markets, optimistic +0.34c to +0.59c, conservative -0.40c to -0.82c. Same
bracket straddling zero, weaker, and small enough that it settles nothing.

**The direction split looked like the bigger prize and does not replicate.** On
the archive, at hold 30s / rest 45s, bid-heavy triggers realise +1.122c +/-0.411
against ask-heavy at +0.439c +/-0.138, and on a forced cross bid-heavy is
+0.473c +/-0.611 while ask-heavy is -0.550c +/-0.239. That is the pattern the
audit's one-sidedness finding predicts, on 84 markets, and it is the most
interesting thing in the run.

On the recovered corpus the two sides are indistinguishable: +0.502c +/-0.628
bid-heavy against +0.464c +/-0.406 ask-heavy in the same cell, on 8 and 11
markets. Eight markets cannot refute anything, so this is not a contradiction so
much as no second reading. It is exactly the position the "three periods, no
survivor" chapter was in, and the lesson from that chapter applies here: a
split found in one period and absent in an underpowered second one is a
hypothesis, not a finding. Worth pre-registering before the next live run.
Not worth restricting a strategy on today.


---

# Closed, this time with the exploit run (2026-08-20)

The measurement program ended with one fair objection standing: every session
had been spent measuring, never exploiting. So the last run stacked everything
learned into the single best configuration and just traded it - commodity
venues only (GOLD, SOL, WTI, SILVER; DOGE and the crypto majors excluded on
their measured toxicity), OBI gate at 90, passive exits, reduce-only into the
close, 1-lot, $25 floor.

29 cycles:

    cash        -$1.64 ledger / -$0.56 balance ($36.01 -> $35.45)
    per cycle   -5.7c +/- 8.8c  (t = -0.64, statistically zero)
    markout     positive in 28 of 29 cycles, ~+0.4c per fill

The terminal signature of the whole project, reproduced one last time in its
best configuration: **the paper says a small edge, the cash says zero.** The
markout is real - the fills are slightly good - and everything between fill and
bank (inventory carry, exit costs, settlement) eats exactly all of it. Not
dramatically, not through any single leak left unfixed. Just, on net, to zero.

Stopped by choice at 29 cycles, well above the floor. Final balance **$35.45 of
the original $50**: the complete program - two audits, a recovered corpus, four
hostile reviews, a live fill-quality test, a randomised exit experiment, a gate
A/B, and this run - cost $14.55.

What the money bought, since it was never going to be the returns:

- **A validated signal.** Order-book imbalance forecasts the next 30 seconds at
  +0.6 to +0.9c against a flat control, monotone across 640 markets, robust to
  every adversarial attack including three we could not think of ourselves.
- **A measured "no".** The signal is worth less than any available round trip
  on this fee schedule, at this scale, in every construction tried - and each
  "no" is a number with a standard error, not an opinion.
- **The instrument.** Extract -> dose-response with a no-signal control ->
  placebo null -> power-checked replication -> guarded live test. It re-answers
  "is there an edge here?" for any order-book venue in an evening, and it is
  packaged as the `venue-audit` skill.

The account keeps the $35.45. The map is the deliverable.


## Amendment, same day: the overnight audit reopens the taker question

The closing chapter above says the program produced "a measured no in every
construction tried". Hours later a hostile audit of the two newest studies
(`9e9123f`) corrected that, and the correction goes against my own conclusion,
so it gets the loudest placement I can give it:

- **The exit-fill study was biased toward its own verdict.** Its fill detector
  required the best bid to rise to our resting ask, which misses the ordinary
  way a resting sell fills - a marketable buy consuming the level. Every missed
  fill was scored as a forced cross with a fee never actually paid. Rebuilt
  two-sided, the KXBTCD 2-5c near-expiry round trip is **bracketed and
  undecided**: +0.6c to +1.1c if fills are read optimistically, -0.4c to 0.0c
  conservatively, and recorded books cannot narrow it because snapshots cannot
  see trades.
- **The gate dose study partly measured our own fills.** The fill stamp was a
  write time, the OBI lookback was hardcoded, and a +5s placebo comes back
  stronger than the signal reading. With clustered errors nothing clears its
  bar. The gate's offline support is withdrawn; the live A/B result (harmless,
  unmeasurable) stands on its own.

So the honest final state is narrower than either verdict written before it:
the **maker** program is measured to zero, including the exploit run above; the
**taker** cell is an open bracket straddling zero with exactly one decidable
experiment left - `docs/pre-registration-exit-fill.md`, 160 pre-registered
1-contract round trips, ~$8 of maximum risk, decision rule fixed before any
order is sent. Whether to run it is a capital decision, not an engineering one,
and trading is currently stopped by choice.

The project's own lesson applied to itself one last time: the error that
survived longest was the one that flattered the author - in this case, me,
because "conclusively dead" closed the story cleanly and "undecided" does not.
