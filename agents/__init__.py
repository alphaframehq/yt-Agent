"""TubePilot AI multi-agent core modules."""

from agents.assemble import VideoAssembler, assemble_shorts, assemble_video
from agents.charts import (
    chart_to_clip,
    competitor_matrix,
    generate_chart_by_type,
    growth_margin_scatter,
    margin_compare,
    market_share_donut,
    revenue_trend,
    scope_tam,
    segment_breakdown,
    unit_economics_waterfall,
)
from agents.research import ResearchAgent, ResearchInput, run_research
from agents.script import ScriptAgent, generate_shorts, run_script
from agents.tts import TTSAgent, synthesize_scene_audio, synthesize_script_audio
from agents.verify import VerificationAgent, run_verification
from agents.visuals import VisualBudgetTracker, VisualPipeline, resolve_scene_visuals

__all__ = [
    # Agents
    "ResearchAgent",
    "ResearchInput",
    "run_research",
    "ScriptAgent",
    "run_script",
    "generate_shorts",
    "VerificationAgent",
    "run_verification",
    "TTSAgent",
    "synthesize_scene_audio",
    "synthesize_script_audio",
    "VisualPipeline",
    "VisualBudgetTracker",
    "resolve_scene_visuals",
    "VideoAssembler",
    "assemble_video",
    "assemble_shorts",
    # Charts
    "revenue_trend",
    "segment_breakdown",
    "margin_compare",
    "unit_economics_waterfall",
    "scope_tam",
    "competitor_matrix",
    "growth_margin_scatter",
    "market_share_donut",
    "generate_chart_by_type",
    "chart_to_clip",
]
