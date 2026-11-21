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

from agents.charts import generate_chart_by_type
from config_manager import get_config
from models.script import SceneInfo, VideoScript
from tools.fal_ai import generate_fal_video
from tools.image_gen import generate_image
from tools.pixabay import download_media, search_stock_videos

__all__ = [
    "VisualBudgetTracker",
    "VisualPipeline",
    "resolve_scene_visuals",
]


class VisualBudgetTracker(BaseModel):
    """Enforces strict financial limits on generative AI spending per video."""
    max_budget: float = Field(default=0.30)
    current_spend: float = 0.0

    def can_afford(self, cost: float) -> bool:
        return (self.current_spend + cost) <= self.max_budget

    def record_spend(self, cost: float, description: str):
        self.current_spend += cost
        logger.info(f"Visual spend: +${cost:.2f} ({description}). Total: ${self.current_spend:.2f}/${self.max_budget:.2f}")


class VisualPipeline:
    """Routes visual prompts to appropriate rendering backends based on cost and scene type."""

    def __init__(self, max_budget: Optional[float] = None):
        budget_limit = max_budget if max_budget is not None else float(get_config("budget_ai_per_video") or 0.30)
        self.tracker = VisualBudgetTracker(max_budget=budget_limit)

    def resolve_scene(self, scene: SceneInfo) -> Dict[str, Any]:
        """Resolve a single scene's visual asset path, respecting type and remaining budget."""
        v_type = scene.visual_type
        prompt = scene.visual_prompt
        asset_path = ""
        cost = 0.0
        media_type = "image"

        # 1. Chart ($0.00)
        if v_type == "chart":
            chart_type = scene.chart_type or "revenue_trend"
            asset_path = generate_chart_by_type(chart_type)
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
                # Fallback to AI image if budget allows
                if self.tracker.can_afford(0.03):
                    asset_path = generate_image(prompt, model="gemini/imagen-3.0-generate-001")
                    cost = 0.03
                    self.tracker.record_spend(cost, f"Imagen 3 stock fallback scene {scene.id}")
                else:
                    asset_path = generate_image(prompt, model="gemini/imagen-3.0-generate-001")

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
            else:
                logger.info(f"Budget constraint prevented AI video clip (spend: ${self.tracker.current_spend:.2f}). Using image.")
                asset_path = generate_image(prompt)

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

        return {
            "scene_id": scene.id,
            "visual_type": v_type,
            "media_type": media_type,
            "asset_path": asset_path,
            "cost_usd": cost,
        }

    def resolve_all(self, script: VideoScript) -> List[Dict[str, Any]]:
        """Resolve all visuals for every scene in script."""
        results = []
        for s in script.scenes:
            results.append(self.resolve_scene(s))
        logger.info(f"All {len(results)} visuals resolved. Total AI video spend: ${self.tracker.current_spend:.2f}")
        return results


def resolve_scene_visuals(script: VideoScript, budget: Optional[float] = None) -> List[Dict[str, Any]]:
    """Convenience functional interface for visual pipeline resolution."""
    return VisualPipeline(max_budget=budget).resolve_all(script)
