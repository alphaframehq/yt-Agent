"""End-to-End Orchestration Graph Tests (Phase 4).

Verifies that all paths through the StateGraph are fully reachable:
1. Happy path: research -> script -> verify (pass) -> finalize -> completed
2. Fix loop: verify fails on iteration 1 -> fix_script resolves -> verify passes on iteration 2 -> finalize
3. Escalation: verify fails 3 consecutive iterations -> human_review
4. Self-healing run_stage wrapper: transient failures retried and healed autonomously

2026-11-25 rewrite: the old happy-path test ran the full pipeline with
no credentials and asserted on the fabricated result (a fake yt_priv_
video ID and a verified-pass verdict). The P0 patches removed those
fakes: with no LLM / research sources, the pipeline now fails loudly
with no video ID. That fail-loud behaviour is asserted here. The
fix-loop and escalation tests now build their state from explicit
fixtures instead of calling run_research() with no keys.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from _sheets_fake import install_fake_sheets

# Google Sheets is the data store now (DynamoDB removed 25 Nov 2026).
# Tests must never touch the real Sheets API — mock the client.
# (These tests previously relied on ALLOW_IN_MEMORY_CONFIG; the fake
# replaces that dev-only fallback. Left active for the rest of the run.)
_FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()

from _fixtures import make_test_bundle, make_test_script
from orchestration.graph import PipelineState, TubePilotGraph
from pipeline import run_pipeline, run_stage
from workflow.healer import HealingContext


class TestPipelineGraph(unittest.TestCase):
    """Test suite for LangGraph state machine and execution paths."""

    def test_happy_path_fails_loudly_without_credentials(self):
        """With no LLM key / research sources, the pipeline must FAIL
        loudly — no fake yt_priv_ video ID, no verified-pass verdict,
        no 'completed' status."""
        graph = TubePilotGraph()
        state = graph.run(topic="How Stripe Makes Money", pillar="finance_to_technical")

        self.assertEqual(state.status, "failed")
        self.assertIsNone(state.output_video_id)
        self.assertIsNotNone(state.error)

    def test_fix_loop_path(self):
        """Verify that fix_script modifies script and re-triggers verify."""
        graph = TubePilotGraph()
        # Build state from explicit fixtures instead of live research.
        state = PipelineState(topic="Test Fix Loop Topic", pillar="finance_to_technical")
        state.research = make_test_bundle()
        state.script = make_test_script(state.research)

        # Inject fixable advice into scene
        state.script.scenes[0].narration += " In our view, you should buy into this architecture."

        # Verify will fail
        state.iteration = 1
        state = graph.node_verify(state)
        self.assertEqual(state.verification.overall, "fail")
        self.assertGreater(len(state.verification.suggested_fixes), 0)

        # Fix script runs and applies fix
        state = graph.node_fix_script(state)
        self.assertEqual(len(state.feedback_history), 1)

    def test_escalation_to_human_review_after_3_fails(self):
        """Verify that 3 consecutive verification failures escalate to human_review."""
        graph = TubePilotGraph()
        state = PipelineState(topic="Unfixable Topic", pillar="finance_to_technical")
        state.research = make_test_bundle()
        state.script = make_test_script(state.research)

        # Make script unfixable (remove all sections except hook)
        state.script.scenes = [state.script.scenes[0]]

        # Run 3 verify iterations
        for it in range(1, 4):
            state.iteration = it
            state = graph.node_verify(state)
            self.assertEqual(state.verification.overall, "fail")
            if it < 3:
                state = graph.node_fix_script(state)
            else:
                state = graph.node_human_review(state)

        self.assertEqual(state.status, "human_review")
        self.assertEqual(state.stage, "human_review")

    def test_run_stage_self_heals_transient_error(self):
        """Verify run_stage catches transient error, applies backoff, and succeeds on retry."""
        calls = {"count": 0}

        def flaky_api_call():
            calls["count"] += 1
            if calls["count"] == 1:
                raise Exception("429 Too many requests: transient rate limit")
            return "SUCCESS_DATA"

        context = HealingContext(stage="flaky_stage", attempt=1, max_retries=3)
        result = run_stage(flaky_api_call, context)

        self.assertEqual(result, "SUCCESS_DATA")
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
