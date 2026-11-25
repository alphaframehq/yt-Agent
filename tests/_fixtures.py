"""Shared test fixtures for the yt-Agent test suite.

These are EXPLICIT TEST DATA ONLY — clearly labelled, small, hand-written
values used to exercise deterministic logic (verification gates, chart
routing, uploader gates) without any live API. They are NOT production
mocks: no production code path reads this module, and no test here claims
a fixture value is a real-world fact. Production code must still raise
without real credentials; these fixtures only let the deterministic
checks run in CI, where no keys, Google Sheets, or YouTube access exist.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.research import (
    CompetitorInfo,
    EntityInfo,
    FinancialData,
    MarginData,
    MarketData,
    ResearchBundle,
    SourceInfo,
)
from models.script import ChapterInfo, SceneInfo, ScriptMeta, SEOData, VideoScript


def _source(claim_id, claim, snippet):
    return SourceInfo(
        claim_id=claim_id,
        claim=claim,
        source="Test Filing (fixture)",
        url="https://example.com/test-filing",
        snippet=snippet,
        confidence=0.95,
    )


def make_test_bundle():
    """A small, fully sourced ResearchBundle built from explicit test values."""
    sources = [
        _source("revenue_2022", "TestCo revenue 2022", "TestCo reported revenue of $800 million in 2022."),
        _source("revenue_2023", "TestCo revenue 2023", "TestCo reported revenue of $1.0 billion in 2023."),
        _source("revenue_2024", "TestCo revenue 2024", "TestCo reported revenue of $1.2 billion in 2024."),
        _source("segment_Payments", "TestCo Payments segment", "Payments is TestCo's largest segment."),
        _source("segment_Software", "TestCo Software segment", "Software is TestCo's second segment."),
        _source("margin_gross", "TestCo gross margin", "TestCo gross margin is 0.6."),
        _source("margin_operating", "TestCo operating margin", "TestCo operating margin is 0.2."),
        _source("margin_net", "TestCo net margin", "TestCo net margin is 0.15."),
        _source("growth_rate", "TestCo growth rate", "TestCo grew 0.2 year over year."),
        _source("ue_revenue", "TestCo unit revenue", "Unit revenue is 100.0."),
        _source("ue_cogs", "TestCo unit cogs", "Unit cogs is 60.0."),
        _source("ue_gross", "TestCo unit gross", "Unit gross is 40.0."),
        _source("ue_opex", "TestCo unit opex", "Unit opex is 25.0."),
        _source("ue_net", "TestCo unit net", "Unit net is 15.0."),
    ]
    return ResearchBundle(
        topic="How TestCo Makes Money",
        pillar="finance_to_technical",
        entities=[EntityInfo(name="TestCo", type="company", facts=["TestCo runs a payment network."])],
        financials=FinancialData(
            revenue_by_year={"2022": 800_000_000.0, "2023": 1_000_000_000.0, "2024": 1_200_000_000.0},
            segments={"Payments": 0.6, "Software": 0.4},
            margins=MarginData(gross=0.6, operating=0.2, net=0.15),
            growth_rate=0.2,
            unit_economics={"revenue": 100.0, "cogs": 60.0, "gross": 40.0, "opex": 25.0, "net": 15.0},
        ),
        technical_context="TestCo uses consensus and distributed systems architecture.",
        competitors=[CompetitorInfo(name="CompCo", metric="take_rate", value=0.019)],
        market_data=MarketData(tam_usd=2_500_000_000_000.0, penetration_pct=4.8),
        sources=sources,
        research_confidence=0.9,
    )


def make_test_script(bundle=None):
    """A valid VideoScript for the fixture bundle: 8 chart scenes, all
    required finance_to_technical sections, a spoken disclaimer, and
    citations to real (fixture) source IDs. Narrations contain only
    numbers that appear in the fixture bundle's verified text."""
    bundle = bundle or make_test_bundle()
    narrations = [
        ("hook", "chart", "revenue_trend",
         "TestCo reported revenue of $1.2 billion in 2024, up from $800 million in 2022."),
        ("the_problem", "chart", "segment_breakdown",
         "Payments is the largest segment of TestCo, and the business depends on that segment."),
        ("technical_solution", "chart", "margin_compare",
         "TestCo operates a payment network built on distributed systems architecture."),
        ("financial_benefit", "chart", "unit_economics_waterfall",
         "Unit revenue is 100.0 in the fixture model, with unit gross of 40.0."),
        ("scope_and_moat", "chart", "scope_tam",
         "The market is large, and TestCo has captured a small share of it so far."),
        ("risks", "chart", "revenue_trend",
         "Revenue grew from $800 million in 2022 to $1.2 billion in 2024, which carries execution risk."),
        ("disclaimer", "diagram", None,
         "This video is for educational purposes only. It explains how payment systems work."),
        ("cta", "chart", "segment_breakdown",
         "Subscribe for more analysis of payment networks and their engineering."),
        ("risks", "chart", "margin_compare",
         "Margins and growth vary by year, and past results do not predict future results."),
    ]
    scenes = []
    for i, (section, v_type, chart_type, narration) in enumerate(narrations, start=1):
        scenes.append(SceneInfo(
            id=i,
            section=section,
            narration=narration,
            visual_prompt=f"TestCo {section} diagram",
            visual_type=v_type,
            chart_type=chart_type,
            duration_sec=60,
            citations=["revenue_2024"],
        ))
    description = (
        "DISCLAIMER: For educational and engineering analysis only. "
        + " ".join(["This analysis explains how TestCo operates its payment network."] * 60)
    )
    seo = SEOData(
        title="How TestCo Makes Money",
        description=description,
        tags=[f"testco{i}" for i in range(16)],
        chapters=[ChapterInfo(time="00:00", title="Intro")],
    )
    word_count = sum(len(s.narration.split()) for s in scenes)
    return VideoScript(
        pillar="finance_to_technical",
        scenes=scenes,
        seo=seo,
        meta=ScriptMeta(word_count=word_count, estimated_duration_sec=sum(s.duration_sec for s in scenes)),
    )
