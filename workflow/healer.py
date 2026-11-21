"""Automated self-healing engine for TubePilot AI.

Translates classified errors into concrete healing actions:
- retry_same (with exponential backoff)
- switch_provider (next in fallback chain)
- switch_key (rotate API key from pool)
- reschedule_next_day (for 24h quota exhaustion)
- re_prompt (schema correction)
- abort (unrecoverable fatal errors)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from loguru import logger
from pydantic import BaseModel, Field

from workflow.errors import ErrorClass, classify_error

__all__ = [
    "HealingAction",
    "HealingContext",
    "HealingDecision",
    "heal",
]


class HealingAction(str):
    RETRY_SAME = "retry_same"
    SWITCH_PROVIDER = "switch_provider"
    SWITCH_KEY = "switch_key"
    RESCHEDULE_NEXT_DAY = "reschedule_next_day"
    RE_PROMPT = "re_prompt"
    ABORT = "abort"


class HealingContext(BaseModel):
    """Contextual metadata passed to healer to inform autonomous recovery."""
    stage: str = Field(..., description="Pipeline stage where error occurred")
    attempt: int = Field(default=1, description="Current retry attempt number")
    max_retries: int = Field(default=3, description="Maximum allowable retries")
    current_provider: Optional[str] = Field(default=None)
    current_model: Optional[str] = Field(default=None)
    fallback_chain: List[List[str]] = Field(
        default_factory=list,
        description="Chain of [provider, model] pairs"
    )
    key_name: Optional[str] = Field(default=None)
    key_pool: List[str] = Field(default_factory=list)


class HealingDecision(BaseModel):
    """Actionable healing decision returned by heal()."""
    action: str = Field(..., description="HealingAction value")
    provider: Optional[str] = None
    model: Optional[str] = None
    key_value: Optional[str] = None
    delay_sec: float = 0.0
    message: str
    should_alert: bool = False


def heal(exc: Exception, context: HealingContext) -> HealingDecision:
    """Analyze error and context to compute automated healing decision."""
    err_class = classify_error(exc)
    logger.info(f"Healer evaluating error at stage '{context.stage}': {err_class.value} ({exc})")

    # 1. TRANSIENT -> retry_same with exponential backoff
    if err_class == ErrorClass.TRANSIENT:
        if context.attempt < context.max_retries:
            delay = 2.0 ** context.attempt
            return HealingDecision(
                action=HealingAction.RETRY_SAME,
                delay_sec=delay,
                message=f"Transient network/rate-limit error. Retrying attempt {context.attempt + 1} after {delay:.1f}s backoff.",
                should_alert=False,
            )
        else:
            # Exhausted retries -> try switching provider if fallback chain exists
            return _try_switch_provider(context, "Transient retries exhausted.")

    # 2. PROVIDER_DOWN -> switch_provider
    elif err_class == ErrorClass.PROVIDER_DOWN:
        return _try_switch_provider(context, f"Provider 5xx/outage detected: {exc}")

    # 3. INVALID_KEY -> switch_key from pool
    elif err_class == ErrorClass.INVALID_KEY:
        if context.key_pool:
            next_key = context.key_pool[0]
            return HealingDecision(
                action=HealingAction.SWITCH_KEY,
                key_value=next_key,
                message=f"API key authorization failure. Swapping to backup key from pool for {context.key_name}.",
                should_alert=True,
            )
        else:
            return HealingDecision(
                action=HealingAction.ABORT,
                message=f"API key unauthorized and key pool is empty for {context.key_name}. Aborting.",
                should_alert=True,
            )

    # 4. QUOTA_EXCEEDED -> reschedule_next_day
    elif err_class == ErrorClass.QUOTA_EXCEEDED:
        return HealingDecision(
            action=HealingAction.RESCHEDULE_NEXT_DAY,
            delay_sec=86400.0,
            message="Daily API quota exhausted. Rescheduling run for next UTC maintenance window (24h).",
            should_alert=True,
        )

    # 5. SCHEMA_FAIL -> re_prompt
    elif err_class == ErrorClass.SCHEMA_FAIL:
        if context.attempt <= context.max_retries:
            return HealingDecision(
                action=HealingAction.RE_PROMPT,
                message=f"JSON schema parsing validation error. Re-prompting model with validation error context (attempt {context.attempt + 1}).",
                should_alert=False,
            )
        else:
            return _try_switch_provider(context, "Max schema repair attempts exceeded.")

    # 6. CONTENT_FAIL / FATAL -> abort
    else:
        return HealingDecision(
            action=HealingAction.ABORT,
            message=f"Unrecoverable error encountered in stage '{context.stage}': {exc}. Halting pipeline and notifying admin.",
            should_alert=True,
        )


def _try_switch_provider(context: HealingContext, reason: str) -> HealingDecision:
    """Helper to advance to next provider in fallback chain."""
    if context.fallback_chain:
        # Find current index
        current = [context.current_provider, context.current_model]
        next_pair = None
        for i, pair in enumerate(context.fallback_chain):
            if pair == current and i + 1 < len(context.fallback_chain):
                next_pair = context.fallback_chain[i + 1]
                break

        if not next_pair and len(context.fallback_chain) > 0 and context.fallback_chain[0] != current:
            next_pair = context.fallback_chain[0]

        if next_pair:
            return HealingDecision(
                action=HealingAction.SWITCH_PROVIDER,
                provider=next_pair[0],
                model=next_pair[1],
                message=f"{reason} Switched provider to {next_pair[0]}/{next_pair[1]}.",
                should_alert=False,
            )

    return HealingDecision(
        action=HealingAction.ABORT,
        message=f"{reason} No fallback provider remaining in chain. Halting pipeline.",
        should_alert=True,
    )
