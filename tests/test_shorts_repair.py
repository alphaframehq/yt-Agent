"""Unit tests for the Shorts repair (27 Nov 2026, runs #19/#20).

The LLM returned Shorts with ``pillar`` missing ("Field required")
and ``seo`` missing or the wrong type, and every schema repair
re-prompt omitted them again — so the run died before any Short
rendered. ``_ShortsList`` now repairs those fields deterministically
from validation context (``{"pillar": bundle.pillar, "topic": ...}``)
instead of failing validation.

These tests use the exact old broken payload shape (missing pillar,
missing / wrong-type seo) and prove it now validates and is repaired
— no LLM, no network.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pydantic import ValidationError

from agents.script import _ShortsList

_SENTENCE = (
    "TestCo reported revenue of one point two billion dollars in "
    "twenty twenty four after strong growth in payments. "
)
_NARRATION = (_SENTENCE * 8).strip()  # 18 words x 8 = 144 words


def _scene(i, narration=_NARRATION):
    return {
        "id": i,
        "section": "hook" if i == 1 else "cta",
        "narration": narration,
        "visual_prompt": "TestCo diagram",
        "visual_type": "diagram",
        "duration_sec": 20,
        "citations": [],
    }


def _short_dict(*, pillar=True, seo=True, seo_value=None):
    """One raw Short dict in the shape the LLM returns."""
    short = {
        "scenes": [_scene(1), _scene(2)],
        "meta": {"word_count": 288, "estimated_duration_sec": 40},
    }
    if pillar:
        short["pillar"] = "ai_everything"
    if seo:
        short["seo"] = seo_value if seo_value is not None else {
            "title": "TestCo in 60 seconds",
            "description": (
                "DISCLAIMER: This video is for informational, "
                "educational, and software engineering analysis "
                "purposes only. Nothing stated here constitutes "
                "financial, investment, legal, or tax advice. We do "
                "not endorse buying or selling any security. "
                + " ".join([_NARRATION] * 2)
            ),
            "tags": [f"testco{i}" for i in range(16)],
        }
    return short


_CONTEXT = {"pillar": "finance_to_technical", "topic": "TestCo revenue breakdown"}


class TestShortsRepair(unittest.TestCase):
    def test_missing_pillar_and_seo_repaired_from_context(self):
        """The exact run #19/#20 payload: no pillar, no seo."""
        payload = {"shorts": [_short_dict(pillar=False, seo=False) for _ in range(3)]}
        result = _ShortsList.model_validate(payload, context=_CONTEXT)
        self.assertEqual(len(result.shorts), 3)
        for short in result.shorts:
            self.assertEqual(short.pillar, "finance_to_technical")
            self.assertTrue(short.seo.title)
            self.assertLessEqual(len(short.seo.title), 100)
            self.assertTrue(
                short.seo.description.startswith("DISCLAIMER"),
                "repaired SEO description must start with the disclaimer",
            )
            self.assertGreaterEqual(len(short.seo.description.split()), 300)
            self.assertGreaterEqual(len(short.seo.tags), 15)
            self.assertLessEqual(len(short.seo.tags), 25)

    def test_seo_wrong_type_repaired(self):
        """seo returned as a plain string instead of a dict."""
        payload = {"shorts": [
            _short_dict(pillar=False, seo=True, seo_value="not a dict")
        ]}
        result = _ShortsList.model_validate(payload, context=_CONTEXT)
        short = result.shorts[0]
        self.assertEqual(short.pillar, "finance_to_technical")
        self.assertTrue(short.seo.title)
        self.assertGreaterEqual(len(short.seo.description.split()), 300)

    def test_valid_payload_passes_through_unchanged(self):
        """A Short the LLM got right is not rewritten."""
        payload = {"shorts": [_short_dict(pillar=True, seo=True)]}
        result = _ShortsList.model_validate(payload, context=_CONTEXT)
        short = result.shorts[0]
        self.assertEqual(short.pillar, "ai_everything")
        self.assertEqual(short.seo.title, "TestCo in 60 seconds")
        self.assertEqual(len(short.seo.tags), 16)

    def test_no_context_defaults_pillar_to_ai_everything(self):
        payload = {"shorts": [_short_dict(pillar=False, seo=False)]}
        result = _ShortsList.model_validate(payload)
        self.assertEqual(result.shorts[0].pillar, "ai_everything")

    def test_short_without_scenes_still_fails(self):
        """Repair never invents scenes: a sceneless Short still
        fails validation loudly."""
        short = _short_dict(pillar=False, seo=False)
        del short["scenes"]
        with self.assertRaises(ValidationError):
            _ShortsList.model_validate({"shorts": [short]}, context=_CONTEXT)


if __name__ == "__main__":
    unittest.main()
