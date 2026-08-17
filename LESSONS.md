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
fee = round_up(0.07 × contracts × P × (1 − P))
```

applied **per order**. At $0.50 that is 1.75c per side, **3.5c for a round
trip**, against a tick of 1c.

(Kalshi's docs say that rounding goes up to the next cent. It does not — see
"the cent ceiling does not exist" below. And the whole formula turns out to
apply only to takers.)

Sit with that. The minimum price increment on the exchange is one cent. The fee
to buy and sell one contract at the midpoint is three and a half cents. A market
maker who captures the **entire** bid-ask spread of a 2c-wide market at $0.50
loses 1.5c per contract per round trip.

No *quoting* parameter fixes this. Not spread, not size, not inventory skew, not
requote logic. It is arithmetic.

There is exactly one thing that fixes it, and it is not a parameter: **never
cross.** That turned out to be the whole ballgame, and it is measured two
sections down.

This is why your live runs saw fees eat everything **whenever you crossed the
spread**. It was not an execution problem or a tuning problem. It was paying the
taker fee on a 1c tick. Read on, though: resting orders are billed differently,
and that changes the conclusion.

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

### The caveat that inverted all of this

Everything above assumed the 0.07 formula applies to **maker** fills. It ended
with a warning that if Kalshi bills makers differently, "this entire section is
wrong."

**It does, and it was.** Measured on a live account, 63 fills:

| | fills | total fee | mean per fill |
| --- | ---: | ---: | ---: |
| **maker** (resting) | 25 | **$0.0000** | $0.0000 |
| **taker** (crossing) | 48 | $0.5879 | $0.0127 |

Zero. Not reduced — zero. Confirmed at midpoint prices, where the fee is
largest, in two independent series:

```
KXBTC15M   maker @ 0.50 -> $0.0000    taker @ 0.52 -> $0.0175
KXETH15M   maker @ 0.59 -> $0.0000    taker @ 0.62 -> $0.0165
```

**This is the explanation for both halves of your experience.** You made a
dollar or so resting orders, and you watched fees eat everything when you
crossed. Same market, opposite sign, decided entirely by whether the order rests
or crosses. At the midpoint one cross costs 1.75c against a 1c spread — you
cannot cross your way out of a position and keep the edge.

So the "3.5c round trip" above is the cost of crossing **both** ways. Rest both
ways and it is zero. Everything in the fee-viability screen still holds for a
taker; for a pure maker, the price-band table stops binding.

Do not read that as "midpoint market making is free money." It moves the binding
constraint rather than removing it: what decides profitability now is queue
position and adverse selection, which is where the rest of this document goes.

### Two bugs worth more than the result

The way that measurement went wrong is more useful than the number.

**The fee reader was broken, and it lied in our favour.** Both measuring scripts
asked for `fees_paid_dollars` with a `"0"` default. The REST fills endpoint
returns `fee_cost`. So every fill on the ledger came back free, and a full
session reported "NO MAKER FEE CHARGED" from a reader that could not have said
anything else. It survived because it confirmed the hypothesis we wanted.

What exposed it was the **takers** also reading zero, which the formula says is
impossible at 53c. Had the bug flattered only the maker side, it would still be
in place.

The habit that follows: **a zero needs a control.** A maker total of $0.00 means
nothing unless taker fills in the same sample were charged something, because a
broken parser and a free market are the same observation. That check is now
enforced in code, and the run prints `control: N taker fill(s) charged $X, so
the reader works` or refuses to conclude. An unreadable fee is recorded as
unknown and never summed as zero.

**The cent ceiling does not exist.** Kalshi documents rounding the per-order fee
up to the next cent. Its ledger rounds up to $0.0001. Across 48 taker fills the
raw formula predicts $0.5860 and we were charged $0.5879; the cent ceiling would
have charged $0.8700 — a **48% overstatement**.

That error hid because it only bites at small sizes: a ceiling to a whole cent
is nearly the entire fee when the fee is under two cents, and disappears on an
order of a few hundred contracts. Every live test used one contract, which is
the one regime where it mattered most.

Note the directions. The reader bug made trading look better than reality; the
ceiling bug made it look worse. Both came from trusting a description instead of
the ledger. **The exchange's own billing record is the only authority on what
the exchange charges** — not its documentation, and not our model.

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

**This section was wrong twice before it was right, and both errors had the same
cause: I reported an incomplete scan as if it were the whole exchange.** Worth
reading for that alone.

### The corrections

My first scan paged the `/markets` endpoint and stopped early. It returned 2,504
markets, contained **zero crypto markets**, and I wrote that "crypto hourly
strikes do not appear at any volume threshold" — presenting an artefact of my own
truncation as a finding about the world. The right conclusion from that data was
"my scan is incomplete", and I did not reach for it because the wrong conclusion
was more interesting.

The complete scan goes through `/events` (which returns markets nested under
events and skips the parlay combos entirely) rather than `/markets`:

| | first scan | complete scan |
| --- | ---: | ---: |
| real markets | 2,504 | **84,181** |
| liquid & two-sided | 250 | **8,061** |
| daily volume | 453,545 | **66,020,772 contracts** |
| clearing the fee | 54 | **3,519** |

The first scan covered about **3% of the exchange**. The fee arithmetic in the
sections above was never wrong; the population I applied it to was.

### The real picture

Of 8,061 liquid markets carrying 66.0M contracts a day, under the *pessimistic*
assumption that the taker rate is charged on both sides:

- **3,519 markets (44%) clear their own fee**
- those carry **17.4M contracts a day (26% of volume)**

By distance from the end of the price range:

| band | viable | hit rate | 24h volume | volume clearing |
| --- | ---: | ---: | ---: | ---: |
| deep tail (<=5c from an end) | 1,196 / 1,352 | 88% | 18.9M | 12.9M |
| tail (5-15c) | 986 / 2,085 | 47% | 13.6M | 2.2M |
| shoulder (15-30c) | 622 / 1,960 | 32% | 13.7M | 0.9M |
| near the money (30-50c) | 715 / 2,664 | 27% | 19.7M | 1.4M |

The tails dominate, exactly as the fee curve predicts. But note the hit rate is
not zero anywhere: a market near the money clears the fee whenever its spread is
wide enough. **The rule is spread versus fee at that price, not "only trade the
tails".**

By category, Sports carries most of Kalshi's volume (54.9M of 66.0M contracts)
and therefore most of the opportunity. Crypto is present and partly viable — 93
of 187 liquid crypto markets clear the fee — and within each BTC or ETH strike
ladder it is the **wings** that clear and the at-the-money strikes that do not.
So the original instinct to trade BTC was not wrong. The strike selection was.

### On the dollar figures

At 10% participation the fee-permitted edge across those markets is roughly
$30k/day. **Do not treat that as an opportunity estimate.** It assumes capturing
the quoted spread on every round trip with no adverse selection, and it
concentrates in wide-spread, thinly traded markets where the participation
assumption is weakest. It is a ceiling on what the fee structure permits, not a
forecast of what a strategy earns. What separates the two is markout.

### The other venue

Polymarket is the only other venue with open enough data to screen. Its fee
schedules are worth reading carefully, because **every one of them sets
`takerOnly: true`** — makers pay nothing and receive a rebate of 15-25% of the
taker fee. Its minimum tick is 0.1c on more than half of active markets, ten
times finer than Kalshi's.

Run the same screen there and *every* liquid market passes. That is not a
$50k/day opportunity; it is the screen reporting that it has stopped being the
binding analysis. Where there is no fee wall, the constraints are adverse
selection and competing with market makers who are already good at this —
neither of which this screen models.

Which reframes the whole exercise: **the fee wall is a property of one fee
schedule, not a law of prediction markets.** And it makes the Kalshi maker-fee
question decisive rather than a footnote. Kalshi's published schedule charges
takers, and public summaries of it say most standard markets carry **no maker
fee at all**. If that is what this account is billed, Kalshi looks far more like
Polymarket than like the pessimistic table above, and most of the exchange opens
up.

One session of real fills, run through `calibrate_from_fills`, settles it.

### Two API traps worth knowing

- The public market-data endpoint needs **no auth**, but its fields are
  `yes_bid_dollars` (decimal string) and `volume_24h_fp` (fixed point), not the
  integer cents older docs imply. Reading the wrong field silently produced a
  zero bid for every market and looked like a finding rather than a bug.
- `/markets` is flooded with auto-generated multivariate parlay combos —
  248,501 of the first 251,000 records — none of them quoted. `/events` with
  `with_nested_markets=true` avoids them entirely and is roughly 50x faster to
  page. Use `/events`.

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
