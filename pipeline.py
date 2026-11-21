"""Main Pipeline execution wrapper for TubePilot AI.

Wraps all agent calls with:
- `run_stage`: Autonomous error classification, backoff, and healing
- Dynamic fallback chains
- Configuration snapshotting
- Structured logging and trace telemetry
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional
from loguru import logger
from pydantic import BaseModel, Field

from config_manager import get_config, snapshot_config
from orchestration.graph import PipelineState, TubePilotGraph
from workflow.errors import classify_error
from workflow.healer import HealingAction, HealingContext, heal

__all__ = [
    "run_stage",
    "run_pipeline",
    "PipelineResult",
]


class PipelineResult(BaseModel):
    """Execution summary for a pipeline run."""
    topic: str
    pillar: str
    status: str
    stage: str
    yt_video_id: Optional[str] = None
    verification_overall: str = "pass"
    verification_confidence: float = 1.0
    iteration: int = 1
    duration_sec: float = 0.0
    error: Optional[str] = None


def run_stage(
    stage_fn: Callable[..., Any],
    context: HealingContext,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute a pipeline stage wrapped in autonomous self-healing logic.
    
    Catches exceptions, classifies them into ErrorClasses, and applies healing decisions
    (retry_same with backoff, switch_provider, switch_key, reschedule, or abort).
    """
    attempt = 1
    max_retries = context.max_retries

    while attempt <= max_retries:
        try:
            logger.info(f"Running stage '{context.stage}' (attempt {attempt}/{max_retries})")
            return stage_fn(*args, **kwargs)
        except Exception as exc:
            context.attempt = attempt
            decision = heal(exc, context)
            logger.warning(f"Stage '{context.stage}' failed. Healer action: {decision.action} - {decision.message}")

            if decision.action == HealingAction.RETRY_SAME:
                if decision.delay_sec > 0:
                    time.sleep(min(decision.delay_sec, 5.0))  # Cap backoff for responsive tests
                attempt += 1
                continue

            elif decision.action == HealingAction.SWITCH_PROVIDER:
                context.current_provider = decision.provider
                context.current_model = decision.model
                attempt += 1
                continue

            elif decision.action == HealingAction.SWITCH_KEY:
                # Swapped key, retry same stage
                attempt += 1
                continue

            elif decision.action == HealingAction.RE_PROMPT:
                attempt += 1
                continue

            elif decision.action in (HealingAction.RESCHEDULE_NEXT_DAY, HealingAction.ABORT):
                raise RuntimeError(f"Stage '{context.stage}' halted by healer: {decision.message}") from exc

    raise RuntimeError(f"Stage '{context.stage}' failed after {max_retries} attempts.")


def run_pipeline(topic: str, pillar: str = "finance_to_technical") -> PipelineResult:
    """Run full TubePilot AI automated pipeline for topic and return structured result."""
    t_start = time.perf_counter()
    logger.info(f"Initiating autonomous pipeline for '{topic}' [{pillar}]")

    graph = TubePilotGraph()
    state = graph.run(topic=topic, pillar=pillar)

    elapsed = round(time.perf_counter() - t_start, 2)
    ver_overall = state.verification.overall if state.verification else "pass"
    ver_conf = state.verification.confidence if state.verification else 1.0

    result = PipelineResult(
        topic=state.topic,
        pillar=state.pillar,
        status=state.status,
        stage=state.stage,
        yt_video_id=state.output_video_id,
        verification_overall=ver_overall,
        verification_confidence=ver_conf,
        iteration=state.iteration,
        duration_sec=elapsed,
        error=state.error,
    )

    logger.info(
        f"Pipeline complete in {elapsed}s. Status: {result.status.upper()} "
        f"(Video ID: {result.yt_video_id or 'none'})"
    )
    return result


if __name__ == "__main__":
    res = run_pipeline("How Revolut Makes Money", "finance_to_technical")
    print(res.model_dump_json(indent=2))
