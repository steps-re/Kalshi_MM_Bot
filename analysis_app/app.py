"""Kalshi market-making: what we learned.

Streamlit entry point. Every number on these pages is computed from
`kalshi_mm_bot` at render time rather than typed in, so the write-up cannot
drift away from the code it describes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
for extra in (APP_DIR, APP_DIR.parent / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

st.set_page_config(
    page_title="Kalshi market making: what we learned",
    page_icon="📉",
    layout="centered",
    initial_sidebar_state="expanded",
)

pages = [
    st.Page("views/findings.py", title="What we found", icon="🎯", default=True),
    st.Page("views/opportunity.py", title="The opportunity", icon="📈"),
    st.Page("views/fee_wall.py", title="Why fees decide it", icon="🧱"),
    st.Page("views/exchange.py", title="What the exchange offers", icon="🔎"),
    st.Page("views/venues.py", title="Kalshi vs the universe", icon="🌐"),
    st.Page("views/backtest_lies.py", title="Why the backtest lied", icon="📉"),
    st.Page("views/adverse_selection.py", title="Adverse selection & expiry", icon="⏱"),
    st.Page("views/what_changed.py", title="What changed, what's next", icon="🛠"),
]

with st.sidebar:
    st.markdown("### Kalshi market making")
    st.caption("Market size, what is addressable, and what it could pay.")

st.navigation(pages).run()

with st.sidebar:
    st.divider()
    st.caption(
        "Code: [steps-re/Kalshi_MM_Bot]"
        "(https://github.com/steps-re/Kalshi_MM_Bot) · "
        "upstream [nathanonderko/Kalshi_MM_Bot]"
        "(https://github.com/nathanonderko/Kalshi_MM_Bot)"
    )
    st.caption("Long form: `LESSONS.md` in the repo.")
