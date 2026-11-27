"""Tests for the run #18 fixes (26 Nov 2026).

Diagnosis (run #18, actions/runs/45873763490): the run got PAST the
run #17 failure point — research completed, a script was generated,
verification PASSED, TTS synthesized all 8 scenes — then died in
two new places:

1. Long video, node_finalize: image generation failed twice —
   "Imagen 3 request failed ... Client error '404 Not Found' for
   url '.../imagen-3.0-generate-001:predict'" — then "Finalize
   failed: RetryError". Imagen 3 is a PAID model; the free Gemini
   key was never going to serve it. Fix: the image path now falls
   back — Imagen 3 -> Pixabay free stock image (PIXABAY_KEY is in
   the workflow env) -> locally rendered programmatic title card —
   and a single image failure never kills finalize. The 404 also
   fails fast now instead of rotating keys and poisoning the
   text-generation pool for a problem that is not the key's fault.
2. Shorts, render_and_upload_shorts: "ShortsScript total duration
   must be 30–60s, got 62s" (gemini-3.5-flash-lite produced 28s,
   openrouter/free 62s) and the 74faecf hard rule just rejected
   them. Fix: the repair pass ACTIVELY fits the narration — trims
   trailing sentences while too long, appends generic CTA lines
   while too short — instead of regenerating and hoping.
3. Attempt 1 also died at the script node: a scene said
   "guaranteed returns". Fix: the repair pass REWRITES forbidden
   investment-advice phrasing into neutral wording instead of
   rejecting the whole script.

No network, no real keys: httpx and the provider functions are
faked. NOTE: the sandbox proxy env breaks real httpx.Client()
construction (InvalidURL), which is also why 3 pre-existing tests
fail here — so the Imagen tests patch httpx.Client at class level
instead of only patching .post.
"""

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.verify import FORBIDDEN_ADVICE_REGEX
from models.script import (
    _FORBIDDEN_PATTERN,
    _rewrite_forbidden_phrases,
    SceneInfo,
    ShortsScript,
    VideoScript,
)

DISCLAIMER = (
    "DISCLAIMER: This video is for informational, educational, and "
    "software engineering analysis purposes only. Nothing stated here "
    "constitutes financial, investment, legal, or tax advice. We do not "
    "endorse buying or selling any security."
)

# 1x1 PNG, base64 (same tiny fixture as test_gemini_pool.py).
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
FAKE_GEMINI_KEY = "AIzaFakeTestKey1234567890"


def _long_desc(words: int) -> str:
    """A DISCLAIMER-led description of exactly ``words`` words."""
    filler = " ".join(["wealth shift robots automation"] * 80)
    text = (DISCLAIMER + " " + filler).split()
    return " ".join(text[:words])


def _sentences(words: int) -> str:
    """Narration of exactly ``words`` words, in ~12-word sentences."""
    vocab = [
        "robots", "wealth", "factories", "automation", "workers",
        "markets", "capital", "systems", "future", "change",
        "growth", "tools",
    ]
    out = []
    remaining = words
    while remaining > 0:
        take = min(12, remaining)
        sent = " ".join(vocab[i % len(vocab)] for i in range(take))
        out.append(sent.capitalize() + ".")
        remaining -= take
    return " ".join(out)


def _seo(title: str = "AI, Robots and the Biggest Wealth Shift in History") -> dict:
    return {
        "title": title,
        "description": _long_desc(300),
        "tags": [f"wealth{i}" for i in range(15)],
        "chapters": [],
    }


def _short_dict(scene_words: list[int], durations: list[int]) -> dict:
    """ShortsScript dict with the given per-scene word counts and
    declared durations (run #18 shape: 62s or 28s totals)."""
    sections = ["hook", "body", "cta"]
    scenes = [
        {
            "id": i + 1,
            "section": sections[i],
            "narration": _sentences(w),
            "visual_prompt": f"AI wealth shift scene {i + 1}",
            "visual_type": "diagram",
            "duration_sec": d,
            "citations": [],
        }
        for i, (w, d) in enumerate(zip(scene_words, durations))
    ]
    return {"pillar": "ai_everything", "scenes": scenes, "seo": _seo()}


def _long_dict(narration_by_scene: dict[int, str]) -> dict:
    """VideoScript dict (3 diagram scenes) with per-scene narration."""
    sections = ["hook", "body", "cta"]
    scenes = [
        {
            "id": i + 1,
            "section": sections[i],
            "narration": narration_by_scene.get(i + 1, _sentences(60)),
            "visual_prompt": f"AI wealth shift scene {i + 1}",
            "visual_type": "diagram",
            "duration_sec": 60,
            "citations": [],
        }
        for i in range(3)
    ]
    return {"pillar": "ai_everything", "scenes": scenes, "seo": _seo()}


class TestImagen404Fallback(unittest.TestCase):
    """Fix 1: Imagen 404 must fail fast and finalize must fall back."""

    def _fake_client_404(self):
        """A fake httpx.Client returning HTTP 404 for Imagen."""
        resp = MagicMock()
        resp.status_code = 404
        resp.request = MagicMock()
        resp.json.return_value = {"error": {"message": "Model not found"}}
        client = MagicMock()
        client.post.return_value = resp
        cm = MagicMock()
        cm.__enter__.return_value = client
        cm.__exit__.return_value = False
        return cm, client

    def test_imagen3_404_fails_fast_without_key_rotation_or_pool_poison(self):
        """A 404 means the model is not served for this key — no
        second key can help, and the key must NOT be marked failed
        (it is fine for text generation)."""
        from tools.image_gen import _generate_imagen3

        cm, client = self._fake_client_404()
        dest = Path("cache/images/test_404_imagen.png")
        with (
            patch.dict(os.environ, {"GEMINI_KEY": FAKE_GEMINI_KEY}),
            patch("tools.image_gen.httpx.Client", return_value=cm),
            patch("tools.image_gen.mark_gemini_key_failed") as mark_failed,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _generate_imagen3("test prompt", dest)
        self.assertIn("404", str(ctx.exception))
        # Fail fast: exactly one request, no rotation to a second key.
        self.assertEqual(client.post.call_count, 1)
        # The key is not the problem: never put in cooldown.
        mark_failed.assert_not_called()

    def test_visuals_fall_back_to_pixabay_when_imagen_fails(self):
        """resolve_scene(ai_image) survives an Imagen failure via a
        Pixabay stock image: real file, $0.00 cost, no raise."""
        from agents.visuals import VisualPipeline

        scene = SceneInfo(
            id=1, section="hook",
            narration=_sentences(40),
            visual_prompt="AI robots in a warehouse",
            visual_type="ai_image", duration_sec=20,
        )
        stock_path = Path("cache/media/test_pixabay_fallback.jpg")
        stock_path.parent.mkdir(parents=True, exist_ok=True)

        def _fake_download(url, extension="jpg"):
            stock_path.write_bytes(base64.b64decode(_TINY_PNG_B64))
            return str(stock_path.resolve())

        try:
            with (
                patch("agents.visuals.generate_image",
                      side_effect=RuntimeError("Imagen 3 unavailable (HTTP 404)")),
                patch("agents.visuals.search_stock_images",
                      return_value=[{"id": 1, "tags": "robots",
                                     "url": "https://example.com/robot.jpg"}]),
                patch("agents.visuals.download_media", side_effect=_fake_download),
            ):
                result = VisualPipeline(max_budget=0.30).resolve_scene(scene)
            self.assertTrue(Path(result["asset_path"]).exists())
            self.assertGreater(Path(result["asset_path"]).stat().st_size, 0)
            self.assertEqual(result["cost_usd"], 0.0)
        finally:
            if stock_path.exists():
                stock_path.unlink()

    def test_visuals_fall_back_to_programmatic_card_when_no_stock(self):
        """With Imagen dead AND no Pixabay hit, the scene still gets
        a real renderable visual (programmatic title card) and
        finalize does not die."""
        from agents.visuals import VisualPipeline
        from tools.image_gen import get_cached_image_path

        prompt = "AI robots in a warehouse programmatic fallback test"
        scene = SceneInfo(
            id=1, section="hook",
            narration=_sentences(40),
            visual_prompt=prompt,
            visual_type="ai_image", duration_sec=20,
        )
        prog_path = get_cached_image_path(prompt, "programmatic")
        try:
            with (
                patch("agents.visuals.generate_image",
                      side_effect=RuntimeError("Imagen 3 unavailable (HTTP 404)")),
                patch("agents.visuals.search_stock_images", return_value=[]),
            ):
                result = VisualPipeline(max_budget=0.30).resolve_scene(scene)
            self.assertTrue(Path(result["asset_path"]).exists())
            self.assertGreater(Path(result["asset_path"]).stat().st_size, 0)
            self.assertEqual(result["cost_usd"], 0.0)
            # A real PNG, not a 0-byte file.
            with open(result["asset_path"], "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
        finally:
            if prog_path.exists():
                prog_path.unlink()

    def test_programmatic_visual_is_a_real_png(self):
        """generate_programmatic_visual writes a non-empty PNG at
        both aspect ratios."""
        from tools.image_gen import generate_programmatic_visual

        for ratio, size in (( "16:9", (1280, 720)), ("9:16", (1080, 1920))):
            dest = Path(f"cache/images/test_prog_{ratio.replace(':', 'x')}.png")
            try:
                out = generate_programmatic_visual(
                    "AI robots reshaping warehouse work", dest, aspect_ratio=ratio
                )
                self.assertTrue(out.exists())
                self.assertGreater(out.stat().st_size, 0)
                from PIL import Image

                with Image.open(out) as img:
                    self.assertEqual(img.size, size)
            finally:
                if dest.exists():
                    dest.unlink()

    def test_pixabay_image_search_returns_empty_without_key(self):
        """No PIXABAY_KEY: the search returns [] instead of raising,
        so the chain can fall through to the programmatic visual."""
        from tools.pixabay import search_stock_images

        env = {k: v for k, v in os.environ.items() if k != "PIXABAY_KEY"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(search_stock_images("robots"), [])


class TestShortsDurationFit(unittest.TestCase):
    """Fix 2: a 62s / 28s Short is repaired into 30–60s."""

    def test_62s_short_is_trimmed_into_range(self):
        """Run #18 shape: 155 words / 62s declared -> validates,
        total and speech-implied length inside 30–60s, hook kept."""
        data = _short_dict([60, 60, 35], [25, 25, 12])
        short = ShortsScript.model_validate(data)
        total = sum(s.duration_sec for s in short.scenes)
        self.assertGreaterEqual(total, 30)
        self.assertLessEqual(total, 60)
        words = sum(len(s.narration.split()) for s in short.scenes)
        implied = words * 60.0 / 150.0
        self.assertGreaterEqual(implied, 30)
        self.assertLessEqual(implied, 60)
        # Trimming works from the end: the hook scene is untouched.
        self.assertEqual(short.scenes[0].narration, _sentences(60))

    def test_28s_short_is_extended_into_range(self):
        """Run #18 shape: 70 words / 28s declared -> validates,
        total inside 30–60s, extended with a generic CTA line."""
        data = _short_dict([30, 25, 15], [12, 10, 6])
        short = ShortsScript.model_validate(data)
        total = sum(s.duration_sec for s in short.scenes)
        self.assertGreaterEqual(total, 30)
        self.assertLessEqual(total, 60)
        words = sum(len(s.narration.split()) for s in short.scenes)
        implied = words * 60.0 / 150.0
        self.assertGreaterEqual(implied, 30)
        self.assertLessEqual(implied, 60)
        self.assertIn(
            "Follow for more quick explainers.", short.scenes[-1].narration
        )

    def test_in_range_short_is_untouched(self):
        """A 45s Short (112 words) passes through with narration
        and durations unchanged — the fit only fixes outliers."""
        data = _short_dict([45, 40, 27], [18, 16, 11])
        short = ShortsScript.model_validate(data)
        self.assertEqual(
            [s.narration for s in short.scenes],
            [_sentences(w) for w in (45, 40, 27)],
        )
        self.assertEqual(
            [s.duration_sec for s in short.scenes], [18, 16, 11]
        )


class TestForbiddenPhraseRewrite(unittest.TestCase):
    """Fix 3: forbidden money phrasing is rewritten, not rejected."""

    def test_guaranteed_returns_rewritten_in_long_script(self):
        """Run #18 attempt-1 shape: scene 5 says 'guaranteed
        returns' -> the script validates, the phrase becomes
        neutral, and neither gate (model validator / verify regex)
        can flag it."""
        narration = (
            "Robots will deliver guaranteed returns for early investors "
            "while factories automate the boring work away from people."
        )
        script = VideoScript.model_validate(
            _long_dict({2: narration})
        )
        fixed = script.scenes[1].narration
        self.assertIn("potential returns", fixed)
        self.assertNotIn("guaranteed", fixed.lower())
        self.assertIsNone(_FORBIDDEN_PATTERN.search(fixed))
        self.assertIsNone(FORBIDDEN_ADVICE_REGEX.search(fixed))

    def test_advice_phrases_rewritten_in_shorts(self):
        """'you should buy this stock' and 'invest in' are rewritten
        into neutral wording; bare buy/sell words are gone too."""
        narration = (
            "You should buy this stock now because everyone will invest "
            "in robots and people who buy early will sell at the top."
        )
        short = ShortsScript.model_validate(
            _short_dict([45, 40, 27], [18, 16, 11])
        )
        rewritten = _rewrite_forbidden_phrases(narration)
        self.assertIn("some investors consider this stock", rewritten)
        self.assertIn("back robots", rewritten)
        self.assertIsNone(_FORBIDDEN_PATTERN.search(rewritten))
        self.assertIsNone(FORBIDDEN_ADVICE_REGEX.search(rewritten))
        # The Short itself validates with the rewritten narration.
        data = _short_dict([45, 40, 27], [18, 16, 11])
        data["scenes"][0]["narration"] = narration
        fixed_short = ShortsScript.model_validate(data)
        self.assertEqual(fixed_short.scenes[0].narration, rewritten)

    def test_disclaimer_scene_is_untouched(self):
        """The exact DISCLAIMER_TEXT contains 'buying or selling'
        (not bare buy/sell) and no forbidden phrase — the rewrite
        must leave it byte-identical."""
        self.assertEqual(_rewrite_forbidden_phrases(DISCLAIMER), DISCLAIMER)
        script = VideoScript.model_validate(
            _long_dict({2: DISCLAIMER})
        )
        self.assertEqual(script.scenes[1].narration, DISCLAIMER)

    def test_rewrite_map_covers_both_gate_lists(self):
        """Every phrase in the model's forbidden list and every word
        in verify's regex is rewritten away — no gate can still
        match the rewritten text."""
        cases = [
            "not financial advice",
            "buy this stock",
            "guaranteed returns",
            "get rich quick",
            "invest now",
            "you should buy",
            "you should sell",
            "financial advice",
            "investment advice",
            "trust me",
            "I promise you",
            "can't lose",
            "risk-free",
            "act now",
            "limited time offer",
            "will rise",
            "sure thing",
            "skyrocket",
            "to the moon",
            "buy low and sell high",
            "invest in robots",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                rewritten = _rewrite_forbidden_phrases(
                    f"Analysts note that {phrase} in this market."
                )
                self.assertIsNone(
                    _FORBIDDEN_PATTERN.search(rewritten),
                    f"model gate still matches after rewrite: {rewritten!r}",
                )
                self.assertIsNone(
                    FORBIDDEN_ADVICE_REGEX.search(rewritten),
                    f"verify gate still matches after rewrite: {rewritten!r}",
                )


if __name__ == "__main__":
    unittest.main()
