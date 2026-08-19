# UPDATED CLAIMS (round 2). The previous round's attacks were tested; these are
# the claims that survived, plus the new ones. Attack THESE.

## N1. The signal is a genuine forecast, not book geometry
Round 1 argued the dose-response was mechanical: at |OBI|>0.9 the crossing side
is thin, so buy-at-ask / mark-exit-at-ask-30s-later profits when that thin level
is consumed. Proposed test: redo on pure mid-to-mid returns, predicted to
"flatten into noise". Result (one number per market, all price bands pooled,
nothing selected):

  band          TOUCH exit   MID-TO-MID   CROSS exit
  ctrl <0.2       -0.905c      +0.013c      -3.089c
  0.2-0.5         -0.725c      +0.179c      -2.893c
  0.5-0.7         -0.513c      +0.376c      -2.681c
  0.7-0.9         -0.335c      +0.546c      -2.506c
  >0.9            -0.014c      +0.861c      -2.193c
  (recovered corpus, 641-658 markets per band, SE ~0.039)
Archive corpus independently: ctrl -0.025c -> >0.9 +0.587c.

Supporting: gradient identical at 1c spread (-0.005 -> +1.067) and 2c spread
(-0.035 -> +0.887). Share of individual markets positive: 51%, 63%, 72%, 78%,
88% across bands; median tracks mean.

## N2. Break-even reduces to one number
For KXBTCD / entry 2-5c long-equivalent / 30s / last 15 min:
  exit rests and fills:  +0.694c (archive, 69 mkts)  +0.695c (recovered, 16 mkts)
  exit must cross out:   -0.509c                     -0.502c
  => break-even passive fill rate = 0.509/(0.694+0.509) = 42%, both corpora.

## N3. Live entry fills land on the displayed touch
13 real 1-contract orders so far: 7/8 measured fills exactly at the displayed
touch, 1 better (bought 1c against a 2c display), 0 worse. Taker fees match
0.07*P*(1-P) to the cent. 1 of 8 filled as maker at zero fee.

## N4. Live passive-exit fill rate so far: 4 of 7 (57%), above the 42% threshold.
Realised cash: -12c over 11 completed trades.

## N5. Economics if it holds
Median depth on the side we cross: 74 contracts (heavy side 3817). ~9.8
triggers/hr on the archive's KXBTCD subset. Account $36.33, floor $25.

## METHOD
- long-equivalent price: a SELL of YES at 97c is treated as a BUY of NO at 3c.
- TOUCH exit = sign*(same-side touch at t+30 - entry) - taker fee(entry)
- CROSS exit = sign*(opposite touch at t+30 - entry) - fee(entry) - fee(exit)
- MID-TO-MID = sign*(mid at t+30 - mid at t0), no fees, no spread
- clustered on market ticker; 5s cooldown per (ticker, OBI band)
- control band |OBI|<0.2 sampled the same way as every other band
