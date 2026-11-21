"""Verification report data models for TubePilot AI.

Defines the ``VerificationReport`` and supporting sub-models that capture
the outcome of deterministic and LLM-based fact-checking passes over a
generated video script.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "Issue",
    "CheckResult",
    "SuggestedFix",
    "VerificationReport",
]


class Issue(BaseModel):
    """A single issue discovered during a verification check."""

    scene_id: Optional[int] = Field(
        default=None,
        description="Scene ID the issue relates to, or None for script-level issues.",
    )
    claim: str = Field(..., description="The claim that was checked.")
    issue: str = Field(..., description="Description of the problem found.")
    severity: Literal["critical", "major", "minor"] = Field(
        ...,
        description="Issue severity level.",
    )
    suggested_fix: str = Field(
        ...,
        description="Recommended remediation for this issue.",
    )


class CheckResult(BaseModel):
    """Result of a single named verification check."""

    name: str = Field(..., description="Name of the verification check.")
    status: Literal["pass", "fail"] = Field(
        ...,
        description="Whether the check passed or failed.",
    )
    issues: list[Issue] = Field(
        default_factory=list,
        description="Issues found during this check (empty when status is 'pass').",
    )


class SuggestedFix(BaseModel):
    """A concrete edit suggestion to resolve a verification issue."""

    scene_id: int = Field(..., description="ID of the scene to modify.")
    field: str = Field(..., description="Field name to update (e.g. 'narration').")
    old: str = Field(..., description="Current value of the field.")
    new: str = Field(..., description="Proposed replacement value.")
    reason: str = Field(..., description="Why this fix is recommended.")


class VerificationReport(BaseModel):
    """Aggregate verification report for a video script.

    Collects the results of all deterministic and LLM-based verification
    checks, along with suggested fixes and timing telemetry.  The
    ``overall`` status is validated to be ``"pass"`` only when no critical
    or major issues exist across all checks.
    """

    overall: Literal["pass", "fail"] = Field(
        ...,
        description="Aggregate pass/fail status.",
    )
    confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="Overall confidence in the verification result (0-1).",
    )
    checks: list[CheckResult] = Field(
        default_factory=list,
        description="Individual check results.",
    )
    suggested_fixes: list[SuggestedFix] = Field(
        default_factory=list,
        description="Actionable fix suggestions.",
    )
    model_used: str = Field(
        ...,
        description="Identifier of the LLM model used for verification.",
    )
    deterministic_time_ms: int = Field(
        ...,
        description="Wall-clock time for deterministic checks in milliseconds.",
    )
    llm_time_ms: int = Field(
        ...,
        description="Wall-clock time for LLM-based checks in milliseconds.",
    )

    @model_validator(mode="after")
    def _overall_consistent_with_issues(self) -> "VerificationReport":
        """Ensure ``overall='pass'`` only when zero critical and zero major issues exist."""
        critical_count = 0
        major_count = 0
        for check in self.checks:
            for iss in check.issues:
                if iss.severity == "critical":
                    critical_count += 1
                elif iss.severity == "major":
                    major_count += 1

        if self.overall == "pass" and (critical_count > 0 or major_count > 0):
            raise ValueError(
                f"overall='pass' is invalid: found {critical_count} critical "
                f"and {major_count} major issue(s). Set overall='fail'."
            )

        return self
