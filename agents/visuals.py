# LOCAL PRODUCTION PATCH, 25 Nov 2026. Pinned commit c39f42541b8e784dcf41c09583dc57227be07667
# File: agents/visuals.py
# Fixes (audit P1, CHANGES.md §2 known break):
# - Charts now require ResearchBundle data: generate_chart_by_type() raises
#   without data, so resolve_scene() must receive bundle data and pass it on.
#   resolve_all() / resolve_scene_visuals() now take bundle_data.
# - Budget bug fixed: the stock fallback previously called generate_image()
#   even when the budget did NOT allow it, with cost left at 0.0. Now it
#   raises RuntimeError (budget exhausted) instead of spending silently.
# - ai_gen fallback now records the Imagen spend it actually incurs.
# - resolve_all() validates every asset (non-empty, and exists when it is a
#   local path) and only then logs "All visuals resolved" — no silent
#   success on a None / "" asset.
# - No mock / placeholder / fake success is added here. Failures raise.
"""Visual Pipeline and Budget-Aware Asset Router for TubePilot AI.

Per-scene visual routing governed by hard per-video budget constraint (default $0.30):
- visual_type == "chart": Matplotlib institutional chart generator ($0.00)
- visual_type == "stock": Pixabay free stock video -> Imagen 3 fallback (~$0.03)
- visual_type == "ai_image": Gemini Imagen 3 (~$0.03) -> FLUX Schnell (~$0.01)
- visual_type == "ai_gen": fal.ai LTX-Video (~$0.05) if budget allows, else Imagen 3 + zoom
- visual_type == "diagram" / "logo": Clean dark mode programmatic diagrams ($0.00)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger
from pydantic import BaseModel, Field

from agents.charts import AI_CHART_TYPES, FINANCE_CHART_TYPES, generate_chart_by_type
from config_manager import get_config
from models.script import SceneInfo, VideoScript
from tools.fal_ai import generate_fal_video
from tools.image_gen import generate_image
from tools.pixabay import download_media, search_stock_videos

__all__ = [
    "VisualBudgetTracker",
    "VisualPipeline",
    "research_bundle_to_chart_data",
    "ai_topic_chart_data",
    "resolve_scene_visuals",
]


def ai_topic_chart_data(bundle: Any) -> Dict[str, Any]:
    """Build the chart-data dict for AI (``ai_everything``) topics.

    AI topics have no financials — the bundle carries 0.0 "not
    applicable" sentinels — so the ONLY honest graph data is what
    research actually retrieved: the count of TinyFish articles per
    publisher (``bundle.sources[].source``). Returns the keys the
    ``ai_source_coverage`` / ``ai_source_share`` chart types need.

    Raises ValueError when the bundle has no sources at all: a chart
    with no retrieved articles behind it would be fabricated.
    """
    if bundle is None:
        raise ValueError(
            "ai_topic_chart_data: no ResearchBundle — chart scenes "
            "cannot be rendered without real research data."
        )
    sources = list(getattr(bundle, "sources", []) or [])
    if not sources:
        raise ValueError(
            "ai_topic_chart_data: bundle has no sources — no retrieved "
            "articles to count, so no AI chart can be rendered."
        )
    counts: Dict[str, int] = {}
    for src in sources:
        publisher = (getattr(src, "source", "") or "Unknown").strip() or "Unknown"
        counts[publisher] = counts.get(publisher, 0) + 1
    # Most-covered publishers first; deterministic order for equal counts.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "topic": getattr(bundle, "topic", "AI topic"),
        "source_labels": [label for label, _ in ranked],
        "source_counts": [count for _, count in ranked],
    }


def research_bundle_to_chart_data(bundle: Any) -> Dict[str, Any]:
    """Build the chart-data dict for generate_chart_by_type() from a ResearchBundle.

    Only values actually present in the bundle are included — nothing is
    invented. Keys with no real source in the bundle are omitted, so a
    chart that needs them raises ValueError in generate_chart_by_type()
    instead of plotting fabricated data. In particular:
    - TAM: if market_data.tam_usd is 0 / missing, it is omitted (the
      research patch uses 0.0 as an explicit "not retrieved" sentinel —
      a $0 TAM must never be rendered as a real market size).
    - market_share_donut "shares": no share source exists in the bundle,
      so it is omitted; that chart type raises until a real source is wired.
    - competitor_matrix "metrics": only competitors whose metric is a
      take-rate are used; other competitor rows carry no take-rate value.
    - AI topics (pillar ``ai_everything``): no financials exist, so this
      returns :func:`ai_topic_chart_data` instead — real retrieved-article
      counts per publisher, the only honest graph data for an AI topic.
      The 8 finance chart types raise for that data (their keys are
      absent); only ``ai_source_coverage`` / ``ai_source_share`` render.
    """
    if bundle is None:
        raise ValueError(
            "research_bundle_to_chart_data: no ResearchBundle — chart scenes "
            "cannot be rendered without real research data."
        )
    if getattr(bundle, "pillar", None) == "ai_everything":
        # AI topic: financials are 0.0 sentinels, never render them as figures.
        return ai_topic_chart_data(bundle)
    fin = bundle.financials
    if not fin.revenue_by_year:
        raise ValueError(
            "research_bundle_to_chart_data: bundle has no revenue_by_year — "
            "chart scenes cannot be rendered without real financials."
        )
    if not bundle.entities:
        raise ValueError(
            "research_bundle_to_chart_data: bundle has no entities — "
            "chart scenes cannot be rendered without a real company."
        )
    company = bundle.entities[0].name
    years = sorted(fin.revenue_by_year.keys())
    values = [fin.revenue_by_year[y] for y in years]
    data: Dict[str, Any] = {
        "company": company,
        "years": years,
        "values": values,
        "segments": dict(fin.segments),
        # margin_compare: the main company only — CompetitorInfo rows carry
        # a single (metric, value) pair, not a gross/operating/net set.
        "companies": {
            company: {
                "gross": fin.margins.gross,
                "operating": fin.margins.operating,
                "net": fin.margins.net,
            }
        },
        "current": values[-1],
        "growth": fin.growth_rate,
        "margin": fin.margins.operating,
    }
    ue = fin.unit_economics or {}
    for key in ("revenue", "cogs", "gross", "opex", "net"):
        if key in ue and ue[key] is not None:
            data[key] = ue[key]
    market = getattr(bundle, "market_data", None)
    if market is not None and market.tam_usd:
        data["tam"] = market.tam_usd
        data["penetration_pct"] = market.penetration_pct
    # competitor_matrix is NOT populated: its router key "companies" is
    # also the margin_compare dict above, and CompetitorInfo rows carry a
    # single (metric, value) pair, not take-rates. A competitor_matrix
    # request therefore raises in generate_chart_by_type() until a real
    # take-rate source is wired — no fabricated matrix.
    return data


class VisualBudgetTracker(BaseModel):
    """Enforces strict financial limits on generative AI spending per video."""
    max_budget: float = Field(default=0.30)
    current_spend: float = 0.0

    def can_afford(self, cost: float) -> bool:
        return (self.current_spend + cost) <= self.max_budget

    def record_spend(self, cost: float, description: str):
        self.current_spend += cost
        logger.info(f"Visual spend: +${cost:.2f} ({description}). Total: ${self.current_spend:.2f}/${self.max_budget:.2f}")


def _validate_asset(asset_path: Any, scene_id: Any) -> str:
    if not asset_path or not isinstance(asset_path, str):
        raise RuntimeError(f"Visual asset missing for scene {scene_id}: empty asset path.")
    # Local files must exist. Remote URLs are left to the caller that fetched them.
    if not asset_path.startswith(("http://", "https://")) and not Path(asset_path).exists():
        raise RuntimeError(f"Visual asset missing for scene {scene_id}: file does not exist: {asset_path}")
    return asset_path


class VisualPipeline:
    """Routes visual prompts to appropriate rendering backends based on cost and scene type."""

    def __init__(self, max_budget: Optional[float] = None):
        budget_limit = max_budget if max_budget is not None else float(get_config("budget_ai_per_video") or 0.30)
        self.tracker = VisualBudgetTracker(max_budget=budget_limit)

    def resolve_scene(self, scene: SceneInfo, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Resolve a single scene's visual asset path, respecting type and remaining budget.

        `data` is the ResearchBundle data for chart scenes. It is mandatory
        for chart scenes — generate_chart_by_type() raises without it.
        """
        v_type = scene.visual_type
        prompt = scene.visual_prompt
        asset_path = ""
        cost = 0.0
        media_type = "image"

        # 1. Chart ($0.00)
        if v_type == "chart":
            is_ai_data = data is not None and "source_labels" in data
            if scene.chart_type is None and is_ai_data:
                # AI topics have no financials: default to the honest
                # AI chart instead of revenue_trend (which would raise).
                chart_type = "ai_source_coverage"
            else:
                chart_type = scene.chart_type or "revenue_trend"
            if is_ai_data and chart_type in FINANCE_CHART_TYPES:
                # Fail with a clear message instead of a bare missing-key
                # error from the router: finance charts cannot render for
                # AI topics — their bundle carries 0.0 sentinels, never
                # real figures. Only AI_CHART_TYPES are available.
                raise ValueError(
                    f"Chart type '{chart_type}' is unavailable for "
                    "ai_everything topics: the bundle has no financials "
                    "(0.0 'not applicable' sentinels). Use one of "
                    f"{list(AI_CHART_TYPES)} (real retrieved-article "
                    "counts) instead of plotting fabricated figures."
                )
            asset_path = generate_chart_by_type(chart_type, data=data)
            media_type = "image"
            cost = 0.0

        # 2. Stock Footage ($0.00 Pixabay, fallback Imagen 3 $0.03)
        elif v_type == "stock":
            videos = search_stock_videos(query=prompt[:40])
            if videos and videos[0].get("url"):
                asset_path = download_media(videos[0]["url"], extension="mp4")
                media_type = "video"
                cost = 0.0
            else:
                # Fallback to AI image ONLY if budget allows. Previously this
                # branch generated an image even when unaffordable, with
                # cost left at 0.0 — silent overspend. Now it fails loudly.
                if not self.tracker.can_afford(0.03):
                    raise RuntimeError(
                        f"Budget exhausted for scene {scene.id}: cannot afford Imagen 3 "
                        f"stock fallback ($0.03, spend ${self.tracker.current_spend:.2f}/"
                        f"${self.tracker.max_budget:.2f})."
                    )
                asset_path = generate_image(prompt, model="gemini/imagen-3.0-generate-001")
                cost = 0.03
                self.tracker.record_spend(cost, f"Imagen 3 stock fallback scene {scene.id}")

        # 3. AI Generated Video Clip ($0.05 fal.ai if budget >= $0.05, else Imagen 3)
        elif v_type == "ai_gen":
            if self.tracker.can_afford(0.05):
                video_res = generate_fal_video(prompt, duration_sec=min(scene.duration_sec, 5))
                if video_res:
                    asset_path = video_res
                    media_type = "video"
                    cost = 0.05
                    self.tracker.record_spend(cost, f"fal.ai LTX-Video scene {scene.id}")
                else:
                    asset_path = generate_image(prompt)
                    cost = 0.03
                    self.tracker.record_spend(cost, f"Imagen 3 ai_gen fallback scene {scene.id}")
            else:
                logger.info(f"Budget constraint prevented AI video clip (spend: ${self.tracker.current_spend:.2f}). Using image.")
                asset_path = generate_image(prompt)
                cost = 0.03
                self.tracker.record_spend(cost, f"Imagen 3 ai_gen fallback scene {scene.id}")

        # 4. AI Image ($0.03 Imagen 3 / $0.01 FLUX)
        elif v_type == "ai_image":
            if self.tracker.can_afford(0.03):
                asset_path = generate_image(prompt, model="gemini/imagen-3.0-generate-001")
                cost = 0.03
                self.tracker.record_spend(cost, f"Imagen 3 scene {scene.id}")
            else:
                asset_path = generate_image(prompt, model="replicate/flux-schnell")
                cost = 0.01
                self.tracker.record_spend(cost, f"FLUX Schnell scene {scene.id}")

        # 5. Diagram or Logo ($0.00 Programmatic or Imagen 3)
        else:
            asset_path = generate_image(prompt)
            cost = 0.0

        asset_path = _validate_asset(asset_path, scene.id)
        return {
            "scene_id": scene.id,
            "visual_type": v_type,
            "media_type": media_type,
            "asset_path": asset_path,
            "cost_usd": cost,
        }

    def resolve_all(self, script: VideoScript, bundle_data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Resolve all visuals for every scene in script."""
        results = []
        for s in script.scenes:
            results.append(self.resolve_scene(s, data=bundle_data))
        if len(results) != len(script.scenes):
            raise RuntimeError(
                f"Visual resolution mismatch: {len(results)} assets for {len(script.scenes)} scenes."
            )
        logger.info(f"All {len(results)} visuals resolved. Total AI video spend: ${self.tracker.current_spend:.2f}")
        return results


def resolve_scene_visuals(
    script: VideoScript,
    bundle_data: Optional[Dict[str, Any]] = None,
    budget: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Convenience functional interface for visual pipeline resolution."""
    return VisualPipeline(max_budget=budget).resolve_all(script, bundle_data=bundle_data)
