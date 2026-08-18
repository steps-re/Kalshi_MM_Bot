# Evaluating a market-making opportunity on any venue

Distilled from two days on Kalshi's 15-minute crypto windows, during which more
findings came from fixing our own instruments than from the market. The order
below matters: each step invalidates work done after it, so doing them out of
order wastes the later steps.

## 1. Read the fee schedule off the ledger, never the docs

Kalshi's documentation described a per-order ceiling that does not exist and
said nothing about makers trading free - the two facts that decide everything.
Both came from placing 1-contract orders and reconciling against
`/portfolio/fills`, for under a dollar.

**Controls are mandatory.** A zero maker fee means nothing unless taker fills in
the same sample were charged: a broken fee reader and a free market produce
identical output. Ours read `$0.00` for a whole session because a field had been
renamed. The exchange's billing record is the only authority.

Reusable: `market/fees.py` (`calibrate_from_fills`), `api/parser.py`
(`parse_fill_fee_micros` - unreadable is None, never zero).

## 2. Measure data resolution before trusting any backtest on it

Polled REST books carried **11.8%** of true level shrinkage versus the
websocket feed on the same market at the same time: a level that trades and
refills between samples reads as unchanged. The same strategy filled 942 times
on feed data and 13 times on the polled copy. Every conclusion drawn from the
polled book was measuring the collector, not the market.

Also: align recording to the instrument's lifecycle. Fixed-clock cycles gave a
book with zero coverage of the half of each window where the edge lives.

Reusable: `scripts/sweep_backtests.py` `measure_resolution` (per-ticker, judged
on the best-recorded market, warns instead of quietly reporting).

## 3. Queue reachability decides the tradable universe, and cancels count

Wide spreads in dead markets are wide because they are dead. Screen on time to
reach the front of the queue - depth at touch over flow, where flow is measured
over the market's *own life*, never a 24-hour average - and remember that **84%
of book shrinkage is cancellation** (measured off the delta feed), which
advances your position just like a trade. Dividing by traded flow alone
overstated waits 6x and rated our best market untradable.

On Kalshi this screen leaves exactly two books. That number IS the capacity
answer, and it caps the strategy harder than any parameter.

Reusable: `analytics/suitability.py`, `scripts/capacity_scan.py`,
`scripts/calibrate_fills.py` (trade fraction from deltas + public trades).

## 4. Fill quality is regime-dependent - measure the decay curve live

Markout by time-to-expiry, from ~800 live 1-contract fills: +0.41c early,
+0.06c late, negative in the final two minutes. The remaining traders near
expiry are the ones who know the answer. No simulator showed this; a $5 live
sample did. Gate the strategy on the regime (`strategy/phase.py`, reduce-only,
never withhold-one-side - one-sided quoting is a directional bet).

Reusable: `live/journal.py` (records placement book, depth ahead, mid at fill
*with its lag stated* - a markout with no horizon compares to nothing),
`scripts/analyze_trials.py`.

## 5. Reconcile the simulator against live before ranking anything

Our fill model filled orders resting *behind* the touch - 71% of its queue
fills were at prices the market never reached, which is why simulated markout
ran 2.4x live and why every strategy ranking made on it was provisional. After
requiring reachability, the residual gap (~2x) is adverse selection proper:
**~0.25c per fill** the simulator cannot see because it does not know why
anyone traded. State the haircut on every report; never silently scale.

Reusable: the reachability check in `sim/fills.py`, the sim-vs-live warning in
the sweep, `sim/adverse.py` as a documented negative result (fill *selection*
cannot reproduce adverse selection; fill *eligibility* was the bug).

## 6. Only the account is ground truth

Markout implied +$2 on a day the account lost $4.70. The gap was inventory,
exit cost, and nineteen positions left to settle on binaries. The session
runner exists to close it: trade the measured regime, flatten every cycle by
crossing (pay to be flat; measure the cost), report balance-before/after next
to markout every cycle. When the two track, the edge is real; when they
diverge, the divergence is the finding.

Reusable: `scripts/session_runner.py`, `live/campaign.py` (premise monitor:
UNKNOWN halts, zeros need controls, PENDING is time-bounded, favourable moves
are surfaced but never auto-acted-on).

## 7. Operational limits are findings

Kalshi's order rate limit 429'd a session in fourteen seconds at the strategy's
natural cadence. Any fill-rate measured without that throttle was partly
fiction. Discover the rate limits *before* interpreting fill statistics.

## Failure patterns that recurred (the real transferable asset)

1. **Silent defaults manufacture results.** A missing fee field defaulting to
   "0" confirmed the hypothesis we most wanted. Unreadable must stay distinct
   from zero, everywhere.
2. **Small samples flatter.** Overall markout, the phase threshold, and the
   momentum magnitude all shrank as n grew - never once did more data improve a
   result.
3. **Enumerate what must fail closed, not what may be forgiven.** Allowlists of
   transient errors, denylists of bad markets, and fixed strategy choice lists
   each broke; the short list is always the permanent-failure list.
4. **Check output against an independent source, not the code against itself.**
   Every major bug (fee reader, resolution, alignment chain, reachability) was
   found by comparing two measurements, none by reading code.
5. **Two conventions for one concept is a standing bug generator.** REST vs
   websocket ask encoding caused three broken analyses; it now lives in one
   module (`market/bookio.py`) with the live payloads pinned in tests.

## Where this applies next

The machinery is Kalshi-shaped only at the API edges. The evaluation pipeline -
ledger-truth fees, resolution gates, queue reachability, live decay curve,
sim-vs-live reconciliation, account-truth sessions - applies unchanged to
Polymarket (maker rebates make step 1 favourable there), sports books in-play,
or any venue with resting orders and a public book. The cost of a full
evaluation, measured: about $7 of live fills and a few days of instrument
debugging. Budget for the instruments.
