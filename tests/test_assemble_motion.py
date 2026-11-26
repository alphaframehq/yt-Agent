"""Motion tests (Phase 2, 26 Nov 2026): Ken Burns, chart fade-in,
scene transitions, burned-in subtitles, AI chart data, hook check.

MoviePy is NOT installed in this environment (and must never be
required for a deterministic test), so a fake ``moviepy.editor``
module is injected into ``sys.modules`` before each test that renders.
The fake records every clip call, so tests assert on the motion that
was actually applied — resize-with-time (Ken Burns), fadein/fadeout
(dip transitions + chart fade-in) — not on a status string. Real
matplotlib IS used for the chart-rendering tests (it is installed).
"""

import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Add yt-agent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._sheets_fake import install_fake_sheets

_FAKE_SHEETS = None
_SHEETS_PATCHER = None


def setUpModule():
    global _FAKE_SHEETS, _SHEETS_PATCHER
    _FAKE_SHEETS, _SHEETS_PATCHER = install_fake_sheets()
    _install_fake_moviepy()


def tearDownModule():
    _remove_fake_moviepy()
    if _SHEETS_PATCHER is not None:
        _SHEETS_PATCHER.stop()


# ---------------------------------------------------------------------------
# Fake MoviePy (records calls; renders nothing)
# ---------------------------------------------------------------------------
class _FakeClip:
    """Minimal stand-in for a MoviePy clip; records method calls."""

    registry = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.args = args
        self.kwargs = kwargs
        self.duration = None
        self.audio = None
        type(self).registry.append(self)

    def set_duration(self, d):
        self.calls.append(("set_duration", d))
        self.duration = d
        return self

    def resize(self, fn):
        self.calls.append(("resize", fn))
        return self

    def set_position(self, pos):
        self.calls.append(("set_position", pos))
        return self

    def fadein(self, t):
        self.calls.append(("fadein", t))
        return self

    def fadeout(self, t):
        self.calls.append(("fadeout", t))
        return self

    def set_audio(self, a):
        self.calls.append(("set_audio", a))
        self.audio = a
        return self

    def volumex(self, v):
        self.calls.append(("volumex", v))
        return self

    def subclip(self, a, b):
        self.calls.append(("subclip", a, b))
        return self

    def write_videofile(self, path, **kwargs):
        self.calls.append(("write_videofile", path))
        Path(path).write_bytes(b"x" * 2048)


class _FakeImageClip(_FakeClip):
    pass


class _FakeVideoClip(_FakeClip):
    pass


class _FakeAudioClip(_FakeClip):
    pass


class _FakeCompositeClip(_FakeClip):
    pass


def _fake_concatenate(clips, method="compose"):
    final = _FakeClip()
    final.calls.append(("concatenate", len(clips), method))
    final.audio = _FakeAudioClip()
    final.duration = sum(c.duration or 0 for c in clips)
    return final


def _install_fake_moviepy():
    editor = types.ModuleType("moviepy.editor")
    editor.ImageClip = _FakeImageClip
    editor.VideoFileClip = _FakeVideoClip
    editor.AudioFileClip = _FakeAudioClip
    editor.CompositeVideoClip = _FakeCompositeClip
    editor.CompositeAudioClip = _FakeAudioClip
    editor.concatenate_videoclips = _fake_concatenate
    parent = types.ModuleType("moviepy")
    parent.editor = editor
    sys.modules["moviepy"] = parent
    sys.modules["moviepy.editor"] = editor


def _remove_fake_moviepy():
    sys.modules.pop("moviepy", None)
    sys.modules.pop("moviepy.editor", None)


def _calls_of(clip, name):
    return [c for c in clip.calls if c[0] == name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from tests._fixtures import make_test_bundle, make_test_script  # noqa: E402


def _make_ai_bundle():
    """A ResearchBundle for an ai_everything topic: zeroed financials,
    real retrieved-article sources with distinct publishers."""
    from models.research import (
        EntityInfo,
        FinancialData,
        MarginData,
        MarketData,
        ResearchBundle,
        SourceInfo,
        YTInsights,
    )

    sources = [
        SourceInfo(
            claim_id="context_0", claim="Tool X launches",
            source="TechCrunch", url="https://example.com/tc",
            snippet="Tool X launches today.", confidence=0.9,
        ),
        SourceInfo(
            claim_id="context_1", claim="Tool X pricing",
            source="TechCrunch", url="https://example.com/tc2",
            snippet="Tool X costs $20.", confidence=0.9,
        ),
        SourceInfo(
            claim_id="context_2", claim="Tool X review",
            source="The Verge", url="https://example.com/v",
            snippet="Tool X reviewed.", confidence=0.9,
        ),
    ]
    return ResearchBundle(
        topic="Tool X launches",
        pillar="ai_everything",
        entities=[EntityInfo(name="Tool X", type="Topic", facts=[])],
        financials=FinancialData(
            revenue_by_year={},
            segments={},
            margins=MarginData(gross=0.0, operating=0.0, net=0.0),
            growth_rate=0.0,
            unit_economics={},
        ),
        technical_context="Tool X is a new AI tool.",
        competitors=[],
        market_data=MarketData(tam_usd=0.0, penetration_pct=0.0),
        sources=sources,
        yt_insights=YTInsights(
            top_titles=["Tool X first look", "Tool X vs rivals"],
            common_tags=[],
            gaps=[],
        ),
        research_confidence=0.8,
    )


class TestKenBurns(unittest.TestCase):
    def setUp(self):
        _FakeClip.registry.clear()

    def test_zoom_in_variant_scales_up_over_time(self):
        from agents.assemble import _ken_burns_clip

        clip = _ken_burns_clip("/tmp/does-not-matter.png", 10.0, 1920, 1080, variant=0)
        image_clip = next(c for c in _FakeClip.registry if isinstance(c, _FakeImageClip))
        (resize_fn,) = [c[1] for c in image_clip.calls if c[0] == "resize"]
        self.assertAlmostEqual(resize_fn(0.0), 1.0)
        self.assertAlmostEqual(resize_fn(10.0), 1.12)
        # Composite crops to the canvas size.
        composite = next(c for c in _FakeClip.registry if isinstance(c, _FakeCompositeClip))
        self.assertEqual(composite.kwargs.get("size"), (1920, 1080))
        # Position is a time-varying pan within the canvas.
        (pos_fn,) = [c[1] for c in image_clip.calls if c[0] == "set_position"]
        x0, y0 = pos_fn(0.0)
        x1, y1 = pos_fn(10.0)
        self.assertNotEqual((x0, y0), (x1, y1))
        self.assertLessEqual(x0, 0)
        self.assertLessEqual(x1, 0)
        self.assertEqual(clip.duration, 10.0)

    def test_zoom_out_variant_scales_down_over_time(self):
        from agents.assemble import _ken_burns_clip

        _ken_burns_clip("/tmp/does-not-matter.png", 8.0, 1920, 1080, variant=1)
        image_clip = next(c for c in _FakeClip.registry if isinstance(c, _FakeImageClip))
        (resize_fn,) = [c[1] for c in image_clip.calls if c[0] == "resize"]
        self.assertAlmostEqual(resize_fn(0.0), 1.12)
        self.assertAlmostEqual(resize_fn(8.0), 1.0)

    def test_variants_are_deterministic(self):
        """Same variant + duration always yields the same scale curve."""
        from agents.assemble import _ken_burns_clip

        _ken_burns_clip("/tmp/a.png", 6.0, 1920, 1080, variant=2)
        fn_a = next(
            c[1] for c in _FakeClip.registry if isinstance(c, _FakeImageClip)
            for c in c.calls if c[0] == "resize"
        )
        _FakeClip.registry.clear()
        _ken_burns_clip("/tmp/a.png", 6.0, 1920, 1080, variant=2)
        fn_b = next(
            c[1] for c in _FakeClip.registry if isinstance(c, _FakeImageClip)
            for c in c.calls if c[0] == "resize"
        )
        for t in (0.0, 3.0, 6.0):
            self.assertEqual(fn_a(t), fn_b(t))

    def test_transition_sec_is_capped_for_short_scenes(self):
        from agents.assemble import _transition_sec

        self.assertEqual(_transition_sec(10.0), 0.5)
        self.assertEqual(_transition_sec(1.0), 0.25)
        self.assertEqual(_transition_sec(0.2), 0.1)


class TestRenderCompositionMotion(unittest.TestCase):
    def setUp(self):
        _FakeClip.registry.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.img = Path(self.tmp.name) / "scene.png"
        self.img.write_bytes(b"fake-png")
        self.vid = Path(self.tmp.name) / "scene.mp4"
        self.vid.write_bytes(b"fake-mp4")
        self.srt = Path(self.tmp.name) / "subtitles.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:00:05,000\nHello\n", encoding="utf-8")
        self.dest = Path(self.tmp.name) / "render.mp4"

    def tearDown(self):
        self.tmp.cleanup()

    def _render(self, visuals, durations=(5.0, 5.0)):
        from agents.assemble import VideoAssembler

        assembler = VideoAssembler(is_shorts=False)
        audio = [
            {"duration_sec": d, "audio_path": "/tmp/a.wav"} for d in durations
        ]
        with mock.patch("agents.assemble.shutil.which", return_value=None):
            out = assembler._render_composition(
                self.dest, audio, visuals, None, self.srt
            )
        return assembler, out

    def test_image_scenes_get_ken_burns_and_transitions(self):
        visuals = [
            {"asset_path": str(self.img), "media_type": "image", "visual_type": "ai_image"},
            {"asset_path": str(self.img), "media_type": "image", "visual_type": "stock"},
        ]
        assembler, out = self._render(visuals)
        self.assertEqual(out, str(self.dest.resolve()))
        self.assertTrue(self.dest.exists())
        # Every still got a time-varying resize (Ken Burns) …
        image_clips = [c for c in _FakeClip.registry if isinstance(c, _FakeImageClip)]
        self.assertEqual(len(image_clips), 2)
        for clip in image_clips:
            self.assertTrue(_calls_of(clip, "resize"))
            fn = _calls_of(clip, "resize")[0][1]
            self.assertTrue(callable(fn))
        # … and every scene clip got a dip transition (fadein + fadeout).
        composites = [c for c in _FakeClip.registry if isinstance(c, _FakeCompositeClip)]
        self.assertEqual(len(composites), 2)
        for clip in composites:
            self.assertTrue(_calls_of(clip, "fadein"))
            self.assertTrue(_calls_of(clip, "fadeout"))
        # Subtitle burn was skipped (no ffmpeg in this test) and flagged.
        self.assertFalse(assembler.subtitles_burned)

    def test_chart_scenes_fade_in_over_0_8s(self):
        from agents.assemble import CHART_FADEIN_SEC

        visuals = [
            {"asset_path": str(self.img), "media_type": "image", "visual_type": "chart"},
        ]
        self._render(visuals, durations=(5.0,))
        composites = [c for c in _FakeClip.registry if isinstance(c, _FakeCompositeClip)]
        self.assertEqual(len(composites), 1)
        fadeins = _calls_of(composites[0], "fadein")
        self.assertEqual(len(fadeins), 1)
        self.assertAlmostEqual(fadeins[0][1], CHART_FADEIN_SEC)

    def test_video_scenes_get_transitions_but_no_ken_burns(self):
        visuals = [
            {"asset_path": str(self.vid), "media_type": "video", "visual_type": "stock"},
        ]
        self._render(visuals, durations=(5.0,))
        video_clips = [c for c in _FakeClip.registry if isinstance(c, _FakeVideoClip)]
        self.assertEqual(len(video_clips), 1)
        self.assertFalse(_calls_of(video_clips[0], "resize"))
        self.assertTrue(_calls_of(video_clips[0], "fadein"))
        self.assertTrue(_calls_of(video_clips[0], "fadeout"))


class TestSubtitleBurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "render.mp4"
        self.video.write_bytes(b"v" * 2048)
        self.srt = Path(self.tmp.name) / "subtitles.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_ffmpeg_success(self, cmd, **kwargs):
        # Last arg is the temp output path: write a real file there.
        Path(cmd[-1]).write_bytes(b"s" * 2048)
        return mock.Mock(returncode=0, stderr="")

    def test_burn_success_replaces_video_and_returns_true(self):
        from agents.assemble import _burn_subtitles

        with (
            mock.patch("agents.assemble.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("agents.assemble.subprocess.run", side_effect=self._fake_ffmpeg_success),
        ):
            self.assertTrue(_burn_subtitles(self.video, self.srt))
        self.assertEqual(self.video.read_bytes(), b"s" * 2048)

    def test_burn_passes_subtitles_filter(self):
        from agents.assemble import _burn_subtitles

        seen = {}

        def _capture(cmd, **kwargs):
            seen["cmd"] = cmd
            return self._fake_ffmpeg_success(cmd, **kwargs)

        with (
            mock.patch("agents.assemble.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("agents.assemble.subprocess.run", side_effect=_capture),
        ):
            _burn_subtitles(self.video, self.srt)
        vf_idx = seen["cmd"].index("-vf")
        self.assertIn("subtitles=", seen["cmd"][vf_idx + 1])
        self.assertIn("force_style=", seen["cmd"][vf_idx + 1])

    def test_burn_failure_keeps_original_and_returns_false(self):
        from agents.assemble import _burn_subtitles

        with (
            mock.patch("agents.assemble.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch(
                "agents.assemble.subprocess.run",
                return_value=mock.Mock(returncode=1, stderr="libass error"),
            ),
        ):
            self.assertFalse(_burn_subtitles(self.video, self.srt))
        self.assertEqual(self.video.read_bytes(), b"v" * 2048)

    def test_burn_skipped_without_ffmpeg(self):
        from agents.assemble import _burn_subtitles

        with mock.patch("agents.assemble.shutil.which", return_value=None):
            self.assertFalse(_burn_subtitles(self.video, self.srt))
        self.assertEqual(self.video.read_bytes(), b"v" * 2048)

    def test_burn_skipped_without_srt(self):
        from agents.assemble import _burn_subtitles

        missing_srt = Path(self.tmp.name) / "nope.srt"
        with mock.patch("agents.assemble.shutil.which", return_value="/usr/bin/ffmpeg"):
            self.assertFalse(_burn_subtitles(self.video, missing_srt))
        self.assertEqual(self.video.read_bytes(), b"v" * 2048)


class TestAiCharts(unittest.TestCase):
    def test_ai_topic_chart_data_counts_real_sources(self):
        from agents.visuals import ai_topic_chart_data

        data = ai_topic_chart_data(_make_ai_bundle())
        self.assertEqual(data["topic"], "Tool X launches")
        self.assertEqual(data["source_labels"], ["TechCrunch", "The Verge"])
        self.assertEqual(data["source_counts"], [2, 1])

    def test_ai_topic_chart_data_raises_without_sources(self):
        from agents.visuals import ai_topic_chart_data

        bundle = _make_ai_bundle()
        bundle.sources = []
        with self.assertRaises(ValueError):
            ai_topic_chart_data(bundle)

    def test_research_bundle_to_chart_data_branches_to_ai_data(self):
        from agents.visuals import research_bundle_to_chart_data

        data = research_bundle_to_chart_data(_make_ai_bundle())
        self.assertIn("source_labels", data)
        self.assertNotIn("years", data)

    def test_finance_bundle_still_returns_finance_data(self):
        from agents.visuals import research_bundle_to_chart_data

        data = research_bundle_to_chart_data(make_test_bundle())
        self.assertIn("years", data)
        self.assertNotIn("source_labels", data)

    def test_ai_source_coverage_renders_real_png(self):
        from agents.charts import generate_chart_by_type

        data = {
            "topic": "Tool X launches",
            "source_labels": ["TechCrunch", "The Verge"],
            "source_counts": [2, 1],
        }
        path = generate_chart_by_type("ai_source_coverage", data=data)
        self.assertTrue(Path(path).exists())
        self.assertGreater(Path(path).stat().st_size, 500)

    def test_ai_source_share_renders_real_png(self):
        from agents.charts import generate_chart_by_type

        data = {
            "topic": "Tool X launches",
            "source_labels": ["TechCrunch", "The Verge"],
            "source_counts": [2, 1],
        }
        path = generate_chart_by_type("ai_source_share", data=data)
        self.assertTrue(Path(path).exists())
        self.assertGreater(Path(path).stat().st_size, 500)

    def test_finance_chart_types_raise_for_ai_data(self):
        from agents.charts import generate_chart_by_type

        data = {
            "topic": "Tool X launches",
            "source_labels": ["TechCrunch"],
            "source_counts": [3],
        }
        for c_type in (
            "revenue_trend", "segment_breakdown", "margin_compare",
            "unit_economics_waterfall", "scope_tam", "competitor_matrix",
            "growth_margin_scatter", "market_share_donut",
        ):
            with self.assertRaises(ValueError, msg=f"chart type: {c_type}"):
                generate_chart_by_type(c_type, data=data)

    def test_visuals_guard_rejects_finance_chart_for_ai_topic(self):
        from agents.visuals import VisualPipeline, research_bundle_to_chart_data
        from models.script import SceneInfo

        data = research_bundle_to_chart_data(_make_ai_bundle())
        scene = SceneInfo(
            id=1, section="what_changed",
            narration="Tool X launched today.",
            visual_prompt="Tool X coverage chart",
            visual_type="chart", chart_type="revenue_trend",
            duration_sec=30, citations=[],
        )
        with self.assertRaises(ValueError) as ctx:
            VisualPipeline().resolve_scene(scene, data=data)
        self.assertIn("ai_source_coverage", str(ctx.exception))

    def test_visuals_default_chart_for_ai_topic_is_ai_chart(self):
        from agents.visuals import VisualPipeline, research_bundle_to_chart_data
        from models.script import SceneInfo

        data = research_bundle_to_chart_data(_make_ai_bundle())
        scene = SceneInfo(
            id=1, section="what_changed",
            narration="Tool X launched today.",
            visual_prompt="Tool X coverage chart",
            visual_type="chart", chart_type=None,
            duration_sec=30, citations=[],
        )
        asset = VisualPipeline().resolve_scene(scene, data=data)
        self.assertEqual(asset["visual_type"], "chart")
        self.assertTrue(Path(asset["asset_path"]).exists())

    def test_chart_to_clip_applies_ken_burns_and_fadein(self):
        from agents.charts import chart_to_clip, generate_chart_by_type

        png = generate_chart_by_type(
            "ai_source_coverage",
            data={"topic": "T", "source_labels": ["A"], "source_counts": [1]},
        )
        _FakeClip.registry.clear()
        clip = chart_to_clip(png, 5.0)
        self.assertIsInstance(clip, _FakeCompositeClip)
        self.assertTrue(_calls_of(clip, "fadein"))


class TestHookStrength(unittest.TestCase):
    def setUp(self):
        # Bypass VerificationAgent.__init__ (config/Sheets): the hook
        # check is deterministic and uses no instance state.
        from agents.verify import VerificationAgent

        self.agent = VerificationAgent.__new__(VerificationAgent)
        self.bundle = make_test_bundle()
        self.script = make_test_script(self.bundle)

    def test_valid_fixture_hook_passes(self):
        check, _fixes = self.agent._check_hook_strength(self.script)
        self.assertEqual(check.status, "pass")
        self.assertEqual(check.issues, [])

    def test_generic_opener_is_major(self):
        script = copy.deepcopy(self.script)
        script.scenes[0].narration = (
            "In this video we will explore how TestCo makes money "
            "and why its payment network matters for everyone."
        )
        check, _fixes = self.agent._check_hook_strength(script)
        self.assertEqual(check.status, "fail")
        self.assertEqual(len(check.issues), 1)
        self.assertEqual(check.issues[0].severity, "major")
        self.assertIn("Generic hook opening", check.issues[0].claim)

    def test_welcome_opener_is_major(self):
        script = copy.deepcopy(self.script)
        script.scenes[0].narration = (
            "Welcome back to the channel. Today we look at TestCo revenue "
            "of $1.2 billion in 2024 and what it means."
        )
        check, _fixes = self.agent._check_hook_strength(script)
        self.assertEqual(check.status, "fail")
        self.assertTrue(any(i.severity == "major" for i in check.issues))

    def test_thin_hook_is_major(self):
        script = copy.deepcopy(self.script)
        script.scenes[0].narration = "TestCo makes money."
        check, _fixes = self.agent._check_hook_strength(script)
        self.assertEqual(check.status, "fail")
        self.assertTrue(any("too thin" in i.issue for i in check.issues))

    def test_hook_not_first_is_major(self):
        script = copy.deepcopy(self.script)
        scenes = list(script.scenes)
        scenes[0], scenes[1] = scenes[1], scenes[0]
        script.scenes = scenes
        check, _fixes = self.agent._check_hook_strength(script)
        self.assertEqual(check.status, "fail")
        self.assertTrue(any("not the first scene" in i.issue for i in check.issues))

    def test_hook_check_runs_inside_full_verification(self):
        """A generic hook fails the whole run (major blocks LLM stage)."""
        from agents.verify import VerificationAgent

        agent = VerificationAgent.__new__(VerificationAgent)
        agent.model = "test-model"  # run() reports it; __init__ skipped
        script = copy.deepcopy(self.script)
        script.scenes[0].narration = (
            "In this video we will explore how TestCo makes money."
        )
        # Deterministic majors already make overall == "fail", so the
        # LLM stage is skipped and no LLM mock is needed.
        report = agent.run(self.bundle, script)
        self.assertEqual(report.overall, "fail")
        hook_checks = [c for c in report.checks if c.name == "HOOK_STRENGTH"]
        self.assertEqual(len(hook_checks), 1)
        self.assertEqual(hook_checks[0].status, "fail")


class TestHookPrompt(unittest.TestCase):
    def test_ai_prompt_requires_concrete_hook(self):
        prompt = Path("prompts/script_ai_everything.txt").read_text(encoding="utf-8")
        self.assertIn("first 3 seconds", prompt)
        self.assertIn("In this video", prompt)
        self.assertIn("bundle-grounded", prompt)
        self.assertIn("ai_source_coverage", prompt)
        self.assertIn("ai_source_share", prompt)

    def test_script_agent_wires_hook_rules(self):
        import inspect

        from agents import script as script_module

        run_src = inspect.getsource(script_module.ScriptAgent.run)
        self.assertIn("HOOK (first 3 seconds)", run_src)
        shorts_src = inspect.getsource(script_module.ScriptAgent.generate_shorts)
        self.assertIn("HOOK (first 1 second)", shorts_src)


if __name__ == "__main__":
    unittest.main()
