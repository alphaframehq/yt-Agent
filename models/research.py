"""Research data models for TubePilot AI.

Defines the ResearchBundle and all supporting sub-models that capture
the structured output of the research agent: entity extraction, financial
data, competitor analysis, market sizing, source citations, and YouTube
content-gap insights.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "EntityInfo",
    "MarginData",
    "FinancialData",
    "CompetitorInfo",
    "MarketData",
    "SourceInfo",
    "YTInsights",
    "ResearchBundle",
]


class EntityInfo(BaseModel):
    """A named entity extracted during research."""

    name: str = Field(..., description="Canonical entity name.")
    type: str = Field(..., description="Entity type, e.g. 'company', 'person', 'product'.")
    facts: list[str] = Field(default_factory=list, description="Key facts about the entity.")


class MarginData(BaseModel):
    """Profit-margin breakdown for a company or segment."""

    gross: float = Field(..., description="Gross margin as a decimal (0-1 or percentage).")
    operating: float = Field(..., description="Operating margin.")
    net: float = Field(..., description="Net margin.")


class FinancialData(BaseModel):
    """Structured financial metrics collected during research."""

    revenue_by_year: dict[str, float] = Field(
        default_factory=dict,
        description="Mapping of year (e.g. '2024') to revenue in USD.",
    )
    segments: dict[str, float] = Field(
        default_factory=dict,
        description="Revenue or value by business segment.",
    )
    margins: MarginData = Field(..., description="Margin breakdown.")
    growth_rate: float = Field(..., description="Year-over-year growth rate.")
    unit_economics: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary unit-economics KPIs.",
    )


class CompetitorInfo(BaseModel):
    """A competitor data point used in comparative analysis."""

    name: str = Field(..., description="Competitor name.")
    metric: str = Field(..., description="Metric being compared, e.g. 'revenue'.")
    value: float = Field(..., description="Metric value.")


class MarketData(BaseModel):
    """High-level market-sizing data."""

    tam_usd: float = Field(..., description="Total Addressable Market in USD.")
    penetration_pct: float = Field(
        ...,
        description="Current market penetration as a percentage.",
    )


class SourceInfo(BaseModel):
    """A citation backing a specific claim in the research bundle."""

    claim_id: str = Field(..., description="Unique identifier for the claim this source backs.")
    claim: str = Field(..., description="Human-readable claim text.")
    source: str = Field(..., description="Source name or publisher.")
    url: str = Field(..., description="URL of the source.")
    snippet: str = Field(..., description="Relevant excerpt from the source.")
    confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="Confidence score for this citation (0-1).",
    )


class YTInsights(BaseModel):
    """YouTube landscape insights derived from competitive analysis."""

    top_titles: list[str] = Field(
        default_factory=list,
        description="High-performing video titles in this niche.",
    )
    common_tags: list[str] = Field(
        default_factory=list,
        description="Frequently used tags by top creators.",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Content gaps identified in the niche.",
    )


class ResearchBundle(BaseModel):
    """Complete research output consumed by the script-writing agent.

    Bundles entity data, financials, competitor benchmarks, market sizing,
    sourced citations, and YouTube content-gap analysis into a single
    validated payload.
    """

    topic: str = Field(..., description="Research topic / video subject.")
    pillar: Literal["finance_to_technical", "tech_to_financial"] = Field(
        ...,
        description="Content pillar direction.",
    )
    entities: list[EntityInfo] = Field(
        default_factory=list,
        description="Extracted entities.",
    )
    financials: FinancialData = Field(..., description="Collected financial data.")
    technical_context: str = Field(
        ...,
        description="Technical background narrative.",
    )
    competitors: list[CompetitorInfo] = Field(
        default_factory=list,
        description="Competitor benchmark data.",
    )
    market_data: MarketData = Field(..., description="Market sizing data.")
    sources: list[SourceInfo] = Field(
        default_factory=list,
        description="All cited sources.",
    )
    yt_insights: YTInsights = Field(
        default_factory=YTInsights,
        description="YouTube content-gap insights.",
    )
    research_confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="Overall confidence score for the research bundle (0-1).",
    )

    @model_validator(mode="after")
    def _every_financial_numeric_has_source(self) -> "ResearchBundle":
        """Validate that every numeric fact in financials has a matching source.

        Each revenue entry, segment value, margin, and growth rate must be
        backed by a ``SourceInfo`` whose ``claim_id`` matches a deterministic
        key derived from the financial field.

        Expected claim_id patterns:
        - ``revenue_<year>``        for each key in ``revenue_by_year``
        - ``segment_<segment>``     for each key in ``segments``
        - ``margin_gross``          for gross margin
        - ``margin_operating``      for operating margin
        - ``margin_net``            for net margin
        - ``growth_rate``           for the growth rate
        - ``ue_<key>``              for each numeric value in ``unit_economics``
        """
        source_ids: set[str] = {s.claim_id for s in self.sources}
        missing: list[str] = []

        # Revenue entries
        for year in self.financials.revenue_by_year:
            cid = f"revenue_{year}"
            if cid not in source_ids:
                missing.append(cid)

        # Segment entries
        for seg in self.financials.segments:
            cid = f"segment_{seg}"
            if cid not in source_ids:
                missing.append(cid)

        # Margins
        for margin_name in ("gross", "operating", "net"):
            cid = f"margin_{margin_name}"
            if cid not in source_ids:
                missing.append(cid)

        # Growth rate
        if "growth_rate" not in source_ids:
            missing.append("growth_rate")

        # Numeric unit economics
        for key, val in self.financials.unit_economics.items():
            if isinstance(val, (int, float)):
                cid = f"ue_{key}"
                if cid not in source_ids:
                    missing.append(cid)

        if missing:
            raise ValueError(
                f"Financial numeric facts missing source citations: {', '.join(missing)}"
            )

        return self
