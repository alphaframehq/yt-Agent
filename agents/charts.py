# LOCAL PRODUCTION PATCH — 2026-11-25 (applied via PR #1, branch fix/production-mocks)..
# Fixes P0 #10 (audit list, 25 Nov 2026): every chart type plotted hardcoded
# data (5 of 8 ignored the `data` argument) and any chart error silently
# returned a content-free stub PNG as success. This patch makes `data` from
# the ResearchBundle mandatory for every chart type (ValueError if missing),
# removes all hardcoded waterfall/TAM/competitor/scatter/donut defaults, and
# re-raises the original error on chart failure instead of returning a stub.
# NOTE: callers that previously called generate_chart_by_type() without data
# (e.g. agents/visuals.py) must now pass bundle data, or the call will raise.
"""Institutional Chart Generation Module for TubePilot AI.

Generates 8 branded fintech financial charts matching official channel styling:
- Dark Mode Background: #0E1117
- Foreground Text: #E6EDF3
- Primary Neon Accent: #00D4AA (Cyan/Mint)
- Secondary Accent: #FF6B6B (Coral/Red)
- High resolution: 12x6 inches, 150 DPI
- Clean minimalist layout: Spines removed, alpha grid, labeled data points

All chart data MUST come from the ResearchBundle via `generate_chart_by_type`
(or the per-type functions directly). There are no hardcoded data defaults:
a missing value raises ValueError, and a rendering failure re-raises the
original exception — a chart is never silently replaced by a stub image.

Chart Types:
1. revenue_trend(years, values, company)
2. segment_breakdown(segments_dict, company)
3. margin_compare(companies_dict)
4. unit_economics_waterfall(revenue, cogs, gross, opex, net)
5. scope_tam(current, tam, penetration_pct)
6. competitor_matrix(companies, metrics)
7. growth_margin_scatter(companies)
8. market_share_donut(shares_dict)
9. ai_source_coverage(labels, counts, topic) — AI topics only
10. ai_source_share(labels, counts, topic) — AI topics only

AI TOPICS (pillar ``ai_everything``): financials and market_data in the
bundle are explicit 0.0 "not applicable" sentinels, so chart types 1-8
CANNOT render for AI topics — they raise ValueError on the missing keys
instead of plotting fabricated data. The only honest graphs for an AI
topic come from data the research actually retrieved:
- ``ai_source_coverage`` / ``ai_source_share``: counts of retrieved
  TinyFish articles per publisher (``bundle.sources[].source``), built
  by ``agents.visuals.ai_topic_chart_data()``.
- A trend/interest-over-time chart is NOT available: TinyFish articles
  carry ``published_at``, but ``agents/research.py`` ``_run_ai_topic``
  drops it when building ``SourceInfo``, and trend items
  (``tools/trends.py`` ``TrendItem``) carry no date either, so no dated
  series exists in the bundle. Do not fabricate dates. Until research
  carries ``published_at`` into the bundle, no dated chart may be plotted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

__all__ = [
    "revenue_trend",
    "segment_breakdown",
    "margin_compare",
    "unit_economics_waterfall",
    "scope_tam",
    "competitor_matrix",
    "growth_margin_scatter",
    "market_share_donut",
    "ai_source_coverage",
    "ai_source_share",
    "generate_chart_by_type",
    "chart_to_clip",
    "AI_CHART_TYPES",
    "FINANCE_CHART_TYPES",
]

# Chart types that need company financials / market data. They raise
# ValueError for ai_everything topics, whose bundle carries 0.0
# "not applicable" sentinels instead of financials — never plot fake data.
FINANCE_CHART_TYPES = (
    "revenue_trend",
    "segment_breakdown",
    "margin_compare",
    "unit_economics_waterfall",
    "scope_tam",
    "competitor_matrix",
    "growth_margin_scatter",
    "market_share_donut",
)

# Chart types that render from real retrieved-article data and are the
# only types available for ai_everything topics.
AI_CHART_TYPES = (
    "ai_source_coverage",
    "ai_source_share",
)

CHART_DIR = Path("cache/charts")
CHART_DIR.mkdir(parents=True, exist_ok=True)

# Brand Style Tokens
BG_COLOR = "#0E1117"
FG_COLOR = "#E6EDF3"
ACCENT_CYAN = "#00D4AA"
ACCENT_CORAL = "#FF6B6B"
GRID_COLOR = "#2A324B"


def _format_usd(value: float) -> str:
    """Compact USD label for chart titles (data-driven, never hardcoded)."""
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.0f}"


def _require(data: Dict[str, Any], key: str, chart_type: str) -> Any:
    """Fetch a required data key, or fail loudly — charts never invent data."""
    if key not in data or data[key] is None:
        raise ValueError(
            f"generate_chart_by_type('{chart_type}'): missing required data "
            f"key '{key}' — charts must be built from the ResearchBundle, "
            "no hardcoded defaults."
        )
    return data[key]


def _setup_figure():
    """Create styled matplotlib figure with brand styling."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)

    # Clean axes
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(FG_COLOR)
    ax.spines["bottom"].set_color(FG_COLOR)

    ax.tick_params(colors=FG_COLOR, labelsize=10)
    ax.grid(color=GRID_COLOR, linestyle="--", linewidth=0.5, alpha=0.3)
    return fig, ax


def _save_chart(fig, filename: str) -> str:
    """Save figure to high-res PNG."""
    import matplotlib.pyplot as plt

    dest_path = CHART_DIR / filename
    fig.tight_layout()
    fig.savefig(dest_path, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    logger.info(f"Generated chart saved to: {dest_path}")
    return str(dest_path.resolve())


def revenue_trend(years: List[str], values: List[float], company: str) -> str:
    """Chart 1: Multi-year annual revenue growth trajectory."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        # Scale values to billions if large
        scaled = [v / 1e9 if max(values) > 1e8 else v for v in values]
        unit = "($B)" if max(values) > 1e8 else "($M)"

        ax.plot(years, scaled, color=ACCENT_CYAN, marker="o", linewidth=3, markersize=8)
        ax.fill_between(years, scaled, color=ACCENT_CYAN, alpha=0.15)

        for x, y in zip(years, scaled):
            ax.annotate(
                f"${y:.1f}{unit[1:3]}",
                (x, y),
                textcoords="offset points",
                xytext=(0, 10),
                ha="center",
                color=FG_COLOR,
                fontweight="bold",
            )

        ax.set_title(f"{company.upper()} // ANNUAL REVENUE TRAJECTORY {unit}", color=FG_COLOR, fontsize=14, pad=15)
        return _save_chart(fig, f"rev_trend_{company.lower().replace(' ', '_')}.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate revenue_trend chart: {e}")
        raise


def segment_breakdown(segments_dict: Dict[str, float], company: str) -> str:
    """Chart 2: Business revenue segment breakdown."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        labels = list(segments_dict.keys())
        values = [v * 100 if max(segments_dict.values()) <= 1.0 else v for v in segments_dict.values()]

        colors = [ACCENT_CYAN, ACCENT_CORAL, "#7928CA", "#FF0080", "#0070F3"][: len(labels)]
        bars = ax.barh(labels, values, color=colors, height=0.55)

        for bar in bars:
            width = bar.get_width()
            ax.text(
                width + 1,
                bar.get_y() + bar.get_height() / 2,
                f"{width:.1f}%",
                va="center",
                color=FG_COLOR,
                fontweight="bold",
            )

        ax.set_title(f"{company.upper()} // REVENUE SEGMENT CONTRIBUTION (%)", color=FG_COLOR, fontsize=14, pad=15)
        return _save_chart(fig, f"segments_{company.lower().replace(' ', '_')}.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate segment_breakdown chart: {e}")
        raise


def margin_compare(companies_dict: Dict[str, Dict[str, float]]) -> str:
    """Chart 3: Gross, Operating, and Net margin benchmark."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np

        fig, ax = _setup_figure()
        companies = list(companies_dict.keys())
        x = np.arange(len(companies))
        width = 0.25

        # P0 #10: margins must come from the ResearchBundle — no 0.6/0.2/0.15 defaults.
        for c in companies:
            for key in ("gross", "operating", "net"):
                if key not in companies_dict[c]:
                    raise ValueError(
                        f"margin_compare: company '{c}' is missing required "
                        f"margin '{key}' from the ResearchBundle."
                    )

        gross = [companies_dict[c]["gross"] * 100 for c in companies]
        operating = [companies_dict[c]["operating"] * 100 for c in companies]
        net = [companies_dict[c]["net"] * 100 for c in companies]

        ax.bar(x - width, gross, width, label="Gross Margin", color=ACCENT_CYAN)
        ax.bar(x, operating, width, label="Operating Margin", color="#0070F3")
        ax.bar(x + width, net, width, label="Net Margin", color=ACCENT_CORAL)

        ax.set_xticks(x)
        ax.set_xticklabels(companies, color=FG_COLOR, fontsize=11)
        ax.legend(facecolor=BG_COLOR, edgecolor=GRID_COLOR, labelcolor=FG_COLOR)
        ax.set_title("MARGIN PROFILE COMPARISON (%)", color=FG_COLOR, fontsize=14, pad=15)

        return _save_chart(fig, "margin_compare.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate margin_compare chart: {e}")
        raise


def unit_economics_waterfall(revenue: float, cogs: float, gross: float, opex: float, net: float) -> str:
    """Chart 4: Waterfall transaction fee and cost distribution."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        steps = ["Gross Rev", "Interchange/COGS", "Gross Profit", "Cloud/OpEx", "Net Spread"]
        values = [revenue, -cogs, gross, -opex, net]
        colors = [ACCENT_CYAN, ACCENT_CORAL, ACCENT_CYAN, ACCENT_CORAL, "#7928CA"]

        ax.bar(steps, values, color=colors, width=0.55)
        for i, v in enumerate(values):
            ax.text(i, v / 2, f"{v:+.2f}%", ha="center", va="center", color=FG_COLOR, fontweight="bold")

        ax.axhline(0, color=FG_COLOR, linewidth=0.8)
        ax.set_title("TRANSACTION UNIT ECONOMICS WATERFALL (% OF GMV)", color=FG_COLOR, fontsize=14, pad=15)
        return _save_chart(fig, "unit_economics_waterfall.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate unit_economics_waterfall chart: {e}")
        raise


def scope_tam(current: float, tam: float, penetration_pct: float) -> str:
    """Chart 5: Total Addressable Market vs Captured Volume."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        labels = ["Captured Volume", "Remaining TAM Runway"]
        sizes = [penetration_pct, 100 - penetration_pct]
        colors = [ACCENT_CYAN, GRID_COLOR]

        wedges, texts, autotexts = ax.pie(
            sizes,
            labels=labels,
            autopct="%1.1f%%",
            startangle=140,
            colors=colors,
            wedgeprops={"edgecolor": BG_COLOR, "linewidth": 2},
        )
        for t in texts:
            t.set_color(FG_COLOR)
        for at in autotexts:
            at.set_color(BG_COLOR)
            at.set_fontweight("bold")

        ax.set_title(f"MANDATORY SCOPE: TAM ({_format_usd(tam)}) VS CURRENT CAPTURE ({penetration_pct}%)", color=FG_COLOR, fontsize=14)
        return _save_chart(fig, "scope_tam.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate scope_tam chart: {e}")
        raise


def competitor_matrix(companies: List[str], metrics: List[float]) -> str:
    """Chart 6: Competitor comparative take rate matrix."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        colors = [ACCENT_CYAN if i == 0 else ACCENT_CORAL for i in range(len(companies))]
        scaled = [m * 100 if max(metrics) <= 1.0 else m for m in metrics]

        bars = ax.bar(companies, scaled, color=colors, width=0.5)
        for b in bars:
            h = b.get_height()
            ax.text(b.get_x() + b.get_width() / 2, h + 0.05, f"{h:.2f}%", ha="center", color=FG_COLOR, fontweight="bold")

        ax.set_title("COMPETITIVE TAKE RATE BENCHMARK (%)", color=FG_COLOR, fontsize=14, pad=15)
        return _save_chart(fig, "competitor_matrix.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate competitor_matrix chart: {e}")
        raise


def growth_margin_scatter(companies: List[Dict[str, Any]]) -> str:
    """Chart 7: Growth rate vs Operating margin frontier."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        for comp in companies:
            # P0 #10: growth/margin/name must come from the ResearchBundle —
            # no 0.25/0.20/"Fintech" defaults.
            for key in ("growth", "margin", "name"):
                if key not in comp:
                    raise ValueError(
                        f"growth_margin_scatter: every company needs '{key}' "
                        "from the ResearchBundle."
                    )
            x = comp["growth"] * 100
            y = comp["margin"] * 100
            name = comp["name"]

            ax.scatter(x, y, color=ACCENT_CYAN, s=180, edgecolors=FG_COLOR, linewidth=1.5)
            ax.annotate(name, (x + 0.8, y + 0.5), color=FG_COLOR, fontweight="bold")

        ax.set_xlabel("YoY Revenue Growth Rate (%)", color=FG_COLOR)
        ax.set_ylabel("Operating Margin (%)", color=FG_COLOR)
        ax.set_title("EFFICIENCY FRONTIER: GROWTH VS OPERATING MARGIN", color=FG_COLOR, fontsize=14, pad=15)
        return _save_chart(fig, "growth_margin_scatter.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate growth_margin_scatter chart: {e}")
        raise


def market_share_donut(shares_dict: Dict[str, float]) -> str:
    """Chart 8: Market share distribution donut."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = _setup_figure()
        labels = list(shares_dict.keys())
        sizes = list(shares_dict.values())
        colors = [ACCENT_CYAN, ACCENT_CORAL, "#7928CA", "#0070F3", "#FF0080"][: len(labels)]

        wedges, texts, autotexts = ax.pie(
            sizes,
            labels=labels,
            autopct="%1.0f%%",
            startangle=90,
            colors=colors,
            pctdistance=0.75,
            wedgeprops={"width": 0.45, "edgecolor": BG_COLOR, "linewidth": 2},
        )
        for t in texts:
            t.set_color(FG_COLOR)
        for at in autotexts:
            at.set_color(FG_COLOR)
            at.set_fontweight("bold")

        ax.set_title("GLOBAL MARKET SHARE DISTRIBUTION", color=FG_COLOR, fontsize=14, pad=15)
        return _save_chart(fig, "market_share_donut.png")
    except Exception as e:
        # P0 #10: never return a content-free stub PNG as success.
        logger.error(f"Failed to generate market_share_donut chart: {e}")
        raise


def ai_source_coverage(labels: List[str], counts: List[int], topic: str) -> str:
    """Chart 9 (AI topics only): retrieved-article coverage per publisher.

    ``labels``/``counts`` are real counts of retrieved TinyFish articles
    per publisher, built by ``agents.visuals.ai_topic_chart_data()`` —
    never fabricated. A publisher with zero retrieved articles simply
    does not appear; counts are never invented to fill the chart.
    """
    try:
        import matplotlib.pyplot as plt

        if not labels or not counts or len(labels) != len(counts):
            raise ValueError(
                "ai_source_coverage: 'labels' and 'counts' must be "
                "non-empty lists of the same length."
            )
        if any(c < 0 for c in counts):
            raise ValueError("ai_source_coverage: counts must be >= 0.")

        fig, ax = _setup_figure()
        colors = [ACCENT_CYAN, ACCENT_CORAL, "#7928CA", "#FF0080", "#0070F3"]
        bar_colors = [colors[i % len(colors)] for i in range(len(labels))]
        bars = ax.barh(labels, counts, color=bar_colors, height=0.55)

        for bar in bars:
            width = bar.get_width()
            ax.text(
                width + 0.1,
                bar.get_y() + bar.get_height() / 2,
                f"{int(width)}",
                va="center",
                color=FG_COLOR,
                fontweight="bold",
            )

        ax.set_xlim(0, max(counts) * 1.2 + 0.5)
        ax.set_xlabel("Retrieved articles", color=FG_COLOR)
        ax.set_title(
            f"{topic.upper()[:60]} // SOURCE COVERAGE (RETRIEVED ARTICLES)",
            color=FG_COLOR,
            fontsize=14,
            pad=15,
        )
        slug = "".join(c if c.isalnum() else "_" for c in topic.lower())[:30].strip("_")
        return _save_chart(fig, f"ai_source_coverage_{slug or 'topic'}.png")
    except Exception as e:
        logger.error(f"Failed to generate ai_source_coverage chart: {e}")
        raise


def ai_source_share(labels: List[str], counts: List[int], topic: str) -> str:
    """Chart 10 (AI topics only): share of retrieved articles per publisher.

    Same real counts as :func:`ai_source_coverage`, rendered as a donut
    of each publisher's share of all retrieved articles for the topic.
    """
    try:
        import matplotlib.pyplot as plt

        if not labels or not counts or len(labels) != len(counts):
            raise ValueError(
                "ai_source_share: 'labels' and 'counts' must be "
                "non-empty lists of the same length."
            )
        if any(c < 0 for c in counts) or sum(counts) <= 0:
            raise ValueError("ai_source_share: counts must be >= 0 with a positive total.")

        fig, ax = _setup_figure()
        colors = [ACCENT_CYAN, ACCENT_CORAL, "#7928CA", "#0070F3", "#FF0080"]
        pie_colors = [colors[i % len(colors)] for i in range(len(labels))]

        wedges, texts, autotexts = ax.pie(
            counts,
            labels=labels,
            autopct="%1.0f%%",
            startangle=90,
            colors=pie_colors,
            pctdistance=0.75,
            wedgeprops={"width": 0.45, "edgecolor": BG_COLOR, "linewidth": 2},
        )
        for t in texts:
            t.set_color(FG_COLOR)
        for at in autotexts:
            at.set_color(FG_COLOR)
            at.set_fontweight("bold")

        ax.set_title(
            f"{topic.upper()[:60]} // SHARE OF RETRIEVED ARTICLES BY SOURCE",
            color=FG_COLOR,
            fontsize=14,
            pad=15,
        )
        slug = "".join(c if c.isalnum() else "_" for c in topic.lower())[:30].strip("_")
        return _save_chart(fig, f"ai_source_share_{slug or 'topic'}.png")
    except Exception as e:
        logger.error(f"Failed to generate ai_source_share chart: {e}")
        raise


def generate_chart_by_type(chart_type: str, data: Optional[Dict[str, Any]] = None) -> str:
    """Dynamic router generating any of the 8 chart types from bundle data.

    P0 #10: `data` is mandatory and must carry every value the chart plots.
    Any missing key raises ValueError — no hardcoded defaults are used.

    AI topics (pillar ``ai_everything``): only ``ai_source_coverage``
    and ``ai_source_share`` are available — they plot real retrieved
    article counts. The 8 finance types raise ValueError for AI data
    (their required keys are absent) instead of plotting fabricated
    figures; a dated trend/interest chart is unavailable until research
    carries ``published_at`` into the bundle (see module docstring).
    """
    if data is None:
        raise ValueError(
            f"generate_chart_by_type('{chart_type}'): 'data' is required — "
            "charts must be built from the ResearchBundle, no hardcoded defaults."
        )

    if chart_type == "ai_source_coverage":
        labels = _require(data, "source_labels", chart_type)
        counts = _require(data, "source_counts", chart_type)
        topic = data.get("topic", "AI topic")
        return ai_source_coverage(labels, counts, topic)

    elif chart_type == "ai_source_share":
        labels = _require(data, "source_labels", chart_type)
        counts = _require(data, "source_counts", chart_type)
        topic = data.get("topic", "AI topic")
        return ai_source_share(labels, counts, topic)

    if chart_type == "revenue_trend":
        company = _require(data, "company", chart_type)
        years = _require(data, "years", chart_type)
        values = _require(data, "values", chart_type)
        return revenue_trend(years, values, company)

    elif chart_type == "segment_breakdown":
        company = _require(data, "company", chart_type)
        segments = _require(data, "segments", chart_type)
        return segment_breakdown(segments, company)

    elif chart_type == "margin_compare":
        comps = _require(data, "companies", chart_type)
        return margin_compare(comps)

    elif chart_type == "unit_economics_waterfall":
        return unit_economics_waterfall(
            _require(data, "revenue", chart_type),
            _require(data, "cogs", chart_type),
            _require(data, "gross", chart_type),
            _require(data, "opex", chart_type),
            _require(data, "net", chart_type),
        )

    elif chart_type == "scope_tam":
        return scope_tam(
            _require(data, "current", chart_type),
            _require(data, "tam", chart_type),
            _require(data, "penetration_pct", chart_type),
        )

    elif chart_type == "competitor_matrix":
        comps = _require(data, "companies", chart_type)
        metrics = _require(data, "metrics", chart_type)
        return competitor_matrix(comps, metrics)

    elif chart_type == "growth_margin_scatter":
        comps = _require(data, "companies", chart_type)
        return growth_margin_scatter(comps)

    elif chart_type == "market_share_donut":
        shares = _require(data, "shares", chart_type)
        return market_share_donut(shares)

    else:
        raise ValueError(
            f"generate_chart_by_type: unknown chart type '{chart_type}'. "
            "Valid types: revenue_trend, segment_breakdown, margin_compare, "
            "unit_economics_waterfall, scope_tam, competitor_matrix, "
            "growth_margin_scatter, market_share_donut, ai_source_coverage, "
            "ai_source_share. Finance types require financials and are "
            "unavailable for ai_everything topics; AI types require "
            "'source_labels'/'source_counts' from real retrieved articles."
        )


def chart_to_clip(png_path: str, duration: float) -> Any:
    """Convert a chart PNG into an animated video clip.

    Applies the same motion treatment as the assembler: a Ken Burns
    zoom/pan (so the chart never sits static) plus a 0.8s fade-in.
    If MoviePy or the motion helper is unavailable, falls back to a
    plain still clip — and if even that fails, returns the PNG path so
    the caller can decide; it never claims motion it did not apply.
    """
    try:
        from moviepy.editor import ImageClip

        try:
            from agents.assemble import (
                CHART_FADEIN_SEC,
                _ken_burns_clip,
            )

            clip = _ken_burns_clip(png_path, duration)
            return clip.fadein(CHART_FADEIN_SEC)
        except Exception as e:
            logger.warning(
                f"Ken Burns unavailable for chart clip {png_path} ({e}); "
                "using a plain still clip with fade-in."
            )
            clip = ImageClip(png_path).set_duration(duration)
            return clip.fadein(0.8)
    except Exception as e:
        logger.warning(f"MoviePy ImageClip not available or failed: {e}")
        return png_path
