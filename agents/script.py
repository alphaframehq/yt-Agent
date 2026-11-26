# LOCAL PRODUCTION PATCH — 2026-11-25 (applied via PR #1, branch fix/production-mocks)..
# Fixes P0 #6 (audit list, 25 Nov 2026): ScriptAgent previously never called an
# LLM and returned 13 fixed template scenes with hardcoded numbers (65%, 68%,
# $2.5T, <5%, 1.8%, ...) while the SEO text falsely claimed figures were
# "verified directly against official SEC EDGAR filings" with
# "zero-hallucination factual accuracy". This patch routes long-form scripts
# and Shorts through tools.llm_provider.call_with_fallback() using the repo's
# prompt files, renders numbers ONLY from bundle.financials / bundle sources,
# and RAISES (no template fallback) when the LLM is unavailable. All false
# verification claims are removed.
"""Agent 2: Script Agent for TubePilot AI.

Transforms a verified `ResearchBundle` into:
1. Long-form documentary `VideoScript` (8-12 minutes, pillar-appropriate
   visuals — 8+ chart scenes for finance pillars, optional AI-native
   charts for ai_everything — strict provenance)
2. Daily short-form `ShortsScript` instances (60s max, 9:16 vertical aspect ratio)

Adheres strictly to editorial pillar section orders:
- finance_to_technical: hook -> the_problem -> technical_solution -> financial_benefit -> scope_and_moat -> risks -> disclaimer -> cta
- tech_to_financial: hook -> what_it_does -> revenue_breakdown -> unit_economics -> scope_and_trajectory -> risks -> disclaimer -> cta
- ai_everything: hook -> what_changed -> how_it_works -> why_it_matters -> risks_and_scams -> predictions -> disclaimer -> cta

All narration numbers come from the ResearchBundle via a real LLM call.
There is no template fallback: if the LLM is unavailable, generation raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from loguru import logger
from pydantic import BaseModel, Field

from models.research import ResearchBundle
from models.script import (
    ShortsScript,
    VideoScript,
)
from config_manager import get_channel_branding, get_config
from tools.llm_provider import call_with_fallback

__all__ = ["ScriptAgent", "run_script", "generate_shorts"]

PROMPT_FINANCE_PATH = Path("prompts/script_finance_to_technical.txt")
PROMPT_TECH_PATH = Path("prompts/script_tech_to_financial.txt")
PROMPT_AI_PATH = Path("prompts/script_ai_everything.txt")

DISCLAIMER_TEXT = (
    "DISCLAIMER: This video is for informational, educational, and software engineering analysis purposes only. "
    "Nothing stated here constitutes financial, investment, legal, or tax advice. We do not endorse buying or selling any security."
)


class _ShortsList(BaseModel):
    """Wrapper so the LLM can return several Shorts in one structured call."""

    shorts: List[ShortsScript] = Field(..., min_length=1)


def _llm_chain() -> List[List[str]]:
    """LLM fallback chain for script generation.

    Prefers an explicit ``script_chain`` config, then the repo's
    ``verify_chain``, then the single configured ``active_models.script``
    model (``gemini/gemini-3.8-flash`` by default).
    """
    for key in ("script_chain", "verify_chain"):
        try:
            chain = get_config(key)
        except Exception:
            chain = None
        if chain:
            return [[str(p), str(m)] for p, m in chain]
    model = "gemini/gemini-3.8-flash"
    try:
        model = get_config("active_models.script") or model
    except Exception:
        pass
    provider, _, name = model.partition("/")
    return [[provider or "gemini", name or "gemini-3.8-flash"]]


def _load_prompt(path: Path) -> str:
    """Read a repo prompt file. Missing prompt file = hard failure, never a silent default."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Script prompt file missing: {path}. Cannot generate a script without it."
        )
    return path.read_text(encoding="utf-8")


def _bundle_context(bundle: ResearchBundle) -> str:
    """Serialize ONLY the verified bundle data the LLM may cite numbers from.

    Every figure the LLM writes must come from this payload; nothing else
    (no model knowledge, no invented figures) may appear as a number.
    """
    payload = {
        "topic": bundle.topic,
        "pillar": bundle.pillar,
        "technical_context": bundle.technical_context,
        "financials": bundle.financials.model_dump(),
        "market_data": bundle.market_data.model_dump(),
        "competitors": [c.model_dump() for c in bundle.competitors],
        "entities": [e.model_dump() for e in bundle.entities],
        "sources": [s.model_dump() for s in bundle.sources],
        "yt_insights": bundle.yt_insights.model_dump(),
        "research_confidence": bundle.research_confidence,
        # Current-trend angles from the pre-script trend-research step
        # (tools/trends.py). Editorial hooks only — no numbers may be
        # taken from here; figures still come only from the bundle.
        "trend_angles": list(getattr(bundle, "trend_angles", []) or []),
        "trend_sources": list(getattr(bundle, "trend_sources", []) or []),
    }
    return json.dumps(payload, indent=2, default=str)


def _validate_citations(script: Any, bundle: ResearchBundle) -> None:
    """Every scene citation must be a real claim_id from the bundle."""
    valid_ids = {s.claim_id for s in bundle.sources}
    for scene in script.scenes:
        for cit in scene.citations:
            if cit not in valid_ids:
                raise ValueError(
                    f"LLM script cites unknown claim_id '{cit}' in scene {scene.id}; "
                    "refusing to use the script."
                )


def _validate_script(script: VideoScript, bundle: ResearchBundle) -> None:
    """Post-LLM guardrails: disclaimer scene, disclaimer-led SEO, valid citations."""
    sections = {s.section for s in script.scenes}
    if "disclaimer" not in sections:
        raise ValueError(
            "LLM script is missing the required 'disclaimer' scene; refusing to use it."
        )
    if not script.seo.description.startswith("DISCLAIMER"):
        raise ValueError(
            "LLM SEO description does not start with the compliance disclaimer; "
            "refusing to use it."
        )
    _validate_citations(script, bundle)


class ScriptAgent:
    """Generates high-impact, verification-gated YouTube scripts via LLM."""

    def __init__(self):
        pass

    def run(self, bundle: ResearchBundle, trend_report: Any = None) -> VideoScript:
        """Generate longform VideoScript from ResearchBundle via LLM.

        Raises (never falls back to a template) if the prompt file is
        missing, every LLM provider in the chain fails, or the returned
        script fails post-generation validation.

        ``trend_report`` (optional TrendReport from tools/trends.py,
        already attached to ``bundle.trend_angles`` by the pipeline
        trend node) steers the hook/angle toward what is trending
        today; it never supplies numbers.
        """
        logger.info(f"Generating VideoScript for '{bundle.topic}' (Pillar: {bundle.pillar})")

        prompt_path = (
            PROMPT_AI_PATH
            if bundle.pillar == "ai_everything"
            else (
                PROMPT_FINANCE_PATH
                if bundle.pillar == "finance_to_technical"
                else PROMPT_TECH_PATH
            )
        )
        system_prompt = _load_prompt(prompt_path)

        branding = get_channel_branding()
        channel_name = branding["channel_name"]
        cta_cfg = branding["cta"]
        cta_outro_tpl = cta_cfg.get("outro") or "Subscribe to {channel_name} for weekly engineering deep dives into the plumbing of global finance."
        try:
            cta_outro = cta_outro_tpl.format(channel_name=channel_name)
        except Exception:
            cta_outro = cta_outro_tpl

        user_prompt = (
            f"Write the long-form VideoScript for topic '{bundle.topic}' "
            f"(pillar: {bundle.pillar}) for channel '{channel_name}'.\n\n"
            "RESEARCH BUNDLE — the ONLY source of numbers, claims and citations "
            "(use no figure that is not in here):\n"
            f"{_bundle_context(bundle)}\n\n"
            "RULES:\n"
            "- HOOK (first 3 seconds): the 'hook' scene MUST open with a "
            "concrete, surprising, bundle-grounded claim — the single most "
            "startling verified fact from the research bundle (name the "
            "tool, model, paper, leak or event AND its concrete "
            "consequence) in the very first sentence. NEVER open with a "
            "generic intro: 'In this video…', 'In today's video…', "
            "'Welcome to…', 'Hello everyone…', \"Today we're going to…\", "
            "'Let's talk about…', 'This video is about…', 'Join me as…', "
            "'Before we begin…'. No throat-clearing, no greeting, no "
            "roadmap sentence. The hook must be a full sentence (10+ "
            "words) naming a specific thing or number from the bundle.\n"
            "- Every number in the narration MUST come from the research bundle above "
            "and MUST cite the matching claim_id from bundle.sources.\n"
            "- Do NOT invent revenue, margin, growth, TAM, penetration, take-rate or "
            "market-share figures.\n"
            "- Do NOT claim figures were verified against SEC EDGAR filings or any "
            "source not listed in bundle.sources; describe provenance only from the "
            "sources listed above.\n"
            "- Include one 'disclaimer' scene whose narration is exactly:\n"
            f'"{DISCLAIMER_TEXT}"\n'
            f"- End with a 'cta' scene using this outro line: \"{cta_outro}\"\n"
            "- Include exactly one verbal internal link in the 'why_it_matters' or "
            "'cta' scene (e.g. \"is topic par hamari ye video dekho — link description "
            "mein hai\"), plus the matching on-screen cue in that scene's visual_prompt "
            "('on-screen text: Watch next on this channel'). NEVER invent a specific "
            "video title, URL, or video ID for the link — no channel catalogue is in "
            "the research bundle; use only this generic pointer.\n"
            "- Visuals follow the pillar prompt: finance pillars need 8+ chart scenes; "
            "ai_everything uses optional AI-native charts only (ai_source_coverage / "
            "ai_source_share, each at most once) and diagram / ai_image for the rest — "
            "never a finance chart type for an AI topic.\n"
            "- The SEO description must be 300+ words and start with the disclaimer "
            "text above.\n"
            "- Output ONLY raw JSON conforming to the VideoScript schema "
            "(no markdown, no commentary)."
        )

        script = call_with_fallback(
            _llm_chain(), system_prompt, user_prompt, schema=VideoScript
        )
        _validate_script(script, bundle)
        logger.info(
            f"VideoScript generated via LLM for '{bundle.topic}': "
            f"{len(script.scenes)} scenes, {script.meta.word_count} words, "
            f"~{script.meta.estimated_duration_sec}s"
        )
        return script

    def generate_shorts(
        self, bundle: ResearchBundle, count: int = 3, trend_report: Any = None
    ) -> List[ShortsScript]:
        """Generate `count` high-retention YouTube Shorts (60s max, 9:16) via LLM.

        Short angles are chosen by the LLM from the research bundle
        (a new tool/capability, a training breakthrough, a scam/fraud
        warning, a prediction, pros vs cons, a leak, ...), not from
        fixed templates. Raises if the LLM is unavailable or
        returns fewer Shorts than requested — never pads with template Shorts.

        CHOICE (daily-output design, 26 Nov 2026): the 3 Shorts derive
        from the SAME day's topic/research as the long video — one
        research pass, one coherent channel day, and each Short teases
        one angle of the long video (with the trend angles steering
        which hooks lead). Distinct short topics would need 3 extra
        research + script cycles and 3x the LLM/time cost inside the
        60-minute daily job. ``trend_report`` (optional TrendReport)
        steers the Shorts' hooks toward today's trending angles.
        """
        logger.info(f"Generating {count} YouTube Shorts for '{bundle.topic}'")
        branding = get_channel_branding()
        channel_name = branding["channel_name"]
        cta_cfg = branding["cta"]
        short_subscribe_cta = cta_cfg.get("like_subscribe") or f"Follow {channel_name} for daily shorts and weekly longform breakdowns."

        system_prompt = (
            "You are the Senior Script Agent for TubePilot AI, writing short-form "
            "YouTube Shorts (60 seconds max, 9:16 vertical aspect ratio). "
            "Each Short has at most 3 scenes and total duration of at most 60 seconds. "
            "HOOK RULE: every Short opens in its first 1 second with a concrete, "
            "surprising, bundle-grounded claim — never a generic intro "
            "('In this video…', 'Welcome to…', 'Today we're going to…'). "
            "Tone: cold, analytical, engineering-first, cinematic. "
            "TTS-ready: no emojis, no markdown symbols (*, _, #) inside narration. "
            "NEVER use forbidden phrases: 'buy', 'sell', 'invest in', 'guaranteed', "
            "'sure thing', \"can't lose\", 'act now'. "
            "Every Short ends with a call-to-action scene. "
            "The SEO description of each Short must be 300+ words and start with "
            "the compliance disclaimer."
        )
        # Short angles are chosen by the LLM from the research bundle.
        # The examples differ by pillar: finance topics get economics
        # angles, AI topics get tool/discovery/scam/prediction angles.
        if bundle.pillar == "ai_everything":
            angle_hint = (
                "the new tool or discovery, how it actually works, "
                "a scam / red-flag check, a prediction or strategy "
                "takeaway, or a pros-vs-cons breakdown"
            )
        else:
            angle_hint = (
                "unit economics / take rate, revenue growth vs margins, "
                "market scope (TAM vs penetration), or a competitor "
                "comparison"
            )
        trend_block = ""
        if trend_report is not None:
            angles = list(getattr(trend_report, "angles", []) or [])
            if angles:
                trend_block = (
                    "\nTODAY'S TRENDING ANGLES — lead the Shorts' hooks with "
                    "these where they fit the bundle (editorial hooks only; "
                    "no numbers come from here):\n"
                    + "\n".join(f"- {a}" for a in angles)
                    + "\n"
                )
        user_prompt = (
            f"Write {count} distinct YouTube Shorts about '{bundle.topic}' "
            f"(pillar: {bundle.pillar}) for channel '{channel_name}'.\n\n"
            "Choose each Short's angle from the research bundle below — e.g. "
            f"{angle_hint}. Do NOT reuse fixed "
            "angles or fixed numbers.\n"
            f"{trend_block}\n"
            "RESEARCH BUNDLE — the ONLY source of numbers, claims and citations:\n"
            f"{_bundle_context(bundle)}\n\n"
            "RULES:\n"
            "- HOOK (first 1 second): every Short MUST open with a concrete, "
            "surprising, bundle-grounded claim in its very first words — "
            "the single most startling verified fact from the research "
            "bundle. NEVER open with a generic intro: 'In this video…', "
            "'Welcome to…', 'Hello everyone…', \"Today we're going to…\", "
            "'Let's talk about…'. No throat-clearing before the claim.\n"
            "- Every number MUST come from the research bundle above and MUST cite "
            "the matching claim_id from bundle.sources.\n"
            "- Do NOT invent figures. Do NOT claim verification against SEC EDGAR "
            "filings or any source not listed in bundle.sources.\n"
            "- Each Short's closing scene points at the full long video on the same "
            "topic (e.g. \"full breakdown hamari long video mein hai — link in "
            "description\"), never inventing a title or URL.\n"
            f"- Start every Short's SEO description with this disclaimer text: \"{DISCLAIMER_TEXT}\"\n"
            f"- Use this subscribe line in the Shorts' closing: \"{short_subscribe_cta}\"\n"
            "- Output ONLY raw JSON conforming to the schema {\"shorts\": [...]} "
            "(no markdown, no commentary)."
        )

        result = call_with_fallback(
            _llm_chain(), system_prompt, user_prompt, schema=_ShortsList
        )
        shorts = result.shorts[:count]
        if len(shorts) < count:
            raise ValueError(
                f"LLM returned {len(shorts)} Shorts, expected {count}; "
                "refusing to pad with template Shorts."
            )
        for short in shorts:
            if not short.seo.description.startswith("DISCLAIMER"):
                raise ValueError(
                    "LLM Short SEO description does not start with the compliance "
                    "disclaimer; refusing to use it."
                )
            _validate_citations(short, bundle)

        logger.info(f"Generated {len(shorts)} YouTube Shorts via LLM for '{bundle.topic}'")
        return shorts


def run_script(bundle: ResearchBundle, trend_report: Any = None) -> VideoScript:
    return ScriptAgent().run(bundle, trend_report=trend_report)


def generate_shorts(
    bundle: ResearchBundle, count: int = 3, trend_report: Any = None
) -> List[ShortsScript]:
    return ScriptAgent().generate_shorts(bundle, count=count, trend_report=trend_report)
