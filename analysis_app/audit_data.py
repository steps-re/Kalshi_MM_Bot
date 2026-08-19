"""Loader for the corrected audit numbers.

Everything the audit pages render comes from `data/audit.json`, produced by
`scripts/export_audit_data.py` off the trigger cache. The first version of this
site typed its numbers into markdown, which is how it kept publishing +0.85c per
unit OBI and a t of -22.6 after both had been superseded. Nothing here is typed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import streamlit as st

DATA = Path(__file__).resolve().parent / "data" / "audit.json"


@lru_cache(maxsize=1)
def _read() -> dict:
    return json.loads(DATA.read_text())


def audit() -> dict:
    return _read()


def cents(value: float | None, places: int = 3) -> str:
    """Cents with an explicit sign. Escapes nothing - cents carry no dollar sign."""

    if value is None:
        return "-"

    return f"{value:+.{places}f}c"


def money(value: float | None, places: int = 2) -> str:
    r"""Dollars, with the sign escaped for Streamlit markdown.

    An unescaped `$` starts LaTeX in st.markdown and silently eats the rest of
    the line, so every dollar figure on this site goes through here.
    """

    if value is None:
        return "-"

    return f"\\${value:,.{places}f}"


def verdict_badge(verdict: str) -> str:
    return {
        "HELD": "HELD",
        "SMALLER": "SMALLER",
        "NO POWER": "NO POWER",
        "CONSISTENT": "CONSISTENT",
        "ABSENT": "ABSENT",
    }.get(verdict, verdict)


def stamp() -> None:
    data = _read()
    corpus = data["corpus"]
    st.caption(
        f"Generated {data['generated_at_utc']} from {corpus['recordings']} recordings, "
        f"{corpus['elapsed_hours']:.1f} hours of real elapsed book coverage, "
        f"{corpus['triggers']:,} triggers. Every number on this page is computed "
        f"from that cache, not typed."
    )
