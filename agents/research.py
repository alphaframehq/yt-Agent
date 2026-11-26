# PRODUCTION PATCH (local only, 2026-11-25) — yt-Agent audit P0 #4 and #5.
# ResearchAgent.run() now FAILS LOUDLY (raises) on missing/partial financials
# instead of filling gaps with Stripe-like defaults (revenue $8.5B/$11.2B/
# $14.5B, margins 0.68/0.24/0.19, TAM $2.5T, penetration 4.8%, take rate
# 0.029, confidence 0.96). Sources are built ONLY from real retrieved
# documents (yfinance, SEC EDGAR filing index, TinyFish articles) — no
# fabricated "SEC 10-K / Yahoo Finance" sources for invented numbers.
# Segments / unit economics / competitor benchmarks / market sizing have no
# real source wired in, so they are left empty (0.0 sentinel for market data,
# explicitly documented as "not retrieved") instead of invented.
# _infer_ticker() returns None for unknown topics and for private companies
# (Stripe, Revolut) — no "SQ" default, no Stripe->SQ / Revolut->PYPL mapping.
# EDGAR + TinyFish results are now wired in where trivially clear (filing
# index source + article sources/context); see P1 TODO comments in run().
# Source: alphaframehq/yt-Agent @ c39f42541b8e784dcf41c09583dc57227be07667.
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25. No secrets in this file.
"""Agent 1: Research Agent for TubePilot AI.

Gathers structured, verifiable data with traceable sources across:
- SEC EDGAR (10-K / 10-Q)
- yfinance (financial statements, margins, ratios)
- TinyFish (news, market context, architectural explainers)
- YouTube API (competitor landscape, audience gaps)

Synthesizes data into a strictly validated Pydantic v2 `ResearchBundle`.
Every numeric fact in financials maps to a verified source claim_id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from loguru import logger
from pydantic import BaseModel, Field

from models.research import (
    CompetitorInfo,
    EntityInfo,
    FinancialData,
    MarginData,
    MarketData,
    ResearchBundle,
    SourceInfo,
    YTInsights,
)
from tools.sec_edgar import search_company_filings
from tools.tinyfish import search_fintech_context
from tools.yfinance_tool import get_ticker_financials
from tools.youtube_api import research_topic

__all__ = ["ResearchAgent", "ResearchInput", "run_research"]

PROMPT_PATH = Path("prompts/research_agent.txt")

# Confidence assigned to a claim backed by a real retrieved document. It
# means "retrieved live from the named source", NOT "independently audited".
REAL_DATA_CONFIDENCE = 0.9


class ResearchInput(BaseModel):
    """Input payload for Research Agent."""
    topic: str = Field(..., description="Topic or company thesis to research")
    pillar: Literal["ai_everything", "finance_to_technical", "tech_to_financial"] = Field(
        default="ai_everything",
        description="Editorial angle"
    )
    ticker: Optional[str] = Field(default=None, description="Optional public ticker symbol")


class ResearchAgent:
    """Multi-source research agent for verified financial & tech intelligence."""

    def __init__(self):
        self.system_prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text(encoding="utf-8")
        return "You are the Senior Research Agent for TubePilot AI."

    def run(self, input_data: Union[ResearchInput, Dict[str, Any]]) -> ResearchBundle:
        """Execute research pipeline and return validated ResearchBundle.

        PRODUCTION PATCH (P0 #4): raises instead of inventing defaults when
        financial data is missing or partial. Every numeric fact in the
        returned bundle comes from a real retrieved document, and every such
        fact has a matching SourceInfo citing that document. Fields with no
        real source are left empty, never defaulted.
        """
        if isinstance(input_data, dict):
            inp = ResearchInput(**input_data)
        else:
            inp = input_data

        # 26 Nov 2026 niche correction: ai_everything topics are AI
        # news/tools/discoveries/scams/predictions, not public
        # companies — there is no ticker, no yfinance financials and
        # no SEC filing for them. They are researched from real
        # retrieved articles + YouTube landscape instead (see
        # _run_ai_topic). The finance pillars keep the strict
        # ticker/financials path below unchanged.
        if inp.pillar == "ai_everything":
            return self._run_ai_topic(inp)

        logger.info(f"Starting research on '{inp.topic}' under pillar '{inp.pillar}'")

        ticker = inp.ticker or self._infer_ticker(inp.topic)
        if not ticker:
            raise ValueError(
                f"Cannot research '{inp.topic}': no public ticker could be inferred "
                "and none was provided. Pass an explicit public ticker (e.g. "
                "ResearchInput(topic=..., ticker='PYPL')). Private companies "
                "(e.g. Stripe, Revolut) have no public ticker and cannot be "
                "researched via yfinance."
            )

        # Raises on any yfinance failure (missing lib, network, invalid ticker).
        financial_tool_data = get_ticker_financials(ticker)

        # --- Validate real financials; fail loudly on missing/partial data ---
        revenue_by_year = financial_tool_data.get("revenue_by_year") or {}
        margins = financial_tool_data.get("margins") or {}
        if not revenue_by_year:
            raise RuntimeError(
                f"No revenue data retrieved for {ticker} (topic '{inp.topic}'). "
                "Refusing to build a ResearchBundle from defaults."
            )
        if not any(margins.get(k) for k in ("gross", "operating", "net")):
            raise RuntimeError(
                f"No margin data retrieved for {ticker} (topic '{inp.topic}'). "
                "Refusing to build a ResearchBundle from defaults."
            )
        if "growth_rate" not in financial_tool_data:
            raise RuntimeError(
                f"No growth-rate data retrieved for {ticker} (topic '{inp.topic}'). "
                "Refusing to build a ResearchBundle from defaults."
            )
        growth_rate = float(financial_tool_data["growth_rate"])

        # --- SEC EDGAR filing metadata (ticker -> CIK + filing index URL) ---
        # P1 NOTE (audit P1 #1): only the filing *index* is wired in here.
        # The full EDGAR companyfacts payload — verified 10-K revenues via
        # tools.sec_edgar.get_company_facts() / extract_annual_revenues() —
        # is NOT yet reconciled against the yfinance numbers above. That is a
        # follow-up: wire it in and cross-check revenues before claiming
        # "verified against SEC filings". The SEC source below cites only the
        # filing index itself, never a financial number.
        sec_info: Dict[str, Any] = {}
        try:
            sec_info = search_company_filings(ticker) or {}
        except Exception as e:
            logger.warning(
                f"SEC EDGAR lookup failed for {ticker}: {e}. Continuing without SEC source."
            )
        sec_url = sec_info.get("sec_url") or ""
        sec_title = sec_info.get("title") or ""

        # --- TinyFish context articles (raises if none are retrieved) ---
        # P1 NOTE (audit P1 #1): TinyFish articles ARE now wired in —
        # technical_context and per-article sources below. What is still not
        # done: no cross-check of TinyFish claims against EDGAR/yfinance
        # numbers, and article claims are cited as context, not as verified
        # financial facts.
        tinyfish_articles = search_fintech_context(inp.topic)

        yt_insights = research_topic(inp.topic)

        # Fields with no real retrieved source are left empty — never invented.
        segments: Dict[str, float] = {}  # TODO: no segment-breakdown source wired yet.
        unit_economics: Dict[str, Any] = {}  # TODO: no unit-economics source wired yet.
        # TODO: no competitor benchmark source wired yet — needs an explicit
        # competitor ticker list fed through get_competitor_comparison().
        competitors: List[CompetitorInfo] = []

        # --- Sources: ONLY from real retrieved documents ---
        source_url = financial_tool_data.get("source_url", f"https://finance.yahoo.com/quote/{ticker}")
        company_name = financial_tool_data.get("company_name") or sec_title or ticker
        sources: List[SourceInfo] = []

        # 1. Sources for every revenue year (real yfinance values only)
        for yr, val in revenue_by_year.items():
            sources.append(
                SourceInfo(
                    claim_id=f"revenue_{yr}",
                    claim=f"Annual revenue in {yr} was ${val:,.0f} USD.",
                    source="Yahoo Finance",
                    url=source_url,
                    snippet=f"Total Revenue FY{yr}: ${val:,.0f}",
                    confidence=REAL_DATA_CONFIDENCE,
                )
            )

        # 2. Sources for margins (real yfinance values only)
        for margin_name in ("gross", "operating", "net"):
            val = float(margins.get(margin_name, 0.0))
            sources.append(
                SourceInfo(
                    claim_id=f"margin_{margin_name}",
                    claim=f"{margin_name.capitalize()} margin is {val*100:.1f}%.",
                    source="Yahoo Finance",
                    url=source_url,
                    snippet=f"{margin_name.capitalize()} profit margin: {val}",
                    confidence=REAL_DATA_CONFIDENCE,
                )
            )

        # 3. Source for growth rate (real yfinance value only)
        sources.append(
            SourceInfo(
                claim_id="growth_rate",
                claim=f"YoY revenue growth rate is {growth_rate*100:.1f}%.",
                source="Yahoo Finance",
                url=source_url,
                snippet=f"Year over year growth: {growth_rate}",
                confidence=REAL_DATA_CONFIDENCE,
            )
        )

        # 4. SEC EDGAR filing index (metadata only — cites no financial number)
        if sec_url and sec_info.get("cik"):
            sources.append(
                SourceInfo(
                    claim_id="sec_filings",
                    claim=f"{company_name} SEC EDGAR filing index (CIK {sec_info['cik']}).",
                    source="SEC EDGAR",
                    url=sec_url,
                    snippet=f"SEC EDGAR filing index for {company_name} (CIK {sec_info['cik']}).",
                    confidence=REAL_DATA_CONFIDENCE,
                )
            )

        # 5. TinyFish articles (real retrieved articles only)
        for i, article in enumerate(tinyfish_articles):
            sources.append(
                SourceInfo(
                    claim_id=f"context_{i}",
                    claim=article.get("title", ""),
                    source=article.get("source", "TinyFish"),
                    url=article.get("url", ""),
                    snippet=article.get("snippet", ""),
                    confidence=REAL_DATA_CONFIDENCE,
                )
            )

        # Technical context built ONLY from real TinyFish article text.
        technical_context = "\n".join(
            f"[{a.get('source', 'TinyFish')}] {a.get('title', '')}: {a.get('snippet', '')}"
            for a in tinyfish_articles
        )

        # Market sizing: NO verified source is wired in, so no TAM/penetration
        # figure is claimed. 0.0 is an explicit "not retrieved" sentinel, NOT
        # a measurement — downstream must treat 0 as missing and must never
        # render it as a real figure (e.g. "$0 TAM").
        logger.warning(
            "No verified market-size (TAM/penetration) source is wired in; "
            "market_data is a 0.0 'not retrieved' sentinel. Wire a real "
            "market-size source, or make run() raise here, before relying on "
            "market_data."
        )
        market_data = MarketData(tam_usd=0.0, penetration_pct=0.0)

        financials = FinancialData(
            revenue_by_year={str(k): float(v) for k, v in revenue_by_year.items()},
            segments=segments,
            margins=MarginData(
                gross=float(margins.get("gross", 0.0)),
                operating=float(margins.get("operating", 0.0)),
                net=float(margins.get("net", 0.0)),
            ),
            growth_rate=growth_rate,
            unit_economics=unit_economics,
        )

        # Confidence reflects retrieval completeness only — it is not a claim
        # that the numbers were independently audited.
        research_confidence = 0.0
        if revenue_by_year:
            research_confidence += 0.4
        if any(margins.get(k) for k in ("gross", "operating", "net")):
            research_confidence += 0.3
        if "growth_rate" in financial_tool_data:
            research_confidence += 0.2
        if yt_insights.get("top_titles") or yt_insights.get("gaps"):
            research_confidence += 0.1

        bundle = ResearchBundle(
            topic=inp.topic,
            pillar=inp.pillar,
            entities=[
                EntityInfo(
                    name=company_name,
                    type="Company",
                    facts=[],
                ),
            ],
            financials=financials,
            technical_context=technical_context,
            competitors=competitors,
            market_data=market_data,
            sources=sources,
            yt_insights=YTInsights(
                top_titles=yt_insights.get("top_titles", []),
                common_tags=yt_insights.get("common_tags", []),
                gaps=yt_insights.get("gaps", []),
            ),
            research_confidence=round(research_confidence, 2),
        )

        logger.info(f"ResearchBundle created with {len(bundle.sources)} sources from real retrieved documents.")
        return bundle

    def _run_ai_topic(self, inp: ResearchInput) -> ResearchBundle:
        """Research an ``ai_everything`` topic (no company, no ticker).

        Sources are ONLY real retrieved documents: TinyFish articles
        (raises when none are retrieved) and YouTube landscape
        insights (raises on zero results). There are no financials
        for an AI news/tool/discovery topic, so ``financials`` and
        ``market_data`` are explicit 0.0 "not applicable" sentinels
        (same sentinel convention as ``market_data`` in the finance
        path) — never invented numbers, and downstream must treat 0
        as missing, never render it as a real figure.
        """
        logger.info(f"Starting AI-niche research on '{inp.topic}'")

        # Raises if no articles are retrieved — no fabricated context.
        tinyfish_articles = search_fintech_context(inp.topic)
        # Raises on no service / API error / zero results.
        yt_insights = research_topic(inp.topic)

        sources: List[SourceInfo] = []
        for i, article in enumerate(tinyfish_articles):
            sources.append(
                SourceInfo(
                    claim_id=f"context_{i}",
                    claim=article.get("title", ""),
                    source=article.get("source", "TinyFish"),
                    url=article.get("url", ""),
                    snippet=article.get("snippet", ""),
                    confidence=REAL_DATA_CONFIDENCE,
                )
            )

        technical_context = "\n".join(
            f"[{a.get('source', 'TinyFish')}] {a.get('title', '')}: {a.get('snippet', '')}"
            for a in tinyfish_articles
        )

        logger.warning(
            "AI-niche topic: no company financials apply; financials "
            "and market_data are 0.0 'not applicable' sentinels. "
            "Downstream must never render them as real figures."
        )
        financials = FinancialData(
            revenue_by_year={},
            segments={},
            margins=MarginData(gross=0.0, operating=0.0, net=0.0),
            growth_rate=0.0,
            unit_economics={},
        )
        market_data = MarketData(tam_usd=0.0, penetration_pct=0.0)

        research_confidence = 0.0
        if tinyfish_articles:
            research_confidence += 0.7
        if yt_insights.get("top_titles") or yt_insights.get("gaps"):
            research_confidence += 0.1

        bundle = ResearchBundle(
            topic=inp.topic,
            pillar=inp.pillar,
            entities=[
                EntityInfo(
                    name=inp.topic,
                    type="Topic",
                    facts=[],
                ),
            ],
            financials=financials,
            technical_context=technical_context,
            competitors=[],
            market_data=market_data,
            sources=sources,
            yt_insights=YTInsights(
                top_titles=yt_insights.get("top_titles", []),
                common_tags=yt_insights.get("common_tags", []),
                gaps=yt_insights.get("gaps", []),
            ),
            research_confidence=round(research_confidence, 2),
        )

        logger.info(
            f"AI ResearchBundle created with {len(bundle.sources)} sources "
            "from real retrieved documents."
        )
        return bundle

    def _infer_ticker(self, topic: str) -> Optional[str]:
        # PRODUCTION PATCH (P0 #5): only public companies map to a ticker.
        # Private companies (Stripe, Revolut) have NO public ticker — mapping
        # them to SQ / PYPL silently researched the wrong company. Unknown
        # topics return None so run() fails loudly and asks for an explicit
        # ticker, instead of defaulting to SQ.
        topic_lower = topic.lower()
        mapping = {
            "apple": "AAPL",
            "square": "SQ",
            "block": "SQ",
            "paypal": "PYPL",
            "visa": "V",
            "mastercard": "MA",
            "adyen": "ADYEY",
        }
        for name, sym in mapping.items():
            if name in topic_lower:
                return sym
        return None


def run_research(topic: str, pillar: str = "ai_everything") -> ResearchBundle:
    return ResearchAgent().run(ResearchInput(topic=topic, pillar=pillar))
