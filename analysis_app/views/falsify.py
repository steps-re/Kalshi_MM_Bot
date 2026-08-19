"""The attack surface, written down before anyone else finds it. For Nate."""

from __future__ import annotations

import streamlit as st

from audit_data import audit, cents, stamp

data = audit()
whole = data["scans"]["all"]
expiry = data["profit"]["near_expiry"]
corpus = data["corpus"]

st.title("Prove this wrong")
st.caption(
    "Mike's bet is that Nate finds a hole. Probably. Here is where the holes are, "
    "ranked by how much they would cost us, with the command to check each one."
)
stamp()

st.markdown(
    """
The last version of this site was confidently wrong in five specific ways, and
none of them were caught by re-reading it. They were caught by re-running it.
So the useful thing to publish is not the conclusion, it is the list of things
that would overturn the conclusion, and the data to try them on.
"""
)

st.header("Everything you need to reproduce it")
st.code(
    """# 9.8GB, 187 recordings, 8/16 to 8/19
gcloud storage rsync -r gs://steps-nate-backtest-data/recordings/ ./recs

# one pass, builds the trigger cache everything else reads
python scripts/taker_extract.py ./recs --out triggers.jsonl

# the published baseline
python scripts/taker_expectancy.py triggers.jsonl --period all

# the conditioned scan that changes the answer
python scripts/taker_expectancy.py triggers.jsonl --period all \\
    --fine-bands --phase 0 900

# freeze on the early days, judge on the later one
python scripts/taker_expectancy.py triggers.jsonl --period pre \\
    --fine-bands --phase 0 900 --freeze winners.json
python scripts/taker_expectancy.py triggers.jsonl --period in \\
    --fine-bands --phase 0 900 --replicate winners.json""",
    language="bash",
)

st.header("The attacks, strongest first")

st.subheader("1. The decay confound")
st.markdown(
    f"""
**The claim at risk:** the near-expiry cheap-tail edge
({cents(expiry['best'])} best slice, {expiry['bh_positive']} slices surviving
FDR).

**The attack:** in the last quarter hour, a deep out-of-the-money strike is
mostly going to expire worthless. Any rule that systematically ends up short the
expensive side earns that decay without forecasting anything. If the "edge" is
drift rather than prediction, it is real money but it is not a signal, and it
will reverse the moment the strike is on the other side of the underlying.

**Why we think it survives:** the placebo flips the traded direction for whole
markets at a time and re-scores. Decay is direction-symmetric under that flip, so
it should wash out, and the observed best still beats
{100 - expiry['placebo_beat_rate'] * 100:.0f}% of placebo draws.

**How to break it:** split the near-expiry slices by whether the strike finished
in or out of the money, and by buy versus sell. If the edge lives entirely in
one cell, it is decay. This is the first thing to run and it has not been run.
"""
)

st.subheader("2. One family, two halves, one of each result")
st.markdown(
    """
**The claim at risk:** that the surviving slice generalises.

**The attack:** the candidate that held its holdout and the candidate that was
flatly refuted are the BTC and ETH versions of the same strike-ladder idea, on
the same exchange, in the same week. That is not two independent confirmations,
it is one idea that worked once and failed once.

**How to break it:** find any third instrument family where the same conditioned
slice is testable. If it is absent or negative there, the surviving slice is
almost certainly the lucky half of a coin flip.
"""
)

st.subheader("3. The missing 15-minute recordings")
st.markdown(
    f"""
**The claim at risk:** everything the original chapter said about GOLD15M,
BTC15M, SOL15M, DOGE15M, WTI and XRP.

**The attack:** none of it can be checked. All {corpus['recordings']} archived
manifests hold strike ladders, MLB, NFL and two weather markets. Zero 15-minute
series. The scan that produced those numbers read a temporary directory on a VM
that has since been stopped.

**How to break it:** if anyone still has that directory, or can re-record the
15M family for a few days, the original claims become testable again. Until
then, treat every 15M number ever published by this project as unverified.
"""
)

st.subheader("4. Live is not replay")
st.markdown(
    """
**The claim at risk:** the entry price.

**The attack:** the scan assumes you take the displayed touch at the instant the
signal appears. Extreme imbalance means that touch is thin, and the thin side is
the first thing consumed. By the time an order arrives the price is worse, or
there is nothing there. This is the classic way a backtested taker edge
evaporates, and no amount of book data can rule it out.

**How to break it:** send real orders at minimum size and compare fills to the
displayed touch. This is the only test that settles it, and it is cheap.
"""
)

st.subheader("5. The exit still assumes a fill")
st.markdown(
    f"""
**The claim at risk:** the whole net column.

**The attack:** the corrected scan prices the exit at the touch, which assumes
your resting order actually trades. Whether it trades depends on queue position
and on why the counterparty is crossing, and neither is visible in book data.
This is the same thing that made the maker backtest untrustworthy, and a taker
strategy inherits it on the way out.

**What we do about it:** report the bracket rather than a point. Rested at the
touch is {cents(whole['exit_touch'])}, forced to cross is
{cents(whole['exit_cross'])}. If your assumption about fill probability moves the
answer between those two, the scan cannot decide the question and you should say
so instead of picking one.
"""
)

st.subheader("6. Multiple comparisons, still")
st.markdown(
    f"""
**The claim at risk:** the count of surviving slices.

**The attack:** splitting price into finer bands and adding a phase filter
created new slices to search. More slices, more chances. The placebo null and
the FDR correction are meant to price that in, but they are calibrated on this
corpus and this null, and a determined critic can argue the null is too
generous.

**How to break it:** re-run the placebo with the sign flipped at the level of
the whole *day* rather than the market, which is a stricter null. If the
surviving slices stop surviving, they were search artefacts.
"""
)

st.header("Where we are confident, and why")
st.markdown(
    f"""
Three results are robust to every attack above, because they do not depend on
any slice surviving:

- **Makers pay nothing and takers pay `0.07 x P(1-P)`.** Reproduced against every
  real charge on the account ledger. This is arithmetic, not inference.
- **Crossing to flatten is a loss.** Measured live, on the account, at
  t = -2.4. Removing it moved the account.
- **The exit convention was worth {cents(whole['mid_convention_cost'])} per
  trade.** This is a property of the book, not of a strategy, and it is why the
  original scan's negatives were as large as they were.

Everything else on this site is a measurement with a standard error, and the
standard errors are now clustered on the market rather than the trigger, which is
the correction that mattered most.
"""
)

st.info(
    "If you break one of these, the fix is a pull request against "
    "`scripts/taker_expectancy.py` and a re-run of `scripts/export_audit_data.py`. "
    "Every number on this site regenerates from that one command, so a correction "
    "propagates rather than needing to be hunted through the prose."
)
