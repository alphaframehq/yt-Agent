# LOCAL PRODUCTION PATCH — 2026-11-25 (applied via PR #1, branch fix/production-mocks)..
# Fixes P0 #7 (audit list, 25 Nov 2026): _check_investment_advice_semantic()
# was a stub that always returned pass with no LLM call. It now performs a
# real LLM semantic check via tools.llm_provider.call_with_fallback() using
# prompts/verify_investment_advice.txt, and FAILS CLOSED (returns fail, never
# pass) when the LLM or the prompt file is unavailable.
# P1 verify loophole FIXED 26 Nov 2026 (Phase 2): _check_factual_accuracy()
# now grounds EVERY number against the scene's cited sources (claim +
# snippet) or the bundle financials/market corpus — a scene that cites
# any source no longer passes an unrelated hallucinated number. See
# _extract_numbers() / _check_factual_accuracy().
"""Agent 3: Verification Agent for TubePilot AI.

Audits `VideoScript` against `ResearchBundle` to catch hallucinations and compliance issues.

CRITICAL DESIGN:
Runs 8 DETERMINISTIC checks FIRST (<1 sec, 0 LLM tokens).
Only if all deterministic checks pass -> invokes LLM for 2 semantic checks.
This saves ~90% LLM tokens while catching almost all issues deterministically.

Deterministic Checks:
1. FACTUAL_ACCURACY — numbers in narration must be grounded in the scene's
   cited sources (claim + snippet) or the bundle financials/market corpus
2. CITATION_VALIDITY — every scene citation must exist in bundle.sources
3. DISCLAIMER_PRESENCE — spoken in narration + written in description top
4. INVESTMENT_ADVICE_REGEX — regex search for forbidden advisory terms
5. CONTRADICTIONS — conflicting numbers across scenes
6. SECTION_COMPLETENESS — required sections present per pillar
7. SEO_SANITY — title <= 100, tags 15-25, description 300+ words
8. HOOK_STRENGTH — opening must be a concrete, bundle-grounded hook,
   never a generic intro ("In this video we will…"); hook must be the
   first scene and carry a real claim in the first 3s (1s for Shorts)

LLM Semantic Checks:
9. INVESTMENT_ADVICE_SEMANTIC — subtle advice / FOMO detection
10. NARRATION_QUALITY — TTS hostility, markdown artifacts, emojis
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from loguru import logger
from pydantic import BaseModel, Field

from config_manager import get_config
from models.research import ResearchBundle
from models.script import VideoScript
from models.verification import CheckResult, Issue, SuggestedFix, VerificationReport
from tools.llm_provider import LLMProvider, call_with_fallback

__all__ = ["VerificationAgent", "run_verification"]

PROMPT_ADVICE_PATH = Path("prompts/verify_investment_advice.txt")
PROMPT_QUALITY_PATH = Path("prompts/verify_narration_quality.txt")


class _AdviceFlag(BaseModel):
    """One flagged phrase from the LLM semantic investment-advice verdict."""

    scene_id: Optional[int] = None
    text: str = ""
    reason: str = ""
    severity: str = "major"


class _AdviceVerdict(BaseModel):
    """Structured verdict matching prompts/verify_investment_advice.txt output."""

    has_investment_advice: bool = False
    confidence: float = 0.0
    flagged_phrases: List[_AdviceFlag] = Field(default_factory=list)

FORBIDDEN_ADVICE_REGEX = re.compile(
    r"\b(buy|sell|invest in|will rise|guaranteed|sure thing|can't lose|act now|skyrocket|to the moon)\b",
    re.IGNORECASE,
)

DISCLAIMER_REGEX = re.compile(
    r"(disclaimer|not financial advice|educational purposes only|informational.*?purposes|does not constitute.*?advice)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Numeric grounding helpers — P1 citation loophole fix, 26 Nov 2026.
# A number used to pass whenever its scene cited ANY source, even an
# unrelated one (the check only flagged numbers when the scene had NO
# citation at all, and matched on a bare digit substring — so "1" in
# "$1.5B" matched almost any text). These helpers extract every number
# in a text as a normalised (value, kind) pair so the check can ask:
# does THIS number, at THIS magnitude, appear in the cited source?
# ---------------------------------------------------------------------------

_MAGNITUDE_WORDS: Dict[str, float] = {
    "thousand": 1e3,
    "million": 1e6,
    "billion": 1e9,
    "trillion": 1e12,
}

_ONES_WORDS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS_WORDS: Dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORDS: frozenset = frozenset(
    list(_ONES_WORDS) + list(_TENS_WORDS)
    + ["hundred"] + list(_MAGNITUDE_WORDS)
)

_DIGIT_NUMBER_RE = re.compile(
    r"\$?\s*(\d[\d,]*\.?\d*)\s*(billion|million|trillion|thousand|%|percent)?",
    re.IGNORECASE,
)


def _words_to_number(words: List[str]) -> Optional[float]:
    """Convert number words (["sixty", "eight"] -> 68.0). None if invalid."""
    total = 0.0
    current = 0.0
    seen = False
    for w in words:
        if w in _ONES_WORDS:
            current += _ONES_WORDS[w]
            seen = True
        elif w in _TENS_WORDS:
            current += _TENS_WORDS[w]
            seen = True
        elif w == "hundred":
            current = (current or 1) * 100
            seen = True
        elif w in _MAGNITUDE_WORDS:
            total += (current or 1) * _MAGNITUDE_WORDS[w]
            current = 0.0
            seen = True
        elif w == "and":
            continue
        else:
            return None
    if not seen:
        return None
    return total + current


def _extract_numbers(text: str) -> Dict[Tuple[float, str], str]:
    """Extract numbers from *text* as {(value, kind): raw_match}.

    kind is "pct" for percentages, else "abs" (money and plain numbers
    share "abs" after magnitude scaling: "$1.2B", "1.2 billion" and
    "1200 million" all normalise to (1200000000.0, "abs")).

    Deliberately NOT extracted (not quantitative claims):
    - standalone 4-digit years (1900-2099, e.g. "in 2024");
    - bare unit-less numbers <= 10 (list counts like "3 reasons",
      model versions like "Claude 3.5", decimals like a "0.6" margin
      written as a fraction). A number WITH a unit (% / $ / billion /
      ...) is always extracted, whatever its size.
    Word-form numbers are handled too ("sixty-eight percent" -> 68 pct).
    """
    found: Dict[Tuple[float, str], str] = {}

    def _add(value: float, kind: str, raw: str, has_unit: bool) -> None:
        if kind == "abs" and not has_unit:
            if 1900 <= value <= 2099 and float(int(value)) == value:
                return  # a year, not a quantity
            if value <= 10:
                return  # list count / version / small fraction
        key = (value, kind)
        found.setdefault(key, raw.strip())

    for m in _DIGIT_NUMBER_RE.finditer(text or ""):
        raw_num, unit = m.group(1), (m.group(2) or "").lower()
        try:
            value = float(raw_num.replace(",", ""))
        except ValueError:
            continue
        if unit in ("billion", "million", "trillion", "thousand"):
            value *= _MAGNITUDE_WORDS[unit]
            kind = "abs"
            has_unit = True
        elif unit in ("%", "percent"):
            kind = "pct"
            has_unit = True
        else:
            kind = "abs"
            has_unit = m.group(0).strip().startswith("$")
        _add(value, kind, m.group(0), has_unit)

    # Word-form numbers ("sixty-eight percent", "two million users").
    # Keep match spans so the reported raw text preserves the original
    # spelling ("sixty-eight", not "sixty eight").
    word_matches = list(re.finditer(r"[a-z]+", (text or "").lower()))
    words = [m.group(0) for m in word_matches]
    i = 0
    while i < len(words):
        if words[i] not in _NUMBER_WORDS:
            i += 1
            continue
        j = i
        run: List[str] = []
        while j < len(words):
            w = words[j]
            if w in _NUMBER_WORDS:
                run.append(w)
                j += 1
            elif w == "and" and run and j + 1 < len(words) and words[j + 1] in _NUMBER_WORDS:
                run.append(w)
                j += 1
            else:
                break
        value = _words_to_number(run)
        if value is not None:
            # A run of bare magnitude words ("million" in "$800 million")
            # is the unit tail of a digit number already extracted above,
            # not a separate word-form number — skip it, or every
            # "$X million" would also invent a phantom 1,000,000 claim.
            if not any(
                w in _ONES_WORDS or w in _TENS_WORDS or w == "hundred"
                for w in run
            ):
                i = max(j, i + 1)
                continue
            kind = "pct" if (j < len(words) and words[j] == "percent") else "abs"
            raw = (text or "")[word_matches[i].start():word_matches[j - 1].end()]
            _add(value, kind, raw, has_unit=(kind == "pct"))
        i = max(j, i + 1)

    return found


def _is_grounded(
    value: float, kind: str, corpus: Dict[Tuple[float, str], str]
) -> bool:
    """True if (value, kind) appears in *corpus* (relative tolerance 1e-6)."""
    for (c_value, c_kind) in corpus:
        if c_kind != kind:
            continue
        if math.isclose(value, c_value, rel_tol=1e-6, abs_tol=1e-9):
            return True
    return False

# HOOK_STRENGTH (Phase 2, 26 Nov 2026): generic openers that waste the
# first 3 seconds (1 second for Shorts). A hook must open with a
# concrete, surprising, bundle-grounded claim — the one new thing and
# why it matters right now — never with throat-clearing. Matched
# against the start of the hook narration only.
HOOK_GENERIC_OPENERS = re.compile(
    r"^\s*(in this video|in today'?s video|in this episode|in this tutorial|"
    r"welcome( back)? to|hello (everyone|and welcome)|hey (guys|everyone)|"
    r"today (we|we'?re|we are) (going to|will)|in this video,? (we|i|we'?re|we are)|"
    r"this video is about|let'?s talk about|join me as|in this (video|episode)|"
    r"before we (begin|start|dive in))",
    re.IGNORECASE,
)

# A hook thinner than this cannot carry a concrete claim in 3 seconds.
HOOK_MIN_WORDS = 10


class VerificationAgent:
    """Rigorous audit agent with dual deterministic and semantic verification stages."""

    def __init__(self):
        self.model = get_config("active_models.verify") or "bedrock/claude-3-5-haiku"

    def run(self, bundle: ResearchBundle, script: VideoScript) -> VerificationReport:
        """Run full 10-step audit pipeline and return VerificationReport."""
        t_start = time.perf_counter()
        logger.info(f"Initiating verification audit on script: '{script.seo.title}'")

        checks: List[CheckResult] = []
        suggested_fixes: List[SuggestedFix] = []

        # =====================================================================
        # STAGE 1: 8 DETERMINISTIC CHECKS (< 1 sec)
        # =====================================================================
        c_accuracy, fix_acc = self._check_factual_accuracy(bundle, script)
        checks.append(c_accuracy)
        suggested_fixes.extend(fix_acc)

        c_citations, fix_cit = self._check_citation_validity(bundle, script)
        checks.append(c_citations)
        suggested_fixes.extend(fix_cit)

        c_disclaimer, fix_disc = self._check_disclaimer_presence(script)
        checks.append(c_disclaimer)
        suggested_fixes.extend(fix_disc)

        c_advice_regex, fix_adv = self._check_investment_advice_regex(script)
        checks.append(c_advice_regex)
        suggested_fixes.extend(fix_adv)

        c_contra, fix_contra = self._check_contradictions(script)
        checks.append(c_contra)
        suggested_fixes.extend(fix_contra)

        c_sections, fix_sec = self._check_section_completeness(bundle, script)
        checks.append(c_sections)
        suggested_fixes.extend(fix_sec)

        c_seo, fix_seo = self._check_seo_sanity(script)
        checks.append(c_seo)
        suggested_fixes.extend(fix_seo)

        c_hook, fix_hook = self._check_hook_strength(script)
        checks.append(c_hook)
        suggested_fixes.extend(fix_hook)

        t_det_ms = int((time.perf_counter() - t_start) * 1000)
        logger.info(f"Deterministic checks completed in {t_det_ms}ms")

        # Evaluate deterministic results
        det_critical = sum(1 for c in checks for i in c.issues if i.severity == "critical")
        det_major = sum(1 for c in checks for i in c.issues if i.severity == "major")

        t_llm_start = time.perf_counter()
        t_llm_ms = 0

        # =====================================================================
        # STAGE 2: 2 LLM SEMANTIC CHECKS (Only if deterministic passes)
        # =====================================================================
        if det_critical == 0 and det_major == 0:
            logger.info("Deterministic checks passed. Proceeding to LLM semantic checks.")
            c_semantic, fix_sem = self._check_investment_advice_semantic(script)
            checks.append(c_semantic)
            suggested_fixes.extend(fix_sem)

            c_quality, fix_qual = self._check_narration_quality(script)
            checks.append(c_quality)
            suggested_fixes.extend(fix_qual)

            t_llm_ms = int((time.perf_counter() - t_llm_start) * 1000)
        else:
            logger.warning(
                f"Skipping LLM semantic checks due to deterministic failures "
                f"({det_critical} critical, {det_major} major issues)."
            )

        # Final Overall Decision: "pass" ONLY if zero critical AND zero major issues
        total_critical = sum(1 for c in checks for i in c.issues if i.severity == "critical")
        total_major = sum(1 for c in checks for i in c.issues if i.severity == "major")
        overall = "pass" if (total_critical == 0 and total_major == 0) else "fail"
        confidence = 0.99 if overall == "pass" else max(0.5, 1.0 - (total_critical * 0.3 + total_major * 0.1))

        report = VerificationReport(
            overall=overall,
            confidence=confidence,
            checks=checks,
            suggested_fixes=suggested_fixes,
            model_used=self.model,
            deterministic_time_ms=t_det_ms,
            llm_time_ms=t_llm_ms,
        )

        logger.info(
            f"Verification completed. Outcome: {report.overall.upper()} "
            f"(Critical: {total_critical}, Major: {total_major})"
        )
        return report

    # -------------------------------------------------------------------------
    # Deterministic Check 1: Factual Accuracy
    # -------------------------------------------------------------------------
    def _check_factual_accuracy(
        self, bundle: ResearchBundle, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Verify that specific numeric claims in narration are grounded.

        P1 loophole fix (26 Nov 2026): previously a number was only
        flagged when its scene had NO citation at all — a scene citing
        ANY source passed even if the cited source did not back that
        number, and matching was a bare digit substring ("1" in
        "$1.5B" matched almost any text). Now EVERY number in every
        scene's narration must be grounded in EITHER:
        (a) the claim + snippet text of a source THIS scene cites, OR
        (b) the bundle financials / market-data corpus (verified
        numeric facts, same corpus the old check used).
        A number in neither is a critical issue, cited or not. Word-form
        numbers ("sixty-eight percent") are checked too. Years and bare
        unit-less numbers <= 10 are not quantitative claims (see
        _extract_numbers) and are skipped.
        """
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        # Cited-source text, per claim_id.
        source_text = {
            s.claim_id: f"{s.claim} {s.snippet}" for s in bundle.sources
        }
        # Verified numeric corpus: financials + market data.
        financial_numbers = _extract_numbers(
            json.dumps(bundle.financials.model_dump())
            + " "
            + json.dumps(bundle.market_data.model_dump())
        )

        for scene in script.scenes:
            cited_numbers: Dict[Tuple[float, str], str] = {}
            for cit in scene.citations:
                if cit in source_text:
                    cited_numbers.update(_extract_numbers(source_text[cit]))
            for (value, kind), raw in _extract_numbers(scene.narration).items():
                if _is_grounded(value, kind, cited_numbers) or _is_grounded(
                    value, kind, financial_numbers
                ):
                    continue
                issues.append(
                    Issue(
                        scene_id=scene.id,
                        claim=f"Unverified numerical figure '{raw}' in scene {scene.id}",
                        issue=(
                            f"Number '{raw}' is backed by none of this "
                            "scene's cited sources "
                            f"({sorted(scene.citations) or 'no citations'}) "
                            "nor by the ResearchBundle financials/market "
                            "data. A citation to an unrelated source does "
                            "not ground a number."
                        ),
                        severity="critical",
                        suggested_fix=(
                            f"Remove figure '{raw}' or replace it with a "
                            "figure stated in one of the cited sources."
                        ),
                    )
                )
                fixes.append(
                    SuggestedFix(
                        scene_id=scene.id,
                        field="narration",
                        old=raw,
                        new="",
                        reason=(
                            "Numeric claim lacked grounding in the cited "
                            "sources or bundle financials; remove it or "
                            "re-source it from a cited claim."
                        ),
                    )
                )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="FACTUAL_ACCURACY", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 2: Citation Validity
    # -------------------------------------------------------------------------
    def _check_citation_validity(
        self, bundle: ResearchBundle, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Verify that every citation claim_id exists in bundle.sources."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []
        valid_ids: Set[str] = {s.claim_id for s in bundle.sources}

        for scene in script.scenes:
            for cit in scene.citations:
                if cit not in valid_ids:
                    issues.append(
                        Issue(
                            scene_id=scene.id,
                            claim=f"Citation '{cit}' in scene {scene.id}",
                            issue=f"Orphan citation ID '{cit}' not found in ResearchBundle sources list.",
                            severity="critical",
                            suggested_fix=f"Link to an existing source ID from: {sorted(list(valid_ids))[:4]}",
                        )
                    )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="CITATION_VALIDITY", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 3: Disclaimer Presence
    # -------------------------------------------------------------------------
    def _check_disclaimer_presence(
        self, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Check for spoken disclaimer in narration and prominent written disclaimer in SEO description."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        # 1. Spoken disclaimer in narration
        all_narration = " ".join(s.narration for s in script.scenes)
        if not DISCLAIMER_REGEX.search(all_narration):
            issues.append(
                Issue(
                    scene_id=None,
                    claim="Spoken compliance disclaimer",
                    issue="No spoken regulatory or educational disclaimer found across scene narrations.",
                    severity="critical",
                    suggested_fix="Insert explicit spoken disclaimer scene before the call to action.",
                )
            )

        # 2. Written disclaimer in description top (first 300 chars)
        top_desc = script.seo.description[:350]
        if not DISCLAIMER_REGEX.search(top_desc):
            issues.append(
                Issue(
                    scene_id=None,
                    claim="Written disclaimer in SEO description header",
                    issue="SEO description does not lead with a clear regulatory compliance disclaimer.",
                    severity="major",
                    suggested_fix="Prepend 'DISCLAIMER: For educational and engineering analysis only.' to description.",
                )
            )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="DISCLAIMER_PRESENCE", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 4: Investment Advice Regex
    # -------------------------------------------------------------------------
    def _check_investment_advice_regex(
        self, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Search narration for prohibited investment advisory keywords."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        for scene in script.scenes:
            # Skip dedicated disclaimer scene
            if scene.section == "disclaimer":
                continue

            matches = FORBIDDEN_ADVICE_REGEX.findall(scene.narration)
            for m in matches:
                issues.append(
                    Issue(
                        scene_id=scene.id,
                        claim=f"Forbidden phrase '{m}' in scene {scene.id}",
                        issue=f"Script uses prohibited investment advisory language: '{m}'.",
                        severity="critical",
                        suggested_fix=f"Remove '{m}' and rephrase with neutral analytical language.",
                    )
                )
                fixes.append(
                    SuggestedFix(
                        scene_id=scene.id,
                        field="narration",
                        old=m,
                        new="analyze",
                        reason="Forbidden investment advice keyword prohibited by policy",
                    )
                )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="INVESTMENT_ADVICE_REGEX", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 5: Contradictions
    # -------------------------------------------------------------------------
    def _check_contradictions(
        self, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Check for conflicting numeric assertions between different scenes."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        # Track reported revenues across scenes
        revenue_mentions: Dict[str, List[int]] = {}
        rev_pattern = re.compile(r"(\$\d+(?:\.\d+)?\s*(?:billion|million))", re.IGNORECASE)

        for scene in script.scenes:
            matches = rev_pattern.findall(scene.narration)
            for m in matches:
                norm = m.lower().strip()
                if norm not in revenue_mentions:
                    revenue_mentions[norm] = []
                revenue_mentions[norm].append(scene.id)

        # If a single scene asserts contradictory figures for same metric
        for scene in script.scenes:
            if "revenue" in scene.narration.lower():
                figures = rev_pattern.findall(scene.narration)
                if len(figures) >= 2 and figures[0] != figures[1] and "from" not in scene.narration.lower():
                    issues.append(
                        Issue(
                            scene_id=scene.id,
                            claim="Contradictory revenue statements",
                            issue=f"Scene {scene.id} states multiple conflicting figures ({figures}) without growth context.",
                            severity="major",
                            suggested_fix="Align all revenue references to the verified annual filing figure.",
                        )
                    )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="CONTRADICTIONS", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 6: Section Completeness
    # -------------------------------------------------------------------------
    def _check_section_completeness(
        self, bundle: ResearchBundle, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Verify that all required sections per pillar are present."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []
        sections_present = {s.section for s in script.scenes}

        required = {"hook", "disclaimer", "cta"}
        if bundle.pillar == "finance_to_technical":
            required.update({"the_problem", "technical_solution", "financial_benefit", "scope_and_moat", "risks"})
        elif bundle.pillar == "ai_everything":
            # 26 Nov 2026: mirrors prompts/script_ai_everything.txt.
            required.update({"what_changed", "how_it_works", "why_it_matters", "risks_and_scams", "predictions"})
        else:
            required.update({"what_it_does", "revenue_breakdown", "unit_economics", "scope_and_trajectory", "risks"})

        missing = required - sections_present
        for m in missing:
            issues.append(
                Issue(
                    scene_id=None,
                    claim=f"Required section '{m}'",
                    issue=f"Pillar '{bundle.pillar}' requires section '{m}', which is missing from scenes.",
                    severity="major",
                    suggested_fix=f"Add at least one scene with section='{m}'.",
                )
            )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="SECTION_COMPLETENESS", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 7: SEO Sanity
    # -------------------------------------------------------------------------
    def _check_seo_sanity(self, script: VideoScript) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Verify title, tag length, description word count."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        # Title length
        if len(script.seo.title) > 100:
            issues.append(
                Issue(
                    scene_id=None,
                    claim="SEO Title Length",
                    issue=f"Title length ({len(script.seo.title)}) exceeds YouTube maximum limit of 100 characters.",
                    severity="major",
                    suggested_fix="Shorten title to <= 100 characters.",
                )
            )

        # Tags count (15 - 25)
        if not (15 <= len(script.seo.tags) <= 25):
            issues.append(
                Issue(
                    scene_id=None,
                    claim="SEO Tags Count",
                    issue=f"Tags count ({len(script.seo.tags)}) must be between 15 and 25.",
                    severity="minor",
                    suggested_fix="Adjust tag count to be within 15-25 tags.",
                )
            )

        # Description 300+ words
        desc_words = len(script.seo.description.split())
        if desc_words < 300:
            issues.append(
                Issue(
                    scene_id=None,
                    claim="SEO Description Depth",
                    issue=f"Description word count ({desc_words}) is below 300-word algorithmic threshold.",
                    severity="major",
                    suggested_fix="Expand description with detailed chapters, methodology, and timestamps.",
                )
            )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="SEO_SANITY", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # Deterministic Check 8: Hook Strength (Phase 2, 26 Nov 2026)
    # -------------------------------------------------------------------------
    def _check_hook_strength(
        self, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Flag a weak/generic opening as a major issue.

        The first 3 seconds (1 second for a Short) must open with a
        concrete, surprising, bundle-grounded claim — the one new thing
        and why it matters right now. Three deterministic failures:
        (a) the hook opens with a generic intro phrase ("In this video
        we will…", "Welcome to…", "Today we're going to…");
        (b) the hook narration is too thin (< 10 words) to carry a
        concrete claim; (c) the hook scene is not the first scene, so
        the viewer does not actually hear the hook first.
        A missing hook section is already a SECTION_COMPLETENESS major
        and is not double-flagged here. This check cannot judge
        "surprising" — only the absence of a generic/thin opening —
        so a pass here means "not generically weak", not "great hook".
        """
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        if not script.scenes:
            issues.append(
                Issue(
                    scene_id=None,
                    claim="Hook scene",
                    issue="Script has no scenes, so there is no opening hook.",
                    severity="major",
                    suggested_fix="Add a 'hook' scene as the first scene.",
                )
            )
            return CheckResult(name="HOOK_STRENGTH", status="fail", issues=issues), fixes

        hook = next((s for s in script.scenes if s.section == "hook"), None)
        if hook is None:
            # SECTION_COMPLETENESS already flags the missing hook.
            return CheckResult(name="HOOK_STRENGTH", status="pass", issues=issues), fixes

        opening = hook.narration.strip()
        opener = HOOK_GENERIC_OPENERS.search(opening)
        if opener:
            issues.append(
                Issue(
                    scene_id=hook.id,
                    claim="Generic hook opening",
                    issue=(
                        f"Hook opens with a generic intro ('{opener.group(0).strip()}…') "
                        "instead of a concrete, surprising, bundle-grounded claim "
                        "in the first 3 seconds (1 second for Shorts)."
                    ),
                    severity="major",
                    suggested_fix=(
                        "Rewrite the hook to open with the single most surprising "
                        "verified fact from the research bundle — name the tool, "
                        "model, leak or event and the concrete consequence — "
                        "with no 'In this video…' throat-clearing."
                    ),
                )
            )
            fixes.append(
                SuggestedFix(
                    scene_id=hook.id,
                    field="narration",
                    old=opener.group(0).strip(),
                    new="(concrete bundle-grounded hook)",
                    reason="Generic intro wastes the opening seconds; hooks must lead with a concrete claim",
                )
            )

        word_count = len(opening.split())
        if word_count < HOOK_MIN_WORDS:
            issues.append(
                Issue(
                    scene_id=hook.id,
                    claim="Hook too thin",
                    issue=(
                        f"Hook narration is only {word_count} words "
                        f"(minimum {HOOK_MIN_WORDS}); too thin to carry a "
                        "concrete, bundle-grounded claim in the opening seconds."
                    ),
                    severity="major",
                    suggested_fix="Expand the hook to a full sentence naming the new thing and its concrete consequence.",
                )
            )

        if script.scenes[0].id != hook.id:
            issues.append(
                Issue(
                    scene_id=hook.id,
                    claim="Hook is not the first scene",
                    issue=(
                        f"Hook scene (id {hook.id}) is not the first scene "
                        f"(id {script.scenes[0].id}); the viewer hears "
                        "something else before the hook."
                    ),
                    severity="major",
                    suggested_fix="Move the 'hook' scene to the first position in the script.",
                )
            )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="HOOK_STRENGTH", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # LLM Semantic Check 9: Investment Advice Semantic
    # -------------------------------------------------------------------------
    def _check_investment_advice_semantic(
        self, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Audit for subtle investment advice using LLM.

        P0 #7 fix (2026-11-25): this was a stub that always returned pass.
        It now calls the LLM with prompts/verify_investment_advice.txt and
        FAILS CLOSED: if the prompt file cannot be read or the LLM call
        fails, the check returns fail (critical), never pass — an unrunnable
        semantic check must not clear a script as advice-free.
        """
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        try:
            system_prompt = PROMPT_ADVICE_PATH.read_text(encoding="utf-8")
        except OSError as e:
            logger.error(f"Investment-advice prompt file unreadable: {e}")
            issues.append(
                Issue(
                    scene_id=None,
                    claim="INVESTMENT_ADVICE_SEMANTIC prompt",
                    issue=f"Prompt file {PROMPT_ADVICE_PATH} could not be read ({e}); semantic check cannot run.",
                    severity="critical",
                    suggested_fix="Restore prompts/verify_investment_advice.txt and re-run verification.",
                )
            )
            return CheckResult(name="INVESTMENT_ADVICE_SEMANTIC", status="fail", issues=issues), fixes

        narration = "\n".join(
            f"[scene {s.id} | {s.section}] {s.narration}" for s in script.scenes
        )
        user_prompt = (
            "Audit the following full narration script for subtle, implicit, "
            "or semantic investment advice.\n\n"
            f"{narration}\n\n"
            "Output ONLY raw JSON conforming to the requested schema "
            "(no markdown, no commentary)."
        )

        try:
            chain = get_config("verify_chain")
        except Exception:
            chain = [["gemini", "gemini-3.8-flash"]]

        try:
            verdict = call_with_fallback(
                chain, system_prompt, user_prompt, schema=_AdviceVerdict
            )
        except Exception as e:
            # Fail CLOSED: never report pass when the check did not actually run.
            logger.error(f"LLM semantic investment-advice check failed: {e}")
            issues.append(
                Issue(
                    scene_id=None,
                    claim="INVESTMENT_ADVICE_SEMANTIC unavailable",
                    issue=f"LLM semantic check could not run ({e}); script cannot be cleared as free of investment advice.",
                    severity="critical",
                    suggested_fix="Fix LLM provider configuration/credentials and re-run verification; do not publish until this check passes.",
                )
            )
            return CheckResult(name="INVESTMENT_ADVICE_SEMANTIC", status="fail", issues=issues), fixes

        logger.info(
            f"Semantic investment-advice verdict: has_advice={verdict.has_investment_advice} "
            f"(confidence={verdict.confidence:.2f}, flagged={len(verdict.flagged_phrases)})"
        )
        if verdict.has_investment_advice:
            for flag in verdict.flagged_phrases:
                severity = (
                    flag.severity.lower()
                    if flag.severity.lower() in ("critical", "major")
                    else "major"
                )
                issues.append(
                    Issue(
                        scene_id=flag.scene_id,
                        claim=f"Semantic investment advice in scene {flag.scene_id}",
                        issue=f"{flag.reason} Flagged text: '{flag.text}'",
                        severity=severity,
                        suggested_fix="Remove the advisory/FOMO phrasing and rephrase with neutral analytical language.",
                    )
                )
                if flag.scene_id is not None:
                    fixes.append(
                        SuggestedFix(
                            scene_id=flag.scene_id,
                            field="narration",
                            old=flag.text,
                            new="analyze",
                            reason="LLM semantic check flagged investment-advice language",
                        )
                    )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="INVESTMENT_ADVICE_SEMANTIC", status=status, issues=issues), fixes

    # -------------------------------------------------------------------------
    # LLM Semantic Check 10: Narration Quality
    # -------------------------------------------------------------------------
    def _check_narration_quality(
        self, script: VideoScript
    ) -> Tuple[CheckResult, List[SuggestedFix]]:
        """Audit for TTS hostility and text flow."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        tts_hostile_pattern = re.compile(r"(\[.*?\]|\(.*?\)|[\*\_#`~])")
        for scene in script.scenes:
            matches = tts_hostile_pattern.findall(scene.narration)
            if matches:
                issues.append(
                    Issue(
                        scene_id=scene.id,
                        claim="TTS Clean Text",
                        issue=f"Scene {scene.id} contains markdown or bracketed characters: {matches}",
                        severity="minor",
                        suggested_fix="Strip all brackets and markdown formatting from narration.",
                    )
                )

        status = "fail" if any(i.severity in ("critical", "major") for i in issues) else "pass"
        return CheckResult(name="NARRATION_QUALITY", status=status, issues=issues), fixes


def run_verification(bundle: ResearchBundle, script: VideoScript) -> VerificationReport:
    """Convenience entrypoint for Verification Agent."""
    return VerificationAgent().run(bundle, script)
