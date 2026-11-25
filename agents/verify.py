# LOCAL PRODUCTION PATCH — 2026-11-25 (applied via PR #1, branch fix/production-mocks)..
# Fixes P0 #7 (audit list, 25 Nov 2026): _check_investment_advice_semantic()
# was a stub that always returned pass with no LLM call. It now performs a
# real LLM semantic check via tools.llm_provider.call_with_fallback() using
# prompts/verify_investment_advice.txt, and FAILS CLOSED (returns fail, never
# pass) when the LLM or the prompt file is unavailable. P1 verify loophole
# (citation present => number passes unchecked) is left as a TODO, not rewritten.
"""Agent 3: Verification Agent for TubePilot AI.

Audits `VideoScript` against `ResearchBundle` to catch hallucinations and compliance issues.

CRITICAL DESIGN:
Runs 7 DETERMINISTIC checks FIRST (<1 sec, 0 LLM tokens).
Only if all deterministic checks pass -> invokes LLM for 2 semantic checks.
This saves ~90% LLM tokens while catching almost all issues deterministically.

Deterministic Checks:
1. FACTUAL_ACCURACY — numbers in narration must be traceable to bundle
2. CITATION_VALIDITY — every scene citation must exist in bundle.sources
3. DISCLAIMER_PRESENCE — spoken in narration + written in description top
4. INVESTMENT_ADVICE_REGEX — regex search for forbidden advisory terms
5. CONTRADICTIONS — conflicting numbers across scenes
6. SECTION_COMPLETENESS — required sections present per pillar
7. SEO_SANITY — title <= 100, tags 15-25, description 300+ words

LLM Semantic Checks:
8. INVESTMENT_ADVICE_SEMANTIC — subtle advice / FOMO detection
9. NARRATION_QUALITY — TTS hostility, markdown artifacts, emojis
"""

from __future__ import annotations

import json
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


class VerificationAgent:
    """Rigorous audit agent with dual deterministic and semantic verification stages."""

    def __init__(self):
        self.model = get_config("active_models.verify") or "bedrock/claude-3-5-haiku"

    def run(self, bundle: ResearchBundle, script: VideoScript) -> VerificationReport:
        """Run full 9-step audit pipeline and return VerificationReport."""
        t_start = time.perf_counter()
        logger.info(f"Initiating verification audit on script: '{script.seo.title}'")

        checks: List[CheckResult] = []
        suggested_fixes: List[SuggestedFix] = []

        # =====================================================================
        # STAGE 1: 7 DETERMINISTIC CHECKS (< 1 sec)
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
        """Verify that specific numeric claims in narration can be linked to verified bundle data."""
        issues: List[Issue] = []
        fixes: List[SuggestedFix] = []

        # Build corpus of verified numbers
        verified_text = json.dumps(bundle.financials.model_dump())
        for s in bundle.sources:
            verified_text += " " + s.claim + " " + s.snippet

        # Extract digits patterns like "$1.5B", "68%", "25%"
        number_pattern = re.compile(r"(\$?\d+(?:\.\d+)?\s*(?:billion|million|trillion|percent|%)?)", re.IGNORECASE)

        # TODO (P1 verify loophole, audit list 25 Nov 2026): a number is only
        # flagged when its scene has NO citation at all. A scene that cites
        # any source passes even if the cited source does not actually back
        # that number, and word-form numbers ("sixty-eight percent") are never
        # checked. Fixing this needs per-claim numeric grounding against the
        # cited source text — deliberately NOT rewritten in this P0 patch.
        for scene in script.scenes:
            # Check numbers in narration
            matches = number_pattern.findall(scene.narration)
            for m in matches:
                # Digits extraction
                digits = re.findall(r"\d+", m)
                if not digits:
                    continue
                d = digits[0]
                # If digits aren't found in bundle text at all, flag as unverified
                if d not in verified_text:
                    # Check if scene cites any source
                    if not scene.citations:
                        issues.append(
                            Issue(
                                scene_id=scene.id,
                                claim=f"Unverified numerical figure '{m}' in scene {scene.id}",
                                issue=f"Number '{m}' is not present in ResearchBundle financials or source citations.",
                                severity="critical",
                                suggested_fix=f"Replace or source figure '{m}' using verified data from bundle.",
                            )
                        )
                        fixes.append(
                            SuggestedFix(
                                scene_id=scene.id,
                                field="narration",
                                old=m,
                                new="approximately verified metric",
                                reason="Numeric claim lacked verified provenance in ResearchBundle",
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
    # LLM Semantic Check 8: Investment Advice Semantic
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
    # LLM Semantic Check 9: Narration Quality
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
