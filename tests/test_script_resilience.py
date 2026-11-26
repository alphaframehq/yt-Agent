"""Tests for the run #17 script-generation resilience fix (26 Nov 2026).

Diagnosis (run #17, actions/runs/45862920108): research succeeded,
then script generation died at node_script with "All LLM providers in
fallback chain failed". The LLM's structured output was *almost*
right — scenes missing section / visual_type / duration_sec, a
251-word SEO description (300 required), 10 tags (15 required) —
and Gemini returned 503 overloads while OpenRouter returned
content=None, which crashed as a pydantic error on LLMResponse
instead of failing that attempt over to the next provider.

Fix under test:
1. ``models/script.py`` repairs near-miss LLM JSON instead of
   hard-failing: missing scene fields get defaults derived from the
   scene's index/narration, SEO minimums drop to achievable floors
   (150-word description, 5 tags) and the script is padded back up
   to the publishing targets (300 words, 15 tags) from its own
   title/scenes. AI-disclosure labelling is stripped at repair time.
2. Long-video length is AUTOMATIC per topic (user, 26 Nov 2026):
   totals outside roughly 5–15 min only warn, never reject.
   Shorts stay fixed: total duration 30–60s, hard-enforced.
3. ``tools/llm_provider.py``: Gemini 503/5xx on every key raises
   ``GeminiOverloadedError`` (a RuntimeError, so tenacity does NOT
   re-run the same failing keys) and the chain moves to the next
   provider promptly; OpenRouter content=None / API errors raise a
   clean ValueError (failed attempt, chain continues); empty
   provider responses are a failed attempt, not a crash;
   ``call_structured`` repair re-prompts list the exact fields to
   fix instead of a raw pydantic dump.

No network, no real keys: httpx and the provider methods are faked.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from pydantic import ValidationError

from models.script import (
    SEO_DESCRIPTION_MIN_WORDS,
    SEO_DESCRIPTION_TARGET_WORDS,
    SEO_TAGS_MIN,
    SEO_TAGS_TARGET,
    ShortsScript,
    VideoScript,
)
from tools.llm_provider import (
    GeminiOverloadedError,
    LLMProvider,
    LLMResponse,
    _summarize_validation_error,
    call_with_fallback,
)

DISCLAIMER = (
    "DISCLAIMER: This video is for informational, educational, and "
    "software engineering analysis purposes only. Nothing stated here "
    "constitutes financial, investment, legal, or tax advice."
)


def _long_desc(words: int) -> str:
    """A DISCLAIMER-led description of exactly ``words`` words."""
    filler = " ".join(["wealth shift robots automation"] * 80)
    text = (DISCLAIMER + " " + filler).split()
    return " ".join(text[:words])


def _malformed_long_dict() -> dict:
    """Run #17-shaped near-miss: 8 ai_everything scenes, some
    missing section/visual_type/duration_sec, 251-word description,
    10 tags, no meta at all."""
    sections = [
        "hook", "what_changed", "how_it_works", "why_it_matters",
        "risks_and_scams", "predictions", "disclaimer", "cta",
    ]
    scenes = []
    for i, section in enumerate(sections, start=1):
        narration = (
            f"This scene explains the {section} of the AI wealth shift "
            "in concrete detail with real examples from the retrieved "
            "research articles and the published sources listed below."
        )
        scene = {
            "id": i,
            "narration": narration,
            "visual_prompt": f"AI wealth shift {section} visual",
            "citations": [],
        }
        # Scenes 2 and 5 omit every optional field (the run #17 shape).
        if i not in (2, 5):
            scene.update(
                {
                    "section": section,
                    "visual_type": "diagram",
                    "duration_sec": 45,
                }
            )
        scenes.append(scene)
    return {
        "pillar": "ai_everything",
        "scenes": scenes,
        "seo": {
            "title": "AI, Robots and the Biggest Wealth Shift in History",
            "description": _long_desc(251),
            "tags": [f"wealth{i}" for i in range(10)],
            "chapters": [],
        },
    }


def _malformed_short_dict() -> dict:
    """Near-miss ShortsScript: 2 scenes missing fields, short
    description, 5 tags. Narrations total ~100 words so the derived
    durations land inside 30–60s (a Short with 30 words of narration
    cannot honestly be 30 seconds long — that must fail, not fake)."""
    return {
        "pillar": "ai_everything",
        "scenes": [
            {
                "id": 1,
                "narration": (
                    "AI robots are taking over warehouses right now and "
                    "the wealth shift is already happening in factories "
                    "across the world as automation spreads from logistics "
                    "to manufacturing and office work, with humanoid robots "
                    "from Tesla and Figure handling boxes, sorting parcels, "
                    "and working beside people on lines that never stop."
                ),
                "citations": [],
            },
            {
                "id": 2,
                "section": "cta",
                "narration": (
                    "Follow for the full breakdown in the long video on "
                    "this channel where we explain the whole wealth shift "
                    "step by step with real numbers and sources."
                ),
                "visual_prompt": "Subscribe visual",
                "visual_type": "ai_image",
                "citations": [],
            },
        ],
        "seo": {
            "title": "AI Robots Wealth Shift",
            "description": _long_desc(260),
            "tags": ["ai", "robots", "wealth", "future", "tech"],
            "chapters": [],
        },
    }


class TestScriptRepair(unittest.TestCase):
    def test_malformed_long_script_repairs_instead_of_failing(self):
        script = VideoScript.model_validate(_malformed_long_dict())
        # Missing scene fields got derived defaults.
        self.assertEqual(script.scenes[1].section, "what_changed")
        self.assertEqual(script.scenes[4].section, "risks_and_scams")
        self.assertEqual(script.scenes[1].visual_type, "diagram")
        self.assertEqual(script.scenes[4].visual_type, "diagram")
        self.assertGreater(script.scenes[1].duration_sec, 0)
        self.assertGreater(script.scenes[4].duration_sec, 0)
        # Provided values were never overwritten.
        self.assertEqual(script.scenes[0].duration_sec, 45)
        self.assertEqual(script.scenes[0].section, "hook")
        # SEO padded back to publishing targets from the script itself.
        self.assertGreaterEqual(
            len(script.seo.description.split()), SEO_DESCRIPTION_TARGET_WORDS
        )
        self.assertTrue(script.seo.description.startswith("DISCLAIMER"))
        self.assertGreaterEqual(len(script.seo.tags), SEO_TAGS_TARGET)
        self.assertLessEqual(len(script.seo.tags), 25)
        self.assertEqual(script.seo.tags[0], "wealth0")
        # Meta filled from the scenes.
        self.assertGreater(script.meta.word_count, 0)
        self.assertEqual(
            script.meta.estimated_duration_sec,
            sum(s.duration_sec for s in script.scenes),
        )

    def test_malformed_short_repairs_and_stays_in_30_60s(self):
        short = ShortsScript.model_validate(_malformed_short_dict())
        total = sum(s.duration_sec for s in short.scenes)
        self.assertGreaterEqual(total, 30)
        self.assertLessEqual(total, 60)
        self.assertEqual(short.scenes[0].section, "hook")
        self.assertEqual(short.scenes[0].visual_type, "diagram")
        self.assertGreaterEqual(len(short.seo.tags), SEO_TAGS_TARGET)
        self.assertGreaterEqual(
            len(short.seo.description.split()), SEO_DESCRIPTION_TARGET_WORDS
        )

    def test_short_total_below_30s_fails_with_clear_message(self):
        data = _malformed_short_dict()
        data["scenes"][0]["duration_sec"] = 10
        data["scenes"][1]["duration_sec"] = 10
        with self.assertRaises(ValidationError) as ctx:
            ShortsScript.model_validate(data)
        self.assertIn("30", str(ctx.exception))
        self.assertIn("60", str(ctx.exception))

    def test_short_total_above_60s_fails_with_clear_message(self):
        data = _malformed_short_dict()
        data["scenes"][0]["duration_sec"] = 40
        data["scenes"][1]["duration_sec"] = 40
        with self.assertRaises(ValidationError) as ctx:
            ShortsScript.model_validate(data)
        self.assertIn("60", str(ctx.exception))

    def test_long_total_outside_5_15_min_only_warns_never_rejects(self):
        # Length is automatic per topic (user, 26 Nov 2026): a 200s
        # or a 1200s script must validate — never be shrunk/rejected.
        for total in (200, 1200):
            data = _malformed_long_dict()
            per_scene = total // len(data["scenes"])
            for scene in data["scenes"]:
                scene["duration_sec"] = per_scene
                scene["section"] = scene.get("section") or "body"
                scene["visual_type"] = "diagram"
            script = VideoScript.model_validate(data)
            self.assertEqual(
                sum(s.duration_sec for s in script.scenes),
                per_scene * len(data["scenes"]),
            )

    def test_degenerate_description_still_fails(self):
        # Even after padding, a ~40-word description cannot reach
        # the 150-word floor: that output is genuinely broken and
        # must fail loudly so the repair loop regenerates it.
        data = _malformed_long_dict()
        data["seo"]["description"] = DISCLAIMER + " tiny."
        data["scenes"] = data["scenes"][:1]
        with self.assertRaises(ValidationError) as ctx:
            VideoScript.model_validate(data)
        self.assertIn(str(SEO_DESCRIPTION_MIN_WORDS), str(ctx.exception))

    def test_ai_disclosure_labelling_stripped_at_repair(self):
        data = _malformed_long_dict()
        data["seo"]["tags"] = ["wealth0", "AI generated", "robots"]
        data["seo"]["description"] = _long_desc(200) + " Made with AI tools."
        script = VideoScript.model_validate(data)
        self.assertNotIn("AI generated", script.seo.tags)
        self.assertNotIn("Made with AI", script.seo.description)

    def test_summarize_validation_error_lists_fields(self):
        data = _malformed_short_dict()
        data["scenes"][0]["duration_sec"] = 5
        data["scenes"][1]["duration_sec"] = 5
        try:
            ShortsScript.model_validate(data)
            self.fail("expected ValidationError")
        except ValidationError as exc:
            summary = _summarize_validation_error(exc)
            self.assertIn("30", summary)
            self.assertLess(len(summary), 1000)


class TestProviderFallback(unittest.TestCase):
    def test_openrouter_content_none_raises_clean_value_error(self):
        """Run #17: content=None crashed as a pydantic error on
        LLMResponse. It must be a clean ValueError so the chain
        moves on."""

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": None}}]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        with (
            patch("tools.llm_provider._get_secret_optional", return_value="k"),
            patch("tools.llm_provider.httpx.Client", _Client),
        ):
            with self.assertRaises(ValueError) as ctx:
                LLMProvider("openrouter", "openrouter/free")._call_openrouter(
                    "sys", "user"
                )
            self.assertIn("no content", str(ctx.exception).lower())

    def test_openrouter_api_error_field_raises_value_error(self):
        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"error": {"message": "rate limited", "code": 429}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        with (
            patch("tools.llm_provider._get_secret_optional", return_value="k"),
            patch("tools.llm_provider.httpx.Client", _Client),
        ):
            with self.assertRaises(ValueError) as ctx:
                LLMProvider("openrouter", "openrouter/free")._call_openrouter(
                    "sys", "user"
                )
            self.assertIn("rate limited", str(ctx.exception))

    def test_fallback_continues_past_failed_provider(self):
        """A failed provider must not end the chain: the next
        provider's answer is used."""
        with (
            patch.object(
                LLMProvider,
                "_call_openrouter",
                side_effect=ValueError("OpenRouter returned no content"),
            ),
            patch.object(
                LLMProvider,
                "_call_gemini",
                return_value=LLMResponse(
                    content='{"ok": true}', provider="gemini", model="m"
                ),
            ),
        ):
            result = call_with_fallback(
                [["openrouter", "openrouter/free"], ["gemini", "gemini-3.8-flash"]],
                "sys",
                "user",
            )
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(result.content, '{"ok": true}')

    def test_gemini_503_all_keys_raises_overload_without_tenacity_retry(self):
        """Run #17: Gemini 503s were retried 3x per call by tenacity
        before the chain moved on. All-keys-503 must raise
        GeminiOverloadedError immediately (one POST per key)."""
        calls = {"n": 0}

        class _Resp:
            status_code = 503

            def __init__(self):
                self.request = httpx.Request("POST", "https://example.com")

            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                calls["n"] += 1
                return _Resp()

        with (
            patch("tools.llm_provider.get_gemini_pool_keys", return_value=["k1", "k2"]),
            patch("tools.llm_provider.mark_gemini_key_failed", return_value=None),
            patch("tools.llm_provider.httpx.Client", _Client),
        ):
            with self.assertRaises(GeminiOverloadedError) as ctx:
                LLMProvider("gemini", "gemini-3.8-flash").call("sys", "user")
            self.assertIn("503", str(ctx.exception))
        self.assertEqual(calls["n"], 2)

    def test_call_structured_repair_lists_exact_fields(self):
        """First attempt omits narration (unrepairable by defaults);
        the repair re-prompt must name the field, and the second
        attempt's valid output is returned."""
        bad = json.dumps(
            {
                "pillar": "ai_everything",
                "scenes": [
                    {
                        "id": 1,
                        "section": "hook",
                        "visual_prompt": "x",
                        "visual_type": "diagram",
                        "duration_sec": 45,
                        "citations": [],
                    }
                ],
                "seo": {
                    "title": "AI Wealth Shift",
                    "description": _long_desc(200),
                    "tags": [f"t{i}" for i in range(16)],
                },
                "meta": {"word_count": 50, "estimated_duration_sec": 45},
            }
        )
        good = json.dumps(_malformed_long_dict())
        seen_prompts: list[str] = []

        def _fake_call(self, system_prompt, user_prompt, schema=None, temperature=0.2):
            seen_prompts.append(user_prompt)
            content = bad if len(seen_prompts) == 1 else good
            return LLMResponse(content=content, provider="gemini", model="m")

        with patch.object(LLMProvider, "call", _fake_call):
            script = LLMProvider("gemini", "gemini-3.8-flash").call_structured(
                "sys", "user", VideoScript, max_repair_attempts=2
            )
        self.assertEqual(len(script.scenes), 8)
        self.assertEqual(len(seen_prompts), 2)
        self.assertIn("narration", seen_prompts[1].lower())


if __name__ == "__main__":
    unittest.main()
