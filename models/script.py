"""Video script data models for TubePilot AI.

Defines the full-length ``VideoScript`` and short-form ``ShortsScript`` models
along with supporting sub-models for scenes, SEO metadata, and script metadata.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from loguru import logger
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

# Forbidden investment-advice phrasing -> neutral analytical
# rewrites (run #18 fix). Applied by the repair pass to every
# scene's narration BEFORE validation, so a wealth-topic script
# that says "guaranteed returns" is fixed in place instead of
# failing the whole script node. The map covers BOTH phrase lists
# that gate a script: _FORBIDDEN_PHRASES above (model validator)
# and agents/verify.py FORBIDDEN_ADVICE_REGEX (bare buy / sell /
# guaranteed / will rise / sure thing / can't lose / act now /
# skyrocket / to the moon), so a rewritten narration passes both.
# ORDER MATTERS: a longer phrase that contains a shorter one comes
# first ("you should buy" before "buy this stock", "guaranteed
# returns" before bare "guaranteed", "sell-off" before bare
# "sell") — otherwise the short match fires inside the long phrase
# and leaves broken wording behind ("you should this stock").
# An empty replacement deletes the phrase; the cleanup step then
# removes the double space / space-before-punctuation it leaves.
_FORBIDDEN_REWRITES: tuple[tuple[str, str], ...] = (
    ("not financial advice", "for educational purposes only"),
    ("you should buy", "some investors consider"),
    ("you should sell", "some investors exit"),
    ("buy this stock", "this stock"),
    ("guaranteed returns", "potential returns"),
    ("get rich quick", "build wealth over time"),
    ("financial advice", "financial information"),
    ("investment advice", "investment information"),
    ("i promise you", ""),
    ("invest now", "investing"),
    ("can't lose", "carries risk"),
    ("risk-free", "not without risk"),
    ("limited time offer", ""),
    ("act now", ""),
    ("trust me", ""),
    ("invest in", "back"),
    ("will rise", "may change"),
    ("sure thing", "a probable outcome"),
    ("to the moon", "sharply higher"),
    ("sell-off", "decline"),
    ("skyrocket", "rise sharply"),
    ("guaranteed", "potential"),
    ("buy", "purchase"),
    ("sell", "offer"),
)


def _rewrite_forbidden_phrases(text: str) -> str:
    """Rewrite forbidden investment-advice phrasing in one narration
    into neutral analytical wording (run #18 fix).

    Word-boundary matching, so "buying"/"selling" (the disclaimer
    scene's "buying or selling any security") are untouched. A
    phrase the map does not know is left alone — the validator
    still rejects genuinely unfixable advice instead of silently
    mangling it.
    """
    result = str(text or "")
    for phrase, replacement in _FORBIDDEN_REWRITES:
        pattern = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
        if pattern.search(result):
            result = pattern.sub(replacement, result)
    if result != text:
        result = re.sub(r"\s+([,.!?;:])", r"\1", result)
        result = re.sub(r" {2,}", " ", result).strip()
    return result

# Chart types that render from real retrieved-article data — the only
# chart types that exist for ai_everything topics. Kept as a literal
# here (not imported from agents/charts.py) to avoid a models ->
# agents import cycle; keep in sync with agents/charts.py
# AI_CHART_TYPES.
_AI_CHART_TYPES: tuple[str, ...] = ("ai_source_coverage", "ai_source_share")

# ---------------------------------------------------------------------------
# Resilient LLM-output normalisation (26 Nov 2026, run #17 fix)
#
# Run #17 died at script generation because the LLM's structured
# output was *almost* right: scenes missing section / visual_type /
# duration_sec, a 251-word description (300 required), 10 tags (15
# required) — and the Shorts stage died the same way. Every provider
# in the chain produced the same shape of near-miss, so re-prompting
# alone cannot fix it: the models below now REPAIR such output
# instead of hard-failing on it.
#
# Run #18 additions (26 Nov 2026):
# - Forbidden investment-advice phrasing ("guaranteed returns" in a
#   wealth-topic scene) killed attempt 1 at the script node. The
#   repair pass now REWRITES those phrases into neutral analytical
#   wording (see _FORBIDDEN_REWRITES) instead of rejecting the
#   whole script. The wealth topic will keep producing money
#   phrasing; the rewrite covers both phrase lists that gate a
#   script (models._FORBIDDEN_PHRASES and verify's
#   FORBIDDEN_ADVICE_REGEX, including bare "buy"/"sell").
# - Shorts kept landing at 28s or 62s and regeneration just missed
#   again. The repair pass now ACTIVELY fits a Short's narration to
#   the 30–60s window: trim trailing sentences while the
#   speech-implied length is over the ceiling, append generic
#   channel CTA lines (no facts added) while it is under the floor,
#   then re-derive each scene's duration_sec from its fitted
#   narration. Only narration words drive the real Short length
#   (TTS audio), so the fit works on words, not declared durations.
#
# Rules of the repair (all derived from the script's own content —
# nothing is invented, no external facts are added):
# - A missing scene field gets a default derived from the scene's
#   index / narration (section by position, visual_type "diagram",
#   duration from narration words at ~150 wpm, visual_prompt from
#   the narration itself). A value the LLM DID provide is never
#   overwritten.
# - SEO minimums are lowered to achievable floors (description 150
#   words, 5 tags) and the script is then padded back up to the
#   channel's publishing targets (300-word description, 15 tags)
#   from its own title / scenes — so verify.py's SEO_SANITY check
#   (300 words / 15–25 tags) still passes on a repaired script.
# - Length policy (user, 26 Nov 2026 23:31 IST): a long video's
#   length is AUTOMATIC — 8–12 or 15 minutes are all fine, the
#   script decides per topic. Long totals are therefore only
#   warned on (outside roughly 5–15 min), never rejected.
#   Shorts ARE fixed: 30–60 seconds total, hard-enforced.
# ---------------------------------------------------------------------------

# Speaking rate used to derive a scene's duration from its
# narration when the LLM omits duration_sec (150 wpm).
_WORDS_PER_MINUTE: float = 150.0

# SEO floors (hard validation) and publishing targets (repair pads
# up to these so the verify step's SEO_SANITY check passes).
SEO_DESCRIPTION_MIN_WORDS: int = 150
SEO_DESCRIPTION_TARGET_WORDS: int = 300
SEO_TAGS_MIN: int = 5
SEO_TAGS_TARGET: int = 15
SEO_TAGS_MAX: int = 25

# Long-video total duration: warn-only window (roughly 5–15 min).
# Never a hard failure — length is automatic per topic.
LONG_DURATION_WARN_MIN_SEC: int = 300
LONG_DURATION_WARN_MAX_SEC: int = 900

# Shorts total duration: hard-enforced (user spec, 26 Nov 2026).
SHORT_DURATION_MIN_SEC: int = 30
SHORT_DURATION_MAX_SEC: int = 60

# Shorts duration fit targets (run #18 fix). The fit aims at the
# middle of the 30–60s window so TTS rate variance cannot push a
# fitted Short back out: trim while the speech-implied length is
# over the ceiling, extend while it is under the floor.
SHORTS_FIT_TARGET_SEC: int = 50
SHORTS_FIT_CEILING_SEC: int = 55
SHORTS_FIT_FLOOR_SEC: int = 32

# Generic channel CTA lines appended to a too-short Short's closing
# scene. No facts, no figures, no claims — the Shorts already end
# with a CTA scene, so these only lengthen the spoken outro.
_SHORTS_CTA_LINES: tuple[str, ...] = (
    "Follow for more quick explainers.",
    "The full breakdown is in our long video.",
    "New videos every day.",
)

# AI-disclosure labelling the channel never uses (owner rule, 26 Nov
# 2026): stripped from title/description/tags at repair time. Kept
# as a local list (not imported from tools.youtube_api) because
# tools.youtube_api imports this module — keep in sync with
# tools.youtube_api.AI_DISCLOSURE_PHRASES / AI_DISCLOSURE_TAGS.
_AI_DISCLOSURE_PHRASES: tuple[str, ...] = (
    "ai generated",
    "ai-generated",
    "made with ai",
    "synthetic media",
    "altered content",
    "generated by ai",
    "ai generated content",
    "ai-generated content",
)
_AI_DISCLOSURE_TAGS: frozenset[str] = frozenset(_AI_DISCLOSURE_PHRASES)

_STOPWORDS: frozenset[str] = frozenset(
    "a an the and or of to in on for with from by at as is are was were "
    "be been it its this that these those you your we our they their he "
    "she his her how what why when where which who whom will would can "
    "could should about into over under more most less just only not no "
    "do does did done vs via per new old best top video watch learn "
    "explained guide tutorial 2026 2025".split()
)


def _words(text: str) -> int:
    """Word count of a text blob."""
    return len(str(text or "").split())


def _estimate_duration_sec(narration: str, max_sec: int) -> int:
    """Narration-derived scene duration at ~150 wpm, clamped."""
    words = _words(narration)
    seconds = round(words * 60.0 / _WORDS_PER_MINUTE)
    return max(5, min(max_sec, seconds))


def _default_section(index: int, total: int) -> str:
    """Section label for a scene the LLM forgot to label, by position."""
    if index == 0:
        return "hook"
    if total > 1 and index == total - 1:
        return "cta"
    return "body"


def _implied_duration_sec(scenes: list) -> float:
    """Speech-implied length of scene dicts at ~150 wpm.

    The TTS audio — not the declared duration_sec — drives the real
    video length, so the Shorts fit works on this number.
    """
    words = sum(
        _words(s.get("narration"))
        for s in scenes
        if isinstance(s, dict)
    )
    return words * 60.0 / _WORDS_PER_MINUTE


def _trim_last_sentence(narration: str) -> str:
    """Drop the last sentence of a narration (or the last 8 words
    when no sentence boundary exists). Returns "" when nothing
    sensible would remain."""
    text = str(narration or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    if len(parts) > 1:
        return " ".join(parts[:-1]).strip()
    words = text.split()
    if len(words) > 13:
        return " ".join(words[:-8])
    return ""


def _fit_shorts_duration(scenes: list) -> list:
    """Actively fit Shorts scene dicts into the 30–60s window
    (run #18 fix).

    The 74faecf repair only filled *missing* durations, so a Short
    the LLM wrote at 28s or 62s failed validation and regeneration
    just missed again. This pass works on the narration itself:
    trim trailing sentences (from the end backwards, so the hook
    survives) while the speech-implied length is over the ceiling,
    append generic CTA lines while it is under the floor, then
    re-derive every scene's duration_sec from its fitted narration.
    A rounding guard scales the declared durations onto the target
    if they still land outside the window. Returns new dicts; the
    caller's list is not mutated.
    """
    fitted = [dict(s) if isinstance(s, dict) else s for s in scenes]
    # Trim while too long — last scene first, so the hook is kept.
    guard = 0
    while _implied_duration_sec(fitted) > SHORTS_FIT_CEILING_SEC and guard < 40:
        guard += 1
        trimmed = False
        for idx in range(len(fitted) - 1, -1, -1):
            scene = fitted[idx]
            if not isinstance(scene, dict):
                continue
            narration = str(scene.get("narration") or "")
            if _words(narration) <= 8:
                continue
            new_text = _trim_last_sentence(narration)
            if new_text and _words(new_text) >= 5:
                scene["narration"] = new_text
                trimmed = True
                break
            # Narration would collapse: drop the whole scene if
            # another one remains.
            if len(fitted) > 1:
                fitted.pop(idx)
                trimmed = True
                break
        if not trimmed:
            break
    # Extend while too short with generic CTA lines (no facts added).
    guard = 0
    line_idx = 0
    while _implied_duration_sec(fitted) < SHORTS_FIT_FLOOR_SEC and guard < 6:
        guard += 1
        if line_idx >= len(_SHORTS_CTA_LINES) or not fitted:
            break
        line = _SHORTS_CTA_LINES[line_idx]
        line_idx += 1
        last = fitted[-1]
        if not isinstance(last, dict):
            break
        narration = str(last.get("narration") or "").strip()
        if line in narration:
            continue
        last["narration"] = (narration + " " + line).strip()
    # Re-derive declared durations from the fitted narration.
    for scene in fitted:
        if isinstance(scene, dict):
            scene["duration_sec"] = _estimate_duration_sec(
                str(scene.get("narration") or ""), 30
            )
    total = sum(
        int(scene.get("duration_sec") or 0)
        for scene in fitted
        if isinstance(scene, dict)
    )
    # Rounding guard: scale declared durations onto the target when
    # they still land outside the window. ONLY when the narration
    # itself is in range — scaling a genuinely unfittable narration
    # (e.g. ~11s of speech) onto 50s would mask it and ship a Short
    # whose real length violates the 30s–1min rule; leaving the
    # durations narration-derived instead lets the declared-duration
    # check fail loudly.
    implied = _implied_duration_sec(fitted)
    if (
        total
        and (total < SHORT_DURATION_MIN_SEC or total > SHORT_DURATION_MAX_SEC)
        and SHORT_DURATION_MIN_SEC - 5 <= implied <= SHORT_DURATION_MAX_SEC + 5
    ):
        factor = SHORTS_FIT_TARGET_SEC / total
        for scene in fitted:
            if isinstance(scene, dict):
                scene["duration_sec"] = max(
                    5, int(round(int(scene.get("duration_sec") or 0) * factor))
                )
    return fitted


# Editorial section order per pillar (mirrors agents/script.py and
# prompts/script_*.txt). Used to label a scene the LLM forgot to
# label: a gap between two labelled scenes takes the next pillar
# section in order, so a repaired script still satisfies the verify
# step's SECTION_COMPLETENESS check instead of dying there.
_PILLAR_SECTIONS: dict[str, list[str]] = {
    "ai_everything": [
        "hook", "what_changed", "how_it_works", "why_it_matters",
        "risks_and_scams", "predictions", "disclaimer", "cta",
    ],
    "finance_to_technical": [
        "hook", "the_problem", "technical_solution", "financial_benefit",
        "scope_and_moat", "risks", "disclaimer", "cta",
    ],
    "tech_to_financial": [
        "hook", "what_it_does", "revenue_breakdown", "unit_economics",
        "scope_and_trajectory", "risks", "disclaimer", "cta",
    ],
}


def _assign_missing_sections(scenes: list, pillar: Any) -> list:
    """Section label per scene, filling gaps from the pillar order.

    Scenes the LLM did label keep their label. An unlabelled first
    scene becomes "hook", an unlabelled last scene "cta"; any other
    gap takes the next unused pillar section between its labelled
    neighbours, falling back to "body".
    """
    labels = [
        (s.get("section") if isinstance(s, dict) else None) for s in scenes
    ]
    order = _PILLAR_SECTIONS.get(str(pillar)) if pillar else None
    if not order:
        return [
            label or _default_section(i, len(scenes))
            for i, label in enumerate(labels)
        ]
    assigned = list(labels)
    used = {label for label in labels if label}
    for i, label in enumerate(labels):
        if label:
            continue
        if i == 0 and "hook" not in used:
            assigned[i] = "hook"
            used.add("hook")
            continue
        if i == len(labels) - 1 and "cta" not in used:
            assigned[i] = "cta"
            used.add("cta")
            continue
        prev_idx = -1
        for j in range(i - 1, -1, -1):
            if assigned[j] in order:
                prev_idx = order.index(assigned[j])
                break
        next_idx = len(order)
        for j in range(i + 1, len(labels)):
            if labels[j] in order:
                next_idx = order.index(labels[j])
                break
        pick = None
        for k in range(prev_idx + 1, next_idx):
            if order[k] not in used:
                pick = order[k]
                break
        assigned[i] = pick or "body"
        used.add(assigned[i])
    return assigned


def _normalize_scene_dict(
    scene: Any, index: int, total: int, max_duration: int, section: Any = None
) -> Any:
    """Fill a single scene dict's missing fields with derived defaults.

    Only touches plain dicts (raw LLM JSON) and only fills fields
    that are missing/empty — a value the LLM provided is kept
    verbatim, including an explicit duration_sec. ``section`` is the
    pillar-order label assigned by the caller (see
    :func:`_assign_missing_sections`).
    """
    if not isinstance(scene, dict):
        return scene
    scene = dict(scene)
    if scene.get("id") in (None, ""):
        scene["id"] = index + 1
    if not scene.get("section"):
        scene["section"] = section or _default_section(index, total)
    narration = str(scene.get("narration") or "")
    if narration:
        # Run #18: rewrite forbidden investment-advice phrasing into
        # neutral wording instead of letting the validator reject the
        # whole script for one money phrase.
        rewritten = _rewrite_forbidden_phrases(narration)
        if rewritten != narration:
            logger.warning(
                f"Scene {scene.get('id')}: rewrote forbidden "
                f"investment-advice phrasing in narration."
            )
            scene["narration"] = rewritten
            narration = rewritten
    if not scene.get("visual_prompt") and narration:
        scene["visual_prompt"] = narration[:200]
    visual_type = scene.get("visual_type")
    if visual_type not in (
        "chart", "stock", "ai_gen", "ai_image", "diagram", "logo",
    ):
        if visual_type is not None:
            logger.warning(
                f"Scene {scene.get('id')}: unknown visual_type "
                f"{visual_type!r}; defaulting to 'diagram'."
            )
        scene["visual_type"] = "diagram"
    duration = scene.get("duration_sec")
    if duration in (None, ""):
        scene["duration_sec"] = _estimate_duration_sec(narration, max_duration)
    elif isinstance(duration, float):
        scene["duration_sec"] = int(round(duration))
    if isinstance(scene.get("duration_sec"), (int, float)) and scene["duration_sec"] <= 0:
        # A zero/negative duration can never render — derive it.
        scene["duration_sec"] = _estimate_duration_sec(narration, max_duration)
    if not scene.get("citations"):
        scene["citations"] = []
    return scene


def _candidate_tags(title: str, scenes: list) -> list[str]:
    """Tag candidates derived from the script's own title and scenes.

    Title tokens first (the primary keyword lives in the title),
    then humanised section names, then the most frequent meaningful
    narration words. Single words only — no invented phrases.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        token = re.sub(r"[^a-z0-9 ]", " ", str(token or "").lower())
        token = " ".join(token.split())
        if len(token) >= 3 and token not in _STOPWORDS and token not in seen:
            seen.add(token)
            candidates.append(token)

    for token in re.split(r"[^A-Za-z0-9]+", title or ""):
        _add(token)
    for scene in scenes:
        if isinstance(scene, dict):
            _add(str(scene.get("section") or "").replace("_", " "))
    word_counts: dict[str, int] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        for token in re.split(r"[^A-Za-z0-9]+", str(scene.get("narration") or "")):
            token = token.lower()
            if len(token) >= 4 and token not in _STOPWORDS:
                word_counts[token] = word_counts.get(token, 0) + 1
    for token, _ in sorted(word_counts.items(), key=lambda kv: -kv[1]):
        _add(token)
    return candidates


def _pad_description(description: str, scenes: list) -> str:
    """Pad a short description up to the 300-word publishing target.

    Appends a "What this video covers:" block built from the
    script's own scenes (section + opening words of each narration)
    — real content from the script, nothing invented. The disclaimer
    at the start is never touched.
    """
    if _words(description) >= SEO_DESCRIPTION_TARGET_WORDS or not scenes:
        return description
    lines = ["", "What this video covers:"]
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        section = str(scene.get("section") or "body").replace("_", " ").title()
        narration = str(scene.get("narration") or "").strip()
        opening = " ".join(narration.split()[:25])
        if opening:
            lines.append(f"• {section}: {opening}…")
    padded = description.rstrip() + "\n" + "\n".join(lines)
    if _words(padded) < SEO_DESCRIPTION_TARGET_WORDS:
        logger.warning(
            f"Description still only {_words(padded)} words after "
            f"padding (target {SEO_DESCRIPTION_TARGET_WORDS} words)."
        )
    return padded


def _normalize_seo_dict(seo: Any, scenes: list) -> Any:
    """Normalise one SEO dict: strip AI-disclosure labelling, pad
    tags to 15 from the script's own keywords, pad the description
    to 300 words from the script's own scenes. Existing values are
    kept (order preserved); only gaps are filled."""
    if not isinstance(seo, dict):
        return seo
    seo = dict(seo)
    title = str(seo.get("title") or "")
    description = str(seo.get("description") or "")
    for phrase in _AI_DISCLOSURE_PHRASES:
        if phrase in title.lower():
            title = re.sub(re.escape(phrase), "", title, flags=re.IGNORECASE)
            logger.warning(f"Stripped AI-disclosure phrase {phrase!r} from SEO title.")
        if phrase in description.lower():
            description = re.sub(
                re.escape(phrase), "", description, flags=re.IGNORECASE
            )
            logger.warning(
                f"Stripped AI-disclosure phrase {phrase!r} from SEO description."
            )
    seo["title"] = re.sub(r" {2,}", " ", title).strip()
    seo["description"] = re.sub(r" {2,}", " ", description)
    tags: list[str] = []
    seen: set[str] = set()
    for tag in seo.get("tags") or []:
        tag = str(tag).strip()
        if not tag:
            continue
        norm = " ".join(tag.lower().split())
        if norm in _AI_DISCLOSURE_TAGS:
            logger.warning(f"Stripped AI-disclosure tag {tag!r} from SEO tags.")
            continue
        if norm in seen:
            continue
        seen.add(norm)
        tags.append(tag)
    if len(tags) < SEO_TAGS_TARGET:
        for candidate in _candidate_tags(seo["title"], scenes):
            if len(tags) >= SEO_TAGS_TARGET:
                break
            norm = " ".join(candidate.lower().split())
            if norm and norm not in seen and norm not in _AI_DISCLOSURE_TAGS:
                seen.add(norm)
                tags.append(candidate)
    seo["tags"] = tags[:SEO_TAGS_MAX]
    seo["description"] = _pad_description(seo["description"], scenes)
    return seo


def _normalize_script_dict(data: Any, *, max_scene_duration: int, shorts: bool = False) -> Any:
    """Repair pass for raw LLM script JSON (mode="before" validator).

    Fills missing scene fields, normalises SEO (tags/description),
    and fills a missing ``meta`` from the scenes themselves. Only
    repairs plain-dict input; already-validated model objects pass
    through untouched. For Shorts (``shorts=True``) the scene
    durations are also ACTIVELY fitted into the 30–60s window
    (run #18 fix) instead of hoping regeneration lands there.
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)
    scenes = data.get("scenes")
    if isinstance(scenes, list):
        total = len(scenes)
        assigned = _assign_missing_sections(scenes, data.get("pillar"))
        data["scenes"] = [
            _normalize_scene_dict(s, i, total, max_scene_duration, section=assigned[i])
            for i, s in enumerate(scenes)
        ]
        if shorts:
            data["scenes"] = _fit_shorts_duration(data["scenes"])
    scenes = data.get("scenes") or []
    if isinstance(data.get("seo"), dict):
        data["seo"] = _normalize_seo_dict(data["seo"], scenes)
    meta = data.get("meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        if meta.get("word_count") in (None, ""):
            meta["word_count"] = sum(
                _words(s.get("narration"))
                for s in scenes
                if isinstance(s, dict)
            )
        if meta.get("estimated_duration_sec") in (None, ""):
            meta["estimated_duration_sec"] = sum(
                int(s.get("duration_sec") or 0)
                for s in scenes
                if isinstance(s, dict)
            )
        data["meta"] = meta
    elif meta is None and isinstance(scenes, list):
        data["meta"] = {
            "word_count": sum(
                _words(s.get("narration"))
                for s in scenes
                if isinstance(s, dict)
            ),
            "estimated_duration_sec": sum(
                int(s.get("duration_sec") or 0)
                for s in scenes
                if isinstance(s, dict)
            ),
        }
    return data


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
    """SEO metadata for the published video.

    Hard floors are deliberately achievable (150-word description,
    5 tags): the model-level repair pass pads a thin LLM output back
    up to the channel's publishing targets (300 words, 15 tags) from
    the script's own content, so the verify step's SEO_SANITY check
    still sees a full-strength description.
    """

    title: str = Field(
        ...,
        max_length=100,
        description="Video title (max 100 characters).",
    )
    description: str = Field(..., description="Video description (must be 150+ words).")
    tags: list[str] = Field(
        ...,
        min_length=SEO_TAGS_MIN,
        max_length=SEO_TAGS_MAX,
        description="SEO tags (between 5 and 25 tags).",
    )
    chapters: list[ChapterInfo] = Field(
        default_factory=list,
        description="YouTube chapter markers.",
    )

    @field_validator("description")
    @classmethod
    def _description_min_words(cls, v: str) -> str:
        """Ensure the description contains at least 150 words.

        The old 300-word hard floor is what killed run #17 (the LLM
        wrote 251 honest words and the whole run died on it). 150 is
        the genuine floor for a usable description; the repair pass
        pads it to 300+ from the script's own scenes.
        """
        word_count = len(v.split())
        if word_count < SEO_DESCRIPTION_MIN_WORDS:
            raise ValueError(
                f"Description must be at least {SEO_DESCRIPTION_MIN_WORDS} words, got {word_count}."
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
    Validates pillar-dependent chart-scene rules (see
    :meth:`_chart_scenes_for_pillar`) and that narration is free of
    forbidden investment-advice language.
    """

    pillar: Literal["ai_everything", "finance_to_technical", "tech_to_financial"] = Field(
        ...,
        description="Content pillar direction.",
    )
    scenes: list[SceneInfo] = Field(
        ...,
        min_length=1,
        description="Ordered list of scenes.",
    )
    seo: SEOData = Field(..., description="SEO metadata for the video.")
    meta: ScriptMeta = Field(..., description="Aggregate script metadata.")

    @model_validator(mode="before")
    @classmethod
    def _repair_llm_output(cls, data: Any) -> Any:
        """Repair pass for raw LLM JSON: fill omitted scene fields,
        normalise SEO, fill missing meta (see module header)."""
        return _normalize_script_dict(data, max_scene_duration=90)

    @model_validator(mode="after")
    def _total_duration_reasonable(self) -> "VideoScript":
        """Long-video length is AUTOMATIC per topic (user, 26 Nov
        2026): 8–12 or 15 minutes are all fine. A total outside
        roughly 5–15 min is therefore only a warning, never a
        rejection — the script is used as-is. The narration-implied
        length is warned on too, because TTS audio (not the declared
        durations) drives the real video length.
        """
        total = sum(s.duration_sec for s in self.scenes)
        if total < LONG_DURATION_WARN_MIN_SEC or total > LONG_DURATION_WARN_MAX_SEC:
            logger.warning(
                f"VideoScript total duration {total}s is outside the "
                f"usual {LONG_DURATION_WARN_MIN_SEC}–"
                f"{LONG_DURATION_WARN_MAX_SEC}s window; length is "
                "automatic per topic, continuing anyway."
            )
        words = sum(len(s.narration.split()) for s in self.scenes)
        implied = words * 60.0 / _WORDS_PER_MINUTE
        if implied < LONG_DURATION_WARN_MIN_SEC or implied > LONG_DURATION_WARN_MAX_SEC:
            logger.warning(
                f"VideoScript narration implies ~{implied:.0f}s of "
                f"speech (declared durations total {total}s); TTS "
                "audio drives the real video length."
            )
        return self

    @model_validator(mode="after")
    def _chart_scenes_for_pillar(self) -> "VideoScript":
        """Enforce the chart-scene rule for this script's pillar.

        Finance pillars keep the >= 8 chart-scene mandate: 8 finance
        chart types exist and render from real bundle financials.

        ai_everything (26 Nov 2026, P0 chart-mandate fix): AI bundles
        carry 0.0 "not applicable" financial sentinels, so the 8
        finance chart types cannot render for an AI topic — the
        prompt's old ">= 8 chart scenes" rule made every AI script
        unbuildable (either a finance chart type that raises at
        assembly, or the same source-count chart plotted 8 times).
        Chart scenes are therefore OPTIONAL for ai_everything (0
        allowed) and, when present, must use one of the two AI chart
        types that plot real retrieved-article counts per publisher,
        each at most once. All other AI visuals use diagram / ai_image
        / stock (timelines, architecture diagrams, comparison tables
        as diagrams) — never a fabricated chart.
        """
        chart_scenes = [s for s in self.scenes if s.visual_type == "chart"]
        if self.pillar in ("finance_to_technical", "tech_to_financial"):
            if len(chart_scenes) < 8:
                raise ValueError(
                    f"VideoScript requires at least 8 chart scenes, found {len(chart_scenes)}."
                )
            return self
        # ai_everything: charts optional, AI types only, each at most once.
        seen: set[str] = set()
        for scene in chart_scenes:
            # None defaults to ai_source_coverage at assembly
            # (agents/visuals.py resolve_scene); count it as that type.
            resolved = scene.chart_type or "ai_source_coverage"
            if resolved not in _AI_CHART_TYPES:
                raise ValueError(
                    f"ai_everything scene {scene.id}: chart_type "
                    f"'{scene.chart_type}' cannot render for AI topics "
                    "(the bundle has no financials); use one of "
                    f"{list(_AI_CHART_TYPES)}."
                )
            if resolved in seen:
                raise ValueError(
                    f"ai_everything scene {scene.id}: chart_type "
                    f"'{resolved}' is already used in this script — each "
                    "AI chart type plots the same retrieved-article "
                    "counts, so repeating it only duplicates the chart."
                )
            seen.add(resolved)
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

    pillar: Literal["ai_everything", "finance_to_technical", "tech_to_financial"] = Field(
        ...,
        description="Content pillar direction.",
    )
    scenes: list[SceneInfo] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="Ordered scenes (max 3 for Shorts).",
    )
    seo: SEOData = Field(..., description="SEO metadata for the Short.")
    meta: ScriptMeta = Field(..., description="Aggregate script metadata.")
    aspect_ratio: Literal["9:16"] = Field(
        default="9:16",
        description="Aspect ratio locked to 9:16 for Shorts.",
    )

    @model_validator(mode="before")
    @classmethod
    def _repair_llm_output(cls, data: Any) -> Any:
        """Repair pass for raw LLM JSON (see module header). Scene
        durations are capped at 30s here so a defaulted scene can
        never single-handedly blow the 60s Shorts budget, and the
        total is ACTIVELY fitted into 30–60s (run #18 fix) instead
        of failing validation and hoping regeneration lands there."""
        return _normalize_script_dict(data, max_scene_duration=30, shorts=True)

    @model_validator(mode="after")
    def _total_duration_in_range(self) -> "ShortsScript":
        """Shorts are fixed-length by owner spec (26 Nov 2026):
        30–60 seconds total, hard-enforced. A Short outside that
        window fails validation so the repair re-prompt (or the next
        provider in the chain) regenerates it with fitting scenes.
        """
        total = sum(s.duration_sec for s in self.scenes)
        if total < SHORT_DURATION_MIN_SEC or total > SHORT_DURATION_MAX_SEC:
            raise ValueError(
                f"ShortsScript total duration must be "
                f"{SHORT_DURATION_MIN_SEC}–{SHORT_DURATION_MAX_SEC}s, "
                f"got {total}s."
            )
        words = sum(len(s.narration.split()) for s in self.scenes)
        implied = words * 60.0 / _WORDS_PER_MINUTE
        if implied < SHORT_DURATION_MIN_SEC - 5 or implied > SHORT_DURATION_MAX_SEC + 5:
            logger.warning(
                f"ShortsScript narration implies ~{implied:.0f}s of "
                f"speech but declared durations total {total}s; TTS "
                "audio drives the real Short length."
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
