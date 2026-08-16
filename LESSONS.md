# Lessons from hardening this bot

Notes for Nate, from the Steps Ventures fork. Written as a code review that got
long, because the interesting parts were not in the code.

Short version of the verdict first: **the engineering here is good.** The
fixed-point money handling is disciplined, the websocket sequencing is careful,
the order manager namespaces its own orders and sweeps them on shutdown, the
self-orders are removed from the book before the strategy sees it, and 78 tests
passed on a clean checkout. Most trading-bot code written by people who trade
for a living is worse than this.

What follows is not a list of things you did badly. It is a list of things that
are invisible from inside a backtest, which is exactly where a good backtest is
most dangerous.

---

## 1. The one number that decides everything

Kalshi's trading fee is

```
fee = round_up_to_next_cent(0.07 × contracts × P × (1 − P))
```

applied **per order**. At $0.50 that is 1.75c per side, **3.5c for a round
trip**, against a tick of 1c.

Sit with that. The minimum price increment on the exchange is one cent. The fee
to buy and sell one contract at the midpoint is three and a half cents. A market
maker who captures the **entire** bid-ask spread of a 2c-wide market at $0.50
loses 1.5c per contract per round trip.

There is no parameter that fixes this. Not spread, not size, not inventory
skew, not requote logic. It is arithmetic.

This is why your live runs saw fees eat everything. It was not an execution
problem or a tuning problem. It was the market selection.

### Where it *does* work

`P × (1 − P)` is a parabola that peaks at $0.50 and collapses toward both ends.
At $0.05 the round-trip fee is 0.68c; at $0.50 it is 3.50c. Five times cheaper.

Viable YES price bands, by how wide the market is quoted:

| quoted spread | where the spread covers the round-trip fee |
| ------------- | ------------------------------------------ |
| 1c – 3c       | at/below $0.077, or at/above $0.923        |
| 4c            | below $0.17 or above $0.83                 |
| 5c            | below $0.31 or above $0.69                 |
| 6c and wider  | anywhere                                   |

One subtlety worth internalising, because it caught me out too. A market
already quoted **one tick wide cannot be improved** — there is nowhere to stand
between the bid and the ask. So you join, and you capture the whole spread. My
first version of this screen assumed "give up a cent per side to get filled" and
applied it everywhere, which subtracted liquidity that was never there and
declared every tick-wide market dead. That is why 1c and 3c land in the same
band above: at 1c you join and keep all of it.

### The per-order ceiling is a fixed cost

The fee rounds **up to the cent, per order**. One contract at $0.50 owes
$0.0175 and pays $0.02. That 14% surcharge does not scale with size, so it
behaves like a fixed cost:

| contracts | round-trip fee | breakeven edge |
| --------: | -------------: | -------------: |
|         1 |        $0.0400 |          4.00c |
|        10 |        $0.3600 |          3.60c |
|       100 |        $3.5000 |          3.50c |
|      1000 |       $35.0000 |          3.50c |

Two consequences. Small live tests read *worse* than the strategy really is,
because you are paying the rounding on every order. And there is a **minimum
economically viable order size** below which no quote can win — implemented as
`KalshiFeeModel.minimum_viable_count`.

### The caveat that could invert all of this

Everything above assumes the 0.07 formula applies to **maker** fills. Kalshi has
had a separate flat per-contract maker fee on some markets. If your account is
billed that way, midpoint market making becomes viable and this entire section
is wrong.

I did not guess. `market/fees.py` has `calibrate_from_fills`, which reconciles
the model against the `fees_paid` Kalshi already reports on real executions.
Your existing fills contain the answer. **Run this before trusting any backtest,
including the fixed one.** It is one session of data and it decides the whole
thesis.

---

## 2. Why your backtest could not have told you this

`SimPortfolio.apply_fill` computed:

```python
signed_cash = -direction * fill.yes_price * fill.count
```

and stopped. **No fee, anywhere in the simulator.** `fees_paid` was parsed off
the wire in `api/parser.py` and never read.

So every backtest reported gross P&L while every live fill paid. That alone
would be a bad day. What made it worse was the optimizer.

`_trial_sort_key` ranked trials by objective, then broke ties on
**`volume_count`**. Among parameter sets with equal (gross, fee-free) P&L, it
preferred the one that traded the most. Every extra round trip is another fee
you were not charging and another chance to be adversely selected. The optimizer
was not merely blind to the cost — it was **actively selecting for the
configuration that maximised it.**

This is the most useful thing to take from the whole exercise: *a backtest
optimises whatever you measure, so anything you do not measure gets spent
freely.* An unmodelled cost is not neutral. The search will find it and pour
your money into it.

Fixed: cash is net of fees, ties break toward ending flat, and the default
objective is net liquidation.

### Two other ways the P&L was flattering

**Inventory was marked at mid.** If a session ends holding 100 contracts, mid is
the price at which nobody has agreed to trade with you. `liquidation_value` now
walks the actual book — 100 contracts against 1 contract on the bid do not all
get the bid — and charges the exit fee. The gap between the two marks is
reported as `inventory_mark_gap`.

**A position the book could not absorb was silently dropped** from the
valuation. For a short that erases the liability entirely and reports the
account as richer than it is. Unfillable inventory is now marked at the worst
case.

---

## 3. What a market maker is actually risking

The strategy modelled inventory risk with a constant (`inventory_skew`). That is
a reasonable first pass, but it misses the two things that actually hurt.

### Adverse selection

Your quote rests. Someone trades against it. The question that matters is: did
they know something you did not? If the mid moves against you right after every
fill, you are being picked off, and no amount of spread saves you — the informed
trader simply waits for a bigger edge.

The right size for your quote's edge is roughly

```
σ × √(how long the quote will rest)
```

where σ is the *instantaneous* volatility of the mid. That is measurable from
the book (`market/dynamics.py`), so the same parameters behave sensibly in a
quiet market and a violent one without special-casing either.

**Markout** is how you measure whether it is happening: for a fill at price `p`
and direction `d`, `d × (mid(t+h) − p)` at several horizons `h`. A market maker
expects this to be positive. Read the markout **before** the P&L — over a ten
minute session the P&L is noise and the markout is not.

### Inventory risk is not one thing

Far from expiry, a position is a diffusion you can flatten out of; the risk is
`σ√(time to flatten)`. Near expiry, flattening stops being a choice, and the
position becomes a coin flip you are stuck holding — worth `√(P(1−P))` in risk
terms, which is *maximal at $0.50*.

`MarketSnapshot.inventory_sigma_ticks` interpolates between the two based on how
much time is left relative to how long a flatten takes.

---

## 4. Your end-of-market observation, generalised

You noticed the last minutes of a 15-minute BTC market behave differently. You
were right, and it is worth being precise about *why*, because "minutes
remaining" does not port to a market that settles next Tuesday.

The clock is not the cause. Two things change together:

1. **σ spikes.** As expiry approaches, the payoff steepens — each tick of the
   underlying moves the probability further. The same BTC move that shifted the
   price 1c an hour ago shifts it 10c now.
2. **Time to flatten grows** while your ability to get out shrinks.

So adverse selection and inventory risk both explode, for reasons that are
measurable rather than calendar-based. Which means the control generalises:
estimate σ from the book, blend inventory risk toward the binary payoff as the
close approaches, and the same parameters work on a 15-minute crypto strike and
a month-long election market.

The strategy now moves through phases: taper size → widen quotes → **reduce
only** → flatten across the spread. And `seconds_to_close` is `None` whenever
the close time is unknown, treated as "no deadline" and never as "closing now" —
an older recording must not make the bot panic.

To check the theory rather than assume it, `markout_by_time_to_close` buckets
markout by time remaining. That turns your impression into a number, and it will
tell you exactly which window to stop quoting in.

**Reduce-only had a nasty bug worth flagging**, found by an adversarial review.
It restricted *direction* but sized off position *capacity*. With a long of 1
contract and a max position of 1000, "reduce only" could sell 1000 and leave you
900 short — the largest position of the session, created by the risk control, in
the final minutes. It now clamps to the position being reduced. Risk controls
need tests that try to break them, not tests that confirm them.

---

## 5. Optimising on one recording finds noise

`optimize_adaptive_backtest` searched up to 250 parameter combinations against a
single recording and returned the best. On one ten-minute session, the best of
250 is mostly a description of that recording's noise. You would get an
impressive-looking winner from a shuffled P&L column too.

The fix is boring and non-negotiable: **fit on some data, score on data the fit
never saw.** `sim/validation.py` does expanding-window walk-forward and reports
both numbers. The gap between them is your honest estimate of how much was real.

It also defaults to selecting the parameter set with the best **worst**
recording rather than the best total, because a set that made everything back on
one lucky replay is precisely what we are trying not to ship.

---

## 6. What the exchange actually offers

I scanned every open market (2026-08-16). Two things worth knowing before you
scan it yourself:

- Kalshi floods `/markets` with auto-generated multivariate parlay combos.
  **248,501 of 251,000 records** were combos, essentially none quoted. Filter on
  `mve_collection_ticker` or they crowd out every real market.
- The public market-data endpoint needs **no auth**, and its fields are
  `yes_bid_dollars` (decimal string) and `volume_24h_fp` (fixed point) — not the
  integer cents the older docs imply. I got this wrong first and every market
  parsed to a zero bid, which looked like a finding rather than a bug.

Results, of ~2,500 real markets:

- 250 had two-sided quotes and 24h volume of 50+ contracts
- **78% of those were structurally unviable** — fee exceeds spread
- 54 cleared the fee, worth an estimated **$64/day of net edge in total** at 10%
  participation

| family              | viable | est. $/day |
| ------------------- | -----: | ---------: |
| MLB player props    |     36 |      50.36 |
| Temperature markets |      9 |       7.97 |
| Soccer totals       |      6 |       5.16 |

**Crypto hourly strikes do not appear at any volume threshold.** Tick-wide
spreads at midpoint prices is the single worst cell in the table, and it is
where the testing was pointed.

Treat $64/day as an upper bound, not a forecast — it assumes capturing the
quoted spread on every round trip, which is exactly what adverse selection
erodes.

### That number is not a size limit

Order size does not enter the estimate at all:

```
daily = net edge per contract × (market volume × our share) / 2
```

Both terms are set by the market:

- **Volume in fee-viable markets: 81,717 contracts/day**, out of 453,545 across
  all liquid markets. That is the entire pool.
- **Volume-weighted net edge: 1.56c per contract** — what survives the fee.

Trading bigger only helps insofar as it wins a larger *share* of a fixed pool,
and the share cannot exceed 100%:

| share of volume intermediated | net edge |
| ----------------------------: | -------: |
|                            5% |   $32/day |
|                           10% |   $64/day |
|                           25% |  $159/day |
|                           50% |  $318/day |
|                          100% |  $637/day |

100% means being the counterparty to every single trade in every fee-viable
market on the exchange. It is a ceiling, not a plan.

Nor is there a hidden pool further down. Dropping the volume threshold from 50
contracts to zero adds 579 more viable markets and about **$3.50/day** — the
long tail is viable but empty.

### The lever that actually moves it

| fee schedule | viable markets | addressable volume | net edge @10% |
| --- | ---: | ---: | ---: |
| Taker 7% both sides (what we assume) | 54 | 81,717 | $63.66/day |
| Maker $0.0025/contract, taker exit | 121 | 235,852 | $82.68/day |
| Zero fees (theoretical ceiling) | 250 | 453,545 | $291.46/day |

Under the taker schedule we can address **18%** of the liquid volume on the
exchange. Under a flat maker fee that rises to **52%**, because the midpoint
markets — where nearly all the volume is — come back into play.

**Capital is not the constraint. The fee schedule is.** Which is why calibrating
it against real fills belongs first, not last.

Run it yourself: `python scripts/screen_markets.py --prod --min-volume 50`.

---

## 7. Habits worth stealing

**Compute published numbers, never type them.** Every figure in this document
and in the app comes out of `market/fees.py` at build time. Typed numbers drift
from the code the moment either changes.

**Make the pessimistic assumption the default.** The fee model charges the taker
rate on every fill because that is the direction of error we want in a system
that decides whether to risk money. Optimism belongs behind a flag.

**Unknown must not mean zero.** The volatility window used to retain a minimum
number of samples regardless of age. After a feed gap, those stale samples
spanned the whole outage, so the variance denominator was enormous and σ
collapsed toward zero — telling the strategy the market was *calm* at the exact
moment it had no idea what was happening, and quoting the tightest spread of the
session. Now the window trims strictly by time, and the strategy widens to a
pessimistic fallback when it has no estimate.

**Silence is the dangerous state.** Resting orders do not cancel themselves when
the websocket stalls. A quiet feed means you are showing prices from a book that
may be seconds old, and everyone whose feed still works can see it.
`max_feed_silence_seconds` in `live/risk.py` is the most important limit there,
and it is the one nobody thinks to add.

**Test the invariant, not the implementation.** Several of my first tests
asserted "the strategy refuses to quote here" and failed — because it quoted
*wider* instead, which is better. The right assertion was the economic one: the
two quotes together must be at least the round-trip fee apart. Tests that encode
behaviour break when behaviour improves; tests that encode invariants do not.

**Get someone else to attack it.** I had two other models review this code cold.
Eight findings held up, including the reduce-only reversal bug. I also found two
they missed by checking my own claims against the live exchange — one of which
(the tick-wide improvement assumption) had me telling Mike the wrong answer for
an hour. Reviewing your own code has a ceiling.

---

## 8. What I would do next, in order

1. **Calibrate the fee model** against real fills. One session. It decides
   whether any of the rest matters.
2. **Point the recorder at the viable markets** — MLB props and temperature, not
   crypto strikes — and collect several sessions.
3. **Read the markout before the P&L.** If markout is negative at every horizon,
   fix that before touching anything else.
4. **Walk-forward** across the sessions. If out-of-sample retention is near
   zero, there is no edge yet and more tuning will not create one.
5. Only then go live, small, with `RiskLimits.conservative()`.

And always compare against `dumb`. A strategy that has not beaten a baseline has
not been shown to do anything.

---

## Where things live

| What | Where |
| --- | --- |
| Fee model, calibration | `src/kalshi_mm_bot/market/fees.py` |
| Volatility, microprice, imbalance | `src/kalshi_mm_bot/market/dynamics.py` |
| The new strategy | `src/kalshi_mm_bot/strategy/horizon.py` |
| Fee-aware accounting, book-walking exit | `src/kalshi_mm_bot/sim/accounting.py` |
| Walk-forward validation | `src/kalshi_mm_bot/sim/validation.py` |
| Markout | `src/kalshi_mm_bot/analytics/markout.py` |
| P&L attribution, drawdown | `src/kalshi_mm_bot/analytics/performance.py` |
| Market screening | `src/kalshi_mm_bot/analytics/screening.py` |
| Risk limits, kill switch | `src/kalshi_mm_bot/live/risk.py` |
| Regression tests for the review findings | `tests/test_review_fixes.py` |

The interactive version of this write-up, with a live fee calculator, is the
Streamlit app in `analysis_app/`.
