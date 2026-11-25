"""Media Pipeline Test Suite (Phase 5: Charts, TTS, Visuals, Assembly).

2026-11-25 rewrite: the old setUpClass called run_research()/run_script()
with no credentials and relied on the production mock/demo fallbacks.
Those fallbacks are removed. Chart tests now pass explicit, labelled
test data; TTS/assembly tests assert the new fail-loud behaviour when
no Polly/LLM credentials exist in CI.
"""

import os
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALLOW_IN_MEMORY_CONFIG", "1")
os.environ.setdefault("ALLOW_IN_MEMORY_SECRETS", "1")

from _fixtures import make_test_bundle, make_test_script
from agents.charts import generate_chart_by_type, revenue_trend
from agents.visuals import VisualBudgetTracker, research_bundle_to_chart_data
from agents.assemble import assemble_video


class TestMediaPipeline(unittest.TestCase):
    """Test suite covering Phase 5 media generation components."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = make_test_bundle()
        cls.script = make_test_script(cls.bundle)
        cls.chart_data = research_bundle_to_chart_data(cls.bundle)

    def test_sample_chart_generation(self):
        """Test exit criteria: generate sample chart PNG and verify size."""
        chart_path = revenue_trend(["2021", "2022", "2023", "2024"], [4.5e9, 7.8e9, 11.2e9, 14.5e9], "Revolut")
        self.assertTrue(os.path.exists(chart_path))
        self.assertGreater(Path(chart_path).stat().st_size, 500)
        self.assertTrue(chart_path.endswith(".png"))
        print(f"\n[SAMPLE CHART GENERATED]: {chart_path} ({Path(chart_path).stat().st_size} bytes)")

    def test_chart_types_with_real_bundle_data(self):
        """Chart types that have a real source in the fixture bundle render."""
        for c_type in ("revenue_trend", "segment_breakdown", "margin_compare",
                       "unit_economics_waterfall", "scope_tam"):
            path = generate_chart_by_type(c_type, data=self.chart_data)
            self.assertTrue(os.path.exists(path), f"Failed for chart type: {c_type}")
            self.assertGreater(Path(path).stat().st_size, 0)

    def test_chart_types_without_data_raise(self):
        """Chart generation without bundle data raises instead of plotting
        fabricated defaults (the old stub-PNG behaviour)."""
        for c_type in ("revenue_trend", "segment_breakdown", "margin_compare",
                       "unit_economics_waterfall", "scope_tam", "competitor_matrix",
                       "growth_margin_scatter", "market_share_donut"):
            with self.assertRaises(ValueError, msg=f"chart type: {c_type}"):
                generate_chart_by_type(c_type)

    def test_chart_types_without_a_real_source_raise(self):
        """Chart types with no real source in the bundle (market share,
        competitor take-rate matrix) raise instead of inventing data."""
        for c_type in ("competitor_matrix", "market_share_donut", "growth_margin_scatter"):
            with self.assertRaises((ValueError, KeyError, TypeError, AttributeError),
                                  msg=f"chart type: {c_type}"):
                generate_chart_by_type(c_type, data=self.chart_data)

    def test_tts_audio_synthesis_fails_without_polly(self):
        """Without AWS Polly credentials, TTS must raise instead of
        returning a 1-second silence stub as success."""
        from agents.tts import synthesize_scene_audio
        narration = "Welcome to TubePilot AI. We deconstruct fintech architecture and capital flows."
        with self.assertRaises(Exception):
            synthesize_scene_audio(narration)

    def test_visual_budget_tracker_enforcement(self):
        """Verify visual budget limits generative AI spending."""
        tracker = VisualBudgetTracker(max_budget=0.10)
        self.assertTrue(tracker.can_afford(0.05))
        tracker.record_spend(0.05, "clip 1")

        self.assertTrue(tracker.can_afford(0.05))
        tracker.record_spend(0.05, "clip 2")

        # Now at $0.10, cannot afford another $0.05 clip
        self.assertFalse(tracker.can_afford(0.05))

    def test_video_assembly_longform_fails_without_tts(self):
        """Assembly without working TTS must raise instead of writing an
        empty placeholder MP4 as success."""
        with self.assertRaises(Exception):
            assemble_video(self.script, "test_render_longform.mp4", bundle_data=self.bundle)

    def test_video_assembly_without_bundle_data_raises(self):
        """Assembly with chart scenes but no research bundle must raise
        instead of plotting fabricated chart data."""
        with self.assertRaises(Exception):
            assemble_video(self.script, "test_render_shorts.mp4", bundle_data=None)


if __name__ == "__main__":
    unittest.main()
