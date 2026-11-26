# LOCAL PRODUCTION PATCH — 2026-11-25
# Source: alphaframehq/yt-Agent main @ c39f42541b8e784dcf41c09583dc57227be07667 — orchestration/graph.py
# Fixes: P0 audit #16/#17 (task #18/#19): node_finalize calls the real uploader, uses the real returned video ID, completed only after confirmed private upload, assembly/upload failure -> failed with error and no video ID, hashlib_sim/yt_priv_ removed; P1: renders all 3 Shorts, visual_spend is the real tracked cost (0.0 if untracked) not 0.09; verify crash is returned as failed, not masked as human_review (Telegram notify TODO left in code).
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# No secrets in this file — env placeholders / env-var names only.

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
from db.sheets import save_trend_report
from models.research import ResearchBundle
from models.script import ShortsScript, VideoScript
from models.verification import VerificationReport
from tools.trends import TrendReport, fetch_trends

__all__ = [
    "PipelineState",
    "TubePilotGraph",
    "create_pipeline_graph",
]


class PipelineState(BaseModel):
    """Execution state passed through graph nodes."""
    topic: str
    pillar: str = "ai_everything"
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
    rendered_video_path: Optional[str] = None
    rendered_shorts_path: Optional[str] = None
    # All rendered Shorts paths (rendered_shorts_path keeps the first one
    # for backwards compatibility).
    rendered_shorts_paths: List[str] = Field(default_factory=list)
    # ShortsScripts matching rendered_shorts_paths, in the same order —
    # the daily runner needs them for the Shorts' SEO at upload time.
    shorts_scripts: List[ShortsScript] = Field(default_factory=list)
    # Trend research for the day's topic (tools/trends.py). None until
    # node_trends runs; an empty-angles report (not None) means the
    # step ran but no trends were found / feeds failed.
    trend_report: Optional[TrendReport] = None
    # Per-Short upload outcomes, filled by the daily runner
    # (agents/shorts.py): [{index, title, status, yt_video_id, error}].
    # A Short's failure is recorded here and never flips the long
    # video's status.
    short_uploads: List[Dict[str, Any]] = Field(default_factory=list)
    # not_run | partial | complete | failed | skipped
    shorts_status: str = "not_run"
    cost_usd: float = 0.0


class TubePilotGraph:
    """State graph orchestrator executing the full multi-agent workflow."""

    def __init__(self):
        self.research_agent = ResearchAgent()
        self.script_agent = ScriptAgent()
        self.verify_agent = VerificationAgent()

    def run(self, topic: str, pillar: str = "ai_everything") -> PipelineState:
        """Run full state machine from initialization to finalization or human review."""
        # 0. Topic safety / niche gate (26 Nov 2026). A blocked or
        # off-niche topic must never reach research, an LLM call, or
        # the YouTube API — return a failed state immediately, with a
        # clear error, so the runner can mark the topic failed in the
        # Sheet and move on to the next queued topic. Checked before
        # snapshot_config() so a bad topic costs no Sheets/LLM work.
        from tools.topic_filter import check_topic

        allowed, filter_reason = check_topic(topic, pillar)
        if not allowed:
            logger.error(f"Topic rejected by topic filter: '{topic}' ({filter_reason})")
            return PipelineState(
                topic=topic,
                pillar=pillar,
                stage="filter",
                status="failed",
                error=f"Topic rejected by topic filter: {filter_reason}",
            )

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

        # Step 1b: Trend research (daily-output design, 26 Nov 2026).
        # Runs BEFORE script generation so the long video and the 3
        # Shorts can lead with today's angles. Non-fatal by design:
        # node_trends never fails the run (see its docstring).
        state = self.node_trends(state)

        # Step 2: Script
        state = self.node_script(state)
        if state.status == "failed":
            return state

        # Step 3: Verification & Fix Loop (max 3 attempts)
        while state.iteration < 3:
            state.iteration += 1
            state = self.node_verify(state)

            if state.status == "failed":
                # P0: a verification crash is a failure, not a fixable
                # script problem and not a human-review escalation. Return
                # it as failed; do not loop into fix_script/human_review.
                # TODO: no Telegram human-review notify is sent here — the
                # Telegram notify path for a failed verification is unclear
                # (no queue/persist step exists in this file); wire one in
                # when that path is defined, do not silently re-label this.
                return state

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

    def node_trends(self, state: PipelineState) -> PipelineState:
        """Node 1b: Fetch current trends/angles for the topic + niche.

        Runs after research, before script generation. NON-FATAL by
        design: ``fetch_trends()`` never raises, and even a total
        failure here (no feeds, no Sheet) only leaves an empty-angles
        report on the state — the run continues with the research
        bundle alone. Trend angles are editorial hooks, never numeric
        facts, so an empty report can never fabricate a claim.

        The report is logged (sources + angles), attached to the
        research bundle (``bundle.trend_angles`` /
        ``bundle.trend_sources``) so the script agent can use it, and
        recorded in the ``yt_trends`` Sheet tab when the Sheet is
        reachable — a Sheets failure is logged and ignored.
        """
        state.stage = "trends"
        try:
            logger.info("Executing node: trends")
            report = fetch_trends(topic=state.topic, pillar=state.pillar)
        except Exception as e:  # noqa: BLE001 — belt & braces; fetch_trends never raises
            logger.warning(f"Trend research failed unexpectedly ({e}); continuing without trends.")
            report = TrendReport(topic=state.topic, pillar=state.pillar, errors=[str(e)])
        state.trend_report = report
        if state.research is not None:
            state.research.trend_angles = list(report.angles)
            state.research.trend_sources = list(report.sources)
        try:
            save_trend_report(state.topic, report, pillar=state.pillar)
        except Exception as e:  # noqa: BLE001 — Sheet unavailable: log, continue
            logger.warning(f"Could not save trend report to Google Sheets (continuing): {e}")
        return state

    def node_script(self, state: PipelineState) -> PipelineState:
        """Node 2: Generate VideoScript from ResearchBundle."""
        state.stage = "script"
        try:
            logger.info("Executing node: script")
            if not state.research:
                raise ValueError("ResearchBundle missing before script generation.")
            script = self.script_agent.run(state.research, trend_report=state.trend_report)
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
        """Node 5: Assemble assets, TTS, and register private video.

        P0: the real uploader (agents/uploader.py) is called and the real
        YouTube video ID it returns is used. ``completed`` is set only
        after that private upload is confirmed. Any assembly or upload
        failure sets status ``failed`` with the error and no video ID.
        No fake ``yt_priv_<md5>`` ID is ever minted.

        P0 fix (26 Nov 2026, Phase 2 review): the long video is
        assembled AND uploaded FIRST, and the 3 Shorts are rendered
        only afterwards inside their own try/except. Previously the
        Shorts were rendered before the upload, so one Short assemble
        failure raised out of finalize and the already-rendered long
        video was never uploaded — contradicting the daily runner's
        Shorts-isolation claim. A Shorts failure now only logs a
        warning (the daily runner re-renders Shorts from the research
        bundle when no rendered pairs exist) and can never lose the
        long video.
        """
        state.stage = "finalize"
        logger.info("Executing node: finalize (Media assembly & private upload)")
        if not state.script:
            state.status = "failed"
            state.error = "No script to finalize: script generation produced no VideoScript."
            state.output_video_id = None
            logger.error(state.error)
            return state
        try:
            from agents.assemble import assemble_video, assemble_shorts
            from agents.uploader import run_upload

            # The ResearchBundle is threaded through to assembly so chart
            # scenes get real data. If it is missing, chart scenes raise
            # in generate_chart_by_type() instead of plotting fabricated
            # data (non-chart scenes do not need it).
            if not state.research:
                raise RuntimeError(
                    "Refusing to finalize: no ResearchBundle available to "
                    "build chart data from."
                )
            video_path = assemble_video(state.script, bundle_data=state.research)
            logger.info(f"Longform video assembled at: {video_path}")
            state.rendered_video_path = video_path

            # Real private upload of the LONG video FIRST (P0 fix, 26 Nov
            # 2026 — see docstring). Only a confirmed upload may
            # complete the run. The real VerificationReport from
            # node_verify is passed through — upload is refused unless
            # it is a pass. Fixed 25 Nov 2026: this was the
            # uploader/graph integration gap in the first patch set
            # (graph called run_upload() without a report, uploader
            # required one).
            if state.verification is None:
                raise RuntimeError(
                    "Refusing upload: no VerificationReport exists for this run."
                )
            if state.verification.overall != "pass":
                raise RuntimeError(
                    f"Refusing upload: verification overall is "
                    f"{state.verification.overall!r}, not 'pass'."
                )
            result = run_upload(
                video_path=video_path,
                thumb_path=None,
                script=state.script,
                verification_report=state.verification,
                cost_usd=state.cost_usd,
                # Internal linking (26 Nov 2026): the uploaded
                # description gets the "Watch next" (related videos)
                # + "Sources / References" blocks from the real
                # research bundle. The day's Shorts links are added
                # later by cross_link_daily_uploads (daily runner),
                # once the Shorts exist.
                bundle=state.research,
            )
            yt_video_id = result.yt_video_id
            if not yt_video_id or yt_video_id.startswith(("yt_sim_", "yt_priv_")):
                raise RuntimeError(
                    f"Uploader returned no real YouTube video ID (got {yt_video_id!r})"
                )
            state.output_video_id = yt_video_id
            state.status = "completed"
            logger.info(f"Private upload confirmed, YouTube video ID: {yt_video_id}")

            # Render ALL Shorts AFTER the long upload, isolated: any
            # Shorts failure is logged and leaves the completed long
            # video untouched. If rendering fails part-way, no partial
            # paths are recorded on the state (paths/scripts would be
            # unpaired), so the daily runner re-renders all Shorts
            # from the research bundle.
            try:
                shorts_paths: List[str] = []
                if state.research:
                    shorts_list = self.script_agent.generate_shorts(
                        state.research, count=3, trend_report=state.trend_report
                    )
                    state.shorts_scripts = list(shorts_list)
                    for short_script in shorts_list:
                        shorts_path = assemble_shorts(short_script, bundle_data=state.research)
                        logger.info(f"Shorts video assembled at: {shorts_path}")
                        shorts_paths.append(shorts_path)
                state.rendered_shorts_paths = shorts_paths
                state.rendered_shorts_path = shorts_paths[0] if shorts_paths else None
            except Exception as exc:  # noqa: BLE001 — Shorts must not kill the long video
                logger.warning(
                    f"Shorts rendering failed after long upload ({exc}); "
                    "long video stays completed, Shorts will be re-rendered "
                    "from the research bundle by the daily runner."
                )
            return state
        except Exception as e:
            logger.error(f"Finalize failed: {e}")
            state.status = "failed"
            state.error = str(e)
            state.output_video_id = None
            return state

    def node_human_review(self, state: PipelineState) -> PipelineState:
        """Node 6: Human review escalation after 3 failed verification iterations."""
        state.stage = "human_review"
        state.status = "human_review"
        logger.warning(f"Pipeline halted for topic '{state.topic}'. Awaiting human review via Telegram bot.")
        return state


def create_pipeline_graph() -> TubePilotGraph:
    """Factory creating configured LangGraph state machine."""
    return TubePilotGraph()


def run_pipeline(topic: str, pillar: str = "ai_everything", config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Top-level pipeline execution function.

    Executes TubePilotGraph and returns standardized result dictionary.
    """
    graph = create_pipeline_graph()
    state = graph.run(topic=topic, pillar=pillar)
    status_mapped = "complete" if state.status == "completed" else state.status
    return {
        "status": status_mapped,
        "video_id": state.output_video_id,
        "script": state.script,
        "research": state.research,
        "verification": state.verification,
        # The failure reason, when status is not "complete" (used by
        # scripts/dev_run.py to decide whether a failure is
        # topic-attributable — and the topic should be marked failed
        # and skipped — or a real run failure to alert on).
        "error": state.error,
        "trend_report": state.trend_report,
        "shorts_scripts": getattr(state, "shorts_scripts", []),
        "short_uploads": getattr(state, "short_uploads", []),
        "shorts_status": getattr(state, "shorts_status", "not_run"),
        # P0/P1: no real visual-cost source is tracked in this graph, so
        # report the tracked cost (0.0 = untracked/unknown), never the
        # fabricated 0.09 fallback.
        "visual_spend": state.cost_usd,
        "rendered_video_path": getattr(state, "rendered_video_path", None),
        "rendered_shorts_path": getattr(state, "rendered_shorts_path", None),
        "rendered_shorts_paths": getattr(state, "rendered_shorts_paths", []),
        "state": state,
    }

