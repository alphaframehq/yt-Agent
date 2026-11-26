"""Regression tests — Phase 2 additions, 26 Nov 2026.

1. P0 chart mandate (models/script.py): finance pillars keep the >= 8
   chart-scene rule; ai_everything makes chart scenes optional and
   rejects finance chart types / duplicate AI chart types.
2. Citation grounding (agents/verify.py): a number must be grounded in
   the scene's cited sources or the bundle financials — citing ANY
   source no longer passes an unrelated hallucinated number.
   Word-form numbers ("sixty-eight percent") are checked too.

Uses the shared fixtures (tests/_fixtures.py) — no live API, no keys.
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._sheets_fake import install_fake_sheets

_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()
    from config_manager import _invalidate_cache

    _invalidate_cache()


def tearDownModule():
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()


from tests._fixtures import make_test_bundle, make_test_script
from agents.verify import VerificationAgent, _extract_numbers
from models.script import (
    ChapterInfo,
    SceneInfo,
    ScriptMeta,
    SEOData,
    VideoScript,
)


def _ai_script(chart_scenes):
    """Minimal valid ai_everything VideoScript; chart_scenes is a list of
    (visual_type, chart_type) pairs appended as extra scenes."""
    sections = [
        "hook", "what_changed", "how_it_works", "why_it_matters",
        "risks_and_scams", "predictions", "disclaimer", "cta",
    ]
    scenes = []
    for i, section in enumerate(sections, start=1):
        narration = (
            "This video is for educational purposes only. "
            if section == "disclaimer"
            else f"This scene explains the {section} of the new AI tool."
        )
        scenes.append(SceneInfo(
            id=i, section=section, narration=narration,
            visual_prompt=f"AI {section} diagram",
            visual_type="diagram", chart_type=None,
            duration_sec=60, citations=[],
        ))
    for j, (v_type, c_type) in enumerate(chart_scenes, start=len(scenes) + 1):
        scenes.append(SceneInfo(
            id=j, section="how_it_works",
            narration="The chart shows how coverage of this topic breaks down by source.",
            visual_prompt="Source coverage chart",
            visual_type=v_type, chart_type=c_type,
            duration_sec=60, citations=[],
        ))
    description = (
        "DISCLAIMER: For educational and engineering analysis only. "
        + " ".join(["This analysis explains a new AI tool."] * 60)
    )
    seo = SEOData(
        title="New AI Tool Explained",
        description=description,
        tags=[f"aitool{i}" for i in range(16)],
        chapters=[ChapterInfo(time="00:00", title="Intro")],
    )
    word_count = sum(len(s.narration.split()) for s in scenes)
    return VideoScript(
        pillar="ai_everything",
        scenes=scenes,
        seo=seo,
        meta=ScriptMeta(word_count=word_count, estimated_duration_sec=sum(s.duration_sec for s in scenes)),
    )


class TestAIChartMandate(unittest.TestCase):
    def test_ai_script_with_zero_chart_scenes_passes(self):
        _ai_script([])  # must not raise

    def test_ai_script_with_one_ai_chart_passes(self):
        _ai_script([("chart", "ai_source_coverage")])

    def test_ai_script_with_both_ai_charts_passes(self):
        _ai_script([("chart", "ai_source_coverage"), ("chart", "ai_source_share")])

    def test_ai_script_with_finance_chart_type_raises(self):
        with self.assertRaises(ValueError):
            _ai_script([("chart", "revenue_trend")])

    def test_ai_script_with_duplicate_ai_chart_raises(self):
        with self.assertRaises(ValueError):
            _ai_script([("chart", "ai_source_coverage"), ("chart", "ai_source_coverage")])

    def test_finance_pillar_still_requires_eight_charts(self):
        script = make_test_script()
        self.assertEqual(
            sum(1 for s in script.scenes if s.visual_type == "chart"), 8
        )
        # Dropping one chart scene below 8 must raise.
        script.scenes[0].visual_type = "diagram"
        with self.assertRaises(ValueError):
            VideoScript.model_validate(script.model_dump())


class TestCitationGrounding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = VerificationAgent()
        cls.bundle = make_test_bundle()
        cls.valid_script = make_test_script(cls.bundle)

    def test_valid_fixture_script_passes_factual_accuracy(self):
        """Numbers in the fixture are grounded via cited sources or
        the bundle financials (e.g. $800M via financials even though
        the scene cites revenue_2024) — no false positive."""
        check, _ = self.agent._check_factual_accuracy(
            self.bundle, self.valid_script
        )
        self.assertEqual(check.status, "pass")
        self.assertEqual(check.issues, [])

    def test_unrelated_citation_no_longer_passes_hallucinated_number(self):
        """THE loophole: scene cites a real but unrelated source while
        stating a hallucinated number. Old code passed this; new code
        must fail it with a critical FACTUAL_ACCURACY issue."""
        script = copy.deepcopy(self.valid_script)
        script.scenes[0].citations = ["revenue_2024"]
        script.scenes[0].narration = (
            "TestCo secretly generated $999.88 billion in offshore untaxed revenue."
        )
        check, _ = self.agent._check_factual_accuracy(self.bundle, script)
        self.assertEqual(check.status, "fail")
        critical = [i for i in check.issues if i.severity == "critical"]
        self.assertGreaterEqual(len(critical), 1)
        self.assertIn("999.88", critical[0].claim)

    def test_word_form_number_is_checked(self):
        script = copy.deepcopy(self.valid_script)
        script.scenes[0].citations = ["revenue_2024"]
        script.scenes[0].narration = "TestCo grew sixty-eight percent last year."
        check, _ = self.agent._check_factual_accuracy(self.bundle, script)
        self.assertEqual(check.status, "fail")
        self.assertTrue(any("sixty-eight" in i.claim for i in check.issues))

    def test_number_grounded_in_cited_source_passes(self):
        script = copy.deepcopy(self.valid_script)
        script.scenes[0].citations = ["revenue_2024"]
        script.scenes[0].narration = "TestCo reported revenue of $1.2 billion in 2024."
        check, _ = self.agent._check_factual_accuracy(self.bundle, script)
        self.assertEqual(check.status, "pass")

    def test_extract_numbers_normalises_magnitudes(self):
        nums = _extract_numbers("Revenue was $1.2 billion, up from 1200 million.")
        self.assertIn((1200000000.0, "abs"), nums)
        self.assertIn((68.0, "pct"), _extract_numbers("sixty-eight percent"))
        # Years and bare small counts are not quantitative claims.
        self.assertNotIn((2024.0, "abs"), _extract_numbers("in 2024"))
        self.assertNotIn((3.0, "abs"), _extract_numbers("3 reasons to watch"))


if __name__ == "__main__":
    unittest.main()
