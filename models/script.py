"""Video script data models for TubePilot AI.

Defines the full-length ``VideoScript`` and short-form ``ShortsScript`` models
along with supporting sub-models for scenes, SEO metadata, and script metadata.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "SceneInfo",
    "ChapterInfo",
    "SEOData",
    "ScriptMeta",
    "VideoScript",
    "ShortsScript",
]

# Phrases that could constitute investment advice and must never appear in narration.
_FORBIDDEN_PHRASES: list[str] = [
    "not financial advice",
    "buy this stock",
    "guaranteed returns",
    "get rich quick",
    "invest now",
    "you should buy",
    "you should sell",
    "financial advice",
    "investment advice",
    "trust me",
    "I promise you",
    "can't lose",
    "risk-free",
    "act now",
    "limited time offer",
]

_FORBIDDEN_PATTERN: re.Pattern[str] = re.compile(
    "|".join(re.escape(p) for p in _FORBIDDEN_PHRASES),
    re.IGNORECASE,
)


class SceneInfo(BaseModel):
    """A single scene within a video script."""

    id: int = Field(..., description="Unique scene identifier (1-indexed).")
    section: str = Field(..., description="Script section this scene belongs to.")
    narration: str = Field(..., description="Narrator text for this scene.")
    visual_prompt: str = Field(
        ...,
        description="Prompt used to generate or select the visual.",
    )
    visual_type: Literal["chart", "stock", "ai_gen", "ai_image", "diagram", "logo"] = Field(
        ...,
        description="Type of visual asset for this scene.",
    )
    chart_type: Optional[str] = Field(
        default=None,
        description="Chart sub-type when visual_type is 'chart'.",
    )
    duration_sec: int = Field(
        ...,
        gt=0,
        description="Scene duration in seconds (must be > 0).",
    )
    citations: list[str] = Field(
        default_factory=list,
        description="Claim IDs cited in this scene's narration.",
    )


class ChapterInfo(BaseModel):
    """A YouTube chapter marker."""

    time: str = Field(
        ...,
        description="Timestamp in MM:SS or HH:MM:SS format.",
    )
    title: str = Field(..., description="Chapter title.")


class SEOData(BaseModel):
    """SEO metadata for the published video."""

    title: str = Field(
        ...,
        max_length=100,
        description="Video title (max 100 characters).",
    )
    description: str = Field(..., description="Video description (must be 300+ words).")
    tags: list[str] = Field(
        ...,
        min_length=15,
        max_length=25,
        description="SEO tags (between 15 and 25 tags).",
    )
    chapters: list[ChapterInfo] = Field(
        default_factory=list,
        description="YouTube chapter markers.",
    )

    @field_validator("description")
    @classmethod
    def _description_min_words(cls, v: str) -> str:
        """Ensure the description contains at least 300 words."""
        word_count = len(v.split())
        if word_count < 300:
            raise ValueError(
                f"Description must be at least 300 words, got {word_count}."
            )
        return v


class ScriptMeta(BaseModel):
    """Aggregate metadata about a generated script."""

    word_count: int = Field(..., description="Total word count of all narration.")
    estimated_duration_sec: int = Field(
        ...,
        description="Estimated video duration in seconds.",
    )


class VideoScript(BaseModel):
    """Full-length YouTube video script.

    Contains an ordered list of scenes, SEO metadata, and aggregate stats.
    Validates that the script meets minimum chart-scene requirements and
    that narration is free of forbidden investment-advice language.
    """

    pillar: Literal["finance_to_technical", "tech_to_financial"] = Field(
        ...,
        description="Content pillar direction.",
    )
    scenes: list[SceneInfo] = Field(
        ...,
        description="Ordered list of scenes.",
    )
    seo: SEOData = Field(..., description="SEO metadata for the video.")
    meta: ScriptMeta = Field(..., description="Aggregate script metadata.")

    @model_validator(mode="after")
    def _min_chart_scenes(self) -> "VideoScript":
        """Ensure at least 8 scenes have visual_type == 'chart'."""
        chart_count = sum(1 for s in self.scenes if s.visual_type == "chart")
        if chart_count < 8:
            raise ValueError(
                f"VideoScript requires at least 8 chart scenes, found {chart_count}."
            )
        return self

    @model_validator(mode="after")
    def _no_forbidden_phrases_in_narration(self) -> "VideoScript":
        """Reject scripts containing forbidden investment-advice language."""
        violations: list[tuple[int, str]] = []
        for scene in self.scenes:
            match = _FORBIDDEN_PATTERN.search(scene.narration)
            if match:
                violations.append((scene.id, match.group()))
        if violations:
            details = "; ".join(
                f"scene {sid}: '{phrase}'" for sid, phrase in violations
            )
            raise ValueError(
                f"Narration contains forbidden investment-advice phrases: {details}"
            )
        return self


class ShortsScript(BaseModel):
    """YouTube Shorts script (vertical, ≤ 60 seconds).

    Mirrors the ``VideoScript`` structure but enforces short-form constraints:
    maximum 3 scenes, total duration ≤ 60 seconds, and a 9:16 aspect ratio.
    """

    pillar: Literal["finance_to_technical", "tech_to_financial"] = Field(
        ...,
        description="Content pillar direction.",
    )
    scenes: list[SceneInfo] = Field(
        ...,
        max_length=3,
        description="Ordered scenes (max 3 for Shorts).",
    )
    seo: SEOData = Field(..., description="SEO metadata for the Short.")
    meta: ScriptMeta = Field(..., description="Aggregate script metadata.")
    aspect_ratio: Literal["9:16"] = Field(
        default="9:16",
        description="Aspect ratio locked to 9:16 for Shorts.",
    )

    @model_validator(mode="after")
    def _max_duration(self) -> "ShortsScript":
        """Ensure total scene duration does not exceed 60 seconds."""
        total = sum(s.duration_sec for s in self.scenes)
        if total > 60:
            raise ValueError(
                f"ShortsScript total duration must be ≤ 60s, got {total}s."
            )
        return self

    @model_validator(mode="after")
    def _no_forbidden_phrases_in_narration(self) -> "ShortsScript":
        """Reject scripts containing forbidden investment-advice language."""
        violations: list[tuple[int, str]] = []
        for scene in self.scenes:
            match = _FORBIDDEN_PATTERN.search(scene.narration)
            if match:
                violations.append((scene.id, match.group()))
        if violations:
            details = "; ".join(
                f"scene {sid}: '{phrase}'" for sid, phrase in violations
            )
            raise ValueError(
                f"Narration contains forbidden investment-advice phrases: {details}"
            )
        return self
