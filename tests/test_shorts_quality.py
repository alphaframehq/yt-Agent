"""Shorts quality fixes (27 Nov 2026, Dev direction change): no
narration subtitles on Shorts — visuals + stats on screen instead.

Covers: stat extraction per scene, Pixabay keyword queries +
vertical orientation, hook text, and the 9:16 programmatic card
with a real TTF (no bitmap font).
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._sheets_fake import install_fake_sheets

try:
    import moviepy  # noqa: F401 — only a capability probe

    HAS_MOVIEPY = True
except Exception:  # noqa: BLE001 — moviepy is optional in unit tests
    HAS_MOVIEPY = False

_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()


def tearDownModule():
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()


class TestExtractStat(unittest.TestCase):
    def test_billion_dollar_figure(self):
        from agents.assemble import _extract_stat

        big, label = _extract_stat(
            "Anthropic just secured over 81 billion dollars in the "
            "largest offering on record, officially making it the "
            "biggest AI IPO in history."
        )
        self.assertEqual(big, "$81B")
        self.assertEqual(label, "Biggest AI IPO History")

    def test_millions_word(self):
        from agents.assemble import _extract_stat

        big, label = _extract_stat(
            "Investors rushed in because Anthropic's Claude models now "
            "power tools used by millions of people every single day."
        )
        self.assertEqual(big, "Millions")
        self.assertEqual(label, "People Every Single Day")

    def test_no_figure_falls_back_to_key_words(self):
        from agents.assemble import _extract_stat

        big, label = _extract_stat(
            "But the big question remains: can Anthropic turn this "
            "massive valuation into real profit before the hype cools down?"
        )
        self.assertEqual(big, "BIG QUESTION")
        self.assertEqual(label, "Anthropic Turn Massive Valuation")

    def test_percent_figure(self):
        from agents.assemble import _extract_stat

        big, label = _extract_stat(
            "Revenue grew 42% year over year as enterprise customers signed up."
        )
        self.assertEqual(big, "42%")
        self.assertTrue(label)

    def test_empty_narration_never_returns_empty(self):
        from agents.assemble import _extract_stat

        big, label = _extract_stat("")
        self.assertTrue(big)
        self.assertTrue(label)


class TestStockQuery(unittest.TestCase):
    def test_keyword_query_from_prose_prompt(self):
        from agents.visuals import _stock_query

        self.assertEqual(
            _stock_query("New York stock exchange trading floor investors"),
            "new york stock exchange",
        )
        self.assertEqual(
            _stock_query("A cinematic wide shot of the New York stock exchange"),
            "new york stock exchange",
        )
        self.assertEqual(
            _stock_query("artificial intelligence data center servers technology"),
            "artificial intelligence data center",
        )

    def test_empty_prompt_falls_back(self):
        from agents.visuals import _stock_query

        self.assertTrue(_stock_query(""))


class TestHookText(unittest.TestCase):
    def test_seo_title_shortened(self):
        from agents.assemble import _hook_text
        from models.script import SceneInfo, ScriptMeta, SEOData, ShortsScript

        script = ShortsScript(
            pillar="ai_everything",
            scenes=[
                SceneInfo(
                    id=1, section="s1",
                    narration="Anthropic just secured over 81 billion dollars.",
                    visual_prompt="stock exchange", visual_type="stock",
                    duration_sec=40,
                )
            ],
            seo=SEOData(
                title="Anthropic IPO: $81 Billion - AI History's Biggest IPO",
                description="x " * 160,
                tags=["Anthropic", "IPO", "AI", "Claude", "finance"],
            ),
            meta=ScriptMeta(word_count=80, estimated_duration_sec=40),
        )
        self.assertEqual(_hook_text(script), "Anthropic IPO: $81 Billion")

    def test_falls_back_to_first_scene_words(self):
        from agents.assemble import _hook_text
        from models.script import SceneInfo, ScriptMeta, SEOData, ShortsScript

        script = ShortsScript(
            pillar="ai_everything",
            scenes=[
                SceneInfo(
                    id=1, section="s1",
                    narration="Anthropic just secured over 81 billion dollars.",
                    visual_prompt="stock exchange", visual_type="stock",
                    duration_sec=40,
                )
            ],
            seo=SEOData(title="", description="x " * 160, tags=["Anthropic", "IPO", "AI", "Claude", "finance"]),
            meta=ScriptMeta(word_count=80, estimated_duration_sec=40),
        )
        self.assertEqual(
            _hook_text(script), "Anthropic just secured over 81"
        )


class TestPixabayOrientation(unittest.TestCase):
    def _fake_client(self, captured):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"hits": []}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params=None):
                captured["url"] = url
                captured["params"] = dict(params or {})
                return FakeResp()

        return FakeClient

    def test_image_search_passes_vertical(self):
        from tools import pixabay

        captured = {}
        with mock.patch("tools.pixabay._get_secret_optional", return_value="key"), mock.patch(
            "tools.pixabay.httpx.Client", self._fake_client(captured)
        ):
            self.assertEqual(
                pixabay.search_stock_images("stock exchange", orientation="vertical"),
                [],
            )
        self.assertEqual(captured["params"].get("orientation"), "vertical")
        self.assertEqual(captured["params"].get("q"), "stock exchange")

    def test_video_search_passes_vertical(self):
        from tools import pixabay

        captured = {}
        with mock.patch("tools.pixabay.get_secret", return_value="key"), mock.patch(
            "tools.pixabay.httpx.Client", self._fake_client(captured)
        ):
            self.assertEqual(
                pixabay.search_stock_videos("stock exchange", orientation="vertical"),
                [],
            )
        self.assertEqual(captured["params"].get("orientation"), "vertical")


class TestProgrammaticCardTTF(unittest.TestCase):
    def test_9x16_card_is_full_size_with_real_text(self):
        """F3: the 9:16 card is 1080x1920 and the title is drawn in
        a real TTF — assert actual bright text pixels exist in the
        title band (the old bitmap font left ~10px text)."""
        from PIL import Image

        from tools.image_gen import generate_programmatic_visual

        dest = Path("cache/images/test_prog_9x16_ttf.png")
        try:
            out = generate_programmatic_visual(
                "Anthropic IPO eighty one billion", dest, aspect_ratio="9:16"
            )
            with Image.open(out) as img:
                self.assertEqual(img.size, (1080, 1920))
                gray = img.convert("L")
                # Title band: upper third, excluding the border.
                band = gray.crop((100, 500, 980, 800))
                bright = sum(1 for px in band.getdata() if px > 180)
                self.assertGreater(
                    bright, 500,
                    "no readable title text found on the 9:16 card",
                )
        finally:
            if dest.exists():
                dest.unlink()


class TestKenBurnsNoBottomStrip(unittest.TestCase):
    """F2 regression (27 Nov 2026, found in the v2 sample): the
    Ken Burns clip composited with a stat card grew a black strip
    at the bottom of the frame on zoom-in variants (6 rows at
    t=0.5s -> 108 rows at t=8.5s in the sample).

    Root cause: the inner CompositeVideoClip was transparent, so
    MoviePy 1.0.3 built a mask for it from add_mask() — a static
    all-ones array of the clip's .size at t=0 — while the frame
    keeps GROWING with the time-varying zoom. The frozen mask
    ended up shorter than the frame and, because the pan centres
    the growing image by moving it up, the bottom rows were left
    uncovered (black) wherever an outer transparent composite
    applied the mask. The fix is an opaque bg_color on the inner
    composite, so the clip carries no mask at all.
    """

    @unittest.skipUnless(HAS_MOVIEPY, "moviepy not installed")
    def test_stat_composite_stays_full_frame(self):
        import tempfile

        import PIL.Image

        # Pillow >=10 removed Image.ANTIALIAS, which moviepy 1.0.3
        # still references (same shim the sample drivers use).
        if not hasattr(PIL.Image, "ANTIALIAS"):
            PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

        from moviepy.editor import CompositeVideoClip

        from agents.assemble import (
            _extract_stat,
            _ken_burns_clip,
            _stat_card_clip,
        )

        with tempfile.TemporaryDirectory() as tmp:
            img_path = os.path.join(tmp, "still.jpg")
            PIL.Image.new("RGB", (1280, 720), (200, 30, 30)).save(img_path)
            big, label = _extract_stat(
                "Anthropic just secured over 81 billion dollars in the "
                "largest offering on record."
            )
            for variant in (0, 1):
                kb = _ken_burns_clip(img_path, 9.0, 1080, 1920, variant=variant)
                self.assertIsNone(
                    kb.mask,
                    f"variant {variant}: Ken Burns clip must carry no "
                    "mask (opaque composite) or the strip comes back",
                )
                stat = _stat_card_clip(big, label, 9.0, 1080, 1920)
                scene = CompositeVideoClip(
                    [kb.fadein(0.5).fadeout(0.5), stat],
                    size=(1080, 1920),
                ).set_duration(9.0)
                for t in (0.5, 4.5, 8.5):
                    frame = scene.get_frame(t)
                    bottom = frame[-120:].mean()
                    top = frame[:120].mean()
                    self.assertGreater(
                        bottom, 50,
                        f"variant {variant} t={t}s: bottom of frame is "
                        f"black (mean {bottom:.1f}) — the strip is back",
                    )
                    self.assertGreater(top, 50, f"variant {variant} t={t}s: top black")


if __name__ == "__main__":
    unittest.main()
