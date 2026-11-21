"""Media Pipeline Test Suite (Phase 5: Charts, TTS, Visuals, Assembly)."""

import os
import sys
import unittest
from pathlib import Path

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.charts import generate_chart_by_type, revenue_trend
from agents.research import run_research
from agents.script import generate_shorts, run_script
from agents.tts import synthesize_scene_audio
from agents.visuals import VisualBudgetTracker, VisualPipeline
from agents.assemble import assemble_shorts, assemble_video


class TestMediaPipeline(unittest.TestCase):
    """Test suite covering Phase 5 media generation components."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = run_research("How Revolut Makes Money", "finance_to_technical")
        cls.script = run_script(cls.bundle)

    def test_sample_chart_generation(self):
        """Test exit criteria: generate sample chart PNG and verify size."""
        chart_path = revenue_trend(["2021", "2022", "2023", "2024"], [4.5e9, 7.8e9, 11.2e9, 14.5e9], "Revolut")
        self.assertTrue(os.path.exists(chart_path))
        self.assertGreater(Path(chart_path).stat().st_size, 500)
        self.assertTrue(chart_path.endswith(".png"))
        print(f"\n[SAMPLE CHART GENERATED]: {chart_path} ({Path(chart_path).stat().st_size} bytes)")

    def test_all_8_chart_types_generated(self):
        """Verify each of the 8 institutional chart types renders successfully."""
        chart_types = [
            "revenue_trend",
            "segment_breakdown",
            "margin_compare",
            "unit_economics_waterfall",
            "scope_tam",
            "competitor_matrix",
            "growth_margin_scatter",
            "market_share_donut",
        ]
        for c_type in chart_types:
            path = generate_chart_by_type(c_type)
            self.assertTrue(os.path.exists(path), f"Failed for chart type: {c_type}")
            self.assertGreater(Path(path).stat().st_size, 0)

    def test_tts_audio_synthesis(self):
        """Verify scene voiceover generation and duration measurement."""
        narration = "Welcome to TubePilot AI. We deconstruct fintech architecture and capital flows."
        path, duration = synthesize_scene_audio(narration)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(duration, 1.0)

    def test_visual_budget_tracker_enforcement(self):
        """Verify visual budget limits generative AI spending."""
        tracker = VisualBudgetTracker(max_budget=0.10)
        self.assertTrue(tracker.can_afford(0.05))
        tracker.record_spend(0.05, "clip 1")
        
        self.assertTrue(tracker.can_afford(0.05))
        tracker.record_spend(0.05, "clip 2")
        
        # Now at $0.10, cannot afford another $0.05 clip
        self.assertFalse(tracker.can_afford(0.05))

    def test_video_assembly_longform(self):
        """Verify longform 16:9 video composition produces output file."""
        output = assemble_video(self.script, "test_render_longform.mp4")
        self.assertTrue(os.path.exists(output))
        self.assertGreater(Path(output).stat().st_size, 0)

    def test_video_assembly_shorts(self):
        """Verify vertical 9:16 shorts composition produces output file."""
        shorts = generate_shorts(self.bundle, count=1)
        self.assertEqual(len(shorts), 1)
        output = assemble_shorts(shorts[0], "test_render_shorts.mp4")
        self.assertTrue(os.path.exists(output))
        self.assertGreater(Path(output).stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
