# CLAIMS AUDIT (settlement calibration pipeline)

Adversarial review of `scripts/calibration_at_t.py`,
`scripts/calibration_diagnostics.py`, `scripts/settlement_candles.py` and
`analysis_app/build_exchange_census.py`, 2026-08-20. Reproduced against
`~/kalshi-audit/*.jsonl` and the shipped `analysis_app/data/exchange_census.json`.

Nine of the eleven directional findings ran the author's way. Six inflated the
edge. **Three supported the CONCLUSION rather than the profit** - which is the
group worth sitting with, because this project's stated payoff is the decay
curve, the "transferable test", and that test turned out to be the artifact
with the least support in the codebase.

---

## A. Sampling and filtering that could manufacture the result

### A1. The page's headline table and the page's generated table came from different samples and disagreed on the SIGN of the headline number

The decay table typed into `anatomy.py` reproduced exactly - all five rows - from
`candles_tennis.jsonl` alone. The `st.dataframe` rendered forty lines above it
came from all four candle files.

| tennis fave BUY @ T-60 | n | losses | net | se | t |
|---|---|---|---|---|---|
| typed on the page (tennis file) | 533 | 50 | **-0.57c** | 2.21 | -0.3 |
| `candles_breadth.jsonl` | 451 | 27 | **+3.58c** | 2.24 | +1.6 |
| shipped JSON, same page | 985 | 77 | **+1.35c** | 1.87 | +0.7 |

"At 60 minutes the trade loses money" rested on **t = -0.3**, published without
an error bar, from one of two samples, where the other gives +3.58c.

**Fixed.** Every number on the page is now generated. `calibration_core` holds
one definition of the trades so the printing script and the site builder cannot
diverge again - they already had, disagreeing on whether a mid of exactly 5c is
a tail (`hi <= 0.05` vs `mid <= 0.05`; the looser one shipped).

### A2. The decay curve compared disjoint populations

A market enters a horizon only if it had an actionable book that far out AND sat
in the zone then. Tennis tail SELL overlap with the T-10 sample: 14% at 2m, 43%
at 5m, 21% at 30m, **6% at 60m**.

**Fixed.** `decay_panel` reports `overlap_with_longest` (Jaccard) per row and a
BALANCED panel holding the market set fixed. On the corrected data the balanced
panel collapses to a handful of markets and the slope **reverses**. Neither
reading survives: the dataset cannot identify the decay. The balanced panel is
itself conditioned on the outcome and is labelled a diagnostic, not an estimate.

### A3. The decay test cannot be run on crypto-15M at all

A 15-minute market has no book an hour before close, so crypto-15M silently
vanished from the 30m and 60m rows while the page concluded "every tail trade in
this study fails it". Untested is not failed.

**Fixed.** `missing_lookbacks` is reported and rendered as an explicit error
block.

### A4. `book_at` had no staleness limit

It took the last candle at or before T-minus-X at any age, so a market that
stopped printing an hour earlier handed back that hour-old book as "the price two
minutes before close". Measured: **25.6% of all book reads were stale by >3
minutes, 6.4% by >30 minutes**; 52.4% of weather reads and 40.8% of sports-props.

**Fixed.** `MAX_STALENESS = 180`.

### A5. `gap` was measured against the bucket centre

Prices pile toward the low end of every tail bucket, so the centre-based gap
overstated tail overpricing by up to **+0.44c**, in the flattering direction, in
every tail bucket - with `avg_bid`/`avg_ask` already in hand.

**Fixed.** The reference is the average mid actually observed.

### A6. Tradeable breadth was measured with the statistic the project declared unusable

`tails`/`faves` were counted from `last_price_dollars`, the converging last
print that `calibration_curves.py` exists to warn about.

**Partly fixed.** Renamed `tails_lastprice`/`faves_lastprice`, and a
`breadth_basis` caveat is emitted and rendered as a warning. Measuring breadth
at a tradeable moment needs candles for all 421 series and has not been done.

### A7. Duplicate markets across candle files

1,239 tickers appear in more than one file. Effect was small (t 10.9 -> 10.3).

**Fixed.** `load_records` deduplicates and reports.

---

## B. Claims the code did not support

* **"one number each, not sixteen buckets to pick from"** - two pre-specified
  trades printed across 86 cells, from which the narrative then selected. Fixed:
  `cells_tested` and a multiplicity note are emitted and shown.
* **"The taker column is not [an upper bound]"** - candlesticks carry no depth;
  a one-lot phantom bid at 4c weighs the same as a real book. Fixed: the
  docstring says both columns are upper bounds and why.
* **"94% of the exchange is parlays", above a table with no parlay row** - the
  census was built from a parlay-free corpus, so the families table's
  denominator was the 615,825 non-parlay traded markets while the header claimed
  the composition of the exchange. Fixed: the census reads the whole crawl
  (7 files, 19,760,357 lines) and derives the split; the denominator is stated.
* **"figures are derived by a script... never typed into a page"** - the
  19.76M/18.48M split, the parlay sample, and the whole decay table were typed.
  Fixed for all of these. Still typed and NOT derived here: "+0.4c per fill",
  "68 live cycles", "$259 of underlying", the tennis path illustration.
* **The parlay sample did not reconcile** - 1,658 + 2,089 + 39 = 3,786, not
  4,000. Two causes: the file holds non-parlay rows, and "placeholder book"
  counted only the 0.001/1.00 case. Fixed: derived exhaustively, and note the
  "39 actionable" figure was itself counting stale books.
* **Commit "retry transient network faults in the crawl"** - only `429` was
  retried; a timeout or 5xx broke out on the first attempt. Fixed.

---

## C. Guards that existed but could not fire on the case they were written for

### C1. The rule-of-three floor divided by CONTRACTS, not clusters

The guard exists because clustering error produced t=24.6 on zero losses. It
then divided loss-count uncertainty by the contract count - the same
independence error the repo exists to avoid.

| shipped zero-loss cell | n | clusters | net | t shipped | t cluster-denominated |
|---|---|---|---|---|---|
| 10m tennis tail SELL | 865 | 278 | +1.91c | **11.0** | 3.5 |
| 5m tennis tail SELL | 808 | 256 | +1.72c | 9.2 | 2.9 |

The page's own warning said the floor "brought that to t=3.8". No shipped cell
was 3.8; the cluster-denominated version is 3.5. **The prose described the
correct fix while the code shipped the wrong denominator.**

**Fixed** in `cluster_stats.loss_count_floor`: loss EVENTS counted at cluster
level, exact Poisson small-count bounds rather than `sqrt(k)` (which claims
sd=1.00 at one observed event against the exact 1.91), and the empirical
cluster-level spread taken as a competing lower bound. Also: a loss moves P&L by
exactly $1 per contract, not by the entry price - the old version scaled by
`ask`, shaving another 15% off every fave-BUY error bar.

**This does not rescue the short-horizon tennis cells.** With 15 losses in 1,548
contracts the loss rate genuinely is pinned, and t stays high. The error bar was
never what killed those cells; the horizon confound in A2/A3 is.

### C2. Leave-one-day-out could not flip on a zero-loss cell

Every per-contract P&L in a zero-loss cell is positive, so every leave-one-out
mean is positive and the test printed "robust" exactly where the data is
weakest. It also clustered on day while the headline test clustered on
series-by-day, so a result driven by one series was invisible.

**Fixed.** Zero-loss cells report `n/a - no losses`; leave-one-out runs over both
clusters and series.

### C3. `parlay-EXCLUDE` was a family label, not an exclusion

`census()` had no filter on it. Verified: zero `KXMVE` records in the input, so
the guard had never once fired, and nothing said so. If a future crawl left
parlays in they would have been folded into `total_markets` and every `share`.

**Fixed.** Parlays are counted, then excluded, in the census itself.

### C4. Exhausting the retries was not counted as a failure

After six 429s the market was dropped, `failures` untouched, so the closing
"N fetched, M failures" under-reported every market the rate limit ate. 429s
cluster in time, so the thinning lands on stretches of the calendar.

**Fixed.** Counted, logged, and the dropped tickers are written to a
`.dropped` sidecar.

### C5. The capacity note told the reader to apply a correction the script threw away

"Scale per-day numbers up by the sampling fraction" - and `markets_per_day` was
populated and never read again.

**Fixed.** `--settled` supplies the population, the script does the scaling, and
without it the per-day column is omitted rather than printed uncorrected.

### C6. A missing candle file was skipped silently

...while `main()` printed the file count it was asked for. **Fixed:** raises.

### C7. No minimum cluster count and no small-sample correction

The only gate was `n < 60` contracts; `indices fave BUY @60m` shipped on 18
clusters, read against 1.96.

**Fixed.** `MIN_CLUSTERS = 20`, and `cluster_t_critical` returns the 95% value on
G-1 df.

### C8. The clustering unit missed the correlation it was chosen to capture

`KXBTCD` and `KXETHD` on one day were two clusters; so were `KXDJI` and `KXSPX`,
and two cities in one weather system.

**Fixed.** `SHARED_UNDERLYING` clusters crypto, indices, commodities and weather
family-by-day. Tennis and sports props stay series-by-day on purpose - separate
matches settle separately, which is the diversification argument itself.

### C9. The reported mean and the reported SE were different estimators

Callers aggregated to cluster means and passed those to `clustered()`, which put
each value in its own group and returned the SE of the EQUAL-weighted mean of
cluster means, while the point estimate was the size-weighted pooled mean.
Direction was not predictable - on 10m tennis fave BUY the coded SE was
conservative (0.48c against 0.25c).

**Fixed.** `clustered_pooled` takes cluster sums and counts and returns the
cluster-robust SE of the estimator actually reported.

---

## What did NOT change

The two pre-specified trades, the 5c/80c thresholds, the horizon set, and the
`MIN_BUCKET`/`n>=60` conventions are all as they were. No trade was redefined to
improve a number.
