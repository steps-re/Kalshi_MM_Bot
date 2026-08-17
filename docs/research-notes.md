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
