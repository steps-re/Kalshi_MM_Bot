# CLAIMS MADE (Kalshi order-book imbalance study)

Corpus: 346 recordings across two GCS buckets, ~100h of recorded order books,
Aug 16-19 2026. Instruments: KXBTCD/KXETHD (hourly BTC/ETH strike ladders),
KX*15M (15-minute crypto/commodity windows), some sports.

Fee schedule (verified against account ledger): makers pay $0. Takers pay
0.07 * contracts * P * (1-P) dollars, P = price in dollars.

## C1. The original project conclusion was wrong in its reasoning
Original: "no slice clears costs; three independent periods, three disjoint
winner lists; therefore noise." We found its headline venue (KXGOLD15M,
"+1.39c/trade, t=2.0, 931 triggers") had data from exactly TWO 15-minute
windows. The t came from treating 931 ticks in 2 price paths as 931 independent
draws.

## C2. Exit convention error worth +0.671c/trade
The scan computed net = (mid at horizon) - entry - fee, while documenting that
the exit "rests as a maker". A rested exit fills at the TOUCH not the mid. On
books <=2c wide that is half a spread. Measured mean difference across all
slices: +0.671c/trade.

## C3. Clustered standard errors change every venue verdict
Clustering on market ticker (one 15-min market = one price path) raises median
SE 1.6-1.7x. KXETHD went -0.980c t=-21.7 to -0.231c t=-2.5.

## C4. Dose-response with a no-signal control (THE KEY CLAIM)
Added a control band |OBI| < 0.2 (no signal by construction). Pooled ALL price
bands and both directions, one number per market, 30s horizon:

  recovered corpus (642 markets): ctrl -0.905c, .2-.5 -0.725c, .5-.7 -0.513c,
                                  .7-.9 -0.335c, >.9 -0.014c   SE ~0.041
  original archive (441 markets): ctrl -0.724c, .2-.5 -0.555c, .5-.7 -0.418c,
                                  .7-.9 -0.314c, >.9 -0.142c   SE ~0.029

Both perfectly monotone. Claim: imbalance is worth +0.891c / +0.581c per trade
over a balanced book, and this rules out a decay/drift confound because drift
does not care how imbalanced the book is.

## C5. One cell clears costs and replicates
KXBTCD, entry price 2-5c long-equivalent, 30s horizon, last 15 min before close:
  archive:   ctrl +0.025c -> obi>.9 +0.927c (95 markets, clears zero)
  recovered: ctrl -0.184c -> obi>.9 +0.551c (24 markets, clears zero)
  z-difference between the two = 1.15 (not significantly different)
Does NOT generalise: same condition on 15M family lifts only ~0.2c, stays negative.

## C6. Live fills land on the displayed touch
5 real orders, 1 contract: sell96->96, buy4->4, sell95->95, buy4->4, sell97->97.
Zero slippage on all five. 2 of 5 filled as MAKER at zero fees. Taker fees
0.0034/0.0027/0.0021 dollars on 5c/4c/3c entries, matching 0.07*P*(1-P).

## C7. Economics
Median depth on the side we must cross: 74 contracts (heavy side 3817, 52x).
9.8 triggers/hr on the archive's KXBTCD subset. Estimated $19-55/day at 25
contracts. Account is $36.66, floor $25.

## METHOD NOTES
- "long-equivalent" price: a SELL of YES at 97c is treated as a BUY of NO at 3c,
  and banded at 3c. Fee is symmetric so cost is identical.
- net per trade = sign*(exit_touch - entry)/ticks_per_cent - taker_fee(entry),
  where for a buy exit_touch = ask at horizon, for a sell = bid at horizon.
- triggers are throttled by a 5s cooldown per (ticker, OBI band).
- an entry is only counted if the recording covers entry + 30s.
- placebo: flip the traded direction of WHOLE MARKETS at random, 400 draws,
  take the max slice mean as the null for the best observed slice.
