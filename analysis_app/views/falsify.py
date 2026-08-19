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
    """# BOTH buckets. The second one 403s on one account and not the other,
# which is how 159 recordings looked like data loss for a day.
gcloud storage rsync -r gs://steps-nate-backtest-data/recordings/ ./recs
gcloud storage rsync -r gs://steps-kalshi-book/recordings/ ./recs2

# one pass, builds the trigger cache everything else reads
python scripts/taker_extract.py ./recs  --out triggers.jsonl
python scripts/taker_extract.py ./recs2 --out t2.jsonl

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

st.subheader("1. The decay confound - ANSWERED")
st.markdown(
    """
**The attack:** in the last quarter hour a deep out-of-the-money strike mostly
expires worthless. Any rule that systematically ends up short the expensive side
earns that decay without forecasting anything. This was ranked first because it
was the most likely way the whole thing was nothing.

**What settles it:** a control band. The re-extraction added books with imbalance
under 0.2, which carry no signal by construction. Decay does not care how
imbalanced the book is, so if the edge were drift, the control would pay the
same as the extreme band. It does not. The response rises monotonically with
imbalance and the control sits at or below zero, on both corpora and on every
venue where the edge appears. See **Where the money might be**.

**What is still open:** the control answers the confound, not the size. On the
15-minute family the imbalance lift is only about 0.2c and never gets above
water, so the surviving cell is one instrument, not a strategy.
"""
)

st.subheader("2. One family, one instrument - CONFIRMED, and it stings")
st.markdown(
    """
**The attack:** that the surviving cell generalises.

**Verdict: it does not.** The cell replicates on KXBTCD across two independently
collected corpora, and the same cheap-entry condition on the 15-minute family
(GOLD, SOL, DOGE, WTI, XRP, BTC, ETH) lifts about 0.2c and stays negative
throughout. So there is a real, replicated, controlled effect on the BTC hourly
strike ladder, and no general Kalshi edge behind it.

Anyone sizing this off the whole exchange is sizing off one instrument.
"""
)

st.subheader("3. The missing 15-minute recordings - RECOVERED")
st.markdown(
    f"""
**The claim at risk:** everything the original chapter said about GOLD15M,
BTC15M, SOL15M, DOGE15M, WTI and XRP.

**The attack:** none of it can be checked. All {corpus['recordings']} archived
manifests hold strike ladders, MLB, NFL and two weather markets. Zero 15-minute
series. The scan that produced those numbers read a temporary directory on a VM
that has since been stopped.

**Resolved 2026-08-19.** The recordings were never lost. The collector had been
writing to a second bucket that returns 403 for one of the two accounts, and the
403 was read as absence. 159 recordings and 16.6GB were recovered, giving 53
GOLD15M markets where the published claim rested on **two**. The original
in-sample table is now testable, and it does not survive: see **The audit**.
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
