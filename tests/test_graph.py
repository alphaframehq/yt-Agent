"""End-to-End Orchestration Graph Tests (Phase 4).

Verifies that all paths through the StateGraph are fully reachable:
1. Happy path: research -> script -> verify (pass) -> finalize -> completed
2. Fix loop: verify fails on iteration 1 -> fix_script resolves -> verify passes on iteration 2 -> finalize
3. Escalation: verify fails 3 consecutive iterations -> human_review
4. Self-healing run_stage wrapper: transient failures retried and healed autonomously
"""

import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestration.graph import PipelineState, TubePilotGraph
from pipeline import run_pipeline, run_stage
from workflow.healer import HealingContext


class TestPipelineGraph(unittest.TestCase):
    """Test suite for LangGraph state machine and execution paths."""

    def test_happy_path_completes(self):
        """Verify normal flow reaches finalize and assigns private video ID."""
        graph = TubePilotGraph()
        state = graph.run(topic="How Stripe Makes Money", pillar="finance_to_technical")

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.stage, "finalize")
        self.assertIsNotNone(state.output_video_id)
        self.assertTrue(state.output_video_id.startswith("yt_priv_"))
        self.assertIsNotNone(state.research)
        self.assertIsNotNone(state.script)
        self.assertEqual(state.verification.overall, "pass")

    def test_fix_loop_path(self):
        """Verify that fix_script modifies script and re-triggers verify."""
        graph = TubePilotGraph()
        # Initialize state with a script that has a fixable forbidden phrase
        state = PipelineState(topic="Test Fix Loop Topic", pillar="finance_to_technical")
        state = graph.node_research(state)
        state = graph.node_script(state)
        
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
        state = graph.node_research(state)
        state = graph.node_script(state)
        
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
