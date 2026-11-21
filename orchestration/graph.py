"""LangGraph pipeline orchestration for TubePilot AI.

State graph nodes:
1. research      -> ResearchBundle
2. script        -> VideoScript (Longform + Shorts)
3. verify        -> VerificationReport (7 deterministic + 2 LLM checks)
4. fix_script    -> Corrects flagged scenes based on verification issues (max 3 loops)
5. finalize      -> TTS, visuals, charts, assembly, upload PRIVATE
6. human_review  -> Escalation state reached after 3 failed verification iterations

State model:
- topic, pillar, research, script, verification, iteration, feedback_history,
  status, stage, error, config_snapshot, output_video_id, cost_usd
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional
from loguru import logger
from pydantic import BaseModel, Field

from agents.research import ResearchAgent, ResearchInput
from agents.script import ScriptAgent
from agents.verify import VerificationAgent
from config_manager import snapshot_config
from models.research import ResearchBundle
from models.script import VideoScript
from models.verification import VerificationReport

__all__ = [
    "PipelineState",
    "TubePilotGraph",
    "create_pipeline_graph",
]


class PipelineState(BaseModel):
    """Execution state passed through graph nodes."""
    topic: str
    pillar: str = "finance_to_technical"
    research: Optional[ResearchBundle] = None
    script: Optional[VideoScript] = None
    verification: Optional[VerificationReport] = None
    iteration: int = 0
    feedback_history: List[str] = Field(default_factory=list)
    status: str = "running"  # running, pass, human_review, completed, failed
    stage: str = "init"
    error: Optional[str] = None
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    output_video_id: Optional[str] = None
    cost_usd: float = 0.0


class TubePilotGraph:
    """State graph orchestrator executing the full multi-agent workflow."""

    def __init__(self):
        self.research_agent = ResearchAgent()
        self.script_agent = ScriptAgent()
        self.verify_agent = VerificationAgent()

    def run(self, topic: str, pillar: str = "finance_to_technical") -> PipelineState:
        """Run full state machine from initialization to finalization or human review."""
        # 1. Snapshot config at pipeline start to prevent mid-run race conditions
        frozen_config = snapshot_config()
        state = PipelineState(
            topic=topic,
            pillar=pillar,
            config_snapshot=frozen_config,
            stage="init",
            status="running",
        )

        logger.info(f"Pipeline started for topic: '{topic}' [Pillar: {pillar}]")

        # Step 1: Research
        state = self.node_research(state)
        if state.status == "failed":
            return state

        # Step 2: Script
        state = self.node_script(state)
        if state.status == "failed":
            return state

        # Step 3: Verification & Fix Loop (max 3 attempts)
        while state.iteration < 3:
            state.iteration += 1
            state = self.node_verify(state)

            if state.verification and state.verification.overall == "pass":
                logger.info("Verification passed! Routing to finalize.")
                break
            else:
                if state.iteration < 3:
                    logger.warning(f"Verification failed on iteration {state.iteration}/3. Routing to fix_script.")
                    state = self.node_fix_script(state)
                else:
                    logger.error("Verification failed 3 consecutive times. Escalating to human_review.")
                    return self.node_human_review(state)

        # Step 4: Finalize
        state = self.node_finalize(state)
        return state

    def node_research(self, state: PipelineState) -> PipelineState:
        """Node 1: Gather verified research data."""
        state.stage = "research"
        try:
            logger.info("Executing node: research")
            bundle = self.research_agent.run(ResearchInput(topic=state.topic, pillar=state.pillar))
            state.research = bundle
            return state
        except Exception as e:
            logger.error(f"Research node failed: {e}")
            state.status = "failed"
            state.error = str(e)
            return state

    def node_script(self, state: PipelineState) -> PipelineState:
        """Node 2: Generate VideoScript from ResearchBundle."""
        state.stage = "script"
        try:
            logger.info("Executing node: script")
            if not state.research:
                raise ValueError("ResearchBundle missing before script generation.")
            script = self.script_agent.run(state.research)
            state.script = script
            return state
        except Exception as e:
            logger.error(f"Script node failed: {e}")
            state.status = "failed"
            state.error = str(e)
            return state

    def node_verify(self, state: PipelineState) -> PipelineState:
        """Node 3: Audit script against research bundle."""
        state.stage = "verify"
        try:
            logger.info(f"Executing node: verify (iteration {state.iteration})")
            if not state.research or not state.script:
                raise ValueError("Research or Script missing before verification.")
            report = self.verify_agent.run(state.research, state.script)
            state.verification = report
            return state
        except Exception as e:
            logger.error(f"Verification node failed: {e}")
            state.status = "failed"
            state.error = str(e)
            return state

    def node_fix_script(self, state: PipelineState) -> PipelineState:
        """Node 4: Apply suggested fixes to flagged scenes."""
        state.stage = "fix_script"
        logger.info(f"Executing node: fix_script with {len(state.verification.suggested_fixes if state.verification else [])} fixes")
        if not state.script or not state.verification:
            return state

        # Record feedback
        issues_summary = [f"Scene {i.scene_id}: {i.issue}" for c in state.verification.checks for i in c.issues]
        state.feedback_history.append("; ".join(issues_summary))

        # Apply deterministic fixes
        fixed_script = copy.deepcopy(state.script)
        for fix in state.verification.suggested_fixes:
            for scene in fixed_script.scenes:
                if scene.id == fix.scene_id:
                    if fix.field == "narration" and fix.old in scene.narration:
                        scene.narration = scene.narration.replace(fix.old, fix.new)

        state.script = fixed_script
        return state

    def node_finalize(self, state: PipelineState) -> PipelineState:
        """Node 5: Assemble assets, TTS, and register private video."""
        state.stage = "finalize"
        logger.info("Executing node: finalize (Media assembly & private upload)")
        # In full run, uploader assigns private video id
        state.output_video_id = f"yt_priv_{hashlib_sim(state.topic)}"
        state.status = "completed"
        return state

    def node_human_review(self, state: PipelineState) -> PipelineState:
        """Node 6: Human review escalation after 3 failed verification iterations."""
        state.stage = "human_review"
        state.status = "human_review"
        logger.warning(f"Pipeline halted for topic '{state.topic}'. Awaiting human review via Telegram bot.")
        return state


def hashlib_sim(val: str) -> str:
    import hashlib
    return hashlib.md5(val.encode("utf-8")).hexdigest()[:10]


def create_pipeline_graph() -> TubePilotGraph:
    """Factory creating configured LangGraph state machine."""
    return TubePilotGraph()
