"""TubePilot AI — Kokoro TTS (local ONNX fallback) unit tests.

27 Nov 2026 (run #22): both Gemini keys hit HTTP 429 together, so
Kokoro (tools/kokoro_tts.py) is the fallback when every Gemini key
fails. All Kokoro engine calls are mocked (no model download, no
network): tools.kokoro_tts._get_kokoro is replaced with a fake whose
create() returns numpy float32 samples. The one real-synthesis check
is a local smoke test (see KOKORO-TTS-NOTES.md), not this file.

Covers:
1. Success writes a real WAV (mono 16-bit 24 kHz); duration is
   measured from frames.
2. Voice mapping: Joanna/Kore -> af_heart, Matthew/Charon -> am_adam;
   a Kokoro voice id passes through unchanged.
3. Cache hit does not call the engine a second time.
4. Raises (fail-loud) on empty text, on engine failure, on missing
   kokoro-onnx package, and on model-download failure — never stub
   audio.
5. agents/tts.py wiring: Gemini failure falls back to Kokoro (and
   Gemini success never calls Kokoro); Kokoro failure still raises
   the ORIGINAL Gemini error when no Polly keys exist.
"""

import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

import tools.kokoro_tts as kokoro_tts
from tools.kokoro_tts import resolve_voice, synthesize_speech


def _fake_samples(seconds: float, rate: int = 24000) -> np.ndarray:
    """Deterministic float32 samples (a quiet sine, test fixture only)."""
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float32) / rate
    return (0.1 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


class _FakeKokoro:
    """Stand-in for kokoro_onnx.Kokoro; records create() calls."""

    def __init__(self, seconds: float = 2.0, fail_with=None):
        self.seconds = seconds
        self.fail_with = fail_with
        self.calls = []

    def create(self, text, voice="af_heart", speed=1.0, lang="en-us"):
        self.calls.append(
            {"text": text, "voice": voice, "speed": speed, "lang": lang}
        )
        if self.fail_with is not None:
            raise self.fail_with
        return _fake_samples(self.seconds), 24000


class TestKokoroTTS(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._tmp.name) / "audio"
        self._cache_dir.mkdir(parents=True)
        self._model_dir = Path(self._tmp.name) / "kokoro"
        self._model_dir.mkdir(parents=True)
        self._patch_cache = patch.object(kokoro_tts, "CACHE_DIR", self._cache_dir)
        self._patch_cache.start()
        self._patch_model_dir = patch.object(
            kokoro_tts, "MODEL_DIR", self._model_dir
        )
        self._patch_model_dir.start()
        self.addCleanup(patch.stopall)
        kokoro_tts._kokoro_instance = None

    def tearDown(self):
        self._tmp.cleanup()
        kokoro_tts._kokoro_instance = None

    def _run_with(self, fake, text="Hello world, this is a test."):
        with patch.object(kokoro_tts, "_get_kokoro", return_value=fake):
            return synthesize_speech(text), fake

    def test_success_writes_real_wav_with_measured_duration(self):
        """2.0s of Kokoro samples becomes a WAV measured at 2.0s."""
        (path, duration), fake = self._run_with(
            _FakeKokoro(seconds=2.0), text="Hello world."
        )
        self.assertEqual(duration, 2.0)
        self.assertTrue(Path(path).suffix == ".wav")
        self.assertTrue(Path(path).stat().st_size > 0)
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 24000)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["voice"], "af_heart")
        self.assertEqual(fake.calls[0]["lang"], "en-us")

    def test_voice_mapping(self):
        self.assertEqual(resolve_voice("Joanna"), "af_heart")
        self.assertEqual(resolve_voice("Kore"), "af_heart")
        self.assertEqual(resolve_voice("Kajal"), "af_heart")
        self.assertEqual(resolve_voice("Matthew"), "am_adam")
        self.assertEqual(resolve_voice("Charon"), "am_adam")
        # A Kokoro voice id passes through unchanged.
        self.assertEqual(resolve_voice("af_sarah"), "af_sarah")
        self.assertEqual(resolve_voice("am_adam"), "am_adam")
        # Unknown names fall back to the default voice, not an error.
        self.assertEqual(resolve_voice("SomeoneElse"), "af_heart")

    def test_cache_hit_does_not_call_engine_again(self):
        fake = _FakeKokoro(seconds=1.5)
        (path1, duration1), _ = self._run_with(fake, text="Cached narration.")
        self.assertEqual(duration1, 1.5)
        self.assertEqual(len(fake.calls), 1)
        (path2, duration2), _ = self._run_with(fake, text="Cached narration.")
        self.assertEqual(duration2, 1.5)
        self.assertEqual(path2, path1)
        self.assertEqual(len(fake.calls), 1)

    def test_raises_on_empty_text(self):
        with self.assertRaises(ValueError):
            synthesize_speech("   ")

    def test_raises_when_engine_fails(self):
        fake = _FakeKokoro(fail_with=RuntimeError("onnx session boom"))
        with self.assertRaises(RuntimeError) as ctx:
            self._run_with(fake, text="Engine failure test.")
        self.assertIn("Kokoro TTS synthesis failed", str(ctx.exception))

    def test_raises_when_package_missing(self):
        """kokoro-onnx not installed -> a clear error, not an ImportError
        leaking out of the fallback path."""
        kokoro_tts._kokoro_instance = None
        with patch.dict(sys.modules, {"kokoro_onnx": None}):
            with self.assertRaises(RuntimeError) as ctx:
                synthesize_speech("Package missing test.")
        self.assertIn("kokoro-onnx", str(ctx.exception))

    def test_raises_when_model_download_fails(self):
        """Model files absent + download failing -> loud error, never
        silence. The real _download retry loop runs (urlretrieve
        mocked to fail) and raises its wrapped error."""
        with patch("urllib.request.urlretrieve",
                   side_effect=OSError("network unreachable")):
            with self.assertRaises(RuntimeError) as ctx:
                synthesize_speech("Download failure test.")
        self.assertIn("Could not download Kokoro model file", str(ctx.exception))

    def test_download_skipped_when_model_cached(self):
        """Model files already present -> _download is never called."""
        (self._model_dir / "kokoro-v1.0.onnx").write_bytes(b"x" * 60_000_000)
        (self._model_dir / "voices-v1.0.bin").write_bytes(b"y" * 2_000_000)
        with patch.object(kokoro_tts, "_download") as mock_dl:
            model_path, voices_path = kokoro_tts._ensure_model_files()
        mock_dl.assert_not_called()
        self.assertEqual(model_path, self._model_dir / "kokoro-v1.0.onnx")
        self.assertEqual(voices_path, self._model_dir / "voices-v1.0.bin")


class TestKokoroFallbackWiring(unittest.TestCase):
    """agents/tts.py: Gemini -> Kokoro -> Polly -> original error."""

    def setUp(self):
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "TTS_PROVIDER"):
            os.environ.pop(name, None)

    def tearDown(self):
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "TTS_PROVIDER"):
            os.environ.pop(name, None)

    def test_kokoro_fallback_on_gemini_failure(self):
        """Gemini 429 on all keys -> Kokoro audio is returned; Polly
        is never called (no AWS keys)."""
        from agents.tts import synthesize_scene_audio

        gemini_err = RuntimeError(
            "All Gemini API keys in pool failed for TTS."
        )
        with patch("agents.tts.synthesize_speech_gemini",
                   side_effect=gemini_err), \
             patch("agents.tts.synthesize_speech_kokoro",
                   return_value=("/tmp/kokoro.wav", 4.0)) as mock_kokoro, \
             patch("agents.tts.synthesize_speech_polly") as mock_polly:
            path, duration = synthesize_scene_audio("Hello.", voice="Joanna")
        self.assertEqual((path, duration), ("/tmp/kokoro.wav", 4.0))
        mock_kokoro.assert_called_once()
        mock_polly.assert_not_called()

    def test_gemini_success_never_calls_kokoro(self):
        """Gemini success -> Kokoro is not touched (primary stays primary)."""
        from agents.tts import synthesize_scene_audio

        with patch("agents.tts.synthesize_speech_gemini",
                   return_value=("/tmp/gemini.wav", 3.5)), \
             patch("agents.tts.synthesize_speech_kokoro") as mock_kokoro, \
             patch("agents.tts.synthesize_speech_polly") as mock_polly:
            path, duration = synthesize_scene_audio("Hello.", voice="Joanna")
        self.assertEqual((path, duration), ("/tmp/gemini.wav", 3.5))
        mock_kokoro.assert_not_called()
        mock_polly.assert_not_called()

    def test_kokoro_failure_raises_original_gemini_error(self):
        """Gemini fails AND Kokoro fails AND no AWS keys -> the
        ORIGINAL Gemini error is raised (fail-loud, never masked)."""
        from agents.tts import synthesize_scene_audio

        gemini_err = RuntimeError("Gemini TTS request failed with HTTP 429")
        with patch("agents.tts.synthesize_speech_gemini",
                   side_effect=gemini_err), \
             patch("agents.tts.synthesize_speech_kokoro",
                   side_effect=RuntimeError("Kokoro down")), \
             patch("agents.tts.synthesize_speech_polly") as mock_polly:
            with self.assertRaises(RuntimeError) as ctx:
                synthesize_scene_audio("Hello.")
        self.assertIs(ctx.exception, gemini_err)
        mock_polly.assert_not_called()


if __name__ == "__main__":
    unittest.main()
