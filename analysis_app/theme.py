"""Chart palette and shared figure helpers.

Colours come from a validated categorical palette (adjacent-pair CVD separation
and normal-vision floors checked, not eyeballed). Most charts here encode a
single magnitude, so they use one hue; the status colours are reserved for
viable/unviable and never double as a series.

The app commits to a light surface deliberately rather than trying to follow a
system theme, so the chart colours are chosen once for that surface.
"""

from __future__ import annotations

import plotly.graph_objects as go

SURFACE = "#ffffff"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"

SERIES_BLUE = "#2a78d6"
SERIES_ORANGE = "#eb6834"

STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def style(fig: go.Figure, *, height: int = 380, x_title: str = "", y_title: str = "") -> go.Figure:
    """Apply the shared chrome: recessive grid, muted axes, no legend box by default."""

    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT, size=13, color=INK_SECONDARY),
        hoverlabel=dict(font_family=FONT, font_size=13),
        showlegend=False,
        xaxis_title=x_title,
        yaxis_title=y_title,
    )
    axis = dict(
        gridcolor=GRIDLINE,
        linecolor=BASELINE,
        zerolinecolor=BASELINE,
        tickfont=dict(color=INK_MUTED),
        title_font=dict(color=INK_SECONDARY, size=13),
    )
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig


def annotate(fig: go.Figure, x, y, text: str, *, color: str = INK_SECONDARY, shift: int = -12):
    """Direct label. Preferred over a legend wherever there is room for it."""

    fig.add_annotation(
        x=x,
        y=y,
        text=text,
        showarrow=False,
        font=dict(family=FONT, size=12, color=color),
        yshift=shift,
        xanchor="left",
    )
    return fig
