# Pre-registration: where in the bracket does the live exit land?

Written 2026-08-20, before any orders are sent. Committed before the run so the
decision rule cannot move after the numbers arrive.

This project has already been burned twice by deciding what counted as a result
after seeing it: the taker scan crowned a different winner in each of three
periods, and the gate study picked its threshold criterion once 90 turned out
not to win on the criterion it had printed. This file exists so that cannot
happen a third time.

## The question

`exit_fill_study.py`, corrected, prices the KXBTCD 2-5c near-expiry round trip
on 598 triggers across 84 markets, and returns a **bracket** rather than an
answer, because book snapshots cannot see trades:

    assumption      realised per trade (hold 30s, rest 45s)
    optimistic      +0.738c +/-0.194     every way the level could have cleared
    conservative    -0.237c +/-0.196     level emptied AND traded through
    never fills     -0.102c +/-0.297

Across the whole grid the bracket runs about **-0.4c to +1.1c**. Nothing in
recorded book data can narrow it. Live orders can, and that is the only reason
to send any.

**Primary endpoint:** mean realised cents per completed round trip, where a
round trip is a 1-contract taker entry at the displayed touch, a hold, a resting
exit at the touch, and a cross of whatever has not filled by the rest deadline.
This is the same quantity the offline study estimates, so it lands somewhere in
the bracket or it refutes the instrument.

## Power, computed before the run and not encouraging

From **79 untraded shadow signals** collected 2026-08-19 (`taker_shadow.jsonl`),
scored at a 30s horizon:

    touch exit   n=79   mean +0.400c   sd 3.232c
    cross exit   n=79   mean -0.895c   sd 3.081c

Per-trade standard deviation is **3.23c**. The clustered offline standard errors
of ~0.2c come from 598 triggers, and they do not mean a handful of live trades
will be precise. At sd 3.23c:

    to place the mean within +/-0.50c (95%)            ~160 trades
    to place the mean within +/-0.25c (95%)            ~642 trades
    to detect a 0.75c difference between two arms      ~291 trades PER ARM
    to detect a 0.50c difference between two arms      ~655 trades PER ARM

**Target: 160 completed round trips.** That places the mean within about
+/-0.5c, which is enough to say which half of the bracket we are in and nothing
finer. Maximum loss is the entry price, 2-5c, so 160 trades risk at most about
**$8** against a $36 account. The floor stops the run at $25 regardless.

## How long that takes, measured rather than assumed

The 8/19 shadow collection logged **83 signals in 0.89 hours, about 93 an
hour**, while it was watching. `claims2.md` N5 put it at "~9.8 triggers/hr on
the archive's KXBTCD subset", which is an order of magnitude lower - that figure
is one recorded subset, not the live ladder.

Signals are not completed round trips. A traded run carries a 60s per-ticker
cooldown, a 15-60s hold and a 20-90s rest, and the ladder is only inside the
trading window for roughly twelve minutes an hour. Taking those together, 160
completed trips is a **single evening of running**, not a multi-day campaign,
and even the +/-0.25c target is within reach in a way the old rate estimate
would have said it was not.

That matters for the decision rule below: the "undecided" branch is not a dead
end here. It means run it again tonight.

## The decision rule, fixed now

Let `m` be the mean realised cents over completed round trips and `ci` its 95%
interval.

| outcome | reading | action |
|---|---|---|
| `ci` excludes 0 and `m > 0` | upper half of the bracket. The resting exit fills often enough that the cell pays. | Escalate to 10 contracts and re-run this test. Size, not sign, becomes the open question. |
| `ci` excludes 0 and `m < 0` | conservative end. The round trip does not pay at 1 contract, and it cannot pay better at size. | Cell is dead. Stop. |
| `ci` contains 0 | undecided at this sample size | Report it as undecided. Do **not** read the sign of `m`. |

The third row is the likely one and saying so now is the point. A mean of
+0.3c on 160 trades has an interval of roughly +/-0.5c and settles nothing;
reporting it as "positive" would be the same error as the "three periods, no
survivor" chapter.

## Secondary hypothesis, pre-registered as UNDERPOWERED

The corrected archive run shows bid-heavy triggers realising about 2.5x
ask-heavy (+1.122c +/-0.411 against +0.439c +/-0.138 at hold 30s / rest 45s),
with the never-fills column separating cleanly (+0.473c against -0.550c). That
is what the audit's one-sidedness finding predicts.

**It does not replicate on the recovered corpus** (+0.502c +/-0.628 bid-heavy
against +0.464c +/-0.406 ask-heavy, on 8 and 11 markets). And detecting a 0.68c
split live needs roughly **291 trades per arm**, or 582 total, which this run
will not reach.

So it is registered now, in advance, as a hypothesis this run **cannot test**.
The split will be reported with its own power figure attached. A null result on
it is not evidence of absence and must not be written up as one. It also must
not be used to restrict the strategy to bid-heavy before it has been tested,
which would bake an untested belief into the instrument that is supposed to
test it.

## Stopping rules and limits

* 1 contract. Not configurable.
* `$25` hard balance floor, checked against the exchange before every entry.
* `--execute` required to send anything.
* 60s cooldown per ticker.
* Run halts on 20s of feed silence while subscribed.
* Hold and rest are randomised per trade from `(15, 30, 60)` and `(20, 45, 90)`
  seconds, independently, so the run measures the fill curve rather than one
  point on it. These arms are for curve-fitting, not for picking a winner: with
  9 cells and 160 trades there are ~18 trades per cell and no cell comparison
  will be significant. Do not crown one.

## What would invalidate the run rather than answer it

* **Mean entry slippage above 0.05c.** Every offline number assumes the order
  gets the displayed touch. If it does not, the bracket itself is wrong and this
  test measures nothing. `taker_live_report.py` prints this first for that
  reason.
* **Fewer than 30 completed round trips.** Report the count and stop; do not
  report a mean.
* **Any exit that neither rests nor crosses** (stranded inventory). The offline
  study has no term for that, so it would put the live and offline numbers on
  different scales.

## What this run does NOT decide

Size. Median depth on the crossing side is 74 contracts and every trade here is
1. A clean result at 1 contract says the price is real; it says nothing about
what 10 or 25 does to it. That is the next test, not this one.
